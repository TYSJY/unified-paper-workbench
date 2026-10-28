"""Stable local paths for optional commercial state."""

from __future__ import annotations

import os
from pathlib import Path


STATE_ENV = "UNIFIED_PAPER_COMMERCIAL_STATE_DIR"


def commercial_state_root() -> Path:
    override = str(os.environ.get(STATE_ENV) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "UnifiedPaperWorkbench" / "commercial"


def device_record_path() -> Path:
    return commercial_state_root() / "device.json"


def license_record_path() -> Path:
    return commercial_state_root() / "license.json"


def session_record_path() -> Path:
    return commercial_state_root() / "session.json"


def diagnostics_spool_dir() -> Path:
    return commercial_state_root() / "diagnostics"
