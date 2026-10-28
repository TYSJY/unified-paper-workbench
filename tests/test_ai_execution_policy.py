from __future__ import annotations

import pytest

from unified_paper_orchestrator.ai_execution_policy import (
    API_KEY_MODE,
    LEGACY_CODEX_MODE,
    ExecutionModePolicyError,
    evaluate_execution_mode_switch,
    mode_snapshot_fields,
    require_no_cross_mode_fallback,
)


def _ready_profile() -> dict[str, object]:
    return {
        "profile_id": "relay-main",
        "revision": 3,
        "wire_api": "responses",
        "capability_status": "responses_compatible",
    }


def test_switch_to_api_requires_a_verified_profile_and_key() -> None:
    decision = evaluate_execution_mode_switch(
        current_mode=LEGACY_CODEX_MODE,
        requested_mode=API_KEY_MODE,
    )
    assert decision.allowed is False
    assert len(decision.blockers) == 3


def test_switch_to_api_is_allowed_only_after_both_execution_surfaces_are_idle() -> None:
    blocked = evaluate_execution_mode_switch(
        current_mode=LEGACY_CODEX_MODE,
        requested_mode=API_KEY_MODE,
        legacy_scheduler_statuses={"writing": {"status": "ACTIVE"}},
        gateway_task_counts={"running": 1, "completed": 20},
        active_profile=_ready_profile(),
        active_profile_has_key=True,
    )
    assert blocked.allowed is False
    assert any("writing" in item for item in blocked.blockers)
    assert any("running=1" in item for item in blocked.blockers)

    allowed = evaluate_execution_mode_switch(
        current_mode=LEGACY_CODEX_MODE,
        requested_mode=API_KEY_MODE,
        legacy_scheduler_statuses={"writing": {"status": "PAUSED"}},
        gateway_task_counts={"completed": 20},
        active_profile=_ready_profile(),
        active_profile_has_key=True,
    )
    assert allowed.allowed is True
    assert allowed.changed is True


def test_switch_back_to_missing_v1_bridge_is_allowed_with_an_explicit_warning() -> None:
    decision = evaluate_execution_mode_switch(
        current_mode=API_KEY_MODE,
        requested_mode=LEGACY_CODEX_MODE,
        legacy_scheduler_statuses={"writing": {"status": "MISSING"}},
    )
    assert decision.allowed is True
    assert decision.warnings


def test_mode_snapshot_never_enables_automatic_cross_mode_fallback() -> None:
    snapshot = mode_snapshot_fields(
        execution_mode=API_KEY_MODE,
        provider_profile_id="relay-main",
        provider_revision=3,
    )
    assert snapshot == {
        "execution_mode": API_KEY_MODE,
        "provider_profile_id": "relay-main",
        "provider_revision": 3,
        "automatic_cross_mode_fallback": False,
    }
    require_no_cross_mode_fallback(snapshot)

    with pytest.raises(ExecutionModePolicyError) as error:
        require_no_cross_mode_fallback(
            {**snapshot, "automatic_cross_mode_fallback": True}
        )
    assert error.value.code == "AI_CROSS_MODE_FALLBACK_FORBIDDEN"


def test_api_snapshot_cannot_be_created_without_a_profile() -> None:
    with pytest.raises(ExecutionModePolicyError) as error:
        mode_snapshot_fields(execution_mode=API_KEY_MODE)
    assert error.value.code == "AI_PROVIDER_PROFILE_REQUIRED"
