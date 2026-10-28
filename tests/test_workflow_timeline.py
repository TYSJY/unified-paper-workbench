from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from unified_paper_orchestrator.workflow.contracts import (
    NodeDefinition,
    NodeType,
    SessionPolicy,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
)
from unified_paper_orchestrator.workflow.store import WorkflowStore, workflow_database_path
from unified_paper_orchestrator.workflow.timeline import WorkflowTimelineService


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def test_timeline_projects_consistent_whitelisted_run_state(tmp_path: Path) -> None:
    store = WorkflowStore(workflow_database_path(tmp_path))
    store.initialize()
    definition = WorkflowDefinition(
        "custom.timeline",
        "Timeline workflow",
        (
            NodeDefinition(
                "review",
                "Review",
                NodeType.HUMAN,
                10,
                session_policy=SessionPolicy(session_group="review", reuse_within_run=True),
            ),
        ),
        (),
        metadata={"builtin": False, "readonly": False},
    )
    draft = store.append_custom_version(definition, now=NOW)["version"]
    version = store.publish_custom_version(draft.version_id)["version"]
    binding = WorkflowRuntimeBinding(
        definition_sha256=version.definition_sha256,
        resources=(),
        binding_id="custom.timeline.local",
    )
    private_input = "private-input-value"
    private_metadata = "private-metadata-value"
    run = store.create_run(
        version,
        binding,
        inputs={"private": private_input},
        metadata={"private": private_metadata},
        now=NOW,
    )
    session = store.get_or_create_session(run["run_id"], "review", "human", now=NOW)
    node = store.latest_node_runs(run["run_id"])[0]
    store.mark_node_ready(node["node_run_id"], now=NOW)
    grant = store.claim_node(node["node_run_id"], "timeline-worker", now=NOW)
    assert grant is not None
    store.attach_session(grant, session["session_record_id"], now=NOW)
    external_session = "private-external-session-123456"
    store.bind_external_session(
        grant,
        session["session_record_id"],
        external_session,
        expected_generation=session["generation"],
        now=NOW,
    )

    timeline = WorkflowTimelineService(store).snapshot(run["run_id"])
    payload = json.dumps(timeline.to_dict(), sort_keys=True)

    assert timeline.workflow_id == "custom.timeline"
    assert timeline.status == "running"
    assert timeline.total_nodes == 1
    assert timeline.completed_nodes == 0
    assert timeline.progress == 0.0
    assert timeline.node_attempts[0].node_id == "review"
    assert [item.sequence for item in timeline.events] == list(range(1, len(timeline.events) + 1))
    assert timeline.sessions[0].external_session_bound is True
    assert len(timeline.sessions[0].external_session_sha256) == 64
    assert external_session not in payload
    assert private_input not in payload
    assert private_metadata not in payload
    assert "fencing_token" not in payload


def test_timeline_module_does_not_scan_legacy_state_or_workspaces() -> None:
    source = Path("unified_paper_orchestrator/workflow/timeline.py").read_text(encoding="utf-8")
    forbidden = (
        "pipeline_state.json",
        "sdk_dispatch",
        "UnifiedOrchestrator",
        "legacy_shadow",
        "glob(",
        "rglob(",
    )
    assert not any(item in source for item in forbidden)
