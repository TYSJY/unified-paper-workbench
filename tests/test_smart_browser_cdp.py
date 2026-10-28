from __future__ import annotations

import pytest

from unified_paper_orchestrator import smart_browser_cdp as cdp_module
from unified_paper_orchestrator.smart_browser_cdp import (
    CdpPageTarget,
    SmartBrowserCdpController,
    SmartBrowserCdpError,
    is_final_submit_control,
)


class FinalControlController(SmartBrowserCdpController):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def _target(self, allowed_origins, *, page_id=""):
        return CdpPageTarget(
            port=9224,
            target_id=page_id or "page-1",
            url="https://submit.example.test/review",
            title="Review",
            websocket_url="ws://127.0.0.1:9224/devtools/page/page-1",
        )

    def inspect_control(self, allowed_origins, ref, *, page_id=""):
        return {
            "ok": True,
            "page_id": page_id or "page-1",
            "ref": ref,
            "marker": "Approve and Submit",
        }

    def _evaluate(self, target, expression, **kwargs):
        self.events.append("evaluate")
        return {"ok": True, "ref": "upo-0001", "marker": "Approve and Submit"}


@pytest.mark.parametrize(
    "label",
    [
        "Submit",
        "Final Submit",
        "Approve and Submit",
        "Complete the submission",
        "Complete my submission",
        "Send to editor",
        "提交稿件",
        "提交文章",
        "提交论文",
        "最终提交",
        "确认提交",
        "确认并提交",
        "完成投稿",
    ],
)
def test_final_submit_labels_are_recognized(label: str) -> None:
    assert is_final_submit_control(label)


def test_generic_click_rejects_final_control() -> None:
    controller = FinalControlController()

    with pytest.raises(SmartBrowserCdpError) as exc_info:
        controller.click(
            ["https://submit.example.test"],
            "upo-0001",
            page_id="page-1",
            allow_final_submit=False,
        )

    assert exc_info.value.code == "SMART_BROWSER_FINAL_SUBMIT_TOOL_REQUIRED"


def test_dedicated_final_click_requires_a_recognized_final_control() -> None:
    controller = FinalControlController()
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "Save draft",
    }

    with pytest.raises(SmartBrowserCdpError) as exc_info:
        controller.click(
            ["https://submit.example.test"],
            "upo-0001",
            page_id="page-1",
            allow_final_submit=True,
        )

    assert exc_info.value.code == "SMART_BROWSER_FINAL_CONTROL_NOT_VERIFIED"


def test_localized_final_control_is_verified_before_one_use_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class PointerSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def command(self, method, params=None):
            payload = dict(params or {})
            if method == "Runtime.evaluate":
                events.append(method)
                return {
                    "result": {
                        "value": {
                            "ok": True,
                            "ref": "upo-0001",
                            "marker": "鎻愪氦绋夸欢",
                            "x": 120,
                            "y": 240,
                            "viewport_width": 1280,
                            "viewport_height": 720,
                        }
                    }
                }
            if method == "Input.dispatchMouseEvent":
                events.append(str(payload.get("type") or ""))
            else:
                events.append(method)
            return {}

    monkeypatch.setattr(cdp_module, "_CdpSocket", PointerSession)
    controller = FinalControlController()
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "提交稿件 finalSubmissionButton",
        "label": "提交稿件",
    }

    result = controller.click(
        ["https://submit.example.test"],
        "upo-0001",
        page_id="page-1",
        allow_final_submit=True,
        before_click=lambda: events.append("claim"),
    )

    assert result["ok"] is True
    assert result["final_submit_control"] is True
    assert result["pointer_click"] is True
    assert events == [
        "Page.bringToFront",
        "Runtime.evaluate",
        "mouseMoved",
        "claim",
        "mousePressed",
        "mouseReleased",
    ]


