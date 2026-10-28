from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from unified_paper_orchestrator import orchestrator as orchestrator_module
from unified_paper_orchestrator.adapters import submission as submission_module
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.submission_routing import (
    SubmissionRouteError,
    legacy_terminal_evidence,
    route_switch_blockers,
    smart_terminal_evidence,
)


def project_config(root: Path) -> dict[str, object]:
    return {
        "label": "submission",
        "root": root,
        "state_path": root / "controller" / "submission_state.json",
        "prompt_path": root / "controller" / "submission_prompt.md",
        "papers_dir": root / "papers",
        "automation_ids": ["submission-scheduler"],
        "primary_automation_id": "submission-scheduler",
        "heartbeat_minutes": 5,
    }


class SubmissionRoutingProjectionTest(unittest.TestCase):
    def _adapter(self, root: Path, legacy: dict, smart: dict):
        project = project_config(root / "legacy")
        smart_path = root / "smart" / "smart_submission_state.json"
        write_json_atomic(Path(project["state_path"]), legacy)
        write_json_atomic(smart_path, smart)
        self.enterContext(patch.object(submission_module, "LEGACY_PROJECTS", {"submission": project}))
        self.enterContext(patch.object(submission_module, "SMART_SUBMISSION_STATE_PATH", smart_path))
        self.enterContext(patch.object(submission_module, "JOURNAL_ROOT", root / "journal"))
        return submission_module.SubmissionAdapter(), Path(project["state_path"]), smart_path

    def test_default_legacy_owner_ignores_unverified_smart_states(self) -> None:
        for smart_status in ("blocked", "waiting", "monitoring"):
            with self.subTest(smart_status=smart_status), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                adapter, _, _ = self._adapter(
                    root,
                    {"papers": {"paper-a": {"workflow_stage": "ready_to_submit"}}},
                    {"papers": {"paper-a": {"status": smart_status}}, "online_queue": []},
                )
                projection = adapter.stage_for("paper-a")
                self.assertEqual(projection["source"], "legacy_submission")
                self.assertEqual(projection["status"], "ready")
                self.assertEqual(projection["execution_owner"], "legacy_auto")
                self.assertFalse(projection.get("terminal_locked", False))

    def test_smart_must_be_explicit_owner_for_nonterminal_projection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            adapter, _, _ = self._adapter(
                root,
                {"papers": {"paper-a": {"workflow_stage": "ready_to_submit"}}},
                {"papers": {"paper-a": {"status": "blocked"}}, "online_queue": []},
            )
            legacy, smart = adapter.load_projection_states()
            projection = adapter.stage_for_from_states("paper-a", legacy, smart, execution_owner="smart")
            self.assertEqual(projection["source"], "smart_submission")
            self.assertEqual(projection["status"], "blocked")
            self.assertEqual(projection["execution_owner"], "smart")

    def test_verified_terminal_evidence_wins_globally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            receipt = root / "receipt.pdf"
            receipt.write_bytes(b"proof")
            adapter, _, _ = self._adapter(
                root,
                {"papers": {"paper-a": {"workflow_stage": "ready_to_submit"}}},
                {
                    "papers": {
                        "paper-a": {
                            "status": "monitoring",
                            "submission_evidence_path": str(receipt),
                        }
                    },
                    "online_queue": [],
                },
            )
            projection = adapter.stage_for("paper-a")
            self.assertEqual(projection["source"], "smart_submission")
            self.assertEqual(projection["status"], "submitted")
            self.assertTrue(projection["terminal_locked"])
            self.assertTrue(projection["terminal_evidence"])

    def test_ids_http_urls_and_existing_files_are_evidence_but_status_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            receipt = root / "receipt.html"
            receipt.write_text("confirmed", encoding="utf-8")
            records = {
                "id": {"submission_id": "ABC-123"},
                "url": {"confirmation_url": "https://journal.example/confirmation/123"},
                "file": {"submission_evidence_path": str(receipt)},
                "status-only": {"status": "monitoring"},
                "bad-url": {"confirmation_url": "javascript:alert(1)"},
                "missing-file": {"submission_evidence_path": str(root / "missing.pdf")},
            }
            state = {"papers": records}
            for paper_id in ("id", "url", "file"):
                self.assertTrue(smart_terminal_evidence(state, paper_id, evidence_base=root))
            for paper_id in ("status-only", "bad-url", "missing-file"):
                self.assertEqual(smart_terminal_evidence(state, paper_id, evidence_base=root), [])

    def test_submission_queue_validates_execution_owner_and_preserves_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            adapter, legacy_path, smart_path = self._adapter(
                root,
                {"papers": {}, "paper_queue": []},
                {"papers": {"paper-a": {"status": "blocked"}}, "online_queue": []},
            )
            legacy_before = legacy_path.read_bytes()
            smart_before = smart_path.read_bytes()
            with self.assertRaises(RuntimeError):
                adapter.queue("paper-a", owner="smart")
            with self.assertRaises(RuntimeError):
                adapter.queue("paper-a", owner="manual")
            with self.assertRaises(ValueError):
                adapter.queue("paper-a", owner="invalid")
            self.assertEqual(legacy_path.read_bytes(), legacy_before)
            self.assertEqual(smart_path.read_bytes(), smart_before)


