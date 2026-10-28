from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .automation import AUTOMATIONS_ROOT, parse_toml_like, writing_parallel_launcher_verified
from .config import (
    LEGACY_PROJECTS,
    WRITING_DEFAULT_PARALLEL_WORKERS,
    WRITING_DISPATCH_LOCK_PATH,
    WRITING_DISPATCH_PROTOCOL,
    WRITING_DISPATCH_STATE_PATH,
    WRITING_MAX_PARALLEL_WORKERS,
)
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .writing_completion import CodexThreadCompletionReader, expected_step_artifacts, fingerprint_step_artifacts
from .writing_evidence_gate import validate_writing_evidence, writing_evidence_blocker
from .writing_novelty_gate import validate_writing_step2_contract, writing_novelty_blocker


WRITING_DISPATCH_SCHEMA = "unified-paper-orchestrator/writing-parallel-dispatch/v1"
WRITING_TICKET_SCHEMA = "unified-paper-orchestrator/writing-dispatch-ticket/v1"
_RESERVATION_TTL = timedelta(minutes=10)
_ACTIVE_STATES = {"reserved", "send_uncertain", "dispatched", "in_progress"}
_PAPER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class WritingDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        logical = path.resolve(strict=True)
        if path.is_symlink() or logical != path.absolute().resolve():
            return None
        logical.relative_to(root.resolve())
        before = logical.stat()
        if not logical.is_file() or before.st_size <= 0:
            return None
        digest = hashlib.sha256(logical.read_bytes()).hexdigest()
        after = logical.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return {
            "path": logical.relative_to(root.resolve()).as_posix(),
            "sha256": digest,
            "size": after.st_size,
        }
    except (OSError, ValueError):
        return None


def _input_paths(root: Path, papers_dir: Path, paper_id: str, step: int) -> list[Path]:
    if step <= 1:
        return [root / "research_profile.md"]
    previous = expected_step_artifacts(root, papers_dir, paper_id, step - 1)
    return list(previous)


def _input_fingerprints(root: Path, papers_dir: Path, paper_id: str, step: int) -> list[dict[str, Any]] | None:
    results: list[dict[str, Any]] = []
    for path in _input_paths(root, papers_dir, paper_id, step):
        item = _file_fingerprint(path, root)
        if item is None:
            return None
        results.append(item)
    return results


def _extract_step_prompt(scheduler_prompt_path: Path, step: int, paper_id: str = "") -> str:
    text = scheduler_prompt_path.read_text(encoding="utf-8")
    if step == 1:
        pattern = r"(?s)^## Exact Step 1 Prompt\s*.*?^```text\s*\n(.*?)^```\s*$"
    else:
        pattern = rf"(?s)^### Step {step}\s*.*?^```text\s*\n(.*?)^```\s*$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match is None:
        raise WritingDispatchError("WRITING_STEP_TEMPLATE_MISSING", f"Step {step} exact scheduler template is missing")
    prompt = match.group(1).rstrip() + "\n"
    if step > 1:
        if not _PAPER_ID_RE.fullmatch(paper_id):
            raise WritingDispatchError("WRITING_PAPER_ID_INVALID", "paper id is invalid")
        prompt = prompt.replace("<idea-id>", paper_id)
        if "<idea-id>" in prompt:
            raise WritingDispatchError("WRITING_STEP_TEMPLATE_UNRESOLVED", "step template still has an idea-id placeholder")
    return prompt


def _ticket_prompt(exact_prompt: str, *, attempt_id: str, paper_id: str, step: int) -> str:
    owner = paper_id or "pending-new-session"
    return (
        f"UNIFIED_WRITING_ATTEMPT={attempt_id}\n"
        f"UNIFIED_WRITING_OWNER={owner}\n"
        f"UNIFIED_WRITING_STEP={step}\n\n"
        "控制边界：只执行下方既有步骤提示词；不要读取、修改或重写 controller/pipeline_state.json、"
        "controller/run_intents、调度账本或任何 prompt 文件。论文共享状态只由并发调度器提交。\n\n"
        + exact_prompt
    )


