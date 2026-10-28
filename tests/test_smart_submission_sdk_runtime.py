from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from unified_paper_orchestrator import smart_submission
from unified_paper_orchestrator import smart_submission_sdk_runtime as sdk_runtime_module
from unified_paper_orchestrator.smart_submission_sdk_runtime import (
    SmartSubmissionSDKRuntime,
    SmartSubmissionSDKRuntimeError,
)


class ReadyAccount:
    def verified_status(self):
        return {
            "ok": True,
            "authenticated": True,
            "isolated_home": True,
        }


class NotAuthenticatedAccount:
    def verified_status(self):
        return {
            "ok": True,
            "authenticated": False,
            "isolated_home": True,
        }


def _runtime(
    tmp_path: Path,
    *,
    account=None,
    credential_broker=None,
) -> SmartSubmissionSDKRuntime:
    return SmartSubmissionSDKRuntime(
        root=tmp_path,
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.json",
        dispatch_lock_path=tmp_path / "dispatch.lock",
        context_store_path=tmp_path / "contexts.json",
        codex_home=tmp_path / "codex-home",
        account_service=account or ReadyAccount(),
        browser_health_probe=lambda **kwargs: {"operational": True},
        credential_broker=credential_broker,
        model="gpt-test",
    )


def test_account_gate_is_local_and_fail_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, account=NotAuthenticatedAccount())

    with pytest.raises(SmartSubmissionSDKRuntimeError) as exc_info:
        runtime.ensure_ready()

    assert exc_info.value.code == "CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED"


def test_sdk_prompt_uses_file_inventory_tool_instead_of_dumping_paths(
    tmp_path: Path,
) -> None:
    prompt = SmartSubmissionSDKRuntime._sdk_prompt(
        "BASE PROMPT",
        {
            "context_id": "sbx_" + "a" * 48,
            "allowed_files": [str(tmp_path / "secret-long-path.pdf")],
        },
    )

    assert "browser_files" in prompt
    assert "browser_upload" in prompt
    assert str(tmp_path / "secret-long-path.pdf") not in prompt
    assert "BASE PROMPT" in prompt


def test_manual_takeover_reconciliation_preserves_user_completed_fields() -> None:
    prompt = SmartSubmissionSDKRuntime._sdk_prompt(
        "BASE PROMPT",
        {"context_id": "sbx_" + "b" * 48},
        preserve_manual_fields=True,
    )

    assert "MANUAL TAKEOVER RECONCILIATION" in prompt
    assert "Preserve every" in prompt
    assert "already completed field" in prompt
    assert "do not restart the form" in prompt


def test_sdk_result_parser_accepts_rejected_monitor_result() -> None:
    response = "\n".join(
        (
            "SMART_SUBMISSION_PAPER_ID=paper-1",
            "SMART_SUBMISSION_ROUTE_REVISION=3",
            "SMART_SUBMISSION_ATTEMPT_ID=attempt-1",
            "SMART_SUBMISSION_STATUS=rejected",
            "SMART_SUBMISSION_NEXT=return to journal selection",
        )
    )

    markers = SmartSubmissionSDKRuntime._parse_markers(
        response,
        paper_id="paper-1",
        route_revision=3,
        attempt_id="attempt-1",
    )

    assert markers["STATUS"] == "rejected"


def test_mailbox_batch_parser_requires_every_exact_attempt() -> None:
    expected = {
        "paper-a": {"route_revision": 1, "attempt_id": "attempt-a"},
        "paper-b": {"route_revision": 2, "attempt_id": "attempt-b"},
    }
    response = "\n".join(
        (
            "SMART_MONITOR_BATCH_BEGIN",
            json.dumps(
                {
                    "schema": smart_submission.SMART_MONITOR_BATCH_SCHEMA,
                    "run_id": "mail-run",
                    "results": [
                        {
                            "paper_id": "paper-a",
                            "route_revision": 1,
                            "attempt_id": "attempt-a",
                            "status": "monitoring",
                        },
                        {
                            "paper_id": "paper-b",
                            "route_revision": 2,
                            "attempt_id": "attempt-b",
                            "status": "revision_needed",
                        },
                    ],
                },
                separators=(",", ":"),
            ),
            "SMART_MONITOR_BATCH_END",
        )
    )

    parsed = SmartSubmissionSDKRuntime._parse_mailbox_monitor_batch(
        response,
        run_id="mail-run",
        expected=expected,
    )

    assert parsed["paper-a"]["status"] == "monitoring"
    assert parsed["paper-b"]["status"] == "revision_needed"


