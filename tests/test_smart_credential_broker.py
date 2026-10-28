from __future__ import annotations

import json
from pathlib import Path

import unified_paper_orchestrator.smart_credential_broker as broker_module
from unified_paper_orchestrator.account_site_store import RecordNotFoundError
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.smart_credential_broker import (
    BROKER_SESSION_READY,
    TrustedCredentialBroker,
)
from unified_paper_orchestrator.submission_site_adapters import (
    INDERSCIENCE_ADAPTER,
    SCHOLAR_ONE_ADAPTER,
    SPRINGER_NATURE_ADAPTER,
    WILEY_ADAPTER,
)


class FakeSigner:
    def sign(self, evidence):
        return {**dict(evidence), "signature": "a" * 64}


class FakeStore:
    def __init__(self, site_registry_path: Path) -> None:
        self.site_registry_path = site_registry_path
        self.accounts: dict[str, dict] = {}
        self.secrets: dict[str, str] = {}
        self.external_accounts: dict[str, dict] = {}
        self.external_secrets: dict[str, str] = {}
        self.recorded: list[dict] = []
        self.fail_record_once = False

    def get_account(self, record_key: str):
        if record_key in self.external_accounts:
            return {
                **dict(self.external_accounts[record_key]),
                "record_key": record_key,
                "auto_fill_ready": record_key in self.external_secrets,
            }
        account_id = record_key.removeprefix("smart:")
        if account_id not in self.accounts:
            raise RecordNotFoundError("missing")
        return dict(self.accounts[account_id])

    def reveal_password(self, record_key: str) -> str:
        if record_key in self.external_secrets:
            return self.external_secrets[record_key]
        return self.secrets[record_key.removeprefix("smart:")]

    def list_account_summaries(self, query="", scope="all", source="all"):
        rows = []
        for record_key, account in self.external_accounts.items():
            if scope not in {"", "all", account.get("scope")}:
                continue
            combined = " ".join(
                str(account.get(key) or "")
                for key in ("provider", "login_url", "username")
            ).casefold()
            if query and str(query).casefold() not in combined:
                continue
            rows.append(
                {
                    **dict(account),
                    "record_key": record_key,
                    "auto_fill_ready": record_key in self.external_secrets,
                }
            )
        return rows

    def create_smart_account_with_secret(self, record, *, password):
        account_id = record["account_id"]
        self.accounts[account_id] = dict(record)
        self.secrets[account_id] = password
        return dict(record)

    def create_smart_session_account(self, record):
        account_id = record["account_id"]
        self.accounts[account_id] = dict(record)
        return dict(record)

    def create_smart_account_with_credential_reference(
        self,
        record,
        *,
        credential_record_key,
    ):
        account_id = record["account_id"]
        self.accounts[account_id] = dict(record)
        self.secrets[account_id] = self.external_secrets[credential_record_key]
        return dict(record)

    def update_smart_account_recovered_secret(self, account_id, *, password):
        self.secrets[account_id] = password
        self.accounts[account_id]["status"] = "ready"
        return dict(self.accounts[account_id])

    def update_smart_account_recovered_username(self, account_id, *, username):
        self.accounts[account_id]["username"] = username
        return dict(self.accounts[account_id])

    def mark_smart_account_registration_ready(self, account_id):
        self.accounts[account_id]["status"] = "ready"
        return dict(self.accounts[account_id])

    def record_browser_session_evidence(
        self,
        account_id,
        system_id,
        expected_origin,
        evidence,
        *,
        signer,
    ):
        if self.fail_record_once:
            self.fail_record_once = False
            raise RuntimeError("transient redirect/store race")
        self.recorded.append(
            {
                "account_id": account_id,
                "system_id": system_id,
                "origin": expected_origin,
                "evidence": dict(evidence),
            }
        )
        return {"ok": True}


class FakeController:
    def __init__(self, snapshots: list[dict]) -> None:
        self.snapshots = [dict(item) for item in snapshots]
        self.last = dict(snapshots[-1])
        self.trusted_values: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.selected: list[tuple[str, str]] = []
        self.navigated: list[str] = []
        self.registration_preflight_result: dict = {}

    def list_pages(self, allowed_origins):
        current = self.snapshots[0] if self.snapshots else self.last
        return [
            {
                "page_id": "page-1",
                "origin": current["origin"],
                "cdp_port": 9224,
            }
        ]

    def snapshot(self, allowed_origins, *, page_id, **kwargs):
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return dict(self.last)

    def fill_trusted_secret(
        self,
        allowed_origins,
        ref,
        secret,
        *,
        page_id,
        **kwargs,
    ):
        self.trusted_values.append(secret)
        return {"ok": True, "value_free": True}

    def fill(self, allowed_origins, ref, value, *, page_id):
        self.filled.append((ref, value))
        return {"ok": True}

    def select(self, allowed_origins, ref, value, *, page_id):
        self.selected.append((ref, value))
        return {"ok": True}

    def click(self, allowed_origins, ref, *, page_id, **kwargs):
        self.clicked.append(ref)
        return {"ok": True}

    def submit_login_form(self, allowed_origins, ref, *, page_id):
        self.clicked.append(f"login-form:{ref}")
        return {"ok": True, "value_free": True}

    def inspect_control(self, allowed_origins, ref, *, page_id):
        markers = {
            "upo-0001": "first-name",
            "upo-0002": "last-name",
        }
        return {"ok": True, "ref": ref, "marker": markers.get(ref, ref)}

    def navigate(self, allowed_origins, url, *, page_id, **kwargs):
        self.navigated.append(str(url))
        return {"ok": True, "page_id": page_id}

    def registration_preflight(self, allowed_origins, *, page_id):
        return {
            "ok": True,
            "email_duplicate": False,
            "username_duplicate": False,
            "captcha_present": False,
            "captcha_completed": False,
            **dict(self.registration_preflight_result),
        }


