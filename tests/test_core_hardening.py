from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from unified_paper_orchestrator import automation as automation_module
from unified_paper_orchestrator import doctor as doctor_module
from unified_paper_orchestrator import io_utils
from unified_paper_orchestrator import orchestrator as orchestrator_module
from unified_paper_orchestrator.adapters import base as base_module
from unified_paper_orchestrator.adapters import journal_author as journal_module
from unified_paper_orchestrator.adapters import submission as submission_module
from unified_paper_orchestrator.io_utils import (
    FileLock,
    JsonReadError,
    read_json,
    read_text,
    write_json_atomic,
    write_text,
)
from unified_paper_orchestrator.models import action_for


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


class JsonIoHardeningTest(unittest.TestCase):
    def test_missing_json_uses_default_but_corrupt_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(read_json(root / "missing.json", {"safe": True}), {"safe": True})
            corrupt = root / "corrupt.json"
            corrupt.write_text("{broken", encoding="utf-8")
            with self.assertRaises(JsonReadError):
                read_json(corrupt, {})

    def test_existing_unreadable_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(Path, "open", side_effect=OSError("denied")):
                with self.assertRaises(JsonReadError):
                    read_json(path, {})

    def test_atomic_write_fsyncs_and_cleans_temp_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "state.json"
            with patch.object(io_utils.os, "fsync") as fsync:
                write_json_atomic(path, {"ok": True})
            self.assertGreaterEqual(fsync.call_count, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})

            failed = root / "failed.json"
            with patch.object(io_utils.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_json_atomic(failed, {"large": [1, 2, 3]})
            self.assertEqual(list(root.glob("failed.json.*.tmp")), [])


class FileLockHardeningTest(unittest.TestCase):
    @staticmethod
    def _lock_holder_script(*, abrupt_exit: bool) -> str:
        final_action = "os._exit(0)" if abrupt_exit else "sys.stdin.readline()"
        return textwrap.dedent(
            f"""
            import os
            import sys
            from pathlib import Path

            from unified_paper_orchestrator.io_utils import FileLock

            with FileLock(Path(sys.argv[1]), stale_seconds=1):
                print("READY", flush=True)
                {final_action}
            """
        )

    def test_live_subprocess_lock_is_never_removed_only_because_mtime_is_old(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "live.lock"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    self._lock_holder_script(abrupt_exit=False),
                    str(lock_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        _stdout, stderr = process.communicate(timeout=1)
                        self.fail(f"lock holder exited early: {stderr}")
                    if lock_path.exists() and "owner_token=" in read_text(lock_path):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("live subprocess did not create its lock")

                lock_before = lock_path.read_bytes()
                os.utime(lock_path, (1, 1))
                with self.assertRaises(RuntimeError):
                    with FileLock(lock_path, stale_seconds=1):
                        pass
                self.assertEqual(lock_path.read_bytes(), lock_before)
            finally:
                if process.poll() is None and process.stdin is not None:
                    process.stdin.write("release\n")
                    process.stdin.flush()
                process.wait(timeout=10)
            self.assertFalse(lock_path.exists())

    def test_dead_subprocess_stale_lock_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "dead.lock"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self._lock_holder_script(abrupt_exit=True),
                    str(lock_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(lock_path.exists())
            dead_lock = read_text(lock_path)
            dead_token = next(
                line.split("=", 1)[1]
                for line in dead_lock.splitlines()
                if line.startswith("owner_token=")
            )
            os.utime(lock_path, (1, 1))

            with FileLock(lock_path, stale_seconds=1) as recovered:
                self.assertNotEqual(recovered.owner_token, dead_token)
                self.assertIn(f"pid={os.getpid()}", read_text(lock_path))
            self.assertFalse(lock_path.exists())

    def test_fresh_dead_subprocess_lock_is_reclaimed_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "fresh-dead.lock"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self._lock_holder_script(abrupt_exit=True),
                    str(lock_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(lock_path.exists())
            dead_lock = read_text(lock_path)
            dead_token = next(
                line.split("=", 1)[1]
                for line in dead_lock.splitlines()
                if line.startswith("owner_token=")
            )

            with FileLock(lock_path, stale_seconds=7 * 24 * 60 * 60) as recovered:
                self.assertNotEqual(recovered.owner_token, dead_token)
                self.assertIn(f"pid={os.getpid()}", read_text(lock_path))
            self.assertFalse(lock_path.exists())

    def test_stale_lock_with_missing_or_invalid_pid_fails_closed(self) -> None:
        payloads = (
            "not-a-lock-payload\n",
            "version=2\npid=not-a-number\nowner_token=unknown\n",
            "version=2\npid=-1\nowner_token=unknown\n",
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as raw:
                lock_path = Path(raw) / "unknown-owner.lock"
                lock_path.write_text(payload, encoding="utf-8")
                lock_before = lock_path.read_bytes()
                os.utime(lock_path, (1, 1))

                with self.assertRaises(RuntimeError):
                    with FileLock(lock_path, stale_seconds=1):
                        pass
                self.assertEqual(lock_path.read_bytes(), lock_before)

    def test_exit_does_not_unlink_a_replacement_owner_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "replacement.lock"
            lock = FileLock(lock_path, stale_seconds=1)
            lock.__enter__()
            replacement_token = "f" * 32
            write_text(
                lock_path,
                "\n".join(
                    (
                        "version=2",
                        f"pid={os.getpid()}",
                        f"owner_token={replacement_token}",
                        "created_at=replaced",
                    )
                ),
            )

            lock.__exit__(None, None, None)
            self.assertTrue(lock_path.exists())
            self.assertIn(f"owner_token={replacement_token}", read_text(lock_path))
            lock_path.unlink()

    def test_exit_retries_transient_windows_delete_sharing_violation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "release-retry.lock"
            lock = FileLock(lock_path, stale_seconds=1)
            lock.__enter__()

            real_unlink = Path.unlink
            attempts = 0

            def transient_unlink(path: Path, *, missing_ok: bool = False) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("simulated sharing violation")
                real_unlink(path, missing_ok=missing_ok)

            with patch.object(Path, "unlink", transient_unlink):
                lock.__exit__(None, None, None)

            self.assertEqual(attempts, 2)
            self.assertFalse(lock_path.exists())

    def test_same_process_lock_instances_serialize_without_orphaning_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "threaded.lock"
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()
            failures: list[BaseException] = []

            def first_holder() -> None:
                try:
                    with FileLock(lock_path, stale_seconds=60):
                        first_entered.set()
                        release_first.wait(timeout=5)
                except BaseException as exc:
                    failures.append(exc)

            def second_holder() -> None:
                try:
                    with FileLock(lock_path, stale_seconds=60):
                        second_entered.set()
                except BaseException as exc:
                    failures.append(exc)

            first = threading.Thread(target=first_holder)
            second = threading.Thread(target=second_holder)
            first.start()
            self.assertTrue(first_entered.wait(timeout=5))
            second.start()
            self.assertFalse(second_entered.wait(timeout=0.1))
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(second_entered.is_set())
            self.assertFalse(lock_path.exists())


class SchedulingHardeningTest(unittest.TestCase):
    def test_blocked_and_waiting_states_are_not_queue_actions(self) -> None:
        blocked, _ = action_for("writing", {"writing": {"status": "blocked"}})
        waiting, _ = action_for("writing", {"writing": {"status": "waiting"}})
        self.assertEqual(blocked, "blocked")
        self.assertEqual(waiting, "wait_writing")

    def test_queue_scan_skips_blocked_and_waiting_records(self) -> None:
        orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
        state = {
            "paper_queue": ["blocked", "waiting", "ready"],
            "papers": {
                "blocked": {"next_action": "blocked"},
                "waiting": {"next_action": "wait_figures"},
                "ready": {"next_action": "queue_journal_author"},
            },
        }
        self.assertEqual(orchestrator._first_queued_actionable(state), "ready")

    def test_dry_run_does_not_persist(self) -> None:
        orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
        orchestrator.sync = Mock(
            return_value={
                "paper_queue": ["paper"],
                "papers": {
                    "paper": {
                        "current_stage": "writing",
                        "next_action": "queue_writing",
                        "stages": {},
                    }
                },
            }
        )
        orchestrator.save = Mock()
        result = orchestrator._advance_once_unlocked(apply=False)
        self.assertEqual(result["status"], "planned")
        orchestrator.save.assert_not_called()

    def test_scheduler_status_only_reads_existing_automation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "automation.toml"
            path.write_text('id = "writing-scheduler"\nstatus = "PAUSED"\n', encoding="utf-8")
            before = path.read_bytes()

            class ReadOnlyAutomation:
                def _candidate_paths(self, stage: str):
                    return [path]

                def _primary_path(self, stage: str):
                    return path

            orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
            orchestrator.automation = ReadOnlyAutomation()
            with patch.object(orchestrator_module, "LEGACY_PROJECTS", {"writing": {}}):
                result = orchestrator.scheduler_status()
            self.assertEqual(result["writing"]["status"], "PAUSED")
            self.assertEqual(path.read_bytes(), before)

    def test_pausing_an_already_paused_stage_is_byte_for_byte_noop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = project_config(root, "submission")
            state_path = Path(project["state_path"])
            write_json_atomic(state_path, {"settings": {"scheduling_mode": "manual"}})
            automation_root = root / "automations"
            automation_path = automation_root / "submission-scheduler" / "automation.toml"
            automation_path.parent.mkdir(parents=True)
            automation_path.write_text(
                "\n".join(
                    [
                        'version = 1',
                        'id = "submission-scheduler"',
                        'kind = "heartbeat"',
                        'name = "submission scheduler"',
                        'prompt = "safe"',
                        'status = "PAUSED"',
                        'rrule = "FREQ=MINUTELY;INTERVAL=5"',
                        'target_thread_id = "thread-1"',
                        'updated_at = 1',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            class SubmissionLikeAdapter(base_module.LegacyAdapter):
                stage_name = "submission"

            with (
                patch.object(automation_module, "LEGACY_PROJECTS", {"submission": project}),
                patch.object(automation_module, "AUTOMATIONS_ROOT", automation_root),
                patch.object(automation_module, "backup_file", side_effect=AssertionError("no automation backup expected")),
                patch.object(base_module, "backup_file", side_effect=AssertionError("no state backup expected")),
            ):
                adapter = SubmissionLikeAdapter(project)
                state_before = state_path.read_bytes()
                automation_before = automation_path.read_bytes()
                result = adapter.pause()

            self.assertFalse(result["updated"][0]["changed"])
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(automation_path.read_bytes(), automation_before)


class SubmissionProjectionTest(unittest.TestCase):
    def test_unverified_smart_monitoring_does_not_mask_legacy_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_root = root / "legacy"
            project = project_config(legacy_root, "submission")
            state_path = Path(project["state_path"])
            smart_path = root / "state" / "smart_submission_state.json"
            write_json_atomic(
                state_path,
                {
                    "papers": {"paper-a": {"workflow_stage": "ready_to_submit", "next_action": "submit"}},
                    "paper_queue": ["paper-a"],
                    "settings": {"scheduling_mode": "manual"},
                },
            )
            write_json_atomic(
                smart_path,
                {
                    "papers": {
                        "paper-a": {
                            "status": "monitoring",
                            "workflow_status": "monitoring",
                            "detail": "submission confirmation recorded",
                        },
                        "paper-b": {"status": "not_submitted", "workflow_status": "incomplete"},
                    },
                    "online_queue": [],
                    "settings": {},
                },
            )
            before = state_path.read_bytes()
            with (
                patch.object(submission_module, "LEGACY_PROJECTS", {"submission": project}),
                patch.object(submission_module, "SMART_SUBMISSION_STATE_PATH", smart_path),
                patch.object(submission_module, "JOURNAL_ROOT", root / "journal"),
            ):
                adapter = submission_module.SubmissionAdapter()
                terminal = adapter.stage_for("paper-a")
                negated = adapter.stage_for("paper-b")
                self.assertEqual(terminal["status"], "queued")
                self.assertEqual(terminal["source"], "legacy_submission")
                self.assertEqual(terminal["smart_status"], "waiting")
                self.assertEqual(negated["status"], "missing")
                self.assertEqual(adapter.all_paper_ids(), {"paper-a", "paper-b"})
                with self.assertRaises(RuntimeError):
                    adapter.queue("paper-a")
            self.assertEqual(state_path.read_bytes(), before)


class JournalAuthorSafetyTest(unittest.TestCase):
    def _adapter(self, root: Path):
        project = project_config(root, "journal_author")
        patcher = patch.object(journal_module, "LEGACY_PROJECTS", {"journal_author": project})
        patcher.start()
        self.addCleanup(patcher.stop)
        return journal_module.JournalAuthorAdapter()

    def test_missing_template_is_read_only_draft_without_invented_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            adapter = self._adapter(Path(raw))
            templates = adapter.load_author_templates()
            form = templates["templates"][0]["form"]
            self.assertEqual(form["status"], "DRAFT")
            self.assertEqual(form["author_contribution_statement"], "")
            self.assertEqual(form["funding"], "")
            self.assertEqual(form["conflict_of_interest"], "")
            self.assertEqual(form["ethics_statement"], "")
            self.assertFalse(adapter.templates_path().exists())

    def test_empty_ready_form_is_downgraded_and_valid_form_can_be_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            adapter = self._adapter(Path(raw))
            empty_ready = adapter._blank_author_form("READY")
            self.assertEqual(adapter._normalize_author_form(empty_ready)["status"], "DRAFT")

            valid = adapter._blank_author_form("READY")
            valid["authors"][0].update({"name": "Author One", "email": "author@example.org", "affiliation": "1", "corresponding": True})
            valid["affiliations"][0]["institution"] = "Example University"
            valid["author_contribution_statement"] = (
                "Author One conceived the study, performed the analysis, and wrote the manuscript."
            )
            valid["recommended_reviewers"] = (
                "1. Expert One\n"
                "   Email: expert.one@example.edu\n"
                "   Address: Example University, Example City\n"
                "   Expertise: mobile and edge systems"
            )
            rendered = adapter._render_author_info(valid)
            self.assertEqual(adapter.author_info_issues(rendered), [])
            parsed = adapter._form_from_content(rendered)
            self.assertEqual(
                parsed["author_contribution_statement"],
                valid["author_contribution_statement"],
            )
            self.assertEqual(
                parsed["recommended_reviewers"],
                valid["recommended_reviewers"],
            )

            record: dict[str, object] = {"paper_id": "paper-a"}
            adapter._apply_template_record_fields(
                record,
                {"form": valid, "content": rendered},
            )
            requirements = record["submission_requirements"]
            self.assertIsInstance(requirements, dict)
            self.assertEqual(
                requirements["recommended_reviewers"],
                valid["recommended_reviewers"],
            )

            legacy = rendered.replace(
                "1. Name: Author One",
                "1. Role: first author\n   Name: Author One",
            )
            legacy_parsed = adapter._form_from_content(legacy)
            self.assertEqual(legacy_parsed["authors"][0]["name"], "Author One")

    def test_ready_template_requires_one_consistent_corresponding_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            adapter = self._adapter(Path(raw))
            form = adapter._blank_author_form("READY")
            form["authors"][0].update(
                {
                    "name": "Author One",
                    "email": "author@example.org",
                    "affiliation": "1",
                    "corresponding": True,
                }
            )
            form["affiliations"][0]["institution"] = "Example University"

            derived = adapter._normalize_author_form(form)
            self.assertEqual(derived["status"], "READY")
            self.assertEqual(
                derived["corresponding_author"],
                {"name": "Author One", "email": "author@example.org"},
            )

            form["corresponding_author"] = {
                "name": "Different Person",
                "email": "different@example.org",
            }
            inconsistent = adapter._normalize_author_form(form)
            self.assertEqual(inconsistent["status"], "DRAFT")
            self.assertIn(
                "corresponding author identity does not match an author marked as corresponding",
                adapter._form_readiness_issues(inconsistent),
            )

    def test_import_does_not_overwrite_existing_problematic_author_info(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            adapter = self._adapter(root)
            paper_dir = adapter.paper_dir("paper-a")
            paper_dir.mkdir(parents=True)
            author_info = paper_dir / "author_info.md"
            write_text(author_info, "# Author Information\n\nStatus: DRAFT\n\nUser supplied partial fact: keep me")
            before = read_text(author_info)
            upstream = root / "upstream"
            upstream.mkdir()
            write_text(upstream / "main.tex", "draft")
            result = adapter._ensure_import("paper-a", upstream)
            self.assertEqual(result["author_info"], "kept")
            self.assertEqual(read_text(author_info), before)


class SyncAndDoctorReadOnlyTest(unittest.TestCase):
    class FakeAdapter:
        def __init__(self, paper_id: str, status: str, state: dict[str, object] | None = None) -> None:
            self.paper_id = paper_id
            self.status = status
            self.state = state or {"papers": {}}

        def all_paper_ids(self) -> set[str]:
            return {self.paper_id}

        def stage_for(self, paper_id: str) -> dict[str, object]:
            return {"status": self.status, "label": "stage", "detail": "", "artifact": "", "raw_status": self.status}

        def load_state(self) -> dict[str, object]:
            return self.state

    class FakeSubmissionAdapter(FakeAdapter):
        def load_projection_states(self):
            return self.state, {"papers": {self.paper_id: {"status": self.status}}}

        def paper_ids_from_states(self, legacy, smart):
            return {self.paper_id}

        def stage_for_from_states(self, paper_id, legacy, smart, execution_owner="legacy_auto"):
            return self.stage_for(paper_id)

    def test_sync_preserves_last_result_and_drops_terminal_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "unified.json"
            write_json_atomic(
                state_path,
                {
                    "paper_queue": ["paper-a"],
                    "papers": {},
                    "history": [],
                    "last_scheduler_result": {"status": "kept", "message": "keep this"},
                },
            )
            orchestrator = orchestrator_module.UnifiedOrchestrator.__new__(orchestrator_module.UnifiedOrchestrator)
            orchestrator.adapters = {
                "writing": self.FakeAdapter("paper-a", "complete"),
                "figures": self.FakeAdapter("paper-a", "complete"),
                "journal_author": self.FakeAdapter("paper-a", "complete"),
                "submission": self.FakeSubmissionAdapter("paper-a", "submitted"),
            }
            with patch.object(orchestrator_module, "UNIFIED_STATE_PATH", state_path):
                snapshot = orchestrator.sync(persist=False)
            self.assertEqual(snapshot["last_scheduler_result"]["status"], "kept")
            self.assertEqual(snapshot["paper_queue"], [])

    def test_doctor_uses_read_only_projection_and_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_dir = root / "state"
            state_dir.mkdir()
            projects = {}
            for stage in ("writing", "figures", "journal_author", "submission"):
                project_root = root / stage
                config = project_config(project_root, stage)
                Path(config["root"]).mkdir(parents=True)
                write_json_atomic(Path(config["state_path"]), {"papers": {}, "paper_queue": [], "history": []})
                write_text(Path(config["prompt_path"]), "prompt")
                projects[stage] = config
            unified_state = state_dir / "unified_state.json"
            smart_state = state_dir / "smart_submission" / "smart_submission_state.json"
            summary = state_dir / "summary.md"
            prompt_path = root / "unified_prompt.md"
            write_json_atomic(unified_state, {"papers": {}, "paper_queue": [], "history": []})
            write_json_atomic(smart_state, {"papers": {}, "paper_queue": [], "online_queue": [], "history": [], "settings": {}})
            write_text(summary, "summary")
            write_text(prompt_path, "prompt")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

            class FakeOrchestrator:
                sync_calls: list[tuple[bool, bool]] = []
                status_calls: list[bool] = []

                def sync(self, persist: bool = True, *, reconcile_writing: bool = True):
                    self.sync_calls.append((persist, reconcile_writing))
                    return {"papers": {}, "paper_queue": []}

                def _scheduler_status_read_only(self):
                    self.status_calls.append(False)
                    return {stage: {"exists": True, "status": "PAUSED", "path": str(root / f"{stage}.toml")} for stage in projects}

            with (
                patch.object(doctor_module, "LEGACY_PROJECTS", projects),
                patch.object(doctor_module, "REPO_ROOT", root),
                patch.object(doctor_module, "STATE_DIR", state_dir),
                patch.object(doctor_module, "SMART_SUBMISSION_STATE_PATH", smart_state),
                patch.object(doctor_module, "UNIFIED_STATE_PATH", unified_state),
                patch.object(doctor_module, "UNIFIED_SUMMARY_PATH", summary),
                patch.object(doctor_module, "UNIFIED_PROMPT_PATH", prompt_path),
                patch.object(doctor_module, "LOCK_PATH", state_dir / "lock"),
                patch.object(doctor_module, "BACKUP_DIR", state_dir / "backups"),
                patch.object(doctor_module, "UnifiedOrchestrator", FakeOrchestrator),
            ):
                result = doctor_module.run_doctor()

            self.assertTrue(result["read_only"])
            self.assertEqual(FakeOrchestrator.sync_calls, [(False, False)])
            self.assertEqual(FakeOrchestrator.status_calls, [False])
            after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_doctor_accepts_local_sdk_runtime_without_legacy_automation_warning(self) -> None:
        class FakeOrchestrator:
            def sync(self, persist: bool = True, *, reconcile_writing: bool = True):
                return {"papers": {}, "paper_queue": []}

            def _scheduler_status_read_only(self):
                return {
                    "writing": {
                        "kind": "local_sdk_event_dispatcher",
                        "status": "PAUSED",
                        "runtime": {
                            "schema": "unified-paper-orchestrator/writing-sdk-runtime/v1",
                            "status": "paused",
                            "isolated_codex_home": "sdk-home",
                        },
                    },
                    "figures": {"exists": True, "status": "PAUSED", "path": "figures.toml"},
                    "journal_author": {
                        "exists": True,
                        "status": "PAUSED",
                        "path": "journal.toml",
                    },
                    "submission": {
                        "exists": True,
                        "status": "PAUSED",
                        "path": "submission.toml",
                    },
                }

        with patch.object(doctor_module, "UnifiedOrchestrator", FakeOrchestrator):
            result = doctor_module.run_doctor()

        runtime_check = next(
            item for item in result["checks"] if item["name"] == "writing.sdk_runtime_known"
        )
        self.assertTrue(runtime_check["ok"])
        self.assertFalse(
            any("writing.automation_known" in warning for warning in result["warnings"])
        )


if __name__ == "__main__":
    unittest.main()
