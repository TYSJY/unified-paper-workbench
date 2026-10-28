from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from unified_paper_orchestrator_v2.contracts import ApprovalPolicy, TaskEnvelope
from unified_paper_orchestrator_v2.kernel import WorkflowKernel
from unified_paper_orchestrator_v2.store import EventStore

from .smart_submission_identity import (
    IDENTITY_CANONICAL_MISMATCH,
    IDENTITY_CONFLICT,
    classify_monitor_action,
    submission_identity_snapshot,
)
from .smart_retarget import (
    retarget_request_is_active,
    retarget_withdrawal_decision,
)


PROGRAM_CONTROL_SCHEMA = "smart-submission-program-control/v1"
PROGRAM_TASK_PROMPT_VERSION = "smart-submission-program-v1"

FINAL_SUBMIT_REVIEW_READY_ACTION = "user_confirmation_before_final_submit_required"
FINAL_SUBMIT_AUTHORIZED_ACTION = "final_submit_authorized_for_exact_attempt"


class ProgramPhase(str, Enum):
    LOCAL_PREFLIGHT = "local_preflight"
    DISCOVERY = "discovery"
    SESSION_REQUIRED = "session_required"
    SITE_EXECUTION = "site_execution"
    WAITING_FINAL_APPROVAL = "waiting_final_approval"
    FINAL_ACTION_CLAIMED = "final_action_claimed"
    RECEIPT_CAPTURED = "receipt_captured"
    MONITORING = "monitoring"
    ACTION_REVIEW = "action_review"
    IDENTITY_RECONCILIATION = "identity_reconciliation"
    TECHNICAL_CORRECTION = "technical_correction"
    SCIENTIFIC_REVISION = "scientific_revision"
    REVISION = "revision"
    RETARGET_STATUS_CHECK = "retarget_status_check"
    WAITING_WITHDRAWAL_CONFIRMATION = "waiting_withdrawal_confirmation"
    RETARGET = "retarget"
    MANUAL_TAKEOVER = "manual_takeover"
    BLOCKED_USER = "blocked_user"
    BLOCKED_SITE = "blocked_site"
    UNKNOWN_FINAL_RESULT = "unknown_final_result"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STOPPED = "stopped"


class ProgramAction(str, Enum):
    PREFLIGHT = "submission.preflight"
    DISCOVER_CONTEXT = "submission.discover_context"
    PROBE_SESSION = "submission.probe_session"
    EXECUTE_SITE_STEP = "submission.execute_site_step"
    WAIT_CURRENT_ACTION = "submission.wait_current_action"
    WAIT_FINAL_APPROVAL = "submission.wait_final_approval"
    AUTO_AUTHORIZE_FINAL_SUBMIT = "submission.auto_authorize_final_submit"
    FINAL_SUBMIT = "submission.final_submit"
    CAPTURE_RECEIPT = "submission.capture_receipt"
    MONITOR_EMAIL = "submission.monitor_email"
    MONITOR_STATUS = "submission.monitor_status"
    VERIFY_FINAL_RESULT = "submission.verify_final_result"
    INSPECT_ACTION_EVIDENCE = "submission.inspect_action_evidence"
    RECONCILE_SUBMISSION_IDENTITY = "submission.reconcile_submission_identity"
    APPLY_TECHNICAL_CORRECTION = "submission.apply_technical_correction"
    PREPARE_SCIENTIFIC_REVISION = "submission.prepare_scientific_revision"
    WITHDRAW_FOR_RETARGET = "submission.withdraw_for_retarget"
    VERIFY_OR_WITHDRAW_FOR_RETARGET = "submission.verify_or_withdraw_for_retarget"
    PREPARE_REVISION = "submission.prepare_revision"
    RETARGET = "submission.retarget"
    WAIT_HUMAN = "submission.wait_human"
    NONE = "submission.none"


class ProgramExecutor(str, Enum):
    DETERMINISTIC = "deterministic"
    SITE_ADAPTER = "site_adapter"
    AI_SEMANTIC = "ai_semantic"
    CODEX_SDK_BROWSER = "codex_sdk_browser"
    HUMAN = "human"
    VISIBLE_WORKER_BRIDGE = "visible_worker_bridge"
    NONE = "none"


