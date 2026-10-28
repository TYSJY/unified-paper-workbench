from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from .contracts import (
    NodeDefinition,
    NodeRun,
    NodeRunStatus,
    NodeType,
    ValidationMode,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
    canonical_json,
    is_sha256,
)


WORKFLOW_STORE_SCHEMA_VERSION = "1"
_TERMINAL_NODE_STATUSES = {
    NodeRunStatus.COMPLETED.value,
    NodeRunStatus.FATAL_FAILURE.value,
    NodeRunStatus.CANCELLED.value,
    NodeRunStatus.SKIPPED.value,
}
_TERMINAL_RUN_STATUSES = {
    WorkflowRunStatus.COMPLETED.value,
    WorkflowRunStatus.FATAL_FAILURE.value,
    WorkflowRunStatus.CANCELLED.value,
}


class WorkflowStoreError(RuntimeError):
    pass


class WorkflowConflictError(WorkflowStoreError):
    pass


class LeaseLostError(WorkflowStoreError):
    pass


@dataclass(frozen=True)
class LeaseGrant:
    lease_id: str
    run_id: str
    node_run_id: str
    generation: int
    fencing_token: str = field(repr=False)
    acquired_at: str
    expires_at: str


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    uri: str
    sha256: str
    media_type: str = "application/octet-stream"
    schema_ref: str = ""
    size_bytes: int | None = None
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        if not self.artifact_id.strip() or not self.uri.strip():
            raise ValueError("artifact_id and uri are required")
        parsed_uri = PurePosixPath(self.uri)
        if (
            "\\" in self.uri
            or parsed_uri.is_absolute()
            or not parsed_uri.parts
            or any(part in {"", ".", ".."} or ":" in part for part in parsed_uri.parts)
            or parsed_uri.as_posix() != self.uri
        ):
            raise ValueError("artifact uri must be a canonical portable relative path")
        if not is_sha256(self.sha256):
            raise ValueError("artifact sha256 is invalid")
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0
        ):
            raise ValueError("artifact size_bytes must be a non-negative integer")
        value = {
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "sha256": self.sha256.lower(),
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
            "size_bytes": self.size_bytes,
            "metadata": dict(self.metadata or {}),
        }
        canonical_json(value)
        return value


