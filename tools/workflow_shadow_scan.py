from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.workflow.legacy_shadow import (  # noqa: E402
    LegacyShadowSourceError,
    LegacySource,
    compare_legacy,
    observe_once,
)
from unified_paper_orchestrator.workflow.contracts import canonical_sha256  # noqa: E402
from unified_paper_orchestrator.workflow.packs import (  # noqa: E402
    SCI_FIGURES_WORKFLOW_ID,
    SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
    SCI_SMART_SUBMISSION_WORKFLOW_ID,
    SCI_WRITING_WORKFLOW_ID,
    build_sci_workflow_pack,
)
from unified_paper_orchestrator.workflow.shadow_store import (  # noqa: E402
    ComparisonWrite,
    ObservationWrite,
    ShadowStore,
    ShadowStoreError,
    shadow_subject_fingerprint,
)


_WORKFLOW_IDS = {
    "writing": SCI_WRITING_WORKFLOW_ID,
    "figures": SCI_FIGURES_WORKFLOW_ID,
    "journal-author": SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
    "smart-submission": SCI_SMART_SUBMISSION_WORKFLOW_ID,
}


def _source(value: str, *, required: bool) -> LegacySource:
    name, separator, raw_path = str(value or "").partition("=")
    if not separator or not name.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("source must use LOGICAL_NAME=PATH")
    return LegacySource(name.strip(), raw_path.strip(), required=required)


def _required_source(value: str) -> LegacySource:
    return _source(value, required=True)


def _optional_source(value: str) -> LegacySource:
    return _source(value, required=False)


def _json_mapping(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON evidence ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON evidence must be an object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only legacy workflow parity scan; no production runtime is imported or called.",
    )
    parser.add_argument("--stage", choices=tuple(_WORKFLOW_IDS), required=True)
    parser.add_argument("--paper-id", required=True)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        type=_required_source,
        metavar="NAME=PATH",
        help="Required legacy JSON source. Repeat for pipeline and SDK journal.",
    )
    parser.add_argument(
        "--optional-source",
        action="append",
        default=[],
        type=_optional_source,
        metavar="NAME=PATH",
    )
    parser.add_argument("--artifact-evidence", help="Optional sanitized node-to-artifact fingerprint JSON.")
    parser.add_argument("--shadow-steps", help="Optional sanitized generic shadow node snapshot JSON.")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--shadow-db",
        help="Explicit isolated shadow SQLite path. Omit for a no-write scan.",
    )
    parser.add_argument("--fail-on-mismatch", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sources = tuple(args.source) + tuple(args.optional_source)
    required_names = {item.logical_name for item in args.source}
    if not {"pipeline", "sdk-journal"}.issubset(required_names):
        print(
            json.dumps(
                {
                    "status": "invalid-input",
                    "code": "required_authorities_missing",
                    "production_affected": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    pack = build_sci_workflow_pack()
    definition = pack.workflow(_WORKFLOW_IDS[args.stage]).definition
    try:
        artifact_evidence = _json_mapping(args.artifact_evidence) if args.artifact_evidence else None
        shadow_steps = _json_mapping(args.shadow_steps) if args.shadow_steps else None
        shadow_steps_sha256 = canonical_sha256(shadow_steps) if shadow_steps is not None else ""

        def read_artifacts(*_: object):
            return artifact_evidence or {}

        observation = observe_once(
            args.stage,
            args.paper_id,
            definition,
            sources=sources,
            artifact_reader=read_artifacts if artifact_evidence is not None else None,
            attempts=args.attempts,
        )
        comparison = compare_legacy(observation, definition, shadow_steps=shadow_steps)
    except LegacyShadowSourceError as exc:
        print(
            json.dumps(
                {
                    "status": "source-error",
                    "source_fingerprint": canonical_sha256(exc.logical_name),
                    "code": exc.code,
                    "production_affected": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    except (TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "invalid-input",
                    "error_type": type(exc).__name__,
                    "message": "shadow scan input rejected",
                    "production_affected": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    persistence: dict[str, Any] = {"requested": bool(args.shadow_db), "persisted": False}
    if args.shadow_db:
        try:
            store = ShadowStore(args.shadow_db)
            store.initialize()
            observation_outcome = store.try_record_observation(
                ObservationWrite(
                    stage=observation.stage,
                    subject_id=observation.paper_id,
                    source_revision_sha256=observation.source_revision_sha256,
                    projector_version=observation.projector_version,
                    capture_status="coherent" if observation.coherent else "unstable",
                    payload=observation.to_storage_payload(),
                )
            )
            if observation_outcome.ok:
                observation_record = observation_outcome.record
                comparison_outcome = store.try_record_comparison(
                    ComparisonWrite(
                        observation_id=observation_record.observation_id,
                        definition_sha256=comparison.definition_sha256,
                        comparator_version=comparison.comparator_version,
                        overall_verdict=comparison.verdict,
                        payload=comparison.to_storage_payload(),
                        binding_sha256=shadow_steps_sha256,
                    )
                )
                if comparison_outcome.ok:
                    persistence = {
                        "requested": True,
                        "persisted": True,
                        "observation_id": observation_record.observation_id,
                        "comparison_id": comparison_outcome.record.comparison_id,
                    }
                else:
                    persistence = {
                        "requested": True,
                        "persisted": False,
                        "diagnostic": comparison_outcome.diagnostic.to_dict(),
                    }
            else:
                persistence = {
                    "requested": True,
                    "persisted": False,
                    "diagnostic": observation_outcome.diagnostic.to_dict(),
                }
        except ShadowStoreError as exc:
            persistence = {
                "requested": True,
                "persisted": False,
                "diagnostic": exc.diagnostic.to_dict(),
            }
        except (OSError, ValueError) as exc:
            persistence = {
                "requested": True,
                "persisted": False,
                "diagnostic": {
                    "code": "SHADOW_STORE_UNAVAILABLE",
                    "operation": "initialize",
                    "detail": type(exc).__name__,
                    "retryable": False,
                },
            }
    subject_fingerprint = shadow_subject_fingerprint(observation.stage, observation.paper_id)
    safe_observation = observation.to_storage_payload()
    safe_observation.update(
        {
            "subject_fingerprint": subject_fingerprint,
            "source_revision_sha256": observation.source_revision_sha256,
            "projector_version": observation.projector_version,
        }
    )
    safe_comparison = comparison.to_storage_payload()
    safe_comparison.update(
        {
            "definition_sha256": comparison.definition_sha256,
            "binding_sha256": shadow_steps_sha256,
            "comparator_version": comparison.comparator_version,
        }
    )
    result = {
        "status": "observed" if observation.coherent else "unstable",
        "production_writer": "legacy-runtime",
        "production_affected": False,
        "persistence": persistence,
        "observation": safe_observation,
        "comparison": safe_comparison,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.fail_on_mismatch and comparison.verdict == "mismatch" else 0


if __name__ == "__main__":
    raise SystemExit(main())
