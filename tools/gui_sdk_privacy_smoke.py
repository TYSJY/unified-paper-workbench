"""Synthetic UI fixture; never reads the operator's accounts or starts a workflow.

Run as: python -m tools.gui_sdk_privacy_smoke qt|tk [--seconds 300]
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace


def show_tk(seconds: int) -> None:
    import tkinter as tk
    from unified_paper_orchestrator.ui.account_site_manager import AccountSiteManager
    root = tk.Tk()
    root.withdraw()
    rows = [{"record_key": "smart:fixture-record", "account_id": "fixture-record", "username": "demo-login",
             "provider": "Demo Mail (fictional)", "scope": "email", "email": "demo@example.test",
             "source_kind": "smart", "status": "verified", "login_url": "https://example.test/login"}]
    store = SimpleNamespace(vault_read_only=False, list_account_summaries=lambda: rows,
                            list_systems=lambda: [], list_journals=lambda: [],
                            is_account_read_only=lambda _: False)
    window = AccountSiteManager(root, store=store)
    window.wm_transient("")
    window.deiconify()
    window.lift()
    window.title("隐私界面验证 - 虚构账号")
    window.protocol("WM_DELETE_WINDOW", root.destroy)
    root.after(seconds * 1000, root.destroy)
    root.mainloop()


def show_qt(seconds: int, catalog_path: Path | None = None) -> None:
    from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from unified_paper_orchestrator.ui_next.i18n import I18nManager
    detected = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path else None

    class Fixture(QObject):
        settingsDataChanged = Signal()
        modelCatalogsChanged = Signal()
        commercialActionCompleted = Signal(str, object)
        codexAccountActionCompleted = Signal(str, object)

        @Property("QVariantMap", constant=True)
        def settingsData(self):
            return {"stages": [{"key": "writing", "label": "写论文", "parallel": 4, "parallelMax": 10,
                "defaultModel": "gpt-5.5" if detected else "fixture-a", "defaultReasoning": "high", "stepCount": 2,
                "steps": [{"position": 1, "stepId": "1", "name": "正常继承配置", "model": "",
                    "reasoning": "", "effectiveModel": "fixture-a", "tokenLimit": 1000000},
                    {"position": 2, "stepId": "2", "name": "保留旧的不可用模型", "model": "old-unavailable",
                    "reasoning": "minimal", "effectiveModel": "old-unavailable", "tokenLimit": 2000000}]}]}

        @Property("QVariantMap", constant=True)
        def codexAccountStatus(self):
            return {"statusKnown": True, "authenticated": True}

        @Property("QVariantMap", notify=modelCatalogsChanged)
        def modelCatalogs(self):
            if detected:
                return {"writing": detected}
            return {"writing": {"state": "ready", "configuredModel": "fixture-a", "models": [
                {"model": "fixture-a", "display_name": "SDK fixture A", "supported_reasoning_efforts": ["low", "high"]},
                {"model": "fixture-b", "display_name": "SDK fixture B", "supported_reasoning_efforts": ["minimal"]}]}}

        @Slot(str)
        def refreshModels(self, stage):
            self.modelCatalogsChanged.emit()

        @Slot()
        def refreshCodexAccount(self):
            pass

    app = QGuiApplication([])
    fixture = Fixture()
    with tempfile.TemporaryDirectory(prefix="upo-gui-fixture-") as temp:
        i18n = I18nManager(settings_path=Path(temp) / "ui.ini")
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("fixture", fixture)
        engine.rootContext().setContextProperty("i18n", i18n)
        components = Path(__file__).resolve().parents[1] / "unified_paper_orchestrator/ui_next/qml/components"
        qml = '''import QtQuick
import QtQuick.Controls
import "components" as UI
ApplicationWindow {
 width: 1280; height: 900; visible: true; title: "SDK 设置界面验证 - 隔离测试"
 UI.SettingsCenter { id: settings; controllerRef: fixture }
 Component.onCompleted: settings.open()
}'''
        engine.loadData(qml.encode(), QUrl.fromLocalFile(str(components.parent / "Fixture.qml")))
        if not engine.rootObjects():
            raise RuntimeError("Settings fixture QML failed to load")
        QTimer.singleShot(seconds * 1000, app.quit)
        app.exec()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("qt", "tk"))
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args()
    if args.mode == "qt":
        show_qt(args.seconds, args.catalog)
    else:
        show_tk(args.seconds)
