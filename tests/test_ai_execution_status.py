from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import unified_paper_orchestrator.__main__ as cli_entry
import unified_paper_orchestrator.ai_execution_status as status_module
import unified_paper_orchestrator.automation as automation_module
from unified_paper_orchestrator.ai_execution_status import collect_ai_execution_status
from unified_paper_orchestrator.ai_provider_store import AIProviderStore


class NonDecryptingCipher:
    scheme = "test"

    def __init__(self) -> None:
        self.decrypt_calls = 0

    def encrypt(self, _plaintext: str) -> str:
        return "opaque-test-ciphertext"

    def decrypt(self, _ciphertext: str) -> str:
        self.decrypt_calls += 1
        raise AssertionError("status command must not resolve a credential")


def _available_sdk() -> dict[str, object]:
    return {
        "distribution": "openai-codex",
        "module": "openai_codex",
        "version": "0.test",
        "module_path": "C:/sdk/openai_codex/__init__.py",
        "path_available": True,
        "available": True,
    }


def _available_runtime() -> dict[str, object]:
    return {
        "distribution": "openai-codex-cli-bin",
        "module": "codex_cli_bin",
        "version": "0.test",
        "module_path": "C:/sdk/codex_cli_bin/__init__.py",
        "module_path_available": True,
        "binary_path": "C:/sdk/codex_cli_bin/bin/codex.exe",
        "binary_path_available": True,
        "available": True,
    }


def _available_legacy() -> dict[str, object]:
    return {
        "ready": True,
        "four_stage_configured_count": 4,
        "stages": {},
        "smart_submission": {"configured": False, "status": "MISSING"},
        "blockers": [],
    }


def _ready_api_store(tmp_path: Path) -> tuple[AIProviderStore, NonDecryptingCipher, str]:
    cipher = NonDecryptingCipher()
    store = AIProviderStore(tmp_path, cipher=cipher)
    secret = "sk-must-never-appear-in-status"
    profile = store.create_profile(
        profile_id="sk-profile-id-must-also-stay-private",
        display_name="Active provider",
        base_url="https://provider.example.test/v1",
        default_model="private-model-name",
        custom_headers={"X-Tenant": "private-tenant"},
    )
    store.set_api_key(profile.profile_id, secret)
    profile = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
    )
    settings = store.set_active_profile(profile.profile_id, expected_revision=0)
    store.set_execution_mode("api_key", expected_revision=settings.revision)
    return store, cipher, secret


def test_status_reports_ready_api_profile_without_resolving_or_displaying_secret(tmp_path: Path) -> None:
    store, cipher, secret = _ready_api_store(tmp_path)

    result = collect_ai_execution_status(
        store,
        sdk_probe=_available_sdk,
        runtime_probe=_available_runtime,
        legacy_probe=_available_legacy,
    )

    assert result["ok"] is True
    assert result["mode"] == {
        "current": "api_key",
        "settings_readable": True,
        "active_mode_ready": True,
    }
    assert result["active_profile"]["ready"] is True
    assert result["active_profile"]["credential_present"] is True
    assert cipher.decrypt_calls == 0
    rendered = json.dumps(result, ensure_ascii=False)
    assert secret not in rendered
    assert "opaque-test-ciphertext" not in rendered
    assert "sk-profile-id-must-also-stay-private" not in rendered
    assert "provider.example.test" not in rendered
    assert "private-model-name" not in rendered
    assert "private-tenant" not in rendered
    assert result["execution_scope"]["writing_execution_owner"] == "local_codex_sdk_event_dispatcher"
    assert result["execution_scope"]["writing_account_checked"] is False


def test_status_fails_profile_readiness_when_runtime_is_unavailable(tmp_path: Path) -> None:
    store, _cipher, _secret = _ready_api_store(tmp_path)
    runtime = _available_runtime()
    runtime.update({"available": False, "binary_path_available": False})

    result = collect_ai_execution_status(
        store,
        sdk_probe=_available_sdk,
        runtime_probe=lambda: runtime,
        legacy_probe=_available_legacy,
    )

    assert result["ok"] is True
    assert result["mode"]["active_mode_ready"] is False
    assert result["active_profile"]["ready"] is False
    assert "CODEX_RUNTIME_UNAVAILABLE" in result["active_profile"]["blockers"]


def test_codex_account_mode_is_ready_from_local_sdk_without_legacy_bridge_state(tmp_path: Path) -> None:
    provider_root = tmp_path / "missing-provider-state"
    store = AIProviderStore(provider_root, cipher=NonDecryptingCipher())

    result = collect_ai_execution_status(
        store,
        sdk_probe=_available_sdk,
        runtime_probe=_available_runtime,
        legacy_probe=lambda: {
            **_available_legacy(),
            "ready": False,
            "four_stage_configured_count": 0,
            "blockers": ["LEGACY_FOUR_STAGE_AUTOMATION_MISSING"],
        },
    )

    assert result["mode"]["current"] == "legacy_codex"
    assert result["mode"]["active_mode_ready"] is True
    assert result["execution_scope"]["codex_account_full_four_stage_execution"] is True
    assert result["execution_scope"]["remaining_v1_stage_owner"] == "none"
    assert result["execution_scope"]["full_four_stage_execution_owner"] == (
        "local_codex_sdk_workflows"
    )
    assert provider_root.exists() is False


