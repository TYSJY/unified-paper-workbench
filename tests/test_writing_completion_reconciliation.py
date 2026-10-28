from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from unified_paper_orchestrator import orchestrator as orchestrator_module
from unified_paper_orchestrator import automation as automation_module
from unified_paper_orchestrator.adapters import base as base_module
from unified_paper_orchestrator.adapters import writing as writing_module
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic
from unified_paper_orchestrator.automation import SchedulerAutomationController
from unified_paper_orchestrator.writing_completion import CodexThreadCompletionReader


PAPER_ID = "2026-07-10_001_restart-envelope-contracts"
THREAD_ID = "019f4b88-162e-7563-8ea0-ed0ec96d5a85"


def project_config(root: Path) -> dict[str, object]:
    return {
        "label": "writing",
        "root": root,
        "state_path": root / "controller" / "pipeline_state.json",
        "prompt_path": root / "controller" / "SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": ["writing-scheduler"],
        "primary_automation_id": "writing-scheduler",
        "heartbeat_minutes": 5,
    }


def write_rollout(
    codex_home: Path,
    *,
    paper_id: str = PAPER_ID,
    step_done: int = 4,
    newer_turn_open: bool = False,
) -> Path:
    path = codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-2026-07-10T18-17-22-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-10T11:54:37.909Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-step-4",
                "completed_at": 1783684477,
                "last_agent_message": (f"Step complete.\nPIPELINE_ID={paper_id}\nSTEP_DONE={step_done}"),
            },
        }
    ]
    if newer_turn_open:
        next_turn_id = "019f4bde-50da-75f1-8b71-fb2ddb105803"
        records.extend(
            [
                {
                    "timestamp": "2026-07-10T11:54:59.900Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": next_turn_id,
                        "started_at": 1783684499,
                    },
                },
                {
                    "timestamp": "2026-07-10T11:54:59.950Z",
                    "type": "turn_context",
                    "payload": {"turn_id": next_turn_id, "cwd": str(codex_home)},
                },
                {
                    "timestamp": "2026-07-10T11:55:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    },
                },
                {
                    "timestamp": "2026-07-10T11:55:00.001Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "continue",
                        "images": [],
                        "local_images": [],
                        "text_elements": [],
                    },
                },
            ]
        )
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def write_state(root: Path, *, status: str = "running", step_done: int = 3) -> Path:
    state_path = root / "controller" / "pipeline_state.json"
    write_json_atomic(
        state_path,
        {
            "sessions": {
                PAPER_ID: {
                    "pipeline_id": PAPER_ID,
                    "thread_id": THREAD_ID,
                    "status": status,
                    "step_done": step_done,
                    "last_dispatched": 4,
                    "run_until_step": 10,
                    "evidence": "STEP_DONE=3; Step 4 dispatched",
                }
            },
            "settings": {"scheduling_mode": "manual"},
            "commands": [],
        },
    )
    return state_path


def make_adapter(root: Path, codex_home: Path) -> writing_module.WritingAdapter:
    project = project_config(root)
    runtime = Mock()
    runtime.assert_revalidation_transaction_resolved = Mock(return_value=None)
    runtime.prepare_pipeline_mutation.return_value = {"recovered": False, "status": "none"}
    runtime.ensure_ready.return_value = {"ok": True, "authenticated": True}
    runtime.start_background.return_value = {"started": True, "already_running": False}
    runtime.request_pause.return_value = {"pause_requested": True}
    with patch.object(writing_module, "LEGACY_PROJECTS", {"writing": project}):
        adapter = writing_module.WritingAdapter(
            CodexThreadCompletionReader(codex_home),
            runtime=runtime,
        )
    adapter.automation = Mock()
    adapter.automation.read_statuses.return_value = [
        {
            "id": "writing-scheduler",
            "path": str(root / "automations" / "writing-scheduler" / "automation.toml"),
            "status": "PAUSED",
        },
        {
            "id": "legacy-writing-scheduler",
            "path": str(root / "automations" / "legacy-writing-scheduler" / "automation.toml"),
            "status": "PAUSED",
        },
    ]
    adapter.automation.set_enabled.return_value = {"enabled": True, "updated": []}
    return adapter


