from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .io_utils import now_iso, read_json, write_json_atomic, write_text
from .windows_subprocess import no_window_subprocess_kwargs


DEFAULT_AI_USE_STATEMENT = (
    "AI-assisted tools, including OpenAI Codex, were used during manuscript preparation for drafting support, "
    "language polishing, formatting, submission-package preparation, and workflow checks. The author reviewed "
    "and edited the output and takes full responsibility for all manuscript content."
)
DEFAULT_AI_USE_POLICY = "WHEN_REQUIRED"
DEFAULT_AI_USE_TOOLS = "OpenAI ChatGPT and Codex"
DEFAULT_AI_USE_SCOPE = (
    "Drafting support, language polishing, formatting, submission-package preparation, and workflow checks."
)
AI_USE_POLICIES = frozenset(
    {
        "WHEN_REQUIRED",
        "ALWAYS",
        "NEVER",
        "MANUAL_CONFIRM",
    }
)
DEFAULT_GITHUB_OWNER = "TYSJY"

_METHOD_PATTERNS = (
    re.compile(r"\\newcommand\s*\{\\(?:method|methodname|sysname|toolname)\}\s*\{([^{}]+)\}"),
    re.compile(r"\\def\s*\\(?:method|methodname|sysname|toolname)\s*\{([^{}]+)\}"),
)
_TITLE_RE = re.compile(r"\\title\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", re.DOTALL)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^##\s+")
_INCLUDEGRAPHICS_RE = re.compile(
    r"(\\includegraphics(?:\[[^\]]*\])?\{)([^}]+)(\})"
)
_GENERIC_GITHUB_RE = re.compile(
    r"(?i)^https?://github\.com/TYSJY/?$"
)
_PLACEHOLDER_CODE_TERMS = (
    "repository-specific url",
    "repository specific url",
    "should be inserted",
    "github account",
)
_AI_TEX_RE = re.compile(
    r"(?mi)^\\textbf\{(?:AI[- ]assisted technologies|AI use|Generative AI)[^}]*\}\s*.*$"
)
_CODE_TEX_RE = re.compile(
    r"(?mi)^\\textbf\{Code availability\.\}\s*.*$"
)
_AI_REQUIREMENT_RE = re.compile(
    r"(?is)(?:AI(?:[- ]assisted|[- ]use| tools?| technologies?| disclosure| acknowledgement)|"
    r"generative AI).{0,180}(?:required|requires|must|mandatory|MANUAL_REQUIRED|"
    r"should\s+(?:be\s+)?(?:included|disclosed|acknowledged)|needs?\s+to)"
)


def normalize_ai_use_policy(value: object) -> str:
    token = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "REQUIRED": "ALWAYS",
        "INSERT": "ALWAYS",
        "DO_NOT_INSERT": "NEVER",
        "OMIT": "NEVER",
        "MANUAL": "MANUAL_CONFIRM",
        "CONFIRM": "MANUAL_CONFIRM",
    }
    token = aliases.get(token, token)
    return token if token in AI_USE_POLICIES else DEFAULT_AI_USE_POLICY


@dataclass(frozen=True, slots=True)
class JournalSubmissionIdentity:
    paper_id: str
    paper_title: str
    repository_name: str
    repository_url: str
    ai_use_policy: str
    ai_use_tools: str
    ai_use_scope: str
    ai_use_statement: str


@dataclass(frozen=True, slots=True)
class JournalSubmissionFinalizeResult:
    ok: bool
    applied: bool
    paper_id: str
    repository_url: str = ""
    changed_files: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


def _safe_paper(root: str | Path, paper_id: str) -> tuple[Path, Path]:
    project_root = Path(root).expanduser().resolve()
    normalized_id = str(paper_id or "").strip()
    if (
        not normalized_id
        or Path(normalized_id).name != normalized_id
        or normalized_id in {".", ".."}
    ):
        raise ValueError("invalid paper_id")
    papers_root = (project_root / "papers").resolve()
    paper = (papers_root / normalized_id).resolve()
    paper.relative_to(papers_root)
    return project_root, paper


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _clean_latex_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"\\(?:textsc|textbf|textit|emph)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[A-Za-z@]+\*?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", text).strip(" :-\t\r\n")


