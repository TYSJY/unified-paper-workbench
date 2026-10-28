from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator_v2.executors.openai_relay import (
    QILIN_RELAY_BASE_URL,
    QILIN_RELAY_PROVIDER_ASSURANCE,
    QILIN_RELAY_PROVIDER_KIND,
    QILIN_RELAY_PROVIDER_PROFILE,
    RELAY_API_KEY_ENV,
    OpenAICompatibleRelayReviewExecutor,
    relay_endpoint_config_sha256,
)
from unified_paper_orchestrator_v2.executors.registry import ExecutorRegistry
from unified_paper_orchestrator_v2.gateway import ExecutionGateway, GatewayError
from unified_paper_orchestrator_v2.review_canary import REQUIRED_HEADINGS
from unified_paper_orchestrator_v2.staging import StagingStore


PAPER_ID = "2026-07-02_001_relay-test"


class _Responses:
    def __init__(
        self,
        output: str,
        *,
        error: Exception | None = None,
        response_id: str = "resp_relay_test",
    ) -> None:
        self.output = output
        self.error = error
        self.response_id = response_id
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            id=self.response_id,
            _request_id="gateway_request_test",
            status="completed",
            output_text=self.output,
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            incomplete_details=None,
        )


class _Client:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses

    def close(self) -> None:
        return None


def _writing_root(tmp_path: Path) -> Path:
    root = tmp_path / "writing"
    (root / "prompts").mkdir(parents=True)
    (root / "ideas").mkdir()
    (root / "refs" / PAPER_ID).mkdir(parents=True)
    (root / "papers" / PAPER_ID).mkdir(parents=True)
    (root / "prompts" / "07_review_round_1.md").write_text(
        "$academic-paper-reviewer\n\n# immutable relay review instructions\n",
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
            parts.append("Add stronger evidence, novelty analysis, fair baselines, and reproducibility details.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None = None,
    response_id: str = "resp_relay_test",
) -> tuple[ExecutionGateway, _Responses]:
    writing_root = _writing_root(tmp_path)
    responses = _Responses(_valid_review(), error=error, response_id=response_id)
    executor = OpenAICompatibleRelayReviewExecutor()
    monkeypatch.setattr(executor, "_client", lambda _task, proxy_url=None: _Client(responses))
    monkeypatch.setenv(RELAY_API_KEY_ENV, "test-only-relay-key")
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
    return gateway, responses


def _approve(gateway: ExecutionGateway, task: dict[str, object]) -> dict[str, object]:
    approval = task["approval"]
    assert isinstance(approval, dict)
    return gateway.approve(
        str(task["task_id"]),
        expected_intent_sha256=str(approval["intent_sha256"]),
        note="explicit third-party disclosure approval",
    )


def test_relay_review_requires_approval_and_writes_only_v2_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, responses = _gateway(tmp_path, monkeypatch)

    queued = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )
    duplicate = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )
    assert duplicate["task_id"] == queued["task_id"]
    assert queued["status"] == "waiting_approval"
    assert queued["approval"]["intent"]["endpoint_base_url"] == QILIN_RELAY_BASE_URL
    assert queued["approval"]["intent"]["third_party_data_disclosure_ack"] is True
    assert responses.calls == []

    with pytest.raises(GatewayError) as blocked:
        gateway.run_once(str(queued["task_id"]))
    assert blocked.value.code == "GATEWAY_TASK_NOT_QUEUED"

    approved = _approve(gateway, queued)
    assert approved["status"] == "queued"
    completed = gateway.run_once(str(queued["task_id"]))
    assert completed is not None and completed["status"] == "completed"
    assert len(responses.calls) == 1
    assert responses.calls[0]["store"] is False
    assert "tools" not in responses.calls[0]

    data = completed["result"]["data"]
    assert data["provider_kind"] == QILIN_RELAY_PROVIDER_KIND
    assert data["provider_profile"] == QILIN_RELAY_PROVIDER_PROFILE
    assert data["provider_assurance"] == QILIN_RELAY_PROVIDER_ASSURANCE
    assert data["endpoint_base_url"] == QILIN_RELAY_BASE_URL
    assert data["endpoint_config_sha256"] == relay_endpoint_config_sha256()
    assert data["gateway_request_id"] == "gateway_request_test"
    assert data["canonical_write"] is False

    staging_dir = Path(data["staging_dir"])
    assert {item.name for item in staging_dir.iterdir()} == {"review_round_1.md", "result.json"}
    manifest = json.loads((staging_dir / "result.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "review_canary_result_v2"
    assert manifest["provider_assurance"] == QILIN_RELAY_PROVIDER_ASSURANCE
    assert manifest["endpoint_config_sha256"] == relay_endpoint_config_sha256()
    assert manifest["canonical_write"] is False

    serialized = json.dumps(completed, ensure_ascii=False)
    serialized += json.dumps(gateway.store.list_events(), ensure_ascii=False)
    assert "test-only-relay-key" not in serialized
    assert "immutable relay review instructions" not in serialized


def test_relay_endpoint_binding_tamper_is_rejected_before_task_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, responses = _gateway(tmp_path, monkeypatch)
    envelope = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )["envelope"]
    tampered = dict(envelope)
    tampered["payload"] = dict(tampered["payload"])
    tampered["payload"]["endpoint_base_url"] = "https://example.invalid"

    from unified_paper_orchestrator_v2.contracts import TaskEnvelope

    with pytest.raises(GatewayError) as caught:
        gateway.submit(TaskEnvelope.from_dict(tampered))
    assert caught.value.code == "GATEWAY_RELAY_ENDPOINT_BINDING_INVALID"
    assert responses.calls == []


def test_relay_timeout_requires_duplicate_charge_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, responses = _gateway(tmp_path, monkeypatch, error=TimeoutError("secret"))
    task = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )
    _approve(gateway, task)
    failed = gateway.run_once(str(task["task_id"]))
    assert failed is not None
    assert failed["status"] == "retryable_failure"
    assert failed["result"]["blocker_code"] == "RELAY_API_TIMEOUT"
    assert len(responses.calls) == 1

    with pytest.raises(GatewayError) as caught:
        gateway.retry(str(task["task_id"]))
    assert caught.value.code == "GATEWAY_REVIEW_RETRY_ACK_REQUIRED"
    retried = gateway.retry(
        str(task["task_id"]),
        acknowledge_possible_duplicate_charge=True,
    )
    assert retried["status"] == "queued"


