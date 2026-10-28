from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .ai_execution_policy import evaluate_execution_mode_switch
from .ai_provider_store import AIProviderSettings, AIProviderStore, ProviderProfile
from unified_paper_orchestrator_v2.config import DATABASE_PATH
from unified_paper_orchestrator_v2.contracts import (
    ResultStatus,
    TaskEnvelope,
    TaskResult,
    canonical_json,
)
from unified_paper_orchestrator_v2.executors.codex_sdk import CodexSDKExecutor
from unified_paper_orchestrator_v2.executors.registry import build_default_registry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway
from unified_paper_orchestrator_v2.provider_runtime import (
    RuntimeProviderConfig,
    responses_endpoint_url,
    validate_provider_config,
)
from unified_paper_orchestrator_v2.review_canary import (
    build_codex_sdk_profile_review_canary_envelope,
)


PROVIDER_COMPATIBILITY_PROMPT = (
    "This is a connection compatibility check. Do not call tools, read files, "
    "or modify anything. Reply with exactly: PROVIDER_OK"
)
PROVIDER_COMPATIBILITY_TOKEN_BUDGET = 32_768


class AIExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        converted = asdict(value)
        return dict(converted) if isinstance(converted, dict) else {}
    return {}


def runtime_provider_config(profile: ProviderProfile | Mapping[str, Any]) -> RuntimeProviderConfig:
    record = _record(profile)
    return RuntimeProviderConfig(
        profile_id=str(record.get("profile_id") or ""),
        display_name=str(record.get("display_name") or ""),
        base_url=str(record.get("base_url") or ""),
        wire_api=str(record.get("wire_api") or "responses"),
        model=str(record.get("default_model") or ""),
        reasoning_effort=str(record.get("reasoning_effort") or ""),
        proxy_url=str(record.get("proxy_url") or ""),
        custom_headers=dict(record.get("custom_headers") or {}),
    )


def read_gateway_task_counts(database_path: Path = DATABASE_PATH) -> dict[str, int]:
    """Read task counts without creating or migrating the V2 database."""

    path = Path(database_path)
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status"
            ).fetchall()
    except (OSError, sqlite3.Error):
        # An unreadable task ledger must block a mode change instead of being
        # mistaken for an idle execution surface.
        return {"running": 1}
    return {str(status): int(count) for status, count in rows}


