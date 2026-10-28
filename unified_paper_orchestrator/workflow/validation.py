from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    CONDITION_LANGUAGE,
    MAX_LOOP_TRAVERSALS,
    ArtifactSourceKind,
    EdgeDefinition,
    EdgeType,
    NodeDefinition,
    NodeType,
    ValidationMode,
    WORKFLOW_DEFINITION_SCHEMA,
    WORKFLOW_RUNTIME_BINDING_SCHEMA,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    is_sha256,
    looks_like_absolute_path,
)


_LOGICAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:/-]{0,255}\Z")
_JSON_LOGIC_OPERATORS = {"!", "!=", "<", "<=", "==", ">", ">=", "and", "in", "missing", "or", "var"}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        location = f" at {self.path}" if self.path else ""
        return f"{self.code}{location}: {self.message}"


class WorkflowValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _reachable(start: str, adjacency: dict[str, set[str]]) -> set[str]:
    reached: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(sorted(adjacency.get(current, set()) - reached, reverse=True))
    return reached


def _has_cycle(node_ids: set[str], adjacency: dict[str, set[str]]) -> bool:
    indegree = {node_id: 0 for node_id in node_ids}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    pending = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        for target in sorted(adjacency.get(current, set())):
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    return visited != len(node_ids)


def _is_logical_id(value: str) -> bool:
    if value != value.strip() or not _LOGICAL_ID_RE.fullmatch(value):
        return False
    if "\\" in value or "://" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        return False
    return True


def is_logical_id(value: str) -> bool:
    """Public predicate shared by persisted identity contracts."""

    return isinstance(value, str) and _is_logical_id(value)


def _valid_condition_expression(value: Any) -> bool:
    if isinstance(value, Mapping):
        if len(value) != 1:
            return False
        operator, operand = next(iter(value.items()))
        if operator not in _JSON_LOGIC_OPERATORS:
            return False
        return _valid_condition_expression(operand)
    if isinstance(value, (list, tuple)):
        return all(_valid_condition_expression(item) for item in value)
    return value is None or isinstance(value, (str, bool, int, float))


