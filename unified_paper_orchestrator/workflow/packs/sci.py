from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ...figure_prompt_integrity import FIGURE_PROMPT_FILES, FIGURE_PROMPT_SHA256
from ...journal_author_prompt_integrity import (
    JOURNAL_AUTHOR_PROMPT_FILES,
    JOURNAL_AUTHOR_PROMPT_SHA256,
)
from ...writing_prompt_integrity import (
    TUNED_WRITING_PROMPT_FILES,
    TUNED_WRITING_PROMPT_SHA256,
)
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


SCI_PACK_ID = "builtin.sci"
SCI_PACK_VERSION = 1
SCI_PACK_CREATED_AT = "2026-08-05T00:00:00Z"
SCI_WRITING_WORKFLOW_ID = "builtin.sci.writing"
SCI_FIGURES_WORKFLOW_ID = "builtin.sci.figures"
SCI_JOURNAL_AUTHOR_WORKFLOW_ID = "builtin.sci.journal-author"
SCI_SMART_SUBMISSION_WORKFLOW_ID = "builtin.sci.smart-submission"

SCI_JOB_SCHEMA = "upo.sci.job.v1"
SCI_PROGRAM_STATE_SCHEMA = "upo.sci.smart-submission.program-state.v1"
SCI_SUBMISSION_EVIDENCE_SCHEMA = "upo.sci.submission-evidence.v1"

_WRITING_TOKEN_LIMITS: Mapping[int, int] = MappingProxyType(
    {
        1: 1_000_000,
        2: 2_000_000,
        3: 3_000_000,
        4: 3_000_000,
        5: 4_000_000,
        6: 4_000_000,
        7: 4_000_000,
        8: 4_000_000,
        9: 4_000_000,
        10: 4_000_000,
    }
)

_WRITING_NAMES: Mapping[int, str] = MappingProxyType(
    {
        1: "Find one research idea",
        2: "Collect references",
        3: "Write TeX draft",
        4: "Upgrade submission draft",
        5: "Complete figures, tables and results",
        6: "Clean verified submission draft",
        7: "Review round 1",
        8: "Revise after review round 1",
        9: "Submission maturity cleanup",
        10: "Final submission hardening",
    }
)

_FIGURE_NAMES: Mapping[int, str] = MappingProxyType(
    {
        1: "Research reference board",
        2: "Make figure plan",
        3: "Generate figures and submission package",
    }
)


def _artifact(
    artifact_id: str,
    media_type: str,
    *,
    schema_ref: str = "",
    required: bool = True,
    description: str = "",
) -> ArtifactSpec:
    return ArtifactSpec(
        artifact_id,
        media_type=media_type,
        schema_ref=schema_ref,
        required=required,
        description=description,
    )


_JOB = _artifact("job", "application/json", schema_ref=SCI_JOB_SCHEMA)
_RESEARCH_PROFILE = _artifact(
    "research_profile",
    "text/markdown",
    description="Portable input corresponding to research_profile.md.",
)
_SOURCE_TEX = _artifact("source_tex", "application/x-tex")

_WRITING_OUTPUTS: Mapping[int, tuple[ArtifactSpec, ...]] = MappingProxyType(
    {
        1: (
            _artifact(
                "idea_decision_card",
                "text/markdown",
                schema_ref="upo.sci.writing.step-01.v1",
                description="ideas/{paper_id}.md",
            ),
        ),
        2: (
            _artifact("references", "text/markdown", schema_ref="upo.sci.writing.step-02.v1"),
            _artifact("references_bib", "application/x-bibtex"),
            _artifact("literature_map", "text/markdown"),
            _artifact("novelty_check", "text/markdown"),
        ),
        3: (_artifact("draft_v0", "application/x-tex", schema_ref="upo.sci.writing.step-03.v1"),),
        4: (_artifact("draft_v1", "application/x-tex", schema_ref="upo.sci.writing.step-04.v1"),),
        5: (_artifact("draft_v2", "application/x-tex", schema_ref="upo.sci.writing.step-05.v1"),),
        6: (_artifact("draft_v3", "application/x-tex", schema_ref="upo.sci.writing.step-06.v1"),),
        7: (_artifact("review_round_1", "text/markdown", schema_ref="upo.sci.writing.step-07.v1"),),
        8: (_artifact("draft_v4", "application/x-tex", schema_ref="upo.sci.writing.step-08.v1"),),
        9: (_artifact("draft_v5", "application/x-tex", schema_ref="upo.sci.writing.step-09.v1"),),
        10: (_artifact("draft_v6", "application/x-tex", schema_ref="upo.sci.writing.step-10.v1"),),
    }
)

