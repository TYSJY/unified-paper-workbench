from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from unified_paper_orchestrator_v2.contracts import ResultStatus, TaskEnvelope
from unified_paper_orchestrator_v2.executors.codex_cli_relay import (
    CODEX_CLI_EXECUTABLE_ENV,
    CODEX_CLI_REASONING_EFFORT,
    CodexCLIInvocation,
    CodexCLIProcessResult,
    CodexCLIRelayReviewExecutor,
    _argv,
    _minimal_child_environment,
    codex_cli_argv_policy_sha256,
    codex_cli_static_config,
    codex_cli_static_config_sha256,
    relay_key_fingerprint,
    resolve_native_codex_cli,
    sha256_file,
)
from unified_paper_orchestrator_v2.executors.openai_relay import (
    RELAY_API_KEY_ENV,
    RELAY_MODEL_ENV,
)
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway
from unified_paper_orchestrator_v2.review_canary import REQUIRED_HEADINGS
from unified_paper_orchestrator_v2.staging import StagingStore
from tools import verify_release_044


PAPER_ID = "2026-07-02_001_codex-cli-isolation"
RELAY_SECRET = "relay-test-secret-never-persist"
THREAD_ID = "12345678-1234-4234-8234-123456789abc"


def _native_executable(root: Path) -> Path:
    executable = root / "standalone" / ("codex.exe" if os.name == "nt" else "codex")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"pinned native codex test binary")
    return executable.resolve()


def _task(tmp_path: Path, executable: Path) -> TaskEnvelope:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return TaskEnvelope(
        paper_id=PAPER_ID,
        stage="writing",
        action="writing.review_round_1.qilin_codex_cli_canary",
        executor="qilin_codex_cli_review",
        payload={
            **codex_cli_static_config(),
            "endpoint_config_sha256": codex_cli_static_config_sha256(),
            "argv_policy_sha256": codex_cli_argv_policy_sha256(),
            "third_party_data_disclosure_ack": True,
            "codex_cli_path": str(executable),
            "codex_cli_sha256": sha256_file(executable),
            "relay_key_fingerprint": relay_key_fingerprint(RELAY_SECRET),
            "model": "gpt-5.5",
            "_prepared_instructions": "Return the requested review only.",
            "_prepared_input": "immutable prepared input",
            "_estimated_input_tokens": 20,
            "max_output_tokens": 8000,
        },
        allowed_tools=[],
        writable_roots=[str(staging.resolve())],
        network_access=True,
        token_budget=50000,
        timeout_seconds=60,
    )


def _success_jsonl(output: str = "review output") -> str:
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": output},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 50,
                "reasoning_output_tokens": 10,
            },
        },
    ]
    return "\n".join(json.dumps(item) for item in events) + "\n"


