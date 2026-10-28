from __future__ import annotations

from pathlib import Path

import pytest

from unified_paper_orchestrator_v2.contracts import ApprovalPolicy, TaskEnvelope
from unified_paper_orchestrator_v2.executors.legacy import LegacyCodexAutomationExecutor
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway, GatewayError
from unified_paper_orchestrator_v2.staging import StagingStore


class FakeController:
    def __init__(self) -> None:
        self.read_calls: list[str] = []
        self.set_calls: list[tuple[str, bool]] = []

    def read_statuses(self, stage: str):
        self.read_calls.append(stage)
        return [{"id": f"{stage}-scheduler", "exists": True, "status": "PAUSED"}]

    def set_enabled(self, stage: str, enabled: bool, state=None):
        self.set_calls.append((stage, enabled))
        return {"updated": [{"changed": True}], "warnings": []}


def make_gateway(tmp_path: Path, controller: FakeController, *, allow_control: bool = True) -> ExecutionGateway:
    registry = ExecutorRegistry()
    registry.register(
        LegacyCodexAutomationExecutor(
            controller,
            allow_control=allow_control,
            execution_mode_reader=lambda: "legacy_codex",
        )
    )
    staging = StagingStore(tmp_path / "staging", acl_hardener=None)
    return ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging.root,
        registry=registry,
        staging_store=staging,
    )


def test_gateway_executes_real_read_only_legacy_automation_status(tmp_path: Path) -> None:
    controller = FakeController()
    gateway = make_gateway(tmp_path, controller)
    queued = gateway.submit_legacy_automation("status", "writing")

    completed = gateway.run_once(queued["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["data"]["automation"]["target_stage"] == "writing"
    assert controller.read_calls == ["writing"]
    assert controller.set_calls == []


def test_gateway_control_requires_durable_approval_and_is_idempotent(tmp_path: Path) -> None:
    controller = FakeController()
    gateway = make_gateway(tmp_path, controller)
    waiting = gateway.submit_legacy_automation(
        "start",
        "figures",
        operation_id="release-044-figures-start",
    )
    duplicate = gateway.submit_legacy_automation(
        "start",
        "figures",
        operation_id="release-044-figures-start",
    )

    assert waiting["status"] == "waiting_approval"
    assert duplicate["task_id"] == waiting["task_id"]
    assert controller.set_calls == []
    approved = gateway.approve(
        waiting["task_id"],
        note="test approval",
        expected_intent_sha256=waiting["approval"]["intent_sha256"],
    )
    assert approved["status"] == "queued"

    completed = gateway.run_once(waiting["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"
    assert controller.set_calls == [("figures", True)]
    assert completed["result"]["data"]["gateway_business_write_enabled"] is False


def test_duplicate_control_submission_after_approval_reuses_durable_task_id(
    tmp_path: Path,
) -> None:
    controller = FakeController()
    gateway = make_gateway(tmp_path, controller)
    waiting = gateway.submit_legacy_automation(
        "pause",
        "writing",
        operation_id="approval-replay-safe",
    )
    gateway.approve(
        waiting["task_id"],
        note="approved once",
        expected_intent_sha256=waiting["approval"]["intent_sha256"],
    )

    duplicate = gateway.submit_legacy_automation(
        "pause",
        "writing",
        operation_id="approval-replay-safe",
    )

    assert duplicate["task_id"] == waiting["task_id"]
    assert duplicate["status"] == "queued"
    assert gateway.approval(waiting["task_id"])["status"] == "approved"
    assert controller.set_calls == []


def test_gateway_rejects_forged_automation_control_capabilities(tmp_path: Path) -> None:
    controller = FakeController()
    gateway = make_gateway(tmp_path, controller)
    forged = TaskEnvelope(
        paper_id="system",
        stage="system",
        action="legacy.automation.pause",
        executor="legacy_codex",
        idempotency_key="legacy-automation:pause:submission:forged-capability",
        payload={
            "target_stage": "submission",
            "operation_id": "forged-capability",
        },
        network_access=True,
    )

    with pytest.raises(GatewayError) as exc_info:
        gateway.submit(forged)

    assert exc_info.value.code == "GATEWAY_LEGACY_CAPABILITY_DENIED"
    assert controller.set_calls == []


def test_generic_submit_cannot_bypass_automation_operation_id_binding(tmp_path: Path) -> None:
    controller = FakeController()
    gateway = make_gateway(tmp_path, controller)
    forged = TaskEnvelope(
        paper_id="system",
        stage="system",
        action="legacy.automation.start",
        executor="legacy_codex",
        idempotency_key="random-unbound-key",
        payload={"target_stage": "figures", "operation_id": "operation-1"},
        approval_policy=ApprovalPolicy.REQUIRED,
    )

    with pytest.raises(GatewayError) as caught:
        gateway.submit(forged)

    assert caught.value.code == "GATEWAY_LEGACY_IDEMPOTENCY_INVALID"
    assert controller.set_calls == []


def test_running_automation_control_reconciles_by_status_without_second_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = FakeController()
    gateway = make_gateway(tmp_path, controller)
    waiting = gateway.submit_legacy_automation(
        "pause",
        "writing",
        operation_id="reconcile-pause",
    )
    intent = waiting["approval"]["intent_sha256"]
    gateway.approve(waiting["task_id"], expected_intent_sha256=intent)
    original_complete = gateway.store.complete_task

    def crash_after_control(*_args, **_kwargs):
        raise OSError("simulated ledger completion crash")

    monkeypatch.setattr(gateway.store, "complete_task", crash_after_control)
    with pytest.raises(OSError, match="simulated ledger"):
        gateway.run_once(waiting["task_id"])
    monkeypatch.setattr(gateway.store, "complete_task", original_complete)
    assert controller.set_calls == [("writing", False)]
    assert gateway.task(waiting["task_id"])["status"] == "running"

    recovered = gateway.recover_legacy_automation_task(
        waiting["task_id"],
        expected_intent_sha256=intent,
    )

    assert recovered["status"] == "completed"
    assert recovered["result"]["data"]["reconciled_from_status"] is True
    assert controller.set_calls == [("writing", False)]
