from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex import CodexConfig

from unified_paper_orchestrator import config
from unified_paper_orchestrator.codex_sdk_account import (
    CODEX_SDK_HOME,
    CodexSDKAccountError,
    CodexSDKAccountService,
)


AUTH_URL = "https://auth.example.test/oauth?secret=browser-url"
VERIFY_URL = "https://auth.example.test/device?secret=device-url"
USER_CODE = "NEVER-LOG-1234"
EMAIL = "private-account@example.test"
PROVIDER_SECRET = "provider-exception-secret"


class _Handle:
    def __init__(
        self,
        *,
        method: str,
        success: bool = True,
        wait_error: Exception | None = None,
    ) -> None:
        self.auth_url = AUTH_URL if method == "browser" else ""
        self.verification_url = VERIFY_URL if method == "device_code" else ""
        self.user_code = USER_CODE if method == "device_code" else ""
        self.success = success
        self.wait_error = wait_error
        self.wait_calls = 0
        self.cancel_calls = 0

    def wait(self) -> object:
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
        return SimpleNamespace(
            success=self.success,
            error=f"failure containing {PROVIDER_SECRET}" if not self.success else None,
            login_id="sdk-login-id-must-stay-private",
        )

    def cancel(self) -> object:
        self.cancel_calls += 1
        return SimpleNamespace(status="cancelled")


class _Client:
    def __init__(
        self,
        *,
        account: object | None = None,
        browser: _Handle | None = None,
        device: _Handle | None = None,
        account_error: Exception | None = None,
    ) -> None:
        self.account_value = account
        self.browser = browser or _Handle(method="browser")
        self.device = device or _Handle(method="device_code")
        self.account_error = account_error
        self.account_refreshes: list[bool] = []
        self.close_calls = 0
        self.logout_calls = 0
        self.browser_calls = 0
        self.device_calls = 0

    def account(self, *, refresh_token: bool = False) -> object:
        self.account_refreshes.append(refresh_token)
        if self.account_error is not None:
            raise self.account_error
        return self.account_value or SimpleNamespace(
            account=None,
            requires_openai_auth=True,
        )

    def login_chatgpt(self) -> _Handle:
        self.browser_calls += 1
        return self.browser

    def login_chatgpt_device_code(self) -> _Handle:
        self.device_calls += 1
        return self.device

    def logout(self) -> None:
        self.logout_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _chatgpt_account() -> object:
    return SimpleNamespace(
        account=SimpleNamespace(
            root=SimpleNamespace(type="chatgpt", email=EMAIL, plan_type="plus")
        ),
        requires_openai_auth=True,
        access_token="must-never-be-read",
    )


def _factory(clients: list[_Client], captures: list[CodexConfig]):
    def create(sdk_config: CodexConfig) -> _Client:
        captures.append(sdk_config)
        return clients.pop(0)

    return create


def test_default_home_is_isolated_and_construction_is_lazy() -> None:
    calls: list[CodexConfig] = []
    service = CodexSDKAccountService(lambda sdk_config: calls.append(sdk_config))

    assert service.codex_home == CODEX_SDK_HOME.resolve()
    assert CODEX_SDK_HOME == config.STATE_DIR / "codex_sdk_home"
    assert calls == []


def test_status_uses_isolated_codex_config_and_returns_no_account_secrets(
    tmp_path: Path,
) -> None:
    client = _Client(account=_chatgpt_account())
    captures: list[CodexConfig] = []
    inherited_secret = "existing-process-secret-must-not-be-copied"
    service = CodexSDKAccountService(
        _factory([client], captures), codex_home=tmp_path / "sdk-home"
    )

    status = service.status()

    assert status == {
        "schema": "unified-paper-orchestrator/codex-sdk-account-status/v1",
        "ok": True,
        "authenticated": True,
        "requires_openai_auth": True,
        "auth_mode": "chatgpt",
        "plan_type": "plus",
        "isolated_home": True,
    }
    assert captures[0].env["CODEX_HOME"] == str((tmp_path / "sdk-home").resolve())
    assert set(key.upper() for key in captures[0].env) <= {"CODEX_HOME", "PATH"}
    assert inherited_secret not in captures[0].env
    assert client.account_refreshes == [False]
    assert client.close_calls == 1
    rendered = json.dumps(status)
    assert EMAIL not in rendered
    assert "access_token" not in rendered


def test_status_failure_returns_fixed_public_code_without_exception_text(
    tmp_path: Path,
) -> None:
    client = _Client(account_error=RuntimeError(f"failure with {PROVIDER_SECRET}"))
    service = CodexSDKAccountService(
        lambda _config: client, codex_home=tmp_path / "sdk-home"
    )

    status = service.status()

    assert status["ok"] is False
    assert status["error_code"] == "CODEX_ACCOUNT_STATUS_UNAVAILABLE"
    assert status["error_type"] == "RuntimeError"
    assert PROVIDER_SECRET not in json.dumps(status)
    assert client.close_calls == 1


