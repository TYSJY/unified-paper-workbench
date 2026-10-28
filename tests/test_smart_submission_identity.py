from __future__ import annotations

from unified_paper_orchestrator.smart_submission_identity import (
    IDENTITY_CLOSED,
    IDENTITY_CONFLICT,
    IDENTITY_VERIFIED,
    classify_monitor_action,
    close_submission_record,
    project_submission_identity,
    submission_identity_snapshot,
    upsert_submission_record,
)


def _paper() -> dict[str, object]:
    return {
        "paper_id": "paper-1",
        "status": "revision_needed",
        "target_journal": "Example Journal",
        "submission_system_id": "snapp",
    }


def test_one_active_submission_is_the_only_mutable_identity() -> None:
    paper = _paper()
    paper["submission_id"] = "MS-26-001"

    snapshot = project_submission_identity(paper)

    assert snapshot["status"] == IDENTITY_VERIFIED
    assert snapshot["canonical_submission_id"] == "MS-26-001"
    assert snapshot["mutation_allowed"] is True
    assert snapshot["new_submission_allowed"] is False


def test_two_active_ids_on_the_same_route_block_all_mutation() -> None:
    paper = _paper()
    upsert_submission_record(
        paper,
        "MS-26-001",
        submission_system_id="snapp",
        target_journal="Example Journal",
        status="revision_needed",
        active=True,
        canonical=True,
    )
    upsert_submission_record(
        paper,
        "MS-26-002",
        submission_system_id="snapp",
        target_journal="Example Journal",
        status="revision_needed",
        active=True,
        canonical=False,
    )

    snapshot = submission_identity_snapshot(paper)

    assert snapshot["status"] == IDENTITY_CONFLICT
    assert snapshot["duplicate_submission_ids"] == ["MS-26-001", "MS-26-002"]
    assert snapshot["mutation_allowed"] is False
    assert snapshot["new_submission_allowed"] is False


def test_confirmed_rejection_closes_old_id_and_allows_one_new_retarget() -> None:
    paper = _paper()
    paper["submission_id"] = "MS-26-001"
    project_submission_identity(paper)

    close_submission_record(
        paper,
        "MS-26-001",
        status="rejected",
        source="unit_test",
    )
    snapshot = project_submission_identity(paper)

    assert snapshot["status"] == IDENTITY_CLOSED
    assert snapshot["active_submission_ids"] == []
    assert snapshot["new_submission_allowed"] is True


def test_monitor_action_classifier_is_conservative_and_deterministic() -> None:
    assert classify_monitor_action(
        status="revision_needed",
        action_kind="unknown",
        summary="Please select a collection and resubmit the amendment.",
    ) == "technical_correction"
    assert classify_monitor_action(
        status="revision_needed",
        action_kind="unknown",
        summary="The manuscript was rejected; withdraw and submit elsewhere.",
    ) == "retarget"
    assert classify_monitor_action(
        status="revision_needed",
        action_kind="unknown",
        summary="Author action is required.",
    ) == "unknown"
