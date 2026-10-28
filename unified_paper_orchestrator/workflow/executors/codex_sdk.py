from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from ..artifacts import BoundArtifact, ProducedArtifact
from ..contracts import NodeType
from ..sessions import SessionHandle
from .base import (
    ExecutionContext,
    ExecutionControl,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorDescriptor,
    blocked_result,
    control_checkpoint,
    enforce_output_contract,
    freeze_bound_artifact,
    produced_artifacts_from_sequence,
)


_FORBIDDEN_PARAMETER_KEYS = frozenset(
    {
        "allowed_tools",
        "authorization",
        "api_base",
        "api_key",
        "base_url",
        "custom_headers",
        "credential",
        "credentials",
        "endpoint",
        "endpoint_base_url",
        "endpoint_url",
        "env_key",
        "http_headers",
        "headers",
        "max_tokens",
        "model",
        "model_provider",
        "network_access",
        "provider",
        "provider_config",
        "provider_id",
        "proxy",
        "proxy_url",
        "reasoning_effort",
        "secret",
        "timeout",
        "timeout_seconds",
        "token_limit",
        "access_token",
        "auth_token",
        "wire_api",
    }
)
_FORBIDDEN_PARAMETER_KEYS_COMPACT = frozenset(
    "".join(character for character in item if character.isalnum())
    for item in _FORBIDDEN_PARAMETER_KEYS
)
_SAFE_GENERATION_PARAMETER_KEYS = frozenset(
    {"frequency_penalty", "presence_penalty", "seed", "stop", "temperature", "top_p"}
)


def _freeze_mapping(value: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{path} keys must be strings")
        if isinstance(item, Mapping):
            frozen[key] = _freeze_mapping(item, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            frozen[key] = tuple(_freeze_value(child, f"{path}.{key}[]") for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{path}.{key} contains a non-finite number")
        elif item is None or isinstance(item, (str, bool, int, float)):
            frozen[key] = item
        else:
            raise TypeError(f"{path}.{key} contains unsupported value type: {type(item).__name__}")
    return MappingProxyType(frozen)


def _freeze_value(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, path)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item, f"{path}[]") for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"{path} contains unsupported value type: {type(value).__name__}")


@dataclass(frozen=True)
class CodexRequest:
    """Provider-neutral request made from the frozen node execution profile."""

    workflow_run_id: str
    node_run_id: str
    prompt: str
    output_root: Path
    model: str
    reasoning_effort: str
    token_limit: int | None
    timeout_seconds: int
    allowed_tools: tuple[str, ...]
    network_access: bool
    inputs: Mapping[str, BoundArtifact] = field(default_factory=dict)
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in {
            "workflow_run_id": self.workflow_run_id,
            "node_run_id": self.node_run_id,
            "prompt": self.prompt,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }.items():
            if not isinstance(value, str):
                raise TypeError(f"CodexRequest.{name} must be a string")
        if not self.prompt:
            raise ValueError("CodexRequest.prompt cannot be empty")
        output_root = Path(self.output_root)
        if not output_root.is_absolute():
            raise ValueError("CodexRequest.output_root must be absolute")
        object.__setattr__(self, "output_root", output_root)
        if self.token_limit is not None:
            if isinstance(self.token_limit, bool) or not isinstance(self.token_limit, int):
                raise TypeError("CodexRequest.token_limit must be an integer or null")
            if self.token_limit <= 0:
                raise ValueError("CodexRequest.token_limit must be positive")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int):
            raise TypeError("CodexRequest.timeout_seconds must be an integer")
        if self.timeout_seconds <= 0:
            raise ValueError("CodexRequest.timeout_seconds must be positive")
        tools = tuple(self.allowed_tools)
        if not all(isinstance(tool, str) and tool.strip() for tool in tools):
            raise ValueError("CodexRequest.allowed_tools must contain non-empty strings")
        object.__setattr__(self, "allowed_tools", tools)
        if not isinstance(self.network_access, bool):
            raise TypeError("CodexRequest.network_access must be a boolean")
        inputs: dict[str, BoundArtifact] = {}
        for artifact_id, artifact in self.inputs.items():
            if not isinstance(artifact_id, str) or not isinstance(artifact, BoundArtifact):
                raise TypeError("CodexRequest.inputs must map strings to BoundArtifact records")
            if artifact_id != artifact.artifact_id:
                raise ValueError("CodexRequest input key must match artifact_id")
            inputs[artifact_id] = freeze_bound_artifact(artifact)
        object.__setattr__(self, "inputs", MappingProxyType(inputs))
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters, "CodexRequest.parameters"))


