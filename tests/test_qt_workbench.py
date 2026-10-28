from __future__ import annotations

import json

from unified_paper_orchestrator.ui_next.controller import (
    _read_submission_monitor_summary,
)
from unified_paper_orchestrator.ui_next.models import PaperListModel
from unified_paper_orchestrator.ui_next.read_model import (
    metrics_for,
    paper_rows,
    selected_paper,
)


def _state() -> dict:
    return {
        "papers": {
            "2026-07-22_001_contract-test": {
                "paper_id": "2026-07-22_001_contract-test",
                "title": "Contract Test",
                "current_stage": "figures",
                "next_action": "queue_figures",
                "stages": {
                    "writing": {
                        "status": "complete",
                        "step_done": 10,
                        "workflow_step_count": 10,
                    },
                    "figures": {
                        "status": "incomplete",
                        "raw_status": "complete",
                        "step_done": 3,
                        "output_contract": {
                            "ok": False,
                            "status": "incomplete",
                            "issues": ["missing manifest"],
                        },
                        "admission": {"admitted": True},
                    },
                    "journal_author": {"status": "missing"},
                    "submission": {"status": "missing"},
                },
            }
        },
        "history": [],
    }


def test_contract_failure_never_renders_impossible_step_number() -> None:
    row = paper_rows(_state())[0]

    assert row["figuresStatus"] == "needs_review"
    assert row["figuresStatusLabel"] == "验收未通过"
    assert row["figuresStatusTone"] == "attention"
    assert row["figuresProgress"] == 1.0
    assert row["figuresStepLabel"] == "已执行 3/3 · 待验收"
    assert "4/3" not in row["figuresStepLabel"]


def test_stage_page_projects_stage_specific_columns() -> None:
    model = PaperListModel()
    model.replace(paper_rows(_state()))
    model.configure(page="figures")

    visible = model.get(0)
    assert visible["currentStage"] == "图表与插图"
    assert visible["status"] == "needs_review"
    assert visible["statusLabel"] == "验收未通过"
    assert visible["progress"] == 1.0
    assert visible["nextActionLabel"] == "继续图表与插图"


def test_stage_detail_uses_focused_stage_instead_of_global_average() -> None:
    detail = selected_paper(
        _state(),
        "2026-07-22_001_contract-test",
        "figures",
    )

    assert detail["currentStage"] == "图表与插图"
    assert detail["progress"] == 1.0
    assert detail["statusLabel"] == "验收未通过"
    assert detail["nextActionLabel"] == "继续图表与插图"


def test_stage_metrics_classify_review_as_pending_not_complete() -> None:
    metrics = {item["key"]: item["value"] for item in metrics_for(paper_rows(_state()), "figures")}

    assert metrics == {
        "total": 1,
        "active": 0,
        "pending": 1,
        "human": 0,
        "complete": 0,
    }


def test_submission_monitor_summary_is_persistent_and_structured(tmp_path) -> None:
    state_path = tmp_path / "smart_submission_state.json"
    state_path.write_text(
        json.dumps(
            {
                "settings": {
                    "daily_monitor_current_run_id": "",
                    "daily_monitor_last_finished_at": "2026-07-23T15:20:04+08:00",
                    "daily_monitor_last_status": "completed",
                    "daily_monitor_last_paper_count": 6,
                    "daily_monitor_last_checked_count": 6,
                    "daily_monitor_last_failed_count": 0,
                    "daily_monitor_last_pending_count": 0,
                    "daily_monitor_last_action_required_count": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    assert _read_submission_monitor_summary(state_path) == {
        "hasRun": True,
        "status": "completed",
        "tone": "success",
        "finishedAt": "2026-07-23T15:20:04+08:00",
        "requested": 6,
        "checked": 6,
        "failed": 0,
        "pending": 0,
        "actionRequired": 1,
    }
