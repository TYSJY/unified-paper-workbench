from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit

from .contracts import canonical_json


RESPONSES_WIRE_API = "responses"
RUNTIME_API_KEY_ENV = "UNIFIED_CODEX_RUNTIME_API_KEY"
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_REASONING_EFFORTS = frozenset({"", "minimal", "low", "medium", "high", "xhigh"})
_SECRET_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "api-key", "x-api-key"}
)
_INHERITED_IDENTITY_ENV = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "CHATGPT_BASE_URL",
    "CODEX_MODEL",
)
_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class ProviderConfigurationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeProviderConfig:
    """A secret-free, trusted binding for one Responses-compatible provider."""

    profile_id: str = ""
    display_name: str = ""
    base_url: str = ""
    wire_api: str = RESPONSES_WIRE_API
    model: str = ""
    reasoning_effort: str = ""
    proxy_url: str = ""
    custom_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "profile_id",
            "display_name",
            "base_url",
            "wire_api",
            "model",
            "reasoning_effort",
            "proxy_url",
        ):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, value.strip())
        if isinstance(self.custom_headers, Mapping):
            object.__setattr__(self, "custom_headers", MappingProxyType(dict(self.custom_headers)))

    def validate(self) -> None:
        result = validate_provider_config(self)
        if not result.compatible:
            raise ProviderConfigurationError(result.code, result.message)

    def digest(self) -> str:
        return provider_config_digest(self)


@dataclass(frozen=True)
class ProviderCompatibilityResult:
    compatible: bool
    phase: str
    code: str
    message: str
    profile_id: str = ""
    provider_digest: str = ""
    wire_api: str = RESPONSES_WIRE_API
    network_attempted: bool = False
    request_count: int = 0
    http_status: int | None = None
    probe_method: str = ""
    billable_request: bool = False

    @property
    def ok(self) -> bool:
        return self.compatible

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderProbeRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    timeout_seconds: float
    body: bytes = b""


@dataclass(frozen=True)
class ProviderProbeResponse:
    status_code: int


class ProviderProbeTransport(Protocol):
    def __call__(self, request: ProviderProbeRequest) -> ProviderProbeResponse | int | Any: ...


def _valid_url(value: str, *, allow_path: bool) -> bool:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return False
    return allow_path or parsed.path in {"", "/"}


def _validation_error(config: RuntimeProviderConfig) -> tuple[str, str] | None:
    if not isinstance(config.profile_id, str) or _PROFILE_ID.fullmatch(config.profile_id) is None:
        return "PROVIDER_PROFILE_ID_INVALID", "profile_id must be a safe custom provider identifier"
    if not isinstance(config.display_name, str) or not config.display_name or len(config.display_name) > 128:
        return "PROVIDER_DISPLAY_NAME_INVALID", "display_name is required and must be at most 128 characters"
    if not _valid_url(config.base_url, allow_path=True):
        return "PROVIDER_BASE_URL_INVALID", "base_url must be an absolute HTTP(S) URL without credentials, query, or fragment"
    if config.wire_api != RESPONSES_WIRE_API:
        return "PROVIDER_WIRE_API_UNSUPPORTED", "Codex provider wire_api must be responses"
    if not isinstance(config.model, str) or not config.model or len(config.model) > 256 or "\n" in config.model:
        return "PROVIDER_MODEL_INVALID", "model is required and must be a single-line identifier"
    if config.reasoning_effort not in _REASONING_EFFORTS:
        return "PROVIDER_REASONING_EFFORT_INVALID", "reasoning_effort is not supported by Codex"
    if config.proxy_url and not _valid_url(config.proxy_url, allow_path=False):
        return "PROVIDER_PROXY_URL_INVALID", "proxy_url must be an HTTP(S) proxy URL without credentials"
    if not isinstance(config.custom_headers, Mapping):
        return "PROVIDER_HEADERS_INVALID", "custom_headers must be a string mapping"
    seen: set[str] = set()
    for raw_name, raw_value in config.custom_headers.items():
        if not isinstance(raw_name, str) or _HEADER_NAME.fullmatch(raw_name) is None:
            return "PROVIDER_HEADERS_INVALID", "custom header names must be valid HTTP field names"
        lowered = raw_name.lower()
        if lowered in seen:
            return "PROVIDER_HEADERS_INVALID", "custom header names must be unique ignoring case"
        if lowered in _SECRET_HEADER_NAMES:
            return "PROVIDER_SECRET_HEADER_FORBIDDEN", "API credentials must use the isolated runtime environment key"
        seen.add(lowered)
        if not isinstance(raw_value, str) or "\r" in raw_value or "\n" in raw_value:
            return "PROVIDER_HEADERS_INVALID", "custom header values must be single-line strings"
    return None


