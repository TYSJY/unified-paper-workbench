from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unified_paper_orchestrator.workflow.artifacts import (
    ArtifactContractError,
    ArtifactService,
    BoundArtifact,
    ProducedArtifact,
    fingerprint_workspace_artifact,
)
from unified_paper_orchestrator.workflow.contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    EdgeDefinition,
    NodeDefinition,
    NodeRunStatus,
    NodeType,
    WorkflowDefinition,
)


class FakeStore:
    def __init__(
        self,
        definition: WorkflowDefinition,
        *,
        inputs: dict[str, Any] | None = None,
        latest: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.run = {
            "run_id": "run-1",
            "definition_sha256": definition.sha256(),
            "inputs": inputs if inputs is not None else {"seed": {"topic": "alpha"}},
        }
        self.latest = latest if latest is not None else []
        self.artifacts = artifacts if artifacts is not None else []

    def get_run(self, run_id: str) -> dict[str, Any]:
        assert run_id == "run-1"
        return self.run

    def latest_node_runs(self, run_id: str) -> list[dict[str, Any]]:
        assert run_id == "run-1"
        return self.latest

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        assert run_id == "run-1"
        return self.artifacts


def _definition() -> WorkflowDefinition:
    seed = ArtifactSpec("seed", media_type="application/json", schema_ref="seed-v1")
    document = ArtifactSpec("document", media_type="text/plain", schema_ref="document-v1")
    source = NodeDefinition(
        node_id="produce",
        name="Produce",
        node_type=NodeType.DETERMINISTIC,
        display_order=10,
        outputs=(document,),
        implementation_ref="tests.produce",
    )
    consume = NodeDefinition(
        node_id="consume",
        name="Consume",
        node_type=NodeType.DETERMINISTIC,
        display_order=20,
        inputs=(
            ArtifactSpec("context", media_type="application/json", schema_ref="seed-v1"),
            ArtifactSpec("draft", media_type="text/plain", schema_ref="document-v1"),
        ),
        input_bindings=(
            ArtifactBinding(
                "context",
                ArtifactSourceKind.WORKFLOW_INPUT,
                "seed",
            ),
            ArtifactBinding(
                "draft",
                ArtifactSourceKind.NODE_OUTPUT,
                "document",
                "produce",
            ),
        ),
        outputs=(
            ArtifactSpec("final", media_type="text/plain", schema_ref="document-v1"),
            ArtifactSpec("notes", media_type="text/plain", required=False),
        ),
        implementation_ref="tests.consume",
    )
    value = WorkflowDefinition(
        workflow_id="tests.artifacts",
        name="Artifact workflow",
        inputs=(seed,),
        outputs=(ArtifactSpec("result", media_type="text/plain", schema_ref="document-v1"),),
        output_bindings=(
            ArtifactBinding(
                "result",
                ArtifactSourceKind.NODE_OUTPUT,
                "final",
                "consume",
            ),
        ),
        nodes=(source, consume),
        edges=(EdgeDefinition("produce-consume", "produce", "consume"),),
    )
    value.validate()
    return value


def _artifact_row(
    *,
    workspace: Path,
    artifact_record_id: str,
    node_run_id: str,
    producer_node_id: str,
    artifact_id: str,
    content: bytes,
    media_type: str,
    schema_ref: str,
) -> dict[str, Any]:
    uri = f"outputs/{artifact_record_id}.txt"
    target = workspace / uri
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {
        "artifact_record_id": artifact_record_id,
        "run_id": "run-1",
        "node_run_id": node_run_id,
        "producer_node_id": producer_node_id,
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": media_type,
        "schema_ref": schema_ref,
        "size_bytes": len(content),
        "metadata": {"source": artifact_record_id},
        "created_at": "2026-08-05T00:00:00.000000Z",
    }


def _latest(
    node_id: str,
    node_run_id: str,
    *,
    status: NodeRunStatus = NodeRunStatus.COMPLETED,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "workflow_run_id": "run-1",
        "node_id": node_id,
        "node_run_id": node_run_id,
        "status": status.value,
        "attempt": attempt,
    }


def _service(workspace: Path, store: Any) -> ArtifactService:
    return ArtifactService(
        workspace,
        store,
        schema_validators={
            "seed-v1": lambda _path: None,
            "document-v1": lambda _path: None,
        },
    )


def test_construction_is_io_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingStore:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"store was accessed during construction: {name}")

    def forbidden_resolve(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("filesystem was accessed during construction")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    service = _service(tmp_path / "not-created", ExplodingStore())

    assert service.workspace_root == tmp_path / "not-created"
    assert not service.workspace_root.exists()


def test_resolve_inputs_uses_frozen_input_and_latest_completed_attempt(tmp_path: Path) -> None:
    definition = _definition()
    old = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-old",
        node_run_id="node-produce-1",
        producer_node_id="produce",
        artifact_id="document",
        content=b"old",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    current = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-current",
        node_run_id="node-produce-2",
        producer_node_id="produce",
        artifact_id="document",
        content=b"current",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    store = FakeStore(
        definition,
        latest=[_latest("produce", "node-produce-2", attempt=2)],
        artifacts=[old, current],
    )
    service = _service(tmp_path, store)

    resolved = service.resolve_inputs(definition, definition.nodes[1], "run-1")

    assert set(resolved) == {"context", "draft"}
    assert isinstance(resolved["context"], BoundArtifact)
    assert resolved["context"].source_kind is ArtifactSourceKind.WORKFLOW_INPUT
    assert resolved["context"].value == {"topic": "alpha"}
    assert resolved["draft"].source_kind is ArtifactSourceKind.NODE_OUTPUT
    assert resolved["draft"].artifact_record_id == "artifact-current"
    assert resolved["draft"].node_run_id == "node-produce-2"
    assert resolved["draft"].sha256 == current["sha256"]


def test_latest_source_attempt_must_itself_be_completed(tmp_path: Path) -> None:
    definition = _definition()
    prior = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-prior",
        node_run_id="node-produce-1",
        producer_node_id="produce",
        artifact_id="document",
        content=b"prior",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    store = FakeStore(
        definition,
        latest=[
            _latest(
                "produce",
                "node-produce-2",
                status=NodeRunStatus.RETRYABLE_FAILURE,
                attempt=2,
            )
        ],
        artifacts=[prior],
    )

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(tmp_path, store).resolve_inputs(
            definition,
            definition.nodes[1],
            "run-1",
        )

    assert exc_info.value.code == "artifact.source.not_completed"


@pytest.mark.parametrize(
    ("inputs", "code"),
    [
        ({}, "artifact.inputs.required"),
        ({"seed": {}, "surprise": True}, "artifact.inputs.unknown"),
        ([], "artifact.inputs.type"),
    ],
)
def test_workflow_inputs_are_strictly_parsed(
    tmp_path: Path,
    inputs: Any,
    code: str,
) -> None:
    definition = _definition()
    store = FakeStore(definition, inputs=inputs)

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(tmp_path, store).resolve_inputs(
            definition,
            definition.nodes[1],
            "run-1",
        )

    assert exc_info.value.code == code


def test_supplied_inputs_and_definition_must_match_frozen_run(tmp_path: Path) -> None:
    definition = _definition()
    store = FakeStore(definition)
    service = _service(tmp_path, store)

    with pytest.raises(ArtifactContractError, match="frozen") as input_error:
        service.resolve_inputs(
            definition,
            definition.nodes[1],
            "run-1",
            workflow_inputs={"seed": {"topic": "changed"}},
        )
    assert input_error.value.code == "artifact.inputs.not_frozen"

    store.run["definition_sha256"] = "0" * 64
    with pytest.raises(ArtifactContractError) as definition_error:
        service.resolve_inputs(definition, definition.nodes[1], "run-1")
    assert definition_error.value.code == "artifact.definition.mismatch"


def test_artifact_ledger_records_are_strict_and_digest_checked(tmp_path: Path) -> None:
    definition = _definition()
    record = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-current",
        node_run_id="node-produce-1",
        producer_node_id="produce",
        artifact_id="document",
        content=b"current",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    record["sha256"] = "not-a-digest"
    store = FakeStore(
        definition,
        latest=[_latest("produce", "node-produce-1")],
        artifacts=[record],
    )

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(tmp_path, store).resolve_inputs(
            definition,
            definition.nodes[1],
            "run-1",
        )

    assert exc_info.value.code == "artifact.record.sha256"

    record["sha256"] = "a" * 64
    record["unexpected"] = True
    with pytest.raises(ArtifactContractError) as shape_error:
        _service(tmp_path, store).resolve_inputs(
            definition,
            definition.nodes[1],
            "run-1",
        )
    assert shape_error.value.code == "artifact.record.shape"


def test_resolve_workflow_outputs_renames_bound_latest_artifact(tmp_path: Path) -> None:
    definition = _definition()
    record = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-final",
        node_run_id="node-consume-1",
        producer_node_id="consume",
        artifact_id="final",
        content=b"done",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    store = FakeStore(
        definition,
        latest=[_latest("consume", "node-consume-1")],
        artifacts=[record],
    )

    resolved = _service(tmp_path, store).resolve_workflow_outputs(
        definition,
        "run-1",
    )

    assert list(resolved) == ["result"]
    assert resolved["result"].artifact_id == "result"
    assert resolved["result"].source_artifact_id == "final"
    assert resolved["result"].artifact_record_id == "artifact-final"


def test_verify_outputs_reads_file_and_returns_ledger_ready_fingerprint(tmp_path: Path) -> None:
    definition = _definition()
    output = tmp_path / "nested" / "final.txt"
    output.parent.mkdir()
    output.write_bytes(b"verified by the program\n")
    service = _service(tmp_path, FakeStore(definition))

    records = service.verify_outputs(
        definition.nodes[1],
        (
            ProducedArtifact(
                "final",
                Path("nested") / "final.txt",
                "text/plain",
                "document-v1",
                {"executor": "deterministic"},
            ),
        ),
    )

    assert len(records) == 1
    record = records[0]
    assert record.artifact_id == "final"
    assert record.uri.startswith(".workflow_artifacts/v1/objects/")
    assert record.uri.endswith(".blob")
    assert Path(record.uri).is_absolute() is False
    assert record.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert record.size_bytes == output.stat().st_size
    assert record.metadata == {"executor": "deterministic"}
    assert record.to_dict()["schema_ref"] == "document-v1"


def test_verified_output_uses_host_owned_seal_after_source_changes(tmp_path: Path) -> None:
    definition = _definition()
    source = tmp_path / "final.txt"
    source.write_text("accepted snapshot", encoding="utf-8")

    record = _service(tmp_path, FakeStore(definition)).verify_outputs(
        definition.nodes[1],
        (ProducedArtifact("final", source, "text/plain", "document-v1"),),
    )[0]
    source.write_text("changed after verification", encoding="utf-8")

    sealed = tmp_path / Path(record.uri)
    assert sealed.read_text(encoding="utf-8") == "accepted snapshot"
    assert hashlib.sha256(sealed.read_bytes()).hexdigest() == record.sha256


def test_readonly_fingerprint_uses_production_containment_without_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_bytes(b"read-only shadow evidence")
    before = artifact.stat()

    fingerprint = fingerprint_workspace_artifact(
        workspace,
        artifact,
        artifact_id="result",
        media_type="text/plain",
        schema_ref="result-v1",
    )

    after = artifact.stat()
    assert fingerprint.uri == "result.txt"
    assert fingerprint.sha256 == hashlib.sha256(b"read-only shadow evidence").hexdigest()
    assert fingerprint.size_bytes == len(b"read-only shadow evidence")
    assert fingerprint.media_type == "text/plain"
    assert fingerprint.schema_ref == "result-v1"
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert not (workspace / ".workflow_artifacts").exists()


def test_output_content_and_schema_are_program_verified(tmp_path: Path) -> None:
    definition = _definition()
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"not-json\xff")
    json_node = replace(
        definition.nodes[1],
        outputs=(ArtifactSpec("payload", media_type="application/json"),),
    )

    with pytest.raises(ArtifactContractError) as content_error:
        ArtifactService(tmp_path, FakeStore(definition)).verify_outputs(
            json_node,
            (ProducedArtifact("payload", invalid, "application/json"),),
        )
    assert content_error.value.code == "artifact.content.invalid"

    valid_text = tmp_path / "valid.txt"
    valid_text.write_text("valid UTF-8", encoding="utf-8")
    with pytest.raises(ArtifactContractError) as schema_error:
        ArtifactService(tmp_path, FakeStore(definition)).verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", valid_text, "text/plain", "document-v1"),),
        )
    assert schema_error.value.code == "artifact.schema.validator_missing"


