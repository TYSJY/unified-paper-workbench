from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from ..contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    EdgeDefinition,
    ExecutionProfile,
    NodeDefinition,
    NodeType,
    PromptRef,
    ResourceBinding,
    RetryPolicy,
    SessionPolicy,
    WorkflowDefinition,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from ..store import WorkflowStore
from .base import WorkflowPack


UNDERGRADUATE_PACK_ID = "builtin.undergraduate-thesis"
UNDERGRADUATE_PACK_VERSION = 1
UNDERGRADUATE_PACK_CREATED_AT = "2026-08-05T00:00:00Z"
UNDERGRADUATE_THESIS_WORKFLOW_ID = "builtin.undergraduate-thesis.evidence-first"

UNDERGRADUATE_PROMPT_FILES: Mapping[str, str] = MappingProxyType(
    {
        "research-scope": "research_scope.md",
        "evidence-register": "evidence_register.md",
        "methodology-plan": "methodology_plan.md",
        "analysis-plan": "analysis_plan.md",
        "draft-assembly": "draft_assembly.md",
        "integrity-review": "integrity_review.md",
    }
)

UNDERGRADUATE_PROMPT_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "research-scope": "178120b8910e22bf0bb1271631ac0febbb0da42c1c148f740a8ed3750f353d0a",
        "evidence-register": "8d62c11389da83c16eb898c5bd45aba1d6e399c6fa7a0b9fd4f9e26b59e52459",
        "methodology-plan": "cca74bd4e50d3b0096a3bd366fbb1c4ad1664ab702298703647e1d223bdb8dc0",
        "analysis-plan": "aa8bb99d9dc7b0deef5c66fe966a06d2697eaa6951254685f2e2e74f1058646c",
        "draft-assembly": "de995b3805f611158247406244da1817a6333a84241ed116e17e87d065c71d1e",
        "integrity-review": "a956320debcc79bf89da1028990fcac23823af9c6af0b22a5fd2caae9ba3fd86",
    }
)


def _artifact(
    artifact_id: str,
    media_type: str,
    schema_ref: str,
    *,
    required: bool = True,
    description: str = "",
) -> ArtifactSpec:
    return ArtifactSpec(
        artifact_id=artifact_id,
        media_type=media_type,
        schema_ref=schema_ref,
        required=required,
        description=description,
    )


_THESIS_BRIEF = _artifact(
    "thesis_brief",
    "application/json",
    "upo.undergraduate.thesis-brief.v1",
    description="Topic, constraints and optional language; language defaults to zh-CN.",
)
_EVIDENCE_BUNDLE = _artifact(
    "evidence_bundle",
    "application/json",
    "upo.undergraduate.evidence-bundle.v1",
    description="User-supplied, traceable evidence and source metadata.",
)
_INSTITUTION_REQUIREMENTS = _artifact(
    "institution_requirements",
    "text/markdown",
    "upo.undergraduate.institution-requirements.v1",
    required=False,
    description="Optional institution, department or supervisor requirements.",
)
_RESEARCH_SCOPE = _artifact(
    "research_scope",
    "text/markdown",
    "upo.undergraduate.research-scope.v1",
)
_EVIDENCE_REGISTER = _artifact(
    "evidence_register",
    "application/json",
    "upo.undergraduate.evidence-register.v1",
)
_METHODOLOGY_PLAN = _artifact(
    "methodology_plan",
    "text/markdown",
    "upo.undergraduate.methodology-plan.v1",
)
_ANALYSIS_PLAN = _artifact(
    "analysis_plan",
    "text/markdown",
    "upo.undergraduate.analysis-plan.v1",
)
_THESIS_DRAFT = _artifact(
    "thesis_draft",
    "text/markdown",
    "upo.undergraduate.thesis-draft.v1",
)
_REVIEWED_THESIS = _artifact(
    "reviewed_thesis",
    "text/markdown",
    "upo.undergraduate.reviewed-thesis.v1",
)
_INTEGRITY_REPORT = _artifact(
    "integrity_report",
    "application/json",
    "upo.undergraduate.integrity-report.v1",
)


def _resource_id(stage: str) -> str:
    return f"{UNDERGRADUATE_PACK_ID}.prompt.{stage}"


def _workflow_input_bindings(specs: Sequence[ArtifactSpec]) -> tuple[ArtifactBinding, ...]:
    return tuple(
        ArtifactBinding(
            target_artifact_id=spec.artifact_id,
            source_kind=ArtifactSourceKind.WORKFLOW_INPUT,
            source_artifact_id=spec.artifact_id,
        )
        for spec in specs
    )


