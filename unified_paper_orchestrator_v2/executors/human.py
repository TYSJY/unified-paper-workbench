from __future__ import annotations

from ..contracts import ResultStatus, TaskEnvelope, TaskResult
from .base import ExecutorDescriptor


class HumanExecutor:
    name = "human"

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            name=self.name,
            available=True,
            kind="human_in_the_loop",
            detail="Creates a durable waiting_human result without performing side effects.",
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        return TaskResult(
            status=ResultStatus.WAITING_HUMAN,
            blocker_code="HUMAN_ACTION_REQUIRED",
            message=str(task.payload.get("instruction") or "Task is waiting for human completion."),
            data={"requested_artifacts": list(task.payload.get("requested_artifacts") or [])},
        )


class BrowserExecutor:
    name = "browser"

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            name=self.name,
            available=False,
            kind="browser_worker",
            detail="Reserved port. No browser side effects are enabled in the V2 foundation release.",
            side_effecting=True,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        return TaskResult(
            status=ResultStatus.BLOCKED,
            blocker_code="BROWSER_EXECUTOR_NOT_CONFIGURED",
            message="V2 browser execution is intentionally disabled; use legacy/manual execution for now.",
        )

