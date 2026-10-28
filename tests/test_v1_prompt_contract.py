from __future__ import annotations

import hashlib
import json
from pathlib import Path

from unified_paper_orchestrator.config import LEGACY_PROJECTS
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator


CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "v1_internal_prompts.json").read_text(encoding="utf-8")
)
WRITING_SCHEDULER_PROMPT_SHA256 = "AAB0DDF40C0698FF4EEA7F84A61DAC19DAD2833B04B66E1F8021CE59FCC207A6"


def test_all_user_tuned_internal_prompts_are_byte_identical() -> None:
    total = 0
    for stage_name, stage in CONTRACT["stages"].items():
        root = Path(stage["root"])
        expected_files = stage["files"]
        assert root.is_dir(), f"missing {stage_name} prompt directory: {root}"
        actual_names = {path.name for path in root.glob("*.md")}
        assert actual_names == set(expected_files), f"{stage_name} prompt filename set changed"
        assert len(expected_files) == stage["file_count"]
        for name, expected_sha256 in expected_files.items():
            path = root / name
            actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            assert actual_sha256 == expected_sha256, f"user-tuned prompt changed: {path}"
            total += 1
    assert total == 20


def test_v1_prompt_inventory_is_bound_to_the_protected_runtime_directories() -> None:
    orchestrator = UnifiedOrchestrator()
    for stage_name, stage in CONTRACT["stages"].items():
        protected_root = Path(stage["root"]).resolve()
        configured_root = (Path(LEGACY_PROJECTS[stage_name]["root"]) / "prompts").resolve()
        assert configured_root == protected_root

        # The two figure files named “副本” are protected user assets, while
        # the existing V1 inventory intentionally hides copy/backup names.
        expected_inventory = {
            name
            for name in stage["files"]
            if not any(marker in name.lower() for marker in ("副本", "copy", "backup", ".bak", "~"))
        }
        runtime_inventory = {path.name for path in orchestrator._task_prompt_files(stage_name)}
        assert runtime_inventory == expected_inventory

        # Verify that the V1 read boundary resolves to these exact protected
        # files rather than to a parallel replacement prompt directory.
        for name in runtime_inventory:
            result = orchestrator.read_prompt_file(stage_name, str(protected_root / name))
            assert Path(result["path"]).resolve() == (protected_root / name).resolve()
            assert hashlib.sha256(result["text"].encode("utf-8")).hexdigest().upper() == stage["files"][name]


def test_writing_scheduler_prompt_is_byte_identical() -> None:
    prompt_path = Path(LEGACY_PROJECTS["writing"]["prompt_path"])
    assert prompt_path.is_file(), f"missing protected writing scheduler prompt: {prompt_path}"
    assert hashlib.sha256(prompt_path.read_bytes()).hexdigest().upper() == WRITING_SCHEDULER_PROMPT_SHA256