@dataclass(frozen=True)
class CodexResponse:
    status: str
    output_text: str = ""
    external_session_id: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in {
            "status": self.status,
            "output_text": self.output_text,
            "external_session_id": self.external_session_id,
        }.items():
            if not isinstance(value, str):
                raise TypeError(f"CodexResponse.{name} must be a string")
        if not self.status.strip():
            raise ValueError("CodexResponse.status is required")
        usage: dict[str, int] = {}
        for key, value in self.usage.items():
            if not isinstance(key, str):
                raise TypeError("CodexResponse.usage keys must be strings")
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("CodexResponse.usage values must be integers")
            if value < 0:
                raise ValueError("CodexResponse.usage values cannot be negative")
            usage[key] = value
        object.__setattr__(self, "usage", MappingProxyType(usage))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "CodexResponse.metadata"))


class CodexClient(Protocol):
    def run(
        self,
        *,
        session: SessionHandle,
        request: CodexRequest,
        control: ExecutionControl,
    ) -> CodexResponse: ...


class CodexOutputCollector(Protocol):
    def __call__(
        self,
        context: ExecutionContext,
        response: CodexResponse,
    ) -> Sequence[ProducedArtifact]: ...


def _normalized_key(value: str) -> str:
    return value.strip().casefold().replace("-", "_")


def _find_forbidden_parameter(value: Any, path: str = "parameters") -> str:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = _normalized_key(str(raw_key))
            compact_key = "".join(character for character in key if character.isalnum())
            current = f"{path}.{raw_key}"
            if (
                key in _FORBIDDEN_PARAMETER_KEYS
                or compact_key in _FORBIDDEN_PARAMETER_KEYS_COMPACT
                or key.endswith("_base_url")
                or key.endswith("_endpoint")
                or key.endswith("_endpoint_url")
                or key.endswith("_api_key")
                or key.endswith("_provider")
                or compact_key.endswith("baseurl")
                or compact_key.endswith("endpoint")
                or compact_key.endswith("endpointurl")
                or compact_key.endswith("apikey")
                or compact_key.endswith("provider")
            ):
                return current
            nested = _find_forbidden_parameter(item, current)
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            nested = _find_forbidden_parameter(item, f"{path}[{index}]")
            if nested:
                return nested
    return ""


def _find_unsupported_parameter(value: Mapping[str, Any]) -> str:
    for raw_key, item in value.items():
        key = _normalized_key(raw_key)
        path = f"parameters.{raw_key}"
        if key not in _SAFE_GENERATION_PARAMETER_KEYS:
            return path
        if key in {"temperature", "top_p", "frequency_penalty", "presence_penalty"}:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
                return path
        elif key == "seed":
            if isinstance(item, bool) or not isinstance(item, int):
                return path
        elif key == "stop":
            if isinstance(item, str):
                continue
            if not isinstance(item, (list, tuple)) or not all(
                isinstance(part, str) and part for part in item
            ):
                return path
    return ""


def _read_sealed_prompt(path: Path, expected_sha256: str) -> str:
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if not path.is_file():
        raise ValueError("Prompt binding is not a file")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Prompt binding changed while it was read")
    if hashlib.sha256(data).hexdigest() != expected_sha256.lower():
        raise ValueError("Prompt binding SHA256 changed after validation")
    return data.decode("utf-8-sig")


def _revalidate_sealed_prompt(context: ExecutionContext, expected_prompt: str) -> None:
    prompt_ref = context.node.prompt_ref
    if prompt_ref is None:
        raise ValueError("Codex PromptRef is missing")
    context.runtime_binding.validate_files(context.definition)
    resource = next(
        item
        for item in context.runtime_binding.resources
        if item.resource_id == prompt_ref.resource_id
    )
    if _read_sealed_prompt(Path(resource.absolute_path), prompt_ref.content_sha256) != expected_prompt:
        raise ValueError("sealed Prompt bytes changed during execution")


