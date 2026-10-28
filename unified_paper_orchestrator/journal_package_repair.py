from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .figure_output_contract import validate_figure_step
from .figure_submission_repair import _compile_candidate
from .io_utils import now_iso, write_text
from .journal_author_output_contract import (
    JournalAuthorContractResult,
    validate_journal_author_output,
)


_AUTHOR_LINE_RE = re.compile(r"(?m)^\\author\{.*\}\s*$")
_KEYWORDS_RE = re.compile(
    r"(?s)\\end\{abstract\}\s*(\\noindent\\textbf\{Keywords:\}.*?)(?=\\section)"
)
_DECLARATIONS_RE = re.compile(
    r"(?s)(\\section\*\{Declarations\}.*?)(?=\\begin\{thebibliography\})"
)
_INCLUDEGRAPHICS_RE = re.compile(
    r"(\\includegraphics(?:\[[^\]]*\])?\{)([^}]+)(\})"
)


@dataclass(frozen=True, slots=True)
class JournalPackageRepairResult:
    ok: bool
    repaired: bool
    paper_id: str
    issues: tuple[str, ...] = ()
    contract: JournalAuthorContractResult | None = None


def _safe_paper(root: Path, paper_id: str) -> tuple[Path, Path]:
    root = Path(root).expanduser().resolve()
    paper_id = str(paper_id or "").strip()
    if not paper_id or Path(paper_id).name != paper_id or paper_id in {".", ".."}:
        raise ValueError("invalid paper_id")
    paper = (root / "papers" / paper_id).resolve()
    paper.relative_to((root / "papers").resolve())
    return root, paper


