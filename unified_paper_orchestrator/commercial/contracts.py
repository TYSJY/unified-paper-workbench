"""Strict, serialisable contracts shared by the desktop and control plane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


ENVELOPE_SCHEMA = "uap.signed-envelope/v1"
LICENSE_SCHEMA = "uap.license/v1"
RELEASE_SCHEMA = "uap.release/v1"
ENTITLEMENTS_SCHEMA = "uap.entitlements/v1"


class ContractError(ValueError):
    """A signed or remotely supplied contract is malformed."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used for signatures."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Contract is not canonical-JSON serialisable: {exc}") from exc
    return text.encode("utf-8")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractError("Contract timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty UTC timestamp.")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    keys = set(value)
    missing = sorted(expected - keys)
    unknown = sorted(keys - expected)
    if missing or unknown:
        detail: list[str] = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if unknown:
            detail.append(f"unknown={','.join(unknown)}")
        raise ContractError(f"{field} fields are invalid ({'; '.join(detail)}).")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be non-empty text.")
    return value.strip()


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field} must be an integer.")
    lower = 0 if allow_zero else 1
    if value < lower:
        raise ContractError(f"{field} must be >= {lower}.")
    return value


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be a list.")
    result: list[str] = []
    for index, item in enumerate(value):
        clean = _text(item, f"{field}[{index}]")
        if clean not in result:
            result.append(clean)
    return tuple(result)


@dataclass(frozen=True)
class Entitlements:
    """Feature limits frozen into one signed licence lease."""

    max_devices: int
    max_concurrent_papers: int
    features: tuple[str, ...]
    update_channels: tuple[str, ...] = ("stable",)
    diagnostics_upload: bool = False

    def __post_init__(self) -> None:
        _positive_int(self.max_devices, "entitlements.max_devices")
        _positive_int(self.max_concurrent_papers, "entitlements.max_concurrent_papers")
        if not self.features:
            raise ContractError("entitlements.features must not be empty.")
        for index, feature in enumerate(self.features):
            _text(feature, f"entitlements.features[{index}]")
        if not self.update_channels:
            raise ContractError("entitlements.update_channels must not be empty.")
        for index, channel in enumerate(self.update_channels):
            _text(channel, f"entitlements.update_channels[{index}]")
        if not isinstance(self.diagnostics_upload, bool):
            raise ContractError("entitlements.diagnostics_upload must be boolean.")

    def has(self, feature: str) -> bool:
        return str(feature or "").strip() in self.features

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ENTITLEMENTS_SCHEMA,
            "max_devices": self.max_devices,
            "max_concurrent_papers": self.max_concurrent_papers,
            "features": list(self.features),
            "update_channels": list(self.update_channels),
            "diagnostics_upload": self.diagnostics_upload,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "Entitlements":
        data = _mapping(value, "entitlements")
        _exact_keys(
            data,
            {
                "schema",
                "max_devices",
                "max_concurrent_papers",
                "features",
                "update_channels",
                "diagnostics_upload",
            },
            "entitlements",
        )
        if data["schema"] != ENTITLEMENTS_SCHEMA:
            raise ContractError("Unsupported entitlements schema.")
        if not isinstance(data["diagnostics_upload"], bool):
            raise ContractError("entitlements.diagnostics_upload must be boolean.")
        return cls(
            max_devices=_positive_int(data["max_devices"], "entitlements.max_devices"),
            max_concurrent_papers=_positive_int(
                data["max_concurrent_papers"],
                "entitlements.max_concurrent_papers",
            ),
            features=_string_tuple(data["features"], "entitlements.features"),
            update_channels=_string_tuple(
                data["update_channels"],
                "entitlements.update_channels",
            ),
            diagnostics_upload=data["diagnostics_upload"],
        )