class WritingParallelDispatcher:
    def __init__(
        self,
        *,
        project: dict[str, Any] | None = None,
        dispatch_path: Path | None = None,
        lock_path: Path | None = None,
        completion_reader: CodexThreadCompletionReader | None = None,
        launcher_verifier: Callable[[], bool] | None = None,
        automation_active_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.project = dict(project or LEGACY_PROJECTS["writing"])
        self.root = Path(self.project["root"])
        self.papers_dir = Path(self.project["papers_dir"])
        self.pipeline_path = Path(self.project["state_path"])
        self.scheduler_prompt_path = Path(self.project["prompt_path"])
        self.dispatch_path = Path(dispatch_path or WRITING_DISPATCH_STATE_PATH)
        self.lock_path = Path(lock_path or WRITING_DISPATCH_LOCK_PATH)
        self.completion_reader = completion_reader or CodexThreadCompletionReader()
        self.launcher_verifier = launcher_verifier or self._default_launcher_verified
        self.automation_active_probe = automation_active_probe or self._default_automation_active

    def _default_launcher_verified(self) -> bool:
        automation_id = str(self.project["primary_automation_id"])
        path = AUTOMATIONS_ROOT / automation_id / "automation.toml"
        return writing_parallel_launcher_verified(path)

    def _default_automation_active(self) -> bool:
        automation_id = str(self.project["primary_automation_id"])
        path = AUTOMATIONS_ROOT / automation_id / "automation.toml"
        return str(parse_toml_like(path).get("status") or "").upper() == "ACTIVE"

    def _empty_dispatch(self) -> dict[str, Any]:
        return {
            "schema": WRITING_DISPATCH_SCHEMA,
            "protocol": WRITING_DISPATCH_PROTOCOL,
            "epoch": 0,
            "claims": {},
            "history": [],
            "updated_at": now_iso(),
        }

    def _load_dispatch(self) -> dict[str, Any]:
        state = read_json(self.dispatch_path, self._empty_dispatch())
        if not isinstance(state, dict) or state.get("schema") != WRITING_DISPATCH_SCHEMA:
            raise WritingDispatchError("WRITING_DISPATCH_STATE_INVALID", "parallel dispatch state is invalid")
        if state.get("protocol") != WRITING_DISPATCH_PROTOCOL:
            raise WritingDispatchError("WRITING_DISPATCH_PROTOCOL_MISMATCH", "parallel dispatch protocol is incompatible")
        if not isinstance(state.get("claims"), dict):
            raise WritingDispatchError("WRITING_DISPATCH_CLAIMS_INVALID", "parallel claims are invalid")
        if not isinstance(state.get("history"), list):
            state["history"] = []
        return state

    @staticmethod
    def _settings(pipeline: dict[str, Any]) -> tuple[dict[str, Any], int]:
        settings = pipeline.get("settings")
        if not isinstance(settings, dict):
            raise WritingDispatchError("WRITING_PARALLEL_SETTINGS_MISSING", "writing settings are missing")
        if settings.get("dispatch_mode") != "parallel_workers":
            raise WritingDispatchError("WRITING_PARALLEL_MODE_DISABLED", "parallel writing mode is not enabled")
        if settings.get("writing_dispatch_protocol") != WRITING_DISPATCH_PROTOCOL:
            raise WritingDispatchError("WRITING_DISPATCH_PROTOCOL_MISMATCH", "writing state has another dispatch protocol")
        raw = settings.get("max_parallel_workers", WRITING_DEFAULT_PARALLEL_WORKERS)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise WritingDispatchError("WRITING_PARALLEL_LIMIT_INVALID", "writing parallel limit must be an integer")
        if not 1 <= raw <= WRITING_MAX_PARALLEL_WORKERS:
            raise WritingDispatchError(
                "WRITING_PARALLEL_LIMIT_INVALID",
                f"writing parallel limit must be 1-{WRITING_MAX_PARALLEL_WORKERS}",
            )
        return settings, raw

    def _append_history(self, dispatch: dict[str, Any], event: dict[str, Any]) -> None:
        history = dispatch.setdefault("history", [])
        history.append({"at": now_iso(), **event})
        if len(history) > 300:
            del history[:-300]

    def _completion_signature(self, result: dict[str, Any]) -> dict[str, str]:
        return {
            "turn_id": str(result.get("turn_id") or ""),
            "timestamp": str(result.get("timestamp") or ""),
            "rollout_file": str(result.get("rollout_file") or ""),
        }

    def _mark_command(self, pipeline: dict[str, Any], command_id: str, *, status: str, message: str) -> None:
        commands = pipeline.get("commands")
        if not isinstance(commands, list):
            return
        for item in commands:
            if isinstance(item, dict) and str(item.get("id") or "") == command_id:
                item["status"] = status
                item["handled_at"] = now_iso()
                item["message"] = message
                return

    def _reconcile_claims(self, pipeline: dict[str, Any], dispatch: dict[str, Any]) -> bool:
        changed = False
        claims = dispatch["claims"]
        sessions = pipeline.setdefault("sessions", {})
        now = datetime.now().astimezone()
        for attempt_id, claim in list(claims.items()):
            if not isinstance(claim, dict):
                del claims[attempt_id]
                changed = True
                continue
            state = str(claim.get("state") or "")
            if state == "reserved":
                reserved_at = _parse_time(claim.get("reserved_at"))
                if reserved_at is not None and now - reserved_at > _RESERVATION_TTL:
                    self._append_history(dispatch, {"event": "reservation_expired", "attempt_id": attempt_id})
                    del claims[attempt_id]
                    changed = True
                    continue
            thread_id = str(claim.get("thread_id") or "")
            if not thread_id:
                continue
            completion = self.completion_reader.inspect(thread_id)
            if completion.get("status") == "newer_turn_open":
                if state == "dispatched":
                    claim["state"] = "in_progress"
                    claim["updated_at"] = now_iso()
                    changed = True
                continue
            if completion.get("status") != "terminal":
                continue
            signature = self._completion_signature(completion)
            baseline = claim.get("baseline_completion") if isinstance(claim.get("baseline_completion"), dict) else {}
            if signature == baseline:
                continue
            expected_step = int(claim.get("step") or 0)
            if int(completion.get("step_done") or 0) != expected_step:
                claim["state"] = "blocked"
                claim["blocker"] = "terminal_step_mismatch"
                changed = True
                continue
            kind = str(claim.get("kind") or "")
            paper_id = str(completion.get("pipeline_id") or "") if kind == "new_session" else str(claim.get("paper_id") or "")
            if not _PAPER_ID_RE.fullmatch(paper_id) or str(completion.get("pipeline_id") or "") != paper_id:
                claim["state"] = "blocked"
                claim["blocker"] = "terminal_pipeline_mismatch"
                changed = True
                continue
            inputs = _input_fingerprints(self.root, self.papers_dir, paper_id, expected_step)
            recorded_inputs = claim.get("input_fingerprints")
            if kind != "new_session" and inputs != recorded_inputs:
                claim["state"] = "blocked"
                claim["blocker"] = "dispatch_inputs_changed"
                changed = True
                continue
            artifacts = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, expected_step)
            if artifacts is None:
                claim["state"] = "blocked"
                claim["blocker"] = "expected_artifact_missing_or_unfingerprintable"
                changed = True
                continue
            session = sessions.setdefault(paper_id, {})
            if not isinstance(session, dict):
                claim["state"] = "blocked"
                claim["blocker"] = "session_not_object"
                changed = True
                continue
            if kind != "new_session" and str(session.get("thread_id") or "") != thread_id:
                claim["state"] = "blocked"
                claim["blocker"] = "thread_owner_changed"
                changed = True
                continue
            session.update(
                {
                    "pipeline_id": paper_id,
                    "thread_id": thread_id,
                    "step_done": expected_step,
                    "status": "complete" if expected_step >= int(session.get("run_until_step") or 10) else "queued",
                    "updated_at": now_iso(),
                    "completion_reconciliation": {
                        "source": "writing_parallel_dispatch_exact_terminal",
                        "attempt_id": attempt_id,
                        "step_done": expected_step,
                        "thread_id": thread_id,
                        "turn_id": signature["turn_id"],
                        "thread_event_at": signature["timestamp"],
                        "artifacts": artifacts,
                        "reconciled_at": now_iso(),
                    },
                }
            )
            session.setdefault("run_until_step", 10)
            if session["status"] == "complete":
                session.pop("run_until_step", None)
            command_id = str(claim.get("command_id") or "")
            if command_id:
                self._mark_command(
                    pipeline,
                    command_id,
                    status="done",
                    message=f"Parallel dispatcher verified STEP_DONE={expected_step} on thread {thread_id}.",
                )
            self._append_history(
                dispatch,
                {"event": "claim_completed", "attempt_id": attempt_id, "paper_id": paper_id, "step": expected_step},
            )
            del claims[attempt_id]
            changed = True
        return changed

    def _gate_ready(self, pipeline: dict[str, Any], paper_id: str, step: int) -> bool:
        if step == 3:
            result = validate_writing_step2_contract(self.root, paper_id)
            if not result.get("ready"):
                session = pipeline["sessions"][paper_id]
                session["status"] = "paused"
                session["evidence"] = writing_novelty_blocker(result)
                session["updated_at"] = now_iso()
                return False
        if step == 6:
            result = validate_writing_evidence(self.papers_dir, paper_id)
            if not result.get("ready"):
                session = pipeline["sessions"][paper_id]
                session["status"] = "paused"
                session["evidence"] = writing_evidence_blocker(result)
                session["updated_at"] = now_iso()
                return False
        return True

    def _new_claim(
        self,
        dispatch: dict[str, Any],
        *,
        kind: str,
        step: int,
        prompt: str,
        paper_id: str = "",
        thread_id: str = "",
        command_id: str = "",
        baseline: dict[str, Any] | None = None,
        inputs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        attempt_id = f"writing-{uuid4().hex}"
        token = uuid4().hex
        claim = {
            "attempt_id": attempt_id,
            "claim_token": token,
            "kind": kind,
            "paper_id": paper_id,
            "thread_id": thread_id,
            "command_id": command_id,
            "step": step,
            "state": "reserved",
            "reserved_at": now_iso(),
            "updated_at": now_iso(),
            "baseline_completion": dict(baseline or {}),
            "input_fingerprints": list(inputs or []),
            "prompt_sha256": _sha256_text(prompt),
        }
        dispatch["claims"][attempt_id] = claim
        return {
            "schema": WRITING_TICKET_SCHEMA,
            "attempt_id": attempt_id,
            "claim_token": token,
            "action": "create_thread" if kind == "new_session" else "continue_thread",
            "paper_id": paper_id,
            "thread_id": thread_id,
            "step": step,
            "project_id": str(self.root.parent),
            "prompt": prompt,
            "prompt_sha256": claim["prompt_sha256"],
        }

    def prepare(self) -> dict[str, Any]:
        try:
            with FileLock(self.lock_path, stale_seconds=30 * 60):
                if not self.launcher_verifier():
                    raise WritingDispatchError(
                        "WRITING_PARALLEL_LAUNCHER_UNVERIFIED",
                        "official writing automation does not contain the verified parallel launcher",
                    )
                if not self.automation_active_probe():
                    raise WritingDispatchError(
                        "WRITING_PARALLEL_AUTOMATION_PAUSED",
                        "official writing automation is paused",
                    )
                pipeline = read_json(self.pipeline_path, {})
                if not isinstance(pipeline, dict):
                    raise WritingDispatchError("WRITING_PIPELINE_STATE_INVALID", "writing pipeline state is invalid")
                settings, cap = self._settings(pipeline)
                dispatch = self._load_dispatch()
                dispatch["epoch"] = int(dispatch.get("epoch") or 0) + 1
                pipeline_changed = self._reconcile_claims(pipeline, dispatch)

                claims = dispatch["claims"]
                claimed_papers = {
                    str(item.get("paper_id") or "").casefold()
                    for item in claims.values()
                    if isinstance(item, dict) and str(item.get("paper_id") or "")
                }
                claimed_threads = {
                    str(item.get("thread_id") or "")
                    for item in claims.values()
                    if isinstance(item, dict) and str(item.get("state") or "") in _ACTIVE_STATES
                }
                active_claims = sum(
                    1
                    for item in claims.values()
                    if isinstance(item, dict) and str(item.get("state") or "") in _ACTIVE_STATES
                )

                sessions = pipeline.setdefault("sessions", {})
                unclaimed_busy = 0
                if isinstance(sessions, dict):
                    for raw in sessions.values():
                        if not isinstance(raw, dict):
                            continue
                        thread_id = str(raw.get("thread_id") or "")
                        if not thread_id or thread_id in claimed_threads:
                            continue
                        if self.completion_reader.inspect(thread_id).get("status") == "newer_turn_open":
                            unclaimed_busy += 1
                            claimed_threads.add(thread_id)

                available = max(cap - active_claims - unclaimed_busy, 0)
                tickets: list[dict[str, Any]] = []

                commands = pipeline.get("commands") if isinstance(pipeline.get("commands"), list) else []
                new_claim_active = any(
                    isinstance(item, dict)
                    and item.get("kind") == "new_session"
                    and str(item.get("state") or "") in _ACTIVE_STATES
                    for item in claims.values()
                )
                if available and not new_claim_active:
                    pending_new = next(
                        (
                            item
                            for item in commands
                            if isinstance(item, dict)
                            and item.get("action") == "new_session"
                            and item.get("status") == "pending"
                            and not any(
                                isinstance(claim, dict) and claim.get("command_id") == item.get("id")
                                for claim in claims.values()
                            )
                        ),
                        None,
                    )
                    if pending_new is not None:
                        exact = _extract_step_prompt(self.scheduler_prompt_path, 1)
                        attempt_preview = f"writing-{uuid4().hex}"
                        prompt = _ticket_prompt(exact, attempt_id=attempt_preview, paper_id="", step=1)
                        ticket = self._new_claim(
                            dispatch,
                            kind="new_session",
                            step=1,
                            prompt=prompt.replace(attempt_preview, "{ATTEMPT_ID}"),
                            command_id=str(pending_new.get("id") or ""),
                        )
                        ticket["prompt"] = ticket["prompt"].replace("{ATTEMPT_ID}", ticket["attempt_id"])
                        claims[ticket["attempt_id"]]["prompt_sha256"] = _sha256_text(ticket["prompt"])
                        ticket["prompt_sha256"] = claims[ticket["attempt_id"]]["prompt_sha256"]
                        tickets.append(ticket)
                        available -= 1

                if available and isinstance(sessions, dict):
                    ordered_ids: list[str] = []
                    for command in commands:
                        if not isinstance(command, dict) or command.get("action") != "resume_session" or command.get("status") != "pending":
                            continue
                        paper_id = str(command.get("pipeline_id") or "")
                        if paper_id and paper_id not in ordered_ids:
                            ordered_ids.append(paper_id)
                    ordered_ids.extend(str(item) for item in sessions if str(item) not in ordered_ids)
                    seen_casefold: set[str] = set()
                    for paper_id in ordered_ids:
                        if available <= 0:
                            break
                        folded = paper_id.casefold()
                        if folded in seen_casefold or folded in claimed_papers:
                            continue
                        seen_casefold.add(folded)
                        session = sessions.get(paper_id)
                        if not isinstance(session, dict) or str(session.get("status") or "") not in {"queued", "running"}:
                            continue
                        thread_id = str(session.get("thread_id") or "")
                        if not thread_id or thread_id in claimed_threads:
                            continue
                        completion = self.completion_reader.inspect(thread_id)
                        if completion.get("status") != "terminal" or str(completion.get("pipeline_id") or "") != paper_id:
                            continue
                        step_done = int(session.get("step_done") or 0)
                        run_until = int(session.get("run_until_step") or 10)
                        next_step = step_done + 1
                        if next_step > min(run_until, 10):
                            continue
                        try:
                            if not self._gate_ready(pipeline, paper_id, next_step):
                                pipeline_changed = True
                                continue
                            inputs = _input_fingerprints(self.root, self.papers_dir, paper_id, next_step)
                            if inputs is None:
                                continue
                            exact = _extract_step_prompt(self.scheduler_prompt_path, next_step, paper_id)
                        except Exception as exc:
                            session["status"] = "paused"
                            session["evidence"] = f"parallel dispatch preparation failed: {type(exc).__name__}"
                            session["updated_at"] = now_iso()
                            pipeline_changed = True
                            continue
                        attempt_preview = f"writing-{uuid4().hex}"
                        prompt = _ticket_prompt(
                            exact,
                            attempt_id=attempt_preview,
                            paper_id=paper_id,
                            step=next_step,
                        )
                        command_id = next(
                            (
                                str(item.get("id") or "")
                                for item in commands
                                if isinstance(item, dict)
                                and item.get("action") == "resume_session"
                                and item.get("status") == "pending"
                                and str(item.get("pipeline_id") or "") == paper_id
                            ),
                            "",
                        )
                        ticket = self._new_claim(
                            dispatch,
                            kind="continue",
                            step=next_step,
                            prompt=prompt.replace(attempt_preview, "{ATTEMPT_ID}"),
                            paper_id=paper_id,
                            thread_id=thread_id,
                            command_id=command_id,
                            baseline=self._completion_signature(completion),
                            inputs=inputs,
                        )
                        ticket["prompt"] = ticket["prompt"].replace("{ATTEMPT_ID}", ticket["attempt_id"])
                        claims[ticket["attempt_id"]]["prompt_sha256"] = _sha256_text(ticket["prompt"])
                        ticket["prompt_sha256"] = claims[ticket["attempt_id"]]["prompt_sha256"]
                        tickets.append(ticket)
                        claimed_papers.add(folded)
                        claimed_threads.add(thread_id)
                        available -= 1

                if pipeline_changed:
                    pipeline["updated_at"] = now_iso()
                    write_json_atomic(self.pipeline_path, pipeline)
                dispatch["updated_at"] = now_iso()
                self._append_history(
                    dispatch,
                    {
                        "event": "prepare",
                        "epoch": dispatch["epoch"],
                        "cap": cap,
                        "active_before": active_claims + unclaimed_busy,
                        "ticket_count": len(tickets),
                    },
                )
                write_json_atomic(self.dispatch_path, dispatch)
                return {
                    "ok": True,
                    "protocol": WRITING_DISPATCH_PROTOCOL,
                    "epoch": dispatch["epoch"],
                    "configured_cap": cap,
                    "active_before": active_claims + unclaimed_busy,
                    "ticket_count": len(tickets),
                    "tickets": tickets,
                }
        except RuntimeError as exc:
            if not self.lock_path.exists():
                raise
            return {
                "ok": False,
                "code": "WRITING_DISPATCH_BUSY",
                "message": f"another writing dispatcher owns the preparation lock: {type(exc).__name__}",
                "tickets": [],
            }

    def mark_uncertain(self, attempt_id: str, token: str) -> dict[str, Any]:
        with FileLock(self.lock_path, stale_seconds=30 * 60):
            dispatch = self._load_dispatch()
            claim = dispatch["claims"].get(str(attempt_id))
            if not isinstance(claim, dict) or claim.get("claim_token") != str(token):
                raise WritingDispatchError("WRITING_DISPATCH_CLAIM_STALE", "claim token is stale or missing")
            if claim.get("state") != "reserved":
                raise WritingDispatchError("WRITING_DISPATCH_STATE_CONFLICT", "claim is not reserved")
            pipeline = read_json(self.pipeline_path, {})
            settings, cap = self._settings(pipeline if isinstance(pipeline, dict) else {})
            if str(settings.get("scheduling_mode") or "").lower() != "auto":
                del dispatch["claims"][attempt_id]
                self._append_history(dispatch, {"event": "reservation_cancelled_scheduler_paused", "attempt_id": attempt_id})
                dispatch["updated_at"] = now_iso()
                write_json_atomic(self.dispatch_path, dispatch)
                raise WritingDispatchError("WRITING_DISPATCH_PAUSED", "writing scheduler is paused")
            claims = dispatch["claims"]
            begun = sum(
                1
                for item in claims.values()
                if isinstance(item, dict) and str(item.get("state") or "") in {"send_uncertain", "dispatched", "in_progress"}
            )
            reservations = sorted(
                (
                    item
                    for item in claims.values()
                    if isinstance(item, dict) and item.get("state") == "reserved"
                ),
                key=lambda item: (str(item.get("reserved_at") or ""), str(item.get("attempt_id") or "")),
            )
            allowed_reservations = max(cap - begun, 0)
            allowed_ids = {str(item.get("attempt_id") or "") for item in reservations[:allowed_reservations]}
            if attempt_id not in allowed_ids:
                del claims[attempt_id]
                self._append_history(dispatch, {"event": "reservation_drained_after_cap_change", "attempt_id": attempt_id})
                dispatch["updated_at"] = now_iso()
                write_json_atomic(self.dispatch_path, dispatch)
                raise WritingDispatchError("WRITING_DISPATCH_CAP_REDUCED", "parallel cap no longer admits this reservation")
            if claim.get("kind") == "continue":
                sessions = pipeline.get("sessions") if isinstance(pipeline.get("sessions"), dict) else {}
                session = sessions.get(str(claim.get("paper_id") or "")) if isinstance(sessions, dict) else None
                if (
                    not isinstance(session, dict)
                    or str(session.get("status") or "") not in {"queued", "running"}
                    or str(session.get("thread_id") or "") != str(claim.get("thread_id") or "")
                ):
                    del claims[attempt_id]
                    self._append_history(dispatch, {"event": "reservation_cancelled_owner_changed", "attempt_id": attempt_id})
                    dispatch["updated_at"] = now_iso()
                    write_json_atomic(self.dispatch_path, dispatch)
                    raise WritingDispatchError("WRITING_DISPATCH_OWNER_CHANGED", "paper was paused, stopped, or reassigned")
            else:
                commands = pipeline.get("commands") if isinstance(pipeline.get("commands"), list) else []
                command = next(
                    (
                        item
                        for item in commands
                        if isinstance(item, dict) and str(item.get("id") or "") == str(claim.get("command_id") or "")
                    ),
                    None,
                )
                if not isinstance(command, dict) or command.get("status") != "pending":
                    del claims[attempt_id]
                    self._append_history(dispatch, {"event": "reservation_cancelled_command_changed", "attempt_id": attempt_id})
                    dispatch["updated_at"] = now_iso()
                    write_json_atomic(self.dispatch_path, dispatch)
                    raise WritingDispatchError("WRITING_DISPATCH_COMMAND_CHANGED", "new-session command is no longer pending")
            claim["state"] = "send_uncertain"
            claim["send_uncertain_at"] = now_iso()
            claim["updated_at"] = now_iso()
            dispatch["updated_at"] = now_iso()
            write_json_atomic(self.dispatch_path, dispatch)
            return {
                "ok": True,
                "attempt_id": attempt_id,
                "state": "send_uncertain",
                "action": "create_thread" if claim.get("kind") == "new_session" else "continue_thread",
                "thread_id": str(claim.get("thread_id") or ""),
                "prompt_sha256": claim.get("prompt_sha256"),
            }

    def record_dispatch(self, attempt_id: str, token: str, thread_id: str) -> dict[str, Any]:
        thread_id = str(thread_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", thread_id):
            raise WritingDispatchError("WRITING_THREAD_ID_INVALID", "returned thread id is invalid")
        with FileLock(self.lock_path, stale_seconds=30 * 60):
            dispatch = self._load_dispatch()
            claim = dispatch["claims"].get(str(attempt_id))
            if not isinstance(claim, dict) or claim.get("claim_token") != str(token):
                raise WritingDispatchError("WRITING_DISPATCH_CLAIM_STALE", "claim token is stale or missing")
            if claim.get("state") not in {"send_uncertain", "dispatched", "in_progress"}:
                raise WritingDispatchError("WRITING_DISPATCH_STATE_CONFLICT", "claim was not prepared for a send")
            existing = str(claim.get("thread_id") or "")
            if existing and existing != thread_id:
                raise WritingDispatchError("WRITING_THREAD_OWNER_MISMATCH", "dispatch returned another thread id")
            for other_id, other in dispatch["claims"].items():
                if other_id != attempt_id and isinstance(other, dict) and str(other.get("thread_id") or "") == thread_id:
                    raise WritingDispatchError("WRITING_THREAD_ALREADY_CLAIMED", "thread already belongs to another claim")
            claim["thread_id"] = thread_id
            claim["state"] = "dispatched"
            claim["dispatched_at"] = claim.get("dispatched_at") or now_iso()
            claim["updated_at"] = now_iso()
            dispatch["updated_at"] = now_iso()
            write_json_atomic(self.dispatch_path, dispatch)
            return {"ok": True, "attempt_id": attempt_id, "state": "dispatched", "thread_id": thread_id}

    def release_reserved(self, attempt_id: str, token: str) -> dict[str, Any]:
        with FileLock(self.lock_path, stale_seconds=30 * 60):
            dispatch = self._load_dispatch()
            claim = dispatch["claims"].get(str(attempt_id))
            if not isinstance(claim, dict) or claim.get("claim_token") != str(token):
                raise WritingDispatchError("WRITING_DISPATCH_CLAIM_STALE", "claim token is stale or missing")
            if claim.get("state") != "reserved":
                raise WritingDispatchError(
                    "WRITING_DISPATCH_RELEASE_REFUSED",
                    "only a definitely unsent reserved claim may be released",
                )
            del dispatch["claims"][attempt_id]
            self._append_history(dispatch, {"event": "reservation_released", "attempt_id": attempt_id})
            dispatch["updated_at"] = now_iso()
            write_json_atomic(self.dispatch_path, dispatch)
            return {"ok": True, "attempt_id": attempt_id, "state": "released"}

    def _require_offline_reconciliation(self, pipeline: dict[str, Any]) -> None:
        if self.automation_active_probe():
            raise WritingDispatchError(
                "WRITING_RECONCILIATION_REQUIRES_PAUSE",
                "pause the writing automation before reconciling an uncertain send",
            )
        settings = pipeline.get("settings") if isinstance(pipeline.get("settings"), dict) else {}
        if str(settings.get("scheduling_mode") or "").strip().lower() != "manual":
            raise WritingDispatchError(
                "WRITING_RECONCILIATION_REQUIRES_MANUAL_MODE",
                "writing scheduling must be manual during uncertain-send reconciliation",
            )

    def reconcile_uncertain_adopt_terminal(
        self,
        attempt_id: str,
        *,
        paper_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Adopt an independently verified Step 1 terminal without re-sending it."""

        attempt_id = str(attempt_id or "").strip()
        paper_id = str(paper_id or "").strip()
        thread_id = str(thread_id or "").strip()
        if not _PAPER_ID_RE.fullmatch(paper_id):
            raise WritingDispatchError("WRITING_PAPER_ID_INVALID", "paper id is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", thread_id):
            raise WritingDispatchError("WRITING_THREAD_ID_INVALID", "thread id is invalid")
        with FileLock(self.lock_path, stale_seconds=30 * 60):
            pipeline = read_json(self.pipeline_path, {})
            if not isinstance(pipeline, dict):
                raise WritingDispatchError("WRITING_PIPELINE_STATE_INVALID", "writing pipeline state is invalid")
            self._require_offline_reconciliation(pipeline)
            dispatch = self._load_dispatch()
            claim = dispatch["claims"].get(attempt_id)
            if not isinstance(claim, dict) or claim.get("state") != "send_uncertain":
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_CLAIM_INVALID",
                    "only a send_uncertain claim may be reconciled",
                )
            if claim.get("kind") != "new_session" or int(claim.get("step") or 0) != 1:
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_KIND_INVALID",
                    "terminal adoption is limited to an uncertain new Step 1 claim",
                )
            completion = self.completion_reader.inspect(thread_id)
            if (
                completion.get("status") != "terminal"
                or str(completion.get("pipeline_id") or "") != paper_id
                or int(completion.get("step_done") or 0) != 1
            ):
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_TERMINAL_INVALID",
                    "the supplied thread is not an exact terminal Step 1 for this paper",
                )
            artifacts = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, 1)
            if artifacts is None:
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_ARTIFACT_INVALID",
                    "the Step 1 artifact is missing or unstable",
                )
            sessions = pipeline.setdefault("sessions", {})
            if not isinstance(sessions, dict):
                raise WritingDispatchError("WRITING_PIPELINE_STATE_INVALID", "writing sessions are invalid")
            for existing_id, existing in sessions.items():
                if not isinstance(existing, dict):
                    continue
                if existing_id != paper_id and str(existing.get("thread_id") or "") == thread_id:
                    raise WritingDispatchError(
                        "WRITING_THREAD_ALREADY_OWNED",
                        "the terminal thread already belongs to another paper",
                    )
            command_id = str(claim.get("command_id") or "")
            commands = pipeline.get("commands") if isinstance(pipeline.get("commands"), list) else []
            command = next(
                (
                    item
                    for item in commands
                    if isinstance(item, dict) and str(item.get("id") or "") == command_id
                ),
                None,
            )
            already_adopted = bool(
                isinstance(command, dict)
                and command.get("status") == "done"
                and str(command.get("adopted_pipeline_id") or "") == paper_id
                and str(command.get("adopted_thread_id") or "") == thread_id
            )
            if not isinstance(command, dict) or (command.get("status") != "pending" and not already_adopted):
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_COMMAND_CHANGED",
                    "the claimed new-session command is no longer pending or identically adopted",
                )
            signature = self._completion_signature(completion)
            session = sessions.setdefault(paper_id, {})
            if not isinstance(session, dict):
                raise WritingDispatchError("WRITING_PIPELINE_STATE_INVALID", "paper session is invalid")
            if session and str(session.get("thread_id") or "") not in {"", thread_id}:
                raise WritingDispatchError(
                    "WRITING_THREAD_OWNER_MISMATCH",
                    "the paper is already bound to another thread",
                )
            session.update(
                {
                    "pipeline_id": paper_id,
                    "thread_id": thread_id,
                    "step_done": 1,
                    "run_until_step": 10,
                    "status": "queued",
                    "updated_at": now_iso(),
                    "completion_reconciliation": {
                        "source": "writing_parallel_offline_terminal_adoption",
                        "attempt_id": attempt_id,
                        "step_done": 1,
                        "thread_id": thread_id,
                        "turn_id": signature["turn_id"],
                        "thread_event_at": signature["timestamp"],
                        "artifacts": artifacts,
                        "reconciled_at": now_iso(),
                    },
                }
            )
            command.update(
                {
                    "status": "done",
                    "handled_at": now_iso(),
                    "message": "Adopted an existing verified terminal Step 1; no replacement thread was created.",
                    "adopted_pipeline_id": paper_id,
                    "adopted_thread_id": thread_id,
                    "adopted_at": now_iso(),
                }
            )
            pipeline["updated_at"] = now_iso()
            # Pipeline first is intentional: a crash leaves the uncertain claim
            # fenced instead of exposing the command for duplicate creation.
            write_json_atomic(self.pipeline_path, pipeline)
            del dispatch["claims"][attempt_id]
            self._append_history(
                dispatch,
                {
                    "event": "claim_completed",
                    "attempt_id": attempt_id,
                    "paper_id": paper_id,
                    "step": 1,
                    "reconciliation": "adopted_existing_terminal",
                    "thread_id": thread_id,
                },
            )
            dispatch["updated_at"] = now_iso()
            write_json_atomic(self.dispatch_path, dispatch)
            return {
                "ok": True,
                "attempt_id": attempt_id,
                "resolution": "adopted_existing_terminal",
                "paper_id": paper_id,
                "thread_id": thread_id,
                "command_id": command_id,
            }

    def reconcile_uncertain_unsent(self, attempt_id: str) -> dict[str, Any]:
        """Close a continue claim only when the owned thread proves no newer turn."""

        attempt_id = str(attempt_id or "").strip()
        with FileLock(self.lock_path, stale_seconds=30 * 60):
            pipeline = read_json(self.pipeline_path, {})
            if not isinstance(pipeline, dict):
                raise WritingDispatchError("WRITING_PIPELINE_STATE_INVALID", "writing pipeline state is invalid")
            self._require_offline_reconciliation(pipeline)
            dispatch = self._load_dispatch()
            claim = dispatch["claims"].get(attempt_id)
            if not isinstance(claim, dict) or claim.get("state") != "send_uncertain":
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_CLAIM_INVALID",
                    "only a send_uncertain claim may be reconciled",
                )
            if claim.get("kind") != "continue":
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_UNSENT_UNPROVABLE",
                    "an uncertain thread creation requires explicit terminal adoption",
                )
            thread_id = str(claim.get("thread_id") or "")
            completion = self.completion_reader.inspect(thread_id)
            signature = self._completion_signature(completion)
            baseline = claim.get("baseline_completion") if isinstance(claim.get("baseline_completion"), dict) else {}
            if completion.get("status") != "terminal" or signature != baseline:
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_NEWER_TURN_POSSIBLE",
                    "the worker thread no longer matches its pre-send terminal baseline",
                )
            paper_id = str(claim.get("paper_id") or "")
            step = int(claim.get("step") or 0)
            expected = expected_step_artifacts(self.root, self.papers_dir, paper_id, step)
            if any(path.exists() for path in expected):
                raise WritingDispatchError(
                    "WRITING_RECONCILIATION_OUTPUT_PRESENT",
                    "next-step output exists; manual review is required before declaring the send unsent",
                )
            del dispatch["claims"][attempt_id]
            self._append_history(
                dispatch,
                {
                    "event": "send_uncertain_reconciled_unsent",
                    "attempt_id": attempt_id,
                    "paper_id": paper_id,
                    "step": step,
                    "thread_id": thread_id,
                },
            )
            dispatch["updated_at"] = now_iso()
            write_json_atomic(self.dispatch_path, dispatch)
            return {
                "ok": True,
                "attempt_id": attempt_id,
                "resolution": "verified_unsent",
                "paper_id": paper_id,
                "thread_id": thread_id,
                "step": step,
            }

    def status(self) -> dict[str, Any]:
        with FileLock(self.lock_path, stale_seconds=30 * 60):
            pipeline = read_json(self.pipeline_path, {})
            try:
                _settings, cap = self._settings(pipeline if isinstance(pipeline, dict) else {})
                settings_ready = True
                blocker = ""
            except WritingDispatchError as exc:
                cap = WRITING_DEFAULT_PARALLEL_WORKERS
                settings_ready = False
                blocker = exc.code
            dispatch = self._load_dispatch()
            active = [
                {
                    "attempt_id": key,
                    "paper_id": str(item.get("paper_id") or ""),
                    "thread_id": str(item.get("thread_id") or ""),
                    "step": item.get("step"),
                    "state": item.get("state"),
                }
                for key, item in dispatch["claims"].items()
                if isinstance(item, dict) and str(item.get("state") or "") in _ACTIVE_STATES
            ]
            return {
                "ok": settings_ready and self.launcher_verifier(),
                "protocol": WRITING_DISPATCH_PROTOCOL,
                "configured_cap": cap,
                "active_count": len(active),
                "available_slots": max(cap - len(active), 0),
                "claims": active,
                "launcher_verified": self.launcher_verifier(),
                "automation_active": self.automation_active_probe(),
                "blocker": blocker,
            }


def prepare_writing_dispatch() -> dict[str, Any]:
    return WritingParallelDispatcher().prepare()


def mark_writing_dispatch_uncertain(attempt_id: str, token: str) -> dict[str, Any]:
    return WritingParallelDispatcher().mark_uncertain(attempt_id, token)


def record_writing_dispatch(attempt_id: str, token: str, thread_id: str) -> dict[str, Any]:
    return WritingParallelDispatcher().record_dispatch(attempt_id, token, thread_id)


def release_writing_dispatch(attempt_id: str, token: str) -> dict[str, Any]:
    return WritingParallelDispatcher().release_reserved(attempt_id, token)


def reconcile_writing_uncertain_adopt_terminal(
    attempt_id: str,
    *,
    paper_id: str,
    thread_id: str,
) -> dict[str, Any]:
    return WritingParallelDispatcher().reconcile_uncertain_adopt_terminal(
        attempt_id,
        paper_id=paper_id,
        thread_id=thread_id,
    )


def reconcile_writing_uncertain_unsent(attempt_id: str) -> dict[str, Any]:
    return WritingParallelDispatcher().reconcile_uncertain_unsent(attempt_id)


def writing_dispatch_status() -> dict[str, Any]:
    return WritingParallelDispatcher().status()


__all__ = [
    "WRITING_DISPATCH_SCHEMA",
    "WRITING_TICKET_SCHEMA",
    "WritingDispatchError",
    "WritingParallelDispatcher",
    "mark_writing_dispatch_uncertain",
    "prepare_writing_dispatch",
    "reconcile_writing_uncertain_adopt_terminal",
    "reconcile_writing_uncertain_unsent",
    "record_writing_dispatch",
    "release_writing_dispatch",
    "writing_dispatch_status",
]
