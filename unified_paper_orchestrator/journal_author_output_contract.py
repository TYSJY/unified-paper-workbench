from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PACKAGE_REQUIRED_FILES = (
    "main.tex",
    "cover_letter.md",
    "declarations.md",
    "submission_checklist.md",
    "file_manifest.md",
    "final_risk_report.md",
    "package_metadata_fingerprint.json",
)
_UNRESOLVED_RE = re.compile(r"(?i)(?:\bTODO\b|【验】|\\VERIFY\b|\bUNRESOLVED\b)")
_LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
_REFERENCE_RE = re.compile(
    r"\\(?:ref|eqref|pageref|autoref|cref|Cref|vref|Vref)\*?\{([^}]+)\}"
)
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_TABLE_ENV_RE = re.compile(r"\\begin\{table\*?\}")
_SECTION_RE = re.compile(r"\\(?:section|subsection|subsubsection)\*?\{")
_UNDEFINED_REFERENCE_RE = re.compile(
    r"(?:LaTeX Warning:\s+Reference\s+[`'].+?[`']\s+on page .+? undefined|"
    r"There were undefined references)",
    re.IGNORECASE,
)
_SELECTION_STALE_MARKER = ".journal_selection_stale"
_JOURNAL_REQUIRED_HEADINGS = (
    "Paper ID",
    "Paper Title",
    "Recommended Journal",
    "Why this journal is the safest choice",
    "Scope match",
    "Quartile / indexing evidence",
    "Similar published papers",
    "APC and cost",
    "Submission format notes",
    "Required files",
    "Top 5 candidates",
    "Rejected journals",
    "Final decision",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _sha256(path),
    }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _journal_decision(text: str) -> str:
    match = re.search(
        r"(?ims)^\s*##\s+Final\s+decision\s*$\s*(?:Final\s+decision\s*:\s*)?([^\r\n]+)",
        text or "",
    )
    if not match:
        return ""
    value = re.sub(r"[`*_]", "", match.group(1)).strip(" :-\t").casefold()
    if "do not submit" in value:
        return "Do not submit yet"
    if "needs user confirmation" in value:
        return "Needs user confirmation"
    if value == "recommended" or value.startswith("recommended "):
        return "Recommended"
    return match.group(1).strip()


def _package_status(text: str) -> str:
    explicit = re.search(
        r"(?im)^\s*(?:PACKAGE_STATUS|Status|Conclusion)\s*[:=]\s*(READY_TO_SUBMIT|NEEDS_MINOR_FIX|BLOCKED)\b",
        text or "",
    )
    if explicit:
        return explicit.group(1).upper()
    for value in ("READY_TO_SUBMIT", "NEEDS_MINOR_FIX", "BLOCKED"):
        if re.search(rf"(?im)^\s*{value}\s*$", text or ""):
            return value
    return ""


def _reference_targets(tex: str) -> set[str]:
    targets: set[str] = set()
    for raw in _REFERENCE_RE.findall(tex):
        targets.update(
            target.strip()
            for target in re.split(r"[,;]", raw)
            if target.strip()
        )
    return targets


def _submission_requirements(paper: Path) -> dict[str, Any] | None:
    path = paper / "submission_metadata_fingerprint.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    payload = data.get("payload") if isinstance(data, dict) else None
    requirements = (
        payload.get("submission_requirements")
        if isinstance(payload, dict)
        else None
    )
    return dict(requirements) if isinstance(requirements, dict) else None


def _specific_github_url(value: object) -> bool:
    text = str(value or "").strip()
    return bool(
        re.fullmatch(
            r"https://github\.com/[^/\s]+/[^/\s#?]+/?",
            text,
            flags=re.IGNORECASE,
        )
    )