def workflow_database_path(workspace_root: str | Path) -> Path:
    """Resolve a workspace-local ledger path without creating anything."""

    return Path(workspace_root).expanduser().resolve() / "state" / "v2" / "orchestrator.sqlite3"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("workflow timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("workflow timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _current_time(value: datetime | None) -> datetime:
    current = value or utc_now()
    if current.tzinfo is None:
        raise ValueError("workflow operation timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WorkflowStore:
    """Append-oriented SQLite ledger for generic workflow execution.

    Construction and import are side-effect free. Call ``initialize``
    explicitly against the intended workspace database before use.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        # io_utils imports the active product configuration, so defer it until
        # the caller explicitly authorizes database initialization.
        from unified_paper_orchestrator.io_utils import FileLock

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.initialize.lock")
        deadline = time.monotonic() + 30.0
        while True:
            lock = FileLock(lock_path, stale_seconds=0)
            try:
                lock.__enter__()
            except RuntimeError as exc:
                if time.monotonic() >= deadline:
                    raise WorkflowStoreError("workflow database initialization lock timed out") from exc
                time.sleep(0.02)
                continue
            except OSError as exc:
                transient_windows_contention = os.name == "nt" and (
                    (isinstance(exc, PermissionError) and os.access(lock_path.parent, os.W_OK))
                    or getattr(exc, "winerror", None) in {32, 33}
                )
                if not transient_windows_contention or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
                continue
            try:
                self._initialize_locked()
                return
            finally:
                lock.__exit__(None, None, None)

    def _initialize_locked(self) -> None:
        with self.connect() as connection:
            marker_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_schema_meta'"
            ).fetchone()
            if marker_table is not None:
                marker = connection.execute(
                    "SELECT value FROM workflow_schema_meta WHERE key='schema_version'"
                ).fetchone()
                if marker is None or str(marker["value"]) != WORKFLOW_STORE_SCHEMA_VERSION:
                    raise WorkflowStoreError("unsupported workflow database schema")
            try:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS workflow_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO workflow_schema_meta(key, value)
                VALUES ('schema_version', '1');

                CREATE TABLE IF NOT EXISTS workflow_versions (
                    version_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    definition_sha256 TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'deprecated')),
                    is_builtin INTEGER NOT NULL CHECK(is_builtin IN (0, 1)),
                    is_readonly INTEGER NOT NULL CHECK(is_readonly IN (0, 1)),
                    created_at TEXT NOT NULL,
                    UNIQUE(workflow_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow
                    ON workflow_versions(workflow_id, version);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_versions_one_published
                    ON workflow_versions(workflow_id)
                    WHERE status = 'published';

                CREATE TABLE IF NOT EXISTS workflow_runtime_bindings (
                    binding_sha256 TEXT PRIMARY KEY,
                    definition_sha256 TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_version_id TEXT NOT NULL,
                    definition_sha256 TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued', 'running', 'paused', 'waiting-approval',
                        'waiting-human', 'blocked', 'retryable-failure',
                        'fatal-failure', 'completed', 'cancelled'
                    )),
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    request_sha256 TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                        CHECK(cancel_requested IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(workflow_version_id)
                        REFERENCES workflow_versions(version_id),
                    FOREIGN KEY(binding_sha256)
                        REFERENCES workflow_runtime_bindings(binding_sha256)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_runs_idempotency
                    ON workflow_runs(idempotency_key)
                    WHERE idempotency_key <> '';
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_created
                    ON workflow_runs(status, created_at);

                CREATE TABLE IF NOT EXISTS node_runs (
                    node_run_id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    attempt INTEGER NOT NULL CHECK(attempt > 0),
                    status TEXT NOT NULL CHECK(status IN (
                        'pending', 'ready', 'running', 'waiting-approval',
                        'waiting-human', 'blocked', 'retry-wait',
                        'retryable-failure', 'fatal-failure', 'completed',
                        'skipped', 'cancelled'
                    )),
                    node_snapshot_json TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    outputs_json TEXT NOT NULL DEFAULT '{}',
                    ready_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(workflow_run_id)
                        REFERENCES workflow_runs(run_id),
                    UNIQUE(workflow_run_id, node_id, attempt),
                    UNIQUE(workflow_run_id, node_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_node_runs_ready
                    ON node_runs(workflow_run_id, status, display_order, attempt);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_node_runs_active_per_run
                    ON node_runs(workflow_run_id)
                    WHERE status IN ('running', 'waiting-approval', 'waiting-human');

                CREATE TABLE IF NOT EXISTS workflow_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    event_type TEXT NOT NULL,
                    node_run_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workflow_run_id)
                        REFERENCES workflow_runs(run_id),
                    FOREIGN KEY(workflow_run_id, node_run_id)
                        REFERENCES node_runs(workflow_run_id, node_run_id),
                    UNIQUE(workflow_run_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_events_run
                    ON workflow_events(workflow_run_id, sequence);

                CREATE TABLE IF NOT EXISTS workflow_sessions (
                    session_record_id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL,
                    session_group TEXT NOT NULL,
                    executor TEXT NOT NULL,
                    external_session_id TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK(state IN ('active', 'rotated', 'closed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(workflow_run_id)
                        REFERENCES workflow_runs(run_id),
                    UNIQUE(workflow_run_id, session_group, executor, generation)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_sessions_active_group
                    ON workflow_sessions(workflow_run_id, session_group, executor)
                    WHERE state = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_sessions_external
                    ON workflow_sessions(external_session_id)
                    WHERE external_session_id <> '';

                CREATE TABLE IF NOT EXISTS workflow_artifacts (
                    artifact_record_id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL,
                    node_run_id TEXT,
                    producer_node_id TEXT NOT NULL DEFAULT '',
                    artifact_id TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    schema_ref TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workflow_run_id)
                        REFERENCES workflow_runs(run_id),
                    FOREIGN KEY(workflow_run_id, node_run_id)
                        REFERENCES node_runs(workflow_run_id, node_run_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_artifacts_source
                    ON workflow_artifacts(
                        workflow_run_id,
                        COALESCE(node_run_id, ''),
                        artifact_id
                    );

                CREATE TABLE IF NOT EXISTS workflow_leases (
                    lease_id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL,
                    node_run_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    fencing_token_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'active', 'released', 'completed', 'failed', 'expired', 'cancelled'
                    )),
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(workflow_run_id)
                        REFERENCES workflow_runs(run_id),
                    FOREIGN KEY(workflow_run_id, node_run_id)
                        REFERENCES node_runs(workflow_run_id, node_run_id),
                    UNIQUE(node_run_id, generation)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_leases_active_run
                    ON workflow_leases(workflow_run_id)
                    WHERE state = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_leases_active_node
                    ON workflow_leases(node_run_id)
                    WHERE state = 'active';
                CREATE INDEX IF NOT EXISTS idx_workflow_leases_expiry
                    ON workflow_leases(state, expires_at);

                CREATE TRIGGER IF NOT EXISTS workflow_versions_definition_immutable
                BEFORE UPDATE ON workflow_versions
                WHEN NEW.version_id <> OLD.version_id
                  OR NEW.workflow_id <> OLD.workflow_id
                  OR NEW.version <> OLD.version
                  OR NEW.definition_sha256 <> OLD.definition_sha256
                  OR NEW.definition_json <> OLD.definition_json
                  OR NEW.is_builtin <> OLD.is_builtin
                  OR NEW.is_readonly <> OLD.is_readonly
                  OR NEW.created_at <> OLD.created_at
                BEGIN SELECT RAISE(ABORT, 'published workflow version content is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_versions_status_transition
                BEFORE UPDATE OF status ON workflow_versions
                WHEN NOT (
                    (OLD.status = 'draft' AND NEW.status = 'published')
                    OR (OLD.status = 'published' AND NEW.status = 'deprecated')
                    OR OLD.status = NEW.status
                )
                BEGIN SELECT RAISE(ABORT, 'invalid workflow version status transition'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_versions_no_delete
                BEFORE DELETE ON workflow_versions
                BEGIN SELECT RAISE(ABORT, 'workflow versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_bindings_no_update
                BEFORE UPDATE ON workflow_runtime_bindings
                BEGIN SELECT RAISE(ABORT, 'workflow bindings are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_bindings_no_delete
                BEFORE DELETE ON workflow_runtime_bindings
                BEGIN SELECT RAISE(ABORT, 'workflow bindings are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_events_no_update
                BEFORE UPDATE ON workflow_events
                BEGIN SELECT RAISE(ABORT, 'workflow events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_events_no_delete
                BEFORE DELETE ON workflow_events
                BEGIN SELECT RAISE(ABORT, 'workflow events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_artifacts_no_update
                BEFORE UPDATE ON workflow_artifacts
                BEGIN SELECT RAISE(ABORT, 'workflow artifacts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_artifacts_no_delete
                BEFORE DELETE ON workflow_artifacts
                BEGIN SELECT RAISE(ABORT, 'workflow artifacts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_versions_status_insert_valid
                BEFORE INSERT ON workflow_versions
                WHEN NEW.status NOT IN ('draft', 'published', 'deprecated')
                BEGIN SELECT RAISE(ABORT, 'invalid workflow version status'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_runs_status_insert_valid
                BEFORE INSERT ON workflow_runs
                WHEN NEW.status NOT IN (
                    'queued', 'running', 'paused', 'waiting-approval',
                    'waiting-human', 'blocked', 'retryable-failure',
                    'fatal-failure', 'completed', 'cancelled'
                )
                BEGIN SELECT RAISE(ABORT, 'invalid workflow run status'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_runs_status_update_valid
                BEFORE UPDATE OF status ON workflow_runs
                WHEN NEW.status NOT IN (
                    'queued', 'running', 'paused', 'waiting-approval',
                    'waiting-human', 'blocked', 'retryable-failure',
                    'fatal-failure', 'completed', 'cancelled'
                )
                BEGIN SELECT RAISE(ABORT, 'invalid workflow run status'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_status_insert_valid
                BEFORE INSERT ON node_runs
                WHEN NEW.status NOT IN (
                    'pending', 'ready', 'running', 'waiting-approval',
                    'waiting-human', 'blocked', 'retry-wait',
                    'retryable-failure', 'fatal-failure', 'completed',
                    'skipped', 'cancelled'
                )
                BEGIN SELECT RAISE(ABORT, 'invalid node run status'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_status_update_valid
                BEFORE UPDATE OF status ON node_runs
                WHEN NEW.status NOT IN (
                    'pending', 'ready', 'running', 'waiting-approval',
                    'waiting-human', 'blocked', 'retry-wait',
                    'retryable-failure', 'fatal-failure', 'completed',
                    'skipped', 'cancelled'
                )
                BEGIN SELECT RAISE(ABORT, 'invalid node run status'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_runs_identity_immutable
                BEFORE UPDATE ON workflow_runs
                WHEN NEW.run_id <> OLD.run_id
                  OR NEW.workflow_version_id <> OLD.workflow_version_id
                  OR NEW.definition_sha256 <> OLD.definition_sha256
                  OR NEW.binding_sha256 <> OLD.binding_sha256
                  OR NEW.owner <> OLD.owner
                  OR NEW.idempotency_key <> OLD.idempotency_key
                  OR NEW.request_sha256 <> OLD.request_sha256
                  OR NEW.inputs_json <> OLD.inputs_json
                  OR NEW.metadata_json <> OLD.metadata_json
                  OR NEW.created_at <> OLD.created_at
                BEGIN SELECT RAISE(ABORT, 'workflow run identity and snapshot are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_runs_snapshot_consistent
                BEFORE INSERT ON workflow_runs
                WHEN NOT EXISTS (
                    SELECT 1 FROM workflow_versions AS version
                    JOIN workflow_runtime_bindings AS binding
                      ON binding.binding_sha256 = NEW.binding_sha256
                    WHERE version.version_id = NEW.workflow_version_id
                      AND version.definition_sha256 = NEW.definition_sha256
                      AND binding.definition_sha256 = NEW.definition_sha256
                )
                BEGIN SELECT RAISE(ABORT, 'workflow run snapshot hashes are inconsistent'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_identity_immutable
                BEFORE UPDATE ON node_runs
                WHEN NEW.node_run_id <> OLD.node_run_id
                  OR NEW.workflow_run_id <> OLD.workflow_run_id
                  OR NEW.node_id <> OLD.node_id
                  OR NEW.display_order <> OLD.display_order
                  OR NEW.attempt <> OLD.attempt
                  OR NEW.node_snapshot_json <> OLD.node_snapshot_json
                  OR NEW.created_at <> OLD.created_at
                BEGIN SELECT RAISE(ABORT, 'node run identity and snapshot are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_session_insert_valid
                BEFORE INSERT ON node_runs
                WHEN NEW.session_id <> '' AND NOT EXISTS (
                    SELECT 1 FROM workflow_sessions AS session
                    WHERE session.session_record_id = NEW.session_id
                      AND session.workflow_run_id = NEW.workflow_run_id
                )
                BEGIN SELECT RAISE(ABORT, 'node session does not belong to workflow run'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_session_update_valid
                BEFORE UPDATE OF session_id ON node_runs
                WHEN NEW.session_id <> '' AND NOT EXISTS (
                    SELECT 1 FROM workflow_sessions AS session
                    WHERE session.session_record_id = NEW.session_id
                      AND session.workflow_run_id = NEW.workflow_run_id
                )
                BEGIN SELECT RAISE(ABORT, 'node session does not belong to workflow run'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_terminal_no_update
                BEFORE UPDATE ON node_runs
                WHEN OLD.status IN (
                    'completed', 'retryable-failure', 'fatal-failure',
                    'blocked', 'cancelled', 'skipped'
                )
                BEGIN SELECT RAISE(ABORT, 'terminal node runs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS node_runs_no_delete
                BEFORE DELETE ON node_runs
                BEGIN SELECT RAISE(ABORT, 'node runs are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_leases_no_delete
                BEFORE DELETE ON workflow_leases
                BEGIN SELECT RAISE(ABORT, 'workflow leases are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_leases_identity_immutable
                BEFORE UPDATE ON workflow_leases
                WHEN NEW.lease_id <> OLD.lease_id
                  OR NEW.workflow_run_id <> OLD.workflow_run_id
                  OR NEW.node_run_id <> OLD.node_run_id
                  OR NEW.worker_id <> OLD.worker_id
                  OR NEW.generation <> OLD.generation
                  OR NEW.fencing_token_sha256 <> OLD.fencing_token_sha256
                  OR NEW.acquired_at <> OLD.acquired_at
                BEGIN SELECT RAISE(ABORT, 'workflow lease identity is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_leases_terminal_no_update
                BEFORE UPDATE ON workflow_leases
                WHEN OLD.state <> 'active'
                BEGIN SELECT RAISE(ABORT, 'terminal workflow leases are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_sessions_identity_immutable
                BEFORE UPDATE ON workflow_sessions
                WHEN NEW.session_record_id <> OLD.session_record_id
                  OR NEW.workflow_run_id <> OLD.workflow_run_id
                  OR NEW.session_group <> OLD.session_group
                  OR NEW.executor <> OLD.executor
                  OR NEW.generation <> OLD.generation
                  OR NEW.created_at <> OLD.created_at
                BEGIN SELECT RAISE(ABORT, 'workflow session identity is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_sessions_binding_immutable
                BEFORE UPDATE ON workflow_sessions
                WHEN OLD.external_session_id <> ''
                  AND NEW.external_session_id <> OLD.external_session_id
                BEGIN SELECT RAISE(ABORT, 'workflow session binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_sessions_state_transition
                BEFORE UPDATE OF state ON workflow_sessions
                WHEN NOT (
                    OLD.state = NEW.state
                    OR (OLD.state = 'active' AND NEW.state IN ('rotated', 'closed'))
                )
                BEGIN SELECT RAISE(ABORT, 'invalid workflow session state transition'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_sessions_terminal_no_update
                BEFORE UPDATE ON workflow_sessions
                WHEN OLD.state <> 'active'
                BEGIN SELECT RAISE(ABORT, 'terminal workflow sessions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workflow_sessions_no_delete
                BEFORE DELETE ON workflow_sessions
                BEGIN SELECT RAISE(ABORT, 'workflow sessions are append-only'); END;
                COMMIT;
                """
                )
            except Exception:
                connection.rollback()
                raise
            row = connection.execute("SELECT value FROM workflow_schema_meta WHERE key='schema_version'").fetchone()
            if row is None or str(row["value"]) != WORKFLOW_STORE_SCHEMA_VERSION:
                raise WorkflowStoreError("unsupported workflow database schema")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            return {
                "schema_version": connection.execute(
                    "SELECT value FROM workflow_schema_meta WHERE key='schema_version'"
                ).fetchone()["value"],
                "version_count": connection.execute("SELECT COUNT(*) AS count FROM workflow_versions").fetchone()[
                    "count"
                ],
                "run_count": connection.execute("SELECT COUNT(*) AS count FROM workflow_runs").fetchone()["count"],
                "active_lease_count": connection.execute(
                    "SELECT COUNT(*) AS count FROM workflow_leases WHERE state='active'"
                ).fetchone()["count"],
            }

    def active_lease_counts_by_node_type(self) -> dict[str, int]:
        """Return live lease occupancy without exposing fencing credentials."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT node.node_snapshot_json FROM workflow_leases AS lease "
                "JOIN node_runs AS node ON node.node_run_id=lease.node_run_id "
                "WHERE lease.state='active'"
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            node = NodeDefinition.from_dict(json.loads(row["node_snapshot_json"]))
            key = node.node_type.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _version_record(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        payload = json.loads(row["definition_json"])
        payload["status"] = str(row["status"])
        version = WorkflowVersion.from_dict(payload)
        if version.definition_sha256 != str(row["definition_sha256"]) or version.definition.sha256() != str(
            row["definition_sha256"]
        ):
            raise WorkflowStoreError("stored workflow version digest does not match its content")
        try:
            WorkflowStore._validate_definition_protection(
                version.definition,
                is_builtin=bool(row["is_builtin"]),
                is_readonly=bool(row["is_readonly"]),
            )
        except (TypeError, ValueError) as exc:
            raise WorkflowStoreError("stored workflow protection flags do not match its definition") from exc
        return {
            "version": version,
            "is_builtin": bool(row["is_builtin"]),
            "is_readonly": bool(row["is_readonly"]),
        }

    @staticmethod
    def _validate_definition_protection(
        definition: WorkflowDefinition,
        *,
        is_builtin: bool,
        is_readonly: bool,
    ) -> None:
        """Keep portable protection metadata and authoritative row flags aligned."""

        if not isinstance(definition, WorkflowDefinition):
            raise TypeError("definition must be a WorkflowDefinition")
        if is_builtin is not is_readonly:
            raise ValueError("workflow builtin and readonly protection flags must be enabled together")
        for key, flag in (("builtin", is_builtin), ("readonly", is_readonly)):
            if key not in definition.metadata:
                if flag:
                    raise ValueError(f"protected workflow definitions must declare metadata.{key}=true")
                continue
            value = definition.metadata[key]
            if not isinstance(value, bool) or value is not flag:
                raise ValueError(f"workflow metadata.{key} must match its store protection flag")
        if any(node.node_type is NodeType.LEGACY_STAGE for node in definition.nodes) and not (
            is_builtin and is_readonly
        ):
            raise ValueError("legacy-stage workflow versions must be built-in and read-only")

    @staticmethod
    def _ensure_version(
        connection: sqlite3.Connection,
        version: WorkflowVersion,
        *,
        is_builtin: bool,
        is_readonly: bool,
    ) -> None:
        WorkflowStore._validate_definition_protection(
            version.definition,
            is_builtin=is_builtin,
            is_readonly=is_readonly,
        )
        version.validate(mode=ValidationMode.GRAPH)
        if version.status is WorkflowVersionStatus.PUBLISHED:
            version.validate(mode=ValidationMode.LINEAR)
        payload = canonical_json(version.to_dict())
        existing = connection.execute(
            "SELECT * FROM workflow_versions WHERE version_id=?",
            (version.version_id,),
        ).fetchone()
        if existing is not None:
            existing_payload = json.loads(existing["definition_json"])
            requested_payload = json.loads(payload)
            existing_payload.pop("status", None)
            requested_payload.pop("status", None)
            if (
                canonical_json(existing_payload) != canonical_json(requested_payload)
                or bool(existing["is_builtin"]) != is_builtin
                or bool(existing["is_readonly"]) != is_readonly
            ):
                raise WorkflowConflictError(
                    f"workflow version identity reused with different content: {version.version_id}"
                )
            return
        try:
            connection.execute(
                "INSERT INTO workflow_versions("
                "version_id, workflow_id, version, definition_sha256, definition_json, "
                "status, is_builtin, is_readonly, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version.version_id,
                    version.workflow_id,
                    version.version,
                    version.definition_sha256,
                    payload,
                    version.status.value,
                    int(is_builtin),
                    int(is_readonly),
                    version.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflictError(
                f"workflow version number already exists: {version.workflow_id}@{version.version}"
            ) from exc

    def _register_version_locked(
        self,
        connection: sqlite3.Connection,
        version: WorkflowVersion,
        *,
        is_builtin: bool,
        is_readonly: bool,
    ) -> dict[str, Any]:
        if not isinstance(version, WorkflowVersion):
            raise TypeError("version must be a WorkflowVersion")
        if not isinstance(is_builtin, bool) or not isinstance(is_readonly, bool):
            raise TypeError("workflow version flags must be boolean")
        self._validate_definition_protection(
            version.definition,
            is_builtin=is_builtin,
            is_readonly=is_readonly,
        )
        if version.status is WorkflowVersionStatus.DRAFT and is_builtin:
            raise ValueError("protected workflow versions cannot be registered as drafts")
        if version.status is WorkflowVersionStatus.PUBLISHED and not (is_builtin and is_readonly):
            raise ValueError("custom versions must enter through the custom draft publication API")
        if version.status is WorkflowVersionStatus.DEPRECATED and not is_builtin:
            raise ValueError("custom versions cannot enter the store as deprecated history")
        existing = connection.execute(
            "SELECT * FROM workflow_versions WHERE version_id=?",
            (version.version_id,),
        ).fetchone()
        history = connection.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id=? ORDER BY version",
            (version.workflow_id,),
        ).fetchall()
        if existing is None and history:
            if any(
                bool(row["is_builtin"]) is not is_builtin or bool(row["is_readonly"]) is not is_readonly
                for row in history
            ):
                raise WorkflowConflictError("workflow history cannot mix custom and protected versions")
            if not is_builtin:
                raise WorkflowConflictError("existing custom history must use append_custom_version")
            if version.version <= max(int(row["version"]) for row in history):
                raise WorkflowConflictError("protected release version must be newer than stored history")
        if version.status is WorkflowVersionStatus.PUBLISHED and existing is None:
            for row in history:
                if str(row["status"]) == WorkflowVersionStatus.PUBLISHED.value:
                    connection.execute(
                        "UPDATE workflow_versions SET status=? WHERE version_id=? AND status=?",
                        (
                            WorkflowVersionStatus.DEPRECATED.value,
                            row["version_id"],
                            WorkflowVersionStatus.PUBLISHED.value,
                        ),
                    )
        self._ensure_version(
            connection,
            version,
            is_builtin=is_builtin,
            is_readonly=is_readonly,
        )
        row = connection.execute(
            "SELECT * FROM workflow_versions WHERE version_id=?",
            (version.version_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStoreError("registered workflow version disappeared")
        return self._version_record(row)

    def register_version(
        self,
        version: WorkflowVersion,
        *,
        is_builtin: bool = False,
        is_readonly: bool = False,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            return self._register_version_locked(
                connection,
                version,
                is_builtin=is_builtin,
                is_readonly=is_readonly,
            )

    def register_builtin_versions(
        self,
        versions: Sequence[WorkflowVersion],
    ) -> tuple[dict[str, Any], ...]:
        """Install or upgrade a built-in pack as one all-or-nothing transaction."""

        frozen = tuple(versions)
        if not frozen:
            raise ValueError("built-in workflow pack cannot be empty")
        if any(not isinstance(version, WorkflowVersion) for version in frozen):
            raise TypeError("built-in workflow pack entries must be WorkflowVersion values")
        if len({version.workflow_id for version in frozen}) != len(frozen):
            raise ValueError("built-in workflow pack contains duplicate workflow IDs")
        with self.transaction() as connection:
            return tuple(
                self._register_version_locked(
                    connection,
                    version,
                    is_builtin=True,
                    is_readonly=True,
                )
                for version in frozen
            )

    def get_version(self, version_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return self._version_record(row)

    def list_versions(self, workflow_id: str = "") -> list[dict[str, Any]]:
        with self.connect() as connection:
            if workflow_id:
                rows = connection.execute(
                    "SELECT * FROM workflow_versions WHERE workflow_id=? ORDER BY version",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM workflow_versions ORDER BY workflow_id, version").fetchall()
        return [self._version_record(row) for row in rows]

    def set_version_status(
        self,
        version_id: str,
        status: str,
        *,
        expected_status: str,
    ) -> dict[str, Any]:
        target = WorkflowVersionStatus(status)
        expected = WorkflowVersionStatus(expected_status)
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM workflow_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            if current is None:
                raise KeyError(version_id)
            if target is WorkflowVersionStatus.PUBLISHED:
                if expected is not WorkflowVersionStatus.DRAFT:
                    raise WorkflowConflictError("custom publication requires an expected draft status")
                record = self._publish_custom_version_locked(connection, current)
                return self._version_record(record)
            if target is not WorkflowVersionStatus.DEPRECATED or expected is not WorkflowVersionStatus.PUBLISHED:
                raise WorkflowConflictError("invalid workflow version status transition")
            if bool(current["is_builtin"]) or bool(current["is_readonly"]):
                raise WorkflowConflictError("built-in/read-only workflow status is managed only by pack registration")
            self._validate_custom_definition(self._version_record(current)["version"].definition)
            try:
                updated = connection.execute(
                    "UPDATE workflow_versions SET status=? WHERE version_id=? AND status=?",
                    (target.value, version_id, expected.value),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                raise WorkflowConflictError("invalid workflow version status transition") from exc
            if updated != 1:
                raise WorkflowConflictError("workflow version status changed concurrently")
            row = connection.execute(
                "SELECT * FROM workflow_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise KeyError(version_id)
            record = dict(row)
        return self._version_record(record)

    @staticmethod
    def _validate_custom_definition(definition: WorkflowDefinition) -> None:
        if not isinstance(definition, WorkflowDefinition):
            raise TypeError("definition must be a WorkflowDefinition")
        definition.validate(mode=ValidationMode.LINEAR)
        if any(
            key in definition.metadata and definition.metadata[key] is not False
            for key in ("builtin", "readonly")
        ):
            raise ValueError("custom workflow definitions cannot be built-in or read-only")
        if any(node.node_type is NodeType.LEGACY_STAGE for node in definition.nodes):
            raise ValueError("custom workflow definitions cannot activate legacy-stage nodes")

    def _publish_custom_version_locked(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row | Mapping[str, Any],
    ) -> dict[str, Any]:
        version_id = str(current["version_id"])
        if bool(current["is_builtin"]) or bool(current["is_readonly"]):
            raise WorkflowConflictError("built-in/read-only workflow versions cannot be published by the editor")
        if str(current["status"]) != WorkflowVersionStatus.DRAFT.value:
            raise WorkflowConflictError("only a custom draft can be published")
        current_version = self._version_record(current)["version"]
        self._validate_custom_definition(current_version.definition)
        latest = connection.execute(
            "SELECT version_id FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC LIMIT 1",
            (current["workflow_id"],),
        ).fetchone()
        if latest is None or str(latest["version_id"]) != version_id:
            raise WorkflowConflictError("only the latest custom draft can be published")
        published = connection.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id=? AND status=? AND version_id<>?",
            (
                current["workflow_id"],
                WorkflowVersionStatus.PUBLISHED.value,
                version_id,
            ),
        ).fetchall()
        if any(bool(row["is_builtin"]) or bool(row["is_readonly"]) for row in published):
            raise WorkflowConflictError("custom release cannot replace a protected workflow version")
        for row in published:
            self._validate_custom_definition(self._version_record(row)["version"].definition)
            connection.execute(
                "UPDATE workflow_versions SET status=? WHERE version_id=? AND status=?",
                (
                    WorkflowVersionStatus.DEPRECATED.value,
                    row["version_id"],
                    WorkflowVersionStatus.PUBLISHED.value,
                ),
            )
        updated = connection.execute(
            "UPDATE workflow_versions SET status=? WHERE version_id=? AND status=?",
            (
                WorkflowVersionStatus.PUBLISHED.value,
                version_id,
                WorkflowVersionStatus.DRAFT.value,
            ),
        ).rowcount
        if updated != 1:
            raise WorkflowConflictError("custom workflow status changed concurrently")
        refreshed = connection.execute(
            "SELECT * FROM workflow_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if refreshed is None:
            raise WorkflowStoreError("published workflow version disappeared")
        return dict(refreshed)

    def append_custom_version(
        self,
        definition: WorkflowDefinition,
        *,
        expected_parent_version_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically append an immutable custom draft.

        ``expected_parent_version_id`` is a compare-and-swap guard. It must be
        empty for a brand-new workflow and must identify the latest custom
        version for every subsequent save.
        """

        self._validate_custom_definition(definition)
        if not isinstance(expected_parent_version_id, str):
            raise TypeError("expected_parent_version_id must be a string")
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            latest = connection.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC LIMIT 1",
                (definition.workflow_id,),
            ).fetchone()
            if expected_parent_version_id:
                parent = connection.execute(
                    "SELECT * FROM workflow_versions WHERE version_id=?",
                    (expected_parent_version_id,),
                ).fetchone()
                if parent is None:
                    raise WorkflowConflictError("expected parent workflow version does not exist")
                if str(parent["workflow_id"]) != definition.workflow_id:
                    raise WorkflowConflictError("expected parent belongs to a different workflow")
                if bool(parent["is_builtin"]) or bool(parent["is_readonly"]):
                    raise WorkflowConflictError("protected workflow versions must be cloned under a new workflow ID")
                try:
                    self._validate_custom_definition(self._version_record(parent)["version"].definition)
                except (TypeError, ValueError, WorkflowStoreError) as exc:
                    raise WorkflowConflictError("expected parent is not a valid custom workflow version") from exc
                if latest is None or str(latest["version_id"]) != expected_parent_version_id:
                    raise WorkflowConflictError("custom workflow changed concurrently")
                version_number = int(parent["version"]) + 1
            else:
                if latest is not None:
                    raise WorkflowConflictError("existing custom workflow requires an expected parent version")
                version_number = 1
            version = WorkflowVersion.from_definition(
                definition,
                version=version_number,
                version_id=_new_id("workflow_version"),
                created_at=timestamp,
                status=WorkflowVersionStatus.DRAFT,
                mode=ValidationMode.LINEAR,
            )
            self._ensure_version(
                connection,
                version,
                is_builtin=False,
                is_readonly=False,
            )
            row = connection.execute(
                "SELECT * FROM workflow_versions WHERE version_id=?",
                (version.version_id,),
            ).fetchone()
            if row is None:
                raise WorkflowStoreError("custom workflow version disappeared during append")
            record = dict(row)
        return self._version_record(record)

    def publish_custom_version(self, version_id: str) -> dict[str, Any]:
        """Publish the latest custom draft and deprecate its prior release."""

        if not isinstance(version_id, str) or not version_id.strip():
            raise ValueError("version_id is required")
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM workflow_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            if current is None:
                raise KeyError(version_id)
            record = self._publish_custom_version_locked(connection, current)
        return self._version_record(record)

    @staticmethod
    def _ensure_binding(
        connection: sqlite3.Connection,
        definition: WorkflowDefinition,
        binding: WorkflowRuntimeBinding,
        *,
        timestamp: str,
    ) -> str:
        binding.validate(definition)
        digest = binding.sha256()
        payload = canonical_json(binding.to_dict())
        existing = connection.execute(
            "SELECT * FROM workflow_runtime_bindings WHERE binding_sha256=?",
            (digest,),
        ).fetchone()
        if existing is not None:
            restored = WorkflowRuntimeBinding.from_dict(json.loads(existing["binding_json"]))
            if (
                restored.canonical_json() != binding.canonical_json()
                or str(existing["definition_sha256"]) != definition.sha256()
            ):
                raise WorkflowConflictError("runtime binding digest collision")
            return digest
        connection.execute(
            "INSERT INTO workflow_runtime_bindings("
            "binding_sha256, definition_sha256, binding_json, created_at"
            ") VALUES (?, ?, ?, ?)",
            (digest, definition.sha256(), payload, timestamp),
        )
        return digest

    def register_runtime_binding(
        self,
        definition: WorkflowDefinition,
        binding: WorkflowRuntimeBinding,
    ) -> WorkflowRuntimeBinding:
        timestamp = format_utc(utc_now())
        with self.transaction() as connection:
            digest = self._ensure_binding(
                connection,
                definition,
                binding,
                timestamp=timestamp,
            )
        return self.get_runtime_binding(digest)

    def get_runtime_binding(self, binding_sha256: str) -> WorkflowRuntimeBinding:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runtime_bindings WHERE binding_sha256=?",
                (binding_sha256,),
            ).fetchone()
        if row is None:
            raise KeyError(binding_sha256)
        binding = WorkflowRuntimeBinding.from_dict(json.loads(row["binding_json"]))
        if binding.sha256() != str(row["binding_sha256"]) or binding.definition_sha256 != str(row["definition_sha256"]):
            raise WorkflowStoreError("stored runtime binding digest does not match its content")
        return binding

    def create_run(
        self,
        version: WorkflowVersion,
        binding: WorkflowRuntimeBinding,
        *,
        inputs: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        owner: str = "generic",
        idempotency_key: str = "",
        run_id: str = "",
        is_builtin: bool = False,
        is_readonly: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not owner.strip():
            raise ValueError("workflow run owner is required")
        if version.status is not WorkflowVersionStatus.PUBLISHED:
            raise ValueError("workflow runs require a published workflow version")
        version.validate(mode=ValidationMode.LINEAR)
        timestamp = format_utc(_current_time(now))
        input_data = dict(inputs or {})
        metadata_data = dict(metadata or {})
        request = {
            "workflow_version_id": version.version_id,
            "definition_sha256": version.definition_sha256,
            "binding_sha256": binding.sha256(),
            "inputs": input_data,
            "metadata": metadata_data,
            "owner": owner,
        }
        request_sha256 = hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
        durable_run_id = run_id or _new_id("workflow_run")
        with self.transaction() as connection:
            self._ensure_version(
                connection,
                version,
                is_builtin=is_builtin,
                is_readonly=is_readonly,
            )
            persisted_version = connection.execute(
                "SELECT status FROM workflow_versions WHERE version_id=?",
                (version.version_id,),
            ).fetchone()
            if persisted_version is None or str(persisted_version["status"]) != WorkflowVersionStatus.PUBLISHED.value:
                raise WorkflowConflictError("persisted workflow version is not published")
            binding_sha256 = self._ensure_binding(
                connection,
                version.definition,
                binding,
                timestamp=timestamp,
            )
            if idempotency_key:
                existing = connection.execute(
                    "SELECT run_id, request_sha256 FROM workflow_runs WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_sha256"]) != request_sha256:
                        raise WorkflowConflictError("workflow run idempotency key reused with different content")
                    return self._run_record(connection, str(existing["run_id"]))
            run = WorkflowRun(
                run_id=durable_run_id,
                workflow_version_id=version.version_id,
                definition_sha256=version.definition_sha256,
                binding_sha256=binding_sha256,
                status=WorkflowRunStatus.QUEUED,
                created_at=timestamp,
                inputs=input_data,
                metadata=metadata_data,
            )
            run.to_dict()
            try:
                connection.execute(
                    "INSERT INTO workflow_runs("
                    "run_id, workflow_version_id, definition_sha256, binding_sha256, owner, "
                    "status, idempotency_key, request_sha256, inputs_json, metadata_json, "
                    "created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id,
                        run.workflow_version_id,
                        run.definition_sha256,
                        run.binding_sha256,
                        owner,
                        run.status.value,
                        idempotency_key,
                        request_sha256,
                        canonical_json(input_data),
                        canonical_json(metadata_data),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkflowConflictError(f"workflow run already exists: {run.run_id}") from exc
            self._event(
                connection,
                run.run_id,
                "run.created",
                {
                    "workflow_version_id": run.workflow_version_id,
                    "definition_sha256": run.definition_sha256,
                    "binding_sha256": run.binding_sha256,
                    "owner": owner,
                },
                timestamp=timestamp,
            )
            for node in sorted(
                version.definition.nodes,
                key=lambda item: (item.display_order, item.node_id),
            ):
                node_run = NodeRun(
                    node_run_id=_new_id("node_run"),
                    workflow_run_id=run.run_id,
                    node_id=node.node_id,
                    attempt=1,
                    status=NodeRunStatus.PENDING,
                    created_at=timestamp,
                )
                node_run.to_dict()
                connection.execute(
                    "INSERT INTO node_runs("
                    "node_run_id, workflow_run_id, node_id, display_order, attempt, status, "
                    "node_snapshot_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        node_run.node_run_id,
                        run.run_id,
                        node.node_id,
                        node.display_order,
                        node_run.attempt,
                        node_run.status.value,
                        canonical_json(node.to_dict()),
                        timestamp,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    run.run_id,
                    "node.created",
                    {"node_id": node.node_id, "attempt": 1},
                    node_run_id=node_run.node_run_id,
                    timestamp=timestamp,
                )
            return self._run_record(connection, run.run_id)

    @staticmethod
    def _run_record(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "run_id": str(row["run_id"]),
            "workflow_version_id": str(row["workflow_version_id"]),
            "definition_sha256": str(row["definition_sha256"]),
            "binding_sha256": str(row["binding_sha256"]),
            "owner": str(row["owner"]),
            "status": str(row["status"]),
            "idempotency_key": str(row["idempotency_key"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "inputs": json.loads(row["inputs_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"]),
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            return self._run_record(connection, run_id)

    def list_runs(
        self,
        *,
        statuses: Sequence[WorkflowRunStatus | str] = (),
        owner: str = "",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        status_values = tuple(WorkflowRunStatus(item).value for item in statuses)
        clauses: list[str] = []
        parameters: list[Any] = []
        if status_values:
            clauses.append(f"status IN ({','.join('?' for _ in status_values)})")
            parameters.extend(status_values)
        if owner:
            clauses.append("owner=?")
            parameters.append(owner)
        sql = "SELECT run_id FROM workflow_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, run_id LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
            return [self._run_record(connection, str(row["run_id"])) for row in rows]

    @staticmethod
    def _node_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "node_run_id": str(row["node_run_id"]),
            "workflow_run_id": str(row["workflow_run_id"]),
            "node_id": str(row["node_id"]),
            "display_order": int(row["display_order"]),
            "attempt": int(row["attempt"]),
            "status": str(row["status"]),
            "node": NodeDefinition.from_dict(json.loads(row["node_snapshot_json"])),
            "session_id": str(row["session_id"]),
            "error_code": str(row["error_code"]),
            "error_message": str(row["error_message"]),
            "outputs": json.loads(row["outputs_json"]),
            "ready_at": str(row["ready_at"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"]),
        }

    def get_node_run(self, node_run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(node_run_id)
        return self._node_record(row)

    def list_node_runs(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM node_runs WHERE workflow_run_id=? ORDER BY display_order, attempt",
                (run_id,),
            ).fetchall()
        return [self._node_record(row) for row in rows]

    def latest_node_runs(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT node.* FROM node_runs AS node "
                "JOIN ("
                "  SELECT node_id, MAX(attempt) AS attempt FROM node_runs "
                "  WHERE workflow_run_id=? GROUP BY node_id"
                ") AS latest ON latest.node_id=node.node_id AND latest.attempt=node.attempt "
                "WHERE node.workflow_run_id=? ORDER BY node.display_order",
                (run_id, run_id),
            ).fetchall()
        return [self._node_record(row) for row in rows]

    def mark_node_ready(
        self,
        node_run_id: str,
        *,
        expected_status: NodeRunStatus = NodeRunStatus.PENDING,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        expected = NodeRunStatus(expected_status)
        if expected is not NodeRunStatus.PENDING:
            raise ValueError("only a pending node attempt can become ready")
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(node_run_id)
            run = connection.execute(
                "SELECT cancel_requested, status FROM workflow_runs WHERE run_id=?",
                (row["workflow_run_id"],),
            ).fetchone()
            if run is None:
                raise WorkflowStoreError("node run has no workflow run")
            if bool(run["cancel_requested"]) or str(run["status"]) not in {
                WorkflowRunStatus.QUEUED.value,
                WorkflowRunStatus.RUNNING.value,
            }:
                raise WorkflowConflictError("only an active queued or running workflow can make a node ready")
            updated = connection.execute(
                "UPDATE node_runs SET status=?, updated_at=? WHERE node_run_id=? AND status=?",
                (
                    NodeRunStatus.READY.value,
                    timestamp,
                    node_run_id,
                    expected.value,
                ),
            ).rowcount
            if updated != 1:
                raise WorkflowConflictError("node status changed concurrently")
            self._event(
                connection,
                str(row["workflow_run_id"]),
                "node.ready",
                {"node_id": str(row["node_id"]), "attempt": int(row["attempt"])},
                node_run_id=node_run_id,
                timestamp=timestamp,
            )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("node run disappeared")
            return self._node_record(refreshed)

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise TypeError("after_sequence must be an integer")
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000):
            raise ValueError("limit must be an integer between 1 and 10000")
        with self.connect() as connection:
            sql = "SELECT * FROM workflow_events WHERE workflow_run_id=? AND sequence>? ORDER BY sequence"
            parameters: tuple[Any, ...] = (run_id, after_sequence)
            if limit is not None:
                sql += " LIMIT ?"
                parameters += (limit,)
            rows = connection.execute(sql, parameters).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "run_id": str(row["workflow_run_id"]),
                "sequence": int(row["sequence"]),
                "event_type": str(row["event_type"]),
                "node_run_id": str(row["node_run_id"] or ""),
                "payload": json.loads(row["payload_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_artifacts WHERE workflow_run_id=? ORDER BY created_at, artifact_record_id",
                (run_id,),
            ).fetchall()
        return [
            {
                "artifact_record_id": str(row["artifact_record_id"]),
                "run_id": str(row["workflow_run_id"]),
                "node_run_id": str(row["node_run_id"] or ""),
                "producer_node_id": str(row["producer_node_id"]),
                "artifact_id": str(row["artifact_id"]),
                "uri": str(row["uri"]),
                "sha256": str(row["sha256"]),
                "media_type": str(row["media_type"]),
                "schema_ref": str(row["schema_ref"]),
                "size_bytes": row["size_bytes"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def read_run_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read one run, all attempts, events, artifacts and sessions consistently."""

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        with self.connect() as connection:
            connection.execute("BEGIN")
            try:
                run = self._run_record(connection, run_id)
                version_row = connection.execute(
                    "SELECT * FROM workflow_versions WHERE version_id=?",
                    (run["workflow_version_id"],),
                ).fetchone()
                if version_row is None:
                    raise WorkflowStoreError("workflow run references a missing version")
                node_rows = connection.execute(
                    "SELECT * FROM node_runs WHERE workflow_run_id=? ORDER BY display_order, attempt",
                    (run_id,),
                ).fetchall()
                event_rows = connection.execute(
                    "SELECT * FROM workflow_events WHERE workflow_run_id=? ORDER BY sequence",
                    (run_id,),
                ).fetchall()
                artifact_rows = connection.execute(
                    "SELECT * FROM workflow_artifacts WHERE workflow_run_id=? "
                    "ORDER BY created_at, artifact_record_id",
                    (run_id,),
                ).fetchall()
                session_rows = connection.execute(
                    "SELECT * FROM workflow_sessions WHERE workflow_run_id=? "
                    "ORDER BY session_group, executor, generation",
                    (run_id,),
                ).fetchall()
                snapshot = {
                    "run": run,
                    "version": self._version_record(version_row),
                    "node_attempts": [self._node_record(row) for row in node_rows],
                    "events": [
                        {
                            "event_id": int(row["event_id"]),
                            "run_id": str(row["workflow_run_id"]),
                            "sequence": int(row["sequence"]),
                            "event_type": str(row["event_type"]),
                            "node_run_id": str(row["node_run_id"] or ""),
                            "payload": json.loads(row["payload_json"]),
                            "created_at": str(row["created_at"]),
                        }
                        for row in event_rows
                    ],
                    "artifacts": [
                        {
                            "artifact_record_id": str(row["artifact_record_id"]),
                            "run_id": str(row["workflow_run_id"]),
                            "node_run_id": str(row["node_run_id"] or ""),
                            "producer_node_id": str(row["producer_node_id"]),
                            "artifact_id": str(row["artifact_id"]),
                            "uri": str(row["uri"]),
                            "sha256": str(row["sha256"]),
                            "media_type": str(row["media_type"]),
                            "schema_ref": str(row["schema_ref"]),
                            "size_bytes": row["size_bytes"],
                            "metadata": json.loads(row["metadata_json"]),
                            "created_at": str(row["created_at"]),
                        }
                        for row in artifact_rows
                    ],
                    "sessions": [self._session_record(row) for row in session_rows],
                }
            finally:
                connection.rollback()
        return snapshot

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be an integer between 1 and 86400")

    @staticmethod
    def _lease_grant(row: sqlite3.Row, fencing_token: str) -> LeaseGrant:
        return LeaseGrant(
            lease_id=str(row["lease_id"]),
            run_id=str(row["workflow_run_id"]),
            node_run_id=str(row["node_run_id"]),
            generation=int(row["generation"]),
            fencing_token=fencing_token,
            acquired_at=str(row["acquired_at"]),
            expires_at=str(row["expires_at"]),
        )

    @staticmethod
    def _require_live_lease(
        connection: sqlite3.Connection,
        grant: LeaseGrant,
        *,
        now: datetime,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workflow_leases WHERE lease_id=?",
            (grant.lease_id,),
        ).fetchone()
        expected_hash = _token_sha256(grant.fencing_token)
        if (
            row is None
            or str(row["workflow_run_id"]) != grant.run_id
            or str(row["node_run_id"]) != grant.node_run_id
            or int(row["generation"]) != grant.generation
            or str(row["state"]) != "active"
            or not secrets.compare_digest(str(row["fencing_token_sha256"]), expected_hash)
            or parse_utc(str(row["expires_at"])) <= now.astimezone(timezone.utc)
        ):
            raise LeaseLostError("workflow lease is stale, expired, or fenced")
        return row

    def assert_live_lease(
        self,
        grant: LeaseGrant,
        *,
        now: datetime | None = None,
    ) -> LeaseGrant:
        """Fail closed unless ``grant`` still owns its running node attempt.

        This read-only preflight is used before executor-adapter operations
        that may perform external side effects. Mutating ledger operations
        must still repeat fencing inside their own transaction.
        """

        current = _current_time(now)
        with self.transaction() as connection:
            row = self._require_live_lease(connection, grant, now=current)
            node = connection.execute(
                "SELECT node.status, run.status AS run_status, run.cancel_requested "
                "FROM node_runs AS node JOIN workflow_runs AS run "
                "ON run.run_id=node.workflow_run_id "
                "WHERE node.node_run_id=? AND node.workflow_run_id=?",
                (grant.node_run_id, grant.run_id),
            ).fetchone()
            if node is None or str(node["status"]) != NodeRunStatus.RUNNING.value:
                raise LeaseLostError("workflow lease no longer owns a running node")
            if bool(node["cancel_requested"]) or str(node["run_status"]) in _TERMINAL_RUN_STATUSES:
                raise WorkflowConflictError("cancelling or terminal workflow cannot start external work")
            return self._lease_grant(row, grant.fencing_token)

    def claim_node(
        self,
        node_run_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 300,
        global_active_limit: int | None = None,
        node_type_active_limit: int | None = None,
        enforce_linear_order: bool = False,
        now: datetime | None = None,
    ) -> LeaseGrant | None:
        self._validate_lease_seconds(lease_seconds)
        for name, value in {
            "global_active_limit": global_active_limit,
            "node_type_active_limit": node_type_active_limit,
        }.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or null")
        if not isinstance(enforce_linear_order, bool):
            raise TypeError("enforce_linear_order must be a boolean")
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        current = _current_time(now)
        timestamp = format_utc(current)
        expires_at = format_utc(current + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if node is None:
                raise KeyError(node_run_id)
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (node["workflow_run_id"],),
            ).fetchone()
            if run is None:
                raise WorkflowStoreError("node run has no workflow run")
            if (
                str(node["status"]) != NodeRunStatus.READY.value
                or bool(run["cancel_requested"])
                or str(run["status"])
                not in {
                    WorkflowRunStatus.QUEUED.value,
                    WorkflowRunStatus.RUNNING.value,
                }
            ):
                return None
            if enforce_linear_order:
                version = connection.execute(
                    "SELECT definition_json FROM workflow_versions WHERE version_id=?",
                    (run["workflow_version_id"],),
                ).fetchone()
                if version is None:
                    raise WorkflowConflictError("linear claim has no frozen workflow definition")
                definition = WorkflowVersion.from_dict(json.loads(version["definition_json"])).definition
                outgoing = {edge.source_node_id: edge.target_node_id for edge in definition.edges}
                incoming = {edge.target_node_id for edge in definition.edges}
                entries = [item.node_id for item in definition.nodes if item.node_id not in incoming]
                if len(entries) != 1:
                    raise WorkflowConflictError("linear claim requires exactly one entry node")
                order: list[str] = []
                cursor = entries[0]
                while cursor not in order:
                    order.append(cursor)
                    if cursor not in outgoing:
                        break
                    cursor = outgoing[cursor]
                if len(order) != len(definition.nodes):
                    raise WorkflowConflictError("linear claim found a disconnected or cyclic definition")
                latest_rows = connection.execute(
                    "SELECT candidate.* FROM node_runs AS candidate JOIN ("
                    "SELECT node_id, MAX(attempt) AS attempt FROM node_runs "
                    "WHERE workflow_run_id=? GROUP BY node_id"
                    ") AS latest ON latest.node_id=candidate.node_id "
                    "AND latest.attempt=candidate.attempt "
                    "WHERE candidate.workflow_run_id=?",
                    (node["workflow_run_id"], node["workflow_run_id"]),
                ).fetchall()
                latest = {str(item["node_id"]): item for item in latest_rows}
                if set(latest) != set(order):
                    raise WorkflowConflictError("linear claim latest-attempt set is incomplete")
                target_index = order.index(str(node["node_id"]))
                for index, node_id in enumerate(order):
                    status = str(latest[node_id]["status"])
                    if index < target_index and status not in {
                        NodeRunStatus.COMPLETED.value,
                        NodeRunStatus.SKIPPED.value,
                    }:
                        raise WorkflowConflictError("linear claim predecessor is not completed")
                    if index == target_index and status != NodeRunStatus.READY.value:
                        raise WorkflowConflictError("linear claim target is not the sole ready node")
                    if index > target_index and status != NodeRunStatus.PENDING.value:
                        raise WorkflowConflictError("linear claim successor became active out of order")
            if global_active_limit is not None:
                global_active = int(
                    connection.execute(
                        "SELECT COUNT(*) AS value FROM workflow_leases WHERE state='active'"
                    ).fetchone()["value"]
                )
                if global_active >= global_active_limit:
                    return None
            if node_type_active_limit is not None:
                claimed_type = NodeDefinition.from_dict(json.loads(node["node_snapshot_json"])).node_type
                active_rows = connection.execute(
                    "SELECT node.node_snapshot_json FROM workflow_leases AS lease "
                    "JOIN node_runs AS node ON node.node_run_id=lease.node_run_id "
                    "WHERE lease.state='active'"
                ).fetchall()
                active_for_type = sum(
                    NodeDefinition.from_dict(json.loads(item["node_snapshot_json"])).node_type
                    is claimed_type
                    for item in active_rows
                )
                if active_for_type >= node_type_active_limit:
                    return None
            active = connection.execute(
                "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
                (node["workflow_run_id"],),
            ).fetchone()
            if active is not None:
                return None
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(lease.generation), 0) + 1 AS value "
                    "FROM workflow_leases AS lease "
                    "JOIN node_runs AS prior ON prior.node_run_id=lease.node_run_id "
                    "WHERE prior.workflow_run_id=? AND prior.node_id=?",
                    (node["workflow_run_id"], node["node_id"]),
                ).fetchone()["value"]
            )
            lease_id = _new_id("lease")
            token = secrets.token_urlsafe(32)
            try:
                connection.execute(
                    "INSERT INTO workflow_leases("
                    "lease_id, workflow_run_id, node_run_id, worker_id, generation, "
                    "fencing_token_sha256, state, acquired_at, heartbeat_at, expires_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        lease_id,
                        node["workflow_run_id"],
                        node_run_id,
                        worker_id,
                        generation,
                        _token_sha256(token),
                        timestamp,
                        timestamp,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkflowConflictError("node claim violated the one-active-node workflow contract") from exc
            updated = connection.execute(
                "UPDATE node_runs SET status=?, started_at=?, updated_at=? WHERE node_run_id=? AND status=?",
                (
                    NodeRunStatus.RUNNING.value,
                    timestamp,
                    timestamp,
                    node_run_id,
                    NodeRunStatus.READY.value,
                ),
            ).rowcount
            if updated != 1:
                raise WorkflowConflictError("node claim lost its compare-and-swap")
            connection.execute(
                "UPDATE workflow_runs SET status=?, started_at=CASE "
                "WHEN started_at='' THEN ? ELSE started_at END, updated_at=? WHERE run_id=?",
                (
                    WorkflowRunStatus.RUNNING.value,
                    timestamp,
                    timestamp,
                    node["workflow_run_id"],
                ),
            )
            self._event(
                connection,
                str(node["workflow_run_id"]),
                "node.claimed",
                {
                    "node_id": str(node["node_id"]),
                    "attempt": int(node["attempt"]),
                    "lease_id": lease_id,
                    "generation": generation,
                    "worker_id": worker_id,
                    "expires_at": expires_at,
                },
                node_run_id=node_run_id,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if row is None:
                raise WorkflowStoreError("lease disappeared during claim")
            return self._lease_grant(row, token)

    def claim_ready_node(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 300,
        global_active_limit: int | None = None,
        node_type_active_limit: int | None = None,
        enforce_linear_order: bool = False,
        now: datetime | None = None,
    ) -> LeaseGrant | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT node_run_id FROM node_runs WHERE workflow_run_id=? AND status=? "
                "ORDER BY display_order, attempt LIMIT 1",
                (run_id, NodeRunStatus.READY.value),
            ).fetchone()
        if row is None:
            return None
        return self.claim_node(
            str(row["node_run_id"]),
            worker_id,
            lease_seconds=lease_seconds,
            global_active_limit=global_active_limit,
            node_type_active_limit=node_type_active_limit,
            enforce_linear_order=enforce_linear_order,
            now=now,
        )

    def heartbeat(
        self,
        grant: LeaseGrant,
        *,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> LeaseGrant:
        self._validate_lease_seconds(lease_seconds)
        current = _current_time(now)
        timestamp = format_utc(current)
        expires_at = format_utc(current + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            self._require_live_lease(connection, grant, now=current)
            run = connection.execute(
                "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
                (grant.run_id,),
            ).fetchone()
            if run is None:
                raise LeaseLostError("workflow run disappeared")
            if bool(run["cancel_requested"]):
                raise WorkflowConflictError("cancelling workflow leases cannot be extended")
            updated = connection.execute(
                "UPDATE workflow_leases SET "
                "heartbeat_at=CASE WHEN heartbeat_at<? THEN ? ELSE heartbeat_at END, "
                "expires_at=CASE WHEN expires_at<? THEN ? ELSE expires_at END "
                "WHERE lease_id=? AND generation=? AND state='active'",
                (
                    timestamp,
                    timestamp,
                    expires_at,
                    expires_at,
                    grant.lease_id,
                    grant.generation,
                ),
            ).rowcount
            if updated != 1:
                raise LeaseLostError("workflow lease heartbeat lost its compare-and-swap")
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id=?",
                (grant.lease_id,),
            ).fetchone()
            if row is None:
                raise LeaseLostError("workflow lease disappeared")
            return self._lease_grant(row, grant.fencing_token)

    def release_lease(
        self,
        grant: LeaseGrant,
        *,
        node_status: NodeRunStatus = NodeRunStatus.READY,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        target = NodeRunStatus(node_status)
        if target not in {NodeRunStatus.READY, NodeRunStatus.CANCELLED}:
            raise ValueError("release_lease only supports ready or cancelled node status")
        current = _current_time(now)
        timestamp = format_utc(current)
        with self.transaction() as connection:
            lease = self._require_live_lease(connection, grant, now=current)
            run = connection.execute(
                "SELECT cancel_requested FROM workflow_runs WHERE run_id=?",
                (grant.run_id,),
            ).fetchone()
            if run is None:
                raise LeaseLostError("workflow run disappeared")
            if bool(run["cancel_requested"]):
                target = NodeRunStatus.CANCELLED
            lease_state = "cancelled" if target is NodeRunStatus.CANCELLED else "released"
            connection.execute(
                "UPDATE workflow_leases SET state=?, released_at=? "
                "WHERE lease_id=? AND generation=? AND state='active'",
                (lease_state, timestamp, grant.lease_id, grant.generation),
            )
            finished_at = timestamp if target is NodeRunStatus.CANCELLED else ""
            updated = connection.execute(
                "UPDATE node_runs SET status=?, updated_at=?, finished_at=? WHERE node_run_id=? AND status=?",
                (
                    target.value,
                    timestamp,
                    finished_at,
                    grant.node_run_id,
                    NodeRunStatus.RUNNING.value,
                ),
            ).rowcount
            if updated != 1:
                raise LeaseLostError("node status no longer belongs to this lease")
            self._event(
                connection,
                grant.run_id,
                "node.lease_released",
                {
                    "lease_id": grant.lease_id,
                    "generation": grant.generation,
                    "node_status": target.value,
                },
                node_run_id=grant.node_run_id,
                timestamp=timestamp,
            )
            if target is NodeRunStatus.CANCELLED:
                cancelled_node = connection.execute(
                    "SELECT node_id, attempt FROM node_runs WHERE node_run_id=?",
                    (grant.node_run_id,),
                ).fetchone()
                if cancelled_node is None:
                    raise WorkflowStoreError("cancelled node disappeared")
                self._event(
                    connection,
                    grant.run_id,
                    "node.cancelled",
                    {
                        "node_id": str(cancelled_node["node_id"]),
                        "attempt": int(cancelled_node["attempt"]),
                        "reason": "cancel-requested",
                    },
                    node_run_id=grant.node_run_id,
                    timestamp=timestamp,
                )
                self._finalize_cancel(connection, grant.run_id, timestamp)
            row = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (grant.node_run_id,),
            ).fetchone()
            if row is None or str(lease["workflow_run_id"]) != grant.run_id:
                raise WorkflowStoreError("lease/node binding was corrupted")
            return self._node_record(row)

    def wait_node(
        self,
        grant: LeaseGrant,
        *,
        status: NodeRunStatus,
        reason: str,
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        target = NodeRunStatus(status)
        if target not in {
            NodeRunStatus.WAITING_APPROVAL,
            NodeRunStatus.WAITING_HUMAN,
        }:
            raise ValueError("wait_node status must be waiting-approval or waiting-human")
        if not reason.strip():
            raise ValueError("waiting reason is required")
        detail_data = dict(details or {})
        canonical_json(detail_data)
        current = _current_time(now)
        timestamp = format_utc(current)
        with self.transaction() as connection:
            self._require_live_lease(connection, grant, now=current)
            run = connection.execute(
                "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
                (grant.run_id,),
            ).fetchone()
            if run is None:
                raise LeaseLostError("workflow run disappeared")
            if bool(run["cancel_requested"]):
                raise WorkflowConflictError("cancelling workflow cannot enter a waiting state")
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=? AND workflow_run_id=?",
                (grant.node_run_id, grant.run_id),
            ).fetchone()
            if node is None or str(node["status"]) != NodeRunStatus.RUNNING.value:
                raise LeaseLostError("workflow node is no longer running")
            released = connection.execute(
                "UPDATE workflow_leases SET state='released', released_at=? "
                "WHERE lease_id=? AND generation=? AND state='active'",
                (timestamp, grant.lease_id, grant.generation),
            ).rowcount
            if released != 1:
                raise LeaseLostError("waiting transition lost its lease fencing")
            updated = connection.execute(
                "UPDATE node_runs SET status=?, updated_at=? WHERE node_run_id=? AND status=?",
                (
                    target.value,
                    timestamp,
                    grant.node_run_id,
                    NodeRunStatus.RUNNING.value,
                ),
            ).rowcount
            if updated != 1:
                raise LeaseLostError("waiting transition lost its node compare-and-swap")
            run_status = (
                WorkflowRunStatus.PAUSED
                if str(run["status"]) == WorkflowRunStatus.PAUSED.value
                else (
                    WorkflowRunStatus.WAITING_APPROVAL
                    if target is NodeRunStatus.WAITING_APPROVAL
                    else WorkflowRunStatus.WAITING_HUMAN
                )
            )
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                (run_status.value, timestamp, grant.run_id),
            )
            self._event(
                connection,
                grant.run_id,
                "node.waiting",
                {
                    "node_id": str(node["node_id"]),
                    "attempt": int(node["attempt"]),
                    "status": target.value,
                    "reason": reason,
                    "details": detail_data,
                    "lease_id": grant.lease_id,
                    "generation": grant.generation,
                },
                node_run_id=grant.node_run_id,
                timestamp=timestamp,
            )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (grant.node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("waiting node disappeared")
            return self._node_record(refreshed)

    def resume_waiting_node(
        self,
        node_run_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if node is None:
                raise KeyError(node_run_id)
            if str(node["status"]) not in {
                NodeRunStatus.WAITING_APPROVAL.value,
                NodeRunStatus.WAITING_HUMAN.value,
            }:
                raise WorkflowConflictError("node is not waiting for approval or human input")
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (node["workflow_run_id"],),
            ).fetchone()
            if run is None:
                raise WorkflowStoreError("waiting node has no workflow run")
            if bool(run["cancel_requested"]) or str(run["status"]) in _TERMINAL_RUN_STATUSES:
                raise WorkflowConflictError("terminal or cancelling workflow cannot resume a node")
            updated = connection.execute(
                "UPDATE node_runs SET status=?, updated_at=? WHERE node_run_id=? AND status=?",
                (
                    NodeRunStatus.READY.value,
                    timestamp,
                    node_run_id,
                    node["status"],
                ),
            ).rowcount
            if updated != 1:
                raise WorkflowConflictError("waiting node changed concurrently")
            target_status = (
                WorkflowRunStatus.PAUSED.value
                if str(run["status"]) == WorkflowRunStatus.PAUSED.value
                else WorkflowRunStatus.QUEUED.value
            )
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                (
                    target_status,
                    timestamp,
                    node["workflow_run_id"],
                ),
            )
            self._event(
                connection,
                str(node["workflow_run_id"]),
                "node.resumed",
                {"node_id": str(node["node_id"]), "attempt": int(node["attempt"])},
                node_run_id=node_run_id,
                timestamp=timestamp,
            )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("resumed node disappeared")
            return self._node_record(refreshed)

    def cancellation_requested(self, run_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return bool(row["cancel_requested"])

    @staticmethod
    def _insert_retry_attempt(
        connection: sqlite3.Connection,
        previous: sqlite3.Row,
        *,
        timestamp: str,
        reason: str,
        respect_backoff: bool,
    ) -> sqlite3.Row:
        latest_attempt = int(
            connection.execute(
                "SELECT MAX(attempt) AS value FROM node_runs WHERE workflow_run_id=? AND node_id=?",
                (previous["workflow_run_id"], previous["node_id"]),
            ).fetchone()["value"]
        )
        if int(previous["attempt"]) != latest_attempt:
            raise WorkflowConflictError("only the latest node attempt can be retried")
        active = connection.execute(
            "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
            (previous["workflow_run_id"],),
        ).fetchone()
        if active is not None:
            raise WorkflowConflictError("cannot create a retry while the workflow has an active lease")
        definition = NodeDefinition.from_dict(json.loads(previous["node_snapshot_json"]))
        preserve_session = definition.session_policy.reuse_on_retry and not definition.session_policy.force_new_session
        session_id = ""
        previous_session_id = str(previous["session_id"])
        if preserve_session and previous_session_id:
            session = connection.execute(
                "SELECT * FROM workflow_sessions WHERE session_record_id=? AND workflow_run_id=? AND state='active'",
                (previous_session_id, previous["workflow_run_id"]),
            ).fetchone()
            configured_group = definition.session_policy.session_group
            if session is not None and (not configured_group or str(session["session_group"]) == configured_group):
                session_id = previous_session_id
        node_run_id = _new_id("node_run")
        attempt = latest_attempt + 1
        delay_seconds = 0.0
        if respect_backoff:
            policy = definition.retry_policy
            delay_seconds = policy.initial_delay_seconds * (
                policy.backoff_multiplier ** max(int(previous["attempt"]) - 1, 0)
            )
            if policy.max_delay_seconds > 0:
                delay_seconds = min(delay_seconds, policy.max_delay_seconds)
        status = NodeRunStatus.RETRY_WAIT if delay_seconds > 0 else NodeRunStatus.READY
        ready_at = format_utc(parse_utc(timestamp) + timedelta(seconds=delay_seconds)) if delay_seconds > 0 else ""
        connection.execute(
            "INSERT INTO node_runs("
            "node_run_id, workflow_run_id, node_id, display_order, attempt, status, "
            "node_snapshot_json, session_id, ready_at, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_run_id,
                previous["workflow_run_id"],
                previous["node_id"],
                previous["display_order"],
                attempt,
                status.value,
                previous["node_snapshot_json"],
                session_id,
                ready_at,
                timestamp,
                timestamp,
            ),
        )
        WorkflowStore._event(
            connection,
            str(previous["workflow_run_id"]),
            "node.retry_created",
            {
                "node_id": str(previous["node_id"]),
                "attempt": attempt,
                "previous_attempt": int(previous["attempt"]),
                "previous_node_run_id": str(previous["node_run_id"]),
                "reason": reason,
                "status": status.value,
                "ready_at": ready_at,
            },
            node_run_id=node_run_id,
            timestamp=timestamp,
        )
        row = connection.execute(
            "SELECT * FROM node_runs WHERE node_run_id=?",
            (node_run_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStoreError("retry node attempt disappeared")
        return row

    def retry_node(
        self,
        node_run_id: str,
        *,
        reason: str = "operator-requested",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("retry reason is required")
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if previous is None:
                raise KeyError(node_run_id)
            if str(previous["status"]) not in {
                NodeRunStatus.RETRYABLE_FAILURE.value,
                NodeRunStatus.FATAL_FAILURE.value,
                NodeRunStatus.BLOCKED.value,
            }:
                raise WorkflowConflictError("node attempt is not in a retryable terminal state")
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (previous["workflow_run_id"],),
            ).fetchone()
            if run is None:
                raise WorkflowStoreError("node run has no workflow run")
            if bool(run["cancel_requested"]) or str(run["status"]) == WorkflowRunStatus.CANCELLED.value:
                raise WorkflowConflictError("cancelled workflow cannot be retried")
            created = self._insert_retry_attempt(
                connection,
                previous,
                timestamp=timestamp,
                reason=reason,
                respect_backoff=False,
            )
            target_status = (
                WorkflowRunStatus.PAUSED.value
                if str(run["status"]) == WorkflowRunStatus.PAUSED.value
                else WorkflowRunStatus.QUEUED.value
            )
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, finished_at='' WHERE run_id=?",
                (target_status, timestamp, previous["workflow_run_id"]),
            )
            return self._node_record(created)

    def schedule_retry(
        self,
        node_run_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if previous is None:
                raise KeyError(node_run_id)
            if str(previous["status"]) != NodeRunStatus.RETRYABLE_FAILURE.value:
                raise WorkflowConflictError("only a retryable failure can be scheduled")
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (previous["workflow_run_id"],),
            ).fetchone()
            if run is None:
                raise WorkflowStoreError("node run has no workflow run")
            if bool(run["cancel_requested"]) or str(run["status"]) in _TERMINAL_RUN_STATUSES:
                raise WorkflowConflictError("terminal or cancelling workflow cannot retry")
            definition = NodeDefinition.from_dict(json.loads(previous["node_snapshot_json"]))
            policy = definition.retry_policy
            allowed_code = not policy.retryable_error_codes or str(previous["error_code"]) in set(
                policy.retryable_error_codes
            )
            if int(previous["attempt"]) >= policy.max_attempts or not allowed_code:
                connection.execute(
                    "UPDATE workflow_runs SET status=?, updated_at=?, finished_at=? WHERE run_id=?",
                    (
                        WorkflowRunStatus.FATAL_FAILURE.value,
                        timestamp,
                        timestamp,
                        previous["workflow_run_id"],
                    ),
                )
                self._event(
                    connection,
                    str(previous["workflow_run_id"]),
                    "run.retry_exhausted",
                    {
                        "node_id": str(previous["node_id"]),
                        "attempt": int(previous["attempt"]),
                        "error_code": str(previous["error_code"]),
                    },
                    node_run_id=node_run_id,
                    timestamp=timestamp,
                )
                return None
            created = self._insert_retry_attempt(
                connection,
                previous,
                timestamp=timestamp,
                reason="retry-policy",
                respect_backoff=True,
            )
            run_status = (
                WorkflowRunStatus.PAUSED.value
                if str(run["status"]) == WorkflowRunStatus.PAUSED.value
                else (
                    WorkflowRunStatus.RETRYABLE_FAILURE.value
                    if str(created["status"]) == NodeRunStatus.RETRY_WAIT.value
                    else WorkflowRunStatus.QUEUED.value
                )
            )
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, finished_at='' WHERE run_id=?",
                (run_status, timestamp, previous["workflow_run_id"]),
            )
            return self._node_record(created)

    def activate_due_retries(
        self,
        *,
        run_id: str = "",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = format_utc(_current_time(now))
        activated: list[dict[str, Any]] = []
        with self.transaction() as connection:
            sql = (
                "SELECT node.* FROM node_runs AS node "
                "JOIN workflow_runs AS run ON run.run_id=node.workflow_run_id "
                "WHERE node.status=? AND node.ready_at<>'' AND node.ready_at<=? "
                "AND run.cancel_requested=0 AND run.status IN (?, ?)"
            )
            parameters: list[Any] = [
                NodeRunStatus.RETRY_WAIT.value,
                timestamp,
                WorkflowRunStatus.RETRYABLE_FAILURE.value,
                WorkflowRunStatus.QUEUED.value,
            ]
            if run_id:
                sql += " AND node.workflow_run_id=?"
                parameters.append(run_id)
            sql += " ORDER BY node.ready_at, node.workflow_run_id, node.display_order"
            rows = connection.execute(sql, tuple(parameters)).fetchall()
            for row in rows:
                updated = connection.execute(
                    "UPDATE node_runs SET status=?, ready_at='', updated_at=? "
                    "WHERE node_run_id=? AND status=? AND ready_at<=?",
                    (
                        NodeRunStatus.READY.value,
                        timestamp,
                        row["node_run_id"],
                        NodeRunStatus.RETRY_WAIT.value,
                        timestamp,
                    ),
                ).rowcount
                if updated != 1:
                    continue
                connection.execute(
                    "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                    (
                        WorkflowRunStatus.QUEUED.value,
                        timestamp,
                        row["workflow_run_id"],
                    ),
                )
                self._event(
                    connection,
                    str(row["workflow_run_id"]),
                    "node.retry_ready",
                    {"node_id": str(row["node_id"]), "attempt": int(row["attempt"])},
                    node_run_id=str(row["node_run_id"]),
                    timestamp=timestamp,
                )
                refreshed = connection.execute(
                    "SELECT * FROM node_runs WHERE node_run_id=?",
                    (row["node_run_id"],),
                ).fetchone()
                if refreshed is None:
                    raise WorkflowStoreError("activated retry disappeared")
                activated.append(self._node_record(refreshed))
        return activated

    def recover_expired_leases(
        self,
        *,
        run_id: str = "",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = _current_time(now)
        timestamp = format_utc(current)
        recovered: list[dict[str, Any]] = []
        with self.transaction() as connection:
            sql = (
                "SELECT lease.*, node.*, run.cancel_requested, run.status AS run_status "
                "FROM workflow_leases AS lease "
                "JOIN node_runs AS node ON node.node_run_id=lease.node_run_id "
                "JOIN workflow_runs AS run ON run.run_id=lease.workflow_run_id "
                "WHERE lease.state='active' AND lease.expires_at<=?"
            )
            parameters: list[Any] = [timestamp]
            if run_id:
                sql += " AND lease.workflow_run_id=?"
                parameters.append(run_id)
            sql += " ORDER BY lease.expires_at, lease.lease_id"
            leases = connection.execute(sql, tuple(parameters)).fetchall()
            for joined in leases:
                expired = connection.execute(
                    "UPDATE workflow_leases SET state='expired', released_at=? "
                    "WHERE lease_id=? AND state='active' AND expires_at<=?",
                    (timestamp, joined["lease_id"], timestamp),
                ).rowcount
                if expired != 1:
                    continue
                self._event(
                    connection,
                    str(joined["workflow_run_id"]),
                    "lease.expired",
                    {
                        "lease_id": str(joined["lease_id"]),
                        "node_id": str(joined["node_id"]),
                        "attempt": int(joined["attempt"]),
                        "generation": int(joined["generation"]),
                    },
                    node_run_id=str(joined["node_run_id"]),
                    timestamp=timestamp,
                )
                node = connection.execute(
                    "SELECT * FROM node_runs WHERE node_run_id=?",
                    (joined["node_run_id"],),
                ).fetchone()
                if node is None:
                    raise WorkflowStoreError("expired lease node disappeared")
                if str(node["status"]) != NodeRunStatus.RUNNING.value:
                    raise WorkflowStoreError("active lease did not own a running node")
                if bool(joined["cancel_requested"]):
                    connection.execute(
                        "UPDATE node_runs SET status=?, error_code=?, updated_at=?, finished_at=? "
                        "WHERE node_run_id=? AND status=?",
                        (
                            NodeRunStatus.CANCELLED.value,
                            "cancel-requested",
                            timestamp,
                            timestamp,
                            node["node_run_id"],
                            NodeRunStatus.RUNNING.value,
                        ),
                    )
                    self._event(
                        connection,
                        str(node["workflow_run_id"]),
                        "node.cancelled",
                        {
                            "node_id": str(node["node_id"]),
                            "attempt": int(node["attempt"]),
                            "reason": "cancel-requested",
                        },
                        node_run_id=str(node["node_run_id"]),
                        timestamp=timestamp,
                    )
                    self._finalize_cancel(connection, str(node["workflow_run_id"]), timestamp)
                    recovered.append(
                        {
                            "run_id": str(node["workflow_run_id"]),
                            "expired_node_run_id": str(node["node_run_id"]),
                            "action": "cancelled",
                            "new_node_run_id": "",
                        }
                    )
                    continue
                definition = NodeDefinition.from_dict(json.loads(node["node_snapshot_json"]))
                retryable_codes = set(definition.retry_policy.retryable_error_codes)
                lease_expiry_allowed = not retryable_codes or "lease-expired" in retryable_codes
                has_attempt = (
                    int(node["attempt"]) < definition.retry_policy.max_attempts
                    and lease_expiry_allowed
                )
                failed_status = NodeRunStatus.RETRYABLE_FAILURE if has_attempt else NodeRunStatus.FATAL_FAILURE
                connection.execute(
                    "UPDATE node_runs SET status=?, error_code='lease-expired', "
                    "error_message='worker lease expired before completion', "
                    "updated_at=?, finished_at=? WHERE node_run_id=? AND status=?",
                    (
                        failed_status.value,
                        timestamp,
                        timestamp,
                        node["node_run_id"],
                        NodeRunStatus.RUNNING.value,
                    ),
                )
                self._event(
                    connection,
                    str(node["workflow_run_id"]),
                    "node.failed",
                    {
                        "node_id": str(node["node_id"]),
                        "attempt": int(node["attempt"]),
                        "status": failed_status.value,
                        "error_code": "lease-expired",
                        "lease_id": str(joined["lease_id"]),
                        "generation": int(joined["generation"]),
                    },
                    node_run_id=str(node["node_run_id"]),
                    timestamp=timestamp,
                )
                if has_attempt:
                    created = self._insert_retry_attempt(
                        connection,
                        node,
                        timestamp=timestamp,
                        reason="lease-expired",
                        respect_backoff=True,
                    )
                    target_status = (
                        WorkflowRunStatus.PAUSED.value
                        if str(joined["run_status"]) == WorkflowRunStatus.PAUSED.value
                        else (
                            WorkflowRunStatus.RETRYABLE_FAILURE.value
                            if str(created["status"]) == NodeRunStatus.RETRY_WAIT.value
                            else WorkflowRunStatus.QUEUED.value
                        )
                    )
                    connection.execute(
                        "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                        (target_status, timestamp, node["workflow_run_id"]),
                    )
                    new_node_run_id = str(created["node_run_id"])
                    action = "retried"
                else:
                    connection.execute(
                        "UPDATE workflow_runs SET status=?, updated_at=?, finished_at=? WHERE run_id=?",
                        (
                            WorkflowRunStatus.FATAL_FAILURE.value,
                            timestamp,
                            timestamp,
                            node["workflow_run_id"],
                        ),
                    )
                    new_node_run_id = ""
                    action = "failed"
                recovered.append(
                    {
                        "run_id": str(node["workflow_run_id"]),
                        "expired_node_run_id": str(node["node_run_id"]),
                        "action": action,
                        "new_node_run_id": new_node_run_id,
                    }
                )
        return recovered

    @staticmethod
    def _finalize_cancel(
        connection: sqlite3.Connection,
        run_id: str,
        timestamp: str,
    ) -> None:
        run = connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(run_id)
        if str(run["status"]) == WorkflowRunStatus.CANCELLED.value:
            return
        active = connection.execute(
            "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
            (run_id,),
        ).fetchone()
        if active is not None:
            raise WorkflowConflictError("cannot finalize cancellation while a lease is active")
        rows = connection.execute(
            "SELECT * FROM node_runs WHERE workflow_run_id=? AND status IN (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                NodeRunStatus.PENDING.value,
                NodeRunStatus.READY.value,
                NodeRunStatus.RUNNING.value,
                NodeRunStatus.RETRY_WAIT.value,
                NodeRunStatus.WAITING_APPROVAL.value,
                NodeRunStatus.WAITING_HUMAN.value,
            ),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE node_runs SET status=?, error_code='cancel-requested', "
                "updated_at=?, finished_at=? WHERE node_run_id=? AND status=?",
                (
                    NodeRunStatus.CANCELLED.value,
                    timestamp,
                    timestamp,
                    row["node_run_id"],
                    row["status"],
                ),
            )
            WorkflowStore._event(
                connection,
                run_id,
                "node.cancelled",
                {"node_id": str(row["node_id"]), "attempt": int(row["attempt"])},
                node_run_id=str(row["node_run_id"]),
                timestamp=timestamp,
            )
        connection.execute(
            "UPDATE workflow_runs SET status=?, cancel_requested=1, updated_at=?, finished_at=? WHERE run_id=?",
            (WorkflowRunStatus.CANCELLED.value, timestamp, timestamp, run_id),
        )
        WorkflowStore._event(
            connection,
            run_id,
            "run.cancelled",
            {},
            timestamp=timestamp,
        )

    def request_cancel(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["status"]) in _TERMINAL_RUN_STATUSES:
                return self._run_record(connection, run_id)
            if not bool(run["cancel_requested"]):
                connection.execute(
                    "UPDATE workflow_runs SET cancel_requested=1, updated_at=? WHERE run_id=?",
                    (timestamp, run_id),
                )
                self._event(
                    connection,
                    run_id,
                    "run.cancel_requested",
                    {},
                    timestamp=timestamp,
                )
            active = connection.execute(
                "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
                (run_id,),
            ).fetchone()
            if active is None:
                self._finalize_cancel(connection, run_id, timestamp)
            return self._run_record(connection, run_id)

    def pause_run(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            current = str(run["status"])
            if current == WorkflowRunStatus.PAUSED.value:
                return self._run_record(connection, run_id)
            if current not in {
                WorkflowRunStatus.QUEUED.value,
                WorkflowRunStatus.RUNNING.value,
                WorkflowRunStatus.WAITING_APPROVAL.value,
                WorkflowRunStatus.WAITING_HUMAN.value,
            } or bool(run["cancel_requested"]):
                raise WorkflowConflictError("only an active or waiting workflow can be paused")
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                (WorkflowRunStatus.PAUSED.value, timestamp, run_id),
            )
            self._event(connection, run_id, "run.paused", {}, timestamp=timestamp)
            return self._run_record(connection, run_id)

    def resume_run(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["status"]) != WorkflowRunStatus.PAUSED.value:
                raise WorkflowConflictError("only a paused workflow can be resumed")
            if bool(run["cancel_requested"]):
                raise WorkflowConflictError("cancelling workflow cannot be resumed")
            active = connection.execute(
                "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
                (run_id,),
            ).fetchone()
            waiting = connection.execute(
                "SELECT status FROM node_runs WHERE workflow_run_id=? AND status IN (?, ?) "
                "ORDER BY display_order, attempt LIMIT 1",
                (
                    run_id,
                    NodeRunStatus.WAITING_APPROVAL.value,
                    NodeRunStatus.WAITING_HUMAN.value,
                ),
            ).fetchone()
            retry_waiting = connection.execute(
                "SELECT 1 FROM node_runs AS node JOIN ("
                "SELECT node_id, MAX(attempt) AS attempt FROM node_runs "
                "WHERE workflow_run_id=? GROUP BY node_id"
                ") AS latest ON latest.node_id=node.node_id AND latest.attempt=node.attempt "
                "WHERE node.workflow_run_id=? AND node.status IN (?, ?) LIMIT 1",
                (
                    run_id,
                    run_id,
                    NodeRunStatus.RETRY_WAIT.value,
                    NodeRunStatus.RETRYABLE_FAILURE.value,
                ),
            ).fetchone()
            if active is not None:
                target = WorkflowRunStatus.RUNNING
            elif waiting is not None:
                target = (
                    WorkflowRunStatus.WAITING_APPROVAL
                    if str(waiting["status"]) == NodeRunStatus.WAITING_APPROVAL.value
                    else WorkflowRunStatus.WAITING_HUMAN
                )
            elif retry_waiting is not None:
                target = WorkflowRunStatus.RETRYABLE_FAILURE
            else:
                target = WorkflowRunStatus.QUEUED
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                (target.value, timestamp, run_id),
            )
            self._event(
                connection,
                run_id,
                "run.resumed",
                {"status": target.value},
                timestamp=timestamp,
            )
            return self._run_record(connection, run_id)

    def complete_run(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["status"]) == WorkflowRunStatus.COMPLETED.value:
                return self._run_record(connection, run_id)
            if str(run["status"]) in _TERMINAL_RUN_STATUSES or bool(run["cancel_requested"]):
                raise WorkflowConflictError("terminal or cancelling workflow cannot complete")
            if (
                connection.execute(
                    "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
                    (run_id,),
                ).fetchone()
                is not None
            ):
                raise WorkflowConflictError("workflow cannot complete while a lease is active")
            latest = connection.execute(
                "SELECT node.status FROM node_runs AS node JOIN ("
                " SELECT node_id, MAX(attempt) AS attempt FROM node_runs "
                " WHERE workflow_run_id=? GROUP BY node_id"
                ") AS selected ON selected.node_id=node.node_id "
                "AND selected.attempt=node.attempt WHERE node.workflow_run_id=?",
                (run_id, run_id),
            ).fetchall()
            if not latest or any(
                str(row["status"]) not in {NodeRunStatus.COMPLETED.value, NodeRunStatus.SKIPPED.value} for row in latest
            ):
                raise WorkflowConflictError("workflow still has unfinished nodes")
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, finished_at=? WHERE run_id=?",
                (WorkflowRunStatus.COMPLETED.value, timestamp, timestamp, run_id),
            )
            self._event(connection, run_id, "run.completed", {}, timestamp=timestamp)
            return self._run_record(connection, run_id)

    @staticmethod
    def _complete_run_if_finished(
        connection: sqlite3.Connection,
        run_id: str,
        timestamp: str,
    ) -> bool:
        """Atomically finalize success with the last node completion."""

        run = connection.execute(
            "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise WorkflowStoreError("completed node has no workflow run")
        if bool(run["cancel_requested"]):
            raise WorkflowConflictError("cancelling workflow cannot finalize success")
        if str(run["status"]) == WorkflowRunStatus.COMPLETED.value:
            return True
        latest = connection.execute(
            "SELECT node.status FROM node_runs AS node JOIN ("
            "SELECT node_id, MAX(attempt) AS attempt FROM node_runs "
            "WHERE workflow_run_id=? GROUP BY node_id"
            ") AS selected ON selected.node_id=node.node_id "
            "AND selected.attempt=node.attempt WHERE node.workflow_run_id=?",
            (run_id, run_id),
        ).fetchall()
        if not latest or any(
            str(row["status"])
            not in {NodeRunStatus.COMPLETED.value, NodeRunStatus.SKIPPED.value}
            for row in latest
        ):
            return False
        updated = connection.execute(
            "UPDATE workflow_runs SET status=?, updated_at=?, finished_at=? "
            "WHERE run_id=? AND cancel_requested=0 AND status<>?",
            (
                WorkflowRunStatus.COMPLETED.value,
                timestamp,
                timestamp,
                run_id,
                WorkflowRunStatus.COMPLETED.value,
            ),
        ).rowcount
        if updated == 1:
            WorkflowStore._event(connection, run_id, "run.completed", {}, timestamp=timestamp)
        return True

    @staticmethod
    def _session_record(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        return {
            "session_record_id": str(row["session_record_id"]),
            "run_id": str(row["workflow_run_id"]),
            "session_group": str(row["session_group"]),
            "executor": str(row["executor"]),
            "external_session_id": str(row["external_session_id"]),
            "generation": int(row["generation"]),
            "state": str(row["state"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _bind_external_session(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        external_session_id: str,
        *,
        timestamp: str,
    ) -> sqlite3.Row:
        external_id = external_session_id.strip()
        if not external_id:
            raise ValueError("external_session_id is required")
        current = str(row["external_session_id"])
        if current:
            if current != external_id:
                raise WorkflowConflictError("workflow session is already bound")
            return row
        if str(row["state"]) != "active":
            raise WorkflowConflictError("only an active workflow session can be bound")
        run = connection.execute(
            "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
            (row["workflow_run_id"],),
        ).fetchone()
        if run is None or str(run["status"]) in _TERMINAL_RUN_STATUSES or bool(run["cancel_requested"]):
            raise WorkflowConflictError("terminal or cancelling workflow cannot bind a session")
        try:
            updated = connection.execute(
                "UPDATE workflow_sessions SET external_session_id=?, updated_at=? "
                "WHERE session_record_id=? AND generation=? AND state='active' "
                "AND external_session_id=''",
                (
                    external_id,
                    timestamp,
                    row["session_record_id"],
                    row["generation"],
                ),
            ).rowcount
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflictError("external session is already bound to another workflow") from exc
        if updated != 1:
            raise WorkflowConflictError("workflow session binding lost its compare-and-swap")
        refreshed = connection.execute(
            "SELECT * FROM workflow_sessions WHERE session_record_id=?",
            (row["session_record_id"],),
        ).fetchone()
        if refreshed is None:
            raise WorkflowStoreError("workflow session disappeared during binding")
        WorkflowStore._event(
            connection,
            str(row["workflow_run_id"]),
            "session.bound",
            {
                "session_record_id": str(row["session_record_id"]),
                "session_group": str(row["session_group"]),
                "executor": str(row["executor"]),
                "generation": int(row["generation"]),
                "external_session_sha256": hashlib.sha256(external_id.encode("utf-8")).hexdigest(),
            },
            timestamp=timestamp,
        )
        return refreshed

    def get_or_create_session(
        self,
        run_id: str,
        session_group: str,
        executor: str,
        *,
        force_new: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        group = session_group.strip()
        executor_name = executor.strip()
        if not group or not executor_name:
            raise ValueError("session_group and executor are required")
        if not isinstance(force_new, bool):
            raise TypeError("force_new must be a boolean")
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["status"]) in _TERMINAL_RUN_STATUSES or bool(run["cancel_requested"]):
                raise WorkflowConflictError("terminal or cancelling workflow cannot create a session")
            active = connection.execute(
                "SELECT * FROM workflow_sessions WHERE workflow_run_id=? "
                "AND session_group=? AND executor=? AND state='active'",
                (run_id, group, executor_name),
            ).fetchone()
            if active is not None and not force_new:
                return self._session_record(active)
            if active is not None:
                referenced = connection.execute(
                    "SELECT 1 FROM node_runs WHERE workflow_run_id=? AND session_id=? "
                    "AND status NOT IN (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        active["session_record_id"],
                        NodeRunStatus.COMPLETED.value,
                        NodeRunStatus.RETRYABLE_FAILURE.value,
                        NodeRunStatus.FATAL_FAILURE.value,
                        NodeRunStatus.BLOCKED.value,
                        NodeRunStatus.CANCELLED.value,
                        NodeRunStatus.SKIPPED.value,
                    ),
                ).fetchone()
                if referenced is not None:
                    raise WorkflowConflictError(
                        "workflow session cannot rotate while an active node still references it"
                    )
                rotated = connection.execute(
                    "UPDATE workflow_sessions SET state='rotated', updated_at=? "
                    "WHERE session_record_id=? AND generation=? AND state='active'",
                    (timestamp, active["session_record_id"], active["generation"]),
                ).rowcount
                if rotated != 1:
                    raise WorkflowConflictError("workflow session rotation lost its compare-and-swap")
                self._event(
                    connection,
                    run_id,
                    "session.rotated",
                    {
                        "session_record_id": str(active["session_record_id"]),
                        "session_group": group,
                        "executor": executor_name,
                        "generation": int(active["generation"]),
                    },
                    timestamp=timestamp,
                )
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
                    "FROM workflow_sessions WHERE workflow_run_id=? "
                    "AND session_group=? AND executor=?",
                    (run_id, group, executor_name),
                ).fetchone()["value"]
            )
            session_record_id = _new_id("session")
            try:
                connection.execute(
                    "INSERT INTO workflow_sessions("
                    "session_record_id, workflow_run_id, session_group, executor, "
                    "external_session_id, generation, state, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, '', ?, 'active', ?, ?)",
                    (
                        session_record_id,
                        run_id,
                        group,
                        executor_name,
                        generation,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkflowConflictError("workflow session was created concurrently") from exc
            created = connection.execute(
                "SELECT * FROM workflow_sessions WHERE session_record_id=?",
                (session_record_id,),
            ).fetchone()
            if created is None:
                raise WorkflowStoreError("created workflow session disappeared")
            self._event(
                connection,
                run_id,
                "session.created",
                {
                    "session_record_id": session_record_id,
                    "session_group": group,
                    "executor": executor_name,
                    "generation": generation,
                },
                timestamp=timestamp,
            )
            return self._session_record(created)

    def bind_external_session(
        self,
        grant: LeaseGrant,
        session_record_id: str,
        external_session_id: str,
        *,
        expected_generation: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation <= 0
        ):
            raise ValueError("expected_generation must be a positive integer")
        current = _current_time(now)
        timestamp = format_utc(current)
        with self.transaction() as connection:
            self._require_live_lease(connection, grant, now=current)
            row = connection.execute(
                "SELECT * FROM workflow_sessions WHERE session_record_id=?",
                (session_record_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_record_id)
            if str(row["workflow_run_id"]) != grant.run_id:
                raise WorkflowConflictError("workflow session does not belong to the leased run")
            if int(row["generation"]) != expected_generation:
                raise WorkflowConflictError("workflow session generation is stale")
            node = connection.execute(
                "SELECT status, session_id FROM node_runs WHERE node_run_id=? AND workflow_run_id=?",
                (grant.node_run_id, grant.run_id),
            ).fetchone()
            if node is None or str(node["status"]) != NodeRunStatus.RUNNING.value:
                raise LeaseLostError("workflow node is no longer running under this lease")
            if str(node["session_id"]) != session_record_id:
                raise WorkflowConflictError("leased node is not attached to this workflow session")
            bound = self._bind_external_session(
                connection,
                row,
                external_session_id,
                timestamp=timestamp,
            )
            return self._session_record(bound)

    def attach_session(
        self,
        grant: LeaseGrant,
        session_record_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _current_time(now)
        timestamp = format_utc(current)
        with self.transaction() as connection:
            self._require_live_lease(connection, grant, now=current)
            run = connection.execute(
                "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
                (grant.run_id,),
            ).fetchone()
            if run is None:
                raise LeaseLostError("workflow run disappeared")
            if bool(run["cancel_requested"]):
                raise WorkflowConflictError("cancelling workflow cannot attach a session")
            session = connection.execute(
                "SELECT * FROM workflow_sessions WHERE session_record_id=?",
                (session_record_id,),
            ).fetchone()
            if session is None or str(session["workflow_run_id"]) != grant.run_id or str(session["state"]) != "active":
                raise WorkflowConflictError("active workflow session does not belong to this run")
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=? AND workflow_run_id=?",
                (grant.node_run_id, grant.run_id),
            ).fetchone()
            if node is None or str(node["status"]) != NodeRunStatus.RUNNING.value:
                raise LeaseLostError("workflow node is no longer running")
            definition = NodeDefinition.from_dict(json.loads(node["node_snapshot_json"]))
            configured_group = definition.session_policy.session_group
            if configured_group and str(session["session_group"]) != configured_group:
                raise WorkflowConflictError("workflow session group does not match the node contract")
            existing = str(node["session_id"])
            if existing and existing != session_record_id:
                raise WorkflowConflictError("workflow node is already attached to another session")
            if not existing:
                updated = connection.execute(
                    "UPDATE node_runs SET session_id=?, updated_at=? "
                    "WHERE node_run_id=? AND status=? AND session_id=''",
                    (
                        session_record_id,
                        timestamp,
                        grant.node_run_id,
                        NodeRunStatus.RUNNING.value,
                    ),
                ).rowcount
                if updated != 1:
                    raise WorkflowConflictError("session attachment lost its compare-and-swap")
                self._event(
                    connection,
                    grant.run_id,
                    "node.session_attached",
                    {
                        "session_record_id": session_record_id,
                        "generation": int(session["generation"]),
                    },
                    node_run_id=grant.node_run_id,
                    timestamp=timestamp,
                )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (grant.node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("session-attached node disappeared")
            return self._node_record(refreshed)

    def list_sessions(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_sessions WHERE workflow_run_id=? ORDER BY session_group, executor, generation",
                (run_id,),
            ).fetchall()
        return [self._session_record(row) for row in rows]

    def get_session(self, session_record_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_sessions WHERE session_record_id=?",
                (session_record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_record_id)
        return self._session_record(row)

    @staticmethod
    def _validated_artifacts(
        node: sqlite3.Row,
        artifacts: Sequence[ArtifactRecord],
    ) -> list[dict[str, Any]]:
        definition = NodeDefinition.from_dict(json.loads(node["node_snapshot_json"]))
        specs = {item.artifact_id: item for item in definition.outputs}
        values = [artifact.to_dict() for artifact in artifacts]
        ids = [str(item["artifact_id"]) for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("node completion contains duplicate artifact IDs")
        unknown = sorted(set(ids) - set(specs))
        if unknown:
            raise ValueError(f"node completion contains undeclared artifacts: {', '.join(unknown)}")
        missing = sorted(artifact_id for artifact_id, spec in specs.items() if spec.required and artifact_id not in ids)
        if missing:
            raise ValueError(f"node completion is missing required artifacts: {', '.join(missing)}")
        for value in values:
            spec = specs[str(value["artifact_id"])]
            if value["media_type"] != spec.media_type:
                raise ValueError(f"artifact media type does not match contract: {spec.artifact_id}")
            if value["schema_ref"] != spec.schema_ref:
                raise ValueError(f"artifact schema does not match contract: {spec.artifact_id}")
        return values

    @staticmethod
    def _insert_artifacts(
        connection: sqlite3.Connection,
        node: sqlite3.Row,
        values: Sequence[Mapping[str, Any]],
        *,
        timestamp: str,
    ) -> dict[str, Mapping[str, Any]]:
        outputs = {str(item["artifact_id"]): dict(item) for item in values}
        for value in values:
            connection.execute(
                "INSERT INTO workflow_artifacts("
                "artifact_record_id, workflow_run_id, node_run_id, producer_node_id, "
                "artifact_id, uri, sha256, media_type, schema_ref, size_bytes, "
                "metadata_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id("artifact"),
                    node["workflow_run_id"],
                    node["node_run_id"],
                    node["node_id"],
                    value["artifact_id"],
                    value["uri"],
                    value["sha256"],
                    value["media_type"],
                    value["schema_ref"],
                    value["size_bytes"],
                    canonical_json(value["metadata"]),
                    timestamp,
                ),
            )
        return outputs

    def complete_waiting_node(
        self,
        node_run_id: str,
        artifacts: Sequence[ArtifactRecord] = (),
        *,
        expected_status: NodeRunStatus,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        expected = NodeRunStatus(expected_status)
        if expected not in {
            NodeRunStatus.WAITING_APPROVAL,
            NodeRunStatus.WAITING_HUMAN,
        }:
            raise ValueError("expected_status must be waiting-approval or waiting-human")
        timestamp = format_utc(_current_time(now))
        with self.transaction() as connection:
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if node is None:
                raise KeyError(node_run_id)
            values = self._validated_artifacts(node, artifacts)
            outputs = {str(item["artifact_id"]): dict(item) for item in values}
            if str(node["status"]) == NodeRunStatus.COMPLETED.value:
                prior = connection.execute(
                    "SELECT 1 FROM workflow_events WHERE workflow_run_id=? "
                    "AND node_run_id=? AND event_type='node.human_completed'",
                    (node["workflow_run_id"], node_run_id),
                ).fetchone()
                if prior is not None and json.loads(node["outputs_json"]) == outputs:
                    return self._node_record(node)
                raise WorkflowConflictError("node was completed by a different operation")
            if str(node["status"]) != expected.value:
                raise WorkflowConflictError("waiting node status changed concurrently")
            run = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (node["workflow_run_id"],),
            ).fetchone()
            if run is None:
                raise WorkflowStoreError("waiting node has no workflow run")
            if bool(run["cancel_requested"]) or str(run["status"]) in _TERMINAL_RUN_STATUSES:
                raise WorkflowConflictError("terminal or cancelling workflow cannot complete a waiting node")
            if (
                connection.execute(
                    "SELECT 1 FROM workflow_leases WHERE workflow_run_id=? AND state='active'",
                    (node["workflow_run_id"],),
                ).fetchone()
                is not None
            ):
                raise WorkflowConflictError("waiting node cannot complete while a lease is active")
            outputs = self._insert_artifacts(
                connection,
                node,
                values,
                timestamp=timestamp,
            )
            updated = connection.execute(
                "UPDATE node_runs SET status=?, outputs_json=?, updated_at=?, finished_at=? "
                "WHERE node_run_id=? AND status=?",
                (
                    NodeRunStatus.COMPLETED.value,
                    canonical_json(outputs),
                    timestamp,
                    timestamp,
                    node_run_id,
                    expected.value,
                ),
            ).rowcount
            if updated != 1:
                raise WorkflowConflictError("waiting completion lost its compare-and-swap")
            target_status = (
                WorkflowRunStatus.PAUSED.value
                if str(run["status"]) == WorkflowRunStatus.PAUSED.value
                else WorkflowRunStatus.QUEUED.value
            )
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=? WHERE run_id=?",
                (
                    target_status,
                    timestamp,
                    node["workflow_run_id"],
                ),
            )
            self._event(
                connection,
                str(node["workflow_run_id"]),
                "node.human_completed",
                {
                    "node_id": str(node["node_id"]),
                    "attempt": int(node["attempt"]),
                    "previous_status": expected.value,
                    "artifact_ids": sorted(outputs),
                },
                node_run_id=node_run_id,
                timestamp=timestamp,
            )
            self._complete_run_if_finished(
                connection,
                str(node["workflow_run_id"]),
                timestamp,
            )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("human-completed node disappeared")
            return self._node_record(refreshed)

    def complete_node(
        self,
        grant: LeaseGrant,
        artifacts: Sequence[ArtifactRecord] = (),
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _current_time(now)
        timestamp = format_utc(current)
        with self.transaction() as connection:
            self._require_live_lease(connection, grant, now=current)
            run = connection.execute(
                "SELECT cancel_requested FROM workflow_runs WHERE run_id=?",
                (grant.run_id,),
            ).fetchone()
            if run is None:
                raise LeaseLostError("workflow run disappeared")
            if bool(run["cancel_requested"]):
                raise WorkflowConflictError("workflow cancellation must be acknowledged before completion")
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=? AND workflow_run_id=?",
                (grant.node_run_id, grant.run_id),
            ).fetchone()
            if node is None or str(node["status"]) != NodeRunStatus.RUNNING.value:
                raise LeaseLostError("node is no longer running under this lease")
            values = self._validated_artifacts(node, artifacts)
            outputs = self._insert_artifacts(
                connection,
                node,
                values,
                timestamp=timestamp,
            )
            updated = connection.execute(
                "UPDATE node_runs SET status=?, outputs_json=?, updated_at=?, finished_at=? "
                "WHERE node_run_id=? AND workflow_run_id=? AND status=?",
                (
                    NodeRunStatus.COMPLETED.value,
                    canonical_json(outputs),
                    timestamp,
                    timestamp,
                    grant.node_run_id,
                    grant.run_id,
                    NodeRunStatus.RUNNING.value,
                ),
            ).rowcount
            if updated != 1:
                raise LeaseLostError("node completion lost its compare-and-swap")
            released = connection.execute(
                "UPDATE workflow_leases SET state='completed', released_at=? "
                "WHERE lease_id=? AND generation=? AND state='active'",
                (timestamp, grant.lease_id, grant.generation),
            ).rowcount
            if released != 1:
                raise LeaseLostError("node completion lost its lease fencing")
            self._event(
                connection,
                grant.run_id,
                "node.completed",
                {
                    "node_id": str(node["node_id"]),
                    "attempt": int(node["attempt"]),
                    "artifact_ids": sorted(outputs),
                    "lease_id": grant.lease_id,
                    "generation": grant.generation,
                },
                node_run_id=grant.node_run_id,
                timestamp=timestamp,
            )
            self._complete_run_if_finished(
                connection,
                str(node["workflow_run_id"]),
                timestamp,
            )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (grant.node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("completed node disappeared")
            return self._node_record(refreshed)

    def fail_node(
        self,
        grant: LeaseGrant,
        *,
        status: NodeRunStatus,
        error_code: str,
        error_message: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        target = NodeRunStatus(status)
        allowed = {
            NodeRunStatus.RETRYABLE_FAILURE,
            NodeRunStatus.FATAL_FAILURE,
            NodeRunStatus.BLOCKED,
        }
        if target not in allowed:
            raise ValueError("fail_node status must be retryable-failure, fatal-failure, or blocked")
        if not error_code.strip():
            raise ValueError("error_code is required")
        current = _current_time(now)
        timestamp = format_utc(current)
        with self.transaction() as connection:
            self._require_live_lease(connection, grant, now=current)
            run = connection.execute(
                "SELECT status, cancel_requested FROM workflow_runs WHERE run_id=?",
                (grant.run_id,),
            ).fetchone()
            if run is None:
                raise LeaseLostError("workflow run disappeared")
            if bool(run["cancel_requested"]):
                raise WorkflowConflictError("workflow cancellation must be acknowledged before failure")
            node = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=? AND workflow_run_id=?",
                (grant.node_run_id, grant.run_id),
            ).fetchone()
            if node is None:
                raise LeaseLostError("node no longer belongs to this workflow run")
            updated = connection.execute(
                "UPDATE node_runs SET status=?, error_code=?, error_message=?, "
                "updated_at=?, finished_at=? WHERE node_run_id=? AND status=?",
                (
                    target.value,
                    error_code,
                    error_message,
                    timestamp,
                    timestamp,
                    grant.node_run_id,
                    NodeRunStatus.RUNNING.value,
                ),
            ).rowcount
            if updated != 1:
                raise LeaseLostError("node failure lost its compare-and-swap")
            released = connection.execute(
                "UPDATE workflow_leases SET state='failed', released_at=? "
                "WHERE lease_id=? AND generation=? AND state='active'",
                (timestamp, grant.lease_id, grant.generation),
            ).rowcount
            if released != 1:
                raise LeaseLostError("node failure lost its lease fencing")
            run_status = {
                NodeRunStatus.RETRYABLE_FAILURE: WorkflowRunStatus.RETRYABLE_FAILURE,
                NodeRunStatus.FATAL_FAILURE: WorkflowRunStatus.FATAL_FAILURE,
                NodeRunStatus.BLOCKED: WorkflowRunStatus.BLOCKED,
            }[target]
            # A pause requested while this lease was executing is an operator
            # barrier. Preserve it across a retryable failure so the retry can
            # be recorded durably without becoming dispatchable before resume.
            if (
                target is NodeRunStatus.RETRYABLE_FAILURE
                and str(run["status"]) == WorkflowRunStatus.PAUSED.value
            ):
                run_status = WorkflowRunStatus.PAUSED
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, "
                "finished_at=CASE WHEN ?=? THEN ? ELSE finished_at END WHERE run_id=?",
                (
                    run_status.value,
                    timestamp,
                    run_status.value,
                    WorkflowRunStatus.FATAL_FAILURE.value,
                    timestamp,
                    grant.run_id,
                ),
            )
            self._event(
                connection,
                grant.run_id,
                "node.failed",
                {
                    "node_id": str(node["node_id"]),
                    "attempt": int(node["attempt"]),
                    "status": target.value,
                    "error_code": error_code,
                    "lease_id": grant.lease_id,
                    "generation": grant.generation,
                },
                node_run_id=grant.node_run_id,
                timestamp=timestamp,
            )
            refreshed = connection.execute(
                "SELECT * FROM node_runs WHERE node_run_id=?",
                (grant.node_run_id,),
            ).fetchone()
            if refreshed is None:
                raise WorkflowStoreError("failed node disappeared")
            return self._node_record(refreshed)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        node_run_id: str | None = None,
        timestamp: str,
    ) -> None:
        canonical = canonical_json(dict(payload))
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM workflow_events WHERE workflow_run_id=?",
                (run_id,),
            ).fetchone()["value"]
        )
        connection.execute(
            "INSERT INTO workflow_events("
            "workflow_run_id, sequence, event_type, node_run_id, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, sequence, event_type, node_run_id, canonical, timestamp),
        )
