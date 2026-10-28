from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import MethodType
from typing import Any

import pytest

from unified_paper_orchestrator import io_utils, orchestrator as orchestrator_module
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator


PAPER_ID = "2026-07-16_001_lifecycle-test"


class _FakeAdapter:
    def __init__(self, project: dict[str, Any], state: dict[str, Any]) -> None:
        self.root = Path(project["root"])
        self.papers_dir = Path(project["papers_dir"])
        self.state_path = Path(project["state_path"])
        write_json_atomic(self.state_path, state)

    def load_state(self) -> dict[str, Any]:
        value = read_json(self.state_path, {})
        return value if isinstance(value, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, state)

    def all_paper_ids(self) -> set[str]:
        state = self.load_state()
        result: set[str] = set()
        for key in ("papers", "sessions"):
            records = state.get(key, {})
            if isinstance(records, dict):
                result.update(str(item) for item in records)
        if self.papers_dir.is_dir():
            result.update(child.name for child in self.papers_dir.iterdir() if child.is_dir())
        return result


def _project(root: Path, stage: str) -> dict[str, Any]:
    papers_dir = root / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    return {
        "label": stage,
        "root": root,
        "state_path": root / "controller" / f"{stage}_state.json",
        "prompt_path": root / "controller" / "prompt.md",
        "papers_dir": papers_dir,
    }