def test_artifact_paths_reject_ntfs_stream_and_noncanonical_ledger_provenance(
    tmp_path: Path,
) -> None:
    definition = _definition()
    carrier = tmp_path / "carrier.txt"
    carrier.write_text("carrier", encoding="utf-8")
    stream_path = Path(f"{carrier}:hidden")
    with pytest.raises(ArtifactContractError) as stream_error:
        _service(tmp_path, FakeStore(definition)).verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", stream_path, "text/plain", "document-v1"),),
        )
    assert stream_error.value.code == "artifact.path.stream"

    record = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-foreign",
        node_run_id="node-produce-1",
        producer_node_id="produce",
        artifact_id="document",
        content=b"foreign",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    record["run_id"] = "run-foreign"
    store = FakeStore(
        definition,
        latest=[_latest("produce", "node-produce-1")],
        artifacts=[record],
    )
    with pytest.raises(ArtifactContractError) as run_error:
        _service(tmp_path, store).resolve_inputs(definition, definition.nodes[1], "run-1")
    assert run_error.value.code == "artifact.store.run_mismatch"


def test_attempt_output_root_is_exclusive_and_rejects_old_workspace_file(tmp_path: Path) -> None:
    definition = _definition()
    service = _service(tmp_path, FakeStore(definition))
    old = tmp_path / "old-final.txt"
    old.write_text("old bytes", encoding="utf-8")
    output_root = service.prepare_output_root("run-1", "node-consume-1")

    with pytest.raises(ArtifactContractError) as replay_error:
        service.verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", old, "text/plain", "document-v1"),),
            output_root=output_root,
        )
    assert replay_error.value.code == "artifact.output.outside_attempt"

    fresh = output_root / "final.txt"
    fresh.write_text("fresh bytes", encoding="utf-8")
    records = service.verify_outputs(
        definition.nodes[1],
        (ProducedArtifact("final", "final.txt", "text/plain", "document-v1"),),
        output_root=output_root,
    )
    assert records[0].uri.startswith(".workflow_artifacts/v1/objects/")
    assert records[0].uri.endswith(".blob")

    with pytest.raises(ArtifactContractError) as reused:
        service.prepare_output_root("run-1", "node-consume-1")
    assert reused.value.code == "artifact.output_root.reused"
    resumed = service.prepare_output_root("run-1", "node-consume-1", generation=2)
    assert resumed != output_root


