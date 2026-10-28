from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unified_paper_orchestrator import config
from unified_paper_orchestrator.adapters import figures as figure_adapter_module
from unified_paper_orchestrator.adapters.figures import FigureAdapter
from unified_paper_orchestrator.figure_output_contract import validate_figure_step
from unified_paper_orchestrator.figure_prompt_integrity import (
    FIGURE_PROMPT_FILES,
    FIGURE_PROMPT_SHA256,
    validate_all_figure_prompts,
)
from unified_paper_orchestrator.figure_sdk_dispatch import FigurePaperPlan, FigureSDKDispatcher
from unified_paper_orchestrator.figure_sdk_runtime import FigureSDKRuntime
from unified_paper_orchestrator.figure_workflow import (
    FigureWorkflowStore,
    default_figure_workflow_snapshot,
    normalize_figure_workflow_snapshot,
)
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.ui.main_window import _figure_task_rows


PAPER_ID = "2026-07-15_001_figure-sdk-test"


def _copy_tuned_prompts(root: Path) -> None:
    source = config.FIGURE_ROOT / "prompts"
    target = root / "prompts"
    target.mkdir(parents=True)
    for filename in FIGURE_PROMPT_FILES.values():
        shutil.copy2(source / filename, target / filename)


def _write_step2(root: Path, paper_id: str) -> None:
    paper = root / "papers" / paper_id
    (paper / "drafts").mkdir(parents=True, exist_ok=True)
    (paper / "figure_plan.md").write_text("# plan\n", encoding="utf-8")
    (paper / "figure_prompts.md").write_text("# prompts\n", encoding="utf-8")
    (paper / "drafts" / "figure1_brief.md").write_text("# brief\n", encoding="utf-8")


