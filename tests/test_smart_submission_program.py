from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

from unified_paper_orchestrator.smart_submission_program import (
    FINAL_SUBMIT_AUTHORIZED_ACTION,
    FINAL_SUBMIT_REVIEW_READY_ACTION,
    ProgramAction,
    ProgramExecutor,
    ProgramPhase,
    build_program_plan,
    program_database_path,
    sync_program_tasks,
)
from unified_paper_orchestrator_v2.store import EventStore


NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)


def _state(
    papers: dict[str, dict],
    *,
    online_queue: list[str] | None = None,
    local_queue: list[str] | None = None,
    mode: str = "run_once",
    max_parallel: int = 4,
    auto_final_submit: bool = True,
) -> dict:
    return {
        "version": 3,
        "mode": mode,
        "updated_at": "2026-07-16T09:00:00+00:00",
        "papers": papers,
        "paper_queue": list(local_queue or []),
        "online_queue": list(online_queue or []),
        "settings": {
            "max_parallel_workers": max_parallel,
            "daily_monitor_enabled": True,
            "auto_final_submit_enabled": auto_final_submit,
        },
    }


def test_program_plan_is_pure_and_respects_queue_order_and_capacity() -> None:
    state = _state(
        {
            "running": {
                "status": "uploading",
                "workflow_status": "running",
                "thread_id": "thread-running",
            },
            "first": {"status": "ready_for_online_submit", "workflow_status": "queued"},
            "second": {"status": "uploading", "workflow_status": "queued"},
        },
        online_queue=["running", "first", "second"],
        max_parallel=2,
    )
    before = copy.deepcopy(state)

    plan = build_program_plan(state, now=NOW)

    assert state == before
    assert plan["active_workers"] == ["running"]
    assert plan["available_slots"] == 1
    assert plan["dispatch_candidate_ids"] == ["first"]
    assert (
        plan["papers"]["first"]["action"]
        == ProgramAction.EXECUTE_SITE_STEP.value
    )
    assert (
        plan["papers"]["first"]["executor"]
        == ProgramExecutor.CODEX_SDK_BROWSER.value
    )


def test_selected_scope_cannot_hide_an_existing_global_worker() -> None:
    state = _state(
        {
            "running": {
                "status": "uploading",
                "workflow_status": "running",
                "thread_id": "thread-running",
            },
            "selected": {"status": "uploading", "workflow_status": "queued"},
        },
        online_queue=["running", "selected"],
        max_parallel=1,
    )

    plan = build_program_plan(state, selected_ids=["selected"], now=NOW)

    assert plan["paper_count"] == 2
    assert plan["selected_paper_ids"] == ["selected"]
    assert plan["active_worker_count"] == 1
    assert plan["available_slots"] == 0
    assert plan["dispatch_candidate_ids"] == []


def test_missing_binding_becomes_program_discovery_and_later_candidate_fills_slot() -> None:
    state = _state(
        {
            "missing": {"status": "ready_for_online_submit", "workflow_status": "queued"},
            "ready": {"status": "uploading", "workflow_status": "queued"},
        },
        online_queue=["missing", "ready"],
        max_parallel=1,
    )
    bindings = {
        "missing": {
            "dispatch_binding_ready": False,
            "dispatch_binding_code": "SMART_PROGRAM_SITE_BINDING_MISSING",
        },
        "ready": {
            "dispatch_binding_ready": True,
            "credential_account_id": "account-1",
            "submission_system_id": "system-1",
            "target_origin": "https://submit.example.test",
        },
    }

    plan = build_program_plan(state, dispatch_bindings=bindings, now=NOW)

    missing = plan["papers"]["missing"]
    assert missing["phase"] == ProgramPhase.DISCOVERY.value
    assert missing["action"] == ProgramAction.DISCOVER_CONTEXT.value
    assert missing["executor"] == ProgramExecutor.DETERMINISTIC.value
    assert missing["dispatchable"] is False
    assert [item["paper_id"] for item in plan["online_control_actions"]] == ["missing"]
    assert plan["dispatch_candidate_ids"] == ["ready"]


def test_program_projection_keeps_the_resolved_binding_system() -> None:
    state = _state(
        {
            "paper": {
                "status": "monitoring",
                "workflow_status": "queued",
                "monitor_check_pending": True,
                "submission_system_id": "stale_system",
            }
        },
        online_queue=["paper"],
    )
    binding = {
        "paper": {
            "dispatch_binding_ready": True,
            "credential_account_id": "account-1",
            "submission_system_id": "taylor_francis",
            "target_origin": "https://rp.tandfonline.com",
        }
    }

    plan = build_program_plan(state, dispatch_bindings=binding, now=NOW)

    assert plan["papers"]["paper"]["submission_system_id"] == "taylor_francis"


