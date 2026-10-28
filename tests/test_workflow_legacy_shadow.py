from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow import legacy_shadow
from unified_paper_orchestrator.workflow.legacy_shadow import (
    LegacyShadowSourceError,
    LegacySource,
    LegacySourceSnapshot,
    SourceRevision,
    capture_once,
    compare_legacy,
    observe_once,
    project_legacy,
)
from unified_paper_orchestrator.workflow.packs import (
    SCI_FIGURES_WORKFLOW_ID,
    SCI_WRITING_WORKFLOW_ID,
    build_sci_workflow_pack,
)


PAPER_ID = "paper-shadow-001"


def _definition(workflow_id: str):
    return build_sci_workflow_pack().workflow(workflow_id).definition


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def test_capture_once_double_reads_sources_without_changing_them(tmp_path: Path) -> None:
    pipeline = tmp_path / "pipeline.json"
    journal = tmp_path / "journal.json"
    _write_json(pipeline, {"schema": "pipeline/v1", "sessions": {PAPER_ID: {"step_done": 1}}})
    _write_json(journal, {"schema": "journal/v1", "papers": {PAPER_ID: {"completed_step": 1}}})
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (pipeline, journal)}

    bundle = capture_once(
        "writing",
        PAPER_ID,
        sources=(LegacySource("pipeline", pipeline), LegacySource("sdk-journal", journal)),
    )

    assert bundle.coherent is True
    assert bundle.attempts == 1
    assert [item.schema for item in bundle.revisions] == ["pipeline/v1", "journal/v1"]
    for path in (pipeline, journal):
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before[path]
    assert set(tmp_path.iterdir()) == {pipeline, journal}


def test_capture_reports_unstable_snapshot_instead_of_false_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = LegacySource("pipeline", tmp_path / "pipeline.json")
    calls = 0

    def changing(_: LegacySource) -> LegacySourceSnapshot:
        nonlocal calls
        calls += 1
        digest = hashlib.sha256(str(calls).encode()).hexdigest()
        return LegacySourceSnapshot(SourceRevision("pipeline", digest, "pipeline/v1"), {"revision": calls})

    monkeypatch.setattr(legacy_shadow, "_read_source", changing)
    bundle = capture_once("writing", PAPER_ID, sources=(source,), attempts=3)

    assert calls == 6
    assert bundle.coherent is False
    assert bundle.issue == "unstable_snapshot"


def test_observe_brackets_artifact_projection_and_retries_changed_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = LegacySource("pipeline", tmp_path / "pipeline.json")
    revision = 1
    artifact_reads = 0

    def read(_: LegacySource) -> LegacySourceSnapshot:
        digest = hashlib.sha256(str(revision).encode()).hexdigest()
        return LegacySourceSnapshot(
            SourceRevision("pipeline", digest),
            {"sessions": {PAPER_ID: {"status": "queued", "step_done": 0}}},
        )

    def artifacts(*_: object):
        nonlocal revision, artifact_reads
        artifact_reads += 1
        if artifact_reads == 1:
            revision = 2
        return {}

    monkeypatch.setattr(legacy_shadow, "_read_source", read)
    observation = observe_once(
        "writing",
        PAPER_ID,
        _definition(SCI_WRITING_WORKFLOW_ID),
        sources=(source,),
        artifact_reader=artifacts,
    )

    assert artifact_reads == 2
    assert observation.coherent is True
    assert observation.source_revisions[0].sha256 == hashlib.sha256(b"2").hexdigest()


def test_incoherent_observation_never_emits_a_parity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = LegacySource("pipeline", tmp_path / "pipeline.json")
    revision = 0

    def read(_: LegacySource) -> LegacySourceSnapshot:
        digest = hashlib.sha256(str(revision).encode()).hexdigest()
        return LegacySourceSnapshot(
            SourceRevision("pipeline", digest),
            {"sessions": {PAPER_ID: {"status": "complete", "step_done": 10}}},
        )

    def artifacts(*_: object):
        nonlocal revision
        revision += 1
        return {"writing-01": ()}

    monkeypatch.setattr(legacy_shadow, "_read_source", read)
    definition = _definition(SCI_WRITING_WORKFLOW_ID)
    observation = observe_once(
        "writing",
        PAPER_ID,
        definition,
        sources=(source,),
        artifact_reader=artifacts,
        attempts=2,
    )
    comparison = compare_legacy(observation, definition)

    assert observation.coherent is False
    assert comparison.verdict == "unknown"
    assert [item.field for item in comparison.fields] == ["source.coherent"]


def test_capture_fails_closed_on_corrupt_required_source(tmp_path: Path) -> None:
    source = tmp_path / "pipeline.json"
    source.write_text("{broken", encoding="utf-8")

    with pytest.raises(LegacyShadowSourceError) as exc_info:
        capture_once("writing", PAPER_ID, sources=(LegacySource("pipeline", source),))

    assert exc_info.value.code == "source_invalid_json"
    assert source.read_text(encoding="utf-8") == "{broken"


