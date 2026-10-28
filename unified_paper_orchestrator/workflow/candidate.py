"""Isolated console candidate for the generic workflow engine.

This module is deliberately not imported by the production application.  It
composes the generic library and scheduler only after a caller supplies an
explicit workspace.  Its acceptance executors are test doubles, are private to
``accept_offline``, and never become normal runtime routes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ProducedArtifact
from .contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    ExecutionProfile,
    NodeDefinition,
    NodeType,
    ResourceBinding,
    RetryPolicy,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
)
from .examples import NORMALIZE_REQUEST_IMPLEMENTATION, generic_example_implementations
from .executors import (
    CodexResponse,
    CodexSDKExecutor,
    DeclaredFileOutputCollector,
    DeterministicExecutor,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorDescriptor,
    ExecutorRegistry,
)
from .library import WorkflowLibrary
from .packs import (
    OFFLINE_DEMO_WORKFLOW_ID,
    SCI_FIGURES_WORKFLOW_ID,
    SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
    SCI_SMART_SUBMISSION_WORKFLOW_ID,
    SCI_WRITING_WORKFLOW_ID,
    UNDERGRADUATE_PROMPT_FILES,
    UNDERGRADUATE_PROMPT_SHA256,
    UNDERGRADUATE_THESIS_WORKFLOW_ID,
    build_generic_examples_pack,
    build_sci_runtime_bindings,
    build_sci_workflow_pack,
    build_undergraduate_workflow_pack,
    undergraduate_prompt_manifest,
    undergraduate_schema_validators,
)
from .resources import ContentAddressedPromptStore, prompt_resource_directory
from .scheduler import SchedulerConfig, SchedulerReport, WorkflowScheduler
from .store import WORKFLOW_STORE_SCHEMA_VERSION, WorkflowStore
from .timeline import WorkflowTimelineService


CANDIDATE_VERSION = "0.13.1-candidate"
CANDIDATE_SCHEMA = "unified-paper-orchestrator/generic-workflow-candidate/v1"
ACCEPTANCE_SCHEMA = "unified-paper-orchestrator/generic-workflow-offline-acceptance/v1"
CANDIDATE_WORKSPACE_SCHEMA = (
    "unified-paper-orchestrator/generic-workflow-candidate-workspace/v1"
)
CANDIDATE_WORKSPACE_MARKER = ".generic-workflow-candidate.json"
CANDIDATE_DATABASE_SCHEMA_KEY = "candidate_workspace_schema"
CANDIDATE_DATABASE_ID_KEY = "candidate_workspace_id"
CANDIDATE_DATABASE_RELATIVE_PATH = "state/v2/orchestrator.sqlite3"
CANDIDATE_PROMPT_RELATIVE_PATH = "state/v2/resources/prompts"
CANDIDATE_DATABASE_SCHEMA_SHA256 = (
    "cd22c4c14a4549efc67edb9fcb9e7c8942179eb14a64246e1394f77f67321a18"
)
SUPPORTED_LOCALES = ("zh-CN", "en-US")

_MESSAGES: Mapping[str, Mapping[str, str]] = {
    "zh-CN": {
        "initialized": "已初始化通用工作流候选工作区。",
        "listed": "已读取工作流库。",
        "doctor": "候选工作区自检完成。",
        "demo_created": "已创建离线演示运行。",
        "tick": "已执行一个有界调度周期。",
        "resumed": "已从持久化账本恢复并运行至稳定状态。",
        "timeline": "已读取脱敏运行时间线。",
        "accepted": "离线候选验收完成；在线生产验收未执行。",
        "error": "候选工作流命令失败。",
    },
    "en-US": {
        "initialized": "The generic workflow candidate workspace is initialized.",
        "listed": "The workflow library was read.",
        "doctor": "Candidate workspace diagnostics completed.",
        "demo_created": "The offline demo run was created.",
        "tick": "One bounded scheduler cycle completed.",
        "resumed": "The run was recovered from the durable ledger and advanced until stable.",
        "timeline": "The sanitized run timeline was read.",
        "accepted": "Offline candidate acceptance completed; online production acceptance was not run.",
        "error": "The workflow candidate command failed.",
    },
}


def normalize_locale(value: str) -> str:
    normalized = str(value or "").strip().replace("_", "-").casefold()
    aliases = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "en": "en-US",
        "en-us": "en-US",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported locale: {value!r}") from exc


def localized_message(locale: str, key: str) -> str:
    resolved = normalize_locale(locale)
    try:
        return _MESSAGES[resolved][key]
    except KeyError as exc:
        raise KeyError(f"unknown candidate message: {key}") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _scheduler_report(report: SchedulerReport) -> dict[str, Any]:
    return {
        "owner_acquired": report.owner_acquired,
        "recovered": report.recovered,
        "retries_activated": report.retries_activated,
        "retries_scheduled": report.retries_scheduled,
        "advanced": report.advanced,
        "claimed": report.claimed,
        "completed": report.completed,
        "waiting": report.waiting,
        "failed": report.failed,
        "cancelled": report.cancelled,
        "lease_lost": report.lease_lost,
        "runnable_remaining": report.runnable_remaining,
        "issues": [
            {
                "run_id": item.run_id,
                "node_run_id": item.node_run_id,
                "code": item.code,
                "message": item.message,
            }
            for item in report.issues
        ],
    }


def _file_fingerprint(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return {"exists": False, "sha256": "", "size": 0}
    return {
        "exists": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _metadata(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_reparse_or_link(details: os.stat_result) -> bool:
    attributes = int(getattr(details, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(details.st_mode) or bool(reparse and attributes & reparse)


def _reject_reparse_components(path: Path, *, terminal_file_ok: bool = False) -> None:
    absolute = path.absolute()
    candidates = tuple(reversed(absolute.parents)) + (absolute,)
    for candidate in candidates:
        details = _metadata(candidate)
        if details is None:
            continue
        if _is_reparse_or_link(details):
            raise ValueError("candidate workspace paths must not traverse symlinks or reparse points")
        if candidate == absolute and terminal_file_ok:
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("candidate workspace file target must be a regular file")
        elif not stat.S_ISDIR(details.st_mode):
            raise ValueError("candidate workspace ancestors must be directories")


def _new_workspace_marker_payload() -> dict[str, Any]:
    return {
        "schema": CANDIDATE_WORKSPACE_SCHEMA,
        "version": 1,
        "workspace_id": str(uuid.uuid4()),
        "status": "initializing",
        "database": CANDIDATE_DATABASE_RELATIVE_PATH,
        "prompt_store": CANDIDATE_PROMPT_RELATIVE_PATH,
        "created_by": CANDIDATE_VERSION,
        "created_at": _timestamp(),
        "owner": "GenericWorkflowCandidate",
    }


def _marker_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validate_marker_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("candidate workspace ownership marker must be a JSON object")
    expected_fields = {
        "schema",
        "version",
        "workspace_id",
        "status",
        "database",
        "prompt_store",
        "created_by",
        "created_at",
        "owner",
    }
    if set(value) != expected_fields:
        raise RuntimeError("candidate workspace ownership marker fields are not recognized")
    if (
        value["schema"] != CANDIDATE_WORKSPACE_SCHEMA
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise RuntimeError("candidate workspace ownership marker schema is not recognized")
    if value["owner"] != "GenericWorkflowCandidate":
        raise RuntimeError("candidate workspace ownership marker owner is not recognized")
    if not isinstance(value["status"], str) or value["status"] not in {
        "initializing",
        "ready",
    }:
        raise RuntimeError("candidate workspace ownership marker status is not recognized")
    if (
        value["database"] != CANDIDATE_DATABASE_RELATIVE_PATH
        or value["prompt_store"] != CANDIDATE_PROMPT_RELATIVE_PATH
    ):
        raise RuntimeError("candidate workspace storage layout is not recognized")
    workspace_id = value["workspace_id"]
    try:
        parsed_id = uuid.UUID(str(workspace_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("candidate workspace ID is invalid") from exc
    if str(parsed_id) != workspace_id:
        raise RuntimeError("candidate workspace ID is not canonical")
    created_by = value["created_by"]
    if not isinstance(created_by, str) or not created_by.strip() or len(created_by) > 100:
        raise RuntimeError("candidate workspace creator is invalid")
    created_at = value["created_at"]
    if not isinstance(created_at, str):
        raise RuntimeError("candidate workspace creation timestamp is invalid")
    try:
        parsed_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise RuntimeError("candidate workspace creation timestamp is invalid") from exc
    if parsed_at.tzinfo is None:
        raise RuntimeError("candidate workspace creation timestamp must include a timezone")
    return dict(value)


def _validate_candidate_workspace_marker(root: Path) -> dict[str, Any]:
    _reject_reparse_components(root)
    if not root.is_dir():
        raise RuntimeError("candidate workspace is unavailable")
    if _metadata(root / "workspace.json") is not None:
        raise RuntimeError("production workspaces cannot be used by the generic candidate")
    marker = root / CANDIDATE_WORKSPACE_MARKER
    details = _metadata(marker)
    if details is None:
        raise RuntimeError("candidate workspace ownership marker is missing; run init first")
    if _is_reparse_or_link(details) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("candidate workspace ownership marker must be a regular file")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("candidate workspace ownership marker is unreadable") from exc
    return _validate_marker_payload(payload)


def _claim_candidate_workspace(root: Path) -> dict[str, Any]:
    _reject_reparse_components(root)
    if root.exists():
        if not root.is_dir():
            raise ValueError("candidate workspace must be a directory")
    else:
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if not root.is_dir():
                raise ValueError("candidate workspace must be a directory") from None
    _reject_reparse_components(root)
    if _metadata(root / "workspace.json") is not None:
        raise ValueError("production workspaces cannot be claimed by the generic candidate")
    marker = root / CANDIDATE_WORKSPACE_MARKER
    if _metadata(marker) is not None:
        return _wait_for_candidate_workspace_marker(root)
    entries = tuple(root.iterdir())
    if entries:
        if any(item.name == CANDIDATE_WORKSPACE_MARKER for item in entries):
            return _wait_for_candidate_workspace_marker(root)
        raise ValueError(
            "refusing to claim a non-empty workspace without a candidate ownership marker"
        )
    payload = _new_workspace_marker_payload()
    try:
        with marker.open("xb") as stream:
            stream.write(_marker_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return _wait_for_candidate_workspace_marker(root)
    return _validate_candidate_workspace_marker(root)


def _wait_for_candidate_workspace_marker(root: Path) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    for _attempt in range(200):
        details = _metadata(root / CANDIDATE_WORKSPACE_MARKER)
        if details is not None and (
            _is_reparse_or_link(details) or not stat.S_ISREG(details.st_mode)
        ):
            return _validate_candidate_workspace_marker(root)
        try:
            return _validate_candidate_workspace_marker(root)
        except RuntimeError as exc:
            last_error = exc
            time.sleep(0.01)
    if last_error is not None:
        raise last_error
    raise RuntimeError("candidate workspace ownership marker did not become available")


def _mark_candidate_workspace_ready(root: Path, claimed: Mapping[str, Any]) -> dict[str, Any]:
    current = _validate_candidate_workspace_marker(root)
    identity_fields = set(current) - {"status"}
    if any(current[field] != claimed[field] for field in identity_fields):
        raise RuntimeError("candidate workspace ownership changed during initialization")
    if current["status"] == "ready":
        return current
    updated = {**current, "status": "ready"}
    temporary = root / f".{CANDIDATE_WORKSPACE_MARKER}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(_marker_bytes(updated))
            stream.flush()
            os.fsync(stream.fileno())
        latest = _validate_candidate_workspace_marker(root)
        if any(latest[field] != claimed[field] for field in identity_fields):
            raise RuntimeError("candidate workspace ownership changed during initialization")
        if latest["status"] == "ready":
            return latest
        if latest != current:
            raise RuntimeError("candidate workspace status changed during initialization")
        os.replace(temporary, root / CANDIDATE_WORKSPACE_MARKER)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    verified = _validate_candidate_workspace_marker(root)
    if verified != updated:
        raise RuntimeError("candidate workspace ready marker verification failed")
    return verified


_CANDIDATE_DATABASE_TABLES = (
    "workflow_versions",
    "workflow_runtime_bindings",
    "workflow_runs",
    "node_runs",
    "workflow_events",
    "workflow_sessions",
    "workflow_artifacts",
    "workflow_leases",
)


def _candidate_database_path(root: Path) -> Path:
    return root.absolute() / Path(CANDIDATE_DATABASE_RELATIVE_PATH)


def _reject_candidate_database_paths(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-journal"),
        path.with_name(f".{path.name}.initialize.lock"),
    ):
        _reject_reparse_components(candidate, terminal_file_ok=True)


def _inspect_candidate_connection(
    connection: sqlite3.Connection,
) -> tuple[dict[str, str], bool]:
    schema_rows = [
        [
            str(row[0]),
            str(row[1]),
            str(row[2]),
            None if row[3] is None else str(row[3]),
        ]
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "AND type IN ('table', 'index', 'trigger', 'view') "
            "ORDER BY type, name"
        )
    ]
    schema_payload = json.dumps(
        schema_rows,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(schema_payload).hexdigest() != CANDIDATE_DATABASE_SCHEMA_SHA256:
        raise RuntimeError("candidate workspace database schema fingerprint is not recognized")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    required = {"workflow_schema_meta", *_CANDIDATE_DATABASE_TABLES}
    if tables != required:
        raise RuntimeError("candidate workspace database contains an unexpected table layout")
    metadata = {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT key, value FROM workflow_schema_meta")
    }
    allowed_metadata = {
        "schema_version",
        CANDIDATE_DATABASE_SCHEMA_KEY,
        CANDIDATE_DATABASE_ID_KEY,
    }
    if not set(metadata).issubset(allowed_metadata):
        raise RuntimeError("candidate workspace database contains unexpected metadata")
    if metadata.get("schema_version") != WORKFLOW_STORE_SCHEMA_VERSION:
        raise RuntimeError("candidate workspace database schema version is unsupported")
    owner = {
        key: metadata[key]
        for key in (CANDIDATE_DATABASE_SCHEMA_KEY, CANDIDATE_DATABASE_ID_KEY)
        if key in metadata
    }
    business_empty = all(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
        for table in _CANDIDATE_DATABASE_TABLES
    )
    sequence_empty = connection.execute("SELECT 1 FROM sqlite_sequence LIMIT 1").fetchone() is None
    freelist_empty = int(connection.execute("PRAGMA freelist_count").fetchone()[0]) == 0
    return owner, business_empty and sequence_empty and freelist_empty


def _inspect_candidate_database(path: Path) -> tuple[dict[str, str], bool]:
    _reject_candidate_database_paths(path)
    if not path.is_file():
        raise RuntimeError("candidate workspace database is missing")
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            return _inspect_candidate_connection(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("candidate workspace database could not be verified read-only") from exc


def _require_candidate_database_owner(path: Path, marker: Mapping[str, Any]) -> None:
    owner, _business_empty = _inspect_candidate_database(path)
    expected = {
        CANDIDATE_DATABASE_SCHEMA_KEY: CANDIDATE_WORKSPACE_SCHEMA,
        CANDIDATE_DATABASE_ID_KEY: str(marker["workspace_id"]),
    }
    if owner != expected:
        raise RuntimeError("candidate workspace marker and database owner do not match")


def _bind_candidate_database_owner(store: WorkflowStore, marker: Mapping[str, Any]) -> None:
    expected = {
        CANDIDATE_DATABASE_SCHEMA_KEY: CANDIDATE_WORKSPACE_SCHEMA,
        CANDIDATE_DATABASE_ID_KEY: str(marker["workspace_id"]),
    }
    with store.transaction() as connection:
        current, pristine_if_unowned = _inspect_candidate_connection(connection)
        if current and current != expected:
            raise RuntimeError("candidate workspace database is already owned by another marker")
        if not current and not pristine_if_unowned:
            raise RuntimeError("refusing to bind a non-pristine unowned workflow database")
        for key, value in expected.items():
            connection.execute(
                "INSERT OR IGNORE INTO workflow_schema_meta(key, value) VALUES (?, ?)",
                (key, value),
            )
        verified = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key, value FROM workflow_schema_meta WHERE key IN (?, ?)",
                (CANDIDATE_DATABASE_SCHEMA_KEY, CANDIDATE_DATABASE_ID_KEY),
            )
        }
        if verified != expected:
            raise RuntimeError("candidate workspace database owner verification failed")


def _initialize_candidate_storage(
    library: WorkflowLibrary,
    root: Path,
    marker: Mapping[str, Any],
) -> None:
    database = _candidate_database_path(root)
    prompt_store = prompt_resource_directory(root)
    _reject_candidate_database_paths(database)
    _reject_reparse_components(prompt_store)
    database_exists = database.is_file()
    if marker["status"] == "ready":
        if not database_exists:
            raise RuntimeError("ready candidate workspace database is missing")
        if not prompt_store.is_dir():
            raise RuntimeError("ready candidate workspace Prompt store is missing")
        _require_candidate_database_owner(database, marker)
    elif database_exists:
        owner, business_empty = _inspect_candidate_database(database)
        expected = {
            CANDIDATE_DATABASE_SCHEMA_KEY: CANDIDATE_WORKSPACE_SCHEMA,
            CANDIDATE_DATABASE_ID_KEY: str(marker["workspace_id"]),
        }
        if owner and owner != expected:
            raise RuntimeError("initializing candidate workspace database owner does not match")
        if not owner and not business_empty:
            raise RuntimeError("refusing to adopt a non-empty unowned workflow database")
    library.store.initialize()
    _reject_candidate_database_paths(database)
    _bind_candidate_database_owner(library.store, marker)
    library.prompt_store.initialize()
    _require_candidate_database_owner(database, marker)


def _registry_path() -> Path:
    override = os.environ.get("UNIFIED_PAPER_WORKSPACE_REGISTRY", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "UnifiedPaperOrchestrator" / "workspaces.json"


def _require_contained(root: Path, target: Path) -> Path:
    base = root.absolute()
    candidate = (target if target.is_absolute() else base / target).absolute()
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("candidate output must remain within the explicit workspace") from exc
    if candidate == base:
        raise ValueError("candidate output must be a file below the explicit workspace")
    if not base.is_dir():
        raise ValueError("candidate workspace must exist before writing evidence")
    real_base = base.resolve(strict=True)

    def reject_linklike(path: Path) -> None:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return
        attributes = int(getattr(details, "st_file_attributes", 0))
        if stat.S_ISLNK(details.st_mode) or attributes & 0x400:
            raise ValueError("candidate output path must not traverse a symlink or reparse point")

    reject_linklike(base)
    cursor = base
    for part in relative.parts[:-1]:
        cursor /= part
        reject_linklike(cursor)
        if cursor.exists():
            if not cursor.is_dir():
                raise ValueError("candidate output parent must be a directory")
            try:
                cursor.resolve(strict=True).relative_to(real_base)
            except ValueError as exc:
                raise ValueError(
                    "candidate output parent escaped the explicit workspace"
                ) from exc
    reject_linklike(candidate)
    return candidate


@dataclass(frozen=True)
class CandidatePaths:
    workspace: Path
    database: Path
    prompt_store: Path


class CandidateApplication:
    """Explicit composition root for the standalone generic candidate."""

    def __init__(self, workspace: str | Path) -> None:
        if not isinstance(workspace, (str, Path)) or not os.fspath(workspace).strip():
            raise ValueError("an explicit candidate workspace is required")
        root = Path(workspace).expanduser().absolute()
        if root.exists() and not root.is_dir():
            raise ValueError("candidate workspace must be a directory")
        self.paths = CandidatePaths(
            workspace=root,
            database=_candidate_database_path(root),
            prompt_store=prompt_resource_directory(root),
        )
        self.library = WorkflowLibrary(
            WorkflowStore(self.paths.database),
            ContentAddressedPromptStore(self.paths.prompt_store),
        )

    def _require_initialized(self) -> None:
        marker = _validate_candidate_workspace_marker(self.paths.workspace)
        if marker["status"] != "ready":
            raise RuntimeError("candidate workspace initialization is incomplete; run init again")
        _reject_candidate_database_paths(self.paths.database)
        _reject_reparse_components(self.paths.prompt_store)
        if not self.paths.database.is_file():
            raise RuntimeError("candidate workspace is not initialized; run init first")
        if not self.paths.prompt_store.is_dir():
            raise RuntimeError("candidate Prompt store is not initialized; run init again")
        _require_candidate_database_owner(self.paths.database, marker)

    @staticmethod
    def _undergraduate_asset_root() -> Path:
        return Path(__file__).resolve().parent / "packs" / "prompts" / "undergraduate"

    def materialize_undergraduate_runtime_binding(self) -> WorkflowRuntimeBinding:
        """Copy sealed package prompts into stable content-addressed workspace files."""

        self._require_initialized()
        return self._materialize_undergraduate_runtime_binding()

    def _materialize_undergraduate_runtime_binding(self) -> WorkflowRuntimeBinding:
        version = build_undergraduate_workflow_pack().workflow(UNDERGRADUATE_THESIS_WORKFLOW_ID)
        manifest = undergraduate_prompt_manifest()
        resources: list[ResourceBinding] = []
        for stage, filename in UNDERGRADUATE_PROMPT_FILES.items():
            source = self._undergraduate_asset_root() / filename
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            expected = UNDERGRADUATE_PROMPT_SHA256[stage]
            if digest != expected:
                raise ValueError(f"undergraduate package Prompt seal mismatch: {stage}")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"undergraduate package Prompt is not UTF-8: {stage}") from exc
            record = self.library.prompt_store.put(text, version="p9-workspace-seal")
            if record.content_sha256 != expected:
                raise ValueError(f"materialized undergraduate Prompt digest mismatch: {stage}")
            resources.append(
                ResourceBinding(
                    resource_id=str(manifest[stage]["resource_id"]),
                    absolute_path=record.absolute_path,
                    content_sha256=expected,
                )
            )
        binding = WorkflowRuntimeBinding(
            definition_sha256=version.definition_sha256,
            resources=tuple(resources),
            binding_id=f"{UNDERGRADUATE_THESIS_WORKFLOW_ID}.candidate-workspace",
            metadata={"source": "candidate-workspace-content-addressed"},
        )
        binding.validate(version.definition)
        binding.validate_files(version.definition)
        return binding

    def initialize_defaults(self) -> dict[str, Any]:
        os.environ["UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"] = "1"
        marker = _claim_candidate_workspace(self.paths.workspace)
        _initialize_candidate_storage(self.library, self.paths.workspace, marker)
        records = []
        for pack in (
            build_generic_examples_pack(),
            build_undergraduate_workflow_pack(),
            build_sci_workflow_pack(),
        ):
            records.extend(self.library.install_pack(pack))
        binding = self._materialize_undergraduate_runtime_binding()
        marker = _mark_candidate_workspace_ready(self.paths.workspace, marker)
        _require_candidate_database_owner(self.paths.database, marker)
        return {
            "schema": CANDIDATE_SCHEMA,
            "candidate_version": CANDIDATE_VERSION,
            "status": "initialized",
            "workspace": str(self.paths.workspace),
            "database": str(self.paths.database),
            "workspace_id": marker["workspace_id"],
            "installed_workflows": len(self.library.list_workflows()),
            "registered_versions": len(records),
            "undergraduate_binding_sha256": binding.sha256(),
            "undergraduate_prompt_count": len(binding.resources),
        }

    def list_workflows(self) -> dict[str, Any]:
        self._require_initialized()
        return {
            "schema": CANDIDATE_SCHEMA,
            "status": "ok",
            "workflows": [item.to_dict() for item in self.library.list_workflows()],
        }

    def doctor(self, *, sci_roots: Mapping[str, str | Path] | None = None) -> dict[str, Any]:
        self._require_initialized()
        checks: list[dict[str, Any]] = []
        entries = self.library.list_workflows()
        checks.append(
            {
                "id": "default-packs",
                "status": "PASS" if len(entries) == 6 else "FAIL",
                "workflow_count": len(entries),
            }
        )
        try:
            binding = self.materialize_undergraduate_runtime_binding()
            checks.append(
                {
                    "id": "undergraduate-prompt-seals",
                    "status": "PASS",
                    "resource_count": len(binding.resources),
                    "binding_sha256": binding.sha256(),
                    "stable_workspace_paths": all(
                        self.paths.prompt_store == Path(item.absolute_path).parent
                        for item in binding.resources
                    ),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "id": "undergraduate-prompt-seals",
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        if sci_roots:
            try:
                bindings = self._sci_bindings(sci_roots)
                checks.append(
                    {
                        "id": "sci-prompt-seals",
                        "status": "PASS",
                        "workflow_count": len(bindings),
                        "resource_count": sum(len(item.resources) for item in bindings.values()),
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "id": "sci-prompt-seals",
                        "status": "FAIL",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        else:
            checks.append(
                {
                    "id": "sci-prompt-seals",
                    "status": "NOT_RUN",
                    "reason": "explicit SCI stage roots were not supplied",
                }
            )
        return {
            "schema": CANDIDATE_SCHEMA,
            "status": "PASS" if all(item["status"] != "FAIL" for item in checks) else "FAIL",
            "workspace": str(self.paths.workspace),
            "checks": checks,
            "online_execution": "NOT_RUN",
            "production_promotion_eligible": False,
        }

    def _demo_scheduler(self) -> WorkflowScheduler:
        registry = ExecutorRegistry(
            (DeterministicExecutor(generic_example_implementations(), max_concurrency=2),)
        )
        return WorkflowScheduler(
            self.library.store,
            self.paths.workspace,
            registry,
            config=SchedulerConfig(max_workers=2, lease_seconds=10, heartbeat_seconds=1, poll_seconds=0.01),
        )

    def _published_version(self, workflow_id: str):
        records = self.library.store.list_versions(workflow_id)
        published = [record["version"] for record in records if record["version"].status.value == "published"]
        if not published:
            raise KeyError(f"published workflow is unavailable: {workflow_id}")
        return published[-1]

    def create_demo(
        self,
        request: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        self._require_initialized()
        version = self._published_version(OFFLINE_DEMO_WORKFLOW_ID)
        binding = self.library.runtime_binding(version.version_id)
        run = self.library.create_run(
            version.version_id,
            binding,
            inputs={"request": dict(request)},
            metadata={"candidate": CANDIDATE_VERSION, "mode": "offline-demo"},
            owner="generic-workflow-candidate",
            idempotency_key=idempotency_key,
        )
        return {"schema": CANDIDATE_SCHEMA, "status": "created", "run": run}

    def tick(self, run_id: str) -> dict[str, Any]:
        self._require_initialized()
        report = self._demo_scheduler().run_once((run_id,))
        return {
            "schema": CANDIDATE_SCHEMA,
            "status": self.library.store.get_run(run_id)["status"],
            "run_id": run_id,
            "scheduler": _scheduler_report(report),
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        self._require_initialized()
        report = self._demo_scheduler().run_until_idle((run_id,), timeout_seconds=60)
        return {
            "schema": CANDIDATE_SCHEMA,
            "status": self.library.store.get_run(run_id)["status"],
            "run_id": run_id,
            "scheduler": _scheduler_report(report),
        }

    def timeline(self, run_id: str) -> dict[str, Any]:
        self._require_initialized()
        return WorkflowTimelineService(self.library.store).snapshot(run_id).to_dict()

    def _sci_bindings(
        self,
        roots: Mapping[str, str | Path],
    ) -> Mapping[str, WorkflowRuntimeBinding]:
        required = {"writing", "figures", "journal_author"}
        if set(roots) != required:
            raise ValueError("SCI roots must contain exactly writing, figures, and journal_author")
        bindings = build_sci_runtime_bindings(
            writing_root=roots["writing"],
            figure_root=roots["figures"],
            journal_author_root=roots["journal_author"],
        )
        pack = build_sci_workflow_pack()
        for workflow_id, binding in bindings.items():
            binding.validate_files(pack.workflow(workflow_id).definition)
        return bindings

    def accept_offline(
        self,
        *,
        sci_roots: Mapping[str, str | Path] | None = None,
    ) -> dict[str, Any]:
        """Run preserved, no-network acceptance and report its limits honestly."""

        os.environ["UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"] = "1"
        registry = _registry_path()
        registry_before = _file_fingerprint(registry)
        self.initialize_defaults()
        # Keep child ledgers below the common Windows MAX_PATH boundary even
        # when pytest or an operator supplies a fairly deep workspace root.
        acceptance_root: Path | None = None
        for index in range(4096):
            candidate_root = self.paths.workspace / f"a{index:x}"
            try:
                candidate_root.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            acceptance_root = candidate_root
            break
        if acceptance_root is None:
            raise RuntimeError("candidate acceptance evidence namespace is exhausted")
        checks: list[dict[str, Any]] = []

        def run_check(check_id: str, assurance: str, callback) -> None:
            try:
                evidence = dict(callback())
                status = str(evidence.pop("status", "PASS"))
                checks.append(
                    {
                        "id": check_id,
                        "status": status,
                        "assurance": assurance,
                        "evidence": evidence,
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "id": check_id,
                        "status": "FAIL",
                        "assurance": assurance,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )

        run_check(
            "deterministic-concurrency-and-custom-clone",
            "real-offline-execution",
            lambda: self._accept_deterministic_concurrency(acceptance_root / "d"),
        )
        run_check(
            "undergraduate-six-node-chain",
            "fake-provider-mechanical-contract",
            lambda: self._accept_undergraduate(acceptance_root / "u"),
        )
        run_check(
            "restart-recovery",
            "real-ledger-reopen",
            lambda: self._accept_restart(acceptance_root / "s"),
        )
        run_check(
            "durable-retry",
            "real-offline-execution",
            lambda: self._accept_retry(acceptance_root / "r"),
        )
        run_check(
            "workspace-isolation",
            "real-offline-execution",
            lambda: self._accept_workspace_isolation(acceptance_root / "w"),
        )
        run_check(
            "two-subject-sci-four-workflow-smoke",
            "contract-simulation-no-provider-no-browser-no-schema-validation",
            lambda: self._accept_sci(acceptance_root / "c", sci_roots),
        )

        registry_after = _file_fingerprint(registry)
        registry_unchanged = registry_before == registry_after
        checks.append(
            {
                "id": "workspace-registry-unchanged",
                "status": "PASS" if registry_unchanged else "FAIL",
                "assurance": "byte-fingerprint",
                "evidence": {
                    "before": registry_before,
                    "after": registry_after,
                    "registry_disabled": os.environ.get(
                        "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY", ""
                    ).casefold()
                    in {"1", "true", "yes", "on"},
                },
            }
        )
        failures = [item for item in checks if item["status"] == "FAIL"]
        not_run = [item for item in checks if item["status"] == "NOT_RUN"]
        offline_status = "FAIL" if failures else ("PASS_WITH_LIMITATIONS" if not_run else "PASS")
        return {
            "schema": ACCEPTANCE_SCHEMA,
            "candidate_version": CANDIDATE_VERSION,
            "created_at": _timestamp(),
            "workspace": str(self.paths.workspace),
            "evidence_root": str(acceptance_root),
            "offline_candidate_acceptance": offline_status,
            "checks": checks,
            "online_production_acceptance": {
                "status": "NOT_RUN",
                "production_equivalence": False,
                "reasons": [
                    "no real Codex provider was called",
                    "no model availability, token usage, cost, or remote session was verified",
                    "no browser, journal account, or submission site was opened",
                    "no irreversible Smart Submission action was authorized",
                    "native SCI cutover remains feature-gated and is not proven by contract simulation",
                ],
            },
            "production_promotion_eligible": False,
        }

    def write_acceptance_report(self, report: Mapping[str, Any], output: str | Path) -> Path:
        target = _require_contained(self.paths.workspace, Path(output).expanduser())
        relative_parent = target.parent.relative_to(self.paths.workspace)
        cursor = self.paths.workspace
        for part in relative_parent.parts:
            cursor /= part
            try:
                cursor.mkdir()
            except FileExistsError:
                pass
            _require_contained(self.paths.workspace, cursor / "containment-probe")
        target = _require_contained(self.paths.workspace, target)
        payload = json.dumps(dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        return target

    @staticmethod
    def _fresh_library(root: Path) -> WorkflowLibrary:
        marker = _claim_candidate_workspace(root)
        library = WorkflowLibrary(
            WorkflowStore(_candidate_database_path(root)),
            ContentAddressedPromptStore(prompt_resource_directory(root)),
        )
        _initialize_candidate_storage(library, root, marker)
        _mark_candidate_workspace_ready(root, marker)
        return library

    def _accept_deterministic_concurrency(self, root: Path) -> Mapping[str, Any]:
        library = self._fresh_library(root)
        source = library.install_pack(build_generic_examples_pack())[0]["version"]
        source_sha256 = source.definition_sha256
        clone = library.clone_version(
            source.version_id,
            workflow_id="custom.acceptance.concurrent-planner",
            name="Acceptance concurrent planner",
        )["version"]
        published = library.publish(clone.version_id)["version"]
        binding = library.runtime_binding(published.version_id)
        request = {
            "goal": "Verify generic workflow concurrency",
            "actions": ["prepare", "execute", "verify"],
            "constraints": ["offline", "deterministic"],
        }
        runs = [
            library.create_run(
                published.version_id,
                binding,
                inputs={"request": request},
                idempotency_key=f"acceptance-concurrent-{index}",
            )
            for index in (1, 2)
        ]
        run_ids = {str(item["run_id"]) for item in runs}
        barrier = threading.Barrier(2, timeout=10)
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        handlers = {}
        for implementation_ref, handler in generic_example_implementations().items():
            def traced(context: ExecutionContext, *, _handler=handler):
                nonlocal active, maximum_active
                if context.node.implementation_ref == NORMALIZE_REQUEST_IMPLEMENTATION:
                    with lock:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    try:
                        barrier.wait()
                        return _handler(context)
                    finally:
                        with lock:
                            active -= 1
                return _handler(context)

            handlers[implementation_ref] = traced
        scheduler = WorkflowScheduler(
            library.store,
            root,
            ExecutorRegistry((DeterministicExecutor(handlers, max_concurrency=2),)),
            config=SchedulerConfig(max_workers=2, lease_seconds=10, heartbeat_seconds=1, poll_seconds=0.01),
        )
        report = scheduler.run_until_idle(tuple(run_ids), timeout_seconds=60)
        statuses = {str(library.store.get_run(run_id)["status"]) for run_id in run_ids}
        if report.failed or statuses != {"completed"} or maximum_active != 2:
            raise AssertionError("two deterministic runs did not execute concurrently to completion")
        if library.store.get_version(source.version_id)["version"].definition_sha256 != source_sha256:
            raise AssertionError("cloning changed the protected source definition")
        return {
            "run_count": len(run_ids),
            "maximum_simultaneous_normalizers": maximum_active,
            "completed_nodes": report.completed,
            "custom_version_id": published.version_id,
            "source_definition_unchanged": True,
        }

    def _accept_undergraduate(self, root: Path) -> Mapping[str, Any]:
        app = CandidateApplication(root)
        app.initialize_defaults()
        version = app._published_version(UNDERGRADUATE_THESIS_WORKFLOW_ID)
        binding = app.materialize_undergraduate_runtime_binding()
        run = app.library.create_run(
            version.version_id,
            binding,
            inputs={
                "thesis_brief": {"topic": "Offline acceptance topic", "language": "zh-CN"},
                "evidence_bundle": {"sources": []},
            },
            idempotency_key="candidate-undergraduate-acceptance",
            owner="candidate-acceptance",
        )
        seen: list[str] = []

        class Client:
            def run(client_self, *, session, request, control):
                node = version.definition.nodes[len(seen)]
                seen.append(node.node_id)
                if request.network_access:
                    raise AssertionError("undergraduate acceptance unexpectedly enabled network access")
                for artifact_id, relative_path in node.metadata["declared_output_files"].items():
                    target = request.output_root / Path(relative_path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if artifact_id == "evidence_register":
                        payload: Any = {
                            "schema": "upo.undergraduate.evidence-register.v1",
                            "language": "zh-CN",
                            "research_questions": [],
                            "sources": [],
                            "claim_evidence_map": [],
                            "gaps": ["[evidence gap: no source supplied]"],
                            "warnings": [],
                        }
                        target.write_text(json.dumps(payload), encoding="utf-8")
                    elif artifact_id == "integrity_report":
                        payload = {
                            "schema": "upo.undergraduate.integrity-report.v1",
                            "language": "zh-CN",
                            "verdict": "needs_evidence",
                            "checks": [],
                            "unresolved_gaps": ["no source supplied"],
                            "required_human_actions": ["supply verified evidence"],
                        }
                        target.write_text(json.dumps(payload), encoding="utf-8")
                    else:
                        target.write_text("# Acceptance fixture\n\n[evidence gap: no source supplied]\n", encoding="utf-8")
                return CodexResponse(
                    "completed",
                    output_text="acceptance fixture created",
                    external_session_id="candidate-undergraduate-fake-session",
                    usage={"total_tokens": 1},
                )

        scheduler = WorkflowScheduler(
            app.library.store,
            root,
            ExecutorRegistry(
                (CodexSDKExecutor(Client(), output_collector=DeclaredFileOutputCollector()),)
            ),
            config=SchedulerConfig(max_workers=1, lease_seconds=10, heartbeat_seconds=1, poll_seconds=0.01),
            schema_validators=undergraduate_schema_validators(),
        )
        report = scheduler.run_until_idle((str(run["run_id"]),), timeout_seconds=60)
        if report.failed or app.library.store.get_run(str(run["run_id"]))["status"] != "completed":
            raise AssertionError("undergraduate fake-provider contract chain did not complete")
        return {
            "provider": "deterministic-fake-provider",
            "production_equivalence": False,
            "network_access": False,
            "nodes": seen,
            "completed_nodes": report.completed,
            "stable_prompt_paths": all(
                app.paths.prompt_store == Path(item.absolute_path).parent for item in binding.resources
            ),
        }

    def _accept_restart(self, root: Path) -> Mapping[str, Any]:
        first = CandidateApplication(root)
        first.initialize_defaults()
        created = first.create_demo(
            {"goal": "Recover after restart", "actions": ["one", "two"], "constraints": ["offline"]},
            idempotency_key="candidate-restart",
        )
        run_id = str(created["run"]["run_id"])
        durable_before = first.library.store.get_run(run_id)
        tick = first.tick(run_id)
        nodes_before = first.library.store.latest_node_runs(run_id)
        statuses_before = [str(item["status"]) for item in nodes_before]
        if (
            tick["scheduler"]["completed"] != 1
            or statuses_before.count("completed") != 1
            or sum(status in {"pending", "ready"} for status in statuses_before) != 2
        ):
            raise AssertionError("restart fixture did not stop after one durable node")
        second = CandidateApplication(root)
        resumed = second.resume(run_id)
        durable_after = second.library.store.get_run(run_id)
        nodes_after = second.library.store.latest_node_runs(run_id)
        statuses_after = [str(item["status"]) for item in nodes_after]
        if resumed["status"] != "completed" or statuses_after != ["completed"] * 3:
            raise AssertionError("reopened candidate did not recover the durable run")
        if (
            durable_before["workflow_version_id"] != durable_after["workflow_version_id"]
            or durable_before["binding_sha256"] != durable_after["binding_sha256"]
        ):
            raise AssertionError("restart changed the immutable version or runtime binding")
        return {
            "run_id": run_id,
            "node_statuses_before_reopen": statuses_before,
            "node_statuses_after_reopen": statuses_after,
            "final_status": resumed["status"],
            "version_id_unchanged": True,
            "binding_sha256": durable_after["binding_sha256"],
        }

    def _accept_retry(self, root: Path) -> Mapping[str, Any]:
        library = self._fresh_library(root)
        output = ArtifactSpec("retry_report", media_type="text/markdown")
        node = NodeDefinition(
            node_id="retry-once",
            name="Retry once",
            node_type=NodeType.DETERMINISTIC,
            display_order=10,
            implementation_ref="candidate.acceptance.retry-once.v1",
            retry_policy=RetryPolicy(
                max_attempts=2,
                initial_delay_seconds=0,
                max_delay_seconds=0,
                retryable_error_codes=("candidate-transient",),
            ),
            execution_profile=ExecutionProfile(network_access=False),
            outputs=(output,),
        )
        definition = WorkflowDefinition(
            workflow_id="custom.candidate.retry",
            name="Candidate durable retry",
            nodes=(node,),
            edges=(),
            outputs=(output,),
            output_bindings=(
                ArtifactBinding(
                    "retry_report",
                    ArtifactSourceKind.NODE_OUTPUT,
                    "retry_report",
                    "retry-once",
                ),
            ),
        )
        draft = library.store.append_custom_version(definition)["version"]
        published = library.publish(draft.version_id)["version"]
        binding = library.runtime_binding(published.version_id)
        run = library.create_run(
            published.version_id,
            binding,
            owner="candidate-acceptance",
            idempotency_key="candidate-retry",
        )

        def retry_once(context: ExecutionContext) -> ExecutionResult:
            if context.node_run.attempt == 1:
                return ExecutionResult(
                    ExecutionOutcome.RETRYABLE_FAILURE,
                    error_code="candidate-transient",
                    message="injected offline transient failure",
                )
            target = context.output_root / "retry-report.md"
            target.write_text("# Retry recovered\n", encoding="utf-8")
            return ExecutionResult(
                ExecutionOutcome.COMPLETED,
                produced_artifacts=(ProducedArtifact("retry_report", target, "text/markdown"),),
            )

        scheduler = WorkflowScheduler(
            library.store,
            root,
            ExecutorRegistry(
                (
                    DeterministicExecutor(
                        {"candidate.acceptance.retry-once.v1": retry_once},
                        max_concurrency=1,
                    ),
                )
            ),
            config=SchedulerConfig(max_workers=1, lease_seconds=10, heartbeat_seconds=1, poll_seconds=0.01),
        )
        report = scheduler.run_until_idle((str(run["run_id"]),), timeout_seconds=60)
        attempts = library.store.list_node_runs(str(run["run_id"]))
        statuses = [str(item["status"]) for item in attempts]
        if (
            statuses != ["retryable-failure", "completed"]
            or library.store.get_run(str(run["run_id"]))["status"] != "completed"
        ):
            raise AssertionError("durable retry did not preserve both attempts")
        return {
            "run_id": run["run_id"],
            "attempt_statuses": statuses,
            "retry_count": len(attempts) - 1,
            "scheduler_retries_scheduled": report.retries_scheduled,
            "final_status": library.store.get_run(str(run["run_id"]))["status"],
        }

    def _accept_workspace_isolation(self, root: Path) -> Mapping[str, Any]:
        left = CandidateApplication(root / "left")
        right = CandidateApplication(root / "right")
        left.initialize_defaults()
        right.initialize_defaults()
        created = left.create_demo(
            {"goal": "Remain in workspace A", "actions": ["verify"], "constraints": []},
            idempotency_key="workspace-left-only",
        )
        left_runs = left.library.store.list_runs()
        right_runs = right.library.store.list_runs()
        if len(left_runs) != 1 or right_runs:
            raise AssertionError("candidate workspaces leaked run records")
        if left.paths.database == right.paths.database or left.paths.prompt_store == right.paths.prompt_store:
            raise AssertionError("candidate workspace storage paths are not isolated")
        reopened = CandidateApplication(root / "left")
        if reopened.library.store.get_run(str(created["run"]["run_id"]))["run_id"] != created["run"]["run_id"]:
            raise AssertionError("reopened workspace did not retain its own run")
        return {
            "left_run_count": len(left_runs),
            "right_run_count": len(right_runs),
            "database_paths_distinct": True,
            "prompt_paths_distinct": True,
        }

    def _accept_sci(
        self,
        root: Path,
        sci_roots: Mapping[str, str | Path] | None,
    ) -> Mapping[str, Any]:
        if sci_roots is None:
            return {
                "status": "NOT_RUN",
                "reason": "explicit SCI stage roots were not supplied",
                "production_equivalence": False,
            }
        app = CandidateApplication(root)
        app.initialize_defaults()
        pack = build_sci_workflow_pack()
        bindings = app._sci_bindings(sci_roots)
        versions = {version.workflow_id: version for version in pack.workflows}
        writing_runs: list[str] = []
        all_runs: list[str] = []
        run_labels: dict[str, dict[str, str]] = {}
        for subject in ("subject-a", "subject-b"):
            inputs_by_workflow = {
                SCI_WRITING_WORKFLOW_ID: {
                    "job": {"subject": subject},
                    "research_profile": f"# Research profile\n\n{subject}\n",
                },
                SCI_FIGURES_WORKFLOW_ID: {
                    "job": {"subject": subject},
                    "source_tex": "\\documentclass{article}\\begin{document}fixture\\end{document}",
                },
                SCI_JOURNAL_AUTHOR_WORKFLOW_ID: {
                    "job": {"subject": subject},
                    "work_manifest": {"subject": subject},
                    "author_info": "Status: TEST FIXTURE",
                    "submission_metadata_fingerprint": {"subject": subject},
                },
                SCI_SMART_SUBMISSION_WORKFLOW_ID: {"job": {"subject": subject}},
            }
            for workflow_id in (
                SCI_WRITING_WORKFLOW_ID,
                SCI_FIGURES_WORKFLOW_ID,
                SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
                SCI_SMART_SUBMISSION_WORKFLOW_ID,
            ):
                run = app.library.create_run(
                    versions[workflow_id].version_id,
                    bindings[workflow_id],
                    inputs=inputs_by_workflow[workflow_id],
                    metadata={
                        "acceptance_subject": subject,
                        "execution_mode": "contract-simulation",
                        "production_equivalence": False,
                    },
                    owner="candidate-sci-contract-simulation",
                    idempotency_key=f"candidate-sci-{subject}-{workflow_id}",
                )
                run_id = str(run["run_id"])
                all_runs.append(run_id)
                run_labels[run_id] = {"subject": subject, "workflow_id": workflow_id}
                if workflow_id == SCI_WRITING_WORKFLOW_ID:
                    writing_runs.append(run_id)

        barrier = threading.Barrier(2, timeout=10)
        concurrency = {"active": 0, "maximum": 0}
        concurrency_lock = threading.Lock()
        codex = _ContractSimulationExecutor(
            NodeType.CODEX,
            barrier_run_ids=frozenset(writing_runs),
            barrier=barrier,
            concurrency=concurrency,
            concurrency_lock=concurrency_lock,
        )
        plugin = _ContractSimulationExecutor(NodeType.PLUGIN)
        acceptance_schema_refs = {
            spec.schema_ref
            for workflow in pack.workflows
            for node in workflow.definition.nodes
            for spec in node.outputs
            if spec.schema_ref
        }
        scheduler = WorkflowScheduler(
            app.library.store,
            root,
            ExecutorRegistry((codex, plugin)),
            config=SchedulerConfig(
                max_workers=8,
                lease_seconds=10,
                heartbeat_seconds=0.1,
                poll_seconds=0.01,
            ),
            schema_validators={schema_ref: lambda _path: None for schema_ref in acceptance_schema_refs},
        )
        writing_report = scheduler.run_until_idle(tuple(writing_runs), timeout_seconds=120)
        remaining = tuple(run_id for run_id in all_runs if run_id not in set(writing_runs))
        remaining_report = scheduler.run_until_idle(remaining, timeout_seconds=120)
        statuses = {str(app.library.store.get_run(run_id)["status"]) for run_id in all_runs}
        if (
            writing_report.failed
            or remaining_report.failed
            or statuses != {"completed"}
            or concurrency["maximum"] != 2
        ):
            failed_runs = []
            for run_id in all_runs:
                run_record = app.library.store.get_run(run_id)
                if run_record["status"] == "completed":
                    continue
                failed_runs.append(
                    {
                        **run_labels[run_id],
                        "run_id": run_id,
                        "status": run_record["status"],
                        "nodes": [
                            {
                                "node_id": item["node_id"],
                                "attempt": item["attempt"],
                                "status": item["status"],
                                "error_code": item["error_code"],
                                "error_message": item["error_message"],
                            }
                            for item in app.library.store.list_node_runs(run_id)
                        ],
                    }
                )
            return {
                "status": "FAIL",
                "execution_mode": "contract-simulation",
                "production_equivalence": False,
                "writing_report": _scheduler_report(writing_report),
                "remaining_report": _scheduler_report(remaining_report),
                "maximum_simultaneous_writing_entries": concurrency["maximum"],
                "run_statuses": sorted(statuses),
                "failed_runs": failed_runs,
            }
        return {
            "execution_mode": "contract-simulation",
            "provider": "acceptance-only-contract-executor",
            "browser_opened": False,
            "submission_performed": False,
            "production_equivalence": False,
            "schema_validation": "NOT_RUN",
            "cross_workflow_artifact_handoff": "NOT_RUN",
            "end_to_end_stage_order": "NOT_RUN",
            "subject_count": 2,
            "workflow_run_count": len(all_runs),
            "maximum_simultaneous_writing_entries": concurrency["maximum"],
            "completed_nodes": writing_report.completed + remaining_report.completed,
            "prompt_resource_count": sum(len(item.resources) for item in bindings.values()),
        }


class _ContractSimulationExecutor:
    """Acceptance-only artifact producer; never used by normal candidate commands."""

    def __init__(
        self,
        node_type: NodeType,
        *,
        barrier_run_ids: frozenset[str] = frozenset(),
        barrier: threading.Barrier | None = None,
        concurrency: dict[str, int] | None = None,
        concurrency_lock: threading.Lock | None = None,
    ) -> None:
        self.node_type = NodeType(node_type)
        self.barrier_run_ids = barrier_run_ids
        self.barrier = barrier
        self.concurrency = concurrency
        self.concurrency_lock = concurrency_lock

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            executor_id=f"candidate-acceptance-{self.node_type.value}",
            node_type=self.node_type,
            available=True,
            detail="Acceptance-only contract simulation; no provider or browser.",
            side_effecting=False,
            max_concurrency=8,
        )

    @staticmethod
    def _write_output(context: ExecutionContext, spec: ArtifactSpec) -> ProducedArtifact:
        suffixes = {
            "application/json": ".json",
            "application/pdf": ".pdf",
            "application/x-bibtex": ".bib",
            "application/x-tex": ".tex",
            "text/markdown": ".md",
        }
        suffix = suffixes.get(spec.media_type, ".bin")
        target = context.output_root / f"{spec.artifact_id}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if spec.media_type == "application/json":
            target.write_text(
                json.dumps(
                    {
                        "acceptance_fixture": True,
                        "artifact_id": spec.artifact_id,
                        "schema_ref": spec.schema_ref,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        elif spec.media_type == "application/pdf":
            target.write_bytes(b"%PDF-1.4\n% acceptance fixture\n%%EOF\n")
        else:
            target.write_text(
                f"Acceptance-only fixture for {spec.artifact_id}.\n",
                encoding="utf-8",
            )
        return ProducedArtifact(
            spec.artifact_id,
            target,
            media_type=spec.media_type,
            schema_ref=spec.schema_ref,
            metadata={"acceptance_fixture": True, "production_equivalence": False},
        )

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        if context.node.node_type is not self.node_type:
            return ExecutionResult(
                ExecutionOutcome.BLOCKED,
                error_code="ACCEPTANCE_NODE_TYPE_MISMATCH",
            )
        use_barrier = (
            context.workflow_run.run_id in self.barrier_run_ids
            and context.node.node_id == "writing-01"
            and self.barrier is not None
            and self.concurrency is not None
            and self.concurrency_lock is not None
        )
        if use_barrier:
            assert self.concurrency is not None
            assert self.concurrency_lock is not None
            with self.concurrency_lock:
                self.concurrency["active"] += 1
                self.concurrency["maximum"] = max(
                    self.concurrency["maximum"], self.concurrency["active"]
                )
            try:
                assert self.barrier is not None
                self.barrier.wait()
            finally:
                with self.concurrency_lock:
                    self.concurrency["active"] -= 1
        # Optional outputs can still be consumed by later nodes in a protected
        # pack. Produce every declared fixture so downstream bindings exercise
        # the complete artifact graph rather than only minimum completion.
        produced = tuple(self._write_output(context, spec) for spec in context.node.outputs)
        external_session_id = (
            f"candidate-simulated-{context.workflow_run.run_id}-{context.node.session_policy.session_group}"
            if self.node_type is NodeType.CODEX
            else ""
        )
        return ExecutionResult(
            ExecutionOutcome.COMPLETED,
            message="acceptance-only contract simulation",
            produced_artifacts=produced,
            external_session_id=external_session_id,
            metadata={
                "execution_mode": "contract-simulation",
                "production_equivalence": False,
            },
        )


__all__ = [
    "ACCEPTANCE_SCHEMA",
    "CANDIDATE_DATABASE_ID_KEY",
    "CANDIDATE_DATABASE_SCHEMA_KEY",
    "CANDIDATE_SCHEMA",
    "CANDIDATE_VERSION",
    "CANDIDATE_WORKSPACE_MARKER",
    "CANDIDATE_WORKSPACE_SCHEMA",
    "SUPPORTED_LOCALES",
    "CandidateApplication",
    "CandidatePaths",
    "localized_message",
    "normalize_locale",
]
