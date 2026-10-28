from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow import (
    EdgeDefinition,
    NodeDefinition,
    NodeRunStatus,
    NodeType,
    RetryPolicy,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.kernel import (
    KernelAction,
    LinearWorkflowKernel,
    WorkflowInvariantError,
)
from unified_paper_orchestrator.workflow.store import WorkflowStore


NOW = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)


def _definition() -> WorkflowDefinition:
    first = NodeDefinition(
        "first",
        "First",
        NodeType.DETERMINISTIC,
        30,
        implementation_ref="tests.first",
        retry_policy=RetryPolicy(max_attempts=2),
    )
    second = NodeDefinition(
        "second",
        "Second",
        NodeType.DETERMINISTIC,
        10,
        implementation_ref="tests.second",
    )
    third = NodeDefinition(
        "third",
        "Third",
        NodeType.DETERMINISTIC,
        20,
        implementation_ref="tests.third",
    )
    return WorkflowDefinition(
        "kernel.edge-order",
        "Kernel edge order",
        nodes=(third, first, second),
        edges=(
            EdgeDefinition("second-third", "second", "third"),
            EdgeDefinition("first-second", "first", "second"),
        ),
    )


def _run(tmp_path: Path) -> tuple[WorkflowStore, LinearWorkflowKernel, str]:
    store = WorkflowStore(tmp_path / "orchestrator.sqlite3")
    store.initialize()
    definition = _definition()
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=NOW.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(definition.sha256(), ())
    run = store.create_run(version, binding, run_id="run-kernel", now=NOW)
    return store, LinearWorkflowKernel(store), str(run["run_id"])


def _complete_ready(store: WorkflowStore, run_id: str, worker: str, now: datetime) -> None:
    grant = store.claim_ready_node(run_id, worker, now=now)
    assert grant is not None
    store.complete_node(grant, now=now + timedelta(microseconds=1))


def test_kernel_follows_success_edges_not_display_order(tmp_path: Path) -> None:
    store, kernel, run_id = _run(tmp_path)

    first = kernel.advance(run_id, now=NOW)
    assert first.action is KernelAction.NODE_READY
    assert first.node_id == "first"
    _complete_ready(store, run_id, "worker-1", NOW + timedelta(seconds=1))

    second = kernel.advance(run_id, now=NOW + timedelta(seconds=2))
    assert second.node_id == "second"
    _complete_ready(store, run_id, "worker-2", NOW + timedelta(seconds=3))

    third = kernel.advance(run_id, now=NOW + timedelta(seconds=4))
    assert third.node_id == "third"
    _complete_ready(store, run_id, "worker-3", NOW + timedelta(seconds=5))

    completed = kernel.advance(run_id, now=NOW + timedelta(seconds=6))
    assert completed.action is KernelAction.NOOP
    assert completed.reason == "run-already-completed"
    assert store.get_run(run_id)["status"] == "completed"
    assert kernel.advance(run_id).reason == "run-already-completed"


def test_concurrent_advance_emits_exactly_one_ready_event(tmp_path: Path) -> None:
    store, kernel, run_id = _run(tmp_path)

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _index: kernel.advance(run_id, now=NOW), range(32)))

    ready_events = [event for event in store.list_events(run_id) if event["event_type"] == "node.ready"]
    assert len(ready_events) == 1
    assert ready_events[0]["payload"]["node_id"] == "first"
    assert sum(decision.action is KernelAction.NODE_READY for decision in decisions) == 1
    assert {decision.node_id for decision in decisions} == {"first"}


def test_paused_waiting_and_failed_runs_do_not_advance(tmp_path: Path) -> None:
    store, kernel, run_id = _run(tmp_path)
    kernel.advance(run_id, now=NOW)
    store.pause_run(run_id, now=NOW + timedelta(seconds=1))
    assert kernel.advance(run_id).reason == "run-paused"
    store.resume_run(run_id, now=NOW + timedelta(seconds=2))
    grant = store.claim_ready_node(run_id, "worker", now=NOW + timedelta(seconds=3))
    assert grant is not None
    store.wait_node(
        grant,
        status=NodeRunStatus.WAITING_HUMAN,
        reason="operator required",
        now=NOW + timedelta(seconds=4),
    )
    assert kernel.advance(run_id).reason == "node-waiting-human"
    assert all(node["status"] == "pending" for node in store.latest_node_runs(run_id) if node["node_id"] != "first")

    store.resume_waiting_node(grant.node_run_id, now=NOW + timedelta(seconds=5))
    replacement = store.claim_node(grant.node_run_id, "worker-2", now=NOW + timedelta(seconds=6))
    assert replacement is not None
    store.fail_node(
        replacement,
        status=NodeRunStatus.BLOCKED,
        error_code="operator-blocked",
        now=NOW + timedelta(seconds=7),
    )
    assert kernel.advance(run_id).reason == "node-blocked"


def test_kernel_fails_closed_on_two_active_nodes(tmp_path: Path) -> None:
    store, kernel, run_id = _run(tmp_path)
    kernel.advance(run_id, now=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP INDEX uq_node_runs_active_per_run")
        connection.execute(
            "UPDATE node_runs SET status='ready' WHERE workflow_run_id=? AND node_id='second'",
            (run_id,),
        )

    with pytest.raises(WorkflowInvariantError, match="more than one active"):
        kernel.inspect(run_id)


def test_kernel_fails_closed_on_snapshot_drift(tmp_path: Path) -> None:
    store, kernel, run_id = _run(tmp_path)
    first = next(node for node in store.latest_node_runs(run_id) if node["node_id"] == "first")
    tampered = first["node"].to_dict()
    tampered["name"] = "Tampered"
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER node_runs_identity_immutable")
        connection.execute(
            "UPDATE node_runs SET node_snapshot_json=? WHERE node_run_id=?",
            (json.dumps(tampered, sort_keys=True), first["node_run_id"]),
        )

    with pytest.raises(WorkflowInvariantError, match="snapshot differs"):
        kernel.inspect(run_id)


def test_linear_chain_is_stable_for_single_node() -> None:
    definition = WorkflowDefinition(
        "kernel.single",
        "Single",
        nodes=(
            NodeDefinition(
                "only",
                "Only",
                NodeType.DETERMINISTIC,
                100,
                implementation_ref="tests.only",
            ),
        ),
        edges=(),
    )

    assert LinearWorkflowKernel.linear_chain(definition) == ("only",)
