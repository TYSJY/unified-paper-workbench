from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from unified_paper_orchestrator.workflow.examples import generic_example_implementations
from unified_paper_orchestrator.workflow.executors import DeterministicExecutor, ExecutorRegistry
from unified_paper_orchestrator.workflow.library import WorkflowLibrary
from unified_paper_orchestrator.workflow.packs.examples import (
    OFFLINE_DEMO_WORKFLOW_ID,
    build_generic_examples_pack,
    offline_demo_definition,
)
from unified_paper_orchestrator.workflow.resources import (
    ContentAddressedPromptStore,
    prompt_resource_directory,
)
from unified_paper_orchestrator.workflow.scheduler import SchedulerConfig, WorkflowScheduler
from unified_paper_orchestrator.workflow.store import WorkflowStore, workflow_database_path


NOW = datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc)
SEMANTIC_ORDER = ["normalize-request", "build-action-plan", "render-summary"]


def _library(tmp_path: Path) -> WorkflowLibrary:
    library = WorkflowLibrary(
        WorkflowStore(workflow_database_path(tmp_path)),
        ContentAddressedPromptStore(prompt_resource_directory(tmp_path)),
    )
    library.initialize()
    return library


def test_offline_demo_contract_uses_edges_as_semantic_order() -> None:
    definition = offline_demo_definition()

    assert len(definition.nodes) == len(SEMANTIC_ORDER)
    assert [node.node_id for node in definition.nodes] != SEMANTIC_ORDER
    assert [node.node_id for node in sorted(definition.nodes, key=lambda node: node.display_order)] != SEMANTIC_ORDER
    assert [(edge.source_node_id, edge.target_node_id) for edge in definition.edges] == [
        ("normalize-request", "build-action-plan"),
        ("build-action-plan", "render-summary"),
    ]
    assert definition.inputs[0].artifact_id == "request"
    assert definition.inputs[0].media_type == "application/json"
    assert all(node.node_type.value == "deterministic" for node in definition.nodes)
    portable_contract = definition.canonical_json().casefold()
    assert "paper_id" not in portable_contract
    assert "legacy-stage" not in portable_contract
    assert "sci" not in portable_contract


def test_generic_examples_pack_install_is_idempotent_and_protected(tmp_path: Path) -> None:
    library = _library(tmp_path)
    pack = build_generic_examples_pack()

    first = library.install_pack(pack)
    second = library.install_pack(pack)
    entries = library.list_workflows()

    assert first[0]["version"].version_id == second[0]["version"].version_id
    assert len(library.store.list_versions(OFFLINE_DEMO_WORKFLOW_ID)) == 1
    assert len(entries) == 1
    assert entries[0].is_builtin is True
    assert entries[0].is_readonly is True
    assert entries[0].cloneable is True
    assert entries[0].latest_status == "published"


def test_clone_publish_create_and_execute_two_stable_offline_runs(tmp_path: Path) -> None:
    library = _library(tmp_path)
    source = library.install_pack(build_generic_examples_pack())[0]["version"]
    clone = library.clone_version(
        source.version_id,
        workflow_id="custom.offline-request-planner",
        name="My offline request planner",
        now=NOW,
    )["version"]
    published = library.publish(clone.version_id)["version"]
    binding = library.runtime_binding(published.version_id)
    request = {
        "actions": ["Collect requirements", "Draft a solution", "Review the result"],
        "constraints": ["Stay offline", "Keep output concise"],
        "goal": "Prepare a reusable project brief",
    }
    first_run = library.create_run(
        published.version_id,
        binding,
        inputs={"request": request},
        idempotency_key="generic-example-first",
        now=NOW,
    )
    second_run = library.create_run(
        published.version_id,
        binding,
        inputs={"request": request},
        idempotency_key="generic-example-second",
        now=NOW,
    )

    calls: dict[str, list[str]] = {}
    calls_lock = threading.Lock()
    traced = {}
    for implementation_ref, handler in generic_example_implementations().items():
        def record(context, *, _handler=handler):
            with calls_lock:
                calls.setdefault(context.workflow_run.run_id, []).append(context.node.node_id)
            return _handler(context)

        traced[implementation_ref] = record

    scheduler = WorkflowScheduler(
        library.store,
        tmp_path,
        ExecutorRegistry((DeterministicExecutor(traced, max_concurrency=2),)),
        config=SchedulerConfig(max_workers=2, lease_seconds=10, heartbeat_seconds=1),
    )
    run_ids = (first_run["run_id"], second_run["run_id"])
    report = scheduler.run_until_idle(run_ids, now=NOW)

    assert report.completed == 6
    assert report.failed == report.lease_lost == 0
    assert {library.store.get_run(run_id)["status"] for run_id in run_ids} == {"completed"}
    assert calls == {run_id: SEMANTIC_ORDER for run_id in run_ids}

    first_digests = {
        artifact["artifact_id"]: artifact["sha256"]
        for artifact in library.store.list_artifacts(first_run["run_id"])
    }
    second_digests = {
        artifact["artifact_id"]: artifact["sha256"]
        for artifact in library.store.list_artifacts(second_run["run_id"])
    }
    assert first_digests == second_digests
    assert set(first_digests) == {"normalized_request", "action_plan", "summary"}

    resolved = scheduler.artifacts.resolve_workflow_outputs(
        published.definition,
        first_run["run_id"],
    )
    summary_path = tmp_path / resolved["summary"].uri
    summary = summary_path.read_text(encoding="utf-8")
    assert "# Workflow Summary" in summary
    assert "Prepare a reusable project brief" in summary
    assert "1. Collect requirements" in summary
