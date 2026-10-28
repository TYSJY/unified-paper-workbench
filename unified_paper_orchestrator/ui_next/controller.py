from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Property,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices

from .. import __version__
from ..codex_sdk_account import CodexSDKAccountService
from ..commercial.runtime import CommercialRuntime
from ..config import (
    ACTIVE_WORKSPACE,
    LEGACY_PROJECTS,
    SMART_SUBMISSION_STATE_PATH,
    UNIFIED_STATE_PATH,
    WORKSPACE_NAME,
    WORKSPACE_ROOT,
)
from ..full_pipeline import get_full_pipeline_runtime
from ..orchestrator import UnifiedOrchestrator
from ..workspace import (
    WORKSPACE_ENV,
    create_workspace,
    list_registered_workspaces,
    load_workspace,
    register_workspace,
    set_active_workspace,
    suggested_workspace_directory,
)
from ..writing_evidence_import import prepare_writing_evidence_import
from ..writing_evidence_manual import prepare_manual_writing_evidence
from .i18n import I18nManager
from .sdk_models import discover_models, validate_profile
from .models import PaperListModel
from .read_model import (
    STAGE_LABELS,
    metrics_for,
    paper_rows,
    readable,
    selected_paper,
    stage_label,
)


LOGGER = logging.getLogger(__name__)


