from __future__ import annotations

import json
import hashlib
import re
import time
import tomllib
from pathlib import Path
from typing import Any

from .config import LEGACY_PROJECTS, REPO_ROOT, WRITING_DISPATCH_PROTOCOL
from .io_utils import backup_file, read_json, read_text, write_text

AUTOMATIONS_ROOT = Path.home() / ".codex" / "automations"
WRITING_PARALLEL_LAUNCHER_MARKER = "UNIFIED_WRITING_PARALLEL_DISPATCH_V1"


def writing_parallel_launcher_prompt(project: dict[str, Any] | None = None) -> str:
    """Return the code-owned Desktop bridge; user-tuned step prompts stay on disk unchanged."""

    project = project or LEGACY_PROJECTS["writing"]
    executable = REPO_ROOT / "dist" / "UnifiedPaperOrchestrator.exe"
    return (
        f"{WRITING_PARALLEL_LAUNCHER_MARKER}. "
        "Act only as the visible writing dispatch bridge; never run a paper step in this controller thread. "
        f"The deterministic protocol is {WRITING_DISPATCH_PROTOCOL}. "
        f"Run `& '{executable}' writing-parallel-prepare` exactly once and parse its JSON. "
        "Never edit pipeline_state.json, run-intent files, the dispatch ledger, or any prompt file directly. "
        "Never search threads by title and never use list_threads as dispatch authority. "
        "For every returned ticket, use only its exact attempt_id, action, thread_id, project_id, and prompt. "
        f"Before any Desktop tool call run `& '{executable}' writing-parallel-mark-uncertain <attempt_id> "
        "--token <exact_claim_token>`. "
        "For action=continue_thread, call send_message_to_thread on the exact ticket thread_id with the exact ticket prompt. "
        "For action=create_thread, call create_thread once with the exact ticket prompt and the exact local project_id; "
        "do not override its model or reasoning effort. "
        f"After a continue tool call succeeds, use the ticket thread_id; after create_thread succeeds, use its returned "
        f"thread id. Then run `& '{executable}' writing-parallel-record-dispatch <attempt_id> "
        "--token <exact_claim_token> --thread-id <exact_thread_id>`. "
        "If a tool call is ambiguous or record-dispatch fails, do not retry: leave the send_uncertain claim for reconciliation. "
        "Dispatch distinct returned tickets up to the sidecar-provided cap, then stop. "
        "The existing scheduler and all internal Step 1-10 prompt files remain authoritative for scientific work; "
        "the ticket prompt is an exact extracted step template plus a state-write prohibition envelope. "
        "settings.auto_create_new_session is the sole opt-in for unrequested follow-on papers. "
        "Each pending explicit new_session command authorizes exactly one new Step 1 thread, and Step 1 creation remains serialized."
    )