def _abstract_word_count(tex: str) -> int | None:
    match = re.search(
        r"(?is)\\begin\{abstract\}(.*?)\\end\{abstract\}",
        tex or "",
    )
    if not match:
        return None
    value = match.group(1)
    value = re.sub(
        r"\\(?:textbf|textit|textsc|emph|url)\s*\{([^{}]*)\}",
        r"\1",
        value,
    )
    value = re.sub(r"\\[A-Za-z@]+\*?(?:\{\})?", " ", value)
    value = re.sub(r"[^A-Za-z0-9%+.-]+", " ", value)
    return len([word for word in value.split() if word])


def _report_action_lines(text: str) -> list[str]:
    actions: list[str] = []
    in_action_section = False
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].casefold()
            in_action_section = any(
                token in heading
                for token in (
                    "minor fix",
                    "issues to fix",
                    "manual action",
                    "before online submission",
                )
            )
            continue
        if not in_action_section:
            continue
        match = re.match(r"^(?:[-*]|\d+\.)\s+(.+)$", stripped)
        if match:
            actions.append(match.group(1).strip())
    return actions


def _suggested_reviewers_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    text = _read(path)
    headings = list(
        re.finditer(
            r"(?im)^##\s+\d+\.\s+.+$",
            text,
        )
    )
    if len(headings) < 4:
        return False
    for index, heading in enumerate(headings[:4]):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.end():end]
        if not re.search(r"(?im)^[-*]\s*Email\s*:\s*\S+@\S+\s*$", block):
            return False
        if not re.search(r"(?im)^[-*]\s*Full address\s*:\s*\S.+$", block):
            return False
        if not re.search(r"(?im)^[-*]\s*Expertise\s*:\s*\S.+$", block):
            return False
    return True


def _classify_report_actions(
    actions: list[str],
    *,
    journal_text: str,
    repository_ready: bool,
    ai_statement_ready: bool,
    abstract_compliant: bool,
    graphics_flat: bool,
    suggested_reviewers_ready: bool,
) -> tuple[list[str], list[str]]:
    manual: list[str] = []
    optional: list[str] = []
    journal_lower = journal_text.casefold()
    template_recommended = (
        "template is recommended" in journal_lower
        or "recommends the springer" in journal_lower
        or "encourage authors to use" in journal_lower
    )
    latex_submission_accepted = bool(
        re.search(
            r"(?i)(?:latex files? (?:are )?accepted|accepts? latex|latex support\s*:\s*yes)",
            journal_text,
        )
    )
    for action in actions:
        lowered = action.casefold()
        if (
            "reviewer" in lowered
            and ("online" in lowered or "form" in lowered)
            and suggested_reviewers_ready
        ):
            optional.append(action)
            continue
        if (
            ("repository" in lowered or "github" in lowered or "code url" in lowered)
            and repository_ready
        ):
            continue
        if (
            ("ai-use" in lowered or "ai use" in lowered or "ai tools" in lowered)
            and ai_statement_ready
        ):
            continue
        if "abstract" in lowered and abstract_compliant:
            continue
        if (
            ("subfolder" in lowered or "flatten" in lowered)
            and graphics_flat
        ):
            continue
        if "template" in lowered and (
            "optional" in lowered
            or "recommend" in lowered
            or "if the user wants" in lowered
            or template_recommended
            or (
                latex_submission_accepted
                and (
                    "or confirm" in lowered
                    or "submission system" in lowered
                    or "scholarone" in lowered
                    or "sage track" in lowered
                )
            )
        ):
            optional.append(action)
            continue
        if "quartile" in lowered and (" if " in f" {lowered} " or "precision" in lowered):
            optional.append(action)
            continue
        if "biograph" in lowered and ("if requested" in lowered or "if required" in lowered):
            optional.append(action)
            continue
        if (
            "if requested" in lowered
            or "if required" in lowered
            or "if the submission system" in lowered
            or "if requested by the online system" in lowered
            or "if the live form" in lowered
        ) and (
            "author contribution" in lowered
            or "credit statement" in lowered
            or "credit taxonomy" in lowered
            or "online metadata" in lowered
            or "reference" in lowered
            or "harvard" in lowered
            or "figure" in lowered
            or "table" in lowered
            or "layout" in lowered
            or "template" in lowered
        ):
            optional.append(action)
            continue
        manual.append(action)
    return manual, optional


