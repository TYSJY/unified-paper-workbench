from __future__ import annotations

import copy
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator.undergraduate_verification import (
    PROTOCOL_MARKER, data_blocks, undergraduate_evidence_gate, uses_undergraduate_verification,
)
from unified_paper_orchestrator.undergraduate_output_contract import validate_undergraduate_output
from unified_paper_orchestrator.writing_evidence_manual import (
    prepare_manual_writing_evidence, prepare_policy_trusted_writing_evidence,
)
from unified_paper_orchestrator.writing_sdk_dispatch import WritingPaperPlan, WritingSDKDispatcher
from test_undergraduate_prompt_schemes import PAPER, store_at, paper_workflow, outputs


BLOCK = """% VERIFY_DATA_START: tab:sample
% DATA_STATUS: SIMULATED_TO_REPLACE
% DATA_PURPOSE: 测试夹具，核对计数；不代表真实调查
% CSV_SCHEMA: group,count
% DATA_ROWS:
% A,12
% B,18
% VERIFY_DATA_END
"""
TABLE = r"""\begin{table}
\caption{模拟占位，待核验\VERIFY}\label{tab:sample}
\begin{tabular}{lr}A & 12\VERIFY \\ B & 18\VERIFY\end{tabular}
\end{table}
表\ref{tab:sample}是模拟占位\VERIFY。
"""


def setup_paper(tmp_path):
    workflow = paper_workflow(store_at(tmp_path))
    outputs(tmp_path, 1)
    outputs(tmp_path, 2)
    outputs(tmp_path, 5)
    draft = tmp_path / "papers" / PAPER / "draft_v2.tex"
    text = draft.read_text(encoding="utf-8").replace("% UG_TABLE_TARGET: 0", "% UG_TABLE_TARGET: 1")
    text = text.replace("\\begin{document}", "\\newcommand{\\VERIFY}{\\textsuperscript{\\textbf{【验】}}}\n\\begin{document}")
    text = text.replace("\\begin{thebibliography}", BLOCK + TABLE + "\\begin{thebibliography}")
    draft.write_text(text, encoding="utf-8")
    return workflow, draft


def check(tmp_path, workflow, step):
    return validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, step, workflow, is_terminal=step == 10)


def clean_draft(draft, *, simulation=False):
    text = draft.read_text(encoding="utf-8")
    text = text.replace("\\newcommand{\\VERIFY}{\\textsuperscript{\\textbf{【验】}}}\n", "")
    text = text.replace("\\VERIFY", "").replace("VERIFY_DATA", "CONFIRMED_DATA")
    text = text.replace("SIMULATED_TO_REPLACE", "VERIFIED_SIMULATION" if simulation else "USER_CONFIRMED_REAL")
    text = text.replace("模拟占位，待核验", "已执行的仿真结果" if simulation else "已核验计数")
    text = text.replace("模拟占位", "已执行的仿真结果" if simulation else "已核验计数")
    return text


def confirm(draft):
    return prepare_manual_writing_evidence(draft.parent.parent, PAPER, check_only=False)


def test_one_undergraduate_block_is_allowed_without_journal_three_block_floor(tmp_path):
    workflow, _ = setup_paper(tmp_path)
    result = check(tmp_path, workflow, 5)
    assert result["ready"], result
    assert result["counts"]["verification_data_blocks"] == 1
    assert not validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, 5, workflow, is_terminal=True)["ready"]


@pytest.mark.parametrize("replacement", [
    ("% VERIFY_DATA_END", ""),
    ("% B,18", "% B,18,extra"),
    ("% B,18", "% B,"),
    ("% CSV_SCHEMA: group,count", "% CSV_SCHEMA: group,group"),
    ("\\label{tab:sample}", "\\label{tab:other}"),
    (TABLE, TABLE.replace("\\VERIFY", "")),
    ("% UG_TABLE_TARGET: 1", "% UG_TABLE_TARGET: 2"),
])
def test_incomplete_or_unmarked_data_and_missing_visuals_block(tmp_path, replacement):
    workflow, draft = setup_paper(tmp_path)
    draft.write_text(draft.read_text(encoding="utf-8").replace(*replacement), encoding="utf-8")
    assert not check(tmp_path, workflow, 5)["ready"]


