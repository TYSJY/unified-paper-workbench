from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator import smart_submission as smart
from unified_paper_orchestrator.__main__ import _compact_smart_cli_payload, build_parser


class FakeConfirmationTransport:
    def list_targets(self, port: int) -> list[dict[str, object]]:
        if port != 9224:
            return []
        return [
            {
                "type": "page",
                "url": "https://example.test/confirmation/MS-123",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9224/devtools/page/confirmation",
            }
        ]

    def evaluate_submission_confirmation_state(
        self,
        _port: int,
        _websocket_url: str,
    ) -> dict[str, object]:
        return {
            "origin": "https://example.test",
            "document_ready": True,
            "confirmation_marker": "successfully_submitted",
            "final_submit_control_visible": False,
            "login_challenge_visible": False,
            "manuscript_id": "MS-123",
            "page_path": "/confirmation/MS-123",
        }


def test_resolved_minor_actions_never_hide_hard_package_blockers() -> None:
    minor_only = "PACKAGE_STATUS=NEEDS_MINOR_FIX\nMANUAL_REQUIRED: enter reviewers online"
    assert smart._has_unresolved_gate_marker(minor_only) is True
    assert (
        smart._has_unresolved_gate_marker(
            minor_only,
            resolved_minor_actions=True,
        )
        is False
    )
    assert (
        smart._has_unresolved_gate_marker(
            "PACKAGE_STATUS=BLOCKED",
            resolved_minor_actions=True,
        )
        is True
    )
    assert (
        smart._has_unresolved_gate_marker(
            "TODO: replace missing legal declaration",
            resolved_minor_actions=True,
        )
        is True
    )


@pytest.fixture()
def isolated_smart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "smart_submission"
    monkeypatch.setattr(smart, "SMART_SUBMISSION_DIR", root)
    monkeypatch.setattr(smart, "SMART_STATE_PATH", root / "smart_submission_state.json")
    monkeypatch.setattr(smart, "SMART_CREDENTIAL_PATH", root / "credential_store.local.json")
    monkeypatch.setattr(smart, "SMART_CREDENTIAL_VAULT_PATH", root / "credential_vault.local.json")
    monkeypatch.setattr(smart, "SMART_SITE_REGISTRY_PATH", root / "site_registry.json")
    monkeypatch.setattr(smart, "SMART_SCHEDULER_PROMPT_PATH", root / "SMART_SUBMISSION_SCHEDULER_PROMPT.md")
    monkeypatch.setattr(smart, "SMART_AUDIT_DIR", root / "audit_runs")
    monkeypatch.setattr(smart, "SMART_SCREENSHOT_DIR", root / "screenshots")
    monkeypatch.setattr(smart, "SMART_SNAPSHOT_DIR", root / "page_snapshots")
    monkeypatch.setattr(smart, "SMART_CONFIRMATION_EVIDENCE_DIR", root / "confirmation_evidence")
    monkeypatch.setattr(smart, "SMART_LOG_DIR", root / "logs")
    monkeypatch.setattr(smart, "SMART_UPLOAD_STAGING_DIR", root / "upload_staging")
    monkeypatch.setattr(smart, "SMART_AUTOMATION_PATH", tmp_path / "automation.toml")
    monkeypatch.setattr(smart, "UNIFIED_STATE_PATH", tmp_path / "unified_state.json")
    monkeypatch.setattr(smart, "LEGACY_SUBMISSION_STATE_PATH", tmp_path / "legacy_submission_state.json")
    monkeypatch.setattr(smart, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(smart, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(smart, "_verify_foreground_ui_gesture", lambda _handle: True)
    return root


def test_new_account_policy_defaults_to_orcid_first_and_is_user_configurable(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})

    assert state["settings"]["new_account_auth_preference"] == "orcid_first"
    assert state["settings"]["orcid_auto_authorize_for_login"] is True

    result = smart.update_smart_credential_policy(
        account_missing_policy="auto_register",
        require_terms_confirmation=True,
        prefer_orcid=False,
        auto_authorize_orcid=False,
    )
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})

    assert result["new_account_auth_preference"] == "site_account"
    assert result["orcid_auto_authorize_for_login"] is False
    assert persisted["settings"]["new_account_auth_preference"] == "site_account"
    assert persisted["settings"]["orcid_auto_authorize_for_login"] is False


class _ManualTakeoverController:
    def __init__(self) -> None:
        self.activated: list[str] = []

    def list_pages(self, _origins):
        return [
            {
                "page_id": "manual-page-1",
                "cdp_port": 9224,
                "origin": "https://www.indersciencesubmissions.com:443",
                "url": "https://www.indersciencesubmissions.com/author/main.php",
                "title": "Inderscience submissions",
            }
        ]

    def activate_page(self, _origins, *, page_id=""):
        self.activated.append(page_id)
        return {"ok": True, "page_id": page_id, "cdp_port": 9224}

    def snapshot(self, _origins, *, page_id=""):
        return {
            "ok": True,
            "page_id": page_id,
            "cdp_port": 9224,
            "origin": "https://www.indersciencesubmissions.com:443",
            "url": "https://www.indersciencesubmissions.com/author/main.php",
            "title": "Inderscience submissions",
            "ready_state": "complete",
            "controls": [{"ref": "upo-0001", "label": "Save"}],
        }


def test_manual_takeover_pauses_and_resumes_only_the_selected_paper(
    isolated_smart: Path,
) -> None:
    state = smart._default_state()
    state["papers"] = {
        "manual": {
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting_user_action",
            "next_action": "continue_visible_login_then_resume",
            "submission_system_id": "inderscience",
            "credential_broker_origin": "https://www.indersciencesubmissions.com:443",
        },
        "other": {
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
            "submission_system_id": "springer_nature",
        },
    }
    state["online_queue"] = ["manual", "other"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    requested = smart.request_smart_manual_takeover(["manual"])
    paused = smart.read_json(smart.SMART_STATE_PATH, {})

    assert requested["active"] == ["manual"]
    assert paused["online_queue"] == ["other"]
    assert paused["papers"]["manual"]["workflow_status"] == "manual_takeover"
    assert paused["papers"]["other"]["workflow_status"] == "queued"

    controller = _ManualTakeoverController()
    opened = smart.open_smart_manual_takeover_page(
        "manual",
        controller=controller,
    )
    completed = smart.complete_smart_manual_takeover(
        ["manual"],
        controller=controller,
    )
    resumed = smart.read_json(smart.SMART_STATE_PATH, {})

    assert opened["ok"] is True
    assert controller.activated == ["manual-page-1"]
    assert completed["resumed"] == ["manual"]
    assert resumed["online_queue"] == ["other", "manual"]
    assert resumed["papers"]["manual"]["workflow_status"] == "queued"
    assert resumed["papers"]["manual"]["manual_takeover_reconcile_pending"] is True
    assert resumed["papers"]["other"]["workflow_status"] == "queued"


def test_running_manual_takeover_waits_for_current_turn_without_releasing_others(
    isolated_smart: Path,
) -> None:
    state = smart._default_state()
    state["papers"] = {
        "running": {
            "status": "uploading",
            "workflow_status": "running",
            "worker_status": "running",
            "thread_id": "thread-running",
            "attempt_id": "attempt-running",
            "submission_system_id": "inderscience",
        },
        "other": {
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
            "submission_system_id": "springer_nature",
        },
    }
    state["online_queue"] = ["running", "other"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.request_smart_manual_takeover(["running"])
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})

    assert result["requested"] == ["running"]
    assert persisted["online_queue"] == ["running", "other"]
    assert persisted["papers"]["running"]["workflow_status"] == "running"
    assert persisted["papers"]["running"]["manual_takeover"]["status"] == "requested"
    assert persisted["papers"]["other"]["workflow_status"] == "queued"


def test_requested_manual_takeover_becomes_active_after_worker_result(
    isolated_smart: Path,
) -> None:
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"running": {}, "other": {}},
            "submission_routes": {
                "running": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 3,
                }
            },
        },
    )
    state = smart._default_state()
    state["papers"] = {
        "running": {
            "paper_id": "running",
            "status": "uploading",
            "workflow_status": "running",
            "worker_status": "running",
            "thread_id": "thread-running",
            "attempt_id": "attempt-running",
            "route_revision": 3,
            "submission_system_id": "inderscience",
        },
        "other": {
            "paper_id": "other",
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
        },
    }
    state["online_queue"] = ["running", "other"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.request_smart_manual_takeover(["running"])

    smart.record_visible_worker_result(
        "running",
        route_revision=3,
        attempt_id="attempt-running",
        status="waiting",
        next_action="continue_current_submission_form",
        detail="The next form page is ready.",
        thread_id="thread-running",
        activate=False,
    )
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})

    assert persisted["online_queue"] == ["other"]
    assert persisted["papers"]["running"]["workflow_status"] == "manual_takeover"
    assert persisted["papers"]["running"]["manual_takeover"]["status"] == "active"
    resume = persisted["papers"]["running"]["manual_takeover"]["resume_state"]
    assert resume["status"] == "waiting"
    assert resume["next_action"] == "wait_for_external_status"
    assert persisted["papers"]["other"]["workflow_status"] == "queued"


def test_worker_result_preserves_value_free_site_binding_recovery_hint(
    isolated_smart: Path,
) -> None:
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-wiley": {}},
            "submission_routes": {
                "paper-wiley": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 3,
                }
            },
        },
    )
    state = smart._default_state()
    state["papers"] = {
        "paper-wiley": {
            "paper_id": "paper-wiley",
            "status": "uploading",
            "workflow_status": "running",
            "worker_status": "running",
            "thread_id": "thread-wiley",
            "attempt_id": "attempt-wiley",
            "route_revision": 3,
            "target_journal": "Concurrency and Computation: Practice and Experience",
            "submission_system_id": "springer_nature",
        }
    }
    state["online_queue"] = ["paper-wiley"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    smart.record_visible_worker_result(
        "paper-wiley",
        route_revision=3,
        attempt_id="attempt-wiley",
        status="revision_needed",
        next_action=(
            "Scheduler must resolve the target lock mismatch: the locked "
            "system is Springer Nature but the selected journal is Wiley."
        ),
        detail="",
        thread_id="thread-wiley",
        activate=False,
    )
    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-wiley"]

    assert paper["next_action"] == "scheduler_review_revision_required"
    assert paper["last_worker_recovery_hint"] == "site_binding_reconciliation"
    assert smart._routine_online_recovery_reason(paper) == "site_binding_reconciliation"


def test_state_migration_removes_recursive_state_and_bounds_history() -> None:
    recursive_event = {
        "at": "2026-01-01T00:00:00",
        "event": "smart_submission_run",
        "daily_sweep": {
            "state": {
                "history": [
                    {"event": "queue_daily_sweep", "state": {"history": [{"event": "old"}]}}
                ]
            }
        },
        "detail": "x" * (smart.SMART_HISTORY_EVENT_MAX_BYTES * 2),
    }
    state = {
        "version": 1,
        "settings": {"auto_retarget_after_site_hard_stop_enabled": True},
        "history": [recursive_event] * 510,
    }

    migrated, report = smart._migrate_smart_state(state)

    assert migrated["version"] == smart.SMART_STATE_VERSION
    assert report["changed"] is True
    assert report["auto_retarget_disabled"] is True
    assert migrated["settings"]["auto_retarget_after_site_hard_stop_enabled"] is False
    assert len(migrated["history"]) <= smart.SMART_HISTORY_MAX_EVENTS
    encoded = json.dumps(migrated["history"], ensure_ascii=False)
    assert '"state":' not in encoded
    assert all(
        len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= smart.SMART_HISTORY_EVENT_MAX_BYTES
        for event in migrated["history"]
    )


def test_daily_monitor_time_is_normalized_and_preserves_legacy_hour() -> None:
    assert smart.normalize_smart_monitor_time("9:05") == "09:05"
    with pytest.raises(ValueError):
        smart.normalize_smart_monitor_time("24:00")
    with pytest.raises(ValueError):
        smart.normalize_smart_monitor_time("9:5")

    local_zone = timezone(timedelta(hours=8))
    settings = {
        "daily_monitor_hour_local": 21,
        "daily_monitor_minute_local": 30,
    }
    schedule = smart._smart_monitor_schedule(
        settings,
        now=datetime(2026, 7, 17, 20, 0, tzinfo=local_zone),
    )

    assert schedule["time_local"] == "21:30"
    assert schedule["rrule"] == "RRULE:FREQ=DAILY;BYHOUR=21;BYMINUTE=30;BYSECOND=0"
    assert schedule["next_run_at"].startswith("2026-07-17T21:30:00")
    assert settings["daily_monitor_time_local"] == "21:30"


def test_daily_monitor_check_runs_at_most_once_per_local_date() -> None:
    local_zone = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 17, 21, 30, tzinfo=local_zone)

    assert smart._smart_monitor_check_due(
        {"last_monitor_checked_at": "2026-07-17T01:00:00+08:00"},
        now=now,
    ) is False
    assert smart._smart_monitor_check_due(
        {"last_monitor_checked_at": "2026-07-16T23:59:00+08:00"},
        now=now,
    ) is True
    assert smart._smart_monitor_check_due({}, now=now) is True