def test_final_review_is_program_authorized_and_unknown_result_never_retries_click() -> None:
    state = _state(
        {
            "approval": {
                "status": "uploading",
                "workflow_status": "queued",
                "next_action": FINAL_SUBMIT_REVIEW_READY_ACTION,
            },
            "unknown": {
                "status": "final_submit_result_unknown",
                "workflow_status": "queued",
            },
        },
        online_queue=["approval", "unknown"],
    )

    plan = build_program_plan(state, now=NOW)

    approval = plan["papers"]["approval"]
    unknown = plan["papers"]["unknown"]
    assert approval["phase"] == ProgramPhase.WAITING_FINAL_APPROVAL.value
    assert approval["action"] == ProgramAction.AUTO_AUTHORIZE_FINAL_SUBMIT.value
    assert approval["executor"] == ProgramExecutor.DETERMINISTIC.value
    assert approval["dispatchable"] is False
    assert unknown["phase"] == ProgramPhase.UNKNOWN_FINAL_RESULT.value
    assert unknown["action"] == ProgramAction.VERIFY_FINAL_RESULT.value
    assert unknown["action"] != ProgramAction.FINAL_SUBMIT.value


def test_manual_final_confirmation_remains_available_when_auto_policy_is_disabled() -> None:
    state = _state(
        {
            "approval": {
                "status": "waiting",
                "workflow_status": "waiting",
                "next_action": FINAL_SUBMIT_REVIEW_READY_ACTION,
            }
        },
        online_queue=["approval"],
        auto_final_submit=False,
    )

    plan = build_program_plan(state, now=NOW)

    approval = plan["papers"]["approval"]
    assert approval["action"] == ProgramAction.WAIT_FINAL_APPROVAL.value
    assert approval["executor"] == ProgramExecutor.HUMAN.value


def test_scheduled_monitoring_is_once_per_day() -> None:
    state = _state(
        {
            "due": {
                "status": "monitoring",
                "last_monitor_checked_at": "2026-07-15T23:59:00+00:00",
            },
            "done_today": {
                "status": "monitoring",
                "last_monitor_checked_at": "2026-07-16T01:00:00+00:00",
            },
        },
        mode="scheduled",
    )

    plan = build_program_plan(state, now=NOW)

    assert [item["paper_id"] for item in plan["monitor_candidates"]] == ["due"]
    assert plan["papers"]["due"]["executor"] == ProgramExecutor.DETERMINISTIC.value


def test_queued_daily_monitor_check_uses_one_bounded_sdk_browser_turn() -> None:
    state = _state(
        {
            "monitor": {
                "status": "monitoring",
                "workflow_status": "queued",
                "monitor_check_pending": True,
                "submission_confirmation_receipt": {"status": "submitted"},
            }
        },
        online_queue=["monitor"],
        mode="scheduled",
        max_parallel=1,
    )

    plan = build_program_plan(state, now=NOW)

    decision = plan["papers"]["monitor"]
    assert decision["action"] == ProgramAction.MONITOR_STATUS.value
    assert decision["executor"] == ProgramExecutor.CODEX_SDK_BROWSER.value
    assert decision["dispatchable"] is True
    assert plan["dispatch_candidate_ids"] == ["monitor"]


def test_revision_state_wins_over_old_submission_evidence() -> None:
    state = _state(
        {
            "revision": {
                "status": "revision_needed",
                "workflow_status": "revision_needed",
                "submission_confirmation_receipt": {"status": "submitted"},
            }
        }
    )

    plan = build_program_plan(state, now=NOW)

    decision = plan["papers"]["revision"]
    assert decision["phase"] == ProgramPhase.ACTION_REVIEW.value
    assert decision["action"] == ProgramAction.INSPECT_ACTION_EVIDENCE.value
    assert decision["action"] != ProgramAction.MONITOR_EMAIL.value


def test_explicit_revision_kinds_have_distinct_program_phases() -> None:
    state = _state(
        {
            "technical": {
                "status": "revision_needed",
                "workflow_status": "queued",
                "submission_id": "MS-1",
                "last_monitor_action_kind": "technical_correction",
            },
            "scientific": {
                "status": "revision_needed",
                "workflow_status": "waiting_revision_package",
                "submission_id": "MS-2",
                "last_monitor_action_kind": "scientific_revision",
            },
        },
        online_queue=["technical"],
    )

    plan = build_program_plan(state, now=NOW)

    assert plan["papers"]["technical"]["phase"] == ProgramPhase.TECHNICAL_CORRECTION.value
    assert plan["papers"]["technical"]["action"] == ProgramAction.APPLY_TECHNICAL_CORRECTION.value
    assert plan["papers"]["technical"]["dispatchable"] is True
    assert plan["papers"]["scientific"]["phase"] == ProgramPhase.SCIENTIFIC_REVISION.value
    assert plan["papers"]["scientific"]["action"] == ProgramAction.PREPARE_SCIENTIFIC_REVISION.value
    assert plan["papers"]["scientific"]["dispatchable"] is False


