from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, QMetaObject
from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

from unified_paper_orchestrator.ui_next.i18n import I18nManager
from unified_paper_orchestrator.writing_workflow import WritingWorkflowStore


def test_switch_and_brief_controls_render_and_bind_without_real_execution(tmp_path):
    store = WritingWorkflowStore(tmp_path / "workflow.json", tmp_path / "custom")

    class Fixture(QObject):
        undergraduateCreated = Signal()
        settingsDataChanged = Signal()
        modelCatalogsChanged = Signal()
        commercialActionCompleted = Signal(str, object)
        codexAccountActionCompleted = Signal(str, object)
        calls = []

        def __init__(self):
            super().__init__()
            from unified_paper_orchestrator.ui_next.undergraduate_materials import UndergraduateMaterials
            self.materials = UndergraduateMaterials(tmp_path / "materials", self)

        @Property(QObject, constant=True)
        def undergraduateMaterials(self):
            return self.materials

        @Property(bool, constant=True)
        def loading(self):
            return False

        @Slot(str, result="QVariantMap")
        def undergraduateScheme(self, identifier):
            return {"version": "fixture-version", "error": ""}

        @Slot(str, result=str)
        def previewUndergraduateFigures(self, identifier):
            from unified_paper_orchestrator.undergraduate_figures import figure_scheme_steps
            return "\n".join(i["prompt_text"] for i in figure_scheme_steps(identifier))

        @Property("QVariantMap", notify=settingsDataChanged)
        def settingsData(self):
            inventory = store.inventory()
            return {"commercial": {"account": {"devices": []}}, "stages": [{"key": "writing", "label": "写论文", "parallel": 4, "parallelMax": 10,
                "defaultModel": "fixture-model", "defaultReasoning": "", "stepCount": 10,
                "promptScheme": inventory["prompt_scheme"], "promptSchemes": inventory["prompt_schemes"],
                "steps": [{"position": step["position"], "stepId": step["step_id"], "kind": step["kind"],
                           "name": step["name"], "effectiveModel": "fixture-model", "tokenLimit": 1000000}
                          for step in inventory["steps"]]}]}

        @Property("QVariantMap", constant=True)
        def codexAccountStatus(self):
            return {"statusKnown": True, "authenticated": False}

        @Property("QVariantMap", constant=True)
        def modelCatalogs(self):
            return {"writing": {"state": "ready", "models": [{"model": "fixture-model", "display_name": "测试模型（离线）",
                                                               "supported_reasoning_efforts": ["low", "high"]}]}}

        @Slot(str)
        def refreshModels(self, _stage):
            pass

        @Slot()
        def refreshCodexAccount(self):
            pass

        @Slot(str, str, str, str)
        def runWorkflowAction(self, stage, action, _step, value):
            assert stage == "writing" and action == "scheme"
            store.select_prompt_scheme(value)
            self.settingsDataChanged.emit()

        @Slot(str, str, str, str, str)
        def createUndergraduateProject(self, title, requirements, minimum, identifier, version):
            self.last_materials = self.materials.snapshot()
            self.calls.append((title, requirements, minimum, version))
            self.undergraduateCreated.emit()

    app = QGuiApplication.instance() or QGuiApplication([])
    # The offscreen Windows platform has no automatic system-font discovery.
    for font_name in ("msyh.ttc", "segoeui.ttf", "segmdl2.ttf"):
        font_path = Path("C:/Windows/Fonts") / font_name
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei"))
    i18n = I18nManager(settings_path=tmp_path / "ui.ini")
    i18n.setLocale("zh_CN")
    fixture = Fixture()
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(error.toString() for error in values))
    engine.rootContext().setContextProperty("i18n", i18n)
    engine.rootContext().setContextProperty("fixture", fixture)
    qml = '''import QtQuick
import QtQuick.Controls
import "components" as UI
ApplicationWindow {
    width: 1280; height: 850; visible: true
    UI.SettingsCenter { id: settings; objectName: "settings"; controllerRef: fixture }
    UI.UndergraduateBrief { id: brief; objectName: "brief"; controllerRef: fixture }
    function showBrief() { settings.close(); brief.prepare("本科人文社科版", "fixture-version") }
    Component.onCompleted: settings.open()
}'''
    ui_root = Path(__file__).resolve().parents[1] / "unified_paper_orchestrator" / "ui_next" / "qml"
    engine.loadData(qml.encode(), QUrl.fromLocalFile(str(ui_root / "UndergraduateFixture.qml")))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    try:
        QTest.qWait(200)
        choice = window.findChild(QObject, "writingPromptScheme")
        apply = window.findChild(QObject, "applyWritingPromptScheme")
        assert choice.property("count") == 3
        choice.setProperty("currentIndex", 1)
        assert QMetaObject.invokeMethod(apply, "clicked")
        QTest.qWait(150)
        assert store.inventory()["prompt_scheme"] == "undergraduate_humanities"
        assert choice.property("currentIndex") == 1
        assert store.inventory()["steps"][0]["name"] == "接收题目与学校要求"
        output = os.environ.get("UPO_QA_OUTPUT_DIR")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            assert window.grabWindow().save(str(Path(output) / "prompt-scheme.png"))
        QMetaObject.invokeMethod(window, "showBrief")
        QTest.qWait(100)
        title = window.findChild(QObject, "undergraduateTitle")
        requirements = window.findChild(QObject, "undergraduateRequirements")
        minimum = window.findChild(QObject, "undergraduateMinimumReferences")
        start = window.findChild(QObject, "startUndergraduateWriting")
        assert not start.property("enabled")
        title.setProperty("text", "社区公共空间的使用分析（界面测试）")
        requirements.setProperty("text", "这是隔离界面测试，不创建论文任务。\n学校要求：中文正文，章节与排版按提供的模板执行。")
        minimum.setProperty("text", "20")
        QTest.qWait(100)
        assert start.property("enabled")
        if output:
            assert window.grabWindow().save(str(Path(output) / "undergraduate-brief.png"))
        QMetaObject.invokeMethod(start, "clicked")
        assert fixture.calls == [(title.property("text"), requirements.property("text"), "20", "fixture-version")]
        # Real local import worker + preview + acknowledgement, still no real model.
        QMetaObject.invokeMethod(window, "showBrief")
        title.setProperty("text", "带学校资料的本科题目")
        school_file = tmp_path / "学校格式.txt"
        school_file.write_text("学校规范：图按章编号，图注使用中文。", encoding="utf-8")
        fixture.materials.add([QUrl.fromLocalFile(str(school_file)).toString()], "requirements")
        assert fixture.materials.busy
        assert not start.property("enabled")
        for _ in range(300):
            if not fixture.materials.busy:
                break
            QTest.qWait(20)
        assert not fixture.materials.busy and len(fixture.materials.items) == 1
        assert "图按章编号" in fixture.materials.preview(fixture.materials.items[0]["id"])
        assert not start.property("enabled")
        reviewed = window.findChild(QObject, "undergraduateMaterialsReviewed")
        reviewed.setProperty("checked", True)
        QTest.qWait(60)
        assert start.property("enabled")
        if output:
            assert window.grabWindow().save(str(Path(output) / "undergraduate-imported.png"))
        QMetaObject.invokeMethod(start, "clicked")
        assert len(fixture.last_materials) == 1 and len(fixture.calls) == 2
        assert not warnings, "\n".join(warnings)
    finally:
        window.close()
        engine.deleteLater()
        app.processEvents()
