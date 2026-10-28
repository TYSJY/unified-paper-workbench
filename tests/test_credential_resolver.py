from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from unified_paper_orchestrator.credential_resolver import (
    CredentialResolutionError,
    SmartCredentialResolver,
)
from unified_paper_orchestrator.credential_vault import VaultError


class FakeVault:
    def __init__(self, value: str = "", error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[str] = []

    def reveal(self, credential_ref: str) -> str:
        self.calls.append(credential_ref)
        if self.error is not None:
            raise self.error
        return self.value


def test_dpapi_reference_takes_priority_over_retained_plaintext() -> None:
    vault = FakeVault("dpapi-secret")
    resolver = SmartCredentialResolver(Path("unused.json"), vault=vault)

    resolved = resolver.resolve_password(
        {"credential_ref": "vault:account-one", "password": "legacy-plaintext"}
    )

    assert resolved == "dpapi-secret"
    assert vault.calls == ["vault:account-one"]
    assert resolver.source({"credential_ref": "vault:account-one", "password": "legacy-plaintext"}) == "dpapi"


def test_broken_reference_fails_closed_without_plaintext_fallback_or_secret_in_error() -> None:
    vault = FakeVault(error=VaultError("backend unavailable"))
    resolver = SmartCredentialResolver(Path("unused.json"), vault=vault)
    account = {"credential_ref": "vault:broken-record", "password": "must-not-fallback"}

    with pytest.raises(CredentialResolutionError) as caught:
        resolver.resolve_password(account)

    message = str(caught.value)
    assert "broken-record" not in message
    assert "must-not-fallback" not in message
    assert resolver.is_available(account) is False


def test_plaintext_is_temporary_fallback_only_without_reference() -> None:
    resolver = SmartCredentialResolver(Path("unused.json"), vault=FakeVault())

    assert resolver.resolve_password({"password": "legacy-only"}) == "legacy-only"
    assert resolver.source({"password": "legacy-only"}) == "legacy_plaintext"


def test_account_vault_reference_routes_to_the_account_vault() -> None:
    legacy_vault = FakeVault(error=AssertionError("legacy vault must not be used"))
    account_vault = FakeVault("account-vault-secret")
    resolver = SmartCredentialResolver(
        Path("legacy.json"),
        vault=legacy_vault,
        account_vault_path=Path("account.json"),
        account_vault=account_vault,
    )

    resolved = resolver.resolve_password(
        {"credential_ref": "account-vault:smart-account-one"}
    )

    assert resolved == "account-vault-secret"
    assert account_vault.calls == ["vault:smart-account-one"]
    assert legacy_vault.calls == []
    assert (
        resolver.source({"credential_ref": "account-vault:smart-account-one"})
        == "dpapi_account_vault"
    )


def test_account_vault_reference_fails_closed_when_backend_is_not_configured() -> None:
    resolver = SmartCredentialResolver(Path("legacy.json"), vault=FakeVault())

    with pytest.raises(CredentialResolutionError):
        resolver.resolve_password(
            {
                "credential_ref": "account-vault:smart-account-one",
                "password": "must-not-fallback",
            }
        )


def test_unsupported_reference_is_not_treated_as_a_password() -> None:
    resolver = SmartCredentialResolver(Path("unused.json"), vault=FakeVault("unused"))

    with pytest.raises(CredentialResolutionError, match="引用类型不受支持"):
        resolver.resolve_password({"credential_ref": "edge-password-manager://record", "password": "fallback"})


def test_corrupt_vault_json_is_a_value_free_resolution_failure(tmp_path: Path) -> None:
    vault_path = tmp_path / "corrupt-vault.json"
    vault_path.write_text("{broken", encoding="utf-8")
    resolver = SmartCredentialResolver(vault_path)

    with pytest.raises(CredentialResolutionError) as caught:
        resolver.resolve_password(
            {"credential_ref": "vault:broken-record", "password": "must-not-fallback"}
        )

    rendered = "".join(traceback.format_exception(caught.value))
    assert "must-not-fallback" not in rendered
    assert "broken-record" not in rendered
    assert "Cannot read JSON" not in rendered


@pytest.mark.parametrize("backend_error", [ValueError, RuntimeError])
def test_backend_secret_is_suppressed_from_full_traceback(backend_error: type[Exception]) -> None:
    resolver = SmartCredentialResolver(
        Path("unused.json"),
        vault=FakeVault(error=backend_error("SENTINEL_BACKEND_SECRET")),
    )

    with pytest.raises(CredentialResolutionError) as caught:
        resolver.resolve_password({"credential_ref": "vault:account-one"})

    rendered = "".join(traceback.format_exception(caught.value))
    assert "SENTINEL_BACKEND_SECRET" not in rendered
