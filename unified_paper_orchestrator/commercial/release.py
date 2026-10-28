"""Signed update-manifest verification without executing remote code."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import ContractError, ReleaseManifest, SignedEnvelope
from .crypto import verify_envelope


@dataclass(frozen=True)
class UpdateDecision:
    available: bool
    mandatory: bool
    reason: str
    manifest: ReleaseManifest | None = None


def _version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[.-]?([A-Za-z]+)(\d*)?)?\s*", value)
    if not match:
        raise ContractError(f"Unsupported release version {value!r}.")
    major, minor, patch, label, label_number = match.groups()
    base = [int(major), int(minor or 0), int(patch or 0)]
    if label:
        # Prereleases sort below the corresponding final release.
        rank = {"a": -30, "alpha": -30, "b": -20, "beta": -20, "rc": -10}.get(label.lower(), -5)
        base.extend([rank, int(label_number or 0)])
    else:
        base.extend([0, 0])
    return tuple(base)


def verify_release_manifest(
    envelope: SignedEnvelope | dict,
    *,
    trusted_keys: Mapping[str, Ed25519PublicKey],
    product_id: str,
) -> ReleaseManifest:
    payload = verify_envelope(
        envelope,
        expected_purpose="release",
        trusted_keys=trusted_keys,
    )
    manifest = ReleaseManifest.from_payload(payload)
    if manifest.product_id != product_id:
        raise ContractError("Release manifest belongs to another product.")
    return manifest


def decide_update(
    manifest: ReleaseManifest,
    *,
    current_version: str,
    allowed_channels: tuple[str, ...],
) -> UpdateDecision:
    if manifest.channel not in allowed_channels:
        return UpdateDecision(False, False, "Release channel is not enabled.")
    available = _version_key(manifest.version) > _version_key(current_version)
    if not available:
        return UpdateDecision(False, False, "Application is already current.", manifest)
    mandatory = bool(
        manifest.mandatory
        or _version_key(current_version) < _version_key(manifest.minimum_supported_version)
    )
    return UpdateDecision(True, mandatory, "A verified update is available.", manifest)


def verify_artifact(path: Path | str, artifact_sha256: str, artifact_size: int) -> None:
    target = Path(path)
    try:
        stat = target.stat()
    except OSError as exc:
        raise ContractError("Downloaded update artifact is unavailable.") from exc
    if stat.st_size != artifact_size:
        raise ContractError("Downloaded update artifact size does not match the signed manifest.")
    digest = hashlib.sha256()
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != artifact_sha256.lower():
        raise ContractError("Downloaded update artifact hash does not match the signed manifest.")