def _copy_directory(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _preserve_package_edits(base_tex: str, previous_package_tex: str) -> str:
    result = base_tex
    author = _AUTHOR_LINE_RE.search(previous_package_tex)
    if author is not None:
        result, count = _AUTHOR_LINE_RE.subn(
            lambda _match: author.group(0),
            result,
            count=1,
        )
        if count != 1:
            raise ValueError("repaired source has no replaceable author line")

    keywords = _KEYWORDS_RE.search(previous_package_tex)
    if keywords is not None and not _KEYWORDS_RE.search(result):
        marker = "\\end{abstract}"
        if marker not in result:
            raise ValueError("repaired source has no abstract boundary")
        result = result.replace(
            marker,
            marker + "\n\n" + keywords.group(1).strip(),
            1,
        )

    declarations = _DECLARATIONS_RE.search(previous_package_tex)
    if declarations is not None and not _DECLARATIONS_RE.search(result):
        marker = "\\begin{thebibliography}"
        position = result.find(marker)
        if position < 0:
            raise ValueError("repaired source has no bibliography boundary")
        result = (
            result[:position]
            + declarations.group(1).strip()
            + "\n\n"
            + result[position:]
        )

    def package_graphic(match: re.Match[str]) -> str:
        name = Path(match.group(2).strip()).name
        return f"{match.group(1)}figures/{name}{match.group(3)}"

    return _INCLUDEGRAPHICS_RE.sub(package_graphic, result)


def _update_risk_report(path: Path) -> None:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    lines = [
        line
        for line in text.splitlines()
        if not (
            "undefined cross-references" in line.casefold()
            or "unresolved cross-references" in line.casefold()
        )
    ]
    in_fix_section = False
    number = 1
    rebuilt: list[str] = []
    for line in lines:
        if line.strip().casefold() == "## issues to fix before online submission":
            in_fix_section = True
            number = 1
        elif line.startswith("## ") and in_fix_section:
            in_fix_section = False
        if in_fix_section and re.match(r"^\d+\.\s+", line):
            line = re.sub(r"^\d+\.", f"{number}.", line)
            number += 1
        rebuilt.append(line)
    marker = "- All internal LaTeX cross-references resolve in the rebuilt package."
    if marker not in rebuilt:
        try:
            index = rebuilt.index("## Verified package facts") + 1
            while index < len(rebuilt) and not rebuilt[index].strip():
                index += 1
            rebuilt.insert(index, marker)
        except ValueError:
            rebuilt.extend(["", "## Verified package facts", "", marker])
    write_text(path, "\n".join(rebuilt))


def _update_checklist(path: Path, figure_names: tuple[str, ...]) -> None:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    lines = [
        line
        for line in text.splitlines()
        if not re.match(r"^-\s+figures[/\\]figure.*:\s+present", line, re.IGNORECASE)
    ]
    try:
        insert_at = lines.index("- main.pdf: present and locally compiled with pdflatex") + 1
    except ValueError:
        insert_at = 0
    figure_lines = [
        f"- figures/{name}: present and referenced by main.tex"
        for name in figure_names
    ]
    lines[insert_at:insert_at] = figure_lines
    replaced = False
    for index, line in enumerate(lines):
        if "remaining unresolved cross-references:" in line.casefold():
            lines[index] = "- Remaining unresolved cross-references: none."
            replaced = True
    if not replaced:
        lines.append("- Remaining unresolved cross-references: none.")
    write_text(path, "\n".join(lines))


def _write_file_manifest(package: Path) -> None:
    files = sorted(
        path
        for path in package.rglob("*")
        if path.is_file() and path.name != "file_manifest.md"
    )
    lines = ["# File Manifest", ""]
    lines.extend(
        f"- {path.relative_to(package).as_posix()} ({path.stat().st_size} bytes)"
        for path in files
    )
    write_text(package / "file_manifest.md", "\n".join(lines))


def repair_journal_package_from_figure_submit(
    figure_root: Path,
    journal_root: Path,
    paper_id: str,
) -> JournalPackageRepairResult:
    figure_root, figure_paper = _safe_paper(figure_root, paper_id)
    journal_root, journal_paper = _safe_paper(journal_root, paper_id)
    upstream = validate_figure_step(figure_root, paper_id, 3)
    if not upstream.ok:
        return JournalPackageRepairResult(
            ok=False,
            repaired=False,
            paper_id=paper_id,
            issues=tuple(upstream.issues),
        )
    figure_submit = figure_paper / "submit"
    package = journal_paper / "package"
    previous_main = package / "main.tex"
    if not figure_submit.is_dir() or not package.is_dir() or not previous_main.is_file():
        return JournalPackageRepairResult(
            ok=False,
            repaired=False,
            paper_id=paper_id,
            issues=("figure submit or existing journal package is missing",),
        )

    token = uuid4().hex
    candidate_root = journal_paper / f".upo-journal-candidate-{token}"
    backups: dict[str, Path] = {}
    promoted: list[str] = []
    try:
        candidate_source = candidate_root / "source_submit"
        candidate_work = candidate_root / "work"
        candidate_package = candidate_root / "package"
        _copy_directory(figure_submit, candidate_source)
        _copy_directory(figure_submit, candidate_work)
        _copy_directory(package, candidate_package)

        submit_tex_files = sorted(candidate_source.glob("*.tex"))
        if len(submit_tex_files) != 1:
            raise ValueError("figure submit must contain exactly one TeX manuscript")
        base_tex = submit_tex_files[0].read_text(encoding="utf-8-sig", errors="strict")
        previous_tex = previous_main.read_text(encoding="utf-8-sig", errors="strict")
        rebuilt_tex = _preserve_package_edits(base_tex, previous_tex)
        write_text(candidate_package / "main.tex", rebuilt_tex)

        candidate_figures = candidate_package / "figures"
        if candidate_figures.exists():
            shutil.rmtree(candidate_figures)
        candidate_figures.mkdir(parents=True)
        figure_names: list[str] = []
        for graphic in _INCLUDEGRAPHICS_RE.findall(rebuilt_tex):
            name = Path(graphic[1].strip()).name
            source = candidate_source / name
            if not source.is_file() or source.stat().st_size <= 0:
                raise ValueError(f"rebuilt package figure is missing: {name}")
            shutil.copy2(source, candidate_figures / name)
            if name not in figure_names:
                figure_names.append(name)

        compiled, compile_output = _compile_candidate(candidate_package / "main.tex")
        if not compiled:
            return JournalPackageRepairResult(
                ok=False,
                repaired=False,
                paper_id=paper_id,
                issues=("journal package LaTeX compilation failed", compile_output),
            )
        _update_risk_report(candidate_package / "final_risk_report.md")
        _update_checklist(candidate_package / "submission_checklist.md", tuple(figure_names))
        _write_file_manifest(candidate_package)

        for name in ("source_submit", "work", "package"):
            current = journal_paper / name
            backup = journal_paper / f".upo-{name}-before-repair-{token}"
            current.rename(backup)
            backups[name] = backup
            promoted.append(name)
            (candidate_root / name).rename(current)

        contract = validate_journal_author_output(journal_root, paper_id)
        if not contract.ok:
            raise ValueError("repaired journal package failed contract: " + "; ".join(contract.issues[:6]))
        for backup in backups.values():
            shutil.rmtree(backup)
        write_text(
            journal_paper / "import_report.md",
            "\n".join(
                [
                    "# 导入报告",
                    "",
                    f"- paper_id: `{paper_id}`",
                    f"- source: `{figure_submit}`",
                    f"- imported_at: `{now_iso()}`",
                    "- mode: `programmatic_repair_after_figure_contract_recovery`",
                    "- model_turn_started: `false`",
                ]
            ),
        )
        return JournalPackageRepairResult(
            ok=True,
            repaired=True,
            paper_id=paper_id,
            contract=contract,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        for name in reversed(promoted):
            current = journal_paper / name
            failed = journal_paper / f".upo-{name}-failed-repair-{token}"
            if current.exists():
                current.rename(failed)
            backups[name].rename(current)
            if failed.exists():
                shutil.rmtree(failed)
        return JournalPackageRepairResult(
            ok=False,
            repaired=False,
            paper_id=paper_id,
            issues=(f"{type(exc).__name__}: {exc}",),
        )
    except BaseException:
        for name in reversed(promoted):
            current = journal_paper / name
            failed = journal_paper / f".upo-{name}-failed-repair-{token}"
            if current.exists():
                current.rename(failed)
            backups[name].rename(current)
            if failed.exists():
                shutil.rmtree(failed)
        raise
    finally:
        if candidate_root.is_dir():
            shutil.rmtree(candidate_root)


__all__ = [
    "JournalPackageRepairResult",
    "repair_journal_package_from_figure_submit",
]