def _write_step3(root: Path, paper_id: str, *, data_requirements: list[dict[str, Any]] | None = None) -> None:
    paper = root / "papers" / paper_id
    drafts = paper / "drafts"
    submit = paper / "submit"
    drafts.mkdir(parents=True, exist_ok=True)
    submit.mkdir(parents=True, exist_ok=True)
    raw = drafts / "figure1_raw.png"
    final = submit / "figure1_hero.pdf"
    tex = submit / f"{paper_id}.tex"
    pdf = submit / f"{paper_id}.pdf"
    raw.write_bytes(b"png-image")
    final.write_bytes(b"pdf-figure")
    tex.write_text("\\includegraphics{figure1_hero.pdf}\n", encoding="utf-8")
    pdf.write_bytes(b"compiled-paper")
    manifest = {
        "paper_id": paper_id,
        "source_tex": f"papers/{paper_id}/input.tex",
        "figure_plan": f"papers/{paper_id}/figure_plan.md",
        "submit_tex": f"papers/{paper_id}/submit/{paper_id}.tex",
        "compiled_pdf": f"papers/{paper_id}/submit/{paper_id}.pdf",
        "compiled_pdf_created": True,
        "figures": [
            {
                "figure": "Figure 1",
                "figure_type": "conceptual_schematic",
                "renderer": "image2",
                "raw_output": f"papers/{paper_id}/drafts/figure1_raw.png",
                "final_pdf": f"papers/{paper_id}/submit/figure1_hero.pdf",
                "inserted_in_tex": True,
            }
        ],
        "tables": [],
        "data_requirements": data_requirements or [],
        "compile_status": "success",
        "compile_error": "",
    }
    (submit / "submission_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def test_tuned_figure_prompts_are_byte_identical() -> None:
    observed = validate_all_figure_prompts(config.FIGURE_ROOT)
    assert {step: value.sha256 for step, value in observed.items()} == dict(FIGURE_PROMPT_SHA256)


def test_figure_prompts_extract_user_confirmed_inline_data_before_blocking() -> None:
    step2 = (config.FIGURE_ROOT / "prompts" / FIGURE_PROMPT_FILES[2]).read_text(encoding="utf-8")
    step3 = (config.FIGURE_ROOT / "prompts" / FIGURE_PROMPT_FILES[3]).read_text(encoding="utf-8")

    assert "User-confirmed data authority (highest priority)" in step2
    assert "A CSV produced by extracting confirmed tables" in step2
    assert "User-confirmed data authority (highest priority)" in step3
    assert "A missing standalone CSV is not missing experimental data" in step3


def test_figure_output_contract_distinguishes_complete_and_waiting_data(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _write_step2(root, PAPER_ID)
    _write_step3(root, PAPER_ID)
    result = validate_figure_step(root, PAPER_ID, 3)
    assert result.ok is True
    assert result.status == "complete"
    assert result.figure_count == 1

    _write_step3(root, PAPER_ID, data_requirements=[{"figure": "Figure 2"}])
    waiting = validate_figure_step(root, PAPER_ID, 3)
    assert waiting.ok is False
    assert waiting.status == "waiting_data"
    assert waiting.data_requirement_count == 1
    revision_gate = validate_figure_step(root, PAPER_ID, 4)
    assert revision_gate.ok is True
    assert revision_gate.status == "complete"


def test_figure_output_contract_accepts_nested_compile_validation(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _write_step2(root, PAPER_ID)
    _write_step3(root, PAPER_ID)
    manifest_path = paper / "submit" / "submission_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    compile_status = manifest.pop("compile_status")
    manifest["validation"] = {"compile_status": compile_status}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    result = validate_figure_step(root, PAPER_ID, 3)

    assert result.ok is True
    assert result.status == "complete"
    assert "manifest.compile_status 位于 validation 子对象；已按兼容格式验收" in result.warnings


class _FakeTurnHandle:
    def __init__(self, thread: "_FakeThread", turn: SimpleNamespace) -> None:
        self.thread = thread
        self.turn_value = turn
        self.id = turn.id

    async def run(self) -> SimpleNamespace:
        prompt = self.thread.prompts[-1]
        if "02_make_figure_plan.md" in prompt:
            _write_step2(self.thread.sdk.root, PAPER_ID)
        elif "03_generate_figures_and_submit_package.md" in prompt:
            _write_step3(self.thread.sdk.root, PAPER_ID)
        self.turn_value.status = SimpleNamespace(value="completed")
        return self.turn_value


class _FakeThread:
    def __init__(self, sdk: "_FakeCodex", thread_id: str) -> None:
        self.sdk = sdk
        self.id = thread_id
        self.name = ""
        self.turns: list[SimpleNamespace] = []
        self.prompts: list[str] = []
        self.turn_kwargs: list[dict[str, Any]] = []

    async def set_name(self, name: str) -> None:
        self.name = name

    async def read(self, *, include_turns: bool = False) -> SimpleNamespace:
        return SimpleNamespace(thread=SimpleNamespace(turns=list(self.turns) if include_turns else []))

    async def turn(self, prompt: str, **kwargs: Any) -> _FakeTurnHandle:
        self.prompts.append(prompt)
        self.turn_kwargs.append(dict(kwargs))
        turn = SimpleNamespace(
            id=f"turn-{len(self.turns) + 1}",
            status=SimpleNamespace(value="inProgress"),
            usage=None,
        )
        self.turns.append(turn)
        return _FakeTurnHandle(self, turn)


class _FakeCodex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.thread = _FakeThread(self, "thread-fixed-figure")
        self.start_calls = 0
        self.resume_calls: list[str] = []

    async def __aenter__(self) -> "_FakeCodex":
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        return None

    async def thread_start(self, **_kwargs: Any) -> _FakeThread:
        self.start_calls += 1
        return self.thread

    async def thread_resume(self, thread_id: str, **_kwargs: Any) -> _FakeThread:
        self.resume_calls.append(thread_id)
        assert thread_id == self.thread.id
        return self.thread

    async def thread_list(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(data=[], next_cursor=None)


def test_dispatcher_reuses_one_fixed_thread_for_step2_and_step3(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _copy_tuned_prompts(root)
    sdk = _FakeCodex(root)
    dispatcher = FigureSDKDispatcher(
        root=root,
        journal_path=tmp_path / "journal.json",
        lock_path=tmp_path / "dispatch.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=1,
    )
    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                FigurePaperPlan(
                    paper_id=PAPER_ID,
                    request_id="request-1",
                    source_tex=f"papers/{PAPER_ID}/input.tex",
                    start_step=2,
                    run_until_step=3,
                )
            ]
        )
    )
    assert result["ok"] is True
    assert sdk.start_calls == 1
    assert sdk.resume_calls == [sdk.thread.id]
    assert len(sdk.thread.prompts) == 2
    assert "02_make_figure_plan.md" in sdk.thread.prompts[0]
    assert "03_generate_figures_and_submit_package.md" in sdk.thread.prompts[1]
    assert result["papers"][PAPER_ID]["thread_id"] == sdk.thread.id
    assert result["papers"][PAPER_ID]["completed_step"] == 3


def test_figure_steps_switch_models_inside_the_same_fixed_thread(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _copy_tuned_prompts(root)
    workflow = copy.deepcopy(default_figure_workflow_snapshot())
    workflow["default_execution_profile"] = {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "hard_token_limit": 0,
        "unavailable_policy": "block",
        "preset": "balanced",
    }
    workflow["steps"][1]["execution_profile"] = {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "hard_token_limit": 0,
        "unavailable_policy": "block",
        "preset": "quality",
    }
    workflow["steps"][2]["execution_profile"] = {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "hard_token_limit": 0,
        "unavailable_policy": "block",
        "preset": "economy",
    }
    workflow = normalize_figure_workflow_snapshot(workflow)
    sdk = _FakeCodex(root)
    dispatcher = FigureSDKDispatcher(
        root=root,
        journal_path=tmp_path / "journal.json",
        lock_path=tmp_path / "dispatch.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=1,
        model="gpt-5.5",
    )

    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                FigurePaperPlan(
                    paper_id=PAPER_ID,
                    request_id="request-profile",
                    source_tex=f"papers/{PAPER_ID}/input.tex",
                    start_step=2,
                    run_until_step=3,
                    workflow_snapshot=workflow,
                )
            ]
        )
    )

    assert result["ok"] is True
    assert sdk.start_calls == 1
    assert [item["model"] for item in sdk.thread.turn_kwargs] == [
        "gpt-5.6-sol",
        "gpt-5.6-luna",
    ]
    assert [item["effort"].value for item in sdk.thread.turn_kwargs] == ["high", "low"]
    assert result["papers"][PAPER_ID]["steps"]["2"]["execution_profile"]["model"] == "gpt-5.6-sol"
    assert result["papers"][PAPER_ID]["steps"]["3"]["execution_profile"]["model"] == "gpt-5.6-luna"


