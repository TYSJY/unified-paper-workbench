from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from unified_paper_orchestrator import windows_security


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL helper")
def test_current_user_sid_uses_value_free_csv_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, *, env=None):
        captured["args"] = args
        captured["env"] = env
        return subprocess.CompletedProcess([], 0, '"HOST\\User","S-1-5-21-123"\r\n', "")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid/v1")
    monkeypatch.setattr(
        windows_security,
        "_run_hidden",
        fake_run,
    )

    assert windows_security.current_user_sid() == "S-1-5-21-123"
    assert Path(str(captured["args"][0])).is_absolute()
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "OPENAI_BASE_URL" not in captured["env"]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL helper")
def test_private_acl_uses_sid_argv_without_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "credential_store.local.json"
    target.write_text("{}", encoding="utf-8")
    calls: list[tuple[Path, str, bool]] = []
    monkeypatch.setattr(windows_security, "current_user_sid", lambda: "S-1-5-21-123")
    monkeypatch.setattr(
        windows_security,
        "_apply_or_verify_private_acl",
        lambda path, sid, *, apply: calls.append((path, sid, apply)),
    )

    windows_security.restrict_private_path(target)

    assert calls == [(target, "S-1-5-21-123", True)]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL helper")
def test_private_acl_live_roundtrip_uses_trusted_system_tools(tmp_path: Path) -> None:
    target = tmp_path / "private-staging"
    target.mkdir()
    sid = windows_security.current_user_sid()

    windows_security.restrict_private_path(target)
    windows_security._verify_private_acl(target, {sid, "S-1-5-18", "S-1-5-32-544"})
