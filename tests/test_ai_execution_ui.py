from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unified_paper_orchestrator.ai_execution import AIExecutionError
from unified_paper_orchestrator.ui.ai_execution_settings import (
    API_KEY_MODE,
    LEGACY_CODEX_MODE,
    AIExecutionSettingsError,
    AIExecutionSettingsService,
    _format_error_for_display,
    _parse_custom_headers,
)


ROOT = Path(__file__).resolve().parents[1]
MAIN_UI_PATH = ROOT / "unified_paper_orchestrator" / "ui" / "main_window.py"
AI_UI_PATH = ROOT / "unified_paper_orchestrator" / "ui" / "ai_execution_settings.py"


@dataclass
class FakeSettings:
    execution_mode: str = LEGACY_CODEX_MODE
    active_profile_id: str = ""
    revision: int = 1


@dataclass
class FakeProfile:
    profile_id: str
    revision: int
    display_name: str
    base_url: str
    wire_api: str
    default_model: str
    reasoning_effort: str = ""
    proxy_url: str = ""
    custom_headers: dict[str, str] | None = None
    capability_status: str = "untested"
    last_tested_at: str = ""


class FakeStore:
    def __init__(self) -> None:
        self.settings = FakeSettings()
        self.profiles: dict[str, FakeProfile] = {}
        self.keys: set[str] = set()
        self.calls: list[tuple[Any, ...]] = []
        self.next_id = 1

    def load_settings(self) -> FakeSettings:
        return self.settings

    def list_profiles(self) -> list[FakeProfile]:
        return list(self.profiles.values())

    def create_profile(self, **values: Any) -> FakeProfile:
        self.calls.append(("create_profile", values))
        profile_id = f"profile-{self.next_id}"
        self.next_id += 1
        profile = FakeProfile(profile_id=profile_id, revision=1, **values)
        self.profiles[profile_id] = profile
        return profile

    def update_profile(self, profile_id: str, *, expected_revision: int, **values: Any) -> FakeProfile:
        self.calls.append(("update_profile", profile_id, expected_revision, values))
        current = self.profiles[profile_id]
        assert current.revision == expected_revision
        profile = FakeProfile(profile_id=profile_id, revision=current.revision + 1, **values)
        self.profiles[profile_id] = profile
        return profile

    def delete_profile(self, profile_id: str, *, expected_revision: int) -> None:
        self.calls.append(("delete_profile", profile_id, expected_revision))
        del self.profiles[profile_id]
        self.keys.discard(profile_id)

    def set_api_key(self, profile_id: str, key: str) -> None:
        self.calls.append(("set_api_key", profile_id, key))
        self.keys.add(profile_id)

    def has_api_key(self, profile_id: str) -> bool:
        self.calls.append(("has_api_key", profile_id))
        return profile_id in self.keys

    def set_active_profile(self, profile_id: str, *, expected_revision: int) -> FakeSettings:
        self.calls.append(("set_active_profile", profile_id, expected_revision))
        assert self.settings.revision == expected_revision
        self.settings.active_profile_id = profile_id
        self.settings.revision += 1
        return self.settings

    def set_execution_mode(self, mode: str, *, expected_revision: int) -> FakeSettings:
        self.calls.append(("set_execution_mode", mode, expected_revision))
        assert self.settings.revision == expected_revision
        self.settings.execution_mode = mode
        self.settings.revision += 1
        return self.settings


def valid_values() -> dict[str, Any]:
    return {
        "display_name": "Primary API",
        "base_url": "https://api.example.test/v1",
        "wire_api": "responses",
        "default_model": "test-model",
        "reasoning_effort": "high",
        "proxy_url": "",
        "custom_headers": {"X-Client": "v1"},
    }


def test_entry_is_appended_to_existing_more_menu_without_a_toolbar_button() -> None:
    source = MAIN_UI_PATH.read_text(encoding="utf-8")
    assert 'project_menu.add_command(label="AI 执行与 API 设置", command=self.open_ai_execution_settings)' in source
    assert 'ttk.Button(toolbar, text="AI 执行与 API 设置"' not in source
    assert source.count('text="AI 执行与 API 设置"') == 0
    assert "from .ai_execution_settings import open_ai_execution_settings" in source


def test_window_keeps_required_labels_masking_and_advanced_fold() -> None:
    source = AI_UI_PATH.read_text(encoding="utf-8")
    for fragment in (
        'text="API 密钥模式（当前仅审阅）"',
        'text="Codex 账号模式（完整流程）"',
        'text="新增"',
        'text="保存"',
        'text="删除"',
        'text="测试连接"',
        'show="*"',
        '"代理", "proxy_url"',
        "self.advanced_frame.grid_remove()",
    ):
        assert fragment in source
    assert "不会静默回退" in source
    assert "不会修改" in source and "既有任务提示词" in source
    assert "完整四阶段" in source and "本地 Codex SDK 工作流" in source
    assert "旧投稿暂时继续使用 V1 兼容执行器" not in source
    assert "连接 ChatGPT 账号" in source
    assert 'getattr(result, "error_code", "")' in source
    assert 'getattr(exc, "code", "")' in source
    assert "不覆盖 Codex Desktop" in source


def test_store_import_is_delayed_and_test_button_has_no_default_network_path() -> None:
    source = AI_UI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(getattr(node, "module", "") != "ai_provider_store" for node in top_level_imports)

    test_method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_test_profile"
    )
    test_source = ast.get_source_segment(source, test_method) or ""
    assert "self.test_callback" in test_source
    assert ".test_profile(" not in test_source
    assert "未发起网络请求" in test_source
    assert "requests" not in source
    assert "subprocess" not in source


