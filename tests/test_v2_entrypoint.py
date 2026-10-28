from __future__ import annotations

from pathlib import Path

from unified_paper_orchestrator_v2 import __main__ as entrypoint
from unified_paper_orchestrator_v2 import ui


def test_no_subcommand_opens_ui(monkeypatch, tmp_path: Path) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(ui, "launch_ui", lambda database: opened.append(database))

    assert entrypoint.main(["--database", str(tmp_path / "v2.sqlite3")]) == 0
    assert opened == [tmp_path / "v2.sqlite3"]


def test_explicit_ui_subcommand_opens_ui(monkeypatch, tmp_path: Path) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(ui, "launch_ui", lambda database: opened.append(database))

    assert entrypoint.main(["--database", str(tmp_path / "v2.sqlite3"), "ui"]) == 0
    assert opened == [tmp_path / "v2.sqlite3"]


def test_local_api_controller_lifecycle(tmp_path: Path) -> None:
    kernel = ui.WorkflowKernel(ui.EventStore(tmp_path / "v2.sqlite3"))
    controller = ui.LocalApiController(kernel)

    host, port = controller.start(port=0)
    try:
        assert host == "127.0.0.1"
        assert port > 0
        assert controller.running is True
        assert controller.start(port=0) == (host, port)
    finally:
        controller.stop()
    assert controller.running is False

