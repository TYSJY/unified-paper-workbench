from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .contracts import (
    ApprovalPolicy,
    ApprovalRequest,
    ResultStatus,
    TaskEnvelope,
    TaskResult,
    canonical_json,
)
from .domains import STAGES, stage_definition
from .executors import ExecutorRegistry, build_default_registry
from .store import EventStore


class WorkflowKernel:
    def __init__(self, store: EventStore, registry: ExecutorRegistry | None = None) -> None:
        self.store = store
        self.registry = registry or build_default_registry()
        self.store.initialize()

    def create_task(self, envelope: TaskEnvelope) -> dict[str, Any]:
        definition = stage_definition(envelope.stage)
        definition.validate_action(envelope.action)
        self.registry.get(envelope.executor)
        envelope.validate()
        needs_approval = (
            envelope.approval_policy == ApprovalPolicy.REQUIRED
            or envelope.action in definition.approval_actions
        )
        approval: ApprovalRequest | None = None
        if needs_approval:
            hashes = [item.sha256 for item in envelope.inputs if item.sha256]
            approval = ApprovalRequest(
                task_id=envelope.task_id,
                reason=(
                    "Approval required before "
                    f"{envelope.stage}:{envelope.action}"
                ),
                artifact_hashes=hashes,
                intent_sha256=hashlib.sha256(
                    canonical_json(envelope.to_dict()).encode("utf-8")
                ).hexdigest(),
                route_revision=envelope.payload.get("route_revision"),
            )
        return self.store.create_task(envelope, approval=approval)

    def dispatch_once(self, task_id: str = "") -> dict[str, Any] | None:
        envelope = self.store.claim_task(task_id, dispatch_owner="kernel")
        if envelope is None:
            return None
        if not self._direct_dispatch_allowed(envelope):
            return self.store.complete_task(
                envelope.task_id,
                TaskResult(
                    status=ResultStatus.BLOCKED,
                    blocker_code="EXECUTION_GATEWAY_REQUIRED",
                    message="Model and side-effecting executors must be dispatched through ExecutionGateway.",
                    data={"executor": envelope.executor, "action": envelope.action, "canonical_write": False},
                ),
            )
        try:
            executor = self.registry.get(envelope.executor)
            result = executor.execute(envelope)
        except Exception as exc:
            result = TaskResult(
                status=ResultStatus.RETRYABLE_FAILURE,
                blocker_code="EXECUTOR_EXCEPTION",
                message=f"Direct executor failed with {type(exc).__name__}.",
                data={"error_type": type(exc).__name__, "canonical_write": False},
            )
        return self.store.complete_task(envelope.task_id, result)

    @staticmethod
    def _direct_dispatch_allowed(envelope: TaskEnvelope) -> bool:
        if envelope.executor == "deterministic":
            return envelope.action in {"system.noop", "artifact.verify"}
        if envelope.executor == "human":
            return True
        return envelope.executor in {"legacy", "legacy_codex"} and envelope.action == "legacy.plan"

    def approve(self, task_id: str, note: str = "") -> dict[str, Any]:
        return self.store.decide_approval(task_id, approve=True, note=note)

    def reject(self, task_id: str, note: str = "") -> dict[str, Any]:
        return self.store.decide_approval(task_id, approve=False, note=note)

    def cancel(self, task_id: str, note: str = "") -> dict[str, Any]:
        return self.store.cancel_task(task_id, note)

    def retry(self, task_id: str, note: str = "") -> dict[str, Any]:
        return self.store.requeue_task(task_id, note)

    def import_legacy(self, path: Path) -> dict[str, Any]:
        return self.store.import_legacy_snapshot(path)

    def status(self) -> dict[str, Any]:
        return {
            "version": "2",
            "runtime": self.store.summary(),
            "stages": {
                name: {
                    "actions": sorted(definition.actions),
                    "default_executor": definition.default_executor,
                    "approval_actions": sorted(definition.approval_actions),
                }
                for name, definition in STAGES.items()
            },
            "executors": self.registry.describe(),
        }
