from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import Counter, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..automation import parse_toml_like
from ..config import (
    ACCOUNT_VAULT_PATH,
    ACTIVE_WORKSPACE,
    APP_ROOT,
    BACKUP_DIR,
    DELETED_DIR,
    FIGURE_MAX_PARALLEL_WORKERS,
    FIGURE_CODEX_MODEL,
    FIGURE_SDK_JOURNAL_PATH,
    JOURNAL_AUTHOR_CODEX_MODEL,
    JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS,
    JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS,
    JOURNAL_AUTHOR_SDK_JOURNAL_PATH,
    LEGACY_PROJECTS,
    LEGACY_CREDENTIAL_VAULT_PATH,
    SMART_SUBMISSION_SDK_JOURNAL_PATH,
    STAGE_LABELS,
    STAGE_ORDER,
    STATE_DIR,
    UNIFIED_STATE_PATH,
    UNIFIED_SUMMARY_PATH,
    WRITING_DEFAULT_PARALLEL_WORKERS,
    WRITING_CODEX_MODEL,
    WRITING_MAX_PARALLEL_WORKERS,
    WRITING_MAX_WORKFLOW_STEPS,
    WRITING_SDK_JOURNAL_PATH,
)
from ..io_utils import read_json
from ..full_pipeline import full_pipeline_status, get_full_pipeline_runtime
from ..models import action_label, row_state, status_token
from ..orchestrator import UnifiedOrchestrator
from ..step_execution import (
    EXECUTION_PRESETS,
    EXECUTION_PRESET_LABELS,
    REASONING_EFFORTS,
    resolve_execution_profile,
    token_limit_label,
)
from ..writing_evidence_import import prepare_writing_evidence_import
from ..writing_evidence_manual import prepare_manual_writing_evidence
from ..writing_gate_policy import AUTO_TRUST_MODE, STRICT_CONFIRMATION_MODE
from ..workspace import (
    WorkspaceDescriptor,
    clear_active_workspace,
    create_workspace,
    set_active_workspace,
)
from ..smart_submission import (
    FINAL_SUBMIT_REVIEW_READY_ACTION,
    SMART_AUDIT_DIR,
    SMART_AUTOMATION_PATH,
    SMART_CREDENTIAL_PATH,
    SMART_SCHEDULER_PROMPT_PATH,
    SMART_SITE_REGISTRY_PATH,
    SMART_STATE_PATH,
    SMART_SUBMISSION_DIR,
    apply_standing_declarations,
    authorize_final_submit,
    complete_smart_manual_takeover as complete_smart_manual_takeover_workflow,
    discover_smart_submission_candidates,
    enable_smart_submission_candidates,
    ensure_smart_submission_module,
    move_online_queue_item,
    open_smart_manual_takeover_page,
    pause_smart_submission_automation,
    prepare_smart_submission_sdk_papers,
    prioritize_online_queue,
    prepare_final_submit_confirmation_challenge,
    reconcile_smart_submission_program_timer,
    refresh_smart_submission_projection,
    request_smart_submission_retarget,
    request_smart_manual_takeover,
    requeue_waiting_approval_workers,
    resume_smart_credential_broker,
    restore_smart_submission_daily_monitoring,
    run_smart_submission_daily_monitoring,
    run_smart_submission_scheduler,
    smart_submission_daily_monitor_due,
    smart_submission_batch_check,
    smart_submission_browser_cleanup,
    smart_submission_program_plan,
    smart_submission_status,
    start_smart_submission_program_runtime,
    update_smart_credential_policy,
    update_smart_submission_daily_monitoring_time,
    update_smart_submission_execution_policy,
)
from ..smart_submission_sdk_runtime import (
    default_smart_submission_sdk_runtime,
    pause_smart_submission_sdk_after_current,
    smart_submission_sdk_status,
)
from .presentation import (
    AUTHOR_STATUS_LABELS,
    SCHEDULER_MODE_LABELS,
    author_status_label,
    author_status_value,
    parallel_limit_label,
    parallel_limit_value,
    responsive_window_size,
    scheduler_mode_label,
    scheduler_mode_value,
)
from .theme import THEME, apply_enterprise_theme, configure_text_widget
from .detail_panel import MetricStrip, StructuredDetailPanel
from .full_pipeline_window import FullPipelineWindow
from .visual_assets import UIIconLibrary, install_window_icon, load_brand_mark
from .workspace_manager import WorkspaceManagerWindow


FILTERS = {
    "全部": "all",
    "统一队列": "queued",
    "待执行": "ready",
    "等待中": "waiting",
    "阻塞/停止": "blocked",
    "投稿阶段": "submission",
    "写论文阶段": "writing",
}

AI_USE_POLICY_LABELS = {
    "WHEN_REQUIRED": "期刊要求时插入",
    "ALWAYS": "始终插入",
    "NEVER": "不插入",
    "MANUAL_CONFIRM": "投稿前确认",
}


def table_status_token(stage: dict[str, Any]) -> str:
    """Return a compact, scannable table label without changing state data."""

    status = str(stage.get("status") or "missing").strip().casefold()
    label = status_token(stage)
    if status in {"complete", "submitted", "active", "accepted", "selection_complete"}:
        return f"✓ {label}"
    if status in {"monitoring", "running", "starting", "creating_thread"}:
        return f"● {label}"
    if status in {"queued", "ready"}:
        return f"◆ {label}"
    if status in {"waiting", "waiting_event", "waiting_data", "paused", "migration_review"}:
        return f"◷ {label}"
    if status in {"blocked", "stopped", "deleted", "missing_upstream", "needs_user"}:
        return f"! {label}"
    return f"○ {label}"


def compact_paper_label(value: object, *, limit: int = 31) -> str:
    """Keep long generated paper IDs readable inside dense tables."""

    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[: max(8, limit - 1)]}…"


def table_issue_summary(value: object, *, limit: int = 58) -> str:
    """Keep raw SDK diagnostics out of the default operational table."""

    text = " ".join(str(value or "").split())
    lowered = text.casefold()
    if not text:
        return "—"
    if "consumed" in lowered and "hard limit" in lowered:
        return "本 Step Token 超出上限；请选择任务查看处理建议"
    if "semantic contract" in lowered:
        return "本 Step 产物未通过验收；请选择任务查看具体原因"
    if "turn" in lowered and "uncertain" in lowered:
        return "SDK 回合状态待确认；系统将继续核对"
    if len(text) <= limit:
        return text
    return f"{text[: max(16, limit - 1)]}…"


def ai_use_policy_label(value: object) -> str:
    return AI_USE_POLICY_LABELS.get(
        str(value or "").strip().upper(),
        AI_USE_POLICY_LABELS["WHEN_REQUIRED"],
    )


def ai_use_policy_value(label: object) -> str:
    token = str(label or "").strip()
    for value, display in AI_USE_POLICY_LABELS.items():
        if token == display:
            return value
    return "WHEN_REQUIRED"


STAGE_TABS = [
    ("writing", "写论文"),
    ("figures", "图表与插图"),
    ("journal_author", "选刊与作者"),
]

AUTOMATION_STATUS_LABELS = {
    "ACTIVE": "运行中",
    "PAUSED": "已暂停",
    "MISSING": "未配置",
    "DISABLED": "已停用",
}

WRITING_TASK_STATUS_LABELS = {
    "queued": "排队，等待执行",
    "creating_thread": "正在创建会话",
    "starting": "正在启动",
    "running": "正在运行",
    "recovery_pending": "正在恢复",
    "waiting_event": "等待已有回合结束",
    "waiting_prerequisite": "等待前置条件",
    "waiting_capacity": "等待并发槽",
    "blocked": "已阻塞",
    "paused": "已暂停",
    "complete": "已完成",
    "ready": "待运行",
    "idle": "空闲",
    "waiting": "等待中",
    "failed": "运行失败",
}

WRITING_TASK_ACTIVE_STATUSES = {
    "creating_thread",
    "starting",
    "running",
    "recovery_pending",
    "waiting_event",
}

_WRITING_SDK_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


def _writing_session_log_path(codex_home: Path, thread_id: str) -> Path | None:
    thread_id = str(thread_id or "").strip()
    if not _WRITING_SDK_THREAD_ID_RE.fullmatch(thread_id):
        return None
    root = Path(codex_home).expanduser().resolve()
    candidates: list[Path] = []
    for folder in ("sessions", "archived_sessions"):
        base = root / folder
        if not base.is_dir():
            continue
        for path in base.rglob(f"*-{thread_id}.jsonl"):
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                candidates.append(resolved)
    if not candidates:
        return None
    try:
        candidates.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    except OSError:
        return None
    return candidates[0]


def _writing_session_transcript(path: Path, *, max_messages: int = 200) -> dict[str, Any]:
    """Read a bounded, human-facing transcript from one local SDK rollout."""

    messages: deque[dict[str, str]] = deque(maxlen=max(1, min(int(max_messages), 500)))
    try:
        before = path.stat()
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_type = str(payload.get("type") or "")
                if payload_type not in {"user_message", "agent_message", "task_started", "task_complete"}:
                    continue
                if payload_type == "user_message":
                    role = "用户 / 调度器"
                    text = str(payload.get("message") or payload.get("text") or "")
                elif payload_type == "agent_message":
                    role = "Codex"
                    text = str(payload.get("message") or payload.get("text") or "")
                elif payload_type == "task_started":
                    role = "系统"
                    text = "Turn 开始"
                else:
                    role = "系统"
                    text = "Turn 完成"
                text = text.strip()
                if not text:
                    continue
                if len(text) > 20_000:
                    text = text[:20_000] + "\n…（单条消息已截断）"
                messages.append(
                    {
                        "timestamp": str(record.get("timestamp") or ""),
                        "role": role,
                        "text": text,
                    }
                )
        after = path.stat()
    except (OSError, UnicodeError):
        return {"ok": False, "messages": [], "changing": False}
    return {
        "ok": True,
        "messages": list(messages),
        "changing": before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns,
    }


def _writing_step_number(value: Any, *, default: int = 0) -> int:
    try:
        step = int(value)
    except (TypeError, ValueError):
        return default
    return step if 0 <= step <= WRITING_MAX_WORKFLOW_STEPS else default


def _writing_friendly_blocker(status: str, blocker: str) -> str:
    if blocker == "WRITING_EVIDENCE_GATE_BLOCKED":
        return "等待你确认实验数据；完成后点“数据已手动改完并继续”"
    if blocker == "GLOBAL_CODEX_SESSION_BUDGET_EXHAUSTED":
        return "其他 Codex 任务占满了全局会话槽"
    if status == "paused":
        return "全部写作调度已暂停；当前 Step 不会被强制中断"
    if status == "blocked":
        return "执行遇到卡点；选择本行可查看技术原因"
    if status == "waiting_capacity":
        return "等待可用并发槽，不会额外消耗 Token"
    return ""


def _effort_display(value: Any) -> str:
    effort = str(value or "").strip().casefold()
    return {
        "": "模型默认",
        "none": "None",
        "minimal": "Minimal",
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "xhigh": "Extra High",
    }.get(effort, effort or "模型默认")


def _task_execution_profile(
    record: dict[str, Any],
    step: int,
    step_record: dict[str, Any],
    *,
    fallback_model: str,
) -> dict[str, Any]:
    persisted = step_record.get("execution_profile")
    if isinstance(persisted, dict) and str(persisted.get("model") or "").strip():
        profile = dict(persisted)
    else:
        workflow = record.get("workflow_snapshot")
        workflow = workflow if isinstance(workflow, dict) else {}
        definitions = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
        definition = next(
            (
                item
                for item in definitions
                if isinstance(item, dict) and int(item.get("position") or 0) == int(step)
            ),
            {},
        )
        try:
            profile = resolve_execution_profile(
                workflow.get("default_execution_profile"),
                definition.get("execution_profile") if isinstance(definition, dict) else {},
                fallback_model=str(record.get("model") or fallback_model),
            )
        except Exception:
            profile = {
                "model": str(record.get("model") or fallback_model),
                "reasoning_effort": "",
                "hard_token_limit": 0,
            }
    budget = step_record.get("token_budget") if isinstance(step_record.get("token_budget"), dict) else {}
    consumed = budget.get("consumed_step_tokens")
    if not isinstance(consumed, int) or isinstance(consumed, bool):
        usage = step_record.get("usage") if isinstance(step_record.get("usage"), dict) else {}
        last = usage.get("last") if isinstance(usage.get("last"), dict) else {}
        consumed = last.get("total_tokens")
    profile["consumed_step_tokens"] = consumed if isinstance(consumed, int) and not isinstance(consumed, bool) else None
    profile["executor_label"] = (
        f"{profile.get('model') or '-'} / {_effort_display(profile.get('reasoning_effort'))}"
    )
    limit = profile.get("hard_token_limit")
    profile["token_limit_label"] = token_limit_label(limit)
    if profile["consumed_step_tokens"] is None:
        profile["token_status_label"] = f"上限 {profile['token_limit_label']}"
    else:
        profile["token_status_label"] = (
            f"已用 {profile['consumed_step_tokens']:,} / 上限 {profile['token_limit_label']}"
        )
    return profile


def _writing_task_rows(journal_papers: Any, pipeline_state: Any = None) -> list[dict[str, Any]]:
    """Build the writing live-view model from journal plus not-yet-journaled commands."""

    records: dict[str, dict[str, Any]] = {}
    if isinstance(journal_papers, dict):
        records.update(
            (str(key), dict(value))
            for key, value in journal_papers.items()
            if str(key) and isinstance(value, dict)
        )

    if isinstance(pipeline_state, dict):
        for command in pipeline_state.get("commands", []):
            if not isinstance(command, dict) or str(command.get("action") or "") != "new_session":
                continue
            if str(command.get("status") or "").strip().lower() != "pending":
                continue
            command_id = str(command.get("id") or "").strip()
            paper_key = str(command.get("sdk_paper_key") or (f"new-session:{command_id}" if command_id else ""))
            if not paper_key or paper_key in records:
                continue
            params = command.get("params") if isinstance(command.get("params"), dict) else {}
            records[paper_key] = {
                "paper_key": paper_key,
                "paper_id": "",
                "reserved_pipeline_prefix": str(
                    command.get("reserved_pipeline_prefix")
                    or params.get("reserved_pipeline_prefix")
                    or ""
                ).strip(),
                "completed_step": 0,
                "run_until_step": _writing_step_number(params.get("run_until_step"), default=10) or 10,
                "status": "queued",
                "steps": {},
                "created_at": str(command.get("created_at") or ""),
                "updated_at": str(command.get("created_at") or ""),
                "synthetic_pending_command": True,
            }

    rows: list[dict[str, Any]] = []
    active_step_states = {
        "creating_thread",
        "dispatching",
        "running",
        "waiting_event",
        "waiting_capacity",
        "send_uncertain",
        "blocked",
    }
    for paper_key, record in records.items():
        status = str(record.get("status") or "queued").strip().lower()
        completed = _writing_step_number(record.get("completed_step"))
        target = _writing_step_number(record.get("run_until_step"), default=10) or 10
        target = max(target, completed, 1)
        current_step = _writing_step_number(record.get("active_step"))
        steps = record.get("steps") if isinstance(record.get("steps"), dict) else {}
        for raw_step, raw_step_record in steps.items():
            if not isinstance(raw_step_record, dict):
                continue
            step = _writing_step_number(raw_step)
            step_state = str(raw_step_record.get("state") or "").strip().lower()
            if step > completed and step_state in active_step_states:
                current_step = max(current_step, step)
        if status == "complete":
            current_step = target
        elif not current_step:
            current_step = min(completed + 1, target)

        blocker = str(record.get("blocker") or "").strip()
        blocker_message = str(record.get("blocker_message") or "").strip()
        thread_id = str(record.get("thread_id") or "").strip()
        current_step_record = steps.get(str(current_step)) if isinstance(steps, dict) else None
        if not isinstance(current_step_record, dict):
            current_step_record = {}
        execution_profile = _task_execution_profile(
            record,
            current_step,
            current_step_record,
            fallback_model=WRITING_CODEX_MODEL,
        )
        turn_id = str(current_step_record.get("turn_id") or "").strip()
        semantic_validation = current_step_record.get("semantic_validation")
        if not isinstance(semantic_validation, dict):
            semantic_validation = {}
        completed_step_record = steps.get(str(completed)) if isinstance(steps, dict) and completed else None
        if not isinstance(completed_step_record, dict):
            completed_step_record = {}
        if not turn_id:
            turn_id = str(completed_step_record.get("turn_id") or "").strip()
        completed_validation = completed_step_record.get("semantic_validation")
        if (
            isinstance(completed_validation, dict)
            and not semantic_validation.get("issues")
            and not semantic_validation.get("warnings")
        ):
            semantic_validation = completed_validation
        semantic_issues = [
            dict(item) for item in semantic_validation.get("issues", []) if isinstance(item, dict)
        ]
        semantic_warnings = [
            dict(item) for item in semantic_validation.get("warnings", []) if isinstance(item, dict)
        ]
        semantic_path = str(semantic_validation.get("path") or "").strip()
        manual_step5_wait = _writing_evidence_transition_eligible({}, record) and completed == 4
        evidence_wait = (blocker == "WRITING_EVIDENCE_GATE_BLOCKED" and completed == 5) or manual_step5_wait
        if manual_step5_wait:
            current_step = 6
            status_label = "等待实验数据确认"
            step_label = "Step 5 产物已生成 → 确认后进入 Step 6"
        elif evidence_wait:
            current_step = 6
            status_label = "等待实验数据确认"
            step_label = "Step 5 已完成 → 等待 Step 6"
        elif status == "complete":
            status_label = WRITING_TASK_STATUS_LABELS["complete"]
            step_label = f"Step {target} / {target} 已完成"
        elif status in WRITING_TASK_ACTIVE_STATUSES:
            status_label = WRITING_TASK_STATUS_LABELS.get(status, "正在运行")
            step_label = f"正在执行 Step {current_step} / {target}"
        elif status == "waiting_capacity":
            status_label = WRITING_TASK_STATUS_LABELS[status]
            step_label = f"Step {current_step} / {target} 等待槽位"
        elif status == "blocked":
            status_label = WRITING_TASK_STATUS_LABELS[status]
            step_label = f"阻塞在 Step {current_step} / {target}"
        elif status == "paused":
            status_label = WRITING_TASK_STATUS_LABELS[status]
            step_label = f"已完成 Step {completed}；下一步 Step {current_step}"
        else:
            status_label = WRITING_TASK_STATUS_LABELS.get(status, "等待调度")
            step_label = f"待执行 Step {current_step} / {target}"

        cells = 10
        filled = min(cells, max(0, round(cells * completed / target)))
        progress = f"{'█' * filled}{'░' * (cells - filled)}  {completed}/{target}"
        paper_id = str(record.get("paper_id") or "").strip()
        if paper_id:
            label = paper_id
        else:
            short_key = paper_key.rsplit("-", 1)[-1][:8]
            reserved_prefix = str(record.get("reserved_pipeline_prefix") or "").strip()
            if reserved_prefix:
                label = f"{reserved_prefix}_待定标题（Step 1 后补全）"
            else:
                label = f"新论文（Step 1 后命名） · {short_key}"
        updated = str(record.get("updated_at") or record.get("created_at") or "").strip()
        updated_label = updated[5:19].replace("T", " ") if len(updated) >= 19 else updated or "-"
        friendly_blocker = _writing_friendly_blocker(status, blocker)
        if manual_step5_wait:
            friendly_blocker = "Step 5 产物可由程序直接接纳；完成后点“数据已手动改完并继续”"
        elif status == "blocked" and blocker_message:
            friendly_blocker = blocker_message
        elif semantic_warnings:
            warning_labels = [str(item.get("message") or item.get("code") or "") for item in semantic_warnings]
            warning_labels = [value for value in warning_labels if value]
            if warning_labels:
                warning_codes = {
                    str(item.get("code") or "") for item in semantic_warnings if isinstance(item, dict)
                }
                if warning_codes and warning_codes <= _WRITING_STEP5_DEFERRED_CLEANUP_CODES:
                    prefix = "待 Step 6 清理："
                else:
                    prefix = "待 Step 10 清理：" if target == 10 else f"待最终 Step {target} 清理："
                friendly_blocker = prefix + "；".join(warning_labels[:2])
        if status in WRITING_TASK_ACTIVE_STATUSES:
            tag = "running"
            rank = 0
        elif status in {"queued", "ready", "waiting_capacity"}:
            tag = "queued"
            rank = 1
        elif evidence_wait or status in {"waiting_prerequisite", "paused", "waiting"}:
            tag = "waiting"
            rank = 2
        elif status in {"blocked", "failed"}:
            tag = "blocked"
            rank = 3
        else:
            tag = "done"
            rank = 4
        rows.append(
            {
                "paper_key": paper_key,
                "paper_id": paper_id,
                "label": label,
                "status": status,
                "status_label": status_label,
                "completed_step": completed,
                "current_step": current_step,
                "run_until_step": target,
                "step_label": step_label,
                "progress": progress,
                "blocker": blocker,
                "blocker_message": blocker_message,
                "blocker_label": friendly_blocker,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "session_label": thread_id or "待创建",
                "model": str(execution_profile.get("model") or ""),
                "reasoning_effort": str(execution_profile.get("reasoning_effort") or ""),
                "hard_token_limit": execution_profile.get("hard_token_limit"),
                "consumed_step_tokens": execution_profile.get("consumed_step_tokens"),
                "executor_label": str(execution_profile.get("executor_label") or "-"),
                "token_status_label": str(execution_profile.get("token_status_label") or "-"),
                "semantic_issues": semantic_issues,
                "semantic_warnings": semantic_warnings,
                "semantic_path": semantic_path,
                "updated_at": updated,
                "updated_label": updated_label,
                "tag": tag,
                "rank": rank,
                "record": record,
            }
        )
    rows.sort(key=lambda row: str(row["label"]).casefold())
    rows.sort(key=lambda row: str(row["updated_at"]), reverse=True)
    rows.sort(key=lambda row: int(row["rank"]))
    return rows


def _figure_task_rows(journal_papers: Any, pipeline_state: Any = None) -> list[dict[str, Any]]:
    """Build a live figure view from the SDK journal plus queued projection."""

    records: dict[str, dict[str, Any]] = {}
    if isinstance(journal_papers, dict):
        records.update(
            (str(key), dict(value))
            for key, value in journal_papers.items()
            if str(key) and isinstance(value, dict)
        )
    if isinstance(pipeline_state, dict):
        papers = pipeline_state.get("papers", {})
        if isinstance(papers, dict):
            for paper_id, raw in papers.items():
                if not isinstance(raw, dict) or str(paper_id) in records:
                    continue
                status = str(raw.get("status") or "")
                has_v2_identity = bool(raw.get("sdk_request_id") or raw.get("sdk_runtime_event"))
                if not has_v2_identity and status not in {"waiting_data", "migration_review"}:
                    continue
                records[str(paper_id)] = {
                    "paper_id": str(paper_id),
                    "request_id": str(raw.get("sdk_request_id") or ""),
                    "status": status or "queued",
                    "completed_step": int(raw.get("step_done") or 0),
                    "active_step": int(raw.get("sdk_current_step") or 0),
                    "run_until_step": int(raw.get("run_until_step") or 3),
                    "run_pipeline": bool(raw.get("run_pipeline", True)),
                    "workflow_snapshot": raw.get("workflow_snapshot") if isinstance(raw.get("workflow_snapshot"), dict) else {},
                    "thread_id": str(raw.get("thread_id") or ""),
                    "blocker": str(raw.get("blocker") or ""),
                    "blocker_message": str(raw.get("evidence") or ""),
                    "updated_at": str(raw.get("updated_at") or ""),
                    "steps": {},
                    "synthetic_pipeline_record": True,
                }

    labels = {
        "queued": "排队，等待执行",
        "creating_thread": "正在创建固定会话",
        "starting": "正在启动",
        "running": "正在运行",
        "waiting_event": "等待已有回合结束",
        "waiting_data": "等待真实实验数据",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "migration_review": "历史任务待重新排队",
        "complete": "已完成",
    }
    active = {"creating_thread", "starting", "running", "waiting_event"}
    rows: list[dict[str, Any]] = []
    for paper_id, record in records.items():
        status = str(record.get("status") or "queued").strip().lower()
        try:
            completed = max(0, int(record.get("completed_step") or 0))
        except (TypeError, ValueError):
            completed = 0
        try:
            target = max(1, int(record.get("run_until_step") or 3))
        except (TypeError, ValueError):
            target = 3
        try:
            current = max(0, int(record.get("active_step") or 0))
        except (TypeError, ValueError):
            current = 0
        steps = record.get("steps", {}) if isinstance(record.get("steps"), dict) else {}
        if not current:
            for raw_step, step_record in steps.items():
                if isinstance(step_record, dict) and str(step_record.get("state") or "") in {
                    "starting",
                    "running",
                    "recovery_pending",
                    "blocked",
                    "waiting_data",
                }:
                    try:
                        current = max(current, int(raw_step))
                    except (TypeError, ValueError):
                        pass
        if status == "complete":
            current = target
        elif not current:
            current = min(max(completed + 1, 1), target)
        workflow = record.get("workflow_snapshot") if isinstance(record.get("workflow_snapshot"), dict) else {}
        workflow_steps = workflow.get("steps", []) if isinstance(workflow.get("steps", []), list) else []
        step_names = {
            int(item.get("position") or 0): str(item.get("name") or "")
            for item in workflow_steps
            if isinstance(item, dict) and int(item.get("position") or 0) > 0
        }
        current_name = step_names.get(current, "")
        name_suffix = f" · {current_name}" if current_name else ""
        revision_only = not bool(record.get("run_pipeline", True)) and bool(record.get("revisions"))
        if revision_only or (target == 4 and not workflow_steps):
            step_label = "正在执行 Step 4（单图修改）" if status in active else "Step 4（单图修改）"
            progress = "██████████  3/3 + 修图"
        else:
            if status == "complete":
                step_label = f"Step {target} / {target} 已完成"
            elif status == "waiting_data":
                step_label = f"Step {current} 已生成部分产物 → 等待数据{name_suffix}"
            elif status == "blocked":
                step_label = f"阻塞在 Step {current} / {target}{name_suffix}"
            elif status == "paused":
                step_label = f"已完成 Step {completed}；下一步 Step {current}{name_suffix}"
            elif status in active:
                step_label = f"正在执行 Step {current} / {target}{name_suffix}"
            else:
                step_label = f"待执行 Step {current} / {target}{name_suffix}"
            filled = min(10, max(0, round(10 * min(completed, target) / target)))
            progress = f"{'█' * filled}{'░' * (10 - filled)}  {min(completed, target)}/{target}"
        current_record = steps.get(str(current)) if isinstance(steps.get(str(current)), dict) else {}
        completed_record = steps.get(str(completed)) if isinstance(steps.get(str(completed)), dict) else {}
        execution_profile = _task_execution_profile(
            record,
            current,
            current_record,
            fallback_model=FIGURE_CODEX_MODEL,
        )
        turn_id = str(current_record.get("turn_id") or completed_record.get("turn_id") or "")
        contract = current_record.get("contract") if isinstance(current_record.get("contract"), dict) else {}
        if not contract and isinstance(completed_record.get("contract"), dict):
            contract = completed_record.get("contract", {})
        issues = [str(value) for value in contract.get("issues", []) if str(value)] if isinstance(contract, dict) else []
        warnings = [str(value) for value in contract.get("warnings", []) if str(value)] if isinstance(contract, dict) else []
        blocker = str(record.get("blocker") or "").strip()
        blocker_message = str(record.get("blocker_message") or "").strip()
        if status == "waiting_data":
            friendly = blocker_message or "缺少真实实验数据；补齐后重新执行 Step 3"
        elif status == "migration_review":
            friendly = blocker_message or "历史任务待迁移；点击“运行选中画图任务”按现有产物继续"
        elif status == "blocked":
            friendly = blocker_message or blocker or "产物验收未通过"
        elif status == "needs_user":
            manual_warnings = [
                value.removeprefix("需人工处理：").strip()
                for value in warnings
                if value.startswith("需人工处理：")
            ]
            friendly = (
                blocker_message
                or "；".join(manual_warnings[:2])
                or "投稿包可用，但仍有明确的人工事项"
            )
        elif status == "waiting_event":
            friendly = "正在内部 SDK 会话等待已有 Turn 结束"
        elif issues:
            friendly = "；".join(issues[:2])
        else:
            friendly = ""
        updated = str(record.get("updated_at") or record.get("created_at") or "")
        updated_label = updated[5:19].replace("T", " ") if len(updated) >= 19 else updated or "-"
        if status in active:
            tag, rank = "running", 0
        elif status == "queued":
            tag, rank = "queued", 1
        elif status in {"waiting_data", "paused", "migration_review"}:
            tag, rank = "waiting", 2
        elif status == "blocked":
            tag, rank = "blocked", 3
        else:
            tag, rank = "done", 4
        rows.append(
            {
                "paper_id": paper_id,
                "label": paper_id,
                "status": status,
                "status_label": labels.get(status, status or "未知"),
                "completed_step": completed,
                "current_step": current,
                "run_until_step": target,
                "workflow_step_name": current_name,
                "workflow_version_id": str(record.get("workflow_version_id") or workflow.get("version_id") or ""),
                "step_label": step_label,
                "progress": progress,
                "thread_id": str(record.get("thread_id") or ""),
                "turn_id": turn_id,
                "session_label": str(record.get("thread_id") or "") or "待创建",
                "model": str(execution_profile.get("model") or ""),
                "reasoning_effort": str(execution_profile.get("reasoning_effort") or ""),
                "hard_token_limit": execution_profile.get("hard_token_limit"),
                "consumed_step_tokens": execution_profile.get("consumed_step_tokens"),
                "executor_label": str(execution_profile.get("executor_label") or "-"),
                "token_status_label": str(execution_profile.get("token_status_label") or "-"),
                "blocker": blocker,
                "blocker_message": blocker_message,
                "blocker_label": friendly,
                "contract_issues": issues,
                "contract_warnings": warnings,
                "updated_at": updated,
                "updated_label": updated_label,
                "tag": tag,
                "rank": rank,
                "record": record,
            }
        )
    rows.sort(key=lambda row: str(row["label"]).casefold())
    rows.sort(key=lambda row: str(row["updated_at"]), reverse=True)
    rows.sort(key=lambda row: int(row["rank"]))
    return rows


def _journal_author_task_rows(journal_papers: Any, pipeline_state: Any = None) -> list[dict[str, Any]]:
    """Build the V2 journal/author live view from the frozen workflow."""

    normalized_pipeline: dict[str, Any] = {"papers": {}}
    if isinstance(pipeline_state, dict):
        normalized_pipeline = dict(pipeline_state)
        source_papers = pipeline_state.get("papers", {})
        projected: dict[str, Any] = {}
        if isinstance(source_papers, dict):
            for paper_id, raw in source_papers.items():
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                status = str(item.get("workflow_status") or item.get("status") or "").strip().lower()
                item["status"] = status
                workflow = item.get("workflow_snapshot") if isinstance(item.get("workflow_snapshot"), dict) else {}
                item["run_until_step"] = int(
                    item.get("run_until_position")
                    or workflow.get("step_count")
                    or 1
                )
                item["step_done"] = int(item.get("sdk_completed_step") or item.get("step_done") or 0)
                item["sdk_current_step"] = int(item.get("sdk_current_step") or (1 if item.get("sdk_request_id") else 0))
                projected[str(paper_id)] = item
        normalized_pipeline["papers"] = projected
    rows = _figure_task_rows(journal_papers, normalized_pipeline)
    labels = {
        "queued": "排队，等待执行",
        "creating_thread": "正在创建固定会话",
        "starting": "正在启动",
        "running": "正在选刊/生成投稿包",
        "waiting_event": "等待已有回合结束",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "needs_user": "需要人工处理",
        "selection_complete": "选刊已完成",
        "complete": "已完成",
    }
    for row in rows:
        record = row.get("record") if isinstance(row.get("record"), dict) else {}
        status = str(row.get("status") or "queued")
        row["status_label"] = labels.get(status, str(row.get("status_label") or status))
        workflow = record.get("workflow_snapshot") if isinstance(record.get("workflow_snapshot"), dict) else {}
        combined_mode = str(workflow.get("mode") or "combined") == "combined"
        steps = record.get("steps") if isinstance(record.get("steps"), dict) else {}
        current = max(1, int(row.get("current_step") or 1))
        step_record = steps.get(str(current)) if isinstance(steps.get(str(current)), dict) else {}
        profile = _task_execution_profile(
            record,
            current,
            step_record,
            fallback_model=JOURNAL_AUTHOR_CODEX_MODEL,
        )
        row.update(
            {
                "model": str(profile.get("model") or ""),
                "reasoning_effort": str(profile.get("reasoning_effort") or ""),
                "hard_token_limit": profile.get("hard_token_limit"),
                "consumed_step_tokens": profile.get("consumed_step_tokens"),
                "executor_label": str(profile.get("executor_label") or "-"),
                "token_status_label": str(profile.get("token_status_label") or "-"),
                "package_status": str(record.get("package_status") or ""),
            }
        )
        if status == "selection_complete":
            row["step_label"] = "期刊选择已完成；等待重新生成投稿包"
        elif status == "needs_user":
            detail = str(row.get("blocker_label") or "").strip()
            row["step_label"] = (
                f"AI 流程已完成；等待处理：{detail}"
                if detail
                else "AI 流程已完成；等待明确的人工事项"
            )
        elif combined_mode and int(row.get("run_until_step") or 1) in {1, 3}:
            if status == "complete":
                row["step_label"] = "合并 Step 2-3 已完成"
                row["progress"] = "██████████  1/1"
            elif status == "blocked":
                row["step_label"] = "合并 Step 2-3 验收阻塞"
            elif status in {"creating_thread", "starting", "running", "waiting_event"}:
                row["step_label"] = "正在执行合并 Step 2-3"
                row["progress"] = "█████░░░░░  进行中"
            else:
                row["step_label"] = "待执行合并 Step 2-3"
    return rows

SMART_WORKFLOW_LABELS = {
    "queued": "等待执行",
    "assigned": "已分配",
    "running": "执行中",
    "monitoring": "监控中",
    "waiting": "等待中",
    "waiting_user_approval": "等待授权",
    "blocked": "已阻塞",
    "completed": "已完成",
    "idle": "空闲",
    "incomplete": "未完成",
    "action_evidence_required": "待核对修改要求",
    "identity_reconciliation_required": "重复投稿待处理",
    "waiting_revision_package": "等待科学修订稿",
    "waiting_journal_author_retarget": "自动转投中",
    "retarget_needed": "等待自动转投",
    "retarget_target_mismatch": "转投目标不一致",
    "manual_takeover": "人工修改中",
}

UI_LOG_NAME = "unified_control_panel.log"
LOGGER = logging.getLogger("unified_paper_orchestrator.ui")


def _stage_result_error(state: Any, stage: str) -> str:
    if not isinstance(state, dict):
        return "调度器没有返回可识别的状态。"
    last = state.get("last_scheduler_result")
    results = last.get("results") if isinstance(last, dict) else None
    result = results.get(stage) if isinstance(results, dict) else None
    if not isinstance(result, dict):
        return "调度器没有返回本阶段的执行结果。"
    if result.get("ok") is True:
        return ""
    return str(result.get("error") or result.get("warning") or "本阶段启动失败，未返回具体原因。")


def _writing_evidence_import_eligible(record: Any) -> bool:
    return _writing_evidence_transition_eligible(record, None)


_WRITING_STEP5_DEFERRED_CLEANUP_CODES = frozenset(
    {
        "latex_unescaped_verify_data_identifier",
        "latex_unescaped_text_underscore",
    }
)


def _writing_evidence_transition_eligible(record: Any, live_record: Any) -> bool:
    if not isinstance(record, dict):
        record = {}
    stages = record.get("stages")
    writing = stages.get("writing") if isinstance(stages, dict) else None
    if isinstance(writing, dict):
        try:
            step_done = int(writing.get("step_done") or 0)
        except (TypeError, ValueError):
            step_done = 0
        gate = writing.get("evidence_gate")
        if step_done == 5 and isinstance(gate, dict) and not bool(gate.get("ready")):
            return True

    if not isinstance(live_record, dict):
        return False
    try:
        completed_step = int(live_record.get("completed_step") or 0)
    except (TypeError, ValueError):
        return False
    blocker = str(live_record.get("blocker") or "").strip()
    if completed_step == 5 and blocker == "WRITING_EVIDENCE_GATE_BLOCKED":
        return True
    if (
        str(live_record.get("status") or "").strip().lower() != "blocked"
        or completed_step != 4
        or blocker != "WRITING_OUTPUT_SEMANTIC_INVALID"
    ):
        return False
    steps = live_record.get("steps")
    step_five = steps.get("5") if isinstance(steps, dict) else None
    if not isinstance(step_five, dict) or str(step_five.get("state") or "") != "blocked":
        return False
    validation = step_five.get("semantic_validation")
    raw_issues = validation.get("issues") if isinstance(validation, dict) else None
    if not isinstance(raw_issues, list) or not raw_issues:
        return False
    issue_codes = {
        str(item.get("code") or "")
        for item in raw_issues
        if isinstance(item, dict) and str(item.get("code") or "")
    }
    return bool(issue_codes) and issue_codes <= _WRITING_STEP5_DEFERRED_CLEANUP_CODES


def _writing_live_record_for_paper_id(journal_papers: Any, paper_id: str) -> dict[str, Any]:
    if not isinstance(journal_papers, dict):
        return {}
    expected = str(paper_id or "").strip().casefold()
    for raw_record in journal_papers.values():
        if (
            isinstance(raw_record, dict)
            and str(raw_record.get("paper_id") or "").strip().casefold() == expected
        ):
            return raw_record
    return {}


def _selected_writing_evidence_ids(panel: Any) -> list[str]:
    tree = getattr(panel, "writing_task_tree", None)
    rows = getattr(panel, "writing_task_rows", {})
    if tree is not None and isinstance(rows, dict):
        selected = [str(item) for item in tree.selection()]
        live_ids = [
            str(rows[item].get("paper_id") or "").strip()
            for item in selected
            if item in rows and isinstance(rows[item], dict)
        ]
        live_ids = [value for value in live_ids if value]
        if live_ids:
            return list(dict.fromkeys(live_ids))
    return list(dict.fromkeys(str(value) for value in panel.selected_ids() if str(value)))


def _writing_evidence_import_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return "证据检查没有返回可识别的结果。"
    if result.get("ok"):
        return "\n".join(
            [
                "真实实验证据只读检查通过。",
                f"证据文件：{result.get('artifact_count') or 0} 个",
                f"定量声明：{result.get('claim_count') or 0} 条",
                f"候选清单 SHA-256：{result.get('manifest_sha256') or '-'}",
            ]
        )
    issues = result.get("issues") if isinstance(result.get("issues"), list) else []
    lines = ["真实实验证据检查未通过。"]
    for issue in issues[:5]:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "evidence_import_blocked")
        path = str(issue.get("path") or "")
        lines.append(f"- {code}（{path}）" if path else f"- {code}")
    if len(issues) > 5:
        lines.append(f"- 另有 {len(issues) - 5} 个问题，请查看技术详情。")
    return "\n".join(lines)


def _writing_existing_evidence_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return "现有实验数据检查没有返回可识别的结果。"
    if result.get("ready") is True and result.get("transition_ready") is True:
        return "\n".join(
            [
                "现有实验数据已通过 Step 5→6 门禁。",
                f"证据文件：{result.get('artifact_count') or 0} 个",
                f"定量声明：{result.get('claim_count') or 0} 条",
            ]
        )
    issues = result.get("issues") if isinstance(result.get("issues"), list) else []
    lines = ["现有实验数据尚未通过 Step 5→6 门禁。"]
    for issue in issues[:5]:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "evidence_gate_blocked")
        path = str(issue.get("path") or "")
        lines.append(f"- {code}（{path}）" if path else f"- {code}")
    if len(issues) > 5:
        lines.append(f"- 另有 {len(issues) - 5} 个问题")
    return "\n".join(lines)


def configure_ui_logging(log_dir: Path | None = None) -> Path:
    """Configure one bounded application log and return its path."""
    target_dir = Path(log_dir) if log_dir is not None else STATE_DIR / "logs"
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / UI_LOG_NAME
    resolved = str(log_path.resolve())
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    for handler in LOGGER.handlers:
        if isinstance(handler, RotatingFileHandler) and str(Path(handler.baseFilename).resolve()) == resolved:
            return log_path
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    LOGGER.addHandler(handler)
    return log_path


def install_global_exception_hooks() -> None:
    """Log uncaught main-thread and background-thread failures."""

    def log_main_exception(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        LOGGER.critical("Unhandled application exception", exc_info=(exc_type, exc, tb))

    def log_thread_exception(args: threading.ExceptHookArgs) -> None:
        LOGGER.critical(
            "Unhandled background exception in %s",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_main_exception
    threading.excepthook = log_thread_exception


class TaskRunner:
    """Run at most one bounded background task and marshal callbacks to Tk."""

    def __init__(self, root: tk.Misc, *, poll_ms: int = 100) -> None:
        self.root = root
        self.poll_ms = poll_ms
        self._lock = threading.Lock()
        self._results: queue.Queue[tuple[str, bool, Any, Callable[[Any], None], Callable[[BaseException], None]]] = queue.Queue()
        self._active_name = ""
        self._closed = False
        self.root.after(self.poll_ms, self._drain)

    @property
    def active_name(self) -> str:
        with self._lock:
            return self._active_name

    def submit(
        self,
        name: str,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> bool:
        with self._lock:
            if self._closed or self._active_name:
                return False
            self._active_name = name

        def run() -> None:
            try:
                result = work()
            except BaseException as exc:  # keep the Tk loop alive and report on the main thread
                LOGGER.error("Background task failed: %s", name, exc_info=(type(exc), exc, exc.__traceback__))
                self._results.put((name, False, exc, on_success, on_error))
            else:
                self._results.put((name, True, result, on_success, on_error))

        threading.Thread(target=run, name=f"ui-task:{name}", daemon=True).start()
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _drain(self) -> None:
        while True:
            try:
                name, ok, payload, on_success, on_error = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                if self._active_name == name:
                    self._active_name = ""
                closed = self._closed
            if closed:
                continue
            try:
                if ok:
                    on_success(payload)
                else:
                    on_error(payload)
            except BaseException as exc:
                LOGGER.error(
                    "Task callback failed: %s",
                    name,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                reporter = getattr(self.root, "report_callback_exception", None)
                if callable(reporter):
                    try:
                        reporter(type(exc), exc, exc.__traceback__)
                    except BaseException:
                        LOGGER.exception("Tk callback exception reporter failed")
        with self._lock:
            closed = self._closed
        if not closed:
            self.root.after(self.poll_ms, self._drain)


def _treeview_row(
    iid: Any,
    text: Any,
    values: tuple[Any, ...] | list[Any],
    tags: tuple[Any, ...] | list[Any] = (),
) -> dict[str, Any]:
    """Build one normalized Treeview row for stable diff rendering."""

    return {
        "iid": str(iid),
        "text": str(text or ""),
        "values": tuple(str(value or "") for value in values),
        "tags": tuple(str(tag) for tag in tags if str(tag)),
    }


def _sync_treeview_rows(
    tree: Any,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    """Apply only changed Treeview rows while preserving selection and scroll.

    Rebuilding every table every two seconds caused visible flicker, repeated
    selection callbacks, and unnecessary Tcl work.  This helper deliberately
    uses only the public Treeview API so it is also testable without a display.
    """

    normalized = [
        _treeview_row(
            row.get("iid"),
            row.get("text"),
            tuple(row.get("values") or ()),
            tuple(row.get("tags") or ()),
        )
        for row in rows
        if str(row.get("iid") or "").strip()
    ]
    desired_ids = [str(row["iid"]) for row in normalized]
    desired_set = set(desired_ids)
    selected = {str(item) for item in tree.selection()}
    focused = str(tree.focus() or "")
    try:
        yview = tuple(tree.yview())
    except Exception:
        yview = ()

    deleted = 0
    inserted = 0
    updated = 0
    moved = 0
    for iid in list(tree.get_children("")):
        iid = str(iid)
        if iid not in desired_set:
            tree.delete(iid)
            deleted += 1

    for index, row in enumerate(normalized):
        iid = str(row["iid"])
        if not tree.exists(iid):
            tree.insert(
                "",
                index,
                iid=iid,
                text=row["text"],
                values=row["values"],
                tags=row["tags"],
            )
            inserted += 1
            continue
        current = tree.item(iid)
        current_values = tuple(str(value or "") for value in current.get("values", ()))
        current_tags = tuple(str(value) for value in current.get("tags", ()))
        if (
            str(current.get("text") or "") != row["text"]
            or current_values != row["values"]
            or current_tags != row["tags"]
        ):
            tree.item(
                iid,
                text=row["text"],
                values=row["values"],
                tags=row["tags"],
            )
            updated += 1
        if int(tree.index(iid)) != index:
            tree.move(iid, "", index)
            moved += 1

    remaining_selection = [iid for iid in desired_ids if iid in selected and tree.exists(iid)]
    if remaining_selection:
        tree.selection_set(*remaining_selection)
    elif tree.selection():
        tree.selection_remove(*tree.selection())
    if focused and tree.exists(focused):
        tree.focus(focused)
    if yview:
        try:
            tree.yview_moveto(float(yview[0]))
        except Exception:
            pass
    return {
        "inserted": inserted,
        "updated": updated,
        "moved": moved,
        "deleted": deleted,
    }


class UnifiedControlPanel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("大一统论文智能体控制台")
        self._window_icon_refs = install_window_icon(self)
        self.ui_icons = UIIconLibrary(self)
        self.brand_mark_image = load_brand_mark(self, 44)
        width, height, minimum_width, minimum_height = responsive_window_size(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            preferred=(1480, 840),
            minimum=(860, 540),
        )
        self.geometry(f"{width}x{height}")
        self.minsize(minimum_width, minimum_height)
        self.orchestrator = UnifiedOrchestrator()
        self.full_pipeline_runtime = get_full_pipeline_runtime(self.orchestrator)
        self.state_data: dict = {}
        self.filter_var = tk.StringVar(value="全部")
        self.overview_search_var = tk.StringVar(value="")
        self._overview_filter_after_id: str | None = None
        self.selected_paper_id = ""
        self.stage_trees: dict[str, ttk.Treeview] = {}
        self.stage_detail_texts: dict[str, tk.Text] = {}
        self.stage_detail_panels: dict[str, StructuredDetailPanel] = {}
        self.stage_status_vars: dict[str, tk.StringVar] = {}
        self.stage_info_vars: dict[str, tk.StringVar] = {}
        self.stage_search_vars: dict[str, tk.StringVar] = {}
        self.stage_filter_vars: dict[str, tk.StringVar] = {}
        self.stage_filter_after_ids: dict[str, str] = {}
        self.stage_action_buttons: dict[str, list[ttk.Button]] = {}
        self.stage_action_hint_vars: dict[str, tk.StringVar] = {}
        self.stage_technical_vars: dict[str, tk.BooleanVar] = {}
        self.stage_live_expanded_vars: dict[str, tk.BooleanVar] = {}
        self.live_task_trees: dict[str, ttk.Treeview] = {}
        self.live_task_scrollbars: dict[str, ttk.Scrollbar] = {}
        self.writing_task_tree: ttk.Treeview | None = None
        self.writing_live_summary_var: tk.StringVar | None = None
        self.writing_task_rows: dict[str, dict[str, Any]] = {}
        self._writing_runtime_status: dict[str, Any] = {}
        self._writing_journal_papers: dict[str, Any] = {}
        self._writing_pipeline_state: dict[str, Any] = {}
        self._writing_live_after_id: str | None = None
        self._unified_projection_mtime_ns = 0
        self.figure_task_tree: ttk.Treeview | None = None
        self.figure_live_summary_var: tk.StringVar | None = None
        self.figure_task_rows: dict[str, dict[str, Any]] = {}
        self._figure_runtime_status: dict[str, Any] = {}
        self._figure_journal_papers: dict[str, Any] = {}
        self._figure_pipeline_state: dict[str, Any] = {}
        self.journal_author_task_tree: ttk.Treeview | None = None
        self.journal_author_live_summary_var: tk.StringVar | None = None
        self.journal_author_task_rows: dict[str, dict[str, Any]] = {}
        self._journal_author_runtime_status: dict[str, Any] = {}
        self._journal_author_journal_papers: dict[str, Any] = {}
        self._journal_author_pipeline_state: dict[str, Any] = {}
        self.smart_submission_state: dict = {}
        self.smart_submission_candidates: dict[str, dict[str, Any]] = {}
        self._smart_submission_status_cache: dict[str, Any] = {}
        self.smart_submission_tree: ttk.Treeview | None = None
        self.smart_submission_detail_panel: StructuredDetailPanel | None = None
        self.smart_context_action_button: ttk.Button | None = None
        self.smart_action_hint_var: tk.StringVar | None = None
        self.smart_submission_monitor_result_var: tk.StringVar | None = None
        self.smart_submission_monitor_result_label: ttk.Label | None = None
        self.smart_metrics_expanded_var: tk.BooleanVar | None = None
        self._smart_monitor_next_poll_at = 0.0
        self._smart_credential_retry_next_poll_at = 0.0
        self._scheduler_status_cache: dict[str, Any] = {}
        self._execution_mode_cache = "unknown"
        self._refresh_pending_request: tuple[str, bool, str, bool, bool] | None = None
        self._refresh_request_sequence = 0
        self._refresh_last_applied_sequence = 0
        self._refresh_last_duration_ms = 0
        self.tab_frames: dict[str, ttk.Frame] = {}
        self._menus: list[tk.Menu] = []
        self._workspace_manager_window: WorkspaceManagerWindow | None = None
        self._full_pipeline_window: FullPipelineWindow | None = None
        self.task_runner = TaskRunner(self)
        self.refresh_runner = TaskRunner(self, poll_ms=75)
        self.dialog_runner = TaskRunner(self, poll_ms=75)
        self.maintenance_runner = TaskRunner(self, poll_ms=250)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self.bind("<F5>", lambda _event: self.refresh(announce=True))
        self._smart_program_timer_reconcile = {
            "ok": False,
            "pending": True,
            "message": "程序定时器正在后台核对",
        }
        self.refresh(announce=True)
        self._schedule_writing_live_refresh()
        try:
            self.full_pipeline_runtime.resume_if_active()
        except Exception as exc:
            LOGGER.error(
                "Failed to resume persisted full-pipeline batch",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self.status_var.set(f"一键全流程恢复失败：{exc}")

    def report_callback_exception(self, exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        """Keep Tk callback failures visible and persist the traceback."""
        LOGGER.error("Unhandled Tk callback", exc_info=(exc_type, exc, tb))
        log_path = configure_ui_logging()
        try:
            messagebox.showerror("界面操作失败", f"{exc}\n\n详细日志：{log_path}", parent=self)
        except tk.TclError:
            pass

    def _run_background_task(
        self,
        name: str,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        def on_error(exc: BaseException) -> None:
            self.status_var.set(f"后台任务失败：{name}")
            messagebox.showerror("后台任务失败", f"{name}\n\n{exc}\n\n详细日志：{configure_ui_logging()}", parent=self)

        if not self.task_runner.submit(name, work, on_success, on_error):
            active = self.task_runner.active_name or "窗口正在关闭"
            messagebox.showwarning("后台任务忙", f"请等待当前任务完成：{active}", parent=self)
            return
        self.status_var.set(f"后台任务运行中：{name}")

    def _run_dialog_background_task(
        self,
        name: str,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        """Load dialog data off the Tk thread so button clicks respond at once."""

        def on_error(exc: BaseException) -> None:
            self.status_var.set(f"打开失败：{name}")
            LOGGER.error(
                "Dialog background load failed: %s",
                name,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            messagebox.showerror(
                name,
                f"无法打开：{exc}\n\n详细日志：{configure_ui_logging()}",
                parent=self,
            )

        if not self.dialog_runner.submit(name, work, on_success, on_error):
            active = self.dialog_runner.active_name or "窗口正在关闭"
            self.status_var.set(f"正在准备窗口：{active}")
            return
        self.status_var.set(f"正在准备窗口：{name}…")

    def _pause_smart_submission_runtime(self, *, reason: str) -> dict[str, Any]:
        sdk = pause_smart_submission_sdk_after_current()
        timer = pause_smart_submission_automation(reason=reason)
        return {
            "sdk_runtime": sdk,
            "program_timer": {
                key: value for key, value in timer.items() if key != "state"
            },
        }

    def _show_execution_blocked(self, action: str, exc: BaseException) -> None:
        code = str(getattr(exc, "code", "") or "").strip()
        suffix = f"\n\n错误码：{code}" if code else ""
        LOGGER.warning("Execution action blocked: %s: %s", action, exc)
        messagebox.showerror(
            "AI 执行方式不匹配或调度器不可用",
            f"{action}未执行。\n\n{exc}{suffix}",
            parent=self,
        )

    def _on_close(self) -> None:
        if self._writing_live_after_id is not None:
            try:
                self.after_cancel(self._writing_live_after_id)
            except tk.TclError:
                pass
            self._writing_live_after_id = None
        self.full_pipeline_runtime.shutdown()
        self.task_runner.close()
        self.refresh_runner.close()
        self.dialog_runner.close()
        self.maintenance_runner.close()
        self.destroy()

    def _build_ui(self) -> None:
        apply_enterprise_theme(self)
        self.configure(background=THEME.canvas)
        root = ttk.Frame(self, padding=(14, 10, 14, 10), style="App.TFrame")
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root, style="Header.TFrame", padding=(12, 8))
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(1, weight=1)

        brand = ttk.Frame(header, style="Header.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(
            brand,
            image=self.brand_mark_image,
            style="BrandMark.TLabel",
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
        brand_title = ttk.Frame(brand, style="Header.TFrame")
        brand_title.grid(row=0, column=1, sticky="w")
        ttk.Label(
            brand_title,
            text="大一统论文智能工作台",
            style="AppTitle.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(brand_title, text="V2", style="Badge.TLabel").pack(
            side=tk.LEFT,
            padx=(9, 0),
        )
        ttk.Label(
            brand,
            text="写作 · 图表 · 选刊作者 · 智能投稿",
            style="AppSubtitle.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))

        workspace_bar = ttk.Frame(header, style="Workspace.TFrame", padding=(10, 6))
        workspace_bar.grid(row=0, column=1, sticky="ew", padx=(18, 18))
        workspace_bar.columnconfigure(0, weight=1)
        self.workspace_display_var = tk.StringVar(
            value=(
                f"工作区：{ACTIVE_WORKSPACE.name}　›　"
                f"{ACTIVE_WORKSPACE.root}"
            )
        )
        workspace_label = ttk.Label(
            workspace_bar,
            textvariable=self.workspace_display_var,
            style="BreadcrumbStrong.TLabel",
            justify=tk.LEFT,
        )
        workspace_label.grid(row=0, column=0, sticky="ew", padx=3)
        workspace_label.bind(
            "<Configure>",
            lambda event: workspace_label.configure(
                wraplength=max(280, int(event.width))
            ),
        )
        self.full_pipeline_summary_var = tk.StringVar(value="一键全流程：未启动")
        pipeline_summary_label = ttk.Label(
            workspace_bar,
            textvariable=self.full_pipeline_summary_var,
            style="Breadcrumb.TLabel",
        )
        pipeline_summary_label.grid(row=1, column=0, sticky="w", padx=3, pady=(3, 0))
        workspace_switch_button = ttk.Button(
            workspace_bar,
            text="切换工作区…",
            command=self.open_workspace_manager,
            style="Quiet.TButton",
            image=self.ui_icons.icon("grid", size=15),
            compound=tk.LEFT,
        )
        workspace_switch_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 3))
        pipeline_view_button = ttk.Button(
            workspace_bar,
            text="查看全流程",
            command=self.open_full_pipeline_window,
            style="Quiet.TButton",
            image=self.ui_icons.icon("pipeline", size=15),
            compound=tk.LEFT,
        )
        pipeline_view_button.grid(row=0, column=2, rowspan=2, sticky="e", padx=3)

        toolbar = ttk.Frame(header, style="CommandBar.TFrame")
        toolbar.grid(row=0, column=2, sticky="e")
        refresh_button = ttk.Button(
            toolbar,
            text="刷新",
            command=lambda: self.refresh(announce=True),
            style="Quiet.TButton",
            image=self.ui_icons.icon("refresh", size=16),
            compound=tk.LEFT,
        )
        refresh_button.pack(side=tk.LEFT, padx=3)
        pipeline_button = ttk.Button(
            toolbar,
            text="一键全流程",
            command=self.open_full_pipeline_window,
            style="Accent.TButton",
            image=self.ui_icons.icon("pipeline", size=16, tone="inverse"),
            compound=tk.LEFT,
        )
        pipeline_button.pack(side=tk.LEFT, padx=3)
        writing_button = ttk.Button(
            toolbar,
            text="仅新建写作",
            command=self.start_new_writing_session,
            image=self.ui_icons.icon("add", size=16),
            compound=tk.LEFT,
        )
        writing_button.pack(side=tk.LEFT, padx=3)
        account_button = ttk.Button(toolbar, text="账号与站点", command=self.open_account_site_manager)
        account_button.configure(image=self.ui_icons.icon("account", size=16), compound=tk.LEFT)
        account_button.pack(side=tk.LEFT, padx=3)

        project_menu = tk.Menu(self, tearoff=0)
        project_menu.add_command(label="查看全部运行状态", command=self.show_scheduler_status)
        project_menu.add_separator()
        project_menu.add_command(label="查看总控报告", command=self.open_summary)
        project_menu.add_command(label="备份中心", command=self.open_backups)
        project_menu.add_command(label="回收站", command=self.open_deleted)
        project_menu.add_command(label="AI 执行与 API 设置", command=self.open_ai_execution_settings)
        self._menus.append(project_menu)
        project_button = ttk.Menubutton(toolbar, text="更多")
        project_button.configure(
            menu=project_menu,
            image=self.ui_icons.icon("more", size=16),
            compound=tk.LEFT,
        )
        project_button.pack(side=tk.LEFT, padx=3)

        emergency_button = ttk.Button(toolbar, text="紧急停止", command=self.pause_all, style="Danger.TButton")
        emergency_button.configure(
            image=self.ui_icons.icon("stop", size=14, tone="inverse"),
            compound=tk.LEFT,
        )
        emergency_button.pack(side=tk.RIGHT, padx=3)

        header_layout = {"mode": "wide"}

        def reflow_header(event: tk.Event) -> None:
            width = int(getattr(event, "width", header.winfo_width()) or 0)
            mode = "compact" if width < 900 else "medium" if width < 1500 else "wide"
            if mode == header_layout["mode"]:
                return
            header_layout["mode"] = mode
            if mode == "compact":
                brand.grid_configure(row=0, column=0, columnspan=3, sticky="w")
                toolbar.grid_configure(
                    row=1,
                    column=0,
                    columnspan=3,
                    sticky="ew",
                    pady=(8, 0),
                )
                workspace_bar.grid_configure(
                    row=2,
                    column=0,
                    columnspan=3,
                    sticky="ew",
                    padx=(0, 0),
                    pady=(8, 0),
                )
            elif mode == "medium":
                brand.grid_configure(row=0, column=0, columnspan=1, sticky="w")
                toolbar.grid_configure(
                    row=0,
                    column=2,
                    columnspan=1,
                    sticky="e",
                    pady=(0, 0),
                )
                workspace_bar.grid_configure(
                    row=1,
                    column=0,
                    columnspan=3,
                    sticky="ew",
                    padx=(0, 0),
                    pady=(8, 0),
                )
            else:
                brand.grid_configure(row=0, column=0, columnspan=1, sticky="w")
                toolbar.grid_configure(
                    row=0,
                    column=2,
                    columnspan=1,
                    sticky="e",
                    pady=(0, 0),
                )
                workspace_bar.grid_configure(
                    row=0,
                    column=1,
                    columnspan=1,
                    sticky="ew",
                    padx=(18, 18),
                    pady=(0, 0),
                )

        header.bind("<Configure>", reflow_header, add="+")

        primary_nav = ttk.Frame(root, style="Header.TFrame", padding=(8, 0))
        primary_nav.grid(row=1, column=0, sticky="ew")
        primary_nav.columnconfigure(5, weight=1)
        primary_nav_buttons: dict[str, ttk.Button] = {}
        primary_nav_markers: dict[str, ttk.Frame] = {}
        primary_nav_frames: dict[str, ttk.Frame] = {}

        self.notebook = ttk.Notebook(root, style="Content.TNotebook")
        self.notebook.grid(row=2, column=0, sticky="nsew")

        def add_primary_nav_item(
            key: str,
            label: str,
            frame: ttk.Frame,
            icon_name: str,
            column: int,
        ) -> None:
            host = ttk.Frame(primary_nav, style="Header.TFrame")
            host.grid(row=0, column=column, sticky="nsw", padx=(0, 4))
            button = ttk.Button(
                host,
                text=label,
                image=self.ui_icons.icon(icon_name, size=18),
                compound=tk.LEFT,
                style="Nav.TButton",
                command=lambda target=frame: self.notebook.select(target),
            )
            button.grid(row=0, column=0, sticky="ew")
            marker = ttk.Frame(host, style="NavLine.TFrame", height=3)
            marker.grid(row=1, column=0, sticky="ew")
            primary_nav_buttons[key] = button
            primary_nav_markers[key] = marker
            primary_nav_frames[key] = frame

        def sync_primary_navigation() -> None:
            selected = str(self.notebook.select() or "")
            for key, button in primary_nav_buttons.items():
                active = selected == str(primary_nav_frames.get(key) or "")
                button.state(["selected"] if active else ["!selected"])
                if active:
                    primary_nav_markers[key].grid()
                else:
                    primary_nav_markers[key].grid_remove()

        def handle_primary_tab_changed(_event: tk.Event) -> None:
            sync_primary_navigation()
            self.on_tab_changed()

        self.notebook.bind("<<NotebookTabChanged>>", handle_primary_tab_changed)

        overview = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
        overview.columnconfigure(0, weight=1)
        overview.rowconfigure(2, weight=1)
        self.tab_frames["overview"] = overview
        self.notebook.add(overview, text="总览")
        self.notebook.tab(
            overview,
            image=self.ui_icons.icon("overview", size=18),
            compound=tk.LEFT,
        )
        add_primary_nav_item("overview", "总览", overview, "overview", 0)
        self._build_overview_tab(overview)

        stage_column = 0
        for stage, label in STAGE_TABS:
            stage_column += 1
            frame = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(2, weight=1)
            self.tab_frames[stage] = frame
            self.notebook.add(frame, text=label)
            self.notebook.tab(
                frame,
                image=self.ui_icons.icon(
                    {
                        "writing": "writing",
                        "figures": "figures",
                        "journal_author": "authors",
                    }.get(stage, "document"),
                    size=18,
                ),
                compound=tk.LEFT,
            )
            add_primary_nav_item(
                stage,
                label,
                frame,
                {
                    "writing": "writing",
                    "figures": "figures",
                    "journal_author": "authors",
                }.get(stage, "document"),
                stage_column,
            )
            self._build_stage_tab(frame, stage)

        submission_hub = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
        submission_hub.columnconfigure(0, weight=1)
        submission_hub.rowconfigure(1, weight=1)
        self.submission_hub = submission_hub
        self.notebook.add(submission_hub, text="投稿中心")
        self.notebook.tab(
            submission_hub,
            image=self.ui_icons.icon("submission", size=18),
            compound=tk.LEFT,
        )
        add_primary_nav_item("submission_hub", "投稿中心", submission_hub, "submission", 4)

        channel_bar = ttk.Frame(submission_hub, style="Header.TFrame", padding=(4, 4, 4, 8))
        channel_bar.grid(row=0, column=0, sticky="ew")
        channel_bar.columnconfigure(2, weight=1)

        self.submission_notebook = ttk.Notebook(submission_hub, style="Content.TNotebook")
        self.submission_notebook.grid(row=1, column=0, sticky="nsew")

        stable_submission = ttk.Frame(self.submission_notebook, padding=(0, 8, 0, 0))
        stable_submission.columnconfigure(0, weight=1)
        stable_submission.rowconfigure(2, weight=1)
        self.tab_frames["submission"] = stable_submission
        self.submission_notebook.add(stable_submission, text="稳定投稿（默认）")
        self.submission_notebook.tab(
            stable_submission,
            image=self.ui_icons.icon("inbox", size=16),
            compound=tk.LEFT,
        )
        self._build_stage_tab(stable_submission, "submission")

        smart_submission = ttk.Frame(self.submission_notebook, padding=(0, 8, 0, 0))
        smart_submission.columnconfigure(0, weight=1)
        smart_submission.rowconfigure(3, weight=1)
        self.tab_frames["smart_submission"] = smart_submission
        self.submission_notebook.add(smart_submission, text="智能投稿（试运行）")
        self.submission_notebook.tab(
            smart_submission,
            image=self.ui_icons.icon("agent", size=16, tone="done"),
            compound=tk.LEFT,
        )
        self._build_smart_submission_tab(smart_submission)

        stable_channel_button = ttk.Button(
            channel_bar,
            text="稳定投稿（默认）\n严格权限边界，适合日常投稿",
            image=self.ui_icons.icon("submission", size=28, tone="info", disc=True),
            compound=tk.LEFT,
            style="Channel.TButton",
            command=lambda: self.submission_notebook.select(stable_submission),
        )
        stable_channel_button.grid(row=0, column=0, sticky="w", padx=(0, 8))
        smart_channel_button = ttk.Button(
            channel_bar,
            text="智能投稿（试运行）\n自主执行与监控，适合受控验证",
            image=self.ui_icons.icon("agent", size=28, tone="done", disc=True),
            compound=tk.LEFT,
            style="Channel.TButton",
            command=lambda: self.submission_notebook.select(smart_submission),
        )
        smart_channel_button.grid(row=0, column=1, sticky="w")
        channel_note = ttk.Label(
            channel_bar,
            text="两个通道共享论文与账号资料；切换不会改变投稿路线。",
            style="SectionMeta.TLabel",
        )
        channel_note.grid(row=0, column=2, sticky="e", padx=(16, 4))

        channel_layout = {"mode": "wide"}

        def reflow_channels(event: tk.Event) -> None:
            width = int(getattr(event, "width", channel_bar.winfo_width()) or 0)
            mode = "compact" if width < 850 else "medium" if width < 1200 else "wide"
            if mode == channel_layout["mode"]:
                return
            channel_layout["mode"] = mode
            if mode == "compact":
                stable_channel_button.grid_configure(row=0, column=0, sticky="ew")
                smart_channel_button.grid_configure(row=1, column=0, sticky="ew", pady=(6, 0))
                channel_note.grid_configure(
                    row=2,
                    column=0,
                    columnspan=3,
                    sticky="w",
                    padx=(4, 0),
                    pady=(6, 0),
                )
            elif mode == "medium":
                stable_channel_button.grid_configure(row=0, column=0, sticky="w")
                smart_channel_button.grid_configure(row=0, column=1, sticky="w", pady=(0, 0))
                channel_note.grid_configure(
                    row=1,
                    column=0,
                    columnspan=3,
                    sticky="w",
                    padx=(4, 0),
                    pady=(6, 0),
                )
            else:
                stable_channel_button.grid_configure(row=0, column=0, sticky="w")
                smart_channel_button.grid_configure(row=0, column=1, sticky="w", pady=(0, 0))
                channel_note.grid_configure(
                    row=0,
                    column=2,
                    columnspan=1,
                    sticky="e",
                    padx=(16, 4),
                    pady=(0, 0),
                )

        channel_bar.bind("<Configure>", reflow_channels, add="+")

        def handle_submission_tab_changed(_event: tk.Event) -> None:
            selected = str(self.submission_notebook.select() or "")
            stable_channel_button.state(
                ["selected"] if selected == str(stable_submission) else ["!selected"]
            )
            smart_channel_button.state(
                ["selected"] if selected == str(smart_submission) else ["!selected"]
            )
            self.on_tab_changed()

        self.submission_notebook.bind("<<NotebookTabChanged>>", handle_submission_tab_changed)
        self.after_idle(lambda: handle_submission_tab_changed(tk.Event()))
        self.after_idle(sync_primary_navigation)

        self.status_var = tk.StringVar(value="")
        status_bar = ttk.Frame(root, style="StatusBar.TFrame", padding=(8, 5))
        status_bar.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        ttk.Label(
            status_bar,
            textvariable=self.status_var,
            style="Status.TLabel",
            anchor=tk.W,
        ).pack(fill=tk.X)

    def open_full_pipeline_window(self) -> None:
        window = self._full_pipeline_window
        try:
            if window is not None and window.winfo_exists():
                window.deiconify()
                window.lift()
                window.focus_force()
                return
        except tk.TclError:
            self._full_pipeline_window = None

        def closed() -> None:
            self._full_pipeline_window = None

        window = FullPipelineWindow(
            self,
            orchestrator=self.orchestrator,
            runtime=self.full_pipeline_runtime,
            run_background=self._run_background_task,
            on_changed=self.refresh,
            on_closed=closed,
        )
        self._full_pipeline_window = window
        self._fit_dialog_window(
            window,
            preferred=(1120, 680),
            minimum=(820, 540),
            margins=(50, 80),
        )
        window.transient(self)
        window.lift()

    def _fit_dialog_window(
        self,
        dialog: tk.Toplevel,
        *,
        preferred: tuple[int, int],
        minimum: tuple[int, int],
        margins: tuple[int, int] = (60, 100),
    ) -> tuple[int, int]:
        """Fit a dialog to the current display instead of using fixed pixels."""

        width, height, minimum_width, minimum_height = responsive_window_size(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            preferred=preferred,
            minimum=minimum,
            margins=margins,
        )
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(minimum_width, minimum_height)
        return width, height

    @staticmethod
    def _install_responsive_action_layout(
        container: tk.Misc,
        widgets: list[tk.Widget],
        *,
        horizontal_gap: int = 3,
        vertical_gap: int = 2,
    ) -> None:
        """Wrap action buttons when the available width becomes too narrow."""

        layout_cache: list[tuple[int, int]] = []

        def reflow(event: tk.Event[tk.Misc] | None = None) -> None:
            available = int(getattr(event, "width", 0) or container.winfo_width() or 0)
            if available <= 1:
                try:
                    available = int(container.winfo_toplevel().winfo_width()) - 40
                except (AttributeError, tk.TclError, TypeError, ValueError):
                    available = 320
            available = max(240, available)
            positions: list[tuple[int, int]] = []
            row = 0
            column = 0
            used = 0
            for widget in widgets:
                required = max(60, int(widget.winfo_reqwidth())) + (horizontal_gap * 2)
                if column and used + required > available:
                    row += 1
                    column = 0
                    used = 0
                positions.append((row, column))
                used += required
                column += 1
            if positions == layout_cache:
                return
            layout_cache[:] = positions
            for widget, (target_row, target_column) in zip(widgets, positions):
                widget.grid(
                    row=target_row,
                    column=target_column,
                    sticky="w",
                    padx=horizontal_gap,
                    pady=vertical_gap,
                )

        container.bind("<Configure>", reflow, add="+")
        container.after_idle(reflow)

    @staticmethod
    def _install_responsive_detail_pane(
        paned: ttk.PanedWindow,
        *,
        preferred_detail_width: int = 420,
    ) -> None:
        """Give tables the wide-screen priority while keeping detail useful."""

        layout_state = {"width": 0}

        def reflow(event: tk.Event | None = None) -> None:
            width = int(getattr(event, "width", 0) or paned.winfo_width() or 0)
            if width <= 1 or width == layout_state["width"]:
                return
            layout_state["width"] = width
            if width >= 1080:
                target = width - preferred_detail_width
            else:
                target = round(width * 0.58)
            target = max(320, min(width - 300, target))
            try:
                paned.sashpos(0, target)
            except (tk.TclError, IndexError):
                pass

        paned.bind("<Configure>", reflow, add="+")
        paned.after_idle(reflow)

    def open_workspace_manager(self) -> None:
        existing = self._workspace_manager_window
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        window = WorkspaceManagerWindow(
            self,
            active=ACTIVE_WORKSPACE,
            legacy_root=APP_ROOT,
            on_create_request=self._create_workspace_from_manager,
            on_switch_request=self._switch_workspace,
        )
        self._workspace_manager_window = window

    def _create_workspace_from_manager(
        self,
        target: Path,
        name: str,
        import_current: bool,
        manager: WorkspaceManagerWindow,
    ) -> None:
        operation = "导入当前工作区" if import_current else "新建论文工作区"

        def on_success(descriptor: WorkspaceDescriptor) -> None:
            if manager.winfo_exists():
                manager.refresh()
            copied = 0
            try:
                manifest = read_json(descriptor.root / "workspace.json", {})
                copied = int(manifest.get("import", {}).get("copied_bytes") or 0)
            except (AttributeError, TypeError, ValueError):
                copied = 0
            copied_text = f"\n复制数据：{copied / (1024 ** 3):.2f} GiB" if copied else ""
            switch_now = messagebox.askyesno(
                "论文工作区已创建",
                (
                    f"工作区：{descriptor.name}\n"
                    f"目录：{descriptor.root}{copied_text}\n\n"
                    "原目录没有移动或删除。是否现在切换到新工作区？"
                ),
                parent=manager if manager.winfo_exists() else self,
            )
            self.status_var.set(f"工作区已创建：{descriptor.name}")
            if switch_now:
                self._switch_workspace(descriptor.root)

        self._run_background_task(
            operation,
            lambda: create_workspace(
                target,
                name,
                source=ACTIVE_WORKSPACE,
                import_data=import_current,
                make_active=False,
            ),
            on_success,
        )

    @staticmethod
    def _active_workspace_runtime_records() -> list[str]:
        active_statuses = {
            "creating_thread",
            "dispatching",
            "recovery_pending",
            "running",
            "starting",
            "waiting_event",
        }
        sources = (
            ("写作", WRITING_SDK_JOURNAL_PATH),
            ("画图", FIGURE_SDK_JOURNAL_PATH),
            ("选刊作者", JOURNAL_AUTHOR_SDK_JOURNAL_PATH),
            ("智能投稿", SMART_STATE_PATH),
        )
        active: list[str] = []
        for label, path in sources:
            state = read_json(path, {})
            records = state.get("papers", {}) if isinstance(state, dict) else {}
            if not isinstance(records, dict):
                continue
            for paper_id, record in records.items():
                if not isinstance(record, dict):
                    continue
                status = str(
                    record.get("status")
                    or record.get("workflow_status")
                    or record.get("state")
                    or ""
                ).strip().casefold()
                if status in active_statuses:
                    active.append(f"{label}：{paper_id}（{status}）")
        return active

    def _workspace_switch_blockers(self) -> list[str]:
        blockers: list[str] = []
        if self.task_runner.active_name:
            blockers.append(f"界面后台操作：{self.task_runner.active_name}")
        blockers.extend(self._active_workspace_runtime_records())
        return blockers

    def _workspace_relaunch_command(self, target: Path | None) -> list[str]:
        if getattr(sys, "frozen", False):
            command = [sys.executable]
            if target is not None:
                command.extend(["--workspace", str(target)])
            return command
        command = [sys.executable, "-m", "unified_paper_orchestrator"]
        if target is not None:
            command.extend(["--workspace", str(target)])
        command.append("ui")
        return command

    def _switch_workspace(self, target: Path | None) -> None:
        blockers = self._workspace_switch_blockers()
        if blockers:
            messagebox.showwarning(
                "暂时不能切换工作区",
                (
                    "以下任务仍在当前工作区执行。切换可能把运行结果写入错误目录：\n\n"
                    + "\n".join(f"• {item}" for item in blockers[:10])
                    + "\n\n请等待当前 Turn 结束或先暂停对应任务。"
                ),
                parent=self,
            )
            return
        target_label = "旧版兼容工作区" if target is None else str(target)
        if not messagebox.askyesno(
            "切换论文工作区",
            f"EXE 将关闭当前窗口并以以下工作区重新打开：\n\n{target_label}\n\n是否继续？",
            parent=self,
        ):
            return

        try:
            if target is None:
                clear_active_workspace()
            else:
                set_active_workspace(target)
            command = self._workspace_relaunch_command(target)
            environment = os.environ.copy()
            if target is None:
                environment.pop("UNIFIED_PAPER_WORKSPACE", None)
            else:
                environment["UNIFIED_PAPER_WORKSPACE"] = str(target)
            kwargs: dict[str, Any] = {
                "cwd": str(APP_ROOT),
                "env": environment,
                "close_fds": True,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = int(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            subprocess.Popen(command, **kwargs)
        except Exception as exc:
            try:
                if ACTIVE_WORKSPACE.is_legacy_bridge:
                    clear_active_workspace()
                else:
                    set_active_workspace(ACTIVE_WORKSPACE.root)
            except Exception:
                pass
            messagebox.showerror(
                "切换工作区失败",
                f"没有关闭当前窗口。\n\n{exc}",
                parent=self,
            )
            return
        self.status_var.set(f"正在切换工作区：{target_label}")
        self.after(300, self._on_close)

    def _build_overview_tab(self, parent: ttk.Frame) -> None:
        self.overview_metrics = MetricStrip(
            parent,
            (
                ("total", "论文总数"),
                ("active", "运行中"),
                ("waiting", "待处理"),
                ("human", "等待人工"),
                ("complete", "已完成"),
            ),
            icons={
                "total": self.ui_icons.icon("total", size=44, tone="info", disc=True),
                "active": self.ui_icons.icon("active", size=44, tone="success", disc=True),
                "waiting": self.ui_icons.icon("waiting", size=44, tone="attention", disc=True),
                "human": self.ui_icons.icon("human", size=44, tone="severe", disc=True),
                "complete": self.ui_icons.icon("complete", size=44, tone="success", disc=True),
            },
        )
        self.overview_metrics.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        filter_bar = ttk.Frame(parent)
        filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        filter_bar.columnconfigure(0, weight=1)
        title_row = ttk.Frame(filter_bar)
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        title_row.columnconfigure(1, weight=1)
        ttk.Label(title_row, text="全部论文", style="SectionTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.summary_var = tk.StringVar(value="")
        ttk.Label(title_row, textvariable=self.summary_var, style="SectionMeta.TLabel").grid(
            row=0,
            column=1,
            sticky="w",
            padx=(12, 0),
        )
        self.show_overview_technical_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            title_row,
            text="显示技术信息",
            variable=self.show_overview_technical_var,
            command=self._toggle_overview_technical_details,
        ).grid(row=0, column=2, sticky="e")

        query_row = ttk.Frame(filter_bar)
        query_row.grid(row=1, column=0, sticky="ew")
        query_row.columnconfigure(1, weight=1)
        ttk.Label(query_row, text="搜索", style="SectionMeta.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 7),
        )
        search = ttk.Entry(query_row, textvariable=self.overview_search_var)
        search.grid(row=0, column=1, sticky="ew")
        search.insert(0, "")
        search.bind("<KeyRelease>", lambda _event: self._schedule_overview_filter_refresh())
        search.bind("<Escape>", lambda _event: self._clear_overview_filters())
        combo = ttk.Combobox(
            query_row,
            textvariable=self.filter_var,
            values=list(FILTERS),
            width=12,
            state="readonly",
        )
        combo.grid(row=0, column=2, sticky="e", padx=(8, 0))
        combo.bind("<<ComboboxSelected>>", lambda _event: self.render_overview_table())
        ttk.Button(
            query_row,
            text="重置",
            command=self._clear_overview_filters,
            style="Quiet.TButton",
        ).grid(row=0, column=3, padx=(6, 0))

        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.grid(row=2, column=0, sticky="nsew")

        left = ttk.Frame(paned)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        paned.add(left, weight=4)

        columns = ("stage", "action", "writing", "figures", "journal", "submission", "route")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="论文编号")
        self.tree.column("#0", width=280, anchor=tk.W, stretch=False)
        headings = {
            "stage": "当前阶段",
            "action": "下一步",
            "writing": "写论文",
            "figures": "图表",
            "journal": "选刊作者",
            "submission": "投稿状态",
            "route": "投稿路线",
        }
        widths = {
            "stage": 66,
            "action": 105,
            "writing": 54,
            "figures": 54,
            "journal": 64,
            "submission": 66,
            "route": 82,
        }
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor=tk.W)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_detail())
        self.tree.bind("<Button-3>", self.show_overview_context_menu)
        self._bind_keyboard_context_menu(
            self.tree,
            lambda event: self.show_overview_context_menu(event, keyboard=True),
        )

        ybar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=self.tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self._configure_tree_tags(self.tree)

        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        paned.add(right, weight=2)
        self._install_responsive_detail_pane(paned)

        notebook = ttk.Notebook(right, style="Detail.TNotebook")
        notebook.grid(row=0, column=0, sticky="nsew")
        self.overview_detail_notebook = notebook
        self.overview_detail_panel = StructuredDetailPanel(
            notebook,
            empty_title="请选择一篇论文",
            technical_label="详细信息",
            empty_image=self.ui_icons.icon("document", size=72, tone="info", disc=True),
        )
        self.detail_title_var = self.overview_detail_panel.title_var
        self.detail_text = self.overview_detail_panel.technical_text
        self.queue_text = self._readonly_text(notebook)
        self.log_text = self._readonly_text(notebook)
        self.technical_text = self._readonly_text(notebook)
        notebook.add(self.overview_detail_panel, text="当前论文")
        notebook.add(self.queue_text.master, text="统一队列")
        notebook.add(self.log_text.master, text="最近记录")

    def _toggle_overview_technical_details(self) -> None:
        notebook = getattr(self, "overview_detail_notebook", None)
        technical_text = getattr(self, "technical_text", None)
        if notebook is None or technical_text is None:
            return
        technical_tab = str(technical_text.master)
        tab_is_visible = technical_tab in notebook.tabs()
        should_show = bool(self.show_overview_technical_var.get())
        if should_show and not tab_is_visible:
            notebook.add(technical_text.master, text="技术详情")
            notebook.select(technical_text.master)
        elif not should_show and tab_is_visible:
            notebook.forget(technical_text.master)

    def _schedule_overview_filter_refresh(self) -> None:
        pending = getattr(self, "_overview_filter_after_id", None)
        if pending:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        self._overview_filter_after_id = self.after(160, self._apply_overview_filter_refresh)

    def _apply_overview_filter_refresh(self) -> None:
        self._overview_filter_after_id = None
        self.render_overview_table()

    def _clear_overview_filters(self) -> str:
        self.overview_search_var.set("")
        self.filter_var.set("全部")
        self.render_overview_table()
        return "break"

    def _schedule_stage_filter_refresh(self, stage: str) -> None:
        pending = self.stage_filter_after_ids.pop(stage, "")
        if pending:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        self.stage_filter_after_ids[stage] = self.after(
            160,
            lambda s=stage: self._apply_stage_filter_refresh(s),
        )

    def _apply_stage_filter_refresh(self, stage: str) -> None:
        self.stage_filter_after_ids.pop(stage, None)
        self.render_stage_tables(only_stage=stage)

    def _clear_stage_filters(self, stage: str) -> str:
        search_var = self.stage_search_vars.get(stage)
        filter_var = self.stage_filter_vars.get(stage)
        if search_var is not None:
            search_var.set("")
        if filter_var is not None:
            filter_var.set("全部状态")
        self.render_stage_tables(only_stage=stage)
        return "break"

    def _selected_ids_for_stage(self, stage: str) -> list[str]:
        tree = self.stage_trees.get(stage)
        selected = [str(item) for item in tree.selection()] if tree is not None else []
        if selected:
            return selected
        live_tree = self.live_task_trees.get(stage)
        live_selected = [str(item) for item in live_tree.selection()] if live_tree is not None else []
        if stage == "writing":
            return [
                str(self.writing_task_rows[item].get("paper_id") or "")
                for item in live_selected
                if item in self.writing_task_rows
                and str(self.writing_task_rows[item].get("paper_id") or "")
            ]
        return live_selected

    def _refresh_stage_action_state(self, stage: str) -> None:
        buttons = self.stage_action_buttons.get(stage, [])
        hint_var = self.stage_action_hint_vars.get(stage)
        selected = self._selected_ids_for_stage(stage)
        state = "normal" if selected else "disabled"
        for button in buttons:
            try:
                button.configure(state=state)
            except tk.TclError:
                continue
        if hint_var is None:
            return
        if not selected:
            hint_var.set("选择一篇或多篇论文后，系统会启用与选中任务有关的操作。")
            return
        first = selected[0]
        record = self.state_data.get("papers", {}).get(first, {})
        next_action = str(
            record.get("next_action_label")
            or action_label(record.get("next_action", ""))
            or "等待执行"
        )
        count_text = f"已选择 {len(selected)} 篇"
        hint_var.set(f"{count_text}；第一篇下一步：{next_action}。主按钮只作用于当前选中论文。")

    def _on_stage_selection_changed(self, stage: str) -> None:
        self.update_stage_detail(stage)
        self._refresh_stage_action_state(stage)

    def _on_live_task_selection_changed(
        self,
        stage: str,
        detail_callback: Callable[[], None],
    ) -> None:
        detail_callback()
        self._refresh_stage_action_state(stage)

    def _build_stage_tab(self, parent: ttk.Frame, stage: str) -> None:
        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(0, weight=1)

        common = ttk.LabelFrame(controls, text="任务操作", padding=(6, 3))
        common.grid(row=0, column=0, sticky="ew")
        common_buttons: list[tk.Widget] = []
        if stage == "writing":
            handoff_label = "运行/继续选中论文"
        elif stage == "figures":
            handoff_label = "运行选中画图"
        elif stage == "journal_author":
            handoff_label = "运行选中选刊任务"
        elif stage == "submission":
            handoff_label = "运行选中投稿任务"
        else:
            handoff_label = "运行选中阶段任务"
        selected_action_buttons: list[ttk.Button] = []
        primary_button = ttk.Button(
            common,
            text=handoff_label,
            command=lambda s=stage: self.apply_selected_stage_handoff(s),
            style="Accent.TButton",
            state="disabled",
            image=self.ui_icons.icon("active", size=15, tone="inverse"),
            compound=tk.LEFT,
        )
        common_buttons.append(primary_button)
        selected_action_buttons.append(primary_button)
        if stage == "writing":
            import_button = ttk.Button(
                common,
                text="导入真实实验证据…",
                command=self.import_selected_writing_evidence,
                state="disabled",
                image=self.ui_icons.icon("submission", size=15),
                compound=tk.LEFT,
            )
            manual_data_button = ttk.Button(
                common,
                text="数据已手动改完并继续",
                command=self.continue_selected_writing_with_existing_evidence,
                state="disabled",
                image=self.ui_icons.icon("check", size=15, tone="success"),
                compound=tk.LEFT,
            )
            common_buttons.extend((import_button, manual_data_button))
            selected_action_buttons.extend((import_button, manual_data_button))
        elif stage == "figures":
            figure_data_button = ttk.Button(
                common,
                text="数据已补齐，继续 Step 3",
                command=self.continue_selected_figures_from_step3,
                state="disabled",
                image=self.ui_icons.icon("check", size=15, tone="success"),
                compound=tk.LEFT,
            )
            common_buttons.append(figure_data_button)
            selected_action_buttons.append(figure_data_button)
        stage_run_menu = tk.Menu(self, tearoff=0)
        if stage == "writing":
            stage_run_menu.add_command(
                label="运行全部写作任务",
                command=lambda s=stage: self.start_stage_single_active(s),
            )
            stage_run_menu.add_command(
                label="暂停全部写作（当前 Step 后）",
                command=lambda s=stage: self.pause_stage(s),
            )
        elif stage == "figures":
            stage_run_menu.add_command(
                label="运行全部画图任务",
                command=lambda s=stage: self.start_stage_single_active(s),
            )
            stage_run_menu.add_command(
                label="暂停全部画图（当前 Step 后）",
                command=lambda s=stage: self.pause_stage(s),
            )
        elif stage == "journal_author":
            stage_run_menu.add_command(
                label="运行全部选刊作者任务",
                command=lambda s=stage: self.start_stage_single_active(s),
            )
            stage_run_menu.add_command(
                label="暂停全部选刊作者（当前任务后）",
                command=lambda s=stage: self.pause_stage(s),
            )
        else:
            start_label = "运行全部稳定投稿任务" if stage == "submission" else "运行本阶段全部任务"
            pause_label = "暂停全部稳定投稿" if stage == "submission" else "暂停本阶段全部任务"
            stage_run_menu.add_command(
                label=start_label,
                command=lambda s=stage: self.start_stage_single_active(s),
            )
            stage_run_menu.add_command(
                label=pause_label,
                command=lambda s=stage: self.pause_stage(s),
            )
        self._menus.append(stage_run_menu)
        stage_run_button = ttk.Menubutton(common, text="运行与暂停")
        stage_run_button.configure(
            menu=stage_run_menu,
            image=self.ui_icons.icon("pipeline", size=15),
            compound=tk.LEFT,
        )
        common_buttons.append(stage_run_button)
        self.stage_action_buttons[stage] = selected_action_buttons
        self._install_responsive_action_layout(common, common_buttons)

        management = ttk.LabelFrame(controls, text="配置与工具", padding=(6, 3))
        management.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        management_widgets: list[tk.Widget] = [
            ttk.Button(
                management,
                text="调度设置与提示词",
                command=lambda s=stage: self.open_stage_prompt_settings(s),
                style="Quiet.TButton",
                image=self.ui_icons.icon("grid", size=15),
                compound=tk.LEFT,
            )
        ]
        if stage == "figures":
            figure_menu = tk.Menu(self, tearoff=0)
            figure_menu.add_command(label="从第 1 步重新执行", command=self.queue_selected_figures_from_step1)
            figure_menu.add_command(label="修改指定图", command=self.queue_custom_figure_revisions)
            figure_menu.add_command(label="重绘图 1 / 图 2", command=self.queue_selected_figure_image2_revisions)
            self._menus.append(figure_menu)
            figure_button = ttk.Menubutton(management, text="修图工具")
            figure_button.configure(
                menu=figure_menu,
                image=self.ui_icons.icon("figures", size=15),
                compound=tk.LEFT,
            )
            management_widgets.append(figure_button)
        if stage == "journal_author":
            management_widgets.append(
                ttk.Button(
                    management,
                    text="作者资料",
                    command=self.manage_author_templates,
                    image=self.ui_icons.icon("authors", size=15),
                    compound=tk.LEFT,
                )
            )
            management_widgets.append(
                ttk.Button(
                    management,
                    text="选刊偏好",
                    command=self.manage_selected_journal_preferences,
                    image=self.ui_icons.icon("check", size=15),
                    compound=tk.LEFT,
                )
            )
        self._install_responsive_action_layout(management, management_widgets)

        more_button = ttk.Button(controls, text="更多操作", style="Quiet.TButton")
        more_button.configure(
            command=lambda s=stage, b=more_button: self.show_stage_tools(s, b),
            image=self.ui_icons.icon("more", size=15),
            compound=tk.LEFT,
        )
        more_button.grid(row=0, column=2, sticky="e", padx=(8, 3))

        stage_control_layout = {"compact": False}

        def reflow_stage_controls(event: tk.Event) -> None:
            compact = int(getattr(event, "width", controls.winfo_width()) or 0) < 860
            if compact == stage_control_layout["compact"]:
                return
            stage_control_layout["compact"] = compact
            if compact:
                management.grid_configure(row=1, column=0, sticky="ew", padx=(0, 0), pady=(4, 0))
                more_button.grid_configure(row=1, column=1, sticky="e", pady=(4, 0))
            else:
                management.grid_configure(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 0))
                more_button.grid_configure(row=0, column=2, sticky="e", pady=(0, 0))

        controls.bind("<Configure>", reflow_stage_controls, add="+")
        self.stage_status_vars[stage] = tk.StringVar(value="")

        info_bar = ttk.Frame(parent)
        info_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        info_bar.columnconfigure(0, weight=1)
        self.stage_info_vars[stage] = tk.StringVar(value="")
        ttk.Label(
            info_bar,
            textvariable=self.stage_info_vars[stage],
            anchor=tk.W,
            justify=tk.LEFT,
            style="SectionMeta.TLabel",
        ).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(
            info_bar,
            textvariable=self.stage_status_vars[stage],
            anchor=tk.E,
            style="SectionMeta.TLabel",
        ).grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )
        self.stage_action_hint_vars[stage] = tk.StringVar(
            value="选择一篇或多篇论文后，系统会启用与选中任务有关的操作。"
        )
        ttk.Label(
            info_bar,
            textvariable=self.stage_action_hint_vars[stage],
            style="SectionMeta.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

        content_row = 2
        if stage == "writing":
            parent.rowconfigure(2, weight=0)
            parent.rowconfigure(3, weight=1)
            self._build_writing_live_panel(parent, row=2)
            content_row = 3
        elif stage == "figures":
            parent.rowconfigure(2, weight=0)
            parent.rowconfigure(3, weight=1)
            self._build_figure_live_panel(parent, row=2)
            content_row = 3
        elif stage == "journal_author":
            parent.rowconfigure(2, weight=0)
            parent.rowconfigure(3, weight=1)
            self._build_journal_author_live_panel(parent, row=2)
            content_row = 3

        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.grid(row=content_row, column=0, sticky="nsew")

        left = ttk.Frame(paned)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        paned.add(left, weight=3)

        table_header = ttk.Frame(left)
        table_header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        table_header.columnconfigure(2, weight=1)
        ttk.Label(table_header, text="全部论文", style="SectionTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 10),
        )
        ttk.Label(table_header, text="搜索", style="SectionMeta.TLabel").grid(
            row=0,
            column=1,
            sticky="w",
            padx=(0, 7),
        )
        self.stage_search_vars[stage] = tk.StringVar(value="")
        stage_search = ttk.Entry(table_header, textvariable=self.stage_search_vars[stage])
        stage_search.grid(row=0, column=2, sticky="ew")
        stage_search.bind(
            "<KeyRelease>",
            lambda _event, s=stage: self._schedule_stage_filter_refresh(s),
        )
        stage_search.bind(
            "<Escape>",
            lambda _event, s=stage: self._clear_stage_filters(s),
        )
        self.stage_filter_vars[stage] = tk.StringVar(value="全部状态")
        stage_filter = ttk.Combobox(
            table_header,
            textvariable=self.stage_filter_vars[stage],
            values=("全部状态", "运行中", "等待处理", "阻塞/失败", "已完成"),
            width=11,
            state="readonly",
        )
        stage_filter.grid(row=0, column=3, sticky="e", padx=(8, 0))
        stage_filter.bind(
            "<<ComboboxSelected>>",
            lambda _event, s=stage: self.render_stage_tables(only_stage=s),
        )
        ttk.Button(
            table_header,
            text="重置",
            command=lambda s=stage: self._clear_stage_filters(s),
            style="Quiet.TButton",
        ).grid(row=0, column=4, padx=(5, 0))

        columns = ("status", "action", "queue", "detail")
        tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="extended")
        tree.heading("#0", text="论文编号")
        tree.column("#0", width=290, anchor=tk.W, stretch=False)
        tree.heading("status", text=f"{STAGE_LABELS[stage]}状态")
        tree.heading("action", text="下一步")
        tree.heading("queue", text="队列")
        tree.heading("detail", text="阶段细节/卡点")
        tree.column("status", width=110, anchor=tk.W)
        tree.column("action", width=145, anchor=tk.W)
        tree.column("queue", width=45, anchor=tk.W)
        tree.column("detail", width=270, anchor=tk.W)
        tree.grid(row=1, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", lambda _event, s=stage: self._on_stage_selection_changed(s))
        tree.bind("<Button-3>", lambda event, s=stage: self.show_stage_context_menu(s, event))
        self._bind_keyboard_context_menu(
            tree,
            lambda event, s=stage: self.show_stage_context_menu(s, event, keyboard=True),
        )
        self._configure_tree_tags(tree)
        self.stage_trees[stage] = tree
        self._refresh_stage_action_state(stage)

        ybar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=tree.yview)
        ybar.grid(row=1, column=1, sticky="ns")
        xbar = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=tree.xview)
        xbar.grid(row=2, column=0, sticky="ew")
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        paned.add(right, weight=2)
        self._install_responsive_detail_pane(paned)
        detail_panel = StructuredDetailPanel(
            right,
            empty_title="请选择一篇论文",
            technical_label="技术信息",
            empty_image=self.ui_icons.icon(
                {
                    "writing": "writing",
                    "figures": "figures",
                    "journal_author": "authors",
                    "submission": "submission",
                }.get(stage, "document"),
                size=72,
                tone="info",
                disc=True,
            ),
        )
        detail_panel.grid(row=0, column=0, sticky="nsew")
        self.stage_detail_panels[stage] = detail_panel
        self.stage_detail_texts[stage] = detail_panel.technical_text

    def _build_writing_live_panel(self, parent: ttk.Frame, *, row: int) -> None:
        panel = ttk.LabelFrame(
            parent,
            text="当前执行任务",
            padding=(8, 6),
        )
        panel.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        panel.columnconfigure(0, weight=1)

        self.writing_live_summary_var = tk.StringVar(value="正在读取写作任务…")
        ttk.Label(panel, textvariable=self.writing_live_summary_var, anchor=tk.W).grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 4),
        )
        self.stage_technical_vars["writing"] = tk.BooleanVar(value=False)
        self.stage_live_expanded_vars["writing"] = tk.BooleanVar(
            value=self.winfo_screenheight() >= 850
        )
        ttk.Checkbutton(
            panel,
            text="展开任务",
            variable=self.stage_live_expanded_vars["writing"],
            command=lambda: self._toggle_live_panel("writing"),
        ).grid(row=0, column=1, sticky="e", pady=(0, 4))
        ttk.Checkbutton(
            panel,
            text="显示模型与会话",
            variable=self.stage_technical_vars["writing"],
            command=lambda: self._toggle_live_technical_columns("writing"),
        ).grid(row=0, column=2, sticky="e", padx=(8, 0), pady=(0, 4))

        columns = ("status", "step", "executor", "progress", "session", "blocker", "updated")
        tree = ttk.Treeview(panel, columns=columns, show="tree headings", selectmode="browse", height=6)
        tree.heading("#0", text="论文 / 待命名任务")
        tree.column("#0", width=280, minwidth=210, anchor=tk.W, stretch=False)
        tree.heading("status", text="实时状态")
        tree.column("status", width=95, minwidth=80, anchor=tk.W, stretch=False)
        tree.heading("step", text="当前 Step")
        tree.column("step", width=150, minwidth=125, anchor=tk.W, stretch=False)
        tree.heading("executor", text="模型 / 推理")
        tree.column("executor", width=175, minwidth=150, anchor=tk.W, stretch=False)
        tree.heading("progress", text="总进度")
        tree.column("progress", width=125, minwidth=105, anchor=tk.W, stretch=False)
        tree.heading("session", text="固定 SDK 会话")
        tree.column("session", width=205, minwidth=175, anchor=tk.W, stretch=False)
        tree.heading("blocker", text="等待 / 卡点")
        tree.column("blocker", width=285, minwidth=190, anchor=tk.W)
        tree.heading("updated", text="最后更新")
        tree.column("updated", width=100, minwidth=90, anchor=tk.W, stretch=False)
        tree.grid(row=1, column=0, sticky="ew")
        tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._on_live_task_selection_changed(
                "writing",
                self.update_writing_task_detail,
            ),
        )
        tree.bind("<Button-3>", lambda event: self.show_live_task_context_menu("writing", event))
        self._bind_keyboard_context_menu(
            tree,
            lambda event: self.show_live_task_context_menu("writing", event, keyboard=True),
        )
        self._configure_tree_tags(tree)
        self.writing_task_tree = tree
        self.live_task_trees["writing"] = tree
        self._toggle_live_technical_columns("writing")

        ybar = ttk.Scrollbar(panel, orient=tk.VERTICAL, command=tree.yview)
        ybar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=ybar.set)
        self.live_task_scrollbars["writing"] = ybar
        self._toggle_live_panel("writing")

    def _build_figure_live_panel(self, parent: ttk.Frame, *, row: int) -> None:
        panel = ttk.LabelFrame(
            parent,
            text="当前执行任务",
            padding=(8, 6),
        )
        panel.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        panel.columnconfigure(0, weight=1)
        self.figure_live_summary_var = tk.StringVar(value="正在读取画图任务…")
        ttk.Label(panel, textvariable=self.figure_live_summary_var, anchor=tk.W).grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 4),
        )
        self.stage_technical_vars["figures"] = tk.BooleanVar(value=False)
        self.stage_live_expanded_vars["figures"] = tk.BooleanVar(
            value=self.winfo_screenheight() >= 850
        )
        ttk.Checkbutton(
            panel,
            text="展开任务",
            variable=self.stage_live_expanded_vars["figures"],
            command=lambda: self._toggle_live_panel("figures"),
        ).grid(row=0, column=1, sticky="e", pady=(0, 4))
        ttk.Checkbutton(
            panel,
            text="显示模型与会话",
            variable=self.stage_technical_vars["figures"],
            command=lambda: self._toggle_live_technical_columns("figures"),
        ).grid(row=0, column=2, sticky="e", padx=(8, 0), pady=(0, 4))
        columns = ("status", "step", "executor", "progress", "session", "blocker", "updated")
        tree = ttk.Treeview(panel, columns=columns, show="tree headings", selectmode="browse", height=5)
        tree.heading("#0", text="论文")
        tree.column("#0", width=280, minwidth=210, anchor=tk.W, stretch=False)
        tree.heading("status", text="实时状态")
        tree.column("status", width=100, minwidth=85, anchor=tk.W, stretch=False)
        tree.heading("step", text="当前 Step")
        tree.column("step", width=165, minwidth=130, anchor=tk.W, stretch=False)
        tree.heading("executor", text="模型 / 推理")
        tree.column("executor", width=175, minwidth=150, anchor=tk.W, stretch=False)
        tree.heading("progress", text="总进度")
        tree.column("progress", width=125, minwidth=105, anchor=tk.W, stretch=False)
        tree.heading("session", text="固定 SDK 会话")
        tree.column("session", width=205, minwidth=175, anchor=tk.W, stretch=False)
        tree.heading("blocker", text="等待 / 验收卡点")
        tree.column("blocker", width=300, minwidth=195, anchor=tk.W)
        tree.heading("updated", text="最后更新")
        tree.column("updated", width=100, minwidth=90, anchor=tk.W, stretch=False)
        tree.grid(row=1, column=0, sticky="ew")
        tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._on_live_task_selection_changed(
                "figures",
                self.update_figure_task_detail,
            ),
        )
        tree.bind("<Button-3>", lambda event: self.show_live_task_context_menu("figures", event))
        self._bind_keyboard_context_menu(
            tree,
            lambda event: self.show_live_task_context_menu("figures", event, keyboard=True),
        )
        self._configure_tree_tags(tree)
        self.figure_task_tree = tree
        self.live_task_trees["figures"] = tree
        self._toggle_live_technical_columns("figures")
        ybar = ttk.Scrollbar(panel, orient=tk.VERTICAL, command=tree.yview)
        ybar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=ybar.set)
        self.live_task_scrollbars["figures"] = ybar
        self._toggle_live_panel("figures")
    def _build_journal_author_live_panel(self, parent: ttk.Frame, *, row: int) -> None:
        panel = ttk.LabelFrame(
            parent,
            text="当前执行任务",
            padding=(8, 6),
        )
        panel.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        panel.columnconfigure(0, weight=1)
        self.journal_author_live_summary_var = tk.StringVar(value="正在读取选刊作者任务…")
        ttk.Label(panel, textvariable=self.journal_author_live_summary_var, anchor=tk.W).grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 4),
        )
        self.stage_technical_vars["journal_author"] = tk.BooleanVar(value=False)
        self.stage_live_expanded_vars["journal_author"] = tk.BooleanVar(
            value=self.winfo_screenheight() >= 850
        )
        ttk.Checkbutton(
            panel,
            text="展开任务",
            variable=self.stage_live_expanded_vars["journal_author"],
            command=lambda: self._toggle_live_panel("journal_author"),
        ).grid(row=0, column=1, sticky="e", pady=(0, 4))
        ttk.Checkbutton(
            panel,
            text="显示模型与会话",
            variable=self.stage_technical_vars["journal_author"],
            command=lambda: self._toggle_live_technical_columns("journal_author"),
        ).grid(row=0, column=2, sticky="e", padx=(8, 0), pady=(0, 4))
        columns = ("status", "step", "executor", "progress", "session", "blocker", "updated")
        tree = ttk.Treeview(panel, columns=columns, show="tree headings", selectmode="browse", height=5)
        tree.heading("#0", text="论文")
        tree.column("#0", width=280, minwidth=210, anchor=tk.W, stretch=False)
        tree.heading("status", text="实时状态")
        tree.column("status", width=105, minwidth=85, anchor=tk.W, stretch=False)
        tree.heading("step", text="当前任务")
        tree.column("step", width=165, minwidth=130, anchor=tk.W, stretch=False)
        tree.heading("executor", text="模型 / 推理")
        tree.column("executor", width=175, minwidth=150, anchor=tk.W, stretch=False)
        tree.heading("progress", text="总进度")
        tree.column("progress", width=125, minwidth=105, anchor=tk.W, stretch=False)
        tree.heading("session", text="固定 SDK 会话")
        tree.column("session", width=205, minwidth=175, anchor=tk.W, stretch=False)
        tree.heading("blocker", text="等待 / 验收卡点")
        tree.column("blocker", width=300, minwidth=195, anchor=tk.W)
        tree.heading("updated", text="最后更新")
        tree.column("updated", width=100, minwidth=90, anchor=tk.W, stretch=False)
        tree.grid(row=1, column=0, sticky="ew")
        tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._on_live_task_selection_changed(
                "journal_author",
                self.update_journal_author_task_detail,
            ),
        )
        tree.bind("<Button-3>", lambda event: self.show_live_task_context_menu("journal_author", event))
        self._bind_keyboard_context_menu(
            tree,
            lambda event: self.show_live_task_context_menu("journal_author", event, keyboard=True),
        )
        self._configure_tree_tags(tree)
        self.journal_author_task_tree = tree
        self.live_task_trees["journal_author"] = tree
        self._toggle_live_technical_columns("journal_author")
        ybar = ttk.Scrollbar(panel, orient=tk.VERTICAL, command=tree.yview)
        ybar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=ybar.set)
        self.live_task_scrollbars["journal_author"] = ybar
        self._toggle_live_panel("journal_author")
    def _toggle_live_technical_columns(self, stage: str) -> None:
        tree = self.live_task_trees.get(stage)
        variable = self.stage_technical_vars.get(stage)
        if tree is None or variable is None:
            return
        basic = ("status", "step", "progress", "blocker", "updated")
        technical = ("status", "step", "executor", "progress", "session", "blocker", "updated")
        tree.configure(displaycolumns=technical if variable.get() else basic)

    def _toggle_live_panel(self, stage: str) -> None:
        tree = self.live_task_trees.get(stage)
        scrollbar = self.live_task_scrollbars.get(stage)
        variable = self.stage_live_expanded_vars.get(stage)
        if tree is None or scrollbar is None or variable is None:
            return
        if variable.get():
            tree.grid()
            scrollbar.grid()
        else:
            tree.grid_remove()
            scrollbar.grid_remove()

    @staticmethod
    def _fit_live_tree_height(tree: ttk.Treeview, row_count: int) -> None:
        # The live area is a status preview.  It should never consume most of
        # the page merely because its original fixed height was six rows.
        # On taller displays a third row is useful; 720p-class windows reserve
        # the extra space for the complete paper table below.
        try:
            max_rows = 3 if tree.winfo_toplevel().winfo_height() >= 820 else 2
        except (AttributeError, tk.TclError):
            max_rows = 2
        tree.configure(height=max(2, min(max_rows, int(row_count or 0))))

    @staticmethod
    def _visible_live_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the live preview focused on unfinished work.

        Completed jobs remain available in the full paper table and technical
        history.  Repeating them in the small live preview displaced the main
        workspace on laptop displays and made "current tasks" misleading.
        """

        terminal = {"complete", "completed", "done", "success"}
        return [
            row
            for row in rows
            if str(row.get("status") or "").strip().casefold() not in terminal
        ]

    def _build_smart_submission_tab(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(0, weight=1)

        common = ttk.LabelFrame(controls, text="快速操作", padding=(6, 3))
        common.grid(row=0, column=0, sticky="ew")
        common_buttons: list[tk.Widget] = [
            ttk.Button(
                common,
                text="启动/继续智能投稿",
                command=self.start_smart_submission_visible_online,
                style="Accent.TButton",
                image=self.ui_icons.icon("active", size=15, tone="inverse"),
                compound=tk.LEFT,
            ),
            ttk.Button(
                common,
                text="立即检查状态",
                command=self.run_smart_submission_monitor_now,
                image=self.ui_icons.icon("refresh", size=15),
                compound=tk.LEFT,
            ),
        ]
        self.smart_context_action_button = ttk.Button(
            common,
            text="当前无需人工操作",
            command=self._run_smart_context_action,
            state="disabled",
            image=self.ui_icons.icon("human", size=15),
            compound=tk.LEFT,
        )
        common_buttons.append(self.smart_context_action_button)
        common_buttons.append(
            ttk.Button(
                common,
                text="暂停智能投稿",
                command=self.pause_smart_submission,
                style="Warning.TButton",
                image=self.ui_icons.icon("stop", size=14, tone="attention"),
                compound=tk.LEFT,
            )
        )
        self._install_responsive_action_layout(common, common_buttons)

        management = ttk.LabelFrame(controls, text="配置", padding=(6, 3))
        management.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        smart_settings_button = ttk.Button(management, text="智能投稿设置", command=self.open_smart_submission_settings)
        smart_settings_button.configure(
            image=self.ui_icons.icon("grid", size=15),
            compound=tk.LEFT,
        )
        management_widgets: list[tk.Widget] = [smart_settings_button]
        self._install_responsive_action_layout(management, management_widgets)

        more_button = ttk.Button(controls, text="更多操作", style="Quiet.TButton")
        more_button.configure(
            command=lambda b=more_button: self.show_smart_submission_tools(b),
            image=self.ui_icons.icon("more", size=15),
            compound=tk.LEFT,
        )
        more_button.grid(row=0, column=2, sticky="e", padx=(8, 3))

        smart_control_layout = {"compact": False}

        def reflow_smart_controls(event: tk.Event) -> None:
            compact = int(getattr(event, "width", controls.winfo_width()) or 0) < 860
            if compact == smart_control_layout["compact"]:
                return
            smart_control_layout["compact"] = compact
            if compact:
                management.grid_configure(row=1, column=0, sticky="ew", padx=(0, 0), pady=(4, 0))
                more_button.grid_configure(row=1, column=1, sticky="e", pady=(4, 0))
            else:
                management.grid_configure(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 0))
                more_button.grid_configure(row=0, column=2, sticky="e", pady=(0, 0))

        controls.bind("<Configure>", reflow_smart_controls, add="+")

        info = ttk.Frame(parent)
        info.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        info.columnconfigure(0, weight=1)
        self.smart_submission_notice_var = tk.StringVar(
            value="试运行通道，不替代稳定投稿。只有明确选择智能投稿路线的论文才允许进入在线任务。"
        )
        smart_notice_label = ttk.Label(
            info,
            textvariable=self.smart_submission_notice_var,
            style="Muted.TLabel",
            justify=tk.LEFT,
        )
        show_smart_guidance = self.winfo_screenheight() >= 850
        if show_smart_guidance:
            smart_notice_label.grid(row=0, column=0, columnspan=2, sticky="ew")
        smart_notice_label.bind(
            "<Configure>",
            lambda event: smart_notice_label.configure(
                wraplength=max(320, int(event.width))
            ),
            add="+",
        )

        self.smart_action_hint_var = tk.StringVar(value="选择一篇论文后，系统会显示当前可执行的人工动作。")
        ttk.Label(
            info,
            textvariable=self.smart_action_hint_var,
            style="SectionMeta.TLabel",
        ).grid(
            row=1 if show_smart_guidance else 0,
            column=0,
            sticky="w",
            pady=(4 if show_smart_guidance else 0, 0),
        )

        self.smart_metrics_expanded_var = tk.BooleanVar(
            value=self.winfo_screenheight() >= 850
        )
        ttk.Checkbutton(
            info,
            text="展开运行概览",
            variable=self.smart_metrics_expanded_var,
            command=self._toggle_smart_metrics,
        ).grid(
            row=1 if show_smart_guidance else 0,
            column=1,
            sticky="e",
            padx=(12, 0),
            pady=(4 if show_smart_guidance else 0, 0),
        )

        self.smart_submission_monitor_result_var = tk.StringVar(
            value="○ 尚未检查投稿状态"
        )
        self.smart_submission_monitor_result_label = ttk.Label(
            info,
            textvariable=self.smart_submission_monitor_result_var,
            anchor=tk.W,
            justify=tk.LEFT,
            foreground="#666666",
        )
        monitor_row = 2 if show_smart_guidance else 1
        self.smart_submission_monitor_result_label.grid(
            row=monitor_row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(4, 0),
        )

        self.smart_submission_status_var = tk.StringVar(value="")
        smart_status_label = ttk.Label(
            info,
            textvariable=self.smart_submission_status_var,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        smart_status_label.grid(
            row=monitor_row + 1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(3, 0),
        )
        smart_status_label.bind(
            "<Configure>",
            lambda event: smart_status_label.configure(
                wraplength=max(320, int(event.width))
            ),
            add="+",
        )

        self.smart_metrics = MetricStrip(
            parent,
            (
                ("enabled", "已启用论文"),
                ("online", "在线队列"),
                ("running", "执行中"),
                ("human", "等待人工"),
                ("monitor", "监控中"),
            ),
            compact=True,
            icons={
                "enabled": self.ui_icons.icon("document", size=36, tone="info", disc=True),
                "online": self.ui_icons.icon("pipeline", size=36, tone="success", disc=True),
                "running": self.ui_icons.icon("active", size=36, tone="info", disc=True),
                "human": self.ui_icons.icon("human", size=36, tone="attention", disc=True),
                "monitor": self.ui_icons.icon("agent", size=36, tone="done", disc=True),
            },
        )
        self.smart_metrics.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._toggle_smart_metrics()

        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.grid(row=3, column=0, sticky="nsew")

        left = ttk.Frame(paned)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        paned.add(left, weight=5)

        table_header = ttk.Frame(left)
        table_header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        table_header.columnconfigure(0, weight=1)
        ttk.Label(table_header, text="投稿任务", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.smart_show_technical_columns_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            table_header,
            text="显示执行器与队列",
            variable=self.smart_show_technical_columns_var,
            command=self._toggle_smart_technical_columns,
        ).grid(row=0, column=1, sticky="e")

        # Keep the established column identifiers for saved UI integrations;
        # their headings and values now expose the program control plane.
        columns = ("status", "workflow", "next", "queue", "journal")
        columns = columns[:1] + ("monitor",) + columns[1:2] + ("credential",) + columns[2:]
        tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="extended")
        tree.heading("#0", text="论文编号")
        tree.heading("status", text="投稿状态")
        tree.heading("monitor", text="状态检查")
        tree.heading("workflow", text="下一动作")
        tree.heading("credential", text="账号状态")
        tree.heading("next", text="执行方式")
        tree.heading("queue", text="队列")
        tree.heading("journal", text="目标期刊 / 投稿系统")
        tree.column("#0", width=275, anchor=tk.W, stretch=False)
        tree.column("status", width=100, anchor=tk.W)
        tree.column("monitor", width=130, minwidth=105, anchor=tk.W)
        tree.column("workflow", width=145, anchor=tk.W)
        tree.column("credential", width=120, anchor=tk.W)
        tree.column("next", width=100, anchor=tk.W)
        tree.column("queue", width=48, anchor=tk.W)
        tree.column("journal", width=175, anchor=tk.W)
        tree.grid(row=1, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", lambda _event: self._on_smart_selection_changed())
        tree.bind("<Button-3>", self.show_smart_submission_context_menu)
        self._bind_keyboard_context_menu(
            tree,
            lambda event: self.show_smart_submission_context_menu(event, keyboard=True),
        )
        self._configure_tree_tags(tree)
        self.smart_submission_tree = tree
        self._toggle_smart_technical_columns()

        ybar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=tree.yview)
        ybar.grid(row=1, column=1, sticky="ns")
        xbar = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=tree.xview)
        xbar.grid(row=2, column=0, sticky="ew")
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        paned.add(right, weight=2)
        self._install_responsive_detail_pane(paned)
        self.smart_submission_detail_panel = StructuredDetailPanel(
            right,
            empty_title="请选择一篇投稿任务",
            technical_label="技术日志",
            empty_image=self.ui_icons.icon("submission", size=72, tone="done", disc=True),
        )
        self.smart_submission_detail_panel.grid(row=0, column=0, sticky="nsew")
        self.smart_submission_text = self.smart_submission_detail_panel.technical_text

    def _toggle_smart_technical_columns(self) -> None:
        tree = self.smart_submission_tree
        variable = getattr(self, "smart_show_technical_columns_var", None)
        if tree is None or variable is None:
            return
        basic = ("status", "monitor", "workflow", "credential", "journal")
        technical = ("status", "monitor", "workflow", "credential", "next", "queue", "journal")
        tree.configure(displaycolumns=technical if variable.get() else basic)

    def _toggle_smart_metrics(self) -> None:
        metrics = getattr(self, "smart_metrics", None)
        variable = getattr(self, "smart_metrics_expanded_var", None)
        if metrics is None or variable is None:
            return
        if variable.get():
            metrics.grid()
        else:
            metrics.grid_remove()

    def _on_smart_selection_changed(self) -> None:
        self.update_smart_submission_detail()
        self._refresh_smart_context_action()

    def _refresh_smart_context_action(self) -> None:
        button = getattr(self, "smart_context_action_button", None)
        hint_var = getattr(self, "smart_action_hint_var", None)
        tree = getattr(self, "smart_submission_tree", None)
        if button is None or hint_var is None or tree is None:
            return
        selected = [str(item) for item in tree.selection()]
        if len(selected) != 1:
            self._smart_context_action = ""
            button.configure(text="当前无需人工操作", state="disabled")
            hint_var.set("只选择一篇论文后，系统会根据投稿状态显示可执行动作。")
            return
        paper_id = selected[0]
        smart_state = getattr(self, "smart_submission_state", {})
        papers = (
            smart_state.get("papers", {})
            if isinstance(smart_state, dict)
            and isinstance(smart_state.get("papers"), dict)
            else {}
        )
        paper = papers.get(paper_id, {}) if isinstance(papers.get(paper_id), dict) else {}
        if not paper:
            candidates = getattr(self, "smart_submission_candidates", {})
            if not isinstance(candidates, dict):
                candidates = {}
            candidate = candidates.get(paper_id, {})
            paper = candidate if isinstance(candidate, dict) else {}
        status = str(paper.get("status") or "candidate").strip().casefold()
        next_action = str(paper.get("next_action") or "").strip().casefold()
        takeover = paper.get("manual_takeover") if isinstance(paper.get("manual_takeover"), dict) else {}
        takeover_status = str(takeover.get("status") or "").strip().casefold()
        credential = str(paper.get("credential_broker_status") or "").strip().casefold()

        if takeover_status in {"active", "opened", "requested", "waiting", "in_progress"} or next_action == "reconcile_manual_page_then_continue":
            self._smart_context_action = "complete_manual"
            button.configure(text="修改完成并继续", state="normal")
            hint_var.set("当前论文处于人工修改模式；完成网页修改后点击此按钮重新检查并继续。")
            return
        if status in {"retarget_needed", "rejected"} or "retarget" in next_action:
            self._smart_context_action = "retarget"
            button.configure(text="转投其他期刊", state="normal")
            hint_var.set("当前状态适合转投；你可以指定期刊，或让系统按偏好重新选刊。")
            return
        manual_credentials = {
            "human_verification",
            "waiting_terms_confirmation",
            "registration_form_incomplete",
            "login_form_incomplete",
            "orcid_login_incomplete",
            "waiting_orcid_authorization",
        }
        if (
            next_action == "manual_takeover_edit_current_page"
            or credential in manual_credentials
            or status == "revision_needed"
        ):
            self._smart_context_action = "start_manual"
            button.configure(text="人工修改当前页面", state="normal")
            hint_var.set("当前步骤需要在可见网页中人工完成；系统会保留任务上下文。")
            return
        self._smart_context_action = ""
        button.configure(text="当前无需人工操作", state="disabled")
        hint_var.set("当前论文可由程序继续处理；转投、人工接管等低频操作在“更多操作”中。")

    def _run_smart_context_action(self) -> None:
        action = str(getattr(self, "_smart_context_action", "") or "")
        if action == "complete_manual":
            self.complete_smart_manual_takeover()
        elif action == "retarget":
            self.open_smart_retarget_dialog()
        elif action == "start_manual":
            self.start_smart_manual_takeover()

    def _readonly_text(self, parent: tk.Widget) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        if not isinstance(parent, ttk.Notebook):
            frame.grid(row=0, column=0, sticky="nsew")
        text = tk.Text(frame, wrap="word", height=12, borderwidth=0, highlightthickness=0)
        configure_text_widget(text, readonly=True)
        text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=bar.set, state="disabled")
        return text

    def _configure_tree_tags(self, tree: ttk.Treeview) -> None:
        tree.tag_configure("ready_submit", background=THEME.attention_muted)
        tree.tag_configure("candidate", background=THEME.surface, foreground=THEME.text_muted)
        # A paper being merely available is the normal state, so keep it
        # visually neutral.  Colour is reserved for actionable transitions
        # such as queued/running/blocked/completed; otherwise a long overview
        # becomes one solid blue block and status loses its meaning.
        tree.tag_configure("ready", background=THEME.surface)
        tree.tag_configure("waiting", background=THEME.surface_subtle)
        tree.tag_configure("blocked", background=THEME.danger_muted)
        # Keep normal and completed rows neutral.  Status is carried by the
        # first status cell, while full-row colour is reserved for queueing,
        # blockers and explicit overrides.  This prevents a mature workspace
        # from becoming a wall of green or blue text.
        tree.tag_configure("done", background=THEME.surface, foreground=THEME.success)
        tree.tag_configure("done", foreground=THEME.text)
        tree.tag_configure("queued", background=THEME.accent_muted)
        tree.tag_configure("queued", foreground=THEME.text)
        tree.tag_configure("running", background="#f7fbff", foreground=THEME.text)
        tree.tag_configure("override", background=THEME.severe_muted)

    @staticmethod
    def _bind_keyboard_context_menu(widget: tk.Misc, callback: Callable[[tk.Event], Any]) -> None:
        for sequence in ("<Shift-F10>", "<KeyPress-App>", "<KeyPress-Menu>"):
            try:
                widget.bind(sequence, callback, add="+")
            except tk.TclError:
                continue

    @staticmethod
    def _context_menu_coordinates(tree: ttk.Treeview, event: tk.Event, *, keyboard: bool) -> tuple[int, int]:
        if not keyboard:
            return int(event.x_root), int(event.y_root)
        focused = str(tree.focus() or "")
        if not focused and tree.selection():
            focused = str(tree.selection()[0])
            tree.focus(focused)
        box = tree.bbox(focused) if focused else ()
        if box:
            x, y, _width, height = box
            return tree.winfo_rootx() + x + 24, tree.winfo_rooty() + y + height
        return tree.winfo_rootx() + 24, tree.winfo_rooty() + 24

    def show_stage_tools(self, stage: str, button: tk.Widget) -> None:
        project = LEGACY_PROJECTS[stage]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label=f"高级：并行启动{STAGE_LABELS[stage]}（不暂停其他阶段）",
            command=lambda s=stage: self.start_stage_parallel(s),
        )
        menu.add_separator()
        menu.add_command(label="加入选中到统一队列", command=self.queue_selected)
        menu.add_command(label="清空统一队列", command=self.clear_queue)
        menu.add_command(label="预演队首下一步", command=self.advance_dry_run)
        menu.add_command(label="执行队首一步", command=self.advance_apply)
        menu.add_command(label="调度设置与提示词", command=lambda s=stage: self.open_stage_prompt_settings(s))
        if stage == "writing":
            menu.add_command(
                label="API 模式：审阅选中论文（私有暂存）",
                command=self.run_selected_api_review,
            )
        if stage == "journal_author":
            menu.add_command(label="作者资料管理", command=self.manage_author_templates)
            menu.add_command(label="更改选中论文的作者模板", command=self.apply_author_template_to_selected)
            menu.add_command(label="编辑选刊偏好", command=self.manage_selected_journal_preferences)
            menu.add_separator()
            menu.add_command(label="只重新选刊（原 Step 2）", command=self.rerun_selected_journal_selection)
            menu.add_command(label="只重新生成投稿包（原 Step 3）", command=self.rerun_selected_journal_package)
            menu.add_command(
                label="打开 journal.md",
                command=lambda: self.open_selected_journal_author_artifact("journal"),
            )
            menu.add_command(
                label="打开 main.pdf",
                command=lambda: self.open_selected_journal_author_artifact("pdf"),
            )
            menu.add_command(
                label="打开投稿包文件夹",
                command=lambda: self.open_selected_journal_author_artifact("package"),
            )
        menu.add_separator()
        menu.add_command(label="查看本阶段运行状态", command=lambda s=stage: self.show_stage_scheduler_status(s))
        menu.add_command(label="打开项目目录", command=lambda p=project["root"]: self.open_path(p))
        menu.add_command(label="打开状态文件", command=lambda p=project["state_path"]: self.open_path(p))
        menu.add_command(label="查看调度提示词", command=lambda p=project["prompt_path"]: self.open_path(p))
        menu.add_separator()
        menu.add_command(label="归档选中论文（所有阶段）", command=self.archive_selected)
        menu.add_command(label="彻底删除选中论文（所有阶段及文件）…", command=self.purge_selected)
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def show_smart_submission_tools(self, button: tk.Widget) -> None:
        menu = tk.Menu(self, tearoff=0)
        has_selection = bool(self.smart_submission_tree is not None and self.smart_submission_tree.selection())
        route_state = "normal" if has_selection else "disabled"
        menu.add_command(
            label="启用并设为智能投稿路线（仅登记，不联网）",
            command=self.import_to_smart_submission,
            state=route_state,
        )
        menu.add_command(
            label="改回稳定投稿路线",
            command=lambda: self.change_selected_submission_route("legacy_auto", "production"),
            state=route_state,
        )
        if self._smart_final_submit_available():
            menu.add_command(label="确认最终提交…", command=self.confirm_smart_submission_final_submit)
        menu.add_command(
            label="转投 / 改投其他期刊…",
            command=self.open_smart_retarget_dialog,
            state=route_state,
        )
        menu.add_separator()
        menu.add_command(label="初始化或修复智能投稿模块", command=self.init_smart_submission)
        menu.add_command(
            label="高级：启用并执行本地预检…",
            command=self.request_smart_submission_run,
        )
        menu.add_command(
            label="高级：重新生成程序计划（不联网）",
            command=self.build_smart_submission_program_plan,
        )
        menu.add_command(label="重新执行本地预检", command=self.run_smart_submission_local_once)
        menu.add_command(label="处理已确认的常用声明", command=self.apply_smart_submission_declarations)
        menu.add_command(label="作者资料管理", command=self.manage_author_templates)
        menu.add_command(
            label="套用作者模板到选中论文",
            command=self.apply_author_template_to_selected,
            state=route_state,
        )
        menu.add_separator()
        menu.add_command(label="清理旧浏览器标签", command=self.cleanup_smart_submission_browser_tabs)
        menu.add_command(label="继续登录 / 注册 / 验证", command=self.resume_smart_submission_login)
        menu.add_command(label="手动修改当前页面", command=self.start_smart_manual_takeover)
        menu.add_command(label="修改完成，重新检查并继续", command=self.complete_smart_manual_takeover)
        menu.add_command(label="解除权限等待并重新排队", command=self.pause_smart_submission_waiting_approval)
        menu.add_separator()
        menu.add_command(label="查看 SDK 调度状态", command=self.open_smart_submission_scheduler_thread)
        menu.add_command(label="查看当前 SDK 会话", command=self.open_smart_submission_worker_thread)
        menu.add_command(label="打开模块目录", command=lambda: self.open_path(SMART_SUBMISSION_DIR))
        menu.add_command(label="打开状态文件", command=lambda: self.open_path(SMART_STATE_PATH))
        menu.add_command(label="查看调度提示词", command=lambda: self.open_path(SMART_SCHEDULER_PROMPT_PATH))
        menu.add_command(label="打开审计记录", command=lambda: self.open_path(SMART_AUDIT_DIR))
        menu.add_separator()
        menu.add_command(
            label="归档选中论文（所有阶段）",
            command=self.archive_selected,
            state=route_state,
        )
        menu.add_command(
            label="彻底删除选中论文（所有阶段及文件）…",
            command=self.purge_selected,
            state=route_state,
        )
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def show_stage_context_menu(self, stage: str, event: tk.Event, *, keyboard: bool = False) -> str | None:
        tree = self.stage_trees.get(stage)
        if tree is None:
            return
        row_id = str(tree.focus() or "") if keyboard else tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
            tree.focus(row_id)
            self.update_stage_detail(stage)
        menu = tk.Menu(self, tearoff=0)
        if stage == "writing":
            menu.add_command(
                label="人工越级进入画图阶段…",
                command=lambda: self.force_selected_into_stage("figures"),
            )
            menu.add_separator()
        if stage == "figures":
            menu.add_command(label="从第 1 步重新执行", command=self.queue_selected_figures_from_step1)
            menu.add_command(
                label="人工越级进入选刊作者阶段…",
                command=lambda: self.force_selected_into_stage("journal_author"),
            )
            menu.add_separator()
        if stage == "journal_author":
            menu.add_command(label="更改作者资料模板", command=self.apply_author_template_to_selected)
            menu.add_command(label="作者资料管理", command=self.manage_author_templates)
            menu.add_command(label="编辑选刊偏好", command=self.manage_selected_journal_preferences)
            menu.add_separator()
            menu.add_command(label="仅重新验收（不调用 AI）", command=self.revalidate_selected_journal_author)
            menu.add_command(label="只重新选刊（原 Step 2）", command=self.rerun_selected_journal_selection)
            menu.add_command(label="只重新生成投稿包（原 Step 3）", command=self.rerun_selected_journal_package)
            menu.add_command(label="打开 journal.md", command=lambda: self.open_selected_journal_author_artifact("journal"))
            menu.add_command(label="打开 main.pdf", command=lambda: self.open_selected_journal_author_artifact("pdf"))
            menu.add_command(label="打开投稿包文件夹", command=lambda: self.open_selected_journal_author_artifact("package"))
            menu.add_separator()
        if stage == "submission":
            menu.add_command(
                label="启用智能投稿（仅登记，不联网）",
                command=self.import_to_smart_submission,
            )
            menu.add_command(
                label="设为稳定投稿路线（默认）",
                command=lambda: self.change_selected_submission_route("legacy_auto", "production"),
            )
            menu.add_command(
                label="设为人工处理",
                command=lambda: self.change_selected_submission_route("manual", "production"),
            )
            menu.add_separator()
        selected_records = [
            self.state_data.get("papers", {}).get(paper_id, {}) for paper_id in self.selected_ids()
        ]
        if stage != "writing" and any(
            str(record.get("stage_admissions", {}).get(stage, {}).get("mode") or "") == "manual_override"
            for record in selected_records
            if isinstance(record, dict)
        ):
            menu.add_command(
                label="取消本阶段人工越级",
                command=lambda s=stage: self.clear_selected_stage_override(s),
            )
            menu.add_separator()
        menu.add_command(label="打开选中项目", command=lambda s=stage: self.open_selected_stage_project(s))
        menu.add_separator()
        menu.add_command(
            label="归档（所有阶段）",
            command=lambda paper_id=row_id: self.archive_selected([paper_id]),
        )
        menu.add_command(
            label="彻底删除（所有阶段及文件）…",
            command=lambda paper_id=row_id: self.purge_selected([paper_id]),
        )
        menu.add_separator()
        menu.add_command(label="调度设置与提示词", command=lambda s=stage: self.open_stage_prompt_settings(s))
        try:
            x_root, y_root = self._context_menu_coordinates(tree, event, keyboard=keyboard)
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
        return "break" if keyboard else None

    def show_live_task_context_menu(self, stage: str, event: tk.Event, *, keyboard: bool = False) -> str | None:
        tree = {
            "writing": self.writing_task_tree,
            "figures": self.figure_task_tree,
            "journal_author": self.journal_author_task_tree,
        }.get(stage)
        if tree is None:
            return "break" if keyboard else None
        row_id = str(tree.focus() or "") if keyboard else str(tree.identify_row(event.y) or "")
        if row_id:
            tree.selection_set(row_id)
            tree.focus(row_id)
            canonical = self.stage_trees.get(stage)
            if canonical is not None and canonical.selection():
                canonical.selection_remove(*canonical.selection())
            if stage == "writing":
                self.update_writing_task_detail()
            elif stage == "figures":
                self.update_figure_task_detail()
            else:
                self.update_journal_author_task_detail()
        if not tree.selection():
            return "break" if keyboard else None

        context_paper_id = row_id
        if stage == "writing":
            context_paper_id = str(
                self.writing_task_rows.get(row_id, {}).get("paper_id") or ""
            )
        has_paper_id = bool(context_paper_id)
        lifecycle_state = "normal" if has_paper_id else "disabled"
        menu = tk.Menu(self, tearoff=0)
        if stage == "writing":
            menu.add_command(label="查看 SDK 会话", command=self.show_selected_writing_sdk_session)
        elif stage == "figures":
            menu.add_command(label="查看 SDK 会话", command=self.show_selected_figure_sdk_session)
            menu.add_command(label="暂停这篇论文（当前 Step 后）", command=self.pause_selected_figures)
        else:
            menu.add_command(label="查看 SDK 会话", command=self.show_selected_journal_author_sdk_session)
            menu.add_command(label="暂停这篇论文（当前任务后）", command=self.pause_selected_journal_author)
            menu.add_command(label="仅重新验收（不调用 AI）", command=self.revalidate_selected_journal_author)
            menu.add_separator()
            menu.add_command(label="只重新选刊（原 Step 2）", command=self.rerun_selected_journal_selection)
            menu.add_command(label="只重新生成投稿包（原 Step 3）", command=self.rerun_selected_journal_package)
            menu.add_command(label="编辑选刊偏好", command=self.manage_selected_journal_preferences)
            menu.add_command(label="打开 journal.md", command=lambda: self.open_selected_journal_author_artifact("journal"))
            menu.add_command(label="打开 main.pdf", command=lambda: self.open_selected_journal_author_artifact("pdf"))
            menu.add_command(label="打开投稿包文件夹", command=lambda: self.open_selected_journal_author_artifact("package"))
        menu.add_separator()
        menu.add_command(
            label="归档（所有阶段）",
            command=lambda paper_id=context_paper_id: self.archive_selected([paper_id]),
            state=lifecycle_state,
        )
        menu.add_command(
            label="彻底删除（所有阶段及文件）…",
            command=lambda paper_id=context_paper_id: self.purge_selected([paper_id]),
            state=lifecycle_state,
        )
        try:
            x_root, y_root = self._context_menu_coordinates(tree, event, keyboard=keyboard)
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
        return "break" if keyboard else None

    def show_overview_context_menu(self, event: tk.Event, *, keyboard: bool = False) -> str | None:
        tree = self.tree
        row_id = str(tree.focus() or "") if keyboard else tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
            tree.focus(row_id)
            self.update_detail()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="人工越级进入画图阶段…", command=lambda: self.force_selected_into_stage("figures"))
        menu.add_command(
            label="人工越级进入选刊作者阶段…",
            command=lambda: self.force_selected_into_stage("journal_author"),
        )
        menu.add_separator()
        menu.add_command(label="加入选中到统一队列", command=self.queue_selected)
        menu.add_separator()
        menu.add_command(
            label="归档（所有阶段）",
            command=lambda paper_id=row_id: self.archive_selected([paper_id]),
        )
        menu.add_command(
            label="彻底删除（所有阶段及文件）…",
            command=lambda paper_id=row_id: self.purge_selected([paper_id]),
        )
        try:
            x_root, y_root = self._context_menu_coordinates(tree, event, keyboard=keyboard)
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
        return "break" if keyboard else None

    def _smart_final_submit_available(self) -> bool:
        tree = self.smart_submission_tree
        if tree is None:
            return False
        selected_ids = [str(item) for item in tree.selection()]
        if len(selected_ids) != 1:
            return False
        papers = (
            self.smart_submission_state.get("papers", {})
            if isinstance(self.smart_submission_state.get("papers"), dict)
            else {}
        )
        settings = (
            self.smart_submission_state.get("settings", {})
            if isinstance(self.smart_submission_state.get("settings"), dict)
            else {}
        )
        if bool(settings.get("auto_final_submit_enabled", True)):
            return False
        selected_paper = papers.get(selected_ids[0], {})
        return bool(
            isinstance(selected_paper, dict)
            and str(selected_paper.get("status") or "").strip().casefold() == "waiting"
            and str(selected_paper.get("workflow_status") or "").strip().casefold() == "waiting"
            and str(selected_paper.get("next_action") or "").strip() == FINAL_SUBMIT_REVIEW_READY_ACTION
        )

    def show_smart_submission_context_menu(self, event: tk.Event, *, keyboard: bool = False) -> str | None:
        tree = self.smart_submission_tree
        if tree is None:
            return
        row_id = str(tree.focus() or "") if keyboard else tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
            tree.focus(row_id)
            self.update_smart_submission_detail()
        if not tree.selection():
            return "break" if keyboard else None
        menu = tk.Menu(self, tearoff=0)
        selected_ids = [str(item) for item in tree.selection()]
        candidate_selected = any(
            paper_id in self.smart_submission_candidates
            for paper_id in selected_ids
        )
        if candidate_selected:
            menu.add_command(
                label="启用并启动智能投稿…",
                command=self.start_smart_submission_visible_online,
            )
            menu.add_command(
                label="仅启用智能投稿（不联网）",
                command=self.import_to_smart_submission,
            )
        else:
            menu.add_command(
                label="转投 / 改投其他期刊…",
                command=self.open_smart_retarget_dialog,
            )
            menu.add_separator()
            menu.add_command(
                label="改回稳定投稿路线",
                command=lambda: self.change_selected_submission_route("legacy_auto", "production"),
            )
            menu.add_separator()
            menu.add_command(label="移到在线队首", command=self.prioritize_smart_submission_selected)
            menu.add_command(label="继续登录 / 注册 / 验证", command=self.resume_smart_submission_login)
            menu.add_command(label="手动修改当前页面", command=self.start_smart_manual_takeover)
            menu.add_command(
                label="修改完成，重新检查并继续",
                command=self.complete_smart_manual_takeover,
            )
            menu.add_command(label="处理已确认的常用声明", command=self.apply_smart_submission_declarations)
            menu.add_command(label="作者资料管理", command=self.manage_author_templates)
            menu.add_command(
                label="套用作者模板到选中论文",
                command=self.apply_author_template_to_selected,
            )
            menu.add_command(label="查看 SDK 会话", command=self.open_smart_submission_worker_thread)
        if self._smart_final_submit_available():
            menu.add_separator()
            menu.add_command(
                label="确认最终提交…",
                command=self.confirm_smart_submission_final_submit,
            )
        menu.add_separator()
        menu.add_command(
            label="归档（所有阶段）",
            command=lambda paper_id=row_id: self.archive_selected([paper_id]),
        )
        menu.add_command(
            label="彻底删除（所有阶段及文件）…",
            command=lambda paper_id=row_id: self.purge_selected([paper_id]),
        )
        try:
            x_root, y_root = self._context_menu_coordinates(tree, event, keyboard=keyboard)
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
        return "break" if keyboard else None

    def confirm_smart_submission_final_submit(self) -> None:
        """Issue one short-lived capability only from an explicit EXE dialog click."""

        tree = self.smart_submission_tree
        selected = [str(item) for item in tree.selection()] if tree is not None else []
        if len(selected) != 1:
            messagebox.showinfo("最终提交", "请先只选择一篇已到最终审核页的论文。", parent=self)
            return
        paper_id = selected[0]
        try:
            state = self._load_smart_submission_state()
            papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
            paper = papers.get(paper_id)
            if not isinstance(paper, dict):
                raise RuntimeError("智能投稿记录不存在")
            attempt_id = str(paper.get("attempt_id") or "").strip()
            route_revision = int(paper.get("route_revision") or 0)
            challenge = prepare_final_submit_confirmation_challenge(
                paper_id,
                route_revision=route_revision,
                attempt_id=attempt_id,
                ui_window_handle=int(self.winfo_id()),
            )
        except Exception as exc:
            LOGGER.error("Final-submit UI challenge failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror(
                "尚不能最终提交",
                f"最终提交门禁未通过：\n\n{exc}",
                parent=self,
            )
            return

        package_short = str(challenge.get("package_binding_sha256") or "")[:16]
        confirmed = messagebox.askyesno(
            "最终提交确认（不可撤销）",
            "请核对本次不可撤销操作：\n\n"
            f"论文：{paper_id}\n"
            f"目标期刊：{challenge.get('target_journal') or '-'}\n"
            f"投稿站点：{challenge.get('target_origin') or '-'}\n"
            f"账号：{challenge.get('account_id') or '-'}\n"
            f"文件包指纹：{package_short}\n"
            f"本次尝试：{challenge.get('attempt_id') or '-'}\n"
            f"确认窗口截止：{challenge.get('expires_at') or '-'}\n\n"
            "点击“是”只授权这一次、这一篇论文、这一份文件包的最终提交。\n"
            "后续仍会重新校验账号、站点、文件包和浏览器会话；不会自动重复点击。",
            parent=self,
            default=messagebox.NO,
        )
        if not confirmed:
            self.refresh_smart_submission()
            return
        confirmation_phrase = str(challenge.pop("confirmation_phrase", ""))
        try:
            result = authorize_final_submit(
                paper_id,
                route_revision=int(challenge["route_revision"]),
                attempt_id=str(challenge["attempt_id"]),
                confirmation_phrase=confirmation_phrase,
                activate=False,
            )
            sdk_start = start_smart_submission_program_runtime([paper_id])
        except Exception as exc:
            LOGGER.error("Final-submit UI authorization failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror("最终提交未授权", str(exc), parent=self)
            return
        finally:
            confirmation_phrase = ""
        self.refresh_smart_submission()
        messagebox.showinfo(
            "已授权一次最终提交",
            f"一次性授权已写入审计。\n\n授权截止：{result.get('expires_at') or '-'}\n"
            "只允许这一次尝试在再次校验后执行一次最终点击。\n"
            f"SDK 执行：{'已启动' if sdk_start.get('started') else '已加入当前运行队列'}",
            parent=self,
        )

    def open_selected_stage_project(self, stage: str) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            return
        project = LEGACY_PROJECTS[stage]
        self.open_path(Path(project["papers_dir"]) / paper_ids[0])

    def _selected_or_remembered_paper_ids(self) -> list[str]:
        selected = self.selected_ids()
        if selected:
            return selected
        papers = self.state_data.get("papers", {}) if isinstance(self.state_data.get("papers"), dict) else {}
        if self.selected_paper_id and self.selected_paper_id in papers:
            return [self.selected_paper_id]
        return []

    def change_selected_submission_route(self, execution_owner: str, mode: str) -> None:
        paper_ids = self._selected_or_remembered_paper_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先选择要调整投稿路线的论文。", parent=self)
            return
        labels = {
            "legacy_auto": "稳定投稿（默认）",
            "smart": "智能投稿（试运行）",
            "manual": "人工处理",
        }
        target = labels.get(execution_owner, execution_owner)
        preview = "\n".join(paper_ids[:10])
        if len(paper_ids) > 10:
            preview += f"\n... 还有 {len(paper_ids) - 10} 篇"
        if not messagebox.askyesno(
            "更改投稿路线",
            f"确认把以下论文设为“{target}”吗？\n\n"
            "已有稿号、在线任务运行中或最终提交结果不明确时，系统会拒绝切换。\n\n"
            f"{preview}",
            parent=self,
        ):
            return

        results: list[dict[str, Any]] = []
        try:
            for paper_id in paper_ids:
                record = self.state_data.get("papers", {}).get(paper_id, {})
                route = record.get("submission_route", {}) if isinstance(record.get("submission_route"), dict) else {}
                results.append(
                    self.orchestrator.set_submission_route(
                        paper_id,
                        execution_owner=execution_owner,
                        mode=mode,
                        expected_revision=int(route.get("revision") or 1),
                    )
                )
        except Exception as exc:
            self.refresh()
            messagebox.showerror("投稿路线未更改", str(exc), parent=self)
            return
        self.refresh()
        self._show_operation_result(self.log_text, "投稿路线已更改", results)

    def open_stage_prompt_settings(self, stage: str) -> None:
        def done(inventory: dict[str, Any]) -> None:
            self.status_var.set(f"{STAGE_LABELS[stage]}设置已载入")
            self._open_stage_prompt_settings_dialog(stage, inventory)

        self._run_dialog_background_task(
            f"载入{STAGE_LABELS[stage]}设置",
            lambda: self.orchestrator.stage_prompt_inventory(stage),
            done,
        )

    def _open_stage_prompt_settings_dialog(
        self,
        stage: str,
        inventory: dict[str, Any],
    ) -> None:

        dialog = tk.Toplevel(self)
        stage_title = "稳定投稿" if stage == "submission" else str(inventory.get("label", stage))
        dialog.title(f"{stage_title}设置")
        self._fit_dialog_window(
            dialog,
            preferred=(1380, 860),
            minimum=(820, 560),
            margins=(40, 80),
        )
        dialog.transient(self)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(dialog)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        settings_tab = ttk.Frame(notebook, padding=10)
        scheduler_prompt_tab = ttk.Frame(notebook, padding=10)
        task_prompt_tab = ttk.Frame(notebook, padding=10)
        workflow_tab = (
            ttk.Frame(notebook, padding=10)
            if stage in {"writing", "figures", "journal_author"}
            else None
        )
        notebook.add(
            settings_tab,
            text="运行设置" if stage in {"writing", "figures", "journal_author"} else "调度器",
        )
        if workflow_tab is not None:
            if stage == "writing":
                notebook.add(workflow_tab, text="写作步骤")
            elif stage == "figures":
                notebook.add(workflow_tab, text="画图步骤")
            else:
                notebook.add(workflow_tab, text="选刊作者步骤")
        notebook.add(
            scheduler_prompt_tab,
            text=(
                "调度提示词（兼容）"
                if stage in {"writing", "figures", "journal_author"}
                else "调度提示词"
            ),
        )
        notebook.add(task_prompt_tab, text="任务模板")

        def make_editor(parent: ttk.Frame) -> tuple[ttk.Frame, tk.Text]:
            frame = ttk.Frame(parent)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            text = tk.Text(frame, wrap="word", undo=True, font=("Consolas", 10))
            text.grid(row=0, column=0, sticky="nsew")
            bar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
            bar.grid(row=0, column=1, sticky="ns")
            text.configure(yscrollcommand=bar.set)
            return frame, text

        def mtime_label(value: float) -> str:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if value else "-"

        settings = inventory.get("settings", {}) if isinstance(inventory.get("settings"), dict) else {}
        automation = inventory.get("automation", {}) if isinstance(inventory.get("automation"), dict) else {}
        project = LEGACY_PROJECTS[stage]

        settings_tab.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(settings_tab, text="项目目录").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Label(settings_tab, text=str(project["root"])).grid(row=row, column=1, sticky="ew", pady=5)
        row += 1
        ttk.Label(settings_tab, text="状态文件").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Label(settings_tab, text=str(project["state_path"])).grid(row=row, column=1, sticky="ew", pady=5)
        row += 1
        ttk.Label(
            settings_tab,
            text="本地执行器" if stage in {"writing", "figures", "journal_author"} else "后台自动任务",
        ).grid(
            row=row, column=0, sticky="w", pady=5
        )
        ttk.Label(
            settings_tab,
            text=(
                f"{self._automation_status_label(str(automation.get('status') or 'MISSING'))}; "
                f"{automation.get('path', '-')}"
            ),
        ).grid(row=row, column=1, sticky="ew", pady=5)
        row += 1
        if stage not in {"writing", "figures", "journal_author"}:
            ttk.Label(settings_tab, text="运行计划").grid(row=row, column=0, sticky="w", pady=5)
            ttk.Label(settings_tab, text=str(automation.get("rrule") or "-")).grid(
                row=row, column=1, sticky="ew", pady=5
            )
            row += 1

        mode_options = ["manual", "run_once", "scheduled"] if stage == "submission" else ["manual", "auto"]
        mode_var = tk.StringVar(value=scheduler_mode_label(settings.get("scheduling_mode") or mode_options[0]))
        ttk.Label(settings_tab, text="调度模式").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(
            settings_tab,
            values=[SCHEDULER_MODE_LABELS[item] for item in mode_options],
            textvariable=mode_var,
            state="readonly",
            width=18,
        ).grid(row=row, column=1, sticky="w", pady=5)
        row += 1

        heartbeat_var = tk.StringVar(value=str(settings.get("heartbeat_minutes") or project.get("heartbeat_minutes") or 5))
        if stage not in {"writing", "figures", "journal_author"}:
            ttk.Label(settings_tab, text="心跳间隔（分钟）").grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(settings_tab, textvariable=heartbeat_var, width=18).grid(
                row=row, column=1, sticky="w", pady=5
            )
            row += 1

        parallel_stage = stage in {"writing", "figures", "journal_author"}
        default_parallel = (
            str(WRITING_DEFAULT_PARALLEL_WORKERS)
            if stage == "writing"
            else str(FIGURE_MAX_PARALLEL_WORKERS)
            if stage == "figures"
            else str(JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS)
        )
        one_action_default = False if parallel_stage else True
        one_action_var = tk.BooleanVar(value=bool(settings.get("one_action_per_run", one_action_default)))
        if stage in {"writing", "figures", "journal_author"}:
            one_action_var.set(False)
        else:
            ttk.Checkbutton(settings_tab, text="每轮只做一个调度动作", variable=one_action_var).grid(
                row=row, column=1, sticky="w", pady=5
            )
        row += 1

        max_parallel_var = tk.StringVar(
            value=parallel_limit_label(settings.get("max_parallel_workers") or default_parallel)
        )
        if parallel_stage:
            if stage == "writing":
                parallel_label = "同时运行的论文上限"
                parallel_hint = (
                    f"可设置 1-{WRITING_MAX_PARALLEL_WORKERS}；每篇论文只占一个 SDK 会话，"
                    "Step 1→当前流程终点在该会话内依次执行；超过上限的论文本地排队。"
                    "降低数量不会中断正在执行的 Step。"
                )
            elif stage == "figures":
                parallel_label = "画图最大并发任务"
                parallel_hint = (
                    f"可设置 1-{FIGURE_MAX_PARALLEL_WORKERS}；每篇论文使用一个固定 SDK 会话，"
                    "Step 1→3 在该会话内依次执行，不同论文并发运行。"
                )
            else:
                parallel_label = "选刊最大并发任务"
                parallel_hint = (
                    f"可设置 1-{JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS}；每篇论文使用一个固定 SDK 会话，"
                    "程序按该论文冻结的合并/精细流程依次发送 Step；超过上限的论文本地排队。"
                )
            ttk.Label(settings_tab, text=parallel_label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(settings_tab, textvariable=max_parallel_var, width=18).grid(row=row, column=1, sticky="w", pady=5)
            row += 1
            ttk.Label(settings_tab, text=parallel_hint).grid(row=row, column=1, sticky="w", pady=(0, 5))
            row += 1

        if stage == "writing":
            gate_policy = (
                dict(inventory.get("writing_gate_policy"))
                if isinstance(inventory.get("writing_gate_policy"), dict)
                else {}
            )
            gate_labels = {
                STRICT_CONFIRMATION_MODE: "严格确认（默认）",
                AUTO_TRUST_MODE: "自动信任并继续",
            }
            gate_values = {label: mode for mode, label in gate_labels.items()}
            gate_frame = ttk.LabelFrame(settings_tab, text="Step 5 数据确认关口", padding=10)
            gate_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
            gate_frame.columnconfigure(1, weight=1)
            gate_mode_var = tk.StringVar(
                value=gate_labels.get(str(gate_policy.get("mode") or ""), gate_labels[STRICT_CONFIRMATION_MODE])
            )
            gate_status_var = tk.StringVar()

            def refresh_gate_status() -> None:
                password_label = "管理密码已设置" if gate_policy.get("password_configured") else "首次切换时设置管理密码"
                revision = int(gate_policy.get("revision") or 0)
                gate_status_var.set(
                    f"当前：{gate_labels.get(str(gate_policy.get('mode') or ''), gate_labels[STRICT_CONFIRMATION_MODE])}；"
                    f"{password_label}；策略修订 {revision}。"
                )

            refresh_gate_status()
            ttk.Label(gate_frame, text="策略").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Combobox(
                gate_frame,
                values=list(gate_values),
                textvariable=gate_mode_var,
                state="readonly",
                width=22,
            ).grid(row=0, column=1, sticky="w", pady=4)
            ttk.Label(gate_frame, textvariable=gate_status_var).grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(2, 4)
            )
            ttk.Label(
                gate_frame,
                text=(
                    "自动信任时 Step 5 仍完整执行；程序把当前 draft_v2.tex 的 SHA-256 与工作区策略绑定，"
                    "不再等待人工点击并直接进入 Step 6。Step 6 只清理标记，不得删除或改写已接受数据。\n"
                    "缺失 draft_v2、SDK 执行失败、产物不完整等硬故障仍会拦截。此设置只作用于当前论文工作区。"
                ),
                wraplength=1040,
                justify=tk.LEFT,
            ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(2, 6))

            def apply_gate_policy() -> None:
                selected_mode = gate_values.get(gate_mode_var.get(), STRICT_CONFIRMATION_MODE)
                if selected_mode == str(gate_policy.get("mode") or STRICT_CONFIRMATION_MODE):
                    messagebox.showinfo("Step 5 数据确认关口", "当前已经是所选策略，无需更改。", parent=dialog)
                    return
                password = simpledialog.askstring(
                    "Step 5 数据确认关口",
                    (
                        "请输入现有管理密码："
                        if gate_policy.get("password_configured")
                        else "首次切换需要设置管理密码（4-128 个字符）："
                    ),
                    show="*",
                    parent=dialog,
                )
                if password is None:
                    return
                confirmation = ""
                if not gate_policy.get("password_configured"):
                    confirmation_value = simpledialog.askstring(
                        "确认管理密码",
                        "请再次输入管理密码：",
                        show="*",
                        parent=dialog,
                    )
                    if confirmation_value is None:
                        return
                    confirmation = confirmation_value
                try:
                    result = self.orchestrator.set_writing_gate_policy(
                        selected_mode,
                        password=password,
                        password_confirmation=confirmation,
                    )
                except Exception as exc:
                    messagebox.showerror("Step 5 数据确认策略未更改", str(exc), parent=dialog)
                    gate_mode_var.set(
                        gate_labels.get(str(gate_policy.get("mode") or ""), gate_labels[STRICT_CONFIRMATION_MODE])
                    )
                    return
                gate_policy.clear()
                gate_policy.update(result)
                gate_mode_var.set(gate_labels.get(str(result.get("mode") or ""), gate_labels[STRICT_CONFIRMATION_MODE]))
                refresh_gate_status()
                self.refresh()
                wakeup = result.get("runtime_wakeup") if isinstance(result.get("runtime_wakeup"), dict) else {}
                wakeup_message = ""
                if wakeup.get("requested") and wakeup.get("ok") is False:
                    wakeup_message = f"\n\n策略已保存，但后台唤醒失败：{wakeup.get('message') or wakeup.get('blocker')}"
                messagebox.showinfo(
                    "Step 5 数据确认策略已保存",
                    f"当前策略：{gate_labels.get(str(result.get('mode') or ''), '-')}。{wakeup_message}",
                    parent=dialog,
                )

            ttk.Button(gate_frame, text="应用策略（需要管理密码）", command=apply_gate_policy).grid(
                row=0, column=2, sticky="e", padx=(12, 0), pady=4
            )
            row += 1

        target_thread_var = tk.StringVar(value=str(settings.get("scheduler_target_thread_id") or ""))
        if stage not in {"writing", "figures", "journal_author"}:
            ttk.Label(settings_tab, text="目标调度会话标识").grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(settings_tab, textvariable=target_thread_var).grid(
                row=row, column=1, sticky="ew", pady=5
            )
            row += 1

        settings_status_var = tk.StringVar(value="")
        ttk.Label(settings_tab, textvariable=settings_status_var).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        def save_settings() -> None:
            try:
                values = {
                    "scheduling_mode": scheduler_mode_value(mode_var.get()),
                    "one_action_per_run": one_action_var.get(),
                }
                if stage not in {"writing", "figures", "journal_author"}:
                    heartbeat = int(heartbeat_var.get().strip())
                    if heartbeat <= 0:
                        raise ValueError("heartbeat must be positive")
                    values["heartbeat_minutes"] = heartbeat
                    values["scheduler_target_thread_id"] = target_thread_var.get().strip()
                if parallel_stage:
                    values["max_parallel_workers"] = parallel_limit_value(max_parallel_var.get()) or "all"
                result = self.orchestrator.save_stage_scheduler_settings(stage, values)
            except Exception as exc:
                messagebox.showerror("保存调度器设置失败", str(exc), parent=dialog)
                return
            self.refresh()
            settings_status_var.set("已保存；后台调度任务已按调度模式同步启停。")
            self._show_operation_result(self.log_text, "调度器设置已保存", result)

        ttk.Button(settings_tab, text="保存调度器设置", command=save_settings).grid(row=row, column=1, sticky="w", pady=8)

        scheduler_prompt = inventory.get("scheduler_prompt", {})
        scheduler_prompt_tab.columnconfigure(0, weight=1)
        scheduler_prompt_tab.rowconfigure(1, weight=1)
        scheduler_path_var = tk.StringVar(value=str(scheduler_prompt.get("path") or ""))
        ttk.Label(scheduler_prompt_tab, textvariable=scheduler_path_var).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        scheduler_editor, scheduler_text = make_editor(scheduler_prompt_tab)
        scheduler_editor.grid(row=1, column=0, sticky="nsew")

        def load_scheduler_prompt() -> None:
            try:
                data = self.orchestrator.read_prompt_file(stage, scheduler_path_var.get())
            except Exception as exc:
                messagebox.showerror("读取调度提示词失败", str(exc), parent=dialog)
                return
            scheduler_text.delete("1.0", tk.END)
            scheduler_text.insert("1.0", data.get("text") or "")

        def save_scheduler_prompt() -> None:
            try:
                result = self.orchestrator.save_prompt_file(stage, scheduler_path_var.get(), scheduler_text.get("1.0", "end-1c"))
            except Exception as exc:
                messagebox.showerror("保存调度提示词失败", str(exc), parent=dialog)
                return
            self.refresh()
            self._show_operation_result(self.log_text, "调度提示词已保存", result)
            messagebox.showinfo("已保存", "调度提示词已保存，并已自动备份原文件。", parent=dialog)

        scheduler_buttons = ttk.Frame(scheduler_prompt_tab)
        scheduler_buttons.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(scheduler_buttons, text="保存", command=save_scheduler_prompt).pack(side=tk.LEFT, padx=3)
        ttk.Button(scheduler_buttons, text="重新读取", command=load_scheduler_prompt).pack(side=tk.LEFT, padx=3)
        ttk.Button(scheduler_buttons, text="打开文件", command=lambda: self.open_path(Path(scheduler_path_var.get()))).pack(side=tk.LEFT, padx=3)
        load_scheduler_prompt()

        task_prompt_tab.columnconfigure(1, weight=1)
        task_prompt_tab.rowconfigure(1, weight=1)
        task_prompts = [item for item in inventory.get("task_prompts", []) if isinstance(item, dict)]
        task_list = tk.Listbox(task_prompt_tab, width=38, exportselection=False)
        task_list.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 8))
        task_path_var = tk.StringVar(value="")
        ttk.Label(task_prompt_tab, textvariable=task_path_var).grid(row=0, column=1, sticky="ew", pady=(0, 6))
        task_editor, task_text = make_editor(task_prompt_tab)
        task_editor.grid(row=1, column=1, sticky="nsew")
        selected_task = {"index": -1}

        for item in task_prompts:
            task_list.insert(tk.END, f"{item.get('name')}  |  {mtime_label(float(item.get('updated_at') or 0))}")

        def load_task_prompt(index: int) -> None:
            if not 0 <= index < len(task_prompts):
                return
            selected_task["index"] = index
            item = task_prompts[index]
            task_path_var.set(str(item.get("path") or ""))
            try:
                data = self.orchestrator.read_prompt_file(stage, task_path_var.get())
            except Exception as exc:
                messagebox.showerror("读取任务提示词失败", str(exc), parent=dialog)
                return
            task_text.delete("1.0", tk.END)
            task_text.insert("1.0", data.get("text") or "")

        def on_task_select(_event: tk.Event) -> None:
            selection = task_list.curselection()
            if selection:
                load_task_prompt(int(selection[0]))

        def save_task_prompt() -> None:
            if selected_task["index"] < 0:
                messagebox.showinfo("未选择", "请先选择一个任务提示词。", parent=dialog)
                return
            try:
                result = self.orchestrator.save_prompt_file(stage, task_path_var.get(), task_text.get("1.0", "end-1c"))
            except Exception as exc:
                messagebox.showerror("保存任务提示词失败", str(exc), parent=dialog)
                return
            self.refresh()
            self._show_operation_result(self.log_text, "任务提示词已保存", result)
            messagebox.showinfo("已保存", "任务提示词已保存，并已自动备份原文件。", parent=dialog)

        task_list.bind("<<ListboxSelect>>", on_task_select)
        task_buttons = ttk.Frame(task_prompt_tab)
        task_buttons.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(task_buttons, text="保存", command=save_task_prompt).pack(side=tk.LEFT, padx=3)
        ttk.Button(task_buttons, text="重新读取", command=lambda: load_task_prompt(selected_task["index"])).pack(side=tk.LEFT, padx=3)
        ttk.Button(task_buttons, text="打开文件", command=lambda: self.open_path(Path(task_path_var.get())) if task_path_var.get() else None).pack(
            side=tk.LEFT, padx=3
        )
        if task_prompts:
            task_list.selection_set(0)
            load_task_prompt(0)
        else:
            task_path_var.set("此阶段没有可编辑的任务提示词文件。")

        if workflow_tab is not None and stage == "__legacy_journal_author_editor__":
            workflow_tab.columnconfigure(0, weight=1)
            workflow_tab.rowconfigure(2, weight=1)
            workflow_data = dict(inventory.get("journal_author_workflow") or {})
            steps = [item for item in workflow_data.get("steps", []) if isinstance(item, dict)]
            step = steps[0] if steps else {}
            ttk.Label(
                workflow_tab,
                text=(
                    "当前阶段沿用已经调好的 02-03 合并提示词，因此只有一个受保护的 AI 任务。"
                    "每篇论文入队时会冻结模型、推理强度和 Token 上限；运行中修改只影响之后入队的论文。"
                ),
                wraplength=1100,
            ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
            summary_var = tk.StringVar()
            ttk.Label(workflow_tab, textvariable=summary_var).grid(row=1, column=0, sticky="ew", pady=(0, 8))

            body = ttk.Frame(workflow_tab)
            body.grid(row=2, column=0, sticky="nsew")
            body.columnconfigure(1, weight=1)
            tree = ttk.Treeview(
                body,
                columns=("position", "name", "model", "effort", "tokens"),
                show="headings",
                height=5,
                selectmode="browse",
            )
            for column, title, width in (
                ("position", "序号", 55),
                ("name", "任务", 280),
                ("model", "模型", 210),
                ("effort", "推理", 100),
                ("tokens", "Token 上限", 120),
            ):
                tree.heading(column, text=title)
                tree.column(column, width=width, anchor=tk.W, stretch=False)
            tree.grid(row=0, column=0, sticky="nsw", padx=(0, 10))

            editor = ttk.LabelFrame(body, text="模型与执行配置（不会修改提示词正文）", padding=10)
            editor.grid(row=0, column=1, sticky="nsew")
            editor.columnconfigure(1, weight=1)
            effort_labels = [_effort_display(value) for value in REASONING_EFFORTS]
            effort_by_label = {_effort_display(value): value for value in REASONING_EFFORTS}
            default_profile = workflow_data.get("default_execution_profile")
            default_profile = default_profile if isinstance(default_profile, dict) else {}
            configured_profile = step.get("execution_profile")
            configured_profile = configured_profile if isinstance(configured_profile, dict) else {}
            default_model_var = tk.StringVar(value=str(default_profile.get("model") or ""))
            default_effort_var = tk.StringVar(value=_effort_display(default_profile.get("reasoning_effort")))
            default_token_var = tk.StringVar(value=str(default_profile.get("hard_token_limit") or 0))
            step_model_var = tk.StringVar(value=str(configured_profile.get("model") or ""))
            step_effort_var = tk.StringVar(value=_effort_display(configured_profile.get("reasoning_effort")))
            step_token_value = configured_profile.get("hard_token_limit")
            step_token_var = tk.StringVar(value="" if step_token_value is None else str(step_token_value))
            hint_var = tk.StringVar(value="模型不可用时会阻塞并明确提示，不会偷偷切换模型。")

            ttk.Label(editor, text="阶段默认模型").grid(row=0, column=0, sticky="w", pady=4)
            default_model_combo = ttk.Combobox(editor, textvariable=default_model_var, state="normal", width=34)
            default_model_combo.grid(row=0, column=1, sticky="ew", pady=4)
            ttk.Label(editor, text="阶段默认推理").grid(row=1, column=0, sticky="w", pady=4)
            ttk.Combobox(
                editor,
                textvariable=default_effort_var,
                values=effort_labels,
                state="readonly",
                width=15,
            ).grid(row=1, column=1, sticky="w", pady=4)
            ttk.Label(editor, text="阶段默认 Token").grid(row=2, column=0, sticky="w", pady=4)
            ttk.Entry(editor, textvariable=default_token_var, width=15).grid(row=2, column=1, sticky="w", pady=4)
            ttk.Separator(editor, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky="ew", pady=8)
            ttk.Label(editor, text="合并任务模型").grid(row=4, column=0, sticky="w", pady=4)
            step_model_combo = ttk.Combobox(editor, textvariable=step_model_var, state="normal", width=34)
            step_model_combo.grid(row=4, column=1, sticky="ew", pady=4)
            ttk.Label(editor, text="合并任务推理").grid(row=5, column=0, sticky="w", pady=4)
            ttk.Combobox(
                editor,
                textvariable=step_effort_var,
                values=effort_labels,
                state="readonly",
                width=15,
            ).grid(row=5, column=1, sticky="w", pady=4)
            ttk.Label(editor, text="合并任务 Token").grid(row=6, column=0, sticky="w", pady=4)
            ttk.Entry(editor, textvariable=step_token_var, width=15).grid(row=6, column=1, sticky="w", pady=4)
            ttk.Label(editor, textvariable=hint_var, foreground="#666666", wraplength=560).grid(
                row=7,
                column=0,
                columnspan=2,
                sticky="ew",
                pady=(8, 4),
            )
            prompt_path = str(step.get("prompt_path") or "")
            ttk.Label(
                editor,
                text=f"受保护提示词：{prompt_path}",
                foreground="#666666",
                wraplength=700,
            ).grid(row=8, column=0, columnspan=2, sticky="ew", pady=4)

            def parse_token(value: str, *, inherit: bool) -> int | None:
                text = str(value or "").replace(",", "").strip()
                if not text:
                    return None if inherit else 0
                parsed = int(text)
                if parsed < 0:
                    raise ValueError("Token 上限不能为负数")
                return parsed

            def refresh_journal_workflow(result: dict[str, Any]) -> None:
                nonlocal workflow_data, steps, step, default_profile, configured_profile
                workflow_data = dict(result)
                steps = [item for item in workflow_data.get("steps", []) if isinstance(item, dict)]
                step = steps[0] if steps else {}
                default_profile = workflow_data.get("default_execution_profile")
                default_profile = default_profile if isinstance(default_profile, dict) else {}
                configured_profile = step.get("execution_profile")
                configured_profile = configured_profile if isinstance(configured_profile, dict) else {}
                default_model_var.set(str(default_profile.get("model") or ""))
                default_effort_var.set(_effort_display(default_profile.get("reasoning_effort")))
                default_token_var.set(str(default_profile.get("hard_token_limit") or 0))
                step_model_var.set(str(configured_profile.get("model") or ""))
                step_effort_var.set(_effort_display(configured_profile.get("reasoning_effort")))
                token = configured_profile.get("hard_token_limit")
                step_token_var.set("" if token is None else str(token))
                tree.delete(*tree.get_children())
                effective = step.get("effective_execution_profile")
                effective = effective if isinstance(effective, dict) else {}
                tree.insert(
                    "",
                    tk.END,
                    iid=str(step.get("step_id") or "builtin-02-03"),
                    values=(
                        1,
                        step.get("name") or "选刊并生成投稿包（原 Step 2-3）",
                        effective.get("model") or JOURNAL_AUTHOR_CODEX_MODEL,
                        _effort_display(effective.get("reasoning_effort")),
                        token_limit_label(effective.get("hard_token_limit")),
                    ),
                )
                summary_var.set(
                    f"当前版本：{workflow_data.get('active_version_id') or '-'}｜1 个合并任务｜"
                    f"已保存版本：{workflow_data.get('version_count') or 1}"
                )

            def save_default() -> None:
                try:
                    result = self.orchestrator.update_journal_author_default_execution_profile(
                        {
                            "model": default_model_var.get().strip(),
                            "reasoning_effort": effort_by_label.get(
                                default_effort_var.get(),
                                default_effort_var.get().strip().casefold(),
                            ),
                            "hard_token_limit": parse_token(default_token_var.get(), inherit=False),
                            "unavailable_policy": "block",
                            "preset": "custom",
                        }
                    )
                except Exception as exc:
                    messagebox.showerror("保存阶段默认失败", str(exc), parent=dialog)
                    return
                refresh_journal_workflow(result)
                hint_var.set("阶段默认已保存；只影响之后进入选刊作者阶段的新请求。")

            def save_step() -> None:
                try:
                    result = self.orchestrator.update_journal_author_step_execution_profile(
                        str(step.get("step_id") or "builtin-02-03"),
                        {
                            "model": step_model_var.get().strip(),
                            "reasoning_effort": effort_by_label.get(
                                step_effort_var.get(),
                                step_effort_var.get().strip().casefold(),
                            ),
                            "hard_token_limit": parse_token(step_token_var.get(), inherit=True),
                            "unavailable_policy": "block",
                            "preset": "custom",
                        },
                    )
                except Exception as exc:
                    messagebox.showerror("保存合并任务配置失败", str(exc), parent=dialog)
                    return
                refresh_journal_workflow(result)
                hint_var.set("合并任务配置已保存；运行中论文仍使用各自冻结快照。")

            def load_models() -> None:
                hint_var.set("正在读取当前账号可用模型…")
                dialog.update_idletasks()
                try:
                    result = self.orchestrator.stage_available_models("journal_author")
                    values = []
                    for item in result.get("models", []):
                        if not isinstance(item, dict):
                            continue
                        model_id = str(item.get("model") or item.get("id") or "").strip()
                        if model_id and model_id not in values:
                            values.append(model_id)
                    for current in (default_model_var.get().strip(), step_model_var.get().strip()):
                        if current and current not in values:
                            values.append(current)
                    default_model_combo.configure(values=values)
                    step_model_combo.configure(values=values)
                    hint_var.set(f"已读取 {len(values)} 个模型；执行前还会再次验证。")
                except Exception as exc:
                    hint_var.set("模型列表读取失败；当前配置没有改变。")
                    messagebox.showerror("读取模型失败", str(exc), parent=dialog)

            buttons = ttk.Frame(workflow_tab)
            buttons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
            ttk.Button(buttons, text="保存阶段默认", command=save_default).pack(side=tk.LEFT, padx=3)
            ttk.Button(buttons, text="保存合并任务配置", command=save_step).pack(side=tk.LEFT, padx=3)
            ttk.Button(buttons, text="读取账号可用模型", command=load_models).pack(side=tk.LEFT, padx=3)
            ttk.Button(
                buttons,
                text="打开受保护提示词",
                command=lambda: self.open_path(Path(prompt_path)) if prompt_path else None,
            ).pack(side=tk.LEFT, padx=3)
            refresh_journal_workflow(workflow_data)

        if workflow_tab is not None:
            workflow_tab.columnconfigure(0, weight=1)
            workflow_tab.columnconfigure(1, weight=2)
            workflow_tab.rowconfigure(2, weight=1)
            is_writing_workflow = stage == "writing"
            is_figure_workflow = stage == "figures"
            is_journal_workflow = stage == "journal_author"
            if is_writing_workflow:
                workflow_data = dict(inventory.get("writing_workflow") or {})
                workflow_stage_label = "写作"
                add_workflow = self.orchestrator.add_writing_workflow_step
                remove_workflow = self.orchestrator.remove_writing_workflow_step
                move_workflow = self.orchestrator.move_writing_workflow_step
                read_workflow_prompt = self.orchestrator.read_writing_workflow_prompt
                save_workflow_prompt_value = self.orchestrator.save_writing_workflow_prompt
                restore_workflow = self.orchestrator.restore_default_writing_workflow
                update_default_execution_profile = self.orchestrator.update_writing_default_execution_profile
                update_step_execution_profile = self.orchestrator.update_writing_step_execution_profile
            elif is_figure_workflow:
                workflow_data = dict(inventory.get("figure_workflow") or {})
                workflow_stage_label = "画图"
                add_workflow = self.orchestrator.add_figure_workflow_step
                remove_workflow = self.orchestrator.remove_figure_workflow_step
                move_workflow = self.orchestrator.move_figure_workflow_step
                read_workflow_prompt = self.orchestrator.read_figure_workflow_prompt
                save_workflow_prompt_value = self.orchestrator.save_figure_workflow_prompt
                restore_workflow = self.orchestrator.restore_default_figure_workflow
                update_default_execution_profile = self.orchestrator.update_figure_default_execution_profile
                update_step_execution_profile = self.orchestrator.update_figure_step_execution_profile
            else:
                workflow_data = dict(inventory.get("journal_author_workflow") or {})
                workflow_stage_label = "选刊作者"
                add_workflow = self.orchestrator.add_journal_author_workflow_step
                remove_workflow = self.orchestrator.remove_journal_author_workflow_step
                move_workflow = self.orchestrator.move_journal_author_workflow_step
                read_workflow_prompt = self.orchestrator.read_journal_author_workflow_prompt
                save_workflow_prompt_value = self.orchestrator.save_journal_author_workflow_prompt
                restore_workflow = self.orchestrator.restore_default_journal_author_workflow
                update_default_execution_profile = self.orchestrator.update_journal_author_default_execution_profile
                update_step_execution_profile = self.orchestrator.update_journal_author_step_execution_profile
            workflow_summary_var = tk.StringVar()
            if is_writing_workflow:
                workflow_help = (
                    "当前流程会在新建论文时复制为固定快照；这里的修改默认只影响之后新建的论文。"
                    "内置步骤存在连续产物依赖；删除一个内置步骤会同时截去它及后续步骤。"
                )
            elif is_figure_workflow:
                workflow_help = (
                    "每次论文进入画图队列时都会冻结当前流程快照；运行中修改配置不会改变已有任务。"
                    "内置 Step 2→3 是最终交付链并受保护，Step 1 可选；自定义步骤追加在末尾。"
                )
            else:
                workflow_help = (
                    "每篇论文入队时会冻结当前选刊作者流程和逐步模型配置。默认合并模式只调用一次 AI；"
                    "精细模式按‘选刊→程序门禁→投稿包’连续执行；内置提示词受保护，自定义步骤可追加在末尾。"
                )
            ttk.Label(
                workflow_tab,
                text=workflow_help,
                wraplength=1000,
            ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            ttk.Label(workflow_tab, textvariable=workflow_summary_var).grid(
                row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6)
            )

            workflow_tree = ttk.Treeview(
                workflow_tab,
                columns=("position", "name", "kind", "contract", "model", "effort", "tokens"),
                show="headings",
                height=16,
                selectmode="browse",
            )
            for column, title, width in (
                ("position", "序号", 55),
                ("name", "步骤名称", 220),
                ("kind", "类型", 80),
                ("contract", "契约", 70),
                ("model", "模型", 185),
                ("effort", "推理", 90),
                ("tokens", "Token 上限", 105),
            ):
                workflow_tree.heading(column, text=title)
                workflow_tree.column(column, width=width, anchor=tk.W, stretch=False)
            workflow_tree.grid(row=2, column=0, sticky="nsew", padx=(0, 8))

            workflow_right = ttk.Frame(workflow_tab)
            workflow_right.columnconfigure(0, weight=1)
            workflow_right.rowconfigure(1, weight=1)
            workflow_right.grid(row=2, column=1, sticky="nsew")

            execution_frame = ttk.LabelFrame(
                workflow_right,
                text="模型与单步执行配置（不修改提示词正文）",
                padding=(7, 5),
            )
            execution_frame.grid(row=0, column=0, sticky="ew", pady=(0, 7))
            # Keep the editable per-Step model name readable even when the
            # neighbouring effort, token and save controls occupy the row.
            execution_frame.columnconfigure(2, weight=1, minsize=230)
            effort_labels = [_effort_display(value) for value in REASONING_EFFORTS]
            effort_by_label = {
                _effort_display(value): value for value in REASONING_EFFORTS
            }
            preset_labels = [
                str(EXECUTION_PRESETS[key]["label"])
                for key in ("inherit", "quality", "balanced", "economy", "custom")
            ]
            default_profile = (
                workflow_data.get("default_execution_profile")
                if isinstance(workflow_data.get("default_execution_profile"), dict)
                else {}
            )
            default_model_var = tk.StringVar(value=str(default_profile.get("model") or ""))
            default_effort_var = tk.StringVar(
                value=_effort_display(default_profile.get("reasoning_effort"))
            )
            default_token_var = tk.StringVar(
                value=str(default_profile.get("hard_token_limit") or 0)
            )
            step_preset_var = tk.StringVar(value=str(EXECUTION_PRESETS["inherit"]["label"]))
            step_model_var = tk.StringVar(value="")
            step_effort_var = tk.StringVar(value=_effort_display(""))
            step_token_var = tk.StringVar(value="")

            ttk.Label(execution_frame, text="阶段默认").grid(row=0, column=0, sticky="w", padx=(0, 5))
            ttk.Label(execution_frame, text="模型").grid(row=0, column=1, sticky="e")
            default_model_combo = ttk.Combobox(
                execution_frame,
                textvariable=default_model_var,
                state="normal",
                width=32,
            )
            default_model_combo.grid(row=0, column=2, columnspan=5, sticky="ew", padx=(4, 7))
            ttk.Label(execution_frame, text="推理").grid(row=1, column=1, sticky="e", pady=(4, 0))
            ttk.Combobox(
                execution_frame,
                textvariable=default_effort_var,
                values=effort_labels,
                state="readonly",
                width=12,
            ).grid(row=1, column=2, sticky="w", padx=(4, 7), pady=(4, 0))
            ttk.Label(execution_frame, text="Token").grid(row=1, column=3, sticky="e", pady=(4, 0))
            ttk.Entry(execution_frame, textvariable=default_token_var, width=12).grid(
                row=1, column=4, sticky="w", padx=(4, 7), pady=(4, 0)
            )
            ttk.Button(
                execution_frame,
                text="保存阶段默认",
                command=lambda: save_default_profile(),
            ).grid(row=0, column=7, sticky="w")

            ttk.Separator(execution_frame, orient=tk.HORIZONTAL).grid(
                row=2, column=0, columnspan=8, sticky="ew", pady=5
            )
            ttk.Label(execution_frame, text="所选 Step").grid(row=3, column=0, sticky="w", padx=(0, 5))
            ttk.Label(execution_frame, text="档案").grid(row=3, column=1, sticky="e")
            step_preset_combo = ttk.Combobox(
                execution_frame,
                textvariable=step_preset_var,
                values=preset_labels,
                state="readonly",
                width=18,
            )
            step_preset_combo.grid(row=3, column=2, sticky="w", padx=(4, 7))
            ttk.Label(execution_frame, text="模型").grid(row=4, column=1, sticky="e", pady=(4, 0))
            step_model_combo = ttk.Combobox(
                execution_frame,
                textvariable=step_model_var,
                state="normal",
                width=28,
            )
            step_model_combo.grid(row=4, column=2, sticky="ew", padx=(4, 7), pady=(4, 0))
            ttk.Label(execution_frame, text="推理").grid(row=4, column=3, sticky="e", pady=(4, 0))
            ttk.Combobox(
                execution_frame,
                textvariable=step_effort_var,
                values=effort_labels,
                state="readonly",
                width=12,
            ).grid(row=4, column=4, sticky="w", padx=(4, 7), pady=(4, 0))
            ttk.Label(execution_frame, text="Token").grid(row=4, column=5, sticky="e", pady=(4, 0))
            ttk.Entry(execution_frame, textvariable=step_token_var, width=12).grid(
                row=4, column=6, sticky="w", padx=(4, 7), pady=(4, 0)
            )
            ttk.Button(
                execution_frame,
                text="保存所选 Step",
                command=lambda: save_selected_step_profile(),
            ).grid(row=3, column=7, sticky="w")
            profile_hint_var = tk.StringVar(
                value="Token 留空表示继承阶段默认，0 表示不限制；模型不可用时会阻塞提示，不会偷偷换模型。"
            )
            ttk.Label(execution_frame, textvariable=profile_hint_var, foreground="#666666").grid(
                row=5, column=0, columnspan=7, sticky="w", pady=(5, 0)
            )
            ttk.Button(
                execution_frame,
                text="读取账号可用模型",
                command=lambda: load_available_models(),
            ).grid(row=5, column=7, sticky="e", pady=(5, 0))

            prompt_frame = ttk.LabelFrame(workflow_right, text="步骤提示词", padding=(5, 4))
            prompt_frame.columnconfigure(0, weight=1)
            prompt_frame.rowconfigure(0, weight=1)
            prompt_frame.grid(row=1, column=0, sticky="nsew")
            workflow_editor_frame, workflow_text = make_editor(prompt_frame)
            workflow_editor_frame.grid(row=0, column=0, sticky="nsew")
            workflow_status_var = tk.StringVar(value="选择自定义步骤后可在右侧编辑；内置提示词保持原样。")
            ttk.Label(workflow_tab, textvariable=workflow_status_var).grid(
                row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )

            def workflow_steps() -> list[dict[str, Any]]:
                return [item for item in workflow_data.get("steps", []) if isinstance(item, dict)]

            def selected_workflow_step() -> dict[str, Any] | None:
                selection = workflow_tree.selection()
                if not selection:
                    return None
                step_id = str(selection[0])
                return next((item for item in workflow_steps() if str(item.get("step_id")) == step_id), None)

            def parse_profile_token(value: str, *, allow_inherit: bool) -> int | None:
                text = str(value or "").replace(",", "").strip()
                if not text:
                    return None if allow_inherit else 0
                return int(text)

            def refresh_default_profile_vars() -> None:
                profile = workflow_data.get("default_execution_profile")
                profile = profile if isinstance(profile, dict) else {}
                default_model_var.set(str(profile.get("model") or ""))
                default_effort_var.set(_effort_display(profile.get("reasoning_effort")))
                default_token_var.set(str(profile.get("hard_token_limit") or 0))

            def load_step_profile_vars(item: dict[str, Any] | None) -> None:
                profile = item.get("execution_profile") if isinstance(item, dict) else {}
                profile = profile if isinstance(profile, dict) else {}
                preset = str(profile.get("preset") or "inherit")
                step_preset_var.set(
                    str(EXECUTION_PRESETS.get(preset, EXECUTION_PRESETS["custom"])["label"])
                )
                step_model_var.set(str(profile.get("model") or ""))
                step_effort_var.set(_effort_display(profile.get("reasoning_effort")))
                token = profile.get("hard_token_limit")
                step_token_var.set("" if token is None else str(token))

            def apply_selected_preset(_event: tk.Event | None = None) -> None:
                key = EXECUTION_PRESET_LABELS.get(step_preset_var.get(), "custom")
                preset = EXECUTION_PRESETS.get(key, EXECUTION_PRESETS["custom"])
                if key == "custom":
                    return
                step_model_var.set(str(preset.get("model") or ""))
                step_effort_var.set(_effort_display(preset.get("reasoning_effort")))
                if key == "inherit":
                    step_token_var.set("")

            def load_available_models() -> None:
                profile_hint_var.set("正在读取当前账号可用模型…")
                dialog.update_idletasks()
                try:
                    result = self.orchestrator.stage_available_models(stage)
                    models = [
                        item for item in result.get("models", []) if isinstance(item, dict)
                    ]
                    values = []
                    for item in models:
                        model_id = str(item.get("model") or item.get("id") or "").strip()
                        if model_id and model_id not in values:
                            values.append(model_id)
                    for current in (default_model_var.get().strip(), step_model_var.get().strip()):
                        if current and current not in values:
                            values.append(current)
                    default_model_combo.configure(values=values)
                    step_model_combo.configure(values=values)
                    profile_hint_var.set(
                        f"已读取 {len(models)} 个可用模型；保存时记录模型字符串，执行前还会再次验证。"
                    )
                except Exception as exc:
                    profile_hint_var.set("模型列表读取失败；未修改当前配置。")
                    messagebox.showerror("读取模型失败", str(exc), parent=dialog)

            def save_default_profile() -> None:
                try:
                    result = update_default_execution_profile(
                        {
                            "model": default_model_var.get().strip(),
                            "reasoning_effort": effort_by_label.get(
                                default_effort_var.get(),
                                default_effort_var.get().strip().casefold(),
                            ),
                            "hard_token_limit": parse_profile_token(
                                default_token_var.get(),
                                allow_inherit=False,
                            ),
                            "unavailable_policy": "block",
                            "preset": "custom",
                        }
                    )
                except Exception as exc:
                    messagebox.showerror("保存阶段默认失败", str(exc), parent=dialog)
                    return
                selected = str(workflow_tree.selection()[0]) if workflow_tree.selection() else ""
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow(selected)
                load_workflow_prompt()
                profile_hint_var.set("阶段默认已保存为新流程版本；只影响之后进入该阶段的新任务。")

            def save_selected_step_profile() -> None:
                item = selected_workflow_step()
                if item is None:
                    messagebox.showinfo("未选择", "请先选择一个 Step。", parent=dialog)
                    return
                try:
                    result = update_step_execution_profile(
                        str(item.get("step_id") or ""),
                        {
                            "model": step_model_var.get().strip(),
                            "reasoning_effort": effort_by_label.get(
                                step_effort_var.get(),
                                step_effort_var.get().strip().casefold(),
                            ),
                            "hard_token_limit": parse_profile_token(
                                step_token_var.get(),
                                allow_inherit=True,
                            ),
                            "unavailable_policy": "block",
                            "preset": EXECUTION_PRESET_LABELS.get(
                                step_preset_var.get(),
                                "custom",
                            ),
                        },
                    )
                except Exception as exc:
                    messagebox.showerror("保存 Step 执行配置失败", str(exc), parent=dialog)
                    return
                step_id = str(item.get("step_id") or "")
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow(step_id)
                load_workflow_prompt()
                profile_hint_var.set(
                    "所选 Step 配置已保存为新流程版本；运行中的论文仍使用原冻结快照。"
                )

            step_preset_combo.bind("<<ComboboxSelected>>", apply_selected_preset)

            def render_workflow(select_id: str = "") -> None:
                workflow_tree.delete(*workflow_tree.get_children())
                refresh_default_profile_vars()
                for item in workflow_steps():
                    step_id = str(item.get("step_id") or "")
                    effective = item.get("effective_execution_profile")
                    effective = effective if isinstance(effective, dict) else {}
                    configured = item.get("execution_profile")
                    configured = configured if isinstance(configured, dict) else {}
                    model_label = str(effective.get("model") or "-")
                    if not str(configured.get("model") or ""):
                        model_label += "（继承）"
                    effort_label = _effort_display(effective.get("reasoning_effort"))
                    if not str(configured.get("reasoning_effort") or ""):
                        effort_label += "（继承）"
                    effective_limit = effective.get("hard_token_limit")
                    token_label = token_limit_label(effective_limit)
                    if configured.get("hard_token_limit") is None:
                        token_label += "（继承）"
                    workflow_tree.insert(
                        "",
                        tk.END,
                        iid=step_id,
                        values=(
                            item.get("position"),
                            item.get("name"),
                            "内置（受保护）" if item.get("kind") == "builtin" else "自定义",
                            f"Step {item.get('contract_step')}",
                            model_label,
                            effort_label,
                            token_label,
                        ),
                    )
                mode_summary = (
                    f"模式：{'精细' if str(workflow_data.get('mode') or 'combined') == 'split' else '合并'}｜"
                    if is_journal_workflow
                    else ""
                )
                workflow_summary_var.set(
                    f"当前版本：{workflow_data.get('active_version_id') or '-'}｜"
                    f"共 {workflow_data.get('step_count') or len(workflow_steps())} 步｜"
                    f"{mode_summary}已保存版本：{workflow_data.get('version_count') or 1}"
                )
                if is_journal_workflow:
                    journal_mode_var.set(
                        "精细模式（选刊→门禁→投稿包）"
                        if str(workflow_data.get("mode") or "combined") == "split"
                        else "合并模式（1 次 AI，推荐）"
                    )
                if select_id and workflow_tree.exists(select_id):
                    workflow_tree.selection_set(select_id)
                    workflow_tree.focus(select_id)

            def load_workflow_prompt(_event: tk.Event | None = None) -> None:
                item = selected_workflow_step()
                load_step_profile_vars(item)
                workflow_text.configure(state="normal")
                workflow_text.delete("1.0", tk.END)
                if not item:
                    workflow_status_var.set("请选择一个步骤。")
                    workflow_text.configure(state="disabled")
                    return
                if item.get("kind") != "custom":
                    workflow_text.insert("1.0", "内置提示词受保护，当前配置不会改写它。\n\n" + str(item.get("prompt_path") or ""))
                    workflow_text.configure(state="disabled")
                    if is_writing_workflow:
                        builtin_hint = "内置步骤提示词保持原样；可删除该步及其后续来缩短新流程。"
                    elif is_figure_workflow:
                        builtin_hint = "内置提示词保持原样；画图 Step 1 可删除，Step 2 和 Step 3 受交付依赖保护。"
                    else:
                        builtin_hint = "内置提示词保持原样且不可删除；用合并/精细模式切换内置交付链。"
                    workflow_status_var.set(builtin_hint)
                    return
                try:
                    data = read_workflow_prompt(str(item.get("step_id") or ""))
                except Exception as exc:
                    workflow_status_var.set(str(exc))
                    return
                workflow_text.insert("1.0", str(data.get("text") or ""))
                workflow_status_var.set(f"自定义提示词：{data.get('path') or '-'}")

            def add_workflow_step() -> None:
                name = simpledialog.askstring(f"新增{workflow_stage_label}步骤", "步骤名称：", parent=dialog)
                if not name:
                    return
                try:
                    result = add_workflow(name)
                except Exception as exc:
                    messagebox.showerror("新增步骤失败", str(exc), parent=dialog)
                    return
                workflow_data.clear()
                workflow_data.update(result)
                new_steps = workflow_steps()
                new_id = str(new_steps[-1].get("step_id") or "") if new_steps else ""
                render_workflow(new_id)
                load_workflow_prompt()
                workflow_status_var.set("已创建新流程版本；请补充右侧自定义提示词并保存。")

            def remove_workflow_step() -> None:
                item = selected_workflow_step()
                if not item:
                    messagebox.showinfo("未选择", "请先选择要删除的步骤。", parent=dialog)
                    return
                extra = ""
                if item.get("kind") == "builtin":
                    if is_writing_workflow:
                        extra = "\n\n该步骤是内置步骤。为保持产物依赖，它和后续所有步骤都会从新版本中移除。"
                    elif is_figure_workflow:
                        extra = "\n\n画图内置 Step 2 和 Step 3 不能删除；只有可选的 Step 1 能移除。"
                    else:
                        extra = "\n\n选刊作者内置交付链不能删除；请改用合并/精细模式切换。"
                if not messagebox.askyesno(
                    f"删除{workflow_stage_label}步骤",
                    f"确认从新流程版本中删除“{item.get('name')}”？{extra}\n\n正在运行的论文不会改变。",
                    parent=dialog,
                ):
                    return
                try:
                    result = remove_workflow(str(item.get("step_id") or ""))
                except Exception as exc:
                    messagebox.showerror("删除步骤失败", str(exc), parent=dialog)
                    return
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow()
                load_workflow_prompt()

            def move_workflow_step(direction: int) -> None:
                item = selected_workflow_step()
                if not item:
                    return
                try:
                    result = move_workflow(
                        str(item.get("step_id") or ""), direction
                    )
                except Exception as exc:
                    messagebox.showerror("调整步骤失败", str(exc), parent=dialog)
                    return
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow(str(item.get("step_id") or ""))

            def restore_default_workflow() -> None:
                restore_label = "默认10步" if is_writing_workflow else "默认3步" if is_figure_workflow else "默认合并模式"
                if not messagebox.askyesno(
                    f"恢复{restore_label}",
                    f"确认新建一个“{restore_label}”流程版本吗？\n\n"
                    + "现有论文仍使用各自快照；自定义提示词文件和历史版本不会删除。",
                    parent=dialog,
                ):
                    return
                try:
                    result = restore_workflow()
                except Exception as exc:
                    messagebox.showerror("恢复默认流程失败", str(exc), parent=dialog)
                    return
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow()
                load_workflow_prompt()

            def save_workflow_prompt() -> None:
                item = selected_workflow_step()
                if not item or item.get("kind") != "custom":
                    messagebox.showinfo("不可保存", "请选择一个自定义步骤。", parent=dialog)
                    return
                try:
                    result = save_workflow_prompt_value(
                        str(item.get("step_id") or ""), workflow_text.get("1.0", "end-1c")
                    )
                except Exception as exc:
                    messagebox.showerror("保存自定义提示词失败", str(exc), parent=dialog)
                    return
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow(str(item.get("step_id") or ""))
                workflow_status_var.set(f"已保存：{result.get('path') or '-'}")

            journal_mode_labels = {"合并模式（1 次 AI，推荐）": "combined", "精细模式（选刊→门禁→投稿包）": "split"}
            journal_mode_var = tk.StringVar(
                value="精细模式（选刊→门禁→投稿包）"
                if str(workflow_data.get("mode") or "combined") == "split"
                else "合并模式（1 次 AI，推荐）"
            )

            def set_journal_workflow_mode() -> None:
                mode = journal_mode_labels.get(journal_mode_var.get(), "combined")
                try:
                    result = self.orchestrator.set_journal_author_workflow_mode(mode)
                except Exception as exc:
                    messagebox.showerror("切换流程模式失败", str(exc), parent=dialog)
                    return
                workflow_data.clear()
                workflow_data.update(result)
                render_workflow()
                load_workflow_prompt()
                workflow_status_var.set(
                    "已切换为合并模式：每篇论文默认只发送 1 个 AI Turn。"
                    if mode == "combined"
                    else "已切换为精细模式：选刊通过程序门禁后，再在同一固定会话生成投稿包。"
                )

            workflow_tree.bind("<<TreeviewSelect>>", load_workflow_prompt)
            workflow_buttons = ttk.Frame(workflow_tab)
            workflow_buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            if is_journal_workflow:
                ttk.Label(workflow_buttons, text="内置流程").pack(side=tk.LEFT, padx=(3, 2))
                ttk.Combobox(
                    workflow_buttons,
                    textvariable=journal_mode_var,
                    values=tuple(journal_mode_labels),
                    state="readonly",
                    width=30,
                ).pack(side=tk.LEFT, padx=3)
                ttk.Button(workflow_buttons, text="应用模式", command=set_journal_workflow_mode).pack(side=tk.LEFT, padx=3)
                ttk.Separator(workflow_buttons, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)
            ttk.Button(workflow_buttons, text="新增末尾步骤", command=add_workflow_step).pack(side=tk.LEFT, padx=3)
            ttk.Button(workflow_buttons, text="删除所选步骤", command=remove_workflow_step).pack(side=tk.LEFT, padx=3)
            ttk.Button(workflow_buttons, text="自定义步骤上移", command=lambda: move_workflow_step(-1)).pack(
                side=tk.LEFT, padx=3
            )
            ttk.Button(workflow_buttons, text="自定义步骤下移", command=lambda: move_workflow_step(1)).pack(
                side=tk.LEFT, padx=3
            )
            ttk.Button(workflow_buttons, text="保存自定义提示词", command=save_workflow_prompt).pack(
                side=tk.LEFT, padx=3
            )
            ttk.Button(
                workflow_buttons,
                text="恢复默认10步" if is_writing_workflow else "恢复默认3步" if is_figure_workflow else "恢复默认合并模式",
                command=restore_default_workflow,
            ).pack(
                side=tk.LEFT, padx=3
            )
            render_workflow()

    def open_account_site_manager(self) -> None:
        existing = getattr(self, "_account_site_manager_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                pass
        try:
            from ..account_site_store import AccountSiteStore
            from .account_site_manager import open_account_site_manager
        except Exception as exc:
            LOGGER.error("Failed to open account and site manager", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror("账号与站点", f"无法打开管理窗口：{exc}", parent=self)
            return

        def load_store() -> AccountSiteStore:
            return AccountSiteStore(
                SMART_CREDENTIAL_PATH,
                SMART_SITE_REGISTRY_PATH,
                vault_path=ACCOUNT_VAULT_PATH,
                legacy_vault_path=LEGACY_CREDENTIAL_VAULT_PATH,
            )

        def done(store: AccountSiteStore) -> None:
            open_account_site_manager(
                parent=self,
                credential_path=SMART_CREDENTIAL_PATH,
                site_registry_path=SMART_SITE_REGISTRY_PATH,
                vault_path=ACCOUNT_VAULT_PATH,
                legacy_vault_path=LEGACY_CREDENTIAL_VAULT_PATH,
                on_change=lambda: self._request_ui_refresh(
                    tab="smart_submission",
                    reason="账号与站点已更新",
                ),
                store=store,
            )
            self.status_var.set("账号与站点已打开")

        self._run_dialog_background_task("账号与站点", load_store, done)

    def open_ai_execution_settings(self) -> None:
        try:
            from .ai_execution_settings import open_ai_execution_settings

            coordinator = self._get_ai_execution_coordinator()
            open_ai_execution_settings(
                parent=self,
                store=coordinator,
                test_callback=lambda store, profile_id: store.test_profile(profile_id),
            )
        except Exception as exc:
            LOGGER.error("Failed to open AI execution settings", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror("AI执行与API", f"无法打开设置窗口：{exc}", parent=self)

    def _get_ai_execution_coordinator(self):
        from ..ai_execution import AIExecutionCoordinator

        coordinator = getattr(self, "_ai_execution_coordinator", None)
        if coordinator is None:
            def legacy_statuses() -> dict[str, dict[str, Any]]:
                statuses = dict(self.orchestrator._scheduler_status_read_only())
                statuses["smart_submission"] = parse_toml_like(SMART_AUTOMATION_PATH)
                return statuses

            coordinator = AIExecutionCoordinator(
                legacy_status_reader=legacy_statuses,
            )
            self._ai_execution_coordinator = coordinator
        return coordinator

    def _continue_writing_after_evidence(self, paper_id: str) -> bool:
        try:
            state = self.orchestrator.resume_writing_papers([paper_id], single_active=True)
        except Exception as exc:
            LOGGER.error("Writing evidence continuation failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror(
                "实验数据已就绪，但继续失败",
                f"实验数据门禁已通过，但写作任务未能进入 Step 6：{type(exc).__name__}",
                parent=self,
            )
            return False
        self.state_data = state
        self.refresh()
        error = _stage_result_error(state, "writing")
        if error:
            messagebox.showerror("进入 Step 6 失败", error, parent=self)
            return False
        self.status_var.set(
            f"{paper_id} 的实验数据门禁已记录，Step 6 启动请求已提交；请以实时任务状态为准。"
        )
        messagebox.showinfo(
            "Step 6 请求已提交",
            "实验数据门禁已通过，当前论文的 Step 6 启动请求已提交。\n\n"
            "实时任务表会继续显示“运行中、已完成或具体卡点”，不会把“已提交”当成“已执行成功”。",
            parent=self,
        )
        return True

    def import_selected_writing_evidence(self) -> None:
        selected = _selected_writing_evidence_ids(self)
        if len(selected) != 1:
            messagebox.showinfo("导入实验数据并继续", "请先只选择一篇停在 Step 5 的论文。", parent=self)
            return
        paper_id = selected[0]
        papers = self.state_data.get("papers", {}) if isinstance(self.state_data, dict) else {}
        record = papers.get(paper_id, {}) if isinstance(papers, dict) else {}
        live_record = _writing_live_record_for_paper_id(
            getattr(self, "_writing_journal_papers", {}),
            paper_id,
        )
        if not _writing_evidence_transition_eligible(record, live_record):
            messagebox.showinfo(
                "当前论文不在实验数据关口",
                "此操作只用于完成 Step 5、正在等待实验数据的论文。",
                parent=self,
            )
            return

        papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
        spec_path = filedialog.askopenfilename(
            title="选择实验数据导入规范",
            initialdir=str(papers_dir / paper_id),
            filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
            parent=self,
        )
        if not spec_path:
            return
        try:
            preview = prepare_writing_evidence_import(
                papers_dir,
                paper_id,
                Path(spec_path),
                check_only=True,
            )
        except Exception as exc:
            LOGGER.error("Writing evidence preview failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror("实验数据检查失败", type(exc).__name__, parent=self)
            return
        self._set_technical_details(f"论文 {paper_id} · 实验数据导入预检", preview)
        summary = _writing_evidence_import_summary(preview)
        if not preview.get("ok"):
            messagebox.showerror("实验数据未通过预检", summary, parent=self)
            return
        if not messagebox.askyesno(
            "导入实验数据并继续",
            f"论文：{paper_id}\n\n{summary}\n\n导入成功后立即排队进入 Step 6，是否继续？",
            parent=self,
            default=messagebox.YES,
        ):
            return
        try:
            result = prepare_writing_evidence_import(
                papers_dir,
                paper_id,
                Path(spec_path),
                check_only=False,
                expected_manifest_sha256=str(preview.get("manifest_sha256") or ""),
            )
        except Exception as exc:
            LOGGER.error("Writing evidence import failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror("实验数据导入失败", type(exc).__name__, parent=self)
            return
        self._set_technical_details(f"论文 {paper_id} · 实验数据导入结果", result)
        if not result.get("ok") or not result.get("transition_ready"):
            messagebox.showerror("实验数据未就绪", _writing_evidence_import_summary(result), parent=self)
            return
        self._continue_writing_after_evidence(paper_id)

    def continue_selected_writing_with_existing_evidence(self) -> None:
        selected = _selected_writing_evidence_ids(self)
        if len(selected) != 1:
            messagebox.showinfo("数据已手动改完并继续", "请先只选择一篇停在 Step 5 的论文。", parent=self)
            return
        paper_id = selected[0]
        papers = self.state_data.get("papers", {}) if isinstance(self.state_data, dict) else {}
        record = papers.get(paper_id, {}) if isinstance(papers, dict) else {}
        live_record = _writing_live_record_for_paper_id(
            getattr(self, "_writing_journal_papers", {}),
            paper_id,
        )
        if not _writing_evidence_transition_eligible(record, live_record):
            messagebox.showinfo(
                "当前论文不在实验数据关口",
                "此操作只用于完成 Step 5、正在等待实验数据的论文。",
                parent=self,
            )
            return
        papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
        try:
            result = prepare_manual_writing_evidence(papers_dir, paper_id, check_only=True)
        except Exception as exc:
            LOGGER.error("Existing writing evidence validation failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror("现有实验数据检查失败", type(exc).__name__, parent=self)
            return
        self._set_technical_details(f"论文 {paper_id} · 手动实验数据检查", result)
        summary = _writing_existing_evidence_summary(result)
        if result.get("ready") is not True or result.get("transition_ready") is not True:
            messagebox.showerror(
                "手动修改的数据尚未就绪",
                f"{summary}\n\n请补齐现有论文目录中的实验数据或验证清单后再次点击。",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "数据已手动改完并继续",
            f"论文：{paper_id}\n\n{summary}\n\n现在进入 Step 6？",
            parent=self,
            default=messagebox.YES,
        ):
            return
        preview = result
        try:
            result = prepare_manual_writing_evidence(
                papers_dir,
                paper_id,
                check_only=False,
                expected_draft_sha256=str(preview.get("draft_v2_sha256") or ""),
            )
        except Exception as exc:
            LOGGER.error(
                "Manual writing evidence confirmation failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            messagebox.showerror("手动实验数据确认失败", type(exc).__name__, parent=self)
            return
        self._set_technical_details(f"论文 {paper_id} · 手动实验数据确认", result)
        if not result.get("ok") or not result.get("transition_ready"):
            messagebox.showerror(
                "手动修改的数据尚未就绪",
                _writing_existing_evidence_summary(result),
                parent=self,
            )
            return
        self._continue_writing_after_evidence(paper_id)

    def _legacy_import_selected_writing_evidence(self) -> None:
        selected = _selected_writing_evidence_ids(self)
        if len(selected) != 1:
            messagebox.showinfo(
                "导入真实实验证据",
                "请先在写论文页只选择一篇停在第 5 步、正在等待真实证据的论文。",
                parent=self,
            )
            return
        paper_id = selected[0]
        papers = self.state_data.get("papers", {}) if isinstance(self.state_data, dict) else {}
        record = papers.get(paper_id, {}) if isinstance(papers, dict) else {}
        live_record = _writing_live_record_for_paper_id(
            getattr(self, "_writing_journal_papers", {}),
            paper_id,
        )
        if not _writing_evidence_transition_eligible(record, live_record):
            messagebox.showinfo(
                "当前论文不需要导入证据",
                "此操作只用于停在第 5 步且证据闸门尚未通过的论文。\n\n"
                "它不会启动第 6 步；若论文已通过闸门，请使用正常的显式继续操作。",
                parent=self,
            )
            return

        papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
        paper_dir = papers_dir / paper_id
        spec_path = filedialog.askopenfilename(
            title="选择真实实验证据导入规范",
            initialdir=str(paper_dir),
            filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
            parent=self,
        )
        if not spec_path:
            return

        try:
            preview = prepare_writing_evidence_import(
                papers_dir,
                paper_id,
                Path(spec_path),
                check_only=True,
            )
        except Exception as exc:
            LOGGER.error("Writing evidence check-only failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror(
                "真实实验证据检查失败",
                f"只读检查未完成：{type(exc).__name__}\n\n没有写入文件，也没有启动任何模型任务。",
                parent=self,
            )
            return
        self._set_technical_details(f"论文 {paper_id} · 真实实验证据只读检查", preview)
        preview_summary = _writing_evidence_import_summary(preview)
        if not preview.get("ok"):
            messagebox.showerror(
                "真实实验证据检查未通过",
                f"{preview_summary}\n\n没有写入回执或验证清单，也没有启动任何模型任务。",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "确认导入真实实验证据",
            f"论文：{paper_id}\n\n{preview_summary}\n\n"
            "确认后只会写入来源回执，并把验证清单作为最后一步原子写入。\n"
            "不会修改提示词或调度状态，不会继续第 6 步，也不会产生模型用量。\n\n"
            "是否导入这份已检查的规范？",
            parent=self,
            default=messagebox.NO,
        ):
            return

        try:
            result = prepare_writing_evidence_import(
                papers_dir,
                paper_id,
                Path(spec_path),
                check_only=False,
                expected_manifest_sha256=str(preview.get("manifest_sha256") or ""),
            )
        except Exception as exc:
            LOGGER.error("Writing evidence commit failed", exc_info=(type(exc), exc, exc.__traceback__))
            messagebox.showerror(
                "真实实验证据导入失败",
                f"导入未完成：{type(exc).__name__}\n\n第 6 步没有启动，也没有启动任何模型任务。",
                parent=self,
            )
            return
        self.refresh()
        self._set_technical_details(f"论文 {paper_id} · 真实实验证据导入结果", result)
        summary = _writing_evidence_import_summary(result)
        if not result.get("ok") or not result.get("transition_ready"):
            messagebox.showerror(
                "真实实验证据未导入",
                f"{summary}\n\n第 6 步仍未启动；请修正问题后重新做只读检查。",
                parent=self,
            )
            return
        self.status_var.set(f"{paper_id} 的真实实验证据已通过闸门；第 6 步仍需显式继续。")
        messagebox.showinfo(
            "真实实验证据已导入",
            f"{summary}\n\n现有证据闸门已接受验证清单。\n"
            "本操作没有继续第 6 步，也没有启动模型任务或产生模型用量。",
            parent=self,
        )

    def run_selected_api_review(self) -> None:
        selected = self.selected_ids()
        if len(selected) != 1:
            messagebox.showinfo(
                "API 论文审阅",
                "请先只选择一篇已经具备第 7 步审阅输入的论文。",
                parent=self,
            )
            return
        coordinator = self._get_ai_execution_coordinator()
        try:
            preview = coordinator.prepare_profile_review(selected[0])
        except Exception as exc:
            messagebox.showerror("无法创建 API 审阅", str(exc), parent=self)
            return
        intent = preview.get("intent", {}) if isinstance(preview.get("intent"), dict) else {}
        intent_sha256 = str(preview.get("intent_sha256") or "")
        confirmed = messagebox.askyesno(
            "确认 API 审阅与数据发送",
            "本次会把所选论文的审阅输入发送到当前 API 服务，并产生模型用量。\n\n"
            f"论文：{selected[0]}\n"
            f"服务配置：{intent.get('provider_profile') or '-'}\n"
            f"API 地址：{intent.get('endpoint_base_url') or '-'}\n"
            f"模型：{intent.get('model') or '-'}\n"
            f"Token 预算：{intent.get('token_budget') or '-'}\n"
            f"任务意图指纹：{intent_sha256}\n\n"
            "结果只写入私有暂存区，不覆盖论文正式文件；失败后不会自动换模式或重试。",
            parent=self,
            default=messagebox.NO,
        )
        if not confirmed:
            return
        try:
            task = coordinator.submit_prepared_profile_review(preview)
        except Exception as exc:
            messagebox.showerror("无法创建 API 审阅", str(exc), parent=self)
            return
        approval = task.get("approval", {}) if isinstance(task.get("approval"), dict) else {}
        task_id = str(task.get("task_id") or "")
        if str(approval.get("intent_sha256") or "") != intent_sha256:
            messagebox.showerror(
                "API 审阅意图不一致",
                "持久化任务与刚才确认的意图不一致，已停止执行。",
                parent=self,
            )
            return
        task_status = str(task.get("status") or "")
        if task_status == "completed":
            self._show_operation_result(
                self.log_text,
                "API 论文审阅",
                task,
                summary="相同输入和配置的审阅已经完成，本次没有再次调用 API。",
            )
            messagebox.showinfo(
                "API 审阅已存在",
                "相同输入和配置的审阅已经完成，本次没有再次调用 API。",
                parent=self,
            )
            return
        if task_status == "waiting_approval":
            try:
                coordinator.approve_profile_review(
                    task_id,
                    intent_sha256=intent_sha256,
                )
            except Exception as exc:
                messagebox.showerror("API 审阅未获授权", str(exc), parent=self)
                return
        elif task_status != "queued":
            messagebox.showerror(
                "API 审阅不能执行",
                f"当前任务状态：{task_status or 'unknown'}。\n\n不会自动重试或切换执行模式。",
                parent=self,
            )
            return

        def completed(result: dict[str, Any]) -> None:
            self.refresh()
            status = str(result.get("status") or "")
            self._show_operation_result(
                self.log_text,
                "API 论文审阅",
                result,
                summary=(
                    "审阅已经通过校验并写入私有暂存区。"
                    if status == "completed"
                    else "审阅未完成；没有覆盖论文正式文件。"
                ),
            )
            if status == "completed":
                messagebox.showinfo(
                    "API 审阅完成",
                    "审阅已通过结构校验并写入私有暂存区。\n\n"
                    "论文正式文件没有被覆盖。",
                    parent=self,
                )
            else:
                blocker = str((result.get("result") or {}).get("blocker_code") or status)
                messagebox.showerror(
                    "API 审阅未完成",
                    f"错误码：{blocker}\n\n没有自动重试，也没有切换到 Codex 账号模式。",
                    parent=self,
                )

        self._run_background_task(
            "API 论文审阅",
            lambda: coordinator.run_profile_review(task_id),
            completed,
        )

    def open_smart_submission_settings(self) -> None:
        status = smart_submission_status()
        state = self._load_smart_submission_state()
        settings = (
            state.get("settings", {})
            if isinstance(state.get("settings"), dict)
            else {}
        )
        automation = status.get("automation", {}) if isinstance(status.get("automation"), dict) else {}
        program_timer = (
            status.get("program_timer", {})
            if isinstance(status.get("program_timer"), dict)
            else {}
        )
        sdk_runtime = (
            status.get("sdk_runtime", {})
            if isinstance(status.get("sdk_runtime"), dict)
            else {}
        )
        program_control = (
            status.get("program_control", {})
            if isinstance(status.get("program_control"), dict)
            else {}
        )

        dialog = tk.Toplevel(self)
        dialog.title("智能投稿设置（试运行通道）")
        self._fit_dialog_window(
            dialog,
            preferred=(1040, 720),
            minimum=(760, 520),
        )
        dialog.transient(self)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(dialog)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        overview = ttk.Frame(notebook, padding=14)
        account_tab = ttk.Frame(notebook, padding=14)
        prompt_tab = ttk.Frame(notebook, padding=10)
        maintenance = ttk.Frame(notebook, padding=14)
        notebook.add(overview, text="运行概况")
        notebook.add(account_tab, text="账号与登录")
        notebook.add(prompt_tab, text="历史回退说明")
        notebook.add(maintenance, text="高级维护")

        overview.columnconfigure(1, weight=1)
        rows = [
            ("通道定位", "程序调度 + Codex SDK 固定论文会话 + 受限浏览器工具"),
            ("运行模式", self._smart_mode_label(str(status.get("mode") or ""))),
            ("SDK 后台", "运行中" if sdk_runtime.get("running") else "空闲"),
            ("SDK 模型", str(sdk_runtime.get("configured_model") or "-")),
            ("固定 SDK 会话", str(sdk_runtime.get("fixed_session_count") or 0)),
            ("并发上限", str(program_control.get("max_parallel_workers") or 1)),
            (
                "程序定时器",
                (
                    f"{'已启用' if program_timer.get('enabled') else '已暂停'}；"
                    f"每天 {program_timer.get('time_local') or '-'}；"
                    + (
                        "Windows 任务 + 窗口内防漏"
                        if program_timer.get("native_task_registered")
                        else "窗口内防漏"
                    )
                ),
            ),
            (
                "历史 Codex Automation",
                (
                    "已停用"
                    if program_timer.get("legacy_automation_retired")
                    else self._automation_status_label(
                        str(automation.get("status") or "MISSING")
                    )
                ),
            ),
            ("本地队列", str(status.get("queue_count") or 0)),
            ("在线队列", str(status.get("online_queue_count") or 0)),
            ("已配置账号", f"{status.get('configured_account_count') or 0}/{status.get('account_count') or 0}"),
            ("投稿系统", str(status.get("system_count") or 0)),
            ("期刊入口", str(status.get("journal_count") or 0)),
            (
                "每日状态检查",
                (
                    f"{'已启用' if status.get('daily_monitor_enabled') else '已暂停'}；"
                    f"每天 {status.get('daily_monitor_time_local') or '09:00'}（本机时间）"
                ),
            ),
            ("下次状态检查", str(status.get("next_monitor_at") or "-")),
            (
                "本机定时任务",
                (
                    "已注册（即使主窗口关闭也可按时唤醒）"
                    if status.get("native_monitor_task_registered")
                    else "尚未注册；主窗口打开时由程序内定时器兜底"
                ),
            ),
            (
                "上次状态检查",
                (
                    f"{status.get('daily_monitor_last_finished_at') or status.get('daily_monitor_last_started_at') or '-'}；"
                    f"状态 {status.get('daily_monitor_last_status') or 'never_run'}；"
                    f"完成 {status.get('daily_monitor_last_checked_count') or 0}/"
                    f"{status.get('daily_monitor_last_paper_count') or 0} 篇；"
                    f"失败 {status.get('daily_monitor_last_failed_count') or 0}；"
                    f"需处理 {status.get('daily_monitor_last_action_required_count') or 0}"
                    f"；自动排队 {status.get('daily_monitor_last_followup_auto_queued_count') or 0}"
                    f"；待论文修订 {status.get('daily_monitor_last_followup_manual_revision_count') or 0}"
                    f"；投稿编号冲突 {status.get('daily_monitor_last_followup_identity_reconciliation_count') or 0}"
                ),
            ),
            ("本地凭据代理", "已启用；密码直接填入本机可见浏览器，不进入 SDK 会话"),
            (
                "缺少账号时",
                {
                    "auto_register": "自动进入站点注册流程",
                    "ask": "等待用户选择",
                    "block": "停止并提示",
                }.get(str(settings.get("account_missing_policy") or ""), "自动进入站点注册流程"),
            ),
        ]
        for row, (label, value) in enumerate(rows):
            ttk.Label(overview, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 16), pady=6)
            ttk.Label(overview, text=value, wraplength=760, justify=tk.LEFT).grid(row=row, column=1, sticky="ew", pady=6)

        ttk.Label(overview, text="最终提交策略").grid(
            row=len(rows), column=0, sticky="nw", padx=(0, 16), pady=6
        )
        execution_policy = ttk.Frame(overview)
        execution_policy.grid(row=len(rows), column=1, sticky="w", pady=6)
        auto_final_submit_var = tk.BooleanVar(
            value=bool(settings.get("auto_final_submit_enabled", True))
        )
        ttk.Checkbutton(
            execution_policy,
            text="最终审核通过后由程序自动完成最终提交（默认）",
            variable=auto_final_submit_var,
        ).pack(side=tk.LEFT)

        def save_execution_policy() -> None:
            result = update_smart_submission_execution_policy(
                auto_final_submit_enabled=auto_final_submit_var.get(),
            )
            self.refresh_smart_submission()
            messagebox.showinfo(
                "最终提交策略",
                (
                    "已启用自动最终提交；启动智能投稿即授权完整流程。"
                    if result.get("auto_final_submit_enabled")
                    else "已关闭自动最终提交；最终页将恢复为逐篇手动确认。"
                ),
                parent=dialog,
            )

        ttk.Button(
            execution_policy,
            text="保存",
            command=save_execution_policy,
        ).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(overview, text="每日检查时间").grid(
            row=len(rows) + 1, column=0, sticky="nw", padx=(0, 16), pady=6
        )
        monitor_schedule = ttk.Frame(overview)
        monitor_schedule.grid(row=len(rows) + 1, column=1, sticky="w", pady=6)
        monitor_time_var = tk.StringVar(
            value=str(status.get("daily_monitor_time_local") or "09:00")
        )
        ttk.Entry(
            monitor_schedule,
            textvariable=monitor_time_var,
            width=8,
        ).pack(side=tk.LEFT)
        ttk.Label(
            monitor_schedule,
            text="本机时间，24 小时制，例如 09:00 或 21:30",
            foreground="#666666",
        ).pack(side=tk.LEFT, padx=(8, 12))

        def save_monitor_schedule() -> None:
            try:
                result = update_smart_submission_daily_monitoring_time(
                    monitor_time_var.get()
                )
            except (ValueError, RuntimeError) as exc:
                messagebox.showerror("每日检查时间无效", str(exc), parent=dialog)
                return
            self._smart_monitor_next_poll_at = 0.0
            monitor_time_var.set(
                str(result.get("daily_monitor_time_local") or monitor_time_var.get())
            )
            self.refresh_smart_submission()
            messagebox.showinfo(
                "每日状态检查",
                (
                    f"已设置为每天 {result.get('daily_monitor_time_local')}（本机时间）。\n\n"
                    f"下一次检查：{result.get('next_monitor_at') or '-'}"
                ),
                parent=dialog,
            )

        ttk.Button(
            monitor_schedule,
            text="保存并启用",
            command=save_monitor_schedule,
        ).pack(side=tk.LEFT)
        ttk.Button(
            monitor_schedule,
            text="立即检查一次",
            command=self.run_smart_submission_monitor_now,
        ).pack(side=tk.LEFT, padx=(8, 0))

        mode_buttons = ttk.Frame(overview)
        mode_buttons.grid(row=len(rows) + 2, column=1, sticky="w", pady=(14, 0))
        ttk.Button(mode_buttons, text="暂停智能投稿", command=self.pause_smart_submission).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(mode_buttons, text="启用每日监控", command=self.enable_smart_submission_daily_monitoring).pack(
            side=tk.LEFT, padx=6
        )

        account_tab.columnconfigure(1, weight=1)
        ttk.Label(
            account_tab,
            text=(
                "保存的密码由本程序内的可信本地代理从 Windows DPAPI 加密库读取，"
                "直接填入可见浏览器。Codex SDK 只能看到“登录已完成”以及投稿页面，"
                "看不到密码、Cookie、一次性验证码或加密库内容。"
            ),
            foreground="#28612f",
            wraplength=780,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        ttk.Label(account_tab, text="没有匹配账号时").grid(
            row=1, column=0, sticky="w", padx=(0, 16), pady=6
        )
        policy_labels = {
            "自动注册（推荐）": "auto_register",
            "先询问": "ask",
            "停止并提示": "block",
        }
        reverse_policy_labels = {value: key for key, value in policy_labels.items()}
        policy_var = tk.StringVar(
            value=reverse_policy_labels.get(
                str(settings.get("account_missing_policy") or "auto_register"),
                "自动注册（推荐）",
            )
        )
        ttk.Combobox(
            account_tab,
            textvariable=policy_var,
            values=list(policy_labels),
            state="readonly",
            width=24,
        ).grid(row=1, column=1, sticky="w", pady=6)
        orcid_var = tk.BooleanVar(
            value=(
                str(
                    settings.get("new_account_auth_preference")
                    or "orcid_first"
                ).casefold()
                == "orcid_first"
            )
        )
        ttk.Checkbutton(
            account_tab,
            text=(
                "新站点无已有账号时优先使用 ORCID 登录/注册；"
                "不可用时自动回退到站点注册"
            ),
            variable=orcid_var,
        ).grid(row=2, column=1, sticky="w", pady=6)
        terms_var = tk.BooleanVar(
            value=bool(
                settings.get(
                    "account_registration_requires_terms_confirmation",
                    True,
                )
            )
        )
        ttk.Checkbutton(
            account_tab,
            text="遇到新网站条款时暂停，待我在可见网页确认后继续",
            variable=terms_var,
        ).grid(row=3, column=1, sticky="w", pady=6)
        ttk.Label(
            account_tab,
            text=(
                "ORCID 常规 OAuth 授权可按上述首选策略自动完成；验证码、2FA、"
                "邮箱验证、付款、强制 OA 及新增法律/伦理事实仍作为可恢复的人工作业显示。"
            ),
            foreground="#805500",
            wraplength=720,
            justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 8))

        def save_credential_policy() -> None:
            update_smart_credential_policy(
                account_missing_policy=policy_labels[policy_var.get()],
                require_terms_confirmation=terms_var.get(),
                prefer_orcid=orcid_var.get(),
                auto_authorize_orcid=orcid_var.get(),
            )
            self.refresh_smart_submission()
            messagebox.showinfo(
                "账号与登录策略",
                "设置已保存，将用于之后启动或恢复的智能投稿任务。",
                parent=dialog,
            )

        account_actions = ttk.Frame(account_tab)
        account_actions.grid(row=5, column=1, sticky="w", pady=(14, 0))
        ttk.Button(
            account_actions,
            text="保存账号与登录策略",
            command=save_credential_policy,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            account_actions,
            text="打开账号与站点",
            command=self.open_account_site_manager,
        ).pack(side=tk.LEFT)

        prompt_tab.columnconfigure(0, weight=1)
        prompt_tab.rowconfigure(1, weight=1)
        ttk.Label(
            prompt_tab,
            text=(
                "当前智能投稿只使用程序状态机和固定 SDK 论文会话。历史 Codex Automation 已停用；"
                "这里保留旧文件仅用于审计和必要时人工回退，不参与日常调度。"
            ),
            foreground="#805500",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        prompt_frame = ttk.Frame(prompt_tab)
        prompt_frame.grid(row=1, column=0, sticky="nsew")
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        prompt_text = tk.Text(prompt_frame, wrap="word", borderwidth=1, relief="solid")
        prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scroll = ttk.Scrollbar(prompt_frame, orient=tk.VERTICAL, command=prompt_text.yview)
        prompt_scroll.grid(row=0, column=1, sticky="ns")
        prompt_text.configure(yscrollcommand=prompt_scroll.set)
        try:
            prompt_text.insert("1.0", SMART_SCHEDULER_PROMPT_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            prompt_text.insert("1.0", f"无法读取调度提示词：{exc}")
        prompt_text.configure(state="disabled")
        ttk.Button(prompt_tab, text="打开历史回退提示词（只读审计）", command=lambda: self.open_path(SMART_SCHEDULER_PROMPT_PATH)).grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )

        ttk.Label(
            maintenance,
            text="以下入口用于排错和审计，不属于日常投稿流程。账号密码请统一通过“账号与站点”管理。",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 12))
        for label, command in (
            ("打开模块目录", lambda: self.open_path(SMART_SUBMISSION_DIR)),
            ("打开状态文件", lambda: self.open_path(SMART_STATE_PATH)),
            ("打开 SDK 执行日志", lambda: self.open_path(SMART_SUBMISSION_SDK_JOURNAL_PATH)),
            ("打开历史 Automation 配置（应为 PAUSED）", lambda: self.open_path(SMART_AUTOMATION_PATH)),
            ("打开审计记录", lambda: self.open_path(SMART_AUDIT_DIR)),
        ):
            ttk.Button(maintenance, text=label, command=command).pack(anchor=tk.W, pady=4)

    def manage_author_templates(self) -> None:
        adapter = self.orchestrator.adapters["journal_author"]
        raw_data = self.orchestrator.author_templates()
        data = json.loads(json.dumps(raw_data, ensure_ascii=False))
        templates = data.get("templates", [])
        if not templates:
            messagebox.showwarning("作者信息模板", "没有可编辑的作者信息模板。")
            return

        dialog = tk.Toplevel(self)
        dialog.title("作者信息管理")
        self._fit_dialog_window(
            dialog,
            preferred=(1120, 760),
            minimum=(760, 520),
        )
        dialog.transient(self)
        dialog.columnconfigure(1, weight=1)
        dialog.rowconfigure(1, weight=1)

        ttk.Label(
            dialog,
            text=(
                "模板内容会写入每篇论文自己的 author_info.md；模板状态为“就绪”且无 TODO 时才继续生成投稿包。"
                "保存时内部仍写入 Status: READY。"
            ),
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))

        left = ttk.Frame(dialog, padding=(10, 0, 8, 10))
        left.grid(row=1, column=0, sticky="ns")
        left.rowconfigure(0, weight=1)
        listbox = tk.Listbox(left, width=24, exportselection=False)
        listbox.grid(row=0, column=0, sticky="ns")

        right = ttk.Frame(dialog, padding=(0, 0, 10, 10))
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)

        ttk.Label(right, text="模板名称").grid(row=0, column=0, sticky="w", pady=(0, 6))
        name_var = tk.StringVar(value="")
        ttk.Entry(right, textvariable=name_var).grid(row=0, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(right, text="模板状态").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ready_var = tk.StringVar(value=author_status_label("DRAFT"))
        ttk.Combobox(
            right,
            textvariable=ready_var,
            values=list(AUTHOR_STATUS_LABELS.values()),
            width=12,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", pady=(0, 6))

        status_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=status_var).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        form_shell = ttk.Frame(right)
        form_shell.grid(row=3, column=0, columnspan=2, sticky="nsew")
        form_shell.columnconfigure(0, weight=1)
        form_shell.rowconfigure(0, weight=1)
        canvas = tk.Canvas(form_shell, borderwidth=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(form_shell, orient=tk.VERTICAL, command=canvas.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=ybar.set)
        form_inner = ttk.Frame(canvas)
        form_window = canvas.create_window((0, 0), window=form_inner, anchor="nw")
        form_inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(form_window, width=event.width))

        current = {"index": 0}
        field_vars: dict[str, tk.StringVar | tk.BooleanVar] = {}
        text_widgets: dict[str, tk.Text] = {}
        form_ready = {"value": False}

        def template_label(item: dict) -> str:
            default_mark = "（默认）" if item.get("id") == data.get("default_template_id") else ""
            return f"{item.get('name') or item.get('id')}{default_mark}"

        def refresh_list() -> None:
            listbox.delete(0, tk.END)
            for item in templates:
                listbox.insert(tk.END, template_label(item))

        def watch(var: tk.Variable) -> None:
            var.trace_add("write", lambda *_args: validate_current_form())

        def entry(parent: ttk.Frame, row: int, col: int, label: str, key: str, width: int = 28) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(6, 4), pady=3)
            var = tk.StringVar(value="")
            watch(var)
            field_vars[key] = var
            ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=col + 1, sticky="ew", padx=(0, 8), pady=3)

        def multiline(parent: ttk.Frame, label: str, key: str, height: int = 3) -> None:
            frame = ttk.LabelFrame(parent, text=label, padding=6)
            frame.pack(fill=tk.X, pady=4)
            frame.columnconfigure(0, weight=1)
            text = tk.Text(frame, height=height, wrap="word", undo=True)
            text.grid(row=0, column=0, sticky="ew")
            text.bind("<KeyRelease>", lambda _event: validate_current_form())
            text_widgets[key] = text

        def build_form(form: dict) -> None:
            form_ready["value"] = False
            for child in form_inner.winfo_children():
                child.destroy()
            field_vars.clear()
            text_widgets.clear()

            normalized = adapter._normalize_author_form(form)
            ready_var.set(author_status_label(normalized.get("status") or "DRAFT"))

            authors_frame = ttk.LabelFrame(form_inner, text="作者信息（空白作者行会被忽略）", padding=8)
            authors_frame.pack(fill=tk.X, pady=(0, 8))
            for author_index, author in enumerate(normalized.get("authors", [])[:4], start=1):
                row_frame = ttk.LabelFrame(authors_frame, text=f"作者 {author_index}", padding=6)
                row_frame.pack(fill=tk.X, pady=4)
                row_frame.columnconfigure(1, weight=1)
                row_frame.columnconfigure(3, weight=1)
                prefix = f"author_{author_index - 1}"
                entry(row_frame, 0, 0, "姓名", f"{prefix}_name")
                entry(row_frame, 0, 2, "单位编号", f"{prefix}_affiliation", width=14)
                entry(row_frame, 1, 0, "邮箱", f"{prefix}_email")
                entry(row_frame, 1, 2, "ORCID", f"{prefix}_orcid", width=20)
                corr_var = tk.BooleanVar(value=bool(author.get("corresponding")))
                watch(corr_var)
                field_vars[f"{prefix}_corresponding"] = corr_var
                ttk.Checkbutton(row_frame, text="通讯作者", variable=corr_var).grid(row=2, column=0, columnspan=2, sticky="w", padx=6)
                field_vars[f"{prefix}_name"].set(str(author.get("name") or ""))
                field_vars[f"{prefix}_affiliation"].set(str(author.get("affiliation") or ""))
                field_vars[f"{prefix}_email"].set(str(author.get("email") or ""))
                field_vars[f"{prefix}_orcid"].set(str(author.get("orcid") or ""))

            affiliations_frame = ttk.LabelFrame(form_inner, text="单位信息（空白单位行会被忽略）", padding=8)
            affiliations_frame.pack(fill=tk.X, pady=(0, 8))
            for affiliation_index, affiliation in enumerate(normalized.get("affiliations", [])[:3], start=1):
                row_frame = ttk.LabelFrame(affiliations_frame, text=f"单位 {affiliation_index}", padding=6)
                row_frame.pack(fill=tk.X, pady=4)
                row_frame.columnconfigure(1, weight=1)
                row_frame.columnconfigure(3, weight=1)
                prefix = f"affiliation_{affiliation_index - 1}"
                entry(row_frame, 0, 0, "机构", f"{prefix}_institution")
                entry(row_frame, 0, 2, "院系", f"{prefix}_department")
                entry(row_frame, 1, 0, "城市", f"{prefix}_city")
                entry(row_frame, 1, 2, "国家", f"{prefix}_country", width=16)
                entry(row_frame, 2, 0, "邮编", f"{prefix}_postal_code", width=18)
                field_vars[f"{prefix}_institution"].set(str(affiliation.get("institution") or ""))
                field_vars[f"{prefix}_department"].set(str(affiliation.get("department") or ""))
                field_vars[f"{prefix}_city"].set(str(affiliation.get("city") or ""))
                field_vars[f"{prefix}_country"].set(str(affiliation.get("country") or ""))
                field_vars[f"{prefix}_postal_code"].set(str(affiliation.get("postal_code") or ""))

            corresponding = ttk.LabelFrame(form_inner, text="通讯作者", padding=8)
            corresponding.pack(fill=tk.X, pady=(0, 8))
            corresponding.columnconfigure(1, weight=1)
            corresponding.columnconfigure(3, weight=1)
            entry(corresponding, 0, 0, "姓名", "corresponding_name")
            entry(corresponding, 0, 2, "邮箱", "corresponding_email")
            ca = normalized.get("corresponding_author", {})
            field_vars["corresponding_name"].set(str(ca.get("name") or ""))
            field_vars["corresponding_email"].set(str(ca.get("email") or ""))

            declarations = ttk.LabelFrame(form_inner, text="声明信息", padding=8)
            declarations.pack(fill=tk.X, pady=(0, 8))
            ai_declaration = ttk.LabelFrame(declarations, text="AI 声明", padding=6)
            ai_declaration.pack(fill=tk.X, pady=4)
            ttk.Label(
                ai_declaration,
                text="选刊与投稿阶段只能按该策略原样使用固定正文，不允许 AI 自行扩写或改写。",
                wraplength=820,
                justify=tk.LEFT,
            ).pack(fill=tk.X, padx=6, pady=(0, 4))
            ai_header = ttk.Frame(ai_declaration)
            ai_header.pack(fill=tk.X)
            ai_header.columnconfigure(1, weight=1)
            ttk.Label(ai_header, text="插入策略").grid(
                row=0,
                column=0,
                sticky="w",
                padx=(6, 4),
                pady=3,
            )
            ai_policy_var = tk.StringVar(
                value=ai_use_policy_label(normalized.get("ai_use_policy"))
            )
            watch(ai_policy_var)
            field_vars["ai_use_policy"] = ai_policy_var
            ttk.Combobox(
                ai_header,
                textvariable=ai_policy_var,
                values=list(AI_USE_POLICY_LABELS.values()),
                state="readonly",
                width=18,
            ).grid(row=0, column=1, sticky="w", padx=(0, 8), pady=3)
            entry(ai_header, 1, 0, "使用工具", "ai_use_tools", width=48)
            multiline(ai_declaration, "使用范围", "ai_use_scope", height=2)
            multiline(ai_declaration, "固定声明正文", "ai_use_statement", height=4)
            field_vars["ai_use_tools"].set(str(normalized.get("ai_use_tools") or ""))
            text_widgets["ai_use_scope"].insert(
                "1.0",
                str(normalized.get("ai_use_scope") or ""),
            )
            text_widgets["ai_use_statement"].insert(
                "1.0",
                str(normalized.get("ai_use_statement") or ""),
            )
            multiline(declarations, "作者贡献声明", "author_contribution_statement")
            multiline(declarations, "基金", "funding")
            multiline(declarations, "利益冲突", "conflict_of_interest")
            multiline(declarations, "数据可用性", "data_availability")
            multiline(declarations, "代码可用性", "code_availability")
            multiline(declarations, "伦理声明", "ethics_statement")
            multiline(
                declarations,
                "推荐审稿专家（姓名、单位、邮箱、完整地址、专长）",
                "recommended_reviewers",
                height=8,
            )
            for key in (
                "author_contribution_statement",
                "funding",
                "conflict_of_interest",
                "data_availability",
                "code_availability",
                "ethics_statement",
                "recommended_reviewers",
            ):
                text_widgets[key].delete("1.0", tk.END)
                text_widgets[key].insert("1.0", str(normalized.get(key) or ""))
            form_ready["value"] = True

        def collect_form() -> dict:
            authors = []
            for index in range(4):
                prefix = f"author_{index}"
                authors.append(
                    {
                        "name": str(field_vars[f"{prefix}_name"].get()).strip(),
                        "affiliation": str(field_vars[f"{prefix}_affiliation"].get()).strip(),
                        "email": str(field_vars[f"{prefix}_email"].get()).strip(),
                        "orcid": str(field_vars[f"{prefix}_orcid"].get()).strip(),
                        "corresponding": bool(field_vars[f"{prefix}_corresponding"].get()),
                    }
                )
            affiliations = []
            for index in range(3):
                prefix = f"affiliation_{index}"
                affiliations.append(
                    {
                        "index": index + 1,
                        "institution": str(field_vars[f"{prefix}_institution"].get()).strip(),
                        "department": str(field_vars[f"{prefix}_department"].get()).strip(),
                        "city": str(field_vars[f"{prefix}_city"].get()).strip(),
                        "country": str(field_vars[f"{prefix}_country"].get()).strip(),
                        "postal_code": str(field_vars[f"{prefix}_postal_code"].get()).strip(),
                    }
                )
            return {
                "status": author_status_value(ready_var.get()),
                "authors": authors,
                "affiliations": affiliations,
                "corresponding_author": {
                    "name": str(field_vars["corresponding_name"].get()).strip(),
                    "email": str(field_vars["corresponding_email"].get()).strip(),
                },
                "author_contribution_statement": text_widgets[
                    "author_contribution_statement"
                ].get("1.0", "end-1c").strip(),
                "funding": text_widgets["funding"].get("1.0", "end-1c").strip(),
                "conflict_of_interest": text_widgets["conflict_of_interest"].get("1.0", "end-1c").strip(),
                "data_availability": text_widgets["data_availability"].get("1.0", "end-1c").strip(),
                "code_availability": text_widgets["code_availability"].get("1.0", "end-1c").strip(),
                "ethics_statement": text_widgets["ethics_statement"].get("1.0", "end-1c").strip(),
                "recommended_reviewers": text_widgets["recommended_reviewers"].get(
                    "1.0", "end-1c"
                ).strip(),
                "ai_use_policy": ai_use_policy_value(
                    field_vars["ai_use_policy"].get()
                ),
                "ai_use_tools": str(field_vars["ai_use_tools"].get()).strip(),
                "ai_use_scope": text_widgets["ai_use_scope"].get("1.0", "end-1c").strip(),
                "ai_use_statement": text_widgets["ai_use_statement"].get("1.0", "end-1c").strip(),
            }

        def validate_current_form() -> None:
            if not form_ready["value"]:
                return
            content = adapter.render_author_info_from_form(collect_form())
            issues = adapter.author_info_issues(content)
            if issues:
                status_var.set("状态：未就绪；" + "；".join(issues))
            else:
                status_var.set("状态：就绪，可通过选刊作者调度器的作者信息门槛")

        def commit_current() -> None:
            index = current["index"]
            if not 0 <= index < len(templates):
                return
            item = templates[index]
            item["name"] = name_var.get().strip() or f"模板 {index + 1}"
            item["form"] = collect_form()
            item["content"] = adapter.render_author_info_from_form(item["form"])
            refresh_list()

        def load_template(index: int) -> None:
            if not 0 <= index < len(templates):
                return
            current["index"] = index
            item = templates[index]
            name_var.set(str(item.get("name") or f"模板 {index + 1}"))
            form = item.get("form") if isinstance(item.get("form"), dict) else adapter._form_from_content(str(item.get("content") or ""))
            build_form(form)
            validate_current_form()

        def on_select(_event: tk.Event) -> None:
            selection = listbox.curselection()
            if not selection:
                return
            new_index = int(selection[0])
            if new_index == current["index"]:
                return
            commit_current()
            load_template(new_index)
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(new_index)

        def set_default() -> None:
            commit_current()
            item = templates[current["index"]]
            data["default_template_id"] = item.get("id")
            refresh_list()
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(current["index"])

        def save_templates() -> None:
            commit_current()
            for item in templates:
                item["is_default"] = item.get("id") == data.get("default_template_id")
            try:
                saved = self.orchestrator.save_author_templates(data)
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)
                return
            data.clear()
            data.update(saved)
            templates[:] = data.get("templates", [])
            refresh_list()
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(current["index"])
            self.refresh()
            messagebox.showinfo("已保存", "作者信息模板已保存。已有论文不会自动覆盖；需要时右键选中论文套用模板。", parent=dialog)

        listbox.bind("<<ListboxSelect>>", on_select)
        ready_var.trace_add("write", lambda *_args: validate_current_form())
        refresh_list()
        listbox.selection_set(0)
        load_template(0)

        buttons = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="设为默认", command=set_default).pack(side=tk.LEFT, padx=3)
        ttk.Button(buttons, text="保存模板", command=save_templates).pack(side=tk.LEFT, padx=3)
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT, padx=3)

    def apply_author_template_to_selected(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在选刊作者或智能投稿页选择要更改作者信息模板的论文。")
            return
        data = self.orchestrator.author_templates()
        templates = [item for item in data.get("templates", []) if isinstance(item, dict)]
        if not templates:
            messagebox.showwarning("作者信息模板", "没有可用模板。")
            return

        dialog = tk.Toplevel(self)
        dialog.title("更改作者信息模板")
        self._fit_dialog_window(
            dialog,
            preferred=(560, 300),
            minimum=(520, 280),
            margins=(80, 140),
        )
        dialog.transient(self)
        dialog.columnconfigure(1, weight=1)

        ttk.Label(dialog, text=f"将模板写入选中 {len(paper_ids)} 篇论文的 author_info.md").grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 8)
        )

        values = []
        by_label: dict[str, str] = {}
        default_id = str(data.get("default_template_id") or "")
        for item in templates:
            label = f"{item.get('name') or item.get('id')} ({item.get('id')})"
            if item.get("id") == default_id:
                label += " 默认"
            values.append(label)
            by_label[label] = str(item.get("id") or "")

        selected_label = tk.StringVar(value=values[0])
        for value in values:
            if by_label.get(value) == default_id:
                selected_label.set(value)
                break

        ttk.Label(dialog, text="模板").grid(row=1, column=0, sticky="w", padx=10, pady=6)
        combo = ttk.Combobox(dialog, values=values, textvariable=selected_label, state="readonly")
        combo.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=6)

        overwrite_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(dialog, text="覆盖已有 author_info.md", variable=overwrite_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=10, pady=6
        )

        status_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=status_var, wraplength=520).grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=6)

        def update_status(_event: tk.Event | None = None) -> None:
            template_id = by_label.get(selected_label.get(), "")
            template = self.orchestrator.adapters["journal_author"].template_by_id(template_id, data)
            issues = self.orchestrator.adapters["journal_author"].author_info_issues(str(template.get("content") or ""))
            status_var.set("模板状态：READY" if not issues else "模板状态：未就绪；" + "；".join(issues))

        def apply_template() -> None:
            template_id = by_label.get(selected_label.get(), "")
            if not template_id:
                return
            preview = "\n".join(paper_ids[:10])
            if len(paper_ids) > 10:
                preview += f"\n... 还有 {len(paper_ids) - 10} 篇"
            if not messagebox.askyesno(
                "确认套用模板",
                "确认把所选模板写入这些论文的 author_info.md 吗？\n\n"
                "这不会启动调度器；如果模板不是 READY，选刊作者调度器会继续阻塞。\n\n"
                f"{preview}",
                parent=dialog,
            ):
                return
            state = self.orchestrator.apply_author_template(paper_ids, template_id, overwrite=overwrite_var.get())
            self.state_data = state
            self.refresh()
            self._show_operation_result(
                self.log_text,
                "作者信息模板已套用",
                state.get("last_scheduler_result", {}),
            )
            dialog.destroy()

        combo.bind("<<ComboboxSelected>>", update_status)
        update_status()

        buttons = ttk.Frame(dialog, padding=(10, 4, 10, 10))
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="套用模板", command=apply_template).pack(side=tk.LEFT, padx=3)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=3)

    def _load_writing_live_snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        adapter = self.orchestrator.adapters["writing"]
        pipeline = adapter.load_state()
        runtime = adapter.runtime
        runtime_status = runtime.status(check_account=False)
        journal = read_json(runtime.journal_path, {"papers": {}})
        journal_papers = journal.get("papers", {}) if isinstance(journal, dict) else {}
        if not isinstance(pipeline, dict):
            pipeline = {}
        if not isinstance(runtime_status, dict):
            runtime_status = {}
        if not isinstance(journal_papers, dict):
            journal_papers = {}
        return pipeline, runtime_status, journal_papers

    def refresh_writing_live_tasks(
        self,
        *,
        update_detail: bool = True,
        snapshot: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tree = self.writing_task_tree
        try:
            pipeline, runtime_status, journal_papers = (
                snapshot if snapshot is not None else self._load_writing_live_snapshot()
            )
        except Exception as exc:
            blocker = str(getattr(exc, "code", type(exc).__name__))
            if self.writing_live_summary_var is not None:
                self.writing_live_summary_var.set(f"实时状态读取失败：{blocker}")
            if "writing" in self.stage_status_vars:
                self.stage_status_vars["writing"].set("写作队列：状态读取失败")
            return {"ok": False, "blocker": blocker, "rows": []}

        self._writing_pipeline_state = pipeline
        self._writing_runtime_status = runtime_status
        archived = self.state_data.get("archived_papers", {})
        archived_ids = set(archived) if isinstance(archived, dict) else set()
        if archived_ids:
            journal_papers = {
                key: record
                for key, record in journal_papers.items()
                if not (
                    isinstance(record, dict)
                    and str(record.get("paper_id") or "").strip() in archived_ids
                )
            }
        self._writing_journal_papers = journal_papers
        rows = _writing_task_rows(journal_papers, pipeline)
        self.writing_task_rows = {str(row["paper_key"]): row for row in rows}
        visible_rows = self._visible_live_rows(rows)

        if tree is not None:
            _sync_treeview_rows(
                tree,
                [
                    _treeview_row(
                        row["paper_key"],
                        compact_paper_label(row["label"]),
                        (
                            row["status_label"],
                            row["step_label"],
                            row["executor_label"],
                            row["progress"],
                            row["session_label"],
                            table_issue_summary(row["blocker_label"]),
                            row["updated_label"],
                        ),
                        (row["tag"],),
                    )
                    for row in visible_rows
                ],
            )
            self._fit_live_tree_height(tree, len(visible_rows))

        counts = Counter(str(row["status"]) for row in rows)
        active_count = int(runtime_status.get("active_count") or 0)
        queued_count = counts.get("queued", 0) + counts.get("ready", 0)
        waiting_count = sum(
            counts.get(status, 0)
            for status in ("waiting_event", "waiting_prerequisite", "waiting_capacity", "waiting")
        )
        blocked_count = counts.get("blocked", 0) + counts.get("failed", 0)
        complete_count = counts.get("complete", 0)
        settings = pipeline.get("settings", {}) if isinstance(pipeline.get("settings"), dict) else {}
        configured_parallel = _writing_step_number(
            settings.get("max_parallel_workers"),
            default=WRITING_DEFAULT_PARALLEL_WORKERS,
        ) or WRITING_DEFAULT_PARALLEL_WORKERS
        scheduling_mode = str(runtime_status.get("scheduling_mode") or settings.get("scheduling_mode") or "")
        scheduler_label = "已暂停" if scheduling_mode.strip().lower() == "manual" else WRITING_TASK_STATUS_LABELS.get(
            str(runtime_status.get("status") or "").strip().lower(),
            "运行中",
        )
        summary = (
            f"运行 {active_count}｜排队 {queued_count}｜等待 {waiting_count}｜"
            f"阻塞 {blocked_count}｜完成 {complete_count}｜并发 {active_count}/{configured_parallel}"
        )
        account = runtime_status.get("account") if isinstance(runtime_status.get("account"), dict) else {}
        auth_mode = str(account.get("auth_mode") or "").strip().lower()
        auth_label = {
            "chatgpt": "ChatGPT 账号",
            "api_key": "API Key",
        }.get(auth_mode, auth_mode or "未识别")
        summary += f"｜V2 SDK 认证：{auth_label}"
        try:
            gate_policy_getter = getattr(self.orchestrator, "writing_gate_policy", None)
            gate_policy = gate_policy_getter() if callable(gate_policy_getter) else {}
            gate_mode = str(gate_policy.get("mode") or STRICT_CONFIRMATION_MODE)
            gate_label = "自动信任" if gate_mode == AUTO_TRUST_MODE else "严格确认"
        except Exception:
            gate_label = "策略异常（按严格确认）"
        summary += f"｜Step 5 关口：{gate_label}"
        if self.writing_live_summary_var is not None:
            self.writing_live_summary_var.set(summary if rows else "当前没有 SDK 写作任务。")
        if "writing" in self.stage_status_vars:
            self.stage_status_vars["writing"].set(f"写作队列：{scheduler_label}｜实时刷新")
        if "writing" in self.stage_info_vars:
            self.stage_info_vars["writing"].set(
                f"实时任务：{len(rows)}｜正在运行：{active_count}｜并发上限：{configured_parallel}｜"
                f"等待：{waiting_count}｜卡点：{blocked_count}｜Step 5：{gate_label}"
            )

        if update_detail and self.current_tab_key() == "writing":
            live_selected = bool(tree is not None and tree.selection())
            canonical_selected = bool(self.stage_trees.get("writing") and self.stage_trees["writing"].selection())
            if live_selected:
                self.update_writing_task_detail()
            elif not canonical_selected:
                self.update_stage_detail("writing")
        return {
            "ok": True,
            "rows": rows,
            "runtime_status": runtime_status,
            "configured_parallel": configured_parallel,
        }

    def _schedule_writing_live_refresh(self) -> None:
        if self._writing_live_after_id is None:
            self._writing_live_after_id = self.after(2_000, self._poll_writing_live_tasks)

    def _poll_writing_live_tasks(self) -> None:
        self._writing_live_after_id = None
        try:
            self._poll_due_smart_submission_monitor()
            self._request_ui_refresh(
                tab=self.current_tab_key(),
                full=False,
                reason="实时状态",
                announce=False,
            )
        except (tk.TclError, RuntimeError):
            LOGGER.exception("SDK live task refresh failed")
        finally:
            try:
                if self.winfo_exists():
                    self._schedule_writing_live_refresh()
            except tk.TclError:
                pass

    def _reload_unified_projection_if_changed(self) -> bool:
        try:
            mtime_ns = UNIFIED_STATE_PATH.stat().st_mtime_ns
        except OSError:
            return False
        if mtime_ns <= self._unified_projection_mtime_ns:
            return False
        data = read_json(UNIFIED_STATE_PATH, {})
        if not isinstance(data, dict) or not isinstance(data.get("papers"), dict):
            return False
        self.state_data = data
        self._unified_projection_mtime_ns = mtime_ns
        self.render_overview_table()
        self.render_stage_tables()
        self.update_queue_panel()
        self.update_log_panel()
        return True

    def update_writing_task_detail(self) -> None:
        tree = self.writing_task_tree
        detail = self.stage_detail_texts.get("writing")
        panel = self.stage_detail_panels.get("writing")
        if tree is None or detail is None or panel is None or not tree.selection():
            return
        paper_key = str(tree.selection()[0])
        row = self.writing_task_rows.get(paper_key)
        if row is None:
            return
        canonical = self.stage_trees.get("writing")
        if canonical is not None:
            canonical_selected = canonical.selection()
            if canonical_selected:
                canonical.selection_remove(*canonical_selected)
        blocker = str(row.get("blocker") or "").strip()
        lines = [
            str(row.get("label") or paper_key),
            "",
            f"实时状态：{row.get('status_label') or '-'}",
            f"当前阶段：{row.get('step_label') or '-'}",
            f"总进度：{row.get('progress') or '-'}",
            f"已完成：Step {row.get('completed_step') or 0}",
            f"下一步：Step {row.get('current_step') or 1}",
            "执行方式：V2 本地 Codex SDK（内部 codex.exe CLI，不是桌面可见任务）",
            f"本 Step 模型：{row.get('model') or '-'}",
            f"推理强度：{_effort_display(row.get('reasoning_effort'))}",
            f"本 Step Token：{row.get('token_status_label') or '-'}",
            f"固定会话 ID：{row.get('thread_id') or '尚未创建'}",
            f"当前/最近 Turn ID：{row.get('turn_id') or '-'}",
            f"最后更新：{row.get('updated_label') or '-'}",
        ]
        friendly = str(row.get("blocker_label") or "").strip()
        if friendly:
            lines.extend(["", f"需要处理：{friendly}"])
        if blocker:
            lines.append(f"技术原因：{blocker}")
        blocker_message = str(row.get("blocker_message") or "").strip()
        if blocker_message:
            lines.append(f"真实说明：{blocker_message}")
        semantic_path = str(row.get("semantic_path") or "").strip()
        if semantic_path:
            lines.append(f"验收文件：{semantic_path}")
        semantic_issues = row.get("semantic_issues") if isinstance(row.get("semantic_issues"), list) else []
        semantic_warnings = (
            row.get("semantic_warnings") if isinstance(row.get("semantic_warnings"), list) else []
        )
        if semantic_issues:
            lines.append("验收问题：")
            lines.extend(
                f"- {item.get('code') or 'unknown'}：{item.get('message') or '-'}"
                for item in semantic_issues[:8]
                if isinstance(item, dict)
            )
        if semantic_warnings:
            lines.append("待后续步骤清理（当前不阻塞）：")
            lines.extend(
                f"- {item.get('message') or item.get('code') or '-'}"
                for item in semantic_warnings[:8]
                if isinstance(item, dict)
            )
        if not str(row.get("paper_id") or ""):
            lines.extend(["", "论文编号会在 Step 1 完成后自动出现；现在仍可在上方实时任务表查看运行状态。"])
        technical = "\n".join(lines)
        self._set_text(detail, technical)
        status = str(row.get("status") or "unknown")
        headline = friendly or f"程序将继续执行 Step {row.get('current_step') or 1}。"
        issue_messages = [
            str(item.get("message") or item.get("code") or "")
            for item in semantic_issues[:5]
            if isinstance(item, dict)
        ]
        panel.render(
            title=str(row.get("label") or paper_key),
            status=str(row.get("status_label") or self._detail_state_label(status)),
            tone=self._detail_tone(status),
            headline=headline,
            sections=(
                {
                    "title": "当前 Step",
                    "fields": (
                        ("步骤", row.get("step_label") or "-"),
                        ("总进度", row.get("progress") or "-"),
                        ("已完成", f"Step {row.get('completed_step') or 0}"),
                        ("最后更新", row.get("updated_label") or "-"),
                    ),
                },
                {
                    "title": "需要处理" if friendly or issue_messages else "下一动作",
                    "body": ([friendly] if friendly else []) + issue_messages
                    or [f"Step {row.get('current_step') or 1} 将由程序按冻结流程继续。"],
                    "tone": "danger" if issue_messages else "attention" if friendly else "info",
                },
            ),
            technical_text=technical,
        )
        self._set_technical_details(
            f"写论文 · SDK 任务 {paper_key}",
            {"runtime": self._writing_runtime_status, "task": row.get("record", {})},
        )

    def show_selected_writing_sdk_session(self) -> None:
        tree = self.writing_task_tree
        if tree is None or not tree.selection():
            messagebox.showinfo("查看 SDK 会话", "请先在实时写作任务表选择一篇论文。", parent=self)
            return
        paper_key = str(tree.selection()[0])
        row = self.writing_task_rows.get(paper_key, {})
        thread_id = str(row.get("thread_id") or "").strip()
        if not thread_id:
            messagebox.showinfo("查看 SDK 会话", "该任务尚未创建固定 SDK 会话。", parent=self)
            return
        runtime = self.orchestrator.adapters["writing"].runtime
        path = _writing_session_log_path(Path(runtime.codex_home), thread_id)
        if path is None:
            messagebox.showerror(
                "SDK 会话记录不存在",
                f"已记录会话 ID，但没有在本程序独立 CODEX_HOME 找到日志：\n\n{thread_id}",
                parent=self,
            )
            return
        transcript = _writing_session_transcript(path)
        if not transcript.get("ok"):
            messagebox.showerror("SDK 会话读取失败", f"无法读取：\n\n{path}", parent=self)
            return

        dialog = tk.Toplevel(self)
        dialog.title(f"SDK 会话 · {row.get('label') or paper_key}")
        self._fit_dialog_window(
            dialog,
            preferred=(1180, 760),
            minimum=(760, 500),
        )
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)

        header = ttk.Frame(dialog, padding=(10, 10, 10, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text=(
                f"论文：{row.get('label') or paper_key}\n"
                f"固定会话 ID：{thread_id}\n"
                f"当前/最近 Turn ID：{row.get('turn_id') or '-'}\n"
                "执行方式：V2 本地 Codex SDK → 内部 codex.exe CLI"
            ),
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="w")

        body = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        transcript_text = self._readonly_text(body)
        rendered: list[str] = []
        if transcript.get("changing"):
            rendered.extend(["提示：会话仍在写入，以下是读取瞬间的内容。", ""])
        for item in transcript.get("messages", []):
            if not isinstance(item, dict):
                continue
            timestamp = str(item.get("timestamp") or "-")
            role = str(item.get("role") or "记录")
            text = str(item.get("text") or "")
            rendered.extend([f"[{timestamp}] {role}", text, ""])
        if not rendered:
            rendered.append("该会话目前没有可展示的用户/Codex消息。")
        self._set_text(transcript_text, "\n".join(rendered))

        actions = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        actions.grid(row=2, column=0, sticky="ew")

        def copy_thread_id() -> None:
            dialog.clipboard_clear()
            dialog.clipboard_append(thread_id)

        ttk.Button(actions, text="复制会话 ID", command=copy_thread_id).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            actions,
            text="用记事本打开原始 JSONL",
            command=lambda: subprocess.Popen(["notepad.exe", str(path)]),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(actions, text=str(path), foreground="#666666").pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def _load_figure_live_snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        adapter = self.orchestrator.adapters["figures"]
        pipeline = adapter.load_state()
        runtime_status = adapter.runtime.status(check_account=False)
        journal = read_json(FIGURE_SDK_JOURNAL_PATH, {"papers": {}})
        journal_papers = journal.get("papers", {}) if isinstance(journal, dict) else {}
        if not isinstance(journal_papers, dict):
            journal_papers = {}
        return pipeline, runtime_status, journal_papers

    def refresh_figure_live_tasks(
        self,
        *,
        update_detail: bool = True,
        snapshot: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tree = self.figure_task_tree
        if tree is None:
            return {}
        try:
            pipeline, runtime_status, journal_papers = (
                snapshot if snapshot is not None else self._load_figure_live_snapshot()
            )
        except Exception as exc:
            blocker = str(getattr(exc, "code", type(exc).__name__))
            if self.figure_live_summary_var is not None:
                self.figure_live_summary_var.set(f"画图实时状态读取失败：{blocker}")
            return {"ok": False, "blocker": blocker}
        self._figure_pipeline_state = pipeline
        self._figure_runtime_status = runtime_status
        archived = self.state_data.get("archived_papers", {})
        archived_ids = set(archived) if isinstance(archived, dict) else set()
        if archived_ids:
            journal_papers = {
                paper_id: record
                for paper_id, record in journal_papers.items()
                if str(paper_id) not in archived_ids
            }
        self._figure_journal_papers = journal_papers
        rows = _figure_task_rows(journal_papers, pipeline)
        self.figure_task_rows = {str(row["paper_id"]): row for row in rows}
        visible_rows = self._visible_live_rows(rows)
        _sync_treeview_rows(
            tree,
            [
                _treeview_row(
                    row["paper_id"],
                    compact_paper_label(row["label"]),
                    (
                        row["status_label"],
                        row["step_label"],
                        row["executor_label"],
                        row["progress"],
                        row["session_label"],
                        table_issue_summary(row["blocker_label"]),
                        row["updated_label"],
                    ),
                    (row["tag"],),
                )
                for row in visible_rows
            ],
        )
        self._fit_live_tree_height(tree, len(visible_rows))
        counts = Counter(str(row.get("status") or "unknown") for row in rows)
        active_count = sum(counts.get(value, 0) for value in ("creating_thread", "starting", "running", "waiting_event"))
        waiting_count = counts.get("waiting_data", 0) + counts.get("paused", 0) + counts.get("migration_review", 0)
        summary = (
            f"实时任务：{len(rows)}｜正在运行：{active_count}｜等待/暂停：{waiting_count}｜"
            f"阻塞：{counts.get('blocked', 0)}｜完成：{counts.get('complete', 0)}｜"
            f"并发上限：{runtime_status.get('max_parallel_workers') or 1}"
        )
        paused_selected = int(runtime_status.get("paused_paper_count") or 0)
        if paused_selected:
            summary += f"｜单篇暂停：{paused_selected}"
        runtime_blocker = str(runtime_status.get("blocker") or "").strip()
        if runtime_blocker:
            summary += f"｜运行时卡点：{runtime_blocker}"
        if self.figure_live_summary_var is not None:
            self.figure_live_summary_var.set(summary if rows else "当前没有 V2 SDK 画图任务。")
        runtime_label = "运行中" if runtime_status.get("background_running") else "已暂停/空闲"
        self.stage_status_vars["figures"].set(
            f"V2 SDK：{runtime_label}｜固定会话：{len(journal_papers)}｜模型：{runtime_status.get('configured_model') or '-'}"
        )
        self.stage_info_vars["figures"].set(summary)
        if update_detail and tree.selection():
            self.update_figure_task_detail()
        return {"ok": True, "rows": rows, "runtime_status": runtime_status}

    def update_figure_task_detail(self) -> None:
        tree = self.figure_task_tree
        detail = self.stage_detail_texts.get("figures")
        panel = self.stage_detail_panels.get("figures")
        if tree is None or detail is None or panel is None or not tree.selection():
            return
        paper_id = str(tree.selection()[0])
        row = self.figure_task_rows.get(paper_id)
        if row is None:
            return
        canonical = self.stage_trees.get("figures")
        if canonical is not None and canonical.selection():
            canonical.selection_remove(*canonical.selection())
        lines = [
            paper_id,
            "",
            f"实时状态：{row.get('status_label') or '-'}",
            f"当前阶段：{row.get('step_label') or '-'}",
            f"总进度：{row.get('progress') or '-'}",
            "执行方式：V2 本地 Codex SDK（程序内部 codex.exe CLI）",
            "会话规则：每篇论文只绑定 1 个固定会话，按该论文冻结的工作流快照依次发送",
            f"本 Step 模型：{row.get('model') or '-'}",
            f"推理强度：{_effort_display(row.get('reasoning_effort'))}",
            f"本 Step Token：{row.get('token_status_label') or '-'}",
            f"流程版本：{row.get('workflow_version_id') or '历史默认3步'}",
            f"固定会话 ID：{row.get('thread_id') or '尚未创建'}",
            f"当前/最近 Turn ID：{row.get('turn_id') or '-'}",
            f"最后更新：{row.get('updated_label') or '-'}",
        ]
        blocker = str(row.get("blocker") or "").strip()
        friendly = str(row.get("blocker_label") or "").strip()
        if friendly:
            lines.extend(["", f"需要处理：{friendly}"])
        if blocker:
            lines.append(f"技术原因：{blocker}")
        issues = row.get("contract_issues") if isinstance(row.get("contract_issues"), list) else []
        warnings = row.get("contract_warnings") if isinstance(row.get("contract_warnings"), list) else []
        if issues:
            lines.append("产物验收问题：")
            lines.extend(f"- {value}" for value in issues[:8])
        if warnings:
            lines.append("产物验收提醒：")
            lines.extend(f"- {value}" for value in warnings[:8])
        technical = "\n".join(lines)
        self._set_text(detail, technical)
        status = str(row.get("status") or "unknown")
        panel.render(
            title=paper_id,
            status=str(row.get("status_label") or self._detail_state_label(status)),
            tone=self._detail_tone(status),
            headline=friendly or "程序将按当前图表流程继续执行。",
            sections=(
                {
                    "title": "当前任务",
                    "fields": (
                        ("步骤", row.get("step_label") or "-"),
                        ("总进度", row.get("progress") or "-"),
                        ("最后更新", row.get("updated_label") or "-"),
                    ),
                },
                {
                    "title": "产物与数据",
                    "body": ([friendly] if friendly else []) + [str(value) for value in issues[:5]],
                    "tone": "danger" if issues else "attention" if friendly else "success",
                },
            ),
            technical_text=technical,
        )
        self._set_technical_details(
            f"图表与插图 · SDK 任务 {paper_id}",
            {"runtime": self._figure_runtime_status, "task": row.get("record", {})},
        )

    def show_selected_figure_sdk_session(self) -> None:
        tree = self.figure_task_tree
        if tree is None or not tree.selection():
            messagebox.showinfo("查看 SDK 会话", "请先在实时画图任务表选择一篇论文。", parent=self)
            return
        paper_id = str(tree.selection()[0])
        row = self.figure_task_rows.get(paper_id, {})
        thread_id = str(row.get("thread_id") or "").strip()
        if not thread_id:
            messagebox.showinfo("查看 SDK 会话", "该画图任务尚未创建固定 SDK 会话。", parent=self)
            return
        runtime = self.orchestrator.adapters["figures"].runtime
        path = _writing_session_log_path(Path(runtime.codex_home), thread_id)
        if path is None:
            messagebox.showerror(
                "SDK 会话记录不存在",
                f"已记录会话 ID，但没有在本程序独立 CODEX_HOME 找到日志：\n\n{thread_id}",
                parent=self,
            )
            return
        transcript = _writing_session_transcript(path)
        if not transcript.get("ok"):
            messagebox.showerror("SDK 会话读取失败", f"无法读取：\n\n{path}", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"画图 SDK 会话 · {paper_id}")
        self._fit_dialog_window(
            dialog,
            preferred=(1180, 760),
            minimum=(760, 500),
        )
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        header = ttk.Frame(dialog, padding=(10, 10, 10, 6))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            header,
            text=(
                f"论文：{paper_id}\n固定会话 ID：{thread_id}\n"
                f"当前/最近 Turn ID：{row.get('turn_id') or '-'}\n"
                "执行方式：V2 本地 Codex SDK → 内部 codex.exe CLI"
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        body = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        transcript_text = self._readonly_text(body)
        rendered: list[str] = []
        if transcript.get("changing"):
            rendered.extend(["提示：会话仍在写入，以下是读取瞬间的内容。", ""])
        for item in transcript.get("messages", []):
            if not isinstance(item, dict):
                continue
            rendered.extend(
                [
                    f"[{item.get('timestamp') or '-'}] {item.get('role') or '记录'}",
                    str(item.get("text") or ""),
                    "",
                ]
            )
        self._set_text(transcript_text, "\n".join(rendered) if rendered else "该会话目前没有可展示的消息。")
        actions = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        actions.grid(row=2, column=0, sticky="ew")

        def copy_thread_id() -> None:
            dialog.clipboard_clear()
            dialog.clipboard_append(thread_id)

        ttk.Button(actions, text="复制会话 ID", command=copy_thread_id).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            actions,
            text="用记事本打开原始 JSONL",
            command=lambda: subprocess.Popen(["notepad.exe", str(path)]),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(actions, text=str(path), foreground="#666666").pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def _load_journal_author_live_snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        adapter = self.orchestrator.adapters["journal_author"]
        pipeline = adapter.load_state()
        runtime_status = adapter.runtime.status(check_account=False)
        journal = read_json(JOURNAL_AUTHOR_SDK_JOURNAL_PATH, {"papers": {}})
        journal_papers = journal.get("papers", {}) if isinstance(journal, dict) else {}
        if not isinstance(journal_papers, dict):
            journal_papers = {}
        return pipeline, runtime_status, journal_papers

    def refresh_journal_author_live_tasks(
        self,
        *,
        update_detail: bool = True,
        snapshot: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tree = self.journal_author_task_tree
        if tree is None:
            return {}
        try:
            pipeline, runtime_status, journal_papers = (
                snapshot if snapshot is not None else self._load_journal_author_live_snapshot()
            )
        except Exception as exc:
            blocker = str(getattr(exc, "code", type(exc).__name__))
            if self.journal_author_live_summary_var is not None:
                self.journal_author_live_summary_var.set(f"选刊作者实时状态读取失败：{blocker}")
            return {"ok": False, "blocker": blocker}
        self._journal_author_pipeline_state = pipeline
        self._journal_author_runtime_status = runtime_status
        archived = self.state_data.get("archived_papers", {})
        archived_ids = set(archived) if isinstance(archived, dict) else set()
        if archived_ids:
            journal_papers = {
                paper_id: record
                for paper_id, record in journal_papers.items()
                if str(paper_id) not in archived_ids
            }
        self._journal_author_journal_papers = journal_papers
        rows = _journal_author_task_rows(journal_papers, pipeline)
        self.journal_author_task_rows = {str(row["paper_id"]): row for row in rows}
        visible_rows = self._visible_live_rows(rows)
        _sync_treeview_rows(
            tree,
            [
                _treeview_row(
                    row["paper_id"],
                    compact_paper_label(row["label"]),
                    (
                        row["status_label"],
                        row["step_label"],
                        row["executor_label"],
                        row["progress"],
                        row["session_label"],
                        table_issue_summary(row["blocker_label"]),
                        row["updated_label"],
                    ),
                    (row["tag"],),
                )
                for row in visible_rows
            ],
        )
        self._fit_live_tree_height(tree, len(visible_rows))
        counts = Counter(str(row.get("status") or "unknown") for row in rows)
        active_count = sum(counts.get(value, 0) for value in ("creating_thread", "starting", "running", "waiting_event"))
        waiting_count = counts.get("paused", 0)
        summary = (
            f"实时任务：{len(rows)}｜正在运行：{active_count}｜等待/暂停：{waiting_count}｜"
            f"阻塞：{counts.get('blocked', 0)}｜完成：{counts.get('complete', 0)}｜"
            f"并发上限：{runtime_status.get('max_parallel_workers') or 1}"
        )
        paused_selected = int(runtime_status.get("paused_paper_count") or 0)
        if paused_selected:
            summary += f"｜单篇暂停：{paused_selected}"
        runtime_blocker = str(runtime_status.get("blocker") or "").strip()
        if runtime_blocker:
            summary += f"｜运行时卡点：{runtime_blocker}"
        if self.journal_author_live_summary_var is not None:
            self.journal_author_live_summary_var.set(summary if rows else "当前没有 V2 SDK 选刊作者任务。")
        runtime_label = "运行中" if runtime_status.get("background_running") else "已暂停/空闲"
        self.stage_status_vars["journal_author"].set(
            f"V2 SDK：{runtime_label}｜固定会话：{len(journal_papers)}｜模型：{runtime_status.get('configured_model') or '-'}"
        )
        self.stage_info_vars["journal_author"].set(summary)
        if update_detail and tree.selection():
            self.update_journal_author_task_detail()
        return {"ok": True, "rows": rows, "runtime_status": runtime_status}

    def update_journal_author_task_detail(self) -> None:
        tree = self.journal_author_task_tree
        detail = self.stage_detail_texts.get("journal_author")
        panel = self.stage_detail_panels.get("journal_author")
        if tree is None or detail is None or panel is None or not tree.selection():
            return
        paper_id = str(tree.selection()[0])
        row = self.journal_author_task_rows.get(paper_id)
        if row is None:
            return
        canonical = self.stage_trees.get("journal_author")
        if canonical is not None and canonical.selection():
            canonical.selection_remove(*canonical.selection())
        lines = [
            paper_id,
            "",
            f"实时状态：{row.get('status_label') or '-'}",
            f"当前任务：{row.get('step_label') or '-'}",
            f"总进度：{row.get('progress') or '-'}",
            "执行方式：V2 本地 Codex SDK（程序内部 codex.exe CLI）",
            "会话规则：每篇论文只绑定 1 个固定会话；各 Step 由程序逻辑按冻结流程依次发送",
            f"本任务模型：{row.get('model') or '-'}",
            f"推理强度：{_effort_display(row.get('reasoning_effort'))}",
            f"本任务 Token：{row.get('token_status_label') or '-'}",
            f"流程版本：{row.get('workflow_version_id') or '合并02-03默认版'}",
            f"投稿包状态：{row.get('package_status') or '-'}",
            f"固定会话 ID：{row.get('thread_id') or '尚未创建'}",
            f"当前/最近 Turn ID：{row.get('turn_id') or '-'}",
            f"最后更新：{row.get('updated_label') or '-'}",
        ]
        blocker = str(row.get("blocker") or "").strip()
        friendly = str(row.get("blocker_label") or "").strip()
        if friendly:
            lines.extend(["", f"需要处理：{friendly}"])
        if blocker:
            lines.append(f"技术原因：{blocker}")
        issues = row.get("contract_issues") if isinstance(row.get("contract_issues"), list) else []
        warnings = row.get("contract_warnings") if isinstance(row.get("contract_warnings"), list) else []
        if issues:
            lines.append("产物验收问题：")
            lines.extend(f"- {value}" for value in issues[:8])
        if warnings:
            lines.append("产物验收提醒：")
            lines.extend(f"- {value}" for value in warnings[:8])
        technical = "\n".join(lines)
        self._set_text(detail, technical)
        status = str(row.get("status") or "unknown")
        panel.render(
            title=paper_id,
            status=str(row.get("status_label") or self._detail_state_label(status)),
            tone=self._detail_tone(status),
            headline=friendly or "程序将继续完成选刊、作者信息与投稿包交付。",
            sections=(
                {
                    "title": "当前任务",
                    "fields": (
                        ("步骤", row.get("step_label") or "-"),
                        ("总进度", row.get("progress") or "-"),
                        ("投稿包", row.get("package_status") or "-"),
                        ("最后更新", row.get("updated_label") or "-"),
                    ),
                },
                {
                    "title": "验收与下一步",
                    "body": ([friendly] if friendly else []) + [str(value) for value in issues[:5]]
                    or ["当前没有需要人工处理的验收问题。"],
                    "tone": "danger" if issues else "attention" if friendly else "success",
                },
            ),
            technical_text=technical,
        )
        self._set_technical_details(
            f"选刊与作者 · SDK 任务 {paper_id}",
            {"runtime": self._journal_author_runtime_status, "task": row.get("record", {})},
        )

    def _run_selected_journal_author_mode(self, run_mode: str) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("选刊与作者", "请先选择至少一篇论文。", parent=self)
            return
        labels = {
            "selection": "只重新执行期刊选择（原 Step 2）",
            "package": "只重新生成投稿包（原 Step 3）",
        }
        label = labels.get(run_mode, "执行完整选刊作者流程")
        if not messagebox.askyesno(
            label,
            f"将对 {len(paper_ids)} 篇论文{label}。\n\n该操作会调用 AI；每篇论文继续使用自己的固定 SDK 会话。是否继续？",
            parent=self,
        ):
            return
        try:
            self.state_data = self.orchestrator.run_journal_author_papers(paper_ids, run_mode=run_mode)
        except Exception as exc:
            messagebox.showerror(label, str(exc), parent=self)
            return
        self.refresh()
        self.status_var.set(f"已提交：{label}；共 {len(paper_ids)} 篇。")

    def rerun_selected_journal_selection(self) -> None:
        self._run_selected_journal_author_mode("selection")

    def rerun_selected_journal_package(self) -> None:
        self._run_selected_journal_author_mode("package")

    def revalidate_selected_journal_author(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("仅重新验收", "请先选择至少一篇论文。", parent=self)
            return
        try:
            self.state_data = self.orchestrator.revalidate_journal_author_papers(paper_ids)
        except Exception as exc:
            messagebox.showerror("仅重新验收失败", str(exc), parent=self)
            return
        last = self.state_data.get("last_scheduler_result", {}) if isinstance(self.state_data, dict) else {}
        results = last.get("results", {}) if isinstance(last, dict) else {}
        passed = sum(
            1
            for item in results.values()
            if isinstance(item, dict)
            and item.get("ok")
            and isinstance(item.get("result"), dict)
            and item["result"].get("ok")
        )
        self.refresh()
        messagebox.showinfo(
            "本地重新验收完成",
            f"已检查 {len(paper_ids)} 篇，通过 {passed} 篇。\n\n本操作没有调用 AI，也没有消耗模型 Token。未通过项会直接显示具体产物卡点。",
            parent=self,
        )

    def open_selected_journal_author_artifact(self, kind: str) -> None:
        paper_ids = self.selected_ids()
        if len(paper_ids) != 1:
            messagebox.showinfo("打开选刊作者产物", "请先只选择一篇论文。", parent=self)
            return
        paper = self.orchestrator.adapters["journal_author"].paper_dir(paper_ids[0])
        paths = {
            "journal": paper / "journal.md",
            "pdf": paper / "package" / "main.pdf",
            "package": paper / "package",
        }
        path = paths.get(kind, paper)
        if not path.exists():
            messagebox.showinfo("产物不存在", f"当前还没有生成：\n\n{path}", parent=self)
            return
        self.open_path(path)

    def manage_selected_journal_preferences(self) -> None:
        paper_ids = self.selected_ids()
        if len(paper_ids) != 1:
            messagebox.showinfo("选刊偏好", "请先只选择一篇论文。", parent=self)
            return
        paper_id = paper_ids[0]
        try:
            data = self.orchestrator.journal_preferences(paper_id)
        except Exception as exc:
            messagebox.showerror("读取选刊偏好失败", str(exc), parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"选刊偏好 · {paper_id}")
        dialog.transient(self)
        dialog.grab_set()
        self._fit_dialog_window(
            dialog,
            preferred=(760, 680),
            minimum=(640, 520),
        )
        dialog.columnconfigure(0, weight=1)
        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text=f"论文：{paper_id}", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        quartile_frame = ttk.LabelFrame(body, text="目标分区", padding=7)
        quartile_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        quartiles = data.get("quartiles") if isinstance(data.get("quartiles"), dict) else {}
        quartile_vars = {key: tk.BooleanVar(value=bool(quartiles.get(key))) for key in ("Q1", "Q2", "Q3", "Q4")}
        for index, key in enumerate(("Q1", "Q2", "Q3", "Q4")):
            ttk.Checkbutton(quartile_frame, text=key, variable=quartile_vars[key]).grid(row=0, column=index, padx=8, sticky="w")

        indexing_frame = ttk.LabelFrame(body, text="最低收录要求", padding=7)
        indexing_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        indexing = data.get("indexing") if isinstance(data.get("indexing"), dict) else {}
        indexing_vars = {key: tk.BooleanVar(value=bool(indexing.get(key))) for key in ("SCIE", "ESCI", "Scopus")}
        for index, key in enumerate(("SCIE", "ESCI", "Scopus")):
            ttk.Checkbutton(indexing_frame, text=key, variable=indexing_vars[key]).grid(row=0, column=index, padx=8, sticky="w")

        fields = ttk.Frame(body)
        fields.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        fields.columnconfigure(1, weight=1)
        strategy_var = tk.StringVar(value=str(data.get("strategy") or ""))
        oa_var = tk.StringVar(value=str(data.get("oa_policy") or "subscription_first"))
        max_apc_var = tk.StringVar(value=str(data.get("max_apc") or ""))
        keywords_var = tk.StringVar(value=str(data.get("keywords") or ""))
        preferred_var = tk.StringVar(value=str(data.get("preferred_journals") or ""))
        rejected_var = tk.StringVar(value=str(data.get("rejected_journals") or ""))
        non_oa_var = tk.BooleanVar(value=bool(data.get("non_oa_required", True)))
        rows = (
            ("稳中稿策略", strategy_var),
            ("最高 APC", max_apc_var),
            ("主题关键词", keywords_var),
            ("优先期刊", preferred_var),
            ("排除期刊", rejected_var),
        )
        for row_index, (label, variable) in enumerate(rows):
            ttk.Label(fields, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(fields, textvariable=variable).grid(row=row_index, column=1, sticky="ew", pady=4)
        ttk.Label(fields, text="OA 偏好").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            fields,
            textvariable=oa_var,
            values=("subscription_first", "either", "oa_allowed", "oa_required"),
            state="readonly",
        ).grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(fields, text="必须优先非 OA / 订阅制期刊", variable=non_oa_var).grid(row=6, column=1, sticky="w", pady=4)

        ttk.Label(body, text="补充说明").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 3))
        notes = tk.Text(body, height=7, wrap="word")
        notes.grid(row=5, column=0, columnspan=2, sticky="nsew")
        notes.insert("1.0", str(data.get("notes") or ""))
        body.rowconfigure(5, weight=1)

        def save_preferences() -> None:
            value = {
                "quartiles": {key: variable.get() for key, variable in quartile_vars.items()},
                "indexing": {key: variable.get() for key, variable in indexing_vars.items()},
                "strategy": strategy_var.get(),
                "oa_policy": oa_var.get(),
                "non_oa_required": non_oa_var.get(),
                "max_apc": max_apc_var.get(),
                "keywords": keywords_var.get(),
                "preferred_journals": preferred_var.get(),
                "rejected_journals": rejected_var.get(),
                "notes": notes.get("1.0", "end-1c"),
            }
            try:
                result = self.orchestrator.save_journal_preferences(paper_id, value)
            except Exception as exc:
                messagebox.showerror("保存选刊偏好失败", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.refresh()
            self.status_var.set(f"{paper_id} 的选刊偏好已保存：{result.get('path') or ''}")

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="保存", command=save_preferences).pack(side=tk.RIGHT, padx=4)

    def show_selected_journal_author_sdk_session(self) -> None:
        tree = self.journal_author_task_tree
        if tree is None or not tree.selection():
            messagebox.showinfo("查看 SDK 会话", "请先在实时选刊作者任务表选择一篇论文。", parent=self)
            return
        paper_id = str(tree.selection()[0])
        row = self.journal_author_task_rows.get(paper_id, {})
        thread_id = str(row.get("thread_id") or "").strip()
        if not thread_id:
            messagebox.showinfo("查看 SDK 会话", "该任务尚未创建固定 SDK 会话。", parent=self)
            return
        runtime = self.orchestrator.adapters["journal_author"].runtime
        path = _writing_session_log_path(Path(runtime.codex_home), thread_id)
        if path is None:
            messagebox.showerror(
                "SDK 会话记录不存在",
                f"已记录会话 ID，但没有在本程序独立 CODEX_HOME 找到日志：\n\n{thread_id}",
                parent=self,
            )
            return
        transcript = _writing_session_transcript(path)
        if not transcript.get("ok"):
            messagebox.showerror("SDK 会话读取失败", f"无法读取：\n\n{path}", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"选刊作者 SDK 会话 · {paper_id}")
        self._fit_dialog_window(
            dialog,
            preferred=(1180, 760),
            minimum=(760, 500),
        )
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        header = ttk.Frame(dialog, padding=(10, 10, 10, 6))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            header,
            text=(
                f"论文：{paper_id}\n固定会话 ID：{thread_id}\n"
                f"当前/最近 Turn ID：{row.get('turn_id') or '-'}\n"
                "执行方式：V2 本地 Codex SDK → 内部 codex.exe CLI"
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        body = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        transcript_text = self._readonly_text(body)
        rendered: list[str] = []
        if transcript.get("changing"):
            rendered.extend(["提示：会话仍在写入，以下是读取瞬间的内容。", ""])
        for item in transcript.get("messages", []):
            if not isinstance(item, dict):
                continue
            rendered.extend(
                [
                    f"[{item.get('timestamp') or '-'}] {item.get('role') or '记录'}",
                    str(item.get("text") or ""),
                    "",
                ]
            )
        self._set_text(transcript_text, "\n".join(rendered) if rendered else "该会话目前没有可展示的消息。")
        actions = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        actions.grid(row=2, column=0, sticky="ew")

        def copy_thread_id() -> None:
            dialog.clipboard_clear()
            dialog.clipboard_append(thread_id)

        ttk.Button(actions, text="复制会话 ID", command=copy_thread_id).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            actions,
            text="用记事本打开原始 JSONL",
            command=lambda: subprocess.Popen(["notepad.exe", str(path)]),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(actions, text=str(path), foreground="#666666").pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def _collect_ui_refresh_snapshot(
        self,
        *,
        tab: str,
        full: bool,
        observed_projection_mtime_ns: int,
        preserve_smart_detail: bool = False,
    ) -> dict[str, Any]:
        """Collect filesystem/runtime state without touching Tk widgets."""

        started = time.monotonic()
        payload: dict[str, Any] = {
            "tab": tab,
            "full": bool(full),
            "preserve_smart_detail": bool(preserve_smart_detail),
        }
        try:
            payload["full_pipeline"] = full_pipeline_status()
        except Exception as exc:
            payload["full_pipeline"] = {
                "ok": False,
                "summary": "一键全流程：状态读取失败",
                "last_error": str(exc),
            }
        if full:
            if bool(self._smart_program_timer_reconcile.get("pending")):
                try:
                    payload["program_timer_reconcile"] = (
                        reconcile_smart_submission_program_timer()
                    )
                except Exception as exc:
                    payload["program_timer_reconcile"] = {
                        "ok": False,
                        "pending": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    LOGGER.error(
                        "Failed to reconcile Smart Submission program timer",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            payload["unified_state"] = self.orchestrator.sync()
            try:
                payload["projection_mtime_ns"] = UNIFIED_STATE_PATH.stat().st_mtime_ns
            except OSError:
                payload["projection_mtime_ns"] = 0
            payload["scheduler_status"] = self.orchestrator.scheduler_status()
            try:
                payload["execution_mode"] = str(
                    self.orchestrator.ai_provider_store.load_settings().execution_mode
                    or "unknown"
                )
            except Exception:
                payload["execution_mode"] = "unknown"
        else:
            try:
                mtime_ns = UNIFIED_STATE_PATH.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            payload["projection_mtime_ns"] = mtime_ns
            if mtime_ns > int(observed_projection_mtime_ns or 0):
                projection = read_json(UNIFIED_STATE_PATH, {})
                if isinstance(projection, dict) and isinstance(projection.get("papers"), dict):
                    payload["unified_state"] = projection

        if full or tab == "writing":
            payload["writing"] = self._load_writing_live_snapshot()
        if full or tab == "figures":
            payload["figures"] = self._load_figure_live_snapshot()
        if full or tab == "journal_author":
            payload["journal_author"] = self._load_journal_author_live_snapshot()
        if tab == "smart_submission":
            payload["smart_submission"] = self._load_smart_submission_refresh_snapshot()
        payload["duration_ms"] = int((time.monotonic() - started) * 1000)
        return payload

    def _render_canonical_views(self) -> None:
        self.render_overview_table()
        self.render_stage_tables()
        self.update_queue_panel()
        self.update_log_panel()

    def _apply_ui_refresh_snapshot(self, sequence: int, payload: dict[str, Any]) -> None:
        if sequence < self._refresh_last_applied_sequence:
            return
        self._refresh_last_applied_sequence = sequence
        self._refresh_last_duration_ms = int(payload.get("duration_ms") or 0)
        state = payload.get("unified_state")
        if isinstance(state, dict) and isinstance(state.get("papers"), dict):
            self.state_data = state
        if isinstance(payload.get("scheduler_status"), dict):
            self._scheduler_status_cache = payload["scheduler_status"]
        if isinstance(payload.get("program_timer_reconcile"), dict):
            self._smart_program_timer_reconcile = payload["program_timer_reconcile"]
        if payload.get("execution_mode") is not None:
            self._execution_mode_cache = str(payload.get("execution_mode") or "unknown")
        full_pipeline = payload.get("full_pipeline")
        if isinstance(full_pipeline, dict):
            self.full_pipeline_summary_var.set(
                str(full_pipeline.get("summary") or "一键全流程：未启动")
            )
        try:
            self._unified_projection_mtime_ns = max(
                int(self._unified_projection_mtime_ns or 0),
                int(payload.get("projection_mtime_ns") or 0),
            )
        except (TypeError, ValueError):
            pass

        if payload.get("full") or isinstance(state, dict):
            self._render_canonical_views()
        if isinstance(payload.get("writing"), tuple):
            self.refresh_writing_live_tasks(
                update_detail=self.current_tab_key() == "writing",
                snapshot=payload["writing"],
            )
        if isinstance(payload.get("figures"), tuple):
            self.refresh_figure_live_tasks(
                update_detail=self.current_tab_key() == "figures",
                snapshot=payload["figures"],
            )
        if isinstance(payload.get("journal_author"), tuple):
            self.refresh_journal_author_live_tasks(
                update_detail=self.current_tab_key() == "journal_author",
                snapshot=payload["journal_author"],
            )
        smart_snapshot = payload.get("smart_submission")
        if isinstance(smart_snapshot, dict):
            self.refresh_smart_submission(
                snapshot=smart_snapshot,
                update_detail=not bool(payload.get("preserve_smart_detail")),
            )

    def _merge_pending_refresh(
        self,
        request: tuple[str, bool, str, bool, bool],
    ) -> None:
        pending = self._refresh_pending_request
        if pending is None:
            self._refresh_pending_request = request
            return
        tab, full, reason, announce, preserve_smart_detail = request
        (
            previous_tab,
            previous_full,
            previous_reason,
            previous_announce,
            previous_preserve_smart_detail,
        ) = pending
        self._refresh_pending_request = (
            tab or previous_tab,
            bool(full or previous_full),
            reason or previous_reason,
            bool(announce or previous_announce),
            bool(preserve_smart_detail or previous_preserve_smart_detail),
        )

    def _flush_pending_refresh(self) -> None:
        if self.refresh_runner.active_name:
            return
        request = self._refresh_pending_request
        self._refresh_pending_request = None
        if request is not None:
            tab, full, reason, announce, preserve_smart_detail = request
            self._request_ui_refresh(
                tab=tab,
                full=full,
                reason=reason,
                announce=announce,
                preserve_smart_detail=preserve_smart_detail,
            )

    def _request_ui_refresh(
        self,
        *,
        tab: str | None = None,
        full: bool = False,
        reason: str = "",
        announce: bool = False,
        preserve_smart_detail: bool = False,
    ) -> bool:
        target_tab = str(tab or self.current_tab_key() or "overview")
        request = (
            target_tab,
            bool(full),
            str(reason or ""),
            bool(announce),
            bool(preserve_smart_detail),
        )
        if self.refresh_runner.active_name:
            self._merge_pending_refresh(request)
            return False

        self._refresh_request_sequence += 1
        sequence = self._refresh_request_sequence
        observed_mtime = int(self._unified_projection_mtime_ns or 0)
        if announce:
            self.status_var.set(f"正在后台刷新：{reason or '全部状态'}…")

        def completed(payload: dict[str, Any]) -> None:
            self._apply_ui_refresh_snapshot(sequence, payload)
            if announce:
                self.status_var.set(
                    f"刷新完成｜{len(self.state_data.get('papers', {}))} 篇论文｜"
                    f"{self._refresh_last_duration_ms} ms"
                )
            self.after_idle(self._flush_pending_refresh)

        def failed(exc: BaseException) -> None:
            LOGGER.error(
                "Background UI refresh failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if announce:
                self.status_var.set(f"刷新失败：{type(exc).__name__}")
            self.after_idle(self._flush_pending_refresh)

        return self.refresh_runner.submit(
            f"refresh:{target_tab}:{sequence}",
            lambda: self._collect_ui_refresh_snapshot(
                tab=target_tab,
                full=bool(full),
                observed_projection_mtime_ns=observed_mtime,
                preserve_smart_detail=bool(preserve_smart_detail),
            ),
            completed,
            failed,
        )

    def refresh(self, *, announce: bool = False) -> None:
        """Refresh all projections in the background; never block the Tk loop."""

        self._request_ui_refresh(
            tab=self.current_tab_key(),
            full=True,
            reason="全部状态",
            announce=announce,
        )

    def init_smart_submission(self) -> None:
        result = ensure_smart_submission_module()
        self.refresh_smart_submission()
        created = result.get("created", [])
        if created:
            messagebox.showinfo("智能投稿", "已初始化新模块。\n\n" + "\n".join(str(item) for item in created))
        else:
            messagebox.showinfo("智能投稿", "新模块已存在，已刷新状态。")

    def _load_smart_submission_state(self) -> dict:
        try:
            data = json.loads(Path(SMART_STATE_PATH).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"智能投稿状态文件不可读：{SMART_STATE_PATH}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"智能投稿状态根节点必须是对象：{SMART_STATE_PATH}")
        return data

    @staticmethod
    def _automation_status_label(status: str) -> str:
        token = str(status or "MISSING").strip().upper()
        return AUTOMATION_STATUS_LABELS.get(token, "需核对，详见审计")

    @staticmethod
    def _smart_mode_label(mode: str) -> str:
        return scheduler_mode_label(mode)

    @staticmethod
    def _smart_workflow_label(status: str) -> str:
        token = str(status or "").strip().lower()
        if token == "candidate":
            return "未建立 SDK"
        return SMART_WORKFLOW_LABELS.get(token, "-" if not token else "需核对，详见审计")

    @staticmethod
    def _submission_route_label(record: dict[str, Any]) -> str:
        route = record.get("submission_route", {}) if isinstance(record.get("submission_route"), dict) else {}
        owner = str(route.get("execution_owner") or "legacy_auto")
        label = {
            "legacy": "稳定投稿",
            "legacy_auto": "稳定投稿",
            "smart": "智能投稿",
            "manual": "人工处理",
        }.get(owner, "需核对，详见审计")
        mode = str(route.get("mode") or "production")
        if owner == "smart" and mode == "canary":
            return "智能投稿（试运行）"
        if mode == "shadow":
            return f"{label}（仅检查）"
        return label

    def _smart_status_label(self, status: str) -> str:
        labels = {
            "candidate": "自动候选",
            "waiting_user_approval": "等权限确认",
            "queued": "本地预检队列",
            "ready_for_online_submit": "待在线",
            "login_check": "登录检查",
            "uploading": "上传中",
            "submitted": "已提交",
            "monitoring": "监控中",
            "revision_needed": "需修改",
            "retarget_needed": "需换投",
            "rejected": "已拒稿，待换投",
            "blocked": "阻塞",
            "accepted": "已接收",
            "waiting": "等待",
            "no_action": "无需动作",
            "discover_account": "找账号",
            "discover_email_account": "找邮箱",
            "discover_journal_site": "找站点",
            "manual_takeover": "人工修改中",
        }
        return labels.get(status, "-" if not status else "需核对，详见审计")

    @staticmethod
    def _smart_detail_tone(status: object) -> str:
        token = str(status or "").strip().casefold()
        if token in {"submitted", "monitoring", "accepted", "no_action"}:
            return "success"
        if token in {"queued", "ready_for_online_submit", "login_check", "uploading"}:
            return "info"
        if token in {"waiting", "waiting_user_approval", "revision_needed"}:
            return "attention"
        if token in {"retarget_needed", "rejected"}:
            return "severe"
        if token in {"blocked", "failed"}:
            return "danger"
        return "neutral"

    @staticmethod
    def _smart_credential_label(status: Any) -> str:
        token = str(status or "").strip().casefold()
        labels = {
            "": "尚未开始",
            "starting": "本地代理启动中",
            "resume_requested": "等待本地代理继续",
            "session_ready": "登录完成",
            "broker_retry_required": "等待自动重试",
            "account_required": "需要配置账号",
            "account_recovery_email_pending": "重置邮件已发，自动等待",
            "account_recovery_request_failed": "重置请求待自动重试",
            "account_recovery_link_incomplete": "恢复链接待自动重试",
            "account_recovery_change_failed": "密码更新待自动重试",
            "account_recovery_form_incomplete": "账号恢复页面待处理",
            "account_recovery_mailbox_unavailable": "恢复邮箱不可用",
            "account_support_recovery_required": "旧账号需官方恢复",
            "existing_account_recovery_required": "已有账号，需补密码",
            "saved_password_rejected": "保存的密码无效",
            "orcid_saved_password_rejected": "保存的 ORCID 密码无效",
            "waiting_orcid_authorization": "等待 ORCID 授权",
            "orcid_login_incomplete": "ORCID 登录待处理",
            "orcid_login_control_missing": "未找到 ORCID 登录入口",
            "registration_identity_required": "需补作者姓名/邮箱",
            "registration_form_incomplete": "注册表单待处理",
            "waiting_email_verification": "等待邮箱验证",
            "waiting_terms_confirmation": "等待确认网站条款",
            "human_verification": "等待验证码 / 2FA",
            "login_form_incomplete": "登录页待处理",
            "login_incomplete": "登录未完成",
            "site_adapter_required": "缺少站点适配器",
            "site_binding_invalid": "站点绑定需修复",
        }
        return labels.get(token, "需核对，详见单篇状态")

    def _smart_next_label(self, action: str) -> str:
        labels = {
            "enable_smart_submission": "启用智能投稿",
            "approve_worker_command": "确认执行权限",
            "preflight": "本地预检",
            "browser_login_check": "检查登录",
            "trusted_credential_broker_login": "本地代理自动登录",
            "trusted_credential_broker_auto_register": "本地代理自动注册",
            "dispatch_fixed_sdk_session": "启动固定 SDK 网页会话",
            "retry_credential_broker": "重试本地登录代理",
            "retry_existing_account_recovery": "自动重试账号恢复",
            "contact_inderscience_support_for_account_recovery": "联系 Inderscience 恢复旧账号",
            "repair_recovery_mailbox_then_resume": "修复恢复邮箱后继续",
            "configure_recovery_mailbox_then_resume": "配置恢复邮箱后继续",
            "verify_email_then_resume": "完成邮箱验证后继续",
            "accept_site_terms_then_resume": "确认网站条款后继续",
            "complete_captcha_or_2fa_then_resume": "完成验证码 / 2FA 后继续",
            "update_saved_password_then_resume": "更新保存密码后继续",
            "update_saved_orcid_password_then_resume": "更新 ORCID 密码后继续",
            "authorize_orcid_then_resume": "完成 ORCID 授权后继续",
            "complete_visible_orcid_login_then_resume": "完成可见 ORCID 登录后继续",
            "open_orcid_login_then_resume": "打开 ORCID 登录入口后继续",
            "manual_takeover_edit_current_page": "等待你在当前页面完成修改",
            "reconcile_manual_page_then_continue": "重新检查当前页面并继续",
            "add_existing_account_password_then_resume": "保存已有账号密码后继续",
            "complete_author_info_with_verified_name_email_institution_country_and_contributions": (
                "补齐作者资料 / 作者贡献声明"
            ),
            "visible_worker_online_submit_or_monitor": "等待执行任务",
            FINAL_SUBMIT_REVIEW_READY_ACTION: "最终审核通过，程序将自动提交",
            "check_submission_status": "查投稿状态",
            "check_submission_status_on_verified_journal_dashboard": "检查期刊后台投稿状态",
            "run_discovery_tasks": "自动发现账号/站点",
            "fix_local_blocker": "修本地阻塞",
            "fix_submission_package_gate": "修复投稿包校验",
            "inspect_decision_evidence_before_online_mutation": "核对期刊要求，不执行网页修改",
            "reconcile_active_submission_ids_before_online_action": "关闭重复投稿并确认唯一编号",
            "close_prior_submission_before_retarget_submit": "关闭旧投稿后继续自动转投",
            "withdraw_existing_submission_before_retarget": "撤回现有投稿后自动转投",
            "verify_current_submission_then_withdraw_if_active": "核验旧稿状态并按需撤稿",
            "queue_user_selected_journal_author_retarget": "按用户设置重新选刊并生成投稿包",
            "retry_user_retarget_journal_selection": "重试用户指定的转投流程",
            "rerun_journal_author_for_exact_user_target": "按指定期刊重新生成投稿包",
            "apply_technical_correction_to_existing_submission": "修改现有投稿的技术信息",
            "apply_requested_files_or_metadata_to_existing_submission": "补充现有投稿的文件或元数据",
            "prepare_scientific_revision_package_from_decision": "生成科学修订稿与回复材料",
            "queue_journal_author_retarget_steps_2_3": "自动重新选刊并生成投稿包",
            "wait_journal_author_retarget_package": "等待自动转投投稿包",
            "enable_online_submit_after_site_adapter_review": "启用在线调度",
            "no_action": "无需动作",
        }
        if action in labels:
            return labels[action]
        lowered = str(action or "").strip().casefold()
        if not lowered:
            return "-"
        if "email-first" in lowered or ("email" in lowered and "monitor" in lowered):
            return "优先检查邮箱与投稿状态"
        if "redispatch" in lowered:
            return "等待调度器重新派发"
        if "snapp" in lowered and ("success" in lowered or "evidence" in lowered):
            return "记录投稿成功证据"
        if "approval" in lowered:
            return "等待权限确认"
        if "no queued" in lowered or "no online-ready" in lowered:
            return "当前无待执行在线任务"
        if re.search(r"[\u4e00-\u9fff]", action):
            return action
        return "查看审计说明"

    @staticmethod
    def _smart_message_label(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        lowered = text.casefold()
        if "no queued" in lowered or "no online-ready" in lowered:
            return "当前没有等待派发的在线任务。"
        if "recorded" in lowered and "monitoring" in lowered:
            return "已记录监控结果；当前没有新的在线任务需要派发。"
        if "email-first" in lowered or ("email" in lowered and "monitor" in lowered):
            return "优先通过邮箱和投稿系统核对状态。"
        if "redispatch" in lowered:
            return "当前执行任务需要由调度器重新派发。"
        if "snapp" in lowered and ("success" in lowered or "evidence" in lowered):
            return "需要补充记录投稿成功证据。"
        if re.search(r"[\u4e00-\u9fff]", text):
            return text
        return "详细技术说明已写入审计记录。"

    @staticmethod
    def _smart_monitor_time_label(value: Any) -> str:
        text = str(value or "").strip()
        match = re.match(r"^\d{4}-(\d{2}-\d{2})[T ](\d{2}:\d{2})", text)
        if match:
            return f"{match.group(1)} {match.group(2)}"
        return text[:16] if text else "-"

    @staticmethod
    def _smart_monitor_summary_presentation(status: dict[str, Any]) -> tuple[str, str]:
        run_status = str(status.get("daily_monitor_last_status") or "never_run").strip().casefold()
        started_at = UnifiedControlPanel._smart_monitor_time_label(
            status.get("daily_monitor_last_started_at")
        )
        finished_at = UnifiedControlPanel._smart_monitor_time_label(
            status.get("daily_monitor_last_finished_at")
            or status.get("daily_monitor_last_started_at")
        )
        total = int(status.get("daily_monitor_last_paper_count") or 0)
        checked = int(status.get("daily_monitor_last_checked_count") or 0)
        failed = int(status.get("daily_monitor_last_failed_count") or 0)
        pending = int(status.get("daily_monitor_last_pending_count") or 0)
        actions = int(status.get("daily_monitor_last_action_required_count") or 0)
        auto_queued = int(
            status.get("daily_monitor_last_followup_auto_queued_count") or 0
        )
        revision_waiting = int(
            status.get("daily_monitor_last_followup_manual_revision_count") or 0
        )
        identity_waiting = int(
            status.get("daily_monitor_last_followup_identity_reconciliation_count") or 0
        )
        followup_failed = int(
            status.get("daily_monitor_last_followup_failed_count") or 0
        )

        if run_status == "running":
            target = int(status.get("daily_monitor_candidate_count") or total or 0)
            return (
                f"● 正在检查投稿状态：目标 {target} 篇｜开始 {started_at}",
                "#1d4ed8",
            )
        if run_status in {"", "never_run"}:
            return (
                "○ 尚未检查投稿状态｜点击“立即检查投稿状态”开始",
                "#666666",
            )
        if run_status == "idle":
            return (
                f"✓ 最近检查完成：当前没有需要检查的投稿｜{finished_at}",
                "#237a3b",
            )
        if run_status not in {"completed", "partial", "failed"}:
            return (
                f"⚠ 最近检查状态需核对：{run_status or 'unknown'}｜完成 {checked}/{total}｜{finished_at}",
                "#9a6700",
            )
        if run_status == "failed" or followup_failed:
            return (
                f"✕ 最近检查存在失败：完成 {checked}/{total}｜检查失败 {failed}｜"
                f"后续处理失败 {followup_failed}｜{finished_at}",
                "#b42318",
            )
        if run_status == "partial" or failed or pending:
            return (
                f"⚠ 最近检查未全部完成：完成 {checked}/{total}｜失败 {failed}｜"
                f"待确认 {pending}｜{finished_at}",
                "#9a6700",
            )
        if actions or auto_queued or revision_waiting or identity_waiting:
            return (
                f"⚠ 最近检查发现新状态：完成 {checked}/{total}｜需处理 {actions}｜"
                f"自动处理/排队 {auto_queued}｜待论文修订 {revision_waiting}｜"
                f"投稿编号冲突 {identity_waiting}｜{finished_at}",
                "#9a6700",
            )
        return (
            f"✓ 最近检查正常：完成 {checked}/{total}｜未发现需处理的期刊消息｜{finished_at}",
            "#237a3b",
        )

    @staticmethod
    def _smart_monitor_paper_label(paper: dict[str, Any]) -> str:
        if bool(paper.get("candidate_only")) or str(paper.get("status") or "") == "candidate":
            return "— 尚未启用"
        if paper.get("monitor_check_pending"):
            if str(paper.get("next_action") or "") == "check_submission_status_on_verified_journal_dashboard":
                return "● 检查中｜期刊后台"
            return "● 检查中｜多邮箱"

        outcome = str(paper.get("last_monitor_check_outcome") or "").strip().casefold()
        result = str(paper.get("last_monitor_result") or "").strip().casefold()
        action_kind = str(paper.get("last_monitor_action_kind") or "none").strip().casefold()
        followup = str(paper.get("last_monitor_followup_status") or "").strip().casefold()
        checked_at = paper.get("last_monitor_checked_at")
        attempted_at = paper.get("last_monitor_attempted_at")
        timestamp = UnifiedControlPanel._smart_monitor_time_label(
            checked_at or attempted_at
        )

        if outcome in {"failed", "failure", "skipped", "deferred"}:
            return f"✕ 检查失败 {timestamp}"
        if followup in {"failed", "retarget_package_blocked"}:
            return f"✕ 后续处理失败 {timestamp}"
        if followup == "identity_reconciliation_required" or str(
            paper.get("submission_identity_status") or ""
        ).strip().casefold() in {"multiple_active_submissions", "canonical_submission_mismatch"}:
            return f"⛔ 重复投稿待处理 {timestamp}"
        if followup == "action_evidence_required":
            return f"⚠ 待核对修改要求 {timestamp}"
        if action_kind == "accepted" or result == "accepted" or followup == "accepted_archived":
            return f"✓ 已录用 {timestamp}"
        if followup == "waiting_scientific_revision" or action_kind == "scientific_revision":
            return f"⚠ 待修订稿 {timestamp}"
        if followup in {"online_withdrawal_queued", "online_withdrawal_running"}:
            return f"↻ 正在撤回后转投 {timestamp}"
        if followup in {
            "journal_author_dispatching",
            "journal_author_queued",
            "retarget_package_ready",
        } or action_kind == "retarget":
            return f"↻ 自动转投 {timestamp}"
        if followup in {"online_correction_queued", "online_correction_running"}:
            return f"↻ 技术修改 {timestamp}"
        if followup == "completed" and action_kind != "none":
            return f"✓ 已自动处理 {timestamp}"
        if action_kind != "none" or result in {
            "revision_needed",
            "retarget_needed",
            "rejected",
        }:
            return f"⚠ 需处理 {timestamp}"
        if checked_at:
            return f"✓ 正常 {timestamp}"
        if str(paper.get("status") or "").strip().casefold() in {
            "submitted",
            "monitoring",
            "accepted",
        }:
            return "○ 尚未检查"
        return "— 不适用"

    @staticmethod
    def _smart_monitor_source_lines(paper: dict[str, Any]) -> list[str]:
        values = paper.get("last_monitor_source_results")
        if not isinstance(values, list) or not values:
            return ["检查来源：尚无逐来源记录"]
        status_labels = {
            "monitoring": "无明确新动作",
            "no_action": "无明确新动作",
            "accepted": "已接收",
            "rejected": "已拒稿",
            "retarget_needed": "需要转投",
            "revision_needed": "需要处理/返修",
            "waiting": "暂不可访问",
            "blocked": "检查受阻",
        }
        lines = ["检查来源（本轮）："]
        for raw in values:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("source_label") or raw.get("source_id") or "未知来源")
            status = str(raw.get("status") or "monitoring").strip().casefold()
            availability = "已检查" if raw.get("available") else "不可用"
            result = status_labels.get(status, status or "未知")
            summary = UnifiedControlPanel._smart_message_label(raw.get("summary"))
            error = str(raw.get("error_code") or "").strip()
            detail = summary if summary != "-" else error
            suffix = f"｜{detail}" if detail else ""
            lines.append(f"- {label}：{availability}｜{result}{suffix}")
        return lines

    @staticmethod
    def _set_smart_monitor_banner(
        panel: Any,
        text: str,
        color: str,
    ) -> None:
        variable = getattr(panel, "smart_submission_monitor_result_var", None)
        if variable is not None:
            variable.set(text)
        label = getattr(panel, "smart_submission_monitor_result_label", None)
        if label is not None:
            label.configure(foreground=color)

    @staticmethod
    def _compact_status_label(value: Any) -> str:
        token = str(value or "").strip().casefold()
        return {
            "active": "进行中",
            "accepted": "已接收",
            "blocked": "已阻塞",
            "complete": "已完成",
            "completed": "已完成",
            "missing": "无记录",
            "monitoring": "监控中",
            "no_action": "无需动作",
            "ready": "就绪",
            "submitted": "已投稿",
            "waiting": "等待中",
        }.get(token, "-" if not token else "需核对，详见审计")

    @staticmethod
    def _stage_detail_label(stage: str, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return text
        if stage != "submission":
            return text if re.search(r"[\u4e00-\u9fff]", text) else "需核对，可显示技术信息排查"
        lowered = text.casefold()
        if "submitted successfully" in lowered or "submitted to" in lowered:
            return "已投稿；期刊和确认凭证见单篇详情"
        if "account recovery" in lowered or "invalid_password" in lowered:
            return "需要恢复或核对投稿账号"
        if "technical" in lowered and ("check" in lowered or "review" in lowered):
            return "投稿后技术检查中"
        if re.search(r"[\u4e00-\u9fff]", text):
            return text
        return "详细说明见稳定投稿记录"

    def _smart_row_tag(self, paper: dict, queue_label: str) -> str:
        status = str(paper.get("status") or "")
        workflow = str(paper.get("workflow_status") or "")
        if bool(paper.get("candidate_only")) or status == "candidate":
            return "candidate"
        if status == "blocked":
            return "blocked"
        if status == "waiting_user_approval" or workflow == "waiting_user_approval":
            return "waiting"
        if queue_label or workflow in {"queued", "running"}:
            return "queued"
        if status in {"submitted", "monitoring", "accepted", "no_action"}:
            return "done"
        if status in {"waiting", "revision_needed", "retarget_needed", "rejected"}:
            return "waiting"
        return "ready"

    def _load_smart_submission_refresh_snapshot(self) -> dict[str, Any]:
        """Read and reconcile Smart state without touching Tk widgets."""

        projection_refresh = refresh_smart_submission_projection()
        projection_load_error = ""
        persisted_unified: dict[str, Any] | None = None
        try:
            persisted_unified = self.orchestrator.load_unified_state()
        except Exception as exc:
            projection_load_error = f"{type(exc).__name__}: {exc}"
            LOGGER.error("Unified projection reload failed", exc_info=(type(exc), exc, exc.__traceback__))
        try:
            status = smart_submission_status()
            state = self._load_smart_submission_state()
        except Exception as exc:
            LOGGER.error("Smart Submission state refresh failed", exc_info=(type(exc), exc, exc.__traceback__))
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "projection_refresh": projection_refresh,
                "projection_load_error": projection_load_error,
                "persisted_unified": persisted_unified,
            }
        unified_for_candidates = (
            persisted_unified
            if isinstance(persisted_unified, dict)
            else self.state_data
        )
        candidate_projection = discover_smart_submission_candidates(
            unified_for_candidates,
            state,
        )
        candidates = (
            candidate_projection.get("papers", {})
            if isinstance(candidate_projection.get("papers"), dict)
            else {}
        )
        return {
            "ok": True,
            "projection_refresh": projection_refresh,
            "projection_load_error": projection_load_error,
            "persisted_unified": persisted_unified,
            "status": status,
            "state": state,
            "candidates": candidates,
        }

    def refresh_smart_submission(
        self,
        *,
        snapshot: dict[str, Any] | None = None,
        update_detail: bool = True,
    ) -> None:
        if snapshot is None and callable(getattr(self, "_request_ui_refresh", None)):
            self._request_ui_refresh(
                tab="smart_submission",
                reason="智能投稿状态",
                preserve_smart_detail=not update_detail,
            )
            return
        snapshot = snapshot or UnifiedControlPanel._load_smart_submission_refresh_snapshot(self)
        projection_refresh = (
            snapshot.get("projection_refresh", {})
            if isinstance(snapshot.get("projection_refresh"), dict)
            else {}
        )
        projection_load_error = str(snapshot.get("projection_load_error") or "")
        persisted_unified = snapshot.get("persisted_unified")
        if isinstance(persisted_unified, dict) and (
            projection_refresh.get("refreshed") or persisted_unified != self.state_data
        ):
            self.state_data = persisted_unified
            UnifiedControlPanel._render_canonical_views(self)
        if not snapshot.get("ok"):
            error = str(snapshot.get("error") or "unknown error")
            self.smart_submission_notice_var.set("智能投稿状态不可读，已停止刷新和写入操作。请先修复状态文件。")
            self.smart_submission_status_var.set(f"读取失败：{error}")
            UnifiedControlPanel._set_smart_monitor_banner(
                self,
                "✕ 投稿状态检查结果不可读｜请先修复状态文件",
                "#b42318",
            )
            return
        status = snapshot.get("status", {}) if isinstance(snapshot.get("status"), dict) else {}
        state = snapshot.get("state", {}) if isinstance(snapshot.get("state"), dict) else {}
        candidates = (
            snapshot.get("candidates", {})
            if isinstance(snapshot.get("candidates"), dict)
            else {}
        )
        notice = (
            "自动候选是只读扫描结果，不会切换投稿路线或联网；明确启用后才写入智能投稿账本，"
            "点击“启动智能投稿”后才由固定 Codex SDK 会话执行网页动作。"
        )
        if projection_refresh.get("dirty") or bool(projection_load_error):
            notice += " 统一总览刷新暂未完成；智能投稿结果已保留，将在下次刷新时重试。"
        self.smart_submission_notice_var.set(notice)
        self.smart_submission_state = state
        self.smart_submission_candidates = candidates
        self._smart_submission_status_cache = status
        program_timer = (
            status.get("program_timer", {})
            if isinstance(status.get("program_timer"), dict)
            else {}
        )
        sdk_runtime = (
            status.get("sdk_runtime", {})
            if isinstance(status.get("sdk_runtime"), dict)
            else {}
        )
        smart_papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        monitoring_count = sum(
            1
            for paper in smart_papers.values()
            if isinstance(paper, dict)
            and str(paper.get("status") or "").strip().casefold() in {"submitted", "monitoring"}
        )
        waiting_human = int(status.get("credential_broker_waiting_count") or 0) + sum(
            1
            for paper in smart_papers.values()
            if isinstance(paper, dict)
            and str(paper.get("status") or "").strip().casefold()
            in {"revision_needed", "retarget_needed", "rejected", "blocked"}
        )
        smart_metrics = getattr(self, "smart_metrics", None)
        if smart_metrics is not None:
            smart_metrics.update_values(
                {
                    "enabled": (status.get("paper_count") or 0, f"另有候选 {len(candidates)}"),
                    "online": (status.get("online_queue_count") or 0, "等待网页执行"),
                    "running": (status.get("online_running_count") or 0, "当前占用会话"),
                    "human": (waiting_human, "需要人工介入"),
                    "monitor": (monitoring_count, "投稿后跟踪"),
                }
            )
        self.smart_submission_status_var.set(
            f"{self._smart_mode_label(str(status.get('mode') or ''))}｜"
            f"程序调度 {'运行中' if program_timer.get('enabled') else '已暂停'}｜"
            f"SDK {'运行中' if sdk_runtime.get('running') else '空闲'}｜"
            f"每日检查 {'已启用' if status.get('daily_monitor_enabled') else '已暂停'} "
            f"{status.get('daily_monitor_time_local') or '09:00'}"
        )
        monitor_text, monitor_color = UnifiedControlPanel._smart_monitor_summary_presentation(
            status
        )
        UnifiedControlPanel._set_smart_monitor_banner(
            self,
            monitor_text,
            monitor_color,
        )
        tree = self.smart_submission_tree
        if tree is not None:
            papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
            displayed_papers = dict(candidates)
            displayed_papers.update(papers)
            program_control = (
                status.get("program_control", {})
                if isinstance(status.get("program_control"), dict)
                else {}
            )
            program_papers = (
                program_control.get("papers", {})
                if isinstance(program_control.get("papers"), dict)
                else {}
            )
            sdk_papers = (
                sdk_runtime.get("papers", {})
                if isinstance(sdk_runtime.get("papers"), dict)
                else {}
            )
            online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
            queue_positions = {
                paper_id: str(index + 1)
                for index, paper_id in enumerate(online_queue)
            }
            render_rows: list[dict[str, Any]] = []
            for paper_id, paper in sorted(displayed_papers.items()):
                if not isinstance(paper, dict):
                    continue
                decision = (
                    program_papers.get(str(paper_id), {})
                    if isinstance(program_papers.get(str(paper_id)), dict)
                    else {}
                )
                sdk_record = (
                    sdk_papers.get(str(paper_id), {})
                    if isinstance(sdk_papers.get(str(paper_id)), dict)
                    else {}
                )
                phase_label = str(
                    decision.get("phase_label")
                    or self._smart_status_label(str(paper.get("status") or ""))
                )
                action_label = str(
                    decision.get("action_label")
                    or self._smart_next_label(str(paper.get("next_action") or ""))
                )
                sdk_error_code = str(sdk_record.get("error_code") or "").strip()
                sdk_status = str(sdk_record.get("status") or "").strip().casefold()
                if sdk_error_code and (
                    sdk_status == "failed"
                    or sdk_status.endswith("_failed")
                    or sdk_status.endswith("_deferred")
                ):
                    phase_label = "自动提交未启动"
                    action_label = f"需要处理：{sdk_error_code}"
                queue_label = queue_positions.get(str(paper_id), "")
                target = str(paper.get("target_journal") or paper.get("submission_system") or "")
                if paper.get("candidate_only") and not target:
                    unified_record = (
                        self.state_data.get("papers", {}).get(str(paper_id), {})
                        if isinstance(self.state_data.get("papers"), dict)
                        else {}
                    )
                    target = f"当前：{self._submission_route_label(unified_record)}"
                if not target:
                    legacy = paper.get("legacy_submission_status", {})
                    if isinstance(legacy, dict):
                        target = str(legacy.get("detail") or "")[:120]
                values = (
                    phase_label,
                    self._smart_monitor_paper_label(paper),
                    action_label,
                    self._smart_credential_label(
                        paper.get("credential_broker_status")
                    ),
                    str(decision.get("executor_label") or self._smart_workflow_label(str(paper.get("workflow_status") or ""))),
                    queue_label,
                    target,
                )
                render_rows.append(
                    _treeview_row(
                        paper_id,
                        compact_paper_label(paper_id),
                        values,
                        (self._smart_row_tag(paper, queue_label),),
                    )
                )
            _sync_treeview_rows(tree, render_rows)
            if not tree.selection() and online_queue and tree.exists(online_queue[0]):
                tree.selection_set(online_queue[0])
            if update_detail:
                self.update_smart_submission_detail(status=status)
            self._refresh_smart_context_action()

    def update_smart_submission_detail(self, status: dict | None = None) -> None:
        detail = getattr(self, "smart_submission_text", None)
        panel = getattr(self, "smart_submission_detail_panel", None)
        tree = getattr(self, "smart_submission_tree", None)
        if detail is None or panel is None or tree is None:
            return
        status = status or getattr(self, "_smart_submission_status_cache", {})
        if not status:
            loading = "正在后台读取智能投稿状态；当前界面仍可继续操作。"
            self._set_text(detail, loading)
            panel.render(
                title="智能投稿",
                status="正在读取",
                tone="info",
                headline=loading,
                sections=(),
                technical_text=loading,
            )
            return
        state = getattr(self, "smart_submission_state", {})
        if not state:
            loading = "正在后台读取智能投稿任务账本。"
            self._set_text(detail, loading)
            panel.render(
                title="智能投稿",
                status="正在读取",
                tone="info",
                headline=loading,
                sections=(),
                technical_text=loading,
            )
            return
        selected = [str(item) for item in tree.selection()]
        if not selected:
            automation = status.get("automation", {}) if isinstance(status.get("automation"), dict) else {}
            program_timer = (
                status.get("program_timer", {})
                if isinstance(status.get("program_timer"), dict)
                else {}
            )
            sdk_runtime = (
                status.get("sdk_runtime", {})
                if isinstance(status.get("sdk_runtime"), dict)
                else {}
            )
            program_control = (
                status.get("program_control", {})
                if isinstance(status.get("program_control"), dict)
                else {}
            )
            lines = [
                "智能投稿（程序控制面）",
                "程序负责状态、下一动作、队列和并发槽；Codex SDK 固定会话只执行程序授权的网页动作。",
                "",
                f"运行模式：{self._smart_mode_label(str(status.get('mode') or ''))}",
                f"控制面：{program_control.get('control_plane') or '-'}",
                f"主执行桥：{program_control.get('primary_execution_bridge') or 'codex_sdk_browser_mcp'}",
                f"兼容回退：{program_control.get('compatibility_bridge') or '-'}",
                f"程序选出的在线候选：{len(program_control.get('dispatch_candidates', []))}",
                f"当前可用并发槽：{program_control.get('available_slots', 0)}",
                f"无需模型处理：{program_control.get('program_only_count', 0)}",
                f"确需模型语义处理：{program_control.get('model_required_count', 0)}",
                f"自动发现但尚未启用：{len(getattr(self, 'smart_submission_candidates', {}))}",
                f"本地队列：{status.get('queue_count')}",
                f"在线队列：{status.get('online_queue_count')}",
                f"待在线：{status.get('ready_for_online_count')}",
                f"本地登录代理处理中：{status.get('credential_broker_active_count') or 0}",
                f"登录/验证待人工：{status.get('credential_broker_waiting_count') or 0}",
                f"待补充账号或站点：{status.get('pending_discovery_count')}",
                f"账号：{status.get('configured_account_count')}/{status.get('account_count')}",
                f"SDK 后台：{'运行中' if sdk_runtime.get('running') else '空闲'}",
                f"SDK 模型：{sdk_runtime.get('configured_model') or '-'}",
                f"固定论文会话：{sdk_runtime.get('fixed_session_count') or 0}",
                (
                    f"每日状态检查：{'已启用' if status.get('daily_monitor_enabled') else '已暂停'}；"
                    f"每天 {status.get('daily_monitor_time_local') or '09:00'}（本机时间）"
                ),
                f"下一次状态检查：{status.get('next_monitor_at') or '-'}",
                (
                    "本机定时任务："
                    + (
                        "已注册"
                        if status.get("native_monitor_task_registered")
                        else "未注册；当前由主窗口内定时器兜底"
                    )
                ),
                (
                    "程序定时器："
                    f"{'已启用' if program_timer.get('enabled') else '已暂停'}；"
                    f"唯一命令 {program_timer.get('command') or '-'}；"
                    "窗口内与 Windows 任务共享同一每日领取锁"
                ),
                (
                    f"上次状态检查：{status.get('daily_monitor_last_finished_at') or status.get('daily_monitor_last_started_at') or '-'}；"
                    f"{status.get('daily_monitor_last_status') or 'never_run'}；"
                    f"完成 {status.get('daily_monitor_last_checked_count') or 0}/"
                    f"{status.get('daily_monitor_last_paper_count') or 0} 篇；"
                    f"失败 {status.get('daily_monitor_last_failed_count') or 0}；"
                    f"需处理 {status.get('daily_monitor_last_action_required_count') or 0}"
                    f"；自动排队 {status.get('daily_monitor_last_followup_auto_queued_count') or 0}"
                    f"；待论文修订 {status.get('daily_monitor_last_followup_manual_revision_count') or 0}"
                    f"；投稿编号冲突 {status.get('daily_monitor_last_followup_identity_reconciliation_count') or 0}"
                ),
                (
                    "历史 Codex Automation："
                    + (
                        "已停用"
                        if program_timer.get("legacy_automation_retired")
                        else self._automation_status_label(
                            str(automation.get("status") or "MISSING")
                        )
                    )
                ),
                f"当前说明：{self._smart_message_label(status.get('processing_note'))}",
                "",
                "选择左侧论文查看单篇投稿状态。"
            ]
            technical = "\n".join(lines)
            self._set_text(detail, technical)
            run_status = str(status.get("mode") or "").strip().casefold()
            monitor_status = str(status.get("daily_monitor_last_status") or "never_run")
            panel.render(
                title="智能投稿运行概览",
                status=self._smart_mode_label(run_status),
                tone="info" if program_timer.get("enabled") else "attention",
                headline="选择左侧论文后，这里会优先显示当前状态、下一动作和是否需要人工处理。",
                sections=(
                    {
                        "title": "调度与队列",
                        "fields": (
                            ("已启用论文", status.get("paper_count") or 0),
                            ("在线队列", status.get("online_queue_count") or 0),
                            ("执行中", status.get("online_running_count") or 0),
                            ("可用并发槽", program_control.get("available_slots") or 0),
                        ),
                    },
                    {
                        "title": "状态监控",
                        "fields": (
                            ("每日检查", f"每天 {status.get('daily_monitor_time_local') or '09:00'}"),
                            ("最近结果", monitor_status),
                            ("最近完成", status.get("daily_monitor_last_finished_at") or "尚未执行"),
                            ("需要处理", status.get("daily_monitor_last_action_required_count") or 0),
                        ),
                    },
                    {
                        "title": "系统职责",
                        "body": (
                            "程序负责状态、队列、并发和下一动作。",
                            "Codex SDK 只执行程序明确授权的网页任务。",
                        ),
                        "tone": "success",
                    },
                ),
                technical_text=technical,
            )
            return
        paper_id = selected[0]
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        paper = papers.get(paper_id, {}) if isinstance(papers.get(paper_id), dict) else {}
        candidates = getattr(self, "smart_submission_candidates", {})
        candidate = (
            candidates.get(paper_id, {})
            if isinstance(candidates, dict)
            and isinstance(candidates.get(paper_id), dict)
            else {}
        )
        if not paper and candidate:
            paper = candidate
        unified_record = self.state_data.get("papers", {}).get(paper_id, {})
        if bool(paper.get("candidate_only")):
            lines = [
                f"论文：{paper_id}",
                f"标题：{paper.get('title') or '-'}",
                "",
                "智能投稿状态：自动候选（尚未启用）",
                f"当前投稿路线：{self._submission_route_label(unified_record)}",
                "选刊作者阶段：已完成并通过投稿包验收",
                f"投稿包：{paper.get('source_package_dir') or '-'}",
                "",
                "这是程序自动扫描得到的只读候选记录：",
                "- 尚未写入智能投稿任务账本",
                "- 尚未建立 Codex SDK 会话",
                "- 不占用并发槽，也不会自动访问投稿网站",
                "",
                "可执行操作：",
                "- 点击“启用并本地检查”：切换到智能投稿路线并做离线预检，不联网",
                "- 点击“启动智能投稿”：切换路线、预检并加入 SDK 在线队列",
                "- 不做任何操作：继续保留当前稳定投稿或人工路线",
            ]
            technical = "\n".join(lines)
            self._set_text(detail, technical)
            panel.render(
                title=paper_id,
                status="自动候选",
                tone="neutral",
                headline="该论文已通过选刊作者交付，但尚未启用智能投稿，也不会访问投稿网站。",
                sections=(
                    {
                        "title": "候选信息",
                        "fields": (
                            ("论文标题", paper.get("title") or "-"),
                            ("当前路线", self._submission_route_label(unified_record)),
                            ("投稿包", paper.get("source_package_dir") or "-"),
                        ),
                    },
                    {
                        "title": "可执行动作",
                        "body": (
                            "点击“启动/继续智能投稿”可启用、预检并加入在线队列。",
                            "如暂不处理，论文会继续保留当前稳定投稿或人工路线。",
                        ),
                        "tone": "info",
                    },
                ),
                technical_text=technical,
            )
            return
        program_control = (
            status.get("program_control", {})
            if isinstance(status.get("program_control"), dict)
            else {}
        )
        program_papers = (
            program_control.get("papers", {})
            if isinstance(program_control.get("papers"), dict)
            else {}
        )
        decision = (
            program_papers.get(paper_id, {})
            if isinstance(program_papers.get(paper_id), dict)
            else {}
        )
        online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
        queue_label = str(online_queue.index(paper_id) + 1) if paper_id in online_queue else "-"
        legacy = paper.get("legacy_submission_status", {}) if isinstance(paper.get("legacy_submission_status"), dict) else {}
        warnings = paper.get("warnings", []) if isinstance(paper.get("warnings"), list) else []
        discovery = paper.get("discovery_tasks", []) if isinstance(paper.get("discovery_tasks"), list) else []
        sdk_runtime = (
            status.get("sdk_runtime", {})
            if isinstance(status.get("sdk_runtime"), dict)
            else {}
        )
        sdk_papers = (
            sdk_runtime.get("papers", {})
            if isinstance(sdk_runtime.get("papers"), dict)
            else {}
        )
        sdk_record = (
            sdk_papers.get(paper_id, {})
            if isinstance(sdk_papers.get(paper_id), dict)
            else {}
        )
        retarget_request = (
            paper.get("retarget_request")
            if isinstance(paper.get("retarget_request"), dict)
            else {}
        )
        retarget_mode_label = {
            "specific": "指定期刊",
            "ai": "AI 智能选刊",
        }.get(str(retarget_request.get("mode") or ""), "-")
        retarget_tier_label = {
            "sci": "SCI/SCIE",
            "chinese_core": "中文核心",
            "general": "普刊",
        }.get(str(retarget_request.get("publication_tier") or ""), "-")
        retarget_priority_label = {
            "acceptance": "中稿优先",
            "speed": "速度优先",
            "quality": "质量优先",
            "balanced": "均衡",
        }.get(str(retarget_request.get("priority") or ""), "-")
        withdrawal_policy_label = {
            "auto": "程序自动核验并决定",
            "force": "强制先撤稿",
            "confirmed_closed": "用户确认已关闭 / 无需撤稿",
        }.get(str(retarget_request.get("withdrawal_policy") or ""), "-")
        manual_takeover = (
            paper.get("manual_takeover")
            if isinstance(paper.get("manual_takeover"), dict)
            else {}
        )
        manual_page = (
            manual_takeover.get("page")
            if isinstance(manual_takeover.get("page"), dict)
            else {}
        )
        lines = [
            f"论文：{paper_id}",
            f"标题：{paper.get('title') or '-'}",
            f"当前投稿路线：{self._submission_route_label(unified_record)}",
            f"程序状态：{decision.get('phase_label') or '-'}",
            f"当前动作：{decision.get('action_label') or '-'}",
            f"指定执行器：{decision.get('executor_label') or '-'}",
            f"备用执行器：{decision.get('fallback_executor_label') or '-'}",
            f"是否需要模型：{'是' if decision.get('requires_model') else '否'}",
            f"程序判断依据：{decision.get('reason') or '-'}",
            "",
            f"投稿状态：{self._smart_status_label(str(paper.get('status') or ''))}",
            f"执行状态：{self._smart_workflow_label(str(paper.get('workflow_status') or ''))}",
            f"在线队列：{queue_label}",
            f"下一步：{self._smart_next_label(str(paper.get('next_action') or ''))}",
            f"人工修改：{manual_takeover.get('status') or '未启用'}",
            f"人工修改页面：{manual_page.get('title') or manual_page.get('url') or '-'}",
            f"人工修改开始：{manual_takeover.get('activated_at') or manual_takeover.get('requested_at') or '-'}",
            "",
            f"目标期刊：{paper.get('target_journal') or '-'}",
            f"投稿系统：{paper.get('submission_system') or '-'}",
            f"投稿身份状态：{paper.get('submission_identity_status') or '-'}",
            f"当前唯一投稿编号：{paper.get('canonical_submission_id') or paper.get('submission_id') or '-'}",
            "仍活跃的投稿编号："
            + (
                ("、".join(str(item) for item in paper.get("active_submission_ids", []) if str(item).strip()) or "-")
                if isinstance(paper.get("active_submission_ids"), list)
                else "-"
            ),
            "重复投稿编号："
            + (
                ("、".join(str(item) for item in paper.get("duplicate_submission_ids", []) if str(item).strip()) or "-")
                if isinstance(paper.get("duplicate_submission_ids"), list)
                else "-"
            ),
            "",
            f"转投请求：{retarget_request.get('status') or '无'}",
            f"转投方式：{retarget_mode_label}",
            f"指定目标：{retarget_request.get('target_journal') or '-'}",
            f"期刊范围：{retarget_tier_label}",
            f"转投优先级：{retarget_priority_label}",
            f"撤稿策略：{withdrawal_policy_label}",
            "待核验 / 撤回编号："
            + (
                "、".join(
                    str(item)
                    for item in retarget_request.get("withdrawal_targets", [])
                    if str(item).strip()
                )
                or "-"
            ),
            f"账号绑定：{paper.get('credential_account_id') or '尚未绑定；可按策略自动注册'}",
            f"本地凭据代理：{self._smart_credential_label(paper.get('credential_broker_status'))}",
            f"凭据代理状态码：{paper.get('credential_broker_code') or '-'}",
            f"凭据代理卡点阶段：{paper.get('credential_broker_failure_stage') or '-'}",
            f"凭据代理错误类型：{paper.get('credential_broker_error_type') or '-'}",
            f"账号恢复邮件请求时间：{paper.get('credential_recovery_requested_at') or '-'}",
            f"当前登录域名：{paper.get('credential_broker_origin') or '-'}",
            "密码执行位置：本程序内本地可信代理直接填入可见浏览器；不会进入 Codex SDK 提示词或会话记录",
            "",
            f"投稿固定 SDK 会话：{paper.get('sdk_thread_id') or '未建立'}",
            f"轻量巡检会话：{paper.get('sdk_monitor_thread_id') or '尚未运行'}（每次巡检自动轮换）",
            f"当前 SDK 通道：{'日常巡检' if paper.get('sdk_active_channel') == 'monitor' else '投稿执行'}",
            f"当前/最近 Turn：{(paper.get('sdk_monitor_turn_id') or '-') if paper.get('sdk_active_channel') == 'monitor' else (paper.get('sdk_turn_id') or sdk_record.get('turn_id') or '-')}",
            f"SDK 状态：{(paper.get('sdk_monitor_runtime_status') or '-') if paper.get('sdk_active_channel') == 'monitor' else (paper.get('sdk_runtime_status') or sdk_record.get('status') or '-')}",
            f"模型：{(paper.get('sdk_monitor_model') or '-') if paper.get('sdk_active_channel') == 'monitor' else (paper.get('sdk_model') or sdk_record.get('model') or '-')}",
            f"Token：{(paper.get('sdk_monitor_last_usage') or '-') if paper.get('sdk_active_channel') == 'monitor' else (paper.get('sdk_last_usage') or sdk_record.get('usage') or '-')}",
            f"SDK 错误码：{sdk_record.get('error_code') or '-'}",
            f"SDK 错误：{self._smart_message_label(sdk_record.get('error'))}",
            "",
            f"卡点：{self._smart_message_label(paper.get('blocker'))}",
            f"说明：{self._smart_message_label(paper.get('detail'))}",
            f"上次检查：{paper.get('last_checked_at') or '-'}",
            (
                "定时状态检查："
                + ("已排队 / 执行中" if paper.get("monitor_check_pending") else "当前无待执行检查")
            ),
            f"上次状态巡检：{paper.get('last_monitor_checked_at') or '-'}",
            f"上次巡检结果：{self._smart_status_label(str(paper.get('last_monitor_result') or ''))}",
            f"状态动作类型：{paper.get('last_monitor_action_kind') or 'none'}",
            f"多来源检查摘要：{self._smart_message_label((paper.get('last_monitor_observation') or {}).get('summary') if isinstance(paper.get('last_monitor_observation'), dict) else '')}",
            f"自动后续状态：{paper.get('last_monitor_followup_status') or '-'}",
            f"自动后续动作：{paper.get('next_action') or '-'}",
            f"自动后续说明：{self._smart_message_label(paper.get('last_monitor_followup_detail'))}",
            "",
            "稳定投稿记录（只读对照）：",
            f"- 状态：{self._compact_status_label(legacy.get('status'))}",
            f"- 说明：{self._smart_message_label(legacy.get('detail'))}",
            f"- 更新时间：{legacy.get('updated_at') or '-'}",
        ]
        lines.extend(["", *self._smart_monitor_source_lines(paper)])
        if warnings:
            lines.extend(["", f"警告：{len(warnings)} 条；具体内容请查看审计记录。"])
        if discovery:
            lines.extend(["", f"待补充账号或站点信息：{len(discovery)} 项。"])
        technical = "\n".join(lines)
        self._set_text(detail, technical)
        paper_status = str(paper.get("status") or "")
        blocker_label = self._smart_message_label(paper.get("blocker"))
        detail_label = self._smart_message_label(paper.get("detail"))
        next_label = self._smart_next_label(str(paper.get("next_action") or ""))
        duplicate_ids = (
            [str(item) for item in paper.get("duplicate_submission_ids", []) if str(item).strip()]
            if isinstance(paper.get("duplicate_submission_ids"), list)
            else []
        )
        action_body = []
        if blocker_label != "-":
            action_body.append(blocker_label)
        if detail_label != "-" and detail_label not in action_body:
            action_body.append(detail_label)
        if duplicate_ids:
            action_body.append("检测到多个投稿编号，必须先确认唯一有效投稿，避免重复提交。")
        if not action_body:
            action_body.append(f"下一步：{next_label}")
        events = []
        if paper.get("last_monitor_checked_at"):
            events.append(
                f"{self._smart_monitor_time_label(paper.get('last_monitor_checked_at'))}  "
                f"状态检查：{self._smart_status_label(str(paper.get('last_monitor_result') or ''))}"
            )
        if paper.get("last_monitor_followup_status"):
            events.append(f"自动后续：{paper.get('last_monitor_followup_status')}")
        panel.render(
            title=paper_id,
            status=self._smart_status_label(paper_status),
            tone=self._smart_detail_tone(paper_status),
            headline=f"下一步：{next_label}",
            sections=(
                {
                    "title": "当前状态",
                    "fields": (
                        ("论文标题", paper.get("title") or "-"),
                        ("投稿状态", self._smart_status_label(paper_status)),
                        ("最近检查", self._smart_monitor_paper_label(paper)),
                        ("在线队列", queue_label),
                    ),
                },
                {
                    "title": "目标期刊与投稿",
                    "fields": (
                        ("目标期刊", paper.get("target_journal") or "-"),
                        ("投稿系统", paper.get("submission_system") or "-"),
                        ("投稿编号", paper.get("canonical_submission_id") or paper.get("submission_id") or "-"),
                        ("投稿身份", paper.get("submission_identity_status") or "-"),
                    ),
                },
                {
                    "title": "账号与网页状态",
                    "fields": (
                        ("账号", paper.get("credential_account_id") or "尚未绑定"),
                        ("登录状态", self._smart_credential_label(paper.get("credential_broker_status"))),
                        ("当前站点", paper.get("credential_broker_origin") or "-"),
                        ("人工修改", manual_takeover.get("status") or "未启用"),
                    ),
                },
                {
                    "title": "下一动作与关键卡点",
                    "body": action_body,
                    "tone": "danger" if blocker_label != "-" or duplicate_ids else "attention" if paper_status in {"waiting", "revision_needed", "retarget_needed", "rejected"} else "info",
                },
            ),
            events=events,
            technical_text=technical,
        )

    def import_to_smart_submission(self) -> None:
        selected = self._selected_or_remembered_paper_ids()
        if not selected:
            messagebox.showinfo(
                "未选择",
                "请先在总览、稳定投稿或智能投稿页选择论文。智能投稿不会默认批量导入全部论文。",
                parent=self,
            )
            return
        scope = f"选中的 {len(selected)} 篇"
        if not messagebox.askyesno(
            "启用智能投稿",
            f"确认对{scope}启用智能投稿吗？\n\n"
            "程序会把投稿路线切换为“智能投稿（试运行）”，并写入智能投稿任务账本。\n"
            "本操作不执行本地预检、不建立 SDK 会话、不访问投稿网站。",
            parent=self,
        ):
            return
        result = enable_smart_submission_candidates(
            selected,
            orchestrator=self.orchestrator,
        )
        self.state_data = self.orchestrator.sync()
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "智能投稿启用结果",
            result,
        )

    def request_smart_submission_run(self) -> None:
        selected = self._selected_or_remembered_paper_ids()
        if not selected:
            messagebox.showinfo(
                "未选择",
                "请先在总览、稳定投稿或智能投稿页选择要检查的论文。",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "启用并本地检查",
            f"确认启用并检查选中的 {len(selected)} 篇论文吗？\n\n"
            "程序会切换到智能投稿路线、写入任务账本并执行本地投稿包预检。\n"
            "不会建立在线 SDK 任务，也不会访问投稿网站。",
            parent=self,
        ):
            return

        def work() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            enrollment = enable_smart_submission_candidates(
                selected,
                orchestrator=self.orchestrator,
            )
            enabled_ids = [
                str(item)
                for item in enrollment.get("enabled", [])
                if str(item).strip()
            ]
            unified_state = self.orchestrator.sync()
            result = (
                smart_submission_batch_check(
                    unified_state=unified_state,
                    selected_ids=enabled_ids,
                    queue_online=False,
                    activate_online=False,
                )
                if enabled_ids
                else {
                    "status": "not_run",
                    "message": "没有论文成功启用，未执行本地预检。",
                }
            )
            return self.orchestrator.sync(), enrollment, result

        def done(payload: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> None:
            self.state_data, enrollment, result = payload
            self.refresh_smart_submission(update_detail=False)
            self.status_var.set("后台任务完成：智能投稿本地预检")
            self._show_operation_result(
                self.smart_submission_text,
                "智能投稿本地预检完成",
                {
                    "enrollment": enrollment,
                    "local_check": {
                        k: v for k, v in result.items() if k != "state"
                    },
                },
            )

        self._run_background_task("智能投稿本地预检", work, done)

    def build_smart_submission_program_plan(self) -> None:
        """Refresh the deterministic plan without starting online workers."""

        selected = self._selected_or_remembered_paper_ids()

        def work() -> dict[str, Any]:
            return smart_submission_program_plan(
                selected_ids=selected or None,
                persist=True,
                sync_ledger=True,
            )

        def done(result: dict[str, Any]) -> None:
            self.refresh_smart_submission(update_detail=False)
            candidates = result.get("dispatch_candidate_ids", [])
            self.status_var.set("后台任务完成：智能投稿程序计划")
            self._show_operation_result(
                self.smart_submission_text,
                "智能投稿程序计划已更新",
                result,
                summary=(
                    f"控制面：{result.get('control_plane') or '-'}\n"
                    f"程序状态：{result.get('paper_count') or 0} 篇\n"
                    f"当前并发槽：{result.get('available_slots') or 0}\n"
                    f"精确在线候选：{len(candidates)} 篇\n"
                    f"无需模型处理：{result.get('program_only_count') or 0} 篇\n"
                    "本操作没有启动浏览器、模型或最终提交。"
                ),
            )

        self._run_background_task("生成智能投稿程序计划", work, done)

    def run_smart_submission_local_once(self) -> None:
        if not messagebox.askyesno(
            "本地执行智能投稿",
            "确认本地执行一轮智能投稿预检吗？\n\n"
            "它会更新新模块自己的状态和审计记录，不会打开浏览器最终提交，也不会改稳定投稿模块。",
        ):
            return

        def done(result: dict[str, Any]) -> None:
            self.refresh_smart_submission(update_detail=False)
            self.status_var.set("后台任务完成：智能投稿本地执行")
            self._show_operation_result(self.smart_submission_text, "智能投稿本地检查完成", result)

        self._run_background_task(
            "智能投稿本地执行",
            lambda: run_smart_submission_scheduler(force=True),
            done,
        )

    def run_smart_submission_monitor_now(self) -> None:
        status = smart_submission_status()
        candidate_count = int(status.get("daily_monitor_candidate_count") or 0)
        if candidate_count <= 0:
            messagebox.showinfo(
                "立即检查投稿状态",
                "当前没有已经投稿且具备期刊后台入口的论文需要检查。",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "立即检查投稿状态",
            (
                f"确认立即检查 {candidate_count} 篇已投稿论文吗？\n\n"
                "这会跳过今日一次限制：程序先检查账号库中启用的学校邮箱和 Gmail 投稿通知，"
                "没有明确结论时再核对对应期刊投稿后台；SDK 只接收脱敏后的限定结果。"
                "程序会逐篇、逐来源核对，遗漏或失败会明确显示并保留重试入口。"
                "发现接收会自动归档；退稿会自动进入重新选刊；明确的技术、文件或元数据问题会在原投稿编号下排队修复并重投。"
            ),
            parent=self,
        ):
            return

        def done(result: dict[str, Any]) -> None:
            self.refresh_smart_submission(update_detail=False)
            run_summary = result.get("summary", {})
            if not isinstance(run_summary, dict):
                run_summary = {}
            requested = int(
                run_summary.get("requested_count")
                or len(result.get("monitor_check_ids", []))
            )
            checked = int(run_summary.get("checked_count") or 0)
            failed = int(run_summary.get("failed_count") or 0)
            pending = int(run_summary.get("pending_count") or 0)
            action_required = int(run_summary.get("action_required_count") or 0)
            followup_auto = int(run_summary.get("followup_auto_queued_count") or 0)
            followup_manual = int(run_summary.get("followup_manual_revision_count") or 0)
            followup_identity = int(
                run_summary.get("followup_identity_reconciliation_count") or 0
            )
            followup_failed = int(run_summary.get("followup_failed_count") or 0)
            sdk_result = result.get("sdk", {})
            if not isinstance(sdk_result, dict):
                sdk_result = {}
            batch = sdk_result.get("mailbox_monitor_batch", {})
            if not isinstance(batch, dict):
                batch = {}
            self.status_var.set(
                f"投稿状态检查：成功 {checked}/{requested}｜失败 {failed}｜"
                f"发现动作 {action_required}｜已自动排队 {followup_auto}｜需论文修订 {followup_manual}"
            )
            self._show_operation_result(
                self.smart_submission_text,
                "投稿状态立即检查结果",
                result,
                summary=(
                    f"请求检查：{requested} 篇\n"
                    f"确认完成：{checked} 篇\n"
                    f"失败 / 未落账：{failed} / {pending} 篇\n"
                    f"发现需处理状态：{action_required} 篇\n"
                    f"已自动处理 / 排队：{followup_auto} 篇\n"
                    f"需生成修订稿后继续：{followup_manual} 篇\n"
                    f"投稿编号冲突待处理：{followup_identity} 篇\n"
                    f"后续编排失败：{followup_failed} 篇\n"
                    f"运行状态：{result.get('status') or '-'}\n"
                    f"批量会话：{batch.get('thread_id') or '-'}\n"
                    f"批量 Turn：{batch.get('turn_id') or '-'}\n"
                    "详细结果、各邮箱/期刊后台状态和后续动作已逐篇写回状态与 SDK 审计记录。"
                ),
            )

        self._run_background_task(
            "立即检查投稿状态",
            lambda: run_smart_submission_daily_monitoring(
                force=True,
                trigger="manual_ui",
            ),
            done,
        )
        if self.task_runner.active_name == "立即检查投稿状态":
            self._set_smart_monitor_banner(
                self,
                f"● 正在启动投稿状态检查：目标 {candidate_count} 篇……",
                "#1d4ed8",
            )

    def _poll_due_smart_submission_monitor(self) -> None:
        current_monotonic = time.monotonic()
        if current_monotonic < self._smart_monitor_next_poll_at:
            return
        self._smart_monitor_next_poll_at = current_monotonic + 15.0
        if current_monotonic >= self._smart_credential_retry_next_poll_at:
            def retry_pending_credentials() -> dict[str, Any]:
                state = self._load_smart_submission_state()
                papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
                online = {
                    str(item)
                    for item in state.get("online_queue", [])
                    if str(item).strip()
                }
                retry_ids = [
                    str(paper_id)
                    for paper_id, paper in papers.items()
                    if str(paper_id) in online
                    and isinstance(paper, dict)
                    and str(paper.get("credential_broker_status") or "").casefold()
                    == "account_recovery_email_pending"
                ]
                runtime = smart_submission_sdk_status()
                resumed_ids: list[str] = []
                if retry_ids and not runtime.get("running"):
                    resumed = resume_smart_credential_broker(retry_ids)
                    resumed_ids = [
                        str(item)
                        for item in resumed.get("resumed", [])
                        if str(item).strip()
                    ]
                    if resumed_ids:
                        start_smart_submission_program_runtime(resumed_ids)
                return {"resumed_ids": resumed_ids}

            def retry_done(result: dict[str, Any]) -> None:
                resumed_ids = result.get("resumed_ids", [])
                if resumed_ids:
                    self.status_var.set(f"账号恢复邮件自动复查：{len(resumed_ids)} 篇")
                    self._request_ui_refresh(
                        tab="smart_submission",
                        reason="账号恢复邮件复查",
                    )

            def retry_failed(exc: BaseException) -> None:
                LOGGER.error(
                    "Automatic Smart credential recovery retry failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

            submitted = self.maintenance_runner.submit(
                "智能投稿账号恢复复查",
                retry_pending_credentials,
                retry_done,
                retry_failed,
            )
            self._smart_credential_retry_next_poll_at = current_monotonic + (
                90.0 if submitted else 15.0
            )
        due = smart_submission_daily_monitor_due()
        if not due.get("due"):
            return

        def done(result: dict[str, Any]) -> None:
            self.refresh_smart_submission()
            run_summary = result.get("summary", {})
            checked = (
                int(run_summary.get("checked_count") or 0)
                if isinstance(run_summary, dict)
                else 0
            )
            failed = (
                int(run_summary.get("failed_count") or 0)
                if isinstance(run_summary, dict)
                else 0
            )
            self.status_var.set(
                f"每日投稿状态检查：完成 {checked} 篇｜失败 {failed} 篇"
            )

        def failed(exc: BaseException) -> None:
            LOGGER.error(
                "Scheduled Smart Submission monitor failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self.status_var.set(f"每日投稿状态检查失败：{exc}")

        if self.task_runner.submit(
            "每日投稿状态检查",
            lambda: run_smart_submission_daily_monitoring(
                force=False,
                trigger="in_app_schedule",
            ),
            done,
            failed,
        ):
            self.status_var.set(
                f"每日投稿状态检查运行中：{due.get('time_local') or '-'}"
            )

    def prioritize_smart_submission_selected(self) -> None:
        selected = self.selected_ids()
        if not selected:
            messagebox.showwarning("智能投稿", "请先选择一篇或多篇论文。")
            return
        result = prioritize_online_queue(selected, activate=False)
        sdk_start = start_smart_submission_program_runtime(selected)
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "智能投稿队列优先级已更新",
            {
                "queue": {k: v for k, v in result.items() if k != "state"},
                "sdk_start": sdk_start,
            },
            summary="选中的论文已移到在线队列前部，并加入 SDK 程序调度。",
        )

    def resume_smart_submission_login(self) -> None:
        selected = self.selected_ids()
        if not selected:
            messagebox.showinfo(
                "继续登录 / 验证",
                "请先选择需要继续登录、注册、邮箱验证、验证码或条款确认的论文。",
                parent=self,
            )
            return
        resumed = resume_smart_credential_broker(selected)
        paper_ids = [
            str(item)
            for item in resumed.get("resumed", [])
            if str(item).strip()
        ]
        sdk_start = (
            start_smart_submission_program_runtime(paper_ids)
            if paper_ids
            else {"ok": False, "started": False}
        )
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "本地登录代理恢复结果",
            {
                "resume": {
                    key: value
                    for key, value in resumed.items()
                    if key != "state"
                },
                "sdk_start": sdk_start,
            },
            summary=(
                f"已继续：{len(paper_ids)} 篇\n"
                f"未继续：{len(resumed.get('skipped', {}))} 篇\n"
                f"SDK 后台：{'正在运行' if sdk_start.get('started') or sdk_start.get('already_running') else '未启动'}"
            ),
        )

    def start_smart_manual_takeover(self) -> None:
        selected = self.selected_ids()
        if len(selected) != 1:
            messagebox.showinfo(
                "手动修改当前页面",
                "请先只选择一篇需要人工修改网页的智能投稿论文。",
                parent=self,
            )
            return
        paper_id = selected[0]

        def work() -> dict[str, Any]:
            takeover = request_smart_manual_takeover([paper_id])
            page: dict[str, Any] = {}
            if paper_id in takeover.get("active", []):
                page = open_smart_manual_takeover_page(paper_id)
            elif paper_id in takeover.get("requested", []):
                page = {
                    "ok": False,
                    "pending": True,
                    "code": "SMART_MANUAL_TAKEOVER_WAITING_CURRENT_TURN",
                    "message": "当前 SDK Turn 结束后，这篇论文会单独进入人工修改状态。",
                }
            return {
                "takeover": {
                    key: value
                    for key, value in takeover.items()
                    if key != "state"
                },
                "page": page,
            }

        def done(result: dict[str, Any]) -> None:
            self.refresh_smart_submission(update_detail=False)
            page = result.get("page", {}) if isinstance(result.get("page"), dict) else {}
            if page.get("ok"):
                self.status_var.set(f"{paper_id} 已进入人工修改；自动调度仅暂停此论文")
            elif page.get("pending"):
                self.status_var.set(f"{paper_id} 将在当前 SDK Turn 后进入人工修改")
                messagebox.showinfo(
                    "已预约人工修改",
                    "当前 SDK Turn 仍在收尾，程序不会再给这篇论文派发新 Turn。\n\n"
                    "状态显示“人工修改中”后，再点击一次“手动修改当前页面”即可直接进入该页；"
                    "其他论文继续正常并发运行。",
                    parent=self,
                )
            else:
                messagebox.showwarning(
                    "未能打开当前页面",
                    str(
                        page.get("message")
                        or next(
                            iter(
                                (
                                    result.get("takeover", {}).get("skipped", {})
                                    if isinstance(result.get("takeover"), dict)
                                    else {}
                                ).values()
                            ),
                            page.get("code") or "请查看详细状态。",
                        )
                    ),
                    parent=self,
                )
            self._show_operation_result(
                self.smart_submission_text,
                "智能投稿人工修改",
                result,
                summary=(
                    f"论文：{paper_id}\n"
                    f"状态：{'页面已交给你' if page.get('ok') else ('等待当前 Turn 结束' if page.get('pending') else '未打开')}\n"
                    "作用范围：仅当前论文；其他论文与全局调度不受影响"
                ),
            )

        self._run_background_task("打开智能投稿人工修改页面", work, done)

    def complete_smart_manual_takeover(self) -> None:
        selected = self.selected_ids()
        if len(selected) != 1:
            messagebox.showinfo(
                "修改完成并继续",
                "请先只选择一篇已经处于“人工修改中”的论文。",
                parent=self,
            )
            return
        paper_id = selected[0]

        def work() -> dict[str, Any]:
            completed = complete_smart_manual_takeover_workflow([paper_id])
            resumed_ids = [
                str(item)
                for item in completed.get("resumed", [])
                if str(item).strip()
            ]
            sdk_start = (
                start_smart_submission_program_runtime(resumed_ids)
                if resumed_ids
                else {"ok": False, "started": False}
            )
            return {
                "complete": {
                    key: value
                    for key, value in completed.items()
                    if key != "state"
                },
                "sdk_start": sdk_start,
            }

        def done(result: dict[str, Any]) -> None:
            self.refresh_smart_submission(update_detail=False)
            completed = (
                result.get("complete", {})
                if isinstance(result.get("complete"), dict)
                else {}
            )
            resumed = [str(item) for item in completed.get("resumed", [])]
            if resumed:
                self.status_var.set(f"{paper_id} 已重新检查当前页面并恢复智能投稿")
            else:
                messagebox.showwarning(
                    "尚未恢复",
                    str(
                        next(
                            iter(
                                (
                                    completed.get("skipped", {})
                                    if isinstance(completed.get("skipped"), dict)
                                    else {}
                                ).values()
                            ),
                            "当前真实页面无法验证，请保持页面打开后重试。",
                        )
                    ),
                    parent=self,
                )
            self._show_operation_result(
                self.smart_submission_text,
                "人工修改完成并继续",
                result,
                summary=(
                    f"论文：{paper_id}\n"
                    f"恢复：{'成功' if resumed else '未恢复'}\n"
                    "恢复逻辑：先验证仍打开的真实页面，再由原固定 SDK 会话从当前页继续"
                ),
            )

        self._run_background_task("完成智能投稿人工修改", work, done)

    def move_smart_submission_selected(self, direction: str) -> None:
        selected = self.selected_ids()
        if not selected:
            messagebox.showwarning("智能投稿", "请先选择一篇已排队论文。")
            return
        result = move_online_queue_item(selected[0], direction, activate=False)
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "智能投稿队列顺序已更新",
            {k: v for k, v in result.items() if k != "state"},
        )

    def apply_smart_submission_declarations(self) -> None:
        selected = self.selected_ids()
        if not selected:
            if not messagebox.askyesno(
                "套用默认声明",
                "没有选中论文。是否对所有检测到 AI 使用、作者确认或一稿多投声明阻塞的论文套用默认声明并重新入队？",
            ):
                return
        result = apply_standing_declarations(selected or None, activate=False)
        sdk_start = start_smart_submission_program_runtime(selected or None)
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "已套用事先确认的常用声明",
            {
                "declarations": {
                    k: v for k, v in result.items() if k != "state"
                },
                "sdk_start": sdk_start,
            },
        )

    def open_smart_retarget_dialog(self) -> None:
        tree = self.smart_submission_tree
        selected = [str(item) for item in tree.selection()] if tree is not None else []
        if len(selected) != 1:
            messagebox.showinfo(
                "转投其他期刊",
                "请先在智能投稿页只选择一篇需要转投的论文。",
                parent=self,
            )
            return
        paper_id = selected[0]
        state = self._load_smart_submission_state()
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            messagebox.showwarning(
                "转投其他期刊",
                "这篇论文目前只是自动候选。请先启用智能投稿，再发起转投。",
                parent=self,
            )
            return

        dialog = tk.Toplevel(self)
        dialog.title(f"转投其他期刊 · {paper_id}")
        dialog.transient(self)
        dialog.grab_set()
        self._fit_dialog_window(
            dialog,
            preferred=(760, 720),
            minimum=(640, 560),
            margins=(80, 100),
        )
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        active_ids = [
            str(item)
            for item in paper.get("active_submission_ids", [])
            if str(item).strip()
        ] if isinstance(paper.get("active_submission_ids"), list) else []
        summary = ttk.LabelFrame(body, text="当前投稿", padding=8)
        summary.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(
            summary,
            text=(
                f"当前期刊：{paper.get('target_journal') or '-'}\n"
                f"当前状态：{self._smart_status_label(str(paper.get('status') or ''))}\n"
                f"活动投稿编号：{'、'.join(active_ids) or '-'}\n"
                "程序将先核验这些投稿是否仍有效；只有全部关闭后才会创建新投稿。"
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        mode_labels = {
            "指定具体期刊": "specific",
            "AI 智能选刊": "ai",
        }
        tier_labels = {
            "SCI/SCIE": "sci",
            "中文核心": "chinese_core",
            "普刊": "general",
        }
        priority_labels = {
            "中稿优先": "acceptance",
            "速度优先": "speed",
            "质量优先": "quality",
            "均衡": "balanced",
        }
        withdrawal_labels = {
            "自动核验并决定（推荐）": "auto",
            "强制先撤稿": "force",
            "我确认旧稿已关闭 / 无需撤稿": "confirmed_closed",
        }
        oa_labels = {
            "订阅 / 非 OA 优先": "subscription_first",
            "OA 或非 OA 均可": "either",
            "允许 OA": "oa_allowed",
            "只接受 OA": "oa_required",
        }

        mode_var = tk.StringVar(value="AI 智能选刊")
        target_var = tk.StringVar(value="")
        tier_var = tk.StringVar(value="SCI/SCIE")
        priority_var = tk.StringVar(value="中稿优先")
        withdrawal_var = tk.StringVar(value="自动核验并决定（推荐）")
        oa_var = tk.StringVar(value="订阅 / 非 OA 优先")
        max_apc_var = tk.StringVar(value="")
        keywords_var = tk.StringVar(value="")
        special_issue_var = tk.BooleanVar(value=True)
        quartile_vars = {
            "Q1": tk.BooleanVar(value=False),
            "Q2": tk.BooleanVar(value=False),
            "Q3": tk.BooleanVar(value=True),
            "Q4": tk.BooleanVar(value=True),
        }

        row = 1
        ttk.Label(body, text="转投方式").grid(row=row, column=0, sticky="w", pady=4)
        mode_box = ttk.Combobox(
            body,
            textvariable=mode_var,
            values=list(mode_labels),
            state="readonly",
        )
        mode_box.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="指定目标期刊").grid(row=row, column=0, sticky="w", pady=4)
        target_entry = ttk.Entry(body, textvariable=target_var, state="disabled")
        target_entry.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="AI 选刊范围").grid(row=row, column=0, sticky="w", pady=4)
        tier_box = ttk.Combobox(
            body,
            textvariable=tier_var,
            values=list(tier_labels),
            state="readonly",
        )
        tier_box.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="选刊优先级").grid(row=row, column=0, sticky="w", pady=4)
        priority_box = ttk.Combobox(
            body,
            textvariable=priority_var,
            values=list(priority_labels),
            state="readonly",
        )
        priority_box.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="SCI 分区范围").grid(row=row, column=0, sticky="w", pady=4)
        quartile_frame = ttk.Frame(body)
        quartile_frame.grid(row=row, column=1, sticky="w", pady=4)
        for quartile, variable in quartile_vars.items():
            ttk.Checkbutton(quartile_frame, text=quartile, variable=variable).pack(
                side=tk.LEFT, padx=(0, 10)
            )
        row += 1
        ttk.Label(body, text="开放获取策略").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            body,
            textvariable=oa_var,
            values=list(oa_labels),
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="APC 上限（可空）").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=max_apc_var).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="主题关键词（可空）").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=keywords_var).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="特刊").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Checkbutton(
            body,
            text="允许选择合适的特刊 / 专题",
            variable=special_issue_var,
        ).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        ttk.Label(body, text="旧稿处理").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            body,
            textvariable=withdrawal_var,
            values=list(withdrawal_labels),
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(body, text="补充要求（可空）").grid(row=row, column=0, sticky="nw", pady=4)
        notes = tk.Text(body, height=5, wrap="word")
        notes.grid(row=row, column=1, sticky="nsew", pady=4)
        body.rowconfigure(row, weight=1)

        hint = ttk.Label(
            body,
            text=(
                "自动核验不会仅因日期已过就认定撤稿；必须看到 Rejected、Withdrawn、Closed、"
                "已失效转投且无活动稿件等明确证据。指定期刊生成后还会再次核对目标是否一致。"
            ),
            foreground="#805500",
            wraplength=690,
            justify=tk.LEFT,
        )
        row += 1
        hint.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        def update_mode_state(*_args: Any) -> None:
            specific = mode_labels.get(mode_var.get()) == "specific"
            target_entry.configure(state="normal" if specific else "disabled")
            tier_box.configure(state="disabled" if specific else "readonly")
            priority_box.configure(state="disabled" if specific else "readonly")
            if specific:
                target_entry.focus_set()

        mode_var.trace_add("write", update_mode_state)

        def submit() -> None:
            mode = mode_labels[mode_var.get()]
            target = target_var.get().strip()
            if mode == "specific" and not target:
                messagebox.showwarning(
                    "缺少目标期刊",
                    "选择“指定具体期刊”时必须填写期刊名称。",
                    parent=dialog,
                )
                return
            withdrawal_policy = withdrawal_labels[withdrawal_var.get()]
            if withdrawal_policy == "confirmed_closed" and not messagebox.askyesno(
                "确认旧稿已经关闭",
                "你选择了“旧稿已关闭 / 无需撤稿”。程序会把本地仍显示活动的投稿编号记为已关闭，"
                "然后直接进入转投。只有你已经在期刊后台或邮件中确认旧稿关闭时才应继续。\n\n确认吗？",
                parent=dialog,
            ):
                return
            mode_description = target if mode == "specific" else (
                f"{tier_var.get()}，{priority_var.get()}"
            )
            if not messagebox.askyesno(
                "开始转投",
                f"论文：{paper_id}\n转投方案：{mode_description}\n"
                f"旧稿处理：{withdrawal_var.get()}\n\n"
                "程序会自动核验旧稿、按需撤稿、调用选刊作者 V2 生成新投稿包，"
                "验收通过后进入智能投稿并自动提交。确认开始吗？",
                parent=dialog,
            ):
                return
            payload = {
                "mode": mode,
                "target_journal": target,
                "publication_tier": tier_labels[tier_var.get()],
                "priority": priority_labels[priority_var.get()],
                "quartiles": [
                    key for key, variable in quartile_vars.items() if variable.get()
                ],
                "oa_policy": oa_labels[oa_var.get()],
                "max_apc": max_apc_var.get().strip(),
                "keywords": keywords_var.get().strip(),
                "allow_special_issue": bool(special_issue_var.get()),
                "withdrawal_policy": withdrawal_policy,
                "reason": "user_requested_retarget_from_ui",
                "notes": notes.get("1.0", tk.END).strip(),
            }
            dialog.grab_release()
            dialog.destroy()

            def work() -> dict[str, Any]:
                return request_smart_submission_retarget(
                    paper_id,
                    activate=True,
                    **payload,
                )

            def done(result: dict[str, Any]) -> None:
                self.refresh()
                self.refresh_smart_submission(update_detail=False)
                recorded = result.get("recorded", {}) if isinstance(result, dict) else {}
                withdrawal = (
                    recorded.get("withdrawal", {})
                    if isinstance(recorded, dict)
                    else {}
                )
                decision = str(withdrawal.get("decision") or "")
                summary_text = (
                    "已进入旧稿核验 / 撤稿流程；全部关闭后会自动重新选刊。"
                    if decision == "verify_or_withdraw"
                    else "旧稿已确认关闭，已进入选刊作者 V2 转投流程。"
                )
                self.status_var.set(summary_text)
                self._show_operation_result(
                    self.smart_submission_text,
                    "转投请求已建立",
                    result,
                    summary=summary_text,
                )

            self._run_background_task("建立智能转投请求", work, done)

        buttons = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="开始转投", command=submit).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        update_mode_state()

    def start_smart_submission_visible_online(self) -> None:
        selected = self._selected_or_remembered_paper_ids()
        if not selected:
            messagebox.showwarning(
                "未选择论文",
                "请明确选择一篇或多篇要启动 SDK 智能投稿的论文。",
                parent=self,
            )
            return
        live_before = smart_submission_status()
        program_control = (
            live_before.get("program_control", {})
            if isinstance(live_before.get("program_control"), dict)
            else {}
        )
        max_parallel = int(program_control.get("max_parallel_workers") or 1)
        preview = "\n".join(selected[:10])
        if len(selected) > 10:
            preview += f"\n……另有 {len(selected) - 10} 篇"
        if not messagebox.askyesno(
            "启动 SDK 智能投稿",
            f"已选择：{len(selected)} 篇\n"
            f"程序并发上限：{max_parallel} 篇\n"
            "执行结构：程序调度 + 每篇论文一个固定 Codex SDK 会话\n"
            "登录结构：本地可信代理使用已保存密码自动登录；账号不存在时按设置进入注册流程\n"
            "联网：是，将访问已配置的投稿系统\n\n"
            f"{preview}\n\n"
            "确认后，程序会切换这些论文的投稿路线、执行本地预检，并把通过的论文加入 SDK 队列。"
            "已有 SDK 任务不需要清空，新论文会按可用并发槽继续运行。\n\n"
            "网页会话可能上传文件并填写表单。密码不会进入 SDK 提示词或会话记录。"
            "最终审核页验收通过后，程序会自动签发仅限本篇、本次尝试的一次性权限并完成最终提交，"
            "不会再次弹出逐篇确认，也不会在结果不确定时重复点击。"
            "遇到付款、强制开放获取（OA）、验证码、双重验证（2FA）、邮箱验证、"
            "网站条款、作者确认、强制 ORCID 或新增法律/伦理事实会暂停并显示恢复入口。\n\n确认继续吗？",
            parent=self,
        ):
            return

        def work() -> dict[str, Any]:
            prepared = prepare_smart_submission_sdk_papers(
                selected,
                orchestrator=self.orchestrator,
            )
            queued_ids = [
                str(item)
                for item in prepared.get("queued", [])
                if str(item).strip()
            ]
            sdk_start = (
                start_smart_submission_program_runtime(queued_ids)
                if queued_ids
                else {"ok": False, "started": False, "code": "NO_QUEUED_PAPERS"}
            )
            return {
                "unified_state": self.orchestrator.sync(),
                "prepared": prepared,
                "sdk_start": sdk_start,
                "live_status": smart_submission_status(),
            }

        def done(payload: dict[str, Any]) -> None:
            self.state_data = payload["unified_state"]
            result = payload["prepared"]
            sdk_start = payload["sdk_start"]
            live_status = payload["live_status"]
            self.refresh_smart_submission(update_detail=False)
            if not result.get("ok"):
                self.status_var.set("SDK 智能投稿未启动；请查看预检结果")
                self._set_technical_details(
                    "SDK 智能投稿准备未通过",
                    {k: v for k, v in result.items() if k != "state"},
                )
                skipped = (
                    result.get("skipped", {})
                    if isinstance(result.get("skipped"), dict)
                    else {}
                )
                reason_lines = [
                    f"- {paper_id}：{reason}"
                    for paper_id, reason in list(skipped.items())[:5]
                ]
                messagebox.showwarning(
                    "没有可启动的论文",
                    "所选论文没有进入 SDK 执行队列。"
                    + ("\n\n" + "\n".join(reason_lines) if reason_lines else "")
                    + "\n\n完整原因已放在“总览 → 技术详情”。",
                    parent=self,
                )
                return
            self.status_var.set("SDK 智能投稿已启动")
            queued_ids = [
                str(item)
                for item in result.get("queued", [])
                if str(item).strip()
            ]
            resumed_ids = [
                str(item)
                for item in result.get("resumed", [])
                if str(item).strip()
            ]
            skipped = (
                result.get("skipped", {})
                if isinstance(result.get("skipped"), dict)
                else {}
            )
            self._show_operation_result(
                self.smart_submission_text,
                "SDK 智能投稿已进入程序调度",
                {
                    "preparation": {
                        k: v for k, v in result.items() if k != "state"
                    },
                    "sdk_start": sdk_start,
                },
                summary=(
                    f"已入队：{len(queued_ids)} 篇\n"
                    f"其中续跑已有任务：{len(resumed_ids)} 篇\n"
                    f"未通过：{len(skipped)} 篇\n"
                    f"SDK 后台：{'正在运行' if sdk_start.get('started') or sdk_start.get('already_running') else '未启动'}\n"
                    f"在线队列：{live_status.get('online_queue_count')}\n"
                    f"执行中：{live_status.get('online_running_count')}\n"
                    f"等待执行：{live_status.get('online_queued_count')}"
                ),
            )

        self._run_background_task("启动 SDK 智能投稿", work, done)

    def open_smart_submission_scheduler_thread(self) -> None:
        runtime = smart_submission_sdk_status()
        papers = (
            runtime.get("papers", {})
            if isinstance(runtime.get("papers"), dict)
            else {}
        )
        self._show_operation_result(
            self.smart_submission_text,
            "SDK 智能投稿调度状态",
            runtime,
            summary=(
                "智能投稿调度器现在是程序逻辑，因此没有单独的 AI 调度会话。\n"
                f"SDK 后台：{'运行中' if runtime.get('running') else '空闲'}\n"
                f"固定论文会话：{runtime.get('fixed_session_count') or 0}\n"
                f"等待追加：{len(runtime.get('pending_selected_ids') or [])}\n"
                f"模型：{runtime.get('configured_model') or '-'}\n"
                f"有记录的论文：{len(papers)}"
            ),
        )

    def open_smart_submission_worker_thread(self) -> None:
        runtime_status = smart_submission_sdk_status()
        state = self._load_smart_submission_state()
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        selected = (
            [str(item) for item in self.smart_submission_tree.selection()]
            if self.smart_submission_tree is not None
            else []
        )
        paper_id = selected[0] if selected else ""
        paper = papers.get(paper_id, {}) if paper_id and isinstance(papers.get(paper_id), dict) else {}
        runtime_papers = (
            runtime_status.get("papers", {})
            if isinstance(runtime_status.get("papers"), dict)
            else {}
        )
        runtime_paper = (
            runtime_papers.get(paper_id, {})
            if isinstance(runtime_papers.get(paper_id), dict)
            else {}
        )
        monitor_channel = str(paper.get("sdk_active_channel") or "") == "monitor"
        thread_id = str(
            (paper.get("sdk_monitor_thread_id") if monitor_channel else paper.get("sdk_thread_id"))
            or runtime_paper.get("thread_id")
            or paper.get("thread_id")
            or ""
        ).strip()
        if not thread_id:
            messagebox.showwarning(
                "查看 SDK 会话",
                "请先在智能投稿表选择一篇已创建 SDK 会话的论文。",
            )
            return
        runtime = default_smart_submission_sdk_runtime()
        session_path = _writing_session_log_path(Path(runtime.codex_home), thread_id)
        transcript = (
            _writing_session_transcript(session_path)
            if session_path is not None
            else {"ok": False, "messages": []}
        )

        dialog = tk.Toplevel(self)
        dialog.title(f"智能投稿 SDK 会话 · {paper_id}")
        self._fit_dialog_window(
            dialog,
            preferred=(1180, 760),
            minimum=(760, 500),
        )
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)

        header = ttk.Frame(dialog, padding=(10, 10, 10, 6))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            header,
            text=(
                f"论文：{paper_id}\n"
                f"会话类型：{'轻量巡检（每次轮换）' if monitor_channel else '投稿固定会话'}\n"
                f"会话 ID：{thread_id}\n"
                f"当前/最近 Turn ID：{(paper.get('sdk_monitor_turn_id') or '-') if monitor_channel else (paper.get('sdk_turn_id') or runtime_paper.get('turn_id') or '-')}\n"
                f"状态：{(paper.get('sdk_monitor_runtime_status') or '-') if monitor_channel else (paper.get('sdk_runtime_status') or runtime_paper.get('status') or '-')}\n"
                f"模型：{(paper.get('sdk_monitor_model') or '-') if monitor_channel else (paper.get('sdk_model') or runtime_paper.get('model') or '-')}\n"
                "运行位置：本程序独立 CODEX_HOME 内部 codex.exe CLI；不会出现在桌面任务列表。"
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        body = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        transcript_text = self._readonly_text(body)
        rendered: list[str] = []
        for item in transcript.get("messages", []):
            if not isinstance(item, dict):
                continue
            rendered.extend(
                [
                    f"[{item.get('timestamp') or '-'}] {item.get('role') or '记录'}",
                    str(item.get("text") or ""),
                    "",
                ]
            )
        if not rendered and runtime_paper.get("final_response"):
            rendered.extend(["[最近响应]", str(runtime_paper["final_response"])])
        if not rendered:
            rendered.append("会话已绑定，但当前还没有可显示的 Turn 内容。")
        self._set_text(transcript_text, "\n".join(rendered))

        def copy_session_ids() -> None:
            self.clipboard_clear()
            self.clipboard_append(
                f"paper_id={paper_id}\n"
                f"thread_id={thread_id}\n"
                f"turn_id={(paper.get('sdk_monitor_turn_id') or '') if monitor_channel else (paper.get('sdk_turn_id') or runtime_paper.get('turn_id') or '')}"
            )

        actions = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(actions, text="复制会话标识", command=copy_session_ids).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="打开 SDK 日志目录",
            command=lambda: self.open_path(Path(SMART_SUBMISSION_SDK_JOURNAL_PATH).parent),
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def pause_smart_submission_waiting_approval(self) -> None:
        result = requeue_waiting_approval_workers(activate=False)
        requeued_ids = [
            str(item.get("paper_id") or "")
            for item in result.get("requeued", [])
            if isinstance(item, dict) and str(item.get("paper_id") or "").strip()
        ]
        sdk_start = (
            start_smart_submission_program_runtime(requeued_ids)
            if requeued_ids
            else {"ok": True, "started": False}
        )
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "等待权限确认的任务已重新排队",
            {
                "requeue": {k: v for k, v in result.items() if k != "state"},
                "sdk_start": sdk_start,
            },
        )

    def cleanup_smart_submission_browser_tabs(self) -> None:
        preview = smart_submission_browser_cleanup(close=False, include_stale_submission_tabs=True)
        candidate_count = int(preview.get("candidate_count") or 0)
        scanned_count = int(preview.get("scanned_count") or 0)
        protected_count = int((preview.get("kept_summary") or {}).get("protected_or_target") or 0)
        if candidate_count <= 0:
            messagebox.showinfo(
                "清理浏览器标签",
                f"未发现可安全清理的旧投稿页。\n\n扫描普通页面: {scanned_count}\n保护邮箱/当前目标页: {protected_count}",
            )
            self._show_operation_result(
                self.smart_submission_text,
                "浏览器标签清理预检完成",
                preview,
                summary="没有发现可安全清理的旧投稿页。",
            )
            return
        if not messagebox.askyesno(
            "清理浏览器标签",
            "将关闭旧投稿系统页面，但不会关闭邮箱、账号页和当前在线队列目标页。\n\n"
            f"扫描普通页面: {scanned_count}\n"
            f"保护邮箱/当前目标页: {protected_count}\n"
            f"准备关闭旧投稿页: {candidate_count}\n\n"
            "确认关闭这些旧投稿页吗？",
        ):
            self._show_operation_result(
                self.smart_submission_text,
                "浏览器标签清理已取消",
                preview,
                summary="预检已完成，没有关闭任何标签页。",
            )
            return
        result = smart_submission_browser_cleanup(close=True, include_stale_submission_tabs=True)
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(self.smart_submission_text, "智能投稿浏览器标签清理已完成", result)

    def pause_smart_submission(self) -> None:
        if not messagebox.askyesno(
            "智能投稿手动模式",
            "确认停止智能投稿的新调度吗？\n\n"
            "这会把智能投稿切换为手动暂停，停用程序定时器，并让当前 SDK Turn 完成后暂停。",
        ):
            return
        result = self._pause_smart_submission_runtime(reason="ui_manual_pause")
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(self.smart_submission_text, "智能投稿已切换到手动暂停", result)

    def enable_smart_submission_daily_monitoring(self) -> None:
        status = smart_submission_status()
        configured_time = str(
            status.get("daily_monitor_time_local") or "09:00"
        )
        if not messagebox.askyesno(
            "启用智能投稿每日监控",
            f"确认启用智能投稿每天 {configured_time}（本机时间）的状态巡检吗？\n\n"
            "每日监控会通过每次轮换的轻量 SDK 会话只读检查锁定期刊后台；"
            "如确实需要邮箱证据，会记录明确待处理项，不会假装已经检查邮箱。"
            "如需修改时间，请打开“智能投稿设置 → 运行概况”。",
        ):
            return
        result = restore_smart_submission_daily_monitoring()
        self._smart_monitor_next_poll_at = 0.0
        self.refresh_smart_submission(update_detail=False)
        self._show_operation_result(
            self.smart_submission_text,
            "智能投稿每日监控已启用",
            {key: value for key, value in result.items() if key != "state"},
        )

    def render_overview_table(self) -> None:
        queue = list(self.state_data.get("paper_queue", []))
        queue_positions = {
            paper_id: str(index + 1)
            for index, paper_id in enumerate(queue)
        }
        papers = self.state_data.get("papers", {})
        papers = papers if isinstance(papers, dict) else {}
        state_counts = Counter(row_state(record) for record in papers.values() if isinstance(record, dict))
        human_count = sum(
            1
            for record in papers.values()
            if isinstance(record, dict)
            and any(
                marker in str(
                    record.get("next_action_label")
                    or action_label(record.get("next_action", ""))
                    or record.get("blocked_reason")
                    or ""
                )
                for marker in ("人工", "确认", "补充", "登录", "验证")
            )
        )
        total_count = len(papers)
        active_count = state_counts.get("running", 0) + state_counts.get("queued", 0)
        complete_count = state_counts.get("done", 0)
        waiting_count = max(0, total_count - active_count - complete_count)
        overview_metrics = getattr(self, "overview_metrics", None)
        if overview_metrics is not None:
            def percent(count: int) -> str:
                return f"{(count / total_count * 100):.1f}%" if total_count else "0.0%"

            overview_metrics.update_values(
                {
                    "total": (total_count, "当前工作区"),
                    "active": (active_count, f"{percent(active_count)} · 排队或运行"),
                    "waiting": (waiting_count, f"{percent(waiting_count)} · 尚需处理"),
                    "human": (human_count, f"{percent(human_count)} · 需要你介入"),
                    "complete": (complete_count, f"{percent(complete_count)} · 全部完成"),
                }
            )
        visible = 0
        render_rows: list[dict[str, Any]] = []
        for paper_id, record in sorted(papers.items()):
            if not self.matches_filter(paper_id, record, queue):
                continue
            visible += 1
            stages = record.get("stages", {})
            queue_label = queue_positions.get(str(paper_id), "")
            values = (
                record.get("current_stage_label", ""),
                record.get("next_action_label") or action_label(record.get("next_action", "")),
                table_status_token(stages.get("writing", {})),
                table_status_token(stages.get("figures", {})),
                table_status_token(stages.get("journal_author", {})),
                table_status_token(stages.get("submission", {})),
                self._submission_route_label(record),
            )
            tag = "queued" if queue_label else row_state(record)
            render_rows.append(_treeview_row(paper_id, compact_paper_label(paper_id), values, (tag,)))
        _sync_treeview_rows(self.tree, render_rows)
        if not self.tree.selection() and self.selected_paper_id and self.tree.exists(self.selected_paper_id):
            self.tree.selection_set(self.selected_paper_id)
        self.update_detail()
        self.summary_var.set(f"显示 {visible}/{len(self.state_data.get('papers', {}))} 篇；统一队列 {len(queue)} 条")
        mode_label = {
            "api_key": "API 密钥模式：连接检测 / 私有审阅",
            "legacy_codex": "Codex 账号模式：四阶段 V2 SDK",
        }.get(str(self._execution_mode_cache), "执行配置正在读取")
        self.status_var.set(
            f"已加载 {visible} 篇论文；统一队列 {len(queue)} 条；AI执行：{mode_label}"
        )

    def render_stage_tables(self, *, only_stage: str | None = None) -> None:
        queue = list(self.state_data.get("paper_queue", []))
        queue_positions = {
            paper_id: str(index + 1)
            for index, paper_id in enumerate(queue)
        }
        status_all = self._scheduler_status_cache
        for stage, tree in self.stage_trees.items():
            if only_stage and stage != only_stage:
                continue
            visible = 0
            render_rows: list[dict[str, Any]] = []
            for paper_id, record in sorted(self.state_data.get("papers", {}).items()):
                stage_record = record.get("stages", {}).get(stage, {})
                if not self.stage_row_visible(stage, record, stage_record):
                    continue
                if not self._stage_row_matches_view(stage, paper_id, record, stage_record):
                    continue
                visible += 1
                queue_label = queue_positions.get(str(paper_id), "")
                detail = self._stage_detail_label(
                    stage,
                    stage_record.get("detail") or record.get("blocked_reason") or "",
                )
                admission = record.get("stage_admissions", {}).get(stage, {})
                admission_mode = str(admission.get("mode") or "") if isinstance(admission, dict) else ""
                if admission_mode == "manual_override":
                    detail = f"人工越级｜{detail or admission.get('reason') or '用户已放行'}"
                elif admission_mode == "automatic" and str(stage_record.get("status") or "missing") == "missing":
                    detail = "程序已识别上游完成｜待运行本阶段"
                values = (
                    table_status_token(stage_record),
                    record.get("next_action_label") or action_label(record.get("next_action", "")),
                    queue_label,
                    detail,
                )
                tag = "override" if admission_mode == "manual_override" else "queued" if queue_label else row_state(record)
                render_rows.append(_treeview_row(paper_id, compact_paper_label(paper_id), values, (tag,)))
            _sync_treeview_rows(tree, render_rows)
            self._refresh_stage_action_state(stage)
            config = status_all.get(stage, {})
            status_counts = Counter(
                str(record.get("stages", {}).get(stage, {}).get("status") or "missing")
                for record in self.state_data.get("papers", {}).values()
            )
            action_counts = Counter(
                str(record.get("next_action_label") or action_label(record.get("next_action", "")))
                for record in self.state_data.get("papers", {}).values()
                if str(record.get("current_stage") or "") == stage
            )
            active_count = status_counts.get("queued", 0) + status_counts.get("running", 0)
            action_summary = "；".join(f"{name}:{count}" for name, count in action_counts.most_common(3)) or "-"
            if stage in {"writing", "figures", "journal_author"}:
                live_loaded = {
                    "writing": bool(self._writing_runtime_status or self.writing_task_rows),
                    "figures": bool(self._figure_runtime_status or self.figure_task_rows),
                    "journal_author": bool(
                        self._journal_author_runtime_status or self.journal_author_task_rows
                    ),
                }[stage]
                if not live_loaded:
                    self.stage_status_vars[stage].set(
                        f"{STAGE_LABELS[stage]}队列：正在后台读取…"
                    )
                    self.stage_info_vars[stage].set(
                        f"本页 {visible} 篇｜状态读取不会阻塞界面"
                    )
                if self.current_tab_key() == stage:
                    if tree.selection() or live_loaded:
                        self.update_stage_detail(stage)
                    else:
                        self._set_text(
                            self.stage_detail_texts[stage],
                            "正在后台读取实时任务状态，界面仍可继续操作。",
                        )
                continue
            automation_status = self._automation_status_label(str(config.get("status") or "MISSING"))
            self.stage_status_vars[stage].set(f"后台调度：{automation_status}｜本页 {visible} 篇")
            channel = "｜通道：稳定投稿（默认）" if stage == "submission" else ""
            self.stage_info_vars[stage].set(
                f"本阶段排队/运行：{active_count}｜统一队列：{len(queue)}｜下一步：{action_summary}{channel}"
            )
            self.update_stage_detail(stage)

    def stage_row_visible(self, stage: str, record: dict, stage_record: dict) -> bool:
        admission = record.get("stage_admissions", {}).get(stage, {})
        return bool(isinstance(admission, dict) and admission.get("admitted"))

    def _stage_row_matches_view(
        self,
        stage: str,
        paper_id: str,
        record: dict,
        stage_record: dict,
    ) -> bool:
        search_var = self.stage_search_vars.get(stage)
        query = str(search_var.get() if search_var is not None else "").strip().casefold()
        if query:
            searchable = " ".join(
                str(value or "")
                for value in (
                    paper_id,
                    record.get("title"),
                    record.get("current_stage_label"),
                    record.get("next_action_label"),
                    record.get("blocked_reason"),
                    stage_record.get("detail"),
                    stage_record.get("status"),
                )
            ).casefold()
            if query not in searchable:
                return False

        filter_var = self.stage_filter_vars.get(stage)
        view = str(filter_var.get() if filter_var is not None else "全部状态")
        if view == "全部状态":
            return True
        status = str(stage_record.get("status") or "missing").strip().casefold()
        row = row_state(record)
        if view == "运行中":
            return status in {"queued", "running", "dispatching", "starting"} or row in {
                "queued",
                "running",
            }
        if view == "等待处理":
            return status in {"missing", "waiting", "paused", "ready", "pending"} or row in {
                "ready",
                "waiting",
            }
        if view == "阻塞/失败":
            return status in {"blocked", "failed", "error"} or row == "blocked"
        if view == "已完成":
            return status in {"done", "complete", "completed"} or row == "done"
        return True

    def matches_filter(self, paper_id: str, record: dict, queue: list[str]) -> bool:
        search_var = getattr(self, "overview_search_var", None)
        query = str(search_var.get() if search_var is not None else "").strip().casefold()
        if query:
            searchable = " ".join(
                str(value or "")
                for value in (
                    paper_id,
                    record.get("title"),
                    record.get("current_stage_label"),
                    record.get("next_action_label"),
                    record.get("blocked_reason"),
                    record.get("submission_route"),
                )
            ).casefold()
            if query not in searchable:
                return False
        kind = FILTERS.get(self.filter_var.get(), "all")
        if kind == "all":
            return True
        if kind == "queued":
            return paper_id in queue
        if kind == "ready":
            return str(record.get("next_action") or "").startswith("queue_")
        if kind == "waiting":
            return str(record.get("next_action") or "").startswith("wait_")
        if kind == "blocked":
            return str(record.get("next_action") or "") == "blocked"
        if kind in {"submission", "writing"}:
            return str(record.get("current_stage") or "") == kind
        return True

    def current_tab_key(self) -> str:
        selected = self.notebook.select()
        if hasattr(self, "submission_hub") and str(self.submission_hub) == selected:
            nested = self.submission_notebook.select()
            if str(self.tab_frames.get("submission")) == nested:
                return "submission"
            if str(self.tab_frames.get("smart_submission")) == nested:
                return "smart_submission"
        for key, frame in self.tab_frames.items():
            if str(frame) == selected:
                return key
        return "overview"

    def selected_ids(self) -> list[str]:
        tab = self.current_tab_key()
        if tab in self.stage_trees:
            selected = [str(item) for item in self.stage_trees[tab].selection()]
            if selected:
                return selected
            if tab == "writing" and self.writing_task_tree is not None:
                live_selected = [str(item) for item in self.writing_task_tree.selection()]
                return [
                    str(self.writing_task_rows[item].get("paper_id") or "")
                    for item in live_selected
                    if item in self.writing_task_rows and str(self.writing_task_rows[item].get("paper_id") or "")
                ]
            if tab == "figures" and self.figure_task_tree is not None:
                return [str(item) for item in self.figure_task_tree.selection()]
            if tab == "journal_author" and self.journal_author_task_tree is not None:
                return [str(item) for item in self.journal_author_task_tree.selection()]
            return []
        if tab == "smart_submission" and self.smart_submission_tree is not None:
            return [str(item) for item in self.smart_submission_tree.selection()]
        return [str(item) for item in self.tree.selection()]

    def current_record(self) -> tuple[str, dict]:
        selected = self.selected_ids()
        if not selected:
            return "", {}
        paper_id = selected[0]
        return paper_id, self.state_data.get("papers", {}).get(paper_id, {})

    @staticmethod
    def _detail_tone(state: object) -> str:
        return {
            "done": "success",
            "complete": "success",
            "running": "info",
            "queued": "info",
            "ready": "neutral",
            "waiting": "attention",
            "paused": "attention",
            "blocked": "danger",
            "failed": "danger",
            "override": "severe",
        }.get(str(state or "").strip().lower(), "neutral")

    @staticmethod
    def _detail_state_label(state: object) -> str:
        return {
            "done": "已完成",
            "complete": "已完成",
            "running": "运行中",
            "queued": "排队中",
            "ready": "待运行",
            "waiting": "等待中",
            "paused": "已暂停",
            "blocked": "需要处理",
            "failed": "运行失败",
            "override": "人工越级",
        }.get(str(state or "").strip().lower(), "需核对")

    def _render_record_summary(
        self,
        panel: StructuredDetailPanel,
        paper_id: str,
        record: dict,
        *,
        focus_stage: str = "",
        technical_text: str = "",
    ) -> None:
        stages = record.get("stages", {}) if isinstance(record.get("stages"), dict) else {}
        state = row_state(record)
        next_action = str(
            record.get("next_action_label")
            or action_label(record.get("next_action", ""))
            or "等待程序判断下一步"
        )
        blocker = self._smart_message_label(record.get("blocked_reason"))
        headline = f"下一步：{next_action}"
        if blocker and blocker != "-":
            headline = f"当前需要处理：{blocker}"

        current_fields: list[tuple[str, str]] = [
            ("当前阶段", str(record.get("current_stage_label") or "-")),
            ("下一步", next_action),
        ]
        sections: list[dict[str, Any]] = []
        if focus_stage:
            stage_record = stages.get(focus_stage, {}) if isinstance(stages.get(focus_stage), dict) else {}
            admission = record.get("stage_admissions", {}).get(focus_stage, {})
            admission_mode = {
                "native": "本阶段创建",
                "automatic": "上游完成后自动进入",
                "manual_override": "用户人工越级",
            }.get(str(admission.get("mode") or "") if isinstance(admission, dict) else "", "尚未进入")
            current_fields.extend(
                [
                    ("本阶段状态", status_token(stage_record)),
                    ("进入方式", admission_mode),
                    ("阶段说明", self._stage_detail_label(focus_stage, stage_record.get("detail")) or "-"),
                    ("主要产物", "已生成" if stage_record.get("artifact") else "尚未生成"),
                ]
            )
        sections.append({"title": "当前状态", "fields": current_fields})

        progress_fields = []
        for stage_name in STAGE_ORDER:
            stage_record = stages.get(stage_name, {}) if isinstance(stages.get(stage_name), dict) else {}
            progress_fields.append(
                (
                    STAGE_LABELS[stage_name],
                    status_token(stage_record),
                )
            )
        sections.append({"title": "全流程进度", "fields": progress_fields})
        if blocker and blocker != "-":
            sections.append(
                {
                    "title": "关键卡点",
                    "body": [blocker, "处理完成后可回到本页继续任务。"],
                    "tone": "danger" if state == "blocked" else "attention",
                }
            )
        panel.render(
            title=str(record.get("title") or paper_id),
            status=self._detail_state_label(state),
            tone=self._detail_tone(state),
            headline=headline,
            sections=sections,
            technical_text=technical_text,
        )

    def update_detail(self) -> None:
        paper_id, record = self.current_record()
        self.selected_paper_id = paper_id
        if not paper_id:
            self.detail_title_var.set("未选择论文")
            self._set_text(self.detail_text, "在左侧选择一篇论文查看详情。")
            self.overview_detail_panel.render(
                title="请选择一篇论文",
                status="未选择",
                tone="neutral",
                headline="从左侧列表选择论文后，这里会显示当前状态、下一步和需要处理的事项。",
                sections=(),
                technical_text="在左侧选择一篇论文查看详情。",
            )
            return
        self.detail_title_var.set(paper_id)
        technical = self.detail_text_for_record(paper_id, record)
        self._set_text(self.detail_text, technical)
        self._render_record_summary(
            self.overview_detail_panel,
            paper_id,
            record,
            technical_text=technical,
        )
        self._set_technical_details(f"论文 {paper_id} 技术详情", record)

    def update_stage_detail(self, stage: str) -> None:
        tree = self.stage_trees.get(stage)
        detail = self.stage_detail_texts.get(stage)
        panel = self.stage_detail_panels.get(stage)
        if tree is None or detail is None or panel is None:
            return
        selected = [str(item) for item in tree.selection()]
        if stage == "writing" and not selected and self.writing_task_tree is not None and self.writing_task_tree.selection():
            self.update_writing_task_detail()
            return
        if stage == "figures" and not selected and self.figure_task_tree is not None and self.figure_task_tree.selection():
            self.update_figure_task_detail()
            return
        if (
            stage == "journal_author"
            and not selected
            and self.journal_author_task_tree is not None
            and self.journal_author_task_tree.selection()
        ):
            self.update_journal_author_task_detail()
            return
        if not selected:
            if stage == "writing":
                runtime_status = self._writing_runtime_status
                journal_papers = self._writing_journal_papers
                if not runtime_status:
                    try:
                        _pipeline, runtime_status, journal_papers = self._load_writing_live_snapshot()
                    except Exception as exc:
                        runtime_status = {
                            "status": "failed",
                            "blocker": str(getattr(exc, "code", type(exc).__name__)),
                        }
                        journal_papers = {}
                lines = [
                    "本地写作任务",
                    "",
                    f"调度状态：{WRITING_TASK_STATUS_LABELS.get(str(runtime_status.get('status') or ''), runtime_status.get('status') or '-')}",
                    f"任务总数：{len(self.writing_task_rows)}",
                    f"正在占用并发：{runtime_status.get('active_count') or 0}",
                    f"等待前置条件：{runtime_status.get('waiting_prerequisite_count') or 0}",
                    f"等待容量：{runtime_status.get('waiting_capacity_count') or 0}",
                    f"卡点：{runtime_status.get('blocked_count') or 0}",
                    "",
                ]
                for paper_key, raw_record in sorted(journal_papers.items(), key=lambda item: str(item[0]).casefold()):
                    if not isinstance(raw_record, dict):
                        continue
                    paper_id = str(raw_record.get("paper_id") or "").strip()
                    raw_status = str(raw_record.get("status") or "unknown").strip().lower()
                    label = paper_id or f"待命名任务（{paper_key}）"
                    try:
                        step_done = int(raw_record.get("completed_step") or 0)
                    except (TypeError, ValueError):
                        step_done = 0
                    blocker = str(raw_record.get("blocker") or "").strip()
                    display_status = WRITING_TASK_STATUS_LABELS.get(raw_status, raw_status)
                    if raw_status in {"waiting_event", "waiting_prerequisite"} and blocker == "WRITING_EVIDENCE_GATE_BLOCKED":
                        display_status = "等待真实实验数据（不占用并发）"
                    line = f"- {label}：{display_status}，已完成 Step {step_done}"
                    if blocker:
                        line += f"，卡点 {blocker}"
                    lines.append(line)
                if not journal_papers:
                    lines.append("当前没有本地写作任务。")
                technical = "\n".join(lines)
                self._set_text(detail, technical)
                runtime_state = str(runtime_status.get("status") or "idle").strip().lower()
                runtime_label = WRITING_TASK_STATUS_LABELS.get(runtime_state, "空闲")
                panel.render(
                    title="写论文任务概览",
                    status=runtime_label,
                    tone=self._detail_tone(runtime_state),
                    headline=(
                        f"当前 {runtime_status.get('active_count') or 0} 篇正在执行，"
                        f"{runtime_status.get('blocked_count') or 0} 篇需要处理。"
                    ),
                    sections=(
                        {
                            "title": "运行概况",
                            "fields": (
                                ("任务总数", len(self.writing_task_rows)),
                                ("正在运行", runtime_status.get("active_count") or 0),
                                ("等待前置条件", runtime_status.get("waiting_prerequisite_count") or 0),
                                ("等待并发槽", runtime_status.get("waiting_capacity_count") or 0),
                                ("阻塞", runtime_status.get("blocked_count") or 0),
                            ),
                        },
                        {
                            "title": "使用提示",
                            "body": (
                                "从上方“当前执行任务”选择论文，可查看准确 Step 与卡点。",
                                "模型、Token、会话和原始验收记录在“技术信息”中。",
                            ),
                            "tone": "info",
                        },
                    ),
                    technical_text=technical,
                )
                self._set_technical_details(
                    "写论文 · 本地 SDK 任务技术详情",
                    {"runtime": runtime_status, "papers": journal_papers},
                )
                return
            if stage == "figures":
                runtime_status = self._figure_runtime_status
                journal_papers = self._figure_journal_papers
                if not runtime_status:
                    try:
                        _pipeline, runtime_status, journal_papers = self._load_figure_live_snapshot()
                    except Exception as exc:
                        runtime_status = {
                            "status": "failed",
                            "blocker": str(getattr(exc, "code", type(exc).__name__)),
                        }
                        journal_papers = {}
                lines = [
                    "V2 本地画图任务",
                    "",
                    f"调度状态：{runtime_status.get('status') or '-'}",
                    f"固定会话数：{len(journal_papers)}",
                    f"正在执行：{runtime_status.get('active_count') or 0}",
                    f"等待：{runtime_status.get('waiting_count') or 0}",
                    f"卡点：{runtime_status.get('blocked_count') or 0}",
                    "",
                    "执行规则：程序动态补充并发槽位；每篇论文只用一个 SDK 会话，并按冻结的流程快照顺序连续发送。",
                    "选中上方实时任务可查看当前 Step、会话 ID、Turn ID 和产物验收结果。",
                ]
                if not journal_papers:
                    lines.extend(["", "当前没有 V2 SDK 画图任务。"])
                technical = "\n".join(lines)
                self._set_text(detail, technical)
                runtime_state = str(runtime_status.get("status") or "idle").strip().lower()
                panel.render(
                    title="图表与插图任务概览",
                    status=self._detail_state_label(runtime_state),
                    tone=self._detail_tone(runtime_state),
                    headline=(
                        f"当前 {runtime_status.get('active_count') or 0} 篇正在执行，"
                        f"{runtime_status.get('blocked_count') or 0} 篇需要处理。"
                    ),
                    sections=(
                        {
                            "title": "运行概况",
                            "fields": (
                                ("固定论文任务", len(journal_papers)),
                                ("正在运行", runtime_status.get("active_count") or 0),
                                ("等待", runtime_status.get("waiting_count") or 0),
                                ("阻塞", runtime_status.get("blocked_count") or 0),
                            ),
                        },
                        {
                            "title": "使用提示",
                            "body": (
                                "每篇论文使用一个固定会话，并按冻结流程连续执行。",
                                "选择上方任务查看当前 Step、产物与需要补充的数据。",
                            ),
                            "tone": "info",
                        },
                    ),
                    technical_text=technical,
                )
                self._set_technical_details(
                    "图表与插图 · 本地 SDK 任务技术详情",
                    {"runtime": runtime_status, "papers": journal_papers},
                )
                return
            if stage == "submission":
                technical = (
                    "稳定投稿（默认）\n\n"
                    "继续使用成熟的自动投稿流程，负责日常投稿、监控、修回和改投。\n"
                    "智能投稿不会因为导入或预检记录而替代这条通道。\n\n"
                    "选择左侧论文查看单篇投稿状态。"
                )
                self._set_text(detail, technical)
                panel.render(
                    title="稳定投稿",
                    status="默认通道",
                    tone="success",
                    headline="成熟投稿通道保持可用；选择左侧论文查看单篇状态与下一步。",
                    sections=(
                        {
                            "title": "通道职责",
                            "body": (
                                "负责日常投稿、状态监控、修回和改投。",
                                "智能投稿的导入或预检不会自动替代这条通道。",
                            ),
                            "tone": "success",
                        },
                    ),
                    technical_text=technical,
                )
                return
            project = LEGACY_PROJECTS[stage]
            text = "\n".join(
                [
                    f"{STAGE_LABELS[stage]}项目",
                    "项目连接信息已就绪；如需查看目录、状态文件和调度文件路径，请显示技术信息。",
                    "",
                    "选择左侧论文查看阶段细节。",
                ]
            )
            self._set_text(detail, text)
            panel.render(
                title=f"{STAGE_LABELS[stage]}任务概览",
                status="待选择",
                tone="neutral",
                headline="选择左侧论文查看当前状态、下一步和需要处理的事项。",
                sections=(
                    {
                        "title": "工作区状态",
                        "body": ("项目连接与状态投影已就绪。",),
                        "tone": "success",
                    },
                ),
                technical_text=text,
            )
            self._set_technical_details(f"{STAGE_LABELS[stage]}项目技术详情", project)
            return
        paper_id = selected[0]
        self.selected_paper_id = paper_id
        if stage == "writing" and self.writing_task_tree is not None:
            live_selected = self.writing_task_tree.selection()
            if live_selected:
                self.writing_task_tree.selection_remove(*live_selected)
        if stage == "figures" and self.figure_task_tree is not None:
            live_selected = self.figure_task_tree.selection()
            if live_selected:
                self.figure_task_tree.selection_remove(*live_selected)
        if stage == "journal_author" and self.journal_author_task_tree is not None:
            live_selected = self.journal_author_task_tree.selection()
            if live_selected:
                self.journal_author_task_tree.selection_remove(*live_selected)
        record = self.state_data.get("papers", {}).get(paper_id, {})
        technical = self.detail_text_for_record(paper_id, record, focus_stage=stage)
        self._set_text(detail, technical)
        self._render_record_summary(
            panel,
            paper_id,
            record,
            focus_stage=stage,
            technical_text=technical,
        )
        self._set_technical_details(f"论文 {paper_id} · {STAGE_LABELS[stage]}技术详情", record)

    def detail_text_for_record(self, paper_id: str, record: dict, focus_stage: str = "") -> str:
        stages = record.get("stages", {})
        lines = [
            f"标题：{record.get('title') or paper_id}",
            f"当前阶段：{record.get('current_stage_label', '-')}",
            f"下一步：{record.get('next_action_label') or action_label(record.get('next_action', ''))}",
            f"卡点：{self._smart_message_label(record.get('blocked_reason'))}",
            "",
        ]
        if focus_stage:
            stage = stages.get(focus_stage, {})
            admission = record.get("stage_admissions", {}).get(focus_stage, {})
            admission_mode = {
                "native": "本阶段原生任务",
                "automatic": "程序自动识别上游完成",
                "manual_override": "用户人工越级",
            }.get(str(admission.get("mode") or "") if isinstance(admission, dict) else "", "尚未准入")
            lines.extend(
                [
                    f"{STAGE_LABELS[focus_stage]}阶段：{status_token(stage)}",
                    f"进入方式：{admission_mode}",
                    f"细节：{self._stage_detail_label(focus_stage, stage.get('detail')) or '-'}",
                    f"产物：{'已生成' if stage.get('artifact') else '-'}",
                    f"调度模式：{scheduler_mode_label(stage.get('scheduler_mode'))}",
                    "",
                ]
            )
        lines.append("四阶段状态：")
        for stage_name in STAGE_ORDER:
            stage = stages.get(stage_name, {})
            lines.extend(
                [
                    f"- {stage.get('label') or STAGE_LABELS[stage_name]}：{status_token(stage)}",
                    f"  细节：{self._stage_detail_label(stage_name, stage.get('detail')) or '-'}",
                    f"  产物：{'已生成' if stage.get('artifact') else '-'}",
                    f"  调度模式：{scheduler_mode_label(stage.get('scheduler_mode'))}",
                ]
            )
        lines.extend(["", "下一步写回计划："])
        plan = self.orchestrator._plan_for(paper_id, record) if record else {}
        for item in plan.get("writes", []):
            lines.append(f"- {self._translate_plan_line(item)}")
        return "\n".join(lines)

    def update_queue_panel(self) -> None:
        queue = list(self.state_data.get("paper_queue", []))
        if not queue:
            self._set_text(self.queue_text, "统一队列为空。")
            return
        lines = []
        papers = self.state_data.get("papers", {})
        for index, paper_id in enumerate(queue, start=1):
            record = papers.get(paper_id, {})
            lines.append(
                f"{index}. {paper_id}\n"
                f"   当前阶段：{record.get('current_stage_label', '-')}\n"
                f"   下一步：{record.get('next_action_label') or action_label(record.get('next_action', ''))}\n"
                f"   卡点：{self._smart_message_label(record.get('blocked_reason'))}"
            )
        self._set_text(self.queue_text, "\n\n".join(lines))

    def update_log_panel(self) -> None:
        history = list(self.state_data.get("history", []))[-12:]
        last = self.state_data.get("last_scheduler_result", {})
        lines = ["最近调度结果：", self._payload_summary(last or {})]
        lines.extend(["", f"最近记录：{len(history)} 条"])
        lines.append("- 如需排错，可在总览打开“显示技术信息”。" if history else "- 暂无记录")
        self._set_text(self.log_text, "\n".join(lines))
        self._set_technical_details(
            "最近调度与历史记录",
            {"last_scheduler_result": last or {}, "history": list(reversed(history))},
        )

    def queue_selected(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先选择要加入统一队列的论文。")
            return
        self.state_data = self.orchestrator.queue_papers(paper_ids)
        self.refresh()

    def clear_queue(self) -> None:
        if not messagebox.askyesno("清空队列", "确认清空统一队列？"):
            return
        self.state_data = self.orchestrator.clear_queue()
        self.refresh()

    def archive_selected(self, requested_ids: list[str] | None = None) -> None:
        paper_ids = list(dict.fromkeys(requested_ids if requested_ids is not None else self.selected_ids()))
        paper_ids = [str(paper_id).strip() for paper_id in paper_ids if str(paper_id).strip()]
        if not paper_ids:
            messagebox.showinfo("未选择", "请先选择要归档的论文。", parent=self)
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        if not messagebox.askyesno(
            "归档选中论文（所有阶段）",
            "归档会统一执行以下操作：\n"
            "- 从总览、写论文、画图、选刊作者、投稿和实时任务中移除\n"
            "- 取消所有待执行队列与命令\n"
            "- 将论文、实验数据、图表和 SDK 会话记录集中移入可恢复归档区\n\n"
            "如果当前 AI Step 正在执行，程序会拒绝归档并提示先暂停。\n\n"
            f"{preview}",
            parent=self,
            default=messagebox.NO,
        ):
            return
        try:
            result = self.orchestrator.archive_papers(paper_ids, force_active=True)
        except Exception as exc:
            messagebox.showerror("归档失败", str(exc), parent=self)
            return
        self.refresh()
        self._show_operation_result(self.log_text, "论文全局归档结果", result)
        if result.get("skipped"):
            reasons = "\n".join(
                f"- {item.get('paper_id')}: {item.get('message') or item.get('reason')}"
                for item in result.get("skipped", [])[:8]
                if isinstance(item, dict)
            )
            messagebox.showwarning(
                "部分未归档",
                f"有部分论文未归档：\n\n{reasons or '原因已写入最近记录面板。'}",
                parent=self,
            )

    def purge_selected(self, requested_ids: list[str] | None = None) -> None:
        paper_ids = list(dict.fromkeys(requested_ids if requested_ids is not None else self.selected_ids()))
        paper_ids = [str(paper_id).strip() for paper_id in paper_ids if str(paper_id).strip()]
        if not paper_ids:
            messagebox.showinfo("未选择", "请先选择要彻底删除的论文。", parent=self)
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        if not messagebox.askyesno(
            f"彻底删除 {len(paper_ids)} 篇论文（不可恢复）",
            f"即将彻底删除以下 {len(paper_ids)} 篇论文。只会处理下方明确列出的编号：\n\n"
            f"{preview}\n\n"
            "删除范围包括：\n"
            "- 写作论文、参考文献、实验数据\n"
            "- 图表、插图、投稿包\n"
            "- V2 SDK 任务记录与独立会话日志\n"
            "- 回收区和全局归档区中的对应副本\n\n"
            "此操作不可恢复。请再次核对编号后确认继续。",
            parent=self,
            default=messagebox.NO,
            icon=messagebox.WARNING,
        ):
            return
        try:
            result = self.orchestrator.purge_papers(paper_ids, force_active=True)
        except Exception as exc:
            messagebox.showerror("彻底删除失败", str(exc), parent=self)
            return
        self.refresh()
        self._show_operation_result(self.log_text, "论文彻底删除结果", result)
        if result.get("skipped"):
            reasons = "\n".join(
                f"- {item.get('paper_id')}: {item.get('message') or item.get('reason')}"
                for item in result.get("skipped", [])[:8]
                if isinstance(item, dict)
            )
            messagebox.showwarning(
                "部分未彻底删除",
                f"有部分论文未删除：\n\n{reasons or '原因已写入最近记录面板。'}",
                parent=self,
            )

    def delete_selected(self) -> None:
        """Compatibility alias retained for older menu hooks."""

        self.archive_selected()

    def force_selected_into_stage(self, stage: str) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先选择要人工放行的论文。", parent=self)
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        upstream_index = STAGE_ORDER.index(stage) - 1
        upstream_label = STAGE_LABELS[STAGE_ORDER[upstream_index]]
        if not messagebox.askyesno(
            f"人工越级进入{STAGE_LABELS[stage]}",
            f"这些论文尚未通过“{upstream_label}完成”自动门槛。\n\n"
            f"确认仍将它们放入“{STAGE_LABELS[stage]}”页面吗？系统会保留橙色人工越级标记和操作记录，"
            "但不会在此确认框中直接启动 AI。\n\n"
            f"{preview}",
            parent=self,
        ):
            return
        reason = simpledialog.askstring(
            "越级原因",
            "可填写本次越级原因（将写入审计记录）：",
            initialvalue="当前上游结果已足够进入下一阶段",
            parent=self,
        )
        if reason is None:
            return
        try:
            self.state_data = self.orchestrator.force_stage_admission(paper_ids, stage, reason=reason)
        except Exception as exc:
            messagebox.showerror("人工越级失败", str(exc), parent=self)
            return
        self.refresh()
        messagebox.showinfo(
            "已人工放行",
            f"已将 {len(paper_ids)} 篇论文放入“{STAGE_LABELS[stage]}”页面。\n"
            "需要执行时，请在对应页面选择论文运行，或运行本阶段任务。",
            parent=self,
        )

    def clear_selected_stage_override(self, stage: str) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            return
        if not messagebox.askyesno(
            "取消人工越级",
            f"取消选中论文进入“{STAGE_LABELS[stage]}”的人工越级记录？\n\n"
            "已经生成的文件不会删除；论文是否继续显示将重新由上游完成状态决定。",
            parent=self,
        ):
            return
        try:
            self.state_data = self.orchestrator.clear_stage_admission_override(paper_ids, stage)
        except Exception as exc:
            messagebox.showerror("取消人工越级失败", str(exc), parent=self)
            return
        self.refresh()

    def apply_selected_stage_handoff(self, stage: str) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先选择要运行的本阶段论文。")
            return
        if stage == "writing":
            self.resume_selected_writing(paper_ids)
            return
        if stage == "figures":
            migration = [
                paper_id
                for paper_id in paper_ids
                if str(self.figure_task_rows.get(paper_id, {}).get("status") or "")
                == "migration_review"
            ]
            if migration and len(migration) == len(paper_ids):
                preview = "\n".join(migration[:12])
                if not messagebox.askyesno(
                    "重新排队历史画图任务",
                    "这些论文来自旧画图调度记录，尚未进入当前 V2 SDK journal。\n\n"
                    "程序会先验收已有画图规划：Step 2 合格时直接从 Step 3 继续；"
                    "不合格时才从 Step 2 重跑。旧桌面 Codex 会话仅归档，随后创建一个新的 V2 固定会话。\n\n"
                    + preview,
                    parent=self,
                ):
                    return
                try:
                    self.state_data = self.orchestrator.requeue_legacy_figure_papers(migration)
                except Exception as exc:
                    self._show_execution_blocked("重新排队历史画图任务", exc)
                    return
                self.refresh()
                self._show_operation_result(
                    self.log_text,
                    "历史画图任务已进入 V2",
                    self.state_data.get("last_scheduler_result", {}),
                    summary="已按现有产物契约选择恢复步骤并启动 V2 SDK 调度。",
                )
                return
            blocked = [
                paper_id
                for paper_id in paper_ids
                if str(self.figure_task_rows.get(paper_id, {}).get("status") or "")
                == "blocked"
            ]
            if blocked and len(blocked) == len(paper_ids):
                preview = "\n".join(blocked[:12])
                if not messagebox.askyesno(
                    "重新验收现有画图产物",
                    "这些论文当前停在产物验收卡点。\n\n"
                    "程序会只在本地重新检查现有 Step 3 产物；通过后直接标记完成，"
                    "不会重画、不会发送新的 Codex Turn，也不会消耗模型 Token。"
                    "如果现有产物仍不合格，会保留阻塞并显示具体原因。\n\n"
                    + preview,
                    parent=self,
                ):
                    return
                try:
                    self.state_data = self.orchestrator.revalidate_figure_papers(blocked)
                except Exception as exc:
                    self._show_execution_blocked("重新验收画图产物", exc)
                    return
                self.refresh()
                self._show_operation_result(
                    self.log_text,
                    "画图产物重新验收完成",
                    self.state_data.get("last_scheduler_result", {}),
                    summary="本次只执行本地产物契约检查，没有启动新的模型回合。",
                )
                return
            existing = [
                paper_id
                for paper_id in paper_ids
                if str(self.figure_task_rows.get(paper_id, {}).get("status") or "")
                in {"paused", "queued", "waiting_event"}
            ]
            if existing and len(existing) == len(paper_ids):
                preview = "\n".join(existing[:12])
                if not messagebox.askyesno(
                    "继续选中画图论文",
                    "确认继续这些已有画图任务吗？\n\n"
                    "程序会解除这些论文的单篇暂停，并在空闲并发槽位中从原固定会话和原 Step 边界继续；不会重建任务。\n\n"
                    + preview,
                    parent=self,
                ):
                    return
                try:
                    self.state_data = self.orchestrator.resume_figure_papers(existing)
                except Exception as exc:
                    self._show_execution_blocked("继续选中画图论文", exc)
                    return
                self.refresh()
                return
        if stage == "journal_author":
            existing = [
                paper_id
                for paper_id in paper_ids
                if str(self.journal_author_task_rows.get(paper_id, {}).get("status") or "")
                in {"paused", "queued", "waiting_event"}
            ]
            if existing and len(existing) == len(paper_ids):
                preview = "\n".join(existing[:12])
                if not messagebox.askyesno(
                    "继续选中选刊作者论文",
                    "确认继续这些已有任务吗？\n\n"
                    "程序会解除单篇暂停，并从原固定 SDK 会话边界继续；不会创建 AI 调度器会话。\n\n"
                    + preview,
                    parent=self,
                ):
                    return
                try:
                    self.state_data = self.orchestrator.resume_journal_author_papers(existing)
                except Exception as exc:
                    self._show_execution_blocked("继续选中选刊作者论文", exc)
                    return
                self.refresh()
                return
            blocked = [
                paper_id
                for paper_id in paper_ids
                if str(self.journal_author_task_rows.get(paper_id, {}).get("status") or "") == "blocked"
            ]
            if blocked and len(blocked) == len(paper_ids):
                preview = "\n".join(blocked[:12])
                if not messagebox.askyesno(
                    "重新执行选刊作者任务",
                    "这些论文当前停在输入或投稿包验收卡点。确认修复资料后重新排队吗？\n\n"
                    "程序会创建一个新的请求，但仍复用该论文的固定 SDK 会话，并按当前冻结流程执行。\n\n"
                    + preview,
                    parent=self,
                ):
                    return
                try:
                    self.state_data = self.orchestrator.requeue_journal_author_papers(blocked)
                except Exception as exc:
                    self._show_execution_blocked("重新执行选刊作者任务", exc)
                    return
                self.refresh()
                return

        expected_action = {
            "writing": "queue_writing",
            "figures": "queue_figures",
            "journal_author": "queue_journal_author",
            "submission": "queue_submission",
        }.get(stage, "")
        papers = self.state_data.get("papers", {})
        eligible = [
            paper_id
            for paper_id in paper_ids
            if str(papers.get(paper_id, {}).get("current_stage") or "") == stage
            and str(papers.get(paper_id, {}).get("next_action") or "") == expected_action
        ]
        if not eligible:
            messagebox.showinfo(
                "没有可导入项",
                f"选中的论文当前不需要启动“{STAGE_LABELS[stage]}”阶段。\n\n"
                "正常流程会在上游完成后自动出现在本页；特殊情况请在上游页面右键选择人工越级。",
            )
            return

        preview = "\n".join(eligible[:12])
        if len(eligible) > 12:
            preview += f"\n... 还有 {len(eligible) - 12} 篇"
        if not messagebox.askyesno(
            f"运行{STAGE_LABELS[stage]}",
            f"确认运行这些“{STAGE_LABELS[stage]}”任务吗？\n\n"
            + (
                "这会导入最新写作稿，并立即启动每篇论文的 V2 SDK 固定画图会话。\n\n"
                if stage == "figures"
                else (
                    "这会导入最新画图投稿包，并立即启动每篇论文的 V2 SDK 固定选刊作者会话。\n\n"
                    if stage == "journal_author"
                    else "系统会自动读取上游产物并加入本阶段队列；随后可由本阶段调度器执行。\n\n"
                )
            )
            + f"{preview}",
        ):
            return

        results = []
        try:
            for paper_id in eligible:
                results.append(self.orchestrator.advance_once(apply=True, paper_id=paper_id))
        except Exception as exc:
            self._show_execution_blocked("运行阶段任务", exc)
            return
        self.refresh()
        self._show_operation_result(self.log_text, "本阶段任务已处理", results)

    def queue_selected_figures_from_step1(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在图表与插图页选择论文。")
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        if not messagebox.askyesno(
            "从第 1 步重新执行图表流程",
            "确认把选中的论文从第 1 步加入完整图表流程吗？\n\n"
            "系统会立即启动 V2 SDK 固定会话，并在同一会话依次执行 Step 1、2、3。\n\n"
            f"{preview}",
        ):
            return
        state = self.orchestrator.queue_figures_from_step1(paper_ids)
        self.state_data = state
        self.refresh()
        self._show_operation_result(
            self.log_text,
            "图表流程已从第 1 步重新排队",
            state.get("last_scheduler_result", {}),
        )

    def continue_selected_figures_from_step3(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在图表与插图页选择等待真实数据的论文。", parent=self)
            return
        eligible = [
            paper_id
            for paper_id in paper_ids
            if str(
                self.state_data.get("papers", {})
                .get(paper_id, {})
                .get("stages", {})
                .get("figures", {})
                .get("status")
                or ""
            )
            == "waiting_data"
        ]
        if not eligible:
            messagebox.showinfo(
                "无需从 Step 3 继续",
                "选中的论文当前没有处于“等待真实实验数据”状态。",
                parent=self,
            )
            return
        preview = "\n".join(eligible[:12])
        if not messagebox.askyesno(
            "确认真实数据已补齐",
            "请确认所需真实 CSV/实验结果已经手动放入对应论文目录。\n\n"
            "系统不会改写数据，只会在原固定会话中重新执行 Step 3、重新生成图和提交包。\n\n"
            f"{preview}",
            parent=self,
        ):
            return
        try:
            state = self.orchestrator.queue_figures_from_step3(eligible)
        except Exception as exc:
            self._show_execution_blocked("继续画图 Step 3", exc)
            return
        self.state_data = state
        self.refresh()
        self._show_operation_result(
            self.log_text,
            "画图 Step 3 已重新启动",
            state.get("last_scheduler_result", {}),
            summary=f"已让 {len(eligible)} 篇论文从 Step 3 继续；不会重跑参考研究或画图规划。",
        )

    def resume_selected_writing(self, paper_ids: list[str]) -> None:
        papers = self.state_data.get("papers", {})
        eligible = []
        skipped = []
        for paper_id in paper_ids:
            writing = papers.get(paper_id, {}).get("stages", {}).get("writing", {})
            status = str(writing.get("status") or "missing")
            if status == "complete":
                skipped.append(paper_id)
            else:
                eligible.append(paper_id)
        if not eligible:
            messagebox.showinfo("没有可续写项", "选中的论文写作阶段已经完成，或没有可续写的写作记录。")
            return
        preview = "\n".join(eligible[:12])
        if len(eligible) > 12:
            preview += f"\n... 还有 {len(eligible) - 12} 篇"
        skipped_text = f"\n\n将跳过 {len(skipped)} 篇已完成论文。" if skipped else ""
        if not messagebox.askyesno(
            "继续选中写作",
            "确认把这些论文标记为写作排队，并启动写作调度器吗？\n\n"
            "默认采用单活模式，将暂停画图、选刊作者和旧投稿调度器。"
            f"{skipped_text}\n\n{preview}",
        ):
            return
        try:
            state = self.orchestrator.resume_writing_papers(eligible, single_active=True)
        except Exception as exc:
            self._show_execution_blocked("继续写作", exc)
            return
        self.state_data = state
        self.refresh()
        error = _stage_result_error(state, "writing")
        if error:
            self._show_operation_result(
                self.log_text,
                "继续写作失败",
                state.get("last_scheduler_result", {}),
                summary=error,
            )
            messagebox.showerror("继续写作失败", error, parent=self)
            return
        self._show_operation_result(
            self.log_text,
            "写作继续/重试请求已提交",
            state.get("last_scheduler_result", {}),
            summary=(
                f"已提交 {len(eligible)} 篇选中论文的继续/安全重试请求。"
                "可恢复卡点会先解除再执行；运行结果和具体卡点以实时任务表为准。"
            ),
        )
        self.status_var.set(
            f"已提交 {len(eligible)} 篇选中论文的继续/安全重试请求；请查看实时任务状态。"
        )

    def queue_selected_figure_image2_revisions(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在画图页选择要修正的论文。")
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        if not messagebox.askyesno(
            "使用图像模型重绘图 1/2",
            "确认为选中的论文排队修正图 1 和图 2 吗？\n\n"
            "这会写入画图项目的单图修改命令，并启动画图调度器。"
            "调度器会使用图像模型重新生成概念图或机制图，不会暂停其他阶段调度器。\n\n"
            f"{preview}",
        ):
            return
        try:
            state = self.orchestrator.queue_figure_image2_revisions(paper_ids, ["Figure 1", "Figure 2"])
        except Exception as exc:
            self._show_execution_blocked("重绘图 1/2", exc)
            return
        self.state_data = state
        self.refresh()
        self._show_operation_result(
            self.log_text,
            "图 1/2 重绘任务已排队",
            state.get("last_scheduler_result", {}),
        )

    def queue_custom_figure_revisions(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在画图页选择要修正的论文。")
            return
        figure_text = simpledialog.askstring(
            "修指定图",
            "要修改哪张图？例如：图 3、Figure 3、fig:system 或图 1 和图 2",
            parent=self,
        )
        if figure_text is None:
            return
        figures = self.parse_figure_targets(figure_text)
        if not figures:
            messagebox.showinfo("没有图号", "请输入至少一个图号或 label。")
            return
        request = simpledialog.askstring(
            "修改要求",
            "具体怎么改？留空则按当前图像重绘规则修正。",
            parent=self,
        )
        if request is None:
            return
        preview = "\n".join(paper_ids[:8])
        if len(paper_ids) > 8:
            preview += f"\n... 还有 {len(paper_ids) - 8} 篇"
        figure_preview = ", ".join(figures)
        request_preview = request.strip() or "按当前图像重绘规则修正"
        if not messagebox.askyesno(
            "确认修指定图",
            f"确认为选中的论文排队修图吗？\n\n图：{figure_preview}\n要求：{request_preview}\n\n{preview}",
        ):
            return
        try:
            state = self.orchestrator.queue_figure_image2_revisions(paper_ids, figures, request.strip())
        except Exception as exc:
            self._show_execution_blocked("修改指定图", exc)
            return
        self.state_data = state
        self.refresh()
        self._show_operation_result(
            self.log_text,
            "指定图修改任务已排队",
            state.get("last_scheduler_result", {}),
        )

    @staticmethod
    def parse_figure_targets(text: str) -> list[str]:
        figures: list[str] = []

        def add(value: str) -> None:
            value = value.strip()
            if value and value not in figures:
                figures.append(value)

        # Prefer extracting explicit figure references from free text. This is
        # robust to localized separators that may be mojibaked by legacy Tk.
        for match in re.finditer(r"(?i)(?:figure|fig\.?|f)\s*([0-9]+[a-z]?)", text):
            add(f"Figure {match.group(1)}")
        for match in re.finditer(r"(?i)\bfig:[A-Za-z0-9_.:-]+\b", text):
            add(match.group(0))
        if figures:
            return figures

        normalized = text
        for sep in [",", ";", "|", "/", "\\", "\n", "\t"]:
            normalized = normalized.replace(sep, ",")
        for raw in normalized.split(","):
            add(raw)
        return figures

    def advance_dry_run(self) -> None:
        result = self.orchestrator.advance_once(apply=False)
        self.refresh()
        self._show_operation_result(self.log_text, "队首下一步预演完成", result)

    def advance_apply(self) -> None:
        queue = self.state_data.get("paper_queue", [])
        if not queue:
            messagebox.showinfo("队列为空", "请先把论文加入统一队列。")
            return
        if not messagebox.askyesno("执行队首一步", f"确认写回旧项目状态并推进队首论文？\n\n{queue[0]}"):
            return
        try:
            result = self.orchestrator.advance_once(apply=True)
        except Exception as exc:
            self._show_execution_blocked("执行队首一步", exc)
            return
        self.refresh()
        self._show_operation_result(self.log_text, "队首一步执行完成", result)

    def start_new_writing_session(self) -> None:
        writing_state = self.orchestrator.adapters["writing"].load_state()
        writing_settings = writing_state.get("settings", {}) if isinstance(writing_state, dict) else {}
        raw_limit = (
            writing_settings.get("max_parallel_workers", WRITING_DEFAULT_PARALLEL_WORKERS)
            if isinstance(writing_settings, dict)
            else WRITING_DEFAULT_PARALLEL_WORKERS
        )
        try:
            configured_limit = min(max(int(raw_limit), 1), WRITING_MAX_PARALLEL_WORKERS)
        except (TypeError, ValueError):
            configured_limit = WRITING_DEFAULT_PARALLEL_WORKERS
        count = simpledialog.askinteger(
            "新建论文任务",
            f"本次要新建几篇论文？\n\n"
            f"这里填写的是“新建数量”，不是并发量。当前全局写作并发上限为 {configured_limit}；"
            "可在写论文页的“调度设置与提示词”中调整。",
            initialvalue=1,
            minvalue=1,
            maxvalue=WRITING_MAX_PARALLEL_WORKERS,
            parent=self,
        )
        if count is None:
            return
        if not messagebox.askyesno(
            "从 0 开始写论文",
            f"确定新建并优先运行 {count} 篇论文吗？\n\n"
            f"每篇论文固定使用一个 SDK 会话和独立目录，Step 1→10 在同一会话内依次执行。\n"
            "如果已有 Step 正在执行，会先让该 Step 正常结束，再重新排程并优先启动本次新任务。\n"
            "本按钮只运行本次新建的论文；旧队列只有点击“运行全部写作任务”才会恢复。\n"
            "完成后不会自动新建下一批论文。\n"
            "EXE 使用本地事件调度，不再创建模型调度器会话；默认会暂停画图、选刊作者和投稿阶段。",
        ):
            return
        def completed(state: dict[str, Any]) -> None:
            self.state_data = state
            self.refresh()
            error = _stage_result_error(state, "writing")
            if error:
                self._show_operation_result(
                    self.log_text,
                    "新建论文任务失败",
                    state.get("last_scheduler_result", {}),
                    summary=error,
                )
                messagebox.showerror("新建论文任务失败", error, parent=self)
                return
            summary = (
                f"已创建 {count} 篇论文并请求单独运行本批任务。"
                "若已有 Step 正在执行，本批任务会在当前 Step 结束后启动；旧队列不会自动恢复。"
            )
            self._show_operation_result(
                self.log_text,
                "已从 0 开始创建论文任务",
                state.get("last_scheduler_result", {}),
                summary=summary,
            )
            messagebox.showinfo("论文任务已加入队列", summary, parent=self)

        self._run_background_task(
            "新建论文任务",
            lambda: self.orchestrator.start_new_writing_sessions(count, single_active=True),
            completed,
        )

    def start_stage_parallel(self, stage: str) -> None:
        if not messagebox.askyesno(
            f"并行启动{STAGE_LABELS[stage]}",
            f"确认启动“{STAGE_LABELS[stage]}”调度器吗？\n\n"
            "不会暂停其它阶段；适合流水线并行处理已经导入到各阶段队列里的论文。",
        ):
            return
        self._start_stage_in_background(stage, single_active=False)

    def start_stage_single_active(self, stage: str) -> None:
        if stage == "writing":
            title = "运行全部写作任务"
            prompt = (
                "确认启动整个写作队列吗？\n\n"
                "作用范围：所有排队或暂停的论文，不是当前选中的某一篇；不会新建论文。\n"
                "程序会按“同时运行的论文上限”分配会话，并暂停画图、选刊作者和投稿阶段。"
            )
        elif stage == "journal_author":
            title = "运行全部选刊作者任务"
            prompt = (
                "确认启动整个选刊作者队列吗？\n\n"
                "程序会按并发上限直接分配固定 SDK 会话；每篇论文按各自冻结的合并/精细流程执行。\n"
                "不会创建 AI 调度器会话，并会暂停其他阶段调度器。"
            )
        else:
            title = f"单活启动{STAGE_LABELS[stage]}"
            prompt = (
                f"确认启动“{STAGE_LABELS[stage]}”调度器，并暂停其他三个阶段调度器？\n\n"
                "这可以避免多个阶段同时增加模型用量或争用浏览器。"
            )
        if not messagebox.askyesno(
            title,
            prompt,
        ):
            return
        self._start_stage_in_background(stage, single_active=True)

    def _start_stage_in_background(self, stage: str, *, single_active: bool) -> None:
        label = STAGE_LABELS[stage]

        def completed(state: dict[str, Any]) -> None:
            self.state_data = state
            self.refresh()
            error = _stage_result_error(state, stage)
            if error:
                self._show_operation_result(
                    self.log_text,
                    f"启动{label}失败",
                    state.get("last_scheduler_result", {}),
                    summary=error,
                )
                messagebox.showerror(f"启动{label}失败", error, parent=self)
                return
            if stage == "writing":
                summary = "全部写作任务已进入自动调度；每篇论文的实时 Step 可在上方任务表查看。"
            elif stage == "figures":
                summary = "全部已排队画图任务已进入 V2 SDK 调度；每篇论文的 Step 和固定会话可在上方实时任务表查看。"
            elif stage == "journal_author":
                summary = "全部已排队选刊作者任务已进入 V2 SDK 调度；每篇论文的状态、模型和固定会话可在上方实时任务表查看。"
            else:
                minutes = int(LEGACY_PROJECTS[stage].get("heartbeat_minutes") or 5)
                summary = f"{label}调度器已启动；已有排队任务通常会在 {minutes} 分钟内被处理。"
            self._show_operation_result(
                self.log_text,
                f"{label}调度器已启动",
                state.get("last_scheduler_result", {}),
                summary=summary,
            )
            messagebox.showinfo(f"{label}调度器已启动", summary, parent=self)

        self._run_background_task(
            f"启动{label}调度器",
            lambda: self.orchestrator.start_stage(stage, single_active=single_active),
            completed,
        )

    def pause_stage(self, stage: str) -> None:
        if stage == "writing":
            title = "暂停全部写作"
            prompt = (
                "确认暂停整个写作队列吗？\n\n"
                "作用范围：全部论文，不是选中的某一篇。正在执行的 Step 会正常完成；"
                "完成后不再发送下一 Step。"
            )
        elif stage == "figures":
            title = "暂停全部画图"
            prompt = (
                "确认暂停整个画图队列吗？\n\n"
                "作用范围：全部画图论文。正在执行的 SDK Step 会正常完成；"
                "完成后不再发送下一 Step，不会强制中断当前图片生成。"
            )
        elif stage == "journal_author":
            title = "暂停全部选刊作者"
            prompt = (
                "确认暂停整个选刊作者队列吗？\n\n"
                "尚未启动的论文不会再启动；正在执行的当前 Step 会正常结束，不会强制中断。"
            )
        else:
            title = f"暂停{STAGE_LABELS[stage]}"
            prompt = f"确认暂停“{STAGE_LABELS[stage]}”调度器？"
        if not messagebox.askyesno(title, prompt):
            return
        state = self.orchestrator.pause_stage(stage)
        self.refresh()
        result = state.get("last_scheduler_result", {}).get("result", {})
        if isinstance(result, dict) and not result.get("ok"):
            messagebox.showwarning(
                "暂停状态未能确认",
                str(result.get("warning") or result.get("error") or "未检测到可控制的旧版调度任务。"),
                parent=self,
            )

    def pause_selected_figures(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在图表与插图页选择要暂停的论文。", parent=self)
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        if not messagebox.askyesno(
            "暂停选中画图论文",
            "只暂停下面选中的论文吗？\n\n"
            "当前正在执行的 SDK Step 会正常完成，随后不再发送下一 Step；其他画图论文和全局调度器继续运行。\n\n"
            + preview,
            parent=self,
        ):
            return
        try:
            self.state_data = self.orchestrator.pause_figure_papers(paper_ids)
        except Exception as exc:
            messagebox.showerror("暂停选中论文失败", str(exc), parent=self)
            return
        self.refresh()

    def pause_selected_journal_author(self) -> None:
        paper_ids = self.selected_ids()
        if not paper_ids:
            messagebox.showinfo("未选择", "请先在选刊与作者页选择要暂停的论文。", parent=self)
            return
        preview = "\n".join(paper_ids[:12])
        if len(paper_ids) > 12:
            preview += f"\n... 还有 {len(paper_ids) - 12} 篇"
        if not messagebox.askyesno(
            "暂停选中选刊作者论文",
            "只暂停下面选中的论文吗？\n\n"
            "尚未启动的论文会暂停；已经执行中的当前 Step 会正常结束，其他论文继续运行。\n\n"
            + preview,
            parent=self,
        ):
            return
        try:
            self.state_data = self.orchestrator.pause_journal_author_papers(paper_ids)
        except Exception as exc:
            messagebox.showerror("暂停选中论文失败", str(exc), parent=self)
            return
        self.refresh()

    def pause_all(self) -> None:
        if not messagebox.askyesno(
            "紧急停止全部调度",
            "确认暂停写作、图表、选刊作者、稳定投稿和智能投稿的后台调度吗？\n\n"
            "智能投稿将进入手动暂停状态。"
            "已在 Codex 中运行的执行任务不会被强制终止，需要时请在对应任务中停止。",
        ):
            return
        legacy = self.orchestrator.pause_all()
        smart = self._pause_smart_submission_runtime(reason="ui_emergency_stop")
        self.refresh()
        legacy_results = legacy.get("last_scheduler_result", {}).get("results", {})
        unverified = [
            stage
            for stage, result in (legacy_results.items() if isinstance(legacy_results, dict) else [])
            if not isinstance(result, dict) or not result.get("ok")
        ]
        title = "后台调度暂停请求已完成" if unverified else "全部后台调度已暂停"
        self._show_operation_result(
            self.log_text,
            title,
            {"stable_submission": legacy.get("last_scheduler_result", {}), "smart_submission": smart},
            summary=(
                "本地阶段已设为手动，但以下 V1 Codex 调度器无法确认已暂停："
                + "、".join(STAGE_LABELS.get(stage, stage) for stage in unverified)
                if unverified
                else "全部可控制的后台调度已暂停。"
            ),
        )

    def show_scheduler_status(self) -> None:
        def done(result: dict[str, Any]) -> None:
            self._scheduler_status_cache = result
            self._show_operation_result(self.log_text, "后台调度运行状态", result)
            self.status_var.set("后台调度运行状态已更新")

        self._run_background_task(
            "读取全部运行状态",
            self.orchestrator.scheduler_status,
            done,
        )

    def show_stage_scheduler_status(self, stage: str) -> None:
        def done(all_status: dict[str, Any]) -> None:
            self._scheduler_status_cache = all_status
            result = all_status.get(stage, {})
            detail = self.stage_detail_texts.get(stage)
            if detail is not None:
                self._show_operation_result(detail, f"{STAGE_LABELS[stage]}后台调度状态", result)
            self.status_var.set(f"{STAGE_LABELS[stage]}运行状态已更新")

        self._run_background_task(
            f"读取{STAGE_LABELS[stage]}运行状态",
            self.orchestrator.scheduler_status,
            done,
        )

    def on_tab_changed(self) -> None:
        tab = self.current_tab_key()
        if tab in self.stage_trees:
            has_cached_live_state = {
                "writing": bool(self._writing_runtime_status or self.writing_task_rows),
                "figures": bool(self._figure_runtime_status or self.figure_task_rows),
                "journal_author": bool(
                    self._journal_author_runtime_status or self.journal_author_task_rows
                ),
                "submission": True,
            }.get(tab, True)
            if self.stage_trees[tab].selection() or has_cached_live_state:
                self.update_stage_detail(tab)
            else:
                self._set_text(
                    self.stage_detail_texts[tab],
                    "正在后台读取实时状态；切换页面不会阻塞窗口。",
                )
        elif tab != "smart_submission":
            self.update_detail()
        self._request_ui_refresh(
            tab=tab,
            full=False,
            reason=f"{tab} 状态",
            announce=False,
        )

    def open_summary(self) -> None:
        path = Path(UNIFIED_SUMMARY_PATH)
        if not path.exists():
            self.refresh()
        subprocess.Popen(["notepad.exe", str(path)])

    def open_backups(self) -> None:
        path = Path(BACKUP_DIR)
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer.exe", str(path)])

    def open_deleted(self) -> None:
        path = Path(DELETED_DIR)
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer.exe", str(path)])

    def open_path(self, path: Path) -> None:
        path = Path(path)
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".toml", ".txt"}:
            subprocess.Popen(["notepad.exe", str(path)])
            return
        subprocess.Popen(["explorer.exe", str(path)])

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    _TECHNICAL_REDACTION = "[REDACTED]"
    _SENSITIVE_TECHNICAL_KEY_PARTS = (
        "password",
        "passwd",
        "credential_ref",
        "secret",
        "ciphertext",
        "nonce",
        "confirmation_phrase",
        "authorization",
        "api_key",
        "apikey",
        "cookie",
        "token",
        "otp",
        "one_time_password",
        "response",
        "signature",
        "session",
    )
    _EMBEDDED_URL_PATTERN = re.compile(r"(?i)https?://[^\s<>'\"]+")
    _AUTHORIZATION_PATTERN = re.compile(
        r"(?im)(\bauthorization\b\s*[:=]\s*)[^\r\n]*"
    )
    _COOKIE_HEADER_PATTERN = re.compile(r"(?im)(\b(?:cookie|set-cookie)\b\s*:\s*)[^\r\n]+")
    _SECRET_ASSIGNMENT_PATTERN = re.compile(
        r"(?ix)"
        r"((?:['\"])?\b(?:password|passwd|pwd|credential(?:[\s_-]*ref(?:erence)?)|"
        r"access(?:[\s_-]*token)|refresh(?:[\s_-]*token)|token|secret|cookie|set-cookie|otp|"
        r"one(?:[\s_-]*time)?[\s_-]*password|api(?:[\s_-]*key)|apikey|response|signature|"
        r"session(?:[\s_-]*(?:id|token))?|nonce|confirmation(?:[\s_-]*phrase))\b"
        r"(?:['\"])?\s*[:=]\s*)"
        r"(?:['\"][^'\"]*['\"]|[^\s,;\]}\)]+)"
    )
    _BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;]+")

    @classmethod
    def _technical_key_is_sensitive(cls, key: Any) -> bool:
        token = str(key or "").strip().casefold().replace("-", "_").replace(" ", "_")
        compact = re.sub(r"[^a-z0-9]", "", token)
        return compact == "state" or any(
            re.sub(r"[^a-z0-9]", "", marker) in compact
            for marker in cls._SENSITIVE_TECHNICAL_KEY_PARTS
        )

    @classmethod
    def _sanitize_embedded_url(cls, match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;)]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parts = urlsplit(raw)
            host = parts.hostname or ""
            if not host:
                raise ValueError("missing host")
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
            netloc = f"{host}:{port}" if port is not None else host
            clean = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        except (TypeError, ValueError):
            clean = "https://[INVALID_URL]"
        return clean + trailing

    @classmethod
    def _sanitize_technical_text(cls, value: Any) -> str:
        text = str(value or "")
        text = cls._EMBEDDED_URL_PATTERN.sub(cls._sanitize_embedded_url, text)
        text = cls._COOKIE_HEADER_PATTERN.sub(
            lambda match: match.group(1) + cls._TECHNICAL_REDACTION,
            text,
        )
        text = cls._AUTHORIZATION_PATTERN.sub(
            lambda match: match.group(1) + cls._TECHNICAL_REDACTION,
            text,
        )
        text = cls._SECRET_ASSIGNMENT_PATTERN.sub(
            lambda match: match.group(1) + cls._TECHNICAL_REDACTION,
            text,
        )
        return cls._BEARER_PATTERN.sub(cls._TECHNICAL_REDACTION, text)

    @classmethod
    def _redact_technical_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                clean_key = cls._sanitize_technical_text(key)
                result[clean_key] = (
                    cls._TECHNICAL_REDACTION
                    if cls._technical_key_is_sensitive(key)
                    else cls._redact_technical_payload(item)
                )
            return result
        if isinstance(value, list):
            return [cls._redact_technical_payload(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._redact_technical_payload(item) for item in value)
        if isinstance(value, str):
            return cls._sanitize_technical_text(value)
        if isinstance(value, BaseException):
            return cls._sanitize_technical_text(f"{type(value).__name__}: {value}")
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return cls._sanitize_technical_text(value)

    def _set_technical_details(self, title: str, payload: Any) -> None:
        clean = self._redact_technical_payload(payload)
        self._set_text(
            self.technical_text,
            f"{title}\n\n" + json.dumps(clean, ensure_ascii=False, indent=2),
        )

    def _payload_summary(self, payload: Any) -> str:
        if isinstance(payload, list):
            return f"已处理 {len(payload)} 项。"
        if not isinstance(payload, dict):
            return "操作已完成。"
        if payload.get("ok") is False:
            return "操作未完成；请按界面提示处理，排错时可显示技术信息。"
        counts: list[str] = []
        for key, label in (
            ("processed", "已处理"),
            ("created_count", "已创建"),
            ("updated_count", "已更新"),
            ("queued_count", "已排队"),
            ("skipped_count", "已跳过"),
            ("candidate_count", "待处理"),
        ):
            value = payload.get(key)
            if isinstance(value, int):
                counts.append(f"{label} {value} 项")
        if counts:
            return "；".join(counts) + "。"
        return "操作已完成；排错时可在总览显示技术信息。"

    def _show_operation_result(
        self,
        target: tk.Text,
        title: str,
        payload: Any,
        *,
        summary: str = "",
    ) -> None:
        message = summary.strip() or self._payload_summary(payload)
        result_text = f"{title}\n\n{message}\n\n如需排错，请打开技术信息。"
        self._set_text(target, result_text)
        self._set_technical_details(title, payload)
        smart_target = getattr(self, "smart_submission_text", None)
        smart_panel = getattr(self, "smart_submission_detail_panel", None)
        if target is smart_target and smart_panel is not None:
            failed = isinstance(payload, dict) and payload.get("ok") is False
            clean_payload = self._redact_technical_payload(payload)
            try:
                technical_text = f"{title}\n\n" + json.dumps(
                    clean_payload,
                    ensure_ascii=False,
                    indent=2,
                )
            except (TypeError, ValueError):
                technical_text = result_text
            smart_panel.render(
                title=title,
                status="未完成" if failed else "已完成",
                tone="danger" if failed else "success",
                headline=message,
                sections=(
                    {
                        "title": "处理结果",
                        "body": (
                            "状态与论文列表已同步刷新。",
                            "如需定位技术原因，可切换到右上角“技术日志”。",
                        ),
                        "tone": "danger" if failed else "success",
                    },
                ),
                technical_text=technical_text,
            )

    def _translate_plan_line(self, text: str) -> str:
        replacements = {
            "commands += resume_session": "写入继续写论文命令",
            "sessions[paper_id].status=queued": "标记写论文排队",
            "figures/papers/<paper_id>/input.tex <- writing draft_v6.tex": "把 draft_v6.tex 导入画图项目 input.tex",
            "figure_pipeline_state.json: paper_queue += paper_id; commands += run_pipeline_late": "加入画图队列并写入 02-03 自动画图命令",
            "journal/papers/<paper_id>/source_submit <- figure submit/": "把 submit/ 导入选刊作者项目",
            "journal_author_state.json: paper_queue += paper_id; commands += run_pipeline_02_03": "加入选刊作者队列并写入 02-03 命令",
            "auto_submission_panel_state.json: papers[paper_id].source_package_dir=journal package/": "登记投稿包路径",
            "auto_submission_panel_state.json: paper_queue += paper_id; settings.scheduling_mode=run_once": "加入自动投稿队列并请求一轮调度",
            "none; waiting for upstream/current stage": "不写入；等待当前阶段或上游调度器完成",
            "none": "不写入",
        }
        return replacements.get(text, text)


def main() -> None:
    log_path = configure_ui_logging()
    install_global_exception_hooks()
    LOGGER.info("Starting Unified Paper Orchestrator UI; log=%s", log_path)
    app = UnifiedControlPanel()
    try:
        app.mainloop()
    finally:
        LOGGER.info("Unified Paper Orchestrator UI stopped")


if __name__ == "__main__":
    main()