def _canonical_provider_config(config: RuntimeProviderConfig) -> dict[str, object]:
    return {
        "profile_id": config.profile_id,
        "display_name": config.display_name,
        "base_url": config.base_url.rstrip("/"),
        "wire_api": config.wire_api,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "proxy_url": config.proxy_url.rstrip("/"),
        "custom_headers": dict(sorted(config.custom_headers.items())),
    }


def validate_provider_config(config: RuntimeProviderConfig) -> ProviderCompatibilityResult:
    if not isinstance(config, RuntimeProviderConfig):
        return ProviderCompatibilityResult(
            compatible=False,
            phase="validate",
            code="PROVIDER_CONFIG_TYPE_INVALID",
            message="A RuntimeProviderConfig is required",
        )
    error = _validation_error(config)
    if error is not None:
        return ProviderCompatibilityResult(
            compatible=False,
            phase="validate",
            code=error[0],
            message=error[1],
            profile_id=config.profile_id if isinstance(config.profile_id, str) else "",
            wire_api=config.wire_api if isinstance(config.wire_api, str) else "",
        )
    digest = hashlib.sha256(canonical_json(_canonical_provider_config(config)).encode("utf-8")).hexdigest()
    return ProviderCompatibilityResult(
        compatible=True,
        phase="validate",
        code="PROVIDER_CONFIG_VALID",
        message="Provider configuration is locally valid for the Responses wire API",
        profile_id=config.profile_id,
        provider_digest=digest,
        wire_api=RESPONSES_WIRE_API,
    )


def provider_config_digest(config: RuntimeProviderConfig) -> str:
    result = validate_provider_config(config)
    if not result.compatible:
        raise ProviderConfigurationError(result.code, result.message)
    return result.provider_digest


def responses_endpoint_url(base_url: str) -> str:
    """Return the exact Responses endpoint derived by the Codex provider.

    ``base_url`` is an API prefix, not a complete endpoint.  No vendor-specific
    path such as ``/v1`` is guessed here; callers must include that prefix when
    their provider requires it.
    """

    clean = str(base_url or "").strip().rstrip("/")
    return f"{clean}/responses" if clean else ""


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _headers_inline_table(headers: Mapping[str, str]) -> str:
    if not headers:
        return "{}"
    values = [f"{_toml_string(name)} = {_toml_string(value)}" for name, value in sorted(headers.items())]
    return "{ " + ", ".join(values) + " }"


def _validate_env_key(env_key: str) -> None:
    if _ENV_NAME.fullmatch(str(env_key or "")) is None:
        raise ProviderConfigurationError("PROVIDER_ENV_KEY_INVALID", "env_key must be a safe environment variable name")


def render_codex_config(config: RuntimeProviderConfig, *, env_key: str = RUNTIME_API_KEY_ENV) -> str:
    config.validate()
    _validate_env_key(env_key)
    provider_id = config.profile_id
    lines = [
        f"model = {_toml_string(config.model)}",
        f"model_provider = {_toml_string(provider_id)}",
    ]
    if config.reasoning_effort:
        lines.append(f"model_reasoning_effort = {_toml_string(config.reasoning_effort)}")
    lines.extend(
        [
            "",
        f"[model_providers.{provider_id}]",
        f"name = {_toml_string(config.display_name)}",
        f"base_url = {_toml_string(config.base_url.rstrip('/'))}",
        f"env_key = {_toml_string(env_key)}",
        f"wire_api = {_toml_string(RESPONSES_WIRE_API)}",
        "requires_openai_auth = false",
        "request_max_retries = 0",
        "stream_max_retries = 0",
        f"http_headers = {_headers_inline_table(config.custom_headers)}",
            "",
        ]
    )
    return "\n".join(lines)