class TargetLockedOpeningController:
    def __init__(self) -> None:
        self.opened_urls: list[str] = []

    def list_pages(self, _allowed_origins):
        return [
            {
                "page_id": "other-journal",
                "origin": "https://mc.manuscriptcentral.com:443",
                "url": "https://mc.manuscriptcentral.com/another-journal",
                "cdp_port": 9224,
            }
        ]

    def open_page(self, _allowed_origins, url, *, preferred_port):
        self.opened_urls.append(str(url))
        return {"page_id": "jaise", "cdp_port": preferred_port}

    def snapshot(self, _allowed_origins, *, page_id, **_kwargs):
        return {
            "page_id": page_id,
            "cdp_port": 9224,
            "origin": "https://mc.manuscriptcentral.com:443",
            "url": "https://mc.manuscriptcentral.com/jaise",
            "text": "Cloudflare verification",
            "controls": [],
        }


def _site_registry(path: Path) -> None:
    write_json_atomic(
        path,
        {
            "systems": [
                {
                    "system_id": "springer_nature",
                    "status": "discovered",
                    "login_url": "https://submission.springernature.com/",
                    "submission_url": "https://submission.springernature.com/",
                }
            ],
            "journals": [
                {
                    "system_id": "springer_nature",
                    "journal_name": "The Journal of Supercomputing",
                    "status": "discovered",
                    "submission_url": (
                        "https://submission.springernature.com/"
                        "submission-details/example"
                    ),
                }
            ],
        },
    )