def test_dispatcher_reuses_proven_identical_artifacts_without_new_turns(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _copy_tuned_prompts(root)
    sdk = _FakeCodex(root)
    dispatcher = FigureSDKDispatcher(
        root=root,
        journal_path=tmp_path / "journal.json",
        lock_path=tmp_path / "dispatch.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=1,
    )
    first = asyncio.run(
        dispatcher.run_until_idle(
            [
                FigurePaperPlan(
                    paper_id=PAPER_ID,
                    request_id="request-cache-1",
                    source_tex=f"papers/{PAPER_ID}/input.tex",
                    start_step=2,
                    run_until_step=3,
                )
            ]
        )
    )
    assert first["ok"] is True
    assert len(sdk.thread.prompts) == 2

    second = asyncio.run(
        dispatcher.run_until_idle(
            [
                FigurePaperPlan(
                    paper_id=PAPER_ID,
                    request_id="request-cache-2",
                    source_tex=f"papers/{PAPER_ID}/input.tex",
                    start_step=2,
                    run_until_step=3,
                )
            ]
        )
    )
    assert second["ok"] is True
    assert len(sdk.thread.prompts) == 2
    assert second["papers"][PAPER_ID]["steps"]["2"]["state"] == "skipped_cached"
    assert second["papers"][PAPER_ID]["steps"]["3"]["state"] == "skipped_cached"


def test_dynamic_dispatcher_accepts_new_paper_before_existing_paper_finishes(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper_a = "2026-07-15_201_dynamic-a"
    paper_b = "2026-07-15_202_dynamic-b"
    for paper_id in (paper_a, paper_b):
        target = root / "papers" / paper_id
        target.mkdir(parents=True)
        (target / "input.tex").write_text("paper\n", encoding="utf-8")

    class CodexContext:
        async def __aenter__(self) -> "CodexContext":
            return self

        async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
            return None

    class DynamicDispatcher(FigureSDKDispatcher):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.started_a = asyncio.Event()
            self.started_b = asyncio.Event()
            self.a_finished = False

        async def _run_paper_in_slot(
            self,
            _codex: Any,
            plan: FigurePaperPlan,
            _semaphore: asyncio.Semaphore,
        ) -> None:
            record = self._state["papers"][plan.paper_id]
            record.update({"status": "running", "active_step": plan.start_step})
            if plan.paper_id == paper_a:
                self.started_a.set()
                await asyncio.wait_for(self.started_b.wait(), timeout=1)
                self.a_finished = True
            else:
                assert self.started_a.is_set()
                assert self.a_finished is False
                self.started_b.set()
            record.update({"status": "complete", "completed_step": plan.run_until_step})
            record.pop("active_step", None)
            self._save()

    async def scenario() -> dict[str, Any]:
        dispatcher = DynamicDispatcher(
            root=root,
            journal_path=tmp_path / "dynamic-journal.json",
            lock_path=tmp_path / "dynamic.lock",
            codex_factory=CodexContext,
            max_parallel_papers=2,
        )
        plan_a = FigurePaperPlan(
            paper_id=paper_a,
            request_id="dynamic-a",
            source_tex=f"papers/{paper_a}/input.tex",
            start_step=2,
            run_until_step=2,
        )
        plan_b = FigurePaperPlan(
            paper_id=paper_b,
            request_id="dynamic-b",
            source_tex=f"papers/{paper_b}/input.tex",
            start_step=2,
            run_until_step=2,
        )

        def supply() -> list[FigurePaperPlan]:
            return [plan_a, plan_b] if dispatcher.started_a.is_set() else [plan_a]

        result = await dispatcher.run_dynamic(supply, poll_seconds=0.02, idle_grace_seconds=0.08)
        assert dispatcher.started_b.is_set()
        assert dispatcher.a_finished is True
        return result

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert set(result["papers"]) == {paper_a, paper_b}


def test_figure_workflow_versions_are_configurable_without_touching_tuned_prompts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from unified_paper_orchestrator import figure_workflow as workflow_module

    custom_dir = tmp_path / "custom-prompts"
    monkeypatch.setattr(workflow_module, "FIGURE_CUSTOM_PROMPT_DIR", custom_dir)
    store = FigureWorkflowStore(tmp_path / "workflow.json", custom_dir)
    assert store.inventory()["step_count"] == 3

    shortened = store.remove_step("builtin-01")
    assert shortened["step_count"] == 2
    assert [item["source_step"] for item in shortened["steps"]] == [2, 3]

    added = store.add_custom_step("终稿视觉一致性复核")
    custom = added["steps"][-1]
    saved = store.save_custom_prompt(custom["step_id"], "继续处理 <paper_id>，复核图注与正文引用。\n")
    assert saved["step_count"] == 3
    assert saved["steps"][-1]["prompt_ready"] is True
    assert validate_all_figure_prompts(config.FIGURE_ROOT)


def test_dispatcher_recovers_empty_completed_turn_with_one_safe_retry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _copy_tuned_prompts(root)
    _write_step2(root, PAPER_ID)
    baseline_result = validate_figure_step(root, PAPER_ID, 2)
    baseline = {
        str(item["path"]): dict(item) for item in baseline_result.artifacts
    }
    sdk = _FakeCodex(root)
    sdk.thread.turns.append(
        SimpleNamespace(
            id="turn-empty",
            status=SimpleNamespace(value="completed"),
            usage=None,
            items=[SimpleNamespace(type="userMessage")],
        )
    )
    journal = tmp_path / "journal.json"
    write_json_atomic(
        journal,
        {
            "schema": "unified-paper-orchestrator/figure-sdk-dispatch/v1",
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "request_id": "request-empty-recovery",
                    "source_tex": f"papers/{PAPER_ID}/input.tex",
                    "start_step": 2,
                    "run_until_step": 2,
                    "completed_step": 1,
                    "status": "waiting_event",
                    "active_step": 2,
                    "thread_id": sdk.thread.id,
                    "steps": {
                        "2": {
                            "state": "recovery_pending",
                            "turn_id": "turn-empty",
                            "output_baseline": baseline,
                        }
                    },
                    "revisions": {},
                }
            },
            "history": [],
        },
    )
    dispatcher = FigureSDKDispatcher(
        root=root,
        journal_path=journal,
        lock_path=tmp_path / "dispatch.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=1,
    )

    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                FigurePaperPlan(
                    paper_id=PAPER_ID,
                    request_id="request-empty-recovery",
                    source_tex=f"papers/{PAPER_ID}/input.tex",
                    start_step=2,
                    run_until_step=2,
                )
            ]
        )
    )

    assert result["ok"] is True
    assert len(sdk.thread.prompts) == 1
    step = result["papers"][PAPER_ID]["steps"]["2"]
    assert step["state"] == "complete"
    assert step["empty_completed_retry_count"] == 1
    assert step["empty_completed_turn_ids"] == ["turn-empty"]


