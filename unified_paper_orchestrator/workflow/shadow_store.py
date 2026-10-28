from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .contracts import canonical_json, is_sha256


SHADOW_STORE_SCHEMA_VERSION = "1"
SHADOW_WORKFLOW_MARKER = "shadow-only/1"
SHADOW_OBSERVATION_SCHEMA = "unified-paper-orchestrator/workflow-shadow-observation/v1"
SHADOW_COMPARISON_SCHEMA = "unified-paper-orchestrator/workflow-shadow-comparison/v1"

_CAPTURE_STATUSES = frozenset({"coherent", "unstable", "source-error"})
_COMPARISON_VERDICTS = frozenset({"exact", "equivalent", "unknown", "mismatch"})
_RUNTIME_TABLES = frozenset(
    {
        "workflow_versions",
        "workflow_runtime_bindings",
        "workflow_runs",
        "node_runs",
        "workflow_events",
        "workflow_sessions",
        "workflow_artifacts",
        "workflow_leases",
    }
)
_SHADOW_TABLES = frozenset(
    {
        "workflow_schema_meta",
        "shadow_schema_meta",
        "shadow_observations",
        "shadow_comparisons",
        "sqlite_sequence",
    }
)
_OBSERVATION_COLUMNS = frozenset(
    {
        "sequence",
        "observation_id",
        "stage",
        "subject_id_sha256",
        "source_revision_sha256",
        "projector_version",
        "capture_status",
        "payload_sha256",
        "payload_json",
        "first_observed_at",
        "recorded_at",
    }
)
_COMPARISON_COLUMNS = frozenset(
    {
        "sequence",
        "comparison_id",
        "observation_id",
        "definition_sha256",
        "binding_sha256",
        "comparator_version",
        "overall_verdict",
        "payload_sha256",
        "payload_json",
        "first_compared_at",
        "recorded_at",
    }
)

_MAX_PAYLOAD_BYTES = 512 * 1024
_MAX_JSON_DEPTH = 16
_MAX_JSON_ITEMS = 10_000
_MAX_SEQUENCE_ITEMS = 2_048
_MAX_STRING_CHARS = 8_192
_MAX_DIAGNOSTIC_DETAIL = 240

_LOGICAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_KEY_NORMALIZER_RE = re.compile(r"[^a-z0-9]+")
_WINDOWS_ABSOLUTE_RE = re.compile(r"(?:^|[^a-zA-Z0-9])[a-zA-Z]:[\\/]")
_UNC_BACKSLASH_RE = re.compile(r"(?:^|[\s\"'(<>=])\\\\[^\\/\s]+[\\/]")
_UNC_SLASH_RE = re.compile(r"(?:^|[\s\"'(<>=])//[^/\s]+/")
_HOME_PATH_RE = re.compile(r"(?:^|[\s\"'(<>=])~[\\/]")
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+")
_POSIX_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'(<>=:])/(?!/)[^\s]*")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|client[_-]?secret)"
    r"\s*[:=]\s*[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_KEY_TOKEN_RE = re.compile(r"(?i)\b(?:sk|rk)-[a-z0-9_-]{12,}")
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\b")
_URL_CREDENTIAL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/@\s:]+:[^/@\s]+@")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_FORBIDDEN_EXACT_KEYS = frozenset(
    {
        "absolute_path",
        "access_token",
        "api_key",
        "authorization",
        "auth_token",
        "bearer_token",
        "body",
        "client_secret",
        "command",
        "commands",
        "content",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "cwd",
        "developer_prompt",
        "detail",
        "entity_id",
        "environment",
        "external_session_id",
        "fencing_token",
        "final_response",
        "instructions",
        "message",
        "model_output",
        "output_text",
        "paper_id",
        "paper_key",
        "passwd",
        "password",
        "path",
        "private_key",
        "prompt",
        "prompt_body",
        "prompt_content",
        "prompt_path",
        "prompt_text",
        "raw_payload",
        "raw_state",
        "refresh_token",
        "response",
        "secret",
        "session_id",
        "source_bytes",
        "source_prompt",
        "subject_id",
        "system_prompt",
        "text",
        "thread_id",
        "token",
        "url",
        "workspace_root",
    }
)
_SAFE_PROMPT_KEYS = frozenset(
    {
        "prompt_fingerprint",
        "prompt_id",
        "prompt_ref",
        "prompt_seal_source",
        "prompt_sha256",
        "prompt_version",
    }
)
_SAFE_TOKEN_KEYS = frozenset(
    {
        "cached_input_tokens",
        "hard_token_limit",
        "input_tokens",
        "model_context_window",
        "output_tokens",
        "reasoning_output_tokens",
        "token_budget",
        "token_limit",
        "token_usage",
        "total_tokens",
    }
)


def shadow_database_path(workspace_root: str | Path) -> Path:
    """Return the isolated shadow ledger path without creating anything."""

    return Path(workspace_root).expanduser().absolute() / "state" / "v2" / "workflow-shadow.sqlite3"


