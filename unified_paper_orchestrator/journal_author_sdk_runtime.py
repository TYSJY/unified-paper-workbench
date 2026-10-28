from __future__ import annotations

import asyncio
import copy
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from openai_codex import AsyncCodex, CodexConfig

from . import config
from .automation import SchedulerAutomationController
from .codex_sdk_account import CODEX_SDK_HOME, CodexSDKAccountService
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .journal_author_sdk_dispatch import (
    JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA,
    JournalAuthorPaperPlan,
    JournalAuthorSDKDispatcher,
)
from .journal_author_output_contract import validate_journal_author_output
from .journal_submission_policy import finalize_journal_submission_policy
from .journal_author_workflow import (
    default_journal_author_workflow_snapshot,
    normalize_journal_author_workflow_snapshot,
)
from .sdk_model_catalog import catalog_provenance, read_sdk_models
from .windows_subprocess import configure_openai_codex_no_window
from .codex_runtime import runtime_launch_options


JOURNAL_AUTHOR_SDK_RUNTIME_SCHEMA = "unified-paper-orchestrator/journal-author-sdk-runtime/v1"
_RETRYABLE_JOURNAL_AUTHOR_SDK_BLOCKERS = frozenset(
    {
        "JOURNAL_AUTHOR_EMPTY_COMPLETED_TURN",
        "JOURNAL_AUTHOR_TURN_NOT_COMPLETED",
        "JOURNAL_AUTHOR_TURN_RESULT_UNCERTAIN",
    }
)


class JournalAuthorSDKRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class JournalAuthorSDKRuntime:
    """Program-owned journal/author scheduler; AI only executes paper work."""

    def __init__(
        self,
        project: Mapping[str, Any] | None = None,
        *,
        account_service: Any | None = None,
        codex_home: str | Path | None = None,
        journal_path: str | Path | None = None,
        dispatch_lock_path: str | Path | None = None,
        pipeline_lock_path: str | Path | None = None,
        dispatcher_factory: Callable[..., Any] = JournalAuthorSDKDispatcher,
        codex_factory: Callable[[], Any] | None = None,
        legacy_automation_probe: Callable[[], Any] | None = None,
        model: str | None = None,
    ) -> None:
        project = dict(project or config.LEGACY_PROJECTS["journal_author"])
        self.root = Path(project["root"]).expanduser().resolve()
        self.papers_dir = Path(project["papers_dir"]).expanduser().resolve()
        self.pipeline_path = Path(project["state_path"]).expanduser().resolve()
        self.codex_home = Path(codex_home or CODEX_SDK_HOME).expanduser().resolve()
        self.journal_path = Path(journal_path or config.JOURNAL_AUTHOR_SDK_JOURNAL_PATH).expanduser().resolve()
        self.dispatch_lock_path = Path(dispatch_lock_path or config.JOURNAL_AUTHOR_SDK_LOCK_PATH).expanduser().resolve()
        self.pipeline_lock_path = Path(pipeline_lock_path or config.JOURNAL_AUTHOR_PIPELINE_LOCK_PATH).expanduser().resolve()
        self.account_service = account_service or CodexSDKAccountService(codex_home=self.codex_home)
        self.dispatcher_factory = dispatcher_factory
        self._injected_codex_factory = codex_factory
        self._legacy_automation_probe = legacy_automation_probe
        self._automation = SchedulerAutomationController()
        self.model = str(config.JOURNAL_AUTHOR_CODEX_MODEL if model is None else model).strip()
        if not self.model:
            raise ValueError("journal-author Codex model must not be empty")
        self._background_lock = threading.Lock()
        self._background_thread: threading.Thread | None = None
        self._wakeup_serial = 0
        self._priority_paper_ids: set[str] = set()
        self._background_run_all = False
        self._last_result: dict[str, Any] | None = None
        self._last_error = ""

    def _codex_factory(self) -> Any:
        if self._injected_codex_factory is not None:
            return self._injected_codex_factory()
        configure_openai_codex_no_window()
        self.codex_home.mkdir(parents=True, exist_ok=True)
        return AsyncCodex(
            CodexConfig(
                cwd=str(self.root),
                **runtime_launch_options({"CODEX_HOME": str(self.codex_home)}),
                config_overrides=(
                    "features.multi_agent=false",
                    "features.multi_agent_v2=false",
                    "agents.max_threads=1",
                    "agents.max_depth=1",
                ),
                client_name="unified_paper_orchestrator_journal_author",
                client_title="Unified Paper Orchestrator Journal Author",
            )
        )

    def ensure_ready(self) -> dict[str, Any]:
        verified = getattr(self.account_service, "verified_status", None)
        status = verified() if callable(verified) else self.account_service.status()
        if not isinstance(status, dict) or not status.get("ok"):
            raise JournalAuthorSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_STATUS_UNAVAILABLE",
                "无法读取本程序的 Codex 账号状态；未启动选刊作者任务。",
            )
        if not status.get("isolated_home"):
            raise JournalAuthorSDKRuntimeError(
                "CODEX_SDK_HOME_NOT_ISOLATED",
                "选刊作者 SDK 未使用程序专属 CODEX_HOME",
            )
        if not status.get("authenticated"):
            raise JournalAuthorSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED",
                "请先在“账号与站点”中连接本程序使用的 ChatGPT/Codex 账号。",
            )
        return dict(status)

    require_ready = ensure_ready

    def list_models(self) -> dict[str, Any]:
        account = self.ensure_ready()

        async def fetch() -> list[dict[str, Any]]:
            async with self._codex_factory() as codex:
                method = getattr(codex, "models", None)
                if not callable(method):
                    raise JournalAuthorSDKRuntimeError(
                        "JOURNAL_AUTHOR_MODEL_CATALOG_UNSUPPORTED",
                        "当前 Codex SDK 不支持读取模型列表，请更新程序内置 SDK。",
                    )
                return await read_sdk_models(codex)

        models = asyncio.run(fetch())
        if not models:
            raise JournalAuthorSDKRuntimeError(
                "JOURNAL_AUTHOR_MODEL_CATALOG_EMPTY",
                "当前账号没有返回可用模型。",
            )
        return {
            "ok": True,
            "stage": "journal_author",
            "configured_model": self.model,
            "models": models,
            "catalog": catalog_provenance(self.codex_home),
            "account": account,
        }

    def _legacy_status(self) -> dict[str, Any]:
        if self._legacy_automation_probe is not None:
            value = self._legacy_automation_probe()
            if isinstance(value, bool):
                return {"safe": value, "active": [] if value else [{"id": "unknown"}]}
            if isinstance(value, dict):
                return dict(value)
            return {"safe": False, "active": [], "error": "invalid_probe"}
        try:
            statuses = self._automation.read_statuses("journal_author")
        except Exception as exc:
            return {"safe": False, "active": [], "error": type(exc).__name__}
        checked = [dict(item) for item in statuses if isinstance(item, dict)]
        active = [
            item
            for item in checked
            if item.get("exists", True) and str(item.get("status") or "").upper() == "ACTIVE"
        ]
        unknown = [
            item
            for item in checked
            if item.get("exists", True)
            and str(item.get("status") or "").upper() not in {"ACTIVE", "PAUSED"}
        ]
        return {"safe": not active and not unknown, "active": active, "unknown": unknown, "checked": checked}

    def _require_legacy_quiescent(self) -> dict[str, Any]:
        status = self._legacy_status()
        if not status.get("safe"):
            raise JournalAuthorSDKRuntimeError(
                "JOURNAL_AUTHOR_LEGACY_AUTOMATION_ACTIVE",
                "旧选刊作者 Automation 仍在运行或状态不可确认；为避免重复派发，V2 拒绝启动。",
            )
        return status

    def _load_pipeline(self) -> dict[str, Any]:
        state = read_json(self.pipeline_path, {})
        if not isinstance(state, dict):
            raise JournalAuthorSDKRuntimeError(
                "JOURNAL_AUTHOR_PIPELINE_INVALID",
                "journal-author pipeline state must be an object",
            )
        if not isinstance(state.get("papers", {}), dict):
            state["papers"] = {}
        if not isinstance(state.get("paper_queue", []), list):
            state["paper_queue"] = []
        if not isinstance(state.get("commands", []), list):
            state["commands"] = []
        if not isinstance(state.get("settings", {}), dict):
            state["settings"] = {}
        return state

    def _mutate_pipeline(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with FileLock(self.pipeline_lock_path):
            state = self._load_pipeline()
            result = callback(state)
            state["updated_at"] = now_iso()
            write_json_atomic(self.pipeline_path, state)
            return result

    @staticmethod
    def _parallel_limit(state: Mapping[str, Any]) -> int:
        settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
        raw = settings.get("max_parallel_workers", config.JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = config.JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS
        return max(1, min(config.JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS, value))

    @staticmethod
    def _requeue_selected_recoverable_failures(
        state: dict[str, Any],
        selected_ids: set[str],
    ) -> list[str]:
        """Wake technical SDK failures without creating a second request.

        Business blockers remain stopped.  For an uncertain/empty SDK result,
        the existing request, fixed thread, and turn IDs are retained so the
        dispatcher can reconcile the authoritative history before deciding
        whether another Step must be sent.
        """

        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        queue = [str(value) for value in state.get("paper_queue", []) if str(value)]
        commands = state.get("commands", []) if isinstance(state.get("commands"), list) else []
        recovered: list[str] = []
        for paper_id in sorted(selected_ids, key=str.casefold):
            record = papers.get(paper_id)
            if not isinstance(record, dict):
                continue
            blocker = str(record.get("blocker") or "")
            if blocker not in _RETRYABLE_JOURNAL_AUTHOR_SDK_BLOCKERS:
                continue
            if blocker == "JOURNAL_AUTHOR_TURN_NOT_COMPLETED":
                try:
                    terminal_retries = int(
                        record.get("sdk_terminal_retry_attempts") or 0
                    )
                except (TypeError, ValueError):
                    terminal_retries = 0
                if terminal_retries >= 1:
                    continue
                record["sdk_terminal_retry_attempts"] = terminal_retries + 1
            request_id = str(record.get("sdk_request_id") or "")
            if not request_id:
                continue
            if paper_id not in queue:
                queue.append(paper_id)
            record.update(
                {
                    "workflow_status": "queued",
                    "status": "QUEUED",
                    "last_result_status": "RECOVERY_QUEUED",
                    "last_result_message": "正在从原固定会话恢复结果；不会重复建立转投请求",
                    "sdk_recovery_requested_at": now_iso(),
                }
            )
            record.pop("blocker", None)
            record.pop("blocker_message", None)
            for command in commands:
                if not isinstance(command, dict):
                    continue
                if str(command.get("id") or "") != request_id:
                    continue
                if str(command.get("action") or "") != "run_pipeline_02_03":
                    continue
                if str(command.get("status") or "") == "blocked":
                    command["status"] = "pending"
                    command["recovery_requested_at"] = now_iso()
                break
            recovered.append(paper_id)
        state["paper_queue"] = queue
        return recovered

    def _build_plans(
        self,
        state: Mapping[str, Any],
        selected_paper_ids: set[str] | None,
        priority_paper_ids: set[str] | None = None,
    ) -> list[JournalAuthorPaperPlan]:
        papers = state.get("papers", {}) if isinstance(state.get("papers", {}), Mapping) else {}
        queue = [str(value) for value in state.get("paper_queue", []) if str(value)]
        ordered = list(dict.fromkeys(queue))
        for command in state.get("commands", []):
            if not isinstance(command, Mapping):
                continue
            if str(command.get("status") or "") != "pending" or str(command.get("action") or "") != "run_pipeline_02_03":
                continue
            paper_id = str(command.get("paper_id") or "")
            if paper_id and paper_id not in ordered:
                ordered.append(paper_id)
        priorities = {str(value) for value in priority_paper_ids or set() if str(value)}
        ordered.sort(key=lambda value: (0 if value in priorities else 1, value.casefold()))
        settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
        paused = {str(value) for value in settings.get("sdk_paused_paper_ids", []) if str(value)}
        plans: list[JournalAuthorPaperPlan] = []
        for paper_id in ordered:
            if selected_paper_ids is not None and paper_id not in selected_paper_ids:
                continue
            if paper_id in paused:
                continue
            record = papers.get(paper_id)
            if not isinstance(record, Mapping):
                continue
            request_id = str(record.get("sdk_request_id") or "").strip()
            latest_command: Mapping[str, Any] | None = None
            for command in state.get("commands", []):
                if (
                    isinstance(command, Mapping)
                    and str(command.get("paper_id") or "") == paper_id
                    and str(command.get("action") or "") == "run_pipeline_02_03"
                    and str(command.get("status") or "") == "pending"
                ):
                    latest_command = command
            if not request_id and latest_command is not None:
                request_id = str(latest_command.get("id") or "")
            if not request_id:
                continue
            params = latest_command.get("params", {}) if isinstance(latest_command, Mapping) else {}
            params = params if isinstance(params, Mapping) else {}
            workflow = normalize_journal_author_workflow_snapshot(
                record.get("workflow_snapshot")
                or params.get("workflow_snapshot")
                or default_journal_author_workflow_snapshot()
            )
            plans.append(
                JournalAuthorPaperPlan(
                    paper_id=paper_id,
                    request_id=request_id,
                    workflow_snapshot=workflow,
                    reuse_valid_artifacts=bool(record.get("reuse_valid_artifacts", True)),
                    start_step=int(
                        record.get("run_from_position")
                        or params.get("run_from_position")
                        or 1
                    ),
                    run_until_step=int(
                        record.get("run_until_position")
                        or params.get("run_until_position")
                        or workflow.get("step_count")
                        or 1
                    ),
                    run_mode=str(record.get("run_mode") or params.get("run_mode") or "full"),
                )
            )
        return plans

    def _progress_callback(self, event: str, paper_id: str, snapshot: Mapping[str, Any]) -> None:
        snapshot_copy = copy.deepcopy(dict(snapshot))

        def project(state: dict[str, Any]) -> None:
            papers = state.setdefault("papers", {})
            record = papers.setdefault(paper_id, {})
            if not isinstance(record, dict):
                record = {}
                papers[paper_id] = record
            status = str(snapshot_copy.get("status") or "unknown")
            snapshot_request = str(snapshot_copy.get("request_id") or "")
            current_request = str(record.get("sdk_request_id") or "")
            superseded = bool(snapshot_request and current_request and snapshot_request != current_request)
            record.update(
                {
                    "paper_id": paper_id,
                    "thread_id": str(snapshot_copy.get("thread_id") or ""),
                    "thread_title": str(snapshot_copy.get("thread_name") or ""),
                    "sdk_runtime_event": event,
                    "workflow_snapshot": copy.deepcopy(snapshot_copy.get("workflow_snapshot") or record.get("workflow_snapshot") or {}),
                    "workflow_version_id": str(snapshot_copy.get("workflow_version_id") or record.get("workflow_version_id") or ""),
                    "updated_at": now_iso(),
                }
            )
            if superseded:
                record["workflow_status"] = "queued"
                record["last_result_message"] = "上一个 SDK 请求已结束；较新的选刊作者请求仍保留排队"
                return
            record["sdk_request_id"] = snapshot_request or current_request
            record["sdk_current_step"] = int(snapshot_copy.get("active_step") or 0)
            record["sdk_completed_step"] = int(snapshot_copy.get("completed_step") or 0)
            record["run_from_position"] = int(snapshot_copy.get("start_step") or record.get("run_from_position") or 1)
            record["run_until_position"] = int(snapshot_copy.get("run_until_step") or record.get("run_until_position") or 1)
            record["run_mode"] = str(snapshot_copy.get("run_mode") or record.get("run_mode") or "full")
            record["workflow_status"] = status
            record["status"] = status.upper() if status in {"queued", "running", "blocked", "paused"} else record.get("status", "")
            record["step_done"] = int(
                snapshot_copy.get("legacy_step_done")
                or (3 if status in {"complete", "needs_user"} else 2 if status == "selection_complete" else 1)
            )
            blocker = str(snapshot_copy.get("blocker") or "")
            blocker_message = str(snapshot_copy.get("blocker_message") or "")
            if blocker:
                record["blocker"] = blocker
                record["last_result_message"] = blocker_message or blocker
            else:
                record.pop("blocker", None)
                record["last_result_message"] = {
                    "queued": "已进入 V2 SDK 选刊作者队列",
                    "creating_thread": "正在创建论文固定 SDK 会话",
                    "starting": f"正在启动流程 Step {int(snapshot_copy.get('active_step') or 1)}",
                    "running": f"固定 SDK 会话正在执行流程 Step {int(snapshot_copy.get('active_step') or 1)}",
                    "complete": "选刊和投稿包已通过严格验收",
                    "selection_complete": "期刊选择已通过门禁；投稿包尚未重新生成",
                    "needs_user": "AI 流程已结束，但投稿前仍需用户处理轻微问题",
                    "paused": "已暂停；不会启动新的选刊任务",
                }.get(status, str(record.get("last_result_message") or ""))
            if status in {"complete", "selection_complete", "needs_user"}:
                record["package_status"] = str(snapshot_copy.get("package_status") or "")
                record["last_result_status"] = (
                    "SELECTION_COMPLETE"
                    if status == "selection_complete"
                    else str(snapshot_copy.get("package_status") or ("NEEDS_USER" if status == "needs_user" else "COMPLETE"))
                )
                record["last_completed_at"] = now_iso()
                if status == "complete":
                    record["last_success_at"] = now_iso()
            elif status == "blocked":
                record["last_result_status"] = "BLOCKED"
                record["last_error_at"] = now_iso()
                record["last_error_message"] = blocker_message or blocker
            elif status in {"starting", "running", "waiting_event", "creating_thread"}:
                record["last_result_status"] = "RUNNING"
            queue = [str(value) for value in state.get("paper_queue", [])]
            terminal = status in {"complete", "selection_complete", "needs_user", "blocked"}
            if terminal:
                state["paper_queue"] = [value for value in queue if value != paper_id]
            elif paper_id not in queue:
                queue.append(paper_id)
                state["paper_queue"] = queue
            for command in state.get("commands", []):
                if not isinstance(command, dict) or str(command.get("paper_id") or "") != paper_id:
                    continue
                if str(command.get("action") or "") != "run_pipeline_02_03":
                    continue
                if terminal and str(command.get("status") or "") == "pending":
                    command.update(
                        {
                            "status": "blocked" if status == "blocked" else "done",
                            "handled_at": now_iso(),
                            "message": f"V2 SDK journal-author pipeline {status}",
                        }
                    )
            history = record.setdefault("history", [])
            if isinstance(history, list):
                history.append({"at": now_iso(), "event": f"journal_author_sdk_{event}", "status": status})
                if len(history) > 80:
                    del history[:-80]

        self._mutate_pipeline(project)

    def _should_continue(self, paper_id: str, _snapshot: Mapping[str, Any]) -> bool:
        state = self._load_pipeline()
        settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
        paused = {str(value) for value in settings.get("sdk_paused_paper_ids", []) if str(value)}
        return (
            str(settings.get("scheduling_mode") or "manual") == "auto"
            and not bool(settings.get("sdk_pause_requested"))
            and paper_id not in paused
        )

    def _run_dynamic_background(self) -> dict[str, Any]:
        initial = self._load_pipeline()
        dispatcher = self.dispatcher_factory(
            root=self.root,
            journal_path=self.journal_path,
            lock_path=self.dispatch_lock_path,
            codex_factory=self._codex_factory,
            max_parallel_papers=self._parallel_limit(initial),
            model=self.model,
            progress_callback=self._progress_callback,
            should_continue=self._should_continue,
        )

        def supply() -> list[JournalAuthorPaperPlan]:
            state = self._load_pipeline()
            settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
            if str(settings.get("scheduling_mode") or "manual") != "auto" or bool(settings.get("sdk_pause_requested")):
                return []
            with self._background_lock:
                priority = set(self._priority_paper_ids)
                run_all = self._background_run_all
            selected = None if run_all else (priority or None)
            return self._build_plans(state, selected, priority)

        result = asyncio.run(dispatcher.run_dynamic(supply))
        self._last_result = dict(result) if isinstance(result, dict) else {"result": result}
        self._last_error = ""

        def idle_if_empty(state: dict[str, Any]) -> None:
            queue = [str(value) for value in state.get("paper_queue", []) if str(value)]
            if not queue:
                settings = state.setdefault("settings", {})
                settings["scheduling_mode"] = "manual"
                settings["last_scheduler_message"] = "V2 SDK queue is idle; local dispatcher stopped."

        self._mutate_pipeline(idle_if_empty)
        return self._last_result

    def _persist_background_failure(
        self,
        error_code: str,
        *,
        priority_paper_ids: set[str],
        run_all: bool,
    ) -> None:
        """Keep a failed wakeup visible and retryable after process restart."""

        clean_code = str(error_code or "JOURNAL_AUTHOR_SDK_BACKGROUND_FAILED")[:160]

        def persist(state: dict[str, Any]) -> None:
            queue = [str(value) for value in state.get("paper_queue", []) if str(value)]
            targets = set(priority_paper_ids) or (set(queue) if run_all else set())
            settings = state.setdefault("settings", {})
            errors = settings.get("sdk_background_errors")
            if not isinstance(errors, dict):
                errors = {}
                settings["sdk_background_errors"] = errors
            papers = state.setdefault("papers", {})
            for paper_id in sorted(targets, key=str.casefold):
                record = papers.get(paper_id)
                if not isinstance(record, dict):
                    continue
                errors[paper_id] = clean_code
                record["sdk_background_error"] = clean_code
                record["sdk_background_error_at"] = now_iso()
                record["last_result_status"] = "SDK_BACKGROUND_FAILED"
                record["last_result_message"] = (
                    f"V2 SDK 后台启动失败：{clean_code}；任务仍保留在队列，可修复后继续。"
                )
                history = record.setdefault("history", [])
                if isinstance(history, list):
                    history.append(
                        {
                            "at": now_iso(),
                            "event": "journal_author_sdk_background_failed",
                            "error_code": clean_code,
                        }
                    )
                    if len(history) > 80:
                        del history[:-80]
            settings["last_sdk_background_error"] = clean_code
            settings["last_sdk_background_error_at"] = now_iso()
            settings["last_scheduler_message"] = (
                f"V2 SDK 后台启动失败：{clean_code}；任务未丢失。"
            )

        self._mutate_pipeline(persist)

    def _background_worker(self) -> None:
        while True:
            with self._background_lock:
                serial = self._wakeup_serial
                priority = set(self._priority_paper_ids)
                run_all = self._background_run_all
            try:
                self._run_dynamic_background()
            except Exception as exc:
                self._last_error = str(getattr(exc, "code", type(exc).__name__))
                try:
                    self._persist_background_failure(
                        self._last_error,
                        priority_paper_ids=priority,
                        run_all=run_all,
                    )
                except Exception:
                    # The in-memory blocker remains visible even if the state
                    # file itself is temporarily unavailable.
                    pass
            with self._background_lock:
                if serial == self._wakeup_serial:
                    self._background_thread = None
                    self._priority_paper_ids.clear()
                    self._background_run_all = False
                    return

    def start_background(
        self,
        *,
        priority_paper_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        account = self.ensure_ready()
        self._require_legacy_quiescent()
        selected = priority_paper_ids

        def enable(state: dict[str, Any]) -> None:
            settings = state.setdefault("settings", {})
            selected_ids = {str(value).strip() for value in selected or [] if str(value).strip()}
            recovered_ids = self._requeue_selected_recoverable_failures(
                state,
                selected_ids,
            )
            if recovered_ids:
                settings["last_sdk_recovery_paper_ids"] = recovered_ids
                settings["last_sdk_recovery_requested_at"] = now_iso()
            persisted_errors = settings.get("sdk_background_errors")
            if not isinstance(persisted_errors, dict):
                persisted_errors = {}
            clear_ids = set(persisted_errors) if selected is None else selected_ids
            papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
            for paper_id in clear_ids:
                persisted_errors.pop(paper_id, None)
                record = papers.get(paper_id)
                if isinstance(record, dict):
                    record.pop("sdk_background_error", None)
                    record.pop("sdk_background_error_at", None)
            if persisted_errors:
                settings["sdk_background_errors"] = persisted_errors
                settings["last_sdk_background_error"] = next(
                    iter(sorted(set(str(value) for value in persisted_errors.values())))
                )
            else:
                settings.pop("sdk_background_errors", None)
                settings.pop("last_sdk_background_error", None)
                settings.pop("last_sdk_background_error_at", None)
            paused = {str(value) for value in settings.get("sdk_paused_paper_ids", []) if str(value)}
            settings["sdk_paused_paper_ids"] = sorted(
                set() if selected is None else paused - selected_ids,
                key=str.casefold,
            )
            settings.update(
                {
                    "scheduling_mode": "auto",
                    "dispatch_mode": "sdk_event_driven",
                    "journal_author_dispatch_protocol": config.JOURNAL_AUTHOR_SDK_DISPATCH_PROTOCOL,
                    "max_parallel_workers": self._parallel_limit(state),
                    "one_action_per_run": False,
                    "sdk_pause_requested": False,
                    "updated_by_unified_start_at": now_iso(),
                }
            )

        self._mutate_pipeline(enable)
        with self._background_lock:
            self._wakeup_serial += 1
            self._priority_paper_ids.update(str(value).strip() for value in selected or [] if str(value).strip())
            if selected is None:
                self._background_run_all = True
            worker = self._background_thread
            if worker is not None and worker.is_alive():
                return {
                    "ok": True,
                    "started": False,
                    "already_running": True,
                    "status": "running",
                    "priority_paper_ids": sorted(self._priority_paper_ids),
                    "account": account,
                }
            worker = threading.Thread(
                target=self._background_worker,
                name="upo-journal-author-sdk",
                daemon=True,
            )
            self._background_thread = worker
            worker.start()
        return {
            "ok": True,
            "started": True,
            "already_running": False,
            "status": "running",
            "priority_paper_ids": sorted(str(value).strip() for value in selected or [] if str(value).strip()),
            "account": account,
        }

    def request_pause(self) -> dict[str, Any]:
        def pause(state: dict[str, Any]) -> None:
            settings = state.setdefault("settings", {})
            settings.update(
                {
                    "scheduling_mode": "manual",
                    "sdk_pause_requested": True,
                    "sdk_pause_requested_at": now_iso(),
                }
            )

        self._mutate_pipeline(pause)
        return {
            "ok": True,
            "pause_requested": True,
            "status": "pause_requested",
            "message": "当前正在执行的 Step 不会被强制中断；新的论文不再启动。",
        }

    def request_pause_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise JournalAuthorSDKRuntimeError(
                "JOURNAL_AUTHOR_PAUSE_TARGET_EMPTY",
                "至少选择一篇要暂停的选刊作者论文",
            )

        def pause_selected(state: dict[str, Any]) -> None:
            settings = state.setdefault("settings", {})
            paused = [str(value) for value in settings.get("sdk_paused_paper_ids", []) if str(value)]
            paused.extend(ids)
            settings["sdk_paused_paper_ids"] = sorted(set(paused), key=str.casefold)
            settings["sdk_selected_pause_requested_at"] = now_iso()

        self._mutate_pipeline(pause_selected)
        return {
            "ok": True,
            "pause_requested": True,
            "scope": "selected_papers",
            "paper_ids": ids,
            "status": "pause_requested",
            "message": "尚未启动的选中论文会暂停；已经运行的当前 Step 会正常结束。",
        }

    pause = request_pause

    def revalidate_output(self, paper_id: str) -> dict[str, Any]:
        target = str(paper_id or "").strip()
        if not target or Path(target).name != target:
            raise JournalAuthorSDKRuntimeError(
                "JOURNAL_AUTHOR_PAPER_ID_INVALID",
                "请选择一篇有效论文进行本地重新验收",
            )
        finalized = finalize_journal_submission_policy(
            self.root,
            target,
            compile_tex=True,
        )
        contract = validate_journal_author_output(self.root, target)
        with FileLock(self.dispatch_lock_path, stale_seconds=7 * 24 * 60 * 60):
            journal = read_json(
                self.journal_path,
                {"schema": JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []},
            )
            if not isinstance(journal, dict):
                journal = {"schema": JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []}
            journal["schema"] = JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA
            papers = journal.setdefault("papers", {})
            record = papers.setdefault(target, {})
            status = (
                "blocked"
                if not finalized.ok
                else
                "complete"
                if contract.ok and contract.package_status == "READY_TO_SUBMIT"
                else "needs_user"
                if contract.ok and contract.package_status == "NEEDS_MINOR_FIX"
                else "blocked"
            )
            workflow = (
                record.get("workflow_snapshot", {})
                if isinstance(record.get("workflow_snapshot", {}), Mapping)
                else {}
            )
            terminal_position = max(
                1,
                int(
                    record.get("run_until_step")
                    or workflow.get("step_count")
                    or record.get("active_step")
                    or 1
                ),
            )
            revalidated_at = now_iso()
            record.update(
                {
                    "paper_id": target,
                    "status": status,
                    "legacy_step_done": contract.step_done,
                    "package_status": contract.package_status,
                    "journal_decision": contract.journal_decision,
                    "local_revalidation": contract.as_dict(),
                    "local_revalidated_at": revalidated_at,
                    "updated_at": revalidated_at,
                }
            )
            if status in {"complete", "needs_user"}:
                record["active_step"] = terminal_position
                record["completed_step"] = max(
                    int(record.get("completed_step") or 0),
                    terminal_position,
                )
                step = record.setdefault("steps", {}).setdefault(str(terminal_position), {})
                step.update(
                    {
                        "state": "locally_revalidated",
                        "contract": contract.as_dict(),
                        "completed_at": revalidated_at,
                        "updated_at": revalidated_at,
                    }
                )
            if status == "blocked":
                record["blocker"] = "JOURNAL_AUTHOR_LOCAL_REVALIDATION_FAILED"
                record["blocker_message"] = (
                    "；".join(finalized.issues[:4])
                    if not finalized.ok
                    else "；".join(contract.issues[:4])
                ) or "本地验收未通过"
            elif status == "needs_user":
                record["blocker"] = "JOURNAL_AUTHOR_MINOR_FIX_REQUIRED"
                manual = [
                    value.removeprefix("需人工处理：").strip()
                    for value in contract.warnings
                    if value.startswith("需人工处理：")
                ]
                record["blocker_message"] = (
                    "；".join(manual[:4])
                    or "投稿包可用，但仍有轻微问题需要人工处理后才能进入投稿阶段"
                )
            else:
                record.pop("blocker", None)
                record.pop("blocker_message", None)
            history = journal.setdefault("history", [])
            if isinstance(history, list):
                history.append({"at": now_iso(), "event": "local_revalidated", "paper_id": target, "status": status})
                journal["history"] = history[-500:]
            journal["updated_at"] = now_iso()
            write_json_atomic(self.journal_path, journal)
            snapshot = copy.deepcopy(record)
        self._progress_callback("local_revalidated", target, snapshot)
        return {
            "ok": contract.ok,
            "status": status,
            "paper_id": target,
            "ai_called": False,
            "policy_finalization": {
                "ok": finalized.ok,
                "applied": finalized.applied,
                "repository_url": finalized.repository_url,
                "issues": list(finalized.issues),
            },
            "contract": contract.as_dict(),
        }

    def status(self, *, check_account: bool = True) -> dict[str, Any]:
        pipeline = self._load_pipeline()
        settings = pipeline.get("settings", {}) if isinstance(pipeline.get("settings", {}), Mapping) else {}
        try:
            journal = read_json(
                self.journal_path,
                {"schema": JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA, "papers": {}},
            )
        except Exception:
            journal = {"schema": JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA, "papers": {}}
        papers = journal.get("papers", {}) if isinstance(journal, Mapping) and isinstance(journal.get("papers", {}), Mapping) else {}
        counts: dict[str, int] = {}
        blocker = self._last_error or str(
            settings.get("last_sdk_background_error") or ""
        )
        for record in papers.values():
            if not isinstance(record, Mapping):
                continue
            value = str(record.get("status") or "unknown")
            counts[value] = counts.get(value, 0) + 1
            if not blocker and value == "blocked":
                blocker = str(record.get("blocker") or "JOURNAL_AUTHOR_SDK_BLOCKED")
        with self._background_lock:
            worker = self._background_thread
            background_running = worker is not None and worker.is_alive()
            priority_count = len(self._priority_paper_ids)
            run_all = self._background_run_all
        paused_ids = sorted(
            {str(value) for value in settings.get("sdk_paused_paper_ids", []) if str(value)},
            key=str.casefold,
        )
        account = self.account_service.status() if check_account else None
        active_count = sum(counts.get(value, 0) for value in ("creating_thread", "starting", "running", "waiting_event"))
        if background_running:
            status = "running"
        elif blocker:
            status = "blocked"
        elif counts.get("paused") or str(settings.get("scheduling_mode") or "") == "manual":
            status = "paused"
        elif counts.get("queued"):
            status = "ready"
        else:
            status = "idle"
        return {
            "schema": JOURNAL_AUTHOR_SDK_RUNTIME_SCHEMA,
            "ok": not blocker and (account is None or bool(account.get("ok") and account.get("authenticated"))),
            "status": status,
            "scheduling_mode": str(settings.get("scheduling_mode") or "manual"),
            "background_running": background_running,
            "priority_pending_count": priority_count,
            "dynamic_dispatch": True,
            "dispatch_scope": "all_queued" if run_all else "explicit_wakeups",
            "paused_paper_ids": paused_ids,
            "paused_paper_count": len(paused_ids),
            "configured_model": self.model,
            "max_parallel_workers": self._parallel_limit(pipeline),
            "journal_paper_count": len(papers),
            "journal_status_counts": counts,
            "status_counts": counts,
            "active_count": active_count,
            "running_count": counts.get("running", 0),
            "starting_count": counts.get("starting", 0) + counts.get("creating_thread", 0),
            "waiting_count": counts.get("waiting_event", 0) + counts.get("paused", 0) + counts.get("needs_user", 0),
            "blocked_count": counts.get("blocked", 0),
            "completed_count": counts.get("complete", 0) + counts.get("selection_complete", 0),
            "isolated_codex_home": str(self.codex_home),
            "account": account,
            "blocker": blocker,
            "last_result": self._last_result,
        }


_DEFAULT_RUNTIME: JournalAuthorSDKRuntime | None = None
_DEFAULT_RUNTIME_LOCK = threading.Lock()


def get_journal_author_sdk_runtime() -> JournalAuthorSDKRuntime:
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            _DEFAULT_RUNTIME = JournalAuthorSDKRuntime()
        return _DEFAULT_RUNTIME


def journal_author_sdk_status() -> dict[str, Any]:
    return get_journal_author_sdk_runtime().status()


__all__ = [
    "JOURNAL_AUTHOR_SDK_RUNTIME_SCHEMA",
    "JournalAuthorSDKRuntime",
    "JournalAuthorSDKRuntimeError",
    "get_journal_author_sdk_runtime",
    "journal_author_sdk_status",
]