def test_scholar_one_broker_opens_exact_journal_path_not_generic_root(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    write_json_atomic(
        site_path,
        {
            "systems": [
                {
                    "system_id": "scholar_one",
                    "status": "discovered",
                    "login_url": "https://mc.manuscriptcentral.com/",
                    "submission_url": "https://mc.manuscriptcentral.com/",
                }
            ],
            "journals": [
                {
                    "system_id": "scholar_one",
                    "journal_name": (
                        "Journal of Ambient Intelligence and Smart Environments (JAISE)"
                    ),
                    "status": "discovered",
                    "submission_url": "https://mc.manuscriptcentral.com/jaise",
                }
            ],
        },
    )
    controller = TargetLockedOpeningController()
    broker = TrustedCredentialBroker(
        store=FakeStore(site_path),
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    system, journal = broker._site_records(
        "scholar_one",
        "Journal of Ambient Intelligence and Smart Environments",
    )
    start_url = SCHOLAR_ONE_ADAPTER.target_url(system, journal)
    snapshot = broker._open_or_select_page(
        SCHOLAR_ONE_ADAPTER,
        SCHOLAR_ONE_ADAPTER.allowed_origins,
        SCHOLAR_ONE_ADAPTER.allowed_origins[0],
        start_url=start_url,
    )

    assert start_url == "https://mc.manuscriptcentral.com/jaise"
    assert controller.opened_urls == [start_url]
    assert snapshot["page_id"] == "jaise"


def test_wiley_broker_reuses_loaded_redirected_dashboard_journal_page(
    tmp_path: Path,
) -> None:
    class RedirectedWileyController:
        def __init__(self) -> None:
            self.opened_urls: list[str] = []

        def list_pages(self, _allowed_origins):
            return [
                {
                    "page_id": "cpe-dashboard",
                    "origin": "https://authors.wiley.com:443",
                    "url": "https://authors.wiley.com/dashboard/journal/CPE",
                    "cdp_port": 9224,
                }
            ]

        def open_page(self, _allowed_origins, url, *, preferred_port):
            self.opened_urls.append(str(url))
            return {"page_id": "unexpected", "cdp_port": preferred_port}

        def snapshot(self, _allowed_origins, *, page_id, **_kwargs):
            return {
                "page_id": page_id,
                "cdp_port": 9224,
                "origin": "https://authors.wiley.com",
                "url": "https://authors.wiley.com/dashboard/journal/CPE",
                "path": "/dashboard/journal/CPE",
                "text": "我的仪表盘 开始提交 稿件标题或ID",
                "controls": [],
            }

    controller = RedirectedWileyController()
    broker = TrustedCredentialBroker(
        store=FakeStore(tmp_path / "sites.json"),
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    snapshot = broker._open_or_select_page(
        WILEY_ADAPTER,
        WILEY_ADAPTER.allowed_origins,
        "https://authors.wiley.com",
        start_url="https://authors.wiley.com/journal/CPE",
    )

    assert snapshot["page_id"] == "cpe-dashboard"
    assert controller.opened_urls == []
    assert WILEY_ADAPTER.classify_page(snapshot) == "authenticated"


def test_wiley_broker_uses_bound_federated_final_review_page(
    tmp_path: Path,
) -> None:
    final_url = (
        "https://submission.wiley.com/submission/submissionBoard/"
        "45c19a2c-7910-4173-8f3f-029248569c8f/finalReview"
    )

    class WileyFinalReviewController:
        def __init__(self) -> None:
            self.opened_urls: list[str] = []

        def list_pages(self, _allowed_origins):
            return [
                {
                    "page_id": "wiley-final-review",
                    "origin": "https://submission.wiley.com:443",
                    "url": final_url,
                    "cdp_port": 9224,
                }
            ]

        def open_page(self, _allowed_origins, url, *, preferred_port):
            self.opened_urls.append(str(url))
            return {"page_id": "unexpected", "cdp_port": preferred_port}

        def snapshot(self, _allowed_origins, *, page_id, **_kwargs):
            return {
                "page_id": page_id,
                "cdp_port": 9224,
                "origin": "https://submission.wiley.com:443",
                "url": final_url,
                "path": (
                    "/submission/submissionBoard/"
                    "45c19a2c-7910-4173-8f3f-029248569c8f/finalReview"
                ),
                "text": "Final review Complete my submission",
                "controls": [],
            }

    site_path = tmp_path / "sites.json"
    write_json_atomic(
        site_path,
        {
            "systems": [
                {
                    "system_id": "wiley",
                    "status": "verified",
                    "login_url": "https://authors.wiley.com/",
                    "submission_url": "https://authors.wiley.com/dashboard",
                }
            ],
            "journals": [
                {
                    "system_id": "wiley",
                    "journal_name": (
                        "Concurrency and Computation: Practice and Experience"
                    ),
                    "status": "verified",
                    "submission_url": "https://authors.wiley.com/journal/CPE",
                }
            ],
        },
    )
    store = FakeStore(site_path)
    store.accounts["wiley-session"] = {
        "account_id": "wiley-session",
        "username": "author@example.test",
        "email": "author@example.test",
    }
    controller = WileyFinalReviewController()
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-one",
        paper={
            "credential_account_id": "wiley-session",
            "submission_system_id": "wiley",
            "target_journal": (
                "Concurrency and Computation: Practice and Experience"
            ),
            "draft_url": final_url,
        },
        candidate={
            "credential_account_id": "wiley-session",
            "submission_system_id": "wiley",
            "target_origin": "https://submission.wiley.com:443",
            "allowed_origins": list(WILEY_ADAPTER.allowed_origins),
        },
    )

    assert result["ok"] is True
    assert result["code"] == BROKER_SESSION_READY
    assert result["origin"] == "https://submission.wiley.com:443"
    assert controller.opened_urls == []
    assert store.recorded[-1]["origin"] == "https://submission.wiley.com:443"


def _password_snapshot() -> dict:
    return {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://idp-personal-authenticator.springernature.com",
        "path": "/gateway/password",
        "text": "Sign in",
        "controls": [
            {
                "ref": "upo-0001",
                "tag": "input",
                "type": "password",
                "label": "Password",
                "sensitive": True,
                "disabled": False,
            },
            {
                "ref": "upo-0002",
                "tag": "button",
                "type": "submit",
                "label": "Sign in",
                "disabled": False,
            },
        ],
    }


def _authenticated_snapshot() -> dict:
    return {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://submission.springernature.com",
        "path": "/your-submissions",
        "text": "Your submissions",
        "controls": [],
    }


def test_saved_password_is_filled_locally_and_never_returned(tmp_path: Path) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    store = FakeStore(site_path)
    store.accounts["springer-one"] = {
        "account_id": "springer-one",
        "username": "author@example.test",
        "email": "author@example.test",
    }
    secret = "SENTINEL-SAVED-PASSWORD"
    store.secrets["springer-one"] = secret
    controller = FakeController([_password_snapshot(), _authenticated_snapshot()])
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-one",
        paper={
            "credential_account_id": "springer-one",
            "submission_system_id": "springer_nature",
            "target_journal": "The Journal of Supercomputing",
        },
        candidate={
            "credential_account_id": "springer-one",
            "submission_system_id": "springer_nature",
            "target_origin": "https://submission.springernature.com",
            "allowed_origins": ["https://submission.springernature.com"],
        },
    )

    assert result["ok"] is True
    assert result["code"] == BROKER_SESSION_READY
    assert controller.trusted_values == [secret]
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(store.recorded)


def test_authenticated_wiley_dashboard_binds_session_without_inventing_password(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    write_json_atomic(site_path, {"systems": [], "journals": []})
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        "## Corresponding Author\n"
        "- Name: Researcher Example\n"
        "- Email: author@xujc.com\n",
        encoding="utf-8",
    )
    snapshot = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://authors.wiley.com",
        "path": "/dashboard",
        "text": "Wiley Authors My Dashboard Manuscript title or ID",
        "controls": [],
    }
    store = FakeStore(site_path)
    broker = TrustedCredentialBroker(
        store=store,
        controller=FakeController([snapshot]),
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker._register_account(
        paper={"paper_dir": str(paper_dir)},
        adapter=WILEY_ADAPTER,
        allowed_origins=WILEY_ADAPTER.allowed_origins,
        snapshot=snapshot,
        system_id="wiley",
        target_journal="Concurrency and Computation: Practice and Experience",
        target_origin="https://authors.wiley.com",
    )

    assert result["session_discovered"] is True
    assert result["registration_created"] is False
    assert result["account_id"].startswith("session-wiley-")
    assert result["account_id"] in store.accounts
    assert result["account_id"] not in store.secrets


def test_inderscience_rejected_password_is_recovered_without_manual_login(
    tmp_path: Path,
    monkeypatch,
) -> None:
    site_path = tmp_path / "sites.json"
    write_json_atomic(
        site_path,
        {
            "systems": [
                {
                    "system_id": "inderscience",
                    "status": "discovered",
                    "login_url": INDERSCIENCE_ADAPTER.login_start_url,
                    "submission_url": INDERSCIENCE_ADAPTER.dashboard_url,
                }
            ],
            "journals": [
                {
                    "system_id": "inderscience",
                    "journal_name": "International Journal of Ad Hoc and Ubiquitous Computing",
                    "status": "discovered",
                }
            ],
        },
    )
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        "Status: READY\nName: Researcher Example\nEmail: author@xujc.com\n",
        encoding="utf-8",
    )
    origin = "https://www.indersciencesubmissions.com"
    recovery_form = {
        "page_id": "page-1",
        "origin": origin,
        "path": "/index.php",
        "text": "Forgot username? Forgot password?",
        "controls": [
            {"ref": "email", "tag": "input", "type": "text", "id": "txtEmail"},
            {"ref": "last", "tag": "input", "type": "text", "id": "txtlastName"},
            {"ref": "user-submit", "tag": "input", "type": "button", "name": "cmdSubmit", "label": "Get Username"},
            {"ref": "user", "tag": "input", "type": "text", "id": "txtUserName"},
            {"ref": "email2", "tag": "input", "type": "text", "id": "txtEmailAdd"},
            {"ref": "password-submit", "tag": "input", "type": "button", "name": "cmdSubmit", "label": "Get Password"},
        ],
    }
    login = {
        "page_id": "page-1",
        "origin": origin,
        "path": "/index.php",
        "text": "Login Username Password",
        "controls": [
            {"ref": "login-user", "tag": "input", "type": "text", "id": "txtUsername"},
            {"ref": "login-password", "tag": "input", "type": "password", "id": "txtPassword"},
            {"ref": "login-submit", "tag": "input", "type": "submit", "name": "cmdSubmit", "label": "Login"},
        ],
    }
    authenticated = {
        "page_id": "page-1",
        "origin": origin,
        "path": "/session/index.php",
        "text": "Author dashboard New submission Log out",
        "controls": [],
    }
    controller = FakeController(
        [recovery_form, login, recovery_form, login, login, authenticated]
    )
    store = FakeStore(site_path)
    account_id = "inderscience-one"
    store.accounts[account_id] = {
        "account_id": account_id,
        "username": "stale-user",
        "email": "author@xujc.com",
    }
    store.secrets[account_id] = "stale-secret"
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )
    monkeypatch.setattr(
        broker,
        "_wait_transition",
        lambda _adapter, allowed, *, page_id, **_kwargs: controller.snapshot(
            allowed,
            page_id=page_id,
        ),
    )
    monkeypatch.setattr(
        broker_module,
        "capture_password_recovery_checkpoint",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        broker_module,
        "wait_for_inderscience_credential_reminder",
        lambda _checkpoint, *, reminder_kind, **_kwargs: (
            "recovered-user"
            if reminder_kind == "username"
            else "RECOVERED-PRIVATE-SECRET"
        ),
    )

    result = broker._complete_inderscience_account_recovery(
        paper={"paper_dir": str(paper_dir)},
        adapter=INDERSCIENCE_ADAPTER,
        allowed_origins=INDERSCIENCE_ADAPTER.allowed_origins,
        snapshot={
            "page_id": "page-1",
            "origin": origin,
            "path": "/index.php",
            "text": "We could not recognise the username and/or password",
            "controls": [],
        },
        account_id=account_id,
        account=store.accounts[account_id],
        system_id="inderscience",
    )

    assert result["snapshot"]["path"] == "/session/index.php"
    assert store.accounts[account_id]["username"] == "recovered-user"
    assert store.secrets[account_id] == "RECOVERED-PRIVATE-SECRET"
    assert controller.trusted_values == ["RECOVERED-PRIVATE-SECRET"]
    assert "RECOVERED-PRIVATE-SECRET" not in json.dumps(result)


