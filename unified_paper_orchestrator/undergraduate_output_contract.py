"""Undergraduate-specific mechanical checks; never an academic approval."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .writing_completion import expected_step_artifacts, fingerprint_step_artifacts
from .writing_novelty_gate import _parse_bib_records, _parse_markdown_reference_records, _match_verified_records
from .writing_output_contract import _validate_latex, _BIBITEM_RE, _citation_keys, _strip_latex_comments
from .writing_prompt_schemes import normalize_thesis_brief, scheme_id
from .undergraduate_verification import uses_undergraduate_verification, validate_verification_draft

_GAPS = re.compile(r"待补充|待核验|待验证|待确认|SIMULATED_TO_REPLACE|\bTODO\b|\bTBD\b", re.I)


def validate_undergraduate_output(root: Path | str, papers_dir: Path | str, paper_id: str, step: int,
                                  workflow: Mapping[str, Any], *, is_terminal: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"paper_id": paper_id, "step": step, "prompt_scheme": scheme_id(workflow),
                             "status": "BLOCKED", "ready": False, "issues": [], "warnings": [], "counts": {}}
    issues = result["issues"]

    def fail(code: str, message: str) -> None:
        issues.append({"code": code, "message": message})

    try:
        brief = normalize_thesis_brief(workflow.get("thesis_brief"), required=True)
    except ValueError as exc:
        fail("undergraduate_brief_missing", str(exc))
        return result
    paths = expected_step_artifacts(Path(root), Path(papers_dir), paper_id, step)
    baseline = fingerprint_step_artifacts(Path(root), Path(papers_dir), paper_id, step)
    if not paths or not baseline:
        fail("undergraduate_output_missing", "本步输出缺失、为空或路径不安全")
        return result
    try:
        texts = [path.read_text(encoding="utf-8-sig") for path in paths]
    except (OSError, UnicodeError):
        fail("undergraduate_output_unreadable", "无法读取本步输出")
        return result
    result["path"] = str(paths[0])
    minimum = brief["minimum_references"] or 1
    verification = uses_undergraduate_verification(workflow)
    text = texts[0]
    if step == 1:
        for heading in ("用户给定题目", "学校要求清单", "待确认项"):
            if not re.search(rf"(?m)^##\s+{heading}\s*$", text):
                fail("undergraduate_brief_heading_missing", f"第一步缺少：{heading}")
        if brief["title"] not in text:
            fail("undergraduate_title_changed", "第一步必须保留用户给定题目原文")
    elif step == 2:
        records, errors = _parse_bib_records(texts[1])
        rows = _parse_markdown_reference_records(texts[0])
        matched, verified = _match_verified_records(rows, records)
        keys = [item["key"] for item in records]
        result["counts"].update(reference_count=len(records), verified_reference_count=verified)
        if errors or len(set(keys)) != len(keys):
            fail("undergraduate_bibtex_invalid", "BibTeX 结构错误或引用键重复")
        if len(records) < minimum:
            fail("undergraduate_references_insufficient", f"需要至少 {minimum} 条有效参考文献，当前 {len(records)} 条")
        if matched != len(records) or verified != len(records):
            fail("undergraduate_references_unverified", "有效 BibTeX 条目必须与逐条核验的来源记录一一对应")
        if any(row.get("author_placeholder") for row in rows if row.get("verification") == "verified"):
            fail("undergraduate_reference_author_missing", "已核验文献仍缺少真实作者信息")
        if any(not item.strip() for item in texts):
            fail("undergraduate_reference_artifact_empty", "文献库、文献图谱和定位说明均不能为空")
    elif step == 7:
        for heading in ("自查结论", "主要问题", "修改建议"):
            if not re.search(rf"(?m)^##\s+{heading}\s*$", text):
                fail("undergraduate_review_heading_missing", f"自查意见缺少：{heading}")
    else:
        _validate_latex(text, step, issues, result["warnings"], result["counts"], result,
                        is_terminal=is_terminal, minimums=(1, minimum, 0, 0))
        if verification and step >= 5:
            # Undergraduate papers need one block per actual data figure/table,
            # not the journal's fixed minimum of three simulated experiments.
            issues[:] = [issue for issue in issues if issue["code"] != "latex_verify_data_contract_invalid"]
            validate_verification_draft(text, step, papers_dir, paper_id, issues, result["counts"])
            if step == 5 and is_terminal:
                fail("undergraduate_verification_step_required", "带核验协议的本科流程不能在第5步作为终稿交付，请完成数据核验和第6步")
        source = _strip_latex_comments(text)
        # Both book-style chapters and article-style sections count as structure.
        if re.search(r"\\chapter\*?\s*\{", source):
            issues[:] = [issue for issue in issues if issue["code"] != "latex_sections_insufficient"]
        if re.search(r"SIMULATED_TO_REPLACE|VERIFY_DATA_START", text, re.I) and not (verification and step == 5 and not is_terminal):
            fail("undergraduate_simulated_evidence", "本科稿件不能沿用期刊模拟数据占位块")
        if (step == 6 or is_terminal) and _GAPS.search(source):
            fail("undergraduate_evidence_gaps", "稿件仍有待补充/核验事项，请补齐真实材料后继续，不能删除标记假装完成")
        try:
            bib, errors = _parse_bib_records((Path(root) / "refs" / paper_id / "references.bib").read_text(encoding="utf-8-sig"))
            approved_keys = {item["key"] for item in bib}
            draft_keys = set(_BIBITEM_RE.findall(source)) | _citation_keys(source)
            if errors or not draft_keys.issubset(approved_keys):
                fail("undergraduate_citation_not_in_library", "正文或书目使用了不在本论文文献库中的引用键")
        except (OSError, UnicodeError):
            fail("undergraduate_reference_library_missing", "无法读取本论文的参考文献库")
    if fingerprint_step_artifacts(Path(root), Path(papers_dir), paper_id, step) != baseline:
        fail("undergraduate_output_changed", "输出在检查期间发生变化")
    if not issues:
        result.update(ready=True, status="READY")
    result["warnings"].append({"code": "undergraduate_human_review_required",
                               "message": "这是文件、引用对应及缺口检查；学校排版、学术质量和来源真实性仍需人工复核，不代表学校验收。"})
    return result
