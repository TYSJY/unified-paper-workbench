from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..config import (
    LEGACY_PROJECTS,
    WRITING_DEFAULT_PARALLEL_WORKERS,
    WRITING_DISPATCH_LOCK_PATH,
    WRITING_GATE_POLICY_PATH,
    WRITING_MAX_PARALLEL_WORKERS,
    WRITING_MAX_WORKFLOW_STEPS,
    WRITING_SDK_DISPATCH_GENERATION_KEY,
)
from ..io_utils import FileLock, command_id, now_iso
from ..models import stage_status
from ..writing_completion import CodexThreadCompletionReader, expected_step_artifacts, fingerprint_step_artifacts
from ..writing_evidence_gate import validate_writing_evidence, writing_evidence_blocker
from ..undergraduate_verification import uses_undergraduate_verification, undergraduate_evidence_gate
from ..writing_gate_policy import WritingGatePolicyError, WritingGatePolicyStore
from ..writing_novelty_gate import validate_writing_step2_contract, writing_novelty_blocker
from ..writing_workflow import (
    WritingWorkflowStore,
    default_workflow_snapshot,
    normalize_workflow_snapshot,
    workflow_contract_step,
    workflow_step_count,
)
from ..writing_prompt_schemes import is_undergraduate, normalize_thesis_brief, scheme_id
from ..undergraduate_output_contract import validate_undergraduate_output
from .base import LegacyAdapter


_RECONCILIATION_MAX_ATTEMPTS = 3
_SDK_DISPATCH_MODE = "sdk_event_driven"
_SDK_DISPATCH_PROTOCOL = "sdk_events_v1"
_DAILY_PIPELINE_PREFIX_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<sequence>\d{3})(?:_|$)")


class WritingSDKRuntime(Protocol):
    """Narrow injectable boundary used by the legacy-state adapter."""

    def ensure_ready(self) -> dict[str, Any]: ...

    def start_background(
        self,
        *,
        priority_paper_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]: ...

    def request_pause(self) -> dict[str, Any]: ...

    def assert_revalidation_transaction_resolved(self) -> None: ...

    def recover_revalidation_transaction(self) -> dict[str, Any]: ...

    def prepare_pipeline_mutation(self) -> dict[str, Any]: ...


def _default_sdk_runtime() -> WritingSDKRuntime:
    # Import lazily so read-only projections and packaging checks do not start
    # or even construct the local Codex SDK runtime.
    module = import_module("unified_paper_orchestrator.writing_sdk_runtime")
    return module.get_writing_sdk_runtime()  # type: ignore[no-any-return]


def _is_superseded_novelty_gate_evidence(evidence: str) -> bool:
    normalized = " ".join(evidence.casefold().split())
    return (
        "novelty" in normalized
        and "gate" in normalized
        and any(marker in normalized for marker in ("required", "blocked", "not ready"))
    )


