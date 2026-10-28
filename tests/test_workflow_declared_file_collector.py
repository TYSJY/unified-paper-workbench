from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.contracts import (
    ArtifactSpec,
    ExecutionProfile,
    NodeDefinition,
    NodeType,
    PromptRef,
    ResourceBinding,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.executors import (
    CodexResponse,
    CodexSDKExecutor,
    DeclaredFileOutputCollector,
    DeclaredOutputCollectionError,
    ExecutorRegistry,
    declared_output_files,
)
from unified_paper_orchestrator.workflow.scheduler import SchedulerConfig, WorkflowScheduler
from unified_paper_orchestrator.workflow.store import WorkflowStore


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _node(mapping: object) -> NodeDefinition:
    return NodeDefinition(
        "compose",
        "Compose",
        NodeType.CODEX,
        10,
        prompt_ref=PromptRef("tests.collector.prompt", "a" * 64, "1"),
        execution_profile=ExecutionProfile(
            model="gpt-frozen",
            reasoning_effort="high",
            token_limit=100,
            timeout_seconds=60,
            allowed_tools=("read", "write"),
            network_access=False,
        ),
        outputs=(
            ArtifactSpec("report", "text/markdown"),
            ArtifactSpec("notes", "text/plain", required=False),
        ),
        metadata={"declared_output_files": mapping},
    )


def _run(
    tmp_path: Path,
    node: NodeDefinition,
    write_outputs,
    *,
    run_id: str,
) -> tuple[WorkflowStore, object, bool]:
    prompt = tmp_path / f"{run_id}.prompt"
    prompt.write_text("Create only the declared output files.", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    node = replace(node, prompt_ref=PromptRef("tests.collector.prompt", digest, "1"))
    definition = WorkflowDefinition(f"tests.collector.{run_id}", "Collector", (node,), ())
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        version_id=f"tests.collector.{run_id}.v1",
        created_at=NOW.isoformat(),
        status=WorkflowVersionStatus.PUBLISHED,
    )
    binding = WorkflowRuntimeBinding(
        definition.sha256(),
        (ResourceBinding("tests.collector.prompt", str(prompt.resolve()), digest),),
    )
    store = WorkflowStore(tmp_path / f"{run_id}.sqlite3")
    store.initialize()
    store.create_run(version, binding, run_id=run_id, now=NOW)
    called = False

    class Client:
        def run(self, *, session, request, control):
            nonlocal called
            called = True
            write_outputs(request.output_root)
            return CodexResponse(
                "completed",
                output_text="done",
                external_session_id=f"external-{run_id}",
                usage={"total_tokens": 1},
            )

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry((CodexSDKExecutor(Client(), output_collector=DeclaredFileOutputCollector()),)),
        config=SchedulerConfig(lease_seconds=10, heartbeat_seconds=1),
    )
    report = scheduler.run_until_idle((run_id,), now=NOW)
    return store, report, called


def test_declared_file_collector_completes_and_omits_missing_optional_file(tmp_path: Path) -> None:
    node = _node({"report": "deliverables/report.md", "notes": "deliverables/notes.txt"})

    def write_outputs(output_root: Path) -> None:
        directory = output_root / "deliverables"
        directory.mkdir()
        (directory / "report.md").write_text("verified report", encoding="utf-8")

    store, report, called = _run(tmp_path, node, write_outputs, run_id="collector-success")

    assert called is True
    assert report.completed == 1
    assert store.get_run("collector-success")["status"] == "completed"
    artifacts = store.list_artifacts("collector-success")
    assert [item["artifact_id"] for item in artifacts] == ["report"]
    assert artifacts[0]["sha256"] == hashlib.sha256(b"verified report").hexdigest()


def test_declared_file_collector_includes_an_existing_optional_file(tmp_path: Path) -> None:
    node = _node({"report": "report.md", "notes": "notes.txt"})

    def write_outputs(output_root: Path) -> None:
        (output_root / "report.md").write_text("report", encoding="utf-8")
        (output_root / "notes.txt").write_text("notes", encoding="utf-8")

    store, report, _called = _run(tmp_path, node, write_outputs, run_id="collector-optional")

    assert report.completed == 1
    assert {item["artifact_id"] for item in store.list_artifacts("collector-optional")} == {"report", "notes"}


@pytest.mark.parametrize(
    "mapping, message",
    [
        ({"report": "report.md"}, "exactly match"),
        ({"report": "../report.md", "notes": "notes.txt"}, "canonical"),
        ({"report": "folder\\report.md", "notes": "notes.txt"}, "not portable"),
        ({"report": "CON.txt", "notes": "notes.txt"}, "reserved name"),
        ({"report": "CONIN$.txt", "notes": "notes.txt"}, "reserved name"),
        ({"report": "CLOCK$.txt", "notes": "notes.txt"}, "reserved name"),
        ({"report": "COM¹.txt", "notes": "notes.txt"}, "reserved name"),
        ({"report": "bad?.md", "notes": "notes.txt"}, "canonical"),
        ({"report": "bad\u0001.md", "notes": "notes.txt"}, "canonical"),
        ({"report": "bad＜name.md", "notes": "notes.txt"}, "not portable"),
        ({"report": "report.md:stream", "notes": "notes.txt"}, "not portable"),
        ({"report": "Report.md", "notes": "report.MD"}, "unique across platforms"),
        ({"report": "bundle", "notes": "bundle/notes.txt"}, "unique across platforms"),
    ],
)
def test_declared_output_mapping_rejects_ambiguous_or_nonportable_paths(
    mapping: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(DeclaredOutputCollectionError, match=message):
        declared_output_files(_node(mapping))


def test_declared_file_collector_blocks_missing_required_output(tmp_path: Path) -> None:
    node = _node({"report": "report.md", "notes": "notes.txt"})
    store, report, called = _run(tmp_path, node, lambda _root: None, run_id="collector-missing")

    assert called is True
    assert report.failed == 1
    assert store.get_run("collector-missing")["status"] == "blocked"
    assert store.latest_node_runs("collector-missing")[0]["error_code"] == "CODEX_OUTPUT_COLLECTION_FAILED"


def test_artifact_service_still_rejects_invalid_declared_json(tmp_path: Path) -> None:
    node = replace(
        _node({"report": "report.json", "notes": "notes.txt"}),
        outputs=(ArtifactSpec("report", "application/json"),),
        metadata={"declared_output_files": {"report": "report.json"}},
    )

    def write_outputs(output_root: Path) -> None:
        (output_root / "report.json").write_text("{not-json", encoding="utf-8")

    store, report, _called = _run(tmp_path, node, write_outputs, run_id="collector-invalid-json")

    assert report.failed == 1
    assert store.get_run("collector-invalid-json")["status"] == "blocked"
    assert store.latest_node_runs("collector-invalid-json")[0]["error_code"] == "OUTPUT_VERIFICATION_FAILED"


def test_declared_file_collector_rejects_symlink_output(tmp_path: Path) -> None:
    node = _node({"report": "report.md", "notes": "notes.txt"})
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    probe = tmp_path / "symlink-probe.md"
    try:
        os.symlink(outside, probe)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    probe.unlink()

    def write_outputs(output_root: Path) -> None:
        os.symlink(outside, output_root / "report.md")

    store, report, called = _run(tmp_path, node, write_outputs, run_id="collector-symlink")

    assert called is True
    assert report.failed == 1
    assert store.get_run("collector-symlink")["status"] == "blocked"
