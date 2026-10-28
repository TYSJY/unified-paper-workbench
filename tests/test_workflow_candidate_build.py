from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "tools" / "build_workflow_candidate.ps1"
ENTRY_POINT = ROOT / "tools" / "workflow_candidate.py"
CANDIDATE_MODULE = ROOT / "unified_paper_orchestrator" / "workflow" / "candidate.py"
VERSION_FILE = ROOT / "tools" / "windows_workflow_candidate_version_info.txt"
RELEASE_NOTES = ROOT / "docs" / "GENERIC_WORKFLOW_RELEASE_P9_2026-08-05.md"


def test_candidate_build_is_console_only_and_candidate_scoped() -> None:
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'Join-Path $root "build\\workflow_candidate"' in source
    remove_lines = [line.strip() for line in source.splitlines() if "Remove-Item" in line]
    assert remove_lines == ["Remove-Item -LiteralPath $candidateRoot -Recurse -Force"]
    assert "--onefile" in source
    assert "--console" in source
    assert "--windowed" not in source
    assert "--hide-console" not in source
    assert '"--name", "GenericWorkflowCandidate"' in source
    assert '"--exclude-module", "PySide6"' in source
    assert '"--exclude-module", "unified_paper_orchestrator.app"' in source
    assert "windows_workflow_candidate_version_info.txt" in source
    assert "workflow\\packs\\prompts\\undergraduate" in source
    assert "tools\\workflow_candidate.py" in source
    assert "Assert-NotCandidateReparsePoint -Path $root" in source
    assert "Assert-NotCandidateReparsePoint -Path $expectedBuildParent" in source
    assert "Assert-NotCandidateReparsePoint -Path $candidateRoot" in source
    assert "GenericWorkflowCandidate.exe.sha256" in source
    assert "rollback-drill.json" in source
    assert "UnifiedPaperOrchestrator.exe 0.12.0b3" in source
    assert "pre-generic-workflow-0.12.0b3-20260801" in source
    assert "7f33ff4ee9ccd1dc34e7e01a21a9483c73147836" in source
    assert "tools\\build_exe.ps1" not in source
    assert "UnifiedPaperOrchestrator.exe" in source  # read-only fingerprints only
    assert '"status", "--porcelain=v1", "--untracked-files=all"' in source
    assert "Refusing to attribute a candidate build to a dirty Git tree" in source
    assert not re.search(r"(?:Copy|Move|Remove)-Item[^\n]+productionDist", source)
    assert source.index("tests/test_workflow_candidate.py") < source.index(
        '"Building isolated pure-console candidate..."'
    )


def test_candidate_entry_disables_registry_before_import_and_avoids_gui() -> None:
    source = ENTRY_POINT.read_text(encoding="utf-8")
    candidate = CANDIDATE_MODULE.read_text(encoding="utf-8")

    disable = 'os.environ["UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"] = "1"'
    assert source.index(disable) < source.index(
        "from unified_paper_orchestrator.workflow.candidate import"
    )
    for forbidden in (
        "PySide6",
        "ui_next",
        "unified_paper_orchestrator.app",
        "unified_paper_orchestrator.orchestrator",
        "UnifiedOrchestrator",
        "pipeline_state.json",
    ):
        assert forbidden not in source
        assert forbidden not in candidate


def test_candidate_version_metadata_is_distinct_from_production() -> None:
    source = VERSION_FILE.read_text(encoding="utf-8")

    assert "GenericWorkflowCandidate.exe" in source
    assert "Generic Workflow Candidate" in source
    assert "0.13.1-candidate" in source
    assert "UnifiedPaperOrchestrator.exe" not in source


def test_release_notes_keep_offline_simulation_outside_production_claims() -> None:
    source = RELEASE_NOTES.read_text(encoding="utf-8")

    for required in (
        "8378e75834a21fc56e8038c0b17d31edadf1b5c2231ed5b3f0954c5047c5d371",
        "211,944,903",
        "GenericWorkflowCandidate.exe",
        "NOT_RUN",
        "production_equivalence=false",
        "contract simulation",
        "0.12.0b3",
    ):
        assert required in source
