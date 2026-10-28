from __future__ import annotations

from typing import Any

from .config import STAGE_LABELS, STAGE_ORDER

COMPLETE_STATUSES = {"complete", "submitted", "active", "accepted", "monitoring"}
WAIT_STATUSES = {
    "queued",
    "creating_thread",
    "starting",
    "running",
    "waiting",
    "waiting_event",
    "waiting_data",
    "paused",
    "waiting_approval",
}
BLOCKED_STATUSES = {
    "blocked",
    "stopped",
    "deleted",
    "missing_upstream",
    "manual",
    "manual_required",
    "needs_user",
}

ACTION_LABELS = {
    "queue_writing": "继续写论文",
    "queue_figures": "导入画图",
    "queue_journal_author": "生成投稿包",
    "queue_submission": "加入投稿队列",
    "wait_writing": "等待写论文",
    "wait_figures": "等待画图",
    "wait_journal_author": "等待投稿包",
    "wait_submission": "等待投稿调度器",
    "blocked": "已阻塞",
    "none": "无需动作",
}


def stage_status(
    status: str,
    *,
    label: str,
    detail: str = "",
    artifact: str = "",
    updated_at: str = "",
    raw_status: str = "",
    queue_position: str = "",
    scheduler_mode: str = "",
    step_done: int | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": status,
        "label": label,
        "detail": detail,
        "artifact": artifact,
        "updated_at": updated_at,
        "raw_status": raw_status or status,
        "queue_position": queue_position,
        "scheduler_mode": scheduler_mode,
    }
    if step_done is not None:
        data["step_done"] = step_done
    return data


def is_complete(stage: dict[str, Any]) -> bool:
    return str(stage.get("status") or "") in COMPLETE_STATUSES


def status_token(stage: dict[str, Any]) -> str:
    status = str(stage.get("status") or "missing")
    if status == "complete":
        return "完成"
    if status == "submitted":
        return "已投"
    if status == "active":
        return "已投"
    if status == "accepted":
        return "已接收"
    if status == "monitoring":
        return "监测中"
    if status == "ready":
        return "就绪"
    if status == "queued":
        return "排队"
    if status == "running":
        return "运行"
    if status in {"creating_thread", "starting"}:
        return "启动中"
    if status == "waiting_event":
        return "等回合"
    if status == "waiting_data":
        return "等数据"
    if status == "selection_complete":
        return "选刊完成"
    if status == "needs_user":
        return "待人工"
    if status == "paused":
        return "暂停"
    if status == "blocked":
        return "阻塞"
    if status == "deleted":
        return "删除"
    if status == "stopped":
        return "停止"
    if status == "missing_upstream":
        return "等上游"
    if status == "incomplete":
        return "未完成"
    if status == "migration_review":
        return "待重排"
    if status == "unknown":
        return "未知"
    return "缺失"


def action_label(action: str) -> str:
    token = str(action or "")
    return ACTION_LABELS.get(token, "-" if not token else "需核对，详见审计")


def row_state(record: dict[str, Any]) -> str:
    action = str(record.get("next_action") or "")
    if action == "blocked":
        return "blocked"
    if action.startswith("wait_"):
        return "waiting"
    if action in {"queue_submission"}:
        return "ready_submit"
    if action.startswith("queue_"):
        return "ready"
    stages = record.get("stages", {})
    if str(record.get("current_stage")) == "done":
        return "done"
    if stages and all(is_complete(stage) for stage in stages.values()):
        return "done"
    return "normal"


def current_stage_for(stages: dict[str, dict[str, Any]]) -> str:
    for stage_name in STAGE_ORDER:
        if not is_complete(stages.get(stage_name, {})):
            return stage_name
    return "done"


def undergraduate_stage_boundary(stages: dict[str, dict[str, Any]], current_stage: str) -> str:
    """Undergraduate delivery ends after figures, never at journal submission."""
    from .writing_prompt_schemes import is_undergraduate
    if is_undergraduate({"prompt_scheme": stages.get("writing", {}).get("prompt_scheme", "journal")}):
        if current_stage in {"journal_author", "submission", "done"}:
            if (is_complete(stages.get("writing", {})) and is_complete(stages.get("figures", {}))
                    and stages.get("figures", {}).get("word_delivery_ok") is True):
                return "done"
            return "figures" if is_complete(stages.get("writing", {})) else "writing"
    return current_stage


def action_for(
    stage_name: str,
    stages: dict[str, dict[str, Any]],
    *,
    allow_upstream_incomplete: bool = False,
) -> tuple[str, str]:
    if stage_name == "done":
        return "none", ""

    stage = stages.get(stage_name, {})
    status = str(stage.get("status") or "missing")
    detail = str(stage.get("detail") or "")
    if status in COMPLETE_STATUSES:
        return "none", detail
    if status in WAIT_STATUSES:
        return f"wait_{stage_name}", detail
    if status in BLOCKED_STATUSES:
        return "blocked", detail or "该阶段已阻塞、删除或停止"

    if stage_name == "writing":
        return "queue_writing", detail

    upstream_index = STAGE_ORDER.index(stage_name) - 1
    if upstream_index >= 0:
        upstream_name = STAGE_ORDER[upstream_index]
        upstream = stages.get(upstream_name, {})
        if not is_complete(upstream) and not allow_upstream_incomplete:
            return f"wait_{upstream_name}", f"等待{STAGE_LABELS[upstream_name]}完成"

    if stage_name == "figures":
        return "queue_figures", detail
    if stage_name == "journal_author":
        return "queue_journal_author", detail
    if stage_name == "submission":
        return "queue_submission", detail
    return "none", detail
