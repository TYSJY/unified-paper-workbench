from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..contracts import ResultStatus, TaskEnvelope, TaskResult, canonical_json
from .base import ExecutorDescriptor
from .openai_relay import (
    QILIN_RELAY_BASE_URL,
    QILIN_RELAY_PROVIDER_ASSURANCE,
    QILIN_RELAY_WIRE_API,
    RELAY_API_KEY_ENV,
    RELAY_LOOPBACK_PROXY_ENV,
    RELAY_MODEL_ENV,
)
from .openai_review import OpenAIProxyConfigurationError, validated_loopback_proxy_url


CODEX_CLI_EXECUTABLE_ENV = "UNIFIED_CODEX_CLI_PATH"
CODEX_CLI_CLIENT_KIND = "pinned_native_codex_cli_binary"
CODEX_CLI_CLIENT_PROVENANCE_ASSURANCE = "operator_approved_path_sha256_version_only_v1"
CODEX_CLI_PROVIDER_KIND = "third_party_codex_cli_relay"
CODEX_CLI_PROVIDER_PROFILE = "qilinapi_pinned_native_cli_third_party_provider_v1"
CODEX_CLI_CONFIG_PROFILE = "isolated_ephemeral_zero_desktop_identity_v1"
CODEX_CLI_REASONING_EFFORT = "xhigh"
CODEX_CLI_MIN_VERSION = "0.125.0"
CODEX_CLI_ARGV_POLICY = "codex_cli_review_argv_v1"
CODEX_CLI_ENV_POLICY = "private_profile_child_env_relay_key_only_v2"
CODEX_CLI_JSONL_POLICY = "codex_exec_jsonl_review_v1"
CODEX_CLI_IDENTITY_POLICY = "desktop_chatgpt_identity_forbidden_v1"
CODEX_CLI_CREDENTIAL_POLICY = "parent_memory_to_isolated_child_env_no_disk_v1"
CODEX_CLI_BILLING_POLICY = "qilin_api_key_scope_unverified_account_v1"
CODEX_CLI_FALLBACK_POLICY = "no_cross_executor_or_identity_fallback_v1"
CODEX_CLI_REASONING_NAMESPACE = "codex_cli_api_v1"
CODEX_CLI_USAGE_TOTAL_DERIVATION = "input_plus_output_v1"
CODEX_CLI_OUTPUT_LIMIT_POLICY = "posthoc_only_cli_no_server_hard_limit_v1"
CODEX_CLI_KEY_FINGERPRINT_DOMAIN = b"unified-paper-orchestrator:qilin-relay-key:v1\0"
_SAFE_THREAD_ID = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SAFE_VERSION = re.compile(r"^codex-cli\s+(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?\s*$")
_SAFE_TASK_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024
_MAX_JSONL_LINE_BYTES = 256 * 1024
_MAX_JSONL_EVENTS = 512
_CREATE_NO_WINDOW = 0x08000000


def relay_key_fingerprint(api_key: str) -> str:
    value = str(api_key or "").strip()
    if not value:
        return ""
    return hashlib.sha256(CODEX_CLI_KEY_FINGERPRINT_DOMAIN + value.encode("utf-8")).hexdigest()


def codex_cli_static_config() -> dict[str, object]:
    return {
        "provider_kind": CODEX_CLI_PROVIDER_KIND,
        "provider_profile": CODEX_CLI_PROVIDER_PROFILE,
        "provider_assurance": QILIN_RELAY_PROVIDER_ASSURANCE,
        "wire_api": QILIN_RELAY_WIRE_API,
        "endpoint_base_url": QILIN_RELAY_BASE_URL,
        "client_kind": CODEX_CLI_CLIENT_KIND,
        "client_provenance_assurance": CODEX_CLI_CLIENT_PROVENANCE_ASSURANCE,
        "client_config_profile": CODEX_CLI_CONFIG_PROFILE,
        "reasoning_effort": CODEX_CLI_REASONING_EFFORT,
        "reasoning_effort_namespace": CODEX_CLI_REASONING_NAMESPACE,
        "identity_policy": CODEX_CLI_IDENTITY_POLICY,
        "credential_policy": CODEX_CLI_CREDENTIAL_POLICY,
        "billing_policy": CODEX_CLI_BILLING_POLICY,
        "billing_account_verified": False,
        "chatgpt_subscription_used": False,
        "openai_api_account_used": False,
        "official_openai_endpoint_proven": False,
        "upstream_model_identity_verified": False,
        "model_identity_assurance": "relay_claim_unverified",
        "fallback_policy": CODEX_CLI_FALLBACK_POLICY,
        "output_token_limit_policy": CODEX_CLI_OUTPUT_LIMIT_POLICY,
        "minimum_codex_cli_version": CODEX_CLI_MIN_VERSION,
        "argv_policy": CODEX_CLI_ARGV_POLICY,
        "environment_policy": CODEX_CLI_ENV_POLICY,
        "jsonl_policy": CODEX_CLI_JSONL_POLICY,
    }