_WRITING_PATHS: Mapping[int, tuple[str, ...]] = MappingProxyType(
    {
        1: ("ideas/{paper_id}.md",),
        2: (
            "refs/{paper_id}/references.md",
            "refs/{paper_id}/references.bib",
            "refs/{paper_id}/literature_map.md",
            "refs/{paper_id}/novelty_check.md",
        ),
        3: ("papers/{paper_id}/draft_v0.tex",),
        4: ("papers/{paper_id}/draft_v1.tex",),
        5: ("papers/{paper_id}/draft_v2.tex",),
        6: ("papers/{paper_id}/draft_v3.tex",),
        7: ("papers/{paper_id}/review_round_1.md",),
        8: ("papers/{paper_id}/draft_v4.tex",),
        9: ("papers/{paper_id}/draft_v5.tex",),
        10: ("papers/{paper_id}/draft_v6.tex",),
    }
)

_FIGURE_OUTPUTS: Mapping[int, tuple[ArtifactSpec, ...]] = MappingProxyType(
    {
        1: (
            _artifact("reference_board", "text/markdown", schema_ref="upo.sci.figures.step-01.v1"),
            _artifact("reference_images_manifest", "text/markdown", required=False),
            _artifact("missing_reference_images", "text/markdown", required=False),
        ),
        2: (
            _artifact("figure_plan", "text/markdown", schema_ref="upo.sci.figures.step-02.v1"),
            _artifact("figure_prompts", "text/markdown"),
            _artifact(
                "figure_briefs_manifest",
                "application/json",
                schema_ref="upo.sci.figures.briefs-manifest.v1",
                description="Adapter-owned manifest for drafts/figure*_brief.md.",
            ),
        ),
        3: (
            _artifact(
                "submission_manifest",
                "application/json",
                schema_ref="upo.sci.figures.submission-manifest.v1",
            ),
            _artifact("submit_tex", "application/x-tex"),
            _artifact("compiled_pdf", "application/pdf"),
            _artifact(
                "figure_assets_manifest",
                "application/json",
                schema_ref="upo.sci.figures.assets-manifest.v1",
            ),
        ),
    }
)

_FIGURE_PATHS: Mapping[int, tuple[str, ...]] = MappingProxyType(
    {
        1: (
            "papers/{paper_id}/reference_board.md",
            "papers/{paper_id}/reference_images_manifest.md (optional)",
            "papers/{paper_id}/missing_reference_images.md (optional)",
        ),
        2: (
            "papers/{paper_id}/figure_plan.md",
            "papers/{paper_id}/figure_prompts.md",
            "papers/{paper_id}/drafts/figure*_brief.md (one or more)",
        ),
        3: (
            "papers/{paper_id}/submit/submission_manifest.json",
            "submission_manifest.submit_tex",
            "submission_manifest.compiled_pdf",
            "submission_manifest.figures[].final_pdf",
        ),
    }
)

_JOURNAL_INPUTS = (
    _JOB,
    _artifact("work_manifest", "application/json", schema_ref="upo.sci.journal-author.work-manifest.v1"),
    _artifact("author_info", "text/markdown", schema_ref="upo.sci.journal-author.author-info.v1"),
    _artifact("submission_metadata_fingerprint", "application/json"),
    _artifact("journal_preferences", "text/markdown", required=False),
)

