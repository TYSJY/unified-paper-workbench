from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tools.workflow_candidate import main as candidate_main
from unified_paper_orchestrator.workflow.candidate import (
    CANDIDATE_DATABASE_ID_KEY,
    CANDIDATE_DATABASE_SCHEMA_KEY,
    CANDIDATE_WORKSPACE_SCHEMA,
    CANDIDATE_WORKSPACE_MARKER,
    CandidateApplication,
)
from unified_paper_orchestrator.workflow.packs import UNDERGRADUATE_PROMPT_SHA256
from unified_paper_orchestrator.workflow.store import WorkflowStore


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_candidate_initializes_all_packs_and_materializes_stable_prompts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    app = CandidateApplication(workspace)

    initialized = app.initialize_defaults()
    binding = app.materialize_undergraduate_runtime_binding()
    doctor = app.doctor()

    assert initialized["installed_workflows"] == 6
    assert len(app.list_workflows()["workflows"]) == 6
    assert len(binding.resources) == 6
    expected_digests = set(UNDERGRADUATE_PROMPT_SHA256.values())
    assert {resource.content_sha256 for resource in binding.resources} == expected_digests
    assert all(Path(resource.absolute_path).parent == app.paths.prompt_store for resource in binding.resources)
    assert all("_MEIPASS" not in resource.absolute_path for resource in binding.resources)
    assert all(
        hashlib.sha256(Path(resource.absolute_path).read_bytes()).hexdigest() == resource.content_sha256
        for resource in binding.resources
    )
    assert doctor["status"] == "PASS"
    assert doctor["online_execution"] == "NOT_RUN"
    assert doctor["production_promotion_eligible"] is False
    assert {item["id"]: item["status"] for item in doctor["checks"]} == {
        "default-packs": "PASS",
        "undergraduate-prompt-seals": "PASS",
        "sci-prompt-seals": "NOT_RUN",
    }
    assert os.environ["UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"] == "1"


def test_candidate_cli_localizes_and_recovers_a_demo_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "cli"
    common = ["--workspace", str(workspace)]

    assert candidate_main([*common, "--locale", "zh-CN", "init"]) == 0
    initialized = _json_output(capsys)
    assert initialized["locale"] == "zh-CN"
    assert initialized["message"] == "已初始化通用工作流候选工作区。"

    assert candidate_main([*common, "--locale", "en-US", "list"]) == 0
    listed = _json_output(capsys)
    assert listed["message"] == "The workflow library was read."
    assert len(listed["workflows"]) == 6

    assert (
        candidate_main(
            [
                *common,
                "--locale",
                "en-US",
                "demo-create",
                "--goal",
                "Restart-safe demo",
                "--action",
                "prepare",
                "--action",
                "verify",
                "--constraint",
                "offline",
                "--idempotency-key",
                "candidate-cli-restart",
            ]
        )
        == 0
    )
    created = _json_output(capsys)
    run_id = str(created["run"]["run_id"])

    assert candidate_main([*common, "--locale", "en-US", "tick", run_id]) == 0
    ticked = _json_output(capsys)
    assert ticked["status"] == "running"
    assert ticked["scheduler"]["completed"] == 1

    # Each CLI invocation constructs a new application and reopens the ledger.
    assert candidate_main([*common, "--locale", "zh-CN", "resume", run_id]) == 0
    resumed = _json_output(capsys)
    assert resumed["status"] == "completed"
    assert resumed["locale"] == "zh-CN"

    assert candidate_main([*common, "--locale", "en-US", "timeline", run_id]) == 0
    timeline = _json_output(capsys)
    assert timeline["run_id"] == run_id
    assert timeline["status"] == "completed"
    assert timeline["progress"] == 1.0


