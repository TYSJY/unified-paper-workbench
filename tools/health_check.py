from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.doctor import run_doctor  # noqa: E402


if __name__ == "__main__":
    result = run_doctor()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("status") in {"PASS", "PASS_WITH_WARNINGS"} else 1)
