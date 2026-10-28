from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from unified_paper_orchestrator.workflow import (
    CONDITION_LANGUAGE,
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    EdgeDefinition,
    EdgeType,
    ExecutionProfile,
    JoinPolicy,
    NodeDefinition,
    NodeRun,
    NodeRunStatus,
    NodeType,
    PromptRef,
    ResourceBinding,
    ValidationMode,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRuntimeBinding,
    WorkflowValidationError,
    WorkflowVersion,
    RetryPolicy,
    SessionPolicy,
    canonical_json,
)


PROMPT_SHA256 = "a" * 64


def _linear_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="portable.three-step",
        name="Portable three-step workflow",
        inputs=(ArtifactSpec("topic", media_type="text/plain"),),
        nodes=(
            NodeDefinition(
                node_id="research",
                name="Research",
                node_type=NodeType.CODEX,
                display_order=10,
                prompt_ref=PromptRef("prompts/research", PROMPT_SHA256, "1"),
                inputs=(ArtifactSpec("topic", media_type="text/plain"),),
                input_bindings=(
                    ArtifactBinding(
                        "topic",
                        ArtifactSourceKind.WORKFLOW_INPUT,
                        "topic",
                    ),
                ),
                outputs=(ArtifactSpec("notes", media_type="text/markdown"),),
            ),
            NodeDefinition(
                node_id="draft",
                name="Draft",
                node_type=NodeType.CODEX,
                display_order=20,
                prompt_ref=PromptRef("prompts/draft", PROMPT_SHA256, "1"),
                inputs=(ArtifactSpec("notes", media_type="text/markdown"),),
                input_bindings=(
                    ArtifactBinding(
                        "notes",
                        ArtifactSourceKind.NODE_OUTPUT,
                        "notes",
                        "research",
                    ),
                ),
                outputs=(ArtifactSpec("manuscript", media_type="text/markdown"),),
            ),
            NodeDefinition(
                node_id="verify",
                name="Verify",
                node_type=NodeType.DETERMINISTIC,
                display_order=30,
                inputs=(ArtifactSpec("manuscript", media_type="text/markdown"),),
                input_bindings=(
                    ArtifactBinding(
                        "manuscript",
                        ArtifactSourceKind.NODE_OUTPUT,
                        "manuscript",
                        "draft",
                    ),
                ),
                outputs=(ArtifactSpec("verified", media_type="application/json"),),
                implementation_ref="artifact.verify",
            ),
        ),
        edges=(
            EdgeDefinition("research-to-draft", "research", "draft"),
            EdgeDefinition("draft-to-verify", "draft", "verify"),
        ),
        outputs=(ArtifactSpec("verified", media_type="application/json"),),
        output_bindings=(
            ArtifactBinding("verified", ArtifactSourceKind.NODE_OUTPUT, "verified", "verify"),
        ),
        metadata={"locale": "zh-CN"},
    )


def test_three_node_linear_workflow_validates() -> None:
    definition = _linear_workflow()

    definition.validate()


def test_definition_round_trip_and_canonical_sha256_are_stable() -> None:
    definition = _linear_workflow()
    restored = WorkflowDefinition.from_dict(json.loads(definition.canonical_json()))
    reordered = replace(
        definition,
        nodes=tuple(reversed(definition.nodes)),
        edges=tuple(reversed(definition.edges)),
    )

    assert restored.to_dict() == definition.to_dict()
    assert restored.sha256() == definition.sha256()
    assert reordered.sha256() == definition.sha256()