def test_verified_status_refreshes_token_and_rejects_stale_local_metadata(
    tmp_path: Path,
) -> None:
    client = _Client(
        account=SimpleNamespace(account=None, requires_openai_auth=True)
    )
    service = CodexSDKAccountService(
        lambda _config: client, codex_home=tmp_path / "sdk-home"
    )

    status = service.verified_status()

    assert status["ok"] is True
    assert status["authenticated"] is False
    assert client.account_refreshes == [True]
    assert client.close_calls == 1


def test_browser_begin_and_wait_are_in_memory_and_do_not_auto_open_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    browser = _Handle(method="browser")
    client = _Client(account=_chatgpt_account(), browser=browser)
    captures: list[CodexConfig] = []
    open_calls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: open_calls.append(url))
    caplog.set_level(logging.DEBUG)
    service = CodexSDKAccountService(
        _factory([client], captures), codex_home=tmp_path / "sdk-home"
    )

    challenge = service.begin_browser_login()

    assert challenge.method == "browser"
    assert challenge.verification_url == AUTH_URL
    assert challenge.user_code == ""
    assert AUTH_URL not in repr(challenge)
    assert client.close_calls == 0
    assert open_calls == []

    result = service.wait_for_login(challenge.attempt_id)

    assert result.success is True
    assert result.method == "browser"
    assert result.status["authenticated"] is True
    assert EMAIL not in json.dumps(result.status)
    assert browser.wait_calls == 1
    assert client.account_refreshes == [False]
    assert client.close_calls == 1
    assert AUTH_URL not in caplog.text
    assert not list((tmp_path / "sdk-home").rglob("*.log"))


def test_browser_login_repairs_callback_written_to_default_home(
    tmp_path: Path,
) -> None:
    browser = _Handle(method="browser")
    client = _Client(account=_chatgpt_account(), browser=browser)
    fallback = tmp_path / "default-home" / "auth.json"
    fallback.parent.mkdir(parents=True)
    fallback.write_text('{"generation": 1}', encoding="utf-8")
    service = CodexSDKAccountService(
        lambda _config: client,
        codex_home=tmp_path / "sdk-home",
        fallback_auth_path=fallback,
        acl_hardener=lambda _path: None,
    )

    challenge = service.begin_browser_login()
    fallback.write_text(
        '{"generation": 2, "sentinel": "not-a-real-token"}',
        encoding="utf-8",
    )
    result = service.wait_for_login(challenge.attempt_id)

    assert result.success is True
    assert (service.codex_home / "auth.json").read_text(encoding="utf-8") == (
        fallback.read_text(encoding="utf-8")
    )


def test_device_begin_wait_redacts_code_and_provider_failure_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    device = _Handle(method="device_code", success=False)
    client = _Client(device=device)
    service = CodexSDKAccountService(
        lambda _config: client, codex_home=tmp_path / "sdk-home"
    )
    caplog.set_level(logging.DEBUG)

    challenge = service.begin_device_login()

    assert challenge.verification_url == VERIFY_URL
    assert challenge.user_code == USER_CODE
    assert VERIFY_URL not in repr(challenge)
    assert USER_CODE not in repr(challenge)

    result = service.wait_for_login(challenge.attempt_id)

    assert result.success is False
    assert result.error_code == "CODEX_LOGIN_NOT_COMPLETED"
    assert PROVIDER_SECRET not in repr(result)
    assert VERIFY_URL not in caplog.text
    assert USER_CODE not in caplog.text
    assert client.close_calls == 1


def test_browser_login_reconciles_durable_account_when_notification_is_false(
    tmp_path: Path,
) -> None:
    browser = _Handle(method="browser", success=False)
    client = _Client(account=_chatgpt_account(), browser=browser)
    service = CodexSDKAccountService(
        lambda _config: client, codex_home=tmp_path / "sdk-home"
    )

    challenge = service.begin_browser_login()
    result = service.wait_for_login(challenge.attempt_id)

    assert result.success is True
    assert result.error_code == ""
    assert result.status["authenticated"] is True
    assert result.status["auth_mode"] == "chatgpt"
    assert client.account_refreshes == [False]
    assert client.close_calls == 1


def test_browser_login_retries_changed_auth_until_account_becomes_visible(
    tmp_path: Path,
) -> None:
    browser = _Handle(method="browser", success=False)
    client = _Client(browser=browser)
    responses = iter(
        [
            SimpleNamespace(account=None, requires_openai_auth=True),
            SimpleNamespace(account=None, requires_openai_auth=True),
            _chatgpt_account(),
        ]
    )

    def account(*, refresh_token: bool = False) -> object:
        client.account_refreshes.append(refresh_token)
        return next(responses)

    client.account = account  # type: ignore[method-assign]
    service = CodexSDKAccountService(
        lambda _config: client,
        codex_home=tmp_path / "sdk-home",
        sleep_fn=lambda _seconds: None,
    )

    challenge = service.begin_browser_login()
    (service.codex_home / "auth.json").write_text("{}", encoding="utf-8")
    result = service.wait_for_login(challenge.attempt_id)

    assert result.success is True
    assert result.status["authenticated"] is True
    assert client.account_refreshes == [False, False, False]
    assert client.close_calls == 1