def test_project_writing_combines_pipeline_and_sdk_journal_without_raw_thread_id(tmp_path: Path) -> None:
    pipeline = tmp_path / "pipeline.json"
    journal = tmp_path / "journal.json"
    _write_json(
        pipeline,
        {
            "sessions": {
                PAPER_ID: {
                    "status": "running",
                    "step_done": 1,
                    "thread_id": "thread-sensitive-12345678",
                }
            }
        },
    )
    _write_json(
        journal,
        {
            "papers": {
                f"session:{PAPER_ID}": {
                    "paper_id": PAPER_ID,
                    "status": "running",
                    "completed_step": 1,
                    "active_step": 2,
                    "thread_id": "thread-sensitive-12345678",
                    "workflow_snapshot": {"version_id": "legacy-writing-v1"},
                    "steps": {
                        "1": {
                            "state": "completed",
                            "effective_execution_profile": {
                                "model": "gpt-5.5",
                                "reasoning_effort": "",
                                "hard_token_limit": 1_000_000,
                            },
                        },
                        "2": {
                            "state": "running",
                            "effective_execution_profile": {
                                "model": "gpt-5.5",
                                "reasoning_effort": "",
                                "hard_token_limit": 2_000_000,
                            },
                            "usage": {"total_tokens": 1234},
                        },
                    },
                }
            }
        },
    )
    bundle = capture_once(
        "writing",
        PAPER_ID,
        sources=(LegacySource("pipeline", pipeline), LegacySource("sdk-journal", journal)),
    )

    observation = project_legacy(bundle, _definition(SCI_WRITING_WORKFLOW_ID))
    serialized = json.dumps(observation.to_dict(), sort_keys=True)

    assert observation.workflow_version_id == "legacy-writing-v1"
    assert observation.completed_step == 1
    assert observation.active_step == 2
    assert observation.steps[0].normalized_status == "completed"
    assert observation.steps[1].normalized_status == "running"
    assert observation.steps[1].model == "gpt-5.5"
    assert observation.steps[1].token_limit == 2_000_000
    assert observation.steps[1].token_usage == {"last.total_tokens": 1234}
    assert observation.steps[0].session_fingerprint == observation.steps[1].session_fingerprint
    assert "thread-sensitive-12345678" not in serialized


