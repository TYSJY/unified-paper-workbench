from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .contracts import (
    EdgeType,
    NodeDefinition,
    NodeRunStatus,
    ValidationMode,
    WorkflowDefinition,
    WorkflowRunStatus,
    canonical_json,
)
from .store import WorkflowConflictError, WorkflowStore


class WorkflowInvariantError(RuntimeError):
    """Raised when durable workflow state contradicts its frozen contract."""


class KernelAction(str, Enum):
    NOOP = "noop"
    NODE_READY = "node-ready"
    RUN_COMPLETED = "run-completed"


@dataclass(frozen=True)
class KernelDecision:
    run_id: str
    action: KernelAction
    node_run_id: str = ""
    node_id: str = ""
    reason: str = ""


_SUCCESSFUL_NODE_STATUSES = {
    NodeRunStatus.COMPLETED.value,
    NodeRunStatus.SKIPPED.value,
}
_IMMUTABLE_ATTEMPT_STATUSES = {
    NodeRunStatus.COMPLETED.value,
    NodeRunStatus.RETRYABLE_FAILURE.value,
    NodeRunStatus.FATAL_FAILURE.value,
    NodeRunStatus.BLOCKED.value,
    NodeRunStatus.CANCELLED.value,
    NodeRunStatus.SKIPPED.value,
}
_ACTIVE_NODE_STATUSES = {
    NodeRunStatus.READY.value,
    NodeRunStatus.RUNNING.value,
    NodeRunStatus.WAITING_APPROVAL.value,
    NodeRunStatus.WAITING_HUMAN.value,
    NodeRunStatus.RETRY_WAIT.value,
}


