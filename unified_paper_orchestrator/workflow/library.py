"""Application facade for the generic workflow library and linear editor.

This module owns no Qt objects and imports no legacy runtime. It composes the
portable contracts, immutable ledger, content-addressed Prompt resources and
strict transfer format behind explicit user operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import (
    ResourceBinding,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from .linear_editor import LinearWorkflowEditor
from .linear_editor import LinearWorkflowEditError
from .packs.base import WorkflowPack
from .resources import (
    USER_PROMPT_RESOURCE_PREFIX,
    ContentAddressedPromptStore,
    PromptResourceRecord,
)
from .store import WorkflowConflictError, WorkflowStore
from .transfer import (
    ImportedWorkflowDraft,
    export_workflow_bundle,
    import_workflow_bundle,
)


@dataclass(frozen=True)
class WorkflowLibraryEntry:
    workflow_id: str
    name: str
    description: str
    version_count: int
    latest_version_id: str
    latest_version: int
    latest_status: str
    published_version_id: str
    is_builtin: bool
    is_readonly: bool
    cloneable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "version_count": self.version_count,
            "latest_version_id": self.latest_version_id,
            "latest_version": self.latest_version,
            "latest_status": self.latest_status,
            "published_version_id": self.published_version_id,
            "is_builtin": self.is_builtin,
            "is_readonly": self.is_readonly,
            "cloneable": self.cloneable,
        }


@dataclass(frozen=True)
class WorkflowImportResult:
    version_record: Mapping[str, Any]
    prompt_resources: tuple[PromptResourceRecord, ...]
    draft: ImportedWorkflowDraft


class WorkflowLibrary:
    """Explicit, side-effect-free-until-called generic workflow facade."""

    def __init__(
        self,
        store: WorkflowStore,
        prompt_store: ContentAddressedPromptStore,
    ) -> None:
        if not isinstance(store, WorkflowStore):
            raise TypeError("store must be a WorkflowStore")
        if not isinstance(prompt_store, ContentAddressedPromptStore):
            raise TypeError("prompt_store must be a ContentAddressedPromptStore")
        self.store = store
        self.prompt_store = prompt_store

    def initialize(self) -> None:
        self.store.initialize()
        self.prompt_store.initialize()

    def install_pack(self, pack: WorkflowPack) -> tuple[dict[str, object], ...]:
        if not isinstance(pack, WorkflowPack):
            raise TypeError("pack must be a WorkflowPack")
        return pack.register(self.store)

    def list_workflows(self) -> tuple[WorkflowLibraryEntry, ...]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.store.list_versions():
            version: WorkflowVersion = record["version"]
            grouped.setdefault(version.workflow_id, []).append(record)
        entries: list[WorkflowLibraryEntry] = []
        for workflow_id, records in grouped.items():
            ordered = sorted(records, key=lambda item: item["version"].version)
            latest = ordered[-1]
            latest_version: WorkflowVersion = latest["version"]
            published = [
                item["version"]
                for item in ordered
                if item["version"].status is WorkflowVersionStatus.PUBLISHED
            ]
            entries.append(
                WorkflowLibraryEntry(
                    workflow_id=workflow_id,
                    name=latest_version.definition.name,
                    description=latest_version.definition.description,
                    version_count=len(ordered),
                    latest_version_id=latest_version.version_id,
                    latest_version=latest_version.version,
                    latest_status=latest_version.status.value,
                    published_version_id=published[-1].version_id if published else "",
                    is_builtin=bool(latest["is_builtin"]),
                    is_readonly=bool(latest["is_readonly"]),
                    cloneable=latest_version.definition.metadata.get("cloneable") is True,
                )
            )
        return tuple(sorted(entries, key=lambda item: (not item.is_builtin, item.name.casefold(), item.workflow_id)))

    def open_editor(self, version_id: str) -> LinearWorkflowEditor:
        record = self.store.get_version(version_id)
        return LinearWorkflowEditor.from_version(
            record["version"],
            is_builtin=bool(record["is_builtin"]),
            is_readonly=bool(record["is_readonly"]),
        )

    def clone_version(
        self,
        source_version_id: str,
        *,
        workflow_id: str,
        name: str,
        description: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        source = self.store.get_version(source_version_id)
        if (
            bool(source["is_builtin"]) or bool(source["is_readonly"])
        ) and source["version"].definition.metadata.get("cloneable") is not True:
            raise LinearWorkflowEditError("protected workflow version is not explicitly cloneable")
        editor = LinearWorkflowEditor.clone_as_custom(
            source["version"],
            workflow_id=workflow_id,
            name=name,
            description=description,
        )
        return self.store.append_custom_version(editor.definition, now=now)

    def save_editor(
        self,
        editor: LinearWorkflowEditor,
        *,
        expected_parent_version_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(editor, LinearWorkflowEditor):
            raise TypeError("editor must be a LinearWorkflowEditor")
        if editor.source_version_id != expected_parent_version_id:
            raise WorkflowConflictError("editor source version does not match the expected parent version")
        return self.store.append_custom_version(
            editor.definition,
            expected_parent_version_id=expected_parent_version_id,
            now=now,
        )

    def publish(self, version_id: str) -> dict[str, Any]:
        return self.store.publish_custom_version(version_id)

    def edit_prompt(
        self,
        editor: LinearWorkflowEditor,
        node_id: str,
        text: str,
        *,
        version: str = "",
    ) -> tuple[LinearWorkflowEditor, PromptResourceRecord]:
        if not isinstance(editor, LinearWorkflowEditor):
            raise TypeError("editor must be a LinearWorkflowEditor")
        resource = self.prompt_store.put(text, version=version)
        return editor.update_node(node_id, prompt_ref=resource.prompt_ref), resource

    def export_version(
        self,
        version_id: str,
        *,
        prompt_text_by_resource_id: Mapping[str, str],
    ) -> str:
        return export_workflow_bundle(
            self.store.get_version(version_id)["version"],
            prompt_text_by_resource_id=prompt_text_by_resource_id,
        )

    def import_bundle(
        self,
        payload: str | bytes | Mapping[str, Any],
        *,
        workflow_id: str,
        name: str | None = None,
        description: str | None = None,
        now: datetime | None = None,
    ) -> WorkflowImportResult:
        draft = import_workflow_bundle(
            payload,
            workflow_id=workflow_id,
            name=name,
            description=description,
        )
        resources = draft.publish_resources(self.prompt_store)
        version_record = self.store.append_custom_version(draft.definition, now=now)
        return WorkflowImportResult(version_record, resources, draft)

    def runtime_binding(
        self,
        version_id: str,
        *,
        base_resources: Sequence[ResourceBinding] = (),
    ) -> WorkflowRuntimeBinding:
        record = self.store.get_version(version_id)
        version: WorkflowVersion = record["version"]
        referenced = {
            node.prompt_ref.resource_id: node.prompt_ref
            for node in version.definition.nodes
            if node.prompt_ref is not None
        }
        base_resource_values = tuple(base_resources)
        supplied = {item.resource_id: item for item in base_resource_values}
        if len(supplied) != len(base_resource_values):
            raise ValueError("base_resources contains duplicate resource IDs")
        bindings: list[ResourceBinding] = []
        for resource_id, prompt_ref in sorted(referenced.items()):
            if resource_id.startswith(USER_PROMPT_RESOURCE_PREFIX):
                bindings.append(self.prompt_store.record(prompt_ref.content_sha256).runtime_binding)
                continue
            binding = supplied.get(resource_id)
            if binding is None or binding.content_sha256.casefold() != prompt_ref.content_sha256.casefold():
                raise ValueError(f"runtime resource binding is missing or mismatched: {resource_id}")
            bindings.append(binding)
        binding = WorkflowRuntimeBinding(
            definition_sha256=version.definition_sha256,
            resources=tuple(bindings),
            binding_id=f"{version.workflow_id}.library.{version.definition_sha256[:12]}",
            metadata={"source": "workflow-library"},
        )
        binding.validate(version.definition)
        return binding

    def create_run(
        self,
        version_id: str,
        binding: WorkflowRuntimeBinding,
        *,
        inputs: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        owner: str = "generic-library",
        idempotency_key: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        record = self.store.get_version(version_id)
        return self.store.create_run(
            record["version"],
            binding,
            inputs=inputs,
            metadata=metadata,
            owner=owner,
            idempotency_key=idempotency_key,
            is_builtin=bool(record["is_builtin"]),
            is_readonly=bool(record["is_readonly"]),
            now=now,
        )


__all__ = [
    "WorkflowImportResult",
    "WorkflowLibrary",
    "WorkflowLibraryEntry",
]
