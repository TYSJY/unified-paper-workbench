"""Live pipeline bridge for the isolated local Codex SDK writing engine.

The bridge is deliberately small and conservative: account readiness and a
quiescent legacy Automation are checked before dispatch, the SDK receives an
application-owned ``CODEX_HOME``, and dispatcher progress is merged into the
legacy pipeline with byte-conditional atomic replaces.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from openai_codex import AsyncCodex, CodexConfig

from . import config, projection as unified_projection
from .automation import AUTOMATIONS_ROOT, parse_toml_like
from .codex_sdk_account import CODEX_SDK_HOME, CodexSDKAccountService
from .io_utils import (
    FileLock,
    now_iso,
    read_json,
    read_json_snapshot,
    unlink_file_durable_if_unchanged,
    write_bytes_atomic_if_unchanged,
    write_json_atomic_if_unchanged,
)
from .windows_subprocess import configure_openai_codex_no_window
from .codex_runtime import runtime_launch_options
from .sdk_model_catalog import catalog_provenance, read_sdk_models
from .writing_completion import CodexThreadCompletionReader, expected_step_artifacts, fingerprint_step_artifacts
from .writing_novelty_gate import validate_writing_step2_contract, writing_step2_contract_validation
from .writing_prompt_schemes import is_undergraduate
from .undergraduate_output_contract import validate_undergraduate_output
from .writing_output_contract import validate_writing_step_output, writing_retry_validator_feedback
from .writing_prompt_integrity import TUNED_WRITING_PROMPT_SHA256
from .writing_sdk_dispatch import (
    GLOBAL_CODEX_SESSION_LIMIT,
    WRITING_SDK_AGENT_MAX_DEPTH,
    WRITING_SDK_AGENT_MAX_THREADS,
    WRITING_SDK_JOURNAL_SCHEMA,
    WritingPaperPlan,
    WritingSDKDispatcher,
    existing_writing_prompt_provider,
)
from .writing_workflow import (
    default_workflow_snapshot,
    normalize_workflow_snapshot,
    workflow_contract_step,
    workflow_step_count,
)


WRITING_SDK_RUNTIME_SCHEMA = "unified-paper-orchestrator/writing-sdk-runtime/v1"
WRITING_SDK_JOURNAL_PATH = config.WRITING_SDK_JOURNAL_PATH
WRITING_SDK_LOCK_PATH = config.WRITING_SDK_LOCK_PATH
_PIPELINE_WRITE_ATTEMPTS = 3
_RECOVERABLE_FAILED_BLOCKERS = {
    "WRITING_EXPECTED_ARTIFACT_MISSING",
    "WRITING_EXPECTED_ARTIFACT_STALE",
    "WRITING_FINAL_MARKERS_INVALID",
    "WRITING_OUTPUT_SEMANTIC_INVALID",
    "WRITING_TURN_NOT_COMPLETED",
}
_RECOVERABLE_FAILED_BLOCKERS_WITH_OUTPUT = {
    "WRITING_EXPECTED_ARTIFACT_STALE",
    "WRITING_OUTPUT_SEMANTIC_INVALID",
}
_RECOVERABLE_WAITING_BLOCKERS = {"GLOBAL_CODEX_SESSION_BUDGET_EXHAUSTED"}
_DISPATCH_WAITING_STATUSES = {"waiting_capacity", "waiting_event", "waiting_prerequisite"}
_PIPELINE_ACTIVE_STATUSES = {"queued", "running"}
_SEMANTIC_REVALIDATION_STEPS = {2, 3, 4, 5, 6, 7, 8, 9, 10}
_NON_SEMANTIC_REVALIDATION_ISSUES = {
    "semantic_output_unreadable",
    "semantic_validator_not_applicable",
}
_VALIDATED_RETRY_BASELINE_SCHEMA = "unified-paper-orchestrator/writing-validated-retry-baseline/v1"
_REVALIDATION_TRANSACTION_SCHEMA = "unified-paper-orchestrator/writing-sdk-revalidation-transaction/v1"
_REVALIDATION_TRANSACTION_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_REVALIDATION_TRANSACTION_MAX_MARKER_BYTES = 96 * 1024 * 1024
_JOURNAL_USAGE_FIELDS = (
    "cached_input_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class WritingSDKRuntimeError(RuntimeError):
    """A bounded runtime error suitable for UI and adapter callers."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


@dataclass(frozen=True, slots=True)
class _PlanBinding:
    paper_key: str
    pipeline_id: str
    command_ids: tuple[str, ...]
    run_until_step: int
    workflow_snapshot: dict[str, Any]
    reserved_pipeline_prefix: str = ""


@dataclass(slots=True)
class _TemporaryActivePipelineLease:
    had_active_pipeline: bool
    active_pipeline_id: str
    expected_pipeline_sha256: str | None
    selected_pipeline_ids: set[str]
    valid: bool = True

    def observe_pipeline_id(self, value: Any) -> None:
        pipeline_id = str(value or "").strip()
        if pipeline_id:
            self.selected_pipeline_ids.add(pipeline_id)

    def observe_pipeline_read(self, digest: str | None) -> None:
        if self.valid and digest != self.expected_pipeline_sha256:
            self.valid = False

    def observe_pipeline_commit(self, digest: str) -> None:
        if self.valid:
            self.expected_pipeline_sha256 = digest


def _serialized_json_sha256(value: Any) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _step_number(value: Any) -> int:
    try:
        step = int(value)
    except (TypeError, ValueError):
        return 0
    return step if 0 <= step <= config.WRITING_MAX_WORKFLOW_STEPS else 0


def _run_until_step(value: Any, default: int = 10) -> int:
    try:
        step = int(value)
    except (TypeError, ValueError):
        return default
    return step if 1 <= step <= config.WRITING_MAX_WORKFLOW_STEPS else default


def _workflow_snapshot(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, Mapping) and value.get("steps"):
            return normalize_workflow_snapshot(value)
    return default_workflow_snapshot()


def _record_contract_step(record: Mapping[str, Any], position: int) -> int:
    return workflow_contract_step(_workflow_snapshot(record.get("workflow_snapshot")), position)


def _validate_record_output(
    root: Path,
    papers_dir: Path,
    paper_id: str,
    contract_step: int,
    *,
    is_terminal: bool,
) -> dict[str, Any]:
    if is_terminal and contract_step < 10:
        return validate_writing_step_output(
            root,
            papers_dir,
            paper_id,
            contract_step,
            is_terminal=True,
        )
    return validate_writing_step_output(root, papers_dir, paper_id, contract_step)


def _normalized_sha256(value: Any) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return ""
    return digest


def _step1_prompt_was_sent_unbound(record: Mapping[str, Any]) -> bool:
    """Return whether a proven Step 1 identity was learned after its first send."""

    identity = record.get("step1_identity_recovery")
    if not isinstance(identity, Mapping):
        return False
    paper_id = str(record.get("paper_id") or "").strip()
    return bool(
        paper_id
        and str(identity.get("source") or "") == "terminal_turn_and_semantic_artifact"
        and str(identity.get("paper_id") or "").strip() == paper_id
        and str(identity.get("thread_id") or "").strip()
        and str(identity.get("thread_id") or "").strip() == str(record.get("thread_id") or "").strip()
        and str(identity.get("turn_id") or "").strip()
        and str(identity.get("artifact") or "").strip()
    )


def _step2_semantic_repair_attempt_count(record: Mapping[str, Any]) -> int:
    count = 0
    for collection_name in ("revalidation_attempts", "recovery_attempts"):
        attempts = record.get(collection_name)
        if not isinstance(attempts, list):
            continue
        count += sum(
            1
            for attempt in attempts
            if isinstance(attempt, Mapping)
            and attempt.get("step") == 2
            and (
                attempt.get("action") == "current_validator_reopen_last_completed"
                or (
                    attempt.get("action") == "explicit_recoverable_requeue"
                    and str(attempt.get("blocker") or "") in _RECOVERABLE_FAILED_BLOCKERS_WITH_OUTPUT
                )
            )
        )
    return count


def _command_id(command: Mapping[str, Any]) -> str:
    return str(command.get("id") or "").strip()


def _journal_status(record: Mapping[str, Any]) -> str:
    """Return the semantic journal status, including legacy wait migration.

    Older journals represented a prerequisite gate as ``waiting_event``.
    Distinguish it only when the record proves that no Codex turn was sent;
    genuine recovery waits must remain ``waiting_event``.
    """

    status = str(record.get("status") or "unknown").strip().lower()
    if status != "waiting_event" or str(record.get("waiting_reason") or "").strip().lower() != "prerequisite":
        return status
    if record.get("active_step") not in {None, ""}:
        return status
    waiting_before_step = record.get("waiting_before_step")
    steps = record.get("steps")
    step_record = steps.get(str(waiting_before_step)) if isinstance(steps, Mapping) else None
    if (
        isinstance(step_record, Mapping)
        and str(step_record.get("state") or "") == "waiting_prerequisite"
        and not str(step_record.get("turn_id") or "")
    ):
        return "waiting_prerequisite"
    return status


def _journal_usage_summary(journal_papers: Any) -> dict[str, Any]:
    def breakdown(value: Any) -> dict[str, int] | None:
        if not isinstance(value, Mapping):
            return None
        result: dict[str, int] = {}
        valid = False
        for field in _JOURNAL_USAGE_FIELDS:
            raw = value.get(field)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                result[field] = raw
                valid = True
            else:
                result[field] = 0
        return result if valid else None

    observations_by_turn: dict[str, dict[str, Any]] = {}

    def observe(
        *,
        paper_key: str,
        thread_id: str,
        turn_id: Any,
        usage: Any,
        current_thread: bool,
        fallback_suffix: str,
    ) -> None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        normalized_thread_id = str(thread_id or "").strip() or f"paper:{paper_key}:{fallback_suffix}"
        usage_mapping = usage if isinstance(usage, Mapping) else {}
        observation = {
            "thread_id": normalized_thread_id,
            "last": breakdown(usage_mapping.get("last")),
            "total": breakdown(usage_mapping.get("total")),
            "model_context_window": usage_mapping.get("model_context_window"),
            "current_thread": current_thread,
        }
        existing = observations_by_turn.get(normalized_turn_id)
        if existing is None or (existing.get("total") is None and observation["total"] is not None):
            observations_by_turn[normalized_turn_id] = observation

    if isinstance(journal_papers, Mapping):
        for raw_paper_key, paper in journal_papers.items():
            if not isinstance(paper, Mapping):
                continue
            paper_key = str(raw_paper_key)
            current_thread_id = str(paper.get("thread_id") or "")
            steps = paper.get("steps")
            if isinstance(steps, Mapping):
                for step_number, step in steps.items():
                    if isinstance(step, Mapping):
                        observe(
                            paper_key=paper_key,
                            thread_id=current_thread_id,
                            turn_id=step.get("turn_id"),
                            usage=step.get("usage"),
                            current_thread=True,
                            fallback_suffix=f"step:{step_number}",
                        )
            recovery_attempts = paper.get("recovery_attempts")
            if isinstance(recovery_attempts, list):
                for index, attempt in enumerate(recovery_attempts):
                    if not isinstance(attempt, Mapping):
                        continue
                    step_record = attempt.get("step_record")
                    if isinstance(step_record, Mapping):
                        observe(
                            paper_key=paper_key,
                            thread_id=str(attempt.get("thread_id") or ""),
                            turn_id=step_record.get("turn_id"),
                            usage=step_record.get("usage"),
                            current_thread=False,
                            fallback_suffix=f"recovery:{index}",
                        )

    observations = list(observations_by_turn.values())
    journaled_thread_ids = {str(item["thread_id"]) for item in observations}
    cumulative_by_thread: dict[str, dict[str, Any]] = {}
    context_by_thread: dict[str, dict[str, Any]] = {}
    for observation in observations:
        thread_id = str(observation["thread_id"])
        total = observation.get("total")
        if isinstance(total, dict):
            existing = cumulative_by_thread.get(thread_id)
            if existing is None or total["total_tokens"] >= existing["total"]["total_tokens"]:
                cumulative_by_thread[thread_id] = observation
        last = observation.get("last")
        if observation.get("current_thread") and isinstance(last, dict):
            existing = context_by_thread.get(thread_id)
            existing_total = existing.get("total") if isinstance(existing, dict) else None
            candidate_total = total["total_tokens"] if isinstance(total, dict) else 0
            previous_total = existing_total["total_tokens"] if isinstance(existing_total, dict) else -1
            if existing is None or candidate_total >= previous_total:
                context_by_thread[thread_id] = observation

    cumulative_totals = {field: 0 for field in _JOURNAL_USAGE_FIELDS}
    for observation in cumulative_by_thread.values():
        for field in _JOURNAL_USAGE_FIELDS:
            cumulative_totals[field] += observation["total"][field]
    context_totals = {field: 0 for field in _JOURNAL_USAGE_FIELDS}
    max_context_tokens = 0
    max_model_context_window = 0
    max_context_utilization_ratio = 0.0
    for observation in context_by_thread.values():
        last = observation["last"]
        for field in _JOURNAL_USAGE_FIELDS:
            context_totals[field] += last[field]
        max_context_tokens = max(max_context_tokens, last["total_tokens"])
        context_window = observation.get("model_context_window")
        if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0:
            max_model_context_window = max(max_model_context_window, context_window)
            max_context_utilization_ratio = max(
                max_context_utilization_ratio,
                last["total_tokens"] / context_window,
            )

    def with_cache_metrics(values: Mapping[str, int]) -> dict[str, Any]:
        input_tokens = values["input_tokens"]
        cached_input_tokens = values["cached_input_tokens"]
        return {
            **dict(values),
            "uncached_input_tokens": max(input_tokens - cached_input_tokens, 0),
            "cached_input_ratio": round(cached_input_tokens / input_tokens, 6) if input_tokens else 0.0,
        }

    usage_snapshot_turn_count = sum(
        1 for item in observations if isinstance(item.get("total"), dict) or isinstance(item.get("last"), dict)
    )
    return {
        "basis": "latest_usage_total_per_journaled_thread; usage_last_is_current_context_only",
        "scope": "journaled_root_writing_threads; child_agent_usage_is_not_separately_aggregated",
        "coverage": {
            "journaled_turn_count": len(observations),
            "usage_snapshot_turn_count": usage_snapshot_turn_count,
            "missing_usage_turn_count": len(observations) - usage_snapshot_turn_count,
            "journaled_thread_count": len(journaled_thread_ids),
            "cumulative_usage_thread_count": len(cumulative_by_thread),
        },
        "cumulative_thread_usage": with_cache_metrics(cumulative_totals),
        "current_context_usage": {
            "thread_count": len(context_by_thread),
            **with_cache_metrics(context_totals),
            "max_thread_context_tokens": max_context_tokens,
            "max_model_context_window": max_model_context_window,
            "max_context_utilization_ratio": round(max_context_utilization_ratio, 6),
        },
    }


