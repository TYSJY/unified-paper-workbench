from __future__ import annotations
from .sdk_model_catalog import read_sdk_models

import asyncio
import copy
import hashlib
import inspect
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort, ThreadSourceKind

from .figure_output_contract import FigureContractResult, validate_figure_step
from .figure_prompt_integrity import build_figure_step_prompt, validate_figure_prompt
from .figure_submission_repair import (
    create_figure_submission_checkpoint,
    discard_figure_submission_checkpoint,
    repair_figure_submission,
    restore_figure_submission_checkpoint,
)
from .figure_workflow import (
    FigureWorkflowError,
    default_figure_workflow_snapshot,
    figure_workflow_prompt_fingerprint,
    figure_workflow_step,
    figure_workflow_step_count,
    normalize_figure_workflow_snapshot,
)
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .step_execution import (
    execution_profile_catalog_error,
    normalize_model_catalog,
    resolve_execution_profile,
)


FIGURE_SDK_JOURNAL_SCHEMA = "unified-paper-orchestrator/figure-sdk-dispatch/v1"
_ACTIVE_PATHS: set[str] = set()
_ACTIVE_PATHS_LOCK = threading.Lock()
_ACTIVE_TURN_RECHECK_SECONDS = 3.0


class FigureSDKDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class FigureRevisionPlan:
    command_id: str
    prompt: str
    figure: str = ""

    def validate(self) -> None:
        if not self.command_id.strip():
            raise FigureSDKDispatchError("FIGURE_REVISION_ID_MISSING", "revision command id is required")
        if not self.prompt.strip():
            raise FigureSDKDispatchError("FIGURE_REVISION_PROMPT_MISSING", "revision prompt is required")


@dataclass(frozen=True, slots=True)
class FigurePaperPlan:
    paper_id: str
    request_id: str
    source_tex: str
    start_step: int = 2
    run_until_step: int = 3
    revisions: tuple[FigureRevisionPlan, ...] = ()
    workflow_snapshot: Mapping[str, Any] | None = None
    reuse_valid_artifacts: bool = True
    run_pipeline: bool = True

    def validate(self) -> None:
        paper_id = self.paper_id.strip()
        if not paper_id or Path(paper_id).name != paper_id or paper_id in {".", ".."}:
            raise FigureSDKDispatchError("FIGURE_PAPER_ID_INVALID", "paper id must be one safe directory name")
        if not self.request_id.strip():
            raise FigureSDKDispatchError("FIGURE_REQUEST_ID_MISSING", "figure request id is required")
        try:
            workflow = normalize_figure_workflow_snapshot(
                self.workflow_snapshot or default_figure_workflow_snapshot()
            )
        except FigureWorkflowError as exc:
            raise FigureSDKDispatchError("FIGURE_WORKFLOW_INVALID", str(exc)) from exc
        if self.run_pipeline:
            if not 1 <= self.start_step <= self.run_until_step <= figure_workflow_step_count(workflow):
                raise FigureSDKDispatchError(
                    "FIGURE_STEP_RANGE_INVALID",
                    "figure plan must stay within its immutable workflow snapshot",
                )
        elif not self.revisions:
            raise FigureSDKDispatchError("FIGURE_REVISION_PLAN_EMPTY", "a revision-only plan requires a revision")
        for revision in self.revisions:
            revision.validate()


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
    """Return whether a completed SDK turn contains model/tool activity.

    A transport or authentication failure can leave a terminal turn containing
    only the submitted user message.  Treating that shell as successful makes
    recovery wait forever; treating an unknown SDK shape as empty could cause a
    duplicate send, so missing ``items`` remains conservative.
    """

    items = getattr(turn, "items", None)
    if items is None and isinstance(turn, Mapping):
        items = turn.get("items")
    if items is None:
        return True
    for item in list(items or []):
        item_type = getattr(item, "type", None)
        if item_type is None and isinstance(item, Mapping):
            item_type = item.get("type")
        normalized = str(getattr(item_type, "value", item_type) or "")
        normalized = normalized.replace("_", "").replace("-", "").casefold()
        if normalized not in {"", "usermessage", "user"}:
            return True
    return False