def test_relay_permission_failure_records_only_safe_provider_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PermissionDeniedError(Exception):
        status_code = 403
        request_id = "req_safe_123"
        body = {
            "error": {
                "code": "model_not_allowed",
                "type": "permission_error",
                "message": "secret provider detail must not be persisted",
            }
        }

    gateway, _responses = _gateway(
        tmp_path,
        monkeypatch,
        error=PermissionDeniedError("secret exception detail"),
    )
    task = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )
    _approve(gateway, task)
    failed = gateway.run_once(str(task["task_id"]))

    assert failed is not None
    assert failed["status"] == "blocked"
    assert failed["result"]["blocker_code"] == "RELAY_API_AUTHORIZATION_FAILED"
    data = failed["result"]["data"]
    assert data["http_status"] == 403
    assert data["provider_request_id"] == "req_safe_123"
    assert data["provider_error_code"] == "model_not_allowed"
    assert data["provider_error_type"] == "permission_error"
    serialized = json.dumps(failed, ensure_ascii=False)
    assert "secret provider detail" not in serialized
    assert "secret exception detail" not in serialized


def test_relay_staging_receipt_recovers_ledger_without_second_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, responses = _gateway(tmp_path, monkeypatch)
    task = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )
    _approve(gateway, task)
    original_complete = gateway.store.complete_task

    def crash_after_staging(*_args, **_kwargs):
        raise OSError("simulated relay ledger completion crash")

    monkeypatch.setattr(gateway.store, "complete_task", crash_after_staging)
    with pytest.raises(OSError, match="simulated relay ledger completion crash"):
        gateway.run_once(str(task["task_id"]))
    monkeypatch.setattr(gateway.store, "complete_task", original_complete)

    running = gateway.task(str(task["task_id"]))
    assert running["status"] == "running"
    assert len(responses.calls) == 1
    recovered = gateway.recover_review_task(
        str(task["task_id"]),
        expected_input_manifest_sha256=running["envelope"]["payload"][
            "input_manifest_sha256"
        ],
    )

    assert recovered["status"] == "completed"
    assert recovered["result"]["data"]["reconciled_from_staging"] is True
    assert recovered["result"]["data"]["provider_assurance"] == QILIN_RELAY_PROVIDER_ASSURANCE
    assert recovered["result"]["data"]["endpoint_config_sha256"] == relay_endpoint_config_sha256()
    assert recovered["result"]["external_ref"] == "resp_relay_test"
    assert len(responses.calls) == 1