@dataclass(frozen=True)
class ShadowStoreDiagnostic:
    code: str
    operation: str
    detail: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "operation": self.operation,
            "detail": self.detail,
            "retryable": self.retryable,
        }


class ShadowStoreError(RuntimeError):
    def __init__(self, diagnostic: ShadowStoreDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.code}: {diagnostic.detail}")


class ShadowStoreBusyError(ShadowStoreError):
    pass


class ShadowStoreConflictError(ShadowStoreError):
    pass


class ShadowStoreCorruptionError(ShadowStoreError):
    pass


class ShadowPayloadError(ShadowStoreError):
    pass


@dataclass(frozen=True)
class ObservationWrite:
    stage: str
    subject_id: str = field(repr=False)
    source_revision_sha256: str
    projector_version: str
    capture_status: str
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ComparisonWrite:
    observation_id: str
    definition_sha256: str
    comparator_version: str
    overall_verdict: str
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    binding_sha256: str = ""


@dataclass(frozen=True)
class ShadowObservationRecord:
    sequence: int
    observation_id: str
    stage: str
    subject_id_sha256: str
    source_revision_sha256: str
    projector_version: str
    capture_status: str
    payload_sha256: str
    payload: Mapping[str, Any] = field(repr=False)
    first_observed_at: str = ""
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "observation_id": self.observation_id,
            "stage": self.stage,
            "subject_id_sha256": self.subject_id_sha256,
            "source_revision_sha256": self.source_revision_sha256,
            "projector_version": self.projector_version,
            "capture_status": self.capture_status,
            "payload_sha256": self.payload_sha256,
            "payload": _thaw_json(self.payload),
            "first_observed_at": self.first_observed_at,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class ShadowComparisonRecord:
    sequence: int
    comparison_id: str
    observation_id: str
    definition_sha256: str
    binding_sha256: str
    comparator_version: str
    overall_verdict: str
    payload_sha256: str
    payload: Mapping[str, Any] = field(repr=False)
    first_compared_at: str = ""
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "comparison_id": self.comparison_id,
            "observation_id": self.observation_id,
            "definition_sha256": self.definition_sha256,
            "binding_sha256": self.binding_sha256,
            "comparator_version": self.comparator_version,
            "overall_verdict": self.overall_verdict,
            "payload_sha256": self.payload_sha256,
            "payload": _thaw_json(self.payload),
            "first_compared_at": self.first_compared_at,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class ShadowWriteOutcome:
    record: ShadowObservationRecord | ShadowComparisonRecord | None = None
    diagnostic: ShadowStoreDiagnostic | None = None

    @property
    def ok(self) -> bool:
        return self.record is not None and self.diagnostic is None


def _diagnostic(
    code: str,
    operation: str,
    detail: str,
    *,
    retryable: bool = False,
) -> ShadowStoreDiagnostic:
    clean = " ".join(str(detail or "").split())[:_MAX_DIAGNOSTIC_DETAIL]
    return ShadowStoreDiagnostic(code=code, operation=operation, detail=clean, retryable=retryable)


def _payload_error(detail: str, *, operation: str = "validate-payload") -> ShadowPayloadError:
    return ShadowPayloadError(_diagnostic("SHADOW_PAYLOAD_UNSAFE", operation, detail))


def _normalize_key(value: str) -> str:
    return _KEY_NORMALIZER_RE.sub("_", value.casefold()).strip("_")


def _forbidden_key_reason(value: str) -> str:
    normalized = _normalize_key(value)
    if normalized in _FORBIDDEN_EXACT_KEYS:
        return "field is not permitted in the shadow ledger"
    if normalized.endswith("_path") or normalized.endswith("_url"):
        return "paths and URLs are not permitted in the shadow ledger"
    if any(token in normalized for token in ("credential", "password", "passwd", "private_key")):
        return "credential-bearing fields are not permitted in the shadow ledger"
    if "secret" in normalized:
        return "secret-bearing fields are not permitted in the shadow ledger"
    if "prompt" in normalized and normalized not in _SAFE_PROMPT_KEYS:
        return "Prompt bodies and Prompt paths are not permitted in the shadow ledger"
    if (
        normalized.endswith(("_token", "_tokens"))
        or normalized in {"authorization_token", "id_token"}
    ) and normalized not in _SAFE_TOKEN_KEYS:
        return "credential tokens are not permitted in the shadow ledger"
    if normalized.endswith("_api_key") or normalized.endswith("_client_secret"):
        return "credential-bearing fields are not permitted in the shadow ledger"
    return ""


def _unsafe_string_reason(value: str) -> str:
    if len(value) > _MAX_STRING_CHARS:
        return f"string exceeds {_MAX_STRING_CHARS} characters"
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return "string contains disallowed control characters"
    without_urls = _URL_RE.sub("", value)
    if (
        _WINDOWS_ABSOLUTE_RE.search(value)
        or _UNC_BACKSLASH_RE.search(value)
        or _UNC_SLASH_RE.search(value)
        or _HOME_PATH_RE.search(value)
        or _POSIX_ABSOLUTE_RE.search(without_urls)
        or "file://" in value.casefold()
    ):
        return "absolute or home-relative filesystem paths are not permitted"
    if (
        _SECRET_ASSIGNMENT_RE.search(value)
        or _BEARER_RE.search(value)
        or _KEY_TOKEN_RE.search(value)
        or _JWT_RE.search(value)
        or _URL_CREDENTIAL_RE.search(value)
        or _PRIVATE_KEY_RE.search(value)
    ):
        return "credential-like string is not permitted"
    return ""


