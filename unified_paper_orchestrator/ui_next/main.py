from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine

from .. import __version__
from ..config import STATE_DIR
from .controller import WorkbenchController


def _configure_logging() -> None:
    log_dir = Path(STATE_DIR) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(handler, "_qt_workbench_handler", False) for handler in root.handlers):
        handler = RotatingFileHandler(
            log_dir / "qt_workbench.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler._qt_workbench_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def _packaged_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / relative
    return Path(__file__).resolve().parents[2] / relative


def main() -> int:
    """Launch the modern workbench without changing workflow ownership."""

    _configure_logging()
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Unified Paper Workbench")

    icon_path = _packaged_path("unified_paper_orchestrator/ui/assets/app_icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    controller = WorkbenchController()

    def sync_application_name() -> None:
        os.environ["UNIFIED_PAPER_UI_LOCALE"] = str(controller.i18n.locale)
        display_name = controller.i18n.translate("app.title", "大一统论文智能工作台")
        app.setApplicationName(display_name)
        app.setApplicationDisplayName(display_name)

    sync_application_name()
    controller.i18n.languageChanged.connect(sync_application_name)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("i18n", controller.i18n)
    qml_path = _packaged_path("unified_paper_orchestrator/ui_next/qml/Main.qml")
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if not engine.rootObjects():
        logging.getLogger(__name__).error("QML root failed to load from %s", qml_path)
        return 1

    app.aboutToQuit.connect(controller.shutdown)
    controller.quitRequested.connect(app.quit)
    QTimer.singleShot(220, controller.refresh)
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