@pytest.mark.parametrize("response_id", ["", "offline_replay_123", "mock-response"])
def test_production_relay_rejects_missing_or_offline_response_id_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_id: str,
) -> None:
    gateway, responses = _gateway(
        tmp_path,
        monkeypatch,
        response_id=response_id,
    )
    task = gateway.submit_relay_review_canary(
        PAPER_ID,
        model="gpt-5.5",
        writing_root=tmp_path / "writing",
    )
    _approve(gateway, task)
    failed = gateway.run_once(str(task["task_id"]))

    assert failed is not None
    assert failed["status"] == "fatal_failure"
    assert failed["result"]["blocker_code"] == "REVIEW_CANARY_RESPONSE_ID_INVALID"
    assert len(responses.calls) == 1
    assert not (tmp_path / "staging").exists()


def test_relay_never_falls_back_to_official_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "official-key-must-not-be-used")
    monkeypatch.delenv(RELAY_API_KEY_ENV, raising=False)
    executor = OpenAICompatibleRelayReviewExecutor()
    assert executor.descriptor().available is False
    assert RELAY_API_KEY_ENV in executor.descriptor().detail


def test_relay_sdk_client_suppresses_all_implicit_official_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    captured_headers: dict[str, str] = {}
    real_http_client = httpx.Client

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured_headers.update(
            {str(key).casefold(): str(value) for key, value in request.headers.items()}
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_header_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "mock",
                "output": [],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    class IsolatedHTTPClient(real_http_client):
        def __init__(self, **options):
            options["transport"] = httpx.MockTransport(capture_request)
            super().__init__(**options)

    monkeypatch.setenv(RELAY_API_KEY_ENV, "relay-only-key")
    monkeypatch.setenv("OPENAI_API_KEY", "official-key-must-not-be-used")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-key-must-not-be-used")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-must-not-leak")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "project-must-not-leak")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "authorization: Bearer overridden-official\nX-Private-Header: must-not-leak",
    )
    monkeypatch.delenv("UNIFIED_RELAY_HTTPS_PROXY", raising=False)
    monkeypatch.setattr(httpx, "Client", IsolatedHTTPClient)
    from unified_paper_orchestrator_v2.contracts import TaskEnvelope

    task = TaskEnvelope(
        paper_id=PAPER_ID,
        stage="writing",
        action="writing.review_round_1.compatible_relay_canary",
        executor="openai_compatible_relay_review",
        timeout_seconds=10,
    )
    client = OpenAICompatibleRelayReviewExecutor()._client(task)
    try:
        client.responses.create(
            model="mock",
            input="header isolation probe",
            store=False,
            max_output_tokens=1,
        )
        assert captured_headers["authorization"] == "Bearer relay-only-key"
        assert "openai-organization" not in captured_headers
        assert "openai-project" not in captured_headers
        assert "x-private-header" not in captured_headers
    finally:
        client.close()