def test_in_app_daily_monitor_due_supports_same_day_catch_up(
    isolated_smart: Path,
) -> None:
    local_zone = timezone(timedelta(hours=8))
    state = smart._default_state()
    state["settings"]["daily_monitor_enabled"] = True
    state["settings"]["daily_monitor_time_local"] = "20:42"
    state["settings"]["daily_monitor_last_scheduled_date"] = ""
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    before = smart.smart_submission_daily_monitor_due(
        now=datetime(2026, 7, 18, 20, 41, tzinfo=local_zone)
    )
    after = smart.smart_submission_daily_monitor_due(
        now=datetime(2026, 7, 18, 21, 5, tzinfo=local_zone)
    )
    assert before["due"] is False
    assert after["due"] is True

    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["settings"]["daily_monitor_last_scheduled_date"] = "2026-07-18"
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    claimed = smart.smart_submission_daily_monitor_due(
        now=datetime(2026, 7, 18, 21, 6, tzinfo=local_zone)
    )
    assert claimed["due"] is False


def test_manual_monitor_run_forces_a_check_and_persists_result(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unified_paper_orchestrator import smart_submission_sdk_runtime as runtime_module

    state = smart._default_state()
    state["papers"] = {
        "paper-a": {
            "paper_id": "paper-a",
            "status": "monitoring",
            "workflow_status": "queued",
            "monitor_check_pending": True,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "queue_smart_submission_daily_sweep",
        lambda **kwargs: {
            "monitor_check_ids": ["paper-a"],
            "force_monitor": kwargs.get("force_monitor"),
        },
    )
    def fake_run_once(paper_ids):
        current = smart.read_json(smart.SMART_STATE_PATH, {})
        paper = current["papers"]["paper-a"]
        paper.pop("monitor_check_pending", None)
        paper["last_monitor_checked_at"] = smart.now_iso()
        paper["last_monitor_result"] = "monitoring"
        paper["last_monitor_check_outcome"] = "monitoring"
        smart.write_json_atomic(smart.SMART_STATE_PATH, current)
        return {"ok": True, "paper_ids": list(paper_ids)}

    monkeypatch.setattr(
        runtime_module,
        "default_smart_submission_sdk_runtime",
        lambda: SimpleNamespace(run_once=fake_run_once),
    )

    result = smart.run_smart_submission_daily_monitoring(
        force=True,
        trigger="manual_ui",
    )

    settings = smart.read_json(smart.SMART_STATE_PATH, {})["settings"]
    assert result["status"] == "completed"
    assert result["monitor_check_ids"] == ["paper-a"]
    assert result["sweep"]["force_monitor"] is True
    assert settings["daily_monitor_last_trigger"] == "manual_ui"
    assert settings["daily_monitor_last_status"] == "completed"
    assert settings["daily_monitor_last_paper_count"] == 1
    assert settings["daily_monitor_last_checked_count"] == 1


def test_monitor_run_does_not_report_success_when_sdk_omits_a_paper(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unified_paper_orchestrator import smart_submission_sdk_runtime as runtime_module

    state = smart._default_state()
    state["online_queue"] = ["paper-a"]
    state["papers"] = {
        "paper-a": {
            "paper_id": "paper-a",
            "status": "monitoring",
            "workflow_status": "queued",
            "monitor_check_pending": True,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "queue_smart_submission_daily_sweep",
        lambda **_kwargs: {"monitor_check_ids": ["paper-a"]},
    )
    monkeypatch.setattr(
        runtime_module,
        "default_smart_submission_sdk_runtime",
        lambda: SimpleNamespace(run_once=lambda _paper_ids: {"ok": True, "results": []}),
    )

    result = smart.run_smart_submission_daily_monitoring(
        force=True,
        trigger="manual_ui",
    )

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-a"]
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["summary"]["failed_count"] == 1
    assert paper.get("monitor_check_pending") is None
    assert paper["last_monitor_check_outcome"] == "failed"


def test_native_monitor_task_uses_packaged_exe_and_requested_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(smart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(smart.sys, "executable", r"D:\App\UnifiedPaperOrchestrator.exe")

    def fake_scheduler(*arguments: str) -> dict[str, object]:
        captured.extend(arguments)
        return {"ok": True, "status": "ready"}

    monkeypatch.setattr(smart, "_run_windows_task_scheduler", fake_scheduler)

    result = smart._register_native_smart_monitor_task("20:42")

    assert result["registered"] is True
    assert result["time_local"] == "20:42"
    assert smart.SMART_NATIVE_MONITOR_TASK_NAME in captured
    assert "/ST" in captured
    assert captured[captured.index("/ST") + 1] == "20:42"
    assert smart.SMART_PROGRAM_TIMER_COMMAND in str(result["action"])


def test_automation_prompt_is_only_a_deterministic_program_trigger() -> None:
    prompt = smart._smart_automation_prompt()

    assert "smart-submission-automation-tick" in prompt
    assert "Do not inspect papers" in prompt
    assert "dispatch_candidates" not in prompt
    assert "create/read/send visible threads" not in prompt


def test_legacy_automation_is_retired_without_rewriting_its_prompt(
    isolated_smart: Path,
) -> None:
    state = smart._default_state()
    schedule = smart._smart_monitor_schedule(state["settings"])
    state["settings"]["manual_wakeup_active"] = False
    state["settings"]["manual_wakeup_rrule"] = schedule["rrule"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    stale = smart._smart_automation_toml(
        enabled=False,
        state=state,
        rrule=schedule["rrule"],
    )
    stale = smart._set_toml_value(stale, "prompt", "legacy visible scheduler prompt")
    stale = smart._set_toml_value(stale, "status", "ACTIVE")
    smart.write_text(smart.SMART_AUTOMATION_PATH, stale)

    result = smart._set_smart_submission_automation_wakeup_owned(
        rrule=schedule["rrule"],
        manual_active=False,
        event="test_refresh_prompt",
    )

    refreshed = smart.read_text(smart.SMART_AUTOMATION_PATH)
    assert result["updated"] is True
    assert 'status = "PAUSED"' in refreshed
    assert "legacy visible scheduler prompt" in refreshed
    assert result["legacy_automation"]["retired"] is True


def test_restore_daily_cli_accepts_a_custom_local_time() -> None:
    args = build_parser().parse_args(
        ["smart-submission-restore-daily", "--time", "21:30"]
    )

    assert args.time_local == "21:30"


def test_ensure_module_migrates_legacy_monitor_hour(
    isolated_smart: Path,
) -> None:
    state = smart._default_state()
    state["settings"].pop("daily_monitor_time_local", None)
    state["settings"]["daily_monitor_hour_local"] = 22
    state["settings"]["daily_monitor_minute_local"] = 15
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    smart.ensure_smart_submission_module()

    migrated = smart.read_json(smart.SMART_STATE_PATH, {})
    assert migrated["settings"]["daily_monitor_time_local"] == "22:15"
    assert migrated["settings"]["daily_monitor_hour_local"] == 22
    assert migrated["settings"]["daily_monitor_minute_local"] == 15


def test_daily_sweep_queues_a_real_read_only_monitor_check(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-monitor": {}},
            "submission_routes": {
                "paper-monitor": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 4,
                }
            },
        },
    )
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-monitor": {
            "paper_id": "paper-monitor",
            "status": "monitoring",
            "workflow_status": "monitoring",
            "worker_status": "monitoring",
            "target_journal": "Example Journal",
            "submission_system_id": "example_system",
            "submission_id": "EXAMPLE-123",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep()

    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"]["paper-monitor"]
    assert result["online_queued"] == ["paper-monitor"]
    assert refreshed["online_queue"] == ["paper-monitor"]
    assert paper["monitor_check_pending"] is True
    assert paper["workflow_status"] == "queued"
    assert paper["next_action"] == smart.SCHEDULED_MONITORING_NEXT


def test_daily_monitor_skips_status_only_projection_and_repairs_false_confirmation(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-status-only": {}},
            "submission_routes": {
                "paper-status-only": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 1,
                }
            },
        },
    )
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-status-only": {
            "paper_id": "paper-status-only",
            "status": "monitoring",
            "workflow_status": "monitoring",
            "worker_status": "monitoring",
            "target_journal": "Example Journal",
            "submission_system_id": "example_system",
            "final_submit_confirmed": True,
            "last_worker_result_source": "codex_sdk_mailbox_batch",
            "legacy_submission_status": {"status": "missing", "raw_status": "missing"},
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep(
        monitoring_only=True,
        force_monitor=True,
    )

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-status-only"]
    assert result["monitor_check_ids"] == []
    assert "already-submitted" in result["skipped"]["paper-status-only"]
    assert "final_submit_confirmed" not in paper
    assert paper["monitor_confirmation_projection_repaired_at"]


def test_daily_monitor_keeps_legacy_submission_but_removes_mailbox_confirmation_flag(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-legacy-submitted": {}},
            "submission_routes": {
                "paper-legacy-submitted": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 1,
                }
            },
        },
    )
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-legacy-submitted": {
            "paper_id": "paper-legacy-submitted",
            "status": "monitoring",
            "workflow_status": "monitoring",
            "target_journal": "Example Journal",
            "submission_system_id": "example_system",
            "final_submit_confirmed": True,
            "last_worker_result_source": "codex_sdk_mailbox_batch",
            "legacy_submission_status": {
                "status": "submitted",
                "raw_status": "under_review",
            },
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep(
        monitoring_only=True,
        force_monitor=True,
    )

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"][
        "paper-legacy-submitted"
    ]
    assert result["monitor_check_ids"] == ["paper-legacy-submitted"]
    assert "final_submit_confirmed" not in paper
    assert paper["monitor_confirmation_projection_repaired_at"]


def test_submission_id_is_recovered_from_existing_package_evidence(
    isolated_smart: Path,
) -> None:
    package_dir = isolated_smart / "paper-with-id" / "package"
    package_dir.mkdir(parents=True)
    (package_dir / "submission_checklist.md").write_text(
        "- SUBMITTED_MONITORING: manuscript number TEST-26-0042; monitor email.\n",
        encoding="utf-8",
    )

    recovered = smart._submission_id_from_local_evidence(
        "paper-with-id",
        {"source_package_dir": str(package_dir)},
    )

    assert recovered == "TEST-26-0042"


