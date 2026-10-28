from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .figure_output_contract import FigureContractResult, validate_figure_step
from .io_utils import now_iso, read_json, write_json_atomic
from .windows_subprocess import no_window_subprocess_kwargs


_BEGIN_FIGURE_RE = re.compile(r"\\begin\{figure\*?\}(?:\[[^\]]*\])?")
_END_FIGURE_RE = re.compile(r"\\end\{figure\*?\}")
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
_PLANNED_FIGURE_NUMBER_RE = re.compile(
    r"\bFig(?:ure)?\.?\s*(\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FigureRepairResult:
    ok: bool
    repaired: bool
    paper_id: str
    issues: tuple[str, ...] = ()
    contract: FigureContractResult | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "repaired": self.repaired,
            "paper_id": self.paper_id,
            "issues": list(self.issues),
            "contract": self.contract.as_dict() if self.contract is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FigureSubmissionCheckpoint:
    paper_id: str
    path: Path
    had_submit: bool

    def as_dict(self, root: Path) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "path": self.path.resolve().relative_to(root.resolve()).as_posix(),
            "had_submit": self.had_submit,
        }


@dataclass(frozen=True, slots=True)
class _FigureEnvironment:
    start: int
    end: int
    labels: tuple[str, ...]


def _safe_paper(root: Path, paper_id: str) -> tuple[Path, Path]:
    root = Path(root).expanduser().resolve()
    paper_id = str(paper_id or "").strip()
    if not paper_id or Path(paper_id).name != paper_id or paper_id in {".", ".."}:
        raise ValueError("invalid paper_id")
    paper = (root / "papers" / paper_id).resolve()
    paper.relative_to((root / "papers").resolve())
    return root, paper


def _manifest_labels(value: Any) -> tuple[str, ...]:
    labels = [
        label.strip()
        for label in re.split(r"[,;]", str(value or ""))
        if label.strip()
    ]
    return tuple(dict.fromkeys(labels))


def _figure_environments(tex: str) -> tuple[_FigureEnvironment, ...]:
    environments: list[_FigureEnvironment] = []
    cursor = 0
    while True:
        begin = _BEGIN_FIGURE_RE.search(tex, cursor)
        if begin is None:
            break
        end = _END_FIGURE_RE.search(tex, begin.end())
        if end is None:
            raise ValueError("source TeX has an unterminated figure environment")
        stop = end.end()
        labels = tuple(
            dict.fromkeys(
                label.strip()
                for label in _LABEL_RE.findall(tex[begin.start() : stop])
                if label.strip()
            )
        )
        environments.append(_FigureEnvironment(begin.start(), stop, labels))
        cursor = stop
    return tuple(environments)


def _ensure_graphicx(tex: str) -> str:
    if re.search(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\bgraphicx\b[^}]*\}", tex):
        return tex
    documentclass = re.search(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}\s*", tex)
    if documentclass is None:
        raise ValueError("source TeX has no documentclass")
    return tex[: documentclass.end()] + "\\usepackage{graphicx}\n" + tex[documentclass.end() :]


def _replacement_environment(final_pdf: Path, caption: str, labels: tuple[str, ...]) -> str:
    label_lines = "\n".join(f"\\label{{{label}}}" for label in labels)
    return (
        "\\begin{figure}[t]\n"
        "\\centering\n"
        f"\\includegraphics[width=\\linewidth]{{{final_pdf.name}}}\n"
        f"\\caption{{{caption.strip()}}}\n"
        f"{label_lines}\n"
        "\\end{figure}"
    )