def test_dispatcher_runs_different_papers_concurrently_up_to_limit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "figures"
    _copy_tuned_prompts(root)
    paper_ids = [
        "2026-07-15_101_parallel-a",
        "2026-07-15_102_parallel-b",
    ]
    for paper_id in paper_ids:
        paper = root / "papers" / paper_id
        paper.mkdir(parents=True)
        (paper / "input.tex").write_text("paper\n", encoding="utf-8")

    class TurnHandle:
        def __init__(self, thread: "Thread", turn: SimpleNamespace) -> None:
            self.thread = thread
            self.turn = turn
            self.id = turn.id

        async def run(self) -> SimpleNamespace:
            sdk.active += 1
            sdk.max_active = max(sdk.max_active, sdk.active)
            try:
                await asyncio.sleep(0.05)
                prompt = self.thread.prompts[-1]
                paper_id = next(value for value in paper_ids if value in prompt)
                _write_step2(root, paper_id)
                self.turn.status = SimpleNamespace(value="completed")
                return self.turn
            finally:
                sdk.active -= 1

    class Thread:
        def __init__(self, thread_id: str) -> None:
            self.id = thread_id
            self.name = ""
            self.turns: list[SimpleNamespace] = []
            self.prompts: list[str] = []

        async def set_name(self, name: str) -> None:
            self.name = name

        async def read(self, *, include_turns: bool = False) -> SimpleNamespace:
            return SimpleNamespace(
                thread=SimpleNamespace(
                    turns=list(self.turns) if include_turns else []
                )
            )

        async def turn(self, prompt: str, **_kwargs: Any) -> TurnHandle:
            self.prompts.append(prompt)
            turn = SimpleNamespace(
                id=f"{self.id}-turn-{len(self.turns) + 1}",
                status=SimpleNamespace(value="inProgress"),
                usage=None,
            )
            self.turns.append(turn)
            return TurnHandle(self, turn)

    class Codex:
        def __init__(self) -> None:
            self.threads: dict[str, Thread] = {}
            self.active = 0
            self.max_active = 0

        async def __aenter__(self) -> "Codex":
            return self

        async def __aexit__(
            self, _exc_type: Any, _exc: Any, _tb: Any
        ) -> None:
            return None

        async def thread_start(self, **_kwargs: Any) -> Thread:
            thread = Thread(f"thread-{len(self.threads) + 1}")
            self.threads[thread.id] = thread
            return thread

        async def thread_resume(self, thread_id: str, **_kwargs: Any) -> Thread:
            return self.threads[thread_id]

        async def thread_list(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(data=[], next_cursor=None)

    sdk = Codex()
    dispatcher = FigureSDKDispatcher(
        root=root,
        journal_path=tmp_path / "journal.json",
        lock_path=tmp_path / "dispatch.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=2,
    )
    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                FigurePaperPlan(
                    paper_id=paper_id,
                    request_id=f"request-{paper_id}",
                    source_tex=f"papers/{paper_id}/input.tex",
                    start_step=2,
                    run_until_step=2,
                )
                for paper_id in paper_ids
            ]
        )
    )

    assert result["ok"] is True
    assert sdk.max_active == 2


