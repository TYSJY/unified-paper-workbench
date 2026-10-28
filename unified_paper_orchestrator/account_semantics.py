from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from .browser_session import (
    BROWSER_SESSION_EVIDENCE_SCHEMA,
    BROWSER_SESSION_EVIDENCE_TTL,
    evidence_has_value_free_shape,
    normalize_origin,
)


BROWSER_SESSION_EVIDENCE_MAX_AGE = timedelta(minutes=10)
BROWSER_SESSION_CLOCK_SKEW = timedelta(seconds=30)


ACCOUNT_SCOPE_ALIASES = {
    "email": "email",
    "email_account": "email",
    "journal": "journal_system",
    "journal_system": "journal_system",
    "submission": "journal_system",
    "submission_system": "journal_system",
    "external": "external",
    "external_service": "external",
}

ACCOUNT_FAILURE_STATUSES = {
    "blocked",
    "disabled",
    "inactive",
    "invalid_password",
    "login_failed",
    "not_configured",
    "stopped",
}
ACCOUNT_CONFIGURED_STATUSES = {
    "active",
    "available",
    "configured",
    "ready",
    "verified",
}
ACCOUNT_SESSION_STATUSES = {"browser_session", "logged_in", "session_ready"}


@dataclass(frozen=True)
class AccountReadiness:
    scope: str
    status: str
    state: str
    auth_source: str
    configured: bool
    verified: bool
    requires_secret_broker: bool


def canonical_account_scope(value: Any) -> str:
    token = str(value or "").strip().casefold()
    return ACCOUNT_SCOPE_ALIASES.get(token, token)


def canonical_account_status(value: Any) -> str:
    token = str(value or "unknown").strip().casefold()
    if "invalid_password" in token or "invalid credential" in token:
        return "invalid_password"
    if token.startswith("login_failed"):
        return "login_failed"
    if token.startswith("not_configured"):
        return "not_configured"
    if token.startswith("verified"):
        return "verified"
    if token.startswith("imported_from_legacy"):
        return "configured_unverified"
    return token


def has_explicit_live_browser_session(
    account: Mapping[str, Any],
    *,
    expected_origins: tuple[str, ...] = (),
    evidence_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    now: datetime | None = None,
) -> bool:
    """Accept only typed evidence produced by a live browser-session probe.

    A path/profile/reference string is not proof that the session is currently
    usable.  The future browser adapter owns this evidence object and must set
    all three fields after probing the target origin.
    """

    evidence = account.get("browser_session_evidence")
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("schema") != BROWSER_SESSION_EVIDENCE_SCHEMA:
        return False
    if not evidence_has_value_free_shape(evidence):
        return False
    if evidence_verifier is None or not evidence_verifier(evidence):
        return False
    account_id = str(account.get("account_id") or "").strip()
    if not account_id or str(evidence.get("account_id") or "").strip() != account_id:
        return False
    page_state = evidence.get("page_state")
    if not isinstance(page_state, Mapping):
        return False
    if not (
        page_state.get("target_type") == "page"
        and page_state.get("document_ready") is True
        and page_state.get("exact_origin") is True
        and page_state.get("login_challenge_visible") is False
        and page_state.get("authenticated_ui_visible") is True
    ):
        return False
    checked_at = str(evidence.get("checked_at") or "").strip()
    evidence_origin = normalize_origin(evidence.get("origin"))
    if not checked_at or not evidence_origin:
        return False
    expires_at = str(evidence.get("expires_at") or "").strip()
    if not expires_at:
        return False
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if checked.tzinfo is None or expiry.tzinfo is None:
        return False
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        return False
    if checked > current + BROWSER_SESSION_CLOCK_SKEW:
        return False
    if current - checked > BROWSER_SESSION_EVIDENCE_MAX_AGE:
        return False
    if expiry <= current or expiry <= checked:
        return False
    if expiry - checked > BROWSER_SESSION_EVIDENCE_TTL + BROWSER_SESSION_CLOCK_SKEW:
        return False
    normalized_expected = {
        origin
        for origin in (normalize_origin(item) for item in expected_origins)
        if origin
    }
    return not normalized_expected or evidence_origin in normalized_expected


def normalize_account_readiness(
    account: Mapping[str, Any],
    *,
    resolved_secret_available: bool,
    expected_origins: tuple[str, ...] = (),
    evidence_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    now: datetime | None = None,
) -> AccountReadiness:
    """Return the single value-free account readiness interpretation.

    ``resolved_secret_available`` must come from ``SmartCredentialResolver``;
    this function never inspects or returns a resolved password.
    """

    scope = canonical_account_scope(account.get("scope"))
    status = canonical_account_status(account.get("status"))
    has_credential_ref = bool(str(account.get("credential_ref") or "").strip())
    has_plaintext = bool(str(account.get("password") or "")) and not has_credential_ref
    has_live_session = has_explicit_live_browser_session(
        account,
        expected_origins=expected_origins,
        evidence_verifier=evidence_verifier,
        now=now,
    )

    if status in ACCOUNT_FAILURE_STATUSES:
        return AccountReadiness(scope, status, "unavailable", "none", False, False, False)

    if has_live_session:
        return AccountReadiness(
            scope,
            status,
            "live_browser_session",
            "browser_session",
            True,
            status == "verified",
            False,
        )

    if has_credential_ref:
        if not resolved_secret_available:
            return AccountReadiness(scope, status, "unavailable", "dpapi", False, False, False)
        return AccountReadiness(
            scope,
            status,
            "secret_broker_required",
            "dpapi",
            True,
            status == "verified",
            True,
        )

    if has_plaintext and resolved_secret_available:
        return AccountReadiness(
            scope,
            status,
            "secret_broker_required",
            "legacy_plaintext",
            True,
            status == "verified",
            True,
        )

    # Legacy password_ref/browser_profile strings are deliberately ignored.
    # Only the typed live-session evidence above can authorize session use.
    return AccountReadiness(scope, status, "unavailable", "none", False, False, False)
