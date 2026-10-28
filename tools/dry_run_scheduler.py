from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator  # noqa: E402


if __name__ == "__main__":
    result = UnifiedOrchestrator().advance_once(apply=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
