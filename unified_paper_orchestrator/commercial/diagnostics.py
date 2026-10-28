"""Privacy-minimised diagnostic events.

The diagnostic contract intentionally has no fields for manuscript content,
titles, authors, e-mail addresses, local paths, prompts, credentials, cookies,
tokens, browser HTML, or raw exception messages.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from .contracts import ContractError, format_utc


DIAGNOSTIC_SCHEMA = "uap.diagnostic/v1"
SEVERITIES = {"info", "warning", "error", "fatal"}
TOP_LEVEL_FIELDS = {
    "schema",
    "event_id",
    "occurred_at",
    "app_version",
    "event_type",
    "severity",
    "error_code",
    "component",
    "operation",
    "duration_ms",
    "os_family",
    "release_channel",
    "license_state",
    "anonymous_installation_id",
    "metadata",
}
METADATA_FIELDS = {
    "adapter",
    "exception_class",
    "feature",
    "http_status",
    "retry_count",
    "ui_action",
    "workflow_stage",
}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{0,160}$")


def _safe_text(value: Any, field: str, *, max_length: int = 160) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be text.")
    clean = value.strip()
    if len(clean) > max_length or not _SAFE_TOKEN.fullmatch(clean):
        raise ContractError(f"{field} contains non-diagnostic or sensitive text.")
    if "@" in clean or "\\" in clean or clean.lower().startswith(("bearer", "sk-")):
        raise ContractError(f"{field} appears to contain sensitive data.")
    return clean


def _safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(metadata or {})
    unknown = set(source) - METADATA_FIELDS
    if unknown:
        raise ContractError(f"Diagnostic metadata is not allowlisted: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, value in source.items():
        if key in {"http_status", "retry_count"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"metadata.{key} must be a non-negative integer.")
            result[key] = value
        else:
            result[key] = _safe_text(value, f"metadata.{key}")
    return result


def build_diagnostic_event(
    *,
    app_version: str,
    event_type: str,
    severity: str,
    error_code: str,
    component: str,
    operation: str,
    os_family: str,
    release_channel: str,
    license_state: str,
    anonymous_installation_id: str,
    duration_ms: int = 0,
    metadata: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    if severity not in SEVERITIES:
        raise ContractError("Diagnostic severity is invalid.")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ContractError("duration_ms must be a non-negative integer.")
    event = {
        "schema": DIAGNOSTIC_SCHEMA,
        "event_id": f"diag-{uuid4()}",
        "occurred_at": format_utc(occurred_at or datetime.now(timezone.utc)),
        "app_version": _safe_text(app_version, "app_version"),
        "event_type": _safe_text(event_type, "event_type"),
        "severity": severity,
        "error_code": _safe_text(error_code, "error_code"),
        "component": _safe_text(component, "component"),
        "operation": _safe_text(operation, "operation"),
        "duration_ms": duration_ms,
        "os_family": _safe_text(os_family, "os_family"),
        "release_channel": _safe_text(release_channel, "release_channel"),
        "license_state": _safe_text(license_state, "license_state"),
        "anonymous_installation_id": _safe_text(
            anonymous_installation_id,
            "anonymous_installation_id",
        ),
        "metadata": _safe_metadata(metadata),
    }
    validate_diagnostic_event(event)
    return event


def validate_diagnostic_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("Diagnostic event must be an object.")
    data = dict(value)
    if set(data) != TOP_LEVEL_FIELDS:
        raise ContractError("Diagnostic event fields do not match the allowlist.")
    if data.get("schema") != DIAGNOSTIC_SCHEMA:
        raise ContractError("Unsupported diagnostic schema.")
    if data.get("severity") not in SEVERITIES:
        raise ContractError("Diagnostic severity is invalid.")
    if isinstance(data.get("duration_ms"), bool) or not isinstance(data.get("duration_ms"), int):
        raise ContractError("Diagnostic duration is invalid.")
    for field in TOP_LEVEL_FIELDS - {"schema", "severity", "duration_ms", "metadata"}:
        _safe_text(data.get(field), field)
    if not isinstance(data.get("metadata"), Mapping):
        raise ContractError("Diagnostic metadata must be an object.")
    data["metadata"] = _safe_metadata(data["metadata"])
    return data
