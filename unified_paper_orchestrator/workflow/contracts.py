from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping


WORKFLOW_DEFINITION_SCHEMA = "unified-paper-orchestrator/workflow-definition/v1"
WORKFLOW_VERSION_SCHEMA = "unified-paper-orchestrator/workflow-version/v1"
WORKFLOW_RUNTIME_BINDING_SCHEMA = "unified-paper-orchestrator/workflow-runtime-binding/v1"
WORKFLOW_RUN_SCHEMA = "unified-paper-orchestrator/workflow-run/v1"
NODE_RUN_SCHEMA = "unified-paper-orchestrator/node-run/v1"
CONDITION_LANGUAGE = "json-logic/v1"
MAX_LOOP_TRAVERSALS = 10_000


class FrozenJSONMapping(Mapping[str, Any]):
    """A recursively immutable JSON object used inside frozen snapshots."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        for key in (values or {}):
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
        self._values = {
            key: _freeze_json(item, f".{key}")
            for key, item in (values or {}).items()
        }

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenJSONMapping({self._values!r})"


def _freeze_json(value: Any, path: str = "$") -> Any:
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"JSON object key must be a string at {path}")
        return FrozenJSONMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[]") for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"JSON number must be finite at {path}")
        return value
    raise TypeError(f"unsupported JSON value at {path}: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = _thaw_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _expect_exact(data: Mapping[str, Any], fields: set[str], contract: str) -> None:
    if not isinstance(data, Mapping):
        raise TypeError(f"{contract} must be a JSON object")
    actual = set(data)
    missing = sorted(fields - actual)
    unknown = sorted(actual - fields)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise ValueError(f"invalid {contract}: {'; '.join(details)}")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{path} must be a string")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{path} must be a boolean")
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{path} must be an integer")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result


def _sequence(value: Any, path: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{path} must be an array")
    return tuple(value)


def _json_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    return FrozenJSONMapping(value)


def _validate_timestamp(value: str, path: str, *, required: bool = False) -> None:
    if not value:
        if required:
            raise ValueError(f"{path} is required")
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{path} must include a timezone")


class NodeType(str, Enum):
    CODEX = "codex"
    DETERMINISTIC = "deterministic"
    HUMAN = "human"
    PLUGIN = "plugin"
    SUBWORKFLOW = "subworkflow"
    LEGACY_STAGE = "legacy-stage"


class EdgeType(str, Enum):
    SUCCESS = "success"
    CONDITION = "condition"
    LOOP = "loop"
    ERROR = "error"


class ArtifactSourceKind(str, Enum):
    WORKFLOW_INPUT = "workflow-input"
    NODE_OUTPUT = "node-output"


class ValidationMode(str, Enum):
    LINEAR = "linear"
    GRAPH = "graph"


class JoinPolicy(str, Enum):
    ALL = "all"
    ANY = "any"


class WorkflowVersionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class WorkflowRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting-approval"
    WAITING_HUMAN = "waiting-human"
    BLOCKED = "blocked"
    RETRYABLE_FAILURE = "retryable-failure"
    FATAL_FAILURE = "fatal-failure"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class NodeRunStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting-approval"
    WAITING_HUMAN = "waiting-human"
    BLOCKED = "blocked"
    RETRY_WAIT = "retry-wait"
    RETRYABLE_FAILURE = "retryable-failure"
    FATAL_FAILURE = "fatal-failure"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    """Return the one JSON representation used by workflow fingerprints."""

    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_sha256(value: str) -> bool:
    digest = str(value or "").strip().lower()
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def looks_like_absolute_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if lowered.startswith(("file:", "~", "${", "%")):
        return True
    return (
        Path(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or PurePosixPath(text).is_absolute()
    )


def _mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return _thaw_json(value or {})


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    media_type: str = "application/octet-stream"
    schema_ref: str = ""
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        _text(self.artifact_id, "ArtifactSpec.artifact_id")
        _text(self.media_type, "ArtifactSpec.media_type")
        _text(self.schema_ref, "ArtifactSpec.schema_ref")
        _text(self.description, "ArtifactSpec.description")
        if not isinstance(self.required, bool):
            raise TypeError("ArtifactSpec.required must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
            "required": self.required,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactSpec":
        fields = {"artifact_id", "media_type", "schema_ref", "required", "description"}
        _expect_exact(data, fields, "ArtifactSpec")
        return cls(
            artifact_id=_text(data["artifact_id"], "ArtifactSpec.artifact_id"),
            media_type=_text(data["media_type"], "ArtifactSpec.media_type"),
            schema_ref=_text(data["schema_ref"], "ArtifactSpec.schema_ref"),
            required=_boolean(data["required"], "ArtifactSpec.required"),
            description=_text(data["description"], "ArtifactSpec.description"),
        )


@dataclass(frozen=True)
class ArtifactBinding:
    target_artifact_id: str
    source_kind: ArtifactSourceKind
    source_artifact_id: str
    source_node_id: str = ""

    def __post_init__(self) -> None:
        _text(self.target_artifact_id, "ArtifactBinding.target_artifact_id")
        _text(self.source_artifact_id, "ArtifactBinding.source_artifact_id")
        _text(self.source_node_id, "ArtifactBinding.source_node_id")
        object.__setattr__(self, "source_kind", ArtifactSourceKind(self.source_kind))

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_artifact_id": self.target_artifact_id,
            "source_kind": self.source_kind.value,
            "source_artifact_id": self.source_artifact_id,
            "source_node_id": self.source_node_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactBinding":
        fields = {"target_artifact_id", "source_kind", "source_artifact_id", "source_node_id"}
        _expect_exact(data, fields, "ArtifactBinding")
        return cls(
            target_artifact_id=_text(
                data["target_artifact_id"],
                "ArtifactBinding.target_artifact_id",
            ),
            source_kind=ArtifactSourceKind(_text(data["source_kind"], "ArtifactBinding.source_kind")),
            source_artifact_id=_text(data["source_artifact_id"], "ArtifactBinding.source_artifact_id"),
            source_node_id=_text(data["source_node_id"], "ArtifactBinding.source_node_id"),
        )


@dataclass(frozen=True)
class PromptRef:
    resource_id: str
    content_sha256: str
    version: str = ""

    def __post_init__(self) -> None:
        _text(self.resource_id, "PromptRef.resource_id")
        _text(self.content_sha256, "PromptRef.content_sha256")
        _text(self.version, "PromptRef.version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "content_sha256": self.content_sha256.lower(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PromptRef":
        fields = {"resource_id", "content_sha256", "version"}
        _expect_exact(data, fields, "PromptRef")
        return cls(
            resource_id=_text(data["resource_id"], "PromptRef.resource_id"),
            content_sha256=_text(data["content_sha256"], "PromptRef.content_sha256").lower(),
            version=_text(data["version"], "PromptRef.version"),
        )


@dataclass(frozen=True)
class ResourceBinding:
    resource_id: str
    absolute_path: str
    content_sha256: str

    def __post_init__(self) -> None:
        _text(self.resource_id, "ResourceBinding.resource_id")
        _text(self.absolute_path, "ResourceBinding.absolute_path")
        _text(self.content_sha256, "ResourceBinding.content_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "absolute_path": self.absolute_path,
            "content_sha256": self.content_sha256.lower(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceBinding":
        fields = {"resource_id", "absolute_path", "content_sha256"}
        _expect_exact(data, fields, "ResourceBinding")
        return cls(
            resource_id=_text(data["resource_id"], "ResourceBinding.resource_id"),
            absolute_path=_text(data["absolute_path"], "ResourceBinding.absolute_path"),
            content_sha256=_text(data["content_sha256"], "ResourceBinding.content_sha256").lower(),
        )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    initial_delay_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 0.0
    retryable_error_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", _integer(self.max_attempts, "RetryPolicy.max_attempts"))
        object.__setattr__(
            self,
            "initial_delay_seconds",
            _number(self.initial_delay_seconds, "RetryPolicy.initial_delay_seconds"),
        )
        object.__setattr__(
            self,
            "backoff_multiplier",
            _number(self.backoff_multiplier, "RetryPolicy.backoff_multiplier"),
        )
        object.__setattr__(
            self,
            "max_delay_seconds",
            _number(self.max_delay_seconds, "RetryPolicy.max_delay_seconds"),
        )
        codes = _sequence(self.retryable_error_codes, "RetryPolicy.retryable_error_codes")
        object.__setattr__(
            self,
            "retryable_error_codes",
            tuple(_text(item, "RetryPolicy.retryable_error_codes[]") for item in codes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "initial_delay_seconds": self.initial_delay_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "max_delay_seconds": self.max_delay_seconds,
            "retryable_error_codes": sorted(self.retryable_error_codes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RetryPolicy":
        if data is None:
            raise TypeError("RetryPolicy must be a JSON object")
        fields = {
            "max_attempts",
            "initial_delay_seconds",
            "backoff_multiplier",
            "max_delay_seconds",
            "retryable_error_codes",
        }
        _expect_exact(data, fields, "RetryPolicy")
        return cls(
            max_attempts=_integer(data["max_attempts"], "RetryPolicy.max_attempts"),
            initial_delay_seconds=_number(data["initial_delay_seconds"], "RetryPolicy.initial_delay_seconds"),
            backoff_multiplier=_number(data["backoff_multiplier"], "RetryPolicy.backoff_multiplier"),
            max_delay_seconds=_number(data["max_delay_seconds"], "RetryPolicy.max_delay_seconds"),
            retryable_error_codes=tuple(
                _text(item, "RetryPolicy.retryable_error_codes[]")
                for item in _sequence(data["retryable_error_codes"], "RetryPolicy.retryable_error_codes")
            ),
        )


@dataclass(frozen=True)
class SessionPolicy:
    session_group: str = ""
    reuse_within_run: bool = False
    reuse_on_retry: bool = False
    force_new_session: bool = False

    def __post_init__(self) -> None:
        _text(self.session_group, "SessionPolicy.session_group")
        _boolean(self.reuse_within_run, "SessionPolicy.reuse_within_run")
        _boolean(self.reuse_on_retry, "SessionPolicy.reuse_on_retry")
        _boolean(self.force_new_session, "SessionPolicy.force_new_session")

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_group": self.session_group,
            "reuse_within_run": self.reuse_within_run,
            "reuse_on_retry": self.reuse_on_retry,
            "force_new_session": self.force_new_session,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SessionPolicy":
        if data is None:
            raise TypeError("SessionPolicy must be a JSON object")
        fields = {"session_group", "reuse_within_run", "reuse_on_retry", "force_new_session"}
        _expect_exact(data, fields, "SessionPolicy")
        return cls(
            session_group=_text(data["session_group"], "SessionPolicy.session_group"),
            reuse_within_run=_boolean(data["reuse_within_run"], "SessionPolicy.reuse_within_run"),
            reuse_on_retry=_boolean(data["reuse_on_retry"], "SessionPolicy.reuse_on_retry"),
            force_new_session=_boolean(data["force_new_session"], "SessionPolicy.force_new_session"),
        )


@dataclass(frozen=True)
class ExecutionProfile:
    model: str = ""
    reasoning_effort: str = ""
    token_limit: int | None = None
    timeout_seconds: int = 1800
    allowed_tools: tuple[str, ...] = ()
    network_access: bool = False
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.model, "ExecutionProfile.model")
        _text(self.reasoning_effort, "ExecutionProfile.reasoning_effort")
        if self.token_limit is not None:
            object.__setattr__(self, "token_limit", _integer(self.token_limit, "ExecutionProfile.token_limit"))
        object.__setattr__(self, "timeout_seconds", _integer(self.timeout_seconds, "ExecutionProfile.timeout_seconds"))
        tools = _sequence(self.allowed_tools, "ExecutionProfile.allowed_tools")
        object.__setattr__(
            self,
            "allowed_tools",
            tuple(_text(item, "ExecutionProfile.allowed_tools[]") for item in tools),
        )
        _boolean(self.network_access, "ExecutionProfile.network_access")
        object.__setattr__(self, "parameters", _json_mapping(self.parameters, "ExecutionProfile.parameters"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "token_limit": self.token_limit,
            "timeout_seconds": self.timeout_seconds,
            "allowed_tools": sorted(self.allowed_tools),
            "network_access": self.network_access,
            "parameters": _mapping(self.parameters),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ExecutionProfile":
        if data is None:
            raise TypeError("ExecutionProfile must be a JSON object")
        fields = {
            "model",
            "reasoning_effort",
            "token_limit",
            "timeout_seconds",
            "allowed_tools",
            "network_access",
            "parameters",
        }
        _expect_exact(data, fields, "ExecutionProfile")
        token_limit = data["token_limit"]
        return cls(
            model=_text(data["model"], "ExecutionProfile.model"),
            reasoning_effort=_text(data["reasoning_effort"], "ExecutionProfile.reasoning_effort"),
            token_limit=_integer(token_limit, "ExecutionProfile.token_limit") if token_limit is not None else None,
            timeout_seconds=_integer(data["timeout_seconds"], "ExecutionProfile.timeout_seconds"),
            allowed_tools=tuple(
                _text(item, "ExecutionProfile.allowed_tools[]")
                for item in _sequence(data["allowed_tools"], "ExecutionProfile.allowed_tools")
            ),
            network_access=_boolean(data["network_access"], "ExecutionProfile.network_access"),
            parameters=_json_mapping(data["parameters"], "ExecutionProfile.parameters"),
        )


@dataclass(frozen=True)
class NodeDefinition:
    node_id: str
    name: str
    node_type: NodeType
    display_order: int
    prompt_ref: PromptRef | None = None
    execution_profile: ExecutionProfile = field(default_factory=ExecutionProfile)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    session_policy: SessionPolicy = field(default_factory=SessionPolicy)
    inputs: tuple[ArtifactSpec, ...] = ()
    input_bindings: tuple[ArtifactBinding, ...] = ()
    outputs: tuple[ArtifactSpec, ...] = ()
    join_policy: JoinPolicy = JoinPolicy.ALL
    implementation_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.node_id, "NodeDefinition.node_id")
        _text(self.name, "NodeDefinition.name")
        _text(self.implementation_ref, "NodeDefinition.implementation_ref")
        object.__setattr__(self, "node_type", NodeType(self.node_type))
        object.__setattr__(self, "display_order", _integer(self.display_order, "NodeDefinition.display_order"))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "input_bindings", tuple(self.input_bindings))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "join_policy", JoinPolicy(self.join_policy))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "NodeDefinition.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type.value,
            "display_order": self.display_order,
            "prompt_ref": self.prompt_ref.to_dict() if self.prompt_ref else None,
            "execution_profile": self.execution_profile.to_dict(),
            "retry_policy": self.retry_policy.to_dict(),
            "session_policy": self.session_policy.to_dict(),
            "inputs": [item.to_dict() for item in sorted(self.inputs, key=lambda spec: spec.artifact_id)],
            "input_bindings": [
                item.to_dict()
                for item in sorted(self.input_bindings, key=lambda binding: binding.target_artifact_id)
            ],
            "outputs": [item.to_dict() for item in sorted(self.outputs, key=lambda spec: spec.artifact_id)],
            "join_policy": self.join_policy.value,
            "implementation_ref": self.implementation_ref,
            "metadata": _mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NodeDefinition":
        fields = {
            "node_id",
            "name",
            "node_type",
            "display_order",
            "prompt_ref",
            "execution_profile",
            "retry_policy",
            "session_policy",
            "inputs",
            "input_bindings",
            "outputs",
            "join_policy",
            "implementation_ref",
            "metadata",
        }
        _expect_exact(data, fields, "NodeDefinition")
        prompt_data = data["prompt_ref"]
        if prompt_data is not None and not isinstance(prompt_data, Mapping):
            raise TypeError("NodeDefinition.prompt_ref must be an object or null")
        return cls(
            node_id=_text(data["node_id"], "NodeDefinition.node_id"),
            name=_text(data["name"], "NodeDefinition.name"),
            node_type=NodeType(_text(data["node_type"], "NodeDefinition.node_type")),
            display_order=_integer(data["display_order"], "NodeDefinition.display_order"),
            prompt_ref=PromptRef.from_dict(prompt_data) if isinstance(prompt_data, Mapping) else None,
            execution_profile=ExecutionProfile.from_dict(data["execution_profile"]),
            retry_policy=RetryPolicy.from_dict(data["retry_policy"]),
            session_policy=SessionPolicy.from_dict(data["session_policy"]),
            inputs=tuple(
                ArtifactSpec.from_dict(item)
                for item in _sequence(data["inputs"], "NodeDefinition.inputs")
            ),
            input_bindings=tuple(
                ArtifactBinding.from_dict(item)
                for item in _sequence(data["input_bindings"], "NodeDefinition.input_bindings")
            ),
            outputs=tuple(
                ArtifactSpec.from_dict(item)
                for item in _sequence(data["outputs"], "NodeDefinition.outputs")
            ),
            join_policy=JoinPolicy(_text(data["join_policy"], "NodeDefinition.join_policy")),
            implementation_ref=_text(data["implementation_ref"], "NodeDefinition.implementation_ref"),
            metadata=_json_mapping(data["metadata"], "NodeDefinition.metadata"),
        )


@dataclass(frozen=True)
class EdgeDefinition:
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: EdgeType = EdgeType.SUCCESS
    condition: Mapping[str, Any] | None = None
    condition_language: str = ""
    max_traversals: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.edge_id, "EdgeDefinition.edge_id")
        _text(self.source_node_id, "EdgeDefinition.source_node_id")
        _text(self.target_node_id, "EdgeDefinition.target_node_id")
        _text(self.condition_language, "EdgeDefinition.condition_language")
        object.__setattr__(self, "edge_type", EdgeType(self.edge_type))
        if self.condition is not None:
            object.__setattr__(self, "condition", _json_mapping(self.condition, "EdgeDefinition.condition"))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "EdgeDefinition.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "edge_type": self.edge_type.value,
            "condition": _mapping(self.condition) if self.condition is not None else None,
            "condition_language": self.condition_language,
            "max_traversals": self.max_traversals,
            "metadata": _mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EdgeDefinition":
        fields = {
            "edge_id",
            "source_node_id",
            "target_node_id",
            "edge_type",
            "condition",
            "condition_language",
            "max_traversals",
            "metadata",
        }
        _expect_exact(data, fields, "EdgeDefinition")
        maximum = data["max_traversals"]
        condition = data["condition"]
        return cls(
            edge_id=_text(data["edge_id"], "EdgeDefinition.edge_id"),
            source_node_id=_text(data["source_node_id"], "EdgeDefinition.source_node_id"),
            target_node_id=_text(data["target_node_id"], "EdgeDefinition.target_node_id"),
            edge_type=EdgeType(_text(data["edge_type"], "EdgeDefinition.edge_type")),
            condition=_json_mapping(condition, "EdgeDefinition.condition") if condition is not None else None,
            condition_language=_text(data["condition_language"], "EdgeDefinition.condition_language"),
            max_traversals=_integer(maximum, "EdgeDefinition.max_traversals") if maximum is not None else None,
            metadata=_json_mapping(data["metadata"], "EdgeDefinition.metadata"),
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    name: str
    nodes: tuple[NodeDefinition, ...]
    edges: tuple[EdgeDefinition, ...]
    inputs: tuple[ArtifactSpec, ...] = ()
    outputs: tuple[ArtifactSpec, ...] = ()
    output_bindings: tuple[ArtifactBinding, ...] = ()
    description: str = ""
    schema: str = WORKFLOW_DEFINITION_SCHEMA
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.workflow_id, "WorkflowDefinition.workflow_id")
        _text(self.name, "WorkflowDefinition.name")
        _text(self.description, "WorkflowDefinition.description")
        _text(self.schema, "WorkflowDefinition.schema")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "output_bindings", tuple(self.output_bindings))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "WorkflowDefinition.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "inputs": [item.to_dict() for item in sorted(self.inputs, key=lambda spec: spec.artifact_id)],
            "outputs": [item.to_dict() for item in sorted(self.outputs, key=lambda spec: spec.artifact_id)],
            "output_bindings": [
                item.to_dict()
                for item in sorted(self.output_bindings, key=lambda binding: binding.target_artifact_id)
            ],
            "nodes": [
                item.to_dict()
                for item in sorted(self.nodes, key=lambda node: (node.display_order, node.node_id))
            ],
            "edges": [item.to_dict() for item in sorted(self.edges, key=lambda edge: edge.edge_id)],
            "metadata": _mapping(self.metadata),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def validate(self, mode: ValidationMode = ValidationMode.LINEAR) -> None:
        from .validation import validate_workflow_definition

        validate_workflow_definition(self, mode=mode)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowDefinition":
        fields = {
            "schema",
            "workflow_id",
            "name",
            "description",
            "inputs",
            "outputs",
            "output_bindings",
            "nodes",
            "edges",
            "metadata",
        }
        _expect_exact(data, fields, "WorkflowDefinition")
        value = cls(
            workflow_id=_text(data["workflow_id"], "WorkflowDefinition.workflow_id"),
            name=_text(data["name"], "WorkflowDefinition.name"),
            description=_text(data["description"], "WorkflowDefinition.description"),
            inputs=tuple(
                ArtifactSpec.from_dict(item)
                for item in _sequence(data["inputs"], "WorkflowDefinition.inputs")
            ),
            outputs=tuple(
                ArtifactSpec.from_dict(item)
                for item in _sequence(data["outputs"], "WorkflowDefinition.outputs")
            ),
            output_bindings=tuple(
                ArtifactBinding.from_dict(item)
                for item in _sequence(data["output_bindings"], "WorkflowDefinition.output_bindings")
            ),
            nodes=tuple(
                NodeDefinition.from_dict(item)
                for item in _sequence(data["nodes"], "WorkflowDefinition.nodes")
            ),
            edges=tuple(
                EdgeDefinition.from_dict(item)
                for item in _sequence(data["edges"], "WorkflowDefinition.edges")
            ),
            schema=_text(data["schema"], "WorkflowDefinition.schema"),
            metadata=_json_mapping(data["metadata"], "WorkflowDefinition.metadata"),
        )
        value.validate(mode=ValidationMode.GRAPH)
        return value


@dataclass(frozen=True)
class WorkflowVersion:
    version_id: str
    workflow_id: str
    version: int
    definition_sha256: str
    definition: WorkflowDefinition
    status: WorkflowVersionStatus = WorkflowVersionStatus.DRAFT
    created_at: str = ""
    schema: str = WORKFLOW_VERSION_SCHEMA

    def __post_init__(self) -> None:
        _text(self.version_id, "WorkflowVersion.version_id")
        _text(self.workflow_id, "WorkflowVersion.workflow_id")
        object.__setattr__(self, "version", _integer(self.version, "WorkflowVersion.version"))
        _text(self.definition_sha256, "WorkflowVersion.definition_sha256")
        object.__setattr__(self, "status", WorkflowVersionStatus(self.status))
        _text(self.created_at, "WorkflowVersion.created_at")
        _text(self.schema, "WorkflowVersion.schema")

    def to_dict(self) -> dict[str, Any]:
        self.validate(mode=ValidationMode.GRAPH)
        return {
            "schema": self.schema,
            "version_id": self.version_id,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "definition_sha256": self.definition_sha256.lower(),
            "definition": self.definition.to_dict(),
            "status": self.status.value,
            "created_at": self.created_at,
        }

    def validate(self, mode: ValidationMode = ValidationMode.LINEAR) -> None:
        self.definition.validate(mode=mode)
        if self.schema != WORKFLOW_VERSION_SCHEMA:
            raise ValueError(f"unsupported workflow version schema: {self.schema}")
        from .validation import is_logical_id

        if not is_logical_id(self.version_id) or self.version <= 0:
            raise ValueError("workflow version requires a version_id and positive version")
        if self.workflow_id != self.definition.workflow_id:
            raise ValueError("workflow version workflow_id does not match its definition")
        if self.definition_sha256.lower() != self.definition.sha256():
            raise ValueError("workflow version definition_sha256 does not match its definition")
        if not is_sha256(self.definition_sha256):
            raise ValueError("workflow version definition_sha256 is invalid")
        _validate_timestamp(self.created_at, "WorkflowVersion.created_at", required=True)

    @classmethod
    def from_definition(
        cls,
        definition: WorkflowDefinition,
        *,
        version: int,
        created_at: str,
        version_id: str = "",
        status: WorkflowVersionStatus = WorkflowVersionStatus.DRAFT,
        mode: ValidationMode = ValidationMode.LINEAR,
    ) -> "WorkflowVersion":
        definition.validate(mode=mode)
        value = cls(
            version_id=version_id or f"{definition.workflow_id}@{version}",
            workflow_id=definition.workflow_id,
            version=version,
            definition_sha256=definition.sha256(),
            definition=definition,
            status=status,
            created_at=created_at,
        )
        value.validate(mode=mode)
        return value

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowVersion":
        fields = {
            "schema",
            "version_id",
            "workflow_id",
            "version",
            "definition_sha256",
            "definition",
            "status",
            "created_at",
        }
        _expect_exact(data, fields, "WorkflowVersion")
        value = cls(
            version_id=_text(data["version_id"], "WorkflowVersion.version_id"),
            workflow_id=_text(data["workflow_id"], "WorkflowVersion.workflow_id"),
            version=_integer(data["version"], "WorkflowVersion.version"),
            definition_sha256=_text(data["definition_sha256"], "WorkflowVersion.definition_sha256").lower(),
            definition=WorkflowDefinition.from_dict(data["definition"]),
            status=WorkflowVersionStatus(_text(data["status"], "WorkflowVersion.status")),
            created_at=_text(data["created_at"], "WorkflowVersion.created_at"),
            schema=_text(data["schema"], "WorkflowVersion.schema"),
        )
        value.validate(mode=ValidationMode.GRAPH)
        return value


@dataclass(frozen=True)
class WorkflowRuntimeBinding:
    definition_sha256: str
    resources: tuple[ResourceBinding, ...]
    binding_id: str = ""
    schema: str = WORKFLOW_RUNTIME_BINDING_SCHEMA
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.definition_sha256, "WorkflowRuntimeBinding.definition_sha256")
        _text(self.binding_id, "WorkflowRuntimeBinding.binding_id")
        _text(self.schema, "WorkflowRuntimeBinding.schema")
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "WorkflowRuntimeBinding.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "binding_id": self.binding_id,
            "definition_sha256": self.definition_sha256.lower(),
            "resources": [
                item.to_dict() for item in sorted(self.resources, key=lambda resource: resource.resource_id)
            ],
            "metadata": _mapping(self.metadata),
        }

    def canonical_json(self) -> str:
        # Identity and audit metadata do not affect the executable binding.
        payload = self.to_dict()
        payload.pop("binding_id", None)
        payload.pop("metadata", None)
        return canonical_json(payload)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def validate(self, definition: WorkflowDefinition) -> None:
        from .validation import validate_runtime_binding

        validate_runtime_binding(definition, self)

    def validate_files(self, definition: WorkflowDefinition) -> None:
        from .validation import validate_bound_resource_files

        validate_bound_resource_files(definition, self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowRuntimeBinding":
        fields = {"schema", "binding_id", "definition_sha256", "resources", "metadata"}
        _expect_exact(data, fields, "WorkflowRuntimeBinding")
        value = cls(
            definition_sha256=_text(data["definition_sha256"], "WorkflowRuntimeBinding.definition_sha256").lower(),
            resources=tuple(
                ResourceBinding.from_dict(item)
                for item in _sequence(data["resources"], "WorkflowRuntimeBinding.resources")
            ),
            binding_id=_text(data["binding_id"], "WorkflowRuntimeBinding.binding_id"),
            schema=_text(data["schema"], "WorkflowRuntimeBinding.schema"),
            metadata=_json_mapping(data["metadata"], "WorkflowRuntimeBinding.metadata"),
        )
        if value.schema != WORKFLOW_RUNTIME_BINDING_SCHEMA:
            raise ValueError(f"unsupported workflow runtime binding schema: {value.schema}")
        if not is_sha256(value.definition_sha256):
            raise ValueError("WorkflowRuntimeBinding.definition_sha256 is invalid")
        return value


@dataclass(frozen=True)
class WorkflowRun:
    workflow_version_id: str
    definition_sha256: str
    binding_sha256: str
    run_id: str = field(default_factory=lambda: _new_id("workflow_run"))
    status: WorkflowRunStatus = WorkflowRunStatus.QUEUED
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = WORKFLOW_RUN_SCHEMA

    def __post_init__(self) -> None:
        for name, value in {
            "workflow_version_id": self.workflow_version_id,
            "definition_sha256": self.definition_sha256,
            "binding_sha256": self.binding_sha256,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "schema": self.schema,
        }.items():
            _text(value, f"WorkflowRun.{name}")
        object.__setattr__(self, "status", WorkflowRunStatus(self.status))
        object.__setattr__(self, "inputs", _json_mapping(self.inputs, "WorkflowRun.inputs"))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "WorkflowRun.metadata"))

    def validate(self) -> None:
        from .validation import is_logical_id

        if self.schema != WORKFLOW_RUN_SCHEMA:
            raise ValueError(f"unsupported workflow run schema: {self.schema}")
        for name, value in {
            "run_id": self.run_id,
            "workflow_version_id": self.workflow_version_id,
        }.items():
            if not is_logical_id(value):
                raise ValueError(f"WorkflowRun.{name} must be a logical ID")
        if not is_sha256(self.definition_sha256):
            raise ValueError("WorkflowRun.definition_sha256 is invalid")
        if not is_sha256(self.binding_sha256):
            raise ValueError("WorkflowRun.binding_sha256 is invalid")
        _validate_timestamp(self.created_at, "WorkflowRun.created_at", required=True)
        _validate_timestamp(self.started_at, "WorkflowRun.started_at")
        _validate_timestamp(self.finished_at, "WorkflowRun.finished_at")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "workflow_version_id": self.workflow_version_id,
            "definition_sha256": self.definition_sha256.lower(),
            "binding_sha256": self.binding_sha256.lower(),
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "inputs": _mapping(self.inputs),
            "metadata": _mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowRun":
        fields = {
            "schema",
            "run_id",
            "workflow_version_id",
            "definition_sha256",
            "binding_sha256",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "inputs",
            "metadata",
        }
        _expect_exact(data, fields, "WorkflowRun")
        value = cls(
            run_id=_text(data["run_id"], "WorkflowRun.run_id"),
            workflow_version_id=_text(data["workflow_version_id"], "WorkflowRun.workflow_version_id"),
            definition_sha256=_text(data["definition_sha256"], "WorkflowRun.definition_sha256").lower(),
            binding_sha256=_text(data["binding_sha256"], "WorkflowRun.binding_sha256").lower(),
            status=WorkflowRunStatus(_text(data["status"], "WorkflowRun.status")),
            created_at=_text(data["created_at"], "WorkflowRun.created_at"),
            started_at=_text(data["started_at"], "WorkflowRun.started_at"),
            finished_at=_text(data["finished_at"], "WorkflowRun.finished_at"),
            inputs=_json_mapping(data["inputs"], "WorkflowRun.inputs"),
            metadata=_json_mapping(data["metadata"], "WorkflowRun.metadata"),
            schema=_text(data["schema"], "WorkflowRun.schema"),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class NodeRun:
    workflow_run_id: str
    node_id: str
    attempt: int = 1
    node_run_id: str = field(default_factory=lambda: _new_id("node_run"))
    status: NodeRunStatus = NodeRunStatus.PENDING
    session_id: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_code: str = ""
    error_message: str = ""
    outputs: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = NODE_RUN_SCHEMA

    def __post_init__(self) -> None:
        for name, value in {
            "workflow_run_id": self.workflow_run_id,
            "node_id": self.node_id,
            "node_run_id": self.node_run_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "schema": self.schema,
        }.items():
            _text(value, f"NodeRun.{name}")
        object.__setattr__(self, "attempt", _integer(self.attempt, "NodeRun.attempt"))
        object.__setattr__(self, "status", NodeRunStatus(self.status))
        object.__setattr__(self, "outputs", _json_mapping(self.outputs, "NodeRun.outputs"))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "NodeRun.metadata"))

    def validate(self) -> None:
        from .validation import is_logical_id

        if self.schema != NODE_RUN_SCHEMA:
            raise ValueError(f"unsupported node run schema: {self.schema}")
        for name, value in {
            "node_run_id": self.node_run_id,
            "workflow_run_id": self.workflow_run_id,
            "node_id": self.node_id,
        }.items():
            if not is_logical_id(value):
                raise ValueError(f"NodeRun.{name} must be a logical ID")
        if self.attempt <= 0:
            raise ValueError("NodeRun.attempt must be positive")
        _validate_timestamp(self.created_at, "NodeRun.created_at", required=True)
        _validate_timestamp(self.started_at, "NodeRun.started_at")
        _validate_timestamp(self.finished_at, "NodeRun.finished_at")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": self.schema,
            "node_run_id": self.node_run_id,
            "workflow_run_id": self.workflow_run_id,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "outputs": _mapping(self.outputs),
            "metadata": _mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NodeRun":
        fields = {
            "schema",
            "node_run_id",
            "workflow_run_id",
            "node_id",
            "attempt",
            "status",
            "session_id",
            "created_at",
            "started_at",
            "finished_at",
            "error_code",
            "error_message",
            "outputs",
            "metadata",
        }
        _expect_exact(data, fields, "NodeRun")
        value = cls(
            node_run_id=_text(data["node_run_id"], "NodeRun.node_run_id"),
            workflow_run_id=_text(data["workflow_run_id"], "NodeRun.workflow_run_id"),
            node_id=_text(data["node_id"], "NodeRun.node_id"),
            attempt=_integer(data["attempt"], "NodeRun.attempt"),
            status=NodeRunStatus(_text(data["status"], "NodeRun.status")),
            session_id=_text(data["session_id"], "NodeRun.session_id"),
            created_at=_text(data["created_at"], "NodeRun.created_at"),
            started_at=_text(data["started_at"], "NodeRun.started_at"),
            finished_at=_text(data["finished_at"], "NodeRun.finished_at"),
            error_code=_text(data["error_code"], "NodeRun.error_code"),
            error_message=_text(data["error_message"], "NodeRun.error_message"),
            outputs=_json_mapping(data["outputs"], "NodeRun.outputs"),
            metadata=_json_mapping(data["metadata"], "NodeRun.metadata"),
            schema=_text(data["schema"], "NodeRun.schema"),
        )
        value.validate()
        return value
