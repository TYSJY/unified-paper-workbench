from __future__ import annotations

import ipaddress
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from ..contracts import ResultStatus, TaskEnvelope, TaskResult
from .base import ExecutorDescriptor


OFFICIAL_OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_LOOPBACK_PROXY_ENV = "UNIFIED_OPENAI_HTTPS_PROXY"


class OpenAIProxyConfigurationError(ValueError):
    """Raised when the explicit API proxy is not a credential-free loopback URL."""


def validated_loopback_proxy_url(value: str) -> str:
    """Return one normalized HTTP loopback proxy URL or fail closed.

    Generic HTTP(S)_PROXY variables remain ignored.  This narrow contract is
    only for machines where a user-controlled local CONNECT proxy is required
    to reach the fixed official API endpoint.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise OpenAIProxyConfigurationError(
            "Explicit OpenAI proxy URL is invalid."
        ) from exc
    host = str(parsed.hostname or "").strip().lower()
    if (
        parsed.scheme.lower() != "http"
        or not host
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OpenAIProxyConfigurationError(
            "Explicit OpenAI proxy must be a credential-free HTTP loopback URL with an explicit port."
        )
    if host == "localhost":
        normalized_host = host
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise OpenAIProxyConfigurationError(
                "Explicit OpenAI proxy host must be loopback."
            ) from exc
        if not address.is_loopback:
            raise OpenAIProxyConfigurationError(
                "Explicit OpenAI proxy host must be loopback."
            )
        normalized_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{normalized_host}:{port}"


class OpenAIReviewExecutor:
    """Single-request Responses API executor for the staging-only review canary."""

    name = "openai_review"
    descriptor_kind = "responses_api_staging_review"
    api_key_env = "OPENAI_API_KEY"
    model_env = "OPENAI_MODEL"
    proxy_env = OPENAI_LOOPBACK_PROXY_ENV
    base_url = OFFICIAL_OPENAI_BASE_URL
    provider_kind = "official_openai"
    provider_profile = "official_openai_responses_v1"
    provider_assurance = "official_endpoint_contract"

    def __init__(self, client_factory: Callable[[TaskEnvelope], Any] | None = None) -> None:
        self.client_factory = client_factory

    def _availability(self) -> tuple[bool, str]:
        if self.client_factory is not None:
            return True, "injected Responses API client"
        try:
            import openai  # noqa: F401
        except ImportError:
            return False, "official OpenAI Python SDK is not installed"
        if not os.environ.get(self.api_key_env, "").strip():
            return False, f"{self.api_key_env} is not configured"
        label = self.provider_profile
        if os.environ.get(self.model_env, "").strip():
            return True, f"{label} review executor and default model are configured; generic env proxies are ignored"
        return True, f"{label} review executor is configured; supply a model per task; generic env proxies are ignored"

    def descriptor(self) -> ExecutorDescriptor:
        available, detail = self._availability()
        return ExecutorDescriptor(
            name=self.name,
            available=available,
            kind=self.descriptor_kind,
            detail=detail,
            side_effecting=True,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        result = self._execute(task)
        result.data = {
            **result.data,
            "provider_kind": self._provider_kind(),
            "provider_profile": self._provider_profile(),
            "provider_assurance": self._provider_assurance(),
        }
        return result

    def _execute(self, task: TaskEnvelope) -> TaskResult:
        available, detail = self._availability()
        if not available:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="OPENAI_API_NOT_CONFIGURED",
                message=detail,
            )
        if not task.network_access:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="NETWORK_NOT_AUTHORIZED",
                message="Review canary requires explicit API network authorization.",
            )
        if task.allowed_tools:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="OPENAI_TOOLS_NOT_ALLOWED",
                message="The first review canary does not allow model tools.",
            )
        try:
            proxy_url = self._configured_loopback_proxy()
        except OpenAIProxyConfigurationError as exc:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="OPENAI_LOOPBACK_PROXY_INVALID",
                message=str(exc),
                data={
                    "endpoint_origin": self._endpoint_origin(),
                    "transport_policy": "invalid_explicit_proxy_rejected",
                },
            )
        transport_policy = self._transport_policy(proxy_url)
        instructions = str(task.payload.get("_prepared_instructions") or "")
        input_text = str(task.payload.get("_prepared_input") or "")
        if not instructions or not input_text:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="OPENAI_REVIEW_INPUT_NOT_PREPARED",
                message="Review task was not prepared by the execution gateway.",
            )
        model = str(task.payload.get("model") or os.environ.get(self.model_env) or "").strip()
        if not model:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="OPENAI_MODEL_REQUIRED",
                message="Set OPENAI_MODEL or provide an explicit canary model.",
            )
        try:
            max_output_tokens = int(task.payload.get("max_output_tokens") or 0)
        except (TypeError, ValueError):
            max_output_tokens = 0
        if max_output_tokens <= 0:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="OPENAI_MAX_OUTPUT_TOKENS_INVALID",
                message="A positive max_output_tokens value is required.",
            )
        if task.token_budget is not None and max_output_tokens > task.token_budget:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="OPENAI_TOKEN_BUDGET_INVALID",
                message="max_output_tokens exceeds the declared task token budget.",
            )
        try:
            estimated_input_tokens = int(task.payload.get("_estimated_input_tokens") or 0)
        except (TypeError, ValueError):
            estimated_input_tokens = 0
        if estimated_input_tokens <= 0:
            total_bytes = len(instructions.encode("utf-8")) + len(input_text.encode("utf-8"))
            estimated_input_tokens = max(1, (total_bytes + 2) // 3)
        if task.token_budget is not None and estimated_input_tokens + max_output_tokens > task.token_budget:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="OPENAI_TOKEN_BUDGET_PREFLIGHT",
                message="Estimated input plus maximum output exceeds the declared token budget; API was not called.",
                metrics={
                    "estimated_input_tokens": estimated_input_tokens,
                    "max_output_tokens": max_output_tokens,
                    "token_budget": task.token_budget,
                },
                data={
                    "model": model,
                    "endpoint_origin": self._endpoint_origin(),
                    "transport_policy": transport_policy,
                },
            )

        try:
            client = self._client(task, proxy_url=proxy_url)
        except Exception as exc:
            return self._request_error(
                exc,
                phase="client",
                model=model,
                transport_policy=transport_policy,
            )

        started = time.monotonic()
        try:
            response = client.responses.create(
                model=model,
                instructions=instructions,
                input=input_text,
                store=False,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            return self._request_error(
                exc,
                phase="request",
                model=model,
                transport_policy=transport_policy,
            )
        finally:
            if self.client_factory is None:
                try:
                    client.close()
                except Exception:
                    pass
        duration_ms = int((time.monotonic() - started) * 1000)
        gateway_request_id = str(getattr(response, "_request_id", "") or "")

        response_status = str(getattr(response, "status", "completed") or "completed")
        if response_status != "completed":
            reason = str(getattr(getattr(response, "incomplete_details", None), "reason", "") or "")
            return TaskResult(
                status=ResultStatus.RETRYABLE_FAILURE,
                blocker_code="OPENAI_RESPONSE_INCOMPLETE",
                message="Responses API did not return a completed review.",
                metrics={"duration_ms": duration_ms},
                data={
                    "model": model,
                    "endpoint_origin": self._endpoint_origin(),
                    "transport_policy": transport_policy,
                    "incomplete_reason": reason,
                    "gateway_request_id": gateway_request_id,
                },
                external_ref=str(getattr(response, "id", "") or ""),
            )
        output_text = str(getattr(response, "output_text", "") or "")
        if not output_text.strip():
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="OPENAI_EMPTY_OUTPUT",
                message="Responses API returned no review text.",
                metrics={"duration_ms": duration_ms},
                data={
                    "model": model,
                    "endpoint_origin": self._endpoint_origin(),
                    "transport_policy": transport_policy,
                    "gateway_request_id": gateway_request_id,
                },
                external_ref=str(getattr(response, "id", "") or ""),
            )
        metrics = self._usage_metrics(getattr(response, "usage", None))
        metrics["duration_ms"] = duration_ms
        metrics["estimated_input_tokens"] = estimated_input_tokens
        metrics["max_output_tokens"] = max_output_tokens
        metrics["token_budget"] = task.token_budget
        total_tokens = metrics.get("total_tokens")
        if task.token_budget is not None and isinstance(total_tokens, int) and total_tokens > task.token_budget:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="OPENAI_TOKEN_BUDGET_EXCEEDED",
                message="Responses API usage exceeded the declared task token budget; output was discarded.",
                metrics=metrics,
                data={
                    "model": model,
                    "endpoint_origin": self._endpoint_origin(),
                    "transport_policy": transport_policy,
                    "gateway_request_id": gateway_request_id,
                },
                external_ref=str(getattr(response, "id", "") or ""),
            )
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="Responses API review canary completed; staging validation is pending.",
            metrics=metrics,
            data={
                "output_text": output_text,
                "model": model,
                "endpoint_origin": self._endpoint_origin(),
                "transport_policy": transport_policy,
                "gateway_request_id": gateway_request_id,
            },
            external_ref=str(getattr(response, "id", "") or ""),
        )

    def _client(
        self,
        task: TaskEnvelope,
        *,
        proxy_url: str | None = None,
    ) -> Any:
        if self.client_factory is not None:
            return self.client_factory(task)
        from openai import OpenAI
        from httpx import Client

        if proxy_url is None:
            proxy_url = self._configured_loopback_proxy()
        client_options: dict[str, Any] = {
            "timeout": float(task.timeout_seconds),
            "trust_env": False,
        }
        if proxy_url:
            client_options["proxy"] = proxy_url
        http_client = Client(**client_options)
        try:
            api_key = os.environ.get(self.api_key_env, "").strip()
            client = OpenAI(
                api_key=api_key,
                admin_api_key="",
                organization="",
                project="",
                webhook_secret="",
                base_url=self.base_url,
                timeout=float(task.timeout_seconds),
                max_retries=0,
                default_headers={},
                default_query={},
                http_client=http_client,
            )
            # SDK 2.x merges OPENAI_CUSTOM_HEADERS after explicit constructor
            # options.  Scrub that private cache before the first request and
            # fail closed if a future SDK removes the isolation hook.
            if not isinstance(getattr(client, "_custom_headers", None), dict):
                client.close()
                raise RuntimeError("OpenAI SDK custom-header isolation is unavailable.")
            client._custom_headers = {}
            client.organization = None
            client.project = None
            client.admin_api_key = None
            return client
        except Exception:
            http_client.close()
            raise

    def _endpoint_origin(self) -> str:
        return "injected-client" if self.client_factory is not None else self.base_url

    def _configured_loopback_proxy(self) -> str:
        if self.client_factory is not None:
            return ""
        return validated_loopback_proxy_url(
            os.environ.get(self.proxy_env, "")
        )

    def _provider_kind(self) -> str:
        return (
            "injected_test_transport"
            if self.client_factory is not None
            else self.provider_kind
        )

    def _provider_profile(self) -> str:
        return (
            "injected_test_transport_v1"
            if self.client_factory is not None
            else self.provider_profile
        )

    def _provider_assurance(self) -> str:
        return (
            "injected_test_transport"
            if self.client_factory is not None
            else self.provider_assurance
        )

    def _transport_policy(self, proxy_url: str = "") -> str:
        if self.client_factory is not None:
            return "injected-client"
        return (
            "explicit_loopback_proxy_no_env"
            if proxy_url
            else "direct_no_env_proxy"
        )

    @staticmethod
    def _usage_metrics(usage: Any) -> dict[str, Any]:
        if hasattr(usage, "model_dump"):
            raw = usage.model_dump()
        elif isinstance(usage, dict):
            raw = usage
        else:
            raw = {}
        allowed = {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input_tokens_details",
            "output_tokens_details",
        }
        return {str(key): value for key, value in raw.items() if str(key) in allowed}

    def _request_error(
        self,
        exc: Exception,
        *,
        phase: str,
        model: str,
        transport_policy: str,
    ) -> TaskResult:
        error_type = type(exc).__name__
        status_code_value = getattr(exc, "status_code", None)
        status_code = (
            int(status_code_value)
            if isinstance(status_code_value, int)
            else None
        )
        provider_request_id = self._safe_provider_identifier(
            getattr(exc, "request_id", "")
        )
        provider_error_code = ""
        provider_error_type = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error_body = body.get("error")
            if not isinstance(error_body, dict):
                error_body = body
            provider_error_code = self._safe_provider_identifier(
                error_body.get("code", "")
            )
            provider_error_type = self._safe_provider_identifier(
                error_body.get("type", "")
            )
        if error_type in {"APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"}:
            code = "OPENAI_API_TIMEOUT"
            status = ResultStatus.RETRYABLE_FAILURE
        elif error_type in {"RateLimitError", "APIConnectionError", "InternalServerError"}:
            code = "OPENAI_API_RETRYABLE"
            status = ResultStatus.RETRYABLE_FAILURE
        elif error_type in {"AuthenticationError", "PermissionDeniedError"}:
            code = "OPENAI_API_AUTHORIZATION_FAILED"
            status = ResultStatus.BLOCKED
        elif error_type in {"BadRequestError", "UnprocessableEntityError"}:
            code = "OPENAI_API_REQUEST_REJECTED"
            status = ResultStatus.FATAL_FAILURE
        else:
            code = "OPENAI_API_REQUEST_FAILED"
            status = ResultStatus.RETRYABLE_FAILURE
        return TaskResult(
            status=status,
            blocker_code=code,
            message=f"Responses API {phase} failed with {error_type}.",
            data={
                "error_type": error_type,
                "model": model,
                "endpoint_origin": self._endpoint_origin(),
                "transport_policy": transport_policy,
                "http_status": status_code,
                "provider_request_id": provider_request_id,
                "provider_error_code": provider_error_code,
                "provider_error_type": provider_error_type,
            },
        )

    @staticmethod
    def _safe_provider_identifier(value: Any) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 128:
            return ""
        return text if re.fullmatch(r"[A-Za-z0-9_.:/-]+", text) else ""
