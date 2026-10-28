"""Read-only mailbox discovery and evidence collection for Smart Submission.

The program owns account selection and secret handling.  Codex receives only
bounded, value-free mailbox evidence (subject/snippet/date/sender domain) and
never receives account identifiers, passwords, cookies, or credential refs.
"""

from __future__ import annotations

import email
import hashlib
import html
import os
import poplib
import re
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.policy import default as default_email_policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import config
from .account_site_store import AccountSiteStore
from .io_utils import now_iso
from .smart_submission_identity import normalize_submission_id, submission_records


GMAIL_MONITOR_ORIGIN = "https://mail.google.com:443"
XUJC_WEBMAIL_ORIGINS = (
    "http://mail.xujc.com:80",
    "https://mail.xujc.com:443",
)
MONITOR_SOURCE_STATUSES = {
    "monitoring",
    "revision_needed",
    "retarget_needed",
    "rejected",
    "accepted",
    "waiting",
    "blocked",
    "no_action",
}
MONITOR_ACTION_KINDS = {
    "none",
    "technical_correction",
    "scientific_revision",
    "files_or_metadata",
    "retarget",
    "accepted",
    "unknown",
}
_ACTIONABLE_STATUSES = {
    "revision_needed",
    "retarget_needed",
    "rejected",
    "accepted",
}
_SPACE_RE = re.compile(r"\s+")
_HTML_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MailboxSource:
    source_id: str
    label: str
    kind: str
    origin: str
    record_key: str = ""
    username: str = ""
    host: str = ""
    port: int = 0
    use_ssl: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_label": self.label,
            "source_kind": self.kind,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class MailboxRecoveryCheckpoint:
    """Process-local mailbox cursor used only by the trusted credential broker."""

    source: MailboxSource
    known_uids: frozenset[str]
    captured_at: datetime


def _account_store() -> AccountSiteStore:
    smart_dir = Path(config.STATE_DIR) / "smart_submission"
    return AccountSiteStore(
        smart_dir / "credential_store.local.json",
        smart_dir / "site_registry.json",
        vault_path=config.ACCOUNT_VAULT_PATH,
        legacy_vault_path=config.LEGACY_CREDENTIAL_VAULT_PATH,
    )


