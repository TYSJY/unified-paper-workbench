from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unified_paper_orchestrator.workflow.contracts import (
    NodeDefinition,
    NodeType,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.store import (
    WorkflowConflictError,
    WorkflowStore,
    workflow_database_path,
)
from unified_paper_orchestrator.workflow.packs.base import WorkflowPack


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-08-05T12:00:00+00:00"


def _definition(workflow_id: str = "custom.library-test", *, name: str = "Library test") -> WorkflowDefinition:
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        name=name,
        nodes=(
            NodeDefinition(
                node_id="approve",
                name="Approve",
                node_type=NodeType.HUMAN,
                display_order=10,
            ),
        ),
        edges=(),
        metadata={"builtin": False, "readonly": False, "cloneable": True},
    )
    definition.validate()
    return definition


def _store(tmp_path: Path) -> WorkflowStore:
    store = WorkflowStore(workflow_database_path(tmp_path))
    store.initialize()
    return store


def test_append_custom_version_uses_parent_compare_and_swap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append_custom_version(_definition(), now=NOW)
    changed = replace(first["version"].definition, name="Changed")
    second = store.append_custom_version(
        changed,
        expected_parent_version_id=first["version"].version_id,
        now=NOW,
    )

    assert first["version"].version == 1
    assert second["version"].version == 2
    assert first["version"].status is WorkflowVersionStatus.DRAFT
    assert second["version"].status is WorkflowVersionStatus.DRAFT
    assert first["version"].version_id != second["version"].version_id
    with pytest.raises(WorkflowConflictError, match="concurrently"):
        store.append_custom_version(
            replace(changed, name="Stale edit"),
            expected_parent_version_id=first["version"].version_id,
            now=NOW,
        )


def test_concurrent_custom_saves_allow_exactly_one_child(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = store.append_custom_version(_definition(), now=NOW)["version"]

    def save(index: int) -> str:
        try:
            record = store.append_custom_version(
                replace(parent.definition, name=f"Candidate {index}"),
                expected_parent_version_id=parent.version_id,
                now=NOW,
            )
            return record["version"].version_id
        except WorkflowConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(save, range(8)))

    assert outcomes.count("conflict") == 7
    assert len({item for item in outcomes if item != "conflict"}) == 1
    assert [item["version"].version for item in store.list_versions("custom.library-test")] == [1, 2]


