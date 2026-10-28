from __future__ import annotations

import hashlib
import json
from pathlib import Path

from unified_paper_orchestrator.workspace import (
    WORKSPACE_ENV,
    WorkspaceDescriptor,
    active_workspace,
    create_workspace,
    extract_workspace_argument,
    list_registered_workspaces,
    load_workspace,
    set_active_workspace,
)


STAGES = ("writing", "figures", "journal_author", "submission")
STATE_FILES = {
    "writing": "pipeline_state.json",
    "figures": "figure_pipeline_state.json",
    "journal_author": "journal_author_state.json",
    "submission": "auto_submission_panel_state.json",
}
SCHEDULER_PROMPTS = {
    "writing": "SCHEDULER_PROMPT.md",
    "figures": "FIGURE_SCHEDULER_PROMPT.md",
    "journal_author": "JOURNAL_SCHEDULER_PROMPT.md",
    "submission": "AUTO_SUBMISSION_SCHEDULER_PROMPT.md",
}


def _source_workspace(tmp_path: Path) -> WorkspaceDescriptor:
    root = tmp_path / "legacy"
    state = root / "state"
    state.mkdir(parents=True)
    stage_roots: dict[str, Path] = {}
    for stage in STAGES:
        stage_root = root / stage
        stage_roots[stage] = stage_root
        (stage_root / "prompts").mkdir(parents=True)
        (stage_root / "controller").mkdir(parents=True)
        (stage_root / "papers" / "paper-1").mkdir(parents=True)
        (stage_root / "prompts" / f"{stage}.md").write_bytes(f"prompt:{stage}\r\n".encode())
        (stage_root / "controller" / SCHEDULER_PROMPTS[stage]).write_bytes(
            f"scheduler:{stage}\r\n".encode()
        )
        (stage_root / "controller" / "runtime_helper.py").write_text(
            f"STAGE = {stage!r}\n",
            encoding="utf-8",
        )
        (stage_root / "controller" / STATE_FILES[stage]).write_text(
            json.dumps(
                {
                    "version": 1,
                    "settings": {
                        "scheduling_mode": "auto",
                        "source_root": str(stage_root),
                        "source_root_case_variant": str(stage_root).swapcase(),
                    },
                    "sessions": {},
                    "papers": {},
                    "paper_queue": [],
                    "commands": [],
                }
            ),
            encoding="utf-8",
        )
        (stage_root / "papers" / "paper-1" / "artifact.txt").write_text(
            str(stage_root),
            encoding="utf-8",
        )
    (stage_roots["submission"] / ".chrome-test").mkdir()
    (stage_roots["submission"] / ".chrome-test" / "Cookies").write_bytes(b"secret")
    (stage_roots["submission"] / ".git").mkdir()
    (stage_roots["submission"] / ".git" / "objects").write_bytes(b"objects")
    (state / "unified_state.json").write_text(
        json.dumps({"papers": {}, "source": str(stage_roots["writing"])}),
        encoding="utf-8",
    )
    return WorkspaceDescriptor(
        workspace_id="legacy-test",
        name="Legacy Test",
        root=root,
        state_dir=state,
        stage_roots=stage_roots,
        mode="legacy_bridge",
    )


def test_workspace_argument_can_launch_gui_without_becoming_a_cli_command() -> None:
    selected, remaining = extract_workspace_argument(["--workspace", r"D:\PaperProjects\Main"])
    assert selected == r"D:\PaperProjects\Main"
    assert remaining == []


def test_create_empty_workspace_preserves_prompt_bytes_and_starts_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("UNIFIED_PAPER_WORKSPACE_REGISTRY", str(tmp_path / "registry.json"))
    source = _source_workspace(tmp_path)
    target = tmp_path / "managed-empty"

    descriptor = create_workspace(target, "Empty", source=source, import_data=False)

    assert load_workspace(target).workspace_id == descriptor.workspace_id
    for stage in STAGES:
        source_prompt = source.stage_roots[stage] / "prompts" / f"{stage}.md"
        target_prompt = descriptor.stage_roots[stage] / "prompts" / f"{stage}.md"
        assert target_prompt.read_bytes() == source_prompt.read_bytes()
        assert (
            descriptor.stage_roots[stage] / "controller" / "runtime_helper.py"
        ).read_text(encoding="utf-8") == f"STAGE = {stage!r}\n"
        state = json.loads(
            (
                descriptor.stage_roots[stage]
                / "controller"
                / STATE_FILES[stage]
            ).read_text(encoding="utf-8")
        )
        assert state["settings"]["scheduling_mode"] == "manual"
        assert state.get("paper_queue", []) == []
        assert state.get("papers", {}) == {}
        assert state.get("sessions", {}) == {}

    migration = json.loads(
        (target / "legacy_import" / "migration_manifest.json").read_text(encoding="utf-8")
    )
    assert migration["prompts_byte_identical"] is True
    assert migration["source_directories_untouched"] is True


def test_import_workspace_copies_business_data_filters_runtime_bulk_and_rewrites_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("UNIFIED_PAPER_WORKSPACE_REGISTRY", str(tmp_path / "registry.json"))
    source = _source_workspace(tmp_path)
    target = tmp_path / "managed-import"

    descriptor = create_workspace(
        target,
        "Imported",
        source=source,
        import_data=True,
        make_active=True,
    )

    assert (descriptor.stage_roots["writing"] / "papers" / "paper-1" / "artifact.txt").is_file()
    assert not (descriptor.stage_roots["submission"] / ".chrome-test").exists()
    assert not (descriptor.stage_roots["submission"] / ".git").exists()
    state = json.loads((descriptor.state_dir / "unified_state.json").read_text(encoding="utf-8"))
    assert state["source"] == str(descriptor.stage_roots["writing"])
    stage_state = json.loads(
        (
            descriptor.stage_roots["writing"]
            / "controller"
            / STATE_FILES["writing"]
        ).read_text(encoding="utf-8")
    )
    assert stage_state["settings"]["scheduling_mode"] == "manual"
    assert stage_state["settings"]["source_root"] == str(descriptor.stage_roots["writing"])
    assert stage_state["settings"]["source_root_case_variant"] == str(
        descriptor.stage_roots["writing"]
    )
    assert list_registered_workspaces()[0]["workspace_id"] == descriptor.workspace_id


def test_explicit_workspace_beats_legacy_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("UNIFIED_PAPER_WORKSPACE_REGISTRY", str(tmp_path / "registry.json"))
    source = _source_workspace(tmp_path)
    descriptor = create_workspace(tmp_path / "managed", "Managed", source=source)
    set_active_workspace(descriptor.root)
    monkeypatch.setenv(WORKSPACE_ENV, str(descriptor.root))

    selected = active_workspace(tmp_path / "application")

    assert selected.workspace_id == descriptor.workspace_id
    assert selected.root == descriptor.root
    assert hashlib.sha256(
        (descriptor.stage_roots["writing"] / "prompts" / "writing.md").read_bytes()
    ).digest()
