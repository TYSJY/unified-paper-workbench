from __future__ import annotations

from pathlib import Path

import pytest

from unified_paper_orchestrator.smart_browser_mcp import (
    SMART_BROWSER_TOOL_NAMES,
    SmartBrowserContextError,
    SmartBrowserContextStore,
    SmartBrowserMcpServer,
    SmartBrowserMcpTools,
    _PageBoundConfirmationTransport,
)


class FakeController:
    instances: list["FakeController"] = []

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.__class__.instances.append(self)

    def list_pages(self, origins):
        self.calls.append(("pages", tuple(origins)))
        return [{"page_id": "page-1", "origin": origins[0]}]

    def snapshot(self, origins, **kwargs):
        self.calls.append(("snapshot", tuple(origins), kwargs))
        return {"ok": True, "page_id": "page-1", "controls": []}

    def navigate(self, origins, url, **kwargs):
        self.calls.append(("navigate", tuple(origins), url, kwargs))
        return {"ok": True, "page_id": "page-1", "url": url}

    def upload(self, origins, ref, files, **kwargs):
        self.calls.append(("upload", tuple(origins), ref, tuple(files), kwargs))
        return {"ok": True, "files": list(files)}

    def click(self, origins, ref, **kwargs):
        self.calls.append(("click", tuple(origins), ref, kwargs))
        return {"ok": True, "ref": ref}

    def wait_ready(self, origins, **kwargs):
        self.calls.append(("wait", tuple(origins), kwargs))
        return {"ok": True, "page_id": kwargs.get("page_id")}


class MultiPageController(FakeController):
    pages = [
        {
            "page_id": "dashboard-1",
            "origin": "https://submit.example.test:443",
            "url": "https://submit.example.test/dashboard",
        },
        {
            "page_id": "dashboard-2",
            "origin": "https://submit.example.test:443",
            "url": "https://submit.example.test/dashboard",
        },
    ]

    def list_pages(self, origins):
        self.calls.append(("pages", tuple(origins)))
        return [dict(page) for page in self.__class__.pages]

    def click(self, origins, ref, **kwargs):
        result = super().click(origins, ref, **kwargs)
        page_id = str(kwargs.get("page_id") or "")
        if page_id == "dashboard-1" and not any(
            page["page_id"] == "draft-1" for page in self.__class__.pages
        ):
            self.__class__.pages.append(
                {
                    "page_id": "draft-1",
                    "origin": "https://forms.example.test:443",
                    "url": "https://forms.example.test/submission/draft-1",
                }
            )
        return result


def test_confirmation_transport_never_scans_another_papers_same_origin_page() -> None:
    class Delegate:
        def list_targets(self, _port):
            return [
                {
                    "id": "paper-a-page",
                    "type": "page",
                    "url": "https://submit.example.test/final-review/a",
                },
                {
                    "id": "paper-b-page",
                    "type": "page",
                    "url": "https://submit.example.test/confirmation/b",
                },
            ]

        def evaluate_submission_confirmation_state(self, port, websocket_url):
            return {"port": port, "websocket_url": websocket_url}

    transport = _PageBoundConfirmationTransport(
        "paper-a-page",
        delegate=Delegate(),
    )

    assert transport.list_targets(9224) == [
        {
            "id": "paper-a-page",
            "type": "page",
            "url": "https://submit.example.test/final-review/a",
        }
    ]


def _context(tmp_path: Path) -> tuple[SmartBrowserContextStore, dict, Path]:
    upload = tmp_path / "manuscript.pdf"
    upload.write_bytes(b"%PDF-1.4\n")
    store = SmartBrowserContextStore(tmp_path / "contexts.json")
    context = store.create(
        paper_id="paper-1",
        route_revision=3,
        attempt_id="attempt-1",
        thread_id="thread-1",
        allowed_origins=["https://submit.example.test/path"],
        allowed_files=[upload],
        allowed_file_roots=[tmp_path],
        target_origin="https://submit.example.test",
    )
    return store, context, upload.resolve()


def test_context_is_bound_and_retirement_is_fail_closed(tmp_path: Path) -> None:
    store, context, upload = _context(tmp_path)

    loaded = store.load_active(context["context_id"])

    assert loaded["paper_id"] == "paper-1"
    assert loaded["thread_id"] == "thread-1"
    assert loaded["allowed_origins"] == ["https://submit.example.test:443"]
    assert loaded["allowed_files"] == [str(upload)]

    store.retire(context["context_id"])
    with pytest.raises(SmartBrowserContextError) as exc_info:
        store.load_active(context["context_id"])
    assert exc_info.value.code == "SMART_BROWSER_CONTEXT_EXPIRED"


def test_files_are_listed_by_id_and_upload_resolves_only_approved_ids(
    tmp_path: Path,
) -> None:
    FakeController.instances.clear()
    store, context, upload = _context(tmp_path)
    tools = SmartBrowserMcpTools(
        store,
        controller_factory=FakeController,
    )

    listing = tools.call(
        "browser_files",
        {"context_id": context["context_id"]},
    )
    uploaded = tools.call(
        "browser_upload",
        {
            "context_id": context["context_id"],
            "ref": "upo-0001",
            "files": ["file-0001"],
        },
    )

    assert listing["files"] == [
        {
            "file_id": "file-0001",
            "name": "manuscript.pdf",
            "suffix": ".pdf",
            "size_bytes": 9,
        }
    ]
    assert uploaded["files"] == [str(upload)]
    with pytest.raises(SmartBrowserContextError) as exc_info:
        tools.call(
            "browser_upload",
            {
                "context_id": context["context_id"],
                "ref": "upo-0001",
                "files": ["file-9999"],
            },
        )
    assert exc_info.value.code == "SMART_BROWSER_UPLOAD_FILE_DENIED"


