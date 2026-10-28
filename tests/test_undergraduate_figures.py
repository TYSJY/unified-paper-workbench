from __future__ import annotations

import copy
import asyncio
import json
import pytest

from unified_paper_orchestrator.undergraduate_figures import make_figure_snapshot, build_undergraduate_figure_prompt
from unified_paper_orchestrator.figure_workflow import normalize_figure_workflow_snapshot, figure_workflow_prompt_fingerprint, FigureWorkflowStore
from unified_paper_orchestrator.writing_workflow import WritingWorkflowStore, normalize_workflow_snapshot
from unified_paper_orchestrator.models import undergraduate_stage_boundary, action_for
from test_figure_sdk_v2 import _write_step3, _FakeRuntime, PAPER_ID

BRIEF = {"title": "面向校园的二手交易系统", "requirements": "中文图注，图按章编号", "minimum_references": None}


@pytest.mark.parametrize("identifier", ["undergraduate_humanities", "undergraduate_software"])
def test_matched_figure_scheme_is_pinned_and_not_journal(tmp_path, identifier):
    store = WritingWorkflowStore(tmp_path / "writing.json", tmp_path / "custom")
    figures = make_figure_snapshot(identifier, BRIEF)
    writing = normalize_workflow_snapshot({**store.snapshot_for_scheme(identifier), "thesis_brief": BRIEF,
                                          "figure_workflow_snapshot": figures})
    assert not store.path.exists()
    for index in range(1, 4):
        prompt = build_undergraduate_figure_prompt(figures, index, PAPER_ID, f"papers/{PAPER_ID}/input.tex")
        assert f"## Step {index}｜" in prompt and prompt.count("## Step ") == 1
        assert BRIEF["title"] in prompt and "中文图注" in prompt
        assert "local-LLM systems paper" not in prompt
        assert figure_workflow_prompt_fingerprint(tmp_path, figures, index)["kind"] == "scheme"
    store.select_prompt_scheme("journal")
    assert writing["figure_workflow_snapshot"] == figures
    broken = copy.deepcopy(figures)
    broken["steps"][0]["prompt_text"] += "tampered"
    with pytest.raises(ValueError, match="版本不一致"):
        normalize_figure_workflow_snapshot(broken)
    broken = copy.deepcopy(writing)
    broken["thesis_brief"]["title"] = "另一题目"
    with pytest.raises(ValueError, match="相同"):
        normalize_workflow_snapshot(broken)


def test_technical_diagram_requires_editable_source(tmp_path):
    from unified_paper_orchestrator.figure_output_contract import validate_figure_step
    _write_step3(tmp_path, PAPER_ID)
    manifest_path = tmp_path / "papers" / PAPER_ID / "submit" / "submission_manifest.json"
    data = json.loads(manifest_path.read_text())
    entry = data["figures"][0]
    entry.update(figure_type="technical_diagram", renderer="graphviz", source_file=f"papers/{PAPER_ID}/drafts/figure01.dot")
    manifest_path.write_text(json.dumps(data))
    assert not validate_figure_step(tmp_path, PAPER_ID, 3).ok
    source = tmp_path / entry["source_file"]
    source.write_text("digraph { frontend -> backend }")
    assert validate_figure_step(tmp_path, PAPER_ID, 3).ok
    data["data_requirements"] = [{"figure": "图2", "missing": "真实测试日志"}]
    manifest_path.write_text(json.dumps(data))
    assert validate_figure_step(tmp_path, PAPER_ID, 3).status == "waiting_data"


def test_new_writing_command_pins_both_schemes_and_attachments_without_switching_global(tmp_path, monkeypatch):
    from unified_paper_orchestrator.adapters.writing import WritingAdapter
    from unified_paper_orchestrator.undergraduate_materials import import_materials
    import unified_paper_orchestrator.figure_workflow as figure_module
    store = WritingWorkflowStore(tmp_path / "writing-config.json", tmp_path / "custom")
    base = store.snapshot_for_scheme("undergraduate_software")
    source = tmp_path / "学校要求.txt"
    source.write_text("中文图注", encoding="utf-8")
    attachments = import_materials([str(source)], tmp_path / "materials" / "undergraduate")["items"]
    adapter = WritingAdapter(workflow_store=store)
    adapter.root = tmp_path
    monkeypatch.setattr(figure_module, "FIGURE_WORKFLOW_STATE_PATH", tmp_path / "figure-config.json")
    monkeypatch.setattr(adapter, "_require_runtime_ready", lambda: {"ok": True})
    monkeypatch.setattr(adapter, "_pause_legacy_automation", lambda _: {})
    monkeypatch.setattr(adapter, "_reconcile_before_mutation", lambda _: {})
    state = {"settings": {}, "sessions": {}, "commands": []}
    monkeypatch.setattr(adapter, "load_state", lambda: state)
    monkeypatch.setattr(adapter, "save_state", lambda value: None)
    monkeypatch.setattr(adapter, "_allocate_pipeline_prefixes", lambda *_: ["2026-09-09_001"])
    result = adapter._new_sessions_locked(1, request_context={"prompt_scheme": "undergraduate_software",
        "expected_workflow_version": base["version_id"], "thesis_brief": {**BRIEF, "attachments": attachments}})
    workflow = result["commands"][0]["params"]["workflow_snapshot"]
    figures = workflow["figure_workflow_snapshot"]
    assert figures["prompt_scheme"] == workflow["prompt_scheme"] == "undergraduate_software"
    assert figures["thesis_brief"] == workflow["thesis_brief"]
    assert figures["thesis_brief"]["attachments"] == attachments
    assert not store.path.exists() and store.active_snapshot().get("prompt_scheme", "journal") == "journal"