_PHASE_LABELS = {
    ProgramPhase.LOCAL_PREFLIGHT: "本地预检",
    ProgramPhase.DISCOVERY: "补齐站点/账号信息",
    ProgramPhase.SESSION_REQUIRED: "检查登录会话",
    ProgramPhase.SITE_EXECUTION: "站点执行",
    ProgramPhase.WAITING_FINAL_APPROVAL: "最终审核已完成",
    ProgramPhase.FINAL_ACTION_CLAIMED: "最终提交动作已领取",
    ProgramPhase.RECEIPT_CAPTURED: "已捕获投稿回执",
    ProgramPhase.MONITORING: "投稿后监控",
    ProgramPhase.ACTION_REVIEW: "待核对修改要求",
    ProgramPhase.IDENTITY_RECONCILIATION: "重复投稿待处理",
    ProgramPhase.TECHNICAL_CORRECTION: "技术修改",
    ProgramPhase.SCIENTIFIC_REVISION: "科学返修",
    ProgramPhase.REVISION: "待分类修改",
    ProgramPhase.RETARGET_STATUS_CHECK: "核验旧稿并按需撤稿",
    ProgramPhase.WAITING_WITHDRAWAL_CONFIRMATION: "等待撤稿确认",
    ProgramPhase.RETARGET: "自动转投",
    ProgramPhase.MANUAL_TAKEOVER: "人工修改中",
    ProgramPhase.BLOCKED_USER: "等待用户处理",
    ProgramPhase.BLOCKED_SITE: "站点卡点",
    ProgramPhase.UNKNOWN_FINAL_RESULT: "最终提交结果待核验",
    ProgramPhase.ACCEPTED: "已接收",
    ProgramPhase.REJECTED: "已拒稿",
    ProgramPhase.STOPPED: "已停止",
}

_ACTION_LABELS = {
    ProgramAction.PREFLIGHT: "程序检查投稿包",
    ProgramAction.DISCOVER_CONTEXT: "程序匹配站点与账号",
    ProgramAction.PROBE_SESSION: "程序核验登录会话",
    ProgramAction.EXECUTE_SITE_STEP: "执行当前站点动作",
    ProgramAction.WAIT_CURRENT_ACTION: "等待当前动作结束",
    ProgramAction.WAIT_FINAL_APPROVAL: "等待用户确认最终提交",
    ProgramAction.AUTO_AUTHORIZE_FINAL_SUBMIT: "程序准备自动最终提交",
    ProgramAction.FINAL_SUBMIT: "执行一次最终提交",
    ProgramAction.CAPTURE_RECEIPT: "捕获并校验投稿回执",
    ProgramAction.MONITOR_EMAIL: "程序执行邮件优先监控",
    ProgramAction.MONITOR_STATUS: "检查期刊后台投稿状态",
    ProgramAction.VERIFY_FINAL_RESULT: "只核验提交结果，禁止重试点击",
    ProgramAction.INSPECT_ACTION_EVIDENCE: "核对期刊要求，不执行网页修改",
    ProgramAction.RECONCILE_SUBMISSION_IDENTITY: "关闭重复投稿并确认唯一编号",
    ProgramAction.APPLY_TECHNICAL_CORRECTION: "修改现有投稿的技术信息",
    ProgramAction.PREPARE_SCIENTIFIC_REVISION: "生成修订稿与回复材料",
    ProgramAction.WITHDRAW_FOR_RETARGET: "撤回现有投稿后自动转投",
    ProgramAction.VERIFY_OR_WITHDRAW_FOR_RETARGET: "核验旧稿状态并按需撤稿",
    ProgramAction.PREPARE_REVISION: "分类并准备修改动作",
    ProgramAction.RETARGET: "自动重新选刊并生成新投稿包",
    ProgramAction.WAIT_HUMAN: "等待用户处理卡点",
    ProgramAction.NONE: "无需动作",
}

_EXECUTOR_LABELS = {
    ProgramExecutor.DETERMINISTIC: "程序逻辑",
    ProgramExecutor.SITE_ADAPTER: "站点适配器",
    ProgramExecutor.AI_SEMANTIC: "AI语义处理",
    ProgramExecutor.CODEX_SDK_BROWSER: "Codex SDK受控网页会话",
    ProgramExecutor.HUMAN: "用户",
    ProgramExecutor.VISIBLE_WORKER_BRIDGE: "兼容可见执行会话",
    ProgramExecutor.NONE: "无",
}

_UNKNOWN_FINAL_TOKENS = {
    "dispatch_state_unknown",
    "final_submit_action_unknown",
    "submission_result_unknown",
    "final_submit_result_unknown",
    "final_submit_unknown",
}

_ACCEPTED_TOKENS = {"accepted", "accept", "published", "online_published"}
_REJECTED_TOKENS = {"rejected", "declined"}
_STOPPED_TOKENS = {"stopped", "deleted", "withdrawn", "no_action"}
_MONITORING_TOKENS = {
    "submitted",
    "monitoring",
    "under_review",
    "in_review",
    "technical_check",
    "editorial_review",
    "decision_pending",
}
_DISCOVERY_TOKENS = {
    "discover_account",
    "discover_email_account",
    "discover_journal_site",
}
_READY_TOKENS = {
    "ready",
    "ready_for_online_submit",
    "login_check",
    "uploading",
}
_HUMAN_MARKERS = (
    "captcha",
    "cloudflare",
    "2fa",
    "security challenge",
    "payment",
    "forced oa",
    "orcid",
    "manual",
    "user confirmation",
    "author confirmation",
    "secret_broker",
)


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _text(*values: Any) -> str:
    return "\n".join(str(value or "") for value in values).casefold()


