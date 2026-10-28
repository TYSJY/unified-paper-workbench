from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

from unified_paper_orchestrator import __main__ as cli_module
from unified_paper_orchestrator import automation as automation_module
from unified_paper_orchestrator import writing_evidence_gate as evidence_gate_module
from unified_paper_orchestrator.adapters import writing as writing_module
from unified_paper_orchestrator.ai_provider_store import AIProviderStore
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.writing_evidence_gate import validate_writing_evidence


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_ready_input_snapshot(
    result: dict,
    root: Path,
    paths: set[Path],
    *,
    gate_profile: str,
    scope: str,
) -> None:
    snapshot = result["input_snapshot"]
    expected_files = sorted(
        (
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256(path),
                "size": path.stat().st_size,
            }
            for path in paths
        ),
        key=lambda item: (item["path"].casefold(), item["path"]),
    )
    assert snapshot == {
        "schema": "unified-paper-orchestrator/writing-gate-input-snapshot/v1",
        "gate_profile": gate_profile,
        "paper_id": "paper-1",
        "scope": scope,
        "files": expected_files,
        "snapshot_sha256": snapshot["snapshot_sha256"],
    }
    payload = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert snapshot["snapshot_sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_ready_fixture(papers_dir: Path, paper_id: str = "paper-1") -> Path:
    paper_dir = papers_dir / paper_id
    results_dir = paper_dir / "results"
    results_dir.mkdir(parents=True)
    materials = paper_dir / "paper_materials.md"
    materials.write_text(
        "# Measured materials\n\nStatus: READY\n",
        encoding="utf-8",
    )
    draft = paper_dir / "draft_v2.tex"
    claim_excerpt = "Median latency was 1.25 ms across measured trials."
    draft.write_text(f"\\section{{Measured Results}}\n{claim_excerpt}\n", encoding="utf-8")
    raw = results_dir / "trials.jsonl"
    raw.write_text('{"evidence_id":"trial-1","latency_ms":1.25}\n', encoding="utf-8")
    aggregate = results_dir / "metrics.json"
    aggregate.write_text(
        '{"rows":[{"evidence_id":"metric-1","median_latency_ms":1.25}]}\n',
        encoding="utf-8",
    )
    write_json_atomic(
        results_dir / "verification_manifest.json",
        {
            "schema_version": 2,
            "paper_id": paper_id,
            "status": "READY",
            "draft_v2_sha256": sha256(draft),
            "paper_materials_sha256": sha256(materials),
            "artifacts": [
                {
                    "id": "raw-trials",
                    "role": "raw",
                    "path": "results/trials.jsonl",
                    "sha256": sha256(raw).upper(),
                },
                {
                    "id": "aggregate-metrics",
                    "role": "aggregate",
                    "path": "results/metrics.json",
                    "sha256": sha256(aggregate).upper(),
                },
            ],
            "evidence_contract": {
                "schema_version": 1,
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
            "claim_evidence": {
                "coverage": "all_quantitative_claims_in_draft_v2",
                "draft_v2_sha256": sha256(draft),
                "claims": [
                    {
                        "claim_id": "claim-latency",
                        "type": "quantitative",
                        "draft_excerpt": claim_excerpt,
                        "evidence": [
                            {"artifact_id": "raw-trials", "row_id": "trial-1"},
                            {"artifact_id": "aggregate-metrics", "row_id": "metric-1"},
                        ],
                    }
                ],
            },
        },
    )
    return paper_dir


def refresh_bound_hashes(paper_dir: Path) -> None:
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    draft = paper_dir / "draft_v2.tex"
    materials = paper_dir / "paper_materials.md"
    manifest["draft_v2_sha256"] = sha256(draft)
    manifest["paper_materials_sha256"] = sha256(materials)
    if isinstance(manifest.get("claim_evidence"), dict):
        manifest["claim_evidence"]["draft_v2_sha256"] = sha256(draft)
    write_json_atomic(manifest_path, manifest)


def issue_codes(result: dict) -> set[str]:
    return {str(item.get("code")) for item in result.get("issues", [])}


def project_config(root: Path) -> dict[str, object]:
    return {
        "label": "writing",
        "root": root,
        "state_path": root / "controller" / "pipeline_state.json",
        "prompt_path": root / "controller" / "SCHEDULER_PROMPT.md",
        "papers_dir": root / "papers",
        "automation_ids": ["writing-scheduler"],
        "primary_automation_id": "writing-scheduler",
        "heartbeat_minutes": 5,
    }


def test_ready_gate_validates_manifest_artifact_and_draft_hash(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["status"] == "READY"
    assert result["ready"] is True
    assert result["transition_ready"] is True
    assert result["artifact_count"] == 2
    assert result["claim_count"] == 1
    assert len(result["draft_v2_sha256"]) == 64
    assert len(result["paper_materials_sha256"]) == 64
    assert result["issues"] == []
    assert_ready_input_snapshot(
        result,
        papers_dir,
        {
            paper_dir / "paper_materials.md",
            paper_dir / "draft_v2.tex",
            paper_dir / "results" / "verification_manifest.json",
            paper_dir / "results" / "trials.jsonl",
            paper_dir / "results" / "metrics.json",
        },
        gate_profile="evidence_v2_transition",
        scope="papers_root",
    )


def test_evidence_gate_reads_each_input_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    expected = {
        (paper_dir / "paper_materials.md").absolute(),
        (paper_dir / "draft_v2.tex").absolute(),
        (paper_dir / "results" / "verification_manifest.json").absolute(),
        (paper_dir / "results" / "trials.jsonl").absolute(),
        (paper_dir / "results" / "metrics.json").absolute(),
    }
    reads: Counter[Path] = Counter()
    original_open = Path.open

    def counted_open(path: Path, mode: str = "r", *args, **kwargs):
        if mode == "rb" and path.absolute() in expected:
            reads[path.absolute()] += 1
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["status"] == "READY"
    assert reads == Counter({path: 1 for path in expected})


def test_evidence_gate_fails_if_snapshot_changes_before_validation_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    raw_path = paper_dir / "results" / "trials.jsonl"
    original = evidence_gate_module._validate_claim_evidence

    def mutate_after_snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        raw_path.write_text('{"evidence_id":"changed","latency_ms":9.99}\n', encoding="utf-8")
        return result

    monkeypatch.setattr(evidence_gate_module, "_validate_claim_evidence", mutate_after_snapshot)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "evidence_snapshot_changed" in issue_codes(result)


def test_evidence_gate_fails_if_file_mtime_changes_during_its_single_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    target = (paper_dir / "paper_materials.md").absolute()
    original_open = Path.open

    class MutatingHandle:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self) -> int:
            return self.handle.fileno()

        def read(self) -> bytes:
            data = self.handle.read()
            current = os.stat(target)
            os.utime(
                target,
                ns=(current.st_atime_ns, current.st_mtime_ns + 2_000_000_000),
            )
            return data

    def mutating_open(path: Path, mode: str = "r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if mode == "rb" and path.absolute() == target:
            return MutatingHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", mutating_open)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "paper_materials_snapshot_unstable" in issue_codes(result)


def test_evidence_gate_rejects_symlinked_artifact_even_when_bytes_match(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    link = paper_dir / "results" / "trials.jsonl"
    target = paper_dir / "results" / "unlisted-trials.jsonl"
    target.write_bytes(link.read_bytes())
    link.unlink()
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "verification_artifact_path" in issue_codes(result)


def test_evidence_gate_rejects_simulated_junction_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    junction = (paper_dir / "results").absolute()
    monkeypatch.setattr(
        evidence_gate_module.os.path,
        "isjunction",
        lambda path: Path(path).absolute() == junction,
        raising=False,
    )

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "verification_manifest_unsafe_path" in issue_codes(result)


def test_evidence_gate_rejects_symlinked_paper_directory(tmp_path: Path) -> None:
    real_papers = tmp_path / "real-papers"
    real_paper = make_ready_fixture(real_papers)
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    linked_paper = papers_dir / "paper-1"
    try:
        linked_paper.symlink_to(real_paper, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "paper_path_unsafe" in issue_codes(result)


@pytest.mark.parametrize(
    ("relative_path", "expected_code"),
    [
        ("paper_materials.md", "paper_materials_missing"),
        ("draft_v2.tex", "draft_v2_missing"),
        ("results/verification_manifest.json", "verification_manifest_missing"),
        ("results/trials.jsonl", "verification_artifact_missing"),
    ],
)
def test_missing_required_files_block(tmp_path: Path, relative_path: str, expected_code: str) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    (paper_dir / relative_path).unlink()

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert expected_code in issue_codes(result)


def test_artifact_and_draft_hash_mismatches_block(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["draft_v2_sha256"] = "0" * 64
    manifest["artifacts"][0]["sha256"] = "F" * 64
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert {"draft_v2_hash_mismatch", "verification_artifact_hash_mismatch"} <= issue_codes(result)


def test_artifact_path_traversal_is_rejected_even_when_target_exists(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    outside = papers_dir / "outside.txt"
    outside.write_text("must not be trusted", encoding="utf-8")
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["path"] = "../outside.txt"
    manifest["artifacts"][0]["sha256"] = sha256(outside)
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "verification_artifact_path" in issue_codes(result)


@pytest.mark.parametrize(
    "marker",
    ["\\VERIFY{latency}", "【验】", "SIMULATED_TO_REPLACE", "verification-required placeholder"],
)
def test_forbidden_draft_markers_block(tmp_path: Path, marker: str) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    draft = paper_dir / "draft_v2.tex"
    draft.write_text(f"Measured section\n{marker}\n", encoding="utf-8")
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["draft_v2_sha256"] = sha256(draft)
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "draft_v2_marker_remaining" in issue_codes(result)


def test_correct_unicode_verification_marker_blocks(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    draft = paper_dir / "draft_v2.tex"
    draft.write_text(draft.read_text(encoding="utf-8") + "\n\u3010\u9a8c\u3011\n", encoding="utf-8")
    refresh_bound_hashes(paper_dir)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "draft_v2_marker_remaining" in issue_codes(result)


@pytest.mark.parametrize(
    "marker",
    [
        "BLOCKING TODO",
        "blocking_todo",
        "UNRESOLVED",
        "TODO: replace result",
        "[TODO]",
        "\\todo{replace result}",
        "PLACEHOLDER: replace result",
        "[PLACEHOLDER]",
        "\\placeholder{replace result}",
        "SIMULATED: replace before release",
        "[SIMULATED]",
        "\\simulated{replace before release}",
    ],
)
def test_explicit_unresolved_context_markers_block(tmp_path: Path, marker: str) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    draft = paper_dir / "draft_v2.tex"
    draft.write_text(draft.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8")
    refresh_bound_hashes(paper_dir)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "draft_v2_marker_remaining" in issue_codes(result)


def test_normal_todo_placeholder_simulated_and_unresolved_prose_do_not_false_positive(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    draft = paper_dir / "draft_v2.tex"
    draft.write_text(
        draft.read_text(encoding="utf-8")
        + "\nThe unresolved challenge is discussed as a limitation.\n"
        + "A placeholder variable appears only in the formal model.\n"
        + "Simulated annealing is a cited algorithm, not replacement evidence.\n"
        + "The TODO list is described historically and contains no action marker.\n",
        encoding="utf-8",
    )
    refresh_bound_hashes(paper_dir)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["status"] == "READY"
    assert result["issues"] == []


@pytest.mark.parametrize(
    ("section", "expected_code"),
    [
        ("environment", "evidence_contract_section_missing"),
        ("workflows", "evidence_contract_section_missing"),
        ("faults", "evidence_contract_section_missing"),
        ("seeds", "evidence_contract_section_missing"),
        ("metrics", "evidence_contract_section_missing"),
        ("limitations", "evidence_contract_section_missing"),
        ("raw_artifact_ids", "evidence_contract_artifact_ids_invalid"),
        ("aggregate_artifact_ids", "evidence_contract_artifact_ids_invalid"),
    ],
)
def test_schema_v2_requires_all_machine_readable_evidence_sections(
    tmp_path: Path,
    section: str,
    expected_code: str,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["evidence_contract"][section]
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert expected_code in issue_codes(result)


def test_schema_v2_binds_paper_materials_bytes(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    (paper_dir / "paper_materials.md").write_text("Status: READY\nchanged evidence narrative\n", encoding="utf-8")

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "paper_materials_hash_mismatch" in issue_codes(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing-map", "claim_evidence_missing"),
        ("empty-claims", "claim_evidence_claims_invalid"),
        ("wrong-coverage", "claim_evidence_coverage"),
        ("wrong-draft-hash", "claim_evidence_draft_hash"),
        ("missing-excerpt", "claim_evidence_excerpt_missing"),
        ("nonquantitative-excerpt", "claim_evidence_excerpt_not_quantitative"),
        ("missing-row", "claim_evidence_row_missing"),
        ("aggregate-only", "claim_evidence_raw_row_required"),
        ("duplicate-ref", "claim_evidence_ref_duplicate"),
    ],
)
def test_claim_evidence_contract_fails_closed(tmp_path: Path, mutation: str, expected_code: str) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "missing-map":
        del manifest["claim_evidence"]
    elif mutation == "empty-claims":
        manifest["claim_evidence"]["claims"] = []
    elif mutation == "wrong-coverage":
        manifest["claim_evidence"]["coverage"] = "some_quantitative_claims"
    elif mutation == "wrong-draft-hash":
        manifest["claim_evidence"]["draft_v2_sha256"] = "0" * 64
    elif mutation == "missing-excerpt":
        manifest["claim_evidence"]["claims"][0]["draft_excerpt"] = "A missing 9.99 ms claim."
    elif mutation == "nonquantitative-excerpt":
        manifest["claim_evidence"]["claims"][0]["draft_excerpt"] = "Measured Results"
    elif mutation == "missing-row":
        manifest["claim_evidence"]["claims"][0]["evidence"][0]["row_id"] = "trial-missing"
    elif mutation == "aggregate-only":
        manifest["claim_evidence"]["claims"][0]["evidence"] = [
            {"artifact_id": "aggregate-metrics", "row_id": "metric-1"}
        ]
    elif mutation == "duplicate-ref":
        manifest["claim_evidence"]["claims"][0]["evidence"].append({"artifact_id": "raw-trials", "row_id": "trial-1"})
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert expected_code in issue_codes(result)


def test_csv_raw_rows_are_supported_when_hash_bound(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    csv_path = paper_dir / "results" / "trials.csv"
    csv_path.write_text("evidence_id,latency_ms\ntrial-1,1.25\n", encoding="utf-8")
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["path"] = "results/trials.csv"
    manifest["artifacts"][0]["sha256"] = sha256(csv_path)
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["status"] == "READY"


def test_duplicate_evidence_row_ids_block_claim_binding(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    raw_path = paper_dir / "results" / "trials.jsonl"
    raw_path.write_text(
        '{"evidence_id":"trial-1","latency_ms":1.25}\n{"evidence_id":"trial-1","latency_ms":1.30}\n',
        encoding="utf-8",
    )
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["sha256"] = sha256(raw_path)
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert "claim_evidence_rows_unreadable" in issue_codes(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("self-reference", "verification_artifact_self_reference"),
        ("control-reference", "verification_artifact_control_reference"),
        ("duplicate-path", "verification_artifact_duplicate"),
        ("duplicate-id", "verification_artifact_id_duplicate"),
    ],
)
def test_manifest_rejects_self_reference_control_reference_and_duplicates(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "self-reference":
        manifest["artifacts"][0]["path"] = "results/verification_manifest.json"
        manifest["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "control-reference":
        manifest["artifacts"][0]["path"] = "draft_v2.tex"
        manifest["artifacts"][0]["sha256"] = sha256(paper_dir / "draft_v2.tex")
    elif mutation == "duplicate-path":
        manifest["artifacts"][1]["path"] = "results/trials.jsonl"
        manifest["artifacts"][1]["sha256"] = sha256(paper_dir / "results" / "trials.jsonl")
    elif mutation == "duplicate-id":
        manifest["artifacts"][1]["id"] = "RAW-TRIALS"
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["ready"] is False
    assert expected_code in issue_codes(result)


def test_schema_v1_is_readable_but_cannot_authorize_transition(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest.pop("paper_materials_sha256")
    manifest.pop("evidence_contract")
    manifest.pop("claim_evidence")
    for artifact in manifest["artifacts"]:
        artifact.pop("id")
        artifact.pop("role")
    write_json_atomic(manifest_path, manifest)

    result = validate_writing_evidence(papers_dir, "paper-1")

    assert result["status"] == "LEGACY_V1"
    assert result["legacy_read_only"] is True
    assert result["legacy_integrity_ready"] is True
    assert result["ready"] is False
    assert result["transition_ready"] is False
    assert issue_codes(result) == {"verification_manifest_upgrade_required"}


def test_completed_historical_projection_is_not_downgraded_by_legacy_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "writing"
    project = project_config(root)
    paper_dir = make_ready_fixture(Path(project["papers_dir"]))
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest.pop("paper_materials_sha256")
    manifest.pop("evidence_contract")
    manifest.pop("claim_evidence")
    for artifact in manifest["artifacts"]:
        artifact.pop("id")
        artifact.pop("role")
    write_json_atomic(manifest_path, manifest)
    (paper_dir / "draft_v0.tex").write_text("historical initial draft\n", encoding="utf-8")
    (paper_dir / "draft_v6.tex").write_text("historical final draft\n", encoding="utf-8")
    write_json_atomic(
        Path(project["state_path"]),
        {"sessions": {"paper-1": {"status": "complete", "step_done": 10}}},
    )
    monkeypatch.setattr(writing_module, "LEGACY_PROJECTS", {"writing": project})

    projection = writing_module.WritingAdapter().stage_for("paper-1")

    assert projection["status"] == "complete"
    assert "evidence_gate" not in projection


def test_gate_and_writing_projection_are_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "writing"
    project = project_config(root)
    paper_dir = make_ready_fixture(Path(project["papers_dir"]))
    manifest_path = paper_dir / "results" / "verification_manifest.json"
    manifest_path.unlink()
    write_json_atomic(
        Path(project["state_path"]),
        {
            "sessions": {"paper-1": {"status": "paused", "step_done": 5, "evidence": "old"}},
            "settings": {"scheduling_mode": "manual"},
        },
    )
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(writing_module, "LEGACY_PROJECTS", {"writing": project})

    gate_result = validate_writing_evidence(Path(project["papers_dir"]), "paper-1")
    projection = writing_module.WritingAdapter().stage_for("paper-1")

    after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert gate_result["ready"] is False
    assert projection["status"] == "blocked"
    assert projection["evidence_gate"]["status"] == "BLOCKED"
    assert "verification_manifest_missing" in projection["detail"]
    assert after == before


def test_cli_returns_compact_ready_and_blocked_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = make_ready_fixture(papers_dir)
    project = {"papers_dir": papers_dir}
    monkeypatch.setattr(cli_module, "LEGACY_PROJECTS", {"writing": project})

    assert cli_module.main(["writing-evidence-gate", "paper-1"]) == 0
    ready_output = capsys.readouterr().out.strip()
    assert "\n" not in ready_output
    assert json.loads(ready_output)["status"] == "READY"

    (paper_dir / "paper_materials.md").write_text("Status: BLOCKED\n", encoding="utf-8")
    assert cli_module.main(["writing-evidence-gate", "paper-1"]) != 0
    blocked_output = capsys.readouterr().out.strip()
    assert "\n" not in blocked_output
    assert json.loads(blocked_output)["status"] == "BLOCKED"


def test_new_session_is_auto_continuation_but_not_unbounded_auto_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "writing"
    project = project_config(root)
    state_path = Path(project["state_path"])
    write_json_atomic(
        state_path,
        {
            "settings": {
                "scheduling_mode": "manual",
                "daily_quota": 0,
                "auto_create_new_session": True,
            },
            "commands": [],
        },
    )
    monkeypatch.setattr(writing_module, "LEGACY_PROJECTS", {"writing": project})
    runtime = Mock()
    runtime.assert_revalidation_transaction_resolved = Mock(return_value=None)
    runtime.prepare_pipeline_mutation.return_value = {"recovered": False, "status": "none"}
    runtime.ensure_ready.return_value = {"ok": True, "authenticated": True}
    runtime.start_background.return_value = {"started": True, "already_running": False}
    adapter = writing_module.WritingAdapter(runtime=runtime)
    enabled: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        adapter.automation,
        "read_statuses",
        lambda _stage: [{"id": "writing-scheduler", "status": "PAUSED", "path": "test"}],
    )

    def fake_set_enabled(stage: str, enabled_value: bool, _state: dict) -> dict:
        enabled.append((stage, enabled_value))
        return {"status": "ACTIVE" if enabled_value else "PAUSED"}

    monkeypatch.setattr(adapter.automation, "set_enabled", fake_set_enabled)
    monkeypatch.setattr(
        adapter.automation,
        "require_controllable",
        lambda _stage, _state=None: Path("test"),
    )

    result = adapter.new_session()
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["action"] == "new_session"
    assert state["settings"]["scheduling_mode"] == "auto"
    assert state["settings"]["dispatch_mode"] == "sdk_event_driven"
    assert state["settings"]["writing_dispatch_protocol"] == "sdk_events_v1"
    assert state["settings"]["auto_create_new_session"] is False
    params = state["commands"][-1]["params"]
    assert params["auto_create_new_session"] is False
    assert params["batch_size"] == 1
    assert str(params["request_id"]).startswith("new-writing-batch-")
    assert enabled == [("writing", False)]
    runtime.ensure_ready.assert_called_once_with()
    runtime.start_background.assert_called_once_with(
        priority_paper_keys=[state["commands"][-1]["sdk_paper_key"]]
    )


def test_enabling_writing_reconciles_stale_unlimited_automation_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "writing"
    project = project_config(root)
    automation_root = tmp_path / "automations"
    automation_path = automation_root / "writing-scheduler" / "automation.toml"
    automation_path.parent.mkdir(parents=True)
    automation_path.write_text(
        "\n".join(
            [
                "version = 1",
                'id = "writing-scheduler"',
                'kind = "heartbeat"',
                'name = "stale unlimited scheduler"',
                'prompt = "daily_quota <= 0 means unlimited; always create the next paper"',
                'status = "PAUSED"',
                'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                'target_thread_id = "thread-1"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(automation_module, "LEGACY_PROJECTS", {"writing": project})
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", automation_root)
    monkeypatch.setattr(
        automation_module,
        "backup_file",
        lambda path, label="": str(tmp_path / "backups" / f"{label}_{Path(path).name}"),
    )

    controller = automation_module.SchedulerAutomationController(
        execution_mode_store=AIProviderStore(root=tmp_path / "ai_providers")
    )
    result = controller.set_enabled(
        "writing",
        True,
        {"settings": {"daily_quota": 0, "auto_create_new_session": False}},
    )

    updated = automation_path.read_text(encoding="utf-8")
    assert result["enabled"] is True
    assert 'status = "ACTIVE"' in updated
    assert "sole opt-in" in updated
    assert "authorizes exactly one new Step 1 thread" in updated
    assert "UNIFIED_WRITING_PARALLEL_DISPATCH_V1" in updated
    assert "writing-parallel-prepare" in updated
    assert automation_module.writing_parallel_launcher_verified(automation_path) is True
    assert "always create the next paper" not in updated


def test_orphan_step1_idea_is_visible_and_resumable_in_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "writing"
    project = project_config(root)
    ideas_dir = root / "ideas"
    ideas_dir.mkdir(parents=True)
    paper_id = "2026-07-10_001_orphan-step1"
    (ideas_dir / f"{paper_id}.md").write_text("# Idea\n", encoding="utf-8")
    write_json_atomic(Path(project["state_path"]), {"settings": {"scheduling_mode": "manual"}, "sessions": {}})
    monkeypatch.setattr(writing_module, "LEGACY_PROJECTS", {"writing": project})

    adapter = writing_module.WritingAdapter()
    projection = adapter.stage_for(paper_id)

    assert paper_id in adapter.all_paper_ids()
    assert projection["status"] == "incomplete"
    assert "not registered" in projection["detail"]