def test_bound_artifact_is_reverified_against_ledger_before_consumption(tmp_path: Path) -> None:
    definition = _definition()
    record = _artifact_row(
        workspace=tmp_path,
        artifact_record_id="artifact-current",
        node_run_id="node-produce-1",
        producer_node_id="produce",
        artifact_id="document",
        content=b"sealed",
        media_type="text/plain",
        schema_ref="document-v1",
    )
    (tmp_path / record["uri"]).write_bytes(b"tampered")
    store = FakeStore(
        definition,
        latest=[_latest("produce", "node-produce-1")],
        artifacts=[record],
    )

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(tmp_path, store).resolve_inputs(
            definition,
            definition.nodes[1],
            "run-1",
        )
    assert exc_info.value.code == "artifact.record.changed"


@pytest.mark.parametrize(
    ("produced", "code"),
    [
        ((), "artifact.output.required"),
        (
            (
                ProducedArtifact("final", "final.txt", "text/plain", "document-v1"),
                ProducedArtifact("final", "final-2.txt", "text/plain", "document-v1"),
            ),
            "artifact.output.duplicate",
        ),
        ((ProducedArtifact("unknown", "final.txt"),), "artifact.output.unknown"),
        (
            (ProducedArtifact("final", "final.txt", "application/json", "document-v1"),),
            "artifact.media_type.mismatch",
        ),
        (
            (ProducedArtifact("final", "final.txt", "text/plain", "wrong-schema"),),
            "artifact.schema.mismatch",
        ),
    ],
)
def test_verify_outputs_enforces_declared_contract_before_filesystem_access(
    tmp_path: Path,
    produced: tuple[ProducedArtifact, ...],
    code: str,
) -> None:
    definition = _definition()
    service = _service(tmp_path / "does-not-exist", FakeStore(definition))

    with pytest.raises(ArtifactContractError) as exc_info:
        service.verify_outputs(definition.nodes[1], produced)

    assert exc_info.value.code == code


