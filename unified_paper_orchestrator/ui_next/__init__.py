"""Qt Quick presentation shell for the unified paper workbench.

The package is deliberately presentation-only.  Workflow state, scheduling and
side effects remain owned by :mod:`unified_paper_orchestrator`.
"""

from __future__ import annotations

__all__ = ["main"]


def main() -> int:
    from .main import main as run

    return run()
