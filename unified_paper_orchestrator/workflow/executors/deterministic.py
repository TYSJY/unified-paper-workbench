from __future__ import annotations

from collections.abc import Callable, Mapping

from ..contracts import NodeType
from .base import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorDescriptor,
    blocked_result,
    run_allowlisted_handler,
    validate_handler_mapping,
)


class DeterministicExecutor:
    """Runs only constructor-injected, explicitly named local functions."""

    def __init__(
        self,
        implementations: Mapping[str, Callable[[ExecutionContext], ExecutionResult]] | None = None,
        *,
        max_concurrency: int = 1,
    ) -> None:
        self._implementations = validate_handler_mapping(implementations)
        self._max_concurrency = max_concurrency

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            executor_id="deterministic",
            node_type=NodeType.DETERMINISTIC,
            available=True,
            implementation_refs=tuple(self._implementations),
            detail="Explicitly allowlisted in-process deterministic functions.",
            side_effecting=False,
            max_concurrency=self._max_concurrency,
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        if context.node.node_type is not NodeType.DETERMINISTIC:
            return blocked_result(
                "EXECUTOR_NODE_TYPE_MISMATCH",
                "The deterministic executor accepts only deterministic nodes.",
            )
        return run_allowlisted_handler(
            context=context,
            handlers=self._implementations,
            missing_code="DETERMINISTIC_IMPLEMENTATION_NOT_ALLOWLISTED",
            failure_code="DETERMINISTIC_IMPLEMENTATION_FAILED",
            failure_outcome=ExecutionOutcome.FATAL_FAILURE,
        )
