from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import (
    FIGURE_MAX_PARALLEL_WORKERS,
    FIGURE_PIPELINE_LOCK_PATH,
    FULL_PIPELINE_LOCK_PATH,
    FULL_PIPELINE_MAX_PAPERS,
    FULL_PIPELINE_STATE_PATH,
    JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS,
    SMART_SUBMISSION_STATE_PATH,
    WRITING_MAX_PARALLEL_WORKERS,
)
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .writing_gate_policy import AUTO_TRUST_MODE


FULL_PIPELINE_SCHEMA = "unified-paper-orchestrator/full-pipeline/v1"
FULL_PIPELINE_STAGES = ("writing", "figures", "journal_author", "smart_submission")
FULL_PIPELINE_ACTIVE_STATUSES = frozenset(
    {"preparing", "running", "needs_attention", "paused"}
)
SMART_TERMINAL_STATUSES = frozenset({"submitted", "monitoring", "accepted"})
HARD_STAGE_STATUSES = frozenset(
    {"blocked", "failed", "stopped", "needs_user", "manual_required"}
)
HARD_SMART_STATUSES = frozenset(
    {"blocked", "stopped", "waiting_user_approval", "manual_takeover"}
)
_HISTORY_LIMIT = 300
_DISPATCH_STALE_SECONDS = 10 * 60
_MAX_STAGE_DISPATCH_ATTEMPTS = 3
_SMART_WAKEUP_INTERVAL_SECONDS = 45
LOGGER = logging.getLogger(__name__)


class FullPipelineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _default_state() -> dict[str, Any]:
    return {
        "schema": FULL_PIPELINE_SCHEMA,
        "revision": 0,
        "active_batch_id": "",
        "batches": {},
        "history": [],
        "updated_at": "",
    }


def _normalized_state(value: Any) -> dict[str, Any]:
    if value in ({}, None):
        return _default_state()
    if not isinstance(value, dict):
        raise FullPipelineError(
            "FULL_PIPELINE_STATE_INVALID",
            "一键全流程状态文件根节点不是对象。",
        )
    if value.get("schema") != FULL_PIPELINE_SCHEMA:
        raise FullPipelineError(
            "FULL_PIPELINE_STATE_SCHEMA_INVALID",
            "一键全流程状态文件版本不受支持。",
        )
    state = copy.deepcopy(value)
    try:
        revision = int(state.get("revision") or 0)
    except (TypeError, ValueError):
        revision = -1
    if revision < 0:
        raise FullPipelineError(
            "FULL_PIPELINE_STATE_INVALID",
            "一键全流程状态修订号无效。",
        )
    if not isinstance(state.get("batches"), dict):
        raise FullPipelineError(
            "FULL_PIPELINE_STATE_INVALID",
            "一键全流程批次账本无效。",
        )
    if not isinstance(state.get("history"), list):
        raise FullPipelineError(
            "FULL_PIPELINE_STATE_INVALID",
            "一键全流程审计记录无效。",
        )
    state["revision"] = revision
    state.setdefault("active_batch_id", "")
    state.setdefault("updated_at", "")
    return state


def _append_history(state: dict[str, Any], event: dict[str, Any]) -> None:
    history = state.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        state["history"] = history
    history.append({"at": now_iso(), **event})
    state["history"] = history[-_HISTORY_LIMIT:]


def _write_state(path: Path, state: dict[str, Any]) -> None:
    state["revision"] = int(state.get("revision") or 0) + 1
    state["updated_at"] = now_iso()
    write_json_atomic(path, state)


