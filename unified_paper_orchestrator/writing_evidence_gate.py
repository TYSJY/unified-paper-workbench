from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


LEGACY_MANIFEST_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
EVIDENCE_CONTRACT_SCHEMA_VERSION = 1
CLAIM_COVERAGE_POLICY = "all_quantitative_claims_in_draft_v2"
MANUAL_COMPLETION_SCHEMA = "unified-paper-orchestrator/writing-manual-data-completion/v1"
AUTO_TRUST_COMPLETION_SCHEMA = "unified-paper-orchestrator/writing-policy-data-completion/v1"
MANUAL_COMPLETION_RELATIVE_PATH = "results/manual_data_completion.json"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STATUS_LINE_RE = re.compile(r"^Status\s*:\s*(.*?)\s*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_QUANTITATIVE_SIGNAL_RE = re.compile(r"(?:\d|%|\\%|\\(?:num|SI|qty)\s*\{)")
_ARTIFACT_ROLES = {
    "raw",
    "aggregate",
    "environment",
    "workflows",
    "faults",
    "seeds",
    "metrics",
    "limitations",
    "other",
}
_CONTROL_ARTIFACT_PATHS = {
    "draft_v2.tex",
    "paper_materials.md",
    MANUAL_COMPLETION_RELATIVE_PATH,
    "results/verification_manifest.json",
}
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_INPUT_SNAPSHOT_SCHEMA = "unified-paper-orchestrator/writing-gate-input-snapshot/v1"

# These patterns intentionally target explicit workflow markers.  Ordinary prose
# such as "an unresolved challenge", "placeholder variable", or "simulated
# annealing" must not block a paper merely because it uses those words.
FORBIDDEN_DRAFT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("VERIFY", re.compile(r"(?<![A-Za-z0-9_])VERIFY(?![A-Za-z0-9_])")),
    ("\u3010\u9a8c\u3011", re.compile("\u3010\u9a8c\u3011")),
    ("SIMULATED_TO_REPLACE", re.compile(r"(?i)(?<![A-Za-z0-9_])SIMULATED_TO_REPLACE(?![A-Za-z0-9_])")),
    ("verification-required placeholder", re.compile(r"(?i)verification-required\s+placeholder")),
    ("BLOCKING TODO", re.compile(r"(?i)(?<![A-Za-z0-9_])BLOCKING(?:[\s_-]+)TODO(?![A-Za-z0-9_])")),
    ("UNRESOLVED", re.compile(r"(?<![A-Za-z0-9_])UNRESOLVED(?![A-Za-z0-9_])")),
    ("TODO marker", re.compile(r"(?:\\todo\s*\{|\[TODO\]|(?<![A-Za-z0-9_])TODO\s*:)", re.MULTILINE)),
    (
        "PLACEHOLDER marker",
        re.compile(r"(?:\\placeholder\s*\{|\[PLACEHOLDER\]|(?<![A-Za-z0-9_])PLACEHOLDER\s*:)", re.MULTILINE),
    ),
    (
        "SIMULATED marker",
        re.compile(r"(?:\\simulated\s*\{|\[SIMULATED\]|(?<![A-Za-z0-9_])SIMULATED\s*:)", re.MULTILINE),
    ),
)


@dataclass(frozen=True)
class _StableFileSnapshot:
    path: Path
    data: bytes
    sha256: str
    stat_signature: tuple[int, ...]
    component_signatures: tuple[tuple[Path, tuple[int, ...]], ...]


def _absolute_lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_contained(root: Path, path: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(root), os.fspath(path)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(os.fspath(root))


def _stat_mtime_ns(value: os.stat_result) -> int:
    return int(getattr(value, "st_mtime_ns", round(value.st_mtime * 1_000_000_000)))


def _file_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        _stat_mtime_ns(value),
        int(getattr(value, "st_file_attributes", 0)),
    )


def _component_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
        int(getattr(value, "st_file_attributes", 0)),
    )


