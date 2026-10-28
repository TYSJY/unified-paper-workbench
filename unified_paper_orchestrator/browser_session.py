"""Value-free, fail-closed browser-session evidence for Smart Submission.

The probe only talks to the two loopback CDP endpoints owned by this project.
It never reads cookies, storage, form values, document text, or network bodies.
The only page-derived values allowed into evidence are the exact origin and a
small set of boolean UI-state predicates.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from .credential_vault import protect_text, unprotect_text
from .io_utils import read_json, write_json_atomic
from .windows_security import restrict_private_path


ALLOWED_CDP_PORTS = (9224, 9222)
BROWSER_SESSION_EVIDENCE_SCHEMA = "smart-browser-session-evidence/v1"
BROWSER_SESSION_PROBE_METHOD = "cdp-runtime-value-free/v1"
BROWSER_SESSION_SIGNATURE_ALGORITHM = "hmac-sha256"
BROWSER_SESSION_EVIDENCE_TTL = timedelta(minutes=5)
MAX_CDP_HTTP_BYTES = 1024 * 1024
MAX_CDP_FRAME_BYTES = 1024 * 1024

PROBE_CDP_UNAVAILABLE = "SMART_SESSION_PROBE_CDP_UNAVAILABLE"
PROBE_TARGET_NOT_OPEN = "SMART_SESSION_PROBE_TARGET_ORIGIN_NOT_OPEN"
PROBE_REDIRECT_MISMATCH = "SMART_SESSION_PROBE_REDIRECT_ORIGIN_MISMATCH"
PROBE_LOGIN_VISIBLE = "SMART_SESSION_PROBE_LOGIN_CHALLENGE_VISIBLE"
PROBE_INCONCLUSIVE = "SMART_SESSION_PROBE_INCONCLUSIVE"
PROBE_ORIGIN_UNVERIFIED = "SMART_SESSION_PROBE_ORIGIN_UNVERIFIED"
PROBE_EVIDENCE_INVALID = "SMART_SESSION_PROBE_EVIDENCE_INVALID"

_EVIDENCE_KEYS = {
    "schema",
    "evidence_id",
    "live",
    "account_id",
    "origin",
    "checked_at",
    "expires_at",
    "probe_method",
    "cdp_port",
    "page_state",
    "value_free",
    "signature_alg",
    "signature",
}
_PAGE_STATE_KEYS = {
    "target_type",
    "document_ready",
    "exact_origin",
    "login_challenge_visible",
    "authenticated_ui_visible",
}


class BrowserSessionProbeError(RuntimeError):
    """Stable value-free probe refusal. Backend diagnostics never cross it."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.safe_message = str(message)
        super().__init__(self.safe_message)

    def as_dict(self, *, account_id: str = "", origin: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.safe_message,
        }
        if account_id:
            result["account_id"] = account_id
        if origin:
            result["origin"] = normalize_origin(origin)
        return result


class _CdpTransportError(RuntimeError):
    """Internal marker whose text is never exposed outside this module."""


class CdpProbeTransport(Protocol):
    def list_targets(self, port: int) -> list[Mapping[str, Any]]: ...

    def evaluate_value_free_state(
        self,
        port: int,
        websocket_url: str,
    ) -> Mapping[str, Any]: ...

    def evaluate_submission_confirmation_state(
        self,
        port: int,
        websocket_url: str,
    ) -> Mapping[str, Any]: ...