def test_mailbox_batch_parser_requires_every_configured_source() -> None:
    expected = {
        "paper-a": {
            "route_revision": 1,
            "attempt_id": "attempt-a",
            "source_ids": ["gmail", "xujc-mail-1"],
        }
    }
    response = "\n".join(
        (
            "SMART_MONITOR_BATCH_BEGIN",
            json.dumps(
                {
                    "schema": smart_submission.SMART_MONITOR_BATCH_SCHEMA,
                    "run_id": "mail-run",
                    "results": [
                        {
                            "paper_id": "paper-a",
                            "route_revision": 1,
                            "attempt_id": "attempt-a",
                            "status": "monitoring",
                            "source_results": [
                                {"source_id": "gmail", "status": "monitoring"}
                            ],
                        }
                    ],
                },
                separators=(",", ":"),
            ),
            "SMART_MONITOR_BATCH_END",
        )
    )

    with pytest.raises(SmartSubmissionSDKRuntimeError) as exc_info:
        SmartSubmissionSDKRuntime._parse_mailbox_monitor_batch(
            response,
            run_id="mail-run",
            expected=expected,
        )

    assert exc_info.value.code == "SMART_MONITOR_BATCH_SOURCES_INCOMPLETE"


def test_mailbox_monitor_builds_one_bounded_or_query_for_many_papers() -> None:
    batches = smart_submission._gmail_monitor_query_batches(
        [
            {
                "paper_id": "paper-a",
                "submission_id": "MS-123",
                "submission_ids": ["MS-123", "MS-122-OLD"],
                "title": "First Exact Paper Title",
            },
            {
                "paper_id": "paper-b",
                "submission_id": "",
                "title": "Second Exact Paper Title",
            },
        ]
    )

    assert len(batches) == 1
    assert batches[0]["paper_ids"] == ["paper-a", "paper-b"]
    assert batches[0]["query"].startswith("newer_than:365d {")
    assert '"MS-123"' in batches[0]["query"]
    assert '"MS-122-OLD"' in batches[0]["query"]
    assert '"First Exact Paper Title"' in batches[0]["query"]
    assert '"Second Exact Paper Title"' in batches[0]["query"]