class LinearWorkflowKernel:
    """Pure state-transition kernel for one frozen linear workflow run.

    The kernel follows success edges and only asks the store to perform CAS
    transitions. It never dispatches executors and never infers progression
    from display order.
    """

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store

    @staticmethod
    def linear_chain(definition: WorkflowDefinition) -> tuple[str, ...]:
        definition.validate(mode=ValidationMode.LINEAR)
        node_ids = {node.node_id for node in definition.nodes}
        if len(node_ids) == 1:
            return (next(iter(node_ids)),)
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for edge in definition.edges:
            if edge.edge_type is not EdgeType.SUCCESS:
                raise WorkflowInvariantError("linear execution only supports success edges")
            outgoing[edge.source_node_id].append(edge.target_node_id)
            incoming[edge.target_node_id].append(edge.source_node_id)
        entries = [node_id for node_id in node_ids if not incoming[node_id]]
        if len(entries) != 1:
            raise WorkflowInvariantError("linear workflow does not have exactly one entry")
        chain: list[str] = []
        current = entries[0]
        while True:
            if current in chain:
                raise WorkflowInvariantError("linear workflow contains a cycle")
            chain.append(current)
            targets = outgoing[current]
            if not targets:
                break
            if len(targets) != 1:
                raise WorkflowInvariantError("linear workflow contains a branch")
            current = targets[0]
        if set(chain) != node_ids:
            raise WorkflowInvariantError("linear workflow chain does not cover every node")
        return tuple(chain)

    def _frozen_state(
        self,
        run_id: str,
    ) -> tuple[dict[str, Any], WorkflowDefinition, tuple[str, ...], dict[str, dict[str, Any]]]:
        run = self.store.get_run(run_id)
        try:
            version_record = self.store.get_version(str(run["workflow_version_id"]))
            binding = self.store.get_runtime_binding(str(run["binding_sha256"]))
        except (KeyError, ValueError) as exc:
            raise WorkflowInvariantError("run references a missing or invalid frozen snapshot") from exc
        version = version_record["version"]
        definition = version.definition
        definition.validate(mode=ValidationMode.LINEAR)
        if (
            version.definition_sha256 != run["definition_sha256"]
            or definition.sha256() != run["definition_sha256"]
            or binding.sha256() != run["binding_sha256"]
            or binding.definition_sha256 != run["definition_sha256"]
        ):
            raise WorkflowInvariantError("run hashes do not match its frozen workflow snapshot")
        binding.validate(definition)
        chain = self.linear_chain(definition)
        definitions = {node.node_id: node for node in definition.nodes}
        histories: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in chain}
        for record in self.store.list_node_runs(run_id):
            node_id = str(record["node_id"])
            if node_id not in histories:
                raise WorkflowInvariantError("run contains a node outside its frozen definition")
            histories[node_id].append(record)
        latest: dict[str, dict[str, Any]] = {}
        for node_id in chain:
            attempts = sorted(histories[node_id], key=lambda item: int(item["attempt"]))
            if not attempts:
                raise WorkflowInvariantError("run is missing a frozen node attempt")
            if [int(item["attempt"]) for item in attempts] != list(range(1, len(attempts) + 1)):
                raise WorkflowInvariantError("node attempts are not contiguous")
            expected_snapshot = canonical_json(definitions[node_id].to_dict())
            for prior in attempts:
                node = prior["node"]
                if not isinstance(node, NodeDefinition) or canonical_json(node.to_dict()) != expected_snapshot:
                    raise WorkflowInvariantError("node attempt snapshot differs from the frozen definition")
            if any(str(item["status"]) not in _IMMUTABLE_ATTEMPT_STATUSES for item in attempts[:-1]):
                raise WorkflowInvariantError("an older node attempt is still mutable")
            latest[node_id] = attempts[-1]
        active = [item for item in latest.values() if str(item["status"]) in _ACTIVE_NODE_STATUSES]
        if len(active) > 1:
            raise WorkflowInvariantError("run has more than one active latest node")
        return run, definition, chain, latest

    def inspect(self, run_id: str) -> KernelDecision:
        run, _definition, chain, latest = self._frozen_state(run_id)
        run_status = str(run["status"])
        statuses = [str(latest[node_id]["status"]) for node_id in chain]
        all_successful = all(status in _SUCCESSFUL_NODE_STATUSES for status in statuses)

        if run_status == WorkflowRunStatus.COMPLETED.value:
            if not all_successful:
                raise WorkflowInvariantError("completed run still has an unfinished latest node")
            return KernelDecision(run_id, KernelAction.NOOP, reason="run-already-completed")
        if run_status in {
            WorkflowRunStatus.CANCELLED.value,
            WorkflowRunStatus.FATAL_FAILURE.value,
        }:
            return KernelDecision(run_id, KernelAction.NOOP, reason=f"run-{run_status}")
        if all_successful:
            return KernelDecision(run_id, KernelAction.RUN_COMPLETED, reason="all-nodes-completed")

        first_unfinished = next(
            index for index, status in enumerate(statuses) if status not in _SUCCESSFUL_NODE_STATUSES
        )
        if any(status != NodeRunStatus.PENDING.value for status in statuses[first_unfinished + 1 :]):
            raise WorkflowInvariantError("a downstream node advanced before its predecessor")
        node_id = chain[first_unfinished]
        node_run = latest[node_id]
        node_status = statuses[first_unfinished]

        if run_status == WorkflowRunStatus.PAUSED.value:
            return KernelDecision(
                run_id,
                KernelAction.NOOP,
                str(node_run["node_run_id"]),
                node_id,
                "run-paused",
            )
        if node_status == NodeRunStatus.PENDING.value:
            if run_status not in {
                WorkflowRunStatus.QUEUED.value,
                WorkflowRunStatus.RUNNING.value,
            }:
                raise WorkflowInvariantError("run status cannot make its next pending node ready")
            return KernelDecision(
                run_id,
                KernelAction.NODE_READY,
                str(node_run["node_run_id"]),
                node_id,
                "predecessors-completed",
            )
        expected_run_status = {
            NodeRunStatus.WAITING_APPROVAL.value: WorkflowRunStatus.WAITING_APPROVAL.value,
            NodeRunStatus.WAITING_HUMAN.value: WorkflowRunStatus.WAITING_HUMAN.value,
            NodeRunStatus.BLOCKED.value: WorkflowRunStatus.BLOCKED.value,
            NodeRunStatus.RETRYABLE_FAILURE.value: WorkflowRunStatus.RETRYABLE_FAILURE.value,
            NodeRunStatus.RETRY_WAIT.value: WorkflowRunStatus.RETRYABLE_FAILURE.value,
            NodeRunStatus.FATAL_FAILURE.value: WorkflowRunStatus.FATAL_FAILURE.value,
            NodeRunStatus.CANCELLED.value: WorkflowRunStatus.CANCELLED.value,
        }.get(node_status)
        if expected_run_status and run_status not in {
            expected_run_status,
            WorkflowRunStatus.PAUSED.value,
        }:
            raise WorkflowInvariantError("run status does not match its active node status")
        if node_status in {NodeRunStatus.READY.value, NodeRunStatus.RUNNING.value} and run_status not in {
            WorkflowRunStatus.QUEUED.value,
            WorkflowRunStatus.RUNNING.value,
        }:
            raise WorkflowInvariantError("runnable node belongs to a non-runnable run")
        return KernelDecision(
            run_id,
            KernelAction.NOOP,
            str(node_run["node_run_id"]),
            node_id,
            f"node-{node_status}",
        )

    def advance(self, run_id: str, *, now: datetime | None = None) -> KernelDecision:
        decision = self.inspect(run_id)
        if decision.action is KernelAction.NODE_READY:
            try:
                self.store.mark_node_ready(decision.node_run_id, now=now)
            except WorkflowConflictError:
                return self.inspect(run_id)
            return decision
        if decision.action is KernelAction.RUN_COMPLETED:
            try:
                self.store.complete_run(run_id, now=now)
            except WorkflowConflictError:
                return self.inspect(run_id)
            return decision
        return decision


__all__ = [
    "KernelAction",
    "KernelDecision",
    "LinearWorkflowKernel",
    "WorkflowInvariantError",
]
