from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from unified_paper_orchestrator_v2.executors.codex_sdk import CodexSDKExecutor
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway
from unified_paper_orchestrator_v2.provider_runtime import (
    RuntimeProviderConfig,
    provider_config_digest,
)
from unified_paper_orchestrator_v2.review_canary import REQUIRED_HEADINGS
from unified_paper_orchestrator_v2.staging import StagingStore


PAPER_ID = "2026-07-02_001_sdk-profile-test"
THREAD_ID = "123e4567-e89b-12d3-a456-426614174000"


def _writing_root(tmp_path: Path) -> Path:
    root = tmp_path / "writing"
    (root / "prompts").mkdir(parents=True)
    (root / "ideas").mkdir()
    (root / "refs" / PAPER_ID).mkdir(parents=True)
    (root / "papers" / PAPER_ID).mkdir(parents=True)
    (root / "prompts" / "07_review_round_1.md").write_text(
        "$academic-paper-reviewer\n\n# immutable SDK review instructions\n",
        encoding="utf-8",
    )
    (root / "ideas" / f"{PAPER_ID}.md").write_text("idea", encoding="utf-8")
    (root / "refs" / PAPER_ID / "references.md").write_text("refs", encoding="utf-8")
    (root / "refs" / PAPER_ID / "literature_map.md").write_text("map", encoding="utf-8")
    (root / "refs" / PAPER_ID / "novelty_check.md").write_text("novelty", encoding="utf-8")
    (root / "papers" / PAPER_ID / "draft_v3.tex").write_text(
        "\\begin{document}\n\\begin{figure}x\\end{figure}\n"
        "\\begin{table}x\\end{table}\n\\bibitem{x} x\n\\end{document}\n",
        encoding="utf-8",
    )
    return root


def _valid_review() -> str:
    parts: list[str] = []
    for heading in REQUIRED_HEADINGS:
        parts.append(heading)
        if heading == "# Review Round 1":
            continue
        if heading == "## Overall Verdict":
            parts.append("Reject")
        elif heading == "## Figure and Quantitative Evidence Review":
            parts.extend(
                [
                    "Figure total: 1",
                    "Table total: 1",
                    "Conceptual figure count: 0",
                    "Quantitative evidence figure count: 1",
                    "Hybrid figure count: 0",
                    "Table-disguised-as-figure count: 0",
                    "",
                    "| Fig. | Caption Summary | Current Type | Conceptual? | Quantitative? | Table-like? | Keep / Convert / Replace | Recommended New Type | Required Data / CSV Schema |",
                    "|---|---|---|---|---|---|---|---|---|",
                    "| 1 | Result | plot | No | Yes | No | Keep | plot | method,value,error bars |",
                    "Quantitative evidence requires CSV data, fair baselines, and error bars.",
                ]
            )
        elif heading == "## Required Revisions":
            parts.append("- [ ] Add reproducibility details, baselines, and evidence.")
        else:
            parts.append(
                "Add stronger evidence, novelty analysis, fair baselines, and reproducibility details."
            )
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


class _Thread:
    id = THREAD_ID

    def run(self, prompt: str) -> object:
        assert "immutable SDK review instructions" in prompt
        return SimpleNamespace(
            id="turn-sdk-gateway",
            status="completed",
            final_response=_valid_review(),
            usage=SimpleNamespace(
                total=SimpleNamespace(
                    input_tokens=100,
                    cached_input_tokens=0,
                    output_tokens=50,
                    reasoning_output_tokens=10,
                    total_tokens=150,
                )
            ),
        )


class _CodexContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def thread_start(self, **kwargs):
        assert kwargs["model_provider"] == "custom_relay"
        assert kwargs["model"] == "provider-codex-model"
        return _Thread()


def _provider() -> RuntimeProviderConfig:
    return RuntimeProviderConfig(
        profile_id="custom_relay",
        display_name="Custom relay",
        base_url="https://relay.example.test/v1",
        model="provider-codex-model",
        reasoning_effort="high",
    )


def test_codex_sdk_profile_review_is_approved_validated_and_staging_only(
    tmp_path: Path,
) -> None:
    provider = _provider()
    executor = CodexSDKExecutor(
        lambda runtime: _CodexContext(),
        provider_config=provider,
        key_resolver=lambda profile_id: "secret" if profile_id == provider.profile_id else "",
        runtime_temp_root=tmp_path / "runtime",
    )
    registry = ExecutorRegistry()
    registry.register(executor)
    staging = StagingStore(tmp_path / "staging", acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging.root,
        staging_store=staging,
        registry=registry,
        review_writing_root=_writing_root(tmp_path),
    )

    task = gateway.submit_codex_sdk_profile_review_canary(
        PAPER_ID,
        provider_config=provider,
        writing_root=tmp_path / "writing",
    )

    assert task["status"] == "waiting_approval"
    assert task["approval"]["intent"]["endpoint_base_url"] == provider.base_url
    approval = task["approval"]
    gateway.approve(
        str(task["task_id"]),
        expected_intent_sha256=str(approval["intent_sha256"]),
        note="approve configured provider disclosure",
    )
    completed = gateway.run_once(str(task["task_id"]))

    assert completed is not None and completed["status"] == "completed"
    result = completed["result"]
    assert result["external_ref"] == THREAD_ID
    assert result["data"]["provider_profile"] == provider.profile_id
    assert result["data"]["endpoint_config_sha256"] == provider_config_digest(provider)
    assert result["data"]["canonical_write"] is False
    staging_dir = Path(result["data"]["staging_dir"])
    manifest = json.loads((staging_dir / "result.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "review_canary_result_v3"
    assert manifest["provider_external_ref_kind"] == "codex_thread_id"
    assert manifest["provider_external_ref"] == THREAD_ID
    assert manifest["canonical_write"] is False
    assert "secret" not in json.dumps(completed)