def write_step_artifact(root: Path, step_done: int = 4) -> Path:
    names = {
        4: "draft_v1.tex",
        10: "draft_v6.tex",
    }
    path = root / "papers" / PAPER_ID / names[step_done]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\\documentclass{article}\n\\begin{document}\nVerified draft.\n\\end{document}\n", encoding="utf-8")
    return path


def test_reconciles_terminal_marker_and_artifact_without_using_last_dispatched(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    artifact = write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        result = adapter.reconcile_completed_threads()

    assert result["changed"] is True
    assert result["updated"] == [
        {
            "paper_id": PAPER_ID,
            "thread_id": THREAD_ID,
            "step_done": 4,
            "status": "queued",
        }
    ]
    session = read_json(state_path, {})["sessions"][PAPER_ID]
    assert session["step_done"] == 4
    assert session["status"] == "queued"
    assert session["last_dispatched"] == 4
    reconciliation = session["completion_reconciliation"]
    assert reconciliation["source"] == "codex_task_complete_marker_and_artifact_sha256"
    assert reconciliation["turn_id"] == "turn-step-4"
    assert reconciliation["artifacts"] == [
        {
            "path": f"papers/{PAPER_ID}/draft_v1.tex",
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size": artifact.stat().st_size,
        }
    ]
    projection = adapter.stage_for(PAPER_ID)
    assert projection["step_done"] == 4
    assert projection["raw_status"] == "queued"
    assert projection["completion_reconciliation"]["turn_id"] == "turn-step-4"


def test_reconciliation_is_idempotent_and_does_not_rewrite_state(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        assert adapter.reconcile_completed_threads()["changed"] is True
        before = state_path.read_bytes()
        with patch.object(adapter, "save_state", wraps=adapter.save_state) as save:
            result = adapter.reconcile_completed_threads()

    assert result["changed"] is False
    save.assert_not_called()
    assert state_path.read_bytes() == before


def test_same_step_artifact_change_is_not_silently_reconciled(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    artifact = write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        assert adapter.reconcile_completed_threads()["changed"] is True
        state_before = state_path.read_bytes()
        artifact.write_text(
            "\\documentclass{article}\n\\begin{document}\nExternally changed.\n\\end{document}\n",
            encoding="utf-8",
        )
        with patch.object(adapter, "save_state", wraps=adapter.save_state) as save:
            result = adapter.reconcile_completed_threads()

    assert result["changed"] is False
    assert result["skipped"][PAPER_ID] == "same_step_artifact_provenance_conflict"
    save.assert_not_called()
    assert state_path.read_bytes() == state_before


@pytest.mark.parametrize("active_id", ["writing-scheduler", "legacy-writing-scheduler"])
def test_active_primary_or_legacy_automation_makes_reconciliation_read_only(
    tmp_path: Path,
    active_id: str,
) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)
    for config in adapter.automation.read_statuses.return_value:
        if config["id"] == active_id:
            config["status"] = "ACTIVE"
    before = state_path.read_bytes()

    with patch.object(adapter, "save_state_if_unchanged") as save:
        result = adapter.reconcile_completed_threads()

    assert result["status"] == "scheduler_active_no_write"
    assert result["changed"] is False
    assert result["skipped"]["*"] == "scheduler_active_no_write"
    assert [item["id"] for item in result["automation"]["active"]] == [active_id]
    save.assert_not_called()
    assert state_path.read_bytes() == before


def test_existing_automation_with_unknown_status_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    adapter = make_adapter(root, codex_home)
    adapter.automation.read_statuses.return_value = [
        {"id": "writing-scheduler", "path": "test", "status": "MISSING", "exists": True}
    ]
    before = state_path.read_bytes()

    result = adapter.reconcile_completed_threads()

    assert result["status"] == "scheduler_status_unreadable_no_write"
    assert result["automation"]["unknown"][0]["id"] == "writing-scheduler"
    assert state_path.read_bytes() == before


def test_missing_obsolete_automation_alias_does_not_block_verified_paused_primary(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    write_state(root)
    adapter = make_adapter(root, codex_home)
    adapter.automation.read_statuses.return_value = [
        {"id": "writing-scheduler", "path": "primary", "status": "PAUSED", "exists": True},
        {"id": "obsolete-writing-scheduler", "path": "obsolete", "status": "MISSING", "exists": False},
    ]

    guard = adapter._writing_scheduler_guard()

    assert guard["safe"] is True
    assert guard["status"] == "paused"
    assert [item["id"] for item in guard["missing"]] == ["obsolete-writing-scheduler"]


def test_read_only_automation_inventory_sees_primary_and_legacy_active_config(
    tmp_path: Path,
) -> None:
    root = tmp_path / "writing"
    automation_root = tmp_path / "automations"
    project = project_config(root)
    project["automation_ids"] = ["writing-scheduler", "legacy-writing-scheduler"]
    for automation_id, status in (
        ("writing-scheduler", "PAUSED"),
        ("legacy-writing-scheduler", "ACTIVE"),
    ):
        path = automation_root / automation_id / "automation.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            f'id = "{automation_id}"\nstatus = "{status}"\n',
            encoding="utf-8",
        )
    before = {
        path.relative_to(automation_root).as_posix(): path.read_bytes()
        for path in automation_root.rglob("automation.toml")
    }
    controller = SchedulerAutomationController()
    controller.projects = {"writing": project}

    with patch.object(automation_module, "AUTOMATIONS_ROOT", automation_root):
        statuses = controller.read_statuses("writing")
        active = controller.active_statuses("writing")

    assert {item["id"] for item in statuses} == {
        "writing-scheduler",
        "legacy-writing-scheduler",
    }
    assert [item["id"] for item in active] == ["legacy-writing-scheduler"]
    after = {
        path.relative_to(automation_root).as_posix(): path.read_bytes()
        for path in automation_root.rglob("automation.toml")
    }
    assert after == before


def test_automation_becoming_active_before_replace_aborts_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)
    paused = adapter.automation.read_statuses.return_value
    active = [dict(item) for item in paused]
    active[1]["status"] = "ACTIVE"
    adapter.automation.read_statuses.side_effect = [paused, paused, active]
    before = state_path.read_bytes()

    with patch.object(adapter, "save_state_if_unchanged") as save:
        result = adapter.reconcile_completed_threads()

    assert result["status"] == "scheduler_active_no_write"
    assert result["attempts"] == 1
    save.assert_not_called()
    assert state_path.read_bytes() == before


def test_byte_conflict_retries_and_merges_only_target_session(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)
    real_conditional_write = base_module.write_json_atomic_if_unchanged
    calls = 0

    def inject_scheduler_change(
        path: Path,
        data: object,
        expected_sha256: str | None,
    ) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            latest = read_json(state_path, {})
            latest["sessions"][PAPER_ID]["scheduler_nonce"] = "keep-target-field"
            latest["sessions"]["other-paper"] = {
                "pipeline_id": "other-paper",
                "status": "queued",
                "step_done": 7,
            }
            latest["settings"]["scheduler_revision"] = 41
            latest["commands"].append({"id": "scheduler-command", "action": "resume_session", "status": "pending"})
            write_json_atomic(state_path, latest)
        return real_conditional_write(path, data, expected_sha256)

    with (
        patch.object(base_module, "backup_file", return_value=""),
        patch.object(
            base_module,
            "write_json_atomic_if_unchanged",
            side_effect=inject_scheduler_change,
        ),
    ):
        result = adapter.reconcile_completed_threads()

    assert result["status"] == "reconciled"
    assert result["attempts"] == 2
    assert calls == 2
    state = read_json(state_path, {})
    assert state["sessions"][PAPER_ID]["step_done"] == 4
    assert state["sessions"][PAPER_ID]["scheduler_nonce"] == "keep-target-field"
    assert state["sessions"]["other-paper"]["step_done"] == 7
    assert state["settings"]["scheduler_revision"] == 41
    assert state["commands"] == [{"id": "scheduler-command", "action": "resume_session", "status": "pending"}]


def test_repeated_byte_conflicts_exhaust_without_overwriting_state(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)
    before = state_path.read_bytes()

    with patch.object(adapter, "save_state_if_unchanged", return_value=False) as save:
        result = adapter.reconcile_completed_threads()

    assert result["status"] == "state_changed_retry_exhausted"
    assert result["attempts"] == writing_module._RECONCILIATION_MAX_ATTEMPTS
    assert save.call_count == writing_module._RECONCILIATION_MAX_ATTEMPTS
    assert state_path.read_bytes() == before


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("artifact_missing", "expected_artifact_missing_or_unfingerprintable"),
        ("pipeline_mismatch", "pipeline_marker_mismatch"),
        ("newer_turn", "newer_turn_open"),
    ],
)
def test_fails_closed_without_both_terminal_marker_and_expected_artifact(
    tmp_path: Path,
    case: str,
    expected_reason: str,
) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    if case != "artifact_missing":
        write_step_artifact(root)
    write_rollout(
        codex_home,
        paper_id="different-paper" if case == "pipeline_mismatch" else PAPER_ID,
        newer_turn_open=case == "newer_turn",
    )
    adapter = make_adapter(root, codex_home)
    before = state_path.read_bytes()

    result = adapter.reconcile_completed_threads()

    assert result["changed"] is False
    assert result["skipped"][PAPER_ID] == expected_reason
    assert state_path.read_bytes() == before
    assert read_json(state_path, {})["sessions"][PAPER_ID]["step_done"] == 3