def _safe_repository_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-_")
    name = re.sub(r"-{2,}", "-", name)
    return name[:100].rstrip(".-_") or "paper-repository"


def _tex_sources(paper: Path) -> list[Path]:
    candidates: list[Path] = []
    for directory in (paper / "work", paper / "source_submit", paper / "package"):
        if not directory.is_dir():
            continue
        candidates.extend(
            path
            for path in sorted(directory.glob("*.tex"), key=lambda item: item.name.casefold())
            if path.is_file() and path.name.casefold() != "main_blind.tex"
        )
    return candidates


def discover_submission_identity(
    root: str | Path,
    paper_id: str,
    *,
    github_owner: str = DEFAULT_GITHUB_OWNER,
    ai_use_policy: str = DEFAULT_AI_USE_POLICY,
    ai_use_tools: str = DEFAULT_AI_USE_TOOLS,
    ai_use_scope: str = DEFAULT_AI_USE_SCOPE,
    ai_use_statement: str = DEFAULT_AI_USE_STATEMENT,
) -> JournalSubmissionIdentity:
    _, paper = _safe_paper(root, paper_id)
    method = ""
    title = ""
    for path in _tex_sources(paper):
        text = _read(path)
        if not method:
            for pattern in _METHOD_PATTERNS:
                match = pattern.search(text)
                if match:
                    method = _clean_latex_text(match.group(1))
                    break
        if not title:
            match = _TITLE_RE.search(text)
            if match:
                raw_title = match.group(1)
                if method:
                    raw_title = re.sub(
                        r"\\(?:method|methodname|sysname|toolname)\s*(?:\{\})?",
                        method,
                        raw_title,
                    )
                title = _clean_latex_text(raw_title)
        if method and title:
            break
    title = title or str(paper_id)
    if method:
        repository_name = _safe_repository_name(method)
    else:
        legacy_slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
        repository_name = _safe_repository_name(legacy_slug)
    owner = _safe_repository_name(github_owner) or DEFAULT_GITHUB_OWNER
    return JournalSubmissionIdentity(
        paper_id=str(paper_id),
        paper_title=title,
        repository_name=repository_name,
        repository_url=f"https://github.com/{owner}/{repository_name}",
        ai_use_policy=normalize_ai_use_policy(ai_use_policy),
        ai_use_tools=str(ai_use_tools or DEFAULT_AI_USE_TOOLS).strip(),
        ai_use_scope=str(ai_use_scope or DEFAULT_AI_USE_SCOPE).strip(),
        ai_use_statement=str(ai_use_statement or DEFAULT_AI_USE_STATEMENT).strip(),
    )


def _missing_repository_url(value: object) -> bool:
    url = str(value or "").strip()
    return not url or bool(_GENERIC_GITHUB_RE.fullmatch(url))


def _placeholder_code_availability(value: object) -> bool:
    text = str(value or "").strip()
    lowered = text.casefold()
    return not text or any(term in lowered for term in _PLACEHOLDER_CODE_TERMS)


