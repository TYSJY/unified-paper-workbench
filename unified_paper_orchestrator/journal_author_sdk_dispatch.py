from __future__ import annotations
from .sdk_model_catalog import read_sdk_models

import asyncio
import copy
import hashlib
import inspect
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort, ThreadSourceKind

from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .journal_author_output_contract import (
    JournalAuthorContractResult,
    mark_package_selection_stale,
    sync_package_metadata_fingerprint,
    validate_journal_author_input,
    validate_journal_author_output,
    validate_journal_selection,
)
from .journal_author_prompt_integrity import (
    build_journal_author_combined_prompt,
    build_journal_author_step_prompt,
    validate_journal_author_prompt,
)
from .journal_submission_policy import finalize_journal_submission_policy
from .journal_author_workflow import (
    JournalAuthorWorkflowError,
    default_journal_author_workflow_snapshot,
    journal_author_workflow_prompt_fingerprint,
    journal_author_workflow_step,
    normalize_journal_author_workflow_snapshot,
)
from .step_execution import execution_profile_catalog_error, normalize_model_catalog, resolve_execution_profile


JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA = "unified-paper-orchestrator/journal-author-sdk-dispatch/v1"
_ACTIVE_PATHS: set[str] = set()
_ACTIVE_PATHS_LOCK = threading.Lock()


class JournalAuthorSDKDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class JournalAuthorPaperPlan:
    paper_id: str
    request_id: str
    workflow_snapshot: Mapping[str, Any] | None = None
    reuse_valid_artifacts: bool = True
    start_step: int = 1
    run_until_step: int = 0
    run_mode: str = "full"

    def validate(self) -> None:
        paper_id = self.paper_id.strip()
        if not paper_id or Path(paper_id).name != paper_id or paper_id in {".", ".."}:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_PAPER_ID_INVALID", "paper id must be one safe directory name")
        if not self.request_id.strip():
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_REQUEST_ID_MISSING", "journal-author request id is required")
        try:
            workflow = normalize_journal_author_workflow_snapshot(
                self.workflow_snapshot or default_journal_author_workflow_snapshot()
            )
        except JournalAuthorWorkflowError as exc:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_WORKFLOW_INVALID", str(exc)) from exc
        run_until = int(self.run_until_step or workflow["step_count"])
        if not 1 <= int(self.start_step) <= run_until <= int(workflow["step_count"]):
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_RUN_RANGE_INVALID", "journal-author workflow run range is invalid")
        if str(self.run_mode or "full") not in {
            "full",
            "selection",
            "package",
            "retarget",
        }:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_RUN_MODE_INVALID", "journal-author run mode is invalid")


def _status_value(value: Any) -> str:
    root = getattr(value, "root", value)
    text = str(getattr(root, "type", getattr(value, "value", value)) or "").strip()
    folded = text.replace("_", "").replace("-", "").casefold()
    if folded in {"completed", "complete"}:
        return "completed"
    if folded in {"inprogress", "running", "active"}:
        return "in_progress"
    if folded in {"failed", "error"}:
        return "failed"
    if folded in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    return text


def _thread_turns(response: Any) -> list[Any]:
    thread = getattr(response, "thread", response)
    return list(getattr(thread, "turns", []) or [])


def _usage_snapshot(turn: Any) -> dict[str, Any] | None:
    usage = getattr(turn, "usage", None)
    if usage is None:
        return None
    result: dict[str, Any] = {}
    for group in ("last", "total"):
        value = getattr(usage, group, None)
        if value is None and isinstance(usage, Mapping):
            value = usage.get(group)
        if value is None:
            continue
        fields: dict[str, int] = {}
        for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            item = getattr(value, name, None)
            if item is None and isinstance(value, Mapping):
                item = value.get(name)
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                fields[name] = item
        if fields:
            result[group] = fields
    return result or None


def _turn_has_agent_activity(turn: Any) -> bool:
    items = getattr(turn, "items", None)
    if items is None and isinstance(turn, Mapping):
        items = turn.get("items")
    if items is None:
        return True
    for item in list(items or []):
        root = getattr(item, "root", item)
        if isinstance(item, Mapping) and "root" in item:
            root = item.get("root")
        item_type = getattr(root, "type", None)
        if item_type is None and isinstance(root, Mapping):
            item_type = root.get("type")
        token = str(getattr(item_type, "value", item_type) or "").replace("_", "").replace("-", "").casefold()
        if token not in {"", "usermessage", "user"}:
            return True
    return False


