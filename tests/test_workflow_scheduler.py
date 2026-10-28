from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.io_utils import FileLock
from unified_paper_orchestrator.workflow.artifacts import (
    ArtifactContractError,
    ProducedArtifact,
)
from unified_paper_orchestrator.workflow.contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    EdgeDefinition,
    ExecutionProfile,
    NodeDefinition,
    NodeRunStatus,
    NodeType,
    PromptRef,
    ResourceBinding,
    RetryPolicy,
    SessionPolicy,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.executors import (
    CodexResponse,
    CodexSDKExecutor,
    DeterministicExecutor,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRegistry,
    HumanExecutor,
)
from unified_paper_orchestrator.workflow.scheduler import SchedulerConfig, WorkflowScheduler
from unified_paper_orchestrator.workflow.sessions import SessionHandle
from unified_paper_orchestrator.workflow.store import WorkflowStore


BASE_TIME = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> WorkflowStore:
    store = WorkflowStore(tmp_path / "orchestrator.sqlite3")
    store.initialize()
    return store


def _publish_and_create(
    store: WorkflowStore,
    definition: WorkflowDefinition,
    *,
    run_id: str,
    inputs: dict[str, object] | None = None,
) -> None:
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=BASE_TIME.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(definition.sha256(), ())
    store.create_run(
        version,
        binding,
        run_id=run_id,
        inputs=inputs or {},
        idempotency_key=f"create-{run_id}",
        now=BASE_TIME,
    )


def _single_definition(
    *,
    workflow_id: str = "scheduler.single",
    node_type: NodeType = NodeType.DETERMINISTIC,
    implementation_ref: str = "tests.work",
    retry_policy: RetryPolicy | None = None,
    outputs: tuple[ArtifactSpec, ...] = (),
) -> WorkflowDefinition:
    value = WorkflowDefinition(
        workflow_id,
        "Single node",
        nodes=(
            NodeDefinition(
                "work",
                "Work",
                node_type,
                10,
                implementation_ref=implementation_ref,
                retry_policy=retry_policy or RetryPolicy(max_attempts=1),
                outputs=outputs,
            ),
        ),
        edges=(),
    )
    value.validate()
    return value


def _pipeline_definition() -> WorkflowDefinition:
    topic = ArtifactSpec("topic", media_type="application/json", schema_ref="topic-v1")
    draft = ArtifactSpec("draft", media_type="text/plain", schema_ref="document-v1")
    first = NodeDefinition(
        "draft",
        "Draft",
        NodeType.DETERMINISTIC,
        20,
        implementation_ref="tests.draft",
        inputs=(ArtifactSpec("seed", media_type="application/json", schema_ref="topic-v1"),),
        input_bindings=(
            ArtifactBinding("seed", ArtifactSourceKind.WORKFLOW_INPUT, "topic"),
        ),
        outputs=(draft,),
    )
    second = NodeDefinition(
        "finalize",
        "Finalize",
        NodeType.DETERMINISTIC,
        10,
        implementation_ref="tests.finalize",
        inputs=(ArtifactSpec("source", media_type="text/plain", schema_ref="document-v1"),),
        input_bindings=(
            ArtifactBinding("source", ArtifactSourceKind.NODE_OUTPUT, "draft", "draft"),
        ),
        outputs=(ArtifactSpec("final", media_type="text/plain", schema_ref="document-v1"),),
    )
    value = WorkflowDefinition(
        "scheduler.pipeline",
        "Artifact pipeline",
        nodes=(second, first),
        edges=(EdgeDefinition("draft-finalize", "draft", "finalize"),),
        inputs=(topic,),
        outputs=(ArtifactSpec("result", media_type="text/plain", schema_ref="document-v1"),),
        output_bindings=(
            ArtifactBinding("result", ArtifactSourceKind.NODE_OUTPUT, "final", "finalize"),
        ),
    )
    value.validate()
    return value