def _manual_action_display(value: str) -> str:
    lowered = str(value or "").casefold()
    if (
        ("expert suggestion" in lowered or "reviewer suggestion" in lowered)
        and ("four" in lowered or "4 " in lowered)
    ):
        return "在线投稿表单需要填写 4 位推荐审稿专家；当前尚未提供"
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class JournalAuthorContractResult:
    ok: bool
    status: str
    step_done: int
    package_status: str
    journal_decision: str
    issues: tuple[str, ...]
    warnings: tuple[str, ...]
    artifacts: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "step_done": self.step_done,
            "package_status": self.package_status,
            "journal_decision": self.journal_decision,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "artifacts": [dict(item) for item in self.artifacts],
        }


def validate_journal_author_input(root: str | Path, paper_id: str) -> JournalAuthorContractResult:
    project_root = Path(root).expanduser().resolve()
    paper = project_root / "papers" / paper_id
    issues: list[str] = []
    artifacts: list[dict[str, Any]] = []
    work = paper / "work"
    tex_files = sorted(path for path in work.glob("*.tex") if path.is_file()) if work.is_dir() else []
    if not tex_files:
        issues.append("work 目录缺少主 TeX")
    else:
        artifacts.extend(_artifact(path, project_root) for path in tex_files)
    author = paper / "author_info.md"
    author_text = _read(author)
    if not author.is_file():
        issues.append("author_info.md 不存在")
    else:
        artifacts.append(_artifact(author, project_root))
        if not re.search(r"(?im)^\s*Status\s*:\s*READY\s*$", author_text):
            issues.append("author_info.md 不是 Status: READY")
        if re.search(r"(?i)\bTODO\b", author_text):
            issues.append("author_info.md 仍含 TODO")
    fingerprint = paper / "submission_metadata_fingerprint.json"
    if not fingerprint.is_file():
        issues.append("submission_metadata_fingerprint.json 不存在")
    else:
        artifacts.append(_artifact(fingerprint, project_root))
    return JournalAuthorContractResult(
        ok=not issues,
        status="ready" if not issues else "blocked",
        step_done=1 if tex_files else 0,
        package_status="",
        journal_decision="",
        issues=tuple(issues),
        warnings=(),
        artifacts=tuple(artifacts),
    )


def validate_journal_selection(root: str | Path, paper_id: str) -> JournalAuthorContractResult:
    project_root = Path(root).expanduser().resolve()
    paper = project_root / "papers" / paper_id
    input_result = validate_journal_author_input(project_root, paper_id)
    issues = list(input_result.issues)
    warnings: list[str] = []
    artifacts = [dict(item) for item in input_result.artifacts]
    journal = paper / "journal.md"
    text = _read(journal)
    decision = _journal_decision(text)
    if not journal.is_file():
        issues.append("journal.md 不存在")
    else:
        artifacts.append(_artifact(journal, project_root))
        missing_headings = [
            heading
            for heading in _JOURNAL_REQUIRED_HEADINGS
            if not re.search(rf"(?im)^\s*##\s+{re.escape(heading)}\s*$", text)
        ]
        if missing_headings:
            issues.append("journal.md 缺少栏目：" + "、".join(missing_headings))
        if not re.search(r"(?i)https?://", text):
            issues.append("journal.md 没有期刊官网或核验链接")
        if decision == "Needs user confirmation":
            issues.append("选刊结果需要用户确认")
        elif decision == "Do not submit yet":
            issues.append("选刊结果明确要求暂不投稿")
        elif decision != "Recommended":
            issues.append(f"journal.md Final decision 无效：{decision or '缺失'}")
    return JournalAuthorContractResult(
        ok=not issues,
        status="complete" if not issues else ("needs_user" if decision == "Needs user confirmation" else "blocked"),
        step_done=2 if journal.is_file() else input_result.step_done,
        package_status="",
        journal_decision=decision,
        issues=tuple(dict.fromkeys(issues)),
        warnings=tuple(warnings),
        artifacts=tuple(artifacts),
    )


