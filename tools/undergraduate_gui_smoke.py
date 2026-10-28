"""Offline main-window smoke test in a fresh, explicitly isolated workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("Smoke output must be a new directory")
    output.mkdir(parents=True)
    os.environ.update({
        "UNIFIED_PAPER_ORCHESTRATOR_ROOT": str(output),
        "UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT": str(output),
        "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY": "1", "UNIFIED_PAPER_WORKSPACE": "",
        "UNIFIED_WRITING_ROOT": str(output / "writing"), "UNIFIED_FIGURE_ROOT": str(output / "figures"),
        "UNIFIED_JOURNAL_AUTHOR_ROOT": str(output / "journal_author"), "UNIFIED_SUBMISSION_ROOT": str(output / "submission"),
        "USERPROFILE": str(output / "profile"), "HOME": str(output / "profile"),
        "APPDATA": str(output / "profile" / "Roaming"), "LOCALAPPDATA": str(output / "profile" / "Local"),
        "CODEX_HOME": str(output / "codex"), "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software", "QT_QUICK_CONTROLS_STYLE": "Basic",
    })
    from PySide6.QtCore import QUrl, QObject, QMetaObject
    from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    from unified_paper_orchestrator.ui_next.controller import WorkbenchController
    from unittest.mock import patch
    app = QGuiApplication([])
    for name in ("msyh.ttc", "segoeui.ttf", "segmdl2.ttf"):
        QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name))
    app.setFont(QFont("Microsoft YaHei"))
    with patch.object(WorkbenchController, "_bootstrap_commercial", lambda _: None):
        controller = WorkbenchController()
    controller._projection_timer.stop()
    controller._sync_timer.stop()
    controller.i18n.setLocale("zh_CN")
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(error.toString() for error in values))
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("i18n", controller.i18n)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "unified_paper_orchestrator/ui_next/qml/Main.qml")))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    try:
        QTest.qWait(150)
        assert window.findChild(QObject, "newWritingProject") is not None
        scheme_title = window.findChild(QObject, "writingSchemeTitle")
        new_paper = window.findChild(QObject, "newWritingProject")
        assert scheme_title.property("text") == "SCI / 期刊论文"
        calls = []
        controller.countRequested.connect(lambda *args: calls.append("journal"))
        controller.undergraduateBriefRequested.connect(lambda *args: calls.append("undergraduate"))
        for scheme in ("undergraduate_humanities", "journal", "undergraduate_software", "journal"):
            controller.orchestrator.select_writing_prompt_scheme(scheme)
            inventory = controller.orchestrator.writing_workflow_store.inventory()
            controller._task_completed("refresh_settings", {"stages": [{"key": "writing",
                "promptScheme": inventory["prompt_scheme"], "promptSchemes": inventory["prompt_schemes"],
                "steps": []}]}, 0)
            QTest.qWait(50)
            undergraduate = scheme.startswith("undergraduate_")
            assert scheme_title.property("text") == ("本科毕业论文" if undergraduate else "SCI / 期刊论文")
            assert QMetaObject.invokeMethod(new_paper, "clicked")
            assert calls[-1] == ("undergraduate" if undergraduate else "journal")
            assert QMetaObject.invokeMethod(window.findChild(QObject, "undergraduateBrief" if undergraduate else "countPopup"), "close")
        assert window.grabWindow().save(str(output / "schemes-switched.png"))
        assert window.grabWindow().save(str(output / "main.png"))
        controller.prepareAction("new_undergraduate")
        QTest.qWait(100)
        title = window.findChild(QObject, "undergraduateTitle")
        assert title is not None
        title.setProperty("text", "本科毕业论文入口验证（不启动写作）")
        assert window.grabWindow().save(str(output / "brief.png"))
        QMetaObject.invokeMethod(window.findChild(QObject, "undergraduateBrief"), "close")
        from types import SimpleNamespace
        from unified_paper_orchestrator.codex_sdk_account import CodexLoginChallenge
        tasks = []
        account = SimpleNamespace(
            begin_device_login=lambda: CodexLoginChallenge("offline-attempt", "device_code", "https://auth.example.test/device", "TEST-ONLY"),
            verified_status=lambda: {"authenticated": False},
            cancel_login=lambda attempt: attempt == "offline-attempt",
        )
        with patch.object(controller, "codex_account", account), patch.object(
            controller, "_submit", side_effect=lambda *args, **kwargs: tasks.append(args)
        ), patch("unified_paper_orchestrator.ui_next.controller.QDesktopServices.openUrl", return_value=True):
            settings = window.findChild(QObject, "settingsCenter")
            QMetaObject.invokeMethod(settings, "open")
            window.findChild(QObject, "settingsTabs").setProperty("currentIndex", 2)
            device = window.findChild(QObject, "connectCodexDeviceAccount")
            cancel = window.findChild(QObject, "cancelCodexLogin")
            refresh = window.findChild(QObject, "refreshCodexAccount")
            code = window.findChild(QObject, "codexDeviceCode")
            QMetaObject.invokeMethod(device, "clicked")
            assert tasks[-1][0] == "codex:begin_login"
            controller._complete_codex_account_task("codex:begin_login", tasks[-1][2]())
            QTest.qWait(100)
            assert code.property("text") == "TEST-ONLY"
            assert not device.property("enabled")
            assert refresh.property("enabled") and cancel.property("enabled")
            assert window.grabWindow().save(str(output / "login-recovery.png"))
            QMetaObject.invokeMethod(refresh, "clicked")
            assert tasks[-1][0] == "codex:status"
            controller._complete_codex_account_task("codex:status", tasks[-1][2]())
            assert controller.codexAccountStatus["loginPending"]
            QMetaObject.invokeMethod(cancel, "clicked")
            assert tasks[-1][0] == "codex:cancel_login" and tasks[-1][2]()
            controller._complete_codex_account_task("codex:wait_login", {
                "success": False, "errorCode": "CODEX_LOGIN_CANCELLED", "status": {"authenticated": False}})
            assert code.property("text") == "" and device.property("enabled")
            assert not controller.codexAccountStatus["loginPending"]
            QMetaObject.invokeMethod(settings, "close")
        # Exercise the visible PDF shortcut and folder action in an isolated
        # paper with the same split writing/figure layout as production.
        paper_id = "offline-output-location-test"
        source = output / "writing" / "papers" / paper_id / "draft_v6.tex"
        pdf = output / "figures" / "papers" / paper_id / "submit" / f"{paper_id}.pdf"
        source.parent.mkdir(parents=True)
        source.write_text("offline fixture", encoding="utf-8")
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-test-fixture-not-a-real-paper")
        controller._state = {"papers": {paper_id: {"paper_id": paper_id, "title": "论文 PDF 入口测试",
            "current_stage": "journal_author", "stages": {
                "writing": {"status": "complete", "artifact": str(source)},
                "figures": {"status": "complete", "artifact": str(pdf.parent)},
                "journal_author": {"status": "incomplete"}}}}}
        controller.selectPage("figures")
        controller.selectPaper(paper_id)
        QTest.qWait(100)
        shortcut = window.findChild(QObject, "openSelectedPaperPdf")
        assert shortcut.property("visible")
        opened = []
        with patch("unified_paper_orchestrator.ui_next.controller.os.startfile", side_effect=opened.append):
            QMetaObject.invokeMethod(shortcut, "clicked")
            controller.prepareSelectedAction("open_paper_folder")
        assert opened == [str(pdf.resolve()), str(pdf.parent.resolve())], opened
        action_keys = [a["key"] for a in controller.selectedActions]
        assert "open_paper_pdf" in action_keys and "open_paper_folder" in action_keys
        assert window.grabWindow().save(str(output / "paper-pdf-entry.png"))
        assert not warnings, warnings
        print(json.dumps({"ok": True, "output": str(output), "model_turns": 0, "warnings": warnings}, ensure_ascii=False))
    finally:
        window.close()
        controller.shutdown()
        engine.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    main()