class _FakeRuntime:
    def __init__(self) -> None:
        self.started: list[list[str] | None] = []

    def ensure_ready(self) -> dict[str, Any]:
        return {"ok": True, "authenticated": True}

    def start_background(self, *, priority_paper_ids: list[str] | None = None) -> dict[str, Any]:
        self.started.append(priority_paper_ids)
        return {"ok": True, "started": True}

    def request_pause(self) -> dict[str, Any]:
        return {"ok": True, "pause_requested": True}


def test_adapter_refreshes_changed_source_and_auto_wakes_selected_paper(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "figures"
    state_path = root / "controller" / "figure_pipeline_state.json"
    papers = root / "papers"
    state_path.parent.mkdir(parents=True)
    write_json_atomic(state_path, {"papers": {}, "paper_queue": [], "commands": [], "settings": {}})
    project = {
        "label": "图表与插图",
        "root": root,
        "state_path": state_path,
        "prompt_path": root / "controller" / "FIGURE_SCHEDULER_PROMPT.md",
        "papers_dir": papers,
        "automation_ids": [],
        "primary_automation_id": "",
        "heartbeat_minutes": 5,
    }
    monkeypatch.setitem(figure_adapter_module.LEGACY_PROJECTS, "figures", project)
    monkeypatch.setattr(figure_adapter_module, "FIGURE_PIPELINE_LOCK_PATH", tmp_path / "pipeline.lock")
    runtime = _FakeRuntime()
    adapter = FigureAdapter(runtime=runtime)
    monkeypatch.setattr(adapter, "_pause_legacy_automation", lambda: {"status": "PAUSED"})
    source = tmp_path / "latest.tex"
    source.write_text("new source\n", encoding="utf-8")
    target = papers / PAPER_ID / "input.tex"
    target.parent.mkdir(parents=True)
    target.write_text("old source\n", encoding="utf-8")

    result = adapter.queue(PAPER_ID, str(source))

    assert target.read_text(encoding="utf-8") == "new source\n"
    assert result["scheduler_started"] is True
    assert result["source_changed"] is True
    assert runtime.started == [[PAPER_ID]]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["settings"]["dispatch_mode"] == "sdk_event_driven"
    assert state["settings"]["max_parallel_workers"] == 1
    assert state["papers"][PAPER_ID]["run_from_step"] == 2
    assert state["papers"][PAPER_ID]["sdk_request_id"]

    resumed = adapter.queue_from_step3(PAPER_ID)
    assert resumed["scheduler_started"] is True
    resumed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert resumed_state["papers"][PAPER_ID]["run_from_step"] == 3
    assert resumed_state["papers"][PAPER_ID]["step_done"] == 2
    assert runtime.started[-1] == [PAPER_ID]


def test_adapter_migrates_historical_task_from_valid_step2_and_archives_old_thread(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = tmp_path / "figures"
    state_path = root / "controller" / "figure_pipeline_state.json"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _write_step2(root, PAPER_ID)
    write_json_atomic(
        state_path,
        {
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "status": "migration_review",
                    "source_tex": f"papers/{PAPER_ID}/input.tex",
                    "step_done": 2,
                    "thread_id": "legacy-desktop-thread",
                    "thread_title": "legacy figure session",
                }
            },
            "paper_queue": [],
            "commands": [],
            "settings": {},
        },
    )
    project = {
        "label": "图表与插图",
        "root": root,
        "state_path": state_path,
        "prompt_path": root / "controller" / "FIGURE_SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": [],
        "primary_automation_id": "",
        "heartbeat_minutes": 5,
    }
    monkeypatch.setitem(figure_adapter_module.LEGACY_PROJECTS, "figures", project)
    monkeypatch.setattr(figure_adapter_module, "FIGURE_PIPELINE_LOCK_PATH", tmp_path / "pipeline.lock")
    runtime = _FakeRuntime()
    adapter = FigureAdapter(runtime=runtime)
    monkeypatch.setattr(adapter, "_pause_legacy_automation", lambda: {"status": "PAUSED"})

    result = adapter.queue_migration(PAPER_ID)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = state["papers"][PAPER_ID]
    assert result["migration_start_source_step"] == 3
    assert result["preserved_step2"] is True
    assert record["run_from_step"] == 3
    assert record["step_done"] == 2
    assert record["legacy_thread_id"] == "legacy-desktop-thread"
    assert record["legacy_thread_title"] == "legacy figure session"
    assert "thread_id" not in record
    assert state["commands"][-1]["params"]["legacy_migration"] is True
    assert runtime.started == [[PAPER_ID]]