def _normalized(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip().casefold()


def _safe_text(value: Any, *, limit: int) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    text = "".join(char for char in text if ord(char) >= 32 or char in "\t\n")
    return text[:limit]


def _source_identifier(kind: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}|{identity.casefold()}".encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:12]}"


def _canonical_origin(value: Any) -> str:
    parts = urlsplit(str(value or "").strip())
    scheme = parts.scheme.casefold()
    host = (parts.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host:
        return ""
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return ""
    return f"{scheme}://{host}:{port}"


def _account_source_priority(summary: Mapping[str, Any]) -> tuple[int, int, str]:
    source = str(summary.get("source_kind") or "").casefold()
    source_rank = {"vault": 0, "smart": 1, "legacy": 2}.get(source, 9)
    ready_rank = 0 if summary.get("auto_fill_ready") else 1
    return (source_rank, ready_rank, str(summary.get("record_key") or ""))


def discover_monitor_mailboxes(
    store: AccountSiteStore | None = None,
) -> list[MailboxSource]:
    """Return one canonical source per configured mailbox identity.

    Duplicate legacy/imported records are deliberately collapsed.  The chosen
    record key remains process-local and is never returned by ``public()``.
    """

    active_store = store or _account_store()
    summaries = active_store.list_account_summaries(scope="email")
    gmail_configured = False
    xujc_by_identity: dict[str, Mapping[str, Any]] = {}
    for summary in summaries:
        if not isinstance(summary, Mapping):
            continue
        provider = _normalized(summary.get("provider"))
        login_url = str(summary.get("login_url") or "").strip()
        host = (urlsplit(login_url).hostname or "").casefold()
        username = str(summary.get("username") or "").strip()
        domain = username.rsplit("@", 1)[-1].casefold() if "@" in username else ""
        if "gmail" in provider or host in {"mail.google.com", "accounts.google.com"}:
            gmail_configured = gmail_configured or bool(
                summary.get("auto_fill_ready") or summary.get("password_saved")
            )
        if not (
            domain.endswith("xujc.com")
            or host in {"mail.xujc.com", "pop.xujc.com"}
            or "xujcmail" in provider
            or "school mailbox" in provider
        ):
            continue
        if not username or not summary.get("auto_fill_ready"):
            continue
        identity = username.casefold()
        current = xujc_by_identity.get(identity)
        if current is None or _account_source_priority(summary) < _account_source_priority(current):
            xujc_by_identity[identity] = summary

    sources: list[MailboxSource] = []
    # Retain the existing Gmail browser channel even when the account registry
    # has only a logged-in browser session and no canonical credential record.
    sources.append(
        MailboxSource(
            source_id="gmail",
            label="Gmail",
            kind="gmail_browser",
            origin=GMAIL_MONITOR_ORIGIN,
        )
    )
    _ = gmail_configured  # readiness is determined from the live browser page
    for identity, summary in sorted(xujc_by_identity.items()):
        provider = _safe_text(summary.get("provider") or "学校邮箱", limit=80)
        sources.append(
            MailboxSource(
                source_id=_source_identifier("xujc_mail", identity),
                label=provider or "学校邮箱",
                kind="xujc_pop3",
                origin=XUJC_WEBMAIL_ORIGINS[0],
                record_key=str(summary.get("record_key") or ""),
                username=str(summary.get("username") or "").strip(),
                host="pop.xujc.com",
                port=110,
                use_ssl=False,
            )
        )
    return sources


def _http_connect_socket(host: str, port: int, timeout: float) -> socket.socket:
    proxy_text = (
        os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or ""
    ).strip()
    parsed = urlsplit(proxy_text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return socket.create_connection((host, port), timeout=timeout)
    proxy_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.create_connection((parsed.hostname, proxy_port), timeout=timeout)
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Proxy-Connection: Keep-Alive\r\n\r\n"
    ).encode("ascii")
    sock.sendall(request)
    response = bytearray()
    while b"\r\n\r\n" not in response and len(response) < 16_384:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response.extend(chunk)
    first_line = bytes(response).split(b"\r\n", 1)[0]
    if b" 200 " not in first_line:
        sock.close()
        raise OSError("mail proxy tunnel was not established")
    return sock


class _ProxyAwarePOP3(poplib.POP3):
    def _create_socket(self, timeout: float | object) -> socket.socket:
        resolved_timeout = float(timeout) if isinstance(timeout, (int, float)) else 15.0
        return _http_connect_socket(self.host, self.port, resolved_timeout)


def _open_xujc_pop3(source: MailboxSource, password: str, *, timeout: float = 15.0) -> Any:
    username = source.username.strip()
    if username.casefold().endswith("@xujc.com"):
        # The XUJC teacher-domain login form expects the local part.  Student
        # and alumni domains continue to use the documented full address.
        username = username.split("@", 1)[0]
    factories: list[type[Any]] = [
        poplib.POP3_SSL if source.use_ssl else poplib.POP3
    ]
    proxy_configured = any(
        str(os.environ.get(name) or "").strip()
        for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")
    )
    if not source.use_ssl and proxy_configured:
        # POP3 over an HTTP CONNECT proxy is noticeably less stable on the
        # school server.  Prefer the direct route and retain the proxy as a
        # compatibility fallback for networks where direct port 110 is closed.
        factories.append(_ProxyAwarePOP3)
    last_error: Exception | None = None
    for factory in factories:
        client: Any | None = None
        try:
            client = factory(source.host, source.port, timeout=timeout)
            client.user(username)
            client.pass_(password)
            return client
        except Exception as exc:
            last_error = exc
            try:
                if client is not None:
                    client.quit()
            except Exception:
                try:
                    if client is not None:
                        client.close()
                except Exception:
                    pass
    if last_error is not None:
        raise last_error
    raise OSError("school mailbox connection was not established")


def _pop3_uid_map(client: Any) -> dict[str, int]:
    """Return UIDL -> message number without exposing message content."""

    response = client.uidl()
    lines = response[1] if isinstance(response, tuple) and len(response) > 1 else []
    result: dict[str, int] = {}
    for raw in lines or []:
        parts = bytes(raw).decode("ascii", errors="ignore").strip().split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        result[parts[1]] = int(parts[0])
    return result


def capture_password_recovery_checkpoint(
    email_address: str,
    *,
    store: AccountSiteStore | None = None,
    sources: Iterable[MailboxSource] | None = None,
) -> MailboxRecoveryCheckpoint | None:
    """Capture the current mailbox cursor before requesting a reset link.

    The returned object contains the process-local account record key, so it
    must never be serialized or passed to a model prompt.
    """

    active_store = store or _account_store()
    wanted = str(email_address or "").strip().casefold()
    configured = list(sources or discover_monitor_mailboxes(active_store))
    source = next(
        (
            item
            for item in configured
            if item.kind == "xujc_pop3"
            and item.username.strip().casefold() == wanted
        ),
        None,
    )
    if source is None:
        return None
    password = active_store.reveal_password(source.record_key)
    client: Any | None = None
    try:
        client = _open_xujc_pop3(source, password, timeout=20.0)
        password = ""
        known = frozenset(_pop3_uid_map(client))
        return MailboxRecoveryCheckpoint(
            source=source,
            known_uids=known,
            captured_at=datetime.now(timezone.utc),
        )
    finally:
        password = ""
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


def _message_recovery_material(
    message: email.message.EmailMessage,
) -> tuple[str, str, str]:
    subject = _decoded_header(message.get("Subject"))
    sender_domain = _sender_domain(message.get("From"))
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() != "text":
                continue
            try:
                parts.append(str(part.get_content() or ""))
            except Exception:
                continue
    else:
        try:
            parts.append(str(message.get_content() or ""))
        except Exception:
            pass
    return subject, sender_domain, "\n".join(parts)


def _trusted_recovery_url(
    message: email.message.EmailMessage,
    *,
    allowed_origins: Iterable[str],
    journal_path: str,
) -> str:
    subject, sender_domain, body = _message_recovery_material(message)
    trusted_sender = any(
        sender_domain == domain or sender_domain.endswith("." + domain)
        for domain in (
            "manuscriptcentral.com",
            "scholarone.com",
            "clarivate.com",
            "silverchair.com",
        )
    )
    message_markers = f"{subject}\n{body}".casefold()
    if not trusted_sender or not any(
        marker in message_markers
        for marker in ("reset password", "password reset", "scholarone")
    ):
        return ""
    origins = {
        origin for value in allowed_origins if (origin := _canonical_origin(value))
    }
    raw_urls = re.findall(r"(?i)https?://[^\s<>\"']+", html.unescape(body))
    path_hint = "/" + str(journal_path or "").strip("/").casefold()
    candidates: list[str] = []
    for raw in raw_urls:
        value = raw.rstrip(").,;]>\"")
        parts = urlsplit(value)
        origin = _canonical_origin(value)
        if origin not in origins:
            # Unwrap common mail-security redirect links only when their target
            # itself is on the exact allowlisted journal origin.
            query = parse_qs(parts.query)
            wrapped = next(
                (
                    unquote(item)
                    for key in ("url", "u", "target")
                    for item in query.get(key, [])
                    if str(item).startswith(("http://", "https://"))
                ),
                "",
            )
            if not wrapped:
                continue
            value = wrapped
            parts = urlsplit(value)
            origin = _canonical_origin(value)
            if origin not in origins:
                continue
        path = (parts.path or "/").casefold()
        lowered = value.casefold()
        if any(
            marker in path
            for marker in ("privacy", "terms", "cookie", "accessibility", "help")
        ):
            continue
        explicit_reset = any(
            marker in lowered
            for marker in ("reset", "password", "credential", "token")
        )
        journal_token_link = bool(
            path_hint != "/"
            and (path == path_hint or path.startswith(path_hint + "/"))
            and parts.query
        )
        site_token_link = bool(
            path_hint != "/"
            and path.startswith("/site" + path_hint + "/")
            and parts.query
        )
        if explicit_reset or journal_token_link or site_token_link:
            candidates.append(value)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: ("reset" not in item.casefold(), -len(item)))
    return candidates[0]


def _trusted_inderscience_credential_reminder(
    message: email.message.EmailMessage,
    *,
    reminder_kind: str,
) -> str:
    """Extract one Inderscience reminder value for the local broker only.

    The returned username/password is deliberately never included in mailbox
    evidence, audit output, model prompts, or serialized state.
    """

    kind = str(reminder_kind or "").strip().casefold()
    if kind not in {"username", "password"}:
        return ""
    subject, sender_domain, body = _message_recovery_material(message)
    trusted_sender = any(
        sender_domain == domain or sender_domain.endswith("." + domain)
        for domain in (
            "inderscience.com",
            "indersciencesubmissions.com",
        )
    )
    decoded = html.unescape(body)
    flattened = _SPACE_RE.sub(" ", _HTML_RE.sub(" ", decoded)).strip()
    searchable = f"{subject}\n{flattened}".casefold()
    if not trusted_sender or "inderscience" not in searchable:
        return ""
    if kind == "username":
        patterns = (
            r"(?i)\b(?:your\s+)?user\s*name\b\s*(?:is|:|=|-)\s*([A-Za-z0-9][A-Za-z0-9_.@+-]{1,127})",
            r"(?i)\b(?:your\s+)?user\s*id\b\s*(?:is|:|=|-)\s*([A-Za-z0-9][A-Za-z0-9_.@+-]{1,127})",
        )
    else:
        patterns = (
            r"(?i)\b(?:your\s+)?password\b\s*(?:is|:|=|-)\s*([^\s<>{}]{4,128})",
            r"(?i)\bpassword\s+reminder\b\s*(?:is|:|=|-)\s*([^\s<>{}]{4,128})",
        )
    for pattern in patterns:
        match = re.search(pattern, flattened)
        if match is None:
            continue
        value = str(match.group(1) or "").strip()
        if not value or value.casefold() in {
            "account",
            "details",
            "reminder",
            "request",
            "successfully",
        }:
            continue
        return value
    return ""


def wait_for_inderscience_credential_reminder(
    checkpoint: MailboxRecoveryCheckpoint,
    *,
    reminder_kind: str,
    store: AccountSiteStore | None = None,
    timeout_seconds: float = 45.0,
    poll_interval: float = 3.0,
) -> str:
    """Wait for one trusted private Inderscience username/password reminder."""

    active_store = store or _account_store()
    deadline = time.monotonic() + max(1.0, min(55.0, timeout_seconds))
    seen = set(checkpoint.known_uids)
    parser = BytesParser(policy=default_email_policy)
    while time.monotonic() < deadline:
        password = active_store.reveal_password(checkpoint.source.record_key)
        client: Any | None = None
        try:
            client = _open_xujc_pop3(checkpoint.source, password, timeout=15.0)
            password = ""
            uid_map = _pop3_uid_map(client)
            fresh = [
                (uid, number)
                for uid, number in uid_map.items()
                if uid not in seen
            ]
            fresh.sort(key=lambda item: item[1], reverse=True)
            for uid, number in fresh[:20]:
                seen.add(uid)
                try:
                    _response, lines, _octets = client.retr(number)
                    message = parser.parsebytes(b"\r\n".join(lines))
                except Exception:
                    continue
                value = _trusted_inderscience_credential_reminder(
                    message,
                    reminder_kind=reminder_kind,
                )
                if value:
                    return value
        except (OSError, TimeoutError, poplib.error_proto):
            # The school POP3 endpoint is intermittently slow.  A transient
            # connection failure must remain an automatic retry, not become a
            # visible manual-login checkpoint.
            pass
        finally:
            password = ""
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(max(0.25, poll_interval), remaining))
    return ""


