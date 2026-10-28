from __future__ import annotations

from typing import Any, Callable

from ..contracts import ResultStatus, TaskEnvelope, TaskResult
from .base import ExecutorDescriptor


class OpenAIAPIExecutor:
    """Retired generic port retained only for persisted-envelope compatibility.

    Sending an arbitrary payload.prompt directly to a model bypasses the
    action-specific input hashing, capability policy, validator, and staging
    writer. Every live OpenAI action must therefore use a dedicated executor
    behind ExecutionGateway; the first such route is ``openai_review``.
    """

    name = "openai_api"

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        # Keep the historical constructor shape so tests or persisted factory
        # wiring fail closed instead of failing import. The client is never used.
        self.client_factory = client_factory

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            name=self.name,
            available=False,
            kind="responses_api_generic_disabled",
            detail="Generic prompt execution is disabled; use an action-specific ExecutionGateway adapter.",
            side_effecting=True,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        return TaskResult(
            status=ResultStatus.BLOCKED,
            blocker_code="OPENAI_GENERIC_ROUTE_DISABLED",
            message="Generic OpenAI execution is disabled; this action requires an approved gateway adapter.",
            data={"requested_action": task.action, "canonical_write": False},
        )
