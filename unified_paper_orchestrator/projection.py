from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import (
    SMART_SUBMISSION_STATE_PATH,
    UNIFIED_PROJECTION_DIRTY_PATH,
    UNIFIED_PROJECTION_LOCK_PATH,
    UNIFIED_STATE_PATH,
)
from .io_utils import FileLock, JsonReadError, now_iso, read_json, write_json_atomic


PROJECTION_DIRTY_SCHEMA = 1
PROJECTION_REFRESH_FAILED = "UNIFIED_PROJECTION_REFRESH_FAILED"
PROJECTION_REFRESH_DEFERRED = "UNIFIED_PROJECTION_REFRESH_DEFERRED"
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def dirty_path_for(unified_state_path: Path) -> Path:
    return Path(unified_state_path).with_name(UNIFIED_PROJECTION_DIRTY_PATH.name)


def lock_path_for(dirty_path: Path) -> Path:
    return Path(dirty_path).with_name(UNIFIED_PROJECTION_LOCK_PATH.name)


def canonical_projection_sources(unified_state_path: Path, smart_state_path: Path) -> bool:
    return _same_path(unified_state_path, UNIFIED_STATE_PATH) and _same_path(
        smart_state_path,
        SMART_SUBMISSION_STATE_PATH,
    )


def mark_projection_dirty(
    reason: str,
    *,
    token: str | None = None,
    dirty_path: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Durably record that the human-readable unified projections are stale.

    The marker deliberately contains no paper ids, status details, exception
    messages, paths, credentials, or other state values.  It is written before
    the Smart state commit, so a later projection failure cannot undo a
    successful Smart transaction.
    """

    path = Path(dirty_path or UNIFIED_PROJECTION_DIRTY_PATH)
    guard = Path(lock_path or lock_path_for(path))
    safe_reason = str(reason or "").strip().lower()
    if not _SAFE_REASON.fullmatch(safe_reason):
        safe_reason = "smart_state_changed"

    with FileLock(guard, stale_seconds=5 * 60):
        generation = 1
        marked_at = now_iso()
        try:
            current = read_json(path, {})
        except JsonReadError:
            current = {}
        if isinstance(current, dict):
            try:
                generation = max(0, int(current.get("generation") or 0)) + 1
            except (TypeError, ValueError):
                generation = 1
            marked_at = str(current.get("marked_at") or marked_at)
        requested_token = str(token or "").strip().lower()
        marker_token = requested_token if re.fullmatch(r"[0-9a-f]{32}", requested_token) else uuid4().hex
        marker = {
            "schema": PROJECTION_DIRTY_SCHEMA,
            "dirty": True,
            "token": marker_token,
            "generation": generation,
            "reason": safe_reason,
            "marked_at": marked_at,
        }
        write_json_atomic(path, marker)
    return {**_public_marker(marker), "token": str(marker["token"])}


def projection_dirty_snapshot(
    *,
    dirty_path: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any] | None:
    path = Path(dirty_path or UNIFIED_PROJECTION_DIRTY_PATH)
    guard = Path(lock_path or lock_path_for(path))
    with FileLock(guard, stale_seconds=5 * 60):
        return _snapshot_unlocked(path)


def acknowledge_projection_refresh(
    snapshot: dict[str, Any] | None,
    *,
    dirty_path: Path | None = None,
    lock_path: Path | None = None,
) -> bool:
    """Clear only the exact dirty marker observed before the projection read."""

    if not snapshot:
        return False
    path = Path(dirty_path or UNIFIED_PROJECTION_DIRTY_PATH)
    guard = Path(lock_path or lock_path_for(path))
    with FileLock(guard, stale_seconds=5 * 60):
        current = _snapshot_unlocked(path)
        if not current or not _same_snapshot(snapshot, current):
            return False
        path.unlink(missing_ok=True)
        return True


def refresh_unified_projection_if_dirty(
    *,
    force: bool = False,
    sync_callback: Callable[[], Any] | None = None,
    ready_callback: Callable[[dict[str, Any]], bool] | None = None,
    dirty_path: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Best-effort, idempotent refresh that never raises into a Smart commit."""

    path = Path(dirty_path or UNIFIED_PROJECTION_DIRTY_PATH)
    guard = Path(lock_path or lock_path_for(path))
    try:
        snapshot = projection_dirty_snapshot(dirty_path=path, lock_path=guard)
    except Exception as exc:  # marker I/O must not retroactively fail Smart work
        return _failure_result(exc, dirty=True)
    if snapshot is None and not force:
        return {"status": "clean", "dirty": False, "refreshed": False}
    if snapshot is not None and ready_callback is not None:
        try:
            source_ready = bool(ready_callback(snapshot))
        except Exception as exc:
            return _failure_result(exc, dirty=True)
        if not source_ready:
            return {
                "status": "pending_commit",
                "dirty": True,
                "refreshed": False,
                "code": PROJECTION_REFRESH_DEFERRED,
                **_public_marker(snapshot),
            }

    callback = sync_callback or _default_sync_callback
    try:
        callback()
    except Exception as exc:  # JSON may be fresh while Markdown failed; keep dirty
        return _failure_result(exc, dirty=True)

    if snapshot is not None:
        try:
            acknowledge_projection_refresh(snapshot, dirty_path=path, lock_path=guard)
        except Exception as exc:
            return _failure_result(exc, dirty=True)

    try:
        remaining = projection_dirty_snapshot(dirty_path=path, lock_path=guard)
    except Exception as exc:
        return _failure_result(exc, dirty=True)
    still_dirty = remaining is not None
    result = {
        "status": "dirty" if still_dirty else "refreshed",
        "dirty": still_dirty,
        "refreshed": not still_dirty,
    }
    if snapshot is not None:
        result.update(_public_marker(snapshot))
    return result


def deferred_projection_refresh(
    *,
    dirty_path: Path,
) -> dict[str, Any]:
    """Return a value-free result for intentionally isolated/noncanonical tests."""

    try:
        dirty = projection_dirty_snapshot(dirty_path=dirty_path) is not None
    except Exception:
        dirty = True
    return {
        "status": "deferred",
        "dirty": dirty,
        "refreshed": False,
        "code": PROJECTION_REFRESH_DEFERRED,
    }


def _default_sync_callback() -> None:
    # Lazy import avoids a projection <-> orchestrator import cycle.
    from .orchestrator import UnifiedOrchestrator

    # Projection refresh must not reconcile or mutate an unrelated writing
    # controller merely because Smart state changed.
    UnifiedOrchestrator().sync(reconcile_writing=False)


def _snapshot_unlocked(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    raw = path.read_bytes()
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        marker = read_json(path, {})
    except JsonReadError:
        return {"raw_sha256": raw_sha256, "corrupt": True}
    if not isinstance(marker, dict):
        return {"raw_sha256": raw_sha256, "corrupt": True}
    token = str(marker.get("token") or "")
    if marker.get("dirty") is not True or not re.fullmatch(r"[0-9a-f]{32}", token):
        return {"raw_sha256": raw_sha256, "corrupt": True}
    return {
        "raw_sha256": raw_sha256,
        "token": token,
        "generation": _safe_int(marker.get("generation")),
        "reason": _safe_reason(marker.get("reason")),
        "marked_at": str(marker.get("marked_at") or ""),
        "corrupt": False,
    }


def _same_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(left.get("raw_sha256")) and left.get("raw_sha256") == right.get("raw_sha256")


def _public_marker(marker: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation": _safe_int(marker.get("generation")),
        "reason": _safe_reason(marker.get("reason")),
    }


def _safe_reason(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_REASON.fullmatch(text) else "smart_state_changed"


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _failure_result(exc: Exception, *, dirty: bool) -> dict[str, Any]:
    # Exception messages can contain paths, paper metadata, or secrets.  Only
    # the stable code and exception class cross this value-free boundary.
    return {
        "status": "deferred",
        "dirty": dirty,
        "refreshed": False,
        "code": PROJECTION_REFRESH_FAILED,
        "error_type": type(exc).__name__,
    }


def _same_path(left: Path, right: Path) -> bool:
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(left) == str(right)