def test_scheduler_construction_is_side_effect_free(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-not-created"
    database = tmp_path / "state-not-created" / "orchestrator.sqlite3"
    scheduler = WorkflowScheduler(
        WorkflowStore(database),
        workspace,
        ExecutorRegistry(),
    )

    assert scheduler.workspace_root == workspace.absolute()
    assert not workspace.exists()
    assert not database.parent.exists()
    assert not scheduler.lock_path.exists()


def test_scheduler_executes_edge_order_and_verifies_artifact_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _pipeline_definition()
    _publish_and_create(store, definition, run_id="run-pipeline", inputs={"topic": {"name": "alpha"}})

    observed: list[tuple[str, str]] = []

    def draft(context: ExecutionContext) -> ExecutionResult:
        assert context.inputs["seed"].value == {"name": "alpha"}
        path = context.output_root / "draft.txt"
        path.write_text("draft alpha", encoding="utf-8")
        observed.append((context.node.node_id, context.node_run.node_run_id))
        return ExecutionResult(
            ExecutionOutcome.COMPLETED,
            produced_artifacts=(ProducedArtifact("draft", path, "text/plain", "document-v1"),),
        )

    def finalize(context: ExecutionContext) -> ExecutionResult:
        source = context.inputs["source"]
        assert source.sha256 and source.uri.startswith(".workflow_artifacts/v1/objects/")
        path = context.output_root / "final.txt"
        path.write_text("final alpha", encoding="utf-8")
        observed.append((context.node.node_id, context.node_run.node_run_id))
        return ExecutionResult(
            ExecutionOutcome.COMPLETED,
            produced_artifacts=(ProducedArtifact("final", path, "text/plain", "document-v1"),),
        )

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.draft": draft, "tests.finalize": finalize}),)),
        config=SchedulerConfig(max_workers=2, lease_seconds=10, heartbeat_seconds=1),
        schema_validators={"document-v1": lambda _path: None},
    )
    report = scheduler.run_until_idle(("run-pipeline",), now=BASE_TIME)

    assert report.completed == 2
    assert report.failed == report.lease_lost == 0
    assert store.get_run("run-pipeline")["status"] == "completed"
    assert [item[0] for item in observed] == ["draft", "finalize"]
    artifacts = store.list_artifacts("run-pipeline")
    assert [item["artifact_id"] for item in artifacts] == ["draft", "final"]
    resolved = scheduler.artifacts.resolve_workflow_outputs(definition, "run-pipeline")
    assert resolved["result"].source_artifact_id == "final"


def test_retry_policy_is_durable_and_reaches_second_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(
        retry_policy=RetryPolicy(
            max_attempts=2,
            retryable_error_codes=("transient",),
        )
    )
    _publish_and_create(store, definition, run_id="run-retry")
    calls = 0

    def work(_context: ExecutionContext) -> ExecutionResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ExecutionResult(
                ExecutionOutcome.RETRYABLE_FAILURE,
                error_code="transient",
                message="try again",
            )
        return ExecutionResult(ExecutionOutcome.COMPLETED)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=1),
    )
    report = scheduler.run_until_idle(("run-retry",), now=BASE_TIME)

    assert calls == 2
    assert report.retries_scheduled == 1
    assert store.get_run("run-retry")["status"] == "completed"
    assert [item["status"] for item in store.list_node_runs("run-retry")] == [
        "retryable-failure",
        "completed",
    ]


def test_pause_racing_with_retryable_result_blocks_automatic_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(
        workflow_id="scheduler.pause-retry-race",
        retry_policy=RetryPolicy(
            max_attempts=2,
            retryable_error_codes=("transient",),
        ),
    )
    _publish_and_create(store, definition, run_id="run-pause-retry")
    calls = 0

    def work(_context: ExecutionContext) -> ExecutionResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            store.pause_run("run-pause-retry", now=BASE_TIME)
            return ExecutionResult(
                ExecutionOutcome.RETRYABLE_FAILURE,
                error_code="transient",
            )
        return ExecutionResult(ExecutionOutcome.COMPLETED)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=1),
    )

    first = scheduler.run_once(("run-pause-retry",), now=BASE_TIME)
    blocked_cycle = scheduler.run_once(("run-pause-retry",), now=BASE_TIME)

    assert first.retries_scheduled == 1
    assert store.get_run("run-pause-retry")["status"] == "paused"
    assert store.latest_node_runs("run-pause-retry")[0]["status"] == "ready"
    assert blocked_cycle.claimed == 0
    assert calls == 1

    store.resume_run("run-pause-retry", now=BASE_TIME)
    resumed = scheduler.run_until_idle(("run-pause-retry",), now=BASE_TIME)
    assert resumed.completed == 1
    assert calls == 2
    assert store.get_run("run-pause-retry")["status"] == "completed"


