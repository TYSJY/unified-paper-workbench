from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unified_paper_orchestrator import __main__ as cli_module
from unified_paper_orchestrator import writing_evidence_import as import_module
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.writing_evidence_gate import validate_writing_evidence
from unified_paper_orchestrator.writing_evidence_import import prepare_writing_evidence_import


CLAIM_EXCERPT = "Median latency was 1.25 ms across measured trials."


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_import_fixture(papers_dir: Path, paper_id: str = "paper-1") -> tuple[Path, Path]:
    paper_dir = papers_dir / paper_id
    results_dir = paper_dir / "results"
    results_dir.mkdir(parents=True)
    (paper_dir / "paper_materials.md").write_text(
        "# Measured materials\n\nStatus: READY\n",
        encoding="utf-8",
    )
    (paper_dir / "draft_v2.tex").write_text(
        f"\\section{{Measured Results}}\n{CLAIM_EXCERPT}\n",
        encoding="utf-8",
    )
    (results_dir / "trials.jsonl").write_text(
        '{"evidence_id":"trial-1","latency_ms":1.25}\n',
        encoding="utf-8",
    )
    (results_dir / "metrics.json").write_text(
        '{"rows":[{"evidence_id":"metric-1","median_latency_ms":1.25}]}\n',
        encoding="utf-8",
    )
    spec_path = results_dir / "evidence_import.json"
    write_json_atomic(
        spec_path,
        {
            "import_spec_version": 1,
            "artifacts": [
                {"id": "raw-trials", "role": "raw", "path": "results/trials.jsonl"},
                {"id": "aggregate-metrics", "role": "aggregate", "path": "results/metrics.json"},
            ],
            "evidence_contract": {
                "environment": {
                    "description": "CPython 3.12 on Windows with fixed local inputs",
                    "artifact_ids": ["raw-trials"],
                },
                "workflows": {"ids": ["evidence-bound-report"], "artifact_ids": ["raw-trials"]},
                "faults": {"ids": ["no-fault", "crash-after-cut"], "artifact_ids": ["raw-trials"]},
                "seeds": {"values": [7], "artifact_ids": ["raw-trials"]},
                "metrics": {"ids": ["latency-ms"], "artifact_ids": ["aggregate-metrics"]},
                "limitations": {
                    "items": ["Local deterministic prototype only"],
                    "artifact_ids": ["aggregate-metrics"],
                },
                "raw_artifact_ids": ["raw-trials"],
                "aggregate_artifact_ids": ["aggregate-metrics"],
            },
            "claims": [
                {
                    "claim_id": "claim-latency",
                    "draft_excerpt": CLAIM_EXCERPT,
                    "evidence": [
                        {"artifact_id": "raw-trials", "row_id": "trial-1"},
                        {"artifact_id": "aggregate-metrics", "row_id": "metric-1"},
                    ],
                }
            ],
        },
    )
    return paper_dir, spec_path