def test_inderscience_expired_recovery_falls_back_to_broker_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    site_path = tmp_path / "sites.json"
    write_json_atomic(
        site_path,
        {
            "systems": [
                {
                    "system_id": "inderscience",
                    "status": "discovered",
                    "login_url": INDERSCIENCE_ADAPTER.login_start_url,
                    "submission_url": INDERSCIENCE_ADAPTER.dashboard_url,
                }
            ],
            "journals": [
                {
                    "system_id": "inderscience",
                    "journal_name": "International Journal of Ad Hoc and Ubiquitous Computing",
                    "status": "discovered",
                }
            ],
        },
    )
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        """# Author Information
## Authors
1. Name: Researcher Example
   Affiliation: 1
   Email: author@xujc.com
   Corresponding Author: yes

## Affiliations
1. Institution: Example University
   Country: China

## Corresponding Author
Name: Researcher Example
Email: author@xujc.com
""",
        encoding="utf-8",
    )
    origin = "https://www.indersciencesubmissions.com"
    registration = {
        "page_id": "page-1",
        "origin": origin,
        "path": "/index.php",
        "text": "Register as an author",
        "controls": [
            {"ref": "txtEmail", "tag": "input", "type": "email", "id": "txtEmail"},
            {"ref": "selTitle", "tag": "select", "type": "select", "id": "selTitle", "options": [{"value": "2", "label": "Dr"}]},
            {"ref": "txtFirstName", "tag": "input", "type": "text", "id": "txtFirstName"},
            {"ref": "txtLastName", "tag": "input", "type": "text", "id": "txtLastName"},
            {"ref": "selCountry", "tag": "select", "type": "select", "id": "selCountry", "options": [{"value": "44", "label": "China"}]},
            {"ref": "txtBiography", "tag": "textarea", "type": "textarea", "id": "txtBiography"},
            {"ref": "txtInstitution", "tag": "input", "type": "text", "id": "txtInstitution"},
            {"ref": "txtUserName", "tag": "input", "type": "text", "id": "txtUserName"},
            {"ref": "txtPassword", "tag": "input", "type": "text", "id": "txtPassword", "sensitive": True},
            {"ref": "cmdSubmit", "tag": "input", "type": "submit", "id": "cmdSubmit"},
        ],
    }
    authenticated = {
        "page_id": "page-1",
        "origin": origin,
        "path": "/session/index.php",
        "text": "Author dashboard New submission Log out",
        "controls": [],
    }
    controller = FakeController([registration, authenticated])
    store = FakeStore(site_path)
    old_account_id = "inderscience-stale"
    store.accounts[old_account_id] = {
        "account_id": old_account_id,
        "username": "stale-user",
        "email": "author@xujc.com",
    }
    store.secrets[old_account_id] = "stale-secret"
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )
    monkeypatch.setattr(
        broker,
        "_wait_transition",
        lambda _adapter, allowed, *, page_id, **_kwargs: controller.snapshot(
            allowed,
            page_id=page_id,
        ),
    )
    monkeypatch.setattr(
        broker_module,
        "find_recent_inderscience_credential_reminder",
        lambda *args, **kwargs: "",
    )
    requested_at = broker_module._utc_iso(
        broker_module.datetime.now(broker_module.timezone.utc)
        - broker_module.timedelta(minutes=5)
    )

    result = broker._complete_inderscience_account_recovery(
        paper={
            "paper_dir": str(paper_dir),
            "target_journal": "International Journal of Ad Hoc and Ubiquitous Computing",
            "account_missing_policy": "auto_register",
            "credential_recovery_phase": "username",
            "credential_recovery_requested_at": requested_at,
        },
        adapter=INDERSCIENCE_ADAPTER,
        allowed_origins=INDERSCIENCE_ADAPTER.allowed_origins,
        snapshot={
            "page_id": "page-1",
            "origin": origin,
            "path": "/index.php",
            "text": "We could not recognise the username and/or password",
            "controls": [],
        },
        account_id=old_account_id,
        account=store.accounts[old_account_id],
        system_id="inderscience",
    )

    new_account_id = result["account_id"]
    generated_secret = store.secrets[new_account_id]
    assert new_account_id.startswith("auto-inderscience-")
    assert new_account_id != old_account_id
    assert result["snapshot"]["path"] == "/session/index.php"
    assert 7 <= len(generated_secret) <= 30
    assert any(char.isupper() for char in generated_secret)
    assert any(char.islower() for char in generated_secret)
    assert any(char.isdigit() for char in generated_secret)
    assert set(generated_secret) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_!@#"
    )
    assert controller.trusted_values == [generated_secret]
    assert ("selTitle", "2") in controller.selected
    assert ("selCountry", "44") in controller.selected
    assert any(ref == "txtBiography" and value for ref, value in controller.filled)
    assert generated_secret not in json.dumps(result)