def test_figure_handoff_uses_paper_scheme_then_resumes_its_own_snapshot(tmp_path, monkeypatch):
    from unified_paper_orchestrator.adapters import figures as module
    from unified_paper_orchestrator.io_utils import write_json_atomic
    root = tmp_path / "figures"
    project = {"root": root, "papers_dir": root / "papers", "state_path": root / "state.json",
        "prompt_path": root / "scheduler.md", "label": "测试", "automation_ids": [], "primary_automation_id": "", "heartbeat_minutes": 5}
    monkeypatch.setitem(module.LEGACY_PROJECTS, "figures", project)
    writing_path = tmp_path / "writing-state.json"
    monkeypatch.setitem(module.LEGACY_PROJECTS, "writing", {**module.LEGACY_PROJECTS["writing"], "state_path": writing_path})
    monkeypatch.setattr(module, "FIGURE_PIPELINE_LOCK_PATH", tmp_path / "lock")
    store = WritingWorkflowStore(tmp_path / "workflow.json", tmp_path / "custom")
    figures = make_figure_snapshot("undergraduate_software", BRIEF)
    writing = normalize_workflow_snapshot({**store.snapshot_for_scheme("undergraduate_software"),
        "thesis_brief": BRIEF, "figure_workflow_snapshot": figures})
    write_json_atomic(writing_path, {"sessions": {PAPER_ID: {"workflow_snapshot": writing}}})
    adapter = module.FigureAdapter(runtime=_FakeRuntime(), workflow_store=FigureWorkflowStore(tmp_path / "fig-workflow.json"))
    monkeypatch.setattr(adapter, "_pause_legacy_automation", lambda: {})
    source = tmp_path / "input.tex"
    source.write_text("fixture input")
    result = adapter.queue(PAPER_ID, str(source))
    assert result["command"]["params"]["workflow_snapshot"] == figures
    assert adapter.load_state()["papers"][PAPER_ID]["run_from_step"] == 1
    write_json_atomic(writing_path, {"sessions": {}})
    result = adapter.queue_from_step3(PAPER_ID)
    assert result["command"]["params"]["workflow_snapshot"] == figures
    monkeypatch.setattr(adapter, "_require_runtime_ready", lambda: pytest.fail("must not run journal image2 path"))
    with pytest.raises(ValueError, match="本科结构图"):
        adapter.queue_image2_revision(PAPER_ID, "Figure 1")


def test_undergraduate_delivery_does_not_auto_enter_journal_submission():
    stages = {"writing": {"status": "complete", "prompt_scheme": "undergraduate_software"},
              "figures": {"status": "complete", "word_delivery_ok": True}, "journal_author": {"status": "missing"}, "submission": {"status": "missing"}}
    stage = undergraduate_stage_boundary(stages, "journal_author")
    assert stage == "done" and action_for(stage, stages) == ("none", "")
    stages["writing"]["prompt_scheme"] = "journal"
    assert undergraduate_stage_boundary(stages, "journal_author") == "journal_author"


@pytest.mark.parametrize("identifier", ["undergraduate_humanities", "undergraduate_software"])
def test_offline_three_figure_steps_use_matched_prompts_and_block_tex_only_delivery(tmp_path, monkeypatch, identifier):
    from test_figure_sdk_v2 import _FakeCodex, _FakeTurnHandle, _write_step2
    from unified_paper_orchestrator.figure_sdk_dispatch import FigurePaperPlan, FigureSDKDispatcher
    from types import SimpleNamespace
    paper = tmp_path / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("fixture paper")
    async def run(handle):
        prompt = handle.thread.prompts[-1]
        if "本科配图当前步骤：Step 1" in prompt:
            (paper / "reference_board.md").write_text("学校图表要求与证据列表（测试）", encoding="utf-8")
        elif "本科配图当前步骤：Step 2" in prompt:
            _write_step2(tmp_path, PAPER_ID)
        elif "本科配图当前步骤：Step 3" in prompt:
            _write_step3(tmp_path, PAPER_ID)
        else:
            pytest.fail("undergraduate paper used journal prompt")
        handle.turn_value.status = SimpleNamespace(value="completed")
        return handle.turn_value
    monkeypatch.setattr(_FakeTurnHandle, "run", run)
    sdk = _FakeCodex(tmp_path)
    dispatcher = FigureSDKDispatcher(root=tmp_path, journal_path=tmp_path / "journal.json",
        lock_path=tmp_path / "dispatch.lock", codex_factory=lambda: sdk, max_parallel_papers=1)
    workflow = make_figure_snapshot(identifier, BRIEF)
    result = asyncio.run(dispatcher.run_until_idle([FigurePaperPlan(paper_id=PAPER_ID, request_id="ug-figures",
        source_tex=f"papers/{PAPER_ID}/input.tex", start_step=1, run_until_step=3, workflow_snapshot=workflow)]))
    assert not result["ok"], result
    assert result["papers"][PAPER_ID]["blocker"] == "FIGURE_OUTPUT_CONTRACT_INVALID"
    assert "Word" in result["papers"][PAPER_ID]["blocker_message"]
    assert sdk.start_calls == 1 and len(sdk.thread.prompts) == 3
    assert "undergraduate-word-export" in sdk.thread.prompts[-1]
    assert result["papers"][PAPER_ID]["workflow_snapshot"] == workflow
