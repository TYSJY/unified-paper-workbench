from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from collections.abc import Callable
from typing import Any, Mapping


STAGE_ORDER = ("writing", "figures", "journal_author", "submission")
STAGE_LABELS = {
    "writing": "写论文",
    "figures": "图表与插图",
    "journal_author": "选刊与作者",
    "submission": "投稿中心",
    "done": "全部完成",
}
STATUS_LABELS = {
    "missing": "未开始",
    "incomplete": "未完成",
    "ready": "就绪",
    "queued": "排队中",
    "creating_thread": "创建会话",
    "starting": "启动中",
    "running": "运行中",
    "waiting": "等待中",
    "waiting_event": "等待回复",
    "waiting_data": "等待数据",
    "waiting_approval": "等待确认",
    "needs_review": "验收未通过",
    "needs_user": "等待人工",
    "manual": "人工处理",
    "manual_required": "需要人工",
    "paused": "已暂停",
    "blocked": "已阻塞",
    "stopped": "已停止",
    "migration_review": "待重新排队",
    "selection_complete": "选刊完成",
    "complete": "已完成",
    "submitted": "已投稿",
    "active": "监控中",
    "monitoring": "监控中",
    "accepted": "已接收",
    "rejected": "已拒稿",
    "revision": "待返修",
}
NEXT_ACTION_LABELS = {
    "queue_writing": "继续写论文",
    "queue_figures": "进入画图阶段",
    "queue_journal_author": "进入选刊与作者",
    "queue_submission": "进入投稿队列",
    "wait_writing": "等待写作完成",
    "wait_figures": "等待图表完成",
    "wait_journal_author": "等待投稿包完成",
    "wait_submission": "等待投稿处理",
    "blocked": "查看阻塞原因",
    "none": "无需操作",
}
COMPLETE = {"complete", "submitted", "active", "accepted", "monitoring", "selection_complete"}
ACTIVE = {"creating_thread", "starting", "running"}
HUMAN = {"waiting_data", "waiting_approval", "needs_user", "manual", "manual_required"}
WAITING = {"ready", "queued", "waiting", "waiting_event", "paused", "needs_review", *HUMAN}
BLOCKED = {"blocked", "stopped", "missing_upstream", "migration_review", "rejected"}

_BROKEN_MARKERS = ("\ufffd", "锛", "鐨", "璁", "浠", "绛", "鏁", "鎶")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
Translator = Callable[..., str]


def _tr(
    translator: Translator | None,
    key: str,
    default: str,
    **values: Any,
) -> str:
    if translator is None:
        try:
            return default.format(**values)
        except (KeyError, ValueError):
            return default
    return translator(key, default=default, **values)


def stage_label(stage: str, translator: Translator | None = None) -> str:
    fallback = STAGE_LABELS.get(stage, "需核对")
    return _tr(translator, f"nav.{stage}", fallback)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _looks_broken(text: str) -> bool:
    if not text:
        return False
    if "\ufffd" in text or _CYRILLIC.search(text):
        return True
    return sum(text.count(marker) for marker in _BROKEN_MARKERS[1:]) >= 3


def readable(value: Any, fallback: str = "") -> str:
    text = _text(value)
    return fallback if not text or _looks_broken(text) else text


def _status(stage: Mapping[str, Any]) -> str:
    token = _text(stage.get("status") or stage.get("raw_status") or "missing").lower()
    contract = stage.get("output_contract", {})
    if token == "incomplete" and isinstance(contract, Mapping) and contract.get("ok") is False:
        return "needs_review"
    return token


def status_label(value: Any, translator: Translator | None = None) -> str:
    token = _text(value).lower() or "missing"
    return _tr(
        translator,
        f"status.{token}",
        STATUS_LABELS.get(token, "需核对"),
    )


def status_tone(value: Any) -> str:
    token = _text(value).lower()
    if token in ACTIVE:
        return "info"
    if token in COMPLETE:
        return "success"
    if token in HUMAN or token == "needs_review":
        return "attention"
    if token in BLOCKED:
        return "danger"
    if token in WAITING:
        return "neutral"
    return "neutral"