def _validate_json_value(
    value: Any,
    *,
    path: str,
    depth: int,
    seen: set[int],
    item_count: list[int],
) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise _payload_error(f"{path} exceeds maximum JSON depth {_MAX_JSON_DEPTH}")
    item_count[0] += 1
    if item_count[0] > _MAX_JSON_ITEMS:
        raise _payload_error(f"payload exceeds {_MAX_JSON_ITEMS} JSON values")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _payload_error(f"{path} contains a non-finite number")
        return
    if isinstance(value, str):
        reason = _unsafe_string_reason(value)
        if reason:
            raise _payload_error(f"{path} {reason}")
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise _payload_error(f"{path} contains a circular mapping")
        seen.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str) or not key:
                    raise _payload_error(f"{path} keys must be non-empty strings")
                reason = _forbidden_key_reason(key)
                if reason:
                    raise _payload_error(f"{path}.{key} {reason}")
                _validate_json_value(
                    item,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    seen=seen,
                    item_count=item_count,
                )
        finally:
            seen.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_SEQUENCE_ITEMS:
            raise _payload_error(f"{path} exceeds {_MAX_SEQUENCE_ITEMS} sequence items")
        identity = id(value)
        if identity in seen:
            raise _payload_error(f"{path} contains a circular sequence")
        seen.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_json_value(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    seen=seen,
                    item_count=item_count,
                )
        finally:
            seen.remove(identity)
        return
    raise _payload_error(f"{path} has unsupported type {type(value).__name__}")


def _canonical_payload(value: Mapping[str, Any], *, operation: str) -> tuple[dict[str, Any], str, str]:
    if not isinstance(value, Mapping):
        raise _payload_error("payload must be a JSON object", operation=operation)
    try:
        _validate_json_value(value, path="$", depth=0, seen=set(), item_count=[0])
        serialized = canonical_json(dict(value))
    except ShadowPayloadError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _payload_error(
            f"payload cannot be canonicalized ({type(exc).__name__})",
            operation=operation,
        ) from exc
    encoded = serialized.encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise _payload_error(
            f"canonical payload exceeds {_MAX_PAYLOAD_BYTES} bytes",
            operation=operation,
        )
    plain = json.loads(serialized)
    return plain, serialized, hashlib.sha256(encoded).hexdigest()


