from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import unified_paper_orchestrator_v2.staging as staging_module
from unified_paper_orchestrator_v2.contracts import ArtifactRef, TaskEnvelope, canonical_json
from unified_paper_orchestrator_v2.executors.legacy import LegacyCodexAutomationExecutor
from unified_paper_orchestrator_v2.executors.openai_review import (
    OFFICIAL_OPENAI_BASE_URL,
    OPENAI_LOOPBACK_PROXY_ENV,
    OpenAIProxyConfigurationError,
    OpenAIReviewExecutor,
    validated_loopback_proxy_url,
)
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry, build_default_registry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway, GatewayError
from unified_paper_orchestrator_v2.review_canary import (
    REQUIRED_HEADINGS,
    ReviewCanaryError,
    build_review_canary_envelope,
    expected_review_idempotency_key,
)
from unified_paper_orchestrator_v2.staging import StagingError, StagingStore


PAPER_ID = "2026-07-02_001_test-paper"


class FakeUsage:
    total_tokens = 150

    def model_dump(self) -> dict[str, int]:
        return {"input_tokens": 100, "output_tokens": 50, "total_tokens": self.total_tokens}


class FakeResponses:
    def __init__(self, output_text: str = "", error: Exception | None = None) -> None:
        self.output_text = output_text
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            id="resp_test",
            status="completed",
            output_text=self.output_text,
            usage=FakeUsage(),
            incomplete_details=None,
        )


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


def make_writing_root(tmp_path: Path, *, figure_count: int = 2, table_count: int = 1) -> Path:
    root = tmp_path / "writing"
    (root / "prompts").mkdir(parents=True)
    (root / "ideas").mkdir()
    (root / "refs" / PAPER_ID).mkdir(parents=True)
    (root / "papers" / PAPER_ID).mkdir(parents=True)
    (root / "prompts" / "07_review_round_1.md").write_text(
        "$academic-paper-reviewer\n\n# immutable review instructions\n",
        encoding="utf-8",
    )
    (root / "ideas" / f"{PAPER_ID}.md").write_text("distinctive idea input", encoding="utf-8")
    (root / "refs" / PAPER_ID / "references.md").write_text("verified references", encoding="utf-8")
    (root / "refs" / PAPER_ID / "literature_map.md").write_text("literature map", encoding="utf-8")
    (root / "refs" / PAPER_ID / "novelty_check.md").write_text("novelty check", encoding="utf-8")
    draft = ["distinctive manuscript input", "\\begin{document}"]
    draft.extend("\\begin{figure}x\\end{figure}" for _ in range(figure_count))
    draft.extend("\\begin{table}x\\end{table}" for _ in range(table_count))
    draft.extend(["\\bibitem{x} citation", "\\end{document}"])
    (root / "papers" / PAPER_ID / "draft_v3.tex").write_text("\n".join(draft), encoding="utf-8")
    return root


def valid_review(*, figure_count: int = 2, table_count: int = 1) -> str:
    sections: list[str] = []
    for heading in REQUIRED_HEADINGS:
        sections.append(heading)
        if heading == "# Review Round 1":
            continue
        if heading == "## Overall Verdict":
            sections.append("Reject")
        elif heading == "## Figure and Quantitative Evidence Review":
            sections.extend(
                [
                    f"Figure total: {figure_count}",
                    f"Table total: {table_count}",
                    "Conceptual figure count: 1",
                    "Quantitative evidence figure count: 1",
                    "Hybrid figure count: 0",
                    "Table-disguised-as-figure count: 0",
                    "",
                    "| Fig. | Caption Summary | Current Type | Conceptual? | Quantitative? | Table-like? | Keep / Convert / Replace | Recommended New Type | Required Data / CSV Schema |",
                    "|---|---|---|---|---|---|---|---|---|",
                ]
            )
            sections.extend(
                f"| {index} | Result {index} | data plot | No | Yes | No | Keep | plot | method,value,error bars |"
                for index in range(1, figure_count + 1)
            )
            sections.append("The quantitative data-driven plots require CSV evidence and fair baselines.")
        elif heading == "## Required Revisions":
            sections.append("- [ ] Add reproducibility seeds, hardware protocol, controller implementation, and baseline fairness.")
        else:
            sections.append(
                "The novelty and related work need stronger evidence. Add reproducibility details, controller pseudocode, "
                "fair baseline tuning, quantitative plots, CSV artifacts, and error bars."
            )
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def make_gateway(tmp_path: Path, output_text: str, *, error: Exception | None = None):
    responses = FakeResponses(output_text, error)
    registry = ExecutorRegistry()
    registry.register(OpenAIReviewExecutor(lambda _task: FakeClient(responses)))
    staging = StagingStore(tmp_path / "staging", acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging.root,
        registry=registry,
        staging_store=staging,
        review_writing_root=tmp_path / "writing",
    )
    return gateway, responses