def _build_repaired_tex(
    source: str,
    figures: list[Mapping[str, Any]],
    root: Path,
) -> tuple[str, tuple[Path, ...]]:
    environments = _figure_environments(source)
    by_label: dict[str, _FigureEnvironment] = {}
    for environment in environments:
        for label in environment.labels:
            if label in by_label:
                raise ValueError(f"source TeX contains duplicate figure label {label}")
            by_label[label] = environment

    replacements: dict[int, tuple[int, str]] = {}
    final_pdfs: list[Path] = []
    for index, entry in enumerate(figures, start=1):
        labels = _manifest_labels(entry.get("label"))
        if not labels:
            raise ValueError(f"manifest Figure {index} has no label")
        matched: list[_FigureEnvironment] = []
        for label in labels:
            environment = by_label.get(label)
            if environment is None:
                raise ValueError(f"source TeX has no figure environment for label {label}")
            if environment not in matched:
                matched.append(environment)
        raw_pdf = str(entry.get("final_pdf") or "").strip()
        final_pdf = Path(raw_pdf)
        if not final_pdf.is_absolute():
            final_pdf = root / final_pdf
        final_pdf = final_pdf.resolve()
        final_pdf.relative_to(root)
        if final_pdf.suffix.casefold() != ".pdf" or not final_pdf.is_file() or final_pdf.stat().st_size <= 0:
            raise ValueError(f"manifest Figure {index} final_pdf is missing")
        caption = str(entry.get("caption") or entry.get("figure") or f"Figure {index}").strip()
        first = min(matched, key=lambda item: item.start)
        for environment in matched:
            if environment.start in replacements:
                raise ValueError("one source figure environment is claimed by multiple manifest entries")
            replacement = (
                _replacement_environment(final_pdf, caption, labels)
                if environment == first
                else ""
            )
            replacements[environment.start] = (environment.end, replacement)
        final_pdfs.append(final_pdf)

    repaired = source
    for start, (end, replacement) in sorted(replacements.items(), reverse=True):
        repaired = repaired[:start] + replacement + repaired[end:]
    return _ensure_graphicx(repaired), tuple(final_pdfs)


def _manifest_final_pdfs(
    figures: list[Mapping[str, Any]],
    root: Path,
) -> tuple[Path, ...]:
    final_pdfs: list[Path] = []
    for index, entry in enumerate(figures, start=1):
        raw_pdf = str(entry.get("final_pdf") or "").strip()
        final_pdf = Path(raw_pdf)
        if not final_pdf.is_absolute():
            final_pdf = root / final_pdf
        final_pdf = final_pdf.resolve()
        final_pdf.relative_to(root)
        if (
            final_pdf.suffix.casefold() != ".pdf"
            or not final_pdf.is_file()
            or final_pdf.stat().st_size <= 0
        ):
            raise ValueError(f"manifest Figure {index} final_pdf is missing")
        final_pdfs.append(final_pdf)
    return tuple(final_pdfs)


def _planned_figure_numbers(plan: str, label: str) -> set[int]:
    numbers: set[int] = set()
    for line in plan.splitlines():
        if label not in line:
            continue
        numbers.update(
            int(raw)
            for raw in _PLANNED_FIGURE_NUMBER_RE.findall(line)
            if int(raw) > 0
        )
    return numbers


