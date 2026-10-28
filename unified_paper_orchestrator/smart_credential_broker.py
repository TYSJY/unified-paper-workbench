"""Trusted local credential broker for Smart Submission.

The broker is deliberately outside the Codex SDK/MCP boundary.  It resolves a
DPAPI secret in this process, fills only a visible password field over local
CDP, and returns value-free status/evidence to the workflow.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from . import config
from .account_site_store import (
    AccountSiteStore,
    DuplicateRecordError,
    RecordNotFoundError,
)
from .browser_session import (
    BROWSER_SESSION_EVIDENCE_SCHEMA,
    BROWSER_SESSION_EVIDENCE_TTL,
    BROWSER_SESSION_PROBE_METHOD,
    BROWSER_SESSION_SIGNATURE_ALGORITHM,
    BrowserSessionEvidenceKeyStore,
    BrowserSessionEvidenceSigner,
    normalize_origin,
)
from .io_utils import read_json
from .smart_browser_cdp import SmartBrowserCdpController, SmartBrowserCdpError
from .smart_mailbox import (
    capture_password_recovery_checkpoint,
    find_recent_inderscience_credential_reminder,
    find_recent_password_recovery_link,
    wait_for_inderscience_credential_reminder,
    wait_for_password_recovery_link,
)
from .submission_site_adapters import (
    ORCID_AUTH_ORIGINS,
    SubmissionSiteAdapter,
    has_explicit_terms_confirmation,
    submission_site_adapter,
)


BROKER_SESSION_READY = "SMART_CREDENTIAL_BROKER_SESSION_READY"
BROKER_WAITING_HUMAN = "SMART_CREDENTIAL_BROKER_WAITING_HUMAN"
BROKER_ACCOUNT_RECOVERY_REQUIRED = "SMART_CREDENTIAL_BROKER_ACCOUNT_RECOVERY_REQUIRED"
BROKER_REGISTRATION_IDENTITY_REQUIRED = (
    "SMART_CREDENTIAL_BROKER_REGISTRATION_IDENTITY_REQUIRED"
)
BROKER_SITE_UNSUPPORTED = "SMART_CREDENTIAL_BROKER_SITE_UNSUPPORTED"
BROKER_SAFE_FAILURE = "SMART_CREDENTIAL_BROKER_SAFE_FAILURE"


def _canonical_journal_name(value: Any) -> str:
    text = str(value or "").strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    match = re.fullmatch(r"\s*(.+?)\s*\(([^()]*)\)\s*", text)
    if match is None:
        return normalized
    base = match.group(1).strip()
    suffix = re.sub(r"[^a-z0-9]+", "", match.group(2).casefold())
    words = re.findall(r"[a-z0-9]+", base.casefold())
    stopwords = {"a", "an", "and", "for", "in", "of", "on", "the", "to"}
    initialisms = {
        "".join(word[0] for word in words),
        "".join(word[0] for word in words if word not in stopwords),
    }
    if base and 2 <= len(suffix) <= 16 and suffix in initialisms:
        return re.sub(r"[^a-z0-9]+", " ", base.casefold()).strip()
    return normalized


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_result(
    *,
    ok: bool,
    code: str,
    status: str,
    account_id: str = "",
    system_id: str = "",
    origin: str = "",
    next_action: str = "",
    registration_created: bool = False,
    failure_stage: str = "",
    error_type: str = "",
    recovery_requested_at: str = "",
    recovery_phase: str = "",
) -> dict[str, Any]:
    result = {
        "ok": bool(ok),
        "code": str(code),
        "status": str(status),
        "account_id": str(account_id),
        "system_id": str(system_id),
        "origin": normalize_origin(origin),
        "next_action": str(next_action),
        "registration_created": bool(registration_created),
        "value_free": True,
    }
    if failure_stage:
        result["failure_stage"] = str(failure_stage)[:80]
    if error_type:
        result["error_type"] = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", str(error_type)
        )[:120]
    if recovery_requested_at:
        result["recovery_requested_at"] = str(recovery_requested_at)[:40]
    if recovery_phase:
        result["recovery_phase"] = str(recovery_phase)[:40]
    return result


class TrustedCredentialBroker:
    """Establish exact-origin browser sessions without exposing credentials."""

    def __init__(
        self,
        *,
        store: AccountSiteStore | None = None,
        controller: SmartBrowserCdpController | None = None,
        signer: BrowserSessionEvidenceSigner | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        smart_dir = config.STATE_DIR / "smart_submission"
        self.state_path = Path(
            state_path or config.SMART_SUBMISSION_STATE_PATH
        ).expanduser().resolve()
        self.store = store or AccountSiteStore(
            smart_dir / "credential_store.local.json",
            smart_dir / "site_registry.json",
            vault_path=config.ACCOUNT_VAULT_PATH,
            legacy_vault_path=config.LEGACY_CREDENTIAL_VAULT_PATH,
            runtime_vault_path=config.CREDENTIAL_VAULT_PATH,
            smart_state_path=self.state_path,
            unified_state_path=config.UNIFIED_STATE_PATH,
        )
        self.controller = controller or SmartBrowserCdpController()
        self.signer = signer or BrowserSessionEvidenceSigner(
            BrowserSessionEvidenceKeyStore(
                config.BROWSER_SESSION_EVIDENCE_KEY_PATH
            )
        )

    def _settings(self) -> dict[str, Any]:
        state = read_json(self.state_path, {})
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        return dict(settings) if isinstance(settings, dict) else {}

    def _orcid_preferred(self, paper: Mapping[str, Any]) -> bool:
        raw = paper.get("new_account_auth_preference")
        if raw is None:
            raw = self._settings().get(
                "new_account_auth_preference",
                "orcid_first",
            )
        if isinstance(raw, bool):
            return raw
        return str(raw or "").strip().casefold() in {
            "orcid",
            "orcid_first",
            "prefer_orcid",
        }

    def _orcid_auto_authorize(self, paper: Mapping[str, Any]) -> bool:
        raw = paper.get("orcid_auto_authorize_for_login")
        if raw is None:
            raw = self._settings().get(
                "orcid_auto_authorize_for_login",
                True,
            )
        if isinstance(raw, str):
            return raw.strip().casefold() not in {"", "0", "false", "no", "off"}
        return bool(raw)

    def _site_records(
        self,
        system_id: str,
        target_journal: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        registry = read_json(self.store.site_registry_path, {})
        systems = registry.get("systems", []) if isinstance(registry, dict) else []
        journals = registry.get("journals", []) if isinstance(registry, dict) else []
        system = next(
            (
                dict(item)
                for item in systems
                if isinstance(item, Mapping)
                and str(item.get("system_id") or "").strip().casefold()
                == system_id.casefold()
            ),
            {},
        )
        normalized_target = _canonical_journal_name(target_journal)
        journal = next(
            (
                dict(item)
                for item in journals
                if isinstance(item, Mapping)
                and str(item.get("system_id") or "").strip().casefold()
                == system_id.casefold()
                and _canonical_journal_name(item.get("journal_name"))
                == normalized_target
            ),
            {},
        )
        return system, journal

    def _snapshot(
        self,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        *,
        page_id: str,
    ) -> dict[str, Any]:
        return self.controller.snapshot(
            allowed_origins,
            page_id=page_id,
            max_text_chars=6000,
            max_controls=100,
        )

    def _wait_transition(
        self,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        *,
        page_id: str,
        previous_state: str,
        previous_marker: str = "",
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, min(30.0, timeout_seconds))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                last = self._snapshot(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                )
                next_state = adapter.classify_page(last)
                next_marker = str(
                    last.get("registration_step") or last.get("path") or ""
                ).strip().casefold()
                if (
                    next_state != previous_state
                    or next_state == "authenticated"
                    or (
                        previous_marker
                        and next_marker
                        and next_marker != previous_marker.casefold()
                    )
                ):
                    return last
            except SmartBrowserCdpError:
                pass
            time.sleep(0.4)
        return last or self._snapshot(
            adapter,
            allowed_origins,
            page_id=page_id,
        )

    def _open_or_select_page(
        self,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        target_origin: str,
        *,
        start_url: str = "",
    ) -> dict[str, Any]:
        target_origin = normalize_origin(target_origin)
        destination = str(start_url or adapter.login_start_url).strip()
        destination_parts = urllib.parse.urlsplit(destination)
        destination_path = (destination_parts.path or "/").rstrip("/").casefold()
        destination_paths = {destination_path}
        # Wiley's public journal route redirects to the authenticated
        # dashboard route while preserving the journal suffix.  Treat the
        # loaded dashboard page as the same destination so a second empty SPA
        # tab is not opened and misclassified as an incomplete login.
        if (
            adapter.system_id == "wiley"
            and destination_path.startswith("/journal/")
        ):
            destination_paths.add(f"/dashboard{destination_path}")

        def target_matches(page: Mapping[str, Any]) -> bool:
            page_url = str(page.get("url") or "").strip()
            if not page_url:
                # Test/legacy controllers did not expose the URL. Their page
                # has already passed the exact-origin filter.
                return True
            page_parts = urllib.parse.urlsplit(page_url)
            page_path = (page_parts.path or "/").rstrip("/").casefold()
            if normalize_origin(page_url) != target_origin:
                return False
            if destination_path in {"", "/"}:
                return True
            return any(
                page_path == accepted_path
                or page_path.startswith(accepted_path + "/")
                for accepted_path in destination_paths
            )

        pages = self.controller.list_pages(allowed_origins)
        pages = [page for page in pages if target_matches(page)]
        pages.sort(
            key=lambda item: (
                normalize_origin(item.get("origin") or item.get("url"))
                != target_origin,
                int(item.get("cdp_port") or 9999),
            )
        )
        if pages:
            page_id = str(pages[0].get("page_id") or "")
            return self._snapshot(
                adapter,
                allowed_origins,
                page_id=page_id,
            )
        opened = self.controller.open_page(
            allowed_origins,
            destination,
            preferred_port=9224,
        )
        page_id = str(opened.get("page_id") or "")
        snapshot = self._snapshot(
            adapter,
            allowed_origins,
            page_id=page_id,
        )
        if adapter.classify_page(snapshot) == "unknown":
            return self._wait_transition(
                adapter,
                allowed_origins,
                page_id=page_id,
                previous_state="unknown",
            )
        return snapshot

    def _wait_orcid_or_target(
        self,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        *,
        target_origin: str,
        fallback_page_id: str,
        timeout_seconds: float = 25.0,
    ) -> dict[str, Any]:
        """Follow either same-tab or popup ORCID OAuth and prefer a ready callback."""

        deadline = time.monotonic() + max(1.0, min(30.0, timeout_seconds))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            snapshots: list[dict[str, Any]] = []
            try:
                pages = self.controller.list_pages(allowed_origins)
            except SmartBrowserCdpError:
                pages = []
            for page in pages:
                page_id = str(page.get("page_id") or "")
                if not page_id:
                    continue
                try:
                    current = self._snapshot(
                        adapter,
                        allowed_origins,
                        page_id=page_id,
                    )
                except SmartBrowserCdpError:
                    continue
                snapshots.append(current)
                last = current
            if not snapshots and fallback_page_id:
                try:
                    current = self._snapshot(
                        adapter,
                        allowed_origins,
                        page_id=fallback_page_id,
                    )
                except SmartBrowserCdpError:
                    current = {}
                if current:
                    snapshots.append(current)
                    last = current

            callback = next(
                (
                    item
                    for item in snapshots
                    if normalize_origin(item.get("origin")) == target_origin
                    and adapter.classify_page(item) == "authenticated"
                ),
                None,
            )
            if callback is not None:
                return callback
            orcid_page = next(
                (
                    item
                    for item in snapshots
                    if normalize_origin(item.get("origin")) in ORCID_AUTH_ORIGINS
                ),
                None,
            )
            if orcid_page is not None:
                controls = [
                    item
                    for item in (orcid_page.get("controls") or [])
                    if isinstance(item, Mapping)
                    and not bool(item.get("disabled"))
                ]
                password_ready = any(
                    str(item.get("type") or "").casefold() == "password"
                    for item in controls
                )
                authorization_ready = (
                    adapter.orcid_authorization_control(orcid_page) is not None
                )
                waiting_status, _waiting_action = self._waiting_from_snapshot(
                    orcid_page
                )
                terminal_identity_state = waiting_status in {
                    "human_verification",
                    "waiting_email_verification",
                    "waiting_terms_confirmation",
                }
                if (
                    password_ready
                    or authorization_ready
                    or terminal_identity_state
                    or self._orcid_login_rejected(orcid_page)
                ):
                    return orcid_page
            target_page = next(
                (
                    item
                    for item in snapshots
                    if normalize_origin(item.get("origin")) == target_origin
                ),
                None,
            )
            if target_page is not None and adapter.classify_page(target_page) in {
                "authenticated",
                "profile_update",
                "registration",
                "human_verification",
                "terms_confirmation",
            }:
                return target_page
            time.sleep(0.35)
        if last:
            return last
        return self._snapshot(
            adapter,
            allowed_origins,
            page_id=fallback_page_id,
        )

    def _trusted_identity_click(
        self,
        allowed_origins: tuple[str, ...],
        ref: str,
        *,
        page_id: str,
    ) -> dict[str, Any]:
        """Use a real pointer gesture when the controller supports one."""

        pointer_click = getattr(self.controller, "trusted_pointer_click", None)
        if callable(pointer_click):
            return pointer_click(
                allowed_origins,
                ref,
                page_id=page_id,
            )
        return self.controller.click(
            allowed_origins,
            ref,
            page_id=page_id,
        )

    @staticmethod
    def _waiting_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[str, str]:
        text = str(snapshot.get("text") or "").casefold()
        if any(
            token in text
            for token in (
                "captcha",
                "recaptcha",
                "cloudflare",
                "two-factor",
                "two factor",
                "one-time",
                "verification code",
                "security code",
                "验证码",
                "双重验证",
            )
        ):
            return "human_verification", "complete_captcha_or_2fa_then_resume"
        if any(
            token in text
            for token in (
                "verify your email",
                "verification link",
                "check your inbox",
                "confirm your email",
                "验证您的电子邮件",
                "检查您的邮箱",
                "确认您的电子邮件",
            )
        ):
            return "waiting_email_verification", "verify_email_then_resume"
        if has_explicit_terms_confirmation(snapshot):
            return "waiting_terms_confirmation", "accept_site_terms_then_resume"
        return "login_incomplete", "continue_visible_login_then_resume"

    @staticmethod
    def _existing_account_notice(snapshot: Mapping[str, Any]) -> bool:
        text = re.sub(
            r"\s+", " ", str(snapshot.get("text") or "").casefold()
        ).strip()
        return any(
            marker in text
            for marker in (
                "associated with an existing account",
                "already associated with an account",
                "already linked to an existing account",
                "already have an account using this email address",
                "email address is already registered",
                "email address already exists in our database",
                "email address already exists in the database",
                "entered email address already exists",
                "已关联到现有帐户",
                "已关联到现有账户",
                "已经关联到现有帐户",
                "已经关联到现有账户",
                "邮箱地址已存在于我们的数据库中",
                "电子邮件地址已存在于我们的数据库中",
                "输入的邮箱地址已经存在",
                "输入的电子邮件地址已经存在",
            )
        )

    @staticmethod
    def _inderscience_existing_account_result(
        *,
        paper: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        account_id: str,
        system_id: str,
    ) -> dict[str, Any]:
        """Stop duplicate registration and preserve the paper's old account binding."""

        bound_account_id = str(
            paper.get("credential_account_id") or account_id or ""
        ).strip()
        return _safe_result(
            ok=False,
            code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
            status="account_support_recovery_required",
            account_id=bound_account_id,
            system_id=system_id,
            origin=str(snapshot.get("origin") or ""),
            next_action="contact_inderscience_support_for_account_recovery",
            registration_created=False,
            failure_stage="inderscience_existing_email",
        )

    @staticmethod
    def _recovery_control(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        phrases = (
            "reset password",
            "forgot password",
            "forgotten password",
            "重置密码",
            "忘记密码",
        )
        for control in SubmissionSiteAdapter.visible_controls(snapshot):
            if str(control.get("tag") or "").casefold() not in {
                "a",
                "button",
                "input",
            }:
                continue
            label = re.sub(
                r"\s+", " ", str(control.get("label") or "").casefold()
            ).strip()
            if str(control.get("ref") or "") and any(
                phrase in label for phrase in phrases
            ):
                return control
        return None

    @staticmethod
    def _profile_continue_control(
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        phrases = {
            "continue to browse profile",
            "continue to profile",
            "\u7ee7\u7eed\u6d4f\u89c8\u4e2a\u4eba\u8d44\u6599",
            "\u7ee7\u7eed\u8fdb\u5165\u4e2a\u4eba\u8d44\u6599",
        }
        for control in SubmissionSiteAdapter.visible_controls(snapshot):
            if str(control.get("tag") or "").casefold() not in {
                "a",
                "button",
                "input",
            }:
                continue
            label = re.sub(
                r"\s+", " ", str(control.get("label") or "").casefold()
            ).strip()
            if str(control.get("ref") or "") and label in phrases:
                return control
        return None

    def _complete_scholar_one_profile_update(
        self,
        *,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        account_id: str,
        system_id: str,
    ) -> dict[str, Any]:
        """Complete ScholarOne's required profile gate with privacy defaults."""

        page_id = str(snapshot.get("page_id") or "")
        for _ in range(3):
            if adapter.classify_page(snapshot) != "profile_update":
                return {"snapshot": snapshot}
            controls = adapter.visible_controls(snapshot)
            consent_controls = [
                item
                for item in controls
                if str(item.get("name") or "").casefold()
                == "consent_demographic_question"
            ]
            if consent_controls and not any(
                bool(item.get("checked")) for item in consent_controls
            ):
                decline = next(
                    (
                        item
                        for item in consent_controls
                        if str(item.get("id") or "").casefold() == "consent_no"
                        and str(item.get("ref") or "")
                    ),
                    None,
                )
                if decline is None:
                    return _safe_result(
                        ok=False,
                        code=BROKER_WAITING_HUMAN,
                        status="profile_update_incomplete",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="complete_visible_profile_then_resume",
                        failure_stage="profile_demographic_consent",
                    )
                self.controller.click(
                    allowed_origins,
                    str(decline["ref"]),
                    page_id=page_id,
                )
                snapshot = self._snapshot(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                )
                controls = adapter.visible_controls(snapshot)

            terms = next(
                (
                    item
                    for item in controls
                    if str(item.get("id") or "").casefold() == "tc_agreed"
                ),
                None,
            )
            if terms is not None and not bool(terms.get("checked")):
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status="waiting_terms_confirmation",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="accept_site_terms_then_resume",
                    failure_stage="profile_terms_confirmation",
                )
            next_control = next(
                (
                    item
                    for item in controls
                    if str(item.get("ref") or "")
                    and str(item.get("label") or "").strip().casefold()
                    in {"next_button", "finish_button"}
                ),
                None,
            )
            if next_control is None:
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status="profile_update_incomplete",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="complete_visible_profile_then_resume",
                    failure_stage="profile_next_control",
                )
            self.controller.click(
                allowed_origins,
                str(next_control["ref"]),
                page_id=page_id,
            )
            snapshot = self._wait_transition(
                adapter,
                allowed_origins,
                page_id=page_id,
                previous_state="profile_update",
            )
        if adapter.classify_page(snapshot) != "profile_update":
            return {"snapshot": snapshot}
        return _safe_result(
            ok=False,
            code=BROKER_WAITING_HUMAN,
            status="profile_update_incomplete",
            account_id=account_id,
            system_id=system_id,
            origin=snapshot.get("origin"),
            next_action="complete_visible_profile_then_resume",
            failure_stage="profile_page_limit",
        )

    @staticmethod
    def _scholar_one_reset_url(target_url: str) -> tuple[str, str]:
        parts = urllib.parse.urlsplit(str(target_url or ""))
        segments = [item for item in parts.path.split("/") if item]
        journal_slug = segments[0] if segments else ""
        if (
            parts.scheme.casefold() != "https"
            or parts.hostname != "mc.manuscriptcentral.com"
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", journal_slug)
        ):
            return "", ""
        return (
            urllib.parse.urlunsplit(
                (
                    "https",
                    parts.netloc,
                    f"/site/{journal_slug}/auth/reset-password",
                    "",
                    "",
                )
            ),
            f"/{journal_slug}",
        )

    def _record_session(
        self,
        *,
        account_id: str,
        system_id: str,
        target_origin: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        runtime_origin = normalize_origin(snapshot.get("origin"))
        if runtime_origin != target_origin:
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status="login_incomplete",
                account_id=account_id,
                system_id=system_id,
                origin=runtime_origin,
                next_action="continue_visible_login_then_resume",
            )
        checked = datetime.now(timezone.utc)
        unsigned = {
            "schema": BROWSER_SESSION_EVIDENCE_SCHEMA,
            "evidence_id": uuid4().hex,
            "live": True,
            "account_id": account_id,
            "origin": target_origin,
            "checked_at": _utc_iso(checked),
            "expires_at": _utc_iso(checked + BROWSER_SESSION_EVIDENCE_TTL),
            "probe_method": BROWSER_SESSION_PROBE_METHOD,
            "cdp_port": int(snapshot.get("cdp_port") or 9224),
            "page_state": {
                "target_type": "page",
                "document_ready": True,
                "exact_origin": True,
                "login_challenge_visible": False,
                "authenticated_ui_visible": True,
            },
            "value_free": True,
            "signature_alg": BROWSER_SESSION_SIGNATURE_ALGORITHM,
        }
        evidence = self.signer.sign(unsigned)
        self.store.record_browser_session_evidence(
            account_id,
            system_id,
            target_origin,
            evidence,
            signer=self.signer,
        )
        return _safe_result(
            ok=True,
            code=BROKER_SESSION_READY,
            status="session_ready",
            account_id=account_id,
            system_id=system_id,
            origin=target_origin,
            next_action="dispatch_fixed_sdk_session",
        )

    def _recover_authenticated_redirect(
        self,
        *,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        target_origin: str,
        account_id: str,
        system_id: str,
        start_url: str = "",
    ) -> dict[str, Any] | None:
        """Recover when a successful login redirect outlives one CDP action."""

        if not account_id:
            return None
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            try:
                snapshot = self._open_or_select_page(
                    adapter,
                    allowed_origins,
                    target_origin,
                    start_url=start_url,
                )
            except Exception:
                time.sleep(0.25)
                continue
            if (
                normalize_origin(snapshot.get("origin")) != target_origin
                or adapter.classify_page(snapshot) != "authenticated"
            ):
                return None
            try:
                return self._record_session(
                    account_id=account_id,
                    system_id=system_id,
                    target_origin=target_origin,
                    snapshot=snapshot,
                )
            except Exception:
                return None
        return None

    @staticmethod
    def _usable_profile_value(value: Any) -> str:
        text = str(value or "").strip()
        normalized = re.sub(r"\s+", " ", text).strip().casefold()
        if normalized in {
            "",
            "-",
            "n/a",
            "na",
            "none",
            "not available",
            "not provided",
            "todo",
            "tbd",
            "email:",
        }:
            return ""
        if normalized.startswith("[todo") or normalized.startswith("<todo"):
            return ""
        return text

    @classmethod
    def _author_profile(cls, paper: Mapping[str, Any]) -> dict[str, str]:
        """Merge paper-local facts with the workspace's default author template."""

        candidates: list[Path] = []
        paper_dir = str(paper.get("paper_dir") or "").strip()
        if paper_dir:
            candidates.append(Path(paper_dir) / "author_info.md")
        package_dir = str(paper.get("source_package_dir") or "").strip()
        if package_dir:
            candidates.append(Path(package_dir).parent / "author_info.md")
        text = ""
        for path in candidates:
            try:
                if path.is_file():
                    text = path.read_text(encoding="utf-8", errors="replace")
                    break
            except OSError:
                continue

        profile: dict[str, str] = {}
        paper_corresponding = paper.get("corresponding_author")
        if isinstance(paper_corresponding, Mapping):
            profile["prefix"] = cls._usable_profile_value(
                paper_corresponding.get("prefix")
                or paper_corresponding.get("honorific")
                or paper_corresponding.get("author_title")
            )
            profile["full_name"] = cls._usable_profile_value(
                paper_corresponding.get("name")
            )
            profile["email"] = cls._usable_profile_value(
                paper_corresponding.get("email")
            )
            profile["institution"] = cls._usable_profile_value(
                paper_corresponding.get("affiliation")
                or paper_corresponding.get("institution")
            )
            profile["country"] = cls._usable_profile_value(
                paper_corresponding.get("country")
            )
            profile["orcid"] = cls._usable_profile_value(
                paper_corresponding.get("orcid")
            )

        section_match = re.search(
            r"(?ims)^##\s*Corresponding Author\s*$\s*(.*?)(?=^##\s|\Z)",
            text,
        )
        corresponding_section = section_match.group(1) if section_match else ""

        def markdown_field(source: str, label: str) -> str:
            match = re.search(
                rf"(?mi)^\s*(?:[-*]\s*)?{label}\s*:\s*(.*?)\s*$",
                source,
            )
            return cls._usable_profile_value(match.group(1) if match else "")

        profile["full_name"] = profile.get("full_name") or markdown_field(
            corresponding_section or text,
            r"(?:Name|Full Name)",
        )
        profile["email"] = profile.get("email") or markdown_field(
            corresponding_section or text,
            "Email",
        )
        profile["orcid"] = profile.get("orcid") or markdown_field(
            corresponding_section or text,
            "ORCID",
        )
        profile["prefix"] = profile.get("prefix") or markdown_field(
            corresponding_section or text,
            r"(?:Prefix|Honorific)",
        )
        if not profile.get("email"):
            email_match = re.search(
                r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                text,
            )
            profile["email"] = email_match.group(0) if email_match else ""

        affiliation_match = re.search(
            r"(?ims)^\s*1\.\s*Institution\s*:\s*(?P<institution>.*?)\s*$"
            r"(?P<body>.*?)(?=^\s*\d+\.\s*Institution\s*:|^##\s|\Z)",
            text,
        )
        if affiliation_match:
            body = affiliation_match.group("body")
            profile["institution"] = profile.get("institution") or cls._usable_profile_value(
                affiliation_match.group("institution")
            )
            for key, label in (
                ("department", "Department"),
                ("city", "City"),
                ("country", "Country"),
                ("postal_code", "Postal Code"),
            ):
                profile[key] = profile.get(key) or markdown_field(body, label)

        template_candidates: list[Path] = []
        if paper_dir:
            resolved_paper_dir = Path(paper_dir).expanduser().resolve()
            if resolved_paper_dir.parent.name.casefold() == "papers":
                template_candidates.append(
                    resolved_paper_dir.parent.parent
                    / "controller"
                    / "author_templates.json"
                )
        for template_path in template_candidates:
            data = read_json(template_path, {})
            templates = data.get("templates") if isinstance(data, Mapping) else []
            if not isinstance(templates, list):
                continue
            default_id = str(data.get("default_template_id") or "")
            template = next(
                (
                    item
                    for item in templates
                    if isinstance(item, Mapping)
                    and (
                        str(item.get("id") or "") == default_id
                        or (not default_id and bool(item.get("is_default")))
                    )
                ),
                None,
            )
            if not isinstance(template, Mapping):
                continue
            form = template.get("form")
            if not isinstance(form, Mapping):
                continue
            authors = form.get("authors")
            authors = authors if isinstance(authors, list) else []
            author = next(
                (
                    item
                    for item in authors
                    if isinstance(item, Mapping) and bool(item.get("corresponding"))
                ),
                next((item for item in authors if isinstance(item, Mapping)), {}),
            )
            corresponding = form.get("corresponding_author")
            corresponding = corresponding if isinstance(corresponding, Mapping) else {}
            profile["full_name"] = profile.get("full_name") or cls._usable_profile_value(
                corresponding.get("name") or author.get("name")
            )
            profile["email"] = profile.get("email") or cls._usable_profile_value(
                corresponding.get("email") or author.get("email")
            )
            profile["orcid"] = profile.get("orcid") or cls._usable_profile_value(
                corresponding.get("orcid") or author.get("orcid")
            )
            profile["prefix"] = profile.get("prefix") or cls._usable_profile_value(
                corresponding.get("prefix")
                or corresponding.get("honorific")
                or corresponding.get("author_title")
                or author.get("prefix")
                or author.get("honorific")
                or author.get("author_title")
            )
            affiliation_id = str(author.get("affiliation") or "1").strip()
            affiliations = form.get("affiliations")
            affiliations = affiliations if isinstance(affiliations, list) else []
            affiliation = next(
                (
                    item
                    for item in affiliations
                    if isinstance(item, Mapping)
                    and str(item.get("index") or "") == affiliation_id
                ),
                next((item for item in affiliations if isinstance(item, Mapping)), {}),
            )
            for key in (
                "institution",
                "department",
                "city",
                "country",
                "postal_code",
            ):
                profile[key] = profile.get(key) or cls._usable_profile_value(
                    affiliation.get(key)
                )
            break

        names = [item for item in profile.get("full_name", "").split() if item]
        profile["first_name"] = (
            " ".join(names[:-1]) if len(names) > 1 else profile.get("full_name", "")
        )
        profile["last_name"] = (
            names[-1] if len(names) > 1 else profile.get("full_name", "")
        )
        for key in (
            "middle_name",
            "address1",
            "state",
            "phone",
            "degree",
            "prefix",
        ):
            profile.setdefault(key, "")
        return {key: cls._usable_profile_value(value) for key, value in profile.items()}

    def _preferred_orcid_account(
        self,
        profile: Mapping[str, str],
    ) -> dict[str, Any] | None:
        lister = getattr(self.store, "list_account_summaries", None)
        if not callable(lister):
            return None
        try:
            rows = lister(query="orcid", scope="external")
        except Exception:
            return None
        wanted_orcid = re.sub(
            r"[^0-9x]+",
            "",
            str(profile.get("orcid") or "").casefold(),
        )
        wanted_email = str(profile.get("email") or "").strip().casefold()
        candidates: list[tuple[int, int, str, str, dict[str, Any]]] = []
        for row_value in rows if isinstance(rows, list) else []:
            if not isinstance(row_value, Mapping) or not bool(
                row_value.get("auto_fill_ready")
            ):
                continue
            row = dict(row_value)
            combined = " ".join(
                str(row.get(key) or "")
                for key in ("provider", "login_url", "username")
            ).casefold()
            if "orcid" not in combined:
                continue
            identity = str(row.get("username") or "").strip().casefold()
            identity_orcid = re.sub(r"[^0-9x]+", "", identity)
            score = 1
            if wanted_orcid and identity_orcid == wanted_orcid:
                score += 8
            if wanted_email and identity == wanted_email:
                score += 4
            if normalize_origin(row.get("login_url")) in ORCID_AUTH_ORIGINS:
                score += 2
            source_rank = {
                "smart": 3,
                "vault": 2,
                "legacy": 1,
            }.get(str(row.get("source_kind") or "").casefold(), 0)
            candidates.append(
                (
                    score,
                    source_rank,
                    str(row.get("last_verified_at") or ""),
                    str(row.get("record_key") or ""),
                    row,
                )
            )
        for _score, _source_rank, _verified_at, _record_key, row in sorted(
            candidates,
            key=lambda item: item[:4],
            reverse=True,
        ):
            record_key = str(row.get("record_key") or "").strip()
            if not record_key:
                continue
            try:
                account = self.store.get_account(record_key)
            except Exception:
                continue
            if bool(account.get("auto_fill_ready")):
                return account
        return None

    def _linked_orcid_account(
        self,
        *,
        source_account: Mapping[str, Any],
        adapter: SubmissionSiteAdapter,
        system_id: str,
        target_journal: str,
        profile: Mapping[str, str],
    ) -> dict[str, Any] | None:
        source_record_key = str(source_account.get("record_key") or "").strip()
        if not source_record_key:
            return None
        digest = hashlib.sha256(
            f"{system_id}\x1f{source_record_key.casefold()}".encode("utf-8")
        ).hexdigest()[:16]
        account_id = f"auto-{system_id}-orcid-{digest}"
        try:
            return self.store.get_account(f"smart:{account_id}")
        except RecordNotFoundError:
            pass
        creator = getattr(
            self.store,
            "create_smart_account_with_credential_reference",
            None,
        )
        if not callable(creator):
            return None
        username = str(
            source_account.get("username")
            or source_account.get("email")
            or profile.get("orcid")
            or profile.get("email")
            or ""
        ).strip()
        try:
            return creator(
                {
                    "account_id": account_id,
                    "scope": "journal_system",
                    "provider": f"{adapter.label} via ORCID",
                    "username": username,
                    "email": str(profile.get("email") or "").strip(),
                    "login_url": adapter.login_start_url,
                    "system_id": system_id,
                    "related_journals": target_journal,
                    "status": "registration_pending",
                    "auth_method": "orcid",
                    "identity_provider": "orcid",
                    "notes": (
                        "Journal account linked through the preferred ORCID OAuth route."
                    ),
                },
                credential_record_key=source_record_key,
            )
        except DuplicateRecordError:
            try:
                return self.store.get_account(f"smart:{account_id}")
            except RecordNotFoundError:
                return None

    @staticmethod
    def _orcid_login_rejected(snapshot: Mapping[str, Any]) -> bool:
        text = re.sub(
            r"\s+",
            " ",
            str(snapshot.get("text") or "").casefold(),
        )
        return any(
            marker in text
            for marker in (
                "incorrect email address or password",
                "email address or password is incorrect",
                "invalid email or password",
                "invalid orcid id or password",
                "无法识别电子邮件地址或密码",
                "邮箱或密码不正确",
            )
        )

    def _authenticate_with_orcid(
        self,
        *,
        paper: Mapping[str, Any],
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        target_origin: str,
        system_id: str,
        target_journal: str,
        account: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Use a saved ORCID identity first; return ``None`` for safe site fallback."""

        if not self._orcid_preferred(paper):
            return None
        orcid_control = adapter.orcid_login_control(snapshot)
        if orcid_control is None:
            return None
        profile = self._author_profile(paper)
        linked_account = dict(account) if isinstance(account, Mapping) else {}
        if linked_account:
            if str(linked_account.get("auth_method") or "").casefold() != "orcid":
                return None
        else:
            source_account = self._preferred_orcid_account(profile)
            if source_account is None:
                return None
            linked = self._linked_orcid_account(
                source_account=source_account,
                adapter=adapter,
                system_id=system_id,
                target_journal=target_journal,
                profile=profile,
            )
            if linked is None:
                return None
            linked_account = dict(linked)

        account_id = str(linked_account.get("account_id") or "").strip()
        if not account_id:
            return None
        registration_created = (
            str(linked_account.get("status") or "").casefold()
            == "registration_pending"
        )
        oauth_origins = tuple(
            dict.fromkeys((*allowed_origins, *ORCID_AUTH_ORIGINS))
        )
        page_id = str(snapshot.get("page_id") or "")
        self._trusted_identity_click(
            oauth_origins,
            str(orcid_control.get("ref") or ""),
            page_id=page_id,
        )
        current = self._wait_orcid_or_target(
            adapter,
            oauth_origins,
            target_origin=target_origin,
            fallback_page_id=page_id,
        )
        current_origin = normalize_origin(current.get("origin"))
        if current_origin == target_origin:
            return {
                "account_id": account_id,
                "snapshot": current,
                "registration_created": registration_created,
                "orcid_authenticated": True,
            }
        if current_origin not in ORCID_AUTH_ORIGINS:
            status, action = self._waiting_from_snapshot(current)
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status=status,
                account_id=account_id,
                system_id=system_id,
                origin=current_origin,
                next_action=action,
                registration_created=registration_created,
                failure_stage="orcid_redirect",
            )

        status, action = self._waiting_from_snapshot(current)
        if status in {
            "human_verification",
            "waiting_email_verification",
            "waiting_terms_confirmation",
        }:
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status=status,
                account_id=account_id,
                system_id=system_id,
                origin=current_origin,
                next_action=action,
                registration_created=registration_created,
                failure_stage="orcid_login",
            )
        if self._orcid_login_rejected(current):
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="orcid_saved_password_rejected",
                account_id=account_id,
                system_id=system_id,
                origin=current_origin,
                next_action="update_saved_orcid_password_then_resume",
                registration_created=registration_created,
                failure_stage="orcid_login",
            )

        authorization = adapter.orcid_authorization_control(current)
        password_control = adapter.first_control(current, types=("password",))
        if authorization is None and password_control is not None:
            identity = str(
                linked_account.get("username")
                or linked_account.get("email")
                or profile.get("orcid")
                or profile.get("email")
                or ""
            ).strip()
            identity_control = adapter.first_control(
                current,
                types=("text", "email"),
            )
            if identity_control is not None and identity:
                self.controller.fill(
                    oauth_origins,
                    str(identity_control.get("ref") or ""),
                    identity,
                    page_id=str(current.get("page_id") or page_id),
                )
            secret = self.store.reveal_password(f"smart:{account_id}")
            self.controller.fill_trusted_secret(
                oauth_origins,
                str(password_control.get("ref") or ""),
                secret,
                page_id=str(current.get("page_id") or page_id),
            )
            secret = ""
            submit = adapter.submit_control(current)
            if submit is not None:
                self._trusted_identity_click(
                    oauth_origins,
                    str(submit.get("ref") or ""),
                    page_id=str(current.get("page_id") or page_id),
                )
            else:
                self.controller.submit_login_form(
                    oauth_origins,
                    str(password_control.get("ref") or ""),
                    page_id=str(current.get("page_id") or page_id),
                )
            current = self._wait_orcid_or_target(
                adapter,
                oauth_origins,
                target_origin=target_origin,
                fallback_page_id=str(current.get("page_id") or page_id),
            )
            current_origin = normalize_origin(current.get("origin"))
            if self._orcid_login_rejected(current):
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="orcid_saved_password_rejected",
                    account_id=account_id,
                    system_id=system_id,
                    origin=current_origin,
                    next_action="update_saved_orcid_password_then_resume",
                    registration_created=registration_created,
                    failure_stage="orcid_login",
                )
            if current_origin == target_origin:
                return {
                    "account_id": account_id,
                    "snapshot": current,
                    "registration_created": registration_created,
                    "orcid_authenticated": True,
                }
            authorization = adapter.orcid_authorization_control(current)

        if (
            normalize_origin(current.get("origin")) in ORCID_AUTH_ORIGINS
            and authorization is not None
        ):
            if not self._orcid_auto_authorize(paper):
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status="waiting_orcid_authorization",
                    account_id=account_id,
                    system_id=system_id,
                    origin=current.get("origin"),
                    next_action="authorize_orcid_then_resume",
                    registration_created=registration_created,
                )
            self._trusted_identity_click(
                oauth_origins,
                str(authorization.get("ref") or ""),
                page_id=str(current.get("page_id") or page_id),
            )
            current = self._wait_orcid_or_target(
                adapter,
                oauth_origins,
                target_origin=target_origin,
                fallback_page_id=str(current.get("page_id") or page_id),
            )
            if normalize_origin(current.get("origin")) == target_origin:
                return {
                    "account_id": account_id,
                    "snapshot": current,
                    "registration_created": registration_created,
                    "orcid_authenticated": True,
                }

        status, action = self._waiting_from_snapshot(current)
        if normalize_origin(current.get("origin")) in ORCID_AUTH_ORIGINS:
            status = "orcid_login_incomplete"
            action = "complete_visible_orcid_login_then_resume"
        return _safe_result(
            ok=False,
            code=BROKER_WAITING_HUMAN,
            status=status,
            account_id=account_id,
            system_id=system_id,
            origin=current.get("origin"),
            next_action=action,
            registration_created=registration_created,
            failure_stage="orcid_oauth",
        )

    @staticmethod
    def _registration_field_value(
        marker: str,
        *,
        profile: Mapping[str, str],
        account: Mapping[str, Any],
    ) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(marker).casefold()).strip("_")
        email = str(account.get("email") or account.get("username") or "").strip()
        username = str(account.get("username") or account.get("email") or "").strip()
        if any(
            token in normalized
            for token in (
                "cc_email",
                "ccemail",
                "email2",
                "alternate_email",
                "alternateemail",
                "secondary_email",
                "secondaryemail",
                "backup_email",
                "backupemail",
            )
        ):
            return ""
        if "email_confirm1" in normalized or "confirm_email" in normalized:
            return email
        if (
            normalized.endswith("email")
            or normalized.endswith("email1")
            or normalized in {"email", "email_address"}
        ):
            return email
        if "orcid" in normalized:
            return str(profile.get("orcid") or "")
        if "title" in normalized or "prefix" in normalized:
            return str(
                account.get("registration_title")
                or profile.get("prefix")
                or ""
            )
        if any(token in normalized for token in ("firstname", "first_name", "given_name")):
            return str(profile.get("first_name") or "")
        if "middlename" in normalized or "middle_name" in normalized:
            return str(profile.get("middle_name") or "")
        if any(
            token in normalized
            for token in ("lastname", "last_name", "family_name", "surname")
        ):
            return str(profile.get("last_name") or "")
        if normalized in {"name", "full_name", "user_name"}:
            return str(profile.get("full_name") or "")
        if any(token in normalized for token in ("userid", "user_id", "login_id", "username")):
            return username
        if any(token in normalized for token in ("institution", "organization", "affiliation")):
            return str(profile.get("institution") or "")
        if "biography" in normalized or normalized.endswith("bio"):
            configured = str(account.get("registration_biography") or "").strip()
            if configured:
                return configured
            full_name = str(profile.get("full_name") or "").strip()
            institution = str(profile.get("institution") or "").strip()
            country = str(profile.get("country") or "").strip()
            if full_name and institution:
                suffix = f", {country}" if country else ""
                return f"{full_name} is affiliated with {institution}{suffix}."
            return ""
        if "department" in normalized:
            return str(profile.get("department") or "")
        if any(token in normalized for token in ("address1", "address_1", "street")):
            return str(profile.get("address1") or "")
        if "city" in normalized:
            return str(profile.get("city") or "")
        if any(token in normalized for token in ("state", "province", "region")):
            return str(profile.get("state") or "")
        if any(token in normalized for token in ("postal", "postcode", "zip")):
            return str(profile.get("postal_code") or "")
        if "country" in normalized:
            return str(
                account.get("registration_country")
                or profile.get("country")
                or ""
            )
        if any(token in normalized for token in ("phone", "telephone", "mobile")):
            return str(profile.get("phone") or "")
        if "degree" in normalized:
            return str(profile.get("degree") or "")
        return ""

    @staticmethod
    def _registration_username(
        system_id: str,
        email: str,
        profile: Mapping[str, str],
    ) -> str:
        if str(system_id or "").casefold() != "inderscience":
            return str(email or "").strip()
        local = str(email or "").partition("@")[0]
        local = re.sub(r"[^A-Za-z0-9]+", "", local).strip()
        if not local:
            local = re.sub(
                r"[^A-Za-z0-9]+",
                "",
                str(profile.get("full_name") or "author"),
            ).strip()
        local = (local or "author")[:18]
        digest = hashlib.sha256(str(email or "").casefold().encode("utf-8")).hexdigest()[:8]
        return f"{local}_{digest}"[:30]

    @staticmethod
    def _registration_secret(system_id: str) -> str:
        if str(system_id or "").casefold() != "inderscience":
            return secrets.token_urlsafe(30)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_!@#"
        tail = "".join(secrets.choice(alphabet) for _ in range(18))
        return f"Aa1!{tail}"

    @staticmethod
    def _matching_select_option(control: Mapping[str, Any], desired: str) -> str:
        wanted = re.sub(r"[^a-z0-9]+", "", str(desired).casefold())
        if not wanted:
            return ""
        aliases = {wanted}
        if wanted in {"china", "cn", "chn", "peoplesrepublicofchina"}:
            aliases.update({"china", "cn", "chn", "peoplesrepublicofchina"})
        options = control.get("options")
        if not isinstance(options, list):
            return str(desired)
        for option in options:
            if not isinstance(option, Mapping):
                continue
            for candidate in (option.get("value"), option.get("label")):
                normalized = re.sub(
                    r"[^a-z0-9]+", "", str(candidate or "").casefold()
                )
                if normalized and normalized in aliases:
                    return str(option.get("value") or option.get("label") or "")
        return str(desired)

    @classmethod
    def _author_identity(cls, paper: Mapping[str, Any]) -> tuple[str, str]:
        profile = cls._author_profile(paper)
        return profile.get("full_name", ""), profile.get("email", "")

    @staticmethod
    def _legacy_author_identity(paper: Mapping[str, Any]) -> tuple[str, str]:
        candidates: list[Path] = []
        paper_dir = str(paper.get("paper_dir") or "").strip()
        if paper_dir:
            candidates.append(Path(paper_dir) / "author_info.md")
        package_dir = str(paper.get("source_package_dir") or "").strip()
        if package_dir:
            candidates.append(Path(package_dir).parent / "author_info.md")
        text = ""
        for path in candidates:
            try:
                if path.is_file():
                    text = path.read_text(encoding="utf-8", errors="replace")
                    break
            except OSError:
                continue
        email_match = re.search(
            r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            text,
        )
        email = email_match.group(0) if email_match else ""
        name = ""
        for pattern in (
            r"(?mi)^\s*(?:name|full name|姓名)\s*:\s*(.+?)\s*$",
            r"(?mi)^\s*[-*]\s*(?:name|full name|姓名)\s*:\s*(.+?)\s*$",
        ):
            match = re.search(pattern, text)
            if match:
                name = match.group(1).strip()
                break
        return name, email

    @staticmethod
    def _recent_recovery_request(
        paper: Mapping[str, Any],
        *,
        phase: str,
    ) -> datetime | None:
        recorded_phase = str(
            paper.get("credential_recovery_phase") or ""
        ).strip().casefold()
        if recorded_phase and recorded_phase != str(phase).casefold():
            return None
        value = str(paper.get("credential_recovery_requested_at") or "").strip()
        if not value:
            return None
        try:
            requested = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        requested = requested.astimezone(timezone.utc)
        age = datetime.now(timezone.utc) - requested
        if timedelta(minutes=-1) <= age <= timedelta(hours=24):
            return requested
        return None

    def _recovery_request_expired(self, requested: datetime) -> bool:
        raw = self._settings().get(
            "account_recovery_auto_register_after_seconds",
            120,
        )
        try:
            wait_seconds = float(raw)
        except (TypeError, ValueError):
            wait_seconds = 120.0
        wait_seconds = max(45.0, min(900.0, wait_seconds))
        return datetime.now(timezone.utc) - requested >= timedelta(
            seconds=wait_seconds
        )

    @staticmethod
    def _inderscience_recovery_control(
        snapshot: Mapping[str, Any],
        *,
        control_id: str = "",
        reminder_kind: str = "",
    ) -> dict[str, Any] | None:
        controls = SubmissionSiteAdapter.visible_controls(snapshot)
        wanted_id = str(control_id or "").casefold()
        if wanted_id:
            return next(
                (
                    item
                    for item in controls
                    if str(item.get("id") or "").casefold() == wanted_id
                    and str(item.get("ref") or "")
                ),
                None,
            )
        candidates = [
            item
            for item in controls
            if str(item.get("ref") or "")
            and str(item.get("type") or "").casefold() in {"button", "submit"}
            and str(item.get("name") or "").casefold() == "cmdsubmit"
        ]
        kind = str(reminder_kind or "").casefold()
        tokens = (
            ("username", "user name", "用户名")
            if kind == "username"
            else ("password", "密码")
        )
        labelled = next(
            (
                item
                for item in candidates
                if any(
                    token in str(item.get("label") or "").casefold()
                    for token in tokens
                )
            ),
            None,
        )
        if labelled is not None:
            return labelled
        if not candidates:
            return None
        return candidates[0] if kind == "username" else candidates[-1]

    def _request_inderscience_reminder(
        self,
        *,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        account_id: str,
        account: Mapping[str, Any],
        profile: Mapping[str, str],
        system_id: str,
        reminder_kind: str,
    ) -> dict[str, Any]:
        identity = str(account.get("email") or "").strip()
        username = str(account.get("username") or "").strip()
        last_name = str(profile.get("last_name") or "").strip()
        if not identity or (reminder_kind == "username" and not last_name):
            return _safe_result(
                ok=False,
                code=BROKER_REGISTRATION_IDENTITY_REQUIRED,
                status="registration_identity_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="complete_author_name_and_email_then_resume",
                recovery_phase=reminder_kind,
            )
        try:
            checkpoint = capture_password_recovery_checkpoint(
                identity,
                store=self.store,
            )
        except Exception as exc:
            return _safe_result(
                ok=False,
                code=BROKER_SAFE_FAILURE,
                status="broker_retry_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="retry_account_recovery_mailbox",
                failure_stage="inderscience_mailbox_checkpoint",
                error_type=type(exc).__name__,
                recovery_phase=reminder_kind,
            )
        if checkpoint is None:
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="account_recovery_mailbox_unavailable",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="configure_recovery_mailbox_then_resume",
                failure_stage="inderscience_mailbox_selection",
                recovery_phase=reminder_kind,
            )
        page_id = str(snapshot.get("page_id") or "")
        recovery_url = str(adapter.credential_recovery_url or "").strip()
        if not recovery_url:
            return _safe_result(
                ok=False,
                code=BROKER_SITE_UNSUPPORTED,
                status="site_adapter_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="configure_site_adapter_then_resume",
                recovery_phase=reminder_kind,
            )
        prior_state = adapter.classify_page(snapshot)
        self.controller.navigate(
            allowed_origins,
            recovery_url,
            page_id=page_id,
        )
        form = self._wait_transition(
            adapter,
            allowed_origins,
            page_id=page_id,
            previous_state=prior_state,
        )
        if reminder_kind == "username":
            fields = (
                ("txtEmail", identity),
                ("txtlastName", last_name),
            )
        else:
            fields = (
                ("txtUserName", username),
                ("txtEmailAdd", identity),
            )
        submit = self._inderscience_recovery_control(
            form,
            reminder_kind=reminder_kind,
        )
        resolved_fields: list[tuple[dict[str, Any], str]] = []
        for control_id, value in fields:
            control = self._inderscience_recovery_control(
                form,
                control_id=control_id,
            )
            if control is None or not value:
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_form_incomplete",
                    account_id=account_id,
                    system_id=system_id,
                    origin=form.get("origin"),
                    next_action="retry_existing_account_recovery",
                    failure_stage=f"inderscience_{reminder_kind}_controls",
                    recovery_phase=reminder_kind,
                )
            resolved_fields.append((control, value))
        if submit is None:
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="account_recovery_form_incomplete",
                account_id=account_id,
                system_id=system_id,
                origin=form.get("origin"),
                next_action="retry_existing_account_recovery",
                failure_stage=f"inderscience_{reminder_kind}_submit",
                recovery_phase=reminder_kind,
            )
        for control, value in resolved_fields:
            self.controller.fill(
                allowed_origins,
                str(control["ref"]),
                value,
                page_id=page_id,
            )
        requested = datetime.now(timezone.utc)
        self.controller.click(
            allowed_origins,
            str(submit["ref"]),
            page_id=page_id,
        )
        response = self._wait_transition(
            adapter,
            allowed_origins,
            page_id=page_id,
            previous_state=adapter.classify_page(form),
        )
        return {
            "checkpoint": checkpoint,
            "requested_at": requested,
            "snapshot": response,
        }

    def _register_inderscience_after_recovery_failure(
        self,
        *,
        paper: Mapping[str, Any],
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        system_id: str,
    ) -> dict[str, Any]:
        if (
            str(paper.get("account_missing_policy") or "").casefold()
            != "auto_register"
        ):
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="account_recovery_email_pending",
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="retry_account_recovery_email_check",
                recovery_phase="username",
            )
        registration_url = str(adapter.registration_url or "").strip()
        if not registration_url:
            return _safe_result(
                ok=False,
                code=BROKER_SITE_UNSUPPORTED,
                status="site_adapter_required",
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="configure_site_registration_adapter_then_resume",
            )
        page_id = str(snapshot.get("page_id") or "")
        previous_state = adapter.classify_page(snapshot)
        self.controller.navigate(
            allowed_origins,
            registration_url,
            page_id=page_id,
        )
        registration = self._wait_transition(
            adapter,
            allowed_origins,
            page_id=page_id,
            previous_state=previous_state,
        )
        if adapter.classify_page(registration) != "registration":
            status, action = self._waiting_from_snapshot(registration)
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status=status,
                system_id=system_id,
                origin=registration.get("origin"),
                next_action=action,
                failure_stage="inderscience_registration_open",
            )
        return self._register_account(
            paper=paper,
            adapter=adapter,
            allowed_origins=allowed_origins,
            snapshot=registration,
            system_id=system_id,
            target_journal=str(paper.get("target_journal") or ""),
            target_origin=normalize_origin(adapter.dashboard_url),
        )

    def _complete_inderscience_account_recovery(
        self,
        *,
        paper: Mapping[str, Any],
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        account_id: str,
        account: Mapping[str, Any],
        system_id: str,
    ) -> dict[str, Any]:
        """Recover an Inderscience account entirely inside the local broker."""

        identity = str(account.get("email") or "").strip()
        profile = self._author_profile(paper)
        if not identity:
            return _safe_result(
                ok=False,
                code=BROKER_REGISTRATION_IDENTITY_REQUIRED,
                status="registration_identity_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="complete_author_name_and_email_then_resume",
            )

        username = ""
        username_requested = self._recent_recovery_request(
            paper,
            phase="username",
        )
        if username_requested is not None:
            username = find_recent_inderscience_credential_reminder(
                identity,
                reminder_kind="username",
                requested_after=username_requested,
                store=self.store,
            )
            if not username:
                if self._recovery_request_expired(username_requested):
                    return self._register_inderscience_after_recovery_failure(
                        paper=paper,
                        adapter=adapter,
                        allowed_origins=allowed_origins,
                        snapshot=snapshot,
                        system_id=system_id,
                    )
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_email_pending",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_account_recovery_email_check",
                    recovery_requested_at=_utc_iso(username_requested),
                    recovery_phase="username",
                )
        else:
            requested = self._request_inderscience_reminder(
                adapter=adapter,
                allowed_origins=allowed_origins,
                snapshot=snapshot,
                account_id=account_id,
                account=account,
                profile=profile,
                system_id=system_id,
                reminder_kind="username",
            )
            if "checkpoint" not in requested:
                return requested
            snapshot = dict(requested["snapshot"])
            username = wait_for_inderscience_credential_reminder(
                requested["checkpoint"],
                reminder_kind="username",
                store=self.store,
                timeout_seconds=45.0,
            )
            if not username:
                if self._recovery_request_expired(requested["requested_at"]):
                    return self._register_inderscience_after_recovery_failure(
                        paper=paper,
                        adapter=adapter,
                        allowed_origins=allowed_origins,
                        snapshot=snapshot,
                        system_id=system_id,
                    )
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_email_pending",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_account_recovery_email_check",
                    recovery_requested_at=_utc_iso(requested["requested_at"]),
                    recovery_phase="username",
                )

        updater = getattr(
            self.store,
            "update_smart_account_recovered_username",
            None,
        )
        if not callable(updater):
            username = ""
            return _safe_result(
                ok=False,
                code=BROKER_SAFE_FAILURE,
                status="broker_retry_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="retry_existing_account_recovery",
                failure_stage="inderscience_username_store_commit",
            )
        account = updater(account_id, username=username)
        username = ""

        password = ""
        password_requested = self._recent_recovery_request(
            paper,
            phase="password",
        )
        if password_requested is not None:
            password = find_recent_inderscience_credential_reminder(
                identity,
                reminder_kind="password",
                requested_after=password_requested,
                store=self.store,
            )
            if not password:
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_email_pending",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_account_recovery_email_check",
                    recovery_requested_at=_utc_iso(password_requested),
                    recovery_phase="password",
                )
        else:
            requested = self._request_inderscience_reminder(
                adapter=adapter,
                allowed_origins=allowed_origins,
                snapshot=snapshot,
                account_id=account_id,
                account=account,
                profile=profile,
                system_id=system_id,
                reminder_kind="password",
            )
            if "checkpoint" not in requested:
                return requested
            snapshot = dict(requested["snapshot"])
            password = wait_for_inderscience_credential_reminder(
                requested["checkpoint"],
                reminder_kind="password",
                store=self.store,
                timeout_seconds=45.0,
            )
            if not password:
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_email_pending",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_account_recovery_email_check",
                    recovery_requested_at=_utc_iso(requested["requested_at"]),
                    recovery_phase="password",
                )

        secret_updater = getattr(
            self.store,
            "update_smart_account_recovered_secret",
            None,
        )
        if not callable(secret_updater):
            password = ""
            return _safe_result(
                ok=False,
                code=BROKER_SAFE_FAILURE,
                status="broker_retry_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="retry_existing_account_recovery",
                failure_stage="inderscience_password_store_commit",
            )
        account = secret_updater(account_id, password=password)
        password = ""

        page_id = str(snapshot.get("page_id") or "")
        self.controller.navigate(
            allowed_origins,
            adapter.login_start_url,
            page_id=page_id,
        )
        login = self._wait_transition(
            adapter,
            allowed_origins,
            page_id=page_id,
            previous_state=adapter.classify_page(snapshot),
        )
        identity_control = adapter.first_control(login, types=("text", "email"))
        password_control = adapter.first_control(login, types=("password",))
        submit = adapter.submit_control(login)
        if identity_control is None or password_control is None or submit is None:
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="account_recovery_form_incomplete",
                account_id=account_id,
                system_id=system_id,
                origin=login.get("origin"),
                next_action="retry_existing_account_recovery",
                failure_stage="inderscience_recovered_login_controls",
            )
        self.controller.fill(
            allowed_origins,
            str(identity_control["ref"]),
            str(account.get("username") or account.get("email") or ""),
            page_id=page_id,
        )
        recovered_secret = self.store.reveal_password(f"smart:{account_id}")
        self.controller.fill_trusted_secret(
            allowed_origins,
            str(password_control["ref"]),
            recovered_secret,
            page_id=page_id,
        )
        recovered_secret = ""
        self.controller.click(
            allowed_origins,
            str(submit["ref"]),
            page_id=page_id,
        )
        changed = self._wait_transition(
            adapter,
            allowed_origins,
            page_id=page_id,
            previous_state="password",
        )
        changed_state = adapter.classify_page(changed)
        if changed_state == "authenticated":
            return {
                "account_id": account_id,
                "snapshot": changed,
                "registration_created": False,
                "account_recovered": True,
            }
        if changed_state in {"human_verification", "terms_confirmation"}:
            status, action = self._waiting_from_snapshot(changed)
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status=status,
                account_id=account_id,
                system_id=system_id,
                origin=changed.get("origin"),
                next_action=action,
                failure_stage="inderscience_recovered_login_verification",
            )
        return _safe_result(
            ok=False,
            code=BROKER_SAFE_FAILURE,
            status="broker_retry_required",
            account_id=account_id,
            system_id=system_id,
            origin=changed.get("origin"),
            next_action="retry_existing_account_recovery",
            failure_stage="inderscience_recovered_login",
        )

    def _complete_existing_account_recovery(
        self,
        *,
        paper: Mapping[str, Any],
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        account_id: str,
        account: Mapping[str, Any],
        system_id: str,
        target_url: str,
    ) -> dict[str, Any]:
        """Reset one proven-existing ScholarOne account without exposing secrets."""

        reset_url, journal_path = self._scholar_one_reset_url(target_url)
        identity = str(account.get("email") or account.get("username") or "").strip()
        if system_id != "scholar_one" or not reset_url or not identity:
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="existing_account_recovery_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="add_existing_account_password_then_resume",
            )
        try:
            checkpoint = capture_password_recovery_checkpoint(
                identity,
                store=self.store,
            )
        except Exception as exc:
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="account_recovery_mailbox_unavailable",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="repair_recovery_mailbox_then_resume",
                failure_stage="mailbox_checkpoint",
                error_type=type(exc).__name__,
            )
        if checkpoint is None:
            return _safe_result(
                ok=False,
                code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                status="account_recovery_mailbox_unavailable",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="configure_recovery_mailbox_then_resume",
                failure_stage="mailbox_selection",
            )

        page_id = str(snapshot.get("page_id") or "")
        prior_state = adapter.classify_page(snapshot)
        if prior_state == "password_reset_completed":
            if str(account.get("status") or "").casefold() == "registration_pending":
                recovered_secret = self.store.reveal_password(f"smart:{account_id}")
                updater = getattr(
                    self.store,
                    "update_smart_account_recovered_secret",
                    None,
                )
                if not callable(updater):
                    recovered_secret = ""
                    return _safe_result(
                        ok=False,
                        code=BROKER_SAFE_FAILURE,
                        status="broker_retry_required",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="retry_existing_account_recovery",
                        failure_stage="recovery_store_commit",
                    )
                account = updater(account_id, password=recovered_secret)
                recovered_secret = ""
            continue_profile = self._profile_continue_control(snapshot)
            if continue_profile is not None:
                self.controller.click(
                    allowed_origins,
                    str(continue_profile["ref"]),
                    page_id=page_id,
                )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state="password_reset_completed",
                )
            return {
                "account_id": account_id,
                "snapshot": snapshot,
                "registration_created": False,
                "account_recovered": True,
            }
        requested_at_text = str(
            paper.get("credential_recovery_requested_at") or ""
        ).strip()
        requested_at: datetime | None = None
        try:
            if requested_at_text:
                requested_at = datetime.fromisoformat(
                    requested_at_text.replace("Z", "+00:00")
                )
                if requested_at.tzinfo is None:
                    requested_at = requested_at.replace(tzinfo=timezone.utc)
                requested_at = requested_at.astimezone(timezone.utc)
        except ValueError:
            requested_at = None
        reuse_existing_request = bool(
            requested_at
            and datetime.now(timezone.utc) - timedelta(minutes=20)
            <= requested_at
            <= datetime.now(timezone.utc) + timedelta(minutes=1)
        )
        try:
            recovery_url = ""
            use_current_reset_page = prior_state == "password_reset_change"
            if use_current_reset_page:
                # The exact-origin reset link is already open and classified.
                # Reuse that visible page instead of issuing or consuming
                # another e-mail reset request.
                pass
            elif reuse_existing_request and requested_at is not None:
                recovery_url = find_recent_password_recovery_link(
                    identity,
                    allowed_origins=allowed_origins,
                    journal_path=journal_path,
                    requested_after=requested_at,
                    store=self.store,
                    max_messages=12,
                )
                if not recovery_url:
                    recovery_url = wait_for_password_recovery_link(
                        checkpoint,
                        allowed_origins=allowed_origins,
                        journal_path=journal_path,
                        store=self.store,
                        timeout_seconds=45.0,
                    )
            else:
                self.controller.navigate(
                    allowed_origins,
                    reset_url,
                    page_id=page_id,
                )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state=prior_state,
                )
                if adapter.classify_page(snapshot) != "password_reset_request":
                    return _safe_result(
                        ok=False,
                        code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                        status="account_recovery_form_incomplete",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="retry_existing_account_recovery",
                        failure_stage="reset_request_page",
                    )
                email_control = adapter.first_control(snapshot, types=("email",))
                submit = adapter.submit_control(snapshot)
                if email_control is None or submit is None:
                    return _safe_result(
                        ok=False,
                        code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                        status="account_recovery_form_incomplete",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="retry_existing_account_recovery",
                        failure_stage="reset_request_controls",
                    )
                self.controller.fill(
                    allowed_origins,
                    str(email_control["ref"]),
                    identity,
                    page_id=page_id,
                )
                requested_at = datetime.now(timezone.utc)
                requested_at_text = _utc_iso(requested_at)
                self.controller.click(
                    allowed_origins,
                    str(submit["ref"]),
                    page_id=page_id,
                )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state="password_reset_request",
                )
                if adapter.classify_page(snapshot) != "password_reset_sent":
                    return _safe_result(
                        ok=False,
                        code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                        status="account_recovery_request_failed",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="retry_existing_account_recovery",
                        failure_stage="reset_request_submit",
                        recovery_requested_at=requested_at_text,
                    )
                recovery_url = wait_for_password_recovery_link(
                    checkpoint,
                    allowed_origins=allowed_origins,
                    journal_path=journal_path,
                    store=self.store,
                    timeout_seconds=45.0,
                )
            if not use_current_reset_page and not recovery_url:
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_email_pending",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_existing_account_recovery",
                    failure_stage="reset_email_wait",
                    recovery_requested_at=requested_at_text,
                )
            if not use_current_reset_page:
                self.controller.navigate(
                    allowed_origins,
                    recovery_url,
                    page_id=page_id,
                )
                recovery_url = ""
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state="password_reset_sent",
                )
            page_state = adapter.classify_page(snapshot)
            if page_state in {"human_verification", "terms_confirmation"}:
                status, action = self._waiting_from_snapshot(snapshot)
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status=status,
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action=action,
                    failure_stage="reset_verification",
                )
            if page_state != "password_reset_change":
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_link_incomplete",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_existing_account_recovery",
                    failure_stage="reset_change_page",
                )

            if str(account.get("status") or "").casefold() == "registration_pending":
                new_secret = self.store.reveal_password(f"smart:{account_id}")
            else:
                new_secret = secrets.token_urlsafe(30)
            password_controls = [
                control
                for control in adapter.visible_controls(snapshot)
                if str(control.get("type") or "").casefold() == "password"
                and "current" not in str(control.get("label") or "").casefold()
            ]
            submit = adapter.submit_control(snapshot)
            if not password_controls or submit is None:
                new_secret = ""
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_form_incomplete",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="retry_existing_account_recovery",
                    failure_stage="reset_change_controls",
                )
            for control in password_controls:
                self.controller.fill_trusted_secret(
                    allowed_origins,
                    str(control["ref"]),
                    new_secret,
                    page_id=page_id,
                )
            self.controller.click(
                allowed_origins,
                str(submit["ref"]),
                page_id=page_id,
            )
            changed = self._wait_transition(
                adapter,
                allowed_origins,
                page_id=page_id,
                previous_state="password_reset_change",
            )
            changed_state = adapter.classify_page(changed)
            changed_text = str(changed.get("text") or "").casefold()
            accepted = changed_state in {
                "authenticated",
                "email",
                "password",
                "password_reset_completed",
            } or any(
                marker in changed_text
                for marker in (
                    "password has been reset",
                    "password was reset",
                    "password changed successfully",
                    "password has been changed",
                    "password has been updated",
                    "\u60a8\u7684\u5bc6\u7801\u5df2\u66f4\u65b0",
                    "\u5bc6\u7801\u5df2\u66f4\u65b0",
                )
            )
            if not accepted:
                new_secret = ""
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="account_recovery_change_failed",
                    account_id=account_id,
                    system_id=system_id,
                    origin=changed.get("origin"),
                    next_action="retry_existing_account_recovery",
                    failure_stage="reset_change_submit",
                )
            updater = getattr(
                self.store,
                "update_smart_account_recovered_secret",
                None,
            )
            if not callable(updater):
                new_secret = ""
                raise RuntimeError("credential store recovery mutation unavailable")
            account = updater(account_id, password=new_secret)
            new_secret = ""

            if changed_state == "password_reset_completed":
                continue_profile = self._profile_continue_control(changed)
                if continue_profile is not None:
                    self.controller.click(
                        allowed_origins,
                        str(continue_profile["ref"]),
                        page_id=page_id,
                    )
                    changed = self._wait_transition(
                        adapter,
                        allowed_origins,
                        page_id=page_id,
                        previous_state="password_reset_completed",
                    )
                    changed_state = adapter.classify_page(changed)

            if changed_state not in {"authenticated", "profile_update"}:
                self.controller.navigate(
                    allowed_origins,
                    target_url,
                    page_id=page_id,
                )
                changed = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state=changed_state,
                )
            return {
                "account_id": account_id,
                "snapshot": changed,
                "registration_created": False,
                "account_recovered": True,
            }
        except Exception as exc:
            return _safe_result(
                ok=False,
                code=BROKER_SAFE_FAILURE,
                status="broker_retry_required",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="retry_existing_account_recovery",
                failure_stage="account_recovery",
                error_type=type(exc).__name__,
            )

    def _complete_registration(
        self,
        *,
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        account_id: str,
        account: Mapping[str, Any],
        profile: Mapping[str, str],
        system_id: str,
        remaining_steps: int = 5,
    ) -> dict[str, Any]:
        page_id = str(snapshot.get("page_id") or "")
        if remaining_steps <= 0:
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status="registration_form_incomplete",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="complete_visible_registration_then_resume",
                registration_created=True,
            )
        secret = self.store.reveal_password(f"smart:{account_id}")
        unchecked_terms = False
        for control in adapter.visible_controls(snapshot):
            ref = str(control.get("ref") or "")
            control_type = str(control.get("type") or "").casefold()
            if not ref:
                continue
            marker_hint = " ".join(
                str(control.get(key) or "")
                for key in ("id", "name", "label")
            ).casefold()
            if control_type == "password" or (
                bool(control.get("sensitive")) and "password" in marker_hint
            ):
                self.controller.fill_trusted_secret(
                    allowed_origins,
                    ref,
                    secret,
                    page_id=page_id,
                    accepted_text_field_ids=(
                        ("txtPassword",)
                        if system_id == "inderscience"
                        else ()
                    ),
                )
                continue
            if control_type == "checkbox":
                if not bool(control.get("checked")):
                    unchecked_terms = True
                continue
            if control_type == "select":
                inspected = self.controller.inspect_control(
                    allowed_origins,
                    ref,
                    page_id=page_id,
                )
                marker = str(
                    inspected.get("marker") or control.get("label") or ""
                )
                desired = self._registration_field_value(
                    marker,
                    profile=profile,
                    account=account,
                )
                if desired:
                    self.controller.select(
                        allowed_origins,
                        ref,
                        self._matching_select_option(control, desired),
                        page_id=page_id,
                    )
                continue
            if control_type not in {"text", "email", "tel", "textarea"}:
                continue
            inspected = self.controller.inspect_control(
                allowed_origins,
                ref,
                page_id=page_id,
            )
            marker = str(inspected.get("marker") or control.get("label") or "")
            value = self._registration_field_value(
                marker,
                profile=profile,
                account=account,
            )
            normalized_marker = re.sub(
                r"[^a-z0-9]+",
                "",
                marker.casefold(),
            )
            clear_optional_email = any(
                token in normalized_marker
                for token in (
                    "alternateemail",
                    "secondaryemail",
                    "backupemail",
                    "ccemail",
                )
            )
            if value or clear_optional_email:
                self.controller.fill(
                    allowed_origins,
                    ref,
                    value,
                    page_id=page_id,
                )
        secret = ""
        if system_id == "inderscience":
            preflight_check = getattr(
                self.controller,
                "registration_preflight",
                None,
            )
            if callable(preflight_check):
                preflight = preflight_check(
                    allowed_origins,
                    page_id=page_id,
                )
                if bool(preflight.get("email_duplicate")):
                    return {
                        "existing_account_snapshot": snapshot,
                        "registration_created": False,
                    }
                if bool(preflight.get("username_duplicate")):
                    return _safe_result(
                        ok=False,
                        code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                        status="registration_username_conflict",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="regenerate_registration_username_then_resume",
                        registration_created=False,
                    )
                if bool(preflight.get("captcha_present")) and not bool(
                    preflight.get("captcha_completed")
                ):
                    return _safe_result(
                        ok=False,
                        code=BROKER_WAITING_HUMAN,
                        status="human_verification",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="complete_captcha_then_resume",
                        registration_created=False,
                    )
        if unchecked_terms and self._settings().get(
            "account_registration_requires_terms_confirmation", True
        ):
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status="waiting_terms_confirmation",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="accept_site_terms_then_resume",
                registration_created=True,
            )
        submit = adapter.registration_next_control(snapshot)
        if submit is None:
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status="registration_form_incomplete",
                account_id=account_id,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="complete_visible_registration_then_resume",
                registration_created=True,
            )
        self.controller.click(
            allowed_origins,
            str(submit["ref"]),
            page_id=page_id,
        )
        next_snapshot = self._wait_transition(
            adapter,
            allowed_origins,
            page_id=page_id,
            previous_state="registration",
            previous_marker=str(
                snapshot.get("registration_step") or snapshot.get("path") or ""
            ).strip(),
        )
        if self._existing_account_notice(next_snapshot):
            return {
                "existing_account_snapshot": next_snapshot,
                "registration_created": False,
            }
        next_state = adapter.classify_page(next_snapshot)
        if next_state == "authenticated":
            return {"registration_snapshot": next_snapshot, "registration_created": True}
        if adapter.registration_succeeded(next_snapshot):
            if next_state == "password":
                return {
                    "registration_snapshot": next_snapshot,
                    "registration_created": True,
                }
            self.controller.navigate(
                allowed_origins,
                adapter.login_start_url,
                page_id=page_id,
            )
            login_snapshot = self._wait_transition(
                adapter,
                allowed_origins,
                page_id=page_id,
                previous_state=next_state,
            )
            return {
                "registration_snapshot": login_snapshot,
                "registration_created": True,
            }
        if next_state == "registration":
            previous_marker = str(
                snapshot.get("registration_step") or snapshot.get("path") or ""
            ).strip().casefold()
            next_marker = str(
                next_snapshot.get("registration_step")
                or next_snapshot.get("path")
                or ""
            ).strip().casefold()
            if previous_marker and next_marker == previous_marker:
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status="registration_form_incomplete",
                    account_id=account_id,
                    system_id=system_id,
                    origin=next_snapshot.get("origin"),
                    next_action="complete_visible_registration_then_resume",
                    registration_created=True,
                )
            return self._complete_registration(
                adapter=adapter,
                allowed_origins=allowed_origins,
                snapshot=next_snapshot,
                account_id=account_id,
                account=account,
                profile=profile,
                system_id=system_id,
                remaining_steps=remaining_steps - 1,
            )
        status, action = self._waiting_from_snapshot(next_snapshot)
        return _safe_result(
            ok=False,
            code=BROKER_WAITING_HUMAN,
            status=status,
            account_id=account_id,
            system_id=system_id,
            origin=next_snapshot.get("origin"),
            next_action=action,
            registration_created=True,
        )

    def _register_account(
        self,
        *,
        paper: Mapping[str, Any],
        adapter: SubmissionSiteAdapter,
        allowed_origins: tuple[str, ...],
        snapshot: dict[str, Any],
        system_id: str,
        target_journal: str,
        target_origin: str = "",
    ) -> dict[str, Any]:
        profile = self._author_profile(paper)
        full_name = profile.get("full_name", "")
        email = profile.get("email", "")
        if not full_name or not email:
            return _safe_result(
                ok=False,
                code=BROKER_REGISTRATION_IDENTITY_REQUIRED,
                status="registration_identity_required",
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action="complete_author_name_and_email_then_resume",
            )
        resolved_target_origin = normalize_origin(
            target_origin or adapter.dashboard_url
        )
        preferred_login = self._authenticate_with_orcid(
            paper=paper,
            adapter=adapter,
            allowed_origins=allowed_origins,
            snapshot=snapshot,
            target_origin=resolved_target_origin,
            system_id=system_id,
            target_journal=target_journal,
        )
        if preferred_login is not None:
            return preferred_login
        page_id = str(snapshot.get("page_id") or "")
        state = adapter.classify_page(snapshot)
        if state == "authenticated":
            digest = hashlib.sha256(
                f"{system_id}\x1f{email.casefold()}".encode("utf-8")
            ).hexdigest()[:16]
            account_id = f"session-{system_id}-{digest}"
            try:
                self.store.get_account(f"smart:{account_id}")
            except RecordNotFoundError:
                self.store.create_smart_session_account(
                    {
                        "account_id": account_id,
                        "scope": "journal_system",
                        "provider": adapter.label,
                        "username": email,
                        "email": email,
                        "login_url": adapter.dashboard_url,
                        "system_id": system_id,
                        "related_journals": target_journal,
                        "status": "ready_live_session",
                        "notes": (
                            "Bound to an already authenticated browser session; "
                            "no remote password was invented or stored."
                        ),
                    }
                )
            return {
                "account_id": account_id,
                "snapshot": snapshot,
                "registration_created": False,
                "session_discovered": True,
            }
        if state == "email":
            email_control = adapter.first_control(snapshot, types=("email",))
            submit = adapter.submit_control(snapshot)
            if email_control is None or submit is None:
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status="registration_form_incomplete",
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="complete_visible_registration_then_resume",
                )
            self.controller.fill(
                allowed_origins,
                str(email_control["ref"]),
                email,
                page_id=page_id,
            )
            self.controller.click(
                allowed_origins,
                str(submit["ref"]),
                page_id=page_id,
            )
            snapshot = self._wait_transition(
                adapter,
                allowed_origins,
                page_id=page_id,
                previous_state="email",
            )
            state = adapter.classify_page(snapshot)
        if state == "password":
            register = adapter.registration_control(snapshot)
            if register is not None:
                self.controller.click(
                    allowed_origins,
                    str(register["ref"]),
                    page_id=page_id,
                )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state="password",
                )
                state = adapter.classify_page(snapshot)
            if state == "password":
                return _safe_result(
                    ok=False,
                    code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                    status="existing_account_recovery_required",
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="add_existing_account_password_then_resume",
                )
        if state != "registration":
            status, action = self._waiting_from_snapshot(snapshot)
            return _safe_result(
                ok=False,
                code=BROKER_WAITING_HUMAN,
                status=status,
                system_id=system_id,
                origin=snapshot.get("origin"),
                next_action=action,
            )

        preferred_registration = self._authenticate_with_orcid(
            paper=paper,
            adapter=adapter,
            allowed_origins=allowed_origins,
            snapshot=snapshot,
            target_origin=resolved_target_origin,
            system_id=system_id,
            target_journal=target_journal,
        )
        if preferred_registration is not None:
            return preferred_registration

        if system_id == "inderscience" and self._existing_account_notice(snapshot):
            return self._inderscience_existing_account_result(
                paper=paper,
                snapshot=snapshot,
                account_id=str(paper.get("credential_account_id") or ""),
                system_id=system_id,
            )

        digest = hashlib.sha256(
            f"{system_id}\x1f{email.casefold()}".encode("utf-8")
        ).hexdigest()[:16]
        account_id = f"auto-{system_id}-{digest}"
        registration_username = self._registration_username(
            system_id,
            email,
            profile,
        )
        registration_country = str(profile.get("country") or "").strip()
        registration_title = str(profile.get("prefix") or "").strip()
        if system_id == "inderscience":
            if registration_country.casefold() in {
                "china",
                "cn",
                "chn",
                "people's republic of china",
            }:
                registration_country = "44"
            if not registration_title:
                registration_title = "2"
        registration_biography = self._registration_field_value(
            "biography",
            profile=profile,
            account={},
        )
        try:
            account = self.store.get_account(f"smart:{account_id}")
        except RecordNotFoundError:
            generated_secret = self._registration_secret(system_id)
            account = self.store.create_smart_account_with_secret(
                {
                    "account_id": account_id,
                    "scope": "journal_system",
                    "provider": adapter.label,
                    "username": registration_username,
                    "email": email,
                    "login_url": adapter.login_start_url,
                    "system_id": system_id,
                    "related_journals": target_journal,
                    "status": "registration_pending",
                    "registration_title": registration_title,
                    "registration_country": registration_country,
                    "registration_biography": registration_biography,
                    "notes": "Created by the trusted local credential broker.",
                },
                password=generated_secret,
            )
            generated_secret = ""
        if self._existing_account_notice(snapshot):
            if system_id == "inderscience":
                return self._inderscience_existing_account_result(
                    paper=paper,
                    snapshot=snapshot,
                    account_id=account_id,
                    system_id=system_id,
                )
            system_site, journal_site = self._site_records(
                system_id,
                target_journal,
            )
            return self._complete_existing_account_recovery(
                paper=paper,
                adapter=adapter,
                allowed_origins=allowed_origins,
                snapshot=snapshot,
                account_id=account_id,
                account=account,
                system_id=system_id,
                target_url=adapter.target_url(system_site, journal_site),
            )
        completed = self._complete_registration(
            adapter=adapter,
            allowed_origins=allowed_origins,
            snapshot=snapshot,
            account_id=account_id,
            account=account,
            profile=profile,
            system_id=system_id,
        )
        if "existing_account_snapshot" in completed:
            if system_id == "inderscience":
                return self._inderscience_existing_account_result(
                    paper=paper,
                    snapshot=dict(completed["existing_account_snapshot"]),
                    account_id=account_id,
                    system_id=system_id,
                )
            system_site, journal_site = self._site_records(
                system_id,
                target_journal,
            )
            return self._complete_existing_account_recovery(
                paper=paper,
                adapter=adapter,
                allowed_origins=allowed_origins,
                snapshot=dict(completed["existing_account_snapshot"]),
                account_id=account_id,
                account=account,
                system_id=system_id,
                target_url=adapter.target_url(system_site, journal_site),
            )
        if "registration_snapshot" in completed:
            return {
                "account_id": account_id,
                "snapshot": completed["registration_snapshot"],
                "registration_created": True,
            }
        return completed

    def ensure_session(
        self,
        *,
        paper_id: str,
        paper: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return a value-free session result for one exact paper binding."""

        system_id = str(
            candidate.get("submission_system_id")
            or paper.get("submission_system_id")
            or ""
        ).strip().casefold()
        target_journal = str(paper.get("target_journal") or "").strip()
        adapter = submission_site_adapter(system_id)
        if adapter is None:
            return _safe_result(
                ok=False,
                code=BROKER_SITE_UNSUPPORTED,
                status="site_adapter_required",
                system_id=system_id,
                next_action="configure_site_adapter_then_resume",
            )
        migrate = getattr(self.store, "migrate_smart_plaintext_credentials", None)
        if callable(migrate):
            migration = migrate()
            if isinstance(migration, Mapping) and migration.get("plaintext_remaining"):
                return _safe_result(
                    ok=False,
                    code=BROKER_SAFE_FAILURE,
                    status="broker_retry_required",
                    system_id=system_id,
                    next_action="repair_credential_migration_then_resume",
                )
        target_origin = normalize_origin(
            candidate.get("target_origin") or adapter.dashboard_url
        )
        allowed_values = [
            *(candidate.get("allowed_origins") or []),
            *adapter.allowed_origins,
        ]
        allowed_origins = tuple(
            dict.fromkeys(
                origin
                for origin in map(normalize_origin, allowed_values)
                if origin
            )
        )
        if not target_origin or target_origin not in allowed_origins:
            return _safe_result(
                ok=False,
                code=BROKER_SITE_UNSUPPORTED,
                status="site_binding_invalid",
                system_id=system_id,
                next_action="repair_site_binding_then_resume",
            )
        account_id = str(
            candidate.get("credential_account_id")
            or paper.get("credential_account_id")
            or ""
        ).strip()
        target_url = adapter.login_start_url
        try:
            system_site, journal_site = self._site_records(
                system_id,
                target_journal,
            )
            target_url = adapter.target_url(system_site, journal_site)
            # Federated portals such as Wiley authenticate on one origin but
            # host the bound manuscript workflow on another allowlisted
            # origin.  When the program has already selected that exact
            # manuscript origin, prefer its stored draft/review URL instead
            # of reopening the generic account dashboard.
            if normalize_origin(target_url) != target_origin:
                for key in (
                    "draft_url",
                    "submission_draft_url",
                    "upload_url",
                    "submission_upload_url",
                ):
                    bound_url = str(paper.get(key) or "").strip()
                    if (
                        bound_url
                        and normalize_origin(bound_url) == target_origin
                        and adapter.allows_url(bound_url)
                    ):
                        target_url = bound_url
                        break
            snapshot = self._open_or_select_page(
                adapter,
                allowed_origins,
                target_origin,
                start_url=target_url,
            )
            registration_created = False
            orcid_authenticated = False
            account: dict[str, Any] = {}
            if account_id:
                account = self.store.get_account(f"smart:{account_id}")
            else:
                if (
                    str(
                        paper.get("account_missing_policy")
                        or self._settings().get("account_missing_policy")
                        or "auto_register"
                    ).casefold()
                    != "auto_register"
                ):
                    return _safe_result(
                        ok=False,
                        code=BROKER_REGISTRATION_IDENTITY_REQUIRED,
                        status="account_required",
                        system_id=system_id,
                        next_action="add_account_then_resume",
                    )
                registration = self._register_account(
                    paper=paper,
                    adapter=adapter,
                    allowed_origins=allowed_origins,
                    snapshot=snapshot,
                    system_id=system_id,
                    target_journal=target_journal,
                    target_origin=target_origin,
                )
                if "snapshot" not in registration:
                    return registration
                account_id = str(registration["account_id"])
                account = self.store.get_account(f"smart:{account_id}")
                snapshot = dict(registration["snapshot"])
                registration_created = bool(
                    registration.get("registration_created", True)
                )
                orcid_authenticated = bool(
                    registration.get("orcid_authenticated")
                )

            page_id = str(snapshot.get("page_id") or "")
            page_state = adapter.classify_page(snapshot)
            if page_state == "submission_portal_closed":
                return _safe_result(
                    ok=False,
                    code=BROKER_SITE_UNSUPPORTED,
                    status="site_binding_invalid",
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action="repair_site_binding_then_resume",
                    failure_stage="submission_portal_closed",
                )
            if (
                str(account.get("auth_method") or "").casefold() == "orcid"
                and page_state != "authenticated"
                and not orcid_authenticated
            ):
                preferred_login = self._authenticate_with_orcid(
                    paper=paper,
                    adapter=adapter,
                    allowed_origins=allowed_origins,
                    snapshot=snapshot,
                    target_origin=target_origin,
                    system_id=system_id,
                    target_journal=target_journal,
                    account=account,
                )
                if preferred_login is None:
                    return _safe_result(
                        ok=False,
                        code=BROKER_WAITING_HUMAN,
                        status="orcid_login_control_missing",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="open_orcid_login_then_resume",
                        registration_created=registration_created,
                        failure_stage="orcid_entry",
                    )
                if "snapshot" not in preferred_login:
                    return preferred_login
                account_id = str(preferred_login.get("account_id") or account_id)
                snapshot = dict(preferred_login["snapshot"])
                registration_created = bool(
                    preferred_login.get(
                        "registration_created",
                        registration_created,
                    )
                )
                orcid_authenticated = bool(
                    preferred_login.get("orcid_authenticated")
                )
                account = self.store.get_account(f"smart:{account_id}")
                page_id = str(snapshot.get("page_id") or page_id)
                page_state = adapter.classify_page(snapshot)
            if system_id == "scholar_one" and (
                self._existing_account_notice(snapshot)
                or (
                    str(account.get("status") or "").casefold()
                    == "registration_pending"
                    and page_state
                    in {
                        "password",
                        "password_reset_request",
                        "password_reset_sent",
                        "password_reset_change",
                        "password_reset_completed",
                    }
                )
            ):
                recovery = self._complete_existing_account_recovery(
                    paper=paper,
                    adapter=adapter,
                    allowed_origins=allowed_origins,
                    snapshot=snapshot,
                    account_id=account_id,
                    account=account,
                    system_id=system_id,
                    target_url=target_url,
                )
                if "snapshot" not in recovery:
                    return recovery
                snapshot = dict(recovery["snapshot"])
                account = self.store.get_account(f"smart:{account_id}")
                page_id = str(snapshot.get("page_id") or page_id)
                page_state = adapter.classify_page(snapshot)
            identity = str(account.get("username") or account.get("email") or "")
            if page_state == "email":
                email_control = adapter.first_control(snapshot, types=("email",))
                submit = adapter.submit_control(snapshot)
                if email_control is None or submit is None or not identity:
                    return _safe_result(
                        ok=False,
                        code=BROKER_WAITING_HUMAN,
                        status="login_form_incomplete",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="complete_visible_login_then_resume",
                    )
                self.controller.fill(
                    allowed_origins,
                    str(email_control["ref"]),
                    identity,
                    page_id=page_id,
                )
                self.controller.click(
                    allowed_origins,
                    str(submit["ref"]),
                    page_id=page_id,
                )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state="email",
                )
                page_state = adapter.classify_page(snapshot)
            if page_state == "registration":
                if self._existing_account_notice(snapshot):
                    if system_id == "inderscience":
                        recovery = self._inderscience_existing_account_result(
                            paper=paper,
                            snapshot=snapshot,
                            account_id=account_id,
                            system_id=system_id,
                        )
                    else:
                        recovery = self._complete_existing_account_recovery(
                            paper=paper,
                            adapter=adapter,
                            allowed_origins=allowed_origins,
                            snapshot=snapshot,
                            account_id=account_id,
                            account=account,
                            system_id=system_id,
                            target_url=target_url,
                        )
                    if "snapshot" not in recovery:
                        return recovery
                    snapshot = dict(recovery["snapshot"])
                    account = self.store.get_account(f"smart:{account_id}")
                    page_state = adapter.classify_page(snapshot)
                else:
                    completed = self._complete_registration(
                        adapter=adapter,
                        allowed_origins=allowed_origins,
                        snapshot=snapshot,
                        account_id=account_id,
                        account=account,
                        profile=self._author_profile(paper),
                        system_id=system_id,
                    )
                    if "existing_account_snapshot" in completed:
                        if system_id == "inderscience":
                            recovery = self._inderscience_existing_account_result(
                                paper=paper,
                                snapshot=dict(completed["existing_account_snapshot"]),
                                account_id=account_id,
                                system_id=system_id,
                            )
                        else:
                            recovery = self._complete_existing_account_recovery(
                                paper=paper,
                                adapter=adapter,
                                allowed_origins=allowed_origins,
                                snapshot=dict(completed["existing_account_snapshot"]),
                                account_id=account_id,
                                account=account,
                                system_id=system_id,
                                target_url=target_url,
                            )
                        if "snapshot" not in recovery:
                            return recovery
                        snapshot = dict(recovery["snapshot"])
                        account = self.store.get_account(f"smart:{account_id}")
                        page_state = adapter.classify_page(snapshot)
                    else:
                        if "registration_snapshot" not in completed:
                            return completed
                        snapshot = dict(completed["registration_snapshot"])
                        registration_created = True
                        page_state = adapter.classify_page(snapshot)
            if page_state == "password":
                identity_control = adapter.first_control(
                    snapshot,
                    types=("text", "email"),
                )
                if identity_control is not None and identity:
                    self.controller.fill(
                        allowed_origins,
                        str(identity_control["ref"]),
                        identity,
                        page_id=page_id,
                    )
                password_control = adapter.first_control(
                    snapshot,
                    types=("password",),
                )
                submit = adapter.submit_control(snapshot)
                if password_control is None:
                    return _safe_result(
                        ok=False,
                        code=BROKER_WAITING_HUMAN,
                        status="login_form_incomplete",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="complete_visible_login_then_resume",
                    )
                password = self.store.reveal_password(f"smart:{account_id}")
                self.controller.fill_trusted_secret(
                    allowed_origins,
                    str(password_control["ref"]),
                    password,
                    page_id=page_id,
                )
                password = ""
                if submit is not None:
                    self.controller.click(
                        allowed_origins,
                        str(submit["ref"]),
                        page_id=page_id,
                    )
                else:
                    self.controller.submit_login_form(
                        allowed_origins,
                        str(password_control["ref"]),
                        page_id=page_id,
                    )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=page_id,
                    previous_state="password",
                )
                page_state = adapter.classify_page(snapshot)
                if page_state == "password":
                    if system_id == "scholar_one":
                        recovery = self._complete_existing_account_recovery(
                            paper=paper,
                            adapter=adapter,
                            allowed_origins=allowed_origins,
                            snapshot=snapshot,
                            account_id=account_id,
                            account=account,
                            system_id=system_id,
                            target_url=target_url,
                        )
                        if "snapshot" not in recovery:
                            return recovery
                        snapshot = dict(recovery["snapshot"])
                        account = self.store.get_account(f"smart:{account_id}")
                        page_state = adapter.classify_page(snapshot)
                    else:
                        return _safe_result(
                            ok=False,
                            code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                            status="saved_password_rejected",
                            account_id=account_id,
                            system_id=system_id,
                            origin=snapshot.get("origin"),
                            next_action="update_saved_password_then_resume",
                        )
            if page_state == "password_rejected":
                if system_id != "inderscience":
                    return _safe_result(
                        ok=False,
                        code=BROKER_ACCOUNT_RECOVERY_REQUIRED,
                        status="saved_password_rejected",
                        account_id=account_id,
                        system_id=system_id,
                        origin=snapshot.get("origin"),
                        next_action="update_saved_password_then_resume",
                    )
                recovery = self._complete_inderscience_account_recovery(
                    paper=paper,
                    adapter=adapter,
                    allowed_origins=allowed_origins,
                    snapshot=snapshot,
                    account_id=account_id,
                    account=account,
                    system_id=system_id,
                )
                if "snapshot" not in recovery:
                    return recovery
                account_id = str(recovery.get("account_id") or account_id)
                registration_created = bool(
                    recovery.get("registration_created", registration_created)
                )
                snapshot = dict(recovery["snapshot"])
                account = self.store.get_account(f"smart:{account_id}")
                page_id = str(snapshot.get("page_id") or page_id)
                page_state = adapter.classify_page(snapshot)
            if page_state == "profile_update":
                completed_profile = self._complete_scholar_one_profile_update(
                    adapter=adapter,
                    allowed_origins=allowed_origins,
                    snapshot=snapshot,
                    account_id=account_id,
                    system_id=system_id,
                )
                if "snapshot" not in completed_profile:
                    return completed_profile
                snapshot = dict(completed_profile["snapshot"])
                page_state = adapter.classify_page(snapshot)
            if page_state in {
                "human_verification",
                "terms_confirmation",
                "unknown",
            }:
                status, action = self._waiting_from_snapshot(snapshot)
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status=status,
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action=action,
                    registration_created=registration_created,
                )

            if normalize_origin(snapshot.get("origin")) != target_origin:
                self.controller.navigate(
                    allowed_origins,
                    target_url,
                    page_id=str(snapshot.get("page_id") or ""),
                )
                snapshot = self._wait_transition(
                    adapter,
                    allowed_origins,
                    page_id=str(snapshot.get("page_id") or ""),
                    previous_state=page_state,
                )
            if adapter.classify_page(snapshot) != "authenticated":
                status, action = self._waiting_from_snapshot(snapshot)
                return _safe_result(
                    ok=False,
                    code=BROKER_WAITING_HUMAN,
                    status=status,
                    account_id=account_id,
                    system_id=system_id,
                    origin=snapshot.get("origin"),
                    next_action=action,
                    registration_created=registration_created,
                )
            if registration_created:
                mark_ready = getattr(
                    self.store,
                    "mark_smart_account_registration_ready",
                    None,
                )
                if callable(mark_ready):
                    account = mark_ready(account_id)
            result = self._record_session(
                account_id=account_id,
                system_id=system_id,
                target_origin=target_origin,
                snapshot=snapshot,
            )
            result["registration_created"] = registration_created
            return result
        except (SmartBrowserCdpError, RecordNotFoundError) as exc:
            recovered = self._recover_authenticated_redirect(
                adapter=adapter,
                allowed_origins=allowed_origins,
                target_origin=target_origin,
                account_id=account_id,
                system_id=system_id,
                start_url=target_url,
            )
            if recovered is not None:
                recovered["registration_created"] = False
                return recovered
            return _safe_result(
                ok=False,
                code=BROKER_SAFE_FAILURE,
                status="broker_retry_required",
                account_id=account_id,
                system_id=system_id,
                origin=target_origin,
                next_action="retry_credential_broker",
                failure_stage="ensure_session",
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            recovered = self._recover_authenticated_redirect(
                adapter=adapter,
                allowed_origins=allowed_origins,
                target_origin=target_origin,
                account_id=account_id,
                system_id=system_id,
                start_url=target_url,
            )
            if recovered is not None:
                recovered["registration_created"] = False
                return recovered
            return _safe_result(
                ok=False,
                code=BROKER_SAFE_FAILURE,
                status="broker_retry_required",
                account_id=account_id,
                system_id=system_id,
                origin=target_origin,
                next_action="retry_credential_broker",
                failure_stage="ensure_session",
                error_type=type(exc).__name__,
            )