def codex_cli_static_config_sha256() -> str:
    return hashlib.sha256(canonical_json(codex_cli_static_config()).encode("utf-8")).hexdigest()


def _is_link_like(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return bool(reparse_flag and attributes & reparse_flag)
    except OSError:
        return True


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within_path(path: Path, root: Path) -> bool:
    normalized = _normalized_path(path)
    normalized_root = _normalized_path(root)
    return normalized == normalized_root or normalized.startswith(normalized_root + os.sep)


def _desktop_owned_roots() -> tuple[Path, ...]:
    """Locations the EXE route must never use for its binary or runtime."""

    candidates: list[Path] = [Path.home() / ".codex"]
    for name in ("USERPROFILE", "HOME"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            candidates.append(Path(value) / ".codex")
    if os.name == "nt":
        program_files = str(os.environ.get("PROGRAMFILES") or "").strip()
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if program_files:
            candidates.append(Path(program_files) / "WindowsApps")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Microsoft" / "WindowsApps")
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(_normalized_path(candidate), candidate)
    return tuple(unique.values())


def _reject_desktop_owned_path(path: Path) -> None:
    if any(_is_within_path(path, root) for root in _desktop_owned_roots()):
        raise ValueError("The EXE route cannot use a Codex Desktop-owned path.")


def resolve_native_codex_cli(value: str | Path) -> Path:
    raw = Path(str(value or "").strip())
    if not raw.is_absolute():
        raise ValueError("A pinned absolute native Codex CLI path is required.")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The pinned native Codex CLI executable is unavailable.") from exc
    expected_name = "codex.exe" if os.name == "nt" else "codex"
    if resolved.name.lower() != expected_name or not resolved.is_file() or _is_link_like(resolved):
        raise ValueError("The pinned Codex CLI path is not a native regular executable.")
    _reject_desktop_owned_path(resolved)
    normalized = _normalized_path(resolved)
    if os.name == "nt" and re.search(r"\\windowsapps\\", normalized, flags=re.IGNORECASE):
        raise ValueError("The EXE route cannot reuse a WindowsApps Codex binary or alias.")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CodexCLIInvocation:
    argv: tuple[str, ...]
    stdin_text: str
    env: Mapping[str, str]
    cwd: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CodexCLIProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    capture_oversize: bool = False
    launch_error_type: str = ""


ProcessRunner = Callable[[CodexCLIInvocation], CodexCLIProcessResult]


def _bounded_text(value: str) -> tuple[str, bool]:
    encoded = str(value or "").encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_CAPTURE_BYTES:
        return encoded.decode("utf-8", errors="replace"), False
    return encoded[:_MAX_CAPTURE_BYTES].decode("utf-8", errors="replace"), True


def run_codex_cli_process(invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(invocation.argv),
            input=invocation.stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=invocation.cwd,
            env=dict(invocation.env),
            timeout=max(1, int(invocation.timeout_seconds)),
            check=False,
            shell=False,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_oversize = _bounded_text(
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else str(exc.stdout or "")
        )
        stderr, stderr_oversize = _bounded_text(
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or "")
        )
        return CodexCLIProcessResult(
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            duration_ms=int((time.monotonic() - started) * 1000),
            capture_oversize=stdout_oversize or stderr_oversize,
        )
    except OSError as exc:
        return CodexCLIProcessResult(
            returncode=None,
            stdout="",
            stderr="",
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            launch_error_type=type(exc).__name__,
        )
    stdout, stdout_oversize = _bounded_text(completed.stdout)
    stderr, stderr_oversize = _bounded_text(completed.stderr)
    return CodexCLIProcessResult(
        returncode=int(completed.returncode),
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        duration_ms=int((time.monotonic() - started) * 1000),
        capture_oversize=stdout_oversize or stderr_oversize,
    )


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _SAFE_VERSION.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(item) for item in match.groups())


def _minimal_child_environment(
    *,
    runtime_home: Path,
    runtime_temp: Path,
    api_key: str,
    proxy_url: str,
) -> dict[str, str]:
    source = {str(key).upper(): str(value) for key, value in os.environ.items()}
    names = (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROCESSOR_ARCHITECTURE",
    )
    env = {name: source[name] for name in names if source.get(name)}
    private_profile = runtime_home.resolve()
    private_roaming = private_profile / "AppData" / "Roaming"
    private_local = private_profile / "AppData" / "Local"
    private_temp = runtime_temp.resolve()
    for directory in (private_profile, private_roaming, private_local, private_temp):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    env.update(
        {
            "CODEX_HOME": str(private_profile),
            "HOME": str(private_profile),
            "USERPROFILE": str(private_profile),
            "APPDATA": str(private_roaming),
            "LOCALAPPDATA": str(private_local),
            "TEMP": str(private_temp),
            "TMP": str(private_temp),
            RELAY_API_KEY_ENV: api_key,
            "NO_COLOR": "1",
            "RUST_BACKTRACE": "0",
        }
    )
    if proxy_url:
        env.update(
            {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
            }
        )
    return env


def _config_argument(
    key: str,
    value: str | bool | int | Mapping[str, object],
) -> tuple[str, str]:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, Mapping):
        rendered = canonical_json(dict(value))
    else:
        rendered = json.dumps(str(value), ensure_ascii=False)
    return "-c", f"{key}={rendered}"


