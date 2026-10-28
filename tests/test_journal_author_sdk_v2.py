from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unified_paper_orchestrator import config
from unified_paper_orchestrator import journal_author_workflow as workflow_module
from unified_paper_orchestrator.adapters import journal_author as adapter_module
from unified_paper_orchestrator.adapters.journal_author import JournalAuthorAdapter
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.journal_author_output_contract import (
    validate_journal_author_input,
    validate_journal_author_output,
)
from unified_paper_orchestrator.journal_author_prompt_integrity import (
    JOURNAL_AUTHOR_PROMPT_FILES,
    JOURNAL_AUTHOR_PROMPT_SHA256,
    validate_all_journal_author_prompts,
)
from unified_paper_orchestrator.journal_author_sdk_dispatch import (
    JournalAuthorPaperPlan,
    JournalAuthorSDKDispatcher,
)
from unified_paper_orchestrator.journal_author_sdk_runtime import JournalAuthorSDKRuntime
from unified_paper_orchestrator.journal_author_workflow import (
    JournalAuthorWorkflowStore,
    default_journal_author_workflow_snapshot,
    normalize_journal_author_workflow_snapshot,
    journal_author_workflow_with_mode,
)
from unified_paper_orchestrator.ui.main_window import _journal_author_task_rows


PAPER_ID = "2026-07-16_001_journal-author-sdk-test"


def _copy_tuned_prompts(root: Path) -> None:
    source = Path(config.LEGACY_PROJECTS["journal_author"]["root"])
    for relative in JOURNAL_AUTHOR_PROMPT_FILES.values():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def _write_input(root: Path, paper_id: str) -> None:
    paper = root / "papers" / paper_id
    work = paper / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{paper_id}.tex").write_text(
        "\\documentclass{article}\n\\title{Test}\n\\author{Alice Example}\n",
        encoding="utf-8",
    )
    (paper / "author_info.md").write_text(
        "# Author information\n\nStatus: READY\n\nAlice Example, Example University\n",
        encoding="utf-8",
    )
    (paper / "submission_metadata_fingerprint.json").write_text(
        json.dumps({"paper_id": paper_id, "sha256": "test-fingerprint"}, indent=2),
        encoding="utf-8",
    )


def _write_output(
    root: Path,
    paper_id: str,
    *,
    package_status: str = "READY_TO_SUBMIT",
    include_pdf: bool = True,
) -> None:
    paper = root / "papers" / paper_id
    package = paper / "package"
    package.mkdir(parents=True, exist_ok=True)
    (paper / "journal.md").write_text(
        "# Journal Decision\n\n"
        "## Paper ID\n\nTest\n\n"
        "## Paper Title\n\nTest paper\n\n"
        "## Recommended Journal\n\nExample Journal\n\n"
        "## Why this journal is the safest choice\n\nScope fit\n\n"
        "## Scope match\n\nStrong\n\n"
        "## Quartile / indexing evidence\n\nScopus\n\n"
        "## Similar published papers\n\nhttps://example.org/paper\n\n"
        "## APC and cost\n\nNo APC\n\n"
        "## Submission format notes\n\nLaTeX\n\n"
        "## Required files\n\nMain manuscript\n\n"
        "## Top 5 candidates\n\nFive verified candidates\n\n"
        "## Rejected journals\n\nNone\n\n"
        "## Final decision\n\nFinal decision: Recommended\n",
        encoding="utf-8",
    )
    (package / "main.tex").write_text(
        "\\documentclass{article}\n\\author{Alice Example}\n\\begin{document}Ready\\end{document}\n",
        encoding="utf-8",
    )
    if include_pdf:
        (package / "main.pdf").write_bytes(b"%PDF-1.4\nvalidated\n")
    for name in (
        "cover_letter.md",
        "declarations.md",
        "submission_checklist.md",
        "file_manifest.md",
    ):
        (package / name).write_text(f"# {name}\n\nComplete\n", encoding="utf-8")
    (package / "final_risk_report.md").write_text(
        f"# Final risk report\n\nPACKAGE_STATUS={package_status}\n",
        encoding="utf-8",
    )
    shutil.copy2(
        paper / "submission_metadata_fingerprint.json",
        package / "package_metadata_fingerprint.json",
    )