def find_recent_inderscience_credential_reminder(
    email_address: str,
    *,
    reminder_kind: str,
    requested_after: datetime,
    store: AccountSiteStore | None = None,
    max_messages: int = 20,
) -> str:
    """Find a recent trusted Inderscience reminder without issuing another."""

    active_store = store or _account_store()
    wanted = str(email_address or "").strip().casefold()
    source = next(
        (
            item
            for item in discover_monitor_mailboxes(active_store)
            if item.kind == "xujc_pop3"
            and item.username.strip().casefold() == wanted
        ),
        None,
    )
    if source is None:
        return ""
    threshold = requested_after.astimezone(timezone.utc) - timedelta(minutes=2)
    password = active_store.reveal_password(source.record_key)
    client: Any | None = None
    parser = BytesParser(policy=default_email_policy)
    try:
        client = _open_xujc_pop3(source, password, timeout=15.0)
        password = ""
        numbers = sorted(_pop3_uid_map(client).values(), reverse=True)[
            : max(1, int(max_messages))
        ]
        for number in numbers:
            try:
                _response, headers, _octets = client.top(number, 0)
                header_message = parser.parsebytes(b"\r\n".join(headers))
            except Exception:
                continue
            sender_domain = _sender_domain(header_message.get("From"))
            if not any(
                sender_domain == domain or sender_domain.endswith("." + domain)
                for domain in (
                    "inderscience.com",
                    "indersciencesubmissions.com",
                )
            ):
                continue
            raw_date = str(header_message.get("Date") or "").strip()
            try:
                message_date = parsedate_to_datetime(raw_date)
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if message_date.astimezone(timezone.utc) < threshold:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            try:
                _response, lines, _octets = client.retr(number)
                message = parser.parsebytes(b"\r\n".join(lines))
            except Exception:
                continue
            value = _trusted_inderscience_credential_reminder(
                message,
                reminder_kind=reminder_kind,
            )
            if value:
                return value
        return ""
    except (OSError, TimeoutError, poplib.error_proto):
        return ""
    finally:
        password = ""
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