def _step_total(stage_name: str, stage: Mapping[str, Any]) -> int:
    candidates = (
        stage.get("workflow_step_count"),
        stage.get("terminal_contract_step"),
        10 if stage_name == "writing" else 3 if stage_name == "figures" else 2 if stage_name == "journal_author" else 1,
    )
    for value in candidates:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 1


def stage_progress(stage_name: str, stage: Mapping[str, Any]) -> float:
    token = _status(stage)
    if token in COMPLETE:
        return 1.0
    total = _step_total(stage_name, stage)
    raw_done = stage.get("step_done", stage.get("current_step", 0))
    try:
        done = max(0, int(raw_done or 0))
    except (TypeError, ValueError):
        done = 0
    return min(1.0, done / max(1, total))


def _step_label(
    stage_name: str,
    stage: Mapping[str, Any],
    translator: Translator | None = None,
) -> str:
    token = _status(stage)
    if token in COMPLETE:
        return _tr(translator, "step.all_complete", "全部 Step 已完成")
    total = _step_total(stage_name, stage)
    try:
        done = max(0, int(stage.get("step_done", stage.get("current_step", 0)) or 0))
    except (TypeError, ValueError):
        done = 0
    done = min(done, total)
    try:
        current = max(0, int(stage.get("current_step", 0) or 0))
    except (TypeError, ValueError):
        current = 0
    contract = stage.get("output_contract", {})
    contract_failed = isinstance(contract, Mapping) and (
        contract.get("ok") is False
        or _text(contract.get("status")).lower() in {"failed", "incomplete", "missing", "invalid"}
    )
    if done >= total:
        return _tr(
            translator,
            "step.executed_acceptance" if contract_failed else "step.executed_confirmation",
            "已执行 {done}/{total} · 待验收" if contract_failed else "已执行 {done}/{total} · 待确认",
            done=total,
            total=total,
        )
    if token in ACTIVE:
        active_step = min(total, max(1, current or done + 1))
        return _tr(
            translator,
            "step.running",
            "Step {step}/{total} 执行中",
            step=active_step,
            total=total,
        )
    if token in HUMAN:
        return (
            _tr(
                translator,
                "step.completed_wait",
                "已完成 {done}/{total} · 等待确认",
                done=done,
                total=total,
            )
            if done
            else _tr(translator, "step.wait_human", "等待人工确认")
        )
    if token in BLOCKED:
        blocked_step = min(total, max(1, current or done + 1))
        return _tr(
            translator,
            "step.blocked",
            "阻塞于 Step {step}/{total}",
            step=blocked_step,
            total=total,
        )
    if done:
        return _tr(
            translator,
            "step.completed_pending",
            "已完成 {done}/{total} · 待执行 {next}",
            done=done,
            total=total,
            next=min(total, done + 1),
        )
    return _tr(
        translator,
        "step.pending",
        "待执行 Step 1/{total}",
        total=total,
    )


def _stage_next_label(
    stage_name: str,
    stage: Mapping[str, Any],
    translator: Translator | None = None,
) -> str:
    token = _status(stage)
    if token in COMPLETE:
        return _tr(translator, "next.stage_complete", "本阶段已完成")
    if token in ACTIVE:
        return _tr(translator, "next.step_running", "当前 Step 正在执行")
    if token in HUMAN:
        return _tr(translator, "next.wait_human", "等待人工确认后继续")
    if token in BLOCKED:
        return _tr(translator, "next.handle_blocker", "处理本阶段卡点")
    if token == "missing":
        return _tr(translator, "next.wait_upstream", "等待上游阶段完成")
    return _tr(
        translator,
        "next.continue_stage",
        "继续{stage}",
        stage=stage_label(stage_name, translator),
    )


def _format_time(value: Any) -> str:
    text = _text(value)
    if not text:
        return "-"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[-16:] if len(text) > 16 else text
    return moment.strftime("%m-%d %H:%M")


def _delivery_stages(stages: Mapping[str, Any]) -> tuple[str, ...]:
    writing = stages.get("writing", {})
    if isinstance(writing, Mapping) and writing.get("prompt_scheme") in {"undergraduate_humanities", "undergraduate_software"}:
        return ("writing", "figures")
    return tuple(STAGE_ORDER)