def test_check_only_hashes_real_inputs_without_writing(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir, spec_path = make_import_fixture(papers_dir)

    result = prepare_writing_evidence_import(
        papers_dir,
        "paper-1",
        spec_path,
        check_only=True,
    )

    assert result["ok"] is True
    assert result["status"] == "CHECKED"
    assert result["candidate_ready"] is True
    assert result["transition_ready"] is False
    assert result["manifest_written"] is False
    assert result["receipt_written"] is False
    assert result["model_work_resumed"] is False
    assert result["scheduler_state_changed"] is False
    assert not (paper_dir / "results" / "verification_manifest.json").exists()
    assert not (paper_dir / "results" / "evidence_import_receipts").exists()


def test_commit_writes_receipt_then_atomic_manifest_and_gate_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir, spec_path = make_import_fixture(papers_dir)
    writes: list[str] = []
    original_receipt_write = import_module.write_json_atomic
    original_manifest_write = import_module.write_json_atomic_if_unchanged

    def record_receipt(path: Path, value: object) -> None:
        writes.append("receipt")
        original_receipt_write(path, value)

    def record_manifest(path: Path, value: object, expected: str | None) -> bool:
        writes.append("manifest")
        return original_manifest_write(path, value, expected)

    monkeypatch.setattr(import_module, "write_json_atomic", record_receipt)
    monkeypatch.setattr(import_module, "write_json_atomic_if_unchanged", record_manifest)

    result = prepare_writing_evidence_import(papers_dir, "paper-1", spec_path)

    manifest_path = paper_dir / "results" / "verification_manifest.json"
    receipt_path = paper_dir / result["receipt_path"]
    assert result["ok"] is True
    assert result["status"] == "IMPORTED"
    assert result["manifest_written"] is True
    assert result["receipt_written"] is True
    assert result["transition_ready"] is True
    assert result["model_work_resumed"] is False
    assert writes == ["receipt", "manifest"]
    assert sha256(manifest_path) == result["manifest_sha256"]
    assert receipt_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert manifest["draft_v2_sha256"] == sha256(paper_dir / "draft_v2.tex")
    assert manifest["paper_materials_sha256"] == sha256(paper_dir / "paper_materials.md")
    assert manifest["artifacts"][0]["sha256"] == sha256(paper_dir / "results" / "trials.jsonl")
    assert receipt["manifest"]["sha256"] == sha256(manifest_path)
    assert receipt["spec"]["sha256"] == sha256(spec_path)
    assert receipt["effects"] == {"model_work_resumed": False, "scheduler_state_changed": False}
    assert validate_writing_evidence(papers_dir, "paper-1")["ready"] is True


def test_traversal_spec_fails_closed_without_replacing_existing_manifest(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir, spec_path = make_import_fixture(papers_dir)
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest_path.write_bytes(b"existing manifest must survive\n")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["artifacts"][0]["path"] = "../outside.jsonl"
    write_json_atomic(spec_path, spec)

    result = prepare_writing_evidence_import(papers_dir, "paper-1", spec_path)

    assert result["ok"] is False
    assert result["manifest_written"] is False
    assert manifest_path.read_bytes() == b"existing manifest must survive\n"
    assert {issue["code"] for issue in result["issues"]} >= {"evidence_import_artifact_path"}


def test_source_change_after_receipt_blocks_manifest_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir, spec_path = make_import_fixture(papers_dir)
    raw_path = paper_dir / "results" / "trials.jsonl"
    original_write = import_module.write_json_atomic

    def mutate_after_receipt(path: Path, value: object) -> None:
        original_write(path, value)
        raw_path.write_bytes(raw_path.read_bytes() + b" \n")

    monkeypatch.setattr(import_module, "write_json_atomic", mutate_after_receipt)

    result = prepare_writing_evidence_import(papers_dir, "paper-1", spec_path)

    assert result["ok"] is False
    assert result["receipt_written"] is True
    assert result["manifest_written"] is False
    assert not (paper_dir / "results" / "verification_manifest.json").exists()
    assert "evidence_import_snapshot_changed" in {issue["code"] for issue in result["issues"]}


def test_commit_is_bound_to_explicit_check_only_preview(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir, spec_path = make_import_fixture(papers_dir)
    preview = prepare_writing_evidence_import(papers_dir, "paper-1", spec_path, check_only=True)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["evidence_contract"]["limitations"]["items"] = ["Changed after preview"]
    write_json_atomic(spec_path, spec)

    result = prepare_writing_evidence_import(
        papers_dir,
        "paper-1",
        spec_path,
        expected_manifest_sha256=preview["manifest_sha256"],
    )

    assert result["ok"] is False
    assert result["manifest_written"] is False
    assert result["receipt_written"] is False
    assert not (paper_dir / "results" / "verification_manifest.json").exists()
    assert "evidence_import_preview_changed" in {issue["code"] for issue in result["issues"]}


def test_cli_supports_check_only_and_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir, _spec_path = make_import_fixture(papers_dir)
    monkeypatch.setattr(cli_module, "LEGACY_PROJECTS", {"writing": {"papers_dir": papers_dir}})

    assert (
        cli_module.main(
            ["writing-evidence-import", "paper-1", "--spec", "results/evidence_import.json", "--check-only"]
        )
        == 0
    )
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "CHECKED"
    assert not (paper_dir / "results" / "verification_manifest.json").exists()

    assert cli_module.main(["writing-evidence-import", "paper-1", "--spec", "results/evidence_import.json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["status"] == "IMPORTED"
    assert imported["transition_ready"] is True
