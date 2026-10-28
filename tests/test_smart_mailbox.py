from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from unified_paper_orchestrator import smart_submission as smart
from unified_paper_orchestrator.smart_mailbox import (
    _trusted_inderscience_credential_reminder,
    _trusted_recovery_url,
    aggregate_monitor_source_results,
    collect_local_mailbox_evidence,
    discover_monitor_mailboxes,
)


def test_inderscience_private_reminders_require_trusted_sender() -> None:
    trusted = EmailMessage()
    trusted["Subject"] = "Inderscience Submissions account reminder"
    trusted["From"] = "Inderscience <noreply@inderscience.com>"
    trusted.set_content(
        "Your username is recovered_author\nYour password is Synthetic-Secret-42"
    )
    untrusted = EmailMessage()
    untrusted["Subject"] = trusted["Subject"]
    untrusted["From"] = "Imposter <attacker@example.test>"
    untrusted.set_content(trusted.get_content())

    assert _trusted_inderscience_credential_reminder(
        trusted,
        reminder_kind="username",
    ) == "recovered_author"
    assert _trusted_inderscience_credential_reminder(
        trusted,
        reminder_kind="password",
    ) == "Synthetic-Secret-42"
    assert _trusted_inderscience_credential_reminder(
        untrusted,
        reminder_kind="password",
    ) == ""


def test_recovery_link_accepts_only_exact_allowlisted_journal_origin() -> None:
    message = EmailMessage()
    message["Subject"] = "ScholarOne password reset"
    message["From"] = "ScholarOne <no-reply@manuscriptcentral.com>"
    message.set_content(
        "Reset your password: "
        "https://mc.manuscriptcentral.com/site/jaise/auth/reset-password?token=opaque"
    )

    value = _trusted_recovery_url(
        message,
        allowed_origins=("https://mc.manuscriptcentral.com:443",),
        journal_path="/jaise",
    )

    assert value.startswith("https://mc.manuscriptcentral.com/")
    assert "token=opaque" in value


def test_recovery_link_rejects_untrusted_sender_even_on_allowed_origin() -> None:
    message = EmailMessage()
    message["Subject"] = "ScholarOne password reset"
    message["From"] = "Imposter <attacker@example.test>"
    message.set_content(
        "https://mc.manuscriptcentral.com/site/jaise/auth/reset-password?token=opaque"
    )

    assert (
        _trusted_recovery_url(
            message,
            allowed_origins=("https://mc.manuscriptcentral.com:443",),
            journal_path="/jaise",
        )
        == ""
    )


class FakeStore:
    def __init__(self) -> None:
        self.revealed: list[str] = []

    def list_account_summaries(self, *, scope: str):
        assert scope == "email"
        return [
            {
                "record_key": "legacy:school-old",
                "source_kind": "legacy",
                "scope": "email",
                "provider": "XUJCMail school mailbox",
                "username": "author@xujc.com",
                "login_url": "http://mail.xujc.com",
                "password_saved": True,
                "auto_fill_ready": True,
            },
            {
                "record_key": "vault:school-current",
                "source_kind": "vault",
                "scope": "email",
                "provider": "XUJCMail school mailbox",
                "username": "author@xujc.com",
                "login_url": "http://mail.xujc.com",
                "password_saved": True,
                "auto_fill_ready": True,
            },
            {
                "record_key": "vault:gmail",
                "source_kind": "vault",
                "scope": "email",
                "provider": "Gmail / Google",
                "username": "author@gmail.com",
                "login_url": "https://mail.google.com",
                "password_saved": True,
                "auto_fill_ready": True,
            },
        ]

    def reveal_password(self, record_key: str) -> str:
        self.revealed.append(record_key)
        return "unit-test-secret"


class FakePop:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.closed = False

    def stat(self):
        return len(self.messages), sum(len(item) for item in self.messages)

    def top(self, number: int, _lines: int):
        raw = self.messages[number - 1]
        return b"+OK", raw.splitlines(), len(raw)

    def quit(self):
        self.closed = True


