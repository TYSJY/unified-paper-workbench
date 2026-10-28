from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from unified_paper_orchestrator.account_semantics import normalize_account_readiness
from unified_paper_orchestrator.account_site_store import AccountSiteStore, RegistryValidationError
from unified_paper_orchestrator.browser_session import (
    BrowserSessionEvidenceKeyStore,
    BrowserSessionEvidenceSigner,
    BrowserSessionProbe,
    BrowserSessionProbeError,
    LocalCdpProbeTransport,
    PROBE_CDP_UNAVAILABLE,
    PROBE_INCONCLUSIVE,
    PROBE_LOGIN_VISIBLE,
    PROBE_ORIGIN_UNVERIFIED,
    PROBE_REDIRECT_MISMATCH,
)
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic
from unified_paper_orchestrator import trusted_login_probe as trusted_probe_module
from unified_paper_orchestrator.trusted_login_probe import probe_and_record_browser_session


ORIGIN = "https://submit.example.test:443"
LOGIN_URL = "https://submit.example.test/login"


class FakeTransport:
    def __init__(
        self,
        *,
        state: Mapping[str, Any] | None = None,
        list_error: Exception | None = None,
        evaluate_error: Exception | None = None,
        target_url: str = LOGIN_URL,
    ) -> None:
        self.state = state or {
            "origin": "https://submit.example.test",
            "target_type": "page",
            "document_ready": True,
            "login_challenge_visible": False,
            "authenticated_ui_visible": True,
        }
        self.list_error = list_error
        self.evaluate_error = evaluate_error
        self.target_url = target_url
        self.list_calls: list[int] = []
        self.evaluate_calls: list[tuple[int, str]] = []

    def list_targets(self, port: int) -> list[Mapping[str, Any]]:
        self.list_calls.append(port)
        if self.list_error:
            raise self.list_error
        return [
            {
                "type": "page",
                "url": self.target_url,
                "title": "SECRET PAGE TITLE MUST NEVER ESCAPE",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/test-only",
            }
        ]

    def evaluate_value_free_state(self, port: int, websocket_url: str) -> Mapping[str, Any]:
        self.evaluate_calls.append((port, websocket_url))
        if self.evaluate_error:
            raise self.evaluate_error
        return self.state


def _test_protector(value: str) -> str:
    return "test-dpapi:" + value[::-1]


def _test_unprotector(value: str) -> str:
    assert value.startswith("test-dpapi:")
    return value.removeprefix("test-dpapi:")[::-1]


def make_signer(tmp_path: Path) -> BrowserSessionEvidenceSigner:
    return BrowserSessionEvidenceSigner(
        BrowserSessionEvidenceKeyStore(
            tmp_path / "evidence-key.json",
            protector=_test_protector,
            unprotector=_test_unprotector,
            acl_hardener=lambda _path: None,
        )
    )


def make_store(tmp_path: Path) -> AccountSiteStore:
    credential_path = tmp_path / "credentials.json"
    site_path = tmp_path / "sites.json"
    write_json_atomic(
        credential_path,
        {
            "version": 1,
            "accounts": [
                {
                    "account_id": "journal-account",
                    "scope": "journal_system",
                    "system_id": "example_system",
                    "provider": "Example Submit",
                    "username": "local-user",
                    "login_url": LOGIN_URL,
                    "status": "verified",
                    "password": "LOCAL TEST SECRET",
                }
            ],
        },
    )
    write_json_atomic(
        site_path,
        {
            "version": 1,
            "systems": [
                {
                    "system_id": "example_system",
                    "status": "verified",
                    "login_url": LOGIN_URL,
                    "submission_url": "https://submit.example.test/start",
                }
            ],
            "journals": [
                {
                    "journal_key": "example_journal",
                    "journal_name": "Example Journal",
                    "system_id": "example_system",
                    "status": "verified",
                    "submission_url": "https://submit.example.test/journal",
                }
            ],
        },
    )
    return AccountSiteStore(
        credential_path,
        site_path,
        vault_path=tmp_path / "vault.json",
        legacy_vault_path=tmp_path / "legacy-vault.json",
        runtime_vault_path=tmp_path / "runtime-vault.json",
        smart_state_path=tmp_path / "smart-state.json",
        unified_state_path=tmp_path / "unified-state.json",
        backup_dir=tmp_path / "backups",
        audit_path=tmp_path / "audit.json",
        acl_hardener=lambda _path: None,
    )


