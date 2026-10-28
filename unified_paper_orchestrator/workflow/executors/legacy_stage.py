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


class LegacyStageExecutor:
    """Thin callback port; this module never imports or constructs a production runtime."""

    def __init__(
        self,
        callbacks: Mapping[str, Callable[[ExecutionContext], ExecutionResult]] | None = None,
        *,
        max_concurrency: int = 1,
    ) -> None:
        self._callbacks = validate_handler_mapping(callbacks)
        self._max_concurrency = max_concurrency

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            executor_id="legacy-stage",
            node_type=NodeType.LEGACY_STAGE,
            available=bool(self._callbacks),
            implementation_refs=tuple(self._callbacks),
            detail="Injected compatibility callbacks; no production runtime import or fallback.",
            side_effecting=True,
            max_concurrency=self._max_concurrency,
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        if context.node.node_type is not NodeType.LEGACY_STAGE:
            return blocked_result(
                "EXECUTOR_NODE_TYPE_MISMATCH",
                "The legacy-stage executor accepts only legacy-stage nodes.",
            )
        return run_allowlisted_handler(
            context=context,
            handlers=self._callbacks,
            missing_code="LEGACY_STAGE_CALLBACK_NOT_CONFIGURED",
            failure_code="LEGACY_STAGE_CALLBACK_FAILED",
            failure_outcome=ExecutionOutcome.BLOCKED,
        )
