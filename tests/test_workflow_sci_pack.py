from __future__ import annotations

from pathlib import Path

from unified_paper_orchestrator.figure_prompt_integrity import (
    FIGURE_PROMPT_FILES,
    FIGURE_PROMPT_SHA256,
)
from unified_paper_orchestrator.journal_author_prompt_integrity import (
    JOURNAL_AUTHOR_PROMPT_FILES,
    JOURNAL_AUTHOR_PROMPT_SHA256,
)
from unified_paper_orchestrator.writing_prompt_integrity import (
    TUNED_WRITING_PROMPT_FILES,
    TUNED_WRITING_PROMPT_SHA256,
)
from unified_paper_orchestrator.workflow import NodeType, WorkflowVersionStatus
from unified_paper_orchestrator.workflow.packs import (
    SCI_FIGURES_WORKFLOW_ID,
    SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
    SCI_SMART_SUBMISSION_WORKFLOW_ID,
    SCI_WRITING_WORKFLOW_ID,
    build_sci_runtime_bindings,
    build_sci_workflow_pack,
    register_sci_workflow_pack,
    sci_prompt_seal_manifest,
)
from unified_paper_orchestrator.workflow.store import WorkflowStore


def _nodes(workflow_id: str):
    return build_sci_workflow_pack().workflow(workflow_id).definition.nodes


def test_sci_pack_has_published_readonly_cloneable_stage_contracts() -> None:
    pack = build_sci_workflow_pack()

    assert pack.metadata["builtin"] is True
    assert pack.metadata["readonly"] is True
    assert pack.metadata["cloneable"] is True
    assert {item.workflow_id for item in pack.workflows} == {
        SCI_WRITING_WORKFLOW_ID,
        SCI_FIGURES_WORKFLOW_ID,
        SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
        SCI_SMART_SUBMISSION_WORKFLOW_ID,
    }
    assert all(item.status is WorkflowVersionStatus.PUBLISHED for item in pack.workflows)
    assert [len(_nodes(workflow_id)) for workflow_id in (
        SCI_WRITING_WORKFLOW_ID,
        SCI_FIGURES_WORKFLOW_ID,
        SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
        SCI_SMART_SUBMISSION_WORKFLOW_ID,
    )] == [10, 3, 1, 1]


def test_sci_codex_nodes_reference_exact_prompt_seals_without_prompt_bodies() -> None:
    expected = {
        SCI_WRITING_WORKFLOW_ID: [
            (f"builtin.sci.writing.prompt.{step:02d}", TUNED_WRITING_PROMPT_SHA256[step].lower())
            for step in range(1, 11)
        ],
        SCI_FIGURES_WORKFLOW_ID: [
            (f"builtin.sci.figures.prompt.{step:02d}", FIGURE_PROMPT_SHA256[step].lower())
            for step in (1, 2, 3)
        ],
        SCI_JOURNAL_AUTHOR_WORKFLOW_ID: [
            (
                "builtin.sci.journal-author.prompt.combined",
                JOURNAL_AUTHOR_PROMPT_SHA256["combined"].lower(),
            )
        ],
    }

    for workflow_id, prompt_seals in expected.items():
        nodes = _nodes(workflow_id)
        assert all(node.node_type is NodeType.CODEX for node in nodes)
        assert [
            (node.prompt_ref.resource_id, node.prompt_ref.content_sha256)
            for node in nodes
            if node.prompt_ref is not None
        ] == prompt_seals
        serialized = build_sci_workflow_pack().workflow(workflow_id).definition.canonical_json()
        assert "Enter `" not in serialized
        assert "D:\\\\" not in serialized
        assert ":\\\\" not in serialized

    assert set(FIGURE_PROMPT_FILES) == {1, 2, 3, 4}
    assert all("prompt.04" not in node.prompt_ref.resource_id for node in _nodes(SCI_FIGURES_WORKFLOW_ID))
    assert set(JOURNAL_AUTHOR_PROMPT_FILES) == {"scheduler", "import", "choose", "package", "combined"}


