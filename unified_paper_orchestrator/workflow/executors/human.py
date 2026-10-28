from __future__ import annotations

from ..contracts import NodeType
from .base import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorDescriptor,
    blocked_result,
    control_checkpoint,
)


class HumanExecutor:
    """Creates a wait outcome; the scheduler owns durable human-resume state."""

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            executor_id="human",
            node_type=NodeType.HUMAN,
            available=True,
            detail="Side-effect-free human-in-the-loop wait marker.",
            side_effecting=False,
            max_concurrency=64,
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        if context.node.node_type is not NodeType.HUMAN:
            return blocked_result(
                "EXECUTOR_NODE_TYPE_MISMATCH",
                "The human executor accepts only human nodes.",
            )
        checkpoint = control_checkpoint(context, heartbeat=True)
        if checkpoint is not None:
            return checkpoint
        instruction = context.node.metadata.get("instruction")
        message = instruction if isinstance(instruction, str) and instruction.strip() else "Human action is required."
        return ExecutionResult(
            outcome=ExecutionOutcome.WAITING_HUMAN,
            error_code="HUMAN_ACTION_REQUIRED",
            message=message,
            metadata={
                "required_outputs": [
                    spec.artifact_id
                    for spec in sorted(context.node.outputs, key=lambda item: item.artifact_id)
                    if spec.required
                ]
            },
        )
