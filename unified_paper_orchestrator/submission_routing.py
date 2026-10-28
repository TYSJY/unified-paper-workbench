from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse


VALID_EXECUTION_OWNERS = frozenset({"legacy_auto", "smart", "manual"})
VALID_EXECUTION_MODES = frozenset({"production", "shadow", "canary"})
DEFAULT_EXECUTION_OWNER = "legacy_auto"
DEFAULT_EXECUTION_MODE = "production"

_INVALID_EVIDENCE_VALUES = frozenset({"", "-", "n/a", "none", "null", "pending", "unknown"})
_SMART_ID_KEYS = ("submission_id", "manuscript_id", "submission_number", "tracking_id")
_SMART_URL_KEYS = ("confirmation_url", "submission_confirmation_url")
_SMART_EVIDENCE_PATH_KEYS = (
    "submission_evidence_path",
    "confirmation_evidence_path",
    "confirmation_page_path",
    "evidence_path",
)
_RUNNING_STATUSES = frozenset(
    {
        "browser_active",
        "dispatching",
        "final_submit",
        "final_submitting",
        "running",
        "submitting",
        "worker_running",
    }
)
_FINAL_UNKNOWN_STATUSES = frozenset(
    {
        "final_submit_unknown",
        "submission_result_unknown",
        "submission_unknown",
        "submitted_unconfirmed",
    }
)
_LEGACY_CLOSED_STATUSES = frozenset(
    {
        "cancelled",
        "closed",
        "declined",
        "decisioned_rejected",
        "expired_transfer",
        "rejected",
        "removed",
        "retargeted",
        "submission_closed",
        "transfer_expired",
        "transfer_offer_expired",
        "withdrawn",
    }
)


class SubmissionRouteError(RuntimeError):
    """Raised when a submission route change or dispatch would be unsafe."""


def _status_key(value: Any) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", "_").split())


def _meaningful(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.lower() not in _INVALID_EVIDENCE_VALUES else ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, dict):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "active", "true", "yes"}


