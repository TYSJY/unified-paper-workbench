"""Dependency-free, capability-scoped CDP controls for Smart Submission.

This module deliberately implements only the browser primitives needed by the
submission worker.  It never reads cookies, browser storage, password values,
or network bodies.  The higher-level MCP server supplies the paper-specific
origin and file allow-lists.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .browser_session import ALLOWED_CDP_PORTS, normalize_origin


MAX_CDP_HTTP_BYTES = 1024 * 1024
MAX_CDP_FRAME_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_TEXT_CHARS = 24_000
MAX_SNAPSHOT_CONTROLS = 180
SMART_BROWSER_REF_ATTRIBUTE = "data-upo-smart-ref"

FINAL_CONTROL_PATTERN = re.compile(
    r"(?:^|\b)(?:"
    r"submit|final\s+submit|submit\s+(?:manuscript|article|submission)|"
    r"approve\s+and\s+submit|confirm\s+(?:and\s+)?submit|"
    r"complete\s+(?:(?:the|my)\s+)?submission|send\s+to\s+(?:journal|editor)"
    r")(?:\b|$)|"
    r"(?:^|\s)(?:提交稿件|提交文章|提交论文|最终提交|确认提交|确认并提交|完成投稿)(?:\s|$)",
    re.IGNORECASE,
)
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:password|passcode|one.?time|otp|verification.?code|security.?code|"
    r"credit.?card|card.?number|cvv|cvc|payment|access.?token|api.?key)",
    re.IGNORECASE,
)


class SmartBrowserCdpError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.safe_message = str(message)
        self.details = dict(details or {})
        super().__init__(self.safe_message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.safe_message,
            "details": self.details,
        }


def is_final_submit_control(value: Any) -> bool:
    marker = re.sub(r"\s+", " ", str(value or "")).strip()
    return bool(marker and FINAL_CONTROL_PATTERN.search(marker))


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_CDP_CONNECTION_CLOSED",
                "the local browser closed the CDP connection",
            )
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
    if masked or size > MAX_CDP_FRAME_BYTES:
        raise SmartBrowserCdpError(
            "SMART_BROWSER_CDP_FRAME_INVALID",
            "the local browser returned an invalid CDP frame",
        )
    return opcode, fin, _recv_exact(sock, size)


class _CdpSocket:
    def __init__(self, port: int, websocket_url: str, *, timeout_seconds: float) -> None:
        self.port = int(port)
        self.websocket_url = str(websocket_url)
        self.timeout_seconds = float(timeout_seconds)
        self.sock: socket.socket | None = None
        self._next_id = 1

    def __enter__(self) -> "_CdpSocket":
        if self.port not in ALLOWED_CDP_PORTS:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_CDP_PORT_DENIED",
                "the requested CDP port is outside the project allow-list",
            )
        parsed = urllib.parse.urlsplit(self.websocket_url)
        if (
            parsed.scheme != "ws"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.path.startswith("/devtools/")
        ):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_CDP_ENDPOINT_DENIED",
                "the requested CDP websocket is not an approved local endpoint",
            )
        try:
            if int(parsed.port or 0) != self.port:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_ENDPOINT_DENIED",
                    "the CDP websocket port does not match the approved endpoint",
                )
        except ValueError:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_CDP_ENDPOINT_DENIED",
                "the CDP websocket port is invalid",
            ) from None

        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (
                    websocket_key
                    + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).encode("ascii")
            ).digest()
        ).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        try:
            sock = socket.create_connection(
                ("127.0.0.1", self.port),
                timeout=self.timeout_seconds,
            )
            sock.settimeout(self.timeout_seconds)
            sock.sendall(request)
            header = bytearray()
            while b"\r\n\r\n" not in header:
                chunk = sock.recv(4096)
                if not chunk or len(header) + len(chunk) > 65536:
                    raise SmartBrowserCdpError(
                        "SMART_BROWSER_CDP_HANDSHAKE_FAILED",
                        "the local browser did not complete the CDP handshake",
                    )
                header.extend(chunk)
            header_block, trailing = bytes(header).split(b"\r\n\r\n", 1)
            lines = header_block.decode("iso-8859-1").split("\r\n")
            if not lines or " 101 " not in f" {lines[0]} " or trailing:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_HANDSHAKE_FAILED",
                    "the local browser rejected the CDP websocket",
                )
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, separator, item = line.partition(":")
                if separator:
                    headers[name.strip().casefold()] = item.strip()
            if headers.get("sec-websocket-accept") != expected_accept:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_HANDSHAKE_FAILED",
                    "the local CDP websocket handshake could not be verified",
                )
            self.sock = sock
            return self
        except SmartBrowserCdpError:
            if self.sock is not None:
                self.sock.close()
            raise
        except OSError:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_CDP_UNAVAILABLE",
                "the approved local browser endpoint is unavailable",
            ) from None

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(_masked_client_frame(b"", opcode=8))
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def command(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self.sock is None:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_CDP_CONNECTION_CLOSED",
                "the local CDP connection is not open",
            )
        command_id = self._next_id
        self._next_id += 1
        payload = json.dumps(
            {
                "id": command_id,
                "method": str(method),
                "params": dict(params or {}),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.sock.sendall(_masked_client_frame(payload))
        fragments = bytearray()
        fragment_opcode = 0
        while True:
            opcode, fin, frame_payload = _read_server_frame(self.sock)
            if opcode == 8:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_CONNECTION_CLOSED",
                    "the local browser closed the CDP connection",
                )
            if opcode == 9:
                self.sock.sendall(_masked_client_frame(frame_payload, opcode=10))
                continue
            if opcode in {1, 2}:
                fragment_opcode = opcode
                fragments = bytearray(frame_payload)
            elif opcode == 0 and fragment_opcode:
                fragments.extend(frame_payload)
            else:
                continue
            if not fin:
                continue
            if fragment_opcode != 1 or len(fragments) > MAX_CDP_FRAME_BYTES:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_FRAME_INVALID",
                    "the local browser returned an unsupported CDP response",
                )
            try:
                message = json.loads(bytes(fragments).decode("utf-8"))
            except (UnicodeError, ValueError):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_RESPONSE_INVALID",
                    "the local browser returned malformed CDP JSON",
                ) from None
            fragments = bytearray()
            fragment_opcode = 0
            if not isinstance(message, dict) or message.get("id") != command_id:
                continue
            if isinstance(message.get("error"), dict):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_COMMAND_FAILED",
                    "the local browser rejected the requested page action",
                    details={
                        "method": str(method),
                        "cdp_code": message["error"].get("code"),
                    },
                )
            result = message.get("result")
            return dict(result) if isinstance(result, dict) else {}


@dataclass(frozen=True)
class CdpPageTarget:
    port: int
    target_id: str
    url: str
    title: str
    websocket_url: str

    @property
    def origin(self) -> str:
        return normalize_origin(self.url)

    def public_dict(self) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.url)
        return {
            "page_id": self.target_id,
            "origin": self.origin,
            "url": self.url,
            "path": parsed.path or "/",
            "title": self.title[:500],
            "cdp_port": self.port,
        }


class SmartBrowserCdpController:
    """Operate visible local Chrome pages without exposing browser secrets."""

    def __init__(
        self,
        *,
        ports: Iterable[int] = ALLOWED_CDP_PORTS,
        timeout_seconds: float = 5.0,
    ) -> None:
        values = tuple(int(port) for port in ports)
        if not values or any(port not in ALLOWED_CDP_PORTS for port in values):
            raise ValueError("ports must use the approved local CDP endpoints")
        self.ports = values
        self.timeout_seconds = max(0.5, float(timeout_seconds))

    def _list_targets_for_port(self, port: int) -> list[CdpPageTarget]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/json/list",
            headers={"Accept": "application/json"},
            method="GET",
        )

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirect(),
            )
            with opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read(MAX_CDP_HTTP_BYTES + 1)
            if len(body) > MAX_CDP_HTTP_BYTES:
                raise ValueError
            data = json.loads(body.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            return []
        if not isinstance(data, list):
            return []
        targets: list[CdpPageTarget] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("type") or "").casefold() != "page":
                continue
            target_id = str(item.get("id") or "").strip()
            url = str(item.get("url") or "").strip()
            websocket_url = str(item.get("webSocketDebuggerUrl") or "").strip()
            if not target_id or not url or not websocket_url:
                continue
            targets.append(
                CdpPageTarget(
                    port=int(port),
                    target_id=target_id,
                    url=url,
                    title=str(item.get("title") or ""),
                    websocket_url=websocket_url,
                )
            )
        return targets

    def list_pages(self, allowed_origins: Iterable[str]) -> list[dict[str, Any]]:
        allowed = {normalize_origin(item) for item in allowed_origins}
        allowed.discard("")
        pages: list[dict[str, Any]] = []
        for port in self.ports:
            for target in self._list_targets_for_port(port):
                if target.origin in allowed:
                    pages.append(target.public_dict())
        return pages

    def activate_page(
        self,
        allowed_origins: Iterable[str],
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        """Restore and foreground one already verified browser page.

        The operation is deliberately limited to the caller supplied exact
        origins.  It does not navigate, read form values, or submit anything;
        it only makes the selected managed-browser tab visible for a user-owned
        manual action.
        """

        target = self._target(allowed_origins, page_id=page_id)
        window_id = 0
        with _CdpSocket(
            target.port,
            target.websocket_url,
            timeout_seconds=self.timeout_seconds,
        ) as session:
            try:
                window = session.command(
                    "Browser.getWindowForTarget",
                    {"targetId": target.target_id},
                )
                window_id = int(window.get("windowId") or 0)
                bounds = window.get("bounds")
                if (
                    window_id > 0
                    and isinstance(bounds, Mapping)
                    and str(bounds.get("windowState") or "").casefold()
                    == "minimized"
                ):
                    session.command(
                        "Browser.setWindowBounds",
                        {
                            "windowId": window_id,
                            "bounds": {"windowState": "normal"},
                        },
                    )
            except SmartBrowserCdpError:
                # Some Chromium builds omit the Browser window methods.  The
                # page-level foreground command remains the authoritative
                # operation and is supported by the managed CDP channel.
                window_id = 0
            session.command("Page.bringToFront")
        return {
            "ok": True,
            "page_id": target.target_id,
            "cdp_port": target.port,
            "window_id": window_id,
            "origin": target.origin,
            "url": target.url,
            "title": target.title,
        }

    def open_page(
        self,
        allowed_origins: Iterable[str],
        url: str,
        *,
        preferred_port: int | None = None,
        wait_seconds: float = 12.0,
    ) -> dict[str, Any]:
        """Open one visible tab on an allowlisted origin.

        This is a local controller operation.  It is intentionally not exposed
        as a credential-bearing MCP tool.
        """

        destination = str(url or "").strip()
        destination_origin = normalize_origin(destination)
        allowed = {normalize_origin(item) for item in allowed_origins}
        allowed.discard("")
        if not destination_origin or destination_origin not in allowed:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_NAVIGATION_DENIED",
                "navigation is limited to the paper's verified site origins",
                details={"origin": destination_origin},
            )
        ports = list(self.ports)
        if preferred_port is not None:
            requested = int(preferred_port)
            if requested not in ports:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_CDP_PORT_DENIED",
                    "the requested CDP port is outside the project allow-list",
                )
            ports.remove(requested)
            ports.insert(0, requested)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )
        for port in ports:
            request = urllib.request.Request(
                (
                    f"http://127.0.0.1:{port}/json/new?"
                    + urllib.parse.quote(destination, safe="")
                ),
                headers={"Accept": "application/json"},
                method="PUT",
            )
            try:
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read(MAX_CDP_HTTP_BYTES + 1)
                if len(body) > MAX_CDP_HTTP_BYTES:
                    continue
                payload = json.loads(body.decode("utf-8"))
            except (OSError, UnicodeError, ValueError, urllib.error.URLError):
                continue
            if not isinstance(payload, Mapping):
                continue
            page_id = str(payload.get("id") or "").strip()
            if not page_id:
                continue
            return self.wait_ready(
                allowed,
                page_id=page_id,
                timeout_seconds=wait_seconds,
            )
        raise SmartBrowserCdpError(
            "SMART_BROWSER_CDP_UNAVAILABLE",
            "the approved local browser endpoints could not open a visible page",
        )

    def _target(
        self,
        allowed_origins: Iterable[str],
        *,
        page_id: str = "",
    ) -> CdpPageTarget:
        allowed = {normalize_origin(item) for item in allowed_origins}
        allowed.discard("")
        requested = str(page_id or "").strip()
        candidates: list[CdpPageTarget] = []
        for port in self.ports:
            for target in self._list_targets_for_port(port):
                if target.origin not in allowed:
                    continue
                if requested and target.target_id == requested:
                    return target
                candidates.append(target)
        if requested:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_PAGE_NOT_FOUND",
                "the requested page is no longer open on an allowed origin",
                details={"page_id": requested},
            )
        if not candidates:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_PAGE_NOT_FOUND",
                "no visible browser page is open on the paper's verified site origin",
            )
        return candidates[0]

    def _evaluate(
        self,
        target: CdpPageTarget,
        expression: str,
        *,
        return_by_value: bool = True,
        user_gesture: bool = False,
    ) -> Any:
        with _CdpSocket(
            target.port,
            target.websocket_url,
            timeout_seconds=self.timeout_seconds,
        ) as session:
            result = session.command(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": bool(return_by_value),
                    "awaitPromise": True,
                    "userGesture": bool(user_gesture),
                },
            )
        if result.get("exceptionDetails"):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_PAGE_SCRIPT_FAILED",
                "the page rejected the requested browser action",
            )
        remote = result.get("result")
        if not isinstance(remote, Mapping):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_PAGE_SCRIPT_FAILED",
                "the page did not return a browser-action result",
            )
        if return_by_value:
            return remote.get("value")
        return dict(remote)

    def snapshot(
        self,
        allowed_origins: Iterable[str],
        *,
        page_id: str = "",
        max_text_chars: int = MAX_SNAPSHOT_TEXT_CHARS,
        max_controls: int = MAX_SNAPSHOT_CONTROLS,
    ) -> dict[str, Any]:
        target = self._target(allowed_origins, page_id=page_id)
        text_limit = max(1000, min(MAX_SNAPSHOT_TEXT_CHARS, int(max_text_chars)))
        control_limit = max(10, min(MAX_SNAPSHOT_CONTROLS, int(max_controls)))
        expression = f"""