def _replace_markdown_section(text: str, heading: str, body: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    normalized = re.sub(r"(?<!\n)(##\s+)", r"\n\n\1", normalized)
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$.*?(?=^##\s+|\Z)"
    )
    section = f"## {heading}\n\n{body.strip()}\n"
    if pattern.search(normalized):
        replaced = False

        def replace(match: re.Match[str]) -> str:
            nonlocal replaced
            if replaced:
                return ""
            replaced = True
            return section.rstrip() + "\n\n"

        return pattern.sub(replace, normalized).rstrip() + "\n"
    suffix = "\n\n" if normalized else ""
    return normalized + suffix + section


def apply_submission_defaults_to_record(
    root: str | Path,
    paper_id: str,
    record: dict[str, object],
) -> JournalSubmissionIdentity:
    identity = discover_submission_identity(root, paper_id)
    requirements = record.get("submission_requirements")
    normalized = dict(requirements) if isinstance(requirements, dict) else {}
    if _missing_repository_url(normalized.get("github_repo_url")):
        normalized["github_repo_url"] = identity.repository_url
        normalized["github_repo_name"] = identity.repository_name
        normalized["github_repo_status"] = "planned_user_will_create"
        normalized["github_repo_policy"] = "user_authorized_planned_placeholder"
    normalized["ai_use_policy"] = normalize_ai_use_policy(
        normalized.get("ai_use_policy")
    )
    normalized["ai_use_tools"] = str(
        normalized.get("ai_use_tools") or identity.ai_use_tools
    ).strip()
    normalized["ai_use_scope"] = str(
        normalized.get("ai_use_scope") or identity.ai_use_scope
    ).strip()
    if not str(normalized.get("ai_use_statement") or "").strip():
        normalized["ai_use_statement"] = identity.ai_use_statement
    normalized["ai_use_source"] = str(
        normalized.get("ai_use_source") or "author_template"
    )
    record["submission_requirements"] = normalized
    if not str(record.get("paper_title") or "").strip():
        record["paper_title"] = identity.paper_title
    if _placeholder_code_availability(record.get("code_availability")):
        record["code_availability"] = (
            f"The source code will be made available at {normalized['github_repo_url']}."
        )
    record["submission_defaults_applied_at"] = now_iso()
    return JournalSubmissionIdentity(
        paper_id=identity.paper_id,
        paper_title=str(record.get("paper_title") or identity.paper_title),
        repository_name=str(
            normalized.get("github_repo_name") or identity.repository_name
        ),
        repository_url=str(
            normalized.get("github_repo_url") or identity.repository_url
        ),
        ai_use_policy=normalize_ai_use_policy(
            normalized.get("ai_use_policy")
        ),
        ai_use_tools=str(
            normalized.get("ai_use_tools") or identity.ai_use_tools
        ),
        ai_use_scope=str(
            normalized.get("ai_use_scope") or identity.ai_use_scope
        ),
        ai_use_statement=str(
            normalized.get("ai_use_statement") or identity.ai_use_statement
        ),
    )


def _ai_use_author_info_body(identity: JournalSubmissionIdentity) -> str:
    return "\n".join(
        [
            f"Insertion Policy: {normalize_ai_use_policy(identity.ai_use_policy)}",
            f"Tools: {identity.ai_use_tools.strip()}",
            f"Scope: {identity.ai_use_scope.strip()}",
            "",
            "Statement:",
            identity.ai_use_statement.strip(),
        ]
    ).strip()


def apply_submission_defaults_to_author_info(
    author_info: Path,
    identity: JournalSubmissionIdentity,
) -> bool:
    if not author_info.is_file():
        return False
    before = _read(author_info)
    after = _replace_markdown_section(
        before,
        "Code Availability",
        f"The source code will be made available at {identity.repository_url}.",
    )
    after = _replace_markdown_section(
        after,
        "AI Use Statement",
        _ai_use_author_info_body(identity),
    )
    if after == before:
        return False
    write_text(author_info, after)
    return True


def _metadata_payload_sha(payload: dict[str, object]) -> str:
    packed = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(packed).hexdigest()


def _update_fingerprint(
    path: Path,
    identity: JournalSubmissionIdentity,
) -> bool:
    data = read_json(path, {})
    if not isinstance(data, dict):
        return False
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return False
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    requirements = payload.get("submission_requirements")
    normalized = dict(requirements) if isinstance(requirements, dict) else {}
    normalized.update(
        {
            "github_repo_url": identity.repository_url,
            "github_repo_name": identity.repository_name,
            "github_repo_status": "planned_user_will_create",
            "github_repo_policy": "user_authorized_planned_placeholder",
            "ai_use_policy": normalize_ai_use_policy(identity.ai_use_policy),
            "ai_use_tools": identity.ai_use_tools,
            "ai_use_scope": identity.ai_use_scope,
            "ai_use_statement": identity.ai_use_statement,
            "ai_use_source": "author_template",
        }
    )
    payload["submission_requirements"] = normalized
    payload["paper_title"] = str(payload.get("paper_title") or identity.paper_title)
    payload["code_availability"] = (
        f"The source code will be made available at {identity.repository_url}."
    )
    payload["ai_use_policy"] = normalize_ai_use_policy(identity.ai_use_policy)
    payload["ai_use_tools"] = identity.ai_use_tools
    payload["ai_use_scope"] = identity.ai_use_scope
    payload["ai_use_statement"] = identity.ai_use_statement
    data["sha256"] = _metadata_payload_sha(payload)
    data["updated_at"] = now_iso()
    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if after == before:
        return False
    write_json_atomic(path, data)
    return True


def _remove_markdown_section(text: str, heading: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$.*?(?=^##\s+|\Z)"
    )
    return pattern.sub("", normalized).strip() + ("\n" if normalized else "")


def _package_has_ai_statement(package: Path) -> bool:
    declarations = _read(package / "declarations.md")
    main = _read(package / "main.tex")
    return bool(
        re.search(r"(?im)^##\s+AI\s+Use\s+(?:Statement|Declaration)\s*$", declarations)
        or _AI_TEX_RE.search(main)
    )


def _ai_package_action(
    paper: Path,
    package: Path,
    identity: JournalSubmissionIdentity,
) -> str:
    policy = normalize_ai_use_policy(identity.ai_use_policy)
    if policy == "ALWAYS":
        return "insert"
    if policy == "NEVER":
        return "remove"
    if policy == "MANUAL_CONFIRM":
        return "preserve"
    if _package_has_ai_statement(package):
        return "insert"
    evidence = "\n".join(
        _read(path)
        for path in (
            paper / "journal.md",
            package / "final_risk_report.md",
            package / "submission_checklist.md",
        )
    )
    return "insert" if _AI_REQUIREMENT_RE.search(evidence) else "preserve"


def _update_package_markdown(
    package: Path,
    identity: JournalSubmissionIdentity,
    *,
    ai_action: str,
) -> list[Path]:
    changed: list[Path] = []
    declarations = package / "declarations.md"
    if declarations.is_file():
        before = _read(declarations)
        after = _replace_markdown_section(
            before,
            "Code Availability",
            f"The source code will be made available at {identity.repository_url}.",
        )
        if ai_action == "insert":
            after = _replace_markdown_section(
                after,
                "AI Use Statement",
                identity.ai_use_statement,
            )
        elif ai_action == "remove":
            after = _remove_markdown_section(after, "AI Use Statement")
            after = _remove_markdown_section(after, "AI Use Declaration")
        if after != before:
            write_text(declarations, after)
            changed.append(declarations)
    return changed


def _update_main_tex(
    path: Path,
    identity: JournalSubmissionIdentity,
    *,
    blind: bool = False,
    ai_action: str,
) -> bool:
    if not path.is_file():
        return False
    before = _read(path)
    after = before
    code_line = (
        "\\textbf{Code availability.} Source code will be released after acceptance or at final online "
        "submission; repository-specific identifying details have been omitted for double-blind review."
        if blind
        else (
            "\\textbf{Code availability.} The source code will be made available at "
            f"\\url{{{identity.repository_url}}}."
        )
    )
    if _CODE_TEX_RE.search(after):
        after = _CODE_TEX_RE.sub(lambda _match: code_line, after, count=1)
    if ai_action == "remove":
        after = _AI_TEX_RE.sub("", after)
    elif ai_action == "insert":
        ai_line = (
            "\\textbf{AI-assisted technologies.} "
            + identity.ai_use_statement
        )
        if _AI_TEX_RE.search(after):
            after = _AI_TEX_RE.sub(lambda _match: ai_line, after, count=1)
        elif _CODE_TEX_RE.search(after):
            after = _CODE_TEX_RE.sub(
                lambda match: match.group(0) + "\n\n" + ai_line,
                after,
                count=1,
            )
    if after == before:
        return False
    write_text(path, after)
    return True


def _flatten_graphics(package: Path, main_tex: Path) -> tuple[bool, list[Path]]:
    text = _read(main_tex)
    changed_files: list[Path] = []
    replacements: dict[str, str] = {}
    source_paths: list[Path] = []
    for raw in _INCLUDEGRAPHICS_RE.findall(text):
        value = raw[1].strip().replace("\\", "/")
        graphic = Path(value)
        if len(graphic.parts) <= 1:
            continue
        source = package / graphic
        if not source.suffix:
            source = next(
                (
                    source.with_suffix(suffix)
                    for suffix in (".pdf", ".png", ".jpg", ".jpeg", ".eps")
                    if source.with_suffix(suffix).is_file()
                ),
                source,
            )
        if not source.is_file():
            raise ValueError(f"cannot flatten missing graphic: {value}")
        destination = package / source.name
        if destination.exists() and destination.resolve() != source.resolve():
            if destination.read_bytes() != source.read_bytes():
                raise ValueError(f"graphic basename collision: {source.name}")
        elif destination.resolve() != source.resolve():
            shutil.copy2(source, destination)
            changed_files.append(destination)
        replacements[value] = source.name
        source_paths.append(source)
    if not replacements:
        return False, changed_files

    def replace(match: re.Match[str]) -> str:
        value = match.group(2).strip().replace("\\", "/")
        return (
            f"{match.group(1)}{replacements.get(value, match.group(2))}"
            f"{match.group(3)}"
        )

    updated = _INCLUDEGRAPHICS_RE.sub(replace, text)
    if updated != text:
        write_text(main_tex, updated)
        changed_files.append(main_tex)
    referenced_directories = sorted(
        {
            source.parent
            for source in source_paths
            if source.parent != package and source.parent.is_relative_to(package)
        },
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in referenced_directories:
        if directory.name.casefold() == "figures":
            shutil.rmtree(directory)
            changed_files.append(directory)
    return True, changed_files


def _remove_redundant_figure_directory(package: Path) -> bool:
    directory = package / "figures"
    if not directory.is_dir():
        return False
    files = [path for path in directory.rglob("*") if path.is_file()]
    if not files:
        shutil.rmtree(directory)
        return True
    for source in files:
        destination = package / source.name
        if (
            not destination.is_file()
            or destination.read_bytes() != source.read_bytes()
        ):
            return False
    shutil.rmtree(directory)
    return True


def _compile_main(package: Path, stem: str) -> tuple[bool, str]:
    executable = shutil.which("pdflatex")
    if not executable:
        return False, "pdflatex is unavailable"
    output = ""
    for _ in range(2):
        completed = subprocess.run(
            [
                executable,
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"{stem}.tex",
            ],
            cwd=package,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
            **no_window_subprocess_kwargs(),
        )
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        if completed.returncode != 0:
            return False, output[-6000:]
    pdf = package / f"{stem}.pdf"
    return pdf.is_file() and pdf.stat().st_size > 0, output[-6000:]


def _write_file_manifest(package: Path) -> None:
    files = sorted(
        (
            path
            for path in package.rglob("*")
            if path.is_file() and path.name != "file_manifest.md"
        ),
        key=lambda item: item.relative_to(package).as_posix().casefold(),
    )
    lines = ["# File Manifest", ""]
    lines.extend(
        f"- {path.relative_to(package).as_posix()} ({path.stat().st_size} bytes)"
        for path in files
    )
    write_text(package / "file_manifest.md", "\n".join(lines))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recommended_reviewer_count(path: Path) -> int:
    if not path.is_file():
        return 0
    text = _read(path)
    numbered_headings = re.findall(r"(?m)^##\s+\d+\.\s+\S", text)
    if numbered_headings:
        return len(numbered_headings)
    return len(re.findall(r"(?m)^\s*\d+\.\s+\S", text))


def _recommended_journal(paper: Path) -> str:
    text = _read(paper / "journal.md")
    match = re.search(
        r"(?ims)^#{1,3}\s+(?:Recommended Journal|Target Journal)\s*$"
        r"\s*\n+\s*([^#\n][^\n]*)",
        text,
    )
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip(" -*_`\t\r\n")
    match = re.search(
        r"(?mi)^\s*(?:Recommended Journal|Target Journal)\s*:\s*(.+?)\s*$",
        text,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip(" -*_`\t\r\n") if match else ""


def _submission_file_role(path: Path) -> str:
    name = path.name.casefold()
    roles = {
        "main.pdf": "manuscript_review_pdf",
        "main.tex": "manuscript_source",
        "main_blind.pdf": "blind_manuscript_review_pdf",
        "main_blind.tex": "blind_manuscript_source",
        "cover_letter.md": "cover_letter_source",
        "declarations.md": "declarations_source",
        "suggested_reviewers.md": "recommended_reviewers_source",
    }
    if name in roles:
        return roles[name]
    if name.startswith("figure"):
        return "figure"
    return "supporting_file"


def _write_submission_manifest(
    package: Path,
    paper: Path,
    identity: JournalSubmissionIdentity,
    fingerprint_data: dict[str, object],
) -> Path:
    """Write the deterministic V2 handoff consumed by Smart Submission.

    This is an inventory, not a second readiness decision.  The authoritative
    readiness gate remains ``validate_journal_author_output`` so a stale or
    optimistic manifest can never make a package submit-ready by itself.
    """

    ignored_names = {
        "file_manifest.md",
        "package_metadata_fingerprint.json",
        "submission_manifest.json",
    }
    ignored_suffixes = {".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".synctex.gz"}
    files: list[dict[str, object]] = []
    for path in sorted(
        (item for item in package.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(package).as_posix().casefold(),
    ):
        relative = path.relative_to(package).as_posix()
        if path.name in ignored_names or any(path.name.casefold().endswith(suffix) for suffix in ignored_suffixes):
            continue
        files.append(
            {
                "path": relative,
                "role": _submission_file_role(path),
                "size": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )

    by_role = {str(item["role"]): str(item["path"]) for item in files}
    reviewers_path = package / "suggested_reviewers.md"
    reviewer_count = _recommended_reviewer_count(reviewers_path)
    generated_at = str(
        fingerprint_data.get("updated_at")
        or fingerprint_data.get("created_at")
        or ""
    ).strip()
    manifest: dict[str, object] = {
        "schema": "unified-paper-orchestrator/submission-manifest/v1",
        "version": 1,
        "paper_id": identity.paper_id,
        "paper_title": identity.paper_title,
        "target_journal": _recommended_journal(paper),
        "status": "PACKAGE_ASSEMBLED",
        "package_status": "PENDING_DETERMINISTIC_CONTRACT",
        "contract_authority": "journal_author_output_contract",
        "online_action": "validate_contract_then_submit",
        "metadata_fingerprint": {
            "path": "package_metadata_fingerprint.json",
            "sha256": str(fingerprint_data.get("sha256") or ""),
        },
        "upload_candidates": {
            "manuscript_review_pdf": by_role.get("manuscript_review_pdf", ""),
            "manuscript_source": by_role.get("manuscript_source", ""),
            "blind_manuscript_review_pdf": by_role.get("blind_manuscript_review_pdf", ""),
            "cover_letter_source": by_role.get("cover_letter_source", ""),
            "declarations_source": by_role.get("declarations_source", ""),
            "recommended_reviewers_source": by_role.get("recommended_reviewers_source", ""),
        },
        "recommended_reviewers": {
            "path": "suggested_reviewers.md" if reviewers_path.is_file() else "",
            "count": reviewer_count,
            "ready": reviewer_count >= 4,
        },
        "files": files,
        "remaining_blockers": [],
    }
    if generated_at:
        manifest["generated_at"] = generated_at
    destination = package / "submission_manifest.json"
    write_json_atomic(destination, manifest)
    return destination


def _sync_directory_contents(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or not destination.is_dir():
        raise ValueError("directory synchronization requires existing directories")
    copy_errors: list[str] = []
    for item in sorted(
        source.rglob("*"),
        key=lambda path: (len(path.parts), path.as_posix().casefold()),
    ):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(item, target)
            except OSError as exc:
                copy_errors.append(
                    f"cannot replace package file {target}: {exc}"
                )
    if copy_errors:
        raise OSError("; ".join(copy_errors))
    for target in sorted(
        destination.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        relative = target.relative_to(destination)
        if (source / relative).exists():
            continue
        try:
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            raise OSError(
                f"cannot remove stale package path {target}: {exc}"
            ) from exc


def finalize_journal_submission_policy(
    root: str | Path,
    paper_id: str,
    *,
    compile_tex: bool = True,
) -> JournalSubmissionFinalizeResult:
    project_root, paper = _safe_paper(root, paper_id)
    fingerprint = paper / "submission_metadata_fingerprint.json"
    data = read_json(fingerprint, {})
    payload = data.get("payload") if isinstance(data, dict) else None
    requirements = (
        payload.get("submission_requirements")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(requirements, dict):
        return JournalSubmissionFinalizeResult(
            ok=True,
            applied=False,
            paper_id=str(paper_id),
        )
    discovered = discover_submission_identity(project_root, paper_id)
    identity = JournalSubmissionIdentity(
        paper_id=str(paper_id),
        paper_title=str(payload.get("paper_title") or discovered.paper_title),
        repository_name=str(
            requirements.get("github_repo_name") or discovered.repository_name
        ),
        repository_url=str(
            requirements.get("github_repo_url") or discovered.repository_url
        ),
        ai_use_policy=normalize_ai_use_policy(
            requirements.get("ai_use_policy")
            or payload.get("ai_use_policy")
            or discovered.ai_use_policy
        ),
        ai_use_tools=str(
            requirements.get("ai_use_tools")
            or payload.get("ai_use_tools")
            or discovered.ai_use_tools
        ),
        ai_use_scope=str(
            requirements.get("ai_use_scope")
            or payload.get("ai_use_scope")
            or discovered.ai_use_scope
        ),
        ai_use_statement=str(
            requirements.get("ai_use_statement")
            or payload.get("ai_use_statement")
            or discovered.ai_use_statement
        ),
    )
    if _missing_repository_url(identity.repository_url):
        return JournalSubmissionFinalizeResult(
            ok=False,
            applied=False,
            paper_id=str(paper_id),
            issues=("submission metadata is missing repository URL",),
        )

    changed: list[Path] = []
    if apply_submission_defaults_to_author_info(paper / "author_info.md", identity):
        changed.append(paper / "author_info.md")
    if _update_fingerprint(fingerprint, identity):
        changed.append(fingerprint)
    package = paper / "package"
    if not package.is_dir():
        return JournalSubmissionFinalizeResult(
            ok=True,
            applied=bool(changed),
            paper_id=str(paper_id),
            repository_url=identity.repository_url,
            changed_files=tuple(str(path) for path in changed),
        )

    token = uuid4().hex
    candidate = paper / f".upo-journal-policy-{token}"
    backup = paper / f".upo-package-before-policy-{token}"
    ai_action = _ai_package_action(paper, package, identity)
    if ai_action == "insert" and not identity.ai_use_statement.strip():
        return JournalSubmissionFinalizeResult(
            ok=False,
            applied=False,
            paper_id=str(paper_id),
            repository_url=identity.repository_url,
            changed_files=tuple(str(path) for path in changed),
            issues=("AI-use insertion policy requires a non-empty fixed statement",),
        )
    directory_backup_created = False
    file_synced = False
    try:
        shutil.copytree(package, candidate)
        candidate_changed = _update_package_markdown(
            candidate,
            identity,
            ai_action=ai_action,
        )
        main_changed = _update_main_tex(
            candidate / "main.tex",
            identity,
            ai_action=ai_action,
        )
        blind_changed = _update_main_tex(
            candidate / "main_blind.tex",
            identity,
            blind=True,
            ai_action=ai_action,
        )
        flattened, flattened_paths = _flatten_graphics(
            candidate,
            candidate / "main.tex",
        )
        flattened = _remove_redundant_figure_directory(candidate) or flattened
        main_tex = candidate / "main.tex"
        main_pdf = candidate / "main.pdf"
        pdf_stale = bool(
            main_tex.is_file()
            and (
                not main_pdf.is_file()
                or main_pdf.stat().st_mtime_ns < main_tex.stat().st_mtime_ns
            )
        )
        if main_changed or flattened or pdf_stale:
            if compile_tex:
                compiled, output = _compile_main(candidate, "main")
                if not compiled:
                    raise ValueError("main.tex compilation failed after policy finalization: " + output)
        if blind_changed and compile_tex:
            compiled, output = _compile_main(candidate, "main_blind")
            if not compiled:
                raise ValueError(
                    "main_blind.tex compilation failed after policy finalization: "
                    + output
                )
        for pending in candidate.glob("main_updated.*"):
            if pending.is_file():
                pending.unlink()
        if fingerprint.is_file():
            shutil.copy2(
                fingerprint,
                candidate / "package_metadata_fingerprint.json",
            )
        manifest_fingerprint = read_json(fingerprint, {})
        _write_submission_manifest(
            candidate,
            paper,
            identity,
            manifest_fingerprint if isinstance(manifest_fingerprint, dict) else {},
        )
        _write_file_manifest(candidate)
        try:
            package.rename(backup)
            directory_backup_created = True
            candidate.rename(package)
            shutil.rmtree(backup)
            directory_backup_created = False
        except PermissionError:
            if backup.exists():
                raise
            shutil.copytree(package, backup)
            try:
                _sync_directory_contents(candidate, package)
                file_synced = True
            except BaseException:
                # Candidate files are compiled and internally valid. If a
                # viewer locks one destination file, keep any already copied
                # candidate files and retry later instead of destructively
                # rolling back through the same locked handle.
                raise
            shutil.rmtree(backup)
        changed.extend(
            package / path.relative_to(candidate)
            for path in candidate_changed
            if path.exists() or path.name
        )
        if main_changed or flattened or pdf_stale:
            changed.append(package / "main.tex")
            changed.append(package / "main.pdf")
        if blind_changed:
            changed.append(package / "main_blind.tex")
            changed.append(package / "main_blind.pdf")
        if flattened:
            changed.extend(
                package / path.relative_to(candidate)
                for path in flattened_paths
                if path.is_relative_to(candidate)
            )
        changed.append(package / "file_manifest.md")
        changed.append(package / "submission_manifest.json")
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        if directory_backup_created and backup.is_dir():
            failed = paper / f".upo-package-failed-policy-{token}"
            if package.exists():
                package.rename(failed)
            backup.rename(package)
            if failed.exists():
                shutil.rmtree(failed)
        elif file_synced and backup.is_dir():
            _sync_directory_contents(backup, package)
        return JournalSubmissionFinalizeResult(
            ok=False,
            applied=False,
            paper_id=str(paper_id),
            repository_url=identity.repository_url,
            changed_files=tuple(str(path) for path in changed),
            issues=(f"{type(exc).__name__}: {exc}",),
        )
    finally:
        if candidate.is_dir():
            shutil.rmtree(candidate)
        if backup.is_dir():
            shutil.rmtree(backup)

    package_fingerprint = package / "package_metadata_fingerprint.json"
    if fingerprint.is_file():
        shutil.copy2(fingerprint, package_fingerprint)
        changed.append(package_fingerprint)
    return JournalSubmissionFinalizeResult(
        ok=True,
        applied=bool(changed),
        paper_id=str(paper_id),
        repository_url=identity.repository_url,
        changed_files=tuple(dict.fromkeys(str(path) for path in changed)),
    )


__all__ = [
    "AI_USE_POLICIES",
    "DEFAULT_AI_USE_POLICY",
    "DEFAULT_AI_USE_SCOPE",
    "DEFAULT_AI_USE_STATEMENT",
    "DEFAULT_AI_USE_TOOLS",
    "DEFAULT_GITHUB_OWNER",
    "JournalSubmissionFinalizeResult",
    "JournalSubmissionIdentity",
    "apply_submission_defaults_to_author_info",
    "apply_submission_defaults_to_record",
    "discover_submission_identity",
    "finalize_journal_submission_policy",
    "normalize_ai_use_policy",
]
