from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .config import LEGACY_PROJECTS, STATE_DIR
from .io_utils import now_iso, read_json, write_json_atomic


REGISTRATION_SCHEMA = "unified-paper-orchestrator/v1-automation-registration/v1"
OFFICIAL_REGISTRATION_SOURCE = "codex_app_automation_update"
REGISTRATION_PATH = STATE_DIR / "v1_automation_registration.json"
_THREAD_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)


def _normalized_path(value: Any) -> str:
    text = str(value or "").strip()
    return os.path.normcase(os.path.normpath(text)) if text else ""


def expected_automation_project(stage: str) -> Path:
    root = Path(LEGACY_PROJECTS[stage]["root"])
    return root.parent if stage in {"writing", "figures"} else root


def automation_projection(stage: str, path: Path) -> dict[str, Any]:
    """Return immutable fields emitted by the supported Codex app registrar."""

    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    raw_cwds = data.get("cwds") if isinstance(data.get("cwds"), list) else []
    return {
        "id": str(data.get("id") or "").strip(),
        "kind": str(data.get("kind") or "").strip().lower(),
        "execution_environment": str(data.get("execution_environment") or "").strip().lower(),
        "target_type": str(target.get("type") or "").strip().lower(),
        "target_project_id": _normalized_path(target.get("project_id")),
        "cwds": sorted({_normalized_path(item) for item in raw_cwds if _normalized_path(item)}),
        "model": str(data.get("model") or "").strip(),
        "reasoning_effort": str(data.get("reasoning_effort") or "").strip().lower(),
    }


def expected_projection(stage: str) -> dict[str, Any]:
    project = expected_automation_project(stage)
    return {
        "id": str(LEGACY_PROJECTS[stage]["primary_automation_id"]),
        "kind": "cron",
        "execution_environment": "local",
        "target_type": "project",
        "target_project_id": _normalized_path(project),
        "cwds": [_normalized_path(project)],
        "model": "gpt-5.5",
        "reasoning_effort": "xhigh",
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_official_registration_receipt(
    automation_root: Path,
    *,
    consumer_probe: Mapping[str, Any],
    desktop_package_version: str = "",
    destination: Path | None = None,
) -> dict[str, Any]:
    """Persist an attestation only after an app-registered automation really ran.

    The full TOML hash is retained for audit, not readiness: status, schedule,
    launcher text and timestamps are expected to change during normal EXE use.
    """

    root = Path(automation_root)
    automations: dict[str, Any] = {}
    for stage, project in LEGACY_PROJECTS.items():
        automation_id = str(project["primary_automation_id"])
        path = root / automation_id / "automation.toml"
        projection = automation_projection(stage, path)
        if projection != expected_projection(stage):
            raise ValueError(f"Official automation projection is invalid for stage: {stage}")
        automations[stage] = {
            "projection": projection,
            "registration_file_sha256": _file_sha256(path),
        }

    probe_stage = str(consumer_probe.get("stage") or "").strip()
    probe_thread_id = str(consumer_probe.get("thread_id") or "").strip()
    probe_command_id = str(consumer_probe.get("command_id") or "").strip()
    probe_handled_at = str(consumer_probe.get("handled_at") or "").strip()
    if (
        probe_stage not in automations
        or _THREAD_ID.fullmatch(probe_thread_id) is None
        or not probe_command_id
        or not probe_handled_at
        or str(consumer_probe.get("result") or "").strip() != "consumed"
    ):
        raise ValueError("A bounded successful Codex Automation consumer probe is required")
    memory_path = root / automations[probe_stage]["projection"]["id"] / "memory.md"
    if not memory_path.is_file() or memory_path.stat().st_size <= 0:
        raise ValueError("Codex Automation consumer memory evidence is missing")

    receipt = {
        "schema": REGISTRATION_SCHEMA,
        "source_attestation": OFFICIAL_REGISTRATION_SOURCE,
        "registered_at": now_iso(),
        "desktop_package_version": str(desktop_package_version or "").strip(),
        "automations": automations,
        "consumer_probe": {
            "stage": probe_stage,
            "automation_id": automations[probe_stage]["projection"]["id"],
            "thread_id": probe_thread_id,
            "command_id": probe_command_id,
            "handled_at": probe_handled_at,
            "result": "consumed",
            "memory_present": True,
        },
    }
    write_json_atomic(Path(destination or REGISTRATION_PATH), receipt)
    return receipt


def verify_official_registration(
    automation_root: Path,
    *,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(automation_root)
    receipt = read_json(Path(receipt_path or REGISTRATION_PATH), {})
    blockers: list[str] = []
    if not isinstance(receipt, dict) or receipt.get("schema") != REGISTRATION_SCHEMA:
        blockers.append("LEGACY_AUTOMATION_REGISTRATION_RECEIPT_MISSING")
        receipt = {}
    if receipt.get("source_attestation") != OFFICIAL_REGISTRATION_SOURCE:
        blockers.append("LEGACY_AUTOMATION_REGISTRATION_SOURCE_UNVERIFIED")

    registered = receipt.get("automations") if isinstance(receipt.get("automations"), dict) else {}
    stage_results: dict[str, bool] = {}
    for stage, project in LEGACY_PROJECTS.items():
        automation_id = str(project["primary_automation_id"])
        path = root / automation_id / "automation.toml"
        try:
            observed = automation_projection(stage, path)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
            observed = {}
        record = registered.get(stage) if isinstance(registered.get(stage), dict) else {}
        recorded_projection = record.get("projection") if isinstance(record.get("projection"), dict) else {}
        verified = observed == expected_projection(stage) and recorded_projection == observed
        stage_results[stage] = verified
        if not verified:
            blockers.append(f"LEGACY_AUTOMATION_REGISTRATION_INVALID_{stage.upper()}")

    probe = receipt.get("consumer_probe") if isinstance(receipt.get("consumer_probe"), dict) else {}
    probe_stage = str(probe.get("stage") or "").strip()
    probe_id = str(probe.get("automation_id") or "").strip()
    probe_thread_id = str(probe.get("thread_id") or "").strip()
    memory_path = root / probe_id / "memory.md" if probe_id else root / "missing" / "memory.md"
    probe_verified = (
        probe_stage in stage_results
        and stage_results.get(probe_stage) is True
        and probe_id == str(LEGACY_PROJECTS[probe_stage]["primary_automation_id"])
        and _THREAD_ID.fullmatch(probe_thread_id) is not None
        and str(probe.get("result") or "") == "consumed"
        and bool(str(probe.get("command_id") or "").strip())
        and bool(str(probe.get("handled_at") or "").strip())
        and memory_path.is_file()
        and memory_path.stat().st_size > 0
    )
    if not probe_verified:
        blockers.append("LEGACY_AUTOMATION_CONSUMER_PROBE_UNVERIFIED")

    blockers = list(dict.fromkeys(blockers))
    return {
        "verified": not blockers and all(stage_results.values()) and probe_verified,
        "stages": stage_results,
        "consumer_probe_verified": probe_verified,
        "blockers": blockers,
    }


__all__ = [
    "OFFICIAL_REGISTRATION_SOURCE",
    "REGISTRATION_PATH",
    "REGISTRATION_SCHEMA",
    "automation_projection",
    "create_official_registration_receipt",
    "expected_automation_project",
    "expected_projection",
    "verify_official_registration",
]
