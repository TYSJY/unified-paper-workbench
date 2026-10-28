from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .workspace import bootstrap_process_workspace

bootstrap_process_workspace(sys.argv[1:])

from . import __version__  # noqa: E402
from .config import LEGACY_PROJECTS  # noqa: E402
from .orchestrator import UnifiedOrchestrator  # noqa: E402
from .runtime_budget_gate import (  # noqa: E402
    evaluate_runtime_budget_files,
    launch_runtime_budget_files,
)
from .writing_evidence_gate import validate_writing_evidence  # noqa: E402
from .writing_evidence_import import prepare_writing_evidence_import  # noqa: E402
from .writing_novelty_gate import validate_writing_novelty  # noqa: E402


def _compact_smart_cli_payload(value):
    """Keep Smart CLI output operationally useful without dumping state/history."""
    if isinstance(value, dict):
        if value.get("schema") == "smart-submission-program-control/v1":
            candidates = value.get("dispatch_candidates", [])
            compact_candidates = []
            for item in candidates if isinstance(candidates, list) else []:
                if not isinstance(item, dict):
                    continue
                compact_candidates.append(
                    {
                        key: item.get(key)
                        for key in (
                            "paper_id",
                            "phase",
                            "action",
                            "executor",
                            "fallback_executor",
                            "route_revision",
                            "attempt_id",
                            "thread_id",
                            "credential_account_id",
                            "submission_system_id",
                            "target_origin",
                            "dispatch_binding_ready",
                            "target_journal",
                        )
                    }
                )
            ledger = value.get("task_ledger", {})
            return {
                "schema": value.get("schema"),
                "plan_id": value.get("plan_id"),
                "control_plane": value.get("control_plane"),
                "generated_at": value.get("generated_at"),
                "mode": value.get("mode"),
                "max_parallel_workers": value.get("max_parallel_workers"),
                "active_worker_count": value.get("active_worker_count"),
                "available_slots": value.get("available_slots"),
                "dispatch_candidate_ids": value.get("dispatch_candidate_ids", []),
                "dispatch_candidates": compact_candidates,
                "local_action_count": len(
                    value.get("local_actions", [])
                    if isinstance(value.get("local_actions"), list)
                    else []
                ),
                "online_control_action_count": len(
                    value.get("online_control_actions", [])
                    if isinstance(value.get("online_control_actions"), list)
                    else []
                ),
                "monitor_candidate_count": len(
                    value.get("monitor_candidates", [])
                    if isinstance(value.get("monitor_candidates"), list)
                    else []
                ),
                "program_only_count": value.get("program_only_count"),
                "model_required_count": value.get("model_required_count"),
                "dispatch_binding_blocked_ids": value.get("dispatch_binding_blocked_ids", []),
                "plan_path": value.get("plan_path"),
                "persisted": value.get("persisted"),
                "task_ledger": {
                    "database_path": ledger.get("database_path"),
                    "task_count": ledger.get("task_count", 0),
                    "side_effects_dispatched": ledger.get("side_effects_dispatched", False),
                    "deferred": ledger.get("deferred", False),
                }
                if isinstance(ledger, dict)
                else {},
            }
        if value.get("module") == "smart_submission_sidecar" and "papers" in value:
            papers = value.get("papers") if isinstance(value.get("papers"), dict) else {}
            return {
                "version": value.get("version"),
                "module": value.get("module"),
                "mode": value.get("mode"),
                "paper_count": len(papers),
                "local_queue_count": len(
                    value.get("paper_queue", []) if isinstance(value.get("paper_queue"), list) else []
                ),
                "online_queue_count": len(
                    value.get("online_queue", []) if isinstance(value.get("online_queue"), list) else []
                ),
                "updated_at": value.get("updated_at", ""),
            }
        return {
            str(key): _compact_smart_cli_payload(item)
            for key, item in value.items()
            if str(key) != "state" and not str(key).startswith("history")
        }
    if isinstance(value, list):
        return [_compact_smart_cli_payload(item) for item in value]
    return value


