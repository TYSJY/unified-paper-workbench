from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import unified_paper_orchestrator_v2.executors.legacy as legacy_module
from unified_paper_orchestrator_v2.contracts import ApprovalPolicy, TaskEnvelope
from unified_paper_orchestrator_v2.executors.legacy import (
    AUTOMATION_CONTROL_ENV,
    LegacyCodexAutomationExecutor,
)


PAPER_ID = "2026-07-12_001_executor-test"


@pytest.fixture(autouse=True)
def _stable_legacy_execution_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy_module, "_stored_execution_mode", lambda: "legacy_codex")


class FakeController:
    def __init__(self) -> None:
        self.read_calls: list[str] = []
        self.set_calls: list[tuple[str, bool]] = []
        self.configs: list[dict[str, object]] = [
            {
                "id": "automation-test",
                "exists": True,
                "status": "ACTIVE",
                "prompt": "SECRET PROMPT BODY",
                "password": "SECRET CREDENTIAL",
                "path": "C:/secret/path/automation.toml",
            }
        ]

    def read_statuses(self, stage: str) -> list[dict[str, object]]:
        self.read_calls.append(stage)
        return self.configs

    def status(self, _stage: str) -> dict[str, object]:
        raise AssertionError("mutating status() must never be used")

    def set_enabled(
        self,
        stage: str,
        enabled: bool,
        _state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.set_calls.append((stage, enabled))
        return {
            "updated": [
                {
                    "changed": True,
                    "path": "C:/secret/path/automation.toml",
                    "backup": "SECRET BACKUP PATH",
                }
            ],
            "warnings": ["SECRET WARNING"],
            "primary": "C:/secret/path/automation.toml",
        }


def task(
    action: str,
    *,
    target_stage: str = "writing",
    approval_policy: ApprovalPolicy = ApprovalPolicy.NONE,
    network_access: bool = False,
    allowed_tools: list[str] | None = None,
    writable_roots: list[str] | None = None,
) -> TaskEnvelope:
    operation = action.rsplit(".", 1)[-1]
    operation_id = "executor-test-operation" if operation in {"start", "pause"} else ""
    return TaskEnvelope(
        paper_id=PAPER_ID,
        stage="system",
        action=action,
        executor="legacy_codex",
        idempotency_key=(f"legacy-automation:{operation}:{target_stage}:{operation_id}" if operation_id else ""),
        payload={"target_stage": target_stage, "operation_id": operation_id},
        approval_policy=approval_policy,
        network_access=network_access,
        allowed_tools=allowed_tools or [],
        writable_roots=writable_roots or [],
    )


@pytest.mark.parametrize("target_stage", ["writing", "figures", "journal_author", "submission"])
def test_status_is_read_only_compact_and_supports_all_four_stages(target_stage: str) -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller)

    result = executor.execute(task("legacy.automation.status", target_stage=target_stage))

    assert result.status.value == "completed"
    assert controller.read_calls == [target_stage]
    assert controller.set_calls == []
    assert result.data["automation"] == {
        "target_stage": target_stage,
        "read_only": True,
        "config_count": 1,
        "active_count": 1,
        "statuses": [{"id": "automation-test", "exists": True, "status": "ACTIVE"}],
    }
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "SECRET" not in serialized
    assert "prompt" not in serialized.lower()
    assert "credential" not in serialized.lower()


@pytest.mark.parametrize("action,enabled", [("legacy.automation.start", True), ("legacy.automation.pause", False)])
@pytest.mark.parametrize("target_stage", ["writing", "figures", "journal_author", "submission"])
def test_approved_control_calls_only_set_enabled(
    action: str,
    enabled: bool,
    target_stage: str,
) -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller, allow_control=True)

    result = executor.execute(
        task(
            action,
            target_stage=target_stage,
            approval_policy=ApprovalPolicy.REQUIRED,
        )
    )

    assert result.status.value == "completed"
    assert controller.set_calls == [(target_stage, enabled)]
    assert controller.read_calls == []
    assert result.data["requested_enabled"] is enabled
    assert result.data["updated_count"] == 1
    assert result.data["changed_count"] == 1
    assert result.data["warning_count"] == 1
    assert result.data["gateway_business_write_enabled"] is False
    assert result.outputs == []
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "SECRET" not in serialized
    assert "automation.toml" not in serialized


