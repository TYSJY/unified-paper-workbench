"""Signed licence cache with bounded offline operation and clock-rollback guard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..io_utils import read_json, write_json_atomic
from .contracts import ContractError, LicenseClaims, SignedEnvelope, format_utc, parse_utc
from .crypto import SignatureError, verify_envelope
from .paths import license_record_path


LICENSE_CACHE_SCHEMA = "uap.local-license-cache/v1"


class LicenseState(str, Enum):
    MISSING = "missing"
    ACTIVE = "active"
    REFRESH_DUE = "refresh_due"
    OFFLINE_GRACE = "offline_grace"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True)
class LicenseDecision:
    state: LicenseState
    usable: bool
    reason: str
    observed_at: datetime
    claims: LicenseClaims | None = None

    @property
    def refresh_recommended(self) -> bool:
        return self.state in {LicenseState.REFRESH_DUE, LicenseState.OFFLINE_GRACE}

    def allows(self, feature: str) -> bool:
        return bool(self.usable and self.claims and self.claims.entitlements.has(feature))


class LicenseStore:
    """Persist and validate one device-bound signed lease."""

    def __init__(
        self,
        *,
        trusted_keys: Mapping[str, Ed25519PublicKey],
        product_id: str,
        audience: str,
        device_id: str,
        path: Path | str | None = None,
    ) -> None:
        self.trusted_keys = dict(trusted_keys)
        self.product_id = str(product_id or "").strip()
        self.audience = str(audience or "").strip()
        self.device_id = str(device_id or "").strip()
        self.path = Path(path) if path is not None else license_record_path()
        if not self.product_id or not self.audience or not self.device_id:
            raise ContractError("Licence store identity fields must not be empty.")

    def save(self, envelope: SignedEnvelope | dict, *, received_at: datetime | None = None) -> LicenseClaims:
        parsed = envelope if isinstance(envelope, SignedEnvelope) else SignedEnvelope.from_dict(envelope)
        claims = self._verified_claims(parsed)
        now = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        write_json_atomic(
            self.path,
            {
                "schema": LICENSE_CACHE_SCHEMA,
                "envelope": parsed.to_dict(),
                "received_at": format_utc(now),
                "last_seen_at": format_utc(now),
            },
        )
        return claims

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def evaluate(self, *, now: datetime | None = None) -> LicenseDecision:
        supplied_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.path.exists():
            return LicenseDecision(
                state=LicenseState.MISSING,
                usable=False,
                reason="No signed licence is cached for this device.",
                observed_at=supplied_now,
            )
        try:
            record = read_json(self.path)
            if not isinstance(record, dict) or record.get("schema") != LICENSE_CACHE_SCHEMA:
                raise ContractError("Local licence cache schema is invalid.")
            if set(record) != {"schema", "envelope", "received_at", "last_seen_at"}:
                raise ContractError("Local licence cache fields are invalid.")
            last_seen = parse_utc(record["last_seen_at"], "last_seen_at")
            observed = max(supplied_now, last_seen)
            parsed = SignedEnvelope.from_dict(record["envelope"])
            claims = self._verified_claims(parsed)
        except (ContractError, SignatureError, OSError) as exc:
            return LicenseDecision(
                state=LicenseState.INVALID,
                usable=False,
                reason=f"Cached licence could not be trusted: {exc}",
                observed_at=supplied_now,
            )

        if observed > last_seen:
            updated = dict(record)
            updated["last_seen_at"] = format_utc(observed)
            write_json_atomic(self.path, updated)

        usable_until = min(claims.offline_grace_until, claims.subscription_expires_at)
        online_lease_until = min(claims.lease_expires_at, claims.subscription_expires_at)
        if observed > usable_until:
            return LicenseDecision(
                state=LicenseState.EXPIRED,
                usable=False,
                reason="The subscription or bounded offline grace period has expired.",
                observed_at=observed,
                claims=claims,
            )
        if observed > online_lease_until:
            return LicenseDecision(
                state=LicenseState.OFFLINE_GRACE,
                usable=True,
                reason="Operating from a signed offline lease; reconnect before grace expires.",
                observed_at=observed,
                claims=claims,
            )
        if observed >= claims.refresh_after:
            return LicenseDecision(
                state=LicenseState.REFRESH_DUE,
                usable=True,
                reason="Licence is valid and should be refreshed when online.",
                observed_at=observed,
                claims=claims,
            )
        return LicenseDecision(
            state=LicenseState.ACTIVE,
            usable=True,
            reason="Signed licence is active.",
            observed_at=observed,
            claims=claims,
        )

    def _verified_claims(self, envelope: SignedEnvelope) -> LicenseClaims:
        payload = verify_envelope(
            envelope,
            expected_purpose="license",
            trusted_keys=self.trusted_keys,
        )
        claims = LicenseClaims.from_payload(payload)
        if claims.product_id != self.product_id:
            raise ContractError("Licence product does not match this application.")
        if claims.audience != self.audience:
            raise ContractError("Licence audience does not match this application.")
        if claims.device_id != self.device_id:
            raise ContractError("Licence belongs to another device.")
        return claims
