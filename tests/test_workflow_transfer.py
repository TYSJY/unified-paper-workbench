from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.contracts import (
    NodeDefinition,
    NodeType,
    PromptRef,
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.resources import (
    ContentAddressedPromptStore,
    prompt_resource_directory,
)
from unified_paper_orchestrator.workflow.transfer import (
    WorkflowTransferError,
    export_workflow_bundle,
    import_workflow_bundle,
)


PROMPT = "Portable user prompt\n"
PROMPT_SHA = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()


def _version(*, node_type: NodeType = NodeType.CODEX) -> WorkflowVersion:
    definition = WorkflowDefinition(
        workflow_id="custom.export-source",
        name="Export source",
        nodes=(
            NodeDefinition(
                node_id="compose",
                name="Compose",
                node_type=node_type,
                display_order=10,
                prompt_ref=(PromptRef("custom.prompt.compose", PROMPT_SHA, "v1") if node_type is NodeType.CODEX else None),
                implementation_ref=("legacy.stage" if node_type is NodeType.LEGACY_STAGE else ""),
            ),
        ),
        edges=(),
        metadata={"builtin": False, "readonly": False, "custom_label": "kept"},
    )
    return WorkflowVersion.from_definition(
        definition,
        version=7,
        created_at="2026-08-05T12:00:00+00:00",
        status=WorkflowVersionStatus.PUBLISHED,
    )


def test_export_import_round_trip_is_canonical_and_always_custom_draft(tmp_path: Path) -> None:
    version = _version()
    first = export_workflow_bundle(
        version,
        prompt_text_by_resource_id={"custom.prompt.compose": PROMPT},
    )
    second = export_workflow_bundle(
        version,
        prompt_text_by_resource_id={"custom.prompt.compose": PROMPT},
    )
    imported = import_workflow_bundle(
        first,
        workflow_id="custom.imported",
        name="Imported copy",
    )
    store = ContentAddressedPromptStore(prompt_resource_directory(tmp_path))
    store.initialize()
    records = imported.publish_resources(store)

    assert first == second
    assert "published" not in json.loads(first)
    assert imported.definition.workflow_id == "custom.imported"
    assert imported.definition.name == "Imported copy"
    assert imported.definition.metadata["builtin"] is False
    assert imported.definition.metadata["readonly"] is False
    assert imported.definition.metadata["custom_label"] == "kept"
    assert imported.definition.metadata["imported_from"]["version"] == 7
    prompt_ref = imported.definition.nodes[0].prompt_ref
    assert prompt_ref is not None
    assert prompt_ref.resource_id == f"user.prompt.sha256.{PROMPT_SHA}"
    assert records[0].prompt_ref == prompt_ref
    assert store.read_text(prompt_ref) == PROMPT


def test_import_rejects_unknown_fields_duplicate_keys_and_tampering() -> None:
    bundle = json.loads(
        export_workflow_bundle(
            _version(),
            prompt_text_by_resource_id={"custom.prompt.compose": PROMPT},
        )
    )
    with_unknown = {**bundle, "status": "published"}
    with pytest.raises(WorkflowTransferError, match="missing or unknown"):
        import_workflow_bundle(with_unknown, workflow_id="custom.bad")

    duplicate = '{"schema":"a","schema":"b"}'
    with pytest.raises(WorkflowTransferError, match="duplicate JSON key"):
        import_workflow_bundle(duplicate, workflow_id="custom.bad")

    bundle["resources"][0]["content_utf8"] = "tampered"
    with pytest.raises(WorkflowTransferError, match="bundle digest"):
        import_workflow_bundle(bundle, workflow_id="custom.bad")


def test_import_rejects_legacy_stage_even_with_valid_bundle_digest() -> None:
    version = _version(node_type=NodeType.LEGACY_STAGE)
    payload = export_workflow_bundle(version, prompt_text_by_resource_id={})

    with pytest.raises(WorkflowTransferError, match="legacy-stage"):
        import_workflow_bundle(payload, workflow_id="custom.no-legacy")


def test_import_rejects_absolute_paths_and_never_writes_before_validation(tmp_path: Path) -> None:
    version = _version()
    bundle = json.loads(
        export_workflow_bundle(
            version,
            prompt_text_by_resource_id={"custom.prompt.compose": PROMPT},
        )
    )
    bundle["definition"]["metadata"]["workspace"] = str(tmp_path.absolute())
    core = {key: bundle[key] for key in ("schema", "source", "definition", "resources")}
    from unified_paper_orchestrator.workflow.contracts import canonical_sha256

    bundle["bundle_sha256"] = canonical_sha256(core)
    resource_root = prompt_resource_directory(tmp_path / "workspace")

    with pytest.raises(WorkflowTransferError, match="definition is invalid"):
        import_workflow_bundle(bundle, workflow_id="custom.absolute")
    assert not resource_root.exists()


def test_missing_or_wrong_prompt_content_is_rejected() -> None:
    version = _version()
    with pytest.raises(WorkflowTransferError, match="missing referenced"):
        export_workflow_bundle(version, prompt_text_by_resource_id={})
    with pytest.raises(WorkflowTransferError, match="does not match"):
        export_workflow_bundle(
            replace(version),
            prompt_text_by_resource_id={"custom.prompt.compose": "wrong"},
        )
    nul_prompt = "Portable user prompt\n\x00"
    nul_digest = hashlib.sha256(nul_prompt.encode("utf-8")).hexdigest()
    nul_definition = replace(
        version.definition,
        nodes=(replace(version.definition.nodes[0], prompt_ref=PromptRef("prompt.nul", nul_digest, "1")),),
    )
    nul_version = WorkflowVersion.from_definition(
        nul_definition,
        version=2,
        created_at="2026-08-05T12:00:00+00:00",
    )
    with pytest.raises(WorkflowTransferError, match="empty or too large"):
        export_workflow_bundle(nul_version, prompt_text_by_resource_id={"prompt.nul": nul_prompt})