def test_inderscience_optional_alternate_email_is_never_copied() -> None:
    value = TrustedCredentialBroker._registration_field_value(
        "txtAlternateEmail",
        profile={"email": "author@xujc.com"},
        account={
            "email": "author@xujc.com",
            "username": "author_name",
        },
    )

    assert value == ""


def test_inderscience_duplicate_email_stops_before_register_or_captcha(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    store = FakeStore(site_path)
    account_id = "auto-inderscience-test"
    store.accounts[account_id] = {
        "account_id": account_id,
        "username": "author_name",
        "email": "author@xujc.com",
    }
    store.secrets[account_id] = "PRIVATE-LOCAL-SECRET"
    snapshot = {
        "page_id": "page-1",
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/index.php?action=authorreg",
        "text": "Register as a new author",
        "controls": [
            {"ref": "txtEmail", "tag": "input", "type": "email", "id": "txtEmail"},
            {"ref": "txtAlternateEmail", "tag": "input", "type": "email", "id": "txtAlternateEmail"},
            {"ref": "txtUserName", "tag": "input", "type": "text", "id": "txtUserName"},
            {"ref": "txtPassword", "tag": "input", "type": "text", "id": "txtPassword", "sensitive": True},
            {"ref": "cmdSubmit", "tag": "input", "type": "submit", "id": "cmdSubmit", "label": "Register"},
        ],
    }
    controller = FakeController([snapshot])
    controller.registration_preflight_result = {
        "email_duplicate": True,
        "captcha_present": True,
        "captcha_completed": False,
    }
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker._complete_registration(
        adapter=INDERSCIENCE_ADAPTER,
        allowed_origins=INDERSCIENCE_ADAPTER.allowed_origins,
        snapshot=snapshot,
        account_id=account_id,
        account=store.accounts[account_id],
        profile={"email": "author@xujc.com"},
        system_id="inderscience",
    )

    assert "existing_account_snapshot" in result
    assert ("txtAlternateEmail", "") in controller.filled
    assert controller.clicked == []


def test_inderscience_captcha_stops_before_registration_submit(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    store = FakeStore(site_path)
    account_id = "auto-inderscience-test"
    store.accounts[account_id] = {
        "account_id": account_id,
        "username": "author_name",
        "email": "author@xujc.com",
    }
    store.secrets[account_id] = "PRIVATE-LOCAL-SECRET"
    snapshot = {
        "page_id": "page-1",
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/index.php?action=authorreg",
        "text": "Register as a new author",
        "controls": [
            {"ref": "txtEmail", "tag": "input", "type": "email", "id": "txtEmail"},
            {"ref": "txtPassword", "tag": "input", "type": "text", "id": "txtPassword", "sensitive": True},
            {"ref": "cmdSubmit", "tag": "input", "type": "submit", "id": "cmdSubmit", "label": "Register"},
        ],
    }
    controller = FakeController([snapshot])
    controller.registration_preflight_result = {
        "captcha_present": True,
        "captcha_completed": False,
    }
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker._complete_registration(
        adapter=INDERSCIENCE_ADAPTER,
        allowed_origins=INDERSCIENCE_ADAPTER.allowed_origins,
        snapshot=snapshot,
        account_id=account_id,
        account=store.accounts[account_id],
        profile={"email": "author@xujc.com"},
        system_id="inderscience",
    )

    assert result["status"] == "human_verification"
    assert result["next_action"] == "complete_captcha_then_resume"
    assert controller.clicked == []


def test_inderscience_existing_email_preserves_original_account_binding(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    (tmp_path / "author_info.md").write_text(
        "Status: READY\nName: Researcher Example\nEmail: author@xujc.com\n",
        encoding="utf-8",
    )
    store = FakeStore(site_path)
    controller = FakeController(
        [{"origin": "https://www.indersciencesubmissions.com"}]
    )
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )
    duplicate = {
        "page_id": "page-1",
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/index.php?action=authorreg",
        "text": "The email address already exists in our database.",
        "controls": [
            {"ref": "email", "type": "email", "id": "txtEmail"},
            {"ref": "first", "type": "text", "id": "txtFirstName"},
            {"ref": "last", "type": "text", "id": "txtLastName"},
            {"ref": "user", "type": "text", "id": "txtUserName"},
            {"ref": "password", "type": "text", "id": "txtPassword", "sensitive": True},
        ],
    }

    result = broker._register_account(
        paper={
            "credential_account_id": "legacy-inderscience-account",
            "paper_dir": str(tmp_path),
        },
        adapter=INDERSCIENCE_ADAPTER,
        allowed_origins=INDERSCIENCE_ADAPTER.allowed_origins,
        snapshot=duplicate,
        system_id="inderscience",
        target_journal="International Journal of Ad Hoc and Ubiquitous Computing",
    )

    assert result["status"] == "account_support_recovery_required"
    assert result["account_id"] == "legacy-inderscience-account"
    assert result["next_action"] == "contact_inderscience_support_for_account_recovery"
    assert store.accounts == {}
    assert controller.clicked == []


def test_authenticated_redirect_is_reconciled_after_one_transient_failure(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    store = FakeStore(site_path)
    store.accounts["springer-one"] = {
        "account_id": "springer-one",
        "username": "author@example.test",
        "email": "author@example.test",
    }
    store.secrets["springer-one"] = "SENTINEL-SAVED-PASSWORD"
    store.fail_record_once = True
    controller = FakeController(
        [_password_snapshot(), _authenticated_snapshot()]
    )
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-one",
        paper={
            "credential_account_id": "springer-one",
            "submission_system_id": "springer_nature",
            "target_journal": "The Journal of Supercomputing",
        },
        candidate={
            "credential_account_id": "springer-one",
            "submission_system_id": "springer_nature",
            "target_origin": "https://submission.springernature.com",
            "allowed_origins": ["https://submission.springernature.com"],
        },
    )

    assert result["ok"] is True
    assert result["code"] == BROKER_SESSION_READY
    assert len(store.recorded) == 1


def test_missing_site_account_prefers_saved_orcid_oauth_before_registration(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        (
            "Status: READY\n"
            "## Corresponding Author\n"
            "Name: Example Author\n"
            "Email: author@example.test\n"
            "ORCID: 0000-0002-1825-0097\n"
        ),
        encoding="utf-8",
    )
    site_login = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://idp-personal-authenticator.springernature.com",
        "path": "/gateway/login",
        "text": "Choose how to sign in",
        "controls": [
            {
                "ref": "orcid-entry",
                "tag": "button",
                "type": "button",
                "label": "Sign in with ORCID iD",
            }
        ],
    }
    orcid_login = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://orcid.org",
        "path": "/oauth/authorize",
        "text": "Sign into ORCID",
        "controls": [
            {"ref": "orcid-id", "tag": "input", "type": "text", "label": "Email or ORCID iD"},
            {"ref": "orcid-password", "tag": "input", "type": "password", "label": "Password"},
            {"ref": "orcid-submit", "tag": "button", "type": "submit", "label": "Sign in"},
        ],
    }
    orcid_authorize = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://orcid.org",
        "path": "/oauth/authorize",
        "text": "The journal requests your authenticated ORCID iD",
        "controls": [
            {"ref": "orcid-authorize", "tag": "button", "type": "submit", "label": "Authorize access"}
        ],
    }
    authenticated = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://submission.springernature.com",
        "path": "/your-submissions",
        "text": "Your submissions",
        "controls": [],
    }
    store = FakeStore(site_path)
    store.external_accounts["vault:orcid-primary"] = {
        "account_id": "orcid-primary",
        "scope": "external",
        "provider": "ORCID",
        "username": "0000-0002-1825-0097",
        "email": "author@example.test",
        "login_url": "https://orcid.org/signin",
        "status": "verified",
    }
    store.external_secrets["vault:orcid-primary"] = "SENTINEL-ORCID-PASSWORD"
    controller = FakeController(
        [site_login, orcid_login, orcid_authorize, authenticated]
    )
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-orcid",
        paper={
            "paper_dir": str(paper_dir),
            "submission_system_id": "springer_nature",
            "target_journal": "The Journal of Supercomputing",
            "account_missing_policy": "auto_register",
            "new_account_auth_preference": "orcid_first",
            "orcid_auto_authorize_for_login": True,
        },
        candidate={
            "submission_system_id": "springer_nature",
            "target_origin": "https://submission.springernature.com",
            "allowed_origins": list(SPRINGER_NATURE_ADAPTER.allowed_origins),
        },
    )

    assert result["ok"] is True
    assert result["code"] == BROKER_SESSION_READY
    assert result["registration_created"] is True
    assert controller.clicked == [
        "orcid-entry",
        "orcid-submit",
        "orcid-authorize",
    ]
    assert controller.trusted_values == ["SENTINEL-ORCID-PASSWORD"]
    linked = next(iter(store.accounts.values()))
    assert linked["auth_method"] == "orcid"
    assert "SENTINEL-ORCID-PASSWORD" not in json.dumps(result)
    assert len(store.recorded) == 1