def test_candidate_no_args_menu_is_non_writing_until_selected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "w"
    monkeypatch.setattr(
        "tools.workflow_candidate._interactive_default_workspace",
        lambda: workspace,
    )
    monkeypatch.setattr("tools.workflow_candidate._stdin_is_interactive", lambda: True)
    answers = iter(["0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert candidate_main([]) == 0
    output = capsys.readouterr().out
    assert "通用工作流候选工具" in output
    assert "候选工具已安全退出" in output
    assert not workspace.exists()


def test_candidate_no_args_quick_start_completes_in_safe_default_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "w"
    monkeypatch.setattr(
        "tools.workflow_candidate._interactive_default_workspace",
        lambda: workspace,
    )
    monkeypatch.setattr("tools.workflow_candidate._stdin_is_interactive", lambda: True)
    answers = iter(["1", "", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert candidate_main([]) == 0
    output = capsys.readouterr().out
    assert "快速开始完成" in output
    assert "自检=PASS" in output
    assert "演示=completed" in output
    app = CandidateApplication(workspace)
    assert len(app.list_workflows()["workflows"]) == 6
    runs = app.library.store.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"


def test_candidate_no_args_non_tty_eof_and_interrupt_do_not_hang(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr("tools.workflow_candidate._stdin_is_interactive", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": pytest.fail("non-TTY launch must not read stdin"),
    )
    assert candidate_main([]) == 2
    assert "零参数交互菜单需要" in capsys.readouterr().out

    monkeypatch.setattr("tools.workflow_candidate._stdin_is_interactive", lambda: True)

    def end_of_input(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", end_of_input)
    assert candidate_main([]) == 2
    assert "交互输入已关闭" in capsys.readouterr().out

    def interrupt(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert candidate_main([]) == 130
    assert "已由用户中止" in capsys.readouterr().out


def test_candidate_menu_survives_invalid_default_and_switch_is_atomic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_default = tmp_path / "default-is-a-file"
    invalid_default.write_text("sentinel", encoding="utf-8")
    valid_workspace = tmp_path / "valid-workspace"
    monkeypatch.setattr(
        "tools.workflow_candidate._interactive_default_workspace",
        lambda: invalid_default,
    )
    monkeypatch.setattr("tools.workflow_candidate._stdin_is_interactive", lambda: True)
    answers = iter(["6", str(valid_workspace), "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert candidate_main([]) == 0
    output = capsys.readouterr().out
    assert "不可用（路径不是目录）" in output
    assert "已切换本次菜单的工作区" in output
    assert invalid_default.read_text(encoding="utf-8") == "sentinel"
    assert not valid_workspace.exists()

    initial_workspace = tmp_path / "initial-workspace"
    invalid_switch = tmp_path / "switch-is-a-file"
    invalid_switch.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(
        "tools.workflow_candidate._interactive_default_workspace",
        lambda: initial_workspace,
    )
    answers = iter(["6", str(invalid_switch), "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert candidate_main([]) == 0
    output = capsys.readouterr().out
    assert "candidate workspace must be a directory" in output
    assert "已切换本次菜单的工作区" not in output
    assert output.count(f"当前工作区：{initial_workspace}") == 2
    assert invalid_switch.read_text(encoding="utf-8") == "sentinel"
    assert not initial_workspace.exists()


def test_candidate_menu_rejects_relative_and_production_workspace_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production = tmp_path / "production"
    production.mkdir()
    manifest = production / "workspace.json"
    sentinel = b'{"schema":"unified-paper-orchestrator/workspace/v1"}\n'
    manifest.write_bytes(sentinel)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr("tools.workflow_candidate._stdin_is_interactive", lambda: True)
    answers = iter(["6", "relative-menu-path", "6", str(production), "2", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert candidate_main([]) == 0
    output = capsys.readouterr().out
    assert "只接受绝对工作区路径" in output
    assert "production workspaces cannot be claimed" in output
    assert manifest.read_bytes() == sentinel
    assert not (production / CANDIDATE_WORKSPACE_MARKER).exists()
    assert not (production / "state").exists()


def test_offline_acceptance_runs_real_mechanics_and_states_online_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "registry" / "workspaces.json"
    registry.parent.mkdir()
    registry.write_bytes(b'{"production":"sentinel"}\n')
    monkeypatch.setenv("UNIFIED_PAPER_WORKSPACE_REGISTRY", str(registry))
    app = CandidateApplication(tmp_path / "acceptance")

    report = app.accept_offline()
    checks = {item["id"]: item for item in report["checks"]}

    assert report["offline_candidate_acceptance"] == "PASS_WITH_LIMITATIONS"
    assert checks["deterministic-concurrency-and-custom-clone"]["status"] == "PASS"
    assert checks["deterministic-concurrency-and-custom-clone"]["evidence"]["maximum_simultaneous_normalizers"] == 2
    assert checks["undergraduate-six-node-chain"]["status"] == "PASS"
    assert len(checks["undergraduate-six-node-chain"]["evidence"]["nodes"]) == 6
    assert checks["restart-recovery"]["status"] == "PASS"
    assert checks["restart-recovery"]["evidence"]["version_id_unchanged"] is True
    assert checks["durable-retry"]["status"] == "PASS"
    assert checks["durable-retry"]["evidence"]["attempt_statuses"] == [
        "retryable-failure",
        "completed",
    ]
    assert checks["workspace-isolation"]["status"] == "PASS"
    assert checks["two-subject-sci-four-workflow-smoke"]["status"] == "NOT_RUN"
    assert checks["workspace-registry-unchanged"]["status"] == "PASS"
    assert (
        checks["workspace-registry-unchanged"]["evidence"]["before"]
        == checks["workspace-registry-unchanged"]["evidence"]["after"]
    )
    assert registry.read_bytes() == b'{"production":"sentinel"}\n'
    assert report["online_production_acceptance"]["status"] == "NOT_RUN"
    assert report["online_production_acceptance"]["production_equivalence"] is False
    assert report["production_promotion_eligible"] is False


def test_acceptance_report_is_contained_link_safe_and_non_overwriting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = CandidateApplication(workspace)
    app.initialize_defaults()
    report = {"status": "fixture"}

    written = app.write_acceptance_report(report, Path("reports") / "acceptance.json")
    assert written == workspace / "reports" / "acceptance.json"
    assert json.loads(written.read_text(encoding="utf-8")) == report
    with pytest.raises(FileExistsError):
        app.write_acceptance_report(report, written)
    with pytest.raises(ValueError, match="within the explicit workspace"):
        app.write_acceptance_report(report, tmp_path / "outside.json")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - depends on Windows developer mode/ACL
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink or reparse point"):
        app.write_acceptance_report(report, link / "escaped.json")


def test_candidate_workspace_marker_prevents_foreign_workspace_writes(tmp_path: Path) -> None:
    foreign = tmp_path / "production-workspace"
    database = foreign / "state" / "v2" / "orchestrator.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"production-ledger-sentinel")

    app = CandidateApplication(foreign)
    with pytest.raises(ValueError, match="non-empty workspace"):
        app.initialize_defaults()
    with pytest.raises(RuntimeError, match="ownership marker is missing"):
        app.list_workflows()

    assert database.read_bytes() == b"production-ledger-sentinel"
    assert not (foreign / CANDIDATE_WORKSPACE_MARKER).exists()
    assert not (foreign / "state" / "v2" / "resources").exists()

    empty = tmp_path / "empty-candidate"
    empty.mkdir()
    initialized = CandidateApplication(empty).initialize_defaults()
    assert initialized["status"] == "initialized"
    marker = empty / CANDIDATE_WORKSPACE_MARKER
    assert marker.is_file()
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["status"] == "ready"
    assert marker_payload["workspace_id"] == initialized["workspace_id"]
    with sqlite3.connect(initialized["database"]) as connection:
        metadata = dict(
            connection.execute(
                "SELECT key, value FROM workflow_schema_meta WHERE key IN (?, ?)",
                (CANDIDATE_DATABASE_SCHEMA_KEY, CANDIDATE_DATABASE_ID_KEY),
            )
        )
    assert metadata == {
        CANDIDATE_DATABASE_SCHEMA_KEY: CANDIDATE_WORKSPACE_SCHEMA,
        CANDIDATE_DATABASE_ID_KEY: marker_payload["workspace_id"],
    }
    assert CandidateApplication(empty).doctor()["status"] == "PASS"


def test_candidate_workspace_symlink_is_rejected_before_database_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "candidate-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - depends on Windows developer mode/ACL
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="symlinks or reparse points"):
        CandidateApplication(link).initialize_defaults()
    assert not (outside / "state").exists()
    assert not (outside / CANDIDATE_WORKSPACE_MARKER).exists()


def test_candidate_rejects_reparse_state_and_sqlite_sidecars_on_reopen(tmp_path: Path) -> None:
    workspace = tmp_path / "state-link-workspace"
    app = CandidateApplication(workspace)
    app.initialize_defaults()
    state = workspace / "state"
    preserved_state = tmp_path / "preserved-state"
    state.replace(preserved_state)
    try:
        state.symlink_to(preserved_state, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - depends on Windows developer mode/ACL
        preserved_state.replace(state)
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    database = preserved_state / "v2" / "orchestrator.sqlite3"
    digest_before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="symlinks or reparse points"):
        CandidateApplication(workspace).doctor()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest_before

    sidecar_workspace = tmp_path / "sidecar-link-workspace"
    sidecar_app = CandidateApplication(sidecar_workspace)
    sidecar_app.initialize_defaults()
    external = tmp_path / "external-sidecar-sentinel"
    external.write_bytes(b"external-sidecar-sentinel")
    journal = sidecar_app.paths.database.with_name(f"{sidecar_app.paths.database.name}-journal")
    try:
        journal.symlink_to(external)
    except OSError as exc:  # pragma: no cover - depends on Windows developer mode/ACL
        pytest.skip(f"file symlinks are unavailable: {exc}")
    with pytest.raises(ValueError, match="symlinks or reparse points"):
        CandidateApplication(sidecar_workspace).list_workflows()
    assert external.read_bytes() == b"external-sidecar-sentinel"


def test_candidate_rejects_production_marker_and_marker_database_mismatch(
    tmp_path: Path,
) -> None:
    production = tmp_path / "production"
    production.mkdir()
    manifest = production / "workspace.json"
    manifest.write_text(
        '{"schema":"unified-paper-orchestrator/workspace/v1"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="production workspaces"):
        CandidateApplication(production).initialize_defaults()
    assert not (production / CANDIDATE_WORKSPACE_MARKER).exists()
    assert not (production / "state").exists()

    left = tmp_path / "left-owned"
    right = tmp_path / "right-owned"
    CandidateApplication(left).initialize_defaults()
    CandidateApplication(right).initialize_defaults()
    left_marker = left / CANDIDATE_WORKSPACE_MARKER
    right_marker = right / CANDIDATE_WORKSPACE_MARKER
    left_payload = left_marker.read_bytes()
    right_payload = right_marker.read_bytes()
    left_marker.write_bytes(right_payload)
    right_marker.write_bytes(left_payload)
    with pytest.raises(RuntimeError, match="database owner do not match"):
        CandidateApplication(left).doctor()
    with pytest.raises(RuntimeError, match="database owner do not match"):
        CandidateApplication(right).initialize_defaults()


def test_ready_candidate_does_not_recreate_missing_owned_database(tmp_path: Path) -> None:
    workspace = tmp_path / "owned"
    app = CandidateApplication(workspace)
    app.initialize_defaults()
    database = app.paths.database
    preserved = tmp_path / "preserved.sqlite3"
    database.replace(preserved)

    with pytest.raises(RuntimeError, match="database is missing"):
        CandidateApplication(workspace).initialize_defaults()
    assert not database.exists()
    assert preserved.is_file()


def test_concurrent_candidate_initialization_converges_on_one_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "concurrent-owner"

    def initialize() -> str:
        return str(CandidateApplication(workspace).initialize_defaults()["workspace_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        workspace_ids = list(pool.map(lambda _index: initialize(), range(2)))

    assert len(set(workspace_ids)) == 1
    reopened = CandidateApplication(workspace)
    assert len(reopened.list_workflows()["workflows"]) == 6
    marker = json.loads((workspace / CANDIDATE_WORKSPACE_MARKER).read_text(encoding="utf-8"))
    assert marker["status"] == "ready"
    assert marker["workspace_id"] == workspace_ids[0]


def test_cross_process_candidate_initialization_converges_on_one_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "process-owner"
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        str(root / "tools" / "workflow_candidate.py"),
        "--workspace",
        str(workspace),
        "--locale",
        "en-US",
        "init",
    ]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    processes = [
        subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _index in range(2)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    payloads = [json.loads(stdout) for stdout, _stderr in results]
    assert len({payload["workspace_id"] for payload in payloads}) == 1
    assert len(CandidateApplication(workspace).list_workflows()["workflows"]) == 6


def test_candidate_recovers_owned_initializing_marker_and_empty_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "crash-window"
    app = CandidateApplication(workspace)
    original_initialize = WorkflowStore.initialize

    def injected_crash(_store: WorkflowStore) -> None:
        raise RuntimeError("injected crash before database initialization")

    monkeypatch.setattr(WorkflowStore, "initialize", injected_crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        app.initialize_defaults()
    marker_path = workspace / CANDIDATE_WORKSPACE_MARKER
    initializing = json.loads(marker_path.read_text(encoding="utf-8"))
    assert initializing["status"] == "initializing"
    assert not app.paths.database.exists()

    monkeypatch.setattr(WorkflowStore, "initialize", original_initialize)
    # Simulate a crash after generic schema creation but before the candidate
    # owner keys were committed. Only an empty schema may be adopted here.
    WorkflowStore(app.paths.database).initialize()
    recovered = CandidateApplication(workspace).initialize_defaults()
    ready = json.loads(marker_path.read_text(encoding="utf-8"))
    assert ready["status"] == "ready"
    assert ready["workspace_id"] == initializing["workspace_id"] == recovered["workspace_id"]


def test_candidate_refuses_unknown_schema_metadata_and_bind_time_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_initialize = WorkflowStore.initialize

    def create_initializing_workspace(name: str) -> CandidateApplication:
        app = CandidateApplication(tmp_path / name)

        def injected_crash(_store: WorkflowStore) -> None:
            raise RuntimeError("injected pre-database crash")

        monkeypatch.setattr(WorkflowStore, "initialize", injected_crash)
        with pytest.raises(RuntimeError, match="pre-database crash"):
            app.initialize_defaults()
        monkeypatch.setattr(WorkflowStore, "initialize", original_initialize)
        WorkflowStore(app.paths.database).initialize()
        return app

    unknown_table = create_initializing_workspace("unknown-table")
    with sqlite3.connect(unknown_table.paths.database) as connection:
        connection.execute("CREATE TABLE production_jobs(job_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO production_jobs(job_id) VALUES ('production-sentinel')")
    database_before = unknown_table.paths.database.read_bytes()
    with pytest.raises(RuntimeError, match="schema fingerprint"):
        CandidateApplication(unknown_table.paths.workspace).initialize_defaults()
    assert unknown_table.paths.database.read_bytes() == database_before
    with sqlite3.connect(unknown_table.paths.database) as connection:
        assert connection.execute("SELECT job_id FROM production_jobs").fetchone() == ("production-sentinel",)
        assert (
            connection.execute(
                "SELECT 1 FROM workflow_schema_meta WHERE key=?",
                (CANDIDATE_DATABASE_ID_KEY,),
            ).fetchone()
            is None
        )

    unknown_metadata = create_initializing_workspace("unknown-metadata")
    with sqlite3.connect(unknown_metadata.paths.database) as connection:
        connection.execute("INSERT INTO workflow_schema_meta(key, value) VALUES ('production_owner', 'sentinel')")
    with pytest.raises(RuntimeError, match="unexpected metadata"):
        CandidateApplication(unknown_metadata.paths.workspace).initialize_defaults()

    raced = create_initializing_workspace("bind-race")

    def initialize_then_inject(store: WorkflowStore) -> None:
        original_initialize(store)
        with sqlite3.connect(store.path) as connection:
            connection.execute("CREATE TABLE production_jobs(job_id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO production_jobs(job_id) VALUES ('raced-write')")

    monkeypatch.setattr(WorkflowStore, "initialize", initialize_then_inject)
    with pytest.raises(RuntimeError, match="schema fingerprint"):
        CandidateApplication(raced.paths.workspace).initialize_defaults()
    with sqlite3.connect(raced.paths.database) as connection:
        assert connection.execute("SELECT job_id FROM production_jobs").fetchone() == ("raced-write",)
        assert (
            connection.execute(
                "SELECT 1 FROM workflow_schema_meta WHERE key=?",
                (CANDIDATE_DATABASE_ID_KEY,),
            ).fetchone()
            is None
        )
    marker = json.loads((raced.paths.workspace / CANDIDATE_WORKSPACE_MARKER).read_text(encoding="utf-8"))
    assert marker["status"] == "initializing"


def test_cli_returns_nonzero_for_failed_doctor_and_acceptance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "fail-closed-cli"
    CandidateApplication(workspace).initialize_defaults()

    monkeypatch.setattr(
        CandidateApplication,
        "doctor",
        lambda self, *, sci_roots=None: {"status": "FAIL", "checks": []},
    )
    assert candidate_main(["--workspace", str(workspace), "doctor"]) == 1
    assert _json_output(capsys)["status"] == "FAIL"

    monkeypatch.setattr(
        CandidateApplication,
        "accept_offline",
        lambda self, *, sci_roots=None: {
            "offline_candidate_acceptance": "FAIL",
            "checks": [],
            "online_production_acceptance": {
                "status": "NOT_RUN",
                "production_equivalence": False,
            },
            "production_promotion_eligible": False,
        },
    )
    assert (
        candidate_main(
            [
                "--workspace",
                str(workspace),
                "accept-offline",
                "--report",
                "reports/failed-acceptance.json",
            ]
        )
        == 1
    )
    assert _json_output(capsys)["offline_candidate_acceptance"] == "FAIL"
