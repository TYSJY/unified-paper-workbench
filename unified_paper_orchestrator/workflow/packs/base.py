from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import WorkflowVersion, WorkflowVersionStatus, canonical_json
from ..store import WorkflowStore


@dataclass(frozen=True)
class WorkflowPack:
    """A portable, versioned collection of built-in workflow templates."""

    pack_id: str
    version: int
    name: str
    workflows: tuple[WorkflowVersion, ...]
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pack_id.strip() or not self.name.strip():
            raise ValueError("workflow pack ID and name are required")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("workflow pack version must be a positive integer")
        versions = tuple(self.workflows)
        if not versions:
            raise ValueError("workflow pack must contain at least one workflow")
        if any(not isinstance(item, WorkflowVersion) for item in versions):
            raise TypeError("workflow pack entries must be WorkflowVersion values")
        workflow_ids = [item.workflow_id for item in versions]
        version_ids = [item.version_id for item in versions]
        if len(workflow_ids) != len(set(workflow_ids)) or len(version_ids) != len(set(version_ids)):
            raise ValueError("workflow pack contains duplicate workflow or version IDs")
        if any(item.status is not WorkflowVersionStatus.PUBLISHED for item in versions):
            raise ValueError("built-in workflow pack entries must be published")
        detached = dict(self.metadata)
        canonical_json(detached)
        object.__setattr__(self, "workflows", versions)
        object.__setattr__(self, "metadata", MappingProxyType(detached))

    def workflow(self, workflow_id: str) -> WorkflowVersion:
        matches = [item for item in self.workflows if item.workflow_id == workflow_id]
        if len(matches) != 1:
            raise KeyError(workflow_id)
        return matches[0]

    def register(self, store: WorkflowStore) -> tuple[dict[str, object], ...]:
        if not isinstance(store, WorkflowStore):
            raise TypeError("store must be a WorkflowStore")
        return store.register_builtin_versions(self.workflows)


__all__ = ["WorkflowPack"]
