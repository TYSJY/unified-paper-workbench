from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .io_utils import now_iso, write_json_atomic
from .writing_evidence_gate import (
    AUTO_TRUST_COMPLETION_SCHEMA,
    FORBIDDEN_DRAFT_MARKERS,
    MANUAL_COMPLETION_RELATIVE_PATH,
    MANUAL_COMPLETION_SCHEMA,
    _absolute_lexical,
    _inspect_safe_path,
    _is_contained,
    _is_single_safe_name,
    _resolve_fixed_paper_path,
    _snapshot_text,
    _verify_stable_snapshots,
    validate_writing_evidence,
)


def _add_issue(issues: list[dict[str, str]], code: str, path: str, message: str) -> None:
    issues.append({"code": code, "path": path, "message": message})


def prepare_manual_writing_evidence(
    papers_dir: Path | str,
    paper_id: str,
    *,
    check_only: bool = True,
    expected_draft_sha256: str = "",
) -> dict[str, Any]:
    """Check or record that the user manually applied the real data.

    This route never edits the paper.  After explicit UI confirmation it
    atomically records a hash-bound manual completion attestation.  Existing
    verification markers are intentionally delegated to the tuned Step 6.
    """

    paper_id = str(paper_id or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "status": "BLOCKED",
        "ready": False,
        "transition_ready": False,
        "candidate_ready": False,
        "paper_id": paper_id,
        "mode": "manual_edit",
        "draft_v2_sha256": "",
        "remaining_marker_count": 0,
        "remaining_markers": [],
        "issues": [],
    }
    issues: list[dict[str, str]] = result["issues"]
    if not _is_single_safe_name(paper_id):
        _add_issue(issues, "invalid_paper_id", paper_id, "paper_id must be one safe directory name")
        return result

    root = _absolute_lexical(papers_dir)
    paper_dir = _absolute_lexical(root / paper_id)
    if not _is_contained(root, paper_dir):
        _add_issue(issues, "paper_path_escape", paper_id, "paper directory escapes the configured papers root")
        return result

    draft_path = _resolve_fixed_paper_path(paper_dir, "draft_v2.tex")
    completion_path = _resolve_fixed_paper_path(paper_dir, MANUAL_COMPLETION_RELATIVE_PATH)
    if draft_path is None or completion_path is None:
        _add_issue(issues, "manual_completion_path_unsafe", paper_id, "manual completion path is unsafe")
        return result

    snapshots: dict[Path, Any] = {}
    draft_text, draft_snapshot, draft_error_kind, draft_error = _snapshot_text(root, draft_path, snapshots)
    if draft_snapshot is None:
        code = (
            "draft_v2_missing"
            if draft_error_kind == "missing"
            else "draft_v2_snapshot_unstable"
            if draft_error_kind == "unstable"
            else "draft_v2_unsafe_path"
            if draft_error_kind == "unsafe"
            else "draft_v2_unreadable"
        )
        _add_issue(issues, code, "draft_v2.tex", draft_error)
        return result

    result["draft_v2_sha256"] = draft_snapshot.sha256
    remaining_markers = [label for label, marker in FORBIDDEN_DRAFT_MARKERS if marker.search(draft_text)]
    result["remaining_marker_count"] = len(remaining_markers)
    result["remaining_markers"] = remaining_markers

    expected_hash = str(expected_draft_sha256 or "").strip().casefold()
    if expected_hash and expected_hash != draft_snapshot.sha256:
        _add_issue(
            issues,
            "manual_completion_preview_changed",
            "draft_v2.tex",
            "draft_v2.tex changed after the user-confirmed preview; check it again",
        )

    for changed_path, reason in _verify_stable_snapshots(root, snapshots):
        _add_issue(
            issues,
            "manual_completion_snapshot_changed",
            changed_path.relative_to(paper_dir).as_posix(),
            reason,
        )
    if issues:
        return result

    candidate = {
        "schema": MANUAL_COMPLETION_SCHEMA,
        "paper_id": paper_id,
        "status": "READY",
        "mode": "manual_edit",
        "confirmation": "DATA_MANUALLY_APPLIED",
        "marker_cleanup": "step_6",
        "draft_v2_sha256": draft_snapshot.sha256,
        "confirmed_at": now_iso(),
    }
    canonical = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result.update(
        {
            "ok": True,
            "status": "CHECKED" if check_only else "PREPARED",
            "ready": True,
            "transition_ready": True,
            "candidate_ready": True,
            "manual_completion_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "manual_completion_path": MANUAL_COMPLETION_RELATIVE_PATH,
        }
    )
    if check_only:
        return result

    completion_components, completion_error = _inspect_safe_path(root, completion_path)
    if completion_components is None:
        result.update({"ok": False, "status": "BLOCKED", "ready": False, "transition_ready": False})
        _add_issue(
            issues,
            "manual_completion_path_unsafe",
            MANUAL_COMPLETION_RELATIVE_PATH,
            completion_error,
        )
        return result
    try:
        write_json_atomic(completion_path, candidate)
    except OSError as exc:
        result.update({"ok": False, "status": "BLOCKED", "ready": False, "transition_ready": False})
        _add_issue(
            issues,
            "manual_completion_write_failed",
            MANUAL_COMPLETION_RELATIVE_PATH,
            f"cannot write manual completion record: {type(exc).__name__}",
        )
        return result

    gate = validate_writing_evidence(root, paper_id)
    result["gate"] = gate
    result["ready"] = bool(gate.get("ready"))
    result["transition_ready"] = bool(gate.get("transition_ready"))
    if result["ready"] and result["transition_ready"]:
        result.update({"ok": True, "status": "RECORDED", "candidate_ready": True})
    else:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        issues.extend(gate.get("issues", []))
    return result


