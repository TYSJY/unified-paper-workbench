from __future__ import annotations

from dataclasses import replace

import pytest

from unified_paper_orchestrator.workflow.contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    EdgeDefinition,
    ExecutionProfile,
    NodeDefinition,
    NodeType,
    PromptRef,
    SessionPolicy,
    ValidationMode,
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from unified_paper_orchestrator.workflow.linear_editor import (
    LinearWorkflowEditError,
    LinearWorkflowEditor,
)
from unified_paper_orchestrator.workflow.validation import WorkflowValidationError


NOW = "2026-08-05T12:00:00+00:00"
PROMPT_SHA = "a" * 64


def _definition(*, metadata: dict[str, object] | None = None) -> WorkflowDefinition:
    topic = ArtifactSpec("topic", media_type="text/plain")
    notes = ArtifactSpec("notes", media_type="text/markdown")
    manuscript = ArtifactSpec("manuscript", media_type="text/markdown")
    verified = ArtifactSpec("verified", media_type="application/json")
    return WorkflowDefinition(
        workflow_id="tests.custom-linear",
        name="Custom linear workflow",
        inputs=(topic,),
        outputs=(verified,),
        output_bindings=(
            ArtifactBinding("verified", ArtifactSourceKind.NODE_OUTPUT, "verified", "verify"),
        ),
        nodes=(
            NodeDefinition(
                node_id="research",
                name="Research",
                node_type=NodeType.CODEX,
                display_order=10,
                prompt_ref=PromptRef("prompts/research", PROMPT_SHA, "1"),
                inputs=(topic,),
                input_bindings=(
                    ArtifactBinding("topic", ArtifactSourceKind.WORKFLOW_INPUT, "topic"),
                ),
                outputs=(notes,),
            ),
            NodeDefinition(
                node_id="draft",
                name="Draft",
                node_type=NodeType.CODEX,
                display_order=20,
                prompt_ref=PromptRef("prompts/draft", PROMPT_SHA, "1"),
                inputs=(notes,),
                input_bindings=(
                    ArtifactBinding("notes", ArtifactSourceKind.NODE_OUTPUT, "notes", "research"),
                ),
                outputs=(manuscript,),
            ),
            NodeDefinition(
                node_id="verify",
                name="Verify",
                node_type=NodeType.DETERMINISTIC,
                display_order=30,
                implementation_ref="tests.verify",
                inputs=(manuscript,),
                input_bindings=(
                    ArtifactBinding("manuscript", ArtifactSourceKind.NODE_OUTPUT, "manuscript", "draft"),
                ),
                outputs=(verified,),
            ),
        ),
        edges=(
            EdgeDefinition("research-draft", "research", "draft"),
            EdgeDefinition("draft-verify", "draft", "verify"),
        ),
        metadata=metadata or {"builtin": False, "readonly": False},
    )


def _version(definition: WorkflowDefinition, *, version: int = 3) -> WorkflowVersion:
    return WorkflowVersion.from_definition(
        definition,
        version=version,
        created_at=NOW,
        status=WorkflowVersionStatus.PUBLISHED,
    )


def _pairs(definition: WorkflowDefinition) -> list[tuple[str, str]]:
    return [(edge.source_node_id, edge.target_node_id) for edge in definition.edges]


def _editor(definition: WorkflowDefinition | None = None) -> LinearWorkflowEditor:
    return LinearWorkflowEditor.from_definition(
        definition or _definition(),
        is_builtin=False,
        is_readonly=False,
    )


def test_topology_edits_are_copy_on_write_and_rebuild_only_success_edges() -> None:
    original = _definition()
    original_digest = original.sha256()
    editor = _editor(original)
    review = NodeDefinition(
        node_id="review",
        name="Human review",
        node_type=NodeType.HUMAN,
        display_order=999,
    )

    inserted = editor.add_node(review, index=2)
    moved = inserted.move_node("review", 3)
    restored = moved.remove_node("review")

    assert original.sha256() == original_digest
    assert editor.definition is not original
    assert [node.node_id for node in inserted.definition.nodes] == [
        "research",
        "draft",
        "review",
        "verify",
    ]
    assert [node.display_order for node in inserted.definition.nodes] == [10, 20, 30, 40]
    assert _pairs(inserted.definition) == [
        ("research", "draft"),
        ("draft", "review"),
        ("review", "verify"),
    ]
    assert all(edge.edge_type.value == "success" for edge in inserted.definition.edges)
    assert [node.node_id for node in moved.definition.nodes] == [
        "research",
        "draft",
        "verify",
        "review",
    ]
    assert [node.node_id for node in restored.definition.nodes] == ["research", "draft", "verify"]
    assert _pairs(restored.definition) == [("research", "draft"), ("draft", "verify")]