def _overall_status(record: Mapping[str, Any], stage: Mapping[str, Any]) -> str:
    stages = record.get("stages", {})
    if (isinstance(stages, Mapping) and _delivery_stages(stages) == ("writing", "figures")
            and _status(stages.get("writing", {})) in COMPLETE
            and _status(stages.get("figures", {})) in COMPLETE
            and stages.get("figures", {}).get("word_delivery_ok") is not True):
        return "needs_review"
    if isinstance(stages, Mapping) and stages and all(_status(stages.get(name, {})) in COMPLETE for name in _delivery_stages(stages)):
        return "complete"
    return _status(stage)


def _safe_detail(
    record: Mapping[str, Any],
    stage: Mapping[str, Any],
    status: str,
    translator: Translator | None = None,
) -> str:
    detail = readable(stage.get("detail")) or readable(record.get("blocked_reason"))
    if detail:
        return detail
    defaults = {
        "waiting_data": "等待你确认实验数据后继续。",
        "waiting_approval": "等待人工确认后继续。",
        "blocked": "执行遇到卡点，请查看技术信息。",
        "needs_review": "全部步骤已执行，但阶段产物验收尚未通过。",
        "queued": "任务已经进入队列。",
        "running": "当前 Step 正在执行。",
        "complete": "本阶段已经完成。",
        "missing": "尚未进入本阶段。",
    }
    fallback = defaults.get(status, "状态已同步，暂无额外说明。")
    key = status if status in defaults else "default"
    return _tr(translator, f"safe_detail.{key}", fallback)


def _event_text(event: Mapping[str, Any], translator: Translator | None = None) -> str:
    token = _text(event.get("event") or event.get("action") or "状态更新")
    labels = {
        "queue": "加入统一队列",
        "start_stage": "启动阶段调度",
        "pause_stage": "暂停阶段调度",
        "clear_queue": "清空统一队列",
        "force_stage_admission": "人工进入阶段",
        "archive": "论文已归档",
    }
    fallback = labels.get(token, readable(token, "状态更新"))
    label = _tr(
        translator,
        f"event.{token}" if token in labels else "event.default",
        fallback,
    )
    return f"{_format_time(event.get('at') or event.get('updated_at'))}  {label}"


