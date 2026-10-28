from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from unified_paper_orchestrator import __main__ as cli_module
from unified_paper_orchestrator import writing_novelty_gate as novelty_gate_module
from unified_paper_orchestrator.adapters import writing as writing_module
from unified_paper_orchestrator.io_utils import write_json_atomic
from unified_paper_orchestrator.models import action_for
from unified_paper_orchestrator.writing_novelty_gate import (
    validate_writing_novelty,
    validate_writing_step2_contract,
)


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


def make_bundle(root: Path, paper_id: str = "paper-1", *, manifest: bool = True) -> dict[str, Path]:
    ideas = root / "ideas"
    refs = root / "refs" / paper_id
    ideas.mkdir(parents=True)
    refs.mkdir(parents=True)
    paths = {
        "idea": ideas / f"{paper_id}.md",
        "references_md": refs / "references.md",
        "references_bib": refs / "references.bib",
        "literature_map": refs / "literature_map.md",
        "novelty_check": refs / "novelty_check.md",
        "novelty_fulltext_review": refs / "novelty_fulltext_review.md",
        "formal_claim_spec": refs / "formal_claim_spec.md",
    }
    paths["idea"].write_text("# Narrow idea\n", encoding="utf-8")
    ids = [f"2601.{index:05d}" for index in range(1, 51)]
    paths["references_md"].write_text(
        "\n".join(f"- https://arxiv.org/abs/{source_id}" for source_id in ids) + "\n",
        encoding="utf-8",
    )
    paths["references_bib"].write_text(
        "\n\n".join(
            f"@misc{{ref{index},\n  title = {{Paper {index}}},\n  url = {{https://arxiv.org/abs/{source_id}}}\n}}"
            for index, source_id in enumerate(ids, start=1)
        )
        + "\n",
        encoding="utf-8",
    )
    paths["literature_map"].write_text("# Literature map\n", encoding="utf-8")
    paths["novelty_check"].write_text(
        "# Novelty check\n\nStatus: READY\nDecision: PROCEED\n",
        encoding="utf-8",
    )
    paths["novelty_fulltext_review"].write_text(
        "# Full-text novelty review\n\nOriginal idea: PIVOT\nRevised claim: PROCEED\n",
        encoding="utf-8",
    )
    paths["formal_claim_spec"].write_text(
        "# Formal claim specification\n\nScope: dependency-closed restart state.\n",
        encoding="utf-8",
    )
    if manifest:
        write_json_atomic(
            refs / "novelty_verification_manifest.json",
            {
                "schema_version": 1,
                "paper_id": paper_id,
                "status": "READY",
                "decision": "PROCEED",
                "claim_scope": "Dependency-closed restart state under explicit workflow assumptions.",
                "close_full_text_sources": [
                    {
                        "source_id": f"close-{index}",
                        "official_url": f"https://arxiv.org/abs/2601.{index:05d}",
                        "full_text_checked": True,
                        "finding": "Does not derive dependency-minimal restart state.",
                    }
                    for index in range(1, 8)
                ],
                "traditional_full_text_sources": [
                    {
                        "source_id": f"traditional-{index}",
                        "official_url": f"https://doi.org/10.0000/example-{index}",
                        "full_text_checked": True,
                        "finding": "Provides a component primitive but not the agent-specific joint frontier.",
                    }
                    for index in range(1, 6)
                ],
                "bundle_sha256": {key: sha256(path) for key, path in paths.items()},
            },
        )
    return paths


def make_prompt_02_contract_bundle(root: Path, paper_id: str = "paper-1") -> dict[str, Path]:
    ideas = root / "ideas"
    refs = root / "refs" / paper_id
    ideas.mkdir(parents=True)
    refs.mkdir(parents=True)
    paths = {
        "idea": ideas / f"{paper_id}.md",
        "references_md": refs / "references.md",
        "references_bib": refs / "references.bib",
        "literature_map": refs / "literature_map.md",
        "novelty_check": refs / "novelty_check.md",
    }
    paths["idea"].write_text("# Idea Decision Card\n", encoding="utf-8")
    urls = [f"https://arxiv.org/abs/2601.{index:05d}" for index in range(1, 49)]
    urls.extend(["https://example.org", "https://github.com/example/project"])
    rows = [
        "| # | Title | Authors | Year | Venue | ID | Relevance | Category | Verification |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    rows.extend(
        f"| {index} | Paper {index} | Ada Lovelace | 2026 | Venue | {url} | Relevant | adjacent | verified |"
        for index, url in enumerate(urls, start=1)
    )
    paths["references_md"].write_text("\n".join(rows) + "\n", encoding="utf-8")
    paths["references_bib"].write_text(
        "\n\n".join(
            f"@misc{{ref{index},\n  title = {{Paper {index}}},\n  url = {{{url}}}\n}}"
            for index, url in enumerate(urls, start=1)
        )
        + "\n",
        encoding="utf-8",
    )
    paths["literature_map"].write_text("# Literature map\n\nFoundational and adjacent work.\n", encoding="utf-8")
    paths["novelty_check"].write_text(
        "# Novelty Check\n\n"
        "## 1. Has this been done?\nNo exact duplicate.\n\n"
        "## 2. Dangerous papers\nPaper 1.\n\n"
        "## 3. Defensible parts\nA narrow claim.\n\n"
        "## 4. Claims to avoid\nAvoid broad claims.\n\n"
        "## 5. Keep, modify, or abandon?\nRecommendation: **Keep, but narrow.**\n"
        "Decision: **Keep for the next step.**\n",
        encoding="utf-8",
    )
    return paths


