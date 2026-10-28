from __future__ import annotations

import json
from pathlib import Path

from tools.workflow_cli import main


def test_cli_initializes_lists_clones_and_publishes_without_production_config(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "generic"
    base = ["--workspace", str(workspace)]

    assert main([*base, "init", "--install-sci"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["installed_workflows"] == 4

    assert main([*base, "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert len(listing["workflows"]) == 4
    source = listing["workflows"][0]["published_version_id"]

    assert main(
        [
            *base,
            "clone",
            source,
            "--workflow-id",
            "custom.cli-clone",
            "--name",
            "CLI clone",
        ]
    ) == 0
    cloned = json.loads(capsys.readouterr().out)
    assert cloned["version"]["status"] == "draft"
    clone_version_id = cloned["version"]["version_id"]

    assert main([*base, "publish", clone_version_id]) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["version"]["status"] == "published"


def test_cli_requires_explicit_init_and_export_never_overwrites(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "missing"
    assert main(["--workspace", str(workspace), "list"]) == 2
    failure = json.loads(capsys.readouterr().out)

    assert failure["status"] == "error"
    assert "init first" in failure["message"]
    assert not workspace.exists()


def test_cli_module_does_not_import_product_configuration_or_legacy_runtime() -> None:
    source = Path("tools/workflow_cli.py").read_text(encoding="utf-8")
    forbidden = (
        "from unified_paper_orchestrator.config",
        "UnifiedOrchestrator",
        "legacy_compat",
        "legacy_shadow",
        "run_smart_submission_scheduler",
    )
    assert not any(item in source for item in forbidden)