(() => {{
  const refAttr = {json.dumps(SMART_BROWSER_REF_ATTRIBUTE)};
  const visible = (element) => {{
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const box = element.getBoundingClientRect();
    const role = String(element.getAttribute('role') || '').toLowerCase();
    const inputType = String(element.getAttribute('type') || '').toLowerCase();
    const semanticProxy = role === 'combobox'
      || role === 'checkbox'
      || role === 'radio'
      || (
        element.tagName === 'INPUT'
        && ['checkbox', 'radio'].includes(inputType)
      );
    if (semanticProxy) {{
      // React Select and custom checkbox/radio components keep their native
      // semantic input transparent inside a visible control.  Expose that
      // input when its containing control is visible so the SDK worker can
      // use the standard accessible interaction instead of mistaking nearby
      // policy links for the actionable control.
      const proxy = element.parentElement;
      if (proxy) {{
        const proxyStyle = window.getComputedStyle(proxy);
        const proxyBox = proxy.getBoundingClientRect();
        if (
          proxyStyle.display !== 'none'
          && proxyStyle.visibility !== 'hidden'
          && Number(proxyStyle.opacity || 1) !== 0
          && proxyBox.width > 0
          && proxyBox.height > 0
        ) return true;
      }}
    }}
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0 && box.width > 0 && box.height > 0;
  }};
  const clean = (value, limit = 500) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, limit);
  const labelFor = (element) => {{
    const id = element.getAttribute('id') || '';
    const explicit = id ? document.querySelector(`label[for="${{CSS.escape(id)}}"]`) : null;
    const wrapping = element.closest('label');
    const image = element.querySelector && element.querySelector('img');
    return clean(
      element.getAttribute('aria-label')
      || (explicit && explicit.innerText)
      || (wrapping && wrapping.innerText)
      || element.getAttribute('placeholder')
      || element.getAttribute('title')
      || element.innerText
      || (image && (
        image.getAttribute('alt')
        || image.getAttribute('title')
        || image.getAttribute('name')
      ))
      || element.getAttribute('name')
      || element.getAttribute('id')
      || element.tagName
    );
  }};
  document.querySelectorAll(`[${{refAttr}}]`).forEach(
    (element) => element.removeAttribute(refAttr)
  );
  const candidates = Array.from(document.querySelectorAll(
    'a,button,input,textarea,select,[role="button"],[role="combobox"],'
      + '[role="option"],[role="radio"],[role="checkbox"],[role="tab"],'
      + '[contenteditable="true"]'
  )).filter(visible).slice(0, {control_limit});
  const controls = candidates.map((element, index) => {{
    const ref = `upo-${{String(index + 1).padStart(4, '0')}}`;
    element.setAttribute(refAttr, ref);
    const tag = element.tagName.toLowerCase();
    const role = clean(element.getAttribute('role') || '', 80).toLowerCase();
    const type = clean(element.getAttribute('type') || tag, 80).toLowerCase();
    const href = clean(element.getAttribute('href') || '', 500);
    const registrationMatch = href.match(/CREATE_ACCOUNT_(\\d+)/i);
    const elementId = clean(element.getAttribute('id') || '', 160);
    const elementName = clean(element.getAttribute('name') || '', 160);
    const knownAction = elementId === 'createNewPassword'
      ? 'password_reset_continue'
      : '';
    const sensitive = type === 'password'
      || /password|passcode|one.?time|otp|verification.?code|security.?code|cvv|cvc|payment|token/i.test(
        [
          element.getAttribute('name'),
          element.getAttribute('id'),
          element.getAttribute('autocomplete'),
          element.getAttribute('aria-label'),
          element.getAttribute('placeholder')
        ].filter(Boolean).join(' ')
      );
    const item = {{
      ref,
      tag,
      role,
      type,
      id: elementId,
      name: elementName,
      label: (
        tag === 'input' && ['button', 'submit', 'reset'].includes(type)
          ? clean(element.value || labelFor(element))
          : labelFor(element)
      ),
      disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
      required: Boolean(element.required || element.getAttribute('aria-required') === 'true'),
      checked: typeof element.checked === 'boolean' ? Boolean(element.checked) : null,
      sensitive,
      action: registrationMatch
        ? `registration_step_${{registrationMatch[1]}}`
        : knownAction
    }};
    if (tag === 'select') {{
      item.options = Array.from(element.options || []).slice(0, 80).map((option) => ({{
        value: clean(option.value, 300),
        label: clean(option.textContent, 300),
        selected: Boolean(option.selected)
      }}));
    }}
    return item;
  }});
  const bodyText = clean((document.body && document.body.innerText) || '', {text_limit});
  const currentPage = clean(
    (document.querySelector('input[name="CURRENT_PAGE"]') || {{}}).value || '',
    80
  );
  const registrationStep = /^CREATE_ACCOUNT_\\d+$/i.test(currentPage)
    ? currentPage.toLowerCase().replace('create_account_', 'registration_step_')
    : '';
  return {{
    ok: true,
    origin: window.location.origin,
    url: window.location.href,
    path: window.location.pathname || '/',
    title: clean(document.title, 500),
    ready_state: document.readyState,
    text: bodyText,
    text_truncated: (((document.body && document.body.innerText) || '').length > {text_limit}),
    registration_step: registrationStep,
    controls
  }};
}})()
""".strip()
        value = self._evaluate(target, expression)
        if not isinstance(value, Mapping):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_SNAPSHOT_FAILED",
                "the browser page did not return a usable snapshot",
            )
        result = dict(value)
        runtime_origin = normalize_origin(result.get("origin"))
        allowed = {normalize_origin(item) for item in allowed_origins}
        if runtime_origin not in allowed:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_ORIGIN_CHANGED",
                "the browser page navigated outside the paper's verified site origins",
                details={"observed_origin": runtime_origin},
            )
        result["page_id"] = target.target_id
        result["cdp_port"] = target.port
        return result

    def navigate(
        self,
        allowed_origins: Iterable[str],
        url: str,
        *,
        page_id: str = "",
        wait_seconds: float = 12.0,
    ) -> dict[str, Any]:
        destination = str(url or "").strip()
        destination_origin = normalize_origin(destination)
        allowed = {normalize_origin(item) for item in allowed_origins}
        allowed.discard("")
        if not destination_origin or destination_origin not in allowed:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_NAVIGATION_DENIED",
                "navigation is limited to the paper's verified site origins",
                details={"origin": destination_origin},
            )
        target = self._target(allowed, page_id=page_id)
        with _CdpSocket(
            target.port,
            target.websocket_url,
            timeout_seconds=self.timeout_seconds,
        ) as session:
            session.command("Page.enable")
            session.command("Page.navigate", {"url": destination})
        return self.wait_ready(
            allowed,
            page_id=target.target_id,
            timeout_seconds=wait_seconds,
        )

    def wait_ready(
        self,
        allowed_origins: Iterable[str],
        *,
        page_id: str = "",
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.25, min(30.0, float(timeout_seconds)))
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                target = self._target(allowed_origins, page_id=page_id)
                value = self._evaluate(
                    target,
                    "({origin:window.location.origin,url:window.location.href,"
                    "ready_state:document.readyState})",
                )
                if isinstance(value, Mapping):
                    last_state = dict(value)
                    if (
                        normalize_origin(last_state.get("origin"))
                        in {normalize_origin(item) for item in allowed_origins}
                        and last_state.get("ready_state") in {"interactive", "complete"}
                    ):
                        return {
                            "ok": True,
                            "page_id": target.target_id,
                            "cdp_port": target.port,
                            **last_state,
                        }
            except SmartBrowserCdpError:
                pass
            time.sleep(0.25)
        raise SmartBrowserCdpError(
            "SMART_BROWSER_PAGE_TIMEOUT",
            "the browser page did not become ready before the bounded timeout",
            details={"last_state": last_state},
        )

    @staticmethod
    def _ref_expression(ref: str, body: str) -> str:
        clean_ref = str(ref or "").strip()
        if not re.fullmatch(r"upo-\d{4}", clean_ref):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_REF_INVALID",
                "the browser control reference is invalid; take a fresh snapshot",
            )
        return f"""
