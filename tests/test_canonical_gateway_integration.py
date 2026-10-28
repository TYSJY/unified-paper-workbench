from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unified_paper_orchestrator_v2.canonical import (
    CanonicalCommitAuthorization,
    CanonicalCommitter,
)
from unified_paper_orchestrator_v2.contracts import (
    ApprovalPolicy,
    ApprovalRequest,
    ArtifactRef,
    ResultStatus,
    TaskEnvelope,
    TaskResult,
    canonical_json,
)
from unified_paper_orchestrator_v2.executors import CanonicalCommitExecutor
from unified_paper_orchestrator_v2.executors.base import ExecutorDescriptor
from unified_paper_orchestrator_v2.executors.deterministic import DeterministicExecutor
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway, GatewayError
from unified_paper_orchestrator_v2.kernel import WorkflowKernel
from unified_paper_orchestrator_v2.staging import StagingStore


def _gateway(
    tmp_path: Path,
    *,
    allow_route: bool = True,
) -> tuple[ExecutionGateway, CanonicalCommitter, Path, Path]:
    staging = tmp_path / "staging"
    canonical = tmp_path / "canonical"
    staging.mkdir(exist_ok=True)
    canonical.mkdir(exist_ok=True)
    committer = CanonicalCommitter(
        canonical_roots={"papers": canonical},
        staging_roots=(staging,),
        control_root=tmp_path / "canonical-control",
        authorization=CanonicalCommitAuthorization(
            actor="gateway-integration-test",
            reason="isolated non-production canonical route verification",
        ),
        acl_hardener=None,
    )
    registry = ExecutorRegistry()
    registry.register(DeterministicExecutor())
    registry.register(CanonicalCommitExecutor(committer))
    staging_store = StagingStore(staging, acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging,
        registry=registry,
        staging_store=staging_store,
        allow_canonical_route=allow_route,
    )
    return gateway, committer, staging, canonical


def _submit(
    gateway: ExecutionGateway,
    source: Path,
    *,
    commit_id: str = "isolated-paper-commit-1",
    relative_path: str = "paper.md",
) -> dict:
    return gateway.submit_canonical_commit(
        paper_id="paper-001",
        commit_id=commit_id,
        staged=ArtifactRef.from_file(source, media_type="text/markdown"),
        root_name="papers",
        relative_path=relative_path,
        expected_exists=False,
    )


def _approve(gateway: ExecutionGateway, task_id: str) -> str:
    intent = gateway.approval(task_id)["payload"]["intent_sha256"]
    gateway.approve(task_id, expected_intent_sha256=intent, note="isolated test approval")
    return intent


def test_default_gateway_installs_but_does_not_authorize_canonical_writes(tmp_path: Path) -> None:
    gateway = ExecutionGateway(
        database_path=tmp_path / "default.sqlite3",
        staging_root=tmp_path / "staging",
    )

    status = gateway.status()

    assert status["canonical_gate_installed"] is True
    assert status["canonical_gate_authorized"] is False
    assert status["canonical_route_enabled"] is False
    assert status["canonical_writes_enabled"] is False
    assert status["canonical_production_writes_enabled"] is False
    assert status["canonical_write_owner"] == "v1_existing_workflows"
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "paper.md"
    source.write_bytes(b"must stay staged")
    with pytest.raises(GatewayError) as caught:
        gateway.submit_canonical_commit(
            paper_id="paper",
            commit_id="default-route-denied",
            staged=ArtifactRef.from_file(source),
            root_name="papers",
            relative_path="paper.md",
            expected_exists=False,
        )
    assert caught.value.code == "GATEWAY_CANONICAL_ROUTE_DISABLED"


def test_isolated_canonical_route_requires_visible_intent_approval_and_commits_once(
    tmp_path: Path,
) -> None:
    gateway, _committer, staging, canonical = _gateway(tmp_path)
    source = staging / "paper.md"
    source.write_bytes(b"approved canonical payload")
    waiting = _submit(gateway, source)

    assert waiting["status"] == "waiting_approval"
    assert waiting["dispatch_owner"] == "gateway"
    assert not (canonical / "paper.md").exists()
    intent = gateway.approval(waiting["task_id"])["payload"]["intent_sha256"]
    assert len(intent) == 64
    with pytest.raises(GatewayError) as missing:
        gateway.approve(waiting["task_id"])
    assert missing.value.code == "GATEWAY_APPROVAL_INTENT_REQUIRED"
    with pytest.raises(GatewayError) as mismatch:
        gateway.approve(waiting["task_id"], expected_intent_sha256="0" * 64)
    assert mismatch.value.code == "GATEWAY_APPROVAL_INTENT_MISMATCH"

    gateway.approve(waiting["task_id"], expected_intent_sha256=intent)
    kernel = WorkflowKernel(gateway.store, gateway.registry)
    assert kernel.dispatch_once(waiting["task_id"]) is None
    with pytest.raises(ValueError, match="dispatch_owner mismatch"):
        kernel.approve(waiting["task_id"], note="must not bypass gateway")
    assert gateway.task(waiting["task_id"])["status"] == "queued"

    completed = gateway.run_once(waiting["task_id"])
    duplicate = _submit(gateway, source)

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["data"]["canonical_write"] is True
    assert completed["result"]["data"]["canonical_write_state"] == "committed"
    assert completed["result"]["data"]["fallback_allowed"] is False
    assert (canonical / "paper.md").read_bytes() == b"approved canonical payload"
    assert duplicate["task_id"] == waiting["task_id"]
    assert len((tmp_path / "canonical-control" / "canonical_audit.jsonl").read_text().splitlines()) == 1
    status = gateway.status()
    assert status["canonical_route_enabled"] is True
    assert status["canonical_production_writes_enabled"] is False
    assert status["canonical_write_owner"] == "v1_existing_workflows"