def source_snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_review_canary_fake_api_is_idempotent_and_staging_only(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    before = source_snapshot(writing_root)
    gateway, responses = make_gateway(tmp_path, valid_review())

    first = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)
    duplicate = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)
    assert duplicate["task_id"] == first["task_id"]

    completed = gateway.run_once(first["task_id"])
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["data"]["canonical_write"] is False
    assert completed["result"]["data"]["source_unchanged"] is True
    with pytest.raises(GatewayError) as exc_info:
        gateway.run_once(first["task_id"])
    assert exc_info.value.code == "GATEWAY_TASK_NOT_QUEUED"
    assert len(responses.calls) == 1
    request = responses.calls[0]
    assert request["model"] == "test-model"
    assert request["store"] is False
    assert request["max_output_tokens"] == 8000
    assert "immutable review instructions" in str(request["instructions"])
    assert "distinctive manuscript input" in str(request["input"])

    staging_dir = Path(completed["result"]["data"]["staging_dir"])
    assert {path.name for path in staging_dir.iterdir()} == {"review_round_1.md", "result.json"}
    manifest = json.loads((staging_dir / "result.json").read_text(encoding="utf-8"))
    assert manifest["source_unchanged"] is True
    assert manifest["canonical_write"] is False
    assert manifest["validation"]["ok"] is True
    assert source_snapshot(writing_root) == before

    serialized = json.dumps(completed, ensure_ascii=False)
    serialized += json.dumps(gateway.store.list_events(), ensure_ascii=False)
    assert "distinctive manuscript input" not in serialized
    assert "immutable review instructions" not in serialized
    assert "OPENAI_API_KEY" not in serialized


