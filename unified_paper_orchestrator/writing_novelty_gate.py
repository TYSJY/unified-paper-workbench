from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MANIFEST_SCHEMA_VERSION = 1
MIN_VERIFIED_REFERENCES = 50
MIN_CLOSE_FULL_TEXT_SOURCES = 7
MIN_TRADITIONAL_FULL_TEXT_SOURCES = 5
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STATUS_LINE_RE = re.compile(r"^Status\s*:\s*(.*?)\s*$", re.IGNORECASE)
_DECISION_LINE_RE = re.compile(r"^Decision\s*:\s*(.*?)\s*$", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(r"https://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", re.IGNORECASE)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
_HTTPS_URL_RE = re.compile(r"https://[^\s<>{}\[\]|\"']+", re.IGNORECASE)
_BIB_ENTRY_RE = re.compile(r"(?m)^@\w+\s*\{")
_BIB_KEY_RE = re.compile(r"(?m)^@\w+\s*\{\s*([^,\s]+)\s*,")
_VERIFICATION_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z])(?:UNVERIFIED|VERIFIED)(?![A-Za-z])")
_NOVELTY_SECTION_RE = re.compile(r"(?m)^ {0,3}(?:#{1,6}\s*)?([1-5])[.)]\s+")
_NOVELTY_DECISION_RE = re.compile(r"(?im)^(?:Decision|Recommendation|Action|决定|建议|结论)\s*[:：]\s*(.+?)\s*$")
_NOVELTY_DECISION_BOUNDARY_RE = re.compile(r"[.!?。！？;；]")
_NUMBERED_REFERENCE_RE = re.compile(r"^ {0,3}(?:#{1,6}\s*)?(\d+)[.)]\s+")
_REFERENCE_TABLE_ID_RE = re.compile(
    r"^(?:(?P<numeric>[0-9]{1,9})|R(?P<padded>0[1-9]|[1-9][0-9]{1,8}))$"
)
_PLACEHOLDER_AUTHOR_RE = re.compile(r"(?i)^\s*(?:arxiv\s+authors?|unknown|anonymous|tbd|n/?a|authors?)\s*$")
_BIB_RECORD_START_RE = re.compile(r"(?im)^\s*@([A-Za-z]+)\s*([\{(])")
_NON_RECORD_BIB_TYPES = {"comment", "preamble", "string"}
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_INPUT_SNAPSHOT_SCHEMA = "unified-paper-orchestrator/writing-gate-input-snapshot/v1"
_STEP2_CONTRACT_COUNT_KEYS = (
    "reference_count",
    "verified_reference_count",
    "bib_entry_count",
    "identity_match_count",
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
        return None, "path escapes the configured writing root"

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
        return None, "resolved path escapes the configured writing root"
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
        "scope": "writing_root",
        "files": files,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        **payload,
        "snapshot_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _safe_name(value: str) -> bool:
    native = Path(value)
    windows = PureWindowsPath(value)
    return bool(
        value
        and value not in {".", ".."}
        and "\x00" not in value
        and native.name == value
        and not native.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and "/" not in value
        and "\\" not in value
    )


def _snapshot_text(
    root: Path,
    path: Path,
    cache: dict[Path, _StableFileSnapshot],
    issues: list[dict[str, str]],
    code: str,
    label: str,
) -> tuple[str, _StableFileSnapshot | None]:
    snapshot, error_kind, error = _read_stable_snapshot(root, path, cache)
    if snapshot is None:
        issue_code = code if error_kind == "missing" else f"{code}_{error_kind}"
        issues.append({"code": issue_code, "path": label, "message": f"{label}: {error}"})
        return "", None
    try:
        return snapshot.data.decode("utf-8-sig"), snapshot
    except UnicodeError as exc:
        issues.append(
            {
                "code": f"{code}_unreadable",
                "path": label,
                "message": f"cannot read {label}: {type(exc).__name__}",
            }
        )
        return "", snapshot


def _validate_review_sources(
    manifest: dict[str, Any],
    field: str,
    minimum: int,
    issues: list[dict[str, str]],
) -> int:
    raw = manifest.get(field)
    if not isinstance(raw, list):
        issues.append(
            {
                "code": f"{field}_invalid",
                "path": "novelty_verification_manifest.json",
                "message": f"{field} must be a list",
            }
        )
        return 0
    seen: set[str] = set()
    valid = 0
    for index, item in enumerate(raw):
        ref = f"novelty_verification_manifest.json#{field}[{index}]"
        if not isinstance(item, dict):
            issues.append({"code": f"{field}_entry_invalid", "path": ref, "message": "entry must be an object"})
            continue
        source_id = str(item.get("source_id") or "").strip()
        url = str(item.get("official_url") or "").strip()
        finding = str(item.get("finding") or item.get("distinction") or "").strip()
        if not source_id or source_id in seen:
            issues.append(
                {
                    "code": f"{field}_source_id_invalid",
                    "path": ref,
                    "message": "source_id must be non-empty and unique",
                }
            )
            continue
        seen.add(source_id)
        if item.get("full_text_checked") is not True:
            issues.append(
                {
                    "code": f"{field}_not_full_text",
                    "path": ref,
                    "message": "full_text_checked must be true",
                }
            )
            continue
        if not url.startswith("https://") or not finding:
            issues.append(
                {
                    "code": f"{field}_evidence_incomplete",
                    "path": ref,
                    "message": "official_url and a non-empty finding are required",
                }
            )
            continue
        valid += 1
    if valid < minimum:
        issues.append(
            {
                "code": f"{field}_insufficient",
                "path": "novelty_verification_manifest.json",
                "message": f"at least {minimum} valid full-text sources are required; found {valid}",
            }
        )
    return valid


def _normalized_source_keys(text: str) -> set[str]:
    """Extract stable citation identities across Markdown and BibTeX.

    Prompt 02 accepts an arXiv id, DOI, or official URL.  Treating only arXiv
    links as verified silently rejects valid project and publisher sources, so
    the workflow gate normalizes all three identity forms.
    """

    keys = {f"arxiv:{match.casefold()}" for match in _ARXIV_ID_RE.findall(text)}
    keys.update(f"doi:{match.rstrip('.,;').casefold()}" for match in _DOI_RE.findall(text))
    for raw_url in _HTTPS_URL_RE.findall(text):
        raw_url = raw_url.rstrip(".,;:)")
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            continue
        host = parsed.netloc.casefold()
        path = parsed.path.rstrip("/")
        if not host:
            continue
        if host in {"arxiv.org", "www.arxiv.org"}:
            continue
        if host in {"doi.org", "dx.doi.org"}:
            continue
        normalized = urlunsplit(("https", host, path, "", ""))
        keys.add(f"url:{normalized.casefold()}")
    return keys


def _strip_bibtex_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        escaped = False
        kept: list[str] = []
        for character in line:
            if character == "%" and not escaped:
                break
            kept.append(character)
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        lines.append("".join(kept))
    return "\n".join(lines)


def _parse_bib_records(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse balanced BibTeX records while excluding directives and comments."""

    cleaned = _strip_bibtex_comments(text)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    cursor = 0
    while match := _BIB_RECORD_START_RE.search(cleaned, cursor):
        entry_type = match.group(1).casefold()
        opening = match.group(2)
        closing = "}" if opening == "{" else ")"
        depth = 1
        escaped = False
        quoted = False
        index = match.end()
        while index < len(cleaned) and depth:
            character = cleaned[index]
            if character == '"' and not escaped:
                quoted = not quoted
            if not quoted and not escaped:
                if character == opening:
                    depth += 1
                elif character == closing:
                    depth -= 1
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
            index += 1
        if depth:
            errors.append(f"unterminated @{entry_type} record at offset {match.start()}")
            break
        block = cleaned[match.start() : index]
        cursor = index
        if entry_type in _NON_RECORD_BIB_TYPES:
            continue
        body = cleaned[match.end() : index - 1]
        raw_key, separator, _remainder = body.partition(",")
        key = raw_key.strip()
        if not separator or not key:
            errors.append(f"@{entry_type} record at offset {match.start()} has no citation key")
            continue
        records.append(
            {
                "type": entry_type,
                "key": key,
                "identities": _normalized_source_keys(block),
                "text": block,
            }
        )
    return records, errors


def _verification_state(text: str) -> str:
    tokens = [token.casefold() for token in _VERIFICATION_TOKEN_RE.findall(text)]
    if "unverified" in tokens:
        return "unverified"
    if "verified" in tokens:
        return "verified"
    return "missing"


def _reference_table_number(value: str) -> int | None:
    """Return a bounded row number for legacy numeric or canonical ``R01`` IDs."""

    match = _REFERENCE_TABLE_ID_RE.fullmatch(value.strip())
    if match is None:
        return None
    raw_number = match.group("numeric") or match.group("padded")
    return int(raw_number)


def _parse_markdown_reference_records(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    table_records: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        number = _reference_table_number(cells[0]) if cells else None
        if number is None:
            continue
        author = cells[2] if len(cells) >= 3 else ""
        table_records.append(
            {
                "number": number,
                "identities": _normalized_source_keys(line),
                "verification": _verification_state(cells[-1] if cells else line),
                "author": author,
                "author_placeholder": not author or bool(_PLACEHOLDER_AUTHOR_RE.fullmatch(author)),
                "field_count": len(cells),
            }
        )
    if table_records:
        return table_records

    starts: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if match := _NUMBERED_REFERENCE_RE.match(line):
            starts.append((index, int(match.group(1))))
    records: list[dict[str, Any]] = []
    for position, (start, number) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        author_match = re.search(r"(?im)^\s*(?:[-*]\s*)?authors?\s*[:\uFF1A]\s*(.+?)\s*$", block)
        author = author_match.group(1).strip() if author_match else ""
        records.append(
            {
                "number": number,
                "identities": _normalized_source_keys(block),
                "verification": _verification_state(block),
                "author": author,
                "author_placeholder": not author or bool(_PLACEHOLDER_AUTHOR_RE.fullmatch(author)),
                "field_count": 0,
            }
        )
    return records


def _match_verified_records(
    markdown_records: list[dict[str, Any]],
    bib_records: list[dict[str, Any]],
) -> tuple[int, int]:
    adjacency: list[list[int]] = []
    for record in markdown_records:
        identities = set(record.get("identities") or set())
        adjacency.append(
            [index for index, bib in enumerate(bib_records) if identities & set(bib.get("identities") or set())]
        )

    owner_by_bib: dict[int, int] = {}

    def augment(record_index: int, visited: set[int]) -> bool:
        for bib_index in adjacency[record_index]:
            if bib_index in visited:
                continue
            visited.add(bib_index)
            owner = owner_by_bib.get(bib_index)
            if owner is None or augment(owner, visited):
                owner_by_bib[bib_index] = record_index
                return True
        return False

    order = sorted(
        range(len(markdown_records)),
        key=lambda index: markdown_records[index].get("verification") != "verified",
    )
    for record_index in order:
        augment(record_index, set())
    matched_records = set(owner_by_bib.values())
    verified = sum(1 for index in matched_records if markdown_records[index].get("verification") == "verified")
    return len(matched_records), verified


def _numbered_answer(text: str, number: int) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _NOVELTY_SECTION_RE.match(line)
        if not match or int(match.group(1)) != number:
            continue
        collected: list[str] = []
        inline_answer = line[match.end() :].strip()
        if inline_answer and "?" not in inline_answer:
            collected.append(inline_answer)
        for following in lines[index + 1 :]:
            if _NOVELTY_SECTION_RE.match(following):
                break
            collected.append(following)
        return "\n".join(collected).strip()
    return ""


def validate_writing_step2_contract(writing_root: Path | str, paper_id: str) -> dict[str, Any]:
    """Validate exactly the artifacts and semantics required by tuned Prompt 02.

    This transition gate deliberately does not require auxiliary full-text
    review files or controller-only READY markers that Prompt 02 never asks the
    model to create.  ``validate_writing_novelty`` remains the optional stricter
    full-text audit.
    """

    paper_id = str(paper_id or "").strip()
    result: dict[str, Any] = {
        "paper_id": paper_id,
        "gate_profile": "prompt_02_contract",
        "status": "BLOCKED",
        "ready": False,
        "reference_count": 0,
        "verified_reference_count": 0,
        "bib_entry_count": 0,
        "identity_match_count": 0,
        "issues": [],
    }
    issues: list[dict[str, str]] = result["issues"]
    if not _safe_name(paper_id):
        issues.append(
            {"code": "invalid_paper_id", "path": paper_id, "message": "paper_id must be one safe directory name"}
        )
        return result

    root = _absolute_lexical(writing_root)
    root_components, root_error = _inspect_safe_path(root, root)
    if root_components is None:
        issues.append({"code": "novelty_root_unsafe", "path": str(root), "message": root_error})
        return result
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        issues.append(
            {
                "code": "novelty_root_unreadable",
                "path": str(root),
                "message": f"cannot inspect writing root: {type(exc).__name__}",
            }
        )
        return result
    if _is_reparse_component(root, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        issues.append(
            {
                "code": "novelty_root_unsafe",
                "path": str(root),
                "message": "writing root must be a regular non-reparse directory",
            }
        )
        return result

    refs_dir = root / "refs" / paper_id
    required_paths = {
        "idea": root / "ideas" / f"{paper_id}.md",
        "references_md": refs_dir / "references.md",
        "references_bib": refs_dir / "references.bib",
        "literature_map": refs_dir / "literature_map.md",
        "novelty_check": refs_dir / "novelty_check.md",
    }
    snapshots: dict[Path, _StableFileSnapshot] = {}
    texts: dict[str, str] = {}
    for key, path in required_paths.items():
        label = path.relative_to(root).as_posix()
        text, _snapshot = _snapshot_text(root, path, snapshots, issues, f"{key}_missing", label)
        texts[key] = text

    markdown_records = _parse_markdown_reference_records(texts["references_md"])
    bib_records, bib_errors = _parse_bib_records(texts["references_bib"])
    bib_entries = len(bib_records)
    bib_keys = [str(record.get("key") or "").casefold() for record in bib_records]
    matched_records, verified_records = _match_verified_records(markdown_records, bib_records)
    result["reference_count"] = len(markdown_records)
    result["verified_reference_count"] = verified_records
    result["bib_entry_count"] = bib_entries
    result["identity_match_count"] = matched_records

    if len(markdown_records) < MIN_VERIFIED_REFERENCES:
        issues.append(
            {
                "code": "reference_count_insufficient",
                "path": f"refs/{paper_id}/references.md",
                "message": (
                    f"Prompt 02 requires at least {MIN_VERIFIED_REFERENCES} structured reference records; "
                    f"found {len(markdown_records)}"
                ),
            }
        )
    if bib_entries < MIN_VERIFIED_REFERENCES:
        issues.append(
            {
                "code": "reference_bib_count_insufficient",
                "path": f"refs/{paper_id}/references.bib",
                "message": f"Prompt 02 requires at least {MIN_VERIFIED_REFERENCES} BibTeX entries; found {bib_entries}",
            }
        )
    for error in bib_errors:
        issues.append(
            {
                "code": "reference_bib_parse_error",
                "path": f"refs/{paper_id}/references.bib",
                "message": error,
            }
        )
    if len(set(bib_keys)) != len(bib_keys):
        issues.append(
            {
                "code": "reference_bib_key_duplicate",
                "path": f"refs/{paper_id}/references.bib",
                "message": "BibTeX entry keys must be unique",
            }
        )
    if any(not record.get("identities") for record in bib_records):
        issues.append(
            {
                "code": "reference_bib_identity_missing",
                "path": f"refs/{paper_id}/references.bib",
                "message": "every BibTeX record requires an arXiv id, DOI, or official URL",
            }
        )
    if matched_records < MIN_VERIFIED_REFERENCES:
        issues.append(
            {
                "code": "reference_identity_alignment_insufficient",
                "path": f"refs/{paper_id}",
                "message": (
                    f"at least {MIN_VERIFIED_REFERENCES} distinct Markdown records must bind one-to-one to BibTeX "
                    f"records by arXiv id, DOI, or official URL; found {matched_records}"
                ),
            }
        )
    if verified_records < MIN_VERIFIED_REFERENCES:
        issues.append(
            {
                "code": "verified_references_insufficient",
                "path": f"refs/{paper_id}/references.md",
                "message": (
                    f"Step 3 requires at least {MIN_VERIFIED_REFERENCES} individually verified, BibTeX-bound "
                    f"references; found {verified_records}"
                ),
            }
        )
    placeholder_rows = [
        str(record.get("number") or "?") for record in markdown_records if record.get("author_placeholder")
    ]
    if placeholder_rows:
        issues.append(
            {
                "code": "reference_author_missing_or_placeholder",
                "path": f"refs/{paper_id}/references.md",
                "message": "real author metadata is required for reference rows: " + ", ".join(placeholder_rows[:10]),
            }
        )

    sections = set(_NOVELTY_SECTION_RE.findall(texts["novelty_check"]))
    if sections != {"1", "2", "3", "4", "5"}:
        issues.append(
            {
                "code": "novelty_questions_incomplete",
                "path": f"refs/{paper_id}/novelty_check.md",
                "message": "novelty_check.md must answer all five numbered Prompt 02 questions",
            }
        )
    decisions = [match.strip(" *`_") for match in _NOVELTY_DECISION_RE.findall(texts["novelty_check"])]
    if not decisions:
        fifth_answer = _numbered_answer(texts["novelty_check"], 5)
        if fifth_answer:
            decisions.append(fifth_answer)
    proceed_tokens = (
        "keep",
        "modify",
        "revise",
        "narrow",
        "retain",
        "proceed",
        "\u4fdd\u7559",
        "\u4fee\u6539",
        "\u7ee7\u7eed",
        "\u6536\u7a84",
    )
    abandon_tokens = ("abandon", "drop", "reject", "\u653e\u5f03", "\u5e9f\u5f03")
    # Judge the explicit directive at the start of each Decision/Recommendation
    # line.  Later sentences may describe a conditional fallback (for example,
    # "Modify and proceed. If the measurement fails, abandon the mechanism"),
    # which must not overturn an otherwise unambiguous current decision.
    normalized_directives = [
        _NOVELTY_DECISION_BOUNDARY_RE.split(decision, maxsplit=1)[0].casefold()
        for decision in decisions
    ]
    has_proceed = any(any(token in directive for token in proceed_tokens) for directive in normalized_directives)
    has_abandon = any(any(token in directive for token in abandon_tokens) for directive in normalized_directives)
    if not decisions:
        issues.append(
            {
                "code": "novelty_decision_missing",
                "path": f"refs/{paper_id}/novelty_check.md",
                "message": "the answer to Prompt 02 question 5 must choose keep, modify, or abandon",
            }
        )
    elif has_abandon and not has_proceed:
        issues.append(
            {
                "code": "novelty_decision_abandon",
                "path": f"refs/{paper_id}/novelty_check.md",
                "message": "Prompt 02 recommends abandoning the idea; drafting must not continue",
            }
        )
    elif has_abandon or not has_proceed:
        issues.append(
            {
                "code": "novelty_decision_ambiguous",
                "path": f"refs/{paper_id}/novelty_check.md",
                "message": "the keep/modify/abandon decision is ambiguous",
            }
        )

    for changed_path, reason in _verify_stable_snapshots(root, snapshots):
        issues.append(
            {
                "code": "novelty_snapshot_changed",
                "path": changed_path.relative_to(root).as_posix(),
                "message": reason,
            }
        )
    if not issues:
        result["status"] = "READY"
        result["ready"] = True
        result["input_snapshot"] = _input_snapshot(
            root,
            snapshots,
            gate_profile="prompt_02_contract",
            paper_id=paper_id,
        )
    return result


def writing_step2_contract_validation(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize the Prompt 02 gate into the durable semantic-validation shape.

    The gate exposes its four bounded counters at the top level for historical
    callers, while writing retries consume the same ``counts``/``issues``
    schema as the other step validators.  Keeping this translation here avoids
    dispatcher/runtime drift over which evidence is safe to journal and send
    back in a controller-owned retry envelope.
    """

    return {
        "status": str(result.get("status") or "BLOCKED"),
        "ready": bool(result.get("ready")),
        "counts": {
            key: value
            for key in _STEP2_CONTRACT_COUNT_KEYS
            if isinstance((value := result.get(key)), int)
            and not isinstance(value, bool)
            and value >= 0
        },
        "issues": copy.deepcopy(result.get("issues") or []),
        "gate_profile": str(result.get("gate_profile") or "prompt_02_contract"),
    }


def validate_writing_novelty(writing_root: Path | str, paper_id: str) -> dict[str, Any]:
    """Read-only Step 2 -> Step 3 novelty and citation gate."""

    paper_id = str(paper_id or "").strip()
    result: dict[str, Any] = {
        "paper_id": paper_id,
        "status": "BLOCKED",
        "ready": False,
        "verified_reference_count": 0,
        "close_full_text_count": 0,
        "traditional_full_text_count": 0,
        "issues": [],
    }
    issues: list[dict[str, str]] = result["issues"]
    if not _safe_name(paper_id):
        issues.append(
            {"code": "invalid_paper_id", "path": paper_id, "message": "paper_id must be one safe directory name"}
        )
        return result

    root = _absolute_lexical(writing_root)
    root_components, root_error = _inspect_safe_path(root, root)
    if root_components is None:
        issues.append(
            {
                "code": "novelty_root_unsafe",
                "path": str(root),
                "message": root_error,
            }
        )
        return result
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        issues.append(
            {
                "code": "novelty_root_unreadable",
                "path": str(root),
                "message": f"cannot inspect writing root: {type(exc).__name__}",
            }
        )
        return result
    if _is_reparse_component(root, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        issues.append(
            {
                "code": "novelty_root_unsafe",
                "path": str(root),
                "message": "writing root must be a regular non-reparse directory",
            }
        )
        return result

    idea_path = root / "ideas" / f"{paper_id}.md"
    refs_dir = root / "refs" / paper_id
    required_paths = {
        "idea": idea_path,
        "references_md": refs_dir / "references.md",
        "references_bib": refs_dir / "references.bib",
        "literature_map": refs_dir / "literature_map.md",
        "novelty_check": refs_dir / "novelty_check.md",
        "novelty_fulltext_review": refs_dir / "novelty_fulltext_review.md",
        "formal_claim_spec": refs_dir / "formal_claim_spec.md",
    }
    snapshots: dict[Path, _StableFileSnapshot] = {}
    texts: dict[str, str] = {}
    required_snapshots: dict[str, _StableFileSnapshot] = {}
    for key, path in required_paths.items():
        label = path.relative_to(root).as_posix()
        text, snapshot = _snapshot_text(
            root,
            path,
            snapshots,
            issues,
            f"{key}_missing",
            label,
        )
        texts[key] = text
        if snapshot is not None:
            required_snapshots[key] = snapshot
    md_ids = set(_ARXIV_URL_RE.findall(texts["references_md"]))
    bib_ids = set(_ARXIV_URL_RE.findall(texts["references_bib"]))
    bib_entries = len(_BIB_ENTRY_RE.findall(texts["references_bib"]))
    result["verified_reference_count"] = len(md_ids & bib_ids)
    if md_ids != bib_ids:
        issues.append(
            {
                "code": "reference_id_set_mismatch",
                "path": f"refs/{paper_id}",
                "message": "references.md and references.bib arXiv ID sets differ",
            }
        )
    if len(md_ids) < MIN_VERIFIED_REFERENCES or bib_entries < MIN_VERIFIED_REFERENCES:
        issues.append(
            {
                "code": "verified_references_insufficient",
                "path": f"refs/{paper_id}",
                "message": f"at least {MIN_VERIFIED_REFERENCES} matching arXiv records and BibTeX entries are required",
            }
        )

    novelty_lines = [line.strip() for line in texts["novelty_check"].splitlines()]
    statuses = [match.group(1).upper() for line in novelty_lines if (match := _STATUS_LINE_RE.fullmatch(line))]
    decisions = [match.group(1).upper() for line in novelty_lines if (match := _DECISION_LINE_RE.fullmatch(line))]
    if statuses != ["READY"]:
        issues.append(
            {
                "code": "novelty_status_not_ready",
                "path": f"refs/{paper_id}/novelty_check.md",
                "message": "novelty_check.md must contain exactly one standalone Status: READY line",
            }
        )
    if decisions != ["PROCEED"]:
        issues.append(
            {
                "code": "novelty_decision_not_proceed",
                "path": f"refs/{paper_id}/novelty_check.md",
                "message": "novelty_check.md must contain exactly one standalone Decision: PROCEED line",
            }
        )

    manifest_path = refs_dir / "novelty_verification_manifest.json"
    manifest: dict[str, Any] | None = None
    manifest_snapshot, manifest_error_kind, manifest_error = _read_stable_snapshot(root, manifest_path, snapshots)
    if manifest_snapshot is None and manifest_error_kind == "missing":
        issues.append(
            {
                "code": "novelty_manifest_missing",
                "path": f"refs/{paper_id}/novelty_verification_manifest.json",
                "message": "full-text novelty verification manifest is required",
            }
        )
    elif manifest_snapshot is None:
        issues.append(
            {
                "code": f"novelty_manifest_{manifest_error_kind}",
                "path": f"refs/{paper_id}/novelty_verification_manifest.json",
                "message": manifest_error,
            }
        )
    else:
        try:
            loaded = json.loads(manifest_snapshot.data.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            issues.append(
                {
                    "code": "novelty_manifest_unreadable",
                    "path": f"refs/{paper_id}/novelty_verification_manifest.json",
                    "message": f"manifest is not valid readable JSON: {type(exc).__name__}",
                }
            )
        else:
            if isinstance(loaded, dict):
                manifest = loaded
            else:
                issues.append(
                    {
                        "code": "novelty_manifest_invalid_root",
                        "path": f"refs/{paper_id}/novelty_verification_manifest.json",
                        "message": "manifest root must be an object",
                    }
                )

    if manifest is not None:
        if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            issues.append(
                {
                    "code": "novelty_manifest_schema",
                    "path": "novelty_verification_manifest.json",
                    "message": "schema_version must equal integer 1",
                }
            )
        if manifest.get("paper_id") != paper_id:
            issues.append(
                {
                    "code": "novelty_manifest_paper_id",
                    "path": "novelty_verification_manifest.json",
                    "message": "manifest paper_id does not match selected paper",
                }
            )
        if manifest.get("status") != "READY" or manifest.get("decision") != "PROCEED":
            issues.append(
                {
                    "code": "novelty_manifest_not_ready",
                    "path": "novelty_verification_manifest.json",
                    "message": "manifest status/decision must equal READY/PROCEED",
                }
            )
        if not str(manifest.get("claim_scope") or "").strip():
            issues.append(
                {
                    "code": "novelty_claim_scope_missing",
                    "path": "novelty_verification_manifest.json",
                    "message": "a narrowed claim_scope is required",
                }
            )
        result["close_full_text_count"] = _validate_review_sources(
            manifest, "close_full_text_sources", MIN_CLOSE_FULL_TEXT_SOURCES, issues
        )
        result["traditional_full_text_count"] = _validate_review_sources(
            manifest, "traditional_full_text_sources", MIN_TRADITIONAL_FULL_TEXT_SOURCES, issues
        )
        hashes = manifest.get("bundle_sha256")
        if not isinstance(hashes, dict):
            issues.append(
                {
                    "code": "novelty_bundle_hashes_invalid",
                    "path": "novelty_verification_manifest.json",
                    "message": "bundle_sha256 must be an object",
                }
            )
            hashes = {}
        for key in required_paths:
            expected = str(hashes.get(key) or "").strip()
            if not _SHA256_RE.fullmatch(expected):
                issues.append(
                    {
                        "code": "novelty_bundle_hash_missing",
                        "path": key,
                        "message": f"bundle_sha256.{key} must be a SHA256",
                    }
                )
                continue
            snapshot = required_snapshots.get(key)
            if snapshot is not None and snapshot.sha256 != expected.casefold():
                issues.append(
                    {
                        "code": "novelty_bundle_hash_mismatch",
                        "path": key,
                        "message": f"bundle_sha256.{key} does not match current bytes",
                    }
                )

    for changed_path, reason in _verify_stable_snapshots(root, snapshots):
        issues.append(
            {
                "code": "novelty_snapshot_changed",
                "path": changed_path.relative_to(root).as_posix(),
                "message": reason,
            }
        )

    if not issues:
        result["status"] = "READY"
        result["ready"] = True
    return result


def writing_novelty_blocker(result: dict[str, Any], *, limit: int = 5) -> str:
    issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
    parts = []
    for issue in issues[: max(1, limit)]:
        if not isinstance(issue, dict):
            continue
        parts.append(str(issue.get("code") or "novelty_gate_blocked"))
    suffix = f"; +{len(issues) - len(parts)} more" if len(issues) > len(parts) else ""
    return "Step 2->3 novelty gate BLOCKED: " + "; ".join(parts) + suffix