def test_generic_click_never_enables_final_submit_mode(tmp_path: Path) -> None:
    FakeController.instances.clear()
    store, context, _upload = _context(tmp_path)
    tools = SmartBrowserMcpTools(
        store,
        controller_factory=FakeController,
    )

    result = tools.call(
        "browser_click",
        {
            "context_id": context["context_id"],
            "ref": "upo-0003",
        },
    )

    assert result["ok"] is True
    click = next(
        call
        for call in reversed(FakeController.instances[-1].calls)
        if call[0] == "click"
    )
    assert click[0] == "click"
    assert click[-1]["allow_final_submit"] is False


def test_browser_pages_are_exclusive_per_paper_and_new_tabs_inherit_owner(
    tmp_path: Path,
) -> None:
    MultiPageController.instances.clear()
    MultiPageController.pages = [
        {
            "page_id": "dashboard-1",
            "origin": "https://submit.example.test:443",
            "url": "https://submit.example.test/dashboard",
        },
        {
            "page_id": "dashboard-2",
            "origin": "https://submit.example.test:443",
            "url": "https://submit.example.test/dashboard",
        },
    ]
    store = SmartBrowserContextStore(tmp_path / "contexts.json")
    first = store.create(
        paper_id="paper-1",
        route_revision=1,
        attempt_id="attempt-1",
        thread_id="thread-1",
        allowed_origins=[
            "https://submit.example.test",
            "https://forms.example.test",
        ],
        target_origin="https://submit.example.test",
    )
    second = store.create(
        paper_id="paper-2",
        route_revision=1,
        attempt_id="attempt-2",
        thread_id="thread-2",
        allowed_origins=[
            "https://submit.example.test",
            "https://forms.example.test",
        ],
        target_origin="https://submit.example.test",
    )
    tools = SmartBrowserMcpTools(
        store,
        controller_factory=MultiPageController,
    )

    first_pages = tools.call(
        "browser_pages",
        {"context_id": first["context_id"]},
    )
    second_pages = tools.call(
        "browser_pages",
        {"context_id": second["context_id"]},
    )
    tools.call(
        "browser_click",
        {
            "context_id": first["context_id"],
            "page_id": "dashboard-1",
            "ref": "upo-0001",
        },
    )
    first_after = tools.call(
        "browser_pages",
        {"context_id": first["context_id"]},
    )
    second_after = tools.call(
        "browser_pages",
        {"context_id": second["context_id"]},
    )

    assert [page["page_id"] for page in first_pages["pages"]] == ["dashboard-1"]
    assert [page["page_id"] for page in second_pages["pages"]] == ["dashboard-2"]
    assert {page["page_id"] for page in first_after["pages"]} == {
        "dashboard-1",
        "draft-1",
    }
    assert [page["page_id"] for page in second_after["pages"]] == ["dashboard-2"]
    with pytest.raises(SmartBrowserContextError) as exc_info:
        tools.call(
            "browser_snapshot",
            {
                "context_id": second["context_id"],
                "page_id": "draft-1",
            },
        )
    assert exc_info.value.code == "SMART_BROWSER_PAGE_OWNED_BY_OTHER_PAPER"


def test_browser_wait_honors_bounded_delay(tmp_path: Path) -> None:
    delays: list[float] = []
    store, context, _upload = _context(tmp_path)
    tools = SmartBrowserMcpTools(
        store,
        controller_factory=FakeController,
        sleep=delays.append,
    )

    result = tools.call(
        "browser_wait",
        {
            "context_id": context["context_id"],
            "page_id": "page-1",
            "timeout_seconds": 120,
        },
    )

    assert result["ok"] is True
    assert delays == [30.0]


def test_mail_search_is_gmail_scoped_and_exposed_as_read_only(tmp_path: Path) -> None:
    FakeController.instances.clear()
    store = SmartBrowserContextStore(tmp_path / "contexts.json")
    context = store.create(
        paper_id="monitor-batch",
        route_revision=0,
        attempt_id="monitor-run",
        thread_id="thread-monitor",
        allowed_origins=["https://mail.google.com:443"],
        target_origin="https://mail.google.com:443",
    )
    tools = SmartBrowserMcpTools(
        store,
        controller_factory=FakeController,
        sleep=lambda _seconds: None,
    )

    result = tools.call(
        "browser_mail_search",
        {
            "context_id": context["context_id"],
            "page_id": "page-1",
            "query": 'newer_than:365d {"MS-123" "Exact Paper Title"}',
        },
    )
    calls = FakeController.instances[-1].calls
    navigate = next(item for item in calls if item[0] == "navigate")
    spec = next(
        item for item in SmartBrowserMcpServer(tools).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )["result"]["tools"]
        if item["name"] == "browser_mail_search"
    )

    assert navigate[2].startswith("https://mail.google.com/mail/u/0/#search/")
    assert "%22MS-123%22" in navigate[2]
    assert result["mailbox_mutation_allowed"] is False
    assert spec["annotations"]["readOnlyHint"] is True


def test_mcp_protocol_exposes_only_the_project_browser_tools(tmp_path: Path) -> None:
    store, _context_value, _upload = _context(tmp_path)
    server = SmartBrowserMcpServer(
        SmartBrowserMcpTools(store, controller_factory=FakeController)
    )

    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
            },
        }
    )
    listed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
    )

    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    names = tuple(item["name"] for item in listed["result"]["tools"])
    assert names == SMART_BROWSER_TOOL_NAMES
    assert "browser_final_submit_once" in names