def test_cancel_racing_with_output_commit_is_acknowledged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.cancel-commit-race")
    _publish_and_create(store, definition, run_id="run-cancel-commit")
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {"tests.work": lambda _context: ExecutionResult(ExecutionOutcome.COMPLETED)}
                ),
            )
        ),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=0.1),
    )
    verifying = threading.Event()
    release = threading.Event()
    original_verify = scheduler.artifacts.verify_outputs

    def gated_verify(*args, **kwargs):
        verifying.set()
        assert release.wait(timeout=5)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(scheduler.artifacts, "verify_outputs", gated_verify)
    reports: list[object] = []
    thread = threading.Thread(
        target=lambda: reports.append(scheduler.run_once(("run-cancel-commit",)))
    )
    thread.start()
    assert verifying.wait(timeout=3)
    store.request_cancel("run-cancel-commit")
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert reports[0].cancelled == 1  # type: ignore[union-attr]
    assert reports[0].lease_lost == 0  # type: ignore[union-attr]
    assert store.get_run("run-cancel-commit")["status"] == "cancelled"
    assert store.summary()["active_lease_count"] == 0


def test_final_completion_wins_atomically_over_late_cancel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.complete-cancel-atomic")
    _publish_and_create(store, definition, run_id="run-complete-cancel")
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (DeterministicExecutor({"tests.work": lambda _context: ExecutionResult(ExecutionOutcome.COMPLETED)}),)
        ),
    )
    advancing = threading.Event()
    release = threading.Event()
    original_advance = scheduler.kernel.advance
    advance_calls = 0

    def gated_advance(*args, **kwargs):
        nonlocal advance_calls
        advance_calls += 1
        if advance_calls >= 2:
            advancing.set()
            assert release.wait(timeout=5)
        return original_advance(*args, **kwargs)

    monkeypatch.setattr(scheduler.kernel, "advance", gated_advance)
    reports: list[object] = []
    thread = threading.Thread(
        target=lambda: reports.append(scheduler.run_once(("run-complete-cancel",)))
    )
    thread.start()
    assert advancing.wait(timeout=3)
    cancelled = store.request_cancel("run-complete-cancel")
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert cancelled["status"] == "completed"
    assert reports[0].completed == 1  # type: ignore[union-attr]
    assert store.get_run("run-complete-cancel")["status"] == "completed"
    assert "run.cancelled" not in [event["event_type"] for event in store.list_events("run-complete-cancel")]


def test_codex_executor_receives_durable_session_and_binds_external_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prompt = tmp_path / "sealed.md"
    prompt.write_text("Create the declared report.", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    node = NodeDefinition(
        "compose",
        "Compose",
        NodeType.CODEX,
        10,
        prompt_ref=PromptRef("sci/prompt", digest, "1"),
        execution_profile=ExecutionProfile(
            model="gpt-frozen",
            reasoning_effort="high",
            token_limit=100,
            timeout_seconds=60,
            allowed_tools=("read", "write"),
            network_access=False,
        ),
        outputs=(ArtifactSpec("report", media_type="text/markdown"),),
    )
    definition = WorkflowDefinition("scheduler.codex", "Codex", nodes=(node,), edges=())
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=BASE_TIME.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(
        definition.sha256(),
        (ResourceBinding("sci/prompt", str(prompt.resolve()), digest),),
    )
    store.create_run(version, binding, run_id="run-codex", now=BASE_TIME)
    seen: list[SessionHandle] = []

    class Client:
        def run(self, *, session, request, control):
            assert isinstance(session, SessionHandle)
            assert request.prompt == "Create the declared report."
            control.heartbeat()
            seen.append(session)
            return CodexResponse(
                "completed",
                output_text="done",
                external_session_id="external-codex-1",
                usage={"total_tokens": 12},
            )

    def collect(context: ExecutionContext, _response: CodexResponse):
        target = context.output_root / "report.md"
        target.write_text("program verified", encoding="utf-8")
        return (ProducedArtifact("report", target, "text/markdown"),)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((CodexSDKExecutor(Client(), output_collector=collect),)),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=1),
    )
    report = scheduler.run_until_idle(("run-codex",), now=BASE_TIME)

    assert report.completed == 1
    assert store.get_run("run-codex")["status"] == "completed"
    assert len(seen) == 1
    sessions = store.list_sessions("run-codex")
    assert len(sessions) == 1
    assert sessions[0]["external_session_id"] == "external-codex-1"
    assert sessions[0]["session_record_id"] == seen[0].session_record_id


