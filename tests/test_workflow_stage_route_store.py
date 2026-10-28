from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest

from unified_paper_orchestrator.workflow.stage_route_store import (
    STAGE_ROUTE_DATABASE_NAME,
    StageRouteStore,
    StageRouteStoreCorruptionError,
    StageRouteStoreError,
    stage_route_database_path,
)
from unified_paper_orchestrator.workflow.stage_routing import (
    CRITICAL_PARITY_FIELDS,
    CandidateDescriptor,
    ParityWindow,
    RouteOwner,
    Stage,
    StageRouteRequest,
    StageRoutingConflictError,
    StageRoutingController,
    StageRoutingValidationError,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> StageRouteStore:
    store = StageRouteStore(stage_route_database_path(tmp_path), busy_timeout_ms=10_000)
    store.initialize()
    return store


def _candidate() -> CandidateDescriptor:
    return CandidateDescriptor(
        stage=Stage.JOURNAL_AUTHOR,
        workflow_version_id="builtin.sci.journal-author@ready",
        workflow_id="builtin.sci.journal-author",
        definition_sha256="a" * 64,
        binding_sha256="b" * 64,
        output_contract_sha256="c" * 64,
        published=True,
        builtin=True,
        readonly=True,
        workflow_id_matches_stage=True,
        native_dispatch_ready=True,
        requires_runtime_freeze=False,
        execution_profile_frozen=True,
        runtime_files_verified=True,
        prompt_seals_verified=True,
        executor_available=True,
        output_validators_available=True,
        compatibility_projection_available=True,
        has_legacy_stage_nodes=False,
        production_writer=True,
    )


def _parity(candidate: CandidateDescriptor, head: int) -> ParityWindow:
    return ParityWindow(
        stage=candidate.stage,
        definition_sha256=candidate.definition_sha256,
        binding_sha256=candidate.binding_sha256,
        comparison_head_sequence=head,
        consecutive_passes=3,
        distinct_subjects=3,
        critical_verdicts={name: "exact" for name in CRITICAL_PARITY_FIELDS},
        newest_observed_at=NOW.isoformat(),
        mismatch_count=0,
    )


def _active_controller(store: StageRouteStore) -> tuple[StageRoutingController, object]:
    controller = StageRoutingController(store, allow_candidate=True, clock=lambda: NOW)
    candidate = _candidate()
    controller.enable_shadow(Stage.JOURNAL_AUTHOR, expected_revision=0)
    first = controller.preflight(candidate, _parity(candidate, 1))
    controller.promote_canary(Stage.JOURNAL_AUTHOR, first, expected_revision=1)
    second = controller.preflight(candidate, _parity(candidate, 2))
    controller.promote_active(Stage.JOURNAL_AUTHOR, second, expected_revision=2)
    return controller, controller.preflight(candidate, _parity(candidate, 2))


def test_constructor_is_zero_io_and_location_is_strict(tmp_path: Path) -> None:
    workspace = tmp_path / "not-created"
    path = stage_route_database_path(workspace)
    store = StageRouteStore(path)
    StageRoutingController(store)

    assert path == workspace.absolute() / "state" / "v2" / STAGE_ROUTE_DATABASE_NAME
    assert not workspace.exists()
    with pytest.raises(StageRoutingValidationError, match=STAGE_ROUTE_DATABASE_NAME):
        StageRouteStore((tmp_path / "state" / "v2" / "workflow.sqlite3").absolute())
    with pytest.raises(StageRoutingValidationError, match="state/v2"):
        StageRouteStore((tmp_path / STAGE_ROUTE_DATABASE_NAME).absolute())
    assert not workspace.exists()


def test_initialization_is_idempotent_and_refuses_unrelated_database(tmp_path: Path) -> None:
    path = stage_route_database_path(tmp_path / "route")
    barrier = Barrier(16)

    def initialize(_: int) -> None:
        barrier.wait(timeout=10)
        StageRouteStore(path, busy_timeout_ms=10_000).initialize()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(initialize, range(16)))
    store = StageRouteStore(path)
    assert [item.mode.value for item in store.list_policies()] == ["legacy", "legacy", "legacy"]
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        marker = connection.execute("SELECT value FROM stage_route_meta WHERE key='schema_version'").fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert tables == {
        "stage_route_meta",
        "stage_route_policies",
        "stage_route_preflights",
        "stage_route_assignments",
        "stage_route_events",
    }
    assert marker == ("1",)
    assert journal_mode == ("wal",)
    assert integrity == ("ok",)

    unrelated = stage_route_database_path(tmp_path / "unrelated")
    unrelated.parent.mkdir(parents=True)
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE production_data(secret TEXT)")
    with pytest.raises(StageRouteStoreCorruptionError, match="refusing to claim"):
        StageRouteStore(unrelated).initialize()
    with sqlite3.connect(unrelated) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"production_data"}


