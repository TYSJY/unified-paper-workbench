from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.library import WorkflowLibrary
from unified_paper_orchestrator.workflow.linear_editor import LinearWorkflowEditError
from unified_paper_orchestrator.workflow.packs import (
    SCI_WRITING_WORKFLOW_ID,
    build_sci_workflow_pack,
)
from unified_paper_orchestrator.workflow.resources import (
    ContentAddressedPromptStore,
    prompt_resource_directory,
)
from unified_paper_orchestrator.workflow.store import (
    WorkflowConflictError,
    WorkflowStore,
    workflow_database_path,
)
from unified_paper_orchestrator.workflow.transfer import export_workflow_bundle


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _library(tmp_path: Path) -> WorkflowLibrary:
    library = WorkflowLibrary(
        WorkflowStore(workflow_database_path(tmp_path)),
        ContentAddressedPromptStore(prompt_resource_directory(tmp_path)),
    )
    library.initialize()
    return library


def test_library_installs_and_lists_readonly_sci_templates(tmp_path: Path) -> None:
    library = _library(tmp_path)
    installed = library.install_pack(build_sci_workflow_pack())
    entries = library.list_workflows()

    assert len(installed) == 4
    assert len(entries) == 4
    assert all(item.is_builtin and item.is_readonly and item.cloneable for item in entries)
    assert {item.latest_status for item in entries} == {"published"}


def test_builtin_must_be_cloned_and_custom_history_is_copy_on_write(tmp_path: Path) -> None:
    library = _library(tmp_path)
    library.install_pack(build_sci_workflow_pack())
    source = next(
        item for item in library.store.list_versions(SCI_WRITING_WORKFLOW_ID)
        if item["version"].workflow_id == SCI_WRITING_WORKFLOW_ID
    )["version"]
    source_digest = source.definition_sha256

    with pytest.raises(LinearWorkflowEditError, match="cloned before editing"):
        library.open_editor(source.version_id)
    clone = library.clone_version(
        source.version_id,
        workflow_id="custom.sci-writing-copy",
        name="My writing flow",
        now=NOW,
    )["version"]
    editor = library.open_editor(clone.version_id).update_interface(name="My edited writing flow")
    saved = library.save_editor(
        editor,
        expected_parent_version_id=clone.version_id,
        now=NOW,
    )["version"]

    assert clone.version == 1
    assert saved.version == 2
    assert saved.definition.name == "My edited writing flow"
    assert library.store.get_version(source.version_id)["version"].definition_sha256 == source_digest


def test_stale_editor_cannot_claim_a_newer_parent_version(tmp_path: Path) -> None:
    library = _library(tmp_path)
    library.install_pack(build_sci_workflow_pack())
    source = library.store.list_versions(SCI_WRITING_WORKFLOW_ID)[0]["version"]
    clone = library.clone_version(
        source.version_id,
        workflow_id="custom.stale-editor",
        name="Stale editor",
        now=NOW,
    )["version"]
    stale = library.open_editor(clone.version_id).update_interface(name="Stale change")
    fresh = library.open_editor(clone.version_id).update_interface(name="Fresh change")
    saved = library.save_editor(
        fresh,
        expected_parent_version_id=clone.version_id,
        now=NOW,
    )["version"]

    with pytest.raises(WorkflowConflictError, match="editor source version"):
        library.save_editor(
            stale,
            expected_parent_version_id=saved.version_id,
            now=NOW,
        )

    assert library.store.list_versions(clone.workflow_id)[-1]["version"].definition.name == "Fresh change"


def test_prompt_edit_creates_new_resource_and_runtime_binding(tmp_path: Path) -> None:
    library = _library(tmp_path)
    library.install_pack(build_sci_workflow_pack())
    source = library.store.list_versions(SCI_WRITING_WORKFLOW_ID)[0]["version"]
    clone = library.clone_version(
        source.version_id,
        workflow_id="custom.prompt-edit",
        name="Prompt edit",
        now=NOW,
    )["version"]
    editor = library.open_editor(clone.version_id)
    original_ref = editor.definition.nodes[0].prompt_ref
    updated, resource = library.edit_prompt(editor, "writing-01", "A brand new user Prompt\n")
    saved = library.save_editor(
        updated,
        expected_parent_version_id=clone.version_id,
        now=NOW,
    )["version"]
    assert original_ref is not None
    assert saved.definition.nodes[0].prompt_ref == resource.prompt_ref
    assert saved.definition.nodes[0].prompt_ref != original_ref
    assert Path(resource.absolute_path).read_text(encoding="utf-8") == "A brand new user Prompt\n"
    # A fully edited single-node clone would need no sealed base resources; this
    # 10-node clone deliberately proves missing untouched seals fail closed.
    with pytest.raises(ValueError, match="missing or mismatched"):
        library.runtime_binding(saved.version_id, base_resources=())


def test_import_is_persisted_only_as_custom_draft(tmp_path: Path) -> None:
    prompt = "Import me\n"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    from unified_paper_orchestrator.workflow.contracts import (
        NodeDefinition,
        NodeType,
        PromptRef,
        WorkflowDefinition,
        WorkflowVersion,
    )

    definition = WorkflowDefinition(
        "custom.source",
        "Source",
        (NodeDefinition("compose", "Compose", NodeType.CODEX, 10, prompt_ref=PromptRef("prompt.source", digest, "1")),),
        (),
    )
    source = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at="2026-08-05T12:00:00+00:00",
    )
    bundle = export_workflow_bundle(source, prompt_text_by_resource_id={"prompt.source": prompt})
    target = _library(tmp_path / "target")

    imported = target.import_bundle(
        bundle,
        workflow_id="custom.imported-through-library",
        now=NOW,
    )

    version = imported.version_record["version"]
    assert version.status.value == "draft"
    assert imported.version_record["is_builtin"] is False
    assert imported.version_record["is_readonly"] is False
    assert imported.prompt_resources[0].content_sha256 == digest
    assert target.prompt_store.read_text(version.definition.nodes[0].prompt_ref) == prompt


def test_store_protection_flag_cannot_make_noncloneable_version_cloneable(tmp_path: Path) -> None:
    library = _library(tmp_path)
    from unified_paper_orchestrator.workflow.contracts import (
        NodeDefinition,
        NodeType,
        WorkflowDefinition,
        WorkflowVersion,
        WorkflowVersionStatus,
    )

    definition = WorkflowDefinition(
        "builtin.noncloneable",
        "Noncloneable",
        (NodeDefinition("review", "Review", NodeType.HUMAN, 10),),
        (),
        metadata={"builtin": True, "readonly": True},
    )
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at="2026-08-05T12:00:00+00:00",
        status=WorkflowVersionStatus.PUBLISHED,
    )
    library.store.register_version(version, is_builtin=True, is_readonly=True)

    with pytest.raises(LinearWorkflowEditError, match="not explicitly cloneable"):
        library.clone_version(
            version.version_id,
            workflow_id="custom.should-not-clone",
            name="No",
            now=NOW,
        )


def test_new_library_module_has_no_legacy_or_production_runtime_imports() -> None:
    source = Path("unified_paper_orchestrator/workflow/library.py").read_text(encoding="utf-8")
    forbidden = (
        "UnifiedOrchestrator",
        "legacy_compat",
        "legacy_shadow",
        "full_pipeline",
        "run_smart_submission_scheduler",
        "WorkflowScheduler",
    )
    assert not any(item in source for item in forbidden)
