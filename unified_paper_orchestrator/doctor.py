from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .config import (
    BACKUP_DIR,
    LEGACY_PROJECTS,
    LOCK_PATH,
    SMART_SUBMISSION_STATE_PATH,
    STATE_DIR,
    UNIFIED_PROMPT_PATH,
    UNIFIED_STATE_PATH,
    UNIFIED_SUMMARY_PATH,
    WORKSPACE_ROOT as REPO_ROOT,
)
from .io_utils import read_json
from .orchestrator import UnifiedOrchestrator


def _state_schema_issues(data: Any, *, smart: bool = False, required: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(data, dict):
        return ["root must be a JSON object"]
    issues: list[str] = []
    for key in required:
        if key not in data:
            issues.append(f"missing required field: {key}")
    if "papers" in data and not isinstance(data.get("papers"), dict):
        issues.append("papers must be an object")
    for key in (("online_queue", "paper_queue", "history") if smart else ("paper_queue", "history")):
        if key in data and not isinstance(data.get(key), list):
            issues.append(f"{key} must be an array")
    if "settings" in data and not isinstance(data.get("settings"), dict):
        issues.append("settings must be an object")
    return issues


def _read_state_check(
    checks: list[dict[str, Any]],
    name: str,
    path: Path,
    *,
    smart: bool = False,
    required: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    try:
        data = read_json(path, {})
    except Exception as exc:
        checks.append({"name": f"{name}.state_readable", "ok": False, "path": str(path), "error": str(exc)})
        checks.append({"name": f"{name}.state_schema", "ok": False, "path": str(path), "issues": ["unreadable"]})
        return None
    checks.append({"name": f"{name}.state_readable", "ok": True, "path": str(path)})
    issues = _state_schema_issues(data, smart=smart, required=required)
    checks.append({"name": f"{name}.state_schema", "ok": not issues, "path": str(path), "issues": issues})
    return data if isinstance(data, dict) else None


def run_doctor() -> dict[str, Any]:
    """Inspect the workspace without repairing or writing any state."""

    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    for stage, project in LEGACY_PROJECTS.items():
        root = Path(project["root"])
        state_path = Path(project["state_path"])
        prompt_path = Path(project["prompt_path"])
        checks.append({"name": f"{stage}.root_exists", "ok": root.exists(), "path": str(root)})
        checks.append({"name": f"{stage}.state_exists", "ok": state_path.exists(), "path": str(state_path)})
        checks.append({"name": f"{stage}.prompt_exists", "ok": prompt_path.exists(), "path": str(prompt_path)})
        if state_path.exists():
            _read_state_check(checks, stage, state_path)

    checks.append(
        {
            "name": "smart_submission.state_exists",
            "ok": SMART_SUBMISSION_STATE_PATH.exists(),
            "path": str(SMART_SUBMISSION_STATE_PATH),
        }
    )
    smart_state = None
    if SMART_SUBMISSION_STATE_PATH.exists():
        smart_state = _read_state_check(
            checks,
            "smart_submission",
            SMART_SUBMISSION_STATE_PATH,
            smart=True,
            required=("papers", "paper_queue", "online_queue", "history"),
        )
        checks.append(
            {
                "name": "smart_submission.state_size",
                "ok": True,
                "path": str(SMART_SUBMISSION_STATE_PATH),
                "bytes": SMART_SUBMISSION_STATE_PATH.stat().st_size,
                "paper_count": len(smart_state.get("papers", {})) if isinstance(smart_state, dict) and isinstance(smart_state.get("papers"), dict) else 0,
            }
        )

    checks.append({"name": "unified.state_exists", "ok": UNIFIED_STATE_PATH.exists(), "path": str(UNIFIED_STATE_PATH)})
    if UNIFIED_STATE_PATH.exists():
        _read_state_check(checks, "unified", UNIFIED_STATE_PATH, required=("papers", "paper_queue", "history"))
    checks.append({"name": "unified.summary_exists", "ok": UNIFIED_SUMMARY_PATH.exists(), "path": str(UNIFIED_SUMMARY_PATH)})
    checks.append({"name": "unified.prompt_exists", "ok": UNIFIED_PROMPT_PATH.exists(), "path": str(UNIFIED_PROMPT_PATH)})
    checks.append({"name": "unified.lock_clear", "ok": not LOCK_PATH.exists(), "path": str(LOCK_PATH)})
    checks.append({"name": "unified.backup_dir_ready", "ok": BACKUP_DIR.parent.exists(), "path": str(BACKUP_DIR)})

    orchestrator = UnifiedOrchestrator()
    try:
        snapshot = orchestrator.sync(persist=False, reconcile_writing=False)
        checks.append(
            {
                "name": "unified.projection_readable",
                "ok": True,
                "paper_count": len(snapshot.get("papers", {})),
                "queue_count": len(snapshot.get("paper_queue", [])),
            }
        )
        bad_queue = [paper_id for paper_id in snapshot.get("paper_queue", []) if paper_id not in snapshot.get("papers", {})]
        checks.append({"name": "unified.queue_refs_valid", "ok": not bad_queue, "bad_queue": bad_queue})
    except Exception as exc:
        snapshot = {}
        checks.append({"name": "unified.projection_readable", "ok": False, "error": str(exc)})

    tmp_files = [path for path in STATE_DIR.rglob("*.tmp") if path.is_file()] if STATE_DIR.exists() else []
    tmp_bytes = sum(path.stat().st_size for path in tmp_files)
    checks.append(
        {
            "name": "state.orphan_tmp_files",
            "ok": not tmp_files,
            "severity": "warning",
            "count": len(tmp_files),
            "bytes": tmp_bytes,
            "paths": [str(path) for path in sorted(tmp_files, key=lambda item: item.stat().st_size, reverse=True)[:20]],
        }
    )
    if tmp_files:
        warnings.append(f"Found {len(tmp_files)} state temp files using {tmp_bytes} bytes; inspect before cleanup.")

    try:
        disk = shutil.disk_usage(REPO_ROOT)
        minimum_free = max(1024**3, int(disk.total * 0.05))
        disk_ok = disk.free >= minimum_free
        checks.append(
            {
                "name": "workspace.disk_free",
                "ok": disk_ok,
                "severity": "warning",
                "total_bytes": disk.total,
                "free_bytes": disk.free,
                "minimum_free_bytes": minimum_free,
            }
        )
        if not disk_ok:
            warnings.append(f"Workspace disk is low: {disk.free} bytes free; recommended minimum is {minimum_free}.")
    except OSError as exc:
        checks.append({"name": "workspace.disk_free", "ok": False, "severity": "warning", "error": str(exc)})
        warnings.append(f"Could not inspect workspace disk usage: {exc}")

    try:
        automation_status = orchestrator._scheduler_status_read_only()
    except Exception as exc:
        automation_status = {}
        warnings.append(f"Could not inspect automation status: {exc}")
    for stage in LEGACY_PROJECTS:
        config = automation_status.get(stage, {})
        if config.get("kind") == "local_sdk_event_dispatcher":
            runtime = config.get("runtime")
            runtime = runtime if isinstance(runtime, dict) else {}
            check = {
                "name": f"{stage}.sdk_runtime_known",
                "ok": bool(runtime.get("schema")),
                "severity": "warning",
                "status": config.get("status"),
                "path": runtime.get("isolated_codex_home"),
                "runtime_status": runtime.get("status"),
            }
            checks.append(check)
            if not check["ok"]:
                warnings.append(f"Missing local SDK runtime status: {check['name']}")
            continue
        check = {
            "name": f"{stage}.automation_known",
            "ok": bool(config.get("exists")),
            "severity": "warning",
            "status": config.get("status"),
            "path": config.get("path"),
        }
        checks.append(check)
        if not check["ok"]:
            warnings.append(f"Missing automation config: {check['name']} {check.get('path')}")

    failed = [item for item in checks if not item["ok"] and item.get("severity") != "warning"]
    status = "PASS" if not failed and not warnings else "PASS_WITH_WARNINGS" if not failed else "FAIL"
    return {"status": status, "checks": checks, "warnings": warnings, "failed": failed, "read_only": True}