def sync_package_metadata_fingerprint(root: str | Path, paper_id: str) -> Path:
    project_root = Path(root).expanduser().resolve()
    paper = project_root / "papers" / paper_id
    source = paper / "submission_metadata_fingerprint.json"
    if not source.is_file():
        raise FileNotFoundError("submission_metadata_fingerprint.json 不存在")
    package = paper / "package"
    package.mkdir(parents=True, exist_ok=True)
    destination = package / "package_metadata_fingerprint.json"
    payload = source.read_bytes()
    if not destination.is_file() or destination.read_bytes() != payload:
        destination.write_bytes(payload)
    stale = package / _SELECTION_STALE_MARKER
    if stale.exists():
        stale.unlink()
    return destination


def mark_package_selection_stale(root: str | Path, paper_id: str) -> Path:
    project_root = Path(root).expanduser().resolve()
    marker = project_root / "papers" / paper_id / "package" / _SELECTION_STALE_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("journal.md was regenerated; rerun package generation before submission.\n", encoding="utf-8")
    return marker


def validate_journal_author_output(root: str | Path, paper_id: str) -> JournalAuthorContractResult:
    project_root = Path(root).expanduser().resolve()
    paper = project_root / "papers" / paper_id
    input_result = validate_journal_author_input(project_root, paper_id)
    issues = list(input_result.issues)
    warnings: list[str] = []
    artifacts = [dict(item) for item in input_result.artifacts]
    journal = paper / "journal.md"
    journal_text = _read(journal)
    decision = _journal_decision(journal_text)
    step_done = input_result.step_done
    if not journal.is_file():
        issues.append("journal.md 不存在")
    else:
        artifacts.append(_artifact(journal, project_root))
        step_done = max(step_done, 2)
        if decision != "Recommended":
            issues.append(f"journal.md Final decision 不是 Recommended：{decision or '缺失'}")

    package = paper / "package"
    stale_marker = package / _SELECTION_STALE_MARKER
    if stale_marker.is_file():
        artifacts.append(_artifact(stale_marker, project_root))
        issues.append("journal.md 已重新生成，投稿包需要重新生成或重新验收")
    report = package / "final_risk_report.md"
    report_text = _read(report)
    raw_package_status = _package_status(report_text)
    package_status = raw_package_status
    missing = [name for name in _PACKAGE_REQUIRED_FILES if not (package / name).is_file()]
    if missing:
        issues.append("投稿包缺少：" + "、".join(missing))
    else:
        artifacts.extend(_artifact(package / name, project_root) for name in _PACKAGE_REQUIRED_FILES)

    root_fingerprint = paper / "submission_metadata_fingerprint.json"
    package_fingerprint = package / "package_metadata_fingerprint.json"
    if root_fingerprint.is_file() and package_fingerprint.is_file():
        if root_fingerprint.read_bytes() != package_fingerprint.read_bytes():
            issues.append("投稿包元数据指纹与当前作者/声明配置不一致")

    main_pdf = package / "main.pdf"
    if main_pdf.is_file():
        artifacts.append(_artifact(main_pdf, project_root))
    elif package_status == "READY_TO_SUBMIT":
        issues.append("投稿包标记为 READY_TO_SUBMIT，但缺少 main.pdf")
    elif package_status == "NEEDS_MINOR_FIX":
        warnings.append("投稿包缺少 main.pdf；已按 NEEDS_MINOR_FIX 接受，提交前仍需完成编译")

    main_tex = package / "main.tex"
    main_text = _read(main_tex)
    if main_tex.is_file():
        if (
            main_pdf.is_file()
            and main_pdf.stat().st_mtime_ns < main_tex.stat().st_mtime_ns
        ):
            issues.append("package/main.pdf 早于 main.tex，需重新编译后才能投稿")
        if re.search(r"(?i)Anonymous\s+Authors?", main_text):
            issues.append("package/main.tex 仍包含 Anonymous Authors")
        unresolved = sorted(set(match.group(0) for match in _UNRESOLVED_RE.finditer(main_text)))
        if unresolved:
            issues.append("package/main.tex 仍含未清理标记：" + "、".join(unresolved))

    if main_tex.is_file():
        output_labels = {label.strip() for label in _LABEL_RE.findall(main_text) if label.strip()}
        unresolved_refs = sorted(_reference_targets(main_text) - output_labels, key=str.casefold)
        if unresolved_refs:
            issues.append(
                "package/main.tex 存在未解析交叉引用："
                + ", ".join(unresolved_refs[:12])
            )
        for raw_graphic in _INCLUDEGRAPHICS_RE.findall(main_text):
            graphic = Path(raw_graphic.strip())
            candidates = [package / graphic]
            if not graphic.suffix:
                candidates.extend(
                    (package / graphic).with_suffix(suffix)
                    for suffix in (".pdf", ".png", ".jpg")
                )
            if not any(path.is_file() and path.stat().st_size > 0 for path in candidates):
                issues.append(f"package/main.tex 引用的图片不存在：{raw_graphic}")

        work_tex_files = sorted(
            path for path in (paper / "work").glob("*.tex") if path.is_file()
        )
        if len(work_tex_files) == 1:
            work_text = _read(work_tex_files[0])
            work_labels = {label.strip() for label in _LABEL_RE.findall(work_text) if label.strip()}
            missing_work_labels = sorted(work_labels - output_labels, key=str.casefold)
            if missing_work_labels:
                issues.append(
                    "投稿包丢失导入稿标签：" + ", ".join(missing_work_labels[:12])
                )
            work_tables = len(_TABLE_ENV_RE.findall(work_text))
            package_tables = len(_TABLE_ENV_RE.findall(main_text))
            if package_tables < work_tables:
                issues.append(
                    f"投稿包 table 环境异常减少：导入稿 {work_tables}，投稿包 {package_tables}"
                )
            work_sections = len(_SECTION_RE.findall(work_text))
            package_sections = len(_SECTION_RE.findall(main_text))
            if package_sections < work_sections:
                issues.append(
                    f"投稿包章节结构异常减少：导入稿 {work_sections}，投稿包 {package_sections}"
                )

        main_log = package / "main.log"
        if main_log.is_file():
            artifacts.append(_artifact(main_log, project_root))
            if _UNDEFINED_REFERENCE_RE.search(_read(main_log)):
                issues.append("package/main.log 仍包含 undefined references")
        elif main_pdf.is_file():
            warnings.append("未找到 package/main.log，无法复核 undefined references")

    requirements = _submission_requirements(paper)
    repository_url = (
        str(requirements.get("github_repo_url") or "").strip()
        if isinstance(requirements, dict)
        else ""
    )
    ai_statement = (
        str(requirements.get("ai_use_statement") or "").strip()
        if isinstance(requirements, dict)
        else ""
    )
    declarations_text = _read(package / "declarations.md")
    combined_declarations = declarations_text + "\n" + main_text
    repository_ready = bool(
        _specific_github_url(repository_url)
        and repository_url.casefold() in combined_declarations.casefold()
    )
    ai_statement_ready = bool(
        ai_statement
        and (
            ai_statement[:60].casefold() in combined_declarations.casefold()
            or re.search(
                r"(?i)(?:AI[- ]assisted technologies|AI[- ]use statement|generative AI)",
                combined_declarations,
            )
        )
    )
    if isinstance(requirements, dict):
        if not repository_ready:
            issues.append("投稿包缺少论文专属 GitHub 仓库 URL，或声明未同步该 URL")
        if not ai_statement_ready:
            issues.append("投稿包缺少已授权的 AI-use 声明，或声明未同步到投稿包")

    abstract_words = _abstract_word_count(main_text)
    abstract_rule = bool(
        re.search(
            r"(?i)abstract.{0,80}100\s*(?:-|–|to)\s*150\s+words?",
            journal_text,
        )
    )
    abstract_compliant = bool(
        not abstract_rule
        or (
            abstract_words is not None
            and 100 <= abstract_words <= 150
        )
    )
    if abstract_rule and not abstract_compliant:
        issues.append(
            "摘要不符合目标期刊 100–150 词要求："
            + (f"当前 {abstract_words} 词" if abstract_words is not None else "未找到摘要")
        )

    graphic_paths = [
        Path(value.strip().replace("\\", "/"))
        for value in _INCLUDEGRAPHICS_RE.findall(main_text)
    ]
    graphics_flat = all(len(path.parts) <= 1 for path in graphic_paths)
    no_subfolder_rule = bool(
        re.search(
            r"(?i)(?:do not use|avoid(?:ing)?|without)\s+subfolders?|"
            r"subfolders?.{0,80}(?:reject|not allowed|avoid|flatten)",
            journal_text,
        )
    )
    if no_subfolder_rule and not graphics_flat:
        issues.append("目标期刊要求 LaTeX 上传不使用子目录，但 main.tex 仍引用子目录图片")

    manual_actions, optional_notes = _classify_report_actions(
        _report_action_lines(report_text),
        journal_text=journal_text,
        repository_ready=repository_ready,
        ai_statement_ready=ai_statement_ready,
        abstract_compliant=abstract_compliant,
        graphics_flat=graphics_flat,
        suggested_reviewers_ready=_suggested_reviewers_ready(
            package / "suggested_reviewers.md"
        ),
    )
    warnings.extend(
        f"需人工处理：{_manual_action_display(value)}"
        for value in manual_actions
    )
    warnings.extend(f"非阻塞提示：{value}" for value in optional_notes)

    blocker = paper / "package_blocker.md"
    if blocker.is_file():
        artifacts.append(_artifact(blocker, project_root))
        if not package_status or missing:
            blocker_line = next((line.strip("# -*\t") for line in _read(blocker).splitlines() if line.strip("# -*\t")), "")
            issues.append("package_blocker.md：" + (blocker_line or "存在有效阻塞"))
        else:
            warnings.append("发现旧 package_blocker.md，但完整投稿包优先按当前严格契约验收")

    if raw_package_status not in {"READY_TO_SUBMIT", "NEEDS_MINOR_FIX", "BLOCKED"}:
        issues.append("final_risk_report.md 缺少有效 PACKAGE_STATUS")
    elif raw_package_status == "BLOCKED":
        issues.append("final_risk_report.md 将投稿包标记为 BLOCKED")

    if not missing and decision == "Recommended":
        step_done = 3
    deduped_issues = tuple(dict.fromkeys(value for value in issues if value))
    if (
        raw_package_status == "NEEDS_MINOR_FIX"
        and not deduped_issues
        and not manual_actions
        and main_pdf.is_file()
    ):
        package_status = "READY_TO_SUBMIT"
        warnings.append("AI 风险报告原标记为 NEEDS_MINOR_FIX；程序复核后无剩余必做项，已按可投稿处理")
    elif (
        raw_package_status == "READY_TO_SUBMIT"
        and manual_actions
        and not deduped_issues
    ):
        package_status = "NEEDS_MINOR_FIX"
    if not deduped_issues and package_status in {"READY_TO_SUBMIT", "NEEDS_MINOR_FIX"}:
        status = "complete"
        ok = True
    elif journal.is_file() and (blocker.is_file() or package_status == "BLOCKED"):
        status = "blocked"
        ok = False
    else:
        status = "incomplete"
        ok = False
    return JournalAuthorContractResult(
        ok=ok,
        status=status,
        step_done=step_done,
        package_status=package_status,
        journal_decision=decision,
        issues=deduped_issues,
        warnings=tuple(dict.fromkeys(warnings)),
        artifacts=tuple(artifacts),
    )


__all__ = [
    "JournalAuthorContractResult",
    "mark_package_selection_stale",
    "sync_package_metadata_fingerprint",
    "validate_journal_author_input",
    "validate_journal_author_output",
    "validate_journal_selection",
]