def _positive_identifier(value: Any) -> bool:
    token = str(value or "").strip()
    return bool(token and token.casefold() not in {"none", "null", "-", "n/a"})


def _has_submission_evidence(paper: dict[str, Any]) -> bool:
    if isinstance(paper.get("submission_confirmation_receipt"), dict):
        return True
    for key in ("manuscript_id", "submission_id", "submission_number", "tracking_id"):
        if _positive_identifier(paper.get(key)):
            return True
    for key in ("confirmation_url", "submission_confirmation_url"):
        value = str(paper.get(key) or "").strip()
        if value.startswith(("https://", "http://")):
            return True
    return False


def _final_authorization_claimed(paper: dict[str, Any]) -> bool:
    authorization = paper.get("final_submit_authorization")
    return bool(
        isinstance(authorization, dict)
        and str(authorization.get("consumed_at") or "").strip()
    )


def _decision(
    *,
    paper_id: str,
    phase: ProgramPhase,
    action: ProgramAction,
    executor: ProgramExecutor,
    reason: str,
    legacy_status: str,
    legacy_workflow: str,
    in_local_queue: bool,
    in_online_queue: bool,
    dispatchable: bool = False,
    terminal: bool = False,
    requires_model: bool = False,
    fallback_executor: ProgramExecutor = ProgramExecutor.NONE,
) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "phase": phase.value,
        "phase_label": _PHASE_LABELS[phase],
        "action": action.value,
        "action_label": _ACTION_LABELS[action],
        "executor": executor.value,
        "executor_label": _EXECUTOR_LABELS[executor],
        "fallback_executor": fallback_executor.value,
        "fallback_executor_label": _EXECUTOR_LABELS[fallback_executor],
        "reason": reason,
        "legacy_status": legacy_status,
        "legacy_workflow": legacy_workflow,
        "in_local_queue": in_local_queue,
        "in_online_queue": in_online_queue,
        "dispatchable": dispatchable,
        "terminal": terminal,
        "requires_model": requires_model,
    }