def test_tuned_journal_author_prompts_are_byte_identical() -> None:
    root = Path(config.LEGACY_PROJECTS["journal_author"]["root"])
    observed = validate_all_journal_author_prompts(root)
    assert {name: value.sha256 for name, value in observed.items()} == dict(
        JOURNAL_AUTHOR_PROMPT_SHA256
    )


def test_output_contract_accepts_complete_and_minor_fix_without_pdf(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    _write_input(root, PAPER_ID)
    assert validate_journal_author_input(root, PAPER_ID).ok is True
    _write_output(root, PAPER_ID)
    complete = validate_journal_author_output(root, PAPER_ID)
    assert complete.ok is True
    assert complete.step_done == 3
    assert complete.package_status == "READY_TO_SUBMIT"

    (root / "papers" / PAPER_ID / "package" / "main.pdf").unlink()
    (root / "papers" / PAPER_ID / "package" / "final_risk_report.md").write_text(
        "PACKAGE_STATUS=NEEDS_MINOR_FIX\n",
        encoding="utf-8",
    )
    minor = validate_journal_author_output(root, PAPER_ID)
    assert minor.ok is True
    assert minor.status == "complete"
    assert any("main.pdf" in warning for warning in minor.warnings)


class _TurnHandle:
    def __init__(self, thread: "_Thread", turn: SimpleNamespace) -> None:
        self.thread = thread
        self.turn_value = turn
        self.id = turn.id

    async def run(self) -> SimpleNamespace:
        self.thread.sdk.active += 1
        self.thread.sdk.max_active = max(self.thread.sdk.max_active, self.thread.sdk.active)
        try:
            await asyncio.sleep(0.02)
            _write_output(self.thread.sdk.root, self.thread.paper_id)
            self.turn_value.status = SimpleNamespace(value="completed")
            return self.turn_value
        finally:
            self.thread.sdk.active -= 1


class _Thread:
    def __init__(self, sdk: "_Codex", thread_id: str, paper_id: str) -> None:
        self.sdk = sdk
        self.id = thread_id
        self.paper_id = paper_id
        self.name = ""
        self.turns: list[SimpleNamespace] = []
        self.prompts: list[str] = []
        self.turn_kwargs: list[dict[str, Any]] = []

    async def set_name(self, name: str) -> None:
        self.name = name

    async def read(self, *, include_turns: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            thread=SimpleNamespace(turns=list(self.turns) if include_turns else [])
        )

    async def turn(self, prompt: str, **kwargs: Any) -> _TurnHandle:
        self.prompts.append(prompt)
        self.turn_kwargs.append(dict(kwargs))
        turn = SimpleNamespace(
            id=f"{self.id}-turn-{len(self.turns) + 1}",
            status=SimpleNamespace(value="inProgress"),
            usage=None,
        )
        self.turns.append(turn)
        return _TurnHandle(self, turn)


class _Codex:
    def __init__(self, root: Path, paper_ids: list[str]) -> None:
        self.root = root
        self.paper_ids = list(paper_ids)
        self.threads: dict[str, _Thread] = {}
        self.start_calls = 0
        self.resume_calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def __aenter__(self) -> "_Codex":
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        return None

    async def thread_start(self, **_kwargs: Any) -> _Thread:
        paper_id = self.paper_ids[self.start_calls]
        self.start_calls += 1
        thread = _Thread(self, f"thread-journal-{self.start_calls}", paper_id)
        self.threads[thread.id] = thread
        return thread

    async def thread_resume(self, thread_id: str, **_kwargs: Any) -> _Thread:
        self.resume_calls.append(thread_id)
        return self.threads[thread_id]

    async def thread_list(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(data=[], next_cursor=None)


class _HistoryRefreshTurnHandle(_TurnHandle):
    async def run(self) -> SimpleNamespace:
        self.thread.sdk.active += 1
        try:
            _write_output(self.thread.sdk.root, self.thread.paper_id)
            self.turn_value.status = SimpleNamespace(value="completed")
            self.turn_value.items = [
                SimpleNamespace(root=SimpleNamespace(type="agentMessage"))
            ]
            return SimpleNamespace(
                id=self.turn_value.id,
                status=SimpleNamespace(value="completed"),
                usage=None,
                items=[],
            )
        finally:
            self.thread.sdk.active -= 1


class _HistoryRefreshThread(_Thread):
    async def turn(self, prompt: str, **kwargs: Any) -> _HistoryRefreshTurnHandle:
        self.prompts.append(prompt)
        self.turn_kwargs.append(dict(kwargs))
        turn = SimpleNamespace(
            id=f"{self.id}-turn-{len(self.turns) + 1}",
            status=SimpleNamespace(value="inProgress"),
            usage=None,
            items=[SimpleNamespace(type="userMessage")],
        )
        self.turns.append(turn)
        return _HistoryRefreshTurnHandle(self, turn)


class _HistoryRefreshCodex(_Codex):
    async def thread_start(self, **_kwargs: Any) -> _HistoryRefreshThread:
        paper_id = self.paper_ids[self.start_calls]
        self.start_calls += 1
        thread = _HistoryRefreshThread(
            self,
            f"thread-journal-{self.start_calls}",
            paper_id,
        )
        self.threads[thread.id] = thread
        return thread


def test_dispatcher_uses_one_fixed_thread_and_one_combined_turn(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    _write_input(root, PAPER_ID)
    workflow = copy.deepcopy(default_journal_author_workflow_snapshot())
    workflow["default_execution_profile"] = {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "hard_token_limit": 0,
        "unavailable_policy": "block",
        "preset": "quality",
    }
    workflow = normalize_journal_author_workflow_snapshot(workflow)
    sdk = _Codex(root, [PAPER_ID])
    dispatcher = JournalAuthorSDKDispatcher(
        root=root,
        journal_path=tmp_path / "journal.json",
        lock_path=tmp_path / "dispatch.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=1,
        model="gpt-5.5",
    )

    result = asyncio.run(
        dispatcher.run_until_idle(
            [JournalAuthorPaperPlan(PAPER_ID, "request-1", workflow_snapshot=workflow)]
        )
    )

    assert result["ok"] is True
    assert sdk.start_calls == 1
    thread = next(iter(sdk.threads.values()))
    assert len(thread.prompts) == 1
    assert PAPER_ID in thread.prompts[0]
    assert "02_03_choose_journal_and_make_package.md" in thread.prompts[0]
    assert thread.turn_kwargs[0]["model"] == "gpt-5.6-sol"
    assert thread.turn_kwargs[0]["effort"].value == "high"
    record = result["papers"][PAPER_ID]
    assert record["thread_id"] == thread.id
    assert record["status"] == "complete"
    assert record["legacy_step_done"] == 3


def test_dispatcher_runs_different_papers_concurrently(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    paper_ids = [
        "2026-07-16_101_journal-parallel-a",
        "2026-07-16_102_journal-parallel-b",
    ]
    for paper_id in paper_ids:
        _write_input(root, paper_id)
    sdk = _Codex(root, paper_ids)
    dispatcher = JournalAuthorSDKDispatcher(
        root=root,
        journal_path=tmp_path / "parallel-journal.json",
        lock_path=tmp_path / "parallel.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=2,
    )
    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                JournalAuthorPaperPlan(paper_id, f"request-{paper_id}")
                for paper_id in paper_ids
            ]
        )
    )
    assert result["ok"] is True
    assert sdk.start_calls == 2
    assert sdk.max_active == 2
    assert all(len(thread.prompts) == 1 for thread in sdk.threads.values())


def test_split_workflow_runs_choose_gate_then_package_in_same_thread(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    _write_input(root, PAPER_ID)
    workflow = journal_author_workflow_with_mode(default_journal_author_workflow_snapshot(), "split")
    sdk = _Codex(root, [PAPER_ID])
    dispatcher = JournalAuthorSDKDispatcher(
        root=root,
        journal_path=tmp_path / "split-journal.json",
        lock_path=tmp_path / "split.lock",
        codex_factory=lambda: sdk,
        max_parallel_papers=1,
    )

    result = asyncio.run(
        dispatcher.run_until_idle(
            [JournalAuthorPaperPlan(PAPER_ID, "request-split", workflow_snapshot=workflow)]
        )
    )

    thread = next(iter(sdk.threads.values()))
    assert sdk.start_calls == 1
    assert len(thread.prompts) == 2
    assert "02_choose_journal.md" in thread.prompts[0]
    assert "03_make_submission_package.md" in thread.prompts[1]
    assert result["papers"][PAPER_ID]["status"] == "complete"
    assert result["papers"][PAPER_ID]["completed_step"] == 2


def test_completed_run_shell_is_refreshed_from_fixed_thread_history(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    _write_input(root, PAPER_ID)
    sdk = _HistoryRefreshCodex(root, [PAPER_ID])
    dispatcher = JournalAuthorSDKDispatcher(
        root=root,
        journal_path=tmp_path / "history-refresh-journal.json",
        lock_path=tmp_path / "history-refresh.lock",
        codex_factory=lambda: sdk,
    )

    result = asyncio.run(
        dispatcher.run_until_idle(
            [JournalAuthorPaperPlan(PAPER_ID, "request-history-refresh")]
        )
    )

    record = result["papers"][PAPER_ID]
    assert record["status"] == "complete"
    assert record["steps"]["1"]["turn_activity_source"] == "fixed_thread_history_after_run"
    assert len(next(iter(sdk.threads.values())).prompts) == 1


def test_empty_turn_blocker_recovers_existing_turn_without_resending_selection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    _write_input(root, PAPER_ID)
    _write_output(root, PAPER_ID)
    workflow = journal_author_workflow_with_mode(
        default_journal_author_workflow_snapshot(),
        "split",
    )
    sdk = _Codex(root, [])
    thread = _Thread(sdk, "thread-existing-retarget", PAPER_ID)
    thread.turns.append(
        SimpleNamespace(
            id="turn-existing-selection",
            status=SimpleNamespace(value="completed"),
            usage=None,
            items=[SimpleNamespace(root=SimpleNamespace(type="agentMessage"))],
        )
    )
    sdk.threads[thread.id] = thread
    journal_path = tmp_path / "retarget-recovery-journal.json"
    write_json_atomic(
        journal_path,
        {
            "schema": "unified-paper-orchestrator/journal-author-sdk-dispatch/v1",
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "request_id": "request-retarget-recovery",
                    "status": "blocked",
                    "completed_step": 0,
                    "active_step": 1,
                    "start_step": 1,
                    "run_until_step": 2,
                    "run_mode": "retarget",
                    "reuse_valid_artifacts": False,
                    "workflow_snapshot": workflow,
                    "workflow_version_id": workflow["version_id"],
                    "thread_id": thread.id,
                    "thread_name": "existing-retarget-thread",
                    "steps": {
                        "1": {
                            "state": "blocked",
                            "turn_id": "turn-existing-selection",
                        }
                    },
                    "blocker": "JOURNAL_AUTHOR_EMPTY_COMPLETED_TURN",
                    "blocker_message": "SDK result shell was empty",
                }
            },
            "history": [],
        },
    )
    dispatcher = JournalAuthorSDKDispatcher(
        root=root,
        journal_path=journal_path,
        lock_path=tmp_path / "retarget-recovery.lock",
        codex_factory=lambda: sdk,
    )

    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                JournalAuthorPaperPlan(
                    PAPER_ID,
                    "request-retarget-recovery",
                    workflow_snapshot=workflow,
                    reuse_valid_artifacts=False,
                    run_mode="retarget",
                )
            ]
        )
    )

    record = result["papers"][PAPER_ID]
    assert record["status"] == "complete"
    assert record["steps"]["1"]["turn_activity_source"] == "fixed_thread_history_recovery"
    assert len(thread.prompts) == 1
    assert "03_make_submission_package.md" in thread.prompts[0]
    assert "02_choose_journal.md" not in thread.prompts[0]