def test_scheduler_revalidates_bound_files_before_executor_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prompt = tmp_path / "delete-before-dispatch.md"
    prompt.write_text("Never dispatch stale resources.", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    node = NodeDefinition(
        "compose",
        "Compose",
        NodeType.CODEX,
        10,
        prompt_ref=PromptRef("dispatch/prompt", digest, "1"),
        execution_profile=ExecutionProfile(
            model="gpt-frozen",
            reasoning_effort="high",
            token_limit=100,
            timeout_seconds=60,
            allowed_tools=("read", "write"),
            network_access=False,
        ),
    )
    definition = WorkflowDefinition("scheduler.resource-seal", "Resource seal", nodes=(node,), edges=())
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=BASE_TIME.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(
        definition.sha256(),
        (ResourceBinding("dispatch/prompt", str(prompt.resolve()), digest),),
    )
    store.create_run(version, binding, run_id="run-resource-seal", now=BASE_TIME)
    prompt.unlink()
    called = False

    class Client:
        def run(self, *, session, request, control):
            nonlocal called
            called = True
            raise AssertionError("client must not be called for an unreadable bound resource")

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((CodexSDKExecutor(Client()),)),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=1),
    )
    report = scheduler.run_until_idle(("run-resource-seal",), now=BASE_TIME)

    assert called is False
    assert report.failed == 1
    assert store.get_run("run-resource-seal")["status"] == "blocked"
    latest = store.latest_node_runs("run-resource-seal")[0]
    assert latest["status"] == "blocked"
    assert latest["error_code"] == "EXECUTION_CONTEXT_INVALID"


def test_codex_reconciliation_binds_external_session_before_operator_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prompt = tmp_path / "retry-prompt.md"
    prompt.write_text("Retry safely.", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    node = NodeDefinition(
        "compose",
        "Compose",
        NodeType.CODEX,
        10,
        prompt_ref=PromptRef("retry/prompt", digest, "1"),
        execution_profile=ExecutionProfile(
            model="gpt-frozen",
            reasoning_effort="high",
            token_limit=100,
            timeout_seconds=60,
        ),
        retry_policy=RetryPolicy(
            max_attempts=2,
            retryable_error_codes=("CODEX_SDK_TURN_NOT_COMPLETED",),
        ),
        session_policy=SessionPolicy(
            session_group="compose",
            reuse_within_run=True,
            reuse_on_retry=True,
        ),
    )
    definition = WorkflowDefinition("scheduler.codex-retry", "Codex retry", nodes=(node,), edges=())
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=BASE_TIME.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(
        definition.sha256(),
        (ResourceBinding("retry/prompt", str(prompt.resolve()), digest),),
    )
    store.create_run(version, binding, run_id="run-codex-retry", now=BASE_TIME)
    seen_external: list[str] = []

    class Client:
        def run(self, *, session, request, control):
            seen_external.append(session.external_session_id)
            if len(seen_external) == 1:
                return CodexResponse(
                    "interrupted",
                    external_session_id="external-reused",
                    usage={"total_tokens": 5},
                )
            assert session.external_session_id == "external-reused"
            return CodexResponse(
                "completed",
                output_text="done",
                external_session_id="external-reused",
                usage={"total_tokens": 5},
            )

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((CodexSDKExecutor(Client(), output_collector=lambda _c, _r: ()),)),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=1),
    )
    first = scheduler.run_once(("run-codex-retry",), now=BASE_TIME)
    blocked = store.latest_node_runs("run-codex-retry")[0]

    assert first.failed == 1
    assert blocked["status"] == "blocked"
    assert blocked["error_code"] == "CODEX_TURN_RECONCILIATION_REQUIRED"
    assert store.list_sessions("run-codex-retry")[0]["external_session_id"] == "external-reused"
    store.retry_node(
        str(blocked["node_run_id"]),
        reason="operator-reconciled-remote-turn",
        now=BASE_TIME,
    )
    second = scheduler.run_until_idle(("run-codex-retry",), now=BASE_TIME)

    assert second.completed == 1
    assert seen_external == ["", "external-reused"]
    assert store.get_run("run-codex-retry")["status"] == "completed"
    assert len(store.list_sessions("run-codex-retry")) == 1


