from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from unified_paper_orchestrator.commercial.contracts import (
    ContractError,
    Entitlements,
    LicenseClaims,
    ReleaseArtifact,
    ReleaseManifest,
    SignedEnvelope,
    canonical_json_bytes,
)
from unified_paper_orchestrator.commercial.crypto import (
    SignatureError,
    generate_private_key,
    public_key_id,
    sign_payload,
    verify_envelope,
)
from unified_paper_orchestrator.commercial.diagnostics import build_diagnostic_event
from unified_paper_orchestrator.commercial.release import (
    decide_update,
    verify_artifact,
    verify_release_manifest,
)


def _entitlements() -> Entitlements:
    return Entitlements(
        max_devices=2,
        max_concurrent_papers=8,
        features=("writing", "figures", "journal_author", "smart_submission"),
        update_channels=("stable",),
        diagnostics_upload=True,
    )


def _claims(now: datetime) -> LicenseClaims:
    return LicenseClaims(
        license_id="lic-test",
        product_id="unified-paper-workbench",
        user_id="usr-test",
        device_id="dev-test",
        plan="professional",
        issued_at=now,
        refresh_after=now + timedelta(days=2),
        lease_expires_at=now + timedelta(days=7),
        offline_grace_until=now + timedelta(days=14),
        subscription_expires_at=now + timedelta(days=30),
        entitlements=_entitlements(),
        nonce="one-use-nonce",
        issuer="https://licensing.example.test",
        audience="unified-paper-desktop",
    )


def test_canonical_json_is_stable_and_rejects_nan() -> None:
    assert canonical_json_bytes({"b": 2, "a": "论文"}) == canonical_json_bytes(
        {"a": "论文", "b": 2}
    )
    with pytest.raises(ContractError):
        canonical_json_bytes({"bad": float("nan")})


def test_signed_contract_is_purpose_bound_and_tamper_evident() -> None:
    key = generate_private_key()
    key_id = public_key_id(key.public_key())
    envelope = sign_payload(
        {"schema": "example/v1", "value": 42},
        purpose="license",
        private_key=key,
    )
    trusted = {key_id: key.public_key()}
    assert verify_envelope(
        envelope,
        expected_purpose="license",
        trusted_keys=trusted,
    )["value"] == 42
    with pytest.raises(SignatureError):
        verify_envelope(envelope, expected_purpose="release", trusted_keys=trusted)

    tampered = envelope.to_dict()
    tampered["payload"]["value"] = 43
    with pytest.raises(SignatureError):
        verify_envelope(tampered, expected_purpose="license", trusted_keys=trusted)


def test_license_contract_round_trip_is_strict() -> None:
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    claims = _claims(now)
    assert LicenseClaims.from_payload(claims.to_payload()) == claims
    invalid = claims.to_payload()
    invalid["unexpected"] = True
    with pytest.raises(ContractError):
        LicenseClaims.from_payload(invalid)


def test_signed_release_and_artifact_verification(tmp_path) -> None:
    key = generate_private_key()
    artifact_path = tmp_path / "candidate.exe"
    artifact_path.write_bytes(b"signed candidate")
    artifact = ReleaseArtifact(
        name="UnifiedPaperOrchestrator.exe",
        url="https://downloads.example.test/UnifiedPaperOrchestrator.exe",
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        size=artifact_path.stat().st_size,
    )
    manifest = ReleaseManifest(
        product_id="unified-paper-workbench",
        version="0.12.0b1",
        channel="preview",
        published_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        minimum_supported_version="0.11.6",
        mandatory=False,
        notes_url="https://docs.example.test/releases/0.12.0b1",
        artifact=artifact,
    )
    envelope = sign_payload(
        manifest.to_payload(),
        purpose="release",
        private_key=key,
    )
    trusted = {public_key_id(key.public_key()): key.public_key()}
    verified = verify_release_manifest(
        envelope,
        trusted_keys=trusted,
        product_id="unified-paper-workbench",
    )
    decision = decide_update(
        verified,
        current_version="0.11.6",
        allowed_channels=("stable", "preview"),
    )
    assert decision.available is True
    assert decision.mandatory is False
    verify_artifact(artifact_path, artifact.sha256, artifact.size)
    artifact_path.write_bytes(b"tampered candidate")
    with pytest.raises(ContractError):
        verify_artifact(artifact_path, artifact.sha256, artifact.size)


def test_diagnostics_are_allowlist_only_and_reject_sensitive_text() -> None:
    event = build_diagnostic_event(
        app_version="0.12.0b1",
        event_type="workflow.failure",
        severity="error",
        error_code="SDK_TIMEOUT",
        component="workflow_engine",
        operation="execute_step",
        duration_ms=30000,
        os_family="windows",
        release_channel="preview",
        license_state="active",
        anonymous_installation_id="inst-123",
        metadata={"workflow_stage": "writing", "retry_count": 1},
    )
    assert "paper" not in canonical_json_bytes(event).decode("utf-8").lower()
    with pytest.raises(ContractError):
        build_diagnostic_event(
            app_version="0.12.0b1",
            event_type="workflow.failure",
            severity="error",
            error_code="SDK_TIMEOUT",
            component="workflow_engine",
            operation="execute_step",
            os_family="windows",
            release_channel="preview",
            license_state="active",
            anonymous_installation_id="inst-123",
            metadata={"exception_class": "user@example.test"},
        )
    with pytest.raises(ContractError):
        build_diagnostic_event(
            app_version="0.12.0b1",
            event_type="workflow.failure",
            severity="error",
            error_code="SDK_TIMEOUT",
            component="workflow_engine",
            operation="execute_step",
            os_family="windows",
            release_channel="preview",
            license_state="active",
            anonymous_installation_id="inst-123",
            metadata={"raw_exception": "secret"},
        )


def test_envelope_parser_rejects_unknown_fields() -> None:
    with pytest.raises(ContractError):
        SignedEnvelope.from_dict(
            {
                "schema": "uap.signed-envelope/v1",
                "purpose": "license",
                "key_id": "key",
                "payload": {},
                "signature": "value",
                "extra": True,
            }
        )