class RouteSwitchSafetyTest(unittest.TestCase):
    def test_closed_legacy_manuscript_id_is_history_not_a_fresh_route_blocker(self) -> None:
        rejected = {
            "papers": {
                "paper-a": {
                    "manuscript_id": "OLD-REJECTED-1",
                    "active_submission": False,
                    "workflow_stage": "rejected",
                    "decision": "Reject",
                }
            }
        }
        active = {
            "papers": {
                "paper-a": {
                    "manuscript_id": "ACTIVE-1",
                    "active_submission": True,
                    "workflow_stage": "submitted_active",
                }
            }
        }

        self.assertEqual(legacy_terminal_evidence(rejected, "paper-a"), [])
        self.assertEqual(
            {item["kind"] for item in legacy_terminal_evidence(active, "paper-a")},
            {"active_submission", "manuscript_id"},
        )

    def test_switch_blockers_cover_queue_running_unknown_and_terminal(self) -> None:
        cases = {
            "legacy_queue": ({"paper_queue": ["paper-a"], "papers": {}}, {"papers": {}}),
            "smart_queue": ({"papers": {}}, {"online_queue": ["paper-a"], "papers": {}}),
            "running": ({"papers": {}}, {"papers": {"paper-a": {"worker_status": "running"}}}),
            "final_unknown": (
                {"papers": {}},
                {"papers": {"paper-a": {"final_submit_attempted": True}}},
            ),
            "terminal": (
                {"papers": {"paper-a": {"manuscript_id": "LEG-1"}}},
                {"papers": {}},
            ),
        }
        for label, (legacy, smart) in cases.items():
            with self.subTest(label=label):
                self.assertTrue(route_switch_blockers(legacy, smart, "paper-a"))

        safe = route_switch_blockers(
            {"papers": {"paper-a": {"workflow_stage": "ready_to_submit"}}},
            {"papers": {"paper-a": {"status": "monitoring"}}},
            "paper-a",
        )
        self.assertEqual(safe, [])


