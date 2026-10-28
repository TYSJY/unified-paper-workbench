from __future__ import annotations

from unified_paper_orchestrator.models import action_for
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator
from unified_paper_orchestrator.ui.main_window import UnifiedControlPanel


def _stage(status: str, artifact: str = "") -> dict[str, object]:
    return {"status": status, "artifact": artifact, "detail": ""}


def test_completed_upstream_is_automatically_admitted_to_next_stage() -> None:
    orchestrator = UnifiedOrchestrator.__new__(UnifiedOrchestrator)
    stages = {
        "writing": _stage("complete", "draft.tex"),
        "figures": _stage("missing"),
        "journal_author": _stage("missing"),
        "submission": _stage("missing"),
    }

    admissions = orchestrator._stage_admissions_for(stages, {})

    assert admissions["figures"]["admitted"] is True
    assert admissions["figures"]["mode"] == "automatic"
    assert admissions["journal_author"]["admitted"] is False


def test_downstream_folder_does_not_forge_upstream_completion_or_admission() -> None:
    orchestrator = UnifiedOrchestrator.__new__(UnifiedOrchestrator)
    stages = {
        "writing": _stage("incomplete", "draft_v2.tex"),
        "figures": _stage("incomplete", "old-figure-folder"),
        "journal_author": _stage("missing"),
        "submission": _stage("missing"),
    }

    orchestrator._infer_upstream_handoffs(stages)
    admissions = orchestrator._stage_admissions_for(stages, {})

    assert stages["writing"]["status"] == "incomplete"
    assert admissions["figures"]["admitted"] is False
    assert UnifiedControlPanel.stage_row_visible(
        None, "figures", {"stage_admissions": admissions}, stages["figures"]
    ) is False


def test_downstream_completion_cannot_skip_a_broken_admission_chain() -> None:
    orchestrator = UnifiedOrchestrator.__new__(UnifiedOrchestrator)
    stages = {
        "writing": _stage("incomplete", "draft_v2.tex"),
        "figures": _stage("complete", "old-figure-folder"),
        "journal_author": _stage("complete", "old-journal-folder"),
        "submission": _stage("submitted", "old-submission-folder"),
    }

    admissions = orchestrator._stage_admissions_for(stages, {})

    assert admissions["figures"]["admitted"] is False
    assert admissions["journal_author"]["admitted"] is False
    assert admissions["journal_author"]["upstream_admitted"] is False
    assert admissions["submission"]["admitted"] is False


def test_manual_override_changes_effective_stage_and_bypasses_only_upstream_gate() -> None:
    orchestrator = UnifiedOrchestrator.__new__(UnifiedOrchestrator)
    stages = {
        "writing": _stage("incomplete", "draft_v2.tex"),
        "figures": _stage("missing"),
        "journal_author": _stage("missing"),
        "submission": _stage("missing"),
    }
    overrides = {
        "figures": {
            "admitted": True,
            "mode": "manual_override",
            "at": "2026-07-15T12:00:00+08:00",
            "reason": "operator accepted current draft",
        }
    }

    admissions = orchestrator._stage_admissions_for(stages, overrides)
    current = orchestrator._effective_current_stage(stages, admissions)
    action, _reason = action_for(current, stages, allow_upstream_incomplete=True)

    assert current == "figures"
    assert admissions["figures"]["mode"] == "manual_override"
    assert action == "queue_figures"
    assert UnifiedControlPanel.stage_row_visible(
        None, "figures", {"stage_admissions": admissions}, stages["figures"]
    ) is True