def test_dispatch_owners_prevent_kernel_and_gateway_from_stealing_each_others_tasks(
    tmp_path: Path,
) -> None:
    gateway, _committer, staging, canonical = _gateway(tmp_path)
    source = staging / "paper.md"
    source.write_bytes(b"gateway-owned")
    canonical_task = _submit(gateway, source)
    _approve(gateway, canonical_task["task_id"])
    kernel = WorkflowKernel(gateway.store, gateway.registry)
    noop = kernel.create_task(
        TaskEnvelope(
            paper_id="system",
            stage="system",
            action="system.noop",
            executor="deterministic",
        )
    )

    assert kernel.dispatch_once(canonical_task["task_id"]) is None
    with pytest.raises(GatewayError) as wrong_owner:
        gateway.run_once(noop["task_id"])
    assert wrong_owner.value.code == "GATEWAY_DISPATCH_OWNER_MISMATCH"
    assert gateway.task(noop["task_id"])["status"] == "queued"
    assert not (canonical / "paper.md").exists()
    assert kernel.dispatch_once(noop["task_id"])["status"] == "completed"
    assert gateway.run_once(canonical_task["task_id"])["status"] == "completed"


def test_same_commit_id_with_different_content_returns_stable_conflict(tmp_path: Path) -> None:
    gateway, _committer, staging, _canonical = _gateway(tmp_path)
    first = staging / "first.md"
    second = staging / "second.md"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _submit(gateway, first, commit_id="one-operation")

    with pytest.raises(GatewayError) as caught:
        _submit(gateway, second, commit_id="one-operation")

    assert caught.value.code == "GATEWAY_IDEMPOTENCY_CONFLICT"


def test_generic_submit_cannot_change_canonical_commit_idempotency_key(
    tmp_path: Path,
) -> None:
    gateway, _committer, staging, canonical = _gateway(tmp_path)
    source = staging / "paper.md"
    source.write_bytes(b"canonical binding")
    task = TaskEnvelope(
        paper_id="paper-001",
        stage="system",
        action="artifact.canonical_commit",
        executor="canonical_commit",
        idempotency_key="random-unbound-key",
        inputs=[ArtifactRef.from_file(source)],
        payload={
            "commit_id": "bound-commit-id",
            "root_name": "papers",
            "relative_path": "paper.md",
            "expected_exists": False,
            "expected_sha256": "",
        },
        writable_roots=[str(canonical)],
        approval_policy=ApprovalPolicy.REQUIRED,
    )

    with pytest.raises(GatewayError) as caught:
        gateway.submit(task)

    assert caught.value.code == "GATEWAY_CANONICAL_IDEMPOTENCY_INVALID"


def test_delayed_duplicate_approval_request_cannot_rewind_a_running_task(
    tmp_path: Path,
) -> None:
    gateway, _committer, staging, _canonical = _gateway(tmp_path)
    source = staging / "paper.md"
    source.write_bytes(b"approval race payload")
    waiting = _submit(gateway, source)
    _approve(gateway, waiting["task_id"])
    claimed = gateway.store.claim_task(waiting["task_id"], dispatch_owner="gateway")
    assert claimed is not None
    approval = gateway.approval(waiting["task_id"])
    payload = approval["payload"]

    replayed = gateway.store.request_approval(
        waiting["task_id"],
        ApprovalRequest(
            task_id=waiting["task_id"],
            reason=payload["reason"],
            artifact_hashes=list(payload["artifact_hashes"]),
            intent_sha256=payload["intent_sha256"],
            route_revision=payload["route_revision"],
        ),
    )

    assert replayed["status"] == "approved"
    assert gateway.task(waiting["task_id"])["status"] == "running"
    events = gateway.store.list_events(limit=100)
    assert [item["event_type"] for item in events].count("approval.requested") == 1


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda task, outside: task.payload.update({"extra": "denied"}), "GATEWAY_CANONICAL_CONTRACT_INVALID"),
        (lambda task, outside: setattr(task, "approval_policy", ApprovalPolicy.NONE), "GATEWAY_CANONICAL_APPROVAL_REQUIRED"),
        (lambda task, outside: task.inputs.append(task.inputs[0]), "GATEWAY_CANONICAL_INPUT_INVALID"),
        (lambda task, outside: setattr(task, "writable_roots", [str(outside)]), "GATEWAY_CANONICAL_WRITABLE_ROOT_DENIED"),
        (lambda task, outside: setattr(task, "inputs", [ArtifactRef.from_file(outside)]), "GATEWAY_CANONICAL_STAGING_DENIED"),
    ],
)
def test_forged_canonical_envelopes_fail_closed_before_approval(
    tmp_path: Path,
    mutation,
    expected_code: str,
) -> None:
    gateway, _committer, staging, canonical = _gateway(tmp_path)
    source = staging / "paper.md"
    source.write_bytes(b"payload")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")
    task = TaskEnvelope(
        paper_id="paper-001",
        stage="system",
        action="artifact.canonical_commit",
        executor="canonical_commit",
        idempotency_key="canonical-commit:forged-contract",
        inputs=[ArtifactRef.from_file(source)],
        payload={
            "commit_id": "forged-contract",
            "root_name": "papers",
            "relative_path": "paper.md",
            "expected_exists": False,
            "expected_sha256": "",
        },
        writable_roots=[str(canonical)],
        approval_policy=ApprovalPolicy.REQUIRED,
    )
    mutation(task, outside)

    with pytest.raises(GatewayError) as caught:
        gateway.submit(task)

    assert caught.value.code == expected_code
    assert not (canonical / "paper.md").exists()


