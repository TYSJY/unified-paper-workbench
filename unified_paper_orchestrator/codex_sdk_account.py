"""Isolated, secret-free account management for the local Codex SDK runtime.

The service deliberately keeps login challenges in memory.  Browser URLs,
device codes, SDK login IDs, and provider error text are never written by this
module and never appear in status results.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any, Callable, Literal
from uuid import uuid4

from openai_codex import Codex, CodexConfig

from . import config
from .windows_security import restrict_private_path
from .windows_subprocess import configure_openai_codex_no_window
from .codex_runtime import runtime_launch_options


ACCOUNT_STATUS_SCHEMA = "unified-paper-orchestrator/codex-sdk-account-status/v1"
CODEX_SDK_HOME = config.STATE_DIR / "codex_sdk_home"
LOGIN_RECONCILE_TIMEOUT_SECONDS = 180.0
LOGIN_RECONCILE_INTERVAL_SECONDS = 1.0
LOGIN_WAIT_TIMEOUT_SECONDS = 300.0

_PUBLIC_AUTH_MODES = frozenset(
    {"apikey", "chatgpt", "chatgptAuthTokens", "agentIdentity"}
)
_PUBLIC_PLAN_TYPES = frozenset(
    {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "team",
        "self_serve_business_usage_based",
        "business",
        "enterprise_cbp_usage_based",
        "enterprise",
        "edu",
        "unknown",
    }
)
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


class CodexSDKAccountError(RuntimeError):
    """A fixed-message account error that never reflects provider text."""

    def __init__(self, code: str, *, error_type: str = "") -> None:
        self.code = str(code)
        self.error_type = _safe_error_type(error_type)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CodexLoginChallenge:
    """Ephemeral user-facing information needed to complete one login."""

    attempt_id: str
    method: Literal["browser", "device_code"]
    verification_url: str = field(repr=False)
    user_code: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class CodexLoginResult:
    """Secret-free completion result for an in-memory login attempt."""

    attempt_id: str
    method: Literal["browser", "device_code"]
    success: bool
    status: dict[str, Any]
    error_code: str = ""


@dataclass(slots=True)
class _PendingLogin:
    client: Any
    handle: Any
    method: Literal["browser", "device_code"]
    auth_file_state_before: tuple[bool, int, int]
    fallback_auth_file_state_before: tuple[bool, int, int]
    waiting: bool = False
    closed: bool = False
    cancel_code: str = ""
    completed: Event = field(default_factory=Event)


def _safe_error_type(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_ERROR_TYPE.fullmatch(text) else ""


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _safe_public_value(value: Any, allowed: frozenset[str], default: str) -> str:
    text = _enum_value(value)
    return text if text in allowed else default


class CodexSDKAccountService:
    """Manage a Codex account in an application-owned ``CODEX_HOME``.

    Construction is lazy: it neither starts Codex nor starts a login.  Tests
    and callers can inject a ``CodexConfig -> Codex-like client`` factory.
    """

    def __init__(
        self,
        codex_factory: Callable[[CodexConfig], Any] | None = None,
        *,
        codex_home: str | Path | None = None,
        login_reconcile_timeout_seconds: float = LOGIN_RECONCILE_TIMEOUT_SECONDS,
        login_reconcile_interval_seconds: float = LOGIN_RECONCILE_INTERVAL_SECONDS,
        monotonic_fn: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        fallback_auth_path: str | Path | None = None,
        acl_hardener: Callable[[Path | str], None] | None = None,
        login_wait_timeout_seconds: float = LOGIN_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self.codex_home = Path(codex_home or CODEX_SDK_HOME).expanduser().resolve()
        self.fallback_auth_path = Path(
            fallback_auth_path or (Path.home() / ".codex" / "auth.json")
        ).expanduser().resolve()
        self._codex_factory = codex_factory or Codex
        self._login_reconcile_timeout_seconds = max(
            0.0, float(login_reconcile_timeout_seconds)
        )
        self._login_reconcile_interval_seconds = max(
            0.01, float(login_reconcile_interval_seconds)
        )
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._acl_hardener = acl_hardener or restrict_private_path
        self._login_wait_timeout_seconds = max(0.01, float(login_wait_timeout_seconds))
        self._pending: dict[str, _PendingLogin] = {}
        self._lock = Lock()

    def _config(self) -> CodexConfig:
        return CodexConfig(
            cwd=str(config.REPO_ROOT),
            **runtime_launch_options({"CODEX_HOME": str(self.codex_home)}),
            client_name="unified_paper_orchestrator",
            client_title="Unified Paper Orchestrator",
        )

    def _new_client(self) -> Any:
        configure_openai_codex_no_window()
        self.codex_home.mkdir(parents=True, exist_ok=True)
        return self._codex_factory(self._config())

    @staticmethod
    def _file_state(path: Path) -> tuple[bool, int, int]:
        """Return non-secret metadata used only to detect a completed write."""
        try:
            stat = path.stat()
        except OSError:
            return (False, 0, 0)
        return (True, int(stat.st_size), int(stat.st_mtime_ns))

    def _auth_file_state(self) -> tuple[bool, int, int]:
        return self._file_state(self.codex_home / "auth.json")

    def _fallback_auth_file_state(self) -> tuple[bool, int, int]:
        return self._file_state(self.fallback_auth_path)

    def _reconcile_fallback_auth_file(
        self,
        *,
        isolated_before: tuple[bool, int, int],
        fallback_before: tuple[bool, int, int],
    ) -> bool:
        """Repair an SDK callback that wrote to the default Codex home.

        The copy is authorized only by a change observed during this exact
        in-memory login attempt.  Existing desktop credentials are never
        imported merely because they happen to be present.
        """

        target = (self.codex_home / "auth.json").resolve()
        source = self.fallback_auth_path.resolve()
        if source == target:
            return False
        if self._auth_file_state() != isolated_before:
            return False
        fallback_after = self._fallback_auth_file_state()
        if fallback_after == fallback_before or not fallback_after[0]:
            return False
        if source.is_symlink() or not source.is_file():
            raise CodexSDKAccountError(
                "CODEX_LOGIN_DURABLE_AUTH_RECONCILE_FAILED"
            )
        if fallback_after[1] <= 0 or fallback_after[1] > 1024 * 1024:
            raise CodexSDKAccountError(
                "CODEX_LOGIN_DURABLE_AUTH_RECONCILE_FAILED"
            )

        self.codex_home.mkdir(parents=True, exist_ok=True)
        self._acl_hardener(self.codex_home)
        temporary = self.codex_home / f".auth.{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary)
            self._acl_hardener(temporary)
            os.replace(temporary, target)
            self._acl_hardener(target)
        except CodexSDKAccountError:
            raise
        except Exception as exc:
            raise CodexSDKAccountError(
                "CODEX_LOGIN_DURABLE_AUTH_RECONCILE_FAILED",
                error_type=type(exc).__name__,
            ) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return self._auth_file_state() != isolated_before

    @staticmethod
    def _close_client(client: Any) -> None:
        try:
            client.close()
        except Exception:
            # Cleanup failure must not replace the bounded public result with
            # SDK or provider exception text.
            pass

    @staticmethod
    def _status_from_response(response: Any) -> dict[str, Any]:
        account = getattr(response, "account", None)
        root = getattr(account, "root", account) if account is not None else None
        raw_type = getattr(root, "type", "") if root is not None else ""
        auth_mode = _safe_public_value(raw_type, _PUBLIC_AUTH_MODES, "unknown")
        if root is None:
            auth_mode = "none"
        plan_type = _safe_public_value(
            getattr(root, "plan_type", "") if root is not None else "",
            _PUBLIC_PLAN_TYPES,
            "unknown" if root is not None else "none",
        )
        return {
            "schema": ACCOUNT_STATUS_SCHEMA,
            "ok": True,
            "authenticated": root is not None,
            "requires_openai_auth": bool(
                getattr(response, "requires_openai_auth", True)
            ),
            "auth_mode": auth_mode,
            "plan_type": plan_type,
            "isolated_home": True,
        }

    @staticmethod
    def _unavailable_status(exc: Exception) -> dict[str, Any]:
        return {
            "schema": ACCOUNT_STATUS_SCHEMA,
            "ok": False,
            "authenticated": False,
            "requires_openai_auth": None,
            "auth_mode": "unknown",
            "plan_type": "unknown",
            "isolated_home": True,
            "error_code": "CODEX_ACCOUNT_STATUS_UNAVAILABLE",
            "error_type": _safe_error_type(type(exc).__name__),
        }

    def status(self, *, refresh_token: bool = False) -> dict[str, Any]:
        """Read a bounded account summary without exposing tokens.

        The default remains a fast local read for passive UI projection.  Task
        dispatchers call :meth:`verified_status` so an expired ChatGPT refresh
        token cannot masquerade as a usable account merely because ``auth.json``
        still contains account metadata.
        """

        client: Any | None = None
        try:
            client = self._new_client()
            return self._status_from_response(
                client.account(refresh_token=bool(refresh_token))
            )
        except Exception as exc:
            return self._unavailable_status(exc)
        finally:
            if client is not None:
                self._close_client(client)

    def verified_status(self) -> dict[str, Any]:
        """Verify that the isolated account can refresh before model work starts."""

        return self.status(refresh_token=True)

    def _begin_login(
        self, method: Literal["browser", "device_code"]
    ) -> CodexLoginChallenge:
        client: Any | None = None
        try:
            auth_file_state_before = self._auth_file_state()
            fallback_auth_file_state_before = self._fallback_auth_file_state()
            client = self._new_client()
            handle = (
                client.login_chatgpt()
                if method == "browser"
                else client.login_chatgpt_device_code()
            )
            verification_url = str(
                getattr(
                    handle,
                    "auth_url" if method == "browser" else "verification_url",
                    "",
                )
                or ""
            )
            user_code = (
                str(getattr(handle, "user_code", "") or "")
                if method == "device_code"
                else ""
            )
            if not verification_url or (method == "device_code" and not user_code):
                raise CodexSDKAccountError("CODEX_LOGIN_CHALLENGE_INVALID")
            attempt_id = uuid4().hex
            with self._lock:
                self._pending[attempt_id] = _PendingLogin(
                    client,
                    handle,
                    method,
                    auth_file_state_before,
                    fallback_auth_file_state_before,
                )
            client = None  # Ownership moved to the pending attempt.
            return CodexLoginChallenge(
                attempt_id=attempt_id,
                method=method,
                verification_url=verification_url,
                user_code=user_code,
            )
        except CodexSDKAccountError:
            raise
        except Exception as exc:
            raise CodexSDKAccountError(
                "CODEX_LOGIN_START_FAILED", error_type=type(exc).__name__
            ) from None
        finally:
            if client is not None:
                self._close_client(client)

    def begin_browser_login(self) -> CodexLoginChallenge:
        """Start browser login without opening a browser or persisting its URL."""

        return self._begin_login("browser")

    def begin_device_login(self) -> CodexLoginChallenge:
        """Start device-code login without persisting its URL or one-time code."""

        return self._begin_login("device_code")

    def wait_for_login(self, attempt_id: str) -> CodexLoginResult:
        """Wait for one exact in-memory challenge and then close its client."""

        with self._lock:
            pending = self._pending.get(attempt_id)
            if pending is None:
                raise CodexSDKAccountError("CODEX_LOGIN_ATTEMPT_NOT_FOUND")
            if pending.waiting:
                raise CodexSDKAccountError("CODEX_LOGIN_ALREADY_WAITING")
            pending.waiting = True
        outcome: dict[str, Any] = {}

        def receive_notification() -> None:
            try:
                outcome["notification"] = pending.handle.wait()
            except Exception as exc:
                outcome["error"] = exc
            finally:
                pending.completed.set()

        Thread(target=receive_notification, name="codex-login-notification", daemon=True).start()
        try:
            if not pending.completed.wait(self._login_wait_timeout_seconds):
                self.cancel_login(attempt_id, reason="CODEX_LOGIN_TIMED_OUT")
            if pending.cancel_code:
                return self._cancelled_result(attempt_id, pending)
            wait_error = outcome.get("error")
            notification_success = bool(getattr(outcome.get("notification"), "success", False))

            # The app-server completion notification is transport evidence, not
            # the durable account truth.  Always reconcile against account/read
            # and let the usable account state decide the public result.  A
            # changed auth file also authorizes bounded retries: this tolerates
            # a small local-clock skew without making genuine failures wait.
            auth_file_changed = (
                self._auth_file_state() != pending.auth_file_state_before
            )
            if not auth_file_changed:
                auth_file_changed = self._reconcile_fallback_auth_file(
                    isolated_before=pending.auth_file_state_before,
                    fallback_before=pending.fallback_auth_file_state_before,
                )
            should_retry = notification_success or auth_file_changed
            deadline = self._monotonic() + (
                self._login_reconcile_timeout_seconds if should_retry else 0.0
            )
            status = {
                "schema": ACCOUNT_STATUS_SCHEMA,
                "ok": True,
                "authenticated": False,
                "requires_openai_auth": True,
                "auth_mode": "none",
                "plan_type": "none",
                "isolated_home": True,
            }
            while True:
                if pending.cancel_code:
                    return self._cancelled_result(attempt_id, pending)
                try:
                    status = self._status_from_response(
                        pending.client.account(refresh_token=False)
                    )
                except Exception:
                    if not should_retry:
                        raise
                else:
                    if pending.cancel_code:
                        return self._cancelled_result(attempt_id, pending)
                    if status.get("authenticated") is True:
                        return CodexLoginResult(
                            attempt_id=str(attempt_id),
                            method=pending.method,
                            success=True,
                            status=status,
                        )
                    if not should_retry:
                        break

                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                self._sleep(
                    min(self._login_reconcile_interval_seconds, remaining)
                )

            if wait_error is not None:
                raise wait_error
            return CodexLoginResult(
                attempt_id=str(attempt_id),
                method=pending.method,
                success=False,
                status=status,
                error_code="CODEX_LOGIN_NOT_COMPLETED",
            )
        except Exception as exc:
            if pending.cancel_code:
                return self._cancelled_result(attempt_id, pending)
            raise CodexSDKAccountError(
                "CODEX_LOGIN_WAIT_FAILED", error_type=type(exc).__name__
            ) from None
        finally:
            with self._lock:
                self._pending.pop(attempt_id, None)
            self._close_pending(pending)

    def _close_pending(self, pending: _PendingLogin) -> None:
        with self._lock:
            if pending.closed:
                return
            pending.closed = True
        self._close_client(pending.client)

    @staticmethod
    def _cancelled_result(attempt_id: str, pending: _PendingLogin) -> CodexLoginResult:
        return CodexLoginResult(attempt_id, pending.method, False, {
            "schema": ACCOUNT_STATUS_SCHEMA, "ok": True, "authenticated": False,
            "requires_openai_auth": True, "auth_mode": "none", "plan_type": "none",
            "isolated_home": True,
        }, pending.cancel_code)

    def cancel_login(self, attempt_id: str, *, reason: str = "CODEX_LOGIN_CANCELLED") -> bool:
        """Cancel one exact in-memory login attempt and release its client."""

        with self._lock:
            pending = self._pending.get(attempt_id)
            if pending is None:
                return False
            if pending.cancel_code:
                return True
            pending.cancel_code = ("CODEX_LOGIN_TIMED_OUT" if reason == "CODEX_LOGIN_TIMED_OUT"
                                   else "CODEX_LOGIN_CANCELLED")
            if not pending.waiting:
                self._pending.pop(attempt_id, None)
        try:
            pending.handle.cancel()
            return True
        except Exception as exc:
            raise CodexSDKAccountError(
                "CODEX_LOGIN_CANCEL_FAILED", error_type=type(exc).__name__
            ) from None
        finally:
            self._close_pending(pending)
            pending.completed.set()

    def logout(self) -> dict[str, Any]:
        """Clear only the account stored in this service's isolated home."""

        client: Any | None = None
        try:
            client = self._new_client()
            client.logout()
            return {
                "schema": ACCOUNT_STATUS_SCHEMA,
                "ok": True,
                "authenticated": False,
                "requires_openai_auth": True,
                "auth_mode": "none",
                "plan_type": "none",
                "isolated_home": True,
            }
        except Exception as exc:
            raise CodexSDKAccountError(
                "CODEX_LOGOUT_FAILED", error_type=type(exc).__name__
            ) from None
        finally:
            if client is not None:
                self._close_client(client)

    def close(self) -> None:
        """Cancel and close any abandoned in-memory login attempts."""

        with self._lock:
            attempts = list(self._pending)
        for attempt_id in attempts:
            try:
                self.cancel_login(attempt_id)
            except Exception:
                pass

    def __enter__(self) -> "CodexSDKAccountService":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


__all__ = [
    "ACCOUNT_STATUS_SCHEMA",
    "CODEX_SDK_HOME",
    "CodexLoginChallenge",
    "CodexLoginResult",
    "CodexSDKAccountError",
    "CodexSDKAccountService",
]
