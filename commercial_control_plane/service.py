"""Deterministic commercial domain service.

This layer contains no HTTP or UI concerns, so the same logic can back the
local mock server now and a production FastAPI/PostgreSQL adapter later.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from unified_paper_orchestrator.commercial.contracts import (
    LicenseClaims,
    ReleaseManifest,
    SignedEnvelope,
    format_utc,
    utc_now,
)
from unified_paper_orchestrator.commercial.crypto import sign_payload
from unified_paper_orchestrator.commercial.diagnostics import validate_diagnostic_event

from .catalog import DESKTOP_AUDIENCE, PLAN_CATALOG, PRODUCT_ID, public_catalog
from .mail_delivery import LoginCodeDeliveryError, LoginCodeSender
from .store import ControlPlaneStore


_EMAIL = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,190}$")
_SAFE_DEVICE_VALUE = re.compile(r"^[A-Za-z0-9_.:/ -]{1,160}$")


class ServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ControlPlaneConfig:
    issuer: str = "http://127.0.0.1:8765"
    audience: str = DESKTOP_AUDIENCE
    product_id: str = PRODUCT_ID
    login_code_minutes: int = 10
    session_days: int = 30
    trial_days: int = 14
    licence_lease_days: int = 7
    licence_refresh_days: int = 3
    offline_grace_days: int = 14
    expose_development_codes: bool = False
    allowed_browser_origins: tuple[str, ...] = ()


class ControlPlaneService:
    def __init__(
        self,
        store: ControlPlaneStore,
        signing_key: Ed25519PrivateKey,
        *,
        config: ControlPlaneConfig | None = None,
        clock: Any = utc_now,
        login_code_sender: LoginCodeSender | None = None,
    ) -> None:
        self.store = store
        self.signing_key = signing_key
        self.config = config or ControlPlaneConfig()
        self.clock = clock
        self.login_code_sender = login_code_sender

    @staticmethod
    def _hash_secret(value: str, salt: str = "") -> str:
        return hashlib.sha256(f"{salt}\x00{value}".encode("utf-8")).hexdigest()

    def request_login_code(self, email: str) -> dict[str, Any]:
        normalized = str(email or "").strip().lower()
        if not _EMAIL.fullmatch(normalized):
            raise ServiceError("A valid e-mail address is required.")
        if (
            not self.config.expose_development_codes
            and self.login_code_sender is None
        ):
            raise ServiceError(
                "Verification e-mail delivery is not configured.",
                status_code=503,
            )
        now = self.clock().astimezone(timezone.utc)
        user = self.store.get_or_create_user(
            user_id=f"usr-{uuid4()}",
            email=normalized,
            now=format_utc(now),
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        expires = now + timedelta(minutes=self.config.login_code_minutes)
        self.store.save_login_code(
            code_id=f"code-{uuid4()}",
            user_id=user["id"],
            salt=salt,
            code_hash=self._hash_secret(code, salt),
            expires_at=format_utc(expires),
            now=format_utc(now),
        )
        if self.login_code_sender is not None:
            try:
                self.login_code_sender.send_login_code(
                    recipient=normalized,
                    code=code,
                    expires_at=expires,
                )
            except LoginCodeDeliveryError as exc:
                raise ServiceError(str(exc), status_code=503) from exc
        result: dict[str, Any] = {
            "accepted": True,
            "expires_at": format_utc(expires),
            "delivery": (
                "email"
                if self.login_code_sender is not None
                else "development-console"
            ),
        }
        if self.config.expose_development_codes:
            result["development_code"] = code
        return result

    def verify_login_code(self, email: str, code: str) -> dict[str, Any]:
        normalized = str(email or "").strip().lower()
        supplied_code = str(code or "").strip()
        now = self.clock().astimezone(timezone.utc)
        with self.store.connection() as db:
            user_row = db.execute("SELECT * FROM users WHERE email = ?", (normalized,)).fetchone()
        if user_row is None:
            raise ServiceError("Login code is invalid or expired.", status_code=401)
        user = dict(user_row)
        consumed = self.store.consume_login_code(
            user_id=user["id"],
            code_hash_resolver=lambda salt: self._hash_secret(supplied_code, salt),
            now=format_utc(now),
            maximum_attempts=6,
        )
        if not consumed:
            raise ServiceError("Login code is invalid or expired.", status_code=401)

        subscription = self.store.latest_subscription(
            user_id=user["id"],
            now=format_utc(now),
        )
        previous_subscription = self.store.latest_subscription_record(
            user_id=user["id"]
        )
        if subscription is None and previous_subscription is None:
            expires = now + timedelta(days=self.config.trial_days)
            self.store.save_subscription(
                subscription_id=f"sub-{uuid4()}",
                user_id=user["id"],
                plan="trial",
                starts_at=format_utc(now),
                expires_at=format_utc(expires),
                now=format_utc(now),
            )
            subscription = self.store.latest_subscription(user_id=user["id"], now=format_utc(now))
        effective_subscription = subscription or previous_subscription
        assert effective_subscription is not None

        token = secrets.token_urlsafe(48)
        session_expires = now + timedelta(days=self.config.session_days)
        self.store.save_session(
            session_id=f"ses-{uuid4()}",
            user_id=user["id"],
            token_hash=self._hash_secret(token),
            expires_at=format_utc(session_expires),
            now=format_utc(now),
        )
        return {
            "user_id": user["id"],
            "bearer_token": token,
            "plan": effective_subscription["plan"],
            "subscription_status": "active" if subscription is not None else "expired",
            "subscription_expires_at": effective_subscription["expires_at"],
            "session_expires_at": format_utc(session_expires),
        }

    def account_summary(self, bearer_token: str) -> dict[str, Any]:
        session = self.authenticate(bearer_token)
        now = self.clock().astimezone(timezone.utc)
        active = self.store.latest_subscription(
            user_id=session["user_id"],
            now=format_utc(now),
        )
        latest = active or self.store.latest_subscription_record(
            user_id=session["user_id"]
        )
        devices = self.store.devices(user_id=session["user_id"])
        active_devices = [item for item in devices if not item.get("revoked_at")]
        plan = str(latest["plan"] if latest else "")
        entitlements = PLAN_CATALOG.get(plan)
        return {
            "schema": "uap.account-summary/v1",
            "user": {
                "id": session["user_id"],
                "email": session["email"],
            },
            "subscription": {
                "plan": plan,
                "status": "active" if active is not None else "expired",
                "starts_at": str(latest["starts_at"] if latest else ""),
                "expires_at": str(latest["expires_at"] if latest else ""),
            },
            "limits": {
                "max_devices": entitlements.max_devices if entitlements else 0,
                "max_concurrent_papers": (
                    entitlements.max_concurrent_papers if entitlements else 0
                ),
            },
            "devices": devices,
            "active_device_count": len(active_devices),
            "server_time": format_utc(now),
        }

    def logout(self, bearer_token: str) -> dict[str, Any]:
        token = str(bearer_token or "")
        if not token:
            raise ServiceError("Authentication is required.", status_code=401)
        revoked = self.store.revoke_session(
            token_hash=self._hash_secret(token),
            now=format_utc(self.clock()),
        )
        return {"revoked": bool(revoked)}

    def grant_subscription(
        self,
        *,
        user_id: str,
        plan: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        """Apply a verified billing/admin decision through one deterministic path."""

        clean_plan = str(plan or "").strip()
        if clean_plan not in PLAN_CATALOG or clean_plan == "trial":
            raise ServiceError("Paid subscription plan is unsupported.")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ServiceError("Subscription expiry must include a timezone.")
        now = self.clock().astimezone(timezone.utc)
        expiry = expires_at.astimezone(timezone.utc)
        if expiry <= now:
            raise ServiceError("Subscription expiry must be in the future.")
        if self.store.get_user(user_id=str(user_id or "").strip()) is None:
            raise ServiceError("Subscription user was not found.", status_code=404)
        subscription_id = f"sub-{uuid4()}"
        self.store.replace_active_subscription(
            subscription_id=subscription_id,
            user_id=str(user_id or "").strip(),
            plan=clean_plan,
            starts_at=format_utc(now),
            expires_at=format_utc(expiry),
            now=format_utc(now),
        )
        return {
            "subscription_id": subscription_id,
            "plan": clean_plan,
            "starts_at": format_utc(now),
            "expires_at": format_utc(expiry),
        }

    def authenticate(self, bearer_token: str) -> dict[str, Any]:
        token = str(bearer_token or "")
        if not token:
            raise ServiceError("Authentication is required.", status_code=401)
        now = self.clock().astimezone(timezone.utc)
        session = self.store.resolve_session(
            token_hash=self._hash_secret(token),
            now=format_utc(now),
        )
        if session is None:
            raise ServiceError("Session is invalid or expired.", status_code=401)
        return session

    def activate_license(self, bearer_token: str, device: Any) -> dict[str, Any]:
        session = self.authenticate(bearer_token)
        record = self._validate_device(device)
        now = self.clock().astimezone(timezone.utc)
        subscription = self._active_subscription(session["user_id"], now)
        entitlements = PLAN_CATALOG[subscription["plan"]]
        active_devices = self.store.active_devices(user_id=session["user_id"])
        existing = next(
            (item for item in active_devices if item["device_id"] == record["device_id"]),
            None,
        )
        if existing is None and len(active_devices) >= entitlements.max_devices:
            raise ServiceError(
                "Device limit reached; revoke another device before activation.",
                status_code=409,
            )
        self.store.upsert_device(
            record_id=str(existing["id"]) if existing else f"dvc-{uuid4()}",
            user_id=session["user_id"],
            now=format_utc(now),
            **record,
        )
        envelope = self._issue_license(
            user_id=session["user_id"],
            device_id=record["device_id"],
            subscription=subscription,
            now=now,
        )
        return {"license": envelope.to_dict()}

    def refresh_license(self, bearer_token: str, device_id: str) -> dict[str, Any]:
        session = self.authenticate(bearer_token)
        now = self.clock().astimezone(timezone.utc)
        device = self.store.get_device(
            user_id=session["user_id"],
            device_id=str(device_id or "").strip(),
        )
        if device is None:
            raise ServiceError("Device is not activated.", status_code=404)
        subscription = self._active_subscription(session["user_id"], now)
        envelope = self._issue_license(
            user_id=session["user_id"],
            device_id=device["device_id"],
            subscription=subscription,
            now=now,
        )
        return {"license": envelope.to_dict()}

    def revoke_device(self, bearer_token: str, device_id: str) -> dict[str, Any]:
        session = self.authenticate(bearer_token)
        revoked = self.store.revoke_device(
            user_id=session["user_id"],
            device_id=str(device_id or "").strip(),
            now=format_utc(self.clock()),
        )
        if not revoked:
            raise ServiceError("Active device was not found.", status_code=404)
        return {"revoked": True}

    def policy_catalog(self) -> dict[str, Any]:
        payload = {
            **public_catalog(),
            "issued_at": format_utc(self.clock()),
            "issuer": self.config.issuer,
        }
        return {"catalog": sign_payload(payload, purpose="policy", private_key=self.signing_key).to_dict()}

    def latest_release(self, channel: str) -> dict[str, Any]:
        clean_channel = str(channel or "stable").strip()
        record = self.store.latest_release(
            product_id=self.config.product_id,
            channel=clean_channel,
        )
        return {"release": record["envelope"] if record else None}

    def publish_release(self, manifest: ReleaseManifest) -> SignedEnvelope:
        if manifest.product_id != self.config.product_id:
            raise ServiceError("Release product does not match this control plane.")
        envelope = sign_payload(
            manifest.to_payload(),
            purpose="release",
            private_key=self.signing_key,
        )
        self.store.save_release(
            release_id=f"rel-{uuid4()}",
            product_id=manifest.product_id,
            version=manifest.version,
            channel=manifest.channel,
            envelope=envelope.to_dict(),
            published_at=format_utc(manifest.published_at),
        )
        return envelope

    def report_diagnostic(self, bearer_token: str, event: Any) -> dict[str, Any]:
        session = self.authenticate(bearer_token)
        validated = validate_diagnostic_event(event)
        subscription = self._active_subscription(
            session["user_id"],
            self.clock().astimezone(timezone.utc),
        )
        entitlements = PLAN_CATALOG[subscription["plan"]]
        if not entitlements.diagnostics_upload:
            raise ServiceError(
                "Diagnostic upload is not enabled for this plan.",
                status_code=403,
            )
        diagnostic_id = f"diag-{uuid4()}"
        self.store.save_diagnostic(
            diagnostic_id=diagnostic_id,
            user_id=session["user_id"],
            event=validated,
            now=format_utc(self.clock()),
        )
        return {"diagnostic_id": diagnostic_id}

    def _active_subscription(self, user_id: str, now: datetime) -> dict[str, Any]:
        subscription = self.store.latest_subscription(user_id=user_id, now=format_utc(now))
        if subscription is None:
            raise ServiceError("No active subscription.", status_code=402)
        if subscription["plan"] not in PLAN_CATALOG:
            raise ServiceError("Subscription plan is unsupported.", status_code=409)
        return subscription

    def _issue_license(
        self,
        *,
        user_id: str,
        device_id: str,
        subscription: dict[str, Any],
        now: datetime,
    ) -> SignedEnvelope:
        plan = subscription["plan"]
        licence_id = f"lic-{uuid4()}"
        subscription_expires = datetime.fromisoformat(
            str(subscription["expires_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        claims = LicenseClaims(
            license_id=licence_id,
            product_id=self.config.product_id,
            user_id=user_id,
            device_id=device_id,
            plan=plan,
            issued_at=now,
            refresh_after=now + timedelta(days=self.config.licence_refresh_days),
            lease_expires_at=now + timedelta(days=self.config.licence_lease_days),
            offline_grace_until=now + timedelta(days=self.config.offline_grace_days),
            subscription_expires_at=subscription_expires,
            entitlements=PLAN_CATALOG[plan],
            nonce=secrets.token_urlsafe(24),
            issuer=self.config.issuer,
            audience=self.config.audience,
        )
        envelope = sign_payload(
            claims.to_payload(),
            purpose="license",
            private_key=self.signing_key,
        )
        self.store.save_licence(
            licence_id=licence_id,
            user_id=user_id,
            device_id=device_id,
            plan=plan,
            envelope=envelope.to_dict(),
            now=format_utc(now),
        )
        return envelope

    @staticmethod
    def _validate_device(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ServiceError("Device payload must be an object.")
        expected = {"device_id", "installation_id", "app_version", "os_family"}
        if set(value) != expected:
            raise ServiceError("Device payload fields are invalid.")
        result: dict[str, str] = {}
        for field in expected:
            clean = str(value.get(field) or "").strip()
            if not _SAFE_DEVICE_VALUE.fullmatch(clean):
                raise ServiceError(f"Device field {field} is invalid.")
            result[field] = clean
        return result
