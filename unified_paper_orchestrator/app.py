from __future__ import annotations

import ctypes
import logging
import sys


def _detach_frozen_gui_console() -> None:
    """Detach the console inherited by the hybrid Windows executable in GUI mode."""

    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        # A detached GUI must remain usable even when no console is attached.
        pass


def _show_modern_startup_error(error: BaseException) -> None:
    """Report a modern-workbench startup failure without reopening legacy UI."""

    logging.getLogger(__name__).exception(
        "Modern workbench startup failed",
        exc_info=(type(error), error, error.__traceback__),
    )
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "新版工作台启动失败，请查看 state/logs/qt_workbench.log。\n\n"
            "The modern workbench could not start. See state/logs/qt_workbench.log.",
            "大一统论文智能工作台 / Unified Paper Workbench",
            0x10,
        )
    except (AttributeError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    """Run the GUI without arguments and the real CLI when arguments are present."""

    args = list(sys.argv[1:] if argv is None else argv)
    from unified_paper_orchestrator.workspace import (
        bootstrap_process_workspace,
        extract_workspace_argument,
    )

    bootstrap_process_workspace(args)
    _selected_workspace, non_workspace_args = extract_workspace_argument(args)
    if non_workspace_args:
        from unified_paper_orchestrator.__main__ import main as cli_main

        return cli_main(args)

    _detach_frozen_gui_console()
    try:
        from unified_paper_orchestrator.ui_next import main as ui_main

        return int(ui_main() or 0)
    except Exception as error:
        _show_modern_startup_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