def test_portable_definition_hash_is_independent_from_runtime_paths(tmp_path) -> None:
    definition = _linear_workflow()
    first = WorkflowRuntimeBinding(
        definition_sha256=definition.sha256(),
        resources=(
            ResourceBinding("prompts/research", str((tmp_path / "one" / "research.md").resolve()), PROMPT_SHA256),
            ResourceBinding("prompts/draft", str((tmp_path / "one" / "draft.md").resolve()), PROMPT_SHA256),
        ),
    )
    second = WorkflowRuntimeBinding(
        definition_sha256=definition.sha256(),
        resources=(
            ResourceBinding("prompts/research", str((tmp_path / "two" / "research.md").resolve()), PROMPT_SHA256),
            ResourceBinding("prompts/draft", str((tmp_path / "two" / "draft.md").resolve()), PROMPT_SHA256),
        ),
    )

    first.validate(definition)
    second.validate(definition)

    assert first.definition_sha256 == second.definition_sha256 == definition.sha256()
    assert first.sha256() != second.sha256()
    assert WorkflowRuntimeBinding.from_dict(first.to_dict()).sha256() == first.sha256()
    assert replace(first, binding_id="stored-binding-1").sha256() == first.sha256()
    assert replace(first, metadata={"display_name": "Local binding"}).sha256() == first.sha256()


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (
            lambda value: replace(value, nodes=(value.nodes[0], replace(value.nodes[1], node_id="research"), value.nodes[2])),
            "node.id.duplicate",
        ),
        (
            lambda value: replace(
                value,
                edges=(replace(value.edges[0], target_node_id="missing"), value.edges[1]),
            ),
            "edge.target.missing",
        ),
        (
            lambda value: replace(
                value,
                nodes=value.nodes
                + (
                    NodeDefinition(
                        "isolated",
                        "Isolated",
                        NodeType.DETERMINISTIC,
                        40,
                    ),
                ),
            ),
            "graph.entry.count",
        ),
        (
            lambda value: replace(value, edges=(value.edges[0],)),
            "graph.exit.count",
        ),
    ],
)
def test_invalid_graph_shapes_are_rejected(mutation, error_code: str) -> None:
    with pytest.raises(WorkflowValidationError, match=error_code):
        mutation(_linear_workflow()).validate()


def _branching_graph() -> WorkflowDefinition:
    nodes = (
        NodeDefinition("start", "Start", NodeType.DETERMINISTIC, 10, implementation_ref="system.start"),
        NodeDefinition("plugin-a", "Plugin A", NodeType.PLUGIN, 20, implementation_ref="plugins.lookup-a"),
        NodeDefinition("plugin-b", "Plugin B", NodeType.PLUGIN, 30, implementation_ref="plugins.lookup-b"),
        NodeDefinition(
            "compose",
            "Compose subflow",
            NodeType.SUBWORKFLOW,
            40,
            join_policy=JoinPolicy.ANY,
            implementation_ref="workflows.compose@1",
        ),
    )
    return WorkflowDefinition(
        "future.graph",
        "Future graph contract",
        nodes,
        (
            EdgeDefinition(
                "choose-a",
                "start",
                "plugin-a",
                EdgeType.CONDITION,
                {"==": [{"var": "input.route"}, "a"]},
                CONDITION_LANGUAGE,
            ),
            EdgeDefinition(
                "choose-b",
                "start",
                "plugin-b",
                EdgeType.CONDITION,
                {"==": [{"var": "input.route"}, "b"]},
                CONDITION_LANGUAGE,
            ),
            EdgeDefinition("a-compose", "plugin-a", "compose"),
            EdgeDefinition("b-compose", "plugin-b", "compose"),
            EdgeDefinition(
                "bounded-retry",
                "compose",
                "plugin-a",
                EdgeType.LOOP,
                {"var": "result.retry"},
                CONDITION_LANGUAGE,
                2,
            ),
        ),
    )


def test_linear_mode_rejects_branch_and_loop_but_graph_contract_accepts_them() -> None:
    graph = _branching_graph()

    graph.validate(ValidationMode.GRAPH)
    restored = WorkflowDefinition.from_dict(graph.to_dict())
    assert restored.to_dict() == graph.to_dict()
    assert {node.node_type for node in restored.nodes} >= {NodeType.PLUGIN, NodeType.SUBWORKFLOW}
    assert {edge.edge_type for edge in restored.edges} >= {EdgeType.CONDITION, EdgeType.LOOP}

    with pytest.raises(WorkflowValidationError, match="linear.edge_type"):
        graph.validate(ValidationMode.LINEAR)


def test_loop_edge_without_limit_is_rejected() -> None:
    graph = _branching_graph()
    edges = tuple(
        replace(edge, max_traversals=None) if edge.edge_type is EdgeType.LOOP else edge
        for edge in graph.edges
    )

    with pytest.raises(WorkflowValidationError, match="edge.loop.limit.required"):
        replace(graph, edges=edges).validate(ValidationMode.GRAPH)


@pytest.mark.parametrize("limit", [True, 1.5, 10_001])
def test_loop_limit_is_a_bounded_strict_integer(limit) -> None:
    graph = _branching_graph()
    edges = tuple(
        replace(edge, max_traversals=limit) if edge.edge_type is EdgeType.LOOP else edge
        for edge in graph.edges
    )

    with pytest.raises(WorkflowValidationError, match="edge.loop.limit.required"):
        replace(graph, edges=edges).validate(ValidationMode.GRAPH)