@pytest.mark.parametrize(
    ("record", "boundary"),
    [
        (
            {
                "timestamp": "2026-07-10T11:55:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "019f4bde-50da-75f1-8b71-fb2ddb105803",
                    "started_at": 1783684500,
                },
            },
            "task_started",
        ),
        (
            {
                "timestamp": "2026-07-10T11:55:00.000Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "019f4bde-50da-75f1-8b71-fb2ddb105803",
                    "cwd": "D:/Documents/project",
                },
            },
            "turn_context",
        ),
        (
            {
                "timestamp": "2026-07-10T11:55:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "continue"}],
                },
            },
            "response_item_user_message",
        ),
        (
            {
                "timestamp": "2026-07-10T11:55:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "continue",
                    "images": [],
                    "local_images": [],
                    "text_elements": [],
                },
            },
            "user_message",
        ),
    ],
)
def test_real_rollout_turn_boundary_records_fail_closed(
    tmp_path: Path,
    record: dict[str, object],
    boundary: str,
) -> None:
    codex_home = tmp_path / ".codex"
    rollout = write_rollout(codex_home)
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    result = CodexThreadCompletionReader(codex_home).inspect(THREAD_ID)

    assert result["status"] == "newer_turn_open"
    assert result["newer_turn_evidence"]["boundary"] == boundary