def test_selection_only_finishes_without_rebuilding_package(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    _write_input(root, PAPER_ID)
    workflow = journal_author_workflow_with_mode(default_journal_author_workflow_snapshot(), "split")
    sdk = _Codex(root, [PAPER_ID])
    dispatcher = JournalAuthorSDKDispatcher(
        root=root,
        journal_path=tmp_path / "selection-journal.json",
        lock_path=tmp_path / "selection.lock",
        codex_factory=lambda: sdk,
    )
    result = asyncio.run(
        dispatcher.run_until_idle(
            [
                JournalAuthorPaperPlan(
                    PAPER_ID,
                    "request-selection",
                    workflow_snapshot=workflow,
                    reuse_valid_artifacts=False,
                    start_step=1,
                    run_until_step=1,
                    run_mode="selection",
                )
            ]
        )
    )
    assert result["papers"][PAPER_ID]["status"] == "selection_complete"
    assert (root / "papers" / PAPER_ID / "package" / ".journal_selection_stale").is_file()


class _Runtime:
    def __init__(self) -> None:
        self.started: list[list[str] | None] = []

    def ensure_ready(self) -> dict[str, Any]:
        return {"ok": True, "authenticated": True}

    def start_background(
        self,
        *,
        priority_paper_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self.started.append(priority_paper_ids)
        return {"ok": True, "started": True}

    def request_pause(self) -> dict[str, Any]:
        return {"ok": True, "pause_requested": True}

    def request_pause_papers(self, paper_ids: list[str]) -> dict[str, Any]:
        return {"ok": True, "paper_ids": paper_ids}

    def revalidate_output(self, paper_id: str) -> dict[str, Any]:
        return {"ok": True, "paper_id": paper_id, "ai_called": False}


class _Workflow:
    def executable_snapshot(self) -> dict[str, Any]:
        return default_journal_author_workflow_snapshot()


def test_adapter_queue_immediately_wakes_selected_paper(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_path, {"papers": {}, "paper_queue": [], "commands": [], "settings": {}})
    project = {
        "label": "选刊与作者",
        "root": root,
        "state_path": state_path,
        "prompt_path": root / "controller" / "JOURNAL_SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": [],
        "primary_automation_id": "",
        "heartbeat_minutes": 5,
    }
    monkeypatch.setitem(adapter_module.LEGACY_PROJECTS, "journal_author", project)
    monkeypatch.setattr(adapter_module, "JOURNAL_AUTHOR_PIPELINE_LOCK_PATH", tmp_path / "pipeline.lock")
    upstream = tmp_path / "figure-submit"
    upstream.mkdir()
    (upstream / "paper.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    runtime = _Runtime()
    adapter = JournalAuthorAdapter(runtime=runtime, workflow_store=_Workflow())
    monkeypatch.setattr(adapter, "_pause_legacy_automation", lambda: {"status": "PAUSED"})

    result = adapter.queue(PAPER_ID, str(upstream))

    assert result["scheduler_started"] is True
    assert runtime.started == [[PAPER_ID]]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["paper_queue"] == [PAPER_ID]
    assert state["settings"]["dispatch_mode"] == "sdk_event_driven"
    assert state["settings"]["one_action_per_run"] is False
    assert state["papers"][PAPER_ID]["sdk_request_id"]


def test_adapter_retarget_runs_only_selection_and_package_steps(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = tmp_path / "journal-author"
    _copy_tuned_prompts(root)
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        state_path,
        {"papers": {}, "paper_queue": [], "commands": [], "settings": {}},
    )
    project = {
        "label": "journal-author",
        "root": root,
        "state_path": state_path,
        "prompt_path": root / "controller" / "JOURNAL_SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": [],
        "primary_automation_id": "",
        "heartbeat_minutes": 5,
    }
    monkeypatch.setitem(adapter_module.LEGACY_PROJECTS, "journal_author", project)
    monkeypatch.setattr(
        adapter_module,
        "JOURNAL_AUTHOR_PIPELINE_LOCK_PATH",
        tmp_path / "pipeline.lock",
    )
    upstream = tmp_path / "figure-submit"
    upstream.mkdir()
    (upstream / "paper.tex").write_text(
        "\\documentclass{article}\n",
        encoding="utf-8",
    )
    runtime = _Runtime()
    adapter = JournalAuthorAdapter(runtime=runtime, workflow_store=_Workflow())
    monkeypatch.setattr(adapter, "_pause_legacy_automation", lambda: {"status": "PAUSED"})

    result = adapter.queue(PAPER_ID, str(upstream), run_mode="retarget")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = state["papers"][PAPER_ID]
    assert result["run_mode"] == "retarget"
    assert result["run_from_position"] == 1
    assert result["run_until_position"] == 2
    assert record["workflow_snapshot"]["mode"] == "split"
    assert [step["source_step"] for step in record["workflow_snapshot"]["steps"]] == [2, 3]
    assert runtime.started == [[PAPER_ID]]


def test_dispatch_plan_accepts_retarget_mode_for_split_steps_2_and_3() -> None:
    workflow = journal_author_workflow_with_mode(
        default_journal_author_workflow_snapshot(),
        "split",
    )
    plan = JournalAuthorPaperPlan(
        paper_id=PAPER_ID,
        request_id="request-retarget",
        workflow_snapshot=workflow,
        reuse_valid_artifacts=False,
        start_step=1,
        run_until_step=2,
        run_mode="retarget",
    )

    plan.validate()


def test_workflow_model_versions_do_not_modify_tuned_prompt(tmp_path: Path) -> None:
    root = Path(config.LEGACY_PROJECTS["journal_author"]["root"])
    before = (root / JOURNAL_AUTHOR_PROMPT_FILES["combined"]).read_bytes()
    store = JournalAuthorWorkflowStore(tmp_path / "workflow.json")
    result = store.update_step_execution_profile(
        "builtin-02-03",
        {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "hard_token_limit": 123456,
            "unavailable_policy": "block",
            "preset": "custom",
        },
    )
    assert result["steps"][0]["effective_execution_profile"]["model"] == "gpt-5.6-sol"
    assert (root / JOURNAL_AUTHOR_PROMPT_FILES["combined"]).read_bytes() == before


def test_workflow_mode_and_custom_steps_are_versioned_without_touching_builtins(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    custom_dir = tmp_path / "custom-prompts"
    monkeypatch.setattr(workflow_module, "JOURNAL_AUTHOR_CUSTOM_PROMPT_DIR", custom_dir)
    store = JournalAuthorWorkflowStore(tmp_path / "workflow.json", custom_prompt_dir=custom_dir)
    split = store.set_mode("split")
    assert split["mode"] == "split"
    assert [item["source_step"] for item in split["steps"]] == [2, 3]
    added = store.add_custom_step("最终人工规则复核")
    custom = added["steps"][-1]
    assert custom["kind"] == "custom"
    saved = store.save_custom_prompt(custom["step_id"], "核对投稿文件清单；不要改写论文正文。\n")
    assert saved["steps"][-1]["prompt_ready"] is True
    assert saved["step_count"] == 3


def test_metadata_fingerprint_bytes_stay_stable_when_inputs_do_not_change(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = tmp_path / "journal-author"
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_path, {"papers": {}, "paper_queue": [], "commands": [], "settings": {}})
    project = {
        "label": "选刊与作者",
        "root": root,
        "state_path": state_path,
        "prompt_path": root / "controller" / "JOURNAL_SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": [],
        "primary_automation_id": "",
        "heartbeat_minutes": 5,
    }
    monkeypatch.setitem(adapter_module.LEGACY_PROJECTS, "journal_author", project)
    _write_input(root, PAPER_ID)
    adapter = JournalAuthorAdapter(runtime=_Runtime(), workflow_store=_Workflow())
    record = {"paper_id": PAPER_ID, "status": "READY", "journal_preferences": adapter._default_journal_preferences()}
    path = Path(adapter._write_metadata_fingerprint(PAPER_ID, record))
    first = path.read_bytes()
    first_created_at = json.loads(first)["created_at"]
    adapter._write_metadata_fingerprint(PAPER_ID, record)
    second = path.read_bytes()
    assert second == first
    assert json.loads(second)["created_at"] == first_created_at


def test_local_revalidation_uses_zero_ai_and_keeps_minor_fix_out_of_submission(tmp_path: Path) -> None:
    root = tmp_path / "journal-author"
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_path, {"papers": {PAPER_ID: {}}, "paper_queue": [], "commands": [], "settings": {}})
    project = {"root": root, "papers_dir": root / "papers", "state_path": state_path}
    _write_input(root, PAPER_ID)
    _write_output(root, PAPER_ID, package_status="NEEDS_MINOR_FIX", include_pdf=False)
    runtime = JournalAuthorSDKRuntime(
        project,
        account_service=SimpleNamespace(status=lambda: {"ok": True}),
        journal_path=tmp_path / "local-validation.json",
        dispatch_lock_path=tmp_path / "local-validation.lock",
        pipeline_lock_path=tmp_path / "pipeline.lock",
    )
    result = runtime.revalidate_output(PAPER_ID)
    assert result["ok"] is True
    assert result["ai_called"] is False
    assert result["status"] == "needs_user"
    pipeline = json.loads(state_path.read_text(encoding="utf-8"))
    assert pipeline["papers"][PAPER_ID]["workflow_status"] == "needs_user"


def test_background_failure_is_persisted_without_dropping_the_queued_task(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal-author"
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        state_path,
        {
            "papers": {
                PAPER_ID: {
                    "paper_id": PAPER_ID,
                    "workflow_status": "queued",
                }
            },
            "paper_queue": [PAPER_ID],
            "commands": [],
            "settings": {"scheduling_mode": "auto"},
        },
    )
    runtime = JournalAuthorSDKRuntime(
        {"root": root, "papers_dir": root / "papers", "state_path": state_path},
        account_service=SimpleNamespace(status=lambda: {"ok": True}),
        journal_path=tmp_path / "sdk-journal.json",
        dispatch_lock_path=tmp_path / "sdk.lock",
        pipeline_lock_path=tmp_path / "pipeline.lock",
    )

    runtime._persist_background_failure(
        "JOURNAL_AUTHOR_RUN_MODE_INVALID",
        priority_paper_ids={PAPER_ID},
        run_all=False,
    )

    pipeline = json.loads(state_path.read_text(encoding="utf-8"))
    record = pipeline["papers"][PAPER_ID]
    assert pipeline["paper_queue"] == [PAPER_ID]
    assert record["workflow_status"] == "queued"
    assert record["sdk_background_error"] == "JOURNAL_AUTHOR_RUN_MODE_INVALID"
    assert pipeline["settings"]["last_sdk_background_error"] == (
        "JOURNAL_AUTHOR_RUN_MODE_INVALID"
    )
    assert runtime.status(check_account=False)["status"] == "blocked"


def test_selected_resume_requeues_empty_turn_failure_without_new_request(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal-author"
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    request_id = "request-existing-empty-turn"
    state = {
        "papers": {
            PAPER_ID: {
                "paper_id": PAPER_ID,
                "workflow_status": "blocked",
                "status": "BLOCKED",
                "sdk_request_id": request_id,
                "blocker": "JOURNAL_AUTHOR_EMPTY_COMPLETED_TURN",
                "thread_id": "thread-existing",
            }
        },
        "paper_queue": [],
        "commands": [
            {
                "id": request_id,
                "paper_id": PAPER_ID,
                "action": "run_pipeline_02_03",
                "status": "blocked",
            }
        ],
        "settings": {},
    }

    recovered = JournalAuthorSDKRuntime._requeue_selected_recoverable_failures(
        state,
        {PAPER_ID},
    )

    assert recovered == [PAPER_ID]
    assert state["paper_queue"] == [PAPER_ID]
    assert state["papers"][PAPER_ID]["sdk_request_id"] == request_id
    assert state["papers"][PAPER_ID]["workflow_status"] == "queued"
    assert state["commands"][0]["id"] == request_id
    assert state["commands"][0]["status"] == "pending"


def test_selected_resume_retries_cancelled_turn_only_once_per_request(
    tmp_path: Path,
) -> None:
    request_id = "request-existing-cancelled-turn"
    state = {
        "papers": {
            PAPER_ID: {
                "paper_id": PAPER_ID,
                "workflow_status": "blocked",
                "status": "BLOCKED",
                "sdk_request_id": request_id,
                "blocker": "JOURNAL_AUTHOR_TURN_NOT_COMPLETED",
                "thread_id": "thread-existing",
            }
        },
        "paper_queue": [],
        "commands": [
            {
                "id": request_id,
                "paper_id": PAPER_ID,
                "action": "run_pipeline_02_03",
                "status": "blocked",
            }
        ],
        "settings": {},
    }

    first = JournalAuthorSDKRuntime._requeue_selected_recoverable_failures(
        state,
        {PAPER_ID},
    )
    assert first == [PAPER_ID]
    assert state["papers"][PAPER_ID]["sdk_terminal_retry_attempts"] == 1
    assert state["commands"][0]["status"] == "pending"

    state["papers"][PAPER_ID].update(
        {
            "workflow_status": "blocked",
            "status": "BLOCKED",
            "blocker": "JOURNAL_AUTHOR_TURN_NOT_COMPLETED",
        }
    )
    state["paper_queue"] = []
    state["commands"][0]["status"] = "blocked"
    second = JournalAuthorSDKRuntime._requeue_selected_recoverable_failures(
        state,
        {PAPER_ID},
    )
    assert second == []
    assert state["paper_queue"] == []
    assert state["commands"][0]["status"] == "blocked"


def test_adapter_only_marks_ready_to_submit_package_complete(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = tmp_path / "journal-author"
    state_path = root / "controller" / "journal_author_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_path, {"papers": {PAPER_ID: {}}, "paper_queue": [], "commands": [], "settings": {}})
    project = {
        "label": "选刊与作者",
        "root": root,
        "state_path": state_path,
        "prompt_path": root / "controller" / "JOURNAL_SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": [],
        "primary_automation_id": "",
        "heartbeat_minutes": 5,
    }
    monkeypatch.setitem(adapter_module.LEGACY_PROJECTS, "journal_author", project)
    monkeypatch.setattr(adapter_module, "JOURNAL_AUTHOR_SDK_JOURNAL_PATH", tmp_path / "empty-sdk-journal.json")
    _write_input(root, PAPER_ID)
    _write_output(root, PAPER_ID, package_status="NEEDS_MINOR_FIX", include_pdf=False)
    adapter = JournalAuthorAdapter(runtime=_Runtime(), workflow_store=_Workflow())
    minor = adapter.stage_for(PAPER_ID)
    assert minor["status"] == "needs_user"
    assert minor["ready_to_submit"] is False

    _write_output(root, PAPER_ID, package_status="READY_TO_SUBMIT", include_pdf=True)
    ready = adapter.stage_for(PAPER_ID)
    assert ready["status"] == "complete"
    assert ready["ready_to_submit"] is True


def test_live_rows_show_fixed_session_model_and_combined_progress() -> None:
    rows = _journal_author_task_rows(
        {
            PAPER_ID: {
                "paper_id": PAPER_ID,
                "request_id": "request-live",
                "status": "running",
                "active_step": 1,
                "thread_id": "thread-fixed-journal",
                "workflow_version_id": "workflow-v1",
                "steps": {
                    "1": {
                        "state": "running",
                        "turn_id": "turn-live",
                        "execution_profile": {
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "high",
                            "hard_token_limit": 0,
                        },
                    }
                },
            }
        }
    )
    assert len(rows) == 1
    assert rows[0]["status_label"] == "正在选刊/生成投稿包"
    assert rows[0]["step_label"] == "正在执行合并 Step 2-3"
    assert rows[0]["thread_id"] == "thread-fixed-journal"
    assert rows[0]["turn_id"] == "turn-live"
    assert rows[0]["model"] == "gpt-5.6-sol"
