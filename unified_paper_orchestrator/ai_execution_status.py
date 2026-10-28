"""Secret-free, read-only diagnostics for the selectable AI execution modes."""

from __future__ import annotations

import importlib
import importlib.util
import json
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from .ai_provider_store import AIProviderSettings, AIProviderStore
from .codex_runtime import runtime_identity


STATUS_SCHEMA = "unified-paper-orchestrator/ai-execution-status/v1"
SDK_DISTRIBUTION = "openai-codex"
SDK_MODULE = "openai_codex"
RUNTIME_DISTRIBUTION = "openai-codex-cli-bin"
RUNTIME_MODULE = "codex_cli_bin"
PUBLIC_CAPABILITY_STATUSES = frozenset(
    {"compatible", "incompatible", "untested", "error"}
)


def _distribution_version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except (metadata.PackageNotFoundError, ValueError, OSError):
        return ""


def _safe_version(value: Any) -> str:
    version = str(value or "").strip()
    if not version or len(version) > 64 or any(ord(char) < 32 for char in version):
        return ""
    return version


def _module_path(module: str) -> tuple[str, bool]:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, AttributeError, ValueError):
        return "", False
    origin = str(getattr(spec, "origin", "") or "") if spec is not None else ""
    if not origin or origin in {"built-in", "frozen"}:
        return origin, bool(spec)
    path = Path(origin)
    try:
        return str(path), path.is_file()
    except OSError:
        return str(path), False


def _probe_codex_sdk() -> dict[str, Any]:
    version = _safe_version(_distribution_version(SDK_DISTRIBUTION))
    module_path, path_available = _module_path(SDK_MODULE)
    if not version and path_available:
        try:
            sdk_module = importlib.import_module(SDK_MODULE)
            version = _safe_version(getattr(sdk_module, "__version__", ""))
        except (ImportError, AttributeError, ValueError, OSError):
            version = ""
    return {
        "distribution": SDK_DISTRIBUTION,
        "module": SDK_MODULE,
        "version": version,
        "module_path": module_path,
        "path_available": path_available,
        "available": bool(version and path_available),
    }


def _probe_codex_runtime() -> dict[str, Any]:
    try:
        selected = runtime_identity()
    except (OSError, ValueError, TypeError):
        return {"distribution": RUNTIME_DISTRIBUTION, "module": RUNTIME_MODULE,
                "source": "application_pinned", "version": "", "available": False,
                "binary_path_available": False, "binary_path": ""}
    if selected is not None:
        return {"distribution": RUNTIME_DISTRIBUTION, "module": RUNTIME_MODULE,
                "source": selected["source"], "version": selected["version"],
                "module_path": selected["directory"], "module_path_available": True,
                "binary_path": selected["binary_path"], "binary_path_available": True,
                "available": True}
    version = _safe_version(_distribution_version(RUNTIME_DISTRIBUTION))
    module_path, module_path_available = _module_path(RUNTIME_MODULE)
    binary_path = ""
    binary_path_available = False
    runtime_module: Any | None = None
    if module_path_available:
        try:
            runtime_module = importlib.import_module(RUNTIME_MODULE)
        except (ImportError, AttributeError, ValueError, OSError):
            runtime_module = None
    if runtime_module is not None:
        if not version:
            try:
                metadata_path = Path(module_path).parent / "codex-package.json"
                package_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(package_metadata, dict):
                    version = _safe_version(package_metadata.get("version"))
            except (OSError, UnicodeError, ValueError):
                version = ""
        try:
            resolver = getattr(runtime_module, "bundled_codex_path", None)
            candidate = resolver() if callable(resolver) else None
            if candidate is not None:
                path = Path(candidate)
                binary_path = str(path)
                binary_path_available = path.is_file()
        except (AttributeError, TypeError, ValueError, OSError):
            binary_path = ""
            binary_path_available = False
    return {
        "distribution": RUNTIME_DISTRIBUTION,
        "module": RUNTIME_MODULE,
        "version": version,
        "module_path": module_path,
        "module_path_available": module_path_available,
        "binary_path": binary_path,
        "binary_path_available": binary_path_available,
        "available": bool(version and module_path_available and binary_path_available),
    }