def test_stale_submission_site_is_reconciled_from_registry_and_legacy_evidence(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    paper_id = "paper-tandf"
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        paper_id: {
            "paper_id": paper_id,
            "status": "monitoring",
            "workflow_status": "monitoring",
            "target_journal": "International Journal of Example Systems",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "credential_account_id": "springer-account",
        }
    }
    state["settings"]["account_missing_policy"] = "auto_register"
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.write_json_atomic(
        smart.SMART_CREDENTIAL_PATH,
        {
            "accounts": [
                {
                    "account_id": "springer-account",
                    "scope": "journal_system",
                    "provider": "Springer Nature",
                    "login_url": "https://submission.springernature.com/",
                }
            ]
        },
    )
    smart.write_json_atomic(
        smart.SMART_SITE_REGISTRY_PATH,
        {
            "systems": [
                {
                    "system_id": "taylor_francis",
                    "label": "Taylor & Francis Research Portal",
                    "status": "discovered",
                    "login_url": "https://rp.tandfonline.com/",
                    "submission_url": "https://rp.tandfonline.com/",
                }
            ],
            "journals": [
                {
                    "journal_key": "example_systems",
                    "journal_name": "International Journal of Example Systems",
                    "system_id": "taylor_francis",
                    "status": "discovered",
                    "submission_url": "https://rp.tandfonline.com/dashboard/",
                    "linked_papers": [paper_id],
                }
            ],
        },
    )
    smart.write_json_atomic(
        smart.LEGACY_SUBMISSION_STATE_PATH,
        {
            "papers": {
                paper_id: {
                    "current_journal": "International Journal of Example Systems",
                    "submission_system": "Taylor & Francis Research Portal",
                    "manuscript_id": "262828135",
                    "active_submission": True,
                }
            }
        },
    )

    result = smart.reconcile_smart_submission_site_bindings([paper_id])
    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"][paper_id]
    binding = smart._program_dispatch_binding(
        paper_id,
        paper,
        smart.read_json(smart.SMART_SITE_REGISTRY_PATH, {}),
        account_missing_policy="auto_register",
    )

    assert result["changed"] == [paper_id]
    assert paper["submission_system_id"] == "taylor_francis"
    assert paper["submission_system"] == "Taylor & Francis Research Portal"
    assert paper["submission_id"] == "262828135"
    assert "credential_account_id" not in paper
    assert binding["dispatch_binding_ready"] is True
    assert binding["account_bootstrap_required"] is True
    assert binding["submission_system_id"] == "taylor_francis"


def test_retarget_binding_uses_exact_new_journal_and_never_old_linked_site(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    paper_id = "paper-retargeted-jaise"
    registry = {
        "systems": [
            {
                "system_id": "scholar_one",
                "label": "ScholarOne Manuscripts",
                "status": "discovered",
                "login_url": "https://mc.manuscriptcentral.com/",
                "submission_url": "https://mc.manuscriptcentral.com/",
            },
            {
                "system_id": "springer_nature",
                "label": "Springer Nature",
                "status": "discovered",
                "login_url": "https://submission.springernature.com/",
                "submission_url": "https://submission.springernature.com/",
            },
        ],
        "journals": [
            {
                "journal_key": "mobile_networks_and_applications_mone_springer",
                "journal_name": "Mobile Networks and Applications",
                "system_id": "springer_nature",
                "status": "discovered",
                "submission_url": "https://submission.springernature.com/",
                "linked_papers": [paper_id],
            },
            {
                "journal_key": (
                    "journal_of_ambient_intelligence_and_smart_environments_jaise"
                ),
                "journal_name": (
                    "Journal of Ambient Intelligence and Smart Environments (JAISE)"
                ),
                "system_id": "scholar_one",
                "status": "discovered",
                "submission_url": "https://mc.manuscriptcentral.com/jaise",
                "linked_papers": [paper_id],
            },
        ],
    }
    paper = {
        "paper_id": paper_id,
        "target_journal": (
            "Journal of Ambient Intelligence and Smart Environments"
        ),
        "submission_system_id": "springer_nature",
        "online_intent": "submit_retargeted_package",
        "last_monitor_followup_status": "retarget_package_ready",
        "retarget_request": {"status": "completed_handoff_to_submission"},
    }

    binding = smart._program_dispatch_binding(
        paper_id,
        paper,
        registry,
        account_missing_policy="auto_register",
    )

    assert binding["dispatch_binding_ready"] is True
    assert binding["site_binding_corrected"] is True
    assert binding["submission_system_id"] == "scholar_one"
    assert binding["journal_site_key"] == (
        "journal_of_ambient_intelligence_and_smart_environments_jaise"
    )
    assert binding["target_origin"] == "https://mc.manuscriptcentral.com:443"

    projected = dict(paper)
    projected["submission_id_source"] = "existing_local_submission_evidence"
    projected["submission_id_recovered_at"] = "2026-07-19T01:00:00+08:00"
    legacy_record = {
        "current_journal": (
            "Journal of Ambient Intelligence and Smart Environments (JAISE)"
        ),
        "submission_system": "ScholarOne Manuscripts",
        "manuscript_id": "LEGACY-JAISE-26-0613",
        "active_submission": True,
    }
    reconciled = smart._reconcile_paper_site_binding(
        paper_id,
        projected,
        registry=registry,
        credentials={"accounts": []},
        legacy_state={"papers": {paper_id: legacy_record}},
    )
    assert reconciled["changed"] is True
    assert projected["submission_system_id"] == "scholar_one"
    assert "submission_id" not in projected
    assert "submission_id_source" not in projected
    assert "submission_id_recovered_at" not in projected
    smart.write_json_atomic(
        smart.LEGACY_SUBMISSION_STATE_PATH,
        {"papers": {paper_id: legacy_record}},
    )
    assert smart._submission_id_from_local_evidence(paper_id, projected) == ""

    old_only = dict(registry)
    old_only["journals"] = [registry["journals"][0]]
    missing = smart._registry_binding_for_paper_any_system(
        old_only,
        paper_id=paper_id,
        target_journal=paper["target_journal"],
    )
    assert missing["status"] == "not_found"


def test_target_journal_publisher_hint_rejects_and_repairs_cross_publisher_registry(
    isolated_smart: Path,
) -> None:
    paper_id = "paper-ccpe"
    journal_name = "Concurrency and Computation: Practice and Experience"
    journal_key = "concurrency_and_computation_practice_and_experience"
    registry = {
        "systems": [
            {
                "system_id": "springer_nature",
                "label": "Springer Nature",
                "status": "discovered",
                "login_url": "https://submission.springernature.com/",
                "submission_url": "https://submission.springernature.com/",
            },
            {
                "system_id": "wiley",
                "label": "Wiley Research Exchange",
                "status": "discovered",
                "login_url": "https://authors.wiley.com/dashboard",
                "submission_url": "https://authors.wiley.com/dashboard",
            },
        ],
        "journals": [
            {
                "journal_key": journal_key,
                "journal_name": journal_name,
                "system_id": "springer_nature",
                "status": "discovered",
                "submission_url": "",
                "linked_papers": [paper_id],
            }
        ],
    }

    stale = smart._registry_binding_for_paper_any_system(
        registry,
        paper_id=paper_id,
        target_journal=journal_name,
    )
    assert stale["status"] == "not_found"

    changed = smart._merge_journal_record(
        registry,
        paper_id,
        journal_name,
        "wiley",
        f"{paper_id}/journal.md",
        authoritative_system=True,
    )
    repaired = smart._registry_binding_for_paper_any_system(
        registry,
        paper_id=paper_id,
        target_journal=journal_name,
    )
    program_binding = smart._program_dispatch_binding(
        paper_id,
        {
            "paper_id": paper_id,
            "target_journal": journal_name,
            "submission_system_id": "wiley",
        },
        registry,
        account_missing_policy="auto_register",
    )

    assert changed is True
    assert registry["journals"][0]["system_id"] == "wiley"
    assert repaired["status"] == "matched"
    assert repaired["system_id"] == "wiley"
    assert program_binding["dispatch_binding_ready"] is True
    assert program_binding["submission_system_id"] == "wiley"
    assert program_binding["target_origin"] == "https://authors.wiley.com:443"
    assert smart._routine_online_recovery_reason(
        {
            "status": "revision_needed",
            "workflow_status": "queued",
            "detail": (
                "Scheduler must resolve the target lock mismatch: the locked "
                "system is Springer Nature but the selected journal is Wiley."
            ),
        }
    ) == "site_binding_reconciliation"


def test_site_import_uses_selected_journal_before_backup_candidate_publishers(
    isolated_smart: Path,
) -> None:
    paper_id = "paper-selected-ccpe"
    paper_dir = isolated_smart / "papers" / paper_id
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    (paper_dir / "journal.md").write_text(
        "## Recommended Journal\n"
        "Concurrency and Computation: Practice and Experience\n\n"
        "## Backup candidates\n"
        "Mobile Networks and Applications, Springer Nature\n",
        encoding="utf-8",
    )
    registry = {
        "systems": [
            {
                "system_id": "springer_nature",
                "label": "Springer Nature",
                "status": "discovered",
                "login_url": "https://submission.springernature.com/",
                "submission_url": "https://submission.springernature.com/",
            },
            {
                "system_id": "wiley",
                "label": "Wiley Research Exchange",
                "status": "adapter_planned",
                "login_url": "",
                "submission_url": "",
            },
        ],
        "journals": [
            {
                "journal_key": "concurrency_and_computation_practice_and_experience",
                "journal_name": "Concurrency and Computation: Practice and Experience",
                "system_id": "springer_nature",
                "status": "discovered",
                "submission_url": "",
                "linked_papers": [],
            }
        ],
    }
    state = {
        "papers": {
            paper_id: {
                "paper_id": paper_id,
                "source_package_dir": str(package_dir),
            }
        }
    }

    result = smart._import_legacy_site_records(registry, state)
    journal = registry["journals"][0]
    wiley = next(
        item for item in registry["systems"] if item["system_id"] == "wiley"
    )

    assert result["changed"] is True
    assert journal["system_id"] == "wiley"
    assert journal["linked_papers"] == [paper_id]
    assert wiley["status"] == "discovered"
    assert wiley["submission_url"] == "https://authors.wiley.com/dashboard"


def test_current_wiley_portal_supersedes_retired_scholarone_package_url(
    isolated_smart: Path,
) -> None:
    paper_id = "paper-explicit-ccpe"
    journal_name = "Concurrency and Computation: Practice and Experience (Wiley)"
    paper_dir = isolated_smart / "papers" / paper_id
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    (paper_dir / "journal.md").write_text(
        "## Recommended Journal\n"
        f"{journal_name}\n\n"
        "Submission system: https://mc.manuscriptcentral.com/cpe\n"
        "Official article: "
        "https://onlinelibrary.wiley.com/doi/10.1002/cpe.70744\n",
        encoding="utf-8",
    )
    registry = smart._default_site_registry()
    registry["journals"] = [
        {
            "journal_key": "generic-ccpe-wiley",
            "journal_name": journal_name,
            "system_id": "wiley",
            "status": "discovered",
            "submission_url": "",
            "linked_papers": [paper_id],
        }
    ]
    state = {
        "papers": {
            paper_id: {
                "paper_id": paper_id,
                "source_package_dir": str(package_dir),
            }
        }
    }

    result = smart._import_legacy_site_records(registry, state)
    current = next(
        item
        for item in registry["journals"]
        if item.get("submission_url") == "https://authors.wiley.com/journal/CPE"
    )
    binding = smart._registry_binding_for_paper_any_system(
        registry,
        paper_id=paper_id,
        target_journal=journal_name,
    )

    assert result["changed"] is True
    assert current["system_id"] == "wiley"
    assert binding["status"] == "matched"
    assert binding["system_id"] == "wiley"
    assert binding["journal_site"]["submission_url"] == (
        "https://authors.wiley.com/journal/CPE"
    )

    projected = {
        "paper_id": paper_id,
        "target_journal": journal_name,
        "submission_system_id": "scholar_one",
        "submission_system": "ScholarOne Manuscripts",
        "submission_start_url": "https://mc.manuscriptcentral.com/cpe",
        "submission_start_url_source": "verified_site_registry",
        "credential_account_id": "stale-scholar-one-account",
    }
    reconciled = smart._reconcile_paper_site_binding(
        paper_id,
        projected,
        registry=registry,
        credentials={
            "accounts": [
                {
                    "account_id": "stale-scholar-one-account",
                    "system_id": "scholar_one",
                }
            ]
        },
        legacy_state={
            "papers": {
                paper_id: {
                    "current_journal": journal_name,
                    "submission_system": "ScholarOne Manuscripts",
                }
            }
        },
    )

    assert reconciled["changed"] is True
    assert projected["submission_system_id"] == "wiley"
    assert projected["submission_journal_id"] == "CPE"
    assert projected["submission_start_url"] == (
        "https://authors.wiley.com/journal/CPE"
    )
    assert "credential_account_id" not in projected


def test_submission_id_is_recovered_from_legacy_submission_state(
    isolated_smart: Path,
) -> None:
    smart.write_json_atomic(
        smart.LEGACY_SUBMISSION_STATE_PATH,
        {"papers": {"paper-legacy-id": {"manuscript_id": "LEGACY-26-77"}}},
    )

    recovered = smart._submission_id_from_local_evidence(
        "paper-legacy-id",
        {},
    )

    assert recovered == "LEGACY-26-77"


def test_inderscience_binding_inherits_trusted_system_url_for_exact_journal(
    isolated_smart: Path,
) -> None:
    paper_id = "paper-ijahuc"
    registry = {
        "systems": [
            {
                "system_id": "inderscience",
                "status": "discovered",
                "login_url": "https://www.indersciencesubmissions.com/",
                "submission_url": "https://www.indersciencesubmissions.com/",
            }
        ],
        "journals": [
            {
                "journal_key": "ijahuc_inderscience",
                "journal_name": (
                    "International Journal of Ad Hoc and Ubiquitous Computing "
                    "(IJAHUC), Inderscience Publishers."
                ),
                "system_id": "inderscience",
                "status": "discovered",
                "submission_url": "",
                "linked_papers": [],
            }
        ],
    }
    paper = {
        "paper_id": paper_id,
        "target_journal": "International Journal of Ad Hoc and Ubiquitous Computing",
        "submission_system_id": "inderscience",
        "status": "login_check",
        "workflow_status": "queued",
    }

    binding = smart._program_dispatch_binding(
        paper_id,
        paper,
        registry,
        account_missing_policy="auto_register",
    )

    assert binding["dispatch_binding_ready"] is True
    assert binding["submission_system_id"] == "inderscience"
    assert binding["journal_site_key"] == "ijahuc_inderscience"
    assert binding["target_origin"] == "https://www.indersciencesubmissions.com:443"
    assert binding["account_bootstrap_required"] is True


def _monitor_followup_test_state(
    action_kind: str,
    *,
    status: str = "revision_needed",
) -> dict[str, object]:
    return {
        "paper_id": "paper-followup",
        "status": status,
        "workflow_status": status,
        "worker_status": status,
        "route_revision": 4,
        "attempt_id": "mail-attempt",
        "thread_id": "mail-thread",
        "target_journal": "Example Journal",
        "submission_system_id": "example_system",
        "submission_id": "MS-26-0042",
        "last_monitor_result": status,
        "last_monitor_observation": {
            "at": "2026-07-18T03:00:00+08:00",
            "action_kind": action_kind,
            "summary": "The journal requested one bounded author action.",
            "message_date": "2026-07-18",
            "sender_domain": "journal.example",
            "submission_id": "MS-26-0042",
        },
    }


def test_scheduler_side_recovery_remains_an_executable_online_step(
    isolated_smart: Path,
) -> None:
    paper_id = "paper-scheduler-continuation"
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {paper_id: {}},
            "submission_routes": {
                paper_id: {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 2,
                }
            },
        },
    )
    state = smart._default_state()
    state["papers"] = {
        paper_id: {
            "paper_id": paper_id,
            "status": "blocked",
            "workflow_status": "blocked",
            "worker_status": "blocked",
            "detail": "Scheduler-side browser continuation is required for the article-type step.",
            "blocker": "article-type step",
            "online_intent": "submit_or_retarget",
            "route_revision": 2,
        }
    }

    recovered = smart._recover_auto_actionable_papers_in_state(state)
    paper = state["papers"][paper_id]

    assert recovered["online_recovered"] == [paper_id]
    assert paper["status"] == "uploading"
    assert paper["workflow_status"] == "queued"
    assert paper["worker_status"] == "queued"
    assert paper["online_intent"] == "submit_or_retarget"
    assert paper["next_action"] == "visible_worker_online_submit_or_monitor"
    assert state["online_queue"] == [paper_id]