def _absolute_path_locations(value: Any, path: str = "") -> list[str]:
    locations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            locations.extend(_absolute_path_locations(item, child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            locations.extend(_absolute_path_locations(item, f"{path}[{index}]"))
    elif isinstance(value, str) and looks_like_absolute_path(value):
        locations.append(path)
    return locations


def _dominators(
    entry: str,
    node_ids: set[str],
    reverse: dict[str, set[str]],
) -> dict[str, set[str]]:
    values = {
        node_id: ({entry} if node_id == entry else set(node_ids))
        for node_id in node_ids
    }
    changed = True
    while changed:
        changed = False
        for node_id in sorted(node_ids - {entry}):
            predecessors = reverse.get(node_id, set())
            common = set.intersection(*(values[item] for item in predecessors)) if predecessors else set()
            updated = {node_id} | common
            if updated != values[node_id]:
                values[node_id] = updated
                changed = True
    return values


def _validate_node_contract(node: NodeDefinition, path: str, issues: list[ValidationIssue]) -> None:
    if not _is_logical_id(node.node_id):
        issues.append(ValidationIssue("node.id.invalid", "node_id must be a portable logical ID", f"{path}.node_id"))
    if not node.name.strip():
        issues.append(ValidationIssue("node.name.required", "node name is required", f"{path}.name"))
    if node.display_order < 0:
        issues.append(
            ValidationIssue("node.display_order.invalid", "display_order cannot be negative", f"{path}.display_order")
        )
    if node.node_type is NodeType.CODEX and node.prompt_ref is None:
        issues.append(
            ValidationIssue("node.prompt.required", "codex nodes require a PromptRef", f"{path}.prompt_ref")
        )
    if node.node_type in {NodeType.DETERMINISTIC, NodeType.PLUGIN, NodeType.SUBWORKFLOW, NodeType.LEGACY_STAGE}:
        if not node.implementation_ref.strip():
            issues.append(
                ValidationIssue(
                    "node.implementation_ref.required",
                    f"{node.node_type.value} nodes require a logical implementation_ref",
                    f"{path}.implementation_ref",
                )
            )
        elif not _is_logical_id(node.implementation_ref) or looks_like_absolute_path(node.implementation_ref):
            issues.append(
                ValidationIssue(
                    "node.implementation_ref.not_portable",
                    "implementation_ref must be a logical identifier, not an absolute path",
                    f"{path}.implementation_ref",
                )
            )
    if node.prompt_ref is not None:
        if not _is_logical_id(node.prompt_ref.resource_id):
            issues.append(
                ValidationIssue(
                    "prompt.resource_id.invalid",
                    "prompt resource_id must be a portable logical ID",
                    f"{path}.prompt_ref.resource_id",
                )
            )
        elif looks_like_absolute_path(node.prompt_ref.resource_id):
            issues.append(
                ValidationIssue(
                    "prompt.resource_id.not_portable",
                    "prompt resource_id must not be an absolute path",
                    f"{path}.prompt_ref.resource_id",
                )
            )
        if not is_sha256(node.prompt_ref.content_sha256):
            issues.append(
                ValidationIssue(
                    "prompt.sha256.invalid",
                    "prompt content_sha256 must be a 64-character hexadecimal digest",
                    f"{path}.prompt_ref.content_sha256",
                )
            )
        if node.prompt_ref.version and not _is_logical_id(node.prompt_ref.version):
            issues.append(
                ValidationIssue(
                    "prompt.version.invalid",
                    "prompt version must be a logical ID",
                    f"{path}.prompt_ref.version",
                )
            )
    profile = node.execution_profile
    if profile.token_limit is not None and profile.token_limit <= 0:
        issues.append(
            ValidationIssue("execution.token_limit.invalid", "token_limit must be positive", f"{path}.execution_profile.token_limit")
        )
    if profile.timeout_seconds <= 0:
        issues.append(
            ValidationIssue("execution.timeout.invalid", "timeout_seconds must be positive", f"{path}.execution_profile.timeout_seconds")
        )
    retry = node.retry_policy
    if retry.max_attempts <= 0:
        issues.append(
            ValidationIssue("retry.max_attempts.invalid", "max_attempts must be positive", f"{path}.retry_policy.max_attempts")
        )
    if retry.initial_delay_seconds < 0 or retry.max_delay_seconds < 0 or retry.backoff_multiplier < 1:
        issues.append(
            ValidationIssue(
                "retry.backoff.invalid",
                "retry delays must be non-negative and backoff_multiplier must be at least 1",
                f"{path}.retry_policy",
            )
        )
    session = node.session_policy
    if session.force_new_session and (session.reuse_within_run or session.reuse_on_retry):
        issues.append(
            ValidationIssue(
                "session.policy.conflict",
                "force_new_session cannot be combined with session reuse",
                f"{path}.session_policy",
            )
        )
    if (session.reuse_within_run or session.reuse_on_retry) and not _is_logical_id(session.session_group):
        issues.append(
            ValidationIssue(
                "session.group.required",
                "session reuse requires a non-empty logical session_group",
                f"{path}.session_policy.session_group",
            )
        )
    for duplicate in sorted(_duplicates(spec.artifact_id for spec in node.inputs)):
        issues.append(
            ValidationIssue("artifact.input_spec.duplicate", f"duplicate node input artifact_id: {duplicate}", f"{path}.inputs")
        )
    for index, spec in enumerate(node.inputs):
        _validate_artifact_spec(spec, f"{path}.inputs[{index}]", issues)
    for duplicate in sorted(_duplicates(spec.artifact_id for spec in node.outputs)):
        issues.append(
            ValidationIssue("artifact.output.duplicate", f"duplicate node output artifact_id: {duplicate}", f"{path}.outputs")
        )
    for index, output in enumerate(node.outputs):
        _validate_artifact_spec(output, f"{path}.outputs[{index}]", issues)
    for duplicate in sorted(_duplicates(binding.target_artifact_id for binding in node.input_bindings)):
        issues.append(
            ValidationIssue("artifact.input.duplicate", f"duplicate node input binding: {duplicate}", f"{path}.input_bindings")
        )


def _validate_artifact_spec(spec: Any, path: str, issues: list[ValidationIssue]) -> None:
    if not _is_logical_id(spec.artifact_id):
        issues.append(
            ValidationIssue("artifact.id.invalid", "artifact_id must be a portable logical ID", f"{path}.artifact_id")
        )
    if not spec.media_type.strip():
        issues.append(ValidationIssue("artifact.media_type.required", "media_type is required", f"{path}.media_type"))
    if spec.schema_ref and not _is_logical_id(spec.schema_ref):
        issues.append(
            ValidationIssue("artifact.schema_ref.invalid", "schema_ref must be a logical ID", f"{path}.schema_ref")
        )


def _validate_artifact_compatibility(
    source: Any,
    target: Any,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if target.required and not source.required:
        issues.append(
            ValidationIssue(
                "artifact.binding.required_from_optional",
                "a required artifact target cannot depend on an optional source",
                path,
            )
        )
    if source.media_type != target.media_type:
        issues.append(
            ValidationIssue(
                "artifact.binding.media_type_mismatch",
                f"artifact media type mismatch: {source.media_type} != {target.media_type}",
                path,
            )
        )
    if source.schema_ref != target.schema_ref:
        issues.append(
            ValidationIssue(
                "artifact.binding.schema_mismatch",
                f"artifact schema mismatch: {source.schema_ref or '<none>'} != {target.schema_ref or '<none>'}",
                path,
            )
        )


def _validate_edge_contract(edge: EdgeDefinition, path: str, issues: list[ValidationIssue]) -> None:
    if not _is_logical_id(edge.edge_id):
        issues.append(ValidationIssue("edge.id.invalid", "edge_id must be a portable logical ID", f"{path}.edge_id"))
    if edge.edge_type is EdgeType.LOOP:
        if (
            isinstance(edge.max_traversals, bool)
            or not isinstance(edge.max_traversals, int)
            or not 1 <= edge.max_traversals <= MAX_LOOP_TRAVERSALS
        ):
            issues.append(
                ValidationIssue(
                    "edge.loop.limit.required",
                    f"loop edges require max_traversals between 1 and {MAX_LOOP_TRAVERSALS}",
                    f"{path}.max_traversals",
                )
            )
        if not edge.condition:
            issues.append(
                ValidationIssue("edge.loop.condition.required", "loop edges require an explicit condition", f"{path}.condition")
            )
        elif not _valid_condition_expression(edge.condition):
            issues.append(
                ValidationIssue(
                    "edge.condition.invalid",
                    "condition must use the supported json-logic/v1 expression subset",
                    f"{path}.condition",
                )
            )
        if edge.condition_language != CONDITION_LANGUAGE:
            issues.append(
                ValidationIssue(
                    "edge.condition.language",
                    f"conditional edges must use {CONDITION_LANGUAGE}",
                    f"{path}.condition_language",
                )
            )
    elif edge.max_traversals is not None:
        issues.append(
            ValidationIssue(
                "edge.limit.unexpected",
                "max_traversals is only valid for loop edges",
                f"{path}.max_traversals",
            )
        )
    if edge.edge_type is EdgeType.CONDITION:
        if not edge.condition:
            issues.append(
                ValidationIssue("edge.condition.required", "condition edges require an expression", f"{path}.condition")
            )
        elif not _valid_condition_expression(edge.condition):
            issues.append(
                ValidationIssue(
                    "edge.condition.invalid",
                    "condition must use the supported json-logic/v1 expression subset",
                    f"{path}.condition",
                )
            )
        if edge.condition_language != CONDITION_LANGUAGE:
            issues.append(
                ValidationIssue(
                    "edge.condition.language",
                    f"conditional edges must use {CONDITION_LANGUAGE}",
                    f"{path}.condition_language",
                )
            )
    elif edge.edge_type is not EdgeType.LOOP and (edge.condition is not None or edge.condition_language):
        issues.append(
            ValidationIssue(
                "edge.condition.unexpected",
                "only condition and loop edges may carry a condition",
                f"{path}.condition",
            )
        )


def validate_workflow_definition(
    definition: WorkflowDefinition,
    *,
    mode: ValidationMode = ValidationMode.LINEAR,
) -> None:
    mode = ValidationMode(mode)
    issues: list[ValidationIssue] = []
    if definition.schema != WORKFLOW_DEFINITION_SCHEMA:
        issues.append(
            ValidationIssue("workflow.schema.unsupported", f"unsupported workflow schema: {definition.schema}", "schema")
        )
    if not _is_logical_id(definition.workflow_id):
        issues.append(ValidationIssue("workflow.id.invalid", "workflow_id must be a portable logical ID", "workflow_id"))
    if not definition.name.strip():
        issues.append(ValidationIssue("workflow.name.required", "workflow name is required", "name"))
    if not definition.nodes:
        issues.append(ValidationIssue("workflow.nodes.required", "workflow requires at least one node", "nodes"))
    try:
        definition.canonical_json()
    except (TypeError, ValueError) as exc:
        issues.append(
            ValidationIssue(
                "workflow.json.invalid",
                f"workflow definition must contain finite JSON values: {type(exc).__name__}",
            )
        )
    for path in _absolute_path_locations(definition.to_dict()):
        issues.append(
            ValidationIssue(
                "workflow.path.not_portable",
                "portable workflow definitions cannot contain absolute paths; use WorkflowRuntimeBinding",
                path,
            )
        )

    node_ids = [node.node_id for node in definition.nodes]
    node_id_set = set(node_ids)
    for duplicate in sorted(_duplicates(node_ids)):
        issues.append(ValidationIssue("node.id.duplicate", f"duplicate node_id: {duplicate}", "nodes"))
    for duplicate in sorted(_duplicates(str(node.display_order) for node in definition.nodes)):
        issues.append(
            ValidationIssue("node.display_order.duplicate", f"duplicate display_order: {duplicate}", "nodes")
        )
    for index, node in enumerate(definition.nodes):
        _validate_node_contract(node, f"nodes[{index}]", issues)

    prompt_digests: dict[str, set[str]] = {}
    for node in definition.nodes:
        if node.prompt_ref is not None:
            prompt_digests.setdefault(node.prompt_ref.resource_id, set()).add(
                node.prompt_ref.content_sha256.lower()
            )
    for resource_id, digests in sorted(prompt_digests.items()):
        if len(digests) > 1:
            issues.append(
                ValidationIssue(
                    "prompt.resource_id.conflict",
                    f"PromptRef resource_id has conflicting content digests: {resource_id}",
                    "nodes",
                )
            )

    workflow_input_ids = [spec.artifact_id for spec in definition.inputs]
    for duplicate in sorted(_duplicates(workflow_input_ids)):
        issues.append(
            ValidationIssue("artifact.workflow_input.duplicate", f"duplicate workflow input: {duplicate}", "inputs")
        )
    for index, spec in enumerate(definition.inputs):
        _validate_artifact_spec(spec, f"inputs[{index}]", issues)
    workflow_output_ids = [spec.artifact_id for spec in definition.outputs]
    for duplicate in sorted(_duplicates(workflow_output_ids)):
        issues.append(
            ValidationIssue("artifact.workflow_output.duplicate", f"duplicate workflow output: {duplicate}", "outputs")
        )
    for index, spec in enumerate(definition.outputs):
        _validate_artifact_spec(spec, f"outputs[{index}]", issues)
    for duplicate in sorted(_duplicates(binding.target_artifact_id for binding in definition.output_bindings)):
        issues.append(
            ValidationIssue(
                "artifact.workflow_output_binding.duplicate",
                f"duplicate workflow output binding: {duplicate}",
                "output_bindings",
            )
        )

    edge_ids = [edge.edge_id for edge in definition.edges]
    for duplicate in sorted(_duplicates(edge_ids)):
        issues.append(ValidationIssue("edge.id.duplicate", f"duplicate edge_id: {duplicate}", "edges"))
    for index, edge in enumerate(definition.edges):
        path = f"edges[{index}]"
        _validate_edge_contract(edge, path, issues)
        if edge.source_node_id not in node_id_set:
            issues.append(
                ValidationIssue(
                    "edge.source.missing",
                    f"edge source does not exist: {edge.source_node_id}",
                    f"{path}.source_node_id",
                )
            )
        if edge.target_node_id not in node_id_set:
            issues.append(
                ValidationIssue(
                    "edge.target.missing",
                    f"edge target does not exist: {edge.target_node_id}",
                    f"{path}.target_node_id",
                )
            )

    forward_edges = [
        edge
        for edge in definition.edges
        if edge.edge_type is not EdgeType.LOOP
        and edge.source_node_id in node_id_set
        and edge.target_node_id in node_id_set
    ]
    adjacency = {node_id: set() for node_id in node_id_set}
    reverse = {node_id: set() for node_id in node_id_set}
    for edge in forward_edges:
        adjacency[edge.source_node_id].add(edge.target_node_id)
        reverse[edge.target_node_id].add(edge.source_node_id)

    entries = sorted(node_id for node_id in node_id_set if not reverse[node_id])
    exits = sorted(node_id for node_id in node_id_set if not adjacency[node_id])
    if node_id_set and len(entries) != 1:
        issues.append(
            ValidationIssue("graph.entry.count", f"workflow requires exactly one entry node; found {len(entries)}", "nodes")
        )
    if node_id_set and len(exits) != 1:
        issues.append(
            ValidationIssue("graph.exit.count", f"workflow requires exactly one exit node; found {len(exits)}", "nodes")
        )
    if len(entries) == 1:
        missing = node_id_set - _reachable(entries[0], adjacency)
        if missing:
            issues.append(
                ValidationIssue(
                    "graph.disconnected",
                    f"nodes are not reachable from the entry: {', '.join(sorted(missing))}",
                    "nodes",
                )
            )
    if len(exits) == 1:
        unable_to_exit = node_id_set - _reachable(exits[0], reverse)
        if unable_to_exit:
            issues.append(
                ValidationIssue(
                    "graph.no_exit_path",
                    f"nodes cannot reach the exit: {', '.join(sorted(unable_to_exit))}",
                    "nodes",
                )
            )
    if _has_cycle(node_id_set, adjacency):
        issues.append(
            ValidationIssue("graph.cycle.untyped", "cycles must be represented by explicit finite loop edges", "edges")
        )

    dominators = _dominators(entries[0], node_id_set, reverse) if len(entries) == 1 else {}

    for index, edge in enumerate(definition.edges):
        if (
            edge.edge_type is EdgeType.LOOP
            and edge.source_node_id in node_id_set
            and edge.target_node_id in node_id_set
            and edge.source_node_id not in _reachable(edge.target_node_id, adjacency)
        ):
            issues.append(
                ValidationIssue(
                    "edge.loop.not_back_edge",
                    "a loop edge must return to a node that reaches the loop source on the forward graph",
                    f"edges[{index}]",
                )
            )

    nodes_by_id = {node.node_id: node for node in definition.nodes}
    workflow_inputs = {spec.artifact_id: spec for spec in definition.inputs}
    for node_index, node in enumerate(definition.nodes):
        target_specs = {spec.artifact_id: spec for spec in node.inputs}
        bound_targets = {binding.target_artifact_id for binding in node.input_bindings}
        for required in sorted(spec.artifact_id for spec in node.inputs if spec.required):
            if required not in bound_targets:
                issues.append(
                    ValidationIssue(
                        "artifact.binding.required_missing",
                        f"required node input is not bound: {node.node_id}.{required}",
                        f"nodes[{node_index}].input_bindings",
                    )
                )
        for binding_index, binding in enumerate(node.input_bindings):
            path = f"nodes[{node_index}].input_bindings[{binding_index}]"
            if not _is_logical_id(binding.target_artifact_id) or not _is_logical_id(binding.source_artifact_id):
                issues.append(
                    ValidationIssue(
                        "artifact.binding.invalid",
                        "binding target and source artifact IDs must be logical IDs",
                        path,
                    )
                )
                continue
            target_spec = target_specs.get(binding.target_artifact_id)
            if target_spec is None:
                issues.append(
                    ValidationIssue(
                        "artifact.binding.target_missing",
                        f"node input spec does not exist: {node.node_id}.{binding.target_artifact_id}",
                        path,
                    )
                )
            if binding.source_kind is ArtifactSourceKind.WORKFLOW_INPUT:
                if binding.source_node_id:
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.workflow_input_node",
                            "workflow-input bindings cannot specify source_node_id",
                            path,
                        )
                    )
                source_spec = workflow_inputs.get(binding.source_artifact_id)
                if source_spec is None:
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.workflow_input_missing",
                            f"workflow input does not exist: {binding.source_artifact_id}",
                            path,
                        )
                    )
            else:
                source_spec = None
                source = nodes_by_id.get(binding.source_node_id)
                if source is None:
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.source_node_missing",
                            f"artifact source node does not exist: {binding.source_node_id}",
                            path,
                        )
                    )
                    continue
                source_spec = next(
                    (output for output in source.outputs if output.artifact_id == binding.source_artifact_id),
                    None,
                )
                if source_spec is None:
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.source_output_missing",
                            f"source output does not exist: {binding.source_node_id}.{binding.source_artifact_id}",
                            path,
                        )
                    )
                if node.node_id not in _reachable(source.node_id, adjacency) or source.node_id == node.node_id:
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.not_upstream",
                            f"artifact source node is not upstream of {node.node_id}: {source.node_id}",
                            path,
                        )
                    )
                elif source.node_id not in dominators.get(node.node_id, set()):
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.not_dominating",
                            f"artifact source is not guaranteed on every path to {node.node_id}: {source.node_id}",
                            path,
                        )
                    )
            if target_spec is not None and source_spec is not None:
                _validate_artifact_compatibility(source_spec, target_spec, path, issues)

    workflow_outputs = {spec.artifact_id: spec for spec in definition.outputs}
    bound_workflow_outputs = {binding.target_artifact_id for binding in definition.output_bindings}
    for required in sorted(spec.artifact_id for spec in definition.outputs if spec.required):
        if required not in bound_workflow_outputs:
            issues.append(
                ValidationIssue(
                    "artifact.workflow_output.required_missing",
                    f"required workflow output is not bound: {required}",
                    "output_bindings",
                )
            )
    for index, binding in enumerate(definition.output_bindings):
        path = f"output_bindings[{index}]"
        if not _is_logical_id(binding.target_artifact_id) or not _is_logical_id(binding.source_artifact_id):
            issues.append(
                ValidationIssue(
                    "artifact.binding.invalid",
                    "binding target and source artifact IDs must be logical IDs",
                    path,
                )
            )
        target_spec = workflow_outputs.get(binding.target_artifact_id)
        if target_spec is None:
            issues.append(
                ValidationIssue(
                    "artifact.workflow_output.target_missing",
                    f"workflow output spec does not exist: {binding.target_artifact_id}",
                    path,
                )
            )
        source_spec = None
        if binding.source_kind is ArtifactSourceKind.WORKFLOW_INPUT:
            if binding.source_node_id:
                issues.append(
                    ValidationIssue(
                        "artifact.binding.workflow_input_node",
                        "workflow-input bindings cannot specify source_node_id",
                        path,
                    )
                )
            source_spec = workflow_inputs.get(binding.source_artifact_id)
            if source_spec is None:
                issues.append(
                    ValidationIssue(
                        "artifact.binding.workflow_input_missing",
                        f"workflow input does not exist: {binding.source_artifact_id}",
                        path,
                    )
                )
        else:
            source = nodes_by_id.get(binding.source_node_id)
            if source is None:
                issues.append(
                    ValidationIssue(
                        "artifact.binding.source_node_missing",
                        f"artifact source node does not exist: {binding.source_node_id}",
                        path,
                    )
                )
            else:
                source_spec = next(
                    (output for output in source.outputs if output.artifact_id == binding.source_artifact_id),
                    None,
                )
                if source_spec is None:
                    issues.append(
                        ValidationIssue(
                            "artifact.binding.source_output_missing",
                            f"source output does not exist: {binding.source_node_id}.{binding.source_artifact_id}",
                            path,
                        )
                    )
                elif len(exits) == 1 and target_spec is not None and target_spec.required:
                    if source.node_id not in dominators.get(exits[0], set()):
                        issues.append(
                            ValidationIssue(
                                "artifact.workflow_output.not_dominating",
                                f"required workflow output is not guaranteed on every path: {source.node_id}",
                                path,
                            )
                        )
        if target_spec is not None and source_spec is not None:
            _validate_artifact_compatibility(source_spec, target_spec, path, issues)

    if mode is ValidationMode.LINEAR:
        non_success = [edge.edge_id for edge in definition.edges if edge.edge_type is not EdgeType.SUCCESS]
        if non_success:
            issues.append(
                ValidationIssue(
                    "linear.edge_type",
                    f"linear workflows only allow success edges: {', '.join(sorted(non_success))}",
                    "edges",
                )
            )
        if len(forward_edges) != max(0, len(node_id_set) - 1):
            issues.append(
                ValidationIssue("linear.edge_count", "linear workflows require exactly node_count - 1 edges", "edges")
            )
        branched = sorted(node_id for node_id in node_id_set if len(adjacency[node_id]) > 1)
        merged = sorted(node_id for node_id in node_id_set if len(reverse[node_id]) > 1)
        if branched:
            issues.append(
                ValidationIssue("linear.branch", f"linear workflows cannot branch at: {', '.join(branched)}", "edges")
            )
        if merged:
            issues.append(
                ValidationIssue("linear.merge", f"linear workflows cannot merge at: {', '.join(merged)}", "edges")
            )

    if issues:
        raise WorkflowValidationError(issues)