def test_missing_executor_is_a_typed_block_not_context_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition()
    _publish_and_create(store, definition, run_id="run-missing-executor")

    report = WorkflowScheduler(store, tmp_path, ExecutorRegistry()).run_until_idle(
        ("run-missing-executor",)
    )

    assert report.failed == 1
    node = store.latest_node_runs("run-missing-executor")[0]
    assert node["status"] == "blocked"
    assert node["error_code"] == "EXECUTOR_NOT_REGISTERED"


def test_untrusted_output_outside_workspace_is_blocked_not_recorded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untrusted", encoding="utf-8")
    store = _store(workspace)
    definition = _single_definition(outputs=(ArtifactSpec("result", media_type="text/plain"),))
    _publish_and_create(store, definition, run_id="run-outside")

    def work(_context: ExecutionContext) -> ExecutionResult:
        return ExecutionResult(
            ExecutionOutcome.COMPLETED,
            produced_artifacts=(ProducedArtifact("result", outside, "text/plain"),),
        )

    report = WorkflowScheduler(
        store,
        workspace,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
    ).run_until_idle(("run-outside",))

    assert report.failed == 1
    assert store.get_run("run-outside")["status"] == "blocked"
    assert store.list_artifacts("run-outside") == []
    node = store.latest_node_runs("run-outside")[0]
    assert node["error_code"] == "OUTPUT_CONTRACT_UNSATISFIED"


def test_human_executor_waits_for_programmatic_completion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(
        node_type=NodeType.HUMAN,
        implementation_ref="",
    )
    _publish_and_create(store, definition, run_id="run-human")
    scheduler = WorkflowScheduler(store, tmp_path, ExecutorRegistry((HumanExecutor(),)))

    waiting = scheduler.run_once(("run-human",), now=BASE_TIME)
    node = store.latest_node_runs("run-human")[0]
    assert waiting.waiting == 1
    assert node["status"] == "waiting-human"
    scheduler.complete_waiting_node(
        str(node["node_run_id"]),
        expected_status=NodeRunStatus.WAITING_HUMAN,
        now=BASE_TIME,
    )
    scheduler.run_until_idle(("run-human",), now=BASE_TIME)
    assert store.get_run("run-human")["status"] == "completed"


def test_host_rejects_unverified_human_output_before_waiting_completion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(
        workflow_id="scheduler.human-verified",
        node_type=NodeType.HUMAN,
        implementation_ref="",
        outputs=(ArtifactSpec("answer", media_type="application/json"),),
    )
    _publish_and_create(store, definition, run_id="run-human-verified")
    scheduler = WorkflowScheduler(store, tmp_path, ExecutorRegistry((HumanExecutor(),)))
    scheduler.run_once(("run-human-verified",), now=BASE_TIME)
    node = store.latest_node_runs("run-human-verified")[0]
    invalid = tmp_path / "human-invalid.json"
    invalid.write_bytes(b"not valid JSON\xff")

    with pytest.raises(ArtifactContractError) as exc_info:
        scheduler.complete_waiting_node(
            str(node["node_run_id"]),
            (ProducedArtifact("answer", invalid, "application/json"),),
            expected_status=NodeRunStatus.WAITING_HUMAN,
            now=BASE_TIME,
        )

    assert exc_info.value.code == "artifact.content.invalid"
    assert store.get_node_run(str(node["node_run_id"]))["status"] == "waiting-human"
    assert store.list_artifacts("run-human-verified") == []


