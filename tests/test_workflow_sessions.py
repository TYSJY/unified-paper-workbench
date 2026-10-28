from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow import (
    EdgeDefinition,
    NodeDefinition,
    NodeRunStatus,
    NodeType,
    RetryPolicy,
    SessionPolicy,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.sessions import (
    ExternalSessionStart,
    SessionError,
    SessionManager,
)
from unified_paper_orchestrator.workflow.store import (
    LeaseLostError,
    WorkflowConflictError,
    WorkflowStore,
)


NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)


def _setup(
    tmp_path: Path,
    policies: tuple[SessionPolicy, ...],
    *,
    retry_policy: RetryPolicy | None = None,
) -> tuple[WorkflowStore, str, tuple[NodeDefinition, ...]]:
    nodes = tuple(
        NodeDefinition(
            f"node-{index}",
            f"Node {index}",
            NodeType.DETERMINISTIC,
            index * 10,
            session_policy=policy,
            retry_policy=retry_policy or RetryPolicy(),
            implementation_ref=f"tests.node-{index}",
        )
        for index, policy in enumerate(policies, start=1)
    )
    edges = tuple(
        EdgeDefinition(
            f"edge-{index}",
            nodes[index].node_id,
            nodes[index + 1].node_id,
        )
        for index in range(len(nodes) - 1)
    )
    definition = WorkflowDefinition("sessions.test", "Sessions", nodes, edges)
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=NOW.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(definition.sha256(), ())
    store = WorkflowStore(tmp_path / "orchestrator.sqlite3")
    store.initialize()
    run = store.create_run(version, binding, run_id="run-sessions", now=NOW)
    return store, str(run["run_id"]), nodes


def _claim_node(store: WorkflowStore, run_id: str, node_id: str, now: datetime):
    node = next(item for item in store.latest_node_runs(run_id) if item["node_id"] == node_id)
    store.mark_node_ready(str(node["node_run_id"]), now=now)
    grant = store.claim_node(str(node["node_run_id"]), "worker", now=now)
    assert grant is not None
    return grant