def wait_for_password_recovery_link(
    checkpoint: MailboxRecoveryCheckpoint,
    *,
    allowed_origins: Iterable[str],
    journal_path: str,
    store: AccountSiteStore | None = None,
    timeout_seconds: float = 45.0,
    poll_interval: float = 3.0,
) -> str:
    """Return one exact-origin reset URL without logging or serializing it."""

    active_store = store or _account_store()
    deadline = time.monotonic() + max(1.0, min(55.0, timeout_seconds))
    seen = set(checkpoint.known_uids)
    parser = BytesParser(policy=default_email_policy)
    while time.monotonic() < deadline:
        password = active_store.reveal_password(checkpoint.source.record_key)
        client: Any | None = None
        try:
            client = _open_xujc_pop3(checkpoint.source, password, timeout=20.0)
            password = ""
            uid_map = _pop3_uid_map(client)
            fresh = [
                (uid, number)
                for uid, number in uid_map.items()
                if uid not in seen
            ]
            fresh.sort(key=lambda item: item[1], reverse=True)
            for uid, number in fresh[:20]:
                seen.add(uid)
                try:
                    _response, lines, _octets = client.retr(number)
                    message = parser.parsebytes(b"\r\n".join(lines))
                except Exception:
                    continue
                value = _trusted_recovery_url(
                    message,
                    allowed_origins=allowed_origins,
                    journal_path=journal_path,
                )
                if value:
                    return value
        except (OSError, TimeoutError, poplib.error_proto):
            # Preserve the bounded polling window across transient school-mail
            # connection failures instead of turning them into manual work.
            pass
        finally:
            password = ""
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(max(0.25, poll_interval), remaining))
    return ""


