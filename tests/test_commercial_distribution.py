from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from commercial_control_plane.http_server import ControlPlaneHttpServer
from commercial_control_plane.service import ControlPlaneConfig, ControlPlaneService
from commercial_control_plane.store import ControlPlaneStore
from unified_paper_orchestrator.commercial.contracts import ContractError
from unified_paper_orchestrator.commercial.crypto import (
    generate_private_key,
    public_key_id,
    public_key_to_pem,
)
from unified_paper_orchestrator.commercial.distribution import (
    DISTRIBUTION_POLICY_SCHEMA,
    DistributionPolicy,
)
from unified_paper_orchestrator.commercial.runtime import CommercialRuntime
from tools.prepare_release_profile import prepare_release_profile


def _customer_policy(control_plane_url: str = "http://127.0.0.1:8765") -> DistributionPolicy:
    return DistributionPolicy(
        edition="customer",
        commercial_enforcement=True,
        allow_runtime_configuration=False,
        allow_development_keys=False,
        control_plane_url=control_plane_url,
        account_portal_url="https://product.example.test/account",
        pricing_url="https://product.example.test/pricing",
        support_url="https://product.example.test/support",
        privacy_url="https://product.example.test/privacy",
        terms_url="https://product.example.test/terms",
        release_channel="preview",
        trial_days=14,
        product_name="Unified Paper Workbench",
    )


def _fake_protect(value: str) -> str:
    return f"protected:{value[::-1]}"


def _fake_unprotect(value: str) -> str:
    return value.removeprefix("protected:")[::-1]


def test_customer_distribution_cannot_disable_or_reconfigure_commercial_access(
    tmp_path,
) -> None:
    runtime = CommercialRuntime(
        app_version="0.12.0b3",
        state_root=tmp_path / "desktop",
        distribution_policy=_customer_policy(),
        protector=_fake_protect,
        unprotector=_fake_unprotect,
    )
    status = runtime.status()
    assert status.edition == "customer"
    assert status.enabled is True
    assert status.configuration_locked is True
    assert status.onboarding_required is True
    assert status.can_start is False
    with pytest.raises(ContractError):
        runtime.configure(
            enabled=False,
            control_plane_url="http://127.0.0.1:9999",
            release_channel="stable",
            diagnostics_opt_in=False,
        )

    updated = runtime.update_preferences(diagnostics_opt_in=True)
    assert updated.enabled is True
    assert updated.control_plane_url == "http://127.0.0.1:8765"
    assert updated.release_channel == "preview"
    assert updated.diagnostics_opt_in is True


def test_customer_distribution_rejects_customer_bypass_flags() -> None:
    base = _customer_policy().to_dict()
    assert base["schema"] == DISTRIBUTION_POLICY_SCHEMA
    for field, value in (
        ("commercial_enforcement", False),
        ("allow_runtime_configuration", True),
        ("allow_development_keys", True),
    ):
        invalid = dict(base)
        invalid[field] = value
        with pytest.raises(ContractError):
            DistributionPolicy.from_dict(invalid)


def test_release_profile_stages_customer_policy_and_public_key_only(
    tmp_path,
) -> None:
    profile_path = tmp_path / "customer.json"
    profile_path.write_text(
        json.dumps(_customer_policy().to_dict()),
        encoding="utf-8",
    )
    private_key = generate_private_key()
    public_key_path = tmp_path / "candidate.public.pem"
    public_key_path.write_bytes(public_key_to_pem(private_key.public_key()))
    result = prepare_release_profile(
        profile_path,
        tmp_path / "staged",
        public_key_path=public_key_path,
    )
    assert result["edition"] == "customer"
    assert result["commercial_enforcement"] is True
    assert result["trusted_public_key_count"] == 1
    assert "PRIVATE KEY" not in (
        tmp_path / "staged" / "trusted_keys.json"
    ).read_text(encoding="utf-8")


def test_release_profile_requires_customer_key_and_rejects_internal_key(
    tmp_path,
) -> None:
    customer_path = tmp_path / "customer.json"
    customer_path.write_text(
        json.dumps(_customer_policy().to_dict()),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="require"):
        prepare_release_profile(customer_path, tmp_path / "customer-output")

    internal = _customer_policy().to_dict()
    internal.update(
        {
            "edition": "internal",
            "commercial_enforcement": False,
            "allow_runtime_configuration": True,
            "allow_development_keys": True,
        }
    )
    internal_path = tmp_path / "internal.json"
    internal_path.write_text(json.dumps(internal), encoding="utf-8")
    private_key = generate_private_key()
    public_key_path = tmp_path / "internal.public.pem"
    public_key_path.write_bytes(public_key_to_pem(private_key.public_key()))
    with pytest.raises(ValueError, match="only accepted"):
        prepare_release_profile(
            internal_path,
            tmp_path / "internal-output",
            public_key_path=public_key_path,
        )


def test_expired_customer_lease_enters_read_only_without_deleting_session(
    tmp_path,
) -> None:
    past = datetime(2025, 1, 1, tzinfo=timezone.utc)
    key = generate_private_key()
    service = ControlPlaneService(
        ControlPlaneStore(tmp_path / "server.sqlite3"),
        key,
        config=ControlPlaneConfig(
            issuer="http://127.0.0.1",
            expose_development_codes=True,
        ),
        clock=lambda: past,
    )
    server = ControlPlaneHttpServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        keyring = {
            "schema": "uap.trusted-keys/v1",
            "keys": [
                {
                    "key_id": public_key_id(key.public_key()),
                    "public_key_pem": public_key_to_pem(key.public_key()).decode("ascii"),
                }
            ],
        }
        keyring_path = tmp_path / "trusted-keys.json"
        keyring_path.write_text(json.dumps(keyring), encoding="utf-8")
        runtime = CommercialRuntime(
            app_version="0.12.0b3",
            state_root=tmp_path / "desktop",
            embedded_keys_path=keyring_path,
            distribution_policy=_customer_policy(
                f"http://127.0.0.1:{server.server_address[1]}"
            ),
            protector=_fake_protect,
            unprotector=_fake_unprotect,
        )
        requested = runtime.request_login_code("customer@example.test")
        status = runtime.complete_login(
            "customer@example.test",
            requested["development_code"],
        )
        assert status.signed_in is True
        assert status.usable is False
        assert status.onboarding_required is False
        assert status.read_only is True
        assert status.can_start is False
        assert (tmp_path / "desktop" / "session.json").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
