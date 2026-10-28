"""Explicit, fail-closed bridge to a whole-stage legacy runtime.

This module is deliberately inert at import time.  It does not import a
production runtime, register an executor, or reproduce any of the legacy
runtime's step, retry, or session orchestration.  A composition root must opt
into ``cutover`` mode and inject a stage port before callbacks become
available.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .artifacts import BoundArtifact, ProducedArtifact
from .executors.base import (
    ExecutionContext,
    ExecutionControl,
    ExecutionOutcome,
    ExecutionResult,
    freeze_bound_artifact,
)


class LegacySideEffectState(str, Enum):
    """The legacy runtime's knowledge of durable effects from one stage call."""

    NONE = "none"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LegacyStageRequest:
    """Frozen stage-level data passed to an explicitly injected legacy port."""

    implementation_ref: str
    workflow_run_id: str
    workflow_version_id: str
    node_run_id: str
    node_id: str
    attempt: int
    workspace_root: Path | str
    output_root: Path | str
    inputs: Mapping[str, BoundArtifact] = field(default_factory=dict)
    control: ExecutionControl = field(default_factory=ExecutionControl)

    def __post_init__(self) -> None:
        for name, value in {
            "implementation_ref": self.implementation_ref,
            "workflow_run_id": self.workflow_run_id,
            "workflow_version_id": self.workflow_version_id,
            "node_run_id": self.node_run_id,
            "node_id": self.node_id,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"LegacyStageRequest.{name} is required")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt <= 0:
            raise ValueError("LegacyStageRequest.attempt must be a positive integer")
        if not isinstance(self.control, ExecutionControl):
            raise TypeError("LegacyStageRequest.control must be an ExecutionControl")

        workspace_root = _absolute_path(self.workspace_root, "workspace_root")
        output_root = _absolute_path(self.output_root, "output_root")
        try:
            inside_workspace = os.path.commonpath(
                (os.path.normcase(workspace_root), os.path.normcase(output_root))
            ) == os.path.normcase(workspace_root)
        except ValueError:
            inside_workspace = False
        if not inside_workspace or output_root == workspace_root:
            raise ValueError("LegacyStageRequest.output_root must be a strict workspace descendant")
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "output_root", output_root)

        frozen_inputs: dict[str, BoundArtifact] = {}
        for artifact_id, artifact in self.inputs.items():
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("LegacyStageRequest.inputs keys must be non-empty strings")
            if not isinstance(artifact, BoundArtifact):
                raise TypeError("LegacyStageRequest.inputs values must be BoundArtifact records")
            if artifact_id != artifact.artifact_id:
                raise ValueError("LegacyStageRequest input key must match artifact_id")
            frozen_inputs[artifact_id] = freeze_bound_artifact(artifact)
        object.__setattr__(self, "inputs", MappingProxyType(frozen_inputs))


@dataclass(frozen=True)
class LegacyStageResult:
    """Result reported by the old runtime before conservative translation."""

    outcome: ExecutionOutcome
    side_effects: LegacySideEffectState = LegacySideEffectState.UNKNOWN
    message: str = ""
    error_code: str = ""
    produced_artifacts: tuple[ProducedArtifact, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "side_effects", LegacySideEffectState(self.side_effects))
        validated = ExecutionResult(
            outcome=self.outcome,
            message=self.message,
            error_code=self.error_code,
            produced_artifacts=tuple(self.produced_artifacts),
            metadata=self.metadata,
        )
        object.__setattr__(self, "outcome", validated.outcome)
        object.__setattr__(self, "message", validated.message)
        object.__setattr__(self, "error_code", validated.error_code)
        object.__setattr__(self, "produced_artifacts", validated.produced_artifacts)
        object.__setattr__(self, "metadata", validated.metadata)


@runtime_checkable
class LegacyRuntimePort(Protocol):
    """One whole-stage call; all internal orchestration remains legacy-owned."""

    def run_stage(self, request: LegacyStageRequest) -> LegacyStageResult: ...


LegacyStageCallback = Callable[[ExecutionContext], ExecutionResult]


