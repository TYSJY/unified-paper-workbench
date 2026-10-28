from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from unified_paper_orchestrator import smart_submission as smart
from unified_paper_orchestrator import __main__ as cli
from unified_paper_orchestrator import ai_provider_store as provider_store
from unified_paper_orchestrator.__main__ import main
from unified_paper_orchestrator.credential_resolver import SmartCredentialResolver
from unified_paper_orchestrator.submission_routing import normalize_submission_route, submission_route_for


class FakeCredentialVault:
    def reveal(self, _credential_ref: str) -> str:
        return "test-only-dpapi-secret"


class FakeConfirmationTransport:
    def __init__(
        self,
        *,
        url: str = "https://submission.springernature.com/confirm/MS-2026-123",
        manuscript_id: str = "MS-2026-123",
        confirmation_marker: str = "submission_received",
        final_submit_control_visible: bool = False,
        page_path: str = "/confirm/MS-2026-123",
    ) -> None:
        self.url = url
        self.manuscript_id = manuscript_id
        self.confirmation_marker = confirmation_marker
        self.final_submit_control_visible = final_submit_control_visible
        self.page_path = page_path

    def list_targets(self, port: int) -> list[dict[str, object]]:
        if port != 9224:
            return []
        return [
            {
                "type": "page",
                "url": self.url,
                "webSocketDebuggerUrl": "ws://127.0.0.1:9224/devtools/page/confirmation",
            }
        ]

    def evaluate_submission_confirmation_state(
        self,
        _port: int,
        _websocket_url: str,
    ) -> dict[str, object]:
        return {
            "origin": "https://submission.springernature.com",
            "document_ready": True,
            "confirmation_marker": self.confirmation_marker,
            "final_submit_control_visible": self.final_submit_control_visible,
            "login_challenge_visible": False,
            "manuscript_id": self.manuscript_id,
            "page_path": self.page_path,
        }


class FakeLocalizedReviewController:
    def __init__(self, review_url: str) -> None:
        self.review_url = review_url
        self.snapshot_calls = 0

    def list_pages(self, _allowed_origins) -> list[dict[str, object]]:
        return [
            {
                "page_id": "page-review-1",
                "url": self.review_url,
                "title": "Review",
            }
        ]

    def snapshot(self, _allowed_origins, *, page_id: str = "") -> dict[str, object]:
        assert page_id == "page-review-1"
        self.snapshot_calls += 1
        return {
            "ok": True,
            "page_id": page_id,
            "cdp_port": 9224,
            "url": self.review_url,
            "path": urlparse(self.review_url).path,
            "ready_state": "complete",
            "text": "Review your manuscript. No more changes can be made after submission.",
            "controls": [
                {
                    "ref": "upo-0027",
                    "tag": "button",
                    "type": "submit",
                    "label": "提交稿件",
                    "disabled": False,
                }
            ],
        }


class FakeEnglishReviewController:
    def __init__(self, review_url: str) -> None:
        self.review_url = review_url
        self.snapshot_calls = 0

    def list_pages(self, _allowed_origins) -> list[dict[str, object]]:
        return [
            {
                "page_id": "page-review-1",
                "url": self.review_url,
                "title": "Final Review",
            }
        ]

    def snapshot(self, _allowed_origins, *, page_id: str = "") -> dict[str, object]:
        assert page_id == "page-review-1"
        self.snapshot_calls += 1
        return {
            "ok": True,
            "page_id": page_id,
            "cdp_port": 9224,
            "url": self.review_url,
            "path": urlparse(self.review_url).path,
            "ready_state": "complete",
            "text": "Review your manuscript before completing the submission.",
            "controls": [
                {
                    "ref": "upo-0042",
                    "tag": "button",
                    "type": "submit",
                    "label": "Complete my submission",
                    "disabled": False,
                }
            ],
        }


class FakeSuccessPageController:
    def __init__(self, success_url: str) -> None:
        self.success_url = success_url
        self.snapshot_calls = 0

    def list_pages(self, _allowed_origins) -> list[dict[str, object]]:
        return [
            {
                "page_id": "page-success-1",
                "url": self.success_url,
                "title": "Submission received",
            }
        ]

    def snapshot(self, _allowed_origins, *, page_id: str = "") -> dict[str, object]:
        assert page_id == "page-success-1"
        self.snapshot_calls += 1
        return {
            "ok": True,
            "page_id": page_id,
            "cdp_port": 9224,
            "url": self.success_url,
            "path": "/new/submission/DRAFT-SUCCESS-1/success",
            "ready_state": "complete",
            "title": "Submission received",
            "text": "Thank you. Your submission has been received and is in technical checks.",
            "controls": [],
        }


class FakeRouteOrchestrator:
    def __init__(self, unified_path: Path) -> None:
        self.unified_path = unified_path
        self.calls: list[dict[str, object]] = []

    def set_submission_route(
        self,
        paper_id: str,
        *,
        execution_owner: str,
        mode: str,
        expected_revision: int | None = None,
        operation_id: str = "",
    ) -> dict[str, object]:
        state = json.loads(self.unified_path.read_text(encoding="utf-8"))
        current = submission_route_for(state.get("submission_routes"), paper_id)
        if expected_revision is not None and int(expected_revision) != int(current["revision"]):
            raise RuntimeError("route revision conflict")
        requested = normalize_submission_route(
            {
                "execution_owner": execution_owner,
                "mode": mode,
                "revision": current["revision"],
            }
        )
        changed = (
            requested["execution_owner"] != current["execution_owner"]
            or requested["mode"] != current["mode"]
        )
        if changed:
            requested["revision"] = int(current["revision"]) + 1
            state.setdefault("submission_routes", {})[paper_id] = requested
            if operation_id:
                state.setdefault("submission_route_operations", {})[paper_id] = {
                    "operation_id": operation_id,
                    "route_revision": int(requested["revision"]),
                    "execution_owner": execution_owner,
                    "mode": mode,
                }
            state["papers"][paper_id]["submission_route"] = copy.deepcopy(requested)
            self.unified_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            requested = current
        call = {
            "paper_id": paper_id,
            "execution_owner": execution_owner,
            "mode": mode,
            "expected_revision": expected_revision,
            "operation_id": operation_id,
        }
        self.calls.append(call)
        return {
            "changed": changed,
            "paper_id": paper_id,
            "previous_route": current,
            "submission_route": requested,
            "operation_id": operation_id,
        }

    def sync(self) -> dict[str, object]:
        return json.loads(self.unified_path.read_text(encoding="utf-8"))


