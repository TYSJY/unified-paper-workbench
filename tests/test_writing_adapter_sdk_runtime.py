from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest

from unified_paper_orchestrator import orchestrator as orchestrator_module
from unified_paper_orchestrator.adapters import base as base_module
from unified_paper_orchestrator.adapters import writing as writing_module
from unified_paper_orchestrator.io_utils import FileLock, write_json_atomic
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator


PAPER_A = "2026-07-13_001_sdk-a"
PAPER_B = "2026-07-13_002_sdk-b"


class FakeCompletionReader:
    def inspect(self, thread_id: str) -> dict:
        return {"status": "rollout_missing", "thread_id": thread_id}


class FakeRuntime:
    def __init__(
        self,
        state_path: Path,
        events: list[tuple],
        *,
        ready: bool = True,
        marker_pending: bool = False,
        recovery_error: Exception | None = None,
    ) -> None:
        self.state_path = state_path
        self.events = events
        self.ready = ready
        self.marker_pending = marker_pending
        self.recovery_error = recovery_error
        self.prepare_calls = 0
        self.guard_calls = 0

    def ensure_ready(self) -> dict:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.events.append(("account_preflight", len(state.get("commands", []))))
        if not self.ready:
            raise RuntimeError("CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED")
        return {"ok": True, "authenticated": True, "isolated_home": True}

    def start_background(
        self,
        *,
        priority_paper_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        settings = state.get("settings", {})
        event = (
            "start_background",
            len(state.get("commands", [])),
            settings.get("dispatch_mode"),
            settings.get("writing_dispatch_protocol"),
        )
        if priority_paper_keys is not None:
            event = (*event, tuple(priority_paper_keys))
        self.events.append(event)
        return {"started": True, "already_running": False}

    def request_pause(self) -> dict:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.events.append(("request_pause", state.get("settings", {}).get("scheduling_mode")))
        return {"pause_requested": True, "scheduling_mode": "manual"}

    def assert_revalidation_transaction_resolved(self) -> None:
        self.guard_calls += 1
        if self.marker_pending:
            raise RuntimeError("WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED")

    def recover_revalidation_transaction(self) -> dict:
        return self.prepare_pipeline_mutation()

    def prepare_pipeline_mutation(self) -> dict:
        self.prepare_calls += 1
        if self.recovery_error is not None:
            raise self.recovery_error
        recovered = self.marker_pending
        self.marker_pending = False
        return {"recovered": recovered, "status": "rolled_forward" if recovered else "none"}


class FakeAutomation:
    def __init__(self, events: list[tuple], *, installed: bool = True) -> None:
        self.events = events
        self.installed = installed
        self.status = "ACTIVE" if installed else "MISSING"

    def set_enabled(self, stage: str, enabled: bool, _state: dict) -> dict:
        self.events.append(("automation", stage, enabled))
        assert enabled is False, "the SDK adapter must never enable Codex Automation"
        self.status = "PAUSED" if self.installed else "MISSING"
        return {"stage": stage, "enabled": enabled, "updated": []}

    def read_statuses(self, _stage: str) -> list[dict]:
        return [
            {
                "id": "writing-scheduler",
                "path": "test",
                "status": self.status,
                "exists": self.installed,
            }
        ]


def make_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ready: bool = True,
    installed_automation: bool = True,
    marker_pending: bool = False,
    recovery_error: Exception | None = None,
) -> tuple[writing_module.WritingAdapter, FakeRuntime, FakeAutomation, Path, Path, list[tuple]]:
    root = tmp_path / "writing"
    controller = root / "controller"
    controller.mkdir(parents=True)
    papers_dir = root / "papers"
    papers_dir.mkdir()
    state_path = controller / "pipeline_state.json"
    prompt_path = controller / "SCHEDULER_PROMPT.md"
    prompt_path.write_bytes(b"EXACT WRITING PROMPT\r\nDO NOT CHANGE\r\n")
    write_json_atomic(
        state_path,
        {
            "settings": {
                "scheduling_mode": "manual",
                "dispatch_mode": "parallel_workers",
                "writing_dispatch_protocol": "visible_slots_v1",
                "max_parallel_workers": 2,
            },
            "sessions": {},
            "commands": [],
        },
    )
    project = {
        "label": "Writing",
        "root": root,
        "state_path": state_path,
        "prompt_path": prompt_path,
        "papers_dir": papers_dir,
        "automation_ids": ["writing-scheduler"],
        "primary_automation_id": "writing-scheduler",
    }
    monkeypatch.setattr(writing_module, "LEGACY_PROJECTS", {"writing": project})
    monkeypatch.setattr(writing_module, "WRITING_DISPATCH_LOCK_PATH", tmp_path / "adapter.lock")
    monkeypatch.setattr(base_module, "backup_file", lambda _path, _label="": "")
    events: list[tuple] = []
    runtime = FakeRuntime(
        state_path,
        events,
        ready=ready,
        marker_pending=marker_pending,
        recovery_error=recovery_error,
    )
    automation = FakeAutomation(events, installed=installed_automation)
    adapter = writing_module.WritingAdapter(FakeCompletionReader(), runtime=runtime)
    adapter.automation = automation
    return adapter, runtime, automation, state_path, prompt_path, events


