from __future__ import annotations

import tomllib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import unified_paper_orchestrator.automation as automation_module
import unified_paper_orchestrator.orchestrator as orchestrator_module
import unified_paper_orchestrator.ui.main_window as ui_module
from unified_paper_orchestrator.automation import SchedulerAutomationController
from unified_paper_orchestrator.backend_router import BackendRouter
from unified_paper_orchestrator.orchestrator import V1_BACKEND_ACTION_SPECS, UnifiedOrchestrator
from unified_paper_orchestrator.ui.main_window import UnifiedControlPanel


class _ModeStore:
    def load_settings(self) -> SimpleNamespace:
        return SimpleNamespace(execution_mode="legacy_codex")


class _NoAutomationPreflight:
    def __getattr__(self, name: str):
        raise AssertionError(f"writing SDK control unexpectedly accessed Automation preflight: {name}")


class _WritingAdapter:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events

    def start(self) -> dict:
        self.events.append(("writing", "start"))
        return {"runtime": {"started": True}, "automation": {"enabled": False}}

    def pause(self) -> dict:
        self.events.append(("writing", "pause"))
        return {"ok": True, "paused": True, "runtime": {"pause_requested": True}}

    def new_sessions(self, count: int) -> dict:
        self.events.append(("writing", "new_sessions", count))
        return {
            "commands": [{"id": f"new-{index}"} for index in range(count)],
            "runtime": {"started": True},
            "automation": {"enabled": False},
        }

    def queue_many(self, paper_ids: list[str]) -> dict:
        self.events.append(("writing", "queue_many", tuple(paper_ids)))
        return {
            "commands": [{"id": f"resume-{paper_id}"} for paper_id in paper_ids],
            "runtime": {"started": True},
            "automation": {"enabled": False},
        }


class _OtherAdapter:
    def __init__(self, stage: str, events: list[tuple[object, ...]]) -> None:
        self.stage = stage
        self.events = events

    def load_state(self) -> dict:
        raise AssertionError(f"{self.stage} Automation must not be a Writing start prerequisite")

    def pause(self) -> dict:
        self.events.append((self.stage, "pause"))
        return {"paused": True}


def _orchestrator(events: list[tuple[object, ...]]) -> UnifiedOrchestrator:
    item = object.__new__(UnifiedOrchestrator)
    item.ai_provider_store = _ModeStore()
    item.automation = _NoAutomationPreflight()
    item.backend_router = BackendRouter.legacy_only(V1_BACKEND_ACTION_SPECS)
    item.adapters = {
        "writing": _WritingAdapter(events),
        "figures": _OtherAdapter("figures", events),
        "journal_author": _OtherAdapter("journal_author", events),
        "submission": _OtherAdapter("submission", events),
    }
    item.sync = lambda persist=False: {"history": []}  # type: ignore[method-assign]
    item.save = lambda _state: None  # type: ignore[method-assign]
    return item


@pytest.mark.parametrize("action", ("start", "new", "resume"))
def test_single_active_writing_actions_do_not_require_other_stage_automations(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []
    orchestrator = _orchestrator(events)
    monkeypatch.setattr(orchestrator_module, "_unified_state_lock", nullcontext)

    if action == "start":
        state = orchestrator.start_stage("writing", single_active=True)
        expected = ("writing", "start")
    elif action == "new":
        state = orchestrator.start_new_writing_sessions(2, single_active=True)
        expected = ("writing", "new_sessions", 2)
    else:
        state = orchestrator.resume_writing_papers(["paper-a", "paper-b"], single_active=True)
        expected = ("writing", "queue_many", ("paper-a", "paper-b"))

    assert expected in events
    assert {(stage, operation) for stage, operation, *_rest in events if stage != "writing"} == {
        ("figures", "pause"),
        ("journal_author", "pause"),
        ("submission", "pause"),
    }
    assert state["last_scheduler_result"]["results"]["writing"]["ok"] is True


def test_writing_pause_routes_to_adapter_runtime_without_automation_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []
    orchestrator = _orchestrator(events)
    monkeypatch.setattr(orchestrator_module, "_unified_state_lock", nullcontext)

    state = orchestrator.pause_stage("writing")

    assert events == [("writing", "pause")]
    assert state["last_scheduler_result"]["result"]["ok"] is True


def test_resume_failure_is_shown_instead_of_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returned = {
        "last_scheduler_result": {
            "results": {
                "writing": {
                    "ok": False,
                    "action": "resume_writing_failed",
                    "error": "SDK wakeup refused",
                }
            }
        }
    }
    shown: list[tuple[str, str]] = []
    panel = SimpleNamespace(
        state_data={
            "papers": {
                "paper-a": {"stages": {"writing": {"status": "paused"}}},
            }
        },
        orchestrator=SimpleNamespace(
            resume_writing_papers=lambda _ids, single_active: returned,
        ),
        log_text=object(),
        refresh=lambda: None,
        _show_execution_blocked=lambda _action, _exc: pytest.fail("unexpected exception path"),
        _show_operation_result=lambda _target, title, _payload, summary="": shown.append((title, summary)),
    )
    monkeypatch.setattr(ui_module.messagebox, "askyesno", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        ui_module.messagebox,
        "showerror",
        lambda title, message, **_kwargs: shown.append((title, message)),
    )

    UnifiedControlPanel.resume_selected_writing(panel, ["paper-a"])

    assert shown == [
        ("继续写作失败", "SDK wakeup refused"),
        ("继续写作失败", "SDK wakeup refused"),
    ]
    assert all(title != "写作任务已继续" for title, _message in shown)


@pytest.mark.parametrize("stage", ("writing", "figures", "journal_author", "submission"))
def test_pausing_an_automation_preserves_its_prompt_and_schedule(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    automation_root = tmp_path / "automations"
    primary_id = f"{stage}-primary"
    primary = automation_root / primary_id / "automation.toml"
    primary.parent.mkdir(parents=True)
    exact_prompt_line = 'prompt = "USER-TUNED LEGACY PROMPT\\nKEEP EXACT"'
    primary.write_text(
        "\n".join(
            (
                f'id = "{primary_id}"',
                'status = "ACTIVE"',
                exact_prompt_line,
                'rrule = "FREQ=MINUTELY;INTERVAL=37"',
                "updated_at = 1",
                "",
            )
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text('{"settings":{}}', encoding="utf-8")
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", automation_root)
    controller = SchedulerAutomationController()
    controller.projects = {
        stage: {
            "label": stage,
            "state_path": state_path,
            "prompt_path": tmp_path / "SCHEDULER_PROMPT.md",
            "primary_automation_id": primary_id,
            "automation_ids": [primary_id],
            "heartbeat_minutes": 5,
        }
    }

    result = controller.set_enabled(stage, False, {"settings": {}})

    updated_text = primary.read_text(encoding="utf-8")
    updated = tomllib.loads(updated_text)
    assert result["enabled"] is False
    assert updated["status"] == "PAUSED"
    assert updated["prompt"] == "USER-TUNED LEGACY PROMPT\nKEEP EXACT"
    assert updated["rrule"] == "FREQ=MINUTELY;INTERVAL=37"
    assert exact_prompt_line in updated_text
