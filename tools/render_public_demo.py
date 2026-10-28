"""Render real Qt UI with fictional data, no accounts and no model calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if not output.is_relative_to(repo / "build") or output.exists():
        raise ValueError("Use a new output directory under build/.")
    output.mkdir(parents=True)
    profile = output / "demo-profile"
    os.environ.update({
        "UNIFIED_PAPER_ORCHESTRATOR_ROOT": str(output),
        "UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT": str(output),
        "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY": "1", "UNIFIED_PAPER_WORKSPACE": "",
        "USERPROFILE": str(profile), "HOME": str(profile),
        "APPDATA": str(profile / "Roaming"), "LOCALAPPDATA": str(profile / "Local"),
        "CODEX_HOME": str(output / "unused-codex"),
        "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
        "QT_QUICK_CONTROLS_STYLE": "Basic",
        **{env: str(output / stage) for env, stage in [
            ("UNIFIED_WRITING_ROOT", "writing"), ("UNIFIED_FIGURE_ROOT", "figures"),
            ("UNIFIED_JOURNAL_AUTHOR_ROOT", "journal_author"), ("UNIFIED_SUBMISSION_ROOT", "submission")]},
    })
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    from unified_paper_orchestrator.ui_next.controller import WorkbenchController

    app = QGuiApplication([])
    for font in ("msyh.ttc", "segoeui.ttf", "segmdl2.ttf"):
        path = Path("C:/Windows/Fonts") / font
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont("Microsoft YaHei"))
    with patch.object(WorkbenchController, "_bootstrap_commercial", lambda _: None):
        controller = WorkbenchController()
    controller._projection_timer.stop()
    controller._sync_timer.stop()
    controller.i18n.setLocale("zh_CN")
    stages = ("writing", "figures", "journal_author", "submission")
    scenarios = [
        ("demo-001", "示例：软件系统的设计与实现", "figures", ("complete", "complete", "missing", "missing")),
        ("demo-002", "示例：公共服务满意度研究", "writing", ("waiting_data", "missing", "missing", "missing")),
        ("demo-003", "示例：本地推理性能评估", "journal_author", ("complete", "complete", "ready", "missing")),
    ]
    papers = {}
    for pid, title, current, statuses in scenarios:
        papers[pid] = {"paper_id": pid, "title": title, "current_stage": current,
            "updated_at": "2026-10-28T12:00:00+08:00", "stages": {
                stage: {"status": status, "detail": "虚构演示任务 · 展示界面，不执行研究或模型调用。",
                        "workflow_step_count": 3, "step_done": 3 if status == "complete" else 1}
                for stage, status in zip(stages, statuses)}}
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(error.toString() for error in values))
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("i18n", controller.i18n)
    engine.load(QUrl.fromLocalFile(str(repo / "unified_paper_orchestrator/ui_next/qml/Main.qml")))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    try:
        controller._apply_state({"papers": papers}, duration_ms=0)
        controller.selectPaper("demo-001")
        QTest.qWait(180)
        assert window.grabWindow().save(str(output / "workbench-overview.png"))
        controller.selectPage("figures")
        QTest.qWait(100)
        assert window.grabWindow().save(str(output / "workbench-pdf.png"))
        assert not warnings, warnings
        result = {"ok": True, "synthetic_data": True, "model_turns": 0, "qml_warnings": warnings,
                  "images": ["workbench-overview.png", "workbench-pdf.png"]}
        (output / "receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        window.close()
        controller.shutdown()
        engine.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    main()