def test_gate_requires_confirmation_even_if_markers_were_deleted(tmp_path):
    workflow, draft = setup_paper(tmp_path)
    dispatcher = WritingSDKDispatcher(root=tmp_path, papers_dir=tmp_path / "papers",
        journal_path=tmp_path / "journal.json", lock_path=tmp_path / "lock")
    plan = WritingPaperPlan("ug", lambda *_: "unused", workflow_snapshot=workflow)
    decision = dispatcher._gate_ready_for_plan(plan, PAPER, 6)
    assert not decision["ready"] and decision["waitable"]
    assert decision["blocker"] == "WRITING_EVIDENCE_GATE_BLOCKED"
    draft.write_text(clean_draft(draft), encoding="utf-8")
    assert not dispatcher._gate_ready_for_plan(plan, PAPER, 6)["ready"]
    assert confirm(draft)["ok"]
    assert dispatcher._gate_ready_for_plan(plan, PAPER, 6)["ready"]
    draft.write_text(draft.read_text(encoding="utf-8") + "\n% later edit", encoding="utf-8")
    assert not dispatcher._gate_ready_for_plan(plan, PAPER, 6)["ready"]


def test_cleanup_preserves_confirmed_values_and_requires_no_pending_markers(tmp_path):
    workflow, draft = setup_paper(tmp_path)
    assert confirm(draft)["ok"]
    clean = clean_draft(draft)
    out = draft.with_name("draft_v3.tex")
    out.write_text(clean, encoding="utf-8")
    result = check(tmp_path, workflow, 6)
    assert result["ready"], result
    for corrupt in (clean.replace("% A,12", "% A,13"), clean.replace("% UG_TABLE_TARGET: 1", "% UG_TABLE_TARGET: 0"),
                    clean + "\n【验】", clean.replace("% CONFIRMED_DATA_START: tab:sample", "% deleted")):
        out.write_text(corrupt, encoding="utf-8")
        assert not check(tmp_path, workflow, 6)["ready"]


def test_true_simulation_keeps_disclosure(tmp_path):
    workflow, draft = setup_paper(tmp_path)
    assert confirm(draft)["ok"]
    clean = clean_draft(draft, simulation=True)
    out = draft.with_name("draft_v3.tex")
    out.write_text(clean, encoding="utf-8")
    assert check(tmp_path, workflow, 6)["ready"]
    out.write_text(clean.replace("已执行的仿真结果", "实际调查结果"), encoding="utf-8")
    result = check(tmp_path, workflow, 6)
    assert any(i["code"] == "undergraduate_simulation_disclosure_missing" for i in result["issues"])


def test_journal_auto_trust_cannot_accept_undergraduate_placeholders(tmp_path):
    _, draft = setup_paper(tmp_path)
    policy = {"schema": "unified-paper-orchestrator/writing-gate-policy/v1", "gate_id": "step5_data_confirmation",
              "mode": "auto_trust", "revision": 1, "updated_at": "2026-09-09T00:00:00+08:00"}
    assert prepare_policy_trusted_writing_evidence(draft.parent.parent, PAPER, policy_snapshot=policy, check_only=False)["ok"]
    assert not undergraduate_evidence_gate(draft.parent.parent, PAPER)["ready"]
    assert confirm(draft)["ok"]
    assert undergraduate_evidence_gate(draft.parent.parent, PAPER)["ready"]


def test_legacy_snapshot_is_not_silently_upgraded(tmp_path):
    workflow, _ = setup_paper(tmp_path)
    assert uses_undergraduate_verification(workflow)
    old = copy.deepcopy(workflow)
    for step in old["steps"]:
        step["prompt_text"] = step["prompt_text"].replace(PROTOCOL_MARKER, "")
    assert not uses_undergraduate_verification(old)
    assert not check(tmp_path, old, 5)["ready"]  # old no-placeholder contract stays intact


def test_ui_manual_confirmation_works_even_when_journal_policy_is_auto_trust(tmp_path, monkeypatch):
    import unified_paper_orchestrator.ui_next.controller as module
    workflow, draft = setup_paper(tmp_path)
    monkeypatch.setitem(module.LEGACY_PROJECTS, "writing", {"papers_dir": draft.parent.parent})
    fake = SimpleNamespace(orchestrator=SimpleNamespace(
        writing_gate_policy=lambda: {"mode": "auto_trust"},
        resume_writing_papers=lambda *a, **k: {"resumed": True}),
        _undergraduate_verification_for_paper=lambda _: True)
    assert module.WorkbenchController._confirm_manual_writing_data(fake, PAPER)["resumed"]
    assert undergraduate_evidence_gate(draft.parent.parent, PAPER)["ready"]