def test_success_edge_cannot_smuggle_an_ignored_condition() -> None:
    definition = _linear_workflow()
    first = replace(
        definition.edges[0],
        condition={"var": "ignored"},
        condition_language=CONDITION_LANGUAGE,
    )

    with pytest.raises(WorkflowValidationError, match="edge.condition.unexpected"):
        replace(definition, edges=(first, definition.edges[1])).validate()


@pytest.mark.parametrize(
    ("binding", "error_code"),
    [
        (
            ArtifactBinding("bad", ArtifactSourceKind.WORKFLOW_INPUT, "missing"),
            "artifact.binding.workflow_input_missing",
        ),
        (
            ArtifactBinding("bad", ArtifactSourceKind.NODE_OUTPUT, "manuscript", "draft"),
            "artifact.binding.not_upstream",
        ),
        (
            ArtifactBinding("bad", ArtifactSourceKind.NODE_OUTPUT, "missing", "research"),
            "artifact.binding.source_output_missing",
        ),
    ],
)
def test_artifact_bindings_only_reference_existing_upstream_sources(binding, error_code: str) -> None:
    definition = _linear_workflow()
    first = replace(definition.nodes[0], input_bindings=definition.nodes[0].input_bindings + (binding,))

    with pytest.raises(WorkflowValidationError, match=error_code):
        replace(definition, nodes=(first,) + definition.nodes[1:]).validate()


def test_workflow_version_freezes_and_verifies_definition_digest() -> None:
    definition = _linear_workflow()
    version = WorkflowVersion.from_definition(definition, version=1, created_at="2026-08-05T00:00:00Z")

    version.validate()
    restored = WorkflowVersion.from_dict(version.to_dict())
    restored.validate()
    assert restored.to_dict() == version.to_dict()

    with pytest.raises(ValueError, match="definition_sha256"):
        replace(version, definition_sha256="0" * 64).validate()


def test_workflow_and_node_run_statuses_round_trip_stably() -> None:
    workflow_run = WorkflowRun(
        run_id="run-1",
        workflow_version_id="portable.three-step@1",
        definition_sha256="b" * 64,
        binding_sha256="c" * 64,
        status=WorkflowRunStatus.PAUSED,
        created_at="2026-08-05T00:00:00Z",
        inputs={"topic": {"uri": "artifact://topic"}},
    )
    node_run = NodeRun(
        node_run_id="node-run-1",
        workflow_run_id="run-1",
        node_id="draft",
        attempt=2,
        status=NodeRunStatus.RETRY_WAIT,
        created_at="2026-08-05T00:00:00Z",
        outputs={"manuscript": {"sha256": "d" * 64}},
    )

    assert WorkflowRun.from_dict(workflow_run.to_dict()).to_dict() == workflow_run.to_dict()
    assert NodeRun.from_dict(node_run.to_dict()).to_dict() == node_run.to_dict()


def test_prompt_ref_rejects_absolute_path_in_portable_definition(tmp_path) -> None:
    definition = _linear_workflow()
    prompt = replace(
        definition.nodes[0].prompt_ref,
        resource_id=str((tmp_path / "prompt.md").resolve()),
    )
    first = replace(definition.nodes[0], prompt_ref=prompt)

    with pytest.raises(WorkflowValidationError, match="prompt.resource_id.invalid"):
        replace(definition, nodes=(first,) + definition.nodes[1:]).validate()


def test_absolute_paths_cannot_hide_in_portable_metadata(tmp_path) -> None:
    definition = replace(
        _linear_workflow(),
        metadata={"executor_path": str((tmp_path / "worker.exe").resolve())},
    )

    with pytest.raises(WorkflowValidationError, match="workflow.path.not_portable"):
        definition.validate()


def test_required_branch_artifact_must_dominate_its_consumer() -> None:
    graph = _branching_graph()
    plugin_a = replace(graph.nodes[1], outputs=(ArtifactSpec("lookup"),))
    compose = replace(
        graph.nodes[3],
        inputs=(ArtifactSpec("lookup"),),
        input_bindings=(
            ArtifactBinding("lookup", ArtifactSourceKind.NODE_OUTPUT, "lookup", "plugin-a"),
        ),
    )

    with pytest.raises(WorkflowValidationError, match="artifact.binding.not_dominating"):
        replace(graph, nodes=(graph.nodes[0], plugin_a, graph.nodes[2], compose)).validate(ValidationMode.GRAPH)


def test_canonical_json_rejects_non_standard_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"invalid": float("nan")})


def test_retry_numeric_normalization_preserves_definition_hash() -> None:
    definition = _linear_workflow()
    first = replace(
        definition.nodes[0],
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=1,
            backoff_multiplier=2,
            max_delay_seconds=5,
        ),
    )
    definition = replace(definition, nodes=(first,) + definition.nodes[1:])

    restored = WorkflowDefinition.from_dict(definition.to_dict())
    assert restored.sha256() == definition.sha256()