def test_figure_live_rows_show_current_step_session_and_progress() -> None:
    rows = _figure_task_rows(
        {
            PAPER_ID: {
                "paper_id": PAPER_ID,
                "status": "running",
                "completed_step": 1,
                "active_step": 2,
                "run_until_step": 3,
                "thread_id": "thread-fixed-figure",
                "steps": {"2": {"state": "running", "turn_id": "turn-2"}},
            }
        }
    )
    assert len(rows) == 1
    assert rows[0]["step_label"] == "正在执行 Step 2 / 3"
    assert rows[0]["thread_id"] == "thread-fixed-figure"
    assert rows[0]["turn_id"] == "turn-2"
    assert rows[0]["progress"].endswith("1/3")


class _ReadyAccount:
    def status(self) -> dict[str, Any]:
        return {"ok": True, "authenticated": True, "isolated_home": True}


class _ProjectingDispatcher:
    def __init__(self, **kwargs: Any) -> None:
        self.callback = kwargs["progress_callback"]

    async def run_until_idle(self, plans: list[FigurePaperPlan]) -> dict[str, Any]:
        papers: dict[str, dict[str, Any]] = {}
        for plan in plans:
            snapshot = {
                "paper_id": plan.paper_id,
                "request_id": plan.request_id,
                "status": "complete",
                "completed_step": plan.run_until_step,
                "run_until_step": plan.run_until_step,
                "thread_id": "thread-runtime-projection",
                "thread_name": "upo-figures-test",
                "steps": {},
                "revisions": {},
            }
            self.callback("step_completed", plan.paper_id, snapshot)
            papers[plan.paper_id] = snapshot
        return {"ok": True, "papers": papers}