def test_codex_account_mode_still_fails_closed_when_local_runtime_is_missing(
    tmp_path: Path,
) -> None:
    provider_root = tmp_path / "missing-provider-state"
    store = AIProviderStore(provider_root, cipher=NonDecryptingCipher())
    runtime = _available_runtime()
    runtime.update({"available": False, "binary_path_available": False})

    result = collect_ai_execution_status(
        store,
        sdk_probe=_available_sdk,
        runtime_probe=lambda: runtime,
        legacy_probe=_available_legacy,
    )

    assert result["mode"]["current"] == "legacy_codex"
    assert result["mode"]["active_mode_ready"] is False
    assert provider_root.exists() is False


def test_legacy_toml_files_do_not_prove_a_live_desktop_consumer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    automations_root = tmp_path / "automations"
    for project in automation_module.LEGACY_PROJECTS.values():
        path = automations_root / str(project["primary_automation_id"]) / "automation.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            '\n'.join(
                (
                    f'id = "{project["primary_automation_id"]}"',
                    'kind = "heartbeat"',
                    'status = "PAUSED"',
                    '',
                )
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", automations_root)

    legacy = status_module._probe_legacy_bridge()

    assert legacy["four_stage_configured_count"] == 4
    assert legacy["consumer_verified"] is False
    assert legacy["ready"] is False
    assert legacy["blockers"] == ["LEGACY_AUTOMATION_CONSUMER_UNVERIFIED"]


def test_status_store_error_uses_a_fixed_code_without_echoing_exception_text(tmp_path: Path) -> None:
    store = AIProviderStore(tmp_path, cipher=NonDecryptingCipher())
    sensitive_marker = "sk-corrupt-state-marker"
    store.settings_path.parent.mkdir(parents=True, exist_ok=True)
    store.settings_path.write_text(sensitive_marker, encoding="utf-8")

    result = collect_ai_execution_status(
        store,
        sdk_probe=_available_sdk,
        runtime_probe=_available_runtime,
        legacy_probe=_available_legacy,
    )

    assert result["ok"] is False
    assert result["error_code"] == "AI_EXECUTION_SETTINGS_UNAVAILABLE"
    assert sensitive_marker not in json.dumps(result)


def test_cli_status_is_lazy_and_does_not_construct_the_v1_orchestrator(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        status_module,
        "collect_ai_execution_status",
        lambda: {
            "schema": status_module.STATUS_SCHEMA,
            "ok": True,
            "mode": {"current": "legacy_codex"},
        },
    )
    monkeypatch.setattr(
        cli_entry,
        "UnifiedOrchestrator",
        lambda: (_ for _ in ()).throw(AssertionError("V1 orchestrator must stay lazy")),
    )

    assert cli_entry.main(["ai-execution-status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"]["current"] == "legacy_codex"


def test_cli_status_exception_never_echoes_the_exception_message(monkeypatch, capsys) -> None:
    secret = "sk-exception-secret"

    def fail_safely():
        raise RuntimeError(f"provider failed with {secret}")

    monkeypatch.setattr(status_module, "collect_ai_execution_status", fail_safely)

    assert cli_entry.main(["ai-execution-status"]) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["error_code"] == "AI_EXECUTION_STATUS_FAILED"
    assert payload["error_type"] == "RuntimeError"
    assert secret not in output


def test_dependency_probes_keep_versions_in_a_frozen_style_bundle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(status_module, "runtime_identity", lambda: None)
    sdk_init = tmp_path / "openai_codex" / "__init__.py"
    runtime_init = tmp_path / "codex_cli_bin" / "__init__.py"
    runtime_binary = tmp_path / "codex_cli_bin" / "bin" / "codex.exe"
    sdk_init.parent.mkdir()
    runtime_binary.parent.mkdir(parents=True)
    sdk_init.write_text("", encoding="utf-8")
    runtime_init.write_text("", encoding="utf-8")
    runtime_binary.write_bytes(b"test")
    (runtime_init.parent / "codex-package.json").write_text(
        json.dumps({"version": "0.137.0-alpha.4"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(status_module, "_distribution_version", lambda _name: "")
    monkeypatch.setattr(
        status_module,
        "_module_path",
        lambda name: (
            (str(sdk_init), True)
            if name == status_module.SDK_MODULE
            else (str(runtime_init), True)
        ),
    )

    def import_module(name: str):
        if name == status_module.SDK_MODULE:
            return SimpleNamespace(__version__="0.1.0b3")
        return SimpleNamespace(bundled_codex_path=lambda: runtime_binary)

    monkeypatch.setattr(status_module.importlib, "import_module", import_module)

    sdk = status_module._probe_codex_sdk()
    runtime = status_module._probe_codex_runtime()

    assert sdk["version"] == "0.1.0b3"
    assert sdk["available"] is True
    assert runtime["version"] == "0.137.0-alpha.4"
    assert runtime["binary_path"] == str(runtime_binary)
    assert runtime["available"] is True
