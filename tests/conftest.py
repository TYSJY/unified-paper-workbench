from __future__ import annotations

import os


# Tests must never inherit the operator's currently selected production
# workspace. Individual tests can still opt into a managed workspace by
# setting UNIFIED_PAPER_WORKSPACE explicitly before importing the application.
os.environ.setdefault("UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY", "1")