def decide_paper(
    paper_id: str,
    paper: dict[str, Any],
    *,
    in_local_queue: bool,
    in_online_queue: bool,
    auto_final_submit_enabled: bool = True,
) -> dict[str, Any]:
    """Project one legacy Smart record into one canonical program decision.

    This function is deliberately pure: it never reads files, opens a browser,
    mutates Smart JSON, or calls a model.
    """

    status = _token(paper.get("status"))
    workflow = _token(paper.get("workflow_status"))
    worker = _token(paper.get("worker_status"))
    next_action = str(paper.get("next_action") or "").strip()
    next_token = _token(next_action)
    combined = _text(
        status,
        workflow,
        worker,
        next_action,
        paper.get("blocker"),
        paper.get("detail"),
    )
    identity = submission_identity_snapshot(paper)
    identity_status = str(
        paper.get("submission_identity_status") or identity.get("status") or ""
    ).strip().casefold()

    manual_takeover = (
        paper.get("manual_takeover")
        if isinstance(paper.get("manual_takeover"), dict)
        else {}
    )
    manual_takeover_status = _token(manual_takeover.get("status"))
    if manual_takeover_status in {"requested", "active"}:
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.MANUAL_TAKEOVER,
            action=ProgramAction.WAIT_HUMAN,
            executor=ProgramExecutor.HUMAN,
            reason=(
                "the selected paper is waiting for its current SDK Turn to finish before manual takeover"
                if manual_takeover_status == "requested"
                else "the user owns the current browser page until manual editing is completed"
            ),
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )
    if bool(paper.get("manual_takeover_reconcile_pending")):
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.SITE_EXECUTION,
            action=ProgramAction.EXECUTE_SITE_STEP,
            executor=ProgramExecutor.CODEX_SDK_BROWSER,
            fallback_executor=ProgramExecutor.HUMAN,
            reason=(
                "the user completed a manual page edit; the fixed SDK worker must "
                "reinspect the existing page and preserve completed fields before continuing"
            ),
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            dispatchable=in_online_queue,
            requires_model=True,
        )

    retarget_request = paper.get("retarget_request")
    if retarget_request_is_active(retarget_request):
        request_status = _token(retarget_request.get("status"))
        withdrawal = retarget_withdrawal_decision(paper, retarget_request)
        if request_status == "waiting_withdrawal_confirmation":
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.WAITING_WITHDRAWAL_CONFIRMATION,
                action=ProgramAction.WAIT_HUMAN,
                executor=ProgramExecutor.DETERMINISTIC,
                fallback_executor=ProgramExecutor.HUMAN,
                reason=str(
                    retarget_request.get("withdrawal_detail")
                    or "the journal requires an out-of-band withdrawal confirmation"
                ),
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
            )
        if (
            withdrawal.get("decision") == "verify_or_withdraw"
            or request_status in {"identity_check_required", "withdrawal_required"}
        ):
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.RETARGET_STATUS_CHECK,
                action=ProgramAction.VERIFY_OR_WITHDRAW_FOR_RETARGET,
                executor=ProgramExecutor.CODEX_SDK_BROWSER,
                fallback_executor=ProgramExecutor.HUMAN,
                reason=str(withdrawal.get("reason") or "verify current submission before retarget"),
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
                dispatchable=in_online_queue,
                requires_model=True,
            )
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.RETARGET,
            action=ProgramAction.RETARGET,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason=(
                "user retarget request is waiting for journal-author Steps 2-3"
                if request_status in {"journal_author_dispatching", "journal_author_queued"}
                else "prior submission is closed and the user retarget request may select a journal"
            ),
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if (
        identity_status in {IDENTITY_CONFLICT, IDENTITY_CANONICAL_MISMATCH}
        or workflow == "identity_reconciliation_required"
    ):
        active_ids = identity.get("active_submission_ids", [])
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.IDENTITY_RECONCILIATION,
            action=ProgramAction.RECONCILE_SUBMISSION_IDENTITY,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.HUMAN,
            reason=(
                "online mutation is blocked until one canonical active manuscript remains; "
                f"active_ids={','.join(str(item) for item in active_ids) or '-'}"
            ),
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if status in _ACCEPTED_TOKENS:
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.ACCEPTED,
            action=ProgramAction.NONE,
            executor=ProgramExecutor.NONE,
            reason="accepted terminal state",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            terminal=True,
        )
    if status in _STOPPED_TOKENS:
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.STOPPED,
            action=ProgramAction.NONE,
            executor=ProgramExecutor.NONE,
            reason="stopped terminal state",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            terminal=True,
        )
    if status in _REJECTED_TOKENS:
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.REJECTED,
            action=ProgramAction.RETARGET,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason="rejected paper must return to journal selection",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if (
        status in _UNKNOWN_FINAL_TOKENS
        or next_token in _UNKNOWN_FINAL_TOKENS
        or any(token in combined for token in _UNKNOWN_FINAL_TOKENS)
    ):
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.UNKNOWN_FINAL_RESULT,
            action=ProgramAction.VERIFY_FINAL_RESULT,
            executor=ProgramExecutor.SITE_ADAPTER,
            fallback_executor=ProgramExecutor.HUMAN,
            reason="a final-submit crash window must be verified without retrying the click",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            dispatchable=in_online_queue,
        )

    if workflow == "running" and str(paper.get("thread_id") or "").strip():
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.SITE_EXECUTION,
            action=ProgramAction.WAIT_CURRENT_ACTION,
            executor=ProgramExecutor.DETERMINISTIC,
            reason="one bound worker already owns the current attempt",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if status in {"revision_needed", "revision_requested"}:
        observation = (
            paper.get("last_monitor_observation")
            if isinstance(paper.get("last_monitor_observation"), dict)
            else {}
        )
        action_kind = classify_monitor_action(
            status=status,
            action_kind=(
                paper.get("last_monitor_followup_kind")
                or paper.get("last_monitor_action_kind")
                or observation.get("action_kind")
            ),
            summary=observation.get("summary") or paper.get("detail"),
        )
        if workflow == "action_evidence_required" or action_kind == "unknown":
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.ACTION_REVIEW,
                action=ProgramAction.INSPECT_ACTION_EVIDENCE,
                executor=ProgramExecutor.DETERMINISTIC,
                fallback_executor=ProgramExecutor.CODEX_SDK_BROWSER,
                reason="the exact author action is not yet evidenced, so online mutation is held",
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
            )
        if (
            action_kind == "retarget"
            and str(paper.get("online_intent") or "").strip().casefold()
            == "post_submission_withdraw_for_retarget"
        ):
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.RETARGET,
                action=ProgramAction.WITHDRAW_FOR_RETARGET,
                executor=ProgramExecutor.CODEX_SDK_BROWSER,
                fallback_executor=ProgramExecutor.HUMAN,
                reason="the existing manuscript must be closed before journal-author retargeting starts",
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
                dispatchable=in_online_queue,
                requires_model=True,
            )
        if action_kind in {"technical_correction", "files_or_metadata"}:
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.TECHNICAL_CORRECTION,
                action=ProgramAction.APPLY_TECHNICAL_CORRECTION,
                executor=ProgramExecutor.CODEX_SDK_BROWSER,
                fallback_executor=ProgramExecutor.HUMAN,
                reason="an explicit bounded correction targets one verified existing manuscript",
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
                dispatchable=in_online_queue,
                requires_model=True,
            )
        if action_kind == "scientific_revision" or workflow == "waiting_revision_package":
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.SCIENTIFIC_REVISION,
                action=ProgramAction.PREPARE_SCIENTIFIC_REVISION,
                executor=ProgramExecutor.AI_SEMANTIC,
                fallback_executor=ProgramExecutor.HUMAN,
                reason="a revised manuscript package must be generated before any online resubmission",
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
                requires_model=True,
            )
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.ACTION_REVIEW,
            action=ProgramAction.INSPECT_ACTION_EVIDENCE,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.CODEX_SDK_BROWSER,
            reason="legacy revision state lacks an explicit correction classification",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if status == "retarget_needed":
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.RETARGET,
            action=ProgramAction.RETARGET,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason="retargeting is routed back to the journal-author workflow",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if paper.get("monitor_check_pending"):
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.MONITORING,
            action=ProgramAction.MONITOR_STATUS,
            executor=ProgramExecutor.CODEX_SDK_BROWSER,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason="the configured daily status check has been queued by program logic",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            dispatchable=in_online_queue,
            requires_model=True,
        )

    if _has_submission_evidence(paper):
        if isinstance(paper.get("submission_confirmation_receipt"), dict):
            phase = ProgramPhase.RECEIPT_CAPTURED
            reason = "trusted submission receipt is present"
        else:
            phase = ProgramPhase.MONITORING
            reason = "submission evidence is present"
        return _decision(
            paper_id=paper_id,
            phase=phase,
            action=ProgramAction.MONITOR_EMAIL,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason=reason,
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if status in _MONITORING_TOKENS or workflow == "monitoring":
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.MONITORING,
            action=ProgramAction.MONITOR_EMAIL,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason="legacy record is in post-submission monitoring",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if _final_authorization_claimed(paper):
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.FINAL_ACTION_CLAIMED,
            action=ProgramAction.FINAL_SUBMIT,
            executor=ProgramExecutor.SITE_ADAPTER,
            reason="one-use final-submit capability is already claimed",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            dispatchable=in_online_queue,
        )

    if next_action == FINAL_SUBMIT_REVIEW_READY_ACTION:
        if auto_final_submit_enabled:
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.WAITING_FINAL_APPROVAL,
                action=ProgramAction.AUTO_AUTHORIZE_FINAL_SUBMIT,
                executor=ProgramExecutor.DETERMINISTIC,
                reason=(
                    "the final review is complete and the program will issue one exact-attempt "
                    "authorization under the enabled automatic final-submit policy"
                ),
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
            )
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.WAITING_FINAL_APPROVAL,
            action=ProgramAction.WAIT_FINAL_APPROVAL,
            executor=ProgramExecutor.HUMAN,
            reason="the exact current attempt is waiting for explicit final-submit approval",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if next_action == FINAL_SUBMIT_AUTHORIZED_ACTION:
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.WAITING_FINAL_APPROVAL,
            action=ProgramAction.FINAL_SUBMIT,
            executor=ProgramExecutor.SITE_ADAPTER,
            reason="final-submit authorization exists and must be reserved for one exact worker attempt",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            dispatchable=in_online_queue,
        )

    if status == "blocked":
        if any(marker in combined for marker in _HUMAN_MARKERS):
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.BLOCKED_USER,
                action=ProgramAction.WAIT_HUMAN,
                executor=ProgramExecutor.HUMAN,
                reason="the blocker requires a user-owned action",
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
            )
        if any(marker in combined for marker in ("package", "metadata", "declaration", "local blocker")):
            return _decision(
                paper_id=paper_id,
                phase=ProgramPhase.LOCAL_PREFLIGHT,
                action=ProgramAction.PREFLIGHT,
                executor=ProgramExecutor.DETERMINISTIC,
                reason="the blocker is locally checkable and must not consume an AI worker",
                legacy_status=status,
                legacy_workflow=workflow,
                in_local_queue=in_local_queue,
                in_online_queue=in_online_queue,
            )
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.BLOCKED_SITE,
            action=ProgramAction.WAIT_HUMAN,
            executor=ProgramExecutor.HUMAN,
            fallback_executor=ProgramExecutor.SITE_ADAPTER,
            reason="unclassified blocker is held for explicit recovery",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if status in _DISCOVERY_TOKENS or next_token.startswith("discover_"):
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.DISCOVERY,
            action=ProgramAction.DISCOVER_CONTEXT,
            executor=ProgramExecutor.DETERMINISTIC,
            fallback_executor=ProgramExecutor.AI_SEMANTIC,
            reason="registered site/account sources are checked before any semantic fallback",
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
        )

    if status in _READY_TOKENS or in_online_queue:
        action = ProgramAction.EXECUTE_SITE_STEP
        executor = ProgramExecutor.CODEX_SDK_BROWSER
        return _decision(
            paper_id=paper_id,
            phase=ProgramPhase.SITE_EXECUTION,
            action=action,
            executor=executor,
            fallback_executor=ProgramExecutor.VISIBLE_WORKER_BRIDGE,
            reason=(
                "the program selected this paper and the fixed SDK worker will "
                "revalidate the exact-origin session before using constrained browser tools"
            ),
            legacy_status=status,
            legacy_workflow=workflow,
            in_local_queue=in_local_queue,
            in_online_queue=in_online_queue,
            dispatchable=in_online_queue,
            requires_model=True,
        )

    return _decision(
        paper_id=paper_id,
        phase=ProgramPhase.LOCAL_PREFLIGHT,
        action=ProgramAction.PREFLIGHT,
        executor=ProgramExecutor.DETERMINISTIC,
        reason="unclassified or local-queue records start with deterministic preflight",
        legacy_status=status,
        legacy_workflow=workflow,
        in_local_queue=in_local_queue,
        in_online_queue=in_online_queue,
    )


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _monitor_due(paper: dict[str, Any], now: datetime) -> bool:
    last = _parse_time(
        paper.get("last_monitor_checked_at")
        or paper.get("last_checked_at")
        or paper.get("last_updated_at")
    )
    if last is None:
        return True
    return last.astimezone(now.tzinfo).date() < now.date()


