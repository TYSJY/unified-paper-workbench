from __future__ import annotations

from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.contracts import (
    NodeDefinition,
    NodeRun,
    NodeRunStatus,
    NodeType,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRuntimeBinding,
)
from unified_paper_orchestrator.workflow.executors import (
    ExecutionContext,
    ExecutionControl,
    ExecutionOutcome,
    LegacyStageExecutor,
)
from unified_paper_orchestrator.workflow.legacy_compat import (
    LegacySideEffectState,
    LegacyStageRequest,
    LegacyStageResult,
    build_legacy_stage_callbacks,
)


NOW = "2026-08-05T00:00:00+00:00"
IMPLEMENTATION_REF = "legacy.writing-stage"


class RecordingRuntime:
    def __init__(self, result: LegacyStageResult) -> None:
        self.result = result
        self.requests: list[LegacyStageRequest] = []

    def run_stage(self, request: LegacyStageRequest) -> LegacyStageResult:
        self.requests.append(request)
        return self.result


def _context(tmp_path: Path, *, cancelled: bool = False) -> ExecutionContext:
    node = NodeDefinition(
        node_id="writing-stage",
        name="Writing stage",
        node_type=NodeType.LEGACY_STAGE,
        display_order=10,
        implementation_ref=IMPLEMENTATION_REF,
    )
    definition = WorkflowDefinition(
        workflow_id="tests.legacy-compat",
        name="Legacy compatibility test",
        nodes=(node,),
        edges=(),
    )
    definition.validate()
    binding = WorkflowRuntimeBinding(
        definition_sha256=definition.sha256(),
        resources=(),
    )
    binding.validate(definition)
    workflow_run = WorkflowRun(
        workflow_version_id="tests.legacy-compat.v1",
        definition_sha256=definition.sha256(),
        binding_sha256=binding.sha256(),
        run_id="workflow-run-legacy",
        status=WorkflowRunStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
    )
    node_run = NodeRun(
        workflow_run_id=workflow_run.run_id,
        node_id=node.node_id,
        node_run_id="node-run-legacy",
        status=NodeRunStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
    )
    output_root = (tmp_path / "legacy-output").resolve()
    output_root.mkdir(exist_ok=True)
    return ExecutionContext(
        definition=definition,
        runtime_binding=binding,
        workflow_run=workflow_run,
        node_run=node_run,
        node=node,
        workspace_root=tmp_path.resolve(),
        output_root=output_root,
        control=ExecutionControl(cancellation_probe=lambda: cancelled),
    )


def _completed_runtime() -> RecordingRuntime:
    return RecordingRuntime(
        LegacyStageResult(
            outcome=ExecutionOutcome.COMPLETED,
            side_effects=LegacySideEffectState.COMMITTED,
            metadata={"legacy_receipt": "receipt-1"},
        )
    )


def test_descriptor_stays_unconfigured_when_compatibility_is_disabled(tmp_path: Path) -> None:
    runtime = _completed_runtime()
    callbacks = build_legacy_stage_callbacks({IMPLEMENTATION_REF: runtime})
    executor = LegacyStageExecutor(callbacks)

    assert callbacks == {}
    assert executor.descriptor().available is False
    result = executor.execute(_context(tmp_path))
    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error_code == "LEGACY_STAGE_CALLBACK_NOT_CONFIGURED"
    assert runtime.requests == []


def test_shadow_mode_refuses_a_side_effecting_legacy_stage() -> None:
    with pytest.raises(ValueError, match="cannot run in shadow mode"):
        build_legacy_stage_callbacks({IMPLEMENTATION_REF: _completed_runtime()}, mode="shadow")


def test_cutover_requires_explicit_runtime_injection() -> None:
    with pytest.raises(ValueError, match="explicitly injected"):
        build_legacy_stage_callbacks(mode="cutover")


def test_cutover_calls_only_the_injected_whole_stage_port(tmp_path: Path) -> None:
    runtime = _completed_runtime()
    callbacks = build_legacy_stage_callbacks(
        {IMPLEMENTATION_REF: runtime},
        mode="cutover",
    )
    executor = LegacyStageExecutor(callbacks)

    result = executor.execute(_context(tmp_path))

    assert executor.descriptor().available is True
    assert result.outcome is ExecutionOutcome.COMPLETED
    assert result.metadata["legacy_receipt"] == "receipt-1"
    assert result.metadata["legacy_side_effects"] == "committed"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.implementation_ref == IMPLEMENTATION_REF
    assert request.workflow_run_id == "workflow-run-legacy"
    assert request.node_run_id == "node-run-legacy"
    assert request.attempt == 1
    assert not hasattr(request, "step")
    assert not hasattr(request, "retry_policy")
    assert not hasattr(request, "session")


def test_unknown_side_effects_and_legacy_retry_fail_closed(tmp_path: Path) -> None:
    unknown = RecordingRuntime(LegacyStageResult(outcome=ExecutionOutcome.COMPLETED))
    unknown_result = LegacyStageExecutor(
        build_legacy_stage_callbacks({IMPLEMENTATION_REF: unknown}, mode="cutover")
    ).execute(_context(tmp_path))
    assert unknown_result.outcome is ExecutionOutcome.BLOCKED
    assert unknown_result.error_code == "LEGACY_STAGE_SIDE_EFFECTS_UNKNOWN"

    retryable = RecordingRuntime(
        LegacyStageResult(
            outcome=ExecutionOutcome.RETRYABLE_FAILURE,
            side_effects=LegacySideEffectState.NONE,
        )
    )
    retry_result = LegacyStageExecutor(
        build_legacy_stage_callbacks({IMPLEMENTATION_REF: retryable}, mode="cutover")
    ).execute(_context(tmp_path))
    assert retry_result.outcome is ExecutionOutcome.BLOCKED
    assert retry_result.error_code == "LEGACY_STAGE_RETRY_OWNED_BY_RUNTIME"
    assert len(retryable.requests) == 1


def test_cancellation_before_dispatch_does_not_call_legacy_runtime(tmp_path: Path) -> None:
    runtime = _completed_runtime()
    executor = LegacyStageExecutor(
        build_legacy_stage_callbacks({IMPLEMENTATION_REF: runtime}, mode="cutover")
    )

    result = executor.execute(_context(tmp_path, cancelled=True))

    assert result.outcome is ExecutionOutcome.CANCELLED
    assert runtime.requests == []
