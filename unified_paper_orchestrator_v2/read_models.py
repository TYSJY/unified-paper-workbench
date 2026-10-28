from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def plan_for_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build the V1-compatible action plan from an immutable input snapshot.

    This module is intentionally pure: it imports no store, executor, browser,
    network, filesystem, or clock code. It is therefore suitable for shadow
    comparison before any authoritative V1 action runs.
    """

    paper_id = str(snapshot.get("paper_id") or "")
    record_value = snapshot.get("record", {})
    record = record_value if isinstance(record_value, Mapping) else {}
    stage_labels_value = snapshot.get("stage_labels", {})
    stage_labels = stage_labels_value if isinstance(stage_labels_value, Mapping) else {}

    action = str(record.get("next_action") or "none")
    stage = str(record.get("current_stage") or "")
    plan: dict[str, Any] = {
        "paper_id": paper_id,
        "stage": stage,
        "stage_label": str(stage_labels.get(stage) or stage),
        "action": action,
        "action_label": str(snapshot.get("action_label") or ""),
        "writes": [],
    }
    if action == "queue_writing":
        plan["writes"] = ["writing.pipeline_state.json: commands += resume_session; sessions[paper_id].status=queued"]
    elif action == "queue_figures":
        plan["writes"] = [
            "figures/papers/<paper_id>/input.tex <- writing draft_v6.tex",
            "figure_pipeline_state.json: paper_queue += paper_id; commands += run_pipeline_late",
        ]
    elif action == "queue_journal_author":
        plan["writes"] = [
            "journal/papers/<paper_id>/source_submit <- figure submit/",
            "journal_author_state.json: paper_queue += paper_id; commands += run_pipeline_02_03",
        ]
    elif action == "queue_submission":
        route_value = record.get("submission_route", {})
        route = route_value if isinstance(route_value, Mapping) else {}
        owner = str(route.get("execution_owner") or "legacy_auto")
        plan["execution_owner"] = owner
        if owner == "legacy_auto":
            plan["writes"] = [
                "auto_submission_panel_state.json: papers[paper_id].source_package_dir=journal package/",
                "auto_submission_panel_state.json: paper_queue += paper_id; settings.scheduling_mode=run_once",
            ]
        else:
            plan["writes"] = [f"none; submission execution owner is {owner}"]
    elif action.startswith("wait_"):
        plan["writes"] = ["none; waiting for upstream/current stage"]
    else:
        plan["writes"] = ["none"]
    return plan

