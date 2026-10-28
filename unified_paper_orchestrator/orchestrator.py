from __future__ import annotations

import hashlib
import re
import shutil
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from .adapters import FigureAdapter, JournalAuthorAdapter, SubmissionAdapter, WritingAdapter
from .ai_execution_policy import ExecutionModePolicyError, require_legacy_execution_mode
from .ai_provider_store import AIProviderStore
from .automation import SchedulerAutomationController, parse_toml_like
from unified_paper_orchestrator_v2.read_models import plan_for_snapshot

from .backend_router import ActionSpec, BackendRouteError, BackendRouter, OperationKind, ReadOnlyPreview
from .config import (
    ARCHIVED_PAPERS_DIR,
    DELETED_DIR,
    FIGURE_MAX_PARALLEL_WORKERS,
    FIGURE_SDK_JOURNAL_PATH,
    FIGURE_SDK_LOCK_PATH,
    JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS,
    JOURNAL_AUTHOR_SDK_DISPATCH_PROTOCOL,
    JOURNAL_AUTHOR_SDK_JOURNAL_PATH,
    JOURNAL_AUTHOR_SDK_LOCK_PATH,
    LEGACY_PROJECTS,
    PAPER_LIFECYCLE_AUDIT_PATH,
    SMART_SUBMISSION_STATE_PATH,
    STAGE_LABELS,
    STAGE_ORDER,
    STATE_DIR,
    UNIFIED_STATE_PATH,
    UNIFIED_SUMMARY_PATH,
    WORKSPACE_ROOT as REPO_ROOT,
    WRITING_DISPATCH_LOCK_PATH,
    WRITING_DISPATCH_STATE_PATH,
    WRITING_GATE_POLICY_PATH,
    WRITING_MAX_PARALLEL_WORKERS,
    WRITING_SDK_DISPATCH_PROTOCOL,
    WRITING_SDK_JOURNAL_PATH,
    WRITING_SDK_LOCK_PATH,
)
from .io_utils import FileLock, append_unique, backup_file, markdown_escape, now_iso, read_json, read_text, timestamp_id, write_json_atomic, write_text
from .models import action_for, action_label, current_stage_for, is_complete, status_token, undergraduate_stage_boundary
from .figure_workflow import FigureWorkflowStore
from .journal_author_workflow import JournalAuthorWorkflowStore
from .writing_workflow import WritingWorkflowStore
from .writing_gate_policy import AUTO_TRUST_MODE, WritingGatePolicyStore
from .submission_routing import (
    SubmissionRouteError,
    normalize_submission_route,
    route_switch_blockers,
    submission_route_for,
)


def _unified_state_lock() -> FileLock:
    """Return the lock colocated with the currently configured unified state."""

    return FileLock(path=UNIFIED_STATE_PATH.parent / "unified_scheduler.lock")


STAGE_BACKEND_ACTIONS = frozenset(
    {
        f"stage.{operation}.{stage}"
        for operation in ("start", "pause")
        for stage in STAGE_ORDER
    }
    | {"stage.start_all", "stage.pause_all"}
)
V1_BACKEND_ACTION_SPECS = {
    "_plan_for": ActionSpec("_plan_for", OperationKind.READ_ONLY),
    **{
        action: ActionSpec(
            action,
            OperationKind.MUTATING,
            shadow_allowed=False,
        )
        for action in sorted(STAGE_BACKEND_ACTIONS)
    },
}
V2_PLAN_PREVIEW = ReadOnlyPreview("v2.plan_for", plan_for_snapshot)