_JOURNAL_OUTPUTS = (
    _artifact("journal", "text/markdown", schema_ref="upo.sci.journal-author.journal.v1"),
    _artifact("main_tex", "application/x-tex"),
    _artifact("cover_letter", "text/markdown"),
    _artifact("declarations", "text/markdown"),
    _artifact("submission_checklist", "text/markdown"),
    _artifact("file_manifest", "text/markdown"),
    _artifact("final_risk_report", "text/markdown"),
    _artifact("package_metadata_fingerprint", "application/json"),
    _artifact("main_pdf", "application/pdf", required=False),
)


def _profile(*, token_limit: int | None) -> ExecutionProfile:
    # P4 describes the sealed legacy baseline; it is deliberately not a direct
    # Codex dispatch profile. The legacy control envelope and the current model
    # catalogue must resolve/freeze the omitted effort and unlimited-token
    # semantics before a future native cutover.
    return ExecutionProfile(model="gpt-5.5", reasoning_effort="", token_limit=token_limit)


def _retry_policy() -> RetryPolicy:
    # The legacy runtime owns retry/reconciliation. Generic retries here would
    # duplicate dispatch and are unsafe for externally side-effecting stages.
    return RetryPolicy(max_attempts=1)


def _session_policy(group: str) -> SessionPolicy:
    return SessionPolicy(session_group=group, reuse_within_run=True, reuse_on_retry=True)


def _resource_id(stage: str, key: str | int) -> str:
    suffix = f"{key:02d}" if isinstance(key, int) and not isinstance(key, bool) else str(key).lower()
    return f"{SCI_PACK_ID}.{stage}.prompt.{suffix}"


def _workflow_input_bindings(specs: Sequence[ArtifactSpec]) -> tuple[ArtifactBinding, ...]:
    return tuple(
        ArtifactBinding(spec.artifact_id, ArtifactSourceKind.WORKFLOW_INPUT, spec.artifact_id)
        for spec in specs
    )


def _upstream_bindings(
    specs: Sequence[ArtifactSpec],
    source_node_id: str,
) -> tuple[ArtifactBinding, ...]:
    return tuple(
        ArtifactBinding(
            spec.artifact_id,
            ArtifactSourceKind.NODE_OUTPUT,
            spec.artifact_id,
            source_node_id,
        )
        for spec in specs
    )


def _linear_edges(node_ids: Sequence[str]) -> tuple[EdgeDefinition, ...]:
    return tuple(
        EdgeDefinition(f"{left}-to-{right}", left, right)
        for left, right in zip(node_ids, node_ids[1:])
    )


def _workflow_output_bindings(
    specs: Sequence[ArtifactSpec],
    source_node_id: str,
) -> tuple[ArtifactBinding, ...]:
    return tuple(
        ArtifactBinding(
            spec.artifact_id,
            ArtifactSourceKind.NODE_OUTPUT,
            spec.artifact_id,
            source_node_id,
        )
        for spec in specs
    )


def _codex_metadata(
    *,
    stage: str,
    legacy_step: str | int,
    validator_ref: str,
    legacy_artifacts: Sequence[str],
) -> dict[str, Any]:
    return {
        "pack_id": SCI_PACK_ID,
        "legacy_stage": stage,
        "legacy_step": legacy_step,
        "prompt_seal_source": "p0-baseline",
        "delivery": "legacy-control-envelope",
        "validator_ref": validator_ref,
        "legacy_artifacts": list(legacy_artifacts),
        "legacy_runtime_owns_retry": True,
        "requires_runtime_freeze": True,
        "native_dispatch_ready": False,
        "production_writer": False,
    }