def normalize_origin(value: Any) -> str:
    """Return a canonical HTTP(S) origin including an explicit port."""

    parsed = urllib.parse.urlsplit(str(value or "").strip())
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return ""
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return ""
    return f"{scheme}://{host}:{port}"


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_evidence_bytes(evidence: Mapping[str, Any]) -> bytes:
    payload = {key: value for key, value in evidence.items() if key != "signature"}
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def evidence_has_value_free_shape(evidence: Mapping[str, Any]) -> bool:
    """Reject additional fields so CDP/DOM values cannot hitchhike in evidence."""

    if not isinstance(evidence, Mapping) or set(evidence) != _EVIDENCE_KEYS:
        return False
    page_state = evidence.get("page_state")
    if not isinstance(page_state, Mapping) or set(page_state) != _PAGE_STATE_KEYS:
        return False
    if evidence.get("schema") != BROWSER_SESSION_EVIDENCE_SCHEMA:
        return False
    if evidence.get("probe_method") != BROWSER_SESSION_PROBE_METHOD:
        return False
    if evidence.get("signature_alg") != BROWSER_SESSION_SIGNATURE_ALGORITHM:
        return False
    if evidence.get("live") is not True or evidence.get("value_free") is not True:
        return False
    if page_state.get("target_type") != "page":
        return False
    for field in (
        "document_ready",
        "exact_origin",
        "login_challenge_visible",
        "authenticated_ui_visible",
    ):
        if not isinstance(page_state.get(field), bool):
            return False
    account_id = str(evidence.get("account_id") or "").strip()
    if not account_id or len(account_id) > 128 or any(ord(char) < 0x20 for char in account_id):
        return False
    evidence_id = str(evidence.get("evidence_id") or "")
    if len(evidence_id) != 32 or any(char not in "0123456789abcdef" for char in evidence_id):
        return False
    raw_origin = str(evidence.get("origin") or "")
    if not normalize_origin(raw_origin) or raw_origin != normalize_origin(raw_origin):
        return False
    for field in ("checked_at", "expires_at"):
        timestamp = str(evidence.get(field) or "")
        if not timestamp or len(timestamp) > 40:
            return False
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
    try:
        port = int(evidence.get("cdp_port"))
    except (TypeError, ValueError):
        return False
    if port not in ALLOWED_CDP_PORTS:
        return False
    signature = str(evidence.get("signature") or "")
    return len(signature) == 64 and all(char in "0123456789abcdef" for char in signature)


