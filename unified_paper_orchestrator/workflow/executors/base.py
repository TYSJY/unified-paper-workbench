from __future__ import annotations

import copy
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..artifacts import BoundArtifact, ProducedArtifact
from ..contracts import (
    NodeDefinition,
    NodeRun,
    NodeRunStatus,
    NodeType,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRuntimeBinding,
)
from ..sessions import SessionHandle


class ExecutionOutcome(str, Enum):
    """Executor outcomes understood by the program-controlled scheduler."""

    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting-approval"
    WAITING_HUMAN = "waiting-human"
    BLOCKED = "blocked"
    RETRYABLE_FAILURE = "retryable-failure"
    FATAL_FAILURE = "fatal-failure"
    CANCELLED = "cancelled"


def _not_cancelled() -> bool:
    return False


def _no_heartbeat() -> None:
    return None


def _freeze_json(value: Any, path: str = "$") -> Any:
    """Defensively copy JSON-like executor data into immutable containers."""

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[]") for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if value is None or isinstance(value, (str, bool, int, float)):
        return copy.deepcopy(value)
    raise TypeError(f"{path} contains unsupported value type: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def freeze_bound_artifact(value: BoundArtifact) -> BoundArtifact:
    detached = replace(value)
    object.__setattr__(detached, "value", _freeze_json(value.value, "BoundArtifact.value"))
    object.__setattr__(detached, "metadata", _freeze_json(value.metadata, "BoundArtifact.metadata"))
    return detached


def _frozen_produced_artifact(value: ProducedArtifact) -> ProducedArtifact:
    detached = replace(value)
    object.__setattr__(detached, "metadata", _freeze_json(value.metadata, "ProducedArtifact.metadata"))
    return detached


class ExecutionControl:
    """Narrow capability surface supplied to trusted in-process executors.

    The callbacks are deliberately not public attributes. This removes the
    accidental ``callback.__self__`` escape hatch from the normal executor API.
    Python in-process adapters remain a trusted extension boundary; genuinely
    untrusted plugins require process isolation rather than language privacy.
    """

    __slots__ = ("__cancellation_probe", "__heartbeat_callback")

    def __init__(
        self,
        cancellation_probe: Callable[[], bool] = _not_cancelled,
        heartbeat_callback: Callable[[], None] = _no_heartbeat,
    ) -> None:
        if not callable(cancellation_probe):
            raise TypeError("ExecutionControl.cancellation_probe must be callable")
        if not callable(heartbeat_callback):
            raise TypeError("ExecutionControl.heartbeat_callback must be callable")
        self.__cancellation_probe = cancellation_probe
        self.__heartbeat_callback = heartbeat_callback

    def cancellation_requested(self) -> bool:
        return bool(self.__cancellation_probe())

    def heartbeat(self) -> None:
        self.__heartbeat_callback()


@dataclass(frozen=True)
class ExecutionContext:
    """Frozen run snapshots and resolved inputs supplied to a stateless executor."""

    definition: WorkflowDefinition
    runtime_binding: WorkflowRuntimeBinding
    workflow_run: WorkflowRun
    node_run: NodeRun
    node: NodeDefinition
    workspace_root: Path | str
    output_root: Path | str
    session: SessionHandle | None = None
    inputs: Mapping[str, BoundArtifact] = field(default_factory=dict)
    control: ExecutionControl = field(default_factory=ExecutionControl)

    def __post_init__(self) -> None:
        raw_root = Path(self.workspace_root)
        if not raw_root.is_absolute():
            raise ValueError("ExecutionContext.workspace_root must be absolute")
        root = Path(os.path.abspath(os.fspath(raw_root)))
        object.__setattr__(self, "workspace_root", root)
        raw_output_root = Path(self.output_root)
        if not raw_output_root.is_absolute():
            raise ValueError("ExecutionContext.output_root must be absolute")
        output_root = Path(os.path.abspath(os.fspath(raw_output_root)))
        try:
            contained = os.path.commonpath((os.path.normcase(root), os.path.normcase(output_root))) == os.path.normcase(
                root
            )
        except ValueError:
            contained = False
        if not contained or output_root == root:
            raise ValueError("ExecutionContext.output_root must be a strict workspace descendant")
        object.__setattr__(self, "output_root", output_root)
        if not isinstance(self.control, ExecutionControl):
            raise TypeError("ExecutionContext.control must be an ExecutionControl")
        if self.session is not None and not isinstance(self.session, SessionHandle):
            raise TypeError("ExecutionContext.session must be a SessionHandle or null")

        self.workflow_run.validate()
        self.node_run.validate()
        self.runtime_binding.validate(self.definition)
        definition_sha256 = self.definition.sha256()
        if self.workflow_run.definition_sha256.lower() != definition_sha256:
            raise ValueError("workflow run definition SHA256 does not match the supplied definition")
        if self.runtime_binding.definition_sha256.lower() != definition_sha256:
            raise ValueError("runtime binding does not match the supplied definition")
        if self.workflow_run.binding_sha256.lower() != self.runtime_binding.sha256():
            raise ValueError("workflow run binding SHA256 does not match the supplied binding")
        if self.node_run.workflow_run_id != self.workflow_run.run_id:
            raise ValueError("node run does not belong to the supplied workflow run")
        if self.node_run.node_id != self.node.node_id:
            raise ValueError("node run does not match the supplied node")
        if self.session is None:
            if self.node_run.session_id:
                raise ValueError("node run references a session that was not supplied")
        else:
            policy = self.node.session_policy
            for name, value in {
                "session_record_id": self.session.session_record_id,
                "run_id": self.session.run_id,
                "session_group": self.session.session_group,
                "executor": self.session.executor,
                "external_session_id": self.session.external_session_id,
                "owner_name": self.session.owner_name,
            }.items():
                if not isinstance(value, str):
                    raise TypeError(f"SessionHandle.{name} must be a string")
            if (
                not self.session.session_record_id
                or not self.session.session_group
                or not self.session.executor
                or isinstance(self.session.generation, bool)
                or not isinstance(self.session.generation, int)
                or self.session.generation <= 0
            ):
                raise ValueError("session handle identity is invalid")
            expected_group = (
                policy.session_group
                if policy.session_group
                and (policy.reuse_within_run or policy.reuse_on_retry or policy.force_new_session)
                else f"node/{self.node_run.node_run_id}"
            )
            expected_owner = (
                f"workflow/{self.workflow_run.run_id}/{self.session.session_group}/"
                f"{self.session.executor}/generation-{self.session.generation}"
            )
            if (
                self.session.session_record_id != self.node_run.session_id
                or self.session.run_id != self.workflow_run.run_id
                or self.session.session_group != expected_group
                or self.session.owner_name != expected_owner
            ):
                raise ValueError("session handle does not match the frozen run and node attempt")
        if self.workflow_run.status not in {
            WorkflowRunStatus.RUNNING,
            WorkflowRunStatus.PAUSED,
        }:
            raise ValueError("executor context requires a running or paused workflow run")
        if self.node_run.status is not NodeRunStatus.RUNNING:
            raise ValueError("executor context requires a running node attempt")
        version_node = next(
            (candidate for candidate in self.definition.nodes if candidate.node_id == self.node.node_id),
            None,
        )
        if version_node is None or version_node != self.node:
            raise ValueError("node is not the frozen node definition in the supplied workflow version")

        frozen_inputs: dict[str, BoundArtifact] = {}
        for artifact_id, artifact in self.inputs.items():
            if not isinstance(artifact_id, str):
                raise TypeError("ExecutionContext.inputs keys must be strings")
            if not isinstance(artifact, BoundArtifact):
                raise TypeError("ExecutionContext.inputs values must be BoundArtifact records")
            if artifact_id != artifact.artifact_id:
                raise ValueError("ExecutionContext input key must match artifact_id")
            frozen_inputs[artifact_id] = freeze_bound_artifact(artifact)
        specs = {spec.artifact_id: spec for spec in self.node.inputs}
        unknown_inputs = sorted(set(frozen_inputs) - set(specs))
        if unknown_inputs:
            raise ValueError(f"ExecutionContext contains undeclared inputs: {', '.join(unknown_inputs)}")
        for artifact_id, artifact in frozen_inputs.items():
            spec = specs[artifact_id]
            if spec.media_type and artifact.media_type != spec.media_type:
                raise ValueError(f"ExecutionContext input media type mismatch: {artifact_id}")
            if spec.schema_ref and artifact.schema_ref != spec.schema_ref:
                raise ValueError(f"ExecutionContext input schema mismatch: {artifact_id}")
        missing_inputs = sorted(
            spec.artifact_id
            for spec in self.node.inputs
            if spec.required and spec.artifact_id not in frozen_inputs
        )
        if missing_inputs:
            raise ValueError(f"ExecutionContext is missing required inputs: {', '.join(missing_inputs)}")
        object.__setattr__(self, "inputs", MappingProxyType(frozen_inputs))


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    message: str = ""
    error_code: str = ""
    produced_artifacts: tuple[ProducedArtifact, ...] = ()
    model_output: str = ""
    external_session_id: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", ExecutionOutcome(self.outcome))
        for name, value in {
            "message": self.message,
            "error_code": self.error_code,
            "model_output": self.model_output,
            "external_session_id": self.external_session_id,
        }.items():
            if not isinstance(value, str):
                raise TypeError(f"ExecutionResult.{name} must be a string")
        supplied_artifacts = tuple(self.produced_artifacts)
        if not all(isinstance(item, ProducedArtifact) for item in supplied_artifacts):
            raise TypeError("ExecutionResult.produced_artifacts must contain ProducedArtifact records")
        artifacts = tuple(_frozen_produced_artifact(item) for item in supplied_artifacts)
        if len({item.artifact_id for item in artifacts}) != len(artifacts):
            raise ValueError("ExecutionResult.produced_artifacts contains duplicate artifact IDs")
        if self.outcome is not ExecutionOutcome.COMPLETED and artifacts:
            raise ValueError("only completed execution results may contain produced artifacts")
        object.__setattr__(self, "produced_artifacts", artifacts)

        usage: dict[str, int] = {}
        for key, value in self.usage.items():
            if not isinstance(key, str):
                raise TypeError("ExecutionResult.usage keys must be strings")
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("ExecutionResult.usage values must be integers")
            if value < 0:
                raise ValueError("ExecutionResult.usage values cannot be negative")
            usage[key] = value
        object.__setattr__(self, "usage", MappingProxyType(usage))
        object.__setattr__(self, "metadata", _freeze_json(self.metadata, "ExecutionResult.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "message": self.message,
            "error_code": self.error_code,
            "produced_artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "path": os.fspath(artifact.path),
                    "media_type": artifact.media_type,
                    "schema_ref": artifact.schema_ref,
                    "metadata": _thaw_json(artifact.metadata),
                }
                for artifact in self.produced_artifacts
            ],
            "model_output": self.model_output,
            "external_session_id": self.external_session_id,
            "usage": dict(self.usage),
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class ExecutorDescriptor:
    executor_id: str
    node_type: NodeType
    available: bool
    implementation_refs: tuple[str, ...] = ()
    detail: str = ""
    side_effecting: bool = False
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.executor_id, str) or not self.executor_id.strip():
            raise ValueError("ExecutorDescriptor.executor_id is required")
        object.__setattr__(self, "node_type", NodeType(self.node_type))
        if not isinstance(self.available, bool):
            raise TypeError("ExecutorDescriptor.available must be a boolean")
        if not isinstance(self.detail, str):
            raise TypeError("ExecutorDescriptor.detail must be a string")
        if not isinstance(self.side_effecting, bool):
            raise TypeError("ExecutorDescriptor.side_effecting must be a boolean")
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int):
            raise TypeError("ExecutorDescriptor.max_concurrency must be an integer")
        if not 1 <= self.max_concurrency <= 64:
            raise ValueError("ExecutorDescriptor.max_concurrency must be between 1 and 64")
        refs = tuple(self.implementation_refs)
        if not all(isinstance(item, str) and item.strip() for item in refs):
            raise ValueError("ExecutorDescriptor implementation refs must be non-empty strings")
        if len(set(refs)) != len(refs):
            raise ValueError("ExecutorDescriptor implementation refs must be unique")
        object.__setattr__(self, "implementation_refs", tuple(sorted(refs)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "node_type": self.node_type.value,
            "available": self.available,
            "implementation_refs": list(self.implementation_refs),
            "detail": self.detail,
            "side_effecting": self.side_effecting,
            "max_concurrency": self.max_concurrency,
        }


@runtime_checkable
class Executor(Protocol):
    def descriptor(self) -> ExecutorDescriptor: ...

    def execute(self, context: ExecutionContext) -> ExecutionResult: ...


def cancelled_result() -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.CANCELLED,
        error_code="EXECUTION_CANCELLED",
        message="Execution was cancelled before its result was accepted.",
    )