def test_initialization_is_idempotent_across_processes(tmp_path: Path) -> None:
    path = stage_route_database_path(tmp_path / "processes")
    script = "\n".join(
        (
            "from pathlib import Path",
            "from unified_paper_orchestrator.workflow.stage_route_store import StageRouteStore",
            "StageRouteStore(Path(__import__('sys').argv[1]), busy_timeout_ms=10000).initialize()",
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
    assert [item.mode.value for item in StageRouteStore(path).list_policies()] == [
        "legacy",
        "legacy",
        "legacy",
    ]


def test_initialization_rolls_back_the_entire_schema_on_ddl_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = stage_route_database_path(tmp_path / "atomic")
    original_open = StageRouteStore._open_connection
    deny_ddl = True

    def open_with_authorizer(
        self: StageRouteStore,
        *,
        require_existing: bool,
    ) -> sqlite3.Connection:
        connection = original_open(self, require_existing=require_existing)

        def authorize(
            action: int,
            argument_one: str | None,
            _argument_two: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if deny_ddl and action == sqlite3.SQLITE_CREATE_TABLE and argument_one == "stage_route_preflights":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(StageRouteStore, "_open_connection", open_with_authorizer)
    store = StageRouteStore(path)
    with pytest.raises(StageRouteStoreCorruptionError, match="cannot initialize"):
        store.initialize()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == set()

    deny_ddl = False
    store.initialize()
    assert [item.mode.value for item in store.list_policies()] == ["legacy", "legacy", "legacy"]


def test_concurrent_admission_atomically_converges_on_one_pin(tmp_path: Path) -> None:
    path = stage_route_database_path(tmp_path)
    StageRouteStore(path).initialize()
    request = StageRouteRequest(Stage.JOURNAL_AUTHOR, "same-op", "same-subject", {"action": "run"})

    def admit(_: int):
        store = StageRouteStore(path, busy_timeout_ms=10_000)
        return StageRoutingController(store, clock=lambda: NOW).admit(request)

    with ThreadPoolExecutor(max_workers=16) as pool:
        assignments = list(pool.map(admit, range(64)))

    assert len({item.assignment_id for item in assignments}) == 1
    assert {item.owner for item in assignments} == {RouteOwner.LEGACY}
    assert len(StageRouteStore(path).list_assignments()) == 1


def test_operation_id_is_idempotent_only_for_the_same_subject_and_intent(tmp_path: Path) -> None:
    controller = StageRoutingController(_store(tmp_path), clock=lambda: NOW)
    first = controller.admit(StageRouteRequest(Stage.JOURNAL_AUTHOR, "op", "subject", {"action": "run", "version": 1}))
    same = controller.admit(StageRouteRequest(Stage.JOURNAL_AUTHOR, "op", "subject", {"version": 1, "action": "run"}))
    assert same.assignment_id == first.assignment_id

    with pytest.raises(StageRoutingConflictError, match="different subject or intent"):
        controller.admit(StageRouteRequest(Stage.JOURNAL_AUTHOR, "op", "subject", {"action": "run", "version": 2}))
    with pytest.raises(StageRoutingConflictError, match="different subject or intent"):
        controller.admit(StageRouteRequest(Stage.JOURNAL_AUTHOR, "op", "different", {"action": "run", "version": 1}))


def test_restart_preserves_generic_owner_even_with_closed_latch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller, report = _active_controller(store)
    request = StageRouteRequest(Stage.JOURNAL_AUTHOR, "generic-op", "subject", {"action": "run"})
    pinned = controller.admit(request, report=report)

    restarted = StageRoutingController(StageRouteStore(store.database_path), clock=lambda: NOW)
    recovered = restarted.admit(request)
    new_assignment = restarted.admit(
        StageRouteRequest(Stage.JOURNAL_AUTHOR, "new-op", "subject-2", {"action": "run"}),
        report=report,
    )

    assert pinned.owner is RouteOwner.GENERIC
    assert recovered.assignment_id == pinned.assignment_id
    assert recovered.owner is RouteOwner.GENERIC
    assert new_assignment.owner is RouteOwner.BLOCKED


def test_assignment_owner_and_row_are_sqlite_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assignment = StageRoutingController(store, clock=lambda: NOW).admit(
        StageRouteRequest(Stage.JOURNAL_AUTHOR, "immutable", "subject", {"action": "run"})
    )

    with sqlite3.connect(store.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE stage_route_assignments SET owner='generic' WHERE assignment_id=?",
                (assignment.assignment_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM stage_route_assignments WHERE assignment_id=?",
                (assignment.assignment_id,),
            )
    assert store.get_assignment(assignment.assignment_id).owner is RouteOwner.LEGACY


def test_store_rejects_symlink_or_reparse_workspace(tmp_path: Path) -> None:
    real = tmp_path / "real"
    linked = tmp_path / "linked"
    real.mkdir()
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable for this account")

    store = StageRouteStore(stage_route_database_path(linked))
    with pytest.raises(StageRouteStoreError, match="symlink or reparse"):
        store.initialize()
    assert not (real / "state").exists()


def test_candidate_modules_do_not_import_production_composition_or_legacy_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = (
        root / "unified_paper_orchestrator" / "workflow" / "stage_routing.py",
        root / "unified_paper_orchestrator" / "workflow" / "stage_route_store.py",
    )
    forbidden = {
        "unified_paper_orchestrator.orchestrator",
        "unified_paper_orchestrator.config",
        "unified_paper_orchestrator.adapters",
        "unified_paper_orchestrator.workflow.legacy_compat",
        "unified_paper_orchestrator.workflow.legacy_shadow",
        "unified_paper_orchestrator.workflow.shadow_store",
    }
    imported: set[str] = set()
    for target in targets:
        tree = ast.parse(target.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").lstrip("."))
    assert not imported.intersection(forbidden)
