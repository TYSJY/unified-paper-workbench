from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.automation import writing_parallel_launcher_prompt
from unified_paper_orchestrator.writing_dispatch import WritingDispatchError, WritingParallelDispatcher


PAPER_A = "2026-07-13_001_parallel-a"
PAPER_B = "2026-07-13_002_parallel-b"


class FakeCompletionReader:
    def __init__(self, values: dict[str, dict]) -> None:
        self.values = values

    def inspect(self, thread_id: str) -> dict:
        return dict(self.values.get(thread_id, {"status": "rollout_missing", "thread_id": thread_id}))


def terminal(thread_id: str, paper_id: str, step: int, turn: str) -> dict:
    return {
        "status": "terminal",
        "thread_id": thread_id,
        "pipeline_id": paper_id,
        "step_done": step,
        "timestamp": f"2026-07-13T00:0{step}:00+00:00",
        "turn_id": turn,
        "rollout_file": f"{thread_id}.jsonl",
        "completed_at": None,
    }


def make_dispatcher(tmp_path: Path, *, cap: int = 2, commands: list[dict] | None = None):
    root = tmp_path / "writing"
    controller = root / "controller"
    papers = root / "papers"
    controller.mkdir(parents=True)
    papers.mkdir(parents=True)
    (root / "research_profile.md").write_text("edge llm\n", encoding="utf-8")
    scheduler = controller / "SCHEDULER_PROMPT.md"
    scheduler.write_text(
        """## Exact Step 1 Prompt

```text
run exact step one
```

### Step 2

```text
continue <idea-id> with exact step two
PIPELINE_ID=<idea-id>
STEP_DONE=2
```

### Step 3

```text
continue <idea-id> with exact step three
PIPELINE_ID=<idea-id>
STEP_DONE=3
```
""",
        encoding="utf-8",
    )
    state_path = controller / "pipeline_state.json"
    state = {
        "settings": {
            "scheduling_mode": "auto",
            "dispatch_mode": "parallel_workers",
            "writing_dispatch_protocol": "visible_slots_v1",
            "max_parallel_workers": cap,
        },
        "sessions": {},
        "commands": list(commands or []),
    }
    write_json_atomic(state_path, state)
    reader = FakeCompletionReader({})
    dispatcher = WritingParallelDispatcher(
        project={
            "root": root,
            "papers_dir": papers,
            "state_path": state_path,
            "prompt_path": scheduler,
            "primary_automation_id": "writing-test",
        },
        dispatch_path=tmp_path / "dispatch.json",
        lock_path=tmp_path / "dispatch.lock",
        completion_reader=reader,
        launcher_verifier=lambda: True,
        automation_active_probe=lambda: True,
    )
    return dispatcher, reader, state_path, scheduler