def blocked_result(code: str, message: str, *, metadata: Mapping[str, Any] | None = None) -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.BLOCKED,
        error_code=code,
        message=message,
        metadata=metadata or {},
    )


def control_checkpoint(
    context: ExecutionContext,
    *,
    heartbeat: bool,
) -> ExecutionResult | None:
    """Run scheduler callbacks at a safe executor boundary and fail closed."""

    try:
        if context.control.cancellation_requested():
            return cancelled_result()
        if heartbeat:
            context.control.heartbeat()
    except Exception as exc:
        return ExecutionResult(
            outcome=ExecutionOutcome.RETRYABLE_FAILURE,
            error_code="EXECUTION_CONTROL_FAILED",
            message="A scheduler execution-control callback failed.",
            metadata={"error_type": type(exc).__name__},
        )
    return None


def enforce_output_contract(context: ExecutionContext, result: ExecutionResult) -> ExecutionResult:
    """Check declarations before the host independently verifies their files."""

    if result.outcome is not ExecutionOutcome.COMPLETED:
        return result
    specs = {spec.artifact_id: spec for spec in context.node.outputs}
    issues: list[str] = []
    artifacts = {artifact.artifact_id: artifact for artifact in result.produced_artifacts}
    for artifact_id in sorted(set(artifacts) - set(specs)):
        issues.append(f"undeclared output: {artifact_id}")
    for artifact_id, artifact in sorted(artifacts.items()):
        spec = specs.get(artifact_id)
        if spec is None:
            continue
        artifact_path = Path(artifact.path)
        if not artifact_path.is_absolute():
            issues.append(f"output path is not absolute: {artifact_id}")
        else:
            normalized = Path(os.path.abspath(os.fspath(artifact_path)))
            try:
                inside_output_root = os.path.commonpath(
                    (os.path.normcase(context.output_root), os.path.normcase(normalized))
                ) == os.path.normcase(context.output_root)
            except ValueError:
                inside_output_root = False
            if not inside_output_root or normalized == context.output_root:
                issues.append(f"output path is outside attempt root: {artifact_id}")
        if spec.media_type and artifact.media_type != spec.media_type:
            issues.append(f"media type mismatch: {artifact_id}")
        if spec.schema_ref and artifact.schema_ref != spec.schema_ref:
            issues.append(f"schema mismatch: {artifact_id}")
    for spec in sorted(context.node.outputs, key=lambda item: item.artifact_id):
        if spec.required and spec.artifact_id not in artifacts:
            issues.append(f"required output missing: {spec.artifact_id}")
    if not issues:
        return result
    return ExecutionResult(
        outcome=ExecutionOutcome.BLOCKED,
        error_code="OUTPUT_CONTRACT_UNSATISFIED",
        message="Executor completion was rejected because its artifact declarations do not satisfy the node contract.",
        external_session_id=result.external_session_id,
        usage=result.usage,
        metadata={"issues": issues},
    )