def test_artifact_dependencies_make_unsafe_remove_and_reorder_fail_closed() -> None:
    editor = _editor()

    with pytest.raises(WorkflowValidationError, match="source node does not exist"):
        editor.remove_node("draft")
    with pytest.raises(WorkflowValidationError, match="not upstream"):
        editor.move_node("draft", 2)

    assert [node.node_id for node in editor.definition.nodes] == ["research", "draft", "verify"]
    assert _pairs(editor.definition) == [("research", "draft"), ("draft", "verify")]


def test_update_node_safely_edits_executor_profile_prompt_artifacts_and_session() -> None:
    editor = _editor()
    profile = ExecutionProfile(
        model="gpt-custom",
        reasoning_effort="high",
        token_limit=9000,
        timeout_seconds=1200,
        allowed_tools=("read", "write"),
        parameters={"temperature": 0},
    )
    session = SessionPolicy(
        session_group="custom-drafting",
        reuse_within_run=True,
        reuse_on_retry=True,
    )
    prompt = PromptRef("user.prompts.draft", "b" * 64, "2")
    notes = ArtifactSpec("notes", media_type="text/markdown", description="Verified notes")
    manuscript = ArtifactSpec("manuscript", media_type="text/markdown", description="Draft output")

    updated = editor.update_node(
        "draft",
        name="Compose draft",
        execution_profile=profile,
        prompt_ref=prompt,
        inputs=(notes,),
        input_bindings=(
            ArtifactBinding("notes", ArtifactSourceKind.NODE_OUTPUT, "notes", "research"),
        ),
        outputs=(manuscript,),
        session_policy=session,
    ).update_node(
        "verify",
        node_type=NodeType.PLUGIN,
        implementation_ref="plugins.verify",
    )

    draft = next(node for node in updated.definition.nodes if node.node_id == "draft")
    verify = next(node for node in updated.definition.nodes if node.node_id == "verify")
    assert draft.name == "Compose draft"
    assert draft.execution_profile == profile
    assert draft.prompt_ref == prompt
    assert draft.inputs == (notes,)
    assert draft.outputs == (manuscript,)
    assert draft.session_policy == session
    assert verify.node_type is NodeType.PLUGIN
    assert verify.implementation_ref == "plugins.verify"
    assert editor.definition.nodes[1].prompt_ref != prompt


def test_workflow_artifact_interface_is_replaced_atomically() -> None:
    editor = _editor()
    topic = ArtifactSpec("topic", media_type="text/plain", description="Research topic")
    verified = ArtifactSpec("verified", media_type="application/json", description="Verification receipt")

    updated = editor.update_interface(
        name="Renamed workflow",
        inputs=(topic,),
        outputs=(verified,),
        output_bindings=(
            ArtifactBinding("verified", ArtifactSourceKind.NODE_OUTPUT, "verified", "verify"),
        ),
    )

    assert updated.definition.name == "Renamed workflow"
    assert updated.definition.inputs == (topic,)
    assert updated.definition.outputs == (verified,)
    assert editor.definition.name == "Custom linear workflow"


@pytest.mark.parametrize("metadata", [{"builtin": True, "cloneable": True}, {"readonly": True, "cloneable": True}])
def test_protected_definition_must_be_cloned_before_editing(metadata: dict[str, object]) -> None:
    protected = _definition(metadata=metadata)
    source_digest = protected.sha256()

    with pytest.raises(LinearWorkflowEditError, match="cloned before editing"):
        LinearWorkflowEditor.from_definition(
            protected,
            is_builtin=False,
            is_readonly=False,
        )

    clone = LinearWorkflowEditor.clone_as_custom(
        protected,
        workflow_id="custom.cloned-workflow",
        name="Editable clone",
    )
    edited = clone.add_node(
        NodeDefinition("approval", "Approval", NodeType.HUMAN, 500),
        index=3,
    )

    assert protected.sha256() == source_digest
    assert clone.definition.workflow_id == "custom.cloned-workflow"
    assert clone.definition.metadata["builtin"] is False
    assert clone.definition.metadata["readonly"] is False
    assert clone.definition.metadata["cloned_from"]["definition_sha256"] == source_digest
    assert [node.node_id for node in edited.definition.nodes][-1] == "approval"