def test_concurrent_duplicate_submission_creates_one_task(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, valid_review())

    def submit() -> str:
        return str(gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)["task_id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        task_ids = list(pool.map(lambda _index: submit(), range(4)))
    assert len(set(task_ids)) == 1
    status = gateway.status()
    assert status["runtime"]["task_counts"] == {"queued": 1}
    assert status["canonical_writes_enabled"] is False
    assert status["canonical_write_owner"] == "v1_existing_workflows"
    assert status["canonical_write_switch"] == "backend_router_legacy_only"
    assert status["generic_proxy_environment_ignored"] is True
    assert status["review_transport_policies"] == [
        "direct_no_env_proxy",
        "explicit_loopback_proxy_no_env",
    ]
    assert status["explicit_proxy_scope"] == "credential_free_http_loopback_only"


def test_prompt_fallback_is_in_memory_and_source_hash_is_preserved(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    prompt = writing_root / "prompts" / "07_review_round_1.md"
    before = prompt.read_bytes()
    envelope = build_review_canary_envelope(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
        staging_root=tmp_path / "staging",
    )

    assert envelope.payload["prompt_transform_id"] == "prompt_declared_skill_fallback_v1"
    assert envelope.payload["source_prompt_sha256"] != envelope.payload["rendered_prompt_sha256"]
    assert prompt.read_bytes() == before


def test_input_hash_drift_stops_before_api_call(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)
    draft = writing_root / "papers" / PAPER_ID / "draft_v3.tex"
    draft.write_text(draft.read_text(encoding="utf-8") + "\ndrift", encoding="utf-8")

    failed = gateway.run_once(queued["task_id"])
    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "REVIEW_CANARY_INPUT_SHA256_MISMATCH"
    assert responses.calls == []
    assert not (tmp_path / "staging").exists()


def test_invalid_api_review_is_not_staged(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, "# Review Round 1\n\nReject\n")
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)

    failed = gateway.run_once(queued["task_id"])
    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "REVIEW_CANARY_OUTPUT_INVALID"
    assert failed["result"]["data"]["validation"]["ok"] is False
    assert not (tmp_path / "staging").exists()


def test_gateway_rejects_non_staging_writable_root(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, valid_review())
    envelope = build_review_canary_envelope(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
        staging_root=tmp_path / "staging",
    )
    envelope.writable_roots = [str(tmp_path / "outside")]

    with pytest.raises(GatewayError) as exc_info:
        gateway.submit(envelope)
    assert exc_info.value.code == "GATEWAY_WRITABLE_ROOT_DENIED"


def test_generic_submit_cannot_change_review_idempotency_key(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, valid_review())
    envelope = build_review_canary_envelope(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
        staging_root=tmp_path / "staging",
    )
    envelope.idempotency_key = "random-paid-call-key"

    with pytest.raises(GatewayError) as caught:
        gateway.submit(envelope)

    assert caught.value.code == "GATEWAY_REVIEW_IDEMPOTENCY_INVALID"


def test_review_gateway_rejects_recomputed_key_for_arbitrary_local_file(
    tmp_path: Path,
) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, valid_review())
    envelope = build_review_canary_envelope(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
        staging_root=tmp_path / "staging",
    )
    outside = tmp_path / "private-unrelated.txt"
    outside.write_text("must never be sent", encoding="utf-8")
    replacement = ArtifactRef.from_file(outside, media_type="text/markdown")
    envelope.inputs[0] = replacement
    envelope.payload["input_roles"]["idea"] = {
        "uri": replacement.uri,
        "sha256": replacement.sha256,
    }
    manifest = {
        role: value["sha256"]
        for role, value in envelope.payload["input_roles"].items()
    }
    envelope.payload["input_manifest_sha256"] = hashlib.sha256(
        canonical_json(manifest).encode("utf-8")
    ).hexdigest()
    envelope.idempotency_key = expected_review_idempotency_key(
        envelope,
        staging_root=tmp_path / "staging",
    )

    with pytest.raises(ReviewCanaryError) as caught:
        gateway.submit(envelope)

    assert caught.value.code == "REVIEW_CANARY_SOURCE_SCOPE_DENIED"


def test_api_timeout_is_stable_and_never_staged(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, "", error=TimeoutError("secret request detail"))
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)

    failed = gateway.run_once(queued["task_id"])
    assert failed is not None
    assert failed["status"] == "retryable_failure"
    assert failed["result"]["blocker_code"] == "OPENAI_API_TIMEOUT"
    assert "secret request detail" not in json.dumps(failed, ensure_ascii=False)
    assert len(responses.calls) == 1
    assert not (tmp_path / "staging").exists()


def test_unconfigured_api_fails_closed_without_network_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    writing_root = make_writing_root(tmp_path)
    staging = StagingStore(tmp_path / "staging", acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging.root,
        registry=build_default_registry(),
        staging_store=staging,
        review_writing_root=writing_root,
    )
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)

    blocked = gateway.run_once(queued["task_id"])
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["result"]["blocker_code"] == "OPENAI_API_NOT_CONFIGURED"
    assert not (tmp_path / "staging").exists()