def test_runtime_projects_terminal_sdk_state_and_handles_command(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    state_path = root / "controller" / "figure_pipeline_state.json"
    input_tex = root / "papers" / PAPER_ID / "input.tex"
    input_tex.parent.mkdir(parents=True)
    input_tex.write_text("paper\n", encoding="utf-8")
    command = {
        "id": "figure-command-1",
        "action": "run_pipeline_late",
        "paper_id": PAPER_ID,
        "params": {},
        "status": "pending",
    }
    write_json_atomic(
        state_path,
        {
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "status": "queued",
                    "source_tex": f"papers/{PAPER_ID}/input.tex",
                    "run_from_step": 2,
                    "run_until_step": 3,
                    "sdk_request_id": command["id"],
                }
            },
            "paper_queue": [PAPER_ID],
            "commands": [command],
            "settings": {"scheduling_mode": "auto", "max_parallel_workers": 1},
        },
    )
    project = {
        "root": root,
        "papers_dir": root / "papers",
        "state_path": state_path,
        "prompt_path": root / "controller" / "FIGURE_SCHEDULER_PROMPT.md",
        "automation_ids": [],
    }
    runtime = FigureSDKRuntime(
        project,
        account_service=_ReadyAccount(),
        codex_home=tmp_path / "codex-home",
        journal_path=tmp_path / "journal.json",
        dispatch_lock_path=tmp_path / "dispatch.lock",
        pipeline_lock_path=tmp_path / "pipeline.lock",
        dispatcher_factory=_ProjectingDispatcher,
        legacy_automation_probe=lambda: True,
    )
    result = runtime.run([PAPER_ID])
    assert result["ok"] is True
    projected = json.loads(state_path.read_text(encoding="utf-8"))
    assert projected["papers"][PAPER_ID]["status"] == "complete"
    assert projected["papers"][PAPER_ID]["step_done"] == 3
    assert projected["papers"][PAPER_ID]["thread_id"] == "thread-runtime-projection"
    assert projected["paper_queue"] == []
    assert projected["commands"][0]["status"] == "done"


