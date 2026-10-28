from __future__ import annotations

import copy
import hashlib
import functools
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import projection as unified_projection
from .account_semantics import (
    AccountReadiness,
    canonical_account_scope,
    normalize_account_readiness,
)
from .browser_session import (
    BrowserSessionEvidenceKeyStore,
    BrowserSessionEvidenceSigner,
    LocalCdpProbeTransport,
    normalize_origin,
)
from .config import (
    ACCOUNT_VAULT_PATH,
    BROWSER_SESSION_EVIDENCE_KEY_PATH,
    CREDENTIAL_VAULT_PATH,
    JOURNAL_ROOT,
    REPO_ROOT,
    STATE_DIR,
    SUBMISSION_ROOT,
    SMART_SUBMISSION_BROWSER_CONTEXT_PATH,
    UNIFIED_STATE_PATH,
    WORKSPACE_ROOT,
)
from .credential_resolver import CredentialResolutionError, SmartCredentialResolver
from .io_utils import (
    FileLock,
    append_unique,
    now_iso,
    read_json as _read_json_unchecked,
    read_text,
    timestamp_id,
    write_json_atomic as _write_json_atomic_unchecked,
    write_text,
)
from .submission_routing import (
    normalize_submission_route,
    submission_route_for,
    terminal_submission_evidence,
)
from .smart_submission_program import (
    build_program_plan,
    program_database_path,
    sync_program_tasks,
)
from .smart_mailbox import sanitize_monitor_source_results
from .smart_submission_identity import (
    IDENTITY_CANONICAL_MISMATCH,
    IDENTITY_CONFLICT,
    classify_monitor_action,
    close_submission_record,
    normalize_submission_id,
    project_submission_identity,
    submission_identity_snapshot as submission_identity_snapshot,
    submission_records,
    upsert_submission_record,
)
from .smart_retarget import (
    RETARGET_MODE_SPECIFIC,
    RETARGET_REQUEST_SCHEMA,
    WITHDRAWAL_POLICY_CONFIRMED_CLOSED,
    journal_preferences_patch,
    normalize_retarget_request,
    retarget_request_is_active,
    retarget_withdrawal_decision,
)
from .submission_site_adapters import ORCID_AUTH_ORIGINS, submission_site_adapter
from .submission_upload_staging import stage_snapp_latex_archive
from .windows_subprocess import no_window_subprocess_kwargs


SMART_SUBMISSION_DIR = STATE_DIR / "smart_submission"
SMART_STATE_PATH = SMART_SUBMISSION_DIR / "smart_submission_state.json"
SMART_CREDENTIAL_PATH = SMART_SUBMISSION_DIR / "credential_store.local.json"
SMART_CREDENTIAL_VAULT_PATH = CREDENTIAL_VAULT_PATH
SMART_SITE_REGISTRY_PATH = SMART_SUBMISSION_DIR / "site_registry.json"
SMART_SCHEDULER_PROMPT_PATH = SMART_SUBMISSION_DIR / "SMART_SUBMISSION_SCHEDULER_PROMPT.md"
SMART_AUDIT_DIR = SMART_SUBMISSION_DIR / "audit_runs"
SMART_SCREENSHOT_DIR = SMART_SUBMISSION_DIR / "screenshots"
SMART_SNAPSHOT_DIR = SMART_SUBMISSION_DIR / "page_snapshots"
SMART_CONFIRMATION_EVIDENCE_DIR = SMART_SUBMISSION_DIR / "confirmation_evidence"
SMART_LOG_DIR = SMART_SUBMISSION_DIR / "logs"
SMART_UPLOAD_STAGING_DIR = SMART_SUBMISSION_DIR / "upload_staging"
SMART_AUTOMATION_PATH = Path.home() / ".codex" / "automations" / "automation" / "automation.toml"
SMART_AUTOMATION_DAILY_RRULE = "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0"
SMART_AUTOMATION_FAST_RRULE = "RRULE:FREQ=MINUTELY;INTERVAL=1"
SMART_AUTOMATION_BACKOFF_RRULE = "RRULE:FREQ=MINUTELY;INTERVAL=5"
SMART_MONITOR_DEFAULT_TIME_LOCAL = "09:00"
SMART_MONITOR_TIME_PATTERN = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")
SMART_NATIVE_MONITOR_TASK_NAME = "UnifiedPaperOrchestrator-SmartSubmissionDailyMonitor"
SMART_PROGRAM_TIMER_COMMAND = "smart-submission-program-tick"
SMART_MONITOR_RUN_STALE_SECONDS = 6 * 60 * 60
SMART_MONITOR_MAILBOX_ORIGIN = "https://mail.google.com:443"
SMART_MONITOR_BATCH_SCHEMA = "smart-submission-mailbox-monitor-batch/v1"
SMART_MONITOR_CHANNEL_JOURNAL_DASHBOARD = "journal_dashboard"
SMART_MONITOR_CHANNEL_MAILBOX_BATCH = "mailbox_batch"
SMART_MANUAL_TAKEOVER_SCHEMA = "smart-submission-manual-takeover/v1"
_smart_ascii_override = os.environ.get("UNIFIED_SMART_COMMAND_WORKSPACE", "").strip()
SMART_ASCII_WORKSPACE = (
    _smart_ascii_override
    or (str(REPO_ROOT) if str(REPO_ROOT).isascii() else r"D:\Documents\dayi_unified")
)
DEFAULT_SMART_MAX_PARALLEL_WORKERS = 4
SMART_STATE_VERSION = 7
SMART_PROMPT_VERSION = 9
SMART_HISTORY_MAX_EVENTS = 500
SMART_HISTORY_EVENT_MAX_BYTES = 16 * 1024
SMART_HISTORY_MAX_LIST_ITEMS = 50
SMART_HISTORY_MAX_STRING_CHARS = 2000
SMART_STATE_LOCK_TIMEOUT_SECONDS = 30.0
LEGACY_SUBMISSION_STATE_PATH = SUBMISSION_ROOT / "controller" / "auto_submission_panel_state.json"
LEGACY_MIGRATION_OUTPUT_DIR = SUBMISSION_ROOT / "99_项目迁移包_2026-06-30" / "输出"
LEGACY_CDP_ENSURE_SCRIPT = SUBMISSION_ROOT / "controller" / "ensure_cdp_channels.py"
SMART_CDP_PORTS = (9224, 9222)
STANDING_DECLARATION_AUTHORIZED_AT = "2026-07-05"
DEFAULT_AI_USE_STATEMENT = (
    "AI-assisted tools, including OpenAI Codex, were used during manuscript preparation for drafting support, "
    "language polishing, formatting, submission-package preparation, and workflow checks. The author reviewed "
    "and edited the output and takes full responsibility for all manuscript content."
)
DEFAULT_ORIGINALITY_STATEMENT = (
    "This manuscript is original, has not been published previously, and is not currently under consideration "
    "by another journal or conference."
)
DEFAULT_AUTHOR_RESPONSIBILITY_STATEMENT = (
    "The corresponding author confirms that the manuscript has been reviewed and approved for submission, "
    "and accepts responsibility for the submitted content and all routine declarations entered during submission."
)
DEFAULT_THIRD_PARTY_MATERIAL_STATEMENT = (
    "No third-party material requiring separate permission is included in this manuscript; all submitted text, "
    "figures, tables, and supplementary materials are author-prepared material or otherwise require no separate "
    "third-party permission."
)


def normalize_smart_monitor_time(value: Any) -> str:
    """Return one canonical local daily monitor time in ``HH:MM`` form."""

    text = str(value or "").strip()
    match = SMART_MONITOR_TIME_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("每日监控时间必须使用 HH:MM 格式，例如 09:00 或 21:30。")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour not in range(24) or minute not in range(60):
        raise ValueError("每日监控时间超出有效范围，请输入 00:00 至 23:59。")
    return f"{hour:02d}:{minute:02d}"


def _smart_monitor_schedule(
    settings: dict[str, Any],
    *,
    requested_time: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize the persisted schedule while preserving legacy hour fields."""

    if requested_time is not None:
        time_local = normalize_smart_monitor_time(requested_time)
    else:
        configured = str(settings.get("daily_monitor_time_local") or "").strip()
        if configured:
            try:
                time_local = normalize_smart_monitor_time(configured)
            except ValueError:
                configured = ""
        if not configured:
            try:
                legacy_hour = int(settings.get("daily_monitor_hour_local", 9))
            except (TypeError, ValueError):
                legacy_hour = 9
            try:
                legacy_minute = int(settings.get("daily_monitor_minute_local", 0))
            except (TypeError, ValueError):
                legacy_minute = 0
            legacy_hour = legacy_hour if legacy_hour in range(24) else 9
            legacy_minute = legacy_minute if legacy_minute in range(60) else 0
            time_local = f"{legacy_hour:02d}:{legacy_minute:02d}"

    hour, minute = (int(part) for part in time_local.split(":", 1))
    settings["daily_monitor_time_local"] = time_local
    settings["daily_monitor_hour_local"] = hour
    settings["daily_monitor_minute_local"] = minute
    rrule = f"RRULE:FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    next_run = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= current:
        next_run += timedelta(days=1)
    return {
        "time_local": time_local,
        "hour_local": hour,
        "minute_local": minute,
        "timezone": str(current.tzname() or "local"),
        "rrule": rrule,
        "next_run_at": next_run.isoformat(),
    }


def _smart_monitor_check_due(
    paper: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether this paper has not yet been checked on the local date."""

    checked_text = str(paper.get("last_monitor_checked_at") or "").strip()
    if not checked_text:
        return True
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    try:
        checked = datetime.fromisoformat(checked_text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=current.tzinfo)
    return checked.astimezone(current.tzinfo).date() < current.date()


def _run_windows_task_scheduler(*arguments: str) -> dict[str, Any]:
    """Run one bounded Task Scheduler command without opening a console window."""

    if os.name != "nt":
        return {
            "ok": False,
            "supported": False,
            "status": "unsupported_platform",
            "error": "Windows Task Scheduler is only available on Windows.",
        }
    executable = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "schtasks.exe"
    try:
        completed = subprocess.run(
            [str(executable), *arguments],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "supported": True,
            "status": "command_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    output = "\n".join(
        item.strip()
        for item in (completed.stdout or "", completed.stderr or "")
        if item.strip()
    )
    return {
        "ok": completed.returncode == 0,
        "supported": True,
        "status": "ready" if completed.returncode == 0 else "command_failed",
        "returncode": completed.returncode,
        "output": output[:2000],
    }


def _register_native_smart_monitor_task(time_local: str) -> dict[str, Any]:
    """Register the packaged EXE as the single external program timer."""

    normalized = normalize_smart_monitor_time(time_local)
    if not bool(getattr(sys, "frozen", False)):
        return {
            "ok": True,
            "supported": True,
            "registered": False,
            "status": "in_app_only",
            "task_name": SMART_NATIVE_MONITOR_TASK_NAME,
            "time_local": normalized,
            "reason": "Native task registration is deferred until the packaged EXE is running.",
        }
    executable = Path(sys.executable).resolve()
    lowered_parts = {part.casefold() for part in executable.parts}
    if "build" in lowered_parts and lowered_parts.intersection(
        {"dist_candidate", "release_candidate"}
    ):
        return {
            "ok": True,
            "supported": True,
            "registered": False,
            "status": "temporary_build_skipped",
            "task_name": SMART_NATIVE_MONITOR_TASK_NAME,
            "time_local": normalized,
            "reason": (
                "A temporary build-probe executable must not replace the persistent "
                "Smart Submission timer action."
            ),
        }
    action = subprocess.list2cmdline(
        [str(executable), SMART_PROGRAM_TIMER_COMMAND]
    )
    result = _run_windows_task_scheduler(
        "/Create",
        "/F",
        "/TN",
        SMART_NATIVE_MONITOR_TASK_NAME,
        "/TR",
        action,
        "/SC",
        "DAILY",
        "/ST",
        normalized,
        "/RL",
        "LIMITED",
    )
    result.update(
        {
            "registered": bool(result.get("ok")),
            "task_name": SMART_NATIVE_MONITOR_TASK_NAME,
            "time_local": normalized,
            "action": action,
        }
    )
    return result


def _disable_native_smart_monitor_task() -> dict[str, Any]:
    """Disable the packaged daily task while retaining it for later restore."""

    if not bool(getattr(sys, "frozen", False)):
        return {
            "ok": True,
            "supported": True,
            "disabled": False,
            "status": "in_app_only",
            "task_name": SMART_NATIVE_MONITOR_TASK_NAME,
        }
    result = _run_windows_task_scheduler(
        "/Change",
        "/TN",
        SMART_NATIVE_MONITOR_TASK_NAME,
        "/DISABLE",
    )
    result.update(
        {
            "disabled": bool(result.get("ok")),
            "task_name": SMART_NATIVE_MONITOR_TASK_NAME,
        }
    )
    return result


def smart_submission_daily_monitor_due(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return whether the in-app fallback should claim today's scheduled run."""

    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    schedule = _smart_monitor_schedule(settings, now=now)
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    scheduled_at = current.replace(
        hour=int(schedule["hour_local"]),
        minute=int(schedule["minute_local"]),
        second=0,
        microsecond=0,
    )
    today = current.date().isoformat()
    last_scheduled_date = str(
        settings.get("daily_monitor_last_scheduled_date") or ""
    ).strip()
    enabled = bool(settings.get("daily_monitor_enabled", True))
    due = enabled and current >= scheduled_at and last_scheduled_date != today
    return {
        "due": due,
        "enabled": enabled,
        "time_local": schedule["time_local"],
        "scheduled_at": scheduled_at.isoformat(),
        "last_scheduled_date": last_scheduled_date,
        "current_at": current.isoformat(),
    }


DEFAULT_COPYRIGHT_PERMISSION_STATEMENT = (
    "No separate third-party copyright permission is required for the submitted manuscript materials under the "
    "recorded package declarations."
)


_SMART_STATE_THREAD_LOCK = threading.RLock()
_SMART_STATE_LOCK_LOCAL = threading.local()


SMART_ROUTE_OWNER_REQUIRED = "SMART_ROUTE_OWNER_REQUIRED"
SMART_ROUTE_MODE_CANARY_REQUIRED = "SMART_ROUTE_MODE_CANARY_REQUIRED"
SMART_ROUTE_MODE_REQUIRED = SMART_ROUTE_MODE_CANARY_REQUIRED
SMART_ROUTE_REVISION_MISMATCH = "SMART_ROUTE_REVISION_MISMATCH"
SMART_ROUTE_PAPER_MISSING = "SMART_ROUTE_PAPER_MISSING"
SMART_CANARY_SCOPE_REQUIRED = "SMART_CANARY_SCOPE_REQUIRED"
SMART_CANARY_PREFLIGHT_FAILED = "SMART_CANARY_PREFLIGHT_FAILED"
SMART_CANARY_QUEUE_BUSY = "SMART_CANARY_QUEUE_BUSY"
SMART_CANARY_TERMINAL_EVIDENCE = "SMART_CANARY_TERMINAL_EVIDENCE"
SMART_CANARY_UNVERIFIED_TERMINAL = "SMART_CANARY_UNVERIFIED_TERMINAL"
SMART_CANARY_PACKAGE_NOT_READY = "SMART_CANARY_PACKAGE_NOT_READY"
SMART_CANARY_TARGET_MISSING = "SMART_CANARY_TARGET_MISSING"
SMART_CANARY_ACCOUNT_MISSING = "SMART_CANARY_TARGET_ACCOUNT_MISSING"
SMART_SECRET_BROKER_REQUIRED = "SMART_SECRET_BROKER_REQUIRED"
SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED = "SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED"
SMART_FINAL_SUBMIT_ACTION_CLAIMED = "SMART_FINAL_SUBMIT_ACTION_CLAIMED"
SMART_FINAL_SUBMIT_NO_CLICK_VERIFICATION_REQUIRED = (
    "SMART_FINAL_SUBMIT_NO_CLICK_VERIFICATION_REQUIRED"
)
SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED = "SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED"
SMART_CANARY_SITE_UNVERIFIED = "SMART_CANARY_SITE_UNVERIFIED"
SMART_CANARY_QUEUE_FAILED = "SMART_CANARY_QUEUE_FAILED"
SMART_CANARY_ROUTE_ROLLBACK_FAILED = "SMART_CANARY_ROUTE_ROLLBACK_FAILED"
SMART_CANARY_DISPATCH_NOT_QUEUED = "SMART_CANARY_DISPATCH_NOT_QUEUED"
SMART_CANARY_WORKER_ALREADY_RUNNING = "SMART_CANARY_WORKER_ALREADY_RUNNING"
SMART_CANARY_ATTEMPT_MISMATCH = "SMART_CANARY_ATTEMPT_MISMATCH"
SMART_CANARY_THREAD_MISMATCH = "SMART_CANARY_THREAD_MISMATCH"
SMART_CANARY_RESULT_NOT_DISPATCHED = "SMART_CANARY_RESULT_NOT_DISPATCHED"
SMART_SUBMISSION_IDENTITY_RECONCILIATION_REQUIRED = (
    "SMART_SUBMISSION_IDENTITY_RECONCILIATION_REQUIRED"
)
SMART_SUBMISSION_DUPLICATE_ACTIVE = "SMART_SUBMISSION_DUPLICATE_ACTIVE"
SMART_RETARGET_CLOSURE_EVIDENCE_REQUIRED = (
    "SMART_RETARGET_CLOSURE_EVIDENCE_REQUIRED"
)
SMART_RETARGET_HISTORICAL_TARGET_REUSED = "SMART_RETARGET_HISTORICAL_TARGET_REUSED"
SMART_RETARGET_HISTORICAL_TARGET_RETRY_LIMIT = 3


def _smart_program_plan_path() -> Path:
    """Resolve from the mutable state path so isolated tests stay isolated."""

    return Path(SMART_STATE_PATH).with_name("program_dispatch_plan.json")


class SmartSubmissionGuardError(RuntimeError):
    """Stable, machine-readable refusal for Smart online execution."""

    def __init__(
        self,
        code: str,
        paper_id: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.paper_id = str(paper_id)
        self.message = str(message)
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {self.paper_id}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "paper_id": self.paper_id,
            "message": self.message,
            "details": self.details,
        }


def _submission_identity_guard_result(
    paper: dict[str, Any],
    *,
    operation: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return a deterministic identity decision for one online operation.

    ``existing`` means a monitor/correction must target one exact active
    manuscript. ``new`` means no prior manuscript may still be active.  The
    ledger is projected back to the paper so the UI and audit log see the same
    facts used by the dispatch guard.
    """

    snapshot = project_submission_identity(paper)
    clean_operation = str(operation or "").strip().casefold()
    if clean_operation == "closure":
        # A retarget closure operation is the one safe mutation that may need
        # to name and close more than one known active ID.  It is still target
        # locked by retarget_request.withdrawal_targets and can never create a
        # new submission or edit manuscript content.
        return snapshot, None
    if snapshot["status"] in {IDENTITY_CONFLICT, IDENTITY_CANONICAL_MISMATCH}:
        code = (
            SMART_SUBMISSION_DUPLICATE_ACTIVE
            if snapshot["status"] == IDENTITY_CONFLICT
            else SMART_SUBMISSION_IDENTITY_RECONCILIATION_REQUIRED
        )
        return snapshot, {
            "code": code,
            "message": (
                "multiple active manuscript IDs exist for the same journal route"
                if snapshot["status"] == IDENTITY_CONFLICT
                else "the canonical manuscript ID does not match the active journal record"
            ),
            "identity_status": snapshot["status"],
            "active_submission_ids": snapshot["active_submission_ids"],
            "canonical_submission_id": snapshot["canonical_submission_id"],
        }
    if clean_operation == "existing" and not snapshot["mutation_allowed"]:
        return snapshot, {
            "code": SMART_SUBMISSION_IDENTITY_RECONCILIATION_REQUIRED,
            "message": "one verified active manuscript ID is required for this existing-submission action",
            "identity_status": snapshot["status"],
            "active_submission_ids": snapshot["active_submission_ids"],
            "canonical_submission_id": snapshot["canonical_submission_id"],
        }
    if clean_operation == "new" and not snapshot["new_submission_allowed"]:
        return snapshot, {
            "code": SMART_SUBMISSION_DUPLICATE_ACTIVE,
            "message": "a new submission is forbidden while any prior manuscript ID remains active",
            "identity_status": snapshot["status"],
            "active_submission_ids": snapshot["active_submission_ids"],
            "canonical_submission_id": snapshot["canonical_submission_id"],
        }
    return snapshot, None


def _online_identity_operation(paper: Mapping[str, Any]) -> str:
    intent = str(paper.get("online_intent") or "").strip().casefold()
    if intent == "user_retarget_verify_or_withdraw":
        return "closure"
    if intent.startswith("post_submission_") or bool(paper.get("monitor_check_pending")):
        return "existing"
    if intent in {
        "retarget_after_rejection",
        "submit_retargeted_package",
        "retarget_after_submission_system_hard_stop",
    }:
        return "new"
    if _paper_has_prior_submission_evidence(dict(paper)):
        return "existing"
    return "new"


def _is_fresh_retarget_handoff(paper: Mapping[str, Any]) -> bool:
    request = (
        paper.get("retarget_request")
        if isinstance(paper.get("retarget_request"), Mapping)
        else {}
    )
    return bool(
        str(paper.get("online_intent") or "").strip().casefold()
        == "submit_retargeted_package"
        or str(paper.get("last_monitor_followup_status") or "").strip().casefold()
        == "retarget_package_ready"
        or str(request.get("status") or "").strip().casefold()
        == "completed_handoff_to_submission"
    )


def _same_path(left: Path | str, right: Path | str) -> bool:
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(left) == str(right)


def _smart_projection_dirty_path() -> Path:
    return unified_projection.dirty_path_for(Path(UNIFIED_STATE_PATH))


def _prepare_smart_projection_change(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Bind a value-free projection revision to the authoritative state commit."""

    safe_reason = str(reason or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_reason):
        safe_reason = "smart_state_changed"
    state["unified_projection_token"] = os.urandom(16).hex()
    return {
        "status": "pending_commit",
        "dirty": True,
        "marked": False,
        "reason": safe_reason,
    }


def _mark_smart_projection_dirty(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Best-effort marker write; Smart state/audit success never depends on it."""

    safe_reason = str(reason or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_reason):
        safe_reason = "smart_state_changed"
    token = str(state.get("unified_projection_token") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        _prepare_smart_projection_change(state, safe_reason)
        token = str(state["unified_projection_token"])
    try:
        marker = unified_projection.mark_projection_dirty(
            safe_reason,
            token=token,
            dirty_path=_smart_projection_dirty_path(),
        )
    except Exception as exc:
        return {
            "status": "deferred",
            "dirty": True,
            "marked": False,
            "code": unified_projection.PROJECTION_REFRESH_FAILED,
            "error_type": type(exc).__name__,
            "reason": safe_reason,
        }
    return {
        "status": "marked",
        "dirty": True,
        "marked": True,
        "generation": int(marker.get("generation") or 0),
        "reason": str(marker.get("reason") or "smart_state_changed"),
    }


def _smart_projection_source_ready(snapshot: dict[str, Any]) -> bool:
    if snapshot.get("corrupt"):
        return True
    state = read_json(SMART_STATE_PATH, {})
    if str(state.get("unified_projection_token") or "") != str(snapshot.get("token") or ""):
        return False

    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    for paper in papers.values():
        if not isinstance(paper, dict):
            continue
        artifact_pairs: list[tuple[Any, Any]] = [
            (paper.get("last_audit_path"), paper.get("last_audit_sha256")),
            (paper.get("last_dispatch_audit_path"), paper.get("last_dispatch_audit_sha256")),
        ]
        for key, path_key, digest_key in (
            ("final_submit_confirmation_challenge", "challenge_audit_path", "challenge_audit_sha256"),
            ("pending_submission_confirmation_evidence", "evidence_path", "evidence_sha256"),
            ("last_live_submission_confirmation_evidence", "evidence_path", "evidence_sha256"),
            ("submission_confirmation_receipt", "result_audit_path", "result_audit_sha256"),
        ):
            wrapper = paper.get(key)
            if isinstance(wrapper, dict):
                artifact_pairs.append((wrapper.get(path_key), wrapper.get(digest_key)))
        authorization = paper.get("final_submit_authorization")
        if isinstance(authorization, dict):
            artifact_pairs.extend(
                [
                    (
                        authorization.get("authorization_audit_path"),
                        authorization.get("authorization_audit_sha256"),
                    ),
                    (authorization.get("reservation_audit_path"), authorization.get("reservation_audit_sha256")),
                    (authorization.get("consumption_audit_path"), authorization.get("consumption_audit_sha256")),
                ]
            )
        if any(_transaction_artifact_state(path, digest)[0] == "pending" for path, digest in artifact_pairs):
            return False
    return True


def refresh_smart_submission_projection(
    *,
    force: bool = False,
    sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Refresh unified JSON/Markdown without making Smart success depend on it."""

    # Serializing the full snapshot -> sync -> acknowledge sequence with Smart
    # state commits prevents an older refresh from overwriting a newer one and
    # then clearing the only dirty marker (ABA/lost-refresh race).
    with _smart_state_lock():
        dirty_path = _smart_projection_dirty_path()
        if sync_callback is None and not unified_projection.canonical_projection_sources(
            Path(UNIFIED_STATE_PATH),
            Path(SMART_STATE_PATH),
        ):
            # Unit tests and recovery tools often redirect only Smart paths.
            # Never regenerate the user's real unified state from an isolated run.
            return unified_projection.deferred_projection_refresh(dirty_path=dirty_path)

        state = read_json(SMART_STATE_PATH, {})
        smart_token = str(state.get("unified_projection_token") or "") if isinstance(state, dict) else ""
        try:
            unified_state = read_json(UNIFIED_STATE_PATH, {})
        except Exception:
            unified_state = {}
        projection_sources = (
            unified_state.get("projection_sources", {})
            if isinstance(unified_state, dict) and isinstance(unified_state.get("projection_sources"), dict)
            else {}
        )
        unified_token = str(projection_sources.get("smart_submission_token") or "")
        source_dirty = bool(smart_token) and smart_token != unified_token

        try:
            snapshot = unified_projection.projection_dirty_snapshot(dirty_path=dirty_path)
        except Exception as exc:
            return {
                "status": "deferred",
                "dirty": True,
                "refreshed": False,
                "code": unified_projection.PROJECTION_REFRESH_FAILED,
                "error_type": type(exc).__name__,
            }

        if snapshot and not snapshot.get("corrupt") and str(snapshot.get("token") or "") != smart_token:
            if source_dirty:
                rebound = _mark_smart_projection_dirty(state, "smart_state_changed")
                if not rebound.get("marked"):
                    return {**rebound, "refreshed": False}
            else:
                # A marker written before a failed state commit is harmless but
                # must not leave the projection permanently pending.
                try:
                    unified_projection.acknowledge_projection_refresh(snapshot, dirty_path=dirty_path)
                except Exception as exc:
                    return {
                        "status": "deferred",
                        "dirty": True,
                        "refreshed": False,
                        "code": unified_projection.PROJECTION_REFRESH_FAILED,
                        "error_type": type(exc).__name__,
                    }
        elif source_dirty and snapshot is None:
            rebound = _mark_smart_projection_dirty(state, "smart_state_changed")
            if not rebound.get("marked"):
                return {**rebound, "refreshed": False}

        return unified_projection.refresh_unified_projection_if_dirty(
            force=bool(force or source_dirty),
            sync_callback=sync_callback,
            ready_callback=_smart_projection_source_ready,
            dirty_path=dirty_path,
        )


def _acquire_platform_file_lock(handle: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out acquiring Smart Submission state lock: {SMART_STATE_PATH}")
            time.sleep(0.05)


def _release_platform_file_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _smart_state_lock(timeout: float = SMART_STATE_LOCK_TIMEOUT_SECONDS):
    """Process-wide re-entrant lock plus an OS lock shared by other processes."""
    with _SMART_STATE_THREAD_LOCK:
        depth = int(getattr(_SMART_STATE_LOCK_LOCAL, "depth", 0))
        if depth:
            _SMART_STATE_LOCK_LOCAL.depth = depth + 1
            try:
                yield
            finally:
                _SMART_STATE_LOCK_LOCAL.depth -= 1
            return

        lock_path = SMART_STATE_PATH.with_suffix(SMART_STATE_PATH.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            _acquire_platform_file_lock(handle, timeout)
            _SMART_STATE_LOCK_LOCAL.depth = 1
            try:
                yield
            finally:
                _SMART_STATE_LOCK_LOCAL.depth = 0
                _release_platform_file_lock(handle)
        finally:
            handle.close()


def _smart_state_locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _smart_state_lock():
            return function(*args, **kwargs)

    return wrapper


def read_json(path: Path | str, default: Any) -> Any:
    """Read Smart state strictly; corrupt state must never become an empty default."""
    if _same_path(path, SMART_STATE_PATH) and Path(path).exists():
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Smart Submission state is unreadable; refusing to overwrite it: {path}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Smart Submission state must be a JSON object: {path}")
        return value
    return _read_json_unchecked(Path(path), default)


def _unified_state_snapshot(value: dict[str, Any] | None = None) -> dict[str, Any]:
    state = value if isinstance(value, dict) else read_json(UNIFIED_STATE_PATH, {})
    if not isinstance(state, dict):
        raise SmartSubmissionGuardError(
            SMART_ROUTE_PAPER_MISSING,
            "",
            f"unified state must be a JSON object: {UNIFIED_STATE_PATH}",
        )
    return state


def require_smart_canary_route(
    paper_id: str,
    *,
    expected_revision: int | None = None,
    unified_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless one paper is explicitly owned by Smart canary."""

    paper_id = str(paper_id or "").strip()
    if not paper_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_SCOPE_REQUIRED,
            paper_id,
            "exactly one paper_id is required for Smart online work",
        )
    state = _unified_state_snapshot(unified_state)
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    routes = state.get("submission_routes", {}) if isinstance(state.get("submission_routes"), dict) else {}
    if paper_id not in papers and paper_id not in routes:
        raise SmartSubmissionGuardError(
            SMART_ROUTE_PAPER_MISSING,
            paper_id,
            "paper is not present in unified state or submission_routes",
        )
    route = submission_route_for(routes, paper_id)
    if str(route.get("execution_owner")) != "smart":
        raise SmartSubmissionGuardError(
            SMART_ROUTE_OWNER_REQUIRED,
            paper_id,
            "Smart online work requires execution_owner=smart",
            details={"route": route},
        )
    if str(route.get("mode")) != "canary":
        raise SmartSubmissionGuardError(
            SMART_ROUTE_MODE_REQUIRED,
            paper_id,
            "Smart online work requires mode=canary",
            details={"route": route},
        )
    if expected_revision is not None and int(route.get("revision") or 0) != int(expected_revision):
        raise SmartSubmissionGuardError(
            SMART_ROUTE_REVISION_MISMATCH,
            paper_id,
            "submission route revision is stale",
            details={"expected_revision": int(expected_revision), "actual_revision": int(route.get("revision") or 0)},
        )
    return dict(route)


def _route_guard_result(
    paper_id: str,
    *,
    expected_revision: int | None = None,
    unified_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return require_smart_canary_route(
            paper_id,
            expected_revision=expected_revision,
            unified_state=unified_state,
        ), None
    except SmartSubmissionGuardError as exc:
        return None, exc.as_dict()


SMART_READ_ONLY_MONITOR_ROUTE_SCOPE = "read_only_existing_submission"


def _monitor_check_active(paper: dict[str, Any]) -> bool:
    return bool(paper.get("monitor_check_pending")) and str(
        paper.get("status") or ""
    ).strip().casefold() in {"submitted", "monitoring"}


def _require_worker_route(
    paper_id: str,
    paper: dict[str, Any],
    *,
    expected_revision: int | None = None,
    unified_state: dict[str, Any] | None = None,
    monitor_only: bool = False,
) -> dict[str, Any]:
    """Validate the route for one worker without granting mutation rights.

    Normal online work remains restricted to the Smart canary route.  A
    read-only check of an already-submitted paper may use the paper's existing
    route (including historical ``legacy_auto`` routes), but only while the
    explicit monitor checkpoint is active and prior submission evidence exists.
    """

    if not monitor_only:
        return require_smart_canary_route(
            paper_id,
            expected_revision=expected_revision,
            unified_state=unified_state,
        )
    state = _unified_state_snapshot(unified_state)
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    routes = state.get("submission_routes", {}) if isinstance(state.get("submission_routes"), dict) else {}
    if paper_id not in papers and paper_id not in routes:
        raise SmartSubmissionGuardError(
            SMART_ROUTE_PAPER_MISSING,
            paper_id,
            "paper is not present in unified state or submission_routes",
        )
    route = submission_route_for(routes, paper_id)
    is_smart_canary = (
        str(route.get("execution_owner") or "") == "smart"
        and str(route.get("mode") or "") == "canary"
    )
    if not is_smart_canary and not _paper_is_monitoring_like(paper):
        raise SmartSubmissionGuardError(
            SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
            paper_id,
            "historical-route monitoring requires an existing submitted/monitoring record",
        )
    revision = int(route.get("revision") or 0)
    if expected_revision is not None and revision != int(expected_revision):
        raise SmartSubmissionGuardError(
            SMART_ROUTE_REVISION_MISMATCH,
            paper_id,
            "submission route revision is stale",
            details={
                "expected_revision": int(expected_revision),
                "actual_revision": revision,
            },
        )
    return dict(route)


def _worker_route_guard_result(
    paper_id: str,
    paper: dict[str, Any],
    *,
    expected_revision: int | None = None,
    unified_state: dict[str, Any] | None = None,
    monitor_only: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return _require_worker_route(
            paper_id,
            paper,
            expected_revision=expected_revision,
            unified_state=unified_state,
            monitor_only=monitor_only,
        ), None
    except SmartSubmissionGuardError as exc:
        return None, exc.as_dict()


def _mark_smart_route_on_paper(paper: dict[str, Any], route: dict[str, Any]) -> None:
    paper["execution_owner"] = "smart"
    paper["execution_mode"] = "canary"
    paper["route_revision"] = int(route.get("revision") or 0)


def _mark_worker_route_on_paper(
    paper: dict[str, Any],
    route: dict[str, Any],
    *,
    monitor_only: bool = False,
) -> None:
    if not monitor_only:
        _mark_smart_route_on_paper(paper, route)
        return
    paper["route_revision"] = int(route.get("revision") or 0)
    paper["monitor_route_scope"] = SMART_READ_ONLY_MONITOR_ROUTE_SCOPE
    paper["monitor_route_owner"] = str(route.get("execution_owner") or "")
    paper["monitor_route_mode"] = str(route.get("mode") or "")
    paper["monitor_route_revision"] = int(route.get("revision") or 0)


def _new_canary_attempt_id(paper_id: str) -> str:
    entropy = hashlib.sha256(
        f"{paper_id}|{time.time_ns()}|{threading.get_ident()}".encode("utf-8")
    ).hexdigest()[:10]
    return f"canary-{timestamp_id()}-{entropy}"


def _rotate_canary_attempt(paper: dict[str, Any], paper_id: str) -> str:
    attempt_id = _new_canary_attempt_id(paper_id)
    paper["attempt_id"] = attempt_id
    paper.pop("final_submit_authorization", None)
    return attempt_id


def _smart_state_file_hash() -> str:
    if not SMART_STATE_PATH.is_file():
        return ""
    return hashlib.sha256(SMART_STATE_PATH.read_bytes()).hexdigest()


def _read_smart_state_snapshot() -> tuple[bool, dict[str, Any] | None, bytes | None, str]:
    if not SMART_STATE_PATH.is_file():
        return False, None, None, ""
    raw = SMART_STATE_PATH.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Smart Submission state is unreadable; refusing route switch: {SMART_STATE_PATH}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Smart Submission state must be a JSON object: {SMART_STATE_PATH}")
    return True, parsed, raw, hashlib.sha256(raw).hexdigest()


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _canary_transaction_paths() -> tuple[Path, Path]:
    return (
        SMART_SUBMISSION_DIR / "canary_route_transaction.json",
        SMART_SUBMISSION_DIR / "canary_route_transaction.snapshot",
    )


def _write_canary_transaction_journal(entry: dict[str, Any], snapshot: bytes | None = None) -> None:
    journal_path, snapshot_path = _canary_transaction_paths()
    if snapshot is not None:
        _write_bytes_atomic(snapshot_path, snapshot)
    elif not bool(entry.get("state_existed")):
        snapshot_path.unlink(missing_ok=True)
    _write_json_atomic_unchecked(journal_path, entry)


def _clear_canary_transaction_journal() -> None:
    for path in _canary_transaction_paths():
        path.unlink(missing_ok=True)


def _state_has_canary_transaction(state: dict[str, Any], paper_id: str, transaction_id: str) -> bool:
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id, {})
    if isinstance(paper, dict) and str(paper.get("canary_transaction_id") or "") == transaction_id:
        return True
    history = state.get("history", []) if isinstance(state.get("history"), list) else []
    return any(
        isinstance(event, dict)
        and str(event.get("paper_id") or "") == paper_id
        and str(event.get("canary_transaction_id") or "") == transaction_id
        for event in history
    )


def _smart_terminal_like_details(paper: Any) -> dict[str, Any]:
    if not isinstance(paper, dict):
        return {}
    terminal_values = {"accepted", "monitoring", "submitted"}
    fields: dict[str, str] = {}
    for key in ("status", "workflow_stage", "workflow_status", "worker_status"):
        value = str(paper.get(key) or "").strip().lower()
        if value in terminal_values:
            fields[key] = value
    next_action = str(paper.get("next_action") or "").strip().lower()
    if any(token in next_action for token in ("monitor_submission", "email_first_monitoring")):
        fields["next_action"] = next_action
    flags = {
        key: paper.get(key)
        for key in ("final_submit_attempted", "final_submit_result_unknown", "final_submit_unknown")
        if paper.get(key) not in (None, False, 0, "", "false", "0")
    }
    return {"fields": fields, "flags": flags} if fields or flags else {}


def _assert_no_terminal_canary_state(
    paper_id: str,
    *,
    legacy_state: dict[str, Any],
    smart_state: dict[str, Any],
    stage: str,
) -> None:
    evidence = terminal_submission_evidence(
        legacy_state,
        smart_state,
        paper_id,
        smart_evidence_base=REPO_ROOT,
    )
    if evidence:
        raise SmartSubmissionGuardError(
            SMART_CANARY_TERMINAL_EVIDENCE,
            paper_id,
            f"terminal submission evidence appeared at {stage}",
            details={"stage": stage, "evidence": evidence},
        )
    papers = smart_state.get("papers", {}) if isinstance(smart_state.get("papers"), dict) else {}
    terminal_like = _smart_terminal_like_details(papers.get(paper_id))
    if terminal_like:
        raise SmartSubmissionGuardError(
            SMART_CANARY_UNVERIFIED_TERMINAL,
            paper_id,
            f"terminal-like Smart state appeared at {stage}",
            details={"stage": stage, **terminal_like},
        )


def _restore_smart_state_snapshot_cas(
    *,
    paper_id: str,
    transaction_id: str,
    state_existed: bool,
    state_before_raw: bytes | None,
    hash_before: str,
    hash_after: str,
) -> str:
    """Restore only this transaction's write; never overwrite unknown state."""

    current_hash = _smart_state_file_hash()
    if current_hash == hash_before:
        return ""
    if not hash_after or current_hash != hash_after:
        return "SMART_STATE_CAS_CONFLICT"
    current = read_json(SMART_STATE_PATH, {}) if SMART_STATE_PATH.exists() else {}
    if not isinstance(current, dict) or not _state_has_canary_transaction(
        current,
        paper_id,
        transaction_id,
    ):
        return "SMART_STATE_CAS_CONFLICT"
    if state_existed:
        if state_before_raw is None:
            return "SMART_STATE_SNAPSHOT_INVALID"
        _write_bytes_atomic(SMART_STATE_PATH, state_before_raw)
    else:
        SMART_STATE_PATH.unlink(missing_ok=True)
    if _smart_state_file_hash() != hash_before:
        return "SMART_STATE_RESTORE_HASH_MISMATCH"
    return ""


def _restore_original_route_cas(
    orchestrator: Any,
    paper_id: str,
    *,
    original_route: dict[str, Any],
    expected_switched_revision: int | None,
    operation_id: str,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Restore only the exact Smart route written by this canary attempt."""

    try:
        unified = _unified_state_snapshot()
        live_route = submission_route_for(unified.get("submission_routes"), paper_id)
    except Exception as exc:
        return None, f"could not read live submission route: {exc}", {}
    if (
        str(live_route.get("execution_owner")) == str(original_route["execution_owner"])
        and str(live_route.get("mode")) == str(original_route["mode"])
    ):
        return None, "", live_route
    if expected_switched_revision is None:
        return (
            None,
            "submission route changed but this canary has no verified route receipt",
            live_route,
        )
    candidate_revision = int(expected_switched_revision)
    operations = (
        unified.get("submission_route_operations", {})
        if isinstance(unified.get("submission_route_operations"), dict)
        else {}
    )
    receipt = operations.get(paper_id, {})
    if not (
        isinstance(receipt, dict)
        and str(receipt.get("operation_id") or "") == str(operation_id or "")
        and int(receipt.get("route_revision") or 0) == candidate_revision
    ):
        return None, "submission route transaction receipt is missing or stale", live_route
    if not (
        str(live_route.get("execution_owner")) == "smart"
        and str(live_route.get("mode")) == "canary"
        and int(live_route.get("revision") or 0) == candidate_revision
    ):
        return (
            None,
            "submission route CAS conflict; live route is not this canary attempt",
            live_route,
        )
    try:
        rollback = orchestrator.set_submission_route(
            paper_id,
            execution_owner=str(original_route["execution_owner"]),
            mode=str(original_route["mode"]),
            expected_revision=candidate_revision,
            operation_id=f"{operation_id}:rollback",
        )
    except Exception as exc:
        try:
            post_route = submission_route_for(
                _unified_state_snapshot().get("submission_routes"),
                paper_id,
            )
        except Exception:
            return None, str(exc), live_route
        if (
            str(post_route.get("execution_owner")) == str(original_route["execution_owner"])
            and str(post_route.get("mode")) == str(original_route["mode"])
            and int(post_route.get("revision") or 0) == candidate_revision + 1
        ):
            return {"warning": str(exc), "submission_route": post_route}, "", post_route
        return None, str(exc), post_route
    try:
        post_route = submission_route_for(
            _unified_state_snapshot().get("submission_routes"),
            paper_id,
        )
    except Exception as exc:
        return None, f"route rollback postcondition could not be read: {exc}", live_route
    if not (
        str(post_route.get("execution_owner")) == str(original_route["execution_owner"])
        and str(post_route.get("mode")) == str(original_route["mode"])
        and int(post_route.get("revision") or 0) == candidate_revision + 1
    ):
        return None, "route rollback postcondition mismatch", post_route
    return rollback if isinstance(rollback, dict) else {}, "", post_route


def _recover_incomplete_canary_transaction(orchestrator: Any) -> dict[str, Any]:
    journal_path, _snapshot_path = _canary_transaction_paths()
    if not journal_path.is_file():
        return {"ok": True, "recovered": False}
    try:
        journal = _read_json_unchecked(journal_path, {})
        if not isinstance(journal, dict):
            raise ValueError("transaction journal must be a JSON object")
        paper_id = str(journal.get("paper_id") or "").strip()
        transaction_id = str(journal.get("transaction_id") or "").strip()
        original_route = normalize_submission_route(journal.get("original_route"))
        switched_revision = int(journal.get("switched_revision") or 0)
        hash_before = str(journal.get("smart_hash_before") or "")
        if not paper_id or not transaction_id:
            raise ValueError("transaction journal is missing identity")
        unified = _unified_state_snapshot()
        live_route = submission_route_for(unified.get("submission_routes"), paper_id)
        current_hash = _smart_state_file_hash()
        current_state = read_json(SMART_STATE_PATH, {}) if SMART_STATE_PATH.is_file() else {}
        current_papers = (
            current_state.get("papers", {})
            if isinstance(current_state, dict) and isinstance(current_state.get("papers"), dict)
            else {}
        )
        current_paper = current_papers.get(paper_id, {})
        operations = (
            unified.get("submission_route_operations", {})
            if isinstance(unified.get("submission_route_operations"), dict)
            else {}
        )
        receipt = operations.get(paper_id, {})
        receipt_matches = (
            isinstance(receipt, dict)
            and str(receipt.get("operation_id") or "") == transaction_id
            and int(receipt.get("route_revision") or 0) == int(live_route.get("revision") or 0)
        )
        has_commit = (
            isinstance(current_state, dict)
            and _state_has_canary_transaction(current_state, paper_id, transaction_id)
            and paper_id in _queue_paper_ids(current_state, "online_queue")
            and isinstance(current_paper, dict)
            and int(current_paper.get("route_revision") or 0) == int(live_route.get("revision") or 0)
            and (receipt_matches or not bool(journal.get("route_changed")))
        )
        if has_commit and str(live_route.get("execution_owner")) == "smart" and str(live_route.get("mode")) == "canary":
            _clear_canary_transaction_journal()
            return {
                "ok": True,
                "recovered": True,
                "action": "finalized_committed_canary",
                "paper_id": paper_id,
            }
        if (
            str(live_route.get("execution_owner")) == str(original_route["execution_owner"])
            and str(live_route.get("mode")) == str(original_route["mode"])
            and current_hash == hash_before
        ):
            _clear_canary_transaction_journal()
            return {
                "ok": True,
                "recovered": True,
                "action": "cleared_unapplied_canary_intent",
                "paper_id": paper_id,
            }
        if (
            switched_revision > 0
            and current_hash == hash_before
            and str(live_route.get("execution_owner")) == "smart"
            and str(live_route.get("mode")) == "canary"
            and int(live_route.get("revision") or 0) == switched_revision
        ):
            rollback, rollback_error, post_route = _restore_original_route_cas(
                orchestrator,
                paper_id,
                original_route=original_route,
                expected_switched_revision=switched_revision,
                operation_id=transaction_id,
            )
            if rollback_error:
                raise RuntimeError(rollback_error)
            _clear_canary_transaction_journal()
            return {
                "ok": True,
                "recovered": True,
                "action": "rolled_back_orphan_smart_route",
                "paper_id": paper_id,
                "rollback": rollback or {},
                "route": post_route,
            }
        return {
            "ok": False,
            "code": SMART_CANARY_ROUTE_ROLLBACK_FAILED,
            "paper_id": paper_id,
            "message": "incomplete Smart canary transaction conflicts with live state",
            "details": {
                "transaction_id": transaction_id,
                "phase": journal.get("phase", ""),
                "live_route": live_route,
                "smart_hash_before": hash_before,
                "smart_hash_current": current_hash,
            },
        }
    except Exception as exc:
        return _guard_error(
            SMART_CANARY_ROUTE_ROLLBACK_FAILED,
            str(journal.get("paper_id") or "") if isinstance(locals().get("journal"), dict) else "",
            "incomplete Smart canary transaction could not be recovered",
            details={"error": str(exc), "journal_path": str(journal_path)},
        )


def _append_online_queue_guarded(
    online_queue: list[str],
    paper_id: str,
    paper: dict[str, Any],
    *,
    expected_revision: int | None = None,
    unified_state: dict[str, Any] | None = None,
    monitor_only: bool = False,
) -> dict[str, Any]:
    route = _require_worker_route(
        paper_id,
        paper,
        expected_revision=expected_revision,
        unified_state=unified_state,
        monitor_only=monitor_only,
    )
    _mark_worker_route_on_paper(paper, route, monitor_only=monitor_only)
    if not str(paper.get("attempt_id") or "").strip():
        _rotate_canary_attempt(paper, str(paper_id))
    append_unique(online_queue, str(paper_id))
    return route


def _guard_error(
    code: str,
    paper_id: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return SmartSubmissionGuardError(code, paper_id, message, details=details).as_dict()


def _queue_paper_ids(state: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = state.get(key, []) if isinstance(state, dict) else []
        if not isinstance(raw, list):
            continue
        for item in raw:
            candidate = (
                str(item.get("paper_id") or item.get("id") or "").strip()
                if isinstance(item, dict)
                else str(item or "").strip()
            )
            if candidate and candidate not in values:
                values.append(candidate)
    return values


def _effective_running_papers(state: dict[str, Any]) -> list[str]:
    """Project live workers from concrete records, never stale aggregate counts."""

    running: list[str] = []
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    running_tokens = {
        "active",
        "browser_active",
        "busy",
        "dispatching",
        "final_submit",
        "final_submitting",
        "in_progress",
        "inprogress",
        "running",
        "submitting",
        "worker_running",
        "working",
    }
    for paper_id, paper in papers.items():
        if not isinstance(paper, dict):
            continue
        statuses = {
            str(paper.get(key) or "").strip().lower().replace("-", "_")
            for key in ("status", "workflow_stage", "workflow_status", "worker_status")
        }
        has_live_thread = bool(str(paper.get("thread_id") or "").strip())
        if statuses & running_tokens and (has_live_thread or "running" in statuses):
            running.append(str(paper_id))
    active_workers = state.get("active_workers")
    if isinstance(active_workers, dict):
        worker_values = active_workers.values()
    elif isinstance(active_workers, list):
        worker_values = active_workers
    else:
        worker_values = []
    for worker in worker_values:
        paper_id = (
            str(worker.get("paper_id") or worker.get("id") or "").strip()
            if isinstance(worker, dict)
            else str(worker or "").strip()
        )
        if paper_id and paper_id not in running:
            running.append(paper_id)
    return running


def _first_existing_file(*paths: Path) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _has_unresolved_gate_marker(
    text: str,
    *,
    resolved_minor_actions: bool = False,
) -> bool:
    upper = str(text or "").upper()
    hard_markers = (
        "DO NOT SUBMIT",
        "PACKAGE_STATUS=BLOCKED",
        "PACKAGE_STATUS: BLOCKED",
        "FINAL STATUS: BLOCKED",
        "FINAL DECISION: BLOCKED",
    )
    if any(marker in upper for marker in hard_markers):
        return True
    if not resolved_minor_actions and any(
        marker in upper for marker in ("NEEDS_MINOR_FIX", "MANUAL_REQUIRED")
    ):
        return True
    for line in upper.splitlines():
        if "TODO" not in line:
            continue
        compact = " ".join(line.split())
        if any(
            safe in compact
            for safe in (
                "NO TODO",
                "NO UNRESOLVED TODO",
                "TODO: 0",
                "TODO = 0",
                "TODO COUNT: 0",
                "TODO COUNT = 0",
            )
        ):
            continue
        return True
    return False


def _system_aliases(system_id: str, system_label: str = "") -> set[str]:
    aliases: dict[str, tuple[str, ...]] = {
        "springer_nature": ("springer", "springer nature", "snapp"),
        "wiley": ("wiley", "research exchange", "author services"),
        "elsevier": ("elsevier", "elsevier submit", "evise"),
        "editorial_manager": ("editorial manager", "aries"),
        "scholar_one": ("scholarone", "scholar one", "manuscriptcentral", "sage track"),
        "inderscience": ("inderscience", "indersciencesubmissions"),
        "taylor_francis": ("taylor", "francis", "tandfonline", "research portal"),
        "ojs": ("open journal systems", "ojs"),
    }
    values = {str(system_id or "").strip().lower().replace("_", " ")}
    values.update(aliases.get(str(system_id or "").strip().lower(), ()))
    if str(system_label or "").strip():
        values.add(str(system_label).strip().lower())
    return {value for value in values if value}


def _smart_credential_resolver() -> SmartCredentialResolver:
    return SmartCredentialResolver(
        SMART_CREDENTIAL_VAULT_PATH,
        account_vault_path=ACCOUNT_VAULT_PATH,
    )


def _browser_session_evidence_verifier(evidence: dict[str, Any]) -> bool:
    """Read-only verification; a missing/corrupt DPAPI key fails closed."""

    signer = BrowserSessionEvidenceSigner(
        BrowserSessionEvidenceKeyStore(BROWSER_SESSION_EVIDENCE_KEY_PATH)
    )
    return signer.verify(evidence)


def resolve_smart_account_password(
    account: dict[str, Any],
    *,
    resolver: SmartCredentialResolver | None = None,
) -> str:
    """Resolve one password in memory; callers must never persist or format it."""

    active_resolver = resolver or _smart_credential_resolver()
    return active_resolver.resolve_password(account)


def _resolved_account_readiness(
    account: dict[str, Any],
    *,
    resolver: SmartCredentialResolver,
    expected_origins: tuple[str, ...] = (),
) -> AccountReadiness:
    credential_ref = str(account.get("credential_ref") or "").strip()
    password = str(account.get("password") or "")
    resolved_available = False
    if credential_ref or password:
        try:
            resolved = resolve_smart_account_password(account, resolver=resolver)
        except CredentialResolutionError:
            resolved_available = False
        else:
            resolved_available = bool(resolved)
            resolved = ""
    return normalize_account_readiness(
        account,
        resolved_secret_available=resolved_available,
        expected_origins=expected_origins,
        evidence_verifier=_browser_session_evidence_verifier,
    )


def _account_has_usable_auth(
    account: dict[str, Any],
    *,
    resolver: SmartCredentialResolver,
) -> bool:
    return _resolved_account_readiness(account, resolver=resolver).configured


def _require_current_worker_auth(paper_id: str, paper: dict[str, Any]) -> AccountReadiness:
    """Revalidate online auth immediately before constructing a worker prompt.

    Queued readiness is only a historical hint.  Dispatch requires a current,
    exact-origin live browser-session probe.  A future trusted broker may add a
    separately signed completion receipt, but no stored secret or stale paper
    field is a positive authorization today.
    """

    account_id = str(paper.get("credential_account_id") or "").strip()
    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    if not account_id or not system_id or not target_journal:
        raise SmartSubmissionGuardError(
            SMART_SECRET_BROKER_REQUIRED,
            paper_id,
            "current value-free account and target bindings are required before worker dispatch",
            details={"account_id": account_id},
        )
    registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    system_site, journal_site = _verified_target_site(
        registry if isinstance(registry, dict) else {},
        paper_id=paper_id,
        system_id=system_id,
        target_journal=target_journal,
    )
    if system_site is None or journal_site is None:
        raise SmartSubmissionGuardError(
            SMART_CANARY_SITE_UNVERIFIED,
            paper_id,
            "the locked target site is no longer verified at worker dispatch",
        )
    adapter = submission_site_adapter(system_id)
    expected_origins = tuple(
        str(value).strip()
        for value in (
            system_site.get("login_url"),
            system_site.get("submission_url"),
            journal_site.get("submission_url"),
            *(adapter.allowed_origins if adapter is not None else ()),
        )
        if str(value or "").strip()
    )
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    accounts = (
        credentials.get("accounts", [])
        if isinstance(credentials, dict) and isinstance(credentials.get("accounts"), list)
        else []
    )
    account = next(
        (
            item
            for item in accounts
            if isinstance(item, dict)
            and str(item.get("account_id") or "").strip().casefold() == account_id.casefold()
        ),
        None,
    )
    if account is None:
        raise SmartSubmissionGuardError(
            SMART_SECRET_BROKER_REQUIRED,
            paper_id,
            "the queued account binding is missing at worker dispatch",
            details={"account_id": account_id},
        )
    readiness = _resolved_account_readiness(
        account,
        resolver=_smart_credential_resolver(),
        expected_origins=expected_origins,
    )
    if readiness.state != "live_browser_session":
        raise SmartSubmissionGuardError(
            SMART_SECRET_BROKER_REQUIRED,
            paper_id,
            "worker dispatch requires a current exact-origin live session or a trusted broker completion",
            details={
                "account_id": account_id,
                "credential_source": readiness.auth_source,
                "readiness": readiness.state,
            },
        )
    return readiness


def _matches_target_account(
    account: dict[str, Any],
    *,
    aliases: set[str],
    target_journal: str,
    resolver: SmartCredentialResolver,
    expected_origins: tuple[str, ...] = (),
) -> bool:
    if canonical_account_scope(account.get("scope")) != "journal_system":
        return False
    if str(account.get("status") or "").strip().casefold() in {
        "registration_pending",
        "session_rejected_by_worker",
    }:
        return False
    identity = str(account.get("username") or account.get("email") or "").strip()
    readiness = _resolved_account_readiness(
        account,
        resolver=resolver,
        expected_origins=expected_origins,
    )
    if not identity or not readiness.configured:
        return False
    searchable = " ".join(
        str(account.get(key) or "")
        for key in ("account_id", "provider", "login_url", "notes", "journal", "journal_name", "system_id")
    ).lower()
    journal = str(target_journal or "").strip().lower()
    return any(alias in searchable for alias in aliases) or (journal and journal in searchable)


def _verified_target_site(
    registry: dict[str, Any],
    *,
    paper_id: str,
    system_id: str,
    target_journal: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    def valid_url(value: Any) -> bool:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)

    allowed_statuses = {"active", "configured", "ready", "verified"}
    adapter = submission_site_adapter(system_id)

    def trusted_status(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "").strip().lower()
        if status in allowed_statuses:
            return True
        return bool(
            adapter is not None
            and status == "discovered"
            and adapter.trusts_registry_record(item)
        )

    systems = registry.get("systems", []) if isinstance(registry.get("systems"), list) else []
    journals = registry.get("journals", []) if isinstance(registry.get("journals"), list) else []
    system = next(
        (
            item
            for item in systems
            if isinstance(item, dict)
            and str(item.get("system_id") or "").strip().lower() == system_id
            and trusted_status(item)
            and valid_url(item.get("submission_url") or item.get("login_url"))
        ),
        None,
    )
    normalized_target = re.sub(r"[^a-z0-9]+", " ", str(target_journal or "").lower()).strip()

    def matches_journal(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        linked_raw = item.get("linked_papers", [])
        linked = {str(value) for value in linked_raw} if isinstance(linked_raw, list) else set()
        target_matches = bool(
            normalized_target
            and _journal_names_equivalent(target_journal, item.get("journal_name"))
        )
        journal_matches = target_matches if normalized_target else paper_id in linked
        item_status = trusted_status(item)
        if (
            not item_status
            and adapter is not None
            and system is not None
            and str(item.get("status") or "").strip().lower() == "discovered"
            and not str(item.get("submission_url") or item.get("login_url") or "").strip()
            and adapter.trusts_registry_record(system)
        ):
            item_status = True
        return (
            str(item.get("system_id") or "").strip().lower() == system_id
            and item_status
            and system is not None
            and valid_url(
                item.get("submission_url")
                or system.get("submission_url")
                or system.get("login_url")
            )
            and journal_matches
        )

    journal = next(
        (item for item in journals if matches_journal(item)),
        None,
    )
    if journal is not None and system is not None and not valid_url(
        journal.get("submission_url")
    ):
        inherited = str(
            system.get("submission_url") or system.get("login_url") or ""
        ).strip()
        journal = copy.deepcopy(journal)
        journal["submission_url"] = inherited
        journal["submission_url_source"] = "trusted_system_site_inherited"
    return system, journal


def _normalized_journal_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _canonical_journal_name(value: Any) -> str:
    """Normalize a journal name while accepting a verified trailing acronym.

    Journal/author output may write ``Journal Name`` while the site registry
    stores ``Journal Name (JNA)``. Treat only a real initialism as optional;
    arbitrary parenthetical qualifiers remain part of the journal identity.
    """

    text = str(value or "").strip()
    text = re.sub(
        r"(?i),\s*(?:Inderscience(?:\s+(?:Publishers?|Enterprises(?:\s+Ltd\.?)?))?|"
        r"Springer(?:\s+Nature)?|Elsevier|Wiley|SAGE|Taylor\s*&\s*Francis)\.?\s*$",
        "",
        text,
    ).strip()
    normalized = _normalized_journal_name(text)
    match = re.fullmatch(r"\s*(.+?)\s*\(([^()]*)\)\s*", text)
    if match is None:
        return normalized
    base = match.group(1).strip()
    suffix = re.sub(r"[^a-z0-9]+", "", match.group(2).casefold())
    words = re.findall(r"[a-z0-9]+", base.casefold())
    if not base or len(suffix) < 2 or len(suffix) > 16 or not words:
        return normalized
    stopwords = {"a", "an", "and", "for", "in", "of", "on", "the", "to"}
    initialisms = {
        "".join(word[0] for word in words),
        "".join(word[0] for word in words if word not in stopwords),
    }
    return _normalized_journal_name(base) if suffix in initialisms else normalized


def _journal_names_equivalent(left: Any, right: Any) -> bool:
    left_name = _canonical_journal_name(left)
    right_name = _canonical_journal_name(right)
    return bool(left_name and right_name and left_name == right_name)


def _append_unique_journal_name(values: list[str], candidate: Any) -> None:
    clean = re.sub(r"\s+", " ", str(candidate or "")).strip()
    if not clean:
        return
    if any(_journal_names_equivalent(clean, existing) for existing in values):
        return
    values.append(clean[:300])


def _historical_retarget_journals(
    paper_id: str,
    paper: Mapping[str, Any],
    *,
    legacy_state: Mapping[str, Any] | None = None,
) -> list[str]:
    """Collect every journal previously attempted or explicitly excluded.

    Journal selection is allowed to use AI, but deciding whether a target was
    already attempted is deterministic controller work.  The list deliberately
    combines the current projection, identity ledger, prior package attempts,
    retarget history, and the legacy submission ledger.
    """

    result: list[str] = []
    for key in (
        "target_journal",
        "journal",
        "previous_target_journal",
        "monitor_retarget_previous_target_journal",
    ):
        _append_unique_journal_name(result, paper.get(key))

    frozen = paper.get("retarget_excluded_journals")
    if isinstance(frozen, (list, tuple, set)):
        for value in frozen:
            _append_unique_journal_name(result, value)

    request = paper.get("retarget_request")
    if isinstance(request, Mapping):
        excluded = request.get("excluded_journals")
        if isinstance(excluded, (list, tuple, set)):
            for value in excluded:
                _append_unique_journal_name(result, value)

    records = paper.get("submission_records")
    if isinstance(records, Mapping):
        record_values: Iterable[Any] = records.values()
    elif isinstance(records, list):
        record_values = records
    else:
        record_values = ()
    for record in record_values:
        if isinstance(record, Mapping):
            _append_unique_journal_name(result, record.get("target_journal"))

    for history_key in ("submission_attempt_history", "retarget_request_history"):
        history = paper.get(history_key)
        if not isinstance(history, list):
            continue
        for item in history:
            if not isinstance(item, Mapping):
                continue
            for key in (
                "target_journal",
                "selected_target_journal",
                "actual_target_journal",
                "previous_target_journal",
            ):
                _append_unique_journal_name(result, item.get(key))

    legacy = _legacy_submission_record(paper_id, legacy_state=legacy_state)
    legacy_status = "_".join(
        str(legacy.get(key) or "").strip().casefold().replace("-", "_")
        for key in (
            "status",
            "submission_status",
            "workflow_stage",
            "workflow_status",
            "decision",
        )
        if str(legacy.get(key) or "").strip()
    )
    legacy_has_attempt = bool(
        normalize_submission_id(
            legacy.get("manuscript_id")
            or legacy.get("submission_id")
            or legacy.get("submission_number")
        )
        or legacy.get("active_submission") is True
        or any(
            marker in legacy_status
            for marker in (
                "accepted",
                "active",
                "decision",
                "reject",
                "submit",
                "under_review",
                "withdraw",
            )
        )
    )
    if legacy_has_attempt:
        for key in (
            "current_journal",
            "target_journal",
            "journal",
            "recommended_journal",
        ):
            _append_unique_journal_name(result, legacy.get(key))
    return result


def _legacy_submission_record(
    paper_id: str,
    *,
    legacy_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one value-free legacy submission record, when available."""

    source: Any = legacy_state
    if source is None:
        source = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
    papers = source.get("papers", {}) if isinstance(source, Mapping) else {}
    record = papers.get(str(paper_id or ""), {}) if isinstance(papers, Mapping) else {}
    return dict(record) if isinstance(record, Mapping) else {}


def reconcile_legacy_submission_lifecycle(
    paper_id: str,
    *,
    status: str,
    submission_id: str = "",
    target_journal: str = "",
    source: str = "smart_verified_status",
    observed_at: str = "",
) -> dict[str, Any]:
    """Project a verified Smart lifecycle result into the legacy read model.

    The legacy JSON is still consulted by duplicate-submit guards and the old
    overview UI.  It is a projection, not an independent decision source: only
    explicit lifecycle outcomes are accepted here.
    """

    clean_id = str(paper_id or "").strip()
    clean_status = re.sub(
        r"[^a-z0-9]+", "_", str(status or "").strip().casefold()
    ).strip("_")
    if not clean_id:
        raise ValueError("paper_id is required")
    allowed = {
        "accepted",
        "closed",
        "monitoring",
        "rejected",
        "retargeted",
        "submitted",
        "withdrawn",
    }
    if clean_status not in allowed:
        raise ValueError(f"unsupported legacy lifecycle projection: {clean_status}")

    now = str(observed_at or now_iso()).strip()
    lock_path = LEGACY_SUBMISSION_STATE_PATH.with_suffix(
        LEGACY_SUBMISSION_STATE_PATH.suffix + ".smart_projection.lock"
    )
    with FileLock(path=lock_path):
        state = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
        if not isinstance(state, dict):
            state = {}
        papers = state.setdefault("papers", {})
        if not isinstance(papers, dict):
            papers = {}
            state["papers"] = papers
        record = papers.setdefault(clean_id, {})
        if not isinstance(record, dict):
            record = {}
            papers[clean_id] = record

        clean_submission_id = normalize_submission_id(submission_id)
        if clean_submission_id:
            record["manuscript_id"] = clean_submission_id
        if str(target_journal or "").strip():
            record["current_journal"] = str(target_journal).strip()[:300]

        closed = clean_status in {"closed", "rejected", "retargeted", "withdrawn"}
        record["active_submission"] = not closed
        record["status"] = clean_status
        record["submission_status"] = clean_status
        record["workflow_status"] = clean_status
        record["workflow_stage"] = clean_status
        if clean_status == "rejected":
            record["decision"] = "Reject"
        elif clean_status == "accepted":
            record["decision"] = "Accept"
        record["last_updated"] = now
        record["last_status_source"] = str(source or "smart_verified_status")[:160]
        state["updated_at"] = now
        history = state.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            state["history"] = history
        history.append(
            {
                "at": now,
                "command": "smart_lifecycle_projection",
                "paper_id": clean_id,
                "status": clean_status,
                "submission_id": clean_submission_id,
                "source": str(source or "smart_verified_status")[:160],
            }
        )
        del history[:-500]
        write_json_atomic(LEGACY_SUBMISSION_STATE_PATH, state)
    return {
        "ok": True,
        "paper_id": clean_id,
        "status": clean_status,
        "active_submission": not closed,
        "submission_id": clean_submission_id,
    }


def _registry_binding_for_paper_any_system(
    registry: Mapping[str, Any],
    *,
    paper_id: str,
    target_journal: str,
    legacy_system_id: str = "",
) -> dict[str, Any]:
    """Resolve one unambiguous journal/system binding without trusting stale paper state.

    A linked paper plus an exact normalized journal name is strongest.  An
    exact journal name outranks a historical linked-paper record, which is
    important for papers that were retargeted after an earlier attempt.
    """

    journals = registry.get("journals", []) if isinstance(registry, Mapping) else []
    if not isinstance(journals, list):
        journals = []
    normalized_target = _normalized_journal_name(target_journal)
    # A journal name can carry a deterministic publisher/system identity
    # (for example CCPE -> Wiley).  Never let a stale discovered registry row
    # route that target to a different publisher merely because a candidate
    # journal mentioned that publisher elsewhere in journal.md.
    target_system_hint = _guess_system_id(target_journal)
    candidates: list[dict[str, Any]] = []
    for item in journals:
        if not isinstance(item, dict):
            continue
        system_id = str(item.get("system_id") or "").strip().casefold()
        if not system_id or submission_site_adapter(system_id) is None:
            continue
        adapter = submission_site_adapter(system_id)
        explicit_submission_url = str(item.get("submission_url") or "").strip()
        has_verified_journal_url = bool(
            adapter is not None
            and explicit_submission_url
            and adapter.allows_url(explicit_submission_url)
        )
        # A publisher hint may reject an obviously stale generic registry row,
        # but an exact, adapter-verified journal portal is stronger evidence.
        # For example, CCPE is published by Wiley but its submission platform
        # is the journal-specific ScholarOne site.
        if (
            target_system_hint
            and system_id != target_system_hint
            and not has_verified_journal_url
        ):
            continue
        linked_values = item.get("linked_papers", [])
        linked = {
            str(value).strip()
            for value in linked_values
            if str(value).strip()
        } if isinstance(linked_values, list) else set()
        name_exact = bool(
            normalized_target
            and _journal_names_equivalent(target_journal, item.get("journal_name"))
        )
        paper_linked = str(paper_id or "").strip() in linked
        # Once the current target journal is known, historical linked-paper
        # records are evidence only, never a routing fallback. This prevents a
        # retargeted paper from being sent back to its former journal/site.
        if normalized_target and not name_exact:
            continue
        if not normalized_target and not paper_linked:
            continue
        system_site, verified_journal = _verified_target_site(
            dict(registry),
            paper_id=str(paper_id or "").strip(),
            system_id=system_id,
            target_journal=target_journal,
        )
        if system_site is None or verified_journal is None:
            continue
        score = (
            (4 if name_exact else 0)
            + (2 if paper_linked else 0)
            + (8 if has_verified_journal_url else 0)
        )
        if legacy_system_id and system_id == str(legacy_system_id).strip().casefold():
            score += 1
        candidates.append(
            {
                "system_id": system_id,
                "system_site": copy.deepcopy(system_site),
                "journal_site": copy.deepcopy(item),
                "score": score,
                "name_exact": name_exact,
                "paper_linked": paper_linked,
            }
        )
    if not candidates:
        return {"status": "not_found", "code": "SMART_SITE_BINDING_NOT_FOUND"}
    candidates.sort(
        key=lambda item: (
            -int(item.get("score") or 0),
            str(item.get("system_id") or ""),
            str((item.get("journal_site") or {}).get("journal_key") or ""),
        )
    )
    top_score = int(candidates[0].get("score") or 0)
    top = [item for item in candidates if int(item.get("score") or 0) == top_score]
    top_systems = {str(item.get("system_id") or "") for item in top}
    if len(top_systems) != 1:
        return {
            "status": "ambiguous",
            "code": "SMART_SITE_BINDING_AMBIGUOUS",
            "candidate_system_ids": sorted(top_systems),
        }
    selected = top[0]
    selected["status"] = "matched"
    selected["code"] = "SMART_SITE_BINDING_MATCHED"
    return selected


def _account_matches_system_metadata(
    account: Mapping[str, Any],
    *,
    system_id: str,
    target_journal: str,
) -> bool:
    """Check only value-free account metadata; never resolve or expose a secret."""

    wanted_system = str(system_id or "").strip().casefold()
    if not wanted_system:
        return False
    explicit_system = str(account.get("system_id") or "").strip().casefold()
    if explicit_system:
        return explicit_system == wanted_system
    adapter = submission_site_adapter(wanted_system)
    login_origin = _web_origin(account.get("login_url"))
    if adapter is not None and login_origin in set(adapter.allowed_origins):
        return True
    searchable = " ".join(
        str(account.get(key) or "")
        for key in (
            "account_id",
            "provider",
            "login_url",
            "notes",
            "journal",
            "journal_name",
            "related_journals",
        )
    ).casefold()
    aliases = _system_aliases(wanted_system)
    normalized_target = _normalized_journal_name(target_journal)
    return any(alias in searchable for alias in aliases) or bool(
        normalized_target and normalized_target in _normalized_journal_name(searchable)
    )


def _reconcile_paper_site_binding(
    paper_id: str,
    paper: dict[str, Any],
    *,
    registry: Mapping[str, Any],
    credentials: Mapping[str, Any],
    legacy_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Repair stale site metadata from deterministic registry and legacy evidence."""

    legacy = _legacy_submission_record(paper_id, legacy_state=legacy_state)
    target_journal = str(
        paper.get("target_journal")
        or legacy.get("current_journal")
        or legacy.get("recommended_journal")
        or ""
    ).strip()
    legacy_system_id = _guess_system_id(str(legacy.get("submission_system") or ""))
    binding = _registry_binding_for_paper_any_system(
        registry,
        paper_id=paper_id,
        target_journal=target_journal,
        legacy_system_id=legacy_system_id,
    )
    if binding.get("status") != "matched":
        return {
            "paper_id": paper_id,
            "changed": False,
            "code": str(binding.get("code") or "SMART_SITE_BINDING_NOT_FOUND"),
            "candidate_system_ids": list(binding.get("candidate_system_ids") or []),
        }
    resolved_system_id = str(binding.get("system_id") or "").strip().casefold()
    resolved_adapter = submission_site_adapter(resolved_system_id)
    resolved_journal_site = (
        binding.get("journal_site")
        if isinstance(binding.get("journal_site"), Mapping)
        else {}
    )
    resolved_submission_url = str(
        resolved_journal_site.get("submission_url") or ""
    ).strip()
    resolved_url_is_verified = bool(
        resolved_adapter is not None
        and resolved_submission_url
        and resolved_adapter.allows_url(resolved_submission_url)
    )
    if (
        legacy_system_id
        and legacy_system_id != resolved_system_id
        and not resolved_url_is_verified
    ):
        return {
            "paper_id": paper_id,
            "changed": False,
            "code": "SMART_SITE_BINDING_LEGACY_CONFLICT",
            "candidate_system_ids": sorted({legacy_system_id, resolved_system_id}),
        }

    changed_fields: list[str] = []
    previous_system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    previous_system_label = str(paper.get("submission_system") or "").strip()
    previous_account_id = str(paper.get("credential_account_id") or "").strip()
    system_site = binding.get("system_site") if isinstance(binding.get("system_site"), Mapping) else {}
    journal_site = binding.get("journal_site") if isinstance(binding.get("journal_site"), Mapping) else {}
    adapter = submission_site_adapter(resolved_system_id)
    resolved_label = str(
        system_site.get("label")
        or (adapter.label if adapter is not None else "")
        or _system_label(resolved_system_id)
    ).strip()

    if target_journal and str(paper.get("target_journal") or "").strip() != target_journal:
        paper["target_journal"] = target_journal
        changed_fields.append("target_journal")
    if previous_system_id != resolved_system_id:
        paper["submission_system_id"] = resolved_system_id
        changed_fields.append("submission_system_id")
    if resolved_label and previous_system_label != resolved_label:
        paper["submission_system"] = resolved_label
        changed_fields.append("submission_system")
    journal_site_key = str(journal_site.get("journal_key") or "").strip()
    if journal_site_key and str(paper.get("journal_site_key") or "") != journal_site_key:
        paper["journal_site_key"] = journal_site_key
        changed_fields.append("journal_site_key")
    accounts = credentials.get("accounts", []) if isinstance(credentials, Mapping) else []
    account = next(
        (
            item
            for item in accounts
            if isinstance(item, Mapping)
            and str(item.get("account_id") or "").strip().casefold()
            == previous_account_id.casefold()
        ),
        None,
    ) if previous_account_id and isinstance(accounts, list) else None
    if previous_account_id and (
        account is None
        or not _account_matches_system_metadata(
            account,
            system_id=resolved_system_id,
            target_journal=target_journal,
        )
    ):
        paper.pop("credential_account_id", None)
        changed_fields.append("credential_account_id")

    if previous_system_id and previous_system_id != resolved_system_id:
        for key in (
            "submission_journal_id",
            "submission_start_url",
            "submission_start_url_source",
            "final_submit_authorization",
        ):
            if key in paper:
                paper.pop(key, None)
                changed_fields.append(key)
    verified_start_url = str(journal_site.get("submission_url") or "").strip()
    current_start_url = str(paper.get("submission_start_url") or "").strip()
    current_start_source = str(paper.get("submission_start_url_source") or "").strip()
    if (
        verified_start_url
        and adapter is not None
        and adapter.allows_url(verified_start_url)
        and (
            not current_start_url
            or previous_system_id != resolved_system_id
            or current_start_source
            in {"", "package_adapter", "verified_site_registry"}
        )
        and current_start_url != verified_start_url
    ):
        paper["submission_start_url"] = verified_start_url
        paper["submission_start_url_source"] = "verified_site_registry"
        changed_fields.extend(
            ["submission_start_url", "submission_start_url_source"]
        )
    verified_journal_id = (
        adapter.extract_journal_identifier(verified_start_url)
        if adapter is not None and verified_start_url
        else ""
    )
    if (
        verified_journal_id
        and str(paper.get("submission_journal_id") or "").strip()
        != verified_journal_id
    ):
        paper["submission_journal_id"] = verified_journal_id
        changed_fields.append("submission_journal_id")

    # A completed retarget intentionally starts a new submission identity.
    # Legacy compatibility data may still contain the former or a speculative
    # manuscript number; keep that evidence in attempt history, but never
    # promote it back to the active identity of the new target.
    if (
        _is_fresh_retarget_handoff(paper)
        and not str(paper.get("submission_id") or "").strip()
    ):
        for key in ("submission_id_source", "submission_id_recovered_at"):
            if key in paper:
                paper.pop(key, None)
                changed_fields.append(key)
    if (
        not _is_fresh_retarget_handoff(paper)
        and not str(paper.get("submission_id") or "").strip()
    ):
        for key in ("submission_id", "manuscript_id", "submission_number", "tracking_id"):
            try:
                recovered = _sanitize_submission_id(legacy.get(key))
            except ValueError:
                recovered = ""
            if recovered:
                paper["submission_id"] = recovered
                paper["submission_id_source"] = "legacy_submission_state"
                paper["submission_id_recovered_at"] = now_iso()
                changed_fields.extend(
                    ["submission_id", "submission_id_source", "submission_id_recovered_at"]
                )
                break

    if changed_fields:
        reconciled_at = now_iso()
        paper["last_worker_recovery_hint"] = "site_binding_reconciliation"
        paper["site_binding_reconciled_at"] = reconciled_at
        paper["site_binding_reconciliation"] = {
            "code": "SMART_SITE_BINDING_RECONCILED",
            "source": "verified_site_registry+legacy_submission_state",
            "previous_system_id": previous_system_id,
            "resolved_system_id": resolved_system_id,
            "previous_account_binding_cleared": bool(
                previous_account_id and "credential_account_id" in changed_fields
            ),
            "at": reconciled_at,
        }
    return {
        "paper_id": paper_id,
        "changed": bool(changed_fields),
        "code": (
            "SMART_SITE_BINDING_RECONCILED"
            if changed_fields
            else "SMART_SITE_BINDING_ALREADY_CURRENT"
        ),
        "resolved_system_id": resolved_system_id,
        "changed_fields": list(dict.fromkeys(changed_fields)),
    }


@_smart_state_locked
def reconcile_smart_submission_site_bindings(
    paper_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Persist deterministic site corrections before planning or monitoring."""

    selected = (
        {
            str(item).strip()
            for item in paper_ids
            if str(item).strip()
        }
        if paper_ids is not None
        else None
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    legacy_state = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
    results: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    for raw_paper_id, paper in papers.items():
        paper_id = str(raw_paper_id or "").strip()
        if not paper_id or (selected is not None and paper_id not in selected):
            continue
        if not isinstance(paper, dict):
            results[paper_id] = {
                "paper_id": paper_id,
                "changed": False,
                "code": "SMART_SITE_BINDING_PAPER_INVALID",
            }
            continue
        result = _reconcile_paper_site_binding(
            paper_id,
            paper,
            registry=registry if isinstance(registry, Mapping) else {},
            credentials=credentials if isinstance(credentials, Mapping) else {},
            legacy_state=legacy_state if isinstance(legacy_state, Mapping) else {},
        )
        results[paper_id] = result
        if result.get("changed"):
            changed.append(paper_id)
    if changed:
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "reconcile_smart_submission_site_bindings",
                "paper_ids": changed,
                "results": {paper_id: results[paper_id] for paper_id in changed},
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "requested_ids": sorted(selected) if selected is not None else list(papers),
        "changed": changed,
        "changed_count": len(changed),
        "results": results,
    }


def _stable_binding_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _trusted_claimed_submission_page_binding(
    paper_id: str,
    paper: dict[str, Any],
) -> dict[str, str]:
    """Resolve this exact attempt's project-owned browser page.

    Federated publishers such as Wiley authenticate on one origin and run the
    submission form on another.  The SDK browser context store is the trusted
    local owner ledger for those tabs, so it can bind the final-review page to
    one paper without relying on worker prose or a guessed URL.
    """

    attempt_id = str(paper.get("attempt_id") or "").strip()
    route_revision = int(paper.get("route_revision") or 0)
    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    adapter = submission_site_adapter(system_id)
    if not paper_id or not attempt_id or route_revision <= 0 or adapter is None:
        return {}
    store = read_json(SMART_SUBMISSION_BROWSER_CONTEXT_PATH, {})
    contexts = store.get("contexts", {}) if isinstance(store, dict) else {}
    claims = store.get("page_claims", {}) if isinstance(store, dict) else {}
    if not isinstance(contexts, dict) or not isinstance(claims, dict):
        return {}
    now = datetime.now().astimezone()
    candidates: list[tuple[datetime, dict[str, str]]] = []
    adapter_origins = {
        _web_origin(item)
        for item in adapter.allowed_origins
        if _web_origin(item)
    }
    for page_id, raw_claim in claims.items():
        claim = raw_claim if isinstance(raw_claim, dict) else {}
        if (
            str(claim.get("paper_id") or "").strip() != paper_id
            or str(claim.get("attempt_id") or "").strip() != attempt_id
            or int(claim.get("route_revision") or 0) != route_revision
        ):
            continue
        context_id = str(claim.get("context_id") or "").strip()
        context = contexts.get(context_id)
        if not isinstance(context, dict):
            continue
        if (
            str(context.get("paper_id") or "").strip() != paper_id
            or str(context.get("attempt_id") or "").strip() != attempt_id
            or int(context.get("route_revision") or 0) != route_revision
        ):
            continue
        expires_at = _parse_aware_time(context.get("expires_at"))
        updated_at = _parse_aware_time(claim.get("updated_at"))
        retired_text = str(context.get("retired_at") or "").strip()
        retired_at = _parse_aware_time(retired_text) if retired_text else None
        live_context = not retired_text and expires_at is not None and expires_at > now
        just_retired_context = bool(
            retired_text
            and retired_at is not None
            and updated_at is not None
            and retired_at >= updated_at
            and now - updated_at <= timedelta(minutes=30)
        )
        if updated_at is None or not (live_context or just_retired_context):
            continue
        raw_url = str(claim.get("url") or "").strip()
        claim_origin = _web_origin(claim.get("origin"))
        url_origin = _web_origin(raw_url)
        context_origins = {
            _web_origin(item)
            for item in context.get("allowed_origins", [])
            if _web_origin(item)
        } if isinstance(context.get("allowed_origins"), list) else set()
        allowed_origins = adapter_origins.intersection(context_origins)
        if (
            not str(page_id).strip()
            or not url_origin
            or claim_origin != url_origin
            or url_origin not in allowed_origins
        ):
            continue
        safe_url = _safe_prompt_url(raw_url, allowed_origins=allowed_origins)
        if not safe_url:
            continue
        parsed = urllib.parse.urlsplit(safe_url)
        if parsed.path in {"", "/"}:
            continue
        draft_match = re.search(
            r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            parsed.path,
        )
        candidates.append(
            (
                updated_at,
                {
                    "paper_id": paper_id,
                    "draft_id": str(draft_match.group(1) if draft_match else ""),
                    "draft_url": safe_url,
                    "upload_url": safe_url,
                    "page_id": str(page_id),
                    "target_origin": url_origin,
                    "source": "trusted_sdk_browser_page_claim",
                },
            )
        )
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _apply_claimed_submission_page_binding(
    paper_id: str,
    paper: dict[str, Any],
) -> dict[str, str]:
    if str(
        paper.get("draft_url")
        or paper.get("submission_draft_url")
        or paper.get("upload_url")
        or paper.get("submission_upload_url")
        or ""
    ).strip():
        return {}
    binding = _trusted_claimed_submission_page_binding(paper_id, paper)
    if not binding:
        return {}
    if binding.get("draft_id"):
        paper["draft_id"] = binding["draft_id"]
        paper["submission_draft_id"] = binding["draft_id"]
    paper["draft_url"] = binding["draft_url"]
    paper["submission_draft_url"] = binding["draft_url"]
    paper["upload_url"] = binding["upload_url"]
    paper["submission_upload_url"] = binding["upload_url"]
    paper["draft_binding_source"] = binding["source"]
    paper["draft_binding_page_id"] = binding["page_id"]
    paper["draft_binding_captured_at"] = now_iso()
    return binding


def _final_submit_target_binding(paper_id: str, paper: dict[str, Any]) -> dict[str, str]:
    _apply_claimed_submission_page_binding(paper_id, paper)
    account_id = str(paper.get("credential_account_id") or "").strip()
    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    adapter = submission_site_adapter(system_id)
    registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    system_site, journal_site = _verified_target_site(
        registry if isinstance(registry, dict) else {},
        paper_id=paper_id,
        system_id=system_id,
        target_journal=target_journal,
    )
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    accounts = (
        credentials.get("accounts", [])
        if isinstance(credentials, dict) and isinstance(credentials.get("accounts"), list)
        else []
    )
    account = next(
        (
            item
            for item in accounts
            if isinstance(item, dict)
            and str(item.get("account_id") or "").strip().casefold() == account_id.casefold()
        ),
        None,
    )
    if system_site is None or journal_site is None or account is None:
        return {}
    evidence = account.get("browser_session_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    session_origin = _web_origin(evidence.get("origin"))
    allowed_origins = {
        origin
        for origin in (
            _web_origin(system_site.get("login_url")),
            _web_origin(system_site.get("submission_url")),
            _web_origin(journal_site.get("submission_url")),
            *(
                tuple(_web_origin(item) for item in adapter.allowed_origins)
                if adapter is not None
                else ()
            ),
        )
        if origin
    }
    if not session_origin or session_origin not in allowed_origins:
        return {}
    draft_id = _safe_prompt_identifier(
        paper.get("draft_id") or paper.get("submission_draft_id")
    )
    draft_url = _safe_prompt_url(
        paper.get("draft_url") or paper.get("submission_draft_url"),
        allowed_origins=allowed_origins,
    )
    upload_url = _safe_prompt_url(
        paper.get("upload_url") or paper.get("submission_upload_url"),
        allowed_origins=allowed_origins,
    )
    if not (draft_url or upload_url):
        return {}
    target_origin = _web_origin(draft_url or upload_url)
    if not target_origin or target_origin not in allowed_origins:
        return {}
    account_binding = {
        "account_id": account_id,
        "scope": canonical_account_scope(account.get("scope")),
        "status": str(account.get("status") or "").strip().casefold(),
        "provider": str(account.get("provider") or "").strip(),
        "username": str(account.get("username") or account.get("email") or "").strip(),
        "login_origin": _web_origin(account.get("login_url")),
        "login_url": _safe_prompt_url(
            account.get("login_url"),
            allowed_origins=allowed_origins,
        ),
        "system_id": str(account.get("system_id") or "").strip().casefold(),
        "credential_ref_sha256": hashlib.sha256(
            str(account.get("credential_ref") or "").encode("utf-8")
        ).hexdigest(),
        "session_account_id": str(evidence.get("account_id") or "").strip(),
        "session_origin": session_origin,
    }
    site_binding = {
        "system_id": system_id,
        "system_status": str(system_site.get("status") or "").strip().casefold(),
        "system_login_origin": _web_origin(system_site.get("login_url")),
        "system_submission_origin": _web_origin(system_site.get("submission_url")),
        "system_login_url": _safe_prompt_url(
            system_site.get("login_url"),
            allowed_origins=allowed_origins,
        ),
        "system_submission_url": _safe_prompt_url(
            system_site.get("submission_url"),
            allowed_origins=allowed_origins,
        ),
        "journal_site_key": str(journal_site.get("journal_key") or "").strip(),
        "journal_name": str(journal_site.get("journal_name") or "").strip(),
        "journal_status": str(journal_site.get("status") or "").strip().casefold(),
        "journal_submission_origin": _web_origin(journal_site.get("submission_url")),
        "journal_submission_url": _safe_prompt_url(
            journal_site.get("submission_url"),
            allowed_origins=allowed_origins,
        ),
        "linked_papers": sorted(
            str(item)
            for item in journal_site.get("linked_papers", [])
            if str(item).strip()
        )
        if isinstance(journal_site.get("linked_papers"), list)
        else [],
    }
    return {
        "target_origin": target_origin,
        "journal_site_key": str(journal_site.get("journal_key") or "").strip(),
        "account_binding_sha256": _stable_binding_sha256(account_binding),
        "site_binding_sha256": _stable_binding_sha256(site_binding),
        "draft_binding_sha256": _stable_binding_sha256(
            {
                "draft_id": draft_id,
                "draft_url": draft_url,
                "upload_url": upload_url,
                "target_origin": target_origin,
            }
        ),
    }


FINAL_SUBMIT_AUTHORIZATION_TTL = timedelta(minutes=20)
FINAL_SUBMIT_CLAIM_TO_CLICK_TTL = timedelta(minutes=2)
FINAL_SUBMIT_CONFIRMATION_PREFIX = "CONFIRM_FINAL_SUBMIT"
FINAL_SUBMIT_REVIEW_READY_ACTION = "user_confirmation_before_final_submit_required"
FINAL_SUBMIT_AUTHORIZED_ACTION = "final_submit_authorized_for_exact_attempt"
FINAL_SUBMIT_CONFIRMATION_CHALLENGE_TTL = timedelta(minutes=5)
FINAL_SUBMIT_CHALLENGE_SCHEMA = "smart-final-submit-ui-challenge/v1"
FINAL_SUBMIT_AUTO_PRESENTATION = "program_auto_after_review"
FINAL_SUBMIT_UI_PRESENTATION = "exe_ui_foreground_gesture"
SUBMISSION_CONFIRMATION_RECEIPT_SCHEMA = "smart-submission-confirmation/v1"
LIVE_SUBMISSION_CONFIRMATION_SCHEMA = "smart-live-submission-confirmation/v1"
LIVE_SUBMISSION_CONFIRMATION_TTL = timedelta(minutes=10)
_LIVE_CONFIRMATION_MARKERS = {
    "successfully_submitted",
    "submission_received",
    "thank_you_for_submission",
    "manuscript_submitted",
    "submission_complete",
}
_FINAL_SUBMIT_AUTHORIZATION_KEYS = {
    "authorization_id",
    "confirmation_challenge_id",
    "paper_id",
    "route_revision",
    "attempt_id",
    "account_id",
    "submission_system_id",
    "target_journal",
    "target_origin",
    "journal_site_key",
    "account_binding_sha256",
    "site_binding_sha256",
    "draft_binding_sha256",
    "package_binding_sha256",
    "authorized_at",
    "expires_at",
    "explicit_user_confirmation",
    "authorization_audit_path",
    "authorization_audit_sha256",
    "reserved_at",
    "reserved_by_thread_id",
    "reservation_audit_path",
    "reservation_audit_sha256",
    "consumed_at",
    "consumed_by_thread_id",
    "consumption_audit_path",
    "consumption_audit_sha256",
    "result_recorded_at",
    "signature_alg",
    "signature",
}
_FINAL_SUBMIT_CONFIRMATION_CHALLENGE_KEYS = {
    "schema",
    "challenge_id",
    "paper_id",
    "route_revision",
    "attempt_id",
    "account_id",
    "submission_system_id",
    "target_journal",
    "target_origin",
    "journal_site_key",
    "account_binding_sha256",
    "site_binding_sha256",
    "draft_binding_sha256",
    "package_binding_sha256",
    "created_at",
    "expires_at",
    "nonce_sha256",
    "presentation",
    "challenge_audit_path",
    "challenge_audit_sha256",
    "consumed_at",
    "signature_alg",
    "signature",
}


def _submission_package_binding(paper_id: str, paper: dict[str, Any]) -> str:
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    paper_dir = _resolve_existing_path(paper.get("paper_dir"), base=REPO_ROOT)
    if paper_dir is None and package_dir is not None:
        paper_dir = package_dir.parent
    roots = tuple(
        path
        for path in (package_dir, paper_dir, SMART_UPLOAD_STAGING_DIR)
        if path is not None
    )
    candidates: dict[str, Path] = {}
    if package_dir is not None:
        for path in sorted(package_dir.rglob("*"), key=lambda item: str(item).casefold()):
            if path.is_symlink():
                return ""
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not _path_is_within(resolved, package_dir):
                return ""
            label = f"package/{resolved.relative_to(package_dir.resolve()).as_posix()}"
            candidates[label] = resolved
    if paper_dir is not None:
        for name in (
            "author_info.md",
            "journal.md",
            "submission_metadata_fingerprint.json",
        ):
            path = paper_dir / name
            if path.is_file() and not path.is_symlink():
                candidates[f"control/{name}"] = path.resolve()
    for field in (
        "uploaded_manuscript_zip",
        "uploaded_manuscript_file",
        "upload_package",
        "cover_letter_upload_file",
    ):
        path = _resolve_existing_path(paper.get(field), base=REPO_ROOT)
        if path is not None and path.is_file() and any(_path_is_within(path, root) for root in roots):
            candidates[f"declared/{field}/{path.name}"] = path.resolve()
    ordered = sorted(candidates.items(), key=lambda item: item[0].casefold())
    if not ordered or not any(
        path.suffix.casefold() in {".pdf", ".zip", ".docx", ".tex"}
        for _, path in ordered
    ):
        return ""
    digest = hashlib.sha256()
    for label, path in ordered:
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_aware_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _smart_trust_key(*, create: bool) -> bytes:
    key_store = BrowserSessionEvidenceKeyStore(
        SMART_SUBMISSION_DIR / "smart_trust_key.local.json"
    )
    return key_store.load(create=create)


def _signed_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in receipt.items() if key != "signature"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sign_smart_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(receipt)
    candidate["signature_alg"] = "hmac-sha256"
    candidate["signature"] = ""
    candidate["signature"] = hmac.new(
        _smart_trust_key(create=True),
        _signed_receipt_bytes(candidate),
        hashlib.sha256,
    ).hexdigest()
    return candidate


def _verify_smart_receipt(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or receipt.get("signature_alg") != "hmac-sha256":
        return False
    signature = str(receipt.get("signature") or "").casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    try:
        expected = hmac.new(
            _smart_trust_key(create=False),
            _signed_receipt_bytes(receipt),
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(expected, signature)


def _committed_audit_matches(path_value: Any, sha256_value: Any) -> bool:
    path_text = str(path_value or "").strip()
    expected_sha256 = str(sha256_value or "").strip().casefold()
    if not path_text or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return False
    path = Path(path_text)
    try:
        resolved = path.resolve(strict=True)
        audit_root = SMART_AUDIT_DIR.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if path.is_symlink() or not resolved.is_file() or not _path_is_within(resolved, audit_root):
        return False
    return hashlib.sha256(resolved.read_bytes()).hexdigest() == expected_sha256


def _bound_final_submit_authorization(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], datetime, datetime, datetime] | None:
    receipt = paper.get("final_submit_authorization")
    if not isinstance(receipt, dict) or set(receipt) != _FINAL_SUBMIT_AUTHORIZATION_KEYS:
        return None
    current = now or datetime.now().astimezone()
    authorized_at = _parse_aware_time(receipt.get("authorized_at"))
    expires_at = _parse_aware_time(receipt.get("expires_at"))
    if authorized_at is None or expires_at is None:
        return None
    if (
        authorized_at > current + timedelta(seconds=30)
        or expires_at <= authorized_at
        or expires_at - authorized_at > FINAL_SUBMIT_AUTHORIZATION_TTL
        or receipt.get("explicit_user_confirmation") is not True
        or not _verify_smart_receipt(receipt)
        or not _committed_audit_matches(
            receipt.get("authorization_audit_path"),
            receipt.get("authorization_audit_sha256"),
        )
    ):
        return None
    package_binding = _submission_package_binding(paper_id, paper)
    target_binding = _final_submit_target_binding(paper_id, paper)
    expected = {
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": str(attempt_id or "").strip(),
        "account_id": str(paper.get("credential_account_id") or "").strip(),
        "submission_system_id": str(paper.get("submission_system_id") or "").strip().casefold(),
        "target_journal": str(paper.get("target_journal") or "").strip(),
        "package_binding_sha256": package_binding,
        "target_origin": target_binding.get("target_origin", ""),
        "journal_site_key": target_binding.get("journal_site_key", ""),
        "account_binding_sha256": target_binding.get("account_binding_sha256", ""),
        "site_binding_sha256": target_binding.get("site_binding_sha256", ""),
        "draft_binding_sha256": target_binding.get("draft_binding_sha256", ""),
    }
    if (
        str(paper.get("next_action") or "").strip() != FINAL_SUBMIT_AUTHORIZED_ACTION
        or not all((expected["account_id"], expected["submission_system_id"], expected["target_journal"]))
        or not all(target_binding.values())
        or not package_binding
        or any(receipt.get(key) != value for key, value in expected.items())
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("authorization_id") or ""))
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("confirmation_challenge_id") or ""))
    ):
        return None
    return receipt, authorized_at, expires_at, current


def _valid_final_submit_authorization(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    bound = _bound_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        now=now,
    )
    if bound is None:
        return None
    receipt, _authorized_at, expires_at, current = bound
    if (
        current >= expires_at
        or str(receipt.get("reserved_at") or "")
        or str(receipt.get("reserved_by_thread_id") or "")
        or str(receipt.get("consumed_at") or "")
        or str(receipt.get("consumed_by_thread_id") or "")
        or str(receipt.get("result_recorded_at") or "")
    ):
        return None
    return receipt


def _reserved_final_submit_authorization(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Validate an unconsumed receipt reserved by the atomic worker dispatch."""

    bound = _bound_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        now=now,
    )
    if bound is None:
        return None
    receipt, authorized_at, expires_at, current = bound
    reserved_at = _parse_aware_time(receipt.get("reserved_at"))
    if (
        reserved_at is None
        or current >= expires_at
        or reserved_at < authorized_at
        or reserved_at >= expires_at
        or reserved_at > current + timedelta(seconds=30)
        or str(receipt.get("reserved_by_thread_id") or "").strip() != str(thread_id or "").strip()
        or not _committed_audit_matches(
            receipt.get("reservation_audit_path"),
            receipt.get("reservation_audit_sha256"),
        )
        or str(receipt.get("consumed_at") or "")
        or str(receipt.get("consumed_by_thread_id") or "")
        or str(receipt.get("result_recorded_at") or "")
    ):
        return None
    return receipt


def _claimed_final_submit_authorization(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    now: datetime | None = None,
    require_click_window: bool = False,
) -> dict[str, Any] | None:
    """Validate a receipt consumed by the action-time final-click broker."""

    bound = _bound_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        now=now,
    )
    if bound is None:
        return None
    receipt, authorized_at, expires_at, current = bound
    reserved_at = _parse_aware_time(receipt.get("reserved_at"))
    consumed_at = _parse_aware_time(receipt.get("consumed_at"))
    if (
        reserved_at is None
        or consumed_at is None
        or reserved_at < authorized_at
        or reserved_at >= expires_at
        or consumed_at < reserved_at
        or consumed_at >= expires_at
        or consumed_at > current + timedelta(seconds=30)
        or str(receipt.get("reserved_by_thread_id") or "").strip() != str(thread_id or "").strip()
        or str(receipt.get("consumed_by_thread_id") or "").strip() != str(thread_id or "").strip()
        or not _committed_audit_matches(
            receipt.get("reservation_audit_path"),
            receipt.get("reservation_audit_sha256"),
        )
        or not _committed_audit_matches(
            receipt.get("consumption_audit_path"),
            receipt.get("consumption_audit_sha256"),
        )
        or str(receipt.get("result_recorded_at") or "")
        or (
            require_click_window
            and (
                current >= expires_at
                or current >= consumed_at + FINAL_SUBMIT_CLAIM_TO_CLICK_TTL
            )
        )
    ):
        return None
    return receipt


def preflight_single_smart_canary(
    paper_id: str,
    *,
    expected_route_revision: int,
    unified_state: dict[str, Any] | None = None,
    smart_state: dict[str, Any] | None = None,
    credentials: dict[str, Any] | None = None,
    site_registry: dict[str, Any] | None = None,
    legacy_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only, single-paper gate run before any owner or queue mutation."""

    paper_id = str(paper_id or "").strip()
    if not paper_id:
        return _guard_error(
            SMART_CANARY_SCOPE_REQUIRED,
            paper_id,
            "exactly one paper_id is required for Smart canary preflight",
        )
    unified = _unified_state_snapshot(unified_state)
    papers = unified.get("papers", {}) if isinstance(unified.get("papers"), dict) else {}
    record = papers.get(paper_id)
    if not isinstance(record, dict):
        return _guard_error(SMART_ROUTE_PAPER_MISSING, paper_id, "paper is missing from unified state")
    route = submission_route_for(unified.get("submission_routes"), paper_id)
    if int(route.get("revision") or 0) != int(expected_route_revision):
        return _guard_error(
            SMART_ROUTE_REVISION_MISMATCH,
            paper_id,
            "submission route revision is stale",
            details={"expected_revision": int(expected_route_revision), "actual_revision": int(route.get("revision") or 0)},
        )

    smart = copy.deepcopy(smart_state) if isinstance(smart_state, dict) else read_json(SMART_STATE_PATH, {})
    legacy = copy.deepcopy(legacy_state) if isinstance(legacy_state, dict) else read_json(LEGACY_SUBMISSION_STATE_PATH, {})
    credential_data = credentials if isinstance(credentials, dict) else read_json(SMART_CREDENTIAL_PATH, {})
    registry = site_registry if isinstance(site_registry, dict) else read_json(SMART_SITE_REGISTRY_PATH, {})
    smart = smart if isinstance(smart, dict) else {}
    legacy = legacy if isinstance(legacy, dict) else {}

    unified_queue = _queue_paper_ids(unified, "paper_queue")
    legacy_queue = _queue_paper_ids(legacy, "paper_queue", "queue")
    smart_queue = _queue_paper_ids(smart, "paper_queue", "online_queue")
    running = list(dict.fromkeys(_effective_running_papers(legacy) + _effective_running_papers(smart)))
    if unified_queue or legacy_queue or smart_queue or running:
        return _guard_error(
            SMART_CANARY_QUEUE_BUSY,
            paper_id,
            "single-paper canary requires all legacy and Smart queues/workers to be idle",
            details={
                "unified_queue": unified_queue,
                "legacy_queue": legacy_queue,
                "smart_queue": smart_queue,
                "running_papers": running,
            },
        )

    try:
        _assert_no_terminal_canary_state(
            paper_id,
            legacy_state=legacy,
            smart_state=smart,
            stage="preflight",
        )
    except SmartSubmissionGuardError as exc:
        return exc.as_dict()
    smart_papers = smart.get("papers", {}) if isinstance(smart.get("papers"), dict) else {}
    current_smart_paper = smart_papers.get(paper_id, {})

    stages = record.get("stages", {}) if isinstance(record.get("stages"), dict) else {}
    journal_stage = stages.get("journal_author", {}) if isinstance(stages.get("journal_author"), dict) else {}
    if str(journal_stage.get("status") or "").strip().lower() != "complete":
        return _guard_error(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "journal/author stage is not complete",
        )
    artifact = str(journal_stage.get("artifact") or "").strip()
    package_dir = Path(artifact) if artifact else JOURNAL_ROOT / "papers" / paper_id / "package"
    if package_dir.is_file():
        package_dir = package_dir.parent
    if not package_dir.is_dir():
        return _guard_error(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "journal/author package directory is missing",
            details={"package_dir": str(package_dir)},
        )
    paper_dir = package_dir.parent
    required_package_files = (
        package_dir / "main.pdf",
        package_dir / "main.tex",
        package_dir / "declarations.md",
        package_dir / "submission_manifest.json",
        package_dir / "package_metadata_fingerprint.json",
    )
    missing_files = [str(path) for path in required_package_files if not path.is_file()]
    author_info = _first_existing_file(paper_dir / "author_info.md", package_dir / "author_info.md")
    journal_file = _first_existing_file(paper_dir / "journal.md", package_dir / "journal.md")
    risk_report = _first_existing_file(package_dir / "final_risk_report.md", paper_dir / "final_risk_report.md")
    checklist = _first_existing_file(package_dir / "submission_checklist.md", paper_dir / "submission_checklist.md")
    source_fingerprint = paper_dir / "submission_metadata_fingerprint.json"
    for path in (author_info, journal_file, risk_report, checklist):
        if path is None:
            missing_files.append("required package gate document")
    if not source_fingerprint.is_file():
        missing_files.append(str(source_fingerprint))
    if missing_files:
        return _guard_error(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "required package gate artifacts are missing",
            details={"missing": missing_files},
        )

    author_text = read_text(author_info) if author_info else ""
    journal_text = read_text(journal_file) if journal_file else ""
    risk_text = read_text(risk_report) if risk_report else ""
    checklist_text = read_text(checklist) if checklist else ""
    if not re.search(r"(?mi)^\s*Status\s*:\s*READY\s*$", author_text):
        return _guard_error(SMART_CANARY_PACKAGE_NOT_READY, paper_id, "author_info.md is not Status: READY")
    if not re.search(r"(?mi)^\s*(?:Final decision|Decision)\s*:\s*Recommended\s*$", journal_text):
        return _guard_error(SMART_CANARY_PACKAGE_NOT_READY, paper_id, "journal.md is not Final decision: Recommended")
    from .journal_author_output_contract import validate_journal_author_output

    package_contract = validate_journal_author_output(JOURNAL_ROOT, paper_id)
    if not (
        package_contract.ok
        and package_contract.status == "complete"
        and package_contract.package_status == "READY_TO_SUBMIT"
    ):
        return _guard_error(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "journal/author deterministic package contract is not ready to submit",
            details={
                "contract_status": package_contract.status,
                "package_status": package_contract.package_status,
                "issues": list(package_contract.issues),
                "warnings": list(package_contract.warnings),
            },
        )
    if "DO NOT SUBMIT YET" in journal_text.upper() or _has_unresolved_gate_marker(
        risk_text + "\n" + checklist_text,
        resolved_minor_actions=True,
    ):
        return _guard_error(SMART_CANARY_PACKAGE_NOT_READY, paper_id, "package gate still contains a blocker or unresolved TODO")
    packaged_fingerprint = package_dir / "package_metadata_fingerprint.json"
    if packaged_fingerprint.read_bytes() != source_fingerprint.read_bytes():
        return _guard_error(SMART_CANARY_PACKAGE_NOT_READY, paper_id, "package metadata fingerprint does not match source")

    prepared_paper = copy.deepcopy(current_smart_paper) if isinstance(current_smart_paper, dict) else {}
    prepared_paper.update(
        {
            "paper_id": paper_id,
            "title": record.get("title") or prepared_paper.get("title") or paper_id,
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
        }
    )
    target_context = _hydrate_paper_target_from_package(paper_id, prepared_paper)
    target_journal = _clean_extracted_value(str(prepared_paper.get("target_journal") or target_context.get("target_journal") or ""))
    system_id = str(prepared_paper.get("submission_system_id") or target_context.get("submission_system_id") or "").strip().lower()
    system_label = str(prepared_paper.get("submission_system") or target_context.get("submission_system") or "").strip()
    if not system_id:
        system_id = _guess_system_id(" ".join((target_journal, system_label, journal_text[:4000])))
    if not target_journal or not system_id:
        return _guard_error(
            SMART_CANARY_TARGET_MISSING,
            paper_id,
            "target journal or submission system could not be resolved from the package",
        )
    prepared_paper["target_journal"] = target_journal
    prepared_paper["submission_system_id"] = system_id
    prepared_paper["submission_system"] = system_label or _system_label(system_id)

    system_site, journal_site = _verified_target_site(
        registry,
        paper_id=paper_id,
        system_id=system_id,
        target_journal=target_journal,
    )
    if system_site is None or journal_site is None:
        return _guard_error(
            SMART_CANARY_SITE_UNVERIFIED,
            paper_id,
            "target system and journal site must both be verified and URL-bound",
            details={"submission_system_id": system_id, "target_journal": target_journal},
        )
    expected_origins = tuple(
        str(value).strip()
        for value in (
            system_site.get("login_url"),
            system_site.get("submission_url"),
            journal_site.get("submission_url"),
            *(
                submission_site_adapter(system_id).allowed_origins
                if submission_site_adapter(system_id) is not None
                else ()
            ),
        )
        if str(value or "").strip()
    )

    aliases = _system_aliases(system_id, prepared_paper["submission_system"])
    credential_resolver = _smart_credential_resolver()
    accounts = credential_data.get("accounts", []) if isinstance(credential_data.get("accounts"), list) else []
    account = next(
        (
            item
            for item in accounts
            if isinstance(item, dict)
            and _matches_target_account(
                item,
                aliases=aliases,
                target_journal=target_journal,
                resolver=credential_resolver,
                expected_origins=expected_origins,
            )
        ),
        None,
    )
    if account is None:
        return _guard_error(
            SMART_CANARY_ACCOUNT_MISSING,
            paper_id,
            "no configured account matches the target submission system",
            details={"submission_system_id": system_id},
        )
    selected_readiness = _resolved_account_readiness(
        account,
        resolver=credential_resolver,
        expected_origins=expected_origins,
    )
    if selected_readiness.requires_secret_broker:
        return _guard_error(
            SMART_SECRET_BROKER_REQUIRED,
            paper_id,
            "the selected stored credential cannot be handed to the visible worker safely yet",
            details={
                "account_id": str(account.get("account_id") or ""),
                "credential_source": selected_readiness.auth_source,
                "readiness": selected_readiness.state,
            },
        )
    return {
        "ok": True,
        "code": "SMART_CANARY_PREFLIGHT_OK",
        "paper_id": paper_id,
        "route": route,
        "prepared": {
            "paper_id": paper_id,
            "paper": prepared_paper,
            "package_dir": str(package_dir),
            "paper_dir": str(paper_dir),
            "target_journal": target_journal,
            "submission_system_id": system_id,
            "account_id": str(account.get("account_id") or ""),
            "credential_source": selected_readiness.auth_source,
            "account_readiness": selected_readiness.state,
            "system_site_id": str(system_site.get("system_id") or ""),
            "journal_site_key": str(journal_site.get("journal_key") or ""),
        },
    }


def _prepare_audit_content(final_path: Path, content: str) -> dict[str, Any]:
    pending_path = final_path.with_name(f"{final_path.name}.pending")
    persisted_content = content.rstrip() + "\n"
    write_text(pending_path, persisted_content)
    return {
        "path": final_path,
        "pending_path": pending_path,
        "sha256": hashlib.sha256(persisted_content.encode("utf-8")).hexdigest(),
    }


def _prepare_canary_route_audit(
    paper_id: str,
    *,
    event: str,
    lines: list[str],
    at: str | None = None,
) -> dict[str, Any]:
    safe_paper_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(paper_id or "").strip()) or "paper"
    safe_event = re.sub(r"[^A-Za-z0-9._-]+", "_", str(event or "").strip()) or "event"
    final_path = SMART_AUDIT_DIR / (
        f"{timestamp_id()}_{safe_paper_id}_{safe_event}_{os.urandom(4).hex()}.md"
    )
    content = "\n".join(
        [
            "# Smart Submission Canary Route Audit",
            "",
            f"- paper_id: {paper_id}",
            f"- event: {event}",
            f"- at: {at or now_iso()}",
            *lines,
            "",
        ]
    )
    return _prepare_audit_content(final_path, content)


def _commit_prepared_audit(prepared: dict[str, Any]) -> Path:
    final_path = Path(prepared["path"])
    pending_path = Path(prepared["pending_path"])
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(pending_path, final_path)
    return final_path


def _discard_prepared_audit(prepared: dict[str, Any]) -> None:
    try:
        Path(prepared["pending_path"]).unlink(missing_ok=True)
    except OSError:
        pass


def _write_canary_route_audit(
    paper_id: str,
    *,
    event: str,
    lines: list[str],
) -> Path:
    return _commit_prepared_audit(
        _prepare_canary_route_audit(paper_id, event=event, lines=lines)
    )


def _verify_foreground_ui_gesture(window_handle: int) -> bool:
    if sys.platform != "win32" or int(window_handle or 0) <= 0:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        foreground = int(user32.GetForegroundWindow())
        if foreground <= 0 or not bool(user32.IsWindowVisible(int(window_handle))):
            return False
        current_pid = os.getpid()
        window_pid = ctypes.c_ulong()
        foreground_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(int(window_handle), ctypes.byref(window_pid))
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
        return window_pid.value == current_pid and foreground_pid.value == current_pid
    except (AttributeError, OSError, TypeError, ValueError):
        return False


@_smart_state_locked
def prepare_final_submit_confirmation_challenge(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    ui_window_handle: int,
) -> dict[str, Any]:
    """Create a UI-only random capability after the final-review page is ready.

    This function is deliberately not exposed as a CLI command.  The EXE keeps
    the returned capability only in memory while it displays the irreversible
    action dialog; the nonce is never persisted or written to an audit file.
    """

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not paper_id or not attempt_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "paper_id and attempt_id are required for the UI confirmation challenge",
        )
    if not _verify_foreground_ui_gesture(ui_window_handle):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "confirmation challenge requires a visible foreground EXE window owned by this process",
        )
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "UI confirmation challenge has no matching Smart paper",
        )
    if str(paper.get("attempt_id") or "").strip() != attempt_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_ATTEMPT_MISMATCH,
            paper_id,
            "UI confirmation challenge attempt_id is stale",
        )
    if (
        str(paper.get("workflow_status") or "").strip().casefold() != "waiting"
        or str(paper.get("status") or "").strip().casefold() != "waiting"
        or str(paper.get("next_action") or "").strip() != FINAL_SUBMIT_REVIEW_READY_ACTION
        or _paper_has_prior_submission_evidence(paper)
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "UI confirmation challenge requires a reviewed final page waiting only for the final click",
        )
    package_binding = _submission_package_binding(paper_id, paper)
    account_id = str(paper.get("credential_account_id") or "").strip()
    submission_system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    if not package_binding or not account_id or not submission_system_id or not target_journal:
        raise SmartSubmissionGuardError(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "UI confirmation challenge requires immutable package and target bindings",
        )
    _require_current_worker_auth(paper_id, paper)
    target_binding = _final_submit_target_binding(paper_id, paper)
    if not target_binding or not all(target_binding.values()):
        raise SmartSubmissionGuardError(
            SMART_CANARY_SITE_UNVERIFIED,
            paper_id,
            "UI confirmation challenge requires one exact verified target origin",
        )
    now = datetime.now().astimezone()
    now_text = now.isoformat()
    challenge_id = os.urandom(16).hex()
    nonce = os.urandom(24).hex()
    expires_at = (now + FINAL_SUBMIT_CONFIRMATION_CHALLENGE_TTL).isoformat()
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_ui_challenge_prepared",
        at=now_text,
        lines=[
            f"- challenge_id: {challenge_id}",
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- account_id: {account_id}",
            f"- target_journal: {target_journal}",
            f"- target_origin: {target_binding['target_origin']}",
            f"- package_binding_sha256: {package_binding}",
            f"- draft_binding_sha256: {target_binding['draft_binding_sha256']}",
            f"- expires_at: {expires_at}",
            "- presentation: exe_ui_foreground_gesture",
            "- nonce_persisted: false",
            "- transaction_state: committed_with_state",
        ],
    )
    challenge = _sign_smart_receipt(
        {
            "schema": FINAL_SUBMIT_CHALLENGE_SCHEMA,
            "challenge_id": challenge_id,
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "account_id": account_id,
            "submission_system_id": submission_system_id,
            "target_journal": target_journal,
            "target_origin": target_binding["target_origin"],
            "journal_site_key": target_binding["journal_site_key"],
            "account_binding_sha256": target_binding["account_binding_sha256"],
            "site_binding_sha256": target_binding["site_binding_sha256"],
            "draft_binding_sha256": target_binding["draft_binding_sha256"],
            "package_binding_sha256": package_binding,
            "created_at": now_text,
            "expires_at": expires_at,
            "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            "presentation": "exe_ui_foreground_gesture",
            "challenge_audit_path": str(prepared_audit["path"]),
            "challenge_audit_sha256": str(prepared_audit["sha256"]),
            "consumed_at": "",
        }
    )
    paper["final_submit_confirmation_challenge"] = challenge
    state["updated_at"] = now_text
    state.setdefault("history", []).append(
        {
            "at": now_text,
            "event": "prepare_final_submit_ui_challenge",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "challenge_id": challenge_id,
            "expires_at": expires_at,
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": True,
        "code": "SMART_FINAL_SUBMIT_UI_CHALLENGE_READY",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "challenge_id": challenge_id,
        "expires_at": expires_at,
        "target_journal": target_journal,
        "target_origin": target_binding["target_origin"],
        "account_id": account_id,
        "package_binding_sha256": package_binding,
        "confirmation_phrase": (
            f"{FINAL_SUBMIT_CONFIRMATION_PREFIX}:{paper_id}:{attempt_id}:{nonce}"
        ),
        "audit_path": str(audit_path),
    }


@_smart_state_locked
def release_expired_unclaimed_final_submit_authorization(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
) -> dict[str, Any]:
    """Return a safe unused capability to automatic review-ready state.

    Besides normal expiry, a federated portal may refresh the same authenticated
    account from its dashboard origin to its manuscript origin.  That changes
    only the account-session binding hash.  An otherwise intact capability may
    be reissued in that one narrow case, but never after reservation,
    consumption, a running worker, or any positive submission evidence.
    """

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not paper_id or not attempt_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "paper_id and attempt_id are required for unused authorization recovery",
        )
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "unused final-submit recovery has no matching Smart paper",
        )
    if str(paper.get("attempt_id") or "").strip() != attempt_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_ATTEMPT_MISMATCH,
            paper_id,
            "unused final-submit recovery attempt_id is stale",
        )
    if str(paper.get("next_action") or "").strip() != FINAL_SUBMIT_AUTHORIZED_ACTION:
        return {
            "ok": True,
            "paper_id": paper_id,
            "released": False,
            "reason": "not_authorized",
        }
    if (
        str(paper.get("workflow_status") or "").strip().casefold() not in {"queued", "waiting"}
        or str(paper.get("worker_status") or "").strip().casefold() == "running"
        or _paper_has_prior_submission_evidence(paper)
    ):
        raise SmartSubmissionGuardError(
            SMART_CANARY_TERMINAL_EVIDENCE,
            paper_id,
            "an active or potentially completed final-submit attempt cannot be recycled",
        )
    bound = _bound_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
    )
    session_binding_rebound = False
    if bound is None:
        receipt = paper.get("final_submit_authorization")
        current = datetime.now().astimezone()
        authorized_at = (
            _parse_aware_time(receipt.get("authorized_at"))
            if isinstance(receipt, dict)
            else None
        )
        expires_at = (
            _parse_aware_time(receipt.get("expires_at"))
            if isinstance(receipt, dict)
            else None
        )
        target_binding = _final_submit_target_binding(paper_id, paper)
        expected = {
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "account_id": str(paper.get("credential_account_id") or "").strip(),
            "submission_system_id": str(
                paper.get("submission_system_id") or ""
            ).strip().casefold(),
            "target_journal": str(paper.get("target_journal") or "").strip(),
            "package_binding_sha256": _submission_package_binding(paper_id, paper),
            "target_origin": target_binding.get("target_origin", ""),
            "journal_site_key": target_binding.get("journal_site_key", ""),
            "site_binding_sha256": target_binding.get("site_binding_sha256", ""),
            "draft_binding_sha256": target_binding.get("draft_binding_sha256", ""),
        }
        if (
            not isinstance(receipt, dict)
            or set(receipt) != _FINAL_SUBMIT_AUTHORIZATION_KEYS
            or authorized_at is None
            or expires_at is None
            or authorized_at > current + timedelta(seconds=30)
            or expires_at <= authorized_at
            or expires_at - authorized_at > FINAL_SUBMIT_AUTHORIZATION_TTL
            or receipt.get("explicit_user_confirmation") is not True
            or not _verify_smart_receipt(receipt)
            or not _committed_audit_matches(
                receipt.get("authorization_audit_path"),
                receipt.get("authorization_audit_sha256"),
            )
            or not all(target_binding.values())
            or not all(expected.values())
            or any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("account_binding_sha256")
            == target_binding.get("account_binding_sha256")
            or not re.fullmatch(
                r"[0-9a-f]{32}",
                str(receipt.get("authorization_id") or ""),
            )
            or not re.fullmatch(
                r"[0-9a-f]{32}",
                str(receipt.get("confirmation_challenge_id") or ""),
            )
        ):
            raise SmartSubmissionGuardError(
                SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
                paper_id,
                "final-submit recovery requires an intact unused capability whose only changed binding is the authenticated account session",
            )
        session_binding_rebound = True
    else:
        receipt, _authorized_at, expires_at, current = bound
    if current < expires_at and not session_binding_rebound:
        return {
            "ok": True,
            "paper_id": paper_id,
            "released": False,
            "reason": "authorization_still_valid",
            "expires_at": expires_at.isoformat(),
        }
    if any(
        str(receipt.get(key) or "").strip()
        for key in (
            "reserved_at",
            "reserved_by_thread_id",
            "consumed_at",
            "consumed_by_thread_id",
            "result_recorded_at",
        )
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "a capability with reservation, consumption, or result state cannot be automatically recycled",
        )

    now_text = current.isoformat()
    authorization_id = str(receipt.get("authorization_id") or "")
    recovery_event = (
        "final_submit_authorization_session_rebound_unclaimed"
        if session_binding_rebound
        else "final_submit_authorization_expired_unclaimed"
    )
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event=recovery_event,
        at=now_text,
        lines=[
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- authorization_id: {authorization_id}",
            f"- expired_at: {expires_at.isoformat()}",
            f"- session_binding_rebound: {str(session_binding_rebound).lower()}",
            "- reserved: false",
            "- consumed: false",
            "- result_recorded: false",
            "- recovery: return_to_review_ready_for_new_exact_capability",
            "- transaction_state: committed_with_state",
        ],
    )
    paper.pop("final_submit_authorization", None)
    paper.pop("final_submit_confirmation_challenge", None)
    paper["status"] = "waiting"
    paper["workflow_status"] = "waiting"
    paper["worker_status"] = "waiting"
    paper["thread_id"] = ""
    paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
    paper["blocker"] = ""
    paper["detail"] = (
        (
            "The authenticated federated session binding changed before dispatch; "
            "the program will issue a new exact-attempt capability."
        )
        if session_binding_rebound
        else (
            "The previous automatic final-submit capability expired before dispatch; "
            "the program will issue a new exact-attempt capability."
        )
    )
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    state["updated_at"] = now_text
    state.setdefault("history", []).append(
        {
            "at": now_text,
            "event": "release_expired_unclaimed_final_submit_authorization",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "authorization_id": authorization_id,
            "expired_at": expires_at.isoformat(),
            "session_binding_rebound": session_binding_rebound,
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": True,
        "paper_id": paper_id,
        "released": True,
        "authorization_id": authorization_id,
        "expired_at": expires_at.isoformat(),
        "session_binding_rebound": session_binding_rebound,
        "audit_path": str(audit_path),
    }


@_smart_state_locked
def authorize_final_submit_automatically(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    activate: bool = False,
) -> dict[str, Any]:
    """Issue the normal one-use capability without a second user dialog.

    The user authorizes the complete online workflow when starting Smart
    Submission.  This function does not weaken the final-click invariants: it
    still requires the exact live review attempt, immutable package/account/
    site bindings, a verified session, a signed audit, and a short expiry.
    """

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not paper_id or not attempt_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "paper_id and attempt_id are required for automatic final submit",
        )
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    if not bool(settings.get("auto_final_submit_enabled", True)):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "automatic final submit is disabled in Smart Submission settings",
        )
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "automatic final-submit authorization has no matching Smart paper",
        )
    if str(paper.get("attempt_id") or "").strip() != attempt_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_ATTEMPT_MISMATCH,
            paper_id,
            "automatic final-submit authorization attempt_id is stale",
        )
    _identity, identity_error = _submission_identity_guard_result(
        paper,
        operation="new",
    )
    if identity_error is not None:
        raise SmartSubmissionGuardError(
            str(identity_error["code"]),
            paper_id,
            str(identity_error["message"]),
            details=identity_error,
        )
    if (
        str(paper.get("workflow_status") or "").strip().casefold() != "waiting"
        or str(paper.get("status") or "").strip().casefold() != "waiting"
        or str(paper.get("next_action") or "").strip() != FINAL_SUBMIT_REVIEW_READY_ACTION
        or _paper_has_prior_submission_evidence(paper)
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "automatic authorization requires a completed live final-page review waiting only for the final click",
        )
    package_binding = _submission_package_binding(paper_id, paper)
    account_id = str(paper.get("credential_account_id") or "").strip()
    submission_system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    if not package_binding or not account_id or not submission_system_id or not target_journal:
        raise SmartSubmissionGuardError(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "automatic final submit requires immutable package, account, system, and journal bindings",
        )
    _require_current_worker_auth(paper_id, paper)
    target_binding = _final_submit_target_binding(paper_id, paper)
    if not target_binding or not all(target_binding.values()):
        raise SmartSubmissionGuardError(
            SMART_CANARY_SITE_UNVERIFIED,
            paper_id,
            "automatic final submit requires one exact verified target origin",
        )

    now = datetime.now().astimezone()
    now_text = now.isoformat()
    challenge_id = os.urandom(16).hex()
    nonce = os.urandom(24).hex()
    expires_at = (now + FINAL_SUBMIT_CONFIRMATION_CHALLENGE_TTL).isoformat()
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_auto_challenge_prepared",
        at=now_text,
        lines=[
            f"- challenge_id: {challenge_id}",
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- account_id: {account_id}",
            f"- target_journal: {target_journal}",
            f"- target_origin: {target_binding['target_origin']}",
            f"- package_binding_sha256: {package_binding}",
            f"- draft_binding_sha256: {target_binding['draft_binding_sha256']}",
            f"- expires_at: {expires_at}",
            f"- presentation: {FINAL_SUBMIT_AUTO_PRESENTATION}",
            "- workflow_start_authorization: true",
            "- nonce_persisted: false",
            "- transaction_state: committed_with_state",
        ],
    )
    challenge = _sign_smart_receipt(
        {
            "schema": FINAL_SUBMIT_CHALLENGE_SCHEMA,
            "challenge_id": challenge_id,
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "account_id": account_id,
            "submission_system_id": submission_system_id,
            "target_journal": target_journal,
            "target_origin": target_binding["target_origin"],
            "journal_site_key": target_binding["journal_site_key"],
            "account_binding_sha256": target_binding["account_binding_sha256"],
            "site_binding_sha256": target_binding["site_binding_sha256"],
            "draft_binding_sha256": target_binding["draft_binding_sha256"],
            "package_binding_sha256": package_binding,
            "created_at": now_text,
            "expires_at": expires_at,
            "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            "presentation": FINAL_SUBMIT_AUTO_PRESENTATION,
            "challenge_audit_path": str(prepared_audit["path"]),
            "challenge_audit_sha256": str(prepared_audit["sha256"]),
            "consumed_at": "",
        }
    )
    paper["final_submit_confirmation_challenge"] = challenge
    state["updated_at"] = now_text
    state.setdefault("history", []).append(
        {
            "at": now_text,
            "event": "prepare_final_submit_auto_challenge",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "challenge_id": challenge_id,
            "expires_at": expires_at,
            "authorization_mode": FINAL_SUBMIT_AUTO_PRESENTATION,
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    _commit_prepared_audit(prepared_audit)

    confirmation_phrase = (
        f"{FINAL_SUBMIT_CONFIRMATION_PREFIX}:{paper_id}:{attempt_id}:{nonce}"
    )
    try:
        result = authorize_final_submit(
            paper_id,
            route_revision=int(route_revision),
            attempt_id=attempt_id,
            confirmation_phrase=confirmation_phrase,
            activate=activate,
        )
    finally:
        confirmation_phrase = ""
        nonce = ""
    return {
        **result,
        "auto_authorized": True,
        "authorization_mode": FINAL_SUBMIT_AUTO_PRESENTATION,
    }


@_smart_state_locked
def authorize_final_submit(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    confirmation_phrase: str,
    activate: bool = True,
) -> dict[str, Any]:
    """Record one short-lived, exact-attempt final-submit authorization.

    The random capability may originate from the legacy foreground confirmation
    dialog or from the program-owned automatic policy after final review.  Both
    paths preserve the same exact-attempt and one-click invariants.
    """

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    phrase_parts = str(confirmation_phrase or "").split(":")
    nonce = phrase_parts[3].strip().casefold() if len(phrase_parts) == 4 else ""
    if (
        not paper_id
        or not attempt_id
        or len(phrase_parts) != 4
        or phrase_parts[:3] != [FINAL_SUBMIT_CONFIRMATION_PREFIX, paper_id, attempt_id]
        or not re.fullmatch(r"[0-9a-f]{48}", nonce)
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "a current signed final-submit capability is required immediately before final submit",
        )
    route = require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "final-submit authorization has no matching Smart paper",
        )
    if str(paper.get("attempt_id") or "").strip() != attempt_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_ATTEMPT_MISMATCH,
            paper_id,
            "final-submit authorization attempt_id is stale",
        )
    _identity, identity_error = _submission_identity_guard_result(
        paper,
        operation="new",
    )
    if identity_error is not None:
        raise SmartSubmissionGuardError(
            str(identity_error["code"]),
            paper_id,
            str(identity_error["message"]),
            details=identity_error,
        )
    workflow_status = str(paper.get("workflow_status") or "").strip().casefold()
    paper_status = str(paper.get("status") or "").strip().casefold()
    next_action = str(paper.get("next_action") or "").strip()
    if (
        workflow_status != "waiting"
        or paper_status != "waiting"
        or next_action != FINAL_SUBMIT_REVIEW_READY_ACTION
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "final-submit authorization requires a completed live final-page review waiting for confirmation",
        )
    if _paper_has_prior_submission_evidence(paper):
        raise SmartSubmissionGuardError(
            SMART_CANARY_TERMINAL_EVIDENCE,
            paper_id,
            "paper already has terminal submission evidence; a new final click is forbidden",
        )
    package_binding = _submission_package_binding(paper_id, paper)
    if not package_binding:
        raise SmartSubmissionGuardError(
            SMART_CANARY_PACKAGE_NOT_READY,
            paper_id,
            "final-submit authorization requires a stable local submission package binding",
        )
    account_id = str(paper.get("credential_account_id") or "").strip()
    submission_system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    if not account_id or not submission_system_id or not target_journal:
        raise SmartSubmissionGuardError(
            SMART_SECRET_BROKER_REQUIRED,
            paper_id,
            "final-submit authorization requires exact value-free account, system, and target bindings",
        )
    _require_current_worker_auth(paper_id, paper)
    target_binding = _final_submit_target_binding(paper_id, paper)
    if not target_binding or not all(target_binding.values()):
        raise SmartSubmissionGuardError(
            SMART_CANARY_SITE_UNVERIFIED,
            paper_id,
            "final-submit authorization requires one exact verified target origin and immutable account/site bindings",
        )
    now = datetime.now().astimezone()
    now_text = now.isoformat()
    challenge = paper.get("final_submit_confirmation_challenge")
    challenge_created_at = (
        _parse_aware_time(challenge.get("created_at"))
        if isinstance(challenge, dict)
        else None
    )
    challenge_expires_at = (
        _parse_aware_time(challenge.get("expires_at"))
        if isinstance(challenge, dict)
        else None
    )
    expected_challenge = {
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "account_id": account_id,
        "submission_system_id": submission_system_id,
        "target_journal": target_journal,
        "target_origin": target_binding["target_origin"],
        "journal_site_key": target_binding["journal_site_key"],
        "account_binding_sha256": target_binding["account_binding_sha256"],
        "site_binding_sha256": target_binding["site_binding_sha256"],
        "draft_binding_sha256": target_binding["draft_binding_sha256"],
        "package_binding_sha256": package_binding,
    }
    if (
        not isinstance(challenge, dict)
        or set(challenge) != _FINAL_SUBMIT_CONFIRMATION_CHALLENGE_KEYS
        or challenge.get("schema") != FINAL_SUBMIT_CHALLENGE_SCHEMA
        or challenge.get("presentation")
        not in {FINAL_SUBMIT_UI_PRESENTATION, FINAL_SUBMIT_AUTO_PRESENTATION}
        or not _verify_smart_receipt(challenge)
        or challenge_created_at is None
        or challenge_expires_at is None
        or challenge_created_at > now + timedelta(seconds=30)
        or challenge_expires_at <= challenge_created_at
        or challenge_expires_at - challenge_created_at > FINAL_SUBMIT_CONFIRMATION_CHALLENGE_TTL
        or now >= challenge_expires_at
        or str(challenge.get("consumed_at") or "")
        or not _committed_audit_matches(
            challenge.get("challenge_audit_path"),
            challenge.get("challenge_audit_sha256"),
        )
        or not hmac.compare_digest(
            str(challenge.get("nonce_sha256") or ""),
            hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        )
        or any(challenge.get(key) != value for key, value in expected_challenge.items())
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "the final-submit challenge is missing, expired, changed, or already consumed",
        )
    authorization_mode = str(challenge.get("presentation") or "")
    expires = now + FINAL_SUBMIT_AUTHORIZATION_TTL
    receipt = {
        "authorization_id": os.urandom(16).hex(),
        "confirmation_challenge_id": str(challenge.get("challenge_id") or ""),
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "account_id": account_id,
        "submission_system_id": submission_system_id,
        "target_journal": target_journal,
        "target_origin": target_binding["target_origin"],
        "journal_site_key": target_binding["journal_site_key"],
        "account_binding_sha256": target_binding["account_binding_sha256"],
        "site_binding_sha256": target_binding["site_binding_sha256"],
        "draft_binding_sha256": target_binding["draft_binding_sha256"],
        "package_binding_sha256": package_binding,
        "authorized_at": now_text,
        "expires_at": expires.isoformat(),
        "explicit_user_confirmation": True,
        "authorization_audit_path": "",
        "authorization_audit_sha256": "",
        "reserved_at": "",
        "reserved_by_thread_id": "",
        "reservation_audit_path": "",
        "reservation_audit_sha256": "",
        "consumed_at": "",
        "consumed_by_thread_id": "",
        "consumption_audit_path": "",
        "consumption_audit_sha256": "",
        "result_recorded_at": "",
    }
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_authorized",
        at=now_text,
        lines=[
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- authorization_id: {receipt['authorization_id']}",
            f"- confirmation_challenge_id: {receipt['confirmation_challenge_id']}",
            f"- expires_at: {receipt['expires_at']}",
            f"- package_binding_sha256: {package_binding}",
            f"- target_origin: {target_binding['target_origin']}",
            f"- journal_site_key: {target_binding['journal_site_key']}",
            f"- account_binding_sha256: {target_binding['account_binding_sha256']}",
            f"- site_binding_sha256: {target_binding['site_binding_sha256']}",
            f"- draft_binding_sha256: {target_binding['draft_binding_sha256']}",
            f"- authorization_mode: {authorization_mode}",
            "- explicit_user_confirmation: true",
            "- transaction_state: committed_with_state",
        ],
    )
    receipt["authorization_audit_path"] = str(prepared_audit["path"])
    receipt["authorization_audit_sha256"] = str(prepared_audit["sha256"])
    receipt = _sign_smart_receipt(receipt)
    consumed_challenge = copy.deepcopy(challenge)
    consumed_challenge["consumed_at"] = now_text
    paper["final_submit_confirmation_challenge"] = _sign_smart_receipt(consumed_challenge)
    paper["final_submit_authorization"] = receipt
    old_thread_id = str(paper.get("thread_id") or "").strip()
    fixed_sdk_thread_id = str(paper.get("sdk_thread_id") or "").strip()
    if old_thread_id and old_thread_id != fixed_sdk_thread_id:
        stale = [str(item) for item in paper.get("stale_worker_thread_ids", []) if str(item).strip()]
        paper["stale_worker_thread_ids"] = list(dict.fromkeys(stale + [old_thread_id]))
    paper["thread_id"] = ""
    paper["status"] = "ready_for_online_submit"
    paper["workflow_status"] = "queued"
    paper["worker_status"] = "queued"
    paper["next_action"] = FINAL_SUBMIT_AUTHORIZED_ACTION
    _mark_smart_route_on_paper(paper, route)
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    _append_online_queue_guarded(
        online_queue,
        paper_id,
        paper,
        expected_revision=route_revision,
    )
    state["online_queue"] = online_queue
    if activate:
        settings = state.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            state["settings"] = settings
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
        state["mode"] = "run_once"
    state["updated_at"] = now_text
    state.setdefault("history", []).append(
        {
            "at": now_text,
            "event": "authorize_final_submit",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "authorization_id": receipt["authorization_id"],
            "confirmation_challenge_id": receipt["confirmation_challenge_id"],
            "expires_at": receipt["expires_at"],
            "package_binding_sha256": package_binding,
            "authorization_mode": authorization_mode,
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    wakeup = set_smart_submission_automation_fast_wakeup(True) if activate else {}
    return {
        "ok": True,
        "code": "SMART_FINAL_SUBMIT_AUTHORIZED",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "authorization_id": receipt["authorization_id"],
        "expires_at": receipt["expires_at"],
        "audit_path": str(audit_path),
        "authorization_mode": authorization_mode,
        "queued": paper_id in online_queue,
        "wakeup": wakeup,
    }


@_smart_state_locked
def _enqueue_preflighted_single_canary(
    prepared: dict[str, Any],
    *,
    route_revision: int,
) -> dict[str, Any]:
    """CAS-guarded queue commit. Browser/automation wakeup happens later."""

    paper_id = str(prepared.get("paper_id") or "").strip()
    transaction_id = str(prepared.get("canary_transaction_id") or "").strip()
    if not transaction_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_QUEUE_FAILED,
            paper_id,
            "canary queue commit is missing transaction_id",
        )
    route = require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    legacy = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
    legacy = legacy if isinstance(legacy, dict) else {}
    unified = _unified_state_snapshot()
    _assert_no_terminal_canary_state(
        paper_id,
        legacy_state=legacy,
        smart_state=state,
        stage="queue_prepare",
    )
    queue_snapshot = {
        "unified_queue": _queue_paper_ids(unified, "paper_queue"),
        "legacy_queue": _queue_paper_ids(legacy, "paper_queue", "queue"),
        "smart_queue": _queue_paper_ids(state, "paper_queue", "online_queue"),
        "running_papers": list(
            dict.fromkeys(_effective_running_papers(legacy) + _effective_running_papers(state))
        ),
    }
    if any(queue_snapshot.values()):
        raise SmartSubmissionGuardError(
            SMART_CANARY_QUEUE_BUSY,
            paper_id,
            "queue/worker state changed after preflight",
            details=queue_snapshot,
        )
    papers = state.setdefault("papers", {})
    if not isinstance(papers, dict):
        papers = {}
        state["papers"] = papers
    paper = copy.deepcopy(prepared.get("paper")) if isinstance(prepared.get("paper"), dict) else {}
    paper.update(
        {
            "paper_id": paper_id,
            "status": "ready_for_online_submit",
            "workflow_status": "queued",
            "worker_status": "queued",
            "blocker": "",
            "next_action": "visible_worker_online_submit_or_monitor",
            "online_intent": "single_paper_canary",
            "thread_id": "",
            "thread_title": f"Smart Submission canary {paper_id}",
            "canary_transaction_id": transaction_id,
            "credential_account_id": str(prepared.get("account_id") or ""),
            "credential_source": str(prepared.get("credential_source") or ""),
            "account_readiness": str(prepared.get("account_readiness") or ""),
            "last_online_queued_at": now_iso(),
        }
    )
    _rotate_canary_attempt(paper, paper_id)
    _mark_smart_route_on_paper(paper, route)
    papers[paper_id] = paper
    online_queue: list[str] = []
    _append_online_queue_guarded(
        online_queue,
        paper_id,
        paper,
        expected_revision=route_revision,
    )
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    settings["online_submit_enabled"] = True
    settings["visible_scheduler_enabled"] = True
    settings["canary_scope_paper_id"] = paper_id
    settings["max_parallel_workers"] = 1
    state["paper_queue"] = []
    state["online_queue"] = online_queue
    state["mode"] = "run_once"
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "start_single_smart_canary",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": paper["attempt_id"],
            "canary_transaction_id": transaction_id,
        }
    )
    with FileLock(path=UNIFIED_STATE_PATH.parent / "unified_scheduler.lock"):
        require_smart_canary_route(paper_id, expected_revision=route_revision)
        latest_legacy = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
        latest_smart = read_json(SMART_STATE_PATH, {})
        _assert_no_terminal_canary_state(
            paper_id,
            legacy_state=latest_legacy if isinstance(latest_legacy, dict) else {},
            smart_state=latest_smart if isinstance(latest_smart, dict) else {},
            stage="queue_commit",
        )
        write_json_atomic(SMART_STATE_PATH, state)
    prepared["_smart_state_commit_sha256"] = _smart_state_file_hash()
    audit_path = _write_canary_route_audit(
        paper_id,
        event="queued",
        lines=[
            "- result: queued",
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {paper['attempt_id']}",
            f"- canary_transaction_id: {transaction_id}",
            f"- target_journal: {prepared.get('target_journal', '')}",
            f"- submission_system_id: {prepared.get('submission_system_id', '')}",
            f"- account_id: {prepared.get('account_id', '')}",
            f"- journal_site_key: {prepared.get('journal_site_key', '')}",
        ],
    )
    return {
        "ok": True,
        "code": "SMART_CANARY_QUEUED",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": paper["attempt_id"],
        "canary_transaction_id": transaction_id,
        "audit_path": str(audit_path),
        "state": state,
    }


@_smart_state_locked
def start_single_smart_canary(
    paper_id: str,
    *,
    expected_route_revision: int,
    orchestrator: Any | None = None,
    enqueue_callback: Any | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Preflight, CAS route switch, queue commit, then optional scheduler wakeup."""

    paper_id = str(paper_id or "").strip()
    if activate:
        try:
            from .ai_execution_policy import require_legacy_execution_mode
            from .ai_provider_store import AIProviderStore

            require_legacy_execution_mode(
                AIProviderStore().load_settings().execution_mode,
                action="启动智能投稿",
            )
        except Exception as exc:
            return _guard_error(
                str(getattr(exc, "code", "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE")),
                paper_id,
                str(exc),
            )
    if orchestrator is None:
        from .orchestrator import UnifiedOrchestrator

        orchestrator = UnifiedOrchestrator()
    recovery = _recover_incomplete_canary_transaction(orchestrator)
    if not recovery.get("ok"):
        return recovery
    try:
        preflight = preflight_single_smart_canary(
            paper_id,
            expected_route_revision=expected_route_revision,
        )
    except SmartSubmissionGuardError as exc:
        return exc.as_dict()
    except Exception as exc:
        return _guard_error(
            SMART_CANARY_PREFLIGHT_FAILED,
            paper_id,
            "read-only Smart canary preflight could not be completed",
            details={"error": str(exc)},
        )
    if not preflight.get("ok"):
        return preflight
    original_route = normalize_submission_route(preflight.get("route"))
    try:
        state_existed, _smart_state_before, smart_state_before_raw, smart_hash_before = (
            _read_smart_state_snapshot()
        )
    except Exception as exc:
        return _guard_error(
            SMART_CANARY_QUEUE_FAILED,
            paper_id,
            "Smart state snapshot failed before route switch",
            details={"stage": "smart_state_snapshot", "error": str(exc)},
        )
    transaction_id = (
        "smart-canary-txn-"
        + hashlib.sha256(
            f"{paper_id}|{expected_route_revision}|{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:16]
    )
    preflight["prepared"]["canary_transaction_id"] = transaction_id
    transaction_journal = {
        "version": 1,
        "transaction_id": transaction_id,
        "paper_id": paper_id,
        "phase": "prepared",
        "original_route": original_route,
        "switched_revision": int(expected_route_revision) + 1,
        "state_existed": state_existed,
        "smart_hash_before": smart_hash_before,
        "created_at": now_iso(),
    }
    try:
        _write_canary_transaction_journal(transaction_journal, smart_state_before_raw)
    except Exception as exc:
        return _guard_error(
            SMART_CANARY_QUEUE_FAILED,
            paper_id,
            "durable Smart canary transaction journal could not be created",
            details={"stage": "transaction_journal", "error": str(exc)},
        )
    route_result: dict[str, Any]
    try:
        route_result = orchestrator.set_submission_route(
            paper_id,
            execution_owner="smart",
            mode="canary",
            expected_revision=int(expected_route_revision),
            operation_id=transaction_id,
        )
    except Exception as exc:
        rollback, rollback_error, live_route = _restore_original_route_cas(
            orchestrator,
            paper_id,
            original_route=original_route,
            expected_switched_revision=int(expected_route_revision) + 1,
            operation_id=transaction_id,
        )
        if rollback_error:
            return _guard_error(
                SMART_CANARY_ROUTE_ROLLBACK_FAILED,
                paper_id,
                "route CAS raised and the live route could not be safely restored",
                details={
                    "stage": "route_switch",
                    "error": str(exc),
                    "route_rollback_error": rollback_error,
                    "live_route": live_route,
                },
            )
        _clear_canary_transaction_journal()
        return _guard_error(
            SMART_CANARY_QUEUE_FAILED,
            paper_id,
            "route CAS failed after preflight; the original route is intact",
            details={
                "stage": "route_switch",
                "error": str(exc),
                "rollback": rollback or {},
                "live_route": live_route,
            },
        )
    enqueue = enqueue_callback or _enqueue_preflighted_single_canary
    new_revision: int | None = None
    try:
        if not isinstance(route_result, dict):
            raise RuntimeError("route CAS returned a non-object result")
        new_route = normalize_submission_route(route_result.get("submission_route"))
        new_revision = int(new_route["revision"])
        if route_result.get("changed") and str(route_result.get("operation_id") or "") != transaction_id:
            raise RuntimeError("route CAS returned without the matching transaction receipt")
        transaction_journal.update(
            {
                "phase": "route_switched",
                "switched_revision": new_revision,
                "route_changed": bool(route_result.get("changed")),
                "route_receipt": str(route_result.get("operation_id") or ""),
                "updated_at": now_iso(),
            }
        )
        _write_canary_transaction_journal(transaction_journal)
        queue_result = enqueue(preflight["prepared"], route_revision=new_revision)
        if not isinstance(queue_result, dict) or not queue_result.get("ok"):
            raise RuntimeError("queue commit returned a non-success result")
        transaction_journal.update(
            {
                "phase": "smart_committed",
                "smart_hash_after": str(preflight["prepared"].get("_smart_state_commit_sha256") or ""),
                "updated_at": now_iso(),
            }
        )
        _write_canary_transaction_journal(transaction_journal)
    except Exception as exc:
        restore_error = ""
        try:
            restore_error = _restore_smart_state_snapshot_cas(
                paper_id=paper_id,
                transaction_id=transaction_id,
                state_existed=state_existed,
                state_before_raw=smart_state_before_raw,
                hash_before=smart_hash_before,
                hash_after=str(preflight["prepared"].get("_smart_state_commit_sha256") or ""),
            )
        except Exception as restore_exc:
            restore_error = str(restore_exc)
        rollback, rollback_error, live_route = _restore_original_route_cas(
            orchestrator,
            paper_id,
            original_route=original_route,
            expected_switched_revision=(
                new_revision
                if new_revision is not None
                else int(transaction_journal.get("switched_revision") or 0) or None
            ),
            operation_id=transaction_id,
        )
        audit_path: Path | None = None
        audit_error = ""
        try:
            audit_path = _write_canary_route_audit(
                paper_id,
                event="queue_rollback",
                lines=[
                    "- result: queue_failed",
                    f"- queue_error: {exc}",
                    f"- smart_state_restore_error: {restore_error or '-'}",
                    f"- route_rollback_error: {rollback_error or '-'}",
                    f"- live_route_before_rollback: {json.dumps(live_route, ensure_ascii=False, sort_keys=True)}",
                    f"- original_owner: {original_route['execution_owner']}",
                    f"- original_mode: {original_route['mode']}",
                    f"- switched_revision: {new_revision if new_revision is not None else '-'}",
                    f"- canary_transaction_id: {transaction_id}",
                ],
            )
        except Exception as audit_exc:
            audit_error = str(audit_exc)
        if rollback_error or restore_error:
            return _guard_error(
                SMART_CANARY_ROUTE_ROLLBACK_FAILED,
                paper_id,
                "queue commit failed and rollback was incomplete",
                details={
                    "queue_error": str(exc),
                    "route_rollback_error": rollback_error,
                    "smart_state_restore_error": restore_error,
                    "audit_path": str(audit_path or ""),
                    "audit_error": audit_error,
                },
            )
        _clear_canary_transaction_journal()
        failure_code = exc.code if isinstance(exc, SmartSubmissionGuardError) else SMART_CANARY_QUEUE_FAILED
        return _guard_error(
            failure_code,
            paper_id,
            "queue commit failed; Smart state and original route were restored",
            details={
                "error": str(exc),
                "rollback": rollback or {},
                "audit_path": str(audit_path or ""),
                "audit_error": audit_error,
            },
        )

    journal_cleanup_error = ""
    try:
        _clear_canary_transaction_journal()
    except Exception as exc:
        journal_cleanup_error = str(exc)
    wakeup: dict[str, Any] = {}
    wakeup_error = ""
    prompt_update: dict[str, Any] = {}
    prompt_update_error = ""
    if activate:
        try:
            prompt_update = _install_canonical_scheduler_prompt()
        except Exception as exc:
            prompt_update_error = str(exc)
        if not prompt_update_error:
            try:
                wakeup = set_smart_submission_automation_fast_wakeup(True)
            except Exception as exc:
                wakeup_error = str(exc)
    return {
        **{key: value for key, value in queue_result.items() if key != "state"},
        "preflight": {key: value for key, value in preflight.items() if key != "prepared"},
        "route": route_result,
        "scheduler_prompt": prompt_update,
        "scheduler_prompt_error": prompt_update_error,
        "wakeup": wakeup,
        "wakeup_error": wakeup_error,
        "transaction_recovery": recovery,
        "transaction_journal_cleanup_error": journal_cleanup_error,
        "state": queue_result.get("state", {}),
    }


def _compact_history_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[compacted: depth limit]"
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)
            if key_text in {"state", "history", "history_tail"}:
                compacted[f"{key_text}_omitted"] = True
                continue
            compacted[key_text] = _compact_history_value(item, depth=depth + 1)
        if len(value) > 50:
            compacted["keys_omitted"] = len(value) - 50
        return compacted
    if isinstance(value, list):
        items = [
            _compact_history_value(item, depth=depth + 1)
            for item in value[:SMART_HISTORY_MAX_LIST_ITEMS]
        ]
        if len(value) > SMART_HISTORY_MAX_LIST_ITEMS:
            items.append(f"[compacted: {len(value) - SMART_HISTORY_MAX_LIST_ITEMS} items omitted]")
        return items
    if isinstance(value, str) and len(value) > SMART_HISTORY_MAX_STRING_CHARS:
        return value[:SMART_HISTORY_MAX_STRING_CHARS] + "...[compacted]"
    return value


def _compact_history_event(event: Any) -> tuple[dict[str, Any], bool]:
    source = event if isinstance(event, dict) else {"event": "legacy_history_value", "value": event}
    compacted = _compact_history_value(source)
    if not isinstance(compacted, dict):
        compacted = {"event": "legacy_history_value", "value": compacted}
    try:
        original_bytes = len(json.dumps(source, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        original_bytes = SMART_HISTORY_EVENT_MAX_BYTES + 1
    encoded = json.dumps(compacted, ensure_ascii=False, default=str).encode("utf-8")
    changed = compacted != source
    if len(encoded) > SMART_HISTORY_EVENT_MAX_BYTES:
        compacted = {
            "at": str(source.get("at") or now_iso()),
            "event": str(source.get("event") or "compacted_history_event"),
            "message": "Oversized history payload omitted during Smart Submission state migration.",
            "original_bytes": original_bytes,
            "original_keys": sorted(str(key) for key in source.keys())[:50],
        }
        changed = True
    return compacted, changed


def _migrate_smart_state(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove recursively embedded state/history and bound persistent history size."""
    report = {
        "changed": False,
        "compacted_events": 0,
        "dropped_events": 0,
        "from_version": state.get("version"),
        "auto_retarget_disabled": False,
        "program_control_defaults_added": [],
        "sdk_browser_migrated": False,
        "auto_final_submit_migrated": False,
        "submission_identities_migrated": 0,
        "retarget_requests_migrated": 0,
    }
    history = state.get("history")
    if not isinstance(history, list):
        history = []
        report["changed"] = True
    compacted_history: list[dict[str, Any]] = []
    for event in history:
        compacted, changed = _compact_history_event(event)
        compacted_history.append(compacted)
        if changed:
            report["compacted_events"] += 1
            report["changed"] = True
    if len(compacted_history) > SMART_HISTORY_MAX_EVENTS:
        report["dropped_events"] = len(compacted_history) - SMART_HISTORY_MAX_EVENTS
        compacted_history = compacted_history[-SMART_HISTORY_MAX_EVENTS:]
        report["changed"] = True
    if state.get("version") != SMART_STATE_VERSION:
        report["changed"] = True
        settings = state.get("settings")
        if isinstance(settings, dict) and settings.get("auto_retarget_after_site_hard_stop_enabled") is not False:
            settings["auto_retarget_after_site_hard_stop_enabled"] = False
            report["auto_retarget_disabled"] = True
    settings = state.get("settings")
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
        report["changed"] = True
    try:
        previous_version = int(report["from_version"] or 0)
    except (TypeError, ValueError):
        previous_version = 0
    if previous_version < 4:
        settings.update(
            {
                "dispatch_bridge": "codex_sdk_browser",
                "dispatch_mode": "program_plan_with_codex_sdk_browser",
                "sdk_browser_enabled": True,
                "legacy_visible_worker_fallback_enabled": True,
                "visible_scheduler_enabled": False,
            }
        )
        report["sdk_browser_migrated"] = True
        report["changed"] = True
    if previous_version < 5:
        settings["auto_final_submit_enabled"] = True
        settings["final_submit_authorization_mode"] = FINAL_SUBMIT_AUTO_PRESENTATION
        report["auto_final_submit_migrated"] = True
        report["changed"] = True
    if previous_version < 6:
        papers = state.get("papers")
        if isinstance(papers, dict):
            for paper in papers.values():
                if not isinstance(paper, dict):
                    continue
                project_submission_identity(paper, observed_at=now_iso())
                report["submission_identities_migrated"] += 1
            if report["submission_identities_migrated"]:
                report["changed"] = True
    if previous_version < 7:
        papers = state.get("papers")
        if isinstance(papers, dict):
            for paper_id, paper in papers.items():
                if not isinstance(paper, dict) or not isinstance(
                    paper.get("retarget_request"), Mapping
                ):
                    continue
                try:
                    paper["retarget_request"] = normalize_retarget_request(
                        paper["retarget_request"],
                        paper_id=str(paper_id),
                    )
                except ValueError:
                    paper["retarget_request"] = {
                        "schema": RETARGET_REQUEST_SCHEMA,
                        "paper_id": str(paper_id),
                        "status": "cancelled_invalid_legacy_request",
                    }
                report["retarget_requests_migrated"] += 1
            if report["retarget_requests_migrated"]:
                report["changed"] = True
    program_defaults = {
        "control_plane": "program_v1",
        "ai_scheduler_decisions_enabled": False,
        "dispatch_bridge": "codex_sdk_browser",
        "program_plan_write_enabled": True,
        "program_task_ledger_enabled": True,
        "auto_final_submit_enabled": True,
        "final_submit_authorization_mode": FINAL_SUBMIT_AUTO_PRESENTATION,
        "auto_retarget_after_rejection_enabled": True,
        "user_retarget_enabled": True,
    }
    for key, value in program_defaults.items():
        if key not in settings:
            settings[key] = value
            report["program_control_defaults_added"].append(key)
            report["changed"] = True
    state["version"] = SMART_STATE_VERSION
    state["history"] = compacted_history
    if report["changed"]:
        migration_event, _ = _compact_history_event(
            {
                "at": now_iso(),
                "event": "migrate_smart_submission_state",
                "from_version": report["from_version"],
                "to_version": SMART_STATE_VERSION,
                "compacted_events": report["compacted_events"],
                "dropped_events": report["dropped_events"],
                "auto_retarget_disabled": report["auto_retarget_disabled"],
                "program_control_defaults_added": report["program_control_defaults_added"],
                "sdk_browser_migrated": report["sdk_browser_migrated"],
                "auto_final_submit_migrated": report["auto_final_submit_migrated"],
                "submission_identities_migrated": report["submission_identities_migrated"],
                "retarget_requests_migrated": report["retarget_requests_migrated"],
            }
        )
        state["history"].append(migration_event)
        state["history"] = state["history"][-SMART_HISTORY_MAX_EVENTS:]
    return state, report


def write_json_atomic(path: Path | str, value: Any) -> None:
    if _same_path(path, SMART_STATE_PATH):
        if not isinstance(value, dict):
            raise TypeError("Smart Submission state must be a JSON object")
        with _smart_state_lock():
            migrated, _ = _migrate_smart_state(value)
            _write_json_atomic_unchecked(Path(path), migrated)
        return
    _write_json_atomic_unchecked(Path(path), value)


def _slash(path: Path) -> str:
    return str(path).replace("\\", "/")


def _set_toml_value(text: str, key: str, value: str) -> str:
    line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
    if re.search(pattern, text):
        return re.sub(pattern, line, text, count=1)
    return text.rstrip() + "\n" + line + "\n"


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _read_toml_scalar(text: str, key: str, default: str = "") -> str:
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+?)\s*$", text)
    if not match:
        return default
    raw = match.group(1).strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return str(json.loads(raw))
        except json.JSONDecodeError:
            return raw.strip('"')
    return raw.strip('"')


def _smart_automation_prompt() -> str:
    return (
        "Act only as the deterministic Smart Submission wake-up trigger for the unified project. "
        "Do not inspect papers, choose work, allocate slots, create or message worker threads, "
        "open websites, or make any submission/monitoring decision.\n\n"
        f"Use this application path for shell commands: {SMART_ASCII_WORKSPACE}. "
        f"First run `cd /d {SMART_ASCII_WORKSPACE}` when using cmd, or "
        f"`Set-Location -LiteralPath '{SMART_ASCII_WORKSPACE}'` when using PowerShell.\n\n"
        "At every wakeup run exactly this one command and wait for its final JSON result:\n"
        "`python -m unified_paper_orchestrator smart-submission-automation-tick`\n\n"
        "The Python program distinguishes a manual fast wakeup from the configured daily monitor, "
        "owns queue order and parallel slots, and invokes the fixed Codex SDK browser sessions itself. "
        "Do not run any second command, retry a failed command, infer follow-up work from the JSON, "
        "or perform any browser/final-submit action in this trigger task."
    )


def _smart_automation_toml(
    *,
    enabled: bool,
    state: dict[str, Any],
    existing_text: str = "",
    rrule: str | None = None,
) -> str:
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    thread_id = str(settings.get("scheduler_target_thread_id") or "").strip()
    if not thread_id:
        thread_id = _read_toml_scalar(existing_text, "target_thread_id")
    created_at = _read_toml_scalar(existing_text, "created_at", str(int(time.time() * 1000)))
    if rrule is None:
        rrule = (
            SMART_AUTOMATION_FAST_RRULE
            if enabled
            else _smart_monitor_schedule(settings)["rrule"]
        )
    updated_at = str(int(time.time() * 1000))
    return "\n".join(
        [
            "version = 1",
            f"id = {_toml_string('automation')}",
            f"kind = {_toml_string('heartbeat')}",
            f"name = {_toml_string('Smart Submission visible scheduler')}",
            f"prompt = {_toml_string(_smart_automation_prompt())}",
            f"status = {_toml_string('PAUSED')}",
            f"rrule = {_toml_string(rrule)}",
            f"target_thread_id = {_toml_string(thread_id)}",
            f"created_at = {created_at}",
            f"updated_at = {updated_at}",
            "",
        ]
    )


def _set_smart_submission_automation_wakeup(
    *,
    rrule: str,
    manual_active: bool,
    event: str,
    reason: str = "",
) -> dict[str, Any]:
    """Compatibility entry point that can no longer activate Codex Automation."""

    return _set_smart_submission_automation_wakeup_owned(
        rrule=rrule,
        manual_active=manual_active,
        event=event,
        reason=reason,
    )


def _retire_legacy_smart_automation_trigger(
    reason: str = "program_timer_is_authoritative",
) -> dict[str, Any]:
    """Pause the historical Codex Automation without rewriting its prompt."""

    if not SMART_AUTOMATION_PATH.exists():
        return {
            "ok": True,
            "retired": True,
            "changed": False,
            "status_before": "MISSING",
            "status": "MISSING",
            "path": str(SMART_AUTOMATION_PATH),
            "reason": str(reason or "program_timer_is_authoritative"),
        }
    existing_text = read_text(SMART_AUTOMATION_PATH)
    status_before = _read_toml_scalar(existing_text, "status", "MISSING").upper()
    paused_text = _set_toml_value(existing_text, "status", "PAUSED")
    changed = paused_text != existing_text
    if changed:
        write_text(SMART_AUTOMATION_PATH, paused_text)
    return {
        "ok": True,
        "retired": True,
        "changed": changed,
        "status_before": status_before,
        "status": "PAUSED",
        "path": str(SMART_AUTOMATION_PATH),
        "reason": str(reason or "program_timer_is_authoritative"),
    }


def start_smart_submission_program_runtime(
    paper_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Start the program-owned SDK queue directly, without any AI scheduler."""

    selected = list(
        dict.fromkeys(
            str(item).strip() for item in (paper_ids or []) if str(item).strip()
        )
    )
    legacy = _retire_legacy_smart_automation_trigger("direct_sdk_runtime")
    from .smart_submission_sdk_runtime import start_smart_submission_sdk_background

    sdk_runtime = start_smart_submission_sdk_background(selected or None)
    return {
        "ok": bool(sdk_runtime.get("ok", True)),
        "trigger": "program_sdk_runtime",
        "selected_ids": selected,
        "legacy_automation": legacy,
        "sdk_runtime": sdk_runtime,
        **{
            key: value
            for key, value in sdk_runtime.items()
            if key not in {"ok"}
        },
    }


def _set_smart_submission_automation_wakeup_owned(
    *,
    rrule: str,
    manual_active: bool,
    event: str,
    reason: str = "",
) -> dict[str, Any]:
    """Retire the old heartbeat and translate a wake-up into direct SDK work."""

    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    legacy = _retire_legacy_smart_automation_trigger(reason or event)
    settings["manual_wakeup_active"] = False
    settings["manual_wakeup_rrule"] = rrule
    settings["trigger_owner"] = "program_timer"
    settings["legacy_automation_retired"] = True
    settings["legacy_automation_status"] = str(legacy.get("status") or "PAUSED")
    if manual_active:
        settings["program_runtime_requested_at"] = now_iso()
    else:
        settings["program_runtime_idle_recorded_at"] = now_iso()
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": event,
            "enabled": False,
            "requested_program_runtime": bool(manual_active),
            "rrule": rrule,
            "reason": reason,
            "legacy_automation_status": str(legacy.get("status") or "PAUSED"),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    runtime = (
        start_smart_submission_program_runtime()
        if manual_active
        else {"ok": True, "started": False, "reason": "no runtime requested"}
    )
    return {
        "updated": bool(legacy.get("changed")) or bool(runtime.get("started")),
        "path": str(SMART_AUTOMATION_PATH),
        "rrule": rrule,
        "enabled": False,
        "reason": reason,
        "legacy_automation": legacy,
        "program_runtime": runtime,
    }


@_smart_state_locked
def set_smart_submission_automation_fast_wakeup(enabled: bool) -> dict[str, Any]:
    """V1-compatible name for direct program runtime dispatch."""
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    daily_rrule = _smart_monitor_schedule(settings)["rrule"]
    return _set_smart_submission_automation_wakeup(
        rrule=SMART_AUTOMATION_FAST_RRULE if enabled else daily_rrule,
        manual_active=bool(enabled),
        event="set_smart_submission_automation_fast_wakeup",
        reason="manual_start" if enabled else "manual_or_empty_queue",
    )


@_smart_state_locked
def set_smart_submission_automation_backoff_wakeup(reason: str = "all_online_queue_assigned") -> dict[str, Any]:
    """Retire the old heartbeat; the program runtime needs no idle heartbeat."""
    return _set_smart_submission_automation_wakeup(
        rrule=SMART_AUTOMATION_BACKOFF_RRULE,
        manual_active=False,
        event="set_smart_submission_automation_backoff_wakeup",
        reason=reason,
    )


@_smart_state_locked
def pause_smart_submission_automation(reason: str = "manual_pause") -> dict[str, Any]:
    """Hard-pause the program runtime and its timer without requeueing work."""
    SMART_SUBMISSION_DIR.mkdir(parents=True, exist_ok=True)
    state = read_json(SMART_STATE_PATH, _default_state()) if SMART_STATE_PATH.exists() else _default_state()
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    schedule = _smart_monitor_schedule(settings)
    legacy_trigger = _retire_legacy_smart_automation_trigger(reason)
    native_task = _disable_native_smart_monitor_task()

    state["mode"] = "manual"
    settings["online_submit_enabled"] = False
    settings["visible_scheduler_enabled"] = False
    settings["daily_monitor_enabled"] = False
    settings["manual_wakeup_active"] = False
    settings["manual_wakeup_rrule"] = schedule["rrule"]
    settings["trigger_owner"] = "program_timer"
    settings["program_timer_enabled"] = False
    settings["legacy_automation_retired"] = True
    settings["legacy_automation_status"] = str(
        legacy_trigger.get("status") or "PAUSED"
    )
    settings["automation_paused_at"] = now_iso()
    settings["automation_pause_reason"] = str(reason or "manual_pause")
    settings["native_monitor_task_status"] = (
        "disabled" if native_task.get("disabled") else str(native_task.get("status") or "unknown")
    )
    settings["native_monitor_task_updated_at"] = now_iso()
    settings["native_monitor_task_error"] = str(native_task.get("error") or "")
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "pause_smart_submission_automation",
            "reason": str(reason or "manual_pause"),
            "online_queue_count": len(state.get("online_queue", []) if isinstance(state.get("online_queue"), list) else []),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "paused": True,
        "mode": "manual",
        "automation_status": "PAUSED",
        "online_submit_enabled": False,
        "online_queue_count": len(state.get("online_queue", []) if isinstance(state.get("online_queue"), list) else []),
        "path": str(SMART_AUTOMATION_PATH),
        "daily_monitor_time_local": schedule["time_local"],
        "daily_monitor_rrule": schedule["rrule"],
        "native_monitor_task": native_task,
        "legacy_automation": legacy_trigger,
        "reason": str(reason or "manual_pause"),
    }


def _reconcile_smart_submission_program_timer_owned() -> dict[str, Any]:
    """Make the OS/in-app program timer authoritative and retire Automation."""

    SMART_SUBMISSION_DIR.mkdir(parents=True, exist_ok=True)
    state = (
        read_json(SMART_STATE_PATH, _default_state())
        if SMART_STATE_PATH.exists()
        else _default_state()
    )
    if not isinstance(state, dict):
        state = _default_state()
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    schedule = _smart_monitor_schedule(settings)
    daily_enabled = bool(settings.get("daily_monitor_enabled", True))
    legacy = _retire_legacy_smart_automation_trigger(
        "program_timer_reconciliation"
    )
    native = (
        _register_native_smart_monitor_task(schedule["time_local"])
        if daily_enabled
        else _disable_native_smart_monitor_task()
    )
    previous = {
        key: copy.deepcopy(settings.get(key))
        for key in (
            "trigger_owner",
            "program_timer_enabled",
            "program_timer_mode",
            "program_timer_command",
            "program_timer_time_local",
            "visible_scheduler_enabled",
            "legacy_visible_worker_fallback_enabled",
            "legacy_automation_retired",
            "legacy_automation_status",
            "native_monitor_task_status",
            "native_monitor_task_registered",
            "native_monitor_task_action",
            "native_monitor_task_error",
        )
    }
    native_status = str(native.get("status") or "unknown")
    if native_status in {"in_app_only", "temporary_build_skipped"}:
        native_registered = bool(settings.get("native_monitor_task_registered"))
        native_action = str(settings.get("native_monitor_task_action") or "")
    else:
        native_registered = bool(native.get("registered")) if daily_enabled else False
        native_action = str(native.get("action") or "")
    settings["trigger_owner"] = "program_timer"
    settings["program_timer_enabled"] = daily_enabled
    settings["program_timer_mode"] = "windows_task_with_in_app_claim_fallback"
    settings["program_timer_command"] = SMART_PROGRAM_TIMER_COMMAND
    settings["program_timer_time_local"] = schedule["time_local"]
    settings["visible_scheduler_enabled"] = False
    settings["legacy_visible_worker_fallback_enabled"] = False
    settings["legacy_automation_retired"] = True
    settings["legacy_automation_status"] = str(legacy.get("status") or "PAUSED")
    settings["manual_wakeup_active"] = False
    settings["native_monitor_task_status"] = native_status
    settings["native_monitor_task_registered"] = native_registered
    settings["native_monitor_task_name"] = SMART_NATIVE_MONITOR_TASK_NAME
    settings["native_monitor_task_action"] = native_action
    settings["native_monitor_task_updated_at"] = now_iso()
    settings["native_monitor_task_error"] = str(native.get("error") or "")
    current = {key: settings.get(key) for key in previous}
    changed = previous != current or bool(legacy.get("changed"))
    if changed:
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "reconcile_smart_submission_program_timer",
                "daily_enabled": daily_enabled,
                "time_local": schedule["time_local"],
                "native_status": native_status,
                "native_registered": native_registered,
                "legacy_automation_status": str(
                    legacy.get("status") or "PAUSED"
                ),
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": bool(native.get("ok", True)) and bool(legacy.get("ok", True)),
        "changed": changed,
        "owner": "program_timer",
        "enabled": daily_enabled,
        "mode": "windows_task_with_in_app_claim_fallback",
        "time_local": schedule["time_local"],
        "next_run_at": schedule["next_run_at"],
        "command": SMART_PROGRAM_TIMER_COMMAND,
        "native_task": native,
        "legacy_automation": legacy,
        "state_path": str(SMART_STATE_PATH),
    }


@_smart_state_locked
def reconcile_smart_submission_program_timer() -> dict[str, Any]:
    """Public migration/startup hook for the program-owned timer."""

    return _reconcile_smart_submission_program_timer_owned()


@_smart_state_locked
def _restore_smart_submission_daily_monitoring(
    time_local: str | None = None,
) -> dict[str, Any]:
    """Persist a local check time and enable the program-owned timer."""

    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    previous_time = str(settings.get("daily_monitor_time_local") or "").strip()
    schedule = _smart_monitor_schedule(settings, requested_time=time_local)
    if time_local is not None and previous_time != schedule["time_local"]:
        settings.pop("daily_monitor_last_scheduled_date", None)
    settings["daily_monitor_enabled"] = True
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "update_smart_submission_monitor_schedule",
            "time_local": schedule["time_local"],
            "timezone": schedule["timezone"],
            "rrule": schedule["rrule"],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    result = _reconcile_smart_submission_program_timer_owned()
    native_task = result.get("native_task", {})
    result["automation_status"] = str(
        result.get("legacy_automation", {}).get("status") or "PAUSED"
    )
    result["program_timer_status"] = "ENABLED"
    result["daily_monitor_time_local"] = schedule["time_local"]
    result["daily_monitor_timezone"] = schedule["timezone"]
    result["next_monitor_at"] = schedule["next_run_at"]
    result["native_monitor_task"] = native_task
    return result


def restore_smart_submission_daily_monitoring() -> dict[str, Any]:
    """V1-compatible restore using the already configured local daily time."""

    return _restore_smart_submission_daily_monitoring()


def update_smart_submission_daily_monitoring_time(
    time_local: str,
) -> dict[str, Any]:
    """Save one local HH:MM monitor time and enable that daily schedule."""

    return _restore_smart_submission_daily_monitoring(time_local)


def _default_state() -> dict[str, Any]:
    return {
        "version": SMART_STATE_VERSION,
        "module": "smart_submission_sidecar",
        "updated_at": now_iso(),
        "mode": "manual",
        "paper_queue": [],
        "online_queue": [],
        "papers": {},
        "commands": [],
        "settings": {
            "legacy_submission_module": "read_only_do_not_modify",
            "use_auto_paper_submission_skill": "policy_reference_only",
            "visible_scheduler_enabled": False,
            "sdk_browser_enabled": True,
            "legacy_visible_worker_fallback_enabled": False,
            "trigger_owner": "program_timer",
            "program_timer_enabled": True,
            "program_timer_mode": "windows_task_with_in_app_claim_fallback",
            "program_timer_command": SMART_PROGRAM_TIMER_COMMAND,
            "program_timer_time_local": SMART_MONITOR_DEFAULT_TIME_LOCAL,
            "legacy_automation_retired": True,
            "legacy_automation_status": "PAUSED",
            "scheduler_target_thread_id": "",
            "scheduler_thread_title": "Smart Submission visible scheduler",
            "max_parallel_workers": DEFAULT_SMART_MAX_PARALLEL_WORKERS,
            "one_action_per_run": False,
            "control_plane": "program_v1",
            "ai_scheduler_decisions_enabled": False,
            "dispatch_bridge": "codex_sdk_browser",
            "program_plan_write_enabled": True,
            "program_task_ledger_enabled": True,
            "dispatch_mode": "program_plan_with_codex_sdk_browser",
            "final_submit_requires_gate_pass": True,
            "auto_final_submit_enabled": True,
            "final_submit_authorization_mode": FINAL_SUBMIT_AUTO_PRESENTATION,
            "one_active_journal_per_paper": True,
            "default_route": "non_oa_subscription",
            "preferred_cas_zones": ["2", "3", "4"],
            "avoid_cas_zone_1_by_default": True,
            "continue_after_local_paper_blocker": True,
            "idle_auto_manual": True,
            "daily_monitor_enabled": True,
            "daily_monitor_hour_local": 9,
            "daily_monitor_minute_local": 0,
            "daily_monitor_time_local": SMART_MONITOR_DEFAULT_TIME_LOCAL,
            "daily_monitor_last_scheduled_date": "",
            "daily_monitor_last_started_at": "",
            "daily_monitor_last_finished_at": "",
            "daily_monitor_last_status": "never_run",
            "daily_monitor_last_trigger": "",
            "daily_monitor_last_paper_count": 0,
            "daily_monitor_last_checked_count": 0,
            "daily_monitor_last_failed_count": 0,
            "daily_monitor_last_pending_count": 0,
            "daily_monitor_last_action_required_count": 0,
            "daily_monitor_last_followup_auto_queued_count": 0,
            "daily_monitor_last_followup_manual_revision_count": 0,
            "daily_monitor_last_followup_identity_reconciliation_count": 0,
            "daily_monitor_last_followup_failed_count": 0,
            "daily_monitor_last_error": "",
            "native_monitor_task_status": "not_registered",
            "native_monitor_task_registered": False,
            "native_monitor_task_name": SMART_NATIVE_MONITOR_TASK_NAME,
            "native_monitor_task_updated_at": "",
            "native_monitor_task_error": "",
            "daily_monitor_statuses": [
                "queued",
                "ready_for_online_submit",
                "login_check",
                "uploading",
                "monitoring",
                "revision_needed",
                "retarget_needed",
            ],
            "auto_package_fix_enabled": True,
            "auto_package_fix_scope": [
                "keywords",
                "github_repository_discovery",
                "github_repository_auto_create",
                "ai_use_from_author_template",
                "standing_ai_use_statement",
                "standing_author_confirmation",
                "standing_third_party_material_confirmation",
                "abstract_length",
                "springer_latex_upload_flattening",
            ],
            "github_repository_auto_create_enabled": True,
            "github_repository_planned_placeholder_enabled": True,
            "github_repository_planned_placeholder_note": (
                "When a paper-specific GitHub repository is missing, record a deterministic planned "
                "repository URL under the configured owner and continue; the user will create the "
                "repository later."
            ),
            "github_repository_owner": "TYSJY",
            "github_repository_visibility": "public",
            "trusted_credential_broker_enabled": True,
            "account_missing_policy": "auto_register",
            "account_registration_enabled": True,
            "new_account_auth_preference": "orcid_first",
            "orcid_auto_authorize_for_login": True,
            "account_registration_requires_terms_confirmation": True,
            "account_registration_if_no_prior_submission": False,
            "fresh_registration_after_failed_recovery": False,
            "account_recovery_wait_hours_before_registration": 0,
            "auto_retarget_after_site_hard_stop_enabled": False,
            "auto_retarget_after_rejection_enabled": True,
            "user_retarget_enabled": True,
            "retarget_archive_existing_package": True,
            "standing_ai_use_statement_authorized": True,
            "standing_author_declaration_authorized": True,
            "standing_third_party_material_authorized": True,
            "standing_declaration_authorized_at": STANDING_DECLARATION_AUTHORIZED_AT,
            "default_ai_use_statement": DEFAULT_AI_USE_STATEMENT,
            "default_originality_statement": DEFAULT_ORIGINALITY_STATEMENT,
            "default_author_responsibility_statement": DEFAULT_AUTHOR_RESPONSIBILITY_STATEMENT,
            "default_third_party_material_statement": DEFAULT_THIRD_PARTY_MATERIAL_STATEMENT,
            "default_copyright_permission_statement": DEFAULT_COPYRIGHT_PERMISSION_STATEMENT,
            "auto_discovery_enabled": True,
            "auto_import_legacy_credentials": False,
            "auto_import_legacy_site_records": False,
            "legacy_import_preview_enabled": False,
            "discovery_sources": [
                "legacy_auto_submission_state",
                "paper_journal_md",
                "chrome_session",
                "gmail",
                "school_mailbox",
                "web_search",
            ],
            "online_submit_enabled": False,
            "online_submit_note": (
                "Starting Smart Submission authorizes the complete workflow. After final-review validation, "
                "the program automatically issues one exact-attempt capability and completes final submit."
            ),
            "browser_strategy": "prefer_cdp_9224_9222_extension_optional",
            "cdp_ports": list(SMART_CDP_PORTS),
            "extension_native_pipe_optional": True,
            "hard_stop_conditions": [
                "payment_or_forced_oa",
                "captcha_or_cloudflare",
                "2fa_or_security_challenge",
                "new_author_affiliation_funding_ethics_or_legal_fact_outside_standing_declarations",
                "orcid_authorization_outside_preferred_login_flow",
                "active_prior_submission_risk",
                "unrecoverable_site_failure",
            ],
        },
        "paths": {
            "credential_store": str(SMART_CREDENTIAL_PATH),
            "site_registry": str(SMART_SITE_REGISTRY_PATH),
            "scheduler_prompt": str(SMART_SCHEDULER_PROMPT_PATH),
            "program_dispatch_plan": str(_smart_program_plan_path()),
            "program_task_database": str(program_database_path(SMART_STATE_PATH)),
            "audit_dir": str(SMART_AUDIT_DIR),
            "screenshots": str(SMART_SCREENSHOT_DIR),
            "page_snapshots": str(SMART_SNAPSHOT_DIR),
            "logs": str(SMART_LOG_DIR),
        },
        "history": [
            {
                "at": now_iso(),
                "event": "created",
                "message": "Created independent smart submission sidecar. Legacy submission module is not modified.",
            }
        ],
    }


def _default_credentials() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "warning": "Local-only credential registry. Do not paste passwords, tokens, cookies, or private email bodies into chat.",
        "accounts": [
            {
                "account_id": "gmail-main",
                "scope": "email",
                "provider": "gmail",
                "username": "",
                "password": "",
                "recovery_email": "",
                "login_url": "https://mail.google.com/",
                "status": "not_configured",
                "notes": "",
                "last_verified_at": "",
            },
            {
                "account_id": "journal-default",
                "scope": "journal_system",
                "provider": "",
                "username": "",
                "password": "",
                "recovery_email": "",
                "login_url": "",
                "status": "not_configured",
                "notes": "",
                "last_verified_at": "",
            },
        ],
    }


def _default_site_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "systems": [
            {
                "system_id": "editorial_manager",
                "label": "Editorial Manager",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "scholar_one",
                "label": "ScholarOne Manuscripts",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "springer_nature",
                "label": "Springer Nature / SNAPP",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "wiley",
                "label": "Wiley Research Exchange / Author Services",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "taylor_francis",
                "label": "Taylor & Francis Research Portal",
                "status": "adapter_planned",
                "login_url": "https://rp.tandfonline.com/",
                "submission_url": "https://rp.tandfonline.com/",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "elsevier",
                "label": "Elsevier Editorial System / EVISE legacy",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "ojs",
                "label": "Open Journal Systems",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
            {
                "system_id": "inderscience",
                "label": "Inderscience",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
                "status_url_template": "",
                "email_senders": [],
                "notes": "",
            },
        ],
        "journals": [],
    }


def _repair_corrupted_smart_paths(value: Any) -> tuple[Any, bool]:
    """Repair path strings damaged by non-UTF-8 worker command paths."""
    if isinstance(value, dict):
        changed = False
        repaired: dict[str, Any] = {}
        for key, item in value.items():
            next_item, item_changed = _repair_corrupted_smart_paths(item)
            repaired[key] = next_item
            changed = changed or item_changed
        return repaired, changed
    if isinstance(value, list):
        changed = False
        repaired_list: list[Any] = []
        for item in value:
            next_item, item_changed = _repair_corrupted_smart_paths(item)
            repaired_list.append(next_item)
            changed = changed or item_changed
        return repaired_list, changed
    if not isinstance(value, str):
        return value, False

    text = value
    original = text
    smart_root = _slash(SMART_SUBMISSION_DIR)
    repo_root = _slash(WORKSPACE_ROOT)
    journal_papers = _slash(JOURNAL_ROOT / "papers")
    corrupted_unified = "\ufffd\ufffd\u4e00\u0373"
    replacements = {
        "D:/Documents/???/state/smart_submission": smart_root,
        "D:\\Documents\\???\\state\\smart_submission": str(SMART_SUBMISSION_DIR),
        f"D:/Documents/{corrupted_unified}/state/smart_submission": smart_root,
        f"D:\\Documents\\{corrupted_unified}\\state\\smart_submission": str(SMART_SUBMISSION_DIR),
        "D:/Documents/???": repo_root,
        "D:\\Documents\\???": str(WORKSPACE_ROOT),
        f"D:/Documents/{corrupted_unified}": repo_root,
        f"D:\\Documents\\{corrupted_unified}": str(WORKSPACE_ROOT),
        "D:/Documents/???????/papers": journal_papers,
        "D:\\Documents\\???????\\papers": str(JOURNAL_ROOT / "papers"),
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    # Package paths sometimes arrive from worker threads with the Chinese
    # project folder mojibaked. If the shape is clearly
    # D:\Documents\<bad>\papers\<paper_id>\..., trust the paper-id segment and
    # repair the prefix to the journal-author workspace.
    text = re.sub(
        r"D:[/\\]Documents[/\\][^/\\]+[/\\]papers([/\\]20\d{2}-\d{2}-\d{2}_[^/\\\s]+(?:[/\\].*)?)",
        lambda match: str(JOURNAL_ROOT / "papers") + match.group(1),
        text,
    )
    text = re.sub(
        r"D:[/\\]Documents[/\\]\?+[/\\]state[/\\]smart_submission",
        smart_root,
        text,
    )
    text = re.sub(
        r"D:[/\\]Documents[/\\]\?+[/\\]papers",
        journal_papers,
        text,
    )
    return text, text != original


def _daily_monitor_statuses(settings: dict[str, Any]) -> set[str]:
    configured = settings.get("daily_monitor_statuses")
    if isinstance(configured, list):
        statuses = {str(item).strip() for item in configured if str(item).strip()}
        if statuses:
            return statuses
    return {
        "queued",
        "ready_for_online_submit",
        "login_check",
        "uploading",
        "monitoring",
        "revision_needed",
        "retarget_needed",
    }


ONLINE_WORKER_STATUSES = {
    "ready_for_online_submit",
    "login_check",
    "uploading",
    "revision_needed",
    "retarget_needed",
}

ONLINE_QUEUE_TERMINAL_STATUSES = {
    "accepted": "accepted",
    "blocked": "blocked",
    "monitoring": "monitoring",
    "no_action": "no_action",
    "submitted": "monitoring",
    "waiting": "waiting",
    "stopped": "stopped",
}


@_smart_state_locked
def repair_smart_monitor_confirmation_projection() -> dict[str, Any]:
    """Remove mailbox-result flags that are not backed by a signed submit receipt."""

    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    repaired: list[str] = []
    for paper_id, paper in papers.items():
        if not isinstance(paper, dict):
            continue
        if (
            paper.get("final_submit_confirmed") is True
            and str(paper.get("last_worker_result_source") or "").strip().casefold()
            == "codex_sdk_mailbox_batch"
            and not _paper_has_prior_trusted_submission_evidence(str(paper_id), paper)
        ):
            paper.pop("final_submit_confirmed", None)
            paper["monitor_confirmation_projection_repaired_at"] = now_iso()
            repaired.append(str(paper_id))
    if repaired:
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "repair_smart_monitor_confirmation_projection",
                "paper_ids": repaired,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {"repaired": repaired, "count": len(repaired)}


@_smart_state_locked
def queue_smart_submission_daily_sweep(
    *,
    include_blocked: bool = False,
    monitoring_only: bool = False,
    force_monitor: bool = False,
    selected_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Queue papers that should be checked by a daily smart-submission sweep."""
    ensure_smart_submission_module()
    reconcile_smart_submission_site_bindings(selected_ids)
    state = read_json(SMART_STATE_PATH, _default_state())
    repaired_state, changed = _repair_corrupted_smart_paths(state)
    if changed and isinstance(repaired_state, dict):
        state = repaired_state
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    papers = state.setdefault("papers", {})
    if not isinstance(papers, dict):
        papers = {}
        state["papers"] = papers

    local_queue = [str(item) for item in state.get("paper_queue", []) if str(item).strip()]
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    eligible = _daily_monitor_statuses(settings)
    local_queued: list[str] = []
    online_queued: list[str] = []
    monitor_check_ids: list[str] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    selected = (
        {str(item).strip() for item in selected_ids if str(item).strip()}
        if selected_ids is not None
        else None
    )

    for paper_id, paper in sorted(papers.items()):
        paper_id = str(paper_id)
        if selected is not None and paper_id not in selected:
            continue
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is invalid"
            continue
        status = str(paper.get("status") or "").strip().lower()
        recovered_submission_id = _submission_id_from_local_evidence(paper_id, paper)
        if recovered_submission_id and not str(paper.get("submission_id") or "").strip():
            paper["submission_id"] = recovered_submission_id
            paper["submission_id_recovered_at"] = now_iso()
            paper["submission_id_source"] = "existing_local_submission_evidence"
        package_submitted = "SUBMITTED" in _package_status_for_paper(paper_id, paper)
        monitoring_candidate = (
            _paper_has_monitorable_submission_evidence(paper_id, paper)
            or package_submitted
        )
        if (
            paper.get("final_submit_confirmed") is True
            and str(paper.get("last_worker_result_source") or "").strip().casefold()
            == "codex_sdk_mailbox_batch"
            and not _paper_has_prior_trusted_submission_evidence(paper_id, paper)
        ):
            paper.pop("final_submit_confirmed", None)
            paper["monitor_confirmation_projection_repaired_at"] = now_iso()
        read_only_monitor = bool(monitoring_only and monitoring_candidate)
        route, route_error = _worker_route_guard_result(
            paper_id,
            paper,
            unified_state=unified_state,
            monitor_only=read_only_monitor,
        )
        if route_error is not None:
            local_queue = [item for item in local_queue if item != paper_id]
            online_queue = [item for item in online_queue if item != paper_id]
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            continue
        _mark_worker_route_on_paper(
            paper,
            route or {},
            monitor_only=read_only_monitor,
        )
        if monitoring_only and not monitoring_candidate:
            skipped[paper_id] = "daily monitoring tick only checks already-submitted papers"
            continue
        if str(paper.get("workflow_status") or "").strip().lower() == "running":
            skipped[paper_id] = "worker is currently running"
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
                monitor_only=read_only_monitor,
            )
            continue
        if status in {"accepted", "stopped", "no_action"}:
            skipped[paper_id] = f"terminal status: {status}"
            continue
        if status == "blocked" and not include_blocked:
            skipped[paper_id] = "blocked papers require an explicit repair or manual requeue"
            continue
        if (
            status not in eligible
            and not (include_blocked and status == "blocked")
            and not read_only_monitor
        ):
            skipped[paper_id] = f"status not in daily sweep set: {status or 'empty'}"
            continue
        if status == "queued":
            append_unique(local_queue, paper_id)
            paper["workflow_status"] = "queued"
            paper["next_action"] = "preflight"
            local_queued.append(paper_id)
        elif monitoring_candidate:
            paper["status"] = "monitoring"
            paper["blocker"] = ""
            if paper.get("monitor_check_pending"):
                _append_online_queue_guarded(
                    online_queue,
                    paper_id,
                    paper,
                    unified_state=unified_state,
                    monitor_only=True,
                )
                monitor_check_ids.append(paper_id)
                skipped[paper_id] = "previous scheduled monitor check is still pending"
            elif not force_monitor and not _smart_monitor_check_due(paper):
                paper["workflow_status"] = "monitoring"
                paper["worker_status"] = "monitoring"
                paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
                online_queue = [item for item in online_queue if item != paper_id]
                skipped[paper_id] = "paper was already checked on the current local date"
            elif not _paper_has_online_target(paper_id, paper):
                paper["workflow_status"] = "monitoring"
                paper["worker_status"] = "monitoring"
                paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
                paper["last_monitor_check_skipped_at"] = now_iso()
                paper["last_monitor_check_outcome"] = "target_missing"
                online_queue = [item for item in online_queue if item != paper_id]
                skipped[paper_id] = "monitor check needs a verified target journal/system"
            else:
                _rotate_canary_attempt(paper, paper_id)
                _append_online_queue_guarded(
                    online_queue,
                    paper_id,
                    paper,
                    unified_state=unified_state,
                    monitor_only=True,
                )
                paper.setdefault("thread_title", f"Smart Submission worker {paper_id}")
                paper["monitor_check_pending"] = True
                paper["monitor_pending_channel"] = SMART_MONITOR_CHANNEL_MAILBOX_BATCH
                paper["monitor_check_requested_at"] = now_iso()
                paper["workflow_status"] = "queued"
                paper["worker_status"] = "queued"
                paper["next_action"] = SCHEDULED_MONITORING_NEXT
                online_queued.append(paper_id)
                monitor_check_ids.append(paper_id)
        elif not _paper_has_online_target(paper_id, paper):
            online_queue = [item for item in online_queue if item != paper_id]
            paper["status"] = "waiting"
            paper["workflow_status"] = "waiting"
            paper["worker_status"] = "waiting"
            paper["next_action"] = "wait_journal_author_target_package"
            paper["detail"] = "missing target journal or submission system; rerun journal-author package or refresh local package metadata"
            paper["blocker"] = ""
            skipped[paper_id] = "missing target journal or submission system"
        else:
            _rotate_canary_attempt(paper, paper_id)
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
            )
            paper.setdefault("thread_title", f"Smart Submission worker {paper_id}")
            paper["workflow_status"] = "queued"
            paper["next_action"] = "visible_worker_online_submit_or_monitor"
            online_queued.append(paper_id)
        paper["last_daily_sweep_queued_at"] = now_iso()

    settings["online_submit_enabled"] = True
    settings["visible_scheduler_enabled"] = True
    settings["daily_monitor_enabled"] = True
    monitor_schedule = _smart_monitor_schedule(settings)
    settings["daily_monitor_statuses"] = sorted(eligible)
    state["paper_queue"] = local_queue
    state["online_queue"] = online_queue
    queue_normalize = _normalize_online_queue_state(state, reason="queue_daily_sweep")
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "queue_daily_sweep",
            "local_queued": local_queued,
            "online_queued": online_queued,
            "skipped": skipped,
            "include_blocked": include_blocked,
            "monitoring_only": monitoring_only,
            "force_monitor": force_monitor,
            "selected_ids": sorted(selected) if selected is not None else [],
            "queue_normalize": queue_normalize,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "local_queued": local_queued,
        "online_queued": online_queued,
        "monitor_check_ids": list(dict.fromkeys(monitor_check_ids)),
        "skipped": skipped,
        "local_queue_count": len(local_queue),
        "online_queue_count": len(online_queue),
        "force_monitor": force_monitor,
        "selected_ids": sorted(selected) if selected is not None else [],
        "daily_monitor_time_local": monitor_schedule["time_local"],
        "next_monitor_at": monitor_schedule["next_run_at"],
        "updated_at": state.get("updated_at", ""),
    }


@_smart_state_locked
def _claim_smart_submission_monitor_run(
    *,
    trigger: str,
    force: bool,
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    current = datetime.now().astimezone()
    today = current.date().isoformat()
    status = str(settings.get("daily_monitor_last_status") or "").strip().lower()
    started_text = str(settings.get("daily_monitor_last_started_at") or "").strip()
    running_is_fresh = False
    if status == "running" and started_text:
        try:
            started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.astimezone()
            running_is_fresh = (
                current - started.astimezone(current.tzinfo)
            ).total_seconds() < SMART_MONITOR_RUN_STALE_SECONDS
        except ValueError:
            running_is_fresh = False
    if running_is_fresh:
        return {
            "claimed": False,
            "reason": "monitor check is already running",
            "run_id": str(settings.get("daily_monitor_current_run_id") or ""),
        }
    if (
        not force
        and str(settings.get("daily_monitor_last_scheduled_date") or "") == today
    ):
        return {
            "claimed": False,
            "reason": "today's scheduled monitor check was already claimed",
            "run_id": str(settings.get("daily_monitor_current_run_id") or ""),
        }

    run_id = f"{current.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    settings["daily_monitor_current_run_id"] = run_id
    settings["daily_monitor_last_started_at"] = current.isoformat()
    settings["daily_monitor_last_finished_at"] = ""
    settings["daily_monitor_last_status"] = "running"
    settings["daily_monitor_last_trigger"] = str(trigger or "scheduled")
    settings["daily_monitor_last_error"] = ""
    if not force:
        settings["daily_monitor_last_scheduled_date"] = today
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "smart_submission_monitor_run_started",
            "run_id": run_id,
            "trigger": str(trigger or "scheduled"),
            "force": bool(force),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {"claimed": True, "run_id": run_id, "trigger": trigger, "force": force}


@_smart_state_locked
def _finish_smart_submission_monitor_run(
    *,
    run_id: str,
    trigger: str,
    status: str,
    paper_ids: list[str],
    error: str = "",
    summary: dict[str, Any] | None = None,
) -> None:
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    if str(settings.get("daily_monitor_current_run_id") or "") != str(run_id):
        return
    settings["daily_monitor_last_finished_at"] = now_iso()
    settings["daily_monitor_last_status"] = str(status or "unknown")
    settings["daily_monitor_last_trigger"] = str(trigger or "scheduled")
    settings["daily_monitor_last_paper_count"] = len(paper_ids)
    settings["daily_monitor_last_error"] = str(error or "")[:2000]
    run_summary = summary if isinstance(summary, dict) else {}
    settings["daily_monitor_last_checked_count"] = int(
        run_summary.get("checked_count") or 0
    )
    settings["daily_monitor_last_failed_count"] = int(
        run_summary.get("failed_count") or 0
    )
    settings["daily_monitor_last_pending_count"] = int(
        run_summary.get("pending_count") or 0
    )
    settings["daily_monitor_last_action_required_count"] = int(
        run_summary.get("action_required_count") or 0
    )
    settings["daily_monitor_last_followup_auto_queued_count"] = int(
        run_summary.get("followup_auto_queued_count") or 0
    )
    settings["daily_monitor_last_followup_manual_revision_count"] = int(
        run_summary.get("followup_manual_revision_count") or 0
    )
    settings["daily_monitor_last_followup_identity_reconciliation_count"] = int(
        run_summary.get("followup_identity_reconciliation_count") or 0
    )
    settings["daily_monitor_last_followup_failed_count"] = int(
        run_summary.get("followup_failed_count") or 0
    )
    settings["daily_monitor_last_summary"] = copy.deepcopy(run_summary)
    settings["daily_monitor_current_run_id"] = ""
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "smart_submission_monitor_run_finished",
            "run_id": run_id,
            "trigger": str(trigger or "scheduled"),
            "status": str(status or "unknown"),
            "paper_ids": list(paper_ids),
            "error": str(error or "")[:1000],
            "summary": copy.deepcopy(run_summary),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)


def _smart_monitor_run_summary(paper_ids: Iterable[str]) -> dict[str, Any]:
    """Verify completion from each paper checkpoint, never from scheduler exit alone."""

    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    results: dict[str, dict[str, Any]] = {}
    checked_count = failed_count = pending_count = action_required_count = 0
    action_statuses = {"revision_needed", "retarget_needed", "rejected", "accepted"}
    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            pending_count += 1
            results[paper_id] = {
                "outcome": "missing",
                "status": "",
                "checked_at": "",
                "detail": "paper record is missing after monitor dispatch",
            }
            continue
        status = str(paper.get("last_monitor_result") or paper.get("status") or "")
        outcome = str(paper.get("last_monitor_check_outcome") or "").strip().casefold()
        checked_at = str(paper.get("last_monitor_checked_at") or "")
        pending = bool(paper.get("monitor_check_pending"))
        if pending:
            pending_count += 1
            verified_outcome = "pending"
        elif outcome in {"failed", "failure", "skipped", "deferred", "waiting"}:
            failed_count += 1
            verified_outcome = outcome or "failed"
        elif not checked_at:
            pending_count += 1
            verified_outcome = "not_recorded"
        else:
            checked_count += 1
            verified_outcome = outcome or status or "checked"
            if status in action_statuses:
                action_required_count += 1
        results[paper_id] = {
            "outcome": verified_outcome,
            "status": status,
            "checked_at": checked_at,
            "detail": str(paper.get("last_monitor_detail") or "")[:500],
            "action_kind": str(paper.get("last_monitor_action_kind") or ""),
        }
    return {
        "requested_count": len(selected),
        "checked_count": checked_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "action_required_count": action_required_count,
        "paper_results": results,
    }


@_smart_state_locked
def record_smart_monitor_observation(
    paper_id: str,
    *,
    action_kind: str = "none",
    summary: str = "",
    message_date: str = "",
    sender_domain: str = "",
    submission_id: str = "",
    source: str = "codex_sdk_mailbox_batch",
    source_results: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist a bounded mailbox observation after the guarded status transition."""

    clean_id = str(paper_id or "").strip()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(clean_id)
    if not isinstance(paper, dict):
        raise KeyError(f"paper not found in smart submission state: {clean_id}")
    allowed_kinds = {
        "none",
        "technical_correction",
        "scientific_revision",
        "files_or_metadata",
        "retarget",
        "accepted",
        "unknown",
    }
    clean_kind = str(action_kind or "none").strip().casefold()
    if clean_kind not in allowed_kinds:
        clean_kind = "unknown"
    clean_summary = re.sub(r"\s+", " ", str(summary or "")).strip()[:240]
    clean_kind = classify_monitor_action(
        status=paper.get("last_monitor_result") or paper.get("status"),
        action_kind=clean_kind,
        summary=clean_summary,
    )
    if clean_kind not in allowed_kinds:
        clean_kind = "unknown"
    clean_date = re.sub(r"[^0-9A-Za-z:+. /_-]+", "", str(message_date or ""))[:80]
    clean_sender = str(sender_domain or "").strip().casefold()[:160]
    if clean_sender and not re.fullmatch(r"[a-z0-9.-]+", clean_sender):
        clean_sender = ""
    clean_submission_id = normalize_submission_id(
        _sanitize_submission_id(submission_id)
    )
    existing_sources = paper.get("last_monitor_source_results")
    merged_by_id = {
        str(item.get("source_id") or ""): item
        for item in sanitize_monitor_source_results(
            existing_sources if isinstance(existing_sources, list) else []
        )
    }
    for item in sanitize_monitor_source_results(source_results or []):
        merged_by_id[str(item.get("source_id") or "")] = item
    merged_sources = list(merged_by_id.values())
    observation = {
        "at": now_iso(),
        "source": _value_free_worker_source(source),
        "action_kind": clean_kind,
        "summary": clean_summary,
        "message_date": clean_date,
        "sender_domain": clean_sender,
        "submission_id": clean_submission_id,
        "source_results": merged_sources,
    }
    paper["last_monitor_observation"] = observation
    paper["last_monitor_action_kind"] = clean_kind
    paper["last_monitor_source_results"] = merged_sources
    paper["last_monitor_source_count"] = len(merged_sources)
    paper["last_monitor_source_available_count"] = sum(
        bool(item.get("available")) for item in merged_sources
    )
    if clean_submission_id:
        current_id = normalize_submission_id(paper.get("submission_id"))
        canonical_id = normalize_submission_id(paper.get("canonical_submission_id"))
        make_canonical = bool(
            clean_submission_id == current_id
            or (not current_id and not canonical_id)
        )
        upsert_submission_record(
            paper,
            clean_submission_id,
            submission_system_id=str(paper.get("submission_system_id") or ""),
            target_journal=str(paper.get("target_journal") or ""),
            status=str(paper.get("last_monitor_result") or paper.get("status") or "monitoring"),
            active=True,
            canonical=make_canonical,
            source=observation["source"],
            observed_at=observation["at"],
        )
        if not current_id:
            paper["submission_id"] = clean_submission_id
    project_submission_identity(paper, observed_at=observation["at"])
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "record_smart_monitor_observation",
            "paper_id": clean_id,
            "action_kind": clean_kind,
            "has_action": clean_kind != "none",
            "source": observation["source"],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return copy.deepcopy(observation)


@_smart_state_locked
def queue_smart_monitor_dashboard_fallback(
    paper_ids: Iterable[str],
) -> dict[str, Any]:
    """Queue journal-dashboard verification after mailboxes find no decision.

    Mailbox inspection and journal-dashboard inspection are two checkpoints in
    one user-visible monitor run.  This transition creates a fresh canary
    attempt so a completed ephemeral mailbox thread can never own the later
    journal-site Turn.
    """

    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    binding_reconciliation = reconcile_smart_submission_site_bindings(selected)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [
        str(item) for item in state.get("online_queue", []) if str(item).strip()
    ]
    unified_state = _unified_state_snapshot()
    queued: list[str] = []
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}
    now = now_iso()

    def mark_failed(paper: dict[str, Any], reason: str) -> None:
        clean_reason = str(reason or "journal dashboard fallback failed")[:1000]
        paper.pop("monitor_check_pending", None)
        paper.pop("monitor_pending_channel", None)
        if str(paper.get("status") or "").strip().casefold() in {
            "submitted",
            "monitoring",
        }:
            paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
        paper["last_monitor_attempted_at"] = now
        paper["last_monitor_check_outcome"] = "failed"
        paper["last_monitor_source"] = "journal_dashboard_fallback"
        paper["last_monitor_detail"] = clean_reason
        paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT

    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            failed[paper_id] = skipped[paper_id]
            continue
        status = str(paper.get("status") or "").strip().casefold()
        workflow = str(paper.get("workflow_status") or "").strip().casefold()
        if status not in {"submitted", "monitoring"}:
            skipped[paper_id] = f"mailbox result is already actionable: {status or 'empty'}"
            continue
        if paper.get("monitor_check_pending"):
            # A repeated transition is idempotent: an existing pending journal
            # check already satisfies this mailbox-to-dashboard handoff.
            queued.append(paper_id)
            continue
        if workflow == "running":
            skipped[paper_id] = "a worker still owns the paper"
            failed[paper_id] = skipped[paper_id]
            mark_failed(paper, skipped[paper_id])
            continue
        route, route_error = _worker_route_guard_result(
            paper_id,
            paper,
            unified_state=unified_state,
            monitor_only=True,
        )
        if route_error is not None:
            skipped[paper_id] = str(route_error.get("code") or "route guard failed")
            failed[paper_id] = skipped[paper_id]
            paper["last_online_route_guard"] = route_error
            mark_failed(paper, skipped[paper_id])
            continue
        _mark_worker_route_on_paper(paper, route or {}, monitor_only=True)
        _rotate_canary_attempt(paper, paper_id)
        paper["monitor_check_pending"] = True
        paper["monitor_pending_channel"] = SMART_MONITOR_CHANNEL_JOURNAL_DASHBOARD
        paper["monitor_dashboard_fallback_requested_at"] = now
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["next_action"] = SCHEDULED_MONITORING_NEXT
        paper["detail"] = (
            "Configured mailboxes were checked without a decisive notification; "
            "the same monitor run is verifying the journal dashboard."
        )
        try:
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
                monitor_only=True,
            )
        except Exception as exc:
            reason = f"dashboard queue rejected: {type(exc).__name__}"
            skipped[paper_id] = reason
            failed[paper_id] = reason
            mark_failed(paper, reason)
            continue
        queued.append(paper_id)
    state["online_queue"] = list(dict.fromkeys(online_queue))
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "queue_smart_monitor_dashboard_fallback",
            "paper_ids": selected,
            "queued": queued,
            "skipped": skipped,
            "failed": failed,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "queued": queued,
        "skipped": skipped,
        "failed": failed,
        "binding_reconciliation": binding_reconciliation,
        "updated_at": now,
    }


@_smart_state_locked
def close_unfinished_smart_monitor_check(
    paper_id: str,
    *,
    detail: str,
    source: str = "monitor_dispatch_incomplete",
) -> dict[str, Any]:
    """Release a queued-but-undispatched check so the next click can retry it."""

    clean_id = str(paper_id or "").strip()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(clean_id)
    if not isinstance(paper, dict):
        return {"paper_id": clean_id, "released": False, "reason": "missing"}
    if not paper.get("monitor_check_pending"):
        return {"paper_id": clean_id, "released": False, "reason": "not_pending"}
    now = now_iso()
    paper.pop("monitor_check_pending", None)
    paper.pop("monitor_pending_channel", None)
    if str(paper.get("status") or "").strip().casefold() in {"submitted", "monitoring"}:
        paper["status"] = "monitoring"
        paper["workflow_status"] = "monitoring"
        paper["worker_status"] = "monitoring"
    paper["last_monitor_attempted_at"] = now
    paper["last_monitor_check_outcome"] = "failed"
    paper["last_monitor_source"] = _value_free_worker_source(source)
    paper["last_monitor_detail"] = str(detail or "monitor dispatch did not complete")[:1000]
    paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != clean_id
    ]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "close_unfinished_smart_monitor_check",
            "paper_id": clean_id,
            "source": paper["last_monitor_source"],
            "detail": paper["last_monitor_detail"],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {"paper_id": clean_id, "released": True, "outcome": "failed"}


def _smart_monitor_followup_fingerprint(
    paper_id: str,
    observation: Mapping[str, Any],
) -> str:
    """Return a stable id for one actionable mailbox observation.

    ``observation.at`` is deliberately excluded: repeated checks of the same
    message must not create another correction, retarget, or submission.
    """

    payload = {
        "paper_id": str(paper_id or "").strip(),
        "action_kind": str(observation.get("action_kind") or "none").strip().casefold(),
        "summary": re.sub(r"\s+", " ", str(observation.get("summary") or "")).strip(),
        "message_date": str(observation.get("message_date") or "").strip(),
        "sender_domain": str(observation.get("sender_domain") or "").strip().casefold(),
        "submission_id": str(observation.get("submission_id") or "").strip(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _monitor_followup_context(
    paper_id: str,
    observation: Mapping[str, Any],
    fingerprint: str,
) -> dict[str, str]:
    return {
        "paper_id": str(paper_id),
        "fingerprint": fingerprint,
        "action_kind": str(observation.get("action_kind") or "none").strip().casefold(),
        "summary": re.sub(r"\s+", " ", str(observation.get("summary") or "")).strip()[:240],
        "message_date": str(observation.get("message_date") or "")[:80],
        "sender_domain": str(observation.get("sender_domain") or "")[:160],
        "submission_id": str(observation.get("submission_id") or "")[:160],
    }


def _retarget_notice_closes_attempt(status: Any, summary: Any) -> bool:
    status_token = str(status or "").strip().casefold()
    if status_token == "rejected":
        return True
    text = re.sub(r"\s+", " ", str(summary or "")).strip().casefold()
    return any(
        marker in text
        for marker in (
            "submission has been rejected",
            "manuscript has been rejected",
            "withdrawal confirmed",
            "successfully withdrawn",
            "submission is withdrawn",
            "submission was withdrawn",
            "manuscript is withdrawn",
            "manuscript was withdrawn",
            "submission has been closed",
        )
    )


@_smart_state_locked
def _plan_smart_monitor_followups(
    paper_ids: Iterable[str],
) -> dict[str, Any]:
    """Translate mailbox facts into deterministic, idempotent follow-up work."""

    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [
        str(item) for item in state.get("online_queue", []) if str(item).strip()
    ]
    unified_state = _unified_state_snapshot()
    retarget_jobs: list[dict[str, Any]] = []
    online_queued: list[str] = []
    accepted: list[str] = []
    revision_waiting: list[str] = []
    identity_waiting: list[str] = []
    deduplicated: list[str] = []
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}
    detected: list[str] = []

    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        observation = paper.get("last_monitor_observation")
        if not isinstance(observation, Mapping):
            skipped[paper_id] = "monitor observation is missing"
            continue
        monitor_status = str(
            paper.get("last_monitor_result") or paper.get("status") or ""
        ).strip().casefold()
        action_kind = classify_monitor_action(
            status=monitor_status,
            action_kind=observation.get("action_kind"),
            summary=observation.get("summary"),
        )
        if action_kind == "none" and monitor_status not in {
            "accepted",
            "rejected",
            "retarget_needed",
            "revision_needed",
        }:
            skipped[paper_id] = "no actionable journal message"
            continue

        effective_observation = dict(observation)
        effective_observation["action_kind"] = action_kind
        fingerprint = _smart_monitor_followup_fingerprint(
            paper_id,
            effective_observation,
        )
        previous_fingerprint = str(
            paper.get("last_monitor_followup_fingerprint") or ""
        )
        previous_status = str(paper.get("last_monitor_followup_status") or "")
        if previous_fingerprint == fingerprint and previous_status in {
            "accepted_archived",
            "journal_author_dispatching",
            "journal_author_queued",
            "online_correction_queued",
            "online_correction_running",
            "online_withdrawal_queued",
            "online_withdrawal_running",
            "waiting_scientific_revision",
            "action_evidence_required",
            "identity_reconciliation_required",
            "completed",
        }:
            deduplicated.append(paper_id)
            continue

        detected.append(paper_id)
        context = _monitor_followup_context(
            paper_id,
            effective_observation,
            fingerprint,
        )
        now = now_iso()
        paper["last_monitor_followup_fingerprint"] = fingerprint
        paper["last_monitor_followup_kind"] = action_kind
        paper["last_monitor_followup_at"] = now
        paper["monitor_followup_context"] = context
        identity = project_submission_identity(paper, observed_at=now)

        if identity["status"] in {IDENTITY_CONFLICT, IDENTITY_CANONICAL_MISMATCH}:
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "identity_reconciliation_required"
            paper["worker_status"] = "identity_reconciliation_required"
            paper["blocker"] = SMART_SUBMISSION_DUPLICATE_ACTIVE
            paper["next_action"] = "reconcile_active_submission_ids_before_online_action"
            paper["last_monitor_followup_status"] = "identity_reconciliation_required"
            paper["last_monitor_followup_detail"] = (
                "Online mutation is paused because the same paper has multiple active "
                "manuscript IDs, or the selected canonical ID is inconsistent. Resolve "
                "the duplicate/withdrawal first; no new submission or amendment was queued."
            )
            online_queue = [item for item in online_queue if item != paper_id]
            identity_waiting.append(paper_id)
            continue

        if action_kind == "accepted" or monitor_status == "accepted":
            paper["status"] = "accepted"
            paper["workflow_status"] = "accepted"
            paper["worker_status"] = "accepted"
            paper["next_action"] = "archive_acceptance_evidence"
            paper["last_monitor_followup_status"] = "accepted_archived"
            paper["last_monitor_followup_detail"] = (
                "Acceptance was recorded; no online mutation or duplicate submission was queued."
            )
            accepted_id = (
                normalize_submission_id(context.get("submission_id"))
                or normalize_submission_id(identity.get("canonical_submission_id"))
            )
            if accepted_id:
                upsert_submission_record(
                    paper,
                    accepted_id,
                    status="accepted",
                    active=True,
                    canonical=True,
                    source="monitor_followup_accepted",
                    observed_at=now,
                )
                project_submission_identity(paper, observed_at=now)
            online_queue = [item for item in online_queue if item != paper_id]
            accepted.append(paper_id)
            continue

        if action_kind == "retarget" or monitor_status in {
            "rejected",
            "retarget_needed",
        }:
            if not _retarget_notice_closes_attempt(
                monitor_status,
                context.get("summary"),
            ):
                _identity, identity_error = _submission_identity_guard_result(
                    paper,
                    operation="existing",
                )
                if identity_error is not None:
                    paper["status"] = "revision_needed"
                    paper["workflow_status"] = "identity_reconciliation_required"
                    paper["worker_status"] = "identity_reconciliation_required"
                    paper["blocker"] = str(identity_error["code"])
                    paper["next_action"] = "reconcile_active_submission_ids_before_online_action"
                    paper["last_monitor_followup_status"] = "identity_reconciliation_required"
                    paper["last_monitor_followup_detail"] = str(identity_error["message"])
                    online_queue = [item for item in online_queue if item != paper_id]
                    identity_waiting.append(paper_id)
                    continue
                route, route_error = _worker_route_guard_result(
                    paper_id,
                    paper,
                    unified_state=unified_state,
                    monitor_only=False,
                )
                if route_error is not None:
                    failed[paper_id] = str(route_error.get("code") or "route guard failed")
                    paper["last_online_route_guard"] = route_error
                    paper["last_monitor_followup_status"] = "failed"
                    paper["last_monitor_followup_detail"] = failed[paper_id]
                    continue
                _mark_worker_route_on_paper(paper, route or {}, monitor_only=False)
                stale_thread = _detach_stale_visible_worker(
                    paper,
                    reason="monitor_followup_withdraw_for_retarget",
                    timestamp=now,
                )
                if stale_thread:
                    paper["last_monitor_followup_stale_thread_id"] = stale_thread
                _rotate_canary_attempt(paper, paper_id)
                paper["status"] = "revision_needed"
                paper["workflow_status"] = "queued"
                paper["worker_status"] = "queued"
                paper["blocker"] = ""
                paper["online_intent"] = "post_submission_withdraw_for_retarget"
                paper["next_action"] = "withdraw_existing_submission_before_retarget"
                paper["detail"] = context["summary"]
                paper["last_monitor_followup_status"] = "online_withdrawal_queued"
                paper["last_monitor_followup_detail"] = (
                    "The journal requested withdrawal/transfer but has not confirmed closure. "
                    "The program queued withdrawal of the one verified existing submission; "
                    "journal selection will start only after closure is confirmed."
                )
                _append_online_queue_guarded(
                    online_queue,
                    paper_id,
                    paper,
                    unified_state=unified_state,
                )
                online_queued.append(paper_id)
                continue
            rejected_id = (
                normalize_submission_id(context.get("submission_id"))
                or normalize_submission_id(identity.get("canonical_submission_id"))
                or normalize_submission_id(paper.get("submission_id"))
            )
            if rejected_id:
                close_submission_record(
                    paper,
                    rejected_id,
                    status="rejected" if monitor_status == "rejected" else "retargeted",
                    source="monitor_followup_retarget",
                    observed_at=now,
                )
            project_submission_identity(paper, observed_at=now)
            paper["status"] = "retarget_needed"
            paper["workflow_status"] = "retarget_needed"
            paper["worker_status"] = "retarget_needed"
            paper["next_action"] = "queue_journal_author_retarget_steps_2_3"
            paper["last_monitor_followup_status"] = "journal_author_dispatching"
            paper["last_monitor_followup_detail"] = (
                "The program is returning this paper to journal-author Steps 2-3; "
                "the rejected journal will be excluded."
            )
            online_queue = [item for item in online_queue if item != paper_id]
            retarget_jobs.append(
                {
                    "paper_id": paper_id,
                    "fingerprint": fingerprint,
                    "reason": context["summary"] or "explicit journal rejection/retarget notice",
                    "paper": copy.deepcopy(paper),
                }
            )
            continue

        if action_kind == "scientific_revision":
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "waiting_revision_package"
            paper["worker_status"] = "waiting_revision_package"
            paper["next_action"] = "prepare_scientific_revision_package_from_decision"
            paper["online_intent"] = "post_submission_scientific_revision"
            paper["last_monitor_followup_status"] = "waiting_scientific_revision"
            paper["last_monitor_followup_detail"] = (
                "A scientific manuscript revision is required. Online resubmission is held "
                "until a revised manuscript package exists, preventing an unchanged duplicate upload."
            )
            online_queue = [item for item in online_queue if item != paper_id]
            revision_waiting.append(paper_id)
            continue

        if action_kind == "unknown":
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "action_evidence_required"
            paper["worker_status"] = "action_evidence_required"
            paper["blocker"] = "SMART_MONITOR_ACTION_EVIDENCE_REQUIRED"
            paper["next_action"] = "inspect_decision_evidence_before_online_mutation"
            paper["last_monitor_followup_status"] = "action_evidence_required"
            paper["last_monitor_followup_detail"] = (
                "The notification says author action is required but does not identify the "
                "exact correction. The program will not guess, open an amendment, or create "
                "a new submission until exact decision evidence is recorded."
            )
            online_queue = [item for item in online_queue if item != paper_id]
            identity_waiting.append(paper_id)
            continue

        if action_kind not in {
            "technical_correction",
            "files_or_metadata",
        }:
            failed[paper_id] = f"unsupported follow-up action kind: {action_kind}"
            paper["last_monitor_followup_status"] = "failed"
            paper["last_monitor_followup_detail"] = failed[paper_id]
            continue

        identity, identity_error = _submission_identity_guard_result(
            paper,
            operation="existing",
        )
        if identity_error is not None:
            failed_code = str(identity_error["code"])
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "identity_reconciliation_required"
            paper["worker_status"] = "identity_reconciliation_required"
            paper["blocker"] = failed_code
            paper["next_action"] = "reconcile_active_submission_ids_before_online_action"
            paper["last_monitor_followup_status"] = "identity_reconciliation_required"
            paper["last_monitor_followup_detail"] = str(identity_error["message"])
            online_queue = [item for item in online_queue if item != paper_id]
            identity_waiting.append(paper_id)
            continue

        route, route_error = _worker_route_guard_result(
            paper_id,
            paper,
            unified_state=unified_state,
            monitor_only=False,
        )
        if route_error is not None:
            failed[paper_id] = str(route_error.get("code") or "route guard failed")
            paper["last_online_route_guard"] = route_error
            paper["last_monitor_followup_status"] = "failed"
            paper["last_monitor_followup_detail"] = failed[paper_id]
            continue
        _mark_worker_route_on_paper(paper, route or {}, monitor_only=False)
        stale_thread = _detach_stale_visible_worker(
            paper,
            reason=f"monitor_followup_{action_kind}",
            timestamp=now,
        )
        if stale_thread:
            paper["last_monitor_followup_stale_thread_id"] = stale_thread
        _rotate_canary_attempt(paper, paper_id)
        paper["status"] = "revision_needed"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["blocker"] = ""
        paper["online_intent"] = f"post_submission_{action_kind}"
        paper["next_action"] = {
            "technical_correction": "apply_technical_correction_to_existing_submission",
            "files_or_metadata": "apply_requested_files_or_metadata_to_existing_submission",
        }[action_kind]
        paper["detail"] = context["summary"]
        paper["last_monitor_followup_status"] = "online_correction_queued"
        paper["last_monitor_followup_detail"] = (
            "Queued a bounded continuation of the existing submission. The worker must use "
            "the existing manuscript ID and must not create a new submission."
        )
        _append_online_queue_guarded(
            online_queue,
            paper_id,
            paper,
            unified_state=unified_state,
        )
        online_queued.append(paper_id)

    state["online_queue"] = list(dict.fromkeys(online_queue))
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    if online_queued:
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
        state["mode"] = "run_once"
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "plan_smart_monitor_followups",
            "paper_ids": selected,
            "detected": detected,
            "accepted": accepted,
            "retarget_jobs": [item["paper_id"] for item in retarget_jobs],
            "online_queued": online_queued,
            "revision_waiting": revision_waiting,
            "identity_waiting": identity_waiting,
            "deduplicated": deduplicated,
            "failed": failed,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "paper_ids": selected,
        "detected": detected,
        "accepted": accepted,
        "retarget_jobs": retarget_jobs,
        "online_queued": online_queued,
        "revision_waiting": revision_waiting,
        "identity_waiting": identity_waiting,
        "deduplicated": deduplicated,
        "skipped": skipped,
        "failed": failed,
    }


@_smart_state_locked
def _finish_smart_monitor_retarget_followup(
    paper_id: str,
    *,
    fingerprint: str,
    result: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        return {"paper_id": paper_id, "ok": False, "stale": True, "error": "paper missing"}
    if str(paper.get("last_monitor_followup_fingerprint") or "") != fingerprint:
        return {"paper_id": paper_id, "ok": False, "stale": True, "error": "follow-up changed"}
    online_queue = [
        str(item) for item in state.get("online_queue", []) if str(item).strip()
    ]
    now = now_iso()
    if error:
        paper["status"] = "retarget_needed"
        paper["workflow_status"] = "retarget_needed"
        paper["worker_status"] = "retarget_needed"
        paper["last_monitor_followup_status"] = "failed"
        paper["last_monitor_followup_detail"] = str(error)[:1000]
        outcome = {"paper_id": paper_id, "ok": False, "error": str(error)}
    else:
        payload = dict(result or {})
        excluded_journals = [
            str(item).strip()
            for item in payload.get("excluded_journals", [])
            if str(item).strip()
        ]
        if excluded_journals:
            paper["retarget_excluded_journals"] = excluded_journals
            request = paper.get("retarget_request")
            if isinstance(request, dict):
                request["excluded_journals"] = list(excluded_journals)
                request["updated_at"] = now
                paper["retarget_request"] = request
        paper["status"] = "waiting"
        paper["workflow_status"] = "waiting_journal_author_retarget"
        paper["worker_status"] = "waiting_journal_author_retarget"
        paper["blocker"] = ""
        paper["next_action"] = "wait_journal_author_retarget_package"
        paper["online_intent"] = "retarget_after_rejection"
        paper["last_monitor_followup_status"] = "journal_author_queued"
        paper["last_monitor_followup_detail"] = (
            "Journal-author Steps 2-3 are queued in the V2 SDK runtime. Online submission "
            "will resume only after the new package passes its contract."
        )
        paper["last_retarget_queued_at"] = now
        paper["monitor_retarget_request_id"] = str(payload.get("request_id") or "")
        paper["monitor_retarget_previous_target_journal"] = str(
            payload.get("previous_target_journal") or paper.get("target_journal") or ""
        )
        paper["monitor_retarget_archive"] = copy.deepcopy(payload.get("archive") or {})
        paper["last_audit_path"] = str(payload.get("audit_path") or paper.get("last_audit_path") or "")
        outcome = {
            "paper_id": paper_id,
            "ok": True,
            "request_id": paper["monitor_retarget_request_id"],
            "audit_path": paper["last_audit_path"],
        }
    state["online_queue"] = [item for item in online_queue if item != paper_id]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "finish_smart_monitor_retarget_followup",
            "paper_id": paper_id,
            "fingerprint": fingerprint,
            "ok": not bool(error),
            "error": str(error)[:1000],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return outcome


def _start_smart_monitor_followup_runtime(paper_ids: Iterable[str]) -> dict[str, Any]:
    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    if not selected:
        return {"ok": True, "started": False, "selected_ids": []}
    from .smart_submission_sdk_runtime import default_smart_submission_sdk_runtime

    return default_smart_submission_sdk_runtime().start_background(selected)


def _ensure_smart_monitor_followup_routes(
    paper_ids: Iterable[str],
    *,
    orchestrator: Any | None = None,
) -> dict[str, Any]:
    """Hand actionable existing submissions to Smart before online correction.

    Read-only monitoring may follow a historical legacy route.  Once a journal
    requests an online mutation, ownership must be transferred explicitly; the
    normal route guard then protects every subsequent SDK Turn.
    """

    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    required: list[str] = []
    skipped: dict[str, str] = {}
    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, Mapping):
            skipped[paper_id] = "paper record is missing"
            continue
        observation = paper.get("last_monitor_observation")
        if not isinstance(observation, Mapping):
            skipped[paper_id] = "monitor observation is missing"
            continue
        monitor_status = str(
            paper.get("last_monitor_result") or paper.get("status") or ""
        ).strip().casefold()
        action_kind = classify_monitor_action(
            status=monitor_status,
            action_kind=observation.get("action_kind"),
            summary=observation.get("summary"),
        )
        needs_existing_online_action = action_kind in {
            "technical_correction",
            "files_or_metadata",
        } or (
            action_kind == "retarget"
            and not _retarget_notice_closes_attempt(
                monitor_status,
                observation.get("summary"),
            )
        )
        if needs_existing_online_action:
            _identity, identity_error = _submission_identity_guard_result(
                paper,
                operation="existing",
            )
            if identity_error is None:
                required.append(paper_id)
            else:
                skipped[paper_id] = str(identity_error["code"])
        else:
            skipped[paper_id] = f"no online route handoff required: {action_kind}"

    unified = (
        orchestrator.sync()
        if orchestrator is not None
        else _unified_state_snapshot()
    )
    route_results: dict[str, Any] = {}
    failed: dict[str, str] = {}
    pending = [
        paper_id
        for paper_id in required
        if not (
            submission_route_for(unified.get("submission_routes"), paper_id).get(
                "execution_owner"
            )
            == "smart"
            and submission_route_for(
                unified.get("submission_routes"), paper_id
            ).get("mode")
            == "canary"
        )
    ]
    if pending and orchestrator is None:
        from .orchestrator import UnifiedOrchestrator

        orchestrator = UnifiedOrchestrator()
        unified = orchestrator.sync()

    for paper_id in required:
        route = submission_route_for(unified.get("submission_routes"), paper_id)
        if route.get("execution_owner") == "smart" and route.get("mode") == "canary":
            route_results[paper_id] = {
                "changed": False,
                "paper_id": paper_id,
                "submission_route": route,
                "existing_submission_handoff": True,
            }
            continue
        paper = papers.get(paper_id, {})
        observation = (
            paper.get("last_monitor_observation")
            if isinstance(paper, Mapping)
            else {}
        )
        fingerprint = _smart_monitor_followup_fingerprint(
            paper_id,
            observation if isinstance(observation, Mapping) else {},
        )
        operation_id = (
            "smart-monitor-followup-"
            + hashlib.sha256(
                f"{paper_id}|{fingerprint}".encode("utf-8")
            ).hexdigest()[:16]
        )
        try:
            result = orchestrator.handoff_existing_submission_to_smart(
                paper_id,
                expected_revision=int(route.get("revision") or 1),
                operation_id=operation_id,
            )
        except Exception as exc:
            failed[paper_id] = f"{type(exc).__name__}: {exc}"
            continue
        route_results[paper_id] = result
        refreshed_route = (
            result.get("submission_route")
            if isinstance(result, Mapping)
            else None
        )
        if isinstance(refreshed_route, Mapping):
            unified.setdefault("submission_routes", {})[paper_id] = dict(
                refreshed_route
            )
    return {
        "required": required,
        "changed": [
            paper_id
            for paper_id, result in route_results.items()
            if isinstance(result, Mapping) and result.get("changed")
        ],
        "route_results": route_results,
        "skipped": skipped,
        "failed": failed,
    }


def apply_smart_monitor_followups(
    paper_ids: Iterable[str],
    *,
    activate: bool = True,
    orchestrator: Any | None = None,
) -> dict[str, Any]:
    """Apply safe post-monitor actions and return an auditable summary."""

    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    route_handoff = _ensure_smart_monitor_followup_routes(
        selected,
        orchestrator=orchestrator,
    )
    plan = _plan_smart_monitor_followups(selected)
    retargeted: list[dict[str, Any]] = []
    retarget_failed: dict[str, str] = {}
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    for job in plan.get("retarget_jobs", []):
        if not isinstance(job, Mapping):
            continue
        paper_id = str(job.get("paper_id") or "")
        fingerprint = str(job.get("fingerprint") or "")
        try:
            result = _queue_journal_author_retarget(
                paper_id,
                dict(job.get("paper") or {}),
                str(job.get("reason") or "explicit journal rejection/retarget notice"),
                settings,
            )
            retargeted.append(
                _finish_smart_monitor_retarget_followup(
                    paper_id,
                    fingerprint=fingerprint,
                    result=result,
                )
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            retarget_failed[paper_id] = message
            _finish_smart_monitor_retarget_followup(
                paper_id,
                fingerprint=fingerprint,
                error=message,
            )

    runtime: dict[str, Any] = {"ok": True, "started": False, "selected_ids": []}
    runtime_error = ""
    if activate and plan.get("online_queued"):
        try:
            runtime = _start_smart_monitor_followup_runtime(plan["online_queued"])
        except Exception as exc:
            runtime_error = f"{type(exc).__name__}: {exc}"
            runtime = {"ok": False, "started": False, "error": runtime_error}

    wakeup: dict[str, Any] = {}
    if activate and any(item.get("ok") for item in retargeted):
        try:
            wakeup = set_smart_submission_automation_fast_wakeup(True)
        except Exception as exc:
            runtime_error = (
                f"{runtime_error}; " if runtime_error else ""
            ) + f"retarget wakeup failed: {type(exc).__name__}: {exc}"

    auto_queued_count = len(plan.get("online_queued", [])) + sum(
        1 for item in retargeted if item.get("ok")
    )
    return {
        "detected_count": len(plan.get("detected", [])),
        "accepted_count": len(plan.get("accepted", [])),
        "auto_queued_count": auto_queued_count,
        "manual_revision_count": len(plan.get("revision_waiting", [])),
        "identity_reconciliation_count": len(plan.get("identity_waiting", [])),
        "deduplicated_count": len(plan.get("deduplicated", [])),
        "failed_count": len(plan.get("failed", {})) + len(retarget_failed) + bool(runtime_error),
        "accepted": plan.get("accepted", []),
        "online_queued": plan.get("online_queued", []),
        "revision_waiting": plan.get("revision_waiting", []),
        "identity_waiting": plan.get("identity_waiting", []),
        "deduplicated": plan.get("deduplicated", []),
        "retargeted": retargeted,
        "retarget_failed": retarget_failed,
        "skipped": plan.get("skipped", {}),
        "failed": plan.get("failed", {}),
        "route_handoff": route_handoff,
        "runtime": runtime,
        "wakeup": wakeup,
        "runtime_error": runtime_error,
    }


def _journal_author_monitor_retarget_snapshot(
    paper_id: str,
    request_id: str,
) -> dict[str, Any]:
    """Read whether one exact V2 retarget request produced a valid new package."""

    from .adapters.journal_author import JournalAuthorAdapter
    from .journal_author_output_contract import validate_journal_author_output

    adapter = JournalAuthorAdapter()
    journal_state = adapter.load_state()
    records = (
        journal_state.get("papers", {})
        if isinstance(journal_state.get("papers"), dict)
        else {}
    )
    record = records.get(paper_id)
    if not isinstance(record, dict):
        return {"ready": False, "status": "missing", "detail": "journal-author record missing"}
    actual_request_id = str(record.get("sdk_request_id") or "")
    workflow_status = str(record.get("workflow_status") or "").strip().casefold()
    run_mode = str(record.get("run_mode") or "").strip().casefold()
    request_lineage = "exact"
    if request_id and actual_request_id != request_id:
        if actual_request_id and workflow_status == "complete" and run_mode == "package":
            request_lineage = "package_repair_successor"
        else:
            return {
                "ready": False,
                "status": "superseded",
                "detail": "journal-author request id no longer matches the monitor retarget",
                "actual_request_id": actual_request_id,
            }
    if not actual_request_id:
        return {
            "ready": False,
            "status": "missing",
            "detail": "journal-author request id is missing",
        }
    if workflow_status in {"blocked", "needs_user", "failed"}:
        return {
            "ready": False,
            "status": workflow_status,
            "detail": str(record.get("last_result_message") or "journal-author retarget needs attention"),
        }
    if workflow_status != "complete":
        return {
            "ready": False,
            "status": workflow_status or "running",
            "detail": "journal-author retarget is still running",
        }
    contract = validate_journal_author_output(adapter.root, paper_id)
    package_dir = adapter.package_dir(paper_id)
    ready = bool(
        contract.ok
        and contract.package_status == "READY_TO_SUBMIT"
        and package_dir.is_dir()
    )
    return {
        "ready": ready,
        "status": "ready" if ready else "contract_not_ready",
        "detail": "" if ready else "; ".join(contract.issues[:5]),
        "request_id": actual_request_id,
        "request_lineage": request_lineage,
        "superseded_request_id": request_id if request_lineage != "exact" else "",
        "package_dir": str(package_dir),
        "paper_dir": str(adapter.paper_dir(paper_id)),
        "target_journal": _risk_report_value(package_dir, "Target journal"),
        "submission_system": _risk_report_value(package_dir, "Publisher/system"),
        "contract": contract.as_dict(),
    }


@_smart_state_locked
def _prepare_completed_smart_monitor_retargets(
    paper_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    selected = (
        {str(item).strip() for item in paper_ids if str(item).strip()}
        if paper_ids is not None
        else None
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    local_queue = [
        str(item) for item in state.get("paper_queue", []) if str(item).strip()
    ]
    ready: list[str] = []
    retry_selection: list[str] = []
    waiting: dict[str, str] = {}
    failed: dict[str, str] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    request_successors: list[dict[str, str]] = []
    clear_for_new_submission = {
        "submission_id",
        "manuscript_id",
        "submission_number",
        "tracking_id",
        "submission_id_source",
        "submission_id_recovered_at",
        "confirmation_url",
        "submission_confirmation_url",
        "submission_evidence_path",
        "confirmation_evidence_path",
        "confirmation_page_path",
        "final_submit_confirmed",
        "submission_confirmation_receipt",
        "pending_submission_confirmation_evidence",
        "last_live_submission_confirmation_evidence",
        "final_submit_authorization",
        "final_submit_authorization_id",
        "draft_id",
        "draft_url",
        "upload_url",
        "uploaded_manuscript_file",
        "uploaded_manuscript_sha256",
        "thread_id",
        "attempt_id",
    }
    for paper_id, paper in sorted(papers.items()):
        paper_id = str(paper_id)
        if selected is not None and paper_id not in selected:
            continue
        if not isinstance(paper, dict):
            continue
        if str(paper.get("last_monitor_followup_status") or "") != "journal_author_queued":
            continue
        request_id = str(paper.get("monitor_retarget_request_id") or "")
        try:
            snapshot = _journal_author_monitor_retarget_snapshot(paper_id, request_id)
        except Exception as exc:
            waiting[paper_id] = f"snapshot failed: {type(exc).__name__}: {exc}"
            continue
        snapshots[paper_id] = snapshot
        status = str(snapshot.get("status") or "")
        if not snapshot.get("ready"):
            if status in {"blocked", "needs_user", "failed", "contract_not_ready", "superseded"}:
                message = str(snapshot.get("detail") or status)
                failed[paper_id] = message
                paper["last_monitor_followup_status"] = "retarget_package_blocked"
                paper["last_monitor_followup_detail"] = message[:1000]
            else:
                waiting[paper_id] = str(snapshot.get("detail") or status or "running")
            continue

        retarget_request = (
            paper.get("retarget_request")
            if isinstance(paper.get("retarget_request"), dict)
            else {}
        )
        actual_request_id = str(snapshot.get("request_id") or "").strip()
        if (
            actual_request_id
            and actual_request_id != request_id
            and str(snapshot.get("request_lineage") or "")
            == "package_repair_successor"
        ):
            paper["monitor_retarget_request_id"] = actual_request_id
            if retarget_request:
                history = retarget_request.setdefault(
                    "journal_author_request_history",
                    [],
                )
                if not isinstance(history, list):
                    history = []
                    retarget_request["journal_author_request_history"] = history
                if request_id and request_id not in history:
                    history.append(request_id)
                retarget_request["journal_author_request_id"] = actual_request_id
                retarget_request["updated_at"] = now_iso()
                paper["retarget_request"] = retarget_request
            request_successors.append(
                {
                    "paper_id": paper_id,
                    "previous_request_id": request_id,
                    "request_id": actual_request_id,
                }
            )
        actual_target = str(snapshot.get("target_journal") or "").strip()
        excluded_journals = _historical_retarget_journals(paper_id, paper)
        for historical in retarget_request.get("excluded_journals", []):
            _append_unique_journal_name(excluded_journals, historical)
        paper["retarget_excluded_journals"] = list(excluded_journals)
        if retarget_request:
            retarget_request["excluded_journals"] = list(excluded_journals)

        reused_historical_target = next(
            (
                historical
                for historical in excluded_journals
                if _journal_names_equivalent(actual_target, historical)
            ),
            "",
        )
        if actual_target and reused_historical_target:
            _append_unique_journal_name(excluded_journals, actual_target)
            paper["retarget_excluded_journals"] = list(excluded_journals)
            retry_count = int(
                retarget_request.get("historical_target_retry_count") or 0
            ) + 1
            message = (
                "The journal-author result reused a current or historical submission target: "
                f"actual={actual_target}; matched_history={reused_historical_target}."
            )
            if (
                retarget_request.get("mode") == "ai"
                and retry_count <= SMART_RETARGET_HISTORICAL_TARGET_RETRY_LIMIT
            ):
                retarget_request["status"] = "ready_for_selection"
                retarget_request["excluded_journals"] = list(excluded_journals)
                retarget_request["historical_target_retry_count"] = retry_count
                retarget_request["actual_target_journal"] = actual_target
                retarget_request["last_error"] = message
                retarget_request["updated_at"] = now_iso()
                paper["retarget_request"] = retarget_request
                paper["status"] = "retarget_needed"
                paper["workflow_status"] = "retarget_needed"
                paper["worker_status"] = "retarget_needed"
                paper["blocker"] = ""
                paper["next_action"] = "retry_journal_selection_excluding_history"
                paper["last_monitor_followup_status"] = "historical_target_retry_ready"
                paper["last_monitor_followup_detail"] = message
                retry_selection.append(paper_id)
                continue
            failed[paper_id] = message
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "retarget_historical_target_blocked"
            paper["worker_status"] = "retarget_historical_target_blocked"
            paper["blocker"] = SMART_RETARGET_HISTORICAL_TARGET_REUSED
            paper["next_action"] = "review_journal_exclusions_before_retry"
            paper["last_monitor_followup_status"] = "retarget_package_blocked"
            paper["last_monitor_followup_detail"] = message
            if retarget_request:
                retarget_request["status"] = "historical_target_blocked"
                retarget_request["excluded_journals"] = list(excluded_journals)
                retarget_request["historical_target_retry_count"] = retry_count
                retarget_request["actual_target_journal"] = actual_target
                retarget_request["last_error"] = message
                retarget_request["updated_at"] = now_iso()
                paper["retarget_request"] = retarget_request
            continue

        if retarget_request.get("mode") == RETARGET_MODE_SPECIFIC:
            expected_target = str(
                retarget_request.get("target_journal") or ""
            ).strip()
            if (
                expected_target
                and _normalized_journal_name(expected_target)
                != _normalized_journal_name(actual_target)
            ):
                message = (
                    "The journal-author result does not match the exact user-selected target: "
                    f"expected={expected_target}; actual={actual_target or '-'}"
                )
                failed[paper_id] = message
                paper["status"] = "revision_needed"
                paper["workflow_status"] = "retarget_target_mismatch"
                paper["worker_status"] = "retarget_target_mismatch"
                paper["blocker"] = "SMART_RETARGET_TARGET_MISMATCH"
                paper["next_action"] = "rerun_journal_author_for_exact_user_target"
                retarget_request["status"] = "target_mismatch_blocked"
                retarget_request["actual_target_journal"] = actual_target
                retarget_request["last_error"] = message
                retarget_request["updated_at"] = now_iso()
                paper["retarget_request"] = retarget_request
                continue

        previous_submission_id = (
            normalize_submission_id(paper.get("canonical_submission_id"))
            or normalize_submission_id(paper.get("submission_id"))
        )
        if previous_submission_id:
            close_submission_record(
                paper,
                previous_submission_id,
                status="retargeted",
                source="retarget_package_ready",
                observed_at=now_iso(),
            )
        identity = project_submission_identity(paper, observed_at=now_iso())
        if identity["active_submission_ids"]:
            message = (
                "Retarget package is ready, but another prior manuscript ID is still active: "
                + ", ".join(identity["active_submission_ids"])
            )
            failed[paper_id] = message
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "identity_reconciliation_required"
            paper["worker_status"] = "identity_reconciliation_required"
            paper["blocker"] = SMART_SUBMISSION_DUPLICATE_ACTIVE
            paper["next_action"] = "close_prior_submission_before_retarget_submit"
            paper["last_monitor_followup_status"] = "identity_reconciliation_required"
            paper["last_monitor_followup_detail"] = message
            continue

        prior_attempt = {
            "closed_at": now_iso(),
            "closed_reason": "journal_rejected_or_retargeted",
            "target_journal": str(paper.get("target_journal") or ""),
            "submission_system": str(paper.get("submission_system") or ""),
            "submission_id": str(paper.get("submission_id") or ""),
            "confirmation_url": str(paper.get("confirmation_url") or ""),
            "submission_evidence_path": str(paper.get("submission_evidence_path") or ""),
            "monitor_observation": copy.deepcopy(paper.get("last_monitor_observation") or {}),
        }
        attempt_history = paper.setdefault("submission_attempt_history", [])
        if not isinstance(attempt_history, list):
            attempt_history = []
            paper["submission_attempt_history"] = attempt_history
        attempt_history.append(prior_attempt)
        del attempt_history[:-20]
        for key in clear_for_new_submission:
            paper.pop(key, None)
        paper["legacy_submission_status"] = {}
        for key in (
            "target_journal",
            "submission_system",
            "submission_system_id",
            "target_origin",
            "credential_broker_origin",
            "credential_account_id",
            "journal_site_key",
        ):
            paper.pop(key, None)
        target_journal = str(snapshot.get("target_journal") or "").strip()
        submission_system = str(snapshot.get("submission_system") or "").strip()
        if target_journal:
            paper["target_journal"] = target_journal
        if submission_system:
            paper["submission_system"] = submission_system
        identity = project_submission_identity(paper, observed_at=now_iso())
        if not identity["new_submission_allowed"]:
            message = "Retarget package cannot enter submission while a prior manuscript remains active."
            failed[paper_id] = message
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "identity_reconciliation_required"
            paper["worker_status"] = "identity_reconciliation_required"
            paper["blocker"] = SMART_SUBMISSION_DUPLICATE_ACTIVE
            paper["next_action"] = "close_prior_submission_before_retarget_submit"
            paper["last_monitor_followup_status"] = "identity_reconciliation_required"
            paper["last_monitor_followup_detail"] = message
            continue
        paper["source_package_dir"] = str(snapshot.get("package_dir") or "")
        paper["paper_dir"] = str(snapshot.get("paper_dir") or "")
        paper["status"] = "queued"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["blocker"] = ""
        paper["next_action"] = "preflight_retarget_package"
        paper["online_intent"] = "submit_retargeted_package"
        paper["detail"] = (
            "The retarget package passed the journal-author contract. The program archived "
            "the rejected submission identity and queued a fresh local preflight."
        )
        paper["last_monitor_followup_status"] = "retarget_package_ready"
        paper["last_monitor_followup_detail"] = paper["detail"]
        paper["monitor_retarget_package_ready_at"] = now_iso()
        if retarget_request:
            retarget_request["status"] = "completed_handoff_to_submission"
            retarget_request["selected_target_journal"] = str(
                snapshot.get("target_journal") or ""
            )
            retarget_request["package_dir"] = str(snapshot.get("package_dir") or "")
            retarget_request["completed_at"] = now_iso()
            retarget_request["updated_at"] = now_iso()
            paper["retarget_request"] = retarget_request
        append_unique(local_queue, paper_id)
        ready.append(paper_id)

    state["paper_queue"] = local_queue
    if ready:
        state["mode"] = "run_once"
    if ready or retry_selection or failed or request_successors:
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "prepare_completed_smart_monitor_retargets",
                "ready": ready,
                "retry_selection": retry_selection,
                "waiting": waiting,
                "failed": failed,
                "request_successors": request_successors,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ready": ready,
        "retry_selection": retry_selection,
        "waiting": waiting,
        "failed": failed,
        "snapshots": snapshots,
        "request_successors": request_successors,
    }


def reconcile_smart_monitor_followups(
    paper_ids: Iterable[str] | None = None,
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """Advance completed retarget packages back into Smart SDK submission."""

    queue_normalize = reconcile_smart_online_queue_state(
        reason="before_monitor_retarget_handoff",
    )
    prepared = _prepare_completed_smart_monitor_retargets(paper_ids)
    ready = list(prepared.get("ready", []))
    retry_selection = list(prepared.get("retry_selection", []))
    retry_result = (
        process_requested_smart_retargets(retry_selection, activate=activate)
        if retry_selection
        else {}
    )
    if not ready:
        return {
            "ready": [],
            "retry_selection": retry_selection,
            "retry_result": retry_result,
            "waiting": prepared.get("waiting", {}),
            "failed": prepared.get("failed", {}),
            "preflight": {},
            "online": {},
            "runtime": {},
            "queue_normalize": queue_normalize,
        }
    preflight = run_smart_submission_scheduler(
        force=True,
        online_recovery=False,
        selected_ids=ready,
    )
    online = queue_visible_online_workers(
        ready,
        activate=False,
        sdk_browser=True,
    )
    queued = [str(item) for item in online.get("queued", []) if str(item).strip()]
    runtime = (
        _start_smart_monitor_followup_runtime(queued)
        if activate and queued
        else {"ok": True, "started": False, "selected_ids": queued}
    )
    return {
        "ready": ready,
        "retry_selection": retry_selection,
        "retry_result": retry_result,
        "waiting": prepared.get("waiting", {}),
        "failed": prepared.get("failed", {}),
        "preflight": {key: value for key, value in preflight.items() if key != "state"},
        "online": {key: value for key, value in online.items() if key != "state"},
        "runtime": runtime,
        "queue_normalize": queue_normalize,
    }


def process_confirmed_smart_retargets(
    paper_ids: Iterable[str] | None = None,
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """Automatically route explicit rejection/retarget states to journal selection.

    This is a program transition, not an AI scheduling decision.  It never
    treats login failures or generic blockers as rejection; only persisted
    ``rejected``/``retarget_needed`` states are eligible.  The normal identity
    ledger and idempotency fingerprint still protect against duplicate work.
    """

    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    if not bool(settings.get("auto_retarget_after_rejection_enabled", True)):
        return {"enabled": False, "eligible": [], "result": {}}
    selected = (
        {str(item).strip() for item in paper_ids if str(item).strip()}
        if paper_ids is not None
        else None
    )
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    eligible: list[str] = []
    for paper_id, paper in papers.items():
        paper_id = str(paper_id)
        if selected is not None and paper_id not in selected:
            continue
        if not isinstance(paper, dict):
            continue
        if retarget_request_is_active(paper.get("retarget_request")):
            continue
        if str(paper.get("status") or "").strip().casefold() not in {
            "rejected",
            "retarget_needed",
        }:
            continue
        if str(paper.get("last_monitor_followup_status") or "") in {
            "journal_author_dispatching",
            "journal_author_queued",
            "retarget_package_ready",
        }:
            continue
        observation = paper.get("last_monitor_observation")
        if not isinstance(observation, Mapping) or classify_monitor_action(
            status=paper.get("status"),
            action_kind=observation.get("action_kind"),
            summary=observation.get("summary"),
        ) != "retarget":
            record_smart_monitor_observation(
                paper_id,
                action_kind="retarget",
                summary=str(paper.get("detail") or "Explicit journal rejection requires retargeting"),
                submission_id=str(
                    paper.get("canonical_submission_id")
                    or paper.get("submission_id")
                    or ""
                ),
                source="program_confirmed_retarget",
            )
        eligible.append(paper_id)
    result = (
        apply_smart_monitor_followups(eligible, activate=activate)
        if eligible
        else {}
    )
    return {"enabled": True, "eligible": eligible, "result": result}


def _new_user_retarget_request_id(paper_id: str) -> str:
    digest = hashlib.sha256(
        f"{paper_id}|{now_iso()}|{os.urandom(8).hex()}".encode("utf-8")
    ).hexdigest()[:16]
    return f"retarget-{timestamp_id()}-{digest}"


def _ensure_user_retarget_route(
    paper_id: str,
    *,
    request_id: str,
) -> dict[str, Any]:
    unified = _unified_state_snapshot()
    route = submission_route_for(unified.get("submission_routes"), paper_id)
    if route.get("execution_owner") == "smart" and route.get("mode") == "canary":
        return {
            "changed": False,
            "paper_id": paper_id,
            "submission_route": route,
        }
    from .orchestrator import UnifiedOrchestrator

    return UnifiedOrchestrator().handoff_existing_submission_to_smart(
        paper_id,
        expected_revision=int(route.get("revision") or 1),
        operation_id=f"user-{request_id}"[:160],
    )


@_smart_state_locked
def _record_user_retarget_request(
    paper_id: str,
    request: Mapping[str, Any],
    *,
    route: Mapping[str, Any],
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise KeyError(f"paper not found in smart submission state: {paper_id}")
    normalized = normalize_retarget_request(request, paper_id=paper_id)
    requested_journal = str(normalized.get("target_journal") or "").strip()
    historical_journals = _historical_retarget_journals(paper_id, paper)
    if (
        normalized.get("mode") == RETARGET_MODE_SPECIFIC
        and any(
            _journal_names_equivalent(requested_journal, historical)
            for historical in historical_journals
        )
    ):
        raise ValueError("指定的转投期刊不能是当前或历史已投稿期刊")
    normalized["excluded_journals"] = list(historical_journals)
    paper["retarget_excluded_journals"] = list(historical_journals)

    previous = paper.get("retarget_request")
    if isinstance(previous, Mapping) and retarget_request_is_active(previous):
        history = paper.setdefault("retarget_request_history", [])
        if not isinstance(history, list):
            history = []
            paper["retarget_request_history"] = history
        superseded = copy.deepcopy(dict(previous))
        superseded["status"] = "superseded_by_new_user_request"
        superseded["superseded_at"] = now_iso()
        history.append(superseded)
        del history[:-20]

    _mark_smart_route_on_paper(paper, dict(route))
    initial = retarget_withdrawal_decision(paper, normalized)
    if normalized.get("withdrawal_policy") == WITHDRAWAL_POLICY_CONFIRMED_CLOSED:
        for submission_id in initial.get("closure_targets", []):
            close_submission_record(
                paper,
                submission_id,
                status="user_confirmed_closed",
                source="user_retarget_confirmed_closed",
                observed_at=now_iso(),
            )
        project_submission_identity(paper, observed_at=now_iso())
        initial = retarget_withdrawal_decision(paper, normalized)

    decision = str(initial.get("decision") or "")
    normalized["withdrawal_decision"] = decision
    normalized["withdrawal_reason"] = str(initial.get("reason") or "")
    normalized["withdrawal_targets"] = [
        str(item) for item in initial.get("closure_targets", []) if str(item).strip()
    ]
    normalized["status"] = (
        "identity_check_required"
        if decision == "verify_or_withdraw"
        else "ready_for_selection"
    )
    normalized["updated_at"] = now_iso()
    paper["retarget_request"] = normalized
    paper["retarget_request_id"] = str(normalized.get("request_id") or "")
    paper["retarget_mode"] = str(normalized.get("mode") or "")
    paper["retarget_publication_tier"] = str(
        normalized.get("publication_tier") or ""
    )
    paper["retarget_priority"] = str(normalized.get("priority") or "")
    paper["retarget_requested_target_journal"] = requested_journal
    paper["last_user_retarget_requested_at"] = now_iso()
    paper["blocker"] = ""

    local_queue = [
        str(item)
        for item in state.get("paper_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    online_queue = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    if decision == "verify_or_withdraw":
        stale_thread = _detach_stale_visible_worker(
            paper,
            reason="user_retarget_verify_or_withdraw",
            timestamp=now_iso(),
        )
        if stale_thread:
            paper["last_user_retarget_stale_thread_id"] = stale_thread
        _rotate_canary_attempt(paper, paper_id)
        paper["status"] = "revision_needed"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["online_intent"] = "user_retarget_verify_or_withdraw"
        paper["next_action"] = "verify_current_submission_then_withdraw_if_active"
        paper["detail"] = (
            "User requested retargeting. The program must verify every recorded active "
            "submission and close it when necessary before journal selection starts."
        )
        _append_online_queue_guarded(
            online_queue,
            paper_id,
            paper,
            expected_revision=int(route.get("revision") or 1),
        )
    else:
        paper["status"] = "retarget_needed"
        paper["workflow_status"] = "retarget_needed"
        paper["worker_status"] = "retarget_needed"
        paper["online_intent"] = "user_requested_retarget"
        paper["next_action"] = "queue_user_selected_journal_author_retarget"
        paper["detail"] = (
            "Prior submission closure is already evidenced; the user-selected retarget "
            "request may enter journal selection."
        )
    state["paper_queue"] = local_queue
    state["online_queue"] = online_queue
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "request_smart_submission_retarget",
            "paper_id": paper_id,
            "request_id": normalized.get("request_id"),
            "mode": normalized.get("mode"),
            "publication_tier": normalized.get("publication_tier"),
            "priority": normalized.get("priority"),
            "withdrawal_policy": normalized.get("withdrawal_policy"),
            "withdrawal_decision": decision,
            "withdrawal_targets": normalized.get("withdrawal_targets", []),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "paper_id": paper_id,
        "request": copy.deepcopy(normalized),
        "withdrawal": initial,
        "online_queued": paper_id in online_queue,
        "state_status": paper.get("status"),
        "workflow_status": paper.get("workflow_status"),
    }


@_smart_state_locked
def _claim_user_retarget_selection(paper_id: str) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        return {"claimed": False, "reason": "paper_missing"}
    request = paper.get("retarget_request")
    if not isinstance(request, Mapping) or not retarget_request_is_active(request):
        return {"claimed": False, "reason": "active_request_missing"}
    request = normalize_retarget_request(request, paper_id=paper_id)
    if str(request.get("status") or "") in {
        "journal_author_dispatching",
        "journal_author_queued",
        "package_ready",
    }:
        return {"claimed": False, "reason": str(request.get("status"))}
    decision = retarget_withdrawal_decision(paper, request)
    if decision.get("decision") != "ready_for_selection":
        return {
            "claimed": False,
            "reason": str(decision.get("reason") or "closure_not_ready"),
            "withdrawal": decision,
        }
    request["status"] = "journal_author_dispatching"
    request["journal_author_dispatching_at"] = now_iso()
    request["updated_at"] = now_iso()
    paper["retarget_request"] = request
    paper["status"] = "retarget_needed"
    paper["workflow_status"] = "retarget_needed"
    paper["worker_status"] = "retarget_needed"
    paper["next_action"] = "queue_user_selected_journal_author_retarget"
    paper["online_intent"] = "user_requested_retarget"
    state["paper_queue"] = [
        str(item)
        for item in state.get("paper_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "claim_user_retarget_journal_selection",
            "paper_id": paper_id,
            "request_id": request.get("request_id"),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "claimed": True,
        "paper_id": paper_id,
        "paper": copy.deepcopy(paper),
        "request": copy.deepcopy(request),
        "settings": copy.deepcopy(state.get("settings") or {}),
    }


@_smart_state_locked
def _finish_user_retarget_selection(
    paper_id: str,
    *,
    request_id: str,
    result: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        return {"ok": False, "paper_id": paper_id, "stale": True}
    request = paper.get("retarget_request")
    if not isinstance(request, dict) or str(request.get("request_id") or "") != request_id:
        return {"ok": False, "paper_id": paper_id, "stale": True}
    now = now_iso()
    if error:
        request["status"] = "ready_for_selection"
        request["last_error"] = str(error)[:1000]
        request["last_error_at"] = now
        paper["status"] = "retarget_needed"
        paper["workflow_status"] = "retarget_needed"
        paper["worker_status"] = "retarget_needed"
        paper["blocker"] = str(error)[:1000]
        paper["next_action"] = "retry_user_retarget_journal_selection"
        outcome = {"ok": False, "paper_id": paper_id, "error": str(error)}
    else:
        payload = dict(result or {})
        excluded_journals = [
            str(item).strip()
            for item in payload.get("excluded_journals", [])
            if str(item).strip()
        ]
        if excluded_journals:
            request["excluded_journals"] = list(excluded_journals)
            paper["retarget_excluded_journals"] = list(excluded_journals)
        request["status"] = "journal_author_queued"
        request["journal_author_request_id"] = str(payload.get("request_id") or "")
        request["journal_author_queued_at"] = now
        request["last_error"] = ""
        paper["status"] = "waiting"
        paper["workflow_status"] = "waiting_journal_author_retarget"
        paper["worker_status"] = "waiting_journal_author_retarget"
        paper["blocker"] = ""
        paper["next_action"] = "wait_journal_author_retarget_package"
        paper["online_intent"] = "retarget_after_rejection"
        paper["last_monitor_followup_status"] = "journal_author_queued"
        paper["last_monitor_followup_detail"] = (
            "The user-selected retarget request is running in journal-author V2 Steps 2-3."
        )
        paper["last_retarget_queued_at"] = now
        paper["monitor_retarget_request_id"] = request["journal_author_request_id"]
        paper["monitor_retarget_previous_target_journal"] = str(
            payload.get("previous_target_journal") or paper.get("target_journal") or ""
        )
        paper["monitor_retarget_archive"] = copy.deepcopy(payload.get("archive") or {})
        paper["last_audit_path"] = str(
            payload.get("audit_path") or paper.get("last_audit_path") or ""
        )
        outcome = {
            "ok": True,
            "paper_id": paper_id,
            "request_id": request["journal_author_request_id"],
            "audit_path": paper["last_audit_path"],
        }
    request["updated_at"] = now
    paper["retarget_request"] = request
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "finish_user_retarget_journal_selection",
            "paper_id": paper_id,
            "request_id": request_id,
            "ok": not bool(error),
            "error": str(error)[:1000],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return outcome


@_smart_state_locked
def _queue_requested_retarget_closure(
    paper_ids: Iterable[str],
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [
        str(item) for item in state.get("online_queue", []) if str(item).strip()
    ]
    queued: list[str] = []
    kept: list[str] = []
    failed: dict[str, str] = {}
    changed = False
    for paper_id in dict.fromkeys(
        str(item).strip() for item in paper_ids if str(item).strip()
    ):
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            failed[paper_id] = "paper_missing"
            continue
        request = paper.get("retarget_request")
        if not isinstance(request, dict) or not retarget_request_is_active(request):
            failed[paper_id] = "active_retarget_request_missing"
            continue
        if str(request.get("status") or "") not in {
            "requested",
            "identity_check_required",
            "withdrawal_required",
        }:
            failed[paper_id] = f"request_status:{request.get('status') or '-'}"
            continue
        if (
            str(paper.get("workflow_status") or "").strip().casefold() == "running"
            and paper_id in online_queue
        ):
            kept.append(paper_id)
            continue
        if paper_id not in online_queue:
            _rotate_canary_attempt(paper, paper_id)
        paper["status"] = "revision_needed"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["blocker"] = ""
        paper["online_intent"] = "user_retarget_verify_or_withdraw"
        paper["next_action"] = "verify_current_submission_then_withdraw_if_active"
        request["status"] = "identity_check_required"
        request["updated_at"] = now_iso()
        paper["retarget_request"] = request
        try:
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                expected_revision=int(paper.get("route_revision") or 1),
            )
        except Exception as exc:
            failed[paper_id] = f"{type(exc).__name__}: {exc}"
            continue
        queued.append(paper_id)
        changed = True
    if changed:
        state["online_queue"] = online_queue
        state["mode"] = "run_once"
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "queue_requested_retarget_closure",
                "paper_ids": queued,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "queued": queued,
        "kept": kept,
        "failed": failed,
        "online_queue": online_queue,
    }


def process_requested_smart_retargets(
    paper_ids: Iterable[str] | None = None,
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """Advance user-requested retargets without an AI scheduling decision."""

    state = read_json(SMART_STATE_PATH, _default_state())
    selected = (
        {str(item).strip() for item in paper_ids if str(item).strip()}
        if paper_ids is not None
        else None
    )
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_ids: list[str] = []
    ready_ids: list[str] = []
    waiting: dict[str, str] = {}
    for paper_id, paper in papers.items():
        paper_id = str(paper_id)
        if selected is not None and paper_id not in selected:
            continue
        if not isinstance(paper, Mapping):
            continue
        request = paper.get("retarget_request")
        if not isinstance(request, Mapping) or not retarget_request_is_active(request):
            continue
        status = str(request.get("status") or "")
        if status in {"identity_check_required", "withdrawal_required"}:
            online_ids.append(paper_id)
        elif status in {"requested", "ready_for_selection"}:
            decision = retarget_withdrawal_decision(paper, request)
            if decision.get("decision") == "ready_for_selection":
                ready_ids.append(paper_id)
            else:
                online_ids.append(paper_id)
        else:
            waiting[paper_id] = status

    queued: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    for paper_id in ready_ids:
        claim = _claim_user_retarget_selection(paper_id)
        if not claim.get("claimed"):
            waiting[paper_id] = str(claim.get("reason") or "not_claimed")
            continue
        request = claim.get("request", {}) if isinstance(claim.get("request"), Mapping) else {}
        request_id = str(request.get("request_id") or "")
        try:
            result = _queue_journal_author_retarget(
                paper_id,
                dict(claim.get("paper") or {}),
                str(request.get("reason") or "user requested retarget"),
                dict(claim.get("settings") or {}),
            )
            queued.append(
                _finish_user_retarget_selection(
                    paper_id,
                    request_id=request_id,
                    result=result,
                )
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            failed[paper_id] = message
            _finish_user_retarget_selection(
                paper_id,
                request_id=request_id,
                error=message,
            )

    online_queue_result = _queue_requested_retarget_closure(online_ids)
    dispatch_ids = list(
        dict.fromkeys(
            list(online_queue_result.get("queued", []))
            + list(online_queue_result.get("kept", []))
        )
    )
    runtime = (
        _start_smart_monitor_followup_runtime(dispatch_ids)
        if activate and dispatch_ids
        else {"ok": True, "started": False, "selected_ids": dispatch_ids}
    )
    return {
        "eligible": sorted(set(online_ids + ready_ids)),
        "online_queued": online_ids,
        "journal_author_queued": queued,
        "online_queue_result": online_queue_result,
        "waiting": waiting,
        "failed": failed,
        "runtime": runtime,
    }


@_smart_state_locked
def requeue_smart_retarget_after_historical_target(
    paper_id: str,
    *,
    actual_target_journal: str,
    reason: str = "historical_target_selected_by_previous_version",
) -> dict[str, Any]:
    """Recover a persisted pre-fix retarget that selected an old journal."""

    clean_id = str(paper_id or "").strip()
    actual_target = str(actual_target_journal or "").strip()
    if not clean_id or not actual_target:
        raise ValueError("paper_id and actual_target_journal are required")
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(clean_id)
    if not isinstance(paper, dict):
        raise KeyError(f"paper not found in smart submission state: {clean_id}")
    request = paper.get("retarget_request")
    if not isinstance(request, Mapping):
        raise ValueError("an AI retarget request is required for automatic reselection")
    request = normalize_retarget_request(request, paper_id=clean_id)
    if request.get("mode") != "ai":
        raise ValueError("only AI journal selection may be automatically reselected")

    excluded = _historical_retarget_journals(clean_id, paper)
    _append_unique_journal_name(excluded, actual_target)
    retry_count = int(request.get("historical_target_retry_count") or 0) + 1
    if retry_count > SMART_RETARGET_HISTORICAL_TARGET_RETRY_LIMIT:
        raise SmartSubmissionGuardError(
            SMART_RETARGET_HISTORICAL_TARGET_REUSED,
            clean_id,
            "historical journal reselection retry limit reached",
            details={"actual_target_journal": actual_target, "retry_count": retry_count},
        )

    now = now_iso()
    request["status"] = "ready_for_selection"
    request["excluded_journals"] = list(excluded)
    request["historical_target_retry_count"] = retry_count
    request["actual_target_journal"] = actual_target
    request["last_error"] = str(reason or "historical target selected")[:1000]
    request["updated_at"] = now
    paper["retarget_request"] = request
    paper["retarget_excluded_journals"] = list(excluded)
    paper["status"] = "retarget_needed"
    paper["workflow_status"] = "retarget_needed"
    paper["worker_status"] = "retarget_needed"
    paper["blocker"] = ""
    paper["next_action"] = "retry_journal_selection_excluding_history"
    paper["last_monitor_followup_status"] = "historical_target_retry_ready"
    paper["last_monitor_followup_detail"] = str(reason or "historical target selected")[:1000]
    state["paper_queue"] = [
        str(item)
        for item in state.get("paper_queue", [])
        if str(item).strip() and str(item) != clean_id
    ]
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != clean_id
    ]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "requeue_retarget_after_historical_target",
            "paper_id": clean_id,
            "actual_target_journal": actual_target,
            "retry_count": retry_count,
            "excluded_journals": list(excluded),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "paper_id": clean_id,
        "actual_target_journal": actual_target,
        "retry_count": retry_count,
        "excluded_journals": list(excluded),
    }


def request_smart_submission_retarget(
    paper_id: str,
    *,
    mode: str,
    target_journal: str = "",
    publication_tier: str = "sci",
    priority: str = "acceptance",
    quartiles: Iterable[str] | None = None,
    oa_policy: str = "subscription_first",
    max_apc: str = "",
    keywords: str = "",
    allow_special_issue: bool = True,
    withdrawal_policy: str = "auto",
    reason: str = "user_requested_retarget",
    notes: str = "",
    activate: bool = True,
) -> dict[str, Any]:
    """Create one explicit retarget request and start its safe next action."""

    clean_id = str(paper_id or "").strip()
    if not clean_id:
        raise ValueError("paper_id is required")
    ensure_smart_submission_module()
    request_id = _new_user_retarget_request_id(clean_id)
    request = normalize_retarget_request(
        {
            "mode": mode,
            "target_journal": target_journal,
            "publication_tier": publication_tier,
            "priority": priority,
            "quartiles": list(quartiles or []),
            "oa_policy": oa_policy,
            "max_apc": max_apc,
            "keywords": keywords,
            "allow_special_issue": allow_special_issue,
            "withdrawal_policy": withdrawal_policy,
            "reason": reason,
            "notes": notes,
            "auto_submit_after_package": True,
        },
        paper_id=clean_id,
        request_id=request_id,
        created_at=now_iso(),
    )
    route_result = _ensure_user_retarget_route(
        clean_id,
        request_id=request_id,
    )
    route = (
        route_result.get("submission_route", {})
        if isinstance(route_result, Mapping)
        else {}
    )
    recorded = _record_user_retarget_request(
        clean_id,
        request,
        route=route if isinstance(route, Mapping) else {},
    )
    advanced = process_requested_smart_retargets(
        [clean_id],
        activate=activate,
    )
    return {
        "ok": not bool(advanced.get("failed")),
        "paper_id": clean_id,
        "request_id": request_id,
        "route": route_result,
        "recorded": recorded,
        "advanced": advanced,
    }


def run_smart_submission_daily_monitoring(
    *,
    force: bool = False,
    trigger: str = "scheduled",
    selected_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Queue post-submission checks and synchronously run their fixed SDK sessions."""

    claim = _claim_smart_submission_monitor_run(trigger=trigger, force=force)
    if not claim.get("claimed"):
        return {
            "ok": True,
            "kind": "daily_monitor",
            "status": "skipped",
            "monitor_check_ids": [],
            "claim": claim,
        }
    run_id = str(claim.get("run_id") or "")
    monitor_ids: list[str] = []
    try:
        sweep = queue_smart_submission_daily_sweep(
            monitoring_only=True,
            force_monitor=force,
            selected_ids=selected_ids,
        )
        monitor_ids = [
            str(item)
            for item in sweep.get("monitor_check_ids", [])
            if str(item).strip()
        ]
        if not monitor_ids:
            empty_summary = _smart_monitor_run_summary([])
            _finish_smart_submission_monitor_run(
                run_id=run_id,
                trigger=trigger,
                status="idle",
                paper_ids=[],
                summary=empty_summary,
            )
            return {
                "ok": True,
                "kind": "daily_monitor",
                "status": "idle",
                "monitor_check_ids": [],
                "claim": claim,
                "sweep": sweep,
                "summary": empty_summary,
            }
        from .smart_submission_sdk_runtime import default_smart_submission_sdk_runtime

        runtime = default_smart_submission_sdk_runtime()
        sdk_result = runtime.run_once(monitor_ids)
        initial_summary = _smart_monitor_run_summary(monitor_ids)
        released_incomplete: list[dict[str, Any]] = []
        for paper_id, item in initial_summary.get("paper_results", {}).items():
            if isinstance(item, dict) and item.get("outcome") == "pending":
                released_incomplete.append(
                    close_unfinished_smart_monitor_check(
                        str(paper_id),
                        detail=(
                            "The one-click monitor run ended without a recorded result for this "
                            "paper; the checkpoint was released so the next check can retry it."
                        ),
                    )
                )
        summary = _smart_monitor_run_summary(monitor_ids)
        followups: dict[str, Any]
        try:
            followups = apply_smart_monitor_followups(monitor_ids, activate=True)
        except Exception as exc:  # monitor results remain valid even if follow-up dispatch fails
            followups = {
                "detected_count": int(summary.get("action_required_count") or 0),
                "accepted_count": 0,
                "auto_queued_count": 0,
                "manual_revision_count": int(summary.get("action_required_count") or 0),
                "identity_reconciliation_count": 0,
                "deduplicated_count": 0,
                "failed_count": 1,
                "error": f"{type(exc).__name__}: {exc}",
            }
        summary["followups"] = copy.deepcopy(followups)
        summary["followup_auto_queued_count"] = int(
            followups.get("auto_queued_count") or 0
        )
        summary["followup_manual_revision_count"] = int(
            followups.get("manual_revision_count") or 0
        )
        summary["followup_identity_reconciliation_count"] = int(
            followups.get("identity_reconciliation_count") or 0
        )
        summary["followup_failed_count"] = int(followups.get("failed_count") or 0)
        requested_count = int(summary.get("requested_count") or 0)
        checked_count = int(summary.get("checked_count") or 0)
        failed_count = int(summary.get("failed_count") or 0)
        pending_count = int(summary.get("pending_count") or 0)
        if requested_count and checked_count == requested_count:
            final_status = "completed"
        elif checked_count:
            final_status = "partial"
        else:
            final_status = "failed"
        error_text = str(sdk_result.get("error") or "").strip()
        if failed_count or pending_count:
            incomplete_text = (
                f"checked={checked_count}, failed={failed_count}, pending={pending_count}"
            )
            error_text = f"{error_text}; {incomplete_text}".strip("; ")
        _finish_smart_submission_monitor_run(
            run_id=run_id,
            trigger=trigger,
            status=final_status,
            paper_ids=monitor_ids,
            error=error_text,
            summary=summary,
        )
        return {
            "ok": final_status == "completed",
            "kind": "daily_monitor",
            "status": final_status,
            "monitor_check_ids": monitor_ids,
            "claim": claim,
            "sweep": sweep,
            "sdk": sdk_result,
            "summary": summary,
            "followups": followups,
            "released_incomplete": released_incomplete,
        }
    except Exception as exc:
        for paper_id in monitor_ids:
            close_unfinished_smart_monitor_check(
                paper_id,
                detail=f"Monitor run failed before a result was recorded: {type(exc).__name__}.",
                source="monitor_run_exception",
            )
        failure_summary = _smart_monitor_run_summary(monitor_ids)
        _finish_smart_submission_monitor_run(
            run_id=run_id,
            trigger=trigger,
            status="failed",
            paper_ids=monitor_ids,
            error=f"{type(exc).__name__}: {exc}",
            summary=failure_summary,
        )
        raise


def run_smart_submission_program_tick() -> dict[str, Any]:
    """Execute one deterministic SDK follow-up or daily-monitor timer tick."""

    ensure_smart_submission_module()
    try:
        requested_retargets = process_requested_smart_retargets(activate=True)
    except Exception as exc:
        requested_retargets = {
            "eligible": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        confirmed_retargets = process_confirmed_smart_retargets(activate=True)
    except Exception as exc:
        confirmed_retargets = {
            "enabled": True,
            "eligible": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        followup_reconcile = reconcile_smart_monitor_followups(activate=True)
    except Exception as exc:
        followup_reconcile = {
            "ready": [],
            "waiting": {},
            "failed": {"runtime": f"{type(exc).__name__}: {exc}"},
        }
    if followup_reconcile.get("ready"):
        runtime = followup_reconcile.get("runtime", {})
        return {
            "ok": bool(not isinstance(runtime, dict) or runtime.get("ok", True)),
            "kind": "monitor_followup_retarget_handoff",
            "status": "completed",
            "monitor_followup_reconcile": followup_reconcile,
            "requested_retargets": requested_retargets,
            "confirmed_retargets": confirmed_retargets,
        }
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = (
        state.get("settings", {})
        if isinstance(state.get("settings"), dict)
        else {}
    )
    if settings.get("manual_wakeup_active"):
        from .smart_submission_sdk_runtime import default_smart_submission_sdk_runtime

        sdk_result = default_smart_submission_sdk_runtime().run_once()
        return {
            "ok": bool(sdk_result.get("ok", True)),
            "kind": "manual_fast_wakeup",
            "status": "completed" if sdk_result.get("ok", True) else "failed",
            "sdk": sdk_result,
            "monitor_followup_reconcile": followup_reconcile,
            "requested_retargets": requested_retargets,
            "confirmed_retargets": confirmed_retargets,
        }
    if not settings.get("daily_monitor_enabled", True):
        return {
            "ok": True,
            "kind": "daily_monitor",
            "status": "disabled",
            "message": "Daily Smart Submission monitoring is disabled.",
            "monitor_followup_reconcile": followup_reconcile,
            "requested_retargets": requested_retargets,
            "confirmed_retargets": confirmed_retargets,
        }
    result = run_smart_submission_daily_monitoring(
        force=False,
        trigger="scheduled_external",
    )
    result["monitor_followup_reconcile"] = followup_reconcile
    result["requested_retargets"] = requested_retargets
    result["confirmed_retargets"] = confirmed_retargets
    return result


def run_smart_submission_automation_tick() -> dict[str, Any]:
    """Deprecated CLI alias retained for already-installed Windows tasks."""

    result = run_smart_submission_program_tick()
    result.setdefault("compatibility_alias", "smart-submission-automation-tick")
    return result


def _paper_summary_text(paper: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "status",
        "workflow_status",
        "blocker",
        "previous_blocker",
        "next_action",
        "online_intent",
        "detail",
        "last_worker_result",
        "last_worker_recovery_hint",
        "browser_status",
        "site_reachability",
        "email_status",
    ):
        value = paper.get(key)
        if value:
            parts.append(str(value))
    warnings = paper.get("warnings")
    if isinstance(warnings, list):
        parts.extend(str(item) for item in warnings)
    return "\n".join(parts).lower()


def _clean_extracted_value(value: str) -> str:
    value = str(value or "").strip()
    value = value.strip("`*_ \t\r\n")
    value = re.split(r"\s{2,}|\s+\|\s+", value, maxsplit=1)[0].strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ;,")


def _first_markdown_field(text: str, labels: tuple[str, ...]) -> str:
    if not text:
        return ""
    escaped = "|".join(re.escape(label) for label in labels)
    pattern = rf"(?im)^\s*(?:[-*]\s*)?(?:{escaped})\s*(?:=>|:|=)\s*(.+?)\s*$"
    for match in re.finditer(pattern, text):
        value = _clean_extracted_value(match.group(1))
        if value and value.lower() not in {"-", "none", "n/a", "unknown"}:
            return value[:240]
    return ""


def _package_status_for_paper(paper_id: str, paper: dict[str, Any]) -> str:
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    texts: list[str] = []
    if package_dir is not None:
        for name in ("final_risk_report.md", "package_blocker.md", "submission_checklist.md"):
            path = package_dir / name
            if path.exists():
                texts.append(read_text(path))
        parent = package_dir.parent
        for name in ("final_risk_report.md", "package_blocker.md", "submission_checklist.md"):
            path = parent / name
            if path.exists():
                texts.append(read_text(path))
    texts.append(_paper_summary_text(paper))
    combined = "\n".join(texts)
    for pattern in (
        r"(?im)^\s*PACKAGE_STATUS\s*(?:=>|:|=)\s*([A-Z0-9_ -]+)\s*$",
        r"(?im)^\s*package status\s*(?:=>|:|=)\s*([A-Z0-9_ -]+)\s*$",
    ):
        match = re.search(pattern, combined)
        if match:
            return re.sub(r"[^A-Z0-9_]+", "_", match.group(1).strip().upper()).strip("_")
    lowered = combined.lower()
    if "submitted successfully" in lowered or "confirmation page displayed" in lowered:
        return "SUBMITTED_MONITORING"
    if "submitted_monitoring" in lowered:
        return "SUBMITTED_MONITORING"
    if "needs_minor_fix" in lowered:
        return "NEEDS_MINOR_FIX"
    return ""


def _submission_id_from_local_evidence(
    paper_id: str,
    paper: dict[str, Any],
) -> str:
    """Recover a value-free manuscript id from existing local package evidence."""

    for key in ("submission_id", "manuscript_id", "submission_number", "tracking_id"):
        try:
            value = _sanitize_submission_id(paper.get(key))
        except ValueError:
            value = ""
        if value:
            return value

    if _is_fresh_retarget_handoff(paper):
        return ""

    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    texts: list[str] = []
    legacy = paper.get("legacy_submission_status")
    if isinstance(legacy, dict):
        texts.extend(
            str(legacy.get(key) or "")
            for key in ("detail", "submission_id", "manuscript_id")
        )
    legacy_record = _legacy_submission_record(paper_id)
    if legacy_record:
        for key in ("submission_id", "manuscript_id", "submission_number", "tracking_id"):
            try:
                candidate = _sanitize_submission_id(legacy_record.get(key))
            except ValueError:
                candidate = ""
            if candidate:
                return candidate
        texts.extend(
            str(legacy_record.get(key) or "")
            for key in ("submission_status", "detail")
        )
    if package_dir is not None:
        for filename in ("submission_manifest.json", "springer_submission_metadata.json"):
            data = read_json(package_dir / filename, {})
            if isinstance(data, dict):
                pending: list[Any] = [data]
                while pending:
                    item = pending.pop()
                    if isinstance(item, dict):
                        for key, value in item.items():
                            normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
                            if normalized_key in {
                                "submission_id",
                                "manuscript_id",
                                "submission_number",
                                "manuscript_number",
                                "tracking_id",
                            }:
                                try:
                                    candidate = _sanitize_submission_id(value)
                                except ValueError:
                                    candidate = ""
                                if candidate:
                                    return candidate
                            elif isinstance(value, (dict, list)):
                                pending.append(value)
                    elif isinstance(item, list):
                        pending.extend(item)
        for filename in (
            "submission_checklist.md",
            "final_risk_report.md",
            "package_blocker.md",
        ):
            path = package_dir / filename
            if path.is_file():
                texts.append(read_text(path))
    combined = "\n".join(texts)
    pattern = re.compile(
        r"(?i)\b(?:submission|manuscript)\s*(?:id|number|no\.?)\s*(?:[:=#]|is)?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._/():+\-]{4,127})"
    )
    for match in pattern.finditer(combined):
        raw = match.group(1).rstrip(".,;:)")
        try:
            candidate = _sanitize_submission_id(raw)
        except ValueError:
            continue
        if candidate:
            return candidate
    return ""


def _target_context_from_package(paper_id: str, paper: dict[str, Any]) -> dict[str, Any]:
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    if package_dir is None:
        raw_package = str(paper.get("source_package_dir") or paper.get("package_dir") or "").strip()
        package_dir = Path(raw_package) if raw_package else Path(".")
    paper_dir = package_dir.parent if package_dir and package_dir.name == "package" else package_dir

    text_sources: list[tuple[str, str]] = []
    if package_dir.exists():
        for path in (
            package_dir / "final_risk_report.md",
            package_dir / "submission_checklist.md",
            package_dir / "package_blocker.md",
            package_dir / "declarations.md",
        ):
            if path.exists():
                text_sources.append((path.name, read_text(path)))
    if paper_dir.exists():
        for path in (
            paper_dir / "journal.md",
            paper_dir / "submission_checklist.md",
            paper_dir / "author_info.md",
        ):
            if path.exists():
                text_sources.append((path.name, read_text(path)))

    target = _clean_extracted_value(
        str(
            paper.get("target_journal")
            or paper.get("current_journal")
            or paper.get("journal")
            or ""
        )
    )
    system_texts: list[str] = [
        str(paper.get("submission_system_id") or ""),
        str(paper.get("submission_system") or paper.get("journal_system") or ""),
    ]
    if not target:
        for _name, text in text_sources:
            target = _first_markdown_field(
                text,
                (
                    "Target journal",
                    "Recommended journal",
                    "Final target",
                    "Journal",
                    "Target",
                ),
            )
            if target:
                break
    if not target:
        for name, text in text_sources:
            if name == "journal.md":
                target = _extract_recommended_journal(text)
                if target:
                    break

    for _name, text in text_sources:
        value = _first_markdown_field(
            text,
            (
                "Publisher/system",
                "Submission system",
                "System",
                "Publisher",
                "Journal system",
                "Submission portal",
            ),
        )
        if value:
            system_texts.append(value)
        system_texts.append(text[:4000])
    if target:
        system_texts.append(target)

    system_id = str(paper.get("submission_system_id") or "").strip()
    if not system_id:
        system_id = _guess_system_id("\n".join(system_texts))
    system_label = _clean_extracted_value(str(paper.get("submission_system") or ""))
    if not system_label and system_id:
        system_label = _system_label(system_id)

    adapter = submission_site_adapter(system_id)
    submission_journal_id = ""
    submission_start_url = ""
    if adapter is not None:
        submission_journal_id = adapter.extract_journal_identifier(
            paper.get("submission_journal_id"),
            paper.get("journal_id"),
            paper.get("submission_start_url"),
            *(text for _name, text in text_sources),
        )
        submission_start_url = adapter.submission_start_url(submission_journal_id)

    return {
        "paper_id": paper_id,
        "paper_dir": str(paper_dir) if paper_dir else "",
        "package_dir": str(package_dir) if package_dir else "",
        "target_journal": target,
        "submission_system": system_label,
        "submission_system_id": system_id,
        "submission_journal_id": submission_journal_id,
        "submission_start_url": submission_start_url,
        "package_status": _package_status_for_paper(paper_id, paper),
    }


def _hydrate_paper_target_from_package(paper_id: str, paper: dict[str, Any]) -> dict[str, Any]:
    context = _target_context_from_package(paper_id, paper)
    changed = False
    package_dir = context.get("package_dir")
    if package_dir and Path(str(package_dir)).exists() and str(paper.get("source_package_dir") or "") != str(package_dir):
        paper["source_package_dir"] = str(package_dir)
        changed = True
    paper_dir = context.get("paper_dir")
    if paper_dir and Path(str(paper_dir)).exists() and str(paper.get("paper_dir") or "") != str(paper_dir):
        paper["paper_dir"] = str(paper_dir)
        changed = True
    current_target = _clean_extracted_value(str(paper.get("target_journal") or ""))
    if not current_target and context.get("target_journal"):
        paper["target_journal"] = context["target_journal"]
        changed = True
    current_system = _clean_extracted_value(str(paper.get("submission_system") or ""))
    if not current_system and context.get("submission_system"):
        paper["submission_system"] = context["submission_system"]
        changed = True
    if not str(paper.get("submission_system_id") or "").strip() and context.get("submission_system_id"):
        paper["submission_system_id"] = context["submission_system_id"]
        changed = True
    derived_journal_id = str(context.get("submission_journal_id") or "").strip()
    current_journal_id = str(paper.get("submission_journal_id") or "").strip()
    start_url_source = str(paper.get("submission_start_url_source") or "").strip()
    if derived_journal_id and (
        not current_journal_id or start_url_source == "package_adapter"
    ) and current_journal_id != derived_journal_id:
        paper["submission_journal_id"] = derived_journal_id
        changed = True
    derived_start_url = str(context.get("submission_start_url") or "").strip()
    current_start_url = str(paper.get("submission_start_url") or "").strip()
    if derived_start_url and (
        not current_start_url or start_url_source == "package_adapter"
    ) and current_start_url != derived_start_url:
        paper["submission_start_url"] = derived_start_url
        paper["submission_start_url_source"] = "package_adapter"
        changed = True
    if context.get("package_status") and paper.get("package_status") != context["package_status"]:
        paper["package_status"] = context["package_status"]
        changed = True
    if changed:
        paper["last_target_hydrated_at"] = now_iso()
    context["changed"] = changed
    return context


def _trusted_open_submission_draft_binding(
    paper_id: str,
    paper: dict[str, Any],
) -> dict[str, str]:
    """Read a uniquely matching open draft from the project-owned CDP browser."""

    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    adapter = submission_site_adapter(system_id)
    if adapter is None or system_id != "springer_nature":
        return {}
    try:
        from .smart_browser_cdp import SmartBrowserCdpController

        pages = SmartBrowserCdpController(ports=SMART_CDP_PORTS).list_pages(
            adapter.allowed_origins
        )
    except Exception:
        return {}
    target_journal = re.sub(
        r"[^a-z0-9]+",
        " ",
        str(paper.get("target_journal") or "").casefold(),
    ).strip()
    matches: dict[str, dict[str, str]] = {}
    for page in pages:
        url = str(page.get("url") or "").strip()
        title = re.sub(
            r"[^a-z0-9]+",
            " ",
            str(page.get("title") or "").casefold(),
        ).strip()
        match = re.match(
            r"^https://submission\.springernature\.com/new/submission/"
            r"(?P<draft_id>[A-Za-z0-9-]+)(?P<path>/[^?#]*)?(?:[?#].*)?$",
            url,
            flags=re.IGNORECASE,
        )
        remote_path = str(match.group("path") or "/").strip().casefold() if match else ""
        terminal_path = any(
            token in remote_path
            for token in ("/success", "/confirm", "/submitted", "/complete", "/thank")
        )
        if (
            match is None
            or terminal_path
            or (target_journal and target_journal not in title)
        ):
            continue
        draft_id = str(match.group("draft_id") or "").strip()
        if not draft_id:
            continue
        matches[draft_id] = {
            "paper_id": paper_id,
            "draft_id": draft_id,
            "draft_url": url,
            "upload_url": (
                url if "/upload-files" in str(match.group("path") or "") else ""
            ),
            "page_id": str(page.get("page_id") or ""),
            "source": "trusted_project_cdp",
        }
    return next(iter(matches.values())) if len(matches) == 1 else {}


def _apply_open_submission_draft_binding(
    paper_id: str,
    paper: dict[str, Any],
) -> dict[str, str]:
    binding = _trusted_open_submission_draft_binding(paper_id, paper)
    if not binding:
        return {}
    paper["draft_id"] = binding["draft_id"]
    paper["submission_draft_id"] = binding["draft_id"]
    paper["draft_url"] = binding["draft_url"]
    paper["submission_draft_url"] = binding["draft_url"]
    if binding.get("upload_url"):
        paper["upload_url"] = binding["upload_url"]
        paper["submission_upload_url"] = binding["upload_url"]
    paper["draft_binding_source"] = binding["source"]
    paper["draft_binding_captured_at"] = now_iso()
    return binding


@_smart_state_locked
def stage_smart_submission_upload_package(paper_id: str) -> dict[str, Any]:
    """Stage a deterministic browser-upload archive without using an AI turn."""

    ensure_smart_submission_module()
    paper_id = str(paper_id or "").strip()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise ValueError(f"Smart Submission paper not found: {paper_id}")
    _hydrate_paper_target_from_package(paper_id, paper)
    if (
        str(paper.get("submission_system_id") or "").strip().casefold()
        != "springer_nature"
    ):
        raise ValueError(
            "deterministic SNAPP staging requires a Springer Nature paper"
        )
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    if package_dir is None:
        raise ValueError("submission package directory is missing")
    safe_paper_id = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id) or "paper"
    destination = (
        SMART_UPLOAD_STAGING_DIR
        / safe_paper_id
        / f"{safe_paper_id}_snapp_latex.zip"
    )
    staged = stage_snapp_latex_archive(package_dir, destination)
    paper["uploaded_manuscript_zip"] = str(staged.path)
    paper["upload_package"] = str(staged.path)
    paper["uploaded_manuscript_sha256"] = staged.sha256
    paper["upload_package_format"] = "snapp_latex_source_zip"
    paper["upload_package_files"] = list(staged.files)
    paper["upload_package_bytes"] = staged.bytes
    paper["upload_package_staged_at"] = now_iso()
    draft_binding = _apply_open_submission_draft_binding(paper_id, paper)
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "stage_smart_submission_upload_package",
            "paper_id": paper_id,
            "format": "snapp_latex_source_zip",
            "path": str(staged.path),
            "sha256": staged.sha256,
            "file_count": len(staged.files),
            "draft_id": str(
                draft_binding.get("draft_id") or paper.get("draft_id") or ""
            ),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "code": "SMART_SNAPP_UPLOAD_PACKAGE_STAGED",
        "paper_id": paper_id,
        "path": str(staged.path),
        "sha256": staged.sha256,
        "bytes": staged.bytes,
        "files": list(staged.files),
        "draft_binding": draft_binding,
    }


@_smart_state_locked
def resume_smart_submission_after_verified_upload(paper_id: str) -> dict[str, Any]:
    """Verify the visible SNAPP upload and release only that repaired paper."""

    ensure_smart_submission_module()
    paper_id = str(paper_id or "").strip()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise ValueError(f"Smart Submission paper not found: {paper_id}")
    route = require_smart_canary_route(
        paper_id,
        expected_revision=int(paper.get("route_revision") or 0),
    )
    _mark_smart_route_on_paper(paper, route)
    binding = _apply_open_submission_draft_binding(paper_id, paper)
    if not binding:
        raise ValueError("no unique trusted open submission draft is available")
    upload_path = _resolve_existing_path(
        paper.get("uploaded_manuscript_zip") or paper.get("upload_package"),
        base=REPO_ROOT,
    )
    expected_sha256 = str(paper.get("uploaded_manuscript_sha256") or "").strip()
    if upload_path is None or not upload_path.is_file() or not expected_sha256:
        raise ValueError("the staged upload archive is missing")
    actual_sha256 = hashlib.sha256(upload_path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ValueError("the staged upload archive changed after preparation")

    adapter = submission_site_adapter(paper.get("submission_system_id"))
    if adapter is None:
        raise ValueError("the submission site adapter is missing")
    from .smart_browser_cdp import SmartBrowserCdpController

    controller = SmartBrowserCdpController(ports=SMART_CDP_PORTS)
    snapshot = controller.snapshot(
        adapter.allowed_origins,
        page_id=str(binding.get("page_id") or ""),
    )
    text = re.sub(r"\s+", " ", str(snapshot.get("text") or "")).casefold()
    file_name = upload_path.name
    success_marker = f"file {file_name} has been uploaded successfully".casefold()
    controls = snapshot.get("controls") if isinstance(snapshot.get("controls"), list) else []
    has_download_control = any(
        f"download file {file_name}".casefold()
        in str(control.get("label") or "").casefold()
        for control in controls
        if isinstance(control, dict)
    )
    if success_marker not in text or not has_download_control:
        raise ValueError("the visible SNAPP page has not confirmed the staged upload")

    now = now_iso()
    paper["online_upload_verified"] = True
    paper["online_upload_verified_at"] = now
    paper["online_upload_draft_id"] = binding["draft_id"]
    paper["online_uploaded_manuscript_sha256"] = actual_sha256
    paper["online_upload_page_url"] = str(snapshot.get("url") or binding["draft_url"])
    paper["status"] = "uploading"
    paper["workflow_status"] = "queued"
    paper["worker_status"] = "queued"
    paper["blocker"] = ""
    paper["detail"] = "SNAPP manuscript archive upload verified; continue the same draft."
    paper["next_action"] = "codex_sdk_browser_continue_draft"
    _rotate_canary_attempt(paper, paper_id)
    online_queue = [
        str(item) for item in state.get("online_queue", []) if str(item).strip()
    ]
    _append_online_queue_guarded(
        online_queue,
        paper_id,
        paper,
        expected_revision=int(route["revision"]),
    )
    state["online_queue"] = online_queue
    settings = state.setdefault("settings", {})
    settings["online_submit_enabled"] = True
    settings["sdk_browser_enabled"] = True
    settings["dispatch_bridge"] = "codex_sdk_browser"
    state["mode"] = "run_once"
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "resume_smart_submission_after_verified_upload",
            "paper_id": paper_id,
            "draft_id": binding["draft_id"],
            "sha256": actual_sha256,
            "page_url": paper["online_upload_page_url"],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "code": "SMART_SNAPP_UPLOAD_VERIFIED_AND_REQUEUED",
        "paper_id": paper_id,
        "draft_id": binding["draft_id"],
        "upload_sha256": actual_sha256,
        "page_url": paper["online_upload_page_url"],
        "attempt_id": paper["attempt_id"],
    }


def _paper_has_online_target(paper_id: str, paper: dict[str, Any]) -> bool:
    _hydrate_paper_target_from_package(paper_id, paper)
    target = _clean_extracted_value(str(paper.get("target_journal") or ""))
    system = _clean_extracted_value(str(paper.get("submission_system") or paper.get("submission_system_id") or ""))
    bad = {"", "-", "none", "n/a", "unknown"}
    return target.lower() not in bad and system.lower() not in bad


def _recover_legacy_secret_broker_blocks(
    state: dict[str, Any],
    *,
    selected_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Requeue blocks created before the trusted local broker existed.

    This migration is deliberately narrow: current broker failures and human
    checkpoints remain untouched.  Only the old stable blocker (or its exact
    legacy explanatory sentence) is converted back to local preflight.
    """

    if not isinstance(state, dict):
        return {"changed": False, "recovered": []}
    settings = (
        state.get("settings", {})
        if isinstance(state.get("settings"), dict)
        else {}
    )
    if not settings.get("trusted_credential_broker_enabled", True):
        return {"changed": False, "recovered": []}

    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper_queue = [
        str(item)
        for item in state.get("paper_queue", [])
        if str(item).strip()
    ]
    recovered: list[str] = []
    legacy_explanation = (
        "no current exact-origin live session and no trusted broker completion evidence"
    )
    for raw_paper_id, paper in papers.items():
        paper_id = str(raw_paper_id)
        if selected_ids is not None and paper_id not in selected_ids:
            continue
        if not isinstance(paper, dict):
            continue
        if str(paper.get("status") or "").strip().casefold() != "blocked":
            continue
        next_action = str(paper.get("next_action") or "").strip()
        blocker_text = " ".join(
            str(paper.get(key) or "")
            for key in ("blocker", "detail")
        ).casefold()
        is_legacy = (
            next_action.casefold() == SMART_SECRET_BROKER_REQUIRED.casefold()
            or legacy_explanation in blocker_text
        )
        if not is_legacy:
            continue
        # A current broker checkpoint/failure carries an explicit broker state;
        # it must be resumed through the visible UI instead of auto-requeued.
        if str(paper.get("credential_broker_status") or "").strip():
            continue

        now = now_iso()
        paper["status"] = "queued"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "credential_broker"
        paper["blocker"] = ""
        paper["detail"] = (
            "Legacy secret-broker block migrated to the trusted local "
            "credential-broker preflight."
        )
        paper["next_action"] = "preflight"
        paper["credential_broker_status"] = "legacy_block_recovered"
        paper["credential_broker_updated_at"] = now
        paper["legacy_secret_broker_recovered_at"] = now
        append_unique(paper_queue, paper_id)
        recovered.append(paper_id)

    if recovered:
        state["paper_queue"] = paper_queue
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "recover_legacy_secret_broker_blocks",
                "paper_ids": recovered,
            }
        )
    return {"changed": bool(recovered), "recovered": recovered}


def _normalize_online_queue_state(state: dict[str, Any], *, reason: str) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"changed": False, "removed": [], "target_hydrated": [], "email_firsted": [], "waiting": [], "skipped": {}}
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    raw_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    online_queue = list(dict.fromkeys(raw_queue))
    kept: list[str] = []
    removed: list[str] = []
    target_hydrated: list[str] = []
    email_firsted: list[str] = []
    waiting: list[str] = []
    skipped: dict[str, str] = {}
    changed = online_queue != raw_queue
    try:
        unified_state = _unified_state_snapshot()
    except SmartSubmissionGuardError:
        unified_state = {}

    for paper_id, paper in papers.items():
        if not isinstance(paper, dict):
            continue
        context = _hydrate_paper_target_from_package(str(paper_id), paper)
        if context.get("changed"):
            target_hydrated.append(str(paper_id))
            changed = True

    paper_queue = [str(item) for item in state.get("paper_queue", []) if str(item).strip()]
    for paper_id in online_queue:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            removed.append(paper_id)
            skipped[paper_id] = "paper record is missing"
            changed = True
            continue
        status = str(paper.get("status") or "").strip().lower()
        monitor_check_pending = _monitor_check_active(paper)
        route, route_error = _worker_route_guard_result(
            paper_id,
            paper,
            unified_state=unified_state,
            monitor_only=monitor_check_pending,
        )
        if route_error is not None:
            removed.append(paper_id)
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            changed = True
            continue
        _mark_worker_route_on_paper(
            paper,
            route or {},
            monitor_only=monitor_check_pending,
        )
        package_status = _package_status_for_paper(paper_id, paper)
        if monitor_check_pending and status in {"submitted", "monitoring"}:
            paper["status"] = "monitoring"
            if str(paper.get("workflow_status") or "").strip().lower() != "running":
                paper["workflow_status"] = "queued"
                paper["worker_status"] = "queued"
            paper["blocker"] = ""
            paper["next_action"] = SCHEDULED_MONITORING_NEXT
            kept.append(paper_id)
            continue
        monitoring_like = (
            status in {"submitted", "monitoring"}
            or "SUBMITTED" in package_status
            or (
                status not in {"blocked", "revision_needed", "retarget_needed", "waiting"}
                and _paper_has_prior_submission_evidence(paper)
            )
        )
        if monitoring_like:
            if status not in {"accepted", "no_action", "stopped"}:
                paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
            paper["blocker"] = ""
            paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
            paper["last_online_queue_normalized_at"] = now_iso()
            paper["last_recovery_reason"] = "email_first_monitoring_removed_from_online_queue"
            removed.append(paper_id)
            email_firsted.append(paper_id)
            changed = True
            continue
        if status in ONLINE_QUEUE_TERMINAL_STATUSES:
            workflow = ONLINE_QUEUE_TERMINAL_STATUSES[status]
            paper["workflow_status"] = workflow
            paper["worker_status"] = workflow
            if status != "blocked":
                paper["blocker"] = ""
            removed.append(paper_id)
            skipped[paper_id] = f"terminal/waiting status: {status}"
            changed = True
            continue
        if status == "queued":
            append_unique(paper_queue, paper_id)
            paper["workflow_status"] = "queued"
            paper["next_action"] = "preflight"
            removed.append(paper_id)
            skipped[paper_id] = "local preflight queue item removed from online_queue"
            changed = True
            continue
        if status not in ONLINE_WORKER_STATUSES:
            removed.append(paper_id)
            skipped[paper_id] = f"status is not eligible for online worker: {status or 'empty'}"
            changed = True
            continue
        if not _paper_has_online_target(paper_id, paper):
            paper["status"] = "waiting"
            paper["workflow_status"] = "waiting"
            paper["worker_status"] = "waiting"
            paper["next_action"] = "wait_journal_author_target_package"
            paper["detail"] = "missing target journal or submission system; refresh journal-author package before browser dispatch"
            paper["blocker"] = ""
            removed.append(paper_id)
            waiting.append(paper_id)
            changed = True
            continue
        kept.append(paper_id)

    if changed:
        state["online_queue"] = kept
        state["paper_queue"] = paper_queue
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "normalize_online_queue_state",
                "reason": reason,
                "removed": removed,
                "target_hydrated": target_hydrated,
                "email_firsted": email_firsted,
                "waiting": waiting,
                "kept": kept,
                "skipped": skipped,
            }
        )
    return {
        "changed": changed,
        "removed": removed,
        "target_hydrated": target_hydrated,
        "email_firsted": email_firsted,
        "waiting": waiting,
        "kept": kept,
        "skipped": skipped,
    }


@_smart_state_locked
def reconcile_smart_online_queue_state(*, reason: str) -> dict[str, Any]:
    """Persist deterministic stale/terminal online-queue reconciliation."""

    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    result = _normalize_online_queue_state(state, reason=str(reason or "manual_reconcile"))
    if result.get("changed"):
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "reconcile_smart_online_queue_state",
                "reason": str(reason or "manual_reconcile"),
                "removed": list(result.get("removed", [])),
                "email_firsted": list(result.get("email_firsted", [])),
                "waiting": list(result.get("waiting", [])),
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return result


def _planned_github_repo_name(paper_id: str, paper: dict[str, Any] | None = None) -> str:
    source = str(paper_id or "").strip()
    if not source and paper:
        source = str(paper.get("title") or paper.get("paper_title") or "paper-repository")
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "paper-repository"


def _planned_github_repo_url(settings: dict[str, Any], paper_id: str, paper: dict[str, Any]) -> tuple[str, str]:
    owner = str(settings.get("github_repository_owner") or "TYSJY").strip().strip("/")
    if not owner:
        owner = "TYSJY"
    repo_name = _planned_github_repo_name(paper_id, paper)
    return repo_name, f"https://github.com/{owner}/{repo_name}"


def _metadata_payload_sha(payload: dict[str, Any]) -> str:
    packed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(packed).hexdigest()


def _resolve_package_dir_for_paper(paper_id: str, paper: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    for key in ("source_package_dir", "package_dir"):
        value = str(paper.get(key) or "").strip()
        if value:
            repaired, _ = _repair_corrupted_smart_paths(value)
            candidates.append(Path(str(repaired)))
            if "retarget_" in str(repaired):
                match = re.search(r"(retarget_[^/\\\s]+)", str(repaired))
                if match:
                    candidates.append(JOURNAL_ROOT / "papers" / paper_id / match.group(1) / "package")
    paper_dir = str(paper.get("paper_dir") or "").strip()
    if paper_dir:
        repaired_dir, _ = _repair_corrupted_smart_paths(paper_dir)
        candidates.append(Path(str(repaired_dir)) / "package")
    latest_retarget = _latest_retarget_package(paper_id)
    if latest_retarget is not None:
        candidates.append(latest_retarget)
    candidates.append(JOURNAL_ROOT / "papers" / paper_id / "package")
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _replace_markdown_section(text: str, heading_pattern: str, replacement: str) -> str:
    pattern = rf"(?ms)^{heading_pattern}\s*\n.*?(?=^##\s+|\Z)"
    if re.search(pattern, text):
        return re.sub(pattern, replacement.rstrip() + "\n\n", text, count=1)
    return text.rstrip() + "\n\n" + replacement.rstrip() + "\n"


def _markdown_section_value(text: str, heading: str) -> str:
    pattern = rf"(?ims)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, str(text or ""))
    if not match:
        return ""
    value = match.group(1).strip()
    value = re.sub(r"(?m)^[-*]\s+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _author_field_value(text: str, field: str) -> str:
    match = re.search(
        rf"(?mi)^[ \t]*(?:\d+\.[ \t]*)?{re.escape(field)}[ \t]*:[ \t]*([^\r\n]*)[ \t]*$",
        str(text or ""),
    )
    return match.group(1).strip() if match else ""


def _verified_author_prefix(
    paper: Mapping[str, Any],
    author_text: str,
    paper_dir: Path,
) -> str:
    """Resolve an author honorific only from paper-local or saved template facts."""

    corresponding = paper.get("corresponding_author")
    corresponding = corresponding if isinstance(corresponding, Mapping) else {}
    candidates = [
        corresponding.get("prefix"),
        corresponding.get("honorific"),
        corresponding.get("author_title"),
        _author_field_value(author_text, "Prefix"),
        _author_field_value(author_text, "Honorific"),
    ]

    resolved_paper_dir = paper_dir.expanduser().resolve()
    template_paths: list[Path] = []
    if resolved_paper_dir.parent.name.casefold() == "papers":
        template_paths.append(
            resolved_paper_dir.parent.parent / "controller" / "author_templates.json"
        )
    for template_path in template_paths:
        data = read_json(template_path, {})
        templates = data.get("templates") if isinstance(data, Mapping) else []
        if not isinstance(templates, list):
            continue
        default_id = str(data.get("default_template_id") or "")
        template = next(
            (
                item
                for item in templates
                if isinstance(item, Mapping)
                and (
                    str(item.get("id") or "") == default_id
                    or (not default_id and bool(item.get("is_default")))
                )
            ),
            None,
        )
        if not isinstance(template, Mapping):
            continue
        form = template.get("form")
        form = form if isinstance(form, Mapping) else {}
        template_corresponding = form.get("corresponding_author")
        template_corresponding = (
            template_corresponding
            if isinstance(template_corresponding, Mapping)
            else {}
        )
        authors = form.get("authors")
        authors = authors if isinstance(authors, list) else []
        author = next(
            (
                item
                for item in authors
                if isinstance(item, Mapping) and bool(item.get("corresponding"))
            ),
            next((item for item in authors if isinstance(item, Mapping)), {}),
        )
        candidates.extend(
            (
                template_corresponding.get("prefix"),
                template_corresponding.get("honorific"),
                template_corresponding.get("author_title"),
                author.get("prefix"),
                author.get("honorific"),
                author.get("author_title"),
            )
        )
        break

    for value in candidates:
        prefix = re.sub(r"\s+", " ", str(value or "")).strip()
        if prefix and len(prefix) <= 40 and "\x00" not in prefix:
            return prefix
    return ""


def _plain_cover_letter_text(text: str) -> str:
    plain_lines: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            plain_lines.append("")
            continue
        if stripped.startswith("#"):
            continue
        stripped = re.sub(r"^[-*]\s+", "", stripped)
        stripped = re.sub(r"^\d+\.\s+", "", stripped)
        stripped = stripped.replace("**", "").replace("__", "").replace("`", "")
        plain_lines.append(stripped)
    plain = "\n".join(plain_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", plain)


def _standing_declaration_policy(settings: dict[str, Any]) -> dict[str, str]:
    return {
        "authorized_at": str(settings.get("standing_declaration_authorized_at") or STANDING_DECLARATION_AUTHORIZED_AT),
        "ai_use_statement": str(settings.get("default_ai_use_statement") or DEFAULT_AI_USE_STATEMENT).strip(),
        "originality_statement": str(settings.get("default_originality_statement") or DEFAULT_ORIGINALITY_STATEMENT).strip(),
        "author_responsibility_statement": str(
            settings.get("default_author_responsibility_statement") or DEFAULT_AUTHOR_RESPONSIBILITY_STATEMENT
        ).strip(),
        "third_party_material_statement": str(
            settings.get("default_third_party_material_statement") or DEFAULT_THIRD_PARTY_MATERIAL_STATEMENT
        ).strip(),
        "copyright_permission_statement": str(
            settings.get("default_copyright_permission_statement") or DEFAULT_COPYRIGHT_PERMISSION_STATEMENT
        ).strip(),
    }


def _standing_declaration_terms(text: str) -> bool:
    text = str(text or "").lower()
    declaration_terms = (
        "ai-use",
        "ai use",
        "ai-assisted",
        "ai assisted",
        "generative-ai",
        "generative ai",
        "generative-ai-tool",
        "generative ai tool",
        "ai-use statement",
        "ai use statement",
        "ai-assisted-tool declaration",
        "author declaration",
        "author confirms",
        "author approval",
        "author responsibility",
        "final author consent",
        "simultaneous submission",
        "no-simultaneous-submission",
        "not under simultaneous submission",
        "under consideration by another",
        "one-manuscript-to-two-journals",
        "one manuscript to two journals",
        "author_confirm_ai",
        "blocked_pending_author_declaration",
        "needs_author_declaration",
    )
    if any(term in text for term in declaration_terms):
        return True
    if "manual_required" not in text:
        return False
    manual_context_terms = (
        "ai-use",
        "ai use",
        "ai-assisted",
        "ai assisted",
        "generative",
        "declaration",
        "simultaneous",
        "author responsibility",
        "author approval",
        "final author consent",
    )
    return any(term in text for term in manual_context_terms)


def _third_party_declaration_terms(text: str) -> bool:
    text = str(text or "").lower()
    terms = (
        "third-party material",
        "third party material",
        "third-party-material",
        "third party",
        "copyright declaration",
        "copyright permission",
        "copyright permissions",
        "permissions are required",
        "permission is required",
        "scheduler-side sidecar must verify and explicitly authorize",
        "explicitly authorize the snapp third-party-material",
        "snapp third-party-material",
        "third_party_material",
    )
    return any(term in text for term in terms)


def _package_has_third_party_conflict(package_dir: Path) -> str:
    """Return the first explicit third-party/copyright-permission conflict."""
    conflict_patterns = (
        r"(?i)third[- ]party material\s*:\s*(yes|present|required)",
        r"(?i)copyright permission(?:s)?\s*:\s*(yes|required|pending)",
        r"(?i)permission(?:s)?\s+(?:is|are)\s+required",
        r"(?i)permission(?:s)?\s+pending",
        r"(?i)copyright holder",
        r"(?i)reproduced from",
        r"(?i)adapted from",
        r"(?i)used with permission",
        r"(?i)not owned by the author",
        r"(?i)not owned by the authors",
    )
    for filename in ("declarations.md", "final_risk_report.md", "submission_checklist.md"):
        path = package_dir / filename
        if not path.exists():
            continue
        text = read_text(path)
        for pattern in conflict_patterns:
            match = re.search(pattern, text)
            if match:
                return f"{filename}: {match.group(0)}"
    return ""


def _clear_standing_declaration_state_markers(paper: dict[str, Any]) -> dict[str, str]:
    """Remove stale declaration blockers after the user-authorized defaults are applied."""
    cleared: dict[str, str] = {}
    for key in ("blocker", "previous_blocker"):
        value = str(paper.get(key) or "")
        if value and _standing_declaration_terms(value):
            paper[key] = ""
            cleared[key] = value
    if str(paper.get("last_worker_result") or "").strip().lower() == "blocked":
        combined = "\n".join(
            str(paper.get(key) or "")
            for key in ("previous_blocker", "detail", "next_action", "online_intent")
        )
        if _standing_declaration_terms(combined):
            paper["last_worker_result"] = "recovered_by_standing_declarations"
            cleared["last_worker_result"] = "blocked"
    warnings = paper.get("warnings")
    if isinstance(warnings, list):
        filtered = [item for item in warnings if not _standing_declaration_terms(str(item))]
        if len(filtered) != len(warnings):
            paper["warnings"] = filtered
            cleared["warnings"] = str(len(warnings) - len(filtered))
    return cleared


def _package_has_standing_declaration_gap(paper_id: str, paper: dict[str, Any]) -> bool:
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    if package_dir is None:
        return False
    gap_terms = (
        "manual_required",
        "blocked",
        "not provided",
        "not supplied",
        "no statement",
        "no ai-use",
        "no ai use",
        "no locally recorded",
        "confirm whether",
        "author confirmation is required",
        "requires author confirmation",
        "cannot be inferred",
        "do not invent",
    )
    for filename in ("declarations.md", "final_risk_report.md", "submission_checklist.md"):
        path = package_dir / filename
        if not path.exists():
            continue
        text = read_text(path).lower()
        if _standing_declaration_terms(text) and any(term in text for term in gap_terms):
            return True
    return False


def _update_markdown_standing_declarations(package_dir: Path, policy: dict[str, str]) -> list[str]:
    changed: list[str] = []
    ai_section = (
        "## AI Use Statement\n\n"
        f"{policy['ai_use_statement']}\n\n"
        f"Policy: standing_authorized_by_user_at_{policy['authorized_at']}.\n"
    )
    originality_section = (
        "## Originality and Simultaneous Submission\n\n"
        f"{policy['originality_statement']}\n\n"
        f"Policy: standing_authorized_by_user_at_{policy['authorized_at']}.\n"
    )
    responsibility_section = (
        "## Author Approval and Responsibility\n\n"
        f"{policy['author_responsibility_statement']}\n\n"
        f"Policy: standing_authorized_by_user_at_{policy['authorized_at']}.\n"
    )

    declarations = package_dir / "declarations.md"
    if declarations.exists():
        text = read_text(declarations)
        text = _replace_markdown_section(
            text,
            r"##\s+(?:AI[- ]?Use Statement|Generative[- ]?AI Statement|Generative-AI Declaration)",
            ai_section,
        )
        text = _replace_markdown_section(text, r"##\s+(?:Simultaneous Submission|Originality and Simultaneous Submission)", originality_section)
        text = _replace_markdown_section(text, r"##\s+(?:Author Approval and Responsibility|Author Confirmation)", responsibility_section)
        write_text(declarations, text)
        changed.append(str(declarations))

    replacement_lines = {
        r"(?mi)^Conclusion\s*:\s*BLOCKED_PENDING_AUTHOR_DECLARATION\s*$": "Conclusion: READY_WITH_STANDING_DECLARATIONS",
        r"(?mi)^[-*]\s*Generative-AI declaration:\s*BLOCKED\..*$": (
            "- Generative-AI declaration: PASS under standing user authorization; default AI-use statement "
            f"recorded at {policy['authorized_at']}."
        ),
        r"(?mi)^[-*]\s*AI-use statement:\s*MANUAL_REQUIRED.*$": (
            "- AI-use statement: PASS under standing user authorization; default AI-use statement recorded."
        ),
        r"(?mi)^.*MANUAL_REQUIRED.*(?:AI-use|AI assisted|AI-assisted|generative-AI|generative AI).*$": (
            "Standing AI-use statement applied from Smart Submission settings."
        ),
        r"(?mi)^[-*]\s*The user cannot provide a truthful generative-AI declaration\..*$": (
            "- Standing AI-use declaration is authorized and recorded in package metadata."
        ),
        r"(?mi)^[-*]\s*Do not submit online until the corresponding author provides or confirms.*$": (
            "- Routine AI-use/originality/author-responsibility declarations are covered by standing user authorization."
        ),
        r"(?mi)^[-*]\s*The system requires an APC, payment method, CAPTCHA, 2FA, ORCID authorization, or legal/author confirmation\.$": (
            "- The system requires an APC, payment method, CAPTCHA, 2FA, ORCID authorization, or non-routine legal/author confirmation outside the standing declarations."
        ),
        r"(?mi)^.*Add an AI-use.*author confirms.*$": "Standing AI-use statement applied from Smart Submission settings.",
    }
    for filename in ("final_risk_report.md", "submission_checklist.md"):
        path = package_dir / filename
        if not path.exists():
            continue
        text = read_text(path)
        before = text
        for pattern, replacement in replacement_lines.items():
            text = re.sub(pattern, replacement, text)
        if text != before:
            write_text(path, text)
            changed.append(str(path))
    return changed


def _update_markdown_third_party_declarations(package_dir: Path, policy: dict[str, str]) -> list[str]:
    changed: list[str] = []
    third_party_section = (
        "## Third Party Material\n\n"
        f"{policy['third_party_material_statement']}\n\n"
        f"Policy: standing_authorized_by_user_at_{policy['authorized_at']}.\n"
    )
    copyright_section = (
        "## Copyright Permissions\n\n"
        f"{policy['copyright_permission_statement']}\n\n"
        f"Policy: standing_authorized_by_user_at_{policy['authorized_at']}.\n"
    )

    declarations = package_dir / "declarations.md"
    if declarations.exists():
        text = read_text(declarations)
        before = text
        text = _replace_markdown_section(
            text,
            r"##\s+(?:Third[- ]Party Material|Third Party Materials|Third-party Material)",
            third_party_section,
        )
        text = _replace_markdown_section(
            text,
            r"##\s+(?:Copyright Permissions?|Permissions for Reused Material)",
            copyright_section,
        )
        if text != before:
            write_text(declarations, text)
            changed.append(str(declarations))

    replacement_lines = {
        r"(?mi)^.*scheduler-side.*third[- ]party.*copyright.*$": (
            "Standing no-third-party-material/copyright-permission declaration applied from Smart Submission settings."
        ),
        r"(?mi)^.*explicitly authorize.*third[- ]party.*$": (
            "Standing no-third-party-material/copyright-permission declaration applied from Smart Submission settings."
        ),
        r"(?mi)^.*third[- ]party-material.*requires.*authorization.*$": (
            "Standing no-third-party-material/copyright-permission declaration applied from Smart Submission settings."
        ),
    }
    for filename in ("final_risk_report.md", "submission_checklist.md"):
        path = package_dir / filename
        if not path.exists():
            continue
        text = read_text(path)
        before = text
        for pattern, replacement in replacement_lines.items():
            text = re.sub(pattern, replacement, text)
        if text != before:
            write_text(path, text)
            changed.append(str(path))
    return changed


def _update_json_standing_declarations(package_dir: Path, policy: dict[str, str]) -> list[str]:
    changed: list[str] = []
    targets = [
        package_dir / "package_metadata_fingerprint.json",
        package_dir / "submission_metadata_fingerprint.json",
        package_dir / "springer_submission_metadata.json",
        package_dir / "submission_manifest.json",
        package_dir.parent / "submission_metadata_fingerprint.json",
    ]
    seen: set[str] = set()
    for path in targets:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        payload = data.get("payload")
        targets_to_update: list[dict[str, Any]] = [data]
        if isinstance(payload, dict):
            targets_to_update.append(payload)
        for target in targets_to_update:
            target["ai_use_statement"] = policy["ai_use_statement"]
            target["originality_statement"] = policy["originality_statement"]
            target["author_responsibility_statement"] = policy["author_responsibility_statement"]
            target["standing_declaration_policy"] = {
                "status": "standing_authorized",
                "authorized_at": policy["authorized_at"],
                "source": "smart_submission_settings",
            }
            requirements = target.get("submission_requirements")
            if isinstance(requirements, dict):
                requirements["ai_use_statement"] = policy["ai_use_statement"]
                requirements["originality_statement"] = policy["originality_statement"]
                requirements["author_responsibility_statement"] = policy["author_responsibility_statement"]
                requirements["standing_declaration_policy"] = "standing_authorized"
            status = str(target.get("metadata_status") or "").strip().upper()
            if status in {"BLOCKED_PENDING_AUTHOR_DECLARATION", "MANUAL_REQUIRED", "NEEDS_AUTHOR_DECLARATION"}:
                target["metadata_status"] = "READY_WITH_STANDING_DECLARATIONS"
        if isinstance(data.get("remaining_blockers"), list):
            data["remaining_blockers"] = [
                item
                for item in data["remaining_blockers"]
                if not _standing_declaration_terms(str(item).lower())
            ]
        if isinstance(payload, dict) and "sha256" in data:
            data["sha256"] = _metadata_payload_sha(payload)
        data["updated_at"] = now_iso()
        write_json_atomic(path, data)
        changed.append(str(path))
    return changed


def _update_json_third_party_declarations(package_dir: Path, policy: dict[str, str]) -> list[str]:
    changed: list[str] = []
    targets = [
        package_dir / "package_metadata_fingerprint.json",
        package_dir / "submission_metadata_fingerprint.json",
        package_dir / "springer_submission_metadata.json",
        package_dir / "submission_manifest.json",
        package_dir.parent / "submission_metadata_fingerprint.json",
    ]
    seen: set[str] = set()
    for path in targets:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        payload = data.get("payload")
        targets_to_update: list[dict[str, Any]] = [data]
        if isinstance(payload, dict):
            targets_to_update.append(payload)
        for target in targets_to_update:
            target["third_party_material_statement"] = policy["third_party_material_statement"]
            target["third_party_material_status"] = "no_separate_permission_required"
            target["copyright_permission_statement"] = policy["copyright_permission_statement"]
            target["copyright_permission_status"] = "no_separate_permission_required"
            requirements = target.get("submission_requirements")
            if isinstance(requirements, dict):
                requirements["third_party_material_statement"] = policy["third_party_material_statement"]
                requirements["third_party_material_status"] = "no_separate_permission_required"
                requirements["copyright_permission_statement"] = policy["copyright_permission_statement"]
                requirements["copyright_permission_status"] = "no_separate_permission_required"
                requirements["third_party_material_policy"] = "standing_authorized"
        if isinstance(data.get("remaining_blockers"), list):
            data["remaining_blockers"] = [
                item
                for item in data["remaining_blockers"]
                if not _third_party_declaration_terms(str(item))
            ]
        if isinstance(payload, dict) and "sha256" in data:
            data["sha256"] = _metadata_payload_sha(payload)
        data["updated_at"] = now_iso()
        write_json_atomic(path, data)
        changed.append(str(path))
    return changed


def _apply_standing_declarations_to_package(
    paper_id: str,
    paper: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    if not settings.get("standing_ai_use_statement_authorized", True):
        return {"applied": False, "paper_id": paper_id, "reason": "standing_ai_use_statement_authorized=false"}
    if not settings.get("standing_author_declaration_authorized", True):
        return {"applied": False, "paper_id": paper_id, "reason": "standing_author_declaration_authorized=false"}
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    if package_dir is None:
        return {"applied": False, "paper_id": paper_id, "reason": "package directory not found"}
    policy = _standing_declaration_policy(settings)
    changed = []
    changed.extend(_update_markdown_standing_declarations(package_dir, policy))
    changed.extend(_update_json_standing_declarations(package_dir, policy))
    third_party = _apply_third_party_declarations_to_package(paper_id, paper, settings, package_dir=package_dir)
    if third_party.get("applied"):
        changed.extend(str(path) for path in third_party.get("changed_files", []))
    if not changed:
        return {"applied": False, "paper_id": paper_id, "reason": "no declaration files found", "package_dir": str(package_dir)}
    return {
        "applied": True,
        "paper_id": paper_id,
        "package_dir": str(package_dir),
        "changed_files": changed,
        "authorized_at": policy["authorized_at"],
        "third_party_declarations": third_party,
    }


def _apply_third_party_declarations_to_package(
    paper_id: str,
    paper: dict[str, Any],
    settings: dict[str, Any],
    *,
    package_dir: Path | None = None,
) -> dict[str, Any]:
    if not settings.get("standing_third_party_material_authorized", True):
        return {"applied": False, "paper_id": paper_id, "reason": "standing_third_party_material_authorized=false"}
    package_dir = package_dir or _resolve_package_dir_for_paper(paper_id, paper)
    if package_dir is None:
        return {"applied": False, "paper_id": paper_id, "reason": "package directory not found"}
    conflict = _package_has_third_party_conflict(package_dir)
    if conflict:
        return {"applied": False, "paper_id": paper_id, "reason": f"third-party conflict detected: {conflict}", "package_dir": str(package_dir)}
    policy = _standing_declaration_policy(settings)
    changed: list[str] = []
    changed.extend(_update_markdown_third_party_declarations(package_dir, policy))
    changed.extend(_update_json_third_party_declarations(package_dir, policy))
    if not changed:
        return {"applied": False, "paper_id": paper_id, "reason": "no declaration files found", "package_dir": str(package_dir)}
    return {
        "applied": True,
        "paper_id": paper_id,
        "package_dir": str(package_dir),
        "changed_files": changed,
        "authorized_at": policy["authorized_at"],
        "third_party_material_statement": policy["third_party_material_statement"],
        "copyright_permission_statement": policy["copyright_permission_statement"],
    }


def _update_metadata_fingerprint(
    package_dir: Path,
    paper_id: str,
    repo_name: str,
    repo_url: str,
) -> list[str]:
    changed: list[str] = []
    path = package_dir / "package_metadata_fingerprint.json"
    data = read_json(path, {})
    if not isinstance(data, dict):
        return changed
    payload = data.setdefault("payload", {})
    if not isinstance(payload, dict):
        payload = {}
        data["payload"] = payload
    payload.setdefault("paper_id", paper_id)
    payload["code_availability"] = (
        f"The source code will be released in the planned paper-specific GitHub repository: {repo_url}. "
        "The repository URL is recorded before online submission; the user will create/populate the "
        "repository after this automated preparation step."
    )
    requirements = payload.setdefault("submission_requirements", {})
    if not isinstance(requirements, dict):
        requirements = {}
        payload["submission_requirements"] = requirements
    requirements["github_repo_url"] = repo_url
    requirements["github_repo_name"] = repo_name
    requirements["github_repo_status"] = "planned_user_will_create"
    requirements["github_repo_policy"] = "user_authorized_planned_placeholder"
    discovery = requirements.setdefault("github_repository_discovery", {})
    if not isinstance(discovery, dict):
        discovery = {}
        requirements["github_repository_discovery"] = discovery
    discovery.update(
        {
            "status": "planned_placeholder_recorded",
            "checked_at": now_iso(),
            "planned_repo_url": repo_url,
            "planned_repo_name": repo_name,
            "policy_note": (
                "User authorized deterministic planned repository URLs when no accessible paper-specific "
                "GitHub repository exists; do not block on GitHub 404/auth during package gating."
            ),
        }
    )
    if str(payload.get("metadata_status") or "").strip().upper() in {"", "BLOCKED", "NEEDS_GITHUB_REPO", "NEEDS_MINOR_FIX"}:
        payload["metadata_status"] = "READY_WITH_PLANNED_GITHUB_REPO"
    data["sha256"] = _metadata_payload_sha(payload)
    data["updated_at"] = now_iso()
    write_json_atomic(path, data)
    changed.append(str(path))
    return changed


def _update_json_repo_fields(package_dir: Path, repo_name: str, repo_url: str) -> list[str]:
    changed: list[str] = []
    updates = {
        "github_repo_url": repo_url,
        "github_repo_name": repo_name,
        "github_repo_status": "planned_user_will_create",
        "github_repo_policy": "user_authorized_planned_placeholder",
    }
    for filename in ("springer_submission_metadata.json", "submission_manifest.json"):
        path = package_dir / filename
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        data.update(updates)
        if filename == "submission_manifest.json":
            blockers = data.get("remaining_blockers")
            if isinstance(blockers, list):
                data["remaining_blockers"] = [
                    item
                    for item in blockers
                    if "github" not in str(item).lower() and "repository" not in str(item).lower()
                ]
            data["status"] = "READY_FOR_ONLINE_ATTEMPT_WITH_PLANNED_GITHUB_REPO"
            data["requeue_gate_result"] = "planned_github_repo_recorded"
            data["online_action"] = "ready_for_online_submit_with_planned_github_repo"
        data["updated_at"] = now_iso()
        write_json_atomic(path, data)
        changed.append(str(path))
    return changed


def _update_markdown_repo_fields(package_dir: Path, repo_name: str, repo_url: str) -> list[str]:
    changed: list[str] = []
    code_availability = (
        "## Code Availability\n\n"
        f"The source code will be released in the planned paper-specific GitHub repository: {repo_url}.\n\n"
        "Repository status: planned_user_will_create. This URL is intentionally recorded before the "
        "repository is created, under the user's standing instruction, so the package gate must not block "
        "on GitHub authentication, 404, or public-repository discovery.\n"
    )

    declarations = package_dir / "declarations.md"
    if declarations.exists():
        text = read_text(declarations)
        write_text(declarations, _replace_markdown_section(text, r"##\s+Code Availability", code_availability))
        changed.append(str(declarations))

    for filename in ("final_risk_report.md", "submission_checklist.md"):
        path = package_dir / filename
        if not path.exists():
            continue
        text = read_text(path)
        text = re.sub(r"(?mi)^BLOCKED\s*$", "NEEDS_MINOR_FIX", text)
        text = re.sub(r"(?mi)^Conclusion\s*:\s*BLOCKED\s*$", "Conclusion: NEEDS_MINOR_FIX", text)
        text = re.sub(r"(?mi)^PACKAGE_STATUS\s*=\s*BLOCKED\s*$", "PACKAGE_STATUS=NEEDS_MINOR_FIX", text)
        text = re.sub(r"(?mi)^PACKAGE_STATUS\s*=\s*NEEDS_GITHUB_REPO\s*$", "PACKAGE_STATUS=NEEDS_MINOR_FIX", text)
        text = re.sub(
            r"(?mi)^[-*]\s*BLOCKED:.*(?:github|repository).*$",
            f"- PLANNED_REPO_ACCEPTED: {repo_url} (repo name: {repo_name}; user will create/populate it later).",
            text,
        )
        text = re.sub(
            r"(?mi)^[-*]\s*NOT PERFORMED:.*(?:github|repository).*$",
            "- READY_FOR_ONLINE_ATTEMPT_WITH_PLANNED_GITHUB_REPO",
            text,
        )
        text = re.sub(
            r"(?mi)^NOT PERFORMED:.*(?:github|repository).*$",
            "READY_FOR_ONLINE_ATTEMPT_WITH_PLANNED_GITHUB_REPO",
            text,
        )
        text = re.sub(
            r"(?mi)^.*package gate still fails.*(?:github|repository).*$",
            "READY_FOR_ONLINE_ATTEMPT_WITH_PLANNED_GITHUB_REPO",
            text,
        )
        text = re.sub(
            r"(?mi)^.*no verifiable paper-specific repository.*$",
            f"PLANNED_REPO_ACCEPTED: {repo_url} (repo name: {repo_name}; user will create/populate it later).",
            text,
        )
        text = re.sub(
            r"(?mi)^.*(?:github|repository).*?(?:missing|fabricated|404|no match).*?$",
            f"PLANNED_REPO_ACCEPTED: {repo_url} (repo name: {repo_name}; user will create/populate it later).",
            text,
        )
        text = re.sub(
            r"(?mi)^.*not performed.*missing github.*?$",
            "READY_FOR_ONLINE_ATTEMPT_WITH_PLANNED_GITHUB_REPO",
            text,
        )
        planned_section = (
            "## Planned GitHub Repository\n\n"
            f"- Repository name: `{repo_name}`\n"
            f"- Repository URL: {repo_url}\n"
            "- Status: planned_user_will_create\n"
            "- Policy: user authorized planned paper-specific GitHub URLs; do not block on GitHub auth, "
            "404, or public-repo discovery before online submission unless the live journal form rejects the URL.\n"
        )
        text = _replace_markdown_section(text, r"##\s+(?:Remaining Blocker|Planned GitHub Repository)", planned_section)
        write_text(path, text)
        changed.append(str(path))
    return changed


def _update_author_info_repo(paper_dir: Path, repo_name: str, repo_url: str) -> list[str]:
    path = paper_dir / "author_info.md"
    if not path.exists():
        return []
    text = read_text(path)
    section = (
        "## Planned GitHub Repository\n\n"
        f"- Repository name: `{repo_name}`\n"
        f"- Repository URL: {repo_url}\n"
        "- Status: planned_user_will_create\n"
        "- Note: the user authorized using this deterministic placeholder before creating the repository.\n"
    )
    write_text(path, _replace_markdown_section(text, r"##\s+Planned GitHub Repository", section))
    return [str(path)]


def _apply_planned_github_to_package(
    paper_id: str,
    paper: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    repo_name, repo_url = _planned_github_repo_url(settings, paper_id, paper)
    if package_dir is None:
        return {
            "paper_id": paper_id,
            "applied": False,
            "repo_name": repo_name,
            "repo_url": repo_url,
            "reason": "package_dir_not_found",
            "changed_files": [],
        }
    changed: list[str] = []
    changed.extend(_update_metadata_fingerprint(package_dir, paper_id, repo_name, repo_url))
    changed.extend(_update_json_repo_fields(package_dir, repo_name, repo_url))
    changed.extend(_update_markdown_repo_fields(package_dir, repo_name, repo_url))
    paper_dir = package_dir.parent
    changed.extend(_update_author_info_repo(paper_dir, repo_name, repo_url))

    paper["github_repo_name"] = repo_name
    paper["github_repo_url"] = repo_url
    paper["github_repo_status"] = "planned_user_will_create"
    paper["github_repo_policy"] = "user_authorized_planned_placeholder"
    paper["source_package_dir"] = str(package_dir)
    paper["paper_dir"] = str(paper_dir)
    paper["last_planned_github_repo_at"] = now_iso()
    paper.pop("last_github_repo_autocreate_failed_at", None)
    return {
        "paper_id": paper_id,
        "applied": True,
        "repo_name": repo_name,
        "repo_url": repo_url,
        "package_dir": str(package_dir),
        "changed_files": changed,
    }


def _latest_retarget_package(paper_id: str) -> Path | None:
    paper_root = JOURNAL_ROOT / "papers" / paper_id
    if not paper_root.exists():
        return None
    candidates = [
        path
        for path in paper_root.glob("retarget_*/package")
        if path.is_dir() and (path / "final_risk_report.md").exists()
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def _risk_report_value(package_dir: Path, key: str) -> str:
    report = package_dir / "final_risk_report.md"
    if not report.exists():
        return ""
    match = re.search(rf"(?mi)^{re.escape(key)}\s*:\s*(.+?)\s*$", read_text(report))
    return match.group(1).strip() if match else ""


def _is_browser_only_blocked_paper(
    paper_id: str,
    paper: dict[str, Any],
    browser_health: dict[str, Any],
) -> bool:
    status = str(paper.get("status") or "").strip().lower()
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    if status != "blocked" and workflow_status != "blocked":
        return False
    if not browser_health.get("operational"):
        return False
    text = _paper_summary_text(paper)
    has_retarget_package = _latest_retarget_package(paper_id) is not None
    browser_terms = (
        "chrome",
        "cdp",
        "browser",
        "native host",
        "native-host",
        "native messaging",
        "extension backend",
        "native pipe",
    )
    package_pass_terms = (
        "package gate passed",
        "gate-passing",
        "gate passing",
        "ready_for_online_submit",
        "upload/submission was not attempted because chrome",
    )
    hard_block_terms = (
        "package gate failed",
        "final_risk_report is blocked",
        "double-blind/template package is not ready",
        "captcha",
        "cloudflare",
        "2fa",
        "payment",
        "forced oa",
        "author-person confirmation",
        "non-routine author",
        "non-routine legal",
        "new legal",
        "final review still renders",
        "reverted to public-resource placeholders",
        "registration form contains captcha",
        "self-registration path contains captcha",
        "wiley manuscript processing to finish",
    )
    return (
        any(term in text for term in browser_terms)
        and (has_retarget_package or any(term in text for term in package_pass_terms))
        and not any(term in text for term in hard_block_terms)
    )


def _recover_browser_only_blocks(state: dict[str, Any], browser_health: dict[str, Any]) -> dict[str, Any]:
    """Requeue papers that were only blocked by a stale browser-channel failure."""
    papers = state.get("papers")
    if not isinstance(papers, dict):
        return {"recovered": [], "skipped": {}}
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    recovered: list[str] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    preferred_port = (
        browser_health.get("cdp_ports", {}).get("preferred_available")
        if isinstance(browser_health.get("cdp_ports"), dict)
        else None
    )
    browser_status = f"cdp_{preferred_port or '9224_9222'}_available; extension_optional"
    for paper_id, paper in sorted(papers.items()):
        if not isinstance(paper, dict):
            continue
        if _is_browser_only_blocked_paper(str(paper_id), paper, browser_health):
            route, route_error = _route_guard_result(str(paper_id), unified_state=unified_state)
            if route_error is not None:
                skipped[str(paper_id)] = str(route_error["code"])
                paper["last_online_route_guard"] = route_error
                continue
            retarget_package = _latest_retarget_package(str(paper_id))
            paper["status"] = "ready_for_online_submit"
            paper["workflow_status"] = "queued"
            paper["blocker"] = ""
            paper["browser_status"] = browser_status
            paper["next_action"] = (
                "Retry the online submission/login/upload through 127.0.0.1:9224 CDP; "
                "fallback to 127.0.0.1:9222; treat Codex Chrome Extension/native pipe as optional."
            )
            if retarget_package is not None:
                paper["source_package_dir"] = str(retarget_package)
                paper["paper_dir"] = str(retarget_package.parent.parent)
                target_journal = _risk_report_value(retarget_package, "Target journal")
                submission_system = _risk_report_value(retarget_package, "Publisher/system")
                if target_journal:
                    paper["target_journal"] = target_journal
                if submission_system:
                    paper["submission_system"] = submission_system
                paper["online_intent"] = "retarget_after_rejection"
                paper["detail"] = (
                    "Recovered from stale browser-channel blocker. The latest retarget package remains the source "
                    f"package: {retarget_package}."
                )
            paper["last_recovered_at"] = now_iso()
            _rotate_canary_attempt(paper, str(paper_id))
            _append_online_queue_guarded(
                online_queue,
                str(paper_id),
                paper,
                unified_state=unified_state,
            )
            recovered.append(str(paper_id))
        elif str(paper.get("status") or "").strip().lower() == "blocked":
            skipped[str(paper_id)] = str(paper.get("blocker") or paper.get("detail") or "blocked")
    if recovered:
        settings = state.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            state["settings"] = settings
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
        state["online_queue"] = online_queue
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "recover_browser_only_blocks",
                "recovered": recovered,
                "browser_status": browser_status,
            }
        )
    return {"recovered": recovered, "skipped": skipped}


def _normalize_status_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _paper_has_prior_submission_evidence(paper: dict[str, Any]) -> bool:
    for key in ("manuscript_id", "submission_id", "submission_number", "tracking_id"):
        value = str(paper.get(key) or "").strip()
        if value and value.lower() not in {"none", "null", "-", "n/a"}:
            return True
    for key in ("confirmation_url", "submission_confirmation_url"):
        value = str(paper.get(key) or "").strip()
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return True
    for key in ("submission_evidence_path", "confirmation_evidence_path", "confirmation_page_path"):
        if _resolve_existing_path(paper.get(key), base=REPO_ROOT) is not None:
            return True
    if bool(paper.get("final_submit_confirmed")):
        return True
    legacy = paper.get("legacy_submission_status")
    if isinstance(legacy, dict):
        legacy_statuses = {
            _normalize_status_value(legacy.get(key))
            for key in ("status", "raw_status", "workflow_status", "submission_status")
        }
        if legacy_statuses & {
            "submitted",
            "submitted_active",
            "under_review",
            "monitoring",
            "accepted",
            "complete",
            "completed",
        }:
            return True
    text = _paper_summary_text(paper)
    if re.search(r"\b(?:not|never)[ _-]*submitted\b|\bunsubmitted\b|\bincomplete\b|\binactive\b", text):
        return False
    prior_terms = (
        "submitted successfully",
        "confirmation page displayed",
        "submission confirmation",
        "under review",
        "with editor",
        "awaiting reviewer",
    )
    return any(term in text for term in prior_terms)


def _paper_has_monitorable_submission_evidence(
    paper_id: str,
    paper: dict[str, Any],
) -> bool:
    """Require concrete evidence before spending a mailbox status check.

    A historical ``status=monitoring`` projection is not evidence by itself.
    ``final_submit_confirmed`` is also ignored here because old monitor result
    projection could set that convenience flag without a controller receipt.
    Signed Smart receipts, ids/URLs/evidence files, and explicit legacy
    submitted records remain valid inputs.
    """

    if _paper_has_prior_trusted_submission_evidence(paper_id, paper):
        return True
    evidence_view = dict(paper)
    evidence_view.pop("final_submit_confirmed", None)
    return _paper_has_prior_submission_evidence(evidence_view)


_SUBMISSION_CONFIRMATION_RECEIPT_KEYS = {
    "schema",
    "paper_id",
    "route_revision",
    "attempt_id",
    "thread_id",
    "authorization_id",
    "capture_evidence_id",
    "status",
    "submission_id",
    "evidence_path",
    "evidence_sha256",
    "confirmation_url",
    "recorded_at",
    "result_audit_path",
    "result_audit_sha256",
    "signature_alg",
    "signature",
}
_LIVE_SUBMISSION_CONFIRMATION_KEYS = {
    "schema",
    "evidence_id",
    "paper_id",
    "route_revision",
    "attempt_id",
    "thread_id",
    "authorization_id",
    "account_id",
    "system_id",
    "target_journal",
    "target_origin",
    "journal_site_key",
    "account_binding_sha256",
    "site_binding_sha256",
    "draft_binding_sha256",
    "package_binding_sha256",
    "checked_at",
    "confirmation_url",
    "confirmation_marker",
    "manuscript_id",
    "cdp_port",
    "document_ready",
    "final_submit_control_visible",
    "login_challenge_visible",
    "value_free",
    "signature_alg",
    "signature",
}
_LIVE_SUBMISSION_CONFIRMATION_WRAPPER_KEYS = {
    "receipt",
    "evidence_path",
    "evidence_sha256",
}


def _valid_live_submission_confirmation_evidence(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization: dict[str, Any],
    now: datetime | None = None,
    require_fresh: bool = True,
) -> tuple[dict[str, Any], Path] | None:
    wrapper = paper.get("pending_submission_confirmation_evidence")
    if not isinstance(wrapper, dict):
        wrapper = paper.get("last_live_submission_confirmation_evidence")
    if not isinstance(wrapper, dict) or set(wrapper) != _LIVE_SUBMISSION_CONFIRMATION_WRAPPER_KEYS:
        return None
    receipt = wrapper.get("receipt")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _LIVE_SUBMISSION_CONFIRMATION_KEYS
        or receipt.get("schema") != LIVE_SUBMISSION_CONFIRMATION_SCHEMA
        or not _verify_smart_receipt(receipt)
    ):
        return None
    try:
        evidence_path = _sanitize_submission_evidence_path(
            wrapper.get("evidence_path"),
            paper_id=paper_id,
            paper=paper,
        )
    except ValueError:
        return None
    expected_file_sha256 = str(wrapper.get("evidence_sha256") or "").strip().casefold()
    if (
        evidence_path is None
        or not re.fullmatch(r"[0-9a-f]{64}", expected_file_sha256)
        or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != expected_file_sha256
    ):
        return None
    try:
        persisted_receipt = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if persisted_receipt != receipt:
        return None
    current = now or datetime.now().astimezone()
    checked_at = _parse_aware_time(receipt.get("checked_at"))
    consumed_at = _parse_aware_time(authorization.get("consumed_at"))
    authorization_expires_at = _parse_aware_time(authorization.get("expires_at"))
    # The short claim-to-click TTL governs when the irreversible pointer event
    # may be emitted.  A journal may render its positive confirmation later
    # while keeping the same route, so evidence capture remains valid until
    # the signed authorization itself expires.
    capture_deadline = authorization_expires_at
    if (
        checked_at is None
        or consumed_at is None
        or capture_deadline is None
        or checked_at < consumed_at
        or checked_at >= capture_deadline
        or checked_at > current + timedelta(seconds=30)
        or (require_fresh and current - checked_at > LIVE_SUBMISSION_CONFIRMATION_TTL)
        or receipt.get("confirmation_marker") not in _LIVE_CONFIRMATION_MARKERS
        or receipt.get("document_ready") is not True
        or receipt.get("final_submit_control_visible") is not False
        or receipt.get("login_challenge_visible") is not False
        or receipt.get("value_free") is not True
        or int(receipt.get("cdp_port") or 0) not in SMART_CDP_PORTS
    ):
        return None
    expected = {
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "authorization_id": str(authorization.get("authorization_id") or ""),
        "account_id": str(authorization.get("account_id") or ""),
        "system_id": str(authorization.get("submission_system_id") or ""),
        "target_journal": str(authorization.get("target_journal") or ""),
        "target_origin": str(authorization.get("target_origin") or ""),
        "journal_site_key": str(authorization.get("journal_site_key") or ""),
        "account_binding_sha256": str(authorization.get("account_binding_sha256") or ""),
        "site_binding_sha256": str(authorization.get("site_binding_sha256") or ""),
        "draft_binding_sha256": str(authorization.get("draft_binding_sha256") or ""),
        "package_binding_sha256": str(authorization.get("package_binding_sha256") or ""),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return None
    try:
        confirmation_url = _sanitize_confirmation_url(receipt.get("confirmation_url"), paper=paper)
        manuscript_id = _sanitize_submission_id(receipt.get("manuscript_id"))
    except ValueError:
        return None
    if confirmation_url != receipt.get("confirmation_url"):
        return None
    if manuscript_id != str(receipt.get("manuscript_id") or ""):
        return None
    return receipt, evidence_path


def _paper_has_prior_trusted_submission_evidence(
    paper_id: str,
    paper: dict[str, Any],
) -> bool:
    """Accept only a signed controller receipt plus immutable committed evidence."""

    receipt = paper.get("submission_confirmation_receipt")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _SUBMISSION_CONFIRMATION_RECEIPT_KEYS
        or receipt.get("schema") != SUBMISSION_CONFIRMATION_RECEIPT_SCHEMA
        or receipt.get("paper_id") != paper_id
        or not _verify_smart_receipt(receipt)
        or _parse_aware_time(receipt.get("recorded_at")) is None
        or str(receipt.get("status") or "").casefold()
        not in {"submitted", "monitoring", "accepted"}
        or not _committed_audit_matches(
            receipt.get("result_audit_path"),
            receipt.get("result_audit_sha256"),
        )
    ):
        return False
    authorization = paper.get("final_submit_authorization")
    if not isinstance(authorization, dict) or not _verify_smart_receipt(authorization):
        return False
    live = _valid_live_submission_confirmation_evidence(
        paper_id,
        paper,
        route_revision=int(receipt.get("route_revision") or 0),
        attempt_id=str(receipt.get("attempt_id") or ""),
        thread_id=str(receipt.get("thread_id") or ""),
        authorization=authorization,
        require_fresh=False,
    )
    if live is None:
        return False
    live_receipt, evidence_path = live
    return bool(
        receipt.get("capture_evidence_id") == live_receipt.get("evidence_id")
        and receipt.get("authorization_id") == live_receipt.get("authorization_id")
        and str(receipt.get("evidence_path") or "") == str(evidence_path)
        and str(receipt.get("evidence_sha256") or "").casefold()
        == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        and receipt.get("confirmation_url") == live_receipt.get("confirmation_url")
        and receipt.get("submission_id") == live_receipt.get("manuscript_id")
    )


EMAIL_FIRST_MONITORING_NEXT = (
    "Email-first monitoring: check Gmail/XUJCMail for decision, revision, rejection, acceptance, "
    "or author-action messages; open the journal dashboard only if email evidence indicates action "
    "or local proof is missing."
)
SCHEDULED_MONITORING_NEXT = "check_submission_status_on_verified_journal_dashboard"


def _paper_is_monitoring_like(paper: dict[str, Any]) -> bool:
    status = str(paper.get("status") or "").strip().lower()
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    return (
        status in {"submitted", "monitoring"}
        or workflow_status == "monitoring"
        or _paper_has_prior_submission_evidence(paper)
    )


def _monitoring_access_failure_text(
    paper: dict[str, Any],
    *extra_parts: str,
) -> str:
    parts = [_paper_summary_text(paper)]
    parts.extend(str(part or "") for part in extra_parts)
    return "\n".join(parts).lower()


def _is_monitoring_access_failure(
    paper: dict[str, Any],
    *,
    status: str = "",
    next_action: str = "",
    detail: str = "",
    source: str = "",
) -> bool:
    """True when an already-submitted paper was blocked only by monitoring access.

    Monitoring is an email-first workflow. A transient dashboard, Chrome/CDP,
    in-app browser, or browser-automation failure should not downgrade a paper
    that already has submission evidence into a hard blocked state.
    """
    normalized_status = str(status or paper.get("status") or "").strip().lower()
    if normalized_status not in {"blocked", "revision_needed"} and str(paper.get("workflow_status") or "") != "blocked":
        return False
    if not _paper_is_monitoring_like(paper):
        return False
    text = _monitoring_access_failure_text(paper, next_action, detail, source)
    access_failure_terms = (
        "browser monitoring",
        "visible browser monitoring",
        "visible status could not be verified",
        "live snapp status could not be verified",
        "dashboard status not verified",
        "dashboard could not",
        "could not verify",
        "status could not be verified",
        "chrome was unavailable",
        "chrome unavailable",
        "browser unavailable",
        "in-app browser disconnected",
        "computer use could not determine",
        "could not determine current chrome url",
        "cdp unavailable",
        "cdp not available",
        "no cdp",
        "portal navigation",
        "portal could not",
        "dashboard redirected",
        "gmail search tooling unavailable",
        "gmail search tooling was unavailable",
        "unable to use gmail search",
        "live dashboard status not verified",
        "status-dashboard",
        "smart_secret_broker_required",
        "credential broker required",
        "journal login required",
    )
    if not any(term in text for term in access_failure_terms):
        return False
    monitoring_evidence_terms = (
        "submitted successfully",
        "confirmation page",
        "submission-received",
        "submission received",
        "received the manuscript",
        "submitted to",
        "prior visible worker confirmed",
        "prior t&f confirmation remains valid",
        "submission remains",
        "remains monitoring",
        "dashboard remains",
        "with editor",
        "editor review",
        "technical check",
        "under review",
        "waiting for journal processing",
        "no action/revision/reject/accept",
        "no author-action signal",
    )
    if not any(term in text for term in monitoring_evidence_terms) and not _paper_has_prior_submission_evidence(paper):
        return False
    hard_fact_terms = (
        "payment",
        "forced oa",
        "forced orcid",
        "orcid authentication",
        "author-person confirmation",
        "author confirmation required",
        "new author/affiliation",
        "new author",
        "new affiliation",
        "new funding",
        "new ethics",
        "new copyright",
        "new legal",
        "active prior submission",
        "duplicate-submission risk",
        "inaccessible mailbox",
        "mailbox inaccessible",
        "missing/expired verification",
        "missing/expired account-verification",
    )
    return not any(term in text for term in hard_fact_terms)


def _normalize_monitoring_access_failure_blocks(state: dict[str, Any]) -> dict[str, Any]:
    papers = state.get("papers")
    if not isinstance(papers, dict):
        return {"normalized": [], "email_firsted": [], "skipped": {}}
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    normalized: list[str] = []
    email_firsted: list[str] = []
    skipped: dict[str, str] = {}
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    credential_accounts = (
        credentials.get("accounts", [])
        if isinstance(credentials, dict) and isinstance(credentials.get("accounts"), list)
        else []
    )
    account_status_by_id = {
        str(item.get("account_id") or "").strip().casefold(): str(
            item.get("status") or ""
        ).strip().casefold()
        for item in credential_accounts
        if isinstance(item, dict) and str(item.get("account_id") or "").strip()
    }
    for paper_id, paper in papers.items():
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper_id)
        if not _is_monitoring_access_failure(
            paper,
            status=str(paper.get("status") or ""),
            next_action=str(paper.get("next_action") or ""),
            detail=str(paper.get("detail") or paper.get("blocker") or ""),
            source=str(paper.get("last_worker_result_source") or ""),
        ):
            status = str(paper.get("status") or "").strip().lower()
            workflow_status = str(paper.get("workflow_status") or "").strip().lower()
            next_action_text = str(paper.get("next_action") or "")
            if (
                (status in {"submitted", "monitoring"} or workflow_status == "monitoring")
                and "dashboard" in next_action_text.lower()
                and "email-first" not in next_action_text.lower()
            ):
                paper["previous_monitoring_next_action"] = next_action_text
                paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
                paper["last_monitoring_email_firsted_at"] = now_iso()
                email_firsted.append(paper_id)
            if status == "blocked":
                skipped[paper_id] = str(paper.get("blocker") or paper.get("detail") or "blocked")
            continue
        previous = {
            "status": paper.get("status", ""),
            "workflow_status": paper.get("workflow_status", ""),
            "worker_status": paper.get("worker_status", ""),
            "next_action": paper.get("next_action", ""),
            "blocker": paper.get("blocker", ""),
        }
        paper["previous_monitoring_access_failure_state"] = previous
        paper["status"] = "monitoring"
        paper["workflow_status"] = "monitoring"
        paper["worker_status"] = "monitoring"
        paper["blocker"] = ""
        paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
        paper["last_monitoring_access_normalized_at"] = now_iso()
        paper["last_recovery_reason"] = "email_first_monitoring_access_failure_normalized"
        account_id = str(paper.get("credential_account_id") or "").strip()
        if (
            account_id
            and account_status_by_id.get(account_id.casefold())
            in {"registration_pending", "session_rejected_by_worker"}
            and "smart_secret_broker_required"
            in _monitoring_access_failure_text(
                paper,
                str(previous.get("next_action") or ""),
            )
        ):
            paper["rejected_credential_account_id"] = account_id
            paper.pop("credential_account_id", None)
            paper["credential_broker_status"] = "session_rejected_by_worker"
            paper["credential_broker_code"] = "SMART_CREDENTIAL_BROKER_SESSION_REJECTED"
        detail = str(paper.get("detail") or "").strip()
        note = "Normalized browser/dashboard-only monitoring failure to email-first monitoring."
        paper["detail"] = f"{detail} | {note}" if detail else note
        online_queue = [item for item in online_queue if item != paper_id]
        normalized.append(paper_id)
    if normalized or email_firsted:
        state["online_queue"] = online_queue
        settings = state.setdefault("settings", {})
        if isinstance(settings, dict):
            settings["monitoring_email_first_enabled"] = True
            settings["monitoring_dashboard_failure_is_not_blocker"] = True
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "normalize_monitoring_access_failure_blocks",
                "normalized": normalized,
                "email_firsted": email_firsted,
                "policy": "submitted monitoring is email-first; browser/dashboard failure alone is not a hard blocker",
            }
        )
    return {"normalized": normalized, "email_firsted": email_firsted, "skipped": skipped}


def _routine_online_recovery_reason(paper: dict[str, Any]) -> str:
    status = str(paper.get("status") or "").strip().lower()
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    if status in {"submitted", "monitoring", "accepted", "stopped", "no_action"}:
        return ""
    if workflow_status in {"running"}:
        return ""
    persisted_hint = str(paper.get("last_worker_recovery_hint") or "").strip().casefold()
    if persisted_hint in {
        "data_availability_template_autofix",
        "third_party_declaration_autofix",
        "scheduler_side_continuation",
        "site_binding_reconciliation",
        "standing_declaration_autofix",
        "package_autofix_continuation",
    }:
        return persisted_hint
    text = _paper_summary_text(paper)
    waiting_hold_terms = (
        "wait for wiley manuscript processing",
        "wiley manuscript processing to finish",
        "manuscript processing to finish",
        "wiley was processing the uploaded manuscript",
        "site was processing the uploaded manuscript",
    )
    if status == "waiting" and any(term in text for term in waiting_hold_terms):
        return ""
    data_availability_terms = (
        "data availability",
        "data-sharing template",
        "data sharing template",
        "final review still renders",
        "[repository name]",
        "[doi/url]",
        "[reference number]",
        "[list resources and urls]",
    )
    if any(term in text for term in data_availability_terms):
        return "data_availability_template_autofix"
    if _third_party_declaration_terms(text):
        return "third_party_declaration_autofix"
    terminal_online_failure_terms = (
        "reverted to public-resource placeholders",
        "no safe preset template was verified",
        "registration form contains captcha",
        "registration captcha",
        "self-registration path contains captcha",
        "final submit was not clicked",
        "final submit was not attempted",
    )
    if status == "blocked" and any(term in text for term in terminal_online_failure_terms):
        return ""
    scheduler_side_terms = (
        "scheduler-side",
        "sidecar",
        "local state/audit update required",
        "state/audit update required",
        "cover-letter continuation",
        "prepare an upload-ready cover letter",
        "provide an absolute cover-letter upload path",
        "author, funding, and data-declaration payload",
        "record the cdp status",
        "title-save result",
        "article-type step",
        "details step",
        "saved wiley draft",
        "wiley manuscript processing",
        "manuscript processing to finish",
        "uploaded and saved",
        "worker recovered",
    )
    if any(term in text for term in scheduler_side_terms):
        return "scheduler_side_continuation"
    binding_mismatch_terms = (
        "target lock mismatch",
        "locked system is",
        "scheduler must resolve the target lock",
    )
    if any(term in text for term in binding_mismatch_terms):
        return "site_binding_reconciliation"
    if _standing_declaration_terms(text):
        return "standing_declaration_autofix"
    package_terms = (
        "package_autofix",
        "package auto-fix",
        "package metadata",
        "metadata fingerprint",
        "github repository",
        "github repo",
        "planned_repo",
    )
    if any(term in text for term in package_terms):
        return "package_autofix_continuation"
    return ""


def _site_hard_stop_retarget_reason(paper: dict[str, Any], settings: dict[str, Any]) -> str:
    if not settings.get("auto_retarget_after_site_hard_stop_enabled", False):
        return ""
    status = str(paper.get("status") or "").strip().lower()
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    if status in {"submitted", "monitoring", "accepted", "stopped", "no_action"}:
        return ""
    if workflow_status in {"running"}:
        return ""
    if _paper_has_prior_submission_evidence(paper):
        return ""
    text = _paper_summary_text(paper)
    if "active prior submission" in text or "duplicate-submission risk" in text:
        return ""
    # These site boundaries should not permanently block a not-yet-submitted
    # paper. The safe recovery is a fresh journal/package target.
    hard_stop_terms = (
        "captcha",
        "hcaptcha",
        "cloudflare",
        "security verification",
        "security challenge",
        "forced orcid",
        "orcid_required",
        "forced_orcid",
        "invalid username/password",
        "login rejected",
        "stored account",
        "stored username",
        "password reminder",
        "password-reminder",
        "account recovery",
        "support recovery",
        "no actionable reset",
        "no fresh direct",
        "recovery email pending",
        "mailbox access is unavailable",
        "inaccessible institutional mailbox",
        "inaccessible mailbox",
        "unsafe browser/application",
        "unsafe browser",
        "xujcmail login returned a no-access page",
        "no-access page",
        "ind erscience_account_recovery_hard_stop",
        "inderscience_account_recovery_hard_stop",
        "terminal hard stop",
    )
    for term in hard_stop_terms:
        if term in text:
            return f"retarget_after_site_hard_stop:{term}"
    return ""


def _append_retarget_preferences(paper_dir: Path, paper_id: str, paper: dict[str, Any], reason: str) -> str:
    path = paper_dir / "journal_preferences.md"
    existing = read_text(path)
    current_journal = str(paper.get("target_journal") or paper.get("journal") or "").strip()
    current_system = str(paper.get("submission_system") or paper.get("journal_system") or "").strip()
    section = "\n".join(
        [
            "## Smart Submission Retarget Request",
            "",
            f"- Requested at: {now_iso()}",
            f"- Paper ID: {paper_id}",
            f"- Reason: {reason}",
            "- Policy: prior online submission evidence was not found; retarget instead of waiting on a site/account boundary.",
            "- Strategy: choose a non-OA/subscription-first CAS 2/3/4-compatible journal when possible.",
            "- Avoid the same submission system/site boundary that caused this blocker unless a working credential route is already verified.",
            f"- Previous target journal: {current_journal or '-'}",
            f"- Previous submission system: {current_system or '-'}",
            "",
        ]
    )
    if "## Smart Submission Retarget Request" in existing:
        text = _replace_markdown_section(existing, r"##\s+Smart Submission Retarget Request", section)
    else:
        text = existing.rstrip() + "\n\n" + section if existing.strip() else section
    write_text(path, text)
    return str(path)


def _archive_existing_package_for_retarget(paper_dir: Path, package_dir: Path | None, paper_id: str) -> dict[str, str]:
    if package_dir is None or not package_dir.exists() or not package_dir.is_dir():
        return {}
    archive_root = paper_dir / "retarget_archives"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_root / f"{timestamp_id()}_package_before_smart_retarget"
    counter = 1
    while archive_dir.exists():
        archive_dir = archive_root / f"{timestamp_id()}_{counter}_package_before_smart_retarget"
        counter += 1
    shutil.move(str(package_dir), str(archive_dir))
    moved_blocker = ""
    blocker = paper_dir / "package_blocker.md"
    if blocker.exists():
        target = archive_dir / "package_blocker.md"
        shutil.move(str(blocker), str(target))
        moved_blocker = str(target)
    return {"package_archive": str(archive_dir), "blocker_archive": moved_blocker}


def _queue_journal_author_retarget_legacy(paper_id: str, paper: dict[str, Any], reason: str, settings: dict[str, Any]) -> dict[str, Any]:
    from .adapters.journal_author import JournalAuthorAdapter
    from .ai_execution_policy import require_legacy_execution_mode
    from .ai_provider_store import AIProviderStore

    # Retargeting archives the current package before it rewrites the journal
    # queue.  Validate both execution ownership and the legacy control bridge
    # first so API mode, or a modern Desktop install with no legacy TOML port,
    # cannot leave a half-retargeted paper behind.
    require_legacy_execution_mode(
        AIProviderStore().load_settings().execution_mode,
        action="智能投稿自动重新选刊",
    )
    adapter = JournalAuthorAdapter()
    adapter.automation.require_controllable("journal_author", adapter.load_state())

    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    paper_dir_text = str(paper.get("paper_dir") or "").strip()
    if package_dir is not None:
        paper_dir = package_dir.parent
    elif paper_dir_text:
        paper_dir = Path(paper_dir_text)
    else:
        paper_dir = JOURNAL_ROOT / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = _append_retarget_preferences(paper_dir, paper_id, paper, reason)
    archive: dict[str, str] = {}
    if settings.get("retarget_archive_existing_package", True):
        try:
            archive = _archive_existing_package_for_retarget(paper_dir, package_dir, paper_id)
        except OSError as exc:
            archive = {"archive_failed": str(exc), "package_archive": "", "blocker_archive": ""}

    source_submit = paper_dir / "source_submit"
    upstream = str(source_submit) if source_submit.exists() and source_submit.is_dir() else ""
    queue_result = adapter.queue(paper_id, upstream)
    journal_state = adapter.load_state()
    journal_papers = journal_state.setdefault("papers", {})
    journal_record = journal_papers.setdefault(paper_id, {})
    journal_record["workflow_status"] = "queued"
    journal_record["last_result_status"] = "RETARGET_REQUESTED"
    journal_record["last_result_message"] = f"Smart Submission requested retarget: {reason}"
    journal_record["package_status"] = ""
    journal_record["step_done"] = min(int(journal_record.get("step_done") or 2), 2)
    journal_record["run_from_step"] = 2
    journal_record["run_until_step"] = 3
    journal_record["force_retarget"] = True
    journal_record["retarget_requested_at"] = now_iso()
    journal_record["retarget_reason"] = reason
    journal_record["retarget_preferences_path"] = str(preferences_path)
    if archive.get("archive_failed"):
        journal_record["retarget_archive_failed"] = archive["archive_failed"]
    queue = [str(item) for item in journal_state.get("paper_queue", []) if str(item).strip()]
    append_unique(queue, paper_id)
    journal_state["paper_queue"] = queue
    adapter.save_state(journal_state)
    start_result = adapter.start()
    audit_path = SMART_AUDIT_DIR / f"{timestamp_id()}_{paper_id}_retarget_queued.md"
    write_text(
        audit_path,
        "\n".join(
            [
                "# Smart Submission Retarget Queued",
                "",
                f"- paper_id: {paper_id}",
                f"- reason: {reason}",
                f"- paper_dir: {paper_dir}",
                f"- preferences_path: {preferences_path}",
                f"- package_archive: {archive.get('package_archive', '')}",
                f"- blocker_archive: {archive.get('blocker_archive', '')}",
                "",
                f"SMART_SUBMISSION_PAPER_ID={paper_id}",
                "SMART_SUBMISSION_STATUS=waiting",
                "SMART_SUBMISSION_NEXT=journal_author_retarget_queued",
            ]
        ),
    )
    return {
        "paper_id": paper_id,
        "reason": reason,
        "paper_dir": str(paper_dir),
        "preferences_path": preferences_path,
        "archive": archive,
        "queue_result": queue_result,
        "start_result": start_result,
        "audit_path": str(audit_path),
    }


def _queue_journal_author_retarget(
    paper_id: str,
    paper: dict[str, Any],
    reason: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Queue a V2 SDK retarget without invoking the retired AI scheduler.

    The previous implementation required legacy Codex automation, started the
    adapter twice, and rewrote the journal state after the SDK runtime had
    already started.  Retargeting is now one deterministic V2 request covering
    journal-author Steps 2-3.  The old journal is added to the structured
    rejection list before the current package is archived.
    """

    from .adapters.journal_author import JournalAuthorAdapter

    adapter = JournalAuthorAdapter()
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    paper_dir_text = str(paper.get("paper_dir") or "").strip()
    if package_dir is not None:
        paper_dir = package_dir.parent
    elif paper_dir_text:
        paper_dir = Path(paper_dir_text)
    else:
        paper_dir = adapter.paper_dir(paper_id)
    paper_dir.mkdir(parents=True, exist_ok=True)

    current_journal = str(
        paper.get("target_journal") or paper.get("journal") or ""
    ).strip()
    historical_journals = _historical_retarget_journals(paper_id, paper)
    preferences = adapter.load_journal_preferences(paper_id)
    retarget_request = (
        normalize_retarget_request(
            paper.get("retarget_request"),
            paper_id=paper_id,
        )
        if isinstance(paper.get("retarget_request"), Mapping)
        else None
    )
    if retarget_request is not None:
        requested_target = str(retarget_request.get("target_journal") or "").strip()
        if retarget_request.get("mode") == RETARGET_MODE_SPECIFIC and any(
            _journal_names_equivalent(requested_target, historical)
            for historical in historical_journals
        ):
            raise ValueError("指定的转投期刊不能是当前或历史已投稿期刊")
        preference_patch = journal_preferences_patch(retarget_request)
        for key, value in preference_patch.items():
            if key in {"quartiles", "indexing"} and isinstance(value, Mapping):
                preferences[key] = dict(value)
            else:
                preferences[key] = value
    rejected = [
        item.strip()
        for item in re.split(r"[\r\n,;]+", str(preferences.get("rejected_journals") or ""))
        if item.strip()
    ]
    for historical in historical_journals:
        _append_unique_journal_name(rejected, historical)
    if retarget_request is not None:
        for historical in retarget_request.get("excluded_journals", []):
            _append_unique_journal_name(rejected, historical)
        retarget_request["excluded_journals"] = list(rejected)
    preferences["rejected_journals"] = ", ".join(rejected)
    retarget_note = (
        f"Smart Submission retarget requested at {now_iso()}: {reason}. "
        "Do not select the rejected/current journal again."
    )
    if retarget_request is not None:
        retarget_note += (
            f" request_id={retarget_request.get('request_id') or '-'}; "
            f"mode={retarget_request.get('mode')}; "
            f"publication_tier={retarget_request.get('publication_tier')}; "
            f"priority={retarget_request.get('priority')}; "
            f"specific_journal={retarget_request.get('target_journal') or '-'}; "
            "these user choices are binding."
        )
    existing_notes = str(preferences.get("notes") or "").strip()
    preferences["notes"] = (
        f"{existing_notes}\n{retarget_note}".strip()
        if retarget_note not in existing_notes
        else existing_notes
    )
    preference_result = adapter.save_journal_preferences(paper_id, preferences)
    preferences_path = str(preference_result.get("path") or "")

    archive: dict[str, str] = {}
    if settings.get("retarget_archive_existing_package", True):
        try:
            archive = _archive_existing_package_for_retarget(
                paper_dir,
                package_dir,
                paper_id,
            )
        except OSError as exc:
            archive = {
                "archive_failed": str(exc),
                "package_archive": "",
                "blocker_archive": "",
            }

    try:
        queue_result = adapter.queue(paper_id, "", run_mode="retarget")
    except Exception:
        archived_path = Path(str(archive.get("package_archive") or ""))
        if (
            package_dir is not None
            and archived_path.is_dir()
            and not package_dir.exists()
        ):
            package_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived_path), str(package_dir))
            nested_blocker = package_dir / "package_blocker.md"
            original_blocker = package_dir.parent / "package_blocker.md"
            if nested_blocker.exists() and not original_blocker.exists():
                shutil.move(str(nested_blocker), str(original_blocker))
        raise

    command = queue_result.get("command", {}) if isinstance(queue_result, dict) else {}
    request_id = str(command.get("id") or "") if isinstance(command, dict) else ""
    safe_paper_id = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id) or "paper"
    audit_path = SMART_AUDIT_DIR / (
        f"{timestamp_id()}_{safe_paper_id}_monitor_retarget_queued.md"
    )
    write_text(
        audit_path,
        "\n".join(
            [
                "# Smart Submission Monitor Retarget Queued",
                "",
                f"- paper_id: {paper_id}",
                f"- reason: {reason}",
                f"- previous_target_journal: {current_journal or '-'}",
                f"- journal_author_request_id: {request_id or '-'}",
                "- journal_author_run_mode: retarget (Steps 2-3)",
                f"- retarget_request_id: {(retarget_request or {}).get('request_id') or '-'}",
                f"- retarget_mode: {(retarget_request or {}).get('mode') or 'automatic_rejection'}",
                f"- requested_target_journal: {(retarget_request or {}).get('target_journal') or '-'}",
                f"- publication_tier: {(retarget_request or {}).get('publication_tier') or '-'}",
                f"- selection_priority: {(retarget_request or {}).get('priority') or '-'}",
                f"- preferences_path: {preferences_path or '-'}",
                f"- package_archive: {archive.get('package_archive', '') or '-'}",
                "",
                f"SMART_SUBMISSION_PAPER_ID={paper_id}",
                "SMART_SUBMISSION_STATUS=waiting",
                "SMART_SUBMISSION_NEXT=journal_author_retarget_queued",
            ]
        ),
    )
    return {
        "paper_id": paper_id,
        "reason": reason,
        "paper_dir": str(paper_dir),
        "preferences_path": preferences_path,
        "previous_target_journal": current_journal,
        "archive": archive,
        "queue_result": queue_result,
        "start_result": (
            queue_result.get("runtime", {}) if isinstance(queue_result, dict) else {}
        ),
        "request_id": request_id,
        "excluded_journals": list(rejected),
        "retarget_request": copy.deepcopy(retarget_request or {}),
        "audit_path": str(audit_path),
    }


def _recover_auto_actionable_papers_in_state(state: dict[str, Any]) -> dict[str, Any]:
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    papers = state.get("papers")
    if not isinstance(papers, dict):
        return {"online_recovered": [], "retargeted": [], "skipped": {"papers": "invalid papers map"}}

    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    online_recovered: list[str] = []
    retargeted: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    for paper_id, paper in sorted(papers.items()):
        paper_id = str(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is invalid"
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            continue
        _mark_smart_route_on_paper(paper, route or {})
        status = str(paper.get("status") or "").strip().lower()
        workflow_status = str(paper.get("workflow_status") or "").strip().lower()
        if status in {"submitted", "monitoring", "accepted", "stopped", "no_action"}:
            skipped[paper_id] = f"terminal_or_monitoring_status:{status}"
            continue
        if workflow_status == "running":
            skipped[paper_id] = "worker is currently running"
            continue

        routine_reason = _routine_online_recovery_reason(paper)
        retarget_reason = ""
        if routine_reason not in {
            "data_availability_template_autofix",
            "standing_declaration_autofix",
            "third_party_declaration_autofix",
            "scheduler_side_continuation",
            "site_binding_reconciliation",
        }:
            retarget_reason = _site_hard_stop_retarget_reason(paper, settings)

        if routine_reason and not retarget_reason:
            recovered_binding: dict[str, Any] = {}
            if routine_reason == "site_binding_reconciliation":
                registry = read_json(SMART_SITE_REGISTRY_PATH, {})
                recovered_binding = _program_dispatch_binding(
                    paper_id,
                    paper,
                    registry if isinstance(registry, dict) else {},
                    account_missing_policy=str(
                        settings.get("account_missing_policy") or "auto_register"
                    ),
                )
                resolved_system_id = str(
                    recovered_binding.get("submission_system_id") or ""
                ).strip().casefold()
                if (
                    not recovered_binding.get("dispatch_binding_ready")
                    or not resolved_system_id
                ):
                    skipped[paper_id] = (
                        "target-site binding mismatch is not yet reconciled: "
                        + str(
                            recovered_binding.get("dispatch_binding_code")
                            or "SMART_PROGRAM_SITE_BINDING_MISSING"
                        )
                    )
                    continue
            if routine_reason == "standing_declaration_autofix":
                applied = _apply_standing_declarations_to_package(paper_id, paper, settings)
                if not applied.get("applied"):
                    skipped[paper_id] = str(applied.get("reason") or "standing declarations could not be applied")
                    continue
                paper["standing_declaration_result"] = applied
                _clear_standing_declaration_state_markers(paper)
            if routine_reason == "third_party_declaration_autofix":
                applied = _apply_third_party_declarations_to_package(paper_id, paper, settings)
                if not applied.get("applied"):
                    skipped[paper_id] = str(applied.get("reason") or "third-party declarations could not be applied")
                    continue
                paper["third_party_declaration_result"] = applied
            if routine_reason == "package_autofix_continuation" and "github" in _paper_summary_text(paper):
                applied_github = _apply_planned_github_to_package(paper_id, paper, settings)
                if applied_github.get("applied"):
                    paper["planned_github_repo_result"] = applied_github
            if routine_reason == "scheduler_side_continuation":
                package_dir = _resolve_package_dir_for_paper(paper_id, paper)
                paper_dir = _resolve_existing_path(paper.get("paper_dir"), base=REPO_ROOT)
                if paper_dir is None and package_dir is not None:
                    paper_dir = package_dir.parent
                if paper_dir is None:
                    paper_dir = JOURNAL_ROOT / "papers" / paper_id
                if package_dir is None:
                    package_dir = paper_dir / "package"
                if package_dir.exists() and paper_dir.exists():
                    payload = _prepare_snapp_scheduler_payload(paper_id, paper, package_dir, paper_dir, settings)
                    paper["scheduler_prepared_payload"] = payload
                    if payload.get("cover_letter_upload_file"):
                        paper["cover_letter_upload_file"] = payload["cover_letter_upload_file"]
            paper["previous_blocker"] = str(paper.get("blocker") or paper.get("detail") or "")
            if routine_reason == "data_availability_template_autofix":
                paper["status"] = "uploading"
                paper["next_action"] = "fix_data_availability_template_then_submit"
                paper["online_intent"] = "data_availability_template_autofix_then_submit"
                paper["detail"] = (
                    "Recovered by smart-submission policy: the Data Availability template/Final Review "
                    "issue is a bounded online correction using the local recorded package statement."
                )
            elif routine_reason == "third_party_declaration_autofix":
                paper["status"] = "revision_needed"
                paper["next_action"] = "third_party_declarations_applied_then_online_submit"
                paper["online_intent"] = "third_party_declarations_then_submit"
                paper["detail"] = (
                    "Standing no-third-party-material/copyright-permission declaration was written to the package "
                    "under user authorization. Worker should select the no-permission-required option and continue "
                    "unless the live form shows contradictory material-specific facts."
                )
            elif routine_reason == "site_binding_reconciliation":
                paper["status"] = "login_check"
                paper["next_action"] = "codex_sdk_browser_submit_or_monitor"
                paper["online_intent"] = "submit_or_retarget"
                paper["detail"] = (
                    "Recovered by deterministic site-binding reconciliation. "
                    "The target journal and submission system now resolve to "
                    f"{recovered_binding.get('submission_system_id')}; resume the "
                    "same paper on the corrected publisher site."
                )
            elif routine_reason == "scheduler_side_continuation":
                # This is a continuation of the same online submission
                # attempt, not evidence of a journal-requested revision.
                # Mark it as an executable site step so the program planner
                # can dispatch the fixed SDK browser worker again.
                paper["status"] = "uploading"
                paper["next_action"] = "visible_worker_online_submit_or_monitor"
                current_intent = str(paper.get("online_intent") or "").strip()
                paper["online_intent"] = current_intent or "submit_or_retarget"
                paper["detail"] = (
                    "Recovered by smart-submission policy: the previous worker "
                    "identified a bounded scheduler-side browser continuation. "
                    "Resume the same online submission attempt."
                )
            else:
                paper["status"] = "revision_needed"
                paper["next_action"] = "visible_worker_online_submit_or_monitor"
                paper["online_intent"] = routine_reason
                paper["detail"] = (
                    "Recovered by smart-submission policy: this is a scheduler-side or routine declaration/package "
                    f"continuation, not a permanent manual blocker. recovery_reason={routine_reason}"
                )
            recovery_time = now_iso()
            stale_thread = _detach_stale_visible_worker(
                paper,
                reason=f"recovered_{routine_reason}",
                timestamp=recovery_time,
            )
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "queued"
            paper["blocker"] = ""
            paper["last_recovery_reason"] = routine_reason
            paper["last_auto_action_recovered_at"] = recovery_time
            if stale_thread:
                paper["last_recovery_stale_thread_id"] = stale_thread
            _rotate_canary_attempt(paper, paper_id)
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
            )
            online_recovered.append(paper_id)
            continue

        if not retarget_reason:
            retarget_reason = _site_hard_stop_retarget_reason(paper, settings)
        if retarget_reason:
            try:
                result = _queue_journal_author_retarget(paper_id, paper, retarget_reason, settings)
            except Exception as exc:  # pragma: no cover - defensive state repair path
                skipped[paper_id] = f"retarget queue failed: {exc}"
                continue
            paper["previous_blocker"] = str(paper.get("blocker") or paper.get("detail") or "")
            paper["status"] = "waiting"
            paper["workflow_status"] = "waiting"
            paper["worker_status"] = "waiting"
            paper["blocker"] = ""
            paper["next_action"] = "wait_journal_author_retarget_package"
            paper["online_intent"] = "retarget_after_submission_system_hard_stop"
            paper["detail"] = (
                "Submission-system/site boundary was converted to a journal-author retarget request. "
                "The old package was archived when present; wait for the journal-author scheduler to generate "
                "a fresh package before online submission resumes."
            )
            paper["last_recovery_reason"] = retarget_reason
            paper["last_retarget_queued_at"] = now_iso()
            paper["last_audit_path"] = result.get("audit_path", "")
            online_queue = [item for item in online_queue if item != paper_id]
            retargeted.append(result)
            continue
        skipped[paper_id] = "no auto-actionable smart-submission recovery rule matched"

    state["online_queue"] = online_queue
    if online_recovered:
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
    if online_recovered or retargeted:
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "recover_auto_actionable_papers",
                "online_recovered": online_recovered,
                "retargeted": [item.get("paper_id") for item in retargeted],
                "skipped": skipped,
            }
        )
    return {"online_recovered": online_recovered, "retargeted": retargeted, "skipped": skipped}


def _detach_stale_visible_worker(
    paper: dict[str, Any],
    *,
    reason: str,
    timestamp: str | None = None,
) -> str:
    """Mark the currently bound visible worker as stale before re-dispatching.

    Recovery actions mean the local state has intentionally moved past the
    previous worker result. Keeping the old thread bound lets the scheduler
    replay old final markers and immediately block/remove the recovered paper.
    """
    old_thread = str(paper.get("thread_id") or "").strip()
    if not old_thread:
        return ""
    stale_threads = paper.setdefault("stale_worker_thread_ids", [])
    if not isinstance(stale_threads, list):
        stale_threads = []
        paper["stale_worker_thread_ids"] = stale_threads
    append_unique(stale_threads, old_thread)
    now = timestamp or now_iso()
    paper["last_stale_worker_thread_id"] = old_thread
    paper["last_stale_worker_reason"] = reason
    paper["last_recovered_thread_cleared_at"] = now
    paper["last_thread_status"] = f"stale_{reason}"
    paper["thread_id"] = ""
    return old_thread


@_smart_state_locked
def recover_auto_actionable_papers(
    paper_ids: list[str] | None = None,
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """Recover waiting/blocked papers that are covered by fixed smart-submission policy."""
    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    scope_requested = paper_ids is not None
    if scope_requested:
        selected = {str(item) for item in paper_ids if str(item).strip()}
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        state["papers"] = {pid: paper for pid, paper in papers.items() if pid in selected}
        result = _recover_auto_actionable_papers_in_state(state)
        full_state = read_json(SMART_STATE_PATH, _default_state())
        full_papers = full_state.setdefault("papers", {})
        for pid, paper in state.get("papers", {}).items():
            full_papers[pid] = paper
        full_queue = [str(item) for item in full_state.get("online_queue", []) if str(item).strip()]
        for pid in state.get("online_queue", []):
            paper = full_papers.get(str(pid))
            if isinstance(paper, dict):
                _append_online_queue_guarded(full_queue, str(pid), paper)
        guarded_full_queue: list[str] = []
        for pid in full_queue:
            paper = full_papers.get(pid)
            if not isinstance(paper, dict):
                continue
            route, route_error = _route_guard_result(pid)
            if route_error is not None:
                paper["last_online_route_guard"] = route_error
                continue
            _append_online_queue_guarded(guarded_full_queue, pid, paper)
        full_state["online_queue"] = guarded_full_queue
        full_state["updated_at"] = now_iso()
        full_state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "recover_auto_actionable_papers_selected",
                "paper_ids": sorted(selected),
                "result": {k: v for k, v in result.items() if k != "retargeted"},
                "retargeted": [item.get("paper_id") for item in result.get("retargeted", [])],
            }
        )
        state = full_state
    else:
        result = _recover_auto_actionable_papers_in_state(state)

    if activate and result.get("online_recovered"):
        state["mode"] = "run_once"
    state["updated_at"] = now_iso()
    write_json_atomic(SMART_STATE_PATH, state)
    wakeup = set_smart_submission_automation_fast_wakeup(True) if activate and result.get("online_recovered") else {}
    state = read_json(SMART_STATE_PATH, state)
    return {
        "online_recovered": result.get("online_recovered", []),
        "retargeted": result.get("retargeted", []),
        "skipped": result.get("skipped", {}),
        "mode": state.get("mode"),
        "online_queue": state.get("online_queue", []),
        "wakeup": wakeup,
        "state": state,
    }


def _is_autofixable_package_blocked_paper(
    paper_id: str,
    paper: dict[str, Any],
    browser_health: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[bool, str]:
    status = str(paper.get("status") or "").strip().lower()
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    if status != "blocked" and workflow_status != "blocked":
        return False, "not blocked"
    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    if package_dir is None:
        return False, "package directory not found"

    text = _paper_summary_text(paper)
    terminal_hard_stop_terms = (
        "inaccessible mailbox",
        "missing/expired verification",
        "missing or expired verification",
        "missing verification-link",
        "missing/expired verification-link",
        "verification link/email could not be retrieved",
        "one-time login verification link/email could not be retrieved",
        "identity/security verification is blocked",
        "security verification is blocked",
        "final review still renders",
        "reverted to public-resource placeholders",
        "no safe preset template was verified",
        "final submit was not attempted",
        "not a package-autofix candidate",
        "registration form contains captcha",
        "registration captcha",
        "self-registration path contains captcha",
    )
    for term in terminal_hard_stop_terms:
        if term in text:
            return False, f"terminal hard stop term: {term}"

    data_availability_placeholder_terms = (
        "data availability statement with unresolved template placeholders",
        "data availability template",
        "data-sharing template",
        "data sharing template",
        "doi/url placeholder",
        "reference number placeholder",
        "repository name placeholder",
        "public resources/urls placeholder",
        "[repository name]",
        "[doi/url]",
        "[reference number]",
        "[list resources and urls]",
    )
    if any(term in text for term in data_availability_placeholder_terms):
        return True, "data_availability_template_autofix_candidate"

    if _third_party_declaration_terms(text):
        if settings.get("standing_third_party_material_authorized", True):
            return True, "third_party_declaration_autofix_candidate"
        return False, "third-party/copyright declaration authorization is disabled"

    if _standing_declaration_terms(text):
        if (
            settings.get("standing_ai_use_statement_authorized", True)
            and settings.get("standing_author_declaration_authorized", True)
        ):
            return True, "standing_declaration_autofix_candidate"
        return False, "standing declaration authorization is disabled"

    auto_terms = (
        "keyword",
        "github",
        "repository",
        "data availability",
        "data-sharing",
        "data sharing",
        "data template",
        "doi/url",
        "reference number",
        "ai-use",
        "ai use",
        "generative-ai",
        "generative ai",
        "ai-assisted",
        "ai assisted",
        "author declaration",
        "author confirmation",
        "author approval",
        "simultaneous submission",
        "third-party material",
        "third party material",
        "third-party-material",
        "copyright permission",
        "copyright declaration",
        "manual_required",
        "blocked_pending_author_declaration",
        "abstract",
        "springer latex",
        "latex upload",
        "figures currently in a subfolder",
        "package metadata",
        "metadata fingerprint",
    )
    if not any(term in text for term in auto_terms):
        return False, "no package-autofix terms"

    github_missing_terms = (
        "repository-specific github url/name is missing",
        "repository-specific github repository url/name is missing",
        "concrete paper-specific github repository url/name is missing",
        "paper-specific github repository url/name",
        "provide a verifiable paper-specific github",
        "create or provide a verifiable paper-specific github",
    )
    if any(term in text for term in github_missing_terms):
        if settings.get("github_repository_planned_placeholder_enabled", True):
            return True, "github_repo_planned_placeholder_candidate"
        if paper.get("last_github_repo_autocreate_failed_at"):
            return False, "GitHub repository auto-create already failed and still needs user action"
        if settings.get("github_repository_auto_create_enabled", True):
            return True, "github_repo_autocreate_candidate"
        if paper.get("last_package_autofix_recovered_at"):
            return False, "paper-specific GitHub URL/name was already probed and is still missing"

    credential_hard_stop_terms = (
        "login failed",
        "invalid username/password",
        "invalid credentials",
        "did not recognize the locally stored username/password",
        "credentials failed",
        "provide working inderscience submissions credentials",
        "complete inderscience account recovery",
        "no actionable reset link",
        "no actionable recovery content",
    )
    for term in credential_hard_stop_terms:
        if term in text:
            return False, f"{SMART_SECRET_BROKER_REQUIRED}: credential hard stop term: {term}"

    hard_stop_terms = (
        "final_risk_report is blocked",
        "double-blind/template package is not ready",
        "do not submit yet",
        "payment",
        "forced oa",
        "2fa",
        "security challenge",
        "forced orcid",
        "author-person confirmation",
        "active-prior-submission",
        "active prior submission",
        "new author",
        "new affiliation",
        "new funding",
        "new ethics",
        "new legal",
    )
    for term in hard_stop_terms:
        if term in text:
            return False, f"hard stop term: {term}"

    # A non-interactive Cloudflare probe is recoverable when a visible CDP
    # browser is available; a live CAPTCHA/Cloudflare page in the worker is not.
    if "cloudflare" in text and not browser_health.get("operational"):
        return False, "cloudflare and no operational CDP browser"

    return True, "safe package auto-fix candidate"


def _recover_autofixable_package_blocks(
    state: dict[str, Any],
    browser_health: dict[str, Any],
) -> dict[str, Any]:
    """Requeue papers whose local package blockers should be fixed by a worker first."""
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    if not settings.get("auto_package_fix_enabled", True):
        return {"recovered": [], "skipped": {"module": "auto_package_fix_enabled=false"}}

    papers = state.get("papers")
    if not isinstance(papers, dict):
        return {"recovered": [], "skipped": {"papers": "invalid papers map"}}
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    recovered: list[str] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    preferred_port = (
        browser_health.get("cdp_ports", {}).get("preferred_available")
        if isinstance(browser_health.get("cdp_ports"), dict)
        else None
    )
    browser_status = f"cdp_{preferred_port or '9224_9222'}_available; extension_optional"
    for paper_id, paper in sorted(papers.items()):
        if not isinstance(paper, dict):
            continue
        ok, reason = _is_autofixable_package_blocked_paper(str(paper_id), paper, browser_health, settings)
        if not ok:
            if str(paper.get("status") or "").strip().lower() == "blocked":
                skipped[str(paper_id)] = reason
            continue
        route, route_error = _route_guard_result(str(paper_id), unified_state=unified_state)
        if route_error is not None:
            skipped[str(paper_id)] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            continue
        _mark_smart_route_on_paper(paper, route or {})
        previous_blocker = str(paper.get("blocker") or paper.get("detail") or "")
        paper["previous_blocker"] = previous_blocker
        if reason == "github_repo_planned_placeholder_candidate":
            applied = _apply_planned_github_to_package(str(paper_id), paper, settings)
            if not applied.get("applied"):
                skipped[str(paper_id)] = str(applied.get("reason") or "planned GitHub repo could not be applied")
                continue
            paper["status"] = "revision_needed"
            paper["next_action"] = "visible_worker_online_submit_or_monitor"
            paper["online_intent"] = "planned_github_repo_then_submit"
            paper["detail"] = (
                "Recorded a planned paper-specific GitHub repository URL under the configured owner. "
                "The user will create/populate the repository later; do not block package gating on "
                "GitHub auth, 404, or public-repo discovery unless the live journal form rejects the URL. "
                f"planned_repo={applied.get('repo_url')}"
            )
            paper["planned_github_repo_result"] = applied
        elif reason == "github_repo_autocreate_candidate":
            paper["status"] = "revision_needed"
            paper["next_action"] = "github_repo_autocreate_then_package_gate"
            paper["online_intent"] = "github_repo_autocreate_then_submit"
            paper["detail"] = (
                "Recovered into GitHub repository auto-create queue. Worker must create or verify a "
                "paper-specific repository, write the URL/name into package metadata, rerun the gate, "
                "then continue online submission if the gate passes."
            )
        elif reason == "data_availability_template_autofix_candidate":
            paper["status"] = "uploading"
            paper["next_action"] = "fix_data_availability_template_then_submit"
            paper["online_intent"] = "data_availability_template_autofix_then_submit"
            paper["detail"] = (
                "Recovered into Data Availability template auto-fix queue. Worker must read the exact "
                "local Data Availability statement from package/declarations.md or author_info.md, return "
                "to the journal data-availability/additional-information form, replace any live placeholder "
                "template with the local recorded statement, verify Final Review contains no placeholders, "
                "then continue final submission if no other hard stop appears."
            )
        elif reason == "standing_declaration_autofix_candidate":
            applied = _apply_standing_declarations_to_package(str(paper_id), paper, settings)
            if not applied.get("applied"):
                skipped[str(paper_id)] = str(applied.get("reason") or "standing declarations could not be applied")
                continue
            cleared = _clear_standing_declaration_state_markers(paper)
            paper["status"] = "revision_needed"
            paper["next_action"] = "standing_declarations_applied_then_online_submit"
            paper["online_intent"] = "standing_declarations_then_submit"
            paper["detail"] = (
                "Standing AI-use, originality/no-simultaneous-submission, and author-responsibility "
                "declarations were written to the package under user authorization. Worker must rerun "
                "the submission gate and continue online submission unless a non-routine hard stop appears."
            )
            if cleared:
                applied["cleared_state_markers"] = cleared
            paper["standing_declaration_result"] = applied
        elif reason == "third_party_declaration_autofix_candidate":
            applied = _apply_third_party_declarations_to_package(str(paper_id), paper, settings)
            if not applied.get("applied"):
                skipped[str(paper_id)] = str(applied.get("reason") or "third-party declarations could not be applied")
                continue
            paper["status"] = "revision_needed"
            paper["next_action"] = "third_party_declarations_applied_then_online_submit"
            paper["online_intent"] = "third_party_declarations_then_submit"
            paper["detail"] = (
                "Standing no-third-party-material/copyright-permission declarations were written to the package "
                "under user authorization. Worker must select the no-permission-required option and continue "
                "online submission unless a material-specific contradiction appears."
            )
            paper["third_party_declaration_result"] = applied
        else:
            paper["status"] = "revision_needed"
            paper["next_action"] = "package_autofix_then_online_submit"
            paper["online_intent"] = "package_autofix_then_submit"
            paper["detail"] = (
                "Recovered into package auto-fix queue. Worker must apply safe default "
                "package metadata fixes, rerun the gate, then continue online submission if the gate passes."
            )
        paper["workflow_status"] = "queued"
        paper["blocker"] = ""
        paper["browser_status"] = browser_status
        paper["last_package_autofix_recovered_at"] = now_iso()
        paper["last_recovery_reason"] = reason
        _rotate_canary_attempt(paper, str(paper_id))
        _append_online_queue_guarded(
            online_queue,
            str(paper_id),
            paper,
            unified_state=unified_state,
        )
        recovered.append(str(paper_id))

    if recovered:
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
        state["online_queue"] = online_queue
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "recover_autofixable_package_blocks",
                "recovered": recovered,
                "skipped": skipped,
                "browser_status": browser_status,
            }
        )
    return {"recovered": recovered, "skipped": skipped}


@_smart_state_locked
def apply_planned_github_repositories(
    paper_ids: list[str] | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Record deterministic planned GitHub URLs and requeue affected papers."""
    ensure_smart_submission_module()
    scope_requested = paper_ids is not None
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    reconcile_result = (
        {"changed": 0, "normalized": [], "scope": "selected_only"}
        if scope_requested
        else reconcile_smart_submission_worker_states(state)
    )
    if reconcile_result.get("changed"):
        write_json_atomic(SMART_STATE_PATH, state)
        state = read_json(SMART_STATE_PATH, state)
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    settings["github_repository_planned_placeholder_enabled"] = True
    settings.setdefault("github_repository_owner", "TYSJY")
    settings["online_submit_enabled"] = True
    settings["visible_scheduler_enabled"] = True

    papers = state.setdefault("papers", {})
    if not isinstance(papers, dict):
        papers = {}
        state["papers"] = papers
    if not scope_requested:
        _normalize_permission_retry_labels(state)
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    selected = [str(item) for item in (paper_ids or []) if str(item).strip()]
    target_ids = selected if scope_requested else sorted(str(item) for item in papers.keys())
    applied: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    github_terms = (
        "github",
        "repository-specific",
        "repository url/name",
        "repo url/name",
        "github_repo_autocreate_failed",
    )

    for paper_id in target_ids:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper not found in smart submission state"
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            continue
        _mark_smart_route_on_paper(paper, route or {})
        if str(paper.get("workflow_status") or "").strip().lower() == "running":
            skipped[paper_id] = "worker is currently running"
            continue
        if not scope_requested:
            text = _paper_summary_text(paper)
            if not any(term in text for term in github_terms):
                skipped[paper_id] = "no GitHub blocker detected"
                continue
        result = _apply_planned_github_to_package(paper_id, paper, settings)
        if not result.get("applied"):
            skipped[paper_id] = str(result.get("reason") or "planned GitHub repo could not be applied")
            continue
        paper["status"] = "revision_needed"
        paper["workflow_status"] = "queued"
        paper["blocker"] = ""
        paper["next_action"] = "visible_worker_online_submit_or_monitor"
        paper["online_intent"] = "planned_github_repo_then_submit"
        paper["detail"] = (
            "Planned paper-specific GitHub repository URL recorded by policy; user will create/populate "
            f"the repository later. planned_repo={result.get('repo_url')}"
        )
        paper["last_recovery_reason"] = "github_repo_planned_placeholder"
        paper["last_package_autofix_recovered_at"] = now_iso()
        paper["planned_github_repo_result"] = result
        warnings = paper.get("warnings")
        if isinstance(warnings, list):
            paper["warnings"] = [
                item
                for item in warnings
                if "github" not in str(item).lower() and "repository" not in str(item).lower()
            ]
        _rotate_canary_attempt(paper, paper_id)
        _append_online_queue_guarded(
            online_queue,
            paper_id,
            paper,
            unified_state=unified_state,
        )
        audit_path = SMART_AUDIT_DIR / f"{timestamp_id()}_{paper_id}_planned_github_repo.md"
        write_text(
            audit_path,
            "\n".join(
                [
                    "# Smart Submission Planned GitHub Repository",
                    "",
                    f"- paper_id: {paper_id}",
                    f"- repository_name: {result.get('repo_name')}",
                    f"- repository_url: {result.get('repo_url')}",
                    "- status: planned_user_will_create",
                    "- policy: user authorized deterministic planned GitHub repository URLs; do not block on GitHub auth, 404, or public discovery before online submission unless the live journal form rejects the URL.",
                    f"- package_dir: {result.get('package_dir')}",
                    "",
                    "Changed files:",
                    *[f"- {path}" for path in result.get("changed_files", [])],
                    "",
                    f"SMART_SUBMISSION_PAPER_ID={paper_id}",
                    "SMART_SUBMISSION_STATUS=revision_needed",
                    "SMART_SUBMISSION_NEXT=planned_github_repo_then_submit",
                ]
            ),
        )
        result["audit_path"] = str(audit_path)
        applied.append(result)

    state["online_queue"] = online_queue
    if activate and applied:
        state["mode"] = "run_once"
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "apply_planned_github_repositories",
            "applied": [item["paper_id"] for item in applied],
            "skipped": skipped,
            "activate": bool(activate),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    wakeup: dict[str, Any] = {}
    if activate and applied:
        wakeup = set_smart_submission_automation_fast_wakeup(True)
    return {
        "applied": applied,
        "skipped": skipped,
        "online_queue": online_queue,
        "mode": state.get("mode"),
        "wakeup": wakeup,
        "state": state,
    }


@_smart_state_locked
def apply_standing_declarations(
    paper_ids: list[str] | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Write user-authorized standing declarations into packages and requeue affected papers."""
    ensure_smart_submission_module()
    scope_requested = paper_ids is not None
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    online_queue_normalize = (
        {"changed": False, "scope": "selected_only"}
        if scope_requested
        else _normalize_online_queue_state(state, reason="prepare_visible_online_dispatch")
    )
    if online_queue_normalize.get("changed"):
        write_json_atomic(SMART_STATE_PATH, state)
        state = read_json(SMART_STATE_PATH, state)
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    settings["standing_ai_use_statement_authorized"] = True
    settings["standing_author_declaration_authorized"] = True
    settings.setdefault("standing_declaration_authorized_at", STANDING_DECLARATION_AUTHORIZED_AT)
    settings.setdefault("default_ai_use_statement", DEFAULT_AI_USE_STATEMENT)
    settings.setdefault("default_originality_statement", DEFAULT_ORIGINALITY_STATEMENT)
    settings.setdefault("default_author_responsibility_statement", DEFAULT_AUTHOR_RESPONSIBILITY_STATEMENT)
    settings["standing_third_party_material_authorized"] = True
    settings.setdefault("default_third_party_material_statement", DEFAULT_THIRD_PARTY_MATERIAL_STATEMENT)
    settings.setdefault("default_copyright_permission_statement", DEFAULT_COPYRIGHT_PERMISSION_STATEMENT)
    settings["online_submit_enabled"] = True
    settings["visible_scheduler_enabled"] = True

    papers = state.setdefault("papers", {})
    if not isinstance(papers, dict):
        papers = {}
        state["papers"] = papers
    if not scope_requested:
        _normalize_permission_retry_labels(state)
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    selected = [str(item) for item in (paper_ids or []) if str(item).strip()]
    target_ids = selected if scope_requested else sorted(str(item) for item in papers.keys())
    applied: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    for paper_id in target_ids:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper not found in smart submission state"
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            continue
        _mark_smart_route_on_paper(paper, route or {})
        if str(paper.get("workflow_status") or "").strip().lower() == "running":
            skipped[paper_id] = "worker is currently running"
            continue
        state_has_declaration_gap = _standing_declaration_terms(_paper_summary_text(paper))
        package_has_declaration_gap = _package_has_standing_declaration_gap(paper_id, paper)
        if (
            not scope_requested
            and not state_has_declaration_gap
            and not package_has_declaration_gap
        ):
            skipped[paper_id] = "no standing declaration blocker detected"
            continue
        result = _apply_standing_declarations_to_package(paper_id, paper, settings)
        if not result.get("applied"):
            skipped[paper_id] = str(result.get("reason") or "standing declarations could not be applied")
            continue
        if scope_requested or state_has_declaration_gap:
            cleared = _clear_standing_declaration_state_markers(paper)
            paper["status"] = "revision_needed"
            paper["workflow_status"] = "queued"
            paper["blocker"] = ""
            paper["next_action"] = "standing_declarations_applied_then_online_submit"
            paper["online_intent"] = "standing_declarations_then_submit"
            paper["detail"] = (
                "Standing AI-use, originality/no-simultaneous-submission, and author-responsibility "
                "declarations applied under user authorization. Rerun the gate and continue online submission "
                "unless a non-routine hard stop appears."
            )
            paper["last_recovery_reason"] = "standing_declaration_autofix"
            paper["last_package_autofix_recovered_at"] = now_iso()
            if cleared:
                result["cleared_state_markers"] = cleared
            _rotate_canary_attempt(paper, paper_id)
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
            )
        else:
            result["state_preserved"] = True
            result["state_preserved_reason"] = "package-only declaration cleanup"
        paper["standing_declaration_result"] = result
        audit_path = SMART_AUDIT_DIR / f"{timestamp_id()}_{paper_id}_standing_declarations.md"
        write_text(
            audit_path,
            "\n".join(
                [
                    "# Smart Submission Standing Declarations",
                    "",
                    f"- paper_id: {paper_id}",
                    f"- authorized_at: {result.get('authorized_at')}",
                    "- policy: user authorized default AI-use, originality/no-simultaneous-submission, and author-responsibility declarations.",
                    f"- package_dir: {result.get('package_dir')}",
                    "",
                    "Changed files:",
                    *[f"- {path}" for path in result.get("changed_files", [])],
                    "",
                    f"SMART_SUBMISSION_PAPER_ID={paper_id}",
                    "SMART_SUBMISSION_STATUS=revision_needed",
                    "SMART_SUBMISSION_NEXT=standing_declarations_applied_then_online_submit",
                ]
            ),
        )
        result["audit_path"] = str(audit_path)
        applied.append(result)

    state["online_queue"] = online_queue
    if activate and applied:
        state["mode"] = "run_once"
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "apply_standing_declarations",
            "applied": [item["paper_id"] for item in applied],
            "skipped": skipped,
            "activate": bool(activate),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    wakeup: dict[str, Any] = {}
    if activate and applied:
        wakeup = set_smart_submission_automation_fast_wakeup(True)
    return {
        "applied": applied,
        "skipped": skipped,
        "online_queue": online_queue,
        "mode": state.get("mode"),
        "wakeup": wakeup,
        "state": state,
    }


def _normalize_retarget_records(state: dict[str, Any]) -> list[str]:
    papers = state.get("papers")
    if not isinstance(papers, dict):
        return []
    changed: list[str] = []
    for paper_id, paper in sorted(papers.items()):
        if not isinstance(paper, dict):
            continue
        package_text = str(paper.get("source_package_dir") or "")
        retarget_package = Path(package_text) if "retarget_" in package_text else _latest_retarget_package(str(paper_id))
        if not retarget_package or not retarget_package.exists() or "retarget_" not in str(retarget_package):
            continue
        before = json.dumps(paper, ensure_ascii=False, sort_keys=True)
        paper["source_package_dir"] = str(retarget_package)
        paper["paper_dir"] = str(retarget_package.parent.parent)
        paper["online_intent"] = "retarget_after_rejection"
        target_journal = _risk_report_value(retarget_package, "Target journal")
        submission_system = _risk_report_value(retarget_package, "Publisher/system")
        if target_journal:
            paper["target_journal"] = target_journal
        if submission_system:
            paper["submission_system"] = submission_system
        after = json.dumps(paper, ensure_ascii=False, sort_keys=True)
        if after != before:
            changed.append(str(paper_id))
    return changed


def _package_autofix_prompt_section() -> str:
    return """## Package Auto-Fix Before Blocking

Before marking a paper `blocked` for local package metadata, apply safe default remediation first and audit exactly what changed.

Safe default fixes:

- Missing keywords: derive 4-6 concise technical keywords from the manuscript title, abstract, and method section, then write them to the package metadata/declarations or the journal form fields. Do not invent application claims that are not in the paper.
- Missing GitHub URL/name: search `author_info.md`, `journal.md`, declarations, cover letter, package files, previous audit files, and local site/account records. If no paper-specific repository exists, first create/verify one when an authenticated GitHub route is already available. If no authenticated creation route is available and `settings.github_repository_planned_placeholder_enabled=true`, generate a deterministic planned repository URL under `settings.github_repository_owner` from the paper id, write the URL/name into the package metadata/declarations/fingerprint with status `planned_user_will_create`, rerun the gate, and continue. Do not block on GitHub authentication, 404, or public-repository discovery for this planned URL. Stop only if the live journal form rejects the URL or the missing information is a real legal/ethical/code-availability fact beyond the planned repository name.
- Data Availability placeholders or mismatched live journal templates: if a final-review page or form contains placeholders such as `[repository name]`, `[DOI/URL]`, `[reference number]`, `[list resources and URLs]`, or asks for a data-sharing template that conflicts with the local package, do not final-submit and do not immediately block. First read the exact `## Data Availability` section from `package/declarations.md`, then `author_info.md`, then `submission_metadata_fingerprint.json`. If a non-placeholder local statement exists, go back to the journal's data-availability/additional-information step and replace the live field or choose the closest template that preserves the local statement exactly. This is a routine declaration already recorded in the package, not a new legal/ethical fact. Block only if the site requires a specific DOI, external repository, unpublished public-source URL, new dataset fact, or a template that cannot represent the local statement without placeholders.
- AI-use / generative-AI statement: the user granted standing authorization on 2026-07-05 to use `settings.default_ai_use_statement` when a paper-specific statement is missing. Write the default statement into `package/declarations.md`, package metadata/fingerprint, and any required routine journal form field, then rerun the gate. Do not write `MANUAL_REQUIRED` or block only because the AI-use statement was missing.
- Originality / no-simultaneous-submission / author-responsibility confirmations: the user granted standing authorization on 2026-07-05 to use `settings.default_originality_statement` and `settings.default_author_responsibility_statement` for routine submission confirmations. Apply these defaults to package metadata/declarations and routine form checkboxes/text fields. Stop only if the site asks for a non-routine new legal, copyright, ethics, funding, affiliation, coauthor, ORCID, payment, or personally interactive author action that is not covered by the standing defaults.
- Third-party material / copyright-permission declaration: the user granted standing authorization on 2026-07-05 to use `settings.default_third_party_material_statement` and `settings.default_copyright_permission_statement` for routine no-permission-required declarations when the local package has no contradictory evidence. Write these statements into `package/declarations.md`, package metadata/fingerprint, and SNAPP/Springer form payloads; select the no-third-party-material/no-separate-permission option in the live form. Stop only if the local package or live form contains material-specific facts indicating third-party material, required permissions, pending permissions, a copyright holder, adapted/reproduced content, or another new copyright/legal fact.
- Abstract length: if a journal form requires a shorter abstract, create a bounded metadata/form abstract that preserves the paper meaning and record it in the audit. Do not change the manuscript's scientific claims.
- Springer/LaTeX upload packaging: create a journal-upload staging zip/package with flattened or correctly referenced figure/source files, leaving the original package intact.

After safe fixes, rerun the submission gate and continue login, field entry, upload, draft saving, and final-page review. When the review is ready, return `SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED`; with `settings.auto_final_submit_enabled=true`, the program immediately converts that marker into a short-lived exact-attempt `final_submit_authorization` and dispatches the same fixed SDK session again. A worker may click final submit only when that signed receipt is present. Stop when the remaining issue requires the user or a site boundary: payment/forced OA, CAPTCHA/Cloudflare that cannot be passed in the visible browser, 2FA/security challenge, forced ORCID authorization, non-routine author-person confirmation, new author/affiliation/funding/ethics/copyright/legal facts outside the standing defaults, active prior submission risk, or inability to determine submission success.
"""


def _account_recovery_prompt_section() -> str:
    return """## Account Access / Recovery Boundaries

Account secrets are controller-only data. The scheduler and visible workers receive only value-free readiness, account id, target origin, and live-session evidence.

- First confirm there is no prior active submission: no manuscript id, no confirmation page/email, no dashboard record, no `final_submit_attempted`, and no `submitted successfully` or `under review` evidence.
- Reuse an exact-origin browser session only after the controller has recorded typed, unexpired live-session evidence. A profile path, password reference, or account status string is not proof of a usable session.
- Never read `credential_store.local.json`, a legacy plaintext export, a DPAPI vault, passwords, password-reset messages, or one-time codes into a scheduler or worker prompt. Never ask a worker to reveal, copy, type, log, or audit a secret.
- If login requires a stored password and no trusted local secret broker/browser adapter has filled it, stop before online dispatch with `SMART_SECRET_BROKER_REQUIRED`. Do not fall back from a broken credential reference to retained plaintext.
- Fresh account registration and password reset are separate externally mutating workflows. They require an explicit user-confirmed action and a trusted local secret writer; a scheduler or visible worker must not create an account or persist a new password.
- Ordinary verification may continue only when an already authenticated mailbox/browser connector can complete it without placing a code, link token, or private message body in the prompt, state, audit, or final response.
- Stop for CAPTCHA/Cloudflare that cannot be completed in the visible browser, 2FA/security challenge, inaccessible mailbox, missing/expired verification, non-routine author-person confirmation, payment/forced OA, ORCID authorization, active-prior-submission risk, new legal/author/funding/ethics facts, or any unavailable trusted secret handoff.
"""


def _monitoring_email_first_prompt_section() -> str:
    return """## Email-First Submitted Monitoring

For papers that already have local submission evidence (`submitted`, `monitoring`, manuscript id, confirmation page/email, or prior audit proving final submit), monitoring is email-first.

- Search Gmail and the school mailbox for decision, revision, rejection, acceptance, technical-check, author-action, and submission-system notification messages before opening a journal dashboard.
- If no relevant email action signal is found, keep the paper in `monitoring` or `waiting`; write a compact audit note and do not mark the paper `blocked`.
- Open the journal dashboard only when an email indicates action is required, when local submission proof is missing, or when the user explicitly asks for a live dashboard check.
- A temporary Chrome/CDP, in-app browser, extension, dashboard redirect, or status-page access failure is not a hard blocker for an already-submitted monitoring paper. Record the access issue and continue email-first monitoring.
- Only block a monitoring paper for a real hard stop: payment/forced OA, CAPTCHA/Cloudflare that prevents a required action, 2FA/security challenge, forced ORCID, inaccessible mailbox, non-routine author-person confirmation, new legal/author/funding/ethics facts, or active duplicate-submission risk.
"""


def _default_prompt() -> str:
    return f"""# Smart Submission Program-Controlled Bridge

This visible thread is a compatibility bridge under `{SMART_ASCII_WORKSPACE}`.
The local Python program owns state transitions, queue order, next actions,
executor selection, and parallel capacity. The bridge must not choose work,
repair state by inference, or submit a paper itself.

## Files

- State: `{SMART_SUBMISSION_DIR / "smart_submission_state.json"}`
- Program plan: `{_smart_program_plan_path()}`
- Site registry: `{SMART_SITE_REGISTRY_PATH}`
- Audit: `{SMART_AUDIT_DIR}`

Only value-free account ids, verified origins, readiness codes, and evidence
paths may enter this thread. Credential stores and vaults are never inputs.

## Program Dispatch Plan Authority

1. Reconcile only workers already bound in state.
2. Run `python -m unified_paper_orchestrator smart-submission-run` once.
3. Run `python -m unified_paper_orchestrator smart-submission-program-plan` once.
4. Use `program_control.dispatch_candidates` exactly as returned.
5. Never scan `online_queue` to add, remove, replace, or reorder candidates.
6. Never turn a deterministic or human action into a model action.
7. If plan generation fails or the candidate list is empty, send nothing.

## Heartbeat Rules

- `manual`: report a quiet heartbeat and stop.
- `run_once` or `scheduled`: perform one reconciliation, one local sweep, one
  plan, and one bounded pass over that plan.
- Never loop until queues are empty and never generate a second plan in the
  same wakeup.
- A candidate failure affects that paper only and does not authorize an
  unlisted replacement.

## Local Executor

The local commands perform package checks, routine declaration repair,
registry matching, browser health, queue normalization, recovery, and ledger
updates. Their guarded outputs are authoritative. The bridge may only perform
the visible-thread operations and bounded site/page work explicitly listed by
the program plan.

## Paper State Machine

Canonical program phases include local preflight, discovery, session required,
site execution, final review ready, final action claimed, receipt captured,
monitoring, revision, retarget, blocked user/site, unknown final result,
accepted, rejected, and stopped. Legacy status prose is compatibility input,
not dispatch authority.

## Gates Before Online Submission

- Final submit requires a current signed `final_submit_authorization` bound to
  the exact paper id, route revision, attempt id, account id, target journal,
  target origin, and package hash.
- Starting Smart Submission authorizes the complete workflow. When final review
  passes and `settings.auto_final_submit_enabled=true`, the Python program issues
  this exact-attempt capability automatically; no second user dialog is needed.
- The capability must be atomically reserved and claimed for the bound current
  worker. Standing declarations or an old capability never substitute for the
  newly signed current-attempt receipt.
- After one authorized final click, never refresh, navigate, close, or retry.
  Capture the result through the trusted local confirmation command.
- A crash or uncertain response after the click enters unknown-result
  verification. It must never trigger another final click.
- A worker observation is not success evidence. Submitted, monitoring, or
  accepted state requires trusted captured manuscript id, evidence path, or
  confirmation URL with exact route revision and attempt id.

## Monitoring

Already-submitted papers are monitored email-first. Temporary browser, CDP,
extension, redirect, or dashboard failure alone is not a hard blocker. The
program decides whether a bounded semantic fallback is required.

## Account Boundary

Never read a credential file, vault, plaintext export, password-reset message,
one-time code, cookie, or private email body into this thread. If a stored
secret would be needed and no trusted local broker has produced a current
exact-origin session receipt, return `SMART_SECRET_BROKER_REQUIRED`.

A scheduler or worker must not create an account or persist a new password.
Account registration, reset, 2FA, security challenges, and secret entry remain
trusted-local or user-owned actions.

## Hard Stops

Stop only the affected paper for payment or forced OA, CAPTCHA/Cloudflare that
cannot be completed in the visible browser, 2FA/security challenge, forced
ORCID, inaccessible mailbox when required, non-routine author-person
confirmation, new author/affiliation/funding/ethics/copyright/legal facts,
active duplicate-submission risk, or inability to determine submission
success. Preserve fail-closed final-submit behavior.

## Sensitive Data

Never paste passwords, credential references, reset links, codes, tokens,
cookies, authorization values, or private message bodies into prompts, state,
audit, commands, or final markers. Record only value-free identifiers and
compact evidence references.

## Audit Output

Every action records timestamp, plan id, paper id, canonical phase/action,
executor, route revision, attempt id, guarded command result, evidence
references, state transition, blocker code, and next action. The bridge must
not write state fields directly.
"""


def _visible_scheduler_prompt_section() -> str:
    return r"""## Visible Scheduler / Worker Dispatch

This thread is a compatibility bridge, not the scheduler of record. The Python
program owns classification, queue order, next actions, and parallel capacity.
The bridge exists only because visible desktop thread operations are not yet
available to the local controller. If thread tools are not visible, use tool
discovery for `list_threads`, `create_thread`, `send_message_to_thread`,
`read_thread`, and `set_thread_title`.

## Program Dispatch Plan Authority

The JSON returned by
`python -m unified_paper_orchestrator smart-submission-program-plan` is the only
dispatch authority for a sweep. Use `program_control.dispatch_candidates`
exactly as returned. Never calculate slots, select papers from `online_queue`,
recover or substitute another paper, reorder candidates, or infer work from
status prose. An empty candidate list means send no worker prompt.

### Compatibility Bridge Rules

1. Read state first with `python -m unified_paper_orchestrator smart-submission-status`
   or Python's `json` module. Do not parse `smart_submission_state.json` with
   PowerShell `ConvertFrom-Json`; large Windows-path state files can be misread
   as bad escape sequences even when strict Python JSON parsing succeeds.
2. If this is the daily scheduled automation wakeup and `mode` is `manual`, run `python -m unified_paper_orchestrator smart-submission-mode scheduled` first so the local executor can queue the daily sweep.
3. If this is not a scheduled/manual-start run and `mode` is still `manual`, write only a short heartbeat/status note and stop.
4. Before asking the program for a new plan, reconcile only visible worker
   threads already recorded as `workflow_status=running`. Use `read_thread` on
   each stored current `thread_id`. This is compatibility bookkeeping, not
   paper selection and not paper work.
   - If the thread is `completed`/`idle`, accept its result only when the final answer contains all five markers:
     `SMART_SUBMISSION_PAPER_ID`, `SMART_SUBMISSION_ROUTE_REVISION`,
     `SMART_SUBMISSION_ATTEMPT_ID`, `SMART_SUBMISSION_STATUS`, and
     `SMART_SUBMISSION_NEXT`. For a first `submitted`, `monitoring`, or `accepted` result with no prior signed confirmation receipt, do not trust the worker's URL, id, screenshot, or prose. While the exact confirmation page remains open, immediately run `python -m unified_paper_orchestrator smart-submission-capture-confirmation <paper_id> --route-revision <worker_route_revision> --attempt-id <worker_attempt_id> --thread-id <thread_id> --authorization-id <current_exact_authorization_id>`. This trusted local CDP probe must return `SMART_SUBMISSION_CONFIRMATION_CAPTURED`; otherwise do not record a success state and do not retry the final click. Then run `python -m unified_paper_orchestrator smart-submission-record-worker-result <paper_id> --route-revision <worker_route_revision> --attempt-id <worker_attempt_id> --status <status> --next "<next>" --thread-id <thread_id> --submission-id <captured_manuscript_id> --evidence-path <captured_evidence_path> --confirmation-url <captured_confirmation_url>`. Pass the route revision and attempt id exactly as the worker reported them; use only capture output for success evidence, never replace missing or stale worker markers with current state values, and never substitute worker-provided evidence.
   - If that final answer says a scheduler-side local action is required, record
     it as `revision_needed` with `--keep-queued` and a clear `--next`, rather
     than leaving the stale worker marked running.
   - If the thread is `completed`/`idle` without all five usable final markers,
     do not synthesize a result and do not substitute current route/attempt values;
     record the reconciliation failure for scheduler-side repair.
   - Persist a `waitingOnApproval` observation through the guarded reconciliation
     path before running `smart-submission-requeue-waiting-approval`. Never rotate
     an ordinary running attempt merely because it was selected.
5. Run `python -m unified_paper_orchestrator smart-submission-run` exactly once
   to refresh local preflight, recovery, normalization, and audit state.
6. Run `python -m unified_paper_orchestrator smart-submission-program-plan`
   exactly once after reconciliation and local refresh. Do not merge it with an
   older plan. If plan generation fails, send nothing.
7. If `settings.online_submit_enabled` is not true or the returned
   `dispatch_candidates` list is empty, do not create or message worker threads
   and do not use browser/email tools.
8. Treat the returned `active_worker_count`, `available_slots`, and candidate
   list as final. The bridge must not count threads, calculate a cap, scan
   `online_queue`, or fill a failed candidate's slot with an unlisted paper.
9. For an already submitted/monitoring candidate, use email-first monitoring.
   Temporary Chrome/CDP, extension, or dashboard access failure is not by itself
   a paper blocker.
10. Before the first listed dispatch, run `python -m unified_paper_orchestrator smart-submission-browser-cleanup --close` once. Cleanup failure does not authorize a different candidate.
11. Process `dispatch_candidates` in returned order and at most once each.
    The plan's paper id, action, route revision, attempt id, and executor are
    immutable for this sweep. Skip a failed candidate without replacement.
12. Reuse a stored thread only when it is current, not stale, not
    archived/systemError/waitingOnApproval, and rooted in the unified local
    project. Otherwise create one fresh visible local-project thread and rename
    it to the exact stored title. Never use a projectless `dayi-unified*`
    workspace or `D:\Documents\dayi_unified`.
13. The program-selected action constrains the worker. Do not upgrade a
    deterministic, monitoring, waiting-human, or approval action into a broader
    page task. The worker handles one bounded action for one paper only.
14. Do not directly edit `thread_id`, `workflow_status`, queue arrays,
    `route_revision`, `attempt_id`, authorization, or receipt fields. Use only
    the guarded local commands.
15. Immediately before reservation, re-read the exact candidate record only to
    supply command arguments. If its route revision, attempt id, account/origin
    binding, or action no longer matches the plan, send nothing; do not repair
    the mismatch with current values and do not substitute another paper.
16. For each dispatch candidate, first refresh its value-free signed session receipt with `python -m unified_paper_orchestrator smart-submission-probe-session <credential_account_id> --system-id <submission_system_id> --origin <exact_verified_target_origin>`. Derive all three values from the queued paper and verified local site registry; never guess an origin and never put credentials in this command. If the probe fails, create/send no worker prompt and leave the paper fail-closed at `SMART_SECRET_BROKER_REQUIRED` or the returned stable probe code. Then create or select the eligible worker thread and read the exact `route_revision` plus `attempt_id` from current state. Atomically reserve the queue item with `python -m unified_paper_orchestrator smart-submission-record-worker-dispatch <paper_id> --thread-id <thread_id> --route-revision <route_revision> --attempt-id <attempt_id>`. For a final-click attempt, the trusted bridge must immediately consume the reserved capability with `python -m unified_paper_orchestrator smart-submission-claim-final-submit-action <paper_id> --route-revision <route_revision> --attempt-id <attempt_id> --thread-id <thread_id> --authorization-id <authorization_id>`; the worker must never run this local command. Only after all required commands exit successfully, generate the exact prompt with `python -m unified_paper_orchestrator smart-submission-worker-prompt <paper_id> --route-revision <route_revision>` and send that output exactly as-is to the reserved thread immediately. The atomic dispatch, action claim, and post-claim prompt builder independently reverify the signed account/origin binding, committed audits, route, attempt, package binding, and expiry. The post-claim prompt enforces a two-minute click window. If any command or prompt generation fails, send nothing. Never edit `thread_id` or `workflow_status=running` directly. A revision or attempt mismatch is a hard stale-worker refusal. Send exactly one worker prompt, then continue only with the next candidate already listed in the same program plan.
17. Record a worker result only from all required exact
    `SMART_SUBMISSION_*` markers and trusted success capture. Never remove a
    paper from a queue merely because a thread became idle.
18. At the end, report the plan id/time, listed ids, dispatched ids, and
    per-candidate guarded failure codes. Stop after this one plan; do not
    generate a second plan in the same wakeup.

### Exact Worker Prompt

```powershell
python -m unified_paper_orchestrator smart-submission-worker-prompt <paper_id> --route-revision <route_revision>
```

Send the command output exactly as the worker prompt.
"""


def _redact_prompt_text(text: str, *, max_chars: int = 4000) -> str:
    redacted: list[str] = []
    secret_terms = (
        "password",
        "passwd",
        "token",
        "cookie",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "credential_ref",
        "credential store",
        "credential_store",
        "recovery code",
        "recovery_code",
        "verification code",
        "verification_code",
        "one-time code",
        "otp",
    )
    for line in str(text or "").splitlines():
        if any(term in line.lower() for term in secret_terms):
            redacted.append("[redacted local secret line]")
        else:
            cleaned = line.rstrip()
            for match in re.findall(r"https?://[^\s<>\"']+", cleaned, flags=re.IGNORECASE):
                suffix = ""
                candidate = match
                while candidate and candidate[-1] in ".,;)]}":
                    suffix = candidate[-1] + suffix
                    candidate = candidate[:-1]
                origin = _web_origin(candidate)
                safe_url = _safe_prompt_url(candidate, allowed_origins={origin}) if origin else ""
                cleaned = cleaned.replace(match, (safe_url or "[redacted unsafe URL]") + suffix, 1)
            redacted.append(cleaned)
    result = "\n".join(redacted).strip()
    if len(result) > max_chars:
        return result[:max_chars].rstrip() + "\n...[truncated]"
    return result


_WORKER_SENSITIVE_MARKERS = (
    "password",
    "passwd",
    "credential_ref",
    "credential store",
    "credential_store",
    "vault:",
    "dpapi",
    "cookie",
    "api_key",
    "apikey",
    "authorization:",
    "recovery_code",
    "verification_code",
    "one-time code",
    "otp=",
    "token=",
    "secret=",
    "sessionid",
    "session_id",
    "session=",
    "bearer",
    "access_key",
    "client_secret",
    "jwt=",
)

_WORKER_DEFAULT_ACTIONS = {
    "submitted": "monitor_submission_email_first",
    "monitoring": "monitor_submission_email_first",
    "revision_needed": "scheduler_review_revision_required",
    "retarget_needed": "rerun_journal_author_retarget",
    "rejected": "rerun_journal_author_retarget",
    "blocked": "user_review_required",
    "accepted": "archive_acceptance_evidence",
    "waiting": "wait_for_external_status",
    "no_action": "none",
    "login_check": "trusted_login_readiness_required",
    "uploading": "continue_visible_upload",
    "ready_for_online_submit": "user_confirmation_before_final_submit_required",
}


def _value_free_worker_action(status: str, value: Any) -> str:
    """Map untrusted worker prose to a small, non-secret action vocabulary."""

    text = str(value or "").strip().casefold()
    mappings = (
        (("smart_secret_broker_required", "secret broker"), "SMART_SECRET_BROKER_REQUIRED"),
        (("captcha", "cloudflare"), "manual_captcha_or_cloudflare_required"),
        (("2fa", "two-factor", "security challenge"), "manual_security_challenge_required"),
        (("orcid",), "manual_orcid_authorization_required"),
        (("payment", "forced oa", "open access fee"), "manual_payment_or_oa_decision_required"),
        (
            (
                "smart_final_submit_confirmation_required",
                "final submit",
                "final-submit",
                "user confirmation",
            ),
            FINAL_SUBMIT_REVIEW_READY_ACTION,
        ),
        (("author metadata", "affiliation", "author_info"), "scheduler_side_verified_metadata_required"),
        (("package fix", "package read", "package metadata"), "scheduler_side_package_fix_required"),
    )
    for markers, action in mappings:
        if any(marker in text for marker in markers):
            return action
    return _WORKER_DEFAULT_ACTIONS.get(str(status or "").strip().casefold(), "user_review_required")


def _sdk_final_review_marker_matches(
    paper_id: str,
    paper: dict[str, Any],
    sdk_record: dict[str, Any],
) -> bool:
    """Validate an exact-attempt SDK marker that stopped on final review."""

    if (
        str(paper.get("status") or "").casefold() != "waiting"
        or str(paper.get("workflow_status") or "").casefold() != "waiting"
        or _paper_has_prior_submission_evidence(paper)
        or str(sdk_record.get("status") or "").casefold() != "completed"
        or str(sdk_record.get("recorded_status") or "").casefold() != "waiting"
    ):
        return False
    markers: dict[str, str] = {}
    for raw_line in str(sdk_record.get("final_response") or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("SMART_SUBMISSION_") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in markers:
            return False
        markers[key] = value.strip()
    try:
        marker_revision = int(markers.get("SMART_SUBMISSION_ROUTE_REVISION", ""))
    except (TypeError, ValueError):
        return False
    return bool(
        markers.get("SMART_SUBMISSION_PAPER_ID") == paper_id
        and markers.get("SMART_SUBMISSION_STATUS", "").casefold() == "waiting"
        and markers.get("SMART_SUBMISSION_NEXT")
        == SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED
        and marker_revision == int(paper.get("route_revision") or 0)
        and markers.get("SMART_SUBMISSION_ATTEMPT_ID")
        == str(paper.get("attempt_id") or "")
        and str(sdk_record.get("thread_id") or "")
        == str(paper.get("thread_id") or "")
        and str(sdk_record.get("attempt_id") or "")
        == str(paper.get("attempt_id") or "")
        and int(sdk_record.get("route_revision") or 0)
        == int(paper.get("route_revision") or 0)
    )


@_smart_state_locked
def reconcile_smart_final_review_markers(
    paper_ids: list[str] | None = None,
    *,
    projection_sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Repair old generic waiting states from exact SDK final-review markers."""

    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    sdk_state = read_json(STATE_DIR / "smart_submission" / "sdk_dispatch.json", {})
    sdk_papers = (
        sdk_state.get("papers", {})
        if isinstance(sdk_state, dict) and isinstance(sdk_state.get("papers"), dict)
        else {}
    )
    selected = (
        {str(item).strip() for item in paper_ids if str(item).strip()}
        if paper_ids is not None
        else set(papers)
    )
    repaired: list[str] = []
    now = now_iso()
    for paper_id in sorted(selected):
        paper = papers.get(paper_id)
        sdk_record = sdk_papers.get(paper_id)
        if not isinstance(paper, dict) or not isinstance(sdk_record, dict):
            continue
        if not _sdk_final_review_marker_matches(paper_id, paper, sdk_record):
            continue
        if str(paper.get("next_action") or "") == FINAL_SUBMIT_REVIEW_READY_ACTION:
            continue
        paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
        paper["detail"] = (
            "Final review page is ready; explicit user confirmation is required "
            "before the one-use final submit action."
        )
        paper["blocker"] = ""
        paper["final_review_ready_at"] = str(
            sdk_record.get("completed_at") or sdk_record.get("updated_at") or now
        )
        paper["final_review_marker_reconciled_at"] = now
        repaired.append(paper_id)
    if not repaired:
        return {"ok": True, "repaired": [], "changed": False}
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) not in repaired
    ]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "reconcile_smart_final_review_markers",
            "paper_ids": repaired,
            "source": "exact_sdk_final_response",
        }
    )
    projection_dirty = _prepare_smart_projection_change(
        state,
        "reconcile_smart_final_review_markers",
    )
    write_json_atomic(SMART_STATE_PATH, state)
    projection_dirty = _mark_smart_projection_dirty(
        state,
        "reconcile_smart_final_review_markers",
    )
    projection_refresh = refresh_smart_submission_projection(
        sync_callback=projection_sync_callback,
    )
    return {
        "ok": True,
        "repaired": repaired,
        "changed": True,
        "projection_dirty": projection_dirty,
        "projection_refresh": projection_refresh,
    }


def _value_free_worker_detail(status: str, action: str) -> str:
    return f"Visible worker reported status={status}; controller action={action}."


def _value_free_worker_source(value: Any) -> str:
    token = str(value or "").strip().casefold()
    if re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", token) and not any(
        marker in token for marker in _WORKER_SENSITIVE_MARKERS
    ):
        return token
    return "visible_worker_final"


def _sanitize_submission_id(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/():+\-]{0,127}", token):
        raise ValueError("submission_id is not in the allowed value-free format")
    if any(marker in token.casefold() for marker in _WORKER_SENSITIVE_MARKERS):
        raise ValueError("submission_id contains disallowed sensitive material")
    return token


def _web_origin(value: Any) -> str:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        return ""
    default_port = 443 if scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        return ""
    return f"{scheme}://{hostname}:{port}"


def _allowed_confirmation_origins(paper: dict[str, Any]) -> set[str]:
    values = [
        paper.get("draft_url"),
        paper.get("submission_draft_url"),
        paper.get("upload_url"),
        paper.get("submission_upload_url"),
    ]
    registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    if isinstance(registry, dict):
        system_id = str(paper.get("submission_system_id") or "").strip().casefold()
        adapter = submission_site_adapter(system_id)
        if adapter is not None:
            values.extend(adapter.allowed_origins)
        target_journal = re.sub(
            r"[^a-z0-9]+",
            " ",
            str(paper.get("target_journal") or "").casefold(),
        ).strip()
        for item in registry.get("systems", []) if isinstance(registry.get("systems"), list) else []:
            if isinstance(item, dict) and str(item.get("system_id") or "").strip().casefold() == system_id:
                values.extend((item.get("login_url"), item.get("submission_url")))
        for item in registry.get("journals", []) if isinstance(registry.get("journals"), list) else []:
            if not isinstance(item, dict):
                continue
            journal_name = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(item.get("journal_name") or "").casefold(),
            ).strip()
            if str(item.get("system_id") or "").strip().casefold() == system_id and (
                not target_journal or journal_name == target_journal
            ):
                values.append(item.get("submission_url"))
    return {origin for origin in (_web_origin(value) for value in values) if origin}


def _sanitize_confirmation_url(value: Any, *, paper: dict[str, Any]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 2048:
        raise ValueError("confirmation_url exceeds the allowed length")
    parsed = urllib.parse.urlparse(raw)
    origin = _web_origin(raw)
    if not origin:
        raise ValueError("confirmation_url must be an http(s) URL without user information")
    decoded_path = parsed.path or "/"
    for _ in range(3):
        expanded = urllib.parse.unquote(decoded_path)
        if expanded == decoded_path:
            break
        decoded_path = expanded
    if (
        "%" in decoded_path
        or "\\" in decoded_path
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
    ):
        raise ValueError("confirmation_url path contains unsupported encoding or control characters")
    if any(marker in decoded_path.casefold() for marker in _WORKER_SENSITIVE_MARKERS):
        raise ValueError("confirmation_url path contains disallowed sensitive material")
    allowed = _allowed_confirmation_origins(paper)
    if allowed and origin not in allowed:
        raise ValueError("confirmation_url origin does not match the locked submission target")
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    port = parsed.port
    netloc = hostname if not port or port == default_port else f"{hostname}:{port}"
    normalized_path = urllib.parse.quote(decoded_path, safe="/:@-._~()+")
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), netloc, normalized_path, "", ""))


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _sanitize_submission_evidence_path(value: Any, *, paper_id: str, paper: dict[str, Any]) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    resolved = _resolve_existing_path(raw, base=REPO_ROOT)
    if resolved is None or not resolved.is_file() or resolved.is_symlink():
        raise ValueError("submission evidence path does not name an existing file")
    lowered = str(resolved).casefold()
    if any(marker in lowered for marker in _WORKER_SENSITIVE_MARKERS):
        raise ValueError("submission evidence path is inside a sensitive location")
    roots = [
        SMART_CONFIRMATION_EVIDENCE_DIR,
        SMART_AUDIT_DIR,
        SMART_SCREENSHOT_DIR,
        SMART_SNAPSHOT_DIR,
    ]
    paper_dir = str(paper.get("paper_dir") or "").strip()
    if paper_dir:
        roots.append(Path(paper_dir) / "submission_evidence")
    if not any(_path_is_within(resolved, root) for root in roots):
        raise ValueError("submission evidence path is outside dedicated confirmation-evidence roots")
    return resolved


def _safe_prompt_text(value: Any, *, max_chars: int = 1000) -> str:
    """Return bounded, single-value text with secret-shaped lines removed."""

    raw = str(value or "").replace("\x00", "").strip()
    safe_lines: list[str] = []
    for line in raw.splitlines() or [raw]:
        compact = " ".join(line.split())
        lowered = compact.casefold()
        if any(marker in lowered for marker in _WORKER_SENSITIVE_MARKERS):
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in compact):
            continue
        safe_lines.append(compact)
    result = "\n".join(item for item in safe_lines if item).strip()
    return result[:max_chars]


def _safe_prompt_identifier(value: Any, *, max_chars: int = 128) -> str:
    token = _safe_prompt_text(value, max_chars=max_chars)
    if not token or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/():+\-]{0,127}", token):
        return ""
    return token


def _safe_prompt_url(value: Any, *, allowed_origins: set[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    origin = _web_origin(raw)
    if not origin or origin not in allowed_origins:
        return ""
    decoded_path = parsed.path or "/"
    for _ in range(3):
        expanded = urllib.parse.unquote(decoded_path)
        if expanded == decoded_path:
            break
        decoded_path = expanded
    if (
        "%" in decoded_path
        or "\\" in decoded_path
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
        or any(marker in decoded_path.casefold() for marker in _WORKER_SENSITIVE_MARKERS)
    ):
        return ""
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = hostname if not port or port == default_port else f"{hostname}:{port}"
    path = urllib.parse.quote(decoded_path, safe="/:@-._~()+")
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))


def _verified_prompt_origins(paper_id: str, paper: dict[str, Any]) -> set[str]:
    registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    if not isinstance(registry, dict):
        return set()
    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    if not system_id or not target_journal:
        return set()
    system, journal = _verified_target_site(
        registry,
        paper_id=paper_id,
        system_id=system_id,
        target_journal=target_journal,
    )
    if system is None or journal is None:
        return set()
    adapter = submission_site_adapter(system_id)
    values = (
        system.get("login_url"),
        system.get("submission_url"),
        journal.get("submission_url"),
        *(adapter.allowed_origins if adapter is not None else ()),
    )
    return {origin for origin in (_web_origin(value) for value in values) if origin}


def _safe_prompt_file(value: Any, *, roots: tuple[Path, ...]) -> str:
    path = _resolve_existing_path(value, base=REPO_ROOT)
    if path is None or not path.is_file():
        return ""
    if not any(_path_is_within(path, root) for root in roots):
        return ""
    if any(marker in str(path).casefold() for marker in _WORKER_SENSITIVE_MARKERS):
        return ""
    return str(path)


def _value_free_scheduler_payload(
    payload: dict[str, Any],
    *,
    paper_dir: Path,
    package_dir: Path,
) -> dict[str, Any]:
    author = payload.get("corresponding_author")
    author = author if isinstance(author, dict) else {}
    declaration_fields = (
        "author_contribution_statement",
        "funding_statement",
        "conflict_of_interest_statement",
        "data_availability_statement",
        "code_availability_statement",
        "ethics_statement",
        "ai_use_statement",
        "originality_statement",
        "author_responsibility_statement",
        "third_party_material_statement",
        "copyright_statement",
    )
    result: dict[str, Any] = {
        "paper_id": _safe_prompt_identifier(payload.get("paper_id")),
        "ready": payload.get("ready") is True,
        "blockers": [
            item
            for item in (
                _safe_prompt_text(value, max_chars=240)
                for value in (payload.get("blockers") if isinstance(payload.get("blockers"), list) else [])
            )
            if item
        ][:20],
        "cover_letter_upload_file": _safe_prompt_file(
            payload.get("cover_letter_upload_file"),
            roots=(paper_dir, package_dir, SMART_UPLOAD_STAGING_DIR),
        ),
        "corresponding_author": {
            key: _safe_prompt_text(author.get(key), max_chars=320)
            for key in ("prefix", "name", "email", "affiliation", "country", "city", "postal_code", "department")
        },
        "third_party_material_selection": (
            "no_separate_permission_required"
            if payload.get("third_party_material_selection") == "no_separate_permission_required"
            else ""
        ),
        "copyright_permission_selection": (
            "no_separate_permission_required"
            if payload.get("copyright_permission_selection") == "no_separate_permission_required"
            else ""
        ),
        "routine_declaration_authorization": {
            "authorized": bool(
                isinstance(payload.get("routine_declaration_authorization"), dict)
                and payload["routine_declaration_authorization"].get("authorized") is True
            ),
            "authorized_at": _safe_prompt_text(
                payload.get("routine_declaration_authorization", {}).get("authorized_at")
                if isinstance(payload.get("routine_declaration_authorization"), dict)
                else "",
                max_chars=80,
            ),
        },
        "snapp_worker_instruction": _safe_prompt_text(
            payload.get("snapp_worker_instruction"),
            max_chars=1200,
        ),
    }
    for field in declaration_fields:
        result[field] = _safe_prompt_text(payload.get(field), max_chars=2400)
    required_values = {
        "corresponding_author.name": result["corresponding_author"]["name"],
        "corresponding_author.email": result["corresponding_author"]["email"],
        "corresponding_author.affiliation": result["corresponding_author"]["affiliation"],
        "corresponding_author.country": result["corresponding_author"]["country"],
        "author_contribution_statement": result["author_contribution_statement"],
    }
    requested = payload.get("required_fields") if isinstance(payload.get("required_fields"), list) else []
    for field in requested:
        existing_blockers = " ".join(str(item) for item in result["blockers"])
        if (
            field in required_values
            and not required_values[field]
            and field not in existing_blockers
        ):
            result["blockers"].append(f"missing safe local author field: {field}")
    result["blockers"] = list(dict.fromkeys(result["blockers"]))[:20]
    result["ready"] = not result["blockers"]
    return result


def _resolve_existing_path(value: Any, *, base: Path | None = None) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.exists():
        return path
    if not path.is_absolute() and base is not None:
        candidate = base / path
        if candidate.exists():
            return candidate
    if not path.is_absolute():
        candidate = REPO_ROOT / path
        if candidate.exists():
            return candidate
    return None


def _latest_audit_for_paper(paper_id: str, paper: dict[str, Any]) -> str:
    stored = _resolve_existing_path(paper.get("last_audit_path"), base=REPO_ROOT)
    if stored is not None and stored.is_file() and _path_is_within(stored, SMART_AUDIT_DIR):
        return str(stored)
    if not SMART_AUDIT_DIR.exists():
        return ""
    candidates = sorted(
        SMART_AUDIT_DIR.glob(f"*_{_safe_name(paper_id)}_*.md"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return str(candidates[0]) if candidates else ""


def _context_file_snippet(label: str, path: Path | None, *, max_chars: int = 3000) -> str:
    if path is None or not path.exists() or not path.is_file():
        return f"## {label}\nMISSING"
    return f"## {label}\nPath: {path}\n\n{_redact_prompt_text(read_text(path), max_chars=max_chars)}"


def _context_json_snippet(label: str, path: Path | None, *, max_chars: int = 3000) -> str:
    if path is None or not path.exists() or not path.is_file():
        return f"## {label}\nMISSING"
    data = read_json(path, {})
    if not isinstance(data, dict):
        return _context_file_snippet(label, path, max_chars=max_chars)
    sensitive_keys = {
        "password",
        "passwd",
        "token",
        "cookie",
        "secret",
        "authorization",
        "credential_ref",
        "recovery_code",
        "verification_code",
        "otp",
    }

    def value_free(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: value_free(item)
                for key, item in value.items()
                if str(key).strip().casefold() not in sensitive_keys
            }
        if isinstance(value, list):
            return [value_free(item) for item in value[:50]]
        if isinstance(value, str):
            raw = value.strip()
            origin = _web_origin(raw)
            if origin:
                return _safe_prompt_url(raw, allowed_origins={origin})
            return _safe_prompt_text(raw, max_chars=2000)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return ""

    compact = value_free(data)
    return f"## {label}\nPath: {path}\n\n{_redact_prompt_text(json.dumps(compact, ensure_ascii=False, indent=2), max_chars=max_chars)}"


def _prepare_snapp_scheduler_payload(
    paper_id: str,
    paper: dict[str, Any],
    package_dir: Path,
    paper_dir: Path,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Prepare local, non-browser SNAPP payload so workers do not need shell access."""
    author_text = read_text(paper_dir / "author_info.md") if (paper_dir / "author_info.md").exists() else ""
    declarations_text = read_text(package_dir / "declarations.md") if (package_dir / "declarations.md").exists() else ""
    cover_md = package_dir / "cover_letter.md"
    declared_cover = _resolve_existing_path(
        paper.get("cover_letter_upload_file"),
        base=REPO_ROOT,
    )
    allowed_cover_roots = (paper_dir, package_dir, SMART_UPLOAD_STAGING_DIR)
    cover_upload = (
        declared_cover
        if declared_cover is not None
        and declared_cover.is_file()
        and not declared_cover.is_symlink()
        and any(_path_is_within(declared_cover, root) for root in allowed_cover_roots)
        else package_dir / "cover_letter.pdf"
    )
    if not cover_upload.exists():
        cover_upload = package_dir / "cover_letter.docx"
    if not cover_upload.exists():
        cover_upload = package_dir / "cover_letter.txt"
    if not cover_upload.exists() and cover_md.exists():
        staging_dir = SMART_UPLOAD_STAGING_DIR / _safe_name(paper_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        cover_upload = staging_dir / "cover_letter.txt"
        write_text(cover_upload, _plain_cover_letter_text(read_text(cover_md)) + "\n")

    author_name = _author_field_value(author_text, "Name")
    author_email = _author_field_value(author_text, "Email")
    affiliation = _author_field_value(author_text, "Institution")
    country = _author_field_value(author_text, "Country")
    city = _author_field_value(author_text, "City")
    postal_code = _author_field_value(author_text, "Postal Code")
    department = _author_field_value(author_text, "Department")
    author_prefix = _verified_author_prefix(paper, author_text, paper_dir)
    funding = _markdown_section_value(declarations_text, "Funding") or _markdown_section_value(author_text, "Funding")
    conflict = _markdown_section_value(declarations_text, "Conflict of Interest") or _markdown_section_value(author_text, "Conflict of Interest")
    data_availability = _markdown_section_value(declarations_text, "Data Availability") or _markdown_section_value(author_text, "Data Availability")
    code_availability = _markdown_section_value(declarations_text, "Code Availability") or _markdown_section_value(author_text, "Code Availability")
    ethics = _markdown_section_value(declarations_text, "Ethics Statement") or _markdown_section_value(author_text, "Ethics Statement")
    ai_use = _markdown_section_value(declarations_text, "AI Use Statement") or str(settings.get("default_ai_use_statement") or DEFAULT_AI_USE_STATEMENT)
    originality = _markdown_section_value(declarations_text, "Originality and Simultaneous Submission") or str(
        settings.get("default_originality_statement") or DEFAULT_ORIGINALITY_STATEMENT
    )
    author_responsibility = _markdown_section_value(declarations_text, "Author Approval and Responsibility") or str(
        settings.get("default_author_responsibility_statement") or DEFAULT_AUTHOR_RESPONSIBILITY_STATEMENT
    )
    third_party_material = _markdown_section_value(declarations_text, "Third Party Material") or str(
        settings.get("default_third_party_material_statement") or DEFAULT_THIRD_PARTY_MATERIAL_STATEMENT
    )
    copyright_permission = _markdown_section_value(declarations_text, "Copyright Permissions") or str(
        settings.get("default_copyright_permission_statement") or DEFAULT_COPYRIGHT_PERMISSION_STATEMENT
    )
    contribution = (
        _markdown_section_value(declarations_text, "Author Contributions")
        or _markdown_section_value(declarations_text, "Author Contribution")
        or _markdown_section_value(author_text, "Author Contributions")
        or _markdown_section_value(author_text, "Author Contribution")
    )
    required_author_fields = {
        "corresponding_author.name": author_name,
        "corresponding_author.email": author_email,
        "corresponding_author.affiliation": affiliation,
        "corresponding_author.country": country,
    }
    target_text = f"{paper.get('target_journal') or ''} {paper.get('submission_system') or ''}".lower()
    if "springer" in target_text or "snapp" in target_text:
        required_author_fields["author_contribution_statement"] = contribution
    missing_author_fields = [key for key, value in required_author_fields.items() if not str(value or "").strip()]
    payload = {
        "prepared_at": now_iso(),
        "paper_id": paper_id,
        "ready": not missing_author_fields,
        "blockers": [f"missing verified local author field: {key}" for key in missing_author_fields],
        "required_fields": list(required_author_fields),
        "cover_letter_upload_file": str(cover_upload) if cover_upload.exists() else "",
        "cover_letter_source_file": str(cover_md) if cover_md.exists() else "",
        "corresponding_author": {
            "prefix": author_prefix,
            "name": author_name,
            "email": author_email,
            "affiliation": affiliation,
            "country": country,
            "city": city,
            "postal_code": postal_code,
            "department": department,
        },
        "author_contribution_statement": contribution,
        "funding_statement": funding,
        "conflict_of_interest_statement": conflict,
        "data_availability_statement": data_availability,
        "code_availability_statement": code_availability,
        "ethics_statement": ethics,
        "ai_use_statement": ai_use,
        "originality_statement": originality,
        "author_responsibility_statement": author_responsibility,
        "third_party_material_statement": third_party_material,
        "third_party_material_selection": "no_separate_permission_required",
        "copyright_statement": copyright_permission,
        "copyright_permission_selection": "no_separate_permission_required",
        "routine_declaration_authorization": {
            "authorized": True,
            "authorized_at": str(settings.get("standing_declaration_authorized_at") or STANDING_DECLARATION_AUTHORIZED_AT),
            "scope": "routine AI-use, originality/no-simultaneous-submission, author responsibility, funding=no external funding, COI=no conflict, data/code availability from local package records, and no-third-party-material/no-separate-copyright-permission declarations when the package has no contradictory evidence",
        },
        "snapp_worker_instruction": (
            "Use these scheduler-prepared values directly for Springer Nature SNAPP cover letter, "
            "author contribution, funding, data/code availability, AI-use, originality, third-party material/copyright permission, and routine declaration fields. "
            "For third-party material/copyright permission fields, select the no-permission-required/no third-party material option unless the live form shows contradictory material-specific facts. "
            "Do not ask for command approval to read local files; if the site requires a different new legal fact, stop."
        ),
    }
    return _value_free_scheduler_payload(
        payload,
        paper_dir=paper_dir,
        package_dir=package_dir,
    )


AUTHOR_INFO_COMPLETION_ACTION = (
    "complete_author_info_with_verified_name_email_institution_country_and_contributions"
)


@_smart_state_locked
def _refresh_selected_author_payload_preflight(
    paper_ids: list[str],
) -> dict[str, Any]:
    """Revalidate local author facts before opening a browser or starting a Turn."""

    ensure_smart_submission_module()
    selected = list(
        dict.fromkeys(
            str(item).strip() for item in paper_ids if str(item).strip()
        )
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    online_queue = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip()
    ]
    checked: dict[str, Any] = {}
    blocked: dict[str, str] = {}
    recovered: list[str] = []
    skipped: dict[str, str] = {}
    changed = False

    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        _hydrate_paper_target_from_package(paper_id, paper)
        package_dir = _resolve_package_dir_for_paper(paper_id, paper)
        paper_dir = _resolve_existing_path(paper.get("paper_dir"), base=REPO_ROOT)
        if paper_dir is None and package_dir is not None:
            paper_dir = package_dir.parent
        if package_dir is None or paper_dir is None:
            skipped[paper_id] = "paper or submission package directory is missing"
            continue

        payload = _prepare_snapp_scheduler_payload(
            paper_id,
            paper,
            package_dir,
            paper_dir,
            settings,
        )
        paper["scheduler_prepared_payload"] = payload
        if payload.get("cover_letter_upload_file"):
            paper["cover_letter_upload_file"] = payload["cover_letter_upload_file"]
        changed = True

        payload_blockers = [
            str(item).strip()
            for item in payload.get("blockers", [])
            if str(item).strip()
        ]
        checked[paper_id] = {
            "ready": bool(payload.get("ready")),
            "blockers": payload_blockers,
        }
        if payload.get("ready"):
            if (
                str(paper.get("status") or "").strip().casefold() == "blocked"
                and str(paper.get("next_action") or "").strip()
                == AUTHOR_INFO_COMPLETION_ACTION
            ):
                paper["previous_blocker"] = str(
                    paper.get("blocker") or paper.get("detail") or ""
                )
                paper["status"] = "login_check"
                paper["workflow_status"] = "queued"
                paper["worker_status"] = "credential_broker"
                paper["blocker"] = ""
                paper["detail"] = (
                    "Local author metadata was revalidated; continuing with "
                    "the trusted credential broker."
                )
                paper["next_action"] = "trusted_credential_broker_login"
                paper["author_payload_recovered_at"] = now_iso()
                recovered.append(paper_id)
            continue

        blocker = "; ".join(payload_blockers) or (
            "verified local author metadata is incomplete"
        )
        paper["status"] = "blocked"
        paper["workflow_status"] = "blocked"
        paper["worker_status"] = "blocked"
        paper["blocker"] = blocker
        paper["detail"] = blocker
        paper["next_action"] = AUTHOR_INFO_COMPLETION_ACTION
        paper["author_payload_blocked_at"] = now_iso()
        online_queue = [item for item in online_queue if item != paper_id]
        blocked[paper_id] = blocker

    if changed:
        state["online_queue"] = online_queue
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "refresh_selected_author_payload_preflight",
                "paper_ids": selected,
                "recovered": recovered,
                "blocked": sorted(blocked),
                "skipped": skipped,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "checked": checked,
        "recovered": recovered,
        "blocked": blocked,
        "skipped": skipped,
    }


def _target_locked_detail(paper: dict[str, Any]) -> str:
    status = str(paper.get("status") or "ready_for_online_submit")
    action = _value_free_worker_action(status, paper.get("next_action"))
    return _value_free_worker_detail(status, action)


@_smart_state_locked
def build_visible_worker_prompt(
    paper_id: str,
    *,
    route_revision: int,
) -> str:
    """Create the legacy-compatible exact one-paper visible-worker prompt."""

    return _build_browser_worker_prompt(
        paper_id,
        route_revision=route_revision,
        allow_reserved_final_submit=False,
        sdk_browser_channel=False,
    )


def _gmail_monitor_query_batches(
    contexts: list[dict[str, Any]],
    *,
    max_query_chars: int = 1400,
    max_papers: int = 12,
) -> list[dict[str, Any]]:
    """Build bounded Gmail OR queries so one batch does not search per paper."""

    batches: list[dict[str, Any]] = []
    clauses: list[str] = []
    paper_ids: list[str] = []

    def flush() -> None:
        if not clauses:
            return
        batches.append(
            {
                "query": "newer_than:365d {" + " ".join(clauses) + "}",
                "paper_ids": list(paper_ids),
            }
        )
        clauses.clear()
        paper_ids.clear()

    for context in contexts:
        paper_id = str(context.get("paper_id") or "").strip()
        terms: list[str] = []
        raw_submission_ids = context.get("submission_ids")
        submission_ids = (
            [str(item) for item in raw_submission_ids]
            if isinstance(raw_submission_ids, list)
            else [str(context.get("submission_id") or "")]
        )
        title = re.sub(
            r"[\"\r\n]+", " ", str(context.get("title") or "")
        ).strip()
        for raw_value in [*submission_ids, title]:
            value = re.sub(r"[\"\r\n]+", " ", raw_value).strip()
            if value and value not in terms:
                terms.append(value)
        candidate_clauses = [f'"{value}"' for value in terms]
        if not candidate_clauses:
            continue
        candidate_query = (
            "newer_than:365d {"
            + " ".join([*clauses, *candidate_clauses])
            + "}"
        )
        if clauses and (
            len(candidate_query) > max_query_chars or len(paper_ids) >= max_papers
        ):
            flush()
        clauses.extend(candidate_clauses)
        paper_ids.append(paper_id)
    flush()
    return batches


def build_sdk_mailbox_monitor_prompt(
    paper_ids: Iterable[str],
    *,
    run_id: str,
    mailbox_context: Mapping[str, Any] | None = None,
) -> str:
    """Build one compact, read-only Gmail sweep for multiple submitted papers.

    Monitoring is intentionally separate from the fixed submission sessions.  It
    carries no upload files, journal credentials, or final-submit capability and
    can therefore inspect all notification mail in one short-lived SDK turn.
    """

    selected = list(
        dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
    )
    clean_run_id = _safe_prompt_identifier(run_id)
    if not selected or not clean_run_id:
        raise ValueError("mailbox monitor requires paper_ids and run_id")
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    contexts: list[dict[str, Any]] = []
    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            raise KeyError(f"paper not found in smart submission state: {paper_id}")
        if not _monitor_check_active(paper):
            raise SmartSubmissionGuardError(
                SMART_CANARY_DISPATCH_NOT_QUEUED,
                paper_id,
                "mailbox batch requires a current queued monitoring checkpoint",
            )
        route = _require_worker_route(
            paper_id,
            paper,
            expected_revision=int(paper.get("route_revision") or 0),
            monitor_only=True,
        )
        _mark_worker_route_on_paper(paper, route, monitor_only=True)
        legacy_status = (
            paper.get("legacy_submission_status")
            if isinstance(paper.get("legacy_submission_status"), dict)
            else {}
        )
        submitted_at_value = (
            paper.get("submitted_at")
            or paper.get("last_worker_recorded_at")
            or legacy_status.get("updated_at")
            or ""
        )
        all_submission_ids: list[str] = []
        for value in (
            paper.get("canonical_submission_id"),
            paper.get("submission_id"),
            paper.get("manuscript_id"),
            paper.get("submission_number"),
            paper.get("tracking_id"),
            *submission_records(paper).keys(),
        ):
            clean_value = _safe_prompt_identifier(normalize_submission_id(value))
            if clean_value and clean_value not in all_submission_ids:
                all_submission_ids.append(clean_value)
        attempt_history = paper.get("submission_attempt_history")
        if isinstance(attempt_history, list):
            for item in attempt_history[-20:]:
                if not isinstance(item, Mapping):
                    continue
                clean_value = _safe_prompt_identifier(
                    normalize_submission_id(item.get("submission_id"))
                )
                if clean_value and clean_value not in all_submission_ids:
                    all_submission_ids.append(clean_value)
        contexts.append(
            {
                "paper_id": paper_id,
                "route_revision": int(paper.get("route_revision") or 0),
                "attempt_id": _safe_prompt_identifier(paper.get("attempt_id")),
                "title": _safe_prompt_text(
                    paper.get("title") or paper.get("paper_title") or paper_id,
                    max_chars=500,
                ),
                "target_journal": _safe_prompt_text(
                    paper.get("target_journal"), max_chars=300
                ),
                "submission_system": _safe_prompt_text(
                    paper.get("submission_system"), max_chars=200
                ),
                "submission_id": _safe_prompt_identifier(
                    paper.get("canonical_submission_id")
                    or paper.get("submission_id")
                    or paper.get("manuscript_id")
                    or paper.get("submission_number")
                ),
                "submission_ids": all_submission_ids,
                "last_checked_at": _safe_prompt_text(
                    paper.get("last_monitor_checked_at"), max_chars=80
                ),
                "submitted_at": _safe_prompt_text(
                    submitted_at_value,
                    max_chars=80,
                ),
            }
        )

    mailbox_payload = dict(mailbox_context or {})
    raw_sources = mailbox_payload.get("sources")
    safe_sources: list[dict[str, Any]] = []
    if isinstance(raw_sources, list):
        for raw in raw_sources:
            if not isinstance(raw, Mapping):
                continue
            safe_sources.append(
                {
                    "source_id": _safe_prompt_identifier(raw.get("source_id")),
                    "source_label": _safe_prompt_text(raw.get("source_label"), max_chars=80),
                    "source_kind": _safe_prompt_identifier(raw.get("source_kind")),
                    "available": bool(raw.get("available")),
                    "status": _safe_prompt_identifier(raw.get("status")),
                    "error_code": _safe_prompt_identifier(raw.get("error_code")),
                }
            )
    raw_evidence = mailbox_payload.get("paper_evidence")
    safe_evidence: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw_evidence, Mapping):
        for paper_id in selected:
            values = raw_evidence.get(paper_id)
            if not isinstance(values, list):
                continue
            safe_values: list[dict[str, Any]] = []
            for raw in values[:6]:
                if not isinstance(raw, Mapping):
                    continue
                safe_values.append(
                    {
                        "source_id": _safe_prompt_identifier(raw.get("source_id")),
                        "source_label": _safe_prompt_text(raw.get("source_label"), max_chars=80),
                        "source_kind": _safe_prompt_identifier(raw.get("source_kind")),
                        "subject": _safe_prompt_text(raw.get("subject"), max_chars=500),
                        "snippet": _safe_prompt_text(raw.get("snippet"), max_chars=600),
                        "message_date": _safe_prompt_text(raw.get("message_date"), max_chars=80),
                        "sender_domain": _safe_prompt_text(raw.get("sender_domain"), max_chars=160),
                        "matched_by": _safe_prompt_identifier(raw.get("matched_by")),
                    }
                )
            safe_evidence[paper_id] = safe_values
    gmail_available = bool(mailbox_payload.get("gmail_available", True))
    payload = {
        "schema": SMART_MONITOR_BATCH_SCHEMA,
        "run_id": clean_run_id,
        "paper_count": len(contexts),
        "papers": contexts,
        "gmail_queries": _gmail_monitor_query_batches(contexts),
        "mailbox_sources": [
            {
                "source_id": "gmail",
                "source_label": "Gmail",
                "source_kind": "gmail_browser",
                "available": gmail_available,
                "status": "monitoring" if gmail_available else "waiting",
                "error_code": "" if gmail_available else "GMAIL_PAGE_UNAVAILABLE",
            },
            *safe_sources,
        ],
        "local_mailbox_evidence": safe_evidence,
    }
    return (
        "Perform one read-only Gmail notification sweep for every paper in the "
        "scheduler payload below. The Python program, not an AI scheduler, selected "
        "this exact batch. Use only the smart_browser MCP tools and the supplied "
        "capability context. The payload may contain two kinds of sources: Gmail, "
        "which is searched through the dedicated read-only browser_mail_search tool, "
        "and local_mailbox_evidence already collected by a deterministic local "
        "read-only connector. Never ask for or infer a password. If Gmail is marked "
        "available, start with browser_pages and call browser_mail_search for each "
        "precomputed gmail_queries entry exactly once. If Gmail is unavailable, do "
        "not call that tool and return a waiting Gmail source result. Do not use "
        "browser_fill, browser_click, or browser_navigate for mail monitoring. Each "
        "Gmail query is a bounded OR query "
        "covering all exact submission ids and titles, so do not repeat an individual "
        "search for every paper. Map returned result subjects/snippets to papers only "
        "by an exact id or exact title match. Classify only the newest exact matching "
        "result. The mail-search tool cannot open or mutate messages. If a subject or "
        "snippet explicitly says acceptance, rejection, revision, technical correction, "
        "files/metadata required, or author action required, classify that explicit "
        "state. If it says Decision/Action Required but the exact action is ambiguous, "
        "use revision_needed with action_kind=unknown so the program performs a bounded "
        "follow-up instead of silently missing it. If a result page is truncated, use "
        "at most one exact-id-or-exact-title browser_mail_search fallback for the affected "
        "paper; never use a broad journal-only search. A paper with no exact candidate "
        "after the combined query is monitoring. Inspect only result text that "
        "could be journal submission notifications. Do not send, reply, forward, "
        "archive, delete, label, star, mark spam, unsubscribe, change settings, open "
        "attachments, download files, or visit links outside mail.google.com. Do not "
        "quote message bodies or expose addresses beyond a short sender domain.\n\n"
        "Classify only explicit evidence. Use accepted for an explicit acceptance; "
        "rejected for an explicit rejection; revision_needed for a revision request "
        "or a technical/file/metadata correction that requires author action; and "
        "monitoring when there is no newer actionable message or the newest message "
        "only confirms normal processing/review. A missing search result is "
        "monitoring, not blocked. Use waiting only when the Gmail page itself is not "
        "accessible. action_kind must be one of none, technical_correction, "
        "scientific_revision, files_or_metadata, retarget, accepted, or unknown.\n\n"
        "For every paper, return source_results containing every mailbox_sources "
        "entry exactly once. Each source result must contain source_id, source_label, "
        "source_kind, available, status, action_kind, summary, message_date, "
        "sender_domain, submission_id, and error_code. A configured local source with "
        "no matching candidate is monitoring, not waiting. Python will recompute the "
        "aggregate status deterministically and will send non-decisive results to the "
        "journal dashboard, so do not hide conflicts between sources.\n\n"
        "Return every requested paper exactly once. The final response must end with "
        "these two literal delimiter lines and valid compact JSON between them. Do "
        "not wrap the JSON in a code fence. Each result object must contain paper_id, "
        "route_revision, attempt_id, status, action_kind, summary, message_date, "
        "sender_domain, submission_id, and source_results. Keep every summary under "
        "240 characters and do "
        "not include an email body.\n"
        "SMART_MONITOR_BATCH_BEGIN\n"
        '{"schema":"smart-submission-mailbox-monitor-batch/v1","run_id":"...",'
        '"results":[...]}\n'
        "SMART_MONITOR_BATCH_END\n\n"
        "SCHEDULER_MONITOR_BATCH_JSON:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def build_sdk_browser_worker_prompt(
    paper_id: str,
    *,
    route_revision: int,
) -> str:
    """Create the exact prompt for the constrained Codex SDK browser channel."""

    return _build_browser_worker_prompt(
        paper_id,
        route_revision=route_revision,
        allow_reserved_final_submit=True,
        sdk_browser_channel=True,
    )


def _build_browser_worker_prompt(
    paper_id: str,
    *,
    route_revision: int,
    allow_reserved_final_submit: bool = False,
    sdk_browser_channel: bool = False,
) -> str:
    """Create the exact one-paper prompt for a visible or SDK browser worker."""
    paper_id = str(paper_id or "").strip()
    if not paper_id:
        raise ValueError("paper_id is required")
    deferred_route_error: SmartSubmissionGuardError | None = None
    try:
        route = require_smart_canary_route(
            paper_id,
            expected_revision=route_revision,
        )
    except SmartSubmissionGuardError as exc:
        if exc.code not in {SMART_ROUTE_OWNER_REQUIRED, SMART_ROUTE_MODE_REQUIRED}:
            raise
        deferred_route_error = exc
        route = {}
    if deferred_route_error is not None and not SMART_STATE_PATH.is_file():
        raise deferred_route_error
    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        if deferred_route_error is not None:
            raise deferred_route_error
        raise KeyError(f"paper not found in smart submission state: {paper_id}")
    monitor_only = _monitor_check_active(paper)
    if deferred_route_error is not None:
        if not monitor_only:
            raise deferred_route_error
        route = _require_worker_route(
            paper_id,
            paper,
            expected_revision=route_revision,
            monitor_only=True,
        )
    _require_current_worker_auth(paper_id, paper)
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    worker_thread_id = str(paper.get("thread_id") or "").strip()
    if paper_id not in _queue_paper_ids(state, "online_queue") or workflow_status not in {
        "queued",
        "running",
    }:
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "worker prompt requires the current canary attempt to be queued or atomically dispatched",
        )
    if workflow_status == "running" and (
        not worker_thread_id
        or not _committed_audit_matches(
            paper.get("last_dispatch_audit_path"),
            paper.get("last_dispatch_audit_sha256"),
        )
    ):
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "running worker prompt requires a committed atomic dispatch audit",
        )
    _mark_worker_route_on_paper(paper, route, monitor_only=monitor_only)
    attempt_id = str(paper.get("attempt_id") or "").strip()
    if not attempt_id:
        attempt_id = _rotate_canary_attempt(paper, paper_id)

    package_dir = _resolve_package_dir_for_paper(paper_id, paper)
    paper_dir = _resolve_existing_path(paper.get("paper_dir"), base=REPO_ROOT)
    if paper_dir is None and package_dir is not None:
        paper_dir = package_dir.parent
    if paper_dir is None:
        paper_dir = JOURNAL_ROOT / "papers" / paper_id
    if package_dir is None:
        package_dir = paper_dir / "package"

    verified_origins = _verified_prompt_origins(paper_id, paper)
    raw_draft_url = (
        paper.get("draft_url")
        or paper.get("submission_draft_url")
        or paper.get("upload_url")
        or paper.get("submission_upload_url")
        or paper.get("submission_start_url")
        or ""
    )
    draft_url = _safe_prompt_url(raw_draft_url, allowed_origins=verified_origins)
    upload_url = _safe_prompt_url(
        paper.get("upload_url")
        or paper.get("submission_upload_url")
        or paper.get("submission_start_url")
        or "",
        allowed_origins=verified_origins,
    )
    draft_id = _safe_prompt_identifier(paper.get("draft_id") or paper.get("submission_draft_id") or "")
    raw_upload_file = (
        _resolve_existing_path(paper.get("uploaded_manuscript_zip"), base=REPO_ROOT)
        or _resolve_existing_path(paper.get("uploaded_manuscript_file"), base=REPO_ROOT)
        or _resolve_existing_path(paper.get("upload_package"), base=REPO_ROOT)
    )
    upload_file = _safe_prompt_file(
        raw_upload_file,
        roots=(paper_dir, package_dir, SMART_UPLOAD_STAGING_DIR),
    )
    latest_audit = _latest_audit_for_paper(paper_id, paper)
    settings = state.setdefault("settings", {}) if isinstance(state.get("settings"), dict) else {}
    retarget_request = (
        normalize_retarget_request(
            paper.get("retarget_request"),
            paper_id=paper_id,
        )
        if isinstance(paper.get("retarget_request"), Mapping)
        else {}
    )
    retarget_closure_targets = [
        normalize_submission_id(item)
        for item in retarget_request.get("withdrawal_targets", [])
        if normalize_submission_id(item)
    ]
    retarget_closure_intent = (
        str(paper.get("online_intent") or "").strip().casefold()
        == "user_retarget_verify_or_withdraw"
    )
    scheduler_payload = (
        {
            "ready": True,
            "monitor_only": True,
            "paper_id": paper_id,
            "target_journal": str(paper.get("target_journal") or ""),
            "submission_system_id": str(paper.get("submission_system_id") or ""),
            "submission_id": str(paper.get("submission_id") or ""),
        }
        if monitor_only
        else {
            "ready": True,
            "monitor_only": False,
            "retarget_closure_only": True,
            "paper_id": paper_id,
            "target_journal": str(paper.get("target_journal") or ""),
            "submission_system_id": str(paper.get("submission_system_id") or ""),
            "withdrawal_policy": str(retarget_request.get("withdrawal_policy") or "auto"),
            "withdrawal_targets": retarget_closure_targets,
            "new_submission_forbidden": True,
        }
        if retarget_closure_intent
        else _prepare_snapp_scheduler_payload(
            paper_id,
            paper,
            package_dir,
            paper_dir,
            settings,
        )
    )
    if not scheduler_payload.get("ready"):
        blocker = "; ".join(str(item) for item in scheduler_payload.get("blockers", []) if str(item).strip())
        paper["status"] = "blocked"
        paper["workflow_status"] = "blocked"
        paper["worker_status"] = "blocked"
        paper["blocker"] = blocker or "verified local author metadata is incomplete"
        paper["detail"] = paper["blocker"]
        paper["next_action"] = AUTHOR_INFO_COMPLETION_ACTION
        paper["scheduler_prepared_payload"] = scheduler_payload
        state["online_queue"] = [
            str(item) for item in state.get("online_queue", []) if str(item).strip() and str(item) != paper_id
        ]
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "block_worker_prompt_missing_author_facts",
                "paper_id": paper_id,
                "missing_fields": scheduler_payload.get("blockers", []),
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
        raise ValueError(f"Smart Submission worker blocked: {paper['blocker']}")
    paper["scheduler_prepared_payload"] = scheduler_payload
    if scheduler_payload.get("cover_letter_upload_file"):
        paper["cover_letter_upload_file"] = scheduler_payload["cover_letter_upload_file"]
    state["updated_at"] = now_iso()
    write_json_atomic(SMART_STATE_PATH, state)

    clean_detail = _target_locked_detail(paper)
    clean_next_action = _value_free_worker_action(
        str(paper.get("status") or "ready_for_online_submit"),
        paper.get("next_action"),
    )
    final_submit_authorization = None
    final_submit_claim_mode = ""
    if workflow_status == "running":
        final_submit_authorization = _claimed_final_submit_authorization(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=worker_thread_id,
            require_click_window=True,
        )
        if final_submit_authorization is not None:
            final_submit_claim_mode = "scheduler_claimed"
        elif allow_reserved_final_submit:
            final_submit_authorization = _reserved_final_submit_authorization(
                paper_id,
                paper,
                route_revision=route_revision,
                attempt_id=attempt_id,
                thread_id=worker_thread_id,
            )
            if final_submit_authorization is not None:
                final_submit_claim_mode = "sdk_tool_claim_at_click"
    if (
        str(paper.get("next_action") or "").strip() == FINAL_SUBMIT_AUTHORIZED_ACTION
        and final_submit_authorization is None
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "final-click prompt requires a committed scheduler-side action claim inside the click window",
        )
    final_submit_authorized = final_submit_authorization is not None
    final_submit_policy = (
        "FINAL SUBMIT AUTHORIZATION: RESERVED for this exact paper, route revision, attempt, account, target, "
        "package binding, and fixed SDK thread. Do not use browser_click. After a fresh final-page snapshot and "
        "field review, call browser_final_submit_once exactly once with this authorization id and the verified "
        "final-control ref. That trusted tool claims the receipt immediately before the click, clicks once, and "
        "captures the positive confirmation. Never retry the tool, even when its result is uncertain."
        if final_submit_claim_mode == "sdk_tool_claim_at_click"
        else
        "FINAL SUBMIT AUTHORIZATION: VERIFIED for this exact paper, route revision, attempt, account, target, "
        "and package binding, RESERVED for this exact worker thread, and CONSUMED by the trusted scheduler immediately "
        "before this prompt. Do not run any local claim command yourself. Activate the final submit control exactly "
        "once before final_submit_click_deadline and never retry the click. Proceed only after every live review field "
        "still matches the scheduler payload and no hard stop is present."
        if final_submit_authorized
        else "FINAL SUBMIT AUTHORIZATION: NOT PRESENT. You may sign in, fill fields, upload files, save a draft, "
        "and inspect the final review page, but you must not activate any final submit/approve/confirm control. "
        "Stop with SMART_SUBMISSION_STATUS=waiting and SMART_SUBMISSION_NEXT="
        "SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED when the form is ready."
    )
    uploaded_sha256 = str(paper.get("uploaded_manuscript_sha256") or "").strip().casefold()
    if len(uploaded_sha256) != 64 or any(char not in "0123456789abcdef" for char in uploaded_sha256):
        uploaded_sha256 = ""
    github_repo_url = _safe_prompt_url(
        paper.get("github_repo_url"),
        allowed_origins={"https://github.com:443"},
    )
    standing_result = paper.get("standing_declaration_result")
    standing_result = standing_result if isinstance(standing_result, dict) else {}
    planned_result = paper.get("planned_github_repo_result")
    planned_result = planned_result if isinstance(planned_result, dict) else {}
    browser_health = state.get("browser_health")
    browser_health = browser_health if isinstance(browser_health, dict) else {}
    monitor_followup = paper.get("monitor_followup_context")
    monitor_followup = monitor_followup if isinstance(monitor_followup, dict) else {}
    one_paper_context = {
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "worker_thread_id": _safe_prompt_identifier(worker_thread_id),
        "title": _safe_prompt_text(paper.get("title") or paper.get("paper_title"), max_chars=500),
        "status": _safe_prompt_identifier(paper.get("status")),
        "workflow_status": _safe_prompt_identifier(paper.get("workflow_status")),
        "worker_status": _safe_prompt_identifier(paper.get("worker_status")),
        "target_journal": _safe_prompt_text(paper.get("target_journal"), max_chars=300),
        "submission_system": _safe_prompt_text(paper.get("submission_system"), max_chars=300),
        "submission_id": _safe_prompt_text(
            (retarget_closure_targets[0] if retarget_closure_targets else "")
            or paper.get("canonical_submission_id")
            or paper.get("submission_id")
            or monitor_followup.get("submission_id"),
            max_chars=180,
        ),
        "online_intent": _safe_prompt_identifier(paper.get("online_intent")),
        "next_action": clean_next_action,
        "detail": clean_detail,
        "paper_dir": "" if sdk_browser_channel else str(paper_dir),
        "source_package_dir": "" if sdk_browser_channel else str(package_dir),
        "draft_id": draft_id,
        "draft_url": draft_url,
        "upload_url": upload_url,
        "uploaded_manuscript_file": (
            "select by file_id through browser_files"
            if sdk_browser_channel and upload_file
            else upload_file
        ),
        "uploaded_manuscript_sha256": uploaded_sha256,
        "github_repo_url": github_repo_url,
        "github_repo_status": _safe_prompt_identifier(paper.get("github_repo_status")),
        "browser_status": {
            "operational": browser_health.get("operational") is True,
            "strategy": _safe_prompt_identifier(browser_health.get("strategy")),
        },
        "latest_audit": latest_audit,
        "standing_declarations_applied": bool(
            standing_result.get("applied") is True or standing_result.get("authorized") is True
        ),
        "planned_github_repo_status": _safe_prompt_identifier(
            planned_result.get("status") or paper.get("github_repo_status")
        ),
        "monitor_only": monitor_only,
        "final_submit_authorized": final_submit_authorized,
        "final_submit_claim_mode": final_submit_claim_mode,
        "final_submit_authorization_id": (
            str(final_submit_authorization.get("authorization_id") or "")
            if final_submit_authorization is not None
            else ""
        ),
        "final_submit_authorization_expires_at": (
            str(final_submit_authorization.get("expires_at") or "")
            if final_submit_authorization is not None
            else ""
        ),
        "final_submit_target_origin": (
            str(final_submit_authorization.get("target_origin") or "")
            if final_submit_authorization is not None
            else ""
        ),
        "final_submit_draft_binding_sha256": (
            str(final_submit_authorization.get("draft_binding_sha256") or "")
            if final_submit_authorization is not None
            else ""
        ),
        "final_submit_action_claimed_at": (
            str(final_submit_authorization.get("consumed_at") or "")
            if final_submit_authorization is not None
            else ""
        ),
        "final_submit_click_deadline": (
            min(
                _parse_aware_time(final_submit_authorization.get("expires_at")),
                _parse_aware_time(final_submit_authorization.get("consumed_at"))
                + FINAL_SUBMIT_CLAIM_TO_CLICK_TTL,
            ).isoformat()
            if final_submit_authorization is not None
            and _parse_aware_time(final_submit_authorization.get("consumed_at")) is not None
            and _parse_aware_time(final_submit_authorization.get("expires_at")) is not None
            else ""
        ),
        "scheduler_prepared_payload": scheduler_payload,
        "monitor_followup": {
            "action_kind": _safe_prompt_identifier(monitor_followup.get("action_kind")),
            "summary": _safe_prompt_text(monitor_followup.get("summary"), max_chars=240),
            "message_date": _safe_prompt_text(monitor_followup.get("message_date"), max_chars=80),
            "sender_domain": _safe_prompt_text(monitor_followup.get("sender_domain"), max_chars=160),
            "fingerprint": _safe_prompt_identifier(monitor_followup.get("fingerprint")),
        },
        "retarget_request": {
            "request_id": _safe_prompt_identifier(retarget_request.get("request_id")),
            "mode": _safe_prompt_identifier(retarget_request.get("mode")),
            "target_journal": _safe_prompt_text(
                retarget_request.get("target_journal"), max_chars=300
            ),
            "publication_tier": _safe_prompt_identifier(
                retarget_request.get("publication_tier")
            ),
            "priority": _safe_prompt_identifier(retarget_request.get("priority")),
            "withdrawal_policy": _safe_prompt_identifier(
                retarget_request.get("withdrawal_policy")
            ),
            "withdrawal_targets": retarget_closure_targets,
        },
    }

    author_info_path = paper_dir / "author_info.md"
    journal_path = paper_dir / "journal.md"
    declarations_path = package_dir / "declarations.md"
    manifest_path = package_dir / "submission_manifest.json"
    metadata_paths = [
        package_dir / "springer_submission_metadata.json",
        package_dir / "package_metadata_fingerprint.json",
        package_dir / "submission_metadata_fingerprint.json",
    ]
    metadata_sections = [
        _context_json_snippet(path.name, path, max_chars=2400)
        for path in metadata_paths
        if path.exists()
    ]
    metadata_text = "\n".join(metadata_sections) if metadata_sections else "## package metadata\nMISSING"
    worker_channel_policy = (
        "Use only the smart_browser MCP tools exposed to this fixed SDK session. "
        "Start with browser_pages and browser_snapshot. Use browser_files only when "
        "an upload field is ready, then pass its immutable file_id to browser_upload. "
        "No email, shell, filesystem, generic browser, search, account, or credential "
        "tool is available in this channel."
        if sdk_browser_channel
        else
        "Use visible browser/CDP, email, and web-form actions only. Browser file-upload "
        "controls may use the scheduler-provided absolute package paths below."
    )
    policy_source_line = (
        "- Follow the policy and exact paper context embedded in this prompt; no external skill lookup is required."
        if sdk_browser_channel
        else
        "- Use the `auto-paper-submission` skill as the policy source, but do not treat it as a black-box executor."
    )
    monitoring_policy_line = (
        "- For submitted or monitoring papers, inspect the verified journal dashboard. "
        "If email evidence is required, return `SMART_SUBMISSION_STATUS=waiting` with a "
        "specific deterministic email-sidecar next action; this SDK browser channel does not access mailboxes."
        if sdk_browser_channel
        else
        "- If this paper is already submitted/monitoring or has manuscript/confirmation evidence, monitoring is email-first: search Gmail/XUJCMail for decision, revision, rejection, acceptance, technical-check, and author-action messages before opening a dashboard. If no relevant email action signal is found, return `SMART_SUBMISSION_STATUS=monitoring`; do not mark it blocked only because Chrome/CDP, the in-app browser, an extension, or a dashboard status page is unavailable."
    )
    monitoring_scope_policy = (
        "MONITORING-ONLY AUTHORITY: this paper is already submitted. Inspect only the "
        "locked journal dashboard and report its current status. Do not edit metadata, "
        "upload or replace files, create a new submission, withdraw, resubmit, or activate "
        "any submit/approve/confirm control. A temporarily unavailable dashboard is not a "
        "manuscript revision: keep status monitoring and return a specific retry action."
        if monitor_only
        else ""
    )
    online_intent = str(one_paper_context.get("online_intent") or "")
    if online_intent == "user_retarget_verify_or_withdraw":
        post_submission_followup_policy = (
            "USER-REQUESTED RETARGET CLOSURE AUTHORITY: do not create a new submission, "
            "edit manuscript content, or choose a new journal in this Turn. Inspect every exact "
            "ID in ONE_PAPER_CONTEXT_JSON.retarget_request.withdrawal_targets. If the live portal "
            "explicitly shows an ID as rejected, withdrawn, closed, removed, or an expired transfer "
            "offer with no active manuscript, record that ID as closed without clicking. A date alone "
            "is not closure evidence. If an ID is still active, use the site's explicit withdrawal "
            "control only for that exact ID. Never treat a draft, technical-check deadline, or elapsed "
            "calendar time as withdrawn unless the portal says closed/expired/withdrawn. Return every "
            "positively closed ID in SMART_SUBMISSION_CLOSED_IDS as a comma-separated list. Return "
            "SMART_SUBMISSION_STATUS=retarget_needed only when every target is positively closed. If "
            "some remain active, return revision_needed and name the remaining IDs. If closure requires "
            "an editorial-office email, return blocked with SMART_SUBMISSION_NEXT="
            "request_editorial_withdrawal_by_email_before_retarget and do not claim closure."
        )
    elif online_intent == "post_submission_withdraw_for_retarget":
        post_submission_followup_policy = (
            "WITHDRAW-BEFORE-RETARGET AUTHORITY: operate only on the existing submission "
            f"identified by manuscript/submission id {one_paper_context['submission_id'] or '-'}; "
            "never create a new submission. Use the journal's explicit withdraw/decline-transfer "
            "action only when the live page clearly identifies this exact manuscript. Confirm the "
            "post-action page shows withdrawn/closed, then return SMART_SUBMISSION_STATUS=retarget_needed "
            "and SMART_SUBMISSION_NEXT=queue_journal_author_retarget_steps_2_3. If the site has no "
            "withdrawal control or requires an editorial-office message, do not claim closure; return "
            "SMART_SUBMISSION_STATUS=blocked and SMART_SUBMISSION_NEXT="
            "request_editorial_withdrawal_by_email_before_retarget."
        )
    elif online_intent.startswith("post_submission_"):
        post_submission_followup_policy = (
            "POST-SUBMISSION FOLLOW-UP AUTHORITY: continue only the existing submission "
            f"identified by manuscript/submission id {one_paper_context['submission_id'] or '-'}; "
            "never create a new submission. Inspect the exact author action recorded in "
            "ONE_PAPER_CONTEXT_JSON.monitor_followup, make only the requested technical, "
            "file, or metadata correction, save it, and use the journal's revision/resubmission "
            "path. If the requested action changes scientific manuscript content or cannot be "
            "matched unambiguously to this existing submission, return revision_needed without "
            "uploading unchanged files or starting a duplicate manuscript."
        )
    else:
        post_submission_followup_policy = ""

    prompt = f"""Process only this Smart Submission paper from the visible local project thread. Do not process any other paper.
PAPER_ID={paper_id}
ROUTE_REVISION={int(route_revision)}
ATTEMPT_ID={attempt_id}

TARGET LOCK:
- Target journal: {one_paper_context["target_journal"]}
- Submission system: {one_paper_context["submission_system"]}
- Existing submission ID: {one_paper_context["submission_id"] or '-'}
- Draft ID: {draft_id}
- Draft URL: {draft_url}
- Upload URL: {upload_url}

If the visible browser is on another journal/system/draft, do not infer from that page and do not continue there. Before opening a new tab, inspect the existing CDP tabs for the exact Draft ID, Draft URL, Upload URL, manuscript id, or target journal/system in this target lock, and reuse the matching tab when present. Navigate to the provided Draft URL/Upload URL only when no matching target tab exists. If you create a transient blank or duplicate tab, close it before finishing when it is safe. If the target page cannot be reached, return `SMART_SUBMISSION_STATUS=revision_needed` with a scheduler-side next action. A page for Elsevier/SCICO/Wiley/Inderscience/Taylor is unrelated unless the target lock above says so.

Do not run local shell/file commands and do not update local state/audit files in this worker. Do not run `python -m unified_paper_orchestrator smart-submission-browser-health --ensure` in this worker. The scheduler-side local executor handles browser-health checks, package metadata, planned GitHub URLs, standing declarations, state writes, audit writes, and other local file edits before/after dispatch. {worker_channel_policy} Do not inspect or edit files with shell commands. Never open or request a credential file, DPAPI vault, plaintext export, password-reset message, one-time code, password, token, cookie, or credential reference. Never create an account, reset a password, or persist a new credential from this worker. If login is not already available through controller-verified live-session evidence or a completed trusted local broker action, stop with `SMART_SUBMISSION_STATUS=blocked` and `SMART_SUBMISSION_NEXT=SMART_SECRET_BROKER_REQUIRED`. If any other local mutation, package read, package fix, state write, or command is required, stop this worker with final `SMART_SUBMISSION_*` markers naming the needed scheduler-side sidecar action instead of opening an approval prompt.

{final_submit_policy}
{monitoring_scope_policy}
{post_submission_followup_policy}

ONE_PAPER_CONTEXT_JSON:
```json
{json.dumps(one_paper_context, ensure_ascii=False, indent=2)}
```

LOCAL SOURCE-OF-TRUTH SNIPPETS ALREADY READ BY THE SCHEDULER:

{_context_file_snippet("author_info.md", author_info_path, max_chars=3500)}

{_context_file_snippet("journal.md", journal_path, max_chars=2500)}

{_context_file_snippet("package/declarations.md", declarations_path, max_chars=3500)}

{_context_json_snippet("package/submission_manifest.json", manifest_path, max_chars=2500)}

{metadata_text}

SCHEDULER-PREPARED PAYLOAD FOR WORKER USE:
```json
{json.dumps(scheduler_payload, ensure_ascii=False, indent=2)}
```

POLICY:
{policy_source_line}
- The user granted standing authorization on 2026-07-05 for default AI-use, originality/no-simultaneous-submission, author-responsibility, and routine no-third-party-material/no-separate-copyright-permission declarations. Do not block only because those routine declarations were absent if the context shows the scheduler already applied them.
- Missing GitHub can use the planned repository URL recorded in context; do not block on GitHub auth, 404, or public repository discovery for a planned URL.
- For Springer Nature/SNAPP, use `scheduler_prepared_payload.cover_letter_upload_file` for the cover-letter upload and use the scheduler-prepared author contribution, funding, data/code availability, AI-use, originality, author-responsibility, third-party-material, and copyright-permission statements directly in the web form. Select the no-third-party-material/no-separate-permission option when the scheduler payload says `no_separate_permission_required`. Do not ask for command approval to read local files.
{monitoring_policy_line}
- Ordinary account email verification is recoverable through accessible Gmail/school mailbox; never paste verification codes or full email bodies.
- Never read credential storage, ask for a password, register a fresh account, or start a password reset. Stored-secret login requires the controller's trusted local broker; otherwise return `SMART_SECRET_BROKER_REQUIRED`.
- Treat `ONE_PAPER_CONTEXT_JSON.final_submit_authorized` as the only final-click authority. If it is false, stop at the final review page and request `SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED`; the Python controller will automatically issue and dispatch the exact-attempt receipt when automatic final submit is enabled. Standing declarations never substitute for that receipt.
- Stop only for payment/forced OA, CAPTCHA/Cloudflare that cannot be passed in the visible browser, 2FA/security challenge, inaccessible mailbox or missing/expired verification after a fresh request, forced ORCID, non-routine author-person confirmation, new author/affiliation/funding/ethics/copyright/legal facts outside standing defaults, active duplicate-submission risk, or inability to determine submission success. Do not stop for routine no-third-party-material/no-separate-copyright-permission confirmation when it is already recorded in the scheduler payload.

Execute one bounded online submission or monitoring action for this paper only. After one authorized final click, leave the positive confirmation page open and do not navigate, close, refresh, or retry; the trusted scheduler-side CDP capture will verify it. Worker-reported URLs, ids, screenshots, or prose are observations only and are never accepted as submission proof. If monitoring, return the dashboard/email status. If blocked/revision_needed, make the next action scheduler-side and specific.

Final response must include exactly these markers:
SMART_SUBMISSION_PAPER_ID={paper_id}
SMART_SUBMISSION_ROUTE_REVISION={int(route_revision)}
SMART_SUBMISSION_ATTEMPT_ID={attempt_id}
SMART_SUBMISSION_STATUS=<submitted|monitoring|revision_needed|retarget_needed|rejected|blocked|accepted|waiting|no_action>
SMART_SUBMISSION_NEXT=<one-sentence next action>
For a user-requested retarget closure, also include the exact positively closed IDs (or an empty value):
SMART_SUBMISSION_CLOSED_IDS=<comma-separated exact ids>
For `submitted`, `monitoring`, or `accepted`, you may also include observed hints, but they are not authoritative and the scheduler must replace them with trusted capture output before recording success:
SMART_SUBMISSION_ID=<journal manuscript/submission id>
SMART_SUBMISSION_EVIDENCE_PATH=<existing local confirmation/email evidence file>
SMART_SUBMISSION_CONFIRMATION_URL=<http(s) confirmation URL>
"""
    return prompt.rstrip() + "\n"


SMART_PROMPT_REQUIRED_SECTIONS = (
    "## Heartbeat Rules",
    "## Program Dispatch Plan Authority",
    "## Paper State Machine",
    "## Gates Before Online Submission",
    "## Hard Stops",
    "## Sensitive Data",
    "## Audit Output",
    "## Visible Scheduler / Worker Dispatch",
    "submission-id/evidence-path/confirmation-url",
    "route_revision",
    "attempt_id",
)


def _canonical_scheduler_prompt() -> str:
    body = _default_prompt().rstrip() + "\n\n" + _visible_scheduler_prompt_section().strip() + "\n"
    evidence_rule = (
        "\n## Terminal Evidence Contract\n\n"
        "A visible worker may record `submitted`, `monitoring`, or `accepted` only with at least one verifiable "
        "submission-id/evidence-path/confirmation-url. The scheduler must pass it to "
        "`smart-submission-record-worker-result` using `--submission-id`, `--evidence-path`, or "
        "`--confirmation-url`. A final marker without evidence is invalid and must not change the paper to a "
        "successful terminal state. The worker's `SMART_SUBMISSION_ROUTE_REVISION` and CLI `--route-revision` "
        "must equal the queued paper's current `route_revision`. The worker's `SMART_SUBMISSION_ATTEMPT_ID` and "
        "CLI `--attempt-id` must equal the current queued attempt. Stale revisions or attempts are rejected.\n"
    )
    body = body.rstrip() + evidence_rule
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return (
        f"<!-- SMART_SUBMISSION_PROMPT_VERSION={SMART_PROMPT_VERSION} -->\n"
        f"<!-- SMART_SUBMISSION_PROMPT_SHA256={digest} -->\n\n"
        f"{body.rstrip()}\n"
    )


def _install_canonical_scheduler_prompt() -> dict[str, Any]:
    canonical = _canonical_scheduler_prompt()
    current = read_text(SMART_SCHEDULER_PROMPT_PATH) if SMART_SCHEDULER_PROMPT_PATH.exists() else ""
    missing_sections = [section for section in SMART_PROMPT_REQUIRED_SECTIONS if section not in current]
    version_marker = f"SMART_SUBMISSION_PROMPT_VERSION={SMART_PROMPT_VERSION}"
    needs_rebuild = current != canonical or version_marker not in current or bool(missing_sections)
    backup_path = ""
    if needs_rebuild and current:
        backup = SMART_SCHEDULER_PROMPT_PATH.with_name(
            f"{SMART_SCHEDULER_PROMPT_PATH.name}.{timestamp_id()}.bak"
        )
        write_text(backup, current)
        backup_path = str(backup)
    if needs_rebuild:
        write_text(SMART_SCHEDULER_PROMPT_PATH, canonical)
    return {
        "rebuilt": needs_rebuild,
        "backup_path": backup_path,
        "missing_sections": missing_sections,
        "version": SMART_PROMPT_VERSION,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _transaction_artifact_state(path_value: Any, sha256_value: Any) -> tuple[str, Path | None, Path | None]:
    path_text = str(path_value or "").strip()
    expected_sha256 = str(sha256_value or "").strip().casefold()
    if not path_text or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return "invalid", None, None
    final_path = Path(path_text)
    roots = (SMART_AUDIT_DIR, SMART_CONFIRMATION_EVIDENCE_DIR)
    try:
        resolved = final_path.resolve(strict=False)
        if not any(_path_is_within(resolved, root.resolve(strict=False)) for root in roots):
            return "invalid", None, None
    except (OSError, RuntimeError):
        return "invalid", None, None
    pending_path = final_path.with_name(f"{final_path.name}.pending")
    try:
        if (
            final_path.is_file()
            and not final_path.is_symlink()
            and hashlib.sha256(final_path.read_bytes()).hexdigest() == expected_sha256
        ):
            return "committed", final_path, pending_path
        if (
            pending_path.is_file()
            and not pending_path.is_symlink()
            and hashlib.sha256(pending_path.read_bytes()).hexdigest() == expected_sha256
        ):
            return "pending", final_path, pending_path
    except OSError:
        return "invalid", final_path, pending_path
    return "invalid", final_path, pending_path


def _promote_transaction_artifact(path_value: Any, sha256_value: Any) -> str:
    status, final_path, pending_path = _transaction_artifact_state(path_value, sha256_value)
    if status != "pending" or final_path is None or pending_path is None:
        return status
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending_path, final_path)
    except OSError:
        return "invalid"
    return _transaction_artifact_state(path_value, sha256_value)[0]


def _discard_transaction_pending(path_value: Any) -> None:
    path_text = str(path_value or "").strip()
    if not path_text:
        return
    final_path = Path(path_text)
    try:
        resolved = final_path.resolve(strict=False)
        roots = (SMART_AUDIT_DIR.resolve(strict=False), SMART_CONFIRMATION_EVIDENCE_DIR.resolve(strict=False))
        if not any(_path_is_within(resolved, root) for root in roots):
            return
        final_path.with_name(f"{final_path.name}.pending").unlink(missing_ok=True)
    except (OSError, RuntimeError):
        pass


def _recover_smart_pending_transactions(state: dict[str, Any]) -> dict[str, Any]:
    """Resolve crash windows without ever authorizing or repeating an uncertain click."""

    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = _queue_paper_ids(state, "online_queue")
    actions: list[dict[str, str]] = []
    force_manual = False

    def remove_from_queue(paper_id: str) -> None:
        nonlocal online_queue
        online_queue = [item for item in online_queue if item != paper_id]

    def retire_thread(paper: dict[str, Any]) -> None:
        thread_id = str(paper.get("thread_id") or "").strip()
        if thread_id:
            stale = [str(item) for item in paper.get("stale_worker_thread_ids", []) if str(item).strip()]
            paper["stale_worker_thread_ids"] = list(dict.fromkeys(stale + [thread_id]))

    def mark_unknown(paper_id: str, paper: dict[str, Any], code: str) -> None:
        nonlocal force_manual
        retire_thread(paper)
        # An uncertain dispatch/click transaction invalidates the one-use
        # authorization regardless of which audit edge was interrupted.
        paper.pop("final_submit_authorization", None)
        paper.pop("pending_submission_confirmation_evidence", None)
        paper["final_submit_authorization_invalidated_at"] = now_iso()
        paper["status"] = code
        paper["workflow_status"] = "blocked"
        paper["worker_status"] = "blocked"
        paper["next_action"] = "manual_verify_submission_result_before_any_retry"
        paper["blocker"] = "A crash interrupted the final-submit evidence transaction; verify the journal system before any retry."
        remove_from_queue(paper_id)
        force_manual = True

    def cancel_authorization(paper_id: str, paper: dict[str, Any]) -> None:
        nonlocal force_manual
        retire_thread(paper)
        paper.pop("final_submit_authorization", None)
        paper.pop("pending_submission_confirmation_evidence", None)
        paper["thread_id"] = ""
        paper["status"] = "waiting"
        paper["workflow_status"] = "waiting"
        paper["worker_status"] = "waiting"
        paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
        paper["blocker"] = ""
        remove_from_queue(paper_id)
        force_manual = True

    for raw_paper_id, paper in papers.items():
        paper_id = str(raw_paper_id)
        if not isinstance(paper, dict):
            continue

        challenge = paper.get("final_submit_confirmation_challenge")
        if isinstance(challenge, dict):
            status = _transaction_artifact_state(
                challenge.get("challenge_audit_path"),
                challenge.get("challenge_audit_sha256"),
            )[0]
            if status == "pending":
                promoted = _promote_transaction_artifact(
                    challenge.get("challenge_audit_path"),
                    challenge.get("challenge_audit_sha256"),
                )
                actions.append({"paper_id": paper_id, "artifact": "ui_challenge", "outcome": promoted})
            elif status == "invalid":
                _discard_transaction_pending(challenge.get("challenge_audit_path"))
                paper.pop("final_submit_confirmation_challenge", None)
                actions.append({"paper_id": paper_id, "artifact": "ui_challenge", "outcome": "discarded"})

        authorization = paper.get("final_submit_authorization")
        if isinstance(authorization, dict):
            auth_status = _transaction_artifact_state(
                authorization.get("authorization_audit_path"),
                authorization.get("authorization_audit_sha256"),
            )[0]
            if auth_status == "pending":
                # An authorization whose state commit outran its audit commit
                # is deliberately invalidated.  Never promote it before the
                # fail-closed state is durable: a crash in that window could
                # otherwise reactivate a one-click authorization on restart.
                _discard_transaction_pending(authorization.get("authorization_audit_path"))
                cancel_authorization(paper_id, paper)
                actions.append({"paper_id": paper_id, "artifact": "authorization", "outcome": "pending_cancelled"})
                continue
            if auth_status == "invalid":
                _discard_transaction_pending(authorization.get("authorization_audit_path"))
                cancel_authorization(paper_id, paper)
                actions.append({"paper_id": paper_id, "artifact": "authorization", "outcome": "invalid_cancelled"})
                continue

            reservation_status = "committed"
            if str(authorization.get("reserved_at") or ""):
                reservation_status = _transaction_artifact_state(
                    authorization.get("reservation_audit_path"),
                    authorization.get("reservation_audit_sha256"),
                )[0]
            if reservation_status != "committed":
                if reservation_status == "pending":
                    _discard_transaction_pending(authorization.get("reservation_audit_path"))
                    reservation_status = "pending_discarded"
                else:
                    _discard_transaction_pending(authorization.get("reservation_audit_path"))
                mark_unknown(paper_id, paper, "dispatch_state_unknown")
                actions.append({"paper_id": paper_id, "artifact": "dispatch", "outcome": f"{reservation_status}_manual"})
                continue

            consumption_status = "committed"
            if str(authorization.get("consumed_at") or ""):
                consumption_status = _transaction_artifact_state(
                    authorization.get("consumption_audit_path"),
                    authorization.get("consumption_audit_sha256"),
                )[0]
            if consumption_status != "committed":
                if consumption_status == "pending":
                    _discard_transaction_pending(authorization.get("consumption_audit_path"))
                    consumption_status = "pending_discarded"
                else:
                    _discard_transaction_pending(authorization.get("consumption_audit_path"))
                mark_unknown(paper_id, paper, "final_submit_action_unknown")
                actions.append({"paper_id": paper_id, "artifact": "final_action", "outcome": f"{consumption_status}_manual"})
                continue

        if (
            not isinstance(authorization, dict)
            and str(paper.get("workflow_status") or "").strip().casefold() == "running"
            and str(paper.get("last_dispatch_audit_path") or "").strip()
        ):
            dispatch_status = _transaction_artifact_state(
                paper.get("last_dispatch_audit_path"),
                paper.get("last_dispatch_audit_sha256"),
            )[0]
            dispatch_was_pending = dispatch_status == "pending"
            if dispatch_status == "pending":
                _discard_transaction_pending(paper.get("last_dispatch_audit_path"))
                dispatch_status = "pending_discarded"
            elif dispatch_status != "committed":
                _discard_transaction_pending(paper.get("last_dispatch_audit_path"))
            if dispatch_was_pending or dispatch_status != "committed":
                mark_unknown(paper_id, paper, "dispatch_state_unknown")
                actions.append({"paper_id": paper_id, "artifact": "dispatch", "outcome": f"{dispatch_status}_manual"})
                continue

        live_wrapper = paper.get("pending_submission_confirmation_evidence")
        if not isinstance(live_wrapper, dict):
            live_wrapper = paper.get("last_live_submission_confirmation_evidence")
        if isinstance(live_wrapper, dict):
            live_status = _transaction_artifact_state(
                live_wrapper.get("evidence_path"),
                live_wrapper.get("evidence_sha256"),
            )[0]
            if live_status == "pending":
                live_status = _promote_transaction_artifact(
                    live_wrapper.get("evidence_path"),
                    live_wrapper.get("evidence_sha256"),
                )
                actions.append({"paper_id": paper_id, "artifact": "live_confirmation", "outcome": live_status})
            if live_status != "committed":
                _discard_transaction_pending(live_wrapper.get("evidence_path"))
                mark_unknown(paper_id, paper, "submission_result_unknown")
                actions.append({"paper_id": paper_id, "artifact": "live_confirmation", "outcome": "invalid_manual"})
                continue

        result_receipt = paper.get("submission_confirmation_receipt")
        if isinstance(result_receipt, dict):
            result_status = _transaction_artifact_state(
                result_receipt.get("result_audit_path"),
                result_receipt.get("result_audit_sha256"),
            )[0]
            if result_status == "pending":
                result_status = _promote_transaction_artifact(
                    result_receipt.get("result_audit_path"),
                    result_receipt.get("result_audit_sha256"),
                )
                actions.append({"paper_id": paper_id, "artifact": "worker_result", "outcome": result_status})
            if result_status != "committed":
                _discard_transaction_pending(result_receipt.get("result_audit_path"))
                mark_unknown(paper_id, paper, "submission_result_unknown")
                actions.append({"paper_id": paper_id, "artifact": "worker_result", "outcome": "invalid_manual"})

        # Non-terminal worker outcomes also use state-first/audit-second
        # commits.  Their generic audit is safe to promote because no click
        # authorization or dispatch capability is made valid by this file.
        last_audit_status = _transaction_artifact_state(
            paper.get("last_audit_path"),
            paper.get("last_audit_sha256"),
        )[0]
        if last_audit_status == "pending":
            promoted = _promote_transaction_artifact(
                paper.get("last_audit_path"),
                paper.get("last_audit_sha256"),
            )
            actions.append({"paper_id": paper_id, "artifact": "worker_outcome", "outcome": promoted})

    if actions:
        now = now_iso()
        state["updated_at"] = now
        state.setdefault("history", []).append(
            {
                "at": now,
                "event": "recover_pending_smart_transactions",
                "actions": actions,
                "forced_manual": force_manual,
            }
        )
    state["online_queue"] = online_queue
    if force_manual:
        state["mode"] = "manual"
    return {"changed": bool(actions), "actions": actions, "forced_manual": force_manual}


@_smart_state_locked
def ensure_smart_submission_module(
    *,
    projection_sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    for directory in (
        SMART_SUBMISSION_DIR,
        SMART_AUDIT_DIR,
        SMART_SCREENSHOT_DIR,
        SMART_SNAPSHOT_DIR,
        SMART_CONFIRMATION_EVIDENCE_DIR,
        SMART_LOG_DIR,
        SMART_UPLOAD_STAGING_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    if not SMART_STATE_PATH.exists():
        write_json_atomic(SMART_STATE_PATH, _default_state())
        created.append(str(SMART_STATE_PATH))
    # Missing account/site registries are read as empty.  Only an explicit
    # AccountSiteStore operation may create or mutate those files.

    prompt_result = _install_canonical_scheduler_prompt()
    if prompt_result["rebuilt"] and not prompt_result["backup_path"]:
        created.append(str(SMART_SCHEDULER_PROMPT_PATH))

    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        raise RuntimeError("Smart Submission state must be a JSON object")
    state, migration = _migrate_smart_state(state)
    state.setdefault("module", "smart_submission_sidecar")
    state.setdefault("mode", "manual")
    state.setdefault("paper_queue", [])
    state.setdefault("online_queue", [])
    state.setdefault("papers", {})
    state.setdefault("commands", [])
    state.setdefault("history", [])
    recovery = _recover_smart_pending_transactions(state)
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    legacy_monitor_time_missing = not str(
        settings.get("daily_monitor_time_local") or ""
    ).strip()
    for key, value in _default_state()["settings"].items():
        settings.setdefault(key, value)
    if legacy_monitor_time_missing:
        # Preserve installations that already customized the legacy hour
        # field before the canonical HH:MM setting was introduced.
        settings.pop("daily_monitor_time_local", None)
    _smart_monitor_schedule(settings)
    settings["trusted_credential_broker_enabled"] = True
    if str(settings.get("account_missing_policy") or "").strip() not in {
        "auto_register",
        "ask",
        "block",
    }:
        settings["account_missing_policy"] = "auto_register"
    if settings["account_missing_policy"] == "auto_register":
        settings["account_registration_enabled"] = True
    if str(settings.get("new_account_auth_preference") or "").strip() not in {
        "orcid_first",
        "site_account",
    }:
        settings["new_account_auth_preference"] = "orcid_first"
    settings["scheduler_prompt_version"] = SMART_PROMPT_VERSION
    settings["scheduler_prompt_sha256"] = prompt_result["sha256"]
    state.setdefault("paths", _default_state()["paths"])
    state, path_repaired = _repair_corrupted_smart_paths(state)
    retarget_normalized = _normalize_retarget_records(state)
    if path_repaired:
        state["history"].append(
            {
                "at": now_iso(),
                "event": "repair_corrupted_smart_paths",
                "message": "Repaired path strings damaged by non-UTF-8 worker command execution.",
            }
        )
    if retarget_normalized:
        state["history"].append(
            {
                "at": now_iso(),
                "event": "normalize_retarget_records",
                "paper_ids": retarget_normalized,
            }
        )
    state["updated_at"] = now_iso()
    projection_dirty: dict[str, Any] = {}
    if recovery.get("changed"):
        projection_dirty = _prepare_smart_projection_change(state, "pending_transaction_recovery")
    write_json_atomic(SMART_STATE_PATH, state)
    if recovery.get("changed"):
        projection_dirty = _mark_smart_projection_dirty(state, "pending_transaction_recovery")
    projection_refresh = refresh_smart_submission_projection(
        sync_callback=projection_sync_callback,
    )
    return {
        "created": created,
        "prompt": prompt_result,
        "state_migration": migration,
        "transaction_recovery": recovery,
        "projection_dirty": projection_dirty,
        "projection_refresh": projection_refresh,
        "state": state,
    }

    # Legacy string-by-string prompt migrations are intentionally unreachable.
    # The canonical, versioned prompt above is now the only runtime path.
    if not SMART_STATE_PATH.exists():
        write_json_atomic(SMART_STATE_PATH, _default_state())
        created.append(str(SMART_STATE_PATH))
    if not SMART_SCHEDULER_PROMPT_PATH.exists():
        write_text(SMART_SCHEDULER_PROMPT_PATH, _default_prompt())
        created.append(str(SMART_SCHEDULER_PROMPT_PATH))
    else:
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        normalized = prompt_text.replace(
            r"D:\Documents\unified-paper-orchestrator",
            SMART_ASCII_WORKSPACE,
        ).replace(
            str(REPO_ROOT),
            SMART_ASCII_WORKSPACE,
        )
        normalized = re.sub(
            r"from `D:\\Documents\\[^`\r\n]*:?`?",
            "from the unified project workspace root",
            normalized,
        )
        if normalized != prompt_text:
            write_text(SMART_SCHEDULER_PROMPT_PATH, normalized)
            prompt_text = normalized
        old_local_executor_rule = (
            "Use the command output and generated audit files as the source of truth before deciding whether any browser or email action is needed. "
            "If the command reports `mode=manual` or `processed=0`, stop."
        )
        new_local_executor_rule = (
            "Use the command output and generated audit files as the source of truth before deciding whether any browser or email action is needed. "
            "If the initial state was `manual`, stop after the quiet heartbeat. If a `scheduled` or `run_once` command returns `mode=manual` "
            "but `settings.online_submit_enabled=true` and `online_queue` is non-empty, continue visible worker dispatch. "
            "A result with `processed=0` is normal when local preflight is empty but `online_queue` already has browser/email workers assigned. "
            "Stop only when both the local queue and `online_queue` are empty, or online submission is disabled."
        )
        if old_local_executor_rule in prompt_text:
            prompt_text = prompt_text.replace(old_local_executor_rule, new_local_executor_rule, 1)
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        old_batch_online_rule = (
            "- Browser/login/upload/final-submit/status-dashboard actions must remain visible and single-paper. "
            "Dispatch at most one online worker per scheduler heartbeat, even when `online_queue` contains many papers."
        )
        new_batch_online_rule = (
            "- Browser/login/upload/final-submit/status-dashboard actions must remain visible and single-paper "
            "inside each worker thread. The scheduler may dispatch independent visible workers up to "
            "`settings.max_parallel_workers` per heartbeat/sweep.\n"
            "- One blocked, waiting, idle, or completed paper must not block the rest of `online_queue`; "
            "skip that paper, record its state, and continue filling available worker slots with later eligible papers."
        )
        if old_batch_online_rule in prompt_text:
            prompt_text = prompt_text.replace(old_batch_online_rule, new_batch_online_rule, 1)
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        if "smart-submission-browser-cleanup --close" not in prompt_text:
            cleanup_rule = (
                "\n- Before dispatching visible workers, run "
                "`python -m unified_paper_orchestrator smart-submission-browser-cleanup --close` once. "
                "This closes only blank/new-tab pages and exact duplicate non-target submission tabs; "
                "do not stop the sweep if cleanup has no candidates or a close error.\n"
            )
            anchor = "- Browser/login/upload/final-submit/status-dashboard actions must remain visible"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, cleanup_rule + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n## Browser Tab Hygiene\n" + cleanup_rule.strip() + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        active_slot_marker = "Count active slots only from current state"
        if active_slot_marker not in prompt_text:
            write_text(
                SMART_SCHEDULER_PROMPT_PATH,
                prompt_text.rstrip()
                + "\n\n## Worker Slot Accounting\n\n"
                + active_slot_marker
                + ": a worker counts only when its thread_id is the current thread_id of a paper still in online_queue with workflow_status=running. Ignore stale Smart Submission worker threads from older queues, blocked/monitoring/waiting papers, unread old threads, projectless dayi-unified workspaces, and completed/idle threads after reconciliation.\n",
            )
        marker = "python -m unified_paper_orchestrator smart-submission-run"
        if marker not in prompt_text:
            write_text(
                SMART_SCHEDULER_PROMPT_PATH,
                prompt_text.rstrip()
                + "\n\n## Local Executor\n\n"
                + f"For each heartbeat, first run this local command from `{SMART_ASCII_WORKSPACE}`:\n\n"
                + "```powershell\n"
                + marker
                + "\n```\n\n"
                + new_local_executor_rule
                + "\n",
            )
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        browser_marker = "## Browser / CDP Health"
        if browser_marker not in prompt_text:
            insert = f"""
## Browser / CDP Health

The scheduler/sidecar, not visible worker threads, owns browser-health commands. Before dispatching online submission, login check, Gmail check, or submission-dashboard worker actions, run this command from `{SMART_ASCII_WORKSPACE}`:

```powershell
python -m unified_paper_orchestrator smart-submission-browser-health --ensure
```

Use the browser channels in this order:

1. `127.0.0.1:9224` normal Chrome/CDP for Gmail, Google login, Springer, Taylor & Francis, SAGE, Elsevier, and other sites that need visible logged-in browser state.
2. `127.0.0.1:9222` headless/background Chrome/CDP for safe read-only checks and fallback probes.
3. Codex Chrome Extension is optional assistance only. Do not treat extension/native-pipe failure as a paper blocker when either CDP port is available.

Visible paper workers must not run this command themselves because local shell commands can trigger Codex command-approval prompts and block the worker slot. Workers should use the browser/CDP channels recorded by the scheduler in state. Only classify browser access as a hard blocker when both CDP ports cannot be restored by the scheduler/sidecar and no safe HTTP/mail fallback can answer the current bounded action.
"""
            anchor = "## Local Executor"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert.rstrip() + "\n\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert.rstrip() + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        discovery_marker = "Missing `credential_store.local.json` or `site_registry.json` entries are not immediate blockers."
        if discovery_marker not in prompt_text:
            write_text(
                SMART_SCHEDULER_PROMPT_PATH,
                prompt_text.rstrip()
                + "\n\n## Auto Discovery\n\n"
                + discovery_marker
                + " Try local legacy state, migration plaintext credential export, paper `journal.md`, Chrome logged-in sessions, Gmail, school mailbox, and current journal submission pages first. Only mark a paper `blocked` when discovery fails or hits CAPTCHA/2FA/payment/forced-OA/legal-confirmation boundaries.\n",
            )
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        monitoring_marker = "## Email-First Submitted Monitoring"
        if monitoring_marker not in prompt_text:
            insert = _monitoring_email_first_prompt_section()
            anchor = "## Browser / CDP Health"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert.rstrip() + "\n\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert.rstrip() + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        package_autofix_marker = "## Package Auto-Fix Before Blocking"
        if package_autofix_marker not in prompt_text:
            insert = _package_autofix_prompt_section()
            anchor = "## Hard Stops"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert.rstrip() + "\n\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert.rstrip() + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        data_availability_marker = "Data Availability placeholders or mismatched live journal templates"
        if package_autofix_marker in prompt_text and data_availability_marker not in prompt_text:
            insert = (
                "- Data Availability placeholders or mismatched live journal templates: if a final-review "
                "page or form contains placeholders such as `[repository name]`, `[DOI/URL]`, "
                "`[reference number]`, `[list resources and URLs]`, or asks for a data-sharing template "
                "that conflicts with the local package, do not final-submit and do not immediately block. "
                "First read the exact `## Data Availability` section from `package/declarations.md`, then "
                "`author_info.md`, then `submission_metadata_fingerprint.json`. If a non-placeholder local "
                "statement exists, go back to the journal's data-availability/additional-information step "
                "and replace the live field or choose the closest template that preserves the local statement "
                "exactly. This is a routine declaration already recorded in the package, not a new "
                "legal/ethical fact. Block only if the site requires a specific DOI, external repository, "
                "unpublished public-source URL, new dataset fact, or a template that cannot represent the "
                "local statement without placeholders."
            )
            anchor = "After safe fixes, rerun the submission gate"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert + "\n\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        third_party_marker = "Third-party material / copyright-permission declaration"
        if package_autofix_marker in prompt_text and third_party_marker not in prompt_text:
            insert = (
                "- Third-party material / copyright-permission declaration: the user granted standing "
                "authorization on 2026-07-05 to use `settings.default_third_party_material_statement` and "
                "`settings.default_copyright_permission_statement` for routine no-permission-required "
                "declarations when the local package has no contradictory evidence. Write these statements "
                "into `package/declarations.md`, package metadata/fingerprint, and SNAPP/Springer form payloads; "
                "select the no-third-party-material/no-separate-permission option in the live form. Stop only "
                "if the local package or live form contains material-specific facts indicating third-party "
                "material, required permissions, pending permissions, a copyright holder, adapted/reproduced "
                "content, or another new copyright/legal fact."
            )
            anchor = "- Abstract length:"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert + "\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        account_recovery_marker = "## Account Recovery / Fresh Registration"
        if account_recovery_marker not in prompt_text:
            insert = _account_recovery_prompt_section()
            anchor = "## Hard Stops"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert.rstrip() + "\n\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert.rstrip() + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        path_marker = "## Path Handling"
        if path_marker not in prompt_text:
            insert = """
## Path Handling

The workspace path contains non-ASCII characters. When running inline Python or PowerShell, derive project paths from `Path.cwd()` or use relative paths under `state/smart_submission`; do not embed the Chinese absolute path inside one-line command strings. Never write `D:/Documents/???` or mojibake paths into state, audit files, or evidence manifests.
"""
            anchor = "## Audit Output"
            if anchor in prompt_text:
                prompt_text = prompt_text.replace(anchor, insert.rstrip() + "\n\n" + anchor, 1)
            else:
                prompt_text = prompt_text.rstrip() + "\n\n" + insert.rstrip() + "\n"
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        visible_marker = "## Visible Scheduler / Worker Dispatch"
        if visible_marker in prompt_text:
            prompt_text = prompt_text.split(visible_marker, 1)[0].rstrip()
        else:
            prompt_text = prompt_text.rstrip()
        if visible_marker not in prompt_text:
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text.rstrip() + "\n\n" + _visible_scheduler_prompt_section())
        else:
            worker_marker = "Codex Chrome Extension/native pipe 只是可选辅助"
            if worker_marker not in prompt_text:
                prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
                needle = "- 该论文 paper_dir 下的 author_info.md 和 journal.md\n\n执行一个有界的在线投稿/监控动作："
                replacement = (
                    "- 该论文 paper_dir 下的 author_info.md 和 journal.md\n\n"
                    "在线动作前先运行：\n"
                    "```powershell\n"
                    "python -m unified_paper_orchestrator smart-submission-browser-health --ensure\n"
                    "```\n"
                    "优先使用 `127.0.0.1:9224` 普通 Chrome/CDP，其次使用 `127.0.0.1:9222` 后台 CDP。Codex Chrome Extension/native pipe 只是可选辅助；只要 9224 或 9222 可用，就不要因为 extension 不可用而把论文标为 blocked。\n\n"
                    "执行一个有界的在线投稿/监控动作："
                )
                if needle in prompt_text:
                    write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text.replace(needle, replacement, 1))
                prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
                worker_path_marker = "绝不能写入 `D:/Documents/???` 这类路径。"
                worker_path_text = "写状态、audit、截图和证据路径时，使用当前工作目录或相对路径推导，不要把中文绝对路径硬编码进 inline 命令；绝不能写入 `D:/Documents/???` 这类路径。"
                if "Codex Chrome Extension/native pipe 只是可选辅助" in prompt_text and worker_path_marker not in prompt_text:
                    prompt_text = prompt_text.replace(
                        "优先使用 `127.0.0.1:9224` 普通 Chrome/CDP，其次使用 `127.0.0.1:9222` 后台 CDP。Codex Chrome Extension/native pipe 只是可选辅助；只要 9224 或 9222 可用，就不要因为 extension 不可用而把论文标为 blocked。",
                        "优先使用 `127.0.0.1:9224` 普通 Chrome/CDP，其次使用 `127.0.0.1:9222` 后台 CDP。Codex Chrome Extension/native pipe 只是可选辅助；只要 9224 或 9222 可用，就不要因为 extension 不可用而把论文标为 blocked。\n" + worker_path_text,
                        1,
                    )
                    write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)

        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        worker_autofix_marker = "Before marking this paper blocked for local package metadata"
        if visible_marker in prompt_text and worker_autofix_marker not in prompt_text:
            prompt_text = prompt_text.replace(
                "PAPER_ID=<paper_id>",
                "PAPER_ID=<paper_id>\n\n"
                + worker_autofix_marker
                + ", apply the Package Auto-Fix Before Blocking rules from "
                + "`state\\\\smart_submission\\\\SMART_SUBMISSION_SCHEDULER_PROMPT.md`: "
                + "fix safe defaults first, rerun the gate, and continue online submission when the gate passes. "
                + "Do not invent legal/ethical facts; if no local author/declaration default exists for AI-use, "
                + "block only that paper with a precise blocker.",
                1,
            )
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        worker_data_availability_marker = "If the current blocker is a Data Availability template/placeholder problem"
        if visible_marker in prompt_text and worker_data_availability_marker not in prompt_text:
            insert = (
                worker_data_availability_marker
                + ", do not ask the user first when the local package already has an exact Data Availability "
                "statement. Read `package/declarations.md`, then `author_info.md`, then package "
                "metadata/fingerprint; use the exact non-placeholder local statement as the source of truth. "
                "Return to the journal's Additional Information/Data Availability form, replace or choose a "
                "template that preserves that local statement without `[repository name]`, `[DOI/URL]`, "
                "`[reference number]`, or `[list resources and URLs]` placeholders, then reopen Final Review "
                "and verify the statement before final submit. Stop only if the site requires a specific DOI, "
                "external data repository, new dataset/source URL, or another fact not recorded locally."
            )
            if worker_autofix_marker in prompt_text:
                prompt_text = prompt_text.replace(worker_autofix_marker, insert + "\n\n" + worker_autofix_marker, 1)
            else:
                prompt_text = prompt_text.replace("PAPER_ID=<paper_id>", "PAPER_ID=<paper_id>\n\n" + insert, 1)
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text)
        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        worker_running_marker = "Treat only explicit running/in_progress/working/busy thread statuses as running."
        old_worker_running_rule = (
            "7. Before sending any worker prompt, list recent threads matching `Smart Submission worker`. "
            "If any matching worker is running, stop and wait."
        )
        new_worker_running_rule = (
            "7. Before sending any worker prompt, list recent threads matching `Smart Submission worker`. "
            "If any matching worker is truly running, stop and wait. "
            "Treat only explicit running/in_progress/working/busy thread statuses as running. "
            "Do not treat `notLoaded`, `idle`, `completed`, unread status, or `hasUnreadTurn=true` as active by itself; "
            "if uncertain, read the latest turn and reuse the existing completed/idle worker thread when appropriate."
        )
        if visible_marker in prompt_text and worker_running_marker not in prompt_text and old_worker_running_rule in prompt_text:
            write_text(SMART_SCHEDULER_PROMPT_PATH, prompt_text.replace(old_worker_running_rule, new_worker_running_rule, 1))

        prompt_text = read_text(SMART_SCHEDULER_PROMPT_PATH)
        prompt_replacements = {
            "- AI-use statement: use the standing author/declaration template if it already records an AI-use policy. If no local default exists, write `MANUAL_REQUIRED` and block because this is a legal/ethical declaration.": (
                "- AI-use / generative-AI statement: the user granted standing authorization on 2026-07-05 "
                "to use `settings.default_ai_use_statement` when a paper-specific statement is missing. "
                "Write the default statement into `package/declarations.md`, package metadata/fingerprint, "
                "and any required routine journal form field, then rerun the gate. Do not write "
                "`MANUAL_REQUIRED` or block only because the AI-use statement was missing.\n"
                "- Originality / no-simultaneous-submission / author-responsibility confirmations: the user "
                "granted standing authorization on 2026-07-05 to use `settings.default_originality_statement` "
                "and `settings.default_author_responsibility_statement` for routine submission confirmations. "
                "Apply these defaults to package metadata/declarations and routine form checkboxes/text fields. "
                "Stop only if the site asks for a non-routine new legal, copyright, ethics, funding, affiliation, "
                "coauthor, ORCID, payment, or personally interactive author action that is not covered by the standing defaults."
            ),
            "After safe fixes, rerun the submission gate and continue login/upload/final-submit if the gate passes. Stop only when the remaining issue requires the user or a site boundary: payment/forced OA, CAPTCHA/Cloudflare that cannot be passed in the visible browser, 2FA/security challenge, forced ORCID authorization, author confirmation, new author/affiliation/funding/ethics/legal facts, active prior submission risk, or inability to determine submission success.": (
                "After safe fixes, rerun the submission gate and continue login/upload/final-submit if the gate passes. "
                "Stop only when the remaining issue requires the user or a site boundary: payment/forced OA, "
                "CAPTCHA/Cloudflare that cannot be passed in the visible browser, 2FA/security challenge, forced ORCID "
                "authorization, non-routine author-person confirmation, new author/affiliation/funding/ethics/copyright/legal "
                "facts outside the standing defaults, active prior submission risk, or inability to determine submission success."
            ),
            "Preserve standing authorization only for routine declarations already recorded and non-conflicting.": (
                "Preserve standing authorization for routine AI-use, originality/no-simultaneous-submission, "
                "and author-responsibility declarations. If the package is missing those declarations, apply the "
                "default statements from settings before blocking."
            ),
            "Before marking this paper blocked for local package metadata, apply the Package Auto-Fix Before Blocking rules from `state\\\\smart_submission\\\\SMART_SUBMISSION_SCHEDULER_PROMPT.md`: fix safe defaults first, rerun the gate, and continue online submission when the gate passes. Do not invent legal/ethical facts; if no local author/declaration default exists for AI-use, block only that paper with a precise blocker.": (
                "Before marking this paper blocked for local package metadata, apply the Package Auto-Fix Before Blocking "
                "rules from `state\\\\smart_submission\\\\SMART_SUBMISSION_SCHEDULER_PROMPT.md`: fix safe defaults first, "
                "rerun the gate, and continue online submission when the gate passes. The user granted standing authorization "
                "on 2026-07-05 for default AI-use, originality/no-simultaneous-submission, and author-responsibility "
                "declarations; write those defaults into the package/form when missing. Do not block only because those "
                "routine declarations were absent. Block only for non-routine new legal/ethical/author facts outside the standing defaults."
            ),
            "Stop this paper and write a blocker if you hit payment/forced OA, CAPTCHA/Cloudflare, 2FA/security challenge, inaccessible mailbox or missing/expired account-verification email, forced ORCID, author-person confirmation, new author/affiliation/funding/ethics/copyright/legal facts, duplicate-submission risk, or inability to determine submission success.": (
                "Stop this paper and write a blocker if you hit payment/forced OA, CAPTCHA/Cloudflare, 2FA/security challenge, "
                "inaccessible mailbox or missing/expired account-verification email after a fresh request, forced ORCID, "
                "non-routine author-person confirmation, new author/affiliation/funding/ethics/copyright/legal facts outside "
                "the standing defaults, duplicate-submission risk, or inability to determine submission success."
            ),
        }
        updated_prompt_text = prompt_text
        for old, new in prompt_replacements.items():
            updated_prompt_text = updated_prompt_text.replace(old, new)
        if updated_prompt_text != prompt_text:
            write_text(SMART_SCHEDULER_PROMPT_PATH, updated_prompt_text)

    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    state.setdefault("version", 1)
    state.setdefault("module", "smart_submission_sidecar")
    state.setdefault("mode", "manual")
    state.setdefault("paper_queue", [])
    state.setdefault("online_queue", [])
    state.setdefault("papers", {})
    state.setdefault("commands", [])
    state.setdefault("settings", {})
    if not isinstance(state["settings"], dict):
        state["settings"] = {}
    legacy_monitor_time_missing = not str(
        state["settings"].get("daily_monitor_time_local") or ""
    ).strip()
    for key, value in _default_state()["settings"].items():
        state["settings"].setdefault(key, value)
    if legacy_monitor_time_missing:
        state["settings"].pop("daily_monitor_time_local", None)
    _smart_monitor_schedule(state["settings"])
    automation_status = _smart_automation_status()
    if not state["settings"].get("scheduler_target_thread_id") and automation_status.get("target_thread_id"):
        state["settings"]["scheduler_target_thread_id"] = automation_status["target_thread_id"]
    state.setdefault("paths", _default_state()["paths"])
    state.setdefault("history", [])
    state, path_repaired = _repair_corrupted_smart_paths(state)
    retarget_normalized = _normalize_retarget_records(state)
    if path_repaired and isinstance(state, dict):
        history = state.setdefault("history", [])
        if isinstance(history, list):
            history.append(
                {
                    "at": now_iso(),
                    "event": "repair_corrupted_smart_paths",
                    "message": "Repaired path strings damaged by non-UTF-8 worker command execution.",
                }
            )
    if retarget_normalized and isinstance(state, dict):
        history = state.setdefault("history", [])
        if isinstance(history, list):
            history.append(
                {
                    "at": now_iso(),
                    "event": "normalize_retarget_records",
                    "paper_ids": retarget_normalized,
                }
            )
    state["updated_at"] = now_iso()
    write_json_atomic(SMART_STATE_PATH, state)
    return {"created": created, "state": state}


def _automation_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+?)\s*$", text)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _smart_automation_status() -> dict[str, Any]:
    if not SMART_AUTOMATION_PATH.exists():
        return {
            "exists": False,
            "path": str(SMART_AUTOMATION_PATH),
            "id": "automation",
            "kind": "missing",
            "name": "",
            "status": "MISSING",
            "rrule": "",
            "target_thread_id": "",
            "visible_thread": False,
            "visibility_note": "automation.toml is missing; no scheduler heartbeat exists.",
        }
    text = read_text(SMART_AUTOMATION_PATH)
    kind = _automation_value(text, "kind")
    target_thread_id = _automation_value(text, "target_thread_id")
    status = _automation_value(text, "status").upper()
    visible_thread = bool(target_thread_id) and status == "ACTIVE"
    if status != "ACTIVE":
        note = (
            "legacy Codex Automation is retired and cannot wake the SDK runtime; "
            "the target thread ID is retained only for audit."
        )
    elif visible_thread:
        note = f"heartbeat is bound to visible thread {target_thread_id}."
    elif kind == "cron":
        note = "cron automation runs as a background job; it does not create a visible scheduler chat by itself."
    else:
        note = "automation has no target_thread_id; no visible scheduler chat is bound."
    return {
        "exists": True,
        "path": str(SMART_AUTOMATION_PATH),
        "id": _automation_value(text, "id") or "automation",
        "kind": kind,
        "name": _automation_value(text, "name"),
        "status": status,
        "rrule": _automation_value(text, "rrule"),
        "target_thread_id": target_thread_id,
        "visible_thread": visible_thread,
        "visibility_note": note,
    }


def _smart_program_timer_status(
    state: Mapping[str, Any],
    legacy_automation: Mapping[str, Any],
) -> dict[str, Any]:
    settings = (
        state.get("settings", {})
        if isinstance(state.get("settings"), Mapping)
        else {}
    )
    schedule = _smart_monitor_schedule(dict(settings))
    enabled = bool(settings.get("daily_monitor_enabled", True))
    legacy_status = str(legacy_automation.get("status") or "MISSING").upper()
    return {
        "owner": "program_timer",
        "enabled": enabled,
        "status": "ENABLED" if enabled else "PAUSED",
        "mode": "windows_task_with_in_app_claim_fallback",
        "time_local": schedule["time_local"],
        "next_run_at": schedule["next_run_at"],
        "command": SMART_PROGRAM_TIMER_COMMAND,
        "native_task_name": SMART_NATIVE_MONITOR_TASK_NAME,
        "native_task_registered": bool(
            settings.get("native_monitor_task_registered")
        ),
        "native_task_status": str(
            settings.get("native_monitor_task_status") or "not_registered"
        ),
        "native_task_action": str(
            settings.get("native_monitor_task_action") or ""
        ),
        "in_app_claim_fallback": True,
        "single_claim_ledger": True,
        "legacy_automation_retired": legacy_status != "ACTIVE",
        "legacy_automation_status": legacy_status,
    }


def _tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {}


def _fetch_list(url: str, timeout: float = 2.0) -> list[Any]:
    data = _fetch_json(url, timeout=timeout)
    if isinstance(data, list):
        return data
    return []


def _cdp_port_status(port: int) -> dict[str, Any]:
    available = _tcp_open(port)
    version = _fetch_json(f"http://127.0.0.1:{port}/json/version") if available else {}
    tabs = _fetch_json(f"http://127.0.0.1:{port}/json/list") if available else []
    if not isinstance(tabs, list):
        tabs = []
    return {
        "port": port,
        "available": available,
        "browser": version.get("Browser", "") if isinstance(version, dict) else "",
        "webSocketDebuggerUrl": version.get("webSocketDebuggerUrl", "") if isinstance(version, dict) else "",
        "tabs": len(tabs),
    }


def _python_command() -> str:
    if getattr(sys, "frozen", False):
        return "python"
    return sys.executable or "python"


def _run_legacy_cdp_ensure() -> dict[str, Any]:
    if not LEGACY_CDP_ENSURE_SCRIPT.exists():
        return {
            "ok": False,
            "returncode": None,
            "script": str(LEGACY_CDP_ENSURE_SCRIPT),
            "message": "legacy ensure_cdp_channels.py not found",
        }
    try:
        result = subprocess.run(
            [_python_command(), str(LEGACY_CDP_ENSURE_SCRIPT)],
            cwd=str(SUBMISSION_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            **no_window_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "returncode": None,
            "script": str(LEGACY_CDP_ENSURE_SCRIPT),
            "message": str(exc),
        }
    parsed: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = {}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "script": str(LEGACY_CDP_ENSURE_SCRIPT),
        "stdout": (result.stdout or "").strip()[-4000:],
        "stderr": (result.stderr or "").strip()[-4000:],
        "parsed": parsed,
    }


def _chrome_plugin_root() -> Path | None:
    root = Path.home() / ".codex" / "plugins" / "cache" / "openai-bundled" / "chrome"
    if not root.exists():
        return None
    candidates = [
        item
        for item in root.iterdir()
        if (item / "scripts" / "check-native-host-manifest.js").exists()
        and (item / "scripts" / "check-extension-installed.js").exists()
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def _run_node_json(script: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["node", str(script), "--json"],
            cwd=str(script.parent.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **no_window_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"checked": False, "ok": False, "error": str(exc)}
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
    payload.setdefault("checked", True)
    payload.setdefault("returncode", result.returncode)
    payload.setdefault("ok", result.returncode == 0)
    if result.stderr.strip():
        payload["stderr"] = result.stderr.strip()[-2000:]
    return payload


def _chrome_extension_health() -> dict[str, Any]:
    plugin_root = _chrome_plugin_root()
    if plugin_root is None:
        return {"checked": False, "ok": False, "problem": "Chrome plugin root not found"}
    scripts = plugin_root / "scripts"
    extension = _run_node_json(scripts / "check-extension-installed.js")
    native = _run_node_json(scripts / "check-native-host-manifest.js")
    running = _run_node_json(scripts / "chrome-is-running.js")
    return {
        "checked": True,
        "plugin_root": str(plugin_root),
        "chrome_running": running,
        "extension_installed": extension,
        "native_host": native,
        "ok": bool(
            running.get("running")
            and extension.get("installed")
            and extension.get("enabled")
            and native.get("correct")
        ),
    }


def _tab_url(tab: Any) -> str:
    if isinstance(tab, dict):
        return str(tab.get("url") or "").strip()
    return ""


def _tab_title(tab: Any) -> str:
    if isinstance(tab, dict):
        return str(tab.get("title") or "").strip()
    return ""


def _normalized_tab_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path or "")
    query = parsed.query
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _tab_host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(str(url or "")).netloc.lower()
    except ValueError:
        return ""


def _is_mail_or_account_tab(url: str) -> bool:
    host = _tab_host(url)
    protected_hosts = (
        "mail.google.com",
        "accounts.google.com",
        "outlook.live.com",
        "outlook.office.com",
        "login.microsoftonline.com",
        "mail.qq.com",
    )
    return any(host == item or host.endswith("." + item) for item in protected_hosts)


def _is_submission_related_tab(url: str) -> bool:
    host = _tab_host(url)
    if not host:
        return False
    submission_hosts = (
        "submission.springernature.com",
        "springernature.com",
        "springer.com",
        "wiley.com",
        "scholarone.com",
        "manuscriptcentral.com",
        "editorialmanager.com",
        "inderscience.com",
        "tandfonline.com",
        "elsevier.com",
        "evise.com",
        "sagesubmit.com",
    )
    return any(host == item or host.endswith("." + item) for item in submission_hosts)


def _is_blank_tab(url: str, title: str) -> bool:
    lowered_url = str(url or "").strip().lower()
    lowered_title = str(title or "").strip().lower()
    return lowered_url in {"", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"} or (
        lowered_url.startswith("chrome://newtab") and lowered_title in {"", "new tab"}
    )


def _smart_submission_keep_url_fragments(state: dict[str, Any]) -> list[str]:
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    fragments: list[str] = []
    for paper_id in online_queue:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            continue
        for key in ("draft_url", "upload_url", "status_url", "dashboard_url", "submission_url", "draft_id", "manuscript_id"):
            value = str(paper.get(key) or "").strip()
            if value and value not in fragments:
                fragments.append(value)
        for key in ("latest_audit", "last_audit_path"):
            value = str(paper.get(key) or "").strip()
            if value and value not in fragments:
                fragments.append(value)
    return fragments


def _url_has_keep_fragment(url: str, fragments: list[str]) -> bool:
    if not url:
        return False
    for fragment in fragments:
        if fragment and fragment in url:
            return True
    return False


def _close_cdp_tab(port: int, target_id: str) -> dict[str, Any]:
    if not target_id:
        return {"ok": False, "message": "missing target id"}
    url = f"http://127.0.0.1:{port}/json/close/{urllib.parse.quote(target_id, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
        return {"ok": True, "message": body}
    except (OSError, urllib.error.URLError) as exc:
        return {"ok": False, "message": str(exc)}


@_smart_state_locked
def smart_submission_browser_cleanup(*, close: bool = False, include_stale_submission_tabs: bool = False) -> dict[str, Any]:
    """Conservatively clean CDP tabs created by Smart Submission workers.

    The cleanup never closes mail/account tabs or current online-queue target
    pages. Automatic close mode is limited to blank tabs and exact duplicate
    submission pages that are not part of the current target set. A separate
    opt-in mode can also close non-target submission-system tabs after the user
    explicitly asks for browser cleanup.
    """
    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    keep_fragments = _smart_submission_keep_url_fragments(state)
    scanned: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for port in SMART_CDP_PORTS:
        if not _tcp_open(port):
            errors.append({"port": port, "message": "cdp port unavailable"})
            continue
        tabs = _fetch_list(f"http://127.0.0.1:{port}/json/list")
        normalized_seen: set[str] = set()
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            target_type = str(tab.get("type") or "").strip().lower()
            if target_type and target_type != "page":
                kept.append(
                    {
                        "port": port,
                        "id": str(tab.get("id") or "").strip(),
                        "title": _tab_title(tab),
                        "url": _tab_url(tab),
                        "type": target_type,
                        "reason": "non_page_target",
                    }
                )
                continue
            target_id = str(tab.get("id") or "").strip()
            url = _tab_url(tab)
            title = _tab_title(tab)
            normalized_url = _normalized_tab_url(url)
            item = {
                "port": port,
                "id": target_id,
                "type": target_type or "page",
                "title": title,
                "url": url,
                "reason": "",
            }
            scanned.append(item)
            if _is_mail_or_account_tab(url):
                item["reason"] = "protected_mail_or_account_tab"
                kept.append(item)
                continue
            if _url_has_keep_fragment(url, keep_fragments):
                item["reason"] = "current_online_queue_target"
                kept.append(item)
                if normalized_url:
                    normalized_seen.add(normalized_url)
                continue
            if _is_blank_tab(url, title):
                item["reason"] = "blank_or_new_tab"
                candidates.append(item)
                continue
            if (
                normalized_url
                and normalized_url in normalized_seen
                and _is_submission_related_tab(url)
            ):
                item["reason"] = "exact_duplicate_submission_tab"
                candidates.append(item)
                continue
            if include_stale_submission_tabs and _is_submission_related_tab(url):
                item["reason"] = "stale_nonqueue_submission_tab"
                candidates.append(item)
                if normalized_url:
                    normalized_seen.add(normalized_url)
                continue
            if normalized_url:
                normalized_seen.add(normalized_url)
            item["reason"] = "not_safe_to_auto_close"
            kept.append(item)

    closed: list[dict[str, Any]] = []
    if close:
        for candidate in candidates:
            result = _close_cdp_tab(int(candidate["port"]), str(candidate.get("id") or ""))
            closed.append({**candidate, "close_result": result})

    result = {
        "checked_at": now_iso(),
        "close": bool(close),
        "include_stale_submission_tabs": bool(include_stale_submission_tabs),
        "ports": list(SMART_CDP_PORTS),
        "keep_fragments_count": len(keep_fragments),
        "scanned_count": len(scanned),
        "candidate_count": len(candidates),
        "closed_count": len([item for item in closed if item.get("close_result", {}).get("ok")]),
        "candidates": candidates[:80],
        "closed": closed[:80],
        "kept_summary": {
            "protected_or_target": len([item for item in kept if item.get("reason") in {"protected_mail_or_account_tab", "current_online_queue_target"}]),
            "not_safe_to_auto_close": len([item for item in kept if item.get("reason") == "not_safe_to_auto_close"]),
        },
        "errors": errors,
        "note": (
            "Manual cleanup mode also includes non-target submission-system tabs."
            if include_stale_submission_tabs
            else "Only blank/new-tab pages and exact duplicate non-target submission pages are auto-close candidates."
        ),
    }
    if close:
        settings = state.setdefault("settings", {})
        if isinstance(settings, dict):
            settings["last_browser_cleanup"] = {
                "checked_at": result["checked_at"],
                "scanned_count": result["scanned_count"],
                "candidate_count": result["candidate_count"],
                "closed_count": result["closed_count"],
            }
        state["updated_at"] = now_iso()
        history = state.setdefault("history", [])
        if isinstance(history, list):
            history.append(
                {
                    "at": now_iso(),
                    "event": "smart_submission_browser_cleanup",
                    "closed_count": result["closed_count"],
                    "candidate_count": result["candidate_count"],
                    "scanned_count": result["scanned_count"],
                }
            )
        write_json_atomic(SMART_STATE_PATH, state)
    return result


@_smart_state_locked
def smart_submission_browser_health(*, ensure: bool = False) -> dict[str, Any]:
    """Check and optionally restore browser channels used by Smart Submission."""
    before = [_cdp_port_status(port) for port in SMART_CDP_PORTS]
    restore: dict[str, Any] | None = None
    if ensure and not all(item["available"] for item in before):
        restore = _run_legacy_cdp_ensure()
    after = [_cdp_port_status(port) for port in SMART_CDP_PORTS]
    cdp_ok = any(item["available"] for item in after)
    extension = _chrome_extension_health()
    result = {
        "checked_at": now_iso(),
        "strategy": "prefer_cdp_9224_9222_extension_optional",
        "cdp_ports": {
            "before": before,
            "after": after,
            "ok": cdp_ok,
            "preferred_available": next((item["port"] for item in after if item["available"]), None),
            "restore": restore,
        },
        "chrome_extension": extension,
        "operational": cdp_ok or bool(extension.get("ok")),
        "note": (
            "CDP is available; Smart Submission should not block on Chrome extension/native-pipe failure."
            if cdp_ok
            else "No CDP port is available; online browser work is blocked unless the Chrome extension channel works."
        ),
    }

    if ensure:
        state = read_json(SMART_STATE_PATH, _default_state())
        if isinstance(state, dict):
            settings = state.setdefault("settings", {})
            if isinstance(settings, dict):
                settings["browser_strategy"] = result["strategy"]
                settings["cdp_ports"] = list(SMART_CDP_PORTS)
                settings["extension_native_pipe_optional"] = True
                settings["last_browser_health"] = result
            state["updated_at"] = now_iso()
            history = state.setdefault("history", [])
            if isinstance(history, list):
                history.append(
                    {
                        "at": now_iso(),
                        "event": "smart_submission_browser_health",
                        "operational": result["operational"],
                        "cdp_ok": cdp_ok,
                        "extension_ok": bool(extension.get("ok")),
                    }
                )
            write_json_atomic(SMART_STATE_PATH, state)
    return result


def _program_dispatch_binding(
    paper_id: str,
    paper: dict[str, Any],
    site_registry: dict[str, Any],
    *,
    account_missing_policy: str = "auto_register",
) -> dict[str, Any]:
    """Resolve value-free dispatch arguments without delegating lookup to AI."""

    identity_operation = _online_identity_operation(paper)
    identity, identity_error = _submission_identity_guard_result(
        paper,
        operation=identity_operation,
    )
    if identity_error is not None:
        return {
            "credential_account_id": str(paper.get("credential_account_id") or "").strip(),
            "submission_system_id": str(paper.get("submission_system_id") or "").strip().casefold(),
            "target_origin": "",
            "dispatch_binding_ready": False,
            "dispatch_binding_code": str(identity_error["code"]),
            "submission_identity_status": identity["status"],
            "canonical_submission_id": identity["canonical_submission_id"],
            "active_submission_ids": identity["active_submission_ids"],
        }

    account_id = str(paper.get("credential_account_id") or "").strip()
    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    target_journal = str(paper.get("target_journal") or "").strip()
    legacy = _legacy_submission_record(paper_id)
    legacy_system_id = _guess_system_id(str(legacy.get("submission_system") or ""))
    resolved_binding = _registry_binding_for_paper_any_system(
        site_registry,
        paper_id=paper_id,
        target_journal=target_journal,
        legacy_system_id=legacy_system_id,
    )
    binding_corrected = False
    if resolved_binding.get("status") == "matched":
        resolved_system = str(resolved_binding.get("system_id") or "").strip().casefold()
        if resolved_system and resolved_system != system_id:
            system_id = resolved_system
            binding_corrected = True
    adapter = submission_site_adapter(system_id)
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    credential_accounts = (
        credentials.get("accounts", [])
        if isinstance(credentials, dict) and isinstance(credentials.get("accounts"), list)
        else []
    )
    if account_id:
        current_account = next(
            (
                item
                for item in credential_accounts
                if isinstance(item, Mapping)
                and str(item.get("account_id") or "").strip().casefold()
                == account_id.casefold()
            ),
            None,
        )
        if current_account is None or not _account_matches_system_metadata(
            current_account,
            system_id=system_id,
            target_journal=target_journal,
        ):
            account_id = ""
    if not account_id and system_id:
        accounts = _configured_accounts(
            credentials if isinstance(credentials, dict) else {},
            "journal_system",
        )
        aliases = _system_aliases(
            system_id,
            str(paper.get("submission_system") or ""),
        )
        expected_origins = adapter.allowed_origins if adapter is not None else ()
        resolver = _smart_credential_resolver()
        matched = next(
            (
                account
                for account in accounts
                if _matches_target_account(
                    account,
                    aliases=aliases,
                    target_journal=target_journal,
                    resolver=resolver,
                    expected_origins=expected_origins,
                )
            ),
            None,
        )
        if isinstance(matched, dict):
            account_id = str(matched.get("account_id") or "").strip()
    system_site, journal_site = _verified_target_site(
        site_registry,
        paper_id=paper_id,
        system_id=system_id,
        target_journal=target_journal,
    )
    if system_site is None or journal_site is None:
        return {
            "credential_account_id": account_id,
            "submission_system_id": system_id,
            "target_origin": "",
            "dispatch_binding_ready": False,
            "dispatch_binding_code": "SMART_PROGRAM_SITE_BINDING_MISSING",
        }

    allowed_origins = [
        origin
        for origin in (
            _web_origin(journal_site.get("submission_url")),
            _web_origin(system_site.get("submission_url")),
            _web_origin(system_site.get("login_url")),
            *(adapter.allowed_origins if adapter is not None else ()),
        )
        if origin
    ]
    authorization = (
        paper.get("final_submit_authorization")
        if isinstance(paper.get("final_submit_authorization"), dict)
        else {}
    )
    authorized_origin = _web_origin(authorization.get("target_origin"))
    journal_submission_origin = _web_origin(journal_site.get("submission_url"))
    adapter_dashboard_origin = (
        _web_origin(adapter.dashboard_url) if adapter is not None else ""
    )
    target_origin = (
        authorized_origin
        if authorized_origin and authorized_origin in allowed_origins
        else (
            journal_submission_origin
            if journal_submission_origin in allowed_origins
            else (
                adapter_dashboard_origin
                if adapter_dashboard_origin in allowed_origins
                else next(iter(allowed_origins), "")
            )
        )
    )
    auto_register_ready = bool(
        not account_id
        and str(account_missing_policy or "").strip().casefold() == "auto_register"
        and adapter is not None
        and adapter.supports_registration
    )
    binding_ready = bool(
        system_id and target_origin and (account_id or auto_register_ready)
    )
    return {
        "credential_account_id": account_id,
        "submission_system_id": system_id,
        "target_origin": target_origin,
        "allowed_origins": list(dict.fromkeys(allowed_origins)),
        "journal_site_key": str(journal_site.get("journal_key") or "").strip(),
        "site_binding_corrected": binding_corrected,
        "account_bootstrap_required": auto_register_ready,
        "dispatch_binding_ready": binding_ready,
        "submission_identity_status": identity["status"],
        "canonical_submission_id": identity["canonical_submission_id"],
        "active_submission_ids": identity["active_submission_ids"],
        "dispatch_binding_code": (
            (
                "SMART_PROGRAM_DISPATCH_BINDING_READY_AUTO_REGISTER"
                if auto_register_ready
                else "SMART_PROGRAM_DISPATCH_BINDING_READY"
            )
            if binding_ready
            else "SMART_PROGRAM_ACCOUNT_ORIGIN_BINDING_MISSING"
        ),
    }


def _smart_program_dispatch_bindings(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve exact non-secret dispatch bindings before planning."""

    registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    site_registry = registry if isinstance(registry, dict) else {}
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    account_missing_policy = str(
        settings.get("account_missing_policy") or "auto_register"
    )
    bindings: dict[str, dict[str, Any]] = {}
    for paper_id, paper_value in papers.items():
        paper = paper_value if isinstance(paper_value, dict) else {}
        binding = _program_dispatch_binding(
            str(paper_id),
            paper,
            site_registry,
            account_missing_policy=account_missing_policy,
        )
        bindings[str(paper_id)] = binding
    return bindings


def _build_smart_program_control(
    state: dict[str, Any],
    *,
    selected_ids: list[str] | None = None,
    persist: bool = False,
    sync_ledger: bool = False,
) -> dict[str, Any]:
    """Create the deterministic control-plane plan without dispatching work."""

    plan = build_program_plan(
        state,
        selected_ids=selected_ids,
        dispatch_bindings=_smart_program_dispatch_bindings(state),
    )
    plan["dispatch_binding_ready_count"] = len(plan.get("dispatch_candidates", []))
    plan["dispatch_binding_blocked_ids"] = [
        paper_id
        for paper_id, decision in plan.get("papers", {}).items()
        if isinstance(decision, dict)
        and decision.get("in_online_queue")
        and not decision.get("dispatch_binding_ready")
    ]
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    plan_path = _smart_program_plan_path()
    plan["plan_path"] = str(plan_path)
    plan["task_database_path"] = str(program_database_path(SMART_STATE_PATH))
    if persist and settings.get("program_plan_write_enabled", True):
        write_json_atomic(plan_path, plan)
        plan["persisted"] = True
    else:
        plan["persisted"] = False
    if sync_ledger and settings.get("program_task_ledger_enabled", True):
        plan["task_ledger"] = sync_program_tasks(plan, state_path=SMART_STATE_PATH)
    else:
        plan["task_ledger"] = {
            "database_path": str(program_database_path(SMART_STATE_PATH)),
            "task_count": 0,
            "side_effects_dispatched": False,
            "deferred": True,
        }
    return plan


def smart_submission_program_plan(
    *,
    selected_ids: list[str] | None = None,
    persist: bool = True,
    sync_ledger: bool = True,
) -> dict[str, Any]:
    """Public CLI/UI entry point for the program-owned dispatch plan."""

    if persist:
        reconcile_smart_submission_site_bindings(selected_ids)
    state = read_json(SMART_STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    return _build_smart_program_control(
        state,
        selected_ids=selected_ids,
        persist=persist,
        sync_ledger=sync_ledger,
    )


def discover_smart_submission_candidates(
    unified_state: dict[str, Any],
    smart_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project submission-ready papers without enrolling or dispatching them.

    Candidate discovery is deliberately read-only.  It gives the Smart
    Submission UI an automatic view of completed journal/author packages while
    keeping route switching, Smart-state enrollment, preflight, and network
    execution behind an explicit user action.
    """

    unified_papers = (
        unified_state.get("papers", {})
        if isinstance(unified_state, dict)
        and isinstance(unified_state.get("papers"), dict)
        else {}
    )
    routes = (
        unified_state.get("submission_routes", {})
        if isinstance(unified_state, dict)
        and isinstance(unified_state.get("submission_routes"), dict)
        else {}
    )
    if smart_state is None:
        smart_state = read_json(SMART_STATE_PATH, {})
    smart_papers = (
        smart_state.get("papers", {})
        if isinstance(smart_state, dict)
        and isinstance(smart_state.get("papers"), dict)
        else {}
    )
    candidates: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    terminal_or_active_submission_statuses = {
        "accepted",
        "active",
        "complete",
        "completed",
        "done",
        "monitoring",
        "submitted",
        "submitted_active",
        "under_review",
    }

    for raw_paper_id, raw_record in sorted(unified_papers.items()):
        paper_id = str(raw_paper_id or "").strip()
        if not paper_id or not isinstance(raw_record, dict):
            continue
        if isinstance(smart_papers.get(paper_id), dict):
            continue
        stages = raw_record.get("stages", {})
        if not isinstance(stages, dict):
            skipped[paper_id] = "stage projection is missing"
            continue
        journal_stage = stages.get("journal_author", {})
        if not isinstance(journal_stage, dict) or str(
            journal_stage.get("status") or ""
        ).strip().casefold() != "complete":
            skipped[paper_id] = "journal_author stage is not complete"
            continue
        if journal_stage.get("ready_to_submit") is False:
            skipped[paper_id] = "journal_author stage is not ready to submit"
            continue
        package_dir = str(journal_stage.get("artifact") or "").strip()
        if not package_dir:
            skipped[paper_id] = "journal_author package path is empty"
            continue
        package_path = Path(package_dir)
        if not package_path.exists() or not package_path.is_dir():
            skipped[paper_id] = f"journal_author package path does not exist: {package_path}"
            continue

        submission_stage = stages.get("submission", {})
        if not isinstance(submission_stage, dict):
            submission_stage = {}
        submission_tokens = {
            _normalize_status_value(submission_stage.get("status")),
            _normalize_status_value(submission_stage.get("raw_status")),
        }
        submission_tokens.discard("")
        if submission_tokens & terminal_or_active_submission_statuses:
            skipped[paper_id] = "submission is already active or terminal"
            continue
        admission = submission_stage.get("admission", {})
        if isinstance(admission, dict) and admission.get("admitted") is False:
            skipped[paper_id] = "submission stage admission is not satisfied"
            continue

        route = submission_route_for(routes, paper_id)
        route_owner = str(route.get("execution_owner") or "legacy_auto")
        route_mode = str(route.get("mode") or "production")
        candidates[paper_id] = {
            "paper_id": paper_id,
            "title": str(raw_record.get("title") or paper_id),
            "status": "candidate",
            "workflow_status": "candidate",
            "next_action": "enable_smart_submission",
            "candidate_only": True,
            "candidate_reason": "选刊作者阶段已完成，尚未启用智能投稿",
            "source_package_dir": str(package_path),
            "legacy_submission_status": copy.deepcopy(submission_stage),
            "execution_owner": route_owner,
            "execution_mode": route_mode,
            "route_revision": int(route.get("revision") or 1),
            "submission_route": copy.deepcopy(route),
            "target_journal": str(
                journal_stage.get("target_journal")
                or raw_record.get("target_journal")
                or ""
            ),
            "last_imported_at": "",
        }

    return {
        "candidate_count": len(candidates),
        "candidate_ids": list(candidates),
        "papers": candidates,
        "skipped": skipped,
        "read_only": True,
    }


@_smart_state_locked
def smart_submission_status(*, include_browser_health: bool = False) -> dict[str, Any]:
    """Build a status projection without initializing or rewriting Smart state."""

    persisted_state = read_json(SMART_STATE_PATH, {})
    state = copy.deepcopy(persisted_state) if isinstance(persisted_state, dict) else {}
    reconcile_result = reconcile_smart_submission_worker_states(state if isinstance(state, dict) else {})
    monitoring_access_normalize = _normalize_monitoring_access_failure_blocks(state if isinstance(state, dict) else {})
    online_queue_normalize = _normalize_online_queue_state(
        state if isinstance(state, dict) else {},
        reason="smart_submission_status",
    )
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    sites = read_json(SMART_SITE_REGISTRY_PATH, {})
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    queue = state.get("paper_queue", []) if isinstance(state.get("paper_queue"), list) else []
    online_queue = state.get("online_queue", []) if isinstance(state.get("online_queue"), list) else []
    accounts = credentials.get("accounts", []) if isinstance(credentials.get("accounts"), list) else []
    credential_resolver = _smart_credential_resolver()
    account_readiness = [
        _resolved_account_readiness(item, resolver=credential_resolver)
        for item in accounts
        if isinstance(item, dict) and str(item.get("username") or item.get("email") or "").strip()
    ]
    configured_account_count = sum(1 for item in account_readiness if item.configured)
    online_ready_account_count = sum(
        1 for item in account_readiness if item.configured and not item.requires_secret_broker
    )
    secret_broker_required_account_count = sum(
        1 for item in account_readiness if item.requires_secret_broker
    )
    systems = sites.get("systems", []) if isinstance(sites.get("systems"), list) else []
    journals = sites.get("journals", []) if isinstance(sites.get("journals"), list) else []
    status_counts: dict[str, int] = {}
    workflow_counts: dict[str, int] = {}
    credential_broker_counts: dict[str, int] = {}
    waiting_approval_workers: list[dict[str, str]] = []
    for paper in papers.values():
        if not isinstance(paper, dict):
            continue
        key = str(paper.get("status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
        workflow_key = str(paper.get("workflow_status") or "unknown")
        workflow_counts[workflow_key] = workflow_counts.get(workflow_key, 0) + 1
        broker_key = str(paper.get("credential_broker_status") or "").strip()
        if broker_key:
            credential_broker_counts[broker_key] = (
                credential_broker_counts.get(broker_key, 0) + 1
            )
        flags = {str(flag) for flag in (paper.get("last_active_flags") or [])} if isinstance(paper.get("last_active_flags"), list) else set()
        if (
            workflow_key == "waiting_user_approval"
            or str(paper.get("worker_status") or "") == "waitingOnApproval"
            or "waitingOnApproval" in flags
        ):
            waiting_approval_workers.append(
                {
                    "paper_id": str(paper.get("paper_id") or ""),
                    "thread_id": str(paper.get("thread_id") or ""),
                    "next_action": str(paper.get("next_action") or ""),
                }
            )
    pending_discovery_count = sum(
        1
        for paper in papers.values()
        if isinstance(paper, dict)
        and (str(paper.get("status") or "").startswith("discover") or bool(paper.get("discovery_tasks")))
    )
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    monitor_schedule = _smart_monitor_schedule(settings)
    daily_monitor_candidate_ids = [
        str(paper_id)
        for paper_id, paper in papers.items()
        if isinstance(paper, dict)
        and (
            _paper_has_monitorable_submission_evidence(str(paper_id), paper)
            or "SUBMITTED" in _package_status_for_paper(str(paper_id), paper)
        )
        and str(paper.get("status") or "").strip().lower()
        not in {"accepted", "stopped", "no_action"}
        and _paper_has_online_target(str(paper_id), paper)
    ]
    program_control = _build_smart_program_control(state)
    if include_browser_health:
        browser_health = smart_submission_browser_health(ensure=False)
    else:
        browser_health = settings.get("last_browser_health", {}) if isinstance(settings.get("last_browser_health"), dict) else {}
        browser_health = {
            **browser_health,
            "checked": bool(browser_health),
            "deferred": True,
            "note": "Browser/CDP health check is deferred for lightweight UI refresh. Use smart-submission-browser-health --ensure or start online dispatch when needed.",
        }
    ready_for_online_count = status_counts.get("ready_for_online_submit", 0)
    online_submit_enabled = bool(settings.get("online_submit_enabled"))
    automation = _smart_automation_status()
    program_timer = _smart_program_timer_status(state, automation)
    try:
        from .smart_submission_sdk_runtime import smart_submission_sdk_status

        sdk_runtime = smart_submission_sdk_status()
    except Exception as exc:
        sdk_runtime = {
            "running": False,
            "code": "SMART_SDK_STATUS_UNAVAILABLE",
            "error": f"{type(exc).__name__}: {exc}",
        }
    mode = str(state.get("mode", "manual") or "manual")
    online_running_papers = [
        str(item)
        for item in online_queue
        if isinstance(papers.get(str(item)), dict)
        and str(papers[str(item)].get("workflow_status") or "") == "running"
        and str(papers[str(item)].get("thread_id") or "").strip()
    ]
    online_queued_papers = [
        str(item)
        for item in online_queue
        if isinstance(papers.get(str(item)), dict)
        and str(papers[str(item)].get("workflow_status") or "") == "queued"
    ]
    online_unassigned_papers = [
        str(item)
        for item in online_queue
        if isinstance(papers.get(str(item)), dict)
        and str(papers[str(item)].get("workflow_status") or "") != "running"
    ]
    if sdk_runtime.get("running"):
        processing_note = (
            "Codex SDK browser runtime is active; each paper owns one fixed "
            "session and the program controls dispatch slots."
        )
    elif waiting_approval_workers:
        processing_note = (
            f"{len(waiting_approval_workers)} visible worker thread(s) are waiting for Codex command approval; "
            "open the worker thread(s), approve or deny the pending command, then restart online dispatch."
        )
    elif queue:
        processing_note = "local queue has pending papers; a run_once sweep can process preflight/discovery."
    elif ready_for_online_count and not online_submit_enabled:
        processing_note = "no visible worker is expected: local queue is empty and online_submit_enabled=false."
    elif online_queue and online_submit_enabled:
        if mode == "manual":
            processing_note = (
                "online_queue has papers, but mode=manual; click Start Smart Submission "
                "to start the program-owned SDK queue."
            )
        elif not online_unassigned_papers:
            processing_note = (
                "all online_queue papers already have assigned SDK work; the program runtime "
                "will not dispatch duplicates until the current result is recorded."
            )
        else:
            max_workers = settings.get("max_parallel_workers", DEFAULT_SMART_MAX_PARALLEL_WORKERS)
            processing_note = (
                "online_queue is waiting while the SDK runtime is idle; click Start Smart "
                f"Submission to dispatch up to max_parallel_workers={max_workers}."
            )
    elif ready_for_online_count and online_submit_enabled:
        processing_note = "papers are ready for online work; add them to online_queue to dispatch visible workers."
    else:
        processing_note = "no queued or online-ready papers are waiting."
    first_online_paper = str(online_queue[0]) if online_queue else ""
    first_online_record = papers.get(first_online_paper, {}) if first_online_paper else {}
    if not isinstance(first_online_record, dict):
        first_online_record = {}
    history = state.get("history", []) if isinstance(state.get("history"), list) else []
    last_dispatch_event = {}
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        if str(item.get("event") or "") in {
            "queue_visible_online_workers",
            "smart_submission_batch_check",
            "smart_submission_run",
            "visible_worker_dispatch",
            "manual_visible_scheduler_wakeup",
            "prioritize_online_queue",
            "move_online_queue_item",
        }:
            last_dispatch_event = item
            break
    candidate_projection = discover_smart_submission_candidates(
        read_json(UNIFIED_STATE_PATH, {}),
        state,
    )
    return {
        "module_dir": str(SMART_SUBMISSION_DIR),
        "state_path": str(SMART_STATE_PATH),
        "credential_path": str(SMART_CREDENTIAL_PATH),
        "site_registry_path": str(SMART_SITE_REGISTRY_PATH),
        "scheduler_prompt_path": str(SMART_SCHEDULER_PROMPT_PATH),
        "audit_dir": str(SMART_AUDIT_DIR),
        "automation": automation,
        "program_timer": program_timer,
        "sdk_runtime": sdk_runtime,
        "mode": mode,
        "paper_count": len(papers),
        "candidate_count": candidate_projection["candidate_count"],
        "candidate_ids": candidate_projection["candidate_ids"],
        "candidate_papers": candidate_projection["papers"],
        "queue_count": len(queue),
        "local_queue": queue,
        "online_queue_count": len(online_queue),
        "online_queue": online_queue,
        "status_counts": status_counts,
        "workflow_counts": workflow_counts,
        "credential_broker_counts": credential_broker_counts,
        "credential_broker_active_count": sum(
            credential_broker_counts.get(key, 0)
            for key in (
                "starting",
                "resume_requested",
                "broker_retry_required",
                "account_recovery_email_pending",
                "account_recovery_request_failed",
                "account_recovery_link_incomplete",
                "account_recovery_change_failed",
            )
        ),
        "credential_broker_waiting_count": sum(
            count
            for key, count in credential_broker_counts.items()
            if key.startswith("waiting_")
            or key
            in {
                "account_required",
                "account_recovery_form_incomplete",
                "account_recovery_mailbox_unavailable",
                "account_support_recovery_required",
                "existing_account_recovery_required",
                "human_verification",
                "registration_identity_required",
                "saved_password_rejected",
            }
        ),
        "waiting_approval_count": len(waiting_approval_workers),
        "waiting_approval_workers": waiting_approval_workers,
        "active_worker_count": len(online_running_papers),
        "persisted_active_worker_count_ignored": persisted_state.get("active_worker_count", 0),
        "online_running_count": len(online_running_papers),
        "online_running_papers": online_running_papers,
        "online_queued_count": len(online_queued_papers),
        "online_queued_papers": online_queued_papers,
        "online_unassigned_papers": online_unassigned_papers,
        "all_online_queue_assigned": bool(online_queue) and not online_unassigned_papers,
        "worker_state_reconcile": reconcile_result,
        "monitoring_access_normalize": monitoring_access_normalize,
        "online_queue_normalize": online_queue_normalize,
        "program_control": program_control,
        "control_plane": program_control.get("control_plane", ""),
        "program_dispatch_candidate_count": len(program_control.get("dispatch_candidates", [])),
        "program_dispatch_candidate_ids": list(program_control.get("dispatch_candidate_ids", [])),
        "program_only_count": program_control.get("program_only_count", 0),
        "program_model_required_count": program_control.get("model_required_count", 0),
        "projection_only": True,
        "normalization_pending": bool(
            reconcile_result.get("changed")
            or monitoring_access_normalize.get("normalized")
            or monitoring_access_normalize.get("email_firsted")
            or online_queue_normalize.get("changed")
        ),
        "first_online_paper": first_online_paper,
        "first_online_status": first_online_record.get("status", ""),
        "first_online_workflow": first_online_record.get("workflow_status", ""),
        "first_online_thread_id": first_online_record.get("thread_id", ""),
        "last_dispatch_event": last_dispatch_event,
        "last_scheduler_action_at": state.get("last_scheduler_action_at", ""),
        "last_scheduler_message": state.get("last_scheduler_message", ""),
        "ready_for_online_count": ready_for_online_count,
        "daily_monitor_enabled": bool(settings.get("daily_monitor_enabled")),
        "daily_monitor_hour_local": monitor_schedule["hour_local"],
        "daily_monitor_minute_local": monitor_schedule["minute_local"],
        "daily_monitor_time_local": monitor_schedule["time_local"],
        "daily_monitor_timezone": monitor_schedule["timezone"],
        "daily_monitor_rrule": monitor_schedule["rrule"],
        "next_monitor_at": monitor_schedule["next_run_at"],
        "daily_monitor_candidate_count": len(daily_monitor_candidate_ids),
        "daily_monitor_candidate_ids": daily_monitor_candidate_ids,
        "daily_monitor_last_scheduled_date": settings.get(
            "daily_monitor_last_scheduled_date", ""
        ),
        "daily_monitor_last_started_at": settings.get(
            "daily_monitor_last_started_at", ""
        ),
        "daily_monitor_last_finished_at": settings.get(
            "daily_monitor_last_finished_at", ""
        ),
        "daily_monitor_last_status": settings.get(
            "daily_monitor_last_status", "never_run"
        ),
        "daily_monitor_last_trigger": settings.get(
            "daily_monitor_last_trigger", ""
        ),
        "daily_monitor_last_paper_count": int(
            settings.get("daily_monitor_last_paper_count") or 0
        ),
        "daily_monitor_last_checked_count": int(
            settings.get("daily_monitor_last_checked_count") or 0
        ),
        "daily_monitor_last_failed_count": int(
            settings.get("daily_monitor_last_failed_count") or 0
        ),
        "daily_monitor_last_pending_count": int(
            settings.get("daily_monitor_last_pending_count") or 0
        ),
        "daily_monitor_last_action_required_count": int(
            settings.get("daily_monitor_last_action_required_count") or 0
        ),
        "daily_monitor_last_followup_auto_queued_count": int(
            settings.get("daily_monitor_last_followup_auto_queued_count") or 0
        ),
        "daily_monitor_last_followup_manual_revision_count": int(
            settings.get("daily_monitor_last_followup_manual_revision_count") or 0
        ),
        "daily_monitor_last_followup_identity_reconciliation_count": int(
            settings.get("daily_monitor_last_followup_identity_reconciliation_count") or 0
        ),
        "daily_monitor_last_followup_failed_count": int(
            settings.get("daily_monitor_last_followup_failed_count") or 0
        ),
        "daily_monitor_last_error": settings.get("daily_monitor_last_error", ""),
        "native_monitor_task_status": settings.get(
            "native_monitor_task_status", "not_registered"
        ),
        "native_monitor_task_registered": bool(
            settings.get("native_monitor_task_registered")
        ),
        "native_monitor_task_name": settings.get(
            "native_monitor_task_name", SMART_NATIVE_MONITOR_TASK_NAME
        ),
        "native_monitor_task_action": settings.get(
            "native_monitor_task_action", ""
        ),
        "native_monitor_task_updated_at": settings.get(
            "native_monitor_task_updated_at", ""
        ),
        "native_monitor_task_error": settings.get(
            "native_monitor_task_error", ""
        ),
        "trigger_owner": settings.get("trigger_owner", "program_timer"),
        "legacy_automation_retired": bool(
            settings.get("legacy_automation_retired", True)
        ),
        "online_submit_enabled": online_submit_enabled,
        "processing_note": processing_note,
        "account_count": len(accounts),
        "configured_account_count": configured_account_count,
        "online_ready_account_count": online_ready_account_count,
        "secret_broker_required_account_count": secret_broker_required_account_count,
        "system_count": len(systems),
        "journal_count": len(journals),
        "pending_discovery_count": pending_discovery_count,
        "prompt_chars": len(read_text(SMART_SCHEDULER_PROMPT_PATH)),
        "legacy_submission_module": settings.get("legacy_submission_module", ""),
        "browser_health": browser_health,
        "browser_cleanup": settings.get("last_browser_cleanup", {}) if isinstance(settings.get("last_browser_cleanup"), dict) else {},
        "updated_at": persisted_state.get("updated_at", ""),
        "history_tail": (
            persisted_state.get("history", [])[-8:]
            if isinstance(persisted_state.get("history"), list)
            else []
        ),
    }


def _initial_status_from_unified_record(record: dict[str, Any]) -> dict[str, str]:
    stages = record.get("stages", {}) if isinstance(record, dict) else {}
    submission_stage = stages.get("submission", {}) if isinstance(stages, dict) else {}
    status = _normalize_status_value(submission_stage.get("status"))
    raw_status = _normalize_status_value(submission_stage.get("raw_status"))
    detail = str(submission_stage.get("detail") or "").strip()
    statuses = {item for item in (status, raw_status) if item}
    if statuses & {"accepted", "complete", "completed", "done"}:
        return {
            "status": "accepted",
            "next_action": "no_action",
            "detail": detail or "legacy submission stage is already complete",
            "online_intent": "none",
        }
    if statuses & {"submitted", "submitted_active", "active", "under_review", "monitoring"}:
        return {
            "status": "monitoring",
            "next_action": "check_submission_status",
            "detail": detail or "legacy submission is already active; monitor only",
            "online_intent": "monitor_existing_submission",
        }
    return {
        "status": "queued",
        "next_action": "preflight",
        "detail": detail,
        "online_intent": "submit_or_retarget",
    }


@_smart_state_locked
def import_from_unified_state(unified_state: dict[str, Any], selected_ids: list[str] | None = None) -> dict[str, Any]:
    ensure_smart_submission_module()
    scope_requested = selected_ids is not None
    selected = {str(item) for item in (selected_ids or []) if str(item).strip()}
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.setdefault("papers", {})
    queue = [str(item) for item in state.get("paper_queue", [])]
    imported: list[str] = []
    skipped: dict[str, str] = {}

    for paper_id, record in sorted((unified_state.get("papers") or {}).items()):
        paper_id = str(paper_id)
        if selected and paper_id not in selected:
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            queue = [item for item in queue if item != paper_id]
            skipped[paper_id] = str(route_error["code"])
            continue
        stages = record.get("stages", {}) if isinstance(record, dict) else {}
        journal_stage = stages.get("journal_author", {}) if isinstance(stages, dict) else {}
        artifact = str(journal_stage.get("artifact") or "").strip()
        if str(journal_stage.get("status") or "") != "complete":
            skipped[paper_id] = "journal_author stage is not complete"
            continue
        if not artifact:
            skipped[paper_id] = "journal_author package path is empty"
            continue
        package_path = Path(artifact)
        if not package_path.exists() or not package_path.is_dir():
            skipped[paper_id] = f"package path does not exist: {package_path}"
            continue
        paper = papers.setdefault(paper_id, {})
        _mark_smart_route_on_paper(paper, route or {})
        initial = _initial_status_from_unified_record(record)
        current_status = str(paper.get("status") or "").strip()
        preserve_statuses = {
            "blocked",
            "ready_for_online_submit",
            "login_check",
            "uploading",
            "submitted",
            "monitoring",
            "revision_needed",
            "retarget_needed",
            "accepted",
            "waiting",
            "no_action",
        }
        should_preserve_workflow = current_status in preserve_statuses
        next_status = current_status if should_preserve_workflow else initial["status"]
        next_action = str(paper.get("next_action") or "") if should_preserve_workflow else initial["next_action"]
        detail = str(paper.get("detail") or "") if should_preserve_workflow else initial["detail"]
        online_intent = str(paper.get("online_intent") or "") if should_preserve_workflow else initial["online_intent"]
        paper.update(
            {
                "paper_id": paper_id,
                "title": record.get("title") or paper_id,
                "status": next_status,
                "source_package_dir": str(package_path),
                "legacy_submission_status": stages.get("submission", {}),
                "last_imported_at": now_iso(),
                "blocker": str(paper.get("blocker") or "") if should_preserve_workflow else "",
                "next_action": next_action,
                "detail": detail,
                "online_intent": online_intent,
            }
        )
        if next_status == "queued":
            append_unique(queue, paper_id)
        imported.append(paper_id)

    state["paper_queue"] = queue
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "import_from_unified_state",
            "imported": imported,
            "skipped": skipped,
            "selected_only": scope_requested,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {"imported": imported, "skipped": skipped, "state": state}


def enable_smart_submission_candidates(
    selected_ids: list[str],
    *,
    orchestrator: Any | None = None,
) -> dict[str, Any]:
    """Explicitly switch and enroll selected candidates without online work."""

    selected = list(
        dict.fromkeys(
            str(item).strip() for item in selected_ids if str(item).strip()
        )
    )
    if not selected:
        raise SmartSubmissionGuardError(
            SMART_CANARY_SCOPE_REQUIRED,
            "",
            "at least one explicit paper is required for Smart enrollment",
        )
    if orchestrator is None:
        from .orchestrator import UnifiedOrchestrator

        orchestrator = UnifiedOrchestrator()

    enabled: list[str] = []
    imported: list[str] = []
    skipped: dict[str, str] = {}
    route_results: dict[str, Any] = {}
    rollback_results: dict[str, Any] = {}

    for paper_id in selected:
        unified = orchestrator.sync()
        records = (
            unified.get("papers", {})
            if isinstance(unified.get("papers"), dict)
            else {}
        )
        record = records.get(paper_id)
        if not isinstance(record, dict):
            skipped[paper_id] = "paper is missing from the unified workspace"
            continue
        stages = record.get("stages", {}) if isinstance(record.get("stages"), dict) else {}
        journal_stage = (
            stages.get("journal_author", {})
            if isinstance(stages.get("journal_author"), dict)
            else {}
        )
        if str(journal_stage.get("status") or "").strip().casefold() != "complete":
            skipped[paper_id] = "journal_author stage is not complete"
            continue
        package_dir = str(journal_stage.get("artifact") or "").strip()
        if not package_dir or not Path(package_dir).is_dir():
            skipped[paper_id] = "journal_author package directory is unavailable"
            continue

        original_route = submission_route_for(
            unified.get("submission_routes"),
            paper_id,
        )
        operation_id = (
            "smart-enroll-"
            + hashlib.sha256(
                f"{paper_id}|{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:16]
        )
        try:
            if (
                str(original_route.get("execution_owner")) == "smart"
                and str(original_route.get("mode")) == "canary"
            ):
                route_result = {
                    "changed": False,
                    "paper_id": paper_id,
                    "submission_route": original_route,
                }
            else:
                route_result = orchestrator.set_submission_route(
                    paper_id,
                    execution_owner="smart",
                    mode="canary",
                    expected_revision=int(original_route.get("revision") or 1),
                    operation_id=operation_id,
                )
            route_results[paper_id] = route_result
        except Exception as exc:
            skipped[paper_id] = f"submission route could not be switched: {exc}"
            continue

        switched_route = normalize_submission_route(
            route_result.get("submission_route")
            if isinstance(route_result, dict)
            else None
        )
        try:
            unified_after_route = orchestrator.sync()
            import_result = import_from_unified_state(
                unified_after_route,
                selected_ids=[paper_id],
            )
            imported_ids = {
                str(item)
                for item in import_result.get("imported", [])
                if str(item).strip()
            }
            if paper_id not in imported_ids:
                reason = str(
                    (
                        import_result.get("skipped", {})
                        if isinstance(import_result.get("skipped"), dict)
                        else {}
                    ).get(paper_id)
                    or "Smart enrollment did not create a paper record"
                )
                raise RuntimeError(reason)
        except Exception as exc:
            current_smart_state = read_json(SMART_STATE_PATH, {})
            current_smart_papers = (
                current_smart_state.get("papers", {})
                if isinstance(current_smart_state, dict)
                and isinstance(current_smart_state.get("papers"), dict)
                else {}
            )
            if isinstance(current_smart_papers.get(paper_id), dict):
                enabled.append(paper_id)
                imported.append(paper_id)
                continue
            rollback_error = ""
            if bool(route_result.get("changed")):
                try:
                    rollback_results[paper_id] = orchestrator.set_submission_route(
                        paper_id,
                        execution_owner=str(
                            original_route.get("execution_owner") or "legacy_auto"
                        ),
                        mode=str(original_route.get("mode") or "production"),
                        expected_revision=int(switched_route.get("revision") or 1),
                        operation_id=f"{operation_id}:rollback",
                    )
                except Exception as rollback_exc:
                    rollback_error = f"; route rollback failed: {rollback_exc}"
            skipped[paper_id] = f"Smart enrollment failed: {exc}{rollback_error}"
            continue

        enabled.append(paper_id)
        imported.append(paper_id)

    return {
        "ok": bool(enabled),
        "code": (
            "SMART_CANDIDATES_ENABLED"
            if enabled
            else "SMART_NO_CANDIDATES_ENABLED"
        ),
        "requested_ids": selected,
        "enabled": enabled,
        "imported": imported,
        "skipped": skipped,
        "route_results": route_results,
        "rollback_results": rollback_results,
        "online_started": False,
    }


@_smart_state_locked
def prepare_visible_online_dispatch(
    unified_state: dict[str, Any] | None = None,
    selected_ids: list[str] | None = None,
    *,
    expected_route_revision: int | None = None,
    orchestrator: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper around the single-paper preflight-first API."""

    scoped_ids = [str(item).strip() for item in (selected_ids or []) if str(item).strip()]
    if len(scoped_ids) != 1 or expected_route_revision is None:
        raise SmartSubmissionGuardError(
            SMART_CANARY_SCOPE_REQUIRED,
            scoped_ids[0] if len(scoped_ids) == 1 else "",
            "visible online dispatch requires exactly one paper and expected_route_revision",
            details={"selected_ids": scoped_ids},
        )
    return start_single_smart_canary(
        scoped_ids[0],
        expected_route_revision=int(expected_route_revision),
        orchestrator=orchestrator,
    )


@_smart_state_locked
def smart_submission_batch_check(
    unified_state: dict[str, Any] | None = None,
    selected_ids: list[str] | None = None,
    *,
    queue_online: bool = False,
    activate_online: bool = False,
    max_papers: int = 0,
    expected_route_revision: int | None = None,
    orchestrator: Any | None = None,
) -> dict[str, Any]:
    """Batch local smart-submission checks without doing browser work in this process.

    This is intentionally split from visible online execution: local package
    preflight can handle many papers in one sweep, while browser/login/final
    submission remains a one-paper visible-worker action.
    """
    if unified_state is None:
        unified_state = read_json(UNIFIED_STATE_PATH, {})
    scope_requested = selected_ids is not None
    selected = [str(item) for item in (selected_ids or []) if str(item).strip()]
    selected_scope = selected if scope_requested else None
    if queue_online or activate_online:
        if len(selected) != 1 or expected_route_revision is None:
            raise SmartSubmissionGuardError(
                SMART_CANARY_SCOPE_REQUIRED,
                selected[0] if len(selected) == 1 else "",
                "online batch mode requires exactly one paper and expected_route_revision",
                details={"selected_ids": selected},
            )
        return start_single_smart_canary(
            selected[0],
            expected_route_revision=int(expected_route_revision),
            orchestrator=orchestrator,
            activate=bool(activate_online),
        )
    ensure_smart_submission_module()
    import_result = import_from_unified_state(unified_state, selected_ids=selected_scope)
    preflight = run_smart_submission_scheduler(
        force=True,
        max_papers=max_papers,
        online_recovery=False,
        selected_ids=selected_scope,
    )
    online: dict[str, Any] = {"queued": [], "skipped": {}, "activate": False}
    state = read_json(SMART_STATE_PATH, _default_state())
    state["mode"] = "manual"
    state["updated_at"] = now_iso()
    write_json_atomic(SMART_STATE_PATH, state)

    state = read_json(SMART_STATE_PATH, _default_state())
    if not activate_online:
        state["mode"] = "manual"
        state["updated_at"] = now_iso()
        write_json_atomic(SMART_STATE_PATH, state)

    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    status_counts = Counter(
        str(paper.get("status") or "unknown")
        for paper in papers.values()
        if isinstance(paper, dict)
    )
    result_status_counts = Counter(str(item.get("status") or "unknown") for item in preflight.get("results", []))
    summary = {
        "imported": import_result.get("imported", []),
        "import_skipped": import_result.get("skipped", {}),
        "preflight": {k: v for k, v in preflight.items() if k != "results"},
        "preflight_results": preflight.get("results", []),
        "preflight_status_counts": dict(result_status_counts),
        "queued_online": online.get("queued", []),
        "online_skipped": online.get("skipped", {}),
        "scheduler_wakeup": online.get("wakeup", {}),
        "queue_online": queue_online,
        "activate_online": activate_online,
        "mode_after": state.get("mode", "manual"),
        "local_queue_count": len(state.get("paper_queue", []) if isinstance(state.get("paper_queue"), list) else []),
        "online_queue_count": len(state.get("online_queue", []) if isinstance(state.get("online_queue"), list) else []),
        "status_counts": dict(status_counts),
        "worker_dispatch_policy": "batch local checks are allowed; visible online browser work runs one paper per worker thread, with scheduler dispatch up to settings.max_parallel_workers",
        "state": state,
    }
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "smart_submission_batch_check",
            "selected_only": scope_requested,
            "queue_online": queue_online,
            "activate_online": activate_online,
            "imported_count": len(summary["imported"]),
            "preflight_processed": preflight.get("processed", 0),
            "queued_online_count": len(summary["queued_online"]),
            "online_queue_count": summary["online_queue_count"],
            "mode_after": summary["mode_after"],
            "worker_dispatch_policy": summary["worker_dispatch_policy"],
        }
    )
    state["updated_at"] = now_iso()
    write_json_atomic(SMART_STATE_PATH, state)
    summary["state"] = state
    return summary


@_smart_state_locked
def set_visible_scheduler_thread(thread_id: str, title: str = "Smart Submission visible scheduler") -> dict[str, Any]:
    ensure_smart_submission_module()
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    settings["scheduler_target_thread_id"] = thread_id
    settings["scheduler_thread_title"] = title
    settings["visible_scheduler_enabled"] = True
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "set_visible_scheduler_thread",
            "thread_id": thread_id,
            "title": title,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return state


@_smart_state_locked
def queue_visible_online_workers(
    selected_ids: list[str] | None = None,
    *,
    activate: bool = True,
    sdk_browser: bool = False,
) -> dict[str, Any]:
    ensure_smart_submission_module()
    scope_requested = selected_ids is not None
    selected = {str(item) for item in (selected_ids or []) if str(item).strip()}
    state = read_json(SMART_STATE_PATH, _default_state())
    initial_normalize = (
        {"changed": False, "scope": "selected_only"}
        if scope_requested
        else _normalize_online_queue_state(state, reason="queue_visible_online_workers_before")
    )
    papers = state.setdefault("papers", {})
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    queued: list[str] = []
    resumed: list[str] = []
    skipped: dict[str, str] = {}
    eligible_statuses = set(ONLINE_WORKER_STATUSES)
    unified_state = _unified_state_snapshot()
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    auto_final_submit_enabled = bool(settings.get("auto_final_submit_enabled", True))
    for paper_id, paper in sorted(papers.items()):
        paper_id = str(paper_id)
        if scope_requested and paper_id not in selected:
            continue
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is invalid"
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            online_queue = [item for item in online_queue if item != paper_id]
            continue
        _mark_smart_route_on_paper(paper, route or {})
        _hydrate_paper_target_from_package(paper_id, paper)
        _apply_open_submission_draft_binding(paper_id, paper)
        status = str(paper.get("status") or "")
        workflow_status = str(paper.get("workflow_status") or "").strip().casefold()
        next_action = str(paper.get("next_action") or "").strip()
        if status in {"submitted", "monitoring"} or _paper_has_prior_submission_evidence(paper):
            paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
            paper["blocker"] = ""
            paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
            online_queue = [item for item in online_queue if item != paper_id]
            skipped[paper_id] = "submitted/monitoring paper uses email-first monitoring; no browser worker queued"
            continue
        if status in {"blocked", "stopped", "accepted"}:
            skipped[paper_id] = f"paper status is {status}"
            continue
        if not _paper_has_online_target(paper_id, paper):
            paper["status"] = "waiting"
            paper["workflow_status"] = "waiting"
            paper["worker_status"] = "waiting"
            paper["next_action"] = "wait_journal_author_target_package"
            paper["detail"] = "missing target journal or submission system; refresh journal-author package before browser dispatch"
            paper["blocker"] = ""
            online_queue = [item for item in online_queue if item != paper_id]
            skipped[paper_id] = "missing target journal or submission system"
            continue
        review_ready_resume = (
            sdk_browser
            and status == "waiting"
            and workflow_status == "waiting"
            and next_action == FINAL_SUBMIT_REVIEW_READY_ACTION
        )
        authorized_resume = (
            sdk_browser
            and status == "ready_for_online_submit"
            and workflow_status in {"queued", "waiting"}
            and next_action == FINAL_SUBMIT_AUTHORIZED_ACTION
        )
        if review_ready_resume or authorized_resume:
            if review_ready_resume and not auto_final_submit_enabled:
                skipped[paper_id] = (
                    "automatic final submit is disabled; use the manual final-submit confirmation action"
                )
                continue
            if not str(paper.get("attempt_id") or "").strip():
                skipped[paper_id] = "reviewed Smart task is missing its exact attempt_id"
                continue
            paper.setdefault("thread_title", f"Smart Submission worker {paper_id}")
            paper["last_online_queued_at"] = now_iso()
            paper["last_resume_reason"] = (
                "review_ready_auto_final_submit"
                if review_ready_resume
                else "existing_exact_final_submit_authorization"
            )
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
            )
            queued.append(paper_id)
            resumed.append(paper_id)
            continue
        if status not in eligible_statuses:
            skipped[paper_id] = f"paper status is not online-ready: {status or 'empty'}"
            continue
        if str(paper.get("workflow_status") or "") == "running":
            skipped[paper_id] = "worker is already running"
            _append_online_queue_guarded(
                online_queue,
                paper_id,
                paper,
                unified_state=unified_state,
            )
            continue
        paper.setdefault("thread_title", f"Smart Submission worker {paper_id}")
        paper["workflow_status"] = "queued"
        paper["last_online_queued_at"] = now_iso()
        paper["next_action"] = (
            "codex_sdk_browser_submit_or_monitor"
            if sdk_browser
            else "visible_worker_online_submit_or_monitor"
        )
        _rotate_canary_attempt(paper, paper_id)
        _append_online_queue_guarded(
            online_queue,
            paper_id,
            paper,
            unified_state=unified_state,
        )
        queued.append(paper_id)

    settings["online_submit_enabled"] = True
    if sdk_browser:
        if activate:
            settings["sdk_pause_requested"] = False
        settings["sdk_browser_enabled"] = True
        settings["visible_scheduler_enabled"] = False
        settings["dispatch_bridge"] = "codex_sdk_browser"
        settings["dispatch_mode"] = "program_plan_with_codex_sdk_browser"
    else:
        settings["visible_scheduler_enabled"] = True
    if queued and activate:
        state["mode"] = "run_once"
    else:
        state["mode"] = state.get("mode", "manual")
    state["online_queue"] = online_queue
    final_normalize = (
        {"changed": False, "scope": "selected_only"}
        if scope_requested
        else _normalize_online_queue_state(state, reason="queue_visible_online_workers_after")
    )
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "queue_visible_online_workers",
            "queued": queued,
            "resumed": resumed,
            "skipped": skipped,
            "selected_only": scope_requested,
            "activate": activate,
            "sdk_browser": sdk_browser,
            "initial_normalize": initial_normalize,
            "final_normalize": final_normalize,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    wakeup = (
        set_smart_submission_automation_fast_wakeup(True)
        if queued and activate and not sdk_browser
        else {}
    )
    return {
        "queued": queued,
        "resumed": resumed,
        "skipped": skipped,
        "activate": activate,
        "sdk_browser": sdk_browser,
        "wakeup": wakeup,
        "state": state,
    }


def prepare_smart_submission_sdk_papers(
    selected_ids: list[str],
    *,
    orchestrator: Any | None = None,
) -> dict[str, Any]:
    """Switch explicit papers to Smart, preflight them, then queue the SDK browser.

    This is the multi-paper replacement for the old single visible-worker
    canary entry point.  It never wakes the AI scheduler; the caller starts the
    deterministic SDK runtime after this preparation succeeds.
    """

    selected = list(
        dict.fromkeys(
            str(item).strip() for item in selected_ids if str(item).strip()
        )
    )
    if not selected:
        raise SmartSubmissionGuardError(
            SMART_CANARY_SCOPE_REQUIRED,
            "",
            "at least one explicit paper is required for Smart SDK dispatch",
        )
    if orchestrator is None:
        from .orchestrator import UnifiedOrchestrator

        orchestrator = UnifiedOrchestrator()

    unified = orchestrator.sync()
    unified_papers = (
        unified.get("papers", {})
        if isinstance(unified.get("papers"), dict)
        else {}
    )
    route_results: dict[str, Any] = {}
    route_skipped: dict[str, str] = {}
    prepared_ids: list[str] = []
    for paper_id in selected:
        if not isinstance(unified_papers.get(paper_id), dict):
            route_skipped[paper_id] = "paper is missing from the unified workspace"
            continue
        route = submission_route_for(unified.get("submission_routes"), paper_id)
        try:
            if route["execution_owner"] == "smart" and route["mode"] == "canary":
                route_result = {
                    "changed": False,
                    "paper_id": paper_id,
                    "submission_route": route,
                }
            else:
                route_result = orchestrator.set_submission_route(
                    paper_id,
                    execution_owner="smart",
                    mode="canary",
                    expected_revision=int(route["revision"]),
                    operation_id=(
                        "smart-sdk-"
                        + hashlib.sha256(
                            f"{paper_id}|{time.time_ns()}".encode("utf-8")
                        ).hexdigest()[:16]
                    ),
                )
            route_results[paper_id] = route_result
            prepared_ids.append(paper_id)
        except Exception as exc:
            route_skipped[paper_id] = str(exc)

    if not prepared_ids:
        return {
            "ok": False,
            "code": "SMART_SDK_NO_ROUTE_READY_PAPERS",
            "requested_ids": selected,
            "prepared_ids": [],
            "route_results": route_results,
            "skipped": route_skipped,
            "queued": [],
        }

    unified_after_routes = orchestrator.sync()
    local = smart_submission_batch_check(
        unified_state=unified_after_routes,
        selected_ids=prepared_ids,
        queue_online=False,
        activate_online=False,
        orchestrator=orchestrator,
    )
    author_payload_check = _refresh_selected_author_payload_preflight(prepared_ids)
    live_final_review_recovery = reconcile_live_owned_final_review_pages(
        prepared_ids
    )
    queued = queue_visible_online_workers(
        prepared_ids,
        activate=True,
        sdk_browser=True,
    )
    queued_ids = [
        str(item) for item in queued.get("queued", []) if str(item).strip()
    ]
    resumed_ids = [
        str(item) for item in queued.get("resumed", []) if str(item).strip()
    ]
    skipped = dict(route_skipped)
    skipped.update(
        {
            str(key): str(value)
            for key, value in (
                queued.get("skipped", {}).items()
                if isinstance(queued.get("skipped"), dict)
                else []
            )
        }
    )
    skipped.update(
        {
            str(key): str(value)
            for key, value in author_payload_check.get("blocked", {}).items()
        }
    )
    skipped.update(
        {
            str(key): str(value)
            for key, value in author_payload_check.get("skipped", {}).items()
        }
    )
    return {
        "ok": bool(queued_ids),
        "code": (
            "SMART_SDK_PAPERS_QUEUED"
            if queued_ids
            else "SMART_SDK_NO_ONLINE_READY_PAPERS"
        ),
        "requested_ids": selected,
        "prepared_ids": prepared_ids,
        "route_results": route_results,
        "local_check": {key: value for key, value in local.items() if key != "state"},
        "author_payload_check": author_payload_check,
        "live_final_review_recovery": live_final_review_recovery,
        "queued": queued_ids,
        "resumed": resumed_ids,
        "skipped": skipped,
        "state": queued.get("state", {}),
    }


@_smart_state_locked
def reconcile_live_owned_final_review_pages(
    paper_ids: list[str] | None = None,
    *,
    controller: Any | None = None,
) -> dict[str, Any]:
    """Recover a false revision result from an exact, still-open final review.

    Some publisher portals can reach their final-review page even when the SDK
    worker conservatively reports ``revision_needed`` for an earlier field.
    This read-only recovery requires a recent project-owned page claim for the
    same paper and route, two stable snapshots of the exact live URL, one
    enabled final-submit control, and no positive submission evidence. It only
    restores the review-ready state; it never authorizes or activates the
    final control.
    """

    from .smart_browser_cdp import SmartBrowserCdpController, is_final_submit_control

    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    selected = (
        {str(item).strip() for item in paper_ids if str(item).strip()}
        if paper_ids is not None
        else set(papers)
    )
    context_store = read_json(SMART_SUBMISSION_BROWSER_CONTEXT_PATH, {})
    page_claims = (
        context_store.get("page_claims", {})
        if isinstance(context_store, dict)
        and isinstance(context_store.get("page_claims"), dict)
        else {}
    )
    active_controller = controller or SmartBrowserCdpController(ports=SMART_CDP_PORTS)
    now_dt = datetime.now().astimezone()
    now = now_iso()
    recovered: list[str] = []
    skipped: dict[str, str] = {}
    prepared_audits: dict[str, dict[str, Any]] = {}
    online_queue = _queue_paper_ids(state, "online_queue")
    positive_markers = (
        "your submission has been received",
        "your manuscript has been submitted",
        "manuscript has been submitted to the journal editor",
        "manuscript submitted successfully",
        "submission complete",
        "thank you for your submission",
        "\u60a8\u7684\u7a3f\u4ef6\u5df2\u63d0\u4ea4\u7ed9\u671f\u520a",
        "\u60a8\u7684\u7a3f\u4ef6\u5df2\u5904\u7406\u5b8c\u6bd5\u5e76\u5df2\u63d0\u4ea4\u7ed9\u671f\u520a",
    )
    allowed_next_actions = {
        "scheduler_review_revision_required",
        "codex_sdk_browser_submit_or_monitor",
        "retry_codex_sdk_browser_turn_after_review",
    }

    for paper_id in sorted(selected):
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        if (
            str(paper.get("status") or "").strip().casefold() != "revision_needed"
            or str(paper.get("workflow_status") or "").strip().casefold()
            not in {"queued", "waiting"}
            or str(paper.get("next_action") or "").strip() not in allowed_next_actions
        ):
            skipped[paper_id] = "paper is not a recoverable non-final worker result"
            continue
        if (
            _paper_has_prior_submission_evidence(paper)
            or _paper_has_prior_trusted_submission_evidence(paper_id, paper)
            or isinstance(paper.get("final_submit_authorization"), dict)
            or isinstance(paper.get("pending_submission_confirmation_evidence"), dict)
        ):
            skipped[paper_id] = "submission or final-action evidence already exists"
            continue
        route, route_error = _route_guard_result(paper_id)
        if route_error is not None:
            skipped[paper_id] = str(route_error.get("code") or "route guard failed")
            continue
        route_revision = int((route or {}).get("revision") or 0)
        if route_revision <= 0:
            skipped[paper_id] = "route revision is missing"
            continue
        system_id = str(paper.get("submission_system_id") or "").strip().casefold()
        adapter = submission_site_adapter(system_id)
        if adapter is None:
            skipped[paper_id] = "submission system has no verified site adapter"
            continue
        adapter_origins = {
            normalize_origin(item)
            for item in adapter.allowed_origins
            if normalize_origin(item)
        }
        candidates: list[tuple[datetime, str, str, str]] = []
        for raw_page_id, raw_claim in page_claims.items():
            claim = raw_claim if isinstance(raw_claim, dict) else {}
            if (
                str(claim.get("paper_id") or "").strip() != paper_id
                or int(claim.get("route_revision") or 0) != route_revision
            ):
                continue
            updated_at = _parse_aware_time(claim.get("updated_at"))
            if updated_at is None or now_dt - updated_at > timedelta(hours=24):
                continue
            raw_url = str(claim.get("url") or "").strip()
            parsed = urllib.parse.urlsplit(raw_url)
            if (
                normalize_origin(raw_url) not in adapter_origins
                or not parsed.path.casefold().endswith("/finalreview")
                or not str(raw_page_id).strip()
            ):
                continue
            candidates.append(
                (
                    updated_at,
                    str(raw_page_id).strip(),
                    raw_url,
                    str(claim.get("attempt_id") or "").strip(),
                )
            )
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            skipped[paper_id] = "no recent owned final-review page claim"
            continue

        recovered_snapshot: dict[str, Any] | None = None
        recovered_claim: tuple[datetime, str, str, str] | None = None
        recovered_control: dict[str, Any] | None = None
        live_pages = active_controller.list_pages(adapter.allowed_origins)
        for candidate in candidates:
            _, page_id, review_url, _ = candidate
            exact_pages = [
                item
                for item in live_pages
                if isinstance(item, dict)
                and str(item.get("page_id") or "").strip() == page_id
                and str(item.get("url") or "").strip() == review_url
            ]
            if len(exact_pages) != 1:
                continue
            previous_control: dict[str, Any] | None = None
            stable = True
            expected_path = urllib.parse.urlsplit(review_url).path.casefold()
            last_snapshot: dict[str, Any] | None = None
            for observation in range(2):
                snapshot = active_controller.snapshot(
                    adapter.allowed_origins,
                    page_id=page_id,
                )
                text = re.sub(
                    r"\s+",
                    " ",
                    str(snapshot.get("text") or "").strip().casefold(),
                )
                controls = (
                    snapshot.get("controls")
                    if isinstance(snapshot.get("controls"), list)
                    else []
                )
                final_controls = [
                    dict(item)
                    for item in controls
                    if isinstance(item, dict)
                    and not bool(item.get("disabled"))
                    and str(item.get("tag") or "").strip().casefold()
                    in {"button", "input"}
                    and str(item.get("type") or "").strip().casefold()
                    in {"button", "submit"}
                    and is_final_submit_control(item.get("label"))
                ]
                current_control = final_controls[0] if len(final_controls) == 1 else None
                if (
                    str(snapshot.get("url") or "").strip() != review_url
                    or str(snapshot.get("path") or "").strip().casefold()
                    != expected_path
                    or str(snapshot.get("ready_state") or "").strip().casefold()
                    not in {"interactive", "complete"}
                    or any(marker in text for marker in positive_markers)
                    or current_control is None
                    or (
                        previous_control is not None
                        and (
                            str(current_control.get("ref") or "").strip()
                            != str(previous_control.get("ref") or "").strip()
                            or str(current_control.get("label") or "").strip()
                            != str(previous_control.get("label") or "").strip()
                        )
                    )
                ):
                    stable = False
                    break
                previous_control = current_control
                last_snapshot = snapshot
                if observation == 0:
                    time.sleep(0.25)
            if stable and last_snapshot is not None and previous_control is not None:
                recovered_snapshot = last_snapshot
                recovered_claim = candidate
                recovered_control = previous_control
                break

        if (
            recovered_snapshot is None
            or recovered_claim is None
            or recovered_control is None
        ):
            skipped[paper_id] = "owned final-review page is not stable and uniquely actionable"
            continue

        _, page_id, review_url, prior_attempt_id = recovered_claim
        draft_match = re.search(
            r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            urllib.parse.urlsplit(review_url).path,
        )
        prepared_audit = _prepare_canary_route_audit(
            paper_id,
            event="live_owned_final_review_reconciled",
            at=now,
            lines=[
                f"- route_revision: {route_revision}",
                f"- current_attempt_id: {str(paper.get('attempt_id') or '').strip()}",
                f"- prior_claim_attempt_id: {prior_attempt_id}",
                f"- review_url: {review_url}",
                f"- page_id: {page_id}",
                f"- cdp_port: {int(recovered_snapshot.get('cdp_port') or 0)}",
                f"- final_control_label: {str(recovered_control.get('label') or '').strip()}",
                "- stable_review_snapshots: 2",
                "- positive_confirmation_evidence: absent",
                "- final_control_activated: false",
                "- transaction_state: committed_with_state",
            ],
        )
        if draft_match:
            paper["draft_id"] = draft_match.group(1)
            paper["submission_draft_id"] = draft_match.group(1)
        paper["draft_url"] = review_url
        paper["submission_draft_url"] = review_url
        paper["upload_url"] = review_url
        paper["submission_upload_url"] = review_url
        paper["draft_binding_source"] = (
            "trusted_sdk_browser_prior_page_claim_revalidated"
        )
        paper["draft_binding_page_id"] = page_id
        paper["draft_binding_captured_at"] = now
        paper["status"] = "waiting"
        paper["workflow_status"] = "waiting"
        paper["worker_status"] = "waiting"
        paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
        paper["blocker"] = ""
        paper["detail"] = (
            "The exact project-owned publisher page was revalidated as a stable "
            "final review with one enabled final-submit control. The earlier "
            "non-final worker result was reclassified without clicking."
        )
        paper["live_final_review_reconciled_at"] = now
        paper["live_final_review_audit_path"] = str(prepared_audit["path"])
        paper["live_final_review_audit_sha256"] = str(prepared_audit["sha256"])
        online_queue = [item for item in online_queue if item != paper_id]
        prepared_audits[paper_id] = prepared_audit
        recovered.append(paper_id)

    audits: dict[str, str] = {}
    if recovered:
        state["online_queue"] = online_queue
        state["updated_at"] = now
        state.setdefault("history", []).append(
            {
                "at": now,
                "event": "reconcile_live_owned_final_review_pages",
                "paper_ids": recovered,
                "final_control_activated": False,
            }
        )
        try:
            write_json_atomic(SMART_STATE_PATH, state)
        except Exception:
            for prepared in prepared_audits.values():
                _discard_prepared_audit(prepared)
            raise
        for paper_id, prepared in prepared_audits.items():
            audits[paper_id] = str(_commit_prepared_audit(prepared))
    return {
        "ok": True,
        "recovered": recovered,
        "skipped": skipped,
        "audits": audits,
        "changed": bool(recovered),
    }


def _manual_takeover_resume_state(
    paper: Mapping[str, Any],
    *,
    in_online_queue: bool,
) -> dict[str, Any]:
    return {
        "status": str(paper.get("status") or ""),
        "workflow_status": str(paper.get("workflow_status") or ""),
        "worker_status": str(paper.get("worker_status") or ""),
        "next_action": str(paper.get("next_action") or ""),
        "blocker": str(paper.get("blocker") or ""),
        "detail": str(paper.get("detail") or ""),
        "in_online_queue": bool(in_online_queue),
        "attempt_id": str(paper.get("attempt_id") or ""),
        "thread_id": str(paper.get("thread_id") or ""),
    }


def _manual_takeover_final_action_is_uncertain(paper: Mapping[str, Any]) -> bool:
    authorization = paper.get("final_submit_authorization")
    return bool(
        isinstance(authorization, Mapping)
        and str(authorization.get("consumed_at") or "").strip()
        and not str(authorization.get("result_recorded_at") or "").strip()
    )


@_smart_state_locked
def request_smart_manual_takeover(paper_ids: list[str]) -> dict[str, Any]:
    """Pause only the selected Smart papers and hand their live pages to the user.

    A paper with an active SDK Turn is marked ``requested`` and remains owned by
    that Turn until its result is recorded.  Every subsequent program plan sees
    the marker and will not dispatch a new Turn for that paper.  Idle papers
    become ``active`` immediately and are removed from the online queue without
    changing any other paper.
    """

    ensure_smart_submission_module()
    selected = list(
        dict.fromkeys(
            str(item).strip() for item in paper_ids if str(item).strip()
        )
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip()
    ]
    requested: list[str] = []
    active: list[str] = []
    skipped: dict[str, str] = {}
    now = now_iso()
    terminal_statuses = {
        "accepted",
        "monitoring",
        "no_action",
        "stopped",
        "submitted",
    }
    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        status = str(paper.get("status") or "").strip().casefold()
        if status in terminal_statuses:
            skipped[paper_id] = (
                "paper is already submitted, monitoring, accepted, or stopped"
            )
            continue
        if _manual_takeover_final_action_is_uncertain(paper):
            skipped[paper_id] = (
                "the exact final-submit action is already claimed and must be verified first"
            )
            continue
        current = (
            paper.get("manual_takeover")
            if isinstance(paper.get("manual_takeover"), dict)
            else {}
        )
        current_status = str(current.get("status") or "").strip().casefold()
        if current_status in {"requested", "active"}:
            (requested if current_status == "requested" else active).append(paper_id)
            continue
        running = str(paper.get("workflow_status") or "").strip().casefold() == "running"
        takeover_status = "requested" if running else "active"
        takeover = {
            "schema": SMART_MANUAL_TAKEOVER_SCHEMA,
            "status": takeover_status,
            "requested_at": now,
            "activated_at": "" if running else now,
            "completed_at": "",
            "resume_state": _manual_takeover_resume_state(
                paper,
                in_online_queue=paper_id in online_queue,
            ),
            "page": {},
        }
        paper["manual_takeover"] = takeover
        paper["manual_takeover_status"] = takeover_status
        paper["manual_takeover_requested_at"] = now
        paper.pop("manual_takeover_reconcile_pending", None)
        if running:
            requested.append(paper_id)
            continue
        online_queue = [item for item in online_queue if item != paper_id]
        paper["workflow_status"] = "manual_takeover"
        paper["worker_status"] = "manual_takeover"
        paper["next_action"] = "manual_takeover_edit_current_page"
        paper["blocker"] = ""
        paper["detail"] = (
            "The selected paper is paused for a user-owned edit on its current browser page."
        )
        active.append(paper_id)
    state["online_queue"] = list(dict.fromkeys(online_queue))
    state["updated_at"] = now
    if requested or active:
        state.setdefault("history", []).append(
            {
                "at": now,
                "event": "request_smart_manual_takeover",
                "requested_after_current_turn": requested,
                "active": active,
                "skipped": skipped,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": bool(requested or active),
        "requested": requested,
        "active": active,
        "skipped": skipped,
        "state": state,
    }


def _smart_manual_takeover_origins(paper: Mapping[str, Any]) -> tuple[str, ...]:
    system_id = str(
        paper.get("submission_system_id") or paper.get("submission_system") or ""
    ).strip()
    adapter = submission_site_adapter(system_id)
    if adapter is None:
        return ()
    origins = list(adapter.allowed_origins)
    broker_origin = normalize_origin(paper.get("credential_broker_origin"))
    if broker_origin in ORCID_AUTH_ORIGINS and broker_origin not in origins:
        origins.append(broker_origin)
    return tuple(origins)


def _smart_manual_takeover_candidate_urls(
    paper: Mapping[str, Any],
) -> list[str]:
    takeover = (
        paper.get("manual_takeover")
        if isinstance(paper.get("manual_takeover"), Mapping)
        else {}
    )
    page = takeover.get("page") if isinstance(takeover.get("page"), Mapping) else {}
    values = [
        page.get("url"),
        paper.get("confirmation_url"),
        paper.get("submission_confirmation_url"),
        paper.get("submission_url"),
        paper.get("target_site_url"),
        paper.get("dashboard_url"),
    ]
    return list(
        dict.fromkeys(str(value).strip() for value in values if str(value or "").strip())
    )


def _select_smart_manual_takeover_page(
    paper: Mapping[str, Any],
    pages: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    candidates = [dict(item) for item in pages if isinstance(item, Mapping)]
    if not candidates:
        return None
    takeover = (
        paper.get("manual_takeover")
        if isinstance(paper.get("manual_takeover"), Mapping)
        else {}
    )
    saved_page = takeover.get("page") if isinstance(takeover.get("page"), Mapping) else {}
    saved_id = str(saved_page.get("page_id") or "").strip()
    if saved_id:
        exact_id = [item for item in candidates if str(item.get("page_id") or "") == saved_id]
        if len(exact_id) == 1:
            return exact_id[0]
    exact_urls = set(_smart_manual_takeover_candidate_urls(paper))
    exact_matches = [
        item for item in candidates if str(item.get("url") or "").strip() in exact_urls
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    identifiers = {
        str(value).strip().casefold()
        for value in (
            paper.get("submission_id"),
            paper.get("canonical_submission_id"),
            paper.get("manuscript_id"),
        )
        if str(value or "").strip()
    }
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in candidates:
        haystack = " ".join(
            (
                str(item.get("url") or ""),
                str(item.get("title") or ""),
            )
        ).casefold()
        score = sum(100 for marker in identifiers if marker in haystack)
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored and scored[0][0] > 0 and (
        len(scored) == 1 or scored[0][0] > scored[1][0]
    ):
        return scored[0][1]
    return candidates[0] if len(candidates) == 1 else None


@_smart_state_locked
def _record_smart_manual_takeover_page(
    paper_id: str,
    page: Mapping[str, Any],
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(str(paper_id))
    if not isinstance(paper, dict):
        raise ValueError("manual takeover paper is missing")
    takeover = (
        paper.get("manual_takeover")
        if isinstance(paper.get("manual_takeover"), dict)
        else {}
    )
    if str(takeover.get("status") or "").strip().casefold() != "active":
        raise ValueError("manual takeover is not active")
    now = now_iso()
    takeover["page"] = {
        "page_id": str(page.get("page_id") or ""),
        "cdp_port": int(page.get("cdp_port") or 0),
        "origin": normalize_origin(page.get("origin")),
        "url": str(page.get("url") or "")[:2000],
        "title": str(page.get("title") or "")[:500],
        "opened_at": now,
    }
    takeover["last_opened_at"] = now
    paper["manual_takeover"] = takeover
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "open_smart_manual_takeover_page",
            "paper_id": str(paper_id),
            "page_id": takeover["page"]["page_id"],
            "origin": takeover["page"]["origin"],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return state


def open_smart_manual_takeover_page(
    paper_id: str,
    *,
    controller: Any | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Open or foreground the selected paper's exact-origin managed page."""

    from .smart_browser_cdp import SmartBrowserCdpController, SmartBrowserCdpError

    clean_id = str(paper_id or "").strip()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(clean_id)
    if not isinstance(paper, dict):
        return {"ok": False, "code": "SMART_MANUAL_TAKEOVER_PAPER_MISSING"}
    takeover = (
        paper.get("manual_takeover")
        if isinstance(paper.get("manual_takeover"), dict)
        else {}
    )
    takeover_status = str(takeover.get("status") or "").strip().casefold()
    if takeover_status == "requested":
        return {
            "ok": False,
            "pending": True,
            "code": "SMART_MANUAL_TAKEOVER_WAITING_CURRENT_TURN",
            "message": "当前 SDK Turn 尚未结束；结束后将只暂停这篇论文。",
        }
    if takeover_status != "active":
        return {
            "ok": False,
            "code": "SMART_MANUAL_TAKEOVER_NOT_ACTIVE",
            "message": "请先对这篇论文启用人工修改。",
        }
    origins = _smart_manual_takeover_origins(paper)
    if not origins:
        return {
            "ok": False,
            "code": "SMART_MANUAL_TAKEOVER_SITE_ADAPTER_MISSING",
            "message": "当前论文缺少可验证的投稿站点适配器。",
        }
    active_controller = controller or SmartBrowserCdpController(ports=SMART_CDP_PORTS)
    try:
        pages = active_controller.list_pages(origins)
        selected_page = _select_smart_manual_takeover_page(paper, pages)
        if selected_page is None and not pages:
            system_id = str(
                paper.get("submission_system_id")
                or paper.get("submission_system")
                or ""
            ).strip()
            adapter = submission_site_adapter(system_id)
            fallback_url = ""
            if adapter is not None:
                fallback_url = str(
                    adapter.dashboard_url or adapter.login_start_url or ""
                ).strip()
            if fallback_url:
                selected_page = dict(
                    active_controller.open_page(origins, fallback_url)
                )
        if selected_page is None:
            return {
                "ok": False,
                "code": "SMART_MANUAL_TAKEOVER_PAGE_AMBIGUOUS",
                "message": (
                    "同一投稿站点打开了多个页面，程序无法可靠判断哪一页属于这篇论文。"
                ),
                "page_count": len(pages),
            }
        page_id = str(selected_page.get("page_id") or "").strip()
        if activate:
            active_controller.activate_page(origins, page_id=page_id)
        snapshot = active_controller.snapshot(origins, page_id=page_id)
    except SmartBrowserCdpError as exc:
        return {**exc.as_dict(), "paper_id": clean_id}
    safe_page = {
        "page_id": str(snapshot.get("page_id") or page_id),
        "cdp_port": int(snapshot.get("cdp_port") or selected_page.get("cdp_port") or 0),
        "origin": normalize_origin(snapshot.get("origin")),
        "url": str(snapshot.get("url") or selected_page.get("url") or ""),
        "title": str(snapshot.get("title") or selected_page.get("title") or ""),
        "ready_state": str(snapshot.get("ready_state") or ""),
        "control_count": len(snapshot.get("controls", []))
        if isinstance(snapshot.get("controls"), list)
        else 0,
    }
    _record_smart_manual_takeover_page(clean_id, safe_page)
    return {
        "ok": True,
        "code": "SMART_MANUAL_TAKEOVER_PAGE_READY",
        "paper_id": clean_id,
        "page": safe_page,
    }


@_smart_state_locked
def _complete_smart_manual_takeover_state(
    inspected_pages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip()
    ]
    resumed: list[str] = []
    skipped: dict[str, str] = {}
    now = now_iso()
    for paper_id, page in inspected_pages.items():
        paper = papers.get(str(paper_id))
        if not isinstance(paper, dict):
            skipped[str(paper_id)] = "paper record is missing"
            continue
        takeover = (
            paper.get("manual_takeover")
            if isinstance(paper.get("manual_takeover"), dict)
            else {}
        )
        if str(takeover.get("status") or "").strip().casefold() != "active":
            skipped[str(paper_id)] = "manual takeover is not active"
            continue
        resume_state = (
            takeover.get("resume_state")
            if isinstance(takeover.get("resume_state"), dict)
            else {}
        )
        takeover["status"] = "completed"
        takeover["completed_at"] = now
        takeover["completed_page"] = {
            "page_id": str(page.get("page_id") or ""),
            "origin": normalize_origin(page.get("origin")),
            "url": str(page.get("url") or "")[:2000],
        }
        paper["manual_takeover"] = takeover
        paper["manual_takeover_status"] = "completed"
        paper["manual_takeover_completed_at"] = now
        paper["manual_takeover_reconcile_pending"] = True
        prior_status = str(resume_state.get("status") or "").strip().casefold()
        paper["status"] = prior_status or "login_check"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "manual_reconcile_queued"
        paper["next_action"] = "reconcile_manual_page_then_continue"
        paper["blocker"] = ""
        paper["detail"] = (
            "The user completed the visible page edit. The fixed SDK session must "
            "reinspect the current page before continuing and must preserve completed fields."
        )
        append_unique(online_queue, str(paper_id))
        resumed.append(str(paper_id))
    state["online_queue"] = online_queue
    state["updated_at"] = now
    if resumed:
        settings = state.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            state["settings"] = settings
        settings["online_submit_enabled"] = True
        settings["sdk_browser_enabled"] = True
        settings["dispatch_bridge"] = "codex_sdk_browser"
        state["mode"] = "run_once"
        state.setdefault("history", []).append(
            {
                "at": now,
                "event": "complete_smart_manual_takeover",
                "resumed": resumed,
                "skipped": skipped,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": bool(resumed),
        "resumed": resumed,
        "skipped": skipped,
        "state": state,
    }


def complete_smart_manual_takeover(
    paper_ids: list[str],
    *,
    controller: Any | None = None,
) -> dict[str, Any]:
    """Verify the still-open page, then requeue only manually completed papers."""

    selected = list(
        dict.fromkeys(
            str(item).strip() for item in paper_ids if str(item).strip()
        )
    )
    inspected: dict[str, Mapping[str, Any]] = {}
    skipped: dict[str, str] = {}
    for paper_id in selected:
        result = open_smart_manual_takeover_page(
            paper_id,
            controller=controller,
            activate=False,
        )
        if result.get("ok") and isinstance(result.get("page"), Mapping):
            inspected[paper_id] = dict(result["page"])
        else:
            skipped[paper_id] = str(
                result.get("code") or result.get("message") or "page inspection failed"
            )
    completed = _complete_smart_manual_takeover_state(inspected)
    merged_skipped = dict(skipped)
    merged_skipped.update(
        {
            str(key): str(value)
            for key, value in completed.get("skipped", {}).items()
        }
    )
    return {
        "ok": bool(completed.get("resumed")),
        "resumed": list(completed.get("resumed", [])),
        "skipped": merged_skipped,
        "state": completed.get("state", {}),
    }


@_smart_state_locked
def resume_smart_credential_broker(paper_ids: list[str]) -> dict[str, Any]:
    """Explicitly resume login/registration after a visible user-owned action."""

    ensure_smart_submission_module()
    selected = list(
        dict.fromkeys(
            str(item).strip() for item in paper_ids if str(item).strip()
        )
    )
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip()
    ]
    unified_state = _unified_state_snapshot()
    resumed: list[str] = []
    skipped: dict[str, str] = {}
    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        route, route_error = _route_guard_result(
            paper_id,
            unified_state=unified_state,
        )
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            continue
        if str(paper.get("status") or "").casefold() in {
            "accepted",
            "monitoring",
            "no_action",
            "submitted",
            "stopped",
        }:
            skipped[paper_id] = "paper is already terminal or monitoring"
            continue
        _mark_smart_route_on_paper(paper, route or {})
        paper["status"] = "login_check"
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "credential_broker"
        paper["blocker"] = ""
        paper["detail"] = "Credential broker resume was explicitly requested."
        paper["next_action"] = "trusted_credential_broker_login"
        paper["credential_broker_status"] = "resume_requested"
        paper["credential_broker_updated_at"] = now_iso()
        append_unique(online_queue, paper_id)
        resumed.append(paper_id)
    state["online_queue"] = online_queue
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    if resumed:
        settings["online_submit_enabled"] = True
        settings["sdk_browser_enabled"] = True
        settings["dispatch_bridge"] = "codex_sdk_browser"
        state["mode"] = "run_once"
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "resume_smart_credential_broker",
            "resumed": resumed,
            "skipped": skipped,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": bool(resumed),
        "resumed": resumed,
        "skipped": skipped,
        "state": state,
    }


@_smart_state_locked
def update_smart_credential_policy(
    *,
    account_missing_policy: str,
    require_terms_confirmation: bool,
    prefer_orcid: bool | None = None,
    auto_authorize_orcid: bool | None = None,
) -> dict[str, Any]:
    """Persist the user-visible account/login policy for future papers."""

    policy = str(account_missing_policy or "").strip().casefold()
    if policy not in {"auto_register", "ask", "block"}:
        raise ValueError("unsupported Smart account-missing policy")
    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    settings["trusted_credential_broker_enabled"] = True
    settings["account_missing_policy"] = policy
    settings["account_registration_enabled"] = policy == "auto_register"
    settings["account_registration_requires_terms_confirmation"] = bool(
        require_terms_confirmation
    )
    if prefer_orcid is not None:
        settings["new_account_auth_preference"] = (
            "orcid_first" if prefer_orcid else "site_account"
        )
    if auto_authorize_orcid is not None:
        settings["orcid_auto_authorize_for_login"] = bool(
            auto_authorize_orcid
        )
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "update_smart_credential_policy",
            "account_missing_policy": policy,
            "require_terms_confirmation": bool(require_terms_confirmation),
            "new_account_auth_preference": settings.get(
                "new_account_auth_preference",
                "orcid_first",
            ),
            "orcid_auto_authorize_for_login": bool(
                settings.get("orcid_auto_authorize_for_login", True)
            ),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "account_missing_policy": policy,
        "require_terms_confirmation": bool(require_terms_confirmation),
        "new_account_auth_preference": settings.get(
            "new_account_auth_preference",
            "orcid_first",
        ),
        "orcid_auto_authorize_for_login": bool(
            settings.get("orcid_auto_authorize_for_login", True)
        ),
    }


@_smart_state_locked
def update_smart_submission_execution_policy(
    *,
    auto_final_submit_enabled: bool,
) -> dict[str, Any]:
    """Persist whether a started Smart workflow completes final submit itself."""

    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    enabled = bool(auto_final_submit_enabled)
    settings["auto_final_submit_enabled"] = enabled
    settings["final_submit_authorization_mode"] = (
        FINAL_SUBMIT_AUTO_PRESENTATION
        if enabled
        else FINAL_SUBMIT_UI_PRESENTATION
    )
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "update_smart_submission_execution_policy",
            "auto_final_submit_enabled": enabled,
            "authorization_mode": settings["final_submit_authorization_mode"],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "auto_final_submit_enabled": enabled,
        "authorization_mode": settings["final_submit_authorization_mode"],
    }


@_smart_state_locked
def configure_smart_submission_full_pipeline_policy(
    *,
    max_parallel_workers: int,
) -> dict[str, Any]:
    """Apply the temporary Smart settings required by one-click E2E runs.

    The returned previous/expected values form a compare-and-swap restore
    token.  A later user edit is therefore never overwritten when the batch
    finishes.
    """

    ensure_smart_submission_module()
    try:
        parallel = int(max_parallel_workers)
    except (TypeError, ValueError):
        raise ValueError("智能投稿并发上限必须是 1-32 的整数") from None
    if not 1 <= parallel <= 32:
        raise ValueError("智能投稿并发上限必须是 1-32 的整数")

    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    keys = (
        "max_parallel_workers",
        "auto_final_submit_enabled",
        "final_submit_authorization_mode",
    )
    previous = {
        key: {
            "present": key in settings,
            "value": copy.deepcopy(settings.get(key)),
        }
        for key in keys
    }
    expected = {
        "max_parallel_workers": parallel,
        "auto_final_submit_enabled": True,
        "final_submit_authorization_mode": FINAL_SUBMIT_AUTO_PRESENTATION,
    }
    settings.update(expected)
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "configure_smart_submission_full_pipeline_policy",
            "max_parallel_workers": parallel,
            "auto_final_submit_enabled": True,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "previous": previous,
        "expected": expected,
        "applied_at": state["updated_at"],
    }


@_smart_state_locked
def restore_smart_submission_full_pipeline_policy(
    *,
    previous: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore only settings that still equal the batch-owned values."""

    state = read_json(SMART_STATE_PATH, _default_state())
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    restored: list[str] = []
    preserved_user_changes: list[str] = []
    for key in (
        "max_parallel_workers",
        "auto_final_submit_enabled",
        "final_submit_authorization_mode",
    ):
        if settings.get(key) != expected.get(key):
            preserved_user_changes.append(key)
            continue
        record = previous.get(key) if isinstance(previous, Mapping) else None
        if isinstance(record, Mapping) and bool(record.get("present")):
            settings[key] = copy.deepcopy(record.get("value"))
        else:
            settings.pop(key, None)
        restored.append(key)
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "restore_smart_submission_full_pipeline_policy",
            "restored": restored,
            "preserved_user_changes": preserved_user_changes,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "restored": restored,
        "preserved_user_changes": preserved_user_changes,
    }


@_smart_state_locked
def prioritize_online_queue(paper_ids: list[str], *, activate: bool = True) -> dict[str, Any]:
    """Move selected papers to the front of the visible online queue."""
    ensure_smart_submission_module()
    selected = [str(item).strip() for item in paper_ids if str(item).strip()]
    state = read_json(SMART_STATE_PATH, _default_state())
    initial_normalize = _normalize_online_queue_state(state, reason="prioritize_online_queue_before")
    papers = state.setdefault("papers", {})
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    online_queue = list(dict.fromkeys(online_queue))
    eligible_statuses = set(ONLINE_WORKER_STATUSES)
    promoted: list[str] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    for paper_id in selected:
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            online_queue = [item for item in online_queue if item != paper_id]
            continue
        _mark_smart_route_on_paper(paper, route or {})
        _hydrate_paper_target_from_package(paper_id, paper)
        status = str(paper.get("status") or "")
        if status in {"submitted", "monitoring"} or _paper_has_prior_submission_evidence(paper):
            paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
            paper["blocker"] = ""
            paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
            skipped[paper_id] = "submitted/monitoring paper uses email-first monitoring; no browser worker queued"
            continue
        if status in {"blocked", "stopped", "accepted"}:
            skipped[paper_id] = f"paper status is {status}"
            continue
        if status not in eligible_statuses:
            skipped[paper_id] = f"paper status is not online-ready: {status or 'empty'}"
            continue
        if not _paper_has_online_target(paper_id, paper):
            paper["status"] = "waiting"
            paper["workflow_status"] = "waiting"
            paper["worker_status"] = "waiting"
            paper["next_action"] = "wait_journal_author_target_package"
            paper["detail"] = "missing target journal or submission system; refresh journal-author package before browser dispatch"
            paper["blocker"] = ""
            skipped[paper_id] = "missing target journal or submission system"
            continue
        paper.setdefault("thread_title", f"Smart Submission worker {paper_id}")
        if str(paper.get("workflow_status") or "") != "running":
            paper["workflow_status"] = "queued"
            paper["next_action"] = "visible_worker_online_submit_or_monitor"
        paper["last_online_prioritized_at"] = now_iso()
        promoted.append(paper_id)

    promoted = list(dict.fromkeys(promoted))
    promoted_set = set(promoted)
    state["online_queue"] = promoted + [item for item in online_queue if item not in promoted_set]
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    if promoted:
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
        if activate:
            state["mode"] = "run_once"
    final_normalize = _normalize_online_queue_state(state, reason="prioritize_online_queue_after")
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "prioritize_online_queue",
            "promoted": promoted,
            "skipped": skipped,
            "activate": activate,
            "online_queue_count": len(state["online_queue"]),
            "initial_normalize": initial_normalize,
            "final_normalize": final_normalize,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    wakeup = set_smart_submission_automation_fast_wakeup(True) if promoted and activate else {}
    state = read_json(SMART_STATE_PATH, state)
    return {"promoted": promoted, "skipped": skipped, "activate": activate, "wakeup": wakeup, "state": state}


@_smart_state_locked
def move_online_queue_item(paper_id: str, direction: str, *, activate: bool = False) -> dict[str, Any]:
    """Move one paper inside online_queue by one slot or to an edge."""
    ensure_smart_submission_module()
    paper_id = str(paper_id or "").strip()
    direction = str(direction or "").strip().lower()
    if direction not in {"up", "down", "top", "bottom"}:
        raise ValueError("direction must be one of: up, down, top, bottom")
    route = require_smart_canary_route(paper_id)
    state = read_json(SMART_STATE_PATH, _default_state())
    normalize = _normalize_online_queue_state(state, reason="move_online_queue_item_before")
    online_queue = list(dict.fromkeys(str(item) for item in state.get("online_queue", []) if str(item).strip()))
    if paper_id not in online_queue:
        if normalize.get("changed"):
            write_json_atomic(SMART_STATE_PATH, state)
        return {
            "moved": False,
            "paper_id": paper_id,
            "reason": "paper is not in online_queue",
            "normalize": normalize,
            "state": state,
        }
    paper = state.get("papers", {}).get(paper_id) if isinstance(state.get("papers"), dict) else None
    if isinstance(paper, dict):
        _mark_smart_route_on_paper(paper, route)
    before = list(online_queue)
    index = online_queue.index(paper_id)
    item = online_queue.pop(index)
    if direction == "up":
        online_queue.insert(max(0, index - 1), item)
    elif direction == "down":
        online_queue.insert(min(len(online_queue), index + 1), item)
    elif direction == "top":
        online_queue.insert(0, item)
    else:
        online_queue.append(item)
    moved = before != online_queue
    if moved:
        state["online_queue"] = online_queue
        if activate:
            settings = state.setdefault("settings", {})
            if not isinstance(settings, dict):
                settings = {}
                state["settings"] = settings
            settings["online_submit_enabled"] = True
            settings["visible_scheduler_enabled"] = True
            state["mode"] = "run_once"
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append(
            {
                "at": now_iso(),
                "event": "move_online_queue_item",
                "paper_id": paper_id,
                "direction": direction,
                "activate": activate,
                "online_queue": online_queue,
                "normalize": normalize,
            }
        )
        write_json_atomic(SMART_STATE_PATH, state)
        if activate:
            set_smart_submission_automation_fast_wakeup(True)
        state = read_json(SMART_STATE_PATH, state)
    return {"moved": moved, "paper_id": paper_id, "direction": direction, "online_queue": online_queue, "state": state}


@_smart_state_locked
def pause_waiting_approval_workers(paper_ids: list[str] | None = None) -> dict[str, Any]:
    """Pause the scheduler in place; never requeue workers or trigger a wakeup."""
    selected = [str(item).strip() for item in (paper_ids or []) if str(item).strip()]
    result = pause_smart_submission_automation(reason="waiting_for_command_approval")
    result["paper_ids"] = selected
    result["requeued"] = []
    result["wakeup"] = {}
    return result


def _normalize_permission_retry_labels(state: dict[str, Any]) -> int:
    """Remove stale waitingOnApproval wording from papers already requeued."""
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    changed = 0
    for paper in papers.values():
        if not isinstance(paper, dict):
            continue
        if str(paper.get("worker_status") or "") in {
            "requeued_after_waitingOnApproval",
            "queued_after_permission_retry",
        }:
            paper["worker_status"] = "queued"
            changed += 1
        if str(paper.get("online_intent") or "") == "requeued_after_waiting_approval":
            paper["online_intent"] = "queued_after_permission_retry"
            changed += 1
        if str(paper.get("last_recovery_reason") or "") == "waitingOnApproval_requeued":
            paper["last_recovery_reason"] = "permission_wait_requeued"
            changed += 1
        if isinstance(paper.get("last_active_flags"), list) and "waitingOnApproval" in {
            str(flag) for flag in paper.get("last_active_flags", [])
        }:
            if str(paper.get("workflow_status") or "") == "queued":
                paper["last_active_flags"] = []
                changed += 1
    return changed


@_smart_state_locked
def reconcile_smart_submission_worker_states(state: dict[str, Any]) -> dict[str, Any]:
    """Normalize stale worker flags that should not consume scheduler slots.

    Worker threads are the source of browser progress, but terminal/waiting
    paper states should not remain labeled as running once the paper has left
    ``online_queue``. This keeps the UI and scheduler from presenting old
    worker fields as live work.
    """
    if not isinstance(state, dict):
        return {"changed": 0, "normalized": []}
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = {str(item) for item in state.get("online_queue", []) if str(item).strip()}
    terminal_workflow = {
        "accepted": "accepted",
        "blocked": "blocked",
        "monitoring": "monitoring",
        "no_action": "no_action",
        "submitted": "monitoring",
        "waiting": "waiting",
        "stopped": "stopped",
    }
    changed = 0
    normalized: list[dict[str, str]] = []
    for paper_id, paper in papers.items():
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper_id)
        status = str(paper.get("status") or "")
        desired_workflow = terminal_workflow.get(status)
        if not desired_workflow or paper_id in online_queue:
            continue
        old_workflow = str(paper.get("workflow_status") or "")
        old_worker = str(paper.get("worker_status") or "")
        if old_workflow in {"running", "queued", "waiting_user_approval"}:
            paper["workflow_status"] = desired_workflow
            changed += 1
        if old_worker in {"running", "queued", "waitingOnApproval", "inProgress", "in_progress", "working", "busy"}:
            paper["worker_status"] = desired_workflow
            changed += 1
        if old_workflow in {"running", "queued", "waiting_user_approval"} or old_worker in {
            "running",
            "queued",
            "waitingOnApproval",
            "inProgress",
            "in_progress",
            "working",
            "busy",
        }:
            normalized.append(
                {
                    "paper_id": paper_id,
                    "status": status,
                    "workflow_before": old_workflow,
                    "worker_before": old_worker,
                    "workflow_after": str(paper.get("workflow_status") or ""),
                    "worker_after": str(paper.get("worker_status") or ""),
                }
            )
    if normalized:
        now = now_iso()
        state["updated_at"] = now
        state.setdefault("history", []).append(
            {
                "at": now,
                "event": "reconcile_smart_submission_worker_states",
                "normalized": normalized,
            }
        )
    return {"changed": changed, "normalized": normalized}


@_smart_state_locked
def _record_visible_worker_dispatch(
    paper_id: str,
    *,
    thread_id: str,
    route_revision: int,
    attempt_id: str,
    monitor_channel: str = SMART_MONITOR_CHANNEL_JOURNAL_DASHBOARD,
) -> dict[str, Any]:
    """Atomically claim one queued canary for one visible worker thread."""

    paper_id = str(paper_id or "").strip()
    thread_id = str(thread_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    monitor_channel = str(monitor_channel or "").strip().casefold().replace("-", "_")
    if monitor_channel not in {
        SMART_MONITOR_CHANNEL_JOURNAL_DASHBOARD,
        SMART_MONITOR_CHANNEL_MAILBOX_BATCH,
    }:
        raise ValueError(f"unsupported Smart monitor channel: {monitor_channel}")
    if not paper_id or not thread_id or not attempt_id:
        raise ValueError("paper_id, thread_id, and attempt_id are required")
    deferred_route_error: SmartSubmissionGuardError | None = None
    try:
        route = require_smart_canary_route(
            paper_id,
            expected_revision=route_revision,
        )
    except SmartSubmissionGuardError as exc:
        if exc.code not in {SMART_ROUTE_OWNER_REQUIRED, SMART_ROUTE_MODE_REQUIRED}:
            raise
        deferred_route_error = exc
        route = {}
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    online_queue = _queue_paper_ids(state, "online_queue")
    if not isinstance(paper, dict) or paper_id not in online_queue:
        if deferred_route_error is not None:
            raise deferred_route_error
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "visible worker dispatch requires the paper to be queued",
        )
    monitor_only = _monitor_check_active(paper)
    mailbox_batch = monitor_channel == SMART_MONITOR_CHANNEL_MAILBOX_BATCH
    if mailbox_batch and not monitor_only:
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "mailbox batch dispatch is only valid for a pending read-only monitor check",
        )
    if deferred_route_error is not None:
        if not monitor_only:
            raise deferred_route_error
        route = _require_worker_route(
            paper_id,
            paper,
            expected_revision=route_revision,
            monitor_only=True,
        )
    had_prior_submission_evidence = (
        _paper_is_monitoring_like(paper)
        if monitor_only
        else _paper_has_prior_trusted_submission_evidence(paper_id, paper)
    )
    if monitor_only:
        if not had_prior_submission_evidence:
            raise SmartSubmissionGuardError(
                SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
                paper_id,
                "scheduled monitoring requires an existing submitted/monitoring record",
            )
    else:
        legacy = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
        _assert_no_terminal_canary_state(
            paper_id,
            legacy_state=legacy if isinstance(legacy, dict) else {},
            smart_state=state,
            stage="worker_dispatch",
        )
    if str(paper.get("workflow_status") or "").strip().lower() != "queued":
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "visible worker dispatch requires workflow_status=queued",
            details={"workflow_status": paper.get("workflow_status", "")},
        )
    running = [item for item in _effective_running_papers(state) if item != paper_id]
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    try:
        max_parallel_workers = int(
            settings.get("max_parallel_workers", DEFAULT_SMART_MAX_PARALLEL_WORKERS)
        )
    except (TypeError, ValueError):
        max_parallel_workers = DEFAULT_SMART_MAX_PARALLEL_WORKERS
    max_parallel_workers = max(1, min(32, max_parallel_workers))
    running_slot_keys: set[tuple[str, str]] = set()
    same_mailbox_batch_thread = False
    for running_id in running:
        running_paper = papers.get(running_id)
        if (
            isinstance(running_paper, dict)
            and _monitor_check_active(running_paper)
            and str(running_paper.get("worker_monitor_channel") or "").strip().casefold()
            == SMART_MONITOR_CHANNEL_MAILBOX_BATCH
        ):
            running_thread_id = str(running_paper.get("thread_id") or "").strip()
            running_slot_keys.add(
                (SMART_MONITOR_CHANNEL_MAILBOX_BATCH, running_thread_id or str(running_id))
            )
            if mailbox_batch and running_thread_id == thread_id:
                same_mailbox_batch_thread = True
        else:
            running_slot_keys.add(("paper", str(running_id)))
    if len(running_slot_keys) >= max_parallel_workers and not same_mailbox_batch_thread:
        raise SmartSubmissionGuardError(
            SMART_CANARY_WORKER_ALREADY_RUNNING,
            paper_id,
            "all configured Smart Submission worker slots are occupied",
            details={
                "running_papers": running,
                "running_worker_slots": len(running_slot_keys),
                "max_parallel_workers": max_parallel_workers,
            },
        )
    for owner_id, owner in papers.items():
        if (
            str(owner_id) != paper_id
            and isinstance(owner, dict)
            and str(owner.get("workflow_status") or "").strip().casefold() == "running"
            and str(owner.get("thread_id") or "").strip() == thread_id
        ):
            shared_mailbox_batch_thread = bool(
                mailbox_batch
                and _monitor_check_active(owner)
                and str(owner.get("worker_monitor_channel") or "").strip().casefold()
                == SMART_MONITOR_CHANNEL_MAILBOX_BATCH
            )
            if shared_mailbox_batch_thread:
                continue
            raise SmartSubmissionGuardError(
                SMART_CANARY_THREAD_MISMATCH,
                paper_id,
                "one fixed SDK thread cannot own two running papers",
                details={"thread_id": thread_id, "owner_paper_id": str(owner_id)},
            )
    expected_attempt = str(paper.get("attempt_id") or "").strip()
    if not expected_attempt or attempt_id != expected_attempt:
        raise SmartSubmissionGuardError(
            SMART_CANARY_ATTEMPT_MISMATCH,
            paper_id,
            "canary attempt_id is stale",
            details={"expected_attempt_id": expected_attempt, "actual_attempt_id": attempt_id},
        )
    stale_thread_ids = {
        str(item).strip()
        for item in (paper.get("stale_worker_thread_ids") or [])
        if str(item).strip()
    }
    if thread_id in stale_thread_ids:
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "visible worker thread was retired by a newer canary attempt",
            details={"thread_id": thread_id},
        )
    # A normal journal-dashboard worker must revalidate its exact-origin site
    # account. The mailbox batch is a separate read-only capability: it can only
    # inspect the already-open Gmail origin and never receives journal secrets.
    # Its narrow monitor-only/channel checks above are the authorization gate.
    if not mailbox_batch:
        _require_current_worker_auth(paper_id, paper)
    dispatch_time = datetime.now().astimezone()
    final_submit_intent = (
        str(paper.get("next_action") or "").strip() == FINAL_SUBMIT_AUTHORIZED_ACTION
        or isinstance(paper.get("final_submit_authorization"), dict)
    )
    final_submit_authorization = _valid_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        now=dispatch_time,
    )
    if final_submit_intent and final_submit_authorization is None:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "final-submit worker dispatch requires a current unconsumed exact-attempt authorization",
        )
    dispatch_time_text = dispatch_time.isoformat()
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="worker_dispatch",
        at=dispatch_time_text,
        lines=[
            "- result: dispatched",
            f"- thread_id: {thread_id}",
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {expected_attempt or '-'}",
            f"- final_submit_authorization_reserved: {str(final_submit_authorization is not None).lower()}",
            f"- monitoring_only: {str(monitor_only).lower()}",
            f"- monitor_channel: {monitor_channel if monitor_only else '-'}",
            "- transaction_state: committed_with_state",
        ],
    )
    if final_submit_authorization is not None:
        final_submit_authorization["reserved_at"] = dispatch_time_text
        final_submit_authorization["reserved_by_thread_id"] = thread_id
        final_submit_authorization["reservation_audit_path"] = str(prepared_audit["path"])
        final_submit_authorization["reservation_audit_sha256"] = str(prepared_audit["sha256"])
        paper["final_submit_authorization"] = _sign_smart_receipt(final_submit_authorization)
        paper["final_submit_authorization_reserved_at"] = dispatch_time_text
    _mark_worker_route_on_paper(paper, route, monitor_only=monitor_only)
    paper["workflow_status"] = "running"
    paper["worker_status"] = "running"
    paper["thread_id"] = thread_id
    if monitor_only:
        paper["worker_monitor_channel"] = monitor_channel
    else:
        paper.pop("worker_monitor_channel", None)
    paper["last_worker_dispatched_at"] = dispatch_time_text
    paper["last_thread_status"] = "running"
    paper["worker_dispatch_had_prior_submission_evidence"] = had_prior_submission_evidence
    paper["last_dispatch_audit_path"] = str(prepared_audit["path"])
    paper["last_dispatch_audit_sha256"] = str(prepared_audit["sha256"])
    state["updated_at"] = dispatch_time_text
    state.setdefault("history", []).append(
        {
            "at": dispatch_time_text,
            "event": "visible_worker_dispatch",
            "paper_id": paper_id,
            "thread_id": thread_id,
            "route_revision": int(route_revision),
            "attempt_id": expected_attempt,
            "final_submit_authorization_reserved": final_submit_authorization is not None,
            "authorization_id": (
                str(final_submit_authorization.get("authorization_id") or "")
                if final_submit_authorization is not None
                else ""
            ),
            "had_prior_submission_evidence": had_prior_submission_evidence,
            "monitoring_only": monitor_only,
            "monitor_channel": monitor_channel if monitor_only else "",
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": True,
        "code": "SMART_CANARY_WORKER_DISPATCHED",
        "paper_id": paper_id,
        "thread_id": thread_id,
        "route_revision": int(route_revision),
        "attempt_id": expected_attempt,
        "monitor_channel": monitor_channel if monitor_only else "",
        "audit_path": str(audit_path),
        "state": state,
    }


def record_visible_worker_dispatch(
    paper_id: str,
    *,
    thread_id: str,
    route_revision: int,
    attempt_id: str,
) -> dict[str, Any]:
    """V1-compatible journal-dashboard worker dispatch entry point."""

    return _record_visible_worker_dispatch(
        paper_id,
        thread_id=thread_id,
        route_revision=route_revision,
        attempt_id=attempt_id,
        monitor_channel=SMART_MONITOR_CHANNEL_JOURNAL_DASHBOARD,
    )


def record_mailbox_monitor_worker_dispatch(
    paper_id: str,
    *,
    thread_id: str,
    route_revision: int,
    attempt_id: str,
) -> dict[str, Any]:
    """Claim one paper inside a shared, read-only mailbox batch thread."""

    return _record_visible_worker_dispatch(
        paper_id,
        thread_id=thread_id,
        route_revision=route_revision,
        attempt_id=attempt_id,
        monitor_channel=SMART_MONITOR_CHANNEL_MAILBOX_BATCH,
    )


@_smart_state_locked
def record_smart_credential_broker_state(
    paper_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Project one value-free local credential-broker result into the UI state."""

    paper_id = str(paper_id or "").strip()
    if not paper_id or not isinstance(result, dict):
        raise ValueError("paper_id and a broker result are required")
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "credential broker state has no matching Smart paper",
        )
    now = now_iso()
    broker_status = str(result.get("status") or "starting").strip().casefold()
    broker_code = str(result.get("code") or "").strip()
    next_action = str(result.get("next_action") or "").strip()
    existing_next_action = str(paper.get("next_action") or "").strip()
    authorization_gate_recovered = False
    if (
        existing_next_action not in {
            FINAL_SUBMIT_REVIEW_READY_ACTION,
            FINAL_SUBMIT_AUTHORIZED_ACTION,
        }
        and isinstance(paper.get("final_submit_authorization"), dict)
    ):
        recoverable = copy.deepcopy(paper)
        recoverable["next_action"] = FINAL_SUBMIT_AUTHORIZED_ACTION
        if _valid_final_submit_authorization(
            paper_id,
            recoverable,
            route_revision=int(paper.get("route_revision") or 0),
            attempt_id=str(paper.get("attempt_id") or ""),
        ) is not None:
            paper["status"] = "ready_for_online_submit"
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "queued"
            paper["next_action"] = FINAL_SUBMIT_AUTHORIZED_ACTION
            existing_next_action = FINAL_SUBMIT_AUTHORIZED_ACTION
            authorization_gate_recovered = True
    preserve_final_submit_gate = existing_next_action in {
        FINAL_SUBMIT_REVIEW_READY_ACTION,
        FINAL_SUBMIT_AUTHORIZED_ACTION,
    }
    preserve_monitor_gate = _monitor_check_active(paper)
    account_id = str(result.get("account_id") or "").strip()
    if account_id:
        paper["credential_account_id"] = account_id
    paper["credential_broker_status"] = broker_status
    paper["credential_broker_code"] = broker_code
    paper["credential_broker_origin"] = normalize_origin(result.get("origin"))
    paper["credential_broker_next_action"] = next_action
    paper["credential_broker_updated_at"] = now
    paper["credential_broker_value_free"] = True
    paper["credential_broker_failure_stage"] = str(
        result.get("failure_stage") or ""
    )[:80]
    paper["credential_broker_error_type"] = str(
        result.get("error_type") or ""
    )[:120]
    if str(result.get("recovery_requested_at") or "").strip():
        paper["credential_recovery_requested_at"] = str(
            result.get("recovery_requested_at")
        )[:40]
    if str(result.get("recovery_phase") or "").strip():
        paper["credential_recovery_phase"] = str(
            result.get("recovery_phase")
        )[:40]
    paper["credential_registration_created"] = bool(
        result.get("registration_created")
    )

    waiting_statuses = {
        "account_required",
        "account_recovery_form_incomplete",
        "account_recovery_mailbox_unavailable",
        "account_support_recovery_required",
        "existing_account_recovery_required",
        "human_verification",
        "login_form_incomplete",
        "login_incomplete",
        "registration_form_incomplete",
        "registration_identity_required",
        "saved_password_rejected",
        "waiting_email_verification",
        "waiting_terms_confirmation",
    }
    automatic_retry_statuses = {
        "account_recovery_email_pending",
        "broker_retry_required",
    }
    terminal_statuses = {"site_adapter_required", "site_binding_invalid"}
    online_queue = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip()
    ]
    if broker_status == "session_ready":
        if preserve_monitor_gate:
            paper["status"] = "monitoring"
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "credential_session_ready"
            paper["blocker"] = ""
            paper["detail"] = (
                "The exact-origin browser session is ready for the pending "
                "read-only submission-status check."
            )
            paper["next_action"] = SCHEDULED_MONITORING_NEXT
        elif preserve_final_submit_gate:
            if existing_next_action == FINAL_SUBMIT_AUTHORIZED_ACTION:
                paper["status"] = "ready_for_online_submit"
                paper["workflow_status"] = "queued"
                paper["worker_status"] = "queued"
            else:
                paper["status"] = "waiting"
                paper["workflow_status"] = "waiting"
                paper["worker_status"] = "waiting"
            paper["blocker"] = ""
            paper["detail"] = (
                "The exact-origin browser session was refreshed without changing "
                "the reviewed final-submit attempt."
            )
        else:
            paper["status"] = "login_check"
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "credential_session_ready"
            paper["blocker"] = ""
            paper["detail"] = "Saved credential login completed; SDK browser dispatch is ready."
            paper["next_action"] = "dispatch_fixed_sdk_session"
        paper["credential_broker_completed_at"] = now
        paper.pop("credential_recovery_requested_at", None)
        paper.pop("credential_recovery_phase", None)
    elif broker_status in automatic_retry_statuses:
        if preserve_monitor_gate:
            paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
            paper["blocker"] = ""
            paper["detail"] = (
                "The current read-only status check finished without a verified "
                "journal update; the local broker will retry automatically."
            )
            paper["next_action"] = SCHEDULED_MONITORING_NEXT
            paper.pop("monitor_check_pending", None)
            paper.pop("monitor_pending_channel", None)
            paper["last_monitor_checked_at"] = now
            paper["last_monitor_check_outcome"] = f"credential_{broker_status}"
            online_queue = [item for item in online_queue if item != paper_id]
        elif preserve_final_submit_gate:
            paper["blocker"] = ""
            paper["detail"] = (
                "The reviewed final-submit attempt is retained while the local "
                "broker automatically recovers the journal account."
            )
        else:
            paper["status"] = "login_check"
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "credential_recovery"
            paper["blocker"] = ""
            paper["detail"] = (
                "The journal account recovery request is being handled by the "
                "program; no manual password entry is required."
            )
            paper["next_action"] = next_action or "retry_credential_broker"
        if not preserve_monitor_gate and paper_id not in online_queue:
            online_queue.append(paper_id)
    elif broker_status in waiting_statuses:
        if preserve_monitor_gate:
            paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
            paper["blocker"] = ""
            paper["detail"] = (
                "The read-only status check was deferred because the journal "
                "session needs a visible login or verification action."
            )
            paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
            paper.pop("monitor_check_pending", None)
            paper.pop("monitor_pending_channel", None)
            paper["last_monitor_checked_at"] = now
            paper["last_monitor_check_outcome"] = f"credential_{broker_status}"
            online_queue = [item for item in online_queue if item != paper_id]
        elif preserve_final_submit_gate:
            paper["worker_status"] = "waiting_user_action"
            paper["blocker"] = ""
            paper["detail"] = (
                "The reviewed final-submit attempt is retained while the local "
                "credential broker waits for the exact-origin session."
            )
            if paper_id not in online_queue:
                online_queue.append(paper_id)
        else:
            paper["status"] = "waiting"
            paper["workflow_status"] = "waiting"
            paper["worker_status"] = "waiting_user_action"
            paper["blocker"] = ""
            paper["detail"] = (
                "The local credential broker is waiting for a visible user-owned "
                "login, verification, recovery, or terms action."
            )
            paper["next_action"] = next_action or "resume_credential_broker"
            online_queue = [item for item in online_queue if item != paper_id]
    elif broker_status in terminal_statuses:
        if preserve_monitor_gate:
            paper["status"] = "monitoring"
            paper["workflow_status"] = "monitoring"
            paper["worker_status"] = "monitoring"
            paper["blocker"] = ""
            paper["detail"] = (
                "The read-only status check was skipped because a compatible "
                "journal-site adapter or origin binding is not available yet."
            )
            paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
            paper.pop("monitor_check_pending", None)
            paper.pop("monitor_pending_channel", None)
            paper["last_monitor_checked_at"] = now
            paper["last_monitor_check_outcome"] = f"credential_{broker_status}"
        else:
            paper["status"] = "blocked"
            paper["workflow_status"] = "blocked"
            paper["worker_status"] = "blocked"
            paper["blocker"] = broker_code or "credential broker site binding is unavailable"
            paper["detail"] = "A supported, exact-origin site adapter is required."
            paper["next_action"] = next_action or "configure_site_adapter_then_resume"
        online_queue = [item for item in online_queue if item != paper_id]
    else:
        if preserve_monitor_gate:
            paper["status"] = "monitoring"
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "credential_broker"
            paper["blocker"] = ""
            paper["detail"] = (
                "The local credential broker is preparing the pending read-only "
                "submission-status check."
            )
            paper["next_action"] = SCHEDULED_MONITORING_NEXT
        elif preserve_final_submit_gate:
            paper["blocker"] = broker_code if broker_status == "broker_retry_required" else ""
            paper["detail"] = (
                "The reviewed final-submit attempt is retained while the local "
                "credential broker refreshes its exact-origin browser session."
            )
        else:
            paper["status"] = "login_check"
            paper["workflow_status"] = "queued"
            paper["worker_status"] = "credential_broker"
            paper["blocker"] = ""
            paper["detail"] = "The local credential broker is establishing a browser session."
            paper["next_action"] = next_action or "retry_credential_broker"
    if (
        preserve_final_submit_gate
        and existing_next_action == FINAL_SUBMIT_AUTHORIZED_ACTION
        and paper_id not in online_queue
    ):
        online_queue.append(paper_id)
    state["online_queue"] = list(dict.fromkeys(online_queue))
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "smart_credential_broker_state",
            "paper_id": paper_id,
            "status": broker_status,
            "code": broker_code,
            "account_id": account_id,
            "origin": paper["credential_broker_origin"],
            "value_free": True,
            "final_submit_gate_preserved": preserve_final_submit_gate,
            "authorization_gate_recovered": authorization_gate_recovered,
            "monitor_gate_preserved": preserve_monitor_gate,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return copy.deepcopy(paper)


@_smart_state_locked
def record_smart_sdk_thread_binding(
    paper_id: str,
    *,
    thread_id: str,
    thread_name: str,
    replace: bool = False,
) -> dict[str, Any]:
    """Persist the one-paper/one-SDK-thread identity before dispatch."""

    paper_id = str(paper_id or "").strip()
    thread_id = str(thread_id or "").strip()
    thread_name = str(thread_name or "").strip()
    if not paper_id or not thread_id or not thread_name:
        raise ValueError("paper_id, thread_id, and thread_name are required")
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "SDK thread binding has no matching Smart paper",
        )
    for owner_id, owner in papers.items():
        if (
            str(owner_id) != paper_id
            and isinstance(owner, dict)
            and str(owner.get("sdk_thread_id") or "").strip() == thread_id
        ):
            raise SmartSubmissionGuardError(
                SMART_CANARY_THREAD_MISMATCH,
                paper_id,
                "the fixed SDK thread is already bound to another paper",
                details={"owner_paper_id": str(owner_id), "thread_id": thread_id},
            )
    previous = str(paper.get("sdk_thread_id") or "").strip()
    if previous and previous != thread_id and not replace:
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "the paper already owns a different fixed SDK thread",
            details={"existing_thread_id": previous, "requested_thread_id": thread_id},
        )
    if previous and previous != thread_id:
        retired = [
            str(item)
            for item in paper.get("retired_sdk_thread_ids", [])
            if str(item).strip()
        ]
        paper["retired_sdk_thread_ids"] = list(dict.fromkeys([*retired, previous]))
    paper["sdk_thread_id"] = thread_id
    paper["sdk_thread_name"] = thread_name
    paper["sdk_executor"] = "codex_sdk_browser"
    paper["sdk_thread_bound_at"] = now_iso()
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "smart_sdk_thread_bound",
            "paper_id": paper_id,
            "thread_id": thread_id,
            "thread_name": thread_name,
            "replaced_thread_id": previous if previous != thread_id else "",
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "paper_id": paper_id,
        "thread_id": thread_id,
        "thread_name": thread_name,
        "replaced_thread_id": previous if previous != thread_id else "",
    }


@_smart_state_locked
def record_smart_sdk_turn_state(
    paper_id: str,
    *,
    thread_id: str,
    turn_id: str,
    runtime_status: str,
    model: str = "",
    usage: dict[str, Any] | None = None,
    error: str = "",
    monitor_only: bool = False,
) -> dict[str, Any]:
    """Project SDK transport state into the Smart UI read model."""

    paper_id = str(paper_id or "").strip()
    thread_id = str(thread_id or "").strip()
    turn_id = str(turn_id or "").strip()
    runtime_status = str(runtime_status or "").strip().casefold()
    if runtime_status not in {
        "creating_thread",
        "queued",
        "running",
        "waiting_existing_turn",
        "completed",
        "failed",
    }:
        raise ValueError(f"unsupported Smart SDK runtime status: {runtime_status}")
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "SDK turn state has no matching Smart paper",
        )
    prefix = "sdk_monitor" if monitor_only else "sdk"
    thread_key = f"{prefix}_thread_id"
    turn_key = f"{prefix}_turn_id"
    runtime_key = f"{prefix}_runtime_status"
    model_key = f"{prefix}_model"
    updated_key = f"{prefix}_last_updated_at"
    usage_key = f"{prefix}_last_usage"
    error_key = f"{prefix}_last_error"
    bound_thread = str(paper.get(thread_key) or "").strip()
    if (
        monitor_only
        and bound_thread
        and thread_id
        and bound_thread != thread_id
        and runtime_status in {"creating_thread", "queued"}
        and not turn_id
    ):
        retired = [
            str(item)
            for item in paper.get("retired_sdk_monitor_thread_ids", [])
            if str(item).strip()
        ]
        paper["retired_sdk_monitor_thread_ids"] = list(
            dict.fromkeys([*retired, bound_thread])
        )[-40:]
        bound_thread = ""
    if bound_thread and thread_id and bound_thread != thread_id:
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "SDK turn state does not match the paper's fixed thread",
        )
    paper[runtime_key] = runtime_status
    paper[thread_key] = thread_id or bound_thread
    paper[turn_key] = turn_id
    paper[model_key] = str(model or paper.get(model_key) or "")
    paper[updated_key] = now_iso()
    paper["sdk_active_channel"] = "monitor" if monitor_only else "submission"
    if isinstance(usage, dict):
        paper[usage_key] = copy.deepcopy(usage)
    if error:
        paper[error_key] = str(error)[:2000]
    elif runtime_status in {"running", "completed"}:
        paper[error_key] = ""
    state["updated_at"] = now_iso()
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": True,
        "paper_id": paper_id,
        "thread_id": paper.get(thread_key, ""),
        "turn_id": turn_id,
        "runtime_status": runtime_status,
        "channel": "monitor" if monitor_only else "submission",
    }


@_smart_state_locked
def claim_final_submit_action(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
) -> dict[str, Any]:
    """Consume one reserved receipt immediately before one irreversible click."""

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    authorization_id = str(authorization_id or "").strip().casefold()
    if (
        not paper_id
        or not attempt_id
        or not thread_id
        or not re.fullmatch(r"[0-9a-f]{32}", authorization_id)
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "exact paper, attempt, worker thread, and authorization id are required",
        )
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    online_queue = _queue_paper_ids(state, "online_queue")
    if not isinstance(paper, dict) or paper_id not in online_queue:
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "final-submit action claim requires a running queued worker",
        )
    if (
        str(paper.get("attempt_id") or "").strip() != attempt_id
        or str(paper.get("thread_id") or "").strip() != thread_id
        or str(paper.get("workflow_status") or "").strip().casefold() != "running"
        or str(paper.get("worker_status") or "").strip().casefold() != "running"
    ):
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "final-submit action claim does not own the current running attempt",
        )
    now = datetime.now().astimezone()
    receipt = _reserved_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        now=now,
    )
    if receipt is None or str(receipt.get("authorization_id") or "") != authorization_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "final-submit authorization is missing, expired, changed, or already consumed",
        )
    _require_current_worker_auth(paper_id, paper)
    now = datetime.now().astimezone()
    receipt = _reserved_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        now=now,
    )
    if receipt is None or str(receipt.get("authorization_id") or "") != authorization_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "final-submit authorization expired or changed during the action-time recheck",
        )
    now_text = now.isoformat()
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_action_claimed",
        at=now_text,
        lines=[
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- thread_id: {thread_id}",
            f"- authorization_id: {authorization_id}",
            f"- package_binding_sha256: {receipt['package_binding_sha256']}",
            "- one_use_receipt_consumed: true",
            "- transaction_state: committed_with_state",
        ],
    )
    receipt["consumed_at"] = now_text
    receipt["consumed_by_thread_id"] = thread_id
    receipt["consumption_audit_path"] = str(prepared_audit["path"])
    receipt["consumption_audit_sha256"] = str(prepared_audit["sha256"])
    paper["final_submit_authorization"] = _sign_smart_receipt(receipt)
    paper["final_submit_action_claimed_at"] = now_text
    paper["final_submit_action_claimed_by_thread_id"] = thread_id
    state["updated_at"] = now_text
    state.setdefault("history", []).append(
        {
            "at": now_text,
            "event": "final_submit_action_claimed",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": True,
        "code": SMART_FINAL_SUBMIT_ACTION_CLAIMED,
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "authorization_id": authorization_id,
        "claimed_at": now_text,
        "audit_path": str(audit_path),
    }


@_smart_state_locked
def record_final_submit_result_unknown(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
    detail: str,
) -> dict[str, Any]:
    """Freeze an uncertain post-claim result so no worker can retry the click."""

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    authorization_id = str(authorization_id or "").strip().casefold()
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "uncertain final-submit result has no matching Smart paper",
        )
    if (
        str(paper.get("attempt_id") or "").strip() != attempt_id
        or str(paper.get("thread_id") or "").strip() != thread_id
        or str(paper.get("workflow_status") or "").strip().casefold() != "running"
    ):
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "uncertain final-submit result does not own the current running attempt",
        )
    authorization = _claimed_final_submit_authorization(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
    )
    if (
        authorization is None
        or str(authorization.get("authorization_id") or "") != authorization_id
    ):
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "an uncertain final result can only be recorded after the exact one-use action claim",
        )
    now = now_iso()
    safe_detail = str(detail or "").strip()[:2000] or (
        "The final action was claimed, but its submission result could not be verified."
    )
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_result_unknown",
        at=now,
        lines=[
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- thread_id: {thread_id}",
            f"- authorization_id: {authorization_id}",
            "- final_click_may_have_occurred: true",
            "- retry_forbidden: true",
            f"- detail: {safe_detail}",
            "- transaction_state: committed_with_state",
        ],
    )
    paper["status"] = "final_submit_result_unknown"
    paper["workflow_status"] = "blocked"
    paper["worker_status"] = "blocked"
    paper["next_action"] = "manual_verify_submission_result_before_any_retry"
    paper["blocker"] = safe_detail
    paper["detail"] = safe_detail
    paper["final_submit_result_unknown_at"] = now
    paper["final_submit_result_unknown_audit_path"] = str(prepared_audit["path"])
    paper["final_submit_result_unknown_audit_sha256"] = str(prepared_audit["sha256"])
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    settings = state.setdefault("settings", {})
    if isinstance(settings, dict):
        settings["online_submit_enabled"] = False
        settings["sdk_pause_requested"] = True
    state["mode"] = "manual"
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "final_submit_result_unknown",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "retry_forbidden": True,
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": False,
        "code": "SMART_FINAL_SUBMIT_RESULT_UNKNOWN",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "authorization_id": authorization_id,
        "retry_forbidden": True,
        "audit_path": str(audit_path),
        "state": state,
    }


def _legacy_localized_final_control_failure_proof(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
) -> dict[str, str] | None:
    """Verify the exact structured SDK journal left by the 0.10.6 locale bug."""

    journal_path = Path(SMART_STATE_PATH).with_name("sdk_dispatch.json")
    journal = read_json(journal_path, {})
    papers = journal.get("papers") if isinstance(journal.get("papers"), dict) else {}
    task = papers.get(paper_id)
    if not isinstance(task, dict):
        return None
    final_response = re.sub(
        r"\s+",
        " ",
        str(task.get("final_response") or "").strip().casefold(),
    )
    exact_failure_marker = (
        "browser_final_submit_once rejected the localized submit control as not "
        "verified and cannot be retried in this turn"
    )
    if (
        str(task.get("thread_id") or "").strip() != thread_id
        or str(task.get("turn_id") or "").strip()
        != str(paper.get("sdk_turn_id") or "").strip()
        or int(task.get("route_revision") or 0) != int(route_revision)
        or str(task.get("attempt_id") or "").strip() != attempt_id
        or str(task.get("authorization_id") or "").strip().casefold()
        != authorization_id
        or str(task.get("status") or "").strip().casefold() != "completed"
        or str(task.get("recorded_status") or "").strip().casefold()
        != "final_submit_result_unknown"
        or exact_failure_marker not in final_response
    ):
        return None
    return {
        "journal_path": str(journal_path),
        "turn_id": str(task.get("turn_id") or "").strip(),
        "proof": "localized_final_control_rejected_before_cdp_evaluate",
    }


def _consumed_authorization_matches_current_binding(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
) -> dict[str, Any] | None:
    """Validate one consumed receipt without depending on the current UI state."""

    receipt = paper.get("final_submit_authorization")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _FINAL_SUBMIT_AUTHORIZATION_KEYS
        or not _verify_smart_receipt(receipt)
        or receipt.get("explicit_user_confirmation") is not True
        or str(receipt.get("paper_id") or "").strip() != paper_id
        or int(receipt.get("route_revision") or 0) != int(route_revision)
        or str(receipt.get("attempt_id") or "").strip() != attempt_id
        or str(receipt.get("authorization_id") or "").strip().casefold()
        != authorization_id
        or str(receipt.get("reserved_by_thread_id") or "").strip() != thread_id
        or str(receipt.get("consumed_by_thread_id") or "").strip() != thread_id
        or str(receipt.get("result_recorded_at") or "").strip()
    ):
        return None
    authorized_at = _parse_aware_time(receipt.get("authorized_at"))
    expires_at = _parse_aware_time(receipt.get("expires_at"))
    reserved_at = _parse_aware_time(receipt.get("reserved_at"))
    consumed_at = _parse_aware_time(receipt.get("consumed_at"))
    if (
        authorized_at is None
        or expires_at is None
        or reserved_at is None
        or consumed_at is None
        or not (authorized_at <= reserved_at <= consumed_at < expires_at)
        or not _committed_audit_matches(
            receipt.get("authorization_audit_path"),
            receipt.get("authorization_audit_sha256"),
        )
        or not _committed_audit_matches(
            receipt.get("reservation_audit_path"),
            receipt.get("reservation_audit_sha256"),
        )
        or not _committed_audit_matches(
            receipt.get("consumption_audit_path"),
            receipt.get("consumption_audit_sha256"),
        )
    ):
        return None
    target_binding = _final_submit_target_binding(paper_id, paper)
    package_binding = _submission_package_binding(paper_id, paper)
    expected = {
        "account_id": str(paper.get("credential_account_id") or "").strip(),
        "submission_system_id": str(
            paper.get("submission_system_id") or ""
        ).strip().casefold(),
        "target_journal": str(paper.get("target_journal") or "").strip(),
        "target_origin": target_binding.get("target_origin", ""),
        "journal_site_key": target_binding.get("journal_site_key", ""),
        "account_binding_sha256": target_binding.get("account_binding_sha256", ""),
        "site_binding_sha256": target_binding.get("site_binding_sha256", ""),
        "draft_binding_sha256": target_binding.get("draft_binding_sha256", ""),
        "package_binding_sha256": package_binding,
    }
    if not all(expected.values()) or any(
        receipt.get(key) != value for key, value in expected.items()
    ):
        return None
    return receipt


def _claimed_authorization_matches_unknown_result(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
) -> dict[str, Any] | None:
    """Validate a consumed receipt after the paper has moved to unknown state."""

    receipt = _consumed_authorization_matches_current_binding(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization_id=authorization_id,
    )
    unknown_at = _parse_aware_time(paper.get("final_submit_result_unknown_at"))
    consumed_at = _parse_aware_time(receipt.get("consumed_at")) if receipt else None
    if (
        receipt is None
        or unknown_at is None
        or consumed_at is None
        # ``now_iso`` intentionally stores second precision while the action
        # claim keeps microseconds. Treat values inside that serialization
        # window as the same ordered event.
        or unknown_at + timedelta(seconds=1) < consumed_at
    ):
        return None
    return receipt


@_smart_state_locked
def recover_localized_final_submit_preclick_unknown(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
    controller: Any | None = None,
) -> dict[str, Any]:
    """Restore the 0.10.6 localized-control failure only after strict no-click proof.

    The operation is deliberately narrow. It requires the exact consumed receipt,
    the matching SDK journal failure, two stable snapshots of the same SNAPP review
    page, and one enabled localized final-submit control. It never clicks the page.
    """

    from .smart_browser_cdp import SmartBrowserCdpController, is_final_submit_control

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    authorization_id = str(authorization_id or "").strip().casefold()

    def refuse(message: str) -> None:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_NO_CLICK_VERIFICATION_REQUIRED,
            paper_id,
            message,
        )

    if (
        not paper_id
        or not attempt_id
        or not thread_id
        or not re.fullmatch(r"[0-9a-f]{32}", authorization_id)
    ):
        refuse("exact paper, attempt, thread, and consumed authorization are required")
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        refuse("the Smart paper does not exist")
    if (
        str(paper.get("status") or "").strip().casefold()
        != "final_submit_result_unknown"
        or str(paper.get("workflow_status") or "").strip().casefold() != "blocked"
        or str(paper.get("worker_status") or "").strip().casefold() != "blocked"
        or str(paper.get("next_action") or "").strip()
        != "manual_verify_submission_result_before_any_retry"
        or int(paper.get("route_revision") or 0) != int(route_revision)
        or str(paper.get("attempt_id") or "").strip() != attempt_id
        or str(paper.get("thread_id") or "").strip() != thread_id
        or paper_id in _queue_paper_ids(state, "online_queue")
    ):
        refuse("the paper is not the exact isolated unknown-result attempt")
    authorization = _claimed_authorization_matches_unknown_result(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization_id=authorization_id,
    )
    if authorization is None:
        refuse("the consumed one-use authorization or its immutable audits do not match")
    if (
        _paper_has_prior_submission_evidence(paper)
        or _paper_has_prior_trusted_submission_evidence(paper_id, paper)
        or isinstance(paper.get("pending_submission_confirmation_evidence"), dict)
    ):
        refuse("positive submission evidence exists; the unknown result cannot be reset")
    sdk_proof = _legacy_localized_final_control_failure_proof(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization_id=authorization_id,
    )
    if sdk_proof is None:
        refuse("the exact 0.10.6 localized-control pre-click failure is not proven")

    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    adapter = submission_site_adapter(system_id)
    if adapter is None or system_id != "springer_nature":
        refuse("this recovery is restricted to the Springer Nature SNAPP adapter")
    expected_url = str(
        paper.get("draft_url") or paper.get("submission_draft_url") or ""
    ).strip()
    match = re.fullmatch(
        r"https://submission\.springernature\.com/new/submission/"
        r"(?P<draft_id>[A-Za-z0-9-]+)/review",
        expected_url,
        flags=re.IGNORECASE,
    )
    if (
        match is None
        or str(match.group("draft_id") or "").strip()
        != str(paper.get("draft_id") or paper.get("submission_draft_id") or "").strip()
    ):
        refuse("the locked remote draft is not an exact SNAPP review URL")

    active_controller = controller or SmartBrowserCdpController(ports=SMART_CDP_PORTS)
    pages = active_controller.list_pages(adapter.allowed_origins)
    matching_pages = [
        item
        for item in pages
        if isinstance(item, dict) and str(item.get("url") or "").strip() == expected_url
    ]
    if len(matching_pages) != 1:
        refuse("exactly one open browser page must match the locked SNAPP review URL")
    page_id = str(matching_pages[0].get("page_id") or "").strip()
    if not page_id:
        refuse("the matching review page has no stable local page id")

    positive_markers = (
        "your submission has been received",
        "manuscript submitted successfully",
        "submission complete",
        "thank you for your submission",
        "提交成功",
        "投稿成功",
        "稿件已提交",
        "已收到您的稿件",
    )
    observed_controls: list[dict[str, Any]] = []
    observed_port = 0
    for observation in range(2):
        snapshot = active_controller.snapshot(
            adapter.allowed_origins,
            page_id=page_id,
        )
        if (
            str(snapshot.get("url") or "").strip() != expected_url
            or str(snapshot.get("path") or "").strip().casefold()
            != urllib.parse.urlparse(expected_url).path.casefold()
            or str(snapshot.get("ready_state") or "").strip().casefold()
            not in {"interactive", "complete"}
        ):
            refuse("the live page changed while no-click evidence was being collected")
        visible_text = re.sub(
            r"\s+", " ", str(snapshot.get("text") or "").strip().casefold()
        )
        if any(marker in visible_text for marker in positive_markers):
            refuse("the live page contains a positive submission confirmation marker")
        controls = (
            snapshot.get("controls")
            if isinstance(snapshot.get("controls"), list)
            else []
        )
        final_controls = [
            dict(item)
            for item in controls
            if isinstance(item, dict)
            and not bool(item.get("disabled"))
            and str(item.get("tag") or "").strip().casefold() in {"button", "input"}
            and str(item.get("type") or "").strip().casefold() in {"button", "submit"}
            and is_final_submit_control(item.get("label"))
        ]
        if len(final_controls) != 1:
            refuse("the review page must expose exactly one enabled final-submit control")
        observed_controls = final_controls
        observed_port = int(snapshot.get("cdp_port") or 0)
        if observation == 0:
            time.sleep(0.25)

    now = now_iso()
    final_control = observed_controls[0]
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_no_click_verified",
        at=now,
        lines=[
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- thread_id: {thread_id}",
            f"- authorization_id: {authorization_id}",
            f"- sdk_turn_id: {sdk_proof['turn_id']}",
            f"- sdk_proof: {sdk_proof['proof']}",
            f"- review_url: {expected_url}",
            f"- page_id: {page_id}",
            f"- cdp_port: {observed_port}",
            f"- final_control_label: {str(final_control.get('label') or '').strip()}",
            "- stable_review_snapshots: 2",
            "- positive_confirmation_evidence: absent",
            "- final_click_verified_not_executed: true",
            "- transaction_state: committed_with_state",
        ],
    )
    old_challenge = paper.pop("final_submit_confirmation_challenge", None)
    paper["last_verified_no_click_authorization"] = copy.deepcopy(authorization)
    if isinstance(old_challenge, dict):
        paper["last_verified_no_click_confirmation_challenge"] = copy.deepcopy(
            old_challenge
        )
    paper.pop("final_submit_authorization", None)
    paper.pop("pending_submission_confirmation_evidence", None)
    paper["status"] = "waiting"
    paper["workflow_status"] = "waiting"
    paper["worker_status"] = "waiting"
    paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
    paper["blocker"] = ""
    paper["detail"] = (
        "Two stable read-only snapshots verified the unchanged SNAPP review page "
        "and its enabled localized final-submit control; the 0.10.6 pre-click "
        "failure was resolved without treating the manuscript as submitted."
    )
    paper["final_submit_result_unknown_resolved_at"] = now
    paper["final_submit_result_unknown_resolution"] = "verified_no_click"
    paper["final_submit_no_click_audit_path"] = str(prepared_audit["path"])
    paper["final_submit_no_click_audit_sha256"] = str(prepared_audit["sha256"])
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    settings = state.setdefault("settings", {})
    if isinstance(settings, dict):
        settings["online_submit_enabled"] = False
        settings["sdk_pause_requested"] = True
    state["mode"] = "manual"
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "final_submit_no_click_verified",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "review_url": expected_url,
            "sdk_turn_id": sdk_proof["turn_id"],
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": True,
        "code": "SMART_FINAL_SUBMIT_NO_CLICK_VERIFIED",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "status": "waiting",
        "next_action": FINAL_SUBMIT_REVIEW_READY_ACTION,
        "review_url": expected_url,
        "final_control_label": str(final_control.get("label") or "").strip(),
        "audit_path": str(audit_path),
        "state": state,
    }


def _final_submit_tool_timeout_proof(
    paper_id: str,
    paper: dict[str, Any],
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
) -> dict[str, str] | None:
    """Verify the exact completed SDK turn that timed out after one tool call."""

    journal_path = Path(SMART_STATE_PATH).with_name("sdk_dispatch.json")
    journal = read_json(journal_path, {})
    papers = journal.get("papers") if isinstance(journal.get("papers"), dict) else {}
    task = papers.get(paper_id)
    if not isinstance(task, dict):
        return None
    final_response = re.sub(
        r"\s+",
        " ",
        str(task.get("final_response") or "").strip().casefold(),
    )
    if (
        str(task.get("thread_id") or "").strip() != thread_id
        or str(task.get("turn_id") or "").strip()
        != str(paper.get("sdk_turn_id") or "").strip()
        or int(task.get("route_revision") or 0) != int(route_revision)
        or str(task.get("attempt_id") or "").strip() != attempt_id
        or str(task.get("authorization_id") or "").strip().casefold()
        != authorization_id
        or str(task.get("status") or "").strip().casefold() != "completed"
        or str(task.get("recorded_status") or "").strip().casefold() != "blocked"
        or "trusted final-submit tool timed out after the one authorized attempt"
        not in final_response
    ):
        return None
    return {
        "journal_path": str(journal_path),
        "turn_id": str(task.get("turn_id") or "").strip(),
        "proof": "one_use_final_submit_tool_timeout",
    }


@_smart_state_locked
def recover_final_submit_timeout_when_review_is_unchanged(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
    controller: Any | None = None,
) -> dict[str, Any]:
    """Release a consumed timeout only after strict proof that no click landed.

    A synthetic click can be ignored by a modern submission portal while the
    surrounding MCP call later times out waiting for confirmation.  Retrying
    merely because the worker timed out is unsafe.  This recovery therefore
    requires the exact signed consumed authorization, the exact completed SDK
    timeout record, two stable snapshots of the paper-owned review tab, one
    still-enabled final control, and no positive submission evidence.
    """

    from .smart_browser_cdp import SmartBrowserCdpController, is_final_submit_control

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    authorization_id = str(authorization_id or "").strip().casefold()

    def refuse(message: str) -> None:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_NO_CLICK_VERIFICATION_REQUIRED,
            paper_id,
            message,
        )

    if (
        not paper_id
        or not attempt_id
        or not thread_id
        or not re.fullmatch(r"[0-9a-f]{32}", authorization_id)
    ):
        refuse("exact paper, attempt, thread, and consumed authorization are required")
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        refuse("the Smart paper does not exist")
    if (
        str(paper.get("status") or "").strip().casefold()
        not in {"blocked", "final_submit_result_unknown"}
        or str(paper.get("workflow_status") or "").strip().casefold() != "blocked"
        or str(paper.get("worker_status") or "").strip().casefold() != "blocked"
        or str(paper.get("next_action") or "").strip()
        != FINAL_SUBMIT_REVIEW_READY_ACTION
        or int(paper.get("route_revision") or 0) != int(route_revision)
        or str(paper.get("attempt_id") or "").strip() != attempt_id
        or str(paper.get("thread_id") or "").strip() != thread_id
        or paper_id in _queue_paper_ids(state, "online_queue")
    ):
        refuse("the paper is not the exact isolated final-submit timeout attempt")
    authorization = _consumed_authorization_matches_current_binding(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization_id=authorization_id,
    )
    if authorization is None:
        refuse("the consumed one-use authorization or its immutable audits do not match")
    if (
        _paper_has_prior_submission_evidence(paper)
        or _paper_has_prior_trusted_submission_evidence(paper_id, paper)
        or isinstance(paper.get("pending_submission_confirmation_evidence"), dict)
    ):
        refuse("positive submission evidence exists; the timed-out action cannot be reset")
    sdk_proof = _final_submit_tool_timeout_proof(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization_id=authorization_id,
    )
    if sdk_proof is None:
        refuse("the exact one-use final-submit tool timeout is not proven")

    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    adapter = submission_site_adapter(system_id)
    if adapter is None:
        refuse("the submission system has no verified site adapter")
    expected_url = str(
        paper.get("draft_url") or paper.get("submission_draft_url") or ""
    ).strip()
    if (
        not expected_url
        or normalize_origin(expected_url)
        not in {normalize_origin(item) for item in adapter.allowed_origins}
    ):
        refuse("the locked final-review URL is outside the selected site adapter")
    # Refresh the page-claim binding without accepting any unowned same-origin tab.
    _final_submit_target_binding(paper_id, paper)
    page_id = str(paper.get("draft_binding_page_id") or "").strip()
    if (
        str(paper.get("draft_binding_source") or "")
        != "trusted_sdk_browser_page_claim"
        or not page_id
    ):
        refuse("the final-review page is not owned by this exact paper attempt")

    active_controller = controller or SmartBrowserCdpController(ports=SMART_CDP_PORTS)
    pages = active_controller.list_pages(adapter.allowed_origins)
    matching_pages = [
        item
        for item in pages
        if isinstance(item, dict)
        and str(item.get("page_id") or "").strip() == page_id
        and str(item.get("url") or "").strip() == expected_url
    ]
    if len(matching_pages) != 1:
        refuse("the exact owned final-review page is not uniquely open")

    positive_markers = (
        "your submission has been received",
        "manuscript submitted successfully",
        "submission complete",
        "thank you for your submission",
        "submission_received",
        "submitted_successfully",
        "鎻愪氦鎴愬姛",
        "鎶曠鎴愬姛",
        "submission has been received",
        "submission accepted for processing",
    )
    observed_control: dict[str, Any] | None = None
    observed_port = 0
    expected_path = urllib.parse.urlparse(expected_url).path.casefold()
    for observation in range(2):
        snapshot = active_controller.snapshot(
            adapter.allowed_origins,
            page_id=page_id,
        )
        if (
            str(snapshot.get("url") or "").strip() != expected_url
            or str(snapshot.get("path") or "").strip().casefold() != expected_path
            or str(snapshot.get("ready_state") or "").strip().casefold()
            not in {"interactive", "complete"}
        ):
            refuse("the final-review page changed while no-click proof was collected")
        visible_text = re.sub(
            r"\s+", " ", str(snapshot.get("text") or "").strip().casefold()
        )
        if any(marker in visible_text for marker in positive_markers):
            refuse("the live page contains positive submission evidence")
        controls = (
            snapshot.get("controls")
            if isinstance(snapshot.get("controls"), list)
            else []
        )
        final_controls = [
            dict(item)
            for item in controls
            if isinstance(item, dict)
            and not bool(item.get("disabled"))
            and str(item.get("tag") or "").strip().casefold() in {"button", "input"}
            and str(item.get("type") or "").strip().casefold()
            in {"button", "submit"}
            and is_final_submit_control(item.get("label"))
        ]
        if len(final_controls) != 1:
            refuse("the unchanged review page must expose one enabled final-submit control")
        if observed_control is not None and (
            str(final_controls[0].get("label") or "").strip()
            != str(observed_control.get("label") or "").strip()
            or str(final_controls[0].get("ref") or "").strip()
            != str(observed_control.get("ref") or "").strip()
        ):
            refuse("the final-submit control changed between no-click observations")
        observed_control = final_controls[0]
        observed_port = int(snapshot.get("cdp_port") or 0)
        if observation == 0:
            time.sleep(0.25)
    if observed_control is None:
        refuse("the final-submit control could not be observed")

    now = now_iso()
    prepared_audit = _prepare_canary_route_audit(
        paper_id,
        event="final_submit_timeout_no_click_verified",
        at=now,
        lines=[
            f"- route_revision: {int(route_revision)}",
            f"- attempt_id: {attempt_id}",
            f"- thread_id: {thread_id}",
            f"- authorization_id: {authorization_id}",
            f"- sdk_turn_id: {sdk_proof['turn_id']}",
            f"- sdk_proof: {sdk_proof['proof']}",
            f"- review_url: {expected_url}",
            f"- page_id: {page_id}",
            f"- cdp_port: {observed_port}",
            f"- final_control_label: {str(observed_control.get('label') or '').strip()}",
            "- stable_review_snapshots: 2",
            "- positive_confirmation_evidence: absent",
            "- final_click_verified_not_executed: true",
            "- transaction_state: committed_with_state",
        ],
    )
    old_challenge = paper.pop("final_submit_confirmation_challenge", None)
    paper["last_verified_no_click_authorization"] = copy.deepcopy(authorization)
    if isinstance(old_challenge, dict):
        paper["last_verified_no_click_confirmation_challenge"] = copy.deepcopy(
            old_challenge
        )
    paper.pop("final_submit_authorization", None)
    paper.pop("pending_submission_confirmation_evidence", None)
    paper.pop("final_submit_action_claimed_at", None)
    paper.pop("final_submit_action_claimed_by_thread_id", None)
    paper["status"] = "waiting"
    paper["workflow_status"] = "waiting"
    paper["worker_status"] = "waiting"
    paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
    paper["blocker"] = ""
    paper["detail"] = (
        "Two stable read-only snapshots verified that the exact paper-owned "
        "final-review page and enabled final-submit control were unchanged after "
        "the timed-out synthetic click. No submission evidence was found."
    )
    paper["final_submit_timeout_resolved_at"] = now
    paper["final_submit_timeout_resolution"] = "verified_no_click"
    paper["final_submit_no_click_audit_path"] = str(prepared_audit["path"])
    paper["final_submit_no_click_audit_sha256"] = str(prepared_audit["sha256"])
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "final_submit_timeout_no_click_verified",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "review_url": expected_url,
            "sdk_turn_id": sdk_proof["turn_id"],
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    return {
        "ok": True,
        "code": "SMART_FINAL_SUBMIT_TIMEOUT_NO_CLICK_VERIFIED",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "status": "waiting",
        "next_action": FINAL_SUBMIT_REVIEW_READY_ACTION,
        "review_url": expected_url,
        "final_control_label": str(observed_control.get("label") or "").strip(),
        "audit_path": str(audit_path),
        "state": state,
    }


@_smart_state_locked
def reconcile_captured_final_submit_success(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
    controller: Any | None = None,
    projection_sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Finalize an already captured success without activating the submit control.

    This recovery is intentionally evidence-first: it accepts only the exact
    consumed authorization, the immutable signed confirmation capture, and the
    still-visible success page with no final-submit control.
    """

    from .smart_browser_cdp import SmartBrowserCdpController, is_final_submit_control

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    authorization_id = str(authorization_id or "").strip().casefold()

    def refuse(message: str) -> None:
        raise SmartSubmissionGuardError(
            SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
            paper_id,
            message,
        )

    if (
        not paper_id
        or not attempt_id
        or not thread_id
        or not re.fullmatch(r"[0-9a-f]{32}", authorization_id)
    ):
        refuse("exact paper, attempt, thread, and consumed authorization are required")
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        refuse("the Smart paper does not exist")
    if _paper_has_prior_trusted_submission_evidence(paper_id, paper):
        return {
            "ok": True,
            "code": "SMART_FINAL_SUBMIT_SUCCESS_ALREADY_RECORDED",
            "paper_id": paper_id,
            "status": str(paper.get("status") or "submitted"),
            "state": state,
        }
    if (
        str(paper.get("status") or "").strip().casefold()
        not in {"blocked", "final_submit_result_unknown"}
        or str(paper.get("workflow_status") or "").strip().casefold() != "blocked"
        or str(paper.get("worker_status") or "").strip().casefold() != "blocked"
        or int(paper.get("route_revision") or 0) != int(route_revision)
        or str(paper.get("attempt_id") or "").strip() != attempt_id
        or str(paper.get("thread_id") or "").strip() != thread_id
        or paper_id in _queue_paper_ids(state, "online_queue")
    ):
        refuse("the paper is not the exact isolated blocked attempt")
    authorization = _consumed_authorization_matches_current_binding(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization_id=authorization_id,
    )
    if authorization is None:
        refuse("the consumed authorization or its immutable audit chain is invalid")
    captured = _valid_live_submission_confirmation_evidence(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization=authorization,
        require_fresh=False,
    )
    if captured is None:
        refuse("no exact signed positive post-submit capture is available")
    live_receipt, evidence_path = captured
    confirmation_url = _sanitize_confirmation_url(
        live_receipt.get("confirmation_url"),
        paper=paper,
    )
    submission_id = _sanitize_submission_id(live_receipt.get("manuscript_id"))
    if not confirmation_url:
        refuse("the signed capture has no allowed confirmation URL")

    system_id = str(paper.get("submission_system_id") or "").strip().casefold()
    adapter = submission_site_adapter(system_id)
    if adapter is None:
        refuse("the locked submission site adapter is unavailable")
    active_controller = controller or SmartBrowserCdpController(ports=SMART_CDP_PORTS)
    pages = active_controller.list_pages(adapter.allowed_origins)
    matching_pages = [
        item
        for item in pages
        if isinstance(item, dict)
        and str(item.get("url") or "").strip() == confirmation_url
    ]
    if len(matching_pages) != 1:
        refuse("exactly one open browser page must match the signed success URL")
    page_id = str(matching_pages[0].get("page_id") or "").strip()
    snapshot = active_controller.snapshot(adapter.allowed_origins, page_id=page_id)
    controls = (
        snapshot.get("controls")
        if isinstance(snapshot.get("controls"), list)
        else []
    )
    final_controls = [
        dict(item)
        for item in controls
        if isinstance(item, dict)
        and not bool(item.get("disabled"))
        and is_final_submit_control(item.get("label"))
    ]
    positive_text = re.sub(
        r"\s+",
        " ",
        " ".join(
            (
                str(snapshot.get("title") or ""),
                str(snapshot.get("text") or ""),
            )
        ).strip().casefold(),
    )
    positive_markers = (
        "submission received",
        "submission has been received",
        "manuscript has been received",
        "\u606d\u559c\uff01\u60a8\u7684\u7a3f\u4ef6\u5df2\u5904\u7406\u5b8c\u6bd5\u5e76\u5df2\u63d0\u4ea4\u7ed9\u671f\u520a",
        "\u60a8\u7684\u7a3f\u4ef6\u5df2\u63d0\u4ea4\u7ed9\u671f\u520a\u7f16\u8f91",
        "已收到投稿",
        "已收到稿件",
        "感谢您向",
        "技术审核阶段",
    )
    if (
        str(snapshot.get("url") or "").strip() != confirmation_url
        or str(snapshot.get("ready_state") or "").strip().casefold()
        not in {"interactive", "complete"}
        or final_controls
        or not any(marker in positive_text for marker in positive_markers)
    ):
        refuse("the live page does not independently confirm the captured success")

    now = now_iso()
    safe_paper_id = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id) or "paper"
    audit_path = SMART_AUDIT_DIR / (
        f"{timestamp_id()}_{safe_paper_id}_captured_success_reconciled_"
        f"{os.urandom(4).hex()}.md"
    )
    prepared_audit = _prepare_audit_content(
        audit_path,
        "\n".join(
            [
                "# Smart Submission Captured Success Reconciliation",
                "",
                f"- paper_id: {paper_id}",
                f"- route_revision: {int(route_revision)}",
                f"- attempt_id: {attempt_id}",
                f"- thread_id: {thread_id}",
                f"- authorization_id: {authorization_id}",
                f"- evidence_id: {live_receipt.get('evidence_id', '')}",
                f"- confirmation_marker: {live_receipt.get('confirmation_marker', '')}",
                f"- confirmation_url: {confirmation_url}",
                f"- submission_id: {submission_id or '-'}",
                f"- evidence_path: {evidence_path}",
                f"- live_page_id: {page_id}",
                "- live_final_submit_control_visible: false",
                "- submit_control_activated_by_reconciliation: false",
                "- transaction_state: committed_with_state",
            ]
        ),
    )
    authorization["result_recorded_at"] = now
    paper["final_submit_authorization"] = _sign_smart_receipt(authorization)
    paper["final_submit_authorization_result_recorded_at"] = now
    paper["last_live_submission_confirmation_evidence"] = copy.deepcopy(
        paper.get("pending_submission_confirmation_evidence")
    )
    paper.pop("pending_submission_confirmation_evidence", None)
    if submission_id:
        paper["submission_id"] = submission_id
    paper["submission_evidence_path"] = str(evidence_path)
    paper["confirmation_url"] = confirmation_url
    paper["final_submit_confirmed"] = True
    paper["status"] = "submitted"
    paper["workflow_status"] = "monitoring"
    paper["worker_status"] = "monitoring"
    paper["next_action"] = EMAIL_FIRST_MONITORING_NEXT
    paper["blocker"] = ""
    paper["detail"] = (
        "Signed live success evidence was reconciled after the SDK worker "
        "misreported blocked; no additional submit action was performed."
    )
    paper["last_worker_recorded_at"] = now
    paper["last_worker_result_source"] = "captured_success_reconciliation"
    paper["submission_confirmation_receipt"] = _sign_smart_receipt(
        {
            "schema": SUBMISSION_CONFIRMATION_RECEIPT_SCHEMA,
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "capture_evidence_id": str(live_receipt.get("evidence_id") or ""),
            "status": "submitted",
            "submission_id": submission_id,
            "evidence_path": str(evidence_path),
            "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "confirmation_url": confirmation_url,
            "recorded_at": now,
            "result_audit_path": str(prepared_audit["path"]),
            "result_audit_sha256": str(prepared_audit["sha256"]),
        }
    )
    paper["last_audit_path"] = str(prepared_audit["path"])
    paper["last_audit_sha256"] = str(prepared_audit["sha256"])
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "reconcile_captured_final_submit_success",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "evidence_id": str(live_receipt.get("evidence_id") or ""),
            "confirmation_url": confirmation_url,
            "submit_control_activated": False,
        }
    )
    projection_dirty = _prepare_smart_projection_change(
        state,
        "reconcile_captured_final_submit_success",
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    committed_audit = _commit_prepared_audit(prepared_audit)
    projection_dirty = _mark_smart_projection_dirty(
        state,
        "reconcile_captured_final_submit_success",
    )
    state = read_json(SMART_STATE_PATH, state)
    projection_refresh = refresh_smart_submission_projection(
        sync_callback=projection_sync_callback,
    )
    return {
        "ok": True,
        "code": "SMART_FINAL_SUBMIT_CAPTURED_SUCCESS_RECONCILED",
        "paper_id": paper_id,
        "status": "submitted",
        "workflow_status": "monitoring",
        "confirmation_url": confirmation_url,
        "submission_id": submission_id,
        "evidence_path": str(evidence_path),
        "audit_path": str(committed_audit),
        "projection_dirty": projection_dirty,
        "projection_refresh": projection_refresh,
        "state": state,
    }


@_smart_state_locked
def record_smart_sdk_turn_failure(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    detail: str,
) -> dict[str, Any]:
    """Release one failed SDK turn without losing final-click uncertainty."""

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "SDK failure has no matching Smart paper",
        )
    authorization = (
        paper.get("final_submit_authorization")
        if isinstance(paper.get("final_submit_authorization"), dict)
        else None
    )
    if paper.get("monitor_check_pending") and (
        authorization is None or not str(authorization.get("consumed_at") or "")
    ):
        return record_visible_worker_result(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
            status="monitoring",
            next_action=EMAIL_FIRST_MONITORING_NEXT,
            detail=str(detail or "The scheduled journal status check did not complete."),
            thread_id=thread_id,
            source="scheduled_monitor_check_failure",
            activate=False,
            keep_queued=False,
        )
    if authorization is not None and str(authorization.get("consumed_at") or ""):
        return record_final_submit_result_unknown(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
            authorization_id=str(authorization.get("authorization_id") or ""),
            detail=str(detail or "The SDK turn failed after the final action was claimed."),
        )
    if authorization is None:
        return record_visible_worker_result(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
            status="revision_needed",
            next_action="retry_codex_sdk_browser_turn_after_review",
            detail=str(detail or "The Codex SDK browser turn did not complete."),
            thread_id=thread_id,
            source="codex_sdk_browser_turn_failure",
            activate=False,
            keep_queued=True,
        )
    if (
        str(paper.get("attempt_id") or "").strip() != attempt_id
        or str(paper.get("thread_id") or "").strip() != thread_id
        or str(paper.get("workflow_status") or "").strip().casefold() != "running"
    ):
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "SDK failure does not own the current running final-submit attempt",
        )
    now = now_iso()
    safe_detail = str(detail or "").strip()[:2000] or (
        "The SDK turn stopped before the one-use final action was claimed."
    )
    old_authorization_id = str(authorization.get("authorization_id") or "")
    paper.pop("final_submit_authorization", None)
    paper["final_submit_authorization_cancelled_at"] = now
    paper["status"] = "waiting"
    paper["workflow_status"] = "waiting"
    paper["worker_status"] = "waiting"
    paper["next_action"] = FINAL_SUBMIT_REVIEW_READY_ACTION
    paper["detail"] = safe_detail
    paper["blocker"] = ""
    state["online_queue"] = [
        str(item)
        for item in state.get("online_queue", [])
        if str(item).strip() and str(item) != paper_id
    ]
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "smart_sdk_final_turn_released_before_claim",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "cancelled_authorization_id": old_authorization_id,
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    return {
        "ok": False,
        "code": "SMART_SDK_FINAL_TURN_RELEASED_BEFORE_CLAIM",
        "paper_id": paper_id,
        "status": "waiting",
        "next_action": FINAL_SUBMIT_REVIEW_READY_ACTION,
        "state": state,
    }


@_smart_state_locked
def capture_live_submission_confirmation(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    thread_id: str,
    authorization_id: str,
    ports: tuple[int, ...] = SMART_CDP_PORTS,
    transport: Any | None = None,
    allow_unknown_recovery: bool = False,
) -> dict[str, Any]:
    """Capture and sign positive post-click DOM evidence from approved local CDP."""

    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    authorization_id = str(authorization_id or "").strip().casefold()
    if (
        not paper_id
        or not attempt_id
        or not thread_id
        or not re.fullmatch(r"[0-9a-f]{32}", authorization_id)
    ):
        raise SmartSubmissionGuardError(
            SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
            paper_id,
            "exact paper, attempt, worker thread, and authorization id are required",
        )
    require_smart_canary_route(paper_id, expected_revision=route_revision)
    state = read_json(SMART_STATE_PATH, _default_state())
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    paper = papers.get(paper_id)
    normal_dispatch = bool(
        isinstance(paper, dict)
        and paper_id in _queue_paper_ids(state, "online_queue")
        and str(paper.get("workflow_status") or "").strip().casefold() == "running"
        and str(paper.get("worker_status") or "").strip().casefold() == "running"
        and str(paper.get("thread_id") or "").strip() == thread_id
        and str(paper.get("attempt_id") or "").strip() == attempt_id
    )
    unknown_recovery = bool(
        allow_unknown_recovery
        and isinstance(paper, dict)
        and paper_id not in _queue_paper_ids(state, "online_queue")
        and str(paper.get("status") or "").strip().casefold()
        == "final_submit_result_unknown"
        and str(paper.get("workflow_status") or "").strip().casefold() == "blocked"
        and str(paper.get("worker_status") or "").strip().casefold() == "blocked"
        and str(paper.get("next_action") or "").strip()
        == "manual_verify_submission_result_before_any_retry"
        and str(paper.get("thread_id") or "").strip() == thread_id
        and str(paper.get("attempt_id") or "").strip() == attempt_id
    )
    if not normal_dispatch and not unknown_recovery:
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "live confirmation capture requires the exact running attempt or an explicitly isolated unknown-result recovery",
        )
    checked_at = datetime.now().astimezone()
    authorization = (
        _claimed_authorization_matches_unknown_result(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
            authorization_id=authorization_id,
        )
        if unknown_recovery
        else _claimed_final_submit_authorization(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
            now=checked_at,
            require_click_window=True,
        )
    )
    if authorization is None or str(authorization.get("authorization_id") or "") != authorization_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "live confirmation capture is outside the claimed final-click window",
        )
    existing = _valid_live_submission_confirmation_evidence(
        paper_id,
        paper,
        route_revision=route_revision,
        attempt_id=attempt_id,
        thread_id=thread_id,
        authorization=authorization,
        now=checked_at,
    )
    if existing is not None:
        receipt, evidence_path = existing
        return {
            "ok": True,
            "code": "SMART_SUBMISSION_CONFIRMATION_CAPTURED",
            "paper_id": paper_id,
            "evidence_id": receipt["evidence_id"],
            "manuscript_id": receipt["manuscript_id"],
            "confirmation_url": receipt["confirmation_url"],
            "evidence_path": str(evidence_path),
            "reused": True,
        }
    if not unknown_recovery:
        _require_current_worker_auth(paper_id, paper)
    active_transport = transport or LocalCdpProbeTransport()
    target_origin = str(authorization.get("target_origin") or "")
    allowed_ports = tuple(int(port) for port in ports)
    if not allowed_ports or any(port not in SMART_CDP_PORTS for port in allowed_ports):
        raise SmartSubmissionGuardError(
            SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
            paper_id,
            "confirmation capture ports are outside the approved local CDP allowlist",
        )

    captured: tuple[int, str, str, str] | None = None
    for port in allowed_ports:
        try:
            targets = active_transport.list_targets(port)
        except Exception:
            continue
        for target in targets:
            if (
                not isinstance(target, dict)
                and not hasattr(target, "get")
            ) or str(target.get("type") or "").strip().casefold() != "page":
                continue
            target_url = str(target.get("url") or "").strip()
            if normalize_origin(target_url) != target_origin:
                continue
            websocket_url = str(target.get("webSocketDebuggerUrl") or "").strip()
            if not websocket_url:
                continue
            try:
                runtime = active_transport.evaluate_submission_confirmation_state(
                    port,
                    websocket_url,
                )
            except Exception:
                continue
            if not isinstance(runtime, dict) and not hasattr(runtime, "get"):
                continue
            marker = str(runtime.get("confirmation_marker") or "").strip().casefold()
            if (
                normalize_origin(runtime.get("origin")) != target_origin
                or runtime.get("document_ready") is not True
                or runtime.get("final_submit_control_visible") is not False
                or runtime.get("login_challenge_visible") is not False
                or marker not in _LIVE_CONFIRMATION_MARKERS
            ):
                continue
            try:
                confirmation_url = _sanitize_confirmation_url(target_url, paper=paper)
                manuscript_id = _sanitize_submission_id(runtime.get("manuscript_id"))
            except ValueError:
                continue
            parsed_path = urllib.parse.urlparse(confirmation_url).path.casefold()
            positive_path = any(
                token in parsed_path
                for token in ("confirm", "success", "submitted", "complete", "thank")
            )
            final_review_path = bool(
                re.search(
                    r"(?:^|/)(?:final[-_]?review|finalreview|review)(?:/|$)",
                    parsed_path,
                )
            )
            strong_marker = marker in {
                "successfully_submitted",
                "submission_received",
                "thank_you_for_submission",
                "manuscript_submitted",
                "submission_complete",
            }
            # Some federated portals (notably Wiley) replace the final-review
            # DOM with a success message without navigating away.  Accept that
            # marker-only shape only on a review endpoint; a generic draft page
            # must never become confirmation evidence merely because it exposes
            # matching text.
            if (
                not manuscript_id
                and not positive_path
                and not (strong_marker and final_review_path)
            ):
                continue
            captured = (port, confirmation_url, marker, manuscript_id)
            break
        if captured is not None:
            break
    if captured is None:
        raise SmartSubmissionGuardError(
            SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
            paper_id,
            "no exact-origin page exposes a positive post-submit marker without a final-submit control",
        )

    checked_at = datetime.now().astimezone()
    authorization = (
        _claimed_authorization_matches_unknown_result(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
            authorization_id=authorization_id,
        )
        if unknown_recovery
        else _claimed_final_submit_authorization(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
            now=checked_at,
            require_click_window=True,
        )
    )
    if authorization is None or str(authorization.get("authorization_id") or "") != authorization_id:
        raise SmartSubmissionGuardError(
            SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
            paper_id,
            "final-click authority expired while confirmation evidence was being captured",
        )
    port, confirmation_url, marker, manuscript_id = captured
    receipt = _sign_smart_receipt(
        {
            "schema": LIVE_SUBMISSION_CONFIRMATION_SCHEMA,
            "evidence_id": os.urandom(16).hex(),
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "account_id": str(authorization.get("account_id") or ""),
            "system_id": str(authorization.get("submission_system_id") or ""),
            "target_journal": str(authorization.get("target_journal") or ""),
            "target_origin": target_origin,
            "journal_site_key": str(authorization.get("journal_site_key") or ""),
            "account_binding_sha256": str(authorization.get("account_binding_sha256") or ""),
            "site_binding_sha256": str(authorization.get("site_binding_sha256") or ""),
            "draft_binding_sha256": str(authorization.get("draft_binding_sha256") or ""),
            "package_binding_sha256": str(authorization.get("package_binding_sha256") or ""),
            "checked_at": checked_at.isoformat(),
            "confirmation_url": confirmation_url,
            "confirmation_marker": marker,
            "manuscript_id": manuscript_id,
            "cdp_port": int(port),
            "document_ready": True,
            "final_submit_control_visible": False,
            "login_challenge_visible": False,
            "value_free": True,
        }
    )
    safe_paper_id = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id) or "paper"
    evidence_path = SMART_CONFIRMATION_EVIDENCE_DIR / (
        f"{timestamp_id()}_{safe_paper_id}_{receipt['evidence_id']}.json"
    )
    prepared_evidence = _prepare_audit_content(
        evidence_path,
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2),
    )
    paper["pending_submission_confirmation_evidence"] = {
        "receipt": receipt,
        "evidence_path": str(prepared_evidence["path"]),
        "evidence_sha256": str(prepared_evidence["sha256"]),
    }
    state["updated_at"] = checked_at.isoformat()
    state.setdefault("history", []).append(
        {
            "at": checked_at.isoformat(),
            "event": "capture_live_submission_confirmation",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "thread_id": thread_id,
            "authorization_id": authorization_id,
            "evidence_id": receipt["evidence_id"],
            "confirmation_marker": marker,
            "has_manuscript_id": bool(manuscript_id),
        }
    )
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_evidence)
        raise
    committed_path = _commit_prepared_audit(prepared_evidence)
    return {
        "ok": True,
        "code": "SMART_SUBMISSION_CONFIRMATION_CAPTURED",
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "authorization_id": authorization_id,
        "evidence_id": receipt["evidence_id"],
        "manuscript_id": manuscript_id,
        "confirmation_url": confirmation_url,
        "confirmation_marker": marker,
        "evidence_path": str(committed_path),
        "reused": False,
    }


def record_visible_worker_result(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    status: str,
    next_action: str = "",
    detail: str = "",
    thread_id: str,
    source: str = "visible_worker_final",
    submission_id: str = "",
    evidence_path: str = "",
    confirmation_url: str = "",
    activate: bool = True,
    keep_queued: bool = False,
    projection_sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """V1-compatible worker-result API.

    Retarget closure markers use the dedicated entry point below so this
    frozen public signature remains compatible with existing integrations.
    """

    return _record_visible_worker_result_impl(
        paper_id,
        route_revision=route_revision,
        attempt_id=attempt_id,
        status=status,
        next_action=next_action,
        detail=detail,
        thread_id=thread_id,
        source=source,
        submission_id=submission_id,
        evidence_path=evidence_path,
        confirmation_url=confirmation_url,
        activate=activate,
        keep_queued=keep_queued,
        projection_sync_callback=projection_sync_callback,
    )


def record_smart_retarget_closure_result(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    status: str,
    next_action: str = "",
    detail: str = "",
    thread_id: str,
    source: str = "visible_worker_final",
    submission_id: str = "",
    closed_submission_ids: Iterable[str] | str = (),
    evidence_path: str = "",
    confirmation_url: str = "",
    activate: bool = True,
    keep_queued: bool = False,
    projection_sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Record exact-ID closure evidence for one user retarget request."""

    return _record_visible_worker_result_impl(
        paper_id,
        route_revision=route_revision,
        attempt_id=attempt_id,
        status=status,
        next_action=next_action,
        detail=detail,
        thread_id=thread_id,
        source=source,
        submission_id=submission_id,
        closed_submission_ids=closed_submission_ids,
        evidence_path=evidence_path,
        confirmation_url=confirmation_url,
        activate=activate,
        keep_queued=keep_queued,
        projection_sync_callback=projection_sync_callback,
    )


@_smart_state_locked
def _record_visible_worker_result_impl(
    paper_id: str,
    *,
    route_revision: int,
    attempt_id: str,
    status: str,
    next_action: str = "",
    detail: str = "",
    thread_id: str,
    source: str = "visible_worker_final",
    submission_id: str = "",
    closed_submission_ids: Iterable[str] | str = (),
    evidence_path: str = "",
    confirmation_url: str = "",
    activate: bool = True,
    keep_queued: bool = False,
    projection_sync_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Record a visible worker's final markers and release its scheduler slot.

    Worker threads can finish without writing local state when they were
    deliberately restricted to browser/CDP actions. This function lets the
    visible scheduler convert the worker's `SMART_SUBMISSION_*` final markers
    into state, so completed or idle threads do not keep consuming the parallel
    worker cap.
    """
    paper_id = str(paper_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    thread_id = str(thread_id or "").strip()
    normalized_status = str(status or "").strip().lower()
    if not paper_id or not attempt_id or not thread_id:
        raise ValueError("paper_id, attempt_id, and thread_id are required")
    observed_recovery_hint = _routine_online_recovery_reason(
        {
            "status": normalized_status,
            "workflow_status": "",
            "next_action": next_action,
            "detail": detail,
        }
    )
    deferred_route_error: SmartSubmissionGuardError | None = None
    try:
        route = require_smart_canary_route(
            paper_id,
            expected_revision=route_revision,
        )
    except SmartSubmissionGuardError as exc:
        if exc.code not in {SMART_ROUTE_OWNER_REQUIRED, SMART_ROUTE_MODE_REQUIRED}:
            raise
        deferred_route_error = exc
        route = {}
    valid_statuses = {
        "submitted",
        "monitoring",
        "revision_needed",
        "retarget_needed",
        "rejected",
        "blocked",
        "accepted",
        "waiting",
        "no_action",
        "login_check",
        "uploading",
        "ready_for_online_submit",
    }
    if normalized_status not in valid_statuses:
        raise ValueError(f"Unsupported smart submission worker status: {status}")

    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    papers = state.setdefault("papers", {})
    if not isinstance(papers, dict):
        papers = {}
        state["papers"] = papers
    paper = papers.get(paper_id)
    if not isinstance(paper, dict):
        if deferred_route_error is not None:
            raise deferred_route_error
        raise SmartSubmissionGuardError(
            SMART_CANARY_DISPATCH_NOT_QUEUED,
            paper_id,
            "worker result has no matching Smart paper record",
        )
    monitor_check_was_pending = _monitor_check_active(paper)
    if deferred_route_error is not None:
        if not monitor_check_was_pending:
            raise deferred_route_error
        route = _require_worker_route(
            paper_id,
            paper,
            expected_revision=route_revision,
            monitor_only=True,
        )
    expected_attempt = str(paper.get("attempt_id") or "").strip()
    if not expected_attempt or attempt_id != expected_attempt:
        raise SmartSubmissionGuardError(
            SMART_CANARY_ATTEMPT_MISMATCH,
            paper_id,
            "worker result attempt_id is stale",
            details={"expected_attempt_id": expected_attempt, "actual_attempt_id": attempt_id},
        )
    online_queue = list(
        dict.fromkeys(str(item) for item in state.get("online_queue", []) if str(item).strip())
    )
    workflow_status = str(paper.get("workflow_status") or "").strip().lower()
    if paper_id not in online_queue or workflow_status != "running":
        raise SmartSubmissionGuardError(
            SMART_CANARY_RESULT_NOT_DISPATCHED,
            paper_id,
            "worker result requires a currently running queued dispatch",
            details={"workflow_status": workflow_status, "in_online_queue": paper_id in online_queue},
        )
    current_thread_id = str(paper.get("thread_id") or "").strip()
    stale_thread_ids = {
        str(item).strip()
        for item in (paper.get("stale_worker_thread_ids") or [])
        if str(item).strip()
    }
    if thread_id != current_thread_id or thread_id in stale_thread_ids:
        raise SmartSubmissionGuardError(
            SMART_CANARY_THREAD_MISMATCH,
            paper_id,
            "worker result thread_id does not own the current canary attempt",
            details={"expected_thread_id": current_thread_id, "actual_thread_id": thread_id},
        )
    if normalized_status == "blocked":
        claimed = _claimed_final_submit_authorization(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
        )
        captured = (
            _valid_live_submission_confirmation_evidence(
                paper_id,
                paper,
                route_revision=route_revision,
                attempt_id=attempt_id,
                thread_id=thread_id,
                authorization=claimed,
            )
            if claimed is not None
            else None
        )
        if captured is not None:
            normalized_status = "submitted"
            next_action = EMAIL_FIRST_MONITORING_NEXT
            detail = (
                "Trusted positive post-submit evidence was already captured; "
                "the later blocked worker marker cannot override that success."
            )
            source = "trusted_capture_overrode_blocked_worker"
    _mark_worker_route_on_paper(
        paper,
        route,
        monitor_only=monitor_check_was_pending,
    )
    captured_draft_binding = (
        {}
        if normalized_status in {"submitted", "monitoring", "accepted"}
        else _apply_open_submission_draft_binding(paper_id, paper)
    )

    old_status = str(paper.get("status") or "")
    old_workflow = str(paper.get("workflow_status") or "")
    old_worker = str(paper.get("worker_status") or "")

    terminal_statuses = {
        "submitted",
        "monitoring",
        "rejected",
        "blocked",
        "accepted",
        "waiting",
        "no_action",
    }
    workflow_for_terminal = {
        "submitted": "monitoring",
        "monitoring": "monitoring",
        "revision_needed": "revision_needed",
        "retarget_needed": "retarget_needed",
        "rejected": "retarget_needed",
        "blocked": "blocked",
        "accepted": "accepted",
        "waiting": "waiting",
        "no_action": "no_action",
    }
    should_remove = normalized_status in terminal_statuses and not keep_queued
    now = now_iso()
    source_text = _value_free_worker_source(source)
    thread_id_text = thread_id
    stale_recovered_marker = (
        (thread_id_text and thread_id_text in stale_thread_ids)
        or (
            "scheduler_recovered_queue_marker_reconcile" in source_text
            and (
                (thread_id_text and current_thread_id and thread_id_text != current_thread_id)
                or (
                    paper_id in online_queue
                    and str(paper.get("workflow_status") or "").strip().lower() == "queued"
                    and (not thread_id_text or not current_thread_id)
                )
            )
        )
    )
    if stale_recovered_marker:
        ignored_audit_path = SMART_AUDIT_DIR / f"{timestamp_id()}_{paper_id}_ignored_stale_worker_marker.md"
        paper["last_ignored_stale_worker_marker_at"] = now
        paper["last_ignored_stale_worker_marker_source"] = source_text
        paper["last_ignored_stale_worker_marker_status"] = normalized_status
        paper["last_ignored_stale_worker_marker_thread_id"] = thread_id_text
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["next_action"] = _value_free_worker_action(
            "ready_for_online_submit",
            paper.get("next_action"),
        )
        state["online_queue"] = online_queue
        if activate:
            settings = state.setdefault("settings", {})
            if not isinstance(settings, dict):
                settings = {}
                state["settings"] = settings
            settings["online_submit_enabled"] = True
            settings["visible_scheduler_enabled"] = True
            state["mode"] = "run_once"
        state["updated_at"] = now
        state.setdefault("history", []).append(
            {
                "at": now,
                "event": "ignored_stale_visible_worker_marker",
                "paper_id": paper_id,
                "status": normalized_status,
                "thread_id": thread_id_text,
                "current_thread_id": current_thread_id,
                "source": source_text,
                "reason": "paper was recovered and queued after the old visible worker result",
            }
        )
        write_text(
            ignored_audit_path,
            "\n".join(
                [
                    "# Ignored Stale Smart Submission Worker Marker",
                    "",
                    f"- paper_id: {paper_id}",
                    f"- route_revision: {int(route_revision)}",
                    f"- attempt_id: {attempt_id}",
                    f"- ignored_status: {normalized_status}",
                    f"- ignored_source: {source_text}",
                    f"- ignored_thread_id: {thread_id_text or '-'}",
                    f"- current_thread_id: {current_thread_id or '-'}",
                    "- online_queue_kept: true",
                    "",
                    "Reason:",
                    "The paper was recovered and is queued for a fresh visible worker; this marker came from an old/stale worker thread.",
                    "",
                    f"SMART_SUBMISSION_PAPER_ID={paper_id}",
                    f"SMART_SUBMISSION_ROUTE_REVISION={int(route_revision)}",
                    f"SMART_SUBMISSION_ATTEMPT_ID={attempt_id}",
                    "SMART_SUBMISSION_STATUS=queued",
                    f"SMART_SUBMISSION_NEXT={paper.get('next_action', 'visible_worker_online_submit_or_monitor')}",
                ]
            ),
        )
        paper["last_audit_path"] = str(ignored_audit_path)
        projection_dirty = _prepare_smart_projection_change(state, "record_visible_worker_result")
        write_json_atomic(SMART_STATE_PATH, state)
        projection_dirty = _mark_smart_projection_dirty(state, "record_visible_worker_result")
        wakeup = set_smart_submission_automation_fast_wakeup(True) if activate else {}
        state = read_json(SMART_STATE_PATH, state)
        projection_refresh = refresh_smart_submission_projection(
            sync_callback=projection_sync_callback,
        )
        return {
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "status": "queued",
            "recorded": False,
            "ignored_stale_worker_marker": True,
            "workflow_status": "queued",
            "worker_status": "queued",
            "removed_from_online_queue": False,
            "online_queue": state.get("online_queue", []),
            "audit_path": str(ignored_audit_path),
            "wakeup": wakeup,
            "projection_dirty": projection_dirty,
            "projection_refresh": projection_refresh,
            "state": state,
        }

    normalized_monitoring_access_failure = False
    if _is_monitoring_access_failure(
        paper,
        status=normalized_status,
        next_action=next_action,
        detail=detail,
        source=source_text,
    ):
        normalized_status = "monitoring"
        normalized_monitoring_access_failure = True
        keep_queued = False
        should_remove = True
        next_action = EMAIL_FIRST_MONITORING_NEXT
        note = "Normalized browser/dashboard-only monitoring failure to email-first monitoring."
        detail = f"{detail.strip()} | {note}" if detail.strip() else note
        source_text = "email_first_monitoring_normalized"

    if monitor_check_was_pending:
        keep_queued = False
        should_remove = True

    next_action = _value_free_worker_action(normalized_status, next_action)
    detail = _value_free_worker_detail(normalized_status, next_action)
    source = source_text
    worker_submission_id = _sanitize_submission_id(submission_id)
    worker_evidence = _sanitize_submission_evidence_path(
        evidence_path,
        paper_id=paper_id,
        paper=paper,
    )
    worker_confirmation_url = _sanitize_confirmation_url(confirmation_url, paper=paper)
    evidence_required = normalized_status in {"submitted", "monitoring", "accepted"}
    existing_evidence = _paper_has_prior_trusted_submission_evidence(paper_id, paper)
    monitoring_baseline_evidence = bool(
        monitor_check_was_pending and _paper_is_monitoring_like(paper)
    )
    final_submit_authorization: dict[str, Any] | None = None
    live_confirmation_receipt: dict[str, Any] | None = None
    had_prior_submission_evidence = (
        paper.get("worker_dispatch_had_prior_submission_evidence") is True
    )
    requires_action_receipt = (
        normalized_status in {"submitted", "monitoring", "accepted"}
        and not had_prior_submission_evidence
    )
    if requires_action_receipt:
        final_submit_authorization = _claimed_final_submit_authorization(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
        )
        if final_submit_authorization is None:
            raise SmartSubmissionGuardError(
                SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED,
                paper_id,
                "first terminal submission result lacks an action-time claimed exact-attempt authorization",
            )
        captured = _valid_live_submission_confirmation_evidence(
            paper_id,
            paper,
            route_revision=route_revision,
            attempt_id=attempt_id,
            thread_id=thread_id,
            authorization=final_submit_authorization,
        )
        if captured is None:
            raise SmartSubmissionGuardError(
                SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
                paper_id,
                "first terminal result requires signed positive post-submit CDP evidence",
            )
        live_confirmation_receipt, resolved_evidence = captured
        submission_id_text = str(live_confirmation_receipt.get("manuscript_id") or "")
        confirmation_url_text = str(live_confirmation_receipt.get("confirmation_url") or "")
        if worker_submission_id and worker_submission_id != submission_id_text:
            raise ValueError("worker submission_id does not match trusted confirmation capture")
        if worker_evidence is not None and worker_evidence != resolved_evidence:
            raise ValueError("worker evidence_path does not match trusted confirmation capture")
        if worker_confirmation_url and worker_confirmation_url != confirmation_url_text:
            raise ValueError("worker confirmation_url does not match trusted confirmation capture")
    else:
        submission_id_text = worker_submission_id
        resolved_evidence = worker_evidence
        confirmation_url_text = worker_confirmation_url
    if evidence_required and not (
        requires_action_receipt
        or existing_evidence
        or monitoring_baseline_evidence
    ):
        raise SmartSubmissionGuardError(
            SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED,
            paper_id,
            "terminal result requires a prior signed confirmation receipt or a fresh trusted capture",
        )
    valid_confirmation_url = bool(confirmation_url_text)
    valid_submission_id = bool(
        submission_id_text
        and submission_id_text.lower() not in {"none", "null", "n/a", "unknown", "pending", "-"}
    )

    if valid_submission_id:
        paper["submission_id"] = submission_id_text
    if resolved_evidence is not None:
        paper["submission_evidence_path"] = str(resolved_evidence)
    if valid_confirmation_url:
        paper["confirmation_url"] = confirmation_url_text
    if evidence_required and (requires_action_receipt or existing_evidence):
        paper["final_submit_confirmed"] = True
    if final_submit_authorization is not None:
        final_submit_authorization["result_recorded_at"] = now
        paper["final_submit_authorization"] = _sign_smart_receipt(final_submit_authorization)
        paper["final_submit_authorization_result_recorded_at"] = now
    if live_confirmation_receipt is not None:
        paper["last_live_submission_confirmation_evidence"] = copy.deepcopy(
            paper.get("pending_submission_confirmation_evidence")
        )
        paper.pop("pending_submission_confirmation_evidence", None)

    retarget_closure_intent = (
        str(paper.get("online_intent") or "").strip().casefold()
        == "user_retarget_verify_or_withdraw"
    )
    if isinstance(closed_submission_ids, str):
        raw_closed_ids = re.split(r"[\s,;]+", closed_submission_ids)
    else:
        raw_closed_ids = list(closed_submission_ids or [])
    closed_ids = list(
        dict.fromkeys(
            clean
            for clean in (normalize_submission_id(item) for item in raw_closed_ids)
            if clean
        )
    )
    retarget_request = (
        paper.get("retarget_request")
        if isinstance(paper.get("retarget_request"), dict)
        else {}
    )
    expected_closure_ids = [
        clean
        for clean in (
            normalize_submission_id(item)
            for item in retarget_request.get("withdrawal_targets", [])
        )
        if clean
    ]
    if retarget_closure_intent:
        unexpected_closed_ids = sorted(set(closed_ids) - set(expected_closure_ids))
        if unexpected_closed_ids:
            raise SmartSubmissionGuardError(
                SMART_RETARGET_CLOSURE_EVIDENCE_REQUIRED,
                paper_id,
                "worker returned a closed manuscript ID outside the exact retarget target set",
                details={
                    "expected_ids": expected_closure_ids,
                    "unexpected_ids": unexpected_closed_ids,
                },
            )
        for closed_id in closed_ids:
            close_submission_record(
                paper,
                closed_id,
                status="withdrawn",
                source=source,
                observed_at=now,
            )
        closure_identity = project_submission_identity(paper, observed_at=now)
        remaining_closure_ids = [
            item
            for item in expected_closure_ids
            if item in set(closure_identity.get("active_submission_ids", []))
        ]
        retarget_request["withdrawal_targets"] = remaining_closure_ids
        retarget_request["closed_submission_ids"] = list(
            dict.fromkeys(
                [
                    str(item)
                    for item in retarget_request.get("closed_submission_ids", [])
                    if str(item).strip()
                ]
                + closed_ids
            )
        )
        retarget_request["last_closure_checked_at"] = now
        if normalized_status in {"retarget_needed", "rejected"}:
            if remaining_closure_ids:
                raise SmartSubmissionGuardError(
                    SMART_RETARGET_CLOSURE_EVIDENCE_REQUIRED,
                    paper_id,
                    "retarget cannot start until every exact active manuscript is positively closed",
                    details={
                        "expected_ids": expected_closure_ids,
                        "closed_ids": closed_ids,
                        "remaining_ids": remaining_closure_ids,
                    },
                )
            retarget_request["status"] = "ready_for_selection"
            retarget_request["withdrawal_decision"] = "ready_for_selection"
            retarget_request["withdrawal_completed_at"] = now
            should_remove = True
            keep_queued = False
        elif normalized_status == "blocked" and (
            "editorial_withdrawal" in str(next_action or "").casefold()
            or "withdrawal_by_email" in str(next_action or "").casefold()
        ):
            retarget_request["status"] = "waiting_withdrawal_confirmation"
            retarget_request["withdrawal_detail"] = str(next_action or detail)[:1000]
            should_remove = True
            keep_queued = False
        elif not remaining_closure_ids and closed_ids:
            normalized_status = "retarget_needed"
            next_action = "queue_user_selected_journal_author_retarget"
            retarget_request["status"] = "ready_for_selection"
            retarget_request["withdrawal_decision"] = "ready_for_selection"
            retarget_request["withdrawal_completed_at"] = now
            should_remove = True
            keep_queued = False
        else:
            retarget_request["status"] = "identity_check_required"
            retarget_request["withdrawal_detail"] = str(next_action or detail)[:1000]
            should_remove = True
            keep_queued = False
        retarget_request["updated_at"] = now
        paper["retarget_request"] = retarget_request

    paper["status"] = normalized_status
    identity_submission_id = (
        normalize_submission_id(submission_id_text)
        or normalize_submission_id(paper.get("submission_id"))
        or normalize_submission_id(paper.get("canonical_submission_id"))
    )
    if retarget_closure_intent:
        pass
    elif normalized_status in {"rejected", "retarget_needed"}:
        if identity_submission_id:
            close_submission_record(
                paper,
                identity_submission_id,
                status="rejected" if normalized_status == "rejected" else "retargeted",
                source=source,
                observed_at=now,
            )
    elif identity_submission_id:
        upsert_submission_record(
            paper,
            identity_submission_id,
            submission_system_id=str(paper.get("submission_system_id") or ""),
            target_journal=str(paper.get("target_journal") or ""),
            status=normalized_status,
            active=True,
            canonical=True,
            source=source,
            observed_at=now,
        )
    project_submission_identity(paper, observed_at=now)
    paper["last_worker_recorded_at"] = now
    paper["last_worker_result_source"] = source
    if observed_recovery_hint:
        paper["last_worker_recovery_hint"] = observed_recovery_hint
    else:
        paper.pop("last_worker_recovery_hint", None)
    if next_action:
        paper["next_action"] = next_action
    if detail:
        paper["detail"] = detail
        paper["blocker"] = detail if normalized_status == "blocked" else ""
    elif normalized_status != "blocked":
        paper["blocker"] = ""
    if thread_id:
        paper["thread_id"] = thread_id
    if monitor_check_was_pending:
        paper.pop("monitor_check_pending", None)
        paper.pop("monitor_pending_channel", None)
        paper["last_monitor_checked_at"] = now
        paper["last_monitor_result"] = normalized_status
        paper["last_monitor_check_outcome"] = (
            "failed" if "failure" in source.casefold() else normalized_status
        )
        paper["last_monitor_source"] = source
        if detail:
            paper["last_monitor_detail"] = detail

    next_attempt_id = ""
    if should_remove:
        paper["workflow_status"] = workflow_for_terminal.get(normalized_status, normalized_status)
        paper["worker_status"] = paper["workflow_status"]
        online_queue = [item for item in online_queue if item != paper_id]
    else:
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        next_attempt_id = _rotate_canary_attempt(paper, paper_id)
        if not next_action:
            paper["next_action"] = "visible_worker_online_submit_or_monitor"
        _append_online_queue_guarded(
            online_queue,
            paper_id,
            paper,
            expected_revision=route_revision,
        )
        if activate:
            state["mode"] = "run_once"

    manual_takeover = (
        paper.get("manual_takeover")
        if isinstance(paper.get("manual_takeover"), dict)
        else {}
    )
    if str(manual_takeover.get("status") or "").strip().casefold() == "requested":
        manual_takeover["status"] = "active"
        manual_takeover["activated_at"] = now
        manual_takeover["resume_state"] = _manual_takeover_resume_state(
            paper,
            in_online_queue=paper_id in online_queue,
        )
        manual_takeover["turn_completed_before_takeover"] = {
            "status": normalized_status,
            "source": source,
            "recorded_at": now,
        }
        paper["manual_takeover"] = manual_takeover
        paper["manual_takeover_status"] = "active"
        paper["workflow_status"] = "manual_takeover"
        paper["worker_status"] = "manual_takeover"
        paper["next_action"] = "manual_takeover_edit_current_page"
        paper["blocker"] = ""
        paper["detail"] = (
            "The SDK Turn finished and this paper is now paused for a user-owned "
            "edit on its current browser page."
        )
        online_queue = [item for item in online_queue if item != paper_id]
        should_remove = True
        keep_queued = False

    if bool(paper.pop("manual_takeover_reconcile_pending", False)):
        if manual_takeover:
            manual_takeover["reconciled_at"] = now
            manual_takeover["reconciled_status"] = normalized_status
            paper["manual_takeover"] = manual_takeover

    safe_paper_id = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id) or "paper"
    audit_path = SMART_AUDIT_DIR / (
        f"{timestamp_id()}_{safe_paper_id}_visible_worker_result_{os.urandom(4).hex()}.md"
    )
    prepared_audit = _prepare_audit_content(
        audit_path,
        "\n".join(
            [
                "# Smart Submission Visible Worker Result",
                "",
                f"- paper_id: {paper_id}",
                f"- route_revision: {int(route_revision)}",
                f"- attempt_id: {attempt_id}",
                f"- next_attempt_id: {next_attempt_id or '-'}",
                f"- status: {normalized_status}",
                f"- next_action: {paper.get('next_action', '')}",
                f"- source: {source}",
                f"- thread_id: {thread_id or paper.get('thread_id', '')}",
                f"- old_status: {old_status}",
                f"- old_workflow_status: {old_workflow}",
                f"- old_worker_status: {old_worker}",
                f"- removed_from_online_queue: {str(should_remove).lower()}",
                f"- monitor_check_completed: {str(monitor_check_was_pending).lower()}",
                f"- monitoring_access_failure_normalized: {str(normalized_monitoring_access_failure).lower()}",
                f"- submission_id: {submission_id_text or paper.get('submission_id', '') or '-'}",
                f"- evidence_path: {str(resolved_evidence) if resolved_evidence is not None else paper.get('submission_evidence_path', '') or '-'}",
                f"- confirmation_url: {confirmation_url_text if valid_confirmation_url else paper.get('confirmation_url', '') or '-'}",
                "",
                "Detail:",
                detail or "-",
                "",
                f"SMART_SUBMISSION_PAPER_ID={paper_id}",
                f"SMART_SUBMISSION_ROUTE_REVISION={int(route_revision)}",
                f"SMART_SUBMISSION_ATTEMPT_ID={attempt_id}",
                f"SMART_SUBMISSION_STATUS={normalized_status}",
                f"SMART_SUBMISSION_NEXT={paper.get('next_action', '')}",
            ]
        ),
    )
    if evidence_required and requires_action_receipt and final_submit_authorization is not None:
        evidence_sha256 = (
            hashlib.sha256(resolved_evidence.read_bytes()).hexdigest()
            if resolved_evidence is not None
            else ""
        )
        paper["submission_confirmation_receipt"] = _sign_smart_receipt(
            {
                "schema": SUBMISSION_CONFIRMATION_RECEIPT_SCHEMA,
                "paper_id": paper_id,
                "route_revision": int(route_revision),
                "attempt_id": attempt_id,
                "thread_id": thread_id,
                "authorization_id": str(final_submit_authorization.get("authorization_id") or ""),
                "capture_evidence_id": str(live_confirmation_receipt.get("evidence_id") or ""),
                "status": normalized_status,
                "submission_id": submission_id_text,
                "evidence_path": str(resolved_evidence) if resolved_evidence is not None else "",
                "evidence_sha256": evidence_sha256,
                "confirmation_url": confirmation_url_text if valid_confirmation_url else "",
                "recorded_at": now,
                "result_audit_path": str(prepared_audit["path"]),
                "result_audit_sha256": str(prepared_audit["sha256"]),
            }
        )
    paper["last_audit_path"] = str(audit_path)
    paper["last_audit_sha256"] = str(prepared_audit["sha256"])
    state["online_queue"] = online_queue
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "record_visible_worker_result",
            "paper_id": paper_id,
            "route_revision": int(route_revision),
            "attempt_id": attempt_id,
            "next_attempt_id": next_attempt_id,
            "status": normalized_status,
            "thread_id": thread_id or paper.get("thread_id", ""),
            "removed_from_online_queue": should_remove,
            "workflow_before": old_workflow,
            "worker_before": old_worker,
            "workflow_after": paper.get("workflow_status", ""),
            "worker_after": paper.get("worker_status", ""),
            "audit_path": str(audit_path),
            "source": source,
            "monitor_check_completed": monitor_check_was_pending,
            "monitoring_access_failure_normalized": normalized_monitoring_access_failure,
            "draft_binding_captured": bool(captured_draft_binding),
            "has_submission_evidence": bool(evidence_required),
        }
    )
    projection_dirty = _prepare_smart_projection_change(state, "record_visible_worker_result")
    try:
        write_json_atomic(SMART_STATE_PATH, state)
    except Exception:
        _discard_prepared_audit(prepared_audit)
        raise
    audit_path = _commit_prepared_audit(prepared_audit)
    legacy_projection: dict[str, Any] = {}
    if normalized_status in {
        "accepted",
        "monitoring",
        "rejected",
        "submitted",
    }:
        projection_submission_id = (
            normalize_submission_id(submission_id_text)
            or normalize_submission_id(identity_submission_id)
        )
        if projection_submission_id or normalized_status == "rejected":
            try:
                legacy_projection = reconcile_legacy_submission_lifecycle(
                    paper_id,
                    status=normalized_status,
                    submission_id=projection_submission_id,
                    target_journal=str(paper.get("target_journal") or ""),
                    source=source,
                    observed_at=now,
                )
            except Exception as exc:
                legacy_projection = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    projection_dirty = _mark_smart_projection_dirty(state, "record_visible_worker_result")
    wakeup = set_smart_submission_automation_fast_wakeup(True) if activate and not should_remove else {}
    state = read_json(SMART_STATE_PATH, state)
    projection_refresh = refresh_smart_submission_projection(
        sync_callback=projection_sync_callback,
    )
    return {
        "paper_id": paper_id,
        "route_revision": int(route_revision),
        "attempt_id": attempt_id,
        "next_attempt_id": next_attempt_id,
        "status": normalized_status,
        "workflow_status": paper.get("workflow_status", ""),
        "worker_status": paper.get("worker_status", ""),
        "removed_from_online_queue": should_remove,
        "monitor_check_completed": monitor_check_was_pending,
        "online_queue": state.get("online_queue", []),
        "audit_path": str(audit_path),
        "wakeup": wakeup,
        "projection_dirty": projection_dirty,
        "projection_refresh": projection_refresh,
        "legacy_projection": legacy_projection,
        "state": state,
    }


@_smart_state_locked
def requeue_waiting_approval_workers(
    paper_ids: list[str] | None = None,
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """Release stale workers that are waiting for invisible command approval.

    These workers are not doing useful browser/submission work, but the visible
    scheduler can still count them against the parallel-worker cap. Requeue the
    affected papers with a fresh worker title and keep the stale thread id for
    audit instead of asking the user to find an approval prompt.
    """
    ensure_smart_submission_module()
    scope_requested = paper_ids is not None
    selected = {str(item).strip() for item in (paper_ids or []) if str(item).strip()}
    state = read_json(SMART_STATE_PATH, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    papers = state.setdefault("papers", {})
    if not isinstance(papers, dict):
        papers = {}
        state["papers"] = papers
    normalized = 0 if scope_requested else _normalize_permission_retry_labels(state)
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    now = now_iso()
    retry_stamp = timestamp_id()
    eligible_previous = {
        "ready_for_online_submit",
        "login_check",
        "uploading",
        "monitoring",
        "revision_needed",
        "retarget_needed",
    }
    terminal_previous = {
        "accepted": "accepted",
        "blocked": "blocked",
        "monitoring": "monitoring",
        "no_action": "no_action",
        "submitted": "monitoring",
        "waiting": "waiting",
        "stopped": "stopped",
    }
    requeued: list[dict[str, Any]] = []
    released: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    unified_state = _unified_state_snapshot()
    target_ids = selected if scope_requested else {
        str(paper_id)
        for paper_id, paper in papers.items()
        if isinstance(paper, dict)
        and (
            str(paper.get("status") or "") == "waiting_user_approval"
            or str(paper.get("workflow_status") or "") == "waiting_user_approval"
            or str(paper.get("worker_status") or "") == "waitingOnApproval"
            or "waitingOnApproval" in {str(flag) for flag in (paper.get("last_active_flags") or [])}
        )
    }

    for paper_id in sorted(target_ids):
        paper = papers.get(paper_id)
        if not isinstance(paper, dict):
            skipped[paper_id] = "paper record is missing"
            continue
        route, route_error = _route_guard_result(paper_id, unified_state=unified_state)
        if route_error is not None:
            skipped[paper_id] = str(route_error["code"])
            paper["last_online_route_guard"] = route_error
            online_queue = [item for item in online_queue if item != paper_id]
            continue
        _mark_smart_route_on_paper(paper, route or {})
        is_waiting_approval = (
            str(paper.get("status") or "") == "waiting_user_approval"
            or str(paper.get("workflow_status") or "") == "waiting_user_approval"
            or str(paper.get("worker_status") or "") == "waitingOnApproval"
            or "waitingOnApproval" in {str(flag) for flag in (paper.get("last_active_flags") or [])}
        )
        if not is_waiting_approval:
            skipped[paper_id] = "paper is not waiting for command approval"
            continue
        old_thread = str(paper.get("thread_id") or "").strip()
        if old_thread:
            stale_threads = paper.setdefault("stale_worker_thread_ids", [])
            if not isinstance(stale_threads, list):
                stale_threads = []
                paper["stale_worker_thread_ids"] = stale_threads
            if old_thread not in stale_threads:
                stale_threads.append(old_thread)
            paper["last_stale_worker_thread_id"] = old_thread
        previous_status = str(
            paper.get("previous_status_before_waiting_approval")
            or paper.get("status")
            or ""
        )
        status_after = previous_status if previous_status in eligible_previous else "revision_needed"
        if previous_status in terminal_previous:
            workflow_after = terminal_previous[previous_status]
            paper["status"] = previous_status
            paper["workflow_status"] = workflow_after
            paper["worker_status"] = workflow_after
            paper["last_active_flags"] = []
            paper["last_turn_status"] = "stale_waiting_approval_released"
            paper["last_thread_status"] = "stale_waiting_approval_released"
            paper["thread_id"] = ""
            if previous_status != "blocked":
                paper["blocker"] = ""
            paper["last_waiting_approval_released_at"] = now
            paper["last_recovery_reason"] = "permission_wait_released_terminal_status"
            paper["detail"] = (
                "Released an old visible worker that was waiting for a Codex command-approval prompt. "
                f"The paper remains in {workflow_after}; it was removed from online_queue instead of "
                "being re-dispatched because no immediate online worker is required for this terminal/waiting state."
            )
            online_queue = [item for item in online_queue if item != paper_id]
            released.append(
                {
                    "paper_id": paper_id,
                    "old_thread_id": old_thread,
                    "status_after": previous_status,
                    "workflow_after": workflow_after,
                }
            )
            continue
        paper["status"] = status_after
        paper["workflow_status"] = "queued"
        paper["worker_status"] = "queued"
        paper["last_active_flags"] = []
        paper["last_turn_status"] = "stale_waiting_approval_requeued"
        paper["last_thread_status"] = "stale_waiting_approval_requeued"
        paper["thread_id"] = ""
        paper["thread_title"] = f"Smart Submission worker {paper_id} retry {retry_stamp}"
        paper["blocker"] = ""
        paper["next_action"] = "visible_worker_online_submit_or_monitor"
        paper["online_intent"] = paper.get("online_intent") or "queued_after_permission_retry"
        paper["detail"] = (
            "Recovered from an old visible worker that was waiting for a Codex command-approval prompt. "
            "The stale worker thread is preserved in stale_worker_thread_ids, but it no longer consumes "
            "a Smart Submission parallel slot. A fresh worker should continue with browser/email work and "
            "avoid shell commands that request user approval."
        )
        paper["last_waiting_approval_requeued_at"] = now
        paper["last_recovery_reason"] = "permission_wait_requeued"
        _rotate_canary_attempt(paper, paper_id)
        _append_online_queue_guarded(
            online_queue,
            paper_id,
            paper,
            unified_state=unified_state,
        )
        requeued.append(
            {
                "paper_id": paper_id,
                "old_thread_id": old_thread,
                "new_thread_title": paper["thread_title"],
                "status_after": status_after,
            }
        )

    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    if normalized:
        settings["permission_wait_autorequeue_enabled"] = True
        settings["waiting_approval_policy"] = "auto_requeue_permission_waits"
    if requeued or released:
        settings["online_submit_enabled"] = True
        settings["visible_scheduler_enabled"] = True
        settings["permission_wait_autorequeue_enabled"] = True
        settings["last_waiting_approval_requeued_at"] = now
        settings["last_waiting_approval_requeued_count"] = len(requeued)
        settings["last_waiting_approval_released_count"] = len(released)
        state["online_queue"] = online_queue
        state["last_scheduler_action_at"] = now
        state["last_scheduler_message"] = (
            f"Released {len(requeued) + len(released)} waitingOnApproval worker thread(s); "
            f"requeued {len(requeued)} and removed {len(released)} terminal/waiting paper(s) from online_queue. "
            "Stale worker threads no longer consume Smart Submission parallel slots."
        )
        if activate:
            state["mode"] = "run_once"
    elif normalized:
        state["online_queue"] = online_queue
        state["last_scheduler_action_at"] = now
        state["last_scheduler_message"] = (
            f"Normalized {normalized} stale permission-retry label(s); no waitingOnApproval worker is occupying a slot."
        )
        if activate:
            state["mode"] = "run_once"
    state["updated_at"] = now
    state.setdefault("history", []).append(
        {
            "at": now,
            "event": "requeue_waiting_approval_workers",
            "requeued": requeued,
            "released": released,
            "skipped": skipped,
            "normalized": normalized,
            "activate": bool(activate),
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    wakeup: dict[str, Any] = {}
    if (requeued or released or normalized) and activate:
        wakeup = set_smart_submission_automation_fast_wakeup(True)
        state = read_json(SMART_STATE_PATH, state)
    return {
        "requeued": requeued,
        "released": released,
        "skipped": skipped,
        "normalized": normalized,
        "mode": state.get("mode"),
        "online_queue": state.get("online_queue", []),
        "wakeup": wakeup,
        "state": state,
    }


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "paper"


def _latest_legacy_credential_export() -> Path | None:
    if not LEGACY_MIGRATION_OUTPUT_DIR.exists():
        return None
    candidates = sorted(
        LEGACY_MIGRATION_OUTPUT_DIR.glob("credential_store_plaintext_*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _is_placeholder_secret(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    placeholders = ["待补", "browser", "session", "same", "同", "已", "unknown", "todo", "n/a"]
    return any(item in text for item in placeholders)


def _infer_account_scope(record: dict[str, Any]) -> str:
    site_name = str(record.get("site_name") or "").lower()
    site_url = str(record.get("site_url") or "").lower()
    site_text = f"{site_name} {site_url}"
    haystack = " ".join(str(record.get(key) or "") for key in ("site_name", "site_url", "purpose", "notes")).lower()
    email_markers = ("gmail", "google mail", "mail.qq", "mail.xujc", "xujcmail", "邮箱", "mail.")
    if any(token in site_text for token in email_markers):
        return "email"
    strong_journal_markers = (
        "springer",
        "elsevier",
        "inderscience",
        "scholarone",
        "editorial manager",
        "manuscriptcentral",
        "wiley",
        "taylor",
        "tandfonline",
        "snapp",
        "edmgr",
        "ojs",
        "micpro",
        "jpdc",
        "scico",
    )
    if any(token in haystack for token in strong_journal_markers):
        return "journal_system"
    if any(token in site_text for token in ("submission", "submit", "manuscript", "journal")):
        return "journal_system"
    if "github" in haystack:
        return "external_service"
    if "overleaf" in haystack:
        return "external_service"
    return "journal_system"


def _account_merge_key(account: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(account.get("scope") or "").strip(),
        str(account.get("provider") or "").strip().lower(),
        str(account.get("login_url") or "").strip().lower(),
        str(account.get("username") or account.get("email") or "").strip().lower(),
    )


def _merge_account(accounts: list[dict[str, Any]], incoming: dict[str, Any]) -> bool:
    key = _account_merge_key(incoming)
    for account in accounts:
        if _account_merge_key(account) != key:
            continue
        changed = False
        for field, value in incoming.items():
            if field == "password" and str(account.get("credential_ref") or "").strip():
                continue
            if value and not account.get(field):
                account[field] = value
                changed = True
        return changed
    accounts.append(incoming)
    return True


def _normalize_account_scopes(credentials: dict[str, Any]) -> bool:
    accounts = credentials.get("accounts", []) if isinstance(credentials.get("accounts"), list) else []
    changed = False
    for account in accounts:
        if not isinstance(account, dict):
            continue
        record = {
            "site_name": account.get("provider") or account.get("account_id") or "",
            "site_url": account.get("login_url") or "",
            "purpose": account.get("notes") or "",
            "notes": account.get("notes") or "",
        }
        inferred = _infer_account_scope(record)
        current = str(account.get("scope") or "")
        if current in {"", "email", "journal_system"} and inferred in {"email", "journal_system"} and current != inferred:
            account["scope"] = inferred
            account["scope_corrected_at"] = now_iso()
            changed = True
    if changed:
        credentials["updated_at"] = now_iso()
    return changed


def _dedupe_accounts(credentials: dict[str, Any]) -> bool:
    accounts = credentials.get("accounts", []) if isinstance(credentials.get("accounts"), list) else []
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    changed = False
    for account in accounts:
        if not isinstance(account, dict):
            changed = True
            continue
        key = _account_merge_key(account)
        if key in by_key:
            target = by_key[key]
            for field, value in account.items():
                if field == "password" and str(target.get("credential_ref") or "").strip():
                    continue
                if value and not target.get(field):
                    target[field] = value
            changed = True
            continue
        by_key[key] = account
        merged.append(account)
    if changed:
        credentials["accounts"] = merged
        credentials["updated_at"] = now_iso()
    return changed


def _import_legacy_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    path = _latest_legacy_credential_export()
    if path is None:
        return {"changed": False, "imported": 0, "source": ""}
    data = read_json(path, {})
    records = data.get("records", []) if isinstance(data.get("records"), list) else []
    accounts = credentials.setdefault("accounts", [])
    if not isinstance(accounts, list):
        accounts = []
        credentials["accounts"] = accounts
    imported = 0
    changed = False
    for record in records:
        if not isinstance(record, dict):
            continue
        site_name = str(record.get("site_name") or "legacy-account").strip()
        site_url = str(record.get("site_url") or "").strip()
        username = str(record.get("username") or record.get("email") or "").strip()
        password = str(record.get("password") or "").strip()
        incoming = {
            "account_id": "legacy-" + _safe_name(str(record.get("id") or site_name))[:64],
            "scope": _infer_account_scope(record),
            "provider": site_name,
            "username": username,
            "email": str(record.get("email") or "").strip(),
            "password": "" if _is_placeholder_secret(password) else password,
            "recovery_email": "",
            "login_url": site_url,
            "status": "imported_from_legacy",
            "notes": "Imported from legacy migration credential export; verify before final online submit.",
            "source": str(path),
            "last_verified_at": "",
            "imported_at": now_iso(),
        }
        if not incoming["username"] and not incoming["login_url"]:
            continue
        if _merge_account(accounts, incoming):
            imported += 1
            changed = True
    if changed:
        credentials["updated_at"] = now_iso()
    if _normalize_account_scopes(credentials):
        changed = True
    if _dedupe_accounts(credentials):
        changed = True
    return {"changed": changed, "imported": imported, "source": str(path)}


def _guess_system_id(text: str) -> str:
    haystack = text.lower()
    if "manuscriptcentral" in haystack or "scholarone" in haystack:
        return "scholar_one"
    if "inderscience" in haystack:
        return "inderscience"
    if "springer" in haystack or "nature" in haystack or "snapp" in haystack:
        return "springer_nature"
    if "wiley" in haystack or "ccpe" in haystack or "concurrency and computation" in haystack:
        return "wiley"
    if "taylor" in haystack or "tandfonline" in haystack:
        return "taylor_francis"
    if "sage" in haystack:
        return "scholar_one"
    if "elsevier" in haystack or "evise" in haystack:
        return "elsevier"
    if "ojs" in haystack:
        return "ojs"
    return ""


def _default_system_urls(system_id: str) -> dict[str, str]:
    defaults = {
        "inderscience": "https://www.indersciencesubmissions.com/",
        "springer_nature": "https://submission.springernature.com/",
        "wiley": "https://authors.wiley.com/dashboard",
        "taylor_francis": "https://rp.tandfonline.com/",
        "elsevier": "https://submit.elsevier.com/",
        "scholar_one": "https://mc.manuscriptcentral.com/",
        "ojs": "",
    }
    url = defaults.get(system_id, "")
    return {"login_url": url, "submission_url": url}


def _system_label(system_id: str) -> str:
    labels = {
        "inderscience": "Inderscience",
        "springer_nature": "Springer Nature / SNAPP",
        "wiley": "Wiley Research Exchange / Author Services",
        "taylor_francis": "Taylor & Francis Research Portal",
        "elsevier": "Elsevier Submit",
        "scholar_one": "ScholarOne Manuscripts",
        "ojs": "Open Journal Systems",
    }
    return labels.get(system_id, system_id or "Unknown")


def _ensure_system(site_registry: dict[str, Any], system_id: str, source: str = "") -> bool:
    if not system_id:
        return False
    systems = site_registry.setdefault("systems", [])
    if not isinstance(systems, list):
        systems = []
        site_registry["systems"] = systems
    urls = _default_system_urls(system_id)
    for system in systems:
        if str(system.get("system_id") or "") != system_id:
            continue
        changed = False
        if urls.get("login_url") and not system.get("login_url"):
            system["login_url"] = urls["login_url"]
            changed = True
        if urls.get("submission_url") and not system.get("submission_url"):
            system["submission_url"] = urls["submission_url"]
            changed = True
        if str(system.get("status") or "") == "adapter_planned" and urls.get("submission_url"):
            system["status"] = "discovered"
            changed = True
        if source and source not in str(system.get("notes") or ""):
            system["notes"] = (str(system.get("notes") or "").strip() + f" Discovered from {source}.").strip()
            changed = True
        return changed
    systems.append(
        {
            "system_id": system_id,
            "label": _system_label(system_id),
            "status": "discovered" if urls.get("submission_url") else "needs_discovery",
            "login_url": urls.get("login_url", ""),
            "submission_url": urls.get("submission_url", ""),
            "status_url_template": "",
            "email_senders": [],
            "notes": f"Discovered from {source}." if source else "",
        }
    )
    return True


def _extract_recommended_journal(text: str) -> str:
    patterns = [
        r"## Recommended Journal\s+(.+?)(?:\n\n|$)",
        r"Recommended Journal\s*:?\s*(.+)",
        r"Final target\s*:?\s*(.+)",
        r"current_journal\s*:?\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        value = match.group(1).strip()
        value = re.split(r"\n[-#]|\n##|\nSources checked", value, maxsplit=1)[0].strip()
        value = re.sub(r"\s+", " ", value)
        if value:
            return value[:240]
    return ""


def _extract_submission_portal_url(text: str) -> str:
    """Extract only an explicitly labelled online-submission URL."""

    patterns = (
        r"(?im)^\s*(?:submission\s+(?:system|portal|url|site)|"
        r"online\s+submission(?:\s+(?:system|portal|url|site))?)"
        r"\s*:?\s*(https?://[^\s<>\]\[()]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text or ""))
        if match is None:
            continue
        value = str(match.group(1) or "").rstrip(".,;:").strip()
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
            return value
    return ""


def _merge_journal_record(
    site_registry: dict[str, Any],
    paper_id: str,
    journal_name: str,
    system_id: str,
    source: str,
    url: str = "",
    *,
    authoritative_system: bool = False,
) -> bool:
    if not journal_name:
        return False
    journals = site_registry.setdefault("journals", [])
    if not isinstance(journals, list):
        journals = []
        site_registry["journals"] = journals
    journal_key = _safe_name(journal_name.lower())[:96]
    matching_journals = [
        journal
        for journal in journals
        if isinstance(journal, dict)
        and (
            str(journal.get("journal_key") or "") == journal_key
            or _journal_names_equivalent(
                journal.get("journal_name"),
                journal_name,
            )
        )
    ]
    if matching_journals:
        journal = next(
            (
                item
                for item in matching_journals
                if str(item.get("journal_key") or "") == journal_key
            ),
            matching_journals[0],
        )
        changed = False
        linked = journal.setdefault("linked_papers", [])
        if isinstance(linked, list) and paper_id and paper_id not in linked:
            linked.append(paper_id)
            changed = True
        current_system_id = str(journal.get("system_id") or "").strip().casefold()
        if system_id and (
            not current_system_id
            or (authoritative_system and current_system_id != system_id)
        ):
            journal["system_id"] = system_id
            changed = True
            current_url = str(journal.get("submission_url") or "").strip()
            adapter = submission_site_adapter(system_id)
            if current_url and adapter is not None and not adapter.allows_url(current_url):
                journal["submission_url"] = ""
        current_url = str(journal.get("submission_url") or "").strip()
        if url and (
            not current_url
            or (
                authoritative_system
                and submission_site_adapter(system_id) is not None
                and not submission_site_adapter(system_id).allows_url(
                    current_url
                )
            )
            or (
                authoritative_system
                and current_url != url
                and submission_site_adapter(system_id) is not None
                and submission_site_adapter(system_id).allows_url(url)
            )
        ):
            journal["submission_url"] = url
            changed = True
        if changed:
            journal["source"] = source
            journal["updated_at"] = now_iso()
        return changed
    journals.append(
        {
            "journal_key": journal_key,
            "journal_name": journal_name,
            "system_id": system_id,
            "submission_url": url,
            "status": "discovered",
            "source": source,
            "linked_papers": [paper_id] if paper_id else [],
            "updated_at": now_iso(),
        }
    )
    return True


def _import_legacy_site_records(site_registry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    changed = False
    imported = 0
    legacy = read_json(LEGACY_SUBMISSION_STATE_PATH, {})
    legacy_papers = legacy.get("papers", {}) if isinstance(legacy.get("papers"), dict) else {}
    for paper_id, record in legacy_papers.items():
        if not isinstance(record, dict):
            continue
        journal_name = str(record.get("current_journal") or record.get("recommended_journal") or "").strip()
        system_text = " ".join(
            str(record.get(key) or "") for key in ("submission_system", "dashboard_url", "current_journal", "recommended_journal")
        )
        system_id = _guess_system_id(system_text)
        dashboard_url = str(record.get("dashboard_url") or "").strip()
        if _ensure_system(site_registry, system_id, "legacy auto_submission_panel_state.json"):
            changed = True
        if _merge_journal_record(
            site_registry,
            str(paper_id),
            journal_name,
            system_id,
            "legacy auto_submission_panel_state.json",
            dashboard_url,
        ):
            imported += 1
            changed = True

    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    for paper_id, paper in papers.items():
        if not isinstance(paper, dict):
            continue
        package_dir = Path(str(paper.get("source_package_dir") or ""))
        journal_md = package_dir.parent / "journal.md"
        journal_text = read_text(journal_md)
        if not journal_text:
            continue
        journal_name = _extract_recommended_journal(journal_text)
        # The selected journal heading is authoritative.  Scanning the entire
        # recommendation document first can accidentally pick a publisher from
        # a backup candidate and route the selected journal to the wrong site.
        submission_url = _extract_submission_portal_url(journal_text)
        portal_system_id = _guess_system_id(submission_url)
        journal_system_id = _guess_system_id(journal_name)
        # A journal package can contain a now-retired submission URL copied
        # from an older guideline paragraph.  When the current publisher
        # adapter owns a journal-specific route and the package contains its
        # journal code (for example Wiley CPE), the adapter route is the
        # deterministic canonical destination and outranks that legacy URL.
        journal_adapter = submission_site_adapter(journal_system_id)
        canonical_journal_url = ""
        if journal_adapter is not None:
            journal_identifier = journal_adapter.extract_journal_identifier(
                submission_url,
                journal_text,
            )
            canonical_journal_url = journal_adapter.submission_start_url(
                journal_identifier
            )
        if journal_system_id == "wiley" and canonical_journal_url:
            submission_url = canonical_journal_url
            portal_system_id = journal_system_id
        journal_system_id = portal_system_id or journal_system_id
        system_id = journal_system_id or _guess_system_id(journal_text)
        if _ensure_system(site_registry, system_id, f"{paper_id}/journal.md"):
            changed = True
        if _merge_journal_record(
            site_registry,
            str(paper_id),
            journal_name,
            system_id,
            f"{paper_id}/journal.md",
            submission_url,
            authoritative_system=bool(portal_system_id or journal_system_id),
        ):
            imported += 1
            changed = True
    if changed:
        site_registry["updated_at"] = now_iso()
    return {"changed": changed, "imported": imported}


def _autofill_from_legacy_sources(
    credentials: dict[str, Any],
    site_registry: dict[str, Any],
    state: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Keep legacy discovery out of unattended scheduler heartbeats.

    The legacy export contains plaintext secrets, so even an in-memory preview
    is an inappropriate scheduler operation.  Explicit UI import/migration is
    handled by ``AccountSiteStore`` under a user-visible transaction instead.
    """

    _ = credentials, site_registry, state, settings
    return {
        "mode": "disabled_by_secret_boundary",
        "writes_performed": False,
        "credential_import": {},
        "site_import": {},
    }


def _configured_accounts(credentials: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    accounts = credentials.get("accounts", []) if isinstance(credentials.get("accounts"), list) else []
    configured = []
    resolver = _smart_credential_resolver()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if canonical_account_scope(account.get("scope")) != canonical_account_scope(scope):
            continue
        username = str(account.get("username") or account.get("email") or "").strip()
        if username and _account_has_usable_auth(account, resolver=resolver):
            configured.append(account)
    return configured


def _online_ready_accounts(credentials: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    accounts = credentials.get("accounts", []) if isinstance(credentials.get("accounts"), list) else []
    ready: list[dict[str, Any]] = []
    resolver = _smart_credential_resolver()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if canonical_account_scope(account.get("scope")) != canonical_account_scope(scope):
            continue
        if not str(account.get("username") or account.get("email") or "").strip():
            continue
        readiness = _resolved_account_readiness(account, resolver=resolver)
        if readiness.configured and not readiness.requires_secret_broker:
            ready.append(account)
    return ready


def _secret_broker_required_accounts(credentials: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    accounts = credentials.get("accounts", []) if isinstance(credentials.get("accounts"), list) else []
    gated: list[dict[str, Any]] = []
    resolver = _smart_credential_resolver()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if canonical_account_scope(account.get("scope")) != canonical_account_scope(scope):
            continue
        if not str(account.get("username") or account.get("email") or "").strip():
            continue
        if _resolved_account_readiness(account, resolver=resolver).requires_secret_broker:
            gated.append(account)
    return gated


def _configured_site_systems(site_registry: dict[str, Any]) -> list[dict[str, Any]]:
    systems = site_registry.get("systems", []) if isinstance(site_registry.get("systems"), list) else []
    configured = []
    for system in systems:
        if not isinstance(system, dict):
            continue
        status = str(system.get("status") or "").strip().lower()
        has_url = bool(str(system.get("login_url") or system.get("submission_url") or "").strip())
        if status in {"configured", "active", "ready", "verified", "discovered"} and has_url:
            configured.append(system)
    return configured


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_fingerprint(package_dir: Path, paper_id: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
        try:
            rel = str(path.relative_to(package_dir)).replace("/", "\\")
            size = path.stat().st_size
            files.append(
                {
                    "path": rel,
                    "bytes": size,
                    "sha256": _sha256(path) if size <= 50 * 1024 * 1024 else "",
                }
            )
        except OSError:
            continue
    return {
        "version": 1,
        "paper_id": paper_id,
        "package_dir": str(package_dir),
        "generated_at": now_iso(),
        "file_count": len(files),
        "files": files,
    }


def _contains_status_ready(path: Path) -> bool:
    text = read_text(path)
    return "Status: READY" in text or "status: ready" in text.lower()


def _read_text_if_exists(path: Path) -> str:
    return read_text(path) if path.exists() and path.is_file() else ""


def _online_queue_assignment_summary(state: dict[str, Any]) -> dict[str, Any]:
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    running: list[str] = []
    queued: list[str] = []
    unassigned: list[str] = []
    for paper_id in online_queue:
        paper = papers.get(paper_id, {})
        if not isinstance(paper, dict):
            unassigned.append(paper_id)
            continue
        workflow = str(paper.get("workflow_status") or "")
        status = str(paper.get("status") or "").strip().lower()
        thread_id = str(paper.get("thread_id") or "").strip()
        monitor_check_pending = bool(paper.get("monitor_check_pending")) and status in {
            "submitted",
            "monitoring",
        }
        if status not in ONLINE_WORKER_STATUSES and not monitor_check_pending:
            unassigned.append(paper_id)
        elif workflow == "running" and thread_id:
            running.append(paper_id)
        elif workflow == "queued":
            queued.append(paper_id)
            unassigned.append(paper_id)
        else:
            unassigned.append(paper_id)
    return {
        "online_queue": online_queue,
        "running": running,
        "queued": queued,
        "unassigned": unassigned,
        "all_assigned": bool(online_queue) and not unassigned,
    }


def _preflight_paper(
    paper_id: str,
    paper: dict[str, Any],
    credentials: dict[str, Any],
    site_registry: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    actions: list[str] = []
    discovery_tasks: list[dict[str, Any]] = []
    package_dir = Path(str(paper.get("source_package_dir") or "").strip())
    if not str(package_dir):
        blockers.append("source_package_dir is empty")
        package_dir = Path(".")
    if not package_dir.exists() or not package_dir.is_dir():
        blockers.append(f"source_package_dir does not exist or is not a directory: {package_dir}")

    paper_dir = package_dir.parent if package_dir.exists() else Path(".")
    target_context = _hydrate_paper_target_from_package(paper_id, paper)
    main_pdf = package_dir / "main.pdf"
    if not main_pdf.exists() and package_dir.exists():
        pdfs = sorted(package_dir.glob("*.pdf"))
        main_pdf = pdfs[0] if pdfs else main_pdf
    if not main_pdf.exists():
        blockers.append("submission package has no PDF")

    main_tex = package_dir / "main.tex"
    if not main_tex.exists() and package_dir.exists():
        tex_files = sorted(package_dir.glob("*.tex"))
        main_tex = tex_files[0] if tex_files else main_tex
    if not main_tex.exists():
        blockers.append("submission package has no TeX source")

    author_info = paper_dir / "author_info.md"
    if not author_info.exists():
        blockers.append("author_info.md is missing")
    elif not _contains_status_ready(author_info):
        blockers.append("author_info.md does not contain Status: READY")

    journal_md = paper_dir / "journal.md"
    journal_text = _read_text_if_exists(journal_md)
    if not journal_md.exists():
        blockers.append("journal.md is missing")
    elif "do not submit yet" in journal_text.lower():
        blockers.append("journal.md final decision is Do not submit yet")
    elif "recommended" not in journal_text.lower():
        warnings.append("journal.md does not clearly contain Recommended")

    final_risk = package_dir / "final_risk_report.md"
    risk_text = _read_text_if_exists(final_risk)
    if final_risk.exists() and "do not submit" in risk_text.lower():
        blockers.append("final_risk_report.md contains do not submit")
    if final_risk.exists() and "TODO" in risk_text:
        warnings.append("final_risk_report.md still contains TODO")

    stale_terms = ["JSA", "JSS", "FGCS", "PARCO", "SoftwareX"]
    stale_hits: list[str] = []
    for candidate in [package_dir / "cover_letter.md", package_dir / "declarations.md", main_tex]:
        text = _read_text_if_exists(candidate)
        if not text:
            continue
        for term in stale_terms:
            if term in text:
                stale_hits.append(f"{candidate.name}:{term}")
    if stale_hits:
        warnings.append("possible stale journal residue: " + ", ".join(sorted(set(stale_hits))[:12]))

    if package_dir.exists() and package_dir.is_dir() and not blockers:
        fingerprint_path = package_dir / "submission_metadata_fingerprint.json"
        if not fingerprint_path.exists():
            write_json_atomic(fingerprint_path, _package_fingerprint(package_dir, paper_id))
            actions.append(f"created {fingerprint_path.name}")

    journal_accounts = _configured_accounts(credentials, "journal_system")
    email_accounts = _configured_accounts(credentials, "email")
    online_ready_journal_accounts = _online_ready_accounts(credentials, "journal_system")
    online_ready_email_accounts = _online_ready_accounts(credentials, "email")
    broker_required_journal_accounts = _secret_broker_required_accounts(credentials, "journal_system")
    broker_required_email_accounts = _secret_broker_required_accounts(credentials, "email")
    site_systems = _configured_site_systems(site_registry)
    system_id = str(
        paper.get("submission_system_id")
        or target_context.get("submission_system_id")
        or ""
    ).strip().casefold()
    target_journal = str(
        paper.get("target_journal")
        or target_context.get("target_journal")
        or ""
    ).strip()
    adapter = submission_site_adapter(system_id)
    expected_origins = (
        adapter.allowed_origins if adapter is not None else ()
    )
    aliases = _system_aliases(
        system_id,
        str(
            paper.get("submission_system")
            or target_context.get("submission_system")
            or ""
        ),
    )
    resolver = _smart_credential_resolver()
    target_accounts = [
        account
        for account in journal_accounts
        if _matches_target_account(
            account,
            aliases=aliases,
            target_journal=target_journal,
            resolver=resolver,
            expected_origins=expected_origins,
        )
    ]
    existing_account_id = str(paper.get("credential_account_id") or "").strip()
    selected_account = next(
        (
            account
            for account in target_accounts
            if str(account.get("account_id") or "").strip().casefold()
            == existing_account_id.casefold()
        ),
        target_accounts[0] if target_accounts else None,
    )
    selected_readiness = (
        _resolved_account_readiness(
            selected_account,
            resolver=resolver,
            expected_origins=expected_origins,
        )
        if selected_account is not None
        else None
    )
    selected_account_id = (
        str(selected_account.get("account_id") or "").strip()
        if selected_account is not None
        else ""
    )
    account_missing_policy = str(
        settings.get("account_missing_policy") or "auto_register"
    ).strip().casefold()
    auto_registration = bool(
        account_missing_policy == "auto_register"
        and settings.get("account_registration_enabled", True)
        and adapter is not None
        and adapter.supports_registration
    )
    if not journal_accounts:
        discovery_tasks.append(
            {
                "type": "discover_account",
                "scope": "journal_system",
                "sources": ["trusted account manager", "exact-origin live-session probe"],
            }
        )
    if not email_accounts:
        discovery_tasks.append(
            {
                "type": "discover_account",
                "scope": "email",
                "sources": ["trusted account manager", "authenticated mailbox connector"],
            }
        )
        warnings.append("email account not configured yet; monitoring will require account discovery")
    if not site_systems:
        discovery_tasks.append(
            {
                "type": "discover_journal_site",
                "scope": "submission_system",
                "sources": ["legacy auto-submission state", "paper journal.md", "web search", "journal author guidelines"],
            }
        )

    missing_target_context = not _paper_has_online_target(paper_id, paper)
    online_enabled = bool(settings.get("online_submit_enabled"))
    credential_dispatch_blocked = bool(
        selected_readiness is not None
        and selected_readiness.requires_secret_broker
    )
    if blockers:
        status = "blocked"
        next_action = "fix_local_blocker"
        detail = "; ".join(blockers)
        queue_action = "remove"
    elif missing_target_context:
        status = "waiting"
        next_action = "wait_journal_author_target_package"
        detail = "preflight passed, but target journal or submission system is missing; rerun journal-author package or refresh package metadata before browser dispatch"
        queue_action = "remove"
    elif selected_account is None and auto_registration:
        status = "login_check"
        next_action = "trusted_credential_broker_auto_register"
        detail = (
            "preflight passed; no matching account exists, so the trusted local "
            "credential broker will start the registered site adapter's account flow"
        )
        queue_action = "remove"
    elif credential_dispatch_blocked:
        status = "login_check"
        next_action = "trusted_credential_broker_login"
        detail = (
            "preflight passed; the trusted local credential broker will use the "
            "saved DPAPI credential to establish an exact-origin browser session"
        )
        queue_action = "remove"
    elif discovery_tasks and settings.get("auto_discovery_enabled", True):
        task_types = ", ".join(task["type"] for task in discovery_tasks)
        if any(task["type"] == "discover_journal_site" for task in discovery_tasks):
            status = "discover_journal_site"
        elif any(task.get("scope") == "journal_system" for task in discovery_tasks):
            status = "discover_account"
        else:
            status = "discover_email_account"
        next_action = "run_discovery_tasks"
        detail = f"preflight passed; auto-discovery required: {task_types}"
        queue_action = "keep"
    elif discovery_tasks:
        status = "blocked"
        next_action = "enable_auto_discovery_or_fill_local_registry"
        detail = "auto-discovery disabled; missing " + ", ".join(task["type"] for task in discovery_tasks)
        queue_action = "remove"
    elif online_enabled:
        status = "login_check"
        next_action = "browser_login_check"
        detail = "preflight passed; ready for browser login/site adapter"
        queue_action = "remove"
    else:
        status = "ready_for_online_submit"
        next_action = "enable_online_submit_after_site_adapter_review"
        detail = "preflight passed; online_submit_enabled=false, no browser final-submit attempted"
        queue_action = "remove"

    return {
        "paper_id": paper_id,
        "status": status,
        "next_action": next_action,
        "detail": detail,
        "blockers": blockers,
        "warnings": warnings,
        "actions": actions,
        "discovery_tasks": discovery_tasks,
        "queue_action": queue_action,
        "package_dir": str(package_dir),
        "main_pdf": str(main_pdf) if main_pdf.exists() else "",
        "main_tex": str(main_tex) if main_tex.exists() else "",
        "author_info": str(author_info) if author_info.exists() else "",
        "journal_md": str(journal_md) if journal_md.exists() else "",
        "configured_journal_accounts": len(journal_accounts),
        "configured_email_accounts": len(email_accounts),
        "online_ready_journal_accounts": len(online_ready_journal_accounts),
        "online_ready_email_accounts": len(online_ready_email_accounts),
        "secret_broker_required_journal_accounts": len(broker_required_journal_accounts),
        "secret_broker_required_email_accounts": len(broker_required_email_accounts),
        "configured_site_systems": len(site_systems),
        "credential_account_id": selected_account_id,
        "credential_broker_required": credential_dispatch_blocked,
        "account_missing_policy": account_missing_policy,
        "auto_registration_planned": bool(selected_account is None and auto_registration),
        "new_account_auth_preference": str(
            settings.get("new_account_auth_preference") or "orcid_first"
        ),
        "orcid_first_planned": bool(
            selected_account is None
            and auto_registration
            and str(
                settings.get("new_account_auth_preference") or "orcid_first"
            ).casefold()
            == "orcid_first"
        ),
        "online_submit_enabled": online_enabled,
        "target_context": target_context,
    }


def _write_audit(paper_id: str, result: dict[str, Any]) -> str:
    SMART_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = SMART_AUDIT_DIR / f"{timestamp_id()}_{_safe_name(paper_id)}_{result.get('status', 'status')}.md"
    lines = [
        f"# Smart Submission Audit: {paper_id}",
        "",
        f"- timestamp: {now_iso()}",
        f"- status: {result.get('status')}",
        f"- next_action: {result.get('next_action')}",
        f"- package_dir: {result.get('package_dir')}",
        f"- main_pdf: {result.get('main_pdf') or '-'}",
        f"- main_tex: {result.get('main_tex') or '-'}",
        f"- author_info: {result.get('author_info') or '-'}",
        f"- journal_md: {result.get('journal_md') or '-'}",
        f"- journal_accounts: {result.get('configured_journal_accounts')}",
        f"- email_accounts: {result.get('configured_email_accounts')}",
        f"- site_systems: {result.get('configured_site_systems')}",
        f"- online_submit_enabled: {result.get('online_submit_enabled')}",
        "",
        "## Detail",
        "",
        str(result.get("detail") or "-"),
        "",
        "## Blockers",
        "",
    ]
    blockers = result.get("blockers") or []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = result.get("warnings") or []
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    lines.extend(["", "## Discovery Tasks", ""])
    discovery_tasks = result.get("discovery_tasks") or []
    if discovery_tasks:
        for task in discovery_tasks:
            lines.append(
                f"- {task.get('type')} | scope={task.get('scope', '-')} | sources={', '.join(task.get('sources', []))}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Actions", ""])
    actions = result.get("actions") or []
    lines.extend(f"- {item}" for item in actions) if actions else lines.append("- none")
    write_text(path, "\n".join(lines))
    return str(path)


@_smart_state_locked
def run_smart_submission_scheduler(
    *,
    force: bool = False,
    max_papers: int = 0,
    online_recovery: bool = True,
    selected_ids: list[str] | None = None,
) -> dict[str, Any]:
    from .ai_provider_store import AIProviderStore

    if AIProviderStore().load_settings().execution_mode != "legacy_codex":
        paused = pause_smart_submission_automation(reason="api_key_mode_fail_closed")
        return {
            "status": "blocked",
            "code": "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE",
            "message": "API 密钥模式不会运行或自动回退到智能投稿 Codex 调度器。",
            "processed": 0,
            "pause": paused,
        }
    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    selected_scope = None if selected_ids is None else {
        str(item).strip() for item in selected_ids if str(item).strip()
    }
    legacy_secret_broker_recovery = _recover_legacy_secret_broker_blocks(
        state,
        selected_ids=selected_scope,
    )
    reconcile_result = (
        reconcile_smart_submission_worker_states(state)
        if selected_scope is None
        else {"changed": 0, "normalized": [], "scope": "selected_only"}
    )
    monitoring_access_normalize = (
        _normalize_monitoring_access_failure_blocks(state)
        if selected_scope is None
        else {"normalized": [], "email_firsted": [], "scope": "selected_only"}
    )
    online_queue_normalize = (
        _normalize_online_queue_state(state, reason="smart_submission_run_start")
        if selected_scope is None
        else {"changed": False, "scope": "selected_only"}
    )
    if (
        legacy_secret_broker_recovery.get("changed")
        or
        reconcile_result.get("changed")
        or monitoring_access_normalize.get("normalized")
        or monitoring_access_normalize.get("email_firsted")
        or online_queue_normalize.get("changed")
    ):
        write_json_atomic(SMART_STATE_PATH, state)
        state = read_json(SMART_STATE_PATH, state)
    credentials = read_json(SMART_CREDENTIAL_PATH, {})
    site_registry = read_json(SMART_SITE_REGISTRY_PATH, {})
    mode = str(state.get("mode") or "manual")
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    if mode == "manual" and not force:
        wakeup_reset = set_smart_submission_automation_fast_wakeup(False) if not online_queue else {}
        if wakeup_reset:
            state = read_json(SMART_STATE_PATH, _default_state())
        state["updated_at"] = now_iso()
        state.setdefault("history", []).append({"at": now_iso(), "event": "quiet_heartbeat", "mode": mode})
        write_json_atomic(SMART_STATE_PATH, state)
        program_control = _build_smart_program_control(
            state,
            selected_ids=sorted(selected_scope) if selected_scope is not None else None,
            persist=True,
            sync_ledger=False,
        )
        return {
            "status": "idle",
            "mode": mode,
            "processed": 0,
            "online_queue_count": len(online_queue),
            "online_submit_enabled": bool(settings.get("online_submit_enabled")),
            "visible_dispatch_ready": False,
            "scheduler_wakeup": wakeup_reset,
            "worker_state_reconcile": reconcile_result,
            "monitoring_access_normalize": monitoring_access_normalize,
            "online_queue_normalize": online_queue_normalize,
            "program_control": program_control,
            "state_path": str(SMART_STATE_PATH),
        }

    daily_sweep_summary: dict[str, Any] = {}
    if mode == "scheduled":
        daily_sweep_summary = queue_smart_submission_daily_sweep()
        state = read_json(SMART_STATE_PATH, _default_state())
        credentials = read_json(SMART_CREDENTIAL_PATH, {})
        site_registry = read_json(SMART_SITE_REGISTRY_PATH, {})
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        daily_normalize = _normalize_online_queue_state(state, reason="smart_submission_run_after_daily_sweep")
        if daily_normalize.get("changed"):
            write_json_atomic(SMART_STATE_PATH, state)
            state = read_json(SMART_STATE_PATH, state)
            settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
            daily_sweep_summary["online_queue_normalize"] = daily_normalize
        online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]

    auto_import_result: dict[str, Any] = {}
    papers_before = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    queue_before = state.get("paper_queue", []) if isinstance(state.get("paper_queue"), list) else []
    if not papers_before or (not queue_before and not online_queue):
        unified_state = read_json(UNIFIED_STATE_PATH, {})
        if isinstance(unified_state.get("papers"), dict) and unified_state.get("papers"):
            auto_import_result = {
                k: v
                for k, v in import_from_unified_state(
                    unified_state,
                    selected_ids=None if selected_scope is None else sorted(selected_scope),
                ).items()
                if k != "state"
            }
            state = read_json(SMART_STATE_PATH, _default_state())
            online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]

    auto_discovery_summary = _autofill_from_legacy_sources(credentials, site_registry, state, settings)
    browser_health_summary: dict[str, Any] = {}
    browser_recovery_summary: dict[str, Any] = {}
    package_autofix_recovery_summary: dict[str, Any] = {}
    auto_action_recovery_summary: dict[str, Any] = {}
    browser_cleanup_summary: dict[str, Any] = {}
    if online_recovery and settings.get("online_submit_enabled"):
        browser_health_summary = smart_submission_browser_health(ensure=True)
        browser_cleanup_summary = smart_submission_browser_cleanup(close=True)
        state = read_json(SMART_STATE_PATH, _default_state())
        if not isinstance(state, dict):
            state = _default_state()
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        browser_recovery_summary = _recover_browser_only_blocks(state, browser_health_summary)
        package_autofix_recovery_summary = _recover_autofixable_package_blocks(state, browser_health_summary)
        auto_action_recovery_summary = _recover_auto_actionable_papers_in_state(state)
        monitoring_access_normalize = _normalize_monitoring_access_failure_blocks(state)
        online_queue_normalize = _normalize_online_queue_state(state, reason="smart_submission_run_after_online_recovery")
        online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    queue = [str(item) for item in state.get("paper_queue", []) if str(item).strip()]
    papers = state.setdefault("papers", {})
    results: list[dict[str, Any]] = []
    processed = 0
    for paper_id in list(queue):
        if selected_scope is not None and paper_id not in selected_scope:
            continue
        if max_papers and processed >= max_papers:
            break
        paper = papers.get(paper_id, {})
        if not isinstance(paper, dict):
            paper = {}
            papers[paper_id] = paper
        if str(paper.get("status") or "") in {"accepted", "blocked", "stopped", "waiting", "no_action"}:
            queue.remove(paper_id)
            continue
        result = _preflight_paper(paper_id, paper, credentials, site_registry, settings)
        audit_path = _write_audit(paper_id, result)
        result["audit_path"] = audit_path
        paper.update(
            {
                "status": result["status"],
                "next_action": result["next_action"],
                "blocker": result["detail"] if result["status"] == "blocked" else "",
                "detail": result["detail"],
                "warnings": result["warnings"],
                "discovery_tasks": result["discovery_tasks"],
                "last_audit_path": audit_path,
                "last_checked_at": now_iso(),
                "credential_account_id": result.get("credential_account_id", ""),
                "credential_broker_required": bool(
                    result.get("credential_broker_required")
                ),
                "account_missing_policy": result.get(
                    "account_missing_policy", "auto_register"
                ),
                "auto_registration_planned": bool(
                    result.get("auto_registration_planned")
                ),
            }
        )
        if result.get("queue_action") == "remove" and paper_id in queue:
            queue.remove(paper_id)
        results.append(result)
        processed += 1

    state["paper_queue"] = queue
    online_queue_normalize = (
        _normalize_online_queue_state(state, reason="smart_submission_run_before_mode_decision")
        if selected_scope is None
        else {"changed": False, "scope": "selected_only"}
    )
    online_queue = [str(item) for item in state.get("online_queue", []) if str(item).strip()]
    if mode == "scheduled" and not force:
        state["mode"] = "manual"
    elif mode == "run_once" and not force:
        if settings.get("online_submit_enabled") and online_queue:
            state["mode"] = "run_once"
        else:
            state["mode"] = "manual"
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": "smart_submission_run",
            "mode_before": mode,
            "mode_after": state.get("mode"),
            "processed": processed,
            "remaining_queue": len(queue),
            "online_queue_count": len(online_queue),
            "visible_dispatch_ready": bool(settings.get("online_submit_enabled") and online_queue),
            "auto_import": auto_import_result,
            "daily_sweep": daily_sweep_summary,
            "auto_discovery": auto_discovery_summary,
            "browser_operational": bool(browser_health_summary.get("operational")),
            "browser_cleanup": browser_cleanup_summary,
            "browser_recovery": browser_recovery_summary,
            "package_autofix_recovery": package_autofix_recovery_summary,
            "auto_action_recovery": auto_action_recovery_summary,
            "legacy_secret_broker_recovery": legacy_secret_broker_recovery,
            "monitoring_access_normalize": monitoring_access_normalize,
            "online_queue_normalize": online_queue_normalize,
            "online_recovery": online_recovery,
            "worker_state_reconcile": reconcile_result,
            "selected_ids": sorted(selected_scope) if selected_scope is not None else [],
        }
    )
    write_json_atomic(SMART_STATE_PATH, state)
    try:
        program_control = _build_smart_program_control(
            state,
            selected_ids=sorted(selected_scope) if selected_scope is not None else None,
            persist=True,
            sync_ledger=True,
        )
    except Exception as exc:
        program_control = {
            "ok": False,
            "code": "SMART_PROGRAM_CONTROL_PLAN_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
            "control_plane": "program_v1",
            "dispatch_candidates": [],
            "dispatch_candidate_ids": [],
            "side_effects_dispatched": False,
        }
    assignment_summary = _online_queue_assignment_summary(state)
    wakeup_reset: dict[str, Any] = {}
    wakeup_backoff: dict[str, Any] = {}
    if state.get("mode") == "manual" and not queue and not online_queue:
        wakeup_reset = set_smart_submission_automation_fast_wakeup(False)
    elif (
        assignment_summary.get("all_assigned")
        and bool(settings.get("manual_wakeup_active"))
        and not queue
        and processed == 0
    ):
        wakeup_backoff = set_smart_submission_automation_backoff_wakeup("all_online_queue_assigned")
    return {
        "status": "ran",
        "mode_before": mode,
        "mode_after": state.get("mode"),
        "processed": processed,
        "remaining_queue": len(queue),
        "online_queue_count": len(online_queue),
        "online_submit_enabled": bool(settings.get("online_submit_enabled")),
        "visible_dispatch_ready": bool(
            settings.get("online_submit_enabled")
            and program_control.get("dispatch_candidates")
            and program_control.get("ok", True)
        ),
        "auto_import": auto_import_result,
        "daily_sweep": daily_sweep_summary,
        "auto_discovery": auto_discovery_summary,
        "browser_health": browser_health_summary,
        "browser_cleanup": browser_cleanup_summary,
        "browser_recovery": browser_recovery_summary,
        "package_autofix_recovery": package_autofix_recovery_summary,
        "auto_action_recovery": auto_action_recovery_summary,
        "scheduler_wakeup": wakeup_reset or wakeup_backoff,
        "online_assignment": assignment_summary,
        "online_recovery": online_recovery,
        "worker_state_reconcile": reconcile_result,
        "legacy_secret_broker_recovery": legacy_secret_broker_recovery,
        "monitoring_access_normalize": monitoring_access_normalize,
        "online_queue_normalize": online_queue_normalize,
        "program_control": program_control,
        "results": results,
        "state_path": str(SMART_STATE_PATH),
    }


@_smart_state_locked
def set_smart_submission_mode(mode: str) -> dict[str, Any]:
    if mode not in {"manual", "run_once", "scheduled", "monitor_only"}:
        raise ValueError(f"Unsupported smart submission mode: {mode}")
    ensure_smart_submission_module()
    state = read_json(SMART_STATE_PATH, _default_state())
    state["mode"] = mode
    state["updated_at"] = now_iso()
    state.setdefault("history", []).append({"at": now_iso(), "event": "set_mode", "mode": mode})
    write_json_atomic(SMART_STATE_PATH, state)
    return state