def test_same_run_group_reuses_session_across_nodes(tmp_path: Path) -> None:
    policy = SessionPolicy("shared", reuse_within_run=True, reuse_on_retry=True)
    store, run_id, nodes = _setup(tmp_path, (policy, policy))
    manager = SessionManager(store)
    first_grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    first = manager.prepare(first_grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert first is not None
    store.complete_node(first_grant, now=NOW + timedelta(seconds=1))

    second_grant = _claim_node(store, run_id, nodes[1].node_id, NOW + timedelta(seconds=2))
    second = manager.prepare(
        second_grant,
        nodes[1],
        "codex",
        requires_session=True,
        now=NOW + timedelta(seconds=2),
    )

    assert second is not None
    assert second.session_record_id == first.session_record_id
    assert second.owner_name == first.owner_name


def test_retry_reuses_live_session_from_previous_attempt(tmp_path: Path) -> None:
    policy = SessionPolicy("retry", reuse_within_run=True, reuse_on_retry=True)
    store, run_id, nodes = _setup(
        tmp_path,
        (policy,),
        retry_policy=RetryPolicy(max_attempts=2),
    )
    manager = SessionManager(store)
    first_grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    first = manager.prepare(first_grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert first is not None
    store.fail_node(
        first_grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code="transient",
        now=NOW + timedelta(seconds=1),
    )
    retry = store.retry_node(first_grant.node_run_id, now=NOW + timedelta(seconds=2))
    retry_grant = store.claim_node(
        str(retry["node_run_id"]),
        "worker-2",
        now=NOW + timedelta(seconds=3),
    )
    assert retry_grant is not None

    second = manager.prepare(
        retry_grant,
        nodes[0],
        "codex",
        requires_session=True,
        now=NOW + timedelta(seconds=3),
    )
    assert second is not None
    assert second.session_record_id == first.session_record_id
    with pytest.raises(LeaseLostError):
        manager.bind_external(first_grant, first, "stale-external", now=NOW + timedelta(seconds=3))
    bound = manager.bind_external(
        retry_grant,
        second,
        "current-external",
        now=NOW + timedelta(seconds=3),
    )
    assert bound.external_session_id == "current-external"


def test_force_new_policy_rotates_after_prior_node_is_terminal(tmp_path: Path) -> None:
    policy = SessionPolicy("fresh", force_new_session=True)
    store, run_id, nodes = _setup(tmp_path, (policy, policy))
    manager = SessionManager(store)
    first_grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    first = manager.prepare(first_grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert first is not None
    store.complete_node(first_grant, now=NOW + timedelta(seconds=1))
    second_grant = _claim_node(store, run_id, nodes[1].node_id, NOW + timedelta(seconds=2))

    second = manager.prepare(
        second_grant,
        nodes[1],
        "codex",
        requires_session=True,
        now=NOW + timedelta(seconds=2),
    )

    assert second is not None
    assert second.generation == first.generation + 1
    assert second.session_record_id != first.session_record_id
    assert [item["state"] for item in store.list_sessions(run_id)] == ["rotated", "active"]


@dataclass
class _Broker:
    matches: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def find_exact(self, owner_name: str) -> tuple[str, ...]:
        return tuple(self.matches)

    def start_named(self, owner_name: str) -> ExternalSessionStart:
        self.started.append(owner_name)
        return ExternalSessionStart("external-new", created=True)

    def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)


def test_external_session_reconciliation_is_idempotent_and_fail_closed(tmp_path: Path) -> None:
    policy = SessionPolicy("external", reuse_within_run=True)
    store, run_id, nodes = _setup(tmp_path, (policy,))
    manager = SessionManager(store)
    grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    handle = manager.prepare(grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert handle is not None
    broker = _Broker()

    bound = manager.reconcile_external(grant, handle, broker, now=NOW + timedelta(seconds=1))
    repeated = manager.reconcile_external(grant, bound, broker, now=NOW + timedelta(seconds=2))

    assert bound.external_session_id == repeated.external_session_id == "external-new"
    assert broker.started == [handle.owner_name]
    events = store.list_events(run_id)
    assert "external-new" not in str(events)


def test_external_reconciliation_rejects_ambiguous_owner_name(tmp_path: Path) -> None:
    policy = SessionPolicy("external", reuse_within_run=True)
    store, run_id, nodes = _setup(tmp_path, (policy,))
    manager = SessionManager(store)
    grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    handle = manager.prepare(grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert handle is not None

    with pytest.raises(SessionError, match="multiple external sessions"):
        manager.reconcile_external(grant, handle, _Broker(matches=["one", "two"]), now=NOW)


def test_external_reconciliation_never_starts_after_cancellation(tmp_path: Path) -> None:
    policy = SessionPolicy("external", reuse_within_run=True)
    store, run_id, nodes = _setup(tmp_path, (policy,))
    manager = SessionManager(store)
    grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    handle = manager.prepare(grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert handle is not None
    broker = _Broker()
    store.request_cancel(run_id, now=NOW + timedelta(seconds=1))

    with pytest.raises(WorkflowConflictError):
        manager.reconcile_external(grant, handle, broker, now=NOW + timedelta(seconds=1))
    assert broker.started == []


def test_external_reconciliation_rechecks_after_lookup_and_compensates_start(
    tmp_path: Path,
) -> None:
    policy = SessionPolicy("external", reuse_within_run=True)
    store, run_id, nodes = _setup(tmp_path, (policy,))
    manager = SessionManager(store)
    grant = _claim_node(store, run_id, nodes[0].node_id, NOW)
    handle = manager.prepare(grant, nodes[0], "codex", requires_session=True, now=NOW)
    assert handle is not None

    class CancelDuringLookup(_Broker):
        def find_exact(self, owner_name: str) -> tuple[str, ...]:
            store.request_cancel(run_id, now=NOW + timedelta(seconds=1))
            return ()

    lookup_broker = CancelDuringLookup()
    with pytest.raises(WorkflowConflictError):
        manager.reconcile_external(
            grant,
            handle,
            lookup_broker,
            now=NOW + timedelta(seconds=1),
        )
    assert lookup_broker.started == []

    # Use a fresh run to exercise cancellation after a remote create. The
    # broker contract reports whether it created the session so compensation
    # cannot accidentally stop an existing shared session.
    store2, run_id2, nodes2 = _setup(tmp_path / "second", (policy,))
    manager2 = SessionManager(store2)
    grant2 = _claim_node(store2, run_id2, nodes2[0].node_id, NOW)
    handle2 = manager2.prepare(grant2, nodes2[0], "codex", requires_session=True, now=NOW)
    assert handle2 is not None

    class CancelDuringStart(_Broker):
        def start_named(self, owner_name: str) -> ExternalSessionStart:
            self.started.append(owner_name)
            store2.request_cancel(run_id2, now=NOW + timedelta(seconds=1))
            return ExternalSessionStart("external-created-then-cancelled", created=True)

    start_broker = CancelDuringStart()
    with pytest.raises(SessionError):
        manager2.reconcile_external(
            grant2,
            handle2,
            start_broker,
            now=NOW + timedelta(seconds=1),
        )
    assert start_broker.stopped == ["external-created-then-cancelled"]