def test_unverified_final_control_does_not_consume_one_use_claim() -> None:
    controller = FinalControlController()
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "Save draft",
        "label": "保存草稿",
    }

    with pytest.raises(SmartBrowserCdpError):
        controller.click(
            ["https://submit.example.test"],
            "upo-0001",
            page_id="page-1",
            allow_final_submit=True,
            before_click=lambda: controller.events.append("claim"),
        )

    assert controller.events == []


def test_login_continue_internal_submit_id_is_not_a_final_submission() -> None:
    controller = FinalControlController()
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "Continue email-submit",
        "label": "Continue",
    }

    result = controller.click(
        ["https://submit.example.test"],
        "upo-0001",
        page_id="page-1",
        allow_final_submit=False,
    )

    assert result["ok"] is True
    assert result["final_submit_control"] is False


def test_trusted_pointer_click_emits_real_mouse_gesture_for_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []

    class PointerSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def command(self, method, params=None):
            payload = dict(params or {})
            events.append((method, payload))
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "ok": True,
                            "ref": "upo-0001",
                            "marker": "Sign in",
                            "x": 120,
                            "y": 240,
                            "viewport_width": 1280,
                            "viewport_height": 720,
                        }
                    }
                }
            return {}

    monkeypatch.setattr(cdp_module, "_CdpSocket", PointerSession)
    controller = FinalControlController()
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "signin-button",
        "label": "Sign in",
    }

    result = controller.trusted_pointer_click(
        ["https://submit.example.test"],
        "upo-0001",
        page_id="page-1",
    )

    assert result["ok"] is True
    assert result["pointer_click"] is True
    assert [item[0] for item in events] == [
        "Page.bringToFront",
        "Runtime.evaluate",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert events[-2][1]["type"] == "mousePressed"
    assert events[-1][1]["type"] == "mouseReleased"


def test_trusted_pointer_click_never_bypasses_final_submit_tool() -> None:
    controller = FinalControlController()

    with pytest.raises(SmartBrowserCdpError) as exc_info:
        controller.trusted_pointer_click(
            ["https://submit.example.test"],
            "upo-0001",
            page_id="page-1",
        )

    assert exc_info.value.code == "SMART_BROWSER_FINAL_SUBMIT_TOOL_REQUIRED"


def test_fill_emits_trusted_keyboard_events_and_verifies_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []
    runtime_calls = 0

    class KeyboardSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def command(self, method, params=None):
            nonlocal runtime_calls
            payload = dict(params or {})
            events.append((method, payload))
            if method == "Runtime.evaluate":
                runtime_calls += 1
                value = (
                    {
                        "ok": True,
                        "ref": "upo-0001",
                        "marker": "AI disclosure",
                        "tag": "textarea",
                        "type": "",
                    }
                    if runtime_calls == 1
                    else {
                        "ok": True,
                        "ref": "upo-0001",
                        "marker": "AI disclosure",
                        "tag": "textarea",
                        "type": "",
                        "value_length": 15,
                        "expected_length": 15,
                    }
                )
                return {"result": {"value": value}}
            return {}

    monkeypatch.setattr(cdp_module, "_CdpSocket", KeyboardSession)
    controller = FinalControlController()
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "AI disclosure",
        "label": "AI disclosure",
        "tag": "textarea",
        "type": "",
    }

    result = controller.fill(
        ["https://submit.example.test"],
        "upo-0001",
        "AI-assisted use",
        page_id="page-1",
    )

    assert result["ok"] is True
    assert result["value_free"] is True
    assert result["trusted_keyboard"] is True
    assert [item[0] for item in events] == [
        "Page.bringToFront",
        "Runtime.evaluate",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Runtime.evaluate",
    ]
    assert events[2][1]["key"] == "a"
    assert events[2][1]["modifiers"] == 2
    assert events[6][1] == {"text": "AI-assisted use"}
    assert events[7][1]["key"] == "Tab"


