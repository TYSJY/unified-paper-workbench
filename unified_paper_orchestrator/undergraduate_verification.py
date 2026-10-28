"""Opt-in, paper-pinned undergraduate verification/cleanup contract.

Mechanical checks bind declared data and user confirmation; they cannot prove
that a survey or experiment actually happened. Journal policy is unchanged.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Mapping

from .writing_evidence_gate import validate_writing_evidence

PROTOCOL_MARKER = "<!-- undergraduate-verification:v1 -->"
_BOUNDARY = re.compile(r"(?m)^\s*%\s*(VERIFY|CONFIRMED)_DATA_(START|END)(?::\s*([^\r\n]+))?\s*$")
_PENDING = re.compile(r"SIMULATED_TO_REPLACE|VERIFY_DATA|\\VERIFY\b|【验】", re.I)
_ENV = re.compile(r"\\begin\{(figure\*?|table\*?)\}(.*?)\\end\{\1\}", re.S)


def uses_undergraduate_verification(workflow: Mapping[str, Any] | None) -> bool:
    return bool(isinstance(workflow, Mapping)
                and str(workflow.get("prompt_scheme", "")).startswith("undergraduate_")
                and any(PROTOCOL_MARKER in str(step.get("prompt_text", ""))
                        for step in workflow.get("steps", []) if isinstance(step, Mapping)))


def undergraduate_evidence_gate(papers_dir: Path | str, paper_id: str) -> dict[str, Any]:
    result = validate_writing_evidence(papers_dir, paper_id)
    # The journal's optional workspace auto-trust is not evidence of replacing
    # an undergraduate placeholder. Never create or accept that attestation here.
    if result.get("policy_completion") or result.get("gate_profile") == "evidence_auto_trust_transition":
        result.update(ready=False, transition_ready=False, status="BLOCKED")
        result.pop("input_snapshot", None)
        result.setdefault("issues", []).append({
            "code": "undergraduate_explicit_data_confirmation_required",
            "path": "results/manual_data_completion.json",
            "message": "本科【验】数据需导入核验材料或明确确认已逐项替换/核对，不能用期刊自动信任策略放行。",
        })
    return result


def visual_targets(text: str) -> tuple[int, int]:
    targets = []
    for kind in ("FIGURE", "TABLE"):
        values = re.findall(rf"(?m)^\s*%\s*UG_{kind}_TARGET:\s*(\d+)\s*$", text)
        if len(values) != 1 or not 0 <= int(values[0]) <= 100:
            raise ValueError(f"必须有唯一的 % UG_{kind}_TARGET: 0-100 图表目标")
        targets.append(int(values[0]))
    if not re.search(r"(?m)^\s*%\s*UG_VISUAL_PLAN:\s*\S.+$", text):
        raise ValueError("缺少 UG_VISUAL_PLAN 图表数量依据与章节覆盖说明")
    return targets[0], targets[1]


def data_blocks(text: str) -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    opened = None
    boundaries = list(_BOUNDARY.finditer(text))
    declared = re.findall(r"(?m)^\s*%\s*(?:VERIFY|CONFIRMED)_DATA_(?:START|END)\b", text)
    if len(boundaries) != len(declared):
        raise ValueError("数据块边界格式错误")
    for match in boundaries:
        kind, boundary, label = match.groups()
        if boundary == "START":
            if opened is not None or not label or label.strip() in blocks:
                raise ValueError("【验】数据块嵌套、标识缺失或重复")
            opened = (kind, label.strip(), match.end())
            continue
        if opened is None or opened[0] != kind or label:
            raise ValueError("【验】数据块边界不匹配")
        _, label, start = opened
        lines = []
        for line in text[start:match.start()].splitlines():
            if not line.strip():
                continue
            if not line.lstrip().startswith("%"):
                raise ValueError("嵌入数据块必须使用 LaTeX 注释")
            lines.append(line.lstrip()[1:].strip())
        fields = {}
        for key in ("DATA_STATUS", "DATA_PURPOSE", "CSV_SCHEMA"):
            values = [line.split(":", 1)[1].strip() for line in lines if line.startswith(key + ":")]
            if len(values) != 1 or not values[0]:
                raise ValueError(f"数据块 {label} 缺少唯一的 {key}")
            fields[key] = values[0]
        if lines.count("DATA_ROWS:") != 1:
            raise ValueError(f"数据块 {label} 缺少唯一的 DATA_ROWS")
        rows = lines[lines.index("DATA_ROWS:") + 1:]
        try:
            columns = next(csv.reader([fields["CSV_SCHEMA"]], strict=True))
            parsed = list(csv.reader(rows, strict=True))
        except csv.Error as exc:
            raise ValueError(f"数据块 {label} CSV 无法读取") from exc
        if (not parsed or len(columns) != len(set(columns)) or any(not col.strip() for col in columns)
                or any(len(row) != len(columns) or any(not cell.strip() for cell in row) for row in parsed)):
            raise ValueError(f"数据块 {label} 数据为空或列数/字段不一致；缺失值应显式写 NA")
        blocks[label] = {"kind": kind, "status": fields["DATA_STATUS"],
                         "payload": (fields["DATA_PURPOSE"], fields["CSV_SCHEMA"], tuple(rows))}
        opened = None
    if opened is not None:
        raise ValueError("【验】数据块未闭合")
    return blocks


def validate_verification_draft(text: str, step: int, papers_dir: Path | str, paper_id: str,
                                issues: list, counts: dict) -> None:
    def fail(code, message):
        issues.append({"code": code, "message": message})

    try:
        targets = visual_targets(text)
        blocks = data_blocks(text)
    except ValueError as exc:
        fail("undergraduate_verification_contract_invalid", str(exc))
        return
    counts.update(figure_target=targets[0], table_target=targets[1], verification_data_blocks=len(blocks))
    for name, target in zip(("figure", "table"), targets):
        if counts.get(name + "_count", 0) < target:
            fail("undergraduate_visual_target_unmet", f"{name} 目标 {target}，实际 {counts.get(name + '_count', 0)}")
    environments = list(_ENV.finditer(text))
    for label, block in blocks.items():
        env = next((m[2] for m in environments
                    if re.search(r"\\label\s*\{" + re.escape(label) + r"\}", m[2])), None)
        if env is None:
            fail("undergraduate_data_label_unbound", f"数据块 {label} 未对应实际 figure/table")
        if block["kind"] == "VERIFY":
            if step != 5 or block["status"] != "SIMULATED_TO_REPLACE":
                fail("undergraduate_pending_data_remaining", f"{label} 仍是待核验数据或状态错误")
            if env is not None and not re.search(r"\\VERIFY\b|【验】", env):
                fail("undergraduate_visible_verify_missing", f"{label} 的图注/表格缺少可见【验】标记")
        elif block["status"] not in {"USER_CONFIRMED_REAL", "VERIFIED_SIMULATION"}:
            fail("undergraduate_confirmed_data_status_invalid", f"{label} 的确认状态不受支持")
        elif block["status"] == "VERIFIED_SIMULATION" and env is not None and not re.search(r"模拟|仿真|合成|simulat|synthetic", env, re.I):
            fail("undergraduate_simulation_disclosure_missing", f"{label} 的图表必须保留模拟/仿真性质说明")
    if step == 5:
        if _PENDING.search(text) and not any(b["kind"] == "VERIFY" for b in blocks.values()):
            fail("undergraduate_unbound_verify_marker", "有【验】标记但没有可追溯的待核验数据块")
        return
    if _PENDING.search(text):
        fail("undergraduate_pending_data_remaining", "清理稿仍含【验】、VERIFY 或模拟待替换状态")
    gate = undergraduate_evidence_gate(papers_dir, paper_id)
    if not gate.get("ready"):
        fail("undergraduate_data_not_confirmed", "第6步及后续稿件必须对应已导入核验材料或手动确认的第5步稿件")
        return
    try:
        prior = (Path(papers_dir) / paper_id / "draft_v2.tex").read_text(encoding="utf-8-sig")
        approved_blocks = data_blocks(prior)
        approved_targets = visual_targets(prior)
    except (OSError, UnicodeError, ValueError) as exc:
        fail("undergraduate_accepted_data_unreadable", f"无法核对已确认数据：{type(exc).__name__}")
        return
    if targets != approved_targets:
        fail("undergraduate_visual_target_changed", "不得降低或更改已确认稿中的图表目标来通过检查")
    if set(blocks) != set(approved_blocks) or any(
        blocks[label]["payload"] != approved_blocks[label]["payload"] for label in set(blocks) & set(approved_blocks)
    ):
        fail("undergraduate_confirmed_data_changed", "已确认的数据块、字段、数据行或数值被删除/改动；请保留核验后的数据")
    if not undergraduate_evidence_gate(papers_dir, paper_id).get("input_snapshot") == gate.get("input_snapshot"):
        fail("undergraduate_confirmation_changed", "核验输入在清理检查期间发生变化")
