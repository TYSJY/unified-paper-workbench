from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unified_paper_orchestrator_v2.contracts import ResultStatus, TaskEnvelope
from unified_paper_orchestrator_v2.executors.codex_sdk import CodexSDKExecutor
from unified_paper_orchestrator_v2.provider_runtime import RuntimeProviderConfig, provider_config_digest


SECRET = "sdk-provider-secret"


def _config() -> RuntimeProviderConfig:
    return RuntimeProviderConfig(
        profile_id="generic_responses",
        display_name="Generic Responses",
        base_url="https://provider.example.test/v1",
        model="trusted-codex-model",
        reasoning_effort="xhigh",
    )


def _task(**payload: object) -> TaskEnvelope:
    return TaskEnvelope(
        paper_id="paper-provider-runtime",
        stage="writing",
        action="writing.draft",
        executor="codex_sdk",
        payload={"prompt": "Return a short result.", **payload},
    )


class _Thread:
    id = "thread-runtime-test"

    def __init__(self, capture: dict[str, Any]) -> None:
        self.capture = capture

    def run(self, prompt: str) -> object:
        self.capture["prompt"] = prompt
        return SimpleNamespace(
            id="turn-runtime-test",
            status="completed",
            final_response="done",
            usage=SimpleNamespace(
                total=SimpleNamespace(
                    input_tokens=10,
                    cached_input_tokens=2,
                    output_tokens=3,
                    reasoning_output_tokens=1,
                    total_tokens=13,
                )
            ),
        )


class _CodexContext:
    def __init__(self, capture: dict[str, Any]) -> None:
        self.capture = capture

    def __enter__(self) -> "_CodexContext":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def thread_start(self, **kwargs: object) -> _Thread:
        self.capture["thread_kwargs"] = kwargs
        return _Thread(self.capture)


def test_executor_uses_injected_provider_runtime_and_cleans_it(tmp_path: Path) -> None:
    capture: dict[str, Any] = {}

    def factory(runtime: object) -> _CodexContext:
        capture["root"] = runtime.root
        capture["config_text"] = runtime.config_path.read_text(encoding="utf-8")
        capture["environment"] = runtime.environment
        capture["env_key"] = runtime.env_key
        assert runtime.environment[runtime.env_key] == SECRET
        return _CodexContext(capture)

    config = _config()
    executor = CodexSDKExecutor(
        factory,
        provider_config=config,
        key_resolver=lambda profile_id: SECRET if profile_id == config.profile_id else "",
        runtime_temp_root=tmp_path,
    )
    result = executor.execute(_task(model="untrusted-task-model"))

    assert result.status == ResultStatus.COMPLETED
    assert capture["thread_kwargs"] == {
        "sandbox": "read_only",
        "model": "trusted-codex-model",
        "model_provider": "generic_responses",
    }
    assert result.data["provider_profile"] == "generic_responses"
    assert result.data["provider_digest"] == provider_config_digest(config)
    assert result.data["provider_config_sha256"] == result.data["provider_digest"]
    assert result.data["usage"]["total_tokens"] == 13
    assert SECRET not in str(result.to_dict())
    assert SECRET not in capture["config_text"]
    assert not capture["root"].exists()
    assert len(capture["environment"]) == 0


def test_executor_rejects_task_endpoint_before_key_or_sdk_use() -> None:
    calls: list[str] = []
    executor = CodexSDKExecutor(
        lambda runtime: calls.append("factory") or _CodexContext({}),
        provider_config=_config(),
        key_resolver=lambda profile_id: calls.append(profile_id) or SECRET,
    )

    result = executor.execute(_task(endpoint_base_url="https://attacker.invalid/v1"))

    assert result.status == ResultStatus.BLOCKED
    assert result.blocker_code == "CODEX_PROVIDER_OVERRIDE_FORBIDDEN"
    assert calls == []


def test_legacy_zero_argument_factory_remains_compatible() -> None:
    capture: dict[str, Any] = {}
    executor = CodexSDKExecutor(lambda: _CodexContext(capture))

    result = executor.execute(_task(model="legacy-model"))

    assert result.status == ResultStatus.COMPLETED
    assert capture["thread_kwargs"]["model"] == "legacy-model"
    assert "model_provider" not in capture["thread_kwargs"]