def run_allowlisted_handler(
    *,
    context: ExecutionContext,
    handlers: Mapping[str, Callable[[ExecutionContext], ExecutionResult]],
    missing_code: str,
    failure_code: str,
    failure_outcome: ExecutionOutcome,
) -> ExecutionResult:
    """Dispatch a logical implementation ref without eval, imports, or global lookup."""

    checkpoint = control_checkpoint(context, heartbeat=True)
    if checkpoint is not None:
        return checkpoint
    implementation_ref = context.node.implementation_ref
    handler = handlers.get(implementation_ref)
    if handler is None:
        return blocked_result(
            missing_code,
            f"No explicitly allowlisted implementation is configured for {implementation_ref!r}.",
        )
    try:
        result = handler(context)
    except Exception as exc:  # The scheduler receives a typed result, never an escaped plugin exception.
        return ExecutionResult(
            outcome=failure_outcome,
            error_code=failure_code,
            message="The injected implementation raised an exception.",
            metadata={"error_type": type(exc).__name__},
        )
    if not isinstance(result, ExecutionResult):
        return ExecutionResult(
            outcome=ExecutionOutcome.FATAL_FAILURE,
            error_code="EXECUTOR_RESULT_INVALID",
            message="The injected implementation did not return an ExecutionResult.",
        )
    checkpoint = control_checkpoint(context, heartbeat=True)
    if checkpoint is not None:
        if checkpoint.outcome is ExecutionOutcome.CANCELLED:
            return checkpoint
        return ExecutionResult(
            outcome=ExecutionOutcome.BLOCKED,
            error_code="POST_EXECUTION_CONTROL_FAILED",
            message="Execution may have produced side effects before the control checkpoint failed.",
            metadata=checkpoint.metadata,
        )
    return enforce_output_contract(context, result)


def validate_handler_mapping(
    handlers: Mapping[str, Callable[[ExecutionContext], ExecutionResult]] | None,
) -> Mapping[str, Callable[[ExecutionContext], ExecutionResult]]:
    copied: dict[str, Callable[[ExecutionContext], ExecutionResult]] = {}
    for implementation_ref, handler in (handlers or {}).items():
        if not isinstance(implementation_ref, str) or not implementation_ref.strip():
            raise ValueError("implementation refs must be non-empty strings")
        if not callable(handler):
            raise TypeError(f"handler for {implementation_ref!r} must be callable")
        copied[implementation_ref] = handler
    return MappingProxyType(copied)


def produced_artifacts_from_sequence(value: Sequence[ProducedArtifact]) -> tuple[ProducedArtifact, ...]:
    artifacts = tuple(value)
    if not all(isinstance(item, ProducedArtifact) for item in artifacts):
        raise TypeError("output collectors must return ProducedArtifact records")
    return artifacts
