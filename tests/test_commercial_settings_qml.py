from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import Property, QObject, Slot, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_QML = (
    ROOT
    / "unified_paper_orchestrator"
    / "ui_next"
    / "qml"
    / "components"
    / "SettingsCenter.qml"
)
ONBOARDING_QML = (
    ROOT
    / "unified_paper_orchestrator"
    / "ui_next"
    / "qml"
    / "components"
    / "CommercialOnboarding.qml"
)


class _I18nStub(QObject):
    @Property(int, constant=True)
    def revision(self) -> int:
        return 0

    @Slot(str, int, result=str)
    def text(self, key: str, _revision: int) -> str:
        return key


def test_commercial_settings_qml_loads_without_binding_errors() -> None:
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    i18n = _I18nStub()
    engine.rootContext().setContextProperty("i18n", i18n)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(SETTINGS_QML)))
    assert component.errors() == []
    instance = component.create()
    try:
        assert instance is not None, "\n".join(error.toString() for error in component.errors())
    finally:
        if instance is not None:
            instance.deleteLater()
        engine.deleteLater()
        app.processEvents()


def test_commercial_onboarding_qml_loads_without_binding_errors() -> None:
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    i18n = _I18nStub()
    engine.rootContext().setContextProperty("i18n", i18n)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ONBOARDING_QML)))
    assert component.errors() == []
    instance = component.create()
    try:
        assert instance is not None, "\n".join(error.toString() for error in component.errors())
    finally:
        if instance is not None:
            instance.deleteLater()
        engine.deleteLater()
        app.processEvents()
