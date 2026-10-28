"""Deterministic request and withdrawal policy for Smart retargeting.

The UI may ask for a specific journal or an AI-selected target, but neither
choice is allowed to bypass the one-active-submission rule.  This module keeps
request validation, publication-scope normalization, and the decision to
verify/withdraw an existing manuscript outside prompts and browser workers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .smart_submission_identity import submission_identity_snapshot


RETARGET_REQUEST_SCHEMA = "smart-submission-retarget-request/v1"

RETARGET_MODE_SPECIFIC = "specific"
RETARGET_MODE_AI = "ai"
RETARGET_MODES = {RETARGET_MODE_SPECIFIC, RETARGET_MODE_AI}

PUBLICATION_TIER_SCI = "sci"
PUBLICATION_TIER_CHINESE_CORE = "chinese_core"
PUBLICATION_TIER_GENERAL = "general"
PUBLICATION_TIERS = {
    PUBLICATION_TIER_SCI,
    PUBLICATION_TIER_CHINESE_CORE,
    PUBLICATION_TIER_GENERAL,
}

RETARGET_PRIORITY_ACCEPTANCE = "acceptance"
RETARGET_PRIORITY_SPEED = "speed"
RETARGET_PRIORITY_QUALITY = "quality"
RETARGET_PRIORITY_BALANCED = "balanced"
RETARGET_PRIORITIES = {
    RETARGET_PRIORITY_ACCEPTANCE,
    RETARGET_PRIORITY_SPEED,
    RETARGET_PRIORITY_QUALITY,
    RETARGET_PRIORITY_BALANCED,
}

WITHDRAWAL_POLICY_AUTO = "auto"
WITHDRAWAL_POLICY_FORCE = "force"
WITHDRAWAL_POLICY_CONFIRMED_CLOSED = "confirmed_closed"
WITHDRAWAL_POLICIES = {
    WITHDRAWAL_POLICY_AUTO,
    WITHDRAWAL_POLICY_FORCE,
    WITHDRAWAL_POLICY_CONFIRMED_CLOSED,
}

RETARGET_REQUEST_ACTIVE_STATUSES = {
    "requested",
    "identity_check_required",
    "withdrawal_required",
    "waiting_withdrawal_confirmation",
    "ready_for_selection",
    "journal_author_dispatching",
    "journal_author_queued",
    "package_ready",
}

_PAPER_CLOSED_STATUSES = {
    "closed",
    "declined",
    "expired_transfer",
    "rejected",
    "retargeted",
    "stopped",
    "submission_closed",
    "transfer_expired",
    "transfer_offer_expired",
    "withdrawn",
}


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _clean_text(value: Any, *, max_chars: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:max_chars]


def _clean_list(values: Any, *, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        clean = _token(value).upper() if str(value or "").strip().upper().startswith("Q") else _token(value)
        if not clean:
            continue
        if allowed is not None and clean not in allowed:
            continue
        if clean not in result:
            result.append(clean)
    return result


def _clean_text_list(
    values: Any,
    *,
    max_items: int = 50,
    max_chars: int = 300,
) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean_text(value, max_chars=max_chars)
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= max_items:
            break
    return result


def normalize_retarget_request(
    value: Mapping[str, Any] | None,
    *,
    paper_id: str = "",
    request_id: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    """Validate one user/program retarget request into a stable schema."""

    source = dict(value or {})
    mode = _token(source.get("mode") or RETARGET_MODE_AI)
    if mode not in RETARGET_MODES:
        raise ValueError("转投方式必须是指定期刊或 AI 智能选刊")
    tier = _token(source.get("publication_tier") or PUBLICATION_TIER_SCI)
    if tier not in PUBLICATION_TIERS:
        raise ValueError("期刊范围必须是 SCI/SCIE、中文核心或普刊")
    priority = _token(source.get("priority") or RETARGET_PRIORITY_ACCEPTANCE)
    if priority not in RETARGET_PRIORITIES:
        raise ValueError("选刊优先级无效")
    withdrawal_policy = _token(
        source.get("withdrawal_policy") or WITHDRAWAL_POLICY_AUTO
    )
    if withdrawal_policy not in WITHDRAWAL_POLICIES:
        raise ValueError("撤稿策略无效")

    target_journal = _clean_text(source.get("target_journal"), max_chars=300)
    if mode == RETARGET_MODE_SPECIFIC and not target_journal:
        raise ValueError("指定期刊转投必须填写目标期刊")
    if mode == RETARGET_MODE_AI:
        target_journal = ""

    quartiles = _clean_list(
        source.get("quartiles") or ["Q3", "Q4"],
        allowed={"Q1", "Q2", "Q3", "Q4"},
    )
    if tier != PUBLICATION_TIER_SCI:
        quartiles = []
    oa_policy = _token(source.get("oa_policy") or "subscription_first")
    if oa_policy not in {"subscription_first", "either", "oa_allowed", "oa_required"}:
        oa_policy = "subscription_first"

    status = _token(source.get("status") or "requested")
    if not status:
        status = "requested"
    try:
        historical_target_retry_count = int(
            source.get("historical_target_retry_count") or 0
        )
    except (TypeError, ValueError):
        historical_target_retry_count = 0
    historical_target_retry_count = max(0, min(historical_target_retry_count, 20))
    normalized = {
        "schema": RETARGET_REQUEST_SCHEMA,
        "request_id": _clean_text(request_id or source.get("request_id"), max_chars=160),
        "paper_id": _clean_text(paper_id or source.get("paper_id"), max_chars=240),
        "status": status,
        "mode": mode,
        "target_journal": target_journal,
        "publication_tier": tier,
        "priority": priority,
        "quartiles": quartiles,
        "oa_policy": oa_policy,
        "max_apc": _clean_text(source.get("max_apc"), max_chars=80),
        "keywords": _clean_text(source.get("keywords"), max_chars=600),
        "allow_special_issue": bool(source.get("allow_special_issue", True)),
        "withdrawal_policy": withdrawal_policy,
        "closure_confirmed_by_user": bool(
            source.get("closure_confirmed_by_user")
            or withdrawal_policy == WITHDRAWAL_POLICY_CONFIRMED_CLOSED
        ),
        "reason": _clean_text(
            source.get("reason") or "user_requested_retarget",
            max_chars=1000,
        ),
        "notes": _clean_text(source.get("notes"), max_chars=2000),
        "auto_submit_after_package": bool(
            source.get("auto_submit_after_package", True)
        ),
        # Frozen by the deterministic controller when journal selection starts.
        # These are data, not prompt instructions: a later retry must retain the
        # exact historical journals that are forbidden for this request.
        "excluded_journals": _clean_text_list(source.get("excluded_journals")),
        "historical_target_retry_count": historical_target_retry_count,
        "created_at": _clean_text(
            created_at or source.get("created_at"), max_chars=80
        ),
    }
    return normalized


def retarget_request_is_active(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and str(value.get("schema") or "") == RETARGET_REQUEST_SCHEMA
        and _token(value.get("status")) in RETARGET_REQUEST_ACTIVE_STATUSES
    )


def retarget_withdrawal_decision(
    paper: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Decide whether journal selection may start or online closure is needed.

    Dates alone never close a submission.  ``ready_for_selection`` is returned
    only when the identity ledger has no active record, the paper has explicit
    terminal evidence, or the user explicitly confirms that the old attempt is
    already closed.
    """

    normalized = normalize_retarget_request(request)
    identity = submission_identity_snapshot(paper)
    active_ids = [str(item) for item in identity.get("active_submission_ids", [])]
    policy = normalized["withdrawal_policy"]
    status = _token(paper.get("status"))

    if policy == WITHDRAWAL_POLICY_CONFIRMED_CLOSED:
        return {
            "decision": "ready_for_selection",
            "reason": "user_confirmed_prior_submission_closed",
            "requires_online_action": False,
            "withdrawal_required": False,
            "closure_targets": active_ids,
            "closure_status": "user_confirmed_closed",
            "identity": identity,
        }

    if active_ids:
        return {
            "decision": "verify_or_withdraw",
            "reason": (
                "forced_withdrawal_requested"
                if policy == WITHDRAWAL_POLICY_FORCE
                else "active_submission_requires_live_status_verification"
            ),
            "requires_online_action": True,
            "withdrawal_required": True,
            "closure_targets": active_ids,
            "closure_status": "withdrawn",
            "identity": identity,
        }

    if identity.get("record_count"):
        return {
            "decision": "ready_for_selection",
            "reason": "all_recorded_submission_attempts_are_closed",
            "requires_online_action": False,
            "withdrawal_required": False,
            "closure_targets": [],
            "closure_status": "",
            "identity": identity,
        }

    if status in _PAPER_CLOSED_STATUSES:
        return {
            "decision": "ready_for_selection",
            "reason": f"explicit_closed_paper_status:{status}",
            "requires_online_action": False,
            "withdrawal_required": False,
            "closure_targets": [],
            "closure_status": status,
            "identity": identity,
        }

    has_prior_evidence = any(
        str(paper.get(key) or "").strip()
        for key in (
            "submission_id",
            "manuscript_id",
            "submission_number",
            "tracking_id",
            "confirmation_url",
            "submission_confirmation_url",
            "submission_evidence_path",
        )
    ) or isinstance(paper.get("submission_confirmation_receipt"), Mapping)
    if has_prior_evidence:
        return {
            "decision": "verify_or_withdraw",
            "reason": "prior_submission_evidence_requires_live_closure_check",
            "requires_online_action": True,
            "withdrawal_required": policy == WITHDRAWAL_POLICY_FORCE,
            "closure_targets": [],
            "closure_status": "withdrawn",
            "identity": identity,
        }

    return {
        "decision": "ready_for_selection",
        "reason": "no_prior_submission_attempt_detected",
        "requires_online_action": False,
        "withdrawal_required": False,
        "closure_targets": [],
        "closure_status": "",
        "identity": identity,
    }