def _compact_result(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return "[omitted]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:24]:
            token = str(key)
            if token in {"state", "papers", "history", "sessions", "commands"}:
                continue
            result[token] = _compact_result(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_compact_result(item, depth=depth + 1) for item in value[:12]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _batch_id() -> str:
    digest = hashlib.sha256(
        f"{time.time_ns()}|{threading.get_ident()}".encode("utf-8")
    ).hexdigest()[:10]
    return f"full-{time.strftime('%Y%m%d-%H%M%S')}-{digest}"


def _normalize_parallel(value: Any) -> int:
    if isinstance(value, bool):
        raise FullPipelineError(
            "FULL_PIPELINE_PARALLEL_INVALID",
            f"并发数量必须是 1-{FULL_PIPELINE_MAX_PAPERS} 的整数。",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise FullPipelineError(
            "FULL_PIPELINE_PARALLEL_INVALID",
            f"并发数量必须是 1-{FULL_PIPELINE_MAX_PAPERS} 的整数。",
        ) from None
    if not 1 <= parsed <= FULL_PIPELINE_MAX_PAPERS:
        raise FullPipelineError(
            "FULL_PIPELINE_PARALLEL_INVALID",
            f"并发数量必须是 1-{FULL_PIPELINE_MAX_PAPERS} 的整数。",
        )
    return parsed


def _stage_status(record: Mapping[str, Any], stage: str) -> str:
    stages = record.get("stages")
    stage_record = stages.get(stage) if isinstance(stages, Mapping) else None
    return (
        str(stage_record.get("status") or "missing").strip().casefold()
        if isinstance(stage_record, Mapping)
        else "missing"
    )


def _stage_detail(record: Mapping[str, Any], stage: str) -> str:
    stages = record.get("stages")
    stage_record = stages.get(stage) if isinstance(stages, Mapping) else None
    return (
        str(stage_record.get("detail") or "").strip()
        if isinstance(stage_record, Mapping)
        else ""
    )


def _stage_artifact(record: Mapping[str, Any], stage: str) -> str:
    stages = record.get("stages")
    stage_record = stages.get(stage) if isinstance(stages, Mapping) else None
    return (
        str(stage_record.get("artifact") or "").strip()
        if isinstance(stage_record, Mapping)
        else ""
    )


def _dispatch_ready(marker: Any, now_epoch: float) -> bool:
    if not isinstance(marker, Mapping):
        return True
    state = str(marker.get("state") or "").strip().casefold()
    if state == "succeeded":
        return False
    if state == "dispatching":
        try:
            claimed_at = float(marker.get("claimed_at_epoch") or 0)
        except (TypeError, ValueError):
            claimed_at = 0
        return bool(claimed_at and now_epoch - claimed_at > _DISPATCH_STALE_SECONDS)
    try:
        attempts = int(marker.get("attempts") or 0)
        retry_at = float(marker.get("next_retry_at_epoch") or 0)
    except (TypeError, ValueError):
        return False
    return attempts < _MAX_STAGE_DISPATCH_ATTEMPTS and now_epoch >= retry_at


class FullPipelineStore:
    def __init__(
        self,
        path: Path | str = FULL_PIPELINE_STATE_PATH,
        lock_path: Path | str = FULL_PIPELINE_LOCK_PATH,
        *,
        lock_wait_seconds: float = 5.0,
        lock_poll_seconds: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.lock_path = Path(lock_path)
        self.lock_wait_seconds = max(float(lock_wait_seconds), 0.0)
        self.lock_poll_seconds = max(float(lock_poll_seconds), 0.01)

    def _load_unlocked(self) -> dict[str, Any]:
        return _normalized_state(read_json(self.path, {}))

    def load(self) -> dict[str, Any]:
        # State commits use atomic replace, so readers always see either the
        # previous complete snapshot or the next complete snapshot.  Taking
        # the writer lock here made the UI's frequent read-only polling race
        # with batch creation and could falsely abort a valid production run.
        return self._load_unlocked()

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        deadline = time.monotonic() + self.lock_wait_seconds
        while True:
            try:
                with FileLock(self.lock_path):
                    state = self._load_unlocked()
                    result = callback(state)
                    _write_state(self.path, state)
                    return result
            except RuntimeError as exc:
                if "统一调度锁已存在" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(self.lock_poll_seconds)


class FullPipelineCoordinator:
    """Persistent deterministic handoff controller for the four production stages."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        store: FullPipelineStore | None = None,
        smart_state_path: Path | str = SMART_SUBMISSION_STATE_PATH,
        smart_prepare: Callable[..., dict[str, Any]] | None = None,
        smart_start: Callable[..., dict[str, Any]] | None = None,
        smart_policy_apply: Callable[..., dict[str, Any]] | None = None,
        smart_policy_restore: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.store = store or FullPipelineStore()
        self.smart_state_path = Path(smart_state_path)
        self._smart_prepare = smart_prepare
        self._smart_start = smart_start
        self._smart_policy_apply = smart_policy_apply
        self._smart_policy_restore = smart_policy_restore
        self._tick_lock = threading.Lock()

    @staticmethod
    def _smart_services() -> tuple[
        Callable[..., dict[str, Any]],
        Callable[..., dict[str, Any]],
        Callable[..., dict[str, Any]],
        Callable[..., dict[str, Any]],
    ]:
        from .smart_submission import (
            configure_smart_submission_full_pipeline_policy,
            prepare_smart_submission_sdk_papers,
            restore_smart_submission_full_pipeline_policy,
            start_smart_submission_program_runtime,
        )

        return (
            prepare_smart_submission_sdk_papers,
            start_smart_submission_program_runtime,
            configure_smart_submission_full_pipeline_policy,
            restore_smart_submission_full_pipeline_policy,
        )

    def _resolved_smart_services(self) -> tuple[
        Callable[..., dict[str, Any]],
        Callable[..., dict[str, Any]],
        Callable[..., dict[str, Any]],
        Callable[..., dict[str, Any]],
    ]:
        defaults = self._smart_services()
        return (
            self._smart_prepare or defaults[0],
            self._smart_start or defaults[1],
            self._smart_policy_apply or defaults[2],
            self._smart_policy_restore or defaults[3],
        )

    def _stage_limit_scope(self, stage: str, adapter: Any) -> Any:
        mutation_scope = getattr(adapter, "pipeline_mutation_scope", None)
        if callable(mutation_scope):
            return mutation_scope()
        if stage == "figures":
            return FileLock(FIGURE_PIPELINE_LOCK_PATH)
        return nullcontext()

    def _apply_stage_limits(self, parallel: int) -> dict[str, Any]:
        caps = {
            "writing": WRITING_MAX_PARALLEL_WORKERS,
            "figures": FIGURE_MAX_PARALLEL_WORKERS,
            "journal_author": JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS,
        }
        result: dict[str, Any] = {}
        try:
            for stage, cap in caps.items():
                adapter = self.orchestrator.adapters[stage]
                expected = min(parallel, cap)
                with self._stage_limit_scope(stage, adapter):
                    state = adapter.load_state()
                    settings = state.setdefault("settings", {})
                    if not isinstance(settings, dict):
                        settings = {}
                        state["settings"] = settings
                    previous = {
                        "present": "max_parallel_workers" in settings,
                        "value": copy.deepcopy(settings.get("max_parallel_workers")),
                    }
                    settings["max_parallel_workers"] = expected
                    settings["full_pipeline_limit_applied_at"] = now_iso()
                    adapter.save_state(state)
                result[stage] = {
                    "previous": previous,
                    "expected": expected,
                    "cap": cap,
                }
        except Exception:
            # A partially applied batch must never leave global stage settings
            # changed.  The restore is compare-and-swap and therefore also
            # preserves any concurrent user edit.
            self._restore_stage_limits(result)
            raise
        return result

    def _restore_stage_limits(self, limits: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for stage in ("writing", "figures", "journal_author"):
            token = limits.get(stage) if isinstance(limits, Mapping) else None
            if not isinstance(token, Mapping):
                continue
            adapter = self.orchestrator.adapters[stage]
            with self._stage_limit_scope(stage, adapter):
                state = adapter.load_state()
                settings = state.setdefault("settings", {})
                if not isinstance(settings, dict):
                    settings = {}
                    state["settings"] = settings
                expected = token.get("expected")
                if settings.get("max_parallel_workers") != expected:
                    result[stage] = {
                        "restored": False,
                        "reason": "user_or_external_change_preserved",
                    }
                    continue
                previous = token.get("previous")
                if isinstance(previous, Mapping) and bool(previous.get("present")):
                    settings["max_parallel_workers"] = copy.deepcopy(
                        previous.get("value")
                    )
                else:
                    settings.pop("max_parallel_workers", None)
                settings.pop("full_pipeline_limit_applied_at", None)
                adapter.save_state(state)
                result[stage] = {"restored": True}
        return result

    @staticmethod
    def _extract_writing_commands(result: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        candidates.append(result.get("commands"))
        scheduler = result.get("last_scheduler_result")
        if isinstance(scheduler, Mapping):
            results = scheduler.get("results")
            if isinstance(results, Mapping):
                writing = results.get("writing")
                if isinstance(writing, Mapping):
                    legacy = writing.get("legacy_result")
                    if isinstance(legacy, Mapping):
                        candidates.append(legacy.get("commands"))
        for value in candidates:
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _command_batch_id(command: Mapping[str, Any]) -> str:
        direct = str(command.get("full_pipeline_batch_id") or "").strip()
        if direct:
            return direct
        params = command.get("params")
        return (
            str(params.get("full_pipeline_batch_id") or "").strip()
            if isinstance(params, Mapping)
            else ""
        )

    @staticmethod
    def _command_prefix(command: Mapping[str, Any]) -> str:
        direct = str(command.get("reserved_pipeline_prefix") or "").strip()
        if direct:
            return direct
        params = command.get("params")
        return (
            str(params.get("reserved_pipeline_prefix") or "").strip()
            if isinstance(params, Mapping)
            else ""
        )

    def _recoverable_failed_start(
        self,
        parallel: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        state = self.store.load()
        if str(state.get("active_batch_id") or ""):
            return "", []
        batches = state.get("batches")
        if not isinstance(batches, Mapping) or not batches:
            return "", []
        batch_id = str(next(reversed(batches)))
        batch = batches.get(batch_id)
        if (
            not isinstance(batch, Mapping)
            or str(batch.get("status") or "") != "failed_start"
            or int(batch.get("requested_count") or 0) != parallel
            or list(batch.get("slots") or [])
        ):
            return "", []
        writing_state = self.orchestrator.adapters["writing"].load_state()
        raw_commands = writing_state.get("commands")
        commands = [
            dict(command)
            for command in raw_commands
            if isinstance(command, Mapping)
            and self._command_batch_id(command) == batch_id
        ] if isinstance(raw_commands, list) else []
        prefixes = [self._command_prefix(command) for command in commands]
        if (
            len(commands) != parallel
            or any(not prefix for prefix in prefixes)
            or len(set(prefixes)) != parallel
        ):
            return "", []
        return batch_id, commands

    def recover_failed_start(self, parallel_count: int) -> dict[str, Any] | None:
        """Resume a batch whose writing commands survived a late start failure.

        This specifically closes the window between durable writing-command
        creation and durable full-pipeline slot registration.  Retrying the
        one-click action must reuse those exact commands instead of creating
        duplicate papers.
        """

        parallel = _normalize_parallel(parallel_count)
        batch_id, commands = self._recoverable_failed_start(parallel)
        if not batch_id:
            return None

        stage_limits: dict[str, Any] = {}
        smart_policy: dict[str, Any] = {}
        try:
            stage_limits = self._apply_stage_limits(parallel)
            _, _, smart_apply, _ = self._resolved_smart_services()
            smart_policy = smart_apply(max_parallel_workers=parallel)

            def reactivate(state: dict[str, Any]) -> None:
                if str(state.get("active_batch_id") or ""):
                    raise FullPipelineError(
                        "FULL_PIPELINE_BATCH_ALREADY_ACTIVE",
                        "另一个一键全流程批次已在恢复期间启动。",
                    )
                batch = state.get("batches", {}).get(batch_id)
                if (
                    not isinstance(batch, dict)
                    or str(batch.get("status") or "") != "failed_start"
                ):
                    raise FullPipelineError(
                        "FULL_PIPELINE_FAILED_START_CHANGED",
                        "待恢复批次状态已变化，请刷新后重试。",
                    )
                batch["stage_limits"] = stage_limits
                batch["smart_policy"] = smart_policy
                batch["limits_restored"] = False
                batch["last_error"] = ""
                batch["status"] = "running"
                batch["updated_at"] = now_iso()
                batch["slots"] = [
                    {
                        "slot": index,
                        "reserved_pipeline_prefix": self._command_prefix(command),
                        "writing_command_id": str(command.get("id") or ""),
                        "paper_id": "",
                        "phase": "writing",
                        "status": "awaiting_writing_identity",
                        "progress_percent": 1,
                        "detail": "恢复已有写作任务，等待 Step 1 生成论文编号",
                        "dispatches": {},
                        "last_error": "",
                        "updated_at": now_iso(),
                    }
                    for index, command in enumerate(commands, start=1)
                ]
                state["active_batch_id"] = batch_id
                _append_history(
                    state,
                    {
                        "event": "full_pipeline_failed_start_recovered",
                        "batch_id": batch_id,
                        "command_count": len(commands),
                    },
                )

            self.store.mutate(reactivate)
            writing_adapter = self.orchestrator.adapters["writing"]
            starter = getattr(writing_adapter, "_start_sdk_background", None)
            if not callable(starter):
                raise FullPipelineError(
                    "FULL_PIPELINE_WRITING_RUNTIME_UNAVAILABLE",
                    "写作 SDK 运行器不支持恢复已有任务。",
                )
            priority_keys = [
                str(command.get("sdk_paper_key") or "").strip()
                or f"new-session:{str(command.get('id') or '').strip()}"
                for command in commands
            ]
            starter(priority_paper_keys=priority_keys)
            return self.status(batch_id=batch_id)
        except Exception:
            if stage_limits:
                self._restore_stage_limits(stage_limits)
            if smart_policy:
                _, _, _, smart_restore = self._resolved_smart_services()
                smart_restore(
                    previous=(smart_policy.get("previous") or {}),
                    expected=(smart_policy.get("expected") or {}),
                )
            raise

    def start_batch(self, parallel_count: int) -> dict[str, Any]:
        parallel = _normalize_parallel(parallel_count)
        policy = self.orchestrator.writing_gate_policy()
        if str(policy.get("mode") or "") != AUTO_TRUST_MODE:
            raise FullPipelineError(
                "FULL_PIPELINE_STEP5_AUTO_TRUST_REQUIRED",
                "一键全流程要求写作 Step 5 使用“自动信任并继续”。请在本窗口输入管理密码启用，或先到写作调度设置中启用。",
            )

        batch_id = _batch_id()

        def create(state: dict[str, Any]) -> None:
            active_id = str(state.get("active_batch_id") or "")
            active = state.get("batches", {}).get(active_id)
            if isinstance(active, Mapping) and str(active.get("status") or "") in FULL_PIPELINE_ACTIVE_STATUSES:
                raise FullPipelineError(
                    "FULL_PIPELINE_BATCH_ALREADY_ACTIVE",
                    f"已有一键全流程批次正在运行：{active_id}",
                )
            now = now_iso()
            state.setdefault("batches", {})[batch_id] = {
                "batch_id": batch_id,
                "status": "preparing",
                "requested_count": parallel,
                "parallel_count": parallel,
                "created_at": now,
                "started_at": now,
                "updated_at": now,
                "completed_at": "",
                "slots": [],
                "stage_limits": {},
                "smart_policy": {},
                "limits_restored": False,
                "last_error": "",
                "history": [],
            }
            state["active_batch_id"] = batch_id
            _append_history(
                state,
                {
                    "event": "full_pipeline_batch_created",
                    "batch_id": batch_id,
                    "parallel_count": parallel,
                },
            )

        self.store.mutate(create)
        stage_limits: dict[str, Any] = {}
        smart_policy: dict[str, Any] = {}
        try:
            stage_limits = self._apply_stage_limits(parallel)
            _, _, smart_apply, _ = self._resolved_smart_services()
            smart_policy = smart_apply(max_parallel_workers=parallel)

            def save_policies(state: dict[str, Any]) -> None:
                batch = state["batches"][batch_id]
                batch["stage_limits"] = stage_limits
                batch["smart_policy"] = smart_policy
                batch["updated_at"] = now_iso()

            self.store.mutate(save_policies)
            writing_result = self.orchestrator.start_new_writing_sessions(
                parallel,
                single_active=False,
                request_context={
                    "full_pipeline_batch_id": batch_id,
                    "full_pipeline_slot_offset": 0,
                },
            )
            commands = self._extract_writing_commands(writing_result)
            if len(commands) != parallel:
                raise FullPipelineError(
                    "FULL_PIPELINE_WRITING_COMMAND_COUNT_MISMATCH",
                    f"写作阶段应创建 {parallel} 个任务，实际返回 {len(commands)} 个。",
                )

            def record_commands(state: dict[str, Any]) -> None:
                batch = state["batches"][batch_id]
                slots: list[dict[str, Any]] = []
                for index, command in enumerate(commands, start=1):
                    prefix = str(command.get("reserved_pipeline_prefix") or "").strip()
                    if not prefix:
                        params = command.get("params")
                        prefix = (
                            str(params.get("reserved_pipeline_prefix") or "").strip()
                            if isinstance(params, Mapping)
                            else ""
                        )
                    if not prefix:
                        raise FullPipelineError(
                            "FULL_PIPELINE_WRITING_IDENTITY_RESERVATION_MISSING",
                            f"第 {index} 个写作任务没有预留论文编号。",
                        )
                    slots.append(
                        {
                            "slot": index,
                            "reserved_pipeline_prefix": prefix,
                            "writing_command_id": str(command.get("id") or ""),
                            "paper_id": "",
                            "phase": "writing",
                            "status": "awaiting_writing_identity",
                            "progress_percent": 1,
                            "detail": "写作 Step 1 正在生成论文编号",
                            "dispatches": {},
                            "last_error": "",
                            "updated_at": now_iso(),
                        }
                    )
                batch["slots"] = slots
                batch["status"] = "running"
                batch["updated_at"] = now_iso()
                batch["writing_start_result"] = _compact_result(writing_result)
                _append_history(
                    state,
                    {
                        "event": "full_pipeline_writing_started",
                        "batch_id": batch_id,
                        "command_count": len(slots),
                    },
                )

            self.store.mutate(record_commands)
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            error_message = str(exc)
            stage_restore: dict[str, Any] = {}
            smart_restore_result: dict[str, Any] = {}
            try:
                if stage_limits:
                    stage_restore = self._restore_stage_limits(stage_limits)
            except Exception as restore_exc:
                stage_restore = {"ok": False, "error": str(restore_exc)}
            try:
                if smart_policy:
                    _, _, _, smart_restore = self._resolved_smart_services()
                    smart_restore_result = smart_restore(
                        previous=(smart_policy.get("previous") or {}),
                        expected=(smart_policy.get("expected") or {}),
                    )
            except Exception as restore_exc:
                smart_restore_result = {"ok": False, "error": str(restore_exc)}
            stage_restore_ok = all(
                isinstance(item, Mapping) and bool(item.get("restored"))
                for item in stage_restore.values()
            ) if stage_restore else True
            smart_restore_ok = bool(smart_restore_result.get("ok", True))
            restore_ok = stage_restore_ok and smart_restore_ok

            def mark_failed(state: dict[str, Any]) -> None:
                batch = state.get("batches", {}).get(batch_id)
                if isinstance(batch, dict):
                    batch["status"] = "failed_start"
                    batch["last_error"] = f"{code}: {error_message}"
                    batch["limits_restored"] = restore_ok
                    batch["limit_restore_result"] = {
                        "stages": stage_restore,
                        "smart_submission": _compact_result(smart_restore_result),
                    }
                    batch["updated_at"] = now_iso()
                if str(state.get("active_batch_id") or "") == batch_id:
                    state["active_batch_id"] = ""
                _append_history(
                    state,
                    {
                        "event": "full_pipeline_start_failed",
                        "batch_id": batch_id,
                        "code": code,
                        "message": error_message[:500],
                    },
                )

            self.store.mutate(mark_failed)
            raise
        return self.status(batch_id=batch_id)

    @staticmethod
    def _match_paper_identity(
        slot: Mapping[str, Any],
        writing_state: Mapping[str, Any],
        assigned: set[str],
    ) -> tuple[str, str]:
        prefix = str(slot.get("reserved_pipeline_prefix") or "").strip()
        sessions = writing_state.get("sessions")
        if not prefix or not isinstance(sessions, Mapping):
            return "", ""
        candidates: list[str] = []
        for raw_paper_id, raw_session in sessions.items():
            paper_id = str(raw_paper_id or "").strip()
            session = raw_session if isinstance(raw_session, Mapping) else {}
            recorded_prefix = str(session.get("reserved_pipeline_prefix") or "").strip()
            if (
                paper_id not in assigned
                and (
                    paper_id.startswith(f"{prefix}_")
                    or recorded_prefix == prefix
                )
            ):
                candidates.append(paper_id)
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return "", f"预留编号 {prefix} 匹配到多个写作会话"
        commands = writing_state.get("commands")
        command_id = str(slot.get("writing_command_id") or "")
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, Mapping) or str(command.get("id") or "") != command_id:
                    continue
                status = str(command.get("status") or "").strip().casefold()
                if status in {"failed", "error", "cancelled"}:
                    return "", str(command.get("message") or f"写作命令状态为 {status}")
        return "", ""

    @staticmethod
    def _slot_observation(
        slot: dict[str, Any],
        paper: Mapping[str, Any],
        smart_paper: Mapping[str, Any] | None,
        now_epoch: float,
    ) -> tuple[str, str, int, str, str | None]:
        """Return phase, status, progress, detail and next deterministic action."""

        dispatches = slot.setdefault("dispatches", {})
        writing_status = _stage_status(paper, "writing")
        figure_status = _stage_status(paper, "figures")
        journal_status = _stage_status(paper, "journal_author")

        smart_status = (
            str(smart_paper.get("status") or "").strip().casefold()
            if isinstance(smart_paper, Mapping)
            else ""
        )
        smart_workflow = (
            str(smart_paper.get("workflow_status") or "").strip().casefold()
            if isinstance(smart_paper, Mapping)
            else ""
        )
        if smart_status in SMART_TERMINAL_STATUSES:
            return (
                "completed",
                smart_status,
                100,
                "投稿已提交，已进入监控" if smart_status != "accepted" else "稿件已接收",
                None,
            )

        smart_dispatched = str(
            (dispatches.get("smart_submission") or {}).get("state")
            if isinstance(dispatches.get("smart_submission"), Mapping)
            else ""
        ) == "succeeded"
        if smart_dispatched or isinstance(smart_paper, Mapping):
            detail = str(
                (smart_paper or {}).get("detail")
                or (smart_paper or {}).get("blocker")
                or "智能投稿正在自动处理"
            )
            if smart_status in HARD_SMART_STATUSES or smart_workflow in HARD_SMART_STATUSES:
                return "smart_submission", "needs_attention", 90, detail, None
            last_wakeup = dispatches.get("smart_wakeup")
            try:
                last_wakeup_epoch = float(
                    last_wakeup.get("finished_at_epoch") or 0
                ) if isinstance(last_wakeup, Mapping) else 0
            except (TypeError, ValueError):
                last_wakeup_epoch = 0
            if not last_wakeup_epoch:
                smart_marker = dispatches.get("smart_submission")
                try:
                    last_wakeup_epoch = float(
                        smart_marker.get("finished_at_epoch") or 0
                    ) if isinstance(smart_marker, Mapping) else 0
                except (TypeError, ValueError):
                    last_wakeup_epoch = 0
            if (
                smart_workflow not in {"running", "waiting_existing_turn"}
                and now_epoch - last_wakeup_epoch >= _SMART_WAKEUP_INTERVAL_SECONDS
            ):
                return "smart_submission", smart_status or "queued", 90, detail, "smart_wakeup"
            return "smart_submission", smart_status or smart_workflow or "running", 90, detail, None

        if journal_status == "complete":
            marker = dispatches.get("smart_submission")
            action = "smart_submission" if _dispatch_ready(marker, now_epoch) else None
            return "journal_author", "complete", 82, "选刊作者已完成，正在交接智能投稿", action
        if journal_status in HARD_STAGE_STATUSES:
            return "journal_author", "needs_attention", 72, _stage_detail(paper, "journal_author"), None

        journal_dispatched = str(
            (dispatches.get("journal_author") or {}).get("state")
            if isinstance(dispatches.get("journal_author"), Mapping)
            else ""
        ) == "succeeded"
        if figure_status == "complete":
            if journal_dispatched:
                return "journal_author", journal_status, 70, "选刊作者阶段正在运行", None
            marker = dispatches.get("journal_author")
            action = "journal_author" if _dispatch_ready(marker, now_epoch) else None
            return "figures", "complete", 62, "画图已完成，正在交接选刊作者", action
        if figure_status in HARD_STAGE_STATUSES:
            return "figures", "needs_attention", 52, _stage_detail(paper, "figures"), None

        figure_dispatched = str(
            (dispatches.get("figures") or {}).get("state")
            if isinstance(dispatches.get("figures"), Mapping)
            else ""
        ) == "succeeded"
        if writing_status == "complete":
            if figure_dispatched:
                return "figures", figure_status, 48, "画图阶段正在运行", None
            marker = dispatches.get("figures")
            action = "figures" if _dispatch_ready(marker, now_epoch) else None
            return "writing", "complete", 42, "写作已完成，正在交接画图", action
        if writing_status in HARD_STAGE_STATUSES:
            return "writing", "needs_attention", 20, _stage_detail(paper, "writing"), None
        return "writing", writing_status, 20, _stage_detail(paper, "writing") or "写作流程正在运行", None

    def _claim_actions(
        self,
        writing_state: Mapping[str, Any],
        unified: Mapping[str, Any],
        smart_state: Mapping[str, Any],
    ) -> tuple[str, list[dict[str, Any]], bool]:
        now_epoch = time.time()
        actions: list[dict[str, Any]] = []
        completed = False

        def mutate(state: dict[str, Any]) -> None:
            nonlocal completed
            batch_id = str(state.get("active_batch_id") or "")
            batch = state.get("batches", {}).get(batch_id)
            if not isinstance(batch, dict) or str(batch.get("status") or "") == "paused":
                return
            slots = batch.get("slots")
            if not isinstance(slots, list):
                slots = []
                batch["slots"] = slots
            assigned = {
                str(item.get("paper_id") or "")
                for item in slots
                if isinstance(item, Mapping) and str(item.get("paper_id") or "")
            }
            papers = unified.get("papers") if isinstance(unified.get("papers"), Mapping) else {}
            smart_papers = smart_state.get("papers") if isinstance(smart_state.get("papers"), Mapping) else {}
            attention = False
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                paper_id = str(slot.get("paper_id") or "")
                if not paper_id:
                    paper_id, identity_error = self._match_paper_identity(slot, writing_state, assigned)
                    if paper_id:
                        slot["paper_id"] = paper_id
                        assigned.add(paper_id)
                        slot["detail"] = "论文编号已建立，继续执行写作流程"
                        slot["updated_at"] = now_iso()
                    elif identity_error:
                        slot.update(
                            {
                                "status": "needs_attention",
                                "detail": identity_error,
                                "last_error": identity_error,
                                "updated_at": now_iso(),
                            }
                        )
                        attention = True
                    continue
                paper = papers.get(paper_id)
                if not isinstance(paper, Mapping):
                    slot.update(
                        {
                            "status": "waiting_projection",
                            "detail": "等待统一状态识别该论文",
                            "updated_at": now_iso(),
                        }
                    )
                    continue
                smart_paper = smart_papers.get(paper_id)
                phase, status, progress, detail, action = self._slot_observation(
                    slot,
                    paper,
                    smart_paper if isinstance(smart_paper, Mapping) else None,
                    now_epoch,
                )
                slot.update(
                    {
                        "phase": phase,
                        "status": status,
                        "progress_percent": progress,
                        "detail": detail,
                        "updated_at": now_iso(),
                    }
                )
                if status == "needs_attention":
                    attention = True
                if not action:
                    continue
                dispatches = slot.setdefault("dispatches", {})
                marker = dispatches.setdefault(action, {})
                try:
                    attempts = int(marker.get("attempts") or 0) + 1
                except (TypeError, ValueError):
                    attempts = 1
                claim_id = hashlib.sha256(
                    f"{batch_id}|{paper_id}|{action}|{attempts}|{time.time_ns()}".encode("utf-8")
                ).hexdigest()[:20]
                marker.update(
                    {
                        "state": "dispatching",
                        "attempts": attempts,
                        "claim_id": claim_id,
                        "claimed_at": now_iso(),
                        "claimed_at_epoch": now_epoch,
                        "last_error": "",
                    }
                )
                actions.append(
                    {
                        "batch_id": batch_id,
                        "slot": int(slot.get("slot") or 0),
                        "paper_id": paper_id,
                        "action": action,
                        "claim_id": claim_id,
                        "writing_artifact": _stage_artifact(paper, "writing"),
                        "figure_artifact": _stage_artifact(paper, "figures"),
                    }
                )
            completed = bool(slots) and all(
                isinstance(slot, Mapping) and str(slot.get("phase") or "") == "completed"
                for slot in slots
            )
            if completed:
                batch["status"] = "completed"
                batch["completed_at"] = now_iso()
                batch["last_error"] = ""
                _append_history(
                    state,
                    {"event": "full_pipeline_batch_completed", "batch_id": batch_id},
                )
            elif attention:
                batch["status"] = "needs_attention"
            elif slots:
                batch["status"] = "running"
                batch["last_error"] = ""
            batch["updated_at"] = now_iso()

        state_before = self.store.load()
        batch_id = str(state_before.get("active_batch_id") or "")
        self.store.mutate(mutate)
        return batch_id, actions, completed

    def _execute_action(self, action: Mapping[str, Any]) -> dict[str, Any]:
        paper_id = str(action.get("paper_id") or "")
        kind = str(action.get("action") or "")
        if kind == "figures":
            result = self.orchestrator.queue_figures_from_step1([paper_id])
            scheduler = result.get("last_scheduler_result") if isinstance(result, Mapping) else None
            paper_result = (
                scheduler.get("results", {}).get(paper_id)
                if isinstance(scheduler, Mapping)
                and isinstance(scheduler.get("results"), Mapping)
                else None
            )
            if not isinstance(paper_result, Mapping) or paper_result.get("ok") is not True:
                raise FullPipelineError(
                    "FULL_PIPELINE_FIGURE_HANDOFF_FAILED",
                    str((paper_result or {}).get("error") or "画图交接未返回成功确认。"),
                )
            return {"ok": True, "result": _compact_result(paper_result)}
        if kind == "journal_author":
            result = self.orchestrator.run_journal_author_papers([paper_id], run_mode="full")
            scheduler = result.get("last_scheduler_result") if isinstance(result, Mapping) else None
            paper_result = (
                scheduler.get("results", {}).get(paper_id)
                if isinstance(scheduler, Mapping)
                and isinstance(scheduler.get("results"), Mapping)
                else None
            )
            if not isinstance(paper_result, Mapping) or paper_result.get("ok") is not True:
                raise FullPipelineError(
                    "FULL_PIPELINE_JOURNAL_HANDOFF_FAILED",
                    str((paper_result or {}).get("error") or "选刊作者交接未返回成功确认。"),
                )
            return {"ok": True, "result": _compact_result(paper_result)}
        prepare, smart_start, _, _ = self._resolved_smart_services()
        if kind == "smart_submission":
            prepared = prepare([paper_id], orchestrator=self.orchestrator)
            queued = [
                str(item)
                for item in [
                    *(prepared.get("queued", []) if isinstance(prepared.get("queued"), list) else []),
                    *(prepared.get("resumed", []) if isinstance(prepared.get("resumed"), list) else []),
                ]
                if str(item)
            ]
            if paper_id not in queued:
                skipped = prepared.get("skipped")
                reason = (
                    str(skipped.get(paper_id) or "").strip()
                    if isinstance(skipped, Mapping)
                    else ""
                )
                raise FullPipelineError(
                    "FULL_PIPELINE_SMART_PREPARE_FAILED",
                    reason or "智能投稿未把论文加入在线队列。",
                )
            started = smart_start([paper_id])
            if started.get("ok") is False:
                raise FullPipelineError(
                    "FULL_PIPELINE_SMART_START_FAILED",
                    str(started.get("error") or started.get("message") or "智能投稿 SDK 未启动。"),
                )
            return {
                "ok": True,
                "prepared": _compact_result(prepared),
                "started": _compact_result(started),
            }
        if kind == "smart_wakeup":
            started = smart_start([paper_id])
            if started.get("ok") is False:
                raise FullPipelineError(
                    "FULL_PIPELINE_SMART_WAKEUP_FAILED",
                    str(started.get("error") or started.get("message") or "智能投稿 SDK 未重新唤醒。"),
                )
            return {"ok": True, "started": _compact_result(started)}
        raise FullPipelineError(
            "FULL_PIPELINE_ACTION_UNKNOWN",
            f"未知全流程动作：{kind}",
        )

    def _record_action_result(
        self,
        action: Mapping[str, Any],
        *,
        result: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        now_epoch = time.time()

        def mutate(state: dict[str, Any]) -> None:
            batch = state.get("batches", {}).get(str(action.get("batch_id") or ""))
            if not isinstance(batch, dict):
                return
            slots = batch.get("slots")
            if not isinstance(slots, list):
                return
            slot = next(
                (
                    item
                    for item in slots
                    if isinstance(item, dict)
                    and int(item.get("slot") or 0) == int(action.get("slot") or 0)
                ),
                None,
            )
            if not isinstance(slot, dict):
                return
            kind = str(action.get("action") or "")
            marker = slot.setdefault("dispatches", {}).setdefault(kind, {})
            if str(marker.get("claim_id") or "") != str(action.get("claim_id") or ""):
                return
            if error is None:
                marker.update(
                    {
                        "state": "succeeded",
                        "finished_at": now_iso(),
                        "finished_at_epoch": now_epoch,
                        "result": _compact_result(result or {}),
                        "last_error": "",
                    }
                )
                slot["last_error"] = ""
                event = "full_pipeline_handoff_succeeded"
            else:
                code = str(getattr(error, "code", type(error).__name__))
                attempts = int(marker.get("attempts") or 1)
                retry_delay = (5, 30, 120)[min(max(attempts - 1, 0), 2)]
                marker.update(
                    {
                        "state": "failed",
                        "finished_at": now_iso(),
                        "finished_at_epoch": now_epoch,
                        "last_error": f"{code}: {error}",
                        "next_retry_at_epoch": now_epoch + retry_delay,
                    }
                )
                slot["last_error"] = f"{code}: {error}"
                if attempts >= _MAX_STAGE_DISPATCH_ATTEMPTS:
                    slot["status"] = "needs_attention"
                    slot["detail"] = str(error)
                    batch["status"] = "needs_attention"
                    batch["last_error"] = f"{code}: {error}"
                event = "full_pipeline_handoff_failed"
            slot["updated_at"] = now_iso()
            batch["updated_at"] = now_iso()
            _append_history(
                state,
                {
                    "event": event,
                    "batch_id": str(action.get("batch_id") or ""),
                    "paper_id": str(action.get("paper_id") or ""),
                    "action": kind,
                    "code": "" if error is None else str(getattr(error, "code", type(error).__name__)),
                },
            )

        self.store.mutate(mutate)

    def _restore_batch_limits(self, batch_id: str) -> dict[str, Any]:
        state = self.store.load()
        batch = state.get("batches", {}).get(batch_id)
        if not isinstance(batch, Mapping) or bool(batch.get("limits_restored")):
            return {"ok": True, "already_restored": True}
        stage_result = self._restore_stage_limits(
            batch.get("stage_limits") if isinstance(batch.get("stage_limits"), Mapping) else {}
        )
        smart_token = batch.get("smart_policy")
        smart_result: dict[str, Any] = {"ok": True, "skipped": True}
        if isinstance(smart_token, Mapping):
            _, _, _, smart_restore = self._resolved_smart_services()
            smart_result = smart_restore(
                previous=(smart_token.get("previous") or {}),
                expected=(smart_token.get("expected") or {}),
            )

        def record(state_value: dict[str, Any]) -> None:
            target = state_value.get("batches", {}).get(batch_id)
            if isinstance(target, dict):
                target["limits_restored"] = True
                target["limit_restore_result"] = {
                    "stages": stage_result,
                    "smart_submission": _compact_result(smart_result),
                }
                target["updated_at"] = now_iso()
            if str(state_value.get("active_batch_id") or "") == batch_id:
                state_value["active_batch_id"] = ""
            _append_history(
                state_value,
                {"event": "full_pipeline_limits_restored", "batch_id": batch_id},
            )

        self.store.mutate(record)
        return {"ok": True, "stages": stage_result, "smart_submission": smart_result}

    def tick(self) -> dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": True, "busy": True}
        try:
            state = self.store.load()
            batch_id = str(state.get("active_batch_id") or "")
            batch = state.get("batches", {}).get(batch_id)
            if not isinstance(batch, Mapping):
                return {"ok": True, "active": False}
            status = str(batch.get("status") or "")
            if status == "paused":
                return {"ok": True, "active": True, "paused": True, "batch_id": batch_id}
            if status == "completed":
                restore = self._restore_batch_limits(batch_id)
                return {"ok": True, "active": False, "completed": True, "restore": restore}

            writing_state = self.orchestrator.adapters["writing"].load_state()
            unified = self.orchestrator.sync()
            smart_state = read_json(self.smart_state_path, {})
            if not isinstance(smart_state, Mapping):
                smart_state = {}
            claimed_batch_id, actions, completed = self._claim_actions(
                writing_state if isinstance(writing_state, Mapping) else {},
                unified if isinstance(unified, Mapping) else {},
                smart_state,
            )
            for action in actions:
                try:
                    result = self._execute_action(action)
                except Exception as exc:
                    self._record_action_result(action, error=exc)
                else:
                    self._record_action_result(action, result=result)
            if completed:
                restore = self._restore_batch_limits(claimed_batch_id)
            else:
                restore = {}
            return {
                "ok": True,
                "active": not completed,
                "batch_id": claimed_batch_id,
                "claimed_count": len(actions),
                "completed": completed,
                "restore": restore,
            }
        finally:
            self._tick_lock.release()

    def pause(self) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> str:
            batch_id = str(state.get("active_batch_id") or "")
            batch = state.get("batches", {}).get(batch_id)
            if not isinstance(batch, dict):
                return ""
            batch["status"] = "paused"
            batch["paused_at"] = now_iso()
            batch["updated_at"] = now_iso()
            _append_history(
                state,
                {"event": "full_pipeline_handoffs_paused", "batch_id": batch_id},
            )
            return batch_id

        batch_id = self.store.mutate(mutate)
        return {"ok": bool(batch_id), "batch_id": batch_id, "status": "paused" if batch_id else "idle"}

    def record_runtime_error(self, error: BaseException) -> None:
        code = str(getattr(error, "code", type(error).__name__))
        message = f"{code}: {error}"[:1000]

        def mutate(state: dict[str, Any]) -> None:
            batch_id = str(state.get("active_batch_id") or "")
            batch = state.get("batches", {}).get(batch_id)
            if not isinstance(batch, dict):
                return
            batch["status"] = "needs_attention"
            batch["last_error"] = message
            batch["updated_at"] = now_iso()
            _append_history(
                state,
                {
                    "event": "full_pipeline_runtime_error",
                    "batch_id": batch_id,
                    "code": code,
                    "message": str(error)[:500],
                },
            )

        self.store.mutate(mutate)

    def resume(self) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> str:
            batch_id = str(state.get("active_batch_id") or "")
            batch = state.get("batches", {}).get(batch_id)
            if not isinstance(batch, dict):
                return ""
            if str(batch.get("status") or "") == "completed":
                return ""
            batch["status"] = "running"
            batch["resumed_at"] = now_iso()
            batch["updated_at"] = now_iso()
            _append_history(
                state,
                {"event": "full_pipeline_handoffs_resumed", "batch_id": batch_id},
            )
            return batch_id

        batch_id = self.store.mutate(mutate)
        return {"ok": bool(batch_id), "batch_id": batch_id, "status": "running" if batch_id else "idle"}

    def status(self, *, batch_id: str = "") -> dict[str, Any]:
        state = self.store.load()
        target_id = str(batch_id or state.get("active_batch_id") or "")
        if not target_id:
            batches = state.get("batches", {})
            if isinstance(batches, Mapping) and batches:
                target_id = str(next(reversed(batches)))
        batch = state.get("batches", {}).get(target_id)
        if not isinstance(batch, Mapping):
            return {
                "ok": True,
                "active": False,
                "status": "idle",
                "status_label": "未启动",
                "summary": "一键全流程：未启动",
                "batch_id": "",
                "papers": [],
                "state_path": str(self.store.path),
            }
        slots = [dict(item) for item in batch.get("slots", []) if isinstance(item, Mapping)]
        phase_counts: dict[str, int] = {}
        for slot in slots:
            phase = str(slot.get("phase") or "writing")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        completed_count = phase_counts.get("completed", 0)
        progress = (
            round(sum(int(item.get("progress_percent") or 0) for item in slots) / len(slots))
            if slots
            else 0
        )
        status_token = str(batch.get("status") or "")
        labels = {
            "preparing": "正在建立写作任务",
            "running": "运行中",
            "needs_attention": "部分论文需处理",
            "paused": "自动交接已暂停",
            "completed": "全部已投稿",
            "failed_start": "启动失败（可重试）",
        }
        label = labels.get(status_token, status_token or "需核对")
        return {
            "ok": True,
            "active": status_token in FULL_PIPELINE_ACTIVE_STATUSES,
            "status": status_token,
            "status_label": label,
            "summary": (
                f"一键全流程：{label}｜{completed_count}/{int(batch.get('requested_count') or len(slots))} 已投稿"
                f"｜总体约 {progress}%"
            ),
            "batch_id": target_id,
            "requested_count": int(batch.get("requested_count") or 0),
            "parallel_count": int(batch.get("parallel_count") or 0),
            "progress_percent": progress,
            "completed_count": completed_count,
            "phase_counts": phase_counts,
            "papers": slots,
            "created_at": str(batch.get("created_at") or ""),
            "updated_at": str(batch.get("updated_at") or ""),
            "completed_at": str(batch.get("completed_at") or ""),
            "last_error": str(batch.get("last_error") or ""),
            "limits_restored": bool(batch.get("limits_restored")),
            "state_path": str(self.store.path),
            "revision": int(state.get("revision") or 0),
        }

    def resume_active_stage_workers(
        self,
        status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Wake the exact stage workers needed by one persisted active batch.

        The coordinator state is durable, while SDK workers are intentionally
        process-local threads.  Restoring only the coordinator poller after an
        EXE/process restart leaves queued or safely-paused stage work visible
        but idle.  This method reconnects those two lifecycles without creating
        new papers or changing any workflow/prompt configuration.
        """

        snapshot = dict(status or self.status())
        if not snapshot.get("active") or str(snapshot.get("status") or "") == "paused":
            return {"ok": True, "status": "inactive", "stages": {}, "errors": {}}
        slots = [dict(item) for item in snapshot.get("papers", []) if isinstance(item, Mapping)]
        runnable = [
            item
            for item in slots
            if str(item.get("phase") or "") not in {"", "completed"}
            and str(item.get("status") or "").strip().casefold() != "needs_attention"
        ]
        phases = {str(item.get("phase") or "") for item in runnable}
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}

        if "writing" in phases:
            try:
                adapter = self.orchestrator.adapters["writing"]
                starter = getattr(adapter, "resume", None)
                if not callable(starter):
                    starter = getattr(adapter, "start", None)
                if not callable(starter):
                    raise FullPipelineError(
                        "FULL_PIPELINE_WRITING_RUNTIME_UNAVAILABLE",
                        "writing adapter does not expose resume/start",
                    )
                results["writing"] = _compact_result(starter())
            except Exception as exc:
                errors["writing"] = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"

        for phase, adapter_name in (("figures", "figures"), ("journal_author", "journal_author")):
            paper_ids = list(
                dict.fromkeys(
                    str(item.get("paper_id") or "").strip()
                    for item in runnable
                    if str(item.get("phase") or "") == phase and str(item.get("paper_id") or "").strip()
                )
            )
            if not paper_ids:
                continue
            try:
                adapter = self.orchestrator.adapters[adapter_name]
                starter = getattr(adapter, "resume_papers", None)
                if not callable(starter):
                    starter = getattr(adapter, "start", None)
                    if not callable(starter):
                        raise FullPipelineError(
                            f"FULL_PIPELINE_{adapter_name.upper()}_RUNTIME_UNAVAILABLE",
                            f"{adapter_name} adapter does not expose resume_papers/start",
                        )
                    stage_result = starter()
                else:
                    stage_result = starter(paper_ids)
                results[phase] = _compact_result(stage_result)
            except Exception as exc:
                errors[phase] = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"

        smart_ids = list(
            dict.fromkeys(
                str(item.get("paper_id") or "").strip()
                for item in runnable
                if str(item.get("phase") or "") == "smart_submission"
                and str(item.get("paper_id") or "").strip()
            )
        )
        if smart_ids:
            try:
                _, smart_start, _, _ = self._resolved_smart_services()
                results["smart_submission"] = _compact_result(smart_start(smart_ids))
            except Exception as exc:
                errors["smart_submission"] = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"

        return {
            "ok": not errors,
            "status": "resumed" if results else ("failed" if errors else "nothing_to_resume"),
            "stages": results,
            "errors": errors,
        }


class FullPipelineRuntime:
    def __init__(
        self,
        coordinator: FullPipelineCoordinator,
        *,
        poll_seconds: float = 2.0,
        error_retry_seconds: float = 5.0,
    ) -> None:
        self.coordinator = coordinator
        self.poll_seconds = max(float(poll_seconds), 0.2)
        self.error_retry_seconds = max(float(error_retry_seconds), 0.05)
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _entry(self) -> None:
        consecutive_errors = 0
        try:
            while not self._stop.is_set():
                try:
                    result = self.coordinator.tick()
                except Exception as exc:
                    consecutive_errors += 1
                    LOGGER.error(
                        "Full-pipeline coordinator tick failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    try:
                        self.coordinator.record_runtime_error(exc)
                    except Exception:
                        LOGGER.exception("Failed to persist full-pipeline runtime error")
                    retry_seconds = min(
                        self.error_retry_seconds * consecutive_errors,
                        30.0,
                    )
                    self._stop.wait(retry_seconds)
                    continue
                consecutive_errors = 0
                if not result.get("active"):
                    return
                if result.get("paused"):
                    return
                self._stop.wait(self.poll_seconds)
        finally:
            with self._thread_lock:
                self._thread = None

    def start_background(self) -> dict[str, Any]:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": True, "already_running": True}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._entry,
                name="unified-full-pipeline",
                daemon=True,
            )
            self._thread.start()
            return {"ok": True, "started": True}

    def start_new(self, parallel_count: int) -> dict[str, Any]:
        from .writing_prompt_schemes import is_undergraduate
        workflow_store = getattr(getattr(self.coordinator, "orchestrator", None), "writing_workflow_store", None)
        if workflow_store is not None and is_undergraduate(workflow_store.active_snapshot()):
            raise ValueError("本科方案请使用新建写作任务；不会进入图表、选刊和投稿全流程。")
        result = self.coordinator.recover_failed_start(parallel_count)
        if result is None:
            result = self.coordinator.start_batch(parallel_count)
        runtime = self.start_background()
        return {**result, "runtime": runtime}

    def resume_if_active(self) -> dict[str, Any]:
        status = self.coordinator.status()
        if status.get("active") and status.get("status") != "paused":
            workers = self.coordinator.resume_active_stage_workers(status)
            return {
                "status": self.coordinator.status(),
                "workers": workers,
                "runtime": self.start_background(),
            }
        return {"status": status, "runtime": {"ok": True, "started": False}}

    def pause(self) -> dict[str, Any]:
        result = self.coordinator.pause()
        self._stop.set()
        return result

    def resume(self) -> dict[str, Any]:
        result = self.coordinator.resume()
        if result.get("ok"):
            result["runtime"] = self.start_background()
        return result

    def shutdown(self) -> None:
        """Stop only this process's poller; persisted batches resume next launch."""

        self._stop.set()

    def status(self) -> dict[str, Any]:
        status = self.coordinator.status()
        with self._thread_lock:
            running = bool(self._thread is not None and self._thread.is_alive())
        status["runtime_running"] = running
        return status


_RUNTIME_LOCK = threading.Lock()
_RUNTIME: FullPipelineRuntime | None = None


def get_full_pipeline_runtime(orchestrator: Any) -> FullPipelineRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = FullPipelineRuntime(FullPipelineCoordinator(orchestrator))
        else:
            _RUNTIME.coordinator.orchestrator = orchestrator
        return _RUNTIME


def full_pipeline_status() -> dict[str, Any]:
    """Read-only projection usable before the runtime is constructed."""

    if _RUNTIME is not None:
        return _RUNTIME.status()
    return _status_from_store(FullPipelineStore())


def _status_from_store(store: FullPipelineStore) -> dict[str, Any]:
    """Small helper used by read-only UI refreshes without an orchestrator."""

    state = store.load()
    active_id = str(state.get("active_batch_id") or "")
    batches = state.get("batches", {}) if isinstance(state.get("batches"), Mapping) else {}
    if not active_id and batches:
        active_id = str(next(reversed(batches)))
    batch = batches.get(active_id)
    if not isinstance(batch, Mapping):
        return {
            "ok": True,
            "active": False,
            "status": "idle",
            "status_label": "未启动",
            "summary": "一键全流程：未启动",
            "batch_id": "",
            "papers": [],
            "state_path": str(store.path),
            "runtime_running": False,
        }
    slots = [dict(item) for item in batch.get("slots", []) if isinstance(item, Mapping)]
    completed_count = sum(1 for item in slots if str(item.get("phase") or "") == "completed")
    progress = (
        round(sum(int(item.get("progress_percent") or 0) for item in slots) / len(slots))
        if slots
        else 0
    )
    token = str(batch.get("status") or "")
    label = {
        "preparing": "正在建立写作任务",
        "running": "运行中",
        "needs_attention": "部分论文需处理",
        "paused": "自动交接已暂停",
        "completed": "全部已投稿",
        "failed_start": "启动失败（可重试）",
    }.get(token, token or "需核对")
    return {
        "ok": True,
        "active": token in FULL_PIPELINE_ACTIVE_STATUSES,
        "status": token,
        "status_label": label,
        "summary": (
            f"一键全流程：{label}｜{completed_count}/{int(batch.get('requested_count') or len(slots))} 已投稿"
            f"｜总体约 {progress}%"
        ),
        "batch_id": active_id,
        "requested_count": int(batch.get("requested_count") or 0),
        "parallel_count": int(batch.get("parallel_count") or 0),
        "progress_percent": progress,
        "completed_count": completed_count,
        "papers": slots,
        "last_error": str(batch.get("last_error") or ""),
        "state_path": str(store.path),
        "runtime_running": False,
    }