def test_later_real_schema_task_complete_closes_the_new_turn(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    rollout = write_rollout(codex_home, newer_turn_open=True)
    next_turn_id = "019f4bde-50da-75f1-8b71-fb2ddb105803"
    terminal = {
        "timestamp": "2026-07-10T11:56:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": next_turn_id,
            "completed_at": 1783684560,
            "duration_ms": 60000,
            "time_to_first_token_ms": 1000,
            "last_agent_message": (f"Step complete.\nPIPELINE_ID={PAPER_ID}\nSTEP_DONE=4"),
        },
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(terminal, ensure_ascii=False) + "\n")

    result = CodexThreadCompletionReader(codex_home).inspect(THREAD_ID)

    assert result["status"] == "terminal"
    assert result["turn_id"] == next_turn_id


def test_preserves_explicit_pause_but_advances_completed_worker_step(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root, status="paused")
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        adapter.reconcile_completed_threads()

    session = read_json(state_path, {})["sessions"][PAPER_ID]
    assert session["step_done"] == 4
    assert session["status"] == "paused"
    assert session["evidence"] == "STEP_DONE=3; Step 4 dispatched"


def test_terminal_marker_releases_running_status_even_when_step_was_already_persisted(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root, status="running", step_done=4)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        result = adapter.reconcile_completed_threads()

    session = read_json(state_path, {})["sessions"][PAPER_ID]
    assert result["changed"] is True
    assert session["step_done"] == 4
    assert session["status"] == "queued"