def test_probe_records_signed_value_free_exact_origin_evidence_with_audit(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    signer = make_signer(tmp_path)
    result = probe_and_record_browser_session(
        account_id="journal-account",
        system_id="example_system",
        origin=LOGIN_URL,
        ports=(9224,),
        store=store,
        transport=FakeTransport(),
        signer=signer,
    )

    assert result["ok"] is True, result
    assert result["origin"] == ORIGIN
    assert result["value_free"] is True
    persisted = read_json(store.credential_path, {})["accounts"][0]
    evidence = persisted["browser_session_evidence"]
    assert signer.verify(evidence) is True
    assert evidence["account_id"] == "journal-account"
    assert evidence["origin"] == ORIGIN
    assert evidence["cdp_port"] == 9224
    assert evidence["page_state"] == {
        "target_type": "page",
        "document_ready": True,
        "exact_origin": True,
        "login_challenge_visible": False,
        "authenticated_ui_visible": True,
    }
    serialized = json.dumps(evidence, ensure_ascii=False).casefold()
    for forbidden in ("cookie", "token", "password", "dom", "secret page title", "local test secret"):
        assert forbidden not in serialized
    audit = json.dumps(read_json(store.audit_path, {}), ensure_ascii=False).casefold()
    assert "record-browser-session-evidence" in audit
    assert "local test secret" not in audit
    assert list((tmp_path / "backups").rglob("*credentials.json"))

    readiness = normalize_account_readiness(
        persisted,
        resolved_secret_available=False,
        expected_origins=(LOGIN_URL,),
        evidence_verifier=signer.verify,
    )
    assert readiness.state == "live_browser_session"


def test_default_probe_store_uses_distinct_current_vault_and_value_free_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    smart_dir = state_dir / "smart_submission"
    credential_path = smart_dir / "credential_store.local.json"
    site_path = smart_dir / "site_registry.json"
    write_json_atomic(
        credential_path,
        {
            "version": 1,
            "accounts": [
                {
                    "account_id": "journal-account",
                    "scope": "journal_system",
                    "system_id": "example_system",
                    "provider": "Example Submit",
                    "username": "local-user",
                    "login_url": LOGIN_URL,
                    "status": "verified",
                    "password": "LOCAL TEST SECRET",
                }
            ],
        },
    )
    write_json_atomic(
        site_path,
        {
            "version": 1,
            "systems": [
                {
                    "system_id": "example_system",
                    "status": "verified",
                    "login_url": LOGIN_URL,
                }
            ],
            "journals": [],
        },
    )
    current_vault = smart_dir / "account_vault.local.json"
    legacy_runtime_vault = tmp_path / "legacy" / "credential_store.json"
    monkeypatch.setattr(trusted_probe_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(trusted_probe_module, "ACCOUNT_VAULT_PATH", current_vault)
    monkeypatch.setattr(
        trusted_probe_module,
        "LEGACY_CREDENTIAL_VAULT_PATH",
        legacy_runtime_vault,
    )
    monkeypatch.setattr(trusted_probe_module, "CREDENTIAL_VAULT_PATH", legacy_runtime_vault)

    result = probe_and_record_browser_session(
        account_id="journal-account",
        system_id="example_system",
        origin=LOGIN_URL,
        ports=(9224,),
        transport=FakeTransport(),
        signer=make_signer(tmp_path),
    )
    assert result["ok"] is True, result
    assert current_vault.exists() is False
    assert legacy_runtime_vault.exists() is False

    probe_sentinel = "PROBE-BACKEND-SENTINEL"
    probe_failed = probe_and_record_browser_session(
        account_id="journal-account",
        system_id="example_system",
        origin=LOGIN_URL,
        ports=(9224,),
        transport=FakeTransport(list_error=RuntimeError(probe_sentinel)),
        signer=make_signer(tmp_path),
    )
    probe_failed_text = json.dumps(probe_failed, ensure_ascii=False)
    assert probe_failed["ok"] is False
    assert probe_failed["code"] == PROBE_CDP_UNAVAILABLE
    assert probe_failed["value_free"] is True
    assert probe_sentinel not in probe_failed_text
    assert "journal-account" not in probe_failed_text
    assert "submit.example.test" not in probe_failed_text

    monkeypatch.setattr(trusted_probe_module, "ACCOUNT_VAULT_PATH", legacy_runtime_vault)
    account_sentinel = "ACCOUNT-CONSTRUCTION-SENTINEL"
    origin_sentinel = "origin-construction-sentinel.example"
    failed = probe_and_record_browser_session(
        account_id=account_sentinel,
        system_id="example_system",
        origin=f"https://{origin_sentinel}/login",
        transport=FakeTransport(),
        signer=make_signer(tmp_path),
    )
    serialized = json.dumps(failed, ensure_ascii=False)
    assert failed == {
        "ok": False,
        "code": "SMART_SESSION_PROBE_BINDING_INVALID",
        "message": "the account, verified system, and target origin binding is invalid",
        "value_free": True,
    }
    assert account_sentinel not in serialized
    assert origin_sentinel not in serialized


def test_unverified_origin_fails_before_any_cdp_or_write(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    signer = make_signer(tmp_path)
    transport = FakeTransport()
    before = store.credential_path.read_bytes()

    result = probe_and_record_browser_session(
        account_id="journal-account",
        system_id="example_system",
        origin="https://wrong.example.test/",
        store=store,
        transport=transport,
        signer=signer,
    )

    assert result["ok"] is False
    assert result["code"] == PROBE_ORIGIN_UNVERIFIED
    assert transport.list_calls == []
    assert store.credential_path.read_bytes() == before
    assert not store.audit_path.exists()
    assert not (tmp_path / "evidence-key.json").exists()


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (
            {
                "origin": "https://redirected.example.test",
                "document_ready": True,
                "login_challenge_visible": False,
                "authenticated_ui_visible": True,
            },
            PROBE_REDIRECT_MISMATCH,
        ),
        (
            {
                "origin": "https://submit.example.test",
                "document_ready": True,
                "login_challenge_visible": True,
                "authenticated_ui_visible": False,
            },
            PROBE_LOGIN_VISIBLE,
        ),
        (
            {
                "origin": "https://submit.example.test",
                "document_ready": True,
                "login_challenge_visible": False,
                "authenticated_ui_visible": False,
            },
            PROBE_INCONCLUSIVE,
        ),
    ],
)
def test_redirect_login_and_no_positive_auth_marker_fail_closed(
    tmp_path: Path,
    state: Mapping[str, Any],
    expected_code: str,
) -> None:
    signer = make_signer(tmp_path)
    probe = BrowserSessionProbe(FakeTransport(state=state), signer, ports=(9224,))
    with pytest.raises(BrowserSessionProbeError) as caught:
        probe.probe(
            account_id="journal-account",
            target_origin=LOGIN_URL,
            verified_origins=(LOGIN_URL,),
        )
    assert caught.value.code == expected_code
    assert "redirected.example.test" not in str(caught.value)
    assert not (tmp_path / "evidence-key.json").exists()


def test_cdp_backend_secret_diagnostics_never_escape(tmp_path: Path) -> None:
    sentinel = "cookie=TOP-SECRET; password=SHOULD-NOT-LEAK"
    signer = make_signer(tmp_path)
    probe = BrowserSessionProbe(
        FakeTransport(list_error=RuntimeError(sentinel)),
        signer,
        ports=(9224, 9222),
    )
    with pytest.raises(BrowserSessionProbeError) as caught:
        probe.probe(
            account_id="journal-account",
            target_origin=LOGIN_URL,
            verified_origins=(LOGIN_URL,),
        )
    payload = json.dumps(caught.value.as_dict(account_id="journal-account", origin=LOGIN_URL))
    assert caught.value.code == PROBE_CDP_UNAVAILABLE
    assert sentinel not in payload
    assert "TOP-SECRET" not in payload

    evaluate_probe = BrowserSessionProbe(
        FakeTransport(evaluate_error=RuntimeError(sentinel)),
        signer,
        ports=(9224,),
    )
    with pytest.raises(BrowserSessionProbeError) as evaluate_caught:
        evaluate_probe.probe(
            account_id="journal-account",
            target_origin=LOGIN_URL,
            verified_origins=(LOGIN_URL,),
        )
    evaluate_payload = json.dumps(evaluate_caught.value.as_dict())
    assert evaluate_caught.value.code == PROBE_INCONCLUSIVE
    assert sentinel not in evaluate_payload


def test_signed_evidence_tamper_expiry_and_extra_sensitive_field_are_rejected(tmp_path: Path) -> None:
    signer = make_signer(tmp_path)
    probe = BrowserSessionProbe(FakeTransport(), signer, ports=(9224,))
    now = datetime.now(timezone.utc)
    evidence = probe.probe(
        account_id="journal-account",
        target_origin=LOGIN_URL,
        verified_origins=(LOGIN_URL,),
        now=now,
    )
    tampered = dict(evidence)
    tampered["origin"] = "https://other.example.test:443"
    assert signer.verify(tampered) is False

    with_extra = dict(evidence)
    with_extra["dom_text"] = "sensitive body"
    assert signer.verify(with_extra) is False

    account = {
        "account_id": "journal-account",
        "scope": "journal_system",
        "status": "verified",
        "browser_session_evidence": evidence,
    }
    expired = normalize_account_readiness(
        account,
        resolved_secret_available=False,
        expected_origins=(LOGIN_URL,),
        evidence_verifier=signer.verify,
        now=now + timedelta(minutes=6),
    )
    assert expired.state == "unavailable"


def test_store_rejects_cross_account_replay_and_invalid_signature(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    signer = make_signer(tmp_path)
    evidence = BrowserSessionProbe(FakeTransport(), signer, ports=(9224,)).probe(
        account_id="different-account",
        target_origin=LOGIN_URL,
        verified_origins=(LOGIN_URL,),
    )
    before = store.credential_path.read_bytes()
    with pytest.raises(RegistryValidationError):
        store.record_browser_session_evidence(
            "journal-account",
            "example_system",
            LOGIN_URL,
            evidence,
            signer=signer,
        )
    assert store.credential_path.read_bytes() == before


def test_local_transport_rejects_non_loopback_websocket_without_connecting() -> None:
    transport = LocalCdpProbeTransport()
    with pytest.raises(Exception):
        transport.evaluate_value_free_state(
            9224,
            "ws://attacker.example.test:9224/devtools/page/secret",
        )
