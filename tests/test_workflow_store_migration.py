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

from unified_paper_orchestrator.workflow.store import (
    WorkflowStore,
    WorkflowStoreError,
    workflow_database_path,
)
from unified_paper_orchestrator_v2.contracts import (
    ApprovalRequest,
    TaskEnvelope,
    canonical_json,
)
from unified_paper_orchestrator_v2.store import EventStore


LEGACY_TABLES = ("tasks", "events", "approvals", "runtime_meta")
WORKFLOW_TABLES = {
    "workflow_schema_meta",
    "workflow_versions",
    "workflow_runtime_bindings",
    "workflow_runs",
    "node_runs",
    "workflow_events",
    "workflow_sessions",
    "workflow_artifacts",
    "workflow_leases",
}


def _table_snapshot(database: Path, names: tuple[str, ...]) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        return {
            name: {
                "schema_objects": connection.execute(
                    "SELECT type, name, sql FROM sqlite_master WHERE tbl_name=? ORDER BY type, name",
                    (name,),
                ).fetchall(),
                "columns": connection.execute(f'PRAGMA table_info("{name}")').fetchall(),
                "foreign_keys": connection.execute(f'PRAGMA foreign_key_list("{name}")').fetchall(),
                "rows": connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(),
            }
            for name in names
        }