def sci_writing_definition() -> WorkflowDefinition:
    nodes: list[NodeDefinition] = []
    node_ids = [f"writing-{step:02d}" for step in range(1, 11)]
    for step, node_id in enumerate(node_ids, start=1):
        inputs: list[ArtifactSpec] = [_JOB]
        bindings: list[ArtifactBinding] = list(_workflow_input_bindings((_JOB,)))
        if step == 1:
            inputs.append(_RESEARCH_PROFILE)
            bindings.extend(_workflow_input_bindings((_RESEARCH_PROFILE,)))
        elif step == 8:
            inputs.extend(_WRITING_OUTPUTS[6])
            bindings.extend(_upstream_bindings(_WRITING_OUTPUTS[6], node_ids[5]))
            inputs.extend(_WRITING_OUTPUTS[7])
            bindings.extend(_upstream_bindings(_WRITING_OUTPUTS[7], node_ids[6]))
        else:
            inputs.extend(_WRITING_OUTPUTS[step - 1])
            bindings.extend(_upstream_bindings(_WRITING_OUTPUTS[step - 1], node_ids[step - 2]))
        validator = "validate_writing_step2_contract" if step == 2 else "validate_writing_step_output"
        nodes.append(
            NodeDefinition(
                node_id=node_id,
                name=_WRITING_NAMES[step],
                node_type=NodeType.CODEX,
                display_order=step * 10,
                prompt_ref=PromptRef(
                    _resource_id("writing", step),
                    TUNED_WRITING_PROMPT_SHA256[step].lower(),
                    "p0-2026-08-01",
                ),
                execution_profile=_profile(token_limit=_WRITING_TOKEN_LIMITS[step]),
                retry_policy=_retry_policy(),
                session_policy=_session_policy("sci-writing"),
                inputs=tuple(inputs),
                input_bindings=tuple(bindings),
                outputs=_WRITING_OUTPUTS[step],
                metadata=_codex_metadata(
                    stage="writing",
                    legacy_step=step,
                    validator_ref=validator,
                    legacy_artifacts=_WRITING_PATHS[step],
                ),
            )
        )
    definition = WorkflowDefinition(
        workflow_id=SCI_WRITING_WORKFLOW_ID,
        name="SCI writing (10 steps)",
        description="Read-only SCI writing contract migrated from the sealed 0.12.0b3 baseline.",
        inputs=(_JOB, _RESEARCH_PROFILE),
        outputs=_WRITING_OUTPUTS[10],
        output_bindings=_workflow_output_bindings(_WRITING_OUTPUTS[10], node_ids[-1]),
        nodes=tuple(nodes),
        edges=_linear_edges(node_ids),
        metadata={
            "pack_id": SCI_PACK_ID,
            "pack_version": SCI_PACK_VERSION,
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "legacy_stage": "writing",
            "session_boundary": "one-fixed-thread-per-paper",
            "pre_dispatch_gates": {
                "3": "validate_writing_step2_contract",
                "6": "validate_writing_evidence-and-step5-trust-policy",
            },
        },
    )
    definition.validate()
    return definition


