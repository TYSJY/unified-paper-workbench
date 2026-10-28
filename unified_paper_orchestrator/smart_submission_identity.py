"""Deterministic submission-identity ledger for Smart Submission.

One paper may accumulate historical manuscript IDs while it moves between
journals.  Online mutation is safe only when the program can name one exact
active submission, or when every prior attempt is closed before a new
submission is created.  This module keeps that decision out of AI prompts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


SUBMISSION_IDENTITY_SCHEMA = "smart-submission-identity/v1"

IDENTITY_VERIFIED = "verified"
IDENTITY_MISSING = "missing"
IDENTITY_CLOSED = "closed"
IDENTITY_CONFLICT = "multiple_active_submissions"
IDENTITY_CANONICAL_MISMATCH = "canonical_submission_mismatch"

_CLOSED_STATUSES = {
    "cancelled",
    "closed",
    "declined",
    "duplicate_withdrawn",
    "expired_transfer",
    "rejected",
    "removed",
    "retargeted",
    "submission_closed",
    "superseded",
    "transfer_expired",
    "transfer_offer_expired",
    "user_confirmed_closed",
    "withdrawn",
}


def normalize_submission_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in {"-", "n/a", "none", "null", "pending", "unknown"}:
        return ""
    if len(text) > 160 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", text):
        return ""
    return text


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _record_is_active(record: Mapping[str, Any]) -> bool:
    if isinstance(record.get("active"), bool):
        return bool(record["active"])
    return _token(record.get("status")) not in _CLOSED_STATUSES


def submission_records(paper: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return normalized records, including the current legacy ID projection."""

    normalized: dict[str, dict[str, Any]] = {}
    raw = paper.get("submission_records")
    if isinstance(raw, Mapping):
        values = raw.values()
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        submission_id = normalize_submission_id(item.get("submission_id"))
        if not submission_id:
            continue
        record = dict(item)
        record["submission_id"] = submission_id
        record["active"] = _record_is_active(record)
        sources = record.get("sources")
        record["sources"] = list(
            dict.fromkeys(str(value).strip() for value in (sources or []) if str(value).strip())
        ) if isinstance(sources, list) else []
        normalized[submission_id] = record

    current_id = normalize_submission_id(
        paper.get("submission_id")
        or paper.get("manuscript_id")
        or paper.get("submission_number")
        or paper.get("tracking_id")
    )
    if current_id and current_id not in normalized:
        normalized[current_id] = {
            "submission_id": current_id,
            "submission_system_id": str(paper.get("submission_system_id") or "").strip().casefold(),
            "target_journal": str(paper.get("target_journal") or "").strip(),
            "status": str(paper.get("status") or "unknown").strip().casefold(),
            "active": _token(paper.get("status")) not in _CLOSED_STATUSES,
            "canonical": True,
            "sources": ["legacy_current_projection"],
        }
    return normalized


def upsert_submission_record(
    paper: dict[str, Any],
    submission_id: Any,
    *,
    submission_system_id: str = "",
    target_journal: str = "",
    status: str = "",
    active: bool | None = None,
    canonical: bool | None = None,
    version: str = "",
    details_url: str = "",
    source: str = "",
    observed_at: str = "",
) -> dict[str, Any] | None:
    clean_id = normalize_submission_id(submission_id)
    if not clean_id:
        return None
    records = submission_records(paper)
    record = dict(records.get(clean_id) or {"submission_id": clean_id})
    if submission_system_id:
        record["submission_system_id"] = str(submission_system_id).strip().casefold()
    elif not record.get("submission_system_id"):
        record["submission_system_id"] = str(paper.get("submission_system_id") or "").strip().casefold()
    if target_journal:
        record["target_journal"] = str(target_journal).strip()
    elif not record.get("target_journal"):
        record["target_journal"] = str(paper.get("target_journal") or "").strip()
    if status:
        record["status"] = str(status).strip().casefold()
    elif not record.get("status"):
        record["status"] = "unknown"
    record["active"] = (
        bool(active)
        if active is not None
        else _token(record.get("status")) not in _CLOSED_STATUSES
    )
    if version:
        record["version"] = str(version).strip()[:80]
    if details_url:
        record["details_url"] = str(details_url).strip()[:1000]
    if observed_at:
        record["last_observed_at"] = str(observed_at).strip()[:80]
        record.setdefault("first_observed_at", record["last_observed_at"])
    sources = [str(item).strip() for item in record.get("sources", []) if str(item).strip()]
    if source and source not in sources:
        sources.append(str(source).strip())
    record["sources"] = sources[-20:]
    if canonical is not None:
        record["canonical"] = bool(canonical)
        if canonical:
            for other in records.values():
                other["canonical"] = False
            paper["canonical_submission_id"] = clean_id
    records[clean_id] = record
    paper["submission_records"] = records
    return record


def close_submission_record(
    paper: dict[str, Any],
    submission_id: Any,
    *,
    status: str,
    source: str = "",
    observed_at: str = "",
) -> dict[str, Any] | None:
    record = upsert_submission_record(
        paper,
        submission_id,
        status=status,
        active=False,
        canonical=False,
        source=source,
        observed_at=observed_at,
    )
    if record is not None and normalize_submission_id(paper.get("canonical_submission_id")) == record["submission_id"]:
        paper["canonical_submission_id"] = ""
    return record


