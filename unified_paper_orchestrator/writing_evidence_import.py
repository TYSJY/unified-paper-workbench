from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from .io_utils import write_json_atomic, write_json_atomic_if_unchanged
from .writing_evidence_gate import (
    CLAIM_COVERAGE_POLICY,
    EVIDENCE_CONTRACT_SCHEMA_VERSION,
    FORBIDDEN_DRAFT_MARKERS,
    MANIFEST_SCHEMA_VERSION,
    _STATUS_LINE_RE,
    _absolute_lexical,
    _inspect_safe_path,
    _is_contained,
    _is_reparse_component,
    _is_single_safe_name,
    _read_stable_snapshot,
    _resolve_paper_relative,
    _snapshot_text,
    _validate_artifacts,
    _validate_claim_evidence,
    _validate_evidence_contract,
    _verify_stable_snapshots,
    validate_writing_evidence,
)


IMPORT_SPEC_VERSION = 1
IMPORT_RECEIPT_SCHEMA = "unified-paper-orchestrator/writing-evidence-import-receipt/v1"
MANIFEST_RELATIVE_PATH = "results/verification_manifest.json"
RECEIPT_DIRECTORY = "results/evidence_import_receipts"

_TOP_LEVEL_KEYS = {"import_spec_version", "artifacts", "evidence_contract", "claims"}
_CONTRACT_KEYS = {
    "environment",
    "workflows",
    "faults",
    "seeds",
    "metrics",
    "limitations",
    "raw_artifact_ids",
    "aggregate_artifact_ids",
}
_CONTRACT_SECTION_KEYS = {
    "environment": {"description", "artifact_ids"},
    "workflows": {"ids", "artifact_ids"},
    "faults": {"ids", "artifact_ids"},
    "seeds": {"values", "artifact_ids"},
    "metrics": {"ids", "artifact_ids"},
    "limitations": {"items", "artifact_ids"},
}


def _issue(issues: list[dict[str, str]], code: str, path: str, message: str) -> None:
    issues.append({"code": code, "path": path, "message": message})


def _result(paper_id: str, *, check_only: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCKED",
        "paper_id": paper_id,
        "check_only": check_only,
        "candidate_ready": False,
        "transition_ready": False,
        "manifest_written": False,
        "receipt_written": False,
        "model_work_resumed": False,
        "scheduler_state_changed": False,
        "manifest_path": MANIFEST_RELATIVE_PATH,
        "receipt_path": "",
        "manifest_sha256": "",
        "artifact_count": 0,
        "claim_count": 0,
        "issues": [],
    }


def _exact_keys(value: Any, expected: set[str], path: str, issues: list[dict[str, str]]) -> bool:
    if not isinstance(value, dict):
        _issue(issues, "evidence_import_spec_object", path, "value must be an object")
        return False
    actual = {str(key) for key in value}
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        _issue(
            issues,
            "evidence_import_spec_field_missing",
            path,
            "missing required fields: " + ", ".join(missing),
        )
    if unknown:
        _issue(
            issues,
            "evidence_import_spec_field_unknown",
            path,
            "unknown fields are rejected to catch misspellings: " + ", ".join(unknown),
        )
    return not missing and not unknown


def _resolve_spec_path(paper_dir: Path, spec_path: Path | str) -> tuple[Path | None, str]:
    try:
        raw_value = os.fspath(spec_path)
    except TypeError:
        return None, "spec path is empty or invalid"
    if not isinstance(raw_value, str):
        return None, "spec path must be a text path"
    raw = raw_value.strip()
    if not raw or "\x00" in raw:
        return None, "spec path is empty or invalid"
    native = Path(raw)
    windows = PureWindowsPath(raw)
    if native.is_absolute() or windows.is_absolute() or windows.drive:
        resolved = _absolute_lexical(native)
        if not _is_contained(paper_dir, resolved):
            return None, "absolute spec path must remain inside the selected paper directory"
        return resolved, ""
    return _resolve_paper_relative(paper_dir, raw)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _snapshot_record(kind: str, paper_dir: Path, snapshot: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": snapshot.path.relative_to(paper_dir).as_posix(),
        "sha256": snapshot.sha256,
        "size": len(snapshot.data),
    }


