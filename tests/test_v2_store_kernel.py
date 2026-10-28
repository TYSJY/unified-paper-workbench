from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from unified_paper_orchestrator_v2.contracts import TaskEnvelope, canonical_json
from unified_paper_orchestrator_v2.kernel import WorkflowKernel
from unified_paper_orchestrator_v2.store import EventStore


def make_kernel(tmp_path) -> WorkflowKernel:
    return WorkflowKernel(EventStore(tmp_path / "v2.sqlite3"))


def test_idempotent_create_and_dispatch(tmp_path) -> None:
    kernel = make_kernel(tmp_path)
    envelope = TaskEnvelope.from_dict(
        {
            "paper_id": "paper-1",
            "stage": "system",
            "action": "system.noop",
            "executor": "deterministic",
            "idempotency_key": "same-request",
            "payload": {"value": 1},
        }
    )
    first = kernel.create_task(envelope)
    duplicate = kernel.create_task(
        TaskEnvelope.from_dict({**envelope.to_dict(), "task_id": "different-task-id"})
    )
    assert duplicate["task_id"] == first["task_id"]
    completed = kernel.dispatch_once()
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["data"]["echo"] == {"value": 1}
    assert kernel.dispatch_once() is None

    with pytest.raises(ValueError, match="idempotency key reused"):
        kernel.create_task(
            TaskEnvelope.from_dict({**envelope.to_dict(), "task_id": "conflict", "payload": {"value": 2}})
        )


def test_submission_execute_waits_for_durable_approval(tmp_path) -> None:
    kernel = make_kernel(tmp_path)
    record = kernel.create_task(
        TaskEnvelope.from_dict(
            {
                "paper_id": "paper-2",
                "stage": "submission",
                "action": "submission.execute",
                "executor": "browser",
                "payload": {"route_revision": 7},
            }
        )
    )
    assert record["status"] == "waiting_approval"
    assert kernel.dispatch_once() is None
    approval = kernel.store.get_approval(record["task_id"])
    assert approval["payload"]["route_revision"] == 7
    approved = kernel.approve(record["task_id"], "approved for canary")
    assert approved["status"] == "queued"
    result = kernel.dispatch_once(record["task_id"])
    assert result is not None
    assert result["status"] == "blocked"
    assert result["result"]["blocker_code"] == "EXECUTION_GATEWAY_REQUIRED"
    retried = kernel.retry(record["task_id"], "browser adapter installed later")
    assert retried["status"] == "queued"


def test_cancelled_waiting_task_cannot_be_approved_after_cancellation(tmp_path) -> None:
    kernel = make_kernel(tmp_path)
    waiting = kernel.create_task(
        TaskEnvelope(
            paper_id="cancel-before-approval",
            stage="submission",
            action="submission.execute",
            executor="browser",
        )
    )
    cancelled = kernel.cancel(waiting["task_id"], "operator cancelled")

    with pytest.raises(ValueError, match="approval already decided"):
        kernel.approve(waiting["task_id"], "late approval must fail")

    assert cancelled["status"] == "cancelled"
    assert kernel.store.get_approval(waiting["task_id"])["status"] == "cancelled"


def test_running_task_cannot_be_cancelled_without_attempt_fencing(tmp_path) -> None:
    kernel = make_kernel(tmp_path)
    queued = kernel.create_task(
        TaskEnvelope(
            paper_id="running-cancel-guard",
            stage="system",
            action="system.noop",
            executor="deterministic",
        )
    )
    claimed = kernel.store.claim_task(queued["task_id"], dispatch_owner="kernel")
    assert claimed is not None

    with pytest.raises(ValueError, match="cannot be cancelled"):
        kernel.cancel(queued["task_id"], "unsafe concurrent cancel")

    assert kernel.store.get_task(queued["task_id"])["status"] == "running"


def test_read_only_legacy_snapshot_import_is_deduplicated(tmp_path) -> None:
    kernel = make_kernel(tmp_path)
    source = tmp_path / "unified_state.json"
    source.write_text(json.dumps({"papers": {"p1": {}, "p2": {}}}), encoding="utf-8")
    before = source.read_bytes()
    first = kernel.import_legacy(source)
    second = kernel.import_legacy(source)
    assert first["snapshot_id"] == second["snapshot_id"]
    assert source.read_bytes() == before
    assert kernel.status()["runtime"]["legacy_snapshot_count"] == 1


def test_existing_schema_one_database_is_additively_migrated_to_dispatch_owner(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-v2.sqlite3"
    envelope = TaskEnvelope(
        paper_id="old-task",
        stage="system",
        action="system.noop",
        executor="deterministic",
        task_id="task_old",
        run_id="run_old",
        idempotency_key="old-idempotency",
    ).to_dict()
    envelope.pop("dispatch_owner")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                paper_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                action TEXT NOT NULL,
                executor TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                envelope_json TEXT NOT NULL,
                result_json TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)",
            (
                "task_old",
                "run_old",
                "old-task",
                "system",
                "system.noop",
                "deterministic",
                "queued",
                "old-idempotency",
                canonical_json(envelope),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )

    store = EventStore(database)
    store.initialize()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
    assert "dispatch_owner" in columns
    assert "execution_attempt_token_sha256" in columns
    assert store.get_task("task_old")["dispatch_owner"] == "kernel"
    claimed = store.claim_task("task_old", dispatch_owner="kernel")
    assert claimed is not None
    assert claimed.dispatch_owner == "kernel"
    assert len(store.get_task("task_old")["execution_attempt_token_sha256"]) == 64


def test_fresh_database_initialization_is_serialized_across_store_instances(
    tmp_path,
) -> None:
    database = tmp_path / "concurrent-init.sqlite3"

    def initialize(_index: int) -> str:
        store = EventStore(database)
        store.initialize()
        return store.summary()["schema_version"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        versions = list(pool.map(initialize, range(32)))

    assert set(versions) == {"1"}
    assert not database.with_name(f".{database.name}.initialize.lock").exists()
