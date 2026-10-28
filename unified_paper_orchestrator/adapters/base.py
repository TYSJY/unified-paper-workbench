from __future__ import annotations

from pathlib import Path
from typing import Any

from ..automation import SchedulerAutomationController
from ..io_utils import (
    backup_file,
    command_id,
    now_iso,
    read_json,
    read_json_snapshot,
    write_json_atomic,
    write_json_atomic_if_unchanged,
)


class LegacyAdapter:
    stage_name = ""
    id_key = "paper_id"
    targeted_run_scope = "target_papers"
    normal_run_scope = "normal"

    def __init__(self, project: dict[str, Any]) -> None:
        self.label = str(project["label"])
        self.root = Path(project["root"])
        self.state_path = Path(project["state_path"])
        self.prompt_path = Path(project["prompt_path"])
        self.papers_dir = Path(project["papers_dir"])
        self.automation = SchedulerAutomationController()

    def load_state(self) -> dict[str, Any]:
        data = read_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def load_state_snapshot(self) -> tuple[dict[str, Any], str | None]:
        data, digest = read_json_snapshot(self.state_path, {})
        return (data if isinstance(data, dict) else {}), digest

    def save_state(self, state: dict[str, Any]) -> None:
        backup_file(self.state_path, self.stage_name or self.label)
        state["updated_at"] = now_iso()
        write_json_atomic(self.state_path, state)

    def save_state_if_unchanged(self, state: dict[str, Any], expected_sha256: str | None) -> bool:
        """Best-effort conditional state replace for an already quiescent writer."""

        backup_file(self.state_path, self.stage_name or self.label)
        state["updated_at"] = now_iso()
        return write_json_atomic_if_unchanged(self.state_path, state, expected_sha256)

    def scheduler_mode(self, state: dict[str, Any] | None = None) -> str:
        state = state or self.load_state()
        return str(state.get("settings", {}).get("scheduling_mode") or "")

    def scheduler_settings(self, state: dict[str, Any]) -> dict[str, Any]:
        settings = state.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            state["settings"] = settings
        return settings

    def set_targeted_run_scope(self, state: dict[str, Any], paper_id: str) -> dict[str, Any]:
        """Restrict the next scheduler run without rewriting its durable queue.

        Legacy schedulers historically treated ``paper_queue`` as both durable
        backlog and the current dispatch set.  The unified controller needs a
        narrower, transient dispatch set for a paper-specific E2E run while
        preserving older queue entries for a later explicit normal run.
        """

        target = str(paper_id or "").strip()
        if not target:
            raise ValueError("paper_id is required for a targeted scheduler run")
        settings = self.scheduler_settings(state)
        settings.update(
            {
                "run_scope": self.targeted_run_scope,
                "target_paper_ids": [target],
                "run_scope_requested_by": "unified_orchestrator",
                "run_scope_requested_at": now_iso(),
                "run_scope_completion_policy": "pause_and_restore_normal",
            }
        )
        state["active_paper_id"] = target
        return settings

    def scoped_paper_queue(self, state: dict[str, Any]) -> list[str]:
        """Return the dispatch view while leaving ``paper_queue`` untouched."""

        raw_queue = state.get("paper_queue", [])
        if not isinstance(raw_queue, list):
            return []
        queue = [str(item).strip() for item in raw_queue if item is not None and str(item).strip()]
        settings = state.get("settings", {})
        if not isinstance(settings, dict) or settings.get("run_scope") != self.targeted_run_scope:
            return queue
        raw_targets = settings.get("target_paper_ids", [])
        if not isinstance(raw_targets, list):
            return []
        targets = {str(item).strip() for item in raw_targets if item is not None and str(item).strip()}
        return [paper_id for paper_id in queue if paper_id in targets]

    def set_scheduler_mode(self, mode: str) -> bool:
        state = self.load_state()
        settings = self.scheduler_settings(state)
        if str(settings.get("scheduling_mode") or "") == mode:
            return False
        settings["scheduling_mode"] = mode
        self.save_state(state)
        return True

    def append_command(
        self,
        state: dict[str, Any],
        action: str,
        entity_id: str,
        params: dict[str, Any] | None = None,
        id_key: str | None = None,
    ) -> dict[str, Any]:
        id_key = id_key or self.id_key
        params = params or {}
        commands = state.setdefault("commands", [])
        if not isinstance(commands, list):
            commands = []
            state["commands"] = commands
        for existing in commands:
            if (
                isinstance(existing, dict)
                and existing.get("status") == "pending"
                and existing.get("action") == action
                and str(existing.get(id_key) or "") == entity_id
                and existing.get("params", {}) == params
            ):
                return existing
        command = {
            "id": command_id(),
            "action": action,
            id_key: entity_id,
            "params": params,
            "status": "pending",
            "created_at": now_iso(),
            "handled_at": "",
            "message": "",
        }
        commands.append(command)
        return command

    def all_paper_ids(self) -> set[str]:
        return set()

    def stage_for(self, paper_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def queue(self, paper_id: str, upstream_artifact: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def start(self) -> dict[str, Any]:
        state = self.load_state()
        self.automation.require_controllable(self.stage_name, state)
        self.set_scheduler_mode("auto")
        return self.automation.set_enabled(self.stage_name, True, self.load_state())

    def pause(self) -> dict[str, Any]:
        self.set_scheduler_mode("manual")
        return self.automation.set_enabled(self.stage_name, False, self.load_state())