class WritingSDKRuntime:
    """Connect one live writing pipeline to ``WritingSDKDispatcher``."""

    _background_lock = threading.Lock()
    _background_thread: threading.Thread | None = None
    _background_owner: "WritingSDKRuntime | None" = None
    _background_wakeup_serial = 0

    def __init__(
        self,
        project: Mapping[str, Any] | None = None,
        *,
        account_service: Any | None = None,
        codex_home: str | Path | None = None,
        journal_path: str | Path | None = None,
        dispatch_lock_path: str | Path | None = None,
        pipeline_lock_path: str | Path | None = None,
        dispatcher_factory: Callable[..., Any] = WritingSDKDispatcher,
        codex_factory: Callable[[], Any] | None = None,
        legacy_automation_probe: Callable[[], Any] | None = None,
        projection_sync_callback: Callable[[], Any] | None = None,
        projection_dirty_path: str | Path | None = None,
        projection_lock_path: str | Path | None = None,
        revalidation_fault_injector: Callable[[str], None] | None = None,
        model: str | None = None,
    ) -> None:
        project = dict(project or config.LEGACY_PROJECTS["writing"])
        self.root = Path(project["root"]).expanduser().resolve()
        self.papers_dir = Path(project["papers_dir"]).expanduser().resolve()
        self.pipeline_path = Path(project["state_path"]).expanduser().resolve()
        self.prompt_path = Path(project["prompt_path"]).expanduser().resolve()
        self.codex_home = Path(codex_home or CODEX_SDK_HOME).expanduser().resolve()
        self.journal_path = Path(journal_path or WRITING_SDK_JOURNAL_PATH).expanduser().resolve()
        self.revalidation_transaction_path = self.journal_path.with_name(
            f"{self.journal_path.stem}.revalidation_transaction.json"
        )
        self.dispatch_lock_path = Path(dispatch_lock_path or WRITING_SDK_LOCK_PATH).expanduser().resolve()
        self.pipeline_lock_path = Path(pipeline_lock_path or config.WRITING_DISPATCH_LOCK_PATH).expanduser().resolve()
        self.account_service = account_service or CodexSDKAccountService(codex_home=self.codex_home)
        self.dispatcher_factory = dispatcher_factory
        self._injected_codex_factory = codex_factory
        self._legacy_automation_probe = legacy_automation_probe
        self._projection_sync_callback = projection_sync_callback
        self._projection_dirty_path = (
            Path(projection_dirty_path).expanduser().resolve() if projection_dirty_path else None
        )
        self._projection_lock_path = Path(projection_lock_path).expanduser().resolve() if projection_lock_path else None
        self._revalidation_fault_injector = revalidation_fault_injector
        self.model = str(config.WRITING_CODEX_MODEL if model is None else model).strip()
        if not self.model or len(self.model) > 200 or any(ord(char) < 32 for char in self.model):
            raise ValueError("writing Codex model must be a non-empty bounded identifier")
        canonical_pipeline_path = Path(config.LEGACY_PROJECTS["writing"]["state_path"]).expanduser().resolve()
        self._projection_is_canonical = self.pipeline_path == canonical_pipeline_path
        self._projection_refresh_enabled = projection_sync_callback is not None or self._projection_is_canonical
        self._automation_ids = tuple(
            str(value).strip() for value in project.get("automation_ids", []) if str(value).strip()
        )
        primary = str(project.get("primary_automation_id") or "").strip()
        if primary and primary not in self._automation_ids:
            self._automation_ids = (primary, *self._automation_ids)
        self._last_result: dict[str, Any] | None = None
        self._last_error = ""
        self._last_projection_refresh: dict[str, Any] | None = None
        self._last_active_pipeline_restore: dict[str, Any] | None = None
        # New-paper wakeups are allowed to preempt an older all-queue dispatch
        # only at a Step boundary.  The next pass runs exactly these keys, so
        # an unrelated backlog cannot consume tokens behind the new batch.
        self._background_priority_keys: set[str] = set()
        self._active_background_priority_keys: tuple[str, ...] = ()

    def _codex_factory(self) -> Any:
        if self._injected_codex_factory is not None:
            return self._injected_codex_factory()
        configure_openai_codex_no_window()
        self.codex_home.mkdir(parents=True, exist_ok=True)
        return AsyncCodex(
            CodexConfig(
                cwd=str(self.root),
                **runtime_launch_options({"CODEX_HOME": str(self.codex_home)}),
                config_overrides=(
                    "features.multi_agent=false",
                    "features.multi_agent_v2=false",
                    f"agents.max_threads={WRITING_SDK_AGENT_MAX_THREADS}",
                    f"agents.max_depth={WRITING_SDK_AGENT_MAX_DEPTH}",
                ),
                client_name="unified_paper_orchestrator_writing",
                client_title="Unified Paper Orchestrator Writing",
            )
        )

    def ensure_ready(self) -> dict[str, Any]:
        """Require a logged-in account in the isolated SDK home.

        This method is intentionally read-only with respect to the writing
        pipeline, so adapters can call it before queueing commands.
        """

        verified_status = getattr(self.account_service, "verified_status", None)
        status = (
            verified_status()
            if callable(verified_status)
            else self.account_service.status()
        )
        if not isinstance(status, dict) or not status.get("ok"):
            raise WritingSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_STATUS_UNAVAILABLE",
                "无法读取本程序的 Codex 账号状态；未改动写作队列。",
            )
        if not status.get("isolated_home"):
            raise WritingSDKRuntimeError(
                "CODEX_SDK_HOME_NOT_ISOLATED",
                "the Codex SDK account is not using the application-owned home",
            )
        if not status.get("authenticated"):
            raise WritingSDKRuntimeError(
                "CODEX_SDK_ACCOUNT_NOT_AUTHENTICATED",
                "请先在“AI执行与API”中选择 Codex 账号模式，并点击“连接 ChatGPT 账号”。",
            )
        return dict(status)

    require_ready = ensure_ready

    def list_models(self) -> dict[str, Any]:
        account = self.ensure_ready()

        async def fetch() -> list[dict[str, Any]]:
            async with self._codex_factory() as codex:
                method = getattr(codex, "models", None)
                if not callable(method):
                    raise WritingSDKRuntimeError(
                        "WRITING_MODEL_CATALOG_UNSUPPORTED",
                        "当前 Codex SDK 不支持读取模型列表，请更新程序内置 SDK。",
                    )
                return await read_sdk_models(codex)

        models = asyncio.run(fetch())
        if not models:
            raise WritingSDKRuntimeError(
                "WRITING_MODEL_CATALOG_EMPTY",
                "当前账号没有返回可用模型。",
            )
        return {
            "ok": True,
            "stage": "writing",
            "configured_model": self.model,
            "models": models,
            "catalog": catalog_provenance(self.codex_home),
            "account": account,
        }

    def _legacy_automation_status(self) -> dict[str, Any]:
        if self._legacy_automation_probe is not None:
            result = self._legacy_automation_probe()
            if isinstance(result, bool):
                return {"safe": result, "active": [] if result else [{"id": "unknown"}]}
            if isinstance(result, dict):
                return dict(result)
            raise WritingSDKRuntimeError(
                "WRITING_LEGACY_AUTOMATION_STATUS_UNAVAILABLE",
                "legacy writing Automation status probe returned an invalid value",
            )
        checked = [
            parse_toml_like(AUTOMATIONS_ROOT / automation_id / "automation.toml")
            for automation_id in self._automation_ids
        ]
        active = [
            item for item in checked if item.get("exists") and str(item.get("status") or "").strip().upper() == "ACTIVE"
        ]
        unknown = [
            item
            for item in checked
            if item.get("exists") and str(item.get("status") or "").strip().upper() not in {"ACTIVE", "PAUSED"}
        ]
        return {
            "safe": not active and not unknown,
            "active": active,
            "unknown": unknown,
            "checked": checked,
        }

    def _require_legacy_quiescent(self) -> dict[str, Any]:
        try:
            status = self._legacy_automation_status()
        except WritingSDKRuntimeError:
            raise
        except Exception as exc:
            raise WritingSDKRuntimeError(
                "WRITING_LEGACY_AUTOMATION_STATUS_UNAVAILABLE",
                f"cannot safely inspect legacy writing Automation: {type(exc).__name__}",
            ) from None
        if not status.get("safe") or status.get("active"):
            raise WritingSDKRuntimeError(
                "WRITING_LEGACY_AUTOMATION_ACTIVE",
                "旧写论文自动任务仍在运行；为避免重复派发，本地事件调度器拒绝启动。",
            )
        return status

    def _load_pipeline_snapshot(self) -> tuple[dict[str, Any], str | None]:
        pipeline, digest = read_json_snapshot(self.pipeline_path, {})
        if not isinstance(pipeline, dict):
            raise WritingSDKRuntimeError("WRITING_PIPELINE_STATE_INVALID", "writing pipeline state must be an object")
        if not isinstance(pipeline.get("sessions", {}), dict):
            raise WritingSDKRuntimeError(
                "WRITING_PIPELINE_STATE_INVALID", "writing pipeline sessions must be an object"
            )
        if not isinstance(pipeline.get("commands", []), list):
            raise WritingSDKRuntimeError("WRITING_PIPELINE_STATE_INVALID", "writing pipeline commands must be a list")
        return pipeline, digest

    def _load_pipeline(self) -> dict[str, Any]:
        pipeline, _digest = self._load_pipeline_snapshot()
        return pipeline

    def _load_journal_papers(self) -> dict[str, dict[str, Any]]:
        journal = read_json(
            self.journal_path,
            {"schema": WRITING_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []},
        )
        if not isinstance(journal, dict) or journal.get("schema") != WRITING_SDK_JOURNAL_SCHEMA:
            raise WritingSDKRuntimeError("WRITING_SDK_JOURNAL_INVALID", "writing SDK journal has an invalid schema")
        raw_papers = journal.get("papers", {})
        if not isinstance(raw_papers, dict):
            raise WritingSDKRuntimeError("WRITING_SDK_JOURNAL_INVALID", "writing SDK journal papers must be an object")
        papers: dict[str, dict[str, Any]] = {}
        for raw_key, raw_record in raw_papers.items():
            key = str(raw_key or "")
            if not key or not isinstance(raw_record, dict):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_JOURNAL_INVALID", "writing SDK journal contains an invalid paper record"
                )
            papers[key] = dict(raw_record)
        return papers

    @staticmethod
    def _pending_commands(pipeline: Mapping[str, Any], action: str) -> list[dict[str, Any]]:
        return [
            item
            for item in pipeline.get("commands", [])
            if isinstance(item, dict)
            and str(item.get("status") or "") == "pending"
            and str(item.get("action") or "") == action
        ]

    @staticmethod
    def _commands_for_key(pipeline: Mapping[str, Any], paper_key: str, pipeline_id: str) -> tuple[str, ...]:
        result: list[str] = []
        for item in pipeline.get("commands", []):
            if not isinstance(item, dict):
                continue
            command_id = _command_id(item)
            if not command_id:
                continue
            matches_key = str(item.get("sdk_paper_key") or "") == paper_key
            matches_resume = (
                str(item.get("status") or "") == "pending"
                and str(item.get("action") or "") == "resume_session"
                and str(item.get("pipeline_id") or "") == pipeline_id
            )
            if matches_key or matches_resume:
                result.append(command_id)
        return tuple(dict.fromkeys(result))

    def _build_plan_bundle(
        self,
        pipeline: dict[str, Any] | None = None,
        *,
        selected_paper_keys: set[str] | None = None,
    ) -> tuple[list[WritingPaperPlan], dict[str, _PlanBinding], dict[str, Any]]:
        if pipeline is None:
            pipeline = self._load_pipeline()
        journal = self._load_journal_papers()
        if selected_paper_keys is not None:
            journal = {key: record for key, record in journal.items() if key in selected_paper_keys}
        journal_by_pipeline: dict[str, tuple[str, dict[str, Any]]] = {}
        for key, record in journal.items():
            paper_id = str(record.get("paper_id") or "").strip()
            if not paper_id:
                continue
            folded = paper_id.casefold()
            if folded in journal_by_pipeline:
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_JOURNAL_INVALID", "one pipeline id is owned by multiple SDK journal records"
                )
            journal_by_pipeline[folded] = (key, record)

        specifications: dict[str, dict[str, Any]] = {}

        for command in self._pending_commands(pipeline, "new_session"):
            command_id = _command_id(command)
            if not command_id:
                raise WritingSDKRuntimeError(
                    "WRITING_PIPELINE_STATE_INVALID", "a pending new-session command has no id"
                )
            paper_key = str(command.get("sdk_paper_key") or f"new-session:{command_id}")
            if selected_paper_keys is not None and paper_key not in selected_paper_keys:
                continue
            record = journal.get(paper_key, {})
            params = command.get("params") if isinstance(command.get("params"), dict) else {}
            workflow = _workflow_snapshot(record.get("workflow_snapshot"), params.get("workflow_snapshot"))
            specifications[paper_key] = {
                "paper_key": paper_key,
                "paper_id": str(record.get("paper_id") or ""),
                "thread_id": str(record.get("thread_id") or ""),
                "completed_step": _step_number(record.get("completed_step")),
                "run_until_step": _run_until_step(
                    params.get("run_until_step"),
                    _run_until_step(record.get("run_until_step"), workflow_step_count(workflow)),
                ),
                "workflow_snapshot": workflow,
                "reserved_pipeline_prefix": str(
                    record.get("reserved_pipeline_prefix")
                    or params.get("reserved_pipeline_prefix")
                    or command.get("reserved_pipeline_prefix")
                    or ""
                ).strip(),
                "migration_new_thread": bool(record.get("migration_new_thread")),
                "command_ids": [command_id],
            }

        sessions = pipeline.get("sessions", {})
        for raw_id, raw_session in sessions.items():
            if not isinstance(raw_session, dict):
                raise WritingSDKRuntimeError(
                    "WRITING_PIPELINE_STATE_INVALID", "writing pipeline contains an invalid session"
                )
            if str(raw_session.get("status") or "").strip().lower() not in {"queued", "running"}:
                continue
            paper_id = str(raw_session.get("pipeline_id") or raw_id or "").strip()
            if not paper_id:
                raise WritingSDKRuntimeError(
                    "WRITING_PIPELINE_STATE_INVALID", "a queued writing session has no pipeline id"
                )
            journal_match = journal_by_pipeline.get(paper_id.casefold())
            paper_key, record = journal_match if journal_match is not None else (f"session:{paper_id}", {})
            if selected_paper_keys is not None and paper_key not in selected_paper_keys:
                continue
            session_step = _step_number(raw_session.get("step_done"))
            journal_step = _step_number(record.get("completed_step"))
            if record and session_step > journal_step:
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_PROGRESS_DIVERGED",
                    "pipeline progress is newer than its existing SDK crash journal",
                )
            completed_step = journal_step if record else session_step
            sdk_thread_id = str(record.get("thread_id") or raw_session.get("sdk_thread_id") or "").strip()
            legacy_thread_id = str(raw_session.get("thread_id") or "").strip()
            if completed_step == 0 and not sdk_thread_id and legacy_thread_id:
                raise WritingSDKRuntimeError(
                    "WRITING_DESKTOP_STEP1_NOT_MIGRATABLE",
                    "an uncompleted Desktop Step 1 thread cannot be duplicated in the isolated SDK home",
                )
            migration = bool(record.get("migration_new_thread"))
            if completed_step > 0 and not sdk_thread_id:
                # Desktop thread ids belong to the user's normal CODEX_HOME and
                # are not resumed from the isolated SDK home.  The first SDK
                # continuation gets one explicitly journaled migration thread.
                migration = True
            workflow = _workflow_snapshot(record.get("workflow_snapshot"), raw_session.get("workflow_snapshot"))
            run_until = _run_until_step(
                raw_session.get("run_until_step"),
                _run_until_step(record.get("run_until_step"), workflow_step_count(workflow)),
            )
            command_ids = list(self._commands_for_key(pipeline, paper_key, paper_id))
            existing = specifications.get(paper_key)
            if existing is not None:
                existing.update(
                    {
                        "paper_id": paper_id,
                        "thread_id": sdk_thread_id,
                        "completed_step": completed_step,
                        "run_until_step": run_until,
                        "migration_new_thread": migration,
                        "workflow_snapshot": workflow,
                        "reserved_pipeline_prefix": str(
                            record.get("reserved_pipeline_prefix")
                            or raw_session.get("reserved_pipeline_prefix")
                            or existing.get("reserved_pipeline_prefix")
                            or ""
                        ).strip(),
                    }
                )
                existing["command_ids"] = list(dict.fromkeys([*existing.get("command_ids", []), *command_ids]))
            else:
                specifications[paper_key] = {
                    "paper_key": paper_key,
                    "paper_id": paper_id,
                    "thread_id": sdk_thread_id,
                    "completed_step": completed_step,
                    "run_until_step": run_until,
                    "migration_new_thread": migration,
                    "workflow_snapshot": workflow,
                    "reserved_pipeline_prefix": str(
                        record.get("reserved_pipeline_prefix")
                        or raw_session.get("reserved_pipeline_prefix")
                        or ""
                    ).strip(),
                    "command_ids": command_ids,
                }

        for paper_key, record in journal.items():
            if paper_key in specifications:
                continue
            completed = _step_number(record.get("completed_step"))
            workflow = _workflow_snapshot(record.get("workflow_snapshot"))
            run_until = _run_until_step(record.get("run_until_step"), workflow_step_count(workflow))
            if completed >= run_until and str(record.get("status") or "") == "complete":
                continue
            paper_id = str(record.get("paper_id") or "")
            specifications[paper_key] = {
                "paper_key": paper_key,
                "paper_id": paper_id,
                "thread_id": str(record.get("thread_id") or ""),
                "completed_step": completed,
                "run_until_step": run_until,
                "migration_new_thread": bool(record.get("migration_new_thread")),
                "workflow_snapshot": workflow,
                "reserved_pipeline_prefix": str(record.get("reserved_pipeline_prefix") or "").strip(),
                "command_ids": list(self._commands_for_key(pipeline, paper_key, paper_id)),
            }

        plans: list[WritingPaperPlan] = []
        bindings: dict[str, _PlanBinding] = {}
        for paper_key in sorted(specifications, key=str.casefold):
            item = specifications[paper_key]
            workflow = _workflow_snapshot(item.get("workflow_snapshot"))
            journal_record = journal.get(paper_key, {})
            plan = WritingPaperPlan(
                paper_key=paper_key,
                prompt_provider=existing_writing_prompt_provider(
                    self.prompt_path,
                    paper_key,
                    workflow,
                    reserved_pipeline_prefix=str(item.get("reserved_pipeline_prefix") or ""),
                    preserve_unbound_step1_owner=_step1_prompt_was_sent_unbound(journal_record),
                ),
                run_until_step=int(item["run_until_step"]),
                completed_step=int(item["completed_step"]),
                paper_id=str(item["paper_id"]),
                thread_id=str(item["thread_id"]),
                migration_new_thread=bool(item["migration_new_thread"]),
                workflow_snapshot=workflow,
                reserved_pipeline_prefix=str(item.get("reserved_pipeline_prefix") or ""),
            )
            plan.validate()
            plans.append(plan)
            bindings[paper_key] = _PlanBinding(
                paper_key=paper_key,
                pipeline_id=plan.paper_id,
                command_ids=tuple(dict.fromkeys(item.get("command_ids", []))),
                run_until_step=plan.run_until_step,
                workflow_snapshot=workflow,
                reserved_pipeline_prefix=plan.reserved_pipeline_prefix,
            )
        return plans, bindings, pipeline

    def build_plans(self) -> list[WritingPaperPlan]:
        """Build a read-only deterministic view of current work."""

        plans, _bindings, _pipeline = self._build_plan_bundle()
        return plans

    def _mutate_pipeline(
        self,
        mutator: Callable[[dict[str, Any]], None],
        *,
        active_lease: _TemporaryActivePipelineLease | None = None,
    ) -> str:
        with FileLock(self.pipeline_lock_path, stale_seconds=30 * 60):
            self._assert_revalidation_transaction_absent()
            for _attempt in range(_PIPELINE_WRITE_ATTEMPTS):
                self._require_legacy_quiescent()
                state, digest = read_json_snapshot(self.pipeline_path, {})
                if not isinstance(state, dict):
                    raise WritingSDKRuntimeError(
                        "WRITING_PIPELINE_STATE_INVALID", "writing pipeline state must be an object"
                    )
                if active_lease is not None:
                    active_lease.observe_pipeline_read(digest)
                mutator(state)
                state["updated_at"] = now_iso()
                committed_digest = _serialized_json_sha256(state)
                self._require_legacy_quiescent()
                if write_json_atomic_if_unchanged(self.pipeline_path, state, digest):
                    if active_lease is not None:
                        active_lease.observe_pipeline_commit(committed_digest)
                    return committed_digest
        raise WritingSDKRuntimeError(
            "WRITING_PIPELINE_STATE_CHANGED",
            "writing pipeline changed repeatedly while SDK progress was being merged",
        )

    @staticmethod
    def _mark_commands(state: dict[str, Any], binding: _PlanBinding, snapshot: Mapping[str, Any], event: str) -> None:
        commands = state.get("commands", [])
        if not isinstance(commands, list):
            raise WritingSDKRuntimeError("WRITING_PIPELINE_STATE_INVALID", "writing pipeline commands must be a list")
        paper_id = str(snapshot.get("paper_id") or binding.pipeline_id or "")
        thread_id = str(snapshot.get("thread_id") or "")
        replaced_thread_id = str(snapshot.get("replaced_thread_id") or "")
        handled_at = now_iso()
        snapshot_status = str(snapshot.get("status") or "").strip().lower()
        sdk_status = {
            "thread_created": "queued",
            "step_completed": snapshot_status or "queued",
            "paper_blocked": "blocked",
            "paused": "paused",
            "waiting": snapshot_status or "waiting",
            "complete": "complete",
            "queued": "queued",
            "running": "running",
            "requeued": "queued",
            "revalidated_reopened": "queued",
        }.get(event, snapshot_status)
        for command in commands:
            if not isinstance(command, dict) or _command_id(command) not in binding.command_ids:
                continue
            command["sdk_paper_key"] = binding.paper_key
            if replaced_thread_id:
                for field in ("sdk_thread_id", "adopted_thread_id"):
                    if str(command.get(field) or "") == replaced_thread_id:
                        command.pop(field, None)
            if thread_id:
                command["sdk_thread_id"] = thread_id
            if paper_id:
                command["adopted_pipeline_id"] = paper_id
            if event == "thread_created" and thread_id:
                command["adopted_thread_id"] = thread_id
            command["sdk_status"] = sdk_status
            if sdk_status == "blocked" or sdk_status in _DISPATCH_WAITING_STATUSES:
                command["sdk_blocker"] = str(snapshot.get("blocker") or "WRITING_SDK_BLOCKED")
                command["sdk_blocker_message"] = str(snapshot.get("blocker_message") or "")
            else:
                command.pop("sdk_blocker", None)
                command.pop("sdk_blocker_message", None)
            prerequisite_gate = snapshot.get("prerequisite_gate")
            if sdk_status in _DISPATCH_WAITING_STATUSES and isinstance(prerequisite_gate, Mapping):
                command["sdk_prerequisite_gate"] = copy.deepcopy(dict(prerequisite_gate))
            else:
                command.pop("sdk_prerequisite_gate", None)
            action = str(command.get("action") or "").strip().lower()
            params = command.get("params") if isinstance(command.get("params"), dict) else {}
            command_target = (
                1 if action == "new_session" else _run_until_step(params.get("run_until_step"), binding.run_until_step)
            )
            completed_step = _step_number(snapshot.get("completed_step"))
            verified_done = (
                event in {"step_completed", "complete"} and completed_step >= command_target and bool(paper_id)
            )
            already_verified = (
                str(command.get("status") or "") == "done" and completed_step >= command_target and bool(paper_id)
            )
            if verified_done or already_verified:
                command["status"] = "done"
                command["handled_at"] = handled_at
            elif sdk_status == "blocked":
                command["status"] = "blocked"
                command["handled_at"] = handled_at
            elif sdk_status in _DISPATCH_WAITING_STATUSES:
                command["status"] = "waiting"
                command["handled_at"] = handled_at
            elif sdk_status == "paused":
                command["status"] = "paused"
                command["handled_at"] = handled_at
            elif event == "thread_created" or sdk_status == "running":
                command["status"] = "running"
                command.pop("handled_at", None)
            else:
                command["status"] = "queued"
                command.pop("handled_at", None)
            if action == "new_session" and str(command.get("status") or "") == "done":
                command["message"] = "Local Codex SDK verified the new session's Step 1 result."
                command["sdk_terminal_at"] = handled_at
            elif str(command.get("status") or "") == "done":
                command["message"] = "Local Codex SDK reached the requested writing step."
                command["sdk_terminal_at"] = handled_at
            elif sdk_status == "blocked":
                command["message"] = "Local Codex SDK stopped on a recorded writing blocker."
                command["sdk_terminal_at"] = handled_at
            elif sdk_status in _DISPATCH_WAITING_STATUSES:
                command["message"] = "Local Codex SDK is waiting for a recorded external condition."
                command["sdk_terminal_at"] = handled_at
            elif sdk_status == "paused":
                command["message"] = "Local Codex SDK paused at a safe step boundary."
                command["sdk_terminal_at"] = handled_at
            elif event == "requeued":
                command["message"] = "Local Codex SDK explicitly requeued the next uncommitted writing step."
                command.pop("sdk_terminal_at", None)
            elif event == "revalidated_reopened":
                command["message"] = "Current semantic validation reopened the last completed writing step."
                command.pop("sdk_terminal_at", None)
            else:
                command.pop("sdk_terminal_at", None)

    @staticmethod
    def _bind_sdk_thread(session: dict[str, Any], sdk_thread_id: str) -> None:
        if not sdk_thread_id:
            return
        stored_sdk = str(session.get("sdk_thread_id") or "")
        if stored_sdk and stored_sdk != sdk_thread_id:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_THREAD_OWNER_MISMATCH", "the pipeline session is bound to another SDK thread"
            )
        current_thread = str(session.get("thread_id") or "")
        if current_thread and current_thread != sdk_thread_id and not stored_sdk:
            session.setdefault("legacy_thread_id", current_thread)
        session["sdk_thread_id"] = sdk_thread_id
        session["thread_id"] = sdk_thread_id

    @staticmethod
    def _normalize_active_pipeline(state: dict[str, Any]) -> None:
        sessions = state.get("sessions", {})
        if not isinstance(sessions, dict):
            raise WritingSDKRuntimeError(
                "WRITING_PIPELINE_STATE_INVALID", "writing pipeline sessions must be an object"
            )
        candidates = sorted(
            (
                str(raw_session.get("pipeline_id") or paper_id)
                for paper_id, raw_session in sessions.items()
                if isinstance(raw_session, dict)
                and str(raw_session.get("status") or "").strip().lower() in _PIPELINE_ACTIVE_STATUSES
            ),
            key=str.casefold,
        )
        current = str(state.get("active_pipeline_id") or "")
        if current in candidates:
            return
        if candidates:
            state["active_pipeline_id"] = candidates[0]
        else:
            state.pop("active_pipeline_id", None)

    def _merge_pipeline_snapshot(
        self,
        state: dict[str, Any],
        binding: _PlanBinding,
        event: str,
        snapshot: Mapping[str, Any],
    ) -> None:
        sessions = state.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            raise WritingSDKRuntimeError(
                "WRITING_PIPELINE_STATE_INVALID", "writing pipeline sessions must be an object"
            )
        snapshot_status = str(snapshot.get("status") or "").strip().lower()
        normalized_event = {
            "paper_paused": "paused",
            "paper_waiting_prerequisite": "waiting",
            "paper_prerequisite_ready": "queued",
            "step_started": "running",
        }.get(event, event)
        if event == "paper_result":
            normalized_event = {
                "blocked": "paper_blocked",
                "paused": "paused",
                "complete": "complete",
                "queued": "queued",
                "running": "running",
                "starting": "running",
                "creating_thread": "running",
                "waiting_capacity": "waiting",
                "waiting_event": "waiting",
                "waiting_prerequisite": "waiting",
            }.get(snapshot_status, "")
        if normalized_event not in {
            "thread_created",
            "step_completed",
            "step_artifacts_reconciled",
            "paper_blocked",
            "paused",
            "complete",
            "queued",
            "running",
            "waiting",
            "requeued",
            "revalidated_reopened",
        }:
            return

        paper_key = binding.paper_key
        paper_id = str(snapshot.get("paper_id") or binding.pipeline_id or "").strip()
        sdk_thread_id = str(snapshot.get("thread_id") or "").strip()
        command_event = normalized_event
        command_snapshot: Mapping[str, Any] = snapshot
        session: dict[str, Any] | None = None
        if paper_id:
            raw_session = sessions.setdefault(paper_id, {})
            if not isinstance(raw_session, dict):
                raise WritingSDKRuntimeError(
                    "WRITING_PIPELINE_STATE_INVALID", "writing pipeline contains an invalid session"
                )
            session = raw_session
            session["pipeline_id"] = paper_id
            session["sdk_paper_key"] = paper_key
            session["run_until_step"] = binding.run_until_step
            session["workflow_snapshot"] = copy.deepcopy(binding.workflow_snapshot)
            session["workflow_version_id"] = str(binding.workflow_snapshot.get("version_id") or "")
            if binding.reserved_pipeline_prefix:
                session["reserved_pipeline_prefix"] = binding.reserved_pipeline_prefix
            self._bind_sdk_thread(session, sdk_thread_id)

        if session is not None:
            exact_sdk_status = snapshot_status or {
                "thread_created": "queued",
                "step_completed": "queued",
                "step_artifacts_reconciled": "queued",
                "paper_blocked": "blocked",
                "paused": "paused",
                "complete": "complete",
                "queued": "queued",
                "running": "running",
                "waiting": "waiting",
                "requeued": "queued",
            }.get(normalized_event, "")
            session["sdk_status"] = exact_sdk_status
            if normalized_event == "thread_created":
                session["status"] = "queued"
                session["evidence"] = "Local Codex SDK thread created and bound."
                session.pop("blocker", None)
                session.pop("sdk_blocker_message", None)
                session.pop("sdk_prerequisite_gate", None)
                session.pop("waiting_reason", None)
            elif normalized_event == "step_artifacts_reconciled":
                step = _step_number(snapshot.get("completed_step"))
                raw_steps = snapshot.get("steps", {})
                step_record = raw_steps.get("2", {}) if isinstance(raw_steps, dict) else {}
                if step != 2 or not isinstance(step_record, dict):
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_RECONCILIATION_INVALID",
                        "artifact reconciliation must describe completed Step 2",
                    )
                artifacts = step_record.get("artifacts")
                original = step_record.get("artifacts_at_completion")
                revisions = step_record.get("artifact_revisions")
                if not isinstance(artifacts, list) or not isinstance(original, list) or not isinstance(revisions, list):
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_RECONCILIATION_INVALID",
                        "artifact reconciliation is missing immutable provenance",
                    )
                existing = session.get("completion_reconciliation")
                turn_id = (str(existing.get("turn_id") or "") if isinstance(existing, dict) else "") or str(
                    step_record.get("turn_id") or ""
                )
                session["step_done"] = 2
                session["completion_reconciliation"] = {
                    "source": "local_codex_sdk_dispatch_journal",
                    "paper_key": paper_key,
                    "thread_id": sdk_thread_id,
                    "step_done": 2,
                    "turn_id": turn_id,
                    "artifacts_at_completion": copy.deepcopy(original),
                    "artifacts": copy.deepcopy(artifacts),
                    "artifact_revisions": copy.deepcopy(revisions),
                    "reconciled_at": str(step_record.get("reconciled_at") or now_iso()),
                }
                session["evidence"] = "Local Codex SDK validated and audited corrected Step 2 metadata."
            elif normalized_event in {"step_completed", "complete"}:
                step = _step_number(snapshot.get("completed_step"))
                session["step_done"] = step
                terminal = step >= binding.run_until_step
                session["status"] = "complete" if terminal else "queued"
                session["sdk_status"] = "complete" if terminal else "queued"
                if normalized_event == "complete" and not terminal:
                    command_event = "step_completed"
                    command_snapshot = {**dict(snapshot), "status": "queued"}
                session["evidence"] = f"Local Codex SDK verified PIPELINE_ID={paper_id}; STEP_DONE={step}."
                raw_steps = snapshot.get("steps", {})
                step_record = raw_steps.get(str(step), {}) if step and isinstance(raw_steps, dict) else {}
                session["completion_reconciliation"] = {
                    "source": "local_codex_sdk_dispatch_journal",
                    "paper_key": paper_key,
                    "thread_id": sdk_thread_id,
                    "step_done": step,
                    "turn_id": str(step_record.get("turn_id") or "") if isinstance(step_record, dict) else "",
                    "artifacts": list(step_record.get("artifacts", []))
                    if isinstance(step_record, dict) and isinstance(step_record.get("artifacts", []), list)
                    else [],
                    "reconciled_at": now_iso(),
                }
                session.pop("blocker", None)
                session.pop("sdk_blocker_message", None)
                session.pop("sdk_prerequisite_gate", None)
                session.pop("waiting_reason", None)
                if terminal:
                    session.pop("run_until_step", None)
            elif normalized_event == "paper_blocked":
                session["status"] = "blocked"
                session["evidence"] = str(snapshot.get("blocker_message") or snapshot.get("blocker") or "")
                session["blocker"] = str(snapshot.get("blocker") or "WRITING_SDK_BLOCKED")
                session["sdk_blocker_message"] = str(snapshot.get("blocker_message") or "")
            elif normalized_event == "paused":
                session["status"] = "paused"
                session["evidence"] = "Local Codex SDK paused at a step boundary."
                session.pop("blocker", None)
                session.pop("sdk_blocker_message", None)
                session.pop("sdk_prerequisite_gate", None)
                session.pop("waiting_reason", None)
            elif normalized_event == "waiting":
                session["status"] = "waiting"
                session["evidence"] = str(snapshot.get("blocker_message") or snapshot.get("blocker") or "")
                session["blocker"] = str(snapshot.get("blocker") or "WRITING_SDK_WAITING")
                session["sdk_blocker_message"] = str(snapshot.get("blocker_message") or "")
                prerequisite_gate = snapshot.get("prerequisite_gate")
                if isinstance(prerequisite_gate, Mapping):
                    session["sdk_prerequisite_gate"] = copy.deepcopy(dict(prerequisite_gate))
                    session["waiting_reason"] = str(snapshot.get("waiting_reason") or "prerequisite")
                else:
                    session.pop("sdk_prerequisite_gate", None)
                    session.pop("waiting_reason", None)
            elif normalized_event == "running":
                session["status"] = "running"
                session.pop("blocker", None)
                session.pop("sdk_blocker_message", None)
                session.pop("sdk_prerequisite_gate", None)
                session.pop("waiting_reason", None)
            elif normalized_event in {"queued", "requeued"}:
                completed = _step_number(snapshot.get("completed_step"))
                if completed:
                    session["step_done"] = max(_step_number(session.get("step_done")), completed)
                session["status"] = "queued"
                session["sdk_status"] = "queued"
                session["evidence"] = (
                    "Local Codex SDK recovery explicitly requeued the next uncommitted step."
                    if normalized_event == "requeued"
                    else "Local Codex SDK queued the next writing step."
                )
                session.pop("blocker", None)
                session.pop("sdk_blocker_message", None)
                session.pop("sdk_prerequisite_gate", None)
                session.pop("waiting_reason", None)
            elif normalized_event == "revalidated_reopened":
                completed = _step_number(snapshot.get("completed_step"))
                reopen = snapshot.get("revalidation_reopen")
                if not completed or not isinstance(reopen, Mapping):
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_REVALIDATE_PIPELINE_INVALID",
                        "revalidation reopen is missing its exact rollback provenance",
                    )
                session["step_done"] = completed
                session["status"] = "queued"
                session["sdk_status"] = "queued"
                session["evidence"] = "Current semantic validation reopened the last completed SDK step."
                session["completion_reconciliation"] = {
                    "source": "current_semantic_validator_reopen",
                    "paper_key": paper_key,
                    "thread_id": sdk_thread_id,
                    "step_done": completed,
                    "invalidated_step": _step_number(reopen.get("step")),
                    "invalidated_turn_id": str(reopen.get("turn_id") or ""),
                    "reconciled_at": str(reopen.get("at") or now_iso()),
                }
                session["revalidation_reopen"] = copy.deepcopy(dict(reopen))
                session.pop("blocker", None)
                session.pop("sdk_blocker_message", None)
                session.pop("sdk_prerequisite_gate", None)
                session.pop("waiting_reason", None)
            session["updated_at"] = now_iso()
        if normalized_event != "step_artifacts_reconciled":
            self._mark_commands(state, binding, command_snapshot, command_event)
        self._normalize_active_pipeline(state)

    def _progress_callback(
        self,
        bindings: Mapping[str, _PlanBinding],
        event: str,
        snapshot: dict[str, Any],
        *,
        active_lease: _TemporaryActivePipelineLease | None = None,
    ) -> None:
        paper_key = str(snapshot.get("paper_key") or "")
        binding = bindings.get(paper_key)
        if binding is None:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_PLAN_BINDING_MISSING", "dispatcher progress has no live pipeline binding"
            )
        normalized_event = {
            "paper_paused": "paused",
            "paper_waiting_prerequisite": "waiting",
            "paper_prerequisite_ready": "queued",
            "step_started": "running",
        }.get(event, event)
        if normalized_event not in {
            "thread_created",
            "step_completed",
            "step_artifacts_reconciled",
            "paper_blocked",
            "paused",
            "waiting",
            "queued",
            "running",
        }:
            return
        if active_lease is not None:
            active_lease.observe_pipeline_id(snapshot.get("paper_id"))

        refresh_projection = normalized_event in {
            "step_completed",
            "paper_blocked",
            "paused",
            "waiting",
            "queued",
            "running",
        }
        if refresh_projection:
            self._mark_unified_projection_dirty()

        def merge(state: dict[str, Any]) -> None:
            self._merge_pipeline_snapshot(state, binding, normalized_event, snapshot)

        self._mutate_pipeline(merge, active_lease=active_lease)
        if refresh_projection:
            self._refresh_unified_projection_after_step()

    def _synchronize_dispatch_result(
        self,
        bindings: Mapping[str, _PlanBinding],
        dispatched: Mapping[str, Any],
        *,
        active_lease: _TemporaryActivePipelineLease | None = None,
    ) -> dict[str, dict[str, Any]]:
        raw_papers = dispatched.get("papers", {})
        if not isinstance(raw_papers, dict):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_RESULT_INVALID", "writing SDK dispatcher papers must be an object"
            )
        papers: dict[str, dict[str, Any]] = {}
        for paper_key, binding in bindings.items():
            raw_snapshot = raw_papers.get(paper_key)
            if not isinstance(raw_snapshot, dict):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_RESULT_INVALID",
                    f"writing SDK dispatcher omitted the result for {paper_key}",
                )
            papers[paper_key] = dict(raw_snapshot)
            if active_lease is not None:
                active_lease.observe_pipeline_id(raw_snapshot.get("paper_id"))

        def merge(state: dict[str, Any]) -> None:
            for paper_key in sorted(papers, key=str.casefold):
                self._merge_pipeline_snapshot(state, bindings[paper_key], "paper_result", papers[paper_key])

        self._mutate_pipeline(merge, active_lease=active_lease)
        return papers

    def _should_continue(self, _step: int, snapshot: dict[str, Any]) -> bool:
        pipeline = self._load_pipeline()
        settings = pipeline.get("settings", {})
        if not isinstance(settings, dict):
            return False
        if str(settings.get("scheduling_mode") or "").strip().lower() != "auto":
            return False
        paper_key = str(snapshot.get("paper_key") or "").strip()
        with self._background_lock:
            pending_priority = set(self._background_priority_keys)
            active_priority = set(self._active_background_priority_keys)
        # A newly-created paper arrived after this dispatch pass took its plan
        # snapshot.  Let active model work finish, then pause older papers at
        # the next Step boundary so the worker can run only the new batch.
        # If the current pass is already an explicitly new batch, finish that
        # batch first and service a later new request on the following pass.
        if not pending_priority:
            return True
        if active_priority:
            return paper_key in active_priority
        return paper_key in pending_priority

    @staticmethod
    def _parallel_limit(pipeline: Mapping[str, Any]) -> int:
        settings = pipeline.get("settings", {})
        raw = (
            settings.get("max_parallel_workers", config.WRITING_DEFAULT_PARALLEL_WORKERS)
            if isinstance(settings, dict)
            else 1
        )
        if isinstance(raw, bool):
            raise WritingSDKRuntimeError(
                "WRITING_PARALLEL_LIMIT_INVALID", "writing parallel limit must be an integer from 1 to 10"
            )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise WritingSDKRuntimeError(
                "WRITING_PARALLEL_LIMIT_INVALID", "writing parallel limit must be an integer from 1 to 10"
            ) from None
        if not 1 <= value <= GLOBAL_CODEX_SESSION_LIMIT:
            raise WritingSDKRuntimeError(
                "WRITING_PARALLEL_LIMIT_INVALID", "writing parallel limit must be an integer from 1 to 10"
            )
        return value

    @staticmethod
    def _aggregate_dispatch_status(papers: Mapping[str, Mapping[str, Any]]) -> tuple[str, dict[str, int]]:
        counts: dict[str, int] = {}
        for paper in papers.values():
            status = str(paper.get("status") or "unknown").strip().lower()
            counts[status] = counts.get(status, 0) + 1
        if counts.get("blocked"):
            return "blocked", counts
        if any(counts.get(status) for status in _DISPATCH_WAITING_STATUSES):
            return "waiting", counts
        if counts.get("paused"):
            return "paused", counts
        if counts and counts.get("complete") == sum(counts.values()):
            return "complete", counts
        return "incomplete", counts

    @staticmethod
    def _validated_until_step(until_step: int | None) -> int | None:
        if until_step is None:
            return None
        if isinstance(until_step, bool):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_UNTIL_STEP_INVALID",
                f"until_step must be an integer from 1 to {config.WRITING_MAX_WORKFLOW_STEPS}",
            )
        try:
            value = int(until_step)
        except (TypeError, ValueError):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_UNTIL_STEP_INVALID",
                f"until_step must be an integer from 1 to {config.WRITING_MAX_WORKFLOW_STEPS}",
            ) from None
        if not 1 <= value <= config.WRITING_MAX_WORKFLOW_STEPS:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_UNTIL_STEP_INVALID",
                f"until_step must be an integer from 1 to {config.WRITING_MAX_WORKFLOW_STEPS}",
            )
        return value

    def _projection_path_kwargs(self) -> dict[str, Path]:
        kwargs: dict[str, Path] = {}
        if self._projection_dirty_path is not None:
            kwargs["dirty_path"] = self._projection_dirty_path
        if self._projection_lock_path is not None:
            kwargs["lock_path"] = self._projection_lock_path
        return kwargs

    def _mark_unified_projection_dirty(self) -> None:
        if not self._projection_refresh_enabled or self._projection_is_canonical:
            return
        try:
            unified_projection.mark_projection_dirty(
                "writing_state_changed",
                **self._projection_path_kwargs(),
            )
        except Exception as exc:
            self._last_projection_refresh = {
                "status": "deferred",
                "dirty": True,
                "refreshed": False,
                "code": unified_projection.PROJECTION_REFRESH_FAILED,
                "error_type": type(exc).__name__,
            }

    def _refresh_unified_projection_after_step(self) -> None:
        if not self._projection_refresh_enabled:
            return
        if self._projection_is_canonical:
            try:
                from .smart_submission import refresh_smart_submission_projection

                self._last_projection_refresh = refresh_smart_submission_projection(
                    force=True,
                    sync_callback=self._projection_sync_callback,
                )
            except Exception as exc:
                self._last_projection_refresh = {
                    "status": "deferred",
                    "dirty": True,
                    "refreshed": False,
                    "code": unified_projection.PROJECTION_REFRESH_FAILED,
                    "error_type": type(exc).__name__,
                }
            return
        kwargs: dict[str, Any] = {
            "force": True,
            **self._projection_path_kwargs(),
        }
        if self._projection_sync_callback is not None:
            kwargs["sync_callback"] = self._projection_sync_callback
        self._last_projection_refresh = unified_projection.refresh_unified_projection_if_dirty(**kwargs)

    def _restore_temporary_active_pipeline(
        self,
        *,
        lease: _TemporaryActivePipelineLease,
    ) -> dict[str, Any]:
        """Restore a temporary exact-run selection without clobbering concurrent selection."""

        if not lease.valid:
            return {"status": "skipped", "restored": False, "reason": "pipeline_state_changed"}
        with FileLock(self.pipeline_lock_path, stale_seconds=30 * 60):
            self._assert_revalidation_transaction_absent()
            for attempt in range(_PIPELINE_WRITE_ATTEMPTS):
                self._require_legacy_quiescent()
                state, digest = read_json_snapshot(self.pipeline_path, {})
                if not isinstance(state, dict):
                    raise WritingSDKRuntimeError(
                        "WRITING_PIPELINE_STATE_INVALID", "writing pipeline state must be an object"
                    )
                sessions = state.get("sessions", {})
                if not isinstance(sessions, dict):
                    raise WritingSDKRuntimeError(
                        "WRITING_PIPELINE_STATE_INVALID", "writing pipeline sessions must be an object"
                    )
                if digest != lease.expected_pipeline_sha256:
                    return {"status": "skipped", "restored": False, "reason": "pipeline_state_changed"}
                current_present = "active_pipeline_id" in state
                current = str(state.get("active_pipeline_id") or "")
                if current_present == lease.had_active_pipeline and current == lease.active_pipeline_id:
                    return {"status": "unchanged", "restored": False, "reason": "already_original"}
                if not lease.selected_pipeline_ids:
                    return {"status": "skipped", "restored": False, "reason": "selected_pipeline_unbound"}
                if lease.had_active_pipeline and lease.active_pipeline_id and lease.active_pipeline_id not in sessions:
                    return {"status": "skipped", "restored": False, "reason": "original_pipeline_missing"}
                if lease.had_active_pipeline:
                    state["active_pipeline_id"] = lease.active_pipeline_id
                else:
                    state.pop("active_pipeline_id", None)
                state["updated_at"] = now_iso()
                self._require_legacy_quiescent()
                if write_json_atomic_if_unchanged(self.pipeline_path, state, digest):
                    return {"status": "restored", "restored": True, "reason": "temporary_selection"}
                if attempt + 1 >= _PIPELINE_WRITE_ATTEMPTS:
                    break
        raise WritingSDKRuntimeError(
            "WRITING_PIPELINE_STATE_CHANGED",
            "writing pipeline changed repeatedly while restoring the active pipeline",
        )

    @staticmethod
    def _active_pipeline_restore_failure(exc: Exception) -> dict[str, Any]:
        return {
            "status": "failed",
            "restored": False,
            "reason": "restore_failed",
            "code": str(getattr(exc, "code", type(exc).__name__)),
            "error_type": type(exc).__name__,
        }

    def _restore_temporary_run_targets(self, targets: Mapping[str, tuple[int, int]]) -> None:
        if not targets:
            return
        with FileLock(self.dispatch_lock_path, stale_seconds=7 * 24 * 60 * 60):
            self._assert_revalidation_transaction_absent()
            for attempt in range(_PIPELINE_WRITE_ATTEMPTS):
                state, digest = read_json_snapshot(
                    self.journal_path,
                    {"schema": WRITING_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []},
                )
                if (
                    not isinstance(state, dict)
                    or state.get("schema") != WRITING_SDK_JOURNAL_SCHEMA
                    or not isinstance(state.get("papers", {}), dict)
                    or not isinstance(state.get("history", []), list)
                ):
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_JOURNAL_INVALID",
                        "writing SDK journal cannot restore a temporary run target safely",
                    )
                restored: list[dict[str, Any]] = []
                for paper_key, (temporary_target, original_target) in targets.items():
                    record = state["papers"].get(paper_key)
                    if record is None:
                        # Registration is first persisted by the dispatcher's
                        # next journal mutation.  A transport/context failure
                        # before that point leaves no temporary target to undo.
                        continue
                    if not isinstance(record, dict):
                        raise WritingSDKRuntimeError(
                            "WRITING_SDK_JOURNAL_INVALID",
                            f"temporary target paper is invalid: {paper_key}",
                        )
                    stored_target = _run_until_step(record.get("run_until_step"))
                    if stored_target == original_target:
                        continue
                    if stored_target != temporary_target:
                        raise WritingSDKRuntimeError(
                            "WRITING_SDK_TEMPORARY_TARGET_DIVERGED",
                            f"temporary target changed unexpectedly for {paper_key}",
                        )
                    record["run_until_step"] = original_target
                    if (
                        str(record.get("status") or "").strip().lower() == "complete"
                        and _step_number(record.get("completed_step")) < original_target
                    ):
                        record["status"] = "queued"
                    record["updated_at"] = now_iso()
                    restored.append(
                        {
                            "paper_key": paper_key,
                            "temporary_target": temporary_target,
                            "restored_target": original_target,
                        }
                    )
                if not restored:
                    return
                restored_at = now_iso()
                state["history"].append(
                    {
                        "at": restored_at,
                        "event": "temporary_run_targets_restored",
                        "targets": restored,
                    }
                )
                state["updated_at"] = restored_at
                if write_json_atomic_if_unchanged(self.journal_path, state, digest):
                    return
                if attempt + 1 >= _PIPELINE_WRITE_ATTEMPTS:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_JOURNAL_CHANGED",
                        "writing SDK journal changed repeatedly while restoring temporary targets",
                    )

    def run(
        self,
        paper_keys: list[str] | None = None,
        *,
        until_step: int | None = None,
    ) -> dict[str, Any]:
        """Blocking run used by CLI callers and the GUI background worker."""

        try:
            return self._run_once(paper_keys=paper_keys, until_step=until_step)
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            self._last_error = code
            self._last_result = {
                "ok": False,
                "schema": WRITING_SDK_RUNTIME_SCHEMA,
                "status": "failed",
                "blocker": code,
                "error_type": type(exc).__name__,
            }
            if self._last_active_pipeline_restore is not None:
                self._last_result["active_pipeline_restore"] = dict(self._last_active_pipeline_restore)
            raise

    def _run_once(
        self,
        *,
        paper_keys: list[str] | None,
        until_step: int | None,
    ) -> dict[str, Any]:
        """Execute one already-authorized blocking SDK scheduling pass."""

        self._last_projection_refresh = None
        self._last_active_pipeline_restore = None
        # Recover before either state file is read for planning.  A second
        # guard runs under the dispatcher's lock to close the cross-process
        # race between this preflight and model construction.
        self._recover_revalidation_transaction()
        account = self.ensure_ready()
        automation = self._require_legacy_quiescent()
        selected_keys: tuple[str, ...] | None = None
        if paper_keys is not None:
            selected_keys = tuple(dict.fromkeys(str(value).strip() for value in paper_keys))
            if any(not value for value in selected_keys):
                raise WritingSDKRuntimeError("WRITING_SDK_RUN_SELECTION_INVALID", "paper keys must be non-empty")
        selected_recovery: dict[str, Any] | None = None
        if selected_keys:
            # An explicit selected-paper wakeup is also an explicit request to
            # retry that exact paper when its recorded blocker is on the
            # proven-safe recovery allowlist.  The adapter has already queued
            # the selected pipeline ids; this closes the second half of the
            # state transition in the authoritative SDK journal before plans
            # are built.  When invoked by the GUI worker, recovery runs at a
            # Step boundary on the worker thread itself, never during a turn.
            selected_recovery = self.requeue_recoverable(
                list(selected_keys),
                _allow_current_background_worker=True,
            )
        selected = set(selected_keys) if selected_keys is not None else None
        pipeline, pipeline_digest = self._load_pipeline_snapshot()
        plans, bindings, pipeline = self._build_plan_bundle(
            pipeline,
            selected_paper_keys=selected,
        )
        if selected_keys is not None:
            available = {plan.paper_key for plan in plans}
            missing = [paper_key for paper_key in selected_keys if paper_key not in available]
            if missing:
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_RUN_SELECTION_NOT_FOUND",
                    f"selected writing SDK plan is unavailable: {missing[0]}",
                )
        temporary_until = self._validated_until_step(until_step)
        if temporary_until is not None and not selected_keys:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_UNTIL_STEP_REQUIRES_SELECTION",
                "until_step requires at least one exact paper key",
            )
        settings = pipeline.get("settings", {})
        scheduling_mode = (
            str(settings.get("scheduling_mode") or "").strip().lower() if isinstance(settings, dict) else ""
        )
        selected_manual_lease = bool(selected_keys) and temporary_until is not None and scheduling_mode == "manual"
        temporary_targets: dict[str, tuple[int, int]] = {}
        if temporary_until is not None:
            capped_plans: list[WritingPaperPlan] = []
            for plan in plans:
                if temporary_until <= plan.completed_step:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_UNTIL_STEP_ALREADY_COMPLETED",
                        f"{plan.paper_key} already completed Step {temporary_until}",
                    )
                if temporary_until > plan.run_until_step:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_UNTIL_STEP_EXPANDS_TARGET",
                        f"until_step exceeds the existing target for {plan.paper_key}",
                    )
                if temporary_until < plan.run_until_step:
                    temporary_targets[plan.paper_key] = (
                        temporary_until,
                        plan.run_until_step,
                    )
                    plan = replace(plan, run_until_step=temporary_until)
                capped_plans.append(plan)
            plans = capped_plans
        temporary_active_pipeline: _TemporaryActivePipelineLease | None = None
        if selected_keys and temporary_until is not None:
            temporary_active_pipeline = _TemporaryActivePipelineLease(
                had_active_pipeline="active_pipeline_id" in pipeline,
                active_pipeline_id=str(pipeline.get("active_pipeline_id") or ""),
                expected_pipeline_sha256=pipeline_digest,
                selected_pipeline_ids={binding.pipeline_id for binding in bindings.values() if binding.pipeline_id},
            )
        if not plans:
            result = {
                "ok": True,
                "schema": WRITING_SDK_RUNTIME_SCHEMA,
                "status": "idle",
                "plan_count": 0,
                "papers": {},
                "selected_paper_keys": list(selected_keys or []),
                "until_step": temporary_until,
                "selected_manual_lease": selected_manual_lease,
                "account": account,
                "legacy_automation": automation,
                "model": self.model,
                "step_token_budgets": dict(config.WRITING_SDK_STEP_TOKEN_BUDGETS),
            }
            if selected_recovery is not None:
                result["selected_recovery"] = selected_recovery
            self._last_result = result
            self._last_error = ""
            return result
        dispatcher = self.dispatcher_factory(
            root=self.root,
            papers_dir=self.papers_dir,
            journal_path=self.journal_path,
            lock_path=self.dispatch_lock_path,
            codex_factory=self._codex_factory,
            max_parallel_papers=self._parallel_limit(pipeline),
            prompt_sha256_manifest=TUNED_WRITING_PROMPT_SHA256,
            progress_callback=lambda event, snapshot: self._progress_callback(
                bindings,
                event,
                snapshot,
                active_lease=temporary_active_pipeline,
            ),
            should_continue=((lambda _step, _snapshot: True) if selected_manual_lease else self._should_continue),
            pre_dispatch_guard=self._dispatch_revalidation_guard,
            model=self.model,
            step_token_budgets=config.WRITING_SDK_STEP_TOKEN_BUDGETS,
            gate_policy_path=config.WRITING_GATE_POLICY_PATH,
        )
        try:
            try:
                dispatched = asyncio.run(dispatcher.run_until_idle(plans))
            finally:
                self._restore_temporary_run_targets(temporary_targets)
            if not isinstance(dispatched, dict):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_RESULT_INVALID", "writing SDK dispatcher returned a non-object result"
                )
            papers = self._synchronize_dispatch_result(
                bindings,
                dispatched,
                active_lease=temporary_active_pipeline,
            )
        except BaseException:
            if temporary_active_pipeline is not None:
                try:
                    self._last_active_pipeline_restore = self._restore_temporary_active_pipeline(
                        lease=temporary_active_pipeline,
                    )
                except Exception as restore_exc:
                    self._last_active_pipeline_restore = self._active_pipeline_restore_failure(restore_exc)
            raise
        else:
            if temporary_active_pipeline is not None:
                try:
                    self._last_active_pipeline_restore = self._restore_temporary_active_pipeline(
                        lease=temporary_active_pipeline,
                    )
                except Exception as restore_exc:
                    self._last_active_pipeline_restore = self._active_pipeline_restore_failure(restore_exc)
                    raise
        aggregate_status, status_counts = self._aggregate_dispatch_status(papers)
        result = {
            **dict(dispatched),
            "schema": WRITING_SDK_RUNTIME_SCHEMA,
            "ok": aggregate_status == "complete",
            "status": aggregate_status,
            "terminal_status_counts": status_counts,
            "plan_count": len(plans),
            "papers": papers,
            "selected_paper_keys": list(selected_keys) if selected_keys is not None else None,
            "until_step": temporary_until,
            "selected_manual_lease": selected_manual_lease,
            "account": account,
            "legacy_automation": automation,
            "model": self.model,
            "step_token_budgets": dict(config.WRITING_SDK_STEP_TOKEN_BUDGETS),
        }
        if selected_recovery is not None:
            result["selected_recovery"] = selected_recovery
        if self._last_active_pipeline_restore is not None:
            result["active_pipeline_restore"] = dict(self._last_active_pipeline_restore)
        if self._last_projection_refresh is not None:
            result["projection_refresh"] = dict(self._last_projection_refresh)
        self._last_result = result
        self._last_error = ""
        return result

    def _dispatch_generation(self) -> int:
        try:
            pipeline = self._load_pipeline()
        except Exception:
            return 0
        settings = pipeline.get("settings", {})
        raw_generation = (
            settings.get(config.WRITING_SDK_DISPATCH_GENERATION_KEY, 0) if isinstance(settings, dict) else 0
        )
        try:
            generation = int(raw_generation)
        except (TypeError, ValueError):
            return 0
        return max(generation, 0)

    def _background_target(self) -> None:
        current_thread = threading.current_thread()
        while True:
            with self._background_lock:
                if self._background_owner is not self or self._background_thread is not current_thread:
                    return
                observed_wakeup = type(self)._background_wakeup_serial
                active_priority = tuple(sorted(self._background_priority_keys, key=str.casefold))
                self._background_priority_keys.clear()
                self._active_background_priority_keys = active_priority
            observed_generation = self._dispatch_generation()
            try:
                if active_priority:
                    self.run(list(active_priority))
                else:
                    self.run()
            except Exception as exc:
                self._last_error = str(getattr(exc, "code", type(exc).__name__))
            current_generation = self._dispatch_generation()
            with self._background_lock:
                self._active_background_priority_keys = ()
                wake_requested = type(self)._background_wakeup_serial != observed_wakeup
                dispatch_requested = current_generation != observed_generation
                priority_requested = bool(self._background_priority_keys)
                if wake_requested or dispatch_requested or priority_requested:
                    continue
                if self._background_owner is self and self._background_thread is current_thread:
                    type(self)._background_owner = None
                    type(self)._background_thread = None
                return

    def start_background(
        self,
        *,
        priority_paper_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Start one GUI worker and optionally prioritize newly-created papers.

        Priority never cancels an active Codex turn.  An older all-queue pass
        yields at its next Step boundary, then the worker runs only the exact
        newly-created paper keys.  Old backlog is resumed only by an explicit
        all-queue start.
        """

        self.ensure_ready()
        self._require_legacy_quiescent()
        normalized_priority = tuple(
            dict.fromkeys(str(value).strip() for value in (priority_paper_keys or ()))
        )
        if any(not value for value in normalized_priority):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_PRIORITY_SELECTION_INVALID",
                "priority paper keys must be non-empty",
            )
        with self._background_lock:
            type(self)._background_wakeup_serial += 1
            existing = self._background_thread
            if existing is not None and existing.is_alive():
                already_running = True
                if self._background_owner is self:
                    self._background_priority_keys.update(normalized_priority)
            else:
                already_running = False
                self._background_priority_keys.update(normalized_priority)
                self._last_error = ""
                self._last_result = {
                    "ok": True,
                    "schema": WRITING_SDK_RUNTIME_SCHEMA,
                    "status": "running",
                }
                worker = threading.Thread(
                    target=self._background_target,
                    name="writing-sdk-runtime",
                    daemon=True,
                )
                type(self)._background_owner = self
                type(self)._background_thread = worker
                worker.start()
        if already_running:
            return {
                "started": False,
                "already_running": True,
                "priority_paper_keys": list(normalized_priority),
                "status": self.status(check_account=False),
            }
        return {
            "started": True,
            "already_running": False,
            "priority_paper_keys": list(normalized_priority),
            "status": self.status(check_account=False),
        }

    @staticmethod
    def _enter_revalidation_lock(path: Path, *, stale_seconds: int) -> FileLock:
        lock = FileLock(path, stale_seconds=stale_seconds)
        try:
            lock.__enter__()
        except RuntimeError as exc:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_LOCKED",
                "an SDK dispatch or pipeline writer lock is active; revalidation made no change",
            ) from exc
        return lock

    @staticmethod
    def _semantic_revalidation_snapshot(result: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(result.get("ready"), bool) or not isinstance(result.get("issues"), list):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_RESULT_INVALID",
                "the current semantic validator returned malformed evidence",
            )
        snapshot: dict[str, Any] = {
            "status": str(result.get("status") or "BLOCKED"),
            "ready": result["ready"],
            "counts": copy.deepcopy(result.get("counts") or {}),
            "issues": copy.deepcopy(result["issues"]),
            "warnings": copy.deepcopy(result.get("warnings") or []),
        }
        if isinstance(result.get("path"), str):
            snapshot["path"] = str(result.get("path") or "")
        compile_contract = result.get("compile_contract")
        if isinstance(compile_contract, Mapping):
            snapshot["compile_contract"] = copy.deepcopy(dict(compile_contract))
        return snapshot

    @staticmethod
    def _validated_retry_baseline(
        step: int,
        artifacts: list[dict[str, Any]],
        semantic_validation: Mapping[str, Any],
        *,
        validated_at: str,
    ) -> dict[str, Any]:
        feedback = writing_retry_validator_feedback(semantic_validation)
        if feedback is None:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_RETRY_VALIDATION_INVALID",
                "semantic retry validation has no bounded allowlisted feedback",
            )
        normalized_artifacts: list[dict[str, Any]] = []
        for artifact in artifacts:
            digest = _normalized_sha256(artifact.get("sha256")) if isinstance(artifact, Mapping) else ""
            if (
                not isinstance(artifact, Mapping)
                or not isinstance(artifact.get("path"), str)
                or not digest
                or not isinstance(artifact.get("size"), int)
                or isinstance(artifact.get("size"), bool)
                or artifact["size"] <= 0
            ):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_RETRY_VALIDATION_INVALID",
                    "semantic retry artifact baseline is malformed",
                )
            normalized_artifacts.append(
                {
                    "path": artifact["path"],
                    "sha256": digest,
                    "size": artifact["size"],
                }
            )
        if not normalized_artifacts:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_RETRY_VALIDATION_INVALID",
                "semantic retry has no current artifact baseline",
            )
        return {
            "schema": _VALIDATED_RETRY_BASELINE_SCHEMA,
            "validated_at": validated_at,
            "step": step,
            "artifacts": normalized_artifacts,
            "validator": feedback,
        }

    def _revalidation_fault(self, point: str) -> None:
        injector = self._revalidation_fault_injector
        if injector is not None:
            injector(point)

    def _revalidation_transaction_present(self) -> bool:
        try:
            self.revalidation_transaction_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            # An inaccessible marker is still unresolved transaction evidence.
            return True
        return True

    @staticmethod
    def _revalidation_transaction_image(value: bytes) -> dict[str, Any]:
        return {
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "base64": base64.b64encode(value).decode("ascii"),
        }

    def _revalidation_transaction_bytes(
        self,
        *,
        paper_key: str,
        journal_original: bytes,
        journal_intended: bytes,
        pipeline_original: bytes,
        pipeline_intended: bytes,
    ) -> bytes:
        images = (journal_original, journal_intended, pipeline_original, pipeline_intended)
        if any(len(image) > _REVALIDATION_TRANSACTION_MAX_IMAGE_BYTES for image in images):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_TOO_LARGE",
                "journal or pipeline state exceeds the bounded revalidation transaction image size",
            )
        marker = {
            "schema": _REVALIDATION_TRANSACTION_SCHEMA,
            "transaction_id": uuid4().hex,
            "operation": "revalidate_last_completed",
            "created_at": now_iso(),
            "paper_key": paper_key,
            "targets": {
                "journal": {
                    "path": str(self.journal_path),
                    "original": self._revalidation_transaction_image(journal_original),
                    "intended": self._revalidation_transaction_image(journal_intended),
                },
                "pipeline": {
                    "path": str(self.pipeline_path),
                    "original": self._revalidation_transaction_image(pipeline_original),
                    "intended": self._revalidation_transaction_image(pipeline_intended),
                },
            },
        }
        raw = (json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(raw) > _REVALIDATION_TRANSACTION_MAX_MARKER_BYTES:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_TOO_LARGE",
                "the revalidation transaction marker exceeds its bounded encoded size",
            )
        return raw

    @staticmethod
    def _decode_revalidation_transaction_image(value: Any, *, label: str) -> bytes:
        expected_keys = {"size", "sha256", "base64"}
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                f"the {label} transaction image has an invalid schema",
            )
        size = value.get("size")
        digest = _normalized_sha256(value.get("sha256"))
        encoded = value.get("base64")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > _REVALIDATION_TRANSACTION_MAX_IMAGE_BYTES
            or not digest
            or not isinstance(encoded, str)
            or len(encoded) > ((_REVALIDATION_TRANSACTION_MAX_IMAGE_BYTES + 2) // 3) * 4
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                f"the {label} transaction image exceeds its bounds or has invalid metadata",
            )
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                f"the {label} transaction image is not strict base64",
            ) from exc
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                f"the {label} transaction image does not match its exact size and digest",
            )
        return raw

    @staticmethod
    def _validate_revalidation_transaction_json(
        images: Mapping[str, tuple[bytes, bytes]], paper_key: str
    ) -> None:
        decoded: dict[str, tuple[Any, Any]] = {}
        for role, pair in images.items():
            values: list[Any] = []
            for raw in pair:
                try:
                    values.append(json.loads(raw.decode("utf-8-sig")))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise WritingSDKRuntimeError(
                        "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                        f"the {role} transaction image is not valid JSON",
                    ) from exc
            decoded[role] = (values[0], values[1])
        for value in decoded["journal"]:
            if (
                not isinstance(value, dict)
                or value.get("schema") != WRITING_SDK_JOURNAL_SCHEMA
                or not isinstance(value.get("papers"), dict)
                or not isinstance(value.get("history"), list)
                or not isinstance(value["papers"].get(paper_key), dict)
            ):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                    "the transaction journal image does not retain its exact paper binding",
                )
        for value in decoded["pipeline"]:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("sessions", {}), dict)
                or not isinstance(value.get("commands", []), list)
            ):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                    "the transaction pipeline image has an invalid state schema",
                )

    def _read_revalidation_transaction(
        self,
    ) -> tuple[dict[str, Any], bytes, dict[str, tuple[bytes, bytes]]] | None:
        path = self.revalidation_transaction_path
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                "the revalidation transaction marker cannot be inspected",
            ) from exc
        if path.is_symlink() or not path.is_file() or stat.st_size > _REVALIDATION_TRANSACTION_MAX_MARKER_BYTES:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                "the revalidation transaction marker is unsafe or exceeds its size bound",
            )
        try:
            raw = path.read_bytes()
            marker = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                "the revalidation transaction marker is unreadable",
            ) from exc
        expected_keys = {"schema", "transaction_id", "operation", "created_at", "paper_key", "targets"}
        transaction_id = str(marker.get("transaction_id") or "") if isinstance(marker, dict) else ""
        paper_key = str(marker.get("paper_key") or "") if isinstance(marker, dict) else ""
        targets = marker.get("targets") if isinstance(marker, dict) else None
        if (
            not isinstance(marker, dict)
            or set(marker) != expected_keys
            or marker.get("schema") != _REVALIDATION_TRANSACTION_SCHEMA
            or marker.get("operation") != "revalidate_last_completed"
            or len(transaction_id) != 32
            or any(character not in "0123456789abcdef" for character in transaction_id)
            or not paper_key
            or len(paper_key) > 1024
            or not isinstance(marker.get("created_at"), str)
            or not marker["created_at"]
            or len(marker["created_at"]) > 128
            or not isinstance(targets, dict)
            or set(targets) != {"journal", "pipeline"}
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                "the revalidation transaction marker has an unsupported schema",
            )
        configured = {"journal": self.journal_path, "pipeline": self.pipeline_path}
        images: dict[str, tuple[bytes, bytes]] = {}
        for role, configured_path in configured.items():
            target = targets[role]
            if not isinstance(target, dict) or set(target) != {"path", "original", "intended"}:
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                    f"the {role} transaction target has an invalid schema",
                )
            stored_path = target.get("path")
            if (
                not isinstance(stored_path, str)
                or not Path(stored_path).is_absolute()
                or stored_path != str(configured_path)
            ):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_TRANSACTION_INVALID",
                    f"the {role} transaction target is not bound to the configured absolute path",
                )
            original = self._decode_revalidation_transaction_image(target["original"], label=f"{role} original")
            intended = self._decode_revalidation_transaction_image(target["intended"], label=f"{role} intended")
            images[role] = (original, intended)
        self._validate_revalidation_transaction_json(images, paper_key)
        return marker, raw, images

    def _assert_revalidation_transaction_absent(self) -> None:
        if self._revalidation_transaction_present():
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED",
                "an interrupted revalidation transaction must be recovered before this state can be written",
            )

    def _remove_revalidation_transaction(self, raw: bytes) -> None:
        if not unlink_file_durable_if_unchanged(
            self.revalidation_transaction_path,
            hashlib.sha256(raw).hexdigest(),
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED",
                "the exact revalidation transaction marker could not be durably removed",
            )

    def _recover_revalidation_transaction_locked(self) -> dict[str, Any]:
        loaded = self._read_revalidation_transaction()
        if loaded is None:
            return {"recovered": False, "status": "none"}
        marker, marker_raw, images = loaded
        states = {
            role: self._revalidation_file_state(
                self.journal_path if role == "journal" else self.pipeline_path,
                pair[0],
                pair[1],
            )
            for role, pair in images.items()
        }
        invalid_states = {role: state for role, state in states.items() if state not in {"original", "intended"}}
        if invalid_states:
            details = ", ".join(f"{role}={state}" for role, state in sorted(invalid_states.items()))
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_CONFLICT",
                f"the interrupted revalidation targets contain unknown bytes ({details}); marker retained",
            )
        paths = {"journal": self.journal_path, "pipeline": self.pipeline_path}
        for role in ("journal", "pipeline"):
            if states[role] == "intended":
                continue
            original, intended = images[role]
            self._require_legacy_quiescent()
            try:
                written = write_bytes_atomic_if_unchanged(
                    paths[role], intended, hashlib.sha256(original).hexdigest()
                )
            except Exception as exc:
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_RECOVERY_FAILED",
                    f"the {role} target could not be rolled forward; marker retained",
                ) from exc
            current_state = self._revalidation_file_state(paths[role], original, intended)
            if not written and current_state != "intended":
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_RECOVERY_CONFLICT",
                    f"the {role} target changed during roll-forward; marker retained",
                )
            if current_state != "intended":
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_RECOVERY_FAILED",
                    f"the {role} target failed exact intended-byte verification; marker retained",
                )
            states[role] = "intended"
            self._revalidation_fault(f"recovery_after_{role}_write")
        self._require_legacy_quiescent()
        final_states = {
            role: self._revalidation_file_state(paths[role], pair[0], pair[1]) for role, pair in images.items()
        }
        if set(final_states.values()) != {"intended"}:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_FAILED",
                "the interrupted revalidation did not converge to both exact intended images; marker retained",
            )
        self._revalidation_fault("recovery_before_marker_cleanup")
        self._remove_revalidation_transaction(marker_raw)
        self._revalidation_fault("recovery_after_marker_cleanup")
        return {
            "recovered": True,
            "status": "rolled_forward",
            "transaction_id": marker["transaction_id"],
            "paper_key": marker["paper_key"],
        }

    def _recover_revalidation_transaction_with_dispatch_lock(self) -> dict[str, Any]:
        if not self._revalidation_transaction_present():
            return {"recovered": False, "status": "none"}
        pipeline_lock = FileLock(self.pipeline_lock_path, stale_seconds=30 * 60)
        try:
            pipeline_lock.__enter__()
        except RuntimeError as exc:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_LOCKED",
                "the interrupted revalidation cannot be recovered while the pipeline writer lock is active",
            ) from exc
        try:
            return self._recover_revalidation_transaction_locked()
        finally:
            pipeline_lock.__exit__(None, None, None)

    def _recover_revalidation_transaction(self) -> dict[str, Any]:
        if not self._revalidation_transaction_present():
            return {"recovered": False, "status": "none"}
        self._require_legacy_quiescent()
        dispatch_lock = FileLock(self.dispatch_lock_path, stale_seconds=7 * 24 * 60 * 60)
        try:
            dispatch_lock.__enter__()
        except RuntimeError as exc:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_LOCKED",
                "the interrupted revalidation cannot be recovered while a writer lock is active",
            ) from exc
        try:
            return self._recover_revalidation_transaction_with_dispatch_lock()
        finally:
            dispatch_lock.__exit__(None, None, None)

    def assert_revalidation_transaction_resolved(self) -> None:
        """Fail closed when a pipeline writer encounters durable redo evidence.

        This read-only gate is safe to call while the cooperative pipeline lock
        is held.  Callers must not attempt recovery from that lock order.
        """

        self._assert_revalidation_transaction_absent()

    def recover_revalidation_transaction(self) -> dict[str, Any]:
        """Explicitly recover pending redo evidence in canonical lock order."""

        with self._background_lock:
            worker = self._background_thread
            if worker is not None and worker.is_alive():
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_RECOVERY_WHILE_RUNNING",
                    "stop the in-process writing SDK worker before transaction recovery",
                )
            return self._recover_revalidation_transaction()

    def prepare_pipeline_mutation(self) -> dict[str, Any]:
        """Resolve redo evidence before a caller acquires the pipeline lock."""

        if not self._revalidation_transaction_present():
            return {"recovered": False, "status": "none"}
        return self.recover_revalidation_transaction()

    def _dispatch_revalidation_guard(self) -> None:
        recovery = self._recover_revalidation_transaction_with_dispatch_lock()
        if recovery.get("recovered"):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERED_RETRY",
                "an interrupted revalidation was recovered after planning; retry dispatch from the coherent state",
            )

    def _revalidation_transaction_status(self) -> dict[str, Any]:
        if not self._revalidation_transaction_present():
            return {"pending": False, "status": "none", "blocker": ""}
        try:
            loaded = self._read_revalidation_transaction()
            if loaded is None:
                return {"pending": False, "status": "none", "blocker": ""}
            marker, _raw, images = loaded
            paths = {"journal": self.journal_path, "pipeline": self.pipeline_path}
            states = {
                role: self._revalidation_file_state(paths[role], pair[0], pair[1])
                for role, pair in images.items()
            }
            if any(state not in {"original", "intended"} for state in states.values()):
                return {
                    "pending": True,
                    "status": "failed",
                    "blocker": "WRITING_SDK_REVALIDATION_RECOVERY_CONFLICT",
                    "transaction_id": marker["transaction_id"],
                    "paper_key": marker["paper_key"],
                    "target_states": states,
                }
            return {
                "pending": True,
                "status": "pending",
                "blocker": "WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED",
                "transaction_id": marker["transaction_id"],
                "paper_key": marker["paper_key"],
                "target_states": states,
            }
        except WritingSDKRuntimeError as exc:
            return {
                "pending": True,
                "status": "failed",
                "blocker": exc.code,
            }

    @staticmethod
    def _revalidation_json_bytes(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _revalidation_original_bytes(
        path: Path,
        expected_sha256: str | None,
        *,
        changed_code: str,
        label: str,
    ) -> bytes:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise WritingSDKRuntimeError(
                changed_code,
                f"the {label} state cannot be snapshotted before the revalidation commit",
            ) from exc
        if expected_sha256 is None or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise WritingSDKRuntimeError(
                changed_code,
                f"the {label} state changed before the revalidation commit",
            )
        return raw

    @staticmethod
    def _revalidation_file_state(path: Path, original: bytes, intended: bytes) -> str:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            return f"unreadable:{type(exc).__name__}"
        if current == original:
            return "original"
        if current == intended:
            return "intended"
        return f"unexpected:{hashlib.sha256(current).hexdigest()[:12]}"

    @classmethod
    def _rollback_revalidation_commit(
        cls,
        *,
        journal_path: Path,
        journal_original: bytes,
        journal_intended: bytes,
        pipeline_path: Path,
        pipeline_original: bytes,
        pipeline_intended: bytes,
    ) -> None:
        """Restore both state files exactly after a failed paired commit."""

        restore_errors: list[str] = []
        targets = (
            ("pipeline", pipeline_path, pipeline_original, pipeline_intended),
            ("journal", journal_path, journal_original, journal_intended),
        )
        for label, path, original, intended in targets:
            state = cls._revalidation_file_state(path, original, intended)
            if state == "original":
                continue
            if state != "intended":
                restore_errors.append(f"{label}={state}")
                continue
            try:
                restored = write_bytes_atomic_if_unchanged(
                    path,
                    original,
                    hashlib.sha256(intended).hexdigest(),
                )
            except Exception as exc:
                restore_errors.append(f"{label}=restore_{type(exc).__name__}")
            else:
                if not restored:
                    restore_errors.append(f"{label}=restore_conflict")

        final_states = {
            "journal": cls._revalidation_file_state(journal_path, journal_original, journal_intended),
            "pipeline": cls._revalidation_file_state(pipeline_path, pipeline_original, pipeline_intended),
        }
        if set(final_states.values()) == {"original"}:
            return
        details = ", ".join(f"{label}={state}" for label, state in final_states.items())
        if restore_errors:
            details = f"{details}; restore_errors={';'.join(restore_errors)}"
        raise WritingSDKRuntimeError(
            "WRITING_SDK_REVALIDATE_FATAL_ROLLBACK_FAILED",
            "FATAL: the revalidation commit failed and exact-byte rollback could not be verified "
            f"({details}); no model was dispatched and the durable marker was retained for fail-closed roll-forward",
        )

    def _commit_revalidation_state_pair(
        self,
        *,
        paper_key: str,
        journal: dict[str, Any],
        journal_digest: str | None,
        pipeline: dict[str, Any],
        pipeline_digest: str | None,
    ) -> None:
        """Commit both files behind one durable redo marker.

        The caller holds dispatch then pipeline locks.  Ordinary exceptions
        retain the prior exact-byte compensation behavior.  A ``BaseException``
        or hard process death bypasses compensation and leaves the marker, so
        the next guarded entry rolls both targets forward idempotently.
        """

        journal_original = self._revalidation_original_bytes(
            self.journal_path,
            journal_digest,
            changed_code="WRITING_SDK_JOURNAL_CHANGED",
            label="writing SDK journal",
        )
        pipeline_original = self._revalidation_original_bytes(
            self.pipeline_path,
            pipeline_digest,
            changed_code="WRITING_PIPELINE_STATE_CHANGED",
            label="writing pipeline",
        )
        journal_intended = self._revalidation_json_bytes(journal)
        pipeline_intended = self._revalidation_json_bytes(pipeline)
        marker_raw = self._revalidation_transaction_bytes(
            paper_key=paper_key,
            journal_original=journal_original,
            journal_intended=journal_intended,
            pipeline_original=pipeline_original,
            pipeline_intended=pipeline_intended,
        )
        marker_created = False

        def rollback_and_raise(stage: str, cause: Exception | None = None) -> None:
            if marker_created:
                self._rollback_revalidation_commit(
                    journal_path=self.journal_path,
                    journal_original=journal_original,
                    journal_intended=journal_intended,
                    pipeline_path=self.pipeline_path,
                    pipeline_original=pipeline_original,
                    pipeline_intended=pipeline_intended,
                )
                # Removing redo evidence is permitted only after the rollback
                # helper has verified both exact original byte images.
                self._remove_revalidation_transaction(marker_raw)
            error = WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_COMMIT_ROLLED_BACK",
                f"the revalidation {stage} failed; both state files were restored byte-for-byte and no model was dispatched",
            )
            if cause is not None:
                raise error from cause
            raise error

        try:
            marker_created = write_bytes_atomic_if_unchanged(
                self.revalidation_transaction_path,
                marker_raw,
                None,
            )
        except Exception as exc:
            # A writer may report an error after its atomic replace.  Retire
            # only our exact marker, and only while both targets still match
            # their exact preimages; otherwise preserve evidence for recovery.
            try:
                marker_matches = self.revalidation_transaction_path.read_bytes() == marker_raw
            except OSError:
                marker_matches = False
            targets_original = (
                self._revalidation_file_state(self.journal_path, journal_original, journal_intended) == "original"
                and self._revalidation_file_state(self.pipeline_path, pipeline_original, pipeline_intended)
                == "original"
            )
            if marker_matches and targets_original:
                self._remove_revalidation_transaction(marker_raw)
            elif self._revalidation_transaction_present():
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED",
                    "transaction marker publication became uncertain; evidence was retained for recovery",
                ) from exc
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_TRANSACTION_WRITE_FAILED",
                "the durable revalidation transaction marker could not be written; no state file was changed",
            ) from exc
        if not marker_created:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATION_RECOVERY_REQUIRED",
                "existing revalidation transaction evidence must be recovered before a new commit",
            )
        try:
            marker_verified = self.revalidation_transaction_path.read_bytes() == marker_raw
            self._revalidation_fault("after_revalidation_marker_write")
        except Exception as exc:
            rollback_and_raise("transaction marker verification", exc)
        if not marker_verified:
            rollback_and_raise("transaction marker verification")

        try:
            journal_written = write_json_atomic_if_unchanged(self.journal_path, journal, journal_digest)
        except Exception as exc:
            rollback_and_raise("journal write", exc)
        if not journal_written:
            journal_state = self._revalidation_file_state(self.journal_path, journal_original, journal_intended)
            pipeline_state = self._revalidation_file_state(self.pipeline_path, pipeline_original, pipeline_intended)
            if journal_state == pipeline_state == "original":
                # This transaction never changed either target.  Retire only
                # its exact redo marker before reporting the original CAS
                # conflict, otherwise a later startup would incorrectly turn
                # an ordinary rejected write into a committed reopen.
                self._remove_revalidation_transaction(marker_raw)
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_JOURNAL_CHANGED",
                    "writing SDK journal changed before the revalidation reopen commit; no state was changed",
                )
            rollback_and_raise("journal compare-and-swap")
        if (
            self._revalidation_file_state(self.journal_path, journal_original, journal_intended) != "intended"
            or self._revalidation_file_state(self.pipeline_path, pipeline_original, pipeline_intended) != "original"
        ):
            rollback_and_raise("journal commit verification")
        try:
            self._revalidation_fault("after_revalidation_journal_write")
        except Exception as exc:
            rollback_and_raise("journal post-write checkpoint", exc)

        try:
            pipeline_written = write_json_atomic_if_unchanged(self.pipeline_path, pipeline, pipeline_digest)
        except Exception as exc:
            rollback_and_raise("pipeline write", exc)
        if not pipeline_written:
            rollback_and_raise("pipeline compare-and-swap")
        if (
            self._revalidation_file_state(self.journal_path, journal_original, journal_intended) != "intended"
            or self._revalidation_file_state(self.pipeline_path, pipeline_original, pipeline_intended) != "intended"
        ):
            rollback_and_raise("paired commit verification")
        try:
            self._revalidation_fault("after_revalidation_pipeline_write")
            self._revalidation_fault("before_revalidation_marker_cleanup")
        except Exception as exc:
            rollback_and_raise("paired post-write checkpoint", exc)
        self._remove_revalidation_transaction(marker_raw)
        self._revalidation_fault("after_revalidation_marker_cleanup")

    def _revalidate_last_completed_locked(self, paper_key: str) -> dict[str, Any]:
        journal, journal_digest = read_json_snapshot(
            self.journal_path,
            {"schema": WRITING_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []},
        )
        if (
            not isinstance(journal, dict)
            or journal.get("schema") != WRITING_SDK_JOURNAL_SCHEMA
            or not isinstance(journal.get("papers"), dict)
            or not isinstance(journal.get("history"), list)
        ):
            raise WritingSDKRuntimeError("WRITING_SDK_JOURNAL_INVALID", "writing SDK journal is invalid")
        record = journal["papers"].get(paper_key)
        if not isinstance(record, dict):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_PAPER_NOT_FOUND",
                "the exact SDK paper key is not present in the journal",
            )
        if str(record.get("paper_key") or paper_key) != paper_key:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_BINDING_INVALID",
                "the journal paper-key binding is inconsistent",
            )
        status = str(record.get("status") or "").strip().lower()
        if status not in {"complete", "queued", "paused", "waiting_event", "waiting_prerequisite"} or record.get(
            "active_step"
        ) not in {
            None,
            "",
        }:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_RECORD_ACTIVE",
                "only a quiescent record at a completed-step boundary can be revalidated",
            )
        step = _step_number(record.get("completed_step"))
        if step not in _SEMANTIC_REVALIDATION_STEPS:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_STEP_NOT_SEMANTIC",
                "the last completed step has no safe static-output semantic retry contract",
            )
        paper_id = str(record.get("paper_id") or "").strip()
        thread_id = str(record.get("thread_id") or "").strip()
        steps = record.get("steps")
        step_record = steps.get(str(step)) if isinstance(steps, dict) else None
        recorded_prompt_sha256 = (
            _normalized_sha256(step_record.get("prompt_sha256")) if isinstance(step_record, Mapping) else ""
        )
        if (
            not paper_id
            or not thread_id
            or not isinstance(step_record, dict)
            or str(step_record.get("state") or "") != "completed"
            or not str(step_record.get("turn_id") or "").strip()
            or not recorded_prompt_sha256
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_COMPLETION_EVIDENCE_MISSING",
                "the last completed step lacks fixed thread, turn, prompt, or state evidence",
            )
        recorded_artifacts = step_record.get("artifacts")
        contract_step = _record_contract_step(record, step)
        current_artifacts = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
        if (
            not isinstance(recorded_artifacts, list)
            or not recorded_artifacts
            or current_artifacts != recorded_artifacts
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_ARTIFACT_MISMATCH",
                "current output bytes do not match the completed step's stored artifact fingerprints",
            )

        superseded_steps: dict[str, Any] = {}
        assert isinstance(steps, dict)
        for raw_step, raw_value in steps.items():
            try:
                future_step = int(raw_step)
            except (TypeError, ValueError):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_JOURNAL_INVALID", "writing SDK journal contains an invalid step key"
                ) from None
            if future_step <= step:
                continue
            if (
                future_step != step + 1
                or not isinstance(raw_value, dict)
                or str(raw_value.get("state") or "") != "waiting_prerequisite"
                or str(raw_value.get("turn_id") or "")
            ):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATE_NOT_LAST_COMPLETED",
                    "a later step has non-derivative or turn evidence; the last completed step was not reopened",
                )
            superseded_steps[str(raw_step)] = copy.deepcopy(raw_value)
        if status in {"waiting_event", "waiting_prerequisite"} and (
            _step_number(record.get("waiting_before_step")) != step + 1 or not superseded_steps
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_WAIT_STATE_INVALID",
                "the recorded future wait is not derived solely from the last completed step",
            )

        pipeline, pipeline_digest = self._load_pipeline_snapshot()
        sessions = pipeline.get("sessions", {})
        session = sessions.get(paper_id) if isinstance(sessions, dict) else None
        if not isinstance(session, dict):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_PIPELINE_BINDING_MISSING",
                "the fixed SDK pipeline session is missing",
            )
        bound_threads = {
            str(session.get(field) or "").strip() for field in ("sdk_thread_id", "thread_id") if session.get(field)
        }
        if (
            (str(session.get("sdk_paper_key") or paper_key) != paper_key)
            or bound_threads != {thread_id}
            or _step_number(session.get("step_done")) != step
            or str(session.get("status") or "").strip().lower() in {"running", "starting"}
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_PIPELINE_BINDING_INVALID",
                "pipeline progress or fixed-thread ownership differs from the completed journal record",
            )

        terminal = CodexThreadCompletionReader(self.codex_home).inspect(thread_id)
        if (
            terminal.get("status") != "terminal"
            or str(terminal.get("thread_id") or "") != thread_id
            or str(terminal.get("turn_id") or "") != str(step_record["turn_id"])
            or str(terminal.get("pipeline_id") or "") != paper_id
            or _step_number(terminal.get("step_done")) != step
        ):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_TERMINAL_EVIDENCE_MISMATCH",
                "the latest fixed-thread terminal does not prove the stored completed step",
            )

        if is_undergraduate(record.get("workflow_snapshot")):
            semantic_result = validate_undergraduate_output(
                self.root, self.papers_dir, paper_id, contract_step, record["workflow_snapshot"],
                is_terminal=step >= _run_until_step(record.get("run_until_step")),
            )
        elif contract_step == 2:
            semantic_result = writing_step2_contract_validation(
                validate_writing_step2_contract(self.root, paper_id)
            )
        else:
            semantic_result = _validate_record_output(
                self.root,
                self.papers_dir,
                paper_id,
                contract_step,
                is_terminal=step >= _run_until_step(record.get("run_until_step")),
            )
        semantic_validation = self._semantic_revalidation_snapshot(semantic_result)
        after_validation = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
        if after_validation != current_artifacts:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_ARTIFACT_CHANGED",
                "the completed output changed during current semantic validation",
            )
        if semantic_validation["ready"]:
            return {
                "ok": True,
                "schema": WRITING_SDK_RUNTIME_SCHEMA,
                "status": "ready_no_change",
                "paper_key": paper_key,
                "step": step,
                "semantic_validation": semantic_validation,
                "dispatched": False,
                "changed": False,
            }
        issue_codes = {str(item.get("code") or "") for item in semantic_validation["issues"] if isinstance(item, dict)}
        if not issue_codes or issue_codes & _NON_SEMANTIC_REVALIDATION_ISSUES:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_NON_SEMANTIC_FAILURE",
                "only a proven current semantic-contract failure may reopen a completed step",
            )
        if step == 2 and writing_retry_validator_feedback(semantic_validation) is None:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_STEP2_REPAIR_NOT_SAFE",
                "the current Step 2 gate failure is factual, transient, unsafe, or outside the bounded repair allowlist",
            )
        if step == 2:
            if _step2_semantic_repair_attempt_count(record):
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_STEP2_REPAIR_LIMIT_REACHED",
                    "Step 2 already consumed its single bounded semantic repair attempt",
                )

        reopened_at = now_iso()
        validated_retry_baseline = self._validated_retry_baseline(
            step,
            current_artifacts,
            semantic_validation,
            validated_at=reopened_at,
        )
        attempts = record.setdefault("revalidation_attempts", [])
        if not isinstance(attempts, list):
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_PROVENANCE_INVALID",
                "existing revalidation provenance is malformed",
            )
        archive_index = len(attempts)
        attempts.append(
            {
                "at": reopened_at,
                "action": "current_validator_reopen_last_completed",
                "step": step,
                "from_completed_step": step,
                "to_completed_step": step - 1,
                "thread_id": thread_id,
                "turn_id": str(step_record["turn_id"]),
                "artifacts": copy.deepcopy(current_artifacts),
                "semantic_validation": copy.deepcopy(semantic_validation),
                "step_record": copy.deepcopy(step_record),
                "superseded_step_records": superseded_steps,
            }
        )
        for raw_step in superseded_steps:
            del steps[raw_step]
        baseline_artifacts = [{**copy.deepcopy(item), "state": "file", "contained": True} for item in current_artifacts]
        steps[str(step)] = {
            "state": "retry_ready",
            "requeued_at": reopened_at,
            "retry_reason": "WRITING_OUTPUT_SEMANTIC_INVALID",
            "retry_origin": "WRITING_OUTPUT_SEMANTIC_INVALID",
            "prompt_sha256": recorded_prompt_sha256,
            "output_baseline": {"mode": "static", "artifacts": baseline_artifacts},
            "semantic_validation": copy.deepcopy(semantic_validation),
            "validated_retry_baseline": validated_retry_baseline,
        }
        record["completed_step"] = step - 1
        record["status"] = "queued"
        record["updated_at"] = reopened_at
        record["revalidation_reopen"] = {
            "at": reopened_at,
            "step": step,
            "turn_id": str(step_record["turn_id"]),
            "archive_index": archive_index,
            "from_completed_step": step,
            "to_completed_step": step - 1,
            "issue_codes": sorted(issue_codes),
        }
        for field in (
            "active_step",
            "blocker",
            "blocker_message",
            "waiting_reason",
            "waiting_before_step",
            "paused_before_step",
            "paused_during_step",
            "prerequisite_gate",
        ):
            record.pop(field, None)
        journal["history"].append(
            {
                "at": reopened_at,
                "event": "paper_last_completed_step_reopened",
                "paper_key": paper_key,
                "paper_id": paper_id,
                "step": step,
                "from_completed_step": step,
                "to_completed_step": step - 1,
                "thread_id": thread_id,
                "turn_id": str(step_record["turn_id"]),
                "issue_codes": sorted(issue_codes),
            }
        )
        if len(journal["history"]) > 500:
            del journal["history"][:-500]
        journal["updated_at"] = reopened_at

        workflow = _workflow_snapshot(record.get("workflow_snapshot"))
        binding = _PlanBinding(
            paper_key=paper_key,
            pipeline_id=paper_id,
            command_ids=self._commands_for_key(pipeline, paper_key, paper_id),
            run_until_step=_run_until_step(record.get("run_until_step"), workflow_step_count(workflow)),
            workflow_snapshot=workflow,
        )
        self._merge_pipeline_snapshot(pipeline, binding, "revalidated_reopened", record)
        pipeline["updated_at"] = reopened_at
        contract_step = workflow_contract_step(workflow, step)
        if fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, contract_step) != current_artifacts:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_ARTIFACT_CHANGED",
                "the completed output changed before the reopen commit",
            )
        self._require_legacy_quiescent()
        self._commit_revalidation_state_pair(
            paper_key=paper_key,
            journal=journal,
            journal_digest=journal_digest,
            pipeline=pipeline,
            pipeline_digest=pipeline_digest,
        )
        return {
            "ok": True,
            "schema": WRITING_SDK_RUNTIME_SCHEMA,
            "status": "reopened",
            "paper_key": paper_key,
            "step": step,
            "from_completed_step": step,
            "to_completed_step": step - 1,
            "semantic_validation": semantic_validation,
            "dispatched": False,
            "changed": True,
            "pipeline_synchronized": True,
        }

    def revalidate_last_completed(self, paper_key: str) -> dict[str, Any]:
        """Revalidate and, only on semantic failure, reopen one exact last completed step."""

        paper_key = str(paper_key or "").strip()
        if not paper_key:
            raise WritingSDKRuntimeError(
                "WRITING_SDK_REVALIDATE_SELECTION_INVALID", "one exact non-empty paper key is required"
            )
        with self._background_lock:
            worker = self._background_thread
            if worker is not None and worker.is_alive():
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_REVALIDATE_WHILE_RUNNING",
                    "stop the in-process writing SDK worker before revalidation",
                )
            self._require_legacy_quiescent()
            dispatch_lock = self._enter_revalidation_lock(self.dispatch_lock_path, stale_seconds=7 * 24 * 60 * 60)
            try:
                pipeline_lock = self._enter_revalidation_lock(self.pipeline_lock_path, stale_seconds=30 * 60)
                try:
                    recovery = self._recover_revalidation_transaction_locked()
                    if recovery.get("recovered"):
                        result = {
                            "ok": True,
                            "schema": WRITING_SDK_RUNTIME_SCHEMA,
                            "status": "revalidation_transaction_recovered",
                            "paper_key": recovery.get("paper_key"),
                            "requested_paper_key": paper_key,
                            "transaction_id": recovery.get("transaction_id"),
                            "dispatched": False,
                            "changed": True,
                            "pipeline_synchronized": True,
                        }
                    else:
                        result = self._revalidate_last_completed_locked(paper_key)
                finally:
                    pipeline_lock.__exit__(None, None, None)
            finally:
                dispatch_lock.__exit__(None, None, None)
        self._last_result = result
        self._last_error = ""
        return result

    def _uncommitted_outputs_absent(
        self, record: Mapping[str, Any], step: int, step_record: Mapping[str, Any]
    ) -> tuple[bool, list[str]]:
        paper_id = str(record.get("paper_id") or "").strip()
        contract_step = _record_contract_step(record, step)
        outputs = expected_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
        if outputs:
            present = [str(path) for path in outputs if path.exists()]
            return not present, present
        if contract_step != 1 or paper_id:
            return False, ["writing output contract could not be resolved"]

        started_at = str(step_record.get("started_at") or "").strip()
        try:
            started = datetime.fromisoformat(started_at)
            cutoff = started.timestamp()
        except (ValueError, OSError, OverflowError):
            return False, ["failed Step 1 has no trustworthy started_at timestamp"]
        ideas_dir = self.root / "ideas"
        if not ideas_dir.exists():
            return True, []
        try:
            possible_outputs = [
                path for path in ideas_dir.glob("*.md") if path.is_file() and path.stat().st_mtime >= cutoff
            ]
        except OSError:
            return False, ["ideas directory could not be inspected safely"]
        return not possible_outputs, [str(path) for path in possible_outputs]

    @staticmethod
    def _static_retry_baseline_fingerprints(step_record: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        baseline = step_record.get("output_baseline")
        if not isinstance(baseline, Mapping) or baseline.get("mode") != "static":
            return None
        raw_artifacts = baseline.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            return None
        artifacts: list[dict[str, Any]] = []
        for raw in raw_artifacts:
            if (
                not isinstance(raw, Mapping)
                or raw.get("state") != "file"
                or raw.get("contained") is not True
                or not isinstance(raw.get("path"), str)
                or not isinstance(raw.get("sha256"), str)
                or len(raw["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in raw["sha256"].casefold())
                or not isinstance(raw.get("size"), int)
                or isinstance(raw.get("size"), bool)
                or raw["size"] <= 0
            ):
                return None
            artifacts.append(
                {
                    "path": raw["path"],
                    "sha256": raw["sha256"].casefold(),
                    "size": raw["size"],
                }
            )
        return artifacts

    @staticmethod
    def _validated_retry_baseline_fingerprints(
        step_record: Mapping[str, Any],
        step: int,
    ) -> list[dict[str, Any]] | None:
        baseline = step_record.get("validated_retry_baseline")
        if (
            not isinstance(baseline, Mapping)
            or baseline.get("schema") != _VALIDATED_RETRY_BASELINE_SCHEMA
            or _step_number(baseline.get("step")) != step
        ):
            return None
        raw_artifacts = baseline.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            return None
        artifacts: list[dict[str, Any]] = []
        for raw in raw_artifacts:
            digest = (
                str(raw.get("sha256") or "").casefold()
                if isinstance(raw, Mapping)
                else ""
            )
            if (
                not isinstance(raw, Mapping)
                or not isinstance(raw.get("path"), str)
                or not raw["path"]
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not isinstance(raw.get("size"), int)
                or isinstance(raw.get("size"), bool)
                or raw["size"] <= 0
            ):
                return None
            artifacts.append(
                {
                    "path": raw["path"],
                    "sha256": digest,
                    "size": raw["size"],
                }
            )
        return artifacts

    def _bind_proven_step1_semantic_retry_identity(
        self,
        record: dict[str, Any],
        step: int,
        step_record: Mapping[str, Any],
        *,
        recovered_at: str,
    ) -> str:
        """Recover a Step 1 paper id only from the completed Turn and exact artifact.

        The dispatcher validates the terminal markers, output freshness, and
        artifact contract before it evaluates semantic requirements.  When that
        last validation fails, the durable record historically retained the
        terminal Turn and semantic artifact path but not ``paper_id``.  That
        made a safe same-thread semantic repair look like an unbound failed
        Step 1 and incorrectly required replacing the thread.
        """

        if str(record.get("paper_id") or "").strip() or step != 1 or _record_contract_step(record, step) != 1:
            return ""
        thread_id = str(record.get("thread_id") or "").strip()
        turn_id = str(step_record.get("turn_id") or "").strip()
        if not thread_id or not turn_id:
            return "step1_semantic_retry_terminal_identity_missing"
        terminal = CodexThreadCompletionReader(self.codex_home).inspect(thread_id)
        pipeline_id = str(terminal.get("pipeline_id") or "").strip()
        if (
            terminal.get("status") != "terminal"
            or str(terminal.get("thread_id") or "") != thread_id
            or str(terminal.get("turn_id") or "") != turn_id
            or _step_number(terminal.get("step_done")) != 1
            or not pipeline_id
        ):
            return "step1_semantic_retry_terminal_evidence_mismatch"
        reserved_prefix = str(record.get("reserved_pipeline_prefix") or "").strip()
        if reserved_prefix and not pipeline_id.startswith(f"{reserved_prefix}_"):
            return "step1_semantic_retry_reserved_prefix_mismatch"

        outputs = expected_step_artifacts(self.root, self.papers_dir, pipeline_id, 1)
        if len(outputs) != 1:
            return "step1_semantic_retry_pipeline_identity_invalid"
        semantic_validation = step_record.get("semantic_validation")
        semantic_path = (
            str(semantic_validation.get("path") or "").strip()
            if isinstance(semantic_validation, Mapping)
            else ""
        )
        if not semantic_path:
            return "step1_semantic_retry_artifact_identity_missing"
        semantic_candidate = Path(semantic_path)
        if not semantic_candidate.is_absolute():
            semantic_candidate = self.root / semantic_candidate
        try:
            if semantic_candidate.resolve() != outputs[0].resolve():
                return "step1_semantic_retry_artifact_identity_mismatch"
        except OSError:
            return "step1_semantic_retry_artifact_identity_unreadable"

        baseline = step_record.get("output_baseline")
        if (
            not isinstance(baseline, Mapping)
            or baseline.get("mode") != "step1_dynamic"
            or baseline.get("contained") is not True
            or baseline.get("directory_state") == "unsafe"
            or baseline.get("unlisted_state") != "missing"
            or not isinstance(baseline.get("artifacts"), list)
        ):
            return "step1_semantic_retry_output_baseline_invalid"
        expected_relative = outputs[0].resolve().relative_to(self.root.resolve()).as_posix()
        if any(
            isinstance(item, Mapping)
            and str(item.get("path") or "").replace("\\", "/").casefold() == expected_relative.casefold()
            and item.get("state") != "missing"
            for item in baseline["artifacts"]
        ):
            return "step1_semantic_retry_output_was_preexisting"
        if fingerprint_step_artifacts(self.root, self.papers_dir, pipeline_id, 1) is None:
            return "step1_semantic_retry_artifact_missing_or_unsafe"

        record["paper_id"] = pipeline_id
        record["step1_identity_recovery"] = {
            "at": recovered_at,
            "source": "terminal_turn_and_semantic_artifact",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "paper_id": pipeline_id,
            "artifact": expected_relative,
        }
        return ""

    def _current_semantic_retry_evidence(
        self,
        record: Mapping[str, Any],
        step: int,
        step_record: Mapping[str, Any],
        *,
        require_attempt_baseline_match: bool,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, str]:
        retry_reason = str(step_record.get("retry_reason") or "")
        retry_origin = str(step_record.get("retry_origin") or retry_reason)
        if require_attempt_baseline_match and retry_origin != "WRITING_OUTPUT_SEMANTIC_INVALID":
            return None, None, "stale_attempt_is_not_a_semantic_repair_retry"
        prior_validation = step_record.get("semantic_validation")
        if not isinstance(prior_validation, Mapping):
            return None, None, "semantic_retry_has_no_retained_semantic_feedback"
        prior_feedback = writing_retry_validator_feedback(prior_validation)
        if prior_feedback is None:
            return None, None, "semantic_retry_feedback_is_not_allowlisted"
        paper_id = str(record.get("paper_id") or "").strip()
        contract_step = _record_contract_step(record, step)
        current_artifacts = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
        if not current_artifacts:
            return None, None, "semantic_retry_current_artifacts_are_missing_or_unsafe"
        if require_attempt_baseline_match:
            baseline_artifacts = self._static_retry_baseline_fingerprints(step_record)
            if baseline_artifacts is None:
                return None, None, "stale_attempt_has_no_safe_static_output_baseline"
            if current_artifacts != baseline_artifacts:
                return None, None, "stale_attempt_output_bytes_differ_from_the_recorded_baseline"

        if is_undergraduate(record.get("workflow_snapshot")):
            current_result = validate_undergraduate_output(
                self.root, self.papers_dir, paper_id, contract_step, record["workflow_snapshot"],
                is_terminal=step >= _run_until_step(record.get("run_until_step")),
            )
        elif contract_step == 2:
            current_result = writing_step2_contract_validation(
                validate_writing_step2_contract(self.root, paper_id)
            )
        else:
            current_result = _validate_record_output(
                self.root,
                self.papers_dir,
                paper_id,
                contract_step,
                is_terminal=step >= _run_until_step(record.get("run_until_step")),
            )
        try:
            current_validation = self._semantic_revalidation_snapshot(current_result)
        except WritingSDKRuntimeError:
            return None, None, "semantic_retry_current_validation_is_malformed"
        after_validation = fingerprint_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
        if after_validation != current_artifacts:
            return None, None, "semantic_retry_output_changed_during_validation"
        current_feedback = writing_retry_validator_feedback(current_validation)
        if current_feedback is None:
            if current_validation.get("ready") is True:
                return current_validation, current_artifacts, "semantic_retry_output_is_currently_ready"
            return None, None, "semantic_retry_has_no_current_allowlisted_failure"
        if current_feedback != prior_feedback:
            return None, None, "semantic_retry_validator_feedback_drifted"
        return current_validation, current_artifacts, ""

    def _promote_policy_ready_semantic_block(
        self,
        record: dict[str, Any],
        step: int,
        step_record: dict[str, Any],
        semantic_validation: Mapping[str, Any],
        artifacts: list[dict[str, Any]],
        *,
        reconciled_at: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Commit a proven completed turn whose current artifacts now validate.

        This covers both the deferred-marker policy migration and an operator
        correction (or validator false-positive fix) applied after a terminal
        turn.  The existing terminal Turn, exact paper/step identity, and the
        current artifact bytes are all revalidated before advancing, so no
        duplicate model Turn is needed.
        """

        if step not in set(range(2, 11)):
            return None, "semantic_policy_promotion_step_not_allowed"
        prior_validation = step_record.get("semantic_validation")
        if not isinstance(prior_validation, Mapping) or prior_validation.get("ready") is not False:
            return None, "semantic_policy_promotion_prior_validation_missing"
        prior_issues = prior_validation.get("issues")
        if not isinstance(prior_issues, list) or not prior_issues:
            return None, "semantic_policy_promotion_prior_issues_missing"
        prior_codes = {
            str(item.get("code") or "")
            for item in prior_issues
            if isinstance(item, Mapping) and str(item.get("code") or "")
        }
        deferred_marker_policy = step in {6, 7, 8, 9} and prior_codes == {
            "latex_forbidden_marker_remaining"
        }
        current_issues = semantic_validation.get("issues")
        if (
            semantic_validation.get("ready") is not True
            or not isinstance(current_issues, list)
            or current_issues
        ):
            return None, "semantic_policy_promotion_current_output_not_ready"
        warnings = semantic_validation.get("warnings")
        if not isinstance(warnings, list):
            return None, "semantic_policy_promotion_current_warnings_invalid"
        if deferred_marker_policy:
            warning_codes = {
                str(item.get("code") or "")
                for item in warnings
                if isinstance(item, Mapping) and str(item.get("code") or "")
            }
            if warning_codes - {"latex_forbidden_marker_deferred"}:
                return None, "semantic_policy_promotion_current_warning_not_deferred_cleanup"
        thread_id = str(record.get("thread_id") or "").strip()
        turn_id = str(step_record.get("turn_id") or "").strip()
        paper_id = str(record.get("paper_id") or "").strip()
        if not thread_id or not turn_id or not paper_id or not artifacts:
            return None, "semantic_policy_promotion_completion_evidence_missing"
        terminal = CodexThreadCompletionReader(self.codex_home).inspect(thread_id)
        if (
            terminal.get("status") != "terminal"
            or str(terminal.get("thread_id") or "") != thread_id
            or str(terminal.get("turn_id") or "") != turn_id
            or str(terminal.get("pipeline_id") or "") != paper_id
            or _step_number(terminal.get("step_done")) != step
        ):
            return None, "semantic_policy_promotion_terminal_evidence_mismatch"

        prior_step_record = copy.deepcopy(step_record)
        reconciliation = (
            "deferred_marker_cleanup_until_step_10"
            if deferred_marker_policy
            else "current_artifacts_revalidated_without_duplicate_turn"
        )
        action = (
            "promote_marker_only_semantic_block_without_ai"
            if deferred_marker_policy
            else "promote_revalidated_semantic_block_without_ai"
        )
        result_reason = (
            "policy_reconciled_without_ai"
            if deferred_marker_policy
            else "semantic_reconciled_without_ai"
        )
        step_record.update(
            {
                "state": "completed",
                "artifacts": copy.deepcopy(artifacts),
                "semantic_validation": copy.deepcopy(dict(semantic_validation)),
                "completed_at": str(step_record.get("blocked_at") or reconciled_at),
                "updated_at": reconciled_at,
                "policy_reconciled_at": reconciled_at,
                "policy_reconciliation": reconciliation,
            }
        )
        step_record.pop("blocker", None)
        step_record.pop("blocker_message", None)
        attempts = record.setdefault("policy_reconciliations", [])
        if not isinstance(attempts, list):
            return None, "semantic_policy_promotion_provenance_invalid"
        attempts.append(
            {
                "at": reconciled_at,
                "action": action,
                "step": step,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "artifacts": copy.deepcopy(artifacts),
                "prior_step_record": prior_step_record,
                "semantic_validation": copy.deepcopy(dict(semantic_validation)),
            }
        )
        run_until_step = _run_until_step(record.get("run_until_step"))
        record["completed_step"] = step
        record["status"] = "complete" if step >= run_until_step else "queued"
        record["updated_at"] = reconciled_at
        record["recovery_requeue"] = {
            "at": reconciled_at,
            "from_status": "blocked",
            "blocker": "WRITING_OUTPUT_SEMANTIC_INVALID",
            "next_step": min(step + 1, run_until_step),
            "thread_replaced": False,
            "mode": result_reason,
            "reconciled_step": step,
        }
        for field in ("active_step", "blocker", "blocker_message"):
            record.pop(field, None)
        return record, result_reason

    def _prepare_recoverable_requeue(
        self,
        paper_key: str,
        source: Mapping[str, Any],
        *,
        replace_failed_step1_threads: bool,
        archived_thread_ids: set[str],
        requeued_at: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        record = copy.deepcopy(dict(source))
        status = str(record.get("status") or "").strip().lower()
        blocker = str(record.get("blocker") or "").strip()
        completed_step = _step_number(record.get("completed_step"))
        next_step = completed_step + 1
        detail: dict[str, Any] = {
            "paper_key": paper_key,
            "previous_status": status,
            "blocker": blocker,
            "completed_step": completed_step,
            "next_step": next_step,
            "thread_id": str(record.get("thread_id") or ""),
        }
        recovery = record.get("recovery_requeue")
        if status == "queued" and isinstance(recovery, dict):
            detail["reason"] = "already_requeued"
            return record, detail
        if status in _DISPATCH_WAITING_STATUSES:
            if blocker not in _RECOVERABLE_WAITING_BLOCKERS:
                detail["reason"] = "waiting_blocker_not_allowlisted"
                return None, detail
            if record.get("active_step") not in {None, ""}:
                detail["reason"] = "waiting_record_has_an_active_step"
                return None, detail
            steps = record.get("steps", {})
            if not isinstance(steps, dict):
                detail["reason"] = "invalid_steps_record"
                return None, detail
            pending = steps.get(str(next_step))
            if isinstance(pending, dict) and str(pending.get("state") or "") in {
                "starting",
                "running",
                "recovery_pending",
                "send_uncertain",
            }:
                detail["reason"] = "waiting_record_has_an_uncertain_turn"
                return None, detail
            record["status"] = "queued"
            record["recovery_requeue"] = {
                "at": requeued_at,
                "from_status": status,
                "blocker": blocker,
                "next_step": next_step,
                "thread_replaced": False,
            }
            record["updated_at"] = requeued_at
            record.pop("blocker", None)
            record.pop("blocker_message", None)
            detail["reason"] = "requeued"
            return record, detail
        if status != "blocked":
            detail["reason"] = "status_not_recoverable"
            return None, detail
        if blocker == "WRITING_RETRY_PROMPT_HASH_MISMATCH":
            steps = record.get("steps", {})
            step_record = steps.get(str(next_step)) if isinstance(steps, dict) else None
            if (
                completed_step != 0
                or next_step != 1
                or not isinstance(step_record, dict)
                or str(step_record.get("state") or "") != "blocked"
                or not _step1_prompt_was_sent_unbound(record)
            ):
                detail["reason"] = "prompt_binding_recovery_not_proven"
                return None, detail
            if (
                str(step_record.get("turn_id") or "").strip()
                or _normalized_sha256(step_record.get("execution_prompt_sha256"))
                or isinstance(step_record.get("retry_execution_envelope"), Mapping)
            ):
                detail["reason"] = "prompt_binding_recovery_send_state_uncertain"
                return None, detail
            if (
                str(step_record.get("retry_reason") or "") != "WRITING_OUTPUT_SEMANTIC_INVALID"
                or str(step_record.get("retry_origin") or "") != "WRITING_OUTPUT_SEMANTIC_INVALID"
            ):
                detail["reason"] = "prompt_binding_recovery_retry_origin_invalid"
                return None, detail
            workflow = _workflow_snapshot(record.get("workflow_snapshot"))
            try:
                expected_prompt = existing_writing_prompt_provider(
                    self.prompt_path,
                    paper_key,
                    workflow,
                    reserved_pipeline_prefix=str(record.get("reserved_pipeline_prefix") or ""),
                    preserve_unbound_step1_owner=True,
                )(1, str(record.get("paper_id") or ""))
                expected_prompt.validate()
            except Exception:
                detail["reason"] = "prompt_binding_recovery_base_prompt_unavailable"
                return None, detail
            if _normalized_sha256(step_record.get("prompt_sha256")) != expected_prompt.sha256:
                detail["reason"] = "prompt_binding_recovery_base_prompt_mismatch"
                return None, detail
            retry_validation, _retry_artifacts, validation_problem = self._current_semantic_retry_evidence(
                record,
                1,
                step_record,
                require_attempt_baseline_match=False,
            )
            if retry_validation is None or validation_problem:
                detail["reason"] = validation_problem or "prompt_binding_recovery_semantic_evidence_invalid"
                return None, detail
            step_record["state"] = "retry_ready"
            step_record["updated_at"] = requeued_at
            step_record.pop("blocker", None)
            step_record.pop("blocker_message", None)
            record["status"] = "queued"
            record["recovery_requeue"] = {
                "at": requeued_at,
                "from_status": status,
                "blocker": blocker,
                "next_step": 1,
                "thread_replaced": False,
                "mode": "reconciled_unbound_step1_prompt_owner",
            }
            record["updated_at"] = requeued_at
            record.pop("active_step", None)
            record.pop("blocker", None)
            record.pop("blocker_message", None)
            detail["reason"] = "requeued_prompt_binding_reconciled"
            return record, detail
        if blocker == "WRITING_RETRY_BASELINE_DRIFT":
            steps = record.get("steps", {})
            step_record = steps.get(str(next_step)) if isinstance(steps, dict) else None
            identity = record.get("step1_identity_recovery")
            if (
                completed_step != 0
                or next_step != 1
                or not isinstance(step_record, dict)
                or str(step_record.get("state") or "") != "blocked"
                or not isinstance(identity, Mapping)
                or not _step1_prompt_was_sent_unbound(record)
            ):
                detail["reason"] = "step1_retry_baseline_recovery_not_proven"
                return None, detail
            if str(step_record.get("turn_id") or "").strip():
                detail["reason"] = "step1_retry_baseline_recovery_turn_was_sent"
                return None, detail
            retry_envelope = step_record.get("retry_execution_envelope")
            if (
                not isinstance(retry_envelope, Mapping)
                or retry_envelope.get("schema")
                != "unified-paper-orchestrator/writing-retry-execution-envelope/v1"
                or _step_number(retry_envelope.get("step")) != 1
                or str(step_record.get("retry_origin") or "")
                != "WRITING_OUTPUT_SEMANTIC_INVALID"
            ):
                detail["reason"] = "step1_retry_baseline_recovery_intent_invalid"
                return None, detail
            thread_id = str(record.get("thread_id") or "").strip()
            original_turn_id = str(identity.get("turn_id") or "").strip()
            paper_id = str(record.get("paper_id") or "").strip()
            terminal = CodexThreadCompletionReader(self.codex_home).inspect(thread_id)
            if (
                not thread_id
                or not original_turn_id
                or not paper_id
                or terminal.get("status") != "terminal"
                or str(terminal.get("thread_id") or "") != thread_id
                or str(terminal.get("turn_id") or "") != original_turn_id
                or str(terminal.get("pipeline_id") or "") != paper_id
                or _step_number(terminal.get("step_done")) != 1
            ):
                detail["reason"] = "step1_retry_baseline_recovery_terminal_mismatch"
                return None, detail
            workflow = _workflow_snapshot(record.get("workflow_snapshot"))
            try:
                expected_prompt = existing_writing_prompt_provider(
                    self.prompt_path,
                    paper_key,
                    workflow,
                    reserved_pipeline_prefix=str(
                        record.get("reserved_pipeline_prefix") or ""
                    ),
                    preserve_unbound_step1_owner=True,
                )(1, paper_id)
                expected_prompt.validate()
            except Exception:
                detail["reason"] = "step1_retry_baseline_recovery_prompt_unavailable"
                return None, detail
            prompt_sha256 = _normalized_sha256(step_record.get("prompt_sha256"))
            if (
                not prompt_sha256
                or prompt_sha256 != expected_prompt.sha256
                or _normalized_sha256(
                    retry_envelope.get("base_prompt_sha256")
                )
                != prompt_sha256
            ):
                detail["reason"] = "step1_retry_baseline_recovery_prompt_mismatch"
                return None, detail
            current_validation, current_artifacts, validation_problem = (
                self._current_semantic_retry_evidence(
                    record,
                    1,
                    step_record,
                    require_attempt_baseline_match=False,
                )
            )
            validated_artifacts = self._validated_retry_baseline_fingerprints(
                step_record,
                1,
            )
            if (
                current_validation is None
                or current_artifacts is None
                or validation_problem
                or validated_artifacts is None
                or current_artifacts != validated_artifacts
            ):
                detail["reason"] = (
                    validation_problem
                    or "step1_retry_baseline_recovery_artifact_mismatch"
                )
                return None, detail
            attempts = record.setdefault("recovery_attempts", [])
            if not isinstance(attempts, list):
                detail["reason"] = "invalid_recovery_attempts_record"
                return None, detail
            attempts.append(
                {
                    "at": requeued_at,
                    "action": "reconcile_step1_dynamic_retry_baseline_without_turn",
                    "step": 1,
                    "blocker": blocker,
                    "thread_id": thread_id,
                    "terminal_turn_id": original_turn_id,
                    "step_record": copy.deepcopy(step_record),
                }
            )
            steps["1"] = {
                "state": "retry_ready",
                "requeued_at": requeued_at,
                "retry_reason": "WRITING_OUTPUT_SEMANTIC_INVALID",
                "retry_origin": "WRITING_OUTPUT_SEMANTIC_INVALID",
                "semantic_validation": copy.deepcopy(
                    step_record["semantic_validation"]
                ),
                "validated_retry_baseline": copy.deepcopy(
                    step_record["validated_retry_baseline"]
                ),
                "prompt_sha256": prompt_sha256,
            }
            record["status"] = "queued"
            record["recovery_requeue"] = {
                "at": requeued_at,
                "from_status": status,
                "blocker": blocker,
                "next_step": 1,
                "thread_replaced": False,
                "mode": "reconciled_step1_static_retry_baseline",
            }
            record["updated_at"] = requeued_at
            record.pop("active_step", None)
            record.pop("blocker", None)
            record.pop("blocker_message", None)
            detail["reason"] = "requeued_step1_retry_baseline_reconciled"
            return record, detail
        if blocker not in _RECOVERABLE_FAILED_BLOCKERS:
            detail["reason"] = "blocked_reason_not_allowlisted"
            return None, detail
        if (
            next_step == 2
            and blocker in _RECOVERABLE_FAILED_BLOCKERS_WITH_OUTPUT
            and _step2_semantic_repair_attempt_count(record)
        ):
            detail["reason"] = "step2_semantic_retry_limit_reached"
            return None, detail

        steps = record.get("steps", {})
        if not isinstance(steps, dict):
            detail["reason"] = "invalid_steps_record"
            return None, detail
        step_record = steps.get(str(next_step))
        if not isinstance(step_record, dict):
            detail["reason"] = "failed_step_record_missing"
            return None, detail
        if str(step_record.get("state") or "") not in {"blocked", "running"} or not str(
            step_record.get("turn_id") or ""
        ):
            detail["reason"] = "failed_step_is_not_a_proven_completed_turn"
            return None, detail
        active_step = _step_number(record.get("active_step"))
        step_state = str(step_record.get("state") or "")
        if active_step not in {0, next_step} or (step_state == "running" and active_step != next_step):
            detail["reason"] = "active_step_mismatch"
            return None, detail
        retry_semantic_validation: dict[str, Any] | None = None
        retry_artifacts: list[dict[str, Any]] | None = None
        semantic_prompt_sha256 = ""
        if blocker in _RECOVERABLE_FAILED_BLOCKERS_WITH_OUTPUT:
            identity_problem = self._bind_proven_step1_semantic_retry_identity(
                record,
                next_step,
                step_record,
                recovered_at=requeued_at,
            )
            if identity_problem:
                detail["reason"] = identity_problem
                return None, detail
            recovered_paper_id = str(record.get("paper_id") or "").strip()
            if completed_step == 0 and next_step == 1 and recovered_paper_id:
                detail["recovered_paper_id"] = recovered_paper_id
            semantic_prompt_sha256 = _normalized_sha256(step_record.get("prompt_sha256"))
            if not semantic_prompt_sha256:
                detail["reason"] = "semantic_retry_prompt_sha256_invalid"
                return None, detail
            retry_semantic_validation, retry_artifacts, validation_problem = self._current_semantic_retry_evidence(
                record,
                next_step,
                step_record,
                require_attempt_baseline_match=blocker == "WRITING_EXPECTED_ARTIFACT_STALE",
            )
            if retry_semantic_validation is None:
                detail["reason"] = validation_problem
                return None, detail
            if validation_problem == "semantic_retry_output_is_currently_ready":
                assert retry_artifacts is not None
                promoted, promotion_reason = self._promote_policy_ready_semantic_block(
                    record,
                    next_step,
                    step_record,
                    retry_semantic_validation,
                    retry_artifacts,
                    reconciled_at=requeued_at,
                )
                detail["reason"] = promotion_reason
                if promoted is None:
                    return None, detail
                detail["reconciled_step"] = next_step
                detail["next_step"] = min(next_step + 1, _run_until_step(record.get("run_until_step")))
                detail["without_ai"] = True
                return promoted, detail
        else:
            outputs_absent, conflicts = self._uncommitted_outputs_absent(record, next_step, step_record)
            if not outputs_absent:
                detail["reason"] = "expected_output_present_or_unverifiable"
                detail["conflicts"] = conflicts
                return None, detail

        old_thread_id = str(record.get("thread_id") or "").strip()
        replace_thread = completed_step == 0 and next_step == 1 and not str(record.get("paper_id") or "").strip()
        if replace_thread:
            detail["old_thread_id"] = old_thread_id
            if blocker != "WRITING_FINAL_MARKERS_INVALID" or not old_thread_id:
                detail["reason"] = "failed_step1_replacement_not_proven_safe"
                return None, detail
            if not replace_failed_step1_threads:
                detail["reason"] = "failed_step1_thread_replacement_required"
                return None, detail
            if old_thread_id not in archived_thread_ids:
                detail["reason"] = "failed_step1_thread_archive_acknowledgement_required"
                return None, detail

        attempts = record.setdefault("recovery_attempts", [])
        if not isinstance(attempts, list):
            detail["reason"] = "invalid_recovery_attempts_record"
            return None, detail
        attempts.append(
            {
                "at": requeued_at,
                "action": "explicit_recoverable_requeue",
                "step": next_step,
                "blocker": blocker,
                "blocker_message": str(record.get("blocker_message") or ""),
                "thread_id": old_thread_id,
                "step_record": copy.deepcopy(step_record),
            }
        )
        retry_step: dict[str, Any] = {
            "state": "retry_ready",
            "requeued_at": requeued_at,
            "retry_reason": blocker,
        }
        if retry_semantic_validation is not None:
            assert retry_artifacts is not None
            validated_retry_baseline = self._validated_retry_baseline(
                next_step,
                retry_artifacts,
                retry_semantic_validation,
                validated_at=requeued_at,
            )
            retry_step.update(
                {
                    "retry_origin": "WRITING_OUTPUT_SEMANTIC_INVALID",
                    "semantic_validation": copy.deepcopy(retry_semantic_validation),
                    "validated_retry_baseline": validated_retry_baseline,
                }
            )
            prior_baseline = step_record.get("output_baseline")
            if isinstance(prior_baseline, dict):
                retry_step["output_baseline"] = copy.deepcopy(prior_baseline)
            safe_feedback = writing_retry_validator_feedback(retry_semantic_validation)
            assert safe_feedback is not None
            issue_codes = [str(item["code"]) for item in safe_feedback["issues"]]
            detail["retry_feedback_issue_codes"] = issue_codes
        prompt_sha256 = semantic_prompt_sha256 or str(step_record.get("prompt_sha256") or "")
        if prompt_sha256:
            retry_step["prompt_sha256"] = prompt_sha256
        steps[str(next_step)] = retry_step
        record["status"] = "queued"
        record["recovery_requeue"] = {
            "at": requeued_at,
            "from_status": status,
            "blocker": blocker,
            "next_step": next_step,
            "thread_replaced": replace_thread,
        }
        if retry_semantic_validation is not None:
            record["recovery_requeue"]["retry_feedback_issue_codes"] = detail[
                "retry_feedback_issue_codes"
            ]
        record["updated_at"] = requeued_at
        record.pop("active_step", None)
        record.pop("blocker", None)
        record.pop("blocker_message", None)
        if replace_thread:
            detail["replaced_thread_id"] = old_thread_id
            record.pop("thread_id", None)
            record.pop("thread_name", None)
            record.pop("migration_new_thread", None)
        detail["reason"] = "requeued"
        return record, detail

    def _synchronize_recovery_records(
        self,
        records: Mapping[str, Mapping[str, Any]],
        replacements: Mapping[str, str],
        requeued_keys: set[str],
    ) -> None:
        if not records:
            return
        pipeline = self._load_pipeline()
        bindings = {
            paper_key: _PlanBinding(
                paper_key=paper_key,
                pipeline_id=str(record.get("paper_id") or ""),
                command_ids=self._commands_for_key(pipeline, paper_key, str(record.get("paper_id") or "")),
                run_until_step=_run_until_step(
                    record.get("run_until_step"),
                    workflow_step_count(_workflow_snapshot(record.get("workflow_snapshot"))),
                ),
                workflow_snapshot=_workflow_snapshot(record.get("workflow_snapshot")),
                reserved_pipeline_prefix=str(record.get("reserved_pipeline_prefix") or "").strip(),
            )
            for paper_key, record in records.items()
        }

        def merge(state: dict[str, Any]) -> None:
            for paper_key in sorted(records, key=str.casefold):
                snapshot = dict(records[paper_key])
                replaced_thread_id = str(replacements.get(paper_key) or "")
                if replaced_thread_id:
                    snapshot["replaced_thread_id"] = replaced_thread_id
                event = (
                    "paper_result"
                    if str(snapshot.get("status") or "").strip().lower() == "complete"
                    else ("requeued" if paper_key in requeued_keys else "paper_result")
                )
                self._merge_pipeline_snapshot(
                    state,
                    bindings[paper_key],
                    event,
                    snapshot,
                )

        self._mutate_pipeline(merge)

    def requeue_recoverable(
        self,
        paper_keys: list[str] | None = None,
        *,
        replace_failed_step1_threads: bool = False,
        archived_thread_ids: list[str] | None = None,
        _allow_current_background_worker: bool = False,
    ) -> dict[str, Any]:
        """Explicitly requeue only proven-safe failed or capacity-waiting records.

        Failed pre-paper Step 1 records require replacement threads because
        their stale context may propose the same paper id.  The caller must
        first archive those threads and acknowledge their exact ids via
        ``archived_thread_ids``; this runtime never claims that external archive
        action happened atomically.
        """

        requested = None
        if paper_keys is not None:
            requested = tuple(dict.fromkeys(str(value).strip() for value in paper_keys))
            if any(not value for value in requested):
                raise WritingSDKRuntimeError("WRITING_SDK_RECOVERY_SELECTION_INVALID", "paper keys must be non-empty")
        archived = {str(value).strip() for value in (archived_thread_ids or []) if str(value).strip()}
        with self._background_lock:
            worker = self._background_thread
            current_worker_is_owner = (
                _allow_current_background_worker
                and worker is threading.current_thread()
                and self._background_owner is self
            )
            if worker is not None and worker.is_alive() and not current_worker_is_owner:
                raise WritingSDKRuntimeError(
                    "WRITING_SDK_RECOVERY_WHILE_RUNNING",
                    "stop the in-process writing SDK worker before requeueing recovery records",
                )
            self._require_legacy_quiescent()
            with FileLock(self.dispatch_lock_path, stale_seconds=7 * 24 * 60 * 60):
                self._recover_revalidation_transaction_with_dispatch_lock()
                for attempt in range(_PIPELINE_WRITE_ATTEMPTS):
                    state, digest = read_json_snapshot(
                        self.journal_path,
                        {"schema": WRITING_SDK_JOURNAL_SCHEMA, "papers": {}, "history": []},
                    )
                    if (
                        not isinstance(state, dict)
                        or state.get("schema") != WRITING_SDK_JOURNAL_SCHEMA
                        or not isinstance(state.get("papers", {}), dict)
                        or not isinstance(state.get("history", []), list)
                    ):
                        raise WritingSDKRuntimeError(
                            "WRITING_SDK_JOURNAL_INVALID",
                            "writing SDK journal cannot be recovered safely",
                        )
                    papers = state["papers"]
                    if requested is None:
                        selected = [
                            str(key)
                            for key, value in papers.items()
                            if isinstance(value, dict)
                            and (
                                str(value.get("status") or "").strip().lower()
                                in {"blocked", *_DISPATCH_WAITING_STATUSES}
                                or (
                                    str(value.get("status") or "").strip().lower() == "queued"
                                    and isinstance(value.get("recovery_requeue"), dict)
                                )
                            )
                        ]
                    else:
                        selected = list(requested)
                    requeued_at = now_iso()
                    changed: list[dict[str, Any]] = []
                    already_requeued: list[dict[str, Any]] = []
                    skipped: list[dict[str, Any]] = []
                    sync_records: dict[str, dict[str, Any]] = {}
                    replacements: dict[str, str] = {}
                    requeued_keys: set[str] = set()
                    old_thread_ids: list[str] = []
                    required_archive_thread_ids: list[str] = []
                    for paper_key in sorted(selected, key=str.casefold):
                        raw_record = papers.get(paper_key)
                        if not isinstance(raw_record, dict):
                            skipped.append({"paper_key": paper_key, "reason": "paper_not_found"})
                            continue
                        prepared, detail = self._prepare_recoverable_requeue(
                            paper_key,
                            raw_record,
                            replace_failed_step1_threads=replace_failed_step1_threads,
                            archived_thread_ids=archived,
                            requeued_at=requeued_at,
                        )
                        old_thread_id = str(detail.get("old_thread_id") or "")
                        if old_thread_id and old_thread_id not in old_thread_ids:
                            old_thread_ids.append(old_thread_id)
                        if detail.get("reason") in {
                            "failed_step1_thread_replacement_required",
                            "failed_step1_thread_archive_acknowledgement_required",
                        }:
                            required_archive_thread_ids.append(old_thread_id)
                        if prepared is None:
                            skipped.append(detail)
                            sync_records[paper_key] = dict(raw_record)
                            continue
                        if detail.get("reason") == "already_requeued":
                            already_requeued.append(detail)
                            sync_records[paper_key] = dict(prepared)
                            requeued_keys.add(paper_key)
                            recovery = prepared.get("recovery_requeue", {})
                            if isinstance(recovery, dict) and recovery.get("thread_replaced"):
                                for prior in prepared.get("recovery_attempts", []):
                                    if isinstance(prior, dict) and str(prior.get("thread_id") or ""):
                                        replacements[paper_key] = str(prior["thread_id"])
                                        break
                            continue
                        papers[paper_key] = prepared
                        changed.append(detail)
                        sync_records[paper_key] = dict(prepared)
                        requeued_keys.add(paper_key)
                        replaced = str(detail.get("replaced_thread_id") or "")
                        if replaced:
                            replacements[paper_key] = replaced
                        state["history"].append(
                            {
                                "at": requeued_at,
                                "event": "paper_requeued_recoverable",
                                "paper_key": paper_key,
                                "previous_status": detail["previous_status"],
                                "blocker": detail["blocker"],
                                "next_step": detail["next_step"],
                                "replaced_thread_id": replaced,
                                "recovery_mode": str(detail.get("reason") or "requeued"),
                                "reconciled_step": detail.get("reconciled_step"),
                                "without_ai": bool(detail.get("without_ai")),
                            }
                        )
                    if changed:
                        state["updated_at"] = requeued_at
                        self._require_legacy_quiescent()
                        if not write_json_atomic_if_unchanged(self.journal_path, state, digest):
                            if attempt + 1 < _PIPELINE_WRITE_ATTEMPTS:
                                continue
                            raise WritingSDKRuntimeError(
                                "WRITING_SDK_JOURNAL_CHANGED",
                                "writing SDK journal changed repeatedly during recovery",
                            )
                    self._synchronize_recovery_records(sync_records, replacements, requeued_keys)
                    break

        if changed:
            self._mark_unified_projection_dirty()
            self._refresh_unified_projection_after_step()
        if changed and required_archive_thread_ids:
            result_status = "partial_requeue_archive_required"
        elif changed:
            result_status = "requeued"
        elif required_archive_thread_ids:
            result_status = "archive_required"
        else:
            result_status = "no_change"
        result = {
            "ok": not required_archive_thread_ids,
            "schema": WRITING_SDK_RUNTIME_SCHEMA,
            "status": result_status,
            "requeued": changed,
            "affected": [str(item["paper_key"]) for item in changed],
            "already_requeued": already_requeued,
            "skipped": skipped,
            "old_thread_ids": old_thread_ids,
            "required_archive_thread_ids": list(dict.fromkeys(required_archive_thread_ids)),
            "dispatched": False,
            "pipeline_synchronized": True,
        }
        if changed and self._last_projection_refresh is not None:
            result["projection_refresh"] = dict(self._last_projection_refresh)
        self._last_result = result
        self._last_error = ""
        return result

    def request_pause(self) -> dict[str, Any]:
        """Request a safe pause; an active turn finishes before the callback stops."""

        current = self._load_pipeline()
        current_settings = current.get("settings", {})
        if (
            isinstance(current_settings, dict)
            and str(current_settings.get("scheduling_mode") or "").strip().lower() == "manual"
        ):
            # WritingAdapter already commits manual mode while holding the
            # shared pipeline lock.  Avoid recursively acquiring the same
            # cross-process lock when it then notifies this in-process worker.
            return {
                "pause_requested": True,
                "scheduling_mode": "manual",
                "status": self.status(check_account=False),
            }

        def pause_state(state: dict[str, Any]) -> None:
            settings = state.setdefault("settings", {})
            if not isinstance(settings, dict):
                raise WritingSDKRuntimeError(
                    "WRITING_PIPELINE_STATE_INVALID", "writing pipeline settings must be an object"
                )
            settings["scheduling_mode"] = "manual"
            settings["sdk_pause_requested_at"] = now_iso()

        self._mutate_pipeline(pause_state)
        return {
            "pause_requested": True,
            "scheduling_mode": "manual",
            "status": self.status(check_account=False),
        }

    pause = request_pause

    def status(self, *, check_account: bool = True) -> dict[str, Any]:
        account = self.account_service.status() if check_account else None
        transaction_status = self._revalidation_transaction_status()
        try:
            pipeline = self._load_pipeline()
            pipeline_blocker = ""
        except Exception as exc:
            if not transaction_status.get("pending"):
                raise
            pipeline = {}
            pipeline_blocker = str(getattr(exc, "code", type(exc).__name__))
        settings = pipeline.get("settings", {})
        scheduling_mode = str(settings.get("scheduling_mode") or "") if isinstance(settings, dict) else ""
        try:
            plans = self.build_plans()
            blocker = ""
        except Exception as exc:
            plans = []
            blocker = str(getattr(exc, "code", type(exc).__name__))
        with self._background_lock:
            worker = self._background_thread
            background_running = worker is not None and worker.is_alive()
            priority_pending_count = len(self._background_priority_keys)
            active_priority_count = len(self._active_background_priority_keys)
        try:
            journal = read_json(
                self.journal_path,
                {"schema": WRITING_SDK_JOURNAL_SCHEMA, "papers": {}},
            )
        except Exception:
            if not transaction_status.get("pending"):
                raise
            journal = {"schema": WRITING_SDK_JOURNAL_SCHEMA, "papers": {}}
        journal_papers = journal.get("papers", {}) if isinstance(journal, dict) else {}
        journal_status_counts: dict[str, int] = {}
        journal_model_counts: dict[str, int] = {}
        journal_blocker = ""
        if isinstance(journal_papers, dict):
            for record in journal_papers.values():
                if not isinstance(record, dict):
                    continue
                journal_status = _journal_status(record)
                journal_status_counts[journal_status] = journal_status_counts.get(journal_status, 0) + 1
                journal_model = str(record.get("model") or "").strip() or "unbound"
                journal_model_counts[journal_model] = journal_model_counts.get(journal_model, 0) + 1
                if journal_status == "blocked" and not journal_blocker:
                    journal_blocker = str(record.get("blocker") or "WRITING_SDK_BLOCKED")
        running_count = journal_status_counts.get("running", 0)
        creating_thread_count = journal_status_counts.get("creating_thread", 0)
        starting_count = journal_status_counts.get("starting", 0) + creating_thread_count
        waiting_event_count = journal_status_counts.get("waiting_event", 0)
        waiting_prerequisite_count = journal_status_counts.get("waiting_prerequisite", 0)
        recovery_count = int(bool(transaction_status.get("pending")))
        waiting_capacity_count = journal_status_counts.get("waiting_capacity", 0)
        # A paper waiting for evidence or another external event has already
        # released its SDK permit.  Counting it as active made the UI claim a
        # concurrency slot was occupied even when no model process existed.
        active_count = running_count + starting_count
        blocked_count = journal_status_counts.get("blocked", 0)
        completed_count = journal_status_counts.get("complete", 0)
        transaction_blocker = str(transaction_status.get("blocker") or "")
        effective_blocker = transaction_blocker or pipeline_blocker or blocker or self._last_error or journal_blocker
        if transaction_status.get("status") == "failed":
            runtime_status = "recovery_failed"
        elif transaction_status.get("pending"):
            runtime_status = "recovery_pending"
        elif background_running:
            runtime_status = "running"
        elif self._last_error:
            runtime_status = "failed"
        elif journal_status_counts.get("blocked"):
            runtime_status = "blocked"
        elif any(journal_status_counts.get(item) for item in _DISPATCH_WAITING_STATUSES):
            runtime_status = "waiting"
        elif journal_status_counts.get("paused"):
            runtime_status = "paused"
        elif plans:
            runtime_status = "ready"
        else:
            runtime_status = "idle"
        return {
            "schema": WRITING_SDK_RUNTIME_SCHEMA,
            "ok": not effective_blocker
            and (account is None or bool(account.get("ok") and account.get("authenticated"))),
            "status": runtime_status,
            "scheduling_mode": scheduling_mode,
            "background_running": background_running,
            "priority_pending_count": priority_pending_count,
            "active_priority_count": active_priority_count,
            "plan_count": len(plans),
            "journal_paper_count": len(journal_papers) if isinstance(journal_papers, dict) else 0,
            "journal_status_counts": journal_status_counts,
            "status_counts": journal_status_counts,
            "configured_model": self.model,
            "step_token_budgets": dict(config.WRITING_SDK_STEP_TOKEN_BUDGETS),
            "journal_model_counts": journal_model_counts,
            "token_usage": _journal_usage_summary(journal_papers),
            "active_count": active_count,
            "running_count": running_count,
            "starting_count": starting_count,
            "creating_thread_count": creating_thread_count,
            "recovery_count": recovery_count,
            "waiting_event_count": waiting_event_count,
            "waiting_prerequisite_count": waiting_prerequisite_count,
            "waiting_capacity_count": waiting_capacity_count,
            "blocked_count": blocked_count,
            "completed_count": completed_count,
            "isolated_codex_home": str(self.codex_home),
            "account": account,
            "blocker": effective_blocker,
            "revalidation_transaction": transaction_status,
            "last_result": self._last_result,
        }


_DEFAULT_RUNTIME: WritingSDKRuntime | None = None
_DEFAULT_RUNTIME_LOCK = threading.Lock()


def get_writing_sdk_runtime() -> WritingSDKRuntime:
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            _DEFAULT_RUNTIME = WritingSDKRuntime()
        return _DEFAULT_RUNTIME


def ensure_ready() -> dict[str, Any]:
    return get_writing_sdk_runtime().ensure_ready()


def require_ready() -> dict[str, Any]:
    return ensure_ready()


def run_writing_sdk(paper_keys: list[str] | None = None, *, until_step: int | None = None) -> dict[str, Any]:
    return get_writing_sdk_runtime().run(paper_keys, until_step=until_step)


def run(paper_keys: list[str] | None = None, *, until_step: int | None = None) -> dict[str, Any]:
    return run_writing_sdk(paper_keys, until_step=until_step)


def writing_sdk_status() -> dict[str, Any]:
    return get_writing_sdk_runtime().status()


def status() -> dict[str, Any]:
    return writing_sdk_status()


def start_background(
    *,
    priority_paper_keys: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return get_writing_sdk_runtime().start_background(priority_paper_keys=priority_paper_keys)


def requeue_recoverable(
    paper_keys: list[str] | None = None,
    *,
    replace_failed_step1_threads: bool = False,
    archived_thread_ids: list[str] | None = None,
) -> dict[str, Any]:
    return get_writing_sdk_runtime().requeue_recoverable(
        paper_keys,
        replace_failed_step1_threads=replace_failed_step1_threads,
        archived_thread_ids=archived_thread_ids,
    )


def revalidate_last_completed(paper_key: str) -> dict[str, Any]:
    return get_writing_sdk_runtime().revalidate_last_completed(paper_key)


def request_pause() -> dict[str, Any]:
    return get_writing_sdk_runtime().request_pause()


def pause() -> dict[str, Any]:
    return request_pause()


__all__ = [
    "WRITING_SDK_JOURNAL_PATH",
    "WRITING_SDK_LOCK_PATH",
    "WRITING_SDK_RUNTIME_SCHEMA",
    "WritingSDKRuntime",
    "WritingSDKRuntimeError",
    "ensure_ready",
    "get_writing_sdk_runtime",
    "pause",
    "requeue_recoverable",
    "revalidate_last_completed",
    "request_pause",
    "require_ready",
    "run",
    "run_writing_sdk",
    "start_background",
    "status",
    "writing_sdk_status",
]