@pytest.fixture()
def guarded_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    smart_root = tmp_path / "state" / "smart_submission"
    journal_root = tmp_path / "journal"
    # Every mode gate in this test module must read an isolated legacy-default
    # provider store, never the user's current V1/API selection under repo state.
    monkeypatch.setattr(provider_store, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(smart, "SMART_SUBMISSION_DIR", smart_root)
    monkeypatch.setattr(smart, "SMART_STATE_PATH", smart_root / "smart_submission_state.json")
    monkeypatch.setattr(smart, "SMART_CREDENTIAL_PATH", smart_root / "credential_store.local.json")
    monkeypatch.setattr(smart, "SMART_CREDENTIAL_VAULT_PATH", smart_root / "credential_vault.local.json")
    monkeypatch.setattr(smart, "BROWSER_SESSION_EVIDENCE_KEY_PATH", smart_root / "browser_session_evidence_key.json")
    monkeypatch.setattr(smart, "SMART_SITE_REGISTRY_PATH", smart_root / "site_registry.json")
    monkeypatch.setattr(smart, "SMART_SCHEDULER_PROMPT_PATH", smart_root / "SMART_SUBMISSION_SCHEDULER_PROMPT.md")
    monkeypatch.setattr(smart, "SMART_AUDIT_DIR", smart_root / "audit_runs")
    monkeypatch.setattr(smart, "SMART_SCREENSHOT_DIR", smart_root / "screenshots")
    monkeypatch.setattr(smart, "SMART_SNAPSHOT_DIR", smart_root / "page_snapshots")
    monkeypatch.setattr(smart, "SMART_CONFIRMATION_EVIDENCE_DIR", smart_root / "confirmation_evidence")
    monkeypatch.setattr(smart, "SMART_LOG_DIR", smart_root / "logs")
    monkeypatch.setattr(smart, "SMART_UPLOAD_STAGING_DIR", smart_root / "upload_staging")
    monkeypatch.setattr(
        smart,
        "SMART_SUBMISSION_BROWSER_CONTEXT_PATH",
        smart_root / "sdk_browser_contexts.json",
    )
    monkeypatch.setattr(smart, "SMART_AUTOMATION_PATH", tmp_path / "automation.toml")
    monkeypatch.setattr(smart, "UNIFIED_STATE_PATH", tmp_path / "unified_state.json")
    monkeypatch.setattr(smart, "LEGACY_SUBMISSION_STATE_PATH", tmp_path / "legacy_submission_state.json")
    monkeypatch.setattr(smart, "JOURNAL_ROOT", journal_root)
    monkeypatch.setattr(smart, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(smart, "_browser_session_evidence_verifier", lambda _evidence: True)
    monkeypatch.setattr(smart, "_verify_foreground_ui_gesture", lambda _handle: True)
    return {
        "root": tmp_path,
        "smart": smart_root,
        "journal": journal_root,
        "unified": smart.UNIFIED_STATE_PATH,
        "legacy": smart.LEGACY_SUBMISSION_STATE_PATH,
    }


def write_route_state(paths: dict[str, Path], *, owner: str = "legacy_auto", mode: str = "production", revision: int = 1) -> None:
    paper_id = "paper-1"
    package_dir = paths["journal"] / "papers" / paper_id / "package"
    state = {
        "papers": {
            paper_id: {
                "paper_id": paper_id,
                "title": "Guarded canary paper",
                "stages": {
                    "journal_author": {
                        "status": "complete",
                        "artifact": str(package_dir),
                    }
                },
                "submission_route": {
                    "execution_owner": owner,
                    "mode": mode,
                    "revision": revision,
                },
            }
        },
        "submission_routes": {
            paper_id: {
                "execution_owner": owner,
                "mode": mode,
                "revision": revision,
            }
        },
        "paper_queue": [],
        "history": [],
    }
    paths["unified"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def write_valid_canary_inputs(paths: dict[str, Path]) -> Path:
    paper_id = "paper-1"
    paper_dir = paths["journal"] / "papers" / paper_id
    work_dir = paper_dir / "work"
    package_dir = paper_dir / "package"
    work_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    manuscript = (
        "\\documentclass{article}\n"
        "\\title{Guarded canary paper}\n"
        "\\author{Test Author}\n"
        "\\begin{document}\n"
        "\\section{Introduction}\n"
        "Validated canary content.\n"
        "\\end{document}\n"
    )
    (work_dir / f"{paper_id}.tex").write_text(manuscript, encoding="utf-8")
    (package_dir / "main.tex").write_text(manuscript, encoding="utf-8")
    (package_dir / "main.pdf").write_bytes(b"%PDF-1.4\n% guarded canary\n")
    (package_dir / "cover_letter.md").write_text(
        "# Cover letter\n\nValidated canary submission.\n",
        encoding="utf-8",
    )
    (package_dir / "declarations.md").write_text(
        "# Declarations\n\nAll routine declarations are recorded.\n",
        encoding="utf-8",
    )
    (package_dir / "file_manifest.md").write_text(
        "# File manifest\n\n- main.pdf\n- main.tex\n",
        encoding="utf-8",
    )
    (package_dir / "submission_manifest.json").write_text("{}", encoding="utf-8")
    fingerprint = '{"paper_id":"paper-1","sha256":"abc"}\n'
    (paper_dir / "submission_metadata_fingerprint.json").write_text(fingerprint, encoding="utf-8")
    (package_dir / "package_metadata_fingerprint.json").write_text(fingerprint, encoding="utf-8")
    (paper_dir / "author_info.md").write_text("Status: READY\n", encoding="utf-8")
    (paper_dir / "journal.md").write_text(
        "# Journal Decision\n\n"
        "## Paper ID\n\npaper-1\n\n"
        "## Paper Title\n\nGuarded canary paper\n\n"
        "## Recommended Journal\n\nExample Journal\n\n"
        "## Why this journal is the safest choice\n\nScope fit.\n\n"
        "## Scope match\n\nStrong.\n\n"
        "## Quartile / indexing evidence\n\nVerified index.\n\n"
        "## Similar published papers\n\nhttps://example.org/paper\n\n"
        "## APC and cost\n\nNo APC.\n\n"
        "## Submission format notes\n\nLaTeX and PDF.\n\n"
        "## Required files\n\nMain manuscript.\n\n"
        "## Top 5 candidates\n\nFive verified candidates.\n\n"
        "## Rejected journals\n\nNone.\n\n"
        "## Final decision\n\nFinal decision: Recommended\n\n"
        "Publisher/system: Springer Nature / SNAPP\n",
        encoding="utf-8",
    )
    (package_dir / "final_risk_report.md").write_text(
        "# Final risk report\n\nPACKAGE_STATUS=READY_TO_SUBMIT\n",
        encoding="utf-8",
    )
    (package_dir / "submission_checklist.md").write_text(
        "# Submission checklist\n\nAll checks complete.\n",
        encoding="utf-8",
    )
    paths["legacy"].write_text(json.dumps({"papers": {}, "paper_queue": []}), encoding="utf-8")
    paths["smart"].mkdir(parents=True, exist_ok=True)
    smart.write_json_atomic(smart.SMART_STATE_PATH, smart._default_state())
    session_checked_at = datetime.now().astimezone()
    smart.write_json_atomic(
        smart.SMART_CREDENTIAL_PATH,
        {
            "accounts": [
                {
                    "account_id": "springer-edge-vault",
                    "scope": "journal_system",
                    "provider": "Springer Nature",
                    "username": "stored-locally@example.test",
                    "password": "test-only-legacy-password",
                    "status": "verified",
                    "login_url": "https://submission.springernature.com/",
                    "browser_session_evidence": {
                        "schema": "smart-browser-session-evidence/v1",
                        "evidence_id": "a" * 32,
                        "live": True,
                        "account_id": "springer-edge-vault",
                        "checked_at": session_checked_at.isoformat(),
                        "expires_at": (session_checked_at + timedelta(minutes=5)).isoformat(),
                        "origin": "https://submission.springernature.com:443",
                        "probe_method": "cdp-runtime-value-free/v1",
                        "cdp_port": 9224,
                        "page_state": {
                            "target_type": "page",
                            "document_ready": True,
                            "exact_origin": True,
                            "login_challenge_visible": False,
                            "authenticated_ui_visible": True,
                        },
                        "value_free": True,
                        "signature_alg": "hmac-sha256",
                        "signature": "0" * 64,
                    },
                }
            ]
        },
    )
    smart.write_json_atomic(
        smart.SMART_SITE_REGISTRY_PATH,
        {
            "systems": [
                {
                    "system_id": "springer_nature",
                    "status": "verified",
                    "login_url": "https://submission.springernature.com/",
                    "submission_url": "https://submission.springernature.com/",
                }
            ],
            "journals": [
                {
                    "journal_key": "example_journal",
                    "journal_name": "Example Journal",
                    "system_id": "springer_nature",
                    "status": "verified",
                    "submission_url": "https://submission.springernature.com/example",
                    "linked_papers": [paper_id],
                }
            ],
        },
    )
    return package_dir


def test_preflight_fails_closed_on_broken_dpapi_reference_even_with_plaintext_fallback(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    credentials = json.loads(smart.SMART_CREDENTIAL_PATH.read_text(encoding="utf-8"))
    account = credentials["accounts"][0]
    account["credential_ref"] = "vault:missing-test-record"
    account["password"] = "must-not-fallback"
    account.pop("browser_session_evidence", None)
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)

    result = smart.preflight_single_smart_canary("paper-1", expected_route_revision=1)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["ok"] is False
    assert result["code"] == smart.SMART_CANARY_ACCOUNT_MISSING
    assert "must-not-fallback" not in serialized
    assert "missing-test-record" not in serialized


def test_preflight_requires_secret_broker_for_resolvable_dpapi_reference(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    credentials = json.loads(smart.SMART_CREDENTIAL_PATH.read_text(encoding="utf-8"))
    account = credentials["accounts"][0]
    account["credential_ref"] = "vault:test-record"
    account["password"] = "retained-plaintext-must-not-be-handed-off"
    account.pop("browser_session_evidence", None)
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)
    resolver = SmartCredentialResolver("unused.json", vault=FakeCredentialVault())
    monkeypatch.setattr(smart, "_smart_credential_resolver", lambda: resolver)

    result = smart.preflight_single_smart_canary("paper-1", expected_route_revision=1)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["ok"] is False
    assert result["code"] == smart.SMART_SECRET_BROKER_REQUIRED
    assert result["details"]["credential_source"] == "dpapi"
    assert "test-only-dpapi-secret" not in serialized
    assert "retained-plaintext-must-not-be-handed-off" not in serialized
    assert "vault:test-record" not in serialized


def test_preflight_requires_secret_broker_for_plaintext_without_live_session(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    credentials = json.loads(smart.SMART_CREDENTIAL_PATH.read_text(encoding="utf-8"))
    credentials["accounts"][0].pop("browser_session_evidence", None)
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)

    result = smart.preflight_single_smart_canary("paper-1", expected_route_revision=1)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["ok"] is False
    assert result["code"] == smart.SMART_SECRET_BROKER_REQUIRED
    assert result["details"]["credential_source"] == "legacy_plaintext"
    assert "test-only-legacy-password" not in serialized


def test_worker_prompt_refuses_queued_dpapi_account_before_any_prompt_build(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=1)
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "ready_for_online_submit",
            "workflow_status": "queued",
            "worker_status": "queued",
            "credential_account_id": "account-one",
            "credential_source": "dpapi",
            "account_readiness": "secret_broker_required",
        }
    }
    state["online_queue"] = ["paper-1"]
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    with pytest.raises(smart.SmartSubmissionGuardError) as caught:
        smart.build_visible_worker_prompt("paper-1", route_revision=1)

    assert caught.value.code == smart.SMART_SECRET_BROKER_REQUIRED


def test_worker_prompt_revalidates_session_after_queue_and_rejects_expiry(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=1)
    write_valid_canary_inputs(guarded_workspace)
    credentials = json.loads(smart.SMART_CREDENTIAL_PATH.read_text(encoding="utf-8"))
    session = credentials["accounts"][0]["browser_session_evidence"]
    now = datetime.now().astimezone()
    session["checked_at"] = (now - timedelta(minutes=20)).isoformat()
    session["expires_at"] = (now + timedelta(minutes=5)).isoformat()
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "ready_for_online_submit",
            "workflow_status": "queued",
            "worker_status": "queued",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
        }
    }
    state["online_queue"] = ["paper-1"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    with pytest.raises(smart.SmartSubmissionGuardError) as caught:
        smart.build_visible_worker_prompt("paper-1", route_revision=1)

    assert caught.value.code == smart.SMART_SECRET_BROKER_REQUIRED


def test_worker_prompt_envelope_is_recursive_allowlist_and_strips_url_secrets(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=1)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    (paper_dir / "author_info.md").write_text(
        "\n".join(
            [
                "Status: READY",
                "Name: Test Author",
                "Email: author@example.test",
                "Institution: Example University",
                "Country: China",
                "City: Example City",
                "Postal Code: 000000",
                "Department: Computer Science",
            ]
        ),
        encoding="utf-8",
    )
    sentinel = "SENTINEL_PROMPT_SECRET"
    (package_dir / "declarations.md").write_text(
        "## Author Contributions\nTest Author: all roles.\n\n"
        f"## Funding\npassword={sentinel}\n",
        encoding="utf-8",
    )
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "title": f"credential_ref=vault:{sentinel}",
            "status": "ready_for_online_submit",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-123",
            "draft_url": f"https://submission.springernature.com/draft/DRAFT-123?token={sentinel}#password={sentinel}",
            "upload_url": f"https://submission.springernature.com/upload?sessionid={sentinel}",
            "github_repo_url": f"https://github.com/example/repo?access_key={sentinel}",
            "standing_declaration_result": {"credential_ref": f"vault:{sentinel}"},
            "planned_github_repo_result": {"status": f"token={sentinel}"},
            "scheduler_prepared_payload": {"password": sentinel, "corresponding_author": {"name": sentinel}},
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    prompt = smart.build_visible_worker_prompt("paper-1", route_revision=1)

    assert sentinel not in prompt
    assert "credential_ref=vault:" not in prompt
    assert "password=" not in prompt
    assert "?token=" not in prompt
    assert "#password=" not in prompt
    assert "https://submission.springernature.com/draft/DRAFT-123" in prompt
    assert "https://submission.springernature.com/upload" in prompt
    assert "https://github.com/example/repo" in prompt
    assert '"final_submit_authorized": false' in prompt
    assert "SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED" in prompt


def test_credential_refresh_preserves_reviewed_final_submit_gate(
    guarded_workspace: dict[str, Path],
) -> None:
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "attempt_id": "attempt-1",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    smart.record_smart_credential_broker_state(
        "paper-1",
        {
            "status": "starting",
            "code": "SMART_CREDENTIAL_BROKER_STARTING",
            "account_id": "account-1",
            "origin": "https://submission.springernature.com:443",
            "next_action": "trusted_credential_broker_login",
        },
    )
    waiting = smart.record_smart_credential_broker_state(
        "paper-1",
        {
            "ok": False,
            "status": "login_incomplete",
            "code": "SMART_CREDENTIAL_BROKER_WAITING_HUMAN",
            "account_id": "account-1",
            "origin": "https://submission.springernature.com:443",
            "next_action": "continue_visible_login_then_resume",
        },
    )
    assert waiting["status"] == "waiting"
    assert waiting["workflow_status"] == "waiting"
    assert waiting["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    refreshed = smart.record_smart_credential_broker_state(
        "paper-1",
        {
            "ok": True,
            "status": "session_ready",
            "code": "SMART_CREDENTIAL_BROKER_SESSION_READY",
            "account_id": "account-1",
            "origin": "https://submission.springernature.com:443",
            "next_action": "dispatch_fixed_sdk_session",
        },
    )

    assert refreshed["status"] == "waiting"
    assert refreshed["workflow_status"] == "waiting"
    assert refreshed["worker_status"] == "waiting"
    assert refreshed["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert refreshed["attempt_id"] == "attempt-1"
    assert refreshed["credential_broker_status"] == "session_ready"
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert persisted["online_queue"] == ["paper-1"]
    broker_events = [
        item
        for item in persisted["history"]
        if item.get("event") == "smart_credential_broker_state"
    ]
    assert broker_events[-1]["final_submit_gate_preserved"] is True


def test_final_review_auto_policy_issues_the_same_exact_one_use_authorization(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    (paper_dir / "author_info.md").write_text(
        "Status: READY\n"
        "Name: Test Author\n"
        "Email: author@example.test\n"
        "Institution: Example University\n"
        "Country: China\n",
        encoding="utf-8",
    )
    (package_dir / "declarations.md").write_text(
        "## Author Contributions\nTest Author: all roles.\n",
        encoding="utf-8",
    )
    staging_cover = smart.SMART_UPLOAD_STAGING_DIR / "paper-1" / "cover_letter.pdf"
    staging_cover.parent.mkdir(parents=True, exist_ok=True)
    staging_cover.write_bytes(b"%PDF-1.4\nauto cover letter\n")
    state = smart._default_state()
    state["settings"]["auto_final_submit_enabled"] = True
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-auto-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-AUTO-1",
            "draft_url": "https://submission.springernature.com/draft/DRAFT-AUTO-1",
            "upload_url": "https://submission.springernature.com/draft/DRAFT-AUTO-1/files",
            "cover_letter_upload_file": str(staging_cover),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    authorized = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-auto-1",
        activate=False,
    )

    assert authorized["code"] == "SMART_FINAL_SUBMIT_AUTHORIZED"
    assert authorized["auto_authorized"] is True
    assert authorized["authorization_mode"] == smart.FINAL_SUBMIT_AUTO_PRESENTATION
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert persisted["next_action"] == smart.FINAL_SUBMIT_AUTHORIZED_ACTION
    assert (
        persisted["final_submit_confirmation_challenge"]["presentation"]
        == smart.FINAL_SUBMIT_AUTO_PRESENTATION
    )
    assert smart._valid_final_submit_authorization(
        "paper-1",
        persisted,
        route_revision=2,
        attempt_id="attempt-auto-1",
    ) is not None

    damaged_state = smart.read_json(smart.SMART_STATE_PATH, {})
    damaged = damaged_state["papers"]["paper-1"]
    damaged["status"] = "waiting"
    damaged["workflow_status"] = "waiting"
    damaged["worker_status"] = "waiting_user_action"
    damaged["next_action"] = "continue_visible_login_then_resume"
    smart.write_json_atomic(smart.SMART_STATE_PATH, damaged_state)
    recovered = smart.record_smart_credential_broker_state(
        "paper-1",
        {
            "ok": True,
            "status": "session_ready",
            "code": "SMART_CREDENTIAL_BROKER_SESSION_READY",
            "account_id": "springer-edge-vault",
            "origin": "https://submission.springernature.com:443",
            "next_action": "dispatch_fixed_sdk_session",
        },
    )
    assert recovered["status"] == "ready_for_online_submit"
    assert recovered["workflow_status"] == "queued"
    assert recovered["next_action"] == smart.FINAL_SUBMIT_AUTHORIZED_ACTION
    recovered_state = smart.read_json(smart.SMART_STATE_PATH, {})
    assert recovered_state["online_queue"] == ["paper-1"]
    history = recovered_state["history"]
    assert history[-1]["authorization_gate_recovered"] is True


def test_unused_final_authorization_is_reissued_after_only_session_binding_changes(
    guarded_workspace: dict[str, Path],
    monkeypatch,
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    state = smart._default_state()
    state["settings"]["auto_final_submit_enabled"] = True
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-rebind-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(package_dir.parent),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-REBIND-1",
            "draft_url": (
                "https://submission.springernature.com/draft/DRAFT-REBIND-1"
            ),
            "upload_url": (
                "https://submission.springernature.com/draft/DRAFT-REBIND-1/files"
            ),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-rebind-1",
        activate=False,
    )
    original_binding = smart._final_submit_target_binding(
        "paper-1",
        smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"],
    )
    rebound = {
        **original_binding,
        "account_binding_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        smart,
        "_final_submit_target_binding",
        lambda _paper_id, _paper: dict(rebound),
    )

    released = smart.release_expired_unclaimed_final_submit_authorization(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-rebind-1",
    )

    assert released["released"] is True
    assert released["session_binding_rebound"] is True
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"]["paper-1"]
    assert paper["status"] == "waiting"
    assert paper["workflow_status"] == "waiting"
    assert paper["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert "final_submit_authorization" not in paper
    assert "paper-1" not in persisted["online_queue"]


def test_federated_wiley_final_binding_uses_owned_submission_page_claim(
    guarded_workspace: dict[str, Path],
) -> None:
    write_valid_canary_inputs(guarded_workspace)
    now = datetime.now().astimezone()
    context_id = "sbx_" + ("a" * 48)
    page_id = "wiley-page-1"
    attempt_id = "attempt-wiley-1"
    review_url = (
        "https://submission.wiley.com/submission/submissionBoard/"
        "45c19a2c-7910-4173-8f3f-029248569c8f/finalReview"
    )
    credentials = smart.read_json(smart.SMART_CREDENTIAL_PATH, {})
    account = credentials["accounts"][0]
    account["account_id"] = "wiley-account"
    account["provider"] = "Wiley"
    account["login_url"] = "https://authors.wiley.com/dashboard"
    account["browser_session_evidence"]["account_id"] = "wiley-account"
    account["browser_session_evidence"]["origin"] = "https://authors.wiley.com:443"
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)
    smart.write_json_atomic(
        smart.SMART_SITE_REGISTRY_PATH,
        {
            "systems": [
                {
                    "system_id": "wiley",
                    "status": "verified",
                    "login_url": "https://authors.wiley.com/dashboard",
                    "submission_url": "https://authors.wiley.com/dashboard",
                }
            ],
            "journals": [
                {
                    "journal_key": "example_journal",
                    "journal_name": "Example Journal",
                    "system_id": "wiley",
                    "status": "verified",
                    "submission_url": "https://authors.wiley.com/journal/EXAMPLE",
                    "linked_papers": ["paper-1"],
                }
            ],
        },
    )
    smart.write_json_atomic(
        smart.SMART_SUBMISSION_BROWSER_CONTEXT_PATH,
        {
            "schema": "smart-submission-sdk-browser-context-store/v1",
            "contexts": {
                context_id: {
                    "paper_id": "paper-1",
                    "attempt_id": attempt_id,
                    "route_revision": 2,
                    "retired_at": (now + timedelta(seconds=1)).isoformat(),
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                    "allowed_origins": [
                        "https://authors.wiley.com:443",
                        "https://submission.wiley.com:443",
                    ],
                }
            },
            "page_claims": {
                page_id: {
                    "paper_id": "paper-1",
                    "context_id": context_id,
                    "route_revision": 2,
                    "attempt_id": attempt_id,
                    "origin": "https://submission.wiley.com:443",
                    "url": review_url,
                    "updated_at": now.isoformat(),
                }
            },
            "updated_at": now.isoformat(),
        },
    )
    paper = {
        "paper_id": "paper-1",
        "attempt_id": attempt_id,
        "route_revision": 2,
        "credential_account_id": "wiley-account",
        "submission_system_id": "wiley",
        "target_journal": "Example Journal",
    }

    binding = smart._final_submit_target_binding("paper-1", paper)

    assert binding["target_origin"] == "https://submission.wiley.com:443"
    assert paper["draft_url"] == review_url
    assert paper["draft_binding_source"] == "trusted_sdk_browser_page_claim"
    assert paper["draft_binding_page_id"] == page_id
    assert smart._sanitize_confirmation_url(review_url, paper=paper) == review_url


def test_submission_page_claim_from_another_attempt_is_not_bound(
    guarded_workspace: dict[str, Path],
) -> None:
    now = datetime.now().astimezone()
    context_id = "sbx_" + ("b" * 48)
    smart.write_json_atomic(
        smart.SMART_SUBMISSION_BROWSER_CONTEXT_PATH,
        {
            "schema": "smart-submission-sdk-browser-context-store/v1",
            "contexts": {
                context_id: {
                    "paper_id": "paper-1",
                    "attempt_id": "stale-attempt",
                    "route_revision": 2,
                    "retired_at": "",
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                    "allowed_origins": ["https://submission.springernature.com:443"],
                }
            },
            "page_claims": {
                "stale-page": {
                    "paper_id": "paper-1",
                    "context_id": context_id,
                    "route_revision": 2,
                    "attempt_id": "stale-attempt",
                    "origin": "https://submission.springernature.com:443",
                    "url": "https://submission.springernature.com/draft/STALE",
                    "updated_at": now.isoformat(),
                }
            },
        },
    )
    paper = {
        "paper_id": "paper-1",
        "attempt_id": "current-attempt",
        "route_revision": 2,
        "credential_account_id": "springer-edge-vault",
        "submission_system_id": "springer_nature",
        "target_journal": "Example Journal",
    }

    assert smart._trusted_claimed_submission_page_binding("paper-1", paper) == {}


def test_localized_preclick_unknown_is_restored_only_after_double_live_proof(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    review_url = (
        "https://submission.springernature.com/new/submission/"
        "DRAFT-LOCALIZED-1/review"
    )
    state = smart._default_state()
    state["settings"]["auto_final_submit_enabled"] = True
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-localized-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-LOCALIZED-1",
            "draft_url": review_url,
            "upload_url": review_url.replace("/review", "/upload-files"),
            "sdk_turn_id": "turn-localized-1",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    authorized = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-localized-1",
        activate=False,
    )
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-localized-1",
        route_revision=2,
        attempt_id="attempt-localized-1",
    )
    smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-localized-1",
        thread_id="thread-localized-1",
        authorization_id=authorized["authorization_id"],
    )
    smart.record_final_submit_result_unknown(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-localized-1",
        thread_id="thread-localized-1",
        authorization_id=authorized["authorization_id"],
        detail="The localized final control was rejected before CDP evaluation.",
    )
    smart.write_json_atomic(
        smart.SMART_STATE_PATH.with_name("sdk_dispatch.json"),
        {
            "papers": {
                "paper-1": {
                    "thread_id": "thread-localized-1",
                    "turn_id": "turn-localized-1",
                    "route_revision": 2,
                    "attempt_id": "attempt-localized-1",
                    "authorization_id": authorized["authorization_id"],
                    "status": "completed",
                    "recorded_status": "final_submit_result_unknown",
                    "final_response": (
                        "browser_final_submit_once rejected the localized submit "
                        "control as not verified and cannot be retried in this turn."
                    ),
                }
            }
        },
    )
    controller = FakeLocalizedReviewController(review_url)

    recovered = smart.recover_localized_final_submit_preclick_unknown(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-localized-1",
        thread_id="thread-localized-1",
        authorization_id=authorized["authorization_id"],
        controller=controller,
    )

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert recovered["code"] == "SMART_FINAL_SUBMIT_NO_CLICK_VERIFIED"
    assert controller.snapshot_calls == 2
    assert paper["status"] == "waiting"
    assert paper["workflow_status"] == "waiting"
    assert paper["worker_status"] == "waiting"
    assert paper["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert paper["final_submit_result_unknown_resolution"] == "verified_no_click"
    assert "final_submit_authorization" not in paper
    assert (
        paper["last_verified_no_click_authorization"]["authorization_id"]
        == authorized["authorization_id"]
    )
    assert Path(recovered["audit_path"]).is_file()


def test_timed_out_final_tool_is_released_only_after_unchanged_review_proof(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    review_url = (
        "https://submission.springernature.com/new/submission/"
        "DRAFT-TIMEOUT-1/review"
    )
    state = smart._default_state()
    state["settings"]["auto_final_submit_enabled"] = True
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-timeout-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-TIMEOUT-1",
            "draft_url": review_url,
            "upload_url": review_url.replace("/review", "/upload-files"),
            "sdk_turn_id": "turn-timeout-1",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    authorized = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-timeout-1",
        activate=False,
    )
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-timeout-1",
        route_revision=2,
        attempt_id="attempt-timeout-1",
    )
    smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-timeout-1",
        thread_id="thread-timeout-1",
        authorization_id=authorized["authorization_id"],
    )
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"]["paper-1"]
    receipt = copy.deepcopy(paper["final_submit_authorization"])
    paper["status"] = "blocked"
    paper["workflow_status"] = "blocked"
    paper["worker_status"] = "blocked"
    paper["next_action"] = smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    paper["sdk_turn_id"] = "turn-timeout-1"
    persisted["online_queue"] = []
    smart.write_json_atomic(smart.SMART_STATE_PATH, persisted)
    smart.write_json_atomic(
        smart.SMART_STATE_PATH.with_name("sdk_dispatch.json"),
        {
            "papers": {
                "paper-1": {
                    "thread_id": "thread-timeout-1",
                    "turn_id": "turn-timeout-1",
                    "route_revision": 2,
                    "attempt_id": "attempt-timeout-1",
                    "authorization_id": authorized["authorization_id"],
                    "status": "completed",
                    "recorded_status": "blocked",
                    "final_response": (
                        "Trusted final-submit tool timed out after the one "
                        "authorized attempt, so the scheduler must inspect the "
                        "live review page."
                    ),
                }
            }
        },
    )
    expected_binding = {
        key: receipt[key]
        for key in (
            "target_origin",
            "journal_site_key",
            "account_binding_sha256",
            "site_binding_sha256",
            "draft_binding_sha256",
        )
    }

    def exact_page_binding(_paper_id, bound_paper):
        bound_paper["draft_binding_source"] = "trusted_sdk_browser_page_claim"
        bound_paper["draft_binding_page_id"] = "page-review-1"
        return dict(expected_binding)

    monkeypatch.setattr(smart, "_final_submit_target_binding", exact_page_binding)
    controller = FakeLocalizedReviewController(review_url)

    recovered = smart.recover_final_submit_timeout_when_review_is_unchanged(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-timeout-1",
        thread_id="thread-timeout-1",
        authorization_id=authorized["authorization_id"],
        controller=controller,
    )

    paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert recovered["code"] == "SMART_FINAL_SUBMIT_TIMEOUT_NO_CLICK_VERIFIED"
    assert controller.snapshot_calls == 2
    assert paper["status"] == "waiting"
    assert paper["workflow_status"] == "waiting"
    assert paper["worker_status"] == "waiting"
    assert paper["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert paper["final_submit_timeout_resolution"] == "verified_no_click"
    assert "final_submit_authorization" not in paper
    assert "final_submit_action_claimed_at" not in paper
    assert Path(recovered["audit_path"]).is_file()


def test_owned_live_final_review_reclassifies_false_revision_without_clicking(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    review_url = (
        "https://submission.springernature.com/new/submission/"
        "45c19a2c-7910-4173-8f3f-029248569c8f/finalReview"
    )
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "revision_needed",
            "workflow_status": "queued",
            "worker_status": "queued",
            "next_action": "codex_sdk_browser_submit_or_monitor",
            "attempt_id": "attempt-current",
            "thread_id": "thread-fixed",
            "route_revision": 2,
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.write_json_atomic(
        smart.SMART_SUBMISSION_BROWSER_CONTEXT_PATH,
        {
            "page_claims": {
                "page-review-1": {
                    "paper_id": "paper-1",
                    "attempt_id": "attempt-prior-worker",
                    "route_revision": 2,
                    "url": review_url,
                    "updated_at": datetime.now().astimezone().isoformat(),
                }
            }
        },
    )
    controller = FakeEnglishReviewController(review_url)

    result = smart.reconcile_live_owned_final_review_pages(
        ["paper-1"],
        controller=controller,
    )

    assert result["recovered"] == ["paper-1"]
    assert controller.snapshot_calls == 2
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"]["paper-1"]
    assert paper["status"] == "waiting"
    assert paper["workflow_status"] == "waiting"
    assert paper["worker_status"] == "waiting"
    assert paper["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert paper["attempt_id"] == "attempt-current"
    assert paper["thread_id"] == "thread-fixed"
    assert (
        paper["draft_binding_source"]
        == "trusted_sdk_browser_prior_page_claim_revalidated"
    )
    assert paper["draft_binding_page_id"] == "page-review-1"
    assert "paper-1" not in persisted["online_queue"]
    assert Path(result["audits"]["paper-1"]).is_file()


def test_sdk_queue_resumes_review_ready_task_without_rotating_exact_attempt(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    state = smart._default_state()
    state["settings"]["auto_final_submit_enabled"] = True
    state["settings"]["sdk_pause_requested"] = True
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-review-ready",
            "sdk_thread_id": "thread-fixed-review-ready",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-REVIEW-READY",
            "draft_url": "https://submission.springernature.com/draft/DRAFT-REVIEW-READY",
            "upload_url": "https://submission.springernature.com/draft/DRAFT-REVIEW-READY/files",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_visible_online_workers(
        ["paper-1"],
        activate=True,
        sdk_browser=True,
    )

    assert result["queued"] == ["paper-1"]
    assert result["resumed"] == ["paper-1"]
    assert result["skipped"] == {}
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"]["paper-1"]
    assert persisted["online_queue"] == ["paper-1"]
    assert persisted["settings"]["sdk_pause_requested"] is False
    assert paper["status"] == "waiting"
    assert paper["workflow_status"] == "waiting"
    assert paper["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert paper["attempt_id"] == "attempt-review-ready"
    assert paper["sdk_thread_id"] == "thread-fixed-review-ready"
    assert "final_submit_authorization" not in paper


def test_expired_unclaimed_auto_authorization_returns_to_review_and_is_reissued(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    (paper_dir / "author_info.md").write_text(
        "Status: READY\n"
        "Name: Test Author\n"
        "Email: author@example.test\n"
        "Institution: Example University\n"
        "Country: China\n",
        encoding="utf-8",
    )
    (package_dir / "declarations.md").write_text(
        "## Author Contributions\nTest Author: all roles.\n",
        encoding="utf-8",
    )
    staging_cover = smart.SMART_UPLOAD_STAGING_DIR / "paper-1" / "cover_letter.pdf"
    staging_cover.parent.mkdir(parents=True, exist_ok=True)
    staging_cover.write_bytes(b"%PDF-1.4\nexpired capability cover letter\n")
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-auto-expired",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-AUTO-EXPIRED",
            "draft_url": "https://submission.springernature.com/draft/DRAFT-AUTO-EXPIRED",
            "upload_url": "https://submission.springernature.com/draft/DRAFT-AUTO-EXPIRED/files",
            "cover_letter_upload_file": str(staging_cover),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    first = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-auto-expired",
        activate=False,
    )
    first_authorization_id = first["authorization_id"]

    persisted_state = smart.read_json(smart.SMART_STATE_PATH, {})
    receipt = dict(persisted_state["papers"]["paper-1"]["final_submit_authorization"])
    authorized_at = datetime.now().astimezone() - timedelta(minutes=30)
    receipt["authorized_at"] = authorized_at.isoformat()
    receipt["expires_at"] = (authorized_at + timedelta(minutes=20)).isoformat()
    persisted_state["papers"]["paper-1"]["final_submit_authorization"] = (
        smart._sign_smart_receipt(receipt)
    )
    smart.write_json_atomic(smart.SMART_STATE_PATH, persisted_state)

    released = smart.release_expired_unclaimed_final_submit_authorization(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-auto-expired",
    )
    assert released["released"] is True
    waiting = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert waiting["next_action"] == smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    assert "final_submit_authorization" not in waiting

    second = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-auto-expired",
        activate=False,
    )
    assert second["authorization_id"] != first_authorization_id
    reissued = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert smart._valid_final_submit_authorization(
        "paper-1",
        reissued,
        route_revision=2,
        attempt_id="attempt-auto-expired",
    ) is not None


def test_final_submit_manual_fallback_authorization_is_exact_and_one_use(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    paper_dir = package_dir.parent
    (paper_dir / "author_info.md").write_text(
        "Status: READY\n"
        "Name: Test Author\n"
        "Email: author@example.test\n"
        "Institution: Example University\n"
        "Country: China\n",
        encoding="utf-8",
    )
    (package_dir / "declarations.md").write_text(
        "## Author Contributions\nTest Author: all roles.\n",
        encoding="utf-8",
    )
    staging_cover = smart.SMART_UPLOAD_STAGING_DIR / "paper-1" / "cover_letter.pdf"
    staging_cover.parent.mkdir(parents=True, exist_ok=True)
    staging_cover.write_bytes(b"%PDF-1.4\nlocked cover letter\n")
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(paper_dir),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-READY-1",
            "draft_url": "https://submission.springernature.com/draft/DRAFT-READY-1",
            "upload_url": "https://submission.springernature.com/draft/DRAFT-READY-1/files",
            "cover_letter_upload_file": str(staging_cover),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    before = smart.SMART_STATE_PATH.read_bytes()

    with pytest.raises(smart.SmartSubmissionGuardError) as wrong_confirmation:
        smart.authorize_final_submit(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            confirmation_phrase="CONFIRM_FINAL_SUBMIT:wrong-paper:attempt-1",
            activate=False,
        )
    assert wrong_confirmation.value.code == smart.SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED
    assert smart.SMART_STATE_PATH.read_bytes() == before

    monkeypatch.setattr(smart, "_verify_foreground_ui_gesture", lambda _handle: False)
    with pytest.raises(smart.SmartSubmissionGuardError) as no_foreground_ui:
        smart.prepare_final_submit_confirmation_challenge(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            ui_window_handle=1,
        )
    assert no_foreground_ui.value.code == smart.SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED
    assert smart.SMART_STATE_PATH.read_bytes() == before
    monkeypatch.setattr(smart, "_verify_foreground_ui_gesture", lambda _handle: True)

    challenge = smart.prepare_final_submit_confirmation_challenge(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        ui_window_handle=1,
    )
    nonce = challenge["confirmation_phrase"].rsplit(":", 1)[-1]
    assert nonce not in smart.SMART_STATE_PATH.read_text(encoding="utf-8")
    assert all(
        nonce not in path.read_text(encoding="utf-8")
        for path in smart.SMART_AUDIT_DIR.glob("*.md")
    )
    state_after_challenge = smart.SMART_STATE_PATH.read_bytes()
    real_commit_audit = smart._commit_prepared_audit

    def fail_audit_commit(_prepared: dict[str, object]) -> Path:
        raise OSError("fault injected audit rename")

    monkeypatch.setattr(smart, "_commit_prepared_audit", fail_audit_commit)
    with pytest.raises(OSError, match="fault injected"):
        smart.authorize_final_submit(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            confirmation_phrase=challenge["confirmation_phrase"],
            activate=False,
        )
    failed_authorization_paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert smart._valid_final_submit_authorization(
        "paper-1",
        failed_authorization_paper,
        route_revision=2,
        attempt_id="attempt-1",
    ) is None
    monkeypatch.setattr(smart, "_commit_prepared_audit", real_commit_audit)
    smart.SMART_STATE_PATH.write_bytes(state_after_challenge)
    for pending in smart.SMART_AUDIT_DIR.glob("*.pending"):
        pending.unlink()

    authorized = smart.authorize_final_submit(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        confirmation_phrase=challenge["confirmation_phrase"],
        activate=False,
    )
    assert authorized["code"] == "SMART_FINAL_SUBMIT_AUTHORIZED"
    authorized_paper = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    expires_at = datetime.fromisoformat(authorized["expires_at"])
    assert smart._valid_final_submit_authorization(
        "paper-1",
        authorized_paper,
        route_revision=2,
        attempt_id="attempt-1",
        now=expires_at,
    ) is None

    original_main = (package_dir / "main.pdf").read_bytes()
    (package_dir / "main.pdf").write_bytes(original_main + b"changed")
    assert smart._valid_final_submit_authorization(
        "paper-1", authorized_paper, route_revision=2, attempt_id="attempt-1"
    ) is None
    (package_dir / "main.pdf").write_bytes(original_main)

    original_cover = staging_cover.read_bytes()
    staging_cover.write_bytes(original_cover + b"changed")
    assert smart._valid_final_submit_authorization(
        "paper-1", authorized_paper, route_revision=2, attempt_id="attempt-1"
    ) is None
    staging_cover.write_bytes(original_cover)

    changed_draft = copy.deepcopy(authorized_paper)
    changed_draft["draft_id"] = "DRAFT-OTHER"
    changed_draft["draft_url"] = "https://submission.springernature.com/draft/DRAFT-OTHER"
    changed_draft["upload_url"] = "https://submission.springernature.com/draft/DRAFT-OTHER/files"
    assert smart._valid_final_submit_authorization(
        "paper-1", changed_draft, route_revision=2, attempt_id="attempt-1"
    ) is None

    credentials_before = smart.SMART_CREDENTIAL_PATH.read_bytes()
    credentials = json.loads(credentials_before.decode("utf-8"))
    credentials["accounts"][0]["username"] = "changed-account@example.test"
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)
    assert smart._valid_final_submit_authorization(
        "paper-1", authorized_paper, route_revision=2, attempt_id="attempt-1"
    ) is None
    smart.SMART_CREDENTIAL_PATH.write_bytes(credentials_before)

    registry_before = smart.SMART_SITE_REGISTRY_PATH.read_bytes()
    registry = json.loads(registry_before.decode("utf-8"))
    registry["journals"][0]["submission_url"] = "https://submission.springernature.com/other-journal"
    smart.write_json_atomic(smart.SMART_SITE_REGISTRY_PATH, registry)
    assert smart._valid_final_submit_authorization(
        "paper-1", authorized_paper, route_revision=2, attempt_id="attempt-1"
    ) is None
    smart.SMART_SITE_REGISTRY_PATH.write_bytes(registry_before)

    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-final",
        route_revision=2,
        attempt_id="attempt-1",
    )
    with pytest.raises(smart.SmartSubmissionGuardError) as unclaimed_prompt:
        smart.build_visible_worker_prompt("paper-1", route_revision=2)
    assert unclaimed_prompt.value.code == smart.SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED
    claimed = smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-final",
        authorization_id=authorized["authorization_id"],
    )
    assert claimed["code"] == smart.SMART_FINAL_SUBMIT_ACTION_CLAIMED
    before_replay = smart.SMART_STATE_PATH.read_bytes()
    audits_before_replay = {path.name for path in smart.SMART_AUDIT_DIR.iterdir()}
    with pytest.raises(smart.SmartSubmissionGuardError):
        smart.claim_final_submit_action(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-final",
            authorization_id=authorized["authorization_id"],
        )
    assert smart.SMART_STATE_PATH.read_bytes() == before_replay
    assert {path.name for path in smart.SMART_AUDIT_DIR.iterdir()} == audits_before_replay
    prompt = smart.build_visible_worker_prompt("paper-1", route_revision=2)
    assert '"final_submit_authorized": true' in prompt
    assert authorized["authorization_id"] in prompt
    assert '"final_submit_click_deadline": ""' not in prompt
    assert "final_submit_action_claim_command" not in prompt
    before_false_capture = smart.SMART_STATE_PATH.read_bytes()
    audits_before_false_capture = {path.name for path in smart.SMART_AUDIT_DIR.iterdir()}
    with pytest.raises(smart.SmartSubmissionGuardError) as draft_page_only:
        smart.capture_live_submission_confirmation(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-final",
            authorization_id=authorized["authorization_id"],
            transport=FakeConfirmationTransport(
                url="https://submission.springernature.com/draft/DRAFT-READY-1",
                manuscript_id="",
                page_path="/draft/DRAFT-READY-1",
            ),
        )
    assert draft_page_only.value.code == smart.SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED
    with pytest.raises(smart.SmartSubmissionGuardError) as final_control_still_visible:
        smart.capture_live_submission_confirmation(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-final",
            authorization_id=authorized["authorization_id"],
            transport=FakeConfirmationTransport(final_submit_control_visible=True),
        )
    assert final_control_still_visible.value.code == smart.SMART_SUBMISSION_CONFIRMATION_EVIDENCE_REQUIRED
    assert smart.SMART_STATE_PATH.read_bytes() == before_false_capture
    assert {path.name for path in smart.SMART_AUDIT_DIR.iterdir()} == audits_before_false_capture
    capture = smart.capture_live_submission_confirmation(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-final",
        authorization_id=authorized["authorization_id"],
        transport=FakeConfirmationTransport(),
    )
    assert capture["code"] == "SMART_SUBMISSION_CONFIRMATION_CAPTURED"
    monkeypatch.setattr(
        smart,
        "_apply_open_submission_draft_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal success must not refresh an open-draft binding")
        ),
    )
    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-final",
        status="submitted",
        submission_id="MS-2026-123",
        evidence_path=capture["evidence_path"],
        confirmation_url="https://submission.springernature.com/confirm/MS-2026-123",
        activate=False,
    )
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert result["status"] == "submitted"
    assert persisted["final_submit_authorization"]["consumed_at"]
    assert smart._paper_has_prior_trusted_submission_evidence("paper-1", persisted) is True
    assert nonce not in smart.SMART_STATE_PATH.read_text(encoding="utf-8")
    assert smart._valid_final_submit_authorization(
        "paper-1",
        persisted,
        route_revision=2,
        attempt_id="attempt-1",
    ) is None


