"""Codex SDK runtime for program-owned Smart Submission browser work."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from . import config
from .codex_sdk_account import CODEX_SDK_HOME, CodexSDKAccountService
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .smart_browser_mcp import (
    SMART_BROWSER_TOOL_NAMES,
    SmartBrowserContextStore,
)
from .smart_credential_broker import TrustedCredentialBroker
from .smart_mailbox import (
    aggregate_monitor_source_results,
    collect_local_mailbox_evidence,
)
from .smart_submission import (
    FINAL_SUBMIT_AUTHORIZED_ACTION,
    FINAL_SUBMIT_REVIEW_READY_ACTION,
    SMART_MONITOR_BATCH_SCHEMA,
    SMART_MONITOR_CHANNEL_MAILBOX_BATCH,
    SMART_MONITOR_MAILBOX_ORIGIN,
    SMART_STATE_PATH,
    authorize_final_submit_automatically,
    build_sdk_mailbox_monitor_prompt,
    build_sdk_browser_worker_prompt,
    queue_smart_monitor_dashboard_fallback,
    record_smart_monitor_observation,
    record_smart_credential_broker_state,
    record_smart_sdk_thread_binding,
    record_smart_sdk_turn_failure,
    record_smart_sdk_turn_state,
    record_mailbox_monitor_worker_dispatch,
    record_visible_worker_dispatch,
    record_smart_retarget_closure_result,
    record_visible_worker_result,
    release_expired_unclaimed_final_submit_authorization,
    smart_submission_browser_health,
    smart_submission_program_plan,
)
from .smart_submission_program import ProgramExecutor
from .windows_subprocess import configure_openai_codex_no_window
from .codex_runtime import runtime_launch_options


SMART_SUBMISSION_SDK_JOURNAL_SCHEMA = (
    "unified-paper-orchestrator/smart-submission-sdk-dispatch/v1"
)
SMART_SUBMISSION_SDK_PROMPT_VERSION = "smart-submission-sdk-browser-v1"
SMART_SUBMISSION_SDK_RUNTIME_SCHEMA = (
    "unified-paper-orchestrator/smart-submission-sdk-runtime/v1"
)

_MARKER_PATTERN = re.compile(
    r"(?m)^SMART_SUBMISSION_([A-Z_]+)=(.*)$"
)
_MONITOR_BATCH_PATTERN = re.compile(
    r"SMART_MONITOR_BATCH_BEGIN\s*(?P<payload>\{.*?\})\s*SMART_MONITOR_BATCH_END",
    flags=re.DOTALL,
)
_VALID_RESULT_STATUSES = {
    "submitted",
    "monitoring",
    "revision_needed",
    "retarget_needed",
    "rejected",
    "blocked",
    "accepted",
    "waiting",
    "no_action",
}


class SmartSubmissionSDKRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(str(message))


def _status_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _usage_snapshot(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="json", by_alias=False)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _turns(response: Any) -> list[Any]:
    thread = getattr(response, "thread", None)
    return list(getattr(thread, "turns", []) or [])


def _turn_final_response(turn: Any) -> str:
    messages: list[tuple[str, str]] = []
    for item in list(getattr(turn, "items", []) or []):
        root = getattr(item, "root", item)
        text = str(getattr(root, "text", "") or "")
        if not text:
            continue
        phase = _status_value(getattr(root, "phase", ""))
        messages.append((phase, text))
    for phase, text in reversed(messages):
        if phase in {"finalAnswer", "final_answer"}:
            return text
    return messages[-1][1] if messages else ""


def _toml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _mcp_process_command() -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ["smart-browser-mcp-serve"]
    return str(Path(sys.executable).resolve()), [
        "-m",
        "unified_paper_orchestrator",
        "smart-browser-mcp-serve",
    ]


class SmartSubmissionSDKRuntime:
    """Program scheduler plus one fixed Codex SDK thread per paper."""

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        state_path: Path | str | None = None,
        journal_path: Path | str | None = None,
        dispatch_lock_path: Path | str | None = None,
        context_store_path: Path | str | None = None,
        codex_home: Path | str | None = None,
        account_service: Any | None = None,
        codex_factory: Callable[[], Any] | None = None,
        browser_health_probe: Callable[..., Mapping[str, Any]] | None = None,
        mailbox_pages_probe: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        mailbox_evidence_collector: Callable[
            [Mapping[str, Mapping[str, Any]]], Mapping[str, Any]
        ]
        | None = None,
        dashboard_fallback_queue: Callable[[Iterable[str]], Mapping[str, Any]]
        | None = None,
        credential_broker: Any | None = None,
        model: str | None = None,
    ) -> None:
        self.root = Path(root or config.REPO_ROOT).expanduser().resolve()
        self.state_path = Path(state_path or SMART_STATE_PATH).expanduser().resolve()
        self.journal_path = Path(
            journal_path or config.SMART_SUBMISSION_SDK_JOURNAL_PATH
        ).expanduser().resolve()
        self.dispatch_lock_path = Path(
            dispatch_lock_path or config.SMART_SUBMISSION_SDK_LOCK_PATH
        ).expanduser().resolve()
        self.context_store_path = Path(
            context_store_path or config.SMART_SUBMISSION_BROWSER_CONTEXT_PATH
        ).expanduser().resolve()
        self.context_store = SmartBrowserContextStore(self.context_store_path)
        self.codex_home = Path(codex_home or CODEX_SDK_HOME).expanduser().resolve()
        self.account_service = account_service or CodexSDKAccountService(
            codex_home=self.codex_home
        )
        self._injected_codex_factory = codex_factory
        self._browser_health_probe = browser_health_probe or smart_submission_browser_health
        self._mailbox_pages_probe = mailbox_pages_probe
        self._mailbox_evidence_collector = mailbox_evidence_collector
        self._dashboard_fallback_queue = dashboard_fallback_queue
        self._credential_broker = credential_broker
        self.model = str(
            config.SMART_SUBMISSION_CODEX_MODEL if model is None else model
        ).strip()
        if not self.model:
            raise ValueError("Smart Submission Codex model must not be empty")
        self._background_lock = threading.Lock()
        self._background_thread: threading.Thread | None = None
        self._stop_after_current = threading.Event()
        self._pending_selected: set[str] = set()
        self._pending_full_sweep = False
        self._last_result: dict[str, Any] | None = None
        self._last_error = ""

    def _broker(self) -> Any:
        if self._credential_broker is None:
            self._credential_broker = TrustedCredentialBroker(
                state_path=self.state_path,
            )
        return self._credential_broker

    def _codex_factory(self) -> Any:
        if self._injected_codex_factory is not None:
            return self._injected_codex_factory()
        configure_openai_codex_no_window()
        self.codex_home.mkdir(parents=True, exist_ok=True)
        command, args = _mcp_process_command()
        args = [*args, "--context-store", str(self.context_store_path)]
        enabled_tools = list(SMART_BROWSER_TOOL_NAMES)
        overrides = (
            "features.multi_agent=false",
            "features.multi_agent_v2=false",
            "features.apps=false",
            "features.plugins=false",
            "features.browser_use=false",
            "features.browser_use_external=false",
            "features.in_app_browser=false",
            "features.computer_use=false",
            "features.image_generation=false",
            "features.shell_tool=false",
            "agents.max_threads=1",
            "agents.max_depth=1",
            f"mcp_servers.smart_browser.command={_toml_value(command)}",
            f"mcp_servers.smart_browser.args={_toml_value(args)}",
            "mcp_servers.smart_browser.required=true",
            "mcp_servers.smart_browser.startup_timeout_sec=15",
            "mcp_servers.smart_browser.tool_timeout_sec=55",
            f"mcp_servers.smart_browser.enabled_tools={_toml_value(enabled_tools)}",
            'mcp_servers.smart_browser.default_tools_approval_mode="approve"',
        )
        return AsyncCodex(
            CodexConfig(
                cwd=str(self.root),
                **runtime_launch_options({"CODEX_HOME": str(self.codex_home)}),
                config_overrides=overrides,
                client_name="unified_paper_orchestrator_smart_submission",
                client_title="Unified Paper Orchestrator Smart Submission",
            )
        )

    def ensure_ready(self) -> dict[str, Any]:
        verified = getattr(self.account_service, "verified_status", None)
        status = verified() if callable(verified) else self.account_service.status()
        if not isinstance(status, dict) or not status.get("ok"):
            raise SmartSubmissionSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_STATUS_UNAVAILABLE",
                "无法读取本程序的 Codex SDK 账号状态。",
            )
        if not status.get("isolated_home"):
            raise SmartSubmissionSDKRuntimeError(
                "CODEX_SDK_HOME_NOT_ISOLATED",
                "智能投稿 SDK 未使用程序专属 CODEX_HOME。",
            )
        if not status.get("authenticated"):
            raise SmartSubmissionSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED",
                "请先在“账号与站点”中连接本程序使用的 ChatGPT/Codex 账号。",
            )
        return dict(status)

    require_ready = ensure_ready

    @staticmethod
    def _empty_journal() -> dict[str, Any]:
        return {
            "schema": SMART_SUBMISSION_SDK_JOURNAL_SCHEMA,
            "papers": {},
            "history": [],
            "updated_at": now_iso(),
        }

    def _load_journal(self) -> dict[str, Any]:
        state = read_json(self.journal_path, self._empty_journal())
        if (
            not isinstance(state, dict)
            or state.get("schema") != SMART_SUBMISSION_SDK_JOURNAL_SCHEMA
        ):
            state = self._empty_journal()
        if not isinstance(state.get("papers"), dict):
            state["papers"] = {}
        if not isinstance(state.get("history"), list):
            state["history"] = []
        return state

    def _mutate_journal(
        self,
        callback: Callable[[dict[str, Any]], Any],
    ) -> Any:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.journal_path.with_suffix(self.journal_path.suffix + ".lock")):
            state = self._load_journal()
            result = callback(state)
            state["updated_at"] = now_iso()
            write_json_atomic(self.journal_path, state)
            return result

    def _journal_event(
        self,
        event: str,
        paper_id: str,
        **fields: Any,
    ) -> None:
        def mutate(state: dict[str, Any]) -> None:
            history = state.setdefault("history", [])
            history.append(
                {
                    "at": now_iso(),
                    "event": str(event),
                    "paper_id": str(paper_id),
                    **copy.deepcopy(fields),
                }
            )
            if len(history) > 500:
                del history[:-500]

        self._mutate_journal(mutate)

    def _journal_paper_update(
        self,
        paper_id: str,
        **fields: Any,
    ) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            papers = state.setdefault("papers", {})
            record = papers.setdefault(str(paper_id), {})
            if not isinstance(record, dict):
                record = {}
                papers[str(paper_id)] = record
            record.update(copy.deepcopy(fields))
            record["updated_at"] = now_iso()
            return copy.deepcopy(record)

        return self._mutate_journal(mutate)

    @staticmethod
    def _thread_name(paper_id: str) -> str:
        digest = hashlib.sha256(str(paper_id).encode("utf-8")).hexdigest()[:24]
        return f"upo-smart-submission-{digest}"

    @staticmethod
    def _monitor_thread_name(paper_id: str) -> str:
        digest = hashlib.sha256(str(paper_id).encode("utf-8")).hexdigest()[:24]
        return f"upo-smart-monitor-{digest}"

    def _smart_paper(self, paper_id: str) -> dict[str, Any]:
        state = read_json(self.state_path, {})
        papers = state.get("papers", {}) if isinstance(state, dict) else {}
        paper = papers.get(str(paper_id)) if isinstance(papers, dict) else None
        if not isinstance(paper, dict):
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_PAPER_MISSING",
                f"智能投稿记录不存在：{paper_id}",
            )
        return copy.deepcopy(paper)

    def _reissue_final_authorization_after_broker_refresh(
        self,
        paper_id: str,
        paper: Mapping[str, Any],
        *,
        route_revision: int,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Rebind only an unused capability changed by the final session refresh."""

        if (
            str(paper.get("next_action") or "").strip()
            != FINAL_SUBMIT_AUTHORIZED_ACTION
            or not isinstance(paper.get("final_submit_authorization"), Mapping)
        ):
            return dict(paper)
        rebound = release_expired_unclaimed_final_submit_authorization(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
        )
        if not rebound.get("released"):
            return dict(paper)
        reauthorized = authorize_final_submit_automatically(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
            activate=False,
        )
        self._journal_paper_update(
            paper_id,
            status="auto_final_submit_reauthorized_after_session_refresh",
            authorization_id=str(reauthorized.get("authorization_id") or ""),
            authorization_mode=str(
                reauthorized.get("authorization_mode") or ""
            ),
            error_code="",
            error="",
        )
        self._journal_event(
            "auto_final_submit_reauthorized_after_session_refresh",
            paper_id,
            attempt_id=attempt_id,
            route_revision=route_revision,
            authorization_id=str(reauthorized.get("authorization_id") or ""),
            session_binding_rebound=bool(rebound.get("session_binding_rebound")),
        )
        return self._smart_paper(paper_id)

    def _auto_authorize_ready_final_submits(
        self,
        selected_ids: Iterable[str] | None,
        attempted: set[tuple[str, str]],
        program_papers: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert reviewed papers into exact one-use final-submit work."""

        state = read_json(self.state_path, {})
        if not isinstance(state, dict):
            return []
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        if not bool(settings.get("auto_final_submit_enabled", True)):
            return []
        scope = (
            {str(item) for item in selected_ids if str(item)}
            if selected_ids is not None
            else None
        )
        papers = state.get("papers", {}) if isinstance(state.get("papers"), dict) else {}
        planned_papers = program_papers if isinstance(program_papers, Mapping) else {}
        results: list[dict[str, Any]] = []
        for raw_paper_id, raw_paper in list(papers.items()):
            paper_id = str(raw_paper_id)
            paper = raw_paper if isinstance(raw_paper, Mapping) else {}
            attempt_id = str(paper.get("attempt_id") or "").strip()
            key = (paper_id, attempt_id)
            status = str(paper.get("status") or "").strip().casefold()
            workflow_status = str(paper.get("workflow_status") or "").strip().casefold()
            next_action = str(paper.get("next_action") or "").strip()
            review_ready = (
                status == "waiting"
                and workflow_status == "waiting"
                and next_action == FINAL_SUBMIT_REVIEW_READY_ACTION
            )
            authorized_pending = (
                status == "ready_for_online_submit"
                and workflow_status == "queued"
                and next_action == FINAL_SUBMIT_AUTHORIZED_ACTION
            )
            if (
                (scope is not None and paper_id not in scope)
                or key in attempted
                or not attempt_id
                or not (review_ready or authorized_pending)
            ):
                continue
            attempted.add(key)
            route_revision = int(paper.get("route_revision") or 0)
            try:
                recovery: dict[str, Any] = {}
                if authorized_pending:
                    recovery = release_expired_unclaimed_final_submit_authorization(
                        paper_id,
                        route_revision=route_revision,
                        attempt_id=attempt_id,
                    )
                    if not recovery.get("released"):
                        continue
                planned = planned_papers.get(paper_id, {})
                candidate = dict(planned) if isinstance(planned, Mapping) else {}
                candidate.setdefault(
                    "credential_account_id",
                    str(paper.get("credential_account_id") or ""),
                )
                candidate.setdefault(
                    "submission_system_id",
                    str(paper.get("submission_system_id") or ""),
                )
                candidate.setdefault(
                    "target_origin",
                    str(paper.get("credential_broker_origin") or ""),
                )
                broker_result = self._broker().ensure_session(
                    paper_id=paper_id,
                    paper=paper,
                    candidate=candidate,
                )
                paper = record_smart_credential_broker_state(
                    paper_id,
                    dict(broker_result),
                )
                self._journal_paper_update(
                    paper_id,
                    status=(
                        "auto_final_submit_session_ready"
                        if broker_result.get("ok")
                        else "auto_final_submit_session_deferred"
                    ),
                    credential_broker_status=str(
                        broker_result.get("status") or ""
                    ),
                    credential_broker_code=str(
                        broker_result.get("code") or ""
                    ),
                    credential_broker_value_free=True,
                    error_code=(
                        "" if broker_result.get("ok") else str(broker_result.get("code") or "")
                    ),
                    error=(
                        ""
                        if broker_result.get("ok")
                        else str(broker_result.get("next_action") or "")[:2000]
                    ),
                )
                self._journal_event(
                    "auto_final_submit_session_refresh",
                    paper_id,
                    status=str(broker_result.get("status") or ""),
                    code=str(broker_result.get("code") or ""),
                    value_free=True,
                )
                if not broker_result.get("ok"):
                    item = {
                        "ok": False,
                        "deferred": True,
                        "paper_id": paper_id,
                        "attempt_id": attempt_id,
                        "route_revision": route_revision,
                        "code": str(broker_result.get("code") or ""),
                        "message": str(broker_result.get("next_action") or ""),
                    }
                    results.append(item)
                    continue
                result = authorize_final_submit_automatically(
                    paper_id,
                    route_revision=route_revision,
                    attempt_id=attempt_id,
                    activate=False,
                )
                item = {
                    "ok": True,
                    "paper_id": paper_id,
                    "attempt_id": attempt_id,
                    "route_revision": route_revision,
                    "authorization_id": str(result.get("authorization_id") or ""),
                    "authorization_mode": str(result.get("authorization_mode") or ""),
                    "expires_at": str(result.get("expires_at") or ""),
                    "recovered_expired_authorization": bool(recovery.get("released")),
                }
                self._journal_paper_update(
                    paper_id,
                    status="auto_final_submit_authorized",
                    auto_final_submit=True,
                    authorization_id=item["authorization_id"],
                    authorization_mode=item["authorization_mode"],
                    error_code="",
                    error="",
                )
                self._journal_event(
                    "auto_final_submit_authorized",
                    paper_id,
                    attempt_id=attempt_id,
                    route_revision=route_revision,
                    authorization_id=item["authorization_id"],
                )
            except Exception as exc:
                item = {
                    "ok": False,
                    "paper_id": paper_id,
                    "attempt_id": attempt_id,
                    "route_revision": route_revision,
                    "code": str(
                        getattr(exc, "code", "SMART_AUTO_FINAL_SUBMIT_AUTHORIZATION_FAILED")
                    ),
                    "message": str(exc),
                }
                self._journal_paper_update(
                    paper_id,
                    status="auto_final_submit_authorization_failed",
                    auto_final_submit=True,
                    error_code=item["code"],
                    error=str(exc)[:2000],
                )
                self._journal_event(
                    "auto_final_submit_authorization_failed",
                    paper_id,
                    attempt_id=attempt_id,
                    route_revision=route_revision,
                    error_code=item["code"],
                )
            results.append(item)
        return results

    async def _find_thread_by_name(self, codex: Any, name: str) -> str:
        try:
            response = await codex.thread_list(
                archived=False,
                limit=100,
                search_term=name,
            )
        except Exception:
            return ""
        matches: set[str] = set()
        for item in list(getattr(response, "data", []) or []):
            if str(getattr(item, "name", "") or "") != name:
                continue
            thread_id = str(getattr(item, "id", "") or "")
            if thread_id:
                matches.add(thread_id)
        return next(iter(matches), "") if len(matches) == 1 else ""

    async def _thread_for_paper(
        self,
        codex: Any,
        paper_id: str,
        *,
        monitor_only: bool = False,
    ) -> Any:
        paper = self._smart_paper(paper_id)
        name = str(paper.get("sdk_thread_name") or self._thread_name(paper_id))
        thread_id = str(paper.get("sdk_thread_id") or "").strip()
        stale_thread_ids = {
            str(value).strip()
            for value in paper.get("stale_worker_thread_ids", [])
            if str(value).strip()
        }
        replace_stale_binding = bool(
            str(paper.get("sdk_thread_id") or "").strip() in stale_thread_ids
        )
        if thread_id and thread_id in stale_thread_ids:
            self._journal_event(
                "stale_thread_skipped",
                paper_id,
                thread_id=thread_id,
            )
            thread_id = ""
        thread_kwargs = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(self.root),
            "model": self.model,
            "sandbox": Sandbox.read_only,
            "developer_instructions": (
                "You are one bounded Smart Submission browser worker. "
                "Process only the paper named in the current prompt. "
                "Use only the smart_browser MCP tools for webpage actions; "
                "never use shell commands, credential stores, or a different browser tool."
            ),
        }
        if monitor_only:
            monitor_name = self._monitor_thread_name(paper_id)
            record_smart_sdk_turn_state(
                paper_id,
                thread_id="",
                turn_id="",
                runtime_status="creating_thread",
                model=self.model,
                monitor_only=True,
            )
            thread = await codex.thread_start(
                ephemeral=True,
                **thread_kwargs,
            )
            monitor_thread_id = str(getattr(thread, "id", "") or "")
            if not monitor_thread_id:
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_SDK_MONITOR_THREAD_ID_MISSING",
                    "Codex SDK 创建轻量监控会话后没有返回会话 ID。",
                )
            record_smart_sdk_turn_state(
                paper_id,
                thread_id=monitor_thread_id,
                turn_id="",
                runtime_status="queued",
                model=self.model,
                monitor_only=True,
            )
            self._journal_paper_update(
                paper_id,
                monitor_thread_id=monitor_thread_id,
                monitor_thread_name=monitor_name,
                monitor_status="queued",
                monitor_model=self.model,
            )
            self._journal_event(
                "monitor_thread_created",
                paper_id,
                thread_id=monitor_thread_id,
                thread_name=monitor_name,
                ephemeral=True,
            )
            return thread
        if thread_id:
            try:
                return await codex.thread_resume(thread_id, **thread_kwargs)
            except Exception as exc:
                self._journal_event(
                    "thread_resume_failed",
                    paper_id,
                    thread_id=thread_id,
                    error=type(exc).__name__,
                )
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_SDK_THREAD_RESUME_FAILED",
                    "固定 SDK 会话无法恢复；为防止同一论文悄悄创建第二个会话，已停止该任务。",
                ) from exc
        recovered = await self._find_thread_by_name(codex, name)
        if recovered and recovered not in stale_thread_ids:
            record_smart_sdk_thread_binding(
                paper_id,
                thread_id=recovered,
                thread_name=name,
                replace=replace_stale_binding,
            )
            self._journal_event(
                "thread_recovered",
                paper_id,
                thread_id=recovered,
            )
            return await codex.thread_resume(recovered, **thread_kwargs)
        if recovered:
            self._journal_event(
                "stale_named_thread_skipped",
                paper_id,
                thread_id=recovered,
                thread_name=name,
            )
        record_smart_sdk_turn_state(
            paper_id,
            thread_id="",
            turn_id="",
            runtime_status="creating_thread",
            model=self.model,
        )
        thread = await codex.thread_start(
            ephemeral=False,
            **thread_kwargs,
        )
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_THREAD_ID_MISSING",
                "Codex SDK 创建会话后没有返回会话 ID。",
            )
        await thread.set_name(name)
        record_smart_sdk_thread_binding(
            paper_id,
            thread_id=thread_id,
            thread_name=name,
            replace=replace_stale_binding,
        )
        self._journal_paper_update(
            paper_id,
            thread_id=thread_id,
            thread_name=name,
            status="queued",
            model=self.model,
        )
        self._journal_event(
            "thread_created",
            paper_id,
            thread_id=thread_id,
            thread_name=name,
        )
        return thread

    @staticmethod
    def _candidate_origins(candidate: Mapping[str, Any]) -> list[str]:
        values = candidate.get("allowed_origins")
        origins = (
            [str(item) for item in values]
            if isinstance(values, list)
            else []
        )
        target = str(candidate.get("target_origin") or "")
        if target:
            origins.insert(0, target)
        result: list[str] = []
        from .browser_session import normalize_origin

        for value in origins:
            origin = normalize_origin(value)
            if origin and origin not in result:
                result.append(origin)
        return result

    def _mailbox_pages(self) -> list[dict[str, Any]]:
        """Return only exact-origin Gmail pages; failure simply enables site fallback."""

        try:
            if self._mailbox_pages_probe is not None:
                values = self._mailbox_pages_probe()
            else:
                from .smart_browser_cdp import SmartBrowserCdpController

                values = SmartBrowserCdpController().list_pages(
                    [SMART_MONITOR_MAILBOX_ORIGIN]
                )
        except Exception:
            return []
        return [dict(item) for item in values if isinstance(item, Mapping)]

    def _local_mailbox_evidence(self, paper_ids: Iterable[str]) -> dict[str, Any]:
        papers: dict[str, Mapping[str, Any]] = {}
        for paper_id in paper_ids:
            try:
                papers[str(paper_id)] = self._smart_paper(str(paper_id))
            except SmartSubmissionSDKRuntimeError:
                continue
        if self._mailbox_evidence_collector is not None:
            try:
                result = self._mailbox_evidence_collector(papers)
                return dict(result) if isinstance(result, Mapping) else {}
            except Exception:
                return {
                    "sources": [],
                    "paper_evidence": {paper_id: [] for paper_id in papers},
                    "error_code": "LOCAL_MAILBOX_COLLECTOR_FAILED",
                }
        # Unit/integration runtimes with an injected temporary state file must
        # never discover or decrypt the user's production account registry.
        try:
            is_production_state = self.state_path == Path(SMART_STATE_PATH).resolve()
        except OSError:
            is_production_state = False
        if not is_production_state:
            return {
                "sources": [],
                "paper_evidence": {paper_id: [] for paper_id in papers},
            }
        try:
            return collect_local_mailbox_evidence(papers)
        except Exception:
            return {
                "sources": [],
                "paper_evidence": {paper_id: [] for paper_id in papers},
                "error_code": "LOCAL_MAILBOX_COLLECTOR_FAILED",
            }

    def _queue_monitor_dashboard_fallback(
        self,
        paper_ids: Iterable[str],
    ) -> dict[str, Any]:
        selected = [str(item) for item in paper_ids if str(item).strip()]
        if self._dashboard_fallback_queue is not None:
            result = self._dashboard_fallback_queue(selected)
            return dict(result) if isinstance(result, Mapping) else {}
        try:
            is_production_state = self.state_path == Path(SMART_STATE_PATH).resolve()
        except OSError:
            is_production_state = False
        if not is_production_state:
            return {
                "queued": [],
                "skipped": {
                    paper_id: "temporary runtime does not mutate production monitor state"
                    for paper_id in selected
                },
            }
        return queue_smart_monitor_dashboard_fallback(selected)

    def _pending_monitor_ids(
        self,
        selected_ids: Iterable[str] | None,
    ) -> list[str]:
        state = read_json(self.state_path, {})
        papers = state.get("papers", {}) if isinstance(state, dict) else {}
        queue = [
            str(item)
            for item in (state.get("online_queue", []) if isinstance(state, dict) else [])
            if str(item).strip()
        ]
        scope = (
            {str(item).strip() for item in selected_ids if str(item).strip()}
            if selected_ids is not None
            else None
        )
        ordered = list(dict.fromkeys([*queue, *map(str, papers.keys())]))
        return [
            paper_id
            for paper_id in ordered
            if (scope is None or paper_id in scope)
            and isinstance(papers.get(paper_id), Mapping)
            and bool(papers[paper_id].get("monitor_check_pending"))
            and str(
                papers[paper_id].get("monitor_pending_channel")
                or SMART_MONITOR_CHANNEL_MAILBOX_BATCH
            ).strip().casefold()
            == SMART_MONITOR_CHANNEL_MAILBOX_BATCH
            and str(papers[paper_id].get("status") or "").strip().casefold()
            in {"submitted", "monitoring"}
        ]

    @staticmethod
    def _parse_mailbox_monitor_batch(
        response: str,
        *,
        run_id: str,
        expected: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        match = _MONITOR_BATCH_PATTERN.search(str(response or ""))
        if match is None:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_MONITOR_BATCH_MARKERS_MISSING",
                "Mailbox monitor did not return the required batch result delimiters.",
            )
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError as exc:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_MONITOR_BATCH_JSON_INVALID",
                "Mailbox monitor returned invalid structured JSON.",
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != SMART_MONITOR_BATCH_SCHEMA
            or str(payload.get("run_id") or "") != str(run_id)
            or not isinstance(payload.get("results"), list)
        ):
            raise SmartSubmissionSDKRuntimeError(
                "SMART_MONITOR_BATCH_BINDING_MISMATCH",
                "Mailbox monitor result does not match the current run binding.",
            )
        parsed: dict[str, dict[str, Any]] = {}
        allowed_statuses = {
            "monitoring",
            "revision_needed",
            "retarget_needed",
            "rejected",
            "accepted",
            "waiting",
            "blocked",
            "no_action",
        }
        for raw in payload["results"]:
            if not isinstance(raw, Mapping):
                continue
            paper_id = str(raw.get("paper_id") or "").strip()
            binding = expected.get(paper_id)
            if binding is None or paper_id in parsed:
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_MONITOR_BATCH_PAPER_MISMATCH",
                    "Mailbox monitor returned an unexpected or duplicate paper.",
                )
            try:
                route_revision = int(raw.get("route_revision") or -1)
            except (TypeError, ValueError):
                route_revision = -1
            if (
                route_revision != int(binding.get("route_revision") or 0)
                or str(raw.get("attempt_id") or "")
                != str(binding.get("attempt_id") or "")
            ):
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_MONITOR_BATCH_ATTEMPT_MISMATCH",
                    f"Mailbox result for {paper_id} has a stale route or attempt.",
                )
            status = str(raw.get("status") or "").strip().casefold()
            if status not in allowed_statuses:
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_MONITOR_BATCH_STATUS_INVALID",
                    f"Mailbox result for {paper_id} has an invalid status.",
                )
            expected_source_ids = {
                str(item).strip().casefold()
                for item in binding.get("source_ids", [])
                if str(item).strip()
            }
            if expected_source_ids:
                raw_source_results = raw.get("source_results")
                if not isinstance(raw_source_results, list):
                    raise SmartSubmissionSDKRuntimeError(
                        "SMART_MONITOR_BATCH_SOURCES_INCOMPLETE",
                        f"Mailbox result for {paper_id} omitted per-source results.",
                    )
                actual_source_ids = {
                    str(item.get("source_id") or "").strip().casefold()
                    for item in raw_source_results
                    if isinstance(item, Mapping)
                    and str(item.get("source_id") or "").strip()
                }
                if actual_source_ids != expected_source_ids:
                    raise SmartSubmissionSDKRuntimeError(
                        "SMART_MONITOR_BATCH_SOURCES_INCOMPLETE",
                        f"Mailbox result for {paper_id} did not cover every configured source.",
                    )
            parsed[paper_id] = {str(key): value for key, value in raw.items()}
            parsed[paper_id]["status"] = status
        missing = [paper_id for paper_id in expected if paper_id not in parsed]
        if missing:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_MONITOR_BATCH_RESULT_INCOMPLETE",
                "Mailbox monitor omitted one or more requested papers.",
            )
        return parsed

    @staticmethod
    def _mailbox_sdk_prompt(base_prompt: str, context: Mapping[str, Any]) -> str:
        return f"""SDK READ-ONLY MAILBOX MONITOR CHANNEL

This is one temporary batch-monitor session selected by Python program logic.
Use only the smart_browser MCP tools and capability context_id
{context["context_id"]}. Pass that context_id to every browser tool call. Gmail
is the only web origin; other configured mailbox evidence was collected by the
local program before this Turn. Never use shell, filesystem, generic search,
account, credential, upload, or final-submit tools. Return the exact structured
batch markers even when no actionable mail exists.

{base_prompt}"""

    async def _run_mailbox_monitor_batch(
        self,
        codex: Any,
        selected_ids: Iterable[str] | None,
    ) -> dict[str, Any]:
        paper_ids = self._pending_monitor_ids(selected_ids)
        pages = self._mailbox_pages() if paper_ids else []
        local_evidence = self._local_mailbox_evidence(paper_ids) if paper_ids else {}
        local_sources = (
            local_evidence.get("sources", [])
            if isinstance(local_evidence.get("sources"), list)
            else []
        )
        if not paper_ids or (not pages and not local_sources):
            return {
                "used": False,
                "paper_ids": paper_ids,
                "handled_paper_ids": [],
                "fallback_paper_ids": paper_ids,
                "reason": (
                    "no_pending_papers"
                    if not paper_ids
                    else "no_configured_mailbox_source_available"
                ),
                "mailbox_sources": local_sources,
            }
        run_id = "mail-" + hashlib.sha256(
            (now_iso() + "|" + "|".join(paper_ids)).encode("utf-8")
        ).hexdigest()[:24]
        thread: Any | None = None
        thread_id = ""
        turn_id = ""
        context: dict[str, Any] | None = None
        bindings: dict[str, dict[str, Any]] = {}
        dispatched: list[str] = []
        usage: dict[str, Any] = {}
        fallback_ids: list[str] = []
        try:
            expected_source_ids = [
                "gmail",
                *[
                    str(item.get("source_id") or "").strip().casefold()
                    for item in local_sources
                    if isinstance(item, Mapping)
                    and str(item.get("source_id") or "").strip()
                ],
            ]
            expected_source_ids = list(dict.fromkeys(expected_source_ids))
            thread = await codex.thread_start(
                ephemeral=True,
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.root),
                model=self.model,
                sandbox=Sandbox.read_only,
                developer_instructions=(
                    "You are one bounded read-only multi-mailbox monitor. Process every "
                    "paper and every configured source in the current batch exactly once. "
                    "Use smart_browser only for Gmail; local mailbox evidence is already "
                    "in the payload. Never change mailbox or submission state directly."
                ),
            )
            thread_id = str(getattr(thread, "id", "") or "")
            if not thread_id:
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_MONITOR_BATCH_THREAD_ID_MISSING",
                    "Codex SDK did not return a mailbox monitor thread id.",
                )
            for paper_id in paper_ids:
                paper = self._smart_paper(paper_id)
                route_revision = int(paper.get("route_revision") or 0)
                attempt_id = str(paper.get("attempt_id") or "")
                record_smart_sdk_turn_state(
                    paper_id,
                    thread_id=thread_id,
                    turn_id="",
                    runtime_status="queued",
                    model=self.model,
                    monitor_only=True,
                )
                record_mailbox_monitor_worker_dispatch(
                    paper_id,
                    thread_id=thread_id,
                    route_revision=route_revision,
                    attempt_id=attempt_id,
                )
                dispatched.append(paper_id)
                current = self._smart_paper(paper_id)
                bindings[paper_id] = {
                    "route_revision": int(current.get("route_revision") or 0),
                    "attempt_id": str(current.get("attempt_id") or ""),
                    "source_ids": expected_source_ids,
                }
                self._journal_paper_update(
                    paper_id,
                    monitor_thread_id=thread_id,
                    monitor_thread_name="upo-smart-monitor-mailbox-batch",
                    monitor_status="queued",
                    monitor_model=self.model,
                    monitor_batch_run_id=run_id,
                )
            context = self.context_store.create(
                paper_id=f"monitor-batch-{run_id}",
                route_revision=0,
                attempt_id=run_id,
                thread_id=thread_id,
                allowed_origins=[SMART_MONITOR_MAILBOX_ORIGIN],
                target_origin=SMART_MONITOR_MAILBOX_ORIGIN,
                metadata={"submission_system_id": "mailbox_monitor"},
            )
            mailbox_context = dict(local_evidence)
            mailbox_context["gmail_available"] = bool(pages)
            base_prompt = build_sdk_mailbox_monitor_prompt(
                paper_ids,
                run_id=run_id,
                mailbox_context=mailbox_context,
            )
            handle = await thread.turn(
                self._mailbox_sdk_prompt(base_prompt, context),
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.root),
                model=self.model,
                sandbox=Sandbox.read_only,
            )
            turn_id = str(getattr(handle, "id", "") or "")
            for paper_id in paper_ids:
                record_smart_sdk_turn_state(
                    paper_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    runtime_status="running",
                    model=self.model,
                    monitor_only=True,
                )
            turn = await handle.run()
            response = str(getattr(turn, "final_response", "") or "")
            usage = _usage_snapshot(getattr(turn, "usage", None))
            parsed = self._parse_mailbox_monitor_batch(
                response,
                run_id=run_id,
                expected=bindings,
            )
            results: list[dict[str, Any]] = []
            for paper_id in paper_ids:
                raw = parsed[paper_id]
                aggregate = aggregate_monitor_source_results(raw)
                status = str(aggregate.get("status") or "monitoring")
                requires_dashboard = bool(aggregate.get("requires_dashboard"))
                recorded = record_visible_worker_result(
                    paper_id,
                    route_revision=int(bindings[paper_id]["route_revision"]),
                    attempt_id=str(bindings[paper_id]["attempt_id"]),
                    status=status,
                    next_action=str(aggregate.get("summary") or ""),
                    detail=str(aggregate.get("summary") or ""),
                    thread_id=thread_id,
                    source="codex_sdk_multi_mailbox_batch",
                    submission_id=str(aggregate.get("submission_id") or ""),
                    activate=False,
                )
                record_smart_monitor_observation(
                    paper_id,
                    action_kind=str(aggregate.get("action_kind") or "none"),
                    summary=str(aggregate.get("summary") or ""),
                    message_date=str(aggregate.get("message_date") or ""),
                    sender_domain=str(aggregate.get("sender_domain") or ""),
                    submission_id=str(aggregate.get("submission_id") or ""),
                    source="codex_sdk_multi_mailbox_batch",
                    source_results=aggregate.get("source_results", []),
                )
                if requires_dashboard:
                    fallback_ids.append(paper_id)
                item_ok = True
                record_smart_sdk_turn_state(
                    paper_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    runtime_status="completed" if item_ok else "failed",
                    model=self.model,
                    usage=usage,
                    monitor_only=True,
                )
                self._journal_paper_update(
                    paper_id,
                    monitor_thread_id=thread_id,
                    monitor_turn_id=turn_id,
                    monitor_status="completed" if item_ok else "failed",
                    monitor_model=self.model,
                    monitor_usage=usage,
                    recorded_status=recorded.get("status"),
                    monitor_batch_run_id=run_id,
                    completed_at=now_iso(),
                )
                results.append(
                    {
                        "ok": item_ok,
                        "paper_id": paper_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "status": recorded.get("status"),
                        "action_kind": str(aggregate.get("action_kind") or "none"),
                        "requires_dashboard": requires_dashboard,
                        "source_results": aggregate.get("source_results", []),
                        "usage": usage,
                    }
                )
            dashboard_fallback = self._queue_monitor_dashboard_fallback(fallback_ids)
            fallback_queued = [
                str(item)
                for item in dashboard_fallback.get("queued", [])
                if str(item).strip()
            ]
            fallback_failed = {
                str(paper_id): str(reason)
                for paper_id, reason in dict(
                    dashboard_fallback.get("failed", {})
                ).items()
                if str(paper_id).strip()
            }
            if fallback_failed:
                for item in results:
                    paper_id = str(item.get("paper_id") or "")
                    if paper_id not in fallback_failed:
                        continue
                    item["ok"] = False
                    item["dashboard_fallback_error"] = fallback_failed[paper_id]
            handled_ids = [
                paper_id for paper_id in paper_ids if paper_id not in set(fallback_queued)
            ]
            self._journal_event(
                "mailbox_monitor_batch_completed",
                "__batch__",
                run_id=run_id,
                paper_ids=paper_ids,
                fallback_paper_ids=fallback_queued,
                fallback_failed=fallback_failed,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            return {
                "used": True,
                "ok": all(item.get("ok") for item in results),
                "run_id": run_id,
                "paper_ids": paper_ids,
                "handled_paper_ids": handled_ids,
                "fallback_paper_ids": fallback_queued,
                "dashboard_fallback": dashboard_fallback,
                "mailbox_sources": [
                    item for item in local_sources if isinstance(item, Mapping)
                ],
                "thread_id": thread_id,
                "turn_id": turn_id,
                "usage": usage,
                "results": results,
            }
        except Exception as exc:
            error_code = str(
                getattr(exc, "code", "SMART_MONITOR_BATCH_TURN_FAILED")
            )
            failures: list[dict[str, Any]] = []
            for paper_id in dispatched:
                binding = bindings.get(paper_id, {})
                try:
                    failure = record_smart_sdk_turn_failure(
                        paper_id,
                        route_revision=int(binding.get("route_revision") or 0),
                        attempt_id=str(binding.get("attempt_id") or ""),
                        thread_id=thread_id,
                        detail=f"{error_code}: {type(exc).__name__}",
                    )
                except Exception as failure_exc:
                    failure = {"status": "monitoring", "error": type(failure_exc).__name__}
                try:
                    record_smart_sdk_turn_state(
                        paper_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        runtime_status="failed",
                        model=self.model,
                        error=f"{error_code}: {exc}",
                        monitor_only=True,
                    )
                except Exception:
                    pass
                failures.append(
                    {
                        "ok": False,
                        "paper_id": paper_id,
                        "status": failure.get("status"),
                        "code": error_code,
                    }
                )
            self._journal_event(
                "mailbox_monitor_batch_failed",
                "__batch__",
                run_id=run_id,
                paper_ids=paper_ids,
                error_code=error_code,
                error_type=type(exc).__name__,
            )
            return {
                "used": True,
                "ok": False,
                "run_id": run_id,
                "paper_ids": paper_ids,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "code": error_code,
                "message": str(exc),
                "results": failures,
            }
        finally:
            if context is not None:
                self.context_store.retire(str(context.get("context_id") or ""))

    def _paper_upload_files(
        self,
        paper: Mapping[str, Any],
    ) -> tuple[list[Path], list[Path]]:
        roots: list[Path] = []
        for key in ("source_package_dir", "paper_dir"):
            raw = str(paper.get(key) or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser().resolve(strict=False)
            if path.is_dir() and path not in roots:
                roots.append(path)
        files: list[Path] = []
        for key in (
            "uploaded_manuscript_zip",
            "uploaded_manuscript_file",
            "upload_package",
            "cover_letter_upload_file",
        ):
            raw = str(paper.get(key) or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser().resolve(strict=False)
            if path.is_file() and not path.is_symlink():
                files.append(path)
                parent = path.parent
                if parent not in roots:
                    roots.append(parent)
        upload_suffixes = {
            ".bib",
            ".csv",
            ".doc",
            ".docx",
            ".eps",
            ".jpeg",
            ".jpg",
            ".json",
            ".md",
            ".pdf",
            ".png",
            ".rtf",
            ".tex",
            ".tif",
            ".tiff",
            ".txt",
            ".xls",
            ".xlsx",
            ".xml",
            ".zip",
        }
        for root in roots:
            try:
                candidates = sorted(
                    (
                        path
                        for path in root.rglob("*")
                        if path.is_file()
                        and not path.is_symlink()
                        and path.suffix.casefold() in upload_suffixes
                    ),
                    key=lambda path: (len(path.parts), path.name.casefold()),
                )
            except OSError:
                continue
            for path in candidates:
                if len(files) >= 160:
                    break
                files.append(path.resolve(strict=False))
        deduplicated: list[Path] = []
        for path in files:
            if path not in deduplicated:
                deduplicated.append(path)
        return deduplicated, roots

    def _create_browser_context(
        self,
        candidate: Mapping[str, Any],
        *,
        paper: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        files, roots = self._paper_upload_files(paper)
        authorization = (
            paper.get("final_submit_authorization")
            if isinstance(paper.get("final_submit_authorization"), dict)
            else {}
        )
        return self.context_store.create(
            paper_id=str(candidate["paper_id"]),
            route_revision=int(candidate.get("route_revision") or 0),
            attempt_id=str(candidate.get("attempt_id") or paper.get("attempt_id") or ""),
            thread_id=thread_id,
            allowed_origins=self._candidate_origins(candidate),
            allowed_files=files,
            allowed_file_roots=roots,
            target_origin=str(candidate.get("target_origin") or ""),
            metadata={
                "submission_system_id": str(
                    candidate.get("submission_system_id") or ""
                ),
                "target_journal": str(candidate.get("target_journal") or ""),
                "authorization_id": str(
                    authorization.get("authorization_id") or ""
                ),
            },
        )

    @staticmethod
    def _sdk_prompt(
        base_prompt: str,
        context: Mapping[str, Any],
        *,
        preserve_manual_fields: bool = False,
    ) -> str:
        channel = f"""SDK BROWSER EXECUTION CHANNEL

This turn runs inside the paper's fixed Codex SDK session. The Python program,
not an AI scheduler, selected this paper and allocated this worker slot.

Use only the `smart_browser` MCP tools:
- capability context_id: {context["context_id"]}
- first call browser_pages, then browser_snapshot;
- call browser_files only when an upload control needs a paper file, and pass
  the returned file_id to browser_upload;
- pass this same context_id to every browser tool call;
- use only control refs from the latest snapshot;
- browser_click cannot activate a final-submit control;
- when and only when the prompt contains a current exact-attempt authorization,
  use browser_final_submit_once exactly once and never retry it;
- do not use shell, exec, filesystem, generic browser, search, or account tools.

Return the required SMART_SUBMISSION_* markers even when a browser tool reports
a bounded blocker.

"""
        if preserve_manual_fields:
            channel += """MANUAL TAKEOVER RECONCILIATION

The user has just completed an explicit manual edit on the current visible
page. Snapshot and validate that existing page before acting. Preserve every
already completed field, selection, and uploaded file; do not restart the form,
do not navigate back to registration, and do not replace user-entered values.
Continue only from the first still-incomplete visible requirement.

"""
        return channel + base_prompt

    @staticmethod
    def _parse_markers(
        response: str,
        *,
        paper_id: str,
        route_revision: int,
        attempt_id: str,
    ) -> dict[str, str]:
        markers = {
            key: value.strip()
            for key, value in _MARKER_PATTERN.findall(str(response or ""))
        }
        if markers.get("PAPER_ID") != paper_id:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_RESULT_PAPER_MISMATCH",
                "SDK Turn 返回的论文编号与固定会话不一致。",
            )
        try:
            returned_revision = int(markers.get("ROUTE_REVISION") or -1)
        except ValueError:
            returned_revision = -1
        if returned_revision != int(route_revision):
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_RESULT_ROUTE_MISMATCH",
                "SDK Turn 返回了过期的投稿路线版本。",
            )
        if markers.get("ATTEMPT_ID") != attempt_id:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_RESULT_ATTEMPT_MISMATCH",
                "SDK Turn 返回了过期的投稿尝试编号。",
            )
        status = str(markers.get("STATUS") or "").strip().casefold()
        if status not in _VALID_RESULT_STATUSES:
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_RESULT_STATUS_INVALID",
                "SDK Turn 未返回完整有效的 SMART_SUBMISSION_STATUS。",
            )
        markers["STATUS"] = status
        return markers

    def _paper_is_running_attempt(
        self,
        paper_id: str,
        attempt_id: str,
        thread_id: str,
    ) -> bool:
        try:
            paper = self._smart_paper(paper_id)
        except SmartSubmissionSDKRuntimeError:
            return False
        return bool(
            str(paper.get("workflow_status") or "").casefold() == "running"
            and str(paper.get("attempt_id") or "") == attempt_id
            and str(paper.get("thread_id") or "") == thread_id
        )

    def _record_response(
        self,
        *,
        paper_id: str,
        route_revision: int,
        attempt_id: str,
        thread_id: str,
        response: str,
    ) -> dict[str, Any]:
        if not self._paper_is_running_attempt(paper_id, attempt_id, thread_id):
            paper = self._smart_paper(paper_id)
            return {
                "paper_id": paper_id,
                "recorded_by_tool": True,
                "status": str(paper.get("status") or ""),
                "workflow_status": str(paper.get("workflow_status") or ""),
            }
        monitor_pending = bool(self._smart_paper(paper_id).get("monitor_check_pending"))
        markers = self._parse_markers(
            response,
            paper_id=paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
        )
        recorded = record_smart_retarget_closure_result(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
            status=markers["STATUS"],
            next_action=markers.get("NEXT", ""),
            detail=markers.get("NEXT", ""),
            thread_id=thread_id,
            source="codex_sdk_browser_final",
            submission_id=markers.get("ID", ""),
            closed_submission_ids=markers.get("CLOSED_IDS", ""),
            evidence_path=markers.get("EVIDENCE_PATH", ""),
            confirmation_url=markers.get("CONFIRMATION_URL", ""),
            activate=False,
        )
        if monitor_pending:
            status = str(markers["STATUS"] or "monitoring")
            action_kind = {
                "accepted": "accepted",
                "rejected": "retarget",
                "retarget_needed": "retarget",
                "revision_needed": "unknown",
            }.get(status, "none")
            record_smart_monitor_observation(
                paper_id,
                action_kind=action_kind,
                summary=markers.get("NEXT", ""),
                submission_id=markers.get("ID", ""),
                source="codex_sdk_journal_dashboard",
                source_results=[
                    {
                        "source_id": "journal_dashboard",
                        "source_label": "期刊投稿后台",
                        "source_kind": "journal_dashboard",
                        "available": status not in {"waiting", "blocked"},
                        "status": status,
                        "action_kind": action_kind,
                        "summary": markers.get("NEXT", ""),
                        "submission_id": markers.get("ID", ""),
                        "error_code": (
                            "JOURNAL_DASHBOARD_UNAVAILABLE"
                            if status in {"waiting", "blocked"}
                            else ""
                        ),
                    }
                ],
            )
        return recorded

    async def _run_candidate(
        self,
        codex: Any,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        paper_id = str(candidate.get("paper_id") or "")
        route_revision = int(candidate.get("route_revision") or 0)
        attempt_id = str(candidate.get("attempt_id") or "")
        thread: Any | None = None
        thread_id = ""
        context: dict[str, Any] | None = None
        dispatched = False
        turn_id = ""
        monitor_only = False
        try:
            paper = self._smart_paper(paper_id)
            preserve_manual_fields = bool(
                paper.get("manual_takeover_reconcile_pending")
            )
            monitor_only = bool(paper.get("monitor_check_pending")) and str(
                paper.get("status") or ""
            ).strip().casefold() in {"submitted", "monitoring"}
            record_smart_credential_broker_state(
                paper_id,
                {
                    "ok": False,
                    "code": "SMART_CREDENTIAL_BROKER_STARTING",
                    "status": "starting",
                    "account_id": str(
                        candidate.get("credential_account_id")
                        or paper.get("credential_account_id")
                        or ""
                    ),
                    "system_id": str(
                        candidate.get("submission_system_id")
                        or paper.get("submission_system_id")
                        or ""
                    ),
                    "origin": str(candidate.get("target_origin") or ""),
                    "next_action": "trusted_credential_broker_login",
                    "value_free": True,
                },
            )
            broker_result = self._broker().ensure_session(
                paper_id=paper_id,
                paper=paper,
                candidate=candidate,
            )
            paper = record_smart_credential_broker_state(
                paper_id,
                dict(broker_result),
            )
            self._journal_paper_update(
                paper_id,
                credential_broker_status=str(
                    broker_result.get("status") or ""
                ),
                credential_broker_code=str(
                    broker_result.get("code") or ""
                ),
                credential_broker_value_free=True,
            )
            self._journal_event(
                "credential_broker_result",
                paper_id,
                status=str(broker_result.get("status") or ""),
                code=str(broker_result.get("code") or ""),
                value_free=True,
            )
            if not broker_result.get("ok"):
                return {
                    "ok": True,
                    "deferred": True,
                    "paper_id": paper_id,
                    "thread_id": "",
                    "turn_id": "",
                    "status": str(broker_result.get("status") or "waiting"),
                    "code": str(broker_result.get("code") or ""),
                    "next_action": str(broker_result.get("next_action") or ""),
                    "value_free": True,
                }
            candidate = {
                **dict(candidate),
                "credential_account_id": str(
                    paper.get("credential_account_id")
                    or broker_result.get("account_id")
                    or ""
                ),
            }
            # Federated publishers may refresh the same authenticated account
            # from the dashboard origin to the manuscript origin. Reissue only
            # through the strict unused-capability path, after this final
            # broker refresh and before a worker owns the capability.
            paper = self._reissue_final_authorization_after_broker_refresh(
                paper_id,
                paper,
                route_revision=route_revision,
                attempt_id=attempt_id,
            )
            thread = await self._thread_for_paper(
                codex,
                paper_id,
                monitor_only=monitor_only,
            )
            thread_id = str(getattr(thread, "id", "") or "")
            paper = self._smart_paper(paper_id)
            attempt_id = str(paper.get("attempt_id") or attempt_id)
            record_visible_worker_dispatch(
                paper_id,
                thread_id=thread_id,
                route_revision=route_revision,
                attempt_id=attempt_id,
            )
            dispatched = True
            paper = self._smart_paper(paper_id)
            context = self._create_browser_context(
                {
                    **dict(candidate),
                    "attempt_id": attempt_id,
                },
                paper=paper,
                thread_id=thread_id,
            )
            base_prompt = build_sdk_browser_worker_prompt(
                paper_id,
                route_revision=route_revision,
            )
            prompt = self._sdk_prompt(
                base_prompt,
                context,
                preserve_manual_fields=preserve_manual_fields,
            )
            handle = await thread.turn(
                prompt,
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.root),
                model=self.model,
                sandbox=Sandbox.read_only,
            )
            turn_id = str(getattr(handle, "id", "") or "")
            record_smart_sdk_turn_state(
                paper_id,
                thread_id=thread_id,
                turn_id=turn_id,
                runtime_status="running",
                model=self.model,
                monitor_only=monitor_only,
            )
            self._journal_paper_update(
                paper_id,
                thread_id=thread_id,
                thread_name=self._thread_name(paper_id),
                turn_id=turn_id,
                status="running",
                model=self.model,
                route_revision=route_revision,
                attempt_id=attempt_id,
                context_id=context["context_id"],
                prompt_version=SMART_SUBMISSION_SDK_PROMPT_VERSION,
                error_code="",
                error_type="",
                error="",
                failure_record={},
            )
            self._journal_event(
                "turn_started",
                paper_id,
                thread_id=thread_id,
                turn_id=turn_id,
                route_revision=route_revision,
                attempt_id=attempt_id,
            )
            turn = await handle.run()
            response = str(getattr(turn, "final_response", "") or "")
            usage = _usage_snapshot(getattr(turn, "usage", None))
            recorded = self._record_response(
                paper_id=paper_id,
                route_revision=route_revision,
                attempt_id=attempt_id,
                thread_id=thread_id,
                response=response,
            )
            record_smart_sdk_turn_state(
                paper_id,
                thread_id=thread_id,
                turn_id=turn_id,
                runtime_status="completed",
                model=self.model,
                usage=usage,
                monitor_only=monitor_only,
            )
            self._journal_paper_update(
                paper_id,
                turn_id=turn_id,
                status="completed",
                turn_status=_status_value(getattr(turn, "status", "")),
                usage=usage,
                final_response=response[-12_000:],
                recorded_status=recorded.get("status"),
                error_code="",
                error_type="",
                error="",
                failure_record={},
                completed_at=now_iso(),
            )
            self._journal_event(
                "turn_completed",
                paper_id,
                thread_id=thread_id,
                turn_id=turn_id,
                status=recorded.get("status"),
            )
            return {
                "ok": True,
                "paper_id": paper_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": recorded.get("status"),
                "recorded_by_tool": bool(recorded.get("recorded_by_tool")),
                "usage": usage,
            }
        except Exception as exc:
            error_code = str(
                getattr(exc, "code", "SMART_SDK_BROWSER_TURN_FAILED")
            )
            failure: dict[str, Any] = {}
            if dispatched and thread_id:
                try:
                    failure = record_smart_sdk_turn_failure(
                        paper_id,
                        route_revision=route_revision,
                        attempt_id=attempt_id,
                        thread_id=thread_id,
                        detail=f"{error_code}: {exc}",
                    )
                except Exception as record_exc:
                    failure = {
                        "record_error": type(record_exc).__name__,
                    }
                try:
                    record_smart_sdk_turn_state(
                        paper_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        runtime_status="failed",
                        model=self.model,
                        error=f"{error_code}: {exc}",
                        monitor_only=monitor_only,
                    )
                except Exception:
                    pass
            self._journal_paper_update(
                paper_id,
                thread_id=thread_id,
                turn_id=turn_id,
                status="failed",
                error_code=error_code,
                error_type=type(exc).__name__,
                error=str(exc)[:2000],
                failure_record={
                    key: value
                    for key, value in failure.items()
                    if key != "state"
                },
            )
            self._journal_event(
                "turn_failed",
                paper_id,
                thread_id=thread_id,
                turn_id=turn_id,
                error_code=error_code,
                error_type=type(exc).__name__,
            )
            return {
                "ok": False,
                "paper_id": paper_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "code": error_code,
                "message": str(exc),
            }
        finally:
            if context is not None:
                self.context_store.retire(str(context.get("context_id") or ""))

    async def _reconcile_running(self, codex: Any) -> list[dict[str, Any]]:
        state = read_json(self.state_path, {})
        papers = state.get("papers", {}) if isinstance(state, dict) else {}
        results: list[dict[str, Any]] = []
        for paper_id, paper in list(papers.items()) if isinstance(papers, dict) else []:
            if self._stop_after_current.is_set():
                break
            monitor_only = bool(
                isinstance(paper, Mapping)
                and paper.get("monitor_check_pending")
                and str(paper.get("status") or "").strip().casefold()
                in {"submitted", "monitoring"}
            )
            thread_id = str(
                paper.get("sdk_monitor_thread_id" if monitor_only else "sdk_thread_id")
                or ""
            ) if isinstance(paper, Mapping) else ""
            if (
                not isinstance(paper, Mapping)
                or str(paper.get("workflow_status") or "").casefold() != "running"
                or not thread_id
            ):
                continue
            turn_id = str(
                paper.get("sdk_monitor_turn_id" if monitor_only else "sdk_turn_id")
                or ""
            )
            attempt_id = str(paper.get("attempt_id") or "")
            route_revision = int(paper.get("route_revision") or 0)
            if monitor_only:
                failure = record_smart_sdk_turn_failure(
                    str(paper_id),
                    route_revision=route_revision,
                    attempt_id=attempt_id,
                    thread_id=thread_id,
                    detail=(
                        "Recovered an interrupted ephemeral monitoring Turn; "
                        "the next scheduled check will use a fresh lightweight session."
                    ),
                )
                record_smart_sdk_turn_state(
                    str(paper_id),
                    thread_id=thread_id,
                    turn_id=turn_id,
                    runtime_status="failed",
                    model=self.model,
                    error="interrupted ephemeral monitoring Turn",
                    monitor_only=True,
                )
                results.append(
                    {
                        "paper_id": str(paper_id),
                        "recovered": True,
                        "status": failure.get("status"),
                        "monitor_session_rotated": True,
                    }
                )
                continue
            if not turn_id:
                failure = record_smart_sdk_turn_failure(
                    str(paper_id),
                    route_revision=route_revision,
                    attempt_id=attempt_id,
                    thread_id=thread_id,
                    detail="Recovered a running SDK worker without a persisted Turn id.",
                )
                results.append(
                    {
                        "paper_id": str(paper_id),
                        "recovered": True,
                        "status": failure.get("status"),
                    }
                )
                continue
            try:
                thread = await codex.thread_resume(
                    thread_id,
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(self.root),
                    model=self.model,
                    sandbox=Sandbox.read_only,
                )
                history = await thread.read(include_turns=True)
                turn = next(
                    (
                        item
                        for item in _turns(history)
                        if str(getattr(item, "id", "") or "") == turn_id
                    ),
                    None,
                )
                if turn is None:
                    raise SmartSubmissionSDKRuntimeError(
                        "SMART_SDK_RECOVERY_TURN_MISSING",
                        "固定会话中找不到上次运行的 Turn。",
                    )
                status = _status_value(getattr(turn, "status", ""))
                if status in {"inProgress", "in_progress"}:
                    record_smart_sdk_turn_state(
                        str(paper_id),
                        thread_id=thread_id,
                        turn_id=turn_id,
                        runtime_status="waiting_existing_turn",
                        model=self.model,
                    )
                    results.append(
                        {
                            "paper_id": str(paper_id),
                            "recovered": True,
                            "status": "waiting_existing_turn",
                        }
                    )
                    continue
                if status == "completed":
                    response = _turn_final_response(turn)
                    recorded = self._record_response(
                        paper_id=str(paper_id),
                        route_revision=route_revision,
                        attempt_id=attempt_id,
                        thread_id=thread_id,
                        response=response,
                    )
                    record_smart_sdk_turn_state(
                        str(paper_id),
                        thread_id=thread_id,
                        turn_id=turn_id,
                        runtime_status="completed",
                        model=self.model,
                    )
                    results.append(
                        {
                            "paper_id": str(paper_id),
                            "recovered": True,
                            "status": recorded.get("status"),
                        }
                    )
                    continue
                raise SmartSubmissionSDKRuntimeError(
                    "SMART_SDK_RECOVERY_TURN_NOT_COMPLETED",
                    f"上次 SDK Turn 状态为 {status or 'unknown'}。",
                )
            except Exception as exc:
                try:
                    failure = record_smart_sdk_turn_failure(
                        str(paper_id),
                        route_revision=route_revision,
                        attempt_id=attempt_id,
                        thread_id=thread_id,
                        detail=f"SDK recovery failed: {type(exc).__name__}.",
                    )
                except Exception:
                    failure = {}
                results.append(
                    {
                        "paper_id": str(paper_id),
                        "recovered": False,
                        "code": str(
                            getattr(exc, "code", "SMART_SDK_RECOVERY_FAILED")
                        ),
                        "status": failure.get("status"),
                    }
                )
        return results

    async def _run_once_async(
        self,
        selected_ids: Iterable[str] | None,
    ) -> dict[str, Any]:
        account = self.ensure_ready()
        health = dict(self._browser_health_probe(ensure=True))
        if not health.get("operational"):
            raise SmartSubmissionSDKRuntimeError(
                "SMART_SDK_BROWSER_UNAVAILABLE",
                "两个受控 CDP 浏览器端口都不可用，未启动网页 Turn。",
            )
        async with self._codex_factory() as codex:
            recovered = await self._reconcile_running(codex)
            selected_scope = (
                [str(item) for item in selected_ids if str(item)]
                if selected_ids is not None
                else None
            )
            mailbox_batch = await self._run_mailbox_monitor_batch(
                codex,
                selected_scope,
            )
            mailbox_ids = {
                str(item)
                for item in mailbox_batch.get("handled_paper_ids", [])
                if mailbox_batch.get("used") and str(item)
            }
            regular_scope = (
                [item for item in selected_scope if item not in mailbox_ids]
                if selected_scope is not None
                else None
            )
            attempted_cycles: set[tuple[str, str]] = set()
            attempted_auto_authorizations: set[tuple[str, str]] = set()
            dispatched_ids: list[str] = list(mailbox_ids)
            results: list[dict[str, Any]] = [
                dict(item)
                for item in mailbox_batch.get("results", [])
                if isinstance(item, Mapping)
            ]
            auto_authorizations: list[dict[str, Any]] = []
            plan: dict[str, Any] = {}
            while not self._stop_after_current.is_set():
                auto_authorizations.extend(
                    self._auto_authorize_ready_final_submits(
                        regular_scope,
                        # Mailbox-monitored papers were already removed from their
                        # read-only checkpoints and cannot be auto-authorized here.
                        attempted_auto_authorizations,
                    )
                )
                plan = smart_submission_program_plan(
                    selected_ids=regular_scope,
                    persist=True,
                    sync_ledger=True,
                )
                candidates = [
                    dict(item)
                    for item in plan.get("dispatch_candidates", [])
                    if isinstance(item, Mapping)
                    and (
                        str(item.get("paper_id") or ""),
                        str(
                            item.get("cycle")
                            or item.get("action")
                            or item.get("attempt_id")
                            or "unversioned"
                        ),
                    )
                    not in attempted_cycles
                    and str(item.get("executor") or "")
                    in {
                        ProgramExecutor.CODEX_SDK_BROWSER.value,
                        ProgramExecutor.SITE_ADAPTER.value,
                    }
                ]
                if not candidates:
                    break
                round_ids = [
                    str(item.get("paper_id") or "") for item in candidates
                ]
                attempted_cycles.update(
                    (
                        str(item.get("paper_id") or ""),
                        str(
                            item.get("cycle")
                            or item.get("action")
                            or item.get("attempt_id")
                            or "unversioned"
                        ),
                    )
                    for item in candidates
                )
                dispatched_ids.extend(round_ids)
                groups: dict[str, list[dict[str, Any]]] = {}
                for item in candidates:
                    group_key = str(
                        item.get("target_origin")
                        or item.get("submission_system_id")
                        or item.get("paper_id")
                    )
                    groups.setdefault(group_key, []).append(item)

                async def run_origin_group(
                    group: list[dict[str, Any]],
                ) -> list[dict[str, Any]]:
                    group_results: list[dict[str, Any]] = []
                    for candidate in group:
                        group_results.append(await self._run_candidate(codex, candidate))
                    return group_results

                grouped_results = await asyncio.gather(
                    *(run_origin_group(group) for group in groups.values())
                )
                for group_results in grouped_results:
                    results.extend(group_results)
        return {
            "ok": all(
                item.get("ok")
                for item in [*auto_authorizations, *results]
            ) if (auto_authorizations or results) else True,
            "schema": SMART_SUBMISSION_SDK_RUNTIME_SCHEMA,
            "model": self.model,
            "account": account,
            "browser_health": health,
            "mailbox_monitor_batch": mailbox_batch,
            "program_plan_id": plan.get("plan_id"),
            "candidate_ids": dispatched_ids,
            "dispatched_count": len(dispatched_ids),
            "auto_authorizations": auto_authorizations,
            "recovered": recovered,
            "results": results,
            "updated_at": now_iso(),
        }

    def run_once(
        self,
        selected_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        with FileLock(self.dispatch_lock_path):
            return asyncio.run(self._run_once_async(selected_ids))

    def _background_entry(self) -> None:
        try:
            while True:
                with self._background_lock:
                    if not self._pending_full_sweep and not self._pending_selected:
                        self._background_thread = None
                        return
                    full_sweep = self._pending_full_sweep
                    selected = sorted(self._pending_selected)
                    self._pending_full_sweep = False
                    self._pending_selected.clear()
                try:
                    self._last_result = self.run_once(
                        None if full_sweep else selected
                    )
                    self._last_error = ""
                except Exception as exc:
                    self._last_error = (
                        f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
                    )
                    self._last_result = {
                        "ok": False,
                        "code": str(
                            getattr(exc, "code", "SMART_SDK_BACKGROUND_FAILED")
                        ),
                        "message": str(exc),
                    }
                if self._stop_after_current.is_set():
                    return
        finally:
            with self._background_lock:
                if self._background_thread is threading.current_thread():
                    self._background_thread = None

    def start_background(
        self,
        selected_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = {
            str(item).strip()
            for item in (selected_ids or [])
            if str(item).strip()
        }
        with self._background_lock:
            self._pending_selected.update(selected)
            if not selected:
                self._pending_full_sweep = True
            self._stop_after_current.clear()
            if self._background_thread is not None and self._background_thread.is_alive():
                return {
                    "ok": True,
                    "started": False,
                    "already_running": True,
                    "queued_selection": sorted(self._pending_selected),
                    "queued_full_sweep": self._pending_full_sweep,
                }
            thread = threading.Thread(
                target=self._background_entry,
                name="SmartSubmissionSDKRuntime",
                daemon=True,
            )
            self._background_thread = thread
            thread.start()
        return {
            "ok": True,
            "started": True,
            "selected_ids": sorted(selected),
            "model": self.model,
        }

    def pause_after_current(self) -> dict[str, Any]:
        self._stop_after_current.set()
        with self._background_lock:
            running = bool(
                self._background_thread is not None
                and self._background_thread.is_alive()
            )
        return {
            "ok": True,
            "pause_requested": True,
            "current_turn_will_finish": running,
        }

    def status(self) -> dict[str, Any]:
        with self._background_lock:
            running = bool(
                self._background_thread is not None
                and self._background_thread.is_alive()
            )
            pending = sorted(self._pending_selected)
            pending_full_sweep = self._pending_full_sweep
        journal = self._load_journal()
        papers = journal.get("papers", {}) if isinstance(journal.get("papers"), dict) else {}
        return {
            "schema": SMART_SUBMISSION_SDK_RUNTIME_SCHEMA,
            "running": running,
            "pause_requested": self._stop_after_current.is_set(),
            "pending_selected_ids": pending,
            "pending_full_sweep": pending_full_sweep,
            "configured_model": self.model,
            "fixed_session_count": sum(
                1
                for item in papers.values()
                if isinstance(item, Mapping) and str(item.get("thread_id") or "")
            ),
            "journal_path": str(self.journal_path),
            "context_store_path": str(self.context_store_path),
            "papers": copy.deepcopy(papers),
            "last_result": copy.deepcopy(self._last_result),
            "last_error": self._last_error,
            "updated_at": str(journal.get("updated_at") or ""),
        }


_DEFAULT_RUNTIME_LOCK = threading.Lock()
_DEFAULT_RUNTIME: SmartSubmissionSDKRuntime | None = None


def default_smart_submission_sdk_runtime() -> SmartSubmissionSDKRuntime:
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            _DEFAULT_RUNTIME = SmartSubmissionSDKRuntime()
        return _DEFAULT_RUNTIME


def start_smart_submission_sdk_background(
    selected_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    return default_smart_submission_sdk_runtime().start_background(selected_ids)


def pause_smart_submission_sdk_after_current() -> dict[str, Any]:
    return default_smart_submission_sdk_runtime().pause_after_current()


def smart_submission_sdk_status() -> dict[str, Any]:
    return default_smart_submission_sdk_runtime().status()


__all__ = [
    "SMART_SUBMISSION_SDK_JOURNAL_SCHEMA",
    "SMART_SUBMISSION_SDK_PROMPT_VERSION",
    "SMART_SUBMISSION_SDK_RUNTIME_SCHEMA",
    "SmartSubmissionSDKRuntime",
    "SmartSubmissionSDKRuntimeError",
    "default_smart_submission_sdk_runtime",
    "pause_smart_submission_sdk_after_current",
    "smart_submission_sdk_status",
    "start_smart_submission_sdk_background",
]
