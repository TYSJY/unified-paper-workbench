from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.contracts import canonical_json
from unified_paper_orchestrator.workflow.shadow_store import (
    ComparisonWrite,
    ObservationWrite,
    SHADOW_WORKFLOW_MARKER,
    ShadowPayloadError,
    ShadowStore,
    ShadowStoreBusyError,
    ShadowStoreConflictError,
    ShadowStoreCorruptionError,
    ShadowStoreError,
    shadow_database_path,
)
from unified_paper_orchestrator.workflow.store import WorkflowStore, WorkflowStoreError


BASE_TIME = datetime(2026, 8, 5, 8, 0, 0, 123456, tzinfo=timezone.utc)
RUNTIME_TABLES = {
    "workflow_versions",
    "workflow_runtime_bindings",
    "workflow_runs",
    "node_runs",
    "workflow_events",
    "workflow_sessions",
    "workflow_artifacts",
    "workflow_leases",
}


def _store(tmp_path: Path, *, busy_timeout_ms: int = 500) -> ShadowStore:
    store = ShadowStore(shadow_database_path(tmp_path), busy_timeout_ms=busy_timeout_ms)
    store.initialize()
    return store


def _observation(
    *,
    stage: str = "writing",
    subject_id: str = "paper-confidential-001",
    revision: str = "a" * 64,
    projector_version: str = "legacy-projector-v1",
    capture_status: str = "coherent",
    payload: dict[str, object] | None = None,
) -> ObservationWrite:
    return ObservationWrite(
        stage=stage,
        subject_id=subject_id,
        source_revision_sha256=revision,
        projector_version=projector_version,
        capture_status=capture_status,
        payload=payload
        or {
            "stage_status": "running",
            "completed_step": 2,
            "active_step": 3,
            "session_fingerprint": "b" * 64,
            "model": "gpt-5.5",
            "reasoning_effort": "high",
            "token_usage": {
                "last": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
            },
            "prompt_sha256": "c" * 64,
            "artifacts": [
                {
                    "artifact_id": "draft",
                    "relative_uri_sha256": "d" * 64,
                    "content_sha256": "e" * 64,
                    "size_bytes": 42,
                    "media_type": "application/x-tex",
                }
            ],
        },
    )


def _comparison(
    observation_id: str,
    *,
    definition_sha256: str = "f" * 64,
    binding_sha256: str = "",
    comparator_version: str = "legacy-comparator-v1",
    verdict: str = "exact",
    payload: dict[str, object] | None = None,
) -> ComparisonWrite:
    return ComparisonWrite(
        observation_id=observation_id,
        definition_sha256=definition_sha256,
        binding_sha256=binding_sha256,
        comparator_version=comparator_version,
        overall_verdict=verdict,
        payload=payload
        or {
            "fields": [
                {
                    "field": "stage_status",
                    "verdict": verdict,
                    "expected": "running",
                    "observed": "running",
                }
            ]
        },
    )


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _database_bytes(path: Path) -> bytes:
    chunks: list[bytes] = []
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.is_file():
            chunks.append(candidate.read_bytes())
    return b"".join(chunks)


def test_path_resolution_construction_and_failed_try_are_side_effect_free(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-not-created"
    path = shadow_database_path(workspace)
    assert path == workspace.resolve() / "state" / "v2" / "workflow-shadow.sqlite3"
    assert not workspace.exists()

    store = ShadowStore(path)
    assert not workspace.exists()
    outcome = store.try_record_observation(_observation())

    assert not outcome.ok
    assert outcome.record is None
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "SHADOW_STORE_NOT_INITIALIZED"
    assert str(path) not in outcome.diagnostic.detail
    assert not workspace.exists()


def test_nonstandard_empty_database_path_is_rejected_before_it_can_be_claimed(tmp_path: Path) -> None:
    production_candidate = tmp_path / "workflow.sqlite3"

    with pytest.raises(ValueError, match="workflow-shadow.sqlite3"):
        ShadowStore(production_candidate)

    assert not production_candidate.exists()
    assert list(tmp_path.iterdir()) == []

    misleading_name = tmp_path / "other" / "workflow-shadow.sqlite3"
    with pytest.raises(ValueError, match="state/v2/workflow-shadow.sqlite3"):
        ShadowStore(misleading_name)
    assert not misleading_name.exists()


def test_initialize_creates_only_shadow_tables_and_poison_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)

    tables = _tables(store.path)
    assert not tables.intersection(RUNTIME_TABLES)
    assert {
        "workflow_schema_meta",
        "shadow_schema_meta",
        "shadow_observations",
        "shadow_comparisons",
    }.issubset(tables)
    with sqlite3.connect(store.path) as connection:
        marker = connection.execute(
            "SELECT value FROM workflow_schema_meta WHERE key='schema_version'"
        ).fetchone()
        mode = connection.execute("PRAGMA journal_mode").fetchone()
        synchronous = connection.execute("PRAGMA synchronous").fetchone()
    assert marker == (SHADOW_WORKFLOW_MARKER,)
    assert str(mode[0]).casefold() == "wal"
    assert int(synchronous[0]) >= 2