def test_runtime_revalidates_valid_blocked_output_without_model_turn(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    state_path = root / "controller" / "figure_pipeline_state.json"
    paper = root / "papers" / PAPER_ID
    paper.mkdir(parents=True)
    (paper / "input.tex").write_text("paper\n", encoding="utf-8")
    _write_step2(root, PAPER_ID)
    _write_step3(root, PAPER_ID)
    command = {
        "id": "figure-command-revalidate",
        "action": "run_pipeline_late",
        "paper_id": PAPER_ID,
        "params": {},
        "status": "blocked",
    }
    write_json_atomic(
        state_path,
        {
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "status": "blocked",
                    "source_tex": f"papers/{PAPER_ID}/input.tex",
                    "run_until_step": 3,
                    "step_done": 2,
                    "sdk_request_id": command["id"],
                    "thread_id": "thread-revalidate",
                    "blocker": "FIGURE_OUTPUT_CONTRACT_INVALID",
                }
            },
            "paper_queue": [],
            "commands": [command],
            "settings": {"scheduling_mode": "auto", "max_parallel_workers": 1},
        },
    )
    journal_path = tmp_path / "journal.json"
    write_json_atomic(
        journal_path,
        {
            "schema": "unified-paper-orchestrator/figure-sdk-dispatch/v1",
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "request_id": command["id"],
                    "status": "blocked",
                    "completed_step": 2,
                    "run_until_step": 3,
                    "thread_id": "thread-revalidate",
                    "blocker": "FIGURE_OUTPUT_CONTRACT_INVALID",
                    "blocker_message": "old validator rejected the manifest",
                    "steps": {"3": {"state": "blocked"}},
                    "revisions": {},
                }
            },
            "history": [],
        },
    )
    project = {
        "root": root,
        "papers_dir": root / "papers",
        "state_path": state_path,
        "prompt_path": root / "controller" / "FIGURE_SCHEDULER_PROMPT.md",
        "automation_ids": [],
    }
    runtime = FigureSDKRuntime(
        project,
        account_service=_ReadyAccount(),
        codex_home=tmp_path / "codex-home",
        journal_path=journal_path,
        dispatch_lock_path=tmp_path / "dispatch.lock",
        pipeline_lock_path=tmp_path / "pipeline.lock",
        legacy_automation_probe=lambda: True,
    )

    result = runtime.revalidate_completed_output(PAPER_ID)

    assert result["ok"] is True
    assert result["model_turn_started"] is False
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["papers"][PAPER_ID]["status"] == "complete"
    assert journal["papers"][PAPER_ID]["completed_step"] == 3
    assert journal["papers"][PAPER_ID]["thread_id"] == "thread-revalidate"
    assert journal["papers"][PAPER_ID]["steps"]["3"]["revalidated_without_model_turn"] is True
    projected = json.loads(state_path.read_text(encoding="utf-8"))
    assert projected["papers"][PAPER_ID]["status"] == "complete"
    assert projected["papers"][PAPER_ID]["step_done"] == 3
    assert projected["commands"][0]["status"] == "done"