def test_publish_latest_custom_draft_deprecates_prior_release(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append_custom_version(_definition(), now=NOW)["version"]
    published_first = store.publish_custom_version(first.version_id)["version"]
    second = store.append_custom_version(
        replace(first.definition, name="Second"),
        expected_parent_version_id=first.version_id,
        now=NOW,
    )["version"]
    published_second = store.publish_custom_version(second.version_id)["version"]
    versions = [item["version"] for item in store.list_versions(first.workflow_id)]

    assert published_first.status is WorkflowVersionStatus.PUBLISHED
    assert published_second.status is WorkflowVersionStatus.PUBLISHED
    assert [item.status for item in versions] == [
        WorkflowVersionStatus.DEPRECATED,
        WorkflowVersionStatus.PUBLISHED,
    ]
    with pytest.raises(WorkflowConflictError, match="only a custom draft"):
        store.publish_custom_version(second.version_id)


def test_custom_api_rejects_protected_and_legacy_definitions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    protected = replace(_definition(), metadata={"builtin": True, "readonly": True})
    legacy = replace(
        _definition("custom.legacy"),
        nodes=(NodeDefinition("legacy", "Legacy", NodeType.LEGACY_STAGE, 10, implementation_ref="legacy"),),
    )

    with pytest.raises(ValueError, match="built-in or read-only"):
        store.append_custom_version(protected, now=NOW)
    with pytest.raises(ValueError, match="built-in or read-only"):
        store.append_custom_version(replace(_definition(), metadata={"builtin": 1}), now=NOW)
    with pytest.raises(ValueError, match="legacy-stage"):
        store.append_custom_version(legacy, now=NOW)

    builtin = WorkflowVersion.from_definition(
        replace(_definition("builtin.test"), metadata={"builtin": True, "readonly": True}),
        version=1,
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.PUBLISHED,
    )
    store.register_version(builtin, is_builtin=True, is_readonly=True)
    with pytest.raises(WorkflowConflictError, match="built-in/read-only"):
        store.publish_custom_version(builtin.version_id)


def test_low_level_registration_cannot_disguise_protected_or_legacy_custom_drafts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    protected = WorkflowVersion.from_definition(
        replace(_definition("custom.disguised"), metadata={"builtin": True, "readonly": True}),
        version=1,
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.DRAFT,
    )
    legacy = WorkflowVersion.from_definition(
        replace(
            _definition("custom.legacy-registration"),
            nodes=(NodeDefinition("legacy", "Legacy", NodeType.LEGACY_STAGE, 10, implementation_ref="legacy"),),
        ),
        version=1,
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.DRAFT,
    )

    with pytest.raises(ValueError, match="metadata.builtin"):
        store.register_version(protected)
    with pytest.raises(ValueError, match="legacy-stage"):
        store.register_version(legacy)


def test_builtin_pack_registration_keeps_exactly_one_published_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    definition = replace(
        _definition("builtin.pack-upgrade"),
        metadata={"builtin": True, "readonly": True, "cloneable": True},
    )
    first = WorkflowVersion.from_definition(
        definition,
        version=1,
        version_id="builtin.pack-upgrade.v1",
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.PUBLISHED,
    )
    second = WorkflowVersion.from_definition(
        replace(definition, name="Pack upgrade v2"),
        version=2,
        version_id="builtin.pack-upgrade.v2",
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.PUBLISHED,
    )

    store.register_version(first, is_builtin=True, is_readonly=True)
    store.register_version(second, is_builtin=True, is_readonly=True)
    store.register_version(second, is_builtin=True, is_readonly=True)

    assert [item["version"].status for item in store.list_versions(definition.workflow_id)] == [
        WorkflowVersionStatus.DEPRECATED,
        WorkflowVersionStatus.PUBLISHED,
    ]


def test_register_version_rejects_direct_custom_published_insertion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    published = WorkflowVersion.from_definition(
        _definition("custom.direct-published"),
        version=1,
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.PUBLISHED,
    )

    with pytest.raises(ValueError, match="custom draft publication API"):
        store.register_version(published)
    readonly_only = WorkflowVersion.from_definition(
        replace(
            _definition("custom.readonly-only"),
            metadata={"builtin": False, "readonly": True},
        ),
        version=1,
        created_at=NOW_TEXT,
        status=WorkflowVersionStatus.PUBLISHED,
    )
    with pytest.raises(ValueError, match="enabled together"):
        store.register_version(readonly_only, is_readonly=True)


def test_builtin_pack_registration_is_atomic_and_cannot_mix_custom_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    custom = store.append_custom_version(_definition("shared.workflow-id"), now=NOW)["version"]

    def builtin_version(workflow_id: str, version_id: str) -> WorkflowVersion:
        definition = replace(
            _definition(workflow_id),
            metadata={"builtin": True, "readonly": True, "cloneable": True},
        )
        return WorkflowVersion.from_definition(
            definition,
            version=2 if workflow_id == custom.workflow_id else 1,
            version_id=version_id,
            created_at=NOW_TEXT,
            status=WorkflowVersionStatus.PUBLISHED,
        )

    first = builtin_version("builtin.atomic-first", "builtin.atomic-first.v1")
    conflicting = builtin_version(custom.workflow_id, "shared.workflow-id.builtin.v2")
    pack = WorkflowPack("builtin.atomic-test", 1, "Atomic test", (first, conflicting))

    with pytest.raises(WorkflowConflictError, match="cannot mix custom and protected"):
        pack.register(store)

    with pytest.raises(KeyError):
        store.get_version(first.version_id)
    assert [item["version"].version_id for item in store.list_versions(custom.workflow_id)] == [custom.version_id]


def test_read_run_snapshot_is_complete_and_does_not_expose_fencing_tokens(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft = store.append_custom_version(_definition(), now=NOW)["version"]
    version = store.publish_custom_version(draft.version_id)["version"]
    binding = WorkflowRuntimeBinding(
        definition_sha256=version.definition_sha256,
        resources=(),
        binding_id="custom.library-test.local",
    )
    run = store.create_run(version, binding, now=NOW)

    snapshot = store.read_run_snapshot(run["run_id"])

    assert snapshot["run"]["run_id"] == run["run_id"]
    assert snapshot["version"]["version"].version_id == version.version_id
    assert [item["node_id"] for item in snapshot["node_attempts"]] == ["approve"]
    assert [item["event_type"] for item in snapshot["events"]] == ["run.created", "node.created"]
    assert snapshot["artifacts"] == []
    assert snapshot["sessions"] == []
    assert "fencing" not in repr(snapshot).casefold()