def _schema_snapshot(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()


def _seed_legacy_v2_database(database: Path) -> dict[str, object]:
    legacy = EventStore(database)
    legacy.initialize()
    envelope = TaskEnvelope(
        task_id="task_legacy_compatibility",
        run_id="run_legacy_compatibility",
        paper_id="paper-legacy",
        stage="submission",
        action="submission.execute",
        executor="browser",
        idempotency_key="legacy-compatibility-key",
        payload={"route_revision": 7},
    )
    envelope_payload = envelope.to_dict()
    approval = ApprovalRequest(
        approval_id="approval_legacy_compatibility",
        task_id=envelope.task_id,
        reason="Preserve this pending approval during the additive migration.",
        intent_sha256=hashlib.sha256(canonical_json(envelope_payload).encode("utf-8")).hexdigest(),
        route_revision=7,
    )
    legacy.create_task(envelope, approval=approval)
    return legacy.summary()


def test_initialize_adds_workflow_ledger_without_mutating_legacy_v2_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orchestrator.sqlite3"
    legacy_summary = _seed_legacy_v2_database(database)
    legacy_before = _table_snapshot(database, LEGACY_TABLES)

    WorkflowStore(database).initialize()

    assert _table_snapshot(database, LEGACY_TABLES) == legacy_before
    with sqlite3.connect(database) as connection:
        table_names = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        legacy_schema_version = connection.execute(
            "SELECT value FROM runtime_meta WHERE key='schema_version'"
        ).fetchone()[0]
        workflow_schema_version = connection.execute(
            "SELECT value FROM workflow_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert WORKFLOW_TABLES <= table_names
    assert legacy_schema_version == "1"
    assert workflow_schema_version == "1"

    reopened_legacy = EventStore(database)
    reopened_legacy.initialize()
    assert reopened_legacy.summary() == legacy_summary
    assert _table_snapshot(database, LEGACY_TABLES) == legacy_before


def test_fresh_initialize_is_safe_across_32_concurrent_store_instances(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "v2" / "orchestrator.sqlite3"

    def initialize(_index: int) -> None:
        WorkflowStore(database).initialize()

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(initialize, range(32)))

    with sqlite3.connect(database) as connection:
        table_names = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert WORKFLOW_TABLES <= table_names
    assert not list(database.parent.glob("*.initialize.lock"))
    assert not list(database.parent.glob(".*.initialize.lock"))


def test_fresh_initialize_is_safe_across_16_processes(tmp_path: Path) -> None:
    database = tmp_path / "processes" / "orchestrator.sqlite3"
    script = "\n".join(
        (
            "from pathlib import Path",
            "from unified_paper_orchestrator.workflow.store import WorkflowStore",
            "WorkflowStore(Path(__import__('sys').argv[1])).initialize()",
        )
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def initialize(_index: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", "-c", script, str(database)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        completed = list(pool.map(initialize, range(16)))

    failures = [item.stderr for item in completed if item.returncode != 0]
    assert failures == []
    assert WorkflowStore(database).summary()["schema_version"] == "1"
    assert not database.with_name(f".{database.name}.initialize.lock").exists()


def test_unknown_workflow_schema_is_rejected_before_any_other_workflow_ddl(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unknown-workflow-schema.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE workflow_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO workflow_schema_meta(key, value)
            VALUES ('schema_version', '999');
            """
        )
    before = _table_snapshot(database, ("workflow_schema_meta",))
    schema_before = _schema_snapshot(database)

    with pytest.raises(WorkflowStoreError, match="unsupported workflow database schema"):
        WorkflowStore(database).initialize()

    assert _schema_snapshot(database) == schema_before
    assert _table_snapshot(database, ("workflow_schema_meta",)) == before
    with sqlite3.connect(database) as connection:
        table_names = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert WORKFLOW_TABLES & table_names == {"workflow_schema_meta"}
    assert not database.with_name(f".{database.name}.initialize.lock").exists()


def test_initialize_rolls_back_every_ddl_statement_when_schema_creation_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "failed-workflow-schema.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE workflow_versions (
                sentinel TEXT PRIMARY KEY
            );
            INSERT INTO workflow_versions(sentinel) VALUES ('must-survive');
            """
        )
    schema_before = _schema_snapshot(database)
    with sqlite3.connect(database) as connection:
        rows_before = connection.execute("SELECT * FROM workflow_versions ORDER BY rowid").fetchall()

    with pytest.raises((sqlite3.DatabaseError, WorkflowStoreError)):
        WorkflowStore(database).initialize()

    assert _schema_snapshot(database) == schema_before
    with sqlite3.connect(database) as connection:
        rows_after = connection.execute("SELECT * FROM workflow_versions ORDER BY rowid").fetchall()
        table_names = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert rows_after == rows_before
    assert WORKFLOW_TABLES & table_names == {"workflow_versions"}
    assert not database.with_name(f".{database.name}.initialize.lock").exists()


def test_workflow_database_path_is_workspace_local_and_side_effect_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected_workspace = tmp_path / "selected-workspace"
    other_workspace = tmp_path / "other-workspace"
    other_database = other_workspace / "state" / "v2" / "orchestrator.sqlite3"
    other_database.parent.mkdir(parents=True)
    other_database.write_bytes(b"other-workspace-database-sentinel")
    registry = tmp_path / "registry.json"
    registry_payload = {
        "schema": "unified-paper-orchestrator/workspace-registry/v1",
        "version": 1,
        "active_workspace_id": "other",
        "active_workspace_path": str(other_workspace),
        "workspaces": [{"workspace_id": "other", "path": str(other_workspace)}],
    }
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    monkeypatch.setenv("UNIFIED_PAPER_WORKSPACE_REGISTRY", str(registry))
    registry_before = (registry.read_bytes(), registry.stat().st_mtime_ns)
    other_before = (other_database.read_bytes(), other_database.stat().st_mtime_ns)

    result = workflow_database_path(selected_workspace)

    assert result == selected_workspace.resolve() / "state" / "v2" / "orchestrator.sqlite3"
    assert not selected_workspace.exists()
    assert (registry.read_bytes(), registry.stat().st_mtime_ns) == registry_before
    assert (other_database.read_bytes(), other_database.stat().st_mtime_ns) == other_before


def test_import_and_store_construction_do_not_create_a_database(tmp_path: Path) -> None:
    workspace = tmp_path / "import-only-workspace"
    registry = tmp_path / "registry-must-not-be-created.json"
    script = "\n".join(
        (
            "from pathlib import Path",
            "from unified_paper_orchestrator.workflow.store import WorkflowStore, workflow_database_path",
            "root = Path(__import__('sys').argv[1])",
            "WorkflowStore(workflow_database_path(root))",
        )
    )
    environment = os.environ.copy()
    environment["UNIFIED_PAPER_WORKSPACE"] = str(workspace)
    environment["UNIFIED_PAPER_WORKSPACE_REGISTRY"] = str(registry)

    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(workspace)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not workspace.exists()
    assert not registry.exists()


def test_store_construction_does_not_create_parent_directories(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "state" / "v2" / "orchestrator.sqlite3"

    WorkflowStore(database)

    assert not database.parent.exists()
    assert not database.exists()