class AIExecutionCoordinator:
    """Guarded product-facing facade over provider storage and SDK testing."""

    def __init__(
        self,
        store: AIProviderStore | None = None,
        *,
        legacy_status_reader: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        gateway_task_counts_reader: Callable[[], Mapping[str, int]] | None = None,
        sdk_executor_factory: Callable[
            [RuntimeProviderConfig, Callable[[str], str]], Any
        ]
        | None = None,
    ) -> None:
        self.store = store or AIProviderStore()
        self.legacy_status_reader = legacy_status_reader or (lambda: {})
        self.gateway_task_counts_reader = gateway_task_counts_reader or read_gateway_task_counts
        self.sdk_executor_factory = sdk_executor_factory or self._default_sdk_executor

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    @staticmethod
    def _default_sdk_executor(
        config: RuntimeProviderConfig,
        key_resolver: Callable[[str], str],
    ) -> CodexSDKExecutor:
        return CodexSDKExecutor(
            provider_config=config,
            key_resolver=key_resolver,
        )

    def set_execution_mode(
        self,
        execution_mode: str,
        *,
        expected_revision: int,
    ) -> AIProviderSettings:
        def validate_switch(
            settings: AIProviderSettings,
            requested_mode: str,
            profile_value: ProviderProfile | None,
            has_key: bool,
        ) -> None:
            profile = profile_value.to_dict() if profile_value is not None else {}
            try:
                legacy_statuses = dict(self.legacy_status_reader() or {})
            except Exception:
                legacy_statuses = {"status_reader": {"status": "ACTIVE"}}
            try:
                gateway_counts = dict(self.gateway_task_counts_reader() or {})
            except Exception:
                gateway_counts = {"running": 1}
            decision = evaluate_execution_mode_switch(
                current_mode=settings.execution_mode,
                requested_mode=requested_mode,
                legacy_scheduler_statuses=legacy_statuses,
                gateway_task_counts=gateway_counts,
                active_profile=profile,
                active_profile_has_key=has_key,
            )
            decision.require_allowed()

        guarded_setter = getattr(self.store, "set_execution_mode_guarded", None)
        if callable(guarded_setter):
            return guarded_setter(
                execution_mode,
                expected_revision=expected_revision,
                guard=validate_switch,
            )

        # Compatibility path for injected UI/test stores.  The production
        # AIProviderStore always uses the atomic guarded setter above.
        settings = self.store.load_settings()
        profile: ProviderProfile | None = None
        has_key = False
        if settings.active_profile_id:
            try:
                profile = self.store.get_profile(settings.active_profile_id)
                has_key = self.store.has_api_key(settings.active_profile_id)
            except Exception:
                profile = None
                has_key = False
        validate_switch(settings, execution_mode, profile, has_key)
        if settings.execution_mode == execution_mode:
            return settings
        return self.store.set_execution_mode(
            execution_mode,
            expected_revision=expected_revision,
        )

    def local_profile_check(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get_profile(profile_id)
        result = validate_provider_config(runtime_provider_config(profile))
        return result.to_dict()

    def test_profile(self, profile_id: str) -> dict[str, Any]:
        """Run one explicit, billable, tool-free SDK canary and persist only its status."""

        profile = self.store.get_profile(profile_id)
        config = runtime_provider_config(profile)
        request_url = responses_endpoint_url(config.base_url)
        local = validate_provider_config(config)
        if not local.compatible:
            self.store.record_capability_result(
                profile_id,
                "incompatible",
                expected_revision=profile.revision,
            )
            raise AIExecutionError(local.code, local.message)
        if not self.store.has_api_key(profile_id):
            raise AIExecutionError(
                "AI_PROVIDER_KEY_REQUIRED",
                "请先保存当前 API 配置的密钥。",
            )

        executor = self.sdk_executor_factory(config, self.store.resolve_api_key)
        task = TaskEnvelope(
            paper_id="provider-compatibility-check",
            stage="system",
            action="system.provider_compatibility_check",
            executor="codex_sdk",
            payload={
                "prompt": PROVIDER_COMPATIBILITY_PROMPT,
                "sandbox": "read_only",
            },
            allowed_tools=[],
            network_access=True,
            token_budget=PROVIDER_COMPATIBILITY_TOKEN_BUDGET,
            timeout_seconds=120,
        )
        try:
            result: TaskResult = executor.execute(task)
        except Exception as exc:
            current = self.store.get_profile(profile_id)
            self.store.record_capability_result(
                profile_id,
                "error",
                expected_revision=current.revision,
            )
            raise AIExecutionError(
                "AI_PROVIDER_TEST_FAILED",
                "连接测试未完成。",
                details={
                    "request_url": request_url,
                    "failure_type": type(exc).__name__,
                },
            ) from None

        final_response = str(result.data.get("final_response") or "").strip()
        compatible = (
            result.status is ResultStatus.COMPLETED
            and final_response == "PROVIDER_OK"
            and bool(str(result.external_ref or "").strip())
        )
        blocker_code = result.blocker_code or "AI_PROVIDER_RESPONSE_INVALID"
        capability_status = (
            "incompatible"
            if result.status is ResultStatus.COMPLETED
            else "error"
        )
        current = self.store.get_profile(profile_id)
        tested = self.store.record_capability_result(
            profile_id,
            "compatible" if compatible else capability_status,
            expected_revision=current.revision,
        )
        response = {
            "profile_id": profile_id,
            "capability_status": tested.capability_status,
            "last_tested_at": tested.last_tested_at,
            "provider_digest": config.digest(),
            "sdk_status": result.status.value,
            "blocker_code": result.blocker_code,
            "thread_id": str(result.external_ref or ""),
            "billable_request": True,
            "request_count": 1,
            "request_url": request_url,
        }
        if not compatible:
            details: dict[str, Any] = {
                "request_url": request_url,
                "sdk_status": result.status.value,
                "blocker_code": blocker_code,
            }
            message = "API 服务未通过完整 Codex SDK 兼容性测试。"
            if blocker_code == "CODEX_SDK_TOKEN_BUDGET_EXCEEDED":
                details.update(
                    {
                        "token_budget": PROVIDER_COMPATIBILITY_TOKEN_BUDGET,
                        "total_tokens": int(result.metrics.get("total_tokens") or 0),
                    }
                )
                message = (
                    "API 连接和 Codex SDK 回合已成功，"
                    "但本地兼容性检测预算过低，输出被安全丢弃。"
                )
            raise AIExecutionError(
                blocker_code,
                message,
                details=details,
            )
        return response

    def _active_api_runtime(self) -> tuple[AIProviderSettings, ProviderProfile, RuntimeProviderConfig]:
        settings = self.store.load_settings()
        if settings.execution_mode != "api_key" or not settings.active_profile_id:
            raise AIExecutionError(
                "AI_API_MODE_NOT_ACTIVE",
                "请先在“AI执行与API”中启用 API 密钥模式。",
            )
        profile = self.store.get_profile(settings.active_profile_id)
        if profile.capability_status != "compatible" or not self.store.has_api_key(
            profile.profile_id
        ):
            raise AIExecutionError(
                "AI_PROVIDER_NOT_READY",
                "当前 API 配置尚未通过完整兼容性测试。",
            )
        return settings, profile, runtime_provider_config(profile)

    def _profile_gateway(self) -> tuple[ExecutionGateway, ProviderProfile, RuntimeProviderConfig]:
        _settings, profile, config = self._active_api_runtime()
        executor = self.sdk_executor_factory(config, self.store.resolve_api_key)
        if type(executor) is not CodexSDKExecutor:
            raise AIExecutionError(
                "AI_CODEX_SDK_EXECUTOR_INVALID",
                "正式 API 审阅必须使用内置 Codex SDK 执行器。",
            )
        registry = build_default_registry(codex_sdk_executor=executor)
        return ExecutionGateway(registry=registry), profile, config

    def queue_profile_review(self, paper_id: str) -> dict[str, Any]:
        preview = self.prepare_profile_review(paper_id)
        return self.submit_prepared_profile_review(preview)

    def prepare_profile_review(self, paper_id: str) -> dict[str, Any]:
        """Build the exact durable intent without creating a task or calling a model."""

        gateway, _profile, config = self._profile_gateway()
        envelope = build_codex_sdk_profile_review_canary_envelope(
            str(paper_id or "").strip(),
            provider_config=config,
            writing_root=gateway.review_writing_root,
            staging_root=gateway.staging.root,
        )
        envelope = TaskEnvelope.from_dict(
            {**envelope.to_dict(), "dispatch_owner": "gateway"}
        )
        intent_sha256 = hashlib.sha256(
            canonical_json(envelope.to_dict()).encode("utf-8")
        ).hexdigest()
        return {
            "envelope": envelope,
            "intent_sha256": intent_sha256,
            "intent": {
                "paper_id": envelope.paper_id,
                "provider_profile": envelope.payload.get("provider_profile", ""),
                "endpoint_base_url": envelope.payload.get("endpoint_base_url", ""),
                "model": envelope.payload.get("model", ""),
                "token_budget": envelope.token_budget,
                "third_party_data_disclosure_ack": envelope.payload.get(
                    "third_party_data_disclosure_ack"
                ),
            },
        }

    def submit_prepared_profile_review(
        self,
        preview: Mapping[str, Any],
    ) -> dict[str, Any]:
        envelope = preview.get("envelope")
        if not isinstance(envelope, TaskEnvelope):
            raise AIExecutionError(
                "AI_PROFILE_REVIEW_PREVIEW_INVALID",
                "API 审阅预览已经失效，请重新操作。",
            )
        observed = hashlib.sha256(
            canonical_json(envelope.to_dict()).encode("utf-8")
        ).hexdigest()
        if observed != str(preview.get("intent_sha256") or ""):
            raise AIExecutionError(
                "AI_PROFILE_REVIEW_INTENT_CHANGED",
                "API 审阅意图在确认后发生变化，已停止。",
            )
        gateway, _profile, config = self._profile_gateway()
        expected_provider = envelope.payload.get("endpoint_config_sha256")
        if expected_provider != config.digest():
            raise AIExecutionError(
                "AI_PROFILE_REVIEW_PROVIDER_CHANGED",
                "API 服务配置在确认后发生变化，已停止。",
            )
        return gateway.submit(envelope)

    def approve_profile_review(
        self,
        task_id: str,
        *,
        intent_sha256: str,
    ) -> dict[str, Any]:
        gateway, _profile, _config = self._profile_gateway()
        return gateway.approve(
            str(task_id or "").strip(),
            expected_intent_sha256=str(intent_sha256 or "").strip(),
            note="approved in EXE for the selected configured API provider",
        )

    def run_profile_review(self, task_id: str) -> dict[str, Any]:
        gateway, _profile, _config = self._profile_gateway()
        result = gateway.run_once(str(task_id or "").strip())
        if result is None:
            raise AIExecutionError(
                "AI_PROFILE_REVIEW_NOT_QUEUED",
                "没有可执行的 API 审阅任务。",
            )
        return result

    def cancel_profile_review(self, task_id: str) -> dict[str, Any]:
        gateway, _profile, _config = self._profile_gateway()
        return gateway.store.cancel_task(
            str(task_id or "").strip(),
            note="cancelled from EXE before provider dispatch",
            dispatch_owner="gateway",
        )


__all__ = [
    "AIExecutionCoordinator",
    "AIExecutionError",
    "PROVIDER_COMPATIBILITY_TOKEN_BUDGET",
    "PROVIDER_COMPATIBILITY_PROMPT",
    "read_gateway_task_counts",
    "runtime_provider_config",
]