def _is_reparse_component(path: Path, value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    if int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE:
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(callable(is_junction) and is_junction(path))


def _path_components(path: Path) -> tuple[Path, ...]:
    return (*reversed(path.parents), path)


def _inspect_safe_path(root: Path, path: Path) -> tuple[tuple[tuple[Path, tuple[int, ...]], ...] | None, str]:
    root = _absolute_lexical(root)
    path = _absolute_lexical(path)
    if not _is_contained(root, path):
        return None, "path escapes the configured papers root"

    signatures: list[tuple[Path, tuple[int, ...]]] = []
    try:
        for component in _path_components(path):
            try:
                component_stat = os.lstat(component)
            except FileNotFoundError:
                continue
            if _is_reparse_component(component, component_stat):
                return None, f"reparse path component is forbidden: {component}"
            signatures.append((component, _component_signature(component_stat)))

        root_resolved = root.resolve(strict=False)
        path_resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return None, f"cannot inspect path safely: {type(exc).__name__}"
    if not _is_contained(root_resolved, path_resolved):
        return None, "resolved path escapes the configured papers root"
    return tuple(signatures), ""


def _read_stable_snapshot(
    root: Path,
    path: Path,
    cache: dict[Path, _StableFileSnapshot],
) -> tuple[_StableFileSnapshot | None, str, str]:
    path = _absolute_lexical(path)
    cached = cache.get(path)
    if cached is not None:
        return cached, "", ""

    components_before, path_error = _inspect_safe_path(root, path)
    if components_before is None:
        return None, "unsafe", path_error
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None, "missing", "file does not exist"
    except OSError as exc:
        return None, "unreadable", f"cannot stat file: {type(exc).__name__}"
    if _is_reparse_component(path, before) or not stat.S_ISREG(before.st_mode):
        return None, "unsafe", "input must be a regular non-reparse file"

    try:
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            data = handle.read()
            opened_after = os.fstat(handle.fileno())
        after = os.lstat(path)
    except FileNotFoundError:
        return None, "unstable", "file disappeared while it was being read"
    except OSError as exc:
        return None, "unreadable", f"cannot read file: {type(exc).__name__}"

    signature = _file_signature(before)
    if (
        signature != _file_signature(opened_before)
        or signature != _file_signature(opened_after)
        or signature != _file_signature(after)
        or len(data) != before.st_size
    ):
        return None, "unstable", "file stat, size, or mtime changed while it was being read"
    components_after, path_error = _inspect_safe_path(root, path)
    if components_after is None:
        return None, "unsafe", path_error
    if components_after != components_before:
        return None, "unstable", "a path component changed while the file was being read"

    snapshot = _StableFileSnapshot(
        path=path,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        stat_signature=signature,
        component_signatures=components_before,
    )
    cache[path] = snapshot
    return snapshot, "", ""


def _verify_stable_snapshots(root: Path, snapshots: dict[Path, _StableFileSnapshot]) -> list[tuple[Path, str]]:
    changed: list[tuple[Path, str]] = []
    for snapshot in snapshots.values():
        components, path_error = _inspect_safe_path(root, snapshot.path)
        if components is None:
            changed.append((snapshot.path, path_error))
            continue
        try:
            current = os.lstat(snapshot.path)
        except OSError as exc:
            changed.append((snapshot.path, f"cannot restat input: {type(exc).__name__}"))
            continue
        if _is_reparse_component(snapshot.path, current):
            changed.append((snapshot.path, "input became a reparse point"))
        elif _file_signature(current) != snapshot.stat_signature:
            changed.append((snapshot.path, "input stat, size, or mtime changed during validation"))
        elif components != snapshot.component_signatures:
            changed.append((snapshot.path, "a path component changed during validation"))
    return changed


def _input_snapshot(
    root: Path,
    snapshots: dict[Path, _StableFileSnapshot],
    *,
    gate_profile: str,
    paper_id: str,
) -> dict[str, Any]:
    """Bind a READY decision to the exact bytes already read by the gate."""

    files = sorted(
        (
            {
                "path": snapshot.path.relative_to(root).as_posix(),
                "sha256": snapshot.sha256,
                "size": len(snapshot.data),
            }
            for snapshot in snapshots.values()
        ),
        key=lambda item: (item["path"].casefold(), item["path"]),
    )
    payload = {
        "schema": _INPUT_SNAPSHOT_SCHEMA,
        "gate_profile": gate_profile,
        "paper_id": paper_id,
        "scope": "papers_root",
        "files": files,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        **payload,
        "snapshot_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _snapshot_text(
    root: Path,
    path: Path,
    snapshots: dict[Path, _StableFileSnapshot],
) -> tuple[str, _StableFileSnapshot | None, str, str]:
    snapshot, error_kind, error = _read_stable_snapshot(root, path, snapshots)
    if snapshot is None:
        return "", None, error_kind, error
    try:
        return snapshot.data.decode("utf-8-sig"), snapshot, "", ""
    except UnicodeError as exc:
        return "", snapshot, "unreadable", f"cannot decode UTF-8 input: {type(exc).__name__}"


def _issue(issues: list[dict[str, str]], code: str, path: str, message: str) -> None:
    issues.append({"code": code, "path": path, "message": message})


def _is_single_safe_name(value: str) -> bool:
    if not value or value in {".", ".."} or "\x00" in value:
        return False
    native = Path(value)
    windows = PureWindowsPath(value)
    return (
        native.name == value
        and not native.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and "/" not in value
        and "\\" not in value
    )


def _resolve_paper_relative(paper_dir: Path, raw_path: Any) -> tuple[Path | None, str]:
    value = str(raw_path or "").strip()
    if not value or "\x00" in value:
        return None, "artifact path is empty or invalid"
    native = Path(value)
    windows = PureWindowsPath(value)
    if native.is_absolute() or windows.is_absolute() or windows.drive:
        return None, "artifact path must be paper-relative"
    if "\\" in value or any(part in {".", ".."} for part in (*native.parts, *windows.parts)):
        return None, "artifact path contains a forbidden traversal component"
    resolved = _absolute_lexical(paper_dir / native)
    if not _is_contained(_absolute_lexical(paper_dir), resolved):
        return None, "artifact path escapes the paper directory"
    return resolved, ""


def _resolve_fixed_paper_path(paper_dir: Path, relative_path: str) -> Path | None:
    resolved = _absolute_lexical(paper_dir / relative_path)
    if not _is_contained(_absolute_lexical(paper_dir), resolved):
        return None
    return resolved


def _nonempty_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        normalized.append(item.strip())
    return normalized


def _nonempty_scalar_list(value: Any) -> list[str | int] | None:
    if not isinstance(value, list) or not value:
        return None
    normalized: list[str | int] = []
    for item in value:
        if type(item) is int:
            normalized.append(item)
        elif isinstance(item, str) and item.strip():
            normalized.append(item.strip())
        else:
            return None
    return normalized


def _load_evidence_row_ids(path: Path, data: bytes) -> tuple[set[str] | None, str]:
    """Load evidence_id values from the already hash-verified byte snapshot."""

    try:
        suffix = path.suffix.casefold()
        text = data.decode("utf-8-sig")
        if suffix == ".jsonl":
            records: list[Any] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    return None, f"invalid JSONL at line {line_number}"
        elif suffix == ".json":
            loaded = json.loads(text)
            if isinstance(loaded, list):
                records = loaded
            elif isinstance(loaded, dict) and isinstance(loaded.get("rows"), list):
                records = loaded["rows"]
            elif isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
                records = loaded["records"]
            else:
                return None, "JSON evidence must be a list or contain a rows/records list"
        elif suffix == ".csv":
            records = list(csv.DictReader(io.StringIO(text, newline="")))
        else:
            return None, "row-bound evidence must use .json, .jsonl, or .csv"
    except (UnicodeError, json.JSONDecodeError, csv.Error) as exc:
        return None, f"cannot parse row-bound evidence: {type(exc).__name__}"

    row_ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            return None, f"evidence row {index} must be an object"
        row_id = record.get("evidence_id")
        if not isinstance(row_id, str) or not _IDENTIFIER_RE.fullmatch(row_id.strip()):
            return None, f"evidence row {index} must contain a safe evidence_id"
        row_id = row_id.strip()
        if row_id in row_ids:
            return None, f"duplicate evidence_id: {row_id}"
        row_ids.add(row_id)
    if not row_ids:
        return None, "row-bound evidence contains no records"
    return row_ids, ""


def _validate_artifacts(
    *,
    root: Path,
    paper_dir: Path,
    manifest_path: Path | None,
    artifacts: Any,
    require_v2: bool,
    issues: list[dict[str, str]],
    snapshots: dict[Path, _StableFileSnapshot],
) -> tuple[dict[str, dict[str, Any]], int]:
    if not isinstance(artifacts, list) or not artifacts:
        _issue(
            issues,
            "verification_artifacts_invalid",
            "results/verification_manifest.json",
            "artifacts must be a non-empty list",
        )
        return {}, 0

    artifact_by_id: dict[str, dict[str, Any]] = {}
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    for index, artifact in enumerate(artifacts):
        manifest_ref = f"results/verification_manifest.json#artifacts[{index}]"
        if not isinstance(artifact, dict):
            _issue(
                issues,
                "verification_artifact_invalid",
                manifest_ref,
                "artifact must be an object with path and sha256",
            )
            continue

        artifact_id = str(artifact.get("id") or "").strip()
        role = str(artifact.get("role") or "").strip()
        if require_v2:
            if not _IDENTIFIER_RE.fullmatch(artifact_id):
                _issue(
                    issues,
                    "verification_artifact_id_invalid",
                    manifest_ref,
                    "schema v2 artifacts require a safe id",
                )
            elif artifact_id.casefold() in seen_ids:
                _issue(
                    issues,
                    "verification_artifact_id_duplicate",
                    artifact_id,
                    "artifact id is listed more than once (case-insensitive)",
                )
            else:
                seen_ids.add(artifact_id.casefold())
            if role not in _ARTIFACT_ROLES:
                _issue(
                    issues,
                    "verification_artifact_role_invalid",
                    artifact_id or manifest_ref,
                    "schema v2 artifact role is missing or unsupported",
                )

        raw_path = str(artifact.get("path") or "").strip()
        expected_hash = str(artifact.get("sha256") or "").strip()
        artifact_path, path_error = _resolve_paper_relative(paper_dir, raw_path)
        if artifact_path is None:
            _issue(issues, "verification_artifact_path", raw_path or manifest_ref, path_error)
            continue
        normalized_path = artifact_path.relative_to(paper_dir).as_posix()
        normalized_key = normalized_path.casefold()
        if normalized_key in seen_paths:
            _issue(
                issues,
                "verification_artifact_duplicate",
                normalized_path,
                "artifact path is listed more than once (case-insensitive)",
            )
            continue
        seen_paths.add(normalized_key)
        if manifest_path is not None and artifact_path == manifest_path:
            _issue(
                issues,
                "verification_artifact_self_reference",
                normalized_path,
                "verification manifest cannot hash-bind itself",
            )
            continue
        if require_v2 and normalized_key in _CONTROL_ARTIFACT_PATHS:
            _issue(
                issues,
                "verification_artifact_control_reference",
                normalized_path,
                "draft, materials, and manifest control files cannot be evidence artifacts",
            )
            continue
        snapshot, snapshot_error_kind, snapshot_error = _read_stable_snapshot(root, artifact_path, snapshots)
        if snapshot is None and snapshot_error_kind == "missing":
            _issue(
                issues,
                "verification_artifact_missing",
                normalized_path,
                "artifact file does not exist",
            )
            continue
        if snapshot is None:
            issue_code = (
                "verification_artifact_path"
                if snapshot_error_kind == "unsafe"
                else "verification_artifact_snapshot_unstable"
                if snapshot_error_kind == "unstable"
                else "verification_artifact_unreadable"
            )
            _issue(issues, issue_code, normalized_path, snapshot_error)
            continue
        if not _SHA256_RE.fullmatch(expected_hash):
            _issue(
                issues,
                "verification_artifact_hash_invalid",
                normalized_path,
                "artifact sha256 must be a 64-character hexadecimal value",
            )
            continue
        verified = snapshot.sha256 == expected_hash.casefold()
        if not verified:
            _issue(
                issues,
                "verification_artifact_hash_mismatch",
                normalized_path,
                "artifact sha256 does not match file bytes",
            )
        if require_v2 and _IDENTIFIER_RE.fullmatch(artifact_id) and artifact_id.casefold() in seen_ids:
            # Only the first exact id is addressable.  A duplicate already has a
            # dedicated issue and must not silently replace the original.
            if artifact_id not in artifact_by_id:
                artifact_by_id[artifact_id] = {
                    "id": artifact_id,
                    "role": role,
                    "path": artifact_path,
                    "normalized_path": normalized_path,
                    "verified": verified,
                    "snapshot_data": snapshot.data,
                }
    return artifact_by_id, len(artifacts)


def _validate_artifact_id_refs(
    *,
    values: Any,
    path: str,
    artifact_by_id: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
    expected_role: str | None = None,
) -> list[str]:
    artifact_ids = _nonempty_string_list(values)
    if artifact_ids is None:
        _issue(issues, "evidence_contract_artifact_ids_invalid", path, "artifact_ids must be a non-empty string list")
        return []
    seen: set[str] = set()
    for artifact_id in artifact_ids:
        if artifact_id in seen:
            _issue(issues, "evidence_contract_artifact_id_duplicate", path, f"duplicate artifact id: {artifact_id}")
            continue
        seen.add(artifact_id)
        artifact = artifact_by_id.get(artifact_id)
        if artifact is None:
            _issue(issues, "evidence_contract_artifact_missing", path, f"unknown artifact id: {artifact_id}")
        elif expected_role is not None and artifact.get("role") != expected_role:
            _issue(
                issues,
                "evidence_contract_artifact_role",
                path,
                f"artifact {artifact_id} must have role {expected_role}",
            )
    return artifact_ids


def _validate_evidence_contract(
    manifest: dict[str, Any],
    artifact_by_id: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    contract_path = "results/verification_manifest.json#evidence_contract"
    contract = manifest.get("evidence_contract")
    if not isinstance(contract, dict):
        _issue(
            issues,
            "evidence_contract_missing",
            contract_path,
            "schema v2 requires a machine-readable evidence_contract object",
        )
        return
    if (
        type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != EVIDENCE_CONTRACT_SCHEMA_VERSION
    ):
        _issue(
            issues,
            "evidence_contract_schema",
            contract_path,
            "evidence_contract.schema_version must equal integer 1",
        )

    section_specs: dict[str, tuple[str, str]] = {
        "environment": ("description", "string"),
        "workflows": ("ids", "strings"),
        "faults": ("ids", "strings"),
        "seeds": ("values", "scalars"),
        "metrics": ("ids", "strings"),
        "limitations": ("items", "strings"),
    }
    for section_name, (content_key, content_kind) in section_specs.items():
        section_path = f"{contract_path}.{section_name}"
        section = contract.get(section_name)
        if not isinstance(section, dict):
            _issue(
                issues,
                "evidence_contract_section_missing",
                section_path,
                f"{section_name} must be a machine-readable object",
            )
            continue
        _validate_artifact_id_refs(
            values=section.get("artifact_ids"),
            path=f"{section_path}.artifact_ids",
            artifact_by_id=artifact_by_id,
            issues=issues,
        )
        content = section.get(content_key)
        if content_kind == "string":
            valid_content = isinstance(content, str) and bool(content.strip())
        elif content_kind == "scalars":
            valid_content = _nonempty_scalar_list(content) is not None
        else:
            valid_content = _nonempty_string_list(content) is not None
        if not valid_content:
            _issue(
                issues,
                "evidence_contract_section_invalid",
                f"{section_path}.{content_key}",
                f"{section_name}.{content_key} must be non-empty",
            )

    _validate_artifact_id_refs(
        values=contract.get("raw_artifact_ids"),
        path=f"{contract_path}.raw_artifact_ids",
        artifact_by_id=artifact_by_id,
        issues=issues,
        expected_role="raw",
    )
    _validate_artifact_id_refs(
        values=contract.get("aggregate_artifact_ids"),
        path=f"{contract_path}.aggregate_artifact_ids",
        artifact_by_id=artifact_by_id,
        issues=issues,
        expected_role="aggregate",
    )


def _validate_claim_evidence(
    *,
    manifest: dict[str, Any],
    draft_text: str,
    draft_hash: str,
    artifact_by_id: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
) -> int:
    claim_map_path = "results/verification_manifest.json#claim_evidence"
    claim_map = manifest.get("claim_evidence")
    if not isinstance(claim_map, dict):
        _issue(
            issues,
            "claim_evidence_missing",
            claim_map_path,
            "schema v2 requires a claim_evidence object",
        )
        return 0
    if claim_map.get("coverage") != CLAIM_COVERAGE_POLICY:
        _issue(
            issues,
            "claim_evidence_coverage",
            claim_map_path,
            f"coverage must equal {CLAIM_COVERAGE_POLICY}",
        )
    claim_draft_hash = str(claim_map.get("draft_v2_sha256") or "").strip()
    if not _SHA256_RE.fullmatch(claim_draft_hash) or (draft_hash and claim_draft_hash.casefold() != draft_hash):
        _issue(
            issues,
            "claim_evidence_draft_hash",
            claim_map_path,
            "claim_evidence.draft_v2_sha256 must match draft_v2.tex bytes",
        )

    claims = claim_map.get("claims")
    if not isinstance(claims, list) or not claims:
        _issue(
            issues,
            "claim_evidence_claims_invalid",
            claim_map_path,
            "claims must be a non-empty list covering every quantitative claim",
        )
        return 0

    seen_claim_ids: set[str] = set()
    row_cache: dict[str, tuple[set[str] | None, str]] = {}
    for index, claim in enumerate(claims):
        claim_path = f"{claim_map_path}.claims[{index}]"
        if not isinstance(claim, dict):
            _issue(issues, "claim_evidence_claim_invalid", claim_path, "claim must be an object")
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        if not _IDENTIFIER_RE.fullmatch(claim_id):
            _issue(issues, "claim_evidence_claim_id", claim_path, "claim_id must be a safe identifier")
        elif claim_id.casefold() in seen_claim_ids:
            _issue(issues, "claim_evidence_claim_duplicate", claim_path, "claim_id is duplicated")
        else:
            seen_claim_ids.add(claim_id.casefold())
        if claim.get("type") != "quantitative":
            _issue(issues, "claim_evidence_claim_type", claim_path, "claim type must equal quantitative")

        excerpt = claim.get("draft_excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            _issue(issues, "claim_evidence_excerpt_invalid", claim_path, "draft_excerpt must be non-empty")
        else:
            occurrences = draft_text.count(excerpt)
            if occurrences == 0:
                _issue(
                    issues,
                    "claim_evidence_excerpt_missing",
                    claim_path,
                    "draft_excerpt does not occur in draft_v2.tex",
                )
            elif occurrences > 1:
                _issue(
                    issues,
                    "claim_evidence_excerpt_ambiguous",
                    claim_path,
                    "draft_excerpt must identify exactly one location",
                )
            if not _QUANTITATIVE_SIGNAL_RE.search(excerpt):
                _issue(
                    issues,
                    "claim_evidence_excerpt_not_quantitative",
                    claim_path,
                    "draft_excerpt must contain an explicit quantitative signal",
                )

        evidence_refs = claim.get("evidence")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            _issue(
                issues,
                "claim_evidence_refs_invalid",
                claim_path,
                "each quantitative claim requires at least one row-bound evidence reference",
            )
            continue
        seen_refs: set[tuple[str, str]] = set()
        verified_raw_row = False
        for ref_index, evidence_ref in enumerate(evidence_refs):
            ref_path = f"{claim_path}.evidence[{ref_index}]"
            if not isinstance(evidence_ref, dict):
                _issue(issues, "claim_evidence_ref_invalid", ref_path, "evidence reference must be an object")
                continue
            artifact_id = str(evidence_ref.get("artifact_id") or "").strip()
            row_id = str(evidence_ref.get("row_id") or "").strip()
            if not _IDENTIFIER_RE.fullmatch(artifact_id) or not _IDENTIFIER_RE.fullmatch(row_id):
                _issue(
                    issues,
                    "claim_evidence_ref_invalid",
                    ref_path,
                    "artifact_id and row_id must be safe identifiers",
                )
                continue
            ref_key = (artifact_id, row_id)
            if ref_key in seen_refs:
                _issue(issues, "claim_evidence_ref_duplicate", ref_path, "evidence reference is duplicated")
                continue
            seen_refs.add(ref_key)
            artifact = artifact_by_id.get(artifact_id)
            if artifact is None:
                _issue(issues, "claim_evidence_artifact_missing", ref_path, f"unknown artifact id: {artifact_id}")
                continue
            if artifact.get("role") not in {"raw", "aggregate"}:
                _issue(
                    issues,
                    "claim_evidence_artifact_role",
                    ref_path,
                    "claim evidence must reference a raw or aggregate artifact",
                )
                continue
            if artifact_id not in row_cache:
                row_cache[artifact_id] = _load_evidence_row_ids(artifact["path"], artifact["snapshot_data"])
            row_ids, row_error = row_cache[artifact_id]
            if row_ids is None:
                _issue(issues, "claim_evidence_rows_unreadable", ref_path, row_error)
                continue
            if row_id not in row_ids:
                _issue(
                    issues,
                    "claim_evidence_row_missing",
                    ref_path,
                    f"row_id {row_id} does not exist in artifact {artifact_id}",
                )
                continue
            if artifact.get("role") == "raw" and artifact.get("verified"):
                verified_raw_row = True
        if not verified_raw_row:
            _issue(
                issues,
                "claim_evidence_raw_row_required",
                claim_path,
                "each quantitative claim must resolve to at least one hash-verified raw evidence row",
            )
    return len(claims)


def validate_writing_evidence(papers_dir: Path | str, paper_id: str) -> dict[str, Any]:
    """Read and validate the deterministic Step 5 -> Step 6 evidence gate.

    This function is deliberately side-effect free.  Schema v2 is the only
    contract that can authorize the transition.  A valid schema-v1 manifest is
    reported as ``LEGACY_V1`` for read-only compatibility, but it is not
    transition-ready because it has no complete claim/evidence contract.
    """

    paper_id = str(paper_id or "").strip()
    result: dict[str, Any] = {
        "paper_id": paper_id,
        "status": "BLOCKED",
        "ready": False,
        "transition_ready": False,
        "legacy_read_only": False,
        "legacy_integrity_ready": False,
        "manual_completion": False,
        "policy_completion": False,
        "transition_mode": "",
        "schema_version": None,
        "artifact_count": 0,
        "claim_count": 0,
        "draft_v2_sha256": "",
        "paper_materials_sha256": "",
        "issues": [],
    }
    issues: list[dict[str, str]] = result["issues"]

    if not _is_single_safe_name(paper_id):
        _issue(issues, "invalid_paper_id", paper_id, "paper_id must be one safe directory name")
        return result

    root = _absolute_lexical(papers_dir)
    paper_dir = _absolute_lexical(root / paper_id)
    if not _is_contained(root, paper_dir):
        _issue(issues, "paper_path_escape", paper_id, "paper directory escapes the configured papers root")
        return result
    root_components, root_error = _inspect_safe_path(root, root)
    paper_components, paper_error = _inspect_safe_path(root, paper_dir)
    if root_components is None or paper_components is None:
        _issue(
            issues,
            "paper_path_unsafe",
            paper_id,
            root_error or paper_error,
        )
        return result
    try:
        root_stat = os.lstat(root)
        paper_stat = os.lstat(paper_dir)
    except FileNotFoundError:
        _issue(issues, "paper_directory_missing", paper_id, "paper directory does not exist")
        return result
    except OSError as exc:
        _issue(
            issues,
            "paper_path_unsafe",
            paper_id,
            f"cannot inspect paper directory safely: {type(exc).__name__}",
        )
        return result
    if (
        _is_reparse_component(root, root_stat)
        or _is_reparse_component(paper_dir, paper_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
        or not stat.S_ISDIR(paper_stat.st_mode)
    ):
        _issue(
            issues,
            "paper_path_unsafe",
            paper_id,
            "papers root and paper directory must be non-reparse directories",
        )
        return result

    snapshots: dict[Path, _StableFileSnapshot] = {}

    fixed_paths = {
        "paper_materials.md": _resolve_fixed_paper_path(paper_dir, "paper_materials.md"),
        "draft_v2.tex": _resolve_fixed_paper_path(paper_dir, "draft_v2.tex"),
        "results/verification_manifest.json": _resolve_fixed_paper_path(
            paper_dir,
            "results/verification_manifest.json",
        ),
        MANUAL_COMPLETION_RELATIVE_PATH: _resolve_fixed_paper_path(
            paper_dir,
            MANUAL_COMPLETION_RELATIVE_PATH,
        ),
    }
    for relative_path, resolved in fixed_paths.items():
        if resolved is None:
            _issue(issues, "required_path_escape", relative_path, "required evidence path escapes the paper directory")
    materials_path = fixed_paths["paper_materials.md"]
    draft_path = fixed_paths["draft_v2.tex"]
    manifest_path = fixed_paths["results/verification_manifest.json"]
    manual_completion_path = fixed_paths[MANUAL_COMPLETION_RELATIVE_PATH]

    materials_text = ""
    materials_hash = ""
    if materials_path is not None:
        materials_text, materials_snapshot, materials_error_kind, materials_error = _snapshot_text(
            root, materials_path, snapshots
        )
        if materials_snapshot is not None:
            materials_hash = materials_snapshot.sha256
        if materials_error_kind == "missing":
            _issue(issues, "paper_materials_missing", "paper_materials.md", "paper_materials.md is required")
        elif materials_error_kind:
            error_code = (
                "paper_materials_unsafe_path"
                if materials_error_kind == "unsafe"
                else "paper_materials_snapshot_unstable"
                if materials_error_kind == "unstable"
                else "paper_materials_unreadable"
            )
            _issue(issues, error_code, "paper_materials.md", materials_error)
        if materials_snapshot is not None and not materials_error_kind:
            status_values = [
                match.group(1)
                for line in materials_text.splitlines()
                if (match := _STATUS_LINE_RE.fullmatch(line.strip()))
            ]
            if "READY" not in status_values:
                _issue(
                    issues,
                    "paper_materials_not_ready",
                    "paper_materials.md",
                    "paper_materials.md must contain a standalone Status: READY line",
                )
            elif any(value != "READY" for value in status_values):
                _issue(
                    issues,
                    "paper_materials_status_conflict",
                    "paper_materials.md",
                    "paper_materials.md contains a conflicting standalone Status line",
                )
    result["paper_materials_sha256"] = materials_hash

    draft_text = ""
    draft_hash = ""
    if draft_path is not None:
        draft_text, draft_snapshot, draft_error_kind, draft_error = _snapshot_text(root, draft_path, snapshots)
        if draft_snapshot is not None:
            draft_hash = draft_snapshot.sha256
        if draft_error_kind == "missing":
            _issue(issues, "draft_v2_missing", "draft_v2.tex", "draft_v2.tex is required")
        elif draft_error_kind:
            error_code = (
                "draft_v2_unsafe_path"
                if draft_error_kind == "unsafe"
                else "draft_v2_snapshot_unstable"
                if draft_error_kind == "unstable"
                else "draft_v2_unreadable"
            )
            _issue(issues, error_code, "draft_v2.tex", draft_error)
        if draft_snapshot is not None and not draft_error_kind:
            for label, marker in FORBIDDEN_DRAFT_MARKERS:
                if marker.search(draft_text):
                    _issue(
                        issues,
                        "draft_v2_marker_remaining",
                        "draft_v2.tex",
                        f"draft_v2.tex still contains forbidden marker: {label}",
                    )
    result["draft_v2_sha256"] = draft_hash

    # Both non-import routes bind the exact draft bytes.  The legacy manual
    # record represents an explicit user click.  The policy record represents
    # a password-protected workspace decision to treat every completed Step 5
    # draft as authoritative.  In both routes Step 6 owns marker cleanup and
    # must preserve the accepted numeric data.
    if manual_completion_path is not None and os.path.lexists(manual_completion_path):
        manual_text, manual_snapshot, manual_error_kind, manual_error = _snapshot_text(
            root,
            manual_completion_path,
            snapshots,
        )
        if manual_error_kind != "missing":
            # paper_materials.md and pre-Step-6 marker removal belong to the
            # importer route.  In either accepted-draft route Step 6 owns the
            # already-tuned cleanup of 【验】 and related placeholders.
            issues[:] = [
                issue
                for issue in issues
                if not str(issue.get("code") or "").startswith("paper_materials_")
                and str(issue.get("code") or "") != "draft_v2_marker_remaining"
            ]
            if materials_path is not None:
                snapshots.pop(_absolute_lexical(materials_path), None)
            result["paper_materials_sha256"] = ""

            completion: dict[str, Any] | None = None
            if manual_error_kind:
                code = (
                    "manual_completion_unsafe_path"
                    if manual_error_kind == "unsafe"
                    else "manual_completion_snapshot_unstable"
                    if manual_error_kind == "unstable"
                    else "manual_completion_unreadable"
                )
                _issue(issues, code, MANUAL_COMPLETION_RELATIVE_PATH, manual_error)
            elif manual_snapshot is not None:
                try:
                    loaded_manual = json.loads(manual_text)
                except json.JSONDecodeError as exc:
                    _issue(
                        issues,
                        "manual_completion_unreadable",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        f"manual completion record is not valid JSON: {type(exc).__name__}",
                    )
                else:
                    if isinstance(loaded_manual, dict):
                        completion = loaded_manual
                        result["manual_completion_sha256"] = manual_snapshot.sha256
                    else:
                        _issue(
                            issues,
                            "manual_completion_invalid_root",
                            MANUAL_COMPLETION_RELATIVE_PATH,
                            "manual completion record root must be an object",
                        )

            if completion is not None:
                completion_schema = completion.get("schema")
                is_manual = completion_schema == MANUAL_COMPLETION_SCHEMA
                is_policy = completion_schema == AUTO_TRUST_COMPLETION_SCHEMA
                result["manual_completion"] = is_manual
                result["policy_completion"] = is_policy
                result["transition_mode"] = "manual_edit" if is_manual else "workspace_auto_trust" if is_policy else ""
                result["manual_completion_schema"] = completion_schema
                result["completion_schema"] = completion_schema
                if not is_manual and not is_policy:
                    _issue(
                        issues,
                        "manual_completion_schema",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        "manual completion schema is unsupported",
                    )
                if completion.get("paper_id") != paper_id:
                    _issue(
                        issues,
                        "manual_completion_paper_id",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        "manual completion paper_id does not match the selected paper",
                    )
                expected_mode = "manual_edit" if is_manual else "workspace_auto_trust"
                if completion.get("status") != "READY" or completion.get("mode") != expected_mode:
                    _issue(
                        issues,
                        "manual_completion_status",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        f"completion must declare READY {expected_mode} status",
                    )
                expected_confirmation = (
                    "DATA_MANUALLY_APPLIED" if is_manual else "DATA_ACCEPTED_BY_WORKSPACE_POLICY"
                )
                if completion.get("confirmation") != expected_confirmation:
                    _issue(
                        issues,
                        "manual_completion_confirmation",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        "manual completion confirmation is missing",
                    )
                if completion.get("marker_cleanup") != "step_6":
                    _issue(
                        issues,
                        "manual_completion_cleanup_stage",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        "manual completion must delegate verification-marker cleanup to Step 6",
                    )
                recorded_draft_hash = str(completion.get("draft_v2_sha256") or "").strip()
                if not _SHA256_RE.fullmatch(recorded_draft_hash):
                    _issue(
                        issues,
                        "manual_completion_draft_hash_invalid",
                        MANUAL_COMPLETION_RELATIVE_PATH,
                        "draft_v2_sha256 must be a 64-character hexadecimal SHA256",
                    )
                elif draft_hash and recorded_draft_hash.casefold() != draft_hash:
                    _issue(
                        issues,
                        "manual_completion_draft_hash_mismatch",
                        "draft_v2.tex",
                        "the manually confirmed draft no longer matches draft_v2.tex bytes",
                    )

                if is_policy:
                    policy = completion.get("policy")
                    if not isinstance(policy, dict):
                        _issue(
                            issues,
                            "policy_completion_snapshot_missing",
                            MANUAL_COMPLETION_RELATIVE_PATH,
                            "workspace auto-trust completion must contain its policy snapshot",
                        )
                    else:
                        result["policy_snapshot"] = dict(policy)
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
                            _issue(
                                issues,
                                "policy_completion_snapshot_invalid",
                                MANUAL_COMPLETION_RELATIVE_PATH,
                                "workspace auto-trust policy snapshot is invalid",
                            )

            changed_snapshots = _verify_stable_snapshots(root, snapshots)
            for changed_path, reason in changed_snapshots:
                _issue(
                    issues,
                    "evidence_snapshot_changed",
                    changed_path.relative_to(paper_dir).as_posix(),
                    reason,
                )
            if not issues:
                result["status"] = "READY"
                result["ready"] = True
                result["transition_ready"] = True
                gate_profile = (
                    "evidence_auto_trust_transition"
                    if result.get("policy_completion")
                    else "evidence_manual_transition"
                )
                result["gate_profile"] = gate_profile
                result["input_snapshot"] = _input_snapshot(
                    root,
                    snapshots,
                    gate_profile=gate_profile,
                    paper_id=paper_id,
                )
            return result

    manifest: dict[str, Any] | None = None
    if manifest_path is not None:
        manifest_text, manifest_snapshot, manifest_error_kind, manifest_error = _snapshot_text(
            root, manifest_path, snapshots
        )
        if manifest_error_kind == "missing":
            _issue(
                issues,
                "verification_manifest_missing",
                "results/verification_manifest.json",
                "verification manifest is required",
            )
        elif manifest_error_kind:
            error_code = (
                "verification_manifest_unsafe_path"
                if manifest_error_kind == "unsafe"
                else "verification_manifest_snapshot_unstable"
                if manifest_error_kind == "unstable"
                else "verification_manifest_unreadable"
            )
            _issue(issues, error_code, "results/verification_manifest.json", manifest_error)
        elif manifest_snapshot is not None:
            try:
                loaded = json.loads(manifest_text)
            except json.JSONDecodeError as exc:
                _issue(
                    issues,
                    "verification_manifest_unreadable",
                    "results/verification_manifest.json",
                    f"verification manifest is not valid readable JSON: {type(exc).__name__}",
                )
            else:
                if isinstance(loaded, dict):
                    manifest = loaded
                else:
                    _issue(
                        issues,
                        "verification_manifest_invalid_root",
                        "results/verification_manifest.json",
                        "verification manifest root must be an object",
                    )

    schema_version: int | None = None
    if manifest is not None:
        raw_schema_version = manifest.get("schema_version")
        if type(raw_schema_version) is int:
            schema_version = raw_schema_version
            result["schema_version"] = schema_version
        if schema_version not in {LEGACY_MANIFEST_SCHEMA_VERSION, MANIFEST_SCHEMA_VERSION}:
            _issue(
                issues,
                "verification_manifest_schema",
                "results/verification_manifest.json",
                "schema_version must equal integer 1 (legacy read-only) or 2 (transition contract)",
            )
        if manifest.get("paper_id") != paper_id:
            _issue(
                issues,
                "verification_manifest_paper_id",
                "results/verification_manifest.json",
                "manifest paper_id does not match the selected paper",
            )
        if manifest.get("status") != "READY":
            _issue(
                issues,
                "verification_manifest_status",
                "results/verification_manifest.json",
                "manifest status must equal READY",
            )

        recorded_draft_hash = str(manifest.get("draft_v2_sha256") or "").strip()
        if not _SHA256_RE.fullmatch(recorded_draft_hash):
            _issue(
                issues,
                "draft_v2_hash_invalid",
                "results/verification_manifest.json",
                "draft_v2_sha256 must be a 64-character hexadecimal SHA256",
            )
        elif draft_hash and recorded_draft_hash.casefold() != draft_hash:
            _issue(
                issues,
                "draft_v2_hash_mismatch",
                "draft_v2.tex",
                "draft_v2_sha256 does not match draft_v2.tex bytes",
            )

        require_v2 = schema_version == MANIFEST_SCHEMA_VERSION
        artifact_by_id, artifact_count = _validate_artifacts(
            root=root,
            paper_dir=paper_dir,
            manifest_path=manifest_path,
            artifacts=manifest.get("artifacts"),
            require_v2=require_v2,
            issues=issues,
            snapshots=snapshots,
        )
        result["artifact_count"] = artifact_count

        if require_v2:
            recorded_materials_hash = str(manifest.get("paper_materials_sha256") or "").strip()
            if not _SHA256_RE.fullmatch(recorded_materials_hash):
                _issue(
                    issues,
                    "paper_materials_hash_invalid",
                    "results/verification_manifest.json",
                    "schema v2 paper_materials_sha256 must be a 64-character hexadecimal SHA256",
                )
            elif materials_hash and recorded_materials_hash.casefold() != materials_hash:
                _issue(
                    issues,
                    "paper_materials_hash_mismatch",
                    "paper_materials.md",
                    "paper_materials_sha256 does not match paper_materials.md bytes",
                )
            _validate_evidence_contract(manifest, artifact_by_id, issues)
            result["claim_count"] = _validate_claim_evidence(
                manifest=manifest,
                draft_text=draft_text,
                draft_hash=draft_hash,
                artifact_by_id=artifact_by_id,
                issues=issues,
            )
        elif schema_version == LEGACY_MANIFEST_SCHEMA_VERSION:
            result["legacy_read_only"] = True
            legacy_integrity_issues = list(issues)
            if not legacy_integrity_issues:
                result["status"] = "LEGACY_V1"
                result["legacy_integrity_ready"] = True
            _issue(
                issues,
                "verification_manifest_upgrade_required",
                "results/verification_manifest.json",
                "schema v1 remains readable but cannot authorize Step 5->6; create a schema v2 evidence contract",
            )

    changed_snapshots = _verify_stable_snapshots(root, snapshots)
    for changed_path, reason in changed_snapshots:
        _issue(
            issues,
            "evidence_snapshot_changed",
            changed_path.relative_to(paper_dir).as_posix(),
            reason,
        )
    if changed_snapshots:
        result["status"] = "BLOCKED"
        result["legacy_integrity_ready"] = False

    if not issues:
        result["status"] = "READY"
        result["ready"] = True
        result["transition_ready"] = True
        result["input_snapshot"] = _input_snapshot(
            root,
            snapshots,
            gate_profile="evidence_v2_transition",
            paper_id=paper_id,
        )
    return result


def writing_evidence_blocker(result: dict[str, Any], *, limit: int = 5) -> str:
    issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
    if not issues:
        return ""
    parts = []
    for issue in issues[: max(1, limit)]:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "evidence_gate_blocked")
        path = str(issue.get("path") or "")
        parts.append(f"{code}({path})" if path else code)
    suffix = f"; +{len(issues) - len(parts)} more" if len(issues) > len(parts) else ""
    return "Step 5->6 evidence gate BLOCKED: " + "; ".join(parts) + suffix