def test_shadow_and_workflow_stores_reject_each_others_databases(tmp_path: Path) -> None:
    shadow = _store(tmp_path / "shadow")
    before = _tables(shadow.path)
    with pytest.raises(WorkflowStoreError, match="unsupported workflow database schema"):
        WorkflowStore(shadow.path).initialize()
    assert _tables(shadow.path) == before
    assert not _tables(shadow.path).intersection(RUNTIME_TABLES)

    workflow_path = shadow_database_path(tmp_path / "workflow")
    workflow = WorkflowStore(workflow_path)
    workflow.initialize()
    workflow_before = _tables(workflow_path)
    with pytest.raises(ShadowStoreError) as captured:
        ShadowStore(workflow_path).initialize()
    assert captured.value.diagnostic.code == "SHADOW_STORE_NOT_ISOLATED"
    assert _tables(workflow_path) == workflow_before
    assert "shadow_observations" not in workflow_before


def test_initialize_rejects_nonempty_unmarked_database_without_ddl(tmp_path: Path) -> None:
    path = shadow_database_path(tmp_path / "unrelated")
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = _tables(path)

    with pytest.raises(ShadowStoreError) as captured:
        ShadowStore(path).initialize()

    assert captured.value.diagnostic.code == "SHADOW_STORE_NOT_ISOLATED"
    assert _tables(path) == before


def test_shadow_store_rejects_symlink_or_reparse_ancestor(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    linked_workspace = tmp_path / "linked"
    real_workspace.mkdir()
    try:
        linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable for this account")

    store = ShadowStore(shadow_database_path(linked_workspace))
    with pytest.raises(ShadowStoreError) as captured:
        store.initialize()

    assert captured.value.diagnostic.code == "SHADOW_STORE_PATH_UNSAFE"
    assert not (real_workspace / "state").exists()


def test_concurrent_initialization_is_idempotent(tmp_path: Path) -> None:
    path = shadow_database_path(tmp_path / "concurrent")

    def initialize(_: int) -> None:
        ShadowStore(path, busy_timeout_ms=2_000).initialize()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(initialize, range(32)))

    store = ShadowStore(path)
    assert dict(store.summary()) == {
        "schema_version": "1",
        "observations": 0,
        "comparisons": 0,
        "last_observation_sequence": 0,
        "last_comparison_sequence": 0,
    }
    assert not _tables(path).intersection(RUNTIME_TABLES)


