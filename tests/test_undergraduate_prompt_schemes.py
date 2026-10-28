from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator.writing_prompt_schemes import (
    bundled_scheme_steps, is_undergraduate, normalize_thesis_brief, scheme_id,
)
from unified_paper_orchestrator.writing_workflow import WritingWorkflowStore, normalize_workflow_snapshot
from unified_paper_orchestrator.writing_sdk_dispatch import (
    WritingPaperPlan, WritingSDKDispatcher, existing_writing_prompt_provider,
)
from unified_paper_orchestrator.undergraduate_output_contract import validate_undergraduate_output
from unified_paper_orchestrator.writing_completion import expected_step_artifacts
from unified_paper_orchestrator.writing_output_contract import validate_writing_step_output
from test_writing_sdk_dispatch import FakeAsyncCodex

PAPER = "2026-09-09_001_undergraduate-fixture"
TITLE = "社区公共空间的使用分析"
BRIEF = {"title": TITLE, "requirements": "中文本科论文，章节按学校模板；允许辅助写作。", "minimum_references": 2}


def store_at(tmp_path):
    return WritingWorkflowStore(tmp_path / "workflow.json", tmp_path / "custom")


def paper_workflow(store, identifier="undergraduate_humanities"):
    store.select_prompt_scheme(identifier)
    return normalize_workflow_snapshot({**store.executable_snapshot(), "thesis_brief": BRIEF})


def outputs(root: Path, step: int, *, gap=False, verified=True):
    paths = expected_step_artifacts(root, root / "papers", PAPER, step)
    bib = "\n".join("@article{ref%d, title={Fixture %d}, author={Fixture Author}, year={2025}, url={https://example.test/source/%d}}" % (i, i, i) for i in (1, 2))
    if step == 1:
        texts = [f"# Idea Decision Card\n## 用户给定题目\n{TITLE}\n## 学校要求清单\n中文论文\n## 待确认项\n无\n"]
    elif step == 2:
        rows = "\n".join(f"| {i} | Fixture {i} | Fixture Author | 2025 | https://example.test/source/{i} | 全文 | {'verified' if verified else 'unverified'} |" for i in (1, 2))
        texts = ["|编号|标题|作者|年份|来源|阅读层级|核验状态|\n|---|---|---|---|---|---|---|\n" + rows,
                 bib, "# 文献图谱\n测试材料，不代表真实研究。", "# 定位说明\n测试材料，不代表真实研究。"]
    elif step == 7:
        texts = ["## 自查结论\n可修订\n## 主要问题\n表达问题\n## 修改建议\n简化重复表述\n"]
    else:
        texts = ["\\documentclass{ctexart}\n\\begin{document}\n"
                 "% UG_FIGURE_TARGET: 0\n% UG_TABLE_TARGET: 0\n% UG_VISUAL_PLAN: 测试夹具无需图表，不是学术充分性结论\n"
                 f"\\title{{{TITLE}}}\n\\section{{研究说明}}\n"
                 + ("[待补充：测试证据]\n" if gap else "测试夹具，不代表实际论文。\n")
                 + "引用 \\cite{ref1,ref2}。\n\\begin{thebibliography}{99}\n"
                 "\\bibitem{ref1} Fixture 1.\n\\bibitem{ref2} Fixture 2.\n"
                 "\\end{thebibliography}\n\\end{document}\n"]
    for path, text in zip(paths, texts):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if step >= 6 and step != 7:
        # Offline fixture emulates an explicit operator confirmation, not AI
        # self-approval. Separate tests assert the runtime waits without it.
        from unified_paper_orchestrator.writing_evidence_manual import prepare_manual_writing_evidence
        from unified_paper_orchestrator.writing_evidence_gate import validate_writing_evidence
        if not (root / "papers" / PAPER / "draft_v2.tex").exists():
            outputs(root, 5)
        if not validate_writing_evidence(root / "papers", PAPER)["ready"]:
            prepare_manual_writing_evidence(root / "papers", PAPER, check_only=False)