def test_mailbox_batch_uses_one_ephemeral_turn_for_all_pending_papers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict] = []
    observations: list[str] = []
    runtime = SmartSubmissionSDKRuntime(
        root=tmp_path,
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.json",
        dispatch_lock_path=tmp_path / "dispatch.lock",
        context_store_path=tmp_path / "contexts.json",
        codex_home=tmp_path / "codex-home",
        account_service=ReadyAccount(),
        browser_health_probe=lambda **kwargs: {"operational": True},
        mailbox_pages_probe=lambda: [
            {
                "page_id": "gmail-page",
                "origin": smart_submission.SMART_MONITOR_MAILBOX_ORIGIN,
            }
        ],
        model="gpt-test",
    )
    runtime.state_path.write_text(
        json.dumps(
            {
                "online_queue": ["paper-a", "paper-b"],
                "papers": {
                    "paper-a": {
                        "paper_id": "paper-a",
                        "status": "monitoring",
                        "workflow_status": "queued",
                        "monitor_check_pending": True,
                        "route_revision": 1,
                        "attempt_id": "attempt-a",
                    },
                    "paper-b": {
                        "paper_id": "paper-b",
                        "status": "submitted",
                        "workflow_status": "queued",
                        "monitor_check_pending": True,
                        "route_revision": 2,
                        "attempt_id": "attempt-b",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def mutate_paper(paper_id: str, callback) -> dict:
        state = json.loads(runtime.state_path.read_text(encoding="utf-8"))
        paper = state["papers"][paper_id]
        callback(paper, state)
        runtime.state_path.write_text(json.dumps(state), encoding="utf-8")
        return dict(paper)

    def fake_dispatch(
        paper_id,
        *,
        thread_id,
        route_revision,
        attempt_id,
    ):
        return mutate_paper(
            paper_id,
            lambda paper, _state: paper.update(
                {"workflow_status": "running", "thread_id": thread_id}
            ),
        )

    def fake_record_result(paper_id, *, status, **_kwargs):
        def update(paper, state):
            paper.pop("monitor_check_pending", None)
            paper.update(
                {
                    "status": status,
                    "workflow_status": "monitoring" if status == "monitoring" else status,
                    "last_monitor_checked_at": "2026-07-18T03:00:00+08:00",
                    "last_monitor_result": status,
                    "last_monitor_check_outcome": status,
                }
            )
            state["online_queue"] = [
                value for value in state.get("online_queue", []) if value != paper_id
            ]

        paper = mutate_paper(paper_id, update)
        return {"paper_id": paper_id, "status": paper["status"]}

    monkeypatch.setattr(
        sdk_runtime_module,
        "record_mailbox_monitor_worker_dispatch",
        fake_dispatch,
    )
    monkeypatch.setattr(sdk_runtime_module, "record_visible_worker_result", fake_record_result)
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_sdk_turn_state",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_monitor_observation",
        lambda paper_id, **_kwargs: observations.append(paper_id) or {},
    )
    monkeypatch.setattr(
        sdk_runtime_module,
        "build_sdk_mailbox_monitor_prompt",
        lambda paper_ids, *, run_id, mailbox_context=None: (
            "SCHEDULER_MONITOR_BATCH_JSON:\n"
            + json.dumps(
                {
                    "run_id": run_id,
                    "papers": [
                        {
                            "paper_id": paper_id,
                            **json.loads(runtime.state_path.read_text(encoding="utf-8"))[
                                "papers"
                            ][paper_id],
                        }
                        for paper_id in paper_ids
                    ],
                },
                separators=(",", ":"),
            )
        ),
    )

    class FakeHandle:
        id = "mailbox-turn"

        def __init__(self, response: str) -> None:
            self.response = response

        async def run(self):
            return type(
                "Turn",
                (),
                {"final_response": self.response, "usage": {"input_tokens": 100}},
            )()

    class FakeThread:
        id = "mailbox-thread"

        async def turn(self, prompt, **_kwargs):
            match = re.search(r"SCHEDULER_MONITOR_BATCH_JSON:\n(\{.*\})", prompt)
            assert match is not None
            payload = json.loads(match.group(1))
            results = []
            for item in payload["papers"]:
                results.append(
                    {
                        "paper_id": item["paper_id"],
                        "route_revision": item["route_revision"],
                        "attempt_id": item["attempt_id"],
                        "status": "monitoring",
                        "action_kind": "none",
                        "summary": "No actionable notification.",
                        "message_date": "",
                        "sender_domain": "",
                        "submission_id": "",
                        "source_results": [
                            {
                                "source_id": "gmail",
                                "source_label": "Gmail",
                                "source_kind": "gmail_browser",
                                "available": True,
                                "status": "monitoring",
                                "action_kind": "none",
                                "summary": "No actionable notification.",
                                "message_date": "",
                                "sender_domain": "",
                                "submission_id": "",
                                "error_code": "",
                            }
                        ],
                    }
                )
            response = "\n".join(
                (
                    "SMART_MONITOR_BATCH_BEGIN",
                    json.dumps(
                        {
                            "schema": smart_submission.SMART_MONITOR_BATCH_SCHEMA,
                            "run_id": payload["run_id"],
                            "results": results,
                        },
                        separators=(",", ":"),
                    ),
                    "SMART_MONITOR_BATCH_END",
                )
            )
            return FakeHandle(response)

    class FakeCodex:
        async def thread_start(self, **kwargs):
            starts.append(dict(kwargs))
            return FakeThread()

    result = asyncio.run(
        runtime._run_mailbox_monitor_batch(FakeCodex(), ["paper-a", "paper-b"])
    )

    assert result["ok"] is True
    assert result["paper_ids"] == ["paper-a", "paper-b"]
    assert len(starts) == 1
    assert starts[0]["ephemeral"] is True
    assert observations == ["paper-a", "paper-b"]


def test_dashboard_checkpoint_does_not_repeat_mailbox_batch(tmp_path: Path) -> None:
    runtime = SmartSubmissionSDKRuntime(
        root=tmp_path,
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.json",
        dispatch_lock_path=tmp_path / "dispatch.lock",
        context_store_path=tmp_path / "contexts.json",
        codex_home=tmp_path / "codex-home",
        account_service=ReadyAccount(),
        browser_health_probe=lambda **kwargs: {"operational": True},
        model="gpt-test",
    )
    runtime.state_path.write_text(
        json.dumps(
            {
                "online_queue": ["mailbox", "dashboard"],
                "papers": {
                    "mailbox": {
                        "status": "monitoring",
                        "monitor_check_pending": True,
                        "monitor_pending_channel": "mailbox_batch",
                    },
                    "dashboard": {
                        "status": "monitoring",
                        "monitor_check_pending": True,
                        "monitor_pending_channel": "journal_dashboard",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert runtime._pending_monitor_ids(["mailbox", "dashboard"]) == ["mailbox"]


def test_monitor_check_uses_fresh_ephemeral_sdk_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state_path.write_text(
        json.dumps(
            {
                "papers": {
                    "paper-monitor": {
                        "paper_id": "paper-monitor",
                        "status": "monitoring",
                        "workflow_status": "queued",
                        "monitor_check_pending": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    starts: list[dict] = []

    class FakeThread:
        id = "monitor-thread-1"

    class FakeCodex:
        async def thread_start(self, **kwargs):
            starts.append(dict(kwargs))
            return FakeThread()

    recorded: list[dict] = []
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_sdk_turn_state",
        lambda paper_id, **kwargs: recorded.append(
            {"paper_id": paper_id, **kwargs}
        )
        or {},
    )
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_sdk_thread_binding",
        lambda *_args, **_kwargs: pytest.fail(
            "monitoring must not replace the fixed submission thread"
        ),
    )

    thread = asyncio.run(
        runtime._thread_for_paper(
            FakeCodex(),
            "paper-monitor",
            monitor_only=True,
        )
    )

    assert thread.id == "monitor-thread-1"
    assert starts[0]["ephemeral"] is True
    assert recorded[0]["runtime_status"] == "creating_thread"
    assert recorded[0]["monitor_only"] is True
    assert recorded[1]["thread_id"] == "monitor-thread-1"
    assert recorded[1]["runtime_status"] == "queued"


def test_stale_submission_thread_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state_path.write_text(
        json.dumps(
            {
                "papers": {
                    "paper-stale": {
                        "paper_id": "paper-stale",
                        "sdk_thread_id": "old-thread",
                        "sdk_thread_name": "upo-smart-submission-stale",
                        "stale_worker_thread_ids": ["old-thread"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    resumed: list[str] = []
    starts: list[dict] = []

    class Thread:
        id = "new-thread"

        async def set_name(self, _name: str) -> None:
            return None

    class OldThread:
        id = "old-thread"
        name = "upo-smart-submission-stale"

    class ThreadList:
        data = [OldThread()]

    class FakeCodex:
        async def thread_list(self, **_kwargs):
            return ThreadList()

        async def thread_resume(self, thread_id: str, **_kwargs):
            resumed.append(thread_id)
            return OldThread()

        async def thread_start(self, **kwargs):
            starts.append(dict(kwargs))
            return Thread()

    bindings: list[dict] = []
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_sdk_turn_state",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_sdk_thread_binding",
        lambda paper_id, **kwargs: bindings.append(
            {"paper_id": paper_id, **kwargs}
        )
        or {},
    )

    thread = asyncio.run(
        runtime._thread_for_paper(FakeCodex(), "paper-stale")
    )

    assert thread.id == "new-thread"
    assert resumed == []
    assert len(starts) == 1
    assert starts[0]["ephemeral"] is False
    assert bindings == [
        {
            "paper_id": "paper-stale",
            "thread_id": "new-thread",
            "thread_name": "upo-smart-submission-stale",
            "replace": True,
        }
    ]


def test_submission_ready_papers_are_projected_as_read_only_candidates(
    tmp_path: Path,
) -> None:
    ready_package = tmp_path / "ready-package"
    ready_package.mkdir()
    active_package = tmp_path / "active-package"
    active_package.mkdir()
    unified = {
        "papers": {
            "paper-ready": {
                "title": "Ready paper",
                "stages": {
                    "journal_author": {
                        "status": "complete",
                        "ready_to_submit": True,
                        "artifact": str(ready_package),
                    },
                    "submission": {
                        "status": "ready",
                        "admission": {"admitted": True},
                    },
                },
            },
            "paper-active": {
                "stages": {
                    "journal_author": {
                        "status": "complete",
                        "artifact": str(active_package),
                    },
                    "submission": {"status": "monitoring"},
                },
            },
        },
        "submission_routes": {
            "paper-ready": {
                "execution_owner": "legacy_auto",
                "mode": "production",
                "revision": 3,
            }
        },
    }

    result = smart_submission.discover_smart_submission_candidates(
        unified,
        {"papers": {}},
    )

    assert result["read_only"] is True
    assert result["candidate_ids"] == ["paper-ready"]
    candidate = result["papers"]["paper-ready"]
    assert candidate["candidate_only"] is True
    assert candidate["status"] == "candidate"
    assert candidate["execution_owner"] == "legacy_auto"
    assert candidate["route_revision"] == 3
    assert result["skipped"]["paper-active"] == "submission is already active or terminal"


def test_explicit_candidate_enrollment_switches_route_without_online_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    class FakeOrchestrator:
        def __init__(self) -> None:
            self.route = {
                "execution_owner": "legacy_auto",
                "mode": "production",
                "revision": 1,
            }
            self.route_calls: list[dict] = []

        def sync(self):
            return {
                "papers": {
                    "paper-ready": {
                        "stages": {
                            "journal_author": {
                                "status": "complete",
                                "artifact": str(package_dir),
                            },
                            "submission": {"status": "ready"},
                        }
                    }
                },
                "submission_routes": {"paper-ready": dict(self.route)},
            }

        def set_submission_route(self, paper_id, **kwargs):
            self.route_calls.append({"paper_id": paper_id, **kwargs})
            self.route = {
                "execution_owner": kwargs["execution_owner"],
                "mode": kwargs["mode"],
                "revision": kwargs["expected_revision"] + 1,
            }
            return {
                "changed": True,
                "paper_id": paper_id,
                "submission_route": dict(self.route),
                "operation_id": kwargs["operation_id"],
            }

    imported: list[str] = []

    def fake_import(_unified, selected_ids=None):
        imported.extend(selected_ids or [])
        return {
            "imported": list(selected_ids or []),
            "skipped": {},
            "state": {},
        }

    monkeypatch.setattr(
        smart_submission,
        "import_from_unified_state",
        fake_import,
    )
    orchestrator = FakeOrchestrator()

    result = smart_submission.enable_smart_submission_candidates(
        ["paper-ready"],
        orchestrator=orchestrator,
    )

    assert result["ok"] is True
    assert result["enabled"] == ["paper-ready"]
    assert result["online_started"] is False
    assert imported == ["paper-ready"]
    assert orchestrator.route["execution_owner"] == "smart"
    assert orchestrator.route["mode"] == "canary"
    assert len(orchestrator.route_calls) == 1


def test_result_markers_are_bound_to_exact_paper_route_and_attempt(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    response = "\n".join(
        [
            "SMART_SUBMISSION_PAPER_ID=paper-1",
            "SMART_SUBMISSION_ROUTE_REVISION=4",
            "SMART_SUBMISSION_ATTEMPT_ID=attempt-1",
            "SMART_SUBMISSION_STATUS=waiting",
            "SMART_SUBMISSION_NEXT=await explicit final approval",
        ]
    )

    markers = runtime._parse_markers(
        response,
        paper_id="paper-1",
        route_revision=4,
        attempt_id="attempt-1",
    )

    assert markers["STATUS"] == "waiting"
    with pytest.raises(SmartSubmissionSDKRuntimeError) as exc_info:
        runtime._parse_markers(
            response,
            paper_id="paper-2",
            route_revision=4,
            attempt_id="attempt-1",
        )
    assert exc_info.value.code == "SMART_SDK_RESULT_PAPER_MISMATCH"


def test_final_confirmation_marker_maps_to_review_ready_action() -> None:
    assert (
        smart_submission._value_free_worker_action(
            "waiting",
            "SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED",
        )
        == smart_submission.FINAL_SUBMIT_REVIEW_READY_ACTION
    )


def test_exact_sdk_final_review_marker_can_repair_generic_waiting_state() -> None:
    paper = {
        "status": "waiting",
        "workflow_status": "waiting",
        "route_revision": 2,
        "attempt_id": "attempt-2",
        "thread_id": "thread-2",
    }
    sdk_record = {
        "status": "completed",
        "recorded_status": "waiting",
        "route_revision": 2,
        "attempt_id": "attempt-2",
        "thread_id": "thread-2",
        "final_response": "\n".join(
            [
                "SMART_SUBMISSION_PAPER_ID=paper-2",
                "SMART_SUBMISSION_ROUTE_REVISION=2",
                "SMART_SUBMISSION_ATTEMPT_ID=attempt-2",
                "SMART_SUBMISSION_STATUS=waiting",
                "SMART_SUBMISSION_NEXT=SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED",
            ]
        ),
    }

    assert smart_submission._sdk_final_review_marker_matches(
        "paper-2", paper, sdk_record
    ) is True
    sdk_record["attempt_id"] = "stale-attempt"
    assert smart_submission._sdk_final_review_marker_matches(
        "paper-2", paper, sdk_record
    ) is False


def test_background_additions_are_drained_without_losing_a_paper(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[list[str] | None] = []

    def fake_run_once(selected_ids=None):
        selected = list(selected_ids) if selected_ids is not None else None
        calls.append(selected)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=5)
        return {"ok": True, "selected": selected}

    runtime.run_once = fake_run_once

    first = runtime.start_background(["paper-a"])
    assert first["started"] is True
    assert first_started.wait(timeout=5)
    second = runtime.start_background(["paper-b"])
    assert second["already_running"] is True
    release_first.set()
    thread = runtime._background_thread
    assert thread is not None
    thread.join(timeout=5)

    assert calls == [["paper-a"], ["paper-b"]]
    assert runtime.status()["running"] is False


def test_review_ready_paper_is_automatically_authorized_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_calls: list[dict] = []

    class ReadyBroker:
        def ensure_session(self, **kwargs):
            broker_calls.append(kwargs)
            return {
                "ok": True,
                "status": "session_ready",
                "code": "SMART_CREDENTIAL_BROKER_SESSION_READY",
                "account_id": "account-auto",
                "system_id": "springer_nature",
                "origin": "https://submission.springernature.com:443",
                "next_action": "dispatch_fixed_sdk_session",
                "value_free": True,
            }

    runtime = _runtime(tmp_path, credential_broker=ReadyBroker())
    runtime.state_path.write_text(
        json.dumps(
            {
                "settings": {"auto_final_submit_enabled": True},
                "papers": {
                    "paper-auto": {
                        "status": "waiting",
                        "workflow_status": "waiting",
                        "next_action": smart_submission.FINAL_SUBMIT_REVIEW_READY_ACTION,
                        "route_revision": 7,
                        "attempt_id": "attempt-auto",
                        "credential_account_id": "account-auto",
                        "submission_system_id": "springer_nature",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []

    def fake_authorize(paper_id, **kwargs):
        calls.append({"paper_id": paper_id, **kwargs})
        return {
            "authorization_id": "a" * 32,
            "authorization_mode": smart_submission.FINAL_SUBMIT_AUTO_PRESENTATION,
            "expires_at": "2026-07-17T12:00:00+00:00",
        }

    monkeypatch.setattr(
        sdk_runtime_module,
        "authorize_final_submit_automatically",
        fake_authorize,
    )
    recorded_broker_states: list[dict] = []

    def fake_record_broker_state(paper_id, result):
        recorded_broker_states.append({"paper_id": paper_id, "result": dict(result)})
        return json.loads(runtime.state_path.read_text(encoding="utf-8"))["papers"][paper_id]

    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_credential_broker_state",
        fake_record_broker_state,
    )
    attempted: set[tuple[str, str]] = set()
    planned = {
        "paper-auto": {
            "credential_account_id": "account-auto",
            "submission_system_id": "springer_nature",
            "target_origin": "https://submission.springernature.com:443",
            "allowed_origins": ["https://submission.springernature.com:443"],
        }
    }

    first = runtime._auto_authorize_ready_final_submits(
        ["paper-auto"], attempted, planned
    )
    second = runtime._auto_authorize_ready_final_submits(
        ["paper-auto"], attempted, planned
    )

    assert len(first) == 1
    assert first[0]["ok"] is True
    assert second == []
    assert len(broker_calls) == 1
    assert broker_calls[0]["candidate"]["target_origin"] == (
        "https://submission.springernature.com:443"
    )
    assert recorded_broker_states[0]["result"]["status"] == "session_ready"
    assert calls == [
        {
            "paper_id": "paper-auto",
            "route_revision": 7,
            "attempt_id": "attempt-auto",
            "activate": False,
        }
    ]


def test_final_broker_refresh_reissues_only_released_unused_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state_path.write_text(
        json.dumps(
            {
                "papers": {
                    "paper-auto": {
                        "paper_id": "paper-auto",
                        "next_action": (
                            smart_submission.FINAL_SUBMIT_AUTHORIZED_ACTION
                        ),
                        "final_submit_authorization": {
                            "authorization_id": "old-authorization"
                        },
                        "marker": "before",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    def fake_release(paper_id, **kwargs):
        calls.append(("release", paper_id))
        assert kwargs == {
            "route_revision": 7,
            "attempt_id": "attempt-auto",
        }
        return {
            "released": True,
            "session_binding_rebound": True,
        }

    def fake_authorize(paper_id, **kwargs):
        calls.append(("authorize", paper_id))
        assert kwargs == {
            "route_revision": 7,
            "attempt_id": "attempt-auto",
            "activate": False,
        }
        state = json.loads(runtime.state_path.read_text(encoding="utf-8"))
        state["papers"][paper_id]["marker"] = "reauthorized"
        runtime.state_path.write_text(json.dumps(state), encoding="utf-8")
        return {
            "authorization_id": "new-authorization",
            "authorization_mode": (
                smart_submission.FINAL_SUBMIT_AUTO_PRESENTATION
            ),
        }

    monkeypatch.setattr(
        sdk_runtime_module,
        "release_expired_unclaimed_final_submit_authorization",
        fake_release,
    )
    monkeypatch.setattr(
        sdk_runtime_module,
        "authorize_final_submit_automatically",
        fake_authorize,
    )

    paper = runtime._smart_paper("paper-auto")
    result = runtime._reissue_final_authorization_after_broker_refresh(
        "paper-auto",
        paper,
        route_revision=7,
        attempt_id="attempt-auto",
    )

    assert calls == [
        ("release", "paper-auto"),
        ("authorize", "paper-auto"),
    ]
    assert result["marker"] == "reauthorized"
    journal = json.loads(runtime.journal_path.read_text(encoding="utf-8"))
    assert journal["papers"]["paper-auto"]["status"] == (
        "auto_final_submit_reauthorized_after_session_refresh"
    )


def test_review_ready_auto_authorization_defers_until_session_refresh_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WaitingBroker:
        def ensure_session(self, **_kwargs):
            return {
                "ok": False,
                "status": "human_verification",
                "code": "SMART_CREDENTIAL_BROKER_WAITING_HUMAN",
                "account_id": "account-auto",
                "system_id": "springer_nature",
                "origin": "https://submission.springernature.com:443",
                "next_action": "complete_captcha_or_2fa_then_resume",
                "value_free": True,
            }

    runtime = _runtime(tmp_path, credential_broker=WaitingBroker())
    runtime.state_path.write_text(
        json.dumps(
            {
                "settings": {"auto_final_submit_enabled": True},
                "papers": {
                    "paper-auto": {
                        "status": "waiting",
                        "workflow_status": "waiting",
                        "next_action": smart_submission.FINAL_SUBMIT_REVIEW_READY_ACTION,
                        "route_revision": 7,
                        "attempt_id": "attempt-auto",
                        "credential_account_id": "account-auto",
                        "submission_system_id": "springer_nature",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sdk_runtime_module,
        "record_smart_credential_broker_state",
        lambda paper_id, _result: json.loads(
            runtime.state_path.read_text(encoding="utf-8")
        )["papers"][paper_id],
    )
    authorize_calls: list[str] = []
    monkeypatch.setattr(
        sdk_runtime_module,
        "authorize_final_submit_automatically",
        lambda paper_id, **_kwargs: authorize_calls.append(paper_id),
    )

    result = runtime._auto_authorize_ready_final_submits(
        ["paper-auto"],
        set(),
        {
            "paper-auto": {
                "credential_account_id": "account-auto",
                "submission_system_id": "springer_nature",
                "target_origin": "https://submission.springernature.com:443",
            }
        },
    )

    assert result == [
        {
            "ok": False,
            "deferred": True,
            "paper_id": "paper-auto",
            "attempt_id": "attempt-auto",
            "route_revision": 7,
            "code": "SMART_CREDENTIAL_BROKER_WAITING_HUMAN",
            "message": "complete_captcha_or_2fa_then_resume",
        }
    ]
    assert authorize_calls == []
    journal = json.loads(runtime.journal_path.read_text(encoding="utf-8"))
    assert journal["papers"]["paper-auto"]["status"] == (
        "auto_final_submit_session_deferred"
    )


def test_multi_paper_preparation_uses_program_queue_without_legacy_wakeup(
    monkeypatch,
) -> None:
    class FakeOrchestrator:
        def __init__(self) -> None:
            self.routes = {
                "paper-a": {
                    "execution_owner": "legacy_auto",
                    "mode": "production",
                    "revision": 2,
                },
                "paper-b": {
                    "execution_owner": "smart",
                    "mode": "canary",
                    "revision": 4,
                },
            }
            self.changed: list[str] = []

        def sync(self):
            return {
                "papers": {"paper-a": {}, "paper-b": {}},
                "submission_routes": dict(self.routes),
            }

        def set_submission_route(
            self,
            paper_id,
            *,
            execution_owner,
            mode,
            expected_revision,
            operation_id,
        ):
            self.changed.append(paper_id)
            route = {
                "execution_owner": execution_owner,
                "mode": mode,
                "revision": expected_revision + 1,
            }
            self.routes[paper_id] = route
            return {
                "changed": True,
                "paper_id": paper_id,
                "submission_route": route,
                "operation_id": operation_id,
            }

    local_calls: list[list[str]] = []
    queue_calls: list[tuple[list[str], bool, bool]] = []

    def fake_local(**kwargs):
        local_calls.append(list(kwargs["selected_ids"]))
        return {"preflight_results": [], "state": {}}

    def fake_queue(selected_ids, *, activate, sdk_browser):
        queue_calls.append((list(selected_ids), activate, sdk_browser))
        return {
            "queued": list(selected_ids),
            "skipped": {},
            "state": {},
        }

    monkeypatch.setattr(
        smart_submission,
        "smart_submission_batch_check",
        fake_local,
    )
    monkeypatch.setattr(
        smart_submission,
        "queue_visible_online_workers",
        fake_queue,
    )
    orchestrator = FakeOrchestrator()

    result = smart_submission.prepare_smart_submission_sdk_papers(
        ["paper-a", "paper-b"],
        orchestrator=orchestrator,
    )

    assert result["ok"] is True
    assert result["queued"] == ["paper-a", "paper-b"]
    assert orchestrator.changed == ["paper-a"]
    assert local_calls == [["paper-a", "paper-b"]]
    assert queue_calls == [(["paper-a", "paper-b"], True, True)]


def test_one_sdk_sweep_refills_parallel_slots_until_selected_queue_is_drained(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeCodexContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = SmartSubmissionSDKRuntime(
        root=tmp_path,
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.json",
        dispatch_lock_path=tmp_path / "dispatch.lock",
        context_store_path=tmp_path / "contexts.json",
        codex_home=tmp_path / "codex-home",
        account_service=ReadyAccount(),
        codex_factory=FakeCodexContext,
        browser_health_probe=lambda **kwargs: {"operational": True},
        model="gpt-test",
    )
    plans = [
        {
            "plan_id": "plan-1",
            "dispatch_candidates": [
                {
                    "paper_id": "paper-a",
                    "executor": "codex_sdk_browser",
                }
            ],
        },
        {
            "plan_id": "plan-2",
            "dispatch_candidates": [
                {
                    "paper_id": "paper-b",
                    "executor": "codex_sdk_browser",
                }
            ],
        },
        {"plan_id": "plan-3", "dispatch_candidates": []},
    ]
    plan_scopes: list[list[str] | None] = []

    def fake_plan(*, selected_ids, persist, sync_ledger):
        plan_scopes.append(list(selected_ids) if selected_ids is not None else None)
        return plans.pop(0)

    async def fake_reconcile(codex):
        return []

    async def fake_candidate(codex, candidate):
        return {"ok": True, "paper_id": candidate["paper_id"]}

    monkeypatch.setattr(
        sdk_runtime_module,
        "smart_submission_program_plan",
        fake_plan,
    )
    runtime._reconcile_running = fake_reconcile
    runtime._run_candidate = fake_candidate

    result = asyncio.run(runtime._run_once_async(["paper-a", "paper-b"]))

    assert result["candidate_ids"] == ["paper-a", "paper-b"]
    assert result["dispatched_count"] == 2
    assert result["program_plan_id"] == "plan-3"
    assert plan_scopes == [
        ["paper-a", "paper-b"],
        ["paper-a", "paper-b"],
        ["paper-a", "paper-b"],
    ]


def test_one_sdk_sweep_can_dispatch_review_and_final_click_turns_for_same_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCodexContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = SmartSubmissionSDKRuntime(
        root=tmp_path,
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.json",
        dispatch_lock_path=tmp_path / "dispatch.lock",
        context_store_path=tmp_path / "contexts.json",
        codex_home=tmp_path / "codex-home",
        account_service=ReadyAccount(),
        codex_factory=FakeCodexContext,
        browser_health_probe=lambda **kwargs: {"operational": True},
        model="gpt-test",
    )
    plans = [
        {
            "plan_id": "review-plan",
            "dispatch_candidates": [
                {
                    "paper_id": "paper-auto",
                    "executor": "codex_sdk_browser",
                    "action": "submission.execute_site_step",
                    "cycle": "review-cycle",
                }
            ],
        },
        {
            "plan_id": "submit-plan",
            "dispatch_candidates": [
                {
                    "paper_id": "paper-auto",
                    "executor": "site_adapter",
                    "action": "submission.final_submit",
                    "cycle": "final-cycle",
                }
            ],
        },
        {"plan_id": "done-plan", "dispatch_candidates": []},
    ]

    monkeypatch.setattr(
        sdk_runtime_module,
        "smart_submission_program_plan",
        lambda **kwargs: plans.pop(0),
    )
    runtime._auto_authorize_ready_final_submits = lambda selected, attempted: []
    runtime._reconcile_running = lambda codex: asyncio.sleep(0, result=[])
    runtime._run_candidate = lambda codex, candidate: asyncio.sleep(
        0,
        result={"ok": True, "paper_id": candidate["paper_id"]},
    )

    result = asyncio.run(runtime._run_once_async(["paper-auto"]))

    assert result["candidate_ids"] == ["paper-auto", "paper-auto"]
    assert result["dispatched_count"] == 2
    assert result["program_plan_id"] == "done-plan"