def test_concurrent_initialization_is_safe_across_processes(tmp_path: Path) -> None:
    path = shadow_database_path(tmp_path / "processes")
    script = "\n".join(
        (
            "from pathlib import Path",
            "from unified_paper_orchestrator.workflow.shadow_store import ShadowStore",
            "ShadowStore(Path(__import__('sys').argv[1]), busy_timeout_ms=2000).initialize()",
        )
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def initialize(_: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", "-c", script, str(path)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        completed = list(pool.map(initialize, range(8)))

    assert [item.stderr for item in completed if item.returncode != 0] == []
    assert dict(ShadowStore(path).summary())["observations"] == 0
    assert not _tables(path).intersection(RUNTIME_TABLES)


def test_observation_uses_canonical_payload_and_is_idempotent_across_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_payload = {"b": {"value": 2}, "a": [3, 1]}
    second_payload = {"a": [3, 1], "b": {"value": 2}}

    first = store.record_observation(
        _observation(payload=first_payload),
        observed_at=BASE_TIME,
        recorded_at=BASE_TIME,
    )
    second = store.record_observation(
        _observation(payload=second_payload),
        observed_at=BASE_TIME + timedelta(days=1),
        recorded_at=BASE_TIME + timedelta(days=1),
    )

    assert second == first
    assert first.sequence == 1
    assert first.payload_sha256 == hashlib.sha256(
        canonical_json(first_payload).encode("utf-8")
    ).hexdigest()
    assert first.first_observed_at == "2026-08-05T08:00:00.123456Z"
    with sqlite3.connect(store.path) as connection:
        stored = connection.execute(
            "SELECT payload_json, COUNT(*) FROM shadow_observations"
        ).fetchone()
    assert stored == (canonical_json(first_payload), 1)


def test_same_source_revision_with_different_facts_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_observation(_observation(payload={"stage_status": "running"}))

    with pytest.raises(ShadowStoreConflictError) as captured:
        store.record_observation(_observation(payload={"stage_status": "complete"}))

    assert captured.value.diagnostic.code == "SHADOW_OBSERVATION_CONFLICT"
    assert store.summary()["observations"] == 1
    outcome = store.try_record_observation(_observation(payload={"stage_status": "complete"}))
    assert not outcome.ok
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "SHADOW_OBSERVATION_CONFLICT"


def test_concurrent_identical_observations_converge_to_one_row(tmp_path: Path) -> None:
    store = _store(tmp_path, busy_timeout_ms=2_000)
    request = _observation()

    def record(index: int):
        return store.record_observation(
            request,
            observed_at=BASE_TIME + timedelta(seconds=index),
            recorded_at=BASE_TIME + timedelta(seconds=index),
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        records = list(pool.map(record, range(64)))

    assert {item.observation_id for item in records} == {records[0].observation_id}
    assert {item.sequence for item in records} == {1}
    assert store.summary()["observations"] == 1


def test_comparison_is_canonical_idempotent_and_definition_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observation = store.record_observation(_observation())
    first = store.record_comparison(
        _comparison(observation.observation_id, payload={"z": 2, "a": 1}),
        compared_at=BASE_TIME,
        recorded_at=BASE_TIME,
    )
    repeated = store.record_comparison(
        _comparison(observation.observation_id, payload={"a": 1, "z": 2}),
        compared_at=BASE_TIME + timedelta(hours=1),
    )
    other_definition = store.record_comparison(
        _comparison(
            observation.observation_id,
            definition_sha256="1" * 64,
            payload={"a": 1, "z": 2},
        )
    )

    assert repeated == first
    assert other_definition.comparison_id != first.comparison_id
    assert store.summary()["comparisons"] == 2


def test_same_comparison_identity_with_different_result_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observation = store.record_observation(_observation())
    store.record_comparison(_comparison(observation.observation_id, verdict="exact"))

    with pytest.raises(ShadowStoreConflictError) as captured:
        store.record_comparison(
            _comparison(
                observation.observation_id,
                verdict="mismatch",
                payload={"fields": [{"field": "model", "verdict": "mismatch"}]},
            )
        )

    assert captured.value.diagnostic.code == "SHADOW_COMPARISON_CONFLICT"


def test_comparison_requires_existing_observation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    missing = f"obs_{'1' * 64}"

    outcome = store.try_record_comparison(_comparison(missing))

    assert not outcome.ok
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "SHADOW_OBSERVATION_NOT_FOUND"
    assert store.summary()["comparisons"] == 0


def test_observation_and_comparison_tables_are_append_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observation = store.record_observation(_observation())
    comparison = store.record_comparison(_comparison(observation.observation_id))

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="observations are append-only"):
            connection.execute(
                "UPDATE shadow_observations SET capture_status='unstable' WHERE observation_id=?",
                (observation.observation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="observations are append-only"):
            connection.execute(
                "DELETE FROM shadow_observations WHERE observation_id=?",
                (observation.observation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="comparisons are append-only"):
            connection.execute(
                "UPDATE shadow_comparisons SET overall_verdict='unknown' WHERE comparison_id=?",
                (comparison.comparison_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="comparisons are append-only"):
            connection.execute(
                "DELETE FROM shadow_comparisons WHERE comparison_id=?",
                (comparison.comparison_id,),
            )
    assert dict(store.summary())["observations"] == 1
    assert dict(store.summary())["comparisons"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "rewrite the production Prompt"},
        {"nested": {"prompt_body": "secret instructions"}},
        {"nested": {"api_key": "sk-examplecredential123456"}},
        {"nested": {"access_tokens": ["opaque-credential"]}},
        {"artifact": {"path": "C:\\private\\artifact.tex"}},
        {"artifact": {"relative_uri": "/home/operator/private.tex"}},
        {"artifact": {"relative_uri": "\\\\server\\share\\private.tex"}},
        {"evidence": {"message": "raw blocker prose"}},
        {"response": "raw final response"},
        {"safe_field": "Authorization: Bearer abcdefghijklmnop"},
        {"safe_field": "password=do-not-store"},
        {"thread_id": "thread-raw-identifier"},
    ],
)
def test_sensitive_or_path_bearing_payload_is_rejected_before_sql(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    store = _store(tmp_path)
    marker = "do-not-store"

    outcome = store.try_record_observation(_observation(payload=payload))

    assert not outcome.ok
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "SHADOW_PAYLOAD_UNSAFE"
    assert store.summary()["observations"] == 0
    assert marker.encode() not in _database_bytes(store.path)


def test_safe_prompt_digest_token_metrics_and_relative_uri_digest_are_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = {
        "prompt_sha256": "1" * 64,
        "token_usage": {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": 25,
            "reasoning_output_tokens": 10,
            "total_tokens": 125,
        },
        "hard_token_limit": 4_000_000,
        "artifact": {
            "relative_uri": "artifacts/main.pdf",
            "content_sha256": "2" * 64,
        },
        "session_fingerprint": "3" * 64,
        "message_sha256": "4" * 64,
    }

    record = store.record_observation(_observation(payload=payload))

    assert record.to_dict()["payload"] == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"bad": float("nan")},
        {"bad": float("inf")},
        {1: "non-string-key"},
        {"bad": object()},
    ],
)
def test_noncanonical_json_payload_is_rejected(tmp_path: Path, payload: dict[object, object]) -> None:
    store = _store(tmp_path)

    with pytest.raises(ShadowPayloadError):
        store.record_observation(_observation(payload=payload))  # type: ignore[arg-type]

    assert store.summary()["observations"] == 0


def test_circular_and_overdeep_payloads_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    circular: dict[str, object] = {}
    circular["child"] = circular
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(20):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    with pytest.raises(ShadowPayloadError, match="circular"):
        store.record_observation(_observation(payload=circular))
    with pytest.raises(ShadowPayloadError, match="maximum JSON depth"):
        store.record_observation(_observation(payload=deep))


def test_subject_identifier_is_domain_hashed_and_never_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    subject = "unique-confidential-paper-sentinel-918273"

    record = store.record_observation(_observation(subject_id=subject))

    assert record.subject_id_sha256 != hashlib.sha256(subject.encode()).hexdigest()
    assert subject not in json.dumps(record.to_dict(), ensure_ascii=False)
    assert subject.encode("utf-8") not in _database_bytes(store.path)


def test_busy_database_returns_sanitized_fail_open_diagnostic(tmp_path: Path) -> None:
    store = _store(tmp_path, busy_timeout_ms=20)
    blocker = sqlite3.connect(store.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = datetime.now(timezone.utc)
        outcome = store.try_record_observation(_observation())
        elapsed = datetime.now(timezone.utc) - started
    finally:
        blocker.rollback()
        blocker.close()

    assert not outcome.ok
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "SHADOW_STORE_BUSY"
    assert outcome.diagnostic.retryable is True
    assert str(store.path) not in outcome.diagnostic.detail
    assert elapsed < timedelta(seconds=2)
    assert store.summary()["observations"] == 0


def test_strict_busy_error_is_catchable_by_caller(tmp_path: Path) -> None:
    store = _store(tmp_path, busy_timeout_ms=10)
    blocker = sqlite3.connect(store.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ShadowStoreBusyError) as captured:
            store.record_observation(_observation())
    finally:
        blocker.rollback()
        blocker.close()
    assert captured.value.diagnostic.retryable


def test_read_detects_coordinated_payload_and_digest_tampering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.record_observation(_observation(payload={"stage_status": "running"}))
    replacement = canonical_json({"stage_status": "complete"})
    replacement_sha256 = hashlib.sha256(replacement.encode()).hexdigest()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER shadow_observations_no_update")
        connection.execute(
            "UPDATE shadow_observations SET payload_json=?, payload_sha256=? WHERE observation_id=?",
            (replacement, replacement_sha256, record.observation_id),
        )

    with pytest.raises(ShadowStoreCorruptionError, match="identity digest"):
        store.get_observation(record.observation_id)


def test_list_pagination_and_subject_filter_are_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.record_observation(_observation(subject_id="paper-a", revision="1" * 64))
    second = store.record_observation(_observation(subject_id="paper-b", revision="2" * 64))
    third = store.record_observation(_observation(subject_id="paper-a", revision="3" * 64))
    for observation in (first, second, third):
        store.record_comparison(
            _comparison(
                observation.observation_id,
                definition_sha256=hashlib.sha256(observation.observation_id.encode()).hexdigest(),
            )
        )

    assert [item.sequence for item in store.list_observations(limit=2)] == [1, 2]
    assert [item.sequence for item in store.list_observations(after_sequence=2)] == [3]
    assert [item.sequence for item in store.list_observations(stage="writing", subject_id="paper-a")] == [1, 3]
    assert [item.sequence for item in store.list_comparisons(after_sequence=1)] == [2, 3]
    with pytest.raises(ShadowPayloadError, match="stage is required"):
        store.list_observations(subject_id="paper-a")
    with pytest.raises(ShadowPayloadError, match="limit"):
        store.list_observations(limit=0)