def test_orcid_wait_ignores_loading_shell_until_login_controls_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    loading = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://orcid.org",
        "path": "/oauth/authorize",
        "text": "Loading ORCID",
        "controls": [],
    }
    ready = {
        **loading,
        "text": "Sign into ORCID",
        "controls": [
            {
                "ref": "orcid-password",
                "tag": "input",
                "type": "password",
                "label": "Password",
            },
            {
                "ref": "orcid-submit",
                "tag": "button",
                "type": "submit",
                "label": "Sign in",
            },
        ],
    }
    controller = FakeController([loading, ready])
    broker = TrustedCredentialBroker(
        store=FakeStore(site_path),
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )
    monkeypatch.setattr(broker_module.time, "sleep", lambda _seconds: None)

    result = broker._wait_orcid_or_target(
        WILEY_ADAPTER,
        ("https://orcid.org", "https://submission.wiley.com"),
        target_origin="https://submission.wiley.com:443",
        fallback_page_id="page-1",
        timeout_seconds=2,
    )

    assert any(
        item.get("type") == "password"
        for item in result.get("controls", [])
    )


def test_orcid_identity_actions_use_trusted_pointer_when_available(
    tmp_path: Path,
) -> None:
    class PointerController(FakeController):
        def __init__(self) -> None:
            super().__init__(
                [
                    {
                        "page_id": "page-1",
                        "origin": "https://orcid.org",
                        "controls": [],
                    }
                ]
            )
            self.pointer_clicked: list[str] = []

        def trusted_pointer_click(
            self,
            allowed_origins,
            ref,
            *,
            page_id,
        ):
            self.pointer_clicked.append(ref)
            return {"ok": True, "pointer_click": True}

    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    controller = PointerController()
    broker = TrustedCredentialBroker(
        store=FakeStore(site_path),
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker._trusted_identity_click(
        ("https://orcid.org",),
        "orcid-submit",
        page_id="page-1",
    )

    assert result["pointer_click"] is True
    assert controller.pointer_clicked == ["orcid-submit"]
    assert controller.clicked == []


def test_preferred_orcid_account_breaks_score_ties_in_favor_of_smart_record(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    store = FakeStore(site_path)
    shared = {
        "scope": "external",
        "provider": "ORCID",
        "username": "0000-0002-1825-0097",
        "login_url": "https://orcid.org/signin",
        "status": "verified",
        "last_verified_at": "2026-07-23T12:00:00+08:00",
    }
    store.external_accounts["vault:orcid-primary"] = {
        **shared,
        "account_id": "vault-orcid",
        "source_kind": "vault",
    }
    store.external_accounts["smart:orcid-primary"] = {
        **shared,
        "account_id": "smart-orcid",
        "source_kind": "smart",
    }
    store.external_secrets["vault:orcid-primary"] = "SENTINEL-VAULT"
    store.external_secrets["smart:orcid-primary"] = "SENTINEL-SMART"
    broker = TrustedCredentialBroker(
        store=store,
        controller=FakeController(
            [{"page_id": "page-1", "origin": "https://orcid.org"}]
        ),
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker._preferred_orcid_account(
        {
            "orcid": "0000-0002-1825-0097",
            "email": "author@example.test",
        }
    )

    assert result is not None
    assert result["account_id"] == "smart-orcid"


def test_footer_terms_link_is_not_a_terms_confirmation_checkpoint() -> None:
    status, action = TrustedCredentialBroker._waiting_from_snapshot(
        {
            "text": "Enter your password Terms and conditions",
            "controls": [
                {
                    "tag": "a",
                    "type": "a",
                    "label": "Terms and conditions",
                    "disabled": False,
                }
            ],
        }
    )

    assert status == "login_incomplete"
    assert action == "continue_visible_login_then_resume"


def test_cookie_privacy_button_is_not_a_terms_confirmation_checkpoint() -> None:
    status, action = TrustedCredentialBroker._waiting_from_snapshot(
        {
            "text": "Authors Terms and conditions Privacy policy",
            "controls": [
                {
                    "tag": "button",
                    "type": "button",
                    "label": "Your privacy choices/Manage cookies",
                    "disabled": False,
                },
                {
                    "tag": "a",
                    "type": "a",
                    "label": "Terms and conditions",
                    "disabled": False,
                },
            ],
        }
    )

    assert status == "login_incomplete"
    assert action == "continue_visible_login_then_resume"


def test_explicit_terms_checkbox_is_a_terms_confirmation_checkpoint() -> None:
    status, action = TrustedCredentialBroker._waiting_from_snapshot(
        {
            "text": "Please accept the submission terms.",
            "controls": [
                {
                    "tag": "input",
                    "type": "checkbox",
                    "label": "I agree to the terms and conditions",
                    "disabled": False,
                }
            ],
        }
    )

    assert status == "waiting_terms_confirmation"
    assert action == "accept_site_terms_then_resume"


def test_missing_account_uses_registration_adapter_and_stores_generated_secret(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        "Status: READY\nName: Example Author\nEmail: author@example.test\n",
        encoding="utf-8",
    )
    email_snapshot = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://idp-personal-authenticator.springernature.com",
        "path": "/gateway",
        "text": "Sign in or register",
        "controls": [
            {
                "ref": "upo-0001",
                "tag": "input",
                "type": "email",
                "label": "Email",
                "disabled": False,
            },
            {
                "ref": "upo-0002",
                "tag": "button",
                "type": "submit",
                "label": "Continue",
                "disabled": False,
            },
        ],
    }
    registration_snapshot = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://idp-personal-authenticator.springernature.com",
        "path": "/gateway/register",
        "text": "Create account",
        "controls": [
            {"ref": "upo-0001", "tag": "input", "type": "text", "label": "First name", "disabled": False},
            {"ref": "upo-0002", "tag": "input", "type": "text", "label": "Last name", "disabled": False},
            {"ref": "upo-0003", "tag": "input", "type": "password", "label": "Password", "disabled": False},
            {"ref": "upo-0004", "tag": "input", "type": "password", "label": "Confirm password", "disabled": False},
            {"ref": "upo-0005", "tag": "button", "type": "submit", "label": "Create account", "disabled": False},
        ],
    }
    store = FakeStore(site_path)
    controller = FakeController(
        [email_snapshot, registration_snapshot, _authenticated_snapshot()]
    )
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-new",
        paper={
            "paper_dir": str(paper_dir),
            "submission_system_id": "springer_nature",
            "target_journal": "The Journal of Supercomputing",
            "account_missing_policy": "auto_register",
        },
        candidate={
            "submission_system_id": "springer_nature",
            "target_origin": "https://submission.springernature.com",
            "allowed_origins": ["https://submission.springernature.com"],
        },
    )

    assert result["ok"] is True
    assert result["registration_created"] is True
    assert len(store.accounts) == 1
    account_id = next(iter(store.accounts))
    generated_secret = store.secrets[account_id]
    assert generated_secret
    assert controller.trusted_values == [generated_secret, generated_secret]
    assert generated_secret not in json.dumps(result)
    assert generated_secret not in json.dumps(store.recorded)