def test_service_accepts_dataclasses_and_separates_api_key_from_profile_metadata() -> None:
    store = FakeStore()
    service = AIExecutionSettingsService(store)
    secret = "secret-for-test"
    saved = service.save_profile(
        profile_id="",
        expected_revision=None,
        values=valid_values(),
        api_key=secret,
    )

    assert saved["profile_id"] == "profile-1"
    create_payload = store.calls[0][1]
    assert "api_key" not in create_payload
    assert secret not in repr(create_payload)
    assert store.calls[1] == ("set_api_key", "profile-1", secret)
    settings, profiles = service.snapshot()
    assert settings["execution_mode"] == LEGACY_CODEX_MODE
    assert profiles[0]["display_name"] == "Primary API"


def test_profile_update_and_delete_use_expected_revision() -> None:
    store = FakeStore()
    service = AIExecutionSettingsService(store)
    created = service.save_profile(
        profile_id="",
        expected_revision=None,
        values=valid_values(),
        api_key="",
    )
    profile_id = str(created["profile_id"])
    edited = valid_values() | {"display_name": "Edited"}
    updated = service.save_profile(
        profile_id=profile_id,
        expected_revision=1,
        values=edited,
        api_key="",
    )
    assert updated["revision"] == 2
    assert ("update_profile", profile_id, 1, edited) in store.calls

    service.delete_profile(profile_id, expected_revision=2)
    assert ("delete_profile", profile_id, 2) in store.calls


def test_api_mode_fails_closed_without_profile_key_or_compatible_status() -> None:
    store = FakeStore()
    service = AIExecutionSettingsService(store)

    with pytest.raises(AIExecutionSettingsError, match="先选择"):
        service.save_mode(API_KEY_MODE, "", [])

    created = service.save_profile(
        profile_id="",
        expected_revision=None,
        values=valid_values(),
        api_key="",
    )
    profile_id = str(created["profile_id"])
    _, profiles = service.snapshot()
    with pytest.raises(AIExecutionSettingsError, match="尚未安全保存密钥"):
        service.save_mode(API_KEY_MODE, profile_id, profiles)

    store.keys.add(profile_id)
    with pytest.raises(AIExecutionSettingsError, match="尚未完成兼容性检测"):
        service.save_mode(API_KEY_MODE, profile_id, profiles)
    assert not any(call[0] == "set_active_profile" for call in store.calls)
    assert store.settings.execution_mode == LEGACY_CODEX_MODE


def test_compatible_profile_is_activated_before_api_mode_is_saved() -> None:
    store = FakeStore()
    service = AIExecutionSettingsService(store)
    created = service.save_profile(
        profile_id="",
        expected_revision=None,
        values=valid_values(),
        api_key="saved-key",
    )
    profile_id = str(created["profile_id"])
    store.profiles[profile_id].capability_status = "compatible"
    _, profiles = service.snapshot()
    store.calls.clear()

    result = service.save_mode(API_KEY_MODE, profile_id, profiles)

    assert result["execution_mode"] == API_KEY_MODE
    assert store.calls == [
        ("has_api_key", profile_id),
        ("set_active_profile", profile_id, 1),
        ("set_execution_mode", API_KEY_MODE, 2),
    ]


def test_legacy_mode_does_not_require_an_api_profile() -> None:
    store = FakeStore()
    store.settings.execution_mode = API_KEY_MODE
    service = AIExecutionSettingsService(store)

    result = service.save_mode(LEGACY_CODEX_MODE, "", [])

    assert result["execution_mode"] == LEGACY_CODEX_MODE
    assert store.calls == [("set_execution_mode", LEGACY_CODEX_MODE, 1)]


def test_custom_headers_are_strict_json_string_pairs() -> None:
    assert _parse_custom_headers('{"X-Client": "v1"}') == {"X-Client": "v1"}
    assert _parse_custom_headers("  ") == {}
    with pytest.raises(AIExecutionSettingsError, match="JSON 对象"):
        _parse_custom_headers("[]")
    with pytest.raises(AIExecutionSettingsError, match="必须是字符串"):
        _parse_custom_headers('{"X-Retry": 3}')


def test_error_display_includes_only_whitelisted_safe_provider_diagnostics() -> None:
    secret = "must-not-appear"
    error = AIExecutionError(
        "CODEX_SDK_TOKEN_BUDGET_EXCEEDED",
        "API 连接已成功，但本地检测预算过低。",
        details={
            "request_url": "https://relay.example.test/v1/responses",
            "sdk_status": "fatal_failure",
            "blocker_code": "CODEX_SDK_TOKEN_BUDGET_EXCEEDED",
            "token_budget": 32_768,
            "total_tokens": 11_125,
            "api_key": secret,
            "response_body": secret,
        },
    )

    rendered = _format_error_for_display(error)

    assert "实际请求地址：https://relay.example.test/v1/responses" in rendered
    assert "错误代码：CODEX_SDK_TOKEN_BUDGET_EXCEEDED" in rendered
    assert "SDK 状态：fatal_failure" in rendered
    assert "本地检测预算：32768" in rendered
    assert "实际总 token：11125" in rendered
    assert secret not in rendered

    untrusted = _format_error_for_display(RuntimeError(f"Bearer {secret}"))
    assert untrusted == "操作未完成（RuntimeError）。"
    assert secret not in untrusted