def test_aria_combobox_click_uses_accessible_arrow_down_interaction() -> None:
    controller = FinalControlController()
    captured: list[str] = []
    controller.inspect_control = lambda *args, **kwargs: {
        "ok": True,
        "page_id": "page-1",
        "marker": "article-type",
        "label": "Article type",
        "role": "combobox",
    }

    def evaluate(_target, expression, **_kwargs):
        captured.append(expression)
        return {
            "ok": True,
            "ref": "upo-0001",
            "marker": "article-type",
            "opened_combobox": True,
        }

    controller._evaluate = evaluate

    result = controller.click(
        ["https://submit.example.test"],
        "upo-0001",
        page_id="page-1",
    )

    assert result["ok"] is True
    assert result["opened_combobox"] is True
    assert "role === 'combobox'" in captured[0]
    assert "key:'ArrowDown'" in captured[0]


def test_snapshot_exposes_transparent_native_checkbox_inside_visible_proxy() -> None:
    controller = FinalControlController()
    captured: list[str] = []

    def evaluate(_target, expression, **_kwargs):
        captured.append(expression)
        return {
            "ok": True,
            "origin": "https://submit.example.test",
            "url": "https://submit.example.test/qualifications",
            "path": "/qualifications",
            "title": "Qualifications",
            "ready_state": "complete",
            "text": "Acknowledge all statements",
            "text_truncated": False,
            "registration_step": "",
            "controls": [],
        }

    controller._evaluate = evaluate

    result = controller.snapshot(
        ["https://submit.example.test"],
        page_id="page-1",
    )

    assert result["ok"] is True
    assert "['checkbox', 'radio'].includes(inputType)" in captured[0]
    assert "const semanticProxy" in captured[0]
    assert "const proxy = element.parentElement" in captured[0]


def test_activate_page_restores_window_and_brings_verified_tab_to_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def command(self, method, params=None):
            payload = dict(params or {})
            events.append((method, payload))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 7, "bounds": {"windowState": "minimized"}}
            return {}

    monkeypatch.setattr(cdp_module, "_CdpSocket", FakeSession)
    controller = FinalControlController()

    result = controller.activate_page(
        ["https://submit.example.test"],
        page_id="page-1",
    )

    assert result["ok"] is True
    assert result["window_id"] == 7
    assert events == [
        ("Browser.getWindowForTarget", {"targetId": "page-1"}),
        (
            "Browser.setWindowBounds",
            {"windowId": 7, "bounds": {"windowState": "normal"}},
        ),
        ("Page.bringToFront", {}),
    ]


def test_upload_resolver_accepts_visible_trigger_with_one_file_input() -> None:
    expression = SmartBrowserCdpController._ref_expression(
        "upo-0001",
        """
  let fileInput = element;
  let scope = element.parentElement;
  const nearby = scope
    ? Array.from(scope.querySelectorAll('input[type="file"]'))
    : [];
  if (nearby.length === 1) fileInput = nearby[0];
  return fileInput;
""",
    )

    assert "element.parentElement" in expression
    assert "input[type=\"file\"]" in expression


def test_upload_resolver_prefers_nearest_custom_uploader_scope() -> None:
    controller = FinalControlController()
    captured: list[str] = []

    class UploadSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def command(self, method, params=None):
            if method == "Runtime.evaluate":
                captured.append(str((params or {}).get("expression") or ""))
                return {
                    "result": {
                        "subtype": "node",
                        "objectId": "file-input-object",
                    }
                }
            if method == "DOM.describeNode":
                return {"node": {"backendNodeId": 17}}
            if method == "DOM.setFileInputFiles":
                return {}
            raise AssertionError(method)

    original_session = cdp_module._CdpSocket
    cdp_module._CdpSocket = UploadSession
    try:
        result = controller.upload(
            ["https://submit.example.test"],
            "upo-0001",
            ["D:/approved/main.tex"],
            page_id="page-1",
        )
    finally:
        cdp_module._CdpSocket = original_session

    assert result["ok"] is True
    assert "let scope = element.parentElement" in captured[0]
    assert "depth < 4" in captured[0]
    assert "nearby.length === 1" in captured[0]
