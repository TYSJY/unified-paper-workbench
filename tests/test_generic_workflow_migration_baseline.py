from __future__ import annotations

import json
from pathlib import Path

from unified_paper_orchestrator import __version__
from unified_paper_orchestrator.figure_prompt_integrity import FIGURE_PROMPT_SHA256
from unified_paper_orchestrator.figure_workflow import default_figure_workflow_snapshot
from unified_paper_orchestrator.journal_author_prompt_integrity import (
    JOURNAL_AUTHOR_PROMPT_SHA256,
)
from unified_paper_orchestrator.journal_author_workflow import (
    default_journal_author_workflow_snapshot,
)
from unified_paper_orchestrator.writing_prompt_integrity import (
    TUNED_WRITING_PROMPT_SHA256,
)
from unified_paper_orchestrator.writing_workflow import default_workflow_snapshot


BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "baselines"
    / "GENERIC_WORKFLOW_BASELINE_0.12.0b3.json"
)


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_generic_workflow_migration_baseline_is_bound_to_product_version() -> None:
    baseline = _baseline()

    assert baseline["schema"] == "unified-paper-orchestrator/generic-workflow-migration-baseline/v1"
    # The migration snapshot remains immutable after GUI-only releases.
    assert baseline["source"]["version"] == "0.12.0b3"
    assert __version__ == "0.12.0b11"


def test_generic_workflow_migration_baseline_preserves_builtin_workflow_shapes() -> None:
    workflows = _baseline()["active_workflows"]

    # Legacy workflow hashes include absolute prompt paths and therefore vary
    # between the production workspace and the test-isolated repository root.
    # Prompt bytes are protected separately below; P1 introduces a portable
    # definition digest that does not conflate path binding with content.
    assert workflows["writing"]["step_count"] == default_workflow_snapshot()["step_count"] == 10
    assert workflows["figures"]["step_count"] == default_figure_workflow_snapshot()["step_count"] == 3
    journal = default_journal_author_workflow_snapshot()
    assert workflows["journal_author"]["step_count"] == journal["step_count"] == 1
    assert workflows["journal_author"]["mode"] == journal["mode"] == "combined"


def test_generic_workflow_migration_baseline_preserves_prompt_seals() -> None:
    seals = _baseline()["prompt_seals"]

    writing = {int(item["step"]): str(item["sha256"]).lower() for item in seals["writing"]}
    figures = {int(item["step"]): str(item["sha256"]).upper() for item in seals["figures"]}
    journal_author = {
        str(item["name"]): str(item["sha256"]).upper()
        for item in seals["journal_author"]
    }

    assert writing == dict(TUNED_WRITING_PROMPT_SHA256)
    assert figures == dict(FIGURE_PROMPT_SHA256)
    assert journal_author == dict(JOURNAL_AUTHOR_PROMPT_SHA256)