def _message(subject: str, body: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Journal Office <editor@journal.example>"
    message["Date"] = "Sat, 18 Jul 2026 12:00:00 +0000"
    message.set_content(body)
    return message.as_bytes()


def test_mailbox_discovery_deduplicates_imports_and_prefers_writable_vault() -> None:
    sources = discover_monitor_mailboxes(FakeStore())

    assert [item.kind for item in sources] == ["gmail_browser", "xujc_pop3"]
    school = sources[1]
    assert school.record_key == "vault:school-current"
    assert school.username == "author@xujc.com"
    assert "author" not in school.public().values()
    assert "record_key" not in school.public()


def test_school_mailbox_collection_matches_historical_submission_without_leaking_secret() -> None:
    store = FakeStore()
    sources = discover_monitor_mailboxes(store)
    pop = FakePop(
        [
            _message("Unrelated message", "Nothing to see."),
            _message(
                "Decision for MS-122-OLD",
                "Major revision requested.",
            ),
        ]
    )
    observed_passwords: list[str] = []

    result = collect_local_mailbox_evidence(
        {
            "paper-1": {
                "title": "Exact Paper Title",
                "submission_id": "MS-123",
                "submission_records": {
                    "MS-122-OLD": {
                        "submission_id": "MS-122-OLD",
                        "status": "rejected",
                        "active": False,
                    }
                },
            }
        },
        store=store,
        sources=sources,
        pop_factory=lambda _source, password: observed_passwords.append(password) or pop,
    )

    assert store.revealed == ["vault:school-current"]
    assert observed_passwords == ["unit-test-secret"]
    assert result["available_source_count"] == 1
    evidence = result["paper_evidence"]["paper-1"]
    assert evidence[0]["matched_by"] == "submission_id"
    assert evidence[0]["sender_domain"] == "journal.example"
    assert "unit-test-secret" not in repr(result)
    assert "author@xujc.com" not in repr(result)
    assert pop.closed is True


def test_source_aggregation_requires_dashboard_when_mail_has_no_decision() -> None:
    aggregate = aggregate_monitor_source_results(
        {
            "source_results": [
                {
                    "source_id": "gmail",
                    "source_label": "Gmail",
                    "source_kind": "gmail_browser",
                    "available": True,
                    "status": "monitoring",
                    "action_kind": "none",
                    "summary": "No actionable notification.",
                },
                {
                    "source_id": "xujc_mail-1",
                    "source_label": "学校邮箱",
                    "source_kind": "xujc_pop3",
                    "available": True,
                    "status": "monitoring",
                    "action_kind": "none",
                    "summary": "No actionable notification.",
                },
            ]
        }
    )

    assert aggregate["status"] == "monitoring"
    assert aggregate["requires_dashboard"] is True
    assert len(aggregate["source_results"]) == 2


def test_source_aggregation_uses_explicit_school_mailbox_decision() -> None:
    aggregate = aggregate_monitor_source_results(
        {
            "source_results": [
                {
                    "source_id": "gmail",
                    "source_label": "Gmail",
                    "source_kind": "gmail_browser",
                    "available": True,
                    "status": "monitoring",
                    "action_kind": "none",
                },
                {
                    "source_id": "xujc_mail-1",
                    "source_label": "学校邮箱",
                    "source_kind": "xujc_pop3",
                    "available": True,
                    "status": "accepted",
                    "action_kind": "accepted",
                    "summary": "The manuscript was accepted.",
                    "message_date": "2026-07-18T12:00:00+00:00",
                },
            ]
        }
    )

    assert aggregate["status"] == "accepted"
    assert aggregate["action_kind"] == "accepted"
    assert aggregate["requires_dashboard"] is False


def test_source_aggregation_sends_undated_conflict_to_dashboard() -> None:
    aggregate = aggregate_monitor_source_results(
        {
            "source_results": [
                {
                    "source_id": "gmail",
                    "status": "accepted",
                    "action_kind": "accepted",
                },
                {
                    "source_id": "xujc_mail-1",
                    "status": "rejected",
                    "action_kind": "retarget",
                },
            ]
        }
    )

    assert aggregate["status"] == "monitoring"
    assert aggregate["conflict"] is True
    assert aggregate["requires_dashboard"] is True


def test_mailbox_no_decision_is_requeued_for_journal_dashboard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "smart_submission_state.json"
    smart.write_json_atomic(
        state_path,
        {
            "version": smart.SMART_STATE_VERSION,
            "settings": {"max_parallel_workers": 2},
            "online_queue": [],
            "papers": {
                "paper-1": {
                    "paper_id": "paper-1",
                    "status": "monitoring",
                    "workflow_status": "monitoring",
                    "worker_status": "monitoring",
                    "route_revision": 2,
                    "attempt_id": "mail-attempt",
                }
            },
        },
    )
    monkeypatch.setattr(smart, "SMART_STATE_PATH", state_path)
    monkeypatch.setattr(smart, "_unified_state_snapshot", lambda: {})
    monkeypatch.setattr(
        smart,
        "_worker_route_guard_result",
        lambda *_args, **_kwargs: ({"owner": "smart", "mode": "sdk", "revision": 2}, None),
    )
    monkeypatch.setattr(smart, "_mark_worker_route_on_paper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        smart,
        "_rotate_canary_attempt",
        lambda paper, _paper_id: paper.update({"attempt_id": "dashboard-attempt"})
        or "dashboard-attempt",
    )
    monkeypatch.setattr(
        smart,
        "_append_online_queue_guarded",
        lambda queue, paper_id, *_args, **_kwargs: queue.append(paper_id)
        if paper_id not in queue
        else None,
    )

    result = smart.queue_smart_monitor_dashboard_fallback(["paper-1"])
    state = smart.read_json(state_path, {})
    paper = state["papers"]["paper-1"]

    assert result["queued"] == ["paper-1"]
    assert paper["monitor_check_pending"] is True
    assert paper["monitor_pending_channel"] == smart.SMART_MONITOR_CHANNEL_JOURNAL_DASHBOARD
    assert paper["workflow_status"] == "queued"
    assert paper["attempt_id"] == "dashboard-attempt"
    assert paper["next_action"] == smart.SCHEDULED_MONITORING_NEXT
    assert state["online_queue"] == ["paper-1"]


def test_dashboard_fallback_failure_is_visible_and_retryable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "smart_submission_state.json"
    smart.write_json_atomic(
        state_path,
        {
            "version": smart.SMART_STATE_VERSION,
            "settings": {"max_parallel_workers": 1},
            "online_queue": [],
            "papers": {
                "paper-1": {
                    "paper_id": "paper-1",
                    "status": "monitoring",
                    "workflow_status": "monitoring",
                    "worker_status": "monitoring",
                    "route_revision": 2,
                    "attempt_id": "mail-attempt",
                }
            },
        },
    )
    monkeypatch.setattr(smart, "SMART_STATE_PATH", state_path)
    monkeypatch.setattr(smart, "_unified_state_snapshot", lambda: {})
    monkeypatch.setattr(
        smart,
        "_worker_route_guard_result",
        lambda *_args, **_kwargs: (
            None,
            {"code": "SMART_ROUTE_OWNER_MISMATCH"},
        ),
    )

    result = smart.queue_smart_monitor_dashboard_fallback(["paper-1"])
    paper = smart.read_json(state_path, {})["papers"]["paper-1"]

    assert result["queued"] == []
    assert result["failed"] == {"paper-1": "SMART_ROUTE_OWNER_MISMATCH"}
    assert paper["last_monitor_check_outcome"] == "failed"
    assert paper["workflow_status"] == "monitoring"
    assert paper["next_action"] == smart.EMAIL_FIRST_MONITORING_NEXT
    assert paper.get("monitor_check_pending") is None