def find_recent_password_recovery_link(
    email_address: str,
    *,
    allowed_origins: Iterable[str],
    journal_path: str,
    requested_after: datetime,
    store: AccountSiteStore | None = None,
    max_messages: int = 30,
) -> str:
    """Find a recently delivered reset link without issuing another request."""

    active_store = store or _account_store()
    wanted = str(email_address or "").strip().casefold()
    source = next(
        (
            item
            for item in discover_monitor_mailboxes(active_store)
            if item.kind == "xujc_pop3"
            and item.username.strip().casefold() == wanted
        ),
        None,
    )
    if source is None:
        return ""
    threshold = requested_after.astimezone(timezone.utc) - timedelta(minutes=2)
    password = active_store.reveal_password(source.record_key)
    client: Any | None = None
    parser = BytesParser(policy=default_email_policy)
    try:
        client = _open_xujc_pop3(source, password, timeout=20.0)
        password = ""
        uid_map = _pop3_uid_map(client)
        numbers = sorted(uid_map.values(), reverse=True)[: max(1, int(max_messages))]
        for number in numbers:
            try:
                _response, lines, _octets = client.retr(number)
                message = parser.parsebytes(b"\r\n".join(lines))
            except Exception:
                continue
            raw_date = str(message.get("Date") or "").strip()
            try:
                message_date = parsedate_to_datetime(raw_date)
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if message_date.astimezone(timezone.utc) < threshold:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            value = _trusted_recovery_url(
                message,
                allowed_origins=allowed_origins,
                journal_path=journal_path,
            )
            if value:
                return value
        return ""
    finally:
        password = ""
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


