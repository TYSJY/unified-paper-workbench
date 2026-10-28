from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unified_paper_orchestrator.full_pipeline import get_full_pipeline_runtime
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator


sys.stdout.reconfigure(encoding="utf-8", errors="replace")

orchestrator = UnifiedOrchestrator()
runtime = get_full_pipeline_runtime(orchestrator)
print(
    "COORDINATOR_RESUME",
    json.dumps(runtime.coordinator.resume(), ensure_ascii=False),
    flush=True,
)
status = runtime.coordinator.status()
print(
    "WORKERS",
    json.dumps(
        runtime.coordinator.resume_active_stage_workers(status),
        ensure_ascii=False,
    ),
    flush=True,
)
print(
    "POLLER",
    json.dumps(runtime.start_background(), ensure_ascii=False),
    flush=True,
)

last = ""
while True:
    snapshot = runtime.status()
    compact = {
        "at": time.strftime("%H:%M:%S"),
        "status": snapshot.get("status"),
        "active": snapshot.get("active"),
        "progress": snapshot.get("progress_percent"),
        "papers": [
            {
                "slot": item.get("slot"),
                "paper_id": item.get("paper_id"),
                "phase": item.get("phase"),
                "status": item.get("status"),
                "progress": item.get("progress_percent"),
                "detail": str(item.get("detail") or "")[:360],
            }
            for item in snapshot.get("papers", [])
        ],
    }
    rendered = json.dumps(compact, ensure_ascii=False)
    if rendered != last:
        print("PIPELINE", rendered, flush=True)
        last = rendered
    if snapshot.get("status") == "completed":
        break
    if snapshot.get("active") and snapshot.get("status") != "paused":
        runtime.start_background()
    time.sleep(5)