def sci_figures_definition() -> WorkflowDefinition:
    node_ids = [f"figures-{step:02d}" for step in (1, 2, 3)]
    nodes: list[NodeDefinition] = []
    for step, node_id in enumerate(node_ids, start=1):
        inputs: list[ArtifactSpec] = [_JOB, _SOURCE_TEX]
        bindings: list[ArtifactBinding] = list(_workflow_input_bindings((_JOB, _SOURCE_TEX)))
        if step > 1:
            inputs.extend(_FIGURE_OUTPUTS[step - 1])
            bindings.extend(_upstream_bindings(_FIGURE_OUTPUTS[step - 1], node_ids[step - 2]))
        nodes.append(
            NodeDefinition(
                node_id=node_id,
                name=_FIGURE_NAMES[step],
                node_type=NodeType.CODEX,
                display_order=step * 10,
                prompt_ref=PromptRef(
                    _resource_id("figures", step),
                    FIGURE_PROMPT_SHA256[step].lower(),
                    "p0-2026-08-01",
                ),
                execution_profile=_profile(token_limit=None),
                retry_policy=_retry_policy(),
                session_policy=_session_policy("sci-figures"),
                inputs=tuple(inputs),
                input_bindings=tuple(bindings),
                outputs=_FIGURE_OUTPUTS[step],
                metadata=_codex_metadata(
                    stage="figures",
                    legacy_step=step,
                    validator_ref="validate_figure_step",
                    legacy_artifacts=_FIGURE_PATHS[step],
                ),
            )
        )
    definition = WorkflowDefinition(
        workflow_id=SCI_FIGURES_WORKFLOW_ID,
        name="SCI figures (3 steps)",
        description="Read-only SCI figure contract migrated from the sealed 0.12.0b3 baseline.",
        inputs=(_JOB, _SOURCE_TEX),
        outputs=_FIGURE_OUTPUTS[3],
        output_bindings=_workflow_output_bindings(_FIGURE_OUTPUTS[3], node_ids[-1]),
        nodes=tuple(nodes),
        edges=_linear_edges(node_ids),
        metadata={
            "pack_id": SCI_PACK_ID,
            "pack_version": SCI_PACK_VERSION,
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "legacy_stage": "figures",
            "session_boundary": "one-fixed-thread-per-paper",
            "optional_start_at_legacy_step": 2,
        },
    )
    definition.validate()
    return definition


def sci_journal_author_definition() -> WorkflowDefinition:
    node_id = "journal-author-02-03"
    node = NodeDefinition(
        node_id=node_id,
        name="Choose journal and build author submission package",
        node_type=NodeType.CODEX,
        display_order=10,
        prompt_ref=PromptRef(
            _resource_id("journal-author", "combined"),
            JOURNAL_AUTHOR_PROMPT_SHA256["combined"].lower(),
            "p0-2026-08-01",
        ),
        execution_profile=_profile(token_limit=None),
        retry_policy=_retry_policy(),
        session_policy=_session_policy("sci-journal-author"),
        inputs=_JOURNAL_INPUTS,
        input_bindings=_workflow_input_bindings(_JOURNAL_INPUTS),
        outputs=_JOURNAL_OUTPUTS,
        metadata=_codex_metadata(
            stage="journal-author",
            legacy_step="2-3",
            validator_ref="validate_journal_author_output",
            legacy_artifacts=(
                "papers/{paper_id}/journal.md",
                "papers/{paper_id}/package/main.tex",
                "papers/{paper_id}/package/cover_letter.md",
                "papers/{paper_id}/package/declarations.md",
                "papers/{paper_id}/package/submission_checklist.md",
                "papers/{paper_id}/package/file_manifest.md",
                "papers/{paper_id}/package/final_risk_report.md",
                "papers/{paper_id}/package/package_metadata_fingerprint.json",
                "papers/{paper_id}/package/main.pdf (required when READY_TO_SUBMIT)",
            ),
        ),
    )
    definition = WorkflowDefinition(
        workflow_id=SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
        name="SCI journal and author package (combined)",
        description="Read-only combined journal-author contract from the sealed 0.12.0b3 baseline.",
        inputs=_JOURNAL_INPUTS,
        outputs=_JOURNAL_OUTPUTS,
        output_bindings=_workflow_output_bindings(_JOURNAL_OUTPUTS, node_id),
        nodes=(node,),
        edges=(),
        metadata={
            "pack_id": SCI_PACK_ID,
            "pack_version": SCI_PACK_VERSION,
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "legacy_stage": "journal-author",
            "legacy_mode": "combined",
            "session_boundary": "one-fixed-thread-per-paper",
            "required_control_prompt_resource": "builtin.sci.journal-author.prompt.scheduler",
        },
    )
    definition.validate()
    return definition


