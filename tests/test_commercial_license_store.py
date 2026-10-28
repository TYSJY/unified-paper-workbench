from __future__ import annotations

from datetime import datetime, timedelta, timezone

from unified_paper_orchestrator.commercial.contracts import Entitlements, LicenseClaims
from unified_paper_orchestrator.commercial.crypto import (
    generate_private_key,
    public_key_id,
    sign_payload,
)
from unified_paper_orchestrator.commercial.device import DeviceIdentityStore
from unified_paper_orchestrator.commercial.license_store import LicenseState, LicenseStore
from unified_paper_orchestrator.commercial.session_store import CommercialSessionStore


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _signed_license(key, *, device_id: str = "dev-test"):
    claims = LicenseClaims(
        license_id="lic-test",
        product_id="unified-paper-workbench",
        user_id="usr-test",
        device_id=device_id,
        plan="professional",
        issued_at=NOW,
        refresh_after=NOW + timedelta(days=2),
        lease_expires_at=NOW + timedelta(days=7),
        offline_grace_until=NOW + timedelta(days=14),
        subscription_expires_at=NOW + timedelta(days=30),
        entitlements=Entitlements(
            max_devices=2,
            max_concurrent_papers=8,
            features=("writing", "figures"),
        ),
        nonce="nonce",
        issuer="https://licensing.example.test",
        audience="unified-paper-desktop",
    )
    return sign_payload(claims.to_payload(), purpose="license", private_key=key)


def test_device_identity_is_random_stable_and_contains_no_hardware_fingerprint(tmp_path) -> None:
    path = tmp_path / "device.json"
    store = DeviceIdentityStore(path)
    first = store.get_or_create()
    second = store.get_or_create()
    assert first == second
    raw = path.read_text(encoding="utf-8")
    assert "mac" not in raw.lower()
    assert "serial" not in raw.lower()
    assert "username" not in raw.lower()


def test_license_transitions_and_clock_rollback_guard(tmp_path) -> None:
    key = generate_private_key()
    trusted = {public_key_id(key.public_key()): key.public_key()}
    store = LicenseStore(
        trusted_keys=trusted,
        product_id="unified-paper-workbench",
        audience="unified-paper-desktop",
        device_id="dev-test",
        path=tmp_path / "license.json",
    )
    store.save(_signed_license(key), received_at=NOW)
    assert store.evaluate(now=NOW + timedelta(days=1)).state is LicenseState.ACTIVE
    assert store.evaluate(now=NOW + timedelta(days=3)).state is LicenseState.REFRESH_DUE
    assert store.evaluate(now=NOW + timedelta(days=8)).state is LicenseState.OFFLINE_GRACE
    expired = store.evaluate(now=NOW + timedelta(days=15))
    assert expired.state is LicenseState.EXPIRED
    assert expired.usable is False
    # Moving the system clock back cannot resurrect an expired cached lease.
    assert store.evaluate(now=NOW + timedelta(days=1)).state is LicenseState.EXPIRED


def test_wrong_device_and_tampered_license_fail_closed(tmp_path) -> None:
    key = generate_private_key()
    trusted = {public_key_id(key.public_key()): key.public_key()}
    store = LicenseStore(
        trusted_keys=trusted,
        product_id="unified-paper-workbench",
        audience="unified-paper-desktop",
        device_id="dev-test",
        path=tmp_path / "license.json",
    )
    envelope = _signed_license(key, device_id="another-device")
    try:
        store.save(envelope, received_at=NOW)
    except Exception:
        pass
    assert store.evaluate(now=NOW).state is LicenseState.MISSING

    good = _signed_license(key).to_dict()
    good["payload"]["plan"] = "lab"
    (tmp_path / "license.json").write_text(
        __import__("json").dumps(
            {
                "schema": "uap.local-license-cache/v1",
                "envelope": good,
                "received_at": "2026-07-24T12:00:00Z",
                "last_seen_at": "2026-07-24T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert store.evaluate(now=NOW).state is LicenseState.INVALID


def test_session_store_never_writes_plaintext_token_or_full_email(tmp_path) -> None:
    def fake_protect(value: str) -> str:
        return f"cipher:{value[::-1]}"

    def fake_unprotect(value: str) -> str:
        return value.removeprefix("cipher:")[::-1]

    path = tmp_path / "session.json"
    store = CommercialSessionStore(
        path,
        protector=fake_protect,
        unprotector=fake_unprotect,
    )
    store.save(
        user_id="usr-test",
        email_hint="researcher@example.test",
        bearer_token="super-secret-token",
    )
    raw = path.read_text(encoding="utf-8")
    assert "super-secret-token" not in raw
    assert "researcher@example.test" not in raw
    assert store.load() == ("usr-test", "res***", "super-secret-token")
