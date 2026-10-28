"""Isolated SQLite ledger for dormant stage-routing policy and assignments."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import canonical_json
from .stage_routing import (
    AssignmentStatus,
    LegacyOperationState,
    PolicyMode,
    PreflightReport,
    RouteAssignment,
    RouteOwner,
    STAGE_ORDER,
    Stage,
    StageRoutePolicy,
    StageRouteRequest,
    StageRoutingBlockedError,
    StageRoutingConflictError,
    StageRoutingError,
    StageRoutingValidationError,
)


STAGE_ROUTE_DATABASE_NAME = "workflow-stage-routing.sqlite3"
STAGE_ROUTE_DATABASE_RELATIVE_PATH = Path("state") / "v2" / STAGE_ROUTE_DATABASE_NAME
_SCHEMA_VERSION = "1"
_VALID_TRANSITIONS = {
    PolicyMode.LEGACY: frozenset({PolicyMode.SHADOW}),
    PolicyMode.SHADOW: frozenset({PolicyMode.LEGACY, PolicyMode.CANARY}),
    PolicyMode.CANARY: frozenset({PolicyMode.ACTIVE, PolicyMode.DRAINING}),
    PolicyMode.ACTIVE: frozenset({PolicyMode.DRAINING}),
    PolicyMode.DRAINING: frozenset({PolicyMode.SHADOW, PolicyMode.LEGACY}),
}
_ASSIGNMENT_TRANSITIONS = {
    AssignmentStatus.RESERVED: frozenset(
        {AssignmentStatus.RUNNING, AssignmentStatus.CANCELLED, AssignmentStatus.BLOCKED}
    ),
    AssignmentStatus.RUNNING: frozenset(
        {
            AssignmentStatus.COMPLETED,
            AssignmentStatus.FAILED,
            AssignmentStatus.BLOCKED,
            AssignmentStatus.UNCERTAIN,
            AssignmentStatus.CANCELLED,
        }
    ),
    AssignmentStatus.UNCERTAIN: frozenset(
        {
            AssignmentStatus.COMPLETED,
            AssignmentStatus.FAILED,
            AssignmentStatus.BLOCKED,
            AssignmentStatus.CANCELLED,
        }
    ),
}
_ROUTING_TABLES = frozenset(
    {
        "stage_route_meta",
        "stage_route_policies",
        "stage_route_preflights",
        "stage_route_assignments",
        "stage_route_events",
    }
)


class StageRouteStoreError(StageRoutingError):
    pass


class StageRouteStoreBusyError(StageRouteStoreError):
    pass


class StageRouteStoreCorruptionError(StageRouteStoreError):
    pass


def _is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    primary_code = (code & 0xFF) if isinstance(code, int) else None
    busy_codes = {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }
    return primary_code in busy_codes or any(token in str(exc).casefold() for token in ("locked", "busy"))


def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute complete DDL statements without executescript's implicit commit."""

    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise RuntimeError("stage route schema contains an incomplete SQL statement")


def _raise_initialization_error(exc: sqlite3.Error) -> None:
    if isinstance(exc, sqlite3.OperationalError):
        if _is_busy(exc):
            raise StageRouteStoreBusyError("stage route database is busy") from exc
        raise StageRouteStoreError("stage route database operation failed") from exc
    raise StageRouteStoreCorruptionError("cannot initialize stage route database") from exc


def stage_route_database_path(workspace_root: str | Path) -> Path:
    root = Path(workspace_root).expanduser().absolute()
    return root / STAGE_ROUTE_DATABASE_RELATIVE_PATH