def sci_smart_submission_definition() -> WorkflowDefinition:
    state_snapshot = _artifact(
        "program_state_snapshot",
        "application/json",
        schema_ref=SCI_PROGRAM_STATE_SCHEMA,
    )
    submission_evidence = _artifact(
        "submission_evidence",
        "application/json",
        schema_ref=SCI_SUBMISSION_EVIDENCE_SCHEMA,
        required=False,
    )
    node = NodeDefinition(
        node_id="smart-submission-program",
        name="Smart submission program state machine",
        node_type=NodeType.PLUGIN,
        display_order=10,
        retry_policy=RetryPolicy(max_attempts=1),
        inputs=(_JOB,),
        input_bindings=_workflow_input_bindings((_JOB,)),
        outputs=(state_snapshot, submission_evidence),
        implementation_ref="smart-submission/program-runtime-v1",
        metadata={
            "pack_id": SCI_PACK_ID,
            "legacy_stage": "smart_submission",
            "external_state_machine": True,
            "production_writer": False,
            "session_owner": "plugin",
            "unknown_side_effect_policy": "reconcile-never-auto-retry",
            "program_control_schema": "smart-submission-program-control/v1",
            "program_task_prompt_version": "smart-submission-program-v1",
            "sdk_prompt_version": "smart-submission-sdk-browser-v1",
            "sdk_journal_schema": "unified-paper-orchestrator/smart-submission-sdk-dispatch/v1",
            "sdk_runtime_schema": "unified-paper-orchestrator/smart-submission-sdk-runtime/v1",
            "smart_prompt_version": 9,
            "smart_state_version": 7,
            "timer_command": "smart-submission-program-tick",
            "entrypoints": {
                "start": "unified_paper_orchestrator.smart_submission.start_smart_submission_program_runtime",
                "tick": "unified_paper_orchestrator.smart_submission.run_smart_submission_program_tick",
                "run_once": "unified_paper_orchestrator.smart_submission_sdk_runtime.default_smart_submission_sdk_runtime.run_once",
                "plan": "unified_paper_orchestrator.smart_submission.smart_submission_program_plan",
            },
        },
    )
    definition = WorkflowDefinition(
        workflow_id=SCI_SMART_SUBMISSION_WORKFLOW_ID,
        name="SCI smart submission plugin",
        description="Plugin contract only; the existing Smart Submission state machine remains authoritative.",
        inputs=(_JOB,),
        outputs=(state_snapshot, submission_evidence),
        output_bindings=_workflow_output_bindings((state_snapshot, submission_evidence), node.node_id),
        nodes=(node,),
        edges=(),
        metadata={
            "pack_id": SCI_PACK_ID,
            "pack_version": SCI_PACK_VERSION,
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "legacy_stage": "smart_submission",
        },
    )
    definition.validate()
    return definition


def sci_prompt_seal_manifest() -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    manifest: dict[str, dict[str, dict[str, Any]]] = {
        "writing": {
            f"{step:02d}": {
                "resource_id": _resource_id("writing", step),
                "relative_path": f"prompts/{TUNED_WRITING_PROMPT_FILES[step]}",
                "sha256": TUNED_WRITING_PROMPT_SHA256[step].lower(),
                "active": True,
            }
            for step in range(1, 11)
        },
        "figures": {
            f"{step:02d}": {
                "resource_id": _resource_id("figures", step),
                "relative_path": f"prompts/{FIGURE_PROMPT_FILES[step]}",
                "sha256": FIGURE_PROMPT_SHA256[step].lower(),
                "active": step in {1, 2, 3},
            }
            for step in sorted(FIGURE_PROMPT_FILES)
        },
        "journal-author": {
            name: {
                "resource_id": _resource_id("journal-author", name),
                "relative_path": JOURNAL_AUTHOR_PROMPT_FILES[name].replace("\\", "/"),
                "sha256": JOURNAL_AUTHOR_PROMPT_SHA256[name].lower(),
                "active": name in {"scheduler", "combined"},
                "node_prompt": name == "combined",
            }
            for name in JOURNAL_AUTHOR_PROMPT_FILES
        },
    }
    return MappingProxyType({stage: MappingProxyType(items) for stage, items in manifest.items()})


