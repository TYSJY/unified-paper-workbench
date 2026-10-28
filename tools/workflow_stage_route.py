"""Standalone management CLI for the dormant P8 candidate-routing ledger.

This tool is intentionally absent from the production CLI registration.  It
can inspect and mutate only the isolated sidecar database; it cannot queue or
execute a production workflow stage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.workflow.packs.sci import (  # noqa: E402
    SCI_FIGURES_WORKFLOW_ID,
    SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
    SCI_WRITING_WORKFLOW_ID,
    build_sci_runtime_bindings,
    build_sci_workflow_pack,
)
from unified_paper_orchestrator.workflow.stage_route_store import (  # noqa: E402
    StageRouteStore,
    stage_route_database_path,
)
from unified_paper_orchestrator.workflow.stage_routing import (  # noqa: E402
    AssignmentStatus,
    CandidateDescriptor,
    LegacyOperationState,
    ParityWindow,
    PolicyMode,
    Stage,
    StageRouteRequest,
    StageRoutingError,
    StageRoutingController,
)


_WORKFLOW_IDS = {
    Stage.JOURNAL_AUTHOR: SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
    Stage.FIGURES: SCI_FIGURES_WORKFLOW_ID,
    Stage.WRITING: SCI_WRITING_WORKFLOW_ID,
}
_SAFETY_ENVELOPE = {
    "candidate_routing_ready": True,
    "production_cutover_enabled": False,
    "production_entry_modified": False,
    "production_affected": False,
    "default_owner": "legacy",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the isolated P8 candidate route ledger; never dispatch production work.",
    )
    parser.add_argument("--workspace", required=True, help="Explicit workspace root for state/v2 sidecar data.")
    parser.add_argument(
        "--allow-candidate",
        action="store_true",
        help="Open the process-local safety latch for this one management invocation.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")

    shadow = commands.add_parser("enable-shadow")
    _stage_argument(shadow)
    shadow.add_argument("--expected-revision", type=int, required=True)
    shadow.add_argument("--min-consecutive-passes", type=int, default=3)
    shadow.add_argument("--min-distinct-subjects", type=int, default=3)
    shadow.add_argument("--max-parity-age-seconds", type=int, default=86_400)

    disable_shadow = commands.add_parser("disable-shadow")
    _stage_argument(disable_shadow)
    disable_shadow.add_argument("--expected-revision", type=int, required=True)

    preflight = commands.add_parser("preflight-sci")
    _stage_argument(preflight)
    preflight.add_argument("--parity-json", help="Strict ParityWindow JSON evidence file.")
    preflight.add_argument("--ttl-seconds", type=int, default=300)

    canary = commands.add_parser("promote-canary")
    _stage_argument(canary)
    canary.add_argument("--expected-revision", type=int, required=True)
    canary.add_argument("--evidence-sha256", required=True)

    active = commands.add_parser("promote-active")
    _stage_argument(active)
    active.add_argument("--expected-revision", type=int, required=True)
    active.add_argument("--evidence-sha256", required=True)

    rollback = commands.add_parser("rollback")
    _stage_argument(rollback)
    rollback.add_argument("--expected-revision", type=int, required=True)

    drain = commands.add_parser("finish-drain")
    _stage_argument(drain)
    drain.add_argument("--expected-revision", type=int, required=True)
    drain.add_argument("--target", choices=("shadow", "legacy"), default="shadow")

    admit = commands.add_parser("simulate-admit")
    _stage_argument(admit)
    admit.add_argument("--operation-id", required=True)
    admit.add_argument("--subject-id", required=True)
    admit.add_argument("--intent-json", required=True)
    admit.add_argument("--canary-opt-in", action="store_true")
    admit.add_argument(
        "--legacy-operation-state",
        choices=tuple(item.value for item in LegacyOperationState),
        default=LegacyOperationState.ABSENT.value,
    )
    admit.add_argument("--evidence-sha256")

    reconcile = commands.add_parser("reconcile-uncertain")
    reconcile.add_argument("--assignment-id", required=True)
    reconcile.add_argument(
        "--status",
        choices=("completed", "failed", "blocked", "cancelled"),
        required=True,
    )
    reconcile.add_argument("--evidence-sha256", required=True)
    return parser


def _stage_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stage", choices=tuple(item.value for item in Stage), required=True)


def _read_json(path: str, *, name: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {name} JSON") from exc


def _sci_candidate(workspace: Path, stage: Stage) -> CandidateDescriptor:
    pack = build_sci_workflow_pack()
    version = pack.workflow(_WORKFLOW_IDS[stage])
    bindings = build_sci_runtime_bindings(
        writing_root=workspace,
        figure_root=workspace,
        journal_author_root=workspace,
    )
    binding = bindings[version.workflow_id]
    files_verified = True
    try:
        binding.validate_files(version.definition)
    except (OSError, TypeError, ValueError):
        files_verified = False
    return CandidateDescriptor.from_workflow(
        stage,
        version,
        binding,
        runtime_files_verified=files_verified,
        prompt_seals_verified=files_verified,
        executor_available=True,
        output_validators_available=False,
        compatibility_projection_available=False,
    )


def _report(store: StageRouteStore, digest: str):
    report = store.get_preflight(digest)
    if report is None:
        raise ValueError("preflight evidence does not exist")
    return report


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().absolute()
    store = StageRouteStore(stage_route_database_path(workspace))
    store.initialize()
    controller = StageRoutingController(store, allow_candidate=args.allow_candidate)
    command = args.command
    if command == "init":
        result: dict[str, Any] = {"status": "initialized", "ledger": store.summary()}
    elif command == "status":
        result = {"status": "ok", "ledger": store.summary()}
    elif command == "enable-shadow":
        policy = controller.enable_shadow(
            Stage(args.stage),
            expected_revision=args.expected_revision,
            min_consecutive_passes=args.min_consecutive_passes,
            min_distinct_subjects=args.min_distinct_subjects,
            max_parity_age_seconds=args.max_parity_age_seconds,
        )
        result = {"status": "shadow-enabled", "policy": policy.to_dict()}
    elif command == "disable-shadow":
        policy = controller.disable_shadow(
            Stage(args.stage),
            expected_revision=args.expected_revision,
        )
        result = {"status": "shadow-disabled", "policy": policy.to_dict()}
    elif command == "preflight-sci":
        stage = Stage(args.stage)
        candidate = _sci_candidate(workspace, stage)
        parity = None
        if args.parity_json:
            raw = _read_json(args.parity_json, name="parity")
            if not isinstance(raw, dict):
                raise ValueError("parity JSON must be an object")
            parity = ParityWindow.from_dict(raw)
        report = controller.preflight(candidate, parity, ttl_seconds=args.ttl_seconds)
        result = {
            "status": "eligible" if report.eligible else "rejected",
            "report": report.to_dict(),
            "failed_checks": sorted(key for key, passed in report.checks.items() if not passed),
        }
    elif command in {"promote-canary", "promote-active"}:
        report = _report(store, args.evidence_sha256)
        promote = controller.promote_canary if command == "promote-canary" else controller.promote_active
        policy = promote(
            Stage(args.stage),
            report,
            expected_revision=args.expected_revision,
        )
        result = {"status": command.removeprefix("promote-") + "-enabled", "policy": policy.to_dict()}
    elif command == "rollback":
        policies = controller.begin_rollback(
            Stage(args.stage),
            expected_revision=args.expected_revision,
        )
        result = {"status": "draining", "policies": [item.to_dict() for item in policies]}
    elif command == "finish-drain":
        policy = controller.finish_drain(
            Stage(args.stage),
            expected_revision=args.expected_revision,
            target=PolicyMode(args.target),
        )
        result = {"status": "drain-finished", "policy": policy.to_dict()}
    elif command == "simulate-admit":
        try:
            intent = json.loads(args.intent_json)
        except json.JSONDecodeError as exc:
            raise ValueError("intent-json must be valid JSON") from exc
        report = _report(store, args.evidence_sha256) if args.evidence_sha256 else None
        assignment = controller.admit(
            StageRouteRequest(
                stage=Stage(args.stage),
                operation_id=args.operation_id,
                subject_id=args.subject_id,
                intent=intent,
                canary_opt_in=args.canary_opt_in,
                legacy_operation_state=LegacyOperationState(args.legacy_operation_state),
            ),
            report=report,
        )
        result = {"status": "simulated", "assignment": assignment.to_dict()}
    elif command == "reconcile-uncertain":
        assignment = controller.reconcile_uncertain(
            args.assignment_id,
            status=AssignmentStatus(args.status),
            evidence_sha256=args.evidence_sha256,
        )
        result = {"status": "reconciled", "assignment": assignment.to_dict()}
    else:  # pragma: no cover - argparse owns command validation
        raise ValueError("unknown command")
    return {**_SAFETY_ENVELOPE, **result}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _execute(args)
    except (OSError, ValueError, StageRoutingError) as exc:
        payload = {
            **_SAFETY_ENVELOPE,
            "status": "rejected",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
