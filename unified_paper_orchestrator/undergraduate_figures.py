"""Paper-bound undergraduate figure prompts, separate from protected journal prompts."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from .writing_prompt_schemes import SCHEMES, is_undergraduate, normalize_thesis_brief, scheme_id


def figure_scheme_steps(identifier: str) -> list[dict]:
    if identifier not in SCHEMES or not is_undergraduate({"prompt_scheme": identifier}):
        raise ValueError("请选择本科配图方案")
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    source = (root / "prompts" / "undergraduate" / ("画图-" + SCHEMES[identifier][1])).read_text(encoding="utf-8-sig")
    matches = list(re.finditer(r"(?m)^## Step (\d+)｜([^\r\n]+)", source))
    if [int(m[1]) for m in matches] != [1, 2, 3]:
        raise ValueError("本科配图提示词三步结构损坏")
    result = []
    for index, match in enumerate(matches):
        text = source[:matches[0].start()].rstrip() + "\n\n" + source[match.start():matches[index+1].start() if index < 2 else len(source)].strip() + "\n"
        result.append({"name": match[2].strip(), "prompt_text": text,
                       "prompt_sha256": hashlib.sha256(text.encode()).hexdigest()})
    return result


def make_figure_snapshot(identifier: str, brief: dict, default_profile=None) -> dict:
    from .figure_workflow import default_figure_workflow_snapshot, normalize_figure_workflow_snapshot
    base = default_figure_workflow_snapshot()
    for step, content in zip(base["steps"], figure_scheme_steps(identifier)):
        step.update(content)
        step["prompt_path"] = f"scheme:{identifier}/figures/{step['source_step']:02d}"
    return normalize_figure_workflow_snapshot({"steps": base["steps"], "prompt_scheme": identifier,
        "thesis_brief": normalize_thesis_brief(brief, required=True), "created_at": "paper-bound",
        "default_execution_profile": default_profile or base["default_execution_profile"]})


def build_undergraduate_figure_prompt(workflow: dict, position: int, paper_id: str, source_tex: str, root: Path | None = None) -> str:
    from .figure_workflow import figure_workflow_step
    from .undergraduate_materials import attachment_prompt
    from .writing_prompt_schemes import snapshot_prompt_fingerprint
    step = figure_workflow_step(workflow, position)
    text = snapshot_prompt_fingerprint(step)["text"].replace("<PAPER_ID>", paper_id)
    brief = normalize_thesis_brief(workflow.get("thesis_brief"), required=True)
    from .undergraduate_word import word_prompt
    from .config import LEGACY_PROJECTS
    word_instructions = word_prompt(root or Path(LEGACY_PROJECTS["figures"]["root"]), paper_id) if int(step["source_step"]) == 3 else ""
    return (f"本科配图当前步骤：Step {position}\nPAPER_ID={paper_id}\nSOURCE_TEX={source_tex}\n"
        + text + "\n## 本论文固定输入\n" + json.dumps({k: v for k, v in brief.items() if k != "attachments"}, ensure_ascii=False)
        + attachment_prompt(brief.get("attachments", []))
        + "\n## 产物技术约定（不是期刊投稿）\n"
          f"所有输出保存于 papers/{paper_id}/。只执行当前一步，不改动其他论文。\n"
          "Step 3 在 submit/ 输出整合 TeX、同名 PDF、编译日志和 figure01.pdf 等图文件。"
          "submit/submission_manifest.json 保持程序兼容名称，不代表要投稿。JSON 包含 paper_id、"
          "submit_tex、compiled_pdf（均为画图工作区相对路径）、compile_status、compiled_pdf_created、figures、data_requirements。"
          "figures 每项包含 figure、figure_type、renderer、final_pdf、label、inserted_in_tex。"
          "结构图使用 figure_type=technical_diagram，renderer 为 graphviz/mermaid/plantuml/tikz/svg，"
          "并添加实际 source_file 路径。统计图用 quantitative 和 matplotlib 等数据绘图方式，必须有可追溯的核验数据；"
          "已真实执行并核验的仿真/合成输入测试要保留数据性质，不能冒充现实调查或生产结果。"
          "只有实际编译成功且全部必要图与引用核对通过才写 compile_status=success、compiled_pdf_created=true。"
          "有缺失材料则保留 data_requirements 并报告阻塞；禁止用伪造文件或成功字段通过检查。\n" + word_instructions)
