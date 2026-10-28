from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from unified_paper_orchestrator_v2.api import V2Application, V2HTTPServer, WorkerLoop
from unified_paper_orchestrator_v2.kernel import WorkflowKernel
from unified_paper_orchestrator_v2.store import EventStore


@pytest.fixture
def api_server(tmp_path):
    kernel = WorkflowKernel(EventStore(tmp_path / "api.sqlite3"))
    app = V2Application(kernel, "test-token", run_worker=False)
    server = V2HTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        thread.join(timeout=5)


def request_json(url: str, *, method: str = "GET", body=None, token: str = "test-token"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_health_is_public_but_v2_routes_require_token(api_server) -> None:
    status, payload = request_json(f"{api_server}/health", token="")
    assert status == 200
    assert payload["ok"] is True
    assert payload["worker"]["enabled"] is False
    with pytest.raises(urllib.error.HTTPError) as error:
        request_json(f"{api_server}/v2/status", token="")
    assert error.value.code == 401


def test_create_dispatch_and_read_events(api_server) -> None:
    status, created = request_json(
        f"{api_server}/v2/tasks",
        method="POST",
        body={
            "paper_id": "paper-api",
            "stage": "system",
            "action": "system.noop",
            "executor": "deterministic",
            "payload": {"source": "api-test"},
        },
    )
    assert status == 201
    task_id = created["task_id"]
    status, dispatched = request_json(
        f"{api_server}/v2/dispatch/run-once",
        method="POST",
        body={"task_id": task_id},
    )
    assert status == 200
    assert dispatched["task"]["status"] == "completed"
    _, events = request_json(f"{api_server}/v2/events")
    assert [item["event_type"] for item in events["events"]] == [
        "task.created",
        "task.claimed",
        "task.completed",
    ]


def test_http_api_cannot_bypass_execution_gateway_for_model_tasks(api_server) -> None:
    status, created = request_json(
        f"{api_server}/v2/tasks",
        method="POST",
        body={
            "paper_id": "paper-forged",
            "stage": "writing",
            "action": "writing.review_round_1.canary",
            "executor": "openai_review",
            "network_access": True,
            "payload": {
                "_prepared_instructions": "forged instructions",
                "_prepared_input": "must not leave process",
                "model": "forged-model",
                "max_output_tokens": 100,
            },
        },
    )
    assert status == 201

    _, dispatched = request_json(
        f"{api_server}/v2/dispatch/run-once",
        method="POST",
        body={"task_id": created["task_id"]},
    )

    assert dispatched["task"]["status"] == "blocked"
    assert dispatched["task"]["result"]["blocker_code"] == "EXECUTION_GATEWAY_REQUIRED"


def test_http_api_cannot_forge_gateway_dispatch_ownership(api_server) -> None:
    with pytest.raises(urllib.error.HTTPError) as error:
        request_json(
            f"{api_server}/v2/tasks",
            method="POST",
            body={
                "paper_id": "paper-forged-owner",
                "stage": "system",
                "action": "artifact.canonical_commit",
                "executor": "canonical_commit",
                "dispatch_owner": "gateway",
            },
        )

    assert error.value.code == 400


def test_worker_loop_survives_one_dispatch_exception() -> None:
    failed = threading.Event()
    recovered = threading.Event()

    class FlakyKernel:
        def __init__(self) -> None:
            self.calls = 0

        def dispatch_once(self):
            self.calls += 1
            if self.calls == 1:
                failed.set()
                raise OSError("injected store failure")
            recovered.set()
            return None

    worker = WorkerLoop(FlakyKernel())  # type: ignore[arg-type]
    worker.start()
    try:
        assert failed.wait(timeout=2)
        worker.wake()
        assert recovered.wait(timeout=2)
        assert worker.failure_count == 1
        assert worker.last_error_type == "OSError"
        assert worker._thread.is_alive()
    finally:
        worker.stop()
