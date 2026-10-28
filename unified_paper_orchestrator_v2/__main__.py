from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import __version__
from .api import V2Application, serve
from .config import DATABASE_PATH, DEFAULT_HOST, DEFAULT_PORT, LEGACY_UNIFIED_STATE_PATH, ensure_runtime, read_api_token
from .contracts import TaskEnvelope
from .kernel import WorkflowKernel
from .store import EventStore


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Internal workflow-kernel lab (not the UnifiedPaperOrchestrator product entry)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ui", help="Open the internal kernel lab; this is not the product control panel.")
    sub.add_parser("init", help="Initialize the separate V2 SQLite runtime and local API token.")
    sub.add_parser("status", help="Print V2 runtime, stage, and executor status.")
    sub.add_parser("executors", help="List executor availability without dispatching anything.")

    serve_parser = sub.add_parser("serve", help="Run the loopback JSON API and deterministic worker.")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--no-worker", action="store_true")

    create = sub.add_parser("create-task", help="Create a typed V2 task from a JSON envelope file.")
    create.add_argument("envelope", type=Path)
    dispatch = sub.add_parser("dispatch", help="Run one queued task; no model heartbeat is used.")
    dispatch.add_argument("--task-id", default="")
    approve = sub.add_parser("approve", help="Approve a waiting V2 task.")
    approve.add_argument("task_id")
    approve.add_argument("--note", default="")
    reject = sub.add_parser("reject", help="Reject a waiting V2 task.")
    reject.add_argument("task_id")
    reject.add_argument("--note", default="")
    cancel = sub.add_parser("cancel", help="Cancel a non-terminal V2 task.")
    cancel.add_argument("task_id")
    cancel.add_argument("--note", default="")
    retry = sub.add_parser("retry", help="Explicitly requeue a blocked, retryable, or waiting-human task.")
    retry.add_argument("task_id")
    retry.add_argument("--note", default="")
    events = sub.add_parser("events", help="Read append-only V2 events.")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=200)
    legacy = sub.add_parser("import-legacy", help="Read-only import of a V1 unified_state.json snapshot.")
    legacy.add_argument("path", type=Path, nargs="?", default=LEGACY_UNIFIED_STATE_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {None, "ui"}:
        from .ui import launch_ui

        launch_ui(args.database)
        return 0

    runtime = ensure_runtime()
    store = EventStore(args.database)
    kernel = WorkflowKernel(store)

    if args.command == "init":
        _print({"ok": True, **runtime, "version": __version__})
    elif args.command == "status":
        _print(kernel.status())
    elif args.command == "executors":
        _print({"executors": kernel.registry.describe()})
    elif args.command == "serve":
        app = V2Application(kernel, read_api_token(), run_worker=not args.no_worker)
        print(f"V2 local API listening on http://{args.host}:{args.port}")
        serve(app, args.host, args.port)
    elif args.command == "create-task":
        data = json.loads(args.envelope.read_text(encoding="utf-8-sig"))
        envelope = TaskEnvelope.from_dict(data)
        if envelope.dispatch_owner != "kernel":
            raise ValueError("gateway-owned tasks require the ExecutionGateway surface")
        _print(kernel.create_task(envelope))
    elif args.command == "dispatch":
        if args.task_id:
            record = store.get_task(args.task_id)
            if record.get("dispatch_owner") != "kernel":
                raise ValueError(
                    "gateway-owned tasks require the ExecutionGateway dispatch surface"
                )
        _print({"task": kernel.dispatch_once(args.task_id)})
    elif args.command == "approve":
        _print(kernel.approve(args.task_id, args.note))
    elif args.command == "reject":
        _print(kernel.reject(args.task_id, args.note))
    elif args.command == "cancel":
        _print(kernel.cancel(args.task_id, args.note))
    elif args.command == "retry":
        _print(kernel.retry(args.task_id, args.note))
    elif args.command == "events":
        _print({"events": store.list_events(after=args.after, limit=args.limit)})
    elif args.command == "import-legacy":
        _print(kernel.import_legacy(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
