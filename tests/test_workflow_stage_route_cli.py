from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools.workflow_stage_route import main
from unified_paper_orchestrator.workflow.stage_route_store import stage_route_database_path


def _run(tmp_path: Path, capsys, *args: str) -> tuple[int, dict[str, object]]:
    code = main(["--workspace", str(tmp_path), *args])
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def test_cli_initializes_only_the_sidecar_and_reports_default_legacy(tmp_path: Path, capsys) -> None:
    code, payload = _run(tmp_path, capsys, "init")

    assert code == 0
    assert payload["candidate_routing_ready"] is True
    assert payload["production_cutover_enabled"] is False
    assert payload["production_entry_modified"] is False
    assert payload["production_affected"] is False
    assert payload["default_owner"] == "legacy"
    assert [item["mode"] for item in payload["ledger"]["policies"]] == [
        "legacy",
        "legacy",
        "legacy",
    ]
    database = stage_route_database_path(tmp_path)
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "workflow_runs" not in tables
    assert "stage_route_assignments" in tables


def test_cli_current_sci_preflight_is_rejected_even_when_latch_is_open(tmp_path: Path, capsys) -> None:
    code, payload = _run(
        tmp_path,
        capsys,
        "--allow-candidate",
        "preflight-sci",
        "--stage",
        "journal-author",
    )

    assert code == 0
    assert payload["status"] == "rejected"
    failed = set(payload["failed_checks"])
    assert "candidate.native_dispatch_ready" in failed
    assert "candidate.runtime_freeze_complete" in failed
    assert "candidate.production_writer" in failed
    assert payload["production_affected"] is False


def test_cli_shadow_and_simulated_admission_never_expose_raw_ids(tmp_path: Path, capsys) -> None:
    code, shadow = _run(
        tmp_path,
        capsys,
        "enable-shadow",
        "--stage",
        "journal-author",
        "--expected-revision",
        "0",
    )
    assert code == 0
    assert shadow["policy"]["mode"] == "shadow"

    operation_id = "private-operation-123"
    subject_id = "private-paper-456"
    code, admission = _run(
        tmp_path,
        capsys,
        "simulate-admit",
        "--stage",
        "journal-author",
        "--operation-id",
        operation_id,
        "--subject-id",
        subject_id,
        "--intent-json",
        '{"action":"run"}',
    )
    serialized = json.dumps(admission)
    assert code == 0
    assert admission["assignment"]["owner"] == "legacy"
    assert operation_id not in serialized
    assert subject_id not in serialized
    assert admission["production_affected"] is False


def test_cli_rejects_non_strict_parity_json_without_enabling_cutover(
    tmp_path: Path,
    capsys,
) -> None:
    parity = tmp_path / "parity.json"
    parity.write_text(
        json.dumps(
            {
                "stage": "journal-author",
                "definition_sha256": "a" * 64,
                "binding_sha256": "b" * 64,
                "comparison_head_sequence": True,
                "consecutive_passes": 3,
                "distinct_subjects": 3,
                "critical_verdicts": {},
                "newest_observed_at": "2026-08-05T00:00:00Z",
                "comparator_version": "legacy-shadow-comparator-v1",
                "mismatch_count": 0,
            }
        ),
        encoding="utf-8",
    )

    code, payload = _run(
        tmp_path,
        capsys,
        "--allow-candidate",
        "preflight-sci",
        "--stage",
        "journal-author",
        "--parity-json",
        str(parity),
    )

    assert code == 2
    assert payload["status"] == "rejected"
    assert payload["production_cutover_enabled"] is False
    assert payload["production_affected"] is False


def test_cli_source_has_no_dispatch_command_or_production_entry_import() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "workflow_stage_route.py").read_text(
        encoding="utf-8"
    )
    assert 'add_parser("dispatch")' not in source
    assert "unified_paper_orchestrator.orchestrator" not in source
    assert "unified_paper_orchestrator.__main__" not in source
