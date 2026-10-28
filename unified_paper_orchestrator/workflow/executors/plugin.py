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


class PluginExecutor:
    """Dispatches pre-registered plugin callbacks without dynamic code loading."""

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
            executor_id="plugin",
            node_type=NodeType.PLUGIN,
            available=True,
            implementation_refs=tuple(self._implementations),
            detail="Explicitly allowlisted plugin callbacks; no eval or dynamic imports.",
            side_effecting=True,
            max_concurrency=self._max_concurrency,
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        if context.node.node_type is not NodeType.PLUGIN:
            return blocked_result(
                "EXECUTOR_NODE_TYPE_MISMATCH",
                "The plugin executor accepts only plugin nodes.",
            )
        return run_allowlisted_handler(
            context=context,
            handlers=self._implementations,
            missing_code="PLUGIN_IMPLEMENTATION_NOT_ALLOWLISTED",
            failure_code="PLUGIN_IMPLEMENTATION_FAILED",
            failure_outcome=ExecutionOutcome.BLOCKED,
        )
