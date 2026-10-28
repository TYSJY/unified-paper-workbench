from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.packs import build_sci_workflow_pack, sci_prompt_seal_manifest
from unified_paper_orchestrator.workflow.resources import (
    ContentAddressedPromptStore,
    PromptResourceError,
    prompt_resource_directory,
)


def _store(tmp_path: Path) -> ContentAddressedPromptStore:
    store = ContentAddressedPromptStore(prompt_resource_directory(tmp_path))
    store.initialize()
    return store


def test_construction_is_side_effect_free_and_root_is_canonical(tmp_path: Path) -> None:
    workspace = tmp_path / "not-created"
    root = prompt_resource_directory(workspace)
    store = ContentAddressedPromptStore(root)

    assert not workspace.exists()
    with pytest.raises(PromptResourceError, match="not been initialized"):
        store.put("A valid prompt")
    assert not workspace.exists()
    with pytest.raises(ValueError, match="state/v2/resources/prompts"):
        ContentAddressedPromptStore(tmp_path / "prompts")


def test_prompt_blobs_are_content_addressed_idempotent_and_never_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.put("Exact prompt bytes\n", version="1")
    repeated = store.put("Exact prompt bytes\n", version="2")
    changed = store.put("Edited prompt bytes\n")

    assert first.content_sha256 == hashlib.sha256(b"Exact prompt bytes\n").hexdigest()
    assert repeated.content_sha256 == first.content_sha256
    assert repeated.absolute_path == first.absolute_path
    assert repeated.version == "2"
    assert changed.content_sha256 != first.content_sha256
    assert Path(first.absolute_path).read_bytes() == b"Exact prompt bytes\n"
    assert store.read_text(first.prompt_ref) == "Exact prompt bytes\n"
    assert first.runtime_binding.resource_id == first.prompt_ref.resource_id


def test_tampered_blob_is_rejected_instead_of_repaired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.put("Immutable prompt")
    path = Path(record.absolute_path)
    path.write_text("tampered", encoding="utf-8")

    with pytest.raises(PromptResourceError, match="immutable digest"):
        store.put("Immutable prompt")
    assert path.read_text(encoding="utf-8") == "tampered"


def test_user_prompt_edit_does_not_change_any_sealed_sci_prompt(tmp_path: Path) -> None:
    pack = build_sci_workflow_pack()
    manifest_before = {
        stage: {name: dict(item) for name, item in entries.items()}
        for stage, entries in sci_prompt_seal_manifest().items()
    }
    definition_digests = {version.workflow_id: version.definition_sha256 for version in pack.workflows}
    store = _store(tmp_path)

    edited = store.put("User-owned replacement prompt\n")

    assert edited.prompt_ref.resource_id.startswith("user.prompt.sha256.")
    assert manifest_before == {
        stage: {name: dict(item) for name, item in entries.items()}
        for stage, entries in sci_prompt_seal_manifest().items()
    }
    assert definition_digests == {
        version.workflow_id: version.definition_sha256
        for version in build_sci_workflow_pack().workflows
    }


def test_symlinked_resource_root_is_rejected(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    linked_workspace = tmp_path / "linked"
    real_workspace.mkdir()
    try:
        linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable for this account")

    store = ContentAddressedPromptStore(prompt_resource_directory(linked_workspace))
    with pytest.raises(PromptResourceError, match="symlink or reparse"):
        store.initialize()
    assert not (real_workspace / "state").exists()