class OrchestratorRoutePersistenceTest(unittest.TestCase):
    class FakeAdapter:
        def __init__(self, status: str = "complete") -> None:
            self.status = status

        def all_paper_ids(self) -> set[str]:
            return {"paper-a"}

        def stage_for(self, paper_id: str) -> dict[str, str]:
            return {
                "status": self.status,
                "label": "stage",
                "detail": "",
                "artifact": "",
                "raw_status": self.status,
            }

        def load_state(self) -> dict:
            return {"papers": {}}

    def _orchestrator(self, root: Path, *, unified: dict, legacy: dict, smart: dict):
        unified_path = root / "state" / "unified_state.json"
        summary_path = root / "state" / "summary.md"
        legacy_root = root / "legacy"
        project = project_config(legacy_root)
        legacy_path = Path(project["state_path"])
        smart_path = root / "smart" / "smart_submission_state.json"
        write_json_atomic(unified_path, unified)
        write_json_atomic(legacy_path, legacy)
        write_json_atomic(smart_path, smart)
        self.enterContext(patch.object(submission_module, "LEGACY_PROJECTS", {"submission": project}))
        self.enterContext(patch.object(submission_module, "SMART_SUBMISSION_STATE_PATH", smart_path))
        self.enterContext(patch.object(orchestrator_module, "REPO_ROOT", root))
        self.enterContext(patch.object(orchestrator_module, "UNIFIED_STATE_PATH", unified_path))
        self.enterContext(patch.object(orchestrator_module, "UNIFIED_SUMMARY_PATH", summary_path))
        self.enterContext(
            patch.object(orchestrator_module, "FileLock", lambda *_args, **_kwargs: nullcontext())
        )
        orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
        orchestrator.adapters = {
            "writing": self.FakeAdapter(),
            "figures": self.FakeAdapter(),
            "journal_author": self.FakeAdapter(),
            "submission": submission_module.SubmissionAdapter(),
        }
        return orchestrator, unified_path, legacy_path, smart_path

    def test_default_route_and_explicit_smart_route_persist_across_sync(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            orchestrator, unified_path, legacy_path, smart_path = self._orchestrator(
                root,
                unified={"papers": {}, "paper_queue": ["paper-a"], "history": []},
                legacy={"papers": {"paper-a": {"workflow_stage": "ready_to_submit"}}, "paper_queue": []},
                smart={"papers": {"paper-a": {"status": "blocked"}}, "online_queue": []},
            )
            initial = orchestrator.sync(persist=True)
            self.assertEqual(initial["submission_routes"]["paper-a"]["execution_owner"], "legacy_auto")
            legacy_before = legacy_path.read_bytes()
            smart_before = smart_path.read_bytes()

            changed = orchestrator.set_submission_route(
                "paper-a",
                execution_owner="smart",
                mode="canary",
                expected_revision=1,
            )
            self.assertTrue(changed["changed"])
            self.assertEqual(changed["submission_route"]["revision"], 2)
            persisted = json.loads(unified_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["submission_routes"]["paper-a"], changed["submission_route"])
            self.assertEqual(persisted["papers"]["paper-a"]["next_action"], "blocked")
            self.assertEqual(legacy_path.read_bytes(), legacy_before)
            self.assertEqual(smart_path.read_bytes(), smart_before)

            synced = orchestrator.sync(persist=False)
            self.assertEqual(synced["submission_routes"]["paper-a"], changed["submission_route"])
            self.assertEqual(synced["papers"]["paper-a"]["submission_route"], changed["submission_route"])

            with self.assertRaises(SubmissionRouteError):
                orchestrator.set_submission_route(
                    "paper-a",
                    execution_owner="legacy_auto",
                    mode="production",
                    expected_revision=1,
                )

    def test_set_route_rejects_active_or_terminal_sources(self) -> None:
        cases = (
            ({"papers": {"paper-a": {"workflow_stage": "ready_to_submit"}}}, {"online_queue": ["paper-a"], "papers": {}}),
            ({"papers": {"paper-a": {"manuscript_id": "LEG-1"}}}, {"online_queue": [], "papers": {}}),
        )
        for legacy, smart in cases:
            with self.subTest(legacy=legacy, smart=smart), tempfile.TemporaryDirectory() as raw:
                orchestrator, _, _, _ = self._orchestrator(
                    Path(raw),
                    unified={"papers": {}, "paper_queue": [], "history": []},
                    legacy=legacy,
                    smart=smart,
                )
                with self.assertRaises(SubmissionRouteError):
                    orchestrator.set_submission_route(
                        "paper-a",
                        execution_owner="smart",
                        mode="canary",
                    )

    def test_existing_submission_handoff_preserves_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            orchestrator, unified_path, _, _ = self._orchestrator(
                Path(raw),
                unified={"papers": {}, "paper_queue": [], "history": []},
                legacy={
                    "papers": {
                        "paper-a": {
                            "manuscript_id": "LEG-1",
                            "active_submission": True,
                        }
                    },
                    "paper_queue": [],
                },
                smart={"online_queue": [], "papers": {}},
            )

            changed = orchestrator.handoff_existing_submission_to_smart(
                "paper-a",
                expected_revision=1,
                operation_id="monitor-followup-1",
            )

            persisted = json.loads(unified_path.read_text(encoding="utf-8"))
            receipt = persisted["submission_route_operations"]["paper-a"]
            self.assertTrue(changed["changed"])
            self.assertTrue(changed["existing_submission_handoff"])
            self.assertEqual(changed["submission_route"]["revision"], 2)
            self.assertTrue(receipt["existing_submission_handoff"])
            self.assertEqual(receipt["terminal_evidence_count"], 2)

    def test_master_advance_never_writes_legacy_for_smart_or_manual_owner(self) -> None:
        for owner in ("smart", "manual"):
            with self.subTest(owner=owner):
                adapter = Mock()
                orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
                orchestrator.adapters = {"submission": adapter}
                record = {
                    "current_stage": "submission",
                    "next_action": "queue_submission",
                    "submission_route": {"execution_owner": owner, "mode": "canary", "revision": 2},
                    "stages": {"journal_author": {"artifact": "package"}},
                }
                state = {
                    "paper_queue": ["paper-a"],
                    "papers": {"paper-a": record},
                    "submission_routes": {"paper-a": record["submission_route"]},
                    "history": [],
                }
                orchestrator.sync = Mock(side_effect=[state, state])
                orchestrator.save = Mock()
                result = orchestrator._advance_once_unlocked(apply=True)
                self.assertEqual(result["status"], "route_owned_elsewhere")
                adapter.queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