def test_duplicate_active_submission_ids_are_program_blocked() -> None:
    state = _state(
        {
            "duplicate": {
                "status": "revision_needed",
                "workflow_status": "queued",
                "target_journal": "Example Journal",
                "submission_system_id": "snapp",
                "submission_id": "MS-1",
                "submission_records": {
                    "MS-1": {
                        "submission_id": "MS-1",
                        "target_journal": "Example Journal",
                        "submission_system_id": "snapp",
                        "active": True,
                        "canonical": True,
                    },
                    "MS-2": {
                        "submission_id": "MS-2",
                        "target_journal": "Example Journal",
                        "submission_system_id": "snapp",
                        "active": True,
                    },
                },
            }
        },
        online_queue=["duplicate"],
    )

    decision = build_program_plan(state, now=NOW)["papers"]["duplicate"]

    assert decision["phase"] == ProgramPhase.IDENTITY_RECONCILIATION.value
    assert decision["action"] == ProgramAction.RECONCILE_SUBMISSION_IDENTITY.value
    assert decision["executor"] == ProgramExecutor.DETERMINISTIC.value
    assert decision["dispatchable"] is False


def test_running_daily_monitor_is_counted_as_an_active_worker() -> None:
    state = _state(
        {
            "monitor": {
                "status": "monitoring",
                "workflow_status": "running",
                "thread_id": "thread-monitor",
                "monitor_check_pending": True,
            }
        },
        online_queue=["monitor"],
        mode="scheduled",
        max_parallel=1,
    )

    plan = build_program_plan(state, now=NOW)

    assert plan["active_workers"] == ["monitor"]
    assert plan["available_slots"] == 0
    assert plan["dispatch_candidate_ids"] == []
    assert (
        plan["papers"]["monitor"]["action"]
        == ProgramAction.WAIT_CURRENT_ACTION.value
    )


def test_manual_takeover_holds_only_the_selected_paper_out_of_dispatch() -> None:
    state = _state(
        {
            "manual": {
                "status": "uploading",
                "workflow_status": "manual_takeover",
                "manual_takeover": {"status": "active"},
            },
            "other": {
                "status": "uploading",
                "workflow_status": "queued",
            },
        },
        online_queue=["other"],
        max_parallel=2,
    )

    plan = build_program_plan(state, now=NOW)

    manual = plan["papers"]["manual"]
    assert manual["phase"] == ProgramPhase.MANUAL_TAKEOVER.value
    assert manual["action"] == ProgramAction.WAIT_HUMAN.value
    assert manual["executor"] == ProgramExecutor.HUMAN.value
    assert manual["dispatchable"] is False
    assert plan["dispatch_candidate_ids"] == ["other"]


def test_completed_manual_takeover_requires_one_bounded_page_reconciliation() -> None:
    state = _state(
        {
            "manual": {
                "status": "waiting",
                "workflow_status": "queued",
                "manual_takeover": {"status": "completed"},
                "manual_takeover_reconcile_pending": True,
            }
        },
        online_queue=["manual"],
        max_parallel=1,
    )

    plan = build_program_plan(state, now=NOW)

    decision = plan["papers"]["manual"]
    assert decision["phase"] == ProgramPhase.SITE_EXECUTION.value
    assert decision["action"] == ProgramAction.EXECUTE_SITE_STEP.value
    assert decision["executor"] == ProgramExecutor.CODEX_SDK_BROWSER.value
    assert decision["dispatchable"] is True
    assert plan["dispatch_candidate_ids"] == ["manual"]


def test_program_ledger_is_idempotent_and_never_dispatches_side_effects(tmp_path: Path) -> None:
    state_path = tmp_path / "smart_submission_state.json"
    state = _state(
        {
            "local": {"status": "queued", "workflow_status": "queued"},
            "final": {
                "status": "uploading",
                "workflow_status": "queued",
                "next_action": FINAL_SUBMIT_AUTHORIZED_ACTION,
            },
        },
        local_queue=["local"],
        online_queue=["final"],
    )
    plan = build_program_plan(state, now=NOW)

    first = sync_program_tasks(plan, state_path=state_path)
    second = sync_program_tasks(plan, state_path=state_path)

    assert first["side_effects_dispatched"] is False
    assert second["side_effects_dispatched"] is False
    database = program_database_path(state_path)
    assert database.exists()
    tasks = EventStore(database).list_tasks(limit=20)
    assert len(tasks) == 2
    final = next(item for item in tasks if item["paper_id"] == "final")
    # The isolated ledger keeps the kernel-wide submission.execute approval
    # marker, but it cannot dispatch side effects. Production authority comes
    # from Smart's signed one-use capability, not from this read-only projection.
    assert final["status"] == "waiting_approval"
    assert final["envelope"]["payload"]["program_action"] == ProgramAction.FINAL_SUBMIT.value
