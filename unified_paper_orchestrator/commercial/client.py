"""Small HTTPS JSON client for the commercial control plane."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .contracts import ContractError, SignedEnvelope


class ControlPlaneError(RuntimeError):
    """A value-free control-plane failure suitable for UI display."""


@dataclass(frozen=True)
class AuthSession:
    user_id: str
    bearer_token: str
    plan: str
    subscription_status: str = "active"
    subscription_expires_at: str = ""


class ControlPlaneClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        parsed = urlparse(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ContractError("Control-plane URL must use HTTPS except for loopback development.")
        if not parsed.netloc:
            raise ContractError("Control-plane URL is invalid.")

    def request_login_code(self, email: str) -> dict[str, Any]:
        return self._request("POST", "/auth/request-code", {"email": email})

    def verify_login_code(self, email: str, code: str) -> AuthSession:
        data = self._request("POST", "/auth/verify-code", {"email": email, "code": code})
        try:
            return AuthSession(
                user_id=str(data["user_id"]),
                bearer_token=str(data["bearer_token"]),
                plan=str(data["plan"]),
                subscription_status=str(
                    data.get("subscription_status") or "active"
                ),
                subscription_expires_at=str(
                    data.get("subscription_expires_at") or ""
                ),
            )
        except (KeyError, TypeError) as exc:
            raise ControlPlaneError("Login response was incomplete.") from exc

    def activate(self, *, bearer_token: str, device: dict[str, Any]) -> SignedEnvelope:
        data = self._request(
            "POST",
            "/licenses/activate",
            {"device": device},
            bearer_token=bearer_token,
        )
        return SignedEnvelope.from_dict(data["license"])

    def refresh(self, *, bearer_token: str, device_id: str) -> SignedEnvelope:
        data = self._request(
            "POST",
            "/licenses/refresh",
            {"device_id": device_id},
            bearer_token=bearer_token,
        )
        return SignedEnvelope.from_dict(data["license"])

    def account_summary(self, *, bearer_token: str) -> dict[str, Any]:
        return self._request("GET", "/account/me", bearer_token=bearer_token)

    def revoke_device(self, *, bearer_token: str, device_id: str) -> bool:
        data = self._request(
            "POST",
            "/devices/revoke",
            {"device_id": device_id},
            bearer_token=bearer_token,
        )
        return bool(data.get("revoked"))

    def logout(self, *, bearer_token: str) -> bool:
        data = self._request(
            "POST",
            "/auth/logout",
            {},
            bearer_token=bearer_token,
        )
        return bool(data.get("revoked"))

    def latest_release(self, *, channel: str, current_version: str) -> SignedEnvelope | None:
        path = f"/releases/latest?channel={channel}&current_version={current_version}"
        data = self._request("GET", path)
        raw = data.get("release")
        return SignedEnvelope.from_dict(raw) if raw else None

    def submit_diagnostic(self, *, bearer_token: str, event: dict[str, Any]) -> str:
        data = self._request(
            "POST",
            "/diagnostics/report",
            {"event": event},
            bearer_token=bearer_token,
        )
        return str(data.get("diagnostic_id") or "")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        bearer_token: str = "",
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "UnifiedPaperWorkbench/0.12"}
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "request rejected")
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                detail = "request rejected"
            raise ControlPlaneError(f"Control-plane request failed ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ControlPlaneError("Control plane is currently unreachable.") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("Control-plane response was not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ControlPlaneError("Control-plane response must be an object.")
        return parsed
