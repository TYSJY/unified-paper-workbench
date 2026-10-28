from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from commercial_control_plane.http_server import ControlPlaneHttpServer
from commercial_control_plane.service import (
    ControlPlaneConfig,
    ControlPlaneService,
    ServiceError,
)
from commercial_control_plane.store import ControlPlaneStore
from unified_paper_orchestrator.commercial.client import ControlPlaneClient
from unified_paper_orchestrator.commercial.contracts import LicenseClaims
from unified_paper_orchestrator.commercial.crypto import (
    generate_private_key,
    public_key_id,
    verify_envelope,
)
from unified_paper_orchestrator.commercial.license_store import LicenseState, LicenseStore


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _service(tmp_path, *, expose_codes: bool = True):
    key = generate_private_key()
    service = ControlPlaneService(
        ControlPlaneStore(tmp_path / "control-plane.sqlite3"),
        key,
        config=ControlPlaneConfig(
            issuer="http://127.0.0.1:8765",
            expose_development_codes=expose_codes,
        ),
        clock=lambda: NOW,
    )
    return service, key


def _login(service: ControlPlaneService) -> dict:
    requested = service.request_login_code("researcher@example.test")
    assert requested["accepted"] is True
    return service.verify_login_code(
        "researcher@example.test",
        requested["development_code"],
    )


def _device(number: int = 1) -> dict[str, str]:
    return {
        "device_id": f"dev-{number}",
        "installation_id": f"inst-{number}",
        "app_version": "0.12.0b1",
        "os_family": "windows",
    }


class _RecordingLoginCodeSender:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    def send_login_code(
        self,
        *,
        recipient: str,
        code: str,
        expires_at: datetime,
    ) -> None:
        self.messages.append(
            {
                "recipient": recipient,
                "code": code,
                "expires_at": expires_at,
            }
        )


def test_direct_service_login_activation_refresh_and_device_limit(tmp_path) -> None:
    service, key = _service(tmp_path)
    session = _login(service)
    activated = service.activate_license(session["bearer_token"], _device())
    trusted = {public_key_id(key.public_key()): key.public_key()}
    payload = verify_envelope(
        activated["license"],
        expected_purpose="license",
        trusted_keys=trusted,
    )
    claims = LicenseClaims.from_payload(payload)
    assert claims.plan == "trial"
    assert claims.device_id == "dev-1"
    refreshed = service.refresh_license(session["bearer_token"], "dev-1")
    assert refreshed["license"]["purpose"] == "license"

    with pytest.raises(ServiceError) as error:
        service.activate_license(session["bearer_token"], _device(2))
    assert error.value.status_code == 409
    assert service.revoke_device(session["bearer_token"], "dev-1")["revoked"] is True
    assert service.activate_license(session["bearer_token"], _device(2))["license"]


def test_login_code_is_one_time_and_bad_codes_are_bounded(tmp_path) -> None:
    service, _key = _service(tmp_path)
    request = service.request_login_code("researcher@example.test")
    code = request["development_code"]
    service.verify_login_code("researcher@example.test", code)
    with pytest.raises(ServiceError):
        service.verify_login_code("researcher@example.test", code)


def test_production_login_requires_delivery_and_sender_receives_code(tmp_path) -> None:
    key = generate_private_key()
    store = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    without_delivery = ControlPlaneService(
        store,
        key,
        config=ControlPlaneConfig(expose_development_codes=False),
        clock=lambda: NOW,
    )
    with pytest.raises(ServiceError) as error:
        without_delivery.request_login_code("researcher@example.test")
    assert error.value.status_code == 503

    sender = _RecordingLoginCodeSender()
    service = ControlPlaneService(
        store,
        key,
        config=ControlPlaneConfig(expose_development_codes=False),
        clock=lambda: NOW,
        login_code_sender=sender,
    )
    requested = service.request_login_code("researcher@example.test")
    assert requested["delivery"] == "email"
    assert "development_code" not in requested
    assert sender.messages[0]["recipient"] == "researcher@example.test"
    session = service.verify_login_code(
        "researcher@example.test",
        str(sender.messages[0]["code"]),
    )
    assert session["subscription_status"] == "active"


def test_trial_is_created_once_then_paid_plan_restores_access(tmp_path) -> None:
    current = [NOW]
    key = generate_private_key()
    store = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    service = ControlPlaneService(
        store,
        key,
        config=ControlPlaneConfig(expose_development_codes=True, trial_days=14),
        clock=lambda: current[0],
    )
    first = _login(service)
    assert first["plan"] == "trial"
    assert first["subscription_status"] == "active"

    current[0] = NOW + timedelta(days=15)
    second_request = service.request_login_code("researcher@example.test")
    second = service.verify_login_code(
        "researcher@example.test",
        second_request["development_code"],
    )
    assert second["plan"] == "trial"
    assert second["subscription_status"] == "expired"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1

    with pytest.raises(ServiceError):
        service.grant_subscription(
            user_id=second["user_id"],
            plan="professional",
            expires_at=datetime(2027, 1, 1),
        )
    granted = service.grant_subscription(
        user_id=second["user_id"],
        plan="professional",
        expires_at=current[0] + timedelta(days=365),
    )
    assert granted["plan"] == "professional"
    summary = service.account_summary(second["bearer_token"])
    assert summary["subscription"]["status"] == "active"
    assert summary["subscription"]["plan"] == "professional"
    assert summary["limits"]["max_concurrent_papers"] == 8

    service.activate_license(second["bearer_token"], _device())
    summary = service.account_summary(second["bearer_token"])
    assert summary["active_device_count"] == 1
    assert summary["devices"][0]["device_id"] == "dev-1"
    assert service.logout(second["bearer_token"])["revoked"] is True
    with pytest.raises(ServiceError) as auth_error:
        service.account_summary(second["bearer_token"])
    assert auth_error.value.status_code == 401


def test_policy_catalog_is_signed_for_separate_purpose(tmp_path) -> None:
    service, key = _service(tmp_path)
    catalog = service.policy_catalog()["catalog"]
    trusted = {public_key_id(key.public_key()): key.public_key()}
    verified = verify_envelope(
        catalog,
        expected_purpose="policy",
        trusted_keys=trusted,
    )
    assert verified["product_id"] == "unified-paper-workbench"
    with pytest.raises(Exception):
        verify_envelope(catalog, expected_purpose="license", trusted_keys=trusted)


def test_http_client_end_to_end_and_local_license_cache(tmp_path) -> None:
    service, key = _service(tmp_path)
    server = ControlPlaneHttpServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ControlPlaneClient(f"http://127.0.0.1:{server.server_address[1]}")
        requested = client.request_login_code("researcher@example.test")
        session = client.verify_login_code(
            "researcher@example.test",
            requested["development_code"],
        )
        envelope = client.activate(
            bearer_token=session.bearer_token,
            device=_device(),
        )
        licence_store = LicenseStore(
            trusted_keys={public_key_id(key.public_key()): key.public_key()},
            product_id="unified-paper-workbench",
            audience="unified-paper-desktop",
            device_id="dev-1",
            path=tmp_path / "desktop-license.json",
        )
        licence_store.save(envelope, received_at=NOW)
        decision = licence_store.evaluate(now=NOW)
        assert decision.state is LicenseState.ACTIVE
        assert decision.allows("writing")
        assert decision.allows("smart_submission")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
