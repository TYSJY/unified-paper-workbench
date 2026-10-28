"""Copy-on-write editing for portable linear workflow definitions.

The editor is deliberately detached from the workflow store, Qt, prompt files,
and every production composition root.  It edits immutable contract objects in
memory, validates every candidate, and materializes a fresh draft version only
when the caller explicitly asks for one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from .contracts import (
    ArtifactBinding,
    ArtifactSpec,
    EdgeDefinition,
    EdgeType,
    ExecutionProfile,
    NodeDefinition,
    NodeType,
    PromptRef,
    RetryPolicy,
    SessionPolicy,
    ValidationMode,
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)


class LinearWorkflowEditError(ValueError):
    """An edit is unsafe or does not identify an editable linear draft."""


class _Unset:
    __slots__ = ()


_UNSET = _Unset()
_PROTECTED_METADATA_KEYS = ("builtin", "readonly")


@dataclass(frozen=True)
class LinearWorkflowEditor:
    """One immutable in-memory editing snapshot.

    Callers may open a custom definition or version directly.  A protected
    built-in/read-only definition must first go through :meth:`clone_as_custom`.
    Every mutator returns a new editor and leaves this editor untouched.
    """

    definition: WorkflowDefinition
    is_builtin: bool
    is_readonly: bool
    source_version_id: str = ""
    next_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.definition, WorkflowDefinition):
            raise TypeError("definition must be a WorkflowDefinition")
        if not isinstance(self.source_version_id, str):
            raise TypeError("source_version_id must be a string")
        if isinstance(self.next_version, bool) or not isinstance(self.next_version, int) or self.next_version <= 0:
            raise ValueError("next_version must be a positive integer")
        if not isinstance(self.is_builtin, bool) or not isinstance(self.is_readonly, bool):
            raise TypeError("workflow protection flags must be booleans")
        _ensure_editable(
            self.definition,
            is_builtin=self.is_builtin,
            is_readonly=self.is_readonly,
        )
        detached = _detached_definition(self.definition)
        detached.validate(mode=ValidationMode.LINEAR)
        object.__setattr__(self, "definition", detached)

    @classmethod
    def from_definition(
        cls,
        definition: WorkflowDefinition,
        *,
        is_builtin: bool,
        is_readonly: bool,
    ) -> "LinearWorkflowEditor":
        return cls(
            definition=definition,
            is_builtin=is_builtin,
            is_readonly=is_readonly,
        )

    @classmethod
    def from_version(
        cls,
        version: WorkflowVersion,
        *,
        is_builtin: bool,
        is_readonly: bool,
    ) -> "LinearWorkflowEditor":
        if not isinstance(version, WorkflowVersion):
            raise TypeError("version must be a WorkflowVersion")
        version.validate(mode=ValidationMode.LINEAR)
        return cls(
            definition=version.definition,
            source_version_id=version.version_id,
            next_version=version.version + 1,
            is_builtin=is_builtin,
            is_readonly=is_readonly,
        )

    @classmethod
    def clone_as_custom(
        cls,
        source: WorkflowDefinition | WorkflowVersion,
        *,
        workflow_id: str,
        name: str,
        description: str | None = None,
    ) -> "LinearWorkflowEditor":
        """Clone any explicitly cloneable protected definition as an editable draft."""

        if isinstance(source, WorkflowVersion):
            source.validate(mode=ValidationMode.LINEAR)
            source_definition = source.definition
            source_version_id = source.version_id
            source_version = source.version
        elif isinstance(source, WorkflowDefinition):
            source.validate(mode=ValidationMode.LINEAR)
            source_definition = source
            source_version_id = ""
            source_version = 0
        else:
            raise TypeError("source must be a WorkflowDefinition or WorkflowVersion")

        protected = _definition_is_protected(source_definition)
        if protected and source_definition.metadata.get("cloneable") is not True:
            raise LinearWorkflowEditError("protected workflow definition is not explicitly cloneable")

        metadata = dict(source_definition.metadata)
        source_pack_id = metadata.pop("pack_id", "")
        source_pack_version = metadata.pop("pack_version", 0)
        metadata.update(
            {
                "builtin": False,
                "readonly": False,
                "cloneable": True,
                "cloned_from": {
                    "workflow_id": source_definition.workflow_id,
                    "version_id": source_version_id,
                    "version": source_version,
                    "definition_sha256": source_definition.sha256(),
                    "pack_id": source_pack_id,
                    "pack_version": source_pack_version,
                },
            }
        )
        clone = replace(
            _detached_definition(source_definition),
            workflow_id=workflow_id,
            name=name,
            description=source_definition.description if description is None else description,
            metadata=metadata,
        )
        clone.validate(mode=ValidationMode.LINEAR)
        return cls(
            definition=clone,
            is_builtin=False,
            is_readonly=False,
            source_version_id=source_version_id,
            next_version=1,
        )

    def add_node(self, node: NodeDefinition, *, index: int | None = None) -> "LinearWorkflowEditor":
        """Insert a complete node at a zero-based position and rebuild control edges."""

        if not isinstance(node, NodeDefinition):
            raise TypeError("node must be a NodeDefinition")
        nodes = list(self.definition.nodes)
        if any(existing.node_id == node.node_id for existing in nodes):
            raise LinearWorkflowEditError(f"node_id already exists: {node.node_id}")
        position = len(nodes) if index is None else _bounded_index(index, len(nodes), allow_end=True)
        nodes.insert(position, node)
        return self._replace_topology(nodes)

    def remove_node(self, node_id: str) -> "LinearWorkflowEditor":
        """Remove a node only when all remaining artifact contracts stay valid."""

        index = self._node_index(node_id)
        if len(self.definition.nodes) == 1:
            raise LinearWorkflowEditError("a linear workflow must retain at least one node")
        nodes = list(self.definition.nodes)
        del nodes[index]
        return self._replace_topology(nodes)

    def move_node(self, node_id: str, index: int) -> "LinearWorkflowEditor":
        """Move a node to a zero-based final position without changing its identity."""

        source_index = self._node_index(node_id)
        target_index = _bounded_index(index, len(self.definition.nodes) - 1, allow_end=True)
        nodes = list(self.definition.nodes)
        node = nodes.pop(source_index)
        nodes.insert(target_index, node)
        return self._replace_topology(nodes)

    def replace_node(self, node_id: str, replacement: NodeDefinition) -> "LinearWorkflowEditor":
        """Atomically replace a node contract while keeping its stable node_id."""

        if not isinstance(replacement, NodeDefinition):
            raise TypeError("replacement must be a NodeDefinition")
        index = self._node_index(node_id)
        if replacement.node_id != node_id:
            raise LinearWorkflowEditError("node_id is immutable; add and remove explicitly instead")
        nodes = list(self.definition.nodes)
        nodes[index] = replace(replacement, display_order=nodes[index].display_order)
        return self._replace_content(nodes=nodes)

    def update_node(
        self,
        node_id: str,
        *,
        name: str | _Unset = _UNSET,
        node_type: NodeType | str | _Unset = _UNSET,
        implementation_ref: str | _Unset = _UNSET,
        execution_profile: ExecutionProfile | _Unset = _UNSET,
        prompt_ref: PromptRef | None | _Unset = _UNSET,
        inputs: Sequence[ArtifactSpec] | _Unset = _UNSET,
        input_bindings: Sequence[ArtifactBinding] | _Unset = _UNSET,
        outputs: Sequence[ArtifactSpec] | _Unset = _UNSET,
        session_policy: SessionPolicy | _Unset = _UNSET,
        retry_policy: RetryPolicy | _Unset = _UNSET,
        metadata: Mapping[str, Any] | _Unset = _UNSET,
    ) -> "LinearWorkflowEditor":
        """Atomically edit executor, Prompt, artifact, and session contracts."""

        index = self._node_index(node_id)
        current = self.definition.nodes[index]
        changes: dict[str, Any] = {}
        if not isinstance(name, _Unset):
            if not isinstance(name, str):
                raise TypeError("name must be a string")
            changes["name"] = name
        if not isinstance(node_type, _Unset):
            changes["node_type"] = NodeType(node_type)
        if not isinstance(implementation_ref, _Unset):
            if not isinstance(implementation_ref, str):
                raise TypeError("implementation_ref must be a string")
            changes["implementation_ref"] = implementation_ref
        if not isinstance(execution_profile, _Unset):
            if not isinstance(execution_profile, ExecutionProfile):
                raise TypeError("execution_profile must be an ExecutionProfile")
            changes["execution_profile"] = execution_profile
        if not isinstance(prompt_ref, _Unset):
            if prompt_ref is not None and not isinstance(prompt_ref, PromptRef):
                raise TypeError("prompt_ref must be a PromptRef or null")
            changes["prompt_ref"] = prompt_ref
        if not isinstance(inputs, _Unset):
            changes["inputs"] = _record_tuple(inputs, ArtifactSpec, "inputs")
        if not isinstance(input_bindings, _Unset):
            changes["input_bindings"] = _record_tuple(
                input_bindings,
                ArtifactBinding,
                "input_bindings",
            )
        if not isinstance(outputs, _Unset):
            changes["outputs"] = _record_tuple(outputs, ArtifactSpec, "outputs")
        if not isinstance(session_policy, _Unset):
            if not isinstance(session_policy, SessionPolicy):
                raise TypeError("session_policy must be a SessionPolicy")
            changes["session_policy"] = session_policy
        if not isinstance(retry_policy, _Unset):
            if not isinstance(retry_policy, RetryPolicy):
                raise TypeError("retry_policy must be a RetryPolicy")
            changes["retry_policy"] = retry_policy
        if not isinstance(metadata, _Unset):
            if not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            changes["metadata"] = metadata
        if not changes:
            return self._replace_content(nodes=self.definition.nodes)
        return self.replace_node(node_id, replace(current, **changes))

    def update_interface(
        self,
        *,
        name: str | _Unset = _UNSET,
        description: str | _Unset = _UNSET,
        inputs: Sequence[ArtifactSpec] | _Unset = _UNSET,
        outputs: Sequence[ArtifactSpec] | _Unset = _UNSET,
        output_bindings: Sequence[ArtifactBinding] | _Unset = _UNSET,
        metadata: Mapping[str, Any] | _Unset = _UNSET,
    ) -> "LinearWorkflowEditor":
        """Atomically replace the workflow-level portable artifact interface."""

        changes: dict[str, Any] = {}
        for field_name, value in {"name": name, "description": description}.items():
            if isinstance(value, _Unset):
                continue
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string")
            changes[field_name] = value
        if not isinstance(inputs, _Unset):
            changes["inputs"] = _record_tuple(inputs, ArtifactSpec, "inputs")
        if not isinstance(outputs, _Unset):
            changes["outputs"] = _record_tuple(outputs, ArtifactSpec, "outputs")
        if not isinstance(output_bindings, _Unset):
            changes["output_bindings"] = _record_tuple(
                output_bindings,
                ArtifactBinding,
                "output_bindings",
            )
        if not isinstance(metadata, _Unset):
            if not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            changes["metadata"] = metadata
        candidate = replace(self.definition, **changes)
        candidate.validate(mode=ValidationMode.LINEAR)
        return self._next_editor(candidate)

    def materialize(
        self,
        *,
        created_at: str,
        version: int | None = None,
    ) -> WorkflowVersion:
        """Create a fresh immutable definition and uniquely identified draft version."""

        version_number = self.next_version if version is None else version
        if isinstance(version_number, bool) or not isinstance(version_number, int) or version_number <= 0:
            raise ValueError("version must be a positive integer")
        if version_number < self.next_version:
            raise ValueError(f"version must be at least {self.next_version}")
        detached = _detached_definition(self.definition)
        detached.validate(mode=ValidationMode.LINEAR)
        value = WorkflowVersion.from_definition(
            detached,
            version=version_number,
            version_id=f"workflow_version_{uuid4().hex}",
            created_at=created_at,
            status=WorkflowVersionStatus.DRAFT,
            mode=ValidationMode.LINEAR,
        )
        if value.version_id == self.source_version_id:
            raise RuntimeError("materialized version identity unexpectedly reused its source")
        return value

    def _node_index(self, node_id: str) -> int:
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id is required")
        index = next(
            (position for position, node in enumerate(self.definition.nodes) if node.node_id == node_id),
            -1,
        )
        if index < 0:
            raise KeyError(node_id)
        return index

    def _replace_topology(self, nodes: Sequence[NodeDefinition]) -> "LinearWorkflowEditor":
        ordered = tuple(
            replace(node, display_order=(index + 1) * 10)
            for index, node in enumerate(nodes)
        )
        candidate = replace(
            self.definition,
            nodes=ordered,
            edges=_linear_success_edges(ordered),
        )
        candidate.validate(mode=ValidationMode.LINEAR)
        return self._next_editor(candidate)

    def _replace_content(self, *, nodes: Sequence[NodeDefinition]) -> "LinearWorkflowEditor":
        candidate = replace(self.definition, nodes=tuple(nodes))
        candidate.validate(mode=ValidationMode.LINEAR)
        return self._next_editor(candidate)

    def _next_editor(self, definition: WorkflowDefinition) -> "LinearWorkflowEditor":
        return LinearWorkflowEditor(
            definition=definition,
            is_builtin=self.is_builtin,
            is_readonly=self.is_readonly,
            source_version_id=self.source_version_id,
            next_version=self.next_version,
        )


def _definition_is_protected(definition: WorkflowDefinition) -> bool:
    return any(
        key in definition.metadata and definition.metadata[key] is not False
        for key in _PROTECTED_METADATA_KEYS
    )


def _ensure_editable(
    definition: WorkflowDefinition,
    *,
    is_builtin: bool,
    is_readonly: bool,
) -> None:
    if is_builtin or is_readonly or _definition_is_protected(definition):
        raise LinearWorkflowEditError(
            "built-in/read-only workflow definitions must be cloned before editing"
        )


def _detached_definition(definition: WorkflowDefinition) -> WorkflowDefinition:
    definition.validate(mode=ValidationMode.LINEAR)
    detached = WorkflowDefinition.from_dict(definition.to_dict())
    ordered_nodes = _nodes_in_success_order(detached)
    node_order = {
        node.node_id: index
        for index, node in enumerate(ordered_nodes)
    }
    return replace(
        detached,
        nodes=ordered_nodes,
        edges=tuple(
            sorted(
                detached.edges,
                key=lambda edge: (
                    node_order.get(edge.source_node_id, len(node_order)),
                    node_order.get(edge.target_node_id, len(node_order)),
                    edge.edge_id,
                ),
            )
        ),
    )


def _nodes_in_success_order(definition: WorkflowDefinition) -> tuple[NodeDefinition, ...]:
    """Recover the execution chain without treating UI display order as semantics."""

    nodes_by_id = {node.node_id: node for node in definition.nodes}
    successor_by_id = {
        edge.source_node_id: edge.target_node_id
        for edge in definition.edges
        if edge.edge_type is EdgeType.SUCCESS
    }
    target_ids = set(successor_by_id.values())
    entry_ids = [node_id for node_id in nodes_by_id if node_id not in target_ids]
    if len(entry_ids) != 1:
        raise LinearWorkflowEditError("linear workflow must have exactly one entry node")

    ordered: list[NodeDefinition] = []
    current_id: str | None = entry_ids[0]
    visited: set[str] = set()
    while current_id is not None:
        if current_id in visited or current_id not in nodes_by_id:
            raise LinearWorkflowEditError("linear workflow success chain is invalid")
        visited.add(current_id)
        ordered.append(nodes_by_id[current_id])
        current_id = successor_by_id.get(current_id)
    if len(ordered) != len(nodes_by_id):
        raise LinearWorkflowEditError("linear workflow success chain does not cover every node")
    return tuple(ordered)


def _bounded_index(value: int, maximum: int, *, allow_end: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("index must be an integer")
    upper = maximum if allow_end else maximum - 1
    if value < 0 or value > upper:
        raise IndexError(f"index must be between 0 and {upper}")
    return value


def _record_tuple(value: Sequence[Any], record_type: type, label: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    records = tuple(value)
    if not all(isinstance(item, record_type) for item in records):
        raise TypeError(f"{label} must contain {record_type.__name__} records")
    return records


def _linear_success_edges(nodes: Sequence[NodeDefinition]) -> tuple[EdgeDefinition, ...]:
    edges: list[EdgeDefinition] = []
    for source, target in zip(nodes, nodes[1:]):
        digest = hashlib.sha256(f"{source.node_id}\0{target.node_id}".encode("utf-8")).hexdigest()[:24]
        edges.append(
            EdgeDefinition(
                edge_id=f"linear-{digest}",
                source_node_id=source.node_id,
                target_node_id=target.node_id,
                edge_type=EdgeType.SUCCESS,
            )
        )
    return tuple(edges)


__all__ = ["LinearWorkflowEditError", "LinearWorkflowEditor"]
