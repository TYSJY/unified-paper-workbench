from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow import (
    CONDITION_LANGUAGE,
    ArtifactSpec,
    EdgeDefinition,
    EdgeType,
    JoinPolicy,
    NodeDefinition,
    NodeRunStatus,
    NodeType,
    RetryPolicy,
    SessionPolicy,
    ValidationMode,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowRunStatus,
    WorkflowValidationError,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.store import (
    ArtifactRecord,
    LeaseLostError,
    WorkflowConflictError,
    WorkflowStore,
    WorkflowStoreError,
)


BASE_TIME = datetime(2026, 8, 5, 4, 0, 0, 123456, tzinfo=timezone.utc)


def _definition(
    *,
    workflow_id: str = "test.single-node",
    max_attempts: int = 3,
    retry_policy: RetryPolicy | None = None,
    outputs: tuple[ArtifactSpec, ...] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        name="Single deterministic node",
        nodes=(
            NodeDefinition(
                node_id="work",
                name="Work",
                node_type=NodeType.DETERMINISTIC,
                display_order=10,
                retry_policy=retry_policy or RetryPolicy(max_attempts=max_attempts),
                session_policy=SessionPolicy(
                    session_group="worker-session",
                    reuse_within_run=True,
                    reuse_on_retry=True,
                ),
                outputs=outputs if outputs is not None else (ArtifactSpec("result", media_type="application/json"),),
                implementation_ref="tests.work",
            ),
        ),
        edges=(),
    )


def _version(
    definition: WorkflowDefinition,
    *,
    status: WorkflowVersionStatus = WorkflowVersionStatus.PUBLISHED,
    mode: ValidationMode = ValidationMode.LINEAR,
) -> WorkflowVersion:
    return WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=BASE_TIME.isoformat(),
        status=status,
        mode=mode,
    )


def _binding(definition: WorkflowDefinition) -> WorkflowRuntimeBinding:
    return WorkflowRuntimeBinding(
        binding_id="test-binding",
        definition_sha256=definition.sha256(),
        resources=(),
    )


def _store(tmp_path: Path) -> WorkflowStore:
    store = WorkflowStore(tmp_path / "orchestrator.sqlite3")
    store.initialize()
    return store


def _create_run(
    store: WorkflowStore,
    *,
    run_id: str = "run-main",
    max_attempts: int = 3,
    retry_policy: RetryPolicy | None = None,
    outputs: tuple[ArtifactSpec, ...] | None = None,
    now: datetime = BASE_TIME,
) -> tuple[WorkflowVersion, WorkflowRuntimeBinding, dict[str, object], dict[str, object]]:
    definition = _definition(
        max_attempts=max_attempts,
        retry_policy=retry_policy,
        outputs=outputs,
    )
    version = _version(definition)
    binding = _binding(definition)
    run = store.create_run(
        version,
        binding,
        run_id=run_id,
        idempotency_key=f"create-{run_id}",
        inputs={"seed": run_id},
        now=now,
    )
    node = store.list_node_runs(run_id)[0]
    return version, binding, run, node


def _ready_and_claim(
    store: WorkflowStore,
    node_run_id: str,
    *,
    now: datetime = BASE_TIME,
    lease_seconds: int = 30,
):
    store.mark_node_ready(node_run_id, now=now)
    grant = store.claim_node(
        node_run_id,
        "worker-1",
        lease_seconds=lease_seconds,
        now=now,
    )
    assert grant is not None
    return grant


def _result_artifact(artifact_id: str = "result") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        uri=f"artifacts/{artifact_id}.json",
        sha256=hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
        media_type="application/json",
    )


@pytest.mark.parametrize(
    "uri",
    ["artifact://result", "C:/outside.txt", "folder\\result.txt", "../result.txt", "file.txt:hidden"],
)
def test_artifact_record_rejects_noncanonical_or_stream_uri(uri: str) -> None:
    with pytest.raises(ValueError, match="canonical portable relative path"):
        ArtifactRecord(
            artifact_id="result",
            uri=uri,
            sha256="a" * 64,
        ).to_dict()


