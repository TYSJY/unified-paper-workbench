from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import (
    JOURNAL_ROOT,
    LEGACY_PROJECTS,
    SMART_SUBMISSION_STATE_PATH,
    WORKSPACE_ROOT as REPO_ROOT,
)
from ..io_utils import append_history, append_unique, now_iso, read_json
from ..models import stage_status
from ..submission_routing import (
    DEFAULT_EXECUTION_OWNER,
    VALID_EXECUTION_OWNERS,
    legacy_terminal_evidence,
    route_switch_blockers,
    smart_terminal_evidence,
)
from .base import LegacyAdapter


_SMART_ACCEPTED_STATUSES = {
    "accepted",
    "accept",
    "published",
    "online_published",
}
_SMART_SUBMITTED_STATUSES = {
    "submitted",
    "under_review",
    "in_review",
    "technical_check",
    "technical_checks",
    "technical_check_in_progress",
    "editorial_review",
    "decision_pending",
    "revision_requested",
    "revision_needed",
}
_SMART_MONITORING_STATUSES = {"monitoring"}
_SMART_CLOSED_STATUSES = {"rejected", "declined", "withdrawn", "stopped", "deleted"}
_SMART_BLOCKED_STATUSES = {"blocked", "manual", "manual_required", "needs_user", "waiting_approval"}
_SMART_WAIT_STATUSES = {"queued", "running", "dispatching", "waiting", "paused"}
_SMART_READY_STATUSES = {"ready", "ready_to_submit"}
_NON_QUEUEABLE_STATUSES = {
    "accepted",
    "active",
    "blocked",
    "monitoring",
    "queued",
    "running",
    "stopped",
    "submitted",
    "waiting",
}


def _status_key(value: Any) -> str:
    """Normalize a status token without substring matching.

    In particular, ``not_submitted`` must never be treated as ``submitted``
    and ``incomplete`` must never be treated as ``complete``.
    """

    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


