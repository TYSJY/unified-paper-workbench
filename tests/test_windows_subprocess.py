from __future__ import annotations

import subprocess
from types import SimpleNamespace

from unified_paper_orchestrator import windows_subprocess


def test_no_window_kwargs_preserve_existing_creation_flags() -> None:
    result = windows_subprocess.no_window_subprocess_kwargs(
        {"creationflags": 0x00000200}
    )

    if windows_subprocess.os.name == "nt":
        assert result["creationflags"] & 0x00000200
        assert result["creationflags"] & windows_subprocess.CREATE_NO_WINDOW
        assert result["startupinfo"].wShowWindow == windows_subprocess.SW_HIDE
    else:
        assert result == {"creationflags": 0x00000200}


def test_sdk_subprocess_proxy_hides_only_its_child_process() -> None:
    calls: list[dict] = []

    def fake_popen(*_args, **kwargs):
        calls.append(dict(kwargs))
        return "process"

    fake_module = SimpleNamespace(Popen=fake_popen, PIPE=object())
    proxy = windows_subprocess._HiddenSubprocessProxy(fake_module)

    assert proxy.PIPE is fake_module.PIPE
    assert proxy.Popen(["codex.exe", "app-server"]) == "process"
    if windows_subprocess.os.name == "nt":
        assert calls[0]["creationflags"] & windows_subprocess.CREATE_NO_WINDOW
        assert calls[0]["startupinfo"].wShowWindow == windows_subprocess.SW_HIDE
    else:
        assert calls[0] == {}


def test_openai_codex_patch_does_not_replace_global_subprocess_popen() -> None:
    import openai_codex.client as codex_client

    original_client_subprocess = codex_client.subprocess
    original_global_popen = subprocess.Popen
    try:
        if getattr(original_client_subprocess, "_unified_no_window_proxy", False):
            codex_client.subprocess = original_client_subprocess._module
            original_client_subprocess = codex_client.subprocess
        configured = windows_subprocess.configure_openai_codex_no_window()
        if windows_subprocess.os.name == "nt":
            assert configured is True
            assert getattr(codex_client.subprocess, "_unified_no_window_proxy", False)
        else:
            assert configured is False
        assert subprocess.Popen is original_global_popen
    finally:
        codex_client.subprocess = original_client_subprocess