def test_monitor_technical_followup_queues_existing_submission_once(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-followup": {}},
            "submission_routes": {
                "paper-followup": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 4,
                }
            },
        },
    )
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-followup": _monitor_followup_test_state("technical_correction")
    }
    state["online_queue"] = []
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    starts: list[list[str]] = []
    monkeypatch.setattr(
        smart,
        "_start_smart_monitor_followup_runtime",
        lambda paper_ids: starts.append(list(paper_ids))
        or {"ok": True, "started": True, "selected_ids": list(paper_ids)},
    )

    first = smart.apply_smart_monitor_followups(["paper-followup"])
    second = smart.apply_smart_monitor_followups(["paper-followup"])

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    assert first["auto_queued_count"] == 1
    assert second["deduplicated_count"] == 1
    assert starts == [["paper-followup"]]
    assert paper["status"] == "revision_needed"
    assert paper["workflow_status"] == "queued"
    assert paper["submission_id"] == "MS-26-0042"
    assert paper["online_intent"] == "post_submission_technical_correction"
    assert paper["next_action"] == "apply_technical_correction_to_existing_submission"
    assert paper["attempt_id"] != "mail-attempt"
    assert smart.read_json(smart.SMART_STATE_PATH, {})["online_queue"] == [
        "paper-followup"
    ]


def test_monitor_unknown_followup_waits_for_evidence_without_route_handoff(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    unified = {
        "papers": {"paper-followup": {}},
        "submission_routes": {
            "paper-followup": {
                "execution_owner": "legacy_auto",
                "mode": "production",
                "revision": 3,
            }
        },
    }
    smart.write_json_atomic(smart.UNIFIED_STATE_PATH, unified)
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-followup": _monitor_followup_test_state("unknown")
    }
    state["online_queue"] = []
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    class FakeOrchestrator:
        def sync(self):
            return smart.read_json(smart.UNIFIED_STATE_PATH, {})

        def handoff_existing_submission_to_smart(self, paper_id, **kwargs):
            current = self.sync()
            previous = dict(current["submission_routes"][paper_id])
            route = {
                "execution_owner": "smart",
                "mode": "canary",
                "revision": previous["revision"] + 1,
            }
            current["submission_routes"][paper_id] = route
            smart.write_json_atomic(smart.UNIFIED_STATE_PATH, current)
            return {
                "changed": True,
                "paper_id": paper_id,
                "previous_route": previous,
                "submission_route": route,
            }

    result = smart.apply_smart_monitor_followups(
        ["paper-followup"],
        activate=False,
        orchestrator=FakeOrchestrator(),
    )

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    route = smart.read_json(smart.UNIFIED_STATE_PATH, {})["submission_routes"][
        "paper-followup"
    ]
    assert result["route_handoff"]["changed"] == []
    assert result["online_queued"] == []
    assert result["identity_waiting"] == ["paper-followup"]
    assert route == {
        "execution_owner": "legacy_auto",
        "mode": "production",
        "revision": 3,
    }
    assert paper["route_revision"] == 4
    assert paper["workflow_status"] == "action_evidence_required"
    assert paper["last_monitor_followup_status"] == "action_evidence_required"


def test_monitor_rejection_retargets_once_and_excludes_duplicate_dispatch(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-followup": _monitor_followup_test_state(
            "retarget",
            status="rejected",
        )
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    dispatched: list[str] = []

    def fake_retarget(paper_id, _paper, _reason, _settings):
        dispatched.append(paper_id)
        return {
            "paper_id": paper_id,
            "request_id": "journal-request-1",
            "previous_target_journal": "Example Journal",
            "archive": {"package_archive": "archive"},
            "audit_path": "audit.md",
        }

    monkeypatch.setattr(smart, "_queue_journal_author_retarget", fake_retarget)

    first = smart.apply_smart_monitor_followups(["paper-followup"], activate=False)
    second = smart.apply_smart_monitor_followups(["paper-followup"], activate=False)

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    assert dispatched == ["paper-followup"]
    assert first["auto_queued_count"] == 1
    assert second["deduplicated_count"] == 1
    assert paper["status"] == "waiting"
    assert paper["workflow_status"] == "waiting_journal_author_retarget"
    assert paper["monitor_retarget_request_id"] == "journal-request-1"
    assert paper["last_monitor_followup_status"] == "journal_author_queued"
    identity = smart.submission_identity_snapshot(paper)
    assert identity["active_submission_ids"] == []
    assert identity["new_submission_allowed"] is True


def test_withdrawal_request_closes_online_before_journal_retarget(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-followup": {}},
            "submission_routes": {
                "paper-followup": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 4,
                }
            },
        },
    )
    paper = _monitor_followup_test_state("retarget")
    paper["last_monitor_observation"]["summary"] = (
        "Please withdraw this submission and submit the manuscript elsewhere."
    )
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {"paper-followup": paper}
    state["online_queue"] = []
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_queue_journal_author_retarget",
        lambda *_args, **_kwargs: pytest.fail(
            "journal retarget must wait until withdrawal is confirmed"
        ),
    )
    monkeypatch.setattr(
        smart,
        "_start_smart_monitor_followup_runtime",
        lambda paper_ids: {"ok": True, "started": True, "selected_ids": list(paper_ids)},
    )

    result = smart.apply_smart_monitor_followups(["paper-followup"])

    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"]["paper-followup"]
    assert result["online_queued"] == ["paper-followup"]
    assert result["retargeted"] == []
    assert paper["online_intent"] == "post_submission_withdraw_for_retarget"
    assert paper["next_action"] == "withdraw_existing_submission_before_retarget"
    assert paper["last_monitor_followup_status"] == "online_withdrawal_queued"
    assert smart.submission_identity_snapshot(paper)["mutation_allowed"] is True


