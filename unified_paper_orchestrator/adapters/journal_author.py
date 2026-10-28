from __future__ import annotations

import hashlib
import json
import re
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from ..config import (
    JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS,
    JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS,
    JOURNAL_AUTHOR_PIPELINE_LOCK_PATH,
    JOURNAL_AUTHOR_SDK_DISPATCH_PROTOCOL,
    JOURNAL_AUTHOR_SDK_JOURNAL_PATH,
    LEGACY_PROJECTS,
)
from ..io_utils import FileLock, backup_file, append_history, append_unique, copy_directory_contents, now_iso, read_json, read_text, rel_to, write_json_atomic, write_text
from ..journal_author_output_contract import validate_journal_author_output
from ..journal_submission_policy import (
    DEFAULT_AI_USE_POLICY,
    DEFAULT_AI_USE_SCOPE,
    DEFAULT_AI_USE_STATEMENT,
    DEFAULT_AI_USE_TOOLS,
    apply_submission_defaults_to_author_info,
    apply_submission_defaults_to_record,
    normalize_ai_use_policy,
)
from ..journal_author_workflow import (
    JournalAuthorWorkflowStore,
    journal_author_workflow_position_for_source,
    journal_author_workflow_with_mode,
)
from ..models import stage_status
from .base import LegacyAdapter


class JournalAuthorSDKRuntime(Protocol):
    def ensure_ready(self) -> dict[str, Any]: ...

    def start_background(
        self,
        *,
        priority_paper_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]: ...

    def request_pause(self) -> dict[str, Any]: ...

    def request_pause_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]: ...

    def revalidate_output(self, paper_id: str) -> dict[str, Any]: ...


def _default_sdk_runtime() -> JournalAuthorSDKRuntime:
    module = import_module("unified_paper_orchestrator.journal_author_sdk_runtime")
    return module.get_journal_author_sdk_runtime()  # type: ignore[no-any-return]


