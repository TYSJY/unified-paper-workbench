"""Read the app's model catalog without starting any paper task or model turn."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from unified_paper_orchestrator.ui_next.sdk_models import discover_models, validate_profile
from unified_paper_orchestrator.writing_sdk_runtime import WritingSDKRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="upo-catalog-probe-") as temporary:
        root = Path(temporary)
        runtime = WritingSDKRuntime(project={"root": root, "papers_dir": root / "papers",
            "state_path": root / "pipeline.json", "prompt_path": root / "unused-prompt.txt"},
            codex_home=args.codex_home, journal_path=root / "journal.json",
            dispatch_lock_path=root / "dispatch.lock", pipeline_lock_path=root / "pipeline.lock")
        source = SimpleNamespace(stage_available_models=lambda stage: runtime.list_models())
        result = discover_models(source, "writing")
        assert result["state"] == "ready", "Catalog freshness was not verified"
        assert any(item["model"] == "gpt-6-astra" for item in result["models"])
        validate_profile(source, "writing", "gpt-6-astra", "max")
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