def test_cancel_request_is_observed_without_exposing_store_or_grant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition()
    _publish_and_create(store, definition, run_id="run-cancel")
    entered = threading.Event()

    def work(context: ExecutionContext) -> ExecutionResult:
        entered.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if context.control.cancellation_requested():
                return ExecutionResult(ExecutionOutcome.CANCELLED)
            time.sleep(0.01)
        raise AssertionError("cancellation was not delivered")

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
        config=SchedulerConfig(lease_seconds=5, heartbeat_seconds=0.1),
    )
    reports: list[object] = []
    thread = threading.Thread(target=lambda: reports.append(scheduler.run_once(("run-cancel",))))
    thread.start()
    assert entered.wait(timeout=3)
    store.request_cancel("run-cancel")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert reports[0].cancelled == 1  # type: ignore[union-attr]
    assert store.get_run("run-cancel")["status"] == "cancelled"


def test_pause_after_claim_does_not_block_owned_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.pause-after-claim")
    _publish_and_create(store, definition, run_id="run-pause-owned")
    calls: list[str] = []
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {
                        "tests.work": lambda _context: (
                            calls.append("work") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        )
                    }
                ),
            )
        ),
    )
    preparing = threading.Event()
    release = threading.Event()
    original_prepare = scheduler._prepare_context

    def gated_prepare(*args, **kwargs):
        preparing.set()
        assert release.wait(timeout=5)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_prepare_context", gated_prepare)
    reports: list[object] = []
    thread = threading.Thread(
        target=lambda: reports.append(scheduler.run_once(("run-pause-owned",)))
    )
    thread.start()
    assert preparing.wait(timeout=3)
    store.pause_run("run-pause-owned")
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert calls == ["work"]
    assert reports[0].completed == 1  # type: ignore[union-attr]
    assert store.get_run("run-pause-owned")["status"] == "completed"


def test_cancel_after_context_preparation_blocks_executor_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.cancel-before-executor")
    _publish_and_create(store, definition, run_id="run-cancel-before-executor")
    calls: list[str] = []
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {
                        "tests.work": lambda _context: (
                            calls.append("executed") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        )
                    }
                ),
            )
        ),
    )
    prepared = threading.Event()
    release = threading.Event()
    original_prepare = scheduler._prepare_context

    def gated_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        prepared.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(scheduler, "_prepare_context", gated_prepare)
    reports: list[object] = []
    thread = threading.Thread(
        target=lambda: reports.append(scheduler.run_once(("run-cancel-before-executor",)))
    )
    thread.start()
    assert prepared.wait(timeout=3)
    store.request_cancel("run-cancel-before-executor")
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert calls == []
    assert reports[0].cancelled == 1  # type: ignore[union-attr]
    assert store.get_run("run-cancel-before-executor")["status"] == "cancelled"


def test_background_heartbeat_keeps_long_node_lease_live(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition()
    _publish_and_create(store, definition, run_id="run-heartbeat")

    def work(_context: ExecutionContext) -> ExecutionResult:
        time.sleep(1.2)
        return ExecutionResult(ExecutionOutcome.COMPLETED)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
        config=SchedulerConfig(lease_seconds=1, heartbeat_seconds=0.1),
    )
    report = scheduler.run_once(("run-heartbeat",))

    assert report.completed == 1
    assert report.lease_lost == 0
    assert store.get_run("run-heartbeat")["status"] == "completed"