def test_explicit_model_works_without_openai_model_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    writing_root = make_writing_root(tmp_path)
    responses = FakeResponses(valid_review())
    executor = OpenAIReviewExecutor()
    monkeypatch.setattr(
        executor,
        "_client",
        lambda _task, **_kwargs: FakeClient(responses),
    )
    registry = ExecutorRegistry()
    registry.register(executor)
    staging = StagingStore(tmp_path / "staging", acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=tmp_path / "gateway.sqlite3",
        staging_root=staging.root,
        registry=registry,
        staging_store=staging,
        review_writing_root=writing_root,
    )

    queued = gateway.submit_review_canary(PAPER_ID, model="explicit-test-model", writing_root=writing_root)
    completed = gateway.run_once(queued["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"
    assert responses.calls[0]["model"] == "explicit-test-model"


def test_actual_usage_over_token_budget_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    monkeypatch.setattr(FakeUsage, "total_tokens", 1001)
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
        token_budget=1000,
        max_output_tokens=100,
    )

    failed = gateway.run_once(queued["task_id"])

    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "OPENAI_TOKEN_BUDGET_EXCEEDED"
    assert len(responses.calls) == 1
    assert not (tmp_path / "staging").exists()


def test_preflight_budget_blocks_before_api_call(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
        token_budget=400,
        max_output_tokens=300,
    )

    failed = gateway.run_once(queued["task_id"])

    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "OPENAI_TOKEN_BUDGET_PREFLIGHT"
    assert responses.calls == []


def test_validator_accepts_prompt_declared_chinese_totals_and_markdown_spacing(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    review = valid_review().replace("Figure total: 2", "- figure 总数：2").replace(
        "Table total: 1",
        "* table 总数：1",
    )
    review = review.replace("| 1 | Result 1 |", "| **1** | Result 1 |")
    gateway, _responses = make_gateway(tmp_path, review)
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)

    completed = gateway.run_once(queued["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"


def test_production_client_ignores_openai_base_url_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    captured: dict[str, object] = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            _custom_headers={},
            organization="",
            project="",
            admin_api_key="",
            close=lambda: None,
        )

    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "official-test-key")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-must-not-leak")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "project-must-not-leak")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer overridden\nX-Private-Header: must-not-leak",
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    task = TaskEnvelope(
        paper_id=PAPER_ID,
        stage="writing",
        action="writing.review_round_1.canary",
        executor="openai_review",
        timeout_seconds=321,
    )

    OpenAIReviewExecutor()._client(task)

    assert captured["base_url"] == OFFICIAL_OPENAI_BASE_URL
    assert captured["api_key"] == "official-test-key"
    assert captured["admin_api_key"] == ""
    assert captured["organization"] == ""
    assert captured["project"] == ""
    assert captured["webhook_secret"] == ""
    assert captured["default_headers"] == {}
    assert captured["default_query"] == {}
    assert captured["timeout"] == 321.0
    assert captured["max_retries"] == 0
    http_client = captured["http_client"]
    assert http_client._trust_env is False
    http_client.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("http://127.1.2.3:8080/", "http://127.1.2.3:8080"),
        ("http://localhost:8888", "http://localhost:8888"),
        ("http://[::1]:1080", "http://[::1]:1080"),
    ],
)
def test_explicit_openai_proxy_accepts_only_normalized_loopback(
    raw: str,
    expected: str,
) -> None:
    assert validated_loopback_proxy_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://127.0.0.1:7890",
        "http://127.0.0.1",
        "http://user:secret@127.0.0.1:7890",
        "http://192.168.1.8:7890",
        "http://proxy.example:7890",
        "http://127.0.0.1:7890/path",
        "http://127.0.0.1:7890?mode=x",
        "not-a-url",
    ],
)
def test_explicit_openai_proxy_rejects_remote_credentials_and_url_payloads(
    raw: str,
) -> None:
    with pytest.raises(OpenAIProxyConfigurationError):
        validated_loopback_proxy_url(raw)


def test_production_client_uses_only_explicit_validated_loopback_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import openai

    captured: dict[str, object] = {}
    client_options: dict[str, object] = {}

    def fake_http_client(**kwargs):
        client_options.update(kwargs)
        return object()

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            _custom_headers={},
            organization="",
            project="",
            admin_api_key="",
            close=lambda: None,
        )

    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv(OPENAI_LOOPBACK_PROXY_ENV, "http://127.0.0.1:7890/")
    monkeypatch.setattr(httpx, "Client", fake_http_client)
    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    task = TaskEnvelope(
        paper_id=PAPER_ID,
        stage="writing",
        action="writing.review_round_1.canary",
        executor="openai_review",
        timeout_seconds=321,
    )

    OpenAIReviewExecutor()._client(task)

    assert client_options == {
        "timeout": 321.0,
        "trust_env": False,
        "proxy": "http://127.0.0.1:7890",
    }
    assert captured["base_url"] == OFFICIAL_OPENAI_BASE_URL