class SubmissionAdapter(LegacyAdapter):
    stage_name = "submission"
    id_key = "paper_id"

    def __init__(self) -> None:
        super().__init__(LEGACY_PROJECTS["submission"])

    def _load_smart_state(self) -> dict[str, Any]:
        data = read_json(SMART_SUBMISSION_STATE_PATH, {})
        if not isinstance(data, dict):
            raise ValueError(f"Smart Submission state must be a JSON object: {SMART_SUBMISSION_STATE_PATH}")
        return data

    def load_projection_states(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.load_state(), self._load_smart_state()

    @staticmethod
    def paper_ids_from_states(legacy: dict[str, Any], smart: dict[str, Any]) -> set[str]:
        ids = set(str(item) for item in legacy.get("papers", {}).keys()) if isinstance(legacy.get("papers"), dict) else set()
        if isinstance(smart.get("papers"), dict):
            ids.update(str(item) for item in smart["papers"].keys())
        return ids

    def all_paper_ids(self) -> set[str]:
        return self.paper_ids_from_states(*self.load_projection_states())

    @staticmethod
    def _queue_items(state: dict[str, Any], *keys: str) -> list[str]:
        result: list[str] = []
        for key in keys:
            values = state.get(key, [])
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict):
                    item = str(value.get("paper_id") or value.get("id") or "").strip()
                else:
                    item = str(value or "").strip()
                if item and item not in result:
                    result.append(item)
        return result

    def _legacy_stage(self, state: dict[str, Any], paper_id: str) -> dict[str, Any]:
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        record = papers.get(paper_id, {})
        if not isinstance(record, dict):
            record = {}
        queue = self._queue_items(state, "paper_queue", "queue")
        queue_position = f"{queue.index(paper_id) + 1}/{len(queue)}" if paper_id in queue else ""
        workflow_stage = _status_key(record.get("workflow_stage"))
        next_action = _status_key(record.get("next_action"))
        package_status = _status_key(record.get("package_status"))
        active = bool(record.get("active_submission")) or bool(str(record.get("manuscript_id") or "").strip())
        if active:
            status = "submitted"
        elif workflow_stage == "stopped" or next_action == "none":
            status = "stopped"
        elif paper_id in queue:
            status = "queued"
        elif workflow_stage in {"ready_to_submit", "needs_status_check"} or next_action in {"submit", "check_status"}:
            status = "ready"
        elif workflow_stage or record:
            status = "blocked" if package_status == "blocked" else "incomplete"
        else:
            status = "missing"
        detail = str(record.get("submission_status") or record.get("blocker") or record.get("next_action") or record.get("package_status") or "")
        result = stage_status(
            status,
            label=self.label,
            detail=detail,
            artifact=str(record.get("source_package_dir") or ""),
            updated_at=str(record.get("last_updated_at") or record.get("updated_at") or ""),
            raw_status=workflow_stage or status,
            queue_position=queue_position,
            scheduler_mode=self.scheduler_mode(state),
        )
        result["source"] = "legacy_submission"
        result["terminal_evidence"] = legacy_terminal_evidence(state, paper_id)
        return result

    def _smart_stage(self, state: dict[str, Any], paper_id: str) -> dict[str, Any]:
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        record = papers.get(paper_id, {})
        if not isinstance(record, dict) or not record:
            result = stage_status("missing", label=self.label, raw_status="missing")
            result["source"] = "smart_submission"
            return result

        queue = self._queue_items(state, "online_queue", "paper_queue")
        queue_position = f"{queue.index(paper_id) + 1}/{len(queue)}" if paper_id in queue else ""
        tokens = {
            token
            for token in (
                _status_key(record.get("status")),
                _status_key(record.get("workflow_status")),
                _status_key(record.get("worker_status")),
            )
            if token
        }
        evidence = smart_terminal_evidence(state, paper_id, evidence_base=REPO_ROOT)
        if evidence and tokens & _SMART_ACCEPTED_STATUSES:
            status = "accepted"
        elif evidence:
            status = "submitted"
        elif tokens & (_SMART_ACCEPTED_STATUSES | _SMART_SUBMITTED_STATUSES | _SMART_MONITORING_STATUSES):
            # A status string is not proof of a completed online side effect.
            # Keep it visible as an unverified wait state until an id, valid
            # confirmation URL, or existing local evidence file is recorded.
            status = "waiting"
        elif tokens & _SMART_CLOSED_STATUSES:
            status = "stopped"
        elif tokens & _SMART_BLOCKED_STATUSES:
            status = "blocked"
        elif paper_id in queue or tokens & _SMART_WAIT_STATUSES:
            status = "queued" if paper_id in queue else "waiting"
        elif tokens & _SMART_READY_STATUSES:
            status = "ready"
        else:
            # Unknown and negated tokens such as not_submitted are deliberately
            # non-terminal until an explicit supported state is recorded.
            status = "incomplete"

        raw_status = _status_key(record.get("status")) or _status_key(record.get("workflow_status")) or status
        detail = str(
            record.get("detail")
            or record.get("submission_status")
            or record.get("blocker")
            or record.get("next_action")
            or ""
        )
        result = stage_status(
            status,
            label=self.label,
            detail=detail,
            artifact=str(record.get("source_package_dir") or record.get("last_audit_path") or ""),
            updated_at=str(record.get("last_updated_at") or record.get("updated_at") or record.get("last_checked_at") or ""),
            raw_status=raw_status,
            queue_position=queue_position,
            scheduler_mode=str(state.get("mode") or state.get("settings", {}).get("scheduling_mode") or ""),
        )
        result["source"] = "smart_submission"
        result["terminal_evidence"] = evidence
        return result

    def stage_for(self, paper_id: str) -> dict[str, Any]:
        return self.stage_for_from_states(paper_id, *self.load_projection_states())

    def stage_for_from_states(
        self,
        paper_id: str,
        legacy_state: dict[str, Any],
        smart_state: dict[str, Any],
        execution_owner: str = DEFAULT_EXECUTION_OWNER,
    ) -> dict[str, Any]:
        if execution_owner not in VALID_EXECUTION_OWNERS:
            raise ValueError(f"invalid submission execution owner: {execution_owner}")
        legacy = self._legacy_stage(legacy_state, paper_id)
        smart = self._smart_stage(smart_state, paper_id)

        legacy_evidence = list(legacy.get("terminal_evidence") or [])
        smart_evidence = list(smart.get("terminal_evidence") or [])
        # Verified terminal evidence is global and locks both execution paths.
        # Without such evidence, only the explicitly selected owner controls
        # effective status; merely importing a Smart record never masks the
        # stable legacy path.
        if legacy_evidence or smart_evidence:
            if execution_owner == "smart" and smart_evidence:
                selected = dict(smart)
            elif legacy_evidence:
                selected = dict(legacy)
            else:
                selected = dict(smart)
            selected["terminal_locked"] = True
            selected["terminal_evidence"] = legacy_evidence + smart_evidence
        elif execution_owner == "smart":
            selected = dict(smart)
        elif execution_owner == "manual":
            selected = dict(legacy if legacy["status"] != "missing" else smart)
        else:
            selected = dict(legacy)
        selected["execution_owner"] = execution_owner
        selected["legacy_status"] = legacy["status"]
        selected["smart_status"] = smart["status"]
        return selected

    def queue(
        self,
        paper_id: str,
        upstream_artifact: str = "",
        *,
        owner: str = DEFAULT_EXECUTION_OWNER,
    ) -> dict[str, Any]:
        if owner not in VALID_EXECUTION_OWNERS:
            raise ValueError(f"invalid submission execution owner: {owner}")
        if owner != "legacy_auto":
            raise RuntimeError(
                f"Refusing to write the legacy submission queue for {paper_id}: execution_owner={owner}."
            )

        legacy_state, smart_state = self.load_projection_states()
        blockers = route_switch_blockers(
            legacy_state,
            smart_state,
            paper_id,
            smart_evidence_base=REPO_ROOT,
        )
        if blockers:
            codes = ", ".join(str(item.get("code") or "unsafe") for item in blockers)
            raise RuntimeError(f"Refusing to queue {paper_id}: cross-engine submission lock ({codes}).")

        projection = self.stage_for_from_states(paper_id, legacy_state, smart_state, owner)
        if str(projection.get("status") or "") in _NON_QUEUEABLE_STATUSES:
            source = str(projection.get("source") or "submission state")
            raise RuntimeError(
                f"Refusing to queue {paper_id}: {source} already records status={projection.get('status')} "
                f"(raw={projection.get('raw_status')})."
            )

        # Queueing writes only the selected legacy engine.  Smart state remains
        # a read-only projection and is never mirrored here.
        state = legacy_state
        package_dir = Path(upstream_artifact) if upstream_artifact else JOURNAL_ROOT / "papers" / paper_id / "package"
        main_pdf = package_dir / "main.pdf"
        papers = state.setdefault("papers", {})
        record = papers.setdefault(paper_id, {})
        record.setdefault("paper_id", paper_id)
        record.setdefault("source_paper_dir", str(JOURNAL_ROOT / "papers" / paper_id))
        record["source_package_dir"] = str(package_dir)
        if main_pdf.exists():
            record["package_status"] = "PACKAGE_READY"
            record["workflow_stage"] = "ready_to_submit"
            record["next_action"] = "submit"
            record["submission_status"] = "未投稿，统一总控已加入投稿队列"
        else:
            record["package_status"] = "BLOCKED"
            record["workflow_stage"] = "package_blocked"
            record["next_action"] = "fix_package"
            record["submission_status"] = "缺 package/main.pdf，调度器需先修投稿包"
        record.setdefault("publishing_route_preference", "non_oa")
        record.setdefault("confirmation_policy", "routine_auto_sensitive_pause")
        record["last_updated_at"] = now_iso()
        append_history(record, "unified_queued", f"统一总控加入自动投稿队列；next_action={record['next_action']}")

        queue = [str(item) for item in state.get("paper_queue", [])]
        append_unique(queue, paper_id)
        state["paper_queue"] = queue
        self.save_state(state)
        return {
            "stage": self.stage_name,
            "action": "queue_submission",
            "paper_id": paper_id,
            "scheduler_started": False,
            "message": "queued only; start the submission scheduler separately when ready",
        }

    def start(self) -> dict[str, Any]:
        state = self.load_state()
        self.automation.require_controllable(self.stage_name, state)
        settings = state.setdefault("settings", {})
        settings["scheduling_mode"] = "run_once"
        settings["scheduler_run_mode"] = "run_once"
        settings["run_now_requested_at"] = now_iso()
        settings["run_now_status"] = "requested"
        settings["last_scheduler_action_at"] = now_iso()
        settings["last_scheduler_message"] = f"统一总控请求执行一轮调度；队列 {len(state.get('paper_queue', []))} 条"
        self.save_state(state)
        return self.automation.set_enabled(self.stage_name, True, state)