@pytest.mark.parametrize("identifier", ["undergraduate_humanities", "undergraduate_software"])
def test_ten_steps_have_only_current_step_and_fixed_topic(identifier):
    steps = bundled_scheme_steps(identifier)
    assert len(steps) == 10
    for index, step in enumerate(steps, 1):
        assert step["prompt_text"].count("## Step ") == 1
        assert f"## Step {index}｜" in step["prompt_text"]
        assert "不自行选题" in step["prompt_text"]


def test_switch_preserves_old_versions_models_and_edits(tmp_path):
    store = store_at(tmp_path)
    original = store.active_snapshot()
    paper = paper_workflow(store)
    original_paper = copy.deepcopy(paper)
    store.save_scheme_prompt("builtin-01", "本科第一步自定义提示词")
    edited = store.active_snapshot()
    store.select_prompt_scheme("undergraduate_software")
    store.select_prompt_scheme("undergraduate_humanities")
    assert store.active_snapshot()["steps"] == edited["steps"]
    store.select_prompt_scheme("journal")
    restored = store.active_snapshot()
    assert restored["steps"] == original["steps"]
    assert restored["sha256"] == original["sha256"]
    assert paper == original_paper
    assert is_undergraduate(paper)
    assert scheme_id(restored) == "journal"


def test_prompt_edit_does_not_change_already_created_paper(tmp_path):
    store = store_at(tmp_path)
    workflow = paper_workflow(store)
    provider = existing_writing_prompt_provider(tmp_path / "controller" / "SCHEDULER_PROMPT.md", "seed", workflow)
    before = provider(1, "")
    assert TITLE in before.text and BRIEF["requirements"] in before.text
    assert "50 篇文献" in before.text  # explicitly states this journal rule does not apply
    store.save_scheme_prompt("builtin-01", "A future paper uses this edited prompt")
    after = provider(1, "")
    assert before == after
    before.validate()
    assert before.source_prompt["kind"] == "scheme"
    assert "## Step 2｜" not in before.text


def test_tampered_prompt_snapshot_is_rejected(tmp_path):
    workflow = paper_workflow(store_at(tmp_path))
    workflow["steps"][0]["prompt_text"] += "tampered"
    with pytest.raises(ValueError, match="版本不一致"):
        normalize_workflow_snapshot(workflow)


@pytest.mark.parametrize("brief", [{}, {"title": TITLE}, {"requirements": "school"}, {**BRIEF, "minimum_references": -1}])
def test_missing_or_invalid_input_fails_before_execution(brief):
    with pytest.raises(ValueError):
        normalize_thesis_brief(brief, required=True)


def test_undergraduate_contract_accepts_two_references_and_no_figures_but_journal_does_not(tmp_path):
    workflow = paper_workflow(store_at(tmp_path))
    for step in (1, 2, 5):
        outputs(tmp_path, step)
        result = validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, step, workflow)
        assert result["ready"], result
    journal = validate_writing_step_output(tmp_path, tmp_path / "papers", PAPER, 5)
    assert not journal["ready"]
    codes = {issue["code"] for issue in journal["issues"]}
    assert {"latex_bibitems_insufficient", "latex_figures_insufficient", "latex_tables_insufficient"} <= codes


def test_unverified_reference_and_evidence_gaps_are_still_blocking(tmp_path):
    workflow = paper_workflow(store_at(tmp_path))
    outputs(tmp_path, 2, verified=False)
    assert not validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, 2, workflow)["ready"]
    outputs(tmp_path, 2)
    outputs(tmp_path, 6, gap=True)
    result = validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, 6, workflow)
    assert any(item["code"] == "undergraduate_evidence_gaps" for item in result["issues"])
    outputs(tmp_path, 10, gap=True)
    assert not validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, 10, workflow, is_terminal=True)["ready"]


def test_explicit_school_reference_minimum_is_enforced(tmp_path):
    workflow = paper_workflow(store_at(tmp_path))
    workflow["thesis_brief"]["minimum_references"] = 3
    outputs(tmp_path, 2)
    result = validate_undergraduate_output(tmp_path, tmp_path / "papers", PAPER, 2, workflow)
    assert any(item["code"] == "undergraduate_references_insufficient" for item in result["issues"])