def test_wait_error_is_fixed_and_consumes_the_attempt(tmp_path: Path) -> None:
    device = _Handle(
        method="device_code",
        wait_error=RuntimeError(f"wait failed with {PROVIDER_SECRET}"),
    )
    client = _Client(device=device)
    service = CodexSDKAccountService(
        lambda _config: client, codex_home=tmp_path / "sdk-home"
    )
    challenge = service.begin_device_login()

    with pytest.raises(CodexSDKAccountError) as caught:
        service.wait_for_login(challenge.attempt_id)

    assert caught.value.code == "CODEX_LOGIN_WAIT_FAILED"
    assert caught.value.error_type == "RuntimeError"
    assert PROVIDER_SECRET not in str(caught.value)
    assert client.close_calls == 1
    with pytest.raises(CodexSDKAccountError) as missing:
        service.wait_for_login(challenge.attempt_id)
    assert missing.value.code == "CODEX_LOGIN_ATTEMPT_NOT_FOUND"


def test_cancel_close_and_logout_release_their_clients(tmp_path: Path) -> None:
    first = _Client()
    second = _Client()
    third = _Client()
    service = CodexSDKAccountService(
        _factory([first, second, third], []), codex_home=tmp_path / "sdk-home"
    )

    browser = service.begin_browser_login()
    assert service.cancel_login(browser.attempt_id) is True
    assert first.browser.cancel_calls == 1
    assert first.close_calls == 1

    service.begin_device_login()
    service.close()
    assert second.device.cancel_calls == 1
    assert second.close_calls == 1

    status = service.logout()
    assert status["authenticated"] is False
    assert third.logout_calls == 1
    assert third.close_calls == 1


def test_login_start_failure_closes_client_and_hides_exception_text(
    tmp_path: Path,
) -> None:
    client = _Client()

    def fail() -> _Handle:
        raise RuntimeError(f"start failed with {PROVIDER_SECRET}")

    client.login_chatgpt = fail  # type: ignore[method-assign]
    service = CodexSDKAccountService(
        lambda _config: client, codex_home=tmp_path / "sdk-home"
    )

    with pytest.raises(CodexSDKAccountError) as caught:
        service.begin_browser_login()

    assert caught.value.code == "CODEX_LOGIN_START_FAILED"
    assert caught.value.error_type == "RuntimeError"
    assert PROVIDER_SECRET not in str(caught.value)
    assert client.close_calls == 1


class _BlockedHandle(_Handle):
    def __init__(self):
        from threading import Event
        super().__init__(method="browser")
        self.entered = Event()
        self.released = Event()

    def wait(self):
        self.entered.set()
        assert self.released.wait(5), "login waiter was not released"
        raise RuntimeError(PROVIDER_SECRET)

    def cancel(self):
        self.cancel_calls += 1
        self.released.set()


def test_cancel_while_waiting_releases_attempt_and_allows_a_fresh_login(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    blocked = _BlockedHandle()
    first = _Client(browser=blocked)
    second = _Client(account=_chatgpt_account())
    service = CodexSDKAccountService(_factory([first, second], []), codex_home=tmp_path)
    challenge = service.begin_browser_login()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.wait_for_login, challenge.attempt_id)
        assert blocked.entered.wait(2)
        assert service.cancel_login(challenge.attempt_id)
        result = future.result(timeout=2)
    assert result.error_code == "CODEX_LOGIN_CANCELLED"
    assert not result.success
    assert PROVIDER_SECRET not in repr(result)
    assert first.close_calls == 1
    assert first.account_refreshes == []
    assert service.wait_for_login(service.begin_browser_login().attempt_id).success
    assert second.close_calls == 1


def test_login_timeout_ends_pending_attempt_without_false_success(tmp_path):
    blocked = _BlockedHandle()
    client = _Client(browser=blocked, account=_chatgpt_account())
    service = CodexSDKAccountService(lambda _: client, codex_home=tmp_path,
                                     login_wait_timeout_seconds=0.03)
    result = service.wait_for_login(service.begin_browser_login().attempt_id)
    assert result.error_code == "CODEX_LOGIN_TIMED_OUT"
    assert not result.success
    assert blocked.cancel_calls == 1
    assert client.close_calls == 1
    assert not service._pending


def test_shutdown_cancels_an_attempt_that_already_has_a_waiter(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    blocked = _BlockedHandle()
    client = _Client(browser=blocked)
    service = CodexSDKAccountService(lambda _: client, codex_home=tmp_path)
    challenge = service.begin_browser_login()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.wait_for_login, challenge.attempt_id)
        assert blocked.entered.wait(2)
        service.close()
        assert future.result(timeout=2).error_code == "CODEX_LOGIN_CANCELLED"
    assert client.close_calls == 1