def test_global_worker_bound_and_round_robin_progress(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.parallel")
    for index in range(4):
        _publish_and_create(store, definition, run_id=f"run-{index}")
    guard = threading.Lock()
    release = threading.Event()
    two_entered = threading.Event()
    active = 0
    peak = 0

    def work(_context: ExecutionContext) -> ExecutionResult:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_entered.set()
        assert release.wait(timeout=5)
        with guard:
            active -= 1
        return ExecutionResult(ExecutionOutcome.COMPLETED)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}, max_concurrency=2),)),
        config=SchedulerConfig(max_workers=2, lease_seconds=10, heartbeat_seconds=0.1),
    )
    reports: list[object] = []
    thread = threading.Thread(target=lambda: reports.append(scheduler.run_once()))
    thread.start()
    assert two_entered.wait(timeout=3)
    release.set()
    thread.join(timeout=5)

    assert reports[0].claimed == 2  # type: ignore[union-attr]
    assert peak == 2
    tail = scheduler.run_until_idle()
    assert tail.completed == 2
    assert all(store.get_run(f"run-{index}")["status"] == "completed" for index in range(4))


def test_executor_specific_concurrency_cap_limits_claims(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.executor-cap")
    for index in range(3):
        _publish_and_create(store, definition, run_id=f"cap-{index}")
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {"tests.work": lambda _context: ExecutionResult(ExecutionOutcome.COMPLETED)},
                    max_concurrency=1,
                ),
            )
        ),
        config=SchedulerConfig(max_workers=3, lease_seconds=10, heartbeat_seconds=1),
    )

    first = scheduler.run_once()

    assert first.claimed == 1
    assert first.completed == 1
    assert first.runnable_remaining == 2
    tail = scheduler.run_until_idle()
    assert tail.completed == 2


def test_atomic_claim_rechecks_global_capacity_after_scheduler_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.capacity-race")
    _publish_and_create(store, definition, run_id="run-capacity-held")
    _publish_and_create(store, definition, run_id="run-capacity-target")
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (DeterministicExecutor({"tests.work": lambda _context: ExecutionResult(ExecutionOutcome.COMPLETED)}),)
        ),
        config=SchedulerConfig(max_workers=1, lease_seconds=10, heartbeat_seconds=1),
    )
    snapshotted = threading.Event()
    release = threading.Event()
    original_summary = store.summary

    def gated_summary():
        result = original_summary()
        snapshotted.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(store, "summary", gated_summary)
    reports: list[object] = []
    thread = threading.Thread(
        target=lambda: reports.append(scheduler.run_once(("run-capacity-target",), now=BASE_TIME))
    )
    thread.start()
    assert snapshotted.wait(timeout=3)
    held = store.latest_node_runs("run-capacity-held")[0]
    store.mark_node_ready(str(held["node_run_id"]), now=BASE_TIME)
    assert store.claim_ready_node("run-capacity-held", "external-owner", now=BASE_TIME) is not None
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert reports[0].claimed == 0  # type: ignore[union-attr]
    assert store.summary()["active_lease_count"] == 1
    assert store.get_run("run-capacity-target")["status"] == "queued"


def test_live_handover_lease_counts_against_executor_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.handover")
    _publish_and_create(store, definition, run_id="run-held")
    _publish_and_create(store, definition, run_id="run-next")
    current = datetime.now(timezone.utc)
    held_node = store.latest_node_runs("run-held")[0]
    store.mark_node_ready(str(held_node["node_run_id"]), now=current)
    held = store.claim_ready_node("run-held", "previous-owner", lease_seconds=60, now=current)
    assert held is not None
    calls = 0

    def work(_context: ExecutionContext) -> ExecutionResult:
        nonlocal calls
        calls += 1
        return ExecutionResult(ExecutionOutcome.COMPLETED)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
        config=SchedulerConfig(max_workers=3, lease_seconds=10, heartbeat_seconds=1),
    )
    report = scheduler.run_once()

    assert report.claimed == 0
    assert report.runnable_remaining == 1
    assert calls == 0
    assert store.get_run("run-next")["status"] == "queued"