def _decoded_header(value: Any) -> str:
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")


def _message_snippet(message: email.message.EmailMessage) -> str:
    text = ""
    try:
        body = message.get_body(preferencelist=("plain", "html"))
    except Exception:
        body = None
    if body is not None:
        try:
            text = body.get_content()
        except Exception:
            text = ""
    if not text and not message.is_multipart():
        try:
            text = message.get_content()
        except Exception:
            text = ""
    text = html.unescape(_HTML_RE.sub(" ", str(text or "")))
    return _safe_text(text, limit=600)


def _sender_domain(value: Any) -> str:
    _name, address = parseaddr(str(value or ""))
    domain = address.rsplit("@", 1)[-1].casefold() if "@" in address else ""
    return domain[:160] if re.fullmatch(r"[a-z0-9.-]+", domain) else ""


def _message_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except (TypeError, ValueError, OverflowError):
        return _safe_text(text, limit=80)


def _paper_needles(paper: Mapping[str, Any]) -> tuple[tuple[str, ...], str]:
    ids: list[str] = []
    for value in (
        paper.get("canonical_submission_id"),
        paper.get("submission_id"),
        paper.get("manuscript_id"),
        paper.get("submission_number"),
        paper.get("tracking_id"),
    ):
        clean = normalize_submission_id(value)
        normalized = _normalized(clean)
        if normalized and normalized not in ids:
            ids.append(normalized)
    for clean in submission_records(paper):
        normalized = _normalized(clean)
        if normalized and normalized not in ids:
            ids.append(normalized)
    history = paper.get("submission_attempt_history")
    if isinstance(history, list):
        for item in history[-20:]:
            if not isinstance(item, Mapping):
                continue
            normalized = _normalized(normalize_submission_id(item.get("submission_id")))
            if normalized and normalized not in ids:
                ids.append(normalized)
    title = _normalized(paper.get("title") or paper.get("paper_title"))
    return tuple(ids), title


