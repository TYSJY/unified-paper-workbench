from __future__ import annotations

import hashlib

import pytest

from unified_paper_orchestrator_v2.contracts import ArtifactManifest, ArtifactRef, ApprovalPolicy, TaskEnvelope
from unified_paper_orchestrator_v2.executors.deterministic import DeterministicExecutor


def test_task_envelope_round_trip_and_default_idempotency() -> None:
    envelope = TaskEnvelope.from_dict(
        {
            "paper_id": "paper-1",
            "stage": "system",
            "action": "system.noop",
            "executor": "deterministic",
            "payload": {"hello": "world"},
            "approval_policy": "none",
        }
    )
    assert envelope.idempotency_key == envelope.task_id
    assert envelope.approval_policy is ApprovalPolicy.NONE
    assert TaskEnvelope.from_dict(envelope.to_dict()).to_dict() == envelope.to_dict()


def test_artifact_ref_rejects_invalid_digest() -> None:
    with pytest.raises(ValueError, match="invalid artifact sha256"):
        ArtifactRef(uri="x", sha256="bad").validate()


def test_deterministic_executor_verifies_artifact(tmp_path) -> None:
    source = tmp_path / "paper.tex"
    source.write_text("paper", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    envelope = TaskEnvelope.from_dict(
        {
            "paper_id": "paper-1",
            "stage": "system",
            "action": "artifact.verify",
            "executor": "deterministic",
            "inputs": [{"uri": str(source), "sha256": digest, "media_type": "text/x-tex"}],
        }
    )
    result = DeterministicExecutor().execute(envelope)
    assert result.status.value == "completed"
    assert result.outputs[0].sha256 == digest


def test_artifact_manifest_has_stable_digest(tmp_path) -> None:
    source = tmp_path / "main.pdf"
    source.write_bytes(b"pdf")
    manifest = ArtifactManifest(
        paper_id="paper-1",
        stage="figures",
        producer="deterministic-test",
        artifacts=[ArtifactRef.from_file(source, media_type="application/pdf")],
        metadata={"version": 1},
    )
    restored = ArtifactManifest.from_dict(manifest.to_dict())
    assert restored.sha256() == manifest.sha256()
