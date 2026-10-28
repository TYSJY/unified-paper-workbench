"""Desktop-side commercial runtime.

Commercial enforcement is disabled by default.  In that compatibility mode all
existing 0.11.6 workflow starts remain permitted and no network request occurs.
When explicitly enabled, new starts require a valid signed entitlement; tasks
already running are never force-terminated by this adapter.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..io_utils import read_json, write_json_atomic
from .client import AuthSession, ControlPlaneClient, ControlPlaneError
from .contracts import ContractError
from .crypto import SignatureError, load_public_key, public_key_id
from .device import DeviceIdentity, DeviceIdentityStore
from .diagnostics import build_diagnostic_event, validate_diagnostic_event
from .distribution import DistributionPolicy, load_distribution_policy
from .license_store import LicenseDecision, LicenseState, LicenseStore
from .paths import commercial_state_root
from .release import UpdateDecision, decide_update, verify_release_manifest
from .session_store import CommercialSessionStore
from .settings import CommercialSettings, CommercialSettingsStore


PRODUCT_ID = "unified-paper-workbench"
AUDIENCE = "unified-paper-desktop"
TRUSTED_KEYS_SCHEMA = "uap.trusted-keys/v1"


class CommercialAccessError(RuntimeError):
    """A new commercial action is not currently entitled."""


@dataclass(frozen=True)
class CommercialStatus:
    enabled: bool
    mode: str
    license_state: str
    usable: bool
    plan: str
    user_id: str
    device_id: str
    reason: str
    refresh_recommended: bool
    diagnostics_opt_in: bool
    release_channel: str
    control_plane_url: str
    development_allowed: bool
    max_devices: int
    max_concurrent_papers: int
    features: tuple[str, ...]
    edition: str = "internal"
    configuration_locked: bool = False
    signed_in: bool = False
    email_hint: str = ""
    onboarding_required: bool = False
    read_only: bool = False
    can_start: bool = True
    account_portal_url: str = ""
    pricing_url: str = ""
    support_url: str = ""
    privacy_url: str = ""
    terms_url: str = ""
    trial_days: int = 14

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "licenseState": self.license_state,
            "usable": self.usable,
            "plan": self.plan,
            "userId": self.user_id,
            "deviceId": self.device_id,
            "reason": self.reason,
            "refreshRecommended": self.refresh_recommended,
            "diagnosticsOptIn": self.diagnostics_opt_in,
            "releaseChannel": self.release_channel,
            "controlPlaneUrl": self.control_plane_url,
            "developmentAllowed": self.development_allowed,
            "maxDevices": self.max_devices,
            "maxConcurrentPapers": self.max_concurrent_papers,
            "features": list(self.features),
            "edition": self.edition,
            "customerEdition": self.edition == "customer",
            "configurationLocked": self.configuration_locked,
            "signedIn": self.signed_in,
            "emailHint": self.email_hint,
            "onboardingRequired": self.onboarding_required,
            "readOnly": self.read_only,
            "canStart": self.can_start,
            "accountPortalUrl": self.account_portal_url,
            "pricingUrl": self.pricing_url,
            "supportUrl": self.support_url,
            "privacyUrl": self.privacy_url,
            "termsUrl": self.terms_url,
            "trialDays": self.trial_days,
        }


class CommercialRuntime:
    def __init__(
        self,
        *,
        app_version: str,
        state_root: Path | str | None = None,
        embedded_keys_path: Path | str | None = None,
        distribution_policy: DistributionPolicy | None = None,
        distribution_policy_path: Path | str | None = None,
        protector: Callable[[str], str] | None = None,
        unprotector: Callable[[str], str] | None = None,
        allow_development_keys: bool | None = None,
    ) -> None:
        self.app_version = str(app_version or "").strip()
        if distribution_policy is not None and distribution_policy_path is not None:
            raise ContractError(
                "Pass either distribution_policy or distribution_policy_path, not both."
            )
        self.distribution_policy = distribution_policy or load_distribution_policy(
            distribution_policy_path
        )
        self.state_root = (
            Path(state_root) if state_root is not None else commercial_state_root()
        )
        self.settings_store = CommercialSettingsStore(self.state_root / "settings.json")
        self.device_store = DeviceIdentityStore(self.state_root / "device.json")
        session_kwargs: dict[str, Any] = {}
        if protector is not None:
            session_kwargs["protector"] = protector
        if unprotector is not None:
            session_kwargs["unprotector"] = unprotector
        self.session_store = CommercialSessionStore(
            self.state_root / "session.json",
            **session_kwargs,
        )
        self.license_path = self.state_root / "license.json"
        self.spool_dir = self.state_root / "diagnostics"
        self.embedded_keys_path = (
            Path(embedded_keys_path)
            if embedded_keys_path is not None
            else Path(__file__).with_name("trusted_keys.json")
        )
        requested_development_keys = (
            bool(allow_development_keys)
            if allow_development_keys is not None
            else bool(
                not getattr(sys, "frozen", False)
                and os.environ.get("UNIFIED_PAPER_COMMERCIAL_DEV_MODE") == "1"
            )
        )
        self.allow_development_keys = bool(
            self.distribution_policy.allow_development_keys
            and requested_development_keys
        )

    def settings(self) -> CommercialSettings:
        stored = self.settings_store.load()
        policy = self.distribution_policy
        if not policy.commercial_enforcement:
            return stored
        return CommercialSettings(
            enabled=True,
            control_plane_url=policy.control_plane_url,
            release_channel=policy.release_channel,
            diagnostics_opt_in=stored.diagnostics_opt_in,
            development_mode=False,
            development_public_key_path="",
        )

    def configure(
        self,
        *,
        enabled: bool,
        control_plane_url: str,
        release_channel: str,
        diagnostics_opt_in: bool,
        development_mode: bool = False,
        development_public_key_path: str = "",
    ) -> CommercialStatus:
        if not self.distribution_policy.allow_runtime_configuration:
            raise ContractError(
                "Commercial service configuration is fixed by this customer build."
            )
        settings = CommercialSettings(
            enabled=bool(enabled),
            control_plane_url=str(control_plane_url or "").strip(),
            release_channel=str(release_channel or "stable").strip().casefold(),
            diagnostics_opt_in=bool(diagnostics_opt_in),
            development_mode=bool(development_mode),
            development_public_key_path=str(development_public_key_path or "").strip(),
        )
        if settings.development_mode:
            if not self.allow_development_keys:
                raise ContractError(
                    "Development signing keys are disabled in this application build."
                )
            path = Path(settings.development_public_key_path)
            if not path.is_file():
                raise ContractError("Development public signing key does not exist.")
            load_public_key(path)
        self.settings_store.save(settings)
        return self.status()

    def update_preferences(
        self,
        *,
        diagnostics_opt_in: bool,
        release_channel: str | None = None,
    ) -> CommercialStatus:
        """Update customer-safe preferences without changing enforcement or origin."""

        current = self.settings_store.load()
        channel = (
            self.distribution_policy.release_channel
            if self.distribution_policy.commercial_enforcement
            else str(release_channel or current.release_channel).strip().casefold()
        )
        self.settings_store.save(
            CommercialSettings(
                enabled=(
                    True
                    if self.distribution_policy.commercial_enforcement
                    else current.enabled
                ),
                control_plane_url=(
                    self.distribution_policy.control_plane_url
                    if self.distribution_policy.commercial_enforcement
                    else current.control_plane_url
                ),
                release_channel=channel,
                diagnostics_opt_in=bool(diagnostics_opt_in),
                development_mode=(
                    False
                    if self.distribution_policy.commercial_enforcement
                    else current.development_mode
                ),
                development_public_key_path=(
                    ""
                    if self.distribution_policy.commercial_enforcement
                    else current.development_public_key_path
                ),
            )
        )
        return self.status()

    def status(self) -> CommercialStatus:
        try:
            settings = self.settings()
        except ContractError as exc:
            return self._decorate_status(CommercialStatus(
                enabled=True,
                mode="configuration_error",
                license_state=LicenseState.INVALID.value,
                usable=False,
                plan="",
                user_id="",
                device_id="",
                reason=str(exc),
                refresh_recommended=False,
                diagnostics_opt_in=False,
                release_channel="",
                control_plane_url="",
                development_allowed=self.allow_development_keys,
                max_devices=0,
                max_concurrent_papers=0,
                features=(),
            ))
        if not settings.enabled:
            return self._decorate_status(CommercialStatus(
                enabled=False,
                mode="local_compatibility",
                license_state="not_required",
                usable=True,
                plan="local",
                user_id="",
                device_id="",
                reason="Commercial enforcement is disabled; local 0.11.6 behaviour is preserved.",
                refresh_recommended=False,
                diagnostics_opt_in=settings.diagnostics_opt_in,
                release_channel=settings.release_channel,
                control_plane_url=settings.control_plane_url,
                development_allowed=self.allow_development_keys,
                max_devices=0,
                max_concurrent_papers=0,
                features=(),
            ))
        try:
            device = self.device_store.get_or_create()
            decision = self._license_store(settings, device).evaluate()
        except (ContractError, SignatureError, OSError) as exc:
            return self._decorate_status(CommercialStatus(
                enabled=True,
                mode="commercial",
                license_state=LicenseState.INVALID.value,
                usable=False,
                plan="",
                user_id="",
                device_id="",
                reason=str(exc),
                refresh_recommended=False,
                diagnostics_opt_in=settings.diagnostics_opt_in,
                release_channel=settings.release_channel,
                control_plane_url=settings.control_plane_url,
                development_allowed=self.allow_development_keys,
                max_devices=0,
                max_concurrent_papers=0,
                features=(),
            ))
        claims = decision.claims
        return self._decorate_status(CommercialStatus(
            enabled=True,
            mode="commercial",
            license_state=decision.state.value,
            usable=decision.usable,
            plan=claims.plan if claims else "",
            user_id=claims.user_id if claims else "",
            device_id=device.device_id,
            reason=decision.reason,
            refresh_recommended=decision.refresh_recommended,
            diagnostics_opt_in=settings.diagnostics_opt_in,
            release_channel=settings.release_channel,
            control_plane_url=settings.control_plane_url,
            development_allowed=self.allow_development_keys,
            max_devices=claims.entitlements.max_devices if claims else 0,
            max_concurrent_papers=(
                claims.entitlements.max_concurrent_papers if claims else 0
            ),
            features=claims.entitlements.features if claims else (),
        ))

    def _decorate_status(self, status: CommercialStatus) -> CommercialStatus:
        policy = self.distribution_policy
        signed_in = False
        user_id = status.user_id
        email_hint = ""
        try:
            session = self.session_store.load()
        except (ContractError, OSError):
            session = None
        if session is not None:
            session_user_id, email_hint, _token = session
            signed_in = True
            user_id = user_id or session_user_id
        onboarding_required = bool(
            policy.customer_edition
            and not status.usable
            and status.license_state
            in {
                LicenseState.MISSING.value,
                LicenseState.INVALID.value,
            }
        )
        read_only = bool(
            policy.customer_edition
            and not status.usable
            and not onboarding_required
        )
        return replace(
            status,
            user_id=user_id,
            edition=policy.edition,
            configuration_locked=not policy.allow_runtime_configuration,
            signed_in=signed_in,
            email_hint=email_hint,
            onboarding_required=onboarding_required,
            read_only=read_only,
            can_start=bool(status.usable),
            account_portal_url=policy.account_portal_url,
            pricing_url=policy.pricing_url,
            support_url=policy.support_url,
            privacy_url=policy.privacy_url,
            terms_url=policy.terms_url,
            trial_days=policy.trial_days,
        )

    def require_feature(self, feature: str) -> None:
        settings = self.settings()
        if not settings.enabled:
            return
        device = self.device_store.get_or_create()
        decision = self._license_store(settings, device).evaluate()
        if not decision.usable:
            raise CommercialAccessError(decision.reason)
        if not decision.allows(feature):
            raise CommercialAccessError(
                f"The active {decision.claims.plan if decision.claims else ''} plan "
                f"does not include {feature}."
            )

    def require_features(self, features: tuple[str, ...]) -> None:
        for feature in features:
            self.require_feature(feature)

    def concurrent_paper_limit(self) -> int | None:
        settings = self.settings()
        if not settings.enabled:
            return None
        decision = self._license_decision(settings)
        if not decision.usable or decision.claims is None:
            raise CommercialAccessError(decision.reason)
        return decision.claims.entitlements.max_concurrent_papers

    def require_capacity(self, requested_papers: int) -> None:
        try:
            requested = int(requested_papers)
        except (TypeError, ValueError) as exc:
            raise CommercialAccessError(
                "Requested paper capacity must be positive."
            ) from exc
        if isinstance(requested_papers, bool) or requested < 1:
            raise CommercialAccessError("Requested paper capacity must be positive.")
        limit = self.concurrent_paper_limit()
        if limit is not None and requested > limit:
            raise CommercialAccessError(
                f"The active plan permits at most {limit} concurrent papers."
            )

    def request_login_code(self, email: str) -> dict[str, Any]:
        settings = self._enabled_settings()
        return self._client(settings).request_login_code(email)

    def complete_login(self, email: str, code: str) -> CommercialStatus:
        settings = self._enabled_settings()
        client = self._client(settings)
        session = client.verify_login_code(email, code)
        self.session_store.save(
            user_id=session.user_id,
            email_hint=email,
            bearer_token=session.bearer_token,
        )
        if session.subscription_status == "active":
            self._activate_for_session(settings, client, session)
        return self.status()

    def refresh_license(self) -> CommercialStatus:
        settings = self._enabled_settings()
        user_id, _hint, token = self._required_session()
        device = self.device_store.get_or_create()
        envelope = self._client(settings).refresh(
            bearer_token=token,
            device_id=device.device_id,
        )
        claims = self._license_store(settings, device).save(envelope)
        if claims.user_id != user_id:
            self.session_store.clear()
            self.license_path.unlink(missing_ok=True)
            raise CommercialAccessError("Refreshed licence user does not match the local session.")
        return self.status()

    def sign_out(self, *, notify_server: bool = True) -> CommercialStatus:
        """Remove only local product credentials and the cached signed lease."""

        if notify_server:
            try:
                settings = self._enabled_settings()
                _user_id, _hint, token = self._required_session()
                self._client(settings).logout(bearer_token=token)
            except (CommercialAccessError, ControlPlaneError, ContractError, OSError):
                pass
        self.session_store.clear()
        self.license_path.unlink(missing_ok=True)
        return self.status()

    def account_summary(self) -> dict[str, Any]:
        settings = self._enabled_settings()
        _user_id, _hint, token = self._required_session()
        return self._client(settings).account_summary(bearer_token=token)

    def revoke_device(self, device_id: str) -> CommercialStatus:
        settings = self._enabled_settings()
        _user_id, _hint, token = self._required_session()
        self._client(settings).revoke_device(
            bearer_token=token,
            device_id=str(device_id or "").strip(),
        )
        local_device = self.device_store.get_or_create()
        if local_device.device_id == str(device_id or "").strip():
            self.session_store.clear()
            self.license_path.unlink(missing_ok=True)
        return self.status()

    def check_for_update(self) -> UpdateDecision:
        settings = self._enabled_settings()
        trusted = self._trusted_keys(settings)
        envelope = self._client(settings).latest_release(
            channel=settings.release_channel,
            current_version=self.app_version,
        )
        if envelope is None:
            return UpdateDecision(False, False, "No release is published on this channel.")
        manifest = verify_release_manifest(
            envelope,
            trusted_keys=trusted,
            product_id=PRODUCT_ID,
        )
        decision = self._license_decision(settings)
        channels = (
            decision.claims.entitlements.update_channels
            if decision.claims and decision.usable
            else ("stable",)
        )
        return decide_update(
            manifest,
            current_version=self.app_version,
            allowed_channels=channels,
        )

    def queue_failure_diagnostic(
        self,
        *,
        component: str,
        operation: str,
        exception_class: str,
        duration_ms: int,
    ) -> str:
        try:
            settings = self.settings()
        except ContractError:
            return ""
        if not settings.diagnostics_opt_in:
            return ""
        device = self.device_store.get_or_create()
        status = self.status()
        event = build_diagnostic_event(
            app_version=self.app_version,
            event_type="operation.failure",
            severity="error",
            error_code="OPERATION_FAILED",
            component=component,
            operation=operation,
            duration_ms=max(0, int(duration_ms)),
            os_family=platform.system().casefold() or "unknown",
            release_channel=settings.release_channel,
            license_state=status.license_state,
            anonymous_installation_id=device.installation_id,
            metadata={"exception_class": exception_class},
        )
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        event_id = str(event["event_id"])
        write_json_atomic(self.spool_dir / f"{event_id}.json", event)
        return event_id

    def flush_diagnostics(self) -> dict[str, int]:
        settings = self._enabled_settings()
        if not settings.diagnostics_opt_in:
            return {"sent": 0, "remaining": self._spool_count()}
        decision = self._license_decision(settings)
        if not (
            decision.usable
            and decision.claims
            and decision.claims.entitlements.diagnostics_upload
        ):
            raise CommercialAccessError("Diagnostic upload is not entitled for this plan.")
        _user_id, _hint, token = self._required_session()
        client = self._client(settings)
        sent = 0
        for path in sorted(self.spool_dir.glob("diag-*.json")):
            try:
                event = validate_diagnostic_event(read_json(path))
                diagnostic_id = client.submit_diagnostic(
                    bearer_token=token,
                    event=event,
                )
                if not diagnostic_id:
                    raise ControlPlaneError("Diagnostic receipt was empty.")
                path.unlink()
                sent += 1
            except (ContractError, ControlPlaneError, OSError):
                break
        return {"sent": sent, "remaining": self._spool_count()}

    def _activate_for_session(
        self,
        settings: CommercialSettings,
        client: ControlPlaneClient,
        session: AuthSession,
    ) -> None:
        device = self.device_store.get_or_create()
        envelope = client.activate(
            bearer_token=session.bearer_token,
            device={
                "device_id": device.device_id,
                "installation_id": device.installation_id,
                "app_version": self.app_version,
                "os_family": platform.system().casefold() or "unknown",
            },
        )
        claims = self._license_store(settings, device).save(envelope)
        if claims.user_id != session.user_id:
            self.session_store.clear()
            self.license_path.unlink(missing_ok=True)
            raise CommercialAccessError("Activated licence user does not match the login session.")

    def _enabled_settings(self) -> CommercialSettings:
        settings = self.settings()
        if not settings.enabled:
            raise CommercialAccessError("Commercial account mode is not enabled.")
        return settings

    @staticmethod
    def _client(settings: CommercialSettings) -> ControlPlaneClient:
        return ControlPlaneClient(settings.control_plane_url)

    def _required_session(self) -> tuple[str, str, str]:
        session = self.session_store.load()
        if session is None:
            raise CommercialAccessError("Sign in before activating or refreshing a licence.")
        return session

    def _license_store(
        self,
        settings: CommercialSettings,
        device: DeviceIdentity,
    ) -> LicenseStore:
        return LicenseStore(
            trusted_keys=self._trusted_keys(settings),
            product_id=PRODUCT_ID,
            audience=AUDIENCE,
            device_id=device.device_id,
            path=self.license_path,
        )

    def _license_decision(self, settings: CommercialSettings) -> LicenseDecision:
        device = self.device_store.get_or_create()
        return self._license_store(settings, device).evaluate()

    def _trusted_keys(self, settings: CommercialSettings) -> dict[str, Ed25519PublicKey]:
        keys = self._embedded_keys()
        if settings.development_mode:
            if not self.allow_development_keys:
                raise SignatureError(
                    "Development signing keys are disabled in this application build."
                )
            key = load_public_key(settings.development_public_key_path)
            keys[public_key_id(key)] = key
        if not keys:
            raise SignatureError("No trusted commercial signing key is configured.")
        return keys

    def _embedded_keys(self) -> dict[str, Ed25519PublicKey]:
        data = read_json(self.embedded_keys_path, {"schema": TRUSTED_KEYS_SCHEMA, "keys": []})
        if not isinstance(data, dict) or data.get("schema") != TRUSTED_KEYS_SCHEMA:
            raise SignatureError("Embedded commercial keyring is invalid.")
        records = data.get("keys")
        if not isinstance(records, list):
            raise SignatureError("Embedded commercial keyring entries are invalid.")
        result: dict[str, Ed25519PublicKey] = {}
        for item in records:
            if not isinstance(item, dict) or set(item) != {"key_id", "public_key_pem"}:
                raise SignatureError("Embedded commercial key entry is invalid.")
            try:
                key = serialization.load_pem_public_key(
                    str(item["public_key_pem"]).encode("ascii")
                )
            except (ValueError, TypeError, UnicodeError) as exc:
                raise SignatureError("Embedded commercial public key is invalid.") from exc
            if not isinstance(key, Ed25519PublicKey):
                raise SignatureError("Embedded commercial key is not Ed25519.")
            key_id = str(item["key_id"] or "")
            if key_id != public_key_id(key):
                raise SignatureError("Embedded commercial key identifier does not match.")
            result[key_id] = key
        return result

    def _spool_count(self) -> int:
        if not self.spool_dir.exists():
            return 0
        return sum(1 for _path in self.spool_dir.glob("diag-*.json"))