def _has_reparse_attribute(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _row_value(row: sqlite3.Row, name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError) as exc:
        raise StageRouteStoreCorruptionError(f"routing row is missing {name}") from exc


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StageRouteStoreCorruptionError("routing timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StageRouteStoreCorruptionError("routing timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


class StageRouteStore:
    """Append-audited sidecar store; construction itself performs no I/O."""

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        path = Path(database_path).expanduser()
        if not path.is_absolute():
            raise StageRoutingValidationError("stage route database path must be absolute")
        if path.name != STAGE_ROUTE_DATABASE_NAME:
            raise StageRoutingValidationError(f"stage route database must be named {STAGE_ROUTE_DATABASE_NAME}")
        if path.parent.name != "v2" or path.parent.parent.name != "state":
            raise StageRoutingValidationError("stage route database must be under state/v2")
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms <= 0:
            raise StageRoutingValidationError("busy_timeout_ms must be a positive integer")
        self.database_path = path.absolute()
        self.workspace_root = self.database_path.parent.parent.parent
        self.busy_timeout_ms = busy_timeout_ms

    def _check_location(self, *, after_create: bool) -> None:
        for path in (
            self.workspace_root,
            self.workspace_root / "state",
            self.workspace_root / "state" / "v2",
            self.database_path,
        ):
            if os.path.lexists(path) and (path.is_symlink() or _has_reparse_attribute(path)):
                raise StageRouteStoreError(f"routing path uses a symlink or reparse point: {path.name}")
        if after_create:
            expected_parent = (self.workspace_root / "state" / "v2").resolve(strict=True)
            if self.database_path.parent.resolve(strict=True) != expected_parent:
                raise StageRouteStoreError("routing database escaped the intended workspace")
            if self.database_path.exists() and not self.database_path.is_file():
                raise StageRouteStoreError("routing database path is not a regular file")

    def initialize(self) -> None:
        self._check_location(after_create=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._check_location(after_create=True)
        with self._initialization_connection() as connection:
            existing_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if existing_tables and (
                "stage_route_meta" not in existing_tables or not existing_tables.issubset(_ROUTING_TABLES)
            ):
                raise StageRouteStoreCorruptionError("refusing to claim a non-routing SQLite database")
            if "stage_route_meta" in existing_tables:
                marker = connection.execute(
                    "SELECT value FROM stage_route_meta WHERE key = 'schema_version'"
                ).fetchone()
                if marker is None or marker["value"] != _SCHEMA_VERSION:
                    raise StageRouteStoreCorruptionError("unsupported stage route database schema")
            _execute_schema_script(
                connection,
                """
                    CREATE TABLE IF NOT EXISTS stage_route_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stage_route_policies (
                        stage TEXT PRIMARY KEY CHECK(stage IN ('journal-author', 'figures', 'writing')),
                        mode TEXT NOT NULL CHECK(mode IN ('legacy', 'shadow', 'canary', 'active', 'draining')),
                        revision INTEGER NOT NULL CHECK(revision >= 0),
                        workflow_version_id TEXT NOT NULL,
                        definition_sha256 TEXT NOT NULL,
                        binding_sha256 TEXT NOT NULL,
                        output_contract_sha256 TEXT NOT NULL,
                        parity_head_sequence INTEGER NOT NULL CHECK(parity_head_sequence >= 0),
                        min_consecutive_passes INTEGER NOT NULL CHECK(min_consecutive_passes > 0),
                        min_distinct_subjects INTEGER NOT NULL CHECK(min_distinct_subjects > 0),
                        max_parity_age_seconds INTEGER NOT NULL CHECK(max_parity_age_seconds > 0),
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stage_route_preflights (
                        evidence_sha256 TEXT PRIMARY KEY,
                        stage TEXT NOT NULL CHECK(stage IN ('journal-author', 'figures', 'writing')),
                        policy_revision INTEGER NOT NULL CHECK(policy_revision >= 0),
                        eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                        expires_at TEXT NOT NULL,
                        comparison_head_sequence INTEGER NOT NULL CHECK(comparison_head_sequence >= 0),
                        report_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stage_route_assignments (
                        assignment_id TEXT PRIMARY KEY,
                        stage TEXT NOT NULL CHECK(stage IN ('journal-author', 'figures', 'writing')),
                        operation_id_sha256 TEXT NOT NULL,
                        subject_id_sha256 TEXT NOT NULL,
                        intent_sha256 TEXT NOT NULL,
                        owner TEXT NOT NULL CHECK(owner IN ('legacy', 'generic', 'blocked')),
                        policy_revision INTEGER NOT NULL CHECK(policy_revision >= 0),
                        preflight_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'reserved', 'running', 'completed', 'failed', 'blocked', 'uncertain', 'cancelled'
                        )),
                        reason_code TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(stage, operation_id_sha256)
                    );
                    CREATE TABLE IF NOT EXISTS stage_route_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        occurred_at TEXT NOT NULL,
                        stage TEXT NOT NULL CHECK(stage IN ('journal-author', 'figures', 'writing')),
                        event_type TEXT NOT NULL,
                        policy_revision INTEGER NOT NULL CHECK(policy_revision >= 0),
                        assignment_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS stage_route_assignment_identity_immutable
                    BEFORE UPDATE ON stage_route_assignments
                    WHEN NEW.assignment_id <> OLD.assignment_id
                      OR NEW.stage <> OLD.stage
                      OR NEW.operation_id_sha256 <> OLD.operation_id_sha256
                      OR NEW.subject_id_sha256 <> OLD.subject_id_sha256
                      OR NEW.intent_sha256 <> OLD.intent_sha256
                      OR NEW.owner <> OLD.owner
                      OR NEW.policy_revision <> OLD.policy_revision
                      OR NEW.preflight_sha256 <> OLD.preflight_sha256
                    BEGIN
                        SELECT RAISE(ABORT, 'route assignment identity and owner are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS stage_route_assignment_no_delete
                    BEFORE DELETE ON stage_route_assignments
                    BEGIN SELECT RAISE(ABORT, 'route assignments are durable and cannot be deleted'); END;
                    CREATE TRIGGER IF NOT EXISTS stage_route_event_append_only_update
                    BEFORE UPDATE ON stage_route_events
                    BEGIN SELECT RAISE(ABORT, 'route events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS stage_route_event_append_only_delete
                    BEFORE DELETE ON stage_route_events
                    BEGIN SELECT RAISE(ABORT, 'route events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS stage_route_preflight_immutable_update
                    BEFORE UPDATE ON stage_route_preflights
                    BEGIN SELECT RAISE(ABORT, 'preflight evidence is immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS stage_route_preflight_immutable_delete
                    BEFORE DELETE ON stage_route_preflights
                    BEGIN SELECT RAISE(ABORT, 'preflight evidence is immutable'); END;
                    """,
            )
            connection.execute(
                "INSERT OR IGNORE INTO stage_route_meta(key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            version = connection.execute("SELECT value FROM stage_route_meta WHERE key = 'schema_version'").fetchone()
            if version is None or version["value"] != _SCHEMA_VERSION:
                raise StageRouteStoreCorruptionError("unsupported stage route database schema")
            for stage in Stage:
                connection.execute(
                    """
                        INSERT OR IGNORE INTO stage_route_policies(
                            stage, mode, revision, workflow_version_id, definition_sha256,
                            binding_sha256, output_contract_sha256, parity_head_sequence,
                            min_consecutive_passes, min_distinct_subjects,
                            max_parity_age_seconds, updated_at
                        ) VALUES (?, 'legacy', 0, '', '', '', '', 0, 3, 3, 86400, '1970-01-01T00:00:00Z')
                        """,
                    (stage.value,),
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise StageRouteStoreCorruptionError("stage route database failed integrity_check")

    def _open_connection(self, *, require_existing: bool) -> sqlite3.Connection:
        self._check_location(after_create=require_existing)
        if require_existing and not self.database_path.is_file():
            raise StageRouteStoreError("stage route database is not initialized")
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _initialization_connection(self) -> Iterator[sqlite3.Connection]:
        deadline = time.monotonic() + max(5.0, self.busy_timeout_ms / 1_000)
        connection: sqlite3.Connection | None = None
        while True:
            try:
                connection = self._open_connection(require_existing=False)
                connection.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.Error as exc:
                if connection is not None:
                    connection.close()
                    connection = None
                if _is_busy(exc) and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                _raise_initialization_error(exc)
        try:
            yield connection
            while True:
                try:
                    connection.commit()
                    break
                except sqlite3.Error as exc:
                    if _is_busy(exc) and time.monotonic() < deadline:
                        time.sleep(0.02)
                        continue
                    _raise_initialization_error(exc)
        except sqlite3.Error as exc:
            _raise_initialization_error(exc)
        finally:
            if connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            connection.close()
        self._enable_wal(deadline)

    def _enable_wal(self, deadline: float) -> None:
        while True:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open_connection(require_existing=True)
                row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is None or str(row[0]).casefold() != "wal":
                    raise sqlite3.OperationalError("WAL mode was not accepted")
                return
            except sqlite3.OperationalError as exc:
                if _is_busy(exc) and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                if _is_busy(exc):
                    raise StageRouteStoreBusyError("stage route database is busy") from exc
                raise StageRouteStoreError("stage route database operation failed") from exc
            except sqlite3.DatabaseError as exc:
                raise StageRouteStoreCorruptionError("stage route database is corrupt") from exc
            finally:
                if connection is not None:
                    connection.close()

    @contextmanager
    def _connect(self, *, require_existing: bool = True) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection(require_existing=require_existing)
            yield connection
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise StageRouteStoreBusyError("stage route database is busy") from exc
            raise StageRouteStoreError("stage route database operation failed") from exc
        except sqlite3.DatabaseError as exc:
            raise StageRouteStoreCorruptionError("stage route database is corrupt") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _policy(row: sqlite3.Row) -> StageRoutePolicy:
        try:
            return StageRoutePolicy(
                stage=Stage(_row_value(row, "stage")),
                mode=PolicyMode(_row_value(row, "mode")),
                revision=int(_row_value(row, "revision")),
                workflow_version_id=str(_row_value(row, "workflow_version_id")),
                definition_sha256=str(_row_value(row, "definition_sha256")),
                binding_sha256=str(_row_value(row, "binding_sha256")),
                output_contract_sha256=str(_row_value(row, "output_contract_sha256")),
                parity_head_sequence=int(_row_value(row, "parity_head_sequence")),
                min_consecutive_passes=int(_row_value(row, "min_consecutive_passes")),
                min_distinct_subjects=int(_row_value(row, "min_distinct_subjects")),
                max_parity_age_seconds=int(_row_value(row, "max_parity_age_seconds")),
                updated_at=str(_row_value(row, "updated_at")),
            )
        except (TypeError, ValueError, StageRoutingValidationError) as exc:
            raise StageRouteStoreCorruptionError("invalid persisted stage route policy") from exc

    @staticmethod
    def _assignment(row: sqlite3.Row) -> RouteAssignment:
        try:
            return RouteAssignment(
                assignment_id=str(_row_value(row, "assignment_id")),
                stage=Stage(_row_value(row, "stage")),
                operation_id_sha256=str(_row_value(row, "operation_id_sha256")),
                subject_id_sha256=str(_row_value(row, "subject_id_sha256")),
                intent_sha256=str(_row_value(row, "intent_sha256")),
                owner=RouteOwner(_row_value(row, "owner")),
                policy_revision=int(_row_value(row, "policy_revision")),
                preflight_sha256=str(_row_value(row, "preflight_sha256")),
                status=AssignmentStatus(_row_value(row, "status")),
                reason_code=str(_row_value(row, "reason_code")),
                created_at=str(_row_value(row, "created_at")),
                updated_at=str(_row_value(row, "updated_at")),
            )
        except (TypeError, ValueError, StageRoutingValidationError) as exc:
            raise StageRouteStoreCorruptionError("invalid persisted route assignment") from exc

    def get_policy(self, stage: Stage | str) -> StageRoutePolicy:
        normalized = Stage(stage)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM stage_route_policies WHERE stage = ?", (normalized.value,)
            ).fetchone()
        if row is None:
            raise StageRouteStoreCorruptionError("stage route policy disappeared")
        return self._policy(row)

    def list_policies(self) -> tuple[StageRoutePolicy, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM stage_route_policies").fetchall()
        policies = {row["stage"]: self._policy(row) for row in rows}
        if set(policies) != set(STAGE_ORDER):
            raise StageRouteStoreCorruptionError("stage route policy set is incomplete")
        return tuple(policies[stage] for stage in STAGE_ORDER)

    def record_preflight(self, report: PreflightReport) -> PreflightReport:
        serialized = canonical_json(report.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT report_json FROM stage_route_preflights WHERE evidence_sha256 = ?",
                (report.evidence_sha256,),
            ).fetchone()
            if existing is not None:
                if existing["report_json"] != serialized:
                    connection.rollback()
                    raise StageRouteStoreCorruptionError("preflight digest collision or changed evidence")
                connection.commit()
                return report
            connection.execute(
                """
                INSERT INTO stage_route_preflights(
                    evidence_sha256, stage, policy_revision, eligible, expires_at,
                    comparison_head_sequence, report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.evidence_sha256,
                    report.stage.value,
                    report.policy_revision,
                    int(report.eligible),
                    report.expires_at,
                    report.comparison_head_sequence,
                    serialized,
                    report.created_at,
                ),
            )
            self._event(
                connection,
                occurred_at=report.created_at,
                stage=report.stage,
                event_type="preflight-recorded",
                policy_revision=report.policy_revision,
                assignment_id="",
                payload={"evidence_sha256": report.evidence_sha256, "eligible": report.eligible},
            )
            connection.commit()
        return report

    def get_preflight(self, evidence_sha256: str) -> PreflightReport | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM stage_route_preflights WHERE evidence_sha256 = ?",
                (str(evidence_sha256).lower(),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["report_json"])
            if not isinstance(payload, dict):
                raise TypeError("preflight JSON must be an object")
            return PreflightReport.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError, StageRoutingValidationError) as exc:
            raise StageRouteStoreCorruptionError("persisted preflight evidence is invalid") from exc

    @staticmethod
    def _write_policy(connection: sqlite3.Connection, policy: StageRoutePolicy) -> None:
        cursor = connection.execute(
            """
            UPDATE stage_route_policies
               SET mode = ?, revision = ?, workflow_version_id = ?, definition_sha256 = ?,
                   binding_sha256 = ?, output_contract_sha256 = ?, parity_head_sequence = ?,
                   min_consecutive_passes = ?, min_distinct_subjects = ?,
                   max_parity_age_seconds = ?, updated_at = ?
             WHERE stage = ? AND revision = ?
            """,
            (
                policy.mode.value,
                policy.revision,
                policy.workflow_version_id,
                policy.definition_sha256,
                policy.binding_sha256,
                policy.output_contract_sha256,
                policy.parity_head_sequence,
                policy.min_consecutive_passes,
                policy.min_distinct_subjects,
                policy.max_parity_age_seconds,
                policy.updated_at,
                policy.stage.value,
                policy.revision - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise StageRoutingConflictError("stage route policy compare-and-set failed")

    def compare_and_set_policy(
        self,
        replacement: StageRoutePolicy,
        *,
        expected_revision: int,
        event_type: str,
    ) -> StageRoutePolicy:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM stage_route_policies WHERE stage = ?", (replacement.stage.value,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise StageRouteStoreCorruptionError("stage route policy disappeared")
            current = self._policy(row)
            if current.revision != expected_revision or replacement.revision != current.revision + 1:
                connection.rollback()
                raise StageRoutingConflictError("stage route policy revision is stale")
            if replacement.mode not in _VALID_TRANSITIONS[current.mode]:
                connection.rollback()
                raise StageRoutingConflictError(
                    f"invalid policy transition {current.mode.value}->{replacement.mode.value}"
                )
            if replacement.mode in {PolicyMode.CANARY, PolicyMode.ACTIVE}:
                self._assert_previous_active(connection, replacement.stage)
                if not all(replacement.candidate_identity):
                    connection.rollback()
                    raise StageRoutingBlockedError("candidate policy identity is incomplete")
            self._write_policy(connection, replacement)
            self._event(
                connection,
                occurred_at=replacement.updated_at,
                stage=replacement.stage,
                event_type=event_type,
                policy_revision=replacement.revision,
                assignment_id="",
                payload={"from": current.mode.value, "to": replacement.mode.value},
            )
            connection.commit()
        return replacement

    @staticmethod
    def _assert_previous_active(connection: sqlite3.Connection, stage: Stage) -> None:
        earlier = STAGE_ORDER[: STAGE_ORDER.index(stage.value)]
        if not earlier:
            return
        rows = connection.execute(
            f"SELECT stage, mode FROM stage_route_policies WHERE stage IN ({','.join('?' for _ in earlier)})",
            tuple(earlier),
        ).fetchall()
        modes = {row["stage"]: row["mode"] for row in rows}
        if any(modes.get(name) != PolicyMode.ACTIVE.value for name in earlier):
            raise StageRoutingBlockedError("previous stages must be ACTIVE before promotion")

    def begin_rollback(
        self,
        stage: Stage | str,
        *,
        expected_revision: int,
        now: str,
    ) -> tuple[StageRoutePolicy, ...]:
        normalized = Stage(stage)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM stage_route_policies").fetchall()
            policies = {row["stage"]: self._policy(row) for row in rows}
            current = policies[normalized.value]
            if current.revision != expected_revision:
                connection.rollback()
                raise StageRoutingConflictError("stage route policy revision is stale")
            if current.mode not in {PolicyMode.CANARY, PolicyMode.ACTIVE}:
                connection.rollback()
                raise StageRoutingConflictError("rollback requires CANARY or ACTIVE")
            affected: list[StageRoutePolicy] = []
            start = STAGE_ORDER.index(normalized.value)
            for stage_name in STAGE_ORDER[start:]:
                policy = policies[stage_name]
                if policy.mode not in {PolicyMode.CANARY, PolicyMode.ACTIVE}:
                    continue
                replacement = StageRoutePolicy(
                    stage=policy.stage,
                    mode=PolicyMode.DRAINING,
                    revision=policy.revision + 1,
                    workflow_version_id=policy.workflow_version_id,
                    definition_sha256=policy.definition_sha256,
                    binding_sha256=policy.binding_sha256,
                    output_contract_sha256=policy.output_contract_sha256,
                    parity_head_sequence=policy.parity_head_sequence,
                    min_consecutive_passes=policy.min_consecutive_passes,
                    min_distinct_subjects=policy.min_distinct_subjects,
                    max_parity_age_seconds=policy.max_parity_age_seconds,
                    updated_at=now,
                )
                self._write_policy(connection, replacement)
                self._event(
                    connection,
                    occurred_at=now,
                    stage=replacement.stage,
                    event_type="rollback-draining",
                    policy_revision=replacement.revision,
                    assignment_id="",
                    payload={"from": policy.mode.value, "to": PolicyMode.DRAINING.value},
                )
                affected.append(replacement)
            connection.commit()
        return tuple(affected)

    def count_nonterminal_generic(self, stage: Stage | str) -> int:
        normalized = Stage(stage)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                  FROM stage_route_assignments
                 WHERE stage = ? AND owner = 'generic' AND status IN ('reserved', 'running', 'uncertain')
                """,
                (normalized.value,),
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def _stored_report(connection: sqlite3.Connection, report: PreflightReport) -> bool:
        row = connection.execute(
            "SELECT report_json FROM stage_route_preflights WHERE evidence_sha256 = ?",
            (report.evidence_sha256,),
        ).fetchone()
        return row is not None and row["report_json"] == canonical_json(report.to_dict())

    @staticmethod
    def _route_new_assignment(
        policy: StageRoutePolicy,
        request: StageRouteRequest,
        report: PreflightReport | None,
        *,
        allow_candidate: bool,
        now: str,
        persisted_report: bool,
    ) -> tuple[RouteOwner, AssignmentStatus, str, str]:
        if request.legacy_operation_state is LegacyOperationState.UNKNOWN_EFFECTS:
            return RouteOwner.BLOCKED, AssignmentStatus.BLOCKED, "legacy-effects-unknown", ""
        candidate_selected = policy.mode is PolicyMode.ACTIVE or (
            policy.mode is PolicyMode.CANARY and request.canary_opt_in
        )
        if not candidate_selected:
            if request.legacy_operation_state in {LegacyOperationState.QUEUED, LegacyOperationState.RUNNING}:
                return RouteOwner.LEGACY, AssignmentStatus.BLOCKED, "legacy-operation-already-owned", ""
            return RouteOwner.LEGACY, AssignmentStatus.RESERVED, f"policy-{policy.mode.value}", ""
        if request.legacy_operation_state in {LegacyOperationState.QUEUED, LegacyOperationState.RUNNING}:
            return RouteOwner.LEGACY, AssignmentStatus.BLOCKED, "legacy-operation-already-owned", ""
        if not allow_candidate:
            return RouteOwner.BLOCKED, AssignmentStatus.BLOCKED, "candidate-process-latch-closed", ""
        if not all(policy.candidate_identity):
            return RouteOwner.BLOCKED, AssignmentStatus.BLOCKED, "candidate-policy-identity-incomplete", ""
        if report is None:
            return RouteOwner.LEGACY, AssignmentStatus.RESERVED, "candidate-preflight-missing", ""
        if not persisted_report:
            return RouteOwner.BLOCKED, AssignmentStatus.BLOCKED, "candidate-preflight-untrusted", ""
        if report.stage is not request.stage:
            return (
                RouteOwner.BLOCKED,
                AssignmentStatus.BLOCKED,
                "candidate-preflight-stage-drift",
                report.evidence_sha256,
            )
        report_identity = (
            report.candidate.workflow_version_id,
            report.candidate.definition_sha256,
            report.candidate.binding_sha256,
            report.candidate.output_contract_sha256,
        )
        if report_identity != policy.candidate_identity:
            return RouteOwner.BLOCKED, AssignmentStatus.BLOCKED, "candidate-identity-drift", report.evidence_sha256
        if (
            not report.eligible
            or report.policy_revision != policy.revision
            or report.comparison_head_sequence < policy.parity_head_sequence
            or _parse_utc(report.expires_at) <= _parse_utc(now)
        ):
            return (
                RouteOwner.LEGACY,
                AssignmentStatus.RESERVED,
                "candidate-evidence-unavailable",
                report.evidence_sha256,
            )
        return RouteOwner.GENERIC, AssignmentStatus.RESERVED, "candidate-eligible", report.evidence_sha256

    def admit(
        self,
        request: StageRouteRequest,
        *,
        report: PreflightReport | None,
        allow_candidate: bool,
        now: str,
    ) -> RouteAssignment:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM stage_route_assignments WHERE stage = ? AND operation_id_sha256 = ?",
                (request.stage.value, request.operation_id_sha256),
            ).fetchone()
            if existing is not None:
                assignment = self._assignment(existing)
                if (
                    assignment.subject_id_sha256 != request.subject_id_sha256
                    or assignment.intent_sha256 != request.intent_sha256
                ):
                    connection.rollback()
                    raise StageRoutingConflictError("operation ID was reused with different subject or intent")
                connection.commit()
                return assignment
            row = connection.execute(
                "SELECT * FROM stage_route_policies WHERE stage = ?", (request.stage.value,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise StageRouteStoreCorruptionError("stage route policy disappeared")
            policy = self._policy(row)
            persisted_report = report is not None and self._stored_report(connection, report)
            owner, status, reason, preflight_sha256 = self._route_new_assignment(
                policy,
                request,
                report,
                allow_candidate=allow_candidate,
                now=now,
                persisted_report=persisted_report,
            )
            assignment = RouteAssignment(
                assignment_id=f"stage-route-{uuid.uuid4().hex}",
                stage=request.stage,
                operation_id_sha256=request.operation_id_sha256,
                subject_id_sha256=request.subject_id_sha256,
                intent_sha256=request.intent_sha256,
                owner=owner,
                policy_revision=policy.revision,
                preflight_sha256=preflight_sha256,
                status=status,
                reason_code=reason,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO stage_route_assignments(
                    assignment_id, stage, operation_id_sha256, subject_id_sha256,
                    intent_sha256, owner, policy_revision, preflight_sha256,
                    status, reason_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment.assignment_id,
                    assignment.stage.value,
                    assignment.operation_id_sha256,
                    assignment.subject_id_sha256,
                    assignment.intent_sha256,
                    assignment.owner.value,
                    assignment.policy_revision,
                    assignment.preflight_sha256,
                    assignment.status.value,
                    assignment.reason_code,
                    assignment.created_at,
                    assignment.updated_at,
                ),
            )
            self._event(
                connection,
                occurred_at=now,
                stage=request.stage,
                event_type="assignment-pinned",
                policy_revision=policy.revision,
                assignment_id=assignment.assignment_id,
                payload={"owner": owner.value, "status": status.value, "reason_code": reason},
            )
            connection.commit()
        return assignment

    def get_assignment(self, assignment_id: str) -> RouteAssignment | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM stage_route_assignments WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
        return self._assignment(row) if row is not None else None

    def list_assignments(self, *, stage: Stage | str | None = None) -> tuple[RouteAssignment, ...]:
        query = "SELECT * FROM stage_route_assignments"
        parameters: tuple[str, ...] = ()
        if stage is not None:
            query += " WHERE stage = ?"
            parameters = (Stage(stage).value,)
        query += " ORDER BY created_at, assignment_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._assignment(row) for row in rows)

    def transition_assignment(
        self,
        assignment_id: str,
        *,
        expected_status: AssignmentStatus,
        status: AssignmentStatus,
        reason_code: str,
        now: str,
    ) -> RouteAssignment:
        expected_status = AssignmentStatus(expected_status)
        status = AssignmentStatus(status)
        if status not in _ASSIGNMENT_TRANSITIONS.get(expected_status, frozenset()):
            raise StageRoutingConflictError(f"invalid assignment transition {expected_status.value}->{status.value}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM stage_route_assignments WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise StageRoutingConflictError("route assignment does not exist")
            current = self._assignment(row)
            if current.status is not expected_status:
                connection.rollback()
                raise StageRoutingConflictError("route assignment status is stale")
            cursor = connection.execute(
                """
                UPDATE stage_route_assignments
                   SET status = ?, reason_code = ?, updated_at = ?
                 WHERE assignment_id = ? AND status = ?
                """,
                (status.value, str(reason_code), now, assignment_id, expected_status.value),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StageRoutingConflictError("route assignment compare-and-set failed")
            updated_row = connection.execute(
                "SELECT * FROM stage_route_assignments WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
            updated = self._assignment(updated_row)
            self._event(
                connection,
                occurred_at=now,
                stage=updated.stage,
                event_type="assignment-status",
                policy_revision=updated.policy_revision,
                assignment_id=updated.assignment_id,
                payload={"from": current.status.value, "to": updated.status.value, "reason_code": reason_code},
            )
            connection.commit()
        return updated

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        occurred_at: str,
        stage: Stage,
        event_type: str,
        policy_revision: int,
        assignment_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO stage_route_events(
                occurred_at, stage, event_type, policy_revision, assignment_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                occurred_at,
                stage.value,
                event_type,
                policy_revision,
                assignment_id,
                canonical_json(payload),
            ),
        )

    def summary(self) -> dict[str, Any]:
        policies = self.list_policies()
        with self._connect() as connection:
            counts = connection.execute(
                "SELECT owner, status, COUNT(*) AS count FROM stage_route_assignments GROUP BY owner, status"
            ).fetchall()
            head = connection.execute("SELECT COALESCE(MAX(sequence), 0) AS head FROM stage_route_events").fetchone()
        return {
            "database": str(self.database_path),
            "policies": [policy.to_dict() for policy in policies],
            "assignment_counts": {f"{row['owner']}:{row['status']}": int(row["count"]) for row in counts},
            "event_head_sequence": int(head["head"]),
        }


__all__ = [
    "STAGE_ROUTE_DATABASE_NAME",
    "STAGE_ROUTE_DATABASE_RELATIVE_PATH",
    "StageRouteStore",
    "StageRouteStoreBusyError",
    "StageRouteStoreCorruptionError",
    "StageRouteStoreError",
    "stage_route_database_path",
]
