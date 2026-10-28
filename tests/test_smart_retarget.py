from __future__ import annotations

import pytest

from unified_paper_orchestrator.smart_retarget import (
    journal_preferences_patch,
    normalize_retarget_request,
    retarget_withdrawal_decision,
)
from unified_paper_orchestrator.smart_submission_identity import (
    project_submission_identity,
    upsert_submission_record,
)
from unified_paper_orchestrator.smart_submission_program import decide_paper


def _request(**overrides):
    value = {
        "request_id": "retarget-1",
        "paper_id": "paper-a",
        "mode": "ai",
        "publication_tier": "sci",
        "priority": "acceptance",
        "withdrawal_policy": "auto",
        "status": "requested",
    }
    value.update(overrides)
    return normalize_retarget_request(value)


def test_specific_retarget_requires_a_target_journal() -> None:
    with pytest.raises(ValueError, match="目标期刊"):
        normalize_retarget_request({"mode": "specific"})

    request = normalize_retarget_request(
        {
            "mode": "specific",
            "target_journal": "Journal of Safe Systems",
            "withdrawal_policy": "auto",
        }
    )
    assert request["mode"] == "specific"
    assert request["target_journal"] == "Journal of Safe Systems"


def test_active_submission_requires_live_verification_before_retarget() -> None:
    paper = {"target_journal": "Old Journal", "submission_system_id": "springer_nature"}
    upsert_submission_record(
        paper,
        "OLD-123",
        target_journal="Old Journal",
        submission_system_id="springer_nature",
        status="under_review",
        active=True,
        canonical=True,
    )
    project_submission_identity(paper)

    decision = retarget_withdrawal_decision(paper, _request())

    assert decision["decision"] == "verify_or_withdraw"
    assert decision["closure_targets"] == ["OLD-123"]
    assert decision["requires_online_action"] is True


def test_explicitly_closed_or_expired_transfer_skips_withdrawal() -> None:
    paper = {"target_journal": "Old Journal", "submission_system_id": "springer_nature"}
    upsert_submission_record(
        paper,
        "OLD-123",
        target_journal="Old Journal",
        submission_system_id="springer_nature",
        status="transfer_offer_expired",
        active=False,
        canonical=False,
    )
    project_submission_identity(paper)

    decision = retarget_withdrawal_decision(paper, _request())

    assert decision["decision"] == "ready_for_selection"
    assert decision["withdrawal_required"] is False


def test_user_confirmed_closed_is_an_explicit_override() -> None:
    paper = {"target_journal": "Old Journal", "submission_system_id": "springer_nature"}
    upsert_submission_record(
        paper,
        "OLD-123",
        target_journal="Old Journal",
        submission_system_id="springer_nature",
        status="under_review",
        active=True,
        canonical=True,
    )
    project_submission_identity(paper)

    decision = retarget_withdrawal_decision(
        paper,
        _request(withdrawal_policy="confirmed_closed"),
    )

    assert decision["decision"] == "ready_for_selection"
    assert decision["reason"] == "user_confirmed_prior_submission_closed"
    assert decision["closure_targets"] == ["OLD-123"]


def test_ai_scope_is_translated_to_existing_journal_preferences() -> None:
    request = _request(
        publication_tier="chinese_core",
        priority="speed",
        quartiles=["Q1"],
        oa_policy="either",
    )

    patch = journal_preferences_patch(request)

    assert patch["targeting_mode"] == "ai"
    assert patch["publication_tier"] == "chinese_core"
    assert patch["selection_priority"] == "speed"
    assert patch["indexing"] == {"SCIE": False, "ESCI": False, "Scopus": False}
    assert not any(patch["quartiles"].values())


def test_specific_target_becomes_a_binding_preference() -> None:
    patch = journal_preferences_patch(
        _request(mode="specific", target_journal="Exact Target Journal")
    )

    assert patch["specific_journal"] == "Exact Target Journal"
    assert patch["preferred_journals"] == "Exact Target Journal"
    assert "exact user-specified journal" in patch["strategy"]


def test_retarget_request_preserves_frozen_historical_exclusions() -> None:
    request = normalize_retarget_request(
        {
            "mode": "ai",
            "excluded_journals": [
                "Old Journal",
                " old journal ",
                "Rejected Journal",
            ],
            "historical_target_retry_count": 2,
        }
    )

    assert request["excluded_journals"] == ["Old Journal", "Rejected Journal"]
    assert request["historical_target_retry_count"] == 2


def test_program_plan_bypasses_duplicate_guard_only_for_retarget_closure() -> None:
    paper = {
        "status": "revision_needed",
        "workflow_status": "queued",
        "online_intent": "user_retarget_verify_or_withdraw",
        "target_journal": "Old Journal",
        "submission_system_id": "springer_nature",
        "retarget_request": _request(status="identity_check_required"),
    }
    for submission_id in ("OLD-1", "OLD-2"):
        upsert_submission_record(
            paper,
            submission_id,
            target_journal="Old Journal",
            submission_system_id="springer_nature",
            status="active",
            active=True,
            canonical=submission_id == "OLD-1",
        )
    project_submission_identity(paper)

    decision = decide_paper(
        "paper-a",
        paper,
        in_local_queue=False,
        in_online_queue=True,
    )

    assert decision["phase"] == "retarget_status_check"
    assert decision["action"] == "submission.verify_or_withdraw_for_retarget"
    assert decision["dispatchable"] is True
    assert decision["executor"] == "codex_sdk_browser"
