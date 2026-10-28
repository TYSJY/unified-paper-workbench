"""Named writing prompt sets; paper snapshots own their exact prompt text.

Only the selected step is sent to the model. Bundled assets are read when a
scheme is selected, never used to silently rewrite an existing paper snapshot.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Mapping

JOURNAL_SCHEME = "journal"
SCHEMES = {
    JOURNAL_SCHEME: ("原期刊版", ""),
    "undergraduate_humanities": ("本科人文社科版", "人文社科版.md"),
    "undergraduate_software": ("本科计算机软件版", "计算机软件版.md"),
}

# Aggregate prompt seals extracted from the delivered b7 EXE. Only completely
# unedited old factory sets may refresh for NEW papers; custom sets stay intact.
_B7_FACTORY_SEALS = {
    "undergraduate_humanities": "a6afc794f27eac907419f82d6d3e35ba7699efe083828da34d2aa01f511dc835",
    "undergraduate_software": "8c9e551f1ffb9c81c46d7ec03c2bd3c4dab8d31c0eab22b9d82833b164ca6386",
}
_B8_FACTORY_SEALS = {
    "undergraduate_humanities": "780fda04836936e2bb067bcf7a665839c2d99c2199a9211ea1ac1865ecd789ae",
    "undergraduate_software": "68409458f9281fea3f6436674c644f1b5066d495285a3a242e932e5302919c6f",
}


def is_previous_undergraduate_factory(snapshot: Mapping[str, Any]) -> bool:
    steps = snapshot.get("steps", [])
    if [step.get("source_step") for step in steps] != list(range(1, 11)):
        return False
    seal = hashlib.sha256("|".join(str(step.get("prompt_sha256", "")) for step in steps).encode()).hexdigest()
    return seal in {_B7_FACTORY_SEALS.get(scheme_id(snapshot)), _B8_FACTORY_SEALS.get(scheme_id(snapshot))}


def scheme_id(snapshot: Any) -> str:
    value = str(snapshot.get("prompt_scheme") or JOURNAL_SCHEME) if isinstance(snapshot, Mapping) else JOURNAL_SCHEME
    if value not in SCHEMES:
        raise ValueError("未知的写作提示词方案")
    return value


def is_undergraduate(snapshot: Any) -> bool:
    return scheme_id(snapshot) != JOURNAL_SCHEME


def scheme_choices() -> list[dict[str, str]]:
    return [{"id": key, "name": value[0]} for key, value in SCHEMES.items()]


def normalize_thesis_brief(value: Any, *, required: bool = False) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    title = str(raw.get("title") or "").strip()
    requirements = str(raw.get("requirements") or "").strip()
    from .undergraduate_materials import normalize_attachments
    attachments = normalize_attachments(raw.get("attachments"))
    if required and (not title or not (requirements or attachments)):
        raise ValueError("本科论文需要先填写题目和学校要求/格式；不会自动另选题目。")
    if len(title) > 500 or len(requirements) > 40000:
        raise ValueError("题目最多 500 字，学校要求最多 40000 字；长文档请填写明确的材料路径。")
    minimum = raw.get("minimum_references")
    if minimum in (None, ""):
        minimum = None
    elif isinstance(minimum, bool) or not re.fullmatch(r"[0-9]{1,4}", str(minimum)) or not 1 <= int(minimum) <= 1000:
        raise ValueError("学校最低参考文献数请留空，或填写 1-1000 的整数。")
    else:
        minimum = int(minimum)
    result = {"title": title, "requirements": requirements, "minimum_references": minimum}
    if attachments:
        result["attachments"] = attachments
    return result


def bundled_scheme_steps(identifier: str) -> list[dict[str, Any]]:
    if identifier not in SCHEMES or identifier == JOURNAL_SCHEME:
        raise ValueError("请选择一个本科提示词方案")
    asset_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    source = (asset_root / "prompts" / "undergraduate" / SCHEMES[identifier][1]).read_text(encoding="utf-8-sig")
    matches = list(re.finditer(r"(?m)^## Step (\d+)｜([^\r\n]+)", source))
    if [int(match[1]) for match in matches] != list(range(1, 11)):
        raise ValueError("本科提示词资源缺失或十步结构损坏")
    common = source[:matches[0].start()].rstrip()
    common += "\n\n" + (asset_root / "prompts" / "undergraduate" / "数据与图表核验协议.md").read_text(encoding="utf-8-sig").strip()
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        text = common + "\n\n" + source[match.start():end].strip() + "\n"
        result.append({"name": match[2].strip(), "prompt_text": text,
                       "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return result


def snapshot_prompt_fingerprint(definition: Mapping[str, Any]) -> dict[str, Any]:
    text = str(definition.get("prompt_text") or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not text.strip() or digest != definition.get("prompt_sha256"):
        raise ValueError("本科提示词与论文保存的版本不一致")
    return {"kind": "scheme", "step": int(definition["source_step"]),
            "sha256": digest, "text": text}


def undergraduate_prompt(definition: Mapping[str, Any], workflow: Mapping[str, Any], paper_id: str, step: int) -> str:
    from .undergraduate_verification import uses_undergraduate_verification
    brief = normalize_thesis_brief(workflow.get("thesis_brief"), required=True)
    text = snapshot_prompt_fingerprint(definition)["text"]
    if paper_id:
        text = text.replace("<PAPER_ID>", paper_id)
    data_rule = (
        "第5步按本科数据与图表核验协议允许带【验】的模拟占位；第6步先通过明确数据确认或核验材料导入关口。"
        "只清理已核验项目的占位标记，保留确认数据、真实局限和合法仿真的性质说明；未核验不能通过删标记继续。\n"
        if uses_undergraduate_verification(workflow) else
        "第六步和最终交接不得保留未解决的证据缺口、模拟数据或虚假的测试结论；"
        "材料不足就停下说明，不能删除标记来假装完成。\n"
    )
    protocol = (
        "本次运行的是本科版契约，不执行原期刊版 8 个侦察网址、50 篇文献、5 图 4 表、ML 审稿或模拟数据要求。\n"
        "学校明确要求优先；未明确的数量不凑数。写作阶段正文使用中文 LaTeX 中间稿；"
        "配图整合后由软件生成可编辑 Word、渲染并逐页复核后交付，第10步不是最终 Word 交付。\n"
        "第一步必须在产物中保留题目原文，并含二级标题：用户给定题目、学校要求清单、待确认项。\n"
        "第二步 references.md 使用表格：编号 | 标题 | 作者 | 年份 | 来源 DOI/URL/ISBN | 阅读层级 | 核验状态。"
        "编号从 1 开始；已实际核验的条目标 verified，未核验的用 unverified 并独立列为候选；"
        "references.bib 只收录已核验条目，真实身份与表格一一对应，引用键唯一。"
        "这仅记录核验依据，不得为了通过检查谎称 verified。\n"
        "第七步使用二级标题：自查结论、主要问题、修改建议。明确只是自查，不是正式评审。\n"
        + data_rule +
        "只执行当前一步。实际完成且产物齐备时，最终回复必须含两行：\n"
        f"PIPELINE_ID={paper_id or '<本步确定的论文ID>'}\nSTEP_DONE={step}\n"
        "有缺口时不输出完成标记。"
    )
    minimum = brief["minimum_references"]
    context = (
        f"\n\n## 本论文固定输入（用户提供，不是已验证的研究结果）\n"
        f"题目原文：{brief['title']}\n学校要求/格式及材料位置：\n{brief['requirements']}\n"
        f"学校最低参考文献数：{minimum if minimum is not None else '未指定，按实际要求核对，不套用期刊数量'}\n"
    )
    from .undergraduate_materials import attachment_prompt
    materials = attachment_prompt(brief.get("attachments", []))
    return f"当前步骤：Step {step}\n\n{text}\n## 本科版执行与输出约定\n{protocol}{context}{materials}"