def _bounded_parallel(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 4
    return max(1, min(parsed, 32))


def _selected_set(selected_ids: Iterable[str] | None) -> set[str] | None:
    if selected_ids is None:
        return None
    return {str(item).strip() for item in selected_ids if str(item).strip()}


def _task_cycle(decision: dict[str, Any], paper: dict[str, Any], now: datetime) -> str:
    if decision["action"] in {
        ProgramAction.MONITOR_EMAIL.value,
        ProgramAction.MONITOR_STATUS.value,
    }:
        return now.date().isoformat()
    route_revision = int(paper.get("route_revision") or 0)
    attempt_id = str(paper.get("attempt_id") or "no-attempt").strip()
    package_binding = str(
        paper.get("package_binding_sha256")
        or paper.get("package_manifest_sha256")
        or paper.get("source_package_dir")
        or ""
    )
    authorization_id = ""
    if decision["action"] == ProgramAction.FINAL_SUBMIT.value:
        authorization = paper.get("final_submit_authorization")
        if isinstance(authorization, dict):
            authorization_id = str(authorization.get("authorization_id") or "")
    raw = "|".join(
        (
            decision["paper_id"],
            str(route_revision),
            attempt_id,
            decision["action"],
            package_binding,
            authorization_id,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_program_plan(
    state: dict[str, Any],
    *,
    selected_ids: Iterable[str] | None = None,
    dispatch_bindings: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the authoritative deterministic control-plane projection."""

    current = now or datetime.now(timezone.utc).astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    local_queue = list(
        dict.fromkeys(str(item) for item in state.get("paper_queue", []) if str(item).strip())
    )
    online_queue = list(
        dict.fromkeys(str(item) for item in state.get("online_queue", []) if str(item).strip())
    )
    local_set = set(local_queue)
    online_set = set(online_queue)
    selected = _selected_set(selected_ids)
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    mode = str(state.get("mode") or "manual")
    max_parallel = _bounded_parallel(settings.get("max_parallel_workers"))

    projected: dict[str, dict[str, Any]] = {}
    for raw_paper_id, raw_paper in sorted(papers.items()):
        paper_id = str(raw_paper_id)
        paper = raw_paper if isinstance(raw_paper, dict) else {}
        decision = decide_paper(
            paper_id,
            paper,
            in_local_queue=paper_id in local_set,
            in_online_queue=paper_id in online_set,
            auto_final_submit_enabled=bool(
                settings.get("auto_final_submit_enabled", True)
            ),
        )
        binding = (
            dispatch_bindings.get(paper_id, {})
            if isinstance(dispatch_bindings, dict)
            and isinstance(dispatch_bindings.get(paper_id), dict)
            else {}
        )
        decision.update(binding)
        if (
            paper_id in online_set
            and decision["dispatchable"]
            and dispatch_bindings is not None
            and not binding.get("dispatch_binding_ready")
        ):
            decision.update(
                {
                    "phase": ProgramPhase.DISCOVERY.value,
                    "phase_label": _PHASE_LABELS[ProgramPhase.DISCOVERY],
                    "action": ProgramAction.DISCOVER_CONTEXT.value,
                    "action_label": _ACTION_LABELS[ProgramAction.DISCOVER_CONTEXT],
                    "executor": ProgramExecutor.DETERMINISTIC.value,
                    "executor_label": _EXECUTOR_LABELS[ProgramExecutor.DETERMINISTIC],
                    "fallback_executor": ProgramExecutor.AI_SEMANTIC.value,
                    "fallback_executor_label": _EXECUTOR_LABELS[ProgramExecutor.AI_SEMANTIC],
                    "reason": (
                        "verified value-free account/site/origin binding is incomplete; "
                        "program discovery must finish before worker dispatch"
                    ),
                    "dispatchable": False,
                    "requires_model": False,
                }
            )
        decision["cycle"] = _task_cycle(decision, paper, current)
        decision["route_revision"] = int(paper.get("route_revision") or 0)
        decision["attempt_id"] = str(paper.get("attempt_id") or "")
        decision["thread_id"] = str(paper.get("thread_id") or "")
        decision["submission_system_id"] = str(
            binding.get("submission_system_id")
            or paper.get("submission_system_id")
            or ""
        )
        decision["target_journal"] = str(paper.get("target_journal") or "")
        projected[paper_id] = decision

    running = [
        paper_id
        for paper_id in online_queue
        if paper_id in projected
        and projected[paper_id]["action"] == ProgramAction.WAIT_CURRENT_ACTION.value
    ]
    available_slots = max(0, max_parallel - len(running))
    dispatch_candidates: list[dict[str, Any]] = []
    for paper_id in online_queue:
        decision = projected.get(paper_id)
        if (
            decision is None
            or (selected is not None and paper_id not in selected)
            or not decision["dispatchable"]
            or decision["terminal"]
            or decision["executor"] == ProgramExecutor.HUMAN.value
        ):
            continue
        if len(dispatch_candidates) >= available_slots:
            break
        dispatch_candidates.append(dict(decision))

    local_actions = [
        dict(projected[paper_id])
        for paper_id in local_queue
        if paper_id in projected
        and (selected is None or paper_id in selected)
        and not projected[paper_id]["terminal"]
    ]
    online_control_actions = [
        dict(projected[paper_id])
        for paper_id in online_queue
        if paper_id in projected
        and (selected is None or paper_id in selected)
        and not projected[paper_id]["terminal"]
        and not projected[paper_id]["dispatchable"]
        and projected[paper_id]["executor"] == ProgramExecutor.DETERMINISTIC.value
        and projected[paper_id]["action"] != ProgramAction.WAIT_CURRENT_ACTION.value
    ]
    monitoring_enabled = bool(settings.get("daily_monitor_enabled", True))
    monitor_candidates: list[dict[str, Any]] = []
    if monitoring_enabled and mode in {"scheduled", "monitor_only"}:
        for paper_id, decision in projected.items():
            paper = papers.get(paper_id, {})
            if (
                (selected is None or paper_id in selected)
                and decision["action"] == ProgramAction.MONITOR_EMAIL.value
                and isinstance(paper, dict)
                and _monitor_due(paper, current)
            ):
                monitor_candidates.append(dict(decision))

    scheduled_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in [
        *local_actions,
        *online_control_actions,
        *dispatch_candidates,
        *monitor_candidates,
    ]:
        key = (item["paper_id"], item["action"], item["cycle"])
        scheduled_by_key[key] = item

    executor_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    for decision in projected.values():
        executor_counts[decision["executor"]] = executor_counts.get(decision["executor"], 0) + 1
        phase_counts[decision["phase"]] = phase_counts.get(decision["phase"], 0) + 1

    plan_identity = {
        "schema": PROGRAM_CONTROL_SCHEMA,
        "state_updated_at": str(state.get("updated_at") or ""),
        "mode": mode,
        "max_parallel_workers": max_parallel,
        "active_workers": running,
        "scheduled_actions": [
            {
                "paper_id": item["paper_id"],
                "action": item["action"],
                "cycle": item["cycle"],
                "route_revision": item.get("route_revision", 0),
                "attempt_id": item.get("attempt_id", ""),
            }
            for item in scheduled_by_key.values()
        ],
    }
    plan_id = hashlib.sha256(
        json.dumps(
            plan_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]

    return {
        "schema": PROGRAM_CONTROL_SCHEMA,
        "plan_id": plan_id,
        "control_plane": "program_v1",
        "compatibility_bridge": "legacy_visible_worker_fallback",
        "primary_execution_bridge": "codex_sdk_browser_mcp",
        "generated_at": current.isoformat(timespec="seconds"),
        "mode": mode,
        "selected_paper_ids": sorted(selected) if selected is not None else [],
        "selection_applied": selected is not None,
        "max_parallel_workers": max_parallel,
        "active_workers": running,
        "active_worker_count": len(running),
        "available_slots": available_slots,
        "dispatch_candidates": dispatch_candidates,
        "dispatch_candidate_ids": [item["paper_id"] for item in dispatch_candidates],
        "local_actions": local_actions,
        "online_control_actions": online_control_actions,
        "monitor_candidates": monitor_candidates,
        "scheduled_actions": list(scheduled_by_key.values()),
        "paper_count": len(projected),
        "phase_counts": phase_counts,
        "executor_counts": executor_counts,
        "model_required_count": sum(1 for item in projected.values() if item["requires_model"]),
        "program_only_count": sum(
            1
            for item in projected.values()
            if item["executor"] == ProgramExecutor.DETERMINISTIC.value
        ),
        "papers": projected,
    }


def program_database_path(state_path: Path | str) -> Path:
    return Path(state_path).resolve().parent / "program_control.sqlite3"


def _kernel_action(program_action: str) -> str:
    if program_action in {
        ProgramAction.MONITOR_EMAIL.value,
        ProgramAction.MONITOR_STATUS.value,
    }:
        return "submission.monitor"
    if program_action in {
        ProgramAction.PREFLIGHT.value,
        ProgramAction.DISCOVER_CONTEXT.value,
        ProgramAction.PROBE_SESSION.value,
        ProgramAction.WAIT_FINAL_APPROVAL.value,
        ProgramAction.AUTO_AUTHORIZE_FINAL_SUBMIT.value,
        ProgramAction.INSPECT_ACTION_EVIDENCE.value,
        ProgramAction.RECONCILE_SUBMISSION_IDENTITY.value,
        ProgramAction.WAIT_HUMAN.value,
    }:
        return "submission.prepare"
    return "submission.execute"


def _kernel_executor(executor: str) -> str:
    return {
        ProgramExecutor.DETERMINISTIC.value: "deterministic",
        ProgramExecutor.SITE_ADAPTER.value: "browser",
        ProgramExecutor.AI_SEMANTIC.value: "codex_sdk",
        ProgramExecutor.CODEX_SDK_BROWSER.value: "codex_sdk",
        ProgramExecutor.HUMAN.value: "human",
        ProgramExecutor.VISIBLE_WORKER_BRIDGE.value: "legacy_codex",
    }.get(executor, "deterministic")


def _idempotency_key(item: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "schema": PROGRAM_CONTROL_SCHEMA,
            "paper_id": item["paper_id"],
            "action": item["action"],
            "cycle": item["cycle"],
            "route_revision": item.get("route_revision", 0),
            "attempt_id": item.get("attempt_id", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "smart-program-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_program_tasks(
    plan: dict[str, Any],
    *,
    state_path: Path | str,
) -> dict[str, Any]:
    """Persist planned actions in an isolated V2-compatible workflow ledger.

    The first production slice deliberately uses a Smart-local database. It
    cannot be claimed by the general API gateway and therefore cannot trigger
    browser/model side effects while the compatibility bridge is still active.
    """

    database = program_database_path(state_path)
    kernel = WorkflowKernel(EventStore(database))
    records: list[dict[str, Any]] = []
    for item in plan.get("scheduled_actions", []):
        if not isinstance(item, dict):
            continue
        program_action = str(item.get("action") or "")
        executor = str(item.get("executor") or "")
        if program_action in {
            ProgramAction.NONE.value,
            ProgramAction.WAIT_CURRENT_ACTION.value,
        }:
            continue
        envelope = TaskEnvelope(
            paper_id=str(item.get("paper_id") or ""),
            stage="submission",
            action=_kernel_action(program_action),
            executor=_kernel_executor(executor),
            dispatch_owner=(
                "gateway"
                if executor
                in {
                    ProgramExecutor.SITE_ADAPTER.value,
                    ProgramExecutor.AI_SEMANTIC.value,
                    ProgramExecutor.CODEX_SDK_BROWSER.value,
                    ProgramExecutor.VISIBLE_WORKER_BRIDGE.value,
                }
                else "kernel"
            ),
            idempotency_key=_idempotency_key(item),
            prompt_version=PROGRAM_TASK_PROMPT_VERSION,
            payload={
                "program_control_schema": PROGRAM_CONTROL_SCHEMA,
                "program_action": program_action,
                "program_phase": str(item.get("phase") or ""),
                "program_executor": executor,
                "compatibility_bridge": plan.get("compatibility_bridge", ""),
                "route_revision": int(item.get("route_revision") or 0),
                "attempt_id": str(item.get("attempt_id") or ""),
                "cycle": str(item.get("cycle") or ""),
                "requires_model": bool(item.get("requires_model")),
                "read_only_plan": True,
            },
            network_access=executor
            in {
                ProgramExecutor.SITE_ADAPTER.value,
                ProgramExecutor.AI_SEMANTIC.value,
                ProgramExecutor.CODEX_SDK_BROWSER.value,
                ProgramExecutor.VISIBLE_WORKER_BRIDGE.value,
            },
            # The production Smart runtime uses the signed, exact-attempt,
            # one-use capability in the main state as its execution authority.
            # This isolated compatibility ledger deliberately retains the V2
            # kernel's global submission.execute approval marker as an inert
            # audit safeguard; it is never claimed or dispatched from here.
            approval_policy=ApprovalPolicy.NONE,
        )
        record = kernel.create_task(envelope)
        records.append(
            {
                "task_id": record["task_id"],
                "paper_id": record["paper_id"],
                "status": record["status"],
                "action": program_action,
                "executor": executor,
                "idempotency_key": record["idempotency_key"],
            }
        )
    runtime = kernel.status()["runtime"]
    return {
        "database_path": str(database),
        "task_count": len(records),
        "tasks": records,
        "runtime": runtime,
        "side_effects_dispatched": False,
    }


__all__ = [
    "PROGRAM_CONTROL_SCHEMA",
    "PROGRAM_TASK_PROMPT_VERSION",
    "ProgramAction",
    "ProgramExecutor",
    "ProgramPhase",
    "build_program_plan",
    "decide_paper",
    "program_database_path",
    "sync_program_tasks",
]
