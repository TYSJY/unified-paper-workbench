from __future__ import annotations

import json
from typing import Any

from unified_paper_orchestrator import __main__ as cli_module
from unified_paper_orchestrator import writing_sdk_runtime as runtime_module


def test_cli_runs_only_exact_selected_paper_to_temporary_step(monkeypatch: Any, capsys: Any) -> None:
    captured: dict[str, Any] = {}

    def run_writing_sdk(paper_keys: list[str] | None = None, *, until_step: int | None = None) -> dict[str, Any]:
        captured.update({"paper_keys": paper_keys, "until_step": until_step})
        return {"ok": True, "status": "complete"}

    monkeypatch.setattr(runtime_module, "run_writing_sdk", run_writing_sdk)

    exit_code = cli_module.main(["writing-sdk-run", "--paper-key", "paper:a", "--until-step", "2"])

    assert exit_code == 0
    assert captured == {"paper_keys": ["paper:a"], "until_step": 2}
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_cli_rejects_unselected_temporary_run(capsys: Any) -> None:
    exit_code = cli_module.main(["writing-sdk-run", "--until-step", "2"])

    assert exit_code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "WRITING_SDK_RUN_SELECTION_REQUIRED"


def test_cli_passes_explicit_archive_acknowledgement_to_recovery(monkeypatch: Any, capsys: Any) -> None:
    captured: dict[str, Any] = {}

    def requeue_recoverable(
        paper_keys: list[str] | None = None,
        *,
        replace_failed_step1_threads: bool = False,
        archived_thread_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        captured.update(
            {
                "paper_keys": paper_keys,
                "replace": replace_failed_step1_threads,
                "archived": archived_thread_ids,
            }
        )
        return {"ok": True, "status": "requeued"}

    monkeypatch.setattr(runtime_module, "requeue_recoverable", requeue_recoverable)

    exit_code = cli_module.main(
        [
            "writing-sdk-requeue-recoverable",
            "--paper-key",
            "new-session:one",
            "--replace-failed-step1-threads",
            "--archived-thread-id",
            "thread-one",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "paper_keys": ["new-session:one"],
        "replace": True,
        "archived": ["thread-one"],
    }
    assert json.loads(capsys.readouterr().out)["status"] == "requeued"


def test_cli_recovery_requires_an_exact_selection(capsys: Any) -> None:
    exit_code = cli_module.main(["writing-sdk-requeue-recoverable"])

    assert exit_code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "WRITING_SDK_RECOVERY_SELECTION_REQUIRED"


def test_cli_revalidates_only_one_exact_last_completed_paper(monkeypatch: Any, capsys: Any) -> None:
    captured: list[str] = []

    def revalidate_last_completed(paper_key: str) -> dict[str, Any]:
        captured.append(paper_key)
        return {"ok": True, "status": "reopened", "dispatched": False}

    monkeypatch.setattr(runtime_module, "revalidate_last_completed", revalidate_last_completed)

    exit_code = cli_module.main(["writing-sdk-revalidate-last-completed", "--paper-key", "session:paper-a"])

    assert exit_code == 0
    assert captured == ["session:paper-a"]
    assert json.loads(capsys.readouterr().out)["status"] == "reopened"


def test_cli_revalidation_requires_exact_paper_key(capsys: Any) -> None:
    exit_code = cli_module.main(["writing-sdk-revalidate-last-completed"])

    assert exit_code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "WRITING_SDK_REVALIDATE_SELECTION_REQUIRED"