def _upstream_bindings(
    source_node_id: str,
    specs: Sequence[ArtifactSpec],
) -> tuple[ArtifactBinding, ...]:
    return tuple(
        ArtifactBinding(
            target_artifact_id=spec.artifact_id,
            source_kind=ArtifactSourceKind.NODE_OUTPUT,
            source_artifact_id=spec.artifact_id,
            source_node_id=source_node_id,
        )
        for spec in specs
    )


def _workflow_output_bindings(
    source_node_id: str,
    specs: Sequence[ArtifactSpec],
) -> tuple[ArtifactBinding, ...]:
    return _upstream_bindings(source_node_id, specs)


def _execution_profile() -> ExecutionProfile:
    return ExecutionProfile(
        model="gpt-5.6-sol",
        reasoning_effort="high",
        token_limit=400_000,
        timeout_seconds=3_600,
        allowed_tools=("read", "write"),
        network_access=False,
        parameters={"temperature": 0.1},
    )


def _session_policy() -> SessionPolicy:
    return SessionPolicy(
        session_group="undergraduate-thesis",
        reuse_within_run=True,
        reuse_on_retry=False,
    )


def _node_metadata(stage: str, declared_output_files: Mapping[str, str]) -> dict[str, Any]:
    return {
        "pack_id": UNDERGRADUATE_PACK_ID,
        "stage": stage,
        "declared_output_files": dict(declared_output_files),
        "evidence_policy": "bound-inputs-only",
        "language_policy": "brief-language-or-zh-CN",
        "forbid_fabricated_citations": True,
        "forbid_fabricated_data": True,
        "forbid_fabricated_ethics_approval": True,
        "unresolved_gap_policy": "mark-explicitly",
        "native_dispatch_ready": True,
        "production_writer": False,
    }


def _codex_node(
    *,
    node_id: str,
    name: str,
    display_order: int,
    stage: str,
    inputs: Sequence[ArtifactSpec],
    input_bindings: Sequence[ArtifactBinding],
    outputs: Sequence[ArtifactSpec],
    declared_output_files: Mapping[str, str],
) -> NodeDefinition:
    return NodeDefinition(
        node_id=node_id,
        name=name,
        node_type=NodeType.CODEX,
        display_order=display_order,
        prompt_ref=PromptRef(
            resource_id=_resource_id(stage),
            content_sha256=UNDERGRADUATE_PROMPT_SHA256[stage],
            version="p7-2026-08-05",
        ),
        execution_profile=_execution_profile(),
        retry_policy=RetryPolicy(max_attempts=1),
        session_policy=_session_policy(),
        inputs=tuple(inputs),
        input_bindings=tuple(input_bindings),
        outputs=tuple(outputs),
        metadata=_node_metadata(stage, declared_output_files),
    )


