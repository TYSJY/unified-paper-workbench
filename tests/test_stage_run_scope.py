from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from unified_paper_orchestrator.adapters import base as base_module
from unified_paper_orchestrator.adapters import figures as figure_module
from unified_paper_orchestrator.adapters import journal_author as journal_module
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic


def project_config(root: Path, stage: str) -> dict[str, object]:
    controller = root / "controller"
    return {
        "label": stage,
        "root": root,
        "state_path": controller / f"{stage}_state.json",
        "prompt_path": controller / f"{stage}_prompt.md",
        "papers_dir": root / "papers",
        "automation_ids": [f"{stage}-scheduler"],
        "primary_automation_id": f"{stage}-scheduler",
        "heartbeat_minutes": 5,
    }


def stale_queue_state() -> dict[str, object]:
    return {
        "paper_queue": ["stale-a", "stale-b"],
        "active_paper_id": "stale-a",
        "papers": {
            "stale-a": {"paper_id": "stale-a", "status": "queued", "sentinel": "keep-a"},
            "stale-b": {"paper_id": "stale-b", "status": "queued", "sentinel": "keep-b"},
        },
        "settings": {
            "scheduling_mode": "manual",
            "dispatch_mode": "parallel_workers",
            "max_parallel_workers": "all",
        },
        "commands": [],
    }


def figure_runtime_mock() -> Mock:
    runtime = Mock()
    runtime.ensure_ready.return_value = {"ok": True, "authenticated": True}
    runtime.start_background.return_value = {"ok": True, "started": True}
    runtime.request_pause.return_value = {"ok": True, "pause_requested": True}
    return runtime


def journal_runtime_mock() -> Mock:
    runtime = Mock()
    runtime.ensure_ready.return_value = {"ok": True, "authenticated": True}
    runtime.start_background.return_value = {"ok": True, "started": True}
    runtime.request_pause.return_value = {"ok": True, "pause_requested": True}
    return runtime


