from __future__ import annotations

import hmac
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .contracts import TaskEnvelope
from .kernel import WorkflowKernel


class WorkerLoop:
    def __init__(self, kernel: WorkflowKernel) -> None:
        self.kernel = kernel
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="v2-dispatcher", daemon=True)
        self.failure_count = 0
        self.last_error_type = ""

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def health(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "alive": self._thread.is_alive(),
            "failure_count": self.failure_count,
            "last_error_type": self.last_error_type,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            progressed = False
            while not self._stop.is_set():
                try:
                    record = self.kernel.dispatch_once()
                except Exception as exc:
                    self.failure_count += 1
                    self.last_error_type = type(exc).__name__
                    break
                if record is None:
                    break
                progressed = True
            if not progressed:
                self._wake.wait(timeout=30)
            self._wake.clear()


class V2Application:
    def __init__(self, kernel: WorkflowKernel, token: str, *, run_worker: bool = True) -> None:
        self.kernel = kernel
        self.token = token
        self.worker = WorkerLoop(kernel) if run_worker else None
        if self.worker:
            self.worker.start()

    def close(self) -> None:
        if self.worker:
            self.worker.stop()

    def wake(self) -> None:
        if self.worker:
            self.worker.wake()

    def health(self) -> dict[str, Any]:
        worker = (
            self.worker.health()
            if self.worker is not None
            else {
                "enabled": False,
                "alive": False,
                "failure_count": 0,
                "last_error_type": "",
            }
        )
        return {
            "ok": self.worker is None or bool(worker["alive"]),
            "service": "unified-paper-orchestrator-v2",
            "worker": worker,
        }


class V2HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: V2Application) -> None:
        self.app = app
        super().__init__(address, V2RequestHandler)


class V2RequestHandler(BaseHTTPRequestHandler):
    server: V2HTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                health = self.server.app.health()
                self._json(
                    HTTPStatus.OK if health["ok"] else HTTPStatus.SERVICE_UNAVAILABLE,
                    health,
                )
                return
            self._require_auth()
            query = parse_qs(parsed.query)
            if parsed.path == "/v2/status":
                self._json(HTTPStatus.OK, self.server.app.kernel.status())
            elif parsed.path == "/v2/executors":
                self._json(HTTPStatus.OK, {"executors": self.server.app.kernel.registry.describe()})
            elif parsed.path == "/v2/tasks":
                status = str(query.get("status", [""])[0])
                limit = int(query.get("limit", [100])[0])
                self._json(HTTPStatus.OK, {"tasks": self.server.app.kernel.store.list_tasks(status=status, limit=limit)})
            elif parsed.path.startswith("/v2/tasks/"):
                task_id = parsed.path.split("/")[3]
                self._json(HTTPStatus.OK, self.server.app.kernel.store.get_task(task_id))
            elif parsed.path == "/v2/events":
                after = int(query.get("after", [0])[0])
                limit = int(query.get("limit", [200])[0])
                self._json(HTTPStatus.OK, {"events": self.server.app.kernel.store.list_events(after=after, limit=limit)})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._require_auth()
            parsed = urlparse(self.path)
            body = self._read_json()
            if parsed.path == "/v2/tasks":
                envelope = TaskEnvelope.from_dict(body)
                if envelope.dispatch_owner != "kernel":
                    raise ValueError(
                        "gateway-owned tasks cannot be created through the direct-kernel API"
                    )
                record = self.server.app.kernel.create_task(envelope)
                self.server.app.wake()
                self._json(HTTPStatus.CREATED, record)
                return
            if parsed.path == "/v2/dispatch/run-once":
                task_id = str(body.get("task_id") or "")
                if task_id:
                    self._require_kernel_owned_task(task_id)
                record = self.server.app.kernel.dispatch_once(task_id)
                self._json(HTTPStatus.OK, {"task": record})
                return
            if parsed.path == "/v2/import/legacy":
                from pathlib import Path

                record = self.server.app.kernel.import_legacy(Path(str(body.get("path") or "")))
                self._json(HTTPStatus.OK, record)
                return
            if parsed.path.startswith("/v2/tasks/"):
                parts = parsed.path.split("/")
                if len(parts) != 5:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                task_id, operation = parts[3], parts[4]
                self._require_kernel_owned_task(task_id)
                note = str(body.get("note") or "")
                if operation == "approve":
                    record = self.server.app.kernel.approve(task_id, note)
                    self.server.app.wake()
                elif operation == "reject":
                    record = self.server.app.kernel.reject(task_id, note)
                elif operation == "cancel":
                    record = self.server.app.kernel.cancel(task_id, note)
                elif operation == "retry":
                    record = self.server.app.kernel.retry(task_id, note)
                    self.server.app.wake()
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                self._json(HTTPStatus.OK, record)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except Exception as exc:
            self._error(exc)

    def _require_auth(self) -> None:
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, self.server.app.token):
            raise PermissionError("valid bearer token required")

    def _require_kernel_owned_task(self, task_id: str) -> None:
        record = self.server.app.kernel.store.get_task(task_id)
        if record.get("dispatch_owner") != "kernel":
            raise ValueError("gateway-owned tasks cannot be mutated through the direct-kernel API")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2 * 1024 * 1024:
            raise ValueError("request body exceeds 2 MiB")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            status = HTTPStatus.UNAUTHORIZED
        elif isinstance(exc, KeyError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, (ValueError, json.JSONDecodeError)):
            status = HTTPStatus.BAD_REQUEST
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._json(status, {"error": type(exc).__name__, "message": str(exc)})

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def serve(app: V2Application, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("V2 foundation server may bind only to loopback")
    server = V2HTTPServer((host, int(port)), app)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        app.close()