def test_pack_manifest_keeps_inactive_and_control_prompt_seals_in_regression_scope() -> None:
    manifest = sci_prompt_seal_manifest()

    assert len(manifest["writing"]) == 10
    assert len(manifest["figures"]) == 4
    assert len(manifest["journal-author"]) == 5
    assert manifest["figures"]["04"] == {
        "resource_id": "builtin.sci.figures.prompt.04",
        "relative_path": f"prompts/{FIGURE_PROMPT_FILES[4]}",
        "sha256": FIGURE_PROMPT_SHA256[4].lower(),
        "active": False,
    }
    assert manifest["journal-author"]["scheduler"]["active"] is True
    assert manifest["journal-author"]["scheduler"]["node_prompt"] is False
    for name, digest in JOURNAL_AUTHOR_PROMPT_SHA256.items():
        assert manifest["journal-author"][name]["sha256"] == digest.lower()


def test_sci_execution_profiles_and_session_boundaries_match_baseline() -> None:
    writing = _nodes(SCI_WRITING_WORKFLOW_ID)
    figures = _nodes(SCI_FIGURES_WORKFLOW_ID)
    journal = _nodes(SCI_JOURNAL_AUTHOR_WORKFLOW_ID)

    assert [node.execution_profile.token_limit for node in writing] == [
        1_000_000,
        2_000_000,
        3_000_000,
        3_000_000,
        4_000_000,
        4_000_000,
        4_000_000,
        4_000_000,
        4_000_000,
        4_000_000,
    ]
    assert {
        node.execution_profile.model
        for node in (*writing, *figures, *journal)
    } == {"gpt-5.5"}
    assert {
        node.execution_profile.reasoning_effort
        for node in (*writing, *figures, *journal)
    } == {""}
    assert [node.execution_profile.token_limit for node in (*figures, *journal)] == [None, None, None, None]
    assert all(node.retry_policy.max_attempts == 1 for node in (*writing, *figures, *journal))
    assert all(node.metadata["requires_runtime_freeze"] is True for node in (*writing, *figures, *journal))
    assert all(node.metadata["native_dispatch_ready"] is False for node in (*writing, *figures, *journal))
    groups = {
        workflow_id: {node.session_policy.session_group for node in _nodes(workflow_id)}
        for workflow_id in (
            SCI_WRITING_WORKFLOW_ID,
            SCI_FIGURES_WORKFLOW_ID,
            SCI_JOURNAL_AUTHOR_WORKFLOW_ID,
        )
    }
    assert groups == {
        SCI_WRITING_WORKFLOW_ID: {"sci-writing"},
        SCI_FIGURES_WORKFLOW_ID: {"sci-figures"},
        SCI_JOURNAL_AUTHOR_WORKFLOW_ID: {"sci-journal-author"},
    }


def test_smart_submission_remains_an_external_plugin_contract() -> None:
    definition = build_sci_workflow_pack().workflow(SCI_SMART_SUBMISSION_WORKFLOW_ID).definition
    node = definition.nodes[0]

    assert node.node_type is NodeType.PLUGIN
    assert node.prompt_ref is None
    assert node.implementation_ref == "smart-submission/program-runtime-v1"
    assert node.metadata["external_state_machine"] is True
    assert node.metadata["production_writer"] is False
    assert node.metadata["session_owner"] == "plugin"
    assert node.session_policy.session_group == ""
    assert node.retry_policy.max_attempts == 1
    assert node.metadata["program_task_prompt_version"] == "smart-submission-program-v1"
    assert node.metadata["sdk_prompt_version"] == "smart-submission-sdk-browser-v1"
    assert node.metadata["smart_prompt_version"] == 9
    assert "run_smart_submission_scheduler" not in str(dict(node.metadata))