def codex_config_overrides(config: RuntimeProviderConfig, *, env_key: str) -> tuple[str, ...]:
    config.validate()
    _validate_env_key(env_key)
    provider = config.profile_id
    overrides = [
        f"model={_toml_string(config.model)}",
        f"model_provider={_toml_string(provider)}",
        f"model_providers.{provider}.name={_toml_string(config.display_name)}",
        f"model_providers.{provider}.base_url={_toml_string(config.base_url.rstrip('/'))}",
        f"model_providers.{provider}.env_key={_toml_string(env_key)}",
        f"model_providers.{provider}.wire_api={_toml_string(RESPONSES_WIRE_API)}",
        f"model_providers.{provider}.requires_openai_auth=false",
        f"model_providers.{provider}.request_max_retries=0",
        f"model_providers.{provider}.stream_max_retries=0",
        f"model_providers.{provider}.http_headers={_headers_inline_table(config.custom_headers)}",
    ]
    if config.reasoning_effort:
        overrides.insert(
            2,
            f"model_reasoning_effort={_toml_string(config.reasoning_effort)}",
        )
    return tuple(overrides)


@dataclass
class IsolatedProviderRuntime:
    config: RuntimeProviderConfig
    root: Path
    codex_home: Path
    config_path: Path
    env_key: str
    provider_digest: str
    config_overrides: tuple[str, ...]
    _environment: dict[str, str] = field(repr=False)

    @property
    def provider_id(self) -> str:
        return self.config.profile_id

    @property
    def environment(self) -> Mapping[str, str]:
        return MappingProxyType(self._environment)

    def clear_secret_material(self) -> None:
        self._environment.clear()


def _private_environment(
    config: RuntimeProviderConfig,
    *,
    root: Path,
    codex_home: Path,
    env_key: str,
    api_key: str,
) -> dict[str, str]:
    private_home = root / "home"
    roaming = private_home / "AppData" / "Roaming"
    local = private_home / "AppData" / "Local"
    temp = root / "temp"
    xdg = private_home / ".config"
    for directory in (private_home, roaming, local, temp, xdg):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(private_home),
        "USERPROFILE": str(private_home),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "XDG_CONFIG_HOME": str(xdg),
        "TEMP": str(temp),
        "TMP": str(temp),
        env_key: api_key,
    }
    env.update({name: "" for name in _INHERITED_IDENTITY_ENV})
    env.update({name: "" for name in _PROXY_ENV})
    if config.proxy_url:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[name] = config.proxy_url
    return env


def _user_codex_roots() -> tuple[Path, ...]:
    roots: list[Path] = [Path.home() / ".codex"]
    for name in ("HOME", "USERPROFILE"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            roots.append(Path(value) / ".codex")
    return tuple(roots)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


@contextmanager
def isolated_provider_runtime(
    config: RuntimeProviderConfig,
    api_key: str,
    *,
    temp_root: str | Path | None = None,
) -> Iterator[IsolatedProviderRuntime]:
    config.validate()
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderConfigurationError("PROVIDER_API_KEY_REQUIRED", "A provider API key is required")
    parent = Path(temp_root).resolve() if temp_root is not None else None
    if parent is not None and any(_is_within(parent, root) for root in _user_codex_roots()):
        raise ProviderConfigurationError("PROVIDER_RUNTIME_HOME_FORBIDDEN", "Runtime files cannot use the user Codex home")
    if parent is not None:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="unified-codex-runtime-", dir=str(parent) if parent else None)).resolve()
    runtime: IsolatedProviderRuntime | None = None
    try:
        if any(_is_within(root, user_root) for user_root in _user_codex_roots()):
            raise ProviderConfigurationError("PROVIDER_RUNTIME_HOME_FORBIDDEN", "Runtime files cannot use the user Codex home")
        codex_home = root / "codex-home"
        codex_home.mkdir(mode=0o700)
        env_key = f"{RUNTIME_API_KEY_ENV}_{secrets.token_hex(8).upper()}"
        config_path = codex_home / "config.toml"
        config_path.write_text(render_codex_config(config, env_key=env_key), encoding="utf-8", newline="\n")
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
        runtime = IsolatedProviderRuntime(
            config=config,
            root=root,
            codex_home=codex_home,
            config_path=config_path,
            env_key=env_key,
            provider_digest=provider_config_digest(config),
            config_overrides=codex_config_overrides(config, env_key=env_key),
            _environment=_private_environment(
                config,
                root=root,
                codex_home=codex_home,
                env_key=env_key,
                api_key=api_key,
            ),
        )
        yield runtime
    finally:
        if runtime is not None:
            runtime.clear_secret_material()
        shutil.rmtree(root, ignore_errors=True)


