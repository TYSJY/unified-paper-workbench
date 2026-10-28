from __future__ import annotations

from pathlib import Path
import re
import tomllib

import pytest

from unified_paper_orchestrator import __version__
import unified_paper_orchestrator.__main__ as cli_entry
import unified_paper_orchestrator.app as app_entry
import unified_paper_orchestrator.ui_next as ui_entry
import unified_paper_orchestrator_v2.__main__ as kernel_lab_entry
import unified_paper_orchestrator_v2.gateway as gateway_module


ROOT = Path(__file__).resolve().parents[1]


def test_app_routes_arguments_to_cli_without_opening_gui(monkeypatch):
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(cli_entry, "main", lambda argv: calls.append(("cli", argv)) or 7)
    monkeypatch.setattr(ui_entry, "main", lambda: calls.append(("ui", None)))

    assert app_entry.main(["doctor"]) == 7
    assert calls == [("cli", ["doctor"])]


def test_app_routes_empty_arguments_to_gui(monkeypatch):
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(app_entry, "_detach_frozen_gui_console", lambda: calls.append(("detach", None)))
    monkeypatch.setattr(cli_entry, "main", lambda argv: calls.append(("cli", argv)) or 0)
    monkeypatch.setattr(ui_entry, "main", lambda: calls.append(("ui", None)))

    assert app_entry.main([]) == 0
    assert calls == [("detach", None), ("ui", None)]


def test_app_fails_closed_without_reopening_classic_gui(monkeypatch):
    calls: list[BaseException] = []

    monkeypatch.setattr(app_entry, "_detach_frozen_gui_console", lambda: None)
    monkeypatch.setattr(
        ui_entry,
        "main",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic Qt startup failure")),
    )
    monkeypatch.setattr(app_entry, "_show_modern_startup_error", calls.append)

    assert app_entry.main([]) == 1
    assert len(calls) == 1
    assert isinstance(calls[0], RuntimeError)


def test_cli_no_longer_exposes_classic_ui_entry(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_entry.main(["ui-legacy"])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_version_is_auditable(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_entry.main(["--version"])

    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_distribution_status_is_read_only_and_auditable(capsys):
    assert cli_entry.main(["distribution-status"]) == 0
    output = capsys.readouterr().out
    assert '"schema": "uap.distribution-policy/v1"' in output
    assert '"edition": "internal"' in output
    assert '"commercial_enforcement": false' in output
    assert '"trusted_public_key_count": 0' in output


def test_release_version_sources_are_consistent():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version_info = (ROOT / "tools" / "windows_version_info.txt").read_text(
        encoding="utf-8"
    )
    build_script = (ROOT / "tools" / "build_exe.ps1").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:(?:a|b|rc)(\d+))?",
        __version__,
    )
    assert match is not None
    numeric = tuple(int(item or 0) for item in match.groups())

    assert pyproject["project"]["version"] == __version__
    assert f"filevers={numeric!r}" in version_info
    assert f"prodvers={numeric!r}" in version_info
    assert f"FileVersion', u'{__version__}'" in version_info
    assert f"ProductVersion', u'{__version__}'" in version_info
    assert f'ExpectedPattern "{re.escape(__version__)}"' in build_script
    assert f"校验版本为 `{__version__}`" in readme


def test_product_entrypoint_is_unique_and_internal_kernel_is_not_named_product_v2():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert scripts["paper-orchestrator"] == "unified_paper_orchestrator.__main__:main"
    assert scripts["paper-orchestrator-kernel-lab"] == "unified_paper_orchestrator_v2.__main__:main"
    assert "paper-orchestrator-v2" not in scripts
    assert "not the UnifiedPaperOrchestrator product entry" in kernel_lab_entry.build_parser().description