class UnifiedOrchestrator:
    def __init__(self) -> None:
        # V1 remains the authoritative implementation. Individual actions can
        # later be shadowed or migrated behind this internal compatibility seam
        # without changing the UI, public methods, state files, or user prompts.
        self.backend_router = BackendRouter.legacy_only(V1_BACKEND_ACTION_SPECS)
        self.adapters = {
            "writing": WritingAdapter(),
            "figures": FigureAdapter(),
            "journal_author": JournalAuthorAdapter(),
            "submission": SubmissionAdapter(),
        }
        self.automation = SchedulerAutomationController()
        self.ai_provider_store = AIProviderStore()
        self.writing_workflow_store = getattr(self.adapters["writing"], "workflow_store", WritingWorkflowStore())
        self.writing_gate_policy_store = WritingGatePolicyStore(WRITING_GATE_POLICY_PATH)
        self.figure_workflow_store = getattr(self.adapters["figures"], "workflow_store", FigureWorkflowStore())
        self.journal_author_workflow_store = getattr(
            self.adapters["journal_author"],
            "workflow_store",
            JournalAuthorWorkflowStore(),
        )
        # Deliberately empty in production today.  A future stage backend must
        # also be selected through BackendRouter's explicit new-mode latch;
        # merely assigning an object here never changes V1 behavior.
        self.stage_execution_backend: Any | None = None

    def _stage_backend_callback(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[], dict[str, Any]] | None:
        backend = getattr(self, "stage_execution_backend", None)
        method = getattr(backend, method_name, None)
        if not callable(method):
            return None

        def execute() -> dict[str, Any]:
            if method_name in {"start_stage", "start_all"}:
                self._require_api_stage_execution_mode("启动 API 阶段后端")
            result = method(*args, **kwargs)
            if not isinstance(result, dict):
                raise BackendRouteError(
                    "STAGE_BACKEND_RESULT_INVALID",
                    f"Stage backend {method_name!r} did not return a dictionary.",
                )
            return result

        return execute

    def _require_api_stage_execution_mode(self, action: str) -> None:
        """Keep an explicitly migrated stage backend bound to API mode."""

        try:
            settings = self.ai_provider_store.load_settings()
        except Exception as exc:
            raise ExecutionModePolicyError(
                "AI_EXECUTION_MODE_STATE_UNREADABLE",
                "AI 执行模式配置无法读取；为避免误用另一种执行方式，本次操作已停止。",
            ) from exc
        if str(settings.execution_mode or "").strip() != "api_key":
            raise ExecutionModePolicyError(
                "AI_API_STAGE_REQUIRES_API_MODE",
                f"{action}仅能在 API 密钥模式中执行；不会自动切换或回退到 V1。",
            )

    def _require_legacy_execution_mode(self, action: str) -> None:
        """Fail closed instead of silently routing an API-mode click to V1."""

        try:
            settings = self.ai_provider_store.load_settings()
        except Exception as exc:
            raise ExecutionModePolicyError(
                "AI_EXECUTION_MODE_STATE_UNREADABLE",
                "AI 执行模式配置无法读取；为避免误用另一种执行方式，本次操作已停止。",
            ) from exc
        require_legacy_execution_mode(settings.execution_mode, action=action)

    @staticmethod
    def _pause_adapter(adapter: Any) -> dict[str, Any]:
        """Pause a local runtime or legacy Automation and report verification."""

        try:
            automation = adapter.pause()
        except Exception as exc:
            return {"ok": False, "action": "pause_failed", "error": str(exc)}
        verified = False
        if isinstance(automation, dict):
            verified = bool(
                automation.get("ok") is True
                or automation.get("paused") is True
                or automation.get("bridge_control_verified")
                or automation.get("updated")
                or str(automation.get("status") or "").lower()
                in {"paused", "pause_requested", "already_paused"}
            )
        return {
            "ok": verified,
            "action": "paused" if verified else "pause_unverified",
            "automation": automation,
            **(
                {}
                if verified
                else {
                    "warning": (
                        "本地阶段已设为手动，但未取得执行器的暂停确认。"
                    )
                }
            ),
        }

    def load_unified_state(self) -> dict[str, Any]:
        state = read_json(UNIFIED_STATE_PATH, {})
        if not isinstance(state, dict):
            raise ValueError(f"Unified state must be a JSON object: {UNIFIED_STATE_PATH}")
        state.setdefault("version", 1)
        state.setdefault("settings", {"scheduling_mode": "manual", "one_action_per_run": True})
        state.setdefault("paper_queue", [])
        state.setdefault("papers", {})
        state.setdefault("submission_routes", {})
        state.setdefault("submission_route_operations", {})
        state.setdefault("stage_admission_overrides", {})
        state.setdefault("archived_papers", {})
        state.setdefault("history", [])
        return state

    def sync(self, persist: bool = True, *, reconcile_writing: bool = True) -> dict[str, Any]:
        if persist:
            # One lock spans read/reconcile/write so a projection refresh can
            # never overwrite a newer route switch with an older snapshot.
            with _unified_state_lock():
                return self._sync_unlocked(persist=True, reconcile_writing=reconcile_writing)
        return self._sync_unlocked(persist=False, reconcile_writing=reconcile_writing)

    def _sync_unlocked(
        self,
        persist: bool = True,
        *,
        reconcile_writing: bool = True,
    ) -> dict[str, Any]:
        # Reconcile terminal writing workers before projecting state.  This is
        # intentionally evidence-based: the writing adapter requires both the
        # real thread's task_complete markers and fingerprintable step output.
        if reconcile_writing:
            self._reconcile_writing_completion()
        previous = self.load_unified_state()
        previous_routes = previous.get("submission_routes", {})
        if not isinstance(previous_routes, dict):
            raise ValueError("unified submission_routes must be a JSON object")
        previous_route_operations = previous.get("submission_route_operations", {})
        if not isinstance(previous_route_operations, dict):
            previous_route_operations = {}
        previous_admission_overrides = previous.get("stage_admission_overrides", {})
        if not isinstance(previous_admission_overrides, dict):
            previous_admission_overrides = {}
        previous_archived_papers = previous.get("archived_papers", {})
        if not isinstance(previous_archived_papers, dict):
            previous_archived_papers = {}
        paper_ids: set[str] = set()
        submission_adapter = self.adapters["submission"]
        submission_states = submission_adapter.load_projection_states()
        previous_projection_sources = previous.get("projection_sources", {})
        projection_sources = (
            dict(previous_projection_sources) if isinstance(previous_projection_sources, dict) else {}
        )
        smart_projection_state = submission_states[1] if isinstance(submission_states[1], dict) else {}
        projection_sources["smart_submission_token"] = str(
            smart_projection_state.get("unified_projection_token") or ""
        )
        for stage, adapter in self.adapters.items():
            if stage == "submission":
                paper_ids.update(submission_adapter.paper_ids_from_states(*submission_states))
            else:
                paper_ids.update(adapter.all_paper_ids())
        paper_ids.update(str(item) for item in previous.get("paper_queue", []) if item)
        # Archived papers are a global lifecycle state.  A stale stage journal
        # or an old controller history entry must never make one reappear in
        # overview/stage projections after it has been archived.
        paper_ids.difference_update(str(item) for item in previous_archived_papers)

        title_states = {
            "submission": submission_states[0],
            "journal_author": self.adapters["journal_author"].load_state(),
        }

        papers: dict[str, Any] = {}
        submission_routes = {
            str(paper_id): normalize_submission_route(route)
            for paper_id, route in previous_routes.items()
            if str(paper_id).strip() and str(paper_id) not in previous_archived_papers
        }
        for paper_id in sorted(paper_ids):
            route = submission_routes.setdefault(paper_id, normalize_submission_route())
            stages = {
                stage: (
                    submission_adapter.stage_for_from_states(
                        paper_id,
                        *submission_states,
                        execution_owner=str(route["execution_owner"]),
                    )
                    if stage == "submission"
                    else self.adapters[stage].stage_for(paper_id)
                )
                for stage in STAGE_ORDER
            }
            self._infer_upstream_handoffs(stages)
            paper_overrides = previous_admission_overrides.get(paper_id, {})
            if not isinstance(paper_overrides, dict):
                paper_overrides = {}
            admissions = self._stage_admissions_for(stages, paper_overrides)
            for stage_name, admission in admissions.items():
                if admission.get("admitted") and str(stages[stage_name].get("status") or "missing") == "missing":
                    stages[stage_name]["status"] = "ready"
                    stages[stage_name]["raw_status"] = "automatic_admission"
                    stages[stage_name]["detail"] = str(admission.get("reason") or "已进入本阶段，等待运行")
                stages[stage_name]["admission"] = dict(admission)
                stages[stage_name]["admission_mode"] = str(admission.get("mode") or "")
            current_stage = self._effective_current_stage(stages, admissions)
            current_stage = undergraduate_stage_boundary(stages, current_stage)
            admission = admissions.get(current_stage, {})
            next_action, blocker = action_for(
                current_stage,
                stages,
                allow_upstream_incomplete=str(admission.get("mode") or "") == "manual_override",
            )
            next_action, blocker = self._route_submission_action(route, current_stage, next_action, blocker)
            title = self._title_for(paper_id, title_states)
            papers[paper_id] = {
                "paper_id": paper_id,
                "title": title,
                "stages": stages,
                "current_stage": current_stage,
                "current_stage_label": STAGE_LABELS.get(current_stage, "完成"),
                "next_action": next_action,
                "next_action_label": action_label(next_action),
                "blocked_reason": blocker,
                "submission_route": dict(route),
                "stage_admissions": admissions,
                "updated_at": now_iso(),
            }

        queue = self._normalized_queue(previous.get("paper_queue", []), papers)
        state = {
            "version": max(int(previous.get("version") or 1), 2),
            "updated_at": now_iso(),
            "settings": previous.get("settings", {"scheduling_mode": "manual", "one_action_per_run": True}),
            "paper_queue": queue,
            "papers": papers,
            "submission_routes": submission_routes,
            "submission_route_operations": previous_route_operations,
            "stage_admission_overrides": previous_admission_overrides,
            "archived_papers": previous_archived_papers,
            "history": previous.get("history", [])[-200:],
            "last_scheduler_result": previous.get("last_scheduler_result", {}),
            "stage_order": list(STAGE_ORDER),
            "stage_labels": STAGE_LABELS,
            "projection_sources": projection_sources,
        }
        if persist:
            self.save(state)
        return state

    def save(self, state: dict[str, Any]) -> None:
        write_json_atomic(UNIFIED_STATE_PATH, state)
        write_text(UNIFIED_SUMMARY_PATH, self.render_summary(state))

    def queue_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        with _unified_state_lock():
            state = self.sync(persist=False)
            queue = [str(item) for item in state.get("paper_queue", [])]
            for paper_id in paper_ids:
                record = state.get("papers", {}).get(paper_id, {})
                if record and str(record.get("next_action") or "none") not in {"none", "blocked"}:
                    append_unique(queue, paper_id)
            state["paper_queue"] = queue
            state.setdefault("history", []).append({"at": now_iso(), "event": "queue", "paper_ids": paper_ids})
            self.save(state)
            return state

    def clear_queue(self) -> dict[str, Any]:
        with _unified_state_lock():
            state = self.sync(persist=False)
            old = list(state.get("paper_queue", []))
            state["paper_queue"] = []
            state.setdefault("history", []).append({"at": now_iso(), "event": "clear_queue", "paper_ids": old})
            self.save(state)
            return state

    def force_stage_admission(
        self,
        paper_ids: list[str],
        stage: str,
        *,
        reason: str = "用户确认当前上游结果可直接进入下一阶段",
    ) -> dict[str, Any]:
        if stage not in STAGE_ORDER[1:]:
            raise ValueError("只能人工放行画图、选刊作者或投稿阶段")
        ids = list(dict.fromkeys(str(item or "").strip() for item in paper_ids if str(item or "").strip()))
        if not ids:
            raise ValueError("至少选择一篇论文")
        with _unified_state_lock():
            state = self.sync(persist=False)
            papers = state.get("papers", {})
            missing = [paper_id for paper_id in ids if paper_id not in papers]
            if missing:
                raise ValueError(f"论文不存在：{', '.join(missing)}")
            overrides = state.setdefault("stage_admission_overrides", {})
            at = now_iso()
            for paper_id in ids:
                record = papers[paper_id]
                stage_index = STAGE_ORDER.index(stage)
                upstream_name = STAGE_ORDER[stage_index - 1]
                upstream = record.get("stages", {}).get(upstream_name, {})
                overrides.setdefault(paper_id, {})[stage] = {
                    "admitted": True,
                    "mode": "manual_override",
                    "at": at,
                    "reason": str(reason or "用户人工越级放行"),
                    "upstream_stage": upstream_name,
                    "upstream_status": str(upstream.get("status") or "missing"),
                    "upstream_step_done": upstream.get("step_done"),
                    "upstream_artifact": str(upstream.get("artifact") or ""),
                }
            state.setdefault("history", []).append(
                {"at": at, "event": "force_stage_admission", "stage": stage, "paper_ids": ids, "reason": reason}
            )
            self.save(state)
            refreshed = self.sync(persist=False, reconcile_writing=False)
            refreshed["last_scheduler_result"] = {
                "status": "stage_admission_overridden",
                "stage": stage,
                "paper_ids": ids,
                "message": f"已人工放行 {len(ids)} 篇论文进入{STAGE_LABELS[stage]}阶段。",
            }
            self.save(refreshed)
            return refreshed

    def clear_stage_admission_override(self, paper_ids: list[str], stage: str) -> dict[str, Any]:
        if stage not in STAGE_ORDER[1:]:
            raise ValueError("该阶段没有人工越级记录")
        ids = list(dict.fromkeys(str(item or "").strip() for item in paper_ids if str(item or "").strip()))
        with _unified_state_lock():
            state = self.sync(persist=False)
            overrides = state.setdefault("stage_admission_overrides", {})
            changed: list[str] = []
            for paper_id in ids:
                paper_overrides = overrides.get(paper_id)
                if isinstance(paper_overrides, dict) and stage in paper_overrides:
                    del paper_overrides[stage]
                    changed.append(paper_id)
                    if not paper_overrides:
                        overrides.pop(paper_id, None)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "clear_stage_admission_override", "stage": stage, "paper_ids": changed}
            )
            self.save(state)
            refreshed = self.sync(persist=False, reconcile_writing=False)
            self.save(refreshed)
            return refreshed

    def set_submission_route(
        self,
        paper_id: str,
        *,
        execution_owner: str,
        mode: str,
        expected_revision: int | None = None,
        operation_id: str = "",
    ) -> dict[str, Any]:
        """Switch one paper route while preserving the V1 public contract."""

        return self._set_submission_route_internal(
            paper_id,
            execution_owner=execution_owner,
            mode=mode,
            expected_revision=expected_revision,
            operation_id=operation_id,
            allow_existing_submission_handoff=False,
        )

    def handoff_existing_submission_to_smart(
        self,
        paper_id: str,
        *,
        expected_revision: int | None = None,
        operation_id: str = "",
    ) -> dict[str, Any]:
        """Transfer an already-submitted paper to Smart canary follow-up."""

        return self._set_submission_route_internal(
            paper_id,
            execution_owner="smart",
            mode="canary",
            expected_revision=expected_revision,
            operation_id=operation_id,
            allow_existing_submission_handoff=True,
        )

    def _set_submission_route_internal(
        self,
        paper_id: str,
        *,
        execution_owner: str,
        mode: str,
        expected_revision: int | None = None,
        operation_id: str = "",
        allow_existing_submission_handoff: bool = False,
    ) -> dict[str, Any]:
        """Atomically switch one paper's execution owner/mode in unified state only.

        Neither legacy nor Smart engine state is modified.  A switch is denied
        if either engine is queued/running, final-submit outcome is unknown, or
        verifiable terminal evidence already exists.  The one explicit
        exception is an existing-submission handoff to Smart canary: terminal
        evidence is preserved while all active-worker and unknown-result
        blockers still fail closed.
        """

        paper_id = str(paper_id or "").strip()
        operation_id = str(operation_id or "").strip()
        if not paper_id:
            raise ValueError("paper_id is required")
        requested = normalize_submission_route(
            {"execution_owner": execution_owner, "mode": mode, "revision": 1}
        )
        with _unified_state_lock():
            state = self.sync(persist=False)
            record = state.get("papers", {}).get(paper_id)
            if not isinstance(record, dict):
                raise KeyError(f"unknown paper_id: {paper_id}")
            current = submission_route_for(state.get("submission_routes"), paper_id)
            if expected_revision is not None and int(expected_revision) != int(current["revision"]):
                raise SubmissionRouteError(
                    f"submission route revision conflict for {paper_id}: "
                    f"expected={expected_revision}, actual={current['revision']}"
                )
            if (
                requested["execution_owner"] == current["execution_owner"]
                and requested["mode"] == current["mode"]
            ):
                return {
                    "changed": False,
                    "paper_id": paper_id,
                    "previous_route": current,
                    "submission_route": current,
                    "operation_id": operation_id,
                }

            submission_adapter = self.adapters["submission"]
            legacy_state, smart_state = submission_adapter.load_projection_states()
            blockers = route_switch_blockers(
                legacy_state,
                smart_state,
                paper_id,
                smart_evidence_base=REPO_ROOT,
            )
            terminal_handoff_evidence: list[dict[str, Any]] = []
            if allow_existing_submission_handoff:
                if not (
                    requested["execution_owner"] == "smart"
                    and requested["mode"] == "canary"
                ):
                    raise SubmissionRouteError(
                        "existing-submission handoff is restricted to smart/canary"
                    )
                terminal_handoff_evidence = [
                    item
                    for blocker in blockers
                    if str(blocker.get("code") or "") == "terminal_evidence"
                    for item in (
                        blocker.get("evidence", [])
                        if isinstance(blocker.get("evidence"), list)
                        else []
                    )
                    if isinstance(item, dict)
                ]
                blockers = [
                    blocker
                    for blocker in blockers
                    if str(blocker.get("code") or "") != "terminal_evidence"
                ]
            if blockers:
                codes = ", ".join(str(item.get("code") or "unsafe") for item in blockers)
                raise SubmissionRouteError(f"refusing unsafe route switch for {paper_id}: {codes}")

            requested["revision"] = int(current["revision"]) + 1
            routes = state.setdefault("submission_routes", {})
            routes[paper_id] = requested
            if operation_id:
                state.setdefault("submission_route_operations", {})[paper_id] = {
                    "operation_id": operation_id,
                    "route_revision": int(requested["revision"]),
                    "execution_owner": str(requested["execution_owner"]),
                    "mode": str(requested["mode"]),
                    "existing_submission_handoff": bool(
                        allow_existing_submission_handoff
                    ),
                    "terminal_evidence_count": len(terminal_handoff_evidence),
                    "at": now_iso(),
                }
            stages = record.get("stages", {})
            stages["submission"] = submission_adapter.stage_for_from_states(
                paper_id,
                legacy_state,
                smart_state,
                execution_owner=str(requested["execution_owner"]),
            )
            self._infer_upstream_handoffs(stages)
            paper_overrides = state.get("stage_admission_overrides", {}).get(paper_id, {})
            if not isinstance(paper_overrides, dict):
                paper_overrides = {}
            admissions = self._stage_admissions_for(stages, paper_overrides)
            for stage_name, admission in admissions.items():
                if admission.get("admitted") and str(stages[stage_name].get("status") or "missing") == "missing":
                    stages[stage_name]["status"] = "ready"
                    stages[stage_name]["raw_status"] = "automatic_admission"
                    stages[stage_name]["detail"] = str(admission.get("reason") or "已进入本阶段，等待运行")
                stages[stage_name]["admission"] = dict(admission)
                stages[stage_name]["admission_mode"] = str(admission.get("mode") or "")
            current_stage = self._effective_current_stage(stages, admissions)
            current_stage = undergraduate_stage_boundary(stages, current_stage)
            admission = admissions.get(current_stage, {})
            next_action, blocker = action_for(
                current_stage,
                stages,
                allow_upstream_incomplete=str(admission.get("mode") or "") == "manual_override",
            )
            next_action, blocker = self._route_submission_action(requested, current_stage, next_action, blocker)
            record.update(
                {
                    "current_stage": current_stage,
                    "current_stage_label": STAGE_LABELS.get(current_stage, "完成"),
                    "next_action": next_action,
                    "next_action_label": action_label(next_action),
                    "blocked_reason": blocker,
                    "submission_route": dict(requested),
                    "stage_admissions": admissions,
                    "updated_at": now_iso(),
                }
            )
            event = {
                "at": now_iso(),
                "event": "set_submission_route",
                "paper_id": paper_id,
                "previous_route": current,
                "submission_route": requested,
                "operation_id": operation_id,
                "existing_submission_handoff": bool(
                    allow_existing_submission_handoff
                ),
                "terminal_evidence_count": len(terminal_handoff_evidence),
            }
            state.setdefault("history", []).append(event)
            state["last_scheduler_result"] = {
                "status": "submission_route_changed",
                "message": f"{paper_id}: {current['execution_owner']} -> {requested['execution_owner']}",
                **event,
            }
            self.save(state)
            return {
                "changed": True,
                "paper_id": paper_id,
                "previous_route": current,
                "submission_route": requested,
                "operation_id": operation_id,
                "existing_submission_handoff": bool(
                    allow_existing_submission_handoff
                ),
            }

    def resume_writing_papers(self, paper_ids: list[str], *, single_active: bool = False) -> dict[str, Any]:
        self._require_legacy_execution_mode("继续写作")
        ids = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
        with _unified_state_lock():
            results: dict[str, Any] = {}
            if single_active:
                for other_stage, adapter in self.adapters.items():
                    if other_stage == "writing":
                        continue
                    results[other_stage] = self._pause_adapter(adapter)
            try:
                batch = self.adapters["writing"].queue_many(ids)
            except Exception as exc:
                results["writing"] = {
                    "ok": False,
                    "action": "resume_writing_failed",
                    "paper_ids": ids,
                    "error": str(exc),
                }
                for paper_id in ids:
                    results[paper_id] = {"ok": False, "action": "resume_writing_failed", "error": str(exc)}
            else:
                results["writing"] = {
                    "ok": True,
                    "action": "resume_writing",
                    "paper_ids": ids,
                    "runtime": batch.get("runtime", {}) if isinstance(batch, dict) else {},
                }
                commands = batch.get("commands", []) if isinstance(batch, dict) else []
                for index, paper_id in enumerate(ids):
                    command = commands[index] if index < len(commands) else {}
                    results[paper_id] = {
                        "ok": True,
                        "action": "resume_writing",
                        "legacy_result": {
                            "stage": "writing",
                            "action": "resume_session",
                            "command": command,
                            "completion_reconciliation": batch.get("completion_reconciliation", {}),
                            "automation": batch.get("automation", {}),
                        },
                    }
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "resume_writing_papers", "paper_ids": ids, "single_active": single_active, "results": results}
            )
            state["last_scheduler_result"] = {
                "status": "resume_writing_papers",
                "paper_ids": ids,
                "single_active": single_active,
                "results": results,
            }
            self.save(state)
            return state

    def queue_figure_image2_revisions(self, paper_ids: list[str], figures: list[str] | None = None, request: str = "") -> dict[str, Any]:
        self._require_legacy_execution_mode("图像模型重绘")
        ids = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
        target_figures = figures or ["Figure 1", "Figure 2"]
        with _unified_state_lock():
            results: dict[str, Any] = {}
            for paper_id in ids:
                paper_results = []
                for figure in target_figures:
                    try:
                        paper_results.append(
                            {
                                "ok": True,
                                "figure": figure,
                                "legacy_result": self.adapters["figures"].queue_image2_revision(paper_id, figure, request),
                            }
                        )
                    except Exception as exc:
                        paper_results.append({"ok": False, "figure": figure, "error": str(exc)})
                results[paper_id] = paper_results
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "queue_figure_image2_revisions",
                    "paper_ids": ids,
                    "figures": target_figures,
                    "request": request,
                    "results": results,
                }
            )
            state["last_scheduler_result"] = {
                "status": "queue_figure_image2_revisions",
                "paper_ids": ids,
                "figures": target_figures,
                "request": request,
                "results": results,
            }
            self.save(state)
            return state

    def queue_figures_from_step1(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
        with _unified_state_lock():
            state = self.sync(persist=False)
            results: dict[str, Any] = {}
            for paper_id in ids:
                record = state.get("papers", {}).get(paper_id, {})
                stages = record.get("stages", {}) if isinstance(record, dict) else {}
                writing = stages.get("writing", {}) if isinstance(stages, dict) else {}
                artifact = str(writing.get("artifact") or "")
                if not artifact:
                    results[paper_id] = {"ok": False, "error": "missing writing artifact draft_v6.tex"}
                    continue
                try:
                    results[paper_id] = {
                        "ok": True,
                        "legacy_result": self.adapters["figures"].queue_from_step1(paper_id, artifact),
                    }
                except Exception as exc:
                    results[paper_id] = {"ok": False, "error": str(exc)}
            refreshed = self.sync(persist=False)
            refreshed.setdefault("history", []).append(
                {"at": now_iso(), "event": "queue_figures_from_step1", "paper_ids": ids, "results": results}
            )
            refreshed["last_scheduler_result"] = {
                "status": "queue_figures_from_step1",
                "paper_ids": ids,
                "results": results,
            }
            self.save(refreshed)
            return refreshed

    def queue_figures_from_step3(self, paper_ids: list[str]) -> dict[str, Any]:
        ids: list[str] = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
        with _unified_state_lock():
            results: dict[str, Any] = {}
            for paper_id in ids:
                try:
                    results[paper_id] = {
                        "ok": True,
                        "legacy_result": self.adapters["figures"].queue_from_step3(paper_id),
                    }
                except Exception as exc:
                    results[paper_id] = {"ok": False, "error": str(exc)}
            refreshed = self.sync(persist=False)
            refreshed.setdefault("history", []).append(
                {"at": now_iso(), "event": "queue_figures_from_step3", "paper_ids": ids, "results": results}
            )
            refreshed["last_scheduler_result"] = {
                "status": "queue_figures_from_step3",
                "paper_ids": ids,
                "results": results,
            }
            self.save(refreshed)
            return refreshed

    def requeue_legacy_figure_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇历史画图任务")
        with _unified_state_lock():
            results: dict[str, Any] = {}
            for paper_id in ids:
                try:
                    results[paper_id] = {
                        "ok": True,
                        "legacy_result": self.adapters["figures"].queue_migration(paper_id),
                    }
                except Exception as exc:
                    results[paper_id] = {"ok": False, "error": str(exc)}
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "requeue_legacy_figure_papers",
                    "paper_ids": ids,
                    "results": results,
                }
            )
            state["last_scheduler_result"] = {
                "status": "requeue_legacy_figure_papers",
                "paper_ids": ids,
                "results": results,
            }
            self.save(state)
            return state

    def revalidate_figure_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要重新验收的画图论文")
        with _unified_state_lock():
            results: dict[str, Any] = {}
            for paper_id in ids:
                try:
                    results[paper_id] = {
                        "ok": True,
                        "result": self.adapters["figures"].revalidate_output(paper_id),
                    }
                except Exception as exc:
                    results[paper_id] = {"ok": False, "error": str(exc)}
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "revalidate_figure_papers",
                    "paper_ids": ids,
                    "results": results,
                }
            )
            state["last_scheduler_result"] = {
                "status": "revalidate_figure_papers",
                "paper_ids": ids,
                "results": results,
            }
            self.save(state)
            return state

    def pause_figure_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要暂停的画图论文")
        with _unified_state_lock():
            result = self.adapters["figures"].pause_papers(ids)
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "pause_figure_papers", "paper_ids": ids, "result": result}
            )
            state["last_scheduler_result"] = {
                "status": "pause_figure_papers",
                "paper_ids": ids,
                "result": result,
            }
            self.save(state)
            return state

    def resume_figure_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要继续的画图论文")
        with _unified_state_lock():
            result = self.adapters["figures"].resume_papers(ids)
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "resume_figure_papers", "paper_ids": ids, "result": result}
            )
            state["last_scheduler_result"] = {
                "status": "resume_figure_papers",
                "paper_ids": ids,
                "result": result,
            }
            self.save(state)
            return state

    def pause_journal_author_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要暂停的选刊作者论文")
        with _unified_state_lock():
            result = self.adapters["journal_author"].pause_papers(ids)
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "pause_journal_author_papers",
                    "paper_ids": ids,
                    "result": result,
                }
            )
            state["last_scheduler_result"] = {
                "status": "pause_journal_author_papers",
                "paper_ids": ids,
                "result": result,
            }
            self.save(state)
            return state

    def resume_journal_author_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要继续的选刊作者论文")
        with _unified_state_lock():
            result = self.adapters["journal_author"].resume_papers(ids)
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "resume_journal_author_papers",
                    "paper_ids": ids,
                    "result": result,
                }
            )
            state["last_scheduler_result"] = {
                "status": "resume_journal_author_papers",
                "paper_ids": ids,
                "result": result,
            }
            self.save(state)
            return state

    def requeue_journal_author_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        return self.run_journal_author_papers(paper_ids, run_mode="full")

    def run_journal_author_papers(self, paper_ids: list[str], *, run_mode: str = "full") -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要重新执行的选刊作者论文")
        mode = str(run_mode or "full").strip().casefold()
        if mode not in {"full", "selection", "package"}:
            raise ValueError("选刊作者运行方式无效")
        with _unified_state_lock():
            projection = self.sync(persist=False)
            results: dict[str, Any] = {}
            for paper_id in ids:
                record = projection.get("papers", {}).get(paper_id, {})
                stages = record.get("stages", {}) if isinstance(record, dict) else {}
                figures = stages.get("figures", {}) if isinstance(stages, dict) else {}
                artifact = str(figures.get("artifact") or "") if isinstance(figures, dict) else ""
                try:
                    results[paper_id] = {
                        "ok": True,
                        "result": self.adapters["journal_author"].queue(paper_id, artifact, run_mode=mode),
                    }
                except Exception as exc:
                    results[paper_id] = {"ok": False, "error": str(exc)}
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "run_journal_author_papers",
                    "paper_ids": ids,
                    "run_mode": mode,
                    "results": results,
                }
            )
            state["last_scheduler_result"] = {
                "status": "run_journal_author_papers",
                "paper_ids": ids,
                "run_mode": mode,
                "results": results,
            }
            self.save(state)
            return state

    def revalidate_journal_author_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要重新验收的选刊作者论文")
        results: dict[str, Any] = {}
        for paper_id in ids:
            try:
                results[paper_id] = {"ok": True, "result": self.adapters["journal_author"].revalidate_output(paper_id)}
            except Exception as exc:
                results[paper_id] = {"ok": False, "error": str(exc)}
        with _unified_state_lock():
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "revalidate_journal_author_papers", "paper_ids": ids, "results": results}
            )
            state["last_scheduler_result"] = {
                "status": "revalidate_journal_author_papers",
                "paper_ids": ids,
                "results": results,
            }
            self.save(state)
            return state

    def journal_preferences(self, paper_id: str) -> dict[str, Any]:
        return self.adapters["journal_author"].load_journal_preferences(paper_id)

    def save_journal_preferences(self, paper_id: str, value: dict[str, Any]) -> dict[str, Any]:
        result = self.adapters["journal_author"].save_journal_preferences(paper_id, value)
        with _unified_state_lock():
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "save_journal_preferences", "paper_id": paper_id}
            )
            state["last_scheduler_result"] = result
            self.save(state)
        return result

    def author_templates(self) -> dict[str, Any]:
        return self.adapters["journal_author"].load_author_templates()

    def save_author_templates(self, data: dict[str, Any]) -> dict[str, Any]:
        with _unified_state_lock():
            templates = self.adapters["journal_author"].save_author_templates(data)
            state = self.sync(persist=False)
            state.setdefault("history", []).append({"at": now_iso(), "event": "save_author_templates"})
            state["last_scheduler_result"] = {"status": "save_author_templates", "template_count": len(templates.get("templates", []))}
            self.save(state)
            return templates

    def apply_author_template(self, paper_ids: list[str], template_id: str, overwrite: bool = True) -> dict[str, Any]:
        ids = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
        with _unified_state_lock():
            result = self.adapters["journal_author"].apply_author_template(ids, template_id, overwrite=overwrite)
            state = self.sync(persist=False)
            state.setdefault("history", []).append({"at": now_iso(), "event": "apply_author_template", "result": result})
            state["last_scheduler_result"] = result
            self.save(state)
            return state

    def update_author_affiliation_fields(
        self,
        paper_ids: list[str],
        affiliation_index: int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        ids = list(
            dict.fromkeys(
                str(raw).strip() for raw in paper_ids if str(raw).strip()
            )
        )
        with _unified_state_lock():
            result = self.adapters[
                "journal_author"
            ].update_author_affiliation_fields(
                ids,
                affiliation_index,
                fields,
            )
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "update_author_affiliation_fields",
                    "result": result,
                }
            )
            state["last_scheduler_result"] = result
            self.save(state)
            return result

    def stage_prompt_inventory(self, stage: str) -> dict[str, Any]:
        if stage not in self.adapters:
            raise KeyError(f"Unknown stage: {stage}")
        if stage == "writing":
            self._reconcile_writing_completion()
        project = LEGACY_PROJECTS[stage]
        adapter = self.adapters[stage]
        state = adapter.load_state()
        runtime_status: dict[str, Any] | None = None
        if stage in {"writing", "figures", "journal_author"}:
            if stage == "writing":
                automation = self._scheduler_status_read_only().get("writing", {})
            else:
                automation = self.automation.status(stage)
            runtime = getattr(adapter, "runtime", None)
            runtime_status_method = getattr(runtime, "status", None)
            if callable(runtime_status_method):
                try:
                    runtime_status = runtime_status_method(check_account=False)
                except Exception as exc:
                    runtime_status = {
                        "ok": False,
                        "blocker": str(getattr(exc, "code", type(exc).__name__)),
                    }
        else:
            automation = self.automation.status(stage)
        prompt_path = Path(project["prompt_path"])
        task_prompts = self._task_prompt_files(stage)
        return {
            "stage": stage,
            "label": STAGE_LABELS.get(stage, stage),
            "root": str(project["root"]),
            "state_path": str(project["state_path"]),
            "scheduler_prompt": self._prompt_file_info(prompt_path, "调度器 Prompt"),
            "task_prompts": [self._prompt_file_info(path, path.name) for path in task_prompts],
            "settings": state.get("settings", {}) if isinstance(state.get("settings"), dict) else {},
            "automation": automation,
            "runtime": runtime_status,
            "writing_gate_policy": self.writing_gate_policy_store.load_public() if stage == "writing" else {},
            "writing_workflow": self.writing_workflow_store.inventory() if stage == "writing" else {},
            "figure_workflow": self.figure_workflow_store.inventory() if stage == "figures" else {},
            "journal_author_workflow": (
                self.journal_author_workflow_store.inventory()
                if stage == "journal_author"
                else {}
            ),
        }

    def writing_gate_policy(self) -> dict[str, Any]:
        return self.writing_gate_policy_store.load_public()

    def set_writing_gate_policy(
        self,
        mode: str,
        *,
        password: str,
        password_confirmation: str = "",
    ) -> dict[str, Any]:
        result = self.writing_gate_policy_store.set_step5_mode(
            mode,
            password=password,
            password_confirmation=password_confirmation,
        )
        wakeup: dict[str, Any] = {"requested": False}
        if result.get("mode") == AUTO_TRUST_MODE:
            adapter = self.adapters["writing"]
            state = adapter.load_state()
            settings = state.get("settings", {}) if isinstance(state, dict) else {}
            scheduling_mode = (
                str(settings.get("scheduling_mode") or "manual").strip().lower()
                if isinstance(settings, dict)
                else "manual"
            )
            if scheduling_mode != "manual":
                runtime = getattr(adapter, "runtime", None)
                start_background = getattr(runtime, "start_background", None)
                if callable(start_background):
                    try:
                        wakeup = {"requested": True, "result": start_background()}
                    except Exception as exc:
                        wakeup = {
                            "requested": True,
                            "ok": False,
                            "blocker": str(getattr(exc, "code", type(exc).__name__)),
                            "message": str(exc),
                        }
        result["runtime_wakeup"] = wakeup
        return result

    def add_writing_workflow_step(self, name: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.add_custom_step(name)
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "add_writing_workflow_step", "result": result}
            )
            state["last_scheduler_result"] = {
                "status": "writing_workflow_updated",
                "action": "add_step",
                "active_version_id": result.get("active_version_id"),
                "step_count": result.get("step_count"),
            }
            self.save(state)
            return result

    def remove_writing_workflow_step(self, step_id: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.remove_step(step_id)
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "remove_writing_workflow_step", "step_id": step_id, "result": result}
            )
            state["last_scheduler_result"] = {
                "status": "writing_workflow_updated",
                "action": "remove_step",
                "active_version_id": result.get("active_version_id"),
                "step_count": result.get("step_count"),
            }
            self.save(state)
            return result

    def restore_default_writing_workflow(self) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.restore_default()
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "restore_default_writing_workflow", "result": result}
            )
            state["last_scheduler_result"] = {
                "status": "writing_workflow_updated",
                "action": "restore_default",
                "active_version_id": result.get("active_version_id"),
                "step_count": result.get("step_count"),
            }
            self.save(state)
            return result

    def select_writing_prompt_scheme(self, identifier: str) -> dict[str, Any]:
        # Publishing settings is deliberately independent of live queues.
        with _unified_state_lock():
            return self.writing_workflow_store.select_prompt_scheme(identifier)

    def save_writing_scheme_prompt(self, step_id: str, text: str, identifier: str) -> dict[str, Any]:
        from .writing_prompt_schemes import scheme_id
        with _unified_state_lock():
            if scheme_id(self.writing_workflow_store.active_snapshot()) != identifier:
                raise ValueError("提示词方案已切换，请重新打开该步骤的提示词。")
            return self.writing_workflow_store.save_scheme_prompt(step_id, text)

    def move_writing_workflow_step(self, step_id: str, direction: int) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.move_custom_step(step_id, direction)
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "move_writing_workflow_step",
                    "step_id": step_id,
                    "direction": int(direction),
                    "result": result,
                }
            )
            self.save(state)
            return result

    def read_writing_workflow_prompt(self, step_id: str) -> dict[str, Any]:
        return self.writing_workflow_store.read_custom_prompt(step_id)

    def save_writing_workflow_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.save_custom_prompt(step_id, text)
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "save_writing_workflow_prompt", "step_id": step_id}
            )
            self.save(state)
            return result

    def update_writing_default_execution_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.update_default_execution_profile(profile)
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "update_writing_default_execution_profile"}
            )
            self.save(state)
            return result

    def update_writing_step_execution_profile(
        self,
        step_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.writing_workflow_store.update_step_execution_profile(step_id, profile)
            state = self.sync(persist=False, reconcile_writing=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "update_writing_step_execution_profile",
                    "step_id": step_id,
                }
            )
            self.save(state)
            return result

    def _record_figure_workflow_update(
        self,
        action: str,
        result: dict[str, Any],
        **detail: Any,
    ) -> dict[str, Any]:
        state = self.sync(persist=False, reconcile_writing=False)
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": f"figure_workflow_{action}",
                **detail,
                "result": result,
            }
        )
        state["last_scheduler_result"] = {
            "status": "figure_workflow_updated",
            "action": action,
            "active_version_id": result.get("active_version_id"),
            "step_count": result.get("step_count"),
        }
        self.save(state)
        return result

    def add_figure_workflow_step(self, name: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.add_custom_step(name)
            return self._record_figure_workflow_update("add_step", result)

    def remove_figure_workflow_step(self, step_id: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.remove_step(step_id)
            return self._record_figure_workflow_update("remove_step", result, step_id=step_id)

    def restore_default_figure_workflow(self) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.restore_default()
            return self._record_figure_workflow_update("restore_default", result)

    def move_figure_workflow_step(self, step_id: str, direction: int) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.move_custom_step(step_id, direction)
            return self._record_figure_workflow_update(
                "move_step",
                result,
                step_id=step_id,
                direction=int(direction),
            )

    def read_figure_workflow_prompt(self, step_id: str) -> dict[str, Any]:
        return self.figure_workflow_store.read_custom_prompt(step_id)

    def save_figure_workflow_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.save_custom_prompt(step_id, text)
            return self._record_figure_workflow_update("save_prompt", result, step_id=step_id)

    def update_figure_default_execution_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.update_default_execution_profile(profile)
            return self._record_figure_workflow_update("update_default_execution_profile", result)

    def update_figure_step_execution_profile(
        self,
        step_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.figure_workflow_store.update_step_execution_profile(step_id, profile)
            return self._record_figure_workflow_update(
                "update_step_execution_profile",
                result,
                step_id=step_id,
            )

    def _record_journal_author_workflow_update(
        self,
        action: str,
        result: dict[str, Any],
        **detail: Any,
    ) -> dict[str, Any]:
        state = self.sync(persist=False, reconcile_writing=False)
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": f"journal_author_workflow_{action}",
                **detail,
                "result": result,
            }
        )
        state["last_scheduler_result"] = {
            "status": "journal_author_workflow_updated",
            "action": action,
            "active_version_id": result.get("active_version_id"),
            "step_count": result.get("step_count"),
        }
        self.save(state)
        return result

    def restore_default_journal_author_workflow(self) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.restore_default()
            return self._record_journal_author_workflow_update("restore_default", result)

    def set_journal_author_workflow_mode(self, mode: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.set_mode(mode)
            return self._record_journal_author_workflow_update("set_mode", result, mode=mode)

    def add_journal_author_workflow_step(self, name: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.add_custom_step(name)
            return self._record_journal_author_workflow_update("add_step", result)

    def remove_journal_author_workflow_step(self, step_id: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.remove_step(step_id)
            return self._record_journal_author_workflow_update("remove_step", result, step_id=step_id)

    def move_journal_author_workflow_step(self, step_id: str, direction: int) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.move_custom_step(step_id, direction)
            return self._record_journal_author_workflow_update(
                "move_step", result, step_id=step_id, direction=int(direction)
            )

    def read_journal_author_workflow_prompt(self, step_id: str) -> dict[str, Any]:
        return self.journal_author_workflow_store.read_custom_prompt(step_id)

    def save_journal_author_workflow_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.save_custom_prompt(step_id, text)
            return self._record_journal_author_workflow_update("save_prompt", result, step_id=step_id)

    def update_journal_author_default_execution_profile(
        self,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.update_default_execution_profile(profile)
            return self._record_journal_author_workflow_update(
                "update_default_execution_profile",
                result,
            )

    def update_journal_author_step_execution_profile(
        self,
        step_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        with _unified_state_lock():
            result = self.journal_author_workflow_store.update_step_execution_profile(step_id, profile)
            return self._record_journal_author_workflow_update(
                "update_step_execution_profile",
                result,
                step_id=step_id,
            )

    def stage_available_models(self, stage: str) -> dict[str, Any]:
        if stage not in {"writing", "figures", "journal_author"}:
            raise KeyError(f"阶段 {stage} 不使用 V2 Codex SDK 模型列表")
        runtime = getattr(self.adapters[stage], "runtime", None)
        method = getattr(runtime, "list_models", None)
        if not callable(method):
            raise RuntimeError("当前阶段运行器不支持读取模型列表")
        result = method()
        if not isinstance(result, dict):
            raise RuntimeError("模型列表返回格式无效")
        return result

    def read_prompt_file(self, stage: str, path: str) -> dict[str, Any]:
        prompt_path = self._validate_prompt_path(stage, Path(path))
        return {"path": str(prompt_path), "text": read_text(prompt_path), "exists": prompt_path.exists()}

    def save_prompt_file(self, stage: str, path: str, text: str) -> dict[str, Any]:
        prompt_path = self._validate_prompt_path(stage, Path(path))
        with _unified_state_lock():
            backup = backup_file(prompt_path, f"{stage}_prompt")
            write_text(prompt_path, text)
            state = self.sync(persist=False)
            result = {"status": "save_prompt_file", "stage": stage, "path": str(prompt_path), "backup": backup}
            state.setdefault("history", []).append({"at": now_iso(), "event": "save_prompt_file", "result": result})
            state["last_scheduler_result"] = result
            self.save(state)
            return result

    def save_stage_scheduler_settings(self, stage: str, values: dict[str, Any]) -> dict[str, Any]:
        if stage not in self.adapters:
            raise KeyError(f"Unknown stage: {stage}")
        adapter = self.adapters[stage]
        with _unified_state_lock():
            mutation_scope = (
                adapter.pipeline_mutation_scope()
                if stage in {"writing", "journal_author"}
                else nullcontext()
            )
            with mutation_scope:
                legacy_state = adapter.load_state()
                settings = legacy_state.setdefault("settings", {})
                if not isinstance(settings, dict):
                    settings = {}
                    legacy_state["settings"] = settings
                mode = str(values.get("scheduling_mode") or settings.get("scheduling_mode") or "manual").strip()
                if mode:
                    settings["scheduling_mode"] = mode
                try:
                    heartbeat = int(values.get("heartbeat_minutes") or settings.get("heartbeat_minutes") or 0)
                except (TypeError, ValueError):
                    heartbeat = 0
                if heartbeat > 0:
                    settings["heartbeat_minutes"] = heartbeat
                if "one_action_per_run" in values:
                    settings["one_action_per_run"] = bool(values.get("one_action_per_run"))
                if "max_parallel_workers" in values:
                    raw_parallel = str(values.get("max_parallel_workers") or "").strip().lower()
                    if stage == "writing":
                        try:
                            max_parallel = int(raw_parallel)
                        except (TypeError, ValueError):
                            raise ValueError(
                                f"写论文最大并发必须是 1-{WRITING_MAX_PARALLEL_WORKERS} 的整数"
                            ) from None
                        if not 1 <= max_parallel <= WRITING_MAX_PARALLEL_WORKERS:
                            raise ValueError(f"写论文最大并发必须是 1-{WRITING_MAX_PARALLEL_WORKERS} 的整数")
                        settings["max_parallel_workers"] = max_parallel
                        settings["dispatch_mode"] = "sdk_event_driven"
                        settings["writing_dispatch_protocol"] = WRITING_SDK_DISPATCH_PROTOCOL
                        settings["one_action_per_run"] = False
                    elif stage == "figures":
                        try:
                            max_parallel = int(raw_parallel)
                        except (TypeError, ValueError):
                            raise ValueError(
                                f"画图最大并发必须是 1-{FIGURE_MAX_PARALLEL_WORKERS} 的整数"
                            ) from None
                        if not 1 <= max_parallel <= FIGURE_MAX_PARALLEL_WORKERS:
                            raise ValueError(
                                f"画图最大并发必须是 1-{FIGURE_MAX_PARALLEL_WORKERS} 的整数"
                            )
                        settings["max_parallel_workers"] = max_parallel
                        settings["dispatch_mode"] = "sdk_event_driven"
                        settings["one_action_per_run"] = False
                    elif stage == "journal_author":
                        try:
                            max_parallel = int(raw_parallel)
                        except (TypeError, ValueError):
                            raise ValueError(
                                f"选刊作者最大并发必须是 1-{JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS} 的整数"
                            ) from None
                        if not 1 <= max_parallel <= JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS:
                            raise ValueError(
                                f"选刊作者最大并发必须是 1-{JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS} 的整数"
                            )
                        settings["max_parallel_workers"] = max_parallel
                        settings["dispatch_mode"] = "sdk_event_driven"
                        settings["journal_author_dispatch_protocol"] = JOURNAL_AUTHOR_SDK_DISPATCH_PROTOCOL
                        settings["one_action_per_run"] = False
                    elif raw_parallel in {"", "all", "unlimited", "none"}:
                        settings["max_parallel_workers"] = "all"
                    else:
                        try:
                            max_parallel = int(raw_parallel)
                        except (TypeError, ValueError):
                            raise ValueError("max_parallel_workers must be 'all' or a positive integer") from None
                        if max_parallel <= 0:
                            raise ValueError("max_parallel_workers must be 'all' or a positive integer")
                        settings["max_parallel_workers"] = max_parallel
                target_thread_id = str(values.get("scheduler_target_thread_id") or "").strip()
                if target_thread_id:
                    settings["scheduler_target_thread_id"] = target_thread_id
                elif "scheduler_target_thread_id" in values:
                    settings.pop("scheduler_target_thread_id", None)
                settings["updated_by_unified_prompt_settings_at"] = now_iso()
                enabled = mode.lower() not in {"manual", "pause", "paused", "off", "false", "0"}
                if enabled:
                    self._require_legacy_execution_mode("启用阶段调度")
                    if stage in {"writing", "journal_author"}:
                        runtime = getattr(adapter, "runtime", None)
                        ensure_ready = getattr(runtime, "ensure_ready", None)
                        if not callable(ensure_ready):
                            raise RuntimeError(f"{stage.upper()}_SDK_RUNTIME_UNAVAILABLE")
                        ensure_ready()
                    else:
                        self.automation.require_controllable(stage, legacy_state)
                adapter.save_state(legacy_state)
                if stage not in {"writing", "journal_author"}:
                    automation_result = self.automation.set_enabled(stage, enabled, legacy_state)
            if stage in {"writing", "journal_author"}:
                automation_result = adapter.start() if enabled else adapter.pause()
            state = self.sync(persist=False)
            result = {
                "status": "save_stage_scheduler_settings",
                "stage": stage,
                "settings": settings,
                "automation": automation_result,
            }
            state.setdefault("history", []).append({"at": now_iso(), "event": "save_stage_scheduler_settings", "result": result})
            state["last_scheduler_result"] = result
            self.save(state)
            return result

    def delete_papers(self, paper_ids: list[str], *, force_active: bool = False) -> dict[str, Any]:
        """Backward-compatible name for the recoverable global archive action."""

        return self.archive_papers(paper_ids, force_active=force_active)

    def archive_papers(self, paper_ids: list[str], *, force_active: bool = False) -> dict[str, Any]:
        """Archive complete paper lifecycles, not just one stage's directory.

        Operational records are removed from all four stages, both V2 SDK
        journals, the retired V1 writing dispatch ledger, and Smart Submission.
        Files and the removed state snapshot are retained under one archive
        bundle so this action remains recoverable.
        """

        ids = self._normalize_paper_ids(paper_ids)
        if not ids:
            return {"status": "idle", "message": "no paper ids selected", "archived": [], "skipped": []}

        with _unified_state_lock():
            state_before = self.sync(persist=False, reconcile_writing=False)
            archived_index = state_before.get("archived_papers", {})
            if not isinstance(archived_index, dict):
                archived_index = {}
            running = self._active_lifecycle_tasks(ids)
            active_submissions = self._active_submission_ids(ids)
            archive_ids: list[str] = []
            results: dict[str, Any] = {
                "status": "archived",
                "requested": ids,
                "archived": [],
                "skipped": [],
                "legacy_state": {},
                "auxiliary_state": {},
                "moved_paths": {},
                "archive_root": "",
            }
            for paper_id in ids:
                if paper_id in archived_index:
                    results["skipped"].append({"paper_id": paper_id, "reason": "already_archived"})
                    continue
                if running.get(paper_id):
                    results["skipped"].append(
                        {
                            "paper_id": paper_id,
                            "reason": "active_sdk_turn",
                            "message": "当前 AI Step 尚未结束；请先暂停并等待当前 Step 结束后再归档。",
                            "sources": running[paper_id],
                        }
                    )
                    continue
                if paper_id in active_submissions and not force_active:
                    results["skipped"].append(
                        {
                            "paper_id": paper_id,
                            "reason": "active_submission",
                            "message": "Active submitted papers are preserved unless force_active is used.",
                        }
                    )
                    continue
                if not self._paper_exists_in_lifecycle(paper_id, state_before):
                    results["skipped"].append({"paper_id": paper_id, "reason": "not_found"})
                    continue
                archive_ids.append(paper_id)

            if not archive_ids:
                results["status"] = "no_change"
                return results

            archived_at = now_iso()
            archive_root = ARCHIVED_PAPERS_DIR / timestamp_id()
            archive_root.mkdir(parents=True, exist_ok=True)
            results["archive_root"] = str(archive_root)
            thread_ids = self._sdk_thread_ids(archive_ids)

            legacy_snapshots: dict[str, Any] = {}
            for stage, adapter in self.adapters.items():
                stage_state = adapter.load_state()
                stage_changes: dict[str, Any] = {}
                stage_snapshot: dict[str, Any] = {}
                for paper_id in archive_ids:
                    snapshot = self._snapshot_state_for_paper(stage_state, paper_id)
                    removed = self._remove_from_legacy_state(stage_state, paper_id)
                    if snapshot:
                        stage_snapshot[paper_id] = snapshot
                    if removed:
                        stage_changes[paper_id] = removed
                if stage_changes:
                    adapter.save_state(stage_state)
                    results["legacy_state"][stage] = stage_changes
                if stage_snapshot:
                    legacy_snapshots[stage] = stage_snapshot

            auxiliary_snapshots = self._remove_auxiliary_state_records(archive_ids)
            results["auxiliary_state"] = {
                source: sorted(records) for source, records in auxiliary_snapshots.items() if records
            }

            for paper_id in archive_ids:
                sources = self._lifecycle_file_sources(
                    paper_id,
                    include_archives=False,
                    thread_ids=thread_ids.get(paper_id, set()),
                )
                paper_root = archive_root / paper_id
                moved = self._move_lifecycle_sources(paper_id, sources, paper_root / "files")
                results["moved_paths"][paper_id] = moved
                write_json_atomic(
                    paper_root / "archive_manifest.json",
                    {
                        "schema": "unified-paper-orchestrator/paper-archive/v1",
                        "paper_id": paper_id,
                        "archived_at": archived_at,
                        "moved_paths": moved,
                        "legacy_state": {
                            stage: records.get(paper_id)
                            for stage, records in legacy_snapshots.items()
                            if paper_id in records
                        },
                        "auxiliary_state": {
                            source: records.get(paper_id)
                            for source, records in auxiliary_snapshots.items()
                            if paper_id in records
                        },
                    },
                )

            refreshed = self.sync(persist=False, reconcile_writing=False)
            refreshed["paper_queue"] = [
                str(item) for item in refreshed.get("paper_queue", []) if str(item) not in archive_ids
            ]
            refreshed_archived = refreshed.get("archived_papers", {})
            if not isinstance(refreshed_archived, dict):
                refreshed_archived = {}
            for paper_id in archive_ids:
                refreshed.get("papers", {}).pop(paper_id, None)
                refreshed.get("submission_routes", {}).pop(paper_id, None)
                refreshed.get("submission_route_operations", {}).pop(paper_id, None)
                refreshed.get("stage_admission_overrides", {}).pop(paper_id, None)
                refreshed_archived[paper_id] = {
                    "status": "archived",
                    "archived_at": archived_at,
                    "archive_dir": str(archive_root / paper_id),
                }
            refreshed["archived_papers"] = refreshed_archived
            results["archived"] = archive_ids
            persisted_result = {
                key: value
                for key, value in results.items()
                if key not in {"moved_paths"}
            }
            refreshed.setdefault("history", []).append(
                {"at": archived_at, "event": "archive_papers", "result": persisted_result}
            )
            refreshed["last_scheduler_result"] = persisted_result
            backup_file(UNIFIED_STATE_PATH, "unified_before_archive")
            self.save(refreshed)
            return results

    def purge_papers(self, paper_ids: list[str], *, force_active: bool = False) -> dict[str, Any]:
        """Irreversibly remove selected papers from every managed stage and archive."""

        ids = self._normalize_paper_ids(paper_ids)
        if not ids:
            return {"status": "idle", "message": "no paper ids selected", "purged": [], "skipped": []}

        with _unified_state_lock():
            state_before = self.sync(persist=False, reconcile_writing=False)
            running = self._active_lifecycle_tasks(ids)
            active_submissions = self._active_submission_ids(ids)
            purge_ids: list[str] = []
            results: dict[str, Any] = {
                "status": "purged",
                "requested": ids,
                "purged": [],
                "skipped": [],
                "legacy_state": {},
                "auxiliary_state": {},
                "deleted_paths": {},
            }
            for paper_id in ids:
                if running.get(paper_id):
                    results["skipped"].append(
                        {
                            "paper_id": paper_id,
                            "reason": "active_sdk_turn",
                            "message": "当前 AI Step 尚未结束；请先暂停并等待当前 Step 结束后再彻底删除。",
                            "sources": running[paper_id],
                        }
                    )
                    continue
                if paper_id in active_submissions and not force_active:
                    results["skipped"].append(
                        {
                            "paper_id": paper_id,
                            "reason": "active_submission",
                            "message": "Active submitted papers require force_active for permanent deletion.",
                        }
                    )
                    continue
                if not self._paper_exists_in_lifecycle(paper_id, state_before):
                    results["skipped"].append({"paper_id": paper_id, "reason": "not_found"})
                    continue
                purge_ids.append(paper_id)

            if not purge_ids:
                results["status"] = "no_change"
                return results

            thread_ids = self._sdk_thread_ids(purge_ids)
            for stage, adapter in self.adapters.items():
                stage_state = adapter.load_state()
                stage_changes: dict[str, Any] = {}
                for paper_id in purge_ids:
                    removed = self._remove_from_legacy_state(stage_state, paper_id)
                    if removed:
                        stage_changes[paper_id] = removed
                if stage_changes:
                    adapter.save_state(stage_state)
                    results["legacy_state"][stage] = stage_changes

            auxiliary = self._remove_auxiliary_state_records(purge_ids)
            results["auxiliary_state"] = {
                source: sorted(records) for source, records in auxiliary.items() if records
            }
            for paper_id in purge_ids:
                sources = self._lifecycle_file_sources(
                    paper_id,
                    include_archives=True,
                    thread_ids=thread_ids.get(paper_id, set()),
                )
                results["deleted_paths"][paper_id] = self._delete_lifecycle_sources(paper_id, sources)

            refreshed = self.sync(persist=False, reconcile_writing=False)
            archived = refreshed.get("archived_papers", {})
            if not isinstance(archived, dict):
                archived = {}
            for paper_id in purge_ids:
                self._remove_from_legacy_state(refreshed, paper_id)
                refreshed.get("submission_routes", {}).pop(paper_id, None)
                refreshed.get("submission_route_operations", {}).pop(paper_id, None)
                refreshed.get("stage_admission_overrides", {}).pop(paper_id, None)
                archived.pop(paper_id, None)
            refreshed["archived_papers"] = archived
            purged_at = now_iso()
            id_hashes = [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in purge_ids]
            persisted_result = {
                "status": "purged",
                "purged_count": len(purge_ids),
                "paper_id_sha256": id_hashes,
                "deleted_path_count": sum(len(value) for value in results["deleted_paths"].values()),
            }
            refreshed.setdefault("history", []).append(
                {"at": purged_at, "event": "purge_papers", "result": persisted_result}
            )
            refreshed["last_scheduler_result"] = persisted_result
            refreshed["paper_queue"] = [
                str(item) for item in refreshed.get("paper_queue", []) if str(item) not in purge_ids
            ]
            self._append_purge_audit(purged_at, persisted_result)
            self.save(refreshed)
            results["purged"] = purge_ids
            return results

    def advance_once(self, apply: bool = False, paper_id: str = "") -> dict[str, Any]:
        if apply:
            self._require_legacy_execution_mode("执行统一队列下一步")
            with _unified_state_lock():
                return self._advance_once_unlocked(apply=True, paper_id=paper_id)
        return self._advance_once_unlocked(apply=False, paper_id=paper_id)

    def _advance_once_unlocked(self, apply: bool = False, paper_id: str = "") -> dict[str, Any]:
        state = self.sync(persist=False)
        selected_id = paper_id or self._first_queued_actionable(state)
        if not selected_id:
            result = {"applied": False, "status": "idle", "message": "统一队列为空，未执行。"}
            if apply:
                state["last_scheduler_result"] = result
                self.save(state)
            return result

        record = state["papers"].get(selected_id)
        if not record:
            result = {"applied": False, "status": "missing", "paper_id": selected_id, "message": "队首论文不在统一状态中。"}
            if apply:
                state["last_scheduler_result"] = result
                self.save(state)
            return result

        action = str(record.get("next_action") or "none")
        plan = self._plan_for(selected_id, record)
        result: dict[str, Any] = {
            "applied": False,
            "status": "planned",
            "paper_id": selected_id,
            "next_action": action,
            "plan": plan,
        }
        if not apply:
            result["message"] = "dry-run only; use --apply to execute this program-controlled handoff."
            return result

        if action == "queue_writing":
            result["legacy_result"] = self.adapters["writing"].queue(selected_id)
        elif action == "queue_figures":
            artifact = record["stages"]["writing"].get("artifact", "")
            result["legacy_result"] = self.adapters["figures"].queue(selected_id, artifact)
        elif action == "queue_journal_author":
            artifact = record["stages"]["figures"].get("artifact", "")
            result["legacy_result"] = self.adapters["journal_author"].queue(selected_id, artifact)
        elif action == "queue_submission":
            route = submission_route_for(state.get("submission_routes"), selected_id)
            owner = str(route["execution_owner"])
            result["execution_owner"] = owner
            if owner != "legacy_auto":
                result["status"] = "route_owned_elsewhere"
                result["message"] = (
                    f"该论文投稿执行路径为 {owner}；统一总控拒绝写入稳定投稿队列。"
                )
            else:
                result["status"] = "smart_submission_required"
                result["message"] = (
                    "旧稳定投稿写入已停用；请使用 Smart Submission 准备与 SDK 队列入口。"
                )
        elif action.startswith("wait_"):
            result["status"] = "waiting"
            result["message"] = str(record.get("blocked_reason") or "等待上游或当前调度器完成。")
        elif action in {"none", "blocked"}:
            result["status"] = action
            result["message"] = str(record.get("blocked_reason") or "没有可执行动作。")
        else:
            result["status"] = "unknown_action"
            result["message"] = f"未知动作：{action}"

        if "legacy_result" in result:
            result["applied"] = True
            result["status"] = "applied"
            if action == "queue_writing":
                result["message"] = "已写回写作项目状态，并启动写论文调度器。"
            elif action == "queue_figures":
                result["message"] = "已导入最新论文源文件，并自动启动画图 V2 SDK 固定会话。"
            else:
                result["message"] = "已写回对应旧项目状态并加入队列；未启动调度器。需要执行时请点击“启动本阶段调度器”。"

        refreshed = self.sync(persist=False)
        refreshed["paper_queue"] = state.get("paper_queue", [])
        refreshed.setdefault("history", state.get("history", []))
        refreshed["history"].append({"at": now_iso(), "event": "advance_once", "result": result})
        refreshed["last_scheduler_result"] = result
        self.save(refreshed)
        return result

    def start_all(self) -> dict[str, Any]:
        router = getattr(self, "backend_router", None)
        if not isinstance(router, BackendRouter):
            return self._start_all_legacy()
        return router.execute(
            "stage.start_all",
            legacy=self._start_all_legacy,
            new_backend=self._stage_backend_callback("start_all"),
        )

    def _start_all_legacy(self) -> dict[str, Any]:
        self._require_legacy_execution_mode("启动全部阶段")
        # Writing now has an EXE-local SDK runtime.  Only the three remaining
        # V1 stages require Codex Automation control.
        for stage, adapter in self.adapters.items():
            if stage != "writing":
                self.automation.require_controllable(stage, adapter.load_state())
        with _unified_state_lock():
            results: dict[str, Any] = {}
            for stage, adapter in self.adapters.items():
                try:
                    results[stage] = {"ok": True, "automation": adapter.start()}
                except Exception as exc:
                    results[stage] = {"ok": False, "error": str(exc)}
            state = self.sync(persist=False)
            state.setdefault("history", []).append({"at": now_iso(), "event": "start_all", "results": results})
            state["last_scheduler_result"] = {"status": "start_all", "results": results}
            self.save(state)
            return state

    @staticmethod
    def _normalize_paper_ids(paper_ids: list[str]) -> list[str]:
        result: list[str] = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in result:
                result.append(paper_id)
        return result

    @staticmethod
    def _paper_key_matches(value: Any, paper_id: str) -> bool:
        key = str(value or "").strip()
        return bool(
            key == paper_id
            or key.endswith(f":{paper_id}")
            or key.startswith(f"{paper_id}:")
        )

    def _record_identity_matches(self, key: Any, value: Any, paper_id: str) -> bool:
        """Match the owner of a lifecycle record without scanning its payload.

        SDK records contain provenance snapshots and artifact paths from other
        papers.  Treating any nested mention as ownership can therefore delete
        an unrelated task.  Only the mapping key or explicit identity fields
        are allowed to establish record ownership.
        """

        if self._paper_key_matches(key, paper_id):
            return True
        if isinstance(value, str):
            return self._paper_key_matches(value, paper_id)
        if not isinstance(value, dict):
            return False
        for field in (
            "paper_id",
            "pipeline_id",
            "canonical_paper_id",
            "source_paper_id",
            "target_paper_id",
            "paper_key",
        ):
            if self._paper_key_matches(value.get(field), paper_id):
                return True
        return False

    def _snapshot_state_for_paper(self, state: dict[str, Any], paper_id: str) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        singleton_mappings = {
            "last_scheduler_action",
            "last_scheduler_result",
            "batch_circuit",
        }
        for name, value in state.items():
            if isinstance(value, dict):
                matched = {
                    str(key): item
                    for key, item in value.items()
                    if self._record_identity_matches(key, item, paper_id)
                }
                if matched:
                    snapshot[name] = matched
                elif name in singleton_mappings and self._record_identity_matches(name, value, paper_id):
                    snapshot[name] = value
            elif isinstance(value, list):
                matched_items = [
                    item for item in value if self._record_identity_matches("", item, paper_id)
                ]
                if matched_items:
                    snapshot[name] = matched_items
            elif isinstance(value, str) and self._paper_key_matches(value, paper_id):
                snapshot[name] = value
        return snapshot

    @staticmethod
    def _auxiliary_state_specs() -> tuple[tuple[str, Path, Path | None], ...]:
        return (
            ("writing_sdk", Path(WRITING_SDK_JOURNAL_PATH), Path(WRITING_SDK_LOCK_PATH)),
            ("figure_sdk", Path(FIGURE_SDK_JOURNAL_PATH), Path(FIGURE_SDK_LOCK_PATH)),
            (
                "journal_author_sdk",
                Path(JOURNAL_AUTHOR_SDK_JOURNAL_PATH),
                Path(JOURNAL_AUTHOR_SDK_LOCK_PATH),
            ),
            ("writing_v1_dispatch", Path(WRITING_DISPATCH_STATE_PATH), Path(WRITING_DISPATCH_LOCK_PATH)),
            ("smart_submission", Path(SMART_SUBMISSION_STATE_PATH), None),
        )

    @staticmethod
    def _auxiliary_state_lock(source: str, path: Path, lock_path: Path | None) -> Any:
        if source == "smart_submission":
            try:
                from . import smart_submission as smart_submission_module

                if Path(smart_submission_module.SMART_STATE_PATH).resolve() == path.resolve():
                    return smart_submission_module._smart_state_lock()
            except Exception:
                pass
            return FileLock(path.with_suffix(path.suffix + ".lifecycle.lock"))
        if lock_path is None:
            return nullcontext()
        return FileLock(lock_path, stale_seconds=7 * 24 * 60 * 60)

    def _remove_auxiliary_state_records(self, paper_ids: list[str]) -> dict[str, dict[str, Any]]:
        removed_by_source: dict[str, dict[str, Any]] = {}
        for source, path, lock_path in self._auxiliary_state_specs():
            if not path.exists():
                continue
            with self._auxiliary_state_lock(source, path, lock_path):
                state = read_json(path, {})
                if not isinstance(state, dict):
                    raise ValueError(f"Lifecycle state must be a JSON object: {path}")
                source_removed: dict[str, Any] = {}
                changed = False
                for paper_id in paper_ids:
                    snapshot = self._snapshot_state_for_paper(state, paper_id)
                    removed = self._remove_from_legacy_state(state, paper_id)
                    if snapshot:
                        source_removed[paper_id] = snapshot
                    if removed:
                        changed = True
                if changed:
                    backup_file(path, f"{source}_before_paper_archive")
                    state["updated_at"] = now_iso()
                    write_json_atomic(path, state)
                if source_removed:
                    removed_by_source[source] = source_removed
        return removed_by_source

    def _active_lifecycle_tasks(self, paper_ids: list[str]) -> dict[str, list[str]]:
        active: dict[str, list[str]] = {paper_id: [] for paper_id in paper_ids}
        active_statuses = {
            "creating_thread",
            "starting",
            "running",
            "waiting_event",
            "dispatching",
            "in_progress",
            "send_uncertain",
            "reserved",
            "dispatched",
        }
        for source, path, _lock_path in self._auxiliary_state_specs():
            state = read_json(path, {}) if path.exists() else {}
            if not isinstance(state, dict):
                continue
            if source in {"writing_sdk", "figure_sdk", "journal_author_sdk", "smart_submission"}:
                records = state.get("papers", {})
            else:
                records = state.get("claims", {})
            if not isinstance(records, dict):
                continue
            for key, record in records.items():
                if not isinstance(record, dict):
                    continue
                status = str(record.get("status") or record.get("state") or "").strip().lower()
                if status not in active_statuses:
                    continue
                for paper_id in paper_ids:
                    if self._record_identity_matches(key, record, paper_id):
                        marker = f"{source}:{status}"
                        if marker not in active[paper_id]:
                            active[paper_id].append(marker)
        return {paper_id: markers for paper_id, markers in active.items() if markers}

    def _sdk_thread_ids(self, paper_ids: list[str]) -> dict[str, set[str]]:
        result = {paper_id: set() for paper_id in paper_ids}

        def collect(value: Any, destination: set[str], *, key_hint: str = "") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    collect(item, destination, key_hint=str(key).lower())
                return
            if isinstance(value, list):
                for item in value:
                    collect(item, destination, key_hint=key_hint)
                return
            if isinstance(value, str) and "thread" in key_hint:
                candidate = value.strip()
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", candidate):
                    destination.add(candidate)

        for source, path, _lock_path in self._auxiliary_state_specs():
            if source not in {"writing_sdk", "figure_sdk", "journal_author_sdk"}:
                continue
            state = read_json(path, {}) if path.exists() else {}
            records = state.get("papers", {}) if isinstance(state, dict) else {}
            if not isinstance(records, dict):
                continue
            for key, record in records.items():
                if not isinstance(record, dict):
                    continue
                for paper_id in paper_ids:
                    if self._paper_key_matches(key, paper_id) or str(record.get("paper_id") or "") == paper_id:
                        collect(record, result[paper_id])
        return result

    def _paper_exists_in_lifecycle(self, paper_id: str, unified_state: dict[str, Any]) -> bool:
        for key in ("papers", "archived_papers"):
            mapping = unified_state.get(key, {})
            if isinstance(mapping, dict) and paper_id in mapping:
                return True
        try:
            if self._exists_in_legacy(paper_id):
                return True
        except Exception:
            pass
        for _source, path, _lock_path in self._auxiliary_state_specs():
            state = read_json(path, {}) if path.exists() else {}
            if not isinstance(state, dict):
                continue
            for key in ("papers", "sessions", "claims"):
                records = state.get(key, {})
                if not isinstance(records, dict):
                    continue
                if any(
                    self._record_identity_matches(record_key, record, paper_id)
                    for record_key, record in records.items()
                ):
                    return True
        return bool(self._lifecycle_file_sources(paper_id, include_archives=True, thread_ids=set()))

    @staticmethod
    def _trash_bundle_matches(path: Path, paper_id: str) -> bool:
        name = path.name
        if name.endswith(f"_{paper_id}") or re.fullmatch(rf".+_{re.escape(paper_id)}_\d+", name):
            return True
        manifest = path / "deleted_manifest.json"
        if manifest.is_file():
            data = read_json(manifest, {})
            if isinstance(data, dict):
                return str(data.get("paper_id") or data.get("pipeline_id") or "") == paper_id
        return False

    def _lifecycle_file_sources(
        self,
        paper_id: str,
        *,
        include_archives: bool,
        thread_ids: set[str],
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []

        def add(label: str, path: Path, root: Path) -> None:
            try:
                if path.exists():
                    sources.append({"label": label, "path": path, "root": root})
            except OSError:
                return

        writing = LEGACY_PROJECTS.get("writing", {})
        if writing:
            writing_root = Path(writing["root"])
            add("writing_idea", writing_root / "ideas" / f"{paper_id}.md", writing_root / "ideas")
            add("writing_refs", writing_root / "refs" / paper_id, writing_root / "refs")
        for stage, project in LEGACY_PROJECTS.items():
            papers_dir = Path(project["papers_dir"])
            add(stage, papers_dir / paper_id, papers_dir)
            trash_root = Path(project["root"]) / "controller" / "trash"
            if trash_root.is_dir():
                for child in trash_root.iterdir():
                    if child.is_dir() and self._trash_bundle_matches(child, paper_id):
                        add(f"{stage}_trash", child, trash_root)

        if DELETED_DIR.is_dir():
            for timestamp_dir in DELETED_DIR.iterdir():
                if timestamp_dir.is_dir():
                    add("legacy_deleted", timestamp_dir / paper_id, DELETED_DIR)
        if include_archives and ARCHIVED_PAPERS_DIR.is_dir():
            for timestamp_dir in ARCHIVED_PAPERS_DIR.iterdir():
                if timestamp_dir.is_dir():
                    add("paper_archive", timestamp_dir / paper_id, ARCHIVED_PAPERS_DIR)

        smart_root = Path(STATE_DIR) / "smart_submission"
        for folder in (
            "evidence",
            "revision_packages",
            "submission_evidence",
            "upload_staging",
            "upload_work",
            "screenshots",
            "page_snapshots",
        ):
            root = smart_root / folder
            if not root.is_dir():
                continue
            try:
                for candidate in root.rglob(f"*{paper_id}*"):
                    add(f"smart_{folder}", candidate, root)
            except OSError:
                continue

        codex_home = Path(STATE_DIR) / "codex_sdk_home"
        for thread_id in sorted(thread_ids):
            for folder in ("sessions", "archived_sessions"):
                root = codex_home / folder
                if not root.is_dir():
                    continue
                try:
                    for candidate in root.rglob(f"*-{thread_id}.jsonl"):
                        add("sdk_session", candidate, codex_home)
                except OSError:
                    continue

        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in sorted(sources, key=lambda item: len(Path(item["path"]).parts)):
            path = Path(source["path"])
            try:
                resolved = path.resolve()
            except OSError:
                continue
            key = str(resolved).casefold()
            if key in seen:
                continue
            if any(self._is_relative_to(resolved, Path(existing["path"]).resolve()) for existing in deduplicated):
                continue
            seen.add(key)
            deduplicated.append(source)
        return deduplicated

    def _safe_lifecycle_source(self, source: dict[str, Any]) -> bool:
        path = Path(source["path"])
        root = Path(source["root"])
        try:
            if path.is_symlink():
                return False
            resolved = path.resolve(strict=True)
            resolved_root = root.resolve(strict=True)
        except OSError:
            return False
        return resolved != resolved_root and self._is_relative_to(resolved, resolved_root)

    @staticmethod
    def _unique_lifecycle_target(target: Path) -> Path:
        candidate = target
        counter = 2
        while candidate.exists():
            candidate = target.with_name(f"{target.name}_{counter}")
            counter += 1
        return candidate

    def _move_lifecycle_sources(
        self,
        paper_id: str,
        sources: list[dict[str, Any]],
        target_root: Path,
    ) -> list[dict[str, str]]:
        moved: list[dict[str, str]] = []
        for source in sources:
            path = Path(source["path"])
            if not self._safe_lifecycle_source(source):
                moved.append({"source": str(path), "status": "skipped_unsafe"})
                continue
            label = re.sub(r"[^A-Za-z0-9_-]+", "_", str(source.get("label") or "files"))
            destination_dir = target_root / label
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = self._unique_lifecycle_target(destination_dir / path.name)
            try:
                shutil.move(str(path), str(destination))
            except OSError as exc:
                moved.append(
                    {
                        "source": str(path),
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            moved.append({"source": str(path), "target": str(destination), "status": "moved"})
        return moved

    def _delete_lifecycle_sources(
        self,
        paper_id: str,
        sources: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        deleted: list[dict[str, str]] = []
        for source in sources:
            path = Path(source["path"])
            if not self._safe_lifecycle_source(source):
                deleted.append({"path": str(path), "status": "skipped_unsafe"})
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                deleted.append(
                    {
                        "path": str(path),
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            deleted.append({"path": str(path), "status": "deleted"})
        return deleted

    @staticmethod
    def _append_purge_audit(at: str, result: dict[str, Any]) -> None:
        lock_path = PAPER_LIFECYCLE_AUDIT_PATH.with_suffix(".lock")
        with FileLock(lock_path):
            audit = read_json(
                PAPER_LIFECYCLE_AUDIT_PATH,
                {"schema": "unified-paper-orchestrator/paper-lifecycle-audit/v1", "events": []},
            )
            if not isinstance(audit, dict):
                audit = {"schema": "unified-paper-orchestrator/paper-lifecycle-audit/v1", "events": []}
            events = audit.setdefault("events", [])
            if not isinstance(events, list):
                events = []
                audit["events"] = events
            events.append({"at": at, "event": "purge_papers", **result})
            if len(events) > 500:
                del events[:-500]
            audit["updated_at"] = at
            write_json_atomic(PAPER_LIFECYCLE_AUDIT_PATH, audit)

    def _exists_in_legacy(self, paper_id: str) -> bool:
        return any(paper_id in adapter.all_paper_ids() for adapter in self.adapters.values())

    def _task_prompt_files(self, stage: str) -> list[Path]:
        project = LEGACY_PROJECTS[stage]
        root = Path(project["root"])
        prompt_dir = root / "prompts"
        files: list[Path] = []
        if prompt_dir.exists():
            files.extend(path for path in prompt_dir.glob("*.md") if path.is_file() and not self._is_prompt_backup(path))
        return sorted(files, key=lambda item: item.name.lower())

    def _is_prompt_backup(self, path: Path) -> bool:
        name = path.name.lower()
        return any(marker in name for marker in ("副本", "copy", "backup", ".bak", "~"))

    def _prompt_file_info(self, path: Path, label: str) -> dict[str, Any]:
        exists = path.exists()
        stat = path.stat() if exists else None
        return {
            "label": label,
            "name": path.name,
            "path": str(path),
            "exists": exists,
            "size": stat.st_size if stat else 0,
            "updated_at": stat.st_mtime if stat else 0,
        }

    def _validate_prompt_path(self, stage: str, path: Path) -> Path:
        if stage not in LEGACY_PROJECTS:
            raise KeyError(f"Unknown stage: {stage}")
        project = LEGACY_PROJECTS[stage]
        root = Path(project["root"]).resolve()
        prompt_path = path.resolve()
        allowed_paths = {Path(project["prompt_path"]).resolve()}
        allowed_paths.update(item.resolve() for item in self._task_prompt_files(stage))
        if prompt_path not in allowed_paths:
            raise ValueError(f"Prompt path is not managed by {stage}: {prompt_path}")
        if prompt_path.suffix.lower() != ".md":
            raise ValueError(f"Only Markdown prompt files can be edited: {prompt_path}")
        if not self._is_relative_to(prompt_path, root):
            raise ValueError(f"Prompt path is outside project root: {prompt_path}")
        return prompt_path

    def _active_submission_ids(self, paper_ids: list[str]) -> set[str]:
        state = self.adapters["submission"].load_state()
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        active: set[str] = set()
        for paper_id in paper_ids:
            record = papers.get(paper_id, {})
            if not isinstance(record, dict):
                continue
            if record.get("active_submission") or str(record.get("manuscript_id") or "").strip():
                active.add(paper_id)
        return active

    def _paths_for_delete(self, paper_id: str) -> list[Path]:
        paths: list[Path] = []
        for project in LEGACY_PROJECTS.values():
            papers_dir = Path(project["papers_dir"])
            candidate = papers_dir / paper_id
            if candidate.exists():
                paths.append(candidate)

        submission_state = self.adapters["submission"].load_state()
        record = submission_state.get("papers", {}).get(paper_id, {})
        if isinstance(record, dict):
            for key in ("source_package_dir", "source_paper_dir"):
                value = str(record.get(key) or "").strip()
                if not value:
                    continue
                source_path = Path(value)
                for project in LEGACY_PROJECTS.values():
                    paper_dir = Path(project["papers_dir"]) / paper_id
                    if paper_dir.exists() and self._is_relative_to(source_path, paper_dir):
                        paths.append(paper_dir)

        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return sorted(unique, key=lambda item: len(item.parts))

    def _archive_paths(self, paper_id: str, paths: list[Path], deleted_root: Path) -> list[dict[str, str]]:
        moved: list[dict[str, str]] = []
        moved_sources: list[Path] = []
        for source in paths:
            if any(source == existing or self._is_relative_to(source, existing) for existing in moved_sources):
                continue
            if not source.exists():
                continue
            if not self._safe_to_archive(source, paper_id):
                moved.append({"source": str(source), "status": "skipped_unsafe"})
                continue
            stage_name = self._stage_for_path(source)
            target_dir = deleted_root / paper_id / stage_name
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            counter = 1
            while target.exists():
                target = target_dir / f"{source.name}_{counter}"
                counter += 1
            shutil.move(str(source), str(target))
            moved.append({"source": str(source), "target": str(target), "status": "moved"})
            moved_sources.append(source)
        return moved

    def _safe_to_archive(self, path: Path, paper_id: str) -> bool:
        if path.name != paper_id:
            return False
        resolved = path.resolve()
        for project in LEGACY_PROJECTS.values():
            papers_dir = Path(project["papers_dir"]).resolve()
            if resolved == papers_dir:
                return False
            if self._is_relative_to(resolved, papers_dir):
                return True
        return False

    def _stage_for_path(self, path: Path) -> str:
        resolved = path.resolve()
        for stage, project in LEGACY_PROJECTS.items():
            if self._is_relative_to(resolved, Path(project["papers_dir"]).resolve()):
                return stage
        return "unknown"

    def _remove_from_legacy_state(self, state: dict[str, Any], paper_id: str) -> dict[str, Any]:
        removed: dict[str, Any] = {}
        for key in (
            "sessions",
            "papers",
            "claims",
            "submission_routes",
            "submission_route_operations",
            "stage_admission_overrides",
            "archived_papers",
        ):
            mapping = state.get(key)
            if not isinstance(mapping, dict):
                continue
            matching = [
                raw_key
                for raw_key, value in mapping.items()
                if self._record_identity_matches(raw_key, value, paper_id)
            ]
            for raw_key in matching:
                mapping.pop(raw_key, None)
            if matching:
                removed[key] = len(matching)

        for key, values in list(state.items()):
            if not isinstance(values, list):
                continue
            retained = [
                item
                for item in values
                if not self._record_identity_matches("", item, paper_id)
            ]
            if len(retained) != len(values):
                state[key] = retained
                removed[key] = int(removed.get(key) or 0) + len(values) - len(retained)

        for key in ("active_pipeline_id", "active_paper_id", "current_paper_id"):
            if str(state.get(key) or "") == paper_id:
                state[key] = ""
                removed[key] = True
        for key in ("last_scheduler_action", "last_scheduler_result", "batch_circuit"):
            value = state.get(key)
            if isinstance(value, dict) and self._record_identity_matches(key, value, paper_id):
                state[key] = {}
                removed[key] = True

        settings = state.get("settings")
        if isinstance(settings, dict):
            pruned, changed = self._prune_paper_references(settings, paper_id)
            if changed:
                state["settings"] = pruned
                removed["settings"] = True
        return removed

    def _prune_paper_references(self, value: Any, paper_id: str) -> tuple[Any, bool]:
        if isinstance(value, dict):
            changed = False
            result: dict[Any, Any] = {}
            for key, item in value.items():
                if self._paper_key_matches(key, paper_id):
                    changed = True
                    continue
                pruned, item_changed = self._prune_paper_references(item, paper_id)
                result[key] = pruned
                changed = changed or item_changed
            return result, changed
        if isinstance(value, list):
            result: list[Any] = []
            changed = False
            for item in value:
                if isinstance(item, str) and self._paper_key_matches(item, paper_id):
                    changed = True
                    continue
                pruned, item_changed = self._prune_paper_references(item, paper_id)
                result.append(pruned)
                changed = changed or item_changed
            return result, changed
        if isinstance(value, str) and paper_id in value:
            return "", True
        return value, False

    def _command_references_paper(self, value: Any, paper_id: str) -> bool:
        if isinstance(value, dict):
            for key in ("paper_id", "pipeline_id"):
                if str(value.get(key) or "") == paper_id:
                    return True
            return any(self._command_references_paper(item, paper_id) for item in value.values())
        if isinstance(value, list):
            return any(self._command_references_paper(item, paper_id) for item in value)
        if isinstance(value, str):
            return value == paper_id or f"\\{paper_id}\\" in value or f"/{paper_id}/" in value
        return False

    def _is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def start_stage(self, stage: str, single_active: bool = False) -> dict[str, Any]:
        if stage not in self.adapters:
            raise KeyError(f"Unknown stage: {stage}")
        router = getattr(self, "backend_router", None)
        if not isinstance(router, BackendRouter):
            return self._start_stage_legacy(stage, single_active=single_active)
        return router.execute(
            f"stage.start.{stage}",
            legacy=lambda: self._start_stage_legacy(stage, single_active=single_active),
            new_backend=self._stage_backend_callback(
                "start_stage",
                stage,
                single_active=single_active,
            ),
        )

    def _start_stage_legacy(self, stage: str, *, single_active: bool = False) -> dict[str, Any]:
        self._require_legacy_execution_mode(f"启动{STAGE_LABELS.get(stage, stage)}阶段")
        if single_active and stage != "writing":
            for other_stage, adapter in self.adapters.items():
                if other_stage not in {"writing", "figures", "journal_author"}:
                    self.automation.require_controllable(other_stage, adapter.load_state())
        elif stage not in {"writing", "figures", "journal_author"}:
            self.automation.require_controllable(stage, self.adapters[stage].load_state())
        with _unified_state_lock():
            results: dict[str, Any] = {}
            if single_active:
                for other_stage, adapter in self.adapters.items():
                    if other_stage == stage:
                        continue
                    results[other_stage] = self._pause_adapter(adapter)
            if stage != "writing":
                admission_state = self.sync(persist=False)
                results["automatic_admission"] = self._queue_admitted_stage_candidates(stage, admission_state)
            try:
                results[stage] = {"ok": True, "action": "started", "automation": self.adapters[stage].start()}
            except Exception as exc:
                results[stage] = {"ok": False, "action": "start_failed", "error": str(exc)}
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {"at": now_iso(), "event": "start_stage", "stage": stage, "single_active": single_active, "results": results}
            )
            state["last_scheduler_result"] = {
                "status": "start_stage",
                "stage": stage,
                "single_active": single_active,
                "results": results,
            }
            self.save(state)
            return state

    def _queue_admitted_stage_candidates(self, stage: str, state: dict[str, Any]) -> dict[str, Any]:
        expected_action = {
            "figures": "queue_figures",
            "journal_author": "queue_journal_author",
            "submission": "queue_submission",
        }.get(stage, "")
        if not expected_action:
            return {"queued": [], "skipped": [], "errors": []}
        result: dict[str, list[Any]] = {"queued": [], "skipped": [], "errors": []}
        for paper_id, record in sorted(state.get("papers", {}).items()):
            admission = record.get("stage_admissions", {}).get(stage, {})
            if not isinstance(admission, dict) or not admission.get("admitted"):
                continue
            if str(record.get("current_stage") or "") != stage or str(record.get("next_action") or "") != expected_action:
                continue
            try:
                if stage == "figures":
                    artifact = str(record.get("stages", {}).get("writing", {}).get("artifact") or "")
                    queued = self.adapters[stage].queue(paper_id, artifact)
                elif stage == "journal_author":
                    artifact = str(record.get("stages", {}).get("figures", {}).get("artifact") or "")
                    queued = self.adapters[stage].queue(paper_id, artifact)
                else:
                    route = submission_route_for(state.get("submission_routes"), paper_id)
                    owner = str(route["execution_owner"])
                    if owner != "legacy_auto":
                        result["skipped"].append({"paper_id": paper_id, "reason": f"route_owned_by_{owner}"})
                        continue
                    artifact = str(record.get("stages", {}).get("journal_author", {}).get("artifact") or "")
                    queued = self.adapters[stage].queue(paper_id, artifact, owner=owner)
                result["queued"].append({"paper_id": paper_id, "result": queued})
            except Exception as exc:
                result["errors"].append({"paper_id": paper_id, "error": str(exc)})
        return result

    def start_new_writing_session(self, single_active: bool = False) -> dict[str, Any]:
        return self.start_new_writing_sessions(1, single_active=single_active)

    def start_new_writing_sessions(
        self,
        count: int,
        *,
        single_active: bool = False,
        request_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_legacy_execution_mode("从 0 开始写论文")
        with _unified_state_lock():
            results: dict[str, Any] = {}
            if single_active:
                for other_stage, adapter in self.adapters.items():
                    if other_stage == "writing":
                        continue
                    results[other_stage] = self._pause_adapter(adapter)
            try:
                writing_adapter = self.adapters["writing"]
                legacy_result = (
                    writing_adapter.new_sessions(count)
                    if request_context is None
                    else writing_adapter.new_sessions(
                        count,
                        request_context=request_context,
                    )
                )
                results["writing"] = {
                    "ok": True,
                    "action": "new_sessions",
                    "legacy_result": legacy_result,
                }
            except Exception as exc:
                results["writing"] = {"ok": False, "action": "new_sessions_failed", "error": str(exc)}
            state = self.sync(persist=False)
            state.setdefault("history", []).append(
                {
                    "at": now_iso(),
                    "event": "start_new_writing_session",
                    "requested_count": count,
                    "single_active": single_active,
                    "request_context": dict(request_context or {}),
                    "results": results,
                }
            )
            state["last_scheduler_result"] = {
                "status": "start_new_writing_session",
                "message": f"已写入 {count} 条 new_session 命令并启动写论文并发调度器。",
                "requested_count": count,
                "single_active": single_active,
                "request_context": dict(request_context or {}),
                "results": results,
            }
            self.save(state)
            return state

    def pause_stage(self, stage: str) -> dict[str, Any]:
        if stage not in self.adapters:
            raise KeyError(f"Unknown stage: {stage}")
        router = getattr(self, "backend_router", None)
        if not isinstance(router, BackendRouter):
            return self._pause_stage_legacy(stage)
        return router.execute(
            f"stage.pause.{stage}",
            legacy=lambda: self._pause_stage_legacy(stage),
            new_backend=self._stage_backend_callback("pause_stage", stage),
        )

    def _pause_stage_legacy(self, stage: str) -> dict[str, Any]:
        with _unified_state_lock():
            result = self._pause_adapter(self.adapters[stage])
            state = self.sync(persist=False)
            state.setdefault("history", []).append({"at": now_iso(), "event": "pause_stage", "stage": stage, "result": result})
            state["last_scheduler_result"] = {"status": "pause_stage", "stage": stage, "result": result}
            self.save(state)
            return state

    def pause_all(self) -> dict[str, Any]:
        router = getattr(self, "backend_router", None)
        if not isinstance(router, BackendRouter):
            return self._pause_all_legacy()
        return router.execute(
            "stage.pause_all",
            legacy=self._pause_all_legacy,
            new_backend=self._stage_backend_callback("pause_all"),
        )

    def _pause_all_legacy(self) -> dict[str, Any]:
        with _unified_state_lock():
            results: dict[str, Any] = {}
            for stage, adapter in self.adapters.items():
                results[stage] = self._pause_adapter(adapter)
            state = self.sync(persist=False)
            state.setdefault("history", []).append({"at": now_iso(), "event": "pause_all", "results": results})
            state["last_scheduler_result"] = {"status": "pause_all", "results": results}
            self.save(state)
            return state

    def scheduler_status(self) -> dict[str, Any]:
        self._reconcile_writing_completion()
        return self._scheduler_status_read_only()

    def _scheduler_status_read_only(self) -> dict[str, Any]:
        # Status inspection must not bootstrap or rewrite automation files.
        # SchedulerAutomationController.status() currently performs repairs, so
        # mirror only its read/selection portion here.
        results: dict[str, Any] = {}
        for stage in LEGACY_PROJECTS:
            paths = self.automation._candidate_paths(stage)
            configs = [parse_toml_like(path) for path in paths]
            if not configs:
                selected = parse_toml_like(self.automation._primary_path(stage))
            else:
                primary = self.automation._primary_path(stage)
                configs.sort(
                    key=lambda item: (Path(item["path"]) == primary, item.get("status") == "ACTIVE"),
                    reverse=True,
                )
                selected = configs[0]
            if stage not in {"writing", "figures", "journal_author"}:
                results[stage] = selected
                continue
            adapters = getattr(self, "adapters", {})
            adapter = adapters.get(stage) if isinstance(adapters, dict) else None
            runtime = getattr(adapter, "runtime", None)
            status_method = getattr(runtime, "status", None)
            runtime_status: dict[str, Any] = {}
            if callable(status_method):
                try:
                    runtime_status = status_method(check_account=False)
                except Exception as exc:
                    runtime_status = {
                        "ok": False,
                        "blocker": str(getattr(exc, "code", type(exc).__name__)),
                    }
            results[stage] = {
                "id": f"local-{stage}-sdk-runtime",
                "kind": "local_sdk_event_dispatcher",
                "status": "ACTIVE" if runtime_status.get("background_running") else "PAUSED",
                "runtime": runtime_status,
                "legacy_fallback": selected,
            }
        return results

    def _reconcile_writing_completion(self) -> dict[str, Any]:
        adapters = getattr(self, "adapters", {})
        adapter = adapters.get("writing") if isinstance(adapters, dict) else None
        reconcile = getattr(adapter, "reconcile_completed_threads", None)
        if not callable(reconcile):
            return {"changed": False, "updated": [], "skipped": {"*": "unsupported"}}
        return reconcile()

    def render_summary(self, state: dict[str, Any]) -> str:
        lines = [
            "# 统一总控状态",
            "",
            f"- 更新时间：`{state.get('updated_at', '')}`",
            f"- 统一队列：{', '.join(state.get('paper_queue', [])) if state.get('paper_queue') else '空'}",
            f"- 最近调度结果：{markdown_escape(state.get('last_scheduler_result', {}).get('message', '-'))}",
            "",
            "| 论文 | 当前阶段 | 下一动作 | 写论文 | 画图 | 选刊作者 | 投稿 | 卡点 |",
            "|---|---:|---|---|---|---|---|---|",
        ]
        for paper_id, record in sorted(state.get("papers", {}).items()):
            stages = record.get("stages", {})
            row = [
                paper_id,
                record.get("current_stage_label", ""),
                record.get("next_action_label", action_label(str(record.get("next_action") or ""))),
                status_token(stages.get("writing", {})),
                status_token(stages.get("figures", {})),
                status_token(stages.get("journal_author", {})),
                status_token(stages.get("submission", {})),
                record.get("blocked_reason", ""),
            ]
            lines.append("| " + " | ".join(markdown_escape(item) for item in row) + " |")

        lines.extend(
            [
                "",
                "## 调度约束",
                "",
                "- Master Scheduler 每次只推进一个队首动作。",
                "- 统一 JSON 是机器状态源；Markdown 只给人看。",
                "- 投稿阶段不处理验证码、2FA、付款、强制 OA、作者确认或最终法律伦理声明。",
            ]
        )
        return "\n".join(lines)

    def _title_for(self, paper_id: str, states: dict[str, dict[str, Any]] | None = None) -> str:
        for stage in ("submission", "journal_author"):
            state = states.get(stage, {}) if states is not None else self.adapters[stage].load_state()
            record = state.get("papers", {}).get(paper_id, {})
            if isinstance(record, dict):
                title = str(record.get("title") or record.get("paper_title") or "").strip()
                if title:
                    return title
        return paper_id.replace("_", " ")

    def _normalized_queue(self, raw_queue: list[Any], papers: dict[str, Any]) -> list[str]:
        queue: list[str] = []
        for raw in raw_queue:
            paper_id = str(raw or "").strip()
            record = papers.get(paper_id, {})
            if (
                paper_id
                and record
                and record.get("next_action") != "none"
                and paper_id not in queue
            ):
                queue.append(paper_id)
        return queue

    def _route_submission_action(
        self,
        route: dict[str, Any],
        current_stage: str,
        next_action: str,
        blocker: str,
    ) -> tuple[str, str]:
        if current_stage != "submission" or next_action != "queue_submission":
            return next_action, blocker
        owner = str(route.get("execution_owner") or "legacy_auto")
        if owner == "smart":
            return "wait_submission", "该论文由智能投稿执行；统一总控不写入稳定投稿队列。"
        if owner == "manual":
            return "blocked", "该论文已设为手动投稿；统一总控不自动写入任一投稿队列。"
        return next_action, blocker

    def _stage_admissions_for(
        self,
        stages: dict[str, dict[str, Any]],
        overrides: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        admissions: dict[str, dict[str, Any]] = {}
        writing_status = str(stages.get("writing", {}).get("status") or "missing")
        admissions["writing"] = {
            "admitted": writing_status != "missing",
            "mode": "native" if writing_status != "missing" else "",
            "reason": "写作记录或写作产物已被识别" if writing_status != "missing" else "尚未创建写作任务",
        }
        for index, stage_name in enumerate(STAGE_ORDER[1:], start=1):
            upstream_name = STAGE_ORDER[index - 1]
            upstream = stages.get(upstream_name, {})
            override = overrides.get(stage_name, {})
            if not isinstance(override, dict):
                override = {}
            upstream_admitted = bool(admissions.get(upstream_name, {}).get("admitted"))
            upstream_complete = upstream_admitted and is_complete(upstream)
            manual = bool(override.get("admitted"))
            admitted = upstream_complete or manual
            if upstream_complete:
                mode = "automatic"
                reason = f"程序已验证{STAGE_LABELS[upstream_name]}阶段完成"
            elif manual:
                mode = "manual_override"
                reason = str(override.get("reason") or "用户人工越级放行")
            else:
                mode = ""
                reason = (
                    f"等待{STAGE_LABELS[upstream_name]}阶段完成"
                    if upstream_admitted
                    else f"等待{STAGE_LABELS[upstream_name]}阶段先通过流程准入"
                )
            admissions[stage_name] = {
                "admitted": admitted,
                "mode": mode,
                "reason": reason,
                "at": str(override.get("at") or "") if manual else "",
                "upstream_stage": upstream_name,
                "upstream_admitted": upstream_admitted,
                "upstream_status": str(upstream.get("status") or "missing"),
                "upstream_artifact": str(upstream.get("artifact") or ""),
            }
        return admissions

    @staticmethod
    def _effective_current_stage(
        stages: dict[str, dict[str, Any]],
        admissions: dict[str, dict[str, Any]],
    ) -> str:
        forced_indices = [
            index
            for index, stage_name in enumerate(STAGE_ORDER)
            if str(admissions.get(stage_name, {}).get("mode") or "") == "manual_override"
        ]
        if not forced_indices:
            return current_stage_for(stages)
        for stage_name in STAGE_ORDER[max(forced_indices) :]:
            if not is_complete(stages.get(stage_name, {})):
                return stage_name
        return "done"

    def _infer_upstream_handoffs(self, stages: dict[str, dict[str, Any]]) -> None:
        seen_downstream = False
        for stage_name in reversed(STAGE_ORDER):
            stage = stages.get(stage_name, {})
            status = str(stage.get("status") or "missing")
            has_artifact = bool(stage.get("artifact"))
            has_state = status not in {"missing", "unknown"} or has_artifact
            if seen_downstream and status == "missing":
                stage["detail"] = "检测到下游历史文件，但缺少本阶段严格完成证据；未自动越级"
                stage["raw_status"] = "downstream_without_upstream_completion"
            if has_state:
                seen_downstream = True

    def _first_queued_actionable(self, state: dict[str, Any]) -> str:
        for paper_id in state.get("paper_queue", []):
            record = state.get("papers", {}).get(paper_id, {})
            # Scan past blocked and waiting records.  They remain visible in the
            # queue, but only deterministic queue_* actions may be dispatched.
            if record and str(record.get("next_action") or "").startswith("queue_"):
                return paper_id
        return ""

    def _plan_for(self, paper_id: str, record: dict[str, Any]) -> dict[str, Any]:
        # Preserve compatibility with older tests and integrations that create
        # the object via ``__new__`` and inject adapters without calling init.
        router = getattr(self, "backend_router", None)
        if not isinstance(router, BackendRouter):
            return self._plan_for_legacy(paper_id, record)
        snapshot = {
            "paper_id": paper_id,
            "record": record,
            "stage_labels": STAGE_LABELS,
            "action_label": action_label(str(record.get("next_action") or "none")),
        }
        return router.execute(
            "_plan_for",
            legacy=lambda: self._plan_for_legacy(paper_id, record),
            shadow_preview=V2_PLAN_PREVIEW,
            new_backend=lambda: plan_for_snapshot(snapshot),
            snapshot=snapshot,
        )

    def _plan_for_legacy(self, paper_id: str, record: dict[str, Any]) -> dict[str, Any]:
        action = str(record.get("next_action") or "none")
        stage = str(record.get("current_stage") or "")
        plan: dict[str, Any] = {
            "paper_id": paper_id,
            "stage": stage,
            "stage_label": STAGE_LABELS.get(stage, stage),
            "action": action,
            "action_label": action_label(action),
            "writes": [],
        }
        if action == "queue_writing":
            plan["writes"] = ["writing.pipeline_state.json: commands += resume_session; sessions[paper_id].status=queued"]
        elif action == "queue_figures":
            plan["writes"] = [
                "figures/papers/<paper_id>/input.tex <- writing draft_v6.tex",
                "figure_pipeline_state.json: paper_queue += paper_id; commands += run_pipeline_late",
            ]
        elif action == "queue_journal_author":
            plan["writes"] = [
                "journal/papers/<paper_id>/source_submit <- figure submit/",
                "journal_author_state.json: paper_queue += paper_id; commands += run_pipeline_02_03",
            ]
        elif action == "queue_submission":
            route = record.get("submission_route", {}) if isinstance(record.get("submission_route"), dict) else {}
            owner = str(route.get("execution_owner") or "legacy_auto")
            plan["execution_owner"] = owner
            if owner == "legacy_auto":
                plan["writes"] = [
                    "auto_submission_panel_state.json: papers[paper_id].source_package_dir=journal package/",
                    "auto_submission_panel_state.json: paper_queue += paper_id; settings.scheduling_mode=run_once",
                ]
            else:
                plan["writes"] = [f"none; submission execution owner is {owner}"]
        elif action.startswith("wait_"):
            plan["writes"] = ["none; waiting for upstream/current stage"]
        else:
            plan["writes"] = ["none"]
        return plan
