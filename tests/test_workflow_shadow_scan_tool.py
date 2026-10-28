from __future__ import annotations

import json
from pathlib import Path

from tools.workflow_shadow_scan import main
from unified_paper_orchestrator.workflow.shadow_store import shadow_database_path


def test_shadow_scan_is_readonly_and_reports_safe_parity(tmp_path: Path, capsys) -> None:
    paper_id = "paper-tool-001"
    pipeline = tmp_path / "pipeline.json"
    journal = tmp_path / "journal.json"
    pipeline.write_text(
        json.dumps({"sessions": {paper_id: {"status": "running", "step_done": 1}}}),
        encoding="utf-8",
    )
    journal.write_text(
        json.dumps(
            {
                "papers": {
                    paper_id: {
                        "status": "running",
                        "completed_step": 1,
                        "active_step": 2,
                        "thread_id": "private-thread-12345678",
                        "steps": {
                            "1": {
                                "status": "complete",
                                "effective_execution_profile": {
                                    "model": "gpt-5.5",
                                    "hard_token_limit": 1_000_000,
                                },
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (pipeline, journal)}

    code = main(
        [
            "--stage",
            "writing",
            "--paper-id",
            paper_id,
            "--source",
            f"pipeline={pipeline}",
            "--source",
            f"sdk-journal={journal}",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["production_writer"] == "legacy-runtime"
    assert payload["production_affected"] is False
    assert payload["persistence"] == {"requested": False, "persisted": False}
    assert payload["observation"]["coherent"] is True
    serialized = json.dumps(payload)
    assert paper_id not in serialized
    assert "private-thread-12345678" not in serialized
    for path in (pipeline, journal):
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before[path]
    assert set(tmp_path.iterdir()) == {pipeline, journal}


def test_shadow_scan_source_failure_is_fail_open_for_production(tmp_path: Path, capsys) -> None:
    source = tmp_path / "broken.json"
    journal = tmp_path / "journal.json"
    source.write_text("{broken", encoding="utf-8")
    journal.write_text("{}", encoding="utf-8")
    before = (source.read_bytes(), source.stat().st_mtime_ns)

    code = main(
        [
            "--stage",
            "figures",
            "--paper-id",
            "paper-tool-002",
            "--source",
            f"pipeline={source}",
            "--source",
            f"sdk-journal={journal}",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["status"] == "source-error"
    assert payload["production_affected"] is False
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_shadow_scan_persists_only_to_explicit_isolated_database(tmp_path: Path, capsys) -> None:
    paper_id = "private-paper-id-003"
    source = tmp_path / "journal.json"
    pipeline = tmp_path / "pipeline.json"
    database = shadow_database_path(tmp_path / "shadow")
    pipeline.write_text("{}", encoding="utf-8")
    source.write_text(
        json.dumps(
            {
                "papers": {
                    paper_id: {
                        "status": "running",
                        "active_step": 1,
                        "thread_id": "private-thread-87654321",
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
            }
        ),
        encoding="utf-8",
    )
    before = (source.read_bytes(), source.stat().st_mtime_ns)

    code = main(
        [
            "--stage",
            "figures",
            "--paper-id",
            paper_id,
            "--source",
            f"pipeline={pipeline}",
            "--source",
            f"sdk-journal={source}",
            "--shadow-db",
            str(database),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["persistence"]["requested"] is True
    assert payload["persistence"]["persisted"] is True
    assert payload["persistence"]["observation_id"].startswith("obs_")
    assert payload["persistence"]["comparison_id"].startswith("cmp_")
    assert database.is_file()
    database_bytes = database.read_bytes()
    assert paper_id.encode() not in database_bytes
    assert b"private-thread-87654321" not in database_bytes
    assert str(source).encode() not in database_bytes
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_shadow_scan_stdout_hashes_blocker_summary_and_rejects_raw_subject(tmp_path: Path, capsys) -> None:
    paper_id = "private-paper-id-004"
    source = tmp_path / "journal.json"
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text("{}", encoding="utf-8")
    source.write_text(
        json.dumps(
            {
                "papers": {
                    paper_id: {
                        "status": "blocked",
                        "active_step": 1,
                        "blocker": "operator wrote a sentence instead of a code",
                        "blocker_message": "private blocker narrative",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--stage",
            "figures",
            "--paper-id",
            paper_id,
            "--source",
            f"pipeline={pipeline}",
            "--source",
            f"sdk-journal={source}",
        ]
    )
    payload = capsys.readouterr().out

    assert code == 0
    assert paper_id not in payload
    assert "private blocker narrative" not in payload
    assert "operator wrote a sentence" not in payload
    assert "LEGACY_BLOCKER_UNCLASSIFIED" not in payload
    assert "blocker_code_sha256" in payload


def test_shadow_scan_requires_both_legacy_authorities(tmp_path: Path, capsys) -> None:
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text("{}", encoding="utf-8")

    code = main(
        [
            "--stage",
            "writing",
            "--paper-id",
            "private-paper-id-005",
            "--source",
            f"pipeline={pipeline}",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload == {
        "status": "invalid-input",
        "code": "required_authorities_missing",
        "production_affected": False,
    }


def test_shadow_steps_digest_scopes_persisted_comparison_identity(tmp_path: Path, capsys) -> None:
    paper_id = "private-paper-id-006"
    pipeline = tmp_path / "pipeline.json"
    journal = tmp_path / "journal.json"
    database = shadow_database_path(tmp_path / "shadow")
    first_steps = tmp_path / "shadow-a.json"
    second_steps = tmp_path / "shadow-b.json"
    pipeline.write_text("{}", encoding="utf-8")
    journal.write_text(json.dumps({"papers": {paper_id: {"status": "running"}}}), encoding="utf-8")
    first_steps.write_text(json.dumps({"figures-01": {"status": "pending"}}), encoding="utf-8")
    second_steps.write_text(json.dumps({"figures-01": {"status": "running"}}), encoding="utf-8")

    base_args = [
        "--stage",
        "figures",
        "--paper-id",
        paper_id,
        "--source",
        f"pipeline={pipeline}",
        "--source",
        f"sdk-journal={journal}",
        "--shadow-db",
        str(database),
    ]
    assert main([*base_args, "--shadow-steps", str(first_steps)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main([*base_args, "--shadow-steps", str(second_steps)]) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["persistence"]["observation_id"] == second["persistence"]["observation_id"]
    assert first["persistence"]["comparison_id"] != second["persistence"]["comparison_id"]
    assert first["comparison"]["binding_sha256"] != second["comparison"]["binding_sha256"]


def test_shadow_scan_end_to_end_never_emits_or_persists_legacy_free_text(
    tmp_path: Path,
    capsys,
) -> None:
    sentinels = {
        "paper": "privatepaperidentifier987654",
        "schema": "private-source-schema-sentinel",
        "version": "private-workflow-version-sentinel",
        "status": "private-status-sentinel",
        "model": "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890",
        "reasoning": "private-reasoning-sentinel",
        "blocker": "privateblockeridentifier987654",
        "message": "private blocker narrative sentinel",
        "artifact_id": "private-artifact-name-sentinel",
        "artifact_uri": "private/artifact-sentinel.txt",
        "media": "private/media-sentinel",
        "schema_ref": "private-artifact-schema-sentinel",
        "expected": "private-shadow-expected-sentinel",
    }
    pipeline = tmp_path / "pipeline.json"
    journal = tmp_path / "journal.json"
    artifacts = tmp_path / "artifacts.json"
    shadow_steps = tmp_path / "shadow-steps.json"
    database = shadow_database_path(tmp_path / "shadow")
    pipeline.write_text("{}", encoding="utf-8")
    journal.write_text(
        json.dumps(
            {
                "schema": sentinels["schema"],
                "papers": {
                    sentinels["paper"]: {
                        "status": sentinels["status"],
                        "active_step": 1,
                        "workflow_version_id": sentinels["version"],
                        "blocker": sentinels["blocker"],
                        "blocker_message": sentinels["message"],
                        "steps": {
                            "1": {
                                "status": sentinels["status"],
                                "effective_execution_profile": {
                                    "model": sentinels["model"],
                                    "reasoning_effort": sentinels["reasoning"],
                                },
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    artifacts.write_text(
        json.dumps(
            {
                "figures-01": [
                    {
                        "artifact_id": sentinels["artifact_id"],
                        "uri": sentinels["artifact_uri"],
                        "sha256": "a" * 64,
                        "size_bytes": 7,
                        "media_type": sentinels["media"],
                        "schema_ref": sentinels["schema_ref"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    shadow_steps.write_text(
        json.dumps({"figures-01": {"status": sentinels["expected"], "blocker": sentinels["blocker"]}}),
        encoding="utf-8",
    )

    code = main(
        [
            "--stage",
            "figures",
            "--paper-id",
            sentinels["paper"],
            "--source",
            f"pipeline={pipeline}",
            "--source",
            f"sdk-journal={journal}",
            "--artifact-evidence",
            str(artifacts),
            "--shadow-steps",
            str(shadow_steps),
            "--shadow-db",
            str(database),
        ]
    )
    stdout = capsys.readouterr().out
    database_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
        if candidate.is_file()
    )

    assert code == 0
    for sentinel in sentinels.values():
        assert sentinel not in stdout
        assert sentinel.encode("utf-8") not in database_bytes