@pytest.mark.parametrize("identifier", ["undergraduate_humanities", "undergraduate_software"])
def test_offline_full_ten_steps_use_one_thread_and_pinned_scheme(tmp_path, identifier):
    store = store_at(tmp_path)
    workflow = paper_workflow(store, identifier)
    # A fake provider must not need a real model catalogue or token metering.
    for step in workflow["steps"]:
        step["execution_profile"]["hard_token_limit"] = 0
    workflow = normalize_workflow_snapshot(workflow)
    provider = existing_writing_prompt_provider(tmp_path / "controller" / "SCHEDULER_PROMPT.md", "ug-paper", workflow)

    async def behavior(_thread, prompt, turn_number):
        assert TITLE in prompt
        assert BRIEF["requirements"] in prompt
        assert f"当前步骤：Step {turn_number}" in prompt
        outputs(tmp_path, turn_number)
        if turn_number == 5:
            from unified_paper_orchestrator.writing_evidence_manual import prepare_manual_writing_evidence
            prepare_manual_writing_evidence(tmp_path / "papers", PAPER, check_only=False)
        if turn_number == 1:
            # Switch the global setting during a running paper.
            store.select_prompt_scheme("journal")
        return f"PIPELINE_ID={PAPER}\nSTEP_DONE={turn_number}"

    sdk = FakeAsyncCodex(behavior)
    dispatcher = WritingSDKDispatcher(root=tmp_path, papers_dir=tmp_path / "papers",
        journal_path=tmp_path / "journal.json", lock_path=tmp_path / "dispatch.lock", codex_factory=lambda: sdk,
        prompt_sha256_manifest={i: "0" * 64 for i in range(1, 11)})
    plan = WritingPaperPlan("ug-paper", provider, run_until_step=10, workflow_snapshot=workflow)
    result = asyncio.run(dispatcher.run_until_idle([plan]))
    assert result["ok"], result
    assert result["papers"]["ug-paper"]["status"] == "complete", result
    assert sdk.start_calls == 1
    assert len(sdk.sent_prompts) == 10
    assert scheme_id(store.active_snapshot()) == "journal"
    assert result["papers"]["ug-paper"]["workflow_snapshot"]["prompt_scheme"] == identifier


def test_settings_action_calls_scheme_api_only():
    from unified_paper_orchestrator.ui_next.controller import WorkbenchController
    calls = []
    fake = SimpleNamespace(orchestrator=SimpleNamespace(select_writing_prompt_scheme=lambda value: calls.append(value)))
    WorkbenchController._run_workflow_action(fake, "writing", "scheme", "", "undergraduate_software")
    assert calls == ["undergraduate_software"]


def test_undergraduate_blocks_full_pipeline_before_state_mutation(tmp_path):
    from unified_paper_orchestrator.full_pipeline import FullPipelineRuntime
    store = store_at(tmp_path)
    paper_workflow(store)
    fake = SimpleNamespace(coordinator=SimpleNamespace(orchestrator=SimpleNamespace(writing_workflow_store=store),
        recover_failed_start=lambda _: pytest.fail("must not mutate full-pipeline state")))
    with pytest.raises(ValueError, match="本科"):
        FullPipelineRuntime.start_new(fake, 1)


def test_new_paper_missing_brief_and_scheme_race_fail_before_account_or_queue(tmp_path):
    from unified_paper_orchestrator.adapters.writing import WritingAdapter
    store = store_at(tmp_path)
    workflow = paper_workflow(store)
    adapter = WritingAdapter()
    adapter.workflow_store = store
    adapter._require_runtime_ready = lambda: pytest.fail("must validate input before account or queue")
    with pytest.raises(ValueError, match="题目|变化"):
        adapter._new_sessions_locked(1, request_context={"expected_workflow_version": workflow["version_id"]})
    store.select_prompt_scheme("journal")
    with pytest.raises(ValueError, match="变化"):
        adapter._new_sessions_locked(1, request_context={"expected_workflow_version": workflow["version_id"],
                                                        "thesis_brief": BRIEF})
