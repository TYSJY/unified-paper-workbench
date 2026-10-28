from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Callable

from ..contracts import ResultStatus, TaskEnvelope, TaskResult
from ..provider_runtime import (
    IsolatedProviderRuntime,
    RuntimeProviderConfig,
    isolated_provider_runtime,
)
from .base import ExecutorDescriptor
from unified_paper_orchestrator.windows_subprocess import (
    configure_openai_codex_no_window,
)
from unified_paper_orchestrator.codex_runtime import runtime_launch_options


class CodexSDKExecutor:
    name = "codex_sdk"

    _FORBIDDEN_PROVIDER_PAYLOAD_KEYS = frozenset(
        {
            "api_base",
            "api_key",
            "base_url",
            "custom_headers",
            "endpoint",
            "endpoint_base_url",
            "endpoint_url",
            "env_key",
            "http_headers",
            "model_provider",
            "provider",
            "provider_config",
            "proxy_url",
            "wire_api",
        }
    )

    def __init__(
        self,
        codex_factory: Callable[..., Any] | None = None,
        *,
        provider_config: RuntimeProviderConfig | None = None,
        key_resolver: Callable[[str], str] | None = None,
        runtime_temp_root: str | Path | None = None,
    ) -> None:
        if provider_config is None and key_resolver is not None:
            raise ValueError("key_resolver requires provider_config")
        if provider_config is not None:
            provider_config.validate()
        self.codex_factory = codex_factory
        self.provider_config = provider_config
        self.key_resolver = key_resolver
        self.runtime_temp_root = Path(runtime_temp_root) if runtime_temp_root is not None else None

    def _availability(self) -> tuple[bool, str]:
        if self.provider_config is not None and self.key_resolver is None:
            return False, "runtime provider key resolver is not configured"
        if self.codex_factory is not None:
            return True, "injected Codex client"
        try:
            import openai_codex  # noqa: F401
        except ImportError:
            return False, "optional beta package 'openai-codex' is not installed"
        return True, "local Codex app-server client is available"

    def descriptor(self) -> ExecutorDescriptor:
        available, detail = self._availability()
        return ExecutorDescriptor(name=self.name, available=available, kind="codex_sdk", detail=detail, side_effecting=True)

    @classmethod
    def _payload_provider_override(cls, payload: dict[str, Any]) -> str:
        for raw_key in payload:
            key = str(raw_key).strip().lower().replace("-", "_")
            if (
                key in cls._FORBIDDEN_PROVIDER_PAYLOAD_KEYS
                or key.endswith("_base_url")
                or key.endswith("_endpoint")
                or key.endswith("_endpoint_url")
            ):
                return str(raw_key)
        return ""

    def _injected_factory_accepts_runtime(self) -> bool:
        if self.codex_factory is None:
            return False
        try:
            signature = inspect.signature(self.codex_factory)
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }
            for parameter in signature.parameters.values()
        )

    def _codex_context(self, runtime: IsolatedProviderRuntime | None) -> Any:
        if self.codex_factory is not None:
            if runtime is not None and self._injected_factory_accepts_runtime():
                return self.codex_factory(runtime)
            return self.codex_factory()
        configure_openai_codex_no_window()
        from openai_codex import Codex, CodexConfig

        if runtime is None:
            return Codex(CodexConfig(**runtime_launch_options({})))
        return Codex(
            CodexConfig(
                config_overrides=runtime.config_overrides,
                **runtime_launch_options(dict(runtime.environment)),
            )
        )

    def _run_codex(
        self,
        *,
        prompt: str,
        sandbox_name: str,
        sandbox_value: Any,
        model: str,
        runtime: IsolatedProviderRuntime | None,
        token_budget: int | None,
    ) -> TaskResult:
        context = self._codex_context(runtime)
        with context as codex:
            kwargs: dict[str, Any] = {"sandbox": sandbox_value}
            if model:
                kwargs["model"] = model
            if runtime is not None:
                kwargs["model_provider"] = runtime.provider_id
            thread = codex.thread_start(**kwargs)
            result = thread.run(prompt)
            final_response = str(getattr(result, "final_response", "") or "")
            thread_id = str(getattr(thread, "id", "") or getattr(result, "thread_id", "") or "")
            turn_id = str(getattr(result, "id", "") or "")
            raw_turn_status = getattr(result, "status", "completed")
            turn_status = str(getattr(raw_turn_status, "value", raw_turn_status) or "")
            usage = self._usage_dict(getattr(result, "usage", None))
        data: dict[str, Any] = {
            "final_response": final_response,
            "output_text": final_response,
            "sandbox": sandbox_name,
            "model": model,
            "turn_id": turn_id,
            "turn_status": turn_status,
            "usage": usage,
        }
        if runtime is not None:
            data.update(
                {
                    "provider_profile": runtime.config.profile_id,
                    "provider_kind": "custom_codex_responses",
                    "provider_assurance": "user_configured_endpoint_unverified",
                    "provider_digest": runtime.provider_digest,
                    "provider_config_sha256": runtime.provider_digest,
                    "endpoint_config_sha256": runtime.provider_digest,
                    "endpoint_base_url": runtime.config.base_url,
                    "endpoint_origin": runtime.config.base_url,
                    "wire_api": runtime.config.wire_api,
                    "reasoning_effort": runtime.config.reasoning_effort,
                    "transport_policy": "isolated_codex_sdk_custom_provider",
                }
            )
        if turn_status != "completed":
            return TaskResult(
                status=(
                    ResultStatus.RETRYABLE_FAILURE
                    if turn_status in {"inProgress", "interrupted"}
                    else ResultStatus.FATAL_FAILURE
                ),
                blocker_code="CODEX_SDK_TURN_NOT_COMPLETED",
                message="Codex SDK turn did not complete successfully",
                metrics=usage,
                data={
                    key: value
                    for key, value in data.items()
                    if key not in {"final_response", "output_text"}
                },
                external_ref=thread_id,
            )
        if not final_response.strip():
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="CODEX_SDK_RESPONSE_EMPTY",
                message="Codex SDK completed without a final response",
                metrics=usage,
                data={
                    key: value
                    for key, value in data.items()
                    if key not in {"final_response", "output_text"}
                },
                external_ref=thread_id,
            )
        total_tokens = int(usage.get("total_tokens") or 0)
        if token_budget is not None and total_tokens > token_budget:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="CODEX_SDK_TOKEN_BUDGET_EXCEEDED",
                message="Codex SDK usage exceeded the task token budget; output was discarded",
                metrics=usage,
                data={
                    key: value
                    for key, value in data.items()
                    if key not in {"final_response", "output_text"}
                },
                external_ref=thread_id,
            )
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="Codex SDK task completed",
            metrics=usage,
            data=data,
            external_ref=thread_id,
        )

    @staticmethod
    def _usage_dict(value: Any) -> dict[str, int]:
        total = getattr(value, "total", None)
        if total is None:
            return {}
        fields = {
            "input_tokens": "input_tokens",
            "cached_input_tokens": "cached_input_tokens",
            "output_tokens": "output_tokens",
            "reasoning_output_tokens": "reasoning_output_tokens",
            "total_tokens": "total_tokens",
        }
        result: dict[str, int] = {}
        for target, source in fields.items():
            try:
                parsed = int(getattr(total, source, 0) or 0)
            except (TypeError, ValueError):
                parsed = 0
            if parsed >= 0:
                result[target] = parsed
        return result

    def execute(self, task: TaskEnvelope) -> TaskResult:
        override = self._payload_provider_override(task.payload)
        if override:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="CODEX_PROVIDER_OVERRIDE_FORBIDDEN",
                message=f"payload.{override} cannot override the executor's trusted provider binding",
            )
        available, detail = self._availability()
        if not available:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="CODEX_SDK_NOT_CONFIGURED",
                message=detail,
            )
        prompt = str(task.payload.get("prompt") or "").strip()
        if not prompt:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="CODEX_PROMPT_REQUIRED",
                message="payload.prompt is required",
            )
        sandbox_name = str(task.payload.get("sandbox") or "read_only").strip()
        if sandbox_name not in {"read_only", "workspace_write"}:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="CODEX_SANDBOX_NOT_ALLOWED",
                message="V2 foundation permits only read_only or workspace_write Codex sandboxes",
            )
        if sandbox_name == "workspace_write" and not task.writable_roots:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="WRITABLE_ROOT_REQUIRED",
                message="workspace_write requires at least one declared writable root",
            )
        if self.codex_factory is not None:
            sandbox_value: Any = sandbox_name
        else:
            from openai_codex import Sandbox

            sandbox_value = Sandbox.read_only if sandbox_name == "read_only" else Sandbox.workspace_write
        if self.provider_config is None:
            model = str(task.payload.get("model") or os.environ.get("CODEX_MODEL") or "").strip()
            return self._run_codex(
                prompt=prompt,
                sandbox_name=sandbox_name,
                sandbox_value=sandbox_value,
                model=model,
                runtime=None,
                token_budget=task.token_budget,
            )
        try:
            api_key = self.key_resolver(self.provider_config.profile_id) if self.key_resolver is not None else ""
        except Exception:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="CODEX_PROVIDER_KEY_RESOLUTION_FAILED",
                message="The trusted provider key could not be resolved",
            )
        if not isinstance(api_key, str) or not api_key.strip():
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="CODEX_PROVIDER_KEY_REQUIRED",
                message="The trusted provider key is unavailable",
            )
        with isolated_provider_runtime(
            self.provider_config,
            api_key,
            temp_root=self.runtime_temp_root,
        ) as runtime:
            return self._run_codex(
                prompt=prompt,
                sandbox_name=sandbox_name,
                sandbox_value=sandbox_value,
                model=self.provider_config.model,
                runtime=runtime,
                token_budget=task.token_budget,
            )