def test_retired_writing_model_scheduler_cannot_be_started_or_reconciled() -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(
        controller,
        allow_control=True,
        retire_writing_start=True,
    )
    request = task(
        "legacy.automation.start",
        target_stage="writing",
        approval_policy=ApprovalPolicy.REQUIRED,
    )

    result = executor.execute(request)
    reconciled = executor.reconcile(request)

    assert result.status.value == "blocked"
    assert result.blocker_code == "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED"
    assert reconciled.status.value == "blocked"
    assert reconciled.blocker_code == "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED"
    assert controller.set_calls == []
    assert controller.read_calls == []


def test_control_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AUTOMATION_CONTROL_ENV, raising=False)
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller)

    result = executor.execute(task("legacy.automation.start", approval_policy=ApprovalPolicy.REQUIRED))

    assert result.status.value == "blocked"
    assert result.blocker_code == "LEGACY_AUTOMATION_CONTROL_DISABLED"
    assert controller.set_calls == []


def test_exact_process_switch_enables_control(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTOMATION_CONTROL_ENV, "1")
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller)

    result = executor.execute(task("legacy.automation.pause", approval_policy=ApprovalPolicy.REQUIRED))

    assert result.status.value == "completed"
    assert controller.set_calls == [("writing", False)]


@pytest.mark.parametrize("unsafe_value", ["true", "yes", "TRUE", " 1 "])
def test_process_switch_fails_closed_for_non_exact_values(
    unsafe_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AUTOMATION_CONTROL_ENV, unsafe_value)
    controller = FakeController()

    result = LegacyCodexAutomationExecutor(controller).execute(
        task("legacy.automation.start", approval_policy=ApprovalPolicy.REQUIRED)
    )

    assert result.blocker_code == "LEGACY_AUTOMATION_CONTROL_DISABLED"
    assert controller.set_calls == []


def test_start_and_pause_require_explicit_approval() -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller, allow_control=True)

    result = executor.execute(task("legacy.automation.start"))

    assert result.status.value == "blocked"
    assert result.blocker_code == "LEGACY_AUTOMATION_APPROVAL_REQUIRED"
    assert controller.set_calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"network_access": True},
        {"allowed_tools": ["browser"]},
        {"writable_roots": ["C:/tmp"]},
    ],
)
def test_automation_actions_require_zero_capabilities(kwargs: dict[str, object]) -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller, allow_control=True)

    result = executor.execute(
        task(
            "legacy.automation.start",
            approval_policy=ApprovalPolicy.REQUIRED,
            **kwargs,
        )
    )

    assert result.status.value == "blocked"
    assert result.blocker_code == "LEGACY_AUTOMATION_CAPABILITIES_DENIED"
    assert controller.read_calls == []
    assert controller.set_calls == []


@pytest.mark.parametrize("target_stage", ["", "system", "smart_submission", "Writing"])
def test_target_stage_is_strictly_limited(target_stage: str) -> None:
    controller = FakeController()

    result = LegacyCodexAutomationExecutor(controller, allow_control=True).execute(
        task(
            "legacy.automation.start",
            target_stage=target_stage,
            approval_policy=ApprovalPolicy.REQUIRED,
        )
    )

    assert result.blocker_code == "LEGACY_AUTOMATION_STAGE_INVALID"
    assert controller.read_calls == []
    assert controller.set_calls == []