def test_sci_nodes_preserve_legacy_artifact_contracts() -> None:
    writing = _nodes(SCI_WRITING_WORKFLOW_ID)
    assert [spec.artifact_id for spec in writing[0].outputs] == ["idea_decision_card"]
    assert [spec.artifact_id for spec in writing[1].outputs] == [
        "references",
        "references_bib",
        "literature_map",
        "novelty_check",
    ]
    assert [node.outputs[0].artifact_id for node in writing[2:]] == [
        "draft_v0",
        "draft_v1",
        "draft_v2",
        "draft_v3",
        "review_round_1",
        "draft_v4",
        "draft_v5",
        "draft_v6",
    ]
    assert {spec.artifact_id for spec in writing[7].inputs} >= {"draft_v3", "review_round_1"}

    figures = _nodes(SCI_FIGURES_WORKFLOW_ID)
    assert [spec.artifact_id for spec in figures[0].outputs] == [
        "reference_board",
        "reference_images_manifest",
        "missing_reference_images",
    ]
    assert [spec.artifact_id for spec in figures[1].outputs] == [
        "figure_plan",
        "figure_prompts",
        "figure_briefs_manifest",
    ]
    assert [spec.artifact_id for spec in figures[2].outputs] == [
        "submission_manifest",
        "submit_tex",
        "compiled_pdf",
        "figure_assets_manifest",
    ]

    journal = _nodes(SCI_JOURNAL_AUTHOR_WORKFLOW_ID)[0]
    assert {spec.artifact_id for spec in journal.inputs} == {
        "job",
        "work_manifest",
        "author_info",
        "submission_metadata_fingerprint",
        "journal_preferences",
    }
    assert {spec.artifact_id for spec in journal.outputs} >= {
        "journal",
        "main_tex",
        "cover_letter",
        "declarations",
        "submission_checklist",
        "file_manifest",
        "final_risk_report",
        "package_metadata_fingerprint",
    }


def test_runtime_paths_are_bound_outside_portable_workflow_identity(tmp_path: Path) -> None:
    first_roots = [tmp_path / "one" / name for name in ("writing", "figures", "journal")]
    second_roots = [tmp_path / "two" / name for name in ("writing", "figures", "journal")]
    first = build_sci_runtime_bindings(
        writing_root=first_roots[0],
        figure_root=first_roots[1],
        journal_author_root=first_roots[2],
    )
    second = build_sci_runtime_bindings(
        writing_root=second_roots[0],
        figure_root=second_roots[1],
        journal_author_root=second_roots[2],
    )
    pack = build_sci_workflow_pack()

    for version in pack.workflows:
        left = first[version.workflow_id]
        right = second[version.workflow_id]
        assert left.definition_sha256 == right.definition_sha256 == version.definition_sha256
        if version.workflow_id == SCI_SMART_SUBMISSION_WORKFLOW_ID:
            assert left.resources == right.resources == ()
            assert left.sha256() == right.sha256()
        else:
            assert left.sha256() != right.sha256()
            assert all(Path(item.absolute_path).is_absolute() for item in left.resources)

    writing_resources = first[SCI_WRITING_WORKFLOW_ID].resources
    assert [Path(item.absolute_path).name for item in writing_resources] == [
        TUNED_WRITING_PROMPT_FILES[step] for step in range(1, 11)
    ]
    assert [item.content_sha256 for item in writing_resources] == [
        TUNED_WRITING_PROMPT_SHA256[step].lower() for step in range(1, 11)
    ]


def test_sci_pack_registration_is_idempotent_builtin_and_readonly(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.initialize()

    first = register_sci_workflow_pack(store)
    second = register_sci_workflow_pack(store)

    assert len(first) == len(second) == 4
    assert len(store.list_versions()) == 4
    assert all(record["is_builtin"] is True for record in second)
    assert all(record["is_readonly"] is True for record in second)
    assert all(record["version"].status is WorkflowVersionStatus.PUBLISHED for record in second)