def normalize_submission_route(value: Any = None) -> dict[str, Any]:
    """Return a validated, compact per-paper route.

    Missing/legacy unified state deliberately defaults to the proven legacy
    automatic-submission path.  Invalid persisted values fail closed instead
    of silently switching an execution engine.
    """

    route = value if isinstance(value, dict) else {}
    owner = str(route.get("execution_owner") or DEFAULT_EXECUTION_OWNER).strip()
    mode = str(route.get("mode") or DEFAULT_EXECUTION_MODE).strip()
    try:
        revision = int(route.get("revision", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("submission route revision must be a positive integer") from exc
    if owner not in VALID_EXECUTION_OWNERS:
        raise ValueError(f"invalid submission execution owner: {owner}")
    if mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"invalid submission execution mode: {mode}")
    if revision < 1:
        raise ValueError("submission route revision must be a positive integer")
    return {"execution_owner": owner, "mode": mode, "revision": revision}


def submission_route_for(routes: Any, paper_id: str) -> dict[str, Any]:
    values = routes if isinstance(routes, dict) else {}
    return normalize_submission_route(values.get(paper_id))


def _paper_record(state: dict[str, Any], paper_id: str) -> dict[str, Any]:
    papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
    record = papers.get(paper_id, {})
    return record if isinstance(record, dict) else {}


def _queue_contains(state: dict[str, Any], paper_id: str, *keys: str) -> bool:
    for key in keys:
        values = state.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                candidate = str(value.get("paper_id") or value.get("id") or "").strip()
            else:
                candidate = str(value or "").strip()
            if candidate == paper_id:
                return True
    return False


def legacy_terminal_evidence(state: dict[str, Any], paper_id: str) -> list[dict[str, str]]:
    """Return only explicit, independently useful legacy terminal evidence."""

    record = _paper_record(state, paper_id)
    evidence: list[dict[str, str]] = []
    active_submission = record.get("active_submission")
    active_is_explicit = "active_submission" in record
    active = _truthy(active_submission)
    statuses = {
        _status_key(record.get(key))
        for key in (
            "status",
            "submission_status",
            "workflow_stage",
            "workflow_status",
            "decision",
        )
        if _status_key(record.get(key))
    }
    explicitly_inactive = active_is_explicit and (
        active_submission is False
        or str(active_submission or "").strip().casefold()
        in {"0", "closed", "false", "inactive", "no"}
    )
    explicitly_closed = explicitly_inactive or bool(statuses & _LEGACY_CLOSED_STATUSES)
    if active:
        evidence.append({"owner": "legacy_auto", "kind": "active_submission", "value": "true"})
    manuscript_id = _meaningful(record.get("manuscript_id"))
    # A manuscript ID remains valuable history after rejection/withdrawal, but
    # it must not block a fresh retarget once the same legacy record explicitly
    # says the attempt is closed.
    if manuscript_id and not explicitly_closed:
        evidence.append({"owner": "legacy_auto", "kind": "manuscript_id", "value": manuscript_id})
    return evidence


def _smart_evidence_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    records = [record]
    for key in ("submission_evidence", "terminal_evidence"):
        nested = record.get(key)
        if isinstance(nested, dict):
            records.append(nested)
    return records


def smart_terminal_evidence(
    state: dict[str, Any],
    paper_id: str,
    *,
    evidence_base: Path | None = None,
) -> list[dict[str, str]]:
    """Return Smart terminal evidence, excluding unverified status strings."""

    record = _paper_record(state, paper_id)
    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in _smart_evidence_records(record):
        for key in _SMART_ID_KEYS:
            value = _meaningful(source.get(key))
            marker = (key, value)
            if value and marker not in seen:
                evidence.append({"owner": "smart", "kind": key, "value": value})
                seen.add(marker)
        for key in _SMART_URL_KEYS:
            value = _meaningful(source.get(key))
            parsed = urlparse(value)
            marker = (key, value)
            if value and parsed.scheme.lower() in {"http", "https"} and parsed.netloc and marker not in seen:
                evidence.append({"owner": "smart", "kind": key, "value": value})
                seen.add(marker)
        for key in _SMART_EVIDENCE_PATH_KEYS:
            value = _meaningful(source.get(key))
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute() and evidence_base is not None:
                path = evidence_base / path
            marker = (key, str(path))
            if path.is_file() and marker not in seen:
                evidence.append({"owner": "smart", "kind": key, "value": str(path)})
                seen.add(marker)
    return evidence


def terminal_submission_evidence(
    legacy_state: dict[str, Any],
    smart_state: dict[str, Any],
    paper_id: str,
    *,
    smart_evidence_base: Path | None = None,
) -> list[dict[str, str]]:
    return legacy_terminal_evidence(legacy_state, paper_id) + smart_terminal_evidence(
        smart_state,
        paper_id,
        evidence_base=smart_evidence_base,
    )


def _record_statuses(record: dict[str, Any]) -> set[str]:
    return {
        token
        for key in ("status", "workflow_stage", "workflow_status", "worker_status", "next_action")
        if (token := _status_key(record.get(key)))
    }


def _active_worker_for(state: dict[str, Any], paper_id: str) -> bool:
    workers = state.get("active_workers")
    if isinstance(workers, dict):
        if paper_id in workers:
            return True
        return any(
            isinstance(value, dict) and str(value.get("paper_id") or value.get("id") or "").strip() == paper_id
            for value in workers.values()
        )
    if isinstance(workers, list):
        return any(
            (str(value.get("paper_id") or value.get("id") or "").strip() if isinstance(value, dict) else str(value).strip())
            == paper_id
            for value in workers
        )
    return False


def route_switch_blockers(
    legacy_state: dict[str, Any],
    smart_state: dict[str, Any],
    paper_id: str,
    *,
    smart_evidence_base: Path | None = None,
) -> list[dict[str, Any]]:
    """Find conditions that make an execution-owner/mode switch unsafe."""

    blockers: list[dict[str, Any]] = []
    evidence = terminal_submission_evidence(
        legacy_state,
        smart_state,
        paper_id,
        smart_evidence_base=smart_evidence_base,
    )
    if evidence:
        blockers.append({"code": "terminal_evidence", "evidence": evidence})

    if _queue_contains(legacy_state, paper_id, "paper_queue", "queue"):
        blockers.append({"code": "legacy_online_queue_active"})
    if _queue_contains(smart_state, paper_id, "online_queue"):
        blockers.append({"code": "smart_online_queue_active"})

    legacy_record = _paper_record(legacy_state, paper_id)
    smart_record = _paper_record(smart_state, paper_id)
    if _record_statuses(legacy_record) & _RUNNING_STATUSES:
        blockers.append({"code": "legacy_running"})
    if _record_statuses(smart_record) & _RUNNING_STATUSES or _active_worker_for(smart_state, paper_id):
        blockers.append({"code": "smart_running"})

    final_unknown = any(
        _truthy(smart_record.get(key))
        for key in ("final_submit_attempted", "final_submit_result_unknown", "final_submit_unknown")
    )
    final_unknown = final_unknown or bool(_record_statuses(smart_record) & _FINAL_UNKNOWN_STATUSES)
    if final_unknown and not smart_terminal_evidence(smart_state, paper_id, evidence_base=smart_evidence_base):
        blockers.append({"code": "final_submit_unknown"})
    return blockers