def journal_preferences_patch(request: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a retarget request into existing journal-author preferences."""

    normalized = normalize_retarget_request(request)
    mode = normalized["mode"]
    tier = normalized["publication_tier"]
    priority = normalized["priority"]
    priority_text = {
        RETARGET_PRIORITY_ACCEPTANCE: "safest-fit and acceptance likelihood first",
        RETARGET_PRIORITY_SPEED: "short review cycle and operational reliability first",
        RETARGET_PRIORITY_QUALITY: "journal quality and academic influence first",
        RETARGET_PRIORITY_BALANCED: "balance fit, quality, review speed, and acceptance risk",
    }[priority]
    tier_text = {
        PUBLICATION_TIER_SCI: "SCI/SCIE journal",
        PUBLICATION_TIER_CHINESE_CORE: "Chinese core journal",
        PUBLICATION_TIER_GENERAL: "general academic journal",
    }[tier]
    if mode == RETARGET_MODE_SPECIFIC:
        strategy = (
            "Use the exact user-specified journal; verify its live requirements and "
            "rebuild the package for that journal."
        )
        preferred = normalized["target_journal"]
    else:
        strategy = f"AI select a {tier_text}; {priority_text}."
        preferred = ""
    quartile_flags = {
        key: key in normalized["quartiles"] for key in ("Q1", "Q2", "Q3", "Q4")
    }
    indexing = {
        "SCIE": tier == PUBLICATION_TIER_SCI,
        "ESCI": False,
        "Scopus": tier == PUBLICATION_TIER_SCI,
    }
    return {
        "targeting_mode": mode,
        "publication_tier": tier,
        "selection_priority": priority,
        "specific_journal": normalized["target_journal"],
        "allow_special_issue": normalized["allow_special_issue"],
        "quartiles": quartile_flags,
        "indexing": indexing,
        "strategy": strategy,
        "preferred_journals": preferred,
        "oa_policy": normalized["oa_policy"],
        "non_oa_required": normalized["oa_policy"] == "subscription_first",
        "max_apc": normalized["max_apc"],
        "keywords": normalized["keywords"],
        "retarget_notes": normalized["notes"],
    }


def retarget_mode_label(value: Any) -> str:
    return {
        RETARGET_MODE_SPECIFIC: "指定期刊",
        RETARGET_MODE_AI: "AI 智能选刊",
    }.get(_token(value), "-")


def publication_tier_label(value: Any) -> str:
    return {
        PUBLICATION_TIER_SCI: "SCI/SCIE",
        PUBLICATION_TIER_CHINESE_CORE: "中文核心",
        PUBLICATION_TIER_GENERAL: "普刊",
    }.get(_token(value), "-")
