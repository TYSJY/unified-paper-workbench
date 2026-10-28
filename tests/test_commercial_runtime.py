from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from commercial_control_plane.http_server import ControlPlaneHttpServer
from commercial_control_plane.service import ControlPlaneConfig, ControlPlaneService
from commercial_control_plane.store import ControlPlaneStore
from unified_paper_orchestrator.commercial.contracts import (
    ContractError,
    ReleaseArtifact,
    ReleaseManifest,
)
from unified_paper_orchestrator.commercial.crypto import (
    generate_private_key,
    public_key_to_pem,
)
from unified_paper_orchestrator.commercial.license_store import LicenseState
from unified_paper_orchestrator.commercial.runtime import (
    CommercialAccessError,
    CommercialRuntime,
)


def _fake_protect(value: str) -> str:
    return f"protected:{value[::-1]}"


def _fake_unprotect(value: str) -> str:
    return value.removeprefix("protected:")[::-1]


def _server(tmp_path):
    key = generate_private_key()
    service = ControlPlaneService(
        ControlPlaneStore(tmp_path / "server.sqlite3"),
        key,
        config=ControlPlaneConfig(
            issuer="http://127.0.0.1",
            expose_development_codes=True,
        ),
    )
    server = ControlPlaneHttpServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key_path = tmp_path / "development.public.pem"
    key_path.write_bytes(public_key_to_pem(key.public_key()))
    return server, thread, service, key_path


def test_commercial_runtime_is_disabled_and_network_free_by_default(tmp_path) -> None:
    runtime = CommercialRuntime(
        app_version="0.11.6",
        state_root=tmp_path / "desktop",
        protector=_fake_protect,
        unprotector=_fake_unprotect,
    )
    status = runtime.status()
    assert status.enabled is False
    assert status.mode == "local_compatibility"
    assert status.usable is True
    runtime.require_features(
        ("writing", "figures", "journal_author", "smart_submission")
    )
    runtime.require_capacity(100)
    assert not (tmp_path / "desktop" / "device.json").exists()


def test_production_runtime_cannot_trust_a_user_supplied_signing_key(tmp_path) -> None:
    key = generate_private_key()
    key_path = tmp_path / "untrusted.public.pem"
    key_path.write_bytes(public_key_to_pem(key.public_key()))
    runtime = CommercialRuntime(
        app_version="0.12.0b1",
        state_root=tmp_path / "desktop",
        protector=_fake_protect,
        unprotector=_fake_unprotect,
        allow_development_keys=False,
    )
    with pytest.raises(ContractError):
        runtime.configure(
            enabled=True,
            control_plane_url="http://127.0.0.1:8765",
            release_channel="preview",
            diagnostics_opt_in=False,
            development_mode=True,
            development_public_key_path=str(key_path),
        )


def test_runtime_login_activation_refresh_update_and_redacted_spool(tmp_path) -> None:
    server, thread, service, key_path = _server(tmp_path)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        runtime = CommercialRuntime(
            app_version="0.11.6",
            state_root=tmp_path / "desktop",
            protector=_fake_protect,
            unprotector=_fake_unprotect,
            allow_development_keys=True,
        )
        configured = runtime.configure(
            enabled=True,
            control_plane_url=url,
            release_channel="preview",
            diagnostics_opt_in=True,
            development_mode=True,
            development_public_key_path=str(key_path),
        )
        assert configured.license_state == LicenseState.MISSING.value
        requested = runtime.request_login_code("researcher@example.test")
        active = runtime.complete_login(
            "researcher@example.test",
            requested["development_code"],
        )
        assert active.usable is True
        assert active.plan == "trial"
        assert active.max_devices == 1
        assert active.max_concurrent_papers == 2
        assert "writing" in active.features
        runtime.require_features(
            ("writing", "figures", "journal_author", "smart_submission")
        )
        runtime.require_capacity(2)
        with pytest.raises(CommercialAccessError):
            runtime.require_capacity(3)
        assert runtime.refresh_license().usable is True

        manifest = ReleaseManifest(
            product_id="unified-paper-workbench",
            version="0.12.0b1",
            channel="preview",
            published_at=datetime.now(timezone.utc),
            minimum_supported_version="0.11.6",
            mandatory=False,
            notes_url="https://docs.example.test/releases/0.12.0b1",
            artifact=ReleaseArtifact(
                name="UnifiedPaperOrchestrator.exe",
                url="https://downloads.example.test/UnifiedPaperOrchestrator.exe",
                sha256="a" * 64,
                size=123,
            ),
        )
        service.publish_release(manifest)
        update = runtime.check_for_update()
        assert update.available is True
        assert update.manifest is not None
        assert update.manifest.version == "0.12.0b1"

        event_id = runtime.queue_failure_diagnostic(
            component="ui_controller",
            operation="start_stage",
            exception_class="TimeoutError",
            duration_ms=5000,
        )
        assert event_id.startswith("diag-")
        event_path = next((tmp_path / "desktop" / "diagnostics").glob("*.json"))
        raw = event_path.read_text(encoding="utf-8")
        assert "researcher@example.test" not in raw
        assert str(tmp_path) not in raw
        with pytest.raises(CommercialAccessError):
            runtime.flush_diagnostics()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_runtime_rejects_tampered_cached_license(tmp_path) -> None:
    server, thread, _service, key_path = _server(tmp_path)
    try:
        runtime = CommercialRuntime(
            app_version="0.11.6",
            state_root=tmp_path / "desktop",
            protector=_fake_protect,
            unprotector=_fake_unprotect,
            allow_development_keys=True,
        )
        runtime.configure(
            enabled=True,
            control_plane_url=f"http://127.0.0.1:{server.server_address[1]}",
            release_channel="stable",
            diagnostics_opt_in=False,
            development_mode=True,
            development_public_key_path=str(key_path),
        )
        requested = runtime.request_login_code("researcher@example.test")
        runtime.complete_login(
            "researcher@example.test",
            requested["development_code"],
        )
        path = tmp_path / "desktop" / "license.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["envelope"]["payload"]["plan"] = "lab"
        path.write_text(json.dumps(data), encoding="utf-8")
        status = runtime.status()
        assert status.usable is False
        assert status.license_state == LicenseState.INVALID.value
        with pytest.raises(CommercialAccessError):
            runtime.require_feature("writing")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
