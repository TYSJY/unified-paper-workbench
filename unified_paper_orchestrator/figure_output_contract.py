from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .io_utils import read_json


_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
_REFERENCE_RE = re.compile(
    r"\\(?:ref|eqref|pageref|autoref|cref|Cref|vref|Vref)\*?\{([^}]+)\}"
)
_TABLE_ENV_RE = re.compile(r"\\begin\{table\*?\}")
_SECTION_RE = re.compile(r"\\(?:section|subsection|subsubsection)\*?\{")
_UNDEFINED_REFERENCE_RE = re.compile(
    r"(?:LaTeX Warning:\s+Reference\s+[`'].+?[`']\s+on page .+? undefined|"
    r"There were undefined references)",
    re.IGNORECASE,
)
_CONCEPTUAL_TYPES = {"conceptual_schematic", "conceptual", "schematic"}
_HYBRID_TYPES = {"hybrid", "hybrid_figure"}
_QUANTITATIVE_TYPES = {
    "quantitative",
    "quantitative_data_figure",
    "data_figure",
    "placeholder_data_figure",
}


@dataclass(frozen=True, slots=True)
class FigureContractResult:
    step: int
    paper_id: str
    status: str
    ok: bool
    issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    figure_count: int = 0
    data_requirement_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "paper_id": self.paper_id,
            "status": self.status,
            "ok": self.ok,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "artifacts": [dict(item) for item in self.artifacts],
            "figure_count": self.figure_count,
            "data_requirement_count": self.data_requirement_count,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    try:
        label = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        label = str(path)
    return {
        "path": label,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": sha256_file(path),
    }


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _safe_manifest_path(root: Path, value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _collect_artifacts(paths: Iterable[Path], root: Path) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not _nonempty_file(path):
            continue
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(_artifact(path, root))
    return tuple(sorted(result, key=lambda item: str(item.get("path") or "").casefold()))


def _step1_contract(root: Path, paper_id: str) -> FigureContractResult:
    paper = root / "papers" / paper_id
    board = paper / "reference_board.md"
    issues: list[str] = []
    warnings: list[str] = []
    if not _nonempty_file(board):
        issues.append("reference_board.md 缺失或为空")
    supporting = [paper / "reference_images_manifest.md", paper / "missing_reference_images.md"]
    if not any(_nonempty_file(path) for path in supporting):
        warnings.append("未找到论文级参考截图清单或缺图说明（提示词允许截图缺失）")
    artifacts = _collect_artifacts([board, *supporting], root)
    return FigureContractResult(
        step=1,
        paper_id=paper_id,
        status="complete" if not issues else "missing",
        ok=not issues,
        issues=tuple(issues),
        warnings=tuple(warnings),
        artifacts=artifacts,
    )


def _step2_contract(root: Path, paper_id: str) -> FigureContractResult:
    paper = root / "papers" / paper_id
    plan = paper / "figure_plan.md"
    prompts = paper / "figure_prompts.md"
    briefs = sorted((paper / "drafts").glob("figure*_brief.md")) if (paper / "drafts").is_dir() else []
    issues: list[str] = []
    if not _nonempty_file(plan):
        issues.append("figure_plan.md 缺失或为空")
    if not _nonempty_file(prompts):
        issues.append("figure_prompts.md 缺失或为空")
    if not any(_nonempty_file(path) for path in briefs):
        issues.append("drafts/figure*_brief.md 缺失")
    artifacts = _collect_artifacts([plan, prompts, *briefs], root)
    return FigureContractResult(
        step=2,
        paper_id=paper_id,
        status="complete" if not issues else "missing",
        ok=not issues,
        issues=tuple(issues),
        artifacts=artifacts,
        figure_count=len([path for path in briefs if _nonempty_file(path)]),
    )


def _resolve_includegraphics(submit_tex: Path, paper_dir: Path, value: str) -> Path | None:
    raw = value.strip()
    candidates = [submit_tex.parent / raw, paper_dir / raw]
    if not Path(raw).suffix:
        candidates.extend(path.with_suffix(ext) for path in list(candidates) for ext in (".pdf", ".png", ".jpg"))
    for candidate in candidates:
        if _nonempty_file(candidate):
            return candidate.resolve()
    return None


def _manifest_labels(value: Any) -> tuple[str, ...]:
    labels = [
        label.strip()
        for label in re.split(r"[,;]", str(value or ""))
        if label.strip()
    ]
    return tuple(dict.fromkeys(labels))


def _reference_targets(tex: str) -> set[str]:
    targets: set[str] = set()
    for raw in _REFERENCE_RE.findall(tex):
        for target in re.split(r"[,;]", raw):
            normalized = target.strip()
            if normalized:
                targets.add(normalized)
    return targets


def _step3_contract(root: Path, paper_id: str, *, allow_data_requirements: bool = False) -> FigureContractResult:
    paper = root / "papers" / paper_id
    submit = paper / "submit"
    manifest_path = submit / "submission_manifest.json"
    issues: list[str] = []
    warnings: list[str] = []
    artifacts: list[Path] = [manifest_path]
    figure_count = 0
    requirement_count = 0

    if not _nonempty_file(manifest_path):
        return FigureContractResult(
            step=3,
            paper_id=paper_id,
            status="missing",
            ok=False,
            issues=("submit/submission_manifest.json 缺失或为空",),
        )
    try:
        manifest = read_json(manifest_path, {})
    except Exception as exc:
        return FigureContractResult(
            step=3,
            paper_id=paper_id,
            status="invalid",
            ok=False,
            issues=(f"submission_manifest.json 无法解析：{type(exc).__name__}",),
            artifacts=_collect_artifacts(artifacts, root),
        )
    if not isinstance(manifest, dict):
        manifest = {}
        issues.append("submission_manifest.json 顶层必须是对象")
    if str(manifest.get("paper_id") or "").strip() != paper_id:
        issues.append("manifest.paper_id 与论文目录不一致")

    submit_tex = _safe_manifest_path(root, manifest.get("submit_tex"))
    compiled_pdf = _safe_manifest_path(root, manifest.get("compiled_pdf"))
    if submit_tex is None or not _nonempty_file(submit_tex):
        issues.append("manifest.submit_tex 缺失、越界或文件为空")
    else:
        artifacts.append(submit_tex)
    if compiled_pdf is None or not _nonempty_file(compiled_pdf):
        issues.append("manifest.compiled_pdf 缺失、越界或文件为空")
    else:
        artifacts.append(compiled_pdf)
    compile_status = str(manifest.get("compile_status") or "").strip()
    validation = manifest.get("validation")
    if not compile_status and isinstance(validation, Mapping):
        compile_status = str(validation.get("compile_status") or "").strip()
        if compile_status:
            warnings.append("manifest.compile_status 位于 validation 子对象；已按兼容格式验收")
    if compile_status.casefold() != "success":
        issues.append("manifest.compile_status 不是 success")
    if manifest.get("compiled_pdf_created") is not True:
        issues.append("manifest.compiled_pdf_created 不是 true")

    raw_figures = manifest.get("figures", [])
    if not isinstance(raw_figures, list) or not raw_figures:
        issues.append("manifest.figures 为空")
        raw_figures = []
    represented_pdfs: set[str] = set()
    final_paths: list[Path] = []
    manifest_figure_labels: dict[str, str] = {}
    manifest_figures: list[tuple[str, Path | None, tuple[str, ...]]] = []
    for index, raw_entry in enumerate(raw_figures, start=1):
        if not isinstance(raw_entry, Mapping):
            issues.append(f"figures[{index}] 不是对象")
            continue
        figure_count += 1
        name = str(raw_entry.get("figure") or f"Figure {index}").strip()
        figure_type = str(raw_entry.get("figure_type") or raw_entry.get("type") or "").strip().casefold()
        renderer = str(raw_entry.get("renderer") or "").strip().casefold()
        final_pdf = _safe_manifest_path(root, raw_entry.get("final_pdf"))
        labels = _manifest_labels(raw_entry.get("label"))
        for label in labels:
            manifest_figure_labels[label] = name
        if final_pdf is None or final_pdf.suffix.casefold() != ".pdf" or not _nonempty_file(final_pdf):
            issues.append(f"{name} 的 final_pdf 缺失、越界、非 PDF 或为空")
        else:
            final_paths.append(final_pdf)
            artifacts.append(final_pdf)
            represented_pdfs.add(str(final_pdf.resolve()).casefold())
        manifest_figures.append((name, final_pdf, labels))
        if raw_entry.get("inserted_in_tex") is not True:
            issues.append(f"{name} 未声明 inserted_in_tex=true")

        conceptual = figure_type in _CONCEPTUAL_TYPES
        if figure_type == "technical_diagram":
            source_file = _safe_manifest_path(root, raw_entry.get("source_file"))
            if renderer not in {"graphviz", "mermaid", "plantuml", "tikz", "svg"}:
                issues.append(f"{name} 的工程/框架图须使用可编辑的结构图渲染器")
            if source_file is None or not _nonempty_file(source_file) or source_file.suffix.lower() not in {".dot", ".mmd", ".puml", ".tex", ".svg"}:
                issues.append(f"{name} 缺少可编辑的结构图源文件")
            else:
                artifacts.append(source_file)
        hybrid = figure_type in _HYBRID_TYPES
        quantitative = figure_type in _QUANTITATIVE_TYPES
        if conceptual and renderer != "image2":
            issues.append(f"{name} 是概念图，但 renderer 不是 image2")
        if quantitative and renderer == "image2":
            issues.append(f"{name} 是定量图，却使用 image2")
        if hybrid and "image2" not in renderer and renderer not in {"mixed", "hybrid"}:
            issues.append(f"{name} 是混合图，但 manifest 未体现 image2 概念面板")
        if conceptual or hybrid:
            raw_output = _safe_manifest_path(root, raw_entry.get("raw_output"))
            if raw_output is None or raw_output.suffix.casefold() != ".png" or not _nonempty_file(raw_output):
                issues.append(f"{name} 缺少有效的原始 image2 PNG")
            else:
                artifacts.append(raw_output)
        if not figure_type:
            warnings.append(f"{name} 未声明 figure_type/type")
        if not renderer:
            issues.append(f"{name} 未声明 renderer")

    actual_figure_pdfs = sorted(path.resolve() for path in submit.glob("figure*.pdf") if _nonempty_file(path))
    for path in actual_figure_pdfs:
        if str(path).casefold() not in represented_pdfs:
            issues.append(f"提交目录中的 {path.name} 未写入 manifest.figures")

    raw_requirements = manifest.get("data_requirements", [])
    if isinstance(raw_requirements, list):
        requirement_count = len(raw_requirements)
    elif raw_requirements:
        requirement_count = 1
        issues.append("manifest.data_requirements 必须是数组")

    tex = ""
    references: list[str] = []
    if submit_tex is not None and _nonempty_file(submit_tex):
        try:
            tex = submit_tex.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            tex = ""
        references = _INCLUDEGRAPHICS_RE.findall(tex)
        if not references and final_paths:
            issues.append("提交 TeX 未找到任何 includegraphics")
        for reference in references:
            if _resolve_includegraphics(submit_tex, paper, reference) is None:
                issues.append(f"提交 TeX 引用的图片不存在：{reference}")

    if submit_tex is not None and _nonempty_file(submit_tex):
        resolved_graphics = {
            str(resolved).casefold()
            for reference in references
            if (resolved := _resolve_includegraphics(submit_tex, paper, reference)) is not None
        }
        output_labels = {label.strip() for label in _LABEL_RE.findall(tex) if label.strip()}
        unresolved_targets = sorted(_reference_targets(tex) - output_labels, key=str.casefold)
        if unresolved_targets:
            issues.append(
                "提交 TeX 存在未解析交叉引用：" + ", ".join(unresolved_targets[:12])
            )
        missing_manifest_labels = sorted(
            set(manifest_figure_labels) - output_labels,
            key=str.casefold,
        )
        if missing_manifest_labels:
            issues.append(
                "manifest 图标签没有真实写入提交 TeX："
                + ", ".join(missing_manifest_labels[:12])
            )
        for name, final_pdf, _labels in manifest_figures:
            if (
                final_pdf is not None
                and _nonempty_file(final_pdf)
                and str(final_pdf.resolve()).casefold() not in resolved_graphics
            ):
                issues.append(f"{name} 的 final_pdf 没有被提交 TeX 实际引用")

        raw_tables = manifest.get("tables", [])
        manifest_table_labels = (
            {
                label
                for raw_label in raw_tables
                for label in _manifest_labels(
                    raw_label.get("label") if isinstance(raw_label, Mapping) else raw_label
                )
            }
            if isinstance(raw_tables, list)
            else set()
        )
        missing_table_labels = sorted(manifest_table_labels - output_labels, key=str.casefold)
        if missing_table_labels:
            issues.append(
                "manifest 表格标签没有真实写入提交 TeX："
                + ", ".join(missing_table_labels[:12])
            )

        source_tex = paper / "input.tex"
        if _nonempty_file(source_tex):
            try:
                source = source_tex.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                source = ""
            source_labels = {label.strip() for label in _LABEL_RE.findall(source) if label.strip()}
            missing_source_labels = sorted(source_labels - output_labels, key=str.casefold)
            if missing_source_labels:
                issues.append(
                    "画图后提交 TeX 丢失原论文标签："
                    + ", ".join(missing_source_labels[:12])
                )
            source_tables = len(_TABLE_ENV_RE.findall(source))
            output_tables = len(_TABLE_ENV_RE.findall(tex))
            if output_tables < source_tables:
                issues.append(
                    f"画图后 table 环境异常减少：原文 {source_tables}，提交包 {output_tables}"
                )
            source_sections = len(_SECTION_RE.findall(source))
            output_sections = len(_SECTION_RE.findall(tex))
            if output_sections < source_sections:
                issues.append(
                    f"画图后章节结构异常减少：原文 {source_sections}，提交包 {output_sections}"
                )

        compile_log = compiled_pdf.with_suffix(".log") if compiled_pdf is not None else None
        if compile_log is not None and _nonempty_file(compile_log):
            try:
                log_text = compile_log.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                log_text = ""
            if _UNDEFINED_REFERENCE_RE.search(log_text):
                issues.append("编译日志仍包含 undefined references")
            artifacts.append(compile_log)
        elif compiled_pdf is not None and _nonempty_file(compiled_pdf):
            warnings.append("未找到编译日志，无法复核 undefined references")

    if compiled_pdf is not None and _nonempty_file(compiled_pdf):
        dependencies = [path for path in [submit_tex, *final_paths] if path is not None and _nonempty_file(path)]
        try:
            newest_dependency = max((path.stat().st_mtime_ns for path in dependencies), default=0)
            if compiled_pdf.stat().st_mtime_ns < newest_dependency:
                issues.append("编译 PDF 早于提交 TeX 或最终图文件，需要重新编译")
        except OSError:
            issues.append("无法验证编译 PDF 的新鲜度")

    status = "complete"
    ok = not issues and (requirement_count == 0 or allow_data_requirements)
    if issues:
        status = "invalid"
    elif requirement_count and not allow_data_requirements:
        status = "waiting_data"
        warnings.append(f"仍有 {requirement_count} 项真实实验数据需求")
    elif requirement_count:
        warnings.append(f"本次仅验收单图修改；提交包仍有 {requirement_count} 项真实数据需求")
    return FigureContractResult(
        step=3,
        paper_id=paper_id,
        status=status,
        ok=ok,
        issues=tuple(dict.fromkeys(issues)),
        warnings=tuple(dict.fromkeys(warnings)),
        artifacts=_collect_artifacts(artifacts, root),
        figure_count=figure_count,
        data_requirement_count=requirement_count,
    )


def validate_figure_step(root: Path, paper_id: str, step: int, *, workflow: dict | None = None) -> FigureContractResult:
    root = Path(root).expanduser().resolve()
    paper_id = str(paper_id or "").strip()
    if not paper_id or Path(paper_id).name != paper_id or paper_id in {".", ".."}:
        return FigureContractResult(
            step=int(step),
            paper_id=paper_id,
            status="invalid",
            ok=False,
            issues=("paper_id 非法",),
        )
    if int(step) == 1:
        return _step1_contract(root, paper_id)
    if int(step) == 2:
        return _step2_contract(root, paper_id)
    if int(step) in {3, 4}:
        result = _step3_contract(root, paper_id, allow_data_requirements=int(step) == 4)
        from .undergraduate_word import is_word_workflow, paper_workflow, validate_word_delivery
        context = workflow if workflow is not None else paper_workflow(root, paper_id)
        if is_word_workflow(context):
            issues, paths = validate_word_delivery(root, paper_id, context)
            result = replace(result, ok=result.ok and not issues,
                             status="invalid" if issues else result.status,
                             issues=(*result.issues, *issues),
                             artifacts=(*result.artifacts, *_collect_artifacts(paths, root)))
        return result
    return FigureContractResult(
        step=int(step),
        paper_id=paper_id,
        status="invalid",
        ok=False,
        issues=(f"不支持的画图 Step：{step}",),
    )


def highest_valid_figure_step(root: Path, paper_id: str) -> tuple[int, FigureContractResult | None]:
    last: FigureContractResult | None = None
    highest = 0
    for step in (1, 2, 3):
        result = validate_figure_step(root, paper_id, step)
        last = result
        if result.ok:
            highest = step
            continue
        if step == 1:
            # Step 1 is optional when a paper starts directly at Step 2.
            continue
        break
    return highest, last


__all__ = [
    "FigureContractResult",
    "highest_valid_figure_step",
    "sha256_file",
    "validate_figure_step",
]
