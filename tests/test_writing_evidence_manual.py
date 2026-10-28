from __future__ import annotations

import json
from pathlib import Path

from unified_paper_orchestrator.writing_evidence_gate import validate_writing_evidence
from unified_paper_orchestrator.writing_evidence_manual import (
    prepare_manual_writing_evidence,
    prepare_policy_trusted_writing_evidence,
)


def test_manual_completion_leaves_markers_for_step6_and_binds_exact_draft(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "paper-1"
    (paper_dir / "results").mkdir(parents=True)
    draft = paper_dir / "draft_v2.tex"
    original = "\\section{Measured Results}\nLatency: 【验】 VERIFY\n"
    draft.write_text(original, encoding="utf-8")

    preview = prepare_manual_writing_evidence(papers_dir, "paper-1", check_only=True)

    assert preview["ok"] is True
    assert preview["remaining_marker_count"] == 2
    assert not (paper_dir / "results" / "manual_data_completion.json").exists()

    committed = prepare_manual_writing_evidence(
        papers_dir,
        "paper-1",
        check_only=False,
        expected_draft_sha256=preview["draft_v2_sha256"],
    )

    assert committed["status"] == "RECORDED"
    assert committed["transition_ready"] is True
    assert draft.read_text(encoding="utf-8") == original

    record_path = paper_dir / "results" / "manual_data_completion.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["marker_cleanup"] == "step_6"
    assert record["draft_v2_sha256"] == preview["draft_v2_sha256"]

    gate = validate_writing_evidence(papers_dir, "paper-1")
    assert gate["status"] == "READY"
    assert gate["manual_completion"] is True
    assert gate["transition_mode"] == "manual_edit"
    assert gate["issues"] == []

    draft.write_text(original + "Changed after confirmation.\n", encoding="utf-8")
    changed_gate = validate_writing_evidence(papers_dir, "paper-1")
    assert changed_gate["ready"] is False
    assert {item["code"] for item in changed_gate["issues"]} == {
        "manual_completion_draft_hash_mismatch"
    }


def test_workspace_policy_completion_leaves_markers_and_binds_exact_draft(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "paper-1"
    (paper_dir / "results").mkdir(parents=True)
    draft = paper_dir / "draft_v2.tex"
    original = "Measured latency 5.2 GB 【验】 VERIFY\n"
    draft.write_text(original, encoding="utf-8")
    policy = {
        "schema": "unified-paper-orchestrator/writing-gate-policy/v1",
        "gate_id": "step5_data_confirmation",
        "mode": "auto_trust",
        "revision": 3,
        "updated_at": "2026-07-18T18:00:00+08:00",
    }

    committed = prepare_policy_trusted_writing_evidence(
        papers_dir,
        "paper-1",
        policy_snapshot=policy,
        check_only=False,
    )

    assert committed["status"] == "RECORDED"
    assert committed["remaining_marker_count"] == 2
    assert draft.read_text(encoding="utf-8") == original
    gate = validate_writing_evidence(papers_dir, "paper-1")
    assert gate["ready"] is True
    assert gate["policy_completion"] is True
    assert gate["manual_completion"] is False
    assert gate["transition_mode"] == "workspace_auto_trust"
    assert gate["gate_profile"] == "evidence_auto_trust_transition"
    assert gate["policy_snapshot"] == policy

    draft.write_text(original + "changed\n", encoding="utf-8")
    changed_gate = validate_writing_evidence(papers_dir, "paper-1")
    assert changed_gate["ready"] is False
    assert {item["code"] for item in changed_gate["issues"]} == {
        "manual_completion_draft_hash_mismatch"
    }