def _read_pop3_candidates(
    client: Any,
    papers: Mapping[str, Mapping[str, Any]],
    source: MailboxSource,
    *,
    max_messages: int,
) -> dict[str, list[dict[str, Any]]]:
    candidates = {str(paper_id): [] for paper_id in papers}
    count = int(client.stat()[0])
    first = max(1, count - max(1, int(max_messages)) + 1)
    parser = BytesParser(policy=default_email_policy)
    for message_number in range(count, first - 1, -1):
        try:
            _response, lines, _octets = client.top(message_number, 30)
        except Exception:
            _response, lines, _octets = client.retr(message_number)
            lines = list(lines[:160])
        raw = b"\r\n".join(lines)
        try:
            message = parser.parsebytes(raw)
        except Exception:
            continue
        subject = _safe_text(_decoded_header(message.get("Subject")), limit=500)
        snippet = _message_snippet(message)
        haystack = _normalized(f"{subject} {snippet}")
        if not haystack:
            continue
        for paper_id, paper in papers.items():
            submission_ids, title = _paper_needles(paper)
            matched_by = ""
            if any(submission_id in haystack for submission_id in submission_ids):
                matched_by = "submission_id"
            elif title and len(title) >= 12 and title in haystack:
                matched_by = "exact_title"
            if not matched_by or len(candidates[str(paper_id)]) >= 3:
                continue
            candidates[str(paper_id)].append(
                {
                    "source_id": source.source_id,
                    "source_label": source.label,
                    "source_kind": source.kind,
                    "subject": subject,
                    "snippet": snippet,
                    "message_date": _message_date(message.get("Date")),
                    "sender_domain": _sender_domain(message.get("From")),
                    "matched_by": matched_by,
                }
            )
    return candidates


def collect_local_mailbox_evidence(
    papers: Mapping[str, Mapping[str, Any]],
    *,
    store: AccountSiteStore | None = None,
    sources: Iterable[MailboxSource] | None = None,
    pop_factory: Callable[[MailboxSource, str], Any] | None = None,
    max_messages: int = 120,
) -> dict[str, Any]:
    """Collect bounded evidence from configured non-Gmail mailboxes.

    Returned data is safe to place in an SDK prompt.  Secrets and account
    identities never cross this function boundary.
    """

    active_store = store or _account_store()
    configured = list(sources or discover_monitor_mailboxes(active_store))
    paper_map = {
        str(paper_id): dict(paper)
        for paper_id, paper in papers.items()
        if str(paper_id).strip() and isinstance(paper, Mapping)
    }
    source_results: list[dict[str, Any]] = []
    paper_evidence = {paper_id: [] for paper_id in paper_map}
    for source in configured:
        if source.kind == "gmail_browser":
            continue
        public = source.public()
        public.update({"available": False, "status": "waiting", "error_code": ""})
        client: Any | None = None
        password = ""
        try:
            password = active_store.reveal_password(source.record_key)
            client = (
                pop_factory(source, password)
                if pop_factory is not None
                else _open_xujc_pop3(source, password)
            )
            password = ""
            matches = _read_pop3_candidates(
                client,
                paper_map,
                source,
                max_messages=max_messages,
            )
            for paper_id, values in matches.items():
                paper_evidence[paper_id].extend(values)
            public.update(
                {
                    "available": True,
                    "status": "monitoring",
                    "error_code": "",
                    "matched_paper_count": sum(bool(values) for values in matches.values()),
                }
            )
        except Exception as exc:
            public["error_code"] = (
                "MAILBOX_AUTH_FAILED"
                if isinstance(exc, poplib.error_proto)
                else "MAILBOX_READ_FAILED"
            )
        finally:
            password = ""
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
        source_results.append(public)
    return {
        "checked_at": now_iso(),
        "sources": source_results,
        "paper_evidence": paper_evidence,
        "configured_source_count": len(source_results),
        "available_source_count": sum(bool(item.get("available")) for item in source_results),
    }


