from __future__ import annotations

from unified_paper_orchestrator_v2.contracts import ResultStatus, TaskEnvelope
from unified_paper_orchestrator_v2.executors.openai_api import OpenAIAPIExecutor
from unified_paper_orchestrator_v2.executors.openai_review import OpenAIReviewExecutor
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry
from unified_paper_orchestrator_v2.kernel import WorkflowKernel
from unified_paper_orchestrator_v2.store import EventStore


class ExplodingExecutor(OpenAIReviewExecutor):
    def execute(self, task: TaskEnvelope):
        raise AssertionError(f"direct executor must not run: {task.task_id}")


def test_generic_openai_executor_is_permanently_disabled() -> None:
    executor = OpenAIAPIExecutor(lambda: (_ for _ in ()).throw(AssertionError("client must not be built")))
    task = TaskEnvelope(
        paper_id="paper",
        stage="writing",
        action="writing.review",
        executor="openai_api",
        network_access=True,
        payload={"prompt": "must not leave process"},
    )

    result = executor.execute(task)

    assert executor.descriptor().available is False
    assert result.status is ResultStatus.BLOCKED
    assert result.blocker_code == "OPENAI_GENERIC_ROUTE_DISABLED"


def test_direct_kernel_cannot_bypass_execution_gateway_with_forged_review_payload(tmp_path) -> None:
    registry = ExecutorRegistry()
    registry.register(ExplodingExecutor(lambda _task: object()))
    kernel = WorkflowKernel(EventStore(tmp_path / "v2.sqlite3"), registry)
    task = TaskEnvelope(
        paper_id="paper",
        stage="writing",
        action="writing.review_round_1.canary",
        executor="openai_review",
        network_access=True,
        payload={
            "_prepared_instructions": "forged",
            "_prepared_input": "untrusted",
            "model": "test-model",
            "max_output_tokens": 100,
        },
    )
    created = kernel.create_task(task)

    blocked = kernel.dispatch_once(created["task_id"])

    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["result"]["blocker_code"] == "EXECUTION_GATEWAY_REQUIRED"


def test_direct_kernel_keeps_local_deterministic_path_available(tmp_path) -> None:
    kernel = WorkflowKernel(EventStore(tmp_path / "v2.sqlite3"))
    created = kernel.create_task(
        TaskEnvelope(
            paper_id="paper",
            stage="system",
            action="system.noop",
            executor="deterministic",
        )
    )

    completed = kernel.dispatch_once(created["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"