def paper_rows(
    state: Mapping[str, Any],
    translator: Translator | None = None,
) -> list[dict[str, Any]]:
    papers = state.get("papers", {})
    if not isinstance(papers, Mapping):
        return []
    queue = [str(item) for item in state.get("paper_queue", []) if item]
    queue_positions = {paper_id: index + 1 for index, paper_id in enumerate(queue)}
    rows: list[dict[str, Any]] = []
    for paper_id, raw in papers.items():
        if not isinstance(raw, Mapping):
            continue
        stages = raw.get("stages", {}) if isinstance(raw.get("stages"), Mapping) else {}
        current_key = _text(raw.get("current_stage") or "writing")
        if current_key not in STAGE_LABELS:
            current_key = "done" if current_key == "done" else "writing"
        current_stage = stages.get(current_key, {}) if isinstance(stages.get(current_key), Mapping) else {}
        status = _overall_status(raw, current_stage)
        progress_by_stage = {
            name: stage_progress(name, stages.get(name, {}) if isinstance(stages.get(name), Mapping) else {})
            for name in STAGE_ORDER
        }
        delivery = _delivery_stages(stages)
        overall = sum(progress_by_stage[name] for name in delivery) / len(delivery)
        if delivery == ("writing", "figures") and stages.get("figures", {}).get("word_delivery_ok") is not True:
            overall = min(overall, 0.95)
        next_action = _text(raw.get("next_action") or "none")
        next_label = _tr(
            translator,
            f"next.{next_action}",
            readable(raw.get("next_action_label"))
            or NEXT_ACTION_LABELS.get(next_action, "查看详情"),
        )
        title = readable(raw.get("title"), str(paper_id).replace("_", " "))
        updated = raw.get("updated_at") or current_stage.get("updated_at")
        detail = _safe_detail(raw, current_stage, status, translator)
        row: dict[str, Any] = {
            "paperId": str(paper_id),
            "title": title,
            "currentStage": stage_label(current_key, translator),
            "stageKey": current_key,
            "status": status,
            "statusLabel": status_label(status, translator),
            "statusTone": status_tone(status),
            "nextAction": next_action,
            "nextActionLabel": next_label,
            "progress": round(overall, 4),
            "stepLabel": _step_label(current_key, current_stage, translator),
            "queueLabel": f"Q{queue_positions[paper_id]}" if paper_id in queue_positions else "-",
            "updatedAt": _format_time(updated),
            "detail": detail,
            "writingProgress": progress_by_stage["writing"],
            "figuresProgress": progress_by_stage["figures"],
            "journalProgress": progress_by_stage["journal_author"],
            "submissionProgress": progress_by_stage["submission"],
            "isActive": status in ACTIVE,
            "isWaiting": status in WAITING,
            "isBlocked": status in BLOCKED,
            "isComplete": status in COMPLETE,
        }
        for stage_name in STAGE_ORDER:
            stage = stages.get(stage_name, {}) if isinstance(stages.get(stage_name), Mapping) else {}
            admission = stage.get("admission", {}) if isinstance(stage.get("admission"), Mapping) else {}
            stage_status = _status(stage)
            prefix = stage_name
            row[f"{prefix}StageLabel"] = stage_label(stage_name, translator)
            row[f"{prefix}Status"] = stage_status
            row[f"{prefix}StatusLabel"] = status_label(stage_status, translator)
            row[f"{prefix}StatusTone"] = status_tone(stage_status)
            row[f"{prefix}StepLabel"] = _step_label(stage_name, stage, translator)
            row[f"{prefix}Progress"] = progress_by_stage[stage_name]
            row[f"{prefix}Detail"] = _safe_detail(raw, stage, stage_status, translator)
            row[f"{prefix}NextActionLabel"] = _stage_next_label(stage_name, stage, translator)
            row[f"visible_{stage_name}"] = bool(
                current_key == stage_name
                or _status(stage) != "missing"
                or admission.get("admitted")
            )
            if stage_name not in delivery and stage_status in {"missing", "ready"}:
                row[f"visible_{stage_name}"] = False
        row["searchText"] = " ".join(
            (str(paper_id), title, row["currentStage"], row["statusLabel"], next_label, detail)
        )
        rows.append(row)
    return sorted(rows, key=lambda item: (item["isComplete"], item["updatedAt"] == "-", item["paperId"]), reverse=False)


def metrics_for(
    rows: list[Mapping[str, Any]],
    page: str = "overview",
    translator: Translator | None = None,
) -> list[dict[str, Any]]:
    visible = [row for row in rows if page == "overview" or row.get(f"visible_{page}")]
    counts = Counter()
    for row in visible:
        token = _text(row.get("status") if page == "overview" else row.get(f"{page}Status")).lower()
        if token in ACTIVE:
            counts["active"] += 1
        elif token in BLOCKED:
            counts["blocked"] += 1
        elif token in WAITING:
            counts["waiting"] += 1
        elif token in COMPLETE:
            counts["complete"] += 1
        else:
            counts["pending"] += 1
        if token in HUMAN:
            counts["human"] += 1
    specs = (
        ("total", "论文总数", len(visible), "当前视图", "document", "info"),
        ("active", "运行中", counts["active"], "正在执行", "play", "success"),
        ("pending", "待处理", counts["pending"] + counts["waiting"], "排队或等待", "clock", "attention"),
        ("human", "等待人工", counts["human"], "需要你介入", "person", "attention"),
        ("complete", "已完成", counts["complete"], "全部步骤完成", "check", "success"),
    )
    return [
        {
            "key": key,
            "label": _tr(translator, f"metric.{key}", label),
            "value": value,
            "meta": _tr(translator, f"metric.{key}_meta", meta),
            "icon": icon,
            "tone": tone,
        }
        for key, label, value, meta, icon, tone in specs
    ]


