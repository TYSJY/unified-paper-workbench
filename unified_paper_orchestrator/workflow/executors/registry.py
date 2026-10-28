from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from ..contracts import NodeType
from .base import ExecutionContext, ExecutionResult, Executor, ExecutorDescriptor, blocked_result


class ExecutorRegistry:
    """Explicit node-type routing table configured by the scheduler composition root."""

    def __init__(self, executors: Iterable[Executor] = ()) -> None:
        items: dict[NodeType, Executor] = {}
        for executor in executors:
            self._register(items, executor)
        self._items = MappingProxyType(items)

    @staticmethod
    def _register(items: dict[NodeType, Executor], executor: Executor) -> None:
        if not isinstance(executor, Executor):
            raise TypeError("registered executor does not implement the Executor protocol")
        descriptor = executor.descriptor()
        if not isinstance(descriptor, ExecutorDescriptor):
            raise TypeError("executor descriptor must be an ExecutorDescriptor")
        if descriptor.node_type in items:
            raise ValueError(f"duplicate executor for node type: {descriptor.node_type.value}")
        items[descriptor.node_type] = executor

    def get(self, node_type: NodeType | str) -> Executor:
        resolved = NodeType(node_type)
        try:
            return self._items[resolved]
        except KeyError as exc:
            raise KeyError(f"no executor is registered for node type: {resolved.value}") from exc

    def describe(self) -> tuple[ExecutorDescriptor, ...]:
        return tuple(
            self._items[node_type].descriptor()
            for node_type in sorted(self._items, key=lambda item: item.value)
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        try:
            executor = self.get(context.node.node_type)
        except KeyError:
            return blocked_result(
                "EXECUTOR_NOT_REGISTERED",
                f"No executor is registered for node type {context.node.node_type.value!r}.",
            )
        descriptor = executor.descriptor()
        if not descriptor.available:
            return blocked_result(
                "EXECUTOR_NOT_AVAILABLE",
                f"Executor {descriptor.executor_id!r} is not configured for execution.",
            )
        return executor.execute(context)