def test_scoped_cycle_recovers_expired_global_lease_before_capacity_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition(workflow_id="scheduler.scoped-recovery")
    _publish_and_create(store, definition, run_id="run-expired-unscoped")
    _publish_and_create(store, definition, run_id="run-scoped-target")
    held_node = store.latest_node_runs("run-expired-unscoped")[0]
    store.mark_node_ready(str(held_node["node_run_id"]), now=BASE_TIME)
    assert store.claim_ready_node(
        "run-expired-unscoped",
        "dead-owner",
        lease_seconds=1,
        now=BASE_TIME,
    ) is not None
    calls: list[str] = []
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {
                        "tests.work": lambda _context: (
                            calls.append("target") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        )
                    }
                ),
            )
        ),
        config=SchedulerConfig(max_workers=1, lease_seconds=10, heartbeat_seconds=1),
    )

    report = scheduler.run_once(
        ("run-scoped-target",),
        now=BASE_TIME.replace(second=BASE_TIME.second + 2),
    )

    assert report.recovered == 1
    assert report.claimed == 1
    assert calls == ["target"]
    assert store.get_run("run-scoped-target")["status"] == "completed"


def test_kernel_invariant_failure_excludes_corrupt_run_from_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _pipeline_definition()
    _publish_and_create(
        store,
        definition,
        run_id="run-corrupt-ready",
        inputs={"topic": {"name": "alpha"}},
    )
    for node in store.latest_node_runs("run-corrupt-ready"):
        store.mark_node_ready(str(node["node_run_id"]), now=BASE_TIME)
    calls: list[str] = []
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {
                        "tests.draft": lambda _context: (
                            calls.append("draft") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        ),
                        "tests.finalize": lambda _context: (
                            calls.append("finalize") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        ),
                    }
                ),
            )
        ),
    )

    report = scheduler.run_once(("run-corrupt-ready",), now=BASE_TIME)

    assert report.claimed == 0
    assert calls == []
    assert any(issue.code == "WORKFLOW_INVARIANT_VIOLATION" for issue in report.issues)


def test_atomic_claim_rechecks_linear_order_after_kernel_advance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    definition = _pipeline_definition()
    _publish_and_create(
        store,
        definition,
        run_id="run-order-race",
        inputs={"topic": {"name": "alpha"}},
    )
    calls: list[str] = []
    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (
                DeterministicExecutor(
                    {
                        "tests.draft": lambda _context: (
                            calls.append("draft") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        ),
                        "tests.finalize": lambda _context: (
                            calls.append("finalize") or ExecutionResult(ExecutionOutcome.COMPLETED)
                        ),
                    }
                ),
            )
        ),
    )
    advanced = threading.Event()
    release = threading.Event()
    original_advance_candidates = scheduler._advance_candidates

    def gated_advance_candidates(*args, **kwargs):
        result = original_advance_candidates(*args, **kwargs)
        advanced.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(scheduler, "_advance_candidates", gated_advance_candidates)
    reports: list[object] = []
    thread = threading.Thread(
        target=lambda: reports.append(scheduler.run_once(("run-order-race",), now=BASE_TIME))
    )
    thread.start()
    assert advanced.wait(timeout=3)
    downstream = next(
        item for item in store.latest_node_runs("run-order-race") if item["node_id"] == "finalize"
    )
    store.mark_node_ready(str(downstream["node_run_id"]), now=BASE_TIME)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert reports[0].claimed == 0  # type: ignore[union-attr]
    assert calls == []
    assert any(  # type: ignore[union-attr]
        issue.code == "CLAIM_INVARIANT_VIOLATION" for issue in reports[0].issues
    )


def test_owner_lock_contention_dispatches_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = _single_definition()
    _publish_and_create(store, definition, run_id="run-locked")
    calls = 0

    def work(_context: ExecutionContext) -> ExecutionResult:
        nonlocal calls
        calls += 1
        return ExecutionResult(ExecutionOutcome.COMPLETED)

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor({"tests.work": work}),)),
    )
    with FileLock(scheduler.lock_path, stale_seconds=0):
        report = scheduler.run_once()

    assert report.owner_acquired is False
    assert report.claimed == 0
    assert calls == 0
    assert store.get_run("run-locked")["status"] == "queued"
