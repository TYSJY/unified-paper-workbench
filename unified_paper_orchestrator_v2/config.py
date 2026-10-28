from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def resolve_repo_root() -> Path:
    override = os.environ.get("UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() in {"dist", "dist_v2"} else exe_dir
    return Path(__file__).resolve().parents[1]


REPO_ROOT = resolve_repo_root()


def resolve_state_root() -> Path:
    override = os.environ.get("UNIFIED_PAPER_ORCHESTRATOR_V2_STATE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    try:
        from unified_paper_orchestrator.config import STATE_DIR as active_state_dir
    except (ImportError, RuntimeError, OSError):
        return REPO_ROOT / "state"
    return Path(active_state_dir).expanduser().resolve()


STATE_ROOT = resolve_state_root()
STATE_DIR = STATE_ROOT / "v2"
DATABASE_PATH = STATE_DIR / "orchestrator.sqlite3"
API_TOKEN_PATH = STATE_DIR / "api_token.local"
LEGACY_UNIFIED_STATE_PATH = STATE_ROOT / "unified_state.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def ensure_runtime() -> dict[str, str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not API_TOKEN_PATH.exists():
        API_TOKEN_PATH.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        try:
            os.chmod(API_TOKEN_PATH, 0o600)
        except OSError:
            pass
    return {
        "state_dir": str(STATE_DIR),
        "database_path": str(DATABASE_PATH),
        "api_token_path": str(API_TOKEN_PATH),
    }


def read_api_token() -> str:
    ensure_runtime()
    return API_TOKEN_PATH.read_text(encoding="utf-8").strip()