def _argv(executable: Path, *, model: str, workdir: Path) -> tuple[str, ...]:
    argv: list[str] = [
        str(executable),
        "-a",
        "never",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--cd",
        str(workdir),
        "--model",
        model,
    ]
    for feature in (
        "shell_tool",
        "shell_snapshot",
        "apps",
        "browser_use",
        "in_app_browser",
        "computer_use",
        "image_generation",
        "multi_agent",
        "plugins",
        "tool_search",
        "tool_suggest",
        "workspace_dependencies",
        "codex_hooks",
        "skill_mcp_dependency_install",
        "tool_call_mcp_elicitation",
        "guardian_approval",
    ):
        argv.extend(("--disable", feature))
    settings: Sequence[tuple[str, str | bool | int | Mapping[str, object]]] = (
        ("model_provider", "qilin_relay"),
        ("model_reasoning_effort", CODEX_CLI_REASONING_EFFORT),
        ("model_reasoning_summary", "none"),
        ("hide_agent_reasoning", True),
        ("show_raw_agent_reasoning", False),
        ("web_search", "disabled"),
        ("features.shell_tool", False),
        ("features.shell_snapshot", False),
        ("features.multi_agent", False),
        ("tools.web_search", False),
        ("tools.view_image", False),
        ("apps._default.enabled", False),
        ("mcp_servers", {}),
        ("approval_policy", "never"),
        ("shell_environment_policy.inherit", "none"),
        ("check_for_update_on_startup", False),
        ("analytics.enabled", False),
        ("feedback.enabled", False),
        ("otel.metrics_exporter", "none"),
        ("include_apps_instructions", False),
        ("include_environment_context", False),
        ("include_permissions_instructions", False),
        ("include_collaboration_mode_instructions", False),
        ("model_providers.qilin_relay.name", "Qilin Codex Relay"),
        ("model_providers.qilin_relay.base_url", QILIN_RELAY_BASE_URL),
        ("model_providers.qilin_relay.env_key", RELAY_API_KEY_ENV),
        ("model_providers.qilin_relay.wire_api", QILIN_RELAY_WIRE_API),
        ("model_providers.qilin_relay.requires_openai_auth", False),
        ("model_providers.qilin_relay.request_max_retries", 0),
        ("model_providers.qilin_relay.stream_max_retries", 0),
    )
    for key, value in settings:
        argv.extend(_config_argument(key, value))
    argv.append("-")
    return tuple(argv)