async def _completed_turn_from_thread_history(
    thread: Any,
    turn_id: str,
) -> Any | None:
    """Return the authoritative completed turn when ``run()`` returns a shell.

    Recent Codex SDK builds can return a completed ``Turn`` whose ``items``
    collection is empty even though the fixed thread history already contains
    the assistant messages, tool calls, and terminal event.  Re-reading the
    same turn is safe: it does not send another prompt and gives the dispatcher
    enough evidence to validate the artifacts without wasting a retry.
    """

    if not turn_id:
        return None
    try:
        history = await thread.read(include_turns=True)
    except BaseException:
        return None
    for candidate in _thread_turns(history):
        if str(getattr(candidate, "id", "") or "") != turn_id:
            continue
        if _status_value(getattr(candidate, "status", "")) != "completed":
            return None
        if not _turn_has_agent_activity(candidate):
            return None
        return candidate
    return None


class JournalAuthorSDKDispatcher:
    """Program-owned workflow dispatcher with one fixed SDK thread per paper."""

    def __init__(
        self,
        *,
        root: str | Path,
        journal_path: str | Path,
        lock_path: str | Path,
        codex_factory: Callable[[], Any],
        max_parallel_papers: int = 1,
        model: str = "",
        progress_callback: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
        should_continue: Callable[[str, Mapping[str, Any]], Any] | None = None,
        active_turn_recheck_seconds: float = 3.0,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.journal_path = Path(journal_path).expanduser().resolve()
        self.lock_path = Path(lock_path).expanduser().resolve()
        self.codex_factory = codex_factory
        self.max_parallel_papers = int(max_parallel_papers)
        if not 1 <= self.max_parallel_papers <= 6:
            raise ValueError("journal-author parallel paper limit must be 1-6")
        self.model = str(model or "").strip()
        self.progress_callback = progress_callback
        self.should_continue = should_continue
        self.active_turn_recheck_seconds = max(0.05, float(active_turn_recheck_seconds))
        self._state: dict[str, Any] = {}
        self._run_lock = asyncio.Lock()
        self._model_catalog_loaded = False
        self._model_catalog: list[dict[str, Any]] | None = None

    def _empty_state(self) -> dict[str, Any]:
        return {"schema": JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA, "papers": {}, "history": [], "updated_at": now_iso()}

    def _load_state(self) -> dict[str, Any]:
        state = read_json(self.journal_path, self._empty_state())
        if not isinstance(state, dict) or state.get("schema") not in {None, JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA}:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_SDK_JOURNAL_INVALID", "journal-author SDK journal is invalid")
        state["schema"] = JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA
        if not isinstance(state.get("papers", {}), dict):
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_SDK_JOURNAL_INVALID", "journal-author SDK papers must be an object")
        if not isinstance(state.get("history", []), list):
            state["history"] = []
        return state

    def _save(self) -> None:
        history = self._state.setdefault("history", [])
        if len(history) > 500:
            del history[:-500]
        self._state["updated_at"] = now_iso()
        write_json_atomic(self.journal_path, self._state)

    def _event(self, event: str, paper_id: str, **fields: Any) -> None:
        self._state.setdefault("history", []).append({"at": now_iso(), "event": event, "paper_id": paper_id, **fields})
        self._save()

    async def _notify(self, event: str, paper_id: str) -> None:
        if self.progress_callback is None:
            return
        response = self.progress_callback(event, paper_id, copy.deepcopy(self._state["papers"][paper_id]))
        if inspect.isawaitable(response):
            await response

    async def _continue(self, paper_id: str) -> bool:
        if self.should_continue is None:
            return True
        response = self.should_continue(paper_id, copy.deepcopy(self._state["papers"][paper_id]))
        if inspect.isawaitable(response):
            response = await response
        return bool(response)

    @staticmethod
    def _workflow(plan: JournalAuthorPaperPlan) -> dict[str, Any]:
        return normalize_journal_author_workflow_snapshot(plan.workflow_snapshot or default_journal_author_workflow_snapshot())

    def _definition(self, plan: JournalAuthorPaperPlan, position: int) -> dict[str, Any]:
        return journal_author_workflow_step(self._workflow(plan), position)

    @staticmethod
    def _run_until(plan: JournalAuthorPaperPlan) -> int:
        workflow = JournalAuthorSDKDispatcher._workflow(plan)
        return int(plan.run_until_step or workflow["step_count"])

    @staticmethod
    def _thread_name(paper_id: str) -> str:
        return f"upo-journal-author-{hashlib.sha256(paper_id.encode('utf-8')).hexdigest()[:24]}"

    def _thread_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"cwd": str(self.root), "approval_mode": ApprovalMode.deny_all, "sandbox": Sandbox.workspace_write}
        if self.model:
            kwargs["model"] = self.model
        return kwargs

    def _register_plans(self, plans: list[JournalAuthorPaperPlan]) -> None:
        papers = self._state.setdefault("papers", {})
        seen: set[str] = set()
        for plan in sorted(plans, key=lambda item: item.paper_id.casefold()):
            plan.validate()
            folded = plan.paper_id.casefold()
            if folded in seen:
                raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_PAPER_DUPLICATE", "paper plans must be unique")
            seen.add(folded)
            workflow = self._workflow(plan)
            existing = papers.get(plan.paper_id)
            existing = existing if isinstance(existing, dict) else {}
            if str(existing.get("request_id") or "") == plan.request_id:
                existing.update(
                    {
                        "start_step": int(plan.start_step),
                        "run_until_step": self._run_until(plan),
                        "run_mode": str(plan.run_mode),
                        "reuse_valid_artifacts": bool(plan.reuse_valid_artifacts),
                        "updated_at": now_iso(),
                    }
                )
                continue
            archived = list(existing.get("archived_requests", [])) if isinstance(existing.get("archived_requests"), list) else []
            if existing.get("request_id"):
                archived.append(
                    {
                        "request_id": str(existing.get("request_id") or ""),
                        "status": str(existing.get("status") or ""),
                        "completed_step": int(existing.get("completed_step") or 0),
                        "steps": copy.deepcopy(existing.get("steps") or {}),
                        "updated_at": str(existing.get("updated_at") or ""),
                    }
                )
            papers[plan.paper_id] = {
                "paper_id": plan.paper_id,
                "request_id": plan.request_id,
                "status": "queued",
                "completed_step": int(plan.start_step) - 1,
                "active_step": 0,
                "start_step": int(plan.start_step),
                "run_until_step": self._run_until(plan),
                "run_mode": str(plan.run_mode),
                "legacy_step_from": 2,
                "legacy_step_until": 3,
                "reuse_valid_artifacts": bool(plan.reuse_valid_artifacts),
                "workflow_snapshot": workflow,
                "workflow_version_id": str(workflow.get("version_id") or ""),
                "thread_id": str(existing.get("thread_id") or ""),
                "thread_name": str(existing.get("thread_name") or ""),
                "steps": {},
                "archived_requests": archived[-20:],
                "updated_at": now_iso(),
            }
        owners: dict[str, str] = {}
        for paper_id, record in papers.items():
            if not isinstance(record, dict):
                continue
            thread_id = str(record.get("thread_id") or "")
            if thread_id and thread_id in owners and owners[thread_id] != paper_id:
                raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_THREAD_OWNER_DUPLICATE", "one fixed Codex thread cannot belong to two papers")
            if thread_id:
                owners[thread_id] = str(paper_id)
        self._save()

    async def _find_named_thread(self, codex: Any, name: str) -> str:
        method = getattr(codex, "thread_list", None)
        if not callable(method):
            return ""
        cursor: str | None = None
        matches: set[str] = set()
        for _ in range(100):
            page = await method(archived=False, cursor=cursor, limit=100, search_term=name, source_kinds=list(ThreadSourceKind), use_state_db_only=False)
            for item in list(getattr(page, "data", []) or []):
                if str(getattr(item, "name", "") or "") == name:
                    thread_id = str(getattr(item, "id", "") or "")
                    if thread_id:
                        matches.add(thread_id)
            cursor = str(getattr(page, "next_cursor", "") or "") or None
            if cursor is None:
                break
        if len(matches) > 1:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_OWNED_THREAD_AMBIGUOUS", "multiple SDK threads have the deterministic paper name")
        return next(iter(matches), "")

    async def _ensure_thread(self, codex: Any, paper_id: str) -> Any:
        record = self._state["papers"][paper_id]
        thread_id = str(record.get("thread_id") or "")
        if thread_id:
            return await codex.thread_resume(thread_id, **self._thread_kwargs())
        name = self._thread_name(paper_id)
        if record.get("status") == "creating_thread":
            recovered = await self._find_named_thread(codex, name)
            if not recovered:
                raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_THREAD_CREATION_UNCERTAIN", "cannot prove whether the fixed journal-author thread was created")
            record.update({"thread_id": recovered, "thread_name": name, "status": "queued", "updated_at": now_iso()})
            self._event("thread_recovered", paper_id, thread_id=recovered)
            await self._notify("thread_recovered", paper_id)
            return await codex.thread_resume(recovered, **self._thread_kwargs())
        record.update({"status": "creating_thread", "thread_name": name, "updated_at": now_iso()})
        self._save()
        await self._notify("thread_creating", paper_id)
        thread = await codex.thread_start(**self._thread_kwargs())
        await thread.set_name(name)
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_THREAD_ID_MISSING", "Codex returned no thread id")
        record.update({"thread_id": thread_id, "status": "queued", "updated_at": now_iso()})
        self._event("thread_created", paper_id, thread_id=thread_id)
        await self._notify("thread_created", paper_id)
        return thread

    def _execution_profile(self, plan: JournalAuthorPaperPlan, position: int) -> dict[str, Any]:
        workflow = self._workflow(plan)
        step = self._definition(plan, position)
        return resolve_execution_profile(workflow.get("default_execution_profile"), step.get("execution_profile"), fallback_model=self.model)

    async def _available_models(self, codex: Any) -> list[dict[str, Any]] | None:
        if self._model_catalog_loaded:
            return self._model_catalog
        self._model_catalog_loaded = True
        method = getattr(codex, "models", None)
        if not callable(method):
            self._model_catalog = None
            return None
        try:
            self._model_catalog = await read_sdk_models(codex)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_MODEL_CATALOG_UNAVAILABLE", f"无法读取当前 Codex 账号的模型列表：{type(exc).__name__}") from exc
        if not self._model_catalog:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_MODEL_CATALOG_EMPTY", "当前 Codex 账号没有返回可用模型；未发送任务。")
        return self._model_catalog

    async def _validate_profile(self, codex: Any, profile: Mapping[str, Any]) -> None:
        problem = execution_profile_catalog_error(profile, await self._available_models(codex))
        if problem is not None:
            suffix, message = problem
            raise JournalAuthorSDKDispatchError(f"JOURNAL_AUTHOR_STEP_{suffix}", message)

    @staticmethod
    def _turn_kwargs(profile: Mapping[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": str(profile.get("model") or "")}
        effort = str(profile.get("reasoning_effort") or "").strip().casefold()
        if effort:
            kwargs["effort"] = ReasoningEffort(effort)
        return kwargs

    def _prompt_fingerprint(self, plan: JournalAuthorPaperPlan, position: int) -> dict[str, Any]:
        try:
            return journal_author_workflow_prompt_fingerprint(self.root, self._workflow(plan), position)
        except JournalAuthorWorkflowError as exc:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_WORKFLOW_PROMPT_INVALID", str(exc)) from exc

    def _build_prompt(self, plan: JournalAuthorPaperPlan, position: int) -> str:
        definition = self._definition(plan, position)
        if definition["kind"] == "builtin":
            source = str(definition.get("source_step") or "")
            if source == "2-3":
                return build_journal_author_combined_prompt(self.root, plan.paper_id)
            return build_journal_author_step_prompt(self.root, plan.paper_id, int(source))
        self._prompt_fingerprint(plan, position)
        try:
            body = Path(str(definition["prompt_path"])).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_WORKFLOW_PROMPT_UNREADABLE", f"cannot read custom prompt: {type(exc).__name__}") from exc
        body = body.replace("<paper_id>", plan.paper_id)
        return (
            f"继续同一篇论文的固定选刊作者会话，执行用户配置的流程 Step {position}。\n\n"
            f"PAPER_ID={plan.paper_id}\n步骤名称：{definition['name']}\n\n"
            "必须在既有论文目录中继续工作，不得另建论文；不得编造期刊、作者、单位、基金或声明；"
            "完成后 journal.md 与 package 必须继续满足最终产物契约。\n\n"
            f"以下是用户保存的自定义提示词：\n\n{body.rstrip()}\n"
        )

    async def _block(self, paper_id: str, code: str, message: str, *, position: int, contract: JournalAuthorContractResult | None = None) -> None:
        record = self._state["papers"][paper_id]
        step = record.setdefault("steps", {}).setdefault(str(position), {})
        step.update({"state": "blocked", "blocker": code, "blocker_message": message, "updated_at": now_iso()})
        if contract is not None:
            step["contract"] = contract.as_dict()
        record.update({"status": "blocked", "active_step": position, "blocker": code, "blocker_message": message, "updated_at": now_iso()})
        self._event("paper_blocked", paper_id, step=position, blocker=code)
        await self._notify("paper_blocked", paper_id)

    async def _needs_user(self, paper_id: str, code: str, message: str, *, position: int, contract: JournalAuthorContractResult) -> None:
        record = self._state["papers"][paper_id]
        step = record.setdefault("steps", {}).setdefault(str(position), {})
        step.update({"state": "needs_user", "contract": contract.as_dict(), "blocker": code, "blocker_message": message, "updated_at": now_iso()})
        record.update(
            {
                "status": "needs_user",
                "completed_step": max(int(record.get("completed_step") or 0), position),
                "active_step": position,
                "legacy_step_done": contract.step_done,
                "package_status": contract.package_status,
                "journal_decision": contract.journal_decision,
                "blocker": code,
                "blocker_message": message,
                "updated_at": now_iso(),
            }
        )
        self._event("paper_needs_user", paper_id, step=position, blocker=code)
        await self._notify("paper_needs_user", paper_id)

    def _contract_after_step(self, plan: JournalAuthorPaperPlan, position: int) -> JournalAuthorContractResult:
        definition = self._definition(plan, position)
        source = str(definition.get("source_step") or "")
        if source == "2":
            return validate_journal_selection(self.root, plan.paper_id)
        finalized = finalize_journal_submission_policy(
            self.root,
            plan.paper_id,
            compile_tex=True,
        )
        if not finalized.ok:
            raise ValueError(
                "投稿包程序化收尾失败："
                + "；".join(finalized.issues[:4])
            )
        sync_package_metadata_fingerprint(self.root, plan.paper_id)
        return validate_journal_author_output(self.root, plan.paper_id)

    async def _complete_step(self, plan: JournalAuthorPaperPlan, position: int, turn: Any | None, *, cached: bool = False) -> bool:
        try:
            contract = self._contract_after_step(plan, position)
        except (OSError, ValueError) as exc:
            await self._block(plan.paper_id, "JOURNAL_AUTHOR_OUTPUT_CONTRACT_INVALID", str(exc), position=position)
            return False
        record = self._state["papers"][plan.paper_id]
        step = record.setdefault("steps", {}).setdefault(str(position), {})
        step["contract"] = contract.as_dict()
        step["artifacts"] = [dict(item) for item in contract.artifacts]
        usage = _usage_snapshot(turn) if turn is not None else None
        if usage:
            step["usage"] = usage
            consumed = (usage.get("last") or {}).get("total_tokens")
            if isinstance(consumed, int):
                hard_limit = int((step.get("execution_profile") or {}).get("hard_token_limit") or 0)
                step["token_budget"] = {"hard_limit_tokens": hard_limit, "consumed_step_tokens": consumed}
                if hard_limit and consumed > hard_limit:
                    await self._block(
                        plan.paper_id,
                        "JOURNAL_AUTHOR_STEP_TOKEN_BUDGET_EXCEEDED",
                        f"流程 Step {position} 消耗 {consumed} Token，超过硬上限 {hard_limit}",
                        position=position,
                        contract=contract,
                    )
                    return False
        if not contract.ok:
            message = "；".join(contract.issues[:4]) or "选刊作者产物未通过严格验收"
            if contract.status == "needs_user":
                await self._needs_user(plan.paper_id, "JOURNAL_SELECTION_NEEDS_USER_CONFIRMATION", message, position=position, contract=contract)
            else:
                await self._block(plan.paper_id, "JOURNAL_AUTHOR_OUTPUT_CONTRACT_INVALID", message, position=position, contract=contract)
            return False

        definition = self._definition(plan, position)
        if str(definition.get("source_step") or "") == "2":
            mark_package_selection_stale(self.root, plan.paper_id)
        step.update({"state": "skipped_cached" if cached else "complete", "completed_at": now_iso(), "updated_at": now_iso()})
        run_until = self._run_until(plan)
        terminal = position >= run_until
        status = "queued"
        if terminal:
            if str(plan.run_mode) == "selection":
                status = "selection_complete"
            elif contract.package_status == "NEEDS_MINOR_FIX":
                status = "needs_user"
            else:
                status = "complete"
        record.update(
            {
                "status": status,
                "completed_step": max(int(record.get("completed_step") or 0), position),
                "active_step": position,
                "legacy_step_done": contract.step_done,
                "package_status": contract.package_status,
                "journal_decision": contract.journal_decision,
                "updated_at": now_iso(),
            }
        )
        record.pop("blocker", None)
        record.pop("blocker_message", None)
        self._event("step_completed", plan.paper_id, step=position, terminal=terminal, status=status, cached=cached)
        await self._notify("paper_completed" if terminal else "step_completed", plan.paper_id)
        return True

    async def _recover_active(self, codex: Any, plan: JournalAuthorPaperPlan) -> bool:
        record = self._state["papers"][plan.paper_id]
        status = str(record.get("status") or "")
        recoverable_empty_turn = (
            status == "blocked"
            and str(record.get("blocker") or "") == "JOURNAL_AUTHOR_EMPTY_COMPLETED_TURN"
        )
        if status not in {"starting", "running", "waiting_event", "recovery_pending"} and not recoverable_empty_turn:
            return False
        position = int(record.get("active_step") or max(int(record.get("completed_step") or 0) + 1, int(plan.start_step)))
        thread_id = str(record.get("thread_id") or "")
        step = record.setdefault("steps", {}).setdefault(str(position), {})
        turn_id = str(step.get("turn_id") or "")
        if not thread_id:
            await self._block(plan.paper_id, "JOURNAL_AUTHOR_RECOVERY_THREAD_MISSING", "未决任务没有固定会话 ID", position=position)
            return True
        thread = await codex.thread_resume(thread_id, **self._thread_kwargs())
        while True:
            history = await thread.read(include_turns=True)
            turns = _thread_turns(history)
            if not turn_id:
                baseline = {str(value) for value in step.get("baseline_turn_ids", []) if str(value)}
                candidates = [turn for turn in turns if str(getattr(turn, "id", "") or "") not in baseline]
                if len(candidates) != 1:
                    await self._block(plan.paper_id, "JOURNAL_AUTHOR_TURN_ID_UNCERTAIN", "无法唯一确认上次是否已发送任务；为避免重复执行已停止", position=position)
                    return True
                turn_id = str(getattr(candidates[0], "id", "") or "")
                step["turn_id"] = turn_id
                self._save()
            turn = next((item for item in turns if str(getattr(item, "id", "") or "") == turn_id), None)
            if turn is None:
                await self._block(plan.paper_id, "JOURNAL_AUTHOR_RECOVERY_TURN_MISSING", "固定会话中找不到未决 Turn；为避免重复执行已停止", position=position)
                return True
            terminal = _status_value(getattr(turn, "status", ""))
            if terminal == "completed":
                if not _turn_has_agent_activity(turn):
                    await self._block(
                        plan.paper_id,
                        "JOURNAL_AUTHOR_EMPTY_COMPLETED_TURN",
                        "SDK Turn 已结束但固定会话中仍没有 AI 回复；请检查账号/网络后重试",
                        position=position,
                    )
                    return True
                step["turn_activity_source"] = "fixed_thread_history_recovery"
                await self._complete_step(plan, position, turn)
                return True
            if terminal in {"failed", "cancelled"}:
                await self._block(plan.paper_id, "JOURNAL_AUTHOR_TURN_NOT_COMPLETED", f"SDK Turn 终态为 {terminal}", position=position)
                return True
            record.update(
                {
                    "status": "waiting_event",
                    "active_step": position,
                    "blocker": "JOURNAL_AUTHOR_EXISTING_TURN_RUNNING",
                    "blocker_message": f"正在内部 SDK 固定会话等待流程 Step {position} 的已有 Turn 结束",
                    "updated_at": now_iso(),
                }
            )
            self._save()
            await self._notify("waiting_existing_turn", plan.paper_id)
            await asyncio.sleep(self.active_turn_recheck_seconds)

    async def _run_turn(self, codex: Any, plan: JournalAuthorPaperPlan, position: int) -> bool:
        record = self._state["papers"][plan.paper_id]
        definition = self._definition(plan, position)
        prompt_fingerprint = self._prompt_fingerprint(plan, position)
        profile = self._execution_profile(plan, position)
        step = record.setdefault("steps", {}).setdefault(str(position), {})
        persisted = step.get("execution_profile")
        if isinstance(persisted, Mapping) and str(persisted.get("fingerprint") or "") and str(persisted.get("fingerprint")) != str(profile.get("fingerprint") or ""):
            await self._block(plan.paper_id, "JOURNAL_AUTHOR_STEP_EXECUTION_PROFILE_MISMATCH", "本任务已记录的模型配置与冻结快照不一致", position=position)
            return False
        try:
            await self._validate_profile(codex, profile)
            thread = await self._ensure_thread(codex, plan.paper_id)
            history = await thread.read(include_turns=True)
            baseline_turn_ids = [str(getattr(turn, "id", "") or "") for turn in _thread_turns(history)]
            prompt = self._build_prompt(plan, position)
            scheduler = validate_journal_author_prompt(self.root, "scheduler")
            step.update(
                {
                    "state": "starting",
                    "workflow_step_id": str(definition["step_id"]),
                    "workflow_step_name": str(definition["name"]),
                    "contract_step": int(definition["contract_step"]),
                    "execution_profile": copy.deepcopy(profile),
                    "scheduler_prompt_sha256": scheduler.sha256,
                    "prompt_sha256": str(prompt_fingerprint.get("sha256") or "").casefold(),
                    "execution_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest().upper(),
                    "baseline_turn_ids": baseline_turn_ids,
                    "started_at": now_iso(),
                    "updated_at": now_iso(),
                }
            )
            record.update({"status": "starting", "active_step": position, "updated_at": now_iso()})
            self._save()
            await self._notify("step_starting", plan.paper_id)
            turn_handle = await thread.turn(prompt, cwd=str(self.root), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.workspace_write, **self._turn_kwargs(profile))
            turn_id = str(getattr(turn_handle, "id", "") or "")
            step.update({"state": "running", "turn_id": turn_id, "updated_at": now_iso()})
            record.update({"status": "running", "active_step": position, "updated_at": now_iso()})
            self._save()
            await self._notify("step_running", plan.paper_id)
            turn = await turn_handle.run()
        except asyncio.CancelledError:
            raise
        except JournalAuthorSDKDispatchError as exc:
            await self._block(plan.paper_id, exc.code, str(exc), position=position)
            return False
        except BaseException as exc:
            step.update({"state": "recovery_pending", "runtime_error_type": type(exc).__name__, "updated_at": now_iso()})
            record.update(
                {
                    "status": "waiting_event",
                    "active_step": position,
                    "blocker": "JOURNAL_AUTHOR_TURN_RESULT_UNCERTAIN",
                    "blocker_message": "SDK 连接中断；程序会从固定会话确认结果，不会重复发送",
                    "updated_at": now_iso(),
                }
            )
            self._save()
            await self._notify("turn_result_uncertain", plan.paper_id)
            return False
        terminal = _status_value(getattr(turn, "status", ""))
        if terminal != "completed":
            await self._block(plan.paper_id, "JOURNAL_AUTHOR_TURN_NOT_COMPLETED", f"SDK Turn 终态为 {terminal or 'unknown'}", position=position)
            return False
        if not _turn_has_agent_activity(turn):
            authoritative = await _completed_turn_from_thread_history(thread, turn_id)
            if authoritative is None:
                await self._block(plan.paper_id, "JOURNAL_AUTHOR_EMPTY_COMPLETED_TURN", "SDK Turn 已结束但没有 AI 回复或新产物；请检查账号/网络后重试", position=position)
                return False
            turn = authoritative
            step["turn_activity_source"] = "fixed_thread_history_after_run"
            step["updated_at"] = now_iso()
            self._save()
        return await self._complete_step(plan, position, turn)

    async def _run_paper(self, codex: Any, plan: JournalAuthorPaperPlan, semaphore: asyncio.Semaphore) -> None:
        # One permit belongs to one paper for its entire workflow. Steps within
        # that paper reuse the same fixed SDK thread and are scheduled by code.
        async with semaphore:
            record = self._state["papers"][plan.paper_id]
            recovered = await self._recover_active(codex, plan)
            if recovered and str(record.get("status") or "") not in {"queued"}:
                return
            input_contract = validate_journal_author_input(self.root, plan.paper_id)
            if not input_contract.ok:
                position = max(int(plan.start_step), int(record.get("completed_step") or 0) + 1)
                await self._block(plan.paper_id, "JOURNAL_AUTHOR_INPUT_CONTRACT_INVALID", "；".join(input_contract.issues[:4]), position=position, contract=input_contract)
                return
            if (
                plan.reuse_valid_artifacts
                and str(plan.run_mode) == "full"
                and int(plan.start_step) == 1
                and not any(item.get("kind") == "custom" for item in self._workflow(plan)["steps"])
                and validate_journal_author_output(self.root, plan.paper_id).ok
            ):
                final_position = self._run_until(plan)
                await self._complete_step(plan, final_position, None, cached=True)
                return
            next_step = max(int(plan.start_step), int(record.get("completed_step") or 0) + 1)
            while next_step <= self._run_until(plan):
                if not await self._continue(plan.paper_id):
                    record.update({"status": "paused", "active_step": next_step, "updated_at": now_iso()})
                    self._save()
                    await self._notify("paper_paused", plan.paper_id)
                    return
                definition = self._definition(plan, next_step)
                source = str(definition.get("source_step") or "")
                if source == "3":
                    prerequisite = validate_journal_selection(self.root, plan.paper_id)
                    if not prerequisite.ok:
                        await self._block(
                            plan.paper_id,
                            "JOURNAL_SELECTION_PREREQUISITE_INVALID",
                            "；".join(prerequisite.issues[:4]),
                            position=next_step,
                            contract=prerequisite,
                        )
                        return
                elif definition.get("kind") == "custom":
                    prerequisite = validate_journal_author_output(self.root, plan.paper_id)
                    if not prerequisite.ok:
                        await self._block(
                            plan.paper_id,
                            "JOURNAL_PACKAGE_PREREQUISITE_INVALID",
                            "自定义步骤启动前，投稿包未通过最终产物契约",
                            position=next_step,
                            contract=prerequisite,
                        )
                        return
                if not await self._run_turn(codex, plan, next_step):
                    return
                next_step += 1

    def _result(self) -> dict[str, Any]:
        return {"ok": True, "schema": JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA, "papers": copy.deepcopy(self._state.get("papers", {})), "updated_at": str(self._state.get("updated_at") or "")}

    async def run_until_idle(self, plans: list[JournalAuthorPaperPlan]) -> dict[str, Any]:
        path_key = os.path.normcase(os.path.abspath(self.lock_path))
        async with self._run_lock:
            with _ACTIVE_PATHS_LOCK:
                if path_key in _ACTIVE_PATHS:
                    raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_SDK_DISPATCHER_ALREADY_RUNNING", "another journal-author SDK dispatcher already owns this journal")
                _ACTIVE_PATHS.add(path_key)
            try:
                with FileLock(self.lock_path, stale_seconds=7 * 24 * 60 * 60):
                    self._state = self._load_state()
                    self._register_plans(list(plans))
                    async with self.codex_factory() as codex:
                        semaphore = asyncio.Semaphore(self.max_parallel_papers)
                        await asyncio.gather(*(self._run_paper(codex, plan, semaphore) for plan in plans))
                    return self._result()
            finally:
                with _ACTIVE_PATHS_LOCK:
                    _ACTIVE_PATHS.discard(path_key)

    @staticmethod
    def _plan_key(plan: JournalAuthorPaperPlan) -> str:
        workflow = JournalAuthorSDKDispatcher._workflow(plan)
        return f"{plan.paper_id}|{plan.request_id}|{workflow['sha256']}|{plan.start_step}|{plan.run_until_step}|{plan.run_mode}"

    async def run_dynamic(
        self,
        plan_supplier: Callable[[], list[JournalAuthorPaperPlan]],
        *,
        poll_seconds: float = 0.2,
        idle_grace_seconds: float = 0.8,
    ) -> dict[str, Any]:
        path_key = os.path.normcase(os.path.abspath(self.lock_path))
        async with self._run_lock:
            with _ACTIVE_PATHS_LOCK:
                if path_key in _ACTIVE_PATHS:
                    raise JournalAuthorSDKDispatchError("JOURNAL_AUTHOR_SDK_DISPATCHER_ALREADY_RUNNING", "another journal-author SDK dispatcher already owns this journal")
                _ACTIVE_PATHS.add(path_key)
            try:
                with FileLock(self.lock_path, stale_seconds=7 * 24 * 60 * 60):
                    self._state = self._load_state()
                    async with self.codex_factory() as codex:
                        semaphore = asyncio.Semaphore(self.max_parallel_papers)
                        tasks: dict[str, asyncio.Task[None]] = {}
                        idle_since: float | None = None
                        loop = asyncio.get_running_loop()
                        while True:
                            supplied = list(plan_supplier())
                            self._register_plans(supplied)
                            for plan in supplied:
                                key = self._plan_key(plan)
                                if key not in tasks:
                                    tasks[key] = asyncio.create_task(self._run_paper(codex, plan, semaphore))
                            for key in [key for key, task in tasks.items() if task.done()]:
                                await tasks.pop(key)
                            if tasks or supplied:
                                idle_since = None
                            else:
                                idle_since = idle_since or loop.time()
                                if loop.time() - idle_since >= max(0.05, idle_grace_seconds):
                                    break
                            await asyncio.sleep(max(0.02, poll_seconds))
                    return self._result()
            finally:
                with _ACTIVE_PATHS_LOCK:
                    _ACTIVE_PATHS.discard(path_key)


__all__ = [
    "JOURNAL_AUTHOR_SDK_JOURNAL_SCHEMA",
    "JournalAuthorPaperPlan",
    "JournalAuthorSDKDispatchError",
    "JournalAuthorSDKDispatcher",
]