def _configs(argv: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, item in enumerate(argv[:-1]):
        if item == "-c":
            key, value = argv[index + 1].split("=", 1)
            values[key] = value
    return values


def test_cli_argv_uses_api_xhigh_without_desktop_identity(tmp_path: Path) -> None:
    executable = _native_executable(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    argv = _argv(executable, model="gpt-5.5", workdir=workdir)
    configs = _configs(argv)
    serialized = "\0".join(argv)

    assert argv.count("--ignore-user-config") == 1
    assert argv.count("--ephemeral") == 1
    assert argv.count("--ignore-rules") == 1
    assert configs["model_reasoning_effort"] == json.dumps(
        CODEX_CLI_REASONING_EFFORT
    )
    assert configs["model_providers.qilin_relay.requires_openai_auth"] == "false"
    assert configs["analytics.enabled"] == "false"
    assert configs["feedback.enabled"] == "false"
    assert configs["otel.metrics_exporter"] == '"none"'
    for feature in (
        "browser_use",
        "in_app_browser",
        "shell_tool",
        "plugins",
        "skill_mcp_dependency_install",
    ):
        assert ("--disable", feature) in zip(argv, argv[1:], strict=False)
    assert "gpt-5.6-sol" not in serialized
    assert "ultra" not in serialized
    assert RELAY_SECRET not in serialized
    assert "auth.json" not in serialized
    assert "config.toml" not in serialized


def test_child_profile_and_secrets_are_fully_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = (tmp_path / "desktop-profile").resolve()
    runtime = (tmp_path / "runtime").resolve()
    home = runtime / "home"
    temp = runtime / "temp"
    for name, value in {
        "USERPROFILE": desktop,
        "HOME": desktop,
        "APPDATA": desktop / "AppData" / "Roaming",
        "LOCALAPPDATA": desktop / "AppData" / "Local",
        "TEMP": desktop / "Temp",
        "TMP": desktop / "Temp",
        "CODEX_HOME": desktop / ".codex",
        "OPENAI_API_KEY": "desktop-api-secret",
        "OPENAI_BASE_URL": "https://desktop.invalid/v1",
        "DESKTOP_SESSION_MODEL": "gpt-5.6-sol",
        "DESKTOP_SESSION_REASONING_EFFORT": "ultra",
        "UNRELATED_SECRET": "unrelated-secret",
    }.items():
        monkeypatch.setenv(name, str(value))
    before = dict(os.environ)

    env = _minimal_child_environment(
        runtime_home=home,
        runtime_temp=temp,
        api_key=RELAY_SECRET,
        proxy_url="",
    )

    for name in (
        "CODEX_HOME",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    ):
        value = Path(env[name]).resolve()
        assert value == runtime or runtime in value.parents
        assert value.exists() and value.is_dir()
        assert value != desktop and desktop not in value.parents
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DESKTOP_SESSION_MODEL",
        "DESKTOP_SESSION_REASONING_EFFORT",
        "UNRELATED_SECRET",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        assert name not in env
    assert env[RELAY_API_KEY_ENV] == RELAY_SECRET
    assert dict(os.environ) == before


def test_desktop_owned_binary_and_runtime_paths_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = tmp_path / "desktop"
    user_codex = desktop / ".codex"
    executable = user_codex / "bin" / ("codex.exe" if os.name == "nt" else "codex")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"desktop-owned")
    monkeypatch.setenv("USERPROFILE", str(desktop))
    monkeypatch.setenv("HOME", str(desktop))

    with pytest.raises(ValueError, match="Desktop-owned"):
        resolve_native_codex_cli(executable)

    safe_executable = _native_executable(tmp_path)
    staging = user_codex / "staging"
    staging.mkdir(parents=True)
    task = _task(tmp_path, safe_executable)
    task.writable_roots = [str(staging.resolve())]
    calls: list[CodexCLIInvocation] = []

    def runner(invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
        calls.append(invocation)
        raise AssertionError("desktop-owned runtime must be rejected before launch")

    monkeypatch.setenv(RELAY_API_KEY_ENV, RELAY_SECRET)
    result = CodexCLIRelayReviewExecutor(process_runner=runner).execute(task)
    assert result.status is ResultStatus.BLOCKED
    assert result.blocker_code == "RELAY_CODEX_CLI_RUNTIME_ROOT_INVALID"
    assert calls == []
    assert not (staging / ".codex_cli_runtime").exists()


@pytest.mark.parametrize("failure_phase", ["version", "request"])
def test_launch_errors_are_compact_and_never_fall_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    executable = _native_executable(tmp_path)
    task = _task(tmp_path, executable)
    monkeypatch.setenv(RELAY_API_KEY_ENV, RELAY_SECRET)
    calls = 0

    def runner(_invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
        nonlocal calls
        calls += 1
        if failure_phase == "version" or calls == 2:
            return CodexCLIProcessResult(
                returncode=None,
                stdout="",
                stderr="",
                timed_out=False,
                duration_ms=1,
                launch_error_type="PermissionError",
            )
        return CodexCLIProcessResult(
            returncode=0,
            stdout="codex-cli 0.125.0\n",
            stderr="",
            timed_out=False,
            duration_ms=1,
        )

    result = CodexCLIRelayReviewExecutor(process_runner=runner).execute(task)
    assert result.status is ResultStatus.BLOCKED
    assert result.blocker_code == "RELAY_CODEX_CLI_LAUNCH_FAILED"
    assert calls == (1 if failure_phase == "version" else 2)
    assert RELAY_SECRET not in json.dumps(result.to_dict())


def test_executor_never_reads_or_modifies_desktop_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = tmp_path / "desktop"
    desktop_codex = desktop / ".codex"
    desktop_codex.mkdir(parents=True)
    sentinels = {
        desktop_codex / "config.toml": b'model = "gpt-5.6-sol"\neffort = "ultra"\n',
        desktop_codex / "auth.json": b'{"desktop":"chatgpt-login"}',
        desktop_codex / "AGENTS.md": b"desktop instructions",
    }
    for path, content in sentinels.items():
        path.write_bytes(content)
    snapshot = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sentinels
    }
    for name, value in {
        "USERPROFILE": desktop,
        "HOME": desktop,
        "APPDATA": desktop / "AppData" / "Roaming",
        "LOCALAPPDATA": desktop / "AppData" / "Local",
        "TEMP": desktop / "Temp",
        "TMP": desktop / "Temp",
        "CODEX_HOME": desktop_codex,
        "OPENAI_API_KEY": "desktop-openai-key",
        "DESKTOP_SESSION_MODEL": "gpt-5.6-sol",
        "DESKTOP_SESSION_REASONING_EFFORT": "ultra",
        RELAY_API_KEY_ENV: RELAY_SECRET,
    }.items():
        monkeypatch.setenv(name, str(value))
    executable = _native_executable(tmp_path)
    task = _task(tmp_path, executable)
    invocations: list[CodexCLIInvocation] = []

    def runner(invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
        invocations.append(invocation)
        for name in (
            "CODEX_HOME",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
        ):
            value = Path(invocation.env[name]).resolve()
            assert desktop not in value.parents and value != desktop
        assert "OPENAI_API_KEY" not in invocation.env
        assert "DESKTOP_SESSION_MODEL" not in invocation.env
        if "--version" in invocation.argv:
            return CodexCLIProcessResult(0, "codex-cli 0.125.0\n", "", False, 1)
        assert RELAY_SECRET not in "\0".join(invocation.argv)
        assert RELAY_SECRET not in invocation.stdin_text
        return CodexCLIProcessResult(0, _success_jsonl(), "", False, 1)

    result = CodexCLIRelayReviewExecutor(process_runner=runner).execute(task)
    assert result.status is ResultStatus.COMPLETED
    assert result.metrics["total_tokens"] == 150
    assert result.metrics["usage_total_source"] == "input_plus_output_v1"
    assert result.data["chatgpt_subscription_used"] is False
    assert result.data["openai_api_account_used"] is False
    assert result.data["requested_model"] == "gpt-5.5"
    assert result.data["reported_model"] == ""
    assert len(invocations) == 2
    for path, (content, mtime_ns) in snapshot.items():
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == mtime_ns
    assert list((Path(task.writable_roots[0]) / ".codex_cli_runtime").glob("*")) == []
    assert RELAY_SECRET not in json.dumps(result.to_dict())


def test_cli_budget_preflight_stops_before_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _native_executable(tmp_path)
    task = _task(tmp_path, executable)
    task.payload["max_output_tokens"] = 90
    task.payload["_estimated_input_tokens"] = 20
    task.token_budget = 100
    monkeypatch.setenv(RELAY_API_KEY_ENV, RELAY_SECRET)
    calls: list[CodexCLIInvocation] = []

    def runner(invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
        calls.append(invocation)
        raise AssertionError("invalid budget must stop before the CLI process")

    result = CodexCLIRelayReviewExecutor(process_runner=runner).execute(task)
    assert result.status is ResultStatus.FATAL_FAILURE
    assert result.blocker_code == "RELAY_CODEX_CLI_TOKEN_BUDGET_PREFLIGHT"
    assert calls == []


def test_cli_output_limit_is_explicitly_posthoc_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _native_executable(tmp_path)
    task = _task(tmp_path, executable)
    task.payload["max_output_tokens"] = 10
    monkeypatch.setenv(RELAY_API_KEY_ENV, RELAY_SECRET)

    def runner(invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
        if "--version" in invocation.argv:
            return CodexCLIProcessResult(0, "codex-cli 0.125.0\n", "", False, 1)
        return CodexCLIProcessResult(0, _success_jsonl(), "", False, 1)

    result = CodexCLIRelayReviewExecutor(process_runner=runner).execute(task)
    assert result.status is ResultStatus.FATAL_FAILURE
    assert result.blocker_code == "RELAY_CODEX_CLI_OUTPUT_LIMIT_EXCEEDED"
    assert result.metrics["posthoc_output_token_limit"] == 10
    assert "output_text" not in result.data


def _writing_root(tmp_path: Path) -> Path:
    root = tmp_path / "writing"
    (root / "prompts").mkdir(parents=True)
    (root / "ideas").mkdir()
    (root / "refs" / PAPER_ID).mkdir(parents=True)
    (root / "papers" / PAPER_ID).mkdir(parents=True)
    (root / "prompts" / "07_review_round_1.md").write_text(
        "$academic-paper-reviewer\n\n# immutable test instructions\n",
        encoding="utf-8",
    )
    (root / "ideas" / f"{PAPER_ID}.md").write_text("idea", encoding="utf-8")
    (root / "refs" / PAPER_ID / "references.md").write_text("refs", encoding="utf-8")
    (root / "refs" / PAPER_ID / "literature_map.md").write_text("map", encoding="utf-8")
    (root / "refs" / PAPER_ID / "novelty_check.md").write_text("novelty", encoding="utf-8")
    (root / "papers" / PAPER_ID / "draft_v3.tex").write_text(
        "\\begin{document}\n\\begin{figure}x\\end{figure}\n"
        "\\begin{table}x\\end{table}\n\\bibitem{x} x\n\\end{document}\n",
        encoding="utf-8",
    )
    return root


def _valid_review() -> str:
    parts: list[str] = []
    for heading in REQUIRED_HEADINGS:
        parts.append(heading)
        if heading == "# Review Round 1":
            continue
        if heading == "## Overall Verdict":
            parts.append("Reject")
        elif heading == "## Figure and Quantitative Evidence Review":
            parts.extend(
                [
                    "Figure total: 1",
                    "Table total: 1",
                    "Conceptual figure count: 0",
                    "Quantitative evidence figure count: 1",
                    "Hybrid figure count: 0",
                    "Table-disguised-as-figure count: 0",
                    "",
                    "| Fig. | Caption Summary | Current Type | Conceptual? | Quantitative? | Table-like? | Keep / Convert / Replace | Recommended New Type | Required Data / CSV Schema |",
                    "|---|---|---|---|---|---|---|---|---|",
                    "| 1 | Result | plot | No | Yes | No | Keep | plot | method,value,error bars |",
                ]
            )
        elif heading == "## Required Revisions":
            parts.append("- [ ] Add reproducibility details and evidence.")
        else:
            parts.append("Add evidence, novelty analysis, and reproducibility details.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def test_gateway_approves_exact_cli_identity_and_writes_v3_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _native_executable(tmp_path)
    writing_root = _writing_root(tmp_path)
    monkeypatch.setenv(CODEX_CLI_EXECUTABLE_ENV, str(executable))
    monkeypatch.setenv(RELAY_API_KEY_ENV, RELAY_SECRET)
    monkeypatch.setenv(RELAY_MODEL_ENV, "gpt-5.5")

    def runner(invocation: CodexCLIInvocation) -> CodexCLIProcessResult:
        if "--version" in invocation.argv:
            return CodexCLIProcessResult(0, "codex-cli 0.125.0\n", "", False, 1)
        return CodexCLIProcessResult(0, _success_jsonl(_valid_review()), "", False, 2)

    executor = CodexCLIRelayReviewExecutor(process_runner=runner)
    registry = ExecutorRegistry()
    registry.register(executor)
    staging = StagingStore(tmp_path / "s", acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging.root,
        staging_store=staging,
        registry=registry,
        review_writing_root=writing_root,
    )

    queued = gateway.submit_codex_cli_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        codex_cli_path=executable,
        writing_root=writing_root,
    )
    assert queued["status"] == "waiting_approval"
    intent = queued["approval"]["intent"]
    assert intent["client_kind"] == "pinned_native_codex_cli_binary"
    assert intent["reasoning_effort"] == "xhigh"
    assert intent["chatgpt_subscription_used"] is False
    assert intent["openai_api_account_used"] is False
    assert intent["fallback_policy"] == "no_cross_executor_or_identity_fallback_v1"
    assert intent["relay_key_fingerprint"] == relay_key_fingerprint(RELAY_SECRET)

    gateway.approve(
        str(queued["task_id"]),
        expected_intent_sha256=str(queued["approval"]["intent_sha256"]),
    )
    completed = gateway.run_once(str(queued["task_id"]))
    assert completed is not None and completed["status"] == "completed", json.dumps(
        completed, ensure_ascii=False, indent=2
    )
    data = completed["result"]["data"]
    manifest = json.loads(
        (Path(data["staging_dir"]) / "result.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "review_canary_result_v3"
    assert manifest["provider_external_ref_kind"] == "codex_thread_id"
    assert manifest["provider_external_ref"] == THREAD_ID
    assert "response_id" not in manifest
    assert manifest["requested_model"] == "gpt-5.5"
    assert manifest["reported_model"] == ""
    assert manifest["provider_response_id"] == ""
    assert manifest["chatgpt_subscription_used"] is False
    assert manifest["openai_api_account_used"] is False
    assert manifest["billing_credential_fingerprint"] == relay_key_fingerprint(
        RELAY_SECRET
    )
    serialized = json.dumps(completed) + json.dumps(gateway.store.list_events())
    assert RELAY_SECRET not in serialized
    assert "gpt-5.6-sol" not in serialized
    assert "ultra" not in serialized


def test_release_verifier_recognizes_only_the_exact_cli_route() -> None:
    assert (
        verify_release_044._route_kind(
            "writing.review_round_1.qilin_codex_cli_canary",
            "qilin_codex_cli_review",
        )
        == "qilin_codex_cli_relay"
    )
    assert (
        verify_release_044._route_kind(
            "writing.review_round_1.qilin_codex_cli_canary",
            "openai_compatible_relay_review",
        )
        == ""
    )
    assert verify_release_044.CODEX_CLI_PROVIDER_PAYLOAD[
        "chatgpt_subscription_used"
    ] is False
    assert verify_release_044.CODEX_CLI_PROVIDER_PAYLOAD[
        "openai_api_account_used"
    ] is False
    assert verify_release_044.CODEX_CLI_PROVIDER_PAYLOAD[
        "upstream_model_identity_verified"
    ] is False
