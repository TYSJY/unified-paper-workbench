from __future__ import annotations

import json
from pathlib import Path

import pytest

import unified_paper_orchestrator.writing_workflow as workflow_module
from unified_paper_orchestrator.adapters.writing import WritingAdapter
from unified_paper_orchestrator.writing_sdk_dispatch import WritingPaperPlan, existing_writing_prompt_provider
from unified_paper_orchestrator.writing_sdk_runtime import WritingSDKRuntime
from unified_paper_orchestrator.writing_workflow import (
    WritingWorkflowStore,
    default_workflow_snapshot,
    normalize_workflow_snapshot,
)


def _store(tmp_path: Path, monkeypatch) -> WritingWorkflowStore:
    prompt_dir = tmp_path / "custom-prompts"
    monkeypatch.setattr(workflow_module, "WRITING_CUSTOM_PROMPT_DIR", prompt_dir)
    return WritingWorkflowStore(tmp_path / "workflow.json", prompt_dir)


def test_default_workflow_is_the_unchanged_ten_step_profile() -> None:
    snapshot = default_workflow_snapshot()

    assert snapshot["step_count"] == 10
    assert [item["source_step"] for item in snapshot["steps"]] == list(range(1, 11))
    assert snapshot["steps"][-1]["contract_step"] == 10


def test_step_execution_profile_is_versioned_and_resolved_without_changing_prompt_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    before = store.active_snapshot()
    prompt_path = before["steps"][0]["prompt_path"]

    inventory = store.update_step_execution_profile(
        "builtin-01",
        {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "hard_token_limit": 123456,
            "unavailable_policy": "block",
            "preset": "quality",
        },
    )

    first = inventory["steps"][0]
    assert inventory["active_version_id"] != before["version_id"]
    assert inventory["sha256"] != before["sha256"]
    assert first["prompt_path"] == prompt_path
    assert first["execution_profile"]["model"] == "gpt-5.6-sol"
    assert first["effective_execution_profile"]["reasoning_effort"] == "high"
    assert first["effective_execution_profile"]["hard_token_limit"] == 123456


def test_truncate_then_add_custom_step_creates_versioned_snapshots(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    original = store.active_snapshot()

    truncated = store.remove_step("builtin-09")
    custom = store.add_custom_step("最终语言一致性检查")
    state = json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))

    assert original["step_count"] == 10
    assert truncated["step_count"] == 8
    assert custom["step_count"] == 9
    assert custom["steps"][-1]["kind"] == "custom"
    assert custom["steps"][-1]["contract_step"] == 8
    assert len(state["versions"]) == 3
    assert original["version_id"] in state["versions"]


def test_custom_tail_step_builds_exact_fixed_thread_prompt(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    inventory = store.add_custom_step("Step 11 final check")
    custom = inventory["steps"][-1]
    inventory = store.save_custom_prompt(custom["step_id"], "检查 `<paper_id>` 的最终稿并保存。\n")
    root = tmp_path / "writing-project"
    scheduler = root / "controller" / "SCHEDULER_PROMPT.md"
    scheduler.parent.mkdir(parents=True)
    scheduler.write_text("compatibility scheduler\n", encoding="utf-8")
    paper_id = "2026-07-15_001_workflow-test"

    provider = existing_writing_prompt_provider(scheduler, "paper-key", inventory)
    exact = provider(11, paper_id)
    plan = WritingPaperPlan(
        paper_key="paper-key",
        prompt_provider=provider,
        run_until_step=11,
        completed_step=10,
        paper_id=paper_id,
        thread_id="thread-workflow-test",
        workflow_snapshot=inventory,
    )

    plan.validate()
    assert f"PIPELINE_ID={paper_id}" in exact.text
    assert "STEP_DONE=11" in exact.text
    assert paper_id in exact.text
    assert exact.source_prompt and exact.source_prompt["kind"] == "custom"


def test_unsaved_custom_prompt_cannot_be_used_for_a_new_paper(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    store.add_custom_step("unfinished custom step")

    with pytest.raises(ValueError, match="尚未保存"):
        store.executable_snapshot()


def test_runtime_plan_uses_new_session_workflow_snapshot(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    workflow = store.add_custom_step("Step 11 final check")
    custom = workflow["steps"][-1]
    workflow = store.save_custom_prompt(custom["step_id"], "执行最终一致性检查并更新 `<paper_id>`。\n")
    root = tmp_path / "project"
    controller = root / "controller"
    controller.mkdir(parents=True)
    pipeline = controller / "pipeline_state.json"
    pipeline.write_text(
        json.dumps(
            {
                "settings": {},
                "sessions": {},
                "commands": [
                    {
                        "id": "cmd-workflow",
                        "action": "new_session",
                        "status": "pending",
                        "sdk_paper_key": "new-session:cmd-workflow",
                        "params": {"run_until_step": 11, "workflow_snapshot": workflow},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps({"schema": "unified-paper-orchestrator/writing-sdk-dispatch/v1", "papers": {}, "history": []}),
        encoding="utf-8",
    )
    runtime = WritingSDKRuntime(
        {
            "root": root,
            "papers_dir": root / "papers",
            "state_path": pipeline,
            "prompt_path": controller / "SCHEDULER_PROMPT.md",
            "automation_ids": [],
        },
        journal_path=journal,
        dispatch_lock_path=tmp_path / "dispatch.lock",
        pipeline_lock_path=tmp_path / "pipeline.lock",
    )

    plans = runtime.build_plans()

    assert len(plans) == 1
    assert plans[0].run_until_step == 11
    assert plans[0].workflow_snapshot and plans[0].workflow_snapshot["sha256"] == workflow["sha256"]


def test_writing_projection_requires_its_snapshotted_terminal_artifact(tmp_path: Path) -> None:
    adapter = WritingAdapter()
    adapter.root = tmp_path / "project"
    adapter.papers_dir = adapter.root / "papers"
    adapter.state_path = adapter.root / "controller" / "pipeline_state.json"
    adapter.state_path.parent.mkdir(parents=True)
    paper_id = "2026-07-15_002_truncated-workflow"
    snapshot = normalize_workflow_snapshot({"steps": default_workflow_snapshot()["steps"][:8]})
    artifact = adapter.papers_dir / paper_id / "draft_v4.tex"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("terminal draft\n", encoding="utf-8")
    adapter.state_path.write_text(
        json.dumps(
            {
                "settings": {},
                "sessions": {
                    paper_id: {
                        "status": "complete",
                        "step_done": 8,
                        "workflow_snapshot": snapshot,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    projection = adapter.stage_for(paper_id)

    assert projection["status"] == "complete"
    assert projection["workflow_step_count"] == 8
    assert projection["artifact"] == str(artifact)