def _parse_result_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sanitize_monitor_source_results(
    values: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            continue
        source_id = re.sub(
            r"[^a-z0-9._-]+",
            "_",
            str(raw.get("source_id") or f"source-{index + 1}").strip().casefold(),
        )[:80]
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        status = str(raw.get("status") or "monitoring").strip().casefold()
        if status not in MONITOR_SOURCE_STATUSES:
            status = "monitoring"
        action_kind = str(raw.get("action_kind") or "none").strip().casefold()
        if action_kind not in MONITOR_ACTION_KINDS:
            action_kind = "unknown"
        sender_domain = str(raw.get("sender_domain") or "").strip().casefold()[:160]
        if sender_domain and not re.fullmatch(r"[a-z0-9.-]+", sender_domain):
            sender_domain = ""
        sanitized.append(
            {
                "source_id": source_id,
                "source_label": _safe_text(
                    raw.get("source_label") or source_id,
                    limit=80,
                ),
                "source_kind": re.sub(
                    r"[^a-z0-9._-]+",
                    "_",
                    str(raw.get("source_kind") or "mailbox").strip().casefold(),
                )[:80],
                "available": bool(raw.get("available", status not in {"waiting", "blocked"})),
                "status": status,
                "action_kind": action_kind,
                "summary": _safe_text(raw.get("summary"), limit=240),
                "message_date": _safe_text(raw.get("message_date"), limit=80),
                "sender_domain": sender_domain,
                "submission_id": re.sub(
                    r"[^A-Za-z0-9._:/-]+",
                    "",
                    str(raw.get("submission_id") or "").strip(),
                )[:160],
                "error_code": re.sub(
                    r"[^A-Z0-9_]+",
                    "_",
                    str(raw.get("error_code") or "").strip().upper(),
                )[:100],
            }
        )
    return sanitized


def aggregate_monitor_source_results(
    raw_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministically merge per-source classifications into one decision."""

    raw_sources = raw_result.get("source_results")
    source_results = sanitize_monitor_source_results(
        raw_sources if isinstance(raw_sources, list) else []
    )
    if not source_results:
        source_results = sanitize_monitor_source_results(
            [
                {
                    "source_id": raw_result.get("source_id") or "gmail",
                    "source_label": raw_result.get("source_label") or "Gmail",
                    "source_kind": raw_result.get("source_kind") or "gmail_browser",
                    "available": str(raw_result.get("status") or "")
                    not in {"waiting", "blocked"},
                    "status": raw_result.get("status") or "monitoring",
                    "action_kind": raw_result.get("action_kind") or "none",
                    "summary": raw_result.get("summary") or "",
                    "message_date": raw_result.get("message_date") or "",
                    "sender_domain": raw_result.get("sender_domain") or "",
                    "submission_id": raw_result.get("submission_id") or "",
                }
            ]
        )
    actionable = [item for item in source_results if item["status"] in _ACTIONABLE_STATUSES]
    if not actionable:
        available_count = sum(bool(item.get("available")) for item in source_results)
        return {
            "status": "monitoring",
            "action_kind": "none",
            "summary": (
                f"{available_count}/{len(source_results)} 个邮箱来源已检查，未发现明确需处理通知；继续核对期刊后台。"
            ),
            "requires_dashboard": True,
            "conflict": False,
            "source_results": source_results,
        }

    statuses = {str(item["status"]) for item in actionable}
    chosen: dict[str, Any] | None = None
    conflict = len(statuses) > 1
    dated = [
        (parsed, item)
        for item in actionable
        if (parsed := _parse_result_time(item.get("message_date"))) is not None
    ]
    if conflict and dated:
        dated.sort(key=lambda pair: pair[0], reverse=True)
        if len(dated) == 1 or dated[0][0] > dated[1][0]:
            chosen = dated[0][1]
            conflict = False
    if conflict:
        return {
            "status": "monitoring",
            "action_kind": "unknown",
            "summary": "不同邮箱来源返回了冲突的明确状态；必须以期刊后台核验结果为准。",
            "requires_dashboard": True,
            "conflict": True,
            "source_results": source_results,
        }
    if chosen is None:
        chosen = max(
            actionable,
            key=lambda item: (
                _parse_result_time(item.get("message_date")) or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("source_id") or ""),
            ),
        )
    return {
        "status": str(chosen["status"]),
        "action_kind": str(chosen.get("action_kind") or "none"),
        "summary": str(chosen.get("summary") or "")[:240],
        "message_date": str(chosen.get("message_date") or "")[:80],
        "sender_domain": str(chosen.get("sender_domain") or "")[:160],
        "submission_id": str(chosen.get("submission_id") or "")[:160],
        "requires_dashboard": False,
        "conflict": False,
        "source_results": source_results,
    }
