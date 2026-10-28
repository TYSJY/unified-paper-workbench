from __future__ import annotations

import pytest

from unified_paper_orchestrator.backend_router import BackendRouter
from unified_paper_orchestrator.models import action_label
from unified_paper_orchestrator.orchestrator import STAGE_LABELS, V1_BACKEND_ACTION_SPECS, UnifiedOrchestrator
from unified_paper_orchestrator_v2.read_models import plan_for_snapshot


@pytest.mark.parametrize(
    "record",
    [
        {"current_stage": "writing", "next_action": "queue_writing"},
        {"current_stage": "figures", "next_action": "queue_figures"},
        {"current_stage": "journal_author", "next_action": "queue_journal_author"},
        {
            "current_stage": "submission",
            "next_action": "queue_submission",
            "submission_route": {"execution_owner": "legacy_auto"},
        },
        {
            "current_stage": "submission",
            "next_action": "queue_submission",
            "submission_route": {"execution_owner": "smart"},
        },
        {"current_stage": "figures", "next_action": "wait_figures"},
        {"current_stage": "", "next_action": "none"},
    ],
)
def test_v2_pure_plan_matches_frozen_v1_behavior(record: dict[str, object]) -> None:
    orchestrator = UnifiedOrchestrator()
    action = str(record.get("next_action") or "none")
    snapshot = {
        "paper_id": "paper-1",
        "record": record,
        "stage_labels": STAGE_LABELS,
        "action_label": action_label(action),
    }

    assert plan_for_snapshot(snapshot) == orchestrator._plan_for_legacy("paper-1", record)


def test_v1_plan_can_run_v2_in_read_only_shadow_without_changing_result() -> None:
    orchestrator = UnifiedOrchestrator()
    orchestrator.backend_router = BackendRouter(V1_BACKEND_ACTION_SPECS, {"_plan_for": "shadow"})
    record = {
        "current_stage": "submission",
        "next_action": "queue_submission",
        "submission_route": {"execution_owner": "legacy_auto"},
    }

    result = orchestrator._plan_for("paper-1", record)

    assert result == orchestrator._plan_for_legacy("paper-1", record)
    observation = orchestrator.backend_router.observations()[-1]
    assert observation.status == "compared"
    assert observation.matched is True


def test_v1_plan_can_switch_to_new_as_the_only_backend_when_explicitly_unlocked() -> None:
    orchestrator = UnifiedOrchestrator()
    orchestrator.backend_router = BackendRouter(
        V1_BACKEND_ACTION_SPECS,
        {"_plan_for": "new"},
        allow_new=True,
    )
    record = {"current_stage": "writing", "next_action": "queue_writing"}

    assert orchestrator._plan_for("paper-1", record) == orchestrator._plan_for_legacy("paper-1", record)
