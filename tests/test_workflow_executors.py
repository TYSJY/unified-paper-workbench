from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.artifacts import ArtifactService
from unified_paper_orchestrator.workflow.contracts import (
    ArtifactSpec,
    ArtifactSourceKind,
    ExecutionProfile,
    NodeDefinition,
    NodeRun,
    NodeRunStatus,
    NodeType,
    PromptRef,
    ResourceBinding,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRuntimeBinding,
)
from unified_paper_orchestrator.workflow.executors import (
    CodexRequest,
    CodexResponse,
    CodexSDKExecutor,
    DeterministicExecutor,
    BoundArtifact,
    ExecutionContext,
    ExecutionControl,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRegistry,
    HumanExecutor,
    LegacyStageExecutor,
    PluginExecutor,
    ProducedArtifact,
)
from unified_paper_orchestrator.workflow.sessions import SessionHandle


NOW = "2026-08-05T00:00:00+00:00"


def _definition(node: NodeDefinition) -> WorkflowDefinition:
    definition = WorkflowDefinition(
        workflow_id="tests.executor-flow",
        name="Executor test",
        description="Isolated executor contract test",
        nodes=(node,),
        edges=(),
    )
    definition.validate()
    return definition


def _context(
    tmp_path: Path,
    node: NodeDefinition,
    *,
    prompt_path: Path | None = None,
    control: ExecutionControl | None = None,
    session: SessionHandle | None = None,
) -> ExecutionContext:
    output_root = (tmp_path / "attempt-output").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    definition = _definition(node)
    if node.prompt_ref is None:
        resources: tuple[ResourceBinding, ...] = ()
    else:
        assert prompt_path is not None
        resources = (
            ResourceBinding(
                resource_id=node.prompt_ref.resource_id,
                absolute_path=str(prompt_path.resolve()),
                content_sha256=node.prompt_ref.content_sha256,
            ),
        )
    binding = WorkflowRuntimeBinding(
        definition_sha256=definition.sha256(),
        resources=resources,
    )
    binding.validate(definition)
    workflow_run = WorkflowRun(
        run_id="workflow-run-test",
        workflow_version_id="tests.executor-flow.v1",
        definition_sha256=definition.sha256(),
        binding_sha256=binding.sha256(),
        status=WorkflowRunStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
    )
    node_run = NodeRun(
        workflow_run_id=workflow_run.run_id,
        node_id=node.node_id,
        node_run_id="node-run-test",
        status=NodeRunStatus.RUNNING,
        session_id=session.session_record_id if session is not None else "",
        created_at=NOW,
        started_at=NOW,
    )
    return ExecutionContext(
        definition=definition,
        runtime_binding=binding,
        workflow_run=workflow_run,
        node_run=node_run,
        node=node,
        workspace_root=tmp_path.resolve(),
        output_root=output_root,
        session=session,
        control=control or ExecutionControl(),
    )


def _artifact(tmp_path: Path, artifact_id: str = "report") -> ProducedArtifact:
    target = tmp_path / "attempt-output" / f"{artifact_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("verified output", encoding="utf-8")
    return ProducedArtifact(
        artifact_id=artifact_id,
        path=target.resolve(),
        media_type="text/markdown",
        metadata={"collector": "test"},
    )


def _deterministic_node(*, implementation_ref: str = "tests.build") -> NodeDefinition:
    return NodeDefinition(
        node_id="build",
        name="Build",
        node_type=NodeType.DETERMINISTIC,
        display_order=10,
        implementation_ref=implementation_ref,
        outputs=(ArtifactSpec("report", media_type="text/markdown"),),
    )