def test_invalid_explicit_openai_proxy_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv(OPENAI_LOOPBACK_PROXY_ENV, "http://remote.invalid:7890")
    task = TaskEnvelope(
        paper_id=PAPER_ID,
        stage="writing",
        action="writing.review_round_1.canary",
        executor="openai_review",
        network_access=True,
        token_budget=100,
        payload={
            "_prepared_instructions": "review safely",
            "_prepared_input": "paper",
            "_estimated_input_tokens": 2,
            "model": "test-model",
            "max_output_tokens": 10,
        },
    )

    result = OpenAIReviewExecutor().execute(task)

    assert result.status.value == "blocked"
    assert result.blocker_code == "OPENAI_LOOPBACK_PROXY_INVALID"
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "remote.invalid" not in serialized
    assert "test-only-key" not in serialized


def test_default_registry_exposes_legacy_codex_compatibility_port() -> None:
    descriptors = {item["name"]: item for item in build_default_registry().describe()}
    assert descriptors["legacy_codex"]["kind"] == "v1_codex_automation_compatibility"
    assert descriptors["legacy_codex"]["side_effecting"] is True
    assert descriptors["openai_review"]["side_effecting"] is True


def test_gateway_and_legacy_codex_port_both_refuse_production_apply(tmp_path: Path) -> None:
    gateway, _responses = make_gateway(tmp_path, valid_review())
    apply_task = TaskEnvelope(
        paper_id=PAPER_ID,
        stage="system",
        action="legacy.apply",
        executor="legacy_codex",
    )

    with pytest.raises(GatewayError) as exc_info:
        gateway.submit(apply_task)

    assert exc_info.value.code == "GATEWAY_LEGACY_APPLY_DISABLED"
    blocked = LegacyCodexAutomationExecutor().execute(apply_task)
    assert blocked.status.value == "blocked"
    assert blocked.blocker_code == "LEGACY_CODEX_GATEWAY_WRITES_DISABLED"


def test_staging_pair_is_not_partially_published_when_manifest_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = StagingStore(tmp_path / "staging", acl_hardener=None)
    original_write = staging_module._durable_write_new

    def fail_manifest(path: Path, payload: bytes) -> None:
        if path.name == "result.json":
            raise staging_module.StagingError("TEST_FAILURE", "injected manifest failure")
        original_write(path, payload)

    monkeypatch.setattr(staging_module, "_durable_write_new", fail_manifest)
    run_id = f"run_{'a' * 32}"
    task_id = f"task_{'b' * 32}"
    with pytest.raises(staging_module.StagingError):
        staging.persist_review(
            run_id=run_id,
            task_id=task_id,
            markdown="# review\n",
            manifest={
                "run_id": run_id,
                "task_id": task_id,
                "canonical_write": False,
            },
        )

    assert not staging.task_dir(run_id, task_id).exists()
    assert not [path for path in staging.root.rglob("*") if path.is_file()]


def test_post_api_staging_failure_preserves_compact_usage_and_response_id(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, valid_review())

    class FailingStagingStore(StagingStore):
        def persist_review(self, **_kwargs):
            raise StagingError("STAGING_TEST_FAILURE", "injected staging failure")

    gateway.staging = FailingStagingStore(gateway.staging.root, acl_hardener=None)
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)
    failed = gateway.run_once(queued["task_id"])

    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "STAGING_TEST_FAILURE"
    assert failed["result"]["metrics"]["total_tokens"] == 150
    assert failed["result"]["external_ref"] == "resp_test"
    assert "output_text" not in json.dumps(failed, ensure_ascii=False)


def test_gateway_task_lookup_and_explicit_retry_are_unambiguous(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, _responses = make_gateway(tmp_path, "", error=TimeoutError("transient"))
    queued = gateway.submit_review_canary(PAPER_ID, model="test-model", writing_root=writing_root)
    failed = gateway.run_once(queued["task_id"])

    assert failed is not None and failed["status"] == "retryable_failure"
    assert gateway.task(queued["task_id"])["status"] == "retryable_failure"
    with pytest.raises(GatewayError) as retry_error:
        gateway.retry(queued["task_id"])
    assert retry_error.value.code == "GATEWAY_REVIEW_RETRY_ACK_REQUIRED"

    retried = gateway.retry(
        queued["task_id"],
        acknowledge_possible_duplicate_charge=True,
    )
    assert retried["status"] == "queued"
    assert retried["execution_attempt_token_sha256"] is None
    requeue_event = [
        event
        for event in gateway.store.list_events()
        if event["event_type"] == "task.requeued"
    ][-1]
    assert requeue_event["payload"]["acknowledgements"] == {
        "possible_duplicate_charge": True
    }
    with pytest.raises(GatewayError) as exc_info:
        gateway.task("task_missing")
    assert exc_info.value.code == "GATEWAY_TASK_NOT_FOUND"


def test_queued_review_never_accepts_a_preplanted_staging_receipt(tmp_path: Path) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
    )
    gateway.staging.persist_review(
        run_id=queued["run_id"],
        task_id=queued["task_id"],
        markdown=valid_review(),
        manifest={
            "run_id": queued["run_id"],
            "task_id": queued["task_id"],
            "response_id": "resp_preplanted",
            "model": "forged-model",
        },
    )

    failed = gateway.run_once(queued["task_id"])

    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "STAGING_TARGET_CONFLICT"
    assert len(responses.calls) == 1
    assert failed["result"]["external_ref"] == "resp_test"