def test_store_flags_also_prevent_direct_editing() -> None:
    definition = _definition(metadata={})

    with pytest.raises(LinearWorkflowEditError):
        LinearWorkflowEditor.from_definition(
            definition,
            is_builtin=True,
            is_readonly=False,
        )
    with pytest.raises(LinearWorkflowEditError):
        LinearWorkflowEditor.from_version(
            _version(definition),
            is_builtin=False,
            is_readonly=True,
        )


def test_replace_node_cannot_rename_stable_identity() -> None:
    editor = _editor()
    replacement = replace(editor.definition.nodes[1], node_id="renamed")

    with pytest.raises(LinearWorkflowEditError, match="node_id is immutable"):
        editor.replace_node("draft", replacement)


def test_materialize_creates_detached_draft_and_fresh_version_identity() -> None:
    source = _version(_definition())
    editor = LinearWorkflowEditor.from_version(
        source,
        is_builtin=False,
        is_readonly=False,
    )

    first = editor.materialize(created_at="2026-08-05T12:01:00+00:00")
    second = editor.materialize(created_at="2026-08-05T12:02:00+00:00")

    assert first.version == second.version == source.version + 1
    assert first.status is second.status is WorkflowVersionStatus.DRAFT
    assert first.version_id != second.version_id
    assert first.version_id != source.version_id
    assert first.definition is not editor.definition
    assert first.definition.sha256() == editor.definition.sha256()
    first.validate(mode=ValidationMode.LINEAR)
    assert source.status is WorkflowVersionStatus.PUBLISHED
    with pytest.raises(ValueError, match="at least 4"):
        editor.materialize(created_at=NOW, version=source.version)


def test_control_edges_define_semantic_order_when_display_order_is_stale() -> None:
    definition = _definition()
    nodes = tuple(
        replace(node, display_order={"research": 30, "draft": 10, "verify": 20}[node.node_id])
        for node in definition.nodes
    )
    stale_display_order = replace(definition, nodes=nodes)
    stale_display_order.validate(mode=ValidationMode.LINEAR)

    editor = _editor(stale_display_order)
    edited = editor.add_node(
        NodeDefinition("approval", "Approval", NodeType.HUMAN, 999),
        index=3,
    )

    assert [node.node_id for node in editor.definition.nodes] == ["research", "draft", "verify"]
    assert [node.node_id for node in edited.definition.nodes] == [
        "research",
        "draft",
        "verify",
        "approval",
    ]
    assert _pairs(edited.definition) == [
        ("research", "draft"),
        ("draft", "verify"),
        ("verify", "approval"),
    ]


def test_non_linear_definition_and_invalid_atomic_update_are_rejected() -> None:
    definition = _definition()
    nonlinear = WorkflowDefinition(
        workflow_id="tests.branching",
        name="Branching workflow",
        nodes=(
            NodeDefinition("start", "Start", NodeType.HUMAN, 10),
            NodeDefinition("left", "Left", NodeType.HUMAN, 20),
            NodeDefinition("right", "Right", NodeType.HUMAN, 30),
            NodeDefinition("finish", "Finish", NodeType.HUMAN, 40),
        ),
        edges=(
            EdgeDefinition("start-left", "start", "left"),
            EdgeDefinition("start-right", "start", "right"),
            EdgeDefinition("left-finish", "left", "finish"),
            EdgeDefinition("right-finish", "right", "finish"),
        ),
    )
    nonlinear.validate(mode=ValidationMode.GRAPH)

    with pytest.raises(WorkflowValidationError, match="linear"):
        LinearWorkflowEditor.from_definition(
            nonlinear,
            is_builtin=False,
            is_readonly=False,
        )

    editor = _editor(definition)
    with pytest.raises(WorkflowValidationError, match="prompt"):
        editor.update_node("draft", prompt_ref=None)
    assert editor.definition.sha256() == definition.sha256()
