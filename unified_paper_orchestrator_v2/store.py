from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from unified_paper_orchestrator.io_utils import FileLock

from .contracts import ApprovalRequest, ResultStatus, TaskEnvelope, TaskResult, TaskStatus, canonical_json


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


RESULT_TO_TASK = {
    ResultStatus.COMPLETED: TaskStatus.COMPLETED,
    ResultStatus.WAITING_HUMAN: TaskStatus.WAITING_HUMAN,
    ResultStatus.BLOCKED: TaskStatus.BLOCKED,
    ResultStatus.RETRYABLE_FAILURE: TaskStatus.RETRYABLE_FAILURE,
    ResultStatus.FATAL_FAILURE: TaskStatus.FATAL_FAILURE,
}


class EventStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.initialize.lock")
        deadline = time.monotonic() + 30.0
        while True:
            lock = FileLock(lock_path, stale_seconds=0)
            try:
                lock.__enter__()
            except RuntimeError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "V2 database initialization lock timed out."
                    ) from exc
                time.sleep(0.02)
                continue
            try:
                self._initialize_locked()
                return
            finally:
                lock.__exit__(None, None, None)

    def _initialize_locked(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    paper_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    action TEXT NOT NULL,
                    executor TEXT NOT NULL,
                    dispatch_owner TEXT NOT NULL DEFAULT 'kernel',
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    envelope_json TEXT NOT NULL,
                    result_json TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    execution_attempt_token_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_paper ON tasks(paper_id, created_at);

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, event_id);

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decision_note TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS legacy_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO runtime_meta(key, value) VALUES ('schema_version', '1');
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "dispatch_owner" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN dispatch_owner TEXT NOT NULL DEFAULT 'kernel'"
                )
            if "execution_attempt_token_sha256" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN execution_attempt_token_sha256 TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_owner_status_created "
                "ON tasks(dispatch_owner, status, created_at)"
            )
            schema = conn.execute("SELECT value FROM runtime_meta WHERE key='schema_version'").fetchone()["value"]
            if schema != "1":
                raise ValueError(f"unsupported V2 database schema: {schema}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _event(self, conn: sqlite3.Connection, event_type: str, task_id: str | None, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events(event_type, task_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_type, task_id, canonical_json(payload), now_iso()),
        )

    def create_task(
        self,
        envelope: TaskEnvelope,
        *,
        approval: ApprovalRequest | None = None,
    ) -> dict[str, Any]:
        envelope.validate()
        payload = canonical_json(envelope.to_dict())
        if approval is not None:
            self._validate_approval_binding(envelope, approval)
        timestamp = now_iso()
        durable_task_id = envelope.task_id
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (envelope.idempotency_key,)
            ).fetchone()
            if existing is not None:
                previous = json.loads(existing["envelope_json"])
                requested = envelope.to_dict()
                for item in (previous, requested):
                    item.pop("task_id", None)
                    item.pop("run_id", None)
                if canonical_json(previous) != canonical_json(requested):
                    raise ValueError(f"idempotency key reused with different task content: {envelope.idempotency_key}")
                durable_task_id = str(existing["task_id"])
                if approval is not None:
                    existing_approval = conn.execute(
                        "SELECT 1 FROM approvals WHERE task_id=?", (durable_task_id,)
                    ).fetchone()
                    if existing_approval is None:
                        if existing["status"] != TaskStatus.QUEUED.value:
                            raise ValueError(
                                f"approval missing for non-queued task: {durable_task_id}"
                            )
                        durable_envelope = TaskEnvelope.from_dict(
                            json.loads(existing["envelope_json"])
                        )
                        repaired = ApprovalRequest(
                            task_id=durable_task_id,
                            reason=approval.reason,
                            artifact_hashes=[
                                item.sha256 for item in durable_envelope.inputs if item.sha256
                            ],
                            intent_sha256=hashlib.sha256(
                                canonical_json(durable_envelope.to_dict()).encode("utf-8")
                            ).hexdigest(),
                            route_revision=durable_envelope.payload.get("route_revision"),
                        )
                        self._insert_approval(conn, repaired, timestamp)
                        conn.execute(
                            "UPDATE tasks SET status=?, updated_at=? "
                            "WHERE task_id=? AND status=?",
                            (
                                TaskStatus.WAITING_APPROVAL.value,
                                timestamp,
                                durable_task_id,
                                TaskStatus.QUEUED.value,
                            ),
                        )
                        self._event(
                            conn,
                            "approval.requested",
                            durable_task_id,
                            repaired.to_dict(),
                        )
            else:
                initial_status = (
                    TaskStatus.WAITING_APPROVAL
                    if approval is not None
                    else TaskStatus.QUEUED
                )
                conn.execute(
                    """INSERT INTO tasks(
                        task_id, run_id, paper_id, stage, action, executor, dispatch_owner, status,
                        idempotency_key, envelope_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        envelope.task_id,
                        envelope.run_id,
                        envelope.paper_id,
                        envelope.stage,
                        envelope.action,
                        envelope.executor,
                        envelope.dispatch_owner,
                        initial_status.value,
                        envelope.idempotency_key,
                        payload,
                        timestamp,
                        timestamp,
                    ),
                )
                self._event(conn, "task.created", envelope.task_id, envelope.to_dict())
                if approval is not None:
                    self._insert_approval(conn, approval, timestamp)
                    self._event(
                        conn,
                        "approval.requested",
                        envelope.task_id,
                        approval.to_dict(),
                    )
        return self.get_task(durable_task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown task_id: {task_id}")
        return self._task_row(row)

    def list_tasks(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._task_row(row) for row in rows]

    def claim_task(
        self,
        task_id: str = "",
        *,
        dispatch_owner: str = "kernel",
    ) -> TaskEnvelope | None:
        if dispatch_owner not in {"kernel", "gateway"}:
            raise ValueError(f"unsupported dispatch_owner: {dispatch_owner}")
        with self.transaction() as conn:
            if task_id:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id=? AND status=? AND dispatch_owner=?",
                    (task_id, TaskStatus.QUEUED.value, dispatch_owner),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE status=? AND dispatch_owner=? "
                    "ORDER BY created_at, task_id LIMIT 1",
                    (TaskStatus.QUEUED.value, dispatch_owner),
                ).fetchone()
            if not row:
                return None
            execution_attempt_token_sha256 = hashlib.sha256(
                secrets.token_bytes(32)
            ).hexdigest()
            updated = conn.execute(
                "UPDATE tasks SET status=?, attempt_count=attempt_count+1, "
                "execution_attempt_token_sha256=?, updated_at=? "
                "WHERE task_id=? AND status=? AND dispatch_owner=?",
                (
                    TaskStatus.RUNNING.value,
                    execution_attempt_token_sha256,
                    now_iso(),
                    row["task_id"],
                    TaskStatus.QUEUED.value,
                    dispatch_owner,
                ),
            )
            if updated.rowcount != 1:
                return None
            self._event(
                conn,
                "task.claimed",
                row["task_id"],
                {
                    "executor": row["executor"],
                    "dispatch_owner": dispatch_owner,
                    "execution_attempt_token_sha256": execution_attempt_token_sha256,
                },
            )
            return TaskEnvelope.from_dict(json.loads(row["envelope_json"]))

    def active_execution_attempt_token_sha256(
        self,
        task_id: str,
        *,
        dispatch_owner: str = "kernel",
    ) -> str:
        """Return the random claim binding for one currently running attempt."""

        self._validate_dispatch_owner(dispatch_owner)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, dispatch_owner, execution_attempt_token_sha256 "
                "FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown task_id: {task_id}")
        if row["dispatch_owner"] != dispatch_owner:
            raise ValueError(
                f"task dispatch_owner mismatch: {task_id} owner={row['dispatch_owner']}"
            )
        token = str(row["execution_attempt_token_sha256"] or "").strip().lower()
        if row["status"] != TaskStatus.RUNNING.value or len(token) != 64:
            raise ValueError(f"task has no active execution attempt: {task_id}")
        return token

    def complete_task(
        self,
        task_id: str,
        result: TaskResult,
        *,
        dispatch_owner: str = "kernel",
    ) -> dict[str, Any]:
        self._validate_dispatch_owner(dispatch_owner)
        target = RESULT_TO_TASK[result.status]
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, dispatch_owner FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown task_id: {task_id}")
            if row["dispatch_owner"] != dispatch_owner:
                raise ValueError(
                    f"task dispatch_owner mismatch: {task_id} owner={row['dispatch_owner']}"
                )
            if row["status"] != TaskStatus.RUNNING.value:
                raise ValueError(f"task is not running: {task_id} status={row['status']}")
            conn.execute(
                "UPDATE tasks SET status=?, result_json=?, updated_at=? WHERE task_id=?",
                (target.value, canonical_json(result.to_dict()), now_iso(), task_id),
            )
            self._event(conn, f"task.{target.value}", task_id, result.to_dict())
        return self.get_task(task_id)

    def request_approval(self, task_id: str, approval: ApprovalRequest) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, envelope_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown task_id: {task_id}")
            self._validate_approval_binding(
                TaskEnvelope.from_dict(json.loads(row["envelope_json"])),
                approval,
            )
            existing = conn.execute("SELECT * FROM approvals WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                previous = json.loads(existing["payload_json"])
                requested = approval.to_dict()
                for item in (previous, requested):
                    item.pop("approval_id", None)
                if canonical_json(previous) != canonical_json(requested):
                    raise ValueError(
                        f"approval intent changed for existing task: {task_id}"
                    )
                if row["status"] == TaskStatus.QUEUED.value:
                    raise ValueError(
                        f"approval/task state is inconsistent for existing task: {task_id}"
                    )
                return self._approval_row(existing)
            if row["status"] != TaskStatus.QUEUED.value:
                raise ValueError(
                    f"approval can only be requested for a queued task: {task_id}"
                )
            conn.execute(
                """INSERT INTO approvals(
                    approval_id, task_id, status, reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    approval.approval_id,
                    task_id,
                    "pending",
                    approval.reason,
                    canonical_json(approval.to_dict()),
                    now_iso(),
                ),
            )
            updated = conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=? AND status=?",
                (
                    TaskStatus.WAITING_APPROVAL.value,
                    now_iso(),
                    task_id,
                    TaskStatus.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError(f"task left queued state before approval request: {task_id}")
            self._event(conn, "approval.requested", task_id, approval.to_dict())
        return self.get_approval(task_id)

    def get_approval(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"no approval for task: {task_id}")
        return self._approval_row(row)

    def decide_approval(
        self,
        task_id: str,
        *,
        approve: bool,
        note: str = "",
        dispatch_owner: str = "kernel",
    ) -> dict[str, Any]:
        self._validate_dispatch_owner(dispatch_owner)
        decision = "approved" if approve else "rejected"
        target = TaskStatus.QUEUED if approve else TaskStatus.CANCELLED
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT approvals.status AS approval_status, "
                "tasks.status AS task_status, tasks.dispatch_owner "
                "FROM approvals JOIN tasks ON tasks.task_id=approvals.task_id "
                "WHERE approvals.task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"no approval for task: {task_id}")
            if row["dispatch_owner"] != dispatch_owner:
                raise ValueError(
                    f"task dispatch_owner mismatch: {task_id} owner={row['dispatch_owner']}"
                )
            if row["approval_status"] != "pending":
                raise ValueError(f"approval already decided: {task_id}")
            if row["task_status"] != TaskStatus.WAITING_APPROVAL.value:
                raise ValueError(
                    f"task is not waiting for approval: {task_id} status={row['task_status']}"
                )
            conn.execute(
                "UPDATE approvals SET status=?, decided_at=?, decision_note=? WHERE task_id=?",
                (decision, now_iso(), note, task_id),
            )
            updated = conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=? AND status=?",
                (target.value, now_iso(), task_id, TaskStatus.WAITING_APPROVAL.value),
            )
            if updated.rowcount != 1:
                raise ValueError(f"task left waiting_approval before decision: {task_id}")
            self._event(conn, f"approval.{decision}", task_id, {"note": note})
        return self.get_task(task_id)

    def cancel_task(
        self,
        task_id: str,
        note: str = "",
        *,
        dispatch_owner: str = "kernel",
    ) -> dict[str, Any]:
        self._validate_dispatch_owner(dispatch_owner)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, dispatch_owner FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown task_id: {task_id}")
            if row["dispatch_owner"] != dispatch_owner:
                raise ValueError(
                    f"task dispatch_owner mismatch: {task_id} owner={row['dispatch_owner']}"
                )
            if row["status"] == TaskStatus.RUNNING.value:
                raise ValueError(
                    f"running task cannot be cancelled without cooperative fencing: {task_id}"
                )
            if row["status"] in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
                return self.get_task(task_id)
            if row["status"] == TaskStatus.WAITING_APPROVAL.value:
                closed = conn.execute(
                    "UPDATE approvals SET status='cancelled', decided_at=?, decision_note=? "
                    "WHERE task_id=? AND status='pending'",
                    (now_iso(), note, task_id),
                )
                if closed.rowcount == 1:
                    self._event(conn, "approval.cancelled", task_id, {"note": note})
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (TaskStatus.CANCELLED.value, now_iso(), task_id),
            )
            self._event(conn, "task.cancelled", task_id, {"note": note})
        return self.get_task(task_id)

    def requeue_task(
        self,
        task_id: str,
        note: str = "",
        *,
        dispatch_owner: str = "kernel",
        acknowledgements: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        self._validate_dispatch_owner(dispatch_owner)
        allowed = {
            TaskStatus.BLOCKED.value,
            TaskStatus.RETRYABLE_FAILURE.value,
            TaskStatus.WAITING_HUMAN.value,
        }
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, dispatch_owner FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown task_id: {task_id}")
            if row["dispatch_owner"] != dispatch_owner:
                raise ValueError(
                    f"task dispatch_owner mismatch: {task_id} owner={row['dispatch_owner']}"
                )
            if row["status"] not in allowed:
                raise ValueError(f"task cannot be requeued from status={row['status']}")
            conn.execute(
                "UPDATE tasks SET status=?, result_json=NULL, "
                "execution_attempt_token_sha256=NULL, updated_at=? WHERE task_id=?",
                (TaskStatus.QUEUED.value, now_iso(), task_id),
            )
            self._event(
                conn,
                "task.requeued",
                task_id,
                {
                    "previous_status": row["status"],
                    "note": note,
                    "acknowledgements": dict(acknowledgements or {}),
                },
            )
        return self.get_task(task_id)

    def list_events(self, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE event_id>? ORDER BY event_id LIMIT ?", (int(after), limit)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def import_legacy_snapshot(self, source: Path) -> dict[str, Any]:
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("legacy unified state must be a JSON object")
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM legacy_snapshots WHERE source_sha256=?", (digest,)
            ).fetchone()
            if existing:
                return self._snapshot_row(existing)
            conn.execute(
                "INSERT INTO legacy_snapshots(source_path, source_sha256, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (str(source.resolve()), digest, canonical_json(payload), now_iso()),
            )
            self._event(
                conn,
                "legacy.snapshot_imported",
                None,
                {
                    "source_path": str(source.resolve()),
                    "source_sha256": digest,
                    "paper_count": len(payload.get("papers", {})) if isinstance(payload.get("papers"), dict) else 0,
                },
            )
            row = conn.execute(
                "SELECT * FROM legacy_snapshots WHERE source_sha256=?", (digest,)
            ).fetchone()
        return self._snapshot_row(row)

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {
                row["status"]: row["count"]
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
            }
            event_count = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            snapshot_count = conn.execute("SELECT COUNT(*) AS count FROM legacy_snapshots").fetchone()["count"]
            schema_version = conn.execute("SELECT value FROM runtime_meta WHERE key='schema_version'").fetchone()["value"]
        return {
            "database_path": str(self.path),
            "schema_version": schema_version,
            "task_counts": counts,
            "event_count": event_count,
            "legacy_snapshot_count": snapshot_count,
        }

    @staticmethod
    def _validate_approval_binding(
        envelope: TaskEnvelope,
        approval: ApprovalRequest,
    ) -> None:
        expected_intent = hashlib.sha256(
            canonical_json(envelope.to_dict()).encode("utf-8")
        ).hexdigest()
        expected_hashes = [item.sha256 for item in envelope.inputs if item.sha256]
        if (
            approval.task_id != envelope.task_id
            or approval.status != "pending"
            or approval.intent_sha256 != expected_intent
            or approval.artifact_hashes != expected_hashes
            or approval.route_revision != envelope.payload.get("route_revision")
            or not approval.reason.strip()
        ):
            raise ValueError("approval request is not bound to the exact task envelope")

    @staticmethod
    def _insert_approval(
        conn: sqlite3.Connection,
        approval: ApprovalRequest,
        timestamp: str,
    ) -> None:
        conn.execute(
            """INSERT INTO approvals(
                approval_id, task_id, status, reason, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                approval.approval_id,
                approval.task_id,
                "pending",
                approval.reason,
                canonical_json(approval.to_dict()),
                timestamp,
            ),
        )

    @staticmethod
    def _validate_dispatch_owner(dispatch_owner: str) -> None:
        if dispatch_owner not in {"kernel", "gateway"}:
            raise ValueError(f"unsupported dispatch_owner: {dispatch_owner}")

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["envelope"] = json.loads(item.pop("envelope_json"))
        raw_result = item.pop("result_json")
        item["result"] = json.loads(raw_result) if raw_result else None
        return item

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        payload = json.loads(item.pop("payload_json"))
        item["paper_count"] = len(payload.get("papers", {})) if isinstance(payload.get("papers"), dict) else 0
        return item

    @staticmethod
    def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result