def validate_runtime_binding(
    definition: WorkflowDefinition,
    binding: WorkflowRuntimeBinding,
) -> None:
    validate_workflow_definition(definition, mode=ValidationMode.GRAPH)
    issues: list[ValidationIssue] = []
    if binding.schema != WORKFLOW_RUNTIME_BINDING_SCHEMA:
        issues.append(
            ValidationIssue("binding.schema.unsupported", f"unsupported runtime binding schema: {binding.schema}", "schema")
        )
    if binding.definition_sha256.lower() != definition.sha256():
        issues.append(
            ValidationIssue(
                "binding.definition.mismatch",
                "runtime binding definition_sha256 does not match the portable definition",
                "definition_sha256",
            )
        )
    resources = {resource.resource_id: resource for resource in binding.resources}
    for duplicate in sorted(_duplicates(resource.resource_id for resource in binding.resources)):
        issues.append(
            ValidationIssue("binding.resource.duplicate", f"duplicate resource binding: {duplicate}", "resources")
        )
    referenced: dict[str, str] = {}
    for node in definition.nodes:
        if node.prompt_ref is None:
            continue
        previous = referenced.get(node.prompt_ref.resource_id)
        digest = node.prompt_ref.content_sha256.lower()
        if previous is not None and previous != digest:
            issues.append(
                ValidationIssue(
                    "binding.resource.prompt_conflict",
                    f"definition has conflicting PromptRef digests: {node.prompt_ref.resource_id}",
                    "resources",
                )
            )
        referenced[node.prompt_ref.resource_id] = digest
    for resource_id, digest in sorted(referenced.items()):
        resource = resources.get(resource_id)
        if resource is None:
            issues.append(
                ValidationIssue("binding.resource.missing", f"missing runtime resource binding: {resource_id}", "resources")
            )
        elif resource.content_sha256.lower() != digest:
            issues.append(
                ValidationIssue(
                    "binding.resource.sha256_mismatch",
                    f"runtime binding digest does not match PromptRef: {resource_id}",
                    "resources",
                )
            )
    for resource_id in sorted(set(resources) - set(referenced)):
        issues.append(
            ValidationIssue(
                "binding.resource.unreferenced",
                f"runtime binding contains an unreferenced resource: {resource_id}",
                "resources",
            )
        )
    for index, resource in enumerate(binding.resources):
        path = f"resources[{index}]"
        if not _is_logical_id(resource.resource_id):
            issues.append(ValidationIssue("binding.resource.id.invalid", "resource_id must be a logical ID", path))
        if not Path(resource.absolute_path).is_absolute():
            issues.append(
                ValidationIssue("binding.resource.path.absolute", "resource path must be absolute", f"{path}.absolute_path")
            )
        if not is_sha256(resource.content_sha256):
            issues.append(
                ValidationIssue("binding.resource.sha256.invalid", "resource content_sha256 is invalid", f"{path}.content_sha256")
            )
    if issues:
        raise WorkflowValidationError(issues)


def validate_bound_resource_files(
    definition: WorkflowDefinition,
    binding: WorkflowRuntimeBinding,
) -> None:
    """Verify binding structure and the bytes currently present on disk.

    Call this immediately before dispatch. A successful earlier check is not a
    substitute for execution-time Prompt seal verification.
    """

    validate_runtime_binding(definition, binding)
    issues: list[ValidationIssue] = []
    for index, resource in enumerate(binding.resources):
        path = Path(resource.absolute_path)
        location = f"resources[{index}].absolute_path"
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            issues.append(
                ValidationIssue(
                    "binding.resource.file_unreadable",
                    f"bound resource cannot be read: {type(exc).__name__}",
                    location,
                )
            )
            continue
        if not path.is_file():
            issues.append(
                ValidationIssue("binding.resource.file_required", "bound resource must be a file", location)
            )
            continue
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            issues.append(
                ValidationIssue("binding.resource.file_unstable", "bound resource changed while being read", location)
            )
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != resource.content_sha256.lower():
            issues.append(
                ValidationIssue(
                    "binding.resource.file_sha256_mismatch",
                    f"bound resource bytes do not match content_sha256: {resource.resource_id}",
                    location,
                )
            )
    if issues:
        raise WorkflowValidationError(issues)