class BrowserSessionEvidenceKeyStore:
    """DPAPI-protected current-user HMAC key; read paths never create it."""

    def __init__(
        self,
        path: Path | str,
        *,
        protector: Callable[[str], str] | None = None,
        unprotector: Callable[[str], str] | None = None,
        acl_hardener: Callable[[Path | str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._protector = protector or protect_text
        self._unprotector = unprotector or unprotect_text
        self._acl_hardener = acl_hardener or restrict_private_path

    def _prepare_private_path(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._acl_hardener(self.path.parent)
            if self.path.exists():
                self._acl_hardener(self.path)
        except Exception:
            raise BrowserSessionProbeError(
                PROBE_EVIDENCE_INVALID,
                "browser-session evidence verifier storage cannot be secured",
            ) from None

    def load(self, *, create: bool = False) -> bytes:
        if not self.path.is_file():
            if not create:
                raise BrowserSessionProbeError(
                    PROBE_EVIDENCE_INVALID,
                    "browser-session evidence verifier is not initialized",
                )
            self._prepare_private_path()
            raw_key = secrets.token_bytes(32)
            encoded = base64.b64encode(raw_key).decode("ascii")
            encrypted = self._protector(encoded)
            write_json_atomic(
                self.path,
                {
                    "version": 1,
                    "scheme": "win32-dpapi-current-user+hmac-sha256",
                    "key_ciphertext": encrypted,
                },
            )
            try:
                self._acl_hardener(self.path)
            except Exception:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise BrowserSessionProbeError(
                    PROBE_EVIDENCE_INVALID,
                    "browser-session evidence verifier storage cannot be secured",
                ) from None
            encoded = ""
            encrypted = ""
            return raw_key
        if create:
            self._prepare_private_path()
        try:
            data = read_json(self.path, {})
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError
            ciphertext = str(data.get("key_ciphertext") or "")
            if not ciphertext:
                raise ValueError
            encoded = self._unprotector(ciphertext)
            raw_key = base64.b64decode(encoded.encode("ascii"), validate=True)
            encoded = ""
            if len(raw_key) != 32:
                raise ValueError
            return raw_key
        except BrowserSessionProbeError:
            raise
        except Exception:
            raise BrowserSessionProbeError(
                PROBE_EVIDENCE_INVALID,
                "browser-session evidence verifier cannot be opened safely",
            ) from None


class BrowserSessionEvidenceSigner:
    def __init__(self, key_store: BrowserSessionEvidenceKeyStore) -> None:
        self.key_store = key_store

    def sign(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(evidence)
        candidate["signature"] = "0" * 64
        if not evidence_has_value_free_shape(candidate):
            raise BrowserSessionProbeError(
                PROBE_EVIDENCE_INVALID,
                "browser-session evidence contains an unsupported field or value",
            )
        key = self.key_store.load(create=True)
        signature = hmac.new(key, _canonical_evidence_bytes(candidate), hashlib.sha256).hexdigest()
        candidate["signature"] = signature
        return candidate

    def verify(self, evidence: Mapping[str, Any]) -> bool:
        if not evidence_has_value_free_shape(evidence):
            return False
        try:
            key = self.key_store.load(create=False)
        except BrowserSessionProbeError:
            return False
        expected = hmac.new(key, _canonical_evidence_bytes(evidence), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(evidence.get("signature") or ""))


_VALUE_FREE_RUNTIME_EXPRESSION = r"""
(() => {
  const visible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
  };
  const passwordVisible = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
  const loginActionVisible = Array.from(document.querySelectorAll('form[action]')).some((element) => {
    const action = (element.getAttribute('action') || '').toLowerCase();
    return visible(element) && /(^|[\/_-])(login|log-in|signin|sign-in|authenticate)([\/_-]|$)/.test(action);
  });
  const authControls = Array.from(document.querySelectorAll('a[href],button,[role="button"]'));
  const authenticatedUiVisible = authControls.some((element) => {
    const attributes = ['href', 'action', 'name', 'id', 'data-testid', 'aria-label', 'title'];
    const marker = attributes.map((name) => element.getAttribute(name) || '').join(' ').toLowerCase();
    return visible(element) && /(^|[\s\/_-])(logout|log-out|signout|sign-out)([\s\/_-]|$)/.test(marker);
  });
  return {
    origin: window.location.origin,
    target_type: 'page',
    document_ready: document.readyState === 'interactive' || document.readyState === 'complete',
    login_challenge_visible: passwordVisible || loginActionVisible,
    authenticated_ui_visible: authenticatedUiVisible
  };
})()
""".strip()


_SUBMISSION_CONFIRMATION_RUNTIME_EXPRESSION = r"""
(() => {
  const visible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
  };
  const text = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').trim();
  const lowered = text.toLowerCase();
  const markers = [
    ['successfully_submitted', /(?:successfully submitted|submitted successfully)/i],
    ['submission_received', /(?:submission (?:has been )?received|we have received your (?:manuscript|submission))/i],
    ['thank_you_for_submission', /thank you for (?:your )?submission/i],
    ['manuscript_submitted', /(?:your )?manuscript (?:has been )?submitted/i],
    ['submission_complete', /submission (?:is )?complete/i],
    ['manuscript_submitted', /(?:\u606d\u559c[!！]?\s*)?\u60a8\u7684\u7a3f\u4ef6(?:\u5df2\u5904\u7406\u5b8c\u6bd5\u5e76)?\u5df2\u63d0\u4ea4\u7ed9\u671f\u520a(?:\u7f16\u8f91)?/i]
  ];
  const marker = (markers.find((item) => item[1].test(text)) || ['', null])[0];
  const controls = Array.from(document.querySelectorAll('button,input[type="submit"],input[type="button"],a,[role="button"]'));
  const finalSubmitControlVisible = controls.some((element) => {
    const label = [element.innerText, element.value, element.getAttribute('aria-label'), element.getAttribute('title')]
      .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
    const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true';
    return visible(element) && !disabled && /^(?:final\s+)?(?:submit|approve\s+and\s+submit|confirm\s+submission|complete\s+(?:(?:the|my)\s+)?submission)$/.test(label);
  });
  const loginChallengeVisible = Array.from(document.querySelectorAll('input[type="password"]')).some(visible)
    || /(?:sign in|log in) to continue/.test(lowered);
  const idMatch = text.match(/(?:manuscript|submission)\s*(?:id|number|no\.?|reference)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/()+:-]{3,127})/i);
  const manuscriptId = idMatch ? idMatch[1].slice(0, 128) : '';
  return {
    origin: window.location.origin,
    document_ready: document.readyState === 'interactive' || document.readyState === 'complete',
    confirmation_marker: marker,
    final_submit_control_visible: finalSubmitControlVisible,
    login_challenge_visible: loginChallengeVisible,
    manuscript_id: manuscriptId,
    page_path: window.location.pathname || '/'
  };
})()
""".strip()


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise _CdpTransportError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _masked_client_frame(payload: bytes, *, opcode: int = 1) -> bytes:
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes((0x80 | opcode, 0x80 | size))
    elif size <= 0xFFFF:
        header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size)
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", size)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + masked


def _read_server_frame(sock: socket.socket) -> tuple[int, bool, bytes]:
    first, second = _recv_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if size > MAX_CDP_FRAME_BYTES or masked:
        raise _CdpTransportError
    return opcode, fin, _recv_exact(sock, size)


class LocalCdpProbeTransport:
    """Minimal dependency-free CDP client restricted to local allowlisted ports."""

    def __init__(self, *, timeout_seconds: float = 2.0) -> None:
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _validated_port(port: int) -> int:
        value = int(port)
        if value not in ALLOWED_CDP_PORTS:
            raise _CdpTransportError
        return value

    def list_targets(self, port: int) -> list[Mapping[str, Any]]:
        value = self._validated_port(port)
        request = urllib.request.Request(
            f"http://127.0.0.1:{value}/json/list",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                    return None

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read(MAX_CDP_HTTP_BYTES + 1)
            if len(body) > MAX_CDP_HTTP_BYTES:
                raise _CdpTransportError
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, list):
                raise _CdpTransportError
            return [item for item in data if isinstance(item, Mapping)]
        except _CdpTransportError:
            raise
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            raise _CdpTransportError from None

    def evaluate_value_free_state(self, port: int, websocket_url: str) -> Mapping[str, Any]:
        return self._evaluate_expression(port, websocket_url, _VALUE_FREE_RUNTIME_EXPRESSION)

    def evaluate_submission_confirmation_state(
        self,
        port: int,
        websocket_url: str,
    ) -> Mapping[str, Any]:
        return self._evaluate_expression(
            port,
            websocket_url,
            _SUBMISSION_CONFIRMATION_RUNTIME_EXPRESSION,
        )

    def _evaluate_expression(
        self,
        port: int,
        websocket_url: str,
        expression: str,
    ) -> Mapping[str, Any]:
        value = self._validated_port(port)
        parsed = urllib.parse.urlsplit(str(websocket_url or ""))
        if (
            parsed.scheme != "ws"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.path.startswith("/devtools/")
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in parsed.path + parsed.query)
        ):
            raise _CdpTransportError
        try:
            if int(parsed.port or 0) != value:
                raise _CdpTransportError
        except ValueError:
            raise _CdpTransportError from None
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{value}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        try:
            with socket.create_connection(("127.0.0.1", value), timeout=self.timeout_seconds) as sock:
                sock.settimeout(self.timeout_seconds)
                sock.sendall(request)
                header = bytearray()
                while b"\r\n\r\n" not in header:
                    chunk = sock.recv(4096)
                    if not chunk or len(header) + len(chunk) > 65536:
                        raise _CdpTransportError
                    header.extend(chunk)
                header_block, trailing = bytes(header).split(b"\r\n\r\n", 1)
                lines = header_block.decode("iso-8859-1").split("\r\n")
                if not lines or " 101 " not in f" {lines[0]} ":
                    raise _CdpTransportError
                headers: dict[str, str] = {}
                for line in lines[1:]:
                    name, separator, item = line.partition(":")
                    if separator:
                        headers[name.strip().casefold()] = item.strip()
                if headers.get("sec-websocket-accept") != expected_accept or trailing:
                    raise _CdpTransportError
                command = json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expression,
                            "returnByValue": True,
                            "awaitPromise": False,
                            "userGesture": False,
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                sock.sendall(_masked_client_frame(command))
                fragments = bytearray()
                fragment_opcode = 0
                while True:
                    opcode, fin, payload = _read_server_frame(sock)
                    if opcode == 8:
                        raise _CdpTransportError
                    if opcode == 9:
                        sock.sendall(_masked_client_frame(payload, opcode=10))
                        continue
                    if opcode in {1, 2}:
                        fragment_opcode = opcode
                        fragments = bytearray(payload)
                    elif opcode == 0 and fragment_opcode:
                        fragments.extend(payload)
                    else:
                        continue
                    if not fin:
                        continue
                    if fragment_opcode != 1 or len(fragments) > MAX_CDP_FRAME_BYTES:
                        raise _CdpTransportError
                    message = json.loads(bytes(fragments).decode("utf-8"))
                    fragments = bytearray()
                    fragment_opcode = 0
                    if not isinstance(message, dict) or message.get("id") != 1:
                        continue
                    value_result = (
                        message.get("result", {})
                        .get("result", {})
                        .get("value")
                    )
                    if not isinstance(value_result, Mapping):
                        raise _CdpTransportError
                    return value_result
        except _CdpTransportError:
            raise
        except Exception:
            raise _CdpTransportError from None


@dataclass(frozen=True)
class BrowserSessionProbe:
    transport: CdpProbeTransport
    signer: BrowserSessionEvidenceSigner
    ports: tuple[int, ...] = ALLOWED_CDP_PORTS

    def probe(
        self,
        *,
        account_id: str,
        target_origin: str,
        verified_origins: tuple[str, ...],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clean_account_id = str(account_id or "").strip()
        origin = normalize_origin(target_origin)
        allowed = {normalize_origin(item) for item in verified_origins}
        allowed.discard("")
        if not clean_account_id or not origin or origin not in allowed:
            raise BrowserSessionProbeError(
                PROBE_ORIGIN_UNVERIFIED,
                "the requested account and exact target origin are not verified in the local site registry",
            )
        ports = tuple(int(port) for port in self.ports)
        if not ports or any(port not in ALLOWED_CDP_PORTS for port in ports):
            raise BrowserSessionProbeError(
                PROBE_CDP_UNAVAILABLE,
                "the local CDP endpoint is outside the Smart Submission allowlist",
            )

        any_transport = False
        exact_target_seen = False
        redirect_seen = False
        login_seen = False
        inconclusive_seen = False
        for port in ports:
            try:
                targets = self.transport.list_targets(port)
            except Exception:
                continue
            any_transport = True
            for target in targets:
                if str(target.get("type") or "").strip().casefold() != "page":
                    continue
                if normalize_origin(target.get("url")) != origin:
                    continue
                exact_target_seen = True
                websocket_url = str(target.get("webSocketDebuggerUrl") or "").strip()
                if not websocket_url:
                    inconclusive_seen = True
                    continue
                try:
                    state = self.transport.evaluate_value_free_state(port, websocket_url)
                except Exception:
                    inconclusive_seen = True
                    continue
                runtime_origin = normalize_origin(state.get("origin"))
                if runtime_origin != origin:
                    redirect_seen = True
                    continue
                bool_fields = (
                    "document_ready",
                    "login_challenge_visible",
                    "authenticated_ui_visible",
                )
                if any(not isinstance(state.get(field), bool) for field in bool_fields):
                    inconclusive_seen = True
                    continue
                if state.get("login_challenge_visible") is True:
                    login_seen = True
                    continue
                if state.get("document_ready") is not True or state.get("authenticated_ui_visible") is not True:
                    inconclusive_seen = True
                    continue
                checked = now or datetime.now(timezone.utc)
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                unsigned = {
                    "schema": BROWSER_SESSION_EVIDENCE_SCHEMA,
                    "evidence_id": uuid4().hex,
                    "live": True,
                    "account_id": clean_account_id,
                    "origin": origin,
                    "checked_at": _iso_utc(checked),
                    "expires_at": _iso_utc(checked + BROWSER_SESSION_EVIDENCE_TTL),
                    "probe_method": BROWSER_SESSION_PROBE_METHOD,
                    "cdp_port": port,
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
                return self.signer.sign(unsigned)

        if redirect_seen:
            raise BrowserSessionProbeError(
                PROBE_REDIRECT_MISMATCH,
                "the CDP page redirected away from the verified target origin",
            )
        if login_seen:
            raise BrowserSessionProbeError(
                PROBE_LOGIN_VISIBLE,
                "the verified target page still exposes a login challenge",
            )
        if exact_target_seen or inconclusive_seen:
            raise BrowserSessionProbeError(
                PROBE_INCONCLUSIVE,
                "the target page does not expose a value-free positive authenticated-session marker",
            )
        if any_transport:
            raise BrowserSessionProbeError(
                PROBE_TARGET_NOT_OPEN,
                "no page is open on the exact verified target origin",
            )
        raise BrowserSessionProbeError(
            PROBE_CDP_UNAVAILABLE,
            "neither approved local CDP endpoint is available",
        )