class WritingAdapter(LegacyAdapter):
    stage_name = "writing"
    id_key = "pipeline_id"

    def __init__(
        self,
        completion_reader: CodexThreadCompletionReader | None = None,
        runtime: WritingSDKRuntime | None = None,
        workflow_store: WritingWorkflowStore | None = None,
    ) -> None:
        super().__init__(LEGACY_PROJECTS["writing"])
        self.completion_reader = completion_reader or CodexThreadCompletionReader()
        self._runtime = runtime
        self.workflow_store = workflow_store or WritingWorkflowStore()
        self._pipeline_writer_state = threading.local()

    @staticmethod
    def _session_workflow(session: dict[str, Any]) -> dict[str, Any]:
        return normalize_workflow_snapshot(session.get("workflow_snapshot") or default_workflow_snapshot())

    @property
    def runtime(self) -> WritingSDKRuntime:
        if self._runtime is None:
            self._runtime = _default_sdk_runtime()
        return self._runtime

    def _assert_revalidation_resolved(self) -> None:
        guard = getattr(self.runtime, "assert_revalidation_transaction_resolved", None)
        if not callable(guard):
            raise RuntimeError("writing SDK runtime does not expose the revalidation transaction guard")
        guard()

    def _prepare_pipeline_mutation(self) -> dict[str, Any]:
        if int(getattr(self._pipeline_writer_state, "depth", 0)):
            self._assert_revalidation_resolved()
            return {"recovered": False, "status": "already_locked"}
        prepare = getattr(self.runtime, "prepare_pipeline_mutation", None)
        if not callable(prepare):
            raise RuntimeError("writing SDK runtime does not expose safe pipeline mutation preparation")
        result = prepare()
        return dict(result) if isinstance(result, dict) else {"recovered": bool(result)}

    @contextmanager
    def _pipeline_write_scope(self) -> Iterator[None]:
        """Serialize every writing pipeline write and close marker races.

        The outer read-only check prevents side effects when a marker is
        already pending.  The second check runs under the pipeline lock, so a
        revalidation cannot publish a marker between the gate and the write.
        Nested adapter saves reuse only this adapter-owned lock scope; they do
        not recursively acquire ``FileLock``.
        """

        self._assert_revalidation_resolved()
        depth = int(getattr(self._pipeline_writer_state, "depth", 0))
        if depth:
            self._assert_revalidation_resolved()
            self._pipeline_writer_state.depth = depth + 1
            try:
                yield
            finally:
                self._pipeline_writer_state.depth = depth
            return
        with FileLock(WRITING_DISPATCH_LOCK_PATH):
            self._pipeline_writer_state.depth = 1
            try:
                self._assert_revalidation_resolved()
                yield
            finally:
                self._pipeline_writer_state.depth = 0

    @contextmanager
    def pipeline_mutation_scope(self) -> Iterator[None]:
        """Prepare and hold the adapter-owned lock across a read/modify/write."""

        self._prepare_pipeline_mutation()
        with self._pipeline_write_scope():
            yield

    def save_state(self, state: dict[str, Any]) -> None:
        with self._pipeline_write_scope():
            super().save_state(state)

    def save_state_if_unchanged(self, state: dict[str, Any], expected_sha256: str | None) -> bool:
        with self._pipeline_write_scope():
            return super().save_state_if_unchanged(state, expected_sha256)

    def _writing_scheduler_guard(self) -> dict[str, Any]:
        """Read all primary/legacy writing automation states without repairing them."""

        try:
            configs = self.automation.read_statuses(self.stage_name)
        except Exception as exc:
            return {
                "safe": False,
                "status": "scheduler_status_unreadable_no_write",
                "checked": [],
                "active": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        checked = [
            {
                "id": str(config.get("id") or ""),
                "path": str(config.get("path") or ""),
                "status": str(config.get("status") or "MISSING").strip().upper(),
                "exists": bool(config.get("exists", True)),
            }
            for config in configs
            if isinstance(config, dict)
        ]
        existing = [config for config in checked if config["exists"]]
        active = [config for config in existing if config["status"] == "ACTIVE"]
        unknown = [config for config in existing if config["status"] not in {"ACTIVE", "PAUSED"}]
        missing = [config for config in checked if not config["exists"]]
        if unknown:
            status = "scheduler_status_unreadable_no_write"
        elif active:
            status = "scheduler_active_no_write"
        else:
            # No legacy Automation file is equivalent to a permanently paused
            # compatibility fallback.  The SDK runtime must not depend on a
            # V1 TOML bridge being installed.
            status = "paused"
        return {
            "safe": status == "paused",
            "status": status,
            "legacy_fallback": "PAUSED" if not existing else "installed_paused",
            "checked": checked,
            "missing": missing,
            "active": active,
            "unknown": unknown,
        }

    def _guarded_no_write_result(
        self,
        guard: dict[str, Any],
        skipped: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        reason = str(guard.get("status") or "scheduler_status_unreadable_no_write")
        reasons = dict(skipped or {})
        reasons["*"] = reason
        return {
            "status": reason,
            "changed": False,
            "updated": [],
            "skipped": reasons,
            "automation": guard,
            "attempts": 0,
        }

    def _require_paused_before_mutation(self, action: str) -> None:
        guard = self._writing_scheduler_guard()
        if guard.get("safe"):
            return
        active = ", ".join(
            str(item.get("id") or item.get("path") or "unknown")
            for item in guard.get("active", [])
            if isinstance(item, dict)
        )
        detail = f" ({active})" if active else ""
        raise RuntimeError(
            f"{guard.get('status')}: writing {action} requires every primary/legacy "
            f"writing automation to be PAUSED before state mutation{detail}"
        )

    def _reconcile_before_mutation(self, action: str) -> dict[str, Any]:
        self._require_paused_before_mutation(action)
        reconciliation = self.reconcile_completed_threads()
        if reconciliation.get("status") not in {"reconciled", "no_change"}:
            raise RuntimeError(f"{reconciliation.get('status')}: writing {action} stopped before state mutation")
        # Rollout inspection can take long enough for an external actor to
        # change automation state.  Re-check before the queue/start write.
        self._require_paused_before_mutation(action)
        return reconciliation

    def _pause_legacy_automation(self, action: str) -> dict[str, Any]:
        """Best-effort V1 compatibility control, followed by fail-closed proof."""

        result = self.automation.set_enabled(self.stage_name, False, self.load_state())
        guard = self._writing_scheduler_guard()
        if not guard.get("safe"):
            active = ", ".join(
                str(item.get("id") or item.get("path") or "unknown")
                for item in guard.get("active", [])
                if isinstance(item, dict)
            )
            detail = f" ({active})" if active else ""
            raise RuntimeError(
                f"{guard.get('status')}: writing {action} could not prove every legacy Automation is PAUSED{detail}"
            )
        normalized = dict(result) if isinstance(result, dict) else {"result": result}
        normalized.update(
            {
                "stage": self.stage_name,
                "enabled": False,
                "status": "PAUSED",
                "guard": guard,
            }
        )
        return normalized

    def _require_runtime_ready(self) -> dict[str, Any]:
        checker = getattr(self.runtime, "ensure_ready", None)
        if not callable(checker):
            checker = getattr(self.runtime, "require_ready", None)
        if not callable(checker):
            raise RuntimeError("writing SDK runtime does not expose an account preflight")
        result = checker()
        if isinstance(result, dict) and (
            result.get("ok") is False or result.get("authenticated") is False or result.get("ready") is False
        ):
            raise RuntimeError("writing SDK account is not ready")
        return dict(result) if isinstance(result, dict) else {"ready": True}

    def _start_sdk_background(
        self,
        *,
        priority_paper_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        starter = getattr(self.runtime, "start_background", None)
        if not callable(starter):
            raise RuntimeError("writing SDK runtime does not expose start_background")
        result = (
            starter()
            if priority_paper_keys is None
            else starter(priority_paper_keys=priority_paper_keys)
        )
        normalized = dict(result) if isinstance(result, dict) else {"started": bool(result)}
        if normalized.get("ok") is False or not (
            normalized.get("started") is True or normalized.get("already_running") is True
        ):
            raise RuntimeError("writing SDK runtime did not acknowledge the background wakeup")
        return normalized

    def _request_sdk_pause(self) -> dict[str, Any]:
        requester = getattr(self.runtime, "request_pause", None)
        if not callable(requester):
            requester = getattr(self.runtime, "pause", None)
        if not callable(requester):
            raise RuntimeError("writing SDK runtime does not expose request_pause/pause")
        result = requester()
        normalized = dict(result) if isinstance(result, dict) else {"pause_requested": bool(result)}
        if normalized.get("ok") is False or normalized.get("pause_requested") is not True:
            raise RuntimeError("writing SDK runtime did not acknowledge the pause request")
        return normalized

    def reconcile_completed_threads(self) -> dict[str, Any]:
        """Merge verified terminal thread progress into the writing state.

        ``last_dispatched`` fields are intentionally ignored.  A step advances
        only when the stored thread has a terminal ``task_complete`` event with
        matching final markers and every expected output can be fingerprinted.
        """

        self._prepare_pipeline_mutation()
        guard = self._writing_scheduler_guard()
        if not guard.get("safe"):
            return self._guarded_no_write_result(guard)

        initial = self.load_state()
        sessions = initial.get("sessions", {})
        if not isinstance(sessions, dict):
            return {
                "status": "no_change",
                "changed": False,
                "updated": [],
                "skipped": {"*": "sessions_not_object"},
                "attempts": 0,
            }

        decisions: dict[str, dict[str, Any]] = {}
        skipped: dict[str, str] = {}
        for raw_paper_id, raw_session in sessions.items():
            paper_id = str(raw_paper_id or "").strip()
            if not isinstance(raw_session, dict):
                skipped[paper_id] = "session_not_object"
                continue
            current_step = _step_number(raw_session.get("step_done"))
            workflow = self._session_workflow(raw_session)
            target_step = workflow_step_count(workflow)
            if current_step >= target_step and str(raw_session.get("status") or "") == "complete":
                continue
            thread_id = str(raw_session.get("thread_id") or "").strip()
            if not thread_id:
                continue
            completion = self.completion_reader.inspect(thread_id)
            if completion.get("status") != "terminal":
                skipped[paper_id] = str(completion.get("status") or "thread_not_terminal")
                continue
            if str(completion.get("pipeline_id") or "") != paper_id:
                skipped[paper_id] = "pipeline_marker_mismatch"
                continue
            marker_step = _step_number(completion.get("step_done"))
            if marker_step < current_step:
                skipped[paper_id] = "terminal_marker_older_than_state"
                continue
            if marker_step > target_step:
                skipped[paper_id] = "terminal_marker_outside_workflow_snapshot"
                continue
            contract_step = workflow_contract_step(workflow, marker_step)
            fingerprints = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
            if fingerprints is None:
                skipped[paper_id] = "expected_artifact_missing_or_unfingerprintable"
                continue
            if marker_step == current_step:
                previous = raw_session.get("completion_reconciliation")
                if isinstance(previous, dict) and previous:
                    if str(previous.get("source") or "") != "codex_task_complete_marker_and_artifact_sha256":
                        skipped[paper_id] = "same_step_owned_by_another_reconciler"
                        continue
                    if str(previous.get("turn_id") or "") != str(completion.get("turn_id") or ""):
                        skipped[paper_id] = "same_step_turn_provenance_conflict"
                        continue
                    if previous.get("artifacts") != fingerprints:
                        skipped[paper_id] = "same_step_artifact_provenance_conflict"
                        continue
            decisions[paper_id] = {
                "thread_id": thread_id,
                "step_done": marker_step,
                "turn_id": str(completion.get("turn_id") or ""),
                "completed_at": completion.get("completed_at"),
                "thread_event_at": str(completion.get("timestamp") or ""),
                "rollout_file": str(completion.get("rollout_file") or ""),
                "artifacts": fingerprints,
            }

        if not decisions:
            return {
                "status": "no_change",
                "changed": False,
                "updated": [],
                "skipped": skipped,
                "attempts": 0,
            }

        # Each retry re-reads the exact source bytes and merges only the
        # decision's target sessions.  Settings, commands, other sessions, and
        # unknown scheduler-owned fields therefore come from the newest read.
        for attempt in range(1, _RECONCILIATION_MAX_ATTEMPTS + 1):
            guard = self._writing_scheduler_guard()
            if not guard.get("safe"):
                result = self._guarded_no_write_result(guard, skipped)
                result["attempts"] = attempt - 1
                return result

            state, source_sha256 = self.load_state_snapshot()
            current_sessions = state.get("sessions", {})
            if not isinstance(current_sessions, dict):
                return {
                    "status": "no_change",
                    "changed": False,
                    "updated": [],
                    "skipped": {"*": "sessions_not_object"},
                    "attempts": attempt,
                }
            reconciled_at = now_iso()
            attempt_skipped = dict(skipped)
            updated: list[dict[str, Any]] = []
            for paper_id, decision in decisions.items():
                session = current_sessions.get(paper_id)
                if not isinstance(session, dict):
                    attempt_skipped[paper_id] = "session_disappeared"
                    continue
                if str(session.get("thread_id") or "").strip() != decision["thread_id"]:
                    attempt_skipped[paper_id] = "thread_owner_changed"
                    continue
                current_step = _step_number(session.get("step_done"))
                marker_step = int(decision["step_done"])
                if current_step > marker_step:
                    attempt_skipped[paper_id] = "state_advanced_during_reconciliation"
                    continue

                desired_status = _status_after_terminal_step(session, marker_step)
                existing_evidence = str(session.get("evidence") or "")
                if desired_status in {"paused", "stopped"} and existing_evidence:
                    evidence = existing_evidence
                else:
                    evidence = (
                        f"Thread task_complete verified: PIPELINE_ID={paper_id}; STEP_DONE={marker_step}; "
                        "expected artifacts fingerprinted."
                    )
                signature = {
                    "source": "codex_task_complete_marker_and_artifact_sha256",
                    **decision,
                }
                previous = session.get("completion_reconciliation", {})
                previous_signature = (
                    {key: value for key, value in previous.items() if key != "reconciled_at"}
                    if isinstance(previous, dict)
                    else {}
                )
                metadata = {
                    **signature,
                    "reconciled_at": (
                        str(previous.get("reconciled_at") or reconciled_at)
                        if previous_signature == signature and isinstance(previous, dict)
                        else reconciled_at
                    ),
                }
                needs_update = any(
                    (
                        current_step != marker_step,
                        str(session.get("status") or "") != desired_status,
                        str(session.get("evidence") or "") != evidence,
                        previous_signature != signature,
                    )
                )
                if not needs_update:
                    continue

                session["step_done"] = marker_step
                session["status"] = desired_status
                session["evidence"] = evidence
                session["completion_reconciliation"] = metadata
                session["updated_at"] = reconciled_at
                if desired_status == "complete":
                    session.pop("run_until_step", None)
                updated.append(
                    {
                        "paper_id": paper_id,
                        "thread_id": decision["thread_id"],
                        "step_done": marker_step,
                        "status": desired_status,
                    }
                )

            if not updated:
                return {
                    "status": "no_change",
                    "changed": False,
                    "updated": [],
                    "skipped": attempt_skipped,
                    "attempts": attempt,
                }

            # Check the automation guard again immediately before the
            # byte-conditional replace.  Paused automation is the primary
            # safety boundary; the digest detects state changes during merge.
            guard = self._writing_scheduler_guard()
            if not guard.get("safe"):
                result = self._guarded_no_write_result(guard, attempt_skipped)
                result["attempts"] = attempt
                return result
            if self.save_state_if_unchanged(state, source_sha256):
                return {
                    "status": "reconciled",
                    "changed": True,
                    "updated": updated,
                    "skipped": attempt_skipped,
                    "attempts": attempt,
                }

        exhausted = dict(skipped)
        exhausted["*"] = "state_changed_retry_exhausted"
        return {
            "status": "state_changed_retry_exhausted",
            "changed": False,
            "updated": [],
            "skipped": exhausted,
            "attempts": _RECONCILIATION_MAX_ATTEMPTS,
        }

    def all_paper_ids(self) -> set[str]:
        state = self.load_state()
        ids = set(str(item) for item in state.get("sessions", {}).keys())
        ideas_dir = self.root / "ideas"
        if ideas_dir.exists():
            ids.update(path.stem for path in ideas_dir.glob("*.md") if path.is_file())
        if self.papers_dir.exists():
            ids.update(child.name for child in self.papers_dir.iterdir() if child.is_dir())
        return ids

    def _reserved_pipeline_prefixes(self, state: dict[str, Any], date: str) -> set[str]:
        """Return every program-owned daily number already present or reserved."""

        prefixes: set[str] = set()

        def collect(value: Any) -> None:
            match = _DAILY_PIPELINE_PREFIX_RE.match(str(value or "").strip())
            if match is not None and match.group("date") == date:
                prefixes.add(f"{date}_{match.group('sequence')}")

        sessions = state.get("sessions", {})
        if isinstance(sessions, dict):
            for raw_id, raw_session in sessions.items():
                collect(raw_id)
                if isinstance(raw_session, dict):
                    collect(raw_session.get("pipeline_id"))
                    collect(raw_session.get("reserved_pipeline_prefix"))
        commands = state.get("commands", [])
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, dict):
                    continue
                collect(command.get("pipeline_id"))
                collect(command.get("reserved_pipeline_prefix"))
                params = command.get("params")
                if isinstance(params, dict):
                    collect(params.get("reserved_pipeline_prefix"))
        ideas_dir = self.root / "ideas"
        if ideas_dir.is_dir():
            for path in ideas_dir.glob(f"{date}_*.md"):
                if path.is_file():
                    collect(path.stem)
        if self.papers_dir.is_dir():
            for child in self.papers_dir.glob(f"{date}_*"):
                if child.is_dir():
                    collect(child.name)
        return prefixes

    def _allocate_pipeline_prefixes(
        self,
        state: dict[str, Any],
        count: int,
        *,
        date: str | None = None,
    ) -> list[str]:
        """Atomically reserve unique daily sequence numbers under the pipeline lock."""

        allocation_date = str(date or now_iso()[:10])
        used = self._reserved_pipeline_prefixes(state, allocation_date)
        allocated: list[str] = []
        for sequence in range(1, 1000):
            candidate = f"{allocation_date}_{sequence:03d}"
            if candidate in used:
                continue
            used.add(candidate)
            allocated.append(candidate)
            if len(allocated) == count:
                return allocated
        raise RuntimeError(f"WRITING_DAILY_PIPELINE_SEQUENCE_EXHAUSTED:{allocation_date}")

    def idea_path(self, paper_id: str) -> Path:
        return self.root / "ideas" / f"{paper_id}.md"

    def draft_path(self, paper_id: str, version: int = 6) -> Path:
        return self.papers_dir / paper_id / f"draft_v{version}.tex"

    def best_artifact(self, paper_id: str) -> Path | None:
        for version in range(6, -1, -1):
            candidate = self.draft_path(paper_id, version)
            if candidate.exists():
                return candidate
        return None

    def stage_for(self, paper_id: str) -> dict[str, Any]:
        state = self.load_state()
        session = state.get("sessions", {}).get(paper_id, {})
        if not isinstance(session, dict):
            session = {}
        workflow = self._session_workflow(session)
        target_step = workflow_step_count(workflow)
        terminal_contract = workflow_contract_step(workflow, target_step)
        terminal_paths = expected_step_artifacts(self.root, self.papers_dir, paper_id, terminal_contract)
        terminal_artifact = terminal_paths[0] if len(terminal_paths) == 1 and terminal_paths[0].is_file() else None
        artifact = terminal_artifact or self.best_artifact(paper_id)
        raw_status = str(session.get("status") or "")
        step_done = int(session.get("step_done") or 0)
        if raw_status == "complete" and step_done >= target_step and terminal_artifact is not None:
            status = "complete"
        elif raw_status in {"queued", "running", "paused", "stopped"}:
            status = raw_status
        elif artifact:
            status = "incomplete"
        elif self.idea_path(paper_id).is_file():
            status = "incomplete"
            detail = (
                "Step 1 idea exists but the writing session is not registered; continue writing to adopt and resume it."
            )
        elif session:
            status = "unknown"
        else:
            status = "missing"
        if not self.idea_path(paper_id).is_file() or artifact or session:
            detail = str(session.get("evidence") or "")
        if step_done and status != "complete":
            detail = f"step_done={step_done}; {detail}".strip("; ")
        evidence_gate: dict[str, Any] | None = None
        novelty_gate: dict[str, Any] | None = None
        draft_v0 = self.draft_path(paper_id, 0)
        if status != "complete" and step_done >= 2 and not draft_v0.is_file():
            novelty_gate = (validate_undergraduate_output(self.root, self.papers_dir, paper_id, 2, workflow)
                            if is_undergraduate(workflow) else validate_writing_step2_contract(self.root, paper_id))
            if not novelty_gate["ready"]:
                status = "blocked"
                detail = ("; ".join(item["message"] for item in novelty_gate["issues"])
                          if is_undergraduate(workflow) else writing_novelty_blocker(novelty_gate))
            elif _is_superseded_novelty_gate_evidence(str(session.get("evidence") or "")):
                detail = f"step_done={step_done}; Step 2->3 novelty gate READY"
        draft_v2 = self.draft_path(paper_id, 2)
        draft_v3 = self.draft_path(paper_id, 3)
        # The gate controls only the live Step 5 -> Step 6 transition.  A
        # completed historical paper is a read-only projection and must not be
        # retroactively downgraded solely because its old v1 manifest predates
        # the stricter v2 contract.
        verification = uses_undergraduate_verification(workflow)
        if status != "complete" and draft_v2.is_file() and ((not draft_v3.is_file() and not is_undergraduate(workflow)) or (verification and step_done == 5)):
            evidence_gate = (undergraduate_evidence_gate(self.papers_dir, paper_id) if verification
                             else validate_writing_evidence(self.papers_dir, paper_id))
            if evidence_gate.get("gate_profile") == "evidence_auto_trust_transition":
                try:
                    active_policy = WritingGatePolicyStore(WRITING_GATE_POLICY_PATH).auto_trust_snapshot()
                except WritingGatePolicyError as exc:
                    active_policy = None
                    evidence_gate.setdefault("issues", []).append(
                        {"code": exc.code, "path": "writing_gate_policy.json", "message": str(exc)}
                    )
                if active_policy is None or evidence_gate.get("policy_snapshot") != active_policy:
                    evidence_gate.update({"status": "BLOCKED", "ready": False, "transition_ready": False})
                    evidence_gate.pop("input_snapshot", None)
                    if not any(
                        str(item.get("code") or "").startswith("WRITING_GATE_POLICY")
                        for item in evidence_gate.get("issues", [])
                        if isinstance(item, dict)
                    ):
                        evidence_gate.setdefault("issues", []).append(
                            {
                                "code": "WRITING_AUTO_TRUST_POLICY_INACTIVE",
                                "path": "writing_gate_policy.json",
                                "message": "current workspace policy no longer authorizes this auto-trust attestation",
                            }
                        )
            if not evidence_gate["ready"]:
                status = "waiting_data" if verification else "blocked"
                detail = writing_evidence_blocker(evidence_gate)

        projection = stage_status(
            status,
            label=self.label,
            detail=detail,
            artifact=str(artifact or ""),
            updated_at=str(session.get("updated_at") or ""),
            raw_status=raw_status or status,
            scheduler_mode=self.scheduler_mode(state),
            step_done=step_done,
        )
        projection.update(
            {
                "workflow_version_id": str(workflow.get("version_id") or ""),
                "workflow_step_count": target_step,
                "terminal_contract_step": terminal_contract,
                "prompt_scheme": workflow.get("prompt_scheme", "journal"),
            }
        )
        if evidence_gate is not None:
            projection["evidence_gate"] = evidence_gate
        if novelty_gate is not None:
            projection["novelty_gate"] = novelty_gate
        reconciliation = session.get("completion_reconciliation")
        if isinstance(reconciliation, dict):
            projection["completion_reconciliation"] = reconciliation
        return projection

    def queue(self, paper_id: str, upstream_artifact: str = "") -> dict[str, Any]:
        batch = self.queue_many([paper_id])
        return {
            "stage": self.stage_name,
            "action": "resume_session",
            "command": batch["commands"][0],
            "account": batch["account"],
            "completion_reconciliation": batch["completion_reconciliation"],
            "runtime": batch["runtime"],
            "automation": batch["automation"],
        }

    def queue_many(self, paper_ids: list[str]) -> dict[str, Any]:
        self._prepare_pipeline_mutation()
        with self._pipeline_write_scope():
            result = self._queue_many_locked(paper_ids)
        priority_paper_keys = [
            str(value).strip()
            for value in result.get("paper_keys", [])
            if str(value).strip()
        ]
        result["priority_paper_keys"] = priority_paper_keys
        result["runtime"] = self._start_sdk_background(priority_paper_keys=priority_paper_keys)
        return result

    def _queue_many_locked(self, paper_ids: list[str]) -> dict[str, Any]:
        """Atomically queue an exact selected batch, then wake the SDK runtime.

        Queueing is all-or-none.  The resulting paper keys are passed back to
        the runtime so “continue selected” cannot silently resume unrelated
        backlog papers.
        """

        ids: list[str] = []
        for raw in paper_ids:
            paper_id = str(raw or "").strip()
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
        if not ids:
            raise ValueError("at least one writing paper id is required")
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation("queue")
        reconciliation = self._reconcile_before_mutation("queue")
        state = self.load_state()
        settings = self._configure_sdk_settings(state)
        settings["scheduling_mode"] = "auto"
        sessions = state.setdefault("sessions", {})
        commands: list[dict[str, Any]] = []
        paper_keys: list[str] = []
        for paper_id in ids:
            session = sessions.setdefault(paper_id, {})
            workflow = self._session_workflow(session)
            target_step = workflow_step_count(workflow)
            paper_key = str(session.get("sdk_paper_key") or f"session:{paper_id}").strip()
            session["pipeline_id"] = paper_id
            session["sdk_paper_key"] = paper_key
            session["status"] = "queued"
            session["workflow_snapshot"] = workflow
            session["workflow_version_id"] = str(workflow.get("version_id") or "")
            session["run_until_step"] = target_step
            session["updated_at"] = now_iso()
            command = self.append_command(
                state,
                "resume_session",
                paper_id,
                {"run_until_step": target_step, "workflow_snapshot": workflow},
                id_key="pipeline_id",
            )
            command["sdk_paper_key"] = paper_key
            commands.append(command)
            paper_keys.append(paper_key)
        state["active_pipeline_id"] = ids[0]
        self._request_sdk_dispatch(settings)
        self.save_state(state)
        return {
            "stage": self.stage_name,
            "action": "resume_sessions",
            "paper_ids": ids,
            "paper_keys": paper_keys,
            "commands": commands,
            "account": account,
            "completion_reconciliation": reconciliation,
            "automation": legacy_pause,
        }

    def start(self) -> dict[str, Any]:
        self._prepare_pipeline_mutation()
        with self._pipeline_write_scope():
            account = self._require_runtime_ready()
            legacy_pause = self._pause_legacy_automation("start")
            reconciliation = self._reconcile_before_mutation("start")
            state = self.load_state()
            settings = self._configure_sdk_settings(state)
            settings["scheduling_mode"] = "auto"
            self._request_sdk_dispatch(settings)
            self.save_state(state)
        runtime = self._start_sdk_background()
        return {
            "stage": self.stage_name,
            "enabled": True,
            "account": account,
            "runtime": runtime,
            "automation": legacy_pause,
            "completion_reconciliation": reconciliation,
        }

    def resume(self) -> dict[str, Any]:
        result = self.start()
        result["action"] = "resume"
        return result

    def pause(self) -> dict[str, Any]:
        self._prepare_pipeline_mutation()
        with self._pipeline_write_scope():
            state = self.load_state()
            settings = self._configure_sdk_settings(state)
            settings["scheduling_mode"] = "manual"
            self.save_state(state)
            legacy_error: Exception | None = None
            legacy_pause: dict[str, Any] = {}
            try:
                legacy_pause = self._pause_legacy_automation("pause")
            except Exception as exc:
                legacy_error = exc
        # The real runtime uses WRITING_DISPATCH_LOCK_PATH for pipeline
        # mutation too, so request the boundary pause only after releasing the
        # adapter's lock.  FileLock is intentionally not re-entrant.
        runtime = self._request_sdk_pause()
        if legacy_error is not None:
            raise legacy_error
        return {
            "ok": True,
            "stage": self.stage_name,
            "enabled": False,
            "paused": True,
            "status": "paused",
            "runtime": runtime,
            "automation": legacy_pause,
        }

    def new_session(self) -> dict[str, Any]:
        batch = self.new_sessions(1)
        return {
            "stage": self.stage_name,
            "action": "new_session",
            "command": batch["commands"][0],
            "account": batch["account"],
            "completion_reconciliation": batch["completion_reconciliation"],
            "runtime": batch["runtime"],
            "automation": batch["automation"],
        }

    def new_sessions(
        self,
        count: int,
        *,
        request_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._prepare_pipeline_mutation()
        with self._pipeline_write_scope():
            result = self._new_sessions_locked(
                count,
                request_context=request_context,
            )
        priority_paper_keys = [
            str(command.get("sdk_paper_key") or "").strip()
            for command in result.get("commands", [])
            if isinstance(command, dict) and str(command.get("sdk_paper_key") or "").strip()
        ]
        result["priority_paper_keys"] = priority_paper_keys
        result["runtime"] = self._start_sdk_background(priority_paper_keys=priority_paper_keys)
        return result

    def _new_sessions_locked(
        self,
        count: int,
        *,
        request_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            requested = int(count)
        except (TypeError, ValueError):
            raise ValueError("new writing session count must be an integer") from None
        if not 1 <= requested <= WRITING_MAX_PARALLEL_WORKERS:
            raise ValueError(f"new writing session count must be 1-{WRITING_MAX_PARALLEL_WORKERS}")
        context = request_context if isinstance(request_context, dict) else {}
        identifier = context.get("prompt_scheme")
        if identifier and identifier not in {"undergraduate_humanities", "undergraduate_software"}:
            raise ValueError("独立毕业论文入口仅接受本科方案")
        workflow = self.workflow_store.executable_snapshot(identifier) if identifier else self.workflow_store.executable_snapshot()
        expected_version = context.get("expected_workflow_version")
        if expected_version and expected_version != workflow["version_id"]:
            raise ValueError("提示词方案已变化，请重新打开新建本科论文窗口。")
        if context.get("thesis_brief") and not is_undergraduate(workflow):
            raise ValueError("当前已切换到期刊方案，请重新选择本科方案后新建。")
        if is_undergraduate(workflow):
            if context.get("full_pipeline_batch_id"):
                raise ValueError("本科提示词方案仅用于写作，不会自动进入选刊投稿全流程。")
            if requested != 1:
                raise ValueError("本科论文请逐篇填写题目和学校要求；每次新建一篇。")
            if context.get("expected_workflow_version") != workflow["version_id"]:
                raise ValueError("提示词方案已变化，请重新打开新建本科论文窗口。")
            workflow = normalize_workflow_snapshot({**workflow, "thesis_brief":
                normalize_thesis_brief(context.get("thesis_brief"), required=True)})
            from ..undergraduate_materials import verify_attachments
            verify_attachments(workflow["thesis_brief"].get("attachments", []),
                               self.root / "materials" / "undergraduate")
            from ..undergraduate_figures import make_figure_snapshot
            from ..figure_workflow import FigureWorkflowStore
            workflow = normalize_workflow_snapshot({**workflow, "figure_workflow_snapshot": make_figure_snapshot(
                scheme_id(workflow), workflow["thesis_brief"],
                FigureWorkflowStore().active_snapshot()["default_execution_profile"])})
        # This must precede reconciliation and command construction: a failed
        # account preflight is a strict no-write outcome for pipeline state.
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation("new_sessions")
        reconciliation = self._reconcile_before_mutation("new_sessions")
        state = self.load_state()
        settings = self._configure_sdk_settings(state)
        settings["scheduling_mode"] = "auto"
        settings["auto_create_new_session"] = False
        target_step = workflow_step_count(workflow)
        batch_id = command_id("new-writing-batch")
        reserved_prefixes = self._allocate_pipeline_prefixes(state, requested)
        commands: list[dict[str, Any]] = []
        context = request_context if isinstance(request_context, dict) else {}
        coordinator_batch_id = str(context.get("full_pipeline_batch_id") or "").strip()
        try:
            coordinator_slot_offset = max(int(context.get("full_pipeline_slot_offset") or 0), 0)
        except (TypeError, ValueError):
            coordinator_slot_offset = 0
        for index in range(requested):
            # A unique request id is required even for a one-paper batch;
            # otherwise LegacyAdapter.append_command can deduplicate a second
            # button click against an older pending unnamed task.
            params: dict[str, Any] = {
                "auto_create_new_session": False,
                "request_id": f"{batch_id}-{index + 1:02d}",
                "batch_size": requested,
                "run_until_step": target_step,
                "workflow_snapshot": workflow,
                "reserved_pipeline_prefix": reserved_prefixes[index],
                "reserved_at": now_iso(),
            }
            if coordinator_batch_id:
                params.update(
                    {
                        "full_pipeline_batch_id": coordinator_batch_id,
                        "full_pipeline_slot": coordinator_slot_offset + index + 1,
                    }
                )
            command = self.append_command(
                state,
                "new_session",
                "",
                params,
                id_key="pipeline_id",
            )
            command["sdk_paper_key"] = f"new-session:{command['id']}"
            command["reserved_pipeline_prefix"] = reserved_prefixes[index]
            if coordinator_batch_id:
                command["full_pipeline_batch_id"] = coordinator_batch_id
                command["full_pipeline_slot"] = coordinator_slot_offset + index + 1
            commands.append(command)
        self._request_sdk_dispatch(settings)
        self.save_state(state)
        return {
            "stage": self.stage_name,
            "action": "new_sessions",
            "requested_count": requested,
            "workflow_version_id": str(workflow.get("version_id") or ""),
            "workflow_step_count": target_step,
            "full_pipeline_batch_id": coordinator_batch_id,
            "commands": commands,
            "account": account,
            "completion_reconciliation": reconciliation,
            "automation": legacy_pause,
        }

    @staticmethod
    def _configure_sdk_settings(state: dict[str, Any]) -> dict[str, Any]:
        settings = state.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            state["settings"] = settings
        raw_limit = settings.get("max_parallel_workers", WRITING_DEFAULT_PARALLEL_WORKERS)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = WRITING_DEFAULT_PARALLEL_WORKERS
        settings["max_parallel_workers"] = min(max(limit, 1), WRITING_MAX_PARALLEL_WORKERS)
        settings["dispatch_mode"] = _SDK_DISPATCH_MODE
        settings["writing_dispatch_protocol"] = _SDK_DISPATCH_PROTOCOL
        settings["one_action_per_run"] = False
        return settings

    @staticmethod
    def _request_sdk_dispatch(settings: dict[str, Any]) -> int:
        raw_generation = settings.get(WRITING_SDK_DISPATCH_GENERATION_KEY, 0)
        try:
            generation = int(raw_generation)
        except (TypeError, ValueError):
            generation = 0
        generation = max(generation, 0) + 1
        settings[WRITING_SDK_DISPATCH_GENERATION_KEY] = generation
        return generation


def _step_number(value: Any) -> int:
    try:
        step = int(value)
    except (TypeError, ValueError):
        return 0
    return step if 0 <= step <= WRITING_MAX_WORKFLOW_STEPS else 0


def _status_after_terminal_step(session: dict[str, Any], step_done: int) -> str:
    workflow = normalize_workflow_snapshot(session.get("workflow_snapshot") or default_workflow_snapshot())
    run_until = _step_number(session.get("run_until_step")) or workflow_step_count(workflow)
    if step_done >= run_until:
        return "complete"
    current = str(session.get("status") or "").strip().lower()
    if current in {"paused", "stopped"}:
        return current
    if current == "complete" and not run_until:
        return current
    return "queued"
