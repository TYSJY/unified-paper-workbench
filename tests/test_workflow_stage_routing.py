from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.packs.sci import (
    build_sci_runtime_bindings,
    build_sci_workflow_pack,
)
from unified_paper_orchestrator.workflow.stage_route_store import (
    StageRouteStore,
    stage_route_database_path,
)
from unified_paper_orchestrator.workflow.stage_routing import (
    CRITICAL_PARITY_FIELDS,
    AssignmentStatus,
    CandidateDescriptor,
    GenericDispatchUncertainError,
    ParityWindow,
    PolicyMode,
    PreflightReport,
    RouteOwner,
    Stage,
    StageRouteRequest,
    StageRoutingBlockedError,
    StageRoutingConflictError,
    StageRoutingController,
    StageRoutingValidationError,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def _store(tmp_path: Path) -> StageRouteStore:
    store = StageRouteStore(stage_route_database_path(tmp_path))
    store.initialize()
    return store


def _candidate(stage: Stage, *, suffix: str = "a") -> CandidateDescriptor:
    stage_ids = {
        Stage.JOURNAL_AUTHOR: "builtin.sci.journal-author",
        Stage.FIGURES: "builtin.sci.figures",
        Stage.WRITING: "builtin.sci.writing",
    }
    digests = {
        "a": ("a" * 64, "b" * 64, "c" * 64),
        "d": ("d" * 64, "e" * 64, "f" * 64),
    }[suffix]
    return CandidateDescriptor(
        stage=stage,
        workflow_version_id=f"{stage_ids[stage]}@ready-{suffix}",
        workflow_id=stage_ids[stage],
        definition_sha256=digests[0],
        binding_sha256=digests[1],
        output_contract_sha256=digests[2],
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


def _parity(candidate: CandidateDescriptor, *, head: int, now: datetime = NOW) -> ParityWindow:
    return ParityWindow(
        stage=candidate.stage,
        definition_sha256=candidate.definition_sha256,
        binding_sha256=candidate.binding_sha256,
        comparison_head_sequence=head,
        consecutive_passes=3,
        distinct_subjects=3,
        critical_verdicts={field: "exact" for field in CRITICAL_PARITY_FIELDS},
        newest_observed_at=now.isoformat(),
        comparator_version="legacy-shadow-comparator-v1",
        mismatch_count=0,
    )


def _activate(
    controller: StageRoutingController,
    stage: Stage,
    candidate: CandidateDescriptor,
    *,
    first_head: int = 1,
) -> PreflightReport:
    legacy = controller._store.get_policy(stage)  # noqa: SLF001 - test verifies persisted CAS revisions
    shadow = controller.enable_shadow(stage, expected_revision=legacy.revision)
    canary_report = controller.preflight(candidate, _parity(candidate, head=first_head))
    canary = controller.promote_canary(stage, canary_report, expected_revision=shadow.revision)
    active_report = controller.preflight(candidate, _parity(candidate, head=first_head + 1))
    controller.promote_active(stage, active_report, expected_revision=canary.revision)
    return controller.preflight(candidate, _parity(candidate, head=first_head + 1))


def test_default_policy_calls_exactly_one_legacy_backend(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    request = StageRouteRequest(Stage.JOURNAL_AUTHOR, "op-1", "subject-1", {"action": "run"})
    assignment = controller.admit(request)
    called: list[str] = []

    result = controller.dispatch(
        assignment,
        legacy=lambda: called.append("legacy") or "legacy-result",
        generic=lambda: called.append("generic") or "generic-result",
    )

    assert result == "legacy-result"
    assert called == ["legacy"]
    assert assignment.owner is RouteOwner.LEGACY
    assert store.get_assignment(assignment.assignment_id).status is AssignmentStatus.COMPLETED


def test_current_sci_contracts_are_all_rejected_by_native_preflight(tmp_path: Path) -> None:
    store = _store(tmp_path / "route")
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    pack = build_sci_workflow_pack()
    bindings = build_sci_runtime_bindings(
        writing_root=tmp_path,
        figure_root=tmp_path,
        journal_author_root=tmp_path,
    )
    by_stage = {
        Stage.JOURNAL_AUTHOR: "builtin.sci.journal-author",
        Stage.FIGURES: "builtin.sci.figures",
        Stage.WRITING: "builtin.sci.writing",
    }

    for index, (stage, workflow_id) in enumerate(by_stage.items(), start=1):
        version = pack.workflow(workflow_id)
        candidate = CandidateDescriptor.from_workflow(
            stage,
            version,
            bindings[workflow_id],
            runtime_files_verified=False,
            prompt_seals_verified=False,
            executor_available=True,
            output_validators_available=False,
            compatibility_projection_available=False,
        )
        report = controller.preflight(candidate, _parity(candidate, head=index))
        assert report.eligible is False
        assert report.checks["candidate.native_dispatch_ready"] is False
        assert report.checks["candidate.runtime_freeze_complete"] is False
        assert report.checks["candidate.production_writer"] is False


def test_parity_contract_rejects_wrong_comparator_mismatch_and_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = Clock()
    controller = StageRoutingController(store, allow_candidate=True, clock=clock)
    candidate = _candidate(Stage.JOURNAL_AUTHOR)
    controller.enable_shadow(Stage.JOURNAL_AUTHOR, expected_revision=0)

    wrong_comparator = controller.preflight(
        candidate,
        replace(_parity(candidate, head=1), comparator_version="attacker-v9"),
    )
    mismatch = controller.preflight(candidate, replace(_parity(candidate, head=1), mismatch_count=1))
    assert wrong_comparator.checks["parity.comparator_version"] is False
    assert mismatch.checks["parity.zero_mismatches"] is False

    canary_report = controller.preflight(candidate, _parity(candidate, head=1))
    canary = controller.promote_canary(Stage.JOURNAL_AUTHOR, canary_report, expected_revision=1)
    replay = controller.preflight(candidate, _parity(candidate, head=1))
    assert replay.eligible is False
    assert replay.checks["parity.head_not_replayed"] is False
    with pytest.raises(StageRoutingBlockedError, match="eligible"):
        controller.promote_active(Stage.JOURNAL_AUTHOR, replay, expected_revision=canary.revision)


def test_stage_order_is_enforced_and_active_admission_allows_current_head(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    figures = _candidate(Stage.FIGURES)
    controller.enable_shadow(Stage.FIGURES, expected_revision=0)
    blocked = controller.preflight(figures, _parity(figures, head=1))
    assert blocked.checks["migration.previous_stages_active"] is False
    with pytest.raises(StageRoutingBlockedError):
        controller.promote_canary(Stage.FIGURES, blocked, expected_revision=1)

    journal_report = _activate(controller, Stage.JOURNAL_AUTHOR, _candidate(Stage.JOURNAL_AUTHOR))
    assert journal_report.eligible is True
    figures_report = controller.preflight(figures, _parity(figures, head=1))
    canary = controller.promote_canary(Stage.FIGURES, figures_report, expected_revision=1)
    fresh = controller.preflight(figures, _parity(figures, head=2))
    active = controller.promote_active(Stage.FIGURES, fresh, expected_revision=canary.revision)
    same_head = controller.preflight(figures, _parity(figures, head=2))
    assert active.mode is PolicyMode.ACTIVE
    assert same_head.eligible is True


def test_generic_exception_never_falls_back_and_unknown_blocks_drain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    candidate = _candidate(Stage.JOURNAL_AUTHOR)
    report = _activate(controller, Stage.JOURNAL_AUTHOR, candidate)
    assignment = controller.admit(
        StageRouteRequest(Stage.JOURNAL_AUTHOR, "op-generic", "subject", {"v": 1}),
        report=report,
    )
    called: list[str] = []

    def fail_generic() -> None:
        called.append("generic")
        raise RuntimeError("ambiguous transport failure")

    with pytest.raises(GenericDispatchUncertainError, match="no fallback"):
        controller.dispatch(
            assignment,
            legacy=lambda: called.append("legacy"),
            generic=fail_generic,
        )

    assert called == ["generic"]
    persisted = store.get_assignment(assignment.assignment_id)
    assert persisted.status is AssignmentStatus.UNCERTAIN
    draining = controller.begin_rollback(Stage.JOURNAL_AUTHOR, expected_revision=3)[0]
    with pytest.raises(StageRoutingBlockedError, match="nonterminal"):
        controller.finish_drain(Stage.JOURNAL_AUTHOR, expected_revision=draining.revision)
    controller.reconcile_uncertain(
        assignment.assignment_id,
        status=AssignmentStatus.FAILED,
        evidence_sha256="9" * 64,
    )
    legacy = controller.finish_drain(
        Stage.JOURNAL_AUTHOR,
        expected_revision=draining.revision,
        target=PolicyMode.LEGACY,
    )
    assert legacy.mode is PolicyMode.LEGACY


def test_policy_rollback_never_changes_existing_assignment_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    candidate = _candidate(Stage.JOURNAL_AUTHOR)
    report = _activate(controller, Stage.JOURNAL_AUTHOR, candidate)
    request = StageRouteRequest(Stage.JOURNAL_AUTHOR, "stable-op", "stable-subject", {"v": 1})
    pinned = controller.admit(request, report=report)
    controller.begin_rollback(Stage.JOURNAL_AUTHOR, expected_revision=3)

    after = controller.admit(request)
    newcomer = controller.admit(
        StageRouteRequest(Stage.JOURNAL_AUTHOR, "new-op", "new-subject", {"v": 1})
    )
    assert after.assignment_id == pinned.assignment_id
    assert after.owner is RouteOwner.GENERIC
    assert newcomer.owner is RouteOwner.LEGACY


def test_strict_evidence_deserialization_rejects_type_coercion_and_unknown_fields() -> None:
    candidate = _candidate(Stage.JOURNAL_AUTHOR)
    parity = _parity(candidate, head=1)
    payload = parity.to_dict()
    payload["comparison_head_sequence"] = True
    with pytest.raises(StageRoutingValidationError, match="integer"):
        ParityWindow.from_dict(payload)

    payload = candidate.to_dict()
    payload["published"] = "false"
    with pytest.raises(StageRoutingValidationError, match="boolean"):
        CandidateDescriptor.from_dict(payload)

    payload = parity.to_dict()
    payload["extra"] = "not allowed"
    with pytest.raises(StageRoutingValidationError, match="unknown fields"):
        ParityWindow.from_dict(payload)


def test_canary_opt_in_and_process_latch_are_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    enabled = StageRoutingController(store, allow_candidate=True, clock=Clock())
    candidate = _candidate(Stage.JOURNAL_AUTHOR)
    enabled.enable_shadow(Stage.JOURNAL_AUTHOR, expected_revision=0)
    report = enabled.preflight(candidate, _parity(candidate, head=1))
    enabled.promote_canary(Stage.JOURNAL_AUTHOR, report, expected_revision=1)
    admission_report = enabled.preflight(candidate, _parity(candidate, head=2))
    legacy = enabled.admit(
        StageRouteRequest(Stage.JOURNAL_AUTHOR, "canary-no", "s1", {"v": 1}),
        report=admission_report,
    )
    generic = enabled.admit(
        StageRouteRequest(
            Stage.JOURNAL_AUTHOR,
            "canary-yes",
            "s2",
            {"v": 1},
            canary_opt_in=True,
        ),
        report=admission_report,
    )
    closed = StageRoutingController(store, clock=Clock()).admit(
        StageRouteRequest(
            Stage.JOURNAL_AUTHOR,
            "canary-closed",
            "s3",
            {"v": 1},
            canary_opt_in=True,
        ),
        report=admission_report,
    )
    assert legacy.owner is RouteOwner.LEGACY
    assert generic.owner is RouteOwner.GENERIC
    assert closed.owner is RouteOwner.BLOCKED
    assert closed.status is AssignmentStatus.BLOCKED


def test_invalid_transitions_and_stale_revision_do_not_change_policy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    with pytest.raises(StageRoutingConflictError):
        controller.promote_active(
            Stage.JOURNAL_AUTHOR,
            object(),  # type: ignore[arg-type]
            expected_revision=0,
        )
    controller.enable_shadow(Stage.JOURNAL_AUTHOR, expected_revision=0)
    with pytest.raises(StageRoutingConflictError, match="stale"):
        controller.enable_shadow(Stage.FIGURES, expected_revision=4)
    assert store.get_policy(Stage.FIGURES).mode is PolicyMode.LEGACY


def test_shadow_can_be_explicitly_disabled_without_candidate_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = StageRoutingController(store, allow_candidate=True, clock=Clock())
    shadow = controller.enable_shadow(Stage.JOURNAL_AUTHOR, expected_revision=0)
    legacy = controller.disable_shadow(Stage.JOURNAL_AUTHOR, expected_revision=shadow.revision)
    assert legacy.mode is PolicyMode.LEGACY
    assert legacy.candidate_identity == ("", "", "", "")