def undergraduate_thesis_definition() -> WorkflowDefinition:
    workflow_inputs = (_THESIS_BRIEF, _EVIDENCE_BUNDLE, _INSTITUTION_REQUIREMENTS)
    nodes = (
        _codex_node(
            node_id="research-scope",
            name="研究范围与问题界定",
            display_order=10,
            stage="research-scope",
            inputs=workflow_inputs,
            input_bindings=_workflow_input_bindings(workflow_inputs),
            outputs=(_RESEARCH_SCOPE,),
            declared_output_files={"research_scope": "undergraduate/01_research_scope.md"},
        ),
        _codex_node(
            node_id="evidence-register",
            name="证据登记与文献映射",
            display_order=20,
            stage="evidence-register",
            inputs=(_THESIS_BRIEF, _EVIDENCE_BUNDLE, _RESEARCH_SCOPE),
            input_bindings=(
                *_workflow_input_bindings((_THESIS_BRIEF, _EVIDENCE_BUNDLE)),
                *_upstream_bindings("research-scope", (_RESEARCH_SCOPE,)),
            ),
            outputs=(_EVIDENCE_REGISTER,),
            declared_output_files={"evidence_register": "undergraduate/02_evidence_register.json"},
        ),
        _codex_node(
            node_id="methodology-plan",
            name="方法与伦理计划",
            display_order=30,
            stage="methodology-plan",
            inputs=(_THESIS_BRIEF, _RESEARCH_SCOPE, _EVIDENCE_REGISTER),
            input_bindings=(
                *_workflow_input_bindings((_THESIS_BRIEF,)),
                *_upstream_bindings("research-scope", (_RESEARCH_SCOPE,)),
                *_upstream_bindings("evidence-register", (_EVIDENCE_REGISTER,)),
            ),
            outputs=(_METHODOLOGY_PLAN,),
            declared_output_files={"methodology_plan": "undergraduate/03_methodology_plan.md"},
        ),
        _codex_node(
            node_id="analysis-plan",
            name="分析与论证计划",
            display_order=40,
            stage="analysis-plan",
            inputs=(_THESIS_BRIEF, _EVIDENCE_REGISTER, _METHODOLOGY_PLAN),
            input_bindings=(
                *_workflow_input_bindings((_THESIS_BRIEF,)),
                *_upstream_bindings("evidence-register", (_EVIDENCE_REGISTER,)),
                *_upstream_bindings("methodology-plan", (_METHODOLOGY_PLAN,)),
            ),
            outputs=(_ANALYSIS_PLAN,),
            declared_output_files={"analysis_plan": "undergraduate/04_analysis_plan.md"},
        ),
        _codex_node(
            node_id="draft-assembly",
            name="证据受控的草稿组装",
            display_order=50,
            stage="draft-assembly",
            inputs=(
                _THESIS_BRIEF,
                _INSTITUTION_REQUIREMENTS,
                _RESEARCH_SCOPE,
                _EVIDENCE_REGISTER,
                _METHODOLOGY_PLAN,
                _ANALYSIS_PLAN,
            ),
            input_bindings=(
                *_workflow_input_bindings((_THESIS_BRIEF, _INSTITUTION_REQUIREMENTS)),
                *_upstream_bindings("research-scope", (_RESEARCH_SCOPE,)),
                *_upstream_bindings("evidence-register", (_EVIDENCE_REGISTER,)),
                *_upstream_bindings("methodology-plan", (_METHODOLOGY_PLAN,)),
                *_upstream_bindings("analysis-plan", (_ANALYSIS_PLAN,)),
            ),
            outputs=(_THESIS_DRAFT,),
            declared_output_files={"thesis_draft": "undergraduate/05_thesis_draft.md"},
        ),
        _codex_node(
            node_id="integrity-review",
            name="完整性与学术诚信复核",
            display_order=60,
            stage="integrity-review",
            inputs=(
                _THESIS_BRIEF,
                _INSTITUTION_REQUIREMENTS,
                _EVIDENCE_REGISTER,
                _METHODOLOGY_PLAN,
                _ANALYSIS_PLAN,
                _THESIS_DRAFT,
            ),
            input_bindings=(
                *_workflow_input_bindings((_THESIS_BRIEF, _INSTITUTION_REQUIREMENTS)),
                *_upstream_bindings("evidence-register", (_EVIDENCE_REGISTER,)),
                *_upstream_bindings("methodology-plan", (_METHODOLOGY_PLAN,)),
                *_upstream_bindings("analysis-plan", (_ANALYSIS_PLAN,)),
                *_upstream_bindings("draft-assembly", (_THESIS_DRAFT,)),
            ),
            outputs=(_REVIEWED_THESIS, _INTEGRITY_REPORT),
            declared_output_files={
                "reviewed_thesis": "undergraduate/06_reviewed_thesis.md",
                "integrity_report": "undergraduate/06_integrity_report.json",
            },
        ),
    )
    edges = tuple(
        EdgeDefinition(
            edge_id=f"{source.node_id}-to-{target.node_id}",
            source_node_id=source.node_id,
            target_node_id=target.node_id,
        )
        for source, target in zip(nodes, nodes[1:])
    )
    definition = WorkflowDefinition(
        workflow_id=UNDERGRADUATE_THESIS_WORKFLOW_ID,
        name="证据优先的本科论文工作流程",
        description="六节点本科论文规划、证据登记、写作和学术诚信复核模板。",
        inputs=workflow_inputs,
        outputs=(_REVIEWED_THESIS, _INTEGRITY_REPORT),
        output_bindings=_workflow_output_bindings(
            "integrity-review",
            (_REVIEWED_THESIS, _INTEGRITY_REPORT),
        ),
        nodes=nodes,
        edges=edges,
        metadata={
            "pack_id": UNDERGRADUATE_PACK_ID,
            "pack_version": UNDERGRADUATE_PACK_VERSION,
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "domain": "undergraduate-thesis",
            "evidence_policy": "bound-inputs-only",
            "language_policy": "brief-language-or-zh-CN",
            "session_boundary": "one-session-group-per-run",
            "production_writer": False,
        },
    )
    definition.validate()
    return definition


def undergraduate_prompt_manifest() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {
            stage: MappingProxyType(
                {
                    "resource_id": _resource_id(stage),
                    "relative_path": f"prompts/undergraduate/{UNDERGRADUATE_PROMPT_FILES[stage]}",
                    "sha256": UNDERGRADUATE_PROMPT_SHA256[stage],
                    "active": True,
                }
            )
            for stage in UNDERGRADUATE_PROMPT_FILES
        }
    )


def _validate_nonempty_markdown(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="strict")
    if not text.strip() or "\x00" in text:
        raise ValueError("undergraduate Markdown artifact must be non-empty UTF-8 text")