def build_legacy_stage_callbacks(
    runtimes: Mapping[str, LegacyRuntimePort] | None = None,
    *,
    mode: str = "disabled",
) -> Mapping[str, LegacyStageCallback]:
    """Build inert-by-default callbacks for :class:`LegacyStageExecutor`.

    ``shadow`` is rejected because a real legacy stage is side-effecting and
    therefore cannot be used as a read-only preview.  ``cutover`` requires an
    explicit implementation-ref-to-port mapping; there is no import fallback.
    """

    if not isinstance(mode, str):
        raise TypeError("legacy compatibility mode must be a string")
    normalized_mode = mode.strip().lower()
    if normalized_mode == "disabled":
        return MappingProxyType({})
    if normalized_mode == "shadow":
        raise ValueError("legacy stage callbacks cannot run in shadow mode")
    if normalized_mode != "cutover":
        raise ValueError(f"unsupported legacy compatibility mode: {mode!r}")
    if not runtimes:
        raise ValueError("cutover mode requires explicitly injected legacy runtime ports")

    callbacks: dict[str, LegacyStageCallback] = {}
    for implementation_ref, runtime in runtimes.items():
        if not isinstance(implementation_ref, str) or not implementation_ref.strip():
            raise ValueError("legacy runtime implementation refs must be non-empty strings")
        if not isinstance(runtime, LegacyRuntimePort):
            raise TypeError(f"legacy runtime for {implementation_ref!r} must implement run_stage")
        callbacks[implementation_ref] = _callback_for(runtime)
    return MappingProxyType(callbacks)


def _absolute_path(value: Path | str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"LegacyStageRequest.{name} must be absolute")
    return Path(os.path.abspath(os.fspath(path)))


def _callback_for(runtime: LegacyRuntimePort) -> LegacyStageCallback:
    def invoke(context: ExecutionContext) -> ExecutionResult:
        request = LegacyStageRequest(
            implementation_ref=context.node.implementation_ref,
            workflow_run_id=context.workflow_run.run_id,
            workflow_version_id=context.workflow_run.workflow_version_id,
            node_run_id=context.node_run.node_run_id,
            node_id=context.node.node_id,
            attempt=context.node_run.attempt,
            workspace_root=context.workspace_root,
            output_root=context.output_root,
            inputs=context.inputs,
            control=context.control,
        )
        try:
            legacy_result = runtime.run_stage(request)
        except Exception as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="LEGACY_STAGE_INVOCATION_UNCERTAIN",
                message="The legacy stage raised after invocation; durable side effects are unknown.",
                metadata={
                    "error_type": type(exc).__name__,
                    "legacy_side_effects": LegacySideEffectState.UNKNOWN.value,
                },
            )
        if not isinstance(legacy_result, LegacyStageResult):
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="LEGACY_STAGE_RESULT_INVALID",
                message="The legacy runtime returned an invalid result; durable side effects are unknown.",
                metadata={"legacy_side_effects": LegacySideEffectState.UNKNOWN.value},
            )
        return _execution_result(legacy_result)

    return invoke


def _execution_result(legacy: LegacyStageResult) -> ExecutionResult:
    audit_metadata = dict(legacy.metadata)
    audit_metadata.update(
        {
            "legacy_reported_outcome": legacy.outcome.value,
            "legacy_side_effects": legacy.side_effects.value,
        }
    )
    if legacy.side_effects is LegacySideEffectState.UNKNOWN:
        return ExecutionResult(
            outcome=ExecutionOutcome.BLOCKED,
            error_code="LEGACY_STAGE_SIDE_EFFECTS_UNKNOWN",
            message="The legacy result was rejected because durable side effects are unknown.",
            metadata=audit_metadata,
        )
    if legacy.outcome is ExecutionOutcome.RETRYABLE_FAILURE:
        return ExecutionResult(
            outcome=ExecutionOutcome.BLOCKED,
            error_code="LEGACY_STAGE_RETRY_OWNED_BY_RUNTIME",
            message="The legacy runtime owns stage retries; the generic scheduler must not invoke it again.",
            metadata=audit_metadata,
        )
    if legacy.outcome is ExecutionOutcome.CANCELLED and legacy.side_effects is not LegacySideEffectState.NONE:
        return ExecutionResult(
            outcome=ExecutionOutcome.BLOCKED,
            error_code="LEGACY_STAGE_CANCELLED_AFTER_EFFECTS",
            message="Legacy cancellation was not propagated because the stage reported committed effects.",
            metadata=audit_metadata,
        )
    return ExecutionResult(
        outcome=legacy.outcome,
        message=legacy.message,
        error_code=legacy.error_code,
        produced_artifacts=legacy.produced_artifacts,
        metadata=audit_metadata,
    )


__all__ = [
    "LegacyRuntimePort",
    "LegacySideEffectState",
    "LegacyStageCallback",
    "LegacyStageRequest",
    "LegacyStageResult",
    "build_legacy_stage_callbacks",
]