def selected_paper(
    state: Mapping[str, Any],
    paper_id: str,
    focus_stage: str = "",
    translator: Translator | None = None,
) -> dict[str, Any]:
    papers = state.get("papers", {})
    raw = papers.get(paper_id, {}) if isinstance(papers, Mapping) else {}
    if not isinstance(raw, Mapping):
        return {}
    stages = raw.get("stages", {}) if isinstance(raw.get("stages"), Mapping) else {}
    current_key = _text(raw.get("current_stage") or "writing")
    is_stage_focus = focus_stage in STAGE_ORDER
    if is_stage_focus:
        current_key = focus_stage
    current = stages.get(current_key, {}) if isinstance(stages.get(current_key), Mapping) else {}
    current_status = _status(current)
    delivery = _delivery_stages(stages)
    if not is_stage_focus and delivery != tuple(STAGE_ORDER):
        current_status = _overall_status(raw, current)
        if current_key == "done":
            current = stages.get("figures", {})
    stage_cards: list[dict[str, Any]] = []
    for name in delivery:
        stage = stages.get(name, {}) if isinstance(stages.get(name), Mapping) else {}
        if name == "figures" and delivery == ("writing", "figures") and _status(stage) in COMPLETE and stage.get("word_delivery_ok") is not True:
            stage = {**stage, "status": "needs_review", "detail": "Word 正式交付尚未通过验收。"}
        token = _status(stage)
        stage_cards.append(
            {
                "key": name,
                "label": "图表与 Word 交付" if name == "figures" and delivery == ("writing", "figures") else stage_label(name, translator),
                "status": token,
                "statusLabel": status_label(token, translator),
                "tone": status_tone(token),
                "progress": stage_progress(name, stage),
                "stepLabel": _step_label(name, stage, translator),
                "detail": _safe_detail(raw, stage, token, translator),
            }
        )
    events: list[str] = []
    history = state.get("history", [])
    if isinstance(history, list):
        for event in reversed(history):
            if not isinstance(event, Mapping):
                continue
            identities = {
                _text(event.get("paper_id")),
                *(_text(item) for item in event.get("paper_ids", []) if isinstance(event.get("paper_ids"), list)),
            }
            if paper_id not in identities:
                continue
            events.append(_event_text(event, translator))
            if len(events) >= 6:
                break
    title = readable(raw.get("title"), paper_id.replace("_", " "))
    next_action = _text(raw.get("next_action") or "none")
    technical = json.dumps(raw, ensure_ascii=False, indent=2)
    return {
        "paperId": paper_id,
        "title": title,
        "currentStage": stage_label(current_key, translator),
        "status": current_status,
        "statusLabel": status_label(current_status, translator),
        "statusTone": status_tone(current_status),
        "progress": (
            stage_progress(current_key, current)
            if is_stage_focus
            else sum(card["progress"] for card in stage_cards) / len(stage_cards)
        ),
        "stepLabel": _step_label(current_key, current, translator),
        "nextAction": next_action,
        "nextActionLabel": (
            _stage_next_label(current_key, current, translator)
            if is_stage_focus
            else _tr(
                translator,
                f"next.{next_action}",
                readable(raw.get("next_action_label"))
                or NEXT_ACTION_LABELS.get(next_action, "查看详情"),
            )
        ),
        "detail": _safe_detail(raw, current, current_status, translator),
        "updatedAt": _format_time(raw.get("updated_at") or current.get("updated_at")),
        "artifact": readable(
            current.get("artifact"),
            _tr(translator, "technical.artifact_missing", "尚未生成或未登记"),
        ),
        "sessionId": readable(
            current.get("thread_id") or current.get("session_id"),
            _tr(translator, "technical.session_unassigned", "未分配"),
        ),
        "schedulerMode": readable(
            current.get("scheduler_mode"),
            _tr(translator, "technical.mode_missing", "未登记"),
        ),
        "stageCards": stage_cards,
        "events": events
        or [
            _tr(
                translator,
                "event.none",
                "暂无与该论文直接关联的近期事件。",
            )
        ],
        "technical": technical,
    }