def build_sci_workflow_pack() -> WorkflowPack:
    definitions = (
        sci_writing_definition(),
        sci_figures_definition(),
        sci_journal_author_definition(),
        sci_smart_submission_definition(),
    )
    versions = tuple(
        WorkflowVersion.from_definition(
            definition,
            version=1,
            created_at=SCI_PACK_CREATED_AT,
            status=WorkflowVersionStatus.PUBLISHED,
        )
        for definition in definitions
    )
    return WorkflowPack(
        pack_id=SCI_PACK_ID,
        version=SCI_PACK_VERSION,
        name="Built-in SCI workflow pack",
        description="Sealed writing, figures, journal-author and Smart Submission contracts.",
        workflows=versions,
        metadata={
            "builtin": True,
            "readonly": True,
            "cloneable": True,
            "prompt_seals": {
                stage: {name: dict(record) for name, record in entries.items()}
                for stage, entries in sci_prompt_seal_manifest().items()
            },
        },
    )


def register_sci_workflow_pack(store: WorkflowStore) -> tuple[dict[str, object], ...]:
    return build_sci_workflow_pack().register(store)


def build_sci_runtime_bindings(
    *,
    writing_root: str | Path,
    figure_root: str | Path,
    journal_author_root: str | Path,
) -> Mapping[str, WorkflowRuntimeBinding]:
    roots = {
        "writing": Path(writing_root).expanduser().absolute(),
        "figures": Path(figure_root).expanduser().absolute(),
        "journal-author": Path(journal_author_root).expanduser().absolute(),
    }
    pack = build_sci_workflow_pack()
    resources: dict[str, tuple[ResourceBinding, ...]] = {
        SCI_WRITING_WORKFLOW_ID: tuple(
            ResourceBinding(
                _resource_id("writing", step),
                str(roots["writing"] / "prompts" / TUNED_WRITING_PROMPT_FILES[step]),
                TUNED_WRITING_PROMPT_SHA256[step].lower(),
            )
            for step in range(1, 11)
        ),
        SCI_FIGURES_WORKFLOW_ID: tuple(
            ResourceBinding(
                _resource_id("figures", step),
                str(roots["figures"] / "prompts" / FIGURE_PROMPT_FILES[step]),
                FIGURE_PROMPT_SHA256[step].lower(),
            )
            for step in (1, 2, 3)
        ),
        SCI_JOURNAL_AUTHOR_WORKFLOW_ID: (
            ResourceBinding(
                _resource_id("journal-author", "combined"),
                str(roots["journal-author"] / JOURNAL_AUTHOR_PROMPT_FILES["combined"]),
                JOURNAL_AUTHOR_PROMPT_SHA256["combined"].lower(),
            ),
        ),
        SCI_SMART_SUBMISSION_WORKFLOW_ID: (),
    }
    result: dict[str, WorkflowRuntimeBinding] = {}
    for version in pack.workflows:
        binding = WorkflowRuntimeBinding(
            definition_sha256=version.definition_sha256,
            resources=resources[version.workflow_id],
            binding_id=f"{version.workflow_id}.local",
        )
        binding.validate(version.definition)
        result[version.workflow_id] = binding
    return MappingProxyType(result)


__all__ = [
    "SCI_FIGURES_WORKFLOW_ID",
    "SCI_JOURNAL_AUTHOR_WORKFLOW_ID",
    "SCI_PACK_ID",
    "SCI_PACK_VERSION",
    "SCI_SMART_SUBMISSION_WORKFLOW_ID",
    "SCI_WRITING_WORKFLOW_ID",
    "build_sci_runtime_bindings",
    "build_sci_workflow_pack",
    "register_sci_workflow_pack",
    "sci_figures_definition",
    "sci_journal_author_definition",
    "sci_prompt_seal_manifest",
    "sci_smart_submission_definition",
    "sci_writing_definition",
]