def test_review_staging_receipt_reconciles_ledger_without_second_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
    )
    original_complete = gateway.store.complete_task

    def crash_after_staging(*_args, **_kwargs):
        raise OSError("simulated completion crash")

    monkeypatch.setattr(gateway.store, "complete_task", crash_after_staging)
    with pytest.raises(OSError, match="simulated completion crash"):
        gateway.run_once(queued["task_id"])
    monkeypatch.setattr(gateway.store, "complete_task", original_complete)
    running = gateway.task(queued["task_id"])
    assert running["status"] == "running"
    assert len(running["execution_attempt_token_sha256"]) == 64
    assert len(responses.calls) == 1

    recovered = gateway.recover_review_task(
        queued["task_id"],
        expected_input_manifest_sha256=running["envelope"]["payload"][
            "input_manifest_sha256"
        ],
    )

    assert recovered["status"] == "completed"
    assert recovered["result"]["data"]["reconciled_from_staging"] is True
    assert recovered["result"]["external_ref"] == "resp_test"
    manifest = json.loads(
        (Path(recovered["result"]["data"]["staging_dir"]) / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["execution_attempt_token_sha256"] == running[
        "execution_attempt_token_sha256"
    ]
    assert len(responses.calls) == 1


def test_review_recovery_without_complete_staging_receipt_never_repeats_api(
    tmp_path: Path,
) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
    )
    envelope = gateway.store.claim_task(queued["task_id"], dispatch_owner="gateway")
    assert envelope is not None

    with pytest.raises(GatewayError) as caught:
        gateway.recover_review_task(
            queued["task_id"],
            expected_input_manifest_sha256=envelope.payload["input_manifest_sha256"],
        )

    assert caught.value.code == "GATEWAY_REVIEW_RECOVERY_EVIDENCE_MISSING"
    assert responses.calls == []
    assert gateway.task(queued["task_id"])["status"] == "running"


def test_review_recovery_rejects_receipt_from_a_different_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writing_root = make_writing_root(tmp_path)
    gateway, responses = make_gateway(tmp_path, valid_review())
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model="test-model",
        writing_root=writing_root,
    )
    original_complete = gateway.store.complete_task

    def crash_after_staging(*_args, **_kwargs):
        raise OSError("simulated completion crash")

    monkeypatch.setattr(gateway.store, "complete_task", crash_after_staging)
    with pytest.raises(OSError, match="simulated completion crash"):
        gateway.run_once(queued["task_id"])
    monkeypatch.setattr(gateway.store, "complete_task", original_complete)
    running = gateway.task(queued["task_id"])
    result_path = (
        gateway.staging.task_dir(running["run_id"], running["task_id"])
        / "result.json"
    )
    manifest = json.loads(result_path.read_text(encoding="utf-8"))
    manifest["execution_attempt_token_sha256"] = "0" * 64
    result_path.write_bytes((canonical_json(manifest) + "\n").encode("utf-8"))

    with pytest.raises(StagingError) as caught:
        gateway.recover_review_task(
            queued["task_id"],
            expected_input_manifest_sha256=running["envelope"]["payload"][
                "input_manifest_sha256"
            ],
        )

    assert caught.value.code == "REVIEW_CANARY_STAGING_RECEIPT_INVALID"
    assert gateway.task(queued["task_id"])["status"] == "running"
    assert len(responses.calls) == 1
