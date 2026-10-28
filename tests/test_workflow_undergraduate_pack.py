from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pytest

from unified_paper_orchestrator.workflow import NodeType, WorkflowVersionStatus
from unified_paper_orchestrator.workflow.library import WorkflowLibrary
from unified_paper_orchestrator.workflow.executors import (
    CodexResponse,
    CodexSDKExecutor,
    DeclaredFileOutputCollector,
    ExecutorRegistry,
)
from unified_paper_orchestrator.workflow.packs import undergraduate as undergraduate_module
from unified_paper_orchestrator.workflow.packs.undergraduate import (
    UNDERGRADUATE_PACK_ID,
    UNDERGRADUATE_PROMPT_FILES,
    UNDERGRADUATE_PROMPT_SHA256,
    UNDERGRADUATE_THESIS_WORKFLOW_ID,
    build_undergraduate_runtime_binding,
    build_undergraduate_workflow_pack,
    register_undergraduate_workflow_pack,
    undergraduate_prompt_manifest,
    undergraduate_schema_validators,
)
from unified_paper_orchestrator.workflow.resources import ContentAddressedPromptStore
from unified_paper_orchestrator.workflow.scheduler import SchedulerConfig, WorkflowScheduler
from unified_paper_orchestrator.workflow.store import WorkflowStore
from unified_paper_orchestrator.workflow.validation import WorkflowValidationError


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _asset_root() -> Path:
    return Path(undergraduate_module.__file__).resolve().parent / "prompts" / "undergraduate"


def _version():
    return build_undergraduate_workflow_pack().workflow(UNDERGRADUATE_THESIS_WORKFLOW_ID)


def test_undergraduate_pack_is_published_readonly_cloneable_and_linear() -> None:
    pack = build_undergraduate_workflow_pack()
    version = _version()
    definition = version.definition

    assert pack.pack_id == UNDERGRADUATE_PACK_ID
    assert pack.metadata["builtin"] is True
    assert pack.metadata["readonly"] is True
    assert pack.metadata["cloneable"] is True
    assert version.status is WorkflowVersionStatus.PUBLISHED
    assert definition.metadata["builtin"] is True
    assert definition.metadata["readonly"] is True
    assert definition.metadata["cloneable"] is True
    assert len(definition.nodes) == 6
    assert len(definition.edges) == 5
    assert all(node.node_type is NodeType.CODEX for node in definition.nodes)
    assert [edge.source_node_id for edge in definition.edges] == [
        node.node_id for node in definition.nodes[:-1]
    ]
    assert [edge.target_node_id for edge in definition.edges] == [
        node.node_id for node in definition.nodes[1:]
    ]
    definition.validate()


def test_artifact_bindings_are_complete_dominating_and_declared_files_are_exact() -> None:
    definition = _version().definition
    node_positions = {node.node_id: index for index, node in enumerate(definition.nodes)}

    for node in definition.nodes:
        assert {spec.artifact_id for spec in node.inputs} == {
            binding.target_artifact_id for binding in node.input_bindings
        }
        declared = dict(node.metadata["declared_output_files"])
        assert set(declared) == {spec.artifact_id for spec in node.outputs}
        assert len(set(declared.values())) == len(declared)
        for relative_path in declared.values():
            path = PurePosixPath(relative_path)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert path.parts[0] == "undergraduate"
        for binding in node.input_bindings:
            if binding.source_node_id:
                assert node_positions[binding.source_node_id] < node_positions[node.node_id]

    assert {spec.artifact_id for spec in definition.outputs} == {
        binding.target_artifact_id for binding in definition.output_bindings
    }
    assert {
        (binding.source_node_id, binding.source_artifact_id)
        for binding in definition.output_bindings
    } == {
        ("integrity-review", "reviewed_thesis"),
        ("integrity-review", "integrity_report"),
    }


def test_profiles_are_frozen_high_reasoning_offline_with_one_run_session_group() -> None:
    nodes = _version().definition.nodes

    assert {node.execution_profile.model for node in nodes} == {"gpt-5.6-sol"}
    assert {node.execution_profile.reasoning_effort for node in nodes} == {"high"}
    assert {node.execution_profile.token_limit for node in nodes} == {400_000}
    assert {node.execution_profile.network_access for node in nodes} == {False}
    assert {node.execution_profile.allowed_tools for node in nodes} == {("read", "write")}
    assert {node.retry_policy.max_attempts for node in nodes} == {1}
    assert {node.session_policy.session_group for node in nodes} == {"undergraduate-thesis"}
    assert all(node.session_policy.reuse_within_run for node in nodes)
    assert all(not node.session_policy.reuse_on_retry for node in nodes)
    assert all(not node.session_policy.force_new_session for node in nodes)