@dataclass(frozen=True)
class LicenseClaims:
    """Server-issued lease used for online and bounded offline operation."""

    license_id: str
    product_id: str
    user_id: str
    device_id: str
    plan: str
    issued_at: datetime
    refresh_after: datetime
    lease_expires_at: datetime
    offline_grace_until: datetime
    subscription_expires_at: datetime
    entitlements: Entitlements
    nonce: str
    issuer: str
    audience: str

    def __post_init__(self) -> None:
        for field in (
            "license_id",
            "product_id",
            "user_id",
            "device_id",
            "plan",
            "nonce",
            "issuer",
            "audience",
        ):
            _text(getattr(self, field), field)
        ordered = (
            self.issued_at,
            self.refresh_after,
            self.lease_expires_at,
            self.offline_grace_until,
            self.subscription_expires_at,
        )
        if any(item.tzinfo is None for item in ordered):
            raise ContractError("All licence timestamps must be timezone-aware.")
        if self.refresh_after < self.issued_at:
            raise ContractError("refresh_after cannot precede issued_at.")
        if self.lease_expires_at < self.refresh_after:
            raise ContractError("lease_expires_at cannot precede refresh_after.")
        if self.offline_grace_until < self.lease_expires_at:
            raise ContractError("offline_grace_until cannot precede lease_expires_at.")
        if self.subscription_expires_at < self.issued_at:
            raise ContractError("subscription_expires_at cannot precede issued_at.")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": LICENSE_SCHEMA,
            "license_id": self.license_id,
            "product_id": self.product_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "plan": self.plan,
            "issued_at": format_utc(self.issued_at),
            "refresh_after": format_utc(self.refresh_after),
            "lease_expires_at": format_utc(self.lease_expires_at),
            "offline_grace_until": format_utc(self.offline_grace_until),
            "subscription_expires_at": format_utc(self.subscription_expires_at),
            "entitlements": self.entitlements.to_dict(),
            "nonce": self.nonce,
            "issuer": self.issuer,
            "audience": self.audience,
        }

    @classmethod
    def from_payload(cls, value: Any) -> "LicenseClaims":
        data = _mapping(value, "license")
        expected = {
            "schema",
            "license_id",
            "product_id",
            "user_id",
            "device_id",
            "plan",
            "issued_at",
            "refresh_after",
            "lease_expires_at",
            "offline_grace_until",
            "subscription_expires_at",
            "entitlements",
            "nonce",
            "issuer",
            "audience",
        }
        _exact_keys(data, expected, "license")
        if data["schema"] != LICENSE_SCHEMA:
            raise ContractError("Unsupported licence schema.")
        return cls(
            license_id=_text(data["license_id"], "license_id"),
            product_id=_text(data["product_id"], "product_id"),
            user_id=_text(data["user_id"], "user_id"),
            device_id=_text(data["device_id"], "device_id"),
            plan=_text(data["plan"], "plan"),
            issued_at=parse_utc(data["issued_at"], "issued_at"),
            refresh_after=parse_utc(data["refresh_after"], "refresh_after"),
            lease_expires_at=parse_utc(data["lease_expires_at"], "lease_expires_at"),
            offline_grace_until=parse_utc(
                data["offline_grace_until"],
                "offline_grace_until",
            ),
            subscription_expires_at=parse_utc(
                data["subscription_expires_at"],
                "subscription_expires_at",
            ),
            entitlements=Entitlements.from_dict(data["entitlements"]),
            nonce=_text(data["nonce"], "nonce"),
            issuer=_text(data["issuer"], "issuer"),
            audience=_text(data["audience"], "audience"),
        )