def codes(result: dict) -> set[str]:
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


def test_prompt_02_contract_accepts_arxiv_and_official_sources_without_controller_only_files(
    tmp_path: Path,
) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert {key: value for key, value in result.items() if key != "input_snapshot"} == {
        "paper_id": "paper-1",
        "gate_profile": "prompt_02_contract",
        "status": "READY",
        "ready": True,
        "reference_count": 50,
        "verified_reference_count": 50,
        "bib_entry_count": 50,
        "identity_match_count": 50,
        "issues": [],
    }
    assert_ready_input_snapshot(
        result,
        tmp_path,
        set(paths.values()),
        gate_profile="prompt_02_contract",
        scope="writing_root",
    )
    assert before == after
    assert set(paths) == {
        "idea",
        "references_md",
        "references_bib",
        "literature_map",
        "novelty_check",
    }
    assert not (tmp_path / "refs" / "paper-1" / "novelty_verification_manifest.json").exists()


def test_prompt_02_contract_accepts_canonical_r_prefixed_reference_ids(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    text = paths["references_md"].read_text(encoding="utf-8")
    for index in range(50, 0, -1):
        text = text.replace(f"| {index} |", f"| R{index:02d} |", 1)
    paths["references_md"].write_text(text, encoding="utf-8")

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["status"] == "READY"
    assert result["ready"] is True
    assert result["reference_count"] == 50
    assert result["verified_reference_count"] == 50
    assert result["bib_entry_count"] == 50
    assert result["identity_match_count"] == 50
    assert result["issues"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", 1),
        ("0001", 1),
        ("R01", 1),
        ("R09", 9),
        ("R10", 10),
        ("R100", 100),
        ("R00", None),
        ("R0", None),
        ("R1", None),
        ("R001", None),
        ("r01", None),
        ("R-01", None),
        ("R01x", None),
        ("Ｒ01", None),
        ("R٠١", None),
        ("1" * 10, None),
    ],
)
def test_reference_table_number_is_ascii_bounded_and_unambiguous(value: str, expected: int | None) -> None:
    assert novelty_gate_module._reference_table_number(value) == expected