def _bounded_text(value: Any, label: str, *, logical: bool = False, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise _payload_error(f"{label} must be a non-empty bounded string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _payload_error(f"{label} contains control characters")
    if logical and _LOGICAL_ID_RE.fullmatch(value) is None:
        raise _payload_error(f"{label} must be a lowercase logical identifier")
    reason = _unsafe_string_reason(value)
    if reason:
        raise _payload_error(f"{label} {reason}")
    return value


def _sha256(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not is_sha256(value):
        raise _payload_error(f"{label} must be a SHA-256 digest")
    return value.casefold()


def _timestamp(value: datetime | None, label: str) -> str:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise _payload_error(f"{label} must be a timezone-aware datetime")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _frozen_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def shadow_subject_fingerprint(stage: str, subject_id: str) -> str:
    """Return the public domain-separated subject identifier used by the ledger."""

    payload = f"upo-shadow-subject-v1\x00{stage}\x00{subject_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse_flag and attributes & reparse_flag)


def _unsafe_path_diagnostic(operation: str, detail: str) -> ShadowStoreError:
    return ShadowStoreError(_diagnostic("SHADOW_STORE_PATH_UNSAFE", operation, detail))


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _sqlite_detail(exc: BaseException) -> str:
    error_name = str(getattr(exc, "sqlite_errorname", "") or "SQLITE_ERROR")
    return f"SQLite {type(exc).__name__} ({error_name})"


def _is_busy(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or "locked" in str(exc).casefold()


class ShadowStore:
    """Independent, append-only diagnostic ledger for legacy shadow facts.

    Construction is side-effect free. ``initialize`` is the only operation
    allowed to create a directory or database. This class deliberately has no
    dependency on ``WorkflowStore`` and never creates runnable workflow tables.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 500,
    ) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError("shadow store path must be a filesystem path")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 0 <= busy_timeout_ms <= 60_000
        ):
            raise ValueError("busy_timeout_ms must be an integer between 0 and 60000")
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("shadow store path must be absolute")
        suffix = tuple(part.casefold() for part in self.path.parts[-3:])
        if suffix != ("state", "v2", "workflow-shadow.sqlite3"):
            raise ValueError("shadow store path must end with state/v2/workflow-shadow.sqlite3")
        self.busy_timeout_ms = busy_timeout_ms

    def _require_safe_path(self, *, operation: str, require_existing: bool) -> None:
        candidates = tuple(reversed(self.path.parents)) + (self.path,)
        for candidate in candidates:
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _unsafe_path_diagnostic(
                    operation, f"path inspection failed ({type(exc).__name__})"
                ) from exc
            if _is_reparse_or_symlink(candidate):
                raise _unsafe_path_diagnostic(operation, "symlink or reparse path is not permitted")
            if candidate == self.path:
                if not stat.S_ISREG(metadata.st_mode):
                    raise _unsafe_path_diagnostic(operation, "database target is not a regular file")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise _unsafe_path_diagnostic(operation, "database ancestor is not a directory")
        if require_existing and not self.path.is_file():
            raise ShadowStoreError(
                _diagnostic(
                    "SHADOW_STORE_NOT_INITIALIZED",
                    operation,
                    "shadow database has not been initialized",
                )
            )

    def initialize(self) -> None:
        operation = "initialize"
        self._require_safe_path(operation=operation, require_existing=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ShadowStoreError(
                _diagnostic(
                    "SHADOW_STORE_INITIALIZE_FAILED",
                    operation,
                    f"directory creation failed ({type(exc).__name__})",
                    retryable=isinstance(exc, PermissionError),
                )
            ) from exc
        self._require_safe_path(operation=operation, require_existing=False)

        deadline = time.monotonic() + max(5.0, self.busy_timeout_ms / 1000.0)
        while True:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open_connection(readonly=False, require_existing=False)
                connection.execute("BEGIN IMMEDIATE")
                self._require_isolated_schema(connection, allow_empty=True)
                self._create_schema(connection)
                self._validate_schema(connection)
                connection.commit()
                break
            except sqlite3.Error as exc:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if _is_busy(exc) and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                self._raise_sqlite(operation, exc)
            except ShadowStoreError:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()

        self._enable_wal(deadline)

    def _enable_wal(self, deadline: float) -> None:
        operation = "initialize-wal"
        while True:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open_connection(readonly=False, require_existing=True)
                mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                if mode != "wal":
                    raise sqlite3.OperationalError("WAL mode was not accepted")
                return
            except sqlite3.Error as exc:
                if _is_busy(exc) and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                self._raise_sqlite(operation, exc)
            finally:
                if connection is not None:
                    connection.close()

    def _open_connection(self, *, readonly: bool, require_existing: bool) -> sqlite3.Connection:
        self._require_safe_path(operation="connect", require_existing=require_existing)
        timeout = self.busy_timeout_ms / 1000.0
        if readonly:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
        else:
            connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        return connection

    @contextmanager
    def _read_connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection(readonly=True, require_existing=True)
            self._require_isolated_schema(connection, allow_empty=False)
            self._validate_schema(connection)
            yield connection
        except ShadowStoreError:
            raise
        except sqlite3.Error as exc:
            self._raise_sqlite(operation, exc)
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _write_connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection(readonly=False, require_existing=True)
            connection.execute("BEGIN IMMEDIATE")
            self._require_isolated_schema(connection, allow_empty=False)
            self._validate_schema(connection)
            yield connection
            connection.commit()
        except ShadowStoreError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            self._raise_sqlite(operation, exc)
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS workflow_schema_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT OR IGNORE INTO workflow_schema_meta(key, value) "
            "VALUES ('schema_version', 'shadow-only/1')",
            "CREATE TABLE IF NOT EXISTS shadow_schema_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT OR IGNORE INTO shadow_schema_meta(key, value) "
            "VALUES ('schema_version', '1')",
            "CREATE TABLE IF NOT EXISTS shadow_observations ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
            "observation_id TEXT NOT NULL UNIQUE,"
            "stage TEXT NOT NULL,"
            "subject_id_sha256 TEXT NOT NULL CHECK(length(subject_id_sha256)=64),"
            "source_revision_sha256 TEXT NOT NULL CHECK(length(source_revision_sha256)=64),"
            "projector_version TEXT NOT NULL,"
            "capture_status TEXT NOT NULL CHECK(capture_status IN ('coherent','unstable','source-error')),"
            "payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),"
            "payload_json TEXT NOT NULL,"
            "first_observed_at TEXT NOT NULL,"
            "recorded_at TEXT NOT NULL,"
            "UNIQUE(stage, subject_id_sha256, source_revision_sha256, projector_version))",
            "CREATE INDEX IF NOT EXISTS idx_shadow_observations_subject "
            "ON shadow_observations(stage, subject_id_sha256, sequence)",
            "CREATE TABLE IF NOT EXISTS shadow_comparisons ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
            "comparison_id TEXT NOT NULL UNIQUE,"
            "observation_id TEXT NOT NULL,"
            "definition_sha256 TEXT NOT NULL CHECK(length(definition_sha256)=64),"
            "binding_sha256 TEXT NOT NULL DEFAULT '' "
            "CHECK(binding_sha256='' OR length(binding_sha256)=64),"
            "comparator_version TEXT NOT NULL,"
            "overall_verdict TEXT NOT NULL "
            "CHECK(overall_verdict IN ('exact','equivalent','unknown','mismatch')),"
            "payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),"
            "payload_json TEXT NOT NULL,"
            "first_compared_at TEXT NOT NULL,"
            "recorded_at TEXT NOT NULL,"
            "FOREIGN KEY(observation_id) REFERENCES shadow_observations(observation_id) ON DELETE RESTRICT,"
            "UNIQUE(observation_id, definition_sha256, binding_sha256, comparator_version))",
            "CREATE INDEX IF NOT EXISTS idx_shadow_comparisons_observation "
            "ON shadow_comparisons(observation_id, sequence)",
            "CREATE TRIGGER IF NOT EXISTS shadow_observations_no_update "
            "BEFORE UPDATE ON shadow_observations "
            "BEGIN SELECT RAISE(ABORT, 'shadow observations are append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS shadow_observations_no_delete "
            "BEFORE DELETE ON shadow_observations "
            "BEGIN SELECT RAISE(ABORT, 'shadow observations are append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS shadow_comparisons_no_update "
            "BEFORE UPDATE ON shadow_comparisons "
            "BEGIN SELECT RAISE(ABORT, 'shadow comparisons are append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS shadow_comparisons_no_delete "
            "BEFORE DELETE ON shadow_comparisons "
            "BEGIN SELECT RAISE(ABORT, 'shadow comparisons are append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS shadow_workflow_marker_no_update "
            "BEFORE UPDATE ON workflow_schema_meta "
            "BEGIN SELECT RAISE(ABORT, 'shadow schema marker is immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS shadow_workflow_marker_no_delete "
            "BEFORE DELETE ON workflow_schema_meta "
            "BEGIN SELECT RAISE(ABORT, 'shadow schema marker is immutable'); END",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def _require_isolated_schema(self, connection: sqlite3.Connection, *, allow_empty: bool) -> None:
        tables = self._table_names(connection)
        unexpected = tables - _SHADOW_TABLES
        if unexpected or tables.intersection(_RUNTIME_TABLES):
            raise ShadowStoreError(
                _diagnostic(
                    "SHADOW_STORE_NOT_ISOLATED",
                    "validate-schema",
                    "database contains non-shadow tables",
                )
            )
        if not tables:
            if allow_empty:
                return
            raise ShadowStoreError(
                _diagnostic(
                    "SHADOW_STORE_NOT_INITIALIZED",
                    "validate-schema",
                    "shadow database schema is missing",
                )
            )
        if "workflow_schema_meta" not in tables:
            raise ShadowStoreError(
                _diagnostic(
                    "SHADOW_STORE_NOT_ISOLATED",
                    "validate-schema",
                    "non-empty database lacks the shadow isolation marker",
                )
            )
        marker = connection.execute(
            "SELECT value FROM workflow_schema_meta WHERE key='schema_version'"
        ).fetchone()
        if marker is None or str(marker["value"]) != SHADOW_WORKFLOW_MARKER:
            raise ShadowStoreError(
                _diagnostic(
                    "SHADOW_STORE_NOT_ISOLATED",
                    "validate-schema",
                    "database is not marked as a shadow-only ledger",
                )
            )
        if "shadow_schema_meta" in tables:
            schema = connection.execute(
                "SELECT value FROM shadow_schema_meta WHERE key='schema_version'"
            ).fetchone()
            if schema is None or str(schema["value"]) != SHADOW_STORE_SCHEMA_VERSION:
                raise ShadowStoreError(
                    _diagnostic(
                        "SHADOW_STORE_SCHEMA_UNSUPPORTED",
                        "validate-schema",
                        "unsupported shadow database schema",
                    )
                )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "workflow_schema_meta",
            "shadow_schema_meta",
            "shadow_observations",
            "shadow_comparisons",
        }
        if not required.issubset(tables):
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_SCHEMA_CORRUPT",
                    "validate-schema",
                    "required shadow tables are missing",
                )
            )
        observation_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(shadow_observations)").fetchall()
        }
        comparison_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(shadow_comparisons)").fetchall()
        }
        if observation_columns != _OBSERVATION_COLUMNS or comparison_columns != _COMPARISON_COLUMNS:
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_SCHEMA_CORRUPT",
                    "validate-schema",
                    "shadow table columns do not match schema version 1",
                )
            )

    @staticmethod
    def _raise_sqlite(operation: str, exc: sqlite3.Error) -> None:
        if _is_busy(exc):
            raise ShadowStoreBusyError(
                _diagnostic(
                    "SHADOW_STORE_BUSY",
                    operation,
                    _sqlite_detail(exc),
                    retryable=True,
                )
            ) from exc
        raise ShadowStoreError(
            _diagnostic(
                "SHADOW_STORE_DATABASE_ERROR",
                operation,
                _sqlite_detail(exc),
                retryable=isinstance(exc, sqlite3.OperationalError),
            )
        ) from exc

    def record_observation(
        self,
        request: ObservationWrite,
        *,
        observed_at: datetime | None = None,
        recorded_at: datetime | None = None,
    ) -> ShadowObservationRecord:
        if not isinstance(request, ObservationWrite):
            raise _payload_error("record_observation requires ObservationWrite")
        stage = _bounded_text(request.stage, "stage", logical=True, maximum=128)
        subject_id = _bounded_text(request.subject_id, "subject_id", maximum=512)
        source_revision = _sha256(request.source_revision_sha256, "source_revision_sha256")
        projector_version = _bounded_text(
            request.projector_version,
            "projector_version",
            logical=True,
            maximum=128,
        )
        capture_status = str(request.capture_status or "")
        if capture_status not in _CAPTURE_STATUSES:
            raise _payload_error("capture_status is unsupported")
        payload, payload_json, payload_sha256 = _canonical_payload(
            request.payload,
            operation="record-observation",
        )
        subject_sha256 = shadow_subject_fingerprint(stage, subject_id)
        observation_id = _stable_id(
            "obs",
            {
                "schema": SHADOW_OBSERVATION_SCHEMA,
                "stage": stage,
                "subject_id_sha256": subject_sha256,
                "source_revision_sha256": source_revision,
                "projector_version": projector_version,
                "capture_status": capture_status,
                "payload_sha256": payload_sha256,
            },
        )
        first_observed_at = _timestamp(observed_at, "observed_at")
        persisted_at = _timestamp(recorded_at, "recorded_at")
        with self._write_connection("record-observation") as connection:
            existing = connection.execute(
                "SELECT * FROM shadow_observations WHERE "
                "stage=? AND subject_id_sha256=? AND source_revision_sha256=? AND projector_version=?",
                (stage, subject_sha256, source_revision, projector_version),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["observation_id"]) != observation_id
                    or str(existing["capture_status"]) != capture_status
                    or str(existing["payload_sha256"]) != payload_sha256
                    or str(existing["payload_json"]) != payload_json
                ):
                    raise ShadowStoreConflictError(
                        _diagnostic(
                            "SHADOW_OBSERVATION_CONFLICT",
                            "record-observation",
                            "same source revision produced different normalized facts",
                        )
                    )
                return self._observation_record(existing)
            connection.execute(
                "INSERT INTO shadow_observations("
                "observation_id, stage, subject_id_sha256, source_revision_sha256, "
                "projector_version, capture_status, payload_sha256, payload_json, "
                "first_observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    stage,
                    subject_sha256,
                    source_revision,
                    projector_version,
                    capture_status,
                    payload_sha256,
                    payload_json,
                    first_observed_at,
                    persisted_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM shadow_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if row is None:
                raise ShadowStoreCorruptionError(
                    _diagnostic(
                        "SHADOW_STORE_WRITE_LOST",
                        "record-observation",
                        "inserted observation could not be read back",
                    )
                )
            return self._observation_record(row)

    def try_record_observation(
        self,
        request: ObservationWrite,
        *,
        observed_at: datetime | None = None,
        recorded_at: datetime | None = None,
    ) -> ShadowWriteOutcome:
        try:
            return ShadowWriteOutcome(
                record=self.record_observation(
                    request,
                    observed_at=observed_at,
                    recorded_at=recorded_at,
                )
            )
        except ShadowStoreError as exc:
            return ShadowWriteOutcome(diagnostic=exc.diagnostic)

    def record_comparison(
        self,
        request: ComparisonWrite,
        *,
        compared_at: datetime | None = None,
        recorded_at: datetime | None = None,
    ) -> ShadowComparisonRecord:
        if not isinstance(request, ComparisonWrite):
            raise _payload_error("record_comparison requires ComparisonWrite")
        observation_id = _bounded_text(request.observation_id, "observation_id", maximum=80)
        if not observation_id.startswith("obs_") or not is_sha256(observation_id[4:]):
            raise _payload_error("observation_id is invalid")
        definition_sha256 = _sha256(request.definition_sha256, "definition_sha256")
        binding_sha256 = _sha256(request.binding_sha256, "binding_sha256", allow_empty=True)
        comparator_version = _bounded_text(
            request.comparator_version,
            "comparator_version",
            logical=True,
            maximum=128,
        )
        overall_verdict = str(request.overall_verdict or "")
        if overall_verdict not in _COMPARISON_VERDICTS:
            raise _payload_error("overall_verdict is unsupported")
        payload, payload_json, payload_sha256 = _canonical_payload(
            request.payload,
            operation="record-comparison",
        )
        comparison_id = _stable_id(
            "cmp",
            {
                "schema": SHADOW_COMPARISON_SCHEMA,
                "observation_id": observation_id,
                "definition_sha256": definition_sha256,
                "binding_sha256": binding_sha256,
                "comparator_version": comparator_version,
                "overall_verdict": overall_verdict,
                "payload_sha256": payload_sha256,
            },
        )
        first_compared_at = _timestamp(compared_at, "compared_at")
        persisted_at = _timestamp(recorded_at, "recorded_at")
        with self._write_connection("record-comparison") as connection:
            observation = connection.execute(
                "SELECT observation_id FROM shadow_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if observation is None:
                raise ShadowStoreConflictError(
                    _diagnostic(
                        "SHADOW_OBSERVATION_NOT_FOUND",
                        "record-comparison",
                        "comparison references an unknown observation",
                    )
                )
            existing = connection.execute(
                "SELECT * FROM shadow_comparisons WHERE "
                "observation_id=? AND definition_sha256=? AND binding_sha256=? AND comparator_version=?",
                (observation_id, definition_sha256, binding_sha256, comparator_version),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["comparison_id"]) != comparison_id
                    or str(existing["overall_verdict"]) != overall_verdict
                    or str(existing["payload_sha256"]) != payload_sha256
                    or str(existing["payload_json"]) != payload_json
                ):
                    raise ShadowStoreConflictError(
                        _diagnostic(
                            "SHADOW_COMPARISON_CONFLICT",
                            "record-comparison",
                            "same comparator identity produced different results",
                        )
                    )
                return self._comparison_record(existing)
            connection.execute(
                "INSERT INTO shadow_comparisons("
                "comparison_id, observation_id, definition_sha256, binding_sha256, "
                "comparator_version, overall_verdict, payload_sha256, payload_json, "
                "first_compared_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    comparison_id,
                    observation_id,
                    definition_sha256,
                    binding_sha256,
                    comparator_version,
                    overall_verdict,
                    payload_sha256,
                    payload_json,
                    first_compared_at,
                    persisted_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM shadow_comparisons WHERE comparison_id=?",
                (comparison_id,),
            ).fetchone()
            if row is None:
                raise ShadowStoreCorruptionError(
                    _diagnostic(
                        "SHADOW_STORE_WRITE_LOST",
                        "record-comparison",
                        "inserted comparison could not be read back",
                    )
                )
            return self._comparison_record(row)

    def try_record_comparison(
        self,
        request: ComparisonWrite,
        *,
        compared_at: datetime | None = None,
        recorded_at: datetime | None = None,
    ) -> ShadowWriteOutcome:
        try:
            return ShadowWriteOutcome(
                record=self.record_comparison(
                    request,
                    compared_at=compared_at,
                    recorded_at=recorded_at,
                )
            )
        except ShadowStoreError as exc:
            return ShadowWriteOutcome(diagnostic=exc.diagnostic)

    def get_observation(self, observation_id: str) -> ShadowObservationRecord | None:
        value = _bounded_text(observation_id, "observation_id", maximum=80)
        with self._read_connection("get-observation") as connection:
            row = connection.execute(
                "SELECT * FROM shadow_observations WHERE observation_id=?",
                (value,),
            ).fetchone()
        return self._observation_record(row) if row is not None else None

    def get_comparison(self, comparison_id: str) -> ShadowComparisonRecord | None:
        value = _bounded_text(comparison_id, "comparison_id", maximum=80)
        with self._read_connection("get-comparison") as connection:
            row = connection.execute(
                "SELECT * FROM shadow_comparisons WHERE comparison_id=?",
                (value,),
            ).fetchone()
        return self._comparison_record(row) if row is not None else None

    def list_observations(
        self,
        *,
        stage: str | None = None,
        subject_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ShadowObservationRecord]:
        sequence, maximum = self._page(after_sequence, limit)
        clauses = ["sequence>?"]
        parameters: list[Any] = [sequence]
        normalized_stage = ""
        if stage is not None:
            normalized_stage = _bounded_text(stage, "stage", logical=True, maximum=128)
            clauses.append("stage=?")
            parameters.append(normalized_stage)
        if subject_id is not None:
            if not normalized_stage:
                raise _payload_error("stage is required when filtering by subject_id")
            subject = _bounded_text(subject_id, "subject_id", maximum=512)
            clauses.append("subject_id_sha256=?")
            parameters.append(shadow_subject_fingerprint(normalized_stage, subject))
        parameters.append(maximum)
        sql = (
            "SELECT * FROM shadow_observations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?"
        )
        with self._read_connection("list-observations") as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [self._observation_record(row) for row in rows]

    def list_comparisons(
        self,
        *,
        observation_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ShadowComparisonRecord]:
        sequence, maximum = self._page(after_sequence, limit)
        clauses = ["sequence>?"]
        parameters: list[Any] = [sequence]
        if observation_id is not None:
            value = _bounded_text(observation_id, "observation_id", maximum=80)
            clauses.append("observation_id=?")
            parameters.append(value)
        parameters.append(maximum)
        sql = (
            "SELECT * FROM shadow_comparisons WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?"
        )
        with self._read_connection("list-comparisons") as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [self._comparison_record(row) for row in rows]

    def summary(self) -> Mapping[str, int | str]:
        with self._read_connection("summary") as connection:
            observations = int(
                connection.execute("SELECT COUNT(*) AS value FROM shadow_observations").fetchone()["value"]
            )
            comparisons = int(
                connection.execute("SELECT COUNT(*) AS value FROM shadow_comparisons").fetchone()["value"]
            )
            last_observation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS value FROM shadow_observations"
                ).fetchone()["value"]
            )
            last_comparison = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS value FROM shadow_comparisons"
                ).fetchone()["value"]
            )
        return MappingProxyType(
            {
                "schema_version": SHADOW_STORE_SCHEMA_VERSION,
                "observations": observations,
                "comparisons": comparisons,
                "last_observation_sequence": last_observation,
                "last_comparison_sequence": last_comparison,
            }
        )

    @staticmethod
    def _page(after_sequence: int, limit: int) -> tuple[int, int]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise _payload_error("after_sequence must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise _payload_error("limit must be an integer between 1 and 10000")
        return after_sequence, limit

    @staticmethod
    def _decode_payload(row: sqlite3.Row, *, kind: str) -> tuple[Mapping[str, Any], str]:
        raw = str(row["payload_json"])
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_RECORD_CORRUPT",
                    f"read-{kind}",
                    "stored payload is not valid JSON",
                )
            ) from exc
        try:
            payload, canonical, digest = _canonical_payload(parsed, operation=f"read-{kind}")
        except ShadowPayloadError as exc:
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_RECORD_CORRUPT",
                    f"read-{kind}",
                    "stored payload violates the shadow safety contract",
                )
            ) from exc
        if canonical != raw or digest != str(row["payload_sha256"]):
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_RECORD_CORRUPT",
                    f"read-{kind}",
                    "stored payload canonical form or digest does not match",
                )
            )
        return _frozen_json(payload), digest

    @classmethod
    def _observation_record(cls, row: sqlite3.Row) -> ShadowObservationRecord:
        payload, digest = cls._decode_payload(row, kind="observation")
        expected_id = _stable_id(
            "obs",
            {
                "schema": SHADOW_OBSERVATION_SCHEMA,
                "stage": str(row["stage"]),
                "subject_id_sha256": str(row["subject_id_sha256"]),
                "source_revision_sha256": str(row["source_revision_sha256"]),
                "projector_version": str(row["projector_version"]),
                "capture_status": str(row["capture_status"]),
                "payload_sha256": digest,
            },
        )
        if expected_id != str(row["observation_id"]):
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_RECORD_CORRUPT",
                    "read-observation",
                    "observation identity digest does not match",
                )
            )
        return ShadowObservationRecord(
            sequence=int(row["sequence"]),
            observation_id=expected_id,
            stage=str(row["stage"]),
            subject_id_sha256=str(row["subject_id_sha256"]),
            source_revision_sha256=str(row["source_revision_sha256"]),
            projector_version=str(row["projector_version"]),
            capture_status=str(row["capture_status"]),
            payload_sha256=digest,
            payload=payload,
            first_observed_at=str(row["first_observed_at"]),
            recorded_at=str(row["recorded_at"]),
        )

    @classmethod
    def _comparison_record(cls, row: sqlite3.Row) -> ShadowComparisonRecord:
        payload, digest = cls._decode_payload(row, kind="comparison")
        expected_id = _stable_id(
            "cmp",
            {
                "schema": SHADOW_COMPARISON_SCHEMA,
                "observation_id": str(row["observation_id"]),
                "definition_sha256": str(row["definition_sha256"]),
                "binding_sha256": str(row["binding_sha256"]),
                "comparator_version": str(row["comparator_version"]),
                "overall_verdict": str(row["overall_verdict"]),
                "payload_sha256": digest,
            },
        )
        if expected_id != str(row["comparison_id"]):
            raise ShadowStoreCorruptionError(
                _diagnostic(
                    "SHADOW_STORE_RECORD_CORRUPT",
                    "read-comparison",
                    "comparison identity digest does not match",
                )
            )
        return ShadowComparisonRecord(
            sequence=int(row["sequence"]),
            comparison_id=expected_id,
            observation_id=str(row["observation_id"]),
            definition_sha256=str(row["definition_sha256"]),
            binding_sha256=str(row["binding_sha256"]),
            comparator_version=str(row["comparator_version"]),
            overall_verdict=str(row["overall_verdict"]),
            payload_sha256=digest,
            payload=payload,
            first_compared_at=str(row["first_compared_at"]),
            recorded_at=str(row["recorded_at"]),
        )


__all__ = [
    "ComparisonWrite",
    "ObservationWrite",
    "SHADOW_COMPARISON_SCHEMA",
    "SHADOW_OBSERVATION_SCHEMA",
    "SHADOW_STORE_SCHEMA_VERSION",
    "SHADOW_WORKFLOW_MARKER",
    "ShadowComparisonRecord",
    "ShadowObservationRecord",
    "ShadowPayloadError",
    "ShadowStore",
    "ShadowStoreBusyError",
    "ShadowStoreConflictError",
    "ShadowStoreCorruptionError",
    "ShadowStoreDiagnostic",
    "ShadowStoreError",
    "ShadowWriteOutcome",
    "shadow_database_path",
    "shadow_subject_fingerprint",
]