(() => {{
  const refAttr = {json.dumps(SMART_BROWSER_REF_ATTRIBUTE)};
  const ref = {json.dumps(clean_ref)};
  const element = document.querySelector(`[${{refAttr}}="${{CSS.escape(ref)}}"]`);
  if (!element) return {{ok:false, code:'SMART_BROWSER_REF_STALE'}};
  const clean = (value, limit = 500) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, limit);
  const marker = clean([
    element.innerText,
    element.value,
    element.getAttribute('aria-label'),
    element.getAttribute('title'),
    element.getAttribute('name'),
    element.getAttribute('id'),
    element.getAttribute('placeholder')
  ].filter(Boolean).join(' '));
  const tag = element.tagName.toLowerCase();
  const role = clean(element.getAttribute('role') || '', 80).toLowerCase();
  const type = clean(element.getAttribute('type') || element.tagName, 80).toLowerCase();
  const label = clean(
    element.getAttribute('aria-label')
    || element.getAttribute('title')
    || (tag === 'input' && ['button', 'submit', 'reset'].includes(type) ? element.value : '')
    || element.innerText
    || element.getAttribute('placeholder')
    || element.tagName
  );
  {body}
}})()
""".strip()

    def inspect_control(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        target = self._target(allowed_origins, page_id=page_id)
        expression = self._ref_expression(
            ref,
            """
  return {
    ok:true,
    ref,
    marker,
    label,
    tag,
    role,
    type,
    id:clean(element.getAttribute('id'), 120),
    name:clean(element.getAttribute('name'), 120),
    disabled:Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
    readonly:Boolean(element.readOnly || element.getAttribute('aria-readonly') === 'true')
  };