def test_prompt_02_contract_blocks_an_abandon_decision(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    paths["novelty_check"].write_text(
        "# Novelty Check\n\n"
        "## 1. Has this been done?\nYes.\n\n"
        "## 2. Dangerous papers\nMany.\n\n"
        "## 3. Defensible parts\nNone.\n\n"
        "## 4. Claims to avoid\nAll.\n\n"
        "## 5. Keep, modify, or abandon?\nDecision: **Abandon this idea.**\n",
        encoding="utf-8",
    )

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["ready"] is False
    assert "novelty_decision_abandon" in codes(result)


def test_prompt_02_contract_requires_all_fifty_cross_file_identities(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    text = paths["references_bib"].read_text(encoding="utf-8")
    paths["references_bib"].write_text(
        text.replace("https://example.org", "https://different.example.org", 1),
        encoding="utf-8",
    )

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["ready"] is False
    assert result["identity_match_count"] == 49
    assert "reference_identity_alignment_insufficient" in codes(result)


def test_prompt_02_contract_does_not_count_unverified_rows_as_verified(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    text = paths["references_md"].read_text(encoding="utf-8")
    paths["references_md"].write_text(text.replace("| verified |", "| UNVERIFIED |", 2), encoding="utf-8")

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["ready"] is False
    assert result["verified_reference_count"] == 48
    assert "verified_references_insufficient" in codes(result)


def test_prompt_02_contract_ignores_bibtex_comment_identities_and_directives(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    text = paths["references_bib"].read_text(encoding="utf-8")
    text = text.replace(
        "url = {https://example.org}",
        "% url = {https://example.org}\n  note = {No stable identifier}",
        1,
    )
    text += "\n@comment{https://example.org}\n"
    paths["references_bib"].write_text(text, encoding="utf-8")

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["ready"] is False
    assert result["bib_entry_count"] == 50
    assert result["identity_match_count"] == 49
    assert "reference_bib_identity_missing" in codes(result)


def test_prompt_02_contract_accepts_numbered_answers_without_controller_markers(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    paths["novelty_check"].write_text(
        "# Novelty Check\n\n"
        "1. No exact duplicate was found.\n\n"
        "2. The closest papers are A and B.\n\n"
        "3. The narrow systems claim remains defensible.\n\n"
        "4. Avoid all broad first-of-kind claims.\n\n"
        "5. Keep and narrow the idea before drafting.\n",
        encoding="utf-8",
    )

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["ready"] is True


def test_prompt_02_contract_rejects_placeholder_author_metadata(tmp_path: Path) -> None:
    paths = make_prompt_02_contract_bundle(tmp_path)
    text = paths["references_md"].read_text(encoding="utf-8")
    paths["references_md"].write_text(text.replace("Ada Lovelace", "arXiv authors", 1), encoding="utf-8")

    result = validate_writing_step2_contract(tmp_path, "paper-1")

    assert result["ready"] is False
    assert "reference_author_missing_or_placeholder" in codes(result)


def test_ready_novelty_bundle_passes_and_is_read_only(tmp_path: Path) -> None:
    paths = make_bundle(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = validate_writing_novelty(tmp_path, "paper-1")

    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert result["status"] == "READY"
    assert result["ready"] is True
    assert result["verified_reference_count"] == 50
    assert result["close_full_text_count"] == 7
    assert result["traditional_full_text_count"] == 5
    assert before == after
    assert set(paths) == {
        "idea",
        "references_md",
        "references_bib",
        "literature_map",
        "novelty_check",
        "novelty_fulltext_review",
        "formal_claim_spec",
    }


def test_novelty_gate_reads_each_input_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = make_bundle(tmp_path)
    manifest_path = tmp_path / "refs" / "paper-1" / "novelty_verification_manifest.json"
    expected = {_path.absolute() for _path in (*paths.values(), manifest_path)}
    reads: Counter[Path] = Counter()
    original_open = Path.open

    def counted_open(path: Path, mode: str = "r", *args, **kwargs):
        if mode == "rb" and path.absolute() in expected:
            reads[path.absolute()] += 1
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["status"] == "READY"
    assert reads == Counter({path: 1 for path in expected})


def test_novelty_gate_fails_if_snapshot_changes_before_validation_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_bundle(tmp_path)
    original = novelty_gate_module._validate_review_sources
    mutated = False

    def mutate_after_snapshot(*args, **kwargs):
        nonlocal mutated
        result = original(*args, **kwargs)
        if not mutated:
            mutated = True
            paths["idea"].write_text("changed during validation\n", encoding="utf-8")
        return result

    monkeypatch.setattr(novelty_gate_module, "_validate_review_sources", mutate_after_snapshot)

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert "novelty_snapshot_changed" in codes(result)


def test_novelty_gate_fails_if_file_mtime_changes_during_its_single_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_bundle(tmp_path)
    target = paths["idea"].absolute()
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

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert "idea_missing_unstable" in codes(result)


def test_novelty_gate_rejects_symlinked_input_even_when_bytes_match(tmp_path: Path) -> None:
    paths = make_bundle(tmp_path)
    link = paths["literature_map"]
    outside = tmp_path / "outside-literature.md"
    outside.write_bytes(link.read_bytes())
    link.unlink()
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert "literature_map_missing_unsafe" in codes(result)


def test_novelty_gate_rejects_simulated_junction_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_bundle(tmp_path)
    junction = (tmp_path / "refs" / "paper-1").absolute()
    monkeypatch.setattr(
        novelty_gate_module.os.path,
        "isjunction",
        lambda path: Path(path).absolute() == junction,
        raising=False,
    )

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert any(code.endswith("_unsafe") for code in codes(result))


def test_novelty_gate_rejects_symlinked_writing_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-writing"
    make_bundle(real_root)
    linked_root = tmp_path / "linked-writing"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = validate_writing_novelty(linked_root, "paper-1")

    assert result["ready"] is False
    assert "novelty_root_unsafe" in codes(result)


@pytest.mark.parametrize(
    ("raw_status", "evidence", "expected_detail"),
    [
        (
            "paused",
            "novelty full-text gate required",
            "step_done=2; Step 2->3 novelty gate READY",
        ),
        (
            "queued",
            "Step 2->3 novelty gate BLOCKED: full-text review required",
            "step_done=2; Step 2->3 novelty gate READY",
        ),
        (
            "paused",
            "operator paused for manuscript review",
            "step_done=2; operator paused for manuscript review",
        ),
    ],
)
def test_ready_novelty_gate_supersedes_only_stale_gate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_status: str,
    evidence: str,
    expected_detail: str,
) -> None:
    root = tmp_path / "writing"
    make_prompt_02_contract_bundle(root)
    project = project_config(root)
    write_json_atomic(
        Path(project["state_path"]),
        {
            "sessions": {
                "paper-1": {
                    "status": raw_status,
                    "step_done": 2,
                    "evidence": evidence,
                }
            },
            "settings": {"scheduling_mode": "manual"},
        },
    )
    monkeypatch.setattr(writing_module, "LEGACY_PROJECTS", {"writing": project})

    projection = writing_module.WritingAdapter().stage_for("paper-1")
    next_action, displayed_reason = action_for("writing", {"writing": projection})

    assert projection["status"] == raw_status
    assert projection["raw_status"] == raw_status
    assert projection["novelty_gate"]["ready"] is True
    assert projection["detail"] == expected_detail
    assert next_action == "wait_writing"
    assert displayed_reason == expected_detail


def test_metadata_only_bundle_blocks_before_drafting(tmp_path: Path) -> None:
    paths = make_bundle(tmp_path, manifest=False)
    paths["novelty_check"].write_text(
        "# Novelty check\n\nBroad novelty rejected. Narrow novelty unconfirmed. Full-text gate required before Step 3.\n",
        encoding="utf-8",
    )

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert {
        "novelty_status_not_ready",
        "novelty_decision_not_proceed",
        "novelty_manifest_missing",
    } <= codes(result)


def test_bundle_hash_and_source_thresholds_fail_closed(tmp_path: Path) -> None:
    paths = make_bundle(tmp_path)
    manifest_path = tmp_path / "refs" / "paper-1" / "novelty_verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["close_full_text_sources"] = manifest["close_full_text_sources"][:2]
    write_json_atomic(manifest_path, manifest)
    paths["idea"].write_text("changed after review\n", encoding="utf-8")

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert "close_full_text_sources_insufficient" in codes(result)
    assert "novelty_bundle_hash_mismatch" in codes(result)


@pytest.mark.parametrize(
    ("artifact_key", "missing_code"),
    [
        ("novelty_fulltext_review", "novelty_fulltext_review_missing"),
        ("formal_claim_spec", "formal_claim_spec_missing"),
    ],
)
def test_required_novelty_artifact_missing_fails_closed(
    tmp_path: Path,
    artifact_key: str,
    missing_code: str,
) -> None:
    paths = make_bundle(tmp_path)
    paths[artifact_key].unlink()

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert missing_code in codes(result)


@pytest.mark.parametrize("artifact_key", ["novelty_fulltext_review", "formal_claim_spec"])
def test_required_novelty_artifact_hash_mismatch_fails_closed(
    tmp_path: Path,
    artifact_key: str,
) -> None:
    paths = make_bundle(tmp_path)
    paths[artifact_key].write_text("tampered after verification\n", encoding="utf-8")

    result = validate_writing_novelty(tmp_path, "paper-1")

    assert result["ready"] is False
    assert any(
        issue.get("code") == "novelty_bundle_hash_mismatch" and issue.get("path") == artifact_key
        for issue in result["issues"]
    )


def test_cli_emits_compact_blocked_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    make_bundle(tmp_path, manifest=False)
    monkeypatch.setattr(cli_module, "LEGACY_PROJECTS", {"writing": {"root": tmp_path}})

    assert cli_module.main(["writing-novelty-gate", "paper-1"]) == 2
    output = capsys.readouterr().out.strip()
    assert "\n" not in output
    assert json.loads(output)["status"] == "BLOCKED"


@pytest.mark.parametrize("paper_id", ["../escape", "C:\\escape", "a/b", ""])
def test_invalid_paper_ids_are_rejected(tmp_path: Path, paper_id: str) -> None:
    result = validate_writing_novelty(tmp_path, paper_id)
    assert result["ready"] is False
    assert "invalid_paper_id" in codes(result)
