"""Built-in, offline examples for the generic workflow engine."""

from __future__ import annotations

from ..contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    EdgeDefinition,
    NodeDefinition,
    NodeType,
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from ..examples import (
    ACTION_PLAN_IMPLEMENTATION,
    NORMALIZE_REQUEST_IMPLEMENTATION,
    SUMMARY_IMPLEMENTATION,
)
from ..store import WorkflowStore
from .base import WorkflowPack


GENERIC_EXAMPLES_PACK_ID = "builtin.generic-examples"
GENERIC_EXAMPLES_PACK_VERSION = 1
GENERIC_EXAMPLES_PACK_CREATED_AT = "2026-08-05T00:00:00Z"
OFFLINE_DEMO_WORKFLOW_ID = "builtin.generic.offline-demo"


def offline_demo_definition() -> WorkflowDefinition:
    """Build a portable edge-ordered request-to-summary workflow."""

    request = ArtifactSpec(
        "request",
        media_type="application/json",
        description="A JSON object with goal, optional actions, and optional constraints.",
    )
    normalized_request = ArtifactSpec("normalized_request", media_type="application/json")
    action_plan = ArtifactSpec("action_plan", media_type="application/json")
    summary = ArtifactSpec("summary", media_type="text/markdown")

    normalize_node = NodeDefinition(
        node_id="normalize-request",
        name="Normalize request",
        node_type=NodeType.DETERMINISTIC,
        display_order=31,
        implementation_ref=NORMALIZE_REQUEST_IMPLEMENTATION,
        inputs=(request,),
        input_bindings=(
            ArtifactBinding("request", ArtifactSourceKind.WORKFLOW_INPUT, "request"),
        ),
        outputs=(normalized_request,),
        metadata={"runtime": "offline", "role": "normalizer"},
    )
    plan_node = NodeDefinition(
        node_id="build-action-plan",
        name="Build action plan",
        node_type=NodeType.DETERMINISTIC,
        display_order=12,
        implementation_ref=ACTION_PLAN_IMPLEMENTATION,
        inputs=(normalized_request,),
        input_bindings=(
            ArtifactBinding(
                "normalized_request",
                ArtifactSourceKind.NODE_OUTPUT,
                "normalized_request",
                "normalize-request",
            ),
        ),
        outputs=(action_plan,),
        metadata={"runtime": "offline", "role": "planner"},
    )
    summary_node = NodeDefinition(
        node_id="render-summary",
        name="Render Markdown summary",
        node_type=NodeType.DETERMINISTIC,
        display_order=23,
        implementation_ref=SUMMARY_IMPLEMENTATION,
        inputs=(action_plan,),
        input_bindings=(
            ArtifactBinding(
                "action_plan",
                ArtifactSourceKind.NODE_OUTPUT,
                "action_plan",
                "build-action-plan",
            ),
        ),
        outputs=(summary,),
        metadata={"runtime": "offline", "role": "renderer"},
    )
    definition = WorkflowDefinition(
        workflow_id=OFFLINE_DEMO_WORKFLOW_ID,
        name="Offline request planner",
        description="A generic local example that turns one JSON request into a Markdown summary.",
        inputs=(request,),
        outputs=(summary,),
        output_bindings=(
            ArtifactBinding(
                "summary",
                ArtifactSourceKind.NODE_OUTPUT,
                "summary",
                "render-summary",
            ),
        ),
        # Tuple and display ordering are intentionally different from the
        # executable order. Success edges are the semantic source of truth.
        nodes=(summary_node, normalize_node, plan_node),
        edges=(
            EdgeDefinition("normalize-to-plan", "normalize-request", "build-action-plan"),
            EdgeDefinition("plan-to-summary", "build-action-plan", "render-summary"),
        ),
        metadata={
            "builtin": True,
            "cloneable": True,
            "pack_id": GENERIC_EXAMPLES_PACK_ID,
            "pack_version": GENERIC_EXAMPLES_PACK_VERSION,
            "readonly": True,
            "runtime": "offline",
        },
    )
    definition.validate()
    return definition


def build_generic_examples_pack() -> WorkflowPack:
    definition = offline_demo_definition()
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=GENERIC_EXAMPLES_PACK_CREATED_AT,
        status=WorkflowVersionStatus.PUBLISHED,
    )
    return WorkflowPack(
        pack_id=GENERIC_EXAMPLES_PACK_ID,
        version=GENERIC_EXAMPLES_PACK_VERSION,
        name="Built-in generic offline examples",
        description="Small cloneable templates that run without external services.",
        workflows=(version,),
        metadata={
            "builtin": True,
            "cloneable": True,
            "readonly": True,
            "runtime": "offline",
        },
    )


def build_generic_examples_workflow_pack() -> WorkflowPack:
    """Compatibility-friendly explicit name matching other built-in pack builders."""

    return build_generic_examples_pack()


def register_generic_examples_pack(store: WorkflowStore) -> tuple[dict[str, object], ...]:
    return build_generic_examples_pack().register(store)


__all__ = [
    "GENERIC_EXAMPLES_PACK_ID",
    "GENERIC_EXAMPLES_PACK_VERSION",
    "OFFLINE_DEMO_WORKFLOW_ID",
    "build_generic_examples_pack",
    "build_generic_examples_workflow_pack",
    "offline_demo_definition",
    "register_generic_examples_pack",
]