def test_queue_many_writes_sdk_protocol_before_start_and_keeps_automation_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _runtime, _automation, state_path, prompt_path, events = make_adapter(
        tmp_path, monkeypatch
    )
    prompt_before = prompt_path.read_bytes()

    result = adapter.queue_many([PAPER_A, PAPER_B, PAPER_A])

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["paper_ids"] == [PAPER_A, PAPER_B]
    assert result["runtime"]["started"] is True
    assert result["automation"]["status"] == "PAUSED"
    assert state["settings"]["scheduling_mode"] == "auto"
    assert state["settings"]["dispatch_mode"] == "sdk_event_driven"
    assert state["settings"]["writing_dispatch_protocol"] == "sdk_events_v1"
    assert state["settings"]["sdk_dispatch_generation"] == 1
    assert [command["pipeline_id"] for command in state["commands"]] == [PAPER_A, PAPER_B]
    expected_keys = (f"session:{PAPER_A}", f"session:{PAPER_B}")
    assert result["priority_paper_keys"] == list(expected_keys)
    assert (
        "start_background",
        2,
        "sdk_event_driven",
        "sdk_events_v1",
        expected_keys,
    ) in events
    assert not any(event[:2] == ("automation", "writing") and event[2] for event in events)
    assert prompt_path.read_bytes() == prompt_before


def test_new_sessions_preflights_account_before_commands_and_background_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _runtime, _automation, state_path, _prompt_path, events = make_adapter(
        tmp_path, monkeypatch
    )

    result = adapter.new_sessions(2)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert events[0] == ("account_preflight", 0)
    priority_keys = tuple(command["sdk_paper_key"] for command in state["commands"])
    assert events[-1] == (
        "start_background",
        2,
        "sdk_event_driven",
        "sdk_events_v1",
        priority_keys,
    )
    assert result["account"]["authenticated"] is True
    assert len(state["commands"]) == 2
    assert all(command["action"] == "new_session" for command in state["commands"])
    assert len({command["params"]["request_id"] for command in state["commands"]}) == 2
    reserved = [command["params"]["reserved_pipeline_prefix"] for command in state["commands"]]
    assert len(set(reserved)) == 2
    assert all(command["reserved_pipeline_prefix"] == prefix for command, prefix in zip(state["commands"], reserved))
    assert all(prefix.endswith(("_001", "_002")) for prefix in reserved)
    assert result["priority_paper_keys"] == list(priority_keys)
    assert state["settings"]["auto_create_new_session"] is False
    assert state["settings"]["sdk_dispatch_generation"] == 1