def test_compare_reports_real_token_mismatch_and_missing_as_unknown(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    _write_json(
        journal,
        {
            "papers": {
                PAPER_ID: {
                    "status": "running",
                    "completed_step": 1,
                    "active_step": 2,
                    "thread_id": "thread-12345678",
                    "steps": {
                        "1": {
                            "status": "complete",
                            "effective_execution_profile": {
                                "model": "gpt-5.5",
                                "hard_token_limit": 1_000_000,
                            },
                        },
                        "2": {
                            "status": "running",
                            "effective_execution_profile": {
                                "model": "gpt-5.5",
                                "hard_token_limit": 999,
                            },
                        },
                    },
                }
            }
        },
    )
    bundle = capture_once("writing", PAPER_ID, sources=(LegacySource("sdk-journal", journal),))
    definition = _definition(SCI_WRITING_WORKFLOW_ID)
    comparison = compare_legacy(project_legacy(bundle, definition), definition)

    step2_token = next(
        item for item in comparison.fields if item.node_id == "writing-02" and item.field == "node.token_limit"
    )
    step3_token = next(
        item for item in comparison.fields if item.node_id == "writing-03" and item.field == "node.token_limit"
    )
    assert step2_token.verdict == "mismatch"
    assert step2_token.expected == 2_000_000
    assert step2_token.observed == 999
    assert step3_token.verdict == "unknown"
    assert comparison.verdict == "mismatch"


def test_unlimited_legacy_token_is_equivalent_to_portable_none(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    _write_json(
        journal,
        {
            "papers": {
                PAPER_ID: {
                    "status": "running",
                    "active_step": 1,
                    "thread_id": "figure-thread-12345678",
                    "steps": {
                        "1": {
                            "status": "running",
                            "effective_execution_profile": {
                                "model": "gpt-5.5",
                                "hard_token_limit": 0,
                            },
                        }
                    },
                }
            }
        },
    )
    definition = _definition(SCI_FIGURES_WORKFLOW_ID)
    bundle = capture_once("figures", PAPER_ID, sources=(LegacySource("sdk-journal", journal),))
    comparison = compare_legacy(project_legacy(bundle, definition), definition)
    token = next(
        item for item in comparison.fields if item.node_id == "figures-01" and item.field == "node.token_limit"
    )

    assert token.expected is None
    assert token.observed == 0
    assert token.verdict == "equivalent"


def test_checked_artifacts_are_compared_by_declared_ids_and_secrets_are_redacted(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    _write_json(
        journal,
        {
            "papers": {
                PAPER_ID: {
                    "status": "complete",
                    "completed_step": 1,
                    "thread_id": "thread-12345678",
                    "steps": {
                        "1": {
                            "status": "complete",
                            "blocker_message": "password=hunter2 Authorization=Bearer-ABCDEFGH123456",
                            "effective_execution_profile": {
                                "model": "gpt-5.5",
                                "hard_token_limit": 1_000_000,
                            },
                        }
                    },
                }
            }
        },
    )
    definition = _definition(SCI_WRITING_WORKFLOW_ID)
    bundle = capture_once("writing", PAPER_ID, sources=(LegacySource("sdk-journal", journal),))
    evidence = {
        "writing-01": (
            {
                "artifact_id": "wrong-artifact",
                "uri": "ideas/paper.md",
                "sha256": "a" * 64,
                "size_bytes": 10,
                "media_type": "text/markdown",
            },
        )
    }
    observation = project_legacy(bundle, definition, artifact_evidence=evidence)
    comparison = compare_legacy(observation, definition)
    artifact = next(
        item for item in comparison.fields if item.node_id == "writing-01" and item.field == "node.artifacts"
    )
    serialized = json.dumps(observation.to_dict())

    assert artifact.verdict == "mismatch"
    assert "hunter2" not in serialized
    assert "ABCDEFGH123456" not in serialized
    assert "[REDACTED]" in serialized


def test_shadow_artifact_evidence_rejects_absolute_or_traversal_paths(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    _write_json(journal, {"papers": {PAPER_ID: {"status": "complete", "completed_step": 1}}})
    definition = _definition(SCI_WRITING_WORKFLOW_ID)
    bundle = capture_once("writing", PAPER_ID, sources=(LegacySource("sdk-journal", journal),))

    for uri in (str((tmp_path / "outside.md").absolute()), "../outside.md", "folder\\outside.md"):
        with pytest.raises(ValueError, match="workspace-relative"):
            project_legacy(
                bundle,
                definition,
                artifact_evidence={
                    "writing-01": (
                        {
                            "artifact_id": "idea_decision_card",
                            "uri": uri,
                            "sha256": "a" * 64,
                            "size_bytes": 1,
                        },
                    )
                },
            )


def test_artifact_evidence_revision_is_part_of_observation_identity(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    _write_json(journal, {"papers": {PAPER_ID: {"status": "complete", "completed_step": 1}}})
    definition = _definition(SCI_WRITING_WORKFLOW_ID)
    bundle = capture_once("writing", PAPER_ID, sources=(LegacySource("sdk-journal", journal),))

    def evidence(digest: str) -> dict[str, tuple[dict[str, object], ...]]:
        return {
            "writing-01": (
                {
                    "artifact_id": "idea_decision_card",
                    "uri": "ideas/card.md",
                    "sha256": digest,
                    "size_bytes": 10,
                },
            )
        }

    first = project_legacy(bundle, definition, artifact_evidence=evidence("a" * 64))
    repeated = project_legacy(bundle, definition, artifact_evidence=evidence("a" * 64))
    changed = project_legacy(bundle, definition, artifact_evidence=evidence("b" * 64))

    assert first.evidence_revision_sha256 == repeated.evidence_revision_sha256
    assert first.source_revision_sha256 == repeated.source_revision_sha256
    assert first.evidence_revision_sha256 != changed.evidence_revision_sha256
    assert first.source_revision_sha256 != changed.source_revision_sha256


def test_storage_payload_hashes_all_legacy_controlled_text(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    sentinels = {
        "workflow": "privatepaperidentifier123456",
        "status": "private-status-value",
        "model": "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890",
        "effort": "private-reasoning-value",
        "blocker": "privateblockeridentifier123456",
    }
    _write_json(
        journal,
        {
            "schema": "private/source/schema",
            "papers": {
                PAPER_ID: {
                    "status": sentinels["status"],
                    "active_step": 1,
                    "workflow_version_id": sentinels["workflow"],
                    "blocker": sentinels["blocker"],
                    "steps": {
                        "1": {
                            "status": sentinels["status"],
                            "effective_execution_profile": {
                                "model": sentinels["model"],
                                "reasoning_effort": sentinels["effort"],
                            },
                        }
                    },
                }
            },
        },
    )
    definition = _definition(SCI_WRITING_WORKFLOW_ID)
    bundle = capture_once("writing", PAPER_ID, sources=(LegacySource("sdk-journal", journal),))
    observation = project_legacy(bundle, definition)
    comparison = compare_legacy(
        observation,
        definition,
        shadow_steps={"writing-01": {"blocker": sentinels["blocker"]}},
    )
    serialized = json.dumps(
        {"observation": observation.to_storage_payload(), "comparison": comparison.to_storage_payload()}
    )

    for sentinel in sentinels.values():
        assert sentinel not in serialized
    assert "blocker_code_sha256" in serialized
