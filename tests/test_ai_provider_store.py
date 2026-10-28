from __future__ import annotations

import json
from pathlib import Path

import pytest

from unified_paper_orchestrator.ai_provider_store import (
    API_KEY,
    LEGACY_CODEX,
    AIProviderStore,
    ActiveProfileError,
    ProviderSecretError,
    ProviderStoreBusyError,
    ProviderValidationError,
    RevisionConflictError,
    normalize_provider_url,
)
from unified_paper_orchestrator.io_utils import FileLock


class ReversingCipher:
    scheme = "test-reversing-cipher"

    def encrypt(self, plaintext: str) -> str:
        return "cipher:" + plaintext[::-1]

    def decrypt(self, ciphertext: str) -> str:
        assert ciphertext.startswith("cipher:")
        return ciphertext.removeprefix("cipher:")[::-1]


def make_store(tmp_path: Path, **kwargs) -> AIProviderStore:
    return AIProviderStore(tmp_path, cipher=ReversingCipher(), **kwargs)


def create_profile(store: AIProviderStore, *, profile_id: str = "primary"):
    return store.create_profile(
        profile_id=profile_id,
        display_name="Primary relay",
        base_url="HTTPS://API.Example.COM:443/custom/v2/",
        wire_api="responses",
        auth_scheme="bearer",
        default_model="gpt-example",
        reasoning_effort="high",
        proxy_url="http://127.0.0.1:8080/",
        custom_headers={"X-Tenant": "paper-team"},
    )


def test_settings_default_to_legacy_and_use_revision_cas(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    initial = store.get_settings()
    assert initial.execution_mode == LEGACY_CODEX
    assert initial.active_profile_id is None
    assert initial.revision == 0

    profile = create_profile(store)
    store.set_secret(profile.profile_id, "sk-test")
    profile = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
    )
    selected = store.set_active_profile(profile.profile_id, expected_revision=0)
    updated = store.set_execution_mode(API_KEY, expected_revision=selected.revision)
    assert updated.execution_mode == API_KEY
    assert updated.revision == 2
    with pytest.raises(RevisionConflictError):
        store.set_execution_mode(LEGACY_CODEX, expected_revision=0)
    with pytest.raises(ProviderValidationError):
        store.set_execution_mode("automatic", expected_revision=updated.revision)


def test_api_mode_cannot_be_enabled_without_profile_key_and_compatibility(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ActiveProfileError):
        store.set_execution_mode(API_KEY, expected_revision=0)

    profile = create_profile(store)
    settings = store.set_active_profile(profile.profile_id, expected_revision=0)
    with pytest.raises(ActiveProfileError):
        store.set_execution_mode(API_KEY, expected_revision=settings.revision)

    store.set_api_key(profile.profile_id, "sk-test")
    with pytest.raises(ActiveProfileError):
        store.set_execution_mode(API_KEY, expected_revision=settings.revision)

    compatible = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
    )
    assert store.has_api_key(compatible.profile_id) is True
    assert store.resolve_api_key(compatible.profile_id) == "sk-test"


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.com/v1",
        "ftp://api.example.com/v1",
        "https:///v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?debug=1",
        "https://api.example.com/v1#fragment",
    ],
)
def test_url_validation_rejects_unsafe_or_ambiguous_urls(value: str) -> None:
    with pytest.raises(ProviderValidationError):
        normalize_provider_url(value)


def test_url_normalization_allows_explicit_loopback_and_never_guesses_v1() -> None:
    assert normalize_provider_url("https://API.Example.COM:443/") == "https://api.example.com"
    assert normalize_provider_url("http://localhost:8000/custom/") == "http://localhost:8000/custom"
    assert normalize_provider_url("http://[::1]:8080/") == "http://[::1]:8080"


def test_profile_crud_active_selection_and_cas(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    profile = create_profile(store)
    assert profile.revision == 1
    assert profile.base_url == "https://api.example.com/custom/v2"
    assert profile.proxy_url == "http://127.0.0.1:8080"
    assert store.list_profiles() == [profile]

    updated = store.update_profile(profile.profile_id, expected_revision=1, display_name="Renamed")
    assert updated.revision == 2
    assert updated.display_name == "Renamed"
    with pytest.raises(RevisionConflictError):
        store.update_profile(profile.profile_id, expected_revision=1, display_name="Stale")

    settings = store.set_active_profile(profile.profile_id, expected_revision=0)
    assert settings.active_profile_id == profile.profile_id
    with pytest.raises(ActiveProfileError):
        store.delete_profile(profile.profile_id, expected_revision=2)
    settings = store.set_active_profile(None, expected_revision=settings.revision)
    assert settings.active_profile_id is None
    store.delete_profile(profile.profile_id, expected_revision=2)
    assert store.list_profiles() == []


def test_custom_headers_cannot_be_used_as_a_secret_side_channel(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ProviderValidationError, match="non-sensitive"):
        store.create_profile(
            display_name="Bad",
            base_url="https://api.example.com",
            default_model="model",
            custom_headers={"Authorization": "Bearer should-not-be-here"},
        )
    with pytest.raises(ProviderValidationError, match="unsupported fields"):
        store.create_profile(
            display_name="Bad",
            base_url="https://api.example.com",
            default_model="model",
            api_key="must-not-enter-profile-json",
        )


def test_api_key_is_encrypted_in_a_separate_file_and_never_in_profile_dict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    profile = create_profile(store)
    secret = "sk-test-DO-NOT-LEAK"

    store.set_secret(profile.profile_id, secret)

    assert store.has_secret(profile.profile_id) is True
    assert store.resolve_secret(profile.profile_id) == secret
    assert secret not in json.dumps(profile.to_dict())
    assert secret not in store.profiles_path.read_text(encoding="utf-8")
    assert secret not in store.settings_path.read_text(encoding="utf-8") if store.settings_path.exists() else True
    secret_document = store.secrets_path.read_text(encoding="utf-8")
    assert secret not in secret_document
    assert "ciphertext" in secret_document

    assert store.delete_secret(profile.profile_id) is True
    assert store.has_secret(profile.profile_id) is False
    assert store.delete_secret(profile.profile_id) is False
    with pytest.raises(ProviderSecretError):
        store.resolve_secret(profile.profile_id)


def test_deleting_a_profile_also_deletes_its_encrypted_secret(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    profile = create_profile(store)
    store.set_secret(profile.profile_id, "sk-delete-me")

    store.delete_profile(profile.profile_id, expected_revision=profile.revision)

    assert store.has_secret(profile.profile_id) is False


def test_capability_result_is_persisted_without_running_a_probe(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    profile = create_profile(store)

    tested = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
        tested_at="2026-07-12T12:30:00+08:00",
    )

    assert tested.capability_status == "compatible"
    assert tested.last_tested_at == "2026-07-12T12:30:00+08:00"
    reloaded = make_store(tmp_path).get_profile(profile.profile_id)
    assert reloaded == tested


def test_store_uses_one_cross_process_lock_for_all_documents(tmp_path: Path) -> None:
    store = make_store(tmp_path, lock_timeout_seconds=0)
    with FileLock(store.lock_path):
        with pytest.raises(ProviderStoreBusyError):
            store.get_settings()