def _same_route(record: Mapping[str, Any], paper: Mapping[str, Any]) -> bool:
    record_system = _token(record.get("submission_system_id"))
    paper_system = _token(paper.get("submission_system_id"))
    if record_system and paper_system and record_system != paper_system:
        return False
    record_journal = _name(record.get("target_journal"))
    paper_journal = _name(paper.get("target_journal"))
    if record_journal and paper_journal and record_journal != paper_journal:
        return False
    return True


def submission_identity_snapshot(paper: Mapping[str, Any]) -> dict[str, Any]:
    records = submission_records(paper)
    active_all = [record for record in records.values() if _record_is_active(record)]
    active_route = [record for record in active_all if _same_route(record, paper)]
    active_route_ids = sorted(record["submission_id"] for record in active_route)
    active_all_ids = sorted(record["submission_id"] for record in active_all)
    requested_canonical = normalize_submission_id(paper.get("canonical_submission_id"))
    marked_canonical = [
        record["submission_id"] for record in records.values() if record.get("canonical") is True
    ]
    canonical_id = requested_canonical or (marked_canonical[0] if len(marked_canonical) == 1 else "")
    current_id = normalize_submission_id(
        paper.get("submission_id")
        or paper.get("manuscript_id")
        or paper.get("submission_number")
        or paper.get("tracking_id")
    )

    if len(active_route_ids) > 1:
        status = IDENTITY_CONFLICT
    elif len(active_route_ids) == 1:
        only_id = active_route_ids[0]
        if canonical_id and canonical_id != only_id:
            status = IDENTITY_CANONICAL_MISMATCH
        else:
            status = IDENTITY_VERIFIED
            canonical_id = only_id
    elif records:
        status = IDENTITY_CLOSED
    else:
        status = IDENTITY_MISSING

    mutation_allowed = bool(
        status == IDENTITY_VERIFIED
        and canonical_id
        and (not current_id or current_id == canonical_id)
    )
    return {
        "schema": SUBMISSION_IDENTITY_SCHEMA,
        "status": status,
        "record_count": len(records),
        "records": records,
        "canonical_submission_id": canonical_id,
        "current_submission_id": current_id,
        "active_submission_ids": active_all_ids,
        "active_route_submission_ids": active_route_ids,
        "duplicate_submission_ids": active_route_ids if len(active_route_ids) > 1 else [],
        "mutation_allowed": mutation_allowed,
        "new_submission_allowed": not active_all_ids,
    }


def project_submission_identity(paper: dict[str, Any], *, observed_at: str = "") -> dict[str, Any]:
    snapshot = submission_identity_snapshot(paper)
    paper["submission_records"] = snapshot["records"]
    paper["submission_identity_schema"] = SUBMISSION_IDENTITY_SCHEMA
    paper["submission_identity_status"] = snapshot["status"]
    paper["submission_identity_record_count"] = snapshot["record_count"]
    paper["active_submission_ids"] = snapshot["active_submission_ids"]
    paper["duplicate_submission_ids"] = snapshot["duplicate_submission_ids"]
    if snapshot["canonical_submission_id"]:
        paper["canonical_submission_id"] = snapshot["canonical_submission_id"]
    if observed_at:
        paper["submission_identity_updated_at"] = str(observed_at).strip()[:80]
    return snapshot


def classify_monitor_action(
    *,
    status: Any,
    action_kind: Any,
    summary: Any,
) -> str:
    """Conservatively classify explicit monitor evidence with program rules."""

    explicit = _token(action_kind)
    if explicit not in {"", "none", "unknown"}:
        return explicit
    status_token = _token(status)
    text = _name(summary)
    if status_token == "accepted" or re.search(r"\b(accepted|acceptance)\b", text):
        return "accepted"
    if any(
        marker in text
        for marker in (
            "title must match",
            "title does not match",
            "technical check",
            "technical correction",
            "select a special issue",
            "choose a special issue",
            "select a collection",
            "choose a collection",
            "metadata correction",
        )
    ):
        return "technical_correction"
    if status_token in {"rejected", "retarget_needed"} or any(
        marker in text
        for marker in (
            "out of scope",
            "outside the scope",
            "choose another journal",
            "select another journal",
            "transfer to another journal",
            "submission has been rejected",
            "withdraw and submit elsewhere",
            "withdrawal and submit elsewhere",
            "submit the manuscript elsewhere",
        )
    ):
        return "retarget"
    if any(
        marker in text
        for marker in (
            "reviewer comments",
            "response to reviewers",
            "scientific revision",
            "major revision",
            "minor revision",
            "revised manuscript after peer review",
        )
    ):
        return "scientific_revision"
    if any(
        marker in text
        for marker in (
            "special issue",
            "select a collection",
            "choose a collection",
            "metadata correction",
        )
    ):
        return "technical_correction"
    if any(
        marker in text
        for marker in (
            "missing file",
            "replace file",
            "upload file",
            "author metadata",
            "affiliation metadata",
            "files or metadata",
        )
    ):
        return "files_or_metadata"
    return "unknown" if status_token in {"revision_needed", "revision_requested"} else (explicit or "none")