def test_sdk_waits_without_a_model_turn_then_resumes_after_real_data_replacement(tmp_path):
    from test_writing_sdk_dispatch import FakeAsyncCodex, FakeThread
    from unified_paper_orchestrator.writing_sdk_dispatch import existing_writing_prompt_provider
    from unified_paper_orchestrator.writing_workflow import normalize_workflow_snapshot
    workflow, draft = setup_paper(tmp_path)
    for step in workflow["steps"]:
        step["execution_profile"]["hard_token_limit"] = 0
    workflow = normalize_workflow_snapshot(workflow)
    async def behavior(_thread, prompt, _turn):
        assert "当前步骤：Step 6" in prompt
        assert "manual-edit transition" in prompt
        draft.with_name("draft_v3.tex").write_text(clean_draft(draft), encoding="utf-8")
        return f"PIPELINE_ID={PAPER}\nSTEP_DONE=6"
    sdk = FakeAsyncCodex(behavior)
    thread = FakeThread(sdk, "ug-confirmation-test")
    sdk.threads[thread.id] = thread
    dispatcher = WritingSDKDispatcher(root=tmp_path, papers_dir=tmp_path / "papers", journal_path=tmp_path / "sdk.json",
        lock_path=tmp_path / "sdk.lock", codex_factory=lambda: sdk, prompt_sha256_manifest={i: "0" * 64 for i in range(1, 11)})
    provider = existing_writing_prompt_provider(tmp_path / "scheduler.md", "ug", workflow)
    plan = WritingPaperPlan("ug", provider, run_until_step=6, completed_step=5, paper_id=PAPER,
                            thread_id=thread.id, workflow_snapshot=workflow)
    first = asyncio.run(dispatcher.run_until_idle([plan]))
    assert first["papers"]["ug"]["status"] == "waiting_prerequisite", first
    assert sdk.sent_prompts == []
    # The test operator supplies replacement values before explicit confirmation.
    draft.write_text(draft.read_text(encoding="utf-8").replace("A,12", "A,13").replace("12\\VERIFY", "13\\VERIFY"), encoding="utf-8")
    assert confirm(draft)["ok"]
    second = asyncio.run(dispatcher.run_until_idle([plan]))
    assert second["ok"], second
    assert len(sdk.sent_prompts) == 1
    assert data_blocks(draft.with_name("draft_v3.tex").read_text(encoding="utf-8"))["tab:sample"]["payload"][2][0] == "A,13"


def test_adapter_exposes_waiting_data_even_with_an_incomplete_cleanup_file(tmp_path, monkeypatch):
    from unified_paper_orchestrator.adapters import writing as module
    workflow, draft = setup_paper(tmp_path)
    adapter = module.WritingAdapter(workflow_store=store_at(tmp_path))
    adapter.root = tmp_path
    adapter.papers_dir = tmp_path / "papers"
    monkeypatch.setattr(adapter, "load_state", lambda: {"sessions": {PAPER: {
        "workflow_snapshot": workflow, "step_done": 5, "status": "waiting_prerequisite"}}})
    draft.with_name("draft_v3.tex").write_text("incomplete cleanup", encoding="utf-8")
    projection = adapter.stage_for(PAPER)
    assert projection["status"] == "waiting_data", projection
    assert not projection["evidence_gate"]["ready"]


@pytest.mark.parametrize("identifier,name", [("undergraduate_humanities", "人文社科版.md"), ("undergraduate_software", "计算机软件版.md")])
def test_delivered_b7_factory_refreshes_only_new_papers(tmp_path, identifier, name):
    import hashlib
    import re
    archive_module = pytest.importorskip("PyInstaller.archive.readers")
    from unified_paper_orchestrator.writing_prompt_schemes import is_previous_undergraduate_factory
    exe = Path(__file__).resolve().parents[1] / "UnifiedPaperWorkbench-0.12.0b7.exe"
    if not exe.is_file():
        pytest.skip("optional delivered-b7 compatibility probe")
    source = archive_module.CArchiveReader(str(exe)).extract("prompts\\undergraduate\\" + name).decode("utf-8-sig")
    matches = list(re.finditer(r"(?m)^## Step (\d+)｜([^\r\n]+)", source))
    store = store_at(tmp_path)
    base = store.snapshot_for_scheme(identifier)
    steps = copy.deepcopy(base["steps"])
    for index, match in enumerate(matches):
        text = source[:matches[0].start()].rstrip() + "\n\n" + source[match.start():matches[index+1].start() if index < 9 else len(source)].strip() + "\n"
        steps[index].update(prompt_text=text, prompt_sha256=hashlib.sha256(text.encode()).hexdigest())
    store._publish(steps, "test_b7_fixture", {}, prompt_scheme=identifier)
    saved = store.active_snapshot()
    assert is_previous_undergraduate_factory(saved)
    pinned = copy.deepcopy(saved)
    refreshed = store.snapshot_for_scheme(identifier)
    assert uses_undergraduate_verification(refreshed)
    assert refreshed["version_id"] != saved["version_id"]
    assert saved == pinned == store.active_snapshot()
    store.save_scheme_prompt("builtin-01", "用户自行修改的提示词，不自动替换")
    custom = store.snapshot_for_scheme(identifier)
    assert not uses_undergraduate_verification(custom)
    assert "用户自行修改" in custom["steps"][0]["prompt_text"]
