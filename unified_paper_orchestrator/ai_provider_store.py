"""Persistent AI provider profiles and current-user protected API keys.

This module deliberately contains no HTTP client.  Capability probes are run by
callers; this store only records their result.
"""

from __future__ import annotations

import ipaddress
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .config import STATE_DIR
from .credential_vault import protect_text, unprotect_text
from .io_utils import FileLock, JsonReadError, now_iso, read_json, write_json_atomic


STORE_VERSION = 1
LEGACY_CODEX = "legacy_codex"
API_KEY = "api_key"
EXECUTION_MODES = frozenset({LEGACY_CODEX, API_KEY})
REASONING_EFFORTS = frozenset({"", "minimal", "low", "medium", "high", "xhigh"})
WIRE_APIS = frozenset({"responses"})
AUTH_SCHEMES = frozenset({"bearer"})
DEFAULT_CAPABILITY_STATUS = "untested"
DPAPI_SCHEME = "win32-dpapi-current-user"

_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "revision",
        "display_name",
        "base_url",
        "wire_api",
        "auth_scheme",
        "default_model",
        "reasoning_effort",
        "proxy_url",
        "custom_headers",
        "capability_status",
        "last_tested_at",
    }
)
_EDITABLE_PROFILE_FIELDS = _PROFILE_FIELDS - {"profile_id", "revision"}
_CAPABILITY_INPUT_FIELDS = frozenset(
    {"base_url", "wire_api", "auth_scheme", "default_model", "reasoning_effort", "proxy_url", "custom_headers"}
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_STATUS = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE_HEADER_PARTS = frozenset({"api", "auth", "authorization", "credential", "key", "password", "secret", "token"})
_FORBIDDEN_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "set-cookie",
        "transfer-encoding",
    }
)


class AIProviderStoreError(RuntimeError):
    """Base class for value-free provider-store failures."""


class ProviderValidationError(AIProviderStoreError, ValueError):
    pass


class ProviderNotFoundError(AIProviderStoreError, KeyError):
    pass


class RevisionConflictError(AIProviderStoreError):
    pass


class ActiveProfileError(AIProviderStoreError):
    pass


class ProviderSecretError(AIProviderStoreError):
    pass


class ProviderStoreBusyError(AIProviderStoreError):
    pass


class ExecutionModeMismatchError(AIProviderStoreError):
    code = "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"


class TextCipher(Protocol):
    scheme: str

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, ciphertext: str) -> str: ...