def _graft_planned_source_figure_labels(
    source: str,
    generated: str,
    figure_plan: str,
) -> tuple[str, dict[str, int]]:
    """Restore only explicitly planned labels lost by a figure merge.

    Step 2 may legitimately merge several source figure environments into one
    final figure and may also introduce a new teaser figure. Rebuilding the
    submission from the source therefore rejects valid new labels such as
    ``fig:hero``. This bounded repair keeps the generated TeX intact and only
    restores source labels whose destination Figure number is explicitly
    recorded in ``figure_plan.md``.
    """

    source_environments = _figure_environments(source)
    output_environments = _figure_environments(generated)
    source_figure_labels = {
        label
        for environment in source_environments
        for label in environment.labels
    }
    source_labels = {
        label.strip()
        for label in _LABEL_RE.findall(source)
        if label.strip()
    }
    output_labels = {
        label.strip()
        for label in _LABEL_RE.findall(generated)
        if label.strip()
    }
    missing_labels = sorted(source_labels - output_labels, key=str.casefold)
    if not missing_labels:
        return generated, {}
    non_figure_missing = [
        label for label in missing_labels if label not in source_figure_labels
    ]
    if non_figure_missing:
        raise ValueError(
            "generated TeX also lost non-figure labels: "
            + ", ".join(non_figure_missing[:12])
        )
    if not output_environments:
        raise ValueError("generated TeX has no figure environments")

    assignments: dict[str, int] = {}
    for label in missing_labels:
        planned_numbers = _planned_figure_numbers(figure_plan, label)
        if len(planned_numbers) != 1:
            raise ValueError(
                f"figure plan does not uniquely map source label {label}"
            )
        planned_number = next(iter(planned_numbers))

        anchored_indexes: set[int] = set()
        for index, environment in enumerate(output_environments, start=1):
            for output_label in environment.labels:
                if output_label not in source_figure_labels:
                    continue
                if planned_numbers & _planned_figure_numbers(
                    figure_plan,
                    output_label,
                ):
                    anchored_indexes.add(index)
        if len(anchored_indexes) == 1:
            target_index = next(iter(anchored_indexes))
        elif not anchored_indexes and planned_number <= len(output_environments):
            # Final Figure numbers are the explicit Step-2 plan order. This
            # fallback is needed when the merged figure uses a brand-new label.
            target_index = planned_number
        else:
            raise ValueError(
                f"figure plan cannot safely place source label {label}"
            )
        assignments[label] = target_index

    additions: dict[int, list[str]] = {}
    for label, target_index in assignments.items():
        additions.setdefault(target_index, []).append(label)
    repaired = generated
    for target_index in sorted(additions, reverse=True):
        environment = output_environments[target_index - 1]
        insertion = "\n".join(
            f"\\label{{{label}}}"
            for label in additions[target_index]
        )
        segment = repaired[environment.start : environment.end]
        end_match = _END_FIGURE_RE.search(segment)
        if end_match is None:
            raise ValueError("generated TeX has an unterminated figure environment")
        replacement = (
            segment[: end_match.start()]
            + insertion
            + "\n"
            + segment[end_match.start() :]
        )
        repaired = (
            repaired[: environment.start]
            + replacement
            + repaired[environment.end :]
        )
    return repaired, assignments


def _reference_targets(tex: str) -> set[str]:
    targets: set[str] = set()
    for raw in _REFERENCE_RE.findall(tex):
        targets.update(
            target.strip()
            for target in re.split(r"[,;]", raw)
            if target.strip()
        )
    return targets


def _validate_candidate(source: str, candidate_tex: Path, final_pdfs: tuple[Path, ...]) -> tuple[str, ...]:
    issues: list[str] = []
    tex = candidate_tex.read_text(encoding="utf-8-sig", errors="ignore")
    source_labels = {label.strip() for label in _LABEL_RE.findall(source) if label.strip()}
    output_labels = {label.strip() for label in _LABEL_RE.findall(tex) if label.strip()}
    missing_labels = sorted(source_labels - output_labels, key=str.casefold)
    if missing_labels:
        issues.append("candidate lost source labels: " + ", ".join(missing_labels[:12]))
    unresolved = sorted(_reference_targets(tex) - output_labels, key=str.casefold)
    if unresolved:
        issues.append("candidate has unresolved references: " + ", ".join(unresolved[:12]))
    source_tables = len(_TABLE_ENV_RE.findall(source))
    output_tables = len(_TABLE_ENV_RE.findall(tex))
    if output_tables < source_tables:
        issues.append(f"candidate table count dropped from {source_tables} to {output_tables}")
    source_sections = len(_SECTION_RE.findall(source))
    output_sections = len(_SECTION_RE.findall(tex))
    if output_sections < source_sections:
        issues.append(f"candidate section count dropped from {source_sections} to {output_sections}")
    graphics = {Path(value.strip()).name.casefold() for value in _INCLUDEGRAPHICS_RE.findall(tex)}
    for final_pdf in final_pdfs:
        if final_pdf.name.casefold() not in graphics:
            issues.append(f"candidate does not include {final_pdf.name}")
    log_path = candidate_tex.with_suffix(".log")
    if not log_path.is_file():
        issues.append("candidate compile log is missing")
    else:
        log_text = log_path.read_text(encoding="utf-8-sig", errors="ignore")
        if _UNDEFINED_REFERENCE_RE.search(log_text):
            issues.append("candidate compile log contains undefined references")
    pdf_path = candidate_tex.with_suffix(".pdf")
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        issues.append("candidate compiled PDF is missing")
    return tuple(dict.fromkeys(issues))