def add_step1_session(state_path: Path, reader: FakeCompletionReader, root: Path, paper_id: str, thread_id: str) -> None:
    idea = root / "ideas" / f"{paper_id}.md"
    idea.parent.mkdir(parents=True, exist_ok=True)
    idea.write_text(f"# {paper_id}\n", encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["sessions"][paper_id] = {
        "pipeline_id": paper_id,
        "thread_id": thread_id,
        "status": "queued",
        "step_done": 1,
        "run_until_step": 10,
    }
    write_json_atomic(state_path, state)
    reader.values[thread_id] = terminal(thread_id, paper_id, 1, f"turn-{paper_id}")


def test_prepare_claims_distinct_papers_up_to_cap_and_second_sweep_does_not_duplicate(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path, cap=2)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    add_step1_session(state_path, reader, dispatcher.root, PAPER_B, "thread-b1")

    first = dispatcher.prepare()
    second = dispatcher.prepare()

    assert first["ok"] is True
    assert first["ticket_count"] == 2
    assert {item["paper_id"] for item in first["tickets"]} == {PAPER_A, PAPER_B}
    assert {item["thread_id"] for item in first["tickets"]} == {"thread-a1", "thread-b1"}
    assert all(item["step"] == 2 for item in first["tickets"])
    assert all("pipeline_state.json" in item["prompt"] for item in first["tickets"])
    assert second["ticket_count"] == 0
    assert second["active_before"] == 2


def test_concurrent_prepare_calls_never_exceed_cap(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path, cap=2)
    for index, paper_id in enumerate((PAPER_A, PAPER_B, "2026-07-13_003_parallel-c"), start=1):
        add_step1_session(state_path, reader, dispatcher.root, paper_id, f"thread-{index}x")
    outputs: list[dict] = []

    threads = [threading.Thread(target=lambda: outputs.append(dispatcher.prepare())) for _ in range(2)]
    for item in threads:
        item.start()
    for item in threads:
        item.join()

    assert sum(result.get("ticket_count", 0) for result in outputs) == 2
    assert dispatcher.status()["active_count"] == 2


def test_mark_uncertain_and_record_dispatch_require_exact_claim_token(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    ticket = dispatcher.prepare()["tickets"][0]

    with pytest.raises(WritingDispatchError, match="stale"):
        dispatcher.mark_uncertain(ticket["attempt_id"], "wrong")
    begun = dispatcher.mark_uncertain(ticket["attempt_id"], ticket["claim_token"])
    sent = dispatcher.record_dispatch(ticket["attempt_id"], ticket["claim_token"], "thread-a1")

    assert begun["state"] == "send_uncertain"
    assert sent["state"] == "dispatched"
    with pytest.raises(WritingDispatchError, match="only a definitely unsent"):
        dispatcher.release_reserved(ticket["attempt_id"], ticket["claim_token"])


def test_serializes_new_session_creation_even_when_cap_is_four(tmp_path: Path) -> None:
    commands = [
        {"id": "new-1", "action": "new_session", "pipeline_id": "", "status": "pending", "params": {}},
        {"id": "new-2", "action": "new_session", "pipeline_id": "", "status": "pending", "params": {}},
    ]
    dispatcher, _reader, _state_path, _scheduler = make_dispatcher(tmp_path, cap=4, commands=commands)

    result = dispatcher.prepare()

    assert result["ticket_count"] == 1
    assert result["tickets"][0]["action"] == "create_thread"
    assert result["tickets"][0]["step"] == 1


def test_newer_exact_terminal_reconciles_claim_and_frees_slot(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path, cap=1)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    ticket = dispatcher.prepare()["tickets"][0]
    dispatcher.mark_uncertain(ticket["attempt_id"], ticket["claim_token"])
    dispatcher.record_dispatch(ticket["attempt_id"], ticket["claim_token"], "thread-a1")
    refs = dispatcher.root / "refs" / PAPER_A
    refs.mkdir(parents=True)
    for name in ("references.md", "references.bib", "literature_map.md", "novelty_check.md"):
        (refs / name).write_text(f"{name}\n", encoding="utf-8")
    reader.values["thread-a1"] = terminal("thread-a1", PAPER_A, 2, "turn-new-step2")

    result = dispatcher.prepare()
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["sessions"][PAPER_A]["step_done"] == 2
    assert dispatcher.status()["active_count"] == 0
    assert result["ticket_count"] == 0  # Step 3 gate is absent and blocks only this paper.
    assert state["sessions"][PAPER_A]["status"] == "paused"


def test_expired_unsent_reservation_rejects_delayed_old_token(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    old = dispatcher.prepare()["tickets"][0]
    dispatch = json.loads(dispatcher.dispatch_path.read_text(encoding="utf-8"))
    dispatch["claims"][old["attempt_id"]]["reserved_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).isoformat()
    write_json_atomic(dispatcher.dispatch_path, dispatch)

    replacement = dispatcher.prepare()["tickets"][0]

    assert replacement["attempt_id"] != old["attempt_id"]
    with pytest.raises(WritingDispatchError, match="stale"):
        dispatcher.mark_uncertain(old["attempt_id"], old["claim_token"])


def test_cap_decrease_drains_unbegun_reservation_without_interrupting_begun_claim(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path, cap=2)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    add_step1_session(state_path, reader, dispatcher.root, PAPER_B, "thread-b1")
    tickets = dispatcher.prepare()["tickets"]
    first, second = tickets
    dispatcher.mark_uncertain(first["attempt_id"], first["claim_token"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settings"]["max_parallel_workers"] = 1
    write_json_atomic(state_path, state)

    with pytest.raises(WritingDispatchError, match="cap no longer"):
        dispatcher.mark_uncertain(second["attempt_id"], second["claim_token"])

    status = dispatcher.status()
    assert status["configured_cap"] == 1
    assert status["active_count"] == 1


def test_pause_between_prepare_and_send_cancels_safe_reservation(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    ticket = dispatcher.prepare()["tickets"][0]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settings"]["scheduling_mode"] = "manual"
    write_json_atomic(state_path, state)

    with pytest.raises(WritingDispatchError, match="paused"):
        dispatcher.mark_uncertain(ticket["attempt_id"], ticket["claim_token"])

    assert dispatcher.status()["active_count"] == 0


def test_offline_reconciliation_adopts_exact_terminal_step1_and_command(tmp_path: Path) -> None:
    commands = [
        {"id": "new-1", "action": "new_session", "pipeline_id": "", "status": "pending", "params": {}},
    ]
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path, commands=commands)
    ticket = dispatcher.prepare()["tickets"][0]
    dispatcher.mark_uncertain(ticket["attempt_id"], ticket["claim_token"])
    idea = dispatcher.root / "ideas" / f"{PAPER_A}.md"
    idea.parent.mkdir(parents=True)
    idea.write_text(f"# {PAPER_A}\n", encoding="utf-8")
    reader.values["thread-adopt"] = terminal("thread-adopt", PAPER_A, 1, "turn-adopt")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settings"]["scheduling_mode"] = "manual"
    write_json_atomic(state_path, state)
    dispatcher.automation_active_probe = lambda: False

    result = dispatcher.reconcile_uncertain_adopt_terminal(
        ticket["attempt_id"],
        paper_id=PAPER_A,
        thread_id="thread-adopt",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["resolution"] == "adopted_existing_terminal"
    assert state["sessions"][PAPER_A]["thread_id"] == "thread-adopt"
    assert state["sessions"][PAPER_A]["step_done"] == 1
    assert state["sessions"][PAPER_A]["status"] == "queued"
    assert state["commands"][0]["status"] == "done"
    assert state["commands"][0]["adopted_pipeline_id"] == PAPER_A
    assert dispatcher.status()["active_count"] == 0


def test_offline_reconciliation_closes_proven_unsent_continue(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    ticket = dispatcher.prepare()["tickets"][0]
    dispatcher.mark_uncertain(ticket["attempt_id"], ticket["claim_token"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settings"]["scheduling_mode"] = "manual"
    write_json_atomic(state_path, state)
    dispatcher.automation_active_probe = lambda: False

    result = dispatcher.reconcile_uncertain_unsent(ticket["attempt_id"])
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["resolution"] == "verified_unsent"
    assert state["sessions"][PAPER_A]["step_done"] == 1
    assert dispatcher.status()["active_count"] == 0


def test_offline_reconciliation_refuses_unsent_when_next_output_exists(tmp_path: Path) -> None:
    dispatcher, reader, state_path, _scheduler = make_dispatcher(tmp_path)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    ticket = dispatcher.prepare()["tickets"][0]
    dispatcher.mark_uncertain(ticket["attempt_id"], ticket["claim_token"])
    refs = dispatcher.root / "refs" / PAPER_A
    refs.mkdir(parents=True)
    (refs / "references.md").write_text("partial\n", encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settings"]["scheduling_mode"] = "manual"
    write_json_atomic(state_path, state)
    dispatcher.automation_active_probe = lambda: False

    with pytest.raises(WritingDispatchError, match="output exists"):
        dispatcher.reconcile_uncertain_unsent(ticket["attempt_id"])


def test_prepare_never_modifies_scheduler_or_step_prompt_bytes(tmp_path: Path) -> None:
    dispatcher, reader, state_path, scheduler = make_dispatcher(tmp_path)
    add_step1_session(state_path, reader, dispatcher.root, PAPER_A, "thread-a1")
    before = scheduler.read_bytes()

    dispatcher.prepare()

    assert scheduler.read_bytes() == before


def test_desktop_launcher_uses_exact_ticket_sidecars_and_forbids_fuzzy_thread_search() -> None:
    prompt = writing_parallel_launcher_prompt()

    assert "writing-parallel-prepare" in prompt
    assert "writing-parallel-mark-uncertain" in prompt
    assert "writing-parallel-record-dispatch" in prompt
    assert "send_message_to_thread" in prompt
    assert "create_thread" in prompt
    assert "never use list_threads" in prompt
    assert "never edit pipeline_state.json" in prompt.lower()


def test_writing_cap_accepts_ten_workers(tmp_path: Path) -> None:
    dispatcher, _reader, state_path, _scheduler = make_dispatcher(tmp_path, cap=10)

    result = dispatcher.prepare()

    assert result["configured_cap"] == 10


@pytest.mark.parametrize("value", [0, -1, 11, True, "2", "all"])
def test_invalid_writing_cap_fails_closed(tmp_path: Path, value) -> None:
    dispatcher, _reader, state_path, _scheduler = make_dispatcher(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settings"]["max_parallel_workers"] = value
    write_json_atomic(state_path, state)

    with pytest.raises(WritingDispatchError, match="parallel limit"):
        dispatcher.prepare()