@pytest.fixture()
def lifecycle_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    projects = {
        stage: _project(tmp_path / stage, stage)
        for stage in ("writing", "figures", "journal_author", "submission")
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    paths = {
        "unified": state_dir / "unified_state.json",
        "summary": state_dir / "summary.md",
        "archive": state_dir / "archived_papers",
        "deleted": state_dir / "deleted",
        "audit": state_dir / "paper_lifecycle_audit.json",
        "writing_sdk": state_dir / "writing_sdk.json",
        "writing_sdk_lock": state_dir / "writing_sdk.lock",
        "figure_sdk": state_dir / "figure_sdk.json",
        "figure_sdk_lock": state_dir / "figure_sdk.lock",
        "writing_v1": state_dir / "writing_v1.json",
        "writing_v1_lock": state_dir / "writing_v1.lock",
        "smart": state_dir / "smart_submission" / "smart_submission_state.json",
    }
    monkeypatch.setattr(orchestrator_module, "LEGACY_PROJECTS", projects)
    monkeypatch.setattr(orchestrator_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(orchestrator_module, "UNIFIED_STATE_PATH", paths["unified"])
    monkeypatch.setattr(orchestrator_module, "UNIFIED_SUMMARY_PATH", paths["summary"])
    monkeypatch.setattr(orchestrator_module, "ARCHIVED_PAPERS_DIR", paths["archive"])
    monkeypatch.setattr(orchestrator_module, "DELETED_DIR", paths["deleted"])
    monkeypatch.setattr(orchestrator_module, "PAPER_LIFECYCLE_AUDIT_PATH", paths["audit"])
    monkeypatch.setattr(orchestrator_module, "WRITING_SDK_JOURNAL_PATH", paths["writing_sdk"])
    monkeypatch.setattr(orchestrator_module, "WRITING_SDK_LOCK_PATH", paths["writing_sdk_lock"])
    monkeypatch.setattr(orchestrator_module, "FIGURE_SDK_JOURNAL_PATH", paths["figure_sdk"])
    monkeypatch.setattr(orchestrator_module, "FIGURE_SDK_LOCK_PATH", paths["figure_sdk_lock"])
    monkeypatch.setattr(orchestrator_module, "WRITING_DISPATCH_STATE_PATH", paths["writing_v1"])
    monkeypatch.setattr(orchestrator_module, "WRITING_DISPATCH_LOCK_PATH", paths["writing_v1_lock"])
    monkeypatch.setattr(orchestrator_module, "SMART_SUBMISSION_STATE_PATH", paths["smart"])
    monkeypatch.setattr(io_utils, "BACKUP_DIR", state_dir / "backups")

    # Production uses Smart Submission's re-entrant OS lock.  Point that
    # module at the same temporary state path so the lifecycle code exercises
    # the real lock path without touching the workspace state.
    from unified_paper_orchestrator import smart_submission

    monkeypatch.setattr(smart_submission, "SMART_STATE_PATH", paths["smart"])
    return {"projects": projects, "paths": paths, "state_dir": state_dir}


def _orchestrator(
    projects: dict[str, dict[str, Any]],
    *,
    unified_before: dict[str, Any],
    adapter_states: dict[str, dict[str, Any]],
) -> tuple[UnifiedOrchestrator, dict[str, Any]]:
    instance = UnifiedOrchestrator.__new__(UnifiedOrchestrator)
    instance.adapters = {
        stage: _FakeAdapter(project, adapter_states.get(stage, {}))
        for stage, project in projects.items()
    }
    saved: dict[str, Any] = {}

    def sync(
        _self: UnifiedOrchestrator,
        persist: bool = True,
        *,
        reconcile_writing: bool = True,
    ) -> dict[str, Any]:
        return copy.deepcopy(unified_before)

    def save(_self: UnifiedOrchestrator, state: dict[str, Any]) -> None:
        saved["state"] = copy.deepcopy(state)

    instance.sync = MethodType(sync, instance)
    instance.save = MethodType(save, instance)
    return instance, saved


def _base_unified(*, archived: bool = False) -> dict[str, Any]:
    return {
        "version": 2,
        "papers": {} if archived else {PAPER_ID: {"paper_id": PAPER_ID}},
        "paper_queue": [PAPER_ID],
        "submission_routes": {PAPER_ID: {"execution_owner": "legacy_auto"}},
        "submission_route_operations": {PAPER_ID: {"paper_id": PAPER_ID}},
        "stage_admission_overrides": {PAPER_ID: {"figures": {"mode": "manual_override"}}},
        "archived_papers": (
            {PAPER_ID: {"status": "archived", "archive_dir": "placeholder"}} if archived else {}
        ),
        "history": [{"event": "paper_seen", "paper_id": PAPER_ID}],
    }


def test_archive_is_global_and_removes_v2_live_records(
    lifecycle_environment: dict[str, Any],
) -> None:
    projects = lifecycle_environment["projects"]
    paths = lifecycle_environment["paths"]
    writing_root = Path(projects["writing"]["root"])
    (writing_root / "ideas").mkdir()
    (writing_root / "ideas" / f"{PAPER_ID}.md").write_text("idea", encoding="utf-8")
    (writing_root / "refs" / PAPER_ID).mkdir(parents=True)
    (writing_root / "refs" / PAPER_ID / "references.md").write_text("refs", encoding="utf-8")
    for stage in ("writing", "figures"):
        paper_dir = Path(projects[stage]["papers_dir"]) / PAPER_ID
        paper_dir.mkdir()
        (paper_dir / f"{stage}.txt").write_text(stage, encoding="utf-8")

    adapter_states = {
        "writing": {
            "sessions": {PAPER_ID: {"status": "paused"}},
            "commands": [{"pipeline_id": PAPER_ID, "status": "pending"}],
            "history": [{"paper_id": PAPER_ID}],
        },
        "figures": {
            "papers": {PAPER_ID: {"status": "waiting_data"}},
            "paper_queue": [PAPER_ID],
            "commands": [{"paper_id": PAPER_ID, "status": "pending"}],
        },
        "journal_author": {"papers": {}},
        "submission": {"papers": {}},
    }
    write_json_atomic(
        paths["writing_sdk"],
        {
            "schema": "test",
            "papers": {
                f"session:{PAPER_ID}": {
                    "paper_id": PAPER_ID,
                    "status": "waiting_prerequisite",
                    "thread_id": "019f0000-0000-7000-8000-000000000001",
                }
            },
            "history": [{"paper_id": PAPER_ID}],
        },
    )
    write_json_atomic(
        paths["figure_sdk"],
        {"schema": "test", "papers": {PAPER_ID: {"paper_id": PAPER_ID, "status": "waiting_data"}}},
    )
    write_json_atomic(
        paths["writing_v1"],
        {"schema": "test", "claims": {}, "history": [{"paper_id": PAPER_ID}]},
    )
    write_json_atomic(
        paths["smart"],
        {"version": 1, "papers": {PAPER_ID: {"status": "queued"}}, "paper_queue": [PAPER_ID]},
    )
    orchestrator, saved = _orchestrator(
        projects,
        unified_before=_base_unified(),
        adapter_states=adapter_states,
    )

    result = orchestrator.archive_papers([PAPER_ID], force_active=True)

    assert result["archived"] == [PAPER_ID]
    assert not (writing_root / "ideas" / f"{PAPER_ID}.md").exists()
    assert not (writing_root / "refs" / PAPER_ID).exists()
    assert not (Path(projects["writing"]["papers_dir"]) / PAPER_ID).exists()
    assert not (Path(projects["figures"]["papers_dir"]) / PAPER_ID).exists()
    archive_dir = Path(result["archive_root"]) / PAPER_ID
    assert (archive_dir / "archive_manifest.json").is_file()
    assert PAPER_ID not in read_json(paths["writing_sdk"], {})["papers"]
    assert f"session:{PAPER_ID}" not in read_json(paths["writing_sdk"], {})["papers"]
    assert PAPER_ID not in read_json(paths["figure_sdk"], {})["papers"]
    assert PAPER_ID not in read_json(paths["smart"], {})["papers"]
    assert PAPER_ID not in saved["state"]["papers"]
    assert PAPER_ID in saved["state"]["archived_papers"]


def test_purge_removes_live_and_archived_files_and_keeps_only_hashed_audit(
    lifecycle_environment: dict[str, Any],
) -> None:
    projects = lifecycle_environment["projects"]
    paths = lifecycle_environment["paths"]
    live_dir = Path(projects["writing"]["papers_dir"]) / PAPER_ID
    live_dir.mkdir()
    (live_dir / "draft.tex").write_text("paper", encoding="utf-8")
    archived_dir = paths["archive"] / "20260716T010101" / PAPER_ID
    archived_dir.mkdir(parents=True)
    (archived_dir / "archived.tex").write_text("paper", encoding="utf-8")
    deleted_dir = paths["deleted"] / "20260716T000000" / PAPER_ID
    deleted_dir.mkdir(parents=True)
    (deleted_dir / "old.tex").write_text("paper", encoding="utf-8")

    adapter_states = {
        "writing": {"sessions": {PAPER_ID: {"status": "paused"}}},
        "figures": {"papers": {}},
        "journal_author": {"papers": {}},
        "submission": {"papers": {}},
    }
    write_json_atomic(
        paths["writing_sdk"],
        {"papers": {f"session:{PAPER_ID}": {"paper_id": PAPER_ID, "status": "paused"}}},
    )
    write_json_atomic(paths["figure_sdk"], {"papers": {}})
    write_json_atomic(paths["writing_v1"], {"claims": {}})
    write_json_atomic(paths["smart"], {"version": 1, "papers": {}})
    orchestrator, saved = _orchestrator(
        projects,
        unified_before=_base_unified(archived=True),
        adapter_states=adapter_states,
    )

    result = orchestrator.purge_papers([PAPER_ID], force_active=True)

    assert result["purged"] == [PAPER_ID]
    assert not live_dir.exists()
    assert not archived_dir.exists()
    assert not deleted_dir.exists()
    assert PAPER_ID not in saved["state"]["archived_papers"]
    audit_text = paths["audit"].read_text(encoding="utf-8")
    assert PAPER_ID not in audit_text
    assert hashlib.sha256(PAPER_ID.encode("utf-8")).hexdigest() in audit_text


def test_purge_does_not_remove_unrelated_sdk_record_with_provenance_reference(
    lifecycle_environment: dict[str, Any],
) -> None:
    projects = lifecycle_environment["projects"]
    paths = lifecycle_environment["paths"]
    unrelated_id = "2026-07-18_001_unrelated-paper"
    unrelated_key = "new-session:cmd-unrelated"
    unrelated_record = {
        "paper_key": unrelated_key,
        "paper_id": unrelated_id,
        "status": "running",
        "completed_step": 1,
        "steps": {
            "1": {
                "state": "completed",
                "output_baseline": {
                    "artifacts": [{"path": f"ideas/{PAPER_ID}.md"}],
                },
            }
        },
    }
    adapter_states = {
        "writing": {"sessions": {PAPER_ID: {"status": "paused"}}},
        "figures": {"papers": {}},
        "journal_author": {"papers": {}},
        "submission": {"papers": {}},
    }
    write_json_atomic(
        paths["writing_sdk"],
        {
            "papers": {
                f"session:{PAPER_ID}": {"paper_id": PAPER_ID, "status": "paused"},
                unrelated_key: unrelated_record,
            },
            "history": [
                {"event": "paper_seen", "paper_id": PAPER_ID},
                {
                    "event": "step_completed",
                    "paper_id": unrelated_id,
                    "artifacts": [{"path": f"ideas/{PAPER_ID}.md"}],
                },
            ],
        },
    )
    write_json_atomic(paths["figure_sdk"], {"papers": {}})
    write_json_atomic(paths["writing_v1"], {"claims": {}})
    write_json_atomic(paths["smart"], {"version": 1, "papers": {}})
    orchestrator, _saved = _orchestrator(
        projects,
        unified_before=_base_unified(),
        adapter_states=adapter_states,
    )

    result = orchestrator.purge_papers([PAPER_ID], force_active=True)

    assert result["purged"] == [PAPER_ID]
    sdk_state = read_json(paths["writing_sdk"], {})
    assert f"session:{PAPER_ID}" not in sdk_state["papers"]
    assert sdk_state["papers"][unrelated_key] == unrelated_record
    assert sdk_state["history"] == [
        {
            "event": "step_completed",
            "paper_id": unrelated_id,
            "artifacts": [{"path": f"ideas/{PAPER_ID}.md"}],
        }
    ]


def test_running_sdk_turn_must_finish_before_archive(
    lifecycle_environment: dict[str, Any],
) -> None:
    projects = lifecycle_environment["projects"]
    paths = lifecycle_environment["paths"]
    adapter_states = {
        "writing": {"sessions": {PAPER_ID: {"status": "running"}}},
        "figures": {"papers": {}},
        "journal_author": {"papers": {}},
        "submission": {"papers": {}},
    }
    write_json_atomic(
        paths["writing_sdk"],
        {"papers": {f"session:{PAPER_ID}": {"paper_id": PAPER_ID, "status": "running"}}},
    )
    write_json_atomic(paths["figure_sdk"], {"papers": {}})
    write_json_atomic(paths["writing_v1"], {"claims": {}})
    write_json_atomic(paths["smart"], {"version": 1, "papers": {}})
    orchestrator, _saved = _orchestrator(
        projects,
        unified_before=_base_unified(),
        adapter_states=adapter_states,
    )

    result = orchestrator.archive_papers([PAPER_ID], force_active=True)

    assert result["archived"] == []
    assert result["skipped"][0]["reason"] == "active_sdk_turn"
    assert PAPER_ID in read_json(paths["writing_sdk"], {})["papers"][f"session:{PAPER_ID}"]["paper_id"]