def _compile_candidate(candidate_tex: Path) -> tuple[bool, str]:
    for suffix in (".aux", ".log", ".out", ".pdf", ".fls", ".fdb_latexmk"):
        candidate_tex.with_suffix(suffix).unlink(missing_ok=True)
    latexmk = shutil.which("latexmk")
    latexmk_output = ""
    if latexmk:
        command = [
            latexmk,
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            candidate_tex.name,
        ]
        completed = subprocess.run(
            command,
            cwd=candidate_tex.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            check=False,
            **no_window_subprocess_kwargs(),
        )
        latexmk_output = (completed.stdout + "\n" + completed.stderr)[-6000:]
        if completed.returncode == 0:
            return True, latexmk_output

    pdflatex = shutil.which("pdflatex")
    if not pdflatex:
        return False, latexmk_output or "neither latexmk nor pdflatex is installed"
    output = ""
    for _ in range(3):
        completed = subprocess.run(
            [
                pdflatex,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                candidate_tex.name,
            ],
            cwd=candidate_tex.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            check=False,
            **no_window_subprocess_kwargs(),
        )
        output = (completed.stdout + "\n" + completed.stderr)[-6000:]
        if completed.returncode != 0:
            return False, (latexmk_output + "\n" + output)[-6000:]
    return True, (latexmk_output + "\n" + output)[-6000:]


def create_figure_submission_checkpoint(
    root: Path,
    paper_id: str,
    *,
    attempt_id: str = "",
) -> FigureSubmissionCheckpoint:
    root, paper = _safe_paper(root, paper_id)
    submit = paper / "submit"
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(attempt_id or uuid4().hex))[:96]
    checkpoint = paper / f".upo-submit-checkpoint-{token}"
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    had_submit = submit.is_dir()
    if had_submit:
        shutil.copytree(submit, checkpoint)
    else:
        checkpoint.mkdir(parents=True)
    return FigureSubmissionCheckpoint(paper_id=paper_id, path=checkpoint, had_submit=had_submit)


def _checkpoint_from_mapping(
    root: Path,
    paper_id: str,
    value: Mapping[str, Any] | FigureSubmissionCheckpoint,
) -> FigureSubmissionCheckpoint:
    if isinstance(value, FigureSubmissionCheckpoint):
        return value
    root, paper = _safe_paper(root, paper_id)
    raw_path = Path(str(value.get("path") or ""))
    path = raw_path if raw_path.is_absolute() else root / raw_path
    path = path.resolve()
    path.relative_to(paper)
    if path.parent != paper or not path.name.startswith(".upo-submit-checkpoint-"):
        raise ValueError("invalid figure submission checkpoint path")
    return FigureSubmissionCheckpoint(
        paper_id=paper_id,
        path=path,
        had_submit=bool(value.get("had_submit")),
    )


def restore_figure_submission_checkpoint(
    root: Path,
    paper_id: str,
    value: Mapping[str, Any] | FigureSubmissionCheckpoint,
    *,
    preserve_failed: bool = False,
) -> bool:
    _root, paper = _safe_paper(root, paper_id)
    checkpoint = _checkpoint_from_mapping(root, paper_id, value)
    if not checkpoint.path.is_dir():
        return False
    submit = paper / "submit"
    failed = paper / f".upo-failed-submit-{uuid4().hex}"
    if submit.exists():
        submit.rename(failed)
    try:
        if checkpoint.had_submit:
            checkpoint.path.rename(submit)
        else:
            checkpoint.path.rmdir()
        if failed.exists() and not preserve_failed:
            shutil.rmtree(failed)
        return True
    except BaseException:
        if not submit.exists() and failed.exists():
            failed.rename(submit)
        raise