def codex_cli_argv_policy_sha256() -> str:
    template = _argv(Path("__EXECUTABLE__"), model="MODEL", workdir=Path("__WORKDIR__"))
    normalized = [
        "<EXECUTABLE>" if index == 0 else "<WORKDIR>" if item == "__WORKDIR__" else item
        for index, item in enumerate(template)
    ]
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def _parse_jsonl(stdout: str) -> dict[str, object]:
    thread_ids: list[str] = []
    final_messages: list[str] = []
    usage: dict[str, int] = {}
    event_count = 0
    last_event_type = ""
    tool_event_types: list[str] = []
    invalid_lines = 0
    failed_event = False
    unknown_event_types: list[str] = []
    turn_started_count = 0
    turn_completed_count = 0
    turn_failed_count = 0
    oversize_line_count = 0
    provider_error_code = ""
    provider_error_type = ""
    provider_http_status: int | None = None
    allowed_event_types = {
        "thread.started",
        "turn.started",
        "item.started",
        "item.updated",
        "item.completed",
        "turn.completed",
        "turn.failed",
        "error",
    }
    allowed_item_types = {"agent_message", "reasoning"}
    forbidden_item_types = {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "collab_tool_call",
        "todo_list",
    }
    for line in str(stdout or "").splitlines():
        if not line.strip():
            continue
        if len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            oversize_line_count += 1
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(event, dict):
            invalid_lines += 1
            continue
        event_count += 1
        event_type = str(event.get("type") or "")
        last_event_type = event_type
        if event_type not in allowed_event_types:
            unknown_event_types.append(event_type or "missing_type")
            continue
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "").strip()
            if thread_id:
                thread_ids.append(thread_id)
        elif event_type == "turn.started":
            turn_started_count += 1
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
            if item_type in forbidden_item_types or (item_type and item_type not in allowed_item_types):
                tool_event_types.append(item_type or "unknown_item")
            if event_type == "item.completed" and item_type == "agent_message" and isinstance(item, dict):
                text = str(item.get("text") or "")
                if text.strip():
                    final_messages.append(text)
        elif event_type == "turn.completed":
            turn_completed_count += 1
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                ):
                    value = raw_usage.get(key)
                    if isinstance(value, int) and value >= 0:
                        usage[key] = value
        elif event_type in {"turn.failed", "error"}:
            failed_event = True
            if event_type == "turn.failed":
                turn_failed_count += 1
            raw_error = event.get("error")
            if not isinstance(raw_error, dict):
                raw_error = event
            raw_status = raw_error.get("http_status_code")
            if isinstance(raw_status, int) and 100 <= raw_status <= 599:
                provider_http_status = raw_status
            for key, target in (("code", "code"), ("type", "type")):
                value = str(raw_error.get(key) or "").strip()
                if value and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:/-]+", value):
                    if target == "code":
                        provider_error_code = value
                    else:
                        provider_error_type = value
    usage_total_source = "reported_total_tokens"
    if "total_tokens" not in usage and "input_tokens" in usage and "output_tokens" in usage:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        usage_total_source = CODEX_CLI_USAGE_TOTAL_DERIVATION
    elif "total_tokens" not in usage:
        usage_total_source = "missing"
    return {
        "thread_ids": thread_ids,
        "final_messages": final_messages,
        "usage": usage,
        "usage_total_source": usage_total_source,
        "event_count": event_count,
        "last_event_type": last_event_type,
        "tool_event_types": sorted(set(tool_event_types)),
        "invalid_lines": invalid_lines,
        "failed_event": failed_event,
        "unknown_event_types": sorted(set(unknown_event_types)),
        "turn_started_count": turn_started_count,
        "turn_completed_count": turn_completed_count,
        "turn_failed_count": turn_failed_count,
        "oversize_line_count": oversize_line_count,
        "event_limit_exceeded": event_count > _MAX_JSONL_EVENTS,
        "provider_error_code": provider_error_code,
        "provider_error_type": provider_error_type,
        "provider_http_status": provider_http_status,
        "jsonl_sha256": hashlib.sha256(str(stdout or "").encode("utf-8")).hexdigest(),
    }


def _safe_runtime_directory(staging_root: Path, task: TaskEnvelope) -> Path:
    if _SAFE_TASK_PART.fullmatch(task.run_id) is None or _SAFE_TASK_PART.fullmatch(task.task_id) is None:
        raise ValueError("Task identity is unsafe for the isolated Codex runtime.")
    root = staging_root.resolve(strict=True)
    _reject_desktop_owned_path(root)
    runtime_parent = root / ".codex_cli_runtime"
    runtime_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _is_link_like(runtime_parent):
        raise ValueError("The isolated Codex runtime parent is unsafe.")
    runtime = Path(tempfile.mkdtemp(prefix=f"{task.task_id}-", dir=runtime_parent))
    runtime.resolve(strict=True).relative_to(root)
    return runtime


def _safe_cleanup_runtime(runtime: Path, staging_root: Path) -> None:
    try:
        root = staging_root.resolve(strict=True)
        target = runtime.resolve(strict=True)
        target.relative_to(root)
        if target == root or target.parent.name != ".codex_cli_runtime":
            return
        shutil.rmtree(target)
    except (OSError, ValueError):
        return