@pytest.mark.parametrize("declared", [Path("..") / "outside.txt", None])
def test_verify_outputs_rejects_workspace_escape(
    tmp_path: Path,
    declared: Path | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    definition = _definition()
    path = outside if declared is None else declared

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(workspace, FakeStore(definition)).verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", path, "text/plain", "document-v1"),),
        )

    assert exc_info.value.code == "artifact.path.outside_workspace"


def test_verify_outputs_rejects_symlink_or_reparse_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    link = workspace / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    definition = _definition()

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(workspace, FakeStore(definition)).verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", link, "text/plain", "document-v1"),),
        )

    assert exc_info.value.code == "artifact.path.reparse"


def test_verify_outputs_rejects_directories_and_missing_files(tmp_path: Path) -> None:
    definition = _definition()
    service = _service(tmp_path, FakeStore(definition))

    with pytest.raises(ArtifactContractError) as directory_error:
        service.verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", tmp_path, "text/plain", "document-v1"),),
        )
    assert directory_error.value.code == "artifact.path.not_file"

    with pytest.raises(ArtifactContractError) as missing_error:
        service.verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", "missing.txt", "text/plain", "document-v1"),),
        )
    assert missing_error.value.code == "artifact.path.missing"


def test_stat_read_stat_rejects_file_changed_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _definition()
    output = tmp_path / "final.txt"
    output.write_bytes(b"stable bytes")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int) -> os.stat_result | SimpleNamespace:
        nonlocal calls
        value = real_fstat(descriptor)
        calls += 1
        # The second call belongs to the newly created sealed destination;
        # mutate the source handle's post-read snapshot on the third call.
        if calls == 3:
            return SimpleNamespace(
                st_dev=value.st_dev,
                st_ino=value.st_ino,
                st_mode=value.st_mode,
                st_size=value.st_size,
                st_mtime=value.st_mtime,
                st_mtime_ns=value.st_mtime_ns + 1,
                st_ctime=value.st_ctime,
                st_ctime_ns=value.st_ctime_ns,
            )
        return value

    monkeypatch.setattr(os, "fstat", changing_fstat)

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(tmp_path, FakeStore(definition)).verify_outputs(
            definition.nodes[1],
            (ProducedArtifact("final", output, "text/plain", "document-v1"),),
        )

    assert calls >= 2
    assert exc_info.value.code == "artifact.file.changed"


def test_produced_and_bound_artifacts_reject_non_json_metadata() -> None:
    with pytest.raises(ArtifactContractError) as produced_error:
        ProducedArtifact("result", "result.txt", metadata={"bad": object()})
    assert produced_error.value.code == "artifact.json.invalid"

    with pytest.raises(ArtifactContractError) as bound_error:
        BoundArtifact(
            artifact_id="input",
            source_kind=ArtifactSourceKind.WORKFLOW_INPUT,
            source_artifact_id="seed",
            media_type="application/json",
            value={"bad": float("nan")},
        )
    assert bound_error.value.code == "artifact.json.invalid"


def test_forged_node_contract_cannot_change_output_acceptance(tmp_path: Path) -> None:
    definition = _definition()
    forged = replace(
        definition.nodes[1],
        inputs=(ArtifactSpec("forged", required=False),),
        input_bindings=(),
    )

    with pytest.raises(ArtifactContractError) as exc_info:
        _service(tmp_path, FakeStore(definition)).resolve_inputs(
            definition,
            forged,
            "run-1",
        )

    assert exc_info.value.code == "artifact.node.not_frozen"