def test_build_pipeline_uses_isolated_python_processes():
    build_script = (ROOT / "tools" / "build_exe.ps1").read_text(encoding="utf-8")
    probe_script = (ROOT / "tools" / "isolated_build_probe.py").read_text(
        encoding="utf-8"
    )

    assert "Invoke-IsolatedPython" in build_script
    assert '$startInfo.Environment.Clear()' in build_script
    assert '"-S", "-P", "-B"' in build_script
    assert "& $python -m pytest" not in build_script
    assert "& $python -m PyInstaller" not in build_script
    assert "mesa_path_absent" in probe_script
    assert "editable_finder_absent" in probe_script
    assert '$environment["PSMODULEPATH"] = $trustedPowerShellModulePath' in build_script
    assert "trusted_powershell_path" in probe_script
    assert "trusted_psmodule_path" in probe_script
    assert "$trustedGitSha256" in build_script
    assert "Get-AuthenticodeSignature -LiteralPath $trustedGit" in build_script
    assert "$trustedGitHome" in build_script
    assert "trusted_git_path" in probe_script
    assert '$startInfo.Environment["USERPROFILE"] = $cliProbeProfile' in build_script
    assert '$startInfo.Environment["HOME"] = $cliProbeProfile' in build_script
    assert '"unified_paper_orchestrator.journal_author_sdk_runtime"' in build_script
    assert '"--console"' in build_script
    assert '"--hide-console"' in build_script
    assert '"hide-early"' in build_script
    assert '"unified_paper_orchestrator.ui_next.main"' in build_script
    assert '$qtUiDir;unified_paper_orchestrator/ui_next/qml' in build_script
    assert '$qtLocaleDir;unified_paper_orchestrator/ui_next/locales' in build_script
    assert '$commercialDistributionPolicy;unified_paper_orchestrator/commercial' in build_script
    assert '[ValidateSet("Internal", "CustomerClosedBeta")]' in build_script
    assert "CustomerClosedBeta builds require -CommercialPublicKeyPath." in build_script
    assert '@("distribution-status")' in build_script
    assert '"unified_paper_orchestrator.ui.main_window"' in build_script
    assert '"unified_paper_orchestrator.ui.account_site_manager"' in build_script


def test_gateway_status_cli_is_lazy_and_does_not_construct_v1_orchestrator(monkeypatch, capsys):
    class FakeGateway:
        def status(self):
            return {"mode": "explicit_dispatch_only", "canonical_writes_enabled": False}

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())
    monkeypatch.setattr(
        cli_entry,
        "UnifiedOrchestrator",
        lambda: (_ for _ in ()).throw(AssertionError("V1 orchestrator should not be constructed")),
    )

    assert cli_entry.main(["gateway-status"]) == 0
    payload = capsys.readouterr().out
    assert "explicit_dispatch_only" in payload
    assert '"canonical_writes_enabled": false' in payload


def test_gateway_review_cli_requires_explicit_execute_for_dispatch(monkeypatch, capsys):
    calls: list[str] = []

    class FakeGateway:
        def submit_review_canary(self, paper_id, **_kwargs):
            calls.append(f"submit:{paper_id}")
            return {
                "task_id": "task-1",
                "run_id": "run-1",
                "paper_id": paper_id,
                "stage": "writing",
                "action": "writing.review_round_1.canary",
                "executor": "openai_review",
                "status": "queued",
            }

        def run_once(self, task_id):
            calls.append(f"run:{task_id}")
            return {"task_id": task_id, "status": "completed"}

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())
    assert cli_entry.main(["gateway-review-canary", "2026-07-02_001_test-paper"]) == 0
    assert calls == ["submit:2026-07-02_001_test-paper"]
    assert '"status": "queued"' in capsys.readouterr().out

    assert cli_entry.main(
        ["gateway-review-canary", "2026-07-02_001_test-paper", "--execute"]
    ) == 0
    assert calls[-2:] == ["submit:2026-07-02_001_test-paper", "run:task-1"]


def test_codex_relay_cli_creates_only_an_approval_bound_task(monkeypatch, capsys):
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeGateway:
        def submit_codex_cli_relay_review_canary(self, paper_id, **kwargs):
            calls.append((paper_id, kwargs))
            return {
                "task_id": "task-cli",
                "paper_id": paper_id,
                "action": "writing.review_round_1.qilin_codex_cli_canary",
                "executor": "qilin_codex_cli_review",
                "status": "waiting_approval",
            }

        def run_once(self, _task_id):
            raise AssertionError("CLI task creation must not dispatch or auto-fallback")

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())
    path = Path("C:/standalone/codex.exe")
    assert (
        cli_entry.main(
            [
                "gateway-codex-relay-review-canary",
                "2026-07-02_001_test-paper",
                "--model",
                "gpt-5.5",
                "--codex-cli-path",
                str(path),
            ]
        )
        == 2
    )
    assert calls == [
        (
            "2026-07-02_001_test-paper",
            {
                "model": "gpt-5.5",
                "codex_cli_path": path,
                "token_budget": 50000,
                "max_output_tokens": 8000,
                "timeout_seconds": 600,
            },
        )
    ]
    assert "waiting_approval" in capsys.readouterr().out