def _unavailable_profile(blocker: str) -> dict[str, Any]:
    return {
        "configured": False,
        "record_available": False,
        "capability_status": "unknown",
        "credential_present": False,
        "ready": False,
        "blockers": [blocker],
    }


def _read_active_profile(store: AIProviderStore, active_profile_id: str) -> dict[str, Any]:
    if not active_profile_id:
        return _unavailable_profile("ACTIVE_PROFILE_NOT_SELECTED")

    result = {
        "configured": True,
        "record_available": False,
        "capability_status": "unknown",
        "credential_present": False,
        "ready": False,
        "blockers": [],
    }
    try:
        profile = store.get_profile(active_profile_id)
    except Exception as exc:  # The diagnostic must never echo store exception text.
        result["blockers"] = ["ACTIVE_PROFILE_UNAVAILABLE"]
        result["error_type"] = type(exc).__name__
        return result

    result["record_available"] = True
    observed_capability = str(profile.capability_status or "unknown").strip().lower()
    capability_status = (
        observed_capability
        if observed_capability in PUBLIC_CAPABILITY_STATUSES
        else "unknown"
    )
    result["capability_status"] = capability_status
    try:
        credential_present = bool(store.has_api_key(active_profile_id))
    except Exception as exc:  # Do not resolve or display the protected value.
        result["blockers"] = ["ACTIVE_PROFILE_CREDENTIAL_STATUS_UNAVAILABLE"]
        result["error_type"] = type(exc).__name__
        return result
    result["credential_present"] = credential_present

    blockers: list[str] = []
    if str(profile.wire_api or "").strip().lower() != "responses":
        blockers.append("ACTIVE_PROFILE_WIRE_API_UNSUPPORTED")
    if capability_status != "compatible":
        blockers.append("ACTIVE_PROFILE_COMPATIBILITY_TEST_REQUIRED")
    if not credential_present:
        blockers.append("ACTIVE_PROFILE_CREDENTIAL_MISSING")
    result["blockers"] = blockers
    return result


def _probe_legacy_bridge() -> dict[str, Any]:
    """Inspect only public legacy TOML files; never repair or provision them."""

    try:
        from .automation import AUTOMATIONS_ROOT, SchedulerAutomationController, parse_toml_like

        raw = SchedulerAutomationController().status_all()
        stages = {
            stage: {
                "configured": bool(record.get("exists")),
                "status": str(record.get("status") or "MISSING").strip().upper(),
            }
            for stage, record in raw.items()
            if isinstance(record, dict)
        }
        smart = parse_toml_like(
            Path.home() / ".codex" / "automations" / "automation" / "automation.toml"
        )
    except Exception as exc:
        return {
            "ready": False,
            "four_stage_configured_count": 0,
            "stages": {},
            "smart_submission": {"configured": False, "status": "UNKNOWN"},
            "blockers": ["LEGACY_BRIDGE_STATUS_UNAVAILABLE"],
            "error_type": type(exc).__name__,
        }
    configured_count = sum(
        1 for item in stages.values() if item.get("configured") is True
    )
    consumer_verified = False
    registration: dict[str, Any] = {}
    if configured_count == 4:
        try:
            from .legacy_automation_registration import verify_official_registration

            registration = verify_official_registration(AUTOMATIONS_ROOT)
        except Exception as exc:
            registration = {
                "verified": False,
                "blockers": ["LEGACY_AUTOMATION_REGISTRATION_STATUS_UNAVAILABLE"],
                "error_type": type(exc).__name__,
            }
        consumer_verified = bool(registration.get("verified"))
        blockers = [] if consumer_verified else ["LEGACY_AUTOMATION_CONSUMER_UNVERIFIED"]
    else:
        blockers = ["LEGACY_FOUR_STAGE_AUTOMATION_MISSING"]
    return {
        "ready": configured_count == 4 and consumer_verified,
        "consumer_verified": consumer_verified,
        "consumer_probe_verified": bool(registration.get("consumer_probe_verified")),
        "control_plane": (
            "official_codex_automation"
            if consumer_verified
            else "legacy_automation_toml"
        ),
        "four_stage_configured_count": configured_count,
        "stages": stages,
        "smart_submission": {
            "configured": bool(smart.get("exists")),
            "status": str(smart.get("status") or "MISSING").strip().upper(),
        },
        "blockers": blockers,
    }