def test_reconcile_captured_success_repairs_false_block_without_clicking(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    success_url = (
        "https://submission.springernature.com/new/submission/"
        "DRAFT-SUCCESS-1/success"
    )
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-success-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(package_dir.parent),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-SUCCESS-1",
            "draft_url": (
                "https://submission.springernature.com/new/submission/"
                "DRAFT-SUCCESS-1/review"
            ),
            "upload_url": (
                "https://submission.springernature.com/new/submission/"
                "DRAFT-SUCCESS-1/upload-files"
            ),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    authorized = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-success-1",
        activate=False,
    )
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-success-1",
        route_revision=2,
        attempt_id="attempt-success-1",
    )
    smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-success-1",
        thread_id="thread-success-1",
        authorization_id=authorized["authorization_id"],
    )
    capture = smart.capture_live_submission_confirmation(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-success-1",
        thread_id="thread-success-1",
        authorization_id=authorized["authorization_id"],
        transport=FakeConfirmationTransport(
            url=success_url,
            manuscript_id="",
            page_path="/new/submission/DRAFT-SUCCESS-1/success",
        ),
    )

    failed_state = smart.read_json(smart.SMART_STATE_PATH, {})
    failed_paper = failed_state["papers"]["paper-1"]
    failed_paper["status"] = "blocked"
    failed_paper["workflow_status"] = "blocked"
    failed_paper["worker_status"] = "blocked"
    failed_paper["next_action"] = smart.FINAL_SUBMIT_REVIEW_READY_ACTION
    failed_paper["blocker"] = "worker reported blocked after success capture"
    failed_state["online_queue"] = []
    smart.write_json_atomic(smart.SMART_STATE_PATH, failed_state)

    controller = FakeSuccessPageController(success_url)
    result = smart.reconcile_captured_final_submit_success(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-success-1",
        thread_id="thread-success-1",
        authorization_id=authorized["authorization_id"],
        controller=controller,
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert result["code"] == "SMART_FINAL_SUBMIT_CAPTURED_SUCCESS_RECONCILED"
    assert controller.snapshot_calls == 1
    assert result["confirmation_url"] == success_url
    assert result["submission_id"] == ""
    assert persisted["status"] == "submitted"
    assert persisted["workflow_status"] == "monitoring"
    assert persisted["worker_status"] == "monitoring"
    assert persisted["next_action"] == smart.EMAIL_FIRST_MONITORING_NEXT
    assert "pending_submission_confirmation_evidence" not in persisted
    assert persisted["last_live_submission_confirmation_evidence"]["evidence_path"] == capture["evidence_path"]
    assert persisted["final_submit_authorization"]["result_recorded_at"]
    assert smart._paper_has_prior_trusted_submission_evidence("paper-1", persisted) is True


def test_unknown_result_can_capture_strong_success_marker_on_review_url(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    review_url = (
        "https://submission.springernature.com/new/submission/"
        "DRAFT-UNCHANGED-PATH-1/review"
    )
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-unchanged-path-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "submission_system": "Springer Nature / SNAPP",
            "target_journal": "Example Journal",
            "paper_dir": str(package_dir.parent),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-UNCHANGED-PATH-1",
            "draft_url": review_url,
            "upload_url": review_url,
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    authorized = smart.authorize_final_submit_automatically(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-unchanged-path-1",
        activate=False,
    )
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-unchanged-path-1",
        route_revision=2,
        attempt_id="attempt-unchanged-path-1",
    )
    smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-unchanged-path-1",
        thread_id="thread-unchanged-path-1",
        authorization_id=authorized["authorization_id"],
    )
    smart.record_final_submit_result_unknown(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-unchanged-path-1",
        thread_id="thread-unchanged-path-1",
        authorization_id=authorized["authorization_id"],
        detail="The final action completed but the portal kept its review URL.",
    )

    capture = smart.capture_live_submission_confirmation(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-unchanged-path-1",
        thread_id="thread-unchanged-path-1",
        authorization_id=authorized["authorization_id"],
        transport=FakeConfirmationTransport(
            url=review_url,
            manuscript_id="",
            confirmation_marker="manuscript_submitted",
            page_path="/new/submission/DRAFT-UNCHANGED-PATH-1/review",
        ),
        allow_unknown_recovery=True,
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert capture["code"] == "SMART_SUBMISSION_CONFIRMATION_CAPTURED"
    assert capture["confirmation_url"] == review_url
    assert capture["manuscript_id"] == ""
    assert isinstance(persisted["pending_submission_confirmation_evidence"], dict)


@pytest.mark.parametrize("terminal_status", ["submitted", "monitoring", "accepted"])
def test_first_terminal_worker_result_without_authorization_is_rejected_without_state_write(
    guarded_workspace: dict[str, Path],
    terminal_status: str,
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
            "paper_dir": str(package_dir.parent),
            "source_package_dir": str(package_dir),
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-1",
        route_revision=2,
        attempt_id="attempt-1",
    )
    before = smart.SMART_STATE_PATH.read_bytes()

    with pytest.raises(smart.SmartSubmissionGuardError) as caught:
        smart.record_visible_worker_result(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-1",
            status=terminal_status,
            submission_id="MS-2026-123",
            confirmation_url="https://submission.springernature.com/confirm/MS-2026-123",
            activate=False,
        )

    assert caught.value.code == smart.SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED
    assert smart.SMART_STATE_PATH.read_bytes() == before


def test_atomic_dispatch_revalidates_session_after_prior_auth_validation(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=1)
    write_valid_canary_inputs(guarded_workspace)
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "ready_for_online_submit",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
        }
    }
    state["online_queue"] = ["paper-1"]
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    initial = smart._require_current_worker_auth("paper-1", state["papers"]["paper-1"])
    assert initial.state == "live_browser_session"

    credentials = json.loads(smart.SMART_CREDENTIAL_PATH.read_text(encoding="utf-8"))
    expired = datetime.now().astimezone() - timedelta(minutes=20)
    credentials["accounts"][0]["browser_session_evidence"]["checked_at"] = expired.isoformat()
    credentials["accounts"][0]["browser_session_evidence"]["expires_at"] = (
        expired + timedelta(minutes=5)
    ).isoformat()
    smart.write_json_atomic(smart.SMART_CREDENTIAL_PATH, credentials)
    before = smart.SMART_STATE_PATH.read_bytes()

    with pytest.raises(smart.SmartSubmissionGuardError) as caught:
        smart.record_visible_worker_dispatch(
            "paper-1",
            thread_id="thread-1",
            route_revision=1,
            attempt_id="attempt-1",
        )

    assert caught.value.code == smart.SMART_SECRET_BROKER_REQUIRED
    assert smart.SMART_STATE_PATH.read_bytes() == before