class CodexCLIRelayReviewExecutor:
    """Pinned Qilin review route executed by a real, isolated Codex CLI."""

    name = "qilin_codex_cli_review"
    descriptor_kind = "third_party_codex_cli_staging_review"
    provider_kind = CODEX_CLI_PROVIDER_KIND
    provider_profile = CODEX_CLI_PROVIDER_PROFILE
    provider_assurance = QILIN_RELAY_PROVIDER_ASSURANCE

    def __init__(self, process_runner: ProcessRunner | None = None) -> None:
        self.process_runner = process_runner or run_codex_cli_process

    def descriptor(self) -> ExecutorDescriptor:
        executable = os.environ.get(CODEX_CLI_EXECUTABLE_ENV, "").strip()
        api_key = os.environ.get(RELAY_API_KEY_ENV, "").strip()
        model = os.environ.get(RELAY_MODEL_ENV, "").strip()
        binary_valid = False
        if executable:
            try:
                resolve_native_codex_cli(executable)
                binary_valid = True
            except ValueError:
                binary_valid = False
        available = bool(binary_valid and api_key)
        if available and model:
            detail = "isolated native CLI, relay credential, and explicit model are configured"
        elif available:
            detail = "isolated native CLI and relay credential are ready; each task must bind an explicit model"
        elif executable and not binary_valid:
            detail = "RELAY_CODEX_CLI_PATH_INVALID"
        elif not api_key:
            detail = f"RELAY_CREDENTIAL_REQUIRED:{RELAY_API_KEY_ENV}"
        else:
            detail = f"RELAY_CODEX_CLI_PATH_REQUIRED:{CODEX_CLI_EXECUTABLE_ENV}"
        return ExecutorDescriptor(
            name=self.name,
            available=available,
            kind=self.descriptor_kind,
            detail=detail,
            side_effecting=True,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        result = self._execute(task)
        result.data = {
            **result.data,
            "provider_kind": self.provider_kind,
            "provider_profile": self.provider_profile,
            "provider_assurance": self.provider_assurance,
            "billing_credential_fingerprint": str(
                task.payload.get("relay_key_fingerprint") or ""
            ),
        }
        return result

    def _execute(self, task: TaskEnvelope) -> TaskResult:
        expected_static = {
            **codex_cli_static_config(),
            "endpoint_config_sha256": codex_cli_static_config_sha256(),
            "argv_policy_sha256": codex_cli_argv_policy_sha256(),
            "third_party_data_disclosure_ack": True,
        }
        if any(task.payload.get(key) != value for key, value in expected_static.items()):
            return self._blocked(
                "RELAY_CODEX_CLI_BINDING_MISMATCH",
                "Codex CLI relay intent no longer matches the pinned execution contract; no process was started.",
                transport_policy="binding_rejected_before_process",
            )
        api_key = os.environ.get(RELAY_API_KEY_ENV, "").strip()
        if not api_key:
            return self._blocked(
                "RELAY_API_NOT_CONFIGURED",
                f"{RELAY_API_KEY_ENV} is not configured.",
                transport_policy="not_started",
            )
        if relay_key_fingerprint(api_key) != str(task.payload.get("relay_key_fingerprint") or ""):
            return self._blocked(
                "RELAY_CREDENTIAL_BINDING_MISMATCH",
                "The in-memory relay credential does not match the durably approved fingerprint; no process was started.",
                transport_policy="credential_binding_rejected_before_process",
            )
        if not task.network_access or task.allowed_tools:
            return self._blocked(
                "RELAY_CODEX_CLI_CAPABILITY_DENIED",
                "Codex CLI review requires network authorization and zero gateway tools.",
                transport_policy="not_started",
            )
        instructions = str(task.payload.get("_prepared_instructions") or "")
        input_text = str(task.payload.get("_prepared_input") or "")
        if not instructions or not input_text:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_REVIEW_INPUT_NOT_PREPARED",
                message="Review task was not prepared by the execution gateway.",
            )
        model = str(task.payload.get("model") or "").strip()
        if not model:
            return self._blocked(
                "RELAY_MODEL_REQUIRED",
                "The approved Codex CLI relay task does not contain an explicit model.",
                transport_policy="not_started",
            )
        try:
            max_output_tokens = int(task.payload.get("max_output_tokens") or 0)
        except (TypeError, ValueError):
            max_output_tokens = 0
        if max_output_tokens <= 0:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_OUTPUT_LIMIT_INVALID",
                message="A positive post-hoc output token limit is required.",
            )
        if task.token_budget is not None and max_output_tokens > task.token_budget:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_TOKEN_BUDGET_INVALID",
                message="The post-hoc output token limit exceeds the declared total token budget.",
            )
        try:
            estimated_input_tokens = int(
                task.payload.get("_estimated_input_tokens") or 0
            )
        except (TypeError, ValueError):
            estimated_input_tokens = 0
        if estimated_input_tokens <= 0:
            total_bytes = len(instructions.encode("utf-8")) + len(
                input_text.encode("utf-8")
            )
            estimated_input_tokens = max(1, (total_bytes + 2) // 3)
        if (
            task.token_budget is not None
            and estimated_input_tokens + max_output_tokens > task.token_budget
        ):
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_TOKEN_BUDGET_PREFLIGHT",
                message="Estimated input plus the post-hoc output limit exceeds the declared budget; no process was started.",
                metrics={
                    "estimated_input_tokens": estimated_input_tokens,
                    "posthoc_output_token_limit": max_output_tokens,
                    "token_budget": task.token_budget,
                },
            )
        try:
            executable = resolve_native_codex_cli(str(task.payload.get("codex_cli_path") or ""))
            observed_sha256 = sha256_file(executable)
        except (OSError, ValueError):
            return self._blocked(
                "RELAY_CODEX_CLI_NOT_AVAILABLE",
                "The approved native Codex CLI executable is unavailable or unsafe.",
                transport_policy="not_started",
            )
        if observed_sha256 != str(task.payload.get("codex_cli_sha256") or ""):
            return self._blocked(
                "RELAY_CODEX_CLI_BINARY_DRIFT",
                "The native Codex CLI binary changed after approval; no process was started.",
                transport_policy="binary_binding_rejected_before_process",
            )
        try:
            proxy_url = validated_loopback_proxy_url(os.environ.get(RELAY_LOOPBACK_PROXY_ENV, ""))
        except OpenAIProxyConfigurationError as exc:
            return self._blocked(
                "RELAY_LOOPBACK_PROXY_INVALID",
                str(exc),
                transport_policy="invalid_explicit_proxy_rejected",
            )
        transport_policy = (
            "pinned_native_codex_cli_explicit_loopback_proxy_no_env"
            if proxy_url
            else "pinned_native_codex_cli_direct_no_env_proxy"
        )
        if len(task.writable_roots) != 1:
            return self._blocked(
                "RELAY_CODEX_CLI_RUNTIME_ROOT_INVALID",
                "Exactly one private staging root is required for the isolated Codex runtime.",
                transport_policy="not_started",
            )
        staging_root = Path(task.writable_roots[0])
        try:
            runtime = _safe_runtime_directory(staging_root, task)
        except (OSError, ValueError):
            return self._blocked(
                "RELAY_CODEX_CLI_RUNTIME_ROOT_INVALID",
                "The isolated Codex runtime directory could not be created safely.",
                transport_policy="not_started",
            )
        runtime_home = runtime / "home"
        workdir = runtime / "workspace"
        runtime_temp = runtime / "temp"
        runtime_home.mkdir(mode=0o700)
        workdir.mkdir(mode=0o700)
        runtime_temp.mkdir(mode=0o700)
        env = _minimal_child_environment(
            runtime_home=runtime_home,
            runtime_temp=runtime_temp,
            api_key=api_key,
            proxy_url=proxy_url,
        )
        try:
            version_run = self.process_runner(
                CodexCLIInvocation(
                    argv=(str(executable), "--version"),
                    stdin_text="",
                    env=env,
                    cwd=workdir,
                    timeout_seconds=min(15, max(1, int(task.timeout_seconds))),
                )
            )
            version = str(version_run.stdout or "").strip()
            parsed_version = _version_tuple(version)
            minimum = _version_tuple(f"codex-cli {CODEX_CLI_MIN_VERSION}")
            if (
                version_run.launch_error_type
                or
                version_run.timed_out
                or version_run.returncode != 0
                or version_run.capture_oversize
                or parsed_version is None
                or minimum is None
                or parsed_version < minimum
            ):
                return self._blocked(
                    (
                        "RELAY_CODEX_CLI_LAUNCH_FAILED"
                        if version_run.launch_error_type
                        else "RELAY_CODEX_CLI_VERSION_UNSUPPORTED"
                    ),
                    (
                        "The pinned native Codex CLI process could not be launched."
                        if version_run.launch_error_type
                        else "The pinned native Codex CLI version could not be verified as supported."
                    ),
                    transport_policy="version_check_failed_before_request",
                )
            prompt = (
                f"{instructions}\n\n"
                "<prepared_review_inputs>\n"
                f"{input_text}\n"
                "</prepared_review_inputs>"
            )
            invocation = CodexCLIInvocation(
                argv=_argv(executable, model=model, workdir=workdir),
                stdin_text=prompt,
                env=env,
                cwd=workdir,
                timeout_seconds=max(1, int(task.timeout_seconds)),
            )
            completed = self.process_runner(invocation)
        finally:
            api_key = ""
            env.pop(RELAY_API_KEY_ENV, None)
            _safe_cleanup_runtime(runtime, staging_root)
        version_text = f"{parsed_version[0]}.{parsed_version[1]}.{parsed_version[2]}"
        if completed.timed_out:
            return TaskResult(
                status=ResultStatus.RETRYABLE_FAILURE,
                blocker_code="RELAY_API_TIMEOUT",
                message="Codex CLI relay execution timed out; a request or charge may already have occurred.",
                metrics={"duration_ms": completed.duration_ms},
                data=self._safe_execution_data(
                    model=model,
                    version=version_text,
                    binary_sha256=observed_sha256,
                    transport_policy=transport_policy,
                    exit_code=None,
                    error_type="TimeoutExpired",
                ),
            )
        if completed.launch_error_type:
            return self._blocked(
                "RELAY_CODEX_CLI_LAUNCH_FAILED",
                "The pinned native Codex CLI process could not be launched.",
                transport_policy="launch_failed_before_request",
            )
        parsed = _parse_jsonl(completed.stdout)
        safe_data = self._safe_execution_data(
            model=model,
            version=version_text,
            binary_sha256=observed_sha256,
            transport_policy=transport_policy,
            exit_code=completed.returncode,
            error_type="CodexCLIExitError" if completed.returncode else "",
            event_count=int(parsed["event_count"]),
            last_event_type=str(parsed["last_event_type"]),
            jsonl_sha256=str(parsed["jsonl_sha256"]),
            tool_event_count=len(parsed["tool_event_types"]),
            stderr_sha256=hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
            stderr_bytes=len(completed.stderr.encode("utf-8")),
        )
        if completed.capture_oversize:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_PROTOCOL_ERROR",
                message="Codex CLI JSONL exceeded the bounded parser limit; output was discarded.",
                metrics={"duration_ms": completed.duration_ms},
                data=safe_data,
            )
        if completed.returncode != 0 or bool(parsed["failed_event"]):
            return self._process_failure(completed, parsed, safe_data)
        if (
            int(parsed["invalid_lines"])
            or int(parsed["oversize_line_count"])
            or bool(parsed["event_limit_exceeded"])
            or parsed["unknown_event_types"]
            or len(parsed["thread_ids"]) != 1
            or int(parsed["turn_started_count"]) != 1
            or int(parsed["turn_completed_count"]) != 1
            or int(parsed["turn_failed_count"]) != 0
        ):
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_PROTOCOL_ERROR",
                message="Codex CLI did not return one complete, valid JSONL thread.",
                metrics={"duration_ms": completed.duration_ms},
                data=safe_data,
            )
        thread_id = str(parsed["thread_ids"][0])
        if _SAFE_THREAD_ID.fullmatch(thread_id) is None:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_THREAD_ID_INVALID",
                message="Codex CLI did not return a route-valid thread identifier.",
                metrics={"duration_ms": completed.duration_ms},
                data=safe_data,
            )
        if parsed["tool_event_types"]:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_TOOL_EVENT_FORBIDDEN",
                message="Codex CLI emitted a forbidden tool event; output was discarded.",
                metrics={"duration_ms": completed.duration_ms},
                data=safe_data,
                external_ref=thread_id,
            )
        messages = parsed["final_messages"]
        output_text = str(messages[-1]) if len(messages) == 1 else ""
        usage = dict(parsed["usage"])
        total_tokens = usage.get("total_tokens")
        if not output_text.strip():
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_FINAL_MESSAGE_INVALID",
                message="Codex CLI did not return exactly one final review message.",
                metrics={**usage, "duration_ms": completed.duration_ms},
                data=safe_data,
                external_ref=thread_id,
            )
        if not isinstance(total_tokens, int) or total_tokens <= 0:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_USAGE_INVALID",
                message="Codex CLI did not return positive token usage evidence.",
                metrics={**usage, "duration_ms": completed.duration_ms},
                data=safe_data,
                external_ref=thread_id,
            )
        if task.token_budget is not None and total_tokens > task.token_budget:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_TOKEN_BUDGET_EXCEEDED",
                message="Codex CLI usage exceeded the declared task budget; output was discarded.",
                metrics={**usage, "duration_ms": completed.duration_ms, "token_budget": task.token_budget},
                data=safe_data,
                external_ref=thread_id,
            )
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int) and output_tokens > max_output_tokens:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="RELAY_CODEX_CLI_OUTPUT_LIMIT_EXCEEDED",
                message="Codex CLI output exceeded the approved post-hoc limit; output was discarded.",
                metrics={
                    **usage,
                    "duration_ms": completed.duration_ms,
                    "posthoc_output_token_limit": max_output_tokens,
                },
                data=safe_data,
                external_ref=thread_id,
            )
        metrics = {
            **usage,
            "usage_total_source": str(parsed["usage_total_source"]),
            "duration_ms": completed.duration_ms,
            "estimated_input_tokens": estimated_input_tokens,
            "posthoc_output_token_limit": max_output_tokens,
            "output_token_limit_policy": CODEX_CLI_OUTPUT_LIMIT_POLICY,
            "token_budget": task.token_budget,
        }
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="Isolated Codex CLI relay review completed; staging validation is pending.",
            metrics=metrics,
            data={
                **safe_data,
                "output_text": output_text,
                "endpoint_base_url": QILIN_RELAY_BASE_URL,
                "endpoint_config_sha256": codex_cli_static_config_sha256(),
                "wire_api": QILIN_RELAY_WIRE_API,
                "provider_external_ref_kind": "codex_thread_id",
            },
            external_ref=thread_id,
        )

    def _process_failure(
        self,
        completed: CodexCLIProcessResult,
        parsed: dict[str, object],
        data: dict[str, object],
    ) -> TaskResult:
        http_status = parsed.get("provider_http_status")
        provider_code = str(parsed.get("provider_error_code") or "").lower()
        provider_type = str(parsed.get("provider_error_type") or "").lower()
        if http_status == 401 or provider_code in {"invalid_api_key", "authentication_error"}:
            code = "RELAY_API_AUTHORIZATION_FAILED"
            status = ResultStatus.BLOCKED
        elif http_status == 403 or provider_type in {"forbidden", "forbidden_error", "permission_error"}:
            code = "RELAY_CODEX_CLIENT_REJECTED"
            status = ResultStatus.BLOCKED
        elif http_status == 429 or provider_code in {"rate_limit_exceeded", "server_overloaded"}:
            code = "RELAY_API_RETRYABLE"
            status = ResultStatus.RETRYABLE_FAILURE
        elif provider_code in {"model_not_found", "model_unavailable", "unsupported_model"}:
            code = "RELAY_MODEL_UNAVAILABLE"
            status = ResultStatus.BLOCKED
        elif completed.returncode == 2 and int(parsed.get("turn_started_count") or 0) == 0:
            code = "RELAY_CODEX_CLI_CONFIG_INVALID"
            status = ResultStatus.FATAL_FAILURE
        elif isinstance(http_status, int) and http_status >= 500:
            code = "RELAY_API_RETRYABLE"
            status = ResultStatus.RETRYABLE_FAILURE
        else:
            code = "RELAY_API_REQUEST_FAILED"
            status = ResultStatus.RETRYABLE_FAILURE
        return TaskResult(
            status=status,
            blocker_code=code,
            message="Codex CLI relay request failed; raw provider output was not persisted.",
            metrics={"duration_ms": completed.duration_ms},
            data={**data, "http_status": http_status},
        )

    def _blocked(self, code: str, message: str, *, transport_policy: str) -> TaskResult:
        return TaskResult(
            status=ResultStatus.BLOCKED,
            blocker_code=code,
            message=message,
            data={
                "endpoint_origin": QILIN_RELAY_BASE_URL,
                "endpoint_base_url": QILIN_RELAY_BASE_URL,
                "endpoint_config_sha256": codex_cli_static_config_sha256(),
                "wire_api": QILIN_RELAY_WIRE_API,
                "transport_policy": transport_policy,
            },
        )

    @staticmethod
    def _safe_execution_data(
        *,
        model: str,
        version: str,
        binary_sha256: str,
        transport_policy: str,
        exit_code: int | None,
        error_type: str,
        event_count: int = 0,
        last_event_type: str = "",
        jsonl_sha256: str = "",
        tool_event_count: int = 0,
        stderr_sha256: str = "",
        stderr_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "model": model,
            "requested_model": model,
            "reported_model": "",
            "provider_response_id": "",
            "endpoint_origin": QILIN_RELAY_BASE_URL,
            "endpoint_base_url": QILIN_RELAY_BASE_URL,
            "endpoint_config_sha256": codex_cli_static_config_sha256(),
            "wire_api": QILIN_RELAY_WIRE_API,
            "transport_policy": transport_policy,
            "client_kind": CODEX_CLI_CLIENT_KIND,
            "client_provenance_assurance": CODEX_CLI_CLIENT_PROVENANCE_ASSURANCE,
            "client_config_profile": CODEX_CLI_CONFIG_PROFILE,
            "reasoning_effort": CODEX_CLI_REASONING_EFFORT,
            "reasoning_effort_namespace": CODEX_CLI_REASONING_NAMESPACE,
            "identity_policy": CODEX_CLI_IDENTITY_POLICY,
            "credential_policy": CODEX_CLI_CREDENTIAL_POLICY,
            "billing_policy": CODEX_CLI_BILLING_POLICY,
            "billing_account_verified": False,
            "chatgpt_subscription_used": False,
            "openai_api_account_used": False,
            "official_openai_endpoint_proven": False,
            "upstream_model_identity_verified": False,
            "model_identity_assurance": "relay_claim_unverified",
            "fallback_policy": CODEX_CLI_FALLBACK_POLICY,
            "output_token_limit_policy": CODEX_CLI_OUTPUT_LIMIT_POLICY,
            "codex_cli_version": version,
            "codex_cli_sha256": binary_sha256,
            "argv_policy_sha256": codex_cli_argv_policy_sha256(),
            "environment_policy": CODEX_CLI_ENV_POLICY,
            "jsonl_policy": CODEX_CLI_JSONL_POLICY,
            "jsonl_sha256": jsonl_sha256,
            "jsonl_event_count": event_count,
            "tool_event_count": tool_event_count,
            "last_event_type": last_event_type,
            "stderr_sha256": stderr_sha256,
            "stderr_bytes": stderr_bytes,
            "exit_code": exit_code,
            "error_type": error_type,
        }
