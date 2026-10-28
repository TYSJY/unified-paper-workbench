from types import MethodType, SimpleNamespace

from unified_paper_orchestrator.ui_next.controller import WorkbenchController


def controller_fixture():
    actions, tasks = [], []
    fake = SimpleNamespace(
        _codex_login_attempt="attempt-1", _codex_login_code="SAMPLE-CODE",
        _codex_account_status={"loginPending": True, "statusKnown": True},
        codexAccountChanged=SimpleNamespace(emit=lambda: None),
        codexAccountActionCompleted=SimpleNamespace(emit=lambda *args: actions.append(args)),
        toastRequested=SimpleNamespace(emit=lambda *args: None),
        _t=lambda key, fallback: key,
        _submit=lambda *args, **kwargs: tasks.append(args),
    )
    fake._publish_codex_account_status = MethodType(WorkbenchController._publish_codex_account_status, fake)
    return fake, actions, tasks


def test_refresh_is_available_during_login_and_does_not_clear_pending():
    fake, _, tasks = controller_fixture()
    fake.codex_account = SimpleNamespace(verified_status=lambda: {"authenticated": False})
    WorkbenchController.refreshCodexAccount(fake)
    assert tasks[0][0] == "codex:status"
    WorkbenchController._complete_codex_account_task(fake, "codex:status", tasks[0][2]())
    assert fake._codex_account_status["loginPending"]
    assert fake._codex_account_status["canCancelLogin"]


def test_cancel_wait_completion_clears_code_and_allows_reconnect():
    fake, actions, tasks = controller_fixture()
    cancelled = []
    fake.codex_account = SimpleNamespace(cancel_login=lambda attempt: cancelled.append(attempt))
    WorkbenchController.cancelCodexLogin(fake)
    tasks[0][2]()
    assert cancelled == ["attempt-1"]
    WorkbenchController._complete_codex_account_task(fake, "codex:wait_login", {
        "success": False, "errorCode": "CODEX_LOGIN_CANCELLED", "status": {"authenticated": False}})
    assert not fake._codex_account_status["loginPending"]
    assert not fake._codex_account_status["canCancelLogin"]
    assert fake._codex_login_code == fake._codex_login_attempt == ""
    assert actions[-1][0] == "codex:login_cancelled"
    fake._begin_codex_browser_login = lambda method: {"method": method}
    WorkbenchController._connect_codex_account(fake, "device_code")
    assert tasks[-1][2]() == {"method": "device_code"}
    assert fake._codex_account_status["loginPending"]


def test_timeout_is_reported_as_timeout_without_leaking_device_code():
    fake, actions, _ = controller_fixture()
    WorkbenchController._complete_codex_account_task(fake, "codex:wait_login", {
        "success": False, "errorCode": "CODEX_LOGIN_TIMED_OUT", "status": {"authenticated": False}})
    assert actions[-1][0] == "codex:login_failed"
    assert actions[-1][1]["error_code"] == "CODEX_LOGIN_TIMED_OUT"
    assert not fake._codex_account_status["loginPending"]
    assert fake._codex_login_code == ""
    assert "SAMPLE-CODE" not in repr(actions)


def test_old_status_response_cannot_overwrite_a_new_successful_login():
    fake, actions, tasks = controller_fixture()
    fake.codex_account = SimpleNamespace(verified_status=lambda: {"authenticated": False})
    WorkbenchController.refreshCodexAccount(fake)
    old_result = tasks[-1][2]()
    WorkbenchController._complete_codex_account_task(fake, "codex:wait_login", {
        "success": True, "status": {"authenticated": True}})
    WorkbenchController._complete_codex_account_task(fake, "codex:status", old_result)
    assert fake._codex_account_status["authenticated"]
    assert actions[-1][0] == "codex:login_succeeded"