def _read_submission_monitor_summary(path: Path | str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
    if not isinstance(settings, dict):
        settings = {}
    current_run_id = str(settings.get("daily_monitor_current_run_id") or "").strip()
    finished_at = str(settings.get("daily_monitor_last_finished_at") or "").strip()
    status = (
        "running"
        if current_run_id
        else str(settings.get("daily_monitor_last_status") or "never_run").strip().casefold()
    )
    tone = {
        "completed": "success",
        "idle": "info",
        "partial": "attention",
        "failed": "danger",
        "running": "info",
    }.get(status, "secondary")
    return {
        "hasRun": bool(current_run_id or finished_at),
        "status": status,
        "tone": tone,
        "finishedAt": finished_at,
        "requested": int(settings.get("daily_monitor_last_paper_count") or 0),
        "checked": int(settings.get("daily_monitor_last_checked_count") or 0),
        "failed": int(settings.get("daily_monitor_last_failed_count") or 0),
        "pending": int(settings.get("daily_monitor_last_pending_count") or 0),
        "actionRequired": int(
            settings.get("daily_monitor_last_action_required_count") or 0
        ),
    }


class _WorkerSignals(QObject):
    completed = Signal(str, object, float)
    failed = Signal(str, str, str, str, float)


class _Worker(QRunnable):
    def __init__(self, key: str, callback: Callable[[], Any]) -> None:
        super().__init__()
        self.key = key
        self.callback = callback
        self.signals = _WorkerSignals()

    @Slot()
    def run(self) -> None:
        started = time.perf_counter()
        try:
            result = self.callback()
        except Exception as exc:  # UI boundary: preserve details in the log only.
            self.signals.failed.emit(
                self.key,
                type(exc).__name__,
                str(exc),
                traceback.format_exc(),
                (time.perf_counter() - started) * 1000,
            )
            return
        self.signals.completed.emit(
            self.key,
            result,
            (time.perf_counter() - started) * 1000,
        )


class WorkbenchController(QObject):
    """Presentation adapter between QML and the existing workflow services."""

    stateChanged = Signal()
    currentPageChanged = Signal()
    metricsChanged = Signal()
    selectedPaperChanged = Signal()
    loadingChanged = Signal()
    statusTextChanged = Signal()
    lastRefreshChanged = Signal()
    confirmRequested = Signal(str, str, str, int)
    countRequested = Signal(str, str, str, int, int, int)
    undergraduateBriefRequested = Signal(str, str)
    undergraduateCreated = Signal()
    toastRequested = Signal(str, str)
    errorRequested = Signal(str, str, str)
    fileRequested = Signal(str, str, str)
    retargetRequested = Signal(str)
    workspaceManagerRequested = Signal()
    workspaceListChanged = Signal()
    settingsDataChanged = Signal()
    modelCatalogsChanged = Signal()
    settingsRequested = Signal()
    promptEditorRequested = Signal(str, str, str, str, str)
    authorTemplateEditorRequested = Signal(str, str, str, bool)
    journalPreferencesRequested = Signal(str, "QVariantMap")
    authorTemplateApplyRequested = Signal(str, "QVariantList", str)
    fullPipelineStatusRequested = Signal("QVariantMap")
    fullPipelineTrustRequested = Signal(int, bool)
    quitRequested = Signal()
    languageChanged = Signal()
    submissionMonitorChanged = Signal()
    commercialActionCompleted = Signal(str, "QVariantMap")
    commercialStatusChanged = Signal()
    codexAccountChanged = Signal()
    codexAccountActionCompleted = Signal(str, "QVariantMap")

    def __init__(self) -> None:
        super().__init__()
        self.orchestrator = UnifiedOrchestrator()
        self.commercial = CommercialRuntime(app_version=__version__)
        self._commercial_status = self.commercial.status().to_dict()
        self._commercial_account: dict[str, Any] = {}
        self.codex_account = CodexSDKAccountService()
        self._codex_login_attempt = ""
        self._codex_login_code = ""
        self._codex_account_generation = 0
        self._codex_account_status: dict[str, Any] = {
            "ok": True,
            "authenticated": False,
            "requires_openai_auth": True,
            "auth_mode": "none",
            "plan_type": "none",
            "isolated_home": True,
            "loginPending": False,
            "statusKnown": False,
        }
        self.full_pipeline_runtime = get_full_pipeline_runtime(self.orchestrator)
        try:
            self._pipeline_bootstrap = self.full_pipeline_runtime.resume_if_active()
        except Exception:
            LOGGER.exception("Could not resume persisted full-pipeline runtime")
            self._pipeline_bootstrap = {"ok": False}
        try:
            self._pipeline_status = self.full_pipeline_runtime.status()
        except Exception:
            LOGGER.exception("Could not read persisted full-pipeline status")
            self._pipeline_status = {"active": False, "status": "idle", "summary": ""}
        self.i18n = I18nManager()
        self.i18n.languageChanged.connect(self._language_changed)
        self._pool = QThreadPool.globalInstance()
        from .undergraduate_materials import UndergraduateMaterials
        self._undergraduate_materials = UndergraduateMaterials(
            Path(LEGACY_PROJECTS["writing"]["root"]) / "materials" / "undergraduate", self)
        self._model = PaperListModel()
        self._state: dict[str, Any] = {}
        self._rows: list[dict[str, Any]] = []
        self._metrics: list[dict[str, Any]] = []
        self._selected: dict[str, Any] = {}
        self._selected_id = ""
        self._page = "overview"
        self._loading = False
        self._pending_refresh = False
        self._last_refresh_duration_ms = 0.0
        self._last_refresh_cached = True
        self._status_text = self._t("controller.reading_cache", "正在读取本地缓存…")
        self._last_refresh = self._t("controller.not_refreshed", "尚未刷新")
        self._active_tasks: set[str] = set()
        self._silent_tasks: set[str] = set()
        self._projection_stamp = self._read_projection_stamp()
        self._registered_workspaces: list[dict[str, Any]] = []
        self._settings_data: dict[str, Any] = {}
        self._writing_prompt_scheme = str(
            self.orchestrator.writing_workflow_store.active_snapshot().get("prompt_scheme") or "journal"
        )
        self._model_catalogs: dict[str, Any] = {}
        self._model_catalog_generation = 0
        self._submission_monitor: dict[str, Any] = {}
        self._pending_workspace_path = ""
        self._load_cached()
        self._reload_registered_workspaces()
        self._projection_timer = QTimer(self)
        self._projection_timer.setInterval(1250)
        self._projection_timer.timeout.connect(self._poll_projection)
        self._projection_timer.start()
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(15000)
        self._sync_timer.timeout.connect(self._auto_refresh)
        self._sync_timer.start()
        QTimer.singleShot(650, self._bootstrap_commercial)

    @Property(QObject, constant=True)
    def paperModel(self) -> PaperListModel:  # noqa: N802 - QML API
        return self._model

    @Property(QObject, constant=True)
    def undergraduateMaterials(self):  # noqa: N802
        return self._undergraduate_materials

    @Property(str, constant=True)
    def version(self) -> str:
        return __version__

    def _t(self, key: str, default: str = "", **values: Any) -> str:
        return self.i18n.translate(key, default=default, **values)

    @Property(str, notify=languageChanged)
    def workspaceName(self) -> str:  # noqa: N802
        return str(WORKSPACE_NAME or self._t("app.workspace_default", "统一论文主工作区"))

    @Property(str, constant=True)
    def workspacePath(self) -> str:  # noqa: N802
        return str(WORKSPACE_ROOT)

    @Property("QVariantList", notify=workspaceListChanged)
    def registeredWorkspaces(self) -> list[dict[str, Any]]:  # noqa: N802
        return self._registered_workspaces

    @Property("QVariantMap", notify=modelCatalogsChanged)
    def modelCatalogs(self) -> dict[str, Any]:  # noqa: N802
        return dict(self._model_catalogs)

    @Slot(str)
    def refreshModels(self, stage: str) -> None:  # noqa: N802
        if stage not in {"writing", "figures", "journal_author"}:
            return
        key = f"models:{self._model_catalog_generation}:{stage}"
        if key in self._active_tasks:
            return
        self._model_catalogs[stage] = {"state": "loading", "models": []}
        self.modelCatalogsChanged.emit()
        self._submit(key, "", lambda: discover_models(self.orchestrator, stage), silent=True, priority=True)

    def _finish_model_catalog(self, key: str, result: Any) -> None:
        _, generation, stage = key.split(":", 2)
        if int(generation) == self._model_catalog_generation:
            self._model_catalogs[stage] = result
            self.modelCatalogsChanged.emit()
        self._set_loading(bool(self._active_tasks - self._silent_tasks))

    @Property("QVariantMap", notify=settingsDataChanged)
    def settingsData(self) -> dict[str, Any]:  # noqa: N802
        return self._settings_data

    @Property(str, notify=settingsDataChanged)
    def writingPromptScheme(self) -> str:  # noqa: N802
        return self._writing_prompt_scheme

    @Property(str, notify=codexAccountChanged)
    def codexLoginCode(self) -> str:  # noqa: N802
        return self._codex_login_code

    @Property("QVariantMap", notify=commercialStatusChanged)
    def commercialStatus(self) -> dict[str, Any]:  # noqa: N802
        return self._commercial_status

    @Property("QVariantMap", notify=codexAccountChanged)
    def codexAccountStatus(self) -> dict[str, Any]:  # noqa: N802
        return self._codex_account_status

    @Property("QVariantMap", notify=submissionMonitorChanged)
    def submissionMonitor(self) -> dict[str, Any]:  # noqa: N802
        return self._submission_monitor

    @Property(str, constant=True)
    def brandLogo(self) -> str:  # noqa: N802
        path = Path(__file__).resolve().parents[1] / "ui" / "assets" / "brand_mark_96.png"
        return QUrl.fromLocalFile(str(path)).toString()

    @Property(str, notify=currentPageChanged)
    def currentPage(self) -> str:  # noqa: N802
        return self._page

    @Property(str, notify=currentPageChanged)
    def currentPageTitle(self) -> str:  # noqa: N802
        if self._page == "overview":
            return self._t("nav.overview", "总览")
        return stage_label(
            self._page,
            self._t,
        ) if self._page in STAGE_LABELS else self._t("nav.workbench", "工作台")

    @Property("QVariantList", notify=metricsChanged)
    def metrics(self) -> list[dict[str, Any]]:
        return self._metrics

    @Property("QVariantMap", notify=selectedPaperChanged)
    def selectedPaper(self) -> dict[str, Any]:  # noqa: N802
        return self._selected

    @Property("QVariantList", notify=selectedPaperChanged)
    def selectedActions(self) -> list[dict[str, Any]]:  # noqa: N802
        if not self._selected_id:
            return []
        status = self._selected_stage_status()
        actions: list[dict[str, Any]] = []
        if self._page == "writing":
            actions.append(self._action("continue_writing", "action.continue_selected", "继续/重试选中论文", "play", "primary"))
            if status == "waiting_data":
                actions.extend(
                    [
                        self._action("confirm_writing_data", "action.confirm_manual_data", "数据已手动改完并继续", "check"),
                        self._action("import_writing_data", "action.import_data", "导入实验数据并继续", "upload"),
                    ]
                )
        elif self._page == "figures":
            if status == "waiting_data":
                actions.append(self._action("continue_figure_step3", "action.continue_figure_step3", "真实数据已补齐，继续 Step 3", "play", "primary"))
            else:
                actions.append(self._action("run_figures", "action.run_selected", "运行/重试选中论文", "play", "primary"))
            actions.extend(
                [
                    self._action("revalidate_figures", "action.revalidate", "重新验收产物", "refresh"),
                    self._action("pause_figures", "action.pause_selected", "暂停选中论文", "pause"),
                    self._action("resume_figures", "action.resume_selected", "继续选中论文", "play"),
                ]
            )
        elif self._page == "journal_author":
            actions.extend(
                [
                    self._action("run_journal_full", "action.run_selected", "运行/重试选中论文", "play", "primary"),
                    self._action("run_journal_selection", "action.rerun_selection", "仅重新选刊", "refresh"),
                    self._action("run_journal_package", "action.rebuild_package", "仅重新生成投稿包", "package"),
                    self._action("edit_journal_preferences", "action.edit_journal_preferences", "编辑选刊偏好", "edit"),
                    self._action("apply_author_template", "action.apply_author_template", "套用作者资料模板", "people"),
                    self._action("revalidate_journal", "action.revalidate", "重新验收产物", "refresh"),
                    self._action("pause_journal", "action.pause_selected", "暂停选中论文", "pause"),
                    self._action("resume_journal", "action.resume_selected", "继续选中论文", "play"),
                ]
            )
        elif self._page == "submission":
            actions.extend(
                [
                    self._action("start_selected_submission", "action.start_selected_submission", "启动/继续这篇智能投稿", "play", "primary"),
                    self._action("check_selected_submission", "action.check_selected_submission", "立即检查这篇投稿状态", "refresh"),
                    self._action("manual_takeover", "action.manual_takeover", "人工修改当前页面", "edit"),
                    self._action("manual_takeover_done", "action.manual_takeover_done", "修改完成并继续", "check"),
                    self._action("resume_submission_login", "action.resume_login", "继续登录/验证", "person"),
                    self._action("retarget_submission", "action.retarget_submission", "转投其他期刊", "forward"),
                ]
            )
        next_stage = self._next_stage_for_selected()
        writing = self._selected_record().get("stages", {}).get("writing", {})
        if writing.get("prompt_scheme") in {"undergraduate_humanities", "undergraduate_software"}:
            actions.extend([
                self._action("open_word_delivery", "action.open_word_delivery", "打开 Word 正式稿", "document"),
                self._action("open_word_preview", "action.open_word_preview", "打开 Word 排版预览", "document"),
            ])
        actions.extend(
            [
                self._action("open_paper_pdf", "action.open_paper_pdf", "打开论文 PDF", "document"),
                self._action("open_paper_folder", "action.open_paper_folder", "打开当前阶段产物目录", "folder"),
                *(
                    [self._action(f"force_stage:{next_stage}", "action.force_next_stage", "人工放行到下一阶段", "forward")]
                    if next_stage
                    else []
                ),
                self._action("archive_paper", "action.archive_paper", "归档整篇论文", "archive", "attention"),
                self._action("purge_paper", "action.purge_paper", "彻底删除整篇论文", "trash", "danger"),
            ]
        )
        return actions

    @Property(str, notify=selectedPaperChanged)
    def selectedPaperId(self) -> str:  # noqa: N802
        return self._selected_id

    @Property(bool, notify=loadingChanged)
    def loading(self) -> bool:
        return self._loading

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:  # noqa: N802
        return self._status_text

    @Property(str, notify=lastRefreshChanged)
    def lastRefresh(self) -> str:  # noqa: N802
        return self._last_refresh

    @Property("QVariantMap", notify=stateChanged)
    def fullPipelineStatus(self) -> dict[str, Any]:  # noqa: N802
        return self._pipeline_status

    @Property(bool, notify=stateChanged)
    def fullPipelineActive(self) -> bool:  # noqa: N802
        return bool(self._pipeline_status.get("active"))

    @Property(str, notify=stateChanged)
    def fullPipelineLabel(self) -> str:  # noqa: N802
        if not self._pipeline_status.get("active"):
            return self._t("action.full_pipeline", "一键全流程")
        progress = int(self._pipeline_status.get("progress_percent") or 0)
        status = str(self._pipeline_status.get("status") or "")
        if status == "paused":
            return self._t("pipeline.resume_short", "继续全流程")
        return self._t("pipeline.progress_short", "全流程 {progress}%", progress=progress)

    @Property(int, notify=stateChanged)
    def totalPapers(self) -> int:  # noqa: N802
        return len(self._rows)

    def _action(
        self,
        key: str,
        translation_key: str,
        fallback: str,
        icon: str,
        tone: str = "secondary",
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": self._t(translation_key, fallback),
            "icon": icon,
            "tone": tone,
        }

    def _selected_record(self) -> dict[str, Any]:
        papers = self._state.get("papers", {}) if isinstance(self._state, dict) else {}
        record = papers.get(self._selected_id, {}) if isinstance(papers, dict) else {}
        return record if isinstance(record, dict) else {}

    def _selected_stage_status(self) -> str:
        record = self._selected_record()
        stage = self._page if self._page in STAGE_LABELS else str(record.get("current_stage") or "writing")
        stages = record.get("stages", {}) if isinstance(record.get("stages"), dict) else {}
        value = stages.get(stage, {}) if isinstance(stages, dict) else {}
        return str(value.get("status") or "missing").strip().casefold() if isinstance(value, dict) else "missing"

    def _next_stage_for_selected(self) -> str:
        order = ("writing", "figures", "journal_author", "submission")
        record = self._selected_record()
        stage = self._page if self._page in order else str(record.get("current_stage") or "")
        try:
            index = order.index(stage)
        except ValueError:
            return ""
        return order[index + 1] if index + 1 < len(order) else ""

    @staticmethod
    def _read_projection_stamp() -> tuple[int, int]:
        try:
            stat = Path(UNIFIED_STATE_PATH).stat()
        except OSError:
            return (0, 0)
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _load_cached(self) -> None:
        started = time.perf_counter()
        try:
            state = self.orchestrator.load_unified_state()
        except Exception:
            LOGGER.exception("Qt shell could not read the cached unified state")
            state = {"papers": {}, "history": []}
        self._apply_state(state, duration_ms=(time.perf_counter() - started) * 1000, cached=True)

    def _apply_state(self, state: Any, *, duration_ms: float, cached: bool = False) -> None:
        if not isinstance(state, dict):
            state = self.orchestrator.load_unified_state()
        self._projection_stamp = self._read_projection_stamp()
        self._state = state
        self._rows = paper_rows(state, self._t)
        self._model.replace(self._rows)
        self._model.configure(page=self._page)
        self._metrics = metrics_for(self._rows, self._page, self._t)
        monitor_summary = _read_submission_monitor_summary(
            SMART_SUBMISSION_STATE_PATH
        )
        monitor_changed = monitor_summary != self._submission_monitor
        self._submission_monitor = monitor_summary
        if self._selected_id and any(row.get("paperId") == self._selected_id for row in self._rows):
            self._selected = selected_paper(
                self._state,
                self._selected_id,
                self._page,
                self._t,
            )
        elif self._rows:
            self._selected_id = str(self._rows[0].get("paperId") or "")
            self._selected = selected_paper(
                self._state,
                self._selected_id,
                self._page,
                self._t,
            )
        else:
            self._selected_id = ""
            self._selected = {}
        self._last_refresh_duration_ms = float(duration_ms)
        self._last_refresh_cached = bool(cached)
        source_label = self._t(
            "footer.cache" if cached else "footer.realtime",
            "缓存" if cached else "实时",
        )
        self._last_refresh = f"{duration_ms:.0f} ms · {source_label}"
        self._status_text = (
            self._t("controller.cache_shown", "缓存已显示，后台同步中")
            if cached
            else self._t(
                "controller.synced",
                "已同步 {count} 篇论文",
                count=len(self._rows),
            )
        )
        self.stateChanged.emit()
        self.metricsChanged.emit()
        self.selectedPaperChanged.emit()
        self.lastRefreshChanged.emit()
        self.statusTextChanged.emit()
        if monitor_changed:
            self.submissionMonitorChanged.emit()

    @Slot()
    def _language_changed(self) -> None:
        self._rows = paper_rows(self._state, self._t)
        self._model.replace(self._rows)
        self._model.configure(page=self._page)
        self._metrics = metrics_for(self._rows, self._page, self._t)
        self._selected = selected_paper(
            self._state,
            self._selected_id,
            self._page,
            self._t,
        ) if self._selected_id else {}
        if self._status_text:
            self._status_text = self._t(
                "controller.synced",
                "已同步 {count} 篇论文",
                count=len(self._rows),
            )
        source_label = self._t(
            "footer.cache" if self._last_refresh_cached else "footer.realtime",
            "缓存" if self._last_refresh_cached else "实时",
        )
        self._last_refresh = f"{self._last_refresh_duration_ms:.0f} ms · {source_label}"
        self.languageChanged.emit()
        self.currentPageChanged.emit()
        self.stateChanged.emit()
        self.metricsChanged.emit()
        self.selectedPaperChanged.emit()
        self.statusTextChanged.emit()
        self.lastRefreshChanged.emit()

    def _set_loading(self, value: bool, status: str = "") -> None:
        value = bool(value)
        if value != self._loading:
            self._loading = value
            self.loadingChanged.emit()
        if status and status != self._status_text:
            self._status_text = status
            self.statusTextChanged.emit()

    def _submission_monitor_completion(
        self,
        result: Any,
    ) -> tuple[str, str]:
        payload = result if isinstance(result, dict) else {}
        summary = (
            payload.get("summary", {})
            if isinstance(payload.get("summary"), dict)
            else {}
        )
        requested = int(summary.get("requested_count") or 0)
        checked = int(summary.get("checked_count") or 0)
        failed = int(summary.get("failed_count") or 0)
        actions = int(summary.get("action_required_count") or 0)
        status = str(payload.get("status") or "").strip().casefold()
        tone = (
            "success"
            if status in {"completed", "idle"} and not failed
            else "danger"
            if status == "failed"
            else "attention"
        )
        return (
            self._t(
                "controller.submission_check_done",
                "投稿状态检查完成：{checked}/{requested}，失败 {failed}，需处理 {actions}",
                checked=checked,
                requested=requested,
                failed=failed,
                actions=actions,
            ),
            tone,
        )

    def _submit(
        self,
        key: str,
        label: str,
        callback: Callable[[], Any],
        *,
        priority: bool = False,
        silent: bool = False,
    ) -> None:
        if key in self._active_tasks:
            if key in {"refresh", "auto_refresh"}:
                self._pending_refresh = True
            return
        silent_refresh_only = bool(self._active_tasks) and self._active_tasks.issubset(
            self._silent_tasks
        )
        if self._active_tasks and not priority and not (silent_refresh_only and not silent):
            if key in {"refresh", "auto_refresh"}:
                self._pending_refresh = True
            elif not silent:
                self.toastRequested.emit(
                    self._t("controller.busy", "当前已有后台操作正在执行，请稍后。"),
                    "attention",
                )
            return
        self._active_tasks.add(key)
        if silent:
            self._silent_tasks.add(key)
        else:
            self._set_loading(True, label)
        worker = _Worker(key, callback)
        worker.signals.completed.connect(self._task_completed)
        worker.signals.failed.connect(self._task_failed)
        self._pool.start(worker)

    def _publish_codex_account_status(
        self,
        status: dict[str, Any] | None,
        *,
        login_pending: bool,
        status_known: bool = True,
    ) -> None:
        """Expose only the bounded, secret-free account projection to QML."""

        source = status if isinstance(status, dict) else {}
        public = {
            "schema": str(source.get("schema") or ""),
            "ok": bool(source.get("ok", True)),
            "authenticated": bool(source.get("authenticated", False)),
            "requires_openai_auth": source.get("requires_openai_auth"),
            "auth_mode": str(source.get("auth_mode") or "none"),
            "plan_type": str(source.get("plan_type") or "none"),
            "isolated_home": bool(source.get("isolated_home", True)),
            "error_code": str(source.get("error_code") or ""),
            "error_type": str(source.get("error_type") or ""),
            "loginPending": bool(login_pending),
            "canCancelLogin": bool(login_pending and getattr(self, "_codex_login_attempt", "")),
            "statusKnown": bool(status_known),
        }
        self._codex_account_status = public
        self.codexAccountChanged.emit()

    def _begin_codex_browser_login(self, method: str = "browser") -> dict[str, str]:
        challenge = (self.codex_account.begin_device_login() if method == "device_code"
                     else self.codex_account.begin_browser_login())
        return {
            "attemptId": str(challenge.attempt_id),
            "method": str(challenge.method),
            "verificationUrl": str(challenge.verification_url),
            **({"userCode": str(challenge.user_code)} if method == "device_code" else {}),
        }

    def _wait_for_codex_browser_login(self, attempt_id: str) -> dict[str, Any]:
        result = self.codex_account.wait_for_login(str(attempt_id))
        return {
            "success": bool(result.success),
            "errorCode": str(result.error_code or ""),
            "status": dict(result.status),
        }

    def _complete_codex_account_task(self, key: str, result: Any) -> None:
        payload = result if isinstance(result, dict) else {}
        if key == "codex:begin_login":
            attempt_id = str(payload.get("attemptId") or "")
            verification_url = str(payload.get("verificationUrl") or "")
            opened = bool(
                attempt_id
                and verification_url
                and QDesktopServices.openUrl(QUrl(verification_url))
            )
            if not opened:
                if attempt_id:
                    try:
                        self.codex_account.cancel_login(attempt_id)
                    except Exception:
                        LOGGER.debug(
                            "Could not cancel Codex login after browser-open failure",
                            exc_info=True,
                        )
                failure = {
                    "ok": False,
                    "authenticated": False,
                    "requires_openai_auth": True,
                    "auth_mode": "none",
                    "plan_type": "none",
                    "isolated_home": True,
                    "error_code": "CODEX_LOGIN_BROWSER_OPEN_FAILED",
                }
                self._publish_codex_account_status(
                    failure,
                    login_pending=False,
                )
                self.codexAccountActionCompleted.emit(
                    "codex:login_failed",
                    dict(self._codex_account_status),
                )
                self.toastRequested.emit(
                    self._t(
                        "settings.codex_browser_failed",
                        "未能打开 ChatGPT 官方授权页面。",
                    ),
                    "danger",
                )
                return

            self._codex_login_attempt = attempt_id
            self._codex_login_code = str(payload.get("userCode") or "")
            self._publish_codex_account_status(
                self._codex_account_status,
                login_pending=True,
                status_known=bool(
                    self._codex_account_status.get("statusKnown", False)
                ),
            )
            self.codexAccountActionCompleted.emit(
                "codex:login_started",
                dict(self._codex_account_status),
            )
            self._submit(
                "codex:wait_login",
                self._t(
                    "settings.codex_waiting",
                    "正在等待浏览器完成 ChatGPT 授权…",
                ),
                lambda: self._wait_for_codex_browser_login(attempt_id),
                priority=True,
                silent=True,
            )
            return

        if key == "codex:wait_login":
            self._codex_account_generation = getattr(self, "_codex_account_generation", 0) + 1
            self._codex_login_attempt = ""
            self._codex_login_code = ""
            status = (
                payload.get("status", {})
                if isinstance(payload.get("status"), dict)
                else {}
            )
            success = bool(payload.get("success")) and bool(
                status.get("authenticated")
            )
            if not success and payload.get("errorCode"):
                status = dict(status)
                status["error_code"] = str(payload.get("errorCode") or "")
            self._publish_codex_account_status(
                status,
                login_pending=False,
            )
            error_code = str(payload.get("errorCode") or "")
            message_key = ("settings.codex_login_success" if success else
                           "settings.codex_login_cancelled" if error_code == "CODEX_LOGIN_CANCELLED" else
                           "settings.codex_login_timeout" if error_code == "CODEX_LOGIN_TIMED_OUT" else
                           "settings.codex_login_failed")
            action = ("codex:login_succeeded" if success else
                      "codex:login_cancelled" if error_code == "CODEX_LOGIN_CANCELLED" else "codex:login_failed")
            response = dict(self._codex_account_status)
            response["success"] = success
            self.codexAccountActionCompleted.emit(action, response)
            self.toastRequested.emit(
                self._t(
                    message_key,
                    (
                        "Codex SDK 账号已连接。"
                        if success
                        else "ChatGPT 授权未完成，请重试。"
                    ),
                ),
                "success" if success else "info" if error_code == "CODEX_LOGIN_CANCELLED" else "danger",
            )
            return

        if key in {"codex:status", "codex:logout"}:
            if key == "codex:status" and "accountStatus" in payload:
                if payload.get("generation") != getattr(self, "_codex_account_generation", 0):
                    return
                payload = payload["accountStatus"]
            self._publish_codex_account_status(
                payload,
                login_pending=bool(self._codex_account_status.get("loginPending")) if key == "codex:status" else False,
            )
            self.codexAccountActionCompleted.emit(
                key,
                dict(self._codex_account_status),
            )
            if key == "codex:logout":
                self.toastRequested.emit(
                    self._t(
                        "settings.codex_logout_success",
                        "已断开本机 Codex SDK 账号。",
                    ),
                    "success",
                )

    @Slot(str, object, float)
    def _task_completed(self, key: str, result: Any, duration_ms: float) -> None:
        silent = key in self._silent_tasks
        self._active_tasks.discard(key)
        self._silent_tasks.discard(key)
        if key.startswith("models:"):
            self._finish_model_catalog(key, result)
            return
        if key in {"codex:wait_login", "codex:logout"}:
            self._model_catalog_generation += 1
            self._model_catalogs.clear()
            self.modelCatalogsChanged.emit()
        try:
            if key == "new_undergraduate":
                self.undergraduateCreated.emit()
            codex_only = key.startswith("codex:")
            if codex_only:
                self._complete_codex_account_task(key, result)
            if key in {"load_settings", "refresh_settings"} and isinstance(result, dict):
                self._settings_data = result
                writing = next((s for s in result.get("stages", []) if s.get("key") == "writing"), {})
                self._writing_prompt_scheme = str(writing.get("promptScheme") or "journal")
                self.settingsDataChanged.emit()
                if key == "load_settings":
                    self.settingsRequested.emit()
            if key == "commercial:account_summary" and isinstance(result, dict):
                self._commercial_account = result
                self.settingsDataChanged.emit()
            elif key == "commercial:sign_out":
                self._commercial_account = {}
                self.settingsDataChanged.emit()
            if (
                key.startswith(("commercial:", "settings:commercial"))
                and isinstance(result, dict)
            ):
                self.commercialActionCompleted.emit(key, result)
            if key.startswith(("commercial:", "settings:commercial")):
                self._refresh_commercial_status()
            commercial_only = key.startswith(
                ("commercial:", "settings:commercial")
            )
            if not commercial_only and not codex_only:
                if isinstance(result, dict) and isinstance(result.get("papers"), dict):
                    state = result
                else:
                    state = self.orchestrator.load_unified_state()
                self._apply_state(state, duration_ms=duration_ms)
                self._refresh_pipeline_status()
            if key in {"register_workspace", "create_workspace"}:
                self._reload_registered_workspaces()
            if key.startswith(("settings:", "commercial:")):
                QTimer.singleShot(120, self._refresh_settings_data)
            if key in {
                "commercial:complete_login",
                "commercial:refresh_license",
                "commercial:startup_refresh",
            }:
                QTimer.singleShot(450, self.refreshCommercialAccount)
            messages = {
                "refresh": self._t("controller.refresh_done", "状态刷新完成"),
                "new_writing": self._t("controller.new_writing_done", "新写作任务已加入队列"),
                "new_undergraduate": "毕业论文已创建，第一步将整理题目与学校资料。",
                "full_pipeline": self._t("controller.pipeline_done", "一键全流程已经启动"),
                "pause_all": self._t("controller.pause_all_done", "全部调度已请求安全暂停"),
                "start_stage": self._t(
                    "controller.start_stage_done",
                    "{stage}调度已启动",
                    stage=self.currentPageTitle,
                ),
                "pause_stage": self._t(
                    "controller.pause_stage_done",
                    "{stage}调度已请求暂停",
                    stage=self.currentPageTitle,
                ),
            }
            if not silent and not codex_only:
                message = messages.get(
                    key,
                    self._t("controller.action_done", "操作已完成"),
                )
                tone = "success"
                if key in {
                    "check_all_submissions",
                    "selected:check_selected_submission",
                }:
                    message, tone = self._submission_monitor_completion(result)
                self.toastRequested.emit(
                    message,
                    tone,
                )
        finally:
            self._set_loading(bool(self._active_tasks - self._silent_tasks))
        if self._pending_refresh and not self._active_tasks:
            self._pending_refresh = False
            QTimer.singleShot(120, self._auto_refresh)

    @Slot(str, str, str, str, float)
    def _task_failed(
        self,
        key: str,
        error_type: str,
        message: str,
        technical: str,
        duration_ms: float,
    ) -> None:
        if key.startswith("models:"):
            self._active_tasks.discard(key)
            self._silent_tasks.discard(key)
            # SDK exceptions may contain account-specific details. Keep those
            # out of both the model picker and general-purpose diagnostics.
            self._finish_model_catalog(key, {"state": "error", "models": []})
            return
        LOGGER.error("Qt UI task %s failed after %.0f ms: %s", key, duration_ms, technical)
        try:
            operation = re.sub(r"[^A-Za-z0-9_.:-]+", "_", key)[:120] or "unknown"
            self.commercial.queue_failure_diagnostic(
                component="qt_controller",
                operation=operation,
                exception_class=str(error_type or "Exception")[:120],
                duration_ms=max(0, int(duration_ms)),
            )
        except Exception:
            LOGGER.debug("Could not queue privacy-minimised diagnostic", exc_info=True)
        silent = key in self._silent_tasks
        self._active_tasks.discard(key)
        self._silent_tasks.discard(key)
        if key.startswith("codex:"):
            if key in {"codex:begin_login", "codex:wait_login"}:
                self._codex_login_attempt = ""
                self._codex_login_code = ""
            failure_status = dict(self._codex_account_status)
            failure_status.update(
                {
                    "ok": False,
                    "error_code": str(message or error_type),
                }
            )
            self._publish_codex_account_status(
                failure_status,
                login_pending=bool(self._codex_account_status.get("loginPending"))
                if key in {"codex:status", "codex:cancel_login"} else False,
            )
            failed_result = dict(self._codex_account_status)
            failed_result["success"] = False
            self.codexAccountActionCompleted.emit(
                "codex:login_failed"
                if key in {"codex:begin_login", "codex:wait_login"}
                else key,
                failed_result,
            )
            self.toastRequested.emit(
                self._t(
                    "settings.codex_action_failed",
                    "Codex SDK 账号操作未完成，请重试。",
                ),
                "danger",
            )
        if key.startswith(("commercial:", "settings:commercial")):
            self._refresh_commercial_status()
        visible_message = str(message or error_type).strip()
        self._status_text = self._t(
            "controller.action_failed",
            "操作未完成：{error}",
            error=visible_message,
        )
        self.statusTextChanged.emit()
        self._set_loading(bool(self._active_tasks - self._silent_tasks))
        if not silent:
            self.errorRequested.emit(
                self._t("controller.error_title", "操作未完成"),
                visible_message,
                technical,
            )
            self.toastRequested.emit(
                self._t(
                    "controller.action_failed_visible",
                    "操作未完成，请查看错误详情。",
                ),
                "danger",
            )
        if self._pending_refresh and not self._active_tasks:
            self._pending_refresh = False
            QTimer.singleShot(120, self._auto_refresh)

    @Slot()
    def _auto_refresh(self) -> None:
        self._submit(
            "auto_refresh",
            "",
            lambda: self.orchestrator.sync(persist=True),
            silent=True,
        )

    def _refresh_commercial_status(self) -> None:
        try:
            status = self.commercial.status().to_dict()
        except Exception as exc:
            LOGGER.exception("Could not refresh commercial status")
            status = {
                **self._commercial_status,
                "usable": False,
                "canStart": False,
                "reason": str(exc),
            }
        if status != self._commercial_status:
            self._commercial_status = status
            self.commercialStatusChanged.emit()

    @Slot()
    def _bootstrap_commercial(self) -> None:
        self._refresh_commercial_status()
        if not (
            self._commercial_status.get("enabled")
            and self._commercial_status.get("signedIn")
            and self._commercial_status.get("refreshRecommended")
        ):
            return
        self._submit(
            "commercial:startup_refresh",
            "",
            lambda: self.commercial.refresh_license().to_dict(),
            silent=True,
        )

    @Slot()
    def _poll_projection(self) -> None:
        self._refresh_pipeline_status()
        stamp = self._read_projection_stamp()
        if stamp == self._projection_stamp:
            return
        self._projection_stamp = stamp
        self._load_cached()

    def _refresh_pipeline_status(self) -> None:
        try:
            status = self.full_pipeline_runtime.status()
        except Exception:
            LOGGER.debug("Could not refresh full-pipeline status", exc_info=True)
            return
        previous = self._pipeline_status
        fingerprint = (
            str(status.get("batch_id") or ""),
            str(status.get("status") or ""),
            int(status.get("revision") or 0),
            int(status.get("progress_percent") or 0),
            bool(status.get("runtime_running")),
            str(status.get("last_error") or ""),
        )
        previous_fingerprint = (
            str(previous.get("batch_id") or ""),
            str(previous.get("status") or ""),
            int(previous.get("revision") or 0),
            int(previous.get("progress_percent") or 0),
            bool(previous.get("runtime_running")),
            str(previous.get("last_error") or ""),
        )
        if fingerprint != previous_fingerprint:
            self._pipeline_status = status
            self.stateChanged.emit()

    @Slot()
    def refresh(self) -> None:
        self._submit(
            "refresh",
            self._t("controller.refreshing", "正在后台同步各阶段状态…"),
            lambda: self.orchestrator.sync(persist=True),
        )

    @Slot(str)
    def selectPage(self, page: str) -> None:  # noqa: N802
        page = str(page or "overview")
        if page not in {"overview", *STAGE_LABELS.keys()} or page == "done":
            page = "overview"
        if page == self._page:
            return
        self._page = page
        self._model.configure(page=page)
        self._metrics = metrics_for(self._rows, page, self._t)
        if self._model.indexOf(self._selected_id) < 0 and self._model.count:
            first = self._model.get(0)
            self._selected_id = str(first.get("paperId") or "")
        self._selected = selected_paper(
            self._state,
            self._selected_id,
            page,
            self._t,
        )
        self.currentPageChanged.emit()
        self.metricsChanged.emit()
        self.selectedPaperChanged.emit()

    @Slot(str)
    def setSearch(self, query: str) -> None:  # noqa: N802
        self._model.configure(query=str(query or ""))

    @Slot(str)
    def setStatusFilter(self, status: str) -> None:  # noqa: N802
        self._model.configure(status=str(status or "all"))

    @Slot(str)
    def selectPaper(self, paper_id: str) -> None:  # noqa: N802
        paper_id = str(paper_id or "")
        if not paper_id or paper_id == self._selected_id:
            return
        self._selected_id = paper_id
        self._selected = selected_paper(
            self._state,
            paper_id,
            self._page,
            self._t,
        )
        self.selectedPaperChanged.emit()

    def _run_entitled(
        self,
        features: tuple[str, ...],
        callback: Callable[[], Any],
        *,
        requested_capacity: int = 0,
    ) -> Any:
        self.commercial.require_features(features)
        if requested_capacity:
            self.commercial.require_capacity(requested_capacity)
        return callback()

    def _configured_stage_parallel(self, stage: str) -> int:
        if stage in {"writing", "figures", "journal_author"}:
            inventory = self.orchestrator.stage_prompt_inventory(stage)
            settings = (
                inventory.get("settings", {})
                if isinstance(inventory.get("settings"), dict)
                else {}
            )
            try:
                return max(1, int(settings.get("max_parallel_workers") or 1))
            except (TypeError, ValueError):
                return 1
        if stage == "submission":
            try:
                state = json.loads(
                    Path(SMART_SUBMISSION_STATE_PATH).read_text(encoding="utf-8")
                )
                settings = (
                    state.get("settings", {})
                    if isinstance(state, dict)
                    and isinstance(state.get("settings"), dict)
                    else {}
                )
                return max(1, int(settings.get("max_parallel_workers") or 1))
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                return 1
        return 1

    @Slot(str)
    def prepareAction(self, action: str) -> None:  # noqa: N802
        action = str(action or "")
        if action == "refresh":
            self.refresh()
            return
        if action == "new_undergraduate":
            from ..writing_prompt_schemes import SCHEMES, scheme_id, is_undergraduate
            workflow = self.orchestrator.writing_workflow_store.active_snapshot()
            identifier = scheme_id(workflow) if is_undergraduate(workflow) else "undergraduate_humanities"
            snapshot = self.orchestrator.writing_workflow_store.snapshot_for_scheme(identifier)
            self.undergraduateBriefRequested.emit(SCHEMES[identifier][0], snapshot["version_id"])
            return
        if action == "new_writing":
            from ..writing_prompt_schemes import is_undergraduate, SCHEMES, scheme_id
            store = getattr(self.orchestrator, "writing_workflow_store", None)
            workflow = store.active_snapshot() if store is not None else {}
            if is_undergraduate(workflow):
                self.undergraduateBriefRequested.emit(SCHEMES[scheme_id(workflow)][0], workflow["version_id"])
                return
            self.countRequested.emit(
                self._t("controller.new_writing_title", "新建写作任务"),
                self._t("controller.new_writing_message", "输入本次需要新建的论文数量。"),
                action,
                1,
                1,
                8,
            )
            return
        if action == "full_pipeline":
            self._refresh_pipeline_status()
            if self._pipeline_status.get("active"):
                self.fullPipelineStatusRequested.emit(self._pipeline_status)
                return
            self.countRequested.emit(
                self._t("controller.pipeline_title", "一键跑完全流程"),
                self._t(
                    "controller.pipeline_message",
                    "系统将按并发数量从写作开始，依次完成图表、选刊作者和智能投稿。",
                ),
                action,
                2,
                1,
                8,
            )
            return
        if action == "pause_full_pipeline":
            self._submit(
                "pause_full_pipeline",
                self._t("pipeline.pausing", "正在暂停全流程自动交接…"),
                self.full_pipeline_runtime.pause,
            )
            return
        if action == "resume_full_pipeline":
            self._submit(
                "resume_full_pipeline",
                self._t("pipeline.resuming", "正在恢复全流程自动交接…"),
                self.full_pipeline_runtime.resume,
            )
            return
        if action == "check_all_submissions":
            self._submit(
                "check_all_submissions",
                self._t("controller.checking_all_submissions", "正在检查全部投稿状态…"),
                lambda: self._run_entitled(
                    ("smart_submission",),
                    self._check_all_submissions,
                ),
            )
            return
        if action == "pause_all":
            self.confirmRequested.emit(
                self._t("controller.stop_title", "紧急停止全部调度"),
                self._t(
                    "controller.stop_message",
                    "正在执行的当前 Step 会安全结束，之后不再发送下一步。确认继续吗？",
                ),
                action,
                0,
            )
            return
        if action == "workspace_switch":
            target = self._pending_workspace_path
            self._pending_workspace_path = ""
            if target:
                self._switch_workspace_now(target)
            return
        if action in {"start_stage", "pause_stage"}:
            if self._page == "overview":
                self.toastRequested.emit(
                    self._t("controller.select_stage", "请先进入具体阶段页面。"),
                    "attention",
                )
                return
            verb = self._t(
                "controller.start" if action == "start_stage" else "controller.pause",
                "启动" if action == "start_stage" else "暂停",
            )
            self.confirmRequested.emit(
                f"{verb}{self.currentPageTitle}",
                self._t(
                    "controller.stage_confirm",
                    "该操作作用于整个“{stage}”队列，不仅是当前选中的论文。确认继续吗？",
                    stage=self.currentPageTitle,
                ),
                action,
                0,
            )
            return

    @Slot(str)
    def prepareSelectedAction(self, action: str) -> None:  # noqa: N802
        action = str(action or "").strip()
        paper_id = self._selected_id
        if not action or not paper_id:
            self.toastRequested.emit(
                self._t("controller.select_paper_first", "请先选择一篇论文。"),
                "attention",
            )
            return
        if action == "open_paper_folder":
            self._open_selected_paper_folder()
            return
        if action == "open_paper_pdf":
            writing = self._selected_record().get("stages", {}).get("writing", {})
            if str(writing.get("prompt_scheme") or "").startswith("undergraduate_"):
                self._open_undergraduate_delivery(preview=True)
            else:
                self._open_selected_paper_pdf()
            return
        if action in {"open_word_delivery", "open_word_preview"}:
            self._open_undergraduate_delivery(preview=action == "open_word_preview")
            return
        if action == "import_writing_data":
            initial = Path(LEGACY_PROJECTS["writing"]["papers_dir"]) / paper_id
            self.fileRequested.emit(
                self._t("controller.import_data_title", "选择实验数据导入规范"),
                action,
                QUrl.fromLocalFile(str(initial)).toString(),
            )
            return
        if action == "retarget_submission":
            self.retargetRequested.emit(paper_id)
            return
        if action == "edit_journal_preferences":
            self._open_journal_preferences(paper_id)
            return
        if action == "apply_author_template":
            self._open_author_template_apply(paper_id)
            return
        title = self._t("controller.selected_action_title", "确认论文操作")
        message = self._t(
            "controller.selected_action_message",
            "论文：{paper}\n\n确认执行“{action}”吗？",
            paper=paper_id,
            action=self._selected_action_label(action),
        )
        if action == "confirm_writing_data" and self._undergraduate_verification_for_paper(paper_id):
            message += ("\n\n请仅在已逐项用真实调研/实验数据替换并核对【验】项目后确认。"
                        "正式仿真须有真实执行结果并保留仿真说明。此按钮记录你的核验确认，"
                        "不是让 AI 自动把模拟数值认定为真实；未完成核验请取消。"
                        "\n可保留【验】标记，由第6步统一清理；确认后修改稿件需重新确认。")
        if action == "archive_paper":
            title = self._t("controller.archive_title", "归档整篇论文")
            message = self._t(
                "controller.archive_message",
                "将从所有阶段移除 {paper}，文件会保存到可恢复的归档包。确认继续吗？",
                paper=paper_id,
            )
        elif action == "purge_paper":
            title = self._t("controller.purge_title", "彻底删除整篇论文")
            message = self._t(
                "controller.purge_message",
                "将永久删除 {paper} 在所有阶段、投稿记录和归档中的文件。此操作不可恢复，确认继续吗？",
                paper=paper_id,
            )
        elif action.startswith("force_stage:"):
            target = action.split(":", 1)[1]
            title = self._t("controller.force_stage_title", "人工放行到下一阶段")
            message = self._t(
                "controller.force_stage_message",
                "将人工放行 {paper} 进入“{stage}”。系统会记录越级原因；上游产物不完整时仍可能在下游失败。确认继续吗？",
                paper=paper_id,
                stage=stage_label(target, self._t),
            )
        self.confirmRequested.emit(title, message, f"selected::{action}", 0)

    def _selected_action_label(self, action: str) -> str:
        for item in self.selectedActions:
            if str(item.get("key") or "") == action:
                return str(item.get("label") or action)
        return action

    @Slot(str, int)
    def executeAction(self, action: str, value: int = 0) -> None:  # noqa: N802
        action = str(action or "")
        if action.startswith("selected::"):
            self.executeSelectedAction(action.split("::", 1)[1])
            return
        if action == "new_writing":
            from ..writing_prompt_schemes import is_undergraduate
            store = getattr(self.orchestrator, "writing_workflow_store", None)
            if store is not None and is_undergraduate(store.active_snapshot()):
                self.prepareAction(action)
                return
            count = max(1, min(8, int(value or 1)))
            self._submit(
                "new_writing",
                self._t(
                    "controller.creating_writing",
                    "正在创建 {count} 篇写作任务…",
                    count=count,
                ),
                lambda: self._run_entitled(
                    ("writing",),
                    lambda: self.orchestrator.start_new_writing_sessions(
                        count,
                        single_active=True,
                    ),
                    requested_capacity=count,
                ),
            )
            return
        if action == "full_pipeline":
            count = max(1, min(8, int(value or 1)))
            gate = self.orchestrator.writing_gate_policy()
            if str(gate.get("mode") or "").strip().casefold() != "auto_trust":
                self.fullPipelineTrustRequested.emit(
                    count,
                    bool(gate.get("password_configured")),
                )
                return
            self._submit(
                "full_pipeline",
                self._t(
                    "controller.starting_pipeline",
                    "正在启动 {count} 篇一键全流程…",
                    count=count,
                ),
                lambda: self._run_entitled(
                    ("writing", "figures", "journal_author", "smart_submission"),
                    lambda: self.full_pipeline_runtime.start_new(count),
                    requested_capacity=count,
                ),
            )
            return
        if action == "pause_all":
            self._submit(
                "pause_all",
                self._t("controller.pausing_all", "正在安全暂停全部调度…"),
                self._pause_everything,
                priority=True,
            )
            return
        if action == "start_stage" and self._page != "overview":
            stage = self._page
            if stage == "submission":
                self._submit(
                    "start_stage",
                    self._t(
                        "controller.starting_smart",
                        "正在准备并启动智能投稿…",
                    ),
                    lambda: self._run_entitled(
                        ("smart_submission",),
                        self._start_smart_submission_stage,
                        requested_capacity=self._configured_stage_parallel(
                            "submission"
                        ),
                    ),
                )
                return
            self._submit(
                "start_stage",
                self._t(
                    "controller.starting_stage",
                    "正在启动{stage}…",
                    stage=stage_label(stage, self._t),
                ),
                lambda: self._run_entitled(
                    (stage,),
                    lambda: self.orchestrator.start_stage(
                        stage,
                        single_active=False,
                    ),
                    requested_capacity=self._configured_stage_parallel(stage),
                ),
            )
            return
        if action == "pause_stage" and self._page != "overview":
            stage = self._page
            if stage == "submission":
                self._submit(
                    "pause_stage",
                    self._t(
                        "controller.pausing_smart",
                        "正在暂停智能投稿…",
                    ),
                    self._pause_smart_submission,
                )
                return
            self._submit(
                "pause_stage",
                self._t(
                    "controller.pausing_stage",
                    "正在暂停{stage}…",
                    stage=stage_label(stage, self._t),
                ),
                lambda: self.orchestrator.pause_stage(stage),
            )

    @Slot(str)
    def executeSelectedAction(self, action: str) -> None:  # noqa: N802
        action = str(action or "").strip()
        paper_id = self._selected_id
        if not action or not paper_id:
            self.toastRequested.emit(
                self._t("controller.select_paper_first", "请先选择一篇论文。"),
                "attention",
            )
            return
        label = self._selected_action_label(action)
        self._submit(
            f"selected:{action}",
            self._t(
                "controller.running_selected_action",
                "正在处理 {paper}：{action}…",
                paper=paper_id,
                action=label,
            ),
            lambda: self._run_selected_action(paper_id, action),
        )

    @Slot(str, str)
    def executeSelectedPathAction(self, action: str, file_url: str) -> None:  # noqa: N802
        action = str(action or "").strip()
        path = QUrl(str(file_url or "")).toLocalFile() or str(file_url or "")
        paper_id = self._selected_id
        if action != "import_writing_data" or not paper_id or not path:
            return
        self._submit(
            "selected:import_writing_data",
            self._t("controller.importing_data", "正在检查并导入实验数据…"),
            lambda: self._run_entitled(
                ("writing",),
                lambda: self._import_writing_data(paper_id, Path(path)),
            ),
        )

    @Slot(str, str, str)
    def requestRetarget(self, mode: str, target_journal: str, publication_tier: str) -> None:  # noqa: N802
        paper_id = self._selected_id
        if not paper_id:
            return
        clean_mode = str(mode or "ai").strip().casefold()
        clean_tier = str(publication_tier or "sci").strip().casefold()
        target = str(target_journal or "").strip()
        self._submit(
            "selected:retarget_submission",
            self._t(
                "controller.requesting_retarget",
                "正在为 {paper} 建立安全转投任务…",
                paper=paper_id,
            ),
            lambda: self._run_entitled(
                ("smart_submission",),
                lambda: self._request_retarget(
                    paper_id,
                    clean_mode,
                    target,
                    clean_tier,
                ),
            ),
        )

    def _run_selected_action(self, paper_id: str, action: str) -> Any:
        entitlement = self._selected_action_entitlement(action)
        if entitlement:
            self.commercial.require_feature(entitlement)
        if action == "continue_writing":
            return self.orchestrator.resume_writing_papers([paper_id], single_active=False)
        if action == "confirm_writing_data":
            return self._confirm_manual_writing_data(paper_id)
        if action == "run_figures":
            return self.orchestrator.queue_figures_from_step1([paper_id])
        if action == "continue_figure_step3":
            return self.orchestrator.queue_figures_from_step3([paper_id])
        if action == "revalidate_figures":
            return self.orchestrator.revalidate_figure_papers([paper_id])
        if action == "pause_figures":
            return self.orchestrator.pause_figure_papers([paper_id])
        if action == "resume_figures":
            return self.orchestrator.resume_figure_papers([paper_id])
        if action == "run_journal_full":
            return self.orchestrator.run_journal_author_papers([paper_id], run_mode="full")
        if action == "run_journal_selection":
            return self.orchestrator.run_journal_author_papers([paper_id], run_mode="selection")
        if action == "run_journal_package":
            return self.orchestrator.run_journal_author_papers([paper_id], run_mode="package")
        if action == "revalidate_journal":
            return self.orchestrator.revalidate_journal_author_papers([paper_id])
        if action == "pause_journal":
            return self.orchestrator.pause_journal_author_papers([paper_id])
        if action == "resume_journal":
            return self.orchestrator.resume_journal_author_papers([paper_id])
        if action == "apply_default_author_template":
            templates = self.orchestrator.author_templates()
            template_id = str(templates.get("default_template_id") or "template_1")
            return self.orchestrator.apply_author_template([paper_id], template_id, overwrite=True)
        if action == "start_selected_submission":
            return self._start_selected_smart_submission(paper_id)
        if action == "check_selected_submission":
            from ..smart_submission import run_smart_submission_daily_monitoring

            return run_smart_submission_daily_monitoring(
                force=True,
                trigger="qt_manual_selected",
                selected_ids=[paper_id],
            )
        if action == "manual_takeover":
            return self._start_manual_takeover(paper_id)
        if action == "manual_takeover_done":
            return self._complete_manual_takeover(paper_id)
        if action == "resume_submission_login":
            return self._resume_smart_login(paper_id)
        if action.startswith("force_stage:"):
            target = action.split(":", 1)[1]
            return self.orchestrator.force_stage_admission(
                [paper_id],
                target,
                reason="用户在新版工作台中明确人工放行",
            )
        if action == "archive_paper":
            return self.orchestrator.archive_papers([paper_id], force_active=False)
        if action == "purge_paper":
            return self.orchestrator.purge_papers([paper_id], force_active=False)
        raise ValueError(f"unsupported selected action: {action}")

    @staticmethod
    def _selected_action_entitlement(action: str) -> str:
        if action in {"continue_writing", "confirm_writing_data"}:
            return "writing"
        if action in {
            "run_figures",
            "continue_figure_step3",
            "revalidate_figures",
            "resume_figures",
        }:
            return "figures"
        if action in {
            "run_journal_full",
            "run_journal_selection",
            "run_journal_package",
            "revalidate_journal",
            "resume_journal",
        }:
            return "journal_author"
        if action in {
            "start_selected_submission",
            "check_selected_submission",
            "manual_takeover_done",
            "resume_submission_login",
        }:
            return "smart_submission"
        return ""

    def _open_journal_preferences(self, paper_id: str) -> None:
        try:
            preferences = self.orchestrator.journal_preferences(paper_id)
        except Exception as exc:
            self.errorRequested.emit(
                self._t("journal_preferences.open_failed", "选刊偏好未能打开"),
                str(exc),
                traceback.format_exc(),
            )
            return
        self.journalPreferencesRequested.emit(paper_id, preferences)

    @Slot(
        str,
        bool,
        bool,
        bool,
        bool,
        bool,
        bool,
        bool,
        str,
        str,
        bool,
        str,
        str,
        str,
        str,
        str,
    )
    def saveJournalPreferences(  # noqa: N802
        self,
        paper_id: str,
        q1: bool,
        q2: bool,
        q3: bool,
        q4: bool,
        scie: bool,
        esci: bool,
        scopus: bool,
        strategy: str,
        oa_policy: str,
        non_oa_required: bool,
        max_apc: str,
        keywords: str,
        preferred_journals: str,
        rejected_journals: str,
        notes: str,
    ) -> None:
        value = {
            "quartiles": {"Q1": q1, "Q2": q2, "Q3": q3, "Q4": q4},
            "indexing": {"SCIE": scie, "ESCI": esci, "Scopus": scopus},
            "strategy": str(strategy or ""),
            "oa_policy": str(oa_policy or "subscription_first"),
            "non_oa_required": bool(non_oa_required),
            "max_apc": str(max_apc or ""),
            "keywords": str(keywords or ""),
            "preferred_journals": str(preferred_journals or ""),
            "rejected_journals": str(rejected_journals or ""),
            "notes": str(notes or ""),
        }
        clean_id = str(paper_id or self._selected_id).strip()
        self._submit(
            f"selected:journal_preferences:{clean_id}",
            self._t("journal_preferences.saving", "正在保存选刊偏好…"),
            lambda: self.orchestrator.save_journal_preferences(clean_id, value),
        )

    def _open_author_template_apply(self, paper_id: str) -> None:
        try:
            data = self.orchestrator.author_templates()
            templates = [
                {
                    "templateId": str(item.get("id") or ""),
                    "name": readable(item.get("name"), str(item.get("id") or "")),
                    "status": str(
                        (item.get("form") if isinstance(item.get("form"), dict) else {}).get("status")
                        or "DRAFT"
                    ),
                    "isDefault": bool(item.get("is_default")),
                }
                for item in data.get("templates", [])
                if isinstance(item, dict)
            ]
            default_id = str(data.get("default_template_id") or "")
        except Exception as exc:
            self.errorRequested.emit(
                self._t("settings.author_open_failed", "作者模板未能打开"),
                str(exc),
                traceback.format_exc(),
            )
            return
        self.authorTemplateApplyRequested.emit(paper_id, templates, default_id)

    @Slot(str, str, bool)
    def applyAuthorTemplate(self, paper_id: str, template_id: str, overwrite: bool) -> None:  # noqa: N802
        clean_id = str(paper_id or self._selected_id).strip()
        clean_template = str(template_id or "").strip()
        self._submit(
            f"selected:author_template:{clean_id}",
            self._t("settings.applying_author", "正在套用作者资料模板…"),
            lambda: self.orchestrator.apply_author_template(
                [clean_id],
                clean_template,
                overwrite=bool(overwrite),
            ),
        )

    def _undergraduate_verification_for_paper(self, paper_id: str) -> bool:
        from ..undergraduate_verification import uses_undergraduate_verification
        adapter = getattr(self.orchestrator, "adapters", {}).get("writing")
        if adapter is None:
            return False
        session = adapter.load_state().get("sessions", {}).get(paper_id, {})
        return uses_undergraduate_verification(session.get("workflow_snapshot"))

    def _confirm_manual_writing_data(self, paper_id: str) -> dict[str, Any]:
        policy = self.orchestrator.writing_gate_policy()
        if (self._undergraduate_verification_for_paper(paper_id)
                or str(policy.get("mode") or "").strip().casefold() != "auto_trust"):
            papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
            preview = prepare_manual_writing_evidence(papers_dir, paper_id, check_only=True)
            if preview.get("ready") is not True or preview.get("transition_ready") is not True:
                raise RuntimeError(
                    str(preview.get("message") or preview.get("reason") or "手动修改的数据尚未通过现有门禁")
                )
            committed = prepare_manual_writing_evidence(
                papers_dir,
                paper_id,
                check_only=False,
                expected_draft_sha256=str(preview.get("draft_v2_sha256") or ""),
            )
            if not committed.get("ok") or not committed.get("transition_ready"):
                raise RuntimeError(
                    str(committed.get("message") or committed.get("reason") or "实验数据确认未能写入")
                )
        return self.orchestrator.resume_writing_papers([paper_id], single_active=False)

    def _import_writing_data(self, paper_id: str, spec_path: Path) -> dict[str, Any]:
        papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
        preview = prepare_writing_evidence_import(
            papers_dir,
            paper_id,
            spec_path,
            check_only=True,
        )
        if not preview.get("ok"):
            raise RuntimeError(str(preview.get("message") or preview.get("reason") or "实验数据导入预检失败"))
        result = prepare_writing_evidence_import(
            papers_dir,
            paper_id,
            spec_path,
            check_only=False,
            expected_manifest_sha256=str(preview.get("manifest_sha256") or ""),
        )
        if not result.get("ok") or not result.get("transition_ready"):
            raise RuntimeError(str(result.get("message") or result.get("reason") or "实验数据尚未就绪"))
        return self.orchestrator.resume_writing_papers([paper_id], single_active=False)

    def _start_selected_smart_submission(self, paper_id: str) -> dict[str, Any]:
        from ..smart_submission import (
            prepare_smart_submission_sdk_papers,
            start_smart_submission_program_runtime,
        )

        prepared = prepare_smart_submission_sdk_papers(
            [paper_id],
            orchestrator=self.orchestrator,
        )
        runnable = list(
            dict.fromkeys([*prepared.get("queued", []), *prepared.get("resumed", [])])
        )
        if not runnable:
            skipped = prepared.get("skipped", {})
            raise RuntimeError(
                "; ".join(f"{key}: {value}" for key, value in skipped.items())
                if isinstance(skipped, dict) and skipped
                else str(prepared.get("code") or "当前论文未能进入智能投稿队列")
            )
        return {
            "prepared": prepared,
            "started": start_smart_submission_program_runtime(runnable),
        }

    @staticmethod
    def _check_all_submissions() -> dict[str, Any]:
        from ..smart_submission import run_smart_submission_daily_monitoring

        return run_smart_submission_daily_monitoring(
            force=True,
            trigger="qt_manual_all",
        )

    @staticmethod
    def _request_retarget(
        paper_id: str,
        mode: str,
        target_journal: str,
        publication_tier: str,
    ) -> dict[str, Any]:
        from ..smart_submission import request_smart_submission_retarget

        return request_smart_submission_retarget(
            paper_id,
            mode=mode,
            target_journal=target_journal,
            publication_tier=publication_tier,
            priority="acceptance",
            withdrawal_policy="auto",
            activate=True,
        )

    @staticmethod
    def _start_manual_takeover(paper_id: str) -> dict[str, Any]:
        from ..smart_submission import (
            open_smart_manual_takeover_page,
            request_smart_manual_takeover,
        )

        requested = request_smart_manual_takeover([paper_id])
        opened = open_smart_manual_takeover_page(paper_id)
        if not opened.get("ok") and not opened.get("pending"):
            raise RuntimeError(str(opened.get("message") or opened.get("code") or "无法打开人工修改页面"))
        return {"requested": requested, "opened": opened}

    @staticmethod
    def _complete_manual_takeover(paper_id: str) -> dict[str, Any]:
        from ..smart_submission import (
            complete_smart_manual_takeover,
            start_smart_submission_program_runtime,
        )

        completed = complete_smart_manual_takeover([paper_id])
        resumed = [str(item) for item in completed.get("resumed", []) if str(item)]
        if not resumed:
            skipped = completed.get("skipped", {})
            raise RuntimeError(
                "; ".join(f"{key}: {value}" for key, value in skipped.items())
                if isinstance(skipped, dict) and skipped
                else "人工修改页面尚未完成或无法验证"
            )
        return {
            "completed": completed,
            "started": start_smart_submission_program_runtime(resumed),
        }

    @staticmethod
    def _resume_smart_login(paper_id: str) -> dict[str, Any]:
        from ..smart_submission import (
            resume_smart_credential_broker,
            start_smart_submission_program_runtime,
        )

        resumed = resume_smart_credential_broker([paper_id])
        ids = [str(item) for item in resumed.get("resumed", []) if str(item)]
        if not ids:
            skipped = resumed.get("skipped", {})
            raise RuntimeError(
                "; ".join(f"{key}: {value}" for key, value in skipped.items())
                if isinstance(skipped, dict) and skipped
                else "登录/验证任务未能恢复"
            )
        return {"resumed": resumed, "started": start_smart_submission_program_runtime(ids)}

    def _open_undergraduate_delivery(self, *, preview: bool) -> None:
        from ..undergraduate_word import paper_workflow, validate_word_delivery, safe_path
        from ..io_utils import read_json
        root = Path(LEGACY_PROJECTS["figures"]["root"])
        workflow = paper_workflow(root, self._selected_id)
        issues, _ = validate_word_delivery(root, self._selected_id, workflow)
        if issues:
            self.toastRequested.emit("Word 正式交付尚未通过：" + "；".join(issues), "attention")
            return
        receipt = read_json(root / "papers" / self._selected_id / "delivery" / "word_delivery.json", {})
        try:
            target = safe_path(root, receipt["pdf" if preview else "docx"]["path"])
            os.startfile(str(target))
        except Exception as exc:
            self.errorRequested.emit("无法打开 Word 交付文件", str(exc), traceback.format_exc())

    def _open_selected_paper_folder(self) -> None:
        from .paper_files import paper_output_directory
        roots = {stage: Path(project["root"]) for stage, project in LEGACY_PROJECTS.items()}
        target = paper_output_directory(roots, self._selected_id, self._page, self._selected_record())
        if target is None:
            self.toastRequested.emit(
                self._t("controller.paper_folder_missing", "没有找到这篇论文的工作目录。"),
                "attention",
            )
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            self.errorRequested.emit(
                self._t("controller.open_folder_failed", "无法打开论文目录"),
                str(exc),
                traceback.format_exc(),
            )

    def _open_selected_paper_pdf(self) -> None:
        from .paper_files import paper_pdf
        roots = {stage: Path(project["root"]) for stage, project in LEGACY_PROJECTS.items()}
        target = paper_pdf(roots, self._selected_id, self._page, self._selected_record())
        if target is None:
            self.toastRequested.emit(self._t("controller.paper_pdf_missing",
                "这篇论文尚未生成可打开的论文 PDF，请先完成图表阶段的编译与验收。"), "attention")
            return
        try:
            os.startfile(str(target))
        except Exception as exc:
            self.errorRequested.emit(self._t("controller.open_pdf_failed", "无法打开论文 PDF"),
                                     str(exc), traceback.format_exc())

    def _submission_candidate_ids(self) -> list[str]:
        result: list[str] = []
        papers = self._state.get("papers", {})
        if not isinstance(papers, dict):
            return result
        terminal = {"accepted", "complete", "monitoring", "submitted"}
        for paper_id, record in papers.items():
            if not isinstance(record, dict):
                continue
            stages = record.get("stages", {})
            submission = stages.get("submission", {}) if isinstance(stages, dict) else {}
            status = str(
                submission.get("status") if isinstance(submission, dict) else ""
            ).strip().casefold()
            if status in terminal:
                continue
            if (
                str(record.get("current_stage") or "") == "submission"
                or str(record.get("next_action") or "") == "queue_submission"
                or status not in {"", "missing"}
            ):
                result.append(str(paper_id))
        return result

    def _start_smart_submission_stage(self) -> dict[str, Any]:
        from ..smart_submission import (
            prepare_smart_submission_sdk_papers,
            start_smart_submission_program_runtime,
        )

        paper_ids = self._submission_candidate_ids()
        if not paper_ids:
            raise RuntimeError(
                self._t(
                    "controller.smart_no_candidates",
                    "当前没有可进入智能投稿的论文。",
                )
            )
        prepared = prepare_smart_submission_sdk_papers(
            paper_ids,
            orchestrator=self.orchestrator,
        )
        runnable = list(
            dict.fromkeys(
                [
                    *prepared.get("queued", []),
                    *prepared.get("resumed", []),
                ]
            )
        )
        if not runnable:
            code = str(prepared.get("code") or "SMART_SDK_NO_ONLINE_READY_PAPERS")
            skipped = prepared.get("skipped", {})
            detail = "; ".join(
                f"{paper_id}: {reason}"
                for paper_id, reason in (
                    skipped.items() if isinstance(skipped, dict) else []
                )
            )
            raise RuntimeError(f"{code}{': ' + detail if detail else ''}")
        started = start_smart_submission_program_runtime(runnable)
        return {"ok": bool(started.get("ok", True)), "prepared": prepared, "started": started}

    @staticmethod
    def _pause_smart_submission() -> dict[str, Any]:
        from ..smart_submission import pause_smart_submission_automation
        from ..smart_submission_sdk_runtime import pause_smart_submission_sdk_after_current

        return {
            "program": pause_smart_submission_automation(reason="qt_ui_stage_pause"),
            "sdk": pause_smart_submission_sdk_after_current(),
        }

    def _pause_everything(self) -> dict[str, Any]:
        result: dict[str, Any] = {"stage_runtimes": self.orchestrator.pause_all()}
        try:
            result["pipeline"] = self.full_pipeline_runtime.pause()
        except Exception as exc:
            result["pipeline"] = {"ok": False, "error": type(exc).__name__}
        try:
            result["smart_submission"] = self._pause_smart_submission()
        except Exception as exc:
            result["smart"] = {"ok": False, "error": type(exc).__name__}
        return result

    @Slot()
    def openWorkspace(self) -> None:  # noqa: N802
        try:
            os.startfile(str(WORKSPACE_ROOT))  # type: ignore[attr-defined]
        except Exception:
            LOGGER.exception("Could not open workspace directory")
            self.toastRequested.emit(
                self._t("controller.workspace_failed", "工作区目录未能打开。"),
                "danger",
            )

    def _reload_registered_workspaces(self) -> None:
        try:
            rows = list_registered_workspaces()
        except Exception:
            LOGGER.exception("Could not list registered workspaces")
            rows = []
        current_path = str(WORKSPACE_ROOT).casefold()
        normalized: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for row in rows:
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            seen_paths.add(path.casefold())
            normalized.append(
                {
                    "workspaceId": str(row.get("workspace_id") or ""),
                    "name": str(row.get("name") or Path(path).name),
                    "path": path,
                    "active": path.casefold() == current_path,
                    "exists": bool(row.get("exists")),
                    "valid": bool(row.get("manifest_exists")),
                }
            )
        if current_path not in seen_paths:
            normalized.insert(
                0,
                {
                    "workspaceId": str(getattr(ACTIVE_WORKSPACE, "workspace_id", "")),
                    "name": str(WORKSPACE_NAME),
                    "path": str(WORKSPACE_ROOT),
                    "active": True,
                    "exists": Path(WORKSPACE_ROOT).is_dir(),
                    "valid": getattr(ACTIVE_WORKSPACE, "manifest_path", None) is not None,
                },
            )
        self._registered_workspaces = normalized
        self.workspaceListChanged.emit()

    @Slot()
    def openWorkspaceManager(self) -> None:  # noqa: N802
        self._reload_registered_workspaces()
        self.workspaceManagerRequested.emit()

    @Slot(str)
    def requestWorkspaceSwitch(self, path: str) -> None:  # noqa: N802
        target = str(path or "").strip()
        if not target or target.casefold() == str(WORKSPACE_ROOT).casefold():
            return
        self._pending_workspace_path = target
        self.confirmRequested.emit(
            self._t("workspace.switch_title", "切换论文工作区"),
            self._t(
                "workspace.switch_message",
                "将切换到：\n{path}\n\n工作台会自动重启。当前正在执行的 Step 不会被强制中断，但本窗口的调度器会安全退出。",
                path=target,
            ),
            "workspace_switch",
            0,
        )

    @Slot(str)
    def registerWorkspacePath(self, folder_url: str) -> None:  # noqa: N802
        path = QUrl(str(folder_url or "")).toLocalFile() or str(folder_url or "")
        if not path:
            return
        self._submit(
            "register_workspace",
            self._t("workspace.registering", "正在注册论文工作区…"),
            lambda: self._register_workspace(path),
        )

    @Slot(str, str, bool)
    def createWorkspaceAt(self, parent_url: str, name: str, import_current: bool) -> None:  # noqa: N802
        parent = QUrl(str(parent_url or "")).toLocalFile() or str(parent_url or "")
        clean_name = " ".join(str(name or "").split())
        if not parent or not clean_name:
            self.errorRequested.emit(
                self._t("workspace.create_failed", "无法创建工作区"),
                self._t("workspace.name_required", "请选择父目录并填写工作区名称。"),
                "",
            )
            return
        target = suggested_workspace_directory(parent, clean_name)
        self._submit(
            "create_workspace",
            self._t("workspace.creating", "正在创建论文工作区…"),
            lambda: self._create_workspace(target, clean_name, bool(import_current)),
        )

    def _register_workspace(self, path: str) -> dict[str, Any]:
        descriptor = load_workspace(path)
        register_workspace(descriptor, make_active=False)
        return {
            "ok": True,
            "workspace": {
                "workspace_id": descriptor.workspace_id,
                "name": descriptor.name,
                "path": str(descriptor.root),
            },
        }

    def _create_workspace(
        self,
        target: Path,
        name: str,
        import_current: bool,
    ) -> dict[str, Any]:
        descriptor = create_workspace(
            target,
            name,
            source=ACTIVE_WORKSPACE,
            import_data=import_current,
            make_active=False,
        )
        return {
            "ok": True,
            "workspace": {
                "workspace_id": descriptor.workspace_id,
                "name": descriptor.name,
                "path": str(descriptor.root),
            },
        }

    def _switch_workspace_now(self, path: str) -> None:
        try:
            descriptor = set_active_workspace(path)
        except Exception as exc:
            LOGGER.exception("Could not switch workspace")
            self.errorRequested.emit(
                self._t("workspace.switch_failed", "工作区切换失败"),
                str(exc),
                traceback.format_exc(),
            )
            return
        try:
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--workspace", str(descriptor.root)]
                launch_directory = descriptor.root
            else:
                command = [
                    sys.executable,
                    "-m",
                    "unified_paper_orchestrator",
                    "--workspace",
                    str(descriptor.root),
                    "ui",
                ]
                launch_directory = Path(__file__).resolve().parents[2]
            child_environment = os.environ.copy()
            child_environment[WORKSPACE_ENV] = str(descriptor.root)
            child_environment["UNIFIED_PAPER_UI_LOCALE"] = str(self.i18n.locale)
            subprocess.Popen(
                command,
                cwd=str(launch_directory),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                close_fds=True,
            )
        except Exception as exc:
            LOGGER.exception("Could not restart after workspace switch")
            self.errorRequested.emit(
                self._t("workspace.restart_failed", "工作区已切换，但工作台未能自动重启"),
                str(exc),
                traceback.format_exc(),
            )
            return
        self.quitRequested.emit()

    @Slot()
    def openSettings(self) -> None:  # noqa: N802
        self._submit(
            "load_settings",
            self._t("settings.loading", "正在读取设置…"),
            self._load_settings_bundle,
        )

    def _refresh_settings_data(self) -> None:
        self._submit(
            "refresh_settings",
            "",
            self._load_settings_bundle,
            silent=True,
        )

    @Slot(str, result="QVariantMap")
    def undergraduateScheme(self, identifier):  # noqa: N802
        try:
            if identifier not in {"undergraduate_humanities", "undergraduate_software"}:
                raise ValueError("请选择本科论文类型")
            snapshot = self.orchestrator.writing_workflow_store.executable_snapshot(identifier)
            return {"version": snapshot["version_id"], "error": ""}
        except ValueError as exc:
            return {"version": "", "error": str(exc)}

    @Slot(str, result=str)
    def previewUndergraduateFigures(self, identifier):  # noqa: N802
        from ..undergraduate_figures import figure_scheme_steps
        try:
            return "\n\n".join(step["prompt_text"] for step in figure_scheme_steps(identifier))
        except ValueError as exc:
            return str(exc)

    @Slot(str, str, str, str, str)
    def createUndergraduateProject(self, title, requirements, minimum, identifier, version):  # noqa: N802
        from ..writing_prompt_schemes import normalize_thesis_brief
        try:
            if identifier not in {"undergraduate_humanities", "undergraduate_software"}:
                raise ValueError("请选择本科论文类型")
            brief = normalize_thesis_brief({"title": title, "requirements": requirements,
                "minimum_references": minimum, "attachments": self._undergraduate_materials.snapshot()}, required=True)
        except (ValueError, OSError) as exc:
            self.errorRequested.emit("毕业论文资料需要检查", str(exc), "")
            return
        self._submit("new_undergraduate", "正在创建毕业论文…", lambda: self._run_entitled(
            ("writing",), lambda: self.orchestrator.start_new_writing_sessions(1, single_active=False,
                request_context={"thesis_brief": brief, "prompt_scheme": identifier,
                                 "expected_workflow_version": version}), requested_capacity=1))

    @Slot(str, str, str, str)
    def startUndergraduateWriting(self, title: str, requirements: str, minimum_references: str,
                                  version_id: str) -> None:  # noqa: N802
        from ..writing_prompt_schemes import normalize_thesis_brief
        try:
            brief = normalize_thesis_brief({"title": title, "requirements": requirements,
                                            "minimum_references": minimum_references}, required=True)
        except ValueError as exc:
            self.errorRequested.emit("本科论文输入未完成", str(exc), "")
            return
        self._submit("new_writing", "正在创建本科论文写作任务…", lambda: self._run_entitled(
            ("writing",), lambda: self.orchestrator.start_new_writing_sessions(
                1, single_active=False, request_context={"thesis_brief": brief,
                                                       "expected_workflow_version": version_id}),
            requested_capacity=1,
        ))

    def _load_settings_bundle(self) -> dict[str, Any]:
        stages: list[dict[str, Any]] = []
        workflow_keys = {
            "writing": "writing_workflow",
            "figures": "figure_workflow",
            "journal_author": "journal_author_workflow",
        }
        limits = {"writing": 10, "figures": 4, "journal_author": 6}
        try:
            commercial_parallel_limit = self.commercial.concurrent_paper_limit()
        except Exception:
            commercial_parallel_limit = None
        if commercial_parallel_limit is not None:
            limits = {
                stage: min(limit, commercial_parallel_limit)
                for stage, limit in limits.items()
            }
        for stage in ("writing", "figures", "journal_author"):
            inventory = self.orchestrator.stage_prompt_inventory(stage)
            settings = inventory.get("settings", {}) if isinstance(inventory.get("settings"), dict) else {}
            workflow = inventory.get(workflow_keys[stage], {})
            workflow = workflow if isinstance(workflow, dict) else {}
            default_profile = (
                workflow.get("default_execution_profile", {})
                if isinstance(workflow.get("default_execution_profile"), dict)
                else {}
            )
            steps: list[dict[str, Any]] = []
            for item in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
                if not isinstance(item, dict):
                    continue
                profile = item.get("execution_profile", {}) if isinstance(item.get("execution_profile"), dict) else {}
                effective = (
                    item.get("effective_execution_profile", {})
                    if isinstance(item.get("effective_execution_profile"), dict)
                    else {}
                )
                prompt_path = str(item.get("prompt_path") or "")
                fallback_name = (
                    Path(prompt_path).stem.replace("_", " ")
                    if prompt_path
                    else f"Step {item.get('position') or len(steps) + 1}"
                )
                steps.append(
                    {
                        "position": int(item.get("position") or len(steps) + 1),
                        "stepId": str(item.get("step_id") or ""),
                        "name": readable(item.get("name"), fallback_name),
                        "kind": str(item.get("kind") or "builtin"),
                        "promptPath": prompt_path,
                        "model": str(profile.get("model") or ""),
                        "effectiveModel": str(effective.get("model") or default_profile.get("model") or ""),
                        "reasoning": str(profile.get("reasoning_effort") or ""),
                        "tokenLimit": int(profile.get("hard_token_limit") or 0),
                    }
                )
            stages.append(
                {
                    "key": stage,
                    "label": stage_label(stage, self._t),
                    "parallel": int(settings.get("max_parallel_workers") or 1),
                    "parallelMax": limits[stage],
                    "schedulingMode": str(settings.get("scheduling_mode") or "manual"),
                    "defaultModel": str(default_profile.get("model") or ""),
                    "defaultReasoning": str(default_profile.get("reasoning_effort") or ""),
                    "stepCount": int(workflow.get("step_count") or len(steps)),
                    "workflowMode": str(workflow.get("mode") or "standard"),
                    "promptScheme": str(workflow.get("prompt_scheme") or "journal"),
                    "promptSchemes": workflow.get("prompt_schemes", []),
                    "steps": steps,
                }
            )
        gate = self.orchestrator.writing_gate_policy()
        try:
            smart_state = json.loads(Path(SMART_SUBMISSION_STATE_PATH).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            smart_state = {}
        smart_settings = (
            smart_state.get("settings", {})
            if isinstance(smart_state, dict) and isinstance(smart_state.get("settings"), dict)
            else {}
        )
        author_data = self.orchestrator.author_templates()
        authors: list[dict[str, Any]] = []
        for index, item in enumerate(
            author_data.get("templates", []) if isinstance(author_data.get("templates"), list) else []
        ):
            if not isinstance(item, dict):
                continue
            form = item.get("form", {}) if isinstance(item.get("form"), dict) else {}
            authors.append(
                {
                    "templateId": str(item.get("id") or ""),
                    "name": readable(item.get("name"), f"Template {index + 1}"),
                    "status": str(form.get("status") or "DRAFT"),
                    "isDefault": bool(item.get("is_default")),
                }
            )
        commercial_status = self.commercial.status().to_dict()
        try:
            commercial_settings = self.commercial.settings()
            commercial_status.update(
                {
                    "developmentMode": commercial_settings.development_mode,
                    "developmentPublicKeyPath": (
                        commercial_settings.development_public_key_path
                    ),
                }
            )
        except Exception:
            commercial_status.update(
                {
                    "developmentMode": False,
                    "developmentPublicKeyPath": "",
                }
            )
        commercial_status["account"] = dict(self._commercial_account)
        return {
            "stages": stages,
            "writingGate": {
                "mode": str(gate.get("mode") or "strict"),
                "passwordConfigured": bool(gate.get("password_configured")),
                "revision": int(gate.get("revision") or 0),
            },
            "smartMonitor": {
                "time": str(smart_settings.get("daily_monitor_time_local") or "09:00"),
                "enabled": bool(smart_settings.get("daily_monitor_enabled", True)),
                "lastStatus": str(smart_settings.get("daily_monitor_last_status") or "never_run"),
                "lastFinishedAt": str(smart_settings.get("daily_monitor_last_finished_at") or ""),
            },
            "smartPolicy": {
                "accountMissingPolicy": str(
                    smart_settings.get("account_missing_policy") or "auto_register"
                ),
                "requireTermsConfirmation": bool(
                    smart_settings.get(
                        "account_registration_requires_terms_confirmation",
                        True,
                    )
                ),
                "preferOrcid": str(
                    smart_settings.get("new_account_auth_preference") or "orcid_first"
                )
                == "orcid_first",
                "autoAuthorizeOrcid": bool(
                    smart_settings.get("orcid_auto_authorize_for_login", True)
                ),
                "autoFinalSubmit": bool(
                    smart_settings.get("auto_final_submit_enabled", True)
                ),
            },
            "authorTemplates": authors,
            "commercial": commercial_status,
        }

    @Slot()
    def refreshCodexAccount(self) -> None:  # noqa: N802
        generation = getattr(self, "_codex_account_generation", 0)
        self._submit(
            "codex:status",
            self._t(
                "settings.codex_checking",
                "正在核验本机 Codex SDK 账号…",
            ),
            lambda: {"generation": generation, "accountStatus": self.codex_account.verified_status()},
            priority=True,
            silent=True,
        )

    @Slot()
    def connectCodexAccount(self) -> None:  # noqa: N802
        self._connect_codex_account("browser")

    @Slot()
    def connectCodexDeviceAccount(self) -> None:  # noqa: N802
        self._connect_codex_account("device_code")

    @Slot()
    def cancelCodexLogin(self) -> None:  # noqa: N802
        attempt_id = self._codex_login_attempt
        if not attempt_id:
            return
        self._submit("codex:cancel_login", "", lambda: self.codex_account.cancel_login(attempt_id),
                     priority=True, silent=True)

    def _connect_codex_account(self, method: str) -> None:
        if bool(self._codex_account_status.get("loginPending")):
            return
        self._codex_account_generation = getattr(self, "_codex_account_generation", 0) + 1
        self._publish_codex_account_status(
            self._codex_account_status,
            login_pending=True,
            status_known=bool(
                self._codex_account_status.get("statusKnown", False)
            ),
        )
        self._submit(
            "codex:begin_login",
            self._t(
                "settings.codex_connecting",
                "正在打开 ChatGPT 官方授权页面…",
            ),
            lambda: self._begin_codex_browser_login(method),
            priority=True,
            silent=True,
        )

    @Slot()
    def disconnectCodexAccount(self) -> None:  # noqa: N802
        if bool(self._codex_account_status.get("loginPending")):
            return
        self._codex_account_generation = getattr(self, "_codex_account_generation", 0) + 1
        self._submit(
            "codex:logout",
            self._t(
                "settings.codex_disconnecting",
                "正在断开本机 Codex SDK 账号…",
            ),
            self.codex_account.logout,
            priority=True,
            silent=True,
        )

    @Slot(bool, str, str, bool, bool, str)
    def configureCommercial(  # noqa: N802
        self,
        enabled: bool,
        control_plane_url: str,
        release_channel: str,
        diagnostics_opt_in: bool,
        development_mode: bool,
        development_public_key_path: str,
    ) -> None:
        self._submit(
            "settings:commercial",
            self._t(
                "settings.commercial_saving",
                "正在保存账户、授权与更新设置…",
            ),
            lambda: self.commercial.configure(
                enabled=bool(enabled),
                control_plane_url=str(control_plane_url or "").strip(),
                release_channel=str(release_channel or "stable").strip(),
                diagnostics_opt_in=bool(diagnostics_opt_in),
                development_mode=bool(development_mode),
                development_public_key_path=str(
                    development_public_key_path or ""
                ).strip(),
            ).to_dict(),
        )

    @Slot(bool)
    def updateCommercialPreferences(  # noqa: N802
        self,
        diagnostics_opt_in: bool,
    ) -> None:
        self._submit(
            "settings:commercial_preferences",
            self._t(
                "settings.commercial_saving",
                "Saving account and privacy preferences…",
            ),
            lambda: self.commercial.update_preferences(
                diagnostics_opt_in=bool(diagnostics_opt_in),
            ).to_dict(),
        )

    @Slot(str)
    def requestCommercialLoginCode(self, email: str) -> None:  # noqa: N802
        self._submit(
            "commercial:request_code",
            self._t(
                "settings.commercial_requesting_code",
                "正在请求登录验证码…",
            ),
            lambda: self.commercial.request_login_code(str(email or "").strip()),
        )

    @Slot(str, str)
    def completeCommercialLogin(self, email: str, code: str) -> None:  # noqa: N802
        self._submit(
            "commercial:complete_login",
            self._t(
                "settings.commercial_activating",
                "正在登录并激活此设备…",
            ),
            lambda: self.commercial.complete_login(
                str(email or "").strip(),
                str(code or "").strip(),
            ).to_dict(),
        )

    @Slot()
    def refreshCommercialLicense(self) -> None:  # noqa: N802
        self._submit(
            "commercial:refresh_license",
            self._t(
                "settings.commercial_refreshing",
                "正在刷新签名授权…",
            ),
            lambda: self.commercial.refresh_license().to_dict(),
        )

    @Slot()
    def checkCommercialUpdate(self) -> None:  # noqa: N802
        self._submit(
            "commercial:check_update",
            self._t(
                "settings.commercial_checking_update",
                "正在检查签名更新清单…",
            ),
            self._commercial_update_result,
        )

    def _commercial_update_result(self) -> dict[str, Any]:
        decision = self.commercial.check_for_update()
        manifest = decision.manifest
        return {
            "available": decision.available,
            "mandatory": decision.mandatory,
            "reason": decision.reason,
            "version": manifest.version if manifest else "",
            "channel": manifest.channel if manifest else "",
            "notesUrl": manifest.notes_url if manifest else "",
        }

    @Slot()
    def flushCommercialDiagnostics(self) -> None:  # noqa: N802
        self._submit(
            "commercial:flush_diagnostics",
            self._t(
                "settings.commercial_sending_diagnostics",
                "正在发送已脱敏诊断…",
            ),
            self.commercial.flush_diagnostics,
        )

    @Slot()
    def signOutCommercial(self) -> None:  # noqa: N802
        self._submit(
            "commercial:sign_out",
            self._t("settings.commercial_signing_out", "Signing out…"),
            lambda: self.commercial.sign_out().to_dict(),
        )

    @Slot()
    def refreshCommercialAccount(self) -> None:  # noqa: N802
        if not self.commercial.status().signed_in:
            self._commercial_account = {}
            self._refresh_commercial_status()
            self.settingsDataChanged.emit()
            return
        self._submit(
            "commercial:account_summary",
            self._t("settings.commercial_refreshing_account", "Refreshing account…"),
            self.commercial.account_summary,
            silent=True,
        )

    @Slot(str)
    def revokeCommercialDevice(self, device_id: str) -> None:  # noqa: N802
        clean = str(device_id or "").strip()
        if not clean:
            return
        self._submit(
            "commercial:revoke_device",
            self._t("settings.commercial_revoking_device", "Revoking device…"),
            lambda: self.commercial.revoke_device(clean).to_dict(),
        )

    @Slot(str)
    def openCommercialUrl(self, target: str) -> None:  # noqa: N802
        key = str(target or "").strip()
        status = self._commercial_status
        urls = {
            "account": status.get("accountPortalUrl"),
            "pricing": status.get("pricingUrl"),
            "support": status.get("supportUrl"),
            "privacy": status.get("privacyUrl"),
            "terms": status.get("termsUrl"),
        }
        url = str(urls.get(key) or "").strip()
        if not url or not QDesktopServices.openUrl(QUrl(url)):
            self.toastRequested.emit(
                self._t(
                    "settings.commercial_open_url_failed",
                    "Could not open the requested product page.",
                ),
                "danger",
            )

    @Slot(str, int, str, str)
    def saveStageSettings(
        self,
        stage: str,
        parallel: int,
        default_model: str,
        default_reasoning: str,
    ) -> None:  # noqa: N802
        clean_stage = str(stage or "")
        self._submit(
            f"settings:stage:{clean_stage}",
            self._t("settings.saving", "正在保存设置…"),
            lambda: self._save_stage_settings(
                clean_stage,
                int(parallel),
                str(default_model or "").strip(),
                str(default_reasoning or "").strip(),
            ),
        )

    def _save_stage_settings(
        self,
        stage: str,
        parallel: int,
        model: str,
        reasoning: str,
    ) -> dict[str, Any]:
        validate_profile(self.orchestrator, stage, model, reasoning)
        self.commercial.require_capacity(int(parallel))
        scheduler = self.orchestrator.save_stage_scheduler_settings(
            stage,
            {"max_parallel_workers": int(parallel)},
        )
        profile = {
            "model": model,
            "reasoning_effort": reasoning,
            "hard_token_limit": 0,
            "unavailable_policy": "block",
        }
        methods = {
            "writing": self.orchestrator.update_writing_default_execution_profile,
            "figures": self.orchestrator.update_figure_default_execution_profile,
            "journal_author": self.orchestrator.update_journal_author_default_execution_profile,
        }
        return {"scheduler": scheduler, "profile": methods[stage](profile)}

    @Slot(str, str, str, str, int)
    def saveStepProfile(
        self,
        stage: str,
        step_id: str,
        model: str,
        reasoning: str,
        token_limit: int,
    ) -> None:  # noqa: N802
        clean_stage = str(stage or "")
        clean_step = str(step_id or "")
        self._submit(
            f"settings:step:{clean_stage}:{clean_step}",
            self._t("settings.saving_step", "正在保存 Step 执行配置…"),
            lambda: self._save_step_profile(
                clean_stage,
                clean_step,
                str(model or "").strip(),
                str(reasoning or "").strip(),
                int(token_limit or 0),
            ),
        )

    def _save_step_profile(
        self,
        stage: str,
        step_id: str,
        model: str,
        reasoning: str,
        token_limit: int,
    ) -> dict[str, Any]:
        validate_profile(self.orchestrator, stage, model, reasoning, inherit=True)
        profile = {
            "model": model,
            "reasoning_effort": reasoning,
            "hard_token_limit": max(0, int(token_limit)),
            "unavailable_policy": "block",
        }
        methods = {
            "writing": self.orchestrator.update_writing_step_execution_profile,
            "figures": self.orchestrator.update_figure_step_execution_profile,
            "journal_author": self.orchestrator.update_journal_author_step_execution_profile,
        }
        return methods[stage](step_id, profile)

    @Slot(str, str, str)
    def saveWritingGate(self, mode: str, password: str, confirmation: str) -> None:  # noqa: N802
        self._submit(
            "settings:writing_gate",
            self._t("settings.saving_gate", "正在保存 Step 5 数据关口…"),
            lambda: self.orchestrator.set_writing_gate_policy(
                str(mode or "strict_confirmation"),
                password=str(password or ""),
                password_confirmation=str(confirmation or ""),
            ),
        )

    @Slot(int, str, str)
    def startFullPipelineWithTrust(  # noqa: N802
        self,
        count: int,
        password: str,
        confirmation: str,
    ) -> None:
        normalized_count = max(1, min(8, int(count or 1)))
        self._submit(
            "full_pipeline",
            self._t(
                "controller.starting_pipeline",
                "正在启动 {count} 篇一键全流程…",
                count=normalized_count,
            ),
            lambda: self._enable_trust_and_start_pipeline(
                normalized_count,
                str(password or ""),
                str(confirmation or ""),
            ),
        )

    def _enable_trust_and_start_pipeline(
        self,
        count: int,
        password: str,
        confirmation: str,
    ) -> dict[str, Any]:
        self.commercial.require_features(
            ("writing", "figures", "journal_author", "smart_submission")
        )
        self.commercial.require_capacity(count)
        gate = self.orchestrator.set_writing_gate_policy(
            "auto_trust",
            password=password,
            password_confirmation=confirmation,
        )
        started = self.full_pipeline_runtime.start_new(count)
        return {"gate": gate, "pipeline": started}

    @Slot(str)
    def saveSmartMonitorTime(self, time_local: str) -> None:  # noqa: N802
        from ..smart_submission import update_smart_submission_daily_monitoring_time

        self._submit(
            "settings:smart_monitor",
            self._t("settings.saving_monitor", "正在保存投稿监控时间…"),
            lambda: update_smart_submission_daily_monitoring_time(str(time_local or "")),
        )

    @Slot(str, bool, bool, bool, bool)
    def saveSmartPolicies(  # noqa: N802
        self,
        account_missing_policy: str,
        require_terms_confirmation: bool,
        prefer_orcid: bool,
        auto_authorize_orcid: bool,
        auto_final_submit: bool,
    ) -> None:
        self._submit(
            "settings:smart_policy",
            self._t("settings.saving_smart_policy", "正在保存智能投稿策略…"),
            lambda: self._save_smart_policies(
                str(account_missing_policy or "auto_register"),
                bool(require_terms_confirmation),
                bool(prefer_orcid),
                bool(auto_authorize_orcid),
                bool(auto_final_submit),
            ),
        )

    @staticmethod
    def _save_smart_policies(
        account_missing_policy: str,
        require_terms_confirmation: bool,
        prefer_orcid: bool,
        auto_authorize_orcid: bool,
        auto_final_submit: bool,
    ) -> dict[str, Any]:
        from ..smart_submission import (
            update_smart_credential_policy,
            update_smart_submission_execution_policy,
        )

        return {
            "credentials": update_smart_credential_policy(
                account_missing_policy=account_missing_policy,
                require_terms_confirmation=require_terms_confirmation,
                prefer_orcid=prefer_orcid,
                auto_authorize_orcid=auto_authorize_orcid,
            ),
            "execution": update_smart_submission_execution_policy(
                auto_final_submit_enabled=auto_final_submit,
            ),
        }

    @Slot(str, str, str, str)
    def runWorkflowAction(
        self,
        stage: str,
        action: str,
        step_id: str = "",
        value: str = "",
    ) -> None:  # noqa: N802
        clean_stage = str(stage or "")
        clean_action = str(action or "")
        self._submit(
            f"settings:workflow:{clean_stage}:{clean_action}",
            self._t("settings.updating_workflow", "正在更新工作流…"),
            lambda: self._run_workflow_action(
                clean_stage,
                clean_action,
                str(step_id or ""),
                str(value or ""),
            ),
        )

    def _run_workflow_action(
        self,
        stage: str,
        action: str,
        step_id: str,
        value: str,
    ) -> dict[str, Any]:
        if stage == "writing" and action == "scheme":
            return self.orchestrator.select_writing_prompt_scheme(value)
        if action == "restore":
            return {
                "writing": self.orchestrator.restore_default_writing_workflow,
                "figures": self.orchestrator.restore_default_figure_workflow,
                "journal_author": self.orchestrator.restore_default_journal_author_workflow,
            }[stage]()
        if action == "add":
            return {
                "writing": self.orchestrator.add_writing_workflow_step,
                "figures": self.orchestrator.add_figure_workflow_step,
                "journal_author": self.orchestrator.add_journal_author_workflow_step,
            }[stage](value)
        if action == "remove":
            return {
                "writing": self.orchestrator.remove_writing_workflow_step,
                "figures": self.orchestrator.remove_figure_workflow_step,
                "journal_author": self.orchestrator.remove_journal_author_workflow_step,
            }[stage](step_id)
        if action in {"up", "down"}:
            direction = -1 if action == "up" else 1
            return {
                "writing": self.orchestrator.move_writing_workflow_step,
                "figures": self.orchestrator.move_figure_workflow_step,
                "journal_author": self.orchestrator.move_journal_author_workflow_step,
            }[stage](step_id, direction)
        raise ValueError(f"unsupported workflow action: {stage}/{action}")

    @Slot(str, str, str, str, str)
    def openPromptEditor(
        self,
        stage: str,
        step_id: str,
        kind: str,
        path: str,
        title: str,
    ) -> None:  # noqa: N802
        try:
            from ..writing_prompt_schemes import is_undergraduate, scheme_id
            workflow = (self.orchestrator.writing_workflow_store.active_snapshot()
                        if str(stage) == "writing" else {})
            if str(stage) == "writing" and str(kind) == "builtin" and is_undergraduate(workflow):
                result = self.orchestrator.writing_workflow_store.read_scheme_prompt(str(step_id))
                kind = "scheme:" + scheme_id(workflow)
            elif str(kind) == "custom":
                methods = {
                    "writing": self.orchestrator.read_writing_workflow_prompt,
                    "figures": self.orchestrator.read_figure_workflow_prompt,
                    "journal_author": self.orchestrator.read_journal_author_workflow_prompt,
                }
                result = methods[str(stage)](str(step_id))
            else:
                result = self.orchestrator.read_prompt_file(str(stage), str(path))
            text = str(result.get("text") or "")
        except Exception as exc:
            self.errorRequested.emit(
                self._t("settings.prompt_open_failed", "提示词未能打开"),
                str(exc),
                traceback.format_exc(),
            )
            return
        self.promptEditorRequested.emit(str(title), str(stage), str(step_id), str(kind), text)

    @Slot(str, str, str, str)
    def savePromptText(self, stage: str, step_id: str, kind: str, text: str) -> None:  # noqa: N802
        clean_stage = str(stage)
        clean_step = str(step_id)
        clean_kind = str(kind)
        self._submit(
            f"settings:prompt:{clean_stage}:{clean_step}",
            self._t("settings.saving_prompt", "正在保存提示词并建立备份…"),
            lambda: self._save_prompt_text(clean_stage, clean_step, clean_kind, str(text)),
        )

    def _save_prompt_text(
        self,
        stage: str,
        step_id: str,
        kind: str,
        text: str,
    ) -> dict[str, Any]:
        if stage == "writing" and kind.startswith("scheme:"):
            return self.orchestrator.save_writing_scheme_prompt(step_id, text, kind.split(":", 1)[1])
        if kind == "custom":
            methods = {
                "writing": self.orchestrator.save_writing_workflow_prompt,
                "figures": self.orchestrator.save_figure_workflow_prompt,
                "journal_author": self.orchestrator.save_journal_author_workflow_prompt,
            }
            return methods[stage](step_id, text)
        inventory = self.orchestrator.stage_prompt_inventory(stage)
        workflow_key = {
            "writing": "writing_workflow",
            "figures": "figure_workflow",
            "journal_author": "journal_author_workflow",
        }[stage]
        workflow = inventory.get(workflow_key, {})
        for item in workflow.get("steps", []) if isinstance(workflow, dict) else []:
            if isinstance(item, dict) and str(item.get("step_id") or "") == step_id:
                return self.orchestrator.save_prompt_file(
                    stage,
                    str(item.get("prompt_path") or ""),
                    text,
                )
        raise KeyError(f"workflow step not found: {stage}/{step_id}")

    @Slot(str)
    def openAuthorTemplateEditor(self, template_id: str) -> None:  # noqa: N802
        try:
            data = self.orchestrator.author_templates()
            item = next(
                (
                    value
                    for value in data.get("templates", [])
                    if isinstance(value, dict)
                    and str(value.get("id") or "") == str(template_id)
                ),
                None,
            )
            if not isinstance(item, dict):
                raise KeyError("author template not found")
        except Exception as exc:
            self.errorRequested.emit(
                self._t("settings.author_open_failed", "作者模板未能打开"),
                str(exc),
                traceback.format_exc(),
            )
            return
        self.authorTemplateEditorRequested.emit(
            str(item.get("id") or ""),
            readable(item.get("name"), str(item.get("id") or "")),
            str(item.get("content") or ""),
            bool(item.get("is_default")),
        )

    @Slot(str, str, str, bool)
    def saveAuthorTemplate(
        self,
        template_id: str,
        name: str,
        content: str,
        make_default: bool,
    ) -> None:  # noqa: N802
        self._submit(
            f"settings:author:{template_id}",
            self._t("settings.saving_author", "正在保存作者模板…"),
            lambda: self._save_author_template(
                str(template_id),
                str(name),
                str(content),
                bool(make_default),
            ),
        )

    def _save_author_template(
        self,
        template_id: str,
        name: str,
        content: str,
        make_default: bool,
    ) -> dict[str, Any]:
        data = self.orchestrator.author_templates()
        found = False
        for item in data.get("templates", []):
            if isinstance(item, dict) and str(item.get("id") or "") == template_id:
                item["name"] = name
                item["content"] = content
                item["form"] = {}
                found = True
        if not found:
            raise KeyError("author template not found")
        if make_default:
            data["default_template_id"] = template_id
        return self.orchestrator.save_author_templates(data)

    @Slot()
    def openAccountCenter(self) -> None:  # noqa: N802
        """Open account/site management without exposing the legacy dashboard."""

        if getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "--workspace",
                str(WORKSPACE_ROOT),
                "account-site-manager",
            ]
            launch_directory = WORKSPACE_ROOT
        else:
            command = [
                sys.executable,
                "-m",
                "unified_paper_orchestrator",
                "--workspace",
                str(WORKSPACE_ROOT),
                "account-site-manager",
            ]
            launch_directory = Path(__file__).resolve().parents[2]
        child_environment = os.environ.copy()
        child_environment[WORKSPACE_ENV] = str(WORKSPACE_ROOT)
        child_environment["UNIFIED_PAPER_UI_LOCALE"] = str(self.i18n.locale)
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            subprocess.Popen(
                command,
                cwd=str(launch_directory),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                close_fds=True,
            )
        except Exception:
            LOGGER.exception("Could not open account and site manager")
            self.toastRequested.emit(
                self._t("controller.account_center_failed", "账号与站点未能打开。"),
                "danger",
            )

    @Slot()
    def shutdown(self) -> None:
        self._projection_timer.stop()
        self._sync_timer.stop()
        try:
            self.codex_account.close()
        except Exception:
            LOGGER.debug("Codex account service shutdown was not available", exc_info=True)
        try:
            self.full_pipeline_runtime.shutdown()
        except Exception:
            LOGGER.debug("Full-pipeline runtime shutdown was not available", exc_info=True)