def _smart_cli_result_failed(result: dict[str, Any]) -> bool:
    if result.get("ok") is False:
        return True
    if str(result.get("status") or "").strip().lower() == "blocked":
        return True
    if str(result.get("code") or "").startswith("AI_"):
        return True

    def guard_codes(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [code for item in value.values() for code in guard_codes(item)]
        if isinstance(value, list):
            return [code for item in value for code in guard_codes(item)]
        text = str(value or "")
        return [text] if text.startswith(("SMART_ROUTE_", "SMART_CANARY_")) else []

    codes = guard_codes(result.get("skipped", {})) + guard_codes(result.get("online_skipped", {}))
    successes = any(
        bool(result.get(key))
        for key in (
            "applied",
            "imported",
            "moved",
            "online_recovered",
            "promoted",
            "queued",
            "released",
            "requeued",
        )
    )
    return bool(codes) and not successes


def _run_smart_cli_dict(callback: Callable[[], dict[str, Any]]) -> int:
    from .smart_submission import SmartSubmissionGuardError

    try:
        result = callback()
    except SmartSubmissionGuardError as exc:
        result = exc.as_dict()
    except ValueError as exc:
        result = {"ok": False, "code": "SMART_CLI_VALIDATION_ERROR", "message": str(exc)}
    except Exception as exc:
        result = {"ok": False, "code": "SMART_CLI_COMMAND_FAILED", "message": str(exc)}
    print(json.dumps(_compact_smart_cli_payload(result), ensure_ascii=False, indent=2))
    return 2 if _smart_cli_result_failed(result) else 0


def _compact_gateway_record(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {"status": "idle", "message": "No queued gateway task was dispatched."}
    return {
        key: record.get(key)
        for key in (
            "task_id",
            "run_id",
            "paper_id",
            "stage",
            "action",
            "executor",
            "status",
            "attempt_count",
            "created_at",
            "updated_at",
            "approval",
            "recovery_intent",
            "result",
        )
        if key in record
    }


def _gateway_exit_code(record: dict[str, Any] | None) -> int:
    status = str((record or {}).get("status") or "idle")
    if status in {"queued", "completed", "idle"}:
        return 0
    if status in {"blocked", "waiting_approval", "waiting_human"}:
        return 2
    return 1


def _execution_control_error(exc: BaseException) -> int:
    code = str(getattr(exc, "code", "") or "EXECUTION_CONTROL_FAILED")
    messages = {
        "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE": ("当前是 API 密钥模式；该操作需要先显式切换到 Codex 账号模式（完整流程）。"),
        "AI_EXECUTION_MODE_STATE_UNREADABLE": "AI 执行模式不可读，本次操作已停止。",
        "LEGACY_CODEX_AUTOMATION_UNAVAILABLE": ("未检测到可由 EXE 控制的旧版 Codex Automation；没有写入旧项目状态。"),
        "CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED": ("请先在 EXE 的“AI执行与API”窗口中连接 ChatGPT 账号。"),
        "WRITING_LEGACY_AUTOMATION_ACTIVE": ("旧写论文自动任务仍在运行；请先暂停，避免重复派发。"),
    }
    payload = {
        "ok": False,
        "code": code,
        "message": messages.get(code, "执行控制操作失败；未报告底层异常内容。"),
        "error_type": type(exc).__name__,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def _run_writing_dispatch_cli(callback: Callable[[], dict[str, Any]]) -> int:
    from .writing_dispatch import WritingDispatchError

    try:
        result = callback()
    except WritingDispatchError as exc:
        result = {"ok": False, "code": exc.code, "message": str(exc)}
    except Exception as exc:
        result = {
            "ok": False,
            "code": "WRITING_DISPATCH_COMMAND_FAILED",
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified paper workflow orchestrator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--workspace",
        default="",
        help="Use this managed paper workspace for the current process.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    word_export = sub.add_parser("undergraduate-word-export", help="Generate and render the undergraduate Word delivery locally.")
    word_export.add_argument("--root", required=True)
    word_export.add_argument("--paper-id", required=True)
    word_export.add_argument("--render-existing", action="store_true")
    sub.add_parser(
        "distribution-status",
        help="Print the immutable edition policy and public trust-key count.",
    )
    sub.add_parser("workspace-status", help="Show the active paper workspace and registered workspaces.")
    sub.add_parser("workspace-list", help="List registered paper workspaces.")
    workspace_create = sub.add_parser(
        "workspace-create",
        help="Create a managed paper workspace from the current workspace templates or data.",
    )
    workspace_create.add_argument("path")
    workspace_create.add_argument("--name", required=True)
    workspace_create.add_argument(
        "--import-current",
        action="store_true",
        help="Copy current papers, state and runtime data without modifying the source directories.",
    )
    workspace_create.add_argument(
        "--activate",
        action="store_true",
        help="Make the new workspace the default for the next EXE start.",
    )
    workspace_use = sub.add_parser("workspace-use", help="Select an existing managed workspace for future starts.")
    workspace_use.add_argument("path")
    sub.add_parser("sync", help="Read four legacy projects and regenerate unified state/summary.")

    queue = sub.add_parser("queue", help="Add paper IDs to the unified queue.")
    queue.add_argument("paper_ids", nargs="+")

    sub.add_parser("clear-queue", help="Clear the unified queue.")

    advance = sub.add_parser("advance", help="Plan or apply one master-scheduler action.")
    advance.add_argument("--apply", action="store_true", help="Write back to the matching legacy project.")
    advance.add_argument("--paper-id", default="", help="Override the queue head for this one action.")

    new_writing = sub.add_parser(
        "new-writing-session", help="Queue a new writing session and start the writing scheduler."
    )
    new_writing.add_argument("--single-active", action="store_true", help="Pause the other stage schedulers.")
    new_writing.add_argument("--count", type=int, default=1, help="Number of explicitly requested papers to create.")

    sub.add_parser("pause-schedulers", help="Pause all four legacy scheduler automations.")
    sub.add_parser("scheduler-status", help="Print status for the four legacy scheduler automations.")
    sub.add_parser(
        "ai-execution-status",
        help="Report the active AI mode and secret-free SDK/runtime readiness.",
    )
    sub.add_parser(
        "codex-account-status",
        help="Check the EXE-owned Codex SDK account without exposing credentials.",
    )
    sub.add_parser(
        "writing-sdk-status",
        help="Show the local event-driven writing dispatcher and queued plan status.",
    )
    writing_sdk_run = sub.add_parser(
        "writing-sdk-run",
        help="Run the local event-driven writing dispatcher in this process until idle.",
        description="Run the local event-driven writing dispatcher in this process until idle.",
    )
    writing_sdk_run.add_argument(
        "--paper-key",
        action="append",
        default=None,
        help="Run only this exact SDK paper key. Repeat to select more than one plan.",
    )
    writing_sdk_run.add_argument(
        "--until-step",
        type=int,
        default=None,
        help="Temporary run cap from 1 to 10; requires at least one --paper-key.",
    )
    writing_sdk_requeue = sub.add_parser(
        "writing-sdk-requeue-recoverable",
        help="Explicitly requeue only proven-safe blocked or capacity-waiting SDK records.",
    )
    writing_sdk_requeue.add_argument(
        "--paper-key",
        action="append",
        default=None,
        help="Recover only this exact SDK paper key. Repeat to select more than one plan.",
    )
    writing_sdk_requeue.add_argument(
        "--replace-failed-step1-threads",
        action="store_true",
        help="Replace failed unnamed Step 1 threads after their exact IDs were archived.",
    )
    writing_sdk_requeue.add_argument(
        "--archived-thread-id",
        action="append",
        default=None,
        help="Acknowledge one exact SDK thread ID already archived by the operator.",
    )
    writing_sdk_revalidate = sub.add_parser(
        "writing-sdk-revalidate-last-completed",
        help="Revalidate and safely reopen only one exact last completed SDK writing step.",
    )
    writing_sdk_revalidate.add_argument(
        "--paper-key",
        default="",
        help="Exact SDK paper key to inspect; this command never dispatches a model turn.",
    )
    start_stage = sub.add_parser("start-stage", help="Start one stage scheduler without pausing the others by default.")
    start_stage.add_argument("stage", choices=["writing", "figures", "journal_author"])
    start_stage.add_argument("--single-active", action="store_true", help="Pause the other stage schedulers.")
    pause_stage = sub.add_parser("pause-stage", help="Pause one stage scheduler.")
    pause_stage.add_argument("stage", choices=["writing", "figures", "journal_author"])
    writing_gate = sub.add_parser(
        "writing-evidence-gate",
        help="Read-only Step 5 to Step 6 evidence validation for one writing paper.",
    )
    writing_gate.add_argument("paper_id")
    writing_import = sub.add_parser(
        "writing-evidence-import",
        help="Check or atomically create a Step 5 schema-v2 evidence manifest from a declaration spec.",
    )
    writing_import.add_argument("paper_id")
    writing_import.add_argument(
        "--spec",
        required=True,
        help="Paper-relative JSON import spec; absolute paths must remain inside the selected paper directory.",
    )
    writing_import.add_argument(
        "--check-only",
        action="store_true",
        help="Validate and hash stable inputs without writing a receipt or manifest.",
    )
    novelty_gate = sub.add_parser(
        "writing-novelty-gate",
        help="Read-only Step 2 to Step 3 full-text novelty validation for one writing paper.",
    )
    novelty_gate.add_argument("paper_id")
    sub.add_parser(
        "writing-parallel-prepare",
        help="Reconcile and atomically reserve available visible writing-worker slots.",
    )
    sub.add_parser(
        "writing-parallel-status",
        help="Read the configured writing parallel limit and active claims.",
    )
    mark_writing_send = sub.add_parser(
        "writing-parallel-mark-uncertain",
        help="Fence one reserved claim immediately before a Desktop send/create tool call.",
    )
    mark_writing_send.add_argument("attempt_id")
    mark_writing_send.add_argument("--token", required=True)
    record_writing_send = sub.add_parser(
        "writing-parallel-record-dispatch",
        help="Bind the exact returned Desktop thread id to a fenced writing claim.",
    )
    record_writing_send.add_argument("attempt_id")
    record_writing_send.add_argument("--token", required=True)
    record_writing_send.add_argument("--thread-id", required=True)
    release_writing = sub.add_parser(
        "writing-parallel-release",
        help="Release a definitely unsent reservation; uncertain sends cannot be released.",
    )
    release_writing.add_argument("attempt_id")
    release_writing.add_argument("--token", required=True)
    reconcile_adopt = sub.add_parser(
        "writing-parallel-reconcile-adopt",
        help="While paused, adopt an exact terminal Step 1 for an uncertain create claim.",
    )
    reconcile_adopt.add_argument("attempt_id")
    reconcile_adopt.add_argument("--paper-id", required=True)
    reconcile_adopt.add_argument("--thread-id", required=True)
    reconcile_unsent = sub.add_parser(
        "writing-parallel-reconcile-unsent",
        help="While paused, prove an uncertain continue send was not delivered.",
    )
    reconcile_unsent.add_argument("attempt_id")
    runtime_budget = sub.add_parser(
        "runtime-budget-check",
        help="Read-only preflight for a declared workload and its recent calibration.",
    )
    runtime_budget.add_argument("workload", type=Path, help="Declarative workload JSON file.")
    runtime_budget.add_argument("calibration", type=Path, help="Recent 5-10 minute calibration JSON file.")
    runtime_budget.add_argument(
        "binding",
        type=Path,
        nargs="?",
        help="Current host/command/execution-shape binding JSON; omission is fail-closed.",
    )
    runtime_launch = sub.add_parser(
        "runtime-budget-launch",
        help="Gate and launch the exact command recorded in a current runtime binding.",
    )
    runtime_launch.add_argument("workload", type=Path, help="Declarative workload JSON file.")
    runtime_launch.add_argument("calibration", type=Path, help="Recent 5-10 minute calibration JSON file.")
    runtime_launch.add_argument("binding", type=Path, help="Current host/command/execution-shape binding JSON.")
    sub.add_parser(
        "gateway-status",
        help="Show the explicit-dispatch multi-executor gateway without starting a worker.",
    )
    review_canary = sub.add_parser(
        "gateway-review-canary",
        help="Create a staging-only OpenAI review task; add --execute for one explicit API request.",
    )
    review_canary.add_argument("paper_id", help="Existing writing paper ID with Prompt 07 inputs.")
    review_canary.add_argument(
        "--model", default="", help="Explicit Responses API model; otherwise OPENAI_MODEL is used."
    )
    review_canary.add_argument("--execute", action="store_true", help="Dispatch this task once after creating it.")
    review_canary.add_argument("--token-budget", type=int, default=50000)
    review_canary.add_argument("--max-output-tokens", type=int, default=8000)
    review_canary.add_argument("--timeout-seconds", type=int, default=600)
    relay_review_canary = sub.add_parser(
        "gateway-relay-review-canary",
        help=(
            "Create a required-approval staging-only review task for the pinned third-party Responses-compatible relay."
        ),
    )
    relay_review_canary.add_argument(
        "paper_id",
        help="Existing writing paper ID with Prompt 07 inputs.",
    )
    relay_review_canary.add_argument(
        "--model",
        default="gpt-5.5",
        help="Relay model claim recorded in the durable task envelope (default: gpt-5.5).",
    )
    relay_review_canary.add_argument("--token-budget", type=int, default=50000)
    relay_review_canary.add_argument("--max-output-tokens", type=int, default=8000)
    relay_review_canary.add_argument("--timeout-seconds", type=int, default=600)
    codex_relay_review_canary = sub.add_parser(
        "gateway-codex-relay-review-canary",
        help=(
            "Create a required-approval review task using a standalone, isolated "
            "Codex CLI with the pinned third-party relay."
        ),
    )
    codex_relay_review_canary.add_argument(
        "paper_id",
        help="Existing writing paper ID with Prompt 07 inputs.",
    )
    codex_relay_review_canary.add_argument(
        "--model",
        default="gpt-5.5",
        help="Explicit relay model bound into approval (default: gpt-5.5).",
    )
    codex_relay_review_canary.add_argument(
        "--codex-cli-path",
        type=Path,
        default=None,
        help=(
            "Absolute standalone native codex executable; Desktop/WindowsApps "
            "binaries and the user .codex tree are rejected."
        ),
    )
    codex_relay_review_canary.add_argument("--token-budget", type=int, default=50000)
    codex_relay_review_canary.add_argument(
        "--max-output-tokens",
        type=int,
        default=8000,
        help="Approved post-hoc CLI output limit; Codex CLI has no server-side hard cap.",
    )
    codex_relay_review_canary.add_argument("--timeout-seconds", type=int, default=600)
    gateway_dispatch = sub.add_parser(
        "gateway-dispatch",
        help="Explicitly dispatch one already queued gateway task; no automatic retry is performed.",
    )
    gateway_dispatch.add_argument("task_id")
    gateway_task = sub.add_parser("gateway-task", help="Show one compact gateway task record by ID.")
    gateway_task.add_argument("task_id")
    gateway_retry = sub.add_parser(
        "gateway-retry",
        help="Explicitly requeue one blocked or retryable gateway task; it does not dispatch it.",
    )
    gateway_retry.add_argument("task_id")
    gateway_retry.add_argument(
        "--acknowledge-possible-duplicate-charge",
        action="store_true",
        help=("Required before retrying an ambiguous review API outcome that may already have incurred a charge."),
    )
    gateway_approve = sub.add_parser(
        "gateway-approve",
        help="Approve one waiting gateway task; approval never dispatches it automatically.",
    )
    gateway_approve.add_argument("task_id")
    gateway_approve.add_argument("--note", default="")
    gateway_approve.add_argument(
        "--intent-sha256",
        default="",
        help="Required acknowledgment digest shown in the gateway task approval block.",
    )
    recover_review = sub.add_parser(
        "gateway-recover-review",
        help="Reconcile one running review from a verified staging receipt without another API call.",
    )
    recover_review.add_argument("task_id")
    recover_review.add_argument("--input-manifest-sha256", required=True)
    recover_automation = sub.add_parser(
        "gateway-recover-automation",
        help="Reconcile a running Automation task by read-only desired-state proof.",
    )
    recover_automation.add_argument("task_id")
    recover_automation.add_argument("--intent-sha256", default="")
    legacy_automation = sub.add_parser(
        "gateway-legacy-automation",
        help="Create a controlled legacy Codex Automation status/start/pause task.",
    )
    legacy_automation.add_argument("operation", choices=["status", "start", "pause"])
    legacy_automation.add_argument("stage", choices=["writing", "figures", "journal_author", "submission"])
    legacy_automation.add_argument("--operation-id", default="")
    legacy_automation.add_argument(
        "--execute",
        action="store_true",
        help="Execute a queued read-only status task; control tasks still require separate approval.",
    )
    sub.add_parser("smart-submission-init", help="Initialize the independent smart submission sidecar.")
    smart_status = sub.add_parser("smart-submission-status", help="Print smart submission sidecar status.")
    smart_status.add_argument(
        "--with-browser-health", action="store_true", help="Also run the slower Chrome/CDP/extension health checks."
    )
    smart_browser_mcp = sub.add_parser(
        "smart-browser-mcp-serve",
        help="Serve the project-owned constrained Smart Submission browser tools over MCP stdio.",
    )
    smart_browser_mcp.add_argument(
        "--context-store",
        required=True,
        help="Absolute path to the short-lived Smart browser capability store.",
    )
    smart_sdk_run = sub.add_parser(
        "smart-submission-sdk-run",
        help="Run one program-planned Smart Submission sweep through fixed Codex SDK sessions.",
    )
    smart_sdk_run.add_argument(
        "paper_ids",
        nargs="*",
        help="Optional paper IDs; default dispatches every currently eligible Smart paper.",
    )
    sub.add_parser(
        "smart-submission-sdk-status",
        help="Print fixed-session Codex SDK Smart Submission runtime status.",
    )
    sub.add_parser(
        "smart-submission-sdk-pause",
        help="Stop new Smart Submission SDK turns after currently running turns finish.",
    )
    smart_program_plan = sub.add_parser(
        "smart-submission-program-plan",
        help="Build the deterministic Smart Submission state/action/parallel-slot plan.",
    )
    smart_program_plan.add_argument(
        "paper_ids",
        nargs="*",
        help="Optional paper IDs; default projects every Smart Submission paper.",
    )
    smart_program_plan.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write program_dispatch_plan.json.",
    )
    smart_program_plan.add_argument(
        "--no-ledger",
        action="store_true",
        help="Do not synchronize scheduled actions into the isolated program-control SQLite ledger.",
    )
    smart_probe = sub.add_parser(
        "smart-submission-probe-session",
        help="Probe and audit one signed, value-free exact-origin browser session.",
    )
    smart_probe.add_argument("account_id", help="Exact Smart journal-system account ID.")
    smart_probe.add_argument("--system-id", required=True, help="Exact verified submission-system ID.")
    smart_probe.add_argument("--origin", required=True, help="Exact verified HTTP(S) target origin or URL.")
    smart_probe.add_argument(
        "--port",
        action="append",
        type=int,
        choices=[9224, 9222],
        help="Approved loopback CDP port; repeat to set order. Default: 9224 then 9222.",
    )
    smart_browser = sub.add_parser(
        "smart-submission-browser-health", help="Check or restore Smart Submission browser/CDP channels."
    )
    smart_browser.add_argument("--ensure", action="store_true", help="Start/restore CDP browser ports when missing.")
    smart_browser_cleanup = sub.add_parser(
        "smart-submission-browser-cleanup", help="List or close safe Smart Submission browser tabs."
    )
    smart_browser_cleanup.add_argument(
        "--close", action="store_true", help="Close safe blank and exact duplicate non-target submission tabs."
    )
    smart_browser_cleanup.add_argument(
        "--include-stale-submission-tabs",
        action="store_true",
        help="Also include non-current submission-system tabs. Intended for explicit manual cleanup, not default scheduling.",
    )
    smart_run = sub.add_parser("smart-submission-run", help="Run one smart submission sidecar scheduler sweep.")
    smart_run.add_argument("--force", action="store_true", help="Run even if smart submission mode is manual.")
    smart_run.add_argument("--max-papers", type=int, default=0, help="Limit the number of queued papers processed.")
    sub.add_parser(
        "smart-submission-program-tick",
        help="Run one program-owned Smart Submission SDK follow-up or daily-monitor tick.",
    )
    sub.add_parser(
        "smart-submission-automation-tick",
        help="Deprecated alias for smart-submission-program-tick.",
    )
    sub.add_parser(
        "smart-submission-program-timer-reconcile",
        help="Retire the Codex Automation trigger and register the current packaged EXE as the program timer.",
    )
    smart_batch = sub.add_parser(
        "smart-submission-batch-check",
        help="Batch import and local-check smart submission papers without dispatching online workers by default.",
    )
    smart_batch.add_argument("paper_ids", nargs="*", help="Optional paper IDs; default checks all eligible papers.")
    smart_batch.add_argument(
        "--no-online-queue", action="store_true", help="Do not add online-ready papers to online_queue."
    )
    smart_batch.add_argument(
        "--activate-online", action="store_true", help="Set mode=run_once after queueing online-ready papers."
    )
    smart_batch.add_argument(
        "--max-papers", type=int, default=0, help="Limit the number of local queued papers preflighted."
    )
    smart_batch.add_argument(
        "--expected-route-revision", type=int, help="Required for the single-paper online canary path."
    )
    smart_mode = sub.add_parser("smart-submission-mode", help="Set smart submission mode.")
    smart_mode.add_argument("mode", choices=["manual", "run_once", "scheduled", "monitor_only"])
    sub.add_parser(
        "smart-submission-wakeup", help="Compatibility command that directly starts the program-owned SDK runtime."
    )
    smart_restore_daily = sub.add_parser(
        "smart-submission-restore-daily",
        help="Enable the program-owned Smart Submission timer at its configured local daily time.",
    )
    smart_restore_daily.add_argument(
        "--time",
        dest="time_local",
        help="Optional local HH:MM time to save before restoring daily monitoring.",
    )
    smart_import = sub.add_parser("smart-submission-import", help="Import package-ready papers into smart submission.")
    smart_import.add_argument("paper_ids", nargs="*", help="Optional paper IDs; default imports all eligible papers.")
    smart_online = sub.add_parser(
        "smart-submission-start-visible-online", help="Start one preflight-first Smart canary."
    )
    smart_online.add_argument("paper_id", help="Exactly one paper ID.")
    smart_online.add_argument(
        "--expected-route-revision", required=True, type=int, help="Unified submission route CAS revision."
    )
    smart_priority = sub.add_parser(
        "smart-submission-prioritize", help="Move paper IDs to the front of the smart submission online queue."
    )
    smart_priority.add_argument("paper_ids", nargs="+")
    smart_priority.add_argument(
        "--no-activate", action="store_true", help="Do not switch mode to run_once or wake the scheduler."
    )
    smart_move = sub.add_parser(
        "smart-submission-move-online", help="Move one paper inside the smart submission online queue."
    )
    smart_move.add_argument("paper_id")
    smart_move.add_argument("direction", choices=["up", "down", "top", "bottom"])
    smart_move.add_argument(
        "--activate", action="store_true", help="Switch mode to run_once and wake the scheduler after moving."
    )
    smart_plan_github = sub.add_parser(
        "smart-submission-apply-planned-github", help="Record planned GitHub repo URLs and requeue papers."
    )
    smart_plan_github.add_argument(
        "paper_ids", nargs="*", help="Optional paper IDs; default applies to detected GitHub blockers."
    )
    smart_plan_github.add_argument(
        "--no-activate", action="store_true", help="Do not switch mode to run_once or wake the scheduler."
    )
    smart_declarations = sub.add_parser(
        "smart-submission-apply-standing-declarations",
        help="Apply standing AI-use/author declarations and requeue papers.",
    )
    smart_declarations.add_argument(
        "paper_ids", nargs="*", help="Optional paper IDs; default applies to detected declaration blockers."
    )
    smart_declarations.add_argument(
        "--no-activate", action="store_true", help="Do not switch mode to run_once or wake the scheduler."
    )
    smart_recover = sub.add_parser(
        "smart-submission-recover-actionable", help="Recover auto-actionable waiting/blocked smart-submission papers."
    )
    smart_recover.add_argument(
        "paper_ids", nargs="*", help="Optional paper IDs; default recovers all auto-actionable papers."
    )
    smart_recover.add_argument(
        "--no-activate", action="store_true", help="Do not switch mode to run_once or wake the scheduler."
    )
    smart_approval = sub.add_parser(
        "smart-submission-pause-waiting-approval",
        help="Mark visible workers waiting for command approval and pause fast scheduling.",
    )
    smart_approval.add_argument(
        "paper_ids",
        nargs="*",
        help="Optional paper IDs; default uses the latest waitingOnApproval scheduler history item.",
    )
    smart_requeue_approval = sub.add_parser(
        "smart-submission-requeue-waiting-approval",
        help="Release waitingOnApproval workers and requeue the affected papers.",
    )
    smart_requeue_approval.add_argument(
        "paper_ids", nargs="*", help="Optional paper IDs; default requeues all papers waiting for command approval."
    )
    smart_requeue_approval.add_argument(
        "--no-activate", action="store_true", help="Do not switch mode to run_once or wake the scheduler."
    )
    smart_worker_prompt = sub.add_parser(
        "smart-submission-worker-prompt",
        help="Print the exact context-rich prompt for one visible smart-submission worker.",
    )
    smart_worker_prompt.add_argument("paper_id")
    smart_worker_prompt.add_argument("--route-revision", required=True, type=int)
    smart_claim_final = sub.add_parser(
        "smart-submission-claim-final-submit-action",
        help="Atomically consume one reserved final-submit receipt immediately before one click.",
    )
    smart_claim_final.add_argument("paper_id")
    smart_claim_final.add_argument("--route-revision", required=True, type=int)
    smart_claim_final.add_argument("--attempt-id", required=True)
    smart_claim_final.add_argument("--thread-id", required=True)
    smart_claim_final.add_argument("--authorization-id", required=True)
    smart_capture_confirmation = sub.add_parser(
        "smart-submission-capture-confirmation",
        help="Capture signed positive post-submit evidence from approved local CDP.",
    )
    smart_capture_confirmation.add_argument("paper_id")
    smart_capture_confirmation.add_argument("--route-revision", required=True, type=int)
    smart_capture_confirmation.add_argument("--attempt-id", required=True)
    smart_capture_confirmation.add_argument("--thread-id", required=True)
    smart_capture_confirmation.add_argument("--authorization-id", required=True)
    smart_record_dispatch = sub.add_parser(
        "smart-submission-record-worker-dispatch", help="Atomically claim one queued Smart canary for a visible worker."
    )
    smart_record_dispatch.add_argument("paper_id")
    smart_record_dispatch.add_argument("--thread-id", required=True)
    smart_record_dispatch.add_argument("--route-revision", required=True, type=int)
    smart_record_dispatch.add_argument("--attempt-id", required=True)
    smart_record_worker = sub.add_parser(
        "smart-submission-record-worker-result",
        help="Record visible-worker final SMART_SUBMISSION markers and release the scheduler slot.",
    )
    smart_record_worker.add_argument("paper_id")
    smart_record_worker.add_argument("--route-revision", required=True, type=int)
    smart_record_worker.add_argument("--attempt-id", required=True)
    smart_record_worker.add_argument("--status", required=True, help="Worker final SMART_SUBMISSION_STATUS.")
    smart_record_worker.add_argument(
        "--next", default="", dest="next_action", help="Worker final SMART_SUBMISSION_NEXT."
    )
    smart_record_worker.add_argument("--detail", default="", help="Compact detail or blocker text.")
    smart_record_worker.add_argument("--thread-id", required=True, help="Visible worker thread id.")
    smart_record_worker.add_argument("--source", default="visible_worker_final", help="Audit source label.")
    smart_record_worker.add_argument("--submission-id", default="", help="Verified journal manuscript/submission id.")
    smart_record_worker.add_argument(
        "--evidence-path", default="", help="Existing local confirmation/email evidence file."
    )
    smart_record_worker.add_argument("--confirmation-url", default="", help="HTTP(S) confirmation URL.")
    smart_record_worker.add_argument(
        "--keep-queued",
        action="store_true",
        help="Keep or return the paper to online_queue instead of treating the status as terminal.",
    )
    smart_record_worker.add_argument(
        "--no-activate", action="store_true", help="Do not switch mode to run_once or wake the scheduler."
    )
    smart_thread = sub.add_parser(
        "smart-submission-set-scheduler-thread", help="Bind smart submission to a visible scheduler thread."
    )
    smart_thread.add_argument("thread_id")
    delete = sub.add_parser("delete", help="Remove paper IDs from all project states and archive their paper folders.")
    delete.add_argument("paper_ids", nargs="+")
    delete.add_argument(
        "--force-active", action="store_true", help="Also delete records with active submissions or manuscript IDs."
    )
    sub.add_parser("doctor", help="Run basic integration checks.")
    sub.add_parser("ui", help="Open the modern Qt Quick unified workbench.")
    sub.add_parser("account-site-manager", help="Open the standalone account and site manager.")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    if args.command == "undergraduate-word-export":
        from .undergraduate_word import export_word
        try:
            result = export_word(args.root, args.paper_id, render_existing=args.render_existing)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "distribution-status":
        from .commercial.distribution import load_distribution_policy
        from .commercial.runtime import TRUSTED_KEYS_SCHEMA
        from .io_utils import read_json

        try:
            policy = load_distribution_policy()
            keyring = read_json(
                Path(__file__).resolve().parent
                / "commercial"
                / "trusted_keys.json"
            )
            if (
                not isinstance(keyring, dict)
                or keyring.get("schema") != TRUSTED_KEYS_SCHEMA
                or set(keyring) != {"schema", "keys"}
                or not isinstance(keyring.get("keys"), list)
            ):
                raise ValueError("Embedded trusted-key file is invalid.")
            result = {
                **policy.to_dict(),
                "trusted_public_key_count": len(keyring["keys"]),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "code": "DISTRIBUTION_STATUS_FAILED",
                "error_type": type(exc).__name__,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) is not False else 2
    if args.command == "smart-browser-mcp-serve":
        from .smart_browser_mcp import serve_smart_browser_mcp

        return serve_smart_browser_mcp(args.context_store)
    if args.command in {
        "workspace-status",
        "workspace-list",
        "workspace-create",
        "workspace-use",
    }:
        from .config import ACTIVE_WORKSPACE, APP_ROOT
        from .workspace import (
            create_workspace,
            list_registered_workspaces,
            set_active_workspace,
            workspace_status,
        )

        try:
            if args.command == "workspace-status":
                result: Any = workspace_status(APP_ROOT)
            elif args.command == "workspace-list":
                result = {
                    "schema": "unified-paper-orchestrator/workspace-list/v1",
                    "workspaces": list_registered_workspaces(),
                }
            elif args.command == "workspace-use":
                selected = set_active_workspace(args.path)
                result = {
                    "ok": True,
                    "status": "selected_for_next_start",
                    "workspace": selected.as_dict(),
                }
            else:
                created = create_workspace(
                    args.path,
                    args.name,
                    source=ACTIVE_WORKSPACE,
                    import_data=bool(args.import_current),
                    make_active=bool(args.activate),
                )
                result = {
                    "ok": True,
                    "status": "created",
                    "workspace": created.as_dict(),
                }
        except Exception as exc:
            result = {
                "ok": False,
                "code": str(getattr(exc, "code", "WORKSPACE_COMMAND_FAILED")),
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) is not False else 2
    if args.command == "ai-execution-status":
        status_schema = "unified-paper-orchestrator/ai-execution-status/v1"
        try:
            from .ai_execution_status import collect_ai_execution_status

            result = collect_ai_execution_status()
        except Exception as exc:
            # This diagnostic must never copy an exception message that could
            # contain provider input or a credential into terminal output.
            result = {
                "schema": status_schema,
                "ok": False,
                "error_code": "AI_EXECUTION_STATUS_FAILED",
                "error_type": type(exc).__name__,
                "safety": {
                    "read_only": True,
                    "network_attempted": False,
                    "process_started": False,
                    "credential_value_resolved": False,
                    "v2_database_opened": False,
                },
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "codex-account-status":
        from .codex_sdk_account import CodexSDKAccountService

        result = CodexSDKAccountService().status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") and result.get("authenticated") else 2
    if args.command in {
        "writing-sdk-status",
        "writing-sdk-run",
        "writing-sdk-requeue-recoverable",
        "writing-sdk-revalidate-last-completed",
    }:
        from .writing_sdk_runtime import (
            WritingSDKRuntimeError,
            requeue_recoverable,
            revalidate_last_completed,
            run_writing_sdk,
            writing_sdk_status,
        )

        try:
            if args.command == "writing-sdk-run":
                if args.until_step is not None and not args.paper_key:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_RUN_SELECTION_REQUIRED",
                        "--until-step requires at least one exact --paper-key",
                    )
                result = run_writing_sdk(args.paper_key, until_step=args.until_step)
            elif args.command == "writing-sdk-requeue-recoverable":
                if not args.paper_key:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_RECOVERY_SELECTION_REQUIRED",
                        "recovery requires at least one exact --paper-key",
                    )
                result = requeue_recoverable(
                    args.paper_key,
                    replace_failed_step1_threads=args.replace_failed_step1_threads,
                    archived_thread_ids=args.archived_thread_id,
                )
            elif args.command == "writing-sdk-revalidate-last-completed":
                if not args.paper_key:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_REVALIDATE_SELECTION_REQUIRED",
                        "revalidation requires one exact --paper-key",
                    )
                result = revalidate_last_completed(args.paper_key)
            else:
                result = writing_sdk_status()
        except WritingSDKRuntimeError as exc:
            result = {"ok": False, "code": exc.code, "message": str(exc)}
        except Exception as exc:
            result = {
                "ok": False,
                "code": "WRITING_SDK_COMMAND_FAILED",
                "error_type": type(exc).__name__,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "writing-evidence-gate":
        papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
        result = validate_writing_evidence(papers_dir, args.paper_id)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["ready"] else 2
    if args.command == "writing-evidence-import":
        papers_dir = Path(LEGACY_PROJECTS["writing"]["papers_dir"])
        result = prepare_writing_evidence_import(
            papers_dir,
            args.paper_id,
            args.spec,
            check_only=args.check_only,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["ok"] else 2
    if args.command == "writing-novelty-gate":
        writing_root = Path(LEGACY_PROJECTS["writing"]["root"])
        result = validate_writing_novelty(writing_root, args.paper_id)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["ready"] else 2
    if args.command == "writing-parallel-prepare":
        from .writing_dispatch import prepare_writing_dispatch

        return _run_writing_dispatch_cli(prepare_writing_dispatch)
    if args.command == "writing-parallel-status":
        from .writing_dispatch import writing_dispatch_status

        return _run_writing_dispatch_cli(writing_dispatch_status)
    if args.command == "writing-parallel-mark-uncertain":
        from .writing_dispatch import mark_writing_dispatch_uncertain

        return _run_writing_dispatch_cli(lambda: mark_writing_dispatch_uncertain(args.attempt_id, args.token))
    if args.command == "writing-parallel-record-dispatch":
        from .writing_dispatch import record_writing_dispatch

        return _run_writing_dispatch_cli(lambda: record_writing_dispatch(args.attempt_id, args.token, args.thread_id))
    if args.command == "writing-parallel-release":
        from .writing_dispatch import release_writing_dispatch

        return _run_writing_dispatch_cli(lambda: release_writing_dispatch(args.attempt_id, args.token))
    if args.command == "writing-parallel-reconcile-adopt":
        from .writing_dispatch import reconcile_writing_uncertain_adopt_terminal

        return _run_writing_dispatch_cli(
            lambda: reconcile_writing_uncertain_adopt_terminal(
                args.attempt_id,
                paper_id=args.paper_id,
                thread_id=args.thread_id,
            )
        )
    if args.command == "writing-parallel-reconcile-unsent":
        from .writing_dispatch import reconcile_writing_uncertain_unsent

        return _run_writing_dispatch_cli(lambda: reconcile_writing_uncertain_unsent(args.attempt_id))
    if args.command == "runtime-budget-check":
        result = evaluate_runtime_budget_files(args.workload, args.calibration, args.binding)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["should_start"] else 2
    if args.command == "runtime-budget-launch":
        decision, completed = launch_runtime_budget_files(
            args.workload,
            args.calibration,
            args.binding,
        )
        payload = {
            "decision": decision,
            "launch": {
                "started": bool(getattr(completed, "started", completed is not None)),
                "returncode": getattr(completed, "returncode", None),
                "timed_out": bool(getattr(completed, "timed_out", False)),
                "tree_guarded": bool(getattr(completed, "tree_guarded", False)),
                "tree_terminated_verified": bool(getattr(completed, "tree_terminated_verified", False)),
                "error_code": getattr(completed, "error_code", ""),
                "error_type": getattr(completed, "error_type", ""),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if not decision["should_start"]:
            return 2
        if completed is None:
            return 1
        if (
            not completed.started
            or completed.timed_out
            or not completed.tree_guarded
            or not completed.tree_terminated_verified
            or completed.error_code
            or completed.returncode != 0
        ):
            return 1
        return 0
    if args.command in {
        "gateway-status",
        "gateway-review-canary",
        "gateway-relay-review-canary",
        "gateway-codex-relay-review-canary",
        "gateway-dispatch",
        "gateway-task",
        "gateway-retry",
        "gateway-approve",
        "gateway-recover-review",
        "gateway-recover-automation",
        "gateway-legacy-automation",
    }:
        from unified_paper_orchestrator_v2.gateway import GatewayError, default_gateway
        from unified_paper_orchestrator_v2.review_canary import ReviewCanaryError

        try:
            gateway = default_gateway()
            if args.command == "gateway-status":
                print(json.dumps(gateway.status(), ensure_ascii=False, indent=2))
                return 0
            if args.command == "gateway-review-canary":
                record = gateway.submit_review_canary(
                    args.paper_id,
                    model=args.model,
                    token_budget=args.token_budget,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout_seconds,
                )
                if args.execute and record.get("status") == "queued":
                    dispatched = gateway.run_once(str(record["task_id"]))
                    if dispatched is not None:
                        record = dispatched
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-relay-review-canary":
                record = gateway.submit_relay_review_canary(
                    args.paper_id,
                    model=args.model,
                    token_budget=args.token_budget,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout_seconds,
                )
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-codex-relay-review-canary":
                record = gateway.submit_codex_cli_relay_review_canary(
                    args.paper_id,
                    model=args.model,
                    codex_cli_path=args.codex_cli_path,
                    token_budget=args.token_budget,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout_seconds,
                )
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-task":
                record = gateway.task(args.task_id)
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-retry":
                retry_options = {}
                if args.acknowledge_possible_duplicate_charge:
                    retry_options["acknowledge_possible_duplicate_charge"] = True
                record = gateway.retry(args.task_id, **retry_options)
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-approve":
                approval_options = {"note": args.note}
                if args.intent_sha256:
                    approval_options["expected_intent_sha256"] = args.intent_sha256
                record = gateway.approve(args.task_id, **approval_options)
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-recover-review":
                record = gateway.recover_review_task(
                    args.task_id,
                    expected_input_manifest_sha256=args.input_manifest_sha256,
                )
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-recover-automation":
                record = gateway.recover_legacy_automation_task(
                    args.task_id,
                    expected_intent_sha256=args.intent_sha256,
                )
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            if args.command == "gateway-legacy-automation":
                record = gateway.submit_legacy_automation(
                    args.operation,
                    args.stage,
                    operation_id=args.operation_id,
                )
                if args.execute and record.get("status") == "queued":
                    record = gateway.run_once(str(record["task_id"])) or record
                print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
                return _gateway_exit_code(record)
            record = gateway.run_once(args.task_id)
            print(json.dumps(_compact_gateway_record(record), ensure_ascii=False, indent=2))
            return _gateway_exit_code(record)
        except (GatewayError, ReviewCanaryError, ValueError) as exc:
            code = str(getattr(exc, "code", "GATEWAY_VALIDATION_ERROR"))
            print(json.dumps({"ok": False, "code": code, "message": str(exc)}, ensure_ascii=False, indent=2))
            return 2

    orchestrator = UnifiedOrchestrator()
    if args.command == "sync":
        state = orchestrator.sync()
        print(f"synced {len(state.get('papers', {}))} papers")
        return 0
    if args.command == "queue":
        state = orchestrator.queue_papers(args.paper_ids)
        print("queue=" + ", ".join(state.get("paper_queue", [])))
        return 0
    if args.command == "clear-queue":
        orchestrator.clear_queue()
        print("queue cleared")
        return 0
    if args.command == "advance":
        try:
            result = orchestrator.advance_once(apply=args.apply, paper_id=args.paper_id)
        except Exception as exc:
            return _execution_control_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") not in {"missing", "unknown_action"} else 2
    if args.command == "new-writing-session":
        try:
            state = orchestrator.start_new_writing_sessions(args.count, single_active=args.single_active)
        except Exception as exc:
            return _execution_control_error(exc)
        payload = state.get("last_scheduler_result", {})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        writing = payload.get("results", {}).get("writing", {}) if isinstance(payload, dict) else {}
        return 0 if isinstance(writing, dict) and writing.get("ok") is True else 2
    if args.command == "pause-schedulers":
        state = orchestrator.pause_all()
        print(json.dumps(state.get("last_scheduler_result", {}), ensure_ascii=False, indent=2))
        return 0
    if args.command == "scheduler-status":
        print(json.dumps(orchestrator.scheduler_status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "start-stage":
        try:
            state = orchestrator.start_stage(args.stage, single_active=args.single_active)
        except Exception as exc:
            return _execution_control_error(exc)
        payload = state.get("last_scheduler_result", {})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        selected = payload.get("results", {}).get(args.stage, {}) if isinstance(payload, dict) else {}
        return 0 if isinstance(selected, dict) and selected.get("ok") is True else 2
    if args.command == "pause-stage":
        state = orchestrator.pause_stage(args.stage)
        print(json.dumps(state.get("last_scheduler_result", {}), ensure_ascii=False, indent=2))
        return 0
    if args.command == "smart-submission-init":
        from .smart_submission import ensure_smart_submission_module

        print(json.dumps(_compact_smart_cli_payload(ensure_smart_submission_module()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "smart-submission-status":
        from .smart_submission import smart_submission_status

        print(
            json.dumps(
                _compact_smart_cli_payload(smart_submission_status(include_browser_health=args.with_browser_health)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "smart-submission-sdk-run":
        from .smart_submission_sdk_runtime import default_smart_submission_sdk_runtime

        return _run_smart_cli_dict(
            lambda: default_smart_submission_sdk_runtime().run_once(args.paper_ids or None)
        )
    if args.command == "smart-submission-sdk-status":
        from .smart_submission_sdk_runtime import smart_submission_sdk_status

        print(json.dumps(smart_submission_sdk_status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "smart-submission-sdk-pause":
        from .smart_submission_sdk_runtime import pause_smart_submission_sdk_after_current

        print(
            json.dumps(
                pause_smart_submission_sdk_after_current(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "smart-submission-program-plan":
        from .smart_submission import smart_submission_program_plan

        return _run_smart_cli_dict(
            lambda: smart_submission_program_plan(
                selected_ids=args.paper_ids or None,
                persist=not args.no_persist,
                sync_ledger=not args.no_ledger,
            )
        )
    if args.command == "smart-submission-probe-session":
        from .trusted_login_probe import probe_and_record_browser_session

        return _run_smart_cli_dict(
            lambda: probe_and_record_browser_session(
                account_id=args.account_id,
                system_id=args.system_id,
                origin=args.origin,
                ports=tuple(args.port or (9224, 9222)),
            )
        )
    if args.command == "smart-submission-browser-health":
        from .smart_submission import smart_submission_browser_health

        result = smart_submission_browser_health(ensure=args.ensure)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("operational") else 1
    if args.command == "smart-submission-browser-cleanup":
        from .smart_submission import smart_submission_browser_cleanup

        print(
            json.dumps(
                smart_submission_browser_cleanup(
                    close=args.close,
                    include_stale_submission_tabs=args.include_stale_submission_tabs,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "smart-submission-run":
        from .smart_submission import run_smart_submission_scheduler

        return _run_smart_cli_dict(
            lambda: run_smart_submission_scheduler(
                force=args.force,
                max_papers=args.max_papers,
            )
        )
    if args.command == "smart-submission-program-tick":
        from .smart_submission import run_smart_submission_program_tick

        return _run_smart_cli_dict(run_smart_submission_program_tick)
    if args.command == "smart-submission-automation-tick":
        from .smart_submission import run_smart_submission_automation_tick

        return _run_smart_cli_dict(run_smart_submission_automation_tick)
    if args.command == "smart-submission-program-timer-reconcile":
        from .smart_submission import reconcile_smart_submission_program_timer

        return _run_smart_cli_dict(reconcile_smart_submission_program_timer)
    if args.command == "smart-submission-batch-check":
        from .smart_submission import smart_submission_batch_check

        return _run_smart_cli_dict(
            lambda: smart_submission_batch_check(
                selected_ids=args.paper_ids or None,
                queue_online=args.activate_online and not args.no_online_queue,
                activate_online=args.activate_online,
                max_papers=args.max_papers,
                expected_route_revision=args.expected_route_revision,
                orchestrator=orchestrator,
            )
        )
    if args.command == "smart-submission-mode":
        from .smart_submission import set_smart_submission_mode

        print(
            json.dumps(_compact_smart_cli_payload(set_smart_submission_mode(args.mode)), ensure_ascii=False, indent=2)
        )
        return 0
    if args.command == "smart-submission-wakeup":
        from .smart_submission import set_smart_submission_automation_fast_wakeup

        return _run_smart_cli_dict(lambda: set_smart_submission_automation_fast_wakeup(True))
    if args.command == "smart-submission-restore-daily":
        from .smart_submission import (
            restore_smart_submission_daily_monitoring,
            update_smart_submission_daily_monitoring_time,
        )

        return _run_smart_cli_dict(
            lambda: (
                update_smart_submission_daily_monitoring_time(args.time_local)
                if args.time_local
                else restore_smart_submission_daily_monitoring()
            )
        )
    if args.command == "smart-submission-import":
        from .smart_submission import import_from_unified_state

        return _run_smart_cli_dict(
            lambda: import_from_unified_state(
                orchestrator.sync(),
                selected_ids=args.paper_ids or None,
            )
        )
    if args.command == "smart-submission-start-visible-online":
        from .smart_submission import start_single_smart_canary

        return _run_smart_cli_dict(
            lambda: start_single_smart_canary(
                args.paper_id,
                expected_route_revision=args.expected_route_revision,
                orchestrator=orchestrator,
            )
        )
    if args.command == "smart-submission-prioritize":
        from .smart_submission import prioritize_online_queue

        return _run_smart_cli_dict(lambda: prioritize_online_queue(args.paper_ids, activate=not args.no_activate))
    if args.command == "smart-submission-move-online":
        from .smart_submission import move_online_queue_item

        return _run_smart_cli_dict(
            lambda: move_online_queue_item(args.paper_id, args.direction, activate=args.activate)
        )
    if args.command == "smart-submission-apply-planned-github":
        from .smart_submission import apply_planned_github_repositories

        return _run_smart_cli_dict(
            lambda: apply_planned_github_repositories(
                args.paper_ids or None,
                activate=not args.no_activate,
            )
        )
    if args.command == "smart-submission-apply-standing-declarations":
        from .smart_submission import apply_standing_declarations

        return _run_smart_cli_dict(
            lambda: apply_standing_declarations(
                args.paper_ids or None,
                activate=not args.no_activate,
            )
        )
    if args.command == "smart-submission-recover-actionable":
        from .smart_submission import recover_auto_actionable_papers

        return _run_smart_cli_dict(
            lambda: recover_auto_actionable_papers(
                args.paper_ids or None,
                activate=not args.no_activate,
            )
        )
    if args.command == "smart-submission-pause-waiting-approval":
        from .smart_submission import pause_waiting_approval_workers

        return _run_smart_cli_dict(lambda: pause_waiting_approval_workers(args.paper_ids or None))
    if args.command == "smart-submission-requeue-waiting-approval":
        from .smart_submission import requeue_waiting_approval_workers

        return _run_smart_cli_dict(
            lambda: requeue_waiting_approval_workers(
                args.paper_ids or None,
                activate=not args.no_activate,
            )
        )
    if args.command == "smart-submission-worker-prompt":
        from .smart_submission import SmartSubmissionGuardError, build_visible_worker_prompt

        try:
            print(build_visible_worker_prompt(args.paper_id, route_revision=args.route_revision), end="")
            return 0
        except SmartSubmissionGuardError as exc:
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {"ok": False, "code": "SMART_CLI_COMMAND_FAILED", "message": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
    if args.command == "smart-submission-record-worker-dispatch":
        from .smart_submission import record_visible_worker_dispatch

        return _run_smart_cli_dict(
            lambda: record_visible_worker_dispatch(
                args.paper_id,
                thread_id=args.thread_id,
                route_revision=args.route_revision,
                attempt_id=args.attempt_id,
            )
        )
    if args.command == "smart-submission-claim-final-submit-action":
        from .smart_submission import claim_final_submit_action

        return _run_smart_cli_dict(
            lambda: claim_final_submit_action(
                args.paper_id,
                route_revision=args.route_revision,
                attempt_id=args.attempt_id,
                thread_id=args.thread_id,
                authorization_id=args.authorization_id,
            )
        )
    if args.command == "smart-submission-capture-confirmation":
        from .smart_submission import capture_live_submission_confirmation

        return _run_smart_cli_dict(
            lambda: capture_live_submission_confirmation(
                args.paper_id,
                route_revision=args.route_revision,
                attempt_id=args.attempt_id,
                thread_id=args.thread_id,
                authorization_id=args.authorization_id,
            )
        )
    if args.command == "smart-submission-record-worker-result":
        from .smart_submission import record_visible_worker_result

        return _run_smart_cli_dict(
            lambda: record_visible_worker_result(
                args.paper_id,
                route_revision=args.route_revision,
                attempt_id=args.attempt_id,
                status=args.status,
                next_action=args.next_action,
                detail=args.detail,
                thread_id=args.thread_id,
                source=args.source,
                submission_id=args.submission_id,
                evidence_path=args.evidence_path,
                confirmation_url=args.confirmation_url,
                activate=not args.no_activate,
                keep_queued=args.keep_queued,
            )
        )
    if args.command == "smart-submission-set-scheduler-thread":
        from .smart_submission import set_visible_scheduler_thread

        return _run_smart_cli_dict(lambda: set_visible_scheduler_thread(args.thread_id))
    if args.command == "delete":
        result = orchestrator.delete_papers(args.paper_ids, force_active=args.force_active)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        from .doctor import run_doctor

        result = run_doctor()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"PASS", "PASS_WITH_WARNINGS"} else 1
    if args.command == "ui":
        from .ui_next import main as ui_main

        return int(ui_main() or 0)
    if args.command == "account-site-manager":
        from .ui.account_site_manager import main as account_site_main

        return int(account_site_main() or 0)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