def test_scholar_one_missing_account_clicks_create_account_before_registration(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    write_json_atomic(
        site_path,
        {
            "systems": [
                {
                    "system_id": "scholar_one",
                    "status": "discovered",
                    "login_url": "https://mc.manuscriptcentral.com/",
                    "submission_url": "https://mc.manuscriptcentral.com/",
                }
            ],
            "journals": [
                {
                    "system_id": "scholar_one",
                    "journal_name": (
                        "Journal of Ambient Intelligence and Smart Environments (JAISE)"
                    ),
                    "status": "discovered",
                    "submission_url": "https://mc.manuscriptcentral.com/jaise",
                }
            ],
        },
    )
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        "Status: READY\nName: Example Author\nEmail: author@example.test\n",
        encoding="utf-8",
    )
    login_snapshot = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "text": "Login Create an Account",
        "controls": [
            {"ref": "user", "tag": "input", "type": "text", "label": "USERID"},
            {"ref": "password", "tag": "input", "type": "password", "label": "PASSWORD"},
            {"ref": "register", "tag": "a", "type": "a", "label": "Create an Account"},
        ],
    }
    registration_snapshot = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise/account/create",
        "text": "Create account",
        "controls": [
            {"ref": "upo-0001", "tag": "input", "type": "text", "label": "First name"},
            {"ref": "upo-0002", "tag": "input", "type": "text", "label": "Last name"},
            {"ref": "pw1", "tag": "input", "type": "password", "label": "Password"},
            {"ref": "pw2", "tag": "input", "type": "password", "label": "Confirm password"},
            {"ref": "create", "tag": "button", "type": "submit", "label": "Create account"},
        ],
    }
    authenticated_snapshot = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise/author",
        "text": "Author Center Start New Submission",
        "controls": [],
    }
    store = FakeStore(site_path)
    controller = FakeController(
        [login_snapshot, registration_snapshot, authenticated_snapshot]
    )
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-jaise",
        paper={
            "paper_dir": str(paper_dir),
            "submission_system_id": "scholar_one",
            "target_journal": (
                "Journal of Ambient Intelligence and Smart Environments"
            ),
            "account_missing_policy": "auto_register",
        },
        candidate={
            "submission_system_id": "scholar_one",
            "target_origin": "https://mc.manuscriptcentral.com",
            "allowed_origins": ["https://mc.manuscriptcentral.com"],
        },
    )

    assert result["ok"] is True
    assert result["registration_created"] is True
    assert controller.clicked[0] == "register"
    assert "create" in controller.clicked


