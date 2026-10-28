"""Loopback HTTP adapter for the deterministic control-plane service."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .service import ControlPlaneService, ServiceError


MAX_REQUEST_BYTES = 1024 * 1024


class ControlPlaneHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ControlPlaneService) -> None:
        super().__init__(address, ControlPlaneRequestHandler)
        self.service = service


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # The development server deliberately avoids request bodies, e-mail
        # addresses and bearer tokens in its default console output.
        return

    def do_GET(self) -> None:
        if self._origin_is_rejected():
            self._respond(HTTPStatus.FORBIDDEN, {"error": "origin not allowed"})
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                self._respond(HTTPStatus.OK, {"status": "ok"})
                return
            if parsed.path == "/policies/catalog":
                self._respond(HTTPStatus.OK, self.server.service.policy_catalog())
                return
            if parsed.path == "/account/me":
                self._respond(
                    HTTPStatus.OK,
                    self.server.service.account_summary(self._bearer_token()),
                )
                return
            if parsed.path == "/releases/latest":
                query = parse_qs(parsed.query)
                channel = str(query.get("channel", ["stable"])[0])
                self._respond(HTTPStatus.OK, self.server.service.latest_release(channel))
                return
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ServiceError as exc:
            self._respond(exc.status_code, {"error": str(exc)})
        except Exception:
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def do_OPTIONS(self) -> None:
        origin = self._allowed_origin()
        if not origin:
            self._respond(HTTPStatus.FORBIDDEN, {"error": "origin not allowed"})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "false")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self._origin_is_rejected():
            self._respond(HTTPStatus.FORBIDDEN, {"error": "origin not allowed"})
            return
        parsed = urlparse(self.path)
        try:
            body = self._json_body()
            token = self._bearer_token()
            if parsed.path == "/auth/request-code":
                result = self.server.service.request_login_code(str(body.get("email") or ""))
            elif parsed.path == "/auth/verify-code":
                result = self.server.service.verify_login_code(
                    str(body.get("email") or ""),
                    str(body.get("code") or ""),
                )
            elif parsed.path == "/auth/logout":
                result = self.server.service.logout(token)
            elif parsed.path == "/licenses/activate":
                result = self.server.service.activate_license(token, body.get("device"))
            elif parsed.path == "/licenses/refresh":
                result = self.server.service.refresh_license(
                    token,
                    str(body.get("device_id") or ""),
                )
            elif parsed.path == "/devices/revoke":
                result = self.server.service.revoke_device(
                    token,
                    str(body.get("device_id") or ""),
                )
            elif parsed.path == "/diagnostics/report":
                result = self.server.service.report_diagnostic(token, body.get("event"))
            else:
                self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._respond(HTTPStatus.OK, result)
        except ServiceError as exc:
            self._respond(exc.status_code, {"error": str(exc)})
        except ValueError as exc:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def _json_body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if size <= 0 or size > MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        raw = self.rfile.read(size)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _bearer_token(self) -> str:
        raw = str(self.headers.get("Authorization") or "")
        prefix = "Bearer "
        return raw[len(prefix) :].strip() if raw.startswith(prefix) else ""

    def _respond(self, status: int | HTTPStatus, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        allowed_origin = self._allowed_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _allowed_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return ""
        allowed = self.server.service.config.allowed_browser_origins
        return origin if origin in allowed else ""

    def _origin_is_rejected(self) -> bool:
        origin = str(self.headers.get("Origin") or "").strip()
        return bool(origin and not self._allowed_origin())