def test_continue_selected_preserves_existing_sdk_paper_key_and_runs_only_that_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _runtime, _automation, state_path, _prompt_path, events = make_adapter(
        tmp_path,
        monkeypatch,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["sessions"] = {
        PAPER_A: {
            "pipeline_id": PAPER_A,
            "sdk_paper_key": "new-session:original-command",
            "status": "paused",
            "step_done": 1,
        }
    }
    write_json_atomic(state_path, state)

    result = adapter.queue_many([PAPER_A])
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["priority_paper_keys"] == ["new-session:original-command"]
    assert saved["sessions"][PAPER_A]["sdk_paper_key"] == "new-session:original-command"
    assert saved["commands"][-1]["sdk_paper_key"] == "new-session:original-command"
    assert events[-1] == (
        "start_background",
        1,
        "sdk_event_driven",
        "sdk_events_v1",
        ("new-session:original-command",),
    )


def test_new_sessions_account_failure_is_byte_exact_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _runtime, _automation, state_path, prompt_path, events = make_adapter(
        tmp_path, monkeypatch, ready=False
    )
    state_before = state_path.read_bytes()
    prompt_before = prompt_path.read_bytes()

    with pytest.raises(RuntimeError, match="CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED"):
        adapter.new_sessions(1)

    assert events == [("account_preflight", 0)]
    assert state_path.read_bytes() == state_before
    assert prompt_path.read_bytes() == prompt_before


def test_repeated_single_new_session_clicks_never_deduplicate_pending_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _runtime, _automation, state_path, _prompt_path, _events = make_adapter(
        tmp_path,
        monkeypatch,
    )

    first = adapter.new_sessions(1)
    second = adapter.new_sessions(1)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["commands"]) == 2
    assert state["commands"][0]["id"] != state["commands"][1]["id"]
    assert state["commands"][0]["params"]["request_id"] != state["commands"][1]["params"]["request_id"]
    assert (
        state["commands"][0]["params"]["reserved_pipeline_prefix"]
        != state["commands"][1]["params"]["reserved_pipeline_prefix"]
    )
    assert first["priority_paper_keys"] != second["priority_paper_keys"]


def test_start_resume_and_pause_use_runtime_while_legacy_fallback_stays_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _runtime, _automation, state_path, _prompt_path, events = make_adapter(
        tmp_path, monkeypatch, installed_automation=False
    )

    started = adapter.start()
    started_generation = json.loads(state_path.read_text(encoding="utf-8"))["settings"][
        "sdk_dispatch_generation"
    ]
    paused = adapter.pause()
    paused_generation = json.loads(state_path.read_text(encoding="utf-8"))["settings"][
        "sdk_dispatch_generation"
    ]
    resumed = adapter.resume()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert started["runtime"]["started"] is True
    assert paused["runtime"]["pause_requested"] is True
    assert resumed["action"] == "resume"
    assert state["settings"]["scheduling_mode"] == "auto"
    assert state["settings"]["dispatch_mode"] == "sdk_event_driven"
    assert state["settings"]["writing_dispatch_protocol"] == "sdk_events_v1"
    assert (started_generation, paused_generation, state["settings"]["sdk_dispatch_generation"]) == (1, 1, 2)
    assert events.count(("request_pause", "manual")) == 1
    assert sum(event[0] == "start_background" for event in events) == 2
    assert all(not event[2] for event in events if event[0] == "automation")
    assert all(
        result["automation"]["status"] == "PAUSED" for result in (started, paused, resumed)
    )


def test_background_wakeup_must_be_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runtime, _automation, _state_path, prompt_path, _events = make_adapter(
        tmp_path, monkeypatch, installed_automation=False
    )
    prompt_before = prompt_path.read_bytes()
    runtime.start_background = lambda: {"started": False, "already_running": False}  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="did not acknowledge the background wakeup"):
        adapter.start()

    assert prompt_path.read_bytes() == prompt_before


def test_pause_request_must_be_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runtime, _automation, _state_path, prompt_path, _events = make_adapter(
        tmp_path, monkeypatch, installed_automation=False
    )
    prompt_before = prompt_path.read_bytes()
    runtime.request_pause = lambda: {"pause_requested": False}  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="did not acknowledge the pause request"):
        adapter.pause()

    assert prompt_path.read_bytes() == prompt_before