def test_legacy_plan_adds_read_only_automation_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = tmp_path / "unified_state.json"
    projection.write_text(
        json.dumps(
            {
                "papers": {
                    PAPER_ID: {
                        "current_stage": "figures",
                        "next_action": "figures.plan",
                        "blocked_reason": "",
                        "credential": "MUST NOT BE PROJECTED",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(legacy_module, "LEGACY_UNIFIED_STATE_PATH", projection)
    controller = FakeController()

    result = LegacyCodexAutomationExecutor(controller).execute(task("legacy.plan"))

    assert result.status.value == "completed"
    assert result.data["paper_id"] == PAPER_ID
    assert result.data["current_stage"] == "figures"
    assert result.data["next_action"] == "figures.plan"
    assert result.data["applied"] is False
    assert result.data["automation"]["target_stage"] == "figures"
    assert result.data["automation"]["read_only"] is True
    assert controller.read_calls == ["figures"]
    assert controller.set_calls == []
    assert "MUST NOT BE PROJECTED" not in json.dumps(result.to_dict())


def test_legacy_apply_is_permanently_refused_even_when_control_is_enabled() -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(controller, allow_control=True)

    result = executor.execute(task("legacy.apply", approval_policy=ApprovalPolicy.REQUIRED))

    assert result.status.value == "blocked"
    assert result.blocker_code == "LEGACY_CODEX_GATEWAY_WRITES_DISABLED"
    assert controller.read_calls == []
    assert controller.set_calls == []
    assert "UnifiedOrchestrator" not in inspect.getsource(legacy_module)


@pytest.mark.parametrize(
    "action,method_name,expected_code",
    [
        ("legacy.automation.status", "read_statuses", "LEGACY_AUTOMATION_STATUS_FAILED"),
        ("legacy.automation.start", "set_enabled", "LEGACY_AUTOMATION_CONTROL_FAILED"),
    ],
)
def test_controller_failures_return_stable_sanitized_codes(
    action: str,
    method_name: str,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = FakeController()

    def fail(*_args, **_kwargs):
        raise RuntimeError("SECRET CONTROLLER DETAIL")

    monkeypatch.setattr(controller, method_name, fail)
    executor = LegacyCodexAutomationExecutor(controller, allow_control=True)
    result = executor.execute(task(action, approval_policy=ApprovalPolicy.REQUIRED))

    assert result.status.value == "blocked"
    assert result.blocker_code == expected_code
    assert "SECRET" not in json.dumps(result.to_dict(), ensure_ascii=False)


def test_control_without_a_matching_legacy_file_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = FakeController()
    monkeypatch.setattr(
        controller,
        "set_enabled",
        lambda _stage, _enabled: {
            "updated": [],
            "warnings": ["automation missing"],
        },
    )

    result = LegacyCodexAutomationExecutor(controller, allow_control=True).execute(
        task("legacy.automation.pause", approval_policy=ApprovalPolicy.REQUIRED)
    )

    assert result.status.value == "blocked"
    assert result.blocker_code == "LEGACY_AUTOMATION_UNAVAILABLE"


def test_api_mode_cannot_use_gateway_control_override_to_start_v1() -> None:
    controller = FakeController()
    executor = LegacyCodexAutomationExecutor(
        controller,
        allow_control=True,
        execution_mode_reader=lambda: "api_key",
    )

    result = executor.execute(task("legacy.automation.start", approval_policy=ApprovalPolicy.REQUIRED))

    assert result.status.value == "blocked"
    assert result.blocker_code == "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"
    assert controller.set_calls == []


def test_unknown_action_is_rejected_without_controller_access() -> None:
    controller = FakeController()

    result = LegacyCodexAutomationExecutor(controller, allow_control=True).execute(
        task("legacy.automation.delete", approval_policy=ApprovalPolicy.REQUIRED)
    )

    assert result.blocker_code == "LEGACY_CODEX_ACTION_UNSUPPORTED"
    assert controller.read_calls == []
    assert controller.set_calls == []