def test_definition_snapshot_is_deeply_immutable() -> None:
    external = {"nested": {"enabled": True}, "items": [1, 2]}
    definition = replace(_linear_workflow(), metadata=external)
    digest = definition.sha256()

    external["nested"]["enabled"] = False
    external["items"].append(3)
    exported = definition.to_dict()
    exported["metadata"]["nested"]["enabled"] = False

    assert definition.sha256() == digest
    assert definition.to_dict()["metadata"] == {"nested": {"enabled": True}, "items": [1, 2]}


@pytest.mark.parametrize("parameters", [{1: "integer"}, {1: "integer", "1": "string"}])
def test_json_mapping_rejects_non_string_keys_without_collision(parameters) -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        ExecutionProfile(parameters=parameters)


def test_wire_contract_rejects_unknown_missing_and_coerced_fields() -> None:
    payload = _linear_workflow().to_dict()
    with pytest.raises(ValueError, match="unknown keys"):
        WorkflowDefinition.from_dict({**payload, "future_field": True})
    without_schema = dict(payload)
    without_schema.pop("schema")
    with pytest.raises(ValueError, match="missing keys: schema"):
        WorkflowDefinition.from_dict(without_schema)

    profile = _linear_workflow().nodes[0].execution_profile.to_dict()
    profile["network_access"] = "false"
    with pytest.raises(TypeError, match="must be a boolean"):
        type(_linear_workflow().nodes[0].execution_profile).from_dict(profile)

    run_payload = WorkflowRun(
        run_id="run-strict",
        workflow_version_id="portable.three-step@1",
        definition_sha256="b" * 64,
        binding_sha256="c" * 64,
        created_at="2026-08-05T00:00:00Z",
    ).to_dict()
    run_payload.pop("run_id")
    with pytest.raises(ValueError, match="missing keys: run_id"):
        WorkflowRun.from_dict(run_payload)


def test_definition_deserialization_rejects_invalid_semantics(tmp_path) -> None:
    unsupported = {**_linear_workflow().to_dict(), "schema": "evil/workflow/v9"}
    with pytest.raises(WorkflowValidationError, match="workflow.schema.unsupported"):
        WorkflowDefinition.from_dict(unsupported)

    nonportable = _linear_workflow().to_dict()
    nonportable["metadata"] = {"runtime_path": str((tmp_path / "worker.exe").resolve())}
    with pytest.raises(WorkflowValidationError, match="workflow.path.not_portable"):
        WorkflowDefinition.from_dict(nonportable)


def test_conflicting_prompt_resource_digests_are_rejected() -> None:
    definition = _linear_workflow()
    second = replace(
        definition.nodes[1],
        prompt_ref=PromptRef("prompts/research", "f" * 64, "2"),
    )

    with pytest.raises(WorkflowValidationError, match="prompt.resource_id.conflict"):
        replace(definition, nodes=(definition.nodes[0], second, definition.nodes[2])).validate()


def test_runtime_binding_rejects_ambient_unreferenced_resources(tmp_path) -> None:
    definition = _linear_workflow()
    binding = WorkflowRuntimeBinding(
        definition_sha256=definition.sha256(),
        resources=(
            ResourceBinding("prompts/research", str((tmp_path / "research.md").resolve()), PROMPT_SHA256),
            ResourceBinding("prompts/draft", str((tmp_path / "draft.md").resolve()), PROMPT_SHA256),
            ResourceBinding("prompts/extra", str((tmp_path / "extra.md").resolve()), PROMPT_SHA256),
        ),
    )

    with pytest.raises(WorkflowValidationError, match="binding.resource.unreferenced"):
        binding.validate(definition)


def test_runtime_binding_can_verify_bound_file_bytes(tmp_path) -> None:
    research = tmp_path / "research.md"
    draft = tmp_path / "draft.md"
    research.write_bytes(b"research")
    draft.write_bytes(b"draft")
    definition = _linear_workflow()
    research_digest = hashlib.sha256(research.read_bytes()).hexdigest()
    draft_digest = hashlib.sha256(draft.read_bytes()).hexdigest()
    nodes = (
        replace(definition.nodes[0], prompt_ref=PromptRef("prompts/research", research_digest, "1")),
        replace(definition.nodes[1], prompt_ref=PromptRef("prompts/draft", draft_digest, "1")),
        definition.nodes[2],
    )
    definition = replace(definition, nodes=nodes)
    binding = WorkflowRuntimeBinding(
        definition_sha256=definition.sha256(),
        resources=(
            ResourceBinding("prompts/research", str(research.resolve()), research_digest),
            ResourceBinding("prompts/draft", str(draft.resolve()), draft_digest),
        ),
    )

    binding.validate_files(definition)
    draft.write_bytes(b"tampered")
    with pytest.raises(WorkflowValidationError, match="file_sha256_mismatch"):
        binding.validate_files(definition)