class FigureSDKDispatcher:
    """Deterministic Step 1-4 dispatcher with one fixed SDK thread per paper."""

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
        should_continue: Callable[[str, int, Mapping[str, Any]], Any] | None = None,
        active_turn_recheck_seconds: float = _ACTIVE_TURN_RECHECK_SECONDS,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.journal_path = Path(journal_path).expanduser().resolve()
        self.lock_path = Path(lock_path).expanduser().resolve()
        self.codex_factory = codex_factory
        self.max_parallel_papers = int(max_parallel_papers)
        if not 1 <= self.max_parallel_papers <= 4:
            raise ValueError("figure parallel paper limit must be 1-4")
        self.model = str(model or "").strip()
        self.progress_callback = progress_callback
        self.should_continue = should_continue
        self.active_turn_recheck_seconds = max(0.05, float(active_turn_recheck_seconds))
        self._state: dict[str, Any] = {}
        self._run_lock = asyncio.Lock()
        self._model_catalog_loaded = False
        self._model_catalog: list[dict[str, Any]] | None = None

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": FIGURE_SDK_JOURNAL_SCHEMA,
            "papers": {},
            "history": [],
            "updated_at": now_iso(),
        }

    def _load_state(self) -> dict[str, Any]:
        state = read_json(self.journal_path, self._empty_state())
        if not isinstance(state, dict):
            raise FigureSDKDispatchError("FIGURE_SDK_JOURNAL_INVALID", "figure SDK journal must be an object")
        if state.get("schema") not in {None, FIGURE_SDK_JOURNAL_SCHEMA}:
            raise FigureSDKDispatchError("FIGURE_SDK_JOURNAL_INVALID", "figure SDK journal schema is unsupported")
        state["schema"] = FIGURE_SDK_JOURNAL_SCHEMA
        if not isinstance(state.get("papers", {}), dict):
            raise FigureSDKDispatchError("FIGURE_SDK_JOURNAL_INVALID", "figure SDK papers must be an object")
        if not isinstance(state.get("history", []), list):
            state["history"] = []
        state.setdefault("papers", {})
        return state

    def _save(self) -> None:
        history = self._state.setdefault("history", [])
        if len(history) > 500:
            del history[:-500]
        self._state["updated_at"] = now_iso()
        write_json_atomic(self.journal_path, self._state)

    def _event(self, event: str, paper_id: str, **fields: Any) -> None:
        self._state.setdefault("history", []).append(
            {"at": now_iso(), "event": event, "paper_id": paper_id, **fields}
        )
        self._save()

    async def _notify(self, event: str, paper_id: str) -> None:
        if self.progress_callback is None:
            return
        record = copy.deepcopy(self._state["papers"][paper_id])
        response = self.progress_callback(event, paper_id, record)
        if inspect.isawaitable(response):
            await response

    async def _continue(self, paper_id: str, next_step: int) -> bool:
        if self.should_continue is None:
            return True
        record = copy.deepcopy(self._state["papers"][paper_id])
        response = self.should_continue(paper_id, next_step, record)
        if inspect.isawaitable(response):
            response = await response
        return bool(response)

    def _register_plans(self, plans: list[FigurePaperPlan]) -> None:
        seen: set[str] = set()
        papers = self._state["papers"]
        for plan in sorted(plans, key=lambda item: item.paper_id.casefold()):
            plan.validate()
            workflow = normalize_figure_workflow_snapshot(
                plan.workflow_snapshot or default_figure_workflow_snapshot()
            )
            folded = plan.paper_id.casefold()
            if folded in seen:
                raise FigureSDKDispatchError("FIGURE_PAPER_DUPLICATE", "paper plans must be unique")
            seen.add(folded)
            record = papers.get(plan.paper_id)
            if not isinstance(record, dict):
                record = None
            if record is None:
                completed_step = plan.start_step - 1 if plan.run_pipeline else figure_workflow_step_count(workflow)
                record = {
                    "paper_id": plan.paper_id,
                    "request_id": plan.request_id,
                    "source_tex": plan.source_tex,
                    "start_step": plan.start_step,
                    "run_until_step": plan.run_until_step,
                    "run_pipeline": bool(plan.run_pipeline),
                    "reuse_valid_artifacts": bool(plan.reuse_valid_artifacts),
                    "workflow_snapshot": workflow,
                    "workflow_version_id": str(workflow.get("version_id") or ""),
                    "completed_step": completed_step,
                    "status": "queued",
                    "steps": {},
                    "revisions": {},
                    "updated_at": now_iso(),
                }
                if self.model:
                    record["model"] = self.model
                papers[plan.paper_id] = record
            elif str(record.get("request_id") or "") != plan.request_id:
                reusable_steps = record.get("reusable_steps", {})
                if not isinstance(reusable_steps, dict):
                    reusable_steps = {}
                for raw_step, raw_cache in record.get("steps", {}).items():
                    if not isinstance(raw_cache, dict):
                        continue
                    if str(raw_cache.get("state") or "") not in {"complete", "skipped_cached"}:
                        continue
                    if raw_cache.get("execution_fingerprint") and raw_cache.get("output_signature"):
                        reusable_steps[str(raw_step)] = copy.deepcopy(raw_cache)
                archived = record.setdefault("runs", [])
                if isinstance(archived, list):
                    archived.append(
                        {
                            "request_id": str(record.get("request_id") or ""),
                            "status": str(record.get("status") or ""),
                            "completed_step": int(record.get("completed_step") or 0),
                            "started_at": str(record.get("started_at") or ""),
                            "updated_at": str(record.get("updated_at") or ""),
                        }
                    )
                    if len(archived) > 20:
                        del archived[:-20]
                completed_step = plan.start_step - 1 if plan.run_pipeline else figure_workflow_step_count(workflow)
                record.update(
                    {
                        "request_id": plan.request_id,
                        "source_tex": plan.source_tex,
                        "start_step": plan.start_step,
                        "run_until_step": plan.run_until_step,
                        "run_pipeline": bool(plan.run_pipeline),
                        "reuse_valid_artifacts": bool(plan.reuse_valid_artifacts),
                        "workflow_snapshot": workflow,
                        "workflow_version_id": str(workflow.get("version_id") or ""),
                        "completed_step": completed_step,
                        "status": "queued",
                        "steps": {},
                        "revisions": {},
                        "reusable_steps": reusable_steps,
                        "blocker": "",
                        "blocker_message": "",
                        "updated_at": now_iso(),
                    }
                )
            else:
                stored_workflow = normalize_figure_workflow_snapshot(
                    record.get("workflow_snapshot") or default_figure_workflow_snapshot()
                )
                if stored_workflow["sha256"] != workflow["sha256"]:
                    raise FigureSDKDispatchError(
                        "FIGURE_WORKFLOW_CHANGED_DURING_REQUEST",
                        "one figure request cannot change its immutable workflow snapshot",
                    )
                record.update(
                    {
                        "source_tex": plan.source_tex,
                        "start_step": plan.start_step,
                        "run_until_step": plan.run_until_step,
                        "run_pipeline": bool(plan.run_pipeline),
                        "reuse_valid_artifacts": bool(plan.reuse_valid_artifacts),
                        "workflow_snapshot": stored_workflow,
                        "workflow_version_id": str(stored_workflow.get("version_id") or ""),
                        "updated_at": now_iso(),
                    }
                )
            revision_records = record.setdefault("revisions", {})
            if not isinstance(revision_records, dict):
                revision_records = {}
                record["revisions"] = revision_records
            for revision in plan.revisions:
                revision_records.setdefault(
                    revision.command_id,
                    {
                        "command_id": revision.command_id,
                        "figure": revision.figure,
                        "state": "queued",
                        "updated_at": now_iso(),
                    },
                )

        owners: dict[str, str] = {}
        for paper_id, record in papers.items():
            if not isinstance(record, dict):
                continue
            thread_id = str(record.get("thread_id") or "")
            if thread_id and thread_id in owners and owners[thread_id] != paper_id:
                raise FigureSDKDispatchError(
                    "FIGURE_THREAD_OWNER_DUPLICATE",
                    "one fixed Codex thread cannot belong to two figure papers",
                )
            if thread_id:
                owners[thread_id] = str(paper_id)
        self._save()

    @staticmethod
    def _thread_name(paper_id: str) -> str:
        suffix = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:24]
        return f"upo-figures-{suffix}"

    def _thread_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.root),
            "approval_mode": ApprovalMode.deny_all,
            "sandbox": Sandbox.workspace_write,
        }
        if self.model:
            kwargs["model"] = self.model
        return kwargs

    def _step_execution_profile(
        self,
        plan: FigurePaperPlan,
        step: int,
        *,
        revision_id: str = "",
    ) -> dict[str, Any]:
        workflow = self._workflow(plan)
        record = self._state.get("papers", {}).get(plan.paper_id, {})
        fallback_model = (
            str(record.get("model") or self.model or "").strip()
            if isinstance(record, Mapping)
            else self.model
        )
        step_profile: Any = {}
        if not revision_id:
            step_profile = self._definition(plan, step).get("execution_profile")
        return resolve_execution_profile(
            workflow.get("default_execution_profile"),
            step_profile,
            fallback_model=fallback_model,
        )

    async def _available_model_catalog(self, codex: Any) -> list[dict[str, Any]] | None:
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
            raise FigureSDKDispatchError(
                "FIGURE_MODEL_CATALOG_UNAVAILABLE",
                f"无法读取当前 Codex 账号的模型列表：{type(exc).__name__}",
            ) from exc
        if not self._model_catalog:
            raise FigureSDKDispatchError(
                "FIGURE_MODEL_CATALOG_EMPTY",
                "当前 Codex 账号没有返回可用模型；未发送本 Step。",
            )
        return self._model_catalog

    async def _validate_step_execution_profile(
        self,
        codex: Any,
        profile: Mapping[str, Any],
    ) -> None:
        problem = execution_profile_catalog_error(
            profile,
            await self._available_model_catalog(codex),
        )
        if problem is None:
            return
        suffix, message = problem
        raise FigureSDKDispatchError(f"FIGURE_STEP_{suffix}", message)

    @staticmethod
    def _turn_execution_kwargs(profile: Mapping[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": str(profile.get("model") or "")}
        effort = str(profile.get("reasoning_effort") or "").strip().casefold()
        if effort:
            kwargs["effort"] = ReasoningEffort(effort)
        return kwargs

    async def _find_named_thread(self, codex: Any, name: str) -> str:
        cursor: str | None = None
        matches: set[str] = set()
        for _ in range(100):
            page = await codex.thread_list(
                archived=False,
                cursor=cursor,
                limit=100,
                search_term=name,
                source_kinds=list(ThreadSourceKind),
                use_state_db_only=False,
            )
            for item in list(getattr(page, "data", []) or []):
                if str(getattr(item, "name", "") or "") == name:
                    thread_id = str(getattr(item, "id", "") or "")
                    if thread_id:
                        matches.add(thread_id)
            cursor = str(getattr(page, "next_cursor", "") or "") or None
            if cursor is None:
                break
        if len(matches) > 1:
            raise FigureSDKDispatchError(
                "FIGURE_OWNED_THREAD_AMBIGUOUS",
                "multiple SDK threads have the deterministic paper name",
            )
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
                raise FigureSDKDispatchError(
                    "FIGURE_THREAD_CREATION_UNCERTAIN",
                    "cannot prove whether the fixed figure thread was created",
                )
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
            raise FigureSDKDispatchError("FIGURE_THREAD_ID_MISSING", "Codex returned no thread id")
        record.update({"thread_id": thread_id, "status": "queued", "updated_at": now_iso()})
        self._event("thread_created", paper_id, thread_id=thread_id)
        await self._notify("thread_created", paper_id)
        return thread

    @staticmethod
    def _artifact_index(result: FigureContractResult) -> dict[str, dict[str, Any]]:
        return {str(item.get("path") or ""): dict(item) for item in result.artifacts if item.get("path")}

    @staticmethod
    def _artifact_signature(artifacts: Any) -> str:
        stable: list[dict[str, Any]] = []
        for item in artifacts or []:
            if not isinstance(item, Mapping) or not str(item.get("path") or ""):
                continue
            stable.append(
                {
                    "path": str(item.get("path") or "").replace("\\", "/"),
                    "size": int(item.get("size") or 0),
                    "sha256": str(item.get("sha256") or "").upper(),
                }
            )
        payload = json.dumps(
            sorted(stable, key=lambda item: item["path"].casefold()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest().upper()

    @staticmethod
    def _workflow(plan: FigurePaperPlan) -> dict[str, Any]:
        return normalize_figure_workflow_snapshot(
            plan.workflow_snapshot or default_figure_workflow_snapshot()
        )

    def _definition(self, plan: FigurePaperPlan, position: int) -> dict[str, Any]:
        return figure_workflow_step(self._workflow(plan), position)

    def _contract_step(self, plan: FigurePaperPlan, position: int, *, revision_id: str = "") -> int:
        if revision_id:
            return 4
        return int(self._definition(plan, position)["contract_step"])

    def _prompt_fingerprint(
        self,
        plan: FigurePaperPlan,
        position: int,
        *,
        revision_id: str = "",
    ) -> dict[str, Any]:
        if revision_id:
            value = validate_figure_prompt(self.root, 4)
            return {
                "kind": "builtin",
                "path": str(value.path.resolve()),
                "sha256": value.sha256.casefold(),
                "size": value.size,
            }
        try:
            return figure_workflow_prompt_fingerprint(self.root, self._workflow(plan), position)
        except FigureWorkflowError as exc:
            raise FigureSDKDispatchError("FIGURE_WORKFLOW_PROMPT_INVALID", str(exc)) from exc

    def _build_workflow_prompt(self, plan: FigurePaperPlan, position: int) -> str:
        definition = self._definition(plan, position)
        from .writing_prompt_schemes import is_undergraduate
        if is_undergraduate(self._workflow(plan)) and definition["kind"] == "builtin":
            from .undergraduate_figures import build_undergraduate_figure_prompt
            self._prompt_fingerprint(plan, position)
            return build_undergraduate_figure_prompt(self._workflow(plan), position, plan.paper_id, plan.source_tex, self.root)
        if definition["kind"] == "builtin":
            start_definition = self._definition(plan, plan.start_step)
            start_source_step = int(start_definition.get("source_step") or 2)
            return build_figure_step_prompt(
                self.root,
                plan.paper_id,
                plan.source_tex,
                int(definition["source_step"]),
                start_step=start_source_step,
            )
        self._prompt_fingerprint(plan, position)
        path = Path(str(definition["prompt_path"]))
        try:
            custom_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FigureSDKDispatchError(
                "FIGURE_WORKFLOW_PROMPT_UNREADABLE",
                f"cannot read custom figure workflow prompt: {type(exc).__name__}",
            ) from exc
        source = str(plan.source_tex or f"papers/{plan.paper_id}/input.tex").replace("\\", "/")
        custom_text = custom_text.replace("<paper_id>", plan.paper_id).replace("<source_tex>", source)
        from .undergraduate_word import word_prompt
        word_instructions = word_prompt(self.root, plan.paper_id) if is_undergraduate(self._workflow(plan)) else ""
        return (
            f"继续同一篇论文的固定画图会话，执行用户配置的画图流程 Step {position}。\n\n"
            f"PAPER_ID={plan.paper_id}\n"
            f"SOURCE_TEX={source}\n"
            f"步骤名称：{definition['name']}\n\n"
            "必须在既有论文目录中继续工作，不得另建论文；完成后最终提交包仍须通过 Step 3 产物契约。\n\n"
            "以下是用户为该步骤保存的原始提示词：\n\n"
            f"{custom_text.rstrip()}\n" + word_instructions
        )

    def _baseline(self, paper_id: str, contract_step: int) -> dict[str, dict[str, Any]]:
        return self._artifact_index(validate_figure_step(self.root, paper_id, contract_step))

    def _source_signature(self, plan: FigurePaperPlan) -> dict[str, Any] | None:
        path = (self.root / plan.source_tex).resolve()
        try:
            stat = path.stat()
            payload = path.read_bytes()
        except OSError:
            return None
        try:
            label = path.relative_to(self.root).as_posix()
        except ValueError:
            label = str(path)
        return {
            "path": label,
            "size": int(stat.st_size),
            "sha256": hashlib.sha256(payload).hexdigest().upper(),
        }

    def _input_signature(self, plan: FigurePaperPlan, position: int) -> str:
        inputs: list[dict[str, Any]] = []
        source = self._source_signature(plan)
        if source is not None:
            inputs.append(source)
        definition = self._definition(plan, position)
        prerequisite_contract = 0
        if position > 1:
            prerequisite_contract = int(self._definition(plan, position - 1)["contract_step"])
        elif int(definition.get("source_step") or 0) == 3:
            prerequisite_contract = 2
        elif definition.get("kind") == "custom":
            prerequisite_contract = 3
        if prerequisite_contract:
            result = validate_figure_step(self.root, plan.paper_id, prerequisite_contract)
            inputs.extend(dict(item) for item in result.artifacts)
        return self._artifact_signature(inputs)

    def _execution_fingerprint(self, plan: FigurePaperPlan, position: int) -> tuple[str, str, str]:
        workflow = self._workflow(plan)
        definition = self._definition(plan, position)
        execution_profile = self._step_execution_profile(plan, position)
        prompt = self._prompt_fingerprint(plan, position)
        input_signature = self._input_signature(plan, position)
        payload = {
            "workflow_sha256": str(workflow.get("sha256") or ""),
            "step_id": str(definition.get("step_id") or ""),
            "contract_step": int(definition.get("contract_step") or 0),
            "prompt_sha256": str(prompt.get("sha256") or "").casefold(),
            "input_signature": input_signature,
            "execution_profile_fingerprint": str(execution_profile.get("fingerprint") or ""),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        return digest, input_signature, str(prompt.get("sha256") or "").casefold()

    async def _reuse_valid_step(self, plan: FigurePaperPlan, position: int) -> bool:
        if not plan.reuse_valid_artifacts:
            return False
        record = self._state["papers"][plan.paper_id]
        cache_root = record.get("reusable_steps", {})
        cached = cache_root.get(str(position)) if isinstance(cache_root, Mapping) else None
        if not isinstance(cached, Mapping):
            return False
        definition = self._definition(plan, position)
        if str(cached.get("workflow_step_id") or "") != str(definition.get("step_id") or ""):
            return False
        execution, input_signature, prompt_sha256 = self._execution_fingerprint(plan, position)
        if str(cached.get("execution_fingerprint") or "") != execution:
            return False
        result = validate_figure_step(
            self.root,
            plan.paper_id,
            int(definition["contract_step"]),
        )
        output_signature = self._artifact_signature(result.artifacts)
        if not result.ok or output_signature != str(cached.get("output_signature") or ""):
            return False
        step_record = record.setdefault("steps", {}).setdefault(str(position), {})
        step_record.update(
            {
                "state": "skipped_cached",
                "workflow_step_id": str(definition["step_id"]),
                "workflow_step_name": str(definition["name"]),
                "contract": result.as_dict(),
                "artifacts": [dict(item) for item in result.artifacts],
                "input_signature": input_signature,
                "prompt_sha256": prompt_sha256,
                "execution_fingerprint": execution,
                "execution_profile": self._step_execution_profile(plan, position),
                "output_signature": output_signature,
                "reused_from_request_id": str(cached.get("request_id") or ""),
                "completed_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        record["completed_step"] = max(int(record.get("completed_step") or 0), position)
        record["status"] = "queued" if position < int(record.get("run_until_step") or position) else "complete"
        record["updated_at"] = now_iso()
        self._event("step_reused", plan.paper_id, step=position)
        await self._notify("step_reused", plan.paper_id)
        return True

    @staticmethod
    def _has_fresh_artifact(
        baseline: Mapping[str, Mapping[str, Any]],
        current: FigureContractResult,
    ) -> bool:
        for path, artifact in FigureSDKDispatcher._artifact_index(current).items():
            previous = baseline.get(path)
            if previous is None:
                return True
            if artifact.get("sha256") != previous.get("sha256"):
                return True
            if artifact.get("mtime_ns") != previous.get("mtime_ns"):
                return True
        return False

    async def _block(self, paper_id: str, code: str, message: str, *, step: int | None = None) -> None:
        record = self._state["papers"][paper_id]
        record.update(
            {
                "status": "blocked",
                "blocker": code,
                "blocker_message": message,
                "updated_at": now_iso(),
            }
        )
        record.pop("active_step", None)
        if step is not None:
            step_record = record.setdefault("steps", {}).setdefault(str(step), {})
            step_record.update({"state": "blocked", "blocker": code, "blocker_message": message, "updated_at": now_iso()})
        self._event("paper_blocked", paper_id, step=step, blocker=code)
        await self._notify("paper_blocked", paper_id)

    async def _mark_paused(self, paper_id: str, next_step: int) -> None:
        record = self._state["papers"][paper_id]
        record.update(
            {
                "status": "paused",
                "paused_before_step": next_step,
                "updated_at": now_iso(),
            }
        )
        record.pop("active_step", None)
        self._event("paper_paused", paper_id, before_step=next_step)
        await self._notify("paper_paused", paper_id)

    def _discard_submission_checkpoint(self, paper_id: str, target: dict[str, Any]) -> None:
        checkpoint = target.get("submission_checkpoint")
        if not isinstance(checkpoint, Mapping):
            return
        try:
            discard_figure_submission_checkpoint(self.root, paper_id, checkpoint)
        except (OSError, ValueError):
            return
        target.pop("submission_checkpoint", None)

    def _restore_submission_checkpoint(
        self,
        paper_id: str,
        target: dict[str, Any],
        *,
        preserve_invalid: bool = False,
    ) -> bool:
        checkpoint = target.get("submission_checkpoint")
        if not isinstance(checkpoint, Mapping):
            return False
        paper_root = (self.root / "papers" / paper_id).resolve()
        existing_archives = {
            path.resolve()
            for path in paper_root.glob(".upo-failed-submit-*")
            if path.is_dir()
        }
        try:
            restored = restore_figure_submission_checkpoint(
                self.root,
                paper_id,
                checkpoint,
                preserve_failed=preserve_invalid,
            )
        except (OSError, ValueError):
            restored = False
        target.pop("submission_checkpoint", None)
        target["submission_checkpoint_restored"] = restored
        if restored and preserve_invalid:
            archives = sorted(
                (
                    path.resolve()
                    for path in paper_root.glob(".upo-failed-submit-*")
                    if path.is_dir() and path.resolve() not in existing_archives
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            if archives:
                target["preserved_invalid_submission"] = (
                    archives[0].relative_to(self.root.resolve()).as_posix()
                )
        return restored

    def _ensure_submission_checkpoint(
        self,
        plan: FigurePaperPlan,
        step: int,
        target: dict[str, Any],
    ) -> None:
        existing = target.get("submission_checkpoint")
        if isinstance(existing, Mapping):
            raw_path = str(existing.get("path") or "")
            path = (self.root / raw_path).resolve() if raw_path else None
            if path is not None and path.is_dir():
                return
        checkpoint = create_figure_submission_checkpoint(
            self.root,
            plan.paper_id,
            attempt_id=f"{plan.request_id}-step-{step}",
        )
        target["submission_checkpoint"] = checkpoint.as_dict(self.root)

    async def _complete_output(
        self,
        plan: FigurePaperPlan,
        step: int,
        turn: Any,
        baseline: Mapping[str, Mapping[str, Any]],
        *,
        revision_id: str = "",
    ) -> bool:
        self._prompt_fingerprint(plan, step, revision_id=revision_id)
        contract_step = self._contract_step(plan, step, revision_id=revision_id)
        result = validate_figure_step(self.root, plan.paper_id, contract_step)
        record = self._state["papers"][plan.paper_id]
        target = record.setdefault("revisions", {}).setdefault(revision_id, {}) if revision_id else record.setdefault("steps", {}).setdefault(str(step), {})
        if result.status == "invalid" and contract_step == 3 and not revision_id:
            repair = repair_figure_submission(self.root, plan.paper_id)
            target["programmatic_repair"] = {
                "attempted": True,
                "ok": repair.ok,
                "repaired": repair.repaired,
                "issues": [str(issue)[:1200] for issue in repair.issues[:4]],
                "model_turn_started": False,
                "updated_at": now_iso(),
            }
            if repair.ok:
                result = validate_figure_step(self.root, plan.paper_id, contract_step)
        target.update(
            {
                "turn_id": str(getattr(turn, "id", "") or target.get("turn_id") or ""),
                "contract": result.as_dict(),
                "completed_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        usage = _usage_snapshot(turn)
        if usage is not None:
            target["usage"] = usage
        if result.status == "waiting_data":
            self._discard_submission_checkpoint(plan.paper_id, target)
            target["state"] = "waiting_data"
            record.update(
                {
                    "status": "waiting_data",
                    "blocker": "FIGURE_DATA_REQUIRED",
                    "blocker_message": f"Step 3 已生成可用部分，但仍缺 {result.data_requirement_count} 项真实数据",
                    "updated_at": now_iso(),
                }
            )
            record.pop("active_step", None)
            self._event("paper_waiting_data", plan.paper_id, step=step, count=result.data_requirement_count)
            await self._notify("paper_waiting_data", plan.paper_id)
            return False
        if not result.ok:
            restored = self._restore_submission_checkpoint(
                plan.paper_id,
                target,
                preserve_invalid=True,
            )
            message = "；".join(result.issues[:4]) or "画图产物契约未通过"
            if restored:
                message = f"{message}；已恢复执行本 Step 前的提交包"
            await self._block(plan.paper_id, "FIGURE_OUTPUT_CONTRACT_INVALID", message, step=step)
            return False
        if not self._has_fresh_artifact(baseline, result):
            self._discard_submission_checkpoint(plan.paper_id, target)
            await self._block(
                plan.paper_id,
                "FIGURE_EXPECTED_ARTIFACT_STALE",
                f"Step {step} 返回完成，但验收产物没有发生变化",
                step=step,
            )
            return False
        self._discard_submission_checkpoint(plan.paper_id, target)
        target["state"] = "complete"
        target["artifacts"] = [dict(item) for item in result.artifacts]
        target["output_signature"] = self._artifact_signature(result.artifacts)
        target["request_id"] = plan.request_id
        if not revision_id:
            definition = self._definition(plan, step)
            target["workflow_step_id"] = str(definition["step_id"])
            target["workflow_step_name"] = str(definition["name"])
            target["contract_step"] = int(definition["contract_step"])
        record.pop("blocker", None)
        record.pop("blocker_message", None)
        record.pop("active_step", None)
        if revision_id:
            record["status"] = "complete"
            self._event("revision_completed", plan.paper_id, step=4, command_id=revision_id)
            await self._notify("revision_completed", plan.paper_id)
        else:
            record["completed_step"] = max(int(record.get("completed_step") or 0), step)
            record["status"] = "queued" if step < int(record.get("run_until_step") or step) else "complete"
            self._event("step_completed", plan.paper_id, step=step, turn_id=str(getattr(turn, "id", "") or ""))
            await self._notify("step_completed", plan.paper_id)
        record["updated_at"] = now_iso()
        self._save()
        return True

    async def _resolve_empty_completed_turn(
        self,
        plan: FigurePaperPlan,
        step: int,
        turn: Any,
        baseline: Mapping[str, Mapping[str, Any]],
        *,
        revision_id: str = "",
    ) -> bool | None:
        """Accept fresh artifacts or queue one safe retry for an empty turn.

        ``None`` means the caller should immediately retry the same Step in the
        same fixed paper thread.  A second empty terminal turn is blocked so a
        persistent provider/account failure cannot burn tokens or spin forever.
        """

        contract_step = self._contract_step(plan, step, revision_id=revision_id)
        self._prompt_fingerprint(plan, step, revision_id=revision_id)
        result = validate_figure_step(self.root, plan.paper_id, contract_step)
        if self._has_fresh_artifact(baseline, result):
            return await self._complete_output(
                plan,
                step,
                turn,
                baseline,
                revision_id=revision_id,
            )

        record = self._state["papers"][plan.paper_id]
        target = (
            record.setdefault("revisions", {}).setdefault(revision_id, {})
            if revision_id
            else record.setdefault("steps", {}).setdefault(str(step), {})
        )
        retry_count = int(target.get("empty_completed_retry_count") or 0)
        turn_id = str(getattr(turn, "id", "") or target.get("turn_id") or "")
        empty_turn_ids = [
            str(value)
            for value in target.get("empty_completed_turn_ids", [])
            if str(value)
        ]
        if turn_id and turn_id not in empty_turn_ids:
            empty_turn_ids.append(turn_id)
        target["empty_completed_turn_ids"] = empty_turn_ids[-4:]
        if retry_count >= 1:
            await self._block(
                plan.paper_id,
                "FIGURE_EMPTY_TURN_RETRY_EXHAUSTED",
                f"Step {step} 连续两次结束但没有 AI 回复或新产物；请检查 Codex 账号/网络后重试",
                step=step,
            )
            return False

        target.update(
            {
                "state": "retry_pending",
                "empty_completed_retry_count": retry_count + 1,
                "last_empty_completed_turn_id": turn_id,
                "updated_at": now_iso(),
            }
        )
        record.update(
            {
                "status": "queued",
                "updated_at": now_iso(),
            }
        )
        record.pop("active_step", None)
        record.pop("blocker", None)
        record.pop("blocker_message", None)
        self._event(
            "empty_completed_turn_retry_queued",
            plan.paper_id,
            step=step,
            turn_id=turn_id,
        )
        await self._notify("empty_completed_turn_retry_queued", plan.paper_id)
        return None

    async def _recover_active(self, codex: Any, plan: FigurePaperPlan) -> bool:
        record = self._state["papers"][plan.paper_id]
        candidates: list[tuple[int, dict[str, Any], str]] = []
        for raw_step, raw in record.get("steps", {}).items():
            if isinstance(raw, dict) and str(raw.get("state") or "") in {"starting", "running", "recovery_pending", "send_uncertain"}:
                candidates.append((int(raw_step), raw, ""))
        for revision_id, raw in record.get("revisions", {}).items():
            if isinstance(raw, dict) and str(raw.get("state") or "") in {"starting", "running", "recovery_pending", "send_uncertain"}:
                candidates.append((4, raw, str(revision_id)))
        if not candidates:
            return False
        if len(candidates) > 1:
            await self._block(plan.paper_id, "FIGURE_RECOVERY_AMBIGUOUS", "一个论文记录了多个未决 SDK turn")
            return True
        step, step_record, revision_id = candidates[0]
        thread_id = str(record.get("thread_id") or "")
        if not thread_id:
            await self._block(plan.paper_id, "FIGURE_RECOVERY_THREAD_MISSING", "未决 Step 没有固定会话 id", step=step)
            return True
        thread = await codex.thread_resume(thread_id, **self._thread_kwargs())
        while True:
            response = await thread.read(include_turns=True)
            turns = _thread_turns(response)
            turn_id = str(step_record.get("turn_id") or "")
            if turn_id:
                matches = [turn for turn in turns if str(getattr(turn, "id", "") or "") == turn_id]
            else:
                baseline_ids = {str(value) for value in step_record.get("baseline_turn_ids", [])}
                matches = [turn for turn in turns if str(getattr(turn, "id", "") or "") not in baseline_ids]
            if len(matches) != 1:
                await self._block(
                    plan.paper_id,
                    "FIGURE_SEND_UNCERTAIN",
                    "无法从固定会话唯一确认上次发送的 turn；不会自动重复发送",
                    step=step,
                )
                return True
            turn = matches[0]
            status = _status_value(getattr(turn, "status", ""))
            if status == "completed":
                baseline = step_record.get("output_baseline", {})
                normalized_baseline = baseline if isinstance(baseline, Mapping) else {}
                if not _turn_has_agent_activity(turn):
                    await self._resolve_empty_completed_turn(
                        plan,
                        step,
                        turn,
                        normalized_baseline,
                        revision_id=revision_id,
                    )
                else:
                    await self._complete_output(
                        plan,
                        step,
                        turn,
                        normalized_baseline,
                        revision_id=revision_id,
                    )
                return True
            if status == "in_progress":
                step_record.update({"state": "recovery_pending", "turn_id": str(getattr(turn, "id", "") or ""), "updated_at": now_iso()})
                record.update({"status": "waiting_event", "active_step": step, "updated_at": now_iso()})
                self._save()
                await self._notify("turn_recovery_wait", plan.paper_id)
                if not await self._continue(plan.paper_id, step):
                    return True
                await asyncio.sleep(self.active_turn_recheck_seconds)
                continue
            await self._block(
                plan.paper_id,
                "FIGURE_RECOVERED_TURN_NOT_COMPLETED",
                f"恢复的 turn 终态为 {status or 'unknown'}",
                step=step,
            )
            return True

    @staticmethod
    def _turn_usage_total_tokens(turn: Any) -> int | None:
        snapshot = _usage_snapshot(turn)
        if not isinstance(snapshot, Mapping):
            return None
        for group in ("last", "total"):
            values = snapshot.get(group)
            total = values.get("total_tokens") if isinstance(values, Mapping) else None
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                return total
        return None

    async def _run_turn_with_budget(
        self,
        turn_handle: Any,
        *,
        paper_id: str,
        step: int,
        revision_id: str,
        hard_limit: int,
    ) -> Any:
        target = (
            self._state["papers"][paper_id].setdefault("revisions", {}).setdefault(revision_id, {})
            if revision_id
            else self._state["papers"][paper_id].setdefault("steps", {}).setdefault(str(step), {})
        )
        stream_factory = getattr(turn_handle, "stream", None)
        if not callable(stream_factory):
            turn = await turn_handle.run()
            consumed = self._turn_usage_total_tokens(turn)
            if consumed is None:
                raise FigureSDKDispatchError(
                    "FIGURE_STEP_TOKEN_USAGE_MISSING",
                    f"Step {step} 已结束，但 SDK 没有返回本回合 Token 用量",
                )
            target["token_budget"] = {
                "hard_limit_tokens": hard_limit,
                "consumed_step_tokens": consumed,
            }
            usage = _usage_snapshot(turn)
            if usage is not None:
                target["usage"] = usage
            self._save()
            if consumed > hard_limit:
                raise FigureSDKDispatchError(
                    "FIGURE_STEP_TOKEN_BUDGET_EXCEEDED",
                    f"Step {step} 消耗 {consumed} Token，超过硬上限 {hard_limit}",
                )
            return turn

        turn_id = str(getattr(turn_handle, "id", "") or "")
        completed_turn: Any = None
        items: list[Any] = []
        usage: Any = None
        consumed: int | None = None
        interrupt_requested = False
        async for event in stream_factory():
            method = str(getattr(event, "method", "") or "")
            payload = getattr(event, "payload", None)
            event_turn_id = str(getattr(payload, "turn_id", "") or "")
            if method == "item/completed" and event_turn_id == turn_id:
                item = getattr(payload, "item", None)
                if item is not None:
                    items.append(item)
            elif method == "thread/tokenUsage/updated" and event_turn_id == turn_id:
                usage = getattr(payload, "token_usage", None)
                consumed = self._turn_usage_total_tokens(SimpleNamespace(usage=usage))
                snapshot = _usage_snapshot(SimpleNamespace(usage=usage))
                if snapshot is not None:
                    target["usage"] = snapshot
                if consumed is not None:
                    target["token_budget"] = {
                        "hard_limit_tokens": hard_limit,
                        "consumed_step_tokens": consumed,
                    }
                target["updated_at"] = now_iso()
                self._save()
                if consumed is not None and consumed > hard_limit and not interrupt_requested:
                    interrupt_requested = True
                    target["state"] = "budget_interrupting"
                    self._save()
                    try:
                        interruption = turn_handle.interrupt()
                        if inspect.isawaitable(interruption):
                            await interruption
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        target["token_budget_interrupt_error"] = type(exc).__name__
                        self._save()
            elif method == "turn/completed":
                candidate = getattr(payload, "turn", None)
                if str(getattr(candidate, "id", "") or "") == turn_id:
                    completed_turn = candidate
        if completed_turn is None:
            raise FigureSDKDispatchError(
                "FIGURE_TURN_RESULT_UNCERTAIN",
                "SDK 流没有返回 Turn 完成事件",
            )
        result = SimpleNamespace(
            id=str(getattr(completed_turn, "id", "") or turn_id),
            status=getattr(completed_turn, "status", ""),
            error=getattr(completed_turn, "error", None),
            started_at=getattr(completed_turn, "started_at", None),
            completed_at=getattr(completed_turn, "completed_at", None),
            duration_ms=getattr(completed_turn, "duration_ms", None),
            final_response=None,
            items=items,
            usage=usage,
        )
        if consumed is None:
            raise FigureSDKDispatchError(
                "FIGURE_STEP_TOKEN_USAGE_MISSING",
                f"Step {step} 已结束，但 SDK 没有返回本回合 Token 用量",
            )
        if consumed > hard_limit:
            raise FigureSDKDispatchError(
                "FIGURE_STEP_TOKEN_BUDGET_EXCEEDED",
                f"Step {step} 消耗 {consumed} Token，超过硬上限 {hard_limit}",
            )
        return result

    async def _run_turn(
        self,
        codex: Any,
        plan: FigurePaperPlan,
        step: int,
        prompt: str,
        semaphore: asyncio.Semaphore,
        *,
        revision_id: str = "",
    ) -> bool:
        prompt_fingerprint = self._prompt_fingerprint(plan, step, revision_id=revision_id)
        contract_step = self._contract_step(plan, step, revision_id=revision_id)
        record = self._state["papers"][plan.paper_id]
        target = record.setdefault("revisions", {}).setdefault(revision_id, {}) if revision_id else record.setdefault("steps", {}).setdefault(str(step), {})
        execution_profile = self._step_execution_profile(
            plan,
            step,
            revision_id=revision_id,
        )
        persisted_profile = target.get("execution_profile")
        if (
            isinstance(persisted_profile, Mapping)
            and str(persisted_profile.get("fingerprint") or "")
            and str(persisted_profile.get("fingerprint") or "")
            != str(execution_profile.get("fingerprint") or "")
        ):
            await self._block(
                plan.paper_id,
                "FIGURE_STEP_EXECUTION_PROFILE_MISMATCH",
                "本 Step 已记录的模型执行配置与论文冻结快照不一致",
                step=step,
            )
            return False
        try:
            await self._validate_step_execution_profile(codex, execution_profile)
        except FigureSDKDispatchError as exc:
            await self._block(plan.paper_id, exc.code, str(exc), step=step)
            return False
        target["execution_profile"] = copy.deepcopy(execution_profile)
        baseline = self._baseline(plan.paper_id, contract_step)
        if not revision_id:
            definition = self._definition(plan, step)
            execution_fingerprint, input_signature, prompt_sha256 = self._execution_fingerprint(plan, step)
            target.update(
                {
                    "workflow_step_id": str(definition["step_id"]),
                    "workflow_step_name": str(definition["name"]),
                    "contract_step": int(definition["contract_step"]),
                    "input_signature": input_signature,
                    "execution_fingerprint": execution_fingerprint,
                    "prompt_sha256": prompt_sha256,
                }
            )
        try:
            thread = await self._ensure_thread(codex, plan.paper_id)
            history = await thread.read(include_turns=True)
            baseline_turn_ids = [str(getattr(turn, "id", "") or "") for turn in _thread_turns(history)]
            target.update(
                {
                    "state": "starting",
                    "prompt_sha256": str(prompt_fingerprint.get("sha256") or "").casefold(),
                    "execution_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest().upper(),
                    "baseline_turn_ids": baseline_turn_ids,
                    "output_baseline": copy.deepcopy(baseline),
                    "execution_profile": copy.deepcopy(execution_profile),
                    "started_at": now_iso(),
                    "updated_at": now_iso(),
                }
            )
            if contract_step == 3 and not revision_id:
                self._ensure_submission_checkpoint(plan, step, target)
            record.update({"status": "starting", "active_step": step, "started_at": record.get("started_at") or now_iso(), "updated_at": now_iso()})
            self._save()
            await self._notify("step_starting", plan.paper_id)
            self._prompt_fingerprint(plan, step, revision_id=revision_id)
            turn_handle = await thread.turn(
                prompt,
                cwd=str(self.root),
                approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.workspace_write,
                **self._turn_execution_kwargs(execution_profile),
            )
            turn_id = str(getattr(turn_handle, "id", "") or "")
            target.update({"state": "running", "turn_id": turn_id, "updated_at": now_iso()})
            record.update({"status": "running", "active_step": step, "updated_at": now_iso()})
            self._save()
            await self._notify("step_running", plan.paper_id)
            hard_limit = int(execution_profile.get("hard_token_limit") or 0)
            if hard_limit:
                turn = await self._run_turn_with_budget(
                    turn_handle,
                    paper_id=plan.paper_id,
                    step=step,
                    revision_id=revision_id,
                    hard_limit=hard_limit,
                )
            else:
                turn = await turn_handle.run()
        except asyncio.CancelledError:
            raise
        except FigureSDKDispatchError as exc:
            await self._block(plan.paper_id, exc.code, str(exc), step=step)
            return False
        except BaseException as exc:
            target.update({"state": "recovery_pending", "runtime_error_type": type(exc).__name__, "updated_at": now_iso()})
            record.update(
                {
                    "status": "waiting_event",
                    "blocker": "FIGURE_TURN_RESULT_UNCERTAIN",
                    "blocker_message": "SDK 连接中断；下次启动会从固定会话确认结果，不会重复发送",
                    "updated_at": now_iso(),
                }
            )
            self._save()
            await self._notify("turn_result_uncertain", plan.paper_id)
            return False
        terminal = _status_value(getattr(turn, "status", ""))
        if terminal != "completed":
            await self._block(
                plan.paper_id,
                "FIGURE_TURN_NOT_COMPLETED",
                f"SDK turn 终态为 {terminal or 'unknown'}",
                step=step,
            )
            return False
        if not _turn_has_agent_activity(turn):
            resolved = await self._resolve_empty_completed_turn(
                plan,
                step,
                turn,
                baseline,
                revision_id=revision_id,
            )
            if resolved is None:
                return await self._run_turn(
                    codex,
                    plan,
                    step,
                    prompt,
                    semaphore,
                    revision_id=revision_id,
                )
            return resolved
        return await self._complete_output(plan, step, turn, baseline, revision_id=revision_id)

    async def _run_paper(self, codex: Any, plan: FigurePaperPlan, semaphore: asyncio.Semaphore) -> None:
        # A permit belongs to one paper for its whole workflow.  This keeps the
        # number of live paper sessions equal to the configured concurrency and
        # lets each fixed session advance through its Steps without AI scheduling.
        async with semaphore:
            await self._run_paper_in_slot(codex, plan, semaphore)

    async def _run_paper_in_slot(
        self,
        codex: Any,
        plan: FigurePaperPlan,
        semaphore: asyncio.Semaphore,
    ) -> None:
        record = self._state["papers"][plan.paper_id]
        from .undergraduate_word import write_context
        write_context(self.root, plan.paper_id, self._workflow(plan))
        recovered = await self._recover_active(codex, plan)
        if recovered and str(record.get("status") or "") not in {"queued", "complete"}:
            return
        if plan.run_pipeline:
            next_step = max(plan.start_step, int(record.get("completed_step") or 0) + 1)
            while next_step <= plan.run_until_step:
                if not await self._continue(plan.paper_id, next_step):
                    await self._mark_paused(plan.paper_id, next_step)
                    return
                input_tex = self.root / plan.source_tex
                if not input_tex.is_file():
                    await self._block(plan.paper_id, "FIGURE_INPUT_TEX_MISSING", f"画图输入不存在：{plan.source_tex}", step=next_step)
                    return
                if await self._reuse_valid_step(plan, next_step):
                    next_step += 1
                    continue
                definition = self._definition(plan, next_step)
                prerequisite_contract = 0
                if next_step > plan.start_step:
                    prerequisite_contract = int(self._definition(plan, next_step - 1)["contract_step"])
                elif int(definition.get("source_step") or 0) == 3:
                    prerequisite_contract = 2
                elif definition.get("kind") == "custom":
                    prerequisite_contract = 3
                if prerequisite_contract:
                    prerequisite = validate_figure_step(self.root, plan.paper_id, prerequisite_contract)
                    if not prerequisite.ok:
                        await self._block(
                            plan.paper_id,
                            "FIGURE_PREREQUISITE_CONTRACT_MISSING",
                            f"流程 Step {next_step} 启动前的产物契约 {prerequisite_contract} 未通过",
                            step=next_step,
                        )
                        return
                prompt = self._build_workflow_prompt(plan, next_step)
                if not await self._run_turn(codex, plan, next_step, prompt, semaphore):
                    return
                next_step += 1
        for revision in plan.revisions:
            revision_record = record.setdefault("revisions", {}).get(revision.command_id, {})
            if isinstance(revision_record, dict) and revision_record.get("state") == "complete":
                continue
            if not (self.root / plan.source_tex).is_file():
                await self._block(
                    plan.paper_id,
                    "FIGURE_INPUT_TEX_MISSING",
                    f"单图修改所需画图输入不存在：{plan.source_tex}",
                    step=4,
                )
                return
            if not await self._continue(plan.paper_id, 4):
                await self._mark_paused(plan.paper_id, 4)
                return
            if not await self._run_turn(
                codex,
                plan,
                4,
                revision.prompt,
                semaphore,
                revision_id=revision.command_id,
            ):
                return
        record["status"] = "complete"
        record.pop("active_step", None)
        record["updated_at"] = now_iso()
        self._save()

    async def run_until_idle(self, plans: list[FigurePaperPlan]) -> dict[str, Any]:
        normalized = list(plans)
        path_key = os.path.normcase(os.path.abspath(self.lock_path))
        async with self._run_lock:
            with _ACTIVE_PATHS_LOCK:
                if path_key in _ACTIVE_PATHS:
                    raise FigureSDKDispatchError(
                        "FIGURE_SDK_DISPATCHER_ALREADY_RUNNING",
                        "another figure SDK dispatcher already owns this journal",
                    )
                _ACTIVE_PATHS.add(path_key)
            try:
                with FileLock(self.lock_path, stale_seconds=7 * 24 * 60 * 60):
                    self._state = self._load_state()
                    self._register_plans(normalized)
                    async with self.codex_factory() as codex:
                        semaphore = asyncio.Semaphore(self.max_parallel_papers)
                        await asyncio.gather(
                            *(
                                self._run_paper(codex, plan, semaphore)
                                for plan in sorted(normalized, key=lambda item: item.paper_id.casefold())
                            )
                        )
                    papers = {
                        plan.paper_id: copy.deepcopy(self._state["papers"][plan.paper_id])
                        for plan in normalized
                    }
                    return {
                        "ok": all(str(record.get("status") or "") == "complete" for record in papers.values()),
                        "schema": FIGURE_SDK_JOURNAL_SCHEMA,
                        "papers": papers,
                    }
            finally:
                with _ACTIVE_PATHS_LOCK:
                    _ACTIVE_PATHS.discard(path_key)

    @staticmethod
    def _plan_key(plan: FigurePaperPlan) -> str:
        workflow = normalize_figure_workflow_snapshot(
            plan.workflow_snapshot or default_figure_workflow_snapshot()
        )
        payload = {
            "paper_id": plan.paper_id,
            "request_id": plan.request_id,
            "source_tex": plan.source_tex,
            "start_step": plan.start_step,
            "run_until_step": plan.run_until_step,
            "run_pipeline": bool(plan.run_pipeline),
            "workflow_sha256": workflow["sha256"],
            "revisions": [item.command_id for item in plan.revisions],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def run_dynamic(
        self,
        plan_supplier: Callable[[], Any],
        *,
        poll_seconds: float = 0.15,
        idle_grace_seconds: float = 0.75,
    ) -> dict[str, Any]:
        """Continuously fill paper slots while the current dispatcher is alive.

        The supplier is program logic, not an AI turn.  A paper queued while
        another paper is running can therefore occupy the next free slot without
        waiting for the original static batch to finish.
        """

        poll_seconds = max(0.05, float(poll_seconds))
        idle_grace_seconds = max(poll_seconds, float(idle_grace_seconds))
        path_key = os.path.normcase(os.path.abspath(self.lock_path))
        async with self._run_lock:
            with _ACTIVE_PATHS_LOCK:
                if path_key in _ACTIVE_PATHS:
                    raise FigureSDKDispatchError(
                        "FIGURE_SDK_DISPATCHER_ALREADY_RUNNING",
                        "another figure SDK dispatcher already owns this journal",
                    )
                _ACTIVE_PATHS.add(path_key)
            try:
                with FileLock(self.lock_path, stale_seconds=7 * 24 * 60 * 60):
                    self._state = self._load_state()
                    touched: set[str] = set()
                    completed_keys: set[str] = set()
                    active: dict[str, tuple[str, asyncio.Task[None]]] = {}
                    loop = asyncio.get_running_loop()
                    quiet_since: float | None = None
                    async with self.codex_factory() as codex:
                        semaphore = asyncio.Semaphore(self.max_parallel_papers)
                        while True:
                            for paper_id, (key, task) in list(active.items()):
                                if not task.done():
                                    continue
                                await task
                                active.pop(paper_id, None)
                                status = str(self._state.get("papers", {}).get(paper_id, {}).get("status") or "")
                                if status != "paused":
                                    completed_keys.add(key)

                            supplied = plan_supplier()
                            if inspect.isawaitable(supplied):
                                supplied = await supplied
                            candidates = [item for item in list(supplied or []) if isinstance(item, FigurePaperPlan)]
                            candidates.sort(key=lambda item: item.paper_id.casefold())
                            available = max(0, self.max_parallel_papers - len(active))
                            launched = False
                            seen_papers: set[str] = set()
                            for plan in candidates:
                                if available <= 0:
                                    break
                                plan.validate()
                                folded = plan.paper_id.casefold()
                                if folded in seen_papers or plan.paper_id in active:
                                    continue
                                seen_papers.add(folded)
                                key = self._plan_key(plan)
                                if key in completed_keys:
                                    continue
                                self._register_plans([plan])
                                task = asyncio.create_task(
                                    self._run_paper(codex, plan, semaphore),
                                    name=f"figure-paper-{plan.paper_id}",
                                )
                                active[plan.paper_id] = (key, task)
                                touched.add(plan.paper_id)
                                available -= 1
                                launched = True

                            if launched or active:
                                quiet_since = None
                            if active:
                                await asyncio.wait(
                                    [task for _key, task in active.values()],
                                    timeout=poll_seconds,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                continue
                            if quiet_since is None:
                                quiet_since = loop.time()
                            if loop.time() - quiet_since >= idle_grace_seconds:
                                break
                            await asyncio.sleep(poll_seconds)

                    papers = {
                        paper_id: copy.deepcopy(self._state["papers"][paper_id])
                        for paper_id in sorted(touched, key=str.casefold)
                        if paper_id in self._state.get("papers", {})
                    }
                    return {
                        "ok": all(str(record.get("status") or "") == "complete" for record in papers.values()),
                        "schema": FIGURE_SDK_JOURNAL_SCHEMA,
                        "papers": papers,
                        "dynamic": True,
                    }
            finally:
                with _ACTIVE_PATHS_LOCK:
                    _ACTIVE_PATHS.discard(path_key)


__all__ = [
    "FIGURE_SDK_JOURNAL_SCHEMA",
    "FigurePaperPlan",
    "FigureRevisionPlan",
    "FigureSDKDispatchError",
    "FigureSDKDispatcher",
]
