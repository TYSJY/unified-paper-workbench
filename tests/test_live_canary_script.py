from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import unified_paper_orchestrator.__main__ as cli_entry
import unified_paper_orchestrator_v2.gateway as gateway_module


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "run_live_review_canary.ps1"
RELAY_SCRIPT = ROOT / "tools" / "run_live_relay_review_canary.ps1"
CODEX_RELAY_SCRIPT = ROOT / "tools" / "run_live_codex_relay_review_canary.ps1"
POWERSHELL = shutil.which("powershell.exe")


def _probe(
    proxy_url: str,
    *,
    script: Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = proxy_url
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Model",
            "transport-probe-only",
            "-TransportProbeOnly",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell contract")
def test_live_canary_script_copies_only_validated_loopback_proxy_without_disclosure() -> None:
    completed = _probe("http://127.0.0.1:9567")

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload == {
        "transport_policy": "explicit_loopback_proxy_no_env",
        "generic_proxy_environment_ignored": True,
        "explicit_proxy_url_disclosed": False,
        "proxy_validation_error": "",
    }
    assert "127.0.0.1" not in completed.stdout
    assert "9567" not in completed.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell contract")
def test_live_canary_script_does_not_copy_remote_environment_proxy() -> None:
    completed = _probe("http://proxy.invalid:9567")

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["transport_policy"] == "direct_no_env_proxy"
    assert payload["generic_proxy_environment_ignored"] is True
    assert payload["explicit_proxy_url_disclosed"] is False
    assert payload["proxy_validation_error"]
    assert "proxy.invalid" not in completed.stdout
    assert "9567" not in completed.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell contract")
def test_live_relay_canary_script_copies_only_validated_loopback_proxy_without_disclosure() -> None:
    completed = _probe("http://127.0.0.1:9567", script=RELAY_SCRIPT)

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload == {
        "transport_policy": "explicit_loopback_proxy_no_env",
        "generic_proxy_environment_ignored": True,
        "explicit_proxy_url_disclosed": False,
        "proxy_validation_error": "",
    }
    assert "127.0.0.1" not in completed.stdout
    assert "9567" not in completed.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell contract")
def test_live_relay_canary_script_does_not_copy_remote_environment_proxy() -> None:
    completed = _probe("http://proxy.invalid:9567", script=RELAY_SCRIPT)

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["transport_policy"] == "direct_no_env_proxy"
    assert payload["generic_proxy_environment_ignored"] is True
    assert payload["explicit_proxy_url_disclosed"] is False
    assert payload["proxy_validation_error"]
    assert "proxy.invalid" not in completed.stdout
    assert "9567" not in completed.stdout


def test_relay_canary_cli_only_creates_required_approval_task(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class FakeGateway:
        def submit_relay_review_canary(self, paper_id: str, **options):
            calls.append(("submit", paper_id, options))
            return {
                "task_id": "task_" + ("1" * 32),
                "run_id": "run_" + ("2" * 32),
                "paper_id": paper_id,
                "stage": "writing",
                "action": "writing.review_round_1.compatible_relay_canary",
                "executor": "openai_compatible_relay_review",
                "status": "waiting_approval",
                "approval": {
                    "status": "pending",
                    "intent_sha256": "a" * 64,
                    "intent": {"action": "writing.review_round_1.compatible_relay_canary"},
                },
            }

        def run_once(self, _task_id: str):
            raise AssertionError("relay task creation must never dispatch")

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())

    paper_id = "2026-07-02_001_test-paper"
    assert cli_entry.main(["gateway-relay-review-canary", paper_id]) == 2
    assert calls == [
        (
            "submit",
            paper_id,
            {
                "model": "gpt-5.5",
                "token_budget": 50000,
                "max_output_tokens": 8000,
                "timeout_seconds": 600,
            },
        )
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "waiting_approval"
    assert payload["action"] == "writing.review_round_1.compatible_relay_canary"
    assert payload["executor"] == "openai_compatible_relay_review"
    assert payload["approval"]["intent_sha256"] == "a" * 64


def test_relay_canary_cli_has_no_execute_shortcut() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "gateway-relay-review-canary",
                "2026-07-02_001_test-paper",
                "--execute",
            ]
        )

    assert exc_info.value.code == 2


def test_live_relay_canary_script_is_pinned_and_secret_fail_closed() -> None:
    source = RELAY_SCRIPT.read_text(encoding="utf-8")

    assert '"https://www.qilinapi.com"' in source
    assert '"$relayBaseUrl/responses"' in source
    assert '"gpt-5.5"' in source
    assert '"UNIFIED_RELAY_API_KEY"' in source
    assert '"UNIFIED_RELAY_HTTPS_PROXY"' in source
    assert 'Read-Host "Newly rotated relay API key (hidden; never written to disk)" -AsSecureString' in source
    assert 'Type RELAY to acknowledge the third-party disclosure' in source
    assert '[string]$RetryTaskId = ""' in source
    assert 'Type RETRY to requeue the exact approved relay task' in source
    assert 'gateway-task $taskId' in source
    assert 'gateway-retry $taskId `' in source
    assert '--acknowledge-possible-duplicate-charge' in source
    assert 'Its claimed upstream model identity cannot be verified.' in source
    assert 'will be sent to this third-party relay.' in source
    assert '"--data", "{}"' in source
    assert "& $curl --disable --noproxy= --proxy $ValidatedLoopbackProxy" in source
    assert '& $curl --disable --noproxy "*"' in source
    assert '$status -ne "401"' in source
    assert source.index("gateway-approve") < source.index("gateway-dispatch")
    assert "OPENAI_API_KEY" not in source
    assert ".codex" not in source.lower()
    assert "auth.json" not in source.lower()
    assert "config.toml" not in source.lower()


def test_isolated_codex_relay_script_keeps_desktop_identity_out_of_scope() -> None:
    source = CODEX_RELAY_SCRIPT.read_text(encoding="utf-8")

    assert '"gateway-codex-relay-review-canary"' not in source
    assert "gateway-codex-relay-review-canary $PaperId" in source
    assert "Get-AuthenticodeSignature" in source
    assert "Get-FileHash" in source
    assert "WindowsApps" in source
    assert 'Join-Path $env:USERPROFILE ".codex"' in source
    assert 'Read-Host "Qilin API key (hidden; held only in process memory)" -AsSecureString' in source
    assert source.index("Qilin API key (hidden") < source.index(
        "gateway-codex-relay-review-canary $PaperId"
    )
    assert "Type APPROVE" in source
    assert source.index("gateway-approve") < source.index("gateway-dispatch")
    assert 'SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $null' in source
    assert "gpt-5.6-sol" in source
    assert "Sol/Luna" in source
    assert "ultra" in source
    assert "config.toml" not in source.lower()
    assert "auth.json" not in source.lower()


def test_isolated_codex_relay_cli_has_no_execute_shortcut() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "gateway-codex-relay-review-canary",
                "2026-07-02_001_test-paper",
                "--execute",
            ]
        )
    assert exc_info.value.code == 2


def test_official_canary_preflight_disables_curlrc_and_binds_proxy_route() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--disable `" in source
    assert "--noproxy= `" in source
    assert '--noproxy "*" `' in source
