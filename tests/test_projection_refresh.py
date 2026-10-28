from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator import projection
from unified_paper_orchestrator import smart_submission as smart
from unified_paper_orchestrator import orchestrator as orchestrator_module
from unified_paper_orchestrator.adapters import submission as submission_module
from unified_paper_orchestrator.adapters.submission import SubmissionAdapter
from unified_paper_orchestrator.models import stage_status
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator
from unified_paper_orchestrator.ui import main_window as main_window_module


class EmptyStageAdapter:
    def __init__(self, label: str) -> None:
        self.label = label

    def all_paper_ids(self) -> set[str]:
        return set()

    def stage_for(self, _paper_id: str) -> dict[str, object]:
        return stage_status("missing", label=self.label)

    def load_state(self) -> dict[str, object]:
        return {}


class CaptureVar:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: object) -> None:
        self.value = str(value)


@pytest.fixture()
def projection_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    state_dir = tmp_path / "state"
    smart_root = state_dir / "smart_submission"
    unified_path = state_dir / "unified_state.json"
    summary_path = state_dir / "unified_summary.md"
    dirty_path = state_dir / "unified_projection_dirty.json"
    dirty_lock = state_dir / "unified_projection_dirty.lock"

    monkeypatch.setattr(smart, "SMART_SUBMISSION_DIR", smart_root)
    monkeypatch.setattr(smart, "SMART_STATE_PATH", smart_root / "smart_submission_state.json")
    monkeypatch.setattr(smart, "SMART_CREDENTIAL_PATH", smart_root / "credential_store.local.json")
    monkeypatch.setattr(smart, "SMART_CREDENTIAL_VAULT_PATH", smart_root / "credential_vault.local.json")
    monkeypatch.setattr(smart, "SMART_SITE_REGISTRY_PATH", smart_root / "site_registry.json")
    monkeypatch.setattr(smart, "SMART_SCHEDULER_PROMPT_PATH", smart_root / "SMART_SUBMISSION_SCHEDULER_PROMPT.md")
    monkeypatch.setattr(smart, "SMART_AUDIT_DIR", smart_root / "audit_runs")
    monkeypatch.setattr(smart, "SMART_SCREENSHOT_DIR", smart_root / "screenshots")
    monkeypatch.setattr(smart, "SMART_SNAPSHOT_DIR", smart_root / "page_snapshots")
    monkeypatch.setattr(smart, "SMART_CONFIRMATION_EVIDENCE_DIR", smart_root / "confirmation_evidence")
    monkeypatch.setattr(smart, "SMART_LOG_DIR", smart_root / "logs")
    monkeypatch.setattr(smart, "SMART_UPLOAD_STAGING_DIR", smart_root / "upload_staging")
    monkeypatch.setattr(smart, "SMART_AUTOMATION_PATH", tmp_path / "automation.toml")
    monkeypatch.setattr(smart, "UNIFIED_STATE_PATH", unified_path)
    monkeypatch.setattr(smart, "LEGACY_SUBMISSION_STATE_PATH", tmp_path / "legacy_submission_state.json")
    monkeypatch.setattr(smart, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(smart, "REPO_ROOT", tmp_path)

    monkeypatch.setattr(orchestrator_module, "UNIFIED_STATE_PATH", unified_path)
    monkeypatch.setattr(orchestrator_module, "UNIFIED_SUMMARY_PATH", summary_path)
    monkeypatch.setattr(submission_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(projection, "UNIFIED_STATE_PATH", unified_path)
    monkeypatch.setattr(projection, "SMART_SUBMISSION_STATE_PATH", smart.SMART_STATE_PATH)
    monkeypatch.setattr(projection, "UNIFIED_PROJECTION_DIRTY_PATH", dirty_path)
    monkeypatch.setattr(projection, "UNIFIED_PROJECTION_LOCK_PATH", dirty_lock)

    state_dir.mkdir(parents=True)
    unified = {
        "version": 2,
        "settings": {"scheduling_mode": "manual", "one_action_per_run": True},
        "paper_queue": [],
        "papers": {"paper-1": {"paper_id": "paper-1", "title": "Projection paper"}},
        "submission_routes": {
            "paper-1": {"execution_owner": "smart", "mode": "canary", "revision": 1}
        },
        "submission_route_operations": {},
        "history": [],
    }
    unified_path.write_text(json.dumps(unified, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "root": tmp_path,
        "smart": smart_root,
        "unified": unified_path,
        "summary": summary_path,
        "dirty": dirty_path,
    }


def build_projection_orchestrator(paths: dict[str, Path]) -> UnifiedOrchestrator:
    submission = SubmissionAdapter()
    submission.load_projection_states = lambda: (  # type: ignore[method-assign]
        {},
        json.loads(smart.SMART_STATE_PATH.read_text(encoding="utf-8")),
    )
    orchestrator = UnifiedOrchestrator()
    orchestrator.adapters = {
        "writing": EmptyStageAdapter("写论文"),
        "figures": EmptyStageAdapter("画图"),
        "journal_author": EmptyStageAdapter("选刊作者"),
        "submission": submission,
    }
    return orchestrator


def test_projection_only_sync_does_not_reconcile_writing_controller(
    projection_workspace: dict[str, Path],
) -> None:
    write_running_smart_state(projection_workspace)
    orchestrator = build_projection_orchestrator(projection_workspace)
    orchestrator._reconcile_writing_completion = lambda: pytest.fail(  # type: ignore[method-assign]
        "projection-only sync must not mutate or reconcile writing state"
    )

    state = orchestrator.sync(reconcile_writing=False)

    assert state["projection_sources"]["smart_submission_token"] == ""


def write_running_smart_state(paths: dict[str, Path]) -> None:
    paths["smart"].mkdir(parents=True, exist_ok=True)
    smart.write_json_atomic(
        smart.SMART_STATE_PATH,
        {
            "version": smart.SMART_STATE_VERSION,
            "module": "smart_submission_sidecar",
            "mode": "run_once",
            "paper_queue": [],
            "online_queue": ["paper-1"],
            "papers": {
                "paper-1": {
                    "paper_id": "paper-1",
                    "status": "uploading",
                    "workflow_status": "running",
                    "worker_status": "running",
                    "attempt_id": "attempt-1",
                    "thread_id": "thread-1",
                    "route_revision": 1,
                    "next_action": "visible_worker_online_submit_or_monitor",
                }
            },
            "commands": [],
            "settings": {},
            "history": [],
        },
    )


def test_record_worker_result_refreshes_json_and_markdown_without_requeue(
    projection_workspace: dict[str, Path],
) -> None:
    write_running_smart_state(projection_workspace)
    orchestrator = build_projection_orchestrator(projection_workspace)

    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=1,
        attempt_id="attempt-1",
        status="blocked",
        next_action="manual_verify_site_access",
        thread_id="thread-1",
        activate=False,
        projection_sync_callback=orchestrator.sync,
    )

    persisted_smart = smart.read_json(smart.SMART_STATE_PATH, {})
    unified = json.loads(projection_workspace["unified"].read_text(encoding="utf-8"))
    summary = projection_workspace["summary"].read_text(encoding="utf-8")
    assert result["projection_dirty"]["reason"] == "record_visible_worker_result"
    assert result["projection_refresh"]["status"] == "refreshed"
    assert persisted_smart["papers"]["paper-1"]["workflow_status"] == "blocked"
    assert persisted_smart["online_queue"] == []
    assert unified["papers"]["paper-1"]["stages"]["submission"]["status"] == "blocked"
    assert unified["papers"]["paper-1"]["stages"]["submission"]["source"] == "smart_submission"
    assert unified["papers"]["paper-1"]["current_stage"] == "writing"
    assert unified["papers"]["paper-1"]["stage_admissions"]["submission"]["admitted"] is False
    assert "| paper-1 | 写论文 |" in summary
    assert "| 阻塞 |" in summary
    assert not projection_workspace["dirty"].exists()

    smart_before = smart.SMART_STATE_PATH.read_bytes()
    retry = smart.refresh_smart_submission_projection(sync_callback=orchestrator.sync)
    assert retry == {"status": "clean", "dirty": False, "refreshed": False}
    assert smart.SMART_STATE_PATH.read_bytes() == smart_before


def test_projection_failure_preserves_smart_success_and_retries_value_free(
    projection_workspace: dict[str, Path],
) -> None:
    write_running_smart_state(projection_workspace)
    orchestrator = build_projection_orchestrator(projection_workspace)
    unified_before = projection_workspace["unified"].read_bytes()
    secret = "E2E-PROJECTION-SECRET"

    def fail_projection() -> None:
        raise RuntimeError(f"projection failed with {secret}")

    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=1,
        attempt_id="attempt-1",
        status="blocked",
        thread_id="thread-1",
        activate=False,
        projection_sync_callback=fail_projection,
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert persisted["papers"]["paper-1"]["workflow_status"] == "blocked"
    assert persisted["online_queue"] == []
    assert result["projection_refresh"]["code"] == projection.PROJECTION_REFRESH_FAILED
    assert result["projection_refresh"]["error_type"] == "RuntimeError"
    assert projection_workspace["unified"].read_bytes() == unified_before
    assert projection_workspace["dirty"].is_file()
    assert secret not in projection_workspace["dirty"].read_text(encoding="utf-8")
    assert secret not in json.dumps(result["projection_refresh"], ensure_ascii=False)

    smart_before_retry = smart.SMART_STATE_PATH.read_bytes()
    retry = smart.refresh_smart_submission_projection(sync_callback=orchestrator.sync)
    assert retry["status"] == "refreshed"
    assert retry["dirty"] is False
    assert not projection_workspace["dirty"].exists()
    assert smart.SMART_STATE_PATH.read_bytes() == smart_before_retry
    assert smart.read_json(smart.SMART_STATE_PATH, {})["online_queue"] == []


def test_projection_marker_lock_failure_never_rolls_back_worker_result(
    projection_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_running_smart_state(projection_workspace)
    orchestrator = build_projection_orchestrator(projection_workspace)
    real_mark = projection.mark_projection_dirty
    secret = "MARKER-LOCK-SECRET"

    def fail_marker(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError(f"marker lock conflict {secret}")

    monkeypatch.setattr(projection, "mark_projection_dirty", fail_marker)
    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=1,
        attempt_id="attempt-1",
        status="blocked",
        thread_id="thread-1",
        activate=False,
        projection_sync_callback=orchestrator.sync,
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert persisted["papers"]["paper-1"]["workflow_status"] == "blocked"
    assert persisted["online_queue"] == []
    assert Path(result["audit_path"]).is_file()
    assert result["projection_dirty"]["code"] == projection.PROJECTION_REFRESH_FAILED
    assert result["projection_refresh"]["code"] == projection.PROJECTION_REFRESH_FAILED
    assert secret not in json.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(projection, "mark_projection_dirty", real_mark)
    retry = smart.refresh_smart_submission_projection(sync_callback=orchestrator.sync)
    assert retry["status"] == "refreshed"
    unified = json.loads(projection_workspace["unified"].read_text(encoding="utf-8"))
    assert unified["papers"]["paper-1"]["stages"]["submission"]["status"] == "blocked"
    assert unified["projection_sources"]["smart_submission_token"] == persisted["unified_projection_token"]


def test_pending_result_recovery_refreshes_projection_without_requeue(
    projection_workspace: dict[str, Path],
) -> None:
    smart.SMART_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    final_audit = smart.SMART_AUDIT_DIR / "pending-result.md"
    pending_audit = final_audit.with_name(f"{final_audit.name}.pending")
    audit_payload = b"# value-free result audit\n"
    pending_audit.write_bytes(audit_payload)
    smart.write_json_atomic(
        smart.SMART_STATE_PATH,
        {
            "version": smart.SMART_STATE_VERSION,
            "module": "smart_submission_sidecar",
            "mode": "manual",
            "paper_queue": [],
            "online_queue": [],
            "papers": {
                "paper-1": {
                    "paper_id": "paper-1",
                    "status": "submitted",
                    "workflow_status": "monitoring",
                    "worker_status": "monitoring",
                    "submission_id": "MS-PROJECTION-1",
                    "submission_confirmation_receipt": {
                        "result_audit_path": str(final_audit),
                        "result_audit_sha256": hashlib.sha256(audit_payload).hexdigest(),
                    },
                }
            },
            "commands": [],
            "settings": {},
            "history": [],
        },
    )
    orchestrator = build_projection_orchestrator(projection_workspace)

    result = smart.ensure_smart_submission_module(projection_sync_callback=orchestrator.sync)

    unified = json.loads(projection_workspace["unified"].read_text(encoding="utf-8"))
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert result["transaction_recovery"]["changed"] is True
    assert result["projection_refresh"]["status"] == "refreshed"
    assert final_audit.read_bytes() == audit_payload
    assert not pending_audit.exists()
    assert persisted["online_queue"] == []
    assert unified["papers"]["paper-1"]["stages"]["submission"]["status"] == "submitted"
    assert not projection_workspace["dirty"].exists()


def test_projection_marker_is_value_free_and_cas_keeps_newer_dirty_marker(tmp_path: Path) -> None:
    dirty = tmp_path / "projection_dirty.json"
    first = projection.mark_projection_dirty("password=DO-NOT-PERSIST", dirty_path=dirty)
    assert first["reason"] == "smart_state_changed"
    assert "DO-NOT-PERSIST" not in dirty.read_text(encoding="utf-8")

    def concurrent_change() -> None:
        projection.mark_projection_dirty("record_visible_worker_result", dirty_path=dirty)

    result = projection.refresh_unified_projection_if_dirty(
        sync_callback=concurrent_change,
        dirty_path=dirty,
    )
    assert result["status"] == "dirty"
    assert result["dirty"] is True
    marker = json.loads(dirty.read_text(encoding="utf-8"))
    assert marker["generation"] == 2
    assert marker["reason"] == "record_visible_worker_result"


def test_smart_projection_waits_for_state_and_audit_commit(
    projection_workspace: dict[str, Path],
) -> None:
    write_running_smart_state(projection_workspace)
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    smart._mark_smart_projection_dirty(state, "record_visible_worker_result")
    calls: list[str] = []

    # A marker that was never adopted by an authoritative Smart state commit
    # is an orphan.  Under the Smart lock it is safe to clear, rather than
    # leaving every future refresh stuck at pending_commit.
    before_state_commit = smart.refresh_smart_submission_projection(
        sync_callback=lambda: calls.append("sync"),
    )
    assert before_state_commit["status"] == "clean"
    assert calls == []
    assert not projection_workspace["dirty"].exists()

    smart.SMART_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = smart.SMART_AUDIT_DIR / "record-result.md"
    audit_payload = b"# committed only after rename\n"
    audit_path.with_name(f"{audit_path.name}.pending").write_bytes(audit_payload)
    state["papers"]["paper-1"]["last_audit_path"] = str(audit_path)
    state["papers"]["paper-1"]["last_audit_sha256"] = hashlib.sha256(audit_payload).hexdigest()
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart._mark_smart_projection_dirty(state, "record_visible_worker_result")

    before_audit_commit = smart.refresh_smart_submission_projection(
        sync_callback=lambda: calls.append("sync"),
    )
    assert before_audit_commit["status"] == "pending_commit"
    assert calls == []

    audit_path.with_name(f"{audit_path.name}.pending").replace(audit_path)
    after_commit = smart.refresh_smart_submission_projection(
        sync_callback=lambda: calls.append("sync"),
    )
    assert after_commit["status"] == "refreshed"
    assert calls == ["sync"]
    assert not projection_workspace["dirty"].exists()


def test_smart_projection_refresh_is_single_flight_and_cannot_aba_to_old_state(
    projection_workspace: dict[str, Path],
) -> None:
    write_running_smart_state(projection_workspace)
    state_a = smart.read_json(smart.SMART_STATE_PATH, {})
    smart._prepare_smart_projection_change(state_a, "record_visible_worker_result")
    smart.write_json_atomic(smart.SMART_STATE_PATH, state_a)
    smart._mark_smart_projection_dirty(state_a, "record_visible_worker_result")

    callback_a_started = threading.Event()
    release_callback_a = threading.Event()
    transaction_b_started = threading.Event()
    projected: dict[str, str] = {}
    results: dict[str, dict[str, object]] = {}

    def callback_a() -> None:
        callback_a_started.set()
        assert release_callback_a.wait(timeout=5)
        projected["value"] = "A"

    def refresh_a() -> None:
        results["a"] = smart.refresh_smart_submission_projection(sync_callback=callback_a)

    def transaction_b() -> None:
        assert callback_a_started.wait(timeout=5)
        transaction_b_started.set()
        with smart._smart_state_lock():
            state_b = smart.read_json(smart.SMART_STATE_PATH, {})
            smart._prepare_smart_projection_change(state_b, "record_visible_worker_result")
            smart.write_json_atomic(smart.SMART_STATE_PATH, state_b)
            smart._mark_smart_projection_dirty(state_b, "record_visible_worker_result")
            results["b"] = smart.refresh_smart_submission_projection(
                sync_callback=lambda: projected.__setitem__("value", "B")
            )

    thread_a = threading.Thread(target=refresh_a)
    thread_b = threading.Thread(target=transaction_b)
    thread_a.start()
    assert callback_a_started.wait(timeout=5)
    thread_b.start()
    assert transaction_b_started.wait(timeout=5)
    release_callback_a.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert projected["value"] == "B"
    assert results["a"]["status"] == "refreshed"
    assert results["b"]["status"] == "refreshed"
    assert not projection_workspace["dirty"].exists()


def test_gui_smart_refresh_reloads_unified_panels_after_projection_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    fake = SimpleNamespace(
        orchestrator=SimpleNamespace(load_unified_state=lambda: {"version": 2}),
        state_data={},
        render_overview_table=lambda: calls.append("overview"),
        render_stage_tables=lambda: calls.append("stages"),
        update_queue_panel=lambda: calls.append("queue"),
        update_log_panel=lambda: calls.append("log"),
        _load_smart_submission_state=lambda: {"papers": {}, "online_queue": []},
        smart_submission_notice_var=CaptureVar(),
        smart_submission_status_var=CaptureVar(),
        smart_submission_tree=None,
        smart_submission_state={},
        _smart_mode_label=lambda _value: "手动",
        _automation_status_label=lambda _value: "未启用",
    )
    monkeypatch.setattr(
        main_window_module,
        "refresh_smart_submission_projection",
        lambda: {"status": "refreshed", "dirty": False, "refreshed": True},
    )
    monkeypatch.setattr(
        main_window_module,
        "smart_submission_status",
        lambda: {"mode": "manual", "automation": {}},
    )

    main_window_module.UnifiedControlPanel.refresh_smart_submission(fake)

    assert fake.state_data == {"version": 2}
    assert calls == ["overview", "stages", "queue", "log"]


def test_gui_smart_refresh_reloads_external_projection_even_when_marker_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    fake = SimpleNamespace(
        orchestrator=SimpleNamespace(load_unified_state=lambda: {"version": 3}),
        state_data={"version": 2},
        render_overview_table=lambda: calls.append("overview"),
        render_stage_tables=lambda: calls.append("stages"),
        update_queue_panel=lambda: calls.append("queue"),
        update_log_panel=lambda: calls.append("log"),
        _load_smart_submission_state=lambda: {"papers": {}, "online_queue": []},
        smart_submission_notice_var=CaptureVar(),
        smart_submission_status_var=CaptureVar(),
        smart_submission_tree=None,
        smart_submission_state={},
        _smart_mode_label=lambda _value: "手动",
        _automation_status_label=lambda _value: "未启用",
    )
    monkeypatch.setattr(
        main_window_module,
        "refresh_smart_submission_projection",
        lambda: {"status": "clean", "dirty": False, "refreshed": False},
    )
    monkeypatch.setattr(
        main_window_module,
        "smart_submission_status",
        lambda: {"mode": "manual", "automation": {}},
    )

    main_window_module.UnifiedControlPanel.refresh_smart_submission(fake)

    assert fake.state_data == {"version": 3}
    assert calls == ["overview", "stages", "queue", "log"]