def test_legacy_owned_direct_worker_cli_is_rejected(
    guarded_workspace: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_route_state(guarded_workspace)

    exit_code = main(
        [
            "smart-submission-worker-prompt",
            "paper-1",
            "--route-revision",
            "1",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["code"] == smart.SMART_ROUTE_OWNER_REQUIRED
    assert not smart.SMART_STATE_PATH.exists()


def test_wrong_worker_revision_rejected_before_state_write(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=4)
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    smart.write_json_atomic(
        smart.SMART_STATE_PATH,
        {
            **smart._default_state(),
            "papers": {"paper-1": {"paper_id": "paper-1", "status": "uploading"}},
            "online_queue": ["paper-1"],
        },
    )
    before = smart.SMART_STATE_PATH.read_bytes()

    with pytest.raises(smart.SmartSubmissionGuardError) as prompt_error:
        smart.build_visible_worker_prompt("paper-1", route_revision=3)
    with pytest.raises(smart.SmartSubmissionGuardError) as result_error:
        smart.record_visible_worker_result(
            "paper-1",
            route_revision=3,
            attempt_id="attempt-1",
            thread_id="thread-1",
            status="revision_needed",
            activate=False,
        )

    assert prompt_error.value.code == smart.SMART_ROUTE_REVISION_MISMATCH
    assert result_error.value.code == smart.SMART_ROUTE_REVISION_MISMATCH
    assert smart.SMART_STATE_PATH.read_bytes() == before


def test_preflight_failure_keeps_route_unchanged(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    smart.SMART_CREDENTIAL_PATH.unlink()
    before = guarded_workspace["unified"].read_bytes()
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])

    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
        activate=False,
    )

    assert result["code"] == smart.SMART_CANARY_ACCOUNT_MISSING
    assert guarded_workspace["unified"].read_bytes() == before
    assert orchestrator.calls == []


def test_queue_failure_cas_rolls_back_original_route(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    smart_before = smart.SMART_STATE_PATH.read_bytes()
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])

    def fail_queue(_prepared: dict[str, object], *, route_revision: int) -> dict[str, object]:
        raise RuntimeError(f"injected queue failure at revision {route_revision}")

    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
        enqueue_callback=fail_queue,
        activate=False,
    )

    persisted = json.loads(guarded_workspace["unified"].read_text(encoding="utf-8"))
    route = persisted["submission_routes"]["paper-1"]
    assert result["code"] == smart.SMART_CANARY_QUEUE_FAILED
    assert route["execution_owner"] == "legacy_auto"
    assert route["mode"] == "production"
    assert route["revision"] == 3
    assert smart.SMART_STATE_PATH.read_bytes() == smart_before
    assert len(orchestrator.calls) == 2
    assert Path(result["details"]["audit_path"]).is_file()