def _validate_json_object(
    path: Path,
    *,
    schema: str,
    fields: frozenset[str],
    list_fields: tuple[str, ...],
) -> None:
    payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError(f"{schema} must contain exactly its declared top-level fields")
    if payload.get("schema") != schema or not isinstance(payload.get("language"), str):
        raise ValueError(f"{schema} identity and language are required")
    if any(not isinstance(payload.get(field), list) for field in list_fields):
        raise ValueError(f"{schema} collection fields must be arrays")


def _validate_evidence_register(path: Path) -> None:
    _validate_json_object(
        path,
        schema="upo.undergraduate.evidence-register.v1",
        fields=frozenset(
            {
                "schema",
                "language",
                "research_questions",
                "sources",
                "claim_evidence_map",
                "gaps",
                "warnings",
            }
        ),
        list_fields=("research_questions", "sources", "claim_evidence_map", "gaps", "warnings"),
    )


def _validate_integrity_report(path: Path) -> None:
    _validate_json_object(
        path,
        schema="upo.undergraduate.integrity-report.v1",
        fields=frozenset(
            {
                "schema",
                "language",
                "verdict",
                "checks",
                "unresolved_gaps",
                "required_human_actions",
            }
        ),
        list_fields=("checks", "unresolved_gaps", "required_human_actions"),
    )
    payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    if payload["verdict"] not in {"needs_evidence", "needs_revision", "ready_for_human_review"}:
        raise ValueError("integrity report verdict is invalid")


def undergraduate_schema_validators() -> Mapping[str, Callable[[Path], None]]:
    """Trusted validators required by the pack's declared output schemas."""

    return MappingProxyType(
        {
            _RESEARCH_SCOPE.schema_ref: _validate_nonempty_markdown,
            _EVIDENCE_REGISTER.schema_ref: _validate_evidence_register,
            _METHODOLOGY_PLAN.schema_ref: _validate_nonempty_markdown,
            _ANALYSIS_PLAN.schema_ref: _validate_nonempty_markdown,
            _THESIS_DRAFT.schema_ref: _validate_nonempty_markdown,
            _REVIEWED_THESIS.schema_ref: _validate_nonempty_markdown,
            _INTEGRITY_REPORT.schema_ref: _validate_integrity_report,
        }
    )


def build_undergraduate_workflow_pack() -> WorkflowPack:
    definition = undergraduate_thesis_definition()
    version = WorkflowVersion.from_definition(
        definition,
        version=1,
        created_at=UNDERGRADUATE_PACK_CREATED_AT,
        status=WorkflowVersionStatus.PUBLISHED,
    )
    return WorkflowPack(
        pack_id=UNDERGRADUATE_PACK_ID,
        version=UNDERGRADUATE_PACK_VERSION,
        name="Built-in undergraduate thesis workflow pack",
        description="Evidence-first, source-traceable undergraduate thesis workflow.",
        workflows=(version,),
        metadata={
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "prompt_seals": {
                stage: dict(record)
                for stage, record in undergraduate_prompt_manifest().items()
            },
        },
    )


def register_undergraduate_workflow_pack(
    store: WorkflowStore,
) -> tuple[dict[str, object], ...]:
    return build_undergraduate_workflow_pack().register(store)


def build_undergraduate_runtime_binding(
    *,
    prompt_root: str | Path | None = None,
) -> WorkflowRuntimeBinding:
    root = (
        Path(prompt_root).expanduser().absolute()
        if prompt_root is not None
        else Path(__file__).resolve().parent / "prompts" / "undergraduate"
    )
    version = build_undergraduate_workflow_pack().workflow(UNDERGRADUATE_THESIS_WORKFLOW_ID)
    binding = WorkflowRuntimeBinding(
        definition_sha256=version.definition_sha256,
        resources=tuple(
            ResourceBinding(
                resource_id=_resource_id(stage),
                absolute_path=str((root / filename).absolute()),
                content_sha256=UNDERGRADUATE_PROMPT_SHA256[stage],
            )
            for stage, filename in UNDERGRADUATE_PROMPT_FILES.items()
        ),
        binding_id=f"{UNDERGRADUATE_THESIS_WORKFLOW_ID}.package-assets",
        metadata={"source": "package-assets"},
    )
    binding.validate(version.definition)
    binding.validate_files(version.definition)
    return binding


__all__ = [
    "UNDERGRADUATE_PACK_ID",
    "UNDERGRADUATE_PACK_VERSION",
    "UNDERGRADUATE_PROMPT_FILES",
    "UNDERGRADUATE_PROMPT_SHA256",
    "UNDERGRADUATE_THESIS_WORKFLOW_ID",
    "build_undergraduate_runtime_binding",
    "build_undergraduate_workflow_pack",
    "register_undergraduate_workflow_pack",
    "undergraduate_prompt_manifest",
    "undergraduate_schema_validators",
    "undergraduate_thesis_definition",
]