""",
        )
        value = self._evaluate(target, expression)
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_REF_STALE",
                "the browser control reference is stale; take a fresh snapshot",
            )
        return {"page_id": target.target_id, **dict(value)}

    def fill_trusted_secret(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        secret: str,
        *,
        page_id: str = "",
        accepted_text_field_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Fill a password field from the trusted local broker.

        The value is sent directly to the visible local browser and is never
        included in the result, an exception, a snapshot, or the SDK tool set.
        """

        target = self._target(allowed_origins, page_id=page_id)
        control = self.inspect_control(
            allowed_origins,
            ref,
            page_id=target.target_id,
        )
        accepted_ids = {
            str(item or "").strip().casefold()
            for item in accepted_text_field_ids
            if str(item or "").strip()
        }
        control_type = str(control.get("type") or "").casefold()
        control_identity = {
            str(control.get("id") or "").strip().casefold(),
            str(control.get("name") or "").strip().casefold(),
        }
        trusted_text_field = bool(
            control_type == "text"
            and accepted_ids
            and control_identity.intersection(accepted_ids)
        )
        if control_type != "password" and not trusted_text_field:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_TRUSTED_SECRET_FIELD_INVALID",
                "the trusted broker may only fill a visible password field",
            )
        if not str(secret):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_TRUSTED_SECRET_EMPTY",
                "the trusted broker received no usable password",
            )
        expression = self._ref_expression(
            ref,
            f"""
  const acceptedTextIds = {json.dumps(sorted(accepted_ids))};
  const identity = String(element.getAttribute('id') || element.getAttribute('name') || '').toLowerCase();
  const trustedTextField = type === 'text' && acceptedTextIds.includes(identity);
  if ((type !== 'password' && !trustedTextField) || element.disabled || element.readOnly
      || element.getAttribute('aria-disabled') === 'true') {{
    return {{ok:false, code:'SMART_BROWSER_TRUSTED_SECRET_FIELD_INVALID'}};
  }}
  const nextValue = {json.dumps(str(secret))};
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  element.focus();
  if (descriptor && descriptor.set) descriptor.set.call(element, nextValue);
  else element.value = nextValue;
  element.dispatchEvent(new Event('input', {{bubbles:true}}));
  element.dispatchEvent(new Event('change', {{bubbles:true}}));
  return {{ok:true, ref, tag, type}};
""",
        )
        result = self._evaluate(target, expression, user_gesture=True)
        secret = ""
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_TRUSTED_SECRET_FILL_FAILED",
                "the trusted local broker could not fill the password field",
            )
        return {
            "ok": True,
            "page_id": target.target_id,
            "ref": str(ref),
            "field_type": "password",
            "value_free": True,
        }

    def registration_preflight(
        self,
        allowed_origins: Iterable[str],
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        """Return value-free duplicate/captcha facts before account creation."""

        target = self._target(allowed_origins, page_id=page_id)
        result = self._evaluate(
            target,
            """
(() => {
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const bodyText = clean(document.body && document.body.innerText);
  const primary = document.getElementById('txtEmail');
  const alternate = document.getElementById('txtAlternateEmail');
  const usernameMessage = clean(document.getElementById('user_used')?.textContent);
  const captchaResponse = document.querySelector('[name="h-captcha-response"]');
  const emailDuplicate = String(document.getElementById('unq_input')?.value || '') === '1'
    || /email address (?:already )?exists in (?:our|the) database/.test(bodyText)
    || /already have an account using this email address/.test(bodyText)
    || /邮箱地址.*(?:已经|已)存在/.test(bodyText)
    || /电子邮件地址.*(?:已经|已)存在/.test(bodyText);
  return {
    ok: true,
    email_duplicate: Boolean(emailDuplicate),
    username_duplicate: /already|taken|已被使用|已经使用/.test(usernameMessage),
    alternate_matches_primary: Boolean(
      alternate && primary && alternate.value && alternate.value === primary.value
    ),
    captcha_present: Boolean(
      document.querySelector('iframe[src*="captcha"], [data-sitekey], .h-captcha, [name="h-captcha-response"]')
    ),
    captcha_completed: Boolean(captchaResponse && captchaResponse.value),
    value_free: true
  };
})()
""",
        )
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_REGISTRATION_PREFLIGHT_FAILED",
                "the browser could not verify the registration form state",
            )
        return {"page_id": target.target_id, **dict(result)}

    def fill(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        value: str,
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        target = self._target(allowed_origins, page_id=page_id)
        control = self.inspect_control(
            allowed_origins,
            ref,
            page_id=target.target_id,
        )
        marker = " ".join(
            (
                str(control.get("marker") or ""),
                str(control.get("type") or ""),
            )
        )
        if SENSITIVE_FIELD_PATTERN.search(marker):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_SENSITIVE_FIELD_DENIED",
                "password, one-time-code, payment, and token fields are not exposed to the SDK worker",
            )
        next_value = str(value)
        expected_length = len(next_value.encode("utf-16-le")) // 2
        prepare_expression = self._ref_expression(
            ref,
            """
  if (element.disabled || element.readOnly || element.getAttribute('aria-disabled') === 'true') {
    return {ok:false, code:'SMART_BROWSER_CONTROL_NOT_EDITABLE'};
  }
  if (type === 'password' || type === 'file' || type === 'hidden') {
    return {ok:false, code:'SMART_BROWSER_SENSITIVE_FIELD_DENIED'};
  }
  if (!element.isContentEditable && tag !== 'textarea' && tag !== 'input') {
    return {ok:false, code:'SMART_BROWSER_CONTROL_NOT_EDITABLE'};
  }
  element.scrollIntoView({block:'center', inline:'center'});
  const box = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);
  if (
    box.width <= 1
    || box.height <= 1
    || style.visibility === 'hidden'
    || style.display === 'none'
  ) {
    return {ok:false, code:'SMART_BROWSER_CONTROL_NOT_VISIBLE'};
  }
  element.focus();
  if (typeof element.select === 'function') element.select();
  return {ok:true, ref, marker, tag, type};
""",
        )
        verify_expression = self._ref_expression(
            ref,
            f"""
  const actualValue = element.isContentEditable
    ? String(element.textContent || '')
    : String(element.value || '');
  const expectedLength = {expected_length};
  return {{
    ok:actualValue.length === expectedLength,
    code:actualValue.length === expectedLength
      ? ''
      : 'SMART_BROWSER_FILL_VERIFICATION_FAILED',
    ref,
    marker,
    tag,
    type,
    value_length:actualValue.length,
    expected_length:expectedLength
  }};
""",
        )
        with _CdpSocket(
            target.port,
            target.websocket_url,
            timeout_seconds=self.timeout_seconds,
        ) as session:
            session.command("Page.bringToFront")
            prepared = session.command(
                "Runtime.evaluate",
                {
                    "expression": prepare_expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                    "userGesture": True,
                },
            )
            if prepared.get("exceptionDetails"):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_PAGE_SCRIPT_FAILED",
                    "the page rejected the trusted keyboard preparation",
                )
            remote = prepared.get("result")
            result = remote.get("value") if isinstance(remote, Mapping) else None
            if not isinstance(result, Mapping) or result.get("ok") is not True:
                raise SmartBrowserCdpError(
                    str(
                        result.get("code")
                        if isinstance(result, Mapping)
                        else "SMART_BROWSER_FILL_FAILED"
                    ),
                    "the selected browser field could not be focused for input",
                )

            select_all = {
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "nativeVirtualKeyCode": 65,
                "modifiers": 2,
            }
            session.command(
                "Input.dispatchKeyEvent",
                {"type": "rawKeyDown", **select_all},
            )
            session.command(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", **select_all},
            )
            backspace = {
                "key": "Backspace",
                "code": "Backspace",
                "windowsVirtualKeyCode": 8,
                "nativeVirtualKeyCode": 8,
            }
            session.command(
                "Input.dispatchKeyEvent",
                {"type": "rawKeyDown", **backspace},
            )
            session.command(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", **backspace},
            )
            session.command("Input.insertText", {"text": next_value})
            tab = {
                "key": "Tab",
                "code": "Tab",
                "windowsVirtualKeyCode": 9,
                "nativeVirtualKeyCode": 9,
            }
            session.command(
                "Input.dispatchKeyEvent",
                {"type": "rawKeyDown", **tab},
            )
            session.command(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", **tab},
            )
            verified = session.command(
                "Runtime.evaluate",
                {
                    "expression": verify_expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            if verified.get("exceptionDetails"):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_PAGE_SCRIPT_FAILED",
                    "the page rejected the trusted keyboard verification",
                )
            remote = verified.get("result")
            verification = (
                remote.get("value") if isinstance(remote, Mapping) else None
            )
            if (
                not isinstance(verification, Mapping)
                or verification.get("ok") is not True
            ):
                raise SmartBrowserCdpError(
                    str(
                        verification.get("code")
                        if isinstance(verification, Mapping)
                        else "SMART_BROWSER_FILL_VERIFICATION_FAILED"
                    ),
                    "the browser field did not retain the supplied text",
                )
        return {
            "ok": True,
            "page_id": target.target_id,
            "ref": str(ref),
            "marker": str(result.get("marker") or ""),
            "tag": str(result.get("tag") or ""),
            "type": str(result.get("type") or ""),
            "value_free": True,
            "trusted_keyboard": True,
        }

    def select(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        option: str,
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        target = self._target(allowed_origins, page_id=page_id)
        expression = self._ref_expression(
            ref,
            f"""
  if (element.tagName.toLowerCase() !== 'select' || element.disabled) {{
    return {{ok:false, code:'SMART_BROWSER_SELECT_INVALID'}};
  }}
  const wanted = {json.dumps(str(option))};
  const choices = Array.from(element.options || []);
  const selected = choices.find((item) => item.value === wanted)
    || choices.find((item) => clean(item.textContent, 300).toLowerCase() === clean(wanted, 300).toLowerCase());
  if (!selected) return {{ok:false, code:'SMART_BROWSER_OPTION_NOT_FOUND'}};
  element.value = selected.value;
  selected.selected = true;
  element.dispatchEvent(new Event('input', {{bubbles:true}}));
  element.dispatchEvent(new Event('change', {{bubbles:true}}));
  return {{ok:true, ref, marker, selected_value:clean(selected.value, 300), selected_label:clean(selected.textContent, 300)}};
""",
        )
        result = self._evaluate(target, expression, user_gesture=True)
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise SmartBrowserCdpError(
                str(
                    result.get("code")
                    if isinstance(result, Mapping)
                    else "SMART_BROWSER_SELECT_FAILED"
                ),
                "the requested browser option could not be selected",
            )
        return {"page_id": target.target_id, **dict(result)}

    def submit_login_form(
        self,
        allowed_origins: Iterable[str],
        password_ref: str,
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        """Submit only the credential form owning one visible password field.

        ScholarOne's legacy login page submits on Enter and exposes no clickable
        submit control.  This trusted-broker-only fallback refuses forms with
        file inputs or final-submission language, so it cannot be repurposed to
        click a manuscript's final submit action.
        """

        target = self._target(allowed_origins, page_id=page_id)
        expression = self._ref_expression(
            password_ref,
            """
  if (type !== 'password' || element.disabled || element.readOnly) {
    return {ok:false, code:'SMART_BROWSER_LOGIN_FORM_INVALID'};
  }
  const form = element.form || element.closest('form');
  if (!form) return {ok:false, code:'SMART_BROWSER_LOGIN_FORM_MISSING'};
  const passwords = Array.from(form.querySelectorAll('input[type="password"]'))
    .filter((item) => !item.disabled);
  const files = Array.from(form.querySelectorAll('input[type="file"]'))
    .filter((item) => !item.disabled);
  const formMarker = clean([
    form.innerText,
    form.getAttribute('name'),
    form.getAttribute('id'),
    form.getAttribute('action')
  ].filter(Boolean).join(' '), 2000).toLowerCase();
  if (passwords.length !== 1 || files.length || /final.?submit|submit.?manuscript|approve.?submission/.test(formMarker)) {
    return {ok:false, code:'SMART_BROWSER_LOGIN_FORM_DENIED'};
  }
  if (typeof form.requestSubmit === 'function') form.requestSubmit();
  else form.submit();
  return {ok:true, ref, marker:'trusted_login_form_submitted'};
""",
        )
        result = self._evaluate(target, expression, user_gesture=True)
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise SmartBrowserCdpError(
                str(
                    result.get("code")
                    if isinstance(result, Mapping)
                    else "SMART_BROWSER_LOGIN_FORM_SUBMIT_FAILED"
                ),
                "the trusted broker could not submit the visible login form",
            )
        return {
            "ok": True,
            "page_id": target.target_id,
            "ref": str(password_ref),
            "value_free": True,
        }

    def click(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        *,
        page_id: str = "",
        allow_final_submit: bool = False,
        before_click: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        target = self._target(allowed_origins, page_id=page_id)
        control = self.inspect_control(
            allowed_origins,
            ref,
            page_id=target.target_id,
        )
        marker = str(control.get("marker") or "")
        visible_label = str(control.get("label") or "")
        control_semantics = visible_label or marker
        final_control = is_final_submit_control(control_semantics)
        if final_control and not allow_final_submit:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_FINAL_SUBMIT_TOOL_REQUIRED",
                "a final submit control can only be activated by the one-use final-submit tool",
                details={"ref": ref},
            )
        if allow_final_submit and not final_control:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_FINAL_CONTROL_NOT_VERIFIED",
                "the selected control is not recognizably the final submit action",
                details={"ref": ref},
            )
        if allow_final_submit:
            # Modern submission portals (including Wiley Authors) may ignore
            # synthetic ``element.click()`` events for the irreversible final
            # action.  Resolve and hit-test the already verified control first,
            # then consume the one-use capability immediately before emitting
            # one real CDP pointer gesture.
            result = self._pointer_click(
                target,
                ref,
                before_click=before_click,
            )
            return {
                "ok": True,
                "page_id": target.target_id,
                "final_submit_control": True,
                **dict(result),
            }
        if before_click is not None:
            before_click()
        expression = self._ref_expression(
            ref,
            """
  if (element.disabled || element.getAttribute('aria-disabled') === 'true') {
    return {ok:false, code:'SMART_BROWSER_CONTROL_DISABLED'};
  }
  element.scrollIntoView({block:'center', inline:'center'});
  element.focus();
  if (role === 'combobox') {
    // React Select's visible container listens for keyboard navigation on
    // its transparent semantic input.  A native click alone does not open
    // the menu, while ArrowDown is the standard accessible interaction.
    element.dispatchEvent(new KeyboardEvent('keydown', {
      key:'ArrowDown',
      code:'ArrowDown',
      keyCode:40,
      which:40,
      bubbles:true
    }));
    return {ok:true, ref, marker, opened_combobox:true};
  }
  element.click();
  return {ok:true, ref, marker};
""",
        )
        result = self._evaluate(target, expression, user_gesture=True)
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise SmartBrowserCdpError(
                str(
                    result.get("code")
                    if isinstance(result, Mapping)
                    else "SMART_BROWSER_CLICK_FAILED"
                ),
                "the selected browser control could not be activated",
            )
        return {
            "ok": True,
            "page_id": target.target_id,
            "final_submit_control": final_control,
            **dict(result),
        }

    def _pointer_click(
        self,
        target: CdpPageTarget,
        ref: str,
        *,
        before_click: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Emit one hit-tested physical pointer gesture on a resolved control."""

        expression = self._ref_expression(
            ref,
            """
  if (element.disabled || element.getAttribute('aria-disabled') === 'true') {
    return {ok:false, code:'SMART_BROWSER_CONTROL_DISABLED'};
  }
  element.scrollIntoView({block:'center', inline:'center'});
  const box = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);
  if (
    box.width <= 1
    || box.height <= 1
    || style.visibility === 'hidden'
    || style.display === 'none'
  ) {
    return {ok:false, code:'SMART_BROWSER_CONTROL_NOT_VISIBLE'};
  }
  const x = Math.max(0, Math.min(window.innerWidth - 1, box.left + box.width / 2));
  const y = Math.max(0, Math.min(window.innerHeight - 1, box.top + box.height / 2));
  const hit = document.elementFromPoint(x, y);
  if (
    !hit
    || !(hit === element || element.contains(hit) || hit.contains(element))
  ) {
    return {ok:false, code:'SMART_BROWSER_CONTROL_OCCLUDED'};
  }
  element.focus();
  return {
    ok:true,
    ref,
    marker,
    x,
    y,
    viewport_width:window.innerWidth,
    viewport_height:window.innerHeight
  };
""",
        )
        with _CdpSocket(
            target.port,
            target.websocket_url,
            timeout_seconds=self.timeout_seconds,
        ) as session:
            session.command("Page.bringToFront")
            evaluated = session.command(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                    "userGesture": True,
                },
            )
            if evaluated.get("exceptionDetails"):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_PAGE_SCRIPT_FAILED",
                    "the page rejected the trusted pointer preparation",
                )
            remote = evaluated.get("result")
            result = remote.get("value") if isinstance(remote, Mapping) else None
            if not isinstance(result, Mapping) or result.get("ok") is not True:
                raise SmartBrowserCdpError(
                    str(
                        result.get("code")
                        if isinstance(result, Mapping)
                        else "SMART_BROWSER_POINTER_CLICK_FAILED"
                    ),
                    "the selected browser control could not be resolved for a pointer click",
                )
            try:
                x = float(result.get("x"))
                y = float(result.get("y"))
                width = float(result.get("viewport_width"))
                height = float(result.get("viewport_height"))
            except (TypeError, ValueError):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_POINTER_CLICK_FAILED",
                    "the selected browser control returned invalid pointer coordinates",
                ) from None
            if not (
                0 <= x < width <= 100_000
                and 0 <= y < height <= 100_000
            ):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_POINTER_CLICK_FAILED",
                    "the selected browser control returned out-of-bounds pointer coordinates",
                )
            session.command(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y},
            )
            if before_click is not None:
                before_click()
            session.command(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 1,
                },
            )
            session.command(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": 0,
                    "clickCount": 1,
                },
            )
        return {
            "ok": True,
            "page_id": target.target_id,
            "ref": str(ref),
            "value_free": True,
            "pointer_click": True,
        }

    def trusted_pointer_click(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        """Physically click one non-final control for the trusted login broker.

        Some identity providers intentionally ignore JavaScript ``element.click()``
        and ``requestSubmit()`` calls.  This bounded fallback resolves the latest
        snapshot ref to a visible, unobscured point and emits a real CDP pointer
        gesture.  Final-submission controls remain unconditionally denied here;
        they can only use the separate one-use final-submit path.
        """

        target = self._target(allowed_origins, page_id=page_id)
        control = self.inspect_control(
            allowed_origins,
            ref,
            page_id=target.target_id,
        )
        marker = str(control.get("marker") or "")
        visible_label = str(control.get("label") or "")
        if is_final_submit_control(visible_label or marker):
            raise SmartBrowserCdpError(
                "SMART_BROWSER_FINAL_SUBMIT_TOOL_REQUIRED",
                "the trusted login pointer cannot activate a final submit control",
                details={"ref": ref},
            )
        return self._pointer_click(target, ref)

    def upload(
        self,
        allowed_origins: Iterable[str],
        ref: str,
        files: Iterable[str],
        *,
        page_id: str = "",
    ) -> dict[str, Any]:
        target = self._target(allowed_origins, page_id=page_id)
        file_list = [str(item) for item in files]
        if not file_list:
            raise SmartBrowserCdpError(
                "SMART_BROWSER_UPLOAD_FILE_REQUIRED",
                "at least one approved upload file is required",
            )
        expression = self._ref_expression(
            ref,
            """
  let fileInput = element;
  const isFileInput = (candidate) => Boolean(
    candidate
    && candidate.tagName
    && candidate.tagName.toLowerCase() === 'input'
    && String(candidate.getAttribute('type') || '').toLowerCase() === 'file'
  );
  if (!isFileInput(fileInput)) {
    // Custom uploaders commonly render a hidden native file input beside a
    // visible Upload button, and a single page may contain many such slots.
    // Resolve from the nearest DOM scope first; a form-wide search is only a
    // safe fallback when it contains exactly one enabled file input.
    let scope = element.parentElement;
    for (let depth = 0; scope && depth < 4; depth += 1) {
      const nearby = Array.from(
        scope.querySelectorAll('input[type="file"]')
      ).filter((candidate) => !candidate.disabled);
      if (nearby.length === 1) {
        fileInput = nearby[0];
        break;
      }
      if (nearby.length > 1) break;
      scope = scope.parentElement;
    }
    if (!isFileInput(fileInput)) {
      const form = element.closest('form');
      const candidates = form
        ? Array.from(form.querySelectorAll('input[type="file"]')).filter(
            (candidate) => !candidate.disabled
          )
        : [];
      if (candidates.length === 1) fileInput = candidates[0];
    }
    if (!isFileInput(fileInput)) {
      return {ok:false, code:'SMART_BROWSER_UPLOAD_CONTROL_INVALID'};
    }
  }
  if (fileInput.disabled) {
    return {ok:false, code:'SMART_BROWSER_UPLOAD_CONTROL_DISABLED'};
  }
  return fileInput;
""",
        )
        with _CdpSocket(
            target.port,
            target.websocket_url,
            timeout_seconds=self.timeout_seconds,
        ) as session:
            evaluated = session.command(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": False,
                    "awaitPromise": True,
                    "userGesture": True,
                },
            )
            remote = evaluated.get("result")
            if (
                not isinstance(remote, Mapping)
                or str(remote.get("subtype") or "") != "node"
                or not str(remote.get("objectId") or "")
            ):
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_UPLOAD_CONTROL_INVALID",
                    "the selected browser control is not a usable file input",
                )
            described = session.command(
                "DOM.describeNode",
                {"objectId": str(remote["objectId"])},
            )
            node = described.get("node")
            backend_node_id = (
                int(node.get("backendNodeId") or 0)
                if isinstance(node, Mapping)
                else 0
            )
            if backend_node_id <= 0:
                raise SmartBrowserCdpError(
                    "SMART_BROWSER_UPLOAD_CONTROL_INVALID",
                    "the selected browser file input could not be resolved",
                )
            session.command(
                "DOM.setFileInputFiles",
                {
                    "files": file_list,
                    "backendNodeId": backend_node_id,
                },
            )
        return {
            "ok": True,
            "page_id": target.target_id,
            "ref": str(ref),
            "file_count": len(file_list),
            "files": file_list,
        }