class StageRunScopeTest(unittest.TestCase):
    def test_figure_queue_and_start_target_only_without_rewriting_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = project_config(root, "figures")
            state_path = Path(project["state_path"])
            write_json_atomic(state_path, stale_queue_state())

            with (
                patch.object(figure_module, "LEGACY_PROJECTS", {"figures": project}),
                patch.object(figure_module, "FIGURE_PIPELINE_LOCK_PATH", root / "figure.lock"),
                patch.object(base_module, "backup_file", return_value=""),
            ):
                target_input = root / "papers" / "target-paper" / "input.tex"
                target_input.parent.mkdir(parents=True)
                target_input.write_text("paper\n", encoding="utf-8")
                runtime = figure_runtime_mock()
                adapter = figure_module.FigureAdapter(runtime=runtime)
                adapter.automation = Mock()
                adapter.automation.set_enabled.return_value = {"enabled": False}

                result = adapter.queue("target-paper")
                queued = read_json(state_path, {})
                self.assertEqual(result["target_paper_ids"], ["target-paper"])
                self.assertEqual(queued["paper_queue"], ["stale-a", "stale-b", "target-paper"])
                self.assertEqual(queued["active_paper_id"], "target-paper")
                self.assertEqual(queued["settings"]["run_scope"], "target_papers")
                self.assertEqual(queued["settings"]["target_paper_ids"], ["target-paper"])
                self.assertEqual(adapter.scoped_paper_queue(queued), ["target-paper"])
                self.assertEqual(queued["papers"]["stale-a"]["sentinel"], "keep-a")
                self.assertEqual(queued["papers"]["stale-b"]["sentinel"], "keep-b")
                self.assertEqual(queued["commands"][-1]["params"]["run_scope"], "target_papers")

                adapter.start()
                started = read_json(state_path, {})
                self.assertEqual(started["settings"]["scheduling_mode"], "auto")
                self.assertEqual(started["settings"]["target_paper_ids"], ["target-paper"])
                self.assertEqual(started["paper_queue"], ["stale-a", "stale-b", "target-paper"])
                self.assertEqual(adapter.automation.set_enabled.call_count, 2)
                self.assertEqual(runtime.start_background.call_count, 2)

    def test_figure_step1_queue_replaces_previous_target_not_durable_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = project_config(root, "figures")
            state = stale_queue_state()
            state["settings"].update({"run_scope": "target_papers", "target_paper_ids": ["old-target"]})
            state["paper_queue"].append("old-target")
            state_path = Path(project["state_path"])
            write_json_atomic(state_path, state)

            with (
                patch.object(figure_module, "LEGACY_PROJECTS", {"figures": project}),
                patch.object(figure_module, "FIGURE_PIPELINE_LOCK_PATH", root / "figure.lock"),
                patch.object(base_module, "backup_file", return_value=""),
            ):
                target_input = root / "papers" / "new-target" / "input.tex"
                target_input.parent.mkdir(parents=True)
                target_input.write_text("paper\n", encoding="utf-8")
                adapter = figure_module.FigureAdapter(runtime=figure_runtime_mock())
                adapter.automation = Mock()
                adapter.automation.set_enabled.return_value = {"enabled": False}
                adapter.queue_from_step1("new-target")

            queued = read_json(state_path, {})
            self.assertEqual(
                queued["paper_queue"],
                ["stale-a", "stale-b", "old-target", "new-target"],
            )
            self.assertEqual(queued["settings"]["target_paper_ids"], ["new-target"])
            self.assertEqual(queued["active_paper_id"], "new-target")

    def test_journal_start_prefills_only_scoped_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = project_config(root, "journal_author")
            state_path = Path(project["state_path"])
            write_json_atomic(state_path, stale_queue_state())
            (root / "papers" / "stale-a").mkdir(parents=True)
            (root / "papers" / "stale-b").mkdir(parents=True)

            with (
                patch.object(journal_module, "LEGACY_PROJECTS", {"journal_author": project}),
                patch.object(journal_module, "JOURNAL_AUTHOR_PIPELINE_LOCK_PATH", root / "journal.lock"),
                patch.object(base_module, "backup_file", return_value=""),
            ):
                adapter = journal_module.JournalAuthorAdapter(runtime=journal_runtime_mock())
                adapter.automation = Mock()
                adapter.automation.set_enabled.return_value = {"enabled": True}

                result = adapter.queue("target-paper")
                adapter.start()

                state = read_json(state_path, {})
                self.assertEqual(result["target_paper_ids"], ["target-paper"])
                self.assertEqual(state["paper_queue"], ["stale-a", "stale-b", "target-paper"])
                self.assertEqual(state["active_paper_id"], "target-paper")
                self.assertEqual(state["settings"]["run_scope"], "target_papers")
                self.assertEqual(state["settings"]["target_paper_ids"], ["target-paper"])
                self.assertEqual(adapter.scoped_paper_queue(state), ["target-paper"])
                self.assertTrue((root / "papers" / "target-paper" / "author_info.md").exists())
                self.assertFalse((root / "papers" / "stale-a" / "author_info.md").exists())
                self.assertFalse((root / "papers" / "stale-b" / "author_info.md").exists())
                self.assertEqual(state["papers"]["stale-a"]["sentinel"], "keep-a")
                self.assertEqual(state["papers"]["stale-b"]["sentinel"], "keep-b")
                self.assertFalse((root / "papers" / "target-paper" / "source_submit").exists())
                self.assertFalse((root / "papers" / "target-paper" / "work").exists())

    def test_scope_view_is_backward_compatible_and_malformed_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = project_config(root, "figures")
            with patch.object(figure_module, "LEGACY_PROJECTS", {"figures": project}):
                adapter = figure_module.FigureAdapter()

            normal = stale_queue_state()
            self.assertEqual(adapter.scoped_paper_queue(normal), ["stale-a", "stale-b"])
            malformed = stale_queue_state()
            malformed["settings"].update({"run_scope": "target_papers", "target_paper_ids": "stale-a"})
            self.assertEqual(adapter.scoped_paper_queue(malformed), [])


if __name__ == "__main__":
    unittest.main()