def test_program_tick_retargets_explicit_rejection_without_ai_scheduler(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    paper = _monitor_followup_test_state("none", status="rejected")
    paper.pop("last_monitor_observation", None)
    paper["detail"] = "The journal explicitly rejected this manuscript."
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {"paper-followup": paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    queued: list[str] = []

    def fake_retarget(paper_id, _paper, _reason, _settings):
        queued.append(paper_id)
        return {
            "paper_id": paper_id,
            "request_id": "auto-request-1",
            "previous_target_journal": "Example Journal",
            "archive": {},
            "audit_path": "audit.md",
        }

    monkeypatch.setattr(smart, "_queue_journal_author_retarget", fake_retarget)

    result = smart.process_confirmed_smart_retargets(activate=False)

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    assert result["eligible"] == ["paper-followup"]
    assert queued == ["paper-followup"]
    assert paper["workflow_status"] == "waiting_journal_author_retarget"
    assert paper["last_monitor_followup_status"] == "journal_author_queued"


def test_monitor_technical_followup_blocks_duplicate_active_ids(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    paper = _monitor_followup_test_state("technical_correction")
    paper["submission_records"] = {
        "MS-26-0042": {
            "submission_id": "MS-26-0042",
            "submission_system_id": "example_system",
            "target_journal": "Example Journal",
            "active": True,
            "canonical": True,
        },
        "MS-26-0043": {
            "submission_id": "MS-26-0043",
            "submission_system_id": "example_system",
            "target_journal": "Example Journal",
            "active": True,
        },
    }
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {"paper-followup": paper}
    state["online_queue"] = []
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_start_smart_monitor_followup_runtime",
        lambda _paper_ids: pytest.fail("duplicate identities must never start an online worker"),
    )

    result = smart.apply_smart_monitor_followups(["paper-followup"])

    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"]["paper-followup"]
    assert result["online_queued"] == []
    assert result["identity_reconciliation_count"] == 1
    assert refreshed["online_queue"] == []
    assert paper["workflow_status"] == "identity_reconciliation_required"
    assert paper["blocker"] == smart.SMART_SUBMISSION_DUPLICATE_ACTIVE


def test_program_dispatch_binding_rejects_duplicate_active_ids() -> None:
    paper = {
        "paper_id": "paper-duplicate",
        "status": "ready_for_online_submit",
        "target_journal": "Example Journal",
        "submission_system_id": "snapp",
        "submission_id": "MS-1",
        "submission_records": {
            "MS-1": {
                "submission_id": "MS-1",
                "submission_system_id": "snapp",
                "target_journal": "Example Journal",
                "active": True,
                "canonical": True,
            },
            "MS-2": {
                "submission_id": "MS-2",
                "submission_system_id": "snapp",
                "target_journal": "Example Journal",
                "active": True,
            },
        },
    }

    binding = smart._program_dispatch_binding("paper-duplicate", paper, {})

    assert binding["dispatch_binding_ready"] is False
    assert binding["dispatch_binding_code"] == smart.SMART_SUBMISSION_DUPLICATE_ACTIVE
    assert binding["active_submission_ids"] == ["MS-1", "MS-2"]


def test_monitor_scientific_revision_never_uploads_unchanged_package(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-followup": _monitor_followup_test_state("scientific_revision")
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_start_smart_monitor_followup_runtime",
        lambda _paper_ids: pytest.fail("scientific revision must not start online upload"),
    )

    result = smart.apply_smart_monitor_followups(["paper-followup"])

    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"]["paper-followup"]
    assert result["manual_revision_count"] == 1
    assert result["auto_queued_count"] == 0
    assert refreshed.get("online_queue", []) == []
    assert paper["workflow_status"] == "waiting_revision_package"
    assert paper["next_action"] == "prepare_scientific_revision_package_from_decision"


def test_monitor_acceptance_is_archived_without_online_dispatch(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-followup": _monitor_followup_test_state("accepted", status="accepted")
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_start_smart_monitor_followup_runtime",
        lambda _paper_ids: pytest.fail("acceptance must not start online work"),
    )

    result = smart.apply_smart_monitor_followups(["paper-followup"])

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    assert result["accepted_count"] == 1
    assert paper["status"] == "accepted"
    assert paper["last_monitor_followup_status"] == "accepted_archived"


def test_completed_retarget_package_reenters_preflight_and_smart_sdk_once(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    package_dir = isolated_smart / "retarget-paper" / "package"
    package_dir.mkdir(parents=True)
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = _monitor_followup_test_state("retarget", status="waiting")
    paper.update(
        {
            "last_monitor_followup_status": "journal_author_queued",
            "monitor_retarget_request_id": "request-2",
            "final_submit_confirmed": True,
            "confirmation_url": "https://old.example/confirmation/MS-26-0042",
            "submission_evidence_path": "old-evidence.json",
            "submission_id_source": "existing_local_submission_evidence",
            "submission_id_recovered_at": "2026-07-19T01:00:00+08:00",
            "legacy_submission_status": {"status": "submitted"},
        }
    )
    state["papers"] = {"paper-followup": paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_journal_author_monitor_retarget_snapshot",
        lambda _paper_id, _request_id: {
            "ready": True,
            "status": "ready",
            "request_id": "request-2",
            "package_dir": str(package_dir),
            "paper_dir": str(package_dir.parent),
            "target_journal": "New Example Journal",
            "submission_system": "new-system",
            "contract": {"ok": True, "package_status": "READY_TO_SUBMIT"},
        },
    )
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        smart,
        "run_smart_submission_scheduler",
        lambda **kwargs: calls.append(("preflight", list(kwargs["selected_ids"])))
        or {"status": "completed", "processed": 1},
    )
    monkeypatch.setattr(
        smart,
        "queue_visible_online_workers",
        lambda paper_ids, **_kwargs: calls.append(("online", list(paper_ids)))
        or {"queued": list(paper_ids), "skipped": {}},
    )
    monkeypatch.setattr(
        smart,
        "_start_smart_monitor_followup_runtime",
        lambda paper_ids: calls.append(("runtime", list(paper_ids)))
        or {"ok": True, "started": True},
    )

    result = smart.reconcile_smart_monitor_followups()

    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"]["paper-followup"]
    assert result["ready"] == ["paper-followup"]
    assert calls == [
        ("preflight", ["paper-followup"]),
        ("online", ["paper-followup"]),
        ("runtime", ["paper-followup"]),
    ]
    assert paper["source_package_dir"] == str(package_dir)
    assert paper["target_journal"] == "New Example Journal"
    assert paper["submission_system"] == "new-system"
    assert paper["status"] == "queued"
    assert "submission_id" not in paper
    assert "submission_id_source" not in paper
    assert "submission_id_recovered_at" not in paper
    assert "final_submit_confirmed" not in paper
    assert paper["legacy_submission_status"] == {}
    assert paper["submission_attempt_history"][-1]["submission_id"] == "MS-26-0042"

    second = smart.reconcile_smart_monitor_followups()
    assert second["ready"] == []
    assert len(calls) == 3


def test_completed_package_repair_successor_advances_active_retarget(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    package_dir = isolated_smart / "retarget-paper" / "package"
    package_dir.mkdir(parents=True)
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = _monitor_followup_test_state("retarget", status="waiting")
    paper.update(
        {
            "last_monitor_followup_status": "journal_author_queued",
            "monitor_retarget_request_id": "selection-request",
            "retarget_request": {
                "request_id": "retarget-request",
                "status": "journal_author_queued",
                "mode": "ai",
                "excluded_journals": ["Rejected Journal"],
            },
        }
    )
    state["papers"] = {"paper-followup": paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_journal_author_monitor_retarget_snapshot",
        lambda _paper_id, _request_id: {
            "ready": True,
            "status": "ready",
            "request_id": "package-repair-request",
            "request_lineage": "package_repair_successor",
            "superseded_request_id": "selection-request",
            "package_dir": str(package_dir),
            "paper_dir": str(package_dir.parent),
            "target_journal": "New Example Journal",
            "submission_system": "new-system",
            "contract": {"ok": True, "package_status": "READY_TO_SUBMIT"},
        },
    )

    result = smart._prepare_completed_smart_monitor_retargets(["paper-followup"])

    saved = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    assert result["ready"] == ["paper-followup"]
    assert result["request_successors"] == [
        {
            "paper_id": "paper-followup",
            "previous_request_id": "selection-request",
            "request_id": "package-repair-request",
        }
    ]
    assert saved["monitor_retarget_request_id"] == "package-repair-request"
    assert saved["retarget_request"]["journal_author_request_id"] == "package-repair-request"
    assert saved["retarget_request"]["journal_author_request_history"] == [
        "selection-request"
    ]


def test_ai_retarget_rejects_historical_journal_and_prepares_bounded_retry(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = _monitor_followup_test_state("retarget", status="waiting")
    paper.update(
        {
            "target_journal": "Current Journal",
            "last_monitor_followup_status": "journal_author_queued",
            "monitor_retarget_request_id": "request-history-1",
            "retarget_request": {
                "schema": "smart-submission-retarget-request/v1",
                "request_id": "retarget-history-1",
                "paper_id": "paper-followup",
                "status": "journal_author_queued",
                "mode": "ai",
                "publication_tier": "sci",
                "priority": "acceptance",
                "withdrawal_policy": "confirmed_closed",
                "excluded_journals": ["Previously Rejected Journal"],
            },
        }
    )
    state["papers"] = {"paper-followup": paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_journal_author_monitor_retarget_snapshot",
        lambda _paper_id, _request_id: {
            "ready": True,
            "status": "ready",
            "request_id": "request-history-1",
            "package_dir": str(isolated_smart / "bad-package"),
            "paper_dir": str(isolated_smart / "bad-paper"),
            "target_journal": "Previously Rejected Journal",
            "submission_system": "example-system",
            "contract": {"ok": True, "package_status": "READY_TO_SUBMIT"},
        },
    )

    result = smart._prepare_completed_smart_monitor_retargets(["paper-followup"])

    saved = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-followup"]
    assert result["ready"] == []
    assert result["retry_selection"] == ["paper-followup"]
    assert saved["blocker"] == ""
    assert saved["next_action"] == "retry_journal_selection_excluding_history"
    assert saved["retarget_request"]["status"] == "ready_for_selection"
    assert saved["retarget_request"]["historical_target_retry_count"] == 1
    assert "Previously Rejected Journal" in saved["retarget_request"]["excluded_journals"]


def test_verified_rejection_projects_closed_status_to_legacy_ledger(
    isolated_smart: Path,
) -> None:
    smart.write_json_atomic(
        smart.LEGACY_SUBMISSION_STATE_PATH,
        {
            "papers": {
                "paper-rejected": {
                    "manuscript_id": "OLD-1",
                    "active_submission": True,
                    "workflow_stage": "submitted_active",
                    "current_journal": "Old Journal",
                }
            }
        },
    )

    result = smart.reconcile_legacy_submission_lifecycle(
        "paper-rejected",
        status="rejected",
        submission_id="OLD-1",
        target_journal="Old Journal",
        source="verified_dashboard_test",
        observed_at="2026-07-19T20:00:00+08:00",
    )

    legacy = smart.read_json(smart.LEGACY_SUBMISSION_STATE_PATH, {})
    record = legacy["papers"]["paper-rejected"]
    assert result["active_submission"] is False
    assert record["active_submission"] is False
    assert record["workflow_stage"] == "rejected"
    assert record["decision"] == "Reject"


def test_monitor_result_is_written_back_and_rejection_leaves_online_queue(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-monitor": {}},
            "submission_routes": {
                "paper-monitor": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 4,
                }
            },
        },
    )
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["online_queue"] = ["paper-monitor"]
    state["papers"] = {
        "paper-monitor": {
            "paper_id": "paper-monitor",
            "status": "monitoring",
            "workflow_status": "running",
            "worker_status": "running",
            "monitor_check_pending": True,
            "attempt_id": "attempt-monitor",
            "thread_id": "thread-monitor",
            "route_revision": 4,
            "target_journal": "Example Journal",
            "submission_system_id": "example_system",
            "worker_dispatch_had_prior_submission_evidence": True,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_paper_has_prior_trusted_submission_evidence",
        lambda _paper_id, _paper: True,
    )
    monkeypatch.setattr(
        smart,
        "_paper_has_prior_submission_evidence",
        lambda _paper: True,
    )

    result = smart.record_visible_worker_result(
        "paper-monitor",
        route_revision=4,
        attempt_id="attempt-monitor",
        status="rejected",
        next_action="return to journal selection",
        detail="The journal declined the manuscript.",
        thread_id="thread-monitor",
        source="scheduled_monitor_dashboard",
        activate=False,
    )

    refreshed = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = refreshed["papers"]["paper-monitor"]
    assert result["monitor_check_completed"] is True
    assert result["removed_from_online_queue"] is True
    assert refreshed["online_queue"] == []
    assert paper["status"] == "rejected"
    assert paper["workflow_status"] == "retarget_needed"
    assert "monitor_check_pending" not in paper
    assert paper["last_monitor_checked_at"]
    assert paper["last_monitor_result"] == "rejected"


def test_credential_broker_preserves_pending_monitor_checkpoint(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["online_queue"] = ["paper-monitor"]
    state["papers"] = {
        "paper-monitor": {
            "paper_id": "paper-monitor",
            "status": "monitoring",
            "workflow_status": "queued",
            "worker_status": "queued",
            "monitor_check_pending": True,
            "next_action": smart.SCHEDULED_MONITORING_NEXT,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    paper = smart.record_smart_credential_broker_state(
        "paper-monitor",
        {
            "status": "session_ready",
            "code": "SMART_CREDENTIAL_BROKER_SESSION_READY",
            "account_id": "account-1",
            "origin": "https://example.test",
            "next_action": "dispatch_fixed_sdk_session",
        },
    )

    assert paper["status"] == "monitoring"
    assert paper["workflow_status"] == "queued"
    assert paper["monitor_check_pending"] is True
    assert paper["next_action"] == smart.SCHEDULED_MONITORING_NEXT


def test_credential_broker_access_failure_does_not_block_submitted_paper(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["online_queue"] = ["paper-monitor"]
    state["papers"] = {
        "paper-monitor": {
            "paper_id": "paper-monitor",
            "status": "monitoring",
            "workflow_status": "queued",
            "worker_status": "queued",
            "monitor_check_pending": True,
            "next_action": smart.SCHEDULED_MONITORING_NEXT,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    paper = smart.record_smart_credential_broker_state(
        "paper-monitor",
        {
            "status": "login_incomplete",
            "code": "SMART_CREDENTIAL_BROKER_WAITING_HUMAN",
            "account_id": "account-1",
            "origin": "https://example.test",
            "next_action": "continue_visible_login_then_resume",
        },
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert paper["status"] == "monitoring"
    assert paper["workflow_status"] == "monitoring"
    assert "monitor_check_pending" not in paper
    assert paper["last_monitor_check_outcome"] == "credential_login_incomplete"
    assert persisted["online_queue"] == []


def test_credential_broker_retry_is_a_completed_monitor_outcome(
    isolated_smart: Path,
) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["online_queue"] = ["paper-monitor"]
    state["papers"] = {
        "paper-monitor": {
            "paper_id": "paper-monitor",
            "status": "monitoring",
            "workflow_status": "queued",
            "worker_status": "queued",
            "monitor_check_pending": True,
            "next_action": smart.SCHEDULED_MONITORING_NEXT,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    paper = smart.record_smart_credential_broker_state(
        "paper-monitor",
        {
            "status": "broker_retry_required",
            "code": "SMART_CREDENTIAL_BROKER_SAFE_FAILURE",
            "origin": "https://example.test",
            "next_action": "retry_credential_broker",
        },
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert paper["status"] == "monitoring"
    assert paper["workflow_status"] == "monitoring"
    assert "monitor_check_pending" not in paper
    assert paper["last_monitor_check_outcome"] == "credential_broker_retry_required"
    assert paper["credential_broker_next_action"] == "retry_credential_broker"
    assert persisted["online_queue"] == []


def test_secret_broker_worker_block_is_monitoring_access_failure() -> None:
    paper = {
        "paper_id": "paper-monitor",
        "status": "monitoring",
        "workflow_status": "monitoring",
        "submission_id": "MS-26-1",
        "legacy_submission_status": {"status": "submitted"},
    }

    assert smart._is_monitoring_access_failure(
        paper,
        status="blocked",
        next_action="SMART_SECRET_BROKER_REQUIRED",
        detail="The journal login required a trusted credential broker.",
        source="codex_sdk_journal_dashboard",
    ) is True


@pytest.mark.parametrize("status", ["incomplete", "not_submitted", "inactive"])
def test_legacy_negative_statuses_are_not_success(status: str) -> None:
    record = {"stages": {"submission": {"status": status, "raw_status": status}}}
    assert smart._initial_status_from_unified_record(record)["status"] == "queued"
    assert smart._paper_has_prior_submission_evidence(
        {"legacy_submission_status": {"status": status}, "detail": status}
    ) is False


def test_target_hydration_derives_springer_new_submission_url(
    isolated_smart: Path,
) -> None:
    paper_id = "paper-springer"
    paper_dir = smart.JOURNAL_ROOT / "papers" / paper_id
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    (paper_dir / "journal.md").write_text(
        "\n".join(
            (
                "# Journal Decision",
                "",
                "## Recommended Journal",
                "The Journal of Supercomputing (Springer Nature)",
                "",
                "- Journal homepage: https://link.springer.com/journal/11227",
            )
        ),
        encoding="utf-8",
    )
    paper = {
        "paper_id": paper_id,
        "paper_dir": str(paper_dir),
        "source_package_dir": str(package_dir),
        "target_journal": "The Journal of Supercomputing",
        "submission_system_id": "springer_nature",
    }

    context = smart._hydrate_paper_target_from_package(paper_id, paper)

    assert context["submission_journal_id"] == "11227"
    assert paper["submission_journal_id"] == "11227"
    assert (
        paper["submission_start_url"]
        == "https://submission.nature.com/new-submission/11227/3"
    )
    assert paper["submission_start_url_source"] == "package_adapter"


def test_terminal_worker_result_requires_verifiable_evidence(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart.ensure_smart_submission_module()
    smart.write_json_atomic(
        smart.UNIFIED_STATE_PATH,
        {
            "papers": {"paper-1": {}},
            "submission_routes": {
                "paper-1": {"execution_owner": "smart", "mode": "canary", "revision": 2}
            },
        },
    )
    paper_dir = isolated_smart / "paper-1"
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    (package_dir / "main.pdf").write_bytes(b"%PDF-1.4\nformal submission artifact\n")
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "attempt_id": "attempt-1",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "credential_account_id": "account-1",
            "submission_system_id": "test_system",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-1",
            "draft_url": "https://example.test/draft/DRAFT-1",
            "upload_url": "https://example.test/draft/DRAFT-1/files",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(smart, "_require_current_worker_auth", lambda _paper_id, _paper: object())
    monkeypatch.setattr(
        smart,
        "_final_submit_target_binding",
        lambda _paper_id, _paper: {
            "target_origin": "https://example.test:443",
            "journal_site_key": "example_journal",
            "account_binding_sha256": "a" * 64,
            "site_binding_sha256": "b" * 64,
            "draft_binding_sha256": "c" * 64,
        },
    )
    challenge = smart.prepare_final_submit_confirmation_challenge(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        ui_window_handle=1,
    )
    authorized = smart.authorize_final_submit(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        confirmation_phrase=challenge["confirmation_phrase"],
        activate=False,
    )
    smart.record_visible_worker_dispatch(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
    )
    smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        authorization_id=authorized["authorization_id"],
    )

    before_missing_capture = smart.SMART_STATE_PATH.read_bytes()
    with pytest.raises(smart.SmartSubmissionGuardError) as missing_capture:
        smart.record_visible_worker_result(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-1",
            status="submitted",
            activate=False,
        )
    assert missing_capture.value.code == smart.SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED
    assert smart.SMART_STATE_PATH.read_bytes() == before_missing_capture

    capture = smart.capture_live_submission_confirmation(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        authorization_id=authorized["authorization_id"],
        transport=FakeConfirmationTransport(),
    )
    real_commit_audit = smart._commit_prepared_audit

    def fail_result_audit_commit(_prepared: dict[str, object]) -> Path:
        raise OSError("fault injected result audit rename")

    monkeypatch.setattr(smart, "_commit_prepared_audit", fail_result_audit_commit)
    with pytest.raises(OSError, match="fault injected"):
        smart.record_visible_worker_result(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-1",
            status="submitted",
            submission_id=capture["manuscript_id"],
            evidence_path=capture["evidence_path"],
            confirmation_url=capture["confirmation_url"],
            activate=False,
        )
    interrupted = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert interrupted["status"] == "submitted"
    assert smart._paper_has_prior_trusted_submission_evidence("paper-1", interrupted) is False
    assert list(smart.SMART_AUDIT_DIR.glob("*.pending"))

    monkeypatch.setattr(smart, "_commit_prepared_audit", real_commit_audit)
    recovery = smart.ensure_smart_submission_module()["transaction_recovery"]
    assert recovery["changed"] is True
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert persisted["papers"]["paper-1"]["submission_evidence_path"] == capture["evidence_path"]
    assert smart._paper_has_prior_trusted_submission_evidence(
        "paper-1", persisted["papers"]["paper-1"]
    ) is True
    assert not list(smart.SMART_AUDIT_DIR.glob("*.pending"))
    assert "paper-1" not in persisted["online_queue"]


def test_canonical_prompt_backs_up_and_rebuilds_drift(isolated_smart: Path) -> None:
    smart.ensure_smart_submission_module()
    smart.SMART_SCHEDULER_PROMPT_PATH.write_text("# drifted prompt\n", encoding="utf-8")

    result = smart.ensure_smart_submission_module()

    prompt = smart.SMART_SCHEDULER_PROMPT_PATH.read_text(encoding="utf-8")
    assert f"SMART_SUBMISSION_PROMPT_VERSION={smart.SMART_PROMPT_VERSION}" in prompt
    assert all(section in prompt for section in smart.SMART_PROMPT_REQUIRED_SECTIONS)
    assert result["prompt"]["backup_path"]
    assert Path(result["prompt"]["backup_path"]).read_text(encoding="utf-8") == "# drifted prompt\n"


@pytest.mark.parametrize(
    ("artifact_kind", "expected_workflow"),
    [
        ("authorization", "waiting"),
        ("reservation", "blocked"),
        ("consumption", "blocked"),
        ("dispatch", "blocked"),
    ],
)
def test_unsafe_pending_recovery_is_replay_safe_across_state_write_crash(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_kind: str,
    expected_workflow: str,
) -> None:
    smart.SMART_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    def artifact(name: str, *, committed: bool) -> tuple[Path, str, Path]:
        final_path = smart.SMART_AUDIT_DIR / f"{name}.md"
        pending_path = final_path.with_name(f"{final_path.name}.pending")
        payload = f"# {name}\n".encode()
        (final_path if committed else pending_path).write_bytes(payload)
        return final_path, hashlib.sha256(payload).hexdigest(), pending_path

    paper: dict[str, object] = {
        "paper_id": "paper-unsafe",
        "status": "ready_for_online_submit",
        "workflow_status": "queued",
        "worker_status": "queued",
        "thread_id": "thread-unsafe",
        "next_action": "final_submit_authorized",
    }
    unsafe_pending: Path
    if artifact_kind == "authorization":
        auth_path, auth_sha, unsafe_pending = artifact("authorization", committed=False)
        paper["final_submit_authorization"] = {
            "authorization_audit_path": str(auth_path),
            "authorization_audit_sha256": auth_sha,
        }
    elif artifact_kind in {"reservation", "consumption"}:
        auth_path, auth_sha, _ = artifact("authorization", committed=True)
        reservation_path, reservation_sha, reservation_pending = artifact(
            "reservation",
            committed=artifact_kind == "consumption",
        )
        authorization: dict[str, object] = {
            "authorization_audit_path": str(auth_path),
            "authorization_audit_sha256": auth_sha,
            "reserved_at": "2026-07-11T12:00:00+00:00",
            "reservation_audit_path": str(reservation_path),
            "reservation_audit_sha256": reservation_sha,
        }
        if artifact_kind == "consumption":
            consumption_path, consumption_sha, unsafe_pending = artifact("consumption", committed=False)
            authorization.update(
                {
                    "consumed_at": "2026-07-11T12:00:01+00:00",
                    "consumption_audit_path": str(consumption_path),
                    "consumption_audit_sha256": consumption_sha,
                }
            )
        else:
            unsafe_pending = reservation_pending
        paper["final_submit_authorization"] = authorization
    else:
        dispatch_path, dispatch_sha, unsafe_pending = artifact("dispatch", committed=False)
        paper.update(
            {
                "workflow_status": "running",
                "worker_status": "running",
                "last_dispatch_audit_path": str(dispatch_path),
                "last_dispatch_audit_sha256": dispatch_sha,
            }
        )

    state = smart._default_state()
    state["mode"] = "run_once"
    state["online_queue"] = ["paper-unsafe"]
    state["papers"] = {"paper-unsafe": paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    real_write = smart.write_json_atomic

    def fail_authoritative_write(path: Path, value: object) -> None:
        if Path(path) == smart.SMART_STATE_PATH:
            raise OSError("fault injected state write")
        real_write(path, value)

    monkeypatch.setattr(smart, "write_json_atomic", fail_authoritative_write)
    with pytest.raises(OSError, match="fault injected state write"):
        smart.ensure_smart_submission_module()

    # The capability-bearing pending file is gone, but the unchanged state is
    # intentionally still unsafe.  The next run must re-derive the safe action
    # from the now-invalid artifact instead of treating it as committed.
    assert not unsafe_pending.exists()
    interrupted = json.loads(smart.SMART_STATE_PATH.read_text(encoding="utf-8"))
    assert "paper-unsafe" in interrupted["online_queue"]

    monkeypatch.setattr(smart, "write_json_atomic", real_write)
    recovered = smart.ensure_smart_submission_module()
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    safe_paper = persisted["papers"]["paper-unsafe"]
    assert recovered["transaction_recovery"]["changed"] is True
    assert safe_paper["workflow_status"] == expected_workflow
    assert safe_paper["worker_status"] == expected_workflow
    assert "final_submit_authorization" not in safe_paper
    assert "paper-unsafe" not in persisted["online_queue"]
    assert persisted["mode"] == "manual"
    assert smart.ensure_smart_submission_module()["transaction_recovery"]["changed"] is False


def test_generic_pending_worker_audit_is_recovered_idempotently(isolated_smart: Path) -> None:
    smart.SMART_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    final_path = smart.SMART_AUDIT_DIR / "blocked-worker-result.md"
    pending_path = final_path.with_name(f"{final_path.name}.pending")
    payload = b"# blocked worker result\n"
    pending_path.write_bytes(payload)
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "blocked",
            "workflow_status": "blocked",
            "worker_status": "blocked",
            "last_audit_path": str(final_path),
            "last_audit_sha256": hashlib.sha256(payload).hexdigest(),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    first = smart.ensure_smart_submission_module()
    second = smart.ensure_smart_submission_module()

    assert first["transaction_recovery"]["changed"] is True
    assert final_path.read_bytes() == payload
    assert not pending_path.exists()
    assert second["transaction_recovery"]["changed"] is False


def test_canonical_prompt_never_delegates_secret_or_account_mutation() -> None:
    prompt = smart._default_prompt()

    assert "Never read a credential file" in prompt
    assert "SMART_SECRET_BROKER_REQUIRED" in prompt
    assert "must not create an account or persist a new password" in prompt
    assert "Read `smart_submission_state.json`, `site_registry.json`, and `credential_store.local.json`" not in prompt
    assert "Register a fresh journal-system account" not in prompt
    assert "migration plaintext credential export" not in prompt


def test_context_json_redacts_nested_secret_material(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    path.write_text(
        json.dumps(
            {
                "safe": "visible",
                "nested": {
                    "password": "must-not-appear",
                    "credential_ref": "must-not-appear-either",
                    "safe_child": "kept",
                },
                "items": [{"verification_code": "123456", "label": "kept-too"}],
            }
        ),
        encoding="utf-8",
    )

    rendered = smart._context_json_snippet("metadata", path)

    assert "must-not-appear" not in rendered
    assert "123456" not in rendered
    assert "visible" in rendered
    assert "kept" in rendered


def test_scheduler_contract_claims_before_send_and_never_substitutes_attempt() -> None:
    prompt = smart._visible_scheduler_prompt_section()
    rule = next(line for line in prompt.splitlines() if line.startswith("16. For each dispatch candidate"))
    assert rule.index("record-worker-dispatch") < rule.index("claim-final-submit-action")
    assert rule.index("claim-final-submit-action") < rule.index("smart-submission-worker-prompt")
    assert "If any command or prompt generation fails, send nothing" in rule
    assert "never replace missing or stale worker markers with current state values" in prompt


def test_snapp_payload_never_invents_author_identity(tmp_path: Path) -> None:
    paper_dir = tmp_path / "paper"
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    (paper_dir / "author_info.md").write_text("Name: Verified Name\n", encoding="utf-8")

    payload = smart._prepare_snapp_scheduler_payload(
        "paper-1",
        {"target_journal": "Example", "submission_system": "Springer Nature SNAPP"},
        package_dir,
        paper_dir,
        {},
    )

    assert payload["ready"] is False
    assert payload["corresponding_author"]["name"] == "Verified Name"
    assert payload["corresponding_author"]["email"] == ""
    assert payload["corresponding_author"]["affiliation"] == ""
    assert payload["corresponding_author"]["country"] == ""
    assert payload["blockers"]


def test_snapp_payload_reads_numbered_affiliation_and_author_contribution(
    tmp_path: Path,
) -> None:
    paper_dir = tmp_path / "paper"
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    (paper_dir / "author_info.md").write_text(
        """# Author Information

Status: READY

## Authors

1. Name: Verified Name
   Prefix: Dr.
   Affiliation: 1
   Email: verified@example.org

## Affiliations

1. Institution: Example University
   Department: Computer Science
   City: Example City
   Country: China
   Postal Code: 100000

## Corresponding Author

Prefix: Dr.
Name: Verified Name
Email: verified@example.org

## Author Contributions

Verified Name conceived the study, performed the analysis, and wrote the manuscript.
""",
        encoding="utf-8",
    )

    payload = smart._prepare_snapp_scheduler_payload(
        "paper-1",
        {
            "target_journal": "The Journal of Supercomputing",
            "submission_system": "Springer Nature SNAPP",
        },
        package_dir,
        paper_dir,
        {},
    )

    assert payload["ready"] is True
    assert payload["corresponding_author"]["prefix"] == "Dr."
    assert payload["corresponding_author"]["affiliation"] == "Example University"
    assert payload["corresponding_author"]["country"] == "China"
    assert payload["author_contribution_statement"].startswith("Verified Name conceived")
    assert payload["blockers"] == []


def test_scheduler_payload_uses_saved_default_author_prefix(tmp_path: Path) -> None:
    stage_root = tmp_path / "journal-author"
    paper_dir = stage_root / "papers" / "paper-1"
    package_dir = paper_dir / "package"
    controller_dir = stage_root / "controller"
    package_dir.mkdir(parents=True)
    controller_dir.mkdir(parents=True)
    (paper_dir / "author_info.md").write_text(
        """# Author Information

## Authors

1. Name: Verified Name
   Email: verified@example.org

## Affiliations

1. Institution: Example University
   Country: China
""",
        encoding="utf-8",
    )
    (controller_dir / "author_templates.json").write_text(
        json.dumps(
            {
                "default_template_id": "template_1",
                "templates": [
                    {
                        "id": "template_1",
                        "form": {
                            "corresponding_author": {
                                "prefix": "Dr.",
                                "name": "Verified Name",
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = smart._prepare_snapp_scheduler_payload(
        "paper-1",
        {"target_journal": "Example", "submission_system": "Wiley"},
        package_dir,
        paper_dir,
        {},
    )

    assert payload["ready"] is True
    assert payload["corresponding_author"]["prefix"] == "Dr."


def test_author_field_parser_keeps_blank_optional_fields_on_their_own_line() -> None:
    author_text = """## Affiliations

1. Institution: Example University
   Department:
   City:
   Country: China
   Postal Code:

## Corresponding Author

Name: Verified Name
"""

    assert smart._author_field_value(author_text, "Institution") == "Example University"
    assert smart._author_field_value(author_text, "Department") == ""
    assert smart._author_field_value(author_text, "City") == ""
    assert smart._author_field_value(author_text, "Country") == "China"
    assert smart._author_field_value(author_text, "Postal Code") == ""


def test_author_payload_preflight_blocks_early_and_recovers_after_local_fix(
    isolated_smart: Path,
    tmp_path: Path,
) -> None:
    smart.ensure_smart_submission_module()
    paper_id = "paper-1"
    paper_dir = tmp_path / "journal" / "papers" / paper_id
    package_dir = paper_dir / "package"
    package_dir.mkdir(parents=True)
    author_info = paper_dir / "author_info.md"
    base_text = """# Author Information

Status: READY

## Authors

1. Name: Verified Name
   Affiliation: 1
   Email: verified@example.org

## Affiliations

1. Institution: Example University
   Country: China

## Corresponding Author

Name: Verified Name
Email: verified@example.org
"""
    author_info.write_text(base_text, encoding="utf-8")
    state = smart._default_state()
    state["papers"] = {
        paper_id: {
            "paper_id": paper_id,
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "target_journal": "The Journal of Supercomputing",
            "submission_system": "Springer Nature / SNAPP",
            "status": "blocked",
            "workflow_status": "blocked",
            "worker_status": "blocked",
            "next_action": smart.AUTHOR_INFO_COMPLETION_ACTION,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    blocked = smart._refresh_selected_author_payload_preflight([paper_id])

    assert blocked["recovered"] == []
    assert list(blocked["blocked"]) == [paper_id]
    assert "author_contribution_statement" in blocked["blocked"][paper_id]
    assert "corresponding_author.affiliation" not in blocked["blocked"][paper_id]

    author_info.write_text(
        base_text
        + """

## Author Contributions

Verified Name conceived the study, performed the analysis, and wrote the manuscript.
""",
        encoding="utf-8",
    )
    recovered = smart._refresh_selected_author_payload_preflight([paper_id])
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"][paper_id]

    assert recovered["recovered"] == [paper_id]
    assert recovered["blocked"] == {}
    assert paper["status"] == "login_check"
    assert paper["workflow_status"] == "queued"
    assert paper["next_action"] == "trusted_credential_broker_login"
    assert paper["blocker"] == ""


def test_pause_is_hard_pause_without_requeue_or_wakeup(isolated_smart: Path) -> None:
    smart.ensure_smart_submission_module()
    state = smart.read_json(smart.SMART_STATE_PATH, {})
    state["online_queue"] = ["paper-1"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    legacy = smart._smart_automation_toml(enabled=False, state=state)
    smart.write_text(
        smart.SMART_AUTOMATION_PATH,
        smart._set_toml_value(legacy, "status", "ACTIVE"),
    )

    result = smart.pause_smart_submission_automation("test")

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    automation = smart.SMART_AUTOMATION_PATH.read_text(encoding="utf-8")
    assert result["automation_status"] == "PAUSED"
    assert persisted["mode"] == "manual"
    assert persisted["online_queue"] == ["paper-1"]
    assert persisted["settings"]["online_submit_enabled"] is False
    assert persisted["settings"]["program_timer_enabled"] is False
    assert 'status = "PAUSED"' in automation


def test_program_timer_reconcile_retires_legacy_and_records_single_owner(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = smart._default_state()
    legacy = smart._smart_automation_toml(enabled=False, state=state)
    smart.write_text(
        smart.SMART_AUTOMATION_PATH,
        smart._set_toml_value(legacy, "status", "ACTIVE"),
    )
    monkeypatch.setattr(
        smart,
        "_register_native_smart_monitor_task",
        lambda time_local: {
            "ok": True,
            "registered": True,
            "status": "ready",
            "time_local": time_local,
            "action": rf"D:\App\UnifiedPaperOrchestrator.exe {smart.SMART_PROGRAM_TIMER_COMMAND}",
        },
    )

    result = smart.reconcile_smart_submission_program_timer()

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    settings = persisted["settings"]
    assert result["ok"] is True
    assert result["owner"] == "program_timer"
    assert settings["trigger_owner"] == "program_timer"
    assert settings["visible_scheduler_enabled"] is False
    assert settings["legacy_automation_retired"] is True
    assert settings["native_monitor_task_registered"] is True
    assert smart.SMART_PROGRAM_TIMER_COMMAND in settings["native_monitor_task_action"]
    assert 'status = "PAUSED"' in smart.read_text(smart.SMART_AUTOMATION_PATH)


def test_status_projection_is_read_only_and_does_not_initialize_registries(isolated_smart: Path) -> None:
    isolated_smart.mkdir(parents=True)
    state = smart._default_state()
    state["updated_at"] = "2026-01-01T00:00:00+08:00"
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "monitoring",
            "workflow_status": "running",
            "worker_status": "running",
            "thread_id": "thread-1",
        }
    }
    state["online_queue"] = ["paper-1"]
    state["active_worker_count"] = 99
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    before = smart.SMART_STATE_PATH.read_bytes()

    result = smart.smart_submission_status()

    assert result["projection_only"] is True
    assert result["normalization_pending"] is True
    assert result["online_queue"] == []
    assert result["active_worker_count"] == 0
    assert result["persisted_active_worker_count_ignored"] == 99
    assert result["updated_at"] == "2026-01-01T00:00:00+08:00"
    assert smart.SMART_STATE_PATH.read_bytes() == before
    assert not smart.SMART_CREDENTIAL_PATH.exists()
    assert not smart.SMART_SITE_REGISTRY_PATH.exists()
    assert not smart.SMART_SCHEDULER_PROMPT_PATH.exists()


def test_status_fails_closed_on_corrupt_referenced_vault_without_plaintext_fallback(
    isolated_smart: Path,
) -> None:
    isolated_smart.mkdir(parents=True)
    smart.write_json_atomic(smart.SMART_STATE_PATH, smart._default_state())
    smart.write_json_atomic(
        smart.SMART_CREDENTIAL_PATH,
        {
            "accounts": [
                {
                    "account_id": "broken-ref",
                    "scope": "journal_system",
                    "username": "test-user",
                    "credential_ref": "vault:broken-ref",
                    "password": "must-not-fallback",
                    "status": "verified",
                }
            ]
        },
    )
    smart.SMART_CREDENTIAL_VAULT_PATH.write_text("{broken", encoding="utf-8")
    state_before = smart.SMART_STATE_PATH.read_bytes()
    credentials_before = smart.SMART_CREDENTIAL_PATH.read_bytes()
    vault_before = smart.SMART_CREDENTIAL_VAULT_PATH.read_bytes()

    result = smart.smart_submission_status()

    assert result["configured_account_count"] == 0
    assert "must-not-fallback" not in json.dumps(result, ensure_ascii=False)
    assert smart.SMART_STATE_PATH.read_bytes() == state_before
    assert smart.SMART_CREDENTIAL_PATH.read_bytes() == credentials_before
    assert smart.SMART_CREDENTIAL_VAULT_PATH.read_bytes() == vault_before


def test_module_initialization_does_not_create_account_or_site_registries(isolated_smart: Path) -> None:
    result = smart.ensure_smart_submission_module()

    assert smart.SMART_STATE_PATH.exists()
    assert not smart.SMART_CREDENTIAL_PATH.exists()
    assert not smart.SMART_SITE_REGISTRY_PATH.exists()
    assert str(smart.SMART_CREDENTIAL_PATH) not in result["created"]
    assert str(smart.SMART_SITE_REGISTRY_PATH) not in result["created"]


def test_prepare_dispatch_preserves_selected_scope(isolated_smart: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def start(paper_id, **kwargs):
        calls["paper_id"] = paper_id
        calls["revision"] = kwargs["expected_route_revision"]
        calls["orchestrator"] = kwargs["orchestrator"]
        return {"ok": True, "code": "SMART_CANARY_QUEUED"}

    monkeypatch.setattr(smart, "start_single_smart_canary", start)
    orchestrator = object()

    result = smart.prepare_visible_online_dispatch(
        {},
        selected_ids=["paper-1"],
        expected_route_revision=3,
        orchestrator=orchestrator,
    )

    assert calls == {
        "paper_id": "paper-1",
        "revision": 3,
        "orchestrator": orchestrator,
    }
    assert result["code"] == "SMART_CANARY_QUEUED"


def test_legacy_autofill_is_disabled_in_unattended_scheduler(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration_dir = tmp_path / "legacy-migration"
    migration_dir.mkdir()
    monkeypatch.setattr(smart, "LEGACY_MIGRATION_OUTPUT_DIR", migration_dir)
    (migration_dir / "credential_store_plaintext_test.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "legacy-one",
                        "site_name": "Example Submit",
                        "site_url": "https://submit.example.test/login",
                        "username": "example-user",
                        "password": "legacy-preview-secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    smart.LEGACY_SUBMISSION_STATE_PATH.write_text(
        json.dumps(
            {
                "papers": {
                    "paper-1": {
                        "current_journal": "Example Journal",
                        "submission_system": "Springer Nature",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    isolated_smart.mkdir(parents=True, exist_ok=True)
    credentials = {
        "accounts": [
            {
                "account_id": "legacy-legacy-one",
                "scope": "journal_system",
                "provider": "Example Submit",
                "login_url": "https://submit.example.test/login",
                "username": "example-user",
                "credential_ref": "vault:existing-reference",
                "status": "verified",
            }
        ]
    }
    sites = {"systems": [], "journals": []}
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)
    smart.write_json_atomic(smart.SMART_SITE_REGISTRY_PATH, sites)
    credentials_before = smart.SMART_CREDENTIAL_PATH.read_bytes()
    sites_before = smart.SMART_SITE_REGISTRY_PATH.read_bytes()

    monkeypatch.setattr(
        smart,
        "_import_legacy_credentials",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("plaintext export must not be parsed")),
    )
    summary = smart._autofill_from_legacy_sources(
        credentials,
        sites,
        {"papers": {}},
        {
            "auto_import_legacy_credentials": True,
            "auto_import_legacy_site_records": True,
        },
    )

    assert summary["mode"] == "disabled_by_secret_boundary"
    assert summary["writes_performed"] is False
    assert "legacy-preview-secret" not in json.dumps(summary, ensure_ascii=False)
    assert "password" not in credentials["accounts"][0]
    assert smart.SMART_CREDENTIAL_PATH.read_bytes() == credentials_before
    assert smart.SMART_SITE_REGISTRY_PATH.read_bytes() == sites_before


def test_new_state_disables_unattended_legacy_discovery() -> None:
    settings = smart._default_state()["settings"]
    assert settings["auto_import_legacy_credentials"] is False
    assert settings["auto_import_legacy_site_records"] is False
    assert settings["legacy_import_preview_enabled"] is False


def test_cli_evidence_arguments_and_state_compaction() -> None:
    args = build_parser().parse_args(
        [
            "smart-submission-record-worker-result",
            "paper-1",
            "--route-revision",
            "2",
            "--attempt-id",
            "attempt-1",
            "--thread-id",
            "thread-1",
            "--status",
            "submitted",
            "--submission-id",
            "MS-123",
            "--evidence-path",
            "confirmation.txt",
            "--confirmation-url",
            "https://example.test/confirmation",
        ]
    )
    assert args.submission_id == "MS-123"
    assert args.route_revision == 2
    assert args.attempt_id == "attempt-1"
    assert args.evidence_path == "confirmation.txt"
    assert args.confirmation_url == "https://example.test/confirmation"

    claim_args = build_parser().parse_args(
        [
            "smart-submission-claim-final-submit-action",
            "paper-1",
            "--route-revision",
            "2",
            "--attempt-id",
            "attempt-1",
            "--thread-id",
            "thread-1",
            "--authorization-id",
            "a" * 32,
        ]
    )
    assert claim_args.thread_id == "thread-1"
    assert claim_args.authorization_id == "a" * 32
    capture_args = build_parser().parse_args(
        [
            "smart-submission-capture-confirmation",
            "paper-1",
            "--route-revision",
            "2",
            "--attempt-id",
            "attempt-1",
            "--thread-id",
            "thread-1",
            "--authorization-id",
            "a" * 32,
        ]
    )
    assert capture_args.command == "smart-submission-capture-confirmation"
    assert "smart-submission-authorize-final-submit" not in build_parser().format_help()

    compact = _compact_smart_cli_payload(
        {
            "state": {"papers": {"secret": {}}, "history": [{"large": True}]},
            "history_tail": [{"large": True}],
            "ok": True,
        }
    )
    assert compact == {"ok": True}


def test_user_retarget_with_active_submission_queues_closure_before_selection(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_smart.mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    paper = {
        "paper_id": "paper-retarget-active",
        "status": "monitoring",
        "workflow_status": "monitoring",
        "target_journal": "Old Journal",
        "submission_system_id": "springer_nature",
        "route_revision": 2,
    }
    smart.upsert_submission_record(
        paper,
        "OLD-123",
        target_journal="Old Journal",
        submission_system_id="springer_nature",
        status="under_review",
        active=True,
        canonical=True,
    )
    smart.project_submission_identity(paper)
    state["papers"] = {paper["paper_id"]: paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    route = {"execution_owner": "smart", "mode": "canary", "revision": 2}
    monkeypatch.setattr(smart, "ensure_smart_submission_module", lambda: {})
    monkeypatch.setattr(
        smart,
        "_ensure_user_retarget_route",
        lambda *_args, **_kwargs: {"changed": False, "submission_route": route},
    )

    def append_guarded(queue, paper_id, _paper, **_kwargs):
        if paper_id not in queue:
            queue.append(paper_id)
        return route

    monkeypatch.setattr(smart, "_append_online_queue_guarded", append_guarded)
    monkeypatch.setattr(
        smart,
        "_queue_journal_author_retarget",
        lambda *_args, **_kwargs: pytest.fail("journal selection must wait for closure"),
    )

    result = smart.request_smart_submission_retarget(
        paper["paper_id"],
        mode="ai",
        publication_tier="sci",
        priority="acceptance",
        withdrawal_policy="auto",
        activate=False,
    )

    saved = smart.read_json(smart.SMART_STATE_PATH, {})
    saved_paper = saved["papers"][paper["paper_id"]]
    assert result["recorded"]["withdrawal"]["decision"] == "verify_or_withdraw"
    assert saved_paper["retarget_request"]["status"] == "identity_check_required"
    assert saved_paper["retarget_request"]["withdrawal_targets"] == ["OLD-123"]
    assert saved_paper["online_intent"] == "user_retarget_verify_or_withdraw"
    assert paper["paper_id"] in saved["online_queue"]


def test_user_specific_retarget_queues_journal_author_after_closed_attempt(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_smart.mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    paper = {
        "paper_id": "paper-retarget-closed",
        "status": "rejected",
        "workflow_status": "retarget_needed",
        "target_journal": "Old Journal",
        "submission_system_id": "springer_nature",
        "route_revision": 3,
    }
    smart.upsert_submission_record(
        paper,
        "OLD-456",
        target_journal="Old Journal",
        submission_system_id="springer_nature",
        status="rejected",
        active=False,
        canonical=False,
    )
    smart.project_submission_identity(paper)
    state["papers"] = {paper["paper_id"]: paper}
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    route = {"execution_owner": "smart", "mode": "canary", "revision": 3}
    monkeypatch.setattr(smart, "ensure_smart_submission_module", lambda: {})
    monkeypatch.setattr(
        smart,
        "_ensure_user_retarget_route",
        lambda *_args, **_kwargs: {"changed": False, "submission_route": route},
    )
    observed = {}

    def queue_retarget(paper_id, queued_paper, reason, _settings):
        observed.update(
            {
                "paper_id": paper_id,
                "reason": reason,
                "request": dict(queued_paper["retarget_request"]),
            }
        )
        return {
            "request_id": "journal-author-request-1",
            "previous_target_journal": "Old Journal",
            "archive": {},
            "audit_path": str(isolated_smart / "retarget.md"),
        }

    monkeypatch.setattr(smart, "_queue_journal_author_retarget", queue_retarget)

    result = smart.request_smart_submission_retarget(
        paper["paper_id"],
        mode="specific",
        target_journal="Exact New Journal",
        withdrawal_policy="auto",
        activate=False,
    )

    saved = smart.read_json(smart.SMART_STATE_PATH, {})
    saved_paper = saved["papers"][paper["paper_id"]]
    assert result["ok"] is True
    assert observed["request"]["target_journal"] == "Exact New Journal"
    assert saved_paper["retarget_request"]["status"] == "journal_author_queued"
    assert saved_paper["monitor_retarget_request_id"] == "journal-author-request-1"
    assert saved_paper["workflow_status"] == "waiting_journal_author_retarget"


def test_retarget_closure_worker_must_close_every_exact_active_id(
    isolated_smart: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_smart.mkdir(parents=True, exist_ok=True)
    smart.SMART_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    paper_id = "paper-retarget-closure"
    route = {"execution_owner": "smart", "mode": "canary", "revision": 4}
    paper = {
        "paper_id": paper_id,
        "status": "revision_needed",
        "workflow_status": "running",
        "worker_status": "running",
        "target_journal": "Old Journal",
        "submission_system_id": "springer_nature",
        "route_revision": 4,
        "attempt_id": "attempt-closure",
        "thread_id": "thread-closure",
        "online_intent": "user_retarget_verify_or_withdraw",
        "retarget_request": {
            "schema": "smart-submission-retarget-request/v1",
            "request_id": "retarget-closure-1",
            "paper_id": paper_id,
            "status": "identity_check_required",
            "mode": "ai",
            "publication_tier": "sci",
            "priority": "acceptance",
            "withdrawal_policy": "auto",
            "withdrawal_targets": ["OLD-1", "OLD-2"],
        },
    }
    for submission_id in ("OLD-1", "OLD-2"):
        smart.upsert_submission_record(
            paper,
            submission_id,
            target_journal="Old Journal",
            submission_system_id="springer_nature",
            status="active",
            active=True,
            canonical=submission_id == "OLD-1",
        )
    smart.project_submission_identity(paper)
    state["papers"] = {paper_id: paper}
    state["online_queue"] = [paper_id]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(smart, "require_smart_canary_route", lambda *_args, **_kwargs: route)
    monkeypatch.setattr(
        smart,
        "refresh_smart_submission_projection",
        lambda **_kwargs: {"ok": True},
    )

    result = smart.record_smart_retarget_closure_result(
        paper_id,
        route_revision=4,
        attempt_id="attempt-closure",
        status="retarget_needed",
        next_action="queue_user_selected_journal_author_retarget",
        thread_id="thread-closure",
        closed_submission_ids="OLD-1,OLD-2",
        activate=False,
    )

    saved = smart.read_json(smart.SMART_STATE_PATH, {})
    saved_paper = saved["papers"][paper_id]
    assert result["status"] == "retarget_needed"
    assert saved_paper["active_submission_ids"] == []
    assert saved_paper["retarget_request"]["status"] == "ready_for_selection"
    assert saved_paper["retarget_request"]["closed_submission_ids"] == ["OLD-1", "OLD-2"]
    assert paper_id not in saved["online_queue"]