def _default_probe_transport(
    request: ProviderProbeRequest,
    *,
    proxy_url: str,
    transport: Any | None = None,
) -> ProviderProbeResponse:
    import httpx

    options: dict[str, Any] = {
        "follow_redirects": False,
        "trust_env": False,
        "timeout": request.timeout_seconds,
    }
    if proxy_url:
        options["proxy"] = proxy_url
    if transport is not None:
        options["transport"] = transport
    with httpx.Client(**options) as client:
        response = client.request(request.method, request.url, headers=dict(request.headers), content=request.body)
    return ProviderProbeResponse(status_code=int(response.status_code))


def probe_provider_compatibility(
    config: RuntimeProviderConfig,
    *,
    api_key: str = "",
    key_resolver: Callable[[str], str] | None = None,
    transport: Any | None = None,
    timeout_seconds: float = 5.0,
) -> ProviderCompatibilityResult:
    """Perform one explicit, non-billable OPTIONS request against /responses."""

    local = validate_provider_config(config)
    if not local.compatible:
        return local
    if timeout_seconds <= 0:
        return ProviderCompatibilityResult(
            compatible=False,
            phase="probe",
            code="PROVIDER_PROBE_TIMEOUT_INVALID",
            message="timeout_seconds must be positive",
            profile_id=config.profile_id,
            provider_digest=local.provider_digest,
        )
    if api_key and key_resolver is not None:
        raise ValueError("Pass api_key or key_resolver, not both")
    try:
        resolved_key = key_resolver(config.profile_id) if key_resolver is not None else api_key
    except Exception:
        return ProviderCompatibilityResult(
            compatible=False,
            phase="probe",
            code="PROVIDER_KEY_RESOLUTION_FAILED",
            message="Provider key resolution failed before the network probe",
            profile_id=config.profile_id,
            provider_digest=local.provider_digest,
        )
    headers = {"Accept": "application/json", **dict(config.custom_headers)}
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"
    request = ProviderProbeRequest(
        method="OPTIONS",
        url=responses_endpoint_url(config.base_url),
        headers=MappingProxyType(headers),
        timeout_seconds=float(timeout_seconds),
    )
    try:
        if transport is None:
            raw_response = _default_probe_transport(request, proxy_url=config.proxy_url)
        elif callable(transport):
            raw_response = transport(request)
        elif hasattr(transport, "handle_request"):
            raw_response = _default_probe_transport(
                request,
                proxy_url=config.proxy_url,
                transport=transport,
            )
        else:
            raise TypeError("transport must be callable or an httpx transport")
        status = int(raw_response if isinstance(raw_response, int) else raw_response.status_code)
    except Exception as exc:
        return ProviderCompatibilityResult(
            compatible=False,
            phase="probe",
            code="PROVIDER_PROBE_TRANSPORT_ERROR",
            message=f"Provider probe failed with {type(exc).__name__}",
            profile_id=config.profile_id,
            provider_digest=local.provider_digest,
            network_attempted=True,
            request_count=1,
            probe_method="OPTIONS",
        )
    compatible = 200 <= status < 300
    return ProviderCompatibilityResult(
        compatible=compatible,
        phase="probe",
        code="PROVIDER_RESPONSES_PROBE_OK" if compatible else "PROVIDER_RESPONSES_PROBE_REJECTED",
        message=(
            "Responses endpoint accepted the non-billable compatibility probe"
            if compatible
            else "Responses endpoint did not accept the non-billable compatibility probe"
        ),
        profile_id=config.profile_id,
        provider_digest=local.provider_digest,
        wire_api=RESPONSES_WIRE_API,
        network_attempted=True,
        request_count=1,
        http_status=status,
        probe_method="OPTIONS",
        billable_request=False,
    )


__all__ = [
    "IsolatedProviderRuntime",
    "ProviderCompatibilityResult",
    "ProviderConfigurationError",
    "ProviderProbeRequest",
    "ProviderProbeResponse",
    "RESPONSES_WIRE_API",
    "RUNTIME_API_KEY_ENV",
    "RuntimeProviderConfig",
    "codex_config_overrides",
    "isolated_provider_runtime",
    "probe_provider_compatibility",
    "provider_config_digest",
    "responses_endpoint_url",
    "render_codex_config",
    "validate_provider_config",
]