def test_gateway_task_and_retry_cli_are_explicit_and_compact(monkeypatch, capsys):
    calls: list[str] = []

    class FakeGateway:
        def task(self, task_id):
            calls.append(f"task:{task_id}")
            return {"task_id": task_id, "status": "retryable_failure", "result": {"blocker_code": "TRANSIENT"}}

        def retry(self, task_id, **options):
            calls.append(f"retry:{task_id}:{options}")
            return {"task_id": task_id, "status": "queued", "attempt_count": 1}

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())
    assert cli_entry.main(["gateway-task", "task-1"]) == 1
    assert cli_entry.main(["gateway-retry", "task-1"]) == 0
    assert cli_entry.main(
        [
            "gateway-retry",
            "task-2",
            "--acknowledge-possible-duplicate-charge",
        ]
    ) == 0
    assert calls == [
        "task:task-1",
        "retry:task-1:{}",
        "retry:task-2:{'acknowledge_possible_duplicate_charge': True}",
    ]
    output = capsys.readouterr().out
    assert '"status": "retryable_failure"' in output
    assert '"status": "queued"' in output


def test_gateway_dispatch_unknown_task_returns_stable_nonzero_error(monkeypatch, capsys):
    class FakeGateway:
        def run_once(self, _task_id):
            raise gateway_module.GatewayError("GATEWAY_TASK_NOT_FOUND", "Gateway task does not exist.")

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())

    assert cli_entry.main(["gateway-dispatch", "task-missing"]) == 2
    output = capsys.readouterr().out
    assert "GATEWAY_TASK_NOT_FOUND" in output


def test_legacy_automation_cli_keeps_control_approval_separate(monkeypatch, capsys):
    calls: list[str] = []

    class FakeGateway:
        def submit_legacy_automation(self, operation, stage, *, operation_id=""):
            calls.append(f"submit:{operation}:{stage}:{operation_id}")
            return {"task_id": "task-control", "status": "waiting_approval"}

        def run_once(self, task_id):
            calls.append(f"run:{task_id}")
            return {"task_id": task_id, "status": "completed"}

        def approve(self, task_id, *, note="", expected_intent_sha256=""):
            calls.append(f"approve:{task_id}:{note}:{expected_intent_sha256}")
            return {"task_id": task_id, "status": "queued"}

    monkeypatch.setattr(gateway_module, "default_gateway", lambda: FakeGateway())

    assert cli_entry.main(
        [
            "gateway-legacy-automation",
            "start",
            "writing",
            "--operation-id",
            "op-1",
            "--execute",
        ]
    ) == 2
    assert calls == ["submit:start:writing:op-1"]
    assert cli_entry.main(
        [
            "gateway-approve",
            "task-control",
            "--note",
            "reviewed",
            "--intent-sha256",
            "a" * 64,
        ]
    ) == 0
    assert calls[-1] == f"approve:task-control:reviewed:{'a' * 64}"
    output = capsys.readouterr().out
    assert "waiting_approval" in output
    assert '"status": "queued"' in output


def test_v1_start_cli_reports_stable_error_without_leaking_exception_text(
    monkeypatch,
    capsys,
) -> None:
    class ControlError(RuntimeError):
        code = "LEGACY_CODEX_AUTOMATION_UNAVAILABLE"

    class FakeOrchestrator:
        def start_stage(self, _stage, single_active=False):
            raise ControlError("SECRET local path and provider input")

    monkeypatch.setattr(cli_entry, "UnifiedOrchestrator", FakeOrchestrator)

    assert cli_entry.main(["start-stage", "journal_author"]) == 2
    output = capsys.readouterr().out
    assert "LEGACY_CODEX_AUTOMATION_UNAVAILABLE" in output
    assert "SECRET" not in output
    assert "provider input" not in output
