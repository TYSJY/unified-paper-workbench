from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from unified_paper_orchestrator_v2.provider_runtime import (
    RUNTIME_API_KEY_ENV,
    ProviderConfigurationError,
    ProviderProbeResponse,
    RuntimeProviderConfig,
    isolated_provider_runtime,
    probe_provider_compatibility,
    provider_config_digest,
    render_codex_config,
    responses_endpoint_url,
    validate_provider_config,
)


SECRET = "provider-secret-never-written"


def _config(**overrides: object) -> RuntimeProviderConfig:
    values: dict[str, object] = {
        "profile_id": "research_relay",
        "display_name": "Research Responses Relay",
        "base_url": "https://relay.example.test/v1",
        "wire_api": "responses",
        "model": "codex-research-1",
        "reasoning_effort": "high",
        "proxy_url": "",
        "custom_headers": {"X-Tenant": "paper-team"},
    }
    values.update(overrides)
    return RuntimeProviderConfig(**values)  # type: ignore[arg-type]


def test_local_validation_is_secret_free_and_responses_only() -> None:
    config = _config()
    result = validate_provider_config(config)

    assert result.compatible is True
    assert result.network_attempted is False
    assert result.provider_digest == provider_config_digest(config)
    assert len(result.provider_digest) == 64
    assert "qilin" not in render_codex_config(config).lower()

    invalid = validate_provider_config(_config(wire_api="chat"))
    assert invalid.compatible is False
    assert invalid.code == "PROVIDER_WIRE_API_UNSUPPORTED"
    with pytest.raises(ProviderConfigurationError, match="responses"):
        render_codex_config(_config(wire_api="chat"))


def test_empty_reasoning_effort_uses_provider_or_model_default() -> None:
    config = _config(reasoning_effort="")

    rendered = render_codex_config(config)

    assert "model_reasoning_effort" not in rendered


def test_responses_endpoint_url_preserves_explicit_api_prefix_without_guessing() -> None:
    assert responses_endpoint_url("https://www.qilinapi.com") == "https://www.qilinapi.com/responses"
    assert responses_endpoint_url("https://relay.example.test/v1/") == "https://relay.example.test/v1/responses"


def test_isolated_runtime_writes_no_key_or_desktop_identity_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = tmp_path / "desktop"
    desktop_codex = desktop / ".codex"
    desktop_codex.mkdir(parents=True)
    sentinel = desktop_codex / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("HOME", str(desktop))
    monkeypatch.setenv("USERPROFILE", str(desktop))
    monkeypatch.setenv("CODEX_HOME", str(desktop_codex))
    monkeypatch.setenv("OPENAI_API_KEY", "desktop-openai-secret")
    monkeypatch.setenv("CODEX_API_KEY", "desktop-codex-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://desktop-proxy.invalid:8080")
    before = dict(os.environ)
    runtime_root: Path
    environment: object

    with isolated_provider_runtime(_config(), SECRET, temp_root=tmp_path / "runtime") as runtime:
        runtime_root = runtime.root
        environment = runtime.environment
        parsed = tomllib.loads(runtime.config_path.read_text(encoding="utf-8"))
        provider = parsed["model_providers"]["research_relay"]

        assert runtime.codex_home != desktop_codex
        assert runtime.config_path.parent == runtime.codex_home
        assert provider["env_key"] == runtime.env_key
        assert runtime.env_key.startswith(RUNTIME_API_KEY_ENV + "_")
        assert provider["wire_api"] == "responses"
        assert provider["requires_openai_auth"] is False
        assert provider["request_max_retries"] == 0
        assert provider["stream_max_retries"] == 0
        assert environment[runtime.env_key] == SECRET  # type: ignore[index]
        assert environment["OPENAI_API_KEY"] == ""  # type: ignore[index]
        assert environment["CODEX_API_KEY"] == ""  # type: ignore[index]
        assert environment["HTTP_PROXY"] == ""  # type: ignore[index]
        serialized = runtime.config_path.read_text(encoding="utf-8")
        assert SECRET not in serialized
        assert "desktop-openai-secret" not in serialized

    assert not runtime_root.exists()
    assert len(environment) == 0  # type: ignore[arg-type]
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert dict(os.environ) == before


def test_runtime_rejects_user_codex_as_temp_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    with pytest.raises(ProviderConfigurationError) as exc_info:
        with isolated_provider_runtime(_config(), SECRET, temp_root=home / ".codex"):
            pass
    assert exc_info.value.code == "PROVIDER_RUNTIME_HOME_FORBIDDEN"


def test_explicit_probe_is_one_non_billable_responses_request() -> None:
    requests = []

    def transport(request: object) -> ProviderProbeResponse:
        requests.append(request)
        return ProviderProbeResponse(status_code=204)

    result = probe_provider_compatibility(_config(), api_key=SECRET, transport=transport)

    assert result.compatible is True
    assert result.network_attempted is True
    assert result.request_count == 1
    assert result.probe_method == "OPTIONS"
    assert result.billable_request is False
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "OPTIONS"
    assert request.url == "https://relay.example.test/v1/responses"
    assert request.body == b""
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(result.to_dict())


def test_invalid_local_config_never_calls_probe_transport() -> None:
    calls = []

    result = probe_provider_compatibility(
        _config(wire_api="chat"),
        transport=lambda request: calls.append(request) or ProviderProbeResponse(204),
    )

    assert result.compatible is False
    assert result.phase == "validate"
    assert result.network_attempted is False
    assert calls == []


def test_probe_accepts_httpx_transport_without_retry() -> None:
    import httpx

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    result = probe_provider_compatibility(
        _config(),
        transport=httpx.MockTransport(handler),
    )

    assert result.compatible is True
    assert len(calls) == 1
    assert calls[0].method == "OPTIONS"
    assert str(calls[0].url) == "https://relay.example.test/v1/responses"