def test_step_ten_becomes_complete_and_clears_run_target(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root, step_done=9)
    write_step_artifact(root, step_done=10)
    write_rollout(codex_home, step_done=10)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        adapter.reconcile_completed_threads()

    session = read_json(state_path, {})["sessions"][PAPER_ID]
    assert session["step_done"] == 10
    assert session["status"] == "complete"
    assert "run_until_step" not in session


def test_repeated_resume_reuses_reconciled_step_and_pending_command(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    write_step_artifact(root)
    write_rollout(codex_home)
    adapter = make_adapter(root, codex_home)

    with patch.object(base_module, "backup_file", return_value=""):
        adapter.queue(PAPER_ID)
        adapter.queue(PAPER_ID)

    state = read_json(state_path, {})
    session = state["sessions"][PAPER_ID]
    assert session["step_done"] == 4
    assert session["status"] == "queued"
    assert session["last_dispatched"] == 4
    pending = [item for item in state["commands"] if item.get("status") == "pending"]
    assert len(pending) == 1
    assert pending[0]["action"] == "resume_session"


def test_queue_refuses_to_mutate_while_any_writing_automation_is_active(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    adapter = make_adapter(root, codex_home)
    adapter.automation.read_statuses.return_value[1]["status"] = "ACTIVE"
    before = state_path.read_bytes()

    with pytest.raises(RuntimeError, match="scheduler_active_no_write"):
        adapter.queue(PAPER_ID)

    adapter.automation.set_enabled.assert_called_once()
    assert adapter.automation.set_enabled.call_args.args[1] is False
    assert state_path.read_bytes() == before


def test_queue_many_persists_complete_serial_backlog_before_one_start(tmp_path: Path) -> None:
    root = tmp_path / "writing"
    codex_home = tmp_path / ".codex"
    state_path = write_state(root)
    adapter = make_adapter(root, codex_home)
    second_id = "2026-07-13_002_second-paper"

    result = adapter.queue_many([PAPER_ID, second_id, PAPER_ID])

    state = read_json(state_path, {})
    assert result["paper_ids"] == [PAPER_ID, second_id]
    assert [item["pipeline_id"] for item in result["commands"]] == [PAPER_ID, second_id]
    assert state["active_pipeline_id"] == PAPER_ID
    assert state["sessions"][PAPER_ID]["status"] == "queued"
    assert state["sessions"][second_id]["status"] == "queued"
    pending = [item for item in state["commands"] if item.get("status") == "pending"]
    assert [item["pipeline_id"] for item in pending] == [PAPER_ID, second_id]
    adapter.automation.set_enabled.assert_called_once()
    assert adapter.automation.set_enabled.call_args.args[1] is False
    expected_keys = [f"session:{PAPER_ID}", f"session:{second_id}"]
    assert result["priority_paper_keys"] == expected_keys
    adapter.runtime.start_background.assert_called_once_with(priority_paper_keys=expected_keys)


@pytest.mark.parametrize("entrypoint", ["sync", "scheduler_status"])
def test_status_and_heartbeat_entrypoints_reconcile_before_projecting(entrypoint: str) -> None:
    orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
    sentinel = RuntimeError("reconciliation-called-first")
    orchestrator._reconcile_writing_completion = Mock(side_effect=sentinel)

    with pytest.raises(RuntimeError, match="reconciliation-called-first"):
        getattr(orchestrator, entrypoint)()

    orchestrator._reconcile_writing_completion.assert_called_once_with()