def collect_ai_execution_status(
    store: AIProviderStore | None = None,
    *,
    sdk_probe: Callable[[], dict[str, Any]] | None = None,
    runtime_probe: Callable[[], dict[str, Any]] | None = None,
    legacy_probe: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect a bounded status snapshot without resolving credentials or starting a process."""

    provider_store = store or AIProviderStore()
    sdk = dict((sdk_probe or _probe_codex_sdk)())
    runtime = dict((runtime_probe or _probe_codex_runtime)())
    legacy_bridge = dict((legacy_probe or _probe_legacy_bridge)())

    try:
        # Avoid creating even a transient lock directory on a fresh install.
        # A missing settings file has the store's documented legacy default.
        settings = (
            provider_store.load_settings()
            if provider_store.settings_path.exists()
            else AIProviderSettings()
        )
    except Exception as exc:
        return {
            "schema": STATUS_SCHEMA,
            "ok": False,
            "error_code": "AI_EXECUTION_SETTINGS_UNAVAILABLE",
            "error_type": type(exc).__name__,
            "mode": {
                "current": "unknown",
                "settings_readable": False,
                "active_mode_ready": False,
            },
            "active_profile": _unavailable_profile("AI_EXECUTION_SETTINGS_UNAVAILABLE"),
            "codex_sdk": sdk,
            "codex_runtime": runtime,
            "legacy_bridge": legacy_bridge,
            "execution_scope": _execution_scope(),
            "safety": _safety_summary(),
        }

    execution_mode = str(settings.execution_mode or "").strip().lower()
    active_profile = _read_active_profile(
        provider_store,
        str(settings.active_profile_id or "").strip(),
    )
    dependency_blockers: list[str] = []
    if not bool(sdk.get("available")):
        dependency_blockers.append("CODEX_SDK_UNAVAILABLE")
    if not bool(runtime.get("available")):
        dependency_blockers.append("CODEX_RUNTIME_UNAVAILABLE")
    active_profile["blockers"] = [
        *list(active_profile.get("blockers") or []),
        *dependency_blockers,
    ]
    active_profile["ready"] = not active_profile["blockers"]
    # ``legacy_codex`` is the persisted compatibility identifier for the
    # current Codex-account execution mode.  Production work no longer depends
    # on the historical Desktop Automation consumer: all four stages are
    # dispatched by the EXE's local Codex SDK runtimes.  Keep probing the old
    # bridge for read-only rollback diagnostics, but never use it as the
    # readiness gate for current work.
    active_mode_ready = (
        not dependency_blockers
        if execution_mode == "legacy_codex"
        else bool(active_profile["ready"])
    )

    return {
        "schema": STATUS_SCHEMA,
        "ok": True,
        "mode": {
            "current": execution_mode,
            "settings_readable": True,
            "active_mode_ready": active_mode_ready,
        },
        "active_profile": active_profile,
        "codex_sdk": sdk,
        "codex_runtime": runtime,
        "legacy_bridge": legacy_bridge,
        "execution_scope": _execution_scope(),
        "safety": _safety_summary(),
    }


def _execution_scope() -> dict[str, Any]:
    return {
        "api_connection_test": True,
        "api_prompt07_private_staging_review": True,
        "api_full_four_stage_execution": False,
        "codex_account_full_four_stage_execution": True,
        "writing_execution_owner": "local_codex_sdk_event_dispatcher",
        "figures_execution_owner": "local_codex_sdk_workflow",
        "journal_author_execution_owner": "local_codex_sdk_workflow",
        "smart_submission_execution_owner": "program_controlled_codex_sdk_browser_workflow",
        "writing_account_required": True,
        "writing_account_checked": False,
        "codex_account_verification": "required_at_dispatch",
        "legacy_automation_role": "paused_compatibility_fallback",
        "remaining_v1_stage_owner": "none",
        "full_four_stage_execution_owner": "local_codex_sdk_workflows",
    }


def _safety_summary() -> dict[str, Any]:
    return {
        "read_only": True,
        "network_attempted": False,
        "process_started": False,
        "credential_value_resolved": False,
        "v2_database_opened": False,
    }


__all__ = ["STATUS_SCHEMA", "collect_ai_execution_status"]