def _branching_definition() -> WorkflowDefinition:
    nodes = (
        NodeDefinition(
            "start",
            "Start",
            NodeType.DETERMINISTIC,
            10,
            implementation_ref="tests.start",
        ),
        NodeDefinition(
            "left",
            "Left",
            NodeType.DETERMINISTIC,
            20,
            implementation_ref="tests.left",
        ),
        NodeDefinition(
            "right",
            "Right",
            NodeType.DETERMINISTIC,
            30,
            implementation_ref="tests.right",
        ),
        NodeDefinition(
            "finish",
            "Finish",
            NodeType.DETERMINISTIC,
            40,
            join_policy=JoinPolicy.ANY,
            implementation_ref="tests.finish",
        ),
    )
    return WorkflowDefinition(
        workflow_id="test.branching",
        name="Graph-only workflow",
        nodes=nodes,
        edges=(
            EdgeDefinition(
                "choose-left",
                "start",
                "left",
                EdgeType.CONDITION,
                {"==": [{"var": "input.route"}, "left"]},
                CONDITION_LANGUAGE,
            ),
            EdgeDefinition(
                "choose-right",
                "start",
                "right",
                EdgeType.CONDITION,
                {"==": [{"var": "input.route"}, "right"]},
                CONDITION_LANGUAGE,
            ),
            EdgeDefinition("left-finish", "left", "finish"),
            EdgeDefinition("right-finish", "right", "finish"),
        ),
    )


def test_published_workflows_are_strictly_gated_to_linear_definitions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    definition = _branching_definition()
    draft = _version(
        definition,
        status=WorkflowVersionStatus.DRAFT,
        mode=ValidationMode.GRAPH,
    )
    store.register_version(draft)

    with pytest.raises(WorkflowValidationError, match="linear"):
        store.set_version_status(
            draft.version_id,
            WorkflowVersionStatus.PUBLISHED,
            expected_status=WorkflowVersionStatus.DRAFT,
        )

    published = replace(draft, status=WorkflowVersionStatus.PUBLISHED)
    with pytest.raises(WorkflowValidationError, match="linear"):
        store.create_run(published, _binding(definition), now=BASE_TIME)


def test_version_status_is_cas_monotonic_and_digest_tampering_is_detected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    definition = _definition()
    draft = _version(definition, status=WorkflowVersionStatus.DRAFT)
    registered = store.register_version(draft)
    assert registered["is_builtin"] is False
    assert registered["is_readonly"] is False

    published = store.set_version_status(
        draft.version_id,
        WorkflowVersionStatus.PUBLISHED,
        expected_status=WorkflowVersionStatus.DRAFT,
    )
    assert published["version"].status is WorkflowVersionStatus.PUBLISHED
    deprecated = store.set_version_status(
        draft.version_id,
        WorkflowVersionStatus.DEPRECATED,
        expected_status=WorkflowVersionStatus.PUBLISHED,
    )
    assert deprecated["version"].status is WorkflowVersionStatus.DEPRECATED
    with pytest.raises(WorkflowConflictError):
        store.set_version_status(
            draft.version_id,
            WorkflowVersionStatus.PUBLISHED,
            expected_status=WorkflowVersionStatus.DEPRECATED,
        )

    # The trigger is the first line of defence. Dropping it in this disposable
    # database simulates offline corruption and verifies the read-time digest
    # check remains an independent defence.
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER workflow_versions_definition_immutable")
        connection.execute(
            "UPDATE workflow_versions SET definition_sha256=? WHERE version_id=?",
            ("0" * 64, draft.version_id),
        )
    with pytest.raises(WorkflowStoreError, match="digest"):
        store.get_version(draft.version_id)


