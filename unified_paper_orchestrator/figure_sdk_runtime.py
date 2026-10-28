from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from openai_codex import AsyncCodex, CodexConfig

from . import config
from .automation import SchedulerAutomationController
from .codex_sdk_account import CODEX_SDK_HOME, CodexSDKAccountService
from .figure_output_contract import validate_figure_step
from .figure_sdk_dispatch import (
    FIGURE_SDK_JOURNAL_SCHEMA,
    FigurePaperPlan,
    FigureRevisionPlan,
    FigureSDKDispatcher,
)
from .figure_workflow import (
    default_figure_workflow_snapshot,
    figure_workflow_step_count,
    normalize_figure_workflow_snapshot,
)
from .windows_subprocess import configure_openai_codex_no_window
from .codex_runtime import runtime_launch_options
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .sdk_model_catalog import catalog_provenance, read_sdk_models


FIGURE_SDK_RUNTIME_SCHEMA = "unified-paper-orchestrator/figure-sdk-runtime/v1"


class FigureSDKRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class FigureSDKRuntime:
    """Program-owned figure scheduler; AI is only used inside paper workers."""

    def __init__(
        self,
        project: Mapping[str, Any] | None = None,
        *,
        account_service: Any | None = None,
        codex_home: str | Path | None = None,
        journal_path: str | Path | None = None,
        dispatch_lock_path: str | Path | None = None,
        pipeline_lock_path: str | Path | None = None,
        dispatcher_factory: Callable[..., Any] = FigureSDKDispatcher,
        codex_factory: Callable[[], Any] | None = None,
        legacy_automation_probe: Callable[[], Any] | None = None,
        model: str | None = None,
    ) -> None:
        project = dict(project or config.LEGACY_PROJECTS["figures"])
        self.root = Path(project["root"]).expanduser().resolve()
        self.papers_dir = Path(project["papers_dir"]).expanduser().resolve()
        self.pipeline_path = Path(project["state_path"]).expanduser().resolve()
        self.codex_home = Path(codex_home or CODEX_SDK_HOME).expanduser().resolve()
        self.journal_path = Path(journal_path or config.FIGURE_SDK_JOURNAL_PATH).expanduser().resolve()
        self.dispatch_lock_path = Path(dispatch_lock_path or config.FIGURE_SDK_LOCK_PATH).expanduser().resolve()
        self.pipeline_lock_path = Path(pipeline_lock_path or config.FIGURE_PIPELINE_LOCK_PATH).expanduser().resolve()
        self.account_service = account_service or CodexSDKAccountService(codex_home=self.codex_home)
        self.dispatcher_factory = dispatcher_factory
        self._injected_codex_factory = codex_factory
        self._legacy_automation_probe = legacy_automation_probe
        self._automation = SchedulerAutomationController()
        self._automation_ids = tuple(str(value).strip() for value in project.get("automation_ids", []) if str(value).strip())
        self.model = str(config.FIGURE_CODEX_MODEL if model is None else model).strip()
        if not self.model:
            raise ValueError("figure Codex model must not be empty")
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
                client_name="unified_paper_orchestrator_figures",
                client_title="Unified Paper Orchestrator Figures",
            )
        )

    def ensure_ready(self) -> dict[str, Any]:
        verified_status = getattr(self.account_service, "verified_status", None)
        status = (
            verified_status()
            if callable(verified_status)
            else self.account_service.status()
        )
        if not isinstance(status, dict) or not status.get("ok"):
            raise FigureSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_STATUS_UNAVAILABLE",
                "无法读取本程序的 Codex 账号状态；未启动画图任务。",
            )
        if not status.get("isolated_home"):
            raise FigureSDKRuntimeError("CODEX_SDK_HOME_NOT_ISOLATED", "画图 SDK 未使用程序专属 CODEX_HOME")
        if not status.get("authenticated"):
            raise FigureSDKRuntimeError(
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
                    raise FigureSDKRuntimeError(
                        "FIGURE_MODEL_CATALOG_UNSUPPORTED",
                        "当前 Codex SDK 不支持读取模型列表，请更新程序内置 SDK。",
                    )
                return await read_sdk_models(codex)

        models = asyncio.run(fetch())
        if not models:
            raise FigureSDKRuntimeError(
                "FIGURE_MODEL_CATALOG_EMPTY",
                "当前账号没有返回可用模型。",
            )
        return {
            "ok": True,
            "stage": "figures",
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
            statuses = self._automation.read_statuses("figures")
        except Exception as exc:
            return {"safe": False, "active": [], "error": type(exc).__name__}
        checked = [dict(item) for item in statuses if isinstance(item, dict)]
        active = [item for item in checked if item.get("exists", True) and str(item.get("status") or "").upper() == "ACTIVE"]
        unknown = [
            item
            for item in checked
            if item.get("exists", True) and str(item.get("status") or "").upper() not in {"ACTIVE", "PAUSED"}
        ]
        return {"safe": not active and not unknown, "active": active, "unknown": unknown, "checked": checked}

    def _require_legacy_quiescent(self) -> dict[str, Any]:
        status = self._legacy_status()
        if not status.get("safe"):
            raise FigureSDKRuntimeError(
                "FIGURE_LEGACY_AUTOMATION_ACTIVE",
                "旧画图 Automation 仍在运行或状态不可确认；为避免重复派发，V2 拒绝启动。",
            )
        return status

    def _load_pipeline(self) -> dict[str, Any]:
        state = read_json(self.pipeline_path, {})
        if not isinstance(state, dict):
            raise FigureSDKRuntimeError("FIGURE_PIPELINE_INVALID", "figure pipeline state must be an object")
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
    def _pending_commands(state: Mapping[str, Any], paper_id: str, actions: set[str] | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in state.get("commands", []):
            if not isinstance(item, dict) or str(item.get("status") or "") != "pending":
                continue
            if str(item.get("paper_id") or "") != paper_id:
                continue
            if actions is not None and str(item.get("action") or "") not in actions:
                continue
            result.append(item)
        return result

    def _reconcile_legacy_projection(self) -> dict[str, Any]:
        """Close stale V1 running rows without dispatching any model work."""

        summary: dict[str, Any] = {"completed": [], "waiting_data": [], "review": []}

        def reconcile(state: dict[str, Any]) -> None:
            papers = state.setdefault("papers", {})
            queue = [str(value) for value in state.get("paper_queue", [])]
            keep: list[str] = []
            for paper_id in queue:
                record = papers.get(paper_id)
                if not isinstance(record, dict):
                    continue
                # Explicit V2 requests belong to the dispatcher, even when an
                # older submit package is still present from a prior run.
                if str(record.get("sdk_request_id") or ""):
                    keep.append(paper_id)
                    continue
                contract = validate_figure_step(self.root, paper_id, 3)
                if contract.ok:
                    record.update(
                        {
                            "status": "complete",
                            "step_done": 3,
                            "sdk_reconciled_from_artifacts": True,
                            "evidence": f"V2 严格验收通过：{contract.figure_count} 张图",
                            "updated_at": now_iso(),
                        }
                    )
                    summary["completed"].append(paper_id)
                elif contract.status == "waiting_data":
                    record.update(
                        {
                            "status": "waiting_data",
                            "step_done": 2,
                            "blocker": "FIGURE_DATA_REQUIRED",
                            "evidence": f"仍缺 {contract.data_requirement_count} 项真实数据",
                            "updated_at": now_iso(),
                        }
                    )
                    summary["waiting_data"].append(paper_id)
                else:
                    record.update(
                        {
                            "status": "migration_review",
                            "blocker": "FIGURE_SDK_MIGRATION_REQUEUE_REQUIRED",
                            "evidence": "历史任务未进入 V2 journal；点击“运行选中画图任务”按现有产物契约恢复",
                            "updated_at": now_iso(),
                        }
                    )
                    summary["review"].append(paper_id)
            state["paper_queue"] = keep
            if any(summary.values()):
                history = state.setdefault("history", [])
                history.append(
                    {"at": now_iso(), "event": "figure_sdk_legacy_reconciliation", **copy.deepcopy(summary)}
                )
                if len(history) > 500:
                    del history[:-500]

        self._mutate_pipeline(reconcile)
        return summary

    @staticmethod
    def _parallel_limit(state: Mapping[str, Any]) -> int:
        settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
        raw = settings.get("max_parallel_workers", config.FIGURE_DEFAULT_PARALLEL_WORKERS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = config.FIGURE_DEFAULT_PARALLEL_WORKERS
        return max(1, min(config.FIGURE_MAX_PARALLEL_WORKERS, value))

    def _build_plans(
        self,
        state: Mapping[str, Any],
        selected_paper_ids: set[str] | None,
        priority_paper_ids: set[str] | None = None,
    ) -> list[FigurePaperPlan]:
        papers = state.get("papers", {}) if isinstance(state.get("papers", {}), Mapping) else {}
        queue = [str(value) for value in state.get("paper_queue", [])]
        ordered_ids = list(dict.fromkeys(queue))
        for command in state.get("commands", []):
            if isinstance(command, Mapping) and command.get("status") == "pending":
                paper_id = str(command.get("paper_id") or "")
                if paper_id and paper_id not in ordered_ids:
                    ordered_ids.append(paper_id)
        priorities = {str(value) for value in priority_paper_ids or set() if str(value)}
        ordered_ids.sort(key=lambda value: (0 if value in priorities else 1, value.casefold()))
        settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
        paused_papers = {
            str(value)
            for value in settings.get("sdk_paused_paper_ids", [])
            if str(value)
        }
        plans: list[FigurePaperPlan] = []
        for paper_id in ordered_ids:
            if selected_paper_ids is not None and paper_id not in selected_paper_ids:
                continue
            if paper_id in paused_papers:
                continue
            record = papers.get(paper_id)
            if not isinstance(record, Mapping):
                continue
            revision_commands = self._pending_commands(state, paper_id, {"revise_figure"})
            revisions = tuple(
                FigureRevisionPlan(
                    command_id=str(command.get("id") or ""),
                    prompt=str((command.get("params") or {}).get("prompt") or "")
                    if isinstance(command.get("params"), Mapping)
                    else "",
                    figure=str((command.get("params") or {}).get("figure") or "")
                    if isinstance(command.get("params"), Mapping)
                    else "",
                )
                for command in revision_commands
            )
            request_id = str(record.get("sdk_request_id") or "").strip()
            normal_commands = self._pending_commands(
                state,
                paper_id,
                {"run_pipeline", "run_pipeline_late", "run_step"},
            )
            has_normal_intent = paper_id in queue or bool(normal_commands)
            if not has_normal_intent:
                request_id = ""
            if not request_id and normal_commands:
                request_id = str(normal_commands[-1].get("id") or "")
            latest_params: Mapping[str, Any] = {}
            if normal_commands and isinstance(normal_commands[-1].get("params"), Mapping):
                latest_params = normal_commands[-1].get("params", {})
            if request_id:
                start_step = int(record.get("run_from_step") or 2)
                run_until = int(record.get("run_until_step") or 3)
                workflow = normalize_figure_workflow_snapshot(
                    record.get("workflow_snapshot")
                    or latest_params.get("workflow_snapshot")
                    or default_figure_workflow_snapshot()
                )
                run_until = min(run_until, figure_workflow_step_count(workflow))
                reuse_valid_artifacts = bool(
                    record.get(
                        "reuse_valid_artifacts",
                        latest_params.get("reuse_valid_artifacts", True),
                    )
                )
                run_pipeline = True
            elif revisions:
                digest = hashlib.sha256("|".join(item.command_id for item in revisions).encode("utf-8")).hexdigest()[:16]
                request_id = f"revision-batch-{digest}"
                start_step = 4
                run_until = 4
                workflow = normalize_figure_workflow_snapshot(
                    record.get("workflow_snapshot") or default_figure_workflow_snapshot()
                )
                reuse_valid_artifacts = False
                run_pipeline = False
            else:
                continue
            source_tex = str(record.get("source_tex") or f"papers/{paper_id}/input.tex").replace("\\", "/")
            plans.append(
                FigurePaperPlan(
                    paper_id=paper_id,
                    request_id=request_id,
                    source_tex=source_tex,
                    start_step=start_step,
                    run_until_step=run_until,
                    revisions=revisions,
                    workflow_snapshot=workflow,
                    reuse_valid_artifacts=reuse_valid_artifacts,
                    run_pipeline=run_pipeline,
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
            snapshot_request_id = str(snapshot_copy.get("request_id") or "")
            current_request_id = str(record.get("sdk_request_id") or "")
            superseded = bool(
                snapshot_request_id
                and current_request_id
                and snapshot_request_id != current_request_id
            )
            record.update(
                {
                    "paper_id": paper_id,
                    "thread_id": str(snapshot_copy.get("thread_id") or ""),
                    "thread_title": str(snapshot_copy.get("thread_name") or ""),
                    "sdk_runtime_event": event,
                    "workflow_snapshot": copy.deepcopy(snapshot_copy.get("workflow_snapshot") or record.get("workflow_snapshot") or {}),
                    "workflow_version_id": str(snapshot_copy.get("workflow_version_id") or record.get("workflow_version_id") or ""),
                    "run_until_step": int(snapshot_copy.get("run_until_step") or record.get("run_until_step") or 3),
                    "updated_at": now_iso(),
                }
            )
            if superseded:
                # A new queue click arrived while the previous Step was still
                # running. Preserve the newer request and its queue slot; only
                # carry forward the fixed thread binding from the older run.
                record["status"] = "queued"
                record["evidence"] = "上一个 SDK 请求已到达 Step 边界；当前较新的画图请求继续保留排队"
                history = state.setdefault("history", [])
                history.append(
                    {
                        "at": now_iso(),
                        "event": "figure_sdk_superseded_progress_ignored",
                        "paper_id": paper_id,
                        "snapshot_request_id": snapshot_request_id,
                        "current_request_id": current_request_id,
                    }
                )
                if len(history) > 500:
                    del history[:-500]
                return
            record.update(
                {
                    "status": status,
                    "step_done": int(snapshot_copy.get("completed_step") or 0),
                    "sdk_request_id": snapshot_request_id or current_request_id,
                    "sdk_current_step": int(snapshot_copy.get("active_step") or 0),
                }
            )
            blocker = str(snapshot_copy.get("blocker") or "")
            blocker_message = str(snapshot_copy.get("blocker_message") or "")
            if blocker:
                record["blocker"] = blocker
                record["evidence"] = blocker_message or blocker
            else:
                record.pop("blocker", None)
                if status in {"queued", "running", "starting", "complete"}:
                    record["evidence"] = {
                        "queued": "已进入 V2 SDK 画图队列",
                        "starting": f"正在启动流程 Step {int(snapshot_copy.get('active_step') or 0)}",
                        "running": f"SDK 会话正在执行流程 Step {int(snapshot_copy.get('active_step') or 0)}",
                        "complete": "画图产物已通过严格验收",
                    }.get(status, "")
            queue = [str(value) for value in state.get("paper_queue", [])]
            if status in {"complete", "waiting_data", "blocked"}:
                state["paper_queue"] = [value for value in queue if value != paper_id]
            elif paper_id not in queue:
                queue.append(paper_id)
                state["paper_queue"] = queue

            terminal = status in {"complete", "waiting_data", "blocked"}
            revisions = snapshot_copy.get("revisions", {})
            for command in state.get("commands", []):
                if not isinstance(command, dict) or str(command.get("paper_id") or "") != paper_id:
                    continue
                command_id = str(command.get("id") or "")
                action = str(command.get("action") or "")
                if action == "revise_figure" and isinstance(revisions, Mapping):
                    revision = revisions.get(command_id)
                    revision_state = str(revision.get("state") or "") if isinstance(revision, Mapping) else ""
                    if revision_state == "complete":
                        command.update({"status": "done", "handled_at": now_iso(), "message": "V2 SDK revision complete"})
                    elif status == "blocked":
                        command.update({"status": "blocked", "handled_at": now_iso(), "message": blocker or "revision blocked"})
                elif action in {"run_pipeline", "run_pipeline_late", "run_step"} and terminal:
                    command.update(
                        {
                            "status": "done" if status in {"complete", "waiting_data"} else "blocked",
                            "handled_at": now_iso(),
                            "message": f"V2 SDK figure pipeline {status}",
                        }
                    )
            history = state.setdefault("history", [])
            history.append({"at": now_iso(), "event": f"figure_sdk_{event}", "paper_id": paper_id, "status": status})
            if len(history) > 500:
                del history[:-500]

        self._mutate_pipeline(project)

    def _should_continue(self, _paper_id: str, _step: int, _snapshot: Mapping[str, Any]) -> bool:
        state = self._load_pipeline()
        settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
        paused_papers = {
            str(value)
            for value in settings.get("sdk_paused_paper_ids", [])
            if str(value)
        }
        return (
            str(settings.get("scheduling_mode") or "manual") == "auto"
            and not bool(settings.get("sdk_pause_requested"))
            and _paper_id not in paused_papers
        )

    def run(self, paper_ids: list[str] | None = None) -> dict[str, Any]:
        try:
            account = self.ensure_ready()
            legacy = self._require_legacy_quiescent()
            reconciliation = self._reconcile_legacy_projection()
            state = self._load_pipeline()
            selected = {str(value).strip() for value in paper_ids or [] if str(value).strip()} if paper_ids is not None else None
            plans = self._build_plans(state, selected)
            if not plans:
                result = {
                    "ok": True,
                    "schema": FIGURE_SDK_RUNTIME_SCHEMA,
                    "status": "idle",
                    "plan_count": 0,
                    "papers": {},
                    "account": account,
                    "legacy_automation": legacy,
                    "reconciliation": reconciliation,
                }
                self._last_result = result
                self._last_error = ""
                return result
            dispatcher = self.dispatcher_factory(
                root=self.root,
                journal_path=self.journal_path,
                lock_path=self.dispatch_lock_path,
                codex_factory=self._codex_factory,
                max_parallel_papers=self._parallel_limit(state),
                model=self.model,
                progress_callback=self._progress_callback,
                should_continue=self._should_continue,
            )
            dispatched = asyncio.run(dispatcher.run_until_idle(plans))
            result = {
                "ok": bool(dispatched.get("ok")),
                "schema": FIGURE_SDK_RUNTIME_SCHEMA,
                "status": "complete" if dispatched.get("ok") else "attention",
                "plan_count": len(plans),
                "papers": dispatched.get("papers", {}),
                "account": account,
                "legacy_automation": legacy,
                "reconciliation": reconciliation,
            }
            self._last_result = result
            self._last_error = ""
            return result
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            self._last_error = code
            self._last_result = {
                "ok": False,
                "schema": FIGURE_SDK_RUNTIME_SCHEMA,
                "status": "failed",
                "blocker": code,
                "error_type": type(exc).__name__,
            }
            raise

    def _run_dynamic_background(self) -> dict[str, Any]:
        try:
            account = self.ensure_ready()
            legacy = self._require_legacy_quiescent()
            reconciliation = self._reconcile_legacy_projection()
            initial_state = self._load_pipeline()
            dispatcher = self.dispatcher_factory(
                root=self.root,
                journal_path=self.journal_path,
                lock_path=self.dispatch_lock_path,
                codex_factory=self._codex_factory,
                max_parallel_papers=self._parallel_limit(initial_state),
                model=self.model,
                progress_callback=self._progress_callback,
                should_continue=self._should_continue,
            )
            worker_targets: set[str] = set()

            def supply() -> list[FigurePaperPlan]:
                with self._background_lock:
                    worker_targets.update(self._priority_paper_ids)
                    run_all = self._background_run_all
                    priorities = set(self._priority_paper_ids)
                state = self._load_pipeline()
                settings = state.get("settings", {}) if isinstance(state.get("settings", {}), Mapping) else {}
                if (
                    str(settings.get("scheduling_mode") or "manual") != "auto"
                    or bool(settings.get("sdk_pause_requested"))
                ):
                    return []
                selected = None if run_all else set(worker_targets)
                return self._build_plans(state, selected, priorities)

            dynamic = getattr(dispatcher, "run_dynamic", None)
            if callable(dynamic):
                dispatched = asyncio.run(dynamic(supply))
            else:
                plans = supply()
                dispatched = asyncio.run(dispatcher.run_until_idle(plans))
            result = {
                "ok": bool(dispatched.get("ok")),
                "schema": FIGURE_SDK_RUNTIME_SCHEMA,
                "status": "complete" if dispatched.get("ok") else "attention",
                "plan_count": len(dispatched.get("papers", {})),
                "papers": dispatched.get("papers", {}),
                "dynamic": bool(dispatched.get("dynamic")),
                "account": account,
                "legacy_automation": legacy,
                "reconciliation": reconciliation,
            }
            self._last_result = result
            self._last_error = ""
            return result
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            self._last_error = code
            self._last_result = {
                "ok": False,
                "schema": FIGURE_SDK_RUNTIME_SCHEMA,
                "status": "failed",
                "blocker": code,
                "error_type": type(exc).__name__,
            }
            raise

    def _background_worker(self) -> None:
        while True:
            with self._background_lock:
                serial = self._wakeup_serial
            try:
                self._run_dynamic_background()
            except Exception:
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
        priority_paper_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        account = self.ensure_ready()
        self._require_legacy_quiescent()
        selected = priority_paper_ids if priority_paper_ids is not None else priority_paper_keys

        def enable(state: dict[str, Any]) -> None:
            settings = state.setdefault("settings", {})
            selected_ids = {
                str(value).strip()
                for value in selected or []
                if str(value).strip()
            }
            paused = {
                str(value)
                for value in settings.get("sdk_paused_paper_ids", [])
                if str(value)
            }
            settings["sdk_paused_paper_ids"] = sorted(
                set() if selected is None else paused - selected_ids,
                key=str.casefold,
            )
            settings.update(
                {
                    "scheduling_mode": "auto",
                    "dispatch_mode": "sdk_event_driven",
                    "max_parallel_workers": self._parallel_limit(state),
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
            worker = threading.Thread(target=self._background_worker, name="upo-figure-sdk", daemon=True)
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

    def revalidate_completed_output(self, paper_id: str) -> dict[str, Any]:
        """Accept an already-produced final package without starting a model turn."""

        paper_id = str(paper_id or "").strip()
        if not paper_id:
            raise FigureSDKRuntimeError("FIGURE_REVALIDATE_TARGET_EMPTY", "画图重新验收缺少论文编号")
        with self._background_lock:
            worker = self._background_thread
            if worker is not None and worker.is_alive():
                raise FigureSDKRuntimeError(
                    "FIGURE_REVALIDATE_WHILE_RUNNING",
                    "画图 SDK 仍在运行；请等待当前 Step 结束后再重新验收",
                )

        contract = validate_figure_step(self.root, paper_id, 3)
        if not contract.ok:
            message = "；".join(contract.issues[:4]) or "现有画图产物仍未通过 Step 3 验收"
            raise FigureSDKRuntimeError("FIGURE_OUTPUT_CONTRACT_INVALID", message)

        with FileLock(self.dispatch_lock_path):
            journal = read_json(
                self.journal_path,
                {"schema": FIGURE_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []},
            )
            if not isinstance(journal, dict):
                raise FigureSDKRuntimeError("FIGURE_SDK_JOURNAL_INVALID", "画图 SDK journal 不是对象")
            papers = journal.setdefault("papers", {})
            if not isinstance(papers, dict):
                raise FigureSDKRuntimeError("FIGURE_SDK_JOURNAL_INVALID", "画图 SDK journal.papers 不是对象")
            record = papers.get(paper_id)
            if not isinstance(record, dict):
                raise FigureSDKRuntimeError(
                    "FIGURE_SDK_JOURNAL_PAPER_MISSING",
                    "当前论文尚未建立 V2 SDK journal，不能只做本地重新验收",
                )
            final_step = max(1, int(record.get("run_until_step") or 3))
            step_record = record.setdefault("steps", {}).setdefault(str(final_step), {})
            step_record.update(
                {
                    "state": "complete",
                    "contract": contract.as_dict(),
                    "artifacts": [dict(item) for item in contract.artifacts],
                    "completed_at": now_iso(),
                    "updated_at": now_iso(),
                    "revalidated_without_model_turn": True,
                }
            )
            for field in ("blocker", "blocker_message"):
                step_record.pop(field, None)
                record.pop(field, None)
            record.update(
                {
                    "status": "complete",
                    "completed_step": max(int(record.get("completed_step") or 0), final_step),
                    "updated_at": now_iso(),
                    "revalidated_without_model_turn": True,
                }
            )
            record.pop("active_step", None)
            history = journal.setdefault("history", [])
            if isinstance(history, list):
                history.append(
                    {
                        "at": now_iso(),
                        "event": "paper_revalidated_without_model_turn",
                        "paper_id": paper_id,
                        "step": final_step,
                    }
                )
                if len(history) > 500:
                    del history[:-500]
            journal["updated_at"] = now_iso()
            write_json_atomic(self.journal_path, journal)
            snapshot = copy.deepcopy(record)

        self._progress_callback("paper_revalidated", paper_id, snapshot)
        return {
            "ok": True,
            "paper_id": paper_id,
            "status": "complete",
            "completed_step": int(snapshot.get("completed_step") or 0),
            "thread_id": str(snapshot.get("thread_id") or ""),
            "model_turn_started": False,
            "contract": contract.as_dict(),
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
            "message": "当前 SDK Step 不会被强制中断；完成后不再发送下一 Step。",
        }

    def request_pause_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise FigureSDKRuntimeError("FIGURE_PAUSE_TARGET_EMPTY", "至少选择一篇要暂停的画图论文")

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
            "message": "选中论文的当前 SDK Step 会正常结束，随后不再发送下一 Step；其他论文继续运行。",
        }

    pause = request_pause

    def status(self, *, check_account: bool = True) -> dict[str, Any]:
        pipeline = self._load_pipeline()
        settings = pipeline.get("settings", {}) if isinstance(pipeline.get("settings", {}), Mapping) else {}
        try:
            journal = read_json(
                self.journal_path,
                {"schema": FIGURE_SDK_JOURNAL_SCHEMA, "papers": {}},
            )
        except Exception:
            journal = {"schema": FIGURE_SDK_JOURNAL_SCHEMA, "papers": {}}
        papers = journal.get("papers", {}) if isinstance(journal, Mapping) and isinstance(journal.get("papers", {}), Mapping) else {}
        counts: dict[str, int] = {}
        blocker = self._last_error
        for record in papers.values():
            if not isinstance(record, Mapping):
                continue
            value = str(record.get("status") or "unknown")
            counts[value] = counts.get(value, 0) + 1
            if not blocker and value == "blocked":
                blocker = str(record.get("blocker") or "FIGURE_SDK_BLOCKED")
        with self._background_lock:
            worker = self._background_thread
            background_running = worker is not None and worker.is_alive()
            priority_count = len(self._priority_paper_ids)
            run_all = self._background_run_all
        paused_paper_ids = sorted(
            {
                str(value)
                for value in settings.get("sdk_paused_paper_ids", [])
                if str(value)
            },
            key=str.casefold,
        )
        account = self.account_service.status() if check_account else None
        active_count = sum(counts.get(value, 0) for value in ("creating_thread", "starting", "running"))
        waiting_count = sum(counts.get(value, 0) for value in ("waiting_event", "waiting_data"))
        if background_running:
            status = "running"
        elif blocker:
            status = "blocked"
        elif counts.get("paused") or str(settings.get("scheduling_mode") or "") == "manual":
            status = "paused"
        elif waiting_count:
            status = "waiting"
        elif counts.get("queued"):
            status = "ready"
        else:
            status = "idle"
        return {
            "schema": FIGURE_SDK_RUNTIME_SCHEMA,
            "ok": not blocker and (account is None or bool(account.get("ok") and account.get("authenticated"))),
            "status": status,
            "scheduling_mode": str(settings.get("scheduling_mode") or "manual"),
            "background_running": background_running,
            "priority_pending_count": priority_count,
            "dynamic_dispatch": True,
            "dispatch_scope": "all_queued" if run_all else "explicit_wakeups",
            "paused_paper_ids": paused_paper_ids,
            "paused_paper_count": len(paused_paper_ids),
            "configured_model": self.model,
            "max_parallel_workers": self._parallel_limit(pipeline),
            "journal_paper_count": len(papers),
            "journal_status_counts": counts,
            "status_counts": counts,
            "active_count": active_count,
            "running_count": counts.get("running", 0),
            "starting_count": counts.get("starting", 0) + counts.get("creating_thread", 0),
            "waiting_count": waiting_count,
            "blocked_count": counts.get("blocked", 0),
            "completed_count": counts.get("complete", 0),
            "isolated_codex_home": str(self.codex_home),
            "account": account,
            "blocker": blocker,
            "last_result": self._last_result,
        }


_DEFAULT_RUNTIME: FigureSDKRuntime | None = None
_DEFAULT_RUNTIME_LOCK = threading.Lock()


def get_figure_sdk_runtime() -> FigureSDKRuntime:
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            _DEFAULT_RUNTIME = FigureSDKRuntime()
        return _DEFAULT_RUNTIME


def figure_sdk_status() -> dict[str, Any]:
    return get_figure_sdk_runtime().status()


__all__ = [
    "FIGURE_SDK_RUNTIME_SCHEMA",
    "FigureSDKRuntime",
    "FigureSDKRuntimeError",
    "figure_sdk_status",
    "get_figure_sdk_runtime",
]