class DpapiTextCipher:
    scheme = DPAPI_SCHEME

    def encrypt(self, plaintext: str) -> str:
        return protect_text(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        return unprotect_text(ciphertext)


class _CallableTextCipher:
    def __init__(self, protector: Callable[[str], str], unprotector: Callable[[str], str]) -> None:
        self.scheme = "injected-test-cipher"
        self._protector = protector
        self._unprotector = unprotector

    def encrypt(self, plaintext: str) -> str:
        return self._protector(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        return self._unprotector(ciphertext)


def _clean_text(value: Any, field_name: str, *, required: bool, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ProviderValidationError(f"{field_name} must be a string.")
    clean = value.strip()
    if required and not clean:
        raise ProviderValidationError(f"{field_name} is required.")
    if len(clean) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in clean):
        raise ProviderValidationError(f"{field_name} is invalid.")
    return clean


def _loopback_host(host: str) -> bool:
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.rstrip(".")).is_loopback
    except ValueError:
        return False


def normalize_provider_url(value: Any, *, field_name: str = "base_url", allow_empty: bool = False) -> str:
    """Validate and canonicalize an HTTPS (or explicit loopback HTTP) URL.

    The path is preserved, apart from redundant trailing slashes.  In
    particular, this function never appends or guesses an API prefix such as
    ``/v1``.
    """

    clean = _clean_text(value, field_name, required=not allow_empty, maximum=2048)
    if not clean:
        return ""
    if "\\" in clean or "?" in clean or "#" in clean:
        raise ProviderValidationError(f"{field_name} must not contain a query, fragment, or backslash.")
    try:
        parsed = urlsplit(clean)
        port = parsed.port
    except ValueError:
        raise ProviderValidationError(f"{field_name} is not a valid URL.") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        raise ProviderValidationError(f"{field_name} must use HTTPS.")
    if not parsed.netloc or not parsed.hostname:
        raise ProviderValidationError(f"{field_name} must include a host.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ProviderValidationError(f"{field_name} must not contain user information.")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderValidationError(f"{field_name} has an invalid port.")
    authority = parsed.netloc
    if authority.endswith(":"):
        raise ProviderValidationError(f"{field_name} has an invalid port.")
    host = parsed.hostname.rstrip(".").lower()
    if not host or "%" in host:
        raise ProviderValidationError(f"{field_name} has an invalid host.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ProviderValidationError(f"{field_name} has an invalid host.") from None
        labels = host.split(".")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
            raise ProviderValidationError(f"{field_name} has an invalid host.")
        if any(re.fullmatch(r"[a-z0-9-]+", label) is None for label in labels):
            raise ProviderValidationError(f"{field_name} has an invalid host.")
        ip = None
    if scheme == "http" and not _loopback_host(host):
        raise ProviderValidationError(f"{field_name} must use HTTPS unless its host is explicit loopback.")
    if ip is not None and ip.version == 6:
        rendered_host = f"[{ip.compressed}]"
    else:
        rendered_host = ip.compressed if ip is not None else host
    if port is not None and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        rendered_host = f"{rendered_host}:{port}"
    path = parsed.path.rstrip("/")
    if path and not path.startswith("/"):
        raise ProviderValidationError(f"{field_name} has an invalid path.")
    return urlunsplit((scheme, rendered_host, path, "", ""))


normalize_base_url = normalize_provider_url


def _normalize_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProviderValidationError("custom_headers must be an object.")
    result: dict[str, str] = {}
    observed: set[str] = set()
    for raw_name, raw_value in value.items():
        name = _clean_text(raw_name, "custom header name", required=True, maximum=128)
        header_value = _clean_text(raw_value, "custom header value", required=False, maximum=4096)
        lowered = name.lower()
        parts = frozenset(part for part in re.split(r"[^a-z0-9]+", lowered) if part)
        if (
            _HEADER_NAME.fullmatch(name) is None
            or lowered in observed
            or lowered in _FORBIDDEN_HEADERS
            or parts & _SENSITIVE_HEADER_PARTS
        ):
            raise ProviderValidationError("custom_headers may contain only unique, non-sensitive headers.")
        observed.add(lowered)
        result[name] = header_value
    return result


def _normalize_timestamp(value: Any) -> str:
    if value in {None, ""}:
        return ""
    clean = _clean_text(value, "last_tested_at", required=True, maximum=64)
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        raise ProviderValidationError("last_tested_at must be an ISO-8601 timestamp.") from None
    if parsed.tzinfo is None:
        raise ProviderValidationError("last_tested_at must include a timezone.")
    return parsed.replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str = field(default_factory=lambda: f"provider-{uuid4().hex}")
    revision: int = 1
    display_name: str = ""
    base_url: str = ""
    wire_api: str = "responses"
    auth_scheme: str = "bearer"
    default_model: str = ""
    reasoning_effort: str = ""
    proxy_url: str = ""
    custom_headers: dict[str, str] = field(default_factory=dict)
    capability_status: str = DEFAULT_CAPABILITY_STATUS
    last_tested_at: str = ""

    def __post_init__(self) -> None:
        profile_id = _clean_text(self.profile_id, "profile_id", required=True, maximum=128)
        if _PROFILE_ID.fullmatch(profile_id) is None:
            raise ProviderValidationError("profile_id is invalid.")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ProviderValidationError("revision must be a positive integer.")
        wire_api = _clean_text(self.wire_api, "wire_api", required=True, maximum=32).lower()
        auth_scheme = _clean_text(self.auth_scheme, "auth_scheme", required=True, maximum=32).lower()
        reasoning_effort = _clean_text(self.reasoning_effort, "reasoning_effort", required=False, maximum=16).lower()
        if wire_api not in WIRE_APIS:
            raise ProviderValidationError("wire_api must be responses.")
        if auth_scheme not in AUTH_SCHEMES:
            raise ProviderValidationError("auth_scheme must be bearer.")
        if reasoning_effort not in REASONING_EFFORTS:
            raise ProviderValidationError("reasoning_effort is invalid.")
        capability_status = _clean_text(
            self.capability_status, "capability_status", required=True, maximum=64
        ).lower()
        if _STATUS.fullmatch(capability_status) is None:
            raise ProviderValidationError("capability_status is invalid.")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "display_name", _clean_text(self.display_name, "display_name", required=True))
        object.__setattr__(self, "base_url", normalize_provider_url(self.base_url))
        object.__setattr__(self, "wire_api", wire_api)
        object.__setattr__(self, "auth_scheme", auth_scheme)
        object.__setattr__(self, "default_model", _clean_text(self.default_model, "default_model", required=True))
        object.__setattr__(self, "reasoning_effort", reasoning_effort)
        object.__setattr__(self, "proxy_url", normalize_provider_url(self.proxy_url, field_name="proxy_url", allow_empty=True))
        object.__setattr__(self, "custom_headers", _normalize_headers(self.custom_headers))
        object.__setattr__(self, "capability_status", capability_status)
        object.__setattr__(self, "last_tested_at", _normalize_timestamp(self.last_tested_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "auth_scheme": self.auth_scheme,
            "default_model": self.default_model,
            "reasoning_effort": self.reasoning_effort,
            "proxy_url": self.proxy_url,
            "custom_headers": dict(self.custom_headers),
            "capability_status": self.capability_status,
            "last_tested_at": self.last_tested_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProviderProfile":
        if not isinstance(payload, Mapping) or set(payload) != _PROFILE_FIELDS:
            raise ProviderValidationError("Provider profile structure is invalid.")
        return cls(**{key: payload[key] for key in _PROFILE_FIELDS})


@dataclass(frozen=True)
class AIProviderSettings:
    revision: int = 0
    execution_mode: str = LEGACY_CODEX
    active_profile_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ProviderValidationError("settings revision must be a non-negative integer.")
        mode = _clean_text(self.execution_mode, "execution_mode", required=True, maximum=32).lower()
        if mode not in EXECUTION_MODES:
            raise ProviderValidationError("execution_mode must be legacy_codex or api_key.")
        active = self.active_profile_id
        if active is not None:
            active = _clean_text(active, "active_profile_id", required=True, maximum=128)
            if _PROFILE_ID.fullmatch(active) is None:
                raise ProviderValidationError("active_profile_id is invalid.")
        object.__setattr__(self, "execution_mode", mode)
        object.__setattr__(self, "active_profile_id", active)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "execution_mode": self.execution_mode,
            "active_profile_id": self.active_profile_id,
        }


class AIProviderStore:
    """Atomic JSON store for provider settings, profiles, and encrypted keys."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        settings_path: Path | str | None = None,
        profiles_path: Path | str | None = None,
        secrets_path: Path | str | None = None,
        lock_path: Path | str | None = None,
        cipher: TextCipher | None = None,
        protector: Callable[[str], str] | None = None,
        unprotector: Callable[[str], str] | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        base = Path(root) if root is not None else STATE_DIR / "ai_providers"
        self.settings_path = Path(settings_path) if settings_path is not None else base / "settings.json"
        self.profiles_path = Path(profiles_path) if profiles_path is not None else base / "profiles.json"
        self.secrets_path = Path(secrets_path) if secrets_path is not None else base / "secrets.local.json"
        self.lock_path = Path(lock_path) if lock_path is not None else base / "store.lock"
        if cipher is not None and (protector is not None or unprotector is not None):
            raise ValueError("Pass either cipher or protector/unprotector, not both.")
        if (protector is None) != (unprotector is None):
            raise ValueError("protector and unprotector must be supplied together.")
        self._cipher: TextCipher = cipher or (
            _CallableTextCipher(protector, unprotector) if protector is not None and unprotector is not None else DpapiTextCipher()
        )
        self.lock_timeout_seconds = max(0.0, float(lock_timeout_seconds))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            lock = FileLock(self.lock_path, stale_seconds=5 * 60)
            try:
                lock.__enter__()
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise ProviderStoreBusyError("AI provider settings are busy; retry the operation.") from None
                time.sleep(0.025)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    @staticmethod
    def _read_document(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            data = read_json(path, default)
        except JsonReadError:
            raise AIProviderStoreError("AI provider state is unreadable; no changes were made.") from None
        if not isinstance(data, dict):
            raise AIProviderStoreError("AI provider state has an invalid structure.")
        return data

    def _read_profiles(self) -> tuple[int, list[ProviderProfile]]:
        data = self._read_document(
            self.profiles_path,
            {"version": STORE_VERSION, "revision": 0, "updated_at": "", "profiles": []},
        )
        if data.get("version") != STORE_VERSION or not isinstance(data.get("revision"), int) or not isinstance(data.get("profiles"), list):
            raise AIProviderStoreError("AI provider profiles have an invalid structure.")
        try:
            profiles = [ProviderProfile.from_dict(item) for item in data["profiles"]]
        except (ProviderValidationError, TypeError):
            raise AIProviderStoreError("AI provider profiles have an invalid structure.") from None
        ids = [item.profile_id for item in profiles]
        if len(ids) != len(set(ids)):
            raise AIProviderStoreError("AI provider profiles contain duplicate identifiers.")
        return data["revision"], profiles

    def _write_profiles(self, document_revision: int, profiles: list[ProviderProfile]) -> None:
        write_json_atomic(
            self.profiles_path,
            {
                "version": STORE_VERSION,
                "revision": document_revision + 1,
                "updated_at": now_iso(),
                "profiles": [profile.to_dict() for profile in profiles],
            },
        )

    def _read_settings(self) -> AIProviderSettings:
        data = self._read_document(
            self.settings_path,
            {"version": STORE_VERSION, "revision": 0, "execution_mode": LEGACY_CODEX, "active_profile_id": None},
        )
        if data.get("version") != STORE_VERSION or set(data) - {
            "version", "revision", "execution_mode", "active_profile_id", "updated_at"
        }:
            raise AIProviderStoreError("AI provider settings have an invalid structure.")
        try:
            return AIProviderSettings(
                revision=data.get("revision"),
                execution_mode=data.get("execution_mode"),
                active_profile_id=data.get("active_profile_id"),
            )
        except ProviderValidationError:
            raise AIProviderStoreError("AI provider settings have an invalid structure.") from None

    def _write_settings(self, settings: AIProviderSettings) -> None:
        write_json_atomic(
            self.settings_path,
            {"version": STORE_VERSION, "updated_at": now_iso(), **settings.to_dict()},
        )

    @staticmethod
    def _expect_revision(actual: int, expected: int, record_type: str) -> None:
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != actual:
            raise RevisionConflictError(f"{record_type} changed since it was read; refresh and retry.")

    def get_settings(self) -> AIProviderSettings:
        with self._locked():
            return self._read_settings()

    load_settings = get_settings

    @contextmanager
    def hold_execution_mode(self, required_mode: str) -> Iterator[AIProviderSettings]:
        """Hold the provider-store lock while a mode-owned side effect occurs.

        The lease closes the race where a legacy scheduler could read V1 mode,
        lose the lock, and become ACTIVE after another process committed API
        mode.  Callers must keep the actual ownership-changing write inside the
        context.
        """

        normalized = _clean_text(
            required_mode,
            "execution_mode",
            required=True,
            maximum=32,
        ).lower()
        if normalized not in EXECUTION_MODES:
            raise ProviderValidationError("execution_mode is not supported.")
        with self._locked():
            current = self._read_settings()
            if current.execution_mode != normalized:
                raise ExecutionModeMismatchError(
                    "当前 AI 执行模式不允许启动 V1 Codex 调度器；不会自动回退。"
                )
            yield current

    def set_execution_mode_guarded(
        self,
        execution_mode: str,
        *,
        expected_revision: int,
        guard: Callable[[AIProviderSettings, str, ProviderProfile | None, bool], None],
    ) -> AIProviderSettings:
        """Validate external execution state and commit under one shared lock."""

        with self._locked():
            current = self._read_settings()
            self._expect_revision(current.revision, expected_revision, "AI provider settings")
            normalized = _clean_text(
                execution_mode,
                "execution_mode",
                required=True,
                maximum=32,
            ).lower()
            if normalized not in EXECUTION_MODES:
                raise ProviderValidationError("execution_mode is not supported.")
            active_profile: ProviderProfile | None = None
            has_key = False
            if current.active_profile_id:
                _document_revision, profiles = self._read_profiles()
                active_profile = next(
                    (
                        profile
                        for profile in profiles
                        if profile.profile_id == current.active_profile_id
                    ),
                    None,
                )
                _secret_revision, secrets = self._read_secrets()
                has_key = current.active_profile_id in secrets
            guard(current, normalized, active_profile, has_key)
            if current.execution_mode == normalized:
                return current
            return self._set_execution_mode_unlocked(current, normalized)

    def set_execution_mode(self, execution_mode: str, *, expected_revision: int) -> AIProviderSettings:
        with self._locked():
            current = self._read_settings()
            self._expect_revision(current.revision, expected_revision, "AI provider settings")
            normalized_mode = _clean_text(
                execution_mode,
                "execution_mode",
                required=True,
                maximum=32,
            ).lower()
            return self._set_execution_mode_unlocked(current, normalized_mode)

    def _set_execution_mode_unlocked(
        self,
        current: AIProviderSettings,
        normalized_mode: str,
    ) -> AIProviderSettings:
        if normalized_mode == API_KEY:
            active_profile_id = str(current.active_profile_id or "").strip()
            if not active_profile_id:
                raise ActiveProfileError(
                    "API key mode requires an active provider profile."
                )
            _document_revision, profiles = self._read_profiles()
            active_profile = next(
                (
                    profile
                    for profile in profiles
                    if profile.profile_id == active_profile_id
                ),
                None,
            )
            if active_profile is None:
                raise ActiveProfileError(
                    "The active AI provider profile no longer exists."
                )
            if active_profile.capability_status != "compatible":
                raise ActiveProfileError(
                    "The active AI provider profile has not passed compatibility testing."
                )
            _secret_revision, secrets = self._read_secrets()
            if active_profile_id not in secrets:
                raise ProviderSecretError(
                    "The active AI provider profile has no stored API key."
                )
        updated = AIProviderSettings(
            current.revision + 1,
            normalized_mode,
            current.active_profile_id,
        )
        self._write_settings(updated)
        return updated

    def set_active_profile(self, profile_id: str | None, *, expected_revision: int) -> AIProviderSettings:
        with self._locked():
            current = self._read_settings()
            self._expect_revision(current.revision, expected_revision, "AI provider settings")
            if profile_id is not None:
                _document_revision, profiles = self._read_profiles()
                if not any(profile.profile_id == profile_id for profile in profiles):
                    raise ProviderNotFoundError("AI provider profile does not exist.")
            updated = AIProviderSettings(current.revision + 1, current.execution_mode, profile_id)
            self._write_settings(updated)
            return updated

    select_active_profile = set_active_profile

    def list_profiles(self) -> list[ProviderProfile]:
        with self._locked():
            _revision, profiles = self._read_profiles()
            return profiles

    def get_profile(self, profile_id: str) -> ProviderProfile:
        with self._locked():
            _revision, profiles = self._read_profiles()
            for profile in profiles:
                if profile.profile_id == profile_id:
                    return profile
        raise ProviderNotFoundError("AI provider profile does not exist.")

    def create_profile(self, profile: ProviderProfile | Mapping[str, Any] | None = None, **fields: Any) -> ProviderProfile:
        if profile is not None and fields:
            raise ProviderValidationError("Pass either a profile or profile fields, not both.")
        if profile is None:
            payload = dict(fields)
        elif isinstance(profile, ProviderProfile):
            payload = profile.to_dict()
        elif isinstance(profile, Mapping):
            payload = dict(profile)
        else:
            raise ProviderValidationError("profile is invalid.")
        if set(payload) - _PROFILE_FIELDS:
            raise ProviderValidationError("Provider profile contains unsupported fields.")
        payload.pop("revision", None)
        payload.setdefault("profile_id", f"provider-{uuid4().hex}")
        candidate = ProviderProfile(revision=1, **payload)
        with self._locked():
            document_revision, profiles = self._read_profiles()
            if any(item.profile_id == candidate.profile_id for item in profiles):
                raise ProviderValidationError("profile_id already exists.")
            profiles.append(candidate)
            self._write_profiles(document_revision, profiles)
        return candidate

    add_profile = create_profile

    def update_profile(self, profile_id: str, *, expected_revision: int, **changes: Any) -> ProviderProfile:
        unknown = set(changes) - _EDITABLE_PROFILE_FIELDS
        if unknown:
            raise ProviderValidationError("Profile update contains unsupported fields.")
        with self._locked():
            document_revision, profiles = self._read_profiles()
            index = next((index for index, item in enumerate(profiles) if item.profile_id == profile_id), None)
            if index is None:
                raise ProviderNotFoundError("AI provider profile does not exist.")
            current = profiles[index]
            self._expect_revision(current.revision, expected_revision, "AI provider profile")
            payload = current.to_dict()
            payload.update(changes)
            payload["revision"] = current.revision + 1
            if set(changes) & _CAPABILITY_INPUT_FIELDS and not ({"capability_status", "last_tested_at"} & set(changes)):
                payload["capability_status"] = DEFAULT_CAPABILITY_STATUS
                payload["last_tested_at"] = ""
            updated = ProviderProfile.from_dict(payload)
            profiles[index] = updated
            self._write_profiles(document_revision, profiles)
            return updated

    edit_profile = update_profile

    def record_capability_result(
        self,
        profile_id: str,
        capability_status: str,
        *,
        expected_revision: int,
        tested_at: str | None = None,
    ) -> ProviderProfile:
        return self.update_profile(
            profile_id,
            expected_revision=expected_revision,
            capability_status=capability_status,
            last_tested_at=tested_at or now_iso(),
        )

    save_capability_result = record_capability_result

    def delete_profile(self, profile_id: str, *, expected_revision: int) -> None:
        with self._locked():
            document_revision, profiles = self._read_profiles()
            index = next((index for index, item in enumerate(profiles) if item.profile_id == profile_id), None)
            if index is None:
                raise ProviderNotFoundError("AI provider profile does not exist.")
            self._expect_revision(profiles[index].revision, expected_revision, "AI provider profile")
            if self._read_settings().active_profile_id == profile_id:
                raise ActiveProfileError("The active AI provider profile cannot be deleted.")
            secret_revision, secrets = self._read_secrets()
            if profile_id in secrets:
                del secrets[profile_id]
                self._write_secrets(secret_revision, secrets)
            del profiles[index]
            self._write_profiles(document_revision, profiles)

    def _read_secrets(self) -> tuple[int, dict[str, dict[str, str]]]:
        data = self._read_document(
            self.secrets_path,
            {"version": STORE_VERSION, "revision": 0, "encryption": DPAPI_SCHEME, "updated_at": "", "secrets": {}},
        )
        secrets = data.get("secrets")
        if data.get("version") != STORE_VERSION or not isinstance(data.get("revision"), int) or not isinstance(secrets, dict):
            raise ProviderSecretError("AI provider secret storage is invalid.")
        result: dict[str, dict[str, str]] = {}
        for profile_id, record in secrets.items():
            if (
                not isinstance(profile_id, str)
                or _PROFILE_ID.fullmatch(profile_id) is None
                or not isinstance(record, dict)
                or set(record) != {"ciphertext", "scheme", "updated_at"}
                or not isinstance(record.get("ciphertext"), str)
                or not record["ciphertext"]
                or not isinstance(record.get("scheme"), str)
                or not isinstance(record.get("updated_at"), str)
            ):
                raise ProviderSecretError("AI provider secret storage is invalid.")
            result[profile_id] = dict(record)
        return data["revision"], result

    def _write_secrets(self, document_revision: int, secrets: dict[str, dict[str, str]]) -> None:
        write_json_atomic(
            self.secrets_path,
            {
                "version": STORE_VERSION,
                "revision": document_revision + 1,
                "encryption": getattr(self._cipher, "scheme", "injected-cipher"),
                "updated_at": now_iso(),
                "secrets": secrets,
            },
        )

    def set_secret(self, profile_id: str, api_key: str) -> None:
        clean_key = _clean_text(api_key, "api_key", required=True, maximum=16384)
        with self._locked():
            _profile_revision, profiles = self._read_profiles()
            if not any(profile.profile_id == profile_id for profile in profiles):
                raise ProviderNotFoundError("AI provider profile does not exist.")
            try:
                ciphertext = self._cipher.encrypt(clean_key)
            except Exception:
                raise ProviderSecretError("The API key could not be protected; nothing was stored.") from None
            if not isinstance(ciphertext, str) or not ciphertext:
                raise ProviderSecretError("The API key cipher returned an invalid result; nothing was stored.")
            document_revision, secrets = self._read_secrets()
            secrets[profile_id] = {
                "ciphertext": ciphertext,
                "scheme": getattr(self._cipher, "scheme", "injected-cipher"),
                "updated_at": now_iso(),
            }
            self._write_secrets(document_revision, secrets)

    def has_secret(self, profile_id: str) -> bool:
        with self._locked():
            _revision, secrets = self._read_secrets()
            return profile_id in secrets

    def resolve_secret(self, profile_id: str) -> str:
        with self._locked():
            _revision, secrets = self._read_secrets()
            record = secrets.get(profile_id)
            if record is None:
                raise ProviderSecretError("No API key is stored for this provider profile.")
            try:
                plaintext = self._cipher.decrypt(record["ciphertext"])
            except Exception:
                raise ProviderSecretError("The stored API key could not be resolved.") from None
            if not isinstance(plaintext, str) or not plaintext:
                raise ProviderSecretError("The stored API key could not be resolved.")
            return plaintext

    def delete_secret(self, profile_id: str) -> bool:
        with self._locked():
            document_revision, secrets = self._read_secrets()
            if profile_id not in secrets:
                return False
            del secrets[profile_id]
            self._write_secrets(document_revision, secrets)
            return True

    # UI-facing names make the secret's purpose explicit without changing the
    # generic encrypted-secret implementation.
    set_api_key = set_secret
    has_api_key = has_secret
    resolve_api_key = resolve_secret
    delete_api_key = delete_secret


# The mixed-case alias is convenient for callers that follow normal Python
# acronym casing, while the all-caps name remains explicit in public APIs.
AiProviderStore = AIProviderStore
ProviderSettings = AIProviderSettings


__all__ = [
    "API_KEY",
    "LEGACY_CODEX",
    "EXECUTION_MODES",
    "REASONING_EFFORTS",
    "ProviderProfile",
    "AIProviderSettings",
    "ProviderSettings",
    "AIProviderStore",
    "AiProviderStore",
    "AIProviderStoreError",
    "ProviderValidationError",
    "ProviderNotFoundError",
    "RevisionConflictError",
    "ActiveProfileError",
    "ProviderSecretError",
    "ProviderStoreBusyError",
    "DpapiTextCipher",
    "normalize_provider_url",
    "normalize_base_url",
]
