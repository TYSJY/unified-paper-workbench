from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from unified_paper_orchestrator import ai_execution_status as status_module
from unified_paper_orchestrator import automation as automation_module
from unified_paper_orchestrator import legacy_automation_registration as registration


def _projects(tmp_path: Path) -> dict[str, dict[str, object]]:
    return {
        "writing": {
            "root": tmp_path / "writing-project" / "edge-llm-research",
            "primary_automation_id": "writing-scheduler",
        },
        "figures": {
            "root": tmp_path / "figure-project" / "paper-figure-codex",
            "primary_automation_id": "figure-scheduler",
        },
        "journal_author": {
            "root": tmp_path / "journal-project",
            "primary_automation_id": "journal-scheduler",
        },
        "submission": {
            "root": tmp_path / "submission-project",
            "primary_automation_id": "submission-scheduler",
        },
    }


def _write_registered_automations(root: Path, projects: dict[str, dict[str, object]]) -> None:
    for stage, project in projects.items():
        automation_id = str(project["primary_automation_id"])
        target = Path(project["root"])
        if stage in {"writing", "figures"}:
            target = target.parent
        path = root / automation_id / "automation.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        target_text = json.dumps(target.as_posix())
        path.write_text(
            "\n".join(
                (
                    "version = 1",
                    f"id = {json.dumps(automation_id)}",
                    'kind = "cron"',
                    f"name = {json.dumps(automation_id)}",
                    'prompt = "read the immutable scheduler prompt"',
                    'status = "PAUSED"',
                    'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                    'model = "gpt-5.5"',
                    'reasoning_effort = "xhigh"',
                    'execution_environment = "local"',
                    f'target = {{ type = "project", project_id = {target_text} }}',
                    f"cwds = [{target_text}]",
                    "created_at = 1",
                    "updated_at = 1",
                    "",
                )
            ),
            encoding="utf-8",
        )
    writing_id = str(projects["writing"]["primary_automation_id"])
    (root / writing_id / "memory.md").write_text("consumer probe completed\n", encoding="utf-8")


def _consumer_probe(projects: dict[str, dict[str, object]]) -> dict[str, str]:
    return {
        "stage": "writing",
        "thread_id": "12345678-1234-4234-8234-123456789abc",
        "command_id": "probe-command",
        "handled_at": "2026-07-13T01:33:17+08:00",
        "result": "consumed",
    }


def test_official_receipt_allows_mutable_status_schedule_and_launcher_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _projects(tmp_path)
    root = tmp_path / "automations"
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(registration, "LEGACY_PROJECTS", projects)
    _write_registered_automations(root, projects)

    registration.create_official_registration_receipt(
        root,
        consumer_probe=_consumer_probe(projects),
        destination=receipt_path,
    )
    writing = root / str(projects["writing"]["primary_automation_id"]) / "automation.toml"
    text = writing.read_text(encoding="utf-8")
    text = text.replace('status = "PAUSED"', 'status = "ACTIVE"')
    text = text.replace("INTERVAL=5", "INTERVAL=1")
    text = text.replace("read the immutable scheduler prompt", "updated launcher text")
    writing.write_text(text, encoding="utf-8")

    result = registration.verify_official_registration(root, receipt_path=receipt_path)

    assert result["verified"] is True
    assert result["consumer_probe_verified"] is True
    assert all(result["stages"].values())


def test_receipt_rejects_changed_immutable_registration_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _projects(tmp_path)
    root = tmp_path / "automations"
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(registration, "LEGACY_PROJECTS", projects)
    _write_registered_automations(root, projects)
    registration.create_official_registration_receipt(
        root,
        consumer_probe=_consumer_probe(projects),
        destination=receipt_path,
    )
    figure = root / str(projects["figures"]["primary_automation_id"]) / "automation.toml"
    figure.write_text(
        figure.read_text(encoding="utf-8").replace('model = "gpt-5.5"', 'model = "different-model"'),
        encoding="utf-8",
    )

    result = registration.verify_official_registration(root, receipt_path=receipt_path)

    assert result["verified"] is False
    assert result["stages"]["figures"] is False
    assert "LEGACY_AUTOMATION_REGISTRATION_INVALID_FIGURES" in result["blockers"]


def test_existing_toml_without_official_receipt_remains_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _projects(tmp_path)
    root = tmp_path / "automations"
    monkeypatch.setattr(registration, "LEGACY_PROJECTS", projects)
    _write_registered_automations(root, projects)

    result = registration.verify_official_registration(root, receipt_path=tmp_path / "missing.json")

    assert result["verified"] is False
    assert "LEGACY_AUTOMATION_REGISTRATION_RECEIPT_MISSING" in result["blockers"]


class _ModeStore:
    @contextmanager
    def hold_execution_mode(self, _required_mode: str):
        yield object()


def test_controller_rejects_an_unverified_existing_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _projects(tmp_path)
    root = tmp_path / "automations"
    _write_registered_automations(root, projects)
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", root)
    controller = automation_module.SchedulerAutomationController(
        execution_mode_store=_ModeStore(),
        consumer_verifier=lambda _stage: False,
    )
    controller.projects = projects

    with pytest.raises(automation_module.LegacyAutomationUnverifiedError):
        controller.require_controllable("writing")


def test_ai_execution_status_reports_verified_official_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = _projects(tmp_path)
    root = tmp_path / "automations"
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(registration, "LEGACY_PROJECTS", projects)
    monkeypatch.setattr(registration, "REGISTRATION_PATH", receipt_path)
    monkeypatch.setattr(automation_module, "LEGACY_PROJECTS", projects)
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", root)
    _write_registered_automations(root, projects)
    registration.create_official_registration_receipt(
        root,
        consumer_probe=_consumer_probe(projects),
        destination=receipt_path,
    )

    result = status_module._probe_legacy_bridge()

    assert result["ready"] is True
    assert result["consumer_verified"] is True
    assert result["consumer_probe_verified"] is True
    assert result["control_plane"] == "official_codex_automation"
    assert result["blockers"] == []