def test_create_run_is_idempotent_and_rejects_key_reuse_with_new_content(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    definition = _definition()
    version = _version(definition)
    binding = _binding(definition)
    first = store.create_run(
        version,
        binding,
        run_id="run-first",
        idempotency_key="same-request",
        inputs={"topic": "same"},
        now=BASE_TIME,
    )
    duplicate = store.create_run(
        version,
        binding,
        run_id="run-discarded",
        idempotency_key="same-request",
        inputs={"topic": "same"},
        now=BASE_TIME + timedelta(seconds=1),
    )

    assert duplicate["run_id"] == first["run_id"] == "run-first"
    assert len(store.list_node_runs("run-first")) == 1
    assert [event["event_type"] for event in store.list_events("run-first")].count("run.created") == 1
    with pytest.raises(WorkflowConflictError, match="idempotency"):
        store.create_run(
            version,
            binding,
            idempotency_key="same-request",
            inputs={"topic": "different"},
            now=BASE_TIME + timedelta(seconds=2),
        )


def test_concurrent_claim_has_exactly_one_winner_and_never_persists_raw_token(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    store.mark_node_ready(str(node["node_run_id"]), now=BASE_TIME)

    def claim(index: int):
        return store.claim_node(
            str(node["node_run_id"]),
            f"worker-{index}",
            lease_seconds=30,
            now=BASE_TIME,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        grants = list(pool.map(claim, range(32)))
    winners = [grant for grant in grants if grant is not None]

    assert len(winners) == 1
    grant = winners[0]
    with sqlite3.connect(store.path) as connection:
        stored = connection.execute(
            "SELECT fencing_token_sha256 FROM workflow_leases WHERE lease_id=?",
            (grant.lease_id,),
        ).fetchone()[0]
        serialized_events = "".join(
            row[0]
            for row in connection.execute(
                "SELECT payload_json FROM workflow_events WHERE workflow_run_id=?",
                (run["run_id"],),
            ).fetchall()
        )
    assert stored == hashlib.sha256(grant.fencing_token.encode("utf-8")).hexdigest()
    assert stored != grant.fencing_token
    assert grant.fencing_token not in serialized_events


@pytest.mark.parametrize("lease_seconds", [True, 0, -1, 86_401, 1.5])
def test_lease_parameters_are_strict_and_naive_operation_times_are_rejected(
    tmp_path: Path,
    lease_seconds,
) -> None:
    store = _store(tmp_path)
    _, _, _, node = _create_run(store)
    store.mark_node_ready(str(node["node_run_id"]), now=BASE_TIME)

    with pytest.raises(ValueError, match="lease_seconds"):
        store.claim_node(
            str(node["node_run_id"]),
            "worker",
            lease_seconds=lease_seconds,
            now=BASE_TIME,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.claim_node(
            str(node["node_run_id"]),
            "worker",
            now=datetime(2026, 8, 5, 4, 0, 0),
        )


def test_heartbeat_extends_a_live_lease_but_bad_tokens_and_expiry_are_fenced(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, _, node = _create_run(store)
    grant = _ready_and_claim(
        store,
        str(node["node_run_id"]),
        lease_seconds=10,
    )

    forged = replace(grant, fencing_token=f"{grant.fencing_token}-forged")
    with pytest.raises(LeaseLostError):
        store.heartbeat(forged, now=BASE_TIME + timedelta(seconds=1))
    with pytest.raises(LeaseLostError):
        store.heartbeat(
            replace(grant, generation=grant.generation + 1),
            now=BASE_TIME + timedelta(seconds=1),
        )

    refreshed = store.heartbeat(
        grant,
        lease_seconds=20,
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert refreshed.expires_at.endswith("Z")
    with pytest.raises(LeaseLostError):
        store.complete_node(
            refreshed,
            (_result_artifact(),),
            now=BASE_TIME + timedelta(seconds=25),
        )


def test_expired_lease_creates_a_new_attempt_and_permanently_fences_old_grant(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store, max_attempts=3)
    grant = _ready_and_claim(
        store,
        str(node["node_run_id"]),
        lease_seconds=5,
    )

    recovered = store.recover_expired_leases(now=BASE_TIME + timedelta(seconds=6))

    assert recovered == [
        {
            "run_id": run["run_id"],
            "expired_node_run_id": node["node_run_id"],
            "action": "retried",
            "new_node_run_id": recovered[0]["new_node_run_id"],
        }
    ]
    attempts = store.list_node_runs(str(run["run_id"]))
    assert [(item["attempt"], item["status"]) for item in attempts] == [
        (1, NodeRunStatus.RETRYABLE_FAILURE.value),
        (2, NodeRunStatus.READY.value),
    ]
    with pytest.raises(LeaseLostError):
        store.complete_node(
            grant,
            (_result_artifact(),),
            now=BASE_TIME + timedelta(seconds=7),
        )
    second = store.claim_node(
        str(recovered[0]["new_node_run_id"]),
        "worker-2",
        now=BASE_TIME + timedelta(seconds=7),
    )
    assert second is not None
    assert second.generation > grant.generation


def test_expiry_at_max_attempt_marks_node_and_run_fatal_without_new_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store, max_attempts=1)
    _ready_and_claim(store, str(node["node_run_id"]), lease_seconds=2)

    recovered = store.recover_expired_leases(now=BASE_TIME + timedelta(seconds=3))

    assert recovered[0]["action"] == "failed"
    assert recovered[0]["new_node_run_id"] == ""
    attempts = store.list_node_runs(str(run["run_id"]))
    assert len(attempts) == 1
    assert attempts[0]["status"] == NodeRunStatus.FATAL_FAILURE.value
    assert store.get_run(str(run["run_id"]))["status"] == "fatal-failure"


def test_explicit_retry_appends_attempt_without_mutating_failure_history(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.fail_node(
        grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code="transient",
        error_message="retry later",
        now=BASE_TIME + timedelta(seconds=1),
    )
    frozen_attempt = store.get_node_run(str(node["node_run_id"]))

    retry = store.retry_node(
        str(node["node_run_id"]),
        reason="operator-approved",
        now=BASE_TIME + timedelta(seconds=2),
    )

    assert retry["attempt"] == 2
    assert retry["status"] == NodeRunStatus.READY.value
    assert store.get_node_run(str(node["node_run_id"])) == frozen_attempt
    assert [item["attempt"] for item in store.list_node_runs(str(run["run_id"]))] == [1, 2]


def test_artifact_registration_and_node_completion_roll_back_as_one_transaction(
    tmp_path: Path,
) -> None:
    outputs = (
        ArtifactSpec("first", media_type="application/json"),
        ArtifactSpec("second", media_type="application/json"),
    )
    store = _store(tmp_path)
    _, _, run, node = _create_run(store, outputs=outputs)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    first = _result_artifact("first")
    second = _result_artifact("second")
    with store.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_second_artifact BEFORE INSERT ON workflow_artifacts "
            "WHEN NEW.artifact_id='second' BEGIN "
            "SELECT RAISE(ABORT, 'injected artifact write failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected artifact"):
        store.complete_node(
            grant,
            (first, second),
            now=BASE_TIME + timedelta(seconds=1),
        )

    assert store.list_artifacts(str(run["run_id"])) == []
    assert store.get_node_run(str(node["node_run_id"]))["status"] == "running"
    assert store.summary()["active_lease_count"] == 1
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER fail_second_artifact")
    completed = store.complete_node(
        grant,
        (first, second),
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert completed["status"] == "completed"
    assert {item["artifact_id"] for item in store.list_artifacts(str(run["run_id"]))} == {
        "first",
        "second",
    }


def test_cancellation_is_cooperative_with_active_lease_and_immediate_when_idle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, active_run, active_node = _create_run(store, run_id="run-active")
    grant = _ready_and_claim(store, str(active_node["node_run_id"]))

    requested = store.request_cancel(
        str(active_run["run_id"]),
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert requested["cancel_requested"] is True
    assert requested["status"] == "running"
    assert store.cancellation_requested(str(active_run["run_id"])) is True
    with pytest.raises(WorkflowConflictError, match="cancellation"):
        store.complete_node(
            grant,
            (_result_artifact(),),
            now=BASE_TIME + timedelta(seconds=2),
        )
    store.release_lease(
        grant,
        node_status=NodeRunStatus.CANCELLED,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert store.get_run(str(active_run["run_id"]))["status"] == "cancelled"

    _, _, idle_run, _ = _create_run(store, run_id="run-idle")
    cancelled = store.request_cancel(
        str(idle_run["run_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert cancelled["status"] == "cancelled"
    assert {item["status"] for item in store.list_node_runs(str(idle_run["run_id"]))} == {"cancelled"}


def test_pause_blocks_new_claims_and_resume_preserves_active_work(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    store.mark_node_ready(str(node["node_run_id"]), now=BASE_TIME)

    assert store.pause_run(str(run["run_id"]), now=BASE_TIME)["status"] == "paused"
    assert (
        store.claim_node(
            str(node["node_run_id"]),
            "worker",
            now=BASE_TIME,
        )
        is None
    )
    assert store.resume_run(str(run["run_id"]), now=BASE_TIME)["status"] == "queued"
    grant = store.claim_node(
        str(node["node_run_id"]),
        "worker",
        now=BASE_TIME,
    )
    assert grant is not None
    assert store.pause_run(str(run["run_id"]), now=BASE_TIME)["status"] == "paused"
    assert store.heartbeat(grant, now=BASE_TIME + timedelta(seconds=1)).generation == grant.generation
    assert store.resume_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=1))["status"] == "running"


def test_pause_is_preserved_when_active_node_schedules_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(
        store,
        retry_policy=RetryPolicy(
            max_attempts=2,
            retryable_error_codes=("transient",),
        ),
    )
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.pause_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=1))

    store.fail_node(
        grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code="transient",
        now=BASE_TIME + timedelta(seconds=2),
    )
    retry = store.schedule_retry(
        str(node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )

    assert retry is not None
    assert retry["status"] == NodeRunStatus.READY.value
    assert store.get_run(str(run["run_id"]))["status"] == WorkflowRunStatus.PAUSED.value
    assert (
        store.claim_node(
            str(retry["node_run_id"]),
            "worker-before-resume",
            now=BASE_TIME + timedelta(seconds=4),
        )
        is None
    )
    assert store.resume_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=5))["status"] == "queued"
    assert (
        store.claim_node(
            str(retry["node_run_id"]),
            "worker-after-resume",
            now=BASE_TIME + timedelta(seconds=6),
        )
        is not None
    )


def test_heartbeat_expiry_is_monotonic_for_out_of_order_callers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, _, node = _create_run(store)
    grant = _ready_and_claim(store, str(node["node_run_id"]), lease_seconds=30)

    newer = store.heartbeat(
        grant,
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=10),
    )
    older = store.heartbeat(
        grant,
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=5),
    )

    assert older.expires_at == newer.expires_at
    assert older.expires_at.endswith("40.123456Z")


def test_resume_paused_backoff_restores_retryable_failure_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(
        store,
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_delay_seconds=60,
            retryable_error_codes=("transient",),
        ),
    )
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.pause_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=1))
    store.fail_node(
        grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code="transient",
        now=BASE_TIME + timedelta(seconds=2),
    )
    retry = store.schedule_retry(
        str(node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert retry is not None and retry["status"] == "retry-wait"

    resumed = store.resume_run(
        str(run["run_id"]),
        now=BASE_TIME + timedelta(seconds=4),
    )
    assert resumed["status"] == "retryable-failure"
    assert store.activate_due_retries(
        run_id=str(run["run_id"]),
        now=BASE_TIME + timedelta(seconds=64),
    )[0]["status"] == "ready"


def test_sessions_are_concurrently_unique_rotatable_and_fenced_across_runs(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    version, binding, run, node = _create_run(store, run_id="run-session-a")

    def acquire(_index: int):
        return store.get_or_create_session(
            str(run["run_id"]),
            "worker-session",
            "codex",
            now=BASE_TIME,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        sessions = list(pool.map(acquire, range(32)))
    assert len({item["session_record_id"] for item in sessions}) == 1
    first = sessions[0]
    second = store.get_or_create_session(
        str(run["run_id"]),
        "worker-session",
        "codex",
        force_new=True,
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert second["generation"] == first["generation"] + 1
    assert [item["state"] for item in store.list_sessions(str(run["run_id"]))] == [
        "rotated",
        "active",
    ]
    grant = _ready_and_claim(
        store,
        str(node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=2),
    )
    attached = store.attach_session(
        grant,
        str(second["session_record_id"]),
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert attached["session_id"] == second["session_record_id"]
    bound = store.bind_external_session(
        grant,
        str(second["session_record_id"]),
        "external-thread-1",
        expected_generation=int(second["generation"]),
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert bound["external_session_id"] == "external-thread-1"

    other_run = store.create_run(
        version,
        binding,
        run_id="run-session-b",
        idempotency_key="create-run-session-b",
        now=BASE_TIME + timedelta(seconds=2),
    )
    other_session = store.get_or_create_session(
        str(other_run["run_id"]),
        "worker-session",
        "codex",
        now=BASE_TIME + timedelta(seconds=2),
    )
    other_node = store.latest_node_runs(str(other_run["run_id"]))[0]
    store.mark_node_ready(
        str(other_node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )
    other_grant = store.claim_ready_node(
        str(other_run["run_id"]),
        "other-worker",
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert other_grant is not None
    store.attach_session(
        other_grant,
        str(other_session["session_record_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )
    with pytest.raises(WorkflowConflictError, match="another workflow"):
        store.bind_external_session(
            other_grant,
            str(other_session["session_record_id"]),
            "external-thread-1",
            expected_generation=int(other_session["generation"]),
            now=BASE_TIME + timedelta(seconds=3),
        )

    with pytest.raises(WorkflowConflictError, match="does not belong"):
        store.attach_session(
            grant,
            str(other_session["session_record_id"]),
            now=BASE_TIME + timedelta(seconds=5),
        )


def test_events_are_gap_free_and_paginate_by_run_local_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    store.mark_node_ready(str(node["node_run_id"]), now=BASE_TIME)
    store.pause_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=1))
    store.resume_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=2))

    all_events = store.list_events(str(run["run_id"]))
    first_page = store.list_events(str(run["run_id"]), limit=2)
    second_page = store.list_events(
        str(run["run_id"]),
        after_sequence=first_page[-1]["sequence"],
    )

    assert first_page + second_page == all_events
    assert [event["sequence"] for event in all_events] == list(range(1, len(all_events) + 1))
    assert {event["run_id"] for event in all_events} == {run["run_id"]}


def test_complete_run_requires_all_latest_attempts_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    with pytest.raises(WorkflowConflictError, match="lease is active"):
        store.complete_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=1))
    store.complete_node(
        grant,
        (_result_artifact(),),
        now=BASE_TIME + timedelta(seconds=1),
    )

    completed = store.complete_run(
        str(run["run_id"]),
        now=BASE_TIME + timedelta(seconds=2),
    )
    duplicate = store.complete_run(
        str(run["run_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )

    assert completed["status"] == duplicate["status"] == "completed"
    assert completed["finished_at"].endswith("Z")
    assert [event["event_type"] for event in store.list_events(str(run["run_id"]))].count("run.completed") == 1


def test_policy_retry_waits_for_backoff_deadline_before_becoming_ready(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=10,
        backoff_multiplier=2,
        max_delay_seconds=60,
        retryable_error_codes=("transient",),
    )
    _, _, run, node = _create_run(store, retry_policy=policy)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.fail_node(
        grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code="transient",
        now=BASE_TIME + timedelta(seconds=1),
    )

    retry = store.schedule_retry(
        str(node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=2),
    )

    assert retry is not None
    assert retry["attempt"] == 2
    assert retry["status"] == NodeRunStatus.RETRY_WAIT.value
    assert retry["ready_at"] == "2026-08-05T04:00:12.123456Z"
    assert store.get_run(str(run["run_id"]))["status"] == "retryable-failure"
    assert (
        store.activate_due_retries(
            run_id=str(run["run_id"]),
            now=BASE_TIME + timedelta(seconds=11, microseconds=999_999),
        )
        == []
    )
    assert store.get_node_run(str(retry["node_run_id"]))["status"] == "retry-wait"

    activated = store.activate_due_retries(
        run_id=str(run["run_id"]),
        now=BASE_TIME + timedelta(seconds=12),
    )
    assert [item["node_run_id"] for item in activated] == [retry["node_run_id"]]
    assert activated[0]["status"] == NodeRunStatus.READY.value
    assert activated[0]["ready_at"] == ""
    assert store.get_run(str(run["run_id"]))["status"] == "queued"


@pytest.mark.parametrize(
    ("run_id", "policy", "error_code"),
    [
        (
            "run-disallowed-code",
            RetryPolicy(max_attempts=3, retryable_error_codes=("transient",)),
            "permanent",
        ),
        (
            "run-attempt-limit",
            RetryPolicy(max_attempts=1, retryable_error_codes=("transient",)),
            "transient",
        ),
    ],
)
def test_policy_retry_honours_error_allowlist_and_attempt_limit(
    tmp_path: Path,
    run_id: str,
    policy: RetryPolicy,
    error_code: str,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store, run_id=run_id, retry_policy=policy)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.fail_node(
        grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code=error_code,
        now=BASE_TIME + timedelta(seconds=1),
    )

    assert (
        store.schedule_retry(
            str(node["node_run_id"]),
            now=BASE_TIME + timedelta(seconds=2),
        )
        is None
    )
    assert store.get_run(str(run["run_id"]))["status"] == "fatal-failure"
    assert len(store.list_node_runs(str(run["run_id"]))) == 1
    assert store.list_events(str(run["run_id"]))[-1]["event_type"] == "run.retry_exhausted"


@pytest.mark.parametrize(
    ("waiting_status", "run_status"),
    [
        (NodeRunStatus.WAITING_HUMAN, "waiting-human"),
        (NodeRunStatus.WAITING_APPROVAL, "waiting-approval"),
    ],
)
def test_waiting_transition_releases_lease_atomically_and_can_be_resumed(
    tmp_path: Path,
    waiting_status: NodeRunStatus,
    run_status: str,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    grant = _ready_and_claim(store, str(node["node_run_id"]))

    waiting = store.wait_node(
        grant,
        status=waiting_status,
        reason="operator input required",
        details={"ticket": "approval-1"},
        now=BASE_TIME + timedelta(seconds=1),
    )

    assert waiting["status"] == waiting_status.value
    assert store.get_run(str(run["run_id"]))["status"] == run_status
    assert store.summary()["active_lease_count"] == 0
    with pytest.raises(LeaseLostError):
        store.heartbeat(grant, now=BASE_TIME + timedelta(seconds=2))
    with pytest.raises(LeaseLostError):
        store.complete_node(
            grant,
            (_result_artifact(),),
            now=BASE_TIME + timedelta(seconds=2),
        )

    resumed = store.resume_waiting_node(
        str(node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert resumed["status"] == NodeRunStatus.READY.value
    assert store.get_run(str(run["run_id"]))["status"] == "queued"
    replacement = store.claim_node(
        str(node["node_run_id"]),
        "worker-2",
        now=BASE_TIME + timedelta(seconds=4),
    )
    assert replacement is not None
    assert replacement.generation > grant.generation


def test_cancelled_active_run_rejects_heartbeat_and_session_attach_until_release(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    session = store.get_or_create_session(
        str(run["run_id"]),
        "worker-session",
        "codex",
        now=BASE_TIME,
    )
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.request_cancel(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=1))

    with pytest.raises(WorkflowConflictError, match="cannot be extended"):
        store.heartbeat(grant, now=BASE_TIME + timedelta(seconds=2))
    with pytest.raises(WorkflowConflictError, match="cannot attach"):
        store.attach_session(
            grant,
            str(session["session_record_id"]),
            now=BASE_TIME + timedelta(seconds=2),
        )
    assert store.get_run(str(run["run_id"]))["status"] == "running"

    store.release_lease(
        grant,
        node_status=NodeRunStatus.CANCELLED,
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert store.get_run(str(run["run_id"]))["status"] == "cancelled"
    assert store.summary()["active_lease_count"] == 0


@pytest.mark.parametrize(
    "failure_status",
    [
        NodeRunStatus.RETRYABLE_FAILURE,
        NodeRunStatus.FATAL_FAILURE,
        NodeRunStatus.BLOCKED,
    ],
)
def test_failed_or_blocked_run_cannot_be_paused(
    tmp_path: Path,
    failure_status: NodeRunStatus,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.fail_node(
        grant,
        status=failure_status,
        error_code="test-failure",
        now=BASE_TIME + timedelta(seconds=1),
    )

    with pytest.raises(WorkflowConflictError, match="active or waiting"):
        store.pause_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=2))


def test_terminal_run_cannot_create_or_bind_sessions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    session = store.get_or_create_session(
        str(run["run_id"]),
        "worker-session",
        "codex",
        now=BASE_TIME,
    )
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.complete_node(
        grant,
        (_result_artifact(),),
        now=BASE_TIME + timedelta(seconds=1),
    )
    store.complete_run(str(run["run_id"]), now=BASE_TIME + timedelta(seconds=2))

    with pytest.raises(WorkflowConflictError, match="terminal"):
        store.get_or_create_session(
            str(run["run_id"]),
            "another-group",
            "codex",
            now=BASE_TIME + timedelta(seconds=3),
        )
    with pytest.raises(LeaseLostError, match="stale|expired|fenced"):
        store.bind_external_session(
            grant,
            str(session["session_record_id"]),
            "late-external-thread",
            expected_generation=int(session["generation"]),
            now=BASE_TIME + timedelta(seconds=3),
        )


def test_session_attach_rejects_group_that_differs_from_frozen_node_contract(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    wrong_group = store.get_or_create_session(
        str(run["run_id"]),
        "wrong-session-group",
        "codex",
        now=BASE_TIME,
    )
    grant = _ready_and_claim(store, str(node["node_run_id"]))

    with pytest.raises(WorkflowConflictError, match="group does not match"):
        store.attach_session(
            grant,
            str(wrong_group["session_record_id"]),
            now=BASE_TIME + timedelta(seconds=1),
        )
    assert store.get_node_run(str(node["node_run_id"]))["session_id"] == ""


def test_retry_drops_rotated_session_and_accepts_current_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    first_session = store.get_or_create_session(
        str(run["run_id"]),
        "worker-session",
        "codex",
        now=BASE_TIME,
    )
    first_grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.attach_session(
        first_grant,
        str(first_session["session_record_id"]),
        now=BASE_TIME + timedelta(seconds=1),
    )
    store.fail_node(
        first_grant,
        status=NodeRunStatus.RETRYABLE_FAILURE,
        error_code="transient",
        now=BASE_TIME + timedelta(seconds=2),
    )
    current_session = store.get_or_create_session(
        str(run["run_id"]),
        "worker-session",
        "codex",
        force_new=True,
        now=BASE_TIME + timedelta(seconds=3),
    )

    retry = store.retry_node(
        str(node["node_run_id"]),
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert retry["session_id"] == ""
    assert store.list_sessions(str(run["run_id"]))[0]["state"] == "rotated"
    retry_grant = store.claim_node(
        str(retry["node_run_id"]),
        "worker-2",
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert retry_grant is not None
    attached = store.attach_session(
        retry_grant,
        str(current_session["session_record_id"]),
        now=BASE_TIME + timedelta(seconds=6),
    )
    assert attached["session_id"] == current_session["session_record_id"]


def test_session_rotation_is_fenced_while_active_node_references_generation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    session = store.get_or_create_session(
        str(run["run_id"]),
        "worker-session",
        "codex",
        now=BASE_TIME,
    )
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.attach_session(
        grant,
        str(session["session_record_id"]),
        now=BASE_TIME + timedelta(seconds=1),
    )

    with pytest.raises(WorkflowConflictError, match="active node still references"):
        store.get_or_create_session(
            str(run["run_id"]),
            "worker-session",
            "codex",
            force_new=True,
            now=BASE_TIME + timedelta(seconds=2),
        )

    store.release_lease(grant, now=BASE_TIME + timedelta(seconds=3))
    with pytest.raises(WorkflowConflictError, match="active node still references"):
        store.get_or_create_session(
            str(run["run_id"]),
            "worker-session",
            "codex",
            force_new=True,
            now=BASE_TIME + timedelta(seconds=4),
        )
    replacement = store.claim_node(
        str(node["node_run_id"]),
        "worker-2",
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert replacement is not None
    store.complete_node(
        replacement,
        (_result_artifact(),),
        now=BASE_TIME + timedelta(seconds=6),
    )
    with pytest.raises(WorkflowConflictError, match="terminal"):
        store.get_or_create_session(
            str(run["run_id"]),
            "worker-session",
            "codex",
            force_new=True,
            now=BASE_TIME + timedelta(seconds=7),
        )


def test_waiting_node_completion_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, _, run, node = _create_run(store)
    grant = _ready_and_claim(store, str(node["node_run_id"]))
    store.wait_node(
        grant,
        status=NodeRunStatus.WAITING_HUMAN,
        reason="operator artifact required",
        now=BASE_TIME + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="missing required"):
        store.complete_waiting_node(
            str(node["node_run_id"]),
            expected_status=NodeRunStatus.WAITING_HUMAN,
            now=BASE_TIME + timedelta(seconds=2),
        )
    assert store.get_node_run(str(node["node_run_id"]))["status"] == "waiting-human"
    assert store.list_artifacts(str(run["run_id"])) == []

    artifact = _result_artifact()
    completed = store.complete_waiting_node(
        str(node["node_run_id"]),
        (artifact,),
        expected_status=NodeRunStatus.WAITING_HUMAN,
        now=BASE_TIME + timedelta(seconds=3),
    )
    duplicate = store.complete_waiting_node(
        str(node["node_run_id"]),
        (artifact,),
        expected_status=NodeRunStatus.WAITING_HUMAN,
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert completed["status"] == duplicate["status"] == "completed"
    assert len(store.list_artifacts(str(run["run_id"]))) == 1
    assert store.get_run(str(run["run_id"]))["status"] == "completed"


def test_database_triggers_freeze_snapshots_statuses_and_session_run_scope(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    version, binding, first_run, first_node = _create_run(store, run_id="run-trigger-a")
    second_run = store.create_run(
        version,
        binding,
        run_id="run-trigger-b",
        idempotency_key="create-run-trigger-b",
        now=BASE_TIME,
    )
    foreign_session = store.get_or_create_session(
        str(second_run["run_id"]),
        "worker-session",
        "codex",
        now=BASE_TIME,
    )

    statements = (
        ("UPDATE workflow_runs SET owner='tampered' WHERE run_id=?", (first_run["run_id"],)),
        ("UPDATE workflow_runs SET status='invented' WHERE run_id=?", (first_run["run_id"],)),
        ("UPDATE node_runs SET display_order=999 WHERE node_run_id=?", (first_node["node_run_id"],)),
        ("UPDATE node_runs SET status='invented' WHERE node_run_id=?", (first_node["node_run_id"],)),
        (
            "UPDATE node_runs SET session_id=? WHERE node_run_id=?",
            (foreign_session["session_record_id"], first_node["node_run_id"]),
        ),
    )
    for statement, parameters in statements:
        with sqlite3.connect(store.path) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