def test_approval_intent_drift_is_detected_before_canonical_write(tmp_path: Path) -> None:
    gateway, _committer, staging, canonical = _gateway(tmp_path)
    (canonical / "nested").mkdir()
    source = staging / "paper.md"
    source.write_bytes(b"approved source")
    waiting = _submit(gateway, source)
    _approve(gateway, waiting["task_id"])
    with sqlite3.connect(gateway.store.path) as connection:
        raw = connection.execute(
            "SELECT envelope_json FROM tasks WHERE task_id=?", (waiting["task_id"],)
        ).fetchone()[0]
        envelope = json.loads(raw)
        envelope["payload"]["relative_path"] = "nested/drifted.md"
        connection.execute(
            "UPDATE tasks SET envelope_json=? WHERE task_id=?",
            (canonical_json(envelope), waiting["task_id"]),
        )

    failed = gateway.run_once(waiting["task_id"])

    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "GATEWAY_APPROVAL_INTENT_DRIFT"
    assert not (canonical / "paper.md").exists()
    assert not (canonical / "nested" / "drifted.md").exists()


def test_audited_commit_reconciles_after_task_ledger_completion_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, committer, staging, canonical = _gateway(tmp_path)
    source = staging / "paper.md"
    source.write_bytes(b"durable before ledger completion")
    waiting = _submit(gateway, source)
    intent = _approve(gateway, waiting["task_id"])

    def crash_after_commit(_task_id, _result, **_kwargs):
        raise OSError("simulated SQLite completion crash")

    monkeypatch.setattr(gateway.store, "complete_task", crash_after_commit)
    with pytest.raises(OSError, match="simulated SQLite"):
        gateway.run_once(waiting["task_id"])
    assert (canonical / "paper.md").read_bytes() == b"durable before ledger completion"
    assert gateway.task(waiting["task_id"])["status"] == "running"
    source.unlink()

    registry = ExecutorRegistry()
    registry.register(DeterministicExecutor())
    registry.register(CanonicalCommitExecutor(committer))
    recovered_gateway = ExecutionGateway(
        database_path=gateway.store.path,
        staging_root=staging,
        registry=registry,
        staging_store=StagingStore(staging, acl_hardener=None),
        allow_canonical_route=True,
    )
    recovered = recovered_gateway.recover_canonical_task(
        waiting["task_id"], expected_intent_sha256=intent
    )

    assert recovered["status"] == "completed"
    assert recovered["result"]["data"]["idempotent"] is True
    assert recovered["result"]["data"]["write_performed"] is False
    assert len((tmp_path / "canonical-control" / "canonical_audit.jsonl").read_text().splitlines()) == 1


class FakeCanonicalExecutor:
    name = "canonical_commit"

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            name=self.name,
            available=True,
            kind="fake",
            side_effecting=True,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        return TaskResult(status=ResultStatus.COMPLETED, message=task.task_id)


def test_same_named_fake_executor_cannot_back_canonical_route(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "paper.md"
    source.write_bytes(b"payload")
    registry = ExecutorRegistry()
    registry.register(FakeCanonicalExecutor())
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging,
        registry=registry,
        staging_store=StagingStore(staging, acl_hardener=None),
        allow_canonical_route=True,
    )

    with pytest.raises(GatewayError) as caught:
        gateway.submit_canonical_commit(
            paper_id="paper",
            commit_id="fake-route",
            staged=ArtifactRef.from_file(source),
            root_name="papers",
            relative_path="paper.md",
            expected_exists=False,
        )

    assert caught.value.code == "GATEWAY_CANONICAL_EXECUTOR_INVALID"