def writing_parallel_launcher_sha256(project: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(writing_parallel_launcher_prompt(project).encode("utf-8")).hexdigest()


def writing_parallel_launcher_verified(path: Path) -> bool:
    path = Path(path)
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return str(config.get("prompt") or "") == writing_parallel_launcher_prompt()


class LegacyAutomationUnavailableError(RuntimeError):
    """The legacy file-based Codex Automation bridge is not controllable."""

    code = "LEGACY_CODEX_AUTOMATION_UNAVAILABLE"

    def __init__(self, stage: str, path: Path) -> None:
        self.stage = stage
        self.path = path
        super().__init__(
            "未检测到可由 EXE 控制的 V1 Codex Automation 配置："
            f"{path}。已停止且没有伪装成启动成功。"
            "当前 Codex 桌面端不会由本程序改写内部数据库；请使用已经连接好的旧版 Automation，"
            "或改用 API 密钥模式中已支持的功能。"
        )


class LegacyAutomationUnverifiedError(RuntimeError):
    """A TOML exists, but it was not registered and consumed by Codex Desktop."""

    code = "LEGACY_CODEX_AUTOMATION_UNVERIFIED"

    def __init__(self, stage: str, path: Path) -> None:
        self.stage = stage
        self.path = path
        super().__init__(
            "检测到 V1 调度配置，但没有通过官方 Codex Automation 注册与真实消费验证："
            f"{path}。为避免把手工复制的 TOML 误报为可运行任务，本次启动已停止。"
        )


class WritingLegacyAutomationRetiredError(RuntimeError):
    """The model-driven writing scheduler is permanently read-only/paused."""

    code = "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED"

    def __init__(self) -> None:
        super().__init__(
            "The writing-stage Codex Automation scheduler has been retired. "
            "Writing is dispatched only by the EXE's deterministic local SDK scheduler; "
            "legacy status and pause operations remain available."
        )


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def set_toml_line(text: str, key: str, value: str) -> str:
    line = f"{key} = {value}"
    pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
    if re.search(pattern, text):
        return re.sub(pattern, lambda _match: line, text, count=1)
    return text.rstrip() + "\n" + line + "\n"


def parse_toml_like(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "exists": path.exists(),
        "path": str(path),
        "id": path.parent.name,
        "status": "MISSING",
        "target_thread_id": "",
        "name": "",
        "prompt": "",
        "rrule": "",
    }
    if not path.exists():
        return data
    text = read_text(path)
    for raw in text.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key in data:
            data[key] = value
    return data


class SchedulerAutomationController:
    def __init__(
        self,
        execution_mode_store: Any | None = None,
        *,
        consumer_verifier: Any | None = None,
    ) -> None:
        self.projects = LEGACY_PROJECTS
        if execution_mode_store is None:
            from .ai_provider_store import AIProviderStore

            execution_mode_store = AIProviderStore()
        self.execution_mode_store = execution_mode_store
        self.consumer_verifier = consumer_verifier

    def _consumer_verified(self, stage: str) -> bool:
        if callable(self.consumer_verifier):
            return bool(self.consumer_verifier(stage))
        # Unit tests inject a temporary automation root.  Production always
        # uses the current user's real Codex root and must carry an official
        # registration receipt plus a bounded successful consumer probe.
        expected_root = Path.home() / ".codex" / "automations"
        try:
            is_production_root = AUTOMATIONS_ROOT.resolve() == expected_root.resolve()
        except OSError:
            is_production_root = False
        if not is_production_root:
            return True
        try:
            from .legacy_automation_registration import verify_official_registration

            result = verify_official_registration(AUTOMATIONS_ROOT)
        except Exception:
            return False
        stages = result.get("stages") if isinstance(result.get("stages"), dict) else {}
        return bool(result.get("verified")) and stages.get(stage) is True

    @staticmethod
    def _writing_model_scheduler_retired(stage: str) -> bool:
        """Retire writing starts in the real user Automation directory only.

        Tests and migration utilities may point the controller at an isolated
        temporary root.  Production must never reactivate the former
        model-driven writing scheduler now that the EXE owns deterministic SDK
        dispatch.
        """

        if stage != "writing":
            return False
        expected_root = Path.home() / ".codex" / "automations"
        try:
            return AUTOMATIONS_ROOT.resolve() == expected_root.resolve()
        except OSError:
            return False

    def status_all(self) -> dict[str, Any]:
        return {stage: self.status(stage) for stage in self.projects}

    def read_statuses(self, stage: str) -> list[dict[str, Any]]:
        """Read every matching automation config without creating or repairing it."""

        if stage not in self.projects:
            raise KeyError(f"Unknown stage: {stage}")
        paths = self._candidate_paths(stage)
        if not paths:
            return [parse_toml_like(self._primary_path(stage))]
        return [parse_toml_like(path) for path in paths]

    def active_statuses(self, stage: str) -> list[dict[str, Any]]:
        """Return all currently ACTIVE configs through a read-only path."""

        return [
            config
            for config in self.read_statuses(stage)
            if str(config.get("status") or "").strip().upper() == "ACTIVE"
        ]

    def status(self, stage: str) -> dict[str, Any]:
        """Return the preferred status without creating or repairing files."""

        configs = self.read_statuses(stage)
        primary = self._primary_path(stage)
        configs.sort(key=lambda item: (Path(item["path"]) == primary, item.get("status") == "ACTIVE"), reverse=True)
        return configs[0]

    def require_controllable(self, stage: str, state: dict[str, Any] | None = None) -> Path:
        """Validate an existing legacy target before any project-state write.

        This method never provisions an automation.  The same root can be
        created by unrelated Smart Submission maintenance, so directory
        existence is not evidence that the current Codex build still consumes
        legacy TOML files.  Provisioning, if ever restored, must be a separate
        explicit and version-gated operation.
        """

        if stage not in self.projects:
            raise KeyError(f"Unknown stage: {stage}")
        primary = self._primary_path(stage)
        if primary.is_file():
            if not self._consumer_verified(stage):
                raise LegacyAutomationUnverifiedError(stage, primary)
            return primary
        raise LegacyAutomationUnavailableError(stage, primary)

    def require_all_controllable(self) -> dict[str, Path]:
        """Validate every stage before an all-stage start can mutate anything."""

        return {
            stage: self.require_controllable(
                stage,
                read_json(Path(project["state_path"]), {}),
            )
            for stage, project in self.projects.items()
        }

    def set_enabled(self, stage: str, enabled: bool, state: dict[str, Any] | None = None) -> dict[str, Any]:
        if enabled and self._writing_model_scheduler_retired(stage):
            raise WritingLegacyAutomationRetiredError()
        if enabled:
            # The actual ACTIVE write stays inside the same cross-process lock
            # used by execution-mode commits.  Therefore API mode and an active
            # V1 scheduler cannot be produced by an interleaving pair of writes.
            with self.execution_mode_store.hold_execution_mode("legacy_codex"):
                return self._set_enabled_core(stage, enabled, state)
        return self._set_enabled_core(stage, enabled, state)

    def _set_enabled_core(self, stage: str, enabled: bool, state: dict[str, Any] | None = None) -> dict[str, Any]:
        if stage not in self.projects:
            raise KeyError(f"Unknown stage: {stage}")

        state = state if state is not None else read_json(Path(self.projects[stage]["state_path"]), {})
        primary = self._primary_path(stage)
        if enabled:
            self.require_controllable(stage, state)
        paths = self._candidate_paths(stage)
        if primary not in paths:
            paths.insert(0, primary)

        updated: list[dict[str, Any]] = []
        warnings: list[str] = []
        for path in paths:
            if not path.exists():
                if path == primary:
                    warnings.append(f"automation missing: {path}")
                continue
            config = parse_toml_like(path)
            is_primary = path == primary or config.get("id") == self.projects[stage].get("primary_automation_id")
            status = "ACTIVE" if enabled and is_primary else "PAUSED"
            text = read_text(path)
            desired = set_toml_line(text, "status", toml_string(status))
            # A pause is a control-only safety action.  In particular, a
            # single-active Writing start pauses the other stages; that must
            # not repair or replace any stage's prompt/rrule as a side effect.
            if is_primary and enabled:
                desired = self._apply_primary_defaults(stage, desired, state)
            if desired == text:
                updated.append({"path": str(path), "status": status, "backup": "", "changed": False})
                continue
            backup = backup_file(path, f"{stage}_automation")
            desired = set_toml_line(desired, "updated_at", str(int(time.time() * 1000)))
            write_text(path, desired)
            updated.append({"path": str(path), "status": status, "backup": backup, "changed": True})

        return {
            "stage": stage,
            "enabled": enabled,
            "updated": updated,
            "warnings": warnings,
            "primary": str(primary),
            "bridge_control_verified": bool(updated),
        }

    def start_all(self) -> dict[str, Any]:
        if "writing" in self.projects and self._writing_model_scheduler_retired("writing"):
            raise WritingLegacyAutomationRetiredError()
        self.require_all_controllable()
        return {stage: self.set_enabled(stage, True) for stage in self.projects}

    def pause_all(self) -> dict[str, Any]:
        return {stage: self.set_enabled(stage, False) for stage in self.projects}

    def _primary_path(self, stage: str) -> Path:
        project = self.projects[stage]
        return AUTOMATIONS_ROOT / str(project["primary_automation_id"]) / "automation.toml"

    def _ensure_primary_from_state(self, stage: str, *, enabled: bool) -> None:
        if stage not in {"figures", "journal_author"} or stage not in self.projects:
            return
        primary = self._primary_path(stage)
        state = read_json(Path(self.projects[stage]["state_path"]), {})
        if primary.exists():
            text = read_text(primary)
            fixed = self._apply_primary_defaults(stage, text, state)
            if fixed != text:
                write_text(primary, fixed)
            return
        thread_id = str(state.get("settings", {}).get("scheduler_target_thread_id") or "").strip()
        if thread_id:
            self._bootstrap(stage, primary, thread_id, enabled)

    def _candidate_paths(self, stage: str) -> list[Path]:
        project = self.projects[stage]
        ids = set(str(item) for item in project.get("automation_ids", []))
        paths = [AUTOMATIONS_ROOT / automation_id / "automation.toml" for automation_id in ids]
        if AUTOMATIONS_ROOT.exists():
            for path in AUTOMATIONS_ROOT.glob("*/automation.toml"):
                if path in paths:
                    continue
                config = parse_toml_like(path)
                haystack = " ".join(str(config.get(key) or "") for key in ("id", "name", "prompt"))
                if config.get("id") in ids or self._looks_like_stage(stage, haystack):
                    paths.append(path)
        return paths

    def _looks_like_stage(self, stage: str, text: str) -> bool:
        text_lower = text.lower()
        if stage == "writing":
            return (
                "visible-edge-llm" in text_lower
                or "edge-llm-research" in text_lower
                or ("edge llm" in text_lower and "scheduler_prompt.md" in text_lower)
            )
        if stage == "figures":
            return (
                "paper-figure-codex" in text_lower
                or "figure_scheduler_prompt.md" in text_lower
                or "paper figure codex" in text_lower
            )
        if stage == "journal_author":
            return (
                "journal-author-codex" in text_lower
                or "journal_author_state.json" in text_lower
                or "journal_scheduler_prompt.md" in text_lower
            )
        if stage == "submission":
            return (
                "auto-submission-codex-scheduler" in text_lower
                or "auto_submission_panel_state.json" in text_lower
                or "auto_submission_scheduler_prompt.md" in text_lower
            )
        return False

    def _bootstrap(self, stage: str, path: Path, thread_id: str, enabled: bool) -> None:
        project = self.projects[stage]
        status = "ACTIVE" if enabled else "PAUSED"
        timestamp = str(int(time.time() * 1000))
        rrule = self._rrule(stage)
        prompt_path = project["prompt_path"]
        text = "\n".join(
            [
                "version = 1",
                f"id = {toml_string(str(project['primary_automation_id']))}",
                'kind = "heartbeat"',
                f"name = {toml_string(str(project['label']) + ' scheduler')}",
                f"prompt = {toml_string(f'Read and follow `{prompt_path}`. Perform at most one scheduling action, then stop.')}",
                f"status = {toml_string(status)}",
                f"rrule = {toml_string(rrule)}",
                f"target_thread_id = {toml_string(thread_id)}",
                f"created_at = {timestamp}",
                f"updated_at = {timestamp}",
                "",
            ]
        )
        write_text(path, text)

    def _apply_primary_defaults(self, stage: str, text: str, state: dict[str, Any]) -> str:
        project = self.projects[stage]
        text = set_toml_line(text, "rrule", toml_string(self._rrule(stage, state)))
        if stage == "writing":
            prompt = writing_parallel_launcher_prompt(project)
            text = set_toml_line(text, "prompt", toml_string(prompt))
        if stage == "figures":
            prompt = (
                "Act as the Paper Figure Codex visible-thread scheduler. "
                "Do not generate figures in this controller thread. "
                f"On each heartbeat, read and follow exactly `{project['prompt_path']}`. "
                "Perform one scheduler sweep; dispatch eligible paper workers up to settings.max_parallel_workers, then stop."
            )
            text = set_toml_line(text, "prompt", toml_string(prompt))
        if stage == "journal_author":
            prompt = (
                "Act as the Journal Author visible-thread scheduler. "
                "Do not choose journals or make submission packages in this controller thread. "
                f"On each heartbeat, read and follow exactly `{project['prompt_path']}`. "
                "Perform one scheduler sweep; dispatch eligible journal-author workers up to settings.max_parallel_workers, then stop."
            )
            text = set_toml_line(text, "prompt", toml_string(prompt))
        if stage == "submission":
            prompt = (
                "Act as the stable submission scheduler. "
                f"The active submission project root is `{project['root']}`. "
                "Any different absolute project root written inside the legacy scheduler prompt is a historical "
                "path alias: map its relative suffix into the active root and do not read or write the old root. "
                f"On each heartbeat, read and follow exactly `{project['prompt_path']}`. "
                "Use only the state and paper paths declared by that prompt, perform at most one bounded "
                "submission scheduling action, then stop."
            )
            text = set_toml_line(text, "prompt", toml_string(prompt))
        if stage in {"figures", "journal_author"}:
            thread_id = str(state.get("settings", {}).get("scheduler_target_thread_id") or "").strip()
            if thread_id:
                text = set_toml_line(text, "target_thread_id", toml_string(thread_id))
        return text

    def _rrule(self, stage: str, state: dict[str, Any] | None = None) -> str:
        minutes = int(self.projects[stage].get("heartbeat_minutes") or 5)
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        if isinstance(settings, dict):
            raw = settings.get("heartbeat_minutes") or settings.get("scheduler_heartbeat_minutes")
            try:
                override = int(raw)
            except (TypeError, ValueError):
                override = 0
            if override > 0:
                minutes = override
        if stage == "submission":
            return f"FREQ=MINUTELY;INTERVAL={minutes}"
        return f"RRULE:FREQ=MINUTELY;INTERVAL={minutes}"