class JournalAuthorAdapter(LegacyAdapter):
    stage_name = "journal_author"
    id_key = "paper_id"

    def __init__(
        self,
        runtime: JournalAuthorSDKRuntime | None = None,
        workflow_store: JournalAuthorWorkflowStore | None = None,
    ) -> None:
        super().__init__(LEGACY_PROJECTS["journal_author"])
        self._runtime = runtime
        self.workflow_store = workflow_store or JournalAuthorWorkflowStore()

    @property
    def runtime(self) -> JournalAuthorSDKRuntime:
        if self._runtime is None:
            self._runtime = _default_sdk_runtime()
        return self._runtime

    def pipeline_mutation_scope(self) -> FileLock:
        return FileLock(JOURNAL_AUTHOR_PIPELINE_LOCK_PATH)

    def _require_runtime_ready(self) -> dict[str, Any]:
        result = self.runtime.ensure_ready()
        if not isinstance(result, dict) or result.get("ok") is False or result.get("authenticated") is False:
            raise RuntimeError("journal-author SDK account is not ready")
        return dict(result)

    def _pause_legacy_automation(self) -> dict[str, Any]:
        result = self.automation.set_enabled(self.stage_name, False, self.load_state())
        return dict(result) if isinstance(result, dict) else {"result": result}

    def _start_sdk_background(self, paper_ids: list[str] | None = None) -> dict[str, Any]:
        result = self.runtime.start_background(priority_paper_ids=paper_ids)
        normalized = dict(result) if isinstance(result, dict) else {"started": bool(result)}
        if normalized.get("ok") is False or not (
            normalized.get("started") is True or normalized.get("already_running") is True
        ):
            raise RuntimeError("journal-author SDK runtime did not acknowledge the background wakeup")
        return normalized

    def templates_path(self) -> Path:
        return self.root / "controller" / "author_templates.json"

    def legacy_template_path(self) -> Path:
        return self.root / "controller" / "author_template.json"

    def load_author_templates(self) -> dict[str, Any]:
        path = self.templates_path()
        data = read_json(path, {})
        if not isinstance(data, dict):
            data = {}

        templates = data.get("templates")
        if not isinstance(templates, list):
            templates = []

        by_id = {str(item.get("id")): item for item in templates if isinstance(item, dict) and item.get("id")}
        default_form = self._legacy_author_form() or self._blank_author_form("DRAFT")
        normalized: list[dict[str, Any]] = []
        for index in range(1, 4):
            template_id = f"template_{index}"
            source = by_id.get(template_id, {})
            if source.get("form"):
                form = self._normalize_author_form(source.get("form"))
            elif index == 1 and default_form:
                form = self._normalize_author_form(default_form)
            else:
                form = self._form_from_content(str(source.get("content") or ""))
            content = self._render_author_info(form)
            item = {
                "id": template_id,
                "name": str(source.get("name") or f"模板 {index}"),
                "form": form,
                "content": content,
                "updated_at": str(source.get("updated_at") or now_iso()),
            }
            normalized.append(item)

        default_id = str(data.get("default_template_id") or "template_1")
        if default_id not in {item["id"] for item in normalized}:
            default_id = "template_1"
        for item in normalized:
            item["is_default"] = item["id"] == default_id

        result = {
            "version": 2,
            "default_template_id": default_id,
            "templates": normalized,
            "updated_at": str(data.get("updated_at") or ""),
        }
        # Loading templates is a query.  Missing/legacy templates are exposed
        # as an in-memory DRAFT and are persisted only by an explicit save.
        return result

    def save_author_templates(self, data: dict[str, Any]) -> dict[str, Any]:
        templates = data.get("templates") if isinstance(data, dict) else []
        if not isinstance(templates, list):
            templates = []
        by_id = {str(item.get("id")): item for item in templates if isinstance(item, dict) and item.get("id")}
        default_id = str(data.get("default_template_id") or "template_1") if isinstance(data, dict) else "template_1"
        if default_id not in {"template_1", "template_2", "template_3"}:
            default_id = "template_1"

        normalized: list[dict[str, Any]] = []
        for index in range(1, 4):
            template_id = f"template_{index}"
            source = by_id.get(template_id, {})
            form = self._normalize_author_form(source.get("form") or self._form_from_content(str(source.get("content") or "")))
            normalized.append(
                {
                    "id": template_id,
                    "name": str(source.get("name") or f"模板 {index}"),
                    "is_default": template_id == default_id,
                    "form": form,
                    "content": self._render_author_info(form),
                    "updated_at": now_iso(),
                }
            )

        result = {
            "version": 2,
            "default_template_id": default_id,
            "templates": normalized,
            "updated_at": now_iso(),
        }
        write_json_atomic(self.templates_path(), result)
        return result

    def default_template(self) -> dict[str, Any]:
        data = self.load_author_templates()
        default_id = str(data.get("default_template_id") or "template_1")
        return self.template_by_id(default_id, data)

    def template_by_id(self, template_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or self.load_author_templates()
        for item in data.get("templates", []):
            if isinstance(item, dict) and str(item.get("id") or "") == template_id:
                return item
        templates = data.get("templates", [])
        if templates and isinstance(templates[0], dict):
            return templates[0]
        return {"id": "template_1", "name": "模板 1", "content": self._blank_author_info()}

    def _default_journal_preferences(self) -> dict[str, Any]:
        return {
            "quartiles": {"Q1": False, "Q2": False, "Q3": True, "Q4": True},
            "strategy": "Conservative non-OA/subscription-first target",
            "targeting_mode": "ai",
            "publication_tier": "sci",
            "selection_priority": "acceptance",
            "specific_journal": "",
            "allow_special_issue": True,
            "non_oa_required": True,
            "oa_policy": "subscription_first",
            "max_apc": "",
            "indexing": {"SCIE": False, "ESCI": False, "Scopus": True},
            "keywords": "",
            "preferred_journals": "",
            "rejected_journals": "",
            "retarget_notes": "",
            "notes": "",
        }

    def _normalize_journal_preferences(self, value: Any) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        result = self._default_journal_preferences()
        quartiles = source.get("quartiles") if isinstance(source.get("quartiles"), dict) else {}
        result["quartiles"] = {key: bool(quartiles.get(key, result["quartiles"][key])) for key in ("Q1", "Q2", "Q3", "Q4")}
        indexing = source.get("indexing") if isinstance(source.get("indexing"), dict) else {}
        result["indexing"] = {key: bool(indexing.get(key, result["indexing"][key])) for key in ("SCIE", "ESCI", "Scopus")}
        for key in (
            "strategy",
            "targeting_mode",
            "publication_tier",
            "selection_priority",
            "specific_journal",
            "oa_policy",
            "max_apc",
            "keywords",
            "preferred_journals",
            "rejected_journals",
            "retarget_notes",
            "notes",
        ):
            if key in source:
                result[key] = str(source.get(key) or "").strip()
        if result["targeting_mode"] not in {"specific", "ai"}:
            result["targeting_mode"] = "ai"
        if result["publication_tier"] not in {"sci", "chinese_core", "general"}:
            result["publication_tier"] = "sci"
        if result["selection_priority"] not in {"acceptance", "speed", "quality", "balanced"}:
            result["selection_priority"] = "acceptance"
        result["allow_special_issue"] = bool(
            source.get("allow_special_issue", result["allow_special_issue"])
        )
        result["non_oa_required"] = bool(source.get("non_oa_required", result["non_oa_required"]))
        if result["oa_policy"] not in {"subscription_first", "either", "oa_allowed", "oa_required"}:
            result["oa_policy"] = "subscription_first"
        return result

    def _render_journal_preferences(self, value: Any) -> str:
        preferences = self._normalize_journal_preferences(value)
        quartiles = [key for key, enabled in preferences["quartiles"].items() if enabled]
        indexing = [key for key, enabled in preferences["indexing"].items() if enabled]
        return "\n".join(
            [
                "# Journal Preferences",
                "",
                f"- Quartiles: {', '.join(quartiles) or 'No explicit preference'}",
                f"- Strategy: {preferences['strategy'] or 'Conservative safest-fit target'}",
                f"- Targeting mode: {preferences['targeting_mode']}",
                f"- Publication tier: {preferences['publication_tier']}",
                f"- Selection priority: {preferences['selection_priority']}",
                f"- Exact target journal: {preferences['specific_journal'] or 'Not specified'}",
                f"- Special issue allowed: {'Yes' if preferences['allow_special_issue'] else 'No'}",
                f"- Non-OA required: {'Yes' if preferences['non_oa_required'] else 'No'}",
                f"- OA policy: {preferences['oa_policy']}",
                f"- Maximum APC: {preferences['max_apc'] or 'Not specified'}",
                f"- Required indexing: {', '.join(indexing) or 'Not specified'}",
                f"- Topic keywords: {preferences['keywords'] or 'Not specified'}",
                f"- Preferred journals: {preferences['preferred_journals'] or 'None'}",
                f"- Rejected journals: {preferences['rejected_journals'] or 'None'}",
                f"- Retarget notes: {preferences['retarget_notes'] or 'None'}",
                f"- Notes: {preferences['notes'] or 'None'}",
                "",
                "These user preferences take priority when the protected journal-selection prompt allows a choice.",
            ]
        )

    def load_journal_preferences(self, paper_id: str) -> dict[str, Any]:
        state = self.load_state()
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        record = papers.get(paper_id, {}) if isinstance(papers.get(paper_id), dict) else {}
        return self._normalize_journal_preferences(record.get("journal_preferences"))

    def save_journal_preferences(self, paper_id: str, value: dict[str, Any]) -> dict[str, Any]:
        preferences = self._normalize_journal_preferences(value)
        with self.pipeline_mutation_scope():
            state = self.load_state()
            record = state.setdefault("papers", {}).setdefault(paper_id, {})
            record.setdefault("paper_id", paper_id)
            record["journal_preferences"] = preferences
            record["journal_preferences_updated_at"] = now_iso()
            paper_dir = self.paper_dir(paper_id)
            paper_dir.mkdir(parents=True, exist_ok=True)
            path = paper_dir / "journal_preferences.md"
            write_text(path, self._render_journal_preferences(preferences))
            self._write_metadata_fingerprint(paper_id, record)
            append_history(record, "journal_preferences_saved", "updated per-paper journal preferences")
            self.save_state(state)
        return {"status": "saved", "paper_id": paper_id, "path": str(path), "journal_preferences": preferences}

    def _default_submission_requirements(self) -> dict[str, Any]:
        return {
            "github_repo_url": "",
            "ai_use_policy": DEFAULT_AI_USE_POLICY,
            "ai_use_tools": DEFAULT_AI_USE_TOOLS,
            "ai_use_scope": DEFAULT_AI_USE_SCOPE,
            "ai_use_statement": DEFAULT_AI_USE_STATEMENT,
            "recommended_reviewers": "",
            "allow_double_blind": True,
            "template_notes": "",
            "submission_notes": "",
        }

    def _apply_submission_defaults(
        self,
        paper_id: str,
        record: dict[str, Any],
    ) -> None:
        identity = apply_submission_defaults_to_record(
            self.root,
            paper_id,
            record,
        )
        apply_submission_defaults_to_author_info(
            self.paper_dir(paper_id) / "author_info.md",
            identity,
        )

    def apply_submission_defaults(
        self,
        paper_ids: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        applied: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        with self.pipeline_mutation_scope():
            state = self.load_state()
            papers = state.setdefault("papers", {})
            for raw_id in paper_ids:
                paper_id = str(raw_id or "").strip()
                if (
                    not paper_id
                    or Path(paper_id).name != paper_id
                    or not self.paper_dir(paper_id).is_dir()
                ):
                    skipped.append(
                        {
                            "paper_id": paper_id,
                            "reason": "paper directory is missing or invalid",
                        }
                    )
                    continue
                record = papers.setdefault(paper_id, {})
                record.setdefault("paper_id", paper_id)
                self._update_author_info_record(record, paper_id)
                self._apply_submission_defaults(paper_id, record)
                fingerprint = self._write_metadata_fingerprint(paper_id, record)
                record["updated_at"] = now_iso()
                append_history(
                    record,
                    "submission_defaults_applied",
                    "filled planned paper repository and standing AI-use statement",
                )
                requirements = record.get("submission_requirements")
                normalized = requirements if isinstance(requirements, dict) else {}
                applied.append(
                    {
                        "paper_id": paper_id,
                        "github_repo_url": str(
                            normalized.get("github_repo_url") or ""
                        ),
                        "fingerprint": fingerprint,
                    }
                )
            if applied:
                self.save_state(state)
        return {
            "status": "submission_defaults_applied",
            "applied": applied,
            "skipped": skipped,
        }

    def _apply_template_record_fields(self, record: dict[str, Any], template: dict[str, Any]) -> None:
        form = self._normalize_author_form(template.get("form") or self._form_from_content(str(template.get("content") or "")))
        roles = [("first", "一作"), ("second", "二作"), ("third", "三作"), ("fourth", "四作")]
        authors = []
        for index, author in enumerate(form.get("authors", [])[: len(roles)]):
            role, role_label = roles[index]
            item = author if isinstance(author, dict) else {}
            authors.append(
                {
                    "role": role,
                    "role_label": role_label,
                    "name": str(item.get("name") or "").strip(),
                    "affiliation": str(item.get("affiliation") or "").strip(),
                    "email": str(item.get("email") or "").strip(),
                    "orcid": str(item.get("orcid") or "").strip(),
                    "corresponding": bool(item.get("corresponding")),
                }
            )
        record["authors"] = authors
        record["affiliations"] = form.get("affiliations", [])
        record["corresponding_author"] = form.get("corresponding_author", {})
        for key in (
            "author_contribution_statement",
            "funding",
            "conflict_of_interest",
            "data_availability",
            "code_availability",
            "ethics_statement",
            "recommended_reviewers",
            "ai_use_tools",
            "ai_use_scope",
            "ai_use_statement",
        ):
            record[key] = str(form.get(key) or "")
        record["ai_use_policy"] = normalize_ai_use_policy(form.get("ai_use_policy"))
        record.setdefault("journal_preferences", self._default_journal_preferences())
        requirements = self._default_submission_requirements()
        existing_requirements = record.get("submission_requirements")
        if isinstance(existing_requirements, dict):
            requirements.update(existing_requirements)
        requirements.update(
            {
                "recommended_reviewers": str(
                    form.get("recommended_reviewers") or ""
                ).strip(),
                "ai_use_policy": record["ai_use_policy"],
                "ai_use_tools": record["ai_use_tools"],
                "ai_use_scope": record["ai_use_scope"],
                "ai_use_statement": record["ai_use_statement"],
                "ai_use_source": "author_template",
            }
        )
        record["submission_requirements"] = requirements
        record["status"] = "READY" if form.get("status") == "READY" else "NEEDS_INFO"

    def _metadata_fingerprint_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        requirements = self._default_submission_requirements()
        existing_requirements = record.get("submission_requirements")
        if isinstance(existing_requirements, dict):
            requirements.update(existing_requirements)
        paper_id = str(record.get("paper_id") or "")
        source_inputs: list[dict[str, Any]] = []
        work_dir = self.paper_dir(paper_id) / "work"
        if paper_id and work_dir.is_dir():
            for path in sorted((item for item in work_dir.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold()):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                source_inputs.append(
                    {
                        "path": path.relative_to(work_dir).as_posix(),
                        "size": int(path.stat().st_size),
                        "sha256": digest,
                    }
                )
        return {
            "paper_id": paper_id,
            "paper_title": str(record.get("paper_title") or record.get("paper_id") or ""),
            "authors": record.get("authors", []),
            "affiliations": record.get("affiliations", []),
            "corresponding_author": record.get("corresponding_author", {}),
            "author_contribution_statement": record.get(
                "author_contribution_statement", ""
            ),
            "funding": record.get("funding", ""),
            "conflict_of_interest": record.get("conflict_of_interest", ""),
            "data_availability": record.get("data_availability", ""),
            "code_availability": record.get("code_availability", ""),
            "ethics_statement": record.get("ethics_statement", ""),
            "ai_use_policy": record.get("ai_use_policy", DEFAULT_AI_USE_POLICY),
            "ai_use_tools": record.get("ai_use_tools", DEFAULT_AI_USE_TOOLS),
            "ai_use_scope": record.get("ai_use_scope", DEFAULT_AI_USE_SCOPE),
            "ai_use_statement": record.get("ai_use_statement", DEFAULT_AI_USE_STATEMENT),
            "journal_preferences": record.get("journal_preferences", self._default_journal_preferences()),
            "submission_requirements": requirements,
            "metadata_status": str(record.get("status") or record.get("author_info_status") or ""),
            "source_inputs": source_inputs,
        }

    def _write_metadata_fingerprint(self, paper_id: str, record: dict[str, Any]) -> str:
        payload = self._metadata_fingerprint_payload(record)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        path = self.paper_dir(paper_id) / "submission_metadata_fingerprint.json"
        existing = read_json(path, {})
        if isinstance(existing, dict) and str(existing.get("sha256") or "").casefold() == digest.casefold() and existing.get("payload") == payload:
            record["input_fingerprint_sha256"] = digest
            return str(path)
        document = {
            "version": 1,
            "paper_id": paper_id,
            "created_at": now_iso(),
            "sha256": digest,
            "payload": payload,
        }
        write_text(path, json.dumps(document, ensure_ascii=False, indent=2))
        record["input_fingerprint_sha256"] = digest
        return str(path)

    def render_author_info_from_form(self, form: dict[str, Any]) -> str:
        return self._render_author_info(self._normalize_author_form(form))

    def apply_author_template(self, paper_ids: list[str], template_id: str, overwrite: bool = True) -> dict[str, Any]:
        template = self.template_by_id(template_id)
        content = self._normalize_author_info_content(str(template.get("content") or ""))
        state = self.load_state()
        papers = state.setdefault("papers", {})
        result: dict[str, Any] = {
            "status": "applied_author_template",
            "template_id": str(template.get("id") or template_id),
            "template_name": str(template.get("name") or template_id),
            "applied": [],
            "skipped": [],
            "cleared_blockers": [],
        }
        for raw_id in paper_ids:
            paper_id = str(raw_id or "").strip()
            if not paper_id:
                continue
            paper_dir = self.paper_dir(paper_id)
            paper_dir.mkdir(parents=True, exist_ok=True)
            author_info = paper_dir / "author_info.md"
            if author_info.exists() and not overwrite:
                result["skipped"].append({"paper_id": paper_id, "reason": "author_info_exists"})
                continue
            write_text(author_info, content)
            record = papers.setdefault(paper_id, {})
            record.setdefault("paper_id", paper_id)
            self._update_author_info_record(record, paper_id, template)
            self._apply_submission_defaults(paper_id, record)
            self._write_metadata_fingerprint(paper_id, record)
            cleared = self._clear_author_info_blocker_if_resolved(paper_id)
            cleared.extend(self._clear_metadata_fingerprint_blocker_if_resolved(paper_id))
            if cleared:
                result["cleared_blockers"].extend(cleared)
                if str(record.get("package_status") or "") == "BLOCKED":
                    record["package_status"] = ""
                record["last_result_status"] = "AUTHOR_INFO_READY"
                record["last_result_message"] = "author_info.md READY; cleared stale author-info blocker"
            append_history(record, "author_template_applied", f"applied {template.get('name') or template_id}")
            result["applied"].append(paper_id)
        if result["applied"]:
            self.save_state(state)
        return result

    def update_author_affiliation_fields(
        self,
        paper_ids: list[str],
        affiliation_index: int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Patch verified affiliation facts without replacing paper-specific fields."""

        index = int(affiliation_index)
        if index <= 0:
            raise ValueError("affiliation_index must be positive")
        allowed = {"institution", "department", "city", "country", "postal_code"}
        updates = {
            key: str(value or "").strip()
            for key, value in fields.items()
            if key in allowed
        }
        if not updates:
            raise ValueError("at least one affiliation field is required")
        result: dict[str, Any] = {
            "status": "author_affiliation_updated",
            "affiliation_index": index,
            "fields": sorted(updates),
            "updated": [],
            "skipped": {},
        }
        with self.pipeline_mutation_scope():
            state = self.load_state()
            papers = state.setdefault("papers", {})
            for raw_id in paper_ids:
                paper_id = str(raw_id or "").strip()
                if not paper_id:
                    continue
                author_info = self.paper_dir(paper_id) / "author_info.md"
                if not author_info.is_file():
                    result["skipped"][paper_id] = "author_info.md is missing"
                    continue
                form = self._normalize_author_form(
                    self._form_from_content(read_text(author_info))
                )
                affiliations = form.get("affiliations")
                if not isinstance(affiliations, list):
                    affiliations = []
                    form["affiliations"] = affiliations
                affiliation = next(
                    (
                        item
                        for item in affiliations
                        if isinstance(item, dict)
                        and int(item.get("index") or 0) == index
                    ),
                    None,
                )
                if affiliation is None:
                    affiliation = {
                        "index": index,
                        "institution": "",
                        "department": "",
                        "city": "",
                        "country": "",
                        "postal_code": "",
                    }
                    affiliations.append(affiliation)
                affiliation.update(updates)
                write_text(
                    author_info,
                    self._normalize_author_info_content(
                        self._render_author_info(form)
                    ),
                )
                record = papers.setdefault(paper_id, {})
                record.setdefault("paper_id", paper_id)
                self._update_author_info_record(record, paper_id)
                self._apply_submission_defaults(paper_id, record)
                fingerprint = self._write_metadata_fingerprint(paper_id, record)
                append_history(
                    record,
                    "author_affiliation_updated",
                    f"updated affiliation {index}: {', '.join(sorted(updates))}",
                )
                result["updated"].append(
                    {
                        "paper_id": paper_id,
                        "fingerprint": fingerprint,
                    }
                )
            if result["updated"]:
                self.save_state(state)
        return result

    def ensure_author_info_for_queue(self) -> dict[str, Any]:
        state = self.load_state()
        queue = self.scoped_paper_queue(state)
        if not queue:
            return {"status": "idle", "applied": []}
        template = self.default_template()
        content = self._normalize_author_info_content(str(template.get("content") or ""))
        papers = state.setdefault("papers", {})
        applied: list[str] = []
        refreshed: list[str] = []
        cleared_blockers: list[dict[str, str]] = []
        for paper_id in queue:
            paper_dir = self.paper_dir(paper_id)
            if not paper_dir.exists():
                continue
            author_info = paper_dir / "author_info.md"
            # Never replace an existing author file merely because it is
            # incomplete.  It may contain user-supplied facts that must be
            # corrected explicitly rather than discarded by a prefill pass.
            should_apply = not author_info.exists()
            if should_apply:
                write_text(author_info, content)
                applied.append(paper_id)
            record = papers.setdefault(paper_id, {})
            record.setdefault("paper_id", paper_id)
            self._update_author_info_record(record, paper_id, template if paper_id in applied else None)
            self._apply_submission_defaults(paper_id, record)
            self._write_metadata_fingerprint(paper_id, record)
            cleared = self._clear_author_info_blocker_if_resolved(paper_id)
            cleared.extend(self._clear_metadata_fingerprint_blocker_if_resolved(paper_id))
            if cleared:
                cleared_blockers.extend(cleared)
                if str(record.get("package_status") or "") == "BLOCKED":
                    record["package_status"] = ""
                record["last_result_status"] = "AUTHOR_INFO_READY"
                record["last_result_message"] = "author_info.md READY; cleared stale author-info blocker"
            refreshed.append(paper_id)
        if applied or refreshed or cleared_blockers:
            self.save_state(state)
        return {"status": "author_info_ready_checked", "applied": applied, "checked": refreshed, "cleared_blockers": cleared_blockers}

    def _ensure_parallel_scheduler_settings(self, state: dict[str, Any]) -> dict[str, Any]:
        settings = self.scheduler_settings(state)
        try:
            limit = int(settings.get("max_parallel_workers", JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS))
        except (TypeError, ValueError):
            limit = JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS
        settings["one_action_per_run"] = False
        settings["max_parallel_workers"] = max(1, min(JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS, limit))
        settings["dispatch_mode"] = "sdk_event_driven"
        settings["journal_author_dispatch_protocol"] = JOURNAL_AUTHOR_SDK_DISPATCH_PROTOCOL
        settings["parallel_policy"] = "one fixed SDK session per paper; program-owned admission and concurrency"
        return settings

    def start(self) -> dict[str, Any]:
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        prefill = self.ensure_author_info_for_queue()
        runtime = self._start_sdk_background()
        return {
            "ok": True,
            "stage": self.stage_name,
            "enabled": True,
            "account": account,
            "runtime": runtime,
            "automation": legacy_pause,
            "author_info_prefill": prefill,
        }

    def resume_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要继续的选刊作者论文")
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        runtime = self._start_sdk_background(ids)
        return {
            "ok": True,
            "stage": self.stage_name,
            "scope": "selected_papers",
            "paper_ids": ids,
            "account": account,
            "automation": legacy_pause,
            "runtime": runtime,
        }

    def pause(self) -> dict[str, Any]:
        legacy_pause = self._pause_legacy_automation()
        runtime = self.runtime.request_pause()
        return {
            "ok": True,
            "stage": self.stage_name,
            "enabled": False,
            "paused": True,
            "status": "pause_requested",
            "runtime": runtime,
            "automation": legacy_pause,
        }

    def pause_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要暂停的选刊作者论文")
        runtime = self.runtime.request_pause_papers(ids)
        return {
            "ok": True,
            "stage": self.stage_name,
            "paused": True,
            "scope": "selected_papers",
            "paper_ids": ids,
            "runtime": runtime,
        }

    def _blank_author_info(self) -> str:
        return self._render_author_info(self._blank_author_form())

    def _blank_author_form(self, status: str = "DRAFT") -> dict[str, Any]:
        return {
            "status": status,
            "authors": [
                {"prefix": "", "name": "", "affiliation": "", "email": "", "orcid": "", "corresponding": False},
                {"prefix": "", "name": "", "affiliation": "", "email": "", "orcid": "", "corresponding": False},
                {"prefix": "", "name": "", "affiliation": "", "email": "", "orcid": "", "corresponding": False},
                {"prefix": "", "name": "", "affiliation": "", "email": "", "orcid": "", "corresponding": False},
            ],
            "affiliations": [
                {"index": 1, "institution": "", "department": "", "city": "", "country": "", "postal_code": ""},
                {"index": 2, "institution": "", "department": "", "city": "", "country": "", "postal_code": ""},
                {"index": 3, "institution": "", "department": "", "city": "", "country": "", "postal_code": ""},
            ],
            "corresponding_author": {"prefix": "", "name": "", "email": ""},
            "author_contribution_statement": "",
            "funding": "",
            "conflict_of_interest": "",
            "data_availability": "",
            "code_availability": "",
            "ethics_statement": "",
            "recommended_reviewers": "",
            "ai_use_policy": DEFAULT_AI_USE_POLICY,
            "ai_use_tools": DEFAULT_AI_USE_TOOLS,
            "ai_use_scope": DEFAULT_AI_USE_SCOPE,
            "ai_use_statement": DEFAULT_AI_USE_STATEMENT,
        }

    def _legacy_author_form(self) -> dict[str, Any] | None:
        data = read_json(self.legacy_template_path(), {})
        if not isinstance(data, dict) or not data:
            return None
        form = self._blank_author_form(str(data.get("status") or "DRAFT"))
        form["authors"] = data.get("authors") if isinstance(data.get("authors"), list) else form["authors"]
        form["affiliations"] = data.get("affiliations") if isinstance(data.get("affiliations"), list) else form["affiliations"]
        form["corresponding_author"] = (
            data.get("corresponding_author") if isinstance(data.get("corresponding_author"), dict) else form["corresponding_author"]
        )
        for key in (
            "author_contribution_statement",
            "funding",
            "conflict_of_interest",
            "data_availability",
            "code_availability",
            "ethics_statement",
            "recommended_reviewers",
            "ai_use_tools",
            "ai_use_scope",
            "ai_use_statement",
        ):
            if str(data.get(key) or "").strip():
                form[key] = str(data.get(key) or "")
        form["ai_use_policy"] = normalize_ai_use_policy(data.get("ai_use_policy"))
        return self._normalize_author_form(form)

    def _normalize_author_form(self, value: Any) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        form = self._blank_author_form(str(source.get("status") or "DRAFT"))
        status = str(source.get("status") or form["status"]).strip().upper()
        form["status"] = "READY" if status == "READY" else "DRAFT"

        authors = source.get("authors")
        if isinstance(authors, list):
            normalized_authors = []
            for author in authors[:4]:
                item = author if isinstance(author, dict) else {}
                normalized_authors.append(
                    {
                        "prefix": str(
                            item.get("prefix")
                            or item.get("honorific")
                            or item.get("author_title")
                            or ""
                        ).strip(),
                        "name": str(item.get("name") or "").strip(),
                        "affiliation": str(item.get("affiliation") or "").strip(),
                        "email": str(item.get("email") or "").strip(),
                        "orcid": str(item.get("orcid") or "").strip(),
                        "corresponding": bool(item.get("corresponding")),
                    }
                )
            while len(normalized_authors) < 4:
                normalized_authors.append({"prefix": "", "name": "", "affiliation": "", "email": "", "orcid": "", "corresponding": False})
            form["authors"] = normalized_authors

        affiliations = source.get("affiliations")
        if isinstance(affiliations, list):
            normalized_affiliations = []
            for index, affiliation in enumerate(affiliations[:3], start=1):
                item = affiliation if isinstance(affiliation, dict) else {}
                normalized_affiliations.append(
                    {
                        "index": int(item.get("index") or index),
                        "institution": str(item.get("institution") or "").strip(),
                        "department": str(item.get("department") or "").strip(),
                        "city": str(item.get("city") or "").strip(),
                        "country": str(item.get("country") or "").strip(),
                        "postal_code": str(item.get("postal_code") or "").strip(),
                    }
                )
            while len(normalized_affiliations) < 3:
                index = len(normalized_affiliations) + 1
                normalized_affiliations.append(
                    {"index": index, "institution": "", "department": "", "city": "", "country": "", "postal_code": ""}
                )
            form["affiliations"] = normalized_affiliations

        corresponding = source.get("corresponding_author") if isinstance(source.get("corresponding_author"), dict) else {}
        corresponding_prefix = str(
            corresponding.get("prefix")
            or corresponding.get("honorific")
            or corresponding.get("author_title")
            or ""
        ).strip()
        form["corresponding_author"] = {
            "name": str(corresponding.get("name") or "").strip(),
            "email": str(corresponding.get("email") or "").strip(),
        }
        if corresponding_prefix:
            form["corresponding_author"]["prefix"] = corresponding_prefix
        active_corresponding = [
            author
            for author in form["authors"]
            if bool(author.get("corresponding"))
            and str(author.get("name") or "").strip()
        ]
        if len(active_corresponding) == 1:
            marked = active_corresponding[0]
            if not form["corresponding_author"]["name"]:
                form["corresponding_author"]["name"] = str(
                    marked.get("name") or ""
                ).strip()
            if not form["corresponding_author"]["email"]:
                form["corresponding_author"]["email"] = str(
                    marked.get("email") or ""
                ).strip()
            if not form["corresponding_author"].get("prefix"):
                marked_prefix = str(marked.get("prefix") or "").strip()
                if marked_prefix:
                    form["corresponding_author"]["prefix"] = marked_prefix
        for key in (
            "author_contribution_statement",
            "funding",
            "conflict_of_interest",
            "data_availability",
            "code_availability",
            "ethics_statement",
            "recommended_reviewers",
            "ai_use_tools",
            "ai_use_scope",
            "ai_use_statement",
        ):
            if key in source:
                form[key] = str(source.get(key) or "").strip()
        form["ai_use_policy"] = normalize_ai_use_policy(
            source.get("ai_use_policy") or form.get("ai_use_policy")
        )
        if form["status"] == "READY" and self._form_readiness_issues(form):
            form["status"] = "DRAFT"
        return form

    def _form_readiness_issues(self, form: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        authors = [item for item in form.get("authors", []) if isinstance(item, dict)]
        active_authors = [
            item
            for item in authors
            if any(str(item.get(key) or "").strip() for key in ("name", "affiliation", "email", "orcid"))
            or bool(item.get("corresponding"))
        ]
        if not active_authors:
            issues.append("missing author name")
        else:
            for index, author in enumerate(active_authors, start=1):
                if not str(author.get("name") or "").strip():
                    issues.append(f"author {index} missing name")
                if not str(author.get("email") or "").strip():
                    issues.append(f"author {index} missing email")
                if not str(author.get("affiliation") or "").strip():
                    issues.append(f"author {index} missing affiliation")

        corresponding_authors = [
            author for author in active_authors if bool(author.get("corresponding"))
        ]
        corresponding = (
            form.get("corresponding_author")
            if isinstance(form.get("corresponding_author"), dict)
            else {}
        )
        corresponding_name = str(corresponding.get("name") or "").strip()
        corresponding_email = str(corresponding.get("email") or "").strip()
        if not corresponding_authors:
            issues.append("missing corresponding author")
        if not corresponding_name:
            issues.append("missing corresponding author name")
        if not corresponding_email:
            issues.append("missing corresponding author email")
        if corresponding_authors and corresponding_name and corresponding_email:
            matching_corresponding = any(
                str(author.get("name") or "").strip().casefold()
                == corresponding_name.casefold()
                and str(author.get("email") or "").strip().casefold()
                == corresponding_email.casefold()
                for author in corresponding_authors
            )
            if not matching_corresponding:
                issues.append(
                    "corresponding author identity does not match an author "
                    "marked as corresponding"
                )

        affiliations = [item for item in form.get("affiliations", []) if isinstance(item, dict)]
        if not any(str(item.get("institution") or "").strip() for item in affiliations):
            issues.append("missing institution")
        return issues

    def _form_from_content(self, content: str) -> dict[str, Any]:
        text = content or ""
        form = self._blank_author_form()
        if re.search(r"(?im)^\s*Status\s*:\s*READY\s*$", text):
            form["status"] = "READY"

        authors_section = self._section_text(text, "Authors")
        parsed_authors: list[dict[str, Any]] = []
        current_author: dict[str, Any] | None = None
        for line in authors_section.splitlines():
            name_match = re.match(
                r"^\s*\d+\.\s*(?:Role\s*:\s*.*?\s+)?Name\s*:\s*(.*)$",
                line,
                re.IGNORECASE,
            )
            if name_match:
                if current_author is not None:
                    parsed_authors.append(current_author)
                current_author = {
                    "prefix": "",
                    "name": name_match.group(1).strip(),
                    "affiliation": "",
                    "email": "",
                    "orcid": "",
                    "corresponding": False,
                }
                continue
            role_match = re.match(
                r"^\s*\d+\.\s*Role\s*:",
                line,
                re.IGNORECASE,
            )
            if role_match:
                if current_author is not None:
                    parsed_authors.append(current_author)
                current_author = {
                    "prefix": "",
                    "name": "",
                    "affiliation": "",
                    "email": "",
                    "orcid": "",
                    "corresponding": False,
                }
                continue
            if current_author is None:
                continue
            field_match = re.match(
                r"^\s*(Prefix|Honorific|Name|Affiliation|Email|ORCID|Corresponding Author)\s*:\s*(.*)$",
                line,
                re.IGNORECASE,
            )
            if not field_match:
                continue
            key = field_match.group(1).strip().lower()
            value = field_match.group(2).strip()
            if key in {"prefix", "honorific"}:
                current_author["prefix"] = value
            elif key == "name":
                current_author["name"] = value
            elif key == "affiliation":
                current_author["affiliation"] = value
            elif key == "email":
                current_author["email"] = value
            elif key == "orcid":
                current_author["orcid"] = value
            else:
                current_author["corresponding"] = value.lower() in {"yes", "true", "1"}
        if current_author is not None:
            parsed_authors.append(current_author)
        if parsed_authors:
            form["authors"] = parsed_authors

        affiliations_section = self._section_text(text, "Affiliations")
        parsed_affiliations: list[dict[str, Any]] = []
        current_affiliation: dict[str, Any] | None = None
        for line in affiliations_section.splitlines():
            institution_match = re.match(r"^\s*(\d+)\.\s*Institution\s*:\s*(.*)$", line, re.IGNORECASE)
            if institution_match:
                if current_affiliation is not None:
                    parsed_affiliations.append(current_affiliation)
                current_affiliation = {
                    "index": int(institution_match.group(1)),
                    "institution": institution_match.group(2).strip(),
                    "department": "",
                    "city": "",
                    "country": "",
                    "postal_code": "",
                }
                continue
            if current_affiliation is None:
                continue
            field_match = re.match(r"^\s*(Department|City|Country|Postal Code)\s*:\s*(.*)$", line, re.IGNORECASE)
            if field_match:
                key = field_match.group(1).strip().lower().replace(" ", "_")
                current_affiliation[key] = field_match.group(2).strip()
        if current_affiliation is not None:
            parsed_affiliations.append(current_affiliation)
        if parsed_affiliations:
            form["affiliations"] = parsed_affiliations

        for key, heading in (
            ("author_contribution_statement", "Author Contributions"),
            ("funding", "Funding"),
            ("conflict_of_interest", "Conflict of Interest"),
            ("data_availability", "Data Availability"),
            ("code_availability", "Code Availability"),
            ("ethics_statement", "Ethics Statement"),
            ("recommended_reviewers", "Recommended Reviewers"),
        ):
            value = self._section_text(text, heading)
            if value:
                form[key] = value
        ai_section = self._section_text(text, "AI Use Statement") or self._section_text(
            text,
            "AI Use Declaration",
        )
        if ai_section:
            policy = re.search(
                r"(?im)^\s*Insertion\s+Policy\s*:\s*(.+?)\s*$",
                ai_section,
            )
            tools = re.search(r"(?im)^\s*Tools\s*:\s*(.+?)\s*$", ai_section)
            scope = re.search(r"(?im)^\s*Scope\s*:\s*(.+?)\s*$", ai_section)
            statement = re.search(
                r"(?is)(?:^|\n)\s*Statement\s*:\s*(.*?)\s*$",
                ai_section,
            )
            if policy:
                form["ai_use_policy"] = normalize_ai_use_policy(policy.group(1))
            if tools:
                form["ai_use_tools"] = tools.group(1).strip()
            if scope:
                form["ai_use_scope"] = scope.group(1).strip()
            if statement:
                form["ai_use_statement"] = statement.group(1).strip()
            elif not any((policy, tools, scope)):
                form["ai_use_statement"] = ai_section.strip()
        corresponding = self._section_text(text, "Corresponding Author")
        if corresponding:
            prefix = re.search(
                r"(?im)^\s*(?:Prefix|Honorific)\s*:\s*(.+)$",
                corresponding,
            )
            name = re.search(r"(?im)^\s*Name\s*:\s*(.+)$", corresponding)
            email = re.search(r"(?im)^\s*Email\s*:\s*(.+)$", corresponding)
            form["corresponding_author"] = {
                "prefix": prefix.group(1).strip() if prefix else "",
                "name": name.group(1).strip() if name else "",
                "email": email.group(1).strip() if email else "",
            }
        return self._normalize_author_form(form)

    def _section_text(self, text: str, heading: str) -> str:
        match = re.search(rf"(?is)^##\s+{re.escape(heading)}\s*(.*?)(?=^##\s+|\Z)", text or "", flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    def _render_author_info(self, form: dict[str, Any]) -> str:
        data = self._normalize_author_form(form)
        lines = ["# Author Information", "", f"Status: {data['status']}", "", "## Authors", ""]
        authors = [item for item in data.get("authors", []) if str(item.get("name") or "").strip()]
        if not authors:
            authors = data.get("authors", [])[:1]
        for index, author in enumerate(authors, start=1):
            lines.extend(
                [
                    f"{index}. Name: {author.get('name', '')}",
                    f"   Prefix: {author.get('prefix', '')}",
                    f"   Affiliation: {author.get('affiliation', '')}",
                    f"   Email: {author.get('email', '')}",
                    f"   ORCID: {author.get('orcid', '')}",
                    f"   Corresponding Author: {'yes' if author.get('corresponding') else 'no'}",
                    "",
                ]
            )

        lines.extend(["## Affiliations", ""])
        affiliations = [item for item in data.get("affiliations", []) if str(item.get("institution") or "").strip()]
        if not affiliations:
            affiliations = data.get("affiliations", [])[:1]
        for index, affiliation in enumerate(affiliations, start=1):
            lines.extend(
                [
                    f"{index}. Institution: {affiliation.get('institution', '')}",
                    f"   Department: {affiliation.get('department', '')}",
                    f"   City: {affiliation.get('city', '')}",
                    f"   Country: {affiliation.get('country', '')}",
                    f"   Postal Code: {affiliation.get('postal_code', '')}",
                    "",
                ]
            )

        corresponding = data.get("corresponding_author", {})
        if not corresponding.get("name") or not corresponding.get("email"):
            for author in data.get("authors", []):
                if author.get("corresponding"):
                    corresponding = {"name": author.get("name", ""), "email": author.get("email", "")}
                    break
        lines.extend(
            [
                "## Corresponding Author",
                "",
                f"Prefix: {corresponding.get('prefix', '')}",
                f"Name: {corresponding.get('name', '')}",
                f"Email: {corresponding.get('email', '')}",
                "",
            ]
        )
        lines.extend(
            [
                "## Author Contributions",
                "",
                str(data.get("author_contribution_statement") or "").strip(),
                "",
            ]
        )
        for heading, key in (
            ("Funding", "funding"),
            ("Conflict of Interest", "conflict_of_interest"),
            ("Data Availability", "data_availability"),
            ("Code Availability", "code_availability"),
            ("Ethics Statement", "ethics_statement"),
            ("Recommended Reviewers", "recommended_reviewers"),
        ):
            lines.extend([f"## {heading}", "", str(data.get(key) or "").strip(), ""])
        lines.extend(
            [
                "## AI Use Statement",
                "",
                f"Insertion Policy: {normalize_ai_use_policy(data.get('ai_use_policy'))}",
                f"Tools: {str(data.get('ai_use_tools') or '').strip()}",
                f"Scope: {str(data.get('ai_use_scope') or '').strip()}",
                "",
                "Statement:",
                str(data.get("ai_use_statement") or "").strip(),
                "",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _normalize_author_info_content(self, content: str) -> str:
        text = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            text = self._blank_author_info()
        lines = text.splitlines()
        has_status = any(re.match(r"^\s*Status\s*:", line, re.IGNORECASE) for line in lines[:12])
        if not has_status:
            insert_at = 1 if lines and lines[0].lstrip().startswith("#") else 0
            lines.insert(insert_at, "Status: DRAFT")
        normalized = "\n".join(lines).rstrip() + "\n"
        if re.search(r"(?im)^\s*Status\s*:\s*READY\s*$", normalized):
            form = self._form_from_content(normalized)
            if self._form_readiness_issues(form):
                normalized = re.sub(
                    r"(?im)^(\s*Status\s*:)\s*READY\s*$",
                    r"\1 DRAFT",
                    normalized,
                    count=1,
                )
        return normalized

    def author_info_issues(self, content: str) -> list[str]:
        issues: list[str] = []
        if not re.search(r"(?im)^\s*Status\s*:\s*READY\s*$", content or ""):
            issues.append("Status is not READY")
        if re.search(r"(?i)\bTODO\b", content or ""):
            issues.append("contains TODO")
        form = self._form_from_content(content or "")
        for issue in self._form_readiness_issues(form):
            if issue not in issues:
                issues.append(issue)
        return issues

    def _clear_author_info_blocker_if_resolved(self, paper_id: str) -> list[dict[str, str]]:
        author_info = self.paper_dir(paper_id) / "author_info.md"
        if self.author_info_issues(read_text(author_info)):
            return []
        blocker = self.paper_dir(paper_id) / "package_blocker.md"
        if not blocker.exists():
            return []
        text = read_text(blocker)
        lower = text.lower()
        if "author_info" not in lower:
            return []
        hard_blocker_patterns = (
            r"final decision[^\n\r]*do not submit",
            r"do not submit yet",
            r"not\s+`?recommended`?",
            r"missing\s+(source|file|pdf|tex|figure)",
            r"compilation\s+(failed|error)",
            r"dry[- ]run\s+(failed|error)",
        )
        if any(re.search(pattern, lower, re.IGNORECASE) for pattern in hard_blocker_patterns):
            return []
        backup = backup_file(blocker, f"{self.stage_name}_package_blocker")
        try:
            blocker.unlink()
        except OSError:
            return []
        return [{"path": str(blocker), "backup": backup}]

    def _clear_metadata_fingerprint_blocker_if_resolved(self, paper_id: str) -> list[dict[str, str]]:
        fingerprint = self.paper_dir(paper_id) / "submission_metadata_fingerprint.json"
        if not fingerprint.exists():
            return []
        blocker = self.paper_dir(paper_id) / "package_blocker.md"
        if not blocker.exists():
            return []
        text = read_text(blocker)
        lower = text.lower()
        if "submission_metadata_fingerprint" not in lower:
            return []
        hard_blocker_patterns = (
            r"final decision[^\n\r]*do not submit",
            r"do not submit yet",
            r"not\s+`?recommended`?",
            r"author_info[^\n\r]*(not ready|does not contain|todo|blank)",
            r"missing\s+(source|file|pdf|tex|figure)",
            r"compilation\s+(failed|error)",
            r"dry[- ]run\s+(failed|error)",
        )
        if any(re.search(pattern, lower, re.IGNORECASE) for pattern in hard_blocker_patterns):
            return []
        backup = backup_file(blocker, f"{self.stage_name}_package_blocker")
        try:
            blocker.unlink()
        except OSError:
            return []
        return [{"path": str(blocker), "backup": backup}]

    def _update_author_info_record(self, record: dict[str, Any], paper_id: str, template: dict[str, Any] | None = None) -> None:
        author_info = self.paper_dir(paper_id) / "author_info.md"
        content = read_text(author_info)
        issues = self.author_info_issues(content)
        record["author_info_path"] = rel_to(author_info, self.root) if author_info.exists() else ""
        record["author_info_status"] = "READY" if not issues else "DRAFT"
        record["author_info_issues"] = issues
        record["author_info_updated_at"] = now_iso()
        if template:
            self._apply_template_record_fields(record, template)
            record["author_template_id"] = str(template.get("id") or "")
            record["author_template_name"] = str(template.get("name") or "")
        else:
            form = self._form_from_content(content)
            record["ai_use_policy"] = normalize_ai_use_policy(
                form.get("ai_use_policy")
            )
            record["ai_use_tools"] = str(form.get("ai_use_tools") or "")
            record["ai_use_scope"] = str(form.get("ai_use_scope") or "")
            record["ai_use_statement"] = str(form.get("ai_use_statement") or "")
            requirements = self._default_submission_requirements()
            existing_requirements = record.get("submission_requirements")
            if isinstance(existing_requirements, dict):
                requirements.update(existing_requirements)
            requirements.update(
                {
                    "recommended_reviewers": str(
                        form.get("recommended_reviewers") or ""
                    ).strip(),
                    "ai_use_policy": record["ai_use_policy"],
                    "ai_use_tools": record["ai_use_tools"],
                    "ai_use_scope": record["ai_use_scope"],
                    "ai_use_statement": record["ai_use_statement"],
                    "ai_use_source": "author_info",
                }
            )
            record["submission_requirements"] = requirements

    def all_paper_ids(self) -> set[str]:
        state = self.load_state()
        ids = set(str(item) for item in state.get("papers", {}).keys())
        if self.papers_dir.exists():
            ids.update(child.name for child in self.papers_dir.iterdir() if child.is_dir())
        return ids

    def paper_dir(self, paper_id: str) -> Path:
        return self.papers_dir / paper_id

    def package_dir(self, paper_id: str) -> Path:
        return self.paper_dir(paper_id) / "package"

    def main_pdf(self, paper_id: str) -> Path:
        return self.package_dir(paper_id) / "main.pdf"

    def stage_for(self, paper_id: str) -> dict[str, Any]:
        state = self.load_state()
        record = state.get("papers", {}).get(paper_id, {})
        if not isinstance(record, dict):
            record = {}
        journal = read_json(JOURNAL_AUTHOR_SDK_JOURNAL_PATH, {"papers": {}})
        journal_papers = journal.get("papers", {}) if isinstance(journal, dict) else {}
        sdk_record = journal_papers.get(paper_id, {}) if isinstance(journal_papers, dict) else {}
        if not isinstance(sdk_record, dict):
            sdk_record = {}
        queue = [str(item) for item in state.get("paper_queue", [])]
        queue_position = f"{queue.index(paper_id) + 1}/{len(queue)}" if paper_id in queue else ""
        package_status = str(record.get("package_status") or "")
        workflow_status = str(record.get("workflow_status") or record.get("status") or "")
        sdk_status = str(sdk_record.get("status") or "")
        contract = validate_journal_author_output(self.root, paper_id)
        if workflow_status in {"deleted", "stopped"}:
            status = workflow_status
        elif sdk_status in {
            "creating_thread",
            "starting",
            "running",
            "waiting_event",
            "queued",
            "paused",
            "blocked",
            "needs_user",
            "selection_complete",
        }:
            status = sdk_status
        elif sdk_status == "complete" and contract.ok and contract.package_status == "READY_TO_SUBMIT":
            status = "complete"
        elif paper_id in queue or workflow_status in {"queued", "running"}:
            status = "queued" if paper_id in queue else workflow_status
        elif package_status == "BLOCKED":
            status = "blocked"
        elif contract.ok:
            status = "complete" if contract.package_status == "READY_TO_SUBMIT" else "needs_user"
        elif self.paper_dir(paper_id).exists():
            status = "incomplete"
        else:
            status = "missing"
        detail = str(
            sdk_record.get("blocker_message")
            or record.get("last_result_message")
            or record.get("next_action")
            or package_status
            or ("；".join(contract.issues[:3]) if status not in {"queued", "running", "starting"} else "")
        )
        projection = stage_status(
            status,
            label=self.label,
            detail=detail,
            artifact=str(self.package_dir(paper_id) if self.package_dir(paper_id).exists() else ""),
            updated_at=str(sdk_record.get("updated_at") or record.get("updated_at") or record.get("last_completed_at") or ""),
            raw_status=sdk_status or workflow_status or package_status or status,
            queue_position=queue_position,
            scheduler_mode=self.scheduler_mode(state),
            step_done=int(sdk_record.get("legacy_step_done") or record.get("step_done") or contract.step_done),
        )
        projection.update(
            {
                "current_step": int(sdk_record.get("active_step") or record.get("sdk_current_step") or 0),
                "thread_id": str(sdk_record.get("thread_id") or record.get("thread_id") or ""),
                "output_contract": contract.as_dict(),
                "package_status": str(sdk_record.get("package_status") or package_status),
                "ready_to_submit": bool(contract.ok and contract.package_status == "READY_TO_SUBMIT"),
            }
        )
        return projection

    def _ensure_import(self, paper_id: str, submit_dir: Path) -> dict[str, str]:
        paper_dir = self.paper_dir(paper_id)
        source_submit = paper_dir / "source_submit"
        work_dir = paper_dir / "work"
        copied_source = copy_directory_contents(submit_dir, source_submit)
        copied_work = copy_directory_contents(submit_dir, work_dir)
        author_info = paper_dir / "author_info.md"
        author_info_action = "kept"
        # Import may create a conservative DRAFT, but it must not overwrite an
        # existing file with validation issues.
        if not author_info.exists():
            write_text(author_info, self._normalize_author_info_content(str(self.default_template().get("content") or "")))
            author_info_action = "default_template_applied"
        cleared_blockers = self._clear_author_info_blocker_if_resolved(paper_id)
        write_text(
            paper_dir / "import_report.md",
            "\n".join(
                [
                    "# 导入报告",
                    "",
                    f"- paper_id: `{paper_id}`",
                    f"- source: `{submit_dir}`",
                    f"- imported_at: `{now_iso()}`",
                    f"- source_submit_files: {len(copied_source)}",
                    f"- work_files: {len(copied_work)}",
                    f"- author_info: {author_info_action}",
                    f"- cleared_author_info_blockers: {len(cleared_blockers)}",
                ]
            ),
        )
        return {
            "source_submit": str(source_submit),
            "work": str(work_dir),
            "author_info": author_info_action,
            "cleared_author_info_blockers": str(len(cleared_blockers)),
        }

    def queue(
        self,
        paper_id: str,
        upstream_artifact: str = "",
        *,
        run_mode: str = "full",
    ) -> dict[str, Any]:
        mode = str(run_mode or "full").strip().casefold()
        if mode not in {"full", "selection", "package", "retarget"}:
            raise ValueError("选刊作者运行方式必须是 full、selection 或 package")
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        workflow = self.workflow_store.executable_snapshot()
        if mode in {"selection", "package", "retarget"}:
            workflow = journal_author_workflow_with_mode(workflow, "split")
        if mode == "selection":
            run_from_position = journal_author_workflow_position_for_source(workflow, 2)
            run_until_position = run_from_position
            action_label = "只重新选刊（原 Step 2）"
        elif mode == "package":
            run_from_position = journal_author_workflow_position_for_source(workflow, 3)
            run_until_position = run_from_position
            action_label = "只重新生成投稿包（原 Step 3）"
        elif mode == "retarget":
            run_from_position = journal_author_workflow_position_for_source(workflow, 2)
            run_until_position = journal_author_workflow_position_for_source(workflow, 3)
            action_label = "Retarget journal and rebuild the submission package (Steps 2-3)"
        else:
            run_from_position = 1
            run_until_position = int(workflow.get("step_count") or 1)
            action_label = "执行完整选刊作者流程"
        # ``Path()`` means the current working directory, not an absent path.
        # Treat an omitted upstream artifact as genuinely absent so a normal
        # queue request can never copy the whole application workspace into a
        # paper's import directories.
        submit_dir = Path(upstream_artifact) if upstream_artifact else None
        with FileLock(JOURNAL_AUTHOR_PIPELINE_LOCK_PATH):
            state = self.load_state()
            settings = self._ensure_parallel_scheduler_settings(state)
            settings["scheduling_mode"] = "auto"
            settings["sdk_pause_requested"] = False
            paused = [
                str(value)
                for value in settings.get("sdk_paused_paper_ids", [])
                if str(value) and str(value) != paper_id
            ]
            settings["sdk_paused_paper_ids"] = list(dict.fromkeys(paused))
            self.set_targeted_run_scope(state, paper_id)
            import_paths: dict[str, str] = {}
            if submit_dir is not None and submit_dir.exists() and submit_dir.is_dir():
                import_paths = self._ensure_import(paper_id, submit_dir)

            papers = state.setdefault("papers", {})
            record = papers.setdefault(paper_id, {})
            record.setdefault("paper_id", paper_id)
            record["workflow_status"] = "queued"
            record["last_result_status"] = "QUEUED"
            record["last_result_message"] = f"V2 SDK 已排队：{action_label}"
            record["updated_at"] = now_iso()
            record["workflow_snapshot"] = workflow
            record["workflow_version_id"] = str(workflow.get("version_id") or "")
            record["run_mode"] = mode
            record["run_from_position"] = run_from_position
            record["run_until_position"] = run_until_position
            record["reuse_valid_artifacts"] = mode == "full"
            if import_paths:
                record["source_submit_dir"] = rel_to(Path(import_paths["source_submit"]), self.root)
                record["work_dir"] = rel_to(Path(import_paths["work"]), self.root)
            template = self.default_template() if import_paths.get("author_info") == "default_template_applied" else None
            self._update_author_info_record(record, paper_id, template)
            self._apply_submission_defaults(paper_id, record)
            self._write_metadata_fingerprint(paper_id, record)
            cleared = self._clear_author_info_blocker_if_resolved(paper_id)
            cleared.extend(self._clear_metadata_fingerprint_blocker_if_resolved(paper_id))
            if cleared and str(record.get("package_status") or "") == "BLOCKED":
                record["package_status"] = ""
            append_history(record, "unified_queued", f"统一总控加入 V2 SDK 队列：{action_label}")

            params = {
                "run_from_step": 2,
                "run_until_step": 3,
                "run_mode": mode,
                "run_from_position": run_from_position,
                "run_until_position": run_until_position,
                "workflow_snapshot": workflow,
                "workflow_version_id": str(workflow.get("version_id") or ""),
                "run_scope": self.targeted_run_scope,
                "target_paper_ids": [paper_id],
            }
            command = self.append_command(state, "run_pipeline_02_03", paper_id, params)
            record["sdk_request_id"] = str(command.get("id") or "")
            record["sdk_terminal_retry_attempts"] = 0
            queue = [str(item) for item in state.get("paper_queue", [])]
            append_unique(queue, paper_id)
            state["paper_queue"] = queue
            state["active_paper_id"] = paper_id
            self.save_state(state)
        runtime = self._start_sdk_background([paper_id])
        return {
            "stage": self.stage_name,
            "action": "run_pipeline_02_03",
            "command": command,
            "run_scope": self.targeted_run_scope,
            "target_paper_ids": [paper_id],
            "scheduler_started": True,
            "account": account,
            "automation": legacy_pause,
            "runtime": runtime,
            "run_mode": mode,
            "run_from_position": run_from_position,
            "run_until_position": run_until_position,
            "message": f"已自动启动 V2 SDK 固定会话：{action_label}；程序逻辑直接调度，不等待 AI 调度器心跳",
        }

    def queue_selection_only(self, paper_id: str) -> dict[str, Any]:
        return self.queue(paper_id, run_mode="selection")

    def queue_package_only(self, paper_id: str) -> dict[str, Any]:
        return self.queue(paper_id, run_mode="package")

    def revalidate_output(self, paper_id: str) -> dict[str, Any]:
        return self.runtime.revalidate_output(paper_id)