def _post_turn_checkpoint_result(
    checkpoint: ExecutionResult | None,
    response: CodexResponse,
) -> ExecutionResult | None:
    if checkpoint is None:
        return None
    if checkpoint.outcome is ExecutionOutcome.CANCELLED:
        return ExecutionResult(
            outcome=ExecutionOutcome.CANCELLED,
            error_code=checkpoint.error_code,
            message=checkpoint.message,
            external_session_id=response.external_session_id,
            usage=response.usage,
        )
    return ExecutionResult(
        outcome=ExecutionOutcome.BLOCKED,
        error_code="CODEX_POST_TURN_CONTROL_FAILED",
        message="The Codex turn may have completed before an execution-control callback failed.",
        external_session_id=response.external_session_id,
        usage=response.usage,
        metadata=checkpoint.metadata,
    )


class CodexSDKExecutor:
    """Injection-only Codex port; provider construction belongs to the composition root."""

    def __init__(
        self,
        client: CodexClient | None = None,
        *,
        output_collector: CodexOutputCollector | None = None,
        max_concurrency: int = 1,
    ) -> None:
        if client is not None and not callable(getattr(client, "run", None)):
            raise TypeError("Codex client must expose a callable run method")
        if output_collector is not None and not callable(output_collector):
            raise TypeError("output_collector must be callable")
        self._client = client
        self._output_collector = output_collector
        self._max_concurrency = max_concurrency

    def descriptor(self) -> ExecutorDescriptor:
        configured = self._client is not None and self._output_collector is not None
        return ExecutorDescriptor(
            executor_id="codex-sdk",
            node_type=NodeType.CODEX,
            available=configured,
            detail=(
                "Injected Codex client and artifact collector; sessions arrive in the frozen execution context."
                if configured
                else "Codex execution requires an injected client and output collector."
            ),
            side_effecting=True,
            max_concurrency=self._max_concurrency,
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        if context.node.node_type is not NodeType.CODEX:
            return blocked_result(
                "EXECUTOR_NODE_TYPE_MISMATCH",
                "The Codex SDK executor accepts only codex nodes.",
            )
        if not self.descriptor().available:
            return blocked_result(
                "CODEX_SDK_NOT_CONFIGURED",
                "Codex execution requires an injected client and output collector.",
            )
        checkpoint = control_checkpoint(context, heartbeat=False)
        if checkpoint is not None:
            return checkpoint

        forbidden = _find_forbidden_parameter(context.node.execution_profile.parameters)
        unsupported = _find_unsupported_parameter(context.node.execution_profile.parameters)
        if forbidden or unsupported:
            return blocked_result(
                "CODEX_EXECUTION_PROFILE_OVERRIDE_FORBIDDEN",
                "Codex node parameters must be safe generation controls and cannot override trusted bindings.",
                metadata={"parameter_path": forbidden or unsupported},
            )
        prompt_ref = context.node.prompt_ref
        if prompt_ref is None:
            return blocked_result("CODEX_PROMPT_REQUIRED", "Codex nodes require a sealed PromptRef.")

        try:
            # This is deliberately repeated immediately before every dispatch.
            context.runtime_binding.validate_files(context.definition)
            resource = next(
                item
                for item in context.runtime_binding.resources
                if item.resource_id == prompt_ref.resource_id
            )
            prompt = _read_sealed_prompt(Path(resource.absolute_path), prompt_ref.content_sha256)
        except Exception as exc:
            return blocked_result(
                "CODEX_PROMPT_BINDING_INVALID",
                "The sealed Prompt binding could not be verified immediately before dispatch.",
                metadata={"error_type": type(exc).__name__},
            )
        if not prompt.strip():
            return blocked_result("CODEX_PROMPT_EMPTY", "The sealed Prompt file is empty.")

        profile = context.node.execution_profile
        if not profile.model.strip() or not profile.reasoning_effort.strip() or profile.token_limit is None:
            return blocked_result(
                "CODEX_EXECUTION_PROFILE_INCOMPLETE",
                "Codex nodes require frozen model, reasoning effort, and token limit values.",
            )
        request = CodexRequest(
            workflow_run_id=context.workflow_run.run_id,
            node_run_id=context.node_run.node_run_id,
            prompt=prompt,
            output_root=context.output_root,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            token_limit=profile.token_limit,
            timeout_seconds=profile.timeout_seconds,
            allowed_tools=profile.allowed_tools,
            network_access=profile.network_access,
            inputs=context.inputs,
            parameters=profile.parameters,
        )
        session = context.session
        if session is None:
            return blocked_result(
                "CODEX_SESSION_NOT_AVAILABLE",
                "The scheduler did not inject a frozen run session.",
            )
        if session.executor != self.descriptor().executor_id:
            return blocked_result(
                "CODEX_SESSION_EXECUTOR_MISMATCH",
                "The frozen session belongs to a different executor.",
            )

        checkpoint = control_checkpoint(context, heartbeat=True)
        if checkpoint is not None:
            return checkpoint
        try:
            response = self._client.run(  # type: ignore[union-attr]
                session=session,
                request=request,
                control=context.control,
            )
        except Exception as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_SDK_OUTCOME_UNKNOWN",
                message="The Codex client failed after dispatch; remote side effects are unknown.",
                metadata={"error_type": type(exc).__name__},
            )
        if not isinstance(response, CodexResponse):
            return ExecutionResult(
                outcome=ExecutionOutcome.FATAL_FAILURE,
                error_code="CODEX_SDK_RESPONSE_INVALID",
                message="The injected Codex client did not return a CodexResponse.",
            )
        if not response.external_session_id.strip():
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_SESSION_ID_MISSING",
                message="Codex did not return the external fixed-session identity.",
                usage=response.usage,
            )
        if session.external_session_id and response.external_session_id != session.external_session_id:
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_SESSION_ID_MISMATCH",
                message="Codex returned a different external session than the frozen session handle.",
                external_session_id=session.external_session_id,
                usage=response.usage,
            )
        checkpoint = control_checkpoint(context, heartbeat=True)
        post_turn = _post_turn_checkpoint_result(checkpoint, response)
        if post_turn is not None:
            return post_turn

        normalized_status = response.status.strip().casefold().replace("-", "_")
        if normalized_status != "completed":
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_TURN_RECONCILIATION_REQUIRED",
                message="The remote Codex turn is not proven terminal and must be reconciled before retry.",
                external_session_id=response.external_session_id,
                usage=response.usage,
                metadata={"turn_status": response.status},
            )
        try:
            _revalidate_sealed_prompt(context, prompt)
        except Exception as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_PROMPT_BINDING_CHANGED",
                message="The sealed Prompt binding changed during the Codex turn.",
                external_session_id=response.external_session_id,
                usage=response.usage,
                metadata={"error_type": type(exc).__name__},
            )
        total_tokens = response.usage.get("total_tokens")
        if total_tokens is None:
            return ExecutionResult(
                outcome=ExecutionOutcome.FATAL_FAILURE,
                error_code="CODEX_USAGE_UNAVAILABLE",
                message="Codex did not return total token usage for the frozen token limit.",
                external_session_id=response.external_session_id,
                usage=response.usage,
            )
        if profile.token_limit is not None and total_tokens > profile.token_limit:
            return ExecutionResult(
                outcome=ExecutionOutcome.FATAL_FAILURE,
                error_code="CODEX_TOKEN_LIMIT_EXCEEDED",
                message="Codex usage exceeded the frozen node token limit; outputs were discarded.",
                external_session_id=response.external_session_id,
                usage=response.usage,
            )
        if not response.output_text.strip() and not context.node.outputs:
            return ExecutionResult(
                outcome=ExecutionOutcome.FATAL_FAILURE,
                error_code="CODEX_RESPONSE_EMPTY",
                message="The Codex SDK turn completed without text or a declared output contract.",
                external_session_id=response.external_session_id,
                usage=response.usage,
            )

        try:
            artifacts = produced_artifacts_from_sequence(
                self._output_collector(context, response)  # type: ignore[misc]
            )
        except Exception as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_OUTPUT_COLLECTION_FAILED",
                message="Programmatic artifact collection failed after the model turn.",
                external_session_id=response.external_session_id,
                usage=response.usage,
                metadata={"error_type": type(exc).__name__},
            )
        try:
            _revalidate_sealed_prompt(context, prompt)
        except Exception as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.BLOCKED,
                error_code="CODEX_PROMPT_BINDING_CHANGED",
                message="The sealed Prompt binding changed during output collection.",
                external_session_id=response.external_session_id,
                usage=response.usage,
                metadata={"error_type": type(exc).__name__},
            )
        checkpoint = control_checkpoint(context, heartbeat=True)
        post_turn = _post_turn_checkpoint_result(checkpoint, response)
        if post_turn is not None:
            return post_turn
        result = ExecutionResult(
            outcome=ExecutionOutcome.COMPLETED,
            message="Codex SDK execution completed and returned to programmatic artifact validation.",
            produced_artifacts=artifacts,
            model_output=response.output_text,
            external_session_id=response.external_session_id,
            usage=response.usage,
            metadata=response.metadata,
        )
        return enforce_output_contract(context, result)