def test_artifact_binding_requires_declared_compatible_target_spec() -> None:
    definition = _linear_workflow()
    draft = replace(
        definition.nodes[1],
        inputs=(ArtifactSpec("notes", media_type="application/pdf"),),
    )

    with pytest.raises(WorkflowValidationError, match="media_type_mismatch"):
        replace(definition, nodes=(definition.nodes[0], draft, definition.nodes[2])).validate()


def test_required_artifacts_cannot_bind_to_optional_sources() -> None:
    definition = _linear_workflow()

    optional_workflow_input = replace(definition.inputs[0], required=False)
    with pytest.raises(WorkflowValidationError, match="required_from_optional"):
        replace(definition, inputs=(optional_workflow_input,)).validate()

    optional_node_output = replace(definition.nodes[0].outputs[0], required=False)
    research = replace(definition.nodes[0], outputs=(optional_node_output,))
    with pytest.raises(WorkflowValidationError, match="required_from_optional"):
        replace(definition, nodes=(research,) + definition.nodes[1:]).validate()

    copied_topic = ArtifactSpec("topic-copy", media_type="text/plain")
    copied_binding = ArtifactBinding(
        "topic-copy",
        ArtifactSourceKind.WORKFLOW_INPUT,
        "topic",
    )
    with pytest.raises(WorkflowValidationError, match="required_from_optional"):
        replace(
            definition,
            inputs=(optional_workflow_input,),
            outputs=definition.outputs + (copied_topic,),
            output_bindings=definition.output_bindings + (copied_binding,),
        ).validate()

    optional_verified = replace(definition.nodes[2].outputs[0], required=False)
    verify = replace(definition.nodes[2], outputs=(optional_verified,))
    with pytest.raises(WorkflowValidationError, match="required_from_optional"):
        replace(definition, nodes=definition.nodes[:2] + (verify,)).validate()


def test_session_reuse_requires_explicit_group_and_codex_requires_prompt() -> None:
    definition = _linear_workflow()
    first = replace(
        definition.nodes[0],
        prompt_ref=None,
        session_policy=SessionPolicy(reuse_within_run=True),
    )

    with pytest.raises(WorkflowValidationError) as captured:
        replace(definition, nodes=(first,) + definition.nodes[1:]).validate()
    message = str(captured.value)
    assert "node.prompt.required" in message
    assert "session.group.required" in message


@pytest.mark.parametrize("status", list(WorkflowRunStatus))
def test_all_workflow_run_statuses_round_trip(status: WorkflowRunStatus) -> None:
    value = WorkflowRun(
        run_id=f"run-{status.value}",
        workflow_version_id="workflow@1",
        definition_sha256="a" * 64,
        binding_sha256="b" * 64,
        status=status,
        created_at="2026-08-05T00:00:00+00:00",
    )
    assert WorkflowRun.from_dict(value.to_dict()).status is status


@pytest.mark.parametrize("status", list(NodeRunStatus))
def test_all_node_run_statuses_round_trip(status: NodeRunStatus) -> None:
    value = NodeRun(
        node_run_id=f"node-run-{status.value}",
        workflow_run_id="run-1",
        node_id="node-1",
        status=status,
        created_at="2026-08-05T00:00:00+00:00",
    )
    assert NodeRun.from_dict(value.to_dict()).status is status


def test_malformed_run_snapshots_cannot_serialize() -> None:
    with pytest.raises(ValueError, match="definition_sha256"):
        WorkflowRun(
            workflow_version_id="workflow@1",
            definition_sha256="bad",
            binding_sha256="b" * 64,
            created_at="2026-08-05T00:00:00Z",
        ).to_dict()
    with pytest.raises(ValueError, match="attempt must be positive"):
        NodeRun(
            workflow_run_id="run-1",
            node_id="node-1",
            attempt=0,
            created_at="2026-08-05T00:00:00Z",
        ).to_dict()
    with pytest.raises(ValueError, match="include a timezone"):
        NodeRun(
            workflow_run_id="run-1",
            node_id="node-1",
            created_at="2026-08-05T00:00:00",
        ).to_dict()