@pytest.mark.parametrize("action", ["queue", "start", "pause"])
def test_pending_revalidation_recovery_failure_is_zero_write_then_operation_succeeds_after_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    recovery_error = RuntimeError("WRITING_SDK_REVALIDATION_RECOVERY_CONFLICT")
    adapter, runtime, _automation, state_path, prompt_path, events = make_adapter(
        tmp_path,
        monkeypatch,
        marker_pending=True,
        recovery_error=recovery_error,
    )
    state_before = state_path.read_bytes()
    prompt_before = prompt_path.read_bytes()

    def invoke() -> dict:
        if action == "queue":
            return adapter.queue_many([PAPER_A])
        if action == "start":
            return adapter.start()
        return adapter.pause()

    with pytest.raises(RuntimeError, match="WRITING_SDK_REVALIDATION_RECOVERY_CONFLICT"):
        invoke()

    assert runtime.prepare_calls == 1
    assert events == []
    assert state_path.read_bytes() == state_before
    assert prompt_path.read_bytes() == prompt_before

    runtime.recovery_error = None
    assert runtime.recover_revalidation_transaction()["recovered"] is True
    result = invoke()

    if action == "pause":
        assert result["runtime"]["pause_requested"] is True
    else:
        assert result["runtime"]["started"] is True
    assert state_path.read_bytes() != state_before
    assert prompt_path.read_bytes() == prompt_before


def test_direct_writing_adapter_save_refuses_pending_marker_without_touching_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runtime, _automation, state_path, _prompt_path, _events = make_adapter(tmp_path, monkeypatch)
    state_before = state_path.read_bytes()
    state = json.loads(state_before.decode("utf-8"))
    state["unsafe"] = True
    runtime.marker_pending = True

    with pytest.raises(RuntimeError, match="WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED"):
        adapter.save_state(state)

    assert state_path.read_bytes() == state_before


def test_start_background_runs_after_pipeline_lock_without_reverse_lock_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runtime, _automation, _state_path, _prompt_path, _events = make_adapter(
        tmp_path,
        monkeypatch,
        installed_automation=False,
    )
    background_lock = threading.Lock()
    rendezvous = threading.Barrier(2)
    pipeline_acquired = threading.Event()
    errors: list[BaseException] = []

    def start_background() -> dict:
        rendezvous.wait(timeout=3)
        with background_lock:
            return {"started": True, "already_running": False}

    runtime.start_background = start_background  # type: ignore[method-assign]

    def hold_background_then_pipeline() -> None:
        try:
            with background_lock:
                rendezvous.wait(timeout=3)
                with FileLock(writing_module.WRITING_DISPATCH_LOCK_PATH):
                    pipeline_acquired.set()
        except BaseException as exc:
            errors.append(exc)

    def run_start() -> None:
        try:
            adapter.start()
        except BaseException as exc:
            errors.append(exc)

    revalidator = threading.Thread(target=hold_background_then_pipeline, daemon=True)
    starter = threading.Thread(target=run_start, daemon=True)
    revalidator.start()
    starter.start()
    revalidator.join(timeout=5)
    starter.join(timeout=5)

    assert not revalidator.is_alive()
    assert not starter.is_alive()
    assert errors == []
    assert pipeline_acquired.is_set()


def test_writing_scheduler_settings_use_adapter_owned_non_reentrant_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runtime, _automation, state_path, _prompt_path, _events = make_adapter(
        tmp_path,
        monkeypatch,
        installed_automation=False,
    )
    orchestrator = object.__new__(UnifiedOrchestrator)
    orchestrator.adapters = {"writing": adapter}
    orchestrator.sync = lambda persist=False: {"history": []}  # type: ignore[method-assign]
    orchestrator.save = lambda _state: None  # type: ignore[method-assign]
    monkeypatch.setattr(orchestrator_module, "_unified_state_lock", nullcontext)
    monkeypatch.setattr(
        orchestrator_module,
        "WRITING_DISPATCH_LOCK_PATH",
        writing_module.WRITING_DISPATCH_LOCK_PATH,
        raising=False,
    )

    result = orchestrator.save_stage_scheduler_settings(
        "writing",
        {"scheduling_mode": "manual", "heartbeat_minutes": 9},
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "save_stage_scheduler_settings"
    assert state["settings"]["heartbeat_minutes"] == 9
    assert result["automation"]["runtime"]["pause_requested"] is True
    assert runtime.prepare_calls == 2
    assert not writing_module.WRITING_DISPATCH_LOCK_PATH.exists()