def test_successful_single_paper_canary(guarded_workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])
    monkeypatch.setattr(
        smart,
        "set_smart_submission_automation_fast_wakeup",
        lambda enabled: {"enabled": enabled, "test_only": True},
    )

    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
    )

    unified = json.loads(guarded_workspace["unified"].read_text(encoding="utf-8"))
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert result["ok"] is True, result.get("details", {}).get("error", result)
    assert result["code"] == "SMART_CANARY_QUEUED"
    assert unified["submission_routes"]["paper-1"] == {
        "execution_owner": "smart",
        "mode": "canary",
        "revision": 2,
    }
    assert persisted["online_queue"] == ["paper-1"]
    assert persisted["papers"]["paper-1"]["route_revision"] == 2
    assert persisted["papers"]["paper-1"]["attempt_id"].startswith("canary-")
    assert persisted["settings"]["max_parallel_workers"] == 1
    assert result["wakeup"]["test_only"] is True


def test_direct_start_cli_uses_same_single_canary_api(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])
    monkeypatch.setattr(cli, "UnifiedOrchestrator", lambda: orchestrator)
    monkeypatch.setattr(
        smart,
        "set_smart_submission_automation_fast_wakeup",
        lambda enabled: {"enabled": enabled, "test_only": True},
    )

    exit_code = cli.main(
        [
            "smart-submission-start-visible-online",
            "paper-1",
            "--expected-route-revision",
            "1",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["code"] == "SMART_CANARY_QUEUED"
    assert smart.read_json(smart.SMART_STATE_PATH, {})["online_queue"] == ["paper-1"]


def test_daily_sweep_filters_non_smart_route(guarded_workspace: dict[str, Path]) -> None:
    unified = {
        "papers": {"paper-smart": {}, "paper-legacy": {}},
        "submission_routes": {
            "paper-smart": {"execution_owner": "smart", "mode": "canary", "revision": 2},
            "paper-legacy": {"execution_owner": "legacy_auto", "mode": "production", "revision": 1},
        },
    }
    guarded_workspace["unified"].write_text(json.dumps(unified), encoding="utf-8")
    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["papers"] = {
        paper_id: {
            "paper_id": paper_id,
            "status": "ready_for_online_submit",
            "target_journal": "Example Journal",
            "submission_system": "Springer Nature / SNAPP",
            "submission_system_id": "springer_nature",
        }
        for paper_id in ("paper-smart", "paper-legacy")
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep()

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert result["online_queued"] == ["paper-smart"]
    assert result["skipped"]["paper-legacy"] == smart.SMART_ROUTE_OWNER_REQUIRED
    assert persisted["online_queue"] == ["paper-smart"]


def test_read_only_monitor_sweep_keeps_existing_legacy_route(
    guarded_workspace: dict[str, Path],
) -> None:
    unified = {
        "papers": {"paper-legacy": {}},
        "submission_routes": {
            "paper-legacy": {
                "execution_owner": "legacy_auto",
                "mode": "production",
                "revision": 3,
            },
        },
    }
    guarded_workspace["unified"].write_text(json.dumps(unified), encoding="utf-8")
    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["papers"] = {
        "paper-legacy": {
            "paper_id": "paper-legacy",
            "status": "submitted",
            "workflow_status": "monitoring",
            "target_journal": "Example Journal",
            "submission_system": "Springer Nature / SNAPP",
            "submission_system_id": "springer_nature",
            "legacy_submission_status": {
                "status": "submitted",
                "raw_status": "submitted_active",
            },
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep(
        monitoring_only=True,
        force_monitor=True,
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"]["paper-legacy"]
    assert result["monitor_check_ids"] == ["paper-legacy"]
    assert result["online_queued"] == ["paper-legacy"]
    assert persisted["online_queue"] == ["paper-legacy"]
    assert paper["monitor_check_pending"] is True
    assert paper["monitor_route_scope"] == smart.SMART_READ_ONLY_MONITOR_ROUTE_SCOPE
    assert paper["monitor_route_owner"] == "legacy_auto"
    assert paper["route_revision"] == 3
    assert paper.get("execution_owner") != "smart"


def test_read_only_monitor_sweep_includes_submitted_status_not_in_default_set(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "submitted",
            "workflow_status": "monitoring",
            "target_journal": "Example Journal",
            "submission_system": "Springer Nature / SNAPP",
            "submission_system_id": "springer_nature",
            "submission_id": "EXAMPLE-123",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep(
        monitoring_only=True,
        force_monitor=True,
    )

    assert result["monitor_check_ids"] == ["paper-1"]
    assert "paper-1" not in result["skipped"]


def test_legacy_route_monitor_dispatch_and_result_remain_read_only(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(
        guarded_workspace,
        owner="legacy_auto",
        mode="production",
        revision=3,
    )
    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "monitoring",
            "workflow_status": "queued",
            "worker_status": "queued",
            "monitor_check_pending": True,
            "attempt_id": "monitor-attempt-1",
            "route_revision": 3,
            "target_journal": "Example Journal",
            "submission_system": "Springer Nature / SNAPP",
            "submission_system_id": "springer_nature",
            "legacy_submission_status": {
                "status": "submitted",
                "raw_status": "submitted_active",
            },
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    monkeypatch.setattr(
        smart,
        "_require_current_worker_auth",
        lambda _paper_id, _paper: object(),
    )

    dispatch = smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-monitor-1",
        route_revision=3,
        attempt_id="monitor-attempt-1",
    )
    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=3,
        attempt_id="monitor-attempt-1",
        status="monitoring",
        next_action=smart.EMAIL_FIRST_MONITORING_NEXT,
        detail="No new journal action was found.",
        thread_id="thread-monitor-1",
        source="scheduled_monitor_dashboard",
        activate=False,
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    paper = persisted["papers"]["paper-1"]
    assert dispatch["code"] == "SMART_CANARY_WORKER_DISPATCHED"
    assert result["monitor_check_completed"] is True
    assert result["removed_from_online_queue"] is True
    assert paper["status"] == "monitoring"
    assert paper["workflow_status"] == "monitoring"
    assert paper["monitor_route_scope"] == smart.SMART_READ_ONLY_MONITOR_ROUTE_SCOPE
    assert paper["monitor_route_owner"] == "legacy_auto"
    assert paper.get("execution_owner") != "smart"
    assert "final_submit_confirmed" not in paper


def test_mailbox_batch_shares_one_read_only_thread_and_one_worker_slot(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(
        guarded_workspace,
        owner="legacy_auto",
        mode="production",
        revision=3,
    )
    unified = smart.read_json(guarded_workspace["unified"], {})
    unified["papers"]["paper-2"] = copy.deepcopy(unified["papers"]["paper-1"])
    unified["papers"]["paper-2"]["paper_id"] = "paper-2"
    unified["submission_routes"]["paper-2"] = copy.deepcopy(
        unified["submission_routes"]["paper-1"]
    )
    smart.write_json_atomic(guarded_workspace["unified"], unified)
    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["settings"]["max_parallel_workers"] = 1
    state["online_queue"] = ["paper-1", "paper-2"]
    state["papers"] = {}
    for index, paper_id in enumerate(state["online_queue"], start=1):
        state["papers"][paper_id] = {
            "paper_id": paper_id,
            "status": "monitoring",
            "workflow_status": "queued",
            "worker_status": "queued",
            "monitor_check_pending": True,
            "attempt_id": f"monitor-attempt-{index}",
            "route_revision": 3,
            "legacy_submission_status": {
                "status": "submitted",
                "raw_status": "submitted_active",
            },
        }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    def unexpected_journal_auth(*_args, **_kwargs):
        raise AssertionError("mailbox batch must not request journal-site credentials")

    monkeypatch.setattr(smart, "_require_current_worker_auth", unexpected_journal_auth)
    first = smart.record_mailbox_monitor_worker_dispatch(
        "paper-1",
        thread_id="thread-mailbox-batch",
        route_revision=3,
        attempt_id="monitor-attempt-1",
    )
    second = smart.record_mailbox_monitor_worker_dispatch(
        "paper-2",
        thread_id="thread-mailbox-batch",
        route_revision=3,
        attempt_id="monitor-attempt-2",
    )

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert first["monitor_channel"] == smart.SMART_MONITOR_CHANNEL_MAILBOX_BATCH
    assert second["monitor_channel"] == smart.SMART_MONITOR_CHANNEL_MAILBOX_BATCH
    assert persisted["papers"]["paper-1"]["workflow_status"] == "running"
    assert persisted["papers"]["paper-2"]["workflow_status"] == "running"
    assert persisted["papers"]["paper-1"]["thread_id"] == "thread-mailbox-batch"
    assert persisted["papers"]["paper-2"]["thread_id"] == "thread-mailbox-batch"


def test_dispatch_claim_and_status_ignore_stale_aggregate(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    write_valid_canary_inputs(guarded_workspace)
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["active_worker_count"] = 12
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "ready_for_online_submit",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
            "target_journal": "Example Journal",
            "submission_system": "Springer Nature / SNAPP",
            "submission_system_id": "springer_nature",
            "credential_account_id": "springer-edge-vault",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    dispatch = smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-1",
        route_revision=2,
        attempt_id="attempt-1",
    )
    status = smart.smart_submission_status()

    assert dispatch["code"] == "SMART_CANARY_WORKER_DISPATCHED"
    assert status["active_worker_count"] == 1
    assert status["persisted_active_worker_count_ignored"] == 12
    assert status["online_running_papers"] == ["paper-1"]


def test_legacy_owned_import_never_enters_smart_local_queue(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    unified = json.loads(guarded_workspace["unified"].read_text(encoding="utf-8"))

    result = smart.import_from_unified_state(unified, selected_ids=["paper-1"])

    assert result["imported"] == []
    assert result["skipped"]["paper-1"] == smart.SMART_ROUTE_OWNER_REQUIRED
    assert "paper-1" not in result["state"]["paper_queue"]
    assert "paper-1" not in result["state"]["papers"]


def test_snapshot_failure_happens_before_route_switch(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    before = guarded_workspace["unified"].read_bytes()
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])

    def fail_snapshot():
        raise RuntimeError("injected snapshot failure")

    monkeypatch.setattr(smart, "_read_smart_state_snapshot", fail_snapshot)
    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
        activate=False,
    )

    assert result["code"] == smart.SMART_CANARY_QUEUE_FAILED
    assert result["details"]["stage"] == "smart_state_snapshot"
    assert orchestrator.calls == []
    assert guarded_workspace["unified"].read_bytes() == before


def test_terminal_evidence_is_rechecked_at_commit_and_route_is_restored(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    smart_before = smart.SMART_STATE_PATH.read_bytes()
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])
    original_set = orchestrator.set_submission_route

    def set_route_with_terminal_evidence(*args, **kwargs):
        result = original_set(*args, **kwargs)
        if kwargs.get("execution_owner") == "smart":
            guarded_workspace["legacy"].write_text(
                json.dumps({"papers": {"paper-1": {"manuscript_id": "MS-LATE"}}}),
                encoding="utf-8",
            )
        return result

    orchestrator.set_submission_route = set_route_with_terminal_evidence  # type: ignore[method-assign]
    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
        activate=False,
    )

    route = json.loads(guarded_workspace["unified"].read_text(encoding="utf-8"))["submission_routes"]["paper-1"]
    assert result["code"] == smart.SMART_CANARY_TERMINAL_EVIDENCE
    assert route["execution_owner"] == "legacy_auto"
    assert route["mode"] == "production"
    assert smart.SMART_STATE_PATH.read_bytes() == smart_before


def test_rollback_hash_cas_preserves_unknown_smart_write(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])

    def concurrent_write_then_fail(_prepared: dict[str, object], *, route_revision: int) -> dict[str, object]:
        state = smart._default_state()
        state["external_marker"] = f"writer-at-{route_revision}"
        smart.write_json_atomic(smart.SMART_STATE_PATH, state)
        raise RuntimeError("injected callback failure")

    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
        enqueue_callback=concurrent_write_then_fail,
        activate=False,
    )

    assert result["code"] == smart.SMART_CANARY_ROUTE_ROLLBACK_FAILED
    assert result["details"]["smart_state_restore_error"] == "SMART_STATE_CAS_CONFLICT"
    assert smart.read_json(smart.SMART_STATE_PATH, {})["external_marker"] == "writer-at-2"


def test_attempt_and_thread_binding_rejects_old_worker(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    write_valid_canary_inputs(guarded_workspace)
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-1",
        route_revision=2,
        attempt_id="attempt-1",
    )
    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        status="revision_needed",
        activate=False,
    )
    assert result["next_attempt_id"]
    assert result["next_attempt_id"] != "attempt-1"

    before = smart.SMART_STATE_PATH.read_bytes()
    with pytest.raises(smart.SmartSubmissionGuardError) as dispatch_error:
        smart.record_visible_worker_dispatch(
            "paper-1",
            thread_id="thread-old",
            route_revision=2,
            attempt_id="attempt-1",
        )
    with pytest.raises(smart.SmartSubmissionGuardError) as result_error:
        smart.record_visible_worker_result(
            "paper-1",
            route_revision=2,
            attempt_id="attempt-1",
            thread_id="thread-1",
            status="revision_needed",
            activate=False,
        )
    assert dispatch_error.value.code == smart.SMART_CANARY_ATTEMPT_MISMATCH
    assert result_error.value.code == smart.SMART_CANARY_ATTEMPT_MISMATCH
    assert smart.SMART_STATE_PATH.read_bytes() == before


def test_worker_result_is_value_free_before_state_audit_and_prompt_reuse(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    write_valid_canary_inputs(guarded_workspace)
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
            "credential_account_id": "springer-edge-vault",
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    smart.record_visible_worker_dispatch(
        "paper-1",
        thread_id="thread-1",
        route_revision=2,
        attempt_id="attempt-1",
    )
    sentinel = "SENTINEL_WORKER_SECRET"

    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        status="blocked",
        next_action=f"final submit after credential_ref=vault:{sentinel}",
        detail=f"password={sentinel}",
        source=f"password:{sentinel}",
        activate=False,
    )

    state_text = smart.SMART_STATE_PATH.read_text(encoding="utf-8")
    audit_text = Path(result["audit_path"]).read_text(encoding="utf-8")
    assert sentinel not in state_text
    assert sentinel not in audit_text
    assert "credential_ref" not in state_text
    assert "password=" not in audit_text


def test_worker_confirmation_url_drops_query_fragment_and_rejects_secret_id(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    package_dir = write_valid_canary_inputs(guarded_workspace)
    state = smart._default_state()
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "waiting",
            "workflow_status": "waiting",
            "worker_status": "waiting",
            "attempt_id": "attempt-1",
            "submission_system_id": "springer_nature",
            "target_journal": "Example Journal",
            "credential_account_id": "springer-edge-vault",
            "paper_dir": str(package_dir.parent),
            "source_package_dir": str(package_dir),
            "next_action": smart.FINAL_SUBMIT_REVIEW_READY_ACTION,
            "draft_id": "DRAFT-READY-1",
            "draft_url": "https://submission.springernature.com/draft/DRAFT-READY-1",
            "upload_url": "https://submission.springernature.com/draft/DRAFT-READY-1/files",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
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
        thread_id="thread-1",
        route_revision=2,
        attempt_id="attempt-1",
    )
    smart.claim_final_submit_action(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        authorization_id=authorized["authorization_id"],
    )
    capture = smart.capture_live_submission_confirmation(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        authorization_id=authorized["authorization_id"],
        transport=FakeConfirmationTransport(
            url="https://submission.springernature.com/confirm/MS-2026-123?token=SENTINEL#password=SENTINEL"
        ),
    )

    result = smart.record_visible_worker_result(
        "paper-1",
        route_revision=2,
        attempt_id="attempt-1",
        thread_id="thread-1",
        status="submitted",
        submission_id="MS-2026-123",
        evidence_path=capture["evidence_path"],
        confirmation_url="https://submission.springernature.com/confirm/MS-2026-123?token=SENTINEL#password=SENTINEL",
        activate=False,
    )
    persisted = smart.read_json(smart.SMART_STATE_PATH, {})["papers"]["paper-1"]
    assert persisted["confirmation_url"] == "https://submission.springernature.com/confirm/MS-2026-123"
    assert "SENTINEL" not in smart.SMART_STATE_PATH.read_text(encoding="utf-8")
    assert "SENTINEL" not in Path(result["audit_path"]).read_text(encoding="utf-8")

    # The strict id format is checked before any later result can be persisted.
    with pytest.raises(ValueError, match="submission_id"):
        smart._sanitize_submission_id("password=SENTINEL")
    for encoded_path in ("/%70assword%3DSENTINEL", "/%74oken%3DSENTINEL"):
        with pytest.raises(ValueError, match="confirmation_url"):
            smart._sanitize_confirmation_url(
                f"https://submission.springernature.com{encoded_path}",
                paper={
                    "paper_id": "paper-1",
                    "submission_system_id": "springer_nature",
                    "target_journal": "Example Journal",
                },
            )


def test_daily_sweep_does_not_rotate_running_attempt(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "uploading",
            "workflow_status": "running",
            "worker_status": "running",
            "thread_id": "thread-1",
            "attempt_id": "attempt-1",
            "target_journal": "Example Journal",
            "submission_system": "Springer Nature / SNAPP",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)

    result = smart.queue_smart_submission_daily_sweep()

    persisted = smart.read_json(smart.SMART_STATE_PATH, {})
    assert result["skipped"]["paper-1"] == "worker is currently running"
    assert persisted["papers"]["paper-1"]["attempt_id"] == "attempt-1"
    assert persisted["papers"]["paper-1"]["workflow_status"] == "running"


def test_raw_byte_snapshot_is_restored_after_post_commit_failure(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    compact = json.dumps(smart._default_state(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    original_raw = b"\xef\xbb\xbf" + compact
    smart.SMART_STATE_PATH.write_bytes(original_raw)
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(smart, "_write_canary_route_audit", fail_audit)
    result = smart.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=orchestrator,
        activate=False,
    )

    assert result["code"] == smart.SMART_CANARY_QUEUE_FAILED
    assert smart.SMART_STATE_PATH.read_bytes() == original_raw


def test_incomplete_route_transaction_is_recovered_by_receipt(guarded_workspace: dict[str, Path]) -> None:
    write_route_state(guarded_workspace)
    write_valid_canary_inputs(guarded_workspace)
    orchestrator = FakeRouteOrchestrator(guarded_workspace["unified"])
    state_existed, _state, snapshot, state_hash = smart._read_smart_state_snapshot()
    transaction_id = "smart-canary-txn-recovery"
    original_route = {"execution_owner": "legacy_auto", "mode": "production", "revision": 1}
    smart._write_canary_transaction_journal(
        {
            "version": 1,
            "transaction_id": transaction_id,
            "paper_id": "paper-1",
            "phase": "route_switched",
            "original_route": original_route,
            "switched_revision": 2,
            "state_existed": state_existed,
            "smart_hash_before": state_hash,
        },
        snapshot,
    )
    orchestrator.set_submission_route(
        "paper-1",
        execution_owner="smart",
        mode="canary",
        expected_revision=1,
        operation_id=transaction_id,
    )

    recovery = smart._recover_incomplete_canary_transaction(orchestrator)

    route = json.loads(guarded_workspace["unified"].read_text(encoding="utf-8"))["submission_routes"]["paper-1"]
    assert recovery["ok"] is True
    assert recovery["action"] == "rolled_back_orphan_smart_route"
    assert route["execution_owner"] == "legacy_auto"
    assert route["mode"] == "production"
    assert not smart._canary_transaction_paths()[0].exists()


def test_dispatch_rechecks_terminal_evidence_and_terminal_like_state(
    guarded_workspace: dict[str, Path],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    guarded_workspace["smart"].mkdir(parents=True, exist_ok=True)
    state = smart._default_state()
    state["online_queue"] = ["paper-1"]
    state["papers"] = {
        "paper-1": {
            "paper_id": "paper-1",
            "status": "uploading",
            "workflow_status": "queued",
            "worker_status": "queued",
            "attempt_id": "attempt-1",
        }
    }
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    guarded_workspace["legacy"].write_text(
        json.dumps({"papers": {"paper-1": {"manuscript_id": "MS-LATE"}}}),
        encoding="utf-8",
    )
    with pytest.raises(smart.SmartSubmissionGuardError) as evidence_error:
        smart.record_visible_worker_dispatch(
            "paper-1",
            thread_id="thread-1",
            route_revision=2,
            attempt_id="attempt-1",
        )
    assert evidence_error.value.code == smart.SMART_CANARY_TERMINAL_EVIDENCE

    guarded_workspace["legacy"].write_text(json.dumps({"papers": {}}), encoding="utf-8")
    state["papers"]["paper-1"]["workflow_status"] = "submitted"
    smart.write_json_atomic(smart.SMART_STATE_PATH, state)
    with pytest.raises(smart.SmartSubmissionGuardError) as terminal_like_error:
        smart.record_visible_worker_dispatch(
            "paper-1",
            thread_id="thread-1",
            route_revision=2,
            attempt_id="attempt-1",
        )
    assert terminal_like_error.value.code == smart.SMART_CANARY_UNVERIFIED_TERMINAL


def test_worker_cli_validation_error_is_structured(
    guarded_workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_route_state(guarded_workspace, owner="smart", mode="canary", revision=2)
    monkeypatch.setattr(cli, "UnifiedOrchestrator", lambda: FakeRouteOrchestrator(guarded_workspace["unified"]))

    exit_code = cli.main(
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
            "not-a-status",
            "--no-activate",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["code"] == "SMART_CLI_VALIDATION_ERROR"