def test_undergraduate_pack_executes_full_declared_file_chain_with_trusted_validators(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    prompt_store = ContentAddressedPromptStore(tmp_path / "state" / "v2" / "resources" / "prompts")
    library = WorkflowLibrary(store, prompt_store)
    library.initialize()
    version = library.install_pack(build_undergraduate_workflow_pack())[0]["version"]
    binding = build_undergraduate_runtime_binding()
    run = library.create_run(
        version.version_id,
        binding,
        inputs={
            "thesis_brief": {"topic": "可核验的校园节能研究", "language": "zh-CN"},
            "evidence_bundle": {"sources": []},
        },
        idempotency_key="undergraduate-e2e",
        now=NOW,
    )
    expected = [node.node_id for node in version.definition.nodes]
    seen: list[str] = []

    class Client:
        def run(self, *, session, request, control):
            node_id = expected[len(seen)]
            seen.append(node_id)
            assert request.network_access is False
            assert request.allowed_tools == ("read", "write")
            files = version.definition.nodes[len(seen) - 1].metadata["declared_output_files"]
            for artifact_id, relative_path in files.items():
                target = request.output_root / Path(relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if artifact_id == "evidence_register":
                    payload = {
                        "schema": "upo.undergraduate.evidence-register.v1",
                        "language": "zh-CN",
                        "research_questions": [],
                        "sources": [],
                        "claim_evidence_map": [],
                        "gaps": ["[证据缺口: 尚无来源]"],
                        "warnings": [],
                    }
                    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                elif artifact_id == "integrity_report":
                    payload = {
                        "schema": "upo.undergraduate.integrity-report.v1",
                        "language": "zh-CN",
                        "verdict": "needs_evidence",
                        "checks": [],
                        "unresolved_gaps": ["尚无来源"],
                        "required_human_actions": ["提供可核验证据"],
                    }
                    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                else:
                    target.write_text(f"# {artifact_id}\n\n[证据缺口: 尚无来源]\n", encoding="utf-8")
            return CodexResponse(
                "completed",
                output_text="declared files written",
                external_session_id="external-undergraduate-e2e",
                usage={"total_tokens": 10},
            )

    scheduler = WorkflowScheduler(
        store,
        tmp_path,
        ExecutorRegistry(
            (CodexSDKExecutor(Client(), output_collector=DeclaredFileOutputCollector()),)
        ),
        config=SchedulerConfig(max_workers=1, lease_seconds=10, heartbeat_seconds=1),
        schema_validators=undergraduate_schema_validators(),
    )
    report = scheduler.run_until_idle((run["run_id"],), now=NOW)

    assert seen == expected
    assert report.completed == 6
    assert report.failed == 0
    assert store.get_run(run["run_id"])["status"] == "completed"
    assert {item["artifact_id"] for item in store.list_artifacts(run["run_id"])} == {
        "research_scope",
        "evidence_register",
        "methodology_plan",
        "analysis_plan",
        "thesis_draft",
        "reviewed_thesis",
        "integrity_report",
    }
    resolved = scheduler.artifacts.resolve_workflow_outputs(version.definition, run["run_id"])
    assert set(resolved) == {"reviewed_thesis", "integrity_report"}


def test_undergraduate_schema_validators_reject_empty_or_structurally_invalid_outputs(tmp_path: Path) -> None:
    validators = undergraduate_schema_validators()
    markdown = tmp_path / "empty.md"
    markdown.write_text("  \n", encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"schema":"upo.undergraduate.evidence-register.v1"}', encoding="utf-8")
    integrity = tmp_path / "integrity.json"
    integrity.write_text(
        json.dumps(
            {
                "schema": "upo.undergraduate.integrity-report.v1",
                "language": "zh-CN",
                "verdict": "approved",
                "checks": [],
                "unresolved_gaps": [],
                "required_human_actions": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-empty"):
        validators["upo.undergraduate.research-scope.v1"](markdown)
    with pytest.raises(ValueError, match="exactly"):
        validators["upo.undergraduate.evidence-register.v1"](evidence)
    with pytest.raises(ValueError, match="verdict"):
        validators["upo.undergraduate.integrity-report.v1"](integrity)


def test_prompt_manifest_seals_every_package_asset_and_default_binding_validates_files() -> None:
    manifest = undergraduate_prompt_manifest()
    root = _asset_root()
    version = _version()
    binding = build_undergraduate_runtime_binding()

    assert set(manifest) == set(UNDERGRADUATE_PROMPT_FILES) == set(UNDERGRADUATE_PROMPT_SHA256)
    assert len(binding.resources) == len(manifest)
    assert binding.definition_sha256 == version.definition_sha256
    assert {resource.resource_id for resource in binding.resources} == {
        record["resource_id"] for record in manifest.values()
    }
    for stage, filename in UNDERGRADUATE_PROMPT_FILES.items():
        path = root / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == UNDERGRADUATE_PROMPT_SHA256[stage]
        assert manifest[stage] == {
            "resource_id": f"{UNDERGRADUATE_PACK_ID}.prompt.{stage}",
            "relative_path": f"prompts/undergraduate/{filename}",
            "sha256": digest,
            "active": True,
        }
    binding.validate(version.definition)
    binding.validate_files(version.definition)
    assert all(Path(resource.absolute_path).is_absolute() for resource in binding.resources)


def test_runtime_paths_do_not_change_portable_definition_identity(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    shutil.copytree(_asset_root(), first_root)
    shutil.copytree(_asset_root(), second_root)

    first = build_undergraduate_runtime_binding(prompt_root=first_root)
    second = build_undergraduate_runtime_binding(prompt_root=second_root)
    version = _version()
    serialized = version.definition.canonical_json()

    assert first.definition_sha256 == second.definition_sha256 == version.definition_sha256
    assert first.sha256() != second.sha256()
    assert str(first_root) not in serialized
    assert str(second_root) not in serialized
    assert not any(resource.absolute_path in serialized for resource in (*first.resources, *second.resources))


def test_runtime_binding_fails_closed_for_missing_or_drifted_prompt(tmp_path: Path) -> None:
    with pytest.raises(WorkflowValidationError, match="file_unreadable"):
        build_undergraduate_runtime_binding(prompt_root=tmp_path / "missing")

    drifted_root = tmp_path / "drifted"
    shutil.copytree(_asset_root(), drifted_root)
    target = drifted_root / UNDERGRADUATE_PROMPT_FILES["research-scope"]
    target.write_text(target.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    with pytest.raises(WorkflowValidationError, match="file_sha256_mismatch"):
        build_undergraduate_runtime_binding(prompt_root=drifted_root)


def test_registration_is_idempotent_and_clone_does_not_mutate_source(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    prompt_store = ContentAddressedPromptStore(tmp_path / "state" / "v2" / "resources" / "prompts")
    library = WorkflowLibrary(store, prompt_store)
    library.initialize()

    first = register_undergraduate_workflow_pack(store)
    second = register_undergraduate_workflow_pack(store)
    source = first[0]["version"]
    source_digest = source.definition_sha256
    clone = library.clone_version(
        source.version_id,
        workflow_id="custom.undergraduate-thesis",
        name="My undergraduate thesis workflow",
        now=NOW,
    )["version"]

    assert len(first) == len(second) == 1
    assert len(store.list_versions(UNDERGRADUATE_THESIS_WORKFLOW_ID)) == 1
    assert second[0]["is_builtin"] is True
    assert second[0]["is_readonly"] is True
    assert clone.status is WorkflowVersionStatus.DRAFT
    assert clone.definition.metadata["builtin"] is False
    assert clone.definition.metadata["readonly"] is False
    assert store.get_version(source.version_id)["version"].definition_sha256 == source_digest
    assert store.get_version(source.version_id)["version"].definition == source.definition


def test_prompts_enforce_language_evidence_and_integrity_without_domain_shortcuts() -> None:
    prompt_texts = [
        (_asset_root() / filename).read_text(encoding="utf-8")
        for filename in UNDERGRADUATE_PROMPT_FILES.values()
    ]
    product_source = Path(undergraduate_module.__file__).read_text(encoding="utf-8")
    corpus = "\n".join((product_source, *prompt_texts))
    lowered = corpus.casefold()

    for text in prompt_texts:
        assert "thesis_brief.language" in text
        assert "简体中文" in text
        assert "证据缺口" in text
        assert "伦理审批" in text
        assert "编造" in text

    forbidden = (
        "legacy",
        "paper_id",
        "step5",
        "step 5",
        "step6",
        "step 6",
        "range(1, 11)",
        "10 steps",
        "ten steps",
    )
    assert not any(token in lowered for token in forbidden)
    assert all(node.metadata["evidence_policy"] == "bound-inputs-only" for node in _version().definition.nodes)
    assert all(node.metadata["language_policy"] == "brief-language-or-zh-CN" for node in _version().definition.nodes)
    assert all(node.metadata["forbid_fabricated_citations"] is True for node in _version().definition.nodes)
    assert all(node.metadata["forbid_fabricated_data"] is True for node in _version().definition.nodes)
    assert all(node.metadata["forbid_fabricated_ethics_approval"] is True for node in _version().definition.nodes)