def _add_snapshot_changes(
    root: Path,
    paper_dir: Path,
    snapshots: dict[Path, Any],
    issues: list[dict[str, str]],
) -> None:
    for changed_path, reason in _verify_stable_snapshots(root, snapshots):
        try:
            relative = changed_path.relative_to(paper_dir).as_posix()
        except ValueError:
            relative = changed_path.name
        _issue(
            issues,
            "evidence_import_snapshot_changed",
            relative,
            reason,
        )


def _validate_paper_directory(root: Path, paper_dir: Path, paper_id: str, issues: list[dict[str, str]]) -> bool:
    root_components, root_error = _inspect_safe_path(root, root)
    paper_components, paper_error = _inspect_safe_path(root, paper_dir)
    if root_components is None or paper_components is None:
        _issue(issues, "evidence_import_paper_path_unsafe", paper_id, root_error or paper_error)
        return False
    try:
        root_stat = os.lstat(root)
        paper_stat = os.lstat(paper_dir)
    except FileNotFoundError:
        _issue(issues, "evidence_import_paper_missing", paper_id, "paper directory does not exist")
        return False
    except OSError as exc:
        _issue(
            issues,
            "evidence_import_paper_path_unsafe",
            paper_id,
            f"cannot inspect paper directory safely: {type(exc).__name__}",
        )
        return False
    if (
        _is_reparse_component(root, root_stat)
        or _is_reparse_component(paper_dir, paper_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
        or not stat.S_ISDIR(paper_stat.st_mode)
    ):
        _issue(
            issues,
            "evidence_import_paper_path_unsafe",
            paper_id,
            "papers root and paper directory must be non-reparse directories",
        )
        return False
    return True


def _read_required_text(
    root: Path,
    paper_dir: Path,
    relative_path: str,
    snapshots: dict[Path, Any],
    issues: list[dict[str, str]],
) -> tuple[str, Any | None]:
    path = _absolute_lexical(paper_dir / relative_path)
    text, snapshot, error_kind, error = _snapshot_text(root, path, snapshots)
    if error_kind:
        code = (
            "evidence_import_input_missing"
            if error_kind == "missing"
            else "evidence_import_input_unsafe"
            if error_kind == "unsafe"
            else "evidence_import_snapshot_changed"
            if error_kind == "unstable"
            else "evidence_import_input_unreadable"
        )
        _issue(issues, code, relative_path, error)
    return text, snapshot


def _copy_evidence_contract(spec_contract: Any, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    path = "spec#evidence_contract"
    if not _exact_keys(spec_contract, _CONTRACT_KEYS, path, issues):
        return None
    contract: dict[str, Any] = {"schema_version": EVIDENCE_CONTRACT_SCHEMA_VERSION}
    for section_name, expected_keys in _CONTRACT_SECTION_KEYS.items():
        section = spec_contract.get(section_name)
        if not _exact_keys(section, expected_keys, f"{path}.{section_name}", issues):
            continue
        contract[section_name] = {key: section.get(key) for key in sorted(expected_keys)}
    contract["raw_artifact_ids"] = spec_contract.get("raw_artifact_ids")
    contract["aggregate_artifact_ids"] = spec_contract.get("aggregate_artifact_ids")
    return contract if not issues else None


def _copy_claims(spec_claims: Any, issues: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    if not isinstance(spec_claims, list) or not spec_claims:
        _issue(
            issues,
            "evidence_import_spec_claims_invalid",
            "spec#claims",
            "claims must be a non-empty list",
        )
        return None
    claims: list[dict[str, Any]] = []
    for index, claim in enumerate(spec_claims):
        path = f"spec#claims[{index}]"
        if not _exact_keys(claim, {"claim_id", "draft_excerpt", "evidence"}, path, issues):
            continue
        refs = claim.get("evidence")
        if not isinstance(refs, list) or not refs:
            _issue(
                issues,
                "evidence_import_spec_refs_invalid",
                f"{path}.evidence",
                "evidence must be a non-empty list",
            )
            continue
        copied_refs: list[dict[str, Any]] = []
        for ref_index, ref in enumerate(refs):
            ref_path = f"{path}.evidence[{ref_index}]"
            if not _exact_keys(ref, {"artifact_id", "row_id"}, ref_path, issues):
                continue
            copied_refs.append({"artifact_id": ref.get("artifact_id"), "row_id": ref.get("row_id")})
        claims.append(
            {
                "claim_id": claim.get("claim_id"),
                "type": "quantitative",
                "draft_excerpt": claim.get("draft_excerpt"),
                "evidence": copied_refs,
            }
        )
    return claims if not issues else None


def prepare_writing_evidence_import(
    papers_dir: Path | str,
    paper_id: str,
    spec_path: Path | str,
    *,
    check_only: bool = False,
    expected_manifest_sha256: str = "",
) -> dict[str, Any]:
    """Prepare or atomically commit a schema-v2 Step 5 evidence manifest.

    The import spec declares evidence and claim-to-row relationships; this
    function only adds fixed contract fields and hashes exact existing bytes.
    It never creates evidence, edits workflow state, or resumes model work.
    """

    paper_id = str(paper_id or "").strip()
    result = _result(paper_id, check_only=bool(check_only))
    issues: list[dict[str, str]] = result["issues"]
    if not _is_single_safe_name(paper_id):
        _issue(issues, "invalid_paper_id", paper_id, "paper_id must be one safe directory name")
        return result

    root = _absolute_lexical(papers_dir)
    paper_dir = _absolute_lexical(root / paper_id)
    if not _is_contained(root, paper_dir):
        _issue(issues, "evidence_import_paper_path_escape", paper_id, "paper directory escapes the papers root")
        return result
    if not _validate_paper_directory(root, paper_dir, paper_id, issues):
        return result

    resolved_spec, spec_error = _resolve_spec_path(paper_dir, spec_path)
    if resolved_spec is None:
        _issue(issues, "evidence_import_spec_path", str(spec_path), spec_error)
        return result
    spec_relative = resolved_spec.relative_to(paper_dir).as_posix()
    if spec_relative.casefold() in {
        "paper_materials.md",
        "draft_v2.tex",
        MANIFEST_RELATIVE_PATH.casefold(),
    } or spec_relative.casefold().startswith(f"{RECEIPT_DIRECTORY.casefold()}/"):
        _issue(
            issues,
            "evidence_import_spec_path",
            spec_relative,
            "spec must not replace a control file or use the receipt directory",
        )
        return result

    snapshots: dict[Path, Any] = {}
    spec_snapshot, spec_error_kind, spec_read_error = _read_stable_snapshot(root, resolved_spec, snapshots)
    if spec_snapshot is None:
        code = (
            "evidence_import_spec_missing"
            if spec_error_kind == "missing"
            else "evidence_import_spec_snapshot_changed"
            if spec_error_kind == "unstable"
            else "evidence_import_spec_unreadable"
        )
        _issue(issues, code, spec_relative, spec_read_error)
        return result
    try:
        loaded_spec = json.loads(spec_snapshot.data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _issue(
            issues,
            "evidence_import_spec_unreadable",
            spec_relative,
            f"spec must be valid UTF-8 JSON: {type(exc).__name__}",
        )
        return result
    if not _exact_keys(loaded_spec, _TOP_LEVEL_KEYS, "spec", issues):
        _add_snapshot_changes(root, paper_dir, snapshots, issues)
        return result
    if type(loaded_spec.get("import_spec_version")) is not int or loaded_spec.get("import_spec_version") != 1:
        _issue(
            issues,
            "evidence_import_spec_schema",
            "spec#import_spec_version",
            "import_spec_version must equal integer 1",
        )

    materials_text, materials_snapshot = _read_required_text(
        root,
        paper_dir,
        "paper_materials.md",
        snapshots,
        issues,
    )
    draft_text, draft_snapshot = _read_required_text(
        root,
        paper_dir,
        "draft_v2.tex",
        snapshots,
        issues,
    )
    if materials_snapshot is not None:
        statuses = [
            match.group(1)
            for line in materials_text.splitlines()
            if (match := _STATUS_LINE_RE.fullmatch(line.strip()))
        ]
        if "READY" not in statuses:
            _issue(
                issues,
                "paper_materials_not_ready",
                "paper_materials.md",
                "paper_materials.md must contain a standalone Status: READY line",
            )
        elif any(status != "READY" for status in statuses):
            _issue(
                issues,
                "paper_materials_status_conflict",
                "paper_materials.md",
                "paper_materials.md contains a conflicting standalone Status line",
            )
    if draft_snapshot is not None:
        for label, marker in FORBIDDEN_DRAFT_MARKERS:
            if marker.search(draft_text):
                _issue(
                    issues,
                    "draft_v2_marker_remaining",
                    "draft_v2.tex",
                    f"draft_v2.tex still contains forbidden marker: {label}",
                )

    artifacts_spec = loaded_spec.get("artifacts")
    artifacts: list[dict[str, Any]] = []
    artifact_inputs: list[tuple[str, Any]] = []
    if not isinstance(artifacts_spec, list) or not artifacts_spec:
        _issue(
            issues,
            "evidence_import_spec_artifacts_invalid",
            "spec#artifacts",
            "artifacts must be a non-empty list",
        )
    else:
        for index, artifact in enumerate(artifacts_spec):
            artifact_ref = f"spec#artifacts[{index}]"
            if not _exact_keys(artifact, {"id", "role", "path"}, artifact_ref, issues):
                continue
            raw_path = artifact.get("path")
            if not isinstance(raw_path, str):
                _issue(
                    issues,
                    "evidence_import_artifact_path",
                    artifact_ref,
                    "artifact path must be a paper-relative string",
                )
                continue
            artifact_path, path_error = _resolve_paper_relative(paper_dir, raw_path)
            if artifact_path is None:
                _issue(issues, "evidence_import_artifact_path", raw_path, path_error)
                continue
            normalized_path = artifact_path.relative_to(paper_dir).as_posix()
            if artifact_path == resolved_spec:
                _issue(
                    issues,
                    "evidence_import_artifact_path",
                    normalized_path,
                    "the import spec cannot also be an evidence artifact",
                )
                continue
            if normalized_path.casefold().startswith(f"{RECEIPT_DIRECTORY.casefold()}/"):
                _issue(
                    issues,
                    "evidence_import_artifact_path",
                    normalized_path,
                    "the importer receipt directory is reserved",
                )
                continue
            snapshot, error_kind, read_error = _read_stable_snapshot(root, artifact_path, snapshots)
            if snapshot is None:
                code = (
                    "evidence_import_artifact_missing"
                    if error_kind == "missing"
                    else "evidence_import_artifact_snapshot_changed"
                    if error_kind == "unstable"
                    else "evidence_import_artifact_unsafe"
                    if error_kind == "unsafe"
                    else "evidence_import_artifact_unreadable"
                )
                _issue(issues, code, normalized_path, read_error)
                continue
            artifact_id = artifact.get("id")
            artifacts.append(
                {
                    "id": artifact_id,
                    "role": artifact.get("role"),
                    "path": normalized_path,
                    "sha256": snapshot.sha256,
                }
            )
            artifact_inputs.append((str(artifact_id or ""), snapshot))

    contract = _copy_evidence_contract(loaded_spec.get("evidence_contract"), issues)
    claims = _copy_claims(loaded_spec.get("claims"), issues)
    manifest_path = _absolute_lexical(paper_dir / MANIFEST_RELATIVE_PATH)
    previous_manifest, previous_error_kind, previous_error = _read_stable_snapshot(root, manifest_path, snapshots)
    if previous_manifest is None and previous_error_kind not in {"", "missing"}:
        _issue(
            issues,
            "evidence_import_existing_manifest_unsafe",
            MANIFEST_RELATIVE_PATH,
            previous_error,
        )

    if not issues and materials_snapshot is not None and draft_snapshot is not None and contract is not None and claims is not None:
        candidate = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "paper_id": paper_id,
            "status": "READY",
            "draft_v2_sha256": draft_snapshot.sha256,
            "paper_materials_sha256": materials_snapshot.sha256,
            "artifacts": artifacts,
            "evidence_contract": contract,
            "claim_evidence": {
                "coverage": CLAIM_COVERAGE_POLICY,
                "draft_v2_sha256": draft_snapshot.sha256,
                "claims": claims,
            },
        }
        gate_issues: list[dict[str, str]] = []
        artifact_by_id, artifact_count = _validate_artifacts(
            root=root,
            paper_dir=paper_dir,
            manifest_path=manifest_path,
            artifacts=artifacts,
            require_v2=True,
            issues=gate_issues,
            snapshots=snapshots,
        )
        _validate_evidence_contract(candidate, artifact_by_id, gate_issues)
        claim_count = _validate_claim_evidence(
            manifest=candidate,
            draft_text=draft_text,
            draft_hash=draft_snapshot.sha256,
            artifact_by_id=artifact_by_id,
            issues=gate_issues,
        )
        issues.extend(gate_issues)
        result["artifact_count"] = artifact_count
        result["claim_count"] = claim_count
    else:
        candidate = None

    _add_snapshot_changes(root, paper_dir, snapshots, issues)
    if issues or candidate is None or spec_snapshot is None or materials_snapshot is None or draft_snapshot is None:
        return result

    manifest_sha256 = hashlib.sha256(_json_bytes(candidate)).hexdigest()
    expected_manifest_sha256 = str(expected_manifest_sha256 or "").strip().casefold()
    if expected_manifest_sha256 and expected_manifest_sha256 != manifest_sha256:
        _issue(
            issues,
            "evidence_import_preview_changed",
            MANIFEST_RELATIVE_PATH,
            "candidate no longer matches the explicitly confirmed check-only preview",
        )
        return result
    inputs = [
        _snapshot_record("paper_materials", paper_dir, materials_snapshot),
        _snapshot_record("draft_v2", paper_dir, draft_snapshot),
    ]
    inputs.extend(_snapshot_record(f"artifact:{artifact_id}", paper_dir, snapshot) for artifact_id, snapshot in artifact_inputs)
    inputs.sort(key=lambda item: (item["path"].casefold(), item["path"], item["kind"]))
    receipt = {
        "schema": IMPORT_RECEIPT_SCHEMA,
        "paper_id": paper_id,
        "operation": "prepare_schema_v2_manifest",
        "spec": {
            "path": spec_relative,
            "sha256": spec_snapshot.sha256,
            "size": len(spec_snapshot.data),
        },
        "manifest": {
            "path": MANIFEST_RELATIVE_PATH,
            "sha256": manifest_sha256,
            "previous_sha256": previous_manifest.sha256 if previous_manifest is not None else None,
        },
        "inputs": inputs,
        "effects": {
            "model_work_resumed": False,
            "scheduler_state_changed": False,
        },
    }
    receipt_sha256 = hashlib.sha256(_json_bytes(receipt)).hexdigest()
    receipt_relative = f"{RECEIPT_DIRECTORY}/{receipt_sha256}.json"
    receipt_path = _absolute_lexical(paper_dir / receipt_relative)
    receipt_components, receipt_error = _inspect_safe_path(root, receipt_path)
    if receipt_components is None:
        _issue(issues, "evidence_import_receipt_path_unsafe", receipt_relative, receipt_error)
        return result
    result.update(
        {
            "ok": True,
            "status": "CHECKED" if check_only else "PREPARED",
            "candidate_ready": True,
            "manifest_sha256": manifest_sha256,
            "receipt_path": receipt_relative,
        }
    )
    if check_only:
        return result

    receipt_components, receipt_error = _inspect_safe_path(root, receipt_path)
    if receipt_components is None:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        _issue(issues, "evidence_import_receipt_path_unsafe", receipt_relative, receipt_error)
        return result
    try:
        write_json_atomic(receipt_path, receipt)
        result["receipt_written"] = True
    except OSError as exc:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        _issue(
            issues,
            "evidence_import_receipt_write_failed",
            receipt_relative,
            f"cannot atomically write provenance receipt: {type(exc).__name__}",
        )
        return result

    _add_snapshot_changes(root, paper_dir, snapshots, issues)
    if issues:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        return result
    manifest_components, manifest_error = _inspect_safe_path(root, manifest_path)
    if manifest_components is None:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        _issue(
            issues,
            "evidence_import_manifest_path_unsafe",
            MANIFEST_RELATIVE_PATH,
            manifest_error,
        )
        return result
    expected_manifest_hash = previous_manifest.sha256 if previous_manifest is not None else None
    try:
        manifest_written = write_json_atomic_if_unchanged(
            manifest_path,
            candidate,
            expected_manifest_hash,
        )
    except OSError as exc:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        _issue(
            issues,
            "evidence_import_manifest_write_failed",
            MANIFEST_RELATIVE_PATH,
            f"cannot atomically write verification manifest: {type(exc).__name__}",
        )
        return result
    if not manifest_written:
        result.update({"ok": False, "status": "BLOCKED", "candidate_ready": False})
        _issue(
            issues,
            "evidence_import_manifest_conflict",
            MANIFEST_RELATIVE_PATH,
            "manifest changed after preparation; rerun check-only and commit from fresh inputs",
        )
        return result

    result["manifest_written"] = True
    gate = validate_writing_evidence(root, paper_id)
    result["gate"] = gate
    result["transition_ready"] = bool(gate.get("transition_ready"))
    if gate.get("ready"):
        result.update({"status": "IMPORTED", "candidate_ready": True, "ok": True})
    else:
        result.update({"status": "BLOCKED", "candidate_ready": False, "ok": False})
        issues.extend(gate.get("issues", []))
    return result