def _codex_context(
    tmp_path: Path,
    *,
    parameters: dict[str, object] | None = None,
    token_limit: int = 100,
) -> ExecutionContext:
    prompt = tmp_path / "sealed-prompt.md"
    prompt.write_text("Produce the declared report.", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    node = NodeDefinition(
        node_id="compose",
        name="Compose",
        node_type=NodeType.CODEX,
        display_order=10,
        prompt_ref=PromptRef("prompts/compose", digest, "1"),
        execution_profile=ExecutionProfile(
            model="gpt-frozen",
            reasoning_effort="high",
            token_limit=token_limit,
            timeout_seconds=321,
            allowed_tools=("read", "write"),
            network_access=False,
            parameters=parameters or {"temperature": 0},
        ),
        outputs=(ArtifactSpec("report", media_type="text/markdown"),),
    )
    session = SessionHandle(
        session_record_id="session-record-test",
        run_id="workflow-run-test",
        session_group="node/node-run-test",
        executor="codex-sdk",
        generation=1,
        external_session_id="",
        owner_name="workflow/workflow-run-test/node/node-run-test/codex-sdk/generation-1",
    )
    return _context(tmp_path, node, prompt_path=prompt, session=session)


def test_context_rejects_snapshot_mismatch_and_freezes_inputs(tmp_path: Path) -> None:
    context = _context(tmp_path, _deterministic_node())
    with pytest.raises(TypeError):
        context.inputs["new"] = _artifact(tmp_path, "new")  # type: ignore[index]

    with pytest.raises(ValueError, match="node run does not match"):
        replace(context, node_run=replace(context.node_run, node_id="different"))


def test_deterministic_executor_requires_explicit_allowlist_and_artifact_evidence(tmp_path: Path) -> None:
    context = _context(tmp_path, _deterministic_node(implementation_ref="os.system"))
    missing = DeterministicExecutor().execute(context)
    assert missing.outcome is ExecutionOutcome.BLOCKED
    assert missing.error_code == "DETERMINISTIC_IMPLEMENTATION_NOT_ALLOWLISTED"

    called: list[str] = []

    def build(current: ExecutionContext) -> ExecutionResult:
        called.append(current.node.node_id)
        return ExecutionResult(
            outcome=ExecutionOutcome.COMPLETED,
            message="built",
            produced_artifacts=(_artifact(tmp_path),),
        )

    allowed_context = _context(
        tmp_path,
        replace(context.node, implementation_ref="tests.build"),
    )
    completed = DeterministicExecutor({"tests.build": build}).execute(allowed_context)
    assert completed.outcome is ExecutionOutcome.COMPLETED
    assert called == ["build"]

    no_evidence = DeterministicExecutor(
        {"tests.build": lambda _context: ExecutionResult(outcome=ExecutionOutcome.COMPLETED)}
    ).execute(allowed_context)
    assert no_evidence.outcome is ExecutionOutcome.BLOCKED
    assert no_evidence.error_code == "OUTPUT_CONTRACT_UNSATISFIED"


def test_plugin_is_allowlisted_and_callback_errors_are_typed(tmp_path: Path) -> None:
    node = replace(_deterministic_node(), node_type=NodeType.PLUGIN, implementation_ref="plugins.export")
    context = _context(tmp_path, node)

    missing = PluginExecutor().execute(context)
    assert missing.error_code == "PLUGIN_IMPLEMENTATION_NOT_ALLOWLISTED"

    def explode(_context: ExecutionContext) -> ExecutionResult:
        raise RuntimeError("secret exception detail")

    failed = PluginExecutor({"plugins.export": explode}).execute(context)
    assert failed.outcome is ExecutionOutcome.BLOCKED
    assert failed.error_code == "PLUGIN_IMPLEMENTATION_FAILED"
    assert "secret exception detail" not in failed.message
    assert failed.metadata["error_type"] == "RuntimeError"


def test_allowlisted_executor_honors_cancellation_and_heartbeat_callbacks(tmp_path: Path) -> None:
    node = _deterministic_node()
    calls: list[str] = []

    def build(_context: ExecutionContext) -> ExecutionResult:
        calls.append("handler")
        return ExecutionResult(
            outcome=ExecutionOutcome.COMPLETED,
            produced_artifacts=(_artifact(tmp_path),),
        )

    cancelled = _context(
        tmp_path,
        node,
        control=ExecutionControl(cancellation_probe=lambda: True),
    )
    result = DeterministicExecutor({"tests.build": build}).execute(cancelled)
    assert result.outcome is ExecutionOutcome.CANCELLED
    assert calls == []

    live = _context(
        tmp_path,
        node,
        control=ExecutionControl(heartbeat_callback=lambda: calls.append("heartbeat")),
    )
    result = DeterministicExecutor({"tests.build": build}).execute(live)
    assert result.outcome is ExecutionOutcome.COMPLETED
    assert calls == ["heartbeat", "handler", "heartbeat"]


def test_human_executor_only_returns_waiting_marker(tmp_path: Path) -> None:
    node = NodeDefinition(
        node_id="approve",
        name="Approve",
        node_type=NodeType.HUMAN,
        display_order=10,
        outputs=(ArtifactSpec("approval", media_type="application/json"),),
        metadata={"instruction": "Review the draft."},
    )
    result = HumanExecutor().execute(_context(tmp_path, node))
    assert result.outcome is ExecutionOutcome.WAITING_HUMAN
    assert result.error_code == "HUMAN_ACTION_REQUIRED"
    assert result.message == "Review the draft."
    assert result.metadata["required_outputs"] == ("approval",)


def test_legacy_stage_never_falls_back_when_callback_is_missing(tmp_path: Path) -> None:
    node = replace(
        _deterministic_node(),
        node_type=NodeType.LEGACY_STAGE,
        implementation_ref="legacy.writing",
    )
    context = _context(tmp_path, node)
    executor = LegacyStageExecutor()
    assert executor.descriptor().available is False
    result = executor.execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "LEGACY_STAGE_CALLBACK_NOT_CONFIGURED"

    called: list[str] = []

    def callback(current: ExecutionContext) -> ExecutionResult:
        called.append(current.node.node_id)
        return ExecutionResult(
            outcome=ExecutionOutcome.COMPLETED,
            produced_artifacts=(_artifact(tmp_path),),
        )

    result = LegacyStageExecutor({"legacy.writing": callback}).execute(context)
    assert result.outcome is ExecutionOutcome.COMPLETED
    assert called == ["build"]


def test_registry_routes_by_node_type_and_refuses_implicit_executor(tmp_path: Path) -> None:
    registry = ExecutorRegistry((HumanExecutor(), DeterministicExecutor()))
    assert [item.node_type for item in registry.describe()] == [NodeType.DETERMINISTIC, NodeType.HUMAN]

    human = NodeDefinition("human", "Human", NodeType.HUMAN, 10)
    assert registry.execute(_context(tmp_path, human)).outcome is ExecutionOutcome.WAITING_HUMAN

    subflow = NodeDefinition(
        "subflow",
        "Subflow",
        NodeType.SUBWORKFLOW,
        10,
        implementation_ref="workflows.child.v1",
    )
    missing = registry.execute(_context(tmp_path, subflow))
    assert missing.error_code == "EXECUTOR_NOT_REGISTERED"

    with pytest.raises(ValueError, match="duplicate executor"):
        ExecutorRegistry((HumanExecutor(), HumanExecutor()))
    assert PluginExecutor().descriptor().max_concurrency == 1
    assert HumanExecutor().descriptor().max_concurrency == 64


def test_context_rejects_cross_run_session_handle(tmp_path: Path) -> None:
    context = _codex_context(tmp_path)
    assert context.session is not None
    foreign = replace(
        context.session,
        run_id="different-run",
        owner_name="workflow/different-run/node/node-run-test/codex-sdk/generation-1",
    )
    with pytest.raises(ValueError, match="session handle does not match"):
        replace(context, session=foreign)


def test_output_declaration_must_stay_in_attempt_root(tmp_path: Path) -> None:
    context = _context(tmp_path, _deterministic_node())
    outside = tmp_path / "outside.md"
    outside.write_text("old output", encoding="utf-8")
    result = DeterministicExecutor(
        {
            "tests.build": lambda _context: ExecutionResult(
                outcome=ExecutionOutcome.COMPLETED,
                produced_artifacts=(
                    ProducedArtifact("report", outside.resolve(), media_type="text/markdown"),
                ),
            )
        }
    ).execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert "outside attempt root" in result.metadata["issues"][0]


class _FakeCodexClient:
    def __init__(self, response: CodexResponse | None = None) -> None:
        self.response = response or CodexResponse(
            status="completed",
            output_text="I created the report.",
            external_session_id="sdk-session-1",
            usage={"total_tokens": 90},
        )
        self.calls: list[tuple[SessionHandle, CodexRequest, ExecutionControl]] = []

    def run(
        self,
        *,
        session: SessionHandle,
        request: CodexRequest,
        control: ExecutionControl,
    ) -> CodexResponse:
        self.calls.append((session, request, control))
        return self.response


def test_codex_sdk_uses_injected_components_and_frozen_profile(tmp_path: Path, monkeypatch) -> None:
    context = _codex_context(tmp_path)
    heartbeats: list[str] = []
    context = replace(
        context,
        control=ExecutionControl(heartbeat_callback=lambda: heartbeats.append("beat")),
    )
    client = _FakeCodexClient()
    session = context.session
    validation_calls: list[str] = []
    original_validate_files = WorkflowRuntimeBinding.validate_files

    def validate_files(binding: WorkflowRuntimeBinding, definition: WorkflowDefinition) -> None:
        validation_calls.append(binding.sha256())
        original_validate_files(binding, definition)

    monkeypatch.setattr(WorkflowRuntimeBinding, "validate_files", validate_files)
    executor = CodexSDKExecutor(
        client,
        output_collector=lambda supplied, _response: (_artifact(tmp_path),) if supplied is context else (),
    )

    result = executor.execute(context)
    assert result.outcome is ExecutionOutcome.COMPLETED
    assert validation_calls == [context.runtime_binding.sha256()] * 3
    assert len(client.calls) == 1
    supplied_session, request, control = client.calls[0]
    assert supplied_session is session
    assert control is context.control
    assert heartbeats == ["beat", "beat", "beat"]
    assert request.prompt == "Produce the declared report."
    assert request.output_root == context.output_root
    assert request.model == "gpt-frozen"
    assert request.reasoning_effort == "high"
    assert request.token_limit == 100
    assert request.timeout_seconds == 321
    assert request.allowed_tools == ("read", "write")
    assert request.network_access is False
    assert request.inputs == {}
    assert request.parameters == {"temperature": 0}
    records = ArtifactService(tmp_path, object()).verify_outputs(
        context.node,
        result.produced_artifacts,
        output_root=context.output_root,
    )
    assert len(records) == 1
    assert records[0].sha256 == hashlib.sha256(
        (tmp_path / "attempt-output" / "report.md").read_bytes()
    ).hexdigest()


def test_execution_result_defensively_freezes_produced_metadata(tmp_path: Path) -> None:
    produced = ProducedArtifact(
        "report",
        tmp_path / "report.md",
        media_type="text/markdown",
        metadata={"nested": {"version": 1}},
    )
    result = ExecutionResult(
        outcome=ExecutionOutcome.COMPLETED,
        produced_artifacts=(produced,),
    )
    produced.metadata["nested"]["version"] = 2
    assert result.produced_artifacts[0].metadata["nested"]["version"] == 1
    with pytest.raises(TypeError):
        result.produced_artifacts[0].metadata["nested"]["version"] = 3
    with pytest.raises(ValueError, match="only completed"):
        ExecutionResult(
            outcome=ExecutionOutcome.RETRYABLE_FAILURE,
            produced_artifacts=(produced,),
        )


def test_codex_request_defensively_freezes_bound_inputs() -> None:
    bound = BoundArtifact(
        artifact_id="payload",
        source_kind=ArtifactSourceKind.WORKFLOW_INPUT,
        source_artifact_id="source",
        media_type="application/json",
        value={"nested": {"version": 1}},
    )
    request = CodexRequest(
        workflow_run_id="run-test",
        node_run_id="node-run-test",
        prompt="sealed",
        output_root=Path("C:/workflow/attempt-output"),
        model="gpt-frozen",
        reasoning_effort="high",
        token_limit=10,
        timeout_seconds=30,
        allowed_tools=(),
        network_access=False,
        inputs={"payload": bound},
    )
    bound.value["nested"]["version"] = 2
    assert request.inputs["payload"].value["nested"]["version"] == 1
    with pytest.raises(TypeError):
        request.inputs["payload"].value["nested"]["version"] = 3


def test_codex_text_cannot_prove_required_artifact(tmp_path: Path) -> None:
    context = _codex_context(tmp_path)
    client = _FakeCodexClient()
    executor = CodexSDKExecutor(
        client,
        output_collector=lambda _context, _response: (),
    )
    result = executor.execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "OUTPUT_CONTRACT_UNSATISFIED"
    assert "required output missing" in result.metadata["issues"][0]


@pytest.mark.parametrize(
    "parameters",
    [
        {"provider": "untrusted"},
        {"nested": {"endpoint-url": "https://untrusted.invalid"}},
        {"nested": {"apiKey": "secret"}},
        {"modelProvider": "untrusted"},
        {"request": [{"model": "mutable"}]},
        {"token_limit": 999999},
        {"extra_headers": [["Authorization", "Bearer attacker"]]},
        {"transport": {"url": "https://untrusted.invalid"}},
        {"client_options": {"host": "untrusted.invalid"}},
    ],
)
def test_codex_rejects_node_provider_and_frozen_profile_overrides(
    tmp_path: Path,
    parameters: dict[str, object],
) -> None:
    context = _codex_context(tmp_path, parameters=parameters)
    client = _FakeCodexClient()
    result = CodexSDKExecutor(
        client,
        output_collector=lambda _context, _response: (_artifact(tmp_path),),
    ).execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "CODEX_EXECUTION_PROFILE_OVERRIDE_FORBIDDEN"
    assert client.calls == []


def test_codex_revalidates_prompt_bytes_before_every_dispatch(tmp_path: Path) -> None:
    context = _codex_context(tmp_path)
    client = _FakeCodexClient()
    executor = CodexSDKExecutor(
        client,
        output_collector=lambda _context, _response: (_artifact(tmp_path),),
    )
    first = executor.execute(context)
    assert first.outcome is ExecutionOutcome.COMPLETED

    prompt_path = Path(context.runtime_binding.resources[0].absolute_path)
    prompt_path.write_text("tampered", encoding="utf-8")
    second = executor.execute(context)
    assert second.outcome is ExecutionOutcome.BLOCKED
    assert second.error_code == "CODEX_PROMPT_BINDING_INVALID"
    assert len(client.calls) == 1


def test_codex_discards_outputs_when_frozen_token_limit_is_exceeded(tmp_path: Path) -> None:
    context = _codex_context(tmp_path, token_limit=10)
    client = _FakeCodexClient(
        CodexResponse(
            status="completed",
            output_text="done",
            external_session_id="sdk-session-1",
            usage={"total_tokens": 11},
        )
    )
    collector_calls: list[str] = []

    def collect(_context: ExecutionContext, _response: CodexResponse) -> tuple[ProducedArtifact, ...]:
        collector_calls.append("called")
        return (_artifact(tmp_path),)

    result = CodexSDKExecutor(
        client,
        output_collector=collect,
    ).execute(context)
    assert result.outcome is ExecutionOutcome.FATAL_FAILURE
    assert result.error_code == "CODEX_TOKEN_LIMIT_EXCEEDED"
    assert collector_calls == []
    assert result.produced_artifacts == ()


def test_codex_fails_closed_when_usage_is_missing(tmp_path: Path) -> None:
    context = _codex_context(tmp_path)
    client = _FakeCodexClient(
        CodexResponse(
            status="completed",
            output_text="done",
            external_session_id="sdk-session-1",
            usage={},
        )
    )
    result = CodexSDKExecutor(
        client,
        output_collector=lambda _context, _response: (_artifact(tmp_path),),
    ).execute(context)
    assert result.outcome is ExecutionOutcome.FATAL_FAILURE
    assert result.error_code == "CODEX_USAGE_UNAVAILABLE"


def test_codex_blocks_ambiguous_client_exception(tmp_path: Path) -> None:
    class AmbiguousClient(_FakeCodexClient):
        def run(
            self,
            *,
            session: SessionHandle,
            request: CodexRequest,
            control: ExecutionControl,
        ) -> CodexResponse:
            raise TimeoutError("after dispatch")

    result = CodexSDKExecutor(
        AmbiguousClient(),
        output_collector=lambda _context, _response: (_artifact(tmp_path),),
    ).execute(_codex_context(tmp_path))
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "CODEX_SDK_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("status", ["in_progress", "interrupted"])
def test_codex_nonterminal_turn_requires_reconciliation(tmp_path: Path, status: str) -> None:
    result = CodexSDKExecutor(
        _FakeCodexClient(
            CodexResponse(
                status=status,
                external_session_id="sdk-session-1",
                usage={"total_tokens": 5},
            )
        ),
        output_collector=lambda _context, _response: (),
    ).execute(_codex_context(tmp_path))
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "CODEX_TURN_RECONCILIATION_REQUIRED"


def test_codex_rejects_prompt_tampering_during_turn(tmp_path: Path) -> None:
    context = _codex_context(tmp_path)
    prompt_path = Path(context.runtime_binding.resources[0].absolute_path)

    class TamperingClient(_FakeCodexClient):
        def run(
            self,
            *,
            session: SessionHandle,
            request: CodexRequest,
            control: ExecutionControl,
        ) -> CodexResponse:
            prompt_path.write_text("tampered during turn", encoding="utf-8")
            return super().run(session=session, request=request, control=control)

    result = CodexSDKExecutor(
        TamperingClient(),
        output_collector=lambda _context, _response: (_artifact(tmp_path),),
    ).execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "CODEX_PROMPT_BINDING_CHANGED"


def test_codex_requires_all_dependencies_to_be_injected(tmp_path: Path) -> None:
    context = _codex_context(tmp_path)
    executor = CodexSDKExecutor()
    assert executor.descriptor().available is False
    result = executor.execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "CODEX_SDK_NOT_CONFIGURED"


@pytest.mark.parametrize(
    "profile",
    [
        ExecutionProfile(model="", reasoning_effort="high", token_limit=100),
        ExecutionProfile(model="gpt-frozen", reasoning_effort="", token_limit=100),
        ExecutionProfile(model="gpt-frozen", reasoning_effort="high", token_limit=None),
    ],
)
def test_codex_requires_a_complete_frozen_execution_profile(
    tmp_path: Path,
    profile: ExecutionProfile,
) -> None:
    original = _codex_context(tmp_path)
    node = replace(original.node, execution_profile=profile)
    context = _context(
        tmp_path,
        node,
        prompt_path=Path(original.runtime_binding.resources[0].absolute_path),
        session=original.session,
    )
    client = _FakeCodexClient()
    result = CodexSDKExecutor(
        client,
        output_collector=lambda _context, _response: (_artifact(tmp_path),),
    ).execute(context)
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "CODEX_EXECUTION_PROFILE_INCOMPLETE"
    assert client.calls == []


def test_executor_modules_have_no_dynamic_loader_or_production_runtime_import() -> None:
    from unified_paper_orchestrator.workflow.executors import codex_sdk, deterministic, legacy_stage, plugin

    codex_source = inspect.getsource(codex_sdk)
    assert "openai_codex" not in codex_source
    assert "unified_paper_orchestrator_v2" not in codex_source
    assert "unified_paper_orchestrator." not in codex_source
    for module in (deterministic, plugin, legacy_stage):
        source = inspect.getsource(module)
        assert "importlib" not in source
        assert "eval(" not in source
