from __future__ import annotations

from datetime import datetime, timedelta

from unified_paper_orchestrator.account_semantics import (
    canonical_account_scope,
    canonical_account_status,
    normalize_account_readiness,
)
from unified_paper_orchestrator.browser_session import normalize_origin
from unified_paper_orchestrator.smart_submission import _configured_accounts
from unified_paper_orchestrator.ui.presentation import status_label


def _signed_shape(
    account_id: str,
    *,
    checked_at: datetime,
    expires_at: datetime,
    origin: str,
) -> dict[str, object]:
    return {
        "schema": "smart-browser-session-evidence/v1",
        "evidence_id": "a" * 32,
        "live": True,
        "account_id": account_id,
        "checked_at": checked_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "origin": normalize_origin(origin),
        "probe_method": "cdp-runtime-value-free/v1",
        "cdp_port": 9224,
        "page_state": {
            "target_type": "page",
            "document_ready": True,
            "exact_origin": True,
            "login_challenge_visible": False,
            "authenticated_ui_visible": True,
        },
        "value_free": True,
        "signature_alg": "hmac-sha256",
        "signature": "0" * 64,
    }


def _fixture_signature_is_valid(_evidence: object) -> bool:
    return True


def test_scope_aliases_share_one_internal_semantics() -> None:
    assert canonical_account_scope("submission_system") == "journal_system"
    assert canonical_account_scope("journal_system") == "journal_system"
    assert canonical_account_scope("email_account") == "email"
    assert canonical_account_scope("email") == "email"


def test_status_normalization_distinguishes_verified_failure_and_imported() -> None:
    assert canonical_account_status("verified_em_login_after_password_reset") == "verified"
    assert canonical_account_status("invalid_password_elsevier_id_one_time_link_requested") == "invalid_password"
    assert canonical_account_status("imported_from_legacy") == "configured_unverified"
    assert status_label("verified_em_login_after_password_reset") == "已验证"
    assert status_label("imported_from_legacy") == "已配置（待登录验证）"


def test_legacy_plaintext_requires_trusted_secret_broker() -> None:
    readiness = normalize_account_readiness(
        {
            "scope": "submission_system",
            "status": "imported_from_legacy",
            "password": "test-only-password",
        },
        resolved_secret_available=True,
    )
    assert readiness.configured is True
    assert readiness.verified is False
    assert readiness.state == "secret_broker_required"
    assert readiness.requires_secret_broker is True


def test_live_session_takes_precedence_over_stored_secret() -> None:
    now = datetime.now().astimezone()
    readiness = normalize_account_readiness(
        {
            "account_id": "test-only",
            "scope": "journal_system",
            "status": "verified",
            "credential_ref": "vault:test-only",
            "browser_session_evidence": _signed_shape(
                "test-only",
                checked_at=now,
                expires_at=now + timedelta(minutes=5),
                origin="https://submit.example.test",
            ),
        },
        resolved_secret_available=True,
        evidence_verifier=_fixture_signature_is_valid,
    )
    assert readiness.state == "live_browser_session"
    assert readiness.requires_secret_broker is False


def test_invalid_password_is_not_configured_even_when_secret_exists() -> None:
    readiness = normalize_account_readiness(
        {
            "scope": "journal_system",
            "status": "invalid_password_reset_requested",
            "password": "test-only-password",
        },
        resolved_secret_available=True,
    )
    assert readiness.configured is False
    assert readiness.state == "unavailable"


def test_profile_or_password_ref_string_is_not_live_session_evidence() -> None:
    for field in ("password_ref", "browser_profile"):
        readiness = normalize_account_readiness(
            {
                "scope": "journal_system",
                "status": "session_ready",
                field: "nonempty-is-not-proof",
            },
            resolved_secret_available=False,
        )
        assert readiness.configured is False


def test_legacy_unsigned_live_session_shape_is_not_authorization() -> None:
    now = datetime.now().astimezone()
    readiness = normalize_account_readiness(
        {
            "account_id": "legacy-shape",
            "scope": "journal_system",
            "status": "session_ready",
            "browser_session_evidence": {
                "live": True,
                "checked_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "origin": "https://submit.example.test",
            },
        },
        resolved_secret_available=False,
        evidence_verifier=_fixture_signature_is_valid,
    )
    assert readiness.state == "unavailable"


def test_signed_typed_live_browser_session_evidence_is_accepted() -> None:
    now = datetime.now().astimezone()
    readiness = normalize_account_readiness(
        {
            "account_id": "test-only",
            "scope": "journal_system",
            "status": "session_ready",
            "browser_session_evidence": _signed_shape(
                "test-only",
                checked_at=now,
                expires_at=now + timedelta(minutes=5),
                origin="https://submit.example.test",
            ),
        },
        resolved_secret_available=False,
        evidence_verifier=_fixture_signature_is_valid,
    )
    assert readiness.configured is True
    assert readiness.state == "live_browser_session"


def test_expired_browser_session_evidence_is_rejected() -> None:
    now = datetime.now().astimezone()
    readiness = normalize_account_readiness(
        {
            "account_id": "test-only",
            "scope": "journal_system",
            "status": "session_ready",
            "browser_session_evidence": _signed_shape(
                "test-only",
                checked_at=now - timedelta(minutes=10),
                expires_at=now - timedelta(minutes=1),
                origin="https://submit.example.test",
            ),
        },
        resolved_secret_available=False,
        evidence_verifier=_fixture_signature_is_valid,
    )
    assert readiness.configured is False


def test_live_browser_session_must_match_target_origin_and_be_fresh() -> None:
    now = datetime.now().astimezone()
    account = {
        "account_id": "test-only",
        "scope": "journal_system",
        "status": "session_ready",
        "browser_session_evidence": _signed_shape(
            "test-only",
            checked_at=now - timedelta(minutes=11),
            expires_at=now - timedelta(minutes=6),
            origin="https://other.example.test",
        ),
    }
    stale = normalize_account_readiness(
        account,
        resolved_secret_available=False,
        expected_origins=("https://submit.example.test/login",),
        evidence_verifier=_fixture_signature_is_valid,
        now=now,
    )
    assert stale.configured is False

    account["browser_session_evidence"] = _signed_shape(
        "test-only",
        checked_at=now,
        expires_at=now + timedelta(minutes=5),
        origin="https://other.example.test",
    )
    wrong_origin = normalize_account_readiness(
        account,
        resolved_secret_available=False,
        expected_origins=("https://submit.example.test/login",),
        evidence_verifier=_fixture_signature_is_valid,
        now=now,
    )
    assert wrong_origin.configured is False

    account["browser_session_evidence"] = _signed_shape(
        "test-only",
        checked_at=now,
        expires_at=now + timedelta(minutes=5),
        origin="https://submit.example.test/another-path",
    )
    matching = normalize_account_readiness(
        account,
        resolved_secret_available=False,
        expected_origins=("https://submit.example.test/login",),
        evidence_verifier=_fixture_signature_is_valid,
        now=now,
    )
    assert matching.state == "live_browser_session"


def test_smart_configured_accounts_uses_scope_and_status_normalizer() -> None:
    credentials = {
        "accounts": [
            {
                "account_id": "alias-ready",
                "scope": "submission_system",
                "username": "ready-user",
                "password": "test-only-password",
                "status": "verified_custom_probe",
            },
            {
                "account_id": "invalid",
                "scope": "journal_system",
                "username": "invalid-user",
                "password": "test-only-password",
                "status": "invalid_password",
            },
        ]
    }

    configured = _configured_accounts(credentials, "journal_system")

    assert [account["account_id"] for account in configured] == ["alias-ready"]