def test_orcid_first_falls_back_before_click_when_no_saved_orcid_account(
    tmp_path: Path,
) -> None:
    site_path = tmp_path / "sites.json"
    _site_registry(site_path)
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "author_info.md").write_text(
        "Status: READY\nName: Example Author\nEmail: author@example.test\n",
        encoding="utf-8",
    )
    login_snapshot = {
        "page_id": "page-1",
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "text": "Login Create an Account",
        "controls": [
            {"ref": "user", "tag": "input", "type": "text", "label": "USERID"},
            {"ref": "password", "tag": "input", "type": "password", "label": "PASSWORD"},
            {"ref": "orcid-entry", "tag": "button", "type": "button", "label": "Sign in with ORCID iD"},
            {"ref": "register", "tag": "a", "type": "a", "label": "Create an Account"},
        ],
    }
    registration_snapshot = {
        "page_id": "page-1",
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise/account/create",
        "text": "Create account",
        "controls": [
            {"ref": "upo-0001", "tag": "input", "type": "text", "label": "First name"},
            {"ref": "upo-0002", "tag": "input", "type": "text", "label": "Last name"},
            {"ref": "pw1", "tag": "input", "type": "password", "label": "Password"},
            {"ref": "pw2", "tag": "input", "type": "password", "label": "Confirm password"},
            {"ref": "create", "tag": "button", "type": "submit", "label": "Create account"},
        ],
    }
    authenticated = {
        "page_id": "page-1",
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise/author",
        "text": "Author Center Start New Submission",
        "controls": [],
    }
    store = FakeStore(site_path)
    controller = FakeController([registration_snapshot, authenticated])
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker._register_account(
        paper={
            "paper_dir": str(paper_dir),
            "new_account_auth_preference": "orcid_first",
        },
        adapter=SCHOLAR_ONE_ADAPTER,
        allowed_origins=SCHOLAR_ONE_ADAPTER.allowed_origins,
        snapshot=login_snapshot,
        system_id="scholar_one",
        target_journal="Journal of Ambient Intelligence and Smart Environments",
        target_origin="https://mc.manuscriptcentral.com",
    )

    assert "snapshot" in result
    assert controller.clicked[0] == "register"
    assert "orcid-entry" not in controller.clicked
    assert next(iter(store.accounts.values())).get("auth_method") != "orcid"


def test_scholar_one_existing_account_is_recovered_through_trusted_mailbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    site_path = tmp_path / "sites.json"
    write_json_atomic(
        site_path,
        {
            "systems": [
                {
                    "system_id": "scholar_one",
                    "status": "discovered",
                    "login_url": "https://mc.manuscriptcentral.com/",
                    "submission_url": "https://mc.manuscriptcentral.com/",
                }
            ],
            "journals": [
                {
                    "system_id": "scholar_one",
                    "journal_name": "Journal of Ambient Intelligence and Smart Environments (JAISE)",
                    "status": "discovered",
                    "submission_url": "https://mc.manuscriptcentral.com/jaise",
                }
            ],
        },
    )
    duplicate = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "registration_step": "registration_step_1",
        "text": "The primary email address entered is associated with an existing account.",
        "controls": [],
    }
    reset_request = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/site/jaise/auth/reset-password",
        "text": "Reset Password Send Reset Link",
        "controls": [
            {"ref": "email", "tag": "input", "type": "email", "label": "Your Primary E-mail"},
            {"ref": "send", "tag": "button", "type": "submit", "label": "Send Reset Link"},
        ],
    }
    reset_sent = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/site/jaise/auth/reset-password-success",
        "text": "Please Check Your E-mail for Instructions",
        "controls": [],
    }
    reset_change = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/site/jaise/auth/reset-password/token",
        "text": "Reset Password New Password Confirm Password",
        "controls": [
            {"ref": "pw1", "tag": "input", "type": "password", "label": "New Password"},
            {"ref": "pw2", "tag": "input", "type": "password", "label": "Confirm Password"},
            {"ref": "save", "tag": "button", "type": "submit", "label": "Save Password"},
        ],
    }
    authenticated = {
        "page_id": "page-1",
        "cdp_port": 9224,
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise/author",
        "text": "Author Center Start New Submission",
        "controls": [],
    }
    store = FakeStore(site_path)
    account_id = "auto-scholar_one-test"
    secret = "SENTINEL-RECOVERY-PASSWORD"
    store.accounts[account_id] = {
        "account_id": account_id,
        "scope": "journal_system",
        "username": "author@xujc.com",
        "email": "author@xujc.com",
        "status": "registration_pending",
    }
    store.secrets[account_id] = secret
    controller = FakeController(
        [duplicate, reset_request, reset_sent, reset_change, authenticated]
    )
    monkeypatch.setattr(
        broker_module,
        "capture_password_recovery_checkpoint",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        broker_module,
        "wait_for_password_recovery_link",
        lambda *args, **kwargs: (
            "https://mc.manuscriptcentral.com/site/jaise/auth/reset-password/token"
        ),
    )
    broker = TrustedCredentialBroker(
        store=store,
        controller=controller,
        signer=FakeSigner(),
        state_path=tmp_path / "state.json",
    )

    result = broker.ensure_session(
        paper_id="paper-jaise-existing",
        paper={
            "credential_account_id": account_id,
            "submission_system_id": "scholar_one",
            "target_journal": "Journal of Ambient Intelligence and Smart Environments",
        },
        candidate={
            "credential_account_id": account_id,
            "submission_system_id": "scholar_one",
            "target_origin": "https://mc.manuscriptcentral.com",
            "allowed_origins": ["https://mc.manuscriptcentral.com"],
        },
    )

    assert result["ok"] is True
    assert result["code"] == BROKER_SESSION_READY
    assert store.accounts[account_id]["status"] == "ready"
    assert controller.trusted_values == [secret, secret]
    assert secret not in json.dumps(result)
    assert controller.navigated[0].endswith("/site/jaise/auth/reset-password")