def prepare_policy_trusted_writing_evidence(
    papers_dir: Path | str,
    paper_id: str,
    *,
    policy_snapshot: dict[str, Any],
    check_only: bool = True,
    expected_draft_sha256: str = "",
) -> dict[str, Any]:
    """Record a workspace-policy acceptance bound to the current draft.

    This is intentionally separate from the explicit manual-click record.  It
    never edits ``draft_v2.tex`` and never removes markers.  Its only mutation
    is an atomic controller attestation that Step 6 can verify before running.
    """

    paper_id = str(paper_id or "").strip()
    policy = dict(policy_snapshot) if isinstance(policy_snapshot, dict) else {}
    result: dict[str, Any] = {
        "ok": False,
        "status": "BLOCKED",
        "ready": False,
        "transition_ready": False,
        "candidate_ready": False,
        "paper_id": paper_id,
        "mode": "workspace_auto_trust",
        "draft_v2_sha256": "",
        "remaining_marker_count": 0,
        "remaining_markers": [],
        "issues": [],
    }
    issues: list[dict[str, str]] = result["issues"]
    if not _is_single_safe_name(paper_id):
        _add_issue(issues, "invalid_paper_id", paper_id, "paper_id must be one safe directory name")
        return result

    revision = policy.get("revision")
    if (
        policy.get("schema") != "unified-paper-orchestrator/writing-gate-policy/v1"
        or policy.get("gate_id") != "step5_data_confirmation"
        or policy.get("mode") != "auto_trust"
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not str(policy.get("updated_at") or "").strip()
    ):
        _add_issue(
            issues,
            "policy_completion_snapshot_invalid",
            "writing_gate_policy.json",
            "workspace auto-trust policy snapshot is invalid",
        )
        return result

    root = _absolute_lexical(papers_dir)
    paper_dir = _absolute_lexical(root / paper_id)
    if not _is_contained(root, paper_dir):
        _add_issue(issues, "paper_path_escape", paper_id, "paper directory escapes the configured papers root")
        return result

    draft_path = _resolve_fixed_paper_path(paper_dir, "draft_v2.tex")
    completion_path = _resolve_fixed_paper_path(paper_dir, MANUAL_COMPLETION_RELATIVE_PATH)
    if draft_path is None or completion_path is None:
        _add_issue(issues, "policy_completion_path_unsafe", paper_id, "policy completion path is unsafe")
        return result

    snapshots: dict[Path, Any] = {}
    draft_text, draft_snapshot, draft_error_kind, draft_error = _snapshot_text(root, draft_path, snapshots)
    if draft_snapshot is None:
        code = (
            "draft_v2_missing"
            if draft_error_kind == "missing"
            else "draft_v2_snapshot_unstable"
            if draft_error_kind == "unstable"
            else "draft_v2_unsafe_path"
            if draft_error_kind == "unsafe"
            else "draft_v2_unreadable"
        )
        _add_issue(issues, code, "draft_v2.tex", draft_error)
        return result

    result["draft_v2_sha256"] = draft_snapshot.sha256
    remaining_markers = [label for label, marker in FORBIDDEN_DRAFT_MARKERS if marker.search(draft_text)]
    result["remaining_marker_count"] = len(remaining_markers)
    result["remaining_markers"] = remaining_markers

    expected_hash = str(expected_draft_sha256 or "").strip().casefold()
    if expected_hash and expected_hash != draft_snapshot.sha256:
        _add_issue(
            issues,
            "policy_completion_preview_changed",
            "draft_v2.tex",
            "draft_v2.tex changed before the workspace-policy attestation was recorded",
        )
    for changed_path, reason in _verify_stable_snapshots(root, snapshots):
        _add_issue(
            issues,
            "policy_completion_snapshot_changed",
            changed_path.relative_to(paper_dir).as_posix(),
            reason,
        )
    if issues:
        return result

    candidate = {
        "schema": AUTO_TRUST_COMPLETION_SCHEMA,
        "paper_id": paper_id,
        "status": "READY",
        "mode": "workspace_auto_trust",
        "confirmation": "DATA_ACCEPTED_BY_WORKSPACE_POLICY",
        "marker_cleanup": "step_6",
        "draft_v2_sha256": draft_snapshot.sha256,
        "policy": policy,
        "confirmed_at": now_iso(),
    }
    canonical = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    completion_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    result.update(
        {
            "ok": True,
            "status": "CHECKED" if check_only else "PREPARED",
            "ready": True,
            "transition_ready": True,
            "candidate_ready": True,
            "policy_completion_sha256": completion_sha256,
            "manual_completion_sha256": completion_sha256,
            "manual_completion_path": MANUAL_COMPLETION_RELATIVE_PATH,
            "policy_snapshot": policy,
        }
    )
    if check_only:
        return result

    completion_components, completion_error = _inspect_safe_path(root, completion_path)
    if completion_components is None:
        result.update({"ok": False, "status": "BLOCKED", "ready": False, "transition_ready": False})
        _add_issue(
            issues,
            "policy_completion_path_unsafe",
            MANUAL_COMPLETION_RELATIVE_PATH,
            completion_error,
        )
        return result
    try:
        write_json_atomic(completion_path, candidate)
    except OSError as exc:
        result.update({"ok": False, "status": "BLOCKED", "ready": False, "transition_ready": False})
        _add_issue(
            issues,
            "policy_completion_write_failed",
            MANUAL_COMPLETION_RELATIVE_PATH,
            f"cannot write policy completion record: {type(exc).__name__}",
        )
        return result

    gate = validate_writing_evidence(root, paper_id)
    result["gate"] = gate
    result["ready"] = bool(gate.get("ready"))
    result["transition_ready"] = bool(gate.get("transition_ready"))
    if (
        result["ready"]
        and result["transition_ready"]
        and gate.get("gate_profile") == "evidence_auto_trust_transition"
    ):
        result.update({"ok": True, "status": "RECORDED", "candidate_ready": True})
    else:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        issues.extend(gate.get("issues", []))
    return result