def discard_figure_submission_checkpoint(
    root: Path,
    paper_id: str,
    value: Mapping[str, Any] | FigureSubmissionCheckpoint,
) -> None:
    checkpoint = _checkpoint_from_mapping(root, paper_id, value)
    if checkpoint.path.is_dir():
        shutil.rmtree(checkpoint.path)


def repair_figure_submission(root: Path, paper_id: str) -> FigureRepairResult:
    root, paper = _safe_paper(root, paper_id)
    source_path = paper / "input.tex"
    submit = paper / "submit"
    manifest_path = submit / "submission_manifest.json"
    if not source_path.is_file() or not manifest_path.is_file():
        return FigureRepairResult(
            ok=False,
            repaired=False,
            paper_id=paper_id,
            issues=("source input.tex or submission_manifest.json is missing",),
        )
    try:
        source = source_path.read_text(encoding="utf-8-sig", errors="strict")
        manifest = read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            raise ValueError("submission manifest is not an object")
        raw_figures = manifest.get("figures")
        if not isinstance(raw_figures, list) or not raw_figures:
            raise ValueError("submission manifest has no figures")
        figures = [entry for entry in raw_figures if isinstance(entry, Mapping)]
        if len(figures) != len(raw_figures):
            raise ValueError("submission manifest contains an invalid figure entry")
        raw_submit_tex = Path(
            str(manifest.get("submit_tex") or f"{paper_id}.tex")
        )
        current_submit_tex = submit / raw_submit_tex.name
        repair_mode = "bounded-latex-environment-replacement"
        label_assignments: dict[str, int] = {}
        graft_error = ""
        if current_submit_tex.is_file():
            generated = current_submit_tex.read_text(
                encoding="utf-8-sig",
                errors="strict",
            )
            figure_plan_path = paper / "figure_plan.md"
            figure_plan = (
                figure_plan_path.read_text(
                    encoding="utf-8-sig",
                    errors="strict",
                )
                if figure_plan_path.is_file()
                else ""
            )
            try:
                repaired_tex, label_assignments = (
                    _graft_planned_source_figure_labels(
                        source,
                        generated,
                        figure_plan,
                    )
                )
                if label_assignments:
                    final_pdfs = _manifest_final_pdfs(figures, root)
                    repair_mode = "bounded-planned-label-graft"
                else:
                    repaired_tex, final_pdfs = _build_repaired_tex(
                        source,
                        figures,
                        root,
                    )
            except ValueError as exc:
                graft_error = str(exc)
                repaired_tex, final_pdfs = _build_repaired_tex(
                    source,
                    figures,
                    root,
                )
        else:
            repaired_tex, final_pdfs = _build_repaired_tex(
                source,
                figures,
                root,
            )
    except (OSError, UnicodeError, ValueError) as exc:
        issue = str(exc)
        if "graft_error" in locals() and graft_error:
            issue = f"{graft_error}; fallback repair failed: {issue}"
        return FigureRepairResult(
            ok=False,
            repaired=False,
            paper_id=paper_id,
            issues=(issue,),
        )

    candidate = paper / f".upo-submit-candidate-{uuid4().hex}"
    backup = paper / f".upo-submit-before-repair-{uuid4().hex}"
    try:
        shutil.copytree(submit, candidate)
        candidate_tex = candidate / raw_submit_tex.name
        candidate_tex.write_text(repaired_tex, encoding="utf-8", newline="\n")
        for final_pdf in final_pdfs:
            target = candidate / final_pdf.name
            if final_pdf.resolve() != target.resolve():
                shutil.copy2(final_pdf, target)
        compiled, compile_output = _compile_candidate(candidate_tex)
        if not compiled:
            return FigureRepairResult(
                ok=False,
                repaired=False,
                paper_id=paper_id,
                issues=("candidate LaTeX compilation failed", compile_output),
            )
        candidate_issues = _validate_candidate(source, candidate_tex, final_pdfs)
        if candidate_issues:
            return FigureRepairResult(
                ok=False,
                repaired=False,
                paper_id=paper_id,
                issues=candidate_issues,
            )
        candidate_manifest_path = candidate / "submission_manifest.json"
        candidate_manifest = json.loads(
            candidate_manifest_path.read_text(encoding="utf-8-sig")
        )
        candidate_manifest["paper_id"] = paper_id
        candidate_manifest["source_tex"] = f"papers/{paper_id}/input.tex"
        candidate_manifest["submit_tex"] = (
            f"papers/{paper_id}/submit/{candidate_tex.name}"
        )
        candidate_manifest["compiled_pdf"] = (
            f"papers/{paper_id}/submit/{candidate_tex.with_suffix('.pdf').name}"
        )
        candidate_manifest["compile_status"] = "success"
        candidate_manifest["compiled_pdf_created"] = True
        candidate_manifest["compile_error"] = ""
        candidate_figures = candidate_manifest.get("figures")
        if isinstance(candidate_figures, list):
            for entry in candidate_figures:
                if isinstance(entry, dict):
                    # _validate_candidate has already proven that every
                    # manifest final_pdf is referenced by the candidate TeX.
                    entry["inserted_in_tex"] = True
        if label_assignments:
            if isinstance(candidate_figures, list):
                for label, target_index in label_assignments.items():
                    if target_index < 1 or target_index > len(candidate_figures):
                        continue
                    entry = candidate_figures[target_index - 1]
                    if not isinstance(entry, dict):
                        continue
                    labels = list(_manifest_labels(entry.get("label")))
                    if label not in labels:
                        labels.append(label)
                    entry["label"] = ", ".join(labels)
        candidate_manifest["programmatic_repair"] = {
            "schema": "unified-paper-orchestrator/figure-step3-repair/v1",
            "repaired_at": now_iso(),
            "source": f"papers/{paper_id}/input.tex",
            "mode": repair_mode,
            "model_turn_started": False,
        }
        write_json_atomic(candidate_manifest_path, candidate_manifest)

        submit.rename(backup)
        try:
            candidate.rename(submit)
        except BaseException:
            backup.rename(submit)
            raise
        contract = validate_figure_step(root, paper_id, 3)
        if not contract.ok:
            failed = paper / f".upo-failed-repair-{uuid4().hex}"
            submit.rename(failed)
            backup.rename(submit)
            shutil.rmtree(failed)
            return FigureRepairResult(
                ok=False,
                repaired=False,
                paper_id=paper_id,
                issues=tuple(contract.issues),
                contract=contract,
            )
        shutil.rmtree(backup)
        return FigureRepairResult(
            ok=True,
            repaired=True,
            paper_id=paper_id,
            contract=contract,
        )
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        if backup.is_dir():
            failed = paper / f".upo-failed-repair-{uuid4().hex}"
            if submit.exists():
                submit.rename(failed)
            backup.rename(submit)
            if failed.exists():
                shutil.rmtree(failed)
        return FigureRepairResult(
            ok=False,
            repaired=False,
            paper_id=paper_id,
            issues=(f"{type(exc).__name__}: {exc}",),
        )
    except BaseException:
        if backup.is_dir():
            failed = paper / f".upo-failed-repair-{uuid4().hex}"
            if submit.exists():
                submit.rename(failed)
            backup.rename(submit)
            if failed.exists():
                shutil.rmtree(failed)
        raise
    finally:
        if candidate.is_dir():
            shutil.rmtree(candidate)


__all__ = [
    "FigureRepairResult",
    "FigureSubmissionCheckpoint",
    "create_figure_submission_checkpoint",
    "discard_figure_submission_checkpoint",
    "repair_figure_submission",
    "restore_figure_submission_checkpoint",
]