@dataclass(frozen=True)
class SignedEnvelope:
    """A purpose-bound payload signed by one identified Ed25519 key."""

    purpose: str
    key_id: str
    payload: dict[str, Any]
    signature: str
    schema: str = ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ENVELOPE_SCHEMA:
            raise ContractError("Unsupported signed-envelope schema.")
        _text(self.purpose, "envelope.purpose")
        _text(self.key_id, "envelope.key_id")
        _mapping(self.payload, "envelope.payload")
        _text(self.signature, "envelope.signature")
        canonical_json_bytes(self.payload)

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "purpose": self.purpose,
            "key_id": self.key_id,
            "payload": self.payload,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: Any) -> "SignedEnvelope":
        data = _mapping(value, "signed_envelope")
        _exact_keys(
            data,
            {"schema", "purpose", "key_id", "payload", "signature"},
            "signed_envelope",
        )
        payload = _mapping(data["payload"], "signed_envelope.payload")
        return cls(
            schema=_text(data["schema"], "signed_envelope.schema"),
            purpose=_text(data["purpose"], "signed_envelope.purpose"),
            key_id=_text(data["key_id"], "signed_envelope.key_id"),
            payload=dict(payload),
            signature=_text(data["signature"], "signed_envelope.signature"),
        )


@dataclass(frozen=True)
class ReleaseArtifact:
    name: str
    url: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _text(self.name, "artifact.name")
        _text(self.url, "artifact.url")
        digest = _text(self.sha256, "artifact.sha256").lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ContractError("artifact.sha256 must be a 64-character hex digest.")
        _positive_int(self.size, "artifact.size", allow_zero=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "sha256": self.sha256.lower(),
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ReleaseArtifact":
        data = _mapping(value, "artifact")
        _exact_keys(data, {"name", "url", "sha256", "size"}, "artifact")
        return cls(
            name=_text(data["name"], "artifact.name"),
            url=_text(data["url"], "artifact.url"),
            sha256=_text(data["sha256"], "artifact.sha256"),
            size=_positive_int(data["size"], "artifact.size", allow_zero=True),
        )


@dataclass(frozen=True)
class ReleaseManifest:
    product_id: str
    version: str
    channel: str
    published_at: datetime
    minimum_supported_version: str
    mandatory: bool
    notes_url: str
    artifact: ReleaseArtifact

    def __post_init__(self) -> None:
        _text(self.product_id, "release.product_id")
        _text(self.version, "release.version")
        _text(self.channel, "release.channel")
        if self.published_at.tzinfo is None:
            raise ContractError("release.published_at must be timezone-aware.")
        _text(self.minimum_supported_version, "release.minimum_supported_version")
        if not isinstance(self.mandatory, bool):
            raise ContractError("release.mandatory must be boolean.")
        _text(self.notes_url, "release.notes_url")
        if not isinstance(self.artifact, ReleaseArtifact):
            raise ContractError("release.artifact must be a ReleaseArtifact.")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": RELEASE_SCHEMA,
            "product_id": self.product_id,
            "version": self.version,
            "channel": self.channel,
            "published_at": format_utc(self.published_at),
            "minimum_supported_version": self.minimum_supported_version,
            "mandatory": self.mandatory,
            "notes_url": self.notes_url,
            "artifact": self.artifact.to_dict(),
        }

    @classmethod
    def from_payload(cls, value: Any) -> "ReleaseManifest":
        data = _mapping(value, "release")
        _exact_keys(
            data,
            {
                "schema",
                "product_id",
                "version",
                "channel",
                "published_at",
                "minimum_supported_version",
                "mandatory",
                "notes_url",
                "artifact",
            },
            "release",
        )
        if data["schema"] != RELEASE_SCHEMA:
            raise ContractError("Unsupported release schema.")
        if not isinstance(data["mandatory"], bool):
            raise ContractError("release.mandatory must be boolean.")
        return cls(
            product_id=_text(data["product_id"], "release.product_id"),
            version=_text(data["version"], "release.version"),
            channel=_text(data["channel"], "release.channel"),
            published_at=parse_utc(data["published_at"], "release.published_at"),
            minimum_supported_version=_text(
                data["minimum_supported_version"],
                "release.minimum_supported_version",
            ),
            mandatory=data["mandatory"],
            notes_url=_text(data["notes_url"], "release.notes_url"),
            artifact=ReleaseArtifact.from_dict(data["artifact"]),
        )
