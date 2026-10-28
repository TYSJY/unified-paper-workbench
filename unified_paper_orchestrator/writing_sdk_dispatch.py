from __future__ import annotations
from .sdk_model_catalog import read_sdk_models

import asyncio
import copy
import hashlib
import inspect
import json
import math
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Mapping, Protocol

from openai_codex import ApprovalMode, AsyncCodex, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort, ThreadSourceKind

from .config import WRITING_MAX_WORKFLOW_STEPS
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .step_execution import (
    execution_profile_catalog_error,
    execution_profile_fingerprint,
    normalize_model_catalog,
    resolve_execution_profile,
)
from .writing_completion import _parse_final_markers, expected_step_artifacts
from .writing_dispatch import _extract_step_prompt, _file_fingerprint, _ticket_prompt
from .writing_evidence_gate import validate_writing_evidence, writing_evidence_blocker
from .writing_evidence_manual import prepare_policy_trusted_writing_evidence
from .writing_gate_policy import WritingGatePolicyError, WritingGatePolicyStore
from .writing_novelty_gate import (
    validate_writing_step2_contract,
    writing_novelty_blocker,
    writing_step2_contract_validation,
)
from .writing_output_contract import validate_writing_step_output, writing_retry_validator_feedback
from .writing_prompt_schemes import is_undergraduate, snapshot_prompt_fingerprint, undergraduate_prompt
from .undergraduate_output_contract import validate_undergraduate_output
from .undergraduate_verification import uses_undergraduate_verification, undergraduate_evidence_gate
from .writing_prompt_integrity import (
    WritingPromptIntegrityError,
    writing_prompt_fingerprint,
)
from .writing_workflow import (
    WritingWorkflowError,
    custom_prompt_fingerprint,
    default_workflow_snapshot,
    normalize_workflow_snapshot,
    workflow_contract_step,
    workflow_previous_contract_step,
    workflow_prompt_unchanged,
    workflow_step,
    workflow_step_count,
)


WRITING_SDK_JOURNAL_SCHEMA = "unified-paper-orchestrator/writing-sdk-dispatch/v1"
WRITING_PROMPT_MANIFEST_MISMATCH = "WRITING_PROMPT_MANIFEST_MISMATCH"
GLOBAL_CODEX_SESSION_LIMIT = 10
# V2 deliberately uses one root SDK thread per paper.  Both Codex multi-agent
# feature flags are disabled below, so no child-agent capacity is reserved.
WRITING_SDK_MAX_CHILD_THREADS = 0
WRITING_SDK_AGENT_MAX_THREADS = 1
WRITING_SDK_AGENT_MAX_DEPTH = 1
WRITING_SDK_SESSION_WEIGHT = 1
_PAPER_KEY_LIMIT = 200
_RESERVED_PIPELINE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{3}$")
_ALL_THREAD_SOURCE_KINDS = list(ThreadSourceKind)
_ACTIVE_DISPATCH_PATHS: set[str] = set()
_ACTIVE_DISPATCH_PATHS_LOCK = threading.Lock()
_PROGRESS_CALLBACK_FAILED = "WRITING_PROGRESS_CALLBACK_FAILED"
_EXTERNAL_CAPACITY_RECHECK_SECONDS = 5.0
_ACTIVE_TURN_RECHECK_SECONDS = 5.0
_BATCH_CIRCUIT_REASON = "WRITING_BATCH_CIRCUIT_OPEN"
_BATCH_CIRCUIT_THRESHOLD = 2
_BATCH_CIRCUIT_HISTORY_LIMIT = 50
_PREREQUISITE_GATE_STEPS = {3, 6}
_RETRY_FEEDBACK_REASONS = {
    "WRITING_OUTPUT_SEMANTIC_INVALID",
    "WRITING_EXPECTED_ARTIFACT_STALE",
}
_RETRY_FEEDBACK_ORIGIN = "WRITING_OUTPUT_SEMANTIC_INVALID"
_RETRY_EXECUTION_ENVELOPE_MARKER = "UPO_WRITING_RETRY_EXECUTION_ENVELOPE_V1"
_RETRY_EXECUTION_ENVELOPE_MAX_CHARS = 4096
_MANUAL_STEP6_EXECUTION_ENVELOPE_MARKER = "UPO_WRITING_MANUAL_STEP6_TRANSITION_V1"
_MANUAL_STEP6_EXECUTION_ENVELOPE_MAX_CHARS = 4096
_TERMINAL_RESPONSE_EXCERPT_MAX_CHARS = 1200
_VALIDATED_RETRY_BASELINE_SCHEMA = "unified-paper-orchestrator/writing-validated-retry-baseline/v1"
_BATCH_CIRCUIT_CLASSES = {
    "WRITING_FINAL_MARKERS_INVALID": "WRITING_TERMINAL_CONTRACT_FAILURE",
    "WRITING_TERMINAL_STEP_MISMATCH": "WRITING_TERMINAL_CONTRACT_FAILURE",
    "WRITING_TERMINAL_PIPELINE_MISMATCH": "WRITING_TERMINAL_CONTRACT_FAILURE",
    "WRITING_EXPECTED_ARTIFACT_MISSING": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_EXPECTED_ARTIFACT_STALE": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_OUTPUT_SEMANTIC_INVALID": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_OUTPUT_CHANGED_DURING_VALIDATION": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_OUTPUT_BASELINE_MISSING": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_OUTPUT_BASELINE_INVALID": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_OUTPUT_BASELINE_UNSAFE": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_STEP1_OUTPUT_PREEXISTING": "WRITING_OUTPUT_CONTRACT_FAILURE",
    "WRITING_DISPATCH_INPUTS_CHANGED": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_PREREQUISITE_GATE_BINDING_MISSING": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_COMPLETED_ARTIFACT_PROVENANCE_MISSING": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_SOURCE_PROMPT_CHANGED": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_SOURCE_PROMPT_UNREADABLE": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_RETRY_FEEDBACK_INVALID": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_RETRY_FEEDBACK_MISSING": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_RETRY_BASELINE_DRIFT": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_RETRY_PROMPT_HASH_INVALID": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_RETRY_PROMPT_HASH_MISMATCH": "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_MANUAL_STEP6_BINDING_INVALID": "WRITING_DISPATCH_CONTRACT_FAILURE",
    WRITING_PROMPT_MANIFEST_MISMATCH: "WRITING_DISPATCH_CONTRACT_FAILURE",
    "WRITING_TURN_EXECUTION_FAILED": "WRITING_TURN_EXECUTION_FAILURE",
    "WRITING_TURN_NOT_COMPLETED": "WRITING_TURN_EXECUTION_FAILURE",
    "WRITING_RECOVERED_TURN_NOT_COMPLETED": "WRITING_TURN_EXECUTION_FAILURE",
    "WRITING_RECOVERY_THREAD_MISSING": "WRITING_THREAD_INFRA_FAILURE",
    "WRITING_RECOVERY_TURN_AMBIGUOUS": "WRITING_THREAD_INFRA_FAILURE",
    "WRITING_SEND_UNCERTAIN": "WRITING_THREAD_INFRA_FAILURE",
    "WRITING_THREAD_CREATION_UNCERTAIN": "WRITING_THREAD_INFRA_FAILURE",
    "WRITING_THREAD_ALREADY_ACTIVE": "WRITING_THREAD_INFRA_FAILURE",
    "WRITING_THREAD_ID_MISSING": "WRITING_THREAD_INFRA_FAILURE",
    "WRITING_OWNED_THREAD_AMBIGUOUS": "WRITING_THREAD_INFRA_FAILURE",
    "GLOBAL_CODEX_SESSION_INSPECTION_FAILED": "WRITING_GLOBAL_SESSION_INFRA_FAILURE",
    "GLOBAL_CODEX_SESSION_CAPACITY_WAIT_FAILED": "WRITING_GLOBAL_SESSION_INFRA_FAILURE",
    _PROGRESS_CALLBACK_FAILED: "WRITING_PROGRESS_INFRA_FAILURE",
}
_TOKEN_USAGE_FIELDS = (
    ("cached_input_tokens", "cachedInputTokens"),
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("reasoning_output_tokens", "reasoningOutputTokens"),
    ("total_tokens", "totalTokens"),
)


class WritingSDKDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GlobalSessionBudgetExhausted(WritingSDKDispatchError):
    def __init__(
        self,
        occupied: int,
        requested_weight: int,
        limit: int,
        *,
        capacity_generation: int | None = None,
    ) -> None:
        super().__init__(
            "GLOBAL_CODEX_SESSION_BUDGET_EXHAUSTED",
            (
                f"detected/reserved Codex occupancy {occupied} plus requested weight "
                f"{requested_weight} exceeds the global limit {limit}"
            ),
        )
        self.active = occupied
        self.requested_weight = requested_weight
        self.limit = limit
        self.capacity_generation = capacity_generation


@dataclass(frozen=True, slots=True)
class ExactWritingPrompt:
    """One externally owned prompt and the hash of its exact UTF-8 bytes."""

    text: str
    sha256: str
    source_root: str = ""
    source_prompt: dict[str, Any] | None = None

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        source_root: Path | str = "",
        source_prompt: Mapping[str, Any] | None = None,
    ) -> "ExactWritingPrompt":
        return cls(
            text=text,
            sha256=_sha256_text(text),
            source_root=str(source_root),
            source_prompt=dict(source_prompt) if source_prompt is not None else None,
        )

    def validate(self) -> None:
        if _sha256_text(self.text) != self.sha256:
            raise WritingSDKDispatchError(
                "WRITING_PROMPT_HASH_MISMATCH",
                "external writing prompt bytes do not match the supplied SHA-256",
            )
        if self.source_prompt is not None and (
            not self.source_root or not workflow_prompt_unchanged(self.source_root, self.source_prompt)
        ):
            raise WritingSDKDispatchError(
                "WRITING_SOURCE_PROMPT_CHANGED",
                "the tuned writing-step prompt changed after its exact byte fingerprint was captured",
            )


PromptProvider = Callable[[int, str], ExactWritingPrompt]
ProgressCallback = Callable[[str, dict[str, Any]], Any]
ShouldContinueCallback = Callable[[int, dict[str, Any]], Any]


def existing_writing_prompt_provider(
    scheduler_prompt_path: Path,
    paper_key: str,
    workflow_snapshot: Mapping[str, Any] | None = None,
    *,
    reserved_pipeline_prefix: str = "",
    preserve_unbound_step1_owner: bool = False,
) -> PromptProvider:
    """Build the existing control envelope around an unchanged exact template.

    The deterministic attempt id makes the envelope hash stable across crash
    recovery.  The source prompt file is only read; its bytes are never written
    or normalized by this provider.
    """

    prompt_path = Path(scheduler_prompt_path)
    project_root = prompt_path.parent.parent
    attempt_seed = hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:24]
    workflow = normalize_workflow_snapshot(workflow_snapshot or default_workflow_snapshot())
    reserved_prefix = str(reserved_pipeline_prefix or "").strip()
    if reserved_prefix and _RESERVED_PIPELINE_PREFIX_RE.fullmatch(reserved_prefix) is None:
        raise WritingSDKDispatchError(
            "WRITING_RESERVED_PIPELINE_PREFIX_INVALID",
            "the program-reserved writing pipeline prefix is invalid",
        )

    def provide(step: int, paper_id: str) -> ExactWritingPrompt:
        try:
            definition = workflow_step(workflow, step)
        except WritingWorkflowError as exc:
            raise WritingSDKDispatchError("WRITING_WORKFLOW_STEP_INVALID", str(exc)) from exc
        source_step: int | None = None
        if definition["kind"] == "builtin":
            source_step = int(definition["source_step"])
            if is_undergraduate(workflow):
                exact_template = undergraduate_prompt(definition, workflow, paper_id, step)
            else:
                exact_template = _extract_step_prompt(prompt_path, source_step, paper_id)
            if source_step == 1 and reserved_prefix:
                exact_template = (
                    "UPO_WRITING_PREALLOCATED_PREFIX_V1\n"
                    f"PROGRAM_RESERVED_PIPELINE_PREFIX={reserved_prefix}\n"
                    "This task already owns the exact daily paper number above. "
                    "Do not scan for or allocate another number. Choose only the semantic short-title suffix, "
                    f"create exactly one ideas/{reserved_prefix}_<short-title>.md file, and make the final "
                    f"PIPELINE_ID begin with {reserved_prefix}_.\n\n"
                    f"{exact_template}"
                )
            try:
                source_prompt = (snapshot_prompt_fingerprint(definition) if is_undergraduate(workflow)
                                 else writing_prompt_fingerprint(project_root, source_step))
            except WritingPromptIntegrityError as exc:
                raise WritingSDKDispatchError("WRITING_SOURCE_PROMPT_UNREADABLE", str(exc)) from exc
        else:
            custom_path = Path(str(definition["prompt_path"]))
            try:
                custom_text = custom_path.read_text(encoding="utf-8")
                source_prompt = custom_prompt_fingerprint(custom_path)
            except (OSError, UnicodeError, WritingWorkflowError) as exc:
                raise WritingSDKDispatchError("WRITING_SOURCE_PROMPT_UNREADABLE", str(exc)) from exc
            if not definition.get("prompt_ready") or source_prompt["sha256"] != str(
                definition.get("prompt_sha256") or ""
            ):
                raise WritingSDKDispatchError(
                    "WRITING_SOURCE_PROMPT_CHANGED",
                    "the custom workflow prompt does not match the paper's immutable workflow snapshot",
                )
            if not paper_id:
                raise WritingSDKDispatchError("WRITING_RESUME_BINDING_MISSING", "a custom writing step requires paper_id")
            custom_text = custom_text.replace("<paper_id>", paper_id).replace("<idea-id>", paper_id)
            contract_step = int(definition["contract_step"])
            contract_paths = expected_step_artifacts(project_root, project_root / "papers", paper_id, contract_step)
            outputs = "\n".join(f"- `{path.relative_to(project_root).as_posix()}`" for path in contract_paths)
            exact_template = (
                f"继续同一个 pipeline，执行用户配置的写作 Step {step}。\n\n"
                f"PIPELINE_ID={paper_id}\n\n"
                f"步骤名称：{definition['name']}\n\n"
                "必须更新以下既有论文产物，不能另建一篇论文：\n"
                f"{outputs}\n\n"
                "以下是用户为该步骤保存的原始提示词：\n\n"
                f"{custom_text.rstrip()}\n\n"
                "完成后最终回复必须包含这两行机器可读标记：\n"
                f"PIPELINE_ID={paper_id}\n"
                f"STEP_DONE={step}\n"
            )
        attempt_id = f"writing-sdk-{attempt_seed}-step-{step}"
        # Step 1 is initially sent before the model has chosen the semantic
        # paper id.  A semantic-contract repair may recover that id from the
        # exact terminal Turn and artifact while still needing the immutable
        # base prompt from the first send.  Keep only the controller-owned
        # ticket owner unbound in that proven recovery case; the tuned Step 1
        # template and its source bytes remain unchanged.
        ticket_paper_id = "" if source_step == 1 and preserve_unbound_step1_owner else paper_id
        envelope = _ticket_prompt(
            exact_template,
            attempt_id=attempt_id,
            paper_id=ticket_paper_id,
            step=step,
        )
        return ExactWritingPrompt.from_text(
            envelope,
            source_root=project_root,
            source_prompt=source_prompt,
        )

    return provide


@dataclass(frozen=True, slots=True)
class WritingPaperPlan:
    """An isolated paper run. ``paper_key`` is stable even before Step 1 names the paper."""

    paper_key: str
    prompt_provider: PromptProvider
    run_until_step: int = 10
    completed_step: int = 0
    paper_id: str = ""
    thread_id: str = ""
    migration_new_thread: bool = False
    workflow_snapshot: dict[str, Any] | None = None
    reserved_pipeline_prefix: str = ""

    def validate(self) -> None:
        if (
            not self.paper_key
            or len(self.paper_key) > _PAPER_KEY_LIMIT
            or any(character in self.paper_key for character in "\r\n\0")
        ):
            raise WritingSDKDispatchError("WRITING_PAPER_KEY_INVALID", "paper key is invalid")
        workflow = normalize_workflow_snapshot(self.workflow_snapshot or default_workflow_snapshot())
        if any(step["kind"] == "custom" and not step.get("prompt_ready") for step in workflow["steps"]):
            raise WritingSDKDispatchError(
                "WRITING_WORKFLOW_PROMPT_NOT_READY",
                "every custom step must have a saved immutable prompt before dispatch",
            )
        if not 0 <= self.completed_step <= self.run_until_step <= workflow_step_count(workflow):
            raise WritingSDKDispatchError(
                "WRITING_STEP_RANGE_INVALID",
                "writing plan must stay within its immutable workflow snapshot",
            )
        if self.completed_step and not self.paper_id:
            raise WritingSDKDispatchError(
                "WRITING_RESUME_BINDING_MISSING",
                "a resumed paper requires paper_id",
            )
        if self.completed_step and not self.thread_id and not self.migration_new_thread:
            raise WritingSDKDispatchError(
                "WRITING_RESUME_BINDING_MISSING",
                "a resumed paper requires thread_id unless one-time SDK migration is explicit",
            )
        if self.migration_new_thread and not (self.completed_step > 0 and self.paper_id and not self.thread_id):
            raise WritingSDKDispatchError(
                "WRITING_MIGRATION_BINDING_INVALID",
                "migration_new_thread requires completed_step > 0, paper_id, and an empty thread_id",
            )
        reserved_prefix = str(self.reserved_pipeline_prefix or "").strip()
        if reserved_prefix and _RESERVED_PIPELINE_PREFIX_RE.fullmatch(reserved_prefix) is None:
            raise WritingSDKDispatchError(
                "WRITING_RESERVED_PIPELINE_PREFIX_INVALID",
                "the program-reserved writing pipeline prefix is invalid",
            )
        if reserved_prefix and self.paper_id and not self.paper_id.startswith(f"{reserved_prefix}_"):
            raise WritingSDKDispatchError(
                "WRITING_RESERVED_PIPELINE_PREFIX_MISMATCH",
                "the fixed paper id does not belong to its program-reserved daily number",
            )


class AsyncCodexFactory(Protocol):
    def __call__(self) -> Any: ...


class GlobalPermitProvider(Protocol):
    def permit(self, codex: Any, target_thread_id: str, *, weight: int = 1) -> Any: ...

    def wait_for_capacity(self, exhausted: GlobalSessionBudgetExhausted) -> Any: ...


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_sha256(value: Any) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return ""
    return digest


def _terminal_response_excerpt(value: str | None) -> str:
    compact = " ".join(str(value or "").split())
    if not compact:
        return "<empty final response>"
    if len(compact) <= _TERMINAL_RESPONSE_EXCERPT_MAX_CHARS:
        return compact
    return compact[: _TERMINAL_RESPONSE_EXCERPT_MAX_CHARS - 1].rstrip() + "…"


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _usage_value(value: Any, snake_name: str, camel_name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(snake_name, value.get(camel_name))
    return getattr(value, snake_name, getattr(value, camel_name, None))


def _usage_breakdown(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    result: dict[str, int] = {}
    for snake_name, camel_name in _TOKEN_USAGE_FIELDS:
        raw = _usage_value(value, snake_name, camel_name)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            result[snake_name] = raw
    return result or None


def _turn_usage_snapshot(turn: Any) -> dict[str, Any] | None:
    usage = getattr(turn, "usage", None)
    if usage is None:
        return None
    result: dict[str, Any] = {}
    last = _usage_breakdown(_usage_value(usage, "last", "last"))
    total = _usage_breakdown(_usage_value(usage, "total", "total"))
    if last is not None:
        result["last"] = last
    if total is not None:
        result["total"] = total
    context_window = _usage_value(usage, "model_context_window", "modelContextWindow")
    if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0:
        result["model_context_window"] = context_window
    if not result:
        direct = _usage_breakdown(usage)
        if direct is not None:
            result["total"] = direct
    return result or None


def _thread_is_active(thread: Any) -> bool:
    status = getattr(thread, "status", None)
    root = getattr(status, "root", status)
    return str(getattr(root, "type", "") or "") == "active"


def _thread_turns(response: Any) -> list[Any]:
    thread = getattr(response, "thread", response)
    turns = getattr(thread, "turns", [])
    return list(turns) if isinstance(turns, (list, tuple)) else []


def _bounded_writing_thread_config() -> dict[str, Any]:
    """Return fresh SDK overrides for one root conversation per paper."""

    return {
        "features": {"multi_agent": False, "multi_agent_v2": False},
        "agents": {
            # The bundled CLI validates this field as >= 1 even when both
            # multi-agent implementations are disabled.
            "max_threads": WRITING_SDK_AGENT_MAX_THREADS,
            "max_depth": WRITING_SDK_AGENT_MAX_DEPTH,
        },
    }


def _final_response_from_turn(turn: Any) -> str | None:
    direct = getattr(turn, "final_response", None)
    if isinstance(direct, str):
        return direct
    fallback: str | None = None
    for item in reversed(list(getattr(turn, "items", []) or [])):
        value = getattr(item, "root", item)
        if str(getattr(value, "type", "") or "") != "agentMessage":
            continue
        text = getattr(value, "text", None)
        if not isinstance(text, str):
            continue
        phase = _status_value(getattr(value, "phase", None))
        if phase == "final_answer":
            return text
        if phase in {"finalAnswer", ""} and fallback is None:
            fallback = text
    return fallback


async def _all_threads(codex: Any, *, search_term: str | None = None) -> list[Any]:
    """List every SDK-visible source kind, including child/sub-agent threads."""

    cursor: str | None = None
    seen_cursors: set[str] = set()
    result: list[Any] = []
    for _page_number in range(1_000):
        page = await codex.thread_list(
            archived=False,
            cursor=cursor,
            limit=100,
            search_term=search_term,
            source_kinds=_ALL_THREAD_SOURCE_KINDS,
            use_state_db_only=False,
        )
        result.extend(list(getattr(page, "data", []) or []))
        next_cursor = str(getattr(page, "next_cursor", "") or "")
        if not next_cursor:
            return result
        if next_cursor in seen_cursors:
            raise WritingSDKDispatchError(
                "CODEX_THREAD_LIST_CURSOR_LOOP",
                "Codex thread listing returned a repeated pagination cursor",
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise WritingSDKDispatchError(
        "CODEX_THREAD_LIST_TOO_LARGE",
        "Codex thread listing exceeded the deterministic pagination bound",
    )


class SDKGlobalPermitProvider:
    """A fail-closed, event-driven permit guard for all detectable active threads.

    The SDK snapshot includes all source kinds, so detectable sub-agent threads
    consume permits.  Local reservations close the gap before a just-started
    turn appears as active in a subsequent SDK snapshot.
    """

    def __init__(
        self,
        limit: int = GLOBAL_CODEX_SESSION_LIMIT,
        *,
        external_recheck_seconds: float = _EXTERNAL_CAPACITY_RECHECK_SECONDS,
    ) -> None:
        if isinstance(limit, bool) or not 1 <= int(limit) <= GLOBAL_CODEX_SESSION_LIMIT:
            raise ValueError(f"global Codex session limit must be 1-{GLOBAL_CODEX_SESSION_LIMIT}")
        if (
            isinstance(external_recheck_seconds, bool)
            or not math.isfinite(float(external_recheck_seconds))
            or float(external_recheck_seconds) <= 0
        ):
            raise ValueError("external capacity recheck interval must be a positive finite number")
        self.limit = int(limit)
        self.external_recheck_seconds = float(external_recheck_seconds)
        self._semaphore = asyncio.Semaphore(self.limit)
        self._snapshot_lock = asyncio.Lock()
        self._capacity_condition = asyncio.Condition()
        self._capacity_generation = 0
        self._reserved_thread_weights: dict[str, int] = {}

    async def _notify_capacity_release(self) -> None:
        async with self._capacity_condition:
            self._capacity_generation += 1
            self._capacity_condition.notify_all()

    async def wait_for_capacity(self, exhausted: GlobalSessionBudgetExhausted) -> None:
        """Wait for a local release or a low-frequency external reinspection.

        Local reservations notify the condition immediately. SDK-visible work
        owned by another process cannot notify this provider, so the bounded
        timeout is only a wakeup to take a fresh authoritative SDK snapshot.
        """

        observed_generation = exhausted.capacity_generation
        async with self._capacity_condition:
            if observed_generation is not None and self._capacity_generation != observed_generation:
                return
            try:
                await asyncio.wait_for(
                    self._capacity_condition.wait_for(
                        lambda: observed_generation is not None and self._capacity_generation != observed_generation
                    ),
                    timeout=self.external_recheck_seconds,
                )
            except TimeoutError:
                return

    def _occupied_with_reservations(self, threads: list[Any]) -> tuple[int, set[str]]:
        by_id = {
            str(getattr(thread, "id", "") or ""): thread for thread in threads if str(getattr(thread, "id", "") or "")
        }
        active_ids = {thread_id for thread_id, thread in by_id.items() if _thread_is_active(thread)}
        parent_by_id = {
            thread_id: str(getattr(thread, "parent_thread_id", "") or "") for thread_id, thread in by_id.items()
        }
        covered_active_ids: set[str] = set()
        reserved_occupancy = 0
        for root_id, weight in self._reserved_thread_weights.items():
            tree_ids = {root_id}
            changed = True
            while changed:
                changed = False
                for thread_id, parent_id in parent_by_id.items():
                    if thread_id not in tree_ids and parent_id in tree_ids:
                        tree_ids.add(thread_id)
                        changed = True
            observed_tree = (active_ids & tree_ids) - covered_active_ids
            covered_active_ids.update(observed_tree)
            reserved_occupancy += max(weight, len(observed_tree))
        occupied = len(active_ids - covered_active_ids) + reserved_occupancy
        return occupied, active_ids

    @asynccontextmanager
    async def permit(self, codex: Any, target_thread_id: str, *, weight: int = 1):
        target_thread_id = str(target_thread_id or "")
        if not target_thread_id:
            raise WritingSDKDispatchError("WRITING_THREAD_ID_MISSING", "cannot reserve an empty thread id")
        if isinstance(weight, bool) or not 1 <= int(weight) <= self.limit:
            raise WritingSDKDispatchError(
                "GLOBAL_CODEX_SESSION_WEIGHT_INVALID",
                f"Codex permit weight must be 1-{self.limit}",
            )
        weight = int(weight)
        await self._semaphore.acquire()
        reserved = False
        try:
            async with self._snapshot_lock:
                try:
                    threads = await _all_threads(codex)
                except WritingSDKDispatchError:
                    raise
                except Exception as exc:
                    raise WritingSDKDispatchError(
                        "GLOBAL_CODEX_SESSION_INSPECTION_FAILED",
                        f"cannot safely inspect active Codex threads: {type(exc).__name__}",
                    ) from exc
                occupied, active_ids = self._occupied_with_reservations(threads)
                if target_thread_id in active_ids or target_thread_id in self._reserved_thread_weights:
                    raise WritingSDKDispatchError(
                        "WRITING_THREAD_ALREADY_ACTIVE",
                        "the fixed paper thread already has an active or reserved turn",
                    )
                if occupied + weight > self.limit:
                    raise GlobalSessionBudgetExhausted(
                        occupied,
                        weight,
                        self.limit,
                        capacity_generation=self._capacity_generation,
                    )
                self._reserved_thread_weights[target_thread_id] = weight
                reserved = True
                snapshot = {
                    "limit": self.limit,
                    "active_detected": len(active_ids),
                    "occupied_before": occupied,
                    "reserved_weight": weight,
                    "reserved_thread_id": target_thread_id,
                }
            yield snapshot
        finally:
            if reserved:
                async with self._snapshot_lock:
                    self._reserved_thread_weights.pop(target_thread_id, None)
            self._semaphore.release()
            if reserved:
                await self._notify_capacity_release()


class WritingSDKDispatcher:
    """Run fixed-thread paper steps through ``AsyncCodex`` completion events.

    This module intentionally owns only its private journal.  It is not wired
    to the live writing pipeline, UI, or the paused Automation fallback.
    """

    def __init__(
        self,
        *,
        root: Path,
        papers_dir: Path,
        journal_path: Path,
        lock_path: Path,
        codex_factory: AsyncCodexFactory = AsyncCodex,
        permit_provider: GlobalPermitProvider | None = None,
        max_parallel_papers: int = GLOBAL_CODEX_SESSION_LIMIT,
        step_weights: Mapping[int, int] | None = None,
        progress_callback: ProgressCallback | None = None,
        should_continue: ShouldContinueCallback | None = None,
        pre_dispatch_guard: Callable[[], None] | None = None,
        prompt_sha256_manifest: Mapping[int, str] | None = None,
        active_turn_recheck_seconds: float = _ACTIVE_TURN_RECHECK_SECONDS,
        model: str | None = None,
        step_token_budgets: Mapping[int, int] | None = None,
        gate_policy_path: Path | None = None,
    ) -> None:
        if isinstance(max_parallel_papers, bool) or not 1 <= int(max_parallel_papers) <= GLOBAL_CODEX_SESSION_LIMIT:
            raise ValueError(f"parallel paper limit must be 1-{GLOBAL_CODEX_SESSION_LIMIT}")
        self.root = Path(root).resolve()
        self.papers_dir = Path(papers_dir).resolve()
        self.journal_path = Path(journal_path)
        self.lock_path = Path(lock_path)
        self.gate_policy_store = WritingGatePolicyStore(
            Path(gate_policy_path) if gate_policy_path is not None else self.journal_path.parent / "writing_gate_policy.json"
        )
        self.codex_factory = codex_factory
        self.permit_provider = permit_provider or SDKGlobalPermitProvider()
        self.max_parallel_papers = int(max_parallel_papers)
        self.step_weights = {
            **{step: WRITING_SDK_SESSION_WEIGHT for step in range(1, 11)},
            **dict(step_weights or {}),
        }
        if any(
            isinstance(step, bool)
            or not 1 <= int(step) <= WRITING_MAX_WORKFLOW_STEPS
            or isinstance(weight, bool)
            or not WRITING_SDK_SESSION_WEIGHT <= int(weight) <= GLOBAL_CODEX_SESSION_LIMIT
            for step, weight in self.step_weights.items()
        ):
            raise ValueError(
                f"writing step weights require Step 1-{WRITING_MAX_WORKFLOW_STEPS} and weight "
                f"{WRITING_SDK_SESSION_WEIGHT}-{GLOBAL_CODEX_SESSION_LIMIT}"
            )
        if (
            isinstance(active_turn_recheck_seconds, bool)
            or not math.isfinite(float(active_turn_recheck_seconds))
            or float(active_turn_recheck_seconds) <= 0
        ):
            raise ValueError("active turn recheck interval must be a positive finite number")
        self.step_weights = {int(step): int(weight) for step, weight in self.step_weights.items()}
        self.progress_callback = progress_callback
        self.should_continue = should_continue
        self.pre_dispatch_guard = pre_dispatch_guard
        self.prompt_sha256_manifest = self._sealed_prompt_sha256_manifest(prompt_sha256_manifest)
        self.active_turn_recheck_seconds = float(active_turn_recheck_seconds)
        self.model = str(model).strip() if model is not None else ""
        if model is not None and (not self.model or len(self.model) > 200 or any(ord(char) < 32 for char in self.model)):
            raise ValueError("writing Codex model must be a non-empty bounded identifier")
        self.step_token_budgets: dict[int, int] = {}
        for raw_step, raw_budget in dict(step_token_budgets or {}).items():
            if (
                isinstance(raw_step, bool)
                or isinstance(raw_budget, bool)
                or not 1 <= int(raw_step) <= WRITING_MAX_WORKFLOW_STEPS
                or not 1 <= int(raw_budget) <= 100_000_000
            ):
                raise ValueError(
                    f"writing step token budgets require Step 1-{WRITING_MAX_WORKFLOW_STEPS} "
                    "and 1-100,000,000 tokens"
                )
            self.step_token_budgets[int(raw_step)] = int(raw_budget)
        self._state_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._legacy_step_one_lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._batch_circuit_failures: dict[str, set[str]] = {}
        self._batch_circuit_classes: dict[str, set[str]] = {}
        self._batch_circuit_open_class = ""
        self._batch_circuit_trigger_blocker = ""
        self._model_catalog_loaded = False
        self._model_catalog: list[dict[str, Any]] | None = None

    @staticmethod
    def _sealed_prompt_sha256_manifest(
        manifest: Mapping[int, str] | None,
    ) -> Mapping[int, str] | None:
        if manifest is None:
            return None
        normalized: dict[int, str] = {}
        for raw_step, raw_digest in manifest.items():
            if isinstance(raw_step, bool):
                raise ValueError("writing prompt manifest steps must be integers 1-10")
            try:
                step = int(raw_step)
            except (TypeError, ValueError):
                raise ValueError("writing prompt manifest steps must be integers 1-10") from None
            digest = str(raw_digest or "").strip().casefold()
            if step in normalized or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("writing prompt manifest requires one valid SHA-256 per step")
            normalized[step] = digest
        if set(normalized) != set(range(1, 11)):
            raise ValueError("writing prompt manifest must seal all 10 writing steps")
        return MappingProxyType(normalized)

    def _validate_prompt_sha256_manifest(self, workflow: Mapping[str, Any] | None = None) -> None:
        if is_undergraduate(workflow):
            for definition in normalize_workflow_snapshot(workflow)["steps"]:
                if definition["kind"] == "builtin":
                    snapshot_prompt_fingerprint(definition)
            return
        manifest = self.prompt_sha256_manifest
        if manifest is None:
            return
        for step in range(1, 11):
            try:
                fingerprint = writing_prompt_fingerprint(self.root, step)
            except WritingPromptIntegrityError as exc:
                raise WritingSDKDispatchError(
                    WRITING_PROMPT_MANIFEST_MISMATCH,
                    f"sealed tuned writing prompt set is unreadable at Step {step}: {exc}",
                ) from exc
            actual = str(fingerprint.get("sha256") or "").casefold()
            if actual != manifest[step]:
                raise WritingSDKDispatchError(
                    WRITING_PROMPT_MANIFEST_MISMATCH,
                    f"sealed tuned writing prompt SHA-256 mismatch at Step {step}",
                )

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema": WRITING_SDK_JOURNAL_SCHEMA,
            "papers": {},
            "history": [],
            "batch_circuit": {},
            "batch_circuit_history": [],
            "updated_at": now_iso(),
        }

    def _load_state(self) -> dict[str, Any]:
        state = read_json(self.journal_path, self._empty_state())
        if not isinstance(state, dict) or state.get("schema") != WRITING_SDK_JOURNAL_SCHEMA:
            raise WritingSDKDispatchError("WRITING_SDK_JOURNAL_INVALID", "writing SDK journal is invalid")
        if not isinstance(state.get("papers"), dict) or not isinstance(state.get("history"), list):
            raise WritingSDKDispatchError("WRITING_SDK_JOURNAL_INVALID", "writing SDK journal collections are invalid")
        state.setdefault("batch_circuit", {})
        state.setdefault("batch_circuit_history", [])
        if not isinstance(state["batch_circuit"], dict) or not isinstance(state["batch_circuit_history"], list):
            raise WritingSDKDispatchError(
                "WRITING_SDK_JOURNAL_INVALID",
                "writing SDK batch circuit journal is invalid",
            )
        # Releases before 0.5.8 used ``waiting_event`` for both an already
        # dispatched Codex turn and a pre-dispatch prerequisite gate.  Only
        # migrate the latter when the journal proves that no turn exists.
        for paper in state["papers"].values():
            if not isinstance(paper, dict):
                continue
            if str(paper.get("status") or "").strip().lower() != "waiting_event":
                continue
            if str(paper.get("waiting_reason") or "").strip().lower() != "prerequisite":
                continue
            if paper.get("active_step") not in {None, ""}:
                continue
            waiting_before_step = paper.get("waiting_before_step")
            steps = paper.get("steps")
            record = steps.get(str(waiting_before_step)) if isinstance(steps, dict) else None
            if (
                isinstance(record, dict)
                and str(record.get("state") or "") == "waiting_prerequisite"
                and not str(record.get("turn_id") or "")
            ):
                paper["status"] = "waiting_prerequisite"
        return state

    def _save_state_unlocked(self) -> None:
        self._state["updated_at"] = now_iso()
        write_json_atomic(self.journal_path, self._state)

    async def _mutate(self, callback: Callable[[dict[str, Any]], None]) -> None:
        async with self._state_lock:
            callback(self._state)
            self._save_state_unlocked()

    async def _set_paper(self, paper_key: str, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            paper = state["papers"][paper_key]
            paper.update(values)
            paper["updated_at"] = now_iso()

        await self._mutate(update)

    async def _set_step(self, paper_key: str, step: int, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            paper = state["papers"][paper_key]
            record = paper.setdefault("steps", {}).setdefault(str(step), {})
            record.update(values)
            record["updated_at"] = now_iso()
            paper["updated_at"] = now_iso()

        await self._mutate(update)

    async def _event(self, event: str, **values: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            history = state["history"]
            history.append({"at": now_iso(), "event": event, **values})
            if len(history) > 500:
                del history[:-500]

        await self._mutate(update)

    def _start_batch_circuit_unlocked(self) -> None:
        previous = self._state.get("batch_circuit")
        circuit_history = self._state.setdefault("batch_circuit_history", [])
        if isinstance(previous, dict) and previous.get("run_id"):
            circuit_history.append(copy.deepcopy(previous))
            if len(circuit_history) > _BATCH_CIRCUIT_HISTORY_LIMIT:
                del circuit_history[:-_BATCH_CIRCUIT_HISTORY_LIMIT]
        started_at = now_iso()
        run_seed = f"{started_at}:{len(circuit_history)}"
        run_id = f"batch-{hashlib.sha256(run_seed.encode('utf-8')).hexdigest()[:16]}"
        self._batch_circuit_failures = {}
        self._batch_circuit_classes = {}
        self._batch_circuit_open_class = ""
        self._batch_circuit_trigger_blocker = ""
        self._state["batch_circuit"] = {
            "run_id": run_id,
            "run_started_at": started_at,
            "status": "closed",
            "threshold": _BATCH_CIRCUIT_THRESHOLD,
            "failure_counts": {},
            "class_counts": {},
            "failure_papers": {},
            "class_papers": {},
            "paused_papers": [],
        }
        history = self._state["history"]
        history.append({"at": started_at, "event": "batch_circuit_run_started", "run_id": run_id})
        if len(history) > 500:
            del history[:-500]

    def _record_batch_failure_unlocked(
        self,
        state: dict[str, Any],
        paper_key: str,
        code: str,
    ) -> None:
        failure_class = _BATCH_CIRCUIT_CLASSES.get(code)
        if failure_class is None:
            return
        self._batch_circuit_failures.setdefault(code, set()).add(paper_key)
        self._batch_circuit_classes.setdefault(failure_class, set()).add(paper_key)
        current = state.setdefault("batch_circuit", {})
        current["failure_counts"] = {
            blocker: len(papers) for blocker, papers in sorted(self._batch_circuit_failures.items())
        }
        current["class_counts"] = {
            category: len(papers) for category, papers in sorted(self._batch_circuit_classes.items())
        }
        current["failure_papers"] = {
            blocker: sorted(papers) for blocker, papers in sorted(self._batch_circuit_failures.items())
        }
        current["class_papers"] = {
            category: sorted(papers) for category, papers in sorted(self._batch_circuit_classes.items())
        }
        history = state["history"]
        history.append(
            {
                "at": now_iso(),
                "event": "batch_circuit_failure_observed",
                "paper_key": paper_key,
                "blocker": code,
                "failure_class": failure_class,
            }
        )
        if (
            not self._batch_circuit_open_class
            and len(self._batch_circuit_classes[failure_class]) >= _BATCH_CIRCUIT_THRESHOLD
        ):
            opened_at = now_iso()
            self._batch_circuit_open_class = failure_class
            self._batch_circuit_trigger_blocker = code
            current.update(
                {
                    "status": "open",
                    "opened_at": opened_at,
                    "reason": _BATCH_CIRCUIT_REASON,
                    "failure_class": failure_class,
                    "trigger_blocker": code,
                    "trigger_blockers": sorted(
                        blocker
                        for blocker in self._batch_circuit_failures
                        if _BATCH_CIRCUIT_CLASSES.get(blocker) == failure_class
                    ),
                    "trigger_papers": sorted(self._batch_circuit_classes[failure_class]),
                }
            )
            history.append(
                {
                    "at": opened_at,
                    "event": "batch_circuit_opened",
                    "reason": _BATCH_CIRCUIT_REASON,
                    "failure_class": failure_class,
                    "trigger_blocker": code,
                    "trigger_papers": sorted(self._batch_circuit_classes[failure_class]),
                }
            )
        if len(history) > 500:
            del history[:-500]

    async def _pause_for_open_batch_circuit(self, paper_key: str, step: int) -> bool:
        notify = False
        async with self._state_lock:
            if not self._batch_circuit_open_class:
                return False
            paper = self._state["papers"][paper_key]
            if paper.get("status") == "paused" and paper.get("pause_reason") == _BATCH_CIRCUIT_REASON:
                return True
            timestamp = now_iso()
            record = paper.setdefault("steps", {}).setdefault(str(step), {})
            if str(record.get("state") or "") != "completed":
                record.update(
                    {
                        "state": "paused_before_send",
                        "pause_reason": _BATCH_CIRCUIT_REASON,
                        "paused_at": timestamp,
                        "updated_at": timestamp,
                    }
                )
            paper.update(
                {
                    "status": "paused",
                    "reason": _BATCH_CIRCUIT_REASON,
                    "pause_reason": _BATCH_CIRCUIT_REASON,
                    "batch_circuit_failure_class": self._batch_circuit_open_class,
                    "batch_circuit_trigger_blocker": self._batch_circuit_trigger_blocker,
                    "paused_before_step": step,
                    "blocker": "",
                    "blocker_message": "",
                    "updated_at": timestamp,
                }
            )
            paper.pop("active_step", None)
            current = self._state["batch_circuit"]
            paused_papers = set(str(value) for value in current.get("paused_papers", []))
            paused_papers.add(paper_key)
            current["paused_papers"] = sorted(paused_papers)
            current["paused_count"] = len(paused_papers)
            history = self._state["history"]
            history.append(
                {
                    "at": timestamp,
                    "event": "batch_circuit_paper_paused",
                    "paper_key": paper_key,
                    "step": step,
                    "reason": _BATCH_CIRCUIT_REASON,
                    "failure_class": self._batch_circuit_open_class,
                }
            )
            if len(history) > 500:
                del history[:-500]
            self._save_state_unlocked()
            notify = True
        if notify:
            await self._notify_progress("paper_paused", paper_key)
        return True

    async def _clear_expired_batch_pause(self, paper_key: str, step: int) -> None:
        async with self._state_lock:
            paper = self._state["papers"][paper_key]
            if paper.get("pause_reason") != _BATCH_CIRCUIT_REASON:
                return
            paper["status"] = "queued"
            for field in (
                "reason",
                "pause_reason",
                "batch_circuit_failure_class",
                "batch_circuit_trigger_blocker",
                "paused_before_step",
            ):
                paper.pop(field, None)
            record = paper.setdefault("steps", {}).get(str(step))
            if isinstance(record, dict) and record.get("state") == "paused_before_send":
                record["state"] = "retry_ready"
                record.pop("pause_reason", None)
                record["updated_at"] = now_iso()
            paper["updated_at"] = now_iso()
            self._save_state_unlocked()

    async def _finish_batch_circuit(self) -> None:
        def finish(state: dict[str, Any]) -> None:
            current = state.get("batch_circuit")
            if not isinstance(current, dict):
                return
            current["finished_at"] = now_iso()
            current["paused_count"] = len(current.get("paused_papers", []))

        await self._mutate(finish)

    async def _paper_snapshot(self, paper_key: str) -> dict[str, Any]:
        async with self._state_lock:
            return copy.deepcopy(self._state["papers"][paper_key])

    @staticmethod
    def _mark_blocked_unlocked(
        state: dict[str, Any],
        paper_key: str,
        code: str,
        message: str,
        *,
        step: int | None = None,
    ) -> int | None:
        """Make a paper and its in-flight step terminal without erasing evidence."""

        paper = state["papers"][paper_key]
        target_step = step
        if target_step is None:
            raw_active_step = paper.get("active_step")
            if isinstance(raw_active_step, int) and not isinstance(raw_active_step, bool):
                target_step = raw_active_step
        timestamp = now_iso()
        if target_step is not None:
            record = paper.setdefault("steps", {}).setdefault(str(target_step), {})
            if str(record.get("state") or "") != "completed":
                record.update(
                    {
                        "state": "blocked",
                        "blocker": code,
                        "blocker_message": message,
                        "blocked_at": timestamp,
                        "updated_at": timestamp,
                    }
                )
        paper.update(
            {
                "status": "blocked",
                "blocker": code,
                "blocker_message": message,
                "updated_at": timestamp,
            }
        )
        paper.pop("active_step", None)
        return target_step

    async def _notify_progress(
        self,
        event: str,
        paper_key: str,
        *,
        fail_closed: bool = True,
    ) -> bool:
        if self.progress_callback is None:
            return True
        snapshot = await self._paper_snapshot(paper_key)
        try:
            result = self.progress_callback(event, snapshot)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            error_type = type(exc).__name__
            message = f"progress callback failed after {event}: {error_type}"

            if not fail_closed:

                def defer(state: dict[str, Any]) -> None:
                    paper = state["papers"][paper_key]
                    paper["progress_callback_warning"] = {
                        "event": event,
                        "error_type": error_type,
                        "at": now_iso(),
                    }
                    history = state["history"]
                    history.append(
                        {
                            "at": now_iso(),
                            "event": "progress_callback_deferred",
                            "paper_key": paper_key,
                            "callback_event": event,
                            "error_type": error_type,
                        }
                    )
                    if len(history) > 500:
                        del history[:-500]

                await self._mutate(defer)
                return True

            def fail(state: dict[str, Any]) -> None:
                self._mark_blocked_unlocked(
                    state,
                    paper_key,
                    _PROGRESS_CALLBACK_FAILED,
                    message,
                )
                paper = state["papers"][paper_key]
                paper["progress_callback_event"] = event
                paper["progress_callback_error"] = error_type
                history = state["history"]
                history.append(
                    {
                        "at": now_iso(),
                        "event": "progress_callback_failed",
                        "paper_key": paper_key,
                        "callback_event": event,
                        "error_type": error_type,
                    }
                )
                self._record_batch_failure_unlocked(
                    state,
                    paper_key,
                    _PROGRESS_CALLBACK_FAILED,
                )
                if len(history) > 500:
                    del history[:-500]

            await self._mutate(fail)
            return False

    async def _continue_before_step(
        self,
        paper_key: str,
        step: int,
        *,
        active_turn: bool = False,
    ) -> bool:
        if self.should_continue is None:
            return True
        snapshot = await self._paper_snapshot(paper_key)
        try:
            result = self.should_continue(step, snapshot)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await self._block(
                paper_key,
                "WRITING_SHOULD_CONTINUE_FAILED",
                f"should_continue failed before Step {step}: {type(exc).__name__}",
                step=step,
            )
            return False
        if bool(result):
            return True

        def pause(state: dict[str, Any]) -> None:
            paper = state["papers"][paper_key]
            timestamp = now_iso()
            paper.update(
                {
                    "status": "paused",
                    "blocker": "",
                    "blocker_message": "",
                    "updated_at": timestamp,
                }
            )
            if active_turn:
                paper["paused_during_step"] = step
                paper.pop("paused_before_step", None)
            else:
                paper["paused_before_step"] = step
                paper.pop("paused_during_step", None)
                if paper.get("active_step") == step:
                    record = paper.setdefault("steps", {}).setdefault(str(step), {})
                    if str(record.get("state") or "") in {
                        "dispatching",
                        "waiting_capacity",
                        "retry_ready",
                        "paused_before_send",
                        "starting",
                    }:
                        record.update(
                            {
                                "state": "paused_before_send",
                                "paused_at": timestamp,
                                "updated_at": timestamp,
                            }
                        )
                paper.pop("active_step", None)
            history = state["history"]
            history.append(
                {
                    "at": timestamp,
                    "event": "paper_paused",
                    "paper_key": paper_key,
                    "before_step": step,
                    "active_turn": active_turn,
                }
            )
            if len(history) > 500:
                del history[:-500]

        await self._mutate(pause)
        await self._notify_progress("paper_paused", paper_key)
        return False

    def _register_plans(self, plans: list[WritingPaperPlan]) -> None:
        papers = self._state["papers"]
        seen_keys: set[str] = set()
        for plan in sorted(plans, key=lambda item: item.paper_key.casefold()):
            plan.validate()
            folded = plan.paper_key.casefold()
            if folded in seen_keys:
                raise WritingSDKDispatchError(
                    "WRITING_PAPER_KEY_DUPLICATE", "paper keys must be case-insensitively unique"
                )
            seen_keys.add(folded)
            record = papers.get(plan.paper_key)
            if record is None:
                workflow = normalize_workflow_snapshot(plan.workflow_snapshot or default_workflow_snapshot())
                record = {
                    "paper_key": plan.paper_key,
                    "paper_id": plan.paper_id,
                    "thread_id": plan.thread_id,
                    "completed_step": plan.completed_step,
                    "run_until_step": plan.run_until_step,
                    "status": "queued" if plan.completed_step < plan.run_until_step else "complete",
                    "migration_new_thread": bool(plan.migration_new_thread),
                    "workflow_snapshot": workflow,
                    "workflow_version_id": workflow["version_id"],
                    "reserved_pipeline_prefix": plan.reserved_pipeline_prefix,
                    "steps": {},
                    "updated_at": now_iso(),
                }
                if self.model:
                    record["model"] = self.model
                    record["model_bound_at"] = now_iso()
                papers[plan.paper_key] = record
                continue
            if not isinstance(record, dict):
                raise WritingSDKDispatchError("WRITING_SDK_JOURNAL_INVALID", "paper journal record is invalid")
            stored_model = str(record.get("model") or "").strip()
            if self.model and stored_model and self.model != stored_model:
                raise WritingSDKDispatchError(
                    "WRITING_MODEL_BINDING_MISMATCH",
                    f"stored writing model {stored_model!r} differs from configured model {self.model!r}",
                )
            if self.model and not stored_model:
                record["model"] = self.model
                record["model_bound_at"] = now_iso()
                record["model_binding_origin"] = (
                    "legacy_journal_migration" if record.get("steps") else "pre_dispatch_binding"
                )
            for field, supplied in (("paper_id", plan.paper_id), ("thread_id", plan.thread_id)):
                stored = str(record.get(field) or "")
                if supplied and stored and supplied != stored:
                    raise WritingSDKDispatchError(
                        "WRITING_FIXED_THREAD_BINDING_MISMATCH",
                        f"stored paper {field} differs from the supplied plan",
                    )
                if supplied and not stored:
                    record[field] = supplied
            if int(record.get("completed_step") or 0) < plan.completed_step:
                raise WritingSDKDispatchError(
                    "WRITING_COMPLETED_STEP_MISMATCH",
                    "the supplied plan claims progress not present in the crash journal",
                )
            supplied_workflow = normalize_workflow_snapshot(plan.workflow_snapshot or default_workflow_snapshot())
            stored_workflow = normalize_workflow_snapshot(record.get("workflow_snapshot") or default_workflow_snapshot())
            if stored_workflow["sha256"] != supplied_workflow["sha256"]:
                raise WritingSDKDispatchError(
                    "WRITING_WORKFLOW_BINDING_MISMATCH",
                    "the paper's stored workflow snapshot differs from the supplied immutable snapshot",
                )
            record["workflow_snapshot"] = stored_workflow
            record["workflow_version_id"] = stored_workflow["version_id"]
            record["run_until_step"] = plan.run_until_step
            if plan.migration_new_thread and not str(record.get("thread_id") or ""):
                record["migration_new_thread"] = True
            stored_prefix = str(record.get("reserved_pipeline_prefix") or "").strip()
            if plan.reserved_pipeline_prefix and stored_prefix and plan.reserved_pipeline_prefix != stored_prefix:
                raise WritingSDKDispatchError(
                    "WRITING_RESERVED_PIPELINE_PREFIX_MISMATCH",
                    "the paper's stored reserved prefix differs from the supplied plan",
                )
            if plan.reserved_pipeline_prefix and not stored_prefix:
                record["reserved_pipeline_prefix"] = plan.reserved_pipeline_prefix

        thread_owners: dict[str, str] = {}
        paper_owners: dict[str, str] = {}
        for key, record in papers.items():
            if not isinstance(record, dict):
                continue
            thread_id = str(record.get("thread_id") or "")
            paper_id = str(record.get("paper_id") or "")
            if thread_id:
                if thread_id in thread_owners and thread_owners[thread_id] != key:
                    raise WritingSDKDispatchError(
                        "WRITING_THREAD_OWNER_DUPLICATE",
                        "one Codex thread cannot be bound to two papers",
                    )
                thread_owners[thread_id] = str(key)
            if paper_id:
                folded = paper_id.casefold()
                if folded in paper_owners and paper_owners[folded] != key:
                    raise WritingSDKDispatchError(
                        "WRITING_PIPELINE_ID_DUPLICATE",
                        "one pipeline id cannot be bound to two paper plans",
                    )
                paper_owners[folded] = str(key)
            if (
                int(record.get("completed_step") or 0) > 0
                and not thread_id
                and not bool(record.get("migration_new_thread"))
            ):
                raise WritingSDKDispatchError(
                    "WRITING_RESUME_BINDING_MISSING",
                    "journaled progress without a thread requires explicit one-time SDK migration",
                )
        self._save_state_unlocked()

    @staticmethod
    def _owned_thread_name(paper_key: str) -> str:
        return f"upo-writing-{hashlib.sha256(paper_key.encode('utf-8')).hexdigest()[:24]}"

    async def _find_named_thread(self, codex: Any, name: str) -> str:
        candidates = [
            str(getattr(item, "id", "") or "")
            for item in await _all_threads(codex, search_term=name)
            if str(getattr(item, "name", "") or "") == name and str(getattr(item, "id", "") or "")
        ]
        unique = sorted(set(candidates))
        if len(unique) > 1:
            raise WritingSDKDispatchError(
                "WRITING_OWNED_THREAD_AMBIGUOUS",
                "more than one Codex thread has the deterministic paper owner name",
            )
        return unique[0] if unique else ""

    def _thread_open_kwargs(self, plan: WritingPaperPlan) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.root),
            "approval_mode": ApprovalMode.deny_all,
            "sandbox": Sandbox.workspace_write,
            "config": _bounded_writing_thread_config(),
        }
        record = self._state.get("papers", {}).get(plan.paper_key, {})
        model = str(record.get("model") or self.model or "").strip() if isinstance(record, dict) else self.model
        if model:
            kwargs["model"] = model
        return kwargs

    def _step_execution_profile(self, plan: WritingPaperPlan, step: int) -> dict[str, Any]:
        workflow = normalize_workflow_snapshot(plan.workflow_snapshot or default_workflow_snapshot())
        definition = workflow_step(workflow, step)
        record = self._state.get("papers", {}).get(plan.paper_key, {})
        fallback_model = (
            str(record.get("model") or self.model or "").strip()
            if isinstance(record, Mapping)
            else self.model
        )
        legacy_limit = self.step_token_budgets.get(step)
        if legacy_limit is None:
            legacy_limit = self.step_token_budgets.get(int(definition.get("contract_step") or 0))
        effective = resolve_execution_profile(
            workflow.get("default_execution_profile"),
            definition.get("execution_profile"),
            fallback_model=fallback_model,
            fallback_hard_token_limit=int(legacy_limit or 0),
        )
        # Compatibility for direct dispatcher callers that predate workflow
        # execution profiles. Runtime-created V2 plans always carry a frozen
        # workflow snapshot and therefore use the snapshot value exclusively.
        if plan.workflow_snapshot is None:
            effective["hard_token_limit"] = int(legacy_limit or 0)
            effective["token_limit_source"] = (
                "legacy_dispatcher_override" if legacy_limit is not None else "legacy_unlimited"
            )
            effective["fingerprint"] = execution_profile_fingerprint(effective)
        return effective

    async def _available_model_catalog(self, codex: Any) -> list[dict[str, Any]] | None:
        if self._model_catalog_loaded:
            return self._model_catalog
        self._model_catalog_loaded = True
        method = getattr(codex, "models", None)
        if not callable(method):
            # Lightweight test doubles and older SDK builds do not expose the
            # model catalogue. Production V2 uses a build that does.
            self._model_catalog = None
            return None
        try:
            self._model_catalog = await read_sdk_models(codex)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WritingSDKDispatchError(
                "WRITING_MODEL_CATALOG_UNAVAILABLE",
                f"无法读取当前 Codex 账号的模型列表：{type(exc).__name__}",
            ) from exc
        if not self._model_catalog:
            raise WritingSDKDispatchError(
                "WRITING_MODEL_CATALOG_EMPTY",
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
        raise WritingSDKDispatchError(f"WRITING_STEP_{suffix}", message)

    @staticmethod
    def _turn_execution_kwargs(profile: Mapping[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": str(profile.get("model") or "")}
        effort = str(profile.get("reasoning_effort") or "").strip().casefold()
        if effort:
            kwargs["effort"] = ReasoningEffort(effort)
        return kwargs

    def _thread_cumulative_usage_baseline(self, paper_key: str, step: int, thread_id: str) -> int | None:
        paper = self._state.get("papers", {}).get(paper_key)
        if not isinstance(paper, dict):
            return None
        minimum_legacy_step = 1
        recovery = paper.get("recovery_requeue")
        if isinstance(recovery, Mapping) and recovery.get("thread_replaced"):
            try:
                minimum_legacy_step = max(1, int(recovery.get("next_step") or 1))
            except (TypeError, ValueError):
                return None
        values: list[int] = []

        def observe(
            record: Any,
            *,
            recorded_step: int,
            archived_thread_id: str = "",
            include_same_step: bool = False,
        ) -> None:
            if not isinstance(record, Mapping):
                return
            if recorded_step > step or (recorded_step == step and not include_same_step):
                return
            recorded_thread_id = str(record.get("thread_id") or archived_thread_id or "")
            if recorded_thread_id:
                if recorded_thread_id != thread_id:
                    return
            elif include_same_step or recorded_step < minimum_legacy_step:
                return
            usage = record.get("usage")
            total = usage.get("total") if isinstance(usage, Mapping) else None
            total_tokens = total.get("total_tokens") if isinstance(total, Mapping) else None
            if isinstance(total_tokens, int) and not isinstance(total_tokens, bool) and total_tokens >= 0:
                values.append(total_tokens)

        steps = paper.get("steps")
        if not isinstance(steps, Mapping):
            return None
        for raw_step, record in steps.items():
            try:
                recorded_step = int(raw_step)
            except (TypeError, ValueError):
                continue
            observe(record, recorded_step=recorded_step)
        for collection_name in ("revalidation_attempts", "recovery_attempts"):
            attempts = paper.get(collection_name)
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                try:
                    archived_step = int(attempt.get("step"))
                except (TypeError, ValueError):
                    continue
                observe(
                    attempt.get("step_record"),
                    recorded_step=archived_step,
                    archived_thread_id=str(attempt.get("thread_id") or ""),
                    include_same_step=True,
                )
        return max(values) if values else None

    @staticmethod
    def _usage_budget_observation(usage: Any, baseline_total: int, hard_limit: int) -> dict[str, int] | None:
        total = _usage_breakdown(_usage_value(usage, "total", "total"))
        if total is None or "total_tokens" not in total:
            return None
        observed_total = total["total_tokens"]
        return {
            "hard_limit_tokens": hard_limit,
            "baseline_cumulative_total_tokens": baseline_total,
            "observed_cumulative_total_tokens": observed_total,
            "consumed_step_tokens": observed_total - baseline_total,
        }

    async def _run_turn_with_budget(
        self,
        turn_handle: Any,
        *,
        paper_key: str,
        step: int,
        baseline_total: int,
        hard_limit: int,
    ) -> Any:
        stream_factory = getattr(turn_handle, "stream", None)
        if not callable(stream_factory):
            return await turn_handle.run()
        turn_id = str(getattr(turn_handle, "id", "") or "")
        completed_turn: Any = None
        items: list[Any] = []
        usage: Any = None
        budget_observation: dict[str, int] | None = None
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
                budget_observation = self._usage_budget_observation(usage, baseline_total, hard_limit)
                usage_snapshot = _turn_usage_snapshot(SimpleNamespace(usage=usage))
                fields: dict[str, Any] = {}
                if usage_snapshot is not None:
                    fields["usage"] = usage_snapshot
                if budget_observation is not None:
                    fields["token_budget"] = dict(budget_observation)
                if fields:
                    await self._set_step(paper_key, step, **fields)
                should_interrupt = budget_observation is not None and (
                    budget_observation["consumed_step_tokens"] < 0
                    or budget_observation["consumed_step_tokens"] > hard_limit
                )
                if should_interrupt and not interrupt_requested:
                    interrupt_requested = True
                    await self._set_step(paper_key, step, state="budget_interrupting")
                    try:
                        interruption = turn_handle.interrupt()
                        if inspect.isawaitable(interruption):
                            await interruption
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Keep consuming the registered stream. Dropping it
                        # here would turn a bounded-budget stop into an
                        # ambiguous remote turn that recovery must poll.
                        await self._set_step(
                            paper_key,
                            step,
                            token_budget_interrupt_error=type(exc).__name__,
                        )
            elif method == "turn/completed":
                candidate = getattr(payload, "turn", None)
                if str(getattr(candidate, "id", "") or "") == turn_id:
                    completed_turn = candidate
        if completed_turn is None:
            raise RuntimeError("turn completed event not received")
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
        if budget_observation is None:
            raise WritingSDKDispatchError(
                "WRITING_STEP_TOKEN_USAGE_MISSING",
                f"Step {step} completed without cumulative SDK token usage evidence",
            )
        if budget_observation["consumed_step_tokens"] < 0:
            raise WritingSDKDispatchError(
                "WRITING_STEP_TOKEN_BASELINE_REGRESSION",
                f"Step {step} cumulative token usage fell below its journaled thread baseline",
            )
        if budget_observation["consumed_step_tokens"] > hard_limit:
            raise WritingSDKDispatchError(
                "WRITING_STEP_TOKEN_BUDGET_EXCEEDED",
                f"Step {step} consumed {budget_observation['consumed_step_tokens']} tokens; hard limit is {hard_limit}",
            )
        return result

    async def _ensure_thread(self, codex: Any, plan: WritingPaperPlan) -> Any:
        self._validate_prompt_sha256_manifest(plan.workflow_snapshot)
        paper = self._state["papers"][plan.paper_key]
        thread_id = str(paper.get("thread_id") or "")
        if thread_id:
            return await codex.thread_resume(thread_id, **self._thread_open_kwargs(plan))

        owner_name = self._owned_thread_name(plan.paper_key)
        if paper.get("status") == "creating_thread" or paper.get("blocker") == "WRITING_THREAD_CREATION_UNCERTAIN":
            recovered = await self._find_named_thread(codex, owner_name)
            if not recovered:
                raise WritingSDKDispatchError(
                    "WRITING_THREAD_CREATION_UNCERTAIN",
                    "thread creation was interrupted before a recoverable owner binding was journaled",
                )
            await self._set_paper(
                plan.paper_key,
                thread_id=recovered,
                status="queued",
                migration_new_thread=False,
            )
            await self._event("thread_recovered", paper_key=plan.paper_key, thread_id=recovered)
            if not await self._notify_progress("thread_created", plan.paper_key):
                raise WritingSDKDispatchError(
                    _PROGRESS_CALLBACK_FAILED,
                    "progress callback failed after recovering the fixed thread",
                )
            return await codex.thread_resume(recovered, **self._thread_open_kwargs(plan))

        await self._set_paper(plan.paper_key, status="creating_thread", thread_name=owner_name)
        try:
            thread = await codex.thread_start(**self._thread_open_kwargs(plan))
            await thread.set_name(owner_name)
        except BaseException:
            # The SDK has no idempotency key for thread/start.  Keeping the
            # creation intent blocks an automatic duplicate after uncertainty.
            raise
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise WritingSDKDispatchError("WRITING_THREAD_ID_MISSING", "Codex returned an empty thread id")
        await self._set_paper(
            plan.paper_key,
            thread_id=thread_id,
            status="queued",
            migration_new_thread=False,
        )
        await self._event("thread_created", paper_key=plan.paper_key, thread_id=thread_id)
        if not await self._notify_progress("thread_created", plan.paper_key):
            raise WritingSDKDispatchError(
                _PROGRESS_CALLBACK_FAILED,
                "progress callback failed after creating the fixed thread",
            )
        return thread

    def _input_fingerprints(
        self,
        paper_id: str,
        step: int,
        plan: WritingPaperPlan | None = None,
    ) -> list[dict[str, Any]] | None:
        if step == 1:
            if plan is not None and is_undergraduate(plan.workflow_snapshot):
                payload = json.dumps((plan.workflow_snapshot or {}).get("thesis_brief", {}),
                                     ensure_ascii=False, sort_keys=True).encode("utf-8")
                return [{"kind": "thesis_brief", "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}]
            fingerprint = _file_fingerprint(self.root / "research_profile.md", self.root)
            return [fingerprint] if fingerprint is not None else None
        fingerprints: list[dict[str, Any]] = []
        workflow = plan.workflow_snapshot if plan is not None else default_workflow_snapshot()
        previous_contract = workflow_previous_contract_step(workflow, step)
        for path in expected_step_artifacts(self.root, self.papers_dir, paper_id, previous_contract):
            item = _file_fingerprint(path, self.root)
            if item is None:
                return None
            fingerprints.append(item)
        if is_undergraduate(workflow) and previous_contract != 1:
            idea = _file_fingerprint(self.root / "ideas" / f"{paper_id}.md", self.root)
            if idea is None:
                return None
            fingerprints.append(idea)
        return fingerprints or None

    def _output_path_baseline(self, path: Path) -> dict[str, Any]:
        empty: dict[str, Any] = {"sha256": None, "size": None}
        try:
            resolved = path.resolve(strict=False)
            relative = resolved.relative_to(self.root)
        except (OSError, ValueError):
            return {
                "path": str(path),
                "state": "unsafe",
                "contained": False,
                **empty,
            }
        label = relative.as_posix()
        if self._path_has_reparse_component(path):
            return {
                "path": label,
                "state": "unsafe",
                "contained": True,
                **empty,
            }
        try:
            exists = path.exists()
        except OSError:
            exists = True
        if not exists:
            return {
                "path": label,
                "state": "missing",
                "contained": True,
                **empty,
            }
        try:
            strict = path.resolve(strict=True)
            relative = strict.relative_to(self.root)
            before = strict.stat()
            if not strict.is_file():
                raise OSError("expected output baseline is not a regular file")
            digest = hashlib.sha256()
            with strict.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = strict.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError("expected output changed during baseline fingerprinting")
        except (OSError, ValueError):
            return {
                "path": label,
                "state": "unsafe",
                "contained": True,
                **empty,
            }
        if self._path_has_reparse_component(path):
            return {
                "path": label,
                "state": "unsafe",
                "contained": True,
                **empty,
            }
        return {
            "path": relative.as_posix(),
            "state": "file",
            "contained": True,
            "sha256": digest.hexdigest(),
            "size": after.st_size,
        }

    def _path_has_reparse_component(self, path: Path) -> bool:
        """Reject symlinks, junctions, and other Windows reparse components."""

        try:
            relative = path.absolute().relative_to(self.root)
        except ValueError:
            return True
        cursor = self.root
        for part in relative.parts:
            cursor /= part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                return False
            except OSError:
                return True
            is_junction = getattr(cursor, "is_junction", None)
            if (
                cursor.is_symlink()
                or (callable(is_junction) and is_junction())
                or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            ):
                return True
        return False

    def _strict_step_artifacts(
        self,
        paper_id: str,
        step: int,
        plan: WritingPaperPlan | None = None,
    ) -> list[dict[str, Any]] | None:
        workflow = plan.workflow_snapshot if plan is not None else default_workflow_snapshot()
        contract_step = workflow_contract_step(workflow, step)
        paths = expected_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
        if not paths:
            return None
        artifacts: list[dict[str, Any]] = []
        for path in paths:
            if self._path_has_reparse_component(path):
                return None
            fingerprint = _file_fingerprint(path, self.root)
            if fingerprint is None or self._path_has_reparse_component(path):
                return None
            artifacts.append(fingerprint)
        return artifacts

    def _step_output_baseline(
        self,
        paper_id: str,
        step: int,
        plan: WritingPaperPlan | None = None,
    ) -> dict[str, Any] | None:
        workflow = plan.workflow_snapshot if plan is not None else default_workflow_snapshot()
        contract_step = workflow_contract_step(workflow, step)
        if contract_step != 1:
            paths = expected_step_artifacts(self.root, self.papers_dir, paper_id, contract_step)
            if not paths:
                return None
            return {
                "mode": "static",
                "artifacts": [self._output_path_baseline(path) for path in paths],
            }

        ideas = self.root / "ideas"
        try:
            resolved = ideas.resolve(strict=False)
            directory_path = resolved.relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return {
                "mode": "step1_dynamic",
                "directory": str(ideas),
                "contained": False,
                "directory_state": "unsafe",
                "unlisted_state": "missing",
                "artifacts": [],
            }
        directory_state = "missing"
        artifacts: list[dict[str, Any]] = []
        if self._path_has_reparse_component(ideas):
            directory_state = "unsafe"
        elif ideas.exists():
            if not ideas.is_dir():
                directory_state = "unsafe"
            else:
                directory_state = "directory"
                try:
                    candidates = sorted(
                        (path for path in ideas.iterdir() if path.suffix.casefold() == ".md"),
                        key=lambda path: path.name.casefold(),
                    )
                except OSError:
                    directory_state = "unsafe"
                else:
                    artifacts = [self._output_path_baseline(path) for path in candidates]
        return {
            "mode": "step1_dynamic",
            "directory": directory_path,
            "contained": True,
            "directory_state": directory_state,
            "unlisted_state": "missing",
            "artifacts": artifacts,
        }

    @staticmethod
    def _output_baseline_problem(baseline: dict[str, Any] | None) -> tuple[str, str] | None:
        if not isinstance(baseline, dict):
            return (
                "WRITING_OUTPUT_BASELINE_MISSING",
                "the expected output baseline was not recorded before turn/start",
            )
        mode = str(baseline.get("mode") or "")
        artifacts = baseline.get("artifacts")
        if mode not in {"static", "step1_dynamic"} or not isinstance(artifacts, list):
            return (
                "WRITING_OUTPUT_BASELINE_INVALID",
                "the expected output baseline is malformed",
            )
        if mode == "step1_dynamic" and (
            baseline.get("contained") is not True
            or baseline.get("directory_state") == "unsafe"
            or baseline.get("unlisted_state") != "missing"
        ):
            return (
                "WRITING_OUTPUT_BASELINE_UNSAFE",
                "the Step 1 output directory is not a safe contained directory",
            )
        if mode == "static" and any(
            not isinstance(item, dict) or item.get("contained") is not True or item.get("state") == "unsafe"
            for item in artifacts
        ):
            return (
                "WRITING_OUTPUT_BASELINE_UNSAFE",
                "an expected output path was unsafe before turn/start",
            )
        return None

    @classmethod
    def _output_freshness_problem(
        cls,
        baseline: dict[str, Any] | None,
        artifacts: list[dict[str, Any]],
    ) -> tuple[str, str] | None:
        problem = cls._output_baseline_problem(baseline)
        if problem is not None:
            return problem
        assert baseline is not None
        baseline_items = baseline["artifacts"]
        by_path: dict[str, dict[str, Any]] = {}
        for item in baseline_items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return (
                    "WRITING_OUTPUT_BASELINE_INVALID",
                    "the expected output baseline contains a malformed path entry",
                )
            path = os.path.normcase(str(item["path"]))
            if path in by_path:
                return (
                    "WRITING_OUTPUT_BASELINE_INVALID",
                    "the expected output baseline contains duplicate paths",
                )
            by_path[path] = item

        for current in artifacts:
            path = str(current.get("path") or "")
            normalized_path = os.path.normcase(path)
            before = by_path.get(normalized_path)
            if before is None:
                if baseline.get("mode") != "step1_dynamic":
                    return (
                        "WRITING_OUTPUT_BASELINE_INVALID",
                        "a static expected output is absent from its recorded baseline",
                    )
                before = {
                    "state": baseline.get("unlisted_state"),
                    "contained": baseline.get("contained"),
                    "sha256": None,
                    "size": None,
                }
            state = before.get("state")
            if before.get("contained") is not True or state == "unsafe":
                return (
                    "WRITING_OUTPUT_BASELINE_UNSAFE",
                    "an expected output had an unsafe pre-turn baseline",
                )
            if state == "missing":
                continue
            if baseline.get("mode") == "step1_dynamic":
                return (
                    "WRITING_STEP1_OUTPUT_PREEXISTING",
                    "Step 1 selected an idea path that already existed before turn/start",
                )
            if state != "file" or not isinstance(before.get("sha256"), str) or not isinstance(before.get("size"), int):
                return (
                    "WRITING_OUTPUT_BASELINE_INVALID",
                    "an expected output has an invalid pre-turn file fingerprint",
                )
            if before["sha256"] == current.get("sha256"):
                return (
                    "WRITING_EXPECTED_ARTIFACT_STALE",
                    "a static step-owned expected output was not created or content-changed",
                )
        return None

    @staticmethod
    def _prerequisite_gate_decision(
        result: Mapping[str, Any],
        *,
        gate_profile: str,
        blocker: str,
        blocker_message: str,
    ) -> dict[str, Any]:
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        counts = {
            str(key): value
            for key, value in result.items()
            if str(key).endswith("_count") and isinstance(value, int) and not isinstance(value, bool)
        }
        counts["issue_count"] = len(issues)
        input_snapshot = result.get("input_snapshot")
        gate = {
            "gate_profile": str(result.get("gate_profile") or gate_profile),
            "status": str(result.get("status") or "BLOCKED"),
            "ready": bool(result.get("ready")),
            "counts": counts,
            "issues": copy.deepcopy(issues),
        }
        if isinstance(input_snapshot, Mapping):
            gate["input_snapshot"] = copy.deepcopy(dict(input_snapshot))
        return {
            "ready": bool(result.get("ready")),
            "waitable": True,
            "blocker": blocker,
            "blocker_message": blocker_message,
            "gate": gate,
        }

    @staticmethod
    def _gate_snapshot(decision: Mapping[str, Any]) -> dict[str, Any] | None:
        gate = decision.get("gate")
        if not isinstance(gate, Mapping):
            return None
        snapshot = gate.get("input_snapshot")
        if not isinstance(snapshot, Mapping):
            return None
        digest = str(snapshot.get("snapshot_sha256") or "").strip().casefold()
        files = snapshot.get("files")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return None
        if not isinstance(files, list) or not files:
            return None
        return copy.deepcopy(dict(snapshot))

    @classmethod
    def _gate_snapshot_token(cls, decision: Mapping[str, Any]) -> str:
        snapshot = cls._gate_snapshot(decision)
        return str(snapshot.get("snapshot_sha256") or "") if snapshot is not None else ""

    @staticmethod
    def _gate_change_wait_decision(
        decision: Mapping[str, Any],
        *,
        message: str,
    ) -> dict[str, Any]:
        changed = copy.deepcopy(dict(decision))
        changed.update(
            {
                "ready": False,
                "waitable": True,
                "blocker": "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED_BEFORE_SEND",
                "blocker_message": message,
            }
        )
        return changed

    async def _reconcile_completed_step2_artifacts(
        self,
        plan: WritingPaperPlan,
        decision: Mapping[str, Any],
    ) -> bool:
        """Audit a valid pre-Step-3 metadata correction without replaying Step 2."""

        paper = self._state["papers"][plan.paper_key]
        if int(paper.get("completed_step") or 0) != 2:
            return True
        paper_id = str(paper.get("paper_id") or "")
        snapshot = self._gate_snapshot(decision)
        if snapshot is None:
            raise WritingSDKDispatchError(
                "WRITING_PREREQUISITE_GATE_BINDING_MISSING",
                "the READY Step 2 contract has no stable input snapshot",
            )
        current_artifacts = self._strict_step_artifacts(paper_id, 2, plan)
        if current_artifacts is None:
            raise WritingSDKDispatchError(
                "WRITING_EXPECTED_ARTIFACT_MISSING",
                "the completed Step 2 artifact contract is missing or unsafe",
            )
        snapshot_files = snapshot.get("files")
        if not isinstance(snapshot_files, list):
            raise WritingSDKDispatchError(
                "WRITING_PREREQUISITE_GATE_BINDING_MISSING",
                "the READY Step 2 input snapshot has no file manifest",
            )
        snapshot_by_path = {
            str(item.get("path") or ""): {
                "path": str(item.get("path") or ""),
                "sha256": str(item.get("sha256") or ""),
                "size": item.get("size"),
            }
            for item in snapshot_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        }
        gate_artifacts = [snapshot_by_path.get(str(item.get("path") or "")) for item in current_artifacts]
        if any(item is None for item in gate_artifacts) or gate_artifacts != current_artifacts:
            raise WritingSDKDispatchError(
                "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED",
                "Step 2 artifacts changed after the READY gate snapshot was captured",
            )

        step2 = paper.setdefault("steps", {}).get("2")
        if not isinstance(step2, dict) or str(step2.get("state") or "") != "completed":
            raise WritingSDKDispatchError(
                "WRITING_COMPLETED_ARTIFACT_PROVENANCE_MISSING",
                "completed Step 2 has no immutable completion record",
            )
        recorded_artifacts = step2.get("artifacts")
        if not isinstance(recorded_artifacts, list) or not recorded_artifacts:
            raise WritingSDKDispatchError(
                "WRITING_COMPLETED_ARTIFACT_PROVENANCE_MISSING",
                "completed Step 2 has no recorded artifact fingerprints",
            )
        if recorded_artifacts == current_artifacts:
            return True
        step3 = paper.setdefault("steps", {}).get("3")
        if isinstance(step3, dict) and (
            str(step3.get("turn_id") or "")
            or str(step3.get("state") or "") in {"starting", "running", "recovery_pending", "send_uncertain"}
        ):
            raise WritingSDKDispatchError(
                "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED",
                "Step 2 artifacts changed after Step 3 dispatch had already started",
            )

        reconciled_at = now_iso()
        gate_digest = self._gate_snapshot_token(decision)

        def reconcile(state: dict[str, Any]) -> None:
            paper_record = state["papers"][plan.paper_key]
            step_record = paper_record.setdefault("steps", {}).get("2")
            if not isinstance(step_record, dict) or step_record.get("artifacts") != recorded_artifacts:
                raise WritingSDKDispatchError(
                    "WRITING_COMPLETED_ARTIFACT_PROVENANCE_MISSING",
                    "Step 2 artifact provenance changed during reconciliation",
                )
            pending = paper_record.setdefault("steps", {}).get("3")
            if isinstance(pending, dict) and (
                str(pending.get("turn_id") or "")
                or str(pending.get("state") or "") in {"starting", "running", "recovery_pending", "send_uncertain"}
            ):
                raise WritingSDKDispatchError(
                    "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED",
                    "Step 3 dispatch started during Step 2 artifact reconciliation",
                )
            step_record.setdefault("artifacts_at_completion", copy.deepcopy(recorded_artifacts))
            revisions = step_record.setdefault("artifact_revisions", [])
            if not isinstance(revisions, list):
                raise WritingSDKDispatchError(
                    "WRITING_COMPLETED_ARTIFACT_PROVENANCE_MISSING",
                    "Step 2 artifact revision history is malformed",
                )
            revisions.append(
                {
                    "reconciled_at": reconciled_at,
                    "source": "validated_prompt_02_contract",
                    "gate_profile": "prompt_02_contract",
                    "gate_snapshot_sha256": gate_digest,
                    "from_artifacts": copy.deepcopy(recorded_artifacts),
                    "to_artifacts": copy.deepcopy(current_artifacts),
                }
            )
            step_record["artifacts"] = copy.deepcopy(current_artifacts)
            step_record["reconciled_at"] = reconciled_at
            step_record["updated_at"] = reconciled_at
            paper_record["updated_at"] = reconciled_at
            history = state["history"]
            history.append(
                {
                    "at": reconciled_at,
                    "event": "step_artifacts_reconciled",
                    "paper_key": plan.paper_key,
                    "paper_id": paper_id,
                    "step": 2,
                    "gate_snapshot_sha256": gate_digest,
                }
            )
            if len(history) > 500:
                del history[:-500]

        await self._mutate(reconcile)
        return await self._notify_progress(
            "step_artifacts_reconciled",
            plan.paper_key,
            fail_closed=False,
        )

    @staticmethod
    def _normalize_gate_decision(value: Any) -> dict[str, Any]:
        """Accept structured prerequisite gates while preserving legacy test overrides."""

        if isinstance(value, Mapping):
            decision = dict(value)
            decision["ready"] = bool(decision.get("ready"))
            decision["waitable"] = bool(decision.get("waitable"))
            decision["blocker"] = str(decision.get("blocker") or "WRITING_PREREQUISITE_GATE_BLOCKED")
            decision["blocker_message"] = str(decision.get("blocker_message") or decision["blocker"])
            gate = decision.get("gate")
            decision["gate"] = copy.deepcopy(dict(gate)) if isinstance(gate, Mapping) else {}
            return decision
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise WritingSDKDispatchError(
                "WRITING_PREREQUISITE_GATE_INVALID",
                "writing prerequisite gate returned an invalid decision",
            )
        ready, blocker = value
        code = str(blocker or "WRITING_PREREQUISITE_GATE_BLOCKED")
        return {
            "ready": bool(ready),
            "waitable": False,
            "blocker": code,
            "blocker_message": code,
            "gate": {},
        }

    def _gate_ready_for_plan(self, plan: WritingPaperPlan, paper_id: str, step: int) -> Mapping[str, Any] | tuple[bool, str]:
        if not is_undergraduate(plan.workflow_snapshot):
            return self._gate_ready(paper_id, step)
        if step not in {3, 6}:
            return True, ""
        result = validate_undergraduate_output(self.root, self.papers_dir, paper_id, 2 if step == 3 else 5,
                                               plan.workflow_snapshot or {})
        if result["ready"] and step == 6 and uses_undergraduate_verification(plan.workflow_snapshot):
            evidence = undergraduate_evidence_gate(self.papers_dir, paper_id)
            return self._prerequisite_gate_decision(
                evidence, gate_profile="evidence_v2_transition", blocker="WRITING_EVIDENCE_GATE_BLOCKED",
                blocker_message=writing_evidence_blocker(evidence),
            )
        return {"ready": result["ready"], "waitable": False, "gate": result,
                "blocker": "WRITING_UNDERGRADUATE_PREREQUISITE_BLOCKED",
                "blocker_message": "; ".join(item["message"] for item in result["issues"])}

    def _gate_ready(self, paper_id: str, step: int) -> Mapping[str, Any] | tuple[bool, str]:
        if step == 3:
            result = validate_writing_step2_contract(self.root, paper_id)
            return self._prerequisite_gate_decision(
                result,
                gate_profile="prompt_02_contract",
                blocker="WRITING_NOVELTY_GATE_BLOCKED",
                blocker_message=writing_novelty_blocker(result),
            )
        if step == 6:
            result = self._step6_evidence_gate_result(paper_id)
            return self._prerequisite_gate_decision(
                result,
                gate_profile="evidence_v2_transition",
                blocker="WRITING_EVIDENCE_GATE_BLOCKED",
                blocker_message=writing_evidence_blocker(result),
            )
        return True, ""

    @staticmethod
    def _blocked_policy_gate_result(
        result: Mapping[str, Any],
        *,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        blocked = copy.deepcopy(dict(result))
        issues = blocked.get("issues")
        if not isinstance(issues, list):
            issues = []
        issues.append({"code": code, "path": "writing_gate_policy.json", "message": message})
        blocked.update(
            {
                "status": "BLOCKED",
                "ready": False,
                "transition_ready": False,
                "issues": issues,
            }
        )
        blocked.pop("input_snapshot", None)
        return blocked

    def _step6_evidence_gate_result(self, paper_id: str) -> dict[str, Any]:
        result = validate_writing_evidence(self.papers_dir, paper_id)
        profile = str(result.get("gate_profile") or "")
        needs_policy = not bool(result.get("ready")) or profile == "evidence_auto_trust_transition"
        if not needs_policy:
            return result

        try:
            policy_snapshot = self.gate_policy_store.auto_trust_snapshot()
        except WritingGatePolicyError as exc:
            return self._blocked_policy_gate_result(
                result,
                code=exc.code,
                message=str(exc),
            )

        if profile == "evidence_auto_trust_transition":
            if policy_snapshot is None:
                return self._blocked_policy_gate_result(
                    result,
                    code="WRITING_AUTO_TRUST_POLICY_DISABLED",
                    message="当前工作区已恢复严格确认；旧的自动信任凭证不再授权进入 Step 6。",
                )
            recorded_policy = result.get("policy_snapshot")
            if isinstance(recorded_policy, Mapping) and dict(recorded_policy) == policy_snapshot:
                return result

        if policy_snapshot is None:
            return result

        prepared = prepare_policy_trusted_writing_evidence(
            self.papers_dir,
            paper_id,
            policy_snapshot=policy_snapshot,
            check_only=False,
        )
        prepared_gate = prepared.get("gate")
        if isinstance(prepared_gate, dict):
            return prepared_gate
        blocked = copy.deepcopy(result)
        prepared_issues = prepared.get("issues")
        if isinstance(prepared_issues, list):
            existing = blocked.get("issues")
            blocked["issues"] = list(existing) + copy.deepcopy(prepared_issues) if isinstance(existing, list) else copy.deepcopy(prepared_issues)
        blocked.update({"status": "BLOCKED", "ready": False, "transition_ready": False})
        blocked.pop("input_snapshot", None)
        return blocked

    async def _wait_for_prerequisite(
        self,
        paper_key: str,
        step: int,
        decision: Mapping[str, Any],
    ) -> None:
        gate = copy.deepcopy(dict(decision.get("gate") or {}))
        code = str(decision.get("blocker") or "WRITING_PREREQUISITE_GATE_BLOCKED")
        message = str(decision.get("blocker_message") or code)

        def wait(state: dict[str, Any]) -> None:
            paper = state["papers"][paper_key]
            timestamp = now_iso()
            gate["checked_at"] = timestamp
            record = paper.setdefault("steps", {}).setdefault(str(step), {})
            record.update(
                {
                    "state": "waiting_prerequisite",
                    "blocker": code,
                    "blocker_message": message,
                    "prerequisite_gate": gate,
                    "waiting_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            paper.update(
                {
                    "status": "waiting_prerequisite",
                    "waiting_reason": "prerequisite",
                    "waiting_before_step": step,
                    "blocker": code,
                    "blocker_message": message,
                    "prerequisite_gate": gate,
                    "updated_at": timestamp,
                }
            )
            paper.pop("active_step", None)
            history = state["history"]
            history.append(
                {
                    "at": timestamp,
                    "event": "paper_waiting_prerequisite",
                    "paper_key": paper_key,
                    "step": step,
                    "blocker": code,
                    "gate_profile": str(gate.get("gate_profile") or ""),
                    "issue_count": int(gate.get("counts", {}).get("issue_count") or 0)
                    if isinstance(gate.get("counts"), dict)
                    else 0,
                }
            )
            if len(history) > 500:
                del history[:-500]

        await self._mutate(wait)
        await self._notify_progress("paper_waiting_prerequisite", paper_key)

    async def _clear_prerequisite_wait(
        self,
        paper_key: str,
        step: int,
        decision: Mapping[str, Any],
    ) -> bool:
        cleared = False

        def clear(state: dict[str, Any]) -> None:
            nonlocal cleared
            paper = state["papers"][paper_key]
            record = paper.setdefault("steps", {}).get(str(step))
            if not isinstance(record, dict) or record.get("state") != "waiting_prerequisite":
                return
            timestamp = now_iso()
            gate = copy.deepcopy(dict(decision.get("gate") or {}))
            gate["checked_at"] = timestamp
            record.update(
                {
                    "state": "retry_ready",
                    "prerequisite_gate": gate,
                    "prerequisite_ready_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            for field in ("blocker", "blocker_message", "waiting_at"):
                record.pop(field, None)
            paper["status"] = "queued"
            for field in (
                "blocker",
                "blocker_message",
                "waiting_reason",
                "waiting_before_step",
                "prerequisite_gate",
            ):
                paper.pop(field, None)
            paper["updated_at"] = timestamp
            history = state["history"]
            history.append(
                {
                    "at": timestamp,
                    "event": "paper_prerequisite_ready",
                    "paper_key": paper_key,
                    "step": step,
                    "gate_profile": str(gate.get("gate_profile") or ""),
                }
            )
            if len(history) > 500:
                del history[:-500]
            cleared = True

        await self._mutate(clear)
        if cleared:
            return await self._notify_progress("paper_prerequisite_ready", paper_key)
        return True

    async def _block(
        self,
        paper_key: str,
        code: str,
        message: str,
        *,
        step: int | None = None,
    ) -> None:
        def block(state: dict[str, Any]) -> None:
            target_step = self._mark_blocked_unlocked(
                state,
                paper_key,
                code,
                message,
                step=step,
            )
            history = state["history"]
            event = {
                "at": now_iso(),
                "event": "paper_blocked",
                "paper_key": paper_key,
                "code": code,
            }
            if target_step is not None:
                event["step"] = target_step
            history.append(event)
            self._record_batch_failure_unlocked(state, paper_key, code)
            if len(history) > 500:
                del history[:-500]

        await self._mutate(block)
        await self._notify_progress("paper_blocked", paper_key)

    async def _complete_turn(
        self,
        plan: WritingPaperPlan,
        step: int,
        turn: Any,
        recorded_inputs: list[dict[str, Any]],
        output_baseline: dict[str, Any] | None,
    ) -> None:
        turn_id = str(getattr(turn, "id", "") or "")
        usage = _turn_usage_snapshot(turn)
        observation: dict[str, Any] = {}
        if turn_id:
            observation["turn_id"] = turn_id
        if usage is not None:
            observation["usage"] = usage
        if observation:
            await self._set_step(plan.paper_key, step, **observation)
        self._validate_prompt_sha256_manifest(plan.workflow_snapshot)
        paper = self._state["papers"][plan.paper_key]
        current_step_record = paper.setdefault("steps", {}).get(str(step))
        source_prompt = current_step_record.get("source_prompt") if isinstance(current_step_record, dict) else None
        if isinstance(source_prompt, dict) and not workflow_prompt_unchanged(self.root, source_prompt):
            raise WritingSDKDispatchError(
                "WRITING_SOURCE_PROMPT_CHANGED",
                "the tuned writing-step prompt changed while the Codex turn was running",
            )
        status = _status_value(getattr(turn, "status", ""))
        if status != "completed":
            raise WritingSDKDispatchError(
                "WRITING_TURN_NOT_COMPLETED",
                f"Codex turn ended with status {status or 'unknown'}",
            )
        final_response = _final_response_from_turn(turn)
        markers = _parse_final_markers(final_response or "")
        if markers is None:
            response_excerpt = _terminal_response_excerpt(final_response)
            await self._set_step(
                plan.paper_key,
                step,
                final_response_sha256=_sha256_text(final_response or ""),
                terminal_response_excerpt=response_excerpt,
            )
            raise WritingSDKDispatchError(
                "WRITING_FINAL_MARKERS_INVALID",
                "completed Codex turn omitted the required PIPELINE_ID/STEP_DONE markers; "
                f"model response: {response_excerpt}",
            )
        pipeline_id, step_done = markers
        contract_step = workflow_contract_step(plan.workflow_snapshot, step)
        source_step = int(workflow_step(plan.workflow_snapshot, step)["source_step"])
        expected_pipeline_id = str(paper.get("paper_id") or "")
        if step_done != step:
            raise WritingSDKDispatchError(
                "WRITING_TERMINAL_STEP_MISMATCH",
                f"terminal marker reported step {step_done}, expected {step}",
            )
        if expected_pipeline_id and pipeline_id != expected_pipeline_id:
            raise WritingSDKDispatchError(
                "WRITING_TERMINAL_PIPELINE_MISMATCH",
                "terminal pipeline marker differs from the fixed paper binding",
            )
        if not expected_pipeline_id:
            expected_pipeline_id = pipeline_id
        reserved_prefix = str(paper.get("reserved_pipeline_prefix") or plan.reserved_pipeline_prefix or "").strip()
        if reserved_prefix and not expected_pipeline_id.startswith(f"{reserved_prefix}_"):
            raise WritingSDKDispatchError(
                "WRITING_RESERVED_PIPELINE_PREFIX_MISMATCH",
                f"Step 1 must use its program-reserved prefix {reserved_prefix}_",
            )
        if source_step in _PREREQUISITE_GATE_STEPS:
            current_gate = self._normalize_gate_decision(self._gate_ready_for_plan(plan, expected_pipeline_id, source_step))
            if current_gate["waitable"]:
                recorded_gate = (
                    current_step_record.get("prerequisite_gate") if isinstance(current_step_record, dict) else None
                )
                recorded_snapshot = (
                    self._gate_snapshot({"gate": recorded_gate}) if isinstance(recorded_gate, Mapping) else None
                )
                if recorded_snapshot is None:
                    raise WritingSDKDispatchError(
                        "WRITING_PREREQUISITE_GATE_BINDING_MISSING",
                        f"Step {step} turn has no journaled prerequisite input snapshot",
                    )
                if not current_gate["ready"]:
                    raise WritingSDKDispatchError(
                        "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED",
                        f"Step {step} prerequisite became invalid while the Codex turn was running",
                    )
                if self._gate_snapshot_token(current_gate) != str(recorded_snapshot.get("snapshot_sha256") or ""):
                    raise WritingSDKDispatchError(
                        "WRITING_PREREQUISITE_GATE_INPUTS_CHANGED",
                        f"Step {step} prerequisite bytes changed while the Codex turn was running",
                    )
        for key, other in self._state["papers"].items():
            if (
                key != plan.paper_key
                and isinstance(other, dict)
                and str(other.get("paper_id") or "").casefold() == expected_pipeline_id.casefold()
            ):
                raise WritingSDKDispatchError(
                    "WRITING_PIPELINE_ID_DUPLICATE",
                    "Step 1 produced a pipeline id already owned by another paper",
                )
        current_inputs = self._input_fingerprints(expected_pipeline_id if step > 1 else "", step, plan)
        if current_inputs != recorded_inputs:
            raise WritingSDKDispatchError(
                "WRITING_DISPATCH_INPUTS_CHANGED",
                "writing inputs changed while the Codex turn was running",
            )
        artifacts = self._strict_step_artifacts(expected_pipeline_id, step, plan)
        if artifacts is None:
            raise WritingSDKDispatchError(
                "WRITING_EXPECTED_ARTIFACT_MISSING",
                "completed Codex turn did not produce the full artifact contract",
            )
        freshness_problem = self._output_freshness_problem(output_baseline, artifacts)
        if freshness_problem is not None:
            raise WritingSDKDispatchError(*freshness_problem)
        semantic_validation: dict[str, Any] | None = None
        if isinstance(source_prompt, dict):
            if is_undergraduate(plan.workflow_snapshot):
                semantic_validation = validate_undergraduate_output(
                    self.root, self.papers_dir, expected_pipeline_id, contract_step,
                    plan.workflow_snapshot or {}, is_terminal=step >= plan.run_until_step,
                )
            elif contract_step == 2:
                semantic_result = validate_writing_step2_contract(self.root, expected_pipeline_id)
                semantic_validation = writing_step2_contract_validation(semantic_result)
            else:
                semantic_result = validate_writing_step_output(
                    self.root,
                    self.papers_dir,
                    expected_pipeline_id,
                    contract_step,
                    is_terminal=step >= plan.run_until_step,
                )
                semantic_validation = {
                    "status": str(semantic_result.get("status") or "BLOCKED"),
                    "ready": bool(semantic_result.get("ready")),
                    "counts": copy.deepcopy(semantic_result.get("counts") or {}),
                    "issues": copy.deepcopy(semantic_result.get("issues") or []),
                    "warnings": copy.deepcopy(semantic_result.get("warnings") or []),
                    "path": str(semantic_result.get("path") or ""),
                    "compile_contract": copy.deepcopy(semantic_result.get("compile_contract") or {}),
                }
            await self._set_step(
                plan.paper_key,
                step,
                semantic_validation=semantic_validation,
            )
            if not semantic_validation["ready"]:
                issue_codes = [
                    str(issue.get("code") or "writing_output_semantic_invalid")
                    for issue in semantic_validation["issues"][:5]
                    if isinstance(issue, dict)
                ]
                suffix = "; ".join(issue_codes) or "semantic output requirements were not met"
                raise WritingSDKDispatchError(
                    "WRITING_OUTPUT_SEMANTIC_INVALID",
                    f"Step {step} output failed its tuned-prompt semantic contract: {suffix}",
                )
            artifacts_after_semantic_check = self._strict_step_artifacts(expected_pipeline_id, step, plan)
            if artifacts_after_semantic_check != artifacts:
                raise WritingSDKDispatchError(
                    "WRITING_OUTPUT_CHANGED_DURING_VALIDATION",
                    "writing output changed during semantic validation",
                )
        status_after = "complete" if step >= plan.run_until_step else "queued"

        def commit(state: dict[str, Any]) -> None:
            for key, other in state["papers"].items():
                if (
                    key != plan.paper_key
                    and isinstance(other, dict)
                    and str(other.get("paper_id") or "").casefold() == expected_pipeline_id.casefold()
                ):
                    raise WritingSDKDispatchError(
                        "WRITING_PIPELINE_ID_DUPLICATE",
                        "Step 1 produced a pipeline id already owned by another paper",
                    )
            paper_record = state["papers"][plan.paper_key]
            step_record = paper_record.setdefault("steps", {}).setdefault(str(step), {})
            step_record.update(
                {
                    "state": "completed",
                    "turn_id": turn_id,
                    "final_response_sha256": _sha256_text(final_response or ""),
                    "output_baseline": output_baseline,
                    "artifacts": artifacts,
                    "completed_at": now_iso(),
                    "updated_at": now_iso(),
                }
            )
            if semantic_validation is not None:
                step_record["semantic_validation"] = semantic_validation
            paper_record.update(
                {
                    "paper_id": expected_pipeline_id,
                    "completed_step": step,
                    "status": status_after,
                    "blocker": "",
                    "blocker_message": "",
                    "updated_at": now_iso(),
                }
            )
            paper_record.pop("active_step", None)
            paper_record.pop("paused_before_step", None)
            paper_record.pop("paused_during_step", None)
            for field in (
                "reason",
                "pause_reason",
                "batch_circuit_failure_class",
                "batch_circuit_trigger_blocker",
            ):
                paper_record.pop(field, None)
            history = state["history"]
            history.append(
                {
                    "at": now_iso(),
                    "event": "step_completed",
                    "paper_key": plan.paper_key,
                    "paper_id": expected_pipeline_id,
                    "step": step,
                    "turn_id": turn_id,
                }
            )
            if len(history) > 500:
                del history[:-500]

        self._validate_prompt_sha256_manifest(plan.workflow_snapshot)
        await self._mutate(commit)
        if not await self._notify_progress("step_completed", plan.paper_key):
            raise WritingSDKDispatchError(
                _PROGRESS_CALLBACK_FAILED,
                "progress callback failed after committing the completed step",
            )

    async def _recover_step(self, codex: Any, plan: WritingPaperPlan, step: int) -> bool:
        paper = self._state["papers"][plan.paper_key]
        record = paper.setdefault("steps", {}).get(str(step))
        if not isinstance(record, dict) or str(record.get("state") or "") not in {
            "starting",
            "running",
            "recovery_pending",
            "send_uncertain",
        }:
            return False
        thread_id = str(paper.get("thread_id") or "")
        if not thread_id:
            await self._block(
                plan.paper_key,
                "WRITING_RECOVERY_THREAD_MISSING",
                "step recovery has no fixed thread",
                step=step,
            )
            return True
        thread = await codex.thread_resume(thread_id, **self._thread_open_kwargs(plan))
        while True:
            response = await thread.read(include_turns=True)
            turns = _thread_turns(response)
            turn_id = str(record.get("turn_id") or "")
            if turn_id:
                matches = [turn for turn in turns if str(getattr(turn, "id", "") or "") == turn_id]
            else:
                baseline = {str(value) for value in record.get("baseline_turn_ids", [])}
                matches = [turn for turn in turns if str(getattr(turn, "id", "") or "") not in baseline]
            if len(matches) > 1:
                await self._block(
                    plan.paper_key,
                    "WRITING_RECOVERY_TURN_AMBIGUOUS",
                    "more than one unjournaled turn appeared on the fixed paper thread",
                    step=step,
                )
                return True
            if not matches:
                await self._block(
                    plan.paper_key,
                    "WRITING_SEND_UNCERTAIN",
                    "the SDK send outcome cannot be proven from the fixed thread history; the step will not be retried",
                    step=step,
                )
                return True
            turn = matches[0]
            status = _status_value(getattr(turn, "status", ""))
            if status == "completed":
                try:
                    output_baseline = record.get("output_baseline")
                    await self._complete_turn(
                        plan,
                        step,
                        turn,
                        list(record.get("input_fingerprints", [])),
                        output_baseline if isinstance(output_baseline, dict) else None,
                    )
                except WritingSDKDispatchError as exc:
                    if exc.code != _PROGRESS_CALLBACK_FAILED:
                        await self._block(plan.paper_key, exc.code, str(exc), step=step)
                return True
            if status in {"inProgress", "in_progress"}:
                await self._set_step(
                    plan.paper_key,
                    step,
                    state="recovery_pending",
                    turn_id=str(getattr(turn, "id", "") or ""),
                )
                await self._set_paper(
                    plan.paper_key,
                    status="waiting_event",
                    blocker="WRITING_TURN_STILL_ACTIVE",
                    active_step=step,
                )
                if not await self._continue_before_step(
                    plan.paper_key,
                    step,
                    active_turn=True,
                ):
                    return True
                await asyncio.sleep(self.active_turn_recheck_seconds)
                continue
            await self._block(
                plan.paper_key,
                "WRITING_RECOVERED_TURN_NOT_COMPLETED",
                f"recovered turn ended with status {status or 'unknown'}; the step will not be duplicated",
                step=step,
            )
            return True

    @staticmethod
    def _retry_feedback_validation_from_archives(
        paper: Mapping[str, Any],
        step: int,
    ) -> Mapping[str, Any] | None:
        recovery_attempts = paper.get("recovery_attempts")
        if isinstance(recovery_attempts, list):
            for attempt in reversed(recovery_attempts):
                if (
                    not isinstance(attempt, Mapping)
                    or attempt.get("action") != "explicit_recoverable_requeue"
                    or attempt.get("step") != step
                    or str(attempt.get("blocker") or "") not in _RETRY_FEEDBACK_REASONS
                ):
                    continue
                archived_step = attempt.get("step_record")
                validation = archived_step.get("semantic_validation") if isinstance(archived_step, Mapping) else None
                if isinstance(validation, Mapping):
                    return validation

        revalidation_attempts = paper.get("revalidation_attempts")
        if isinstance(revalidation_attempts, list):
            for attempt in reversed(revalidation_attempts):
                if (
                    not isinstance(attempt, Mapping)
                    or attempt.get("action") != "current_validator_reopen_last_completed"
                    or attempt.get("step") != step
                ):
                    continue
                validation = attempt.get("semantic_validation")
                if isinstance(validation, Mapping):
                    return validation
        return None

    @classmethod
    def _retry_feedback_for_step(
        cls,
        paper: Mapping[str, Any],
        step: int,
    ) -> dict[str, Any] | None:
        steps = paper.get("steps")
        record = steps.get(str(step)) if isinstance(steps, Mapping) else None
        if not isinstance(record, Mapping):
            return None
        retry_reason = str(record.get("retry_reason") or "")
        retry_origin = str(record.get("retry_origin") or retry_reason)
        if retry_reason not in _RETRY_FEEDBACK_REASONS or retry_origin != _RETRY_FEEDBACK_ORIGIN:
            return None
        state = str(record.get("state") or "")
        if state != "retry_ready" and (
            not isinstance(record.get("retry_execution_envelope"), Mapping)
            or not _normalized_sha256(record.get("execution_prompt_sha256"))
        ):
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_MISSING",
                f"Step {step} left retry_ready without a complete durable retry execution intent",
            )
        prompt_sha256 = _normalized_sha256(record.get("prompt_sha256"))
        if not prompt_sha256:
            raise WritingSDKDispatchError(
                "WRITING_RETRY_PROMPT_HASH_INVALID",
                f"Step {step} semantic retry has no valid 64-hex base prompt SHA-256",
            )
        validation = record.get("semantic_validation")
        if not isinstance(validation, Mapping):
            validation = cls._retry_feedback_validation_from_archives(paper, step)
        if not isinstance(validation, Mapping):
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_MISSING",
                f"Step {step} semantic retry has no allowlisted validator snapshot",
            )
        safe_validation = writing_retry_validator_feedback(validation)
        if safe_validation is None:
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_INVALID",
                f"Step {step} semantic retry validator feedback is malformed or contains an unknown issue code",
            )
        validated_baseline = record.get("validated_retry_baseline")
        if (
            not isinstance(validated_baseline, Mapping)
            or validated_baseline.get("schema") != _VALIDATED_RETRY_BASELINE_SCHEMA
            or validated_baseline.get("step") != step
            or validated_baseline.get("validator") != safe_validation
            or not isinstance(validated_baseline.get("validated_at"), str)
            or not str(validated_baseline.get("validated_at") or "").strip()
        ):
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_INVALID",
                f"Step {step} semantic retry has no matching validated retry baseline",
            )
        raw_artifacts = validated_baseline.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_INVALID",
                f"Step {step} validated retry baseline has no artifacts",
            )
        baseline_artifacts: list[dict[str, Any]] = []
        for artifact in raw_artifacts:
            digest = _normalized_sha256(artifact.get("sha256")) if isinstance(artifact, Mapping) else ""
            if (
                not isinstance(artifact, Mapping)
                or not isinstance(artifact.get("path"), str)
                or not digest
                or not isinstance(artifact.get("size"), int)
                or isinstance(artifact.get("size"), bool)
                or artifact["size"] <= 0
            ):
                raise WritingSDKDispatchError(
                    "WRITING_RETRY_FEEDBACK_INVALID",
                    f"Step {step} validated retry artifact fingerprint is malformed",
                )
            baseline_artifacts.append(
                {
                    "path": artifact["path"],
                    "sha256": digest,
                    "size": artifact["size"],
                }
            )
        attempt_count = 0
        for collection_name in ("revalidation_attempts", "recovery_attempts"):
            attempts = paper.get(collection_name)
            if not isinstance(attempts, list):
                continue
            attempt_count += sum(
                1 for attempt in attempts if isinstance(attempt, Mapping) and attempt.get("step") == step
            )
        return {
            "step": step,
            "retry_reason": retry_reason,
            "retry_origin": retry_origin,
            "attempt_ordinal": min(attempt_count + 1, 1000),
            "validator": safe_validation,
            "prompt_sha256": prompt_sha256,
            "validated_retry_artifacts": baseline_artifacts,
        }

    @staticmethod
    def _retry_execution_prompt(
        base_prompt: ExactWritingPrompt,
        feedback: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if _RETRY_EXECUTION_ENVELOPE_MARKER in base_prompt.text:
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_INVALID",
                "the immutable base prompt unexpectedly contains the reserved retry-envelope marker",
            )
        expected_outputs = copy.deepcopy(feedback["validated_retry_artifacts"])
        payload = {
            "schema": "unified-paper-orchestrator/writing-retry-execution-envelope/v1",
            "step": feedback["step"],
            "attempt_ordinal": feedback["attempt_ordinal"],
            "retry_reason": feedback["retry_reason"],
            "retry_origin": feedback["retry_origin"],
            "base_prompt_sha256": base_prompt.sha256,
            "fresh_artifact_required": True,
            "expected_output_baseline": expected_outputs,
            "validator": copy.deepcopy(feedback["validator"]),
        }
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        envelope = (
            f"\n\n<<<{_RETRY_EXECUTION_ENVELOPE_MARKER}>>>\n"
            "The exact base prompt above is unchanged and remains authoritative. "
            "This controller-generated envelope adds retry facts only.\n"
            "EDIT every listed expected output in place and fix every validator issue. "
            "Each pre-existing expected output must finish with content SHA-256 different from its baseline.\n"
            "Do not report PIPELINE_ID/STEP_DONE until the files are freshly changed and all listed issues are fixed.\n"
            f"{serialized}\n"
            "<<<END_UPO_WRITING_RETRY_EXECUTION_ENVELOPE>>>\n"
        )
        if len(envelope) > _RETRY_EXECUTION_ENVELOPE_MAX_CHARS:
            raise WritingSDKDispatchError(
                "WRITING_RETRY_FEEDBACK_INVALID",
                "semantic retry execution envelope exceeds its controller length bound",
            )
        return base_prompt.text + envelope, payload

    @staticmethod
    def _manual_step6_execution_prompt(
        base_prompt: ExactWritingPrompt,
        execution_prompt: str,
        decision: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        gate = decision.get("gate")
        gate_profile = str(gate.get("gate_profile") or "") if isinstance(gate, Mapping) else ""
        if gate_profile not in {"evidence_manual_transition", "evidence_auto_trust_transition"}:
            return execution_prompt, None
        policy_transition = gate_profile == "evidence_auto_trust_transition"
        snapshot = gate.get("input_snapshot")
        snapshot_sha256 = (
            _normalized_sha256(snapshot.get("snapshot_sha256")) if isinstance(snapshot, Mapping) else ""
        )
        files = snapshot.get("files") if isinstance(snapshot, Mapping) else None
        if not snapshot_sha256 or not isinstance(files, list):
            raise WritingSDKDispatchError(
                "WRITING_MANUAL_STEP6_BINDING_INVALID",
                "manual Step 6 transition has no stable controller input snapshot",
            )
        safe_files: list[dict[str, Any]] = []
        for item in files:
            digest = _normalized_sha256(item.get("sha256")) if isinstance(item, Mapping) else ""
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("path"), str)
                or not digest
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or item["size"] <= 0
            ):
                raise WritingSDKDispatchError(
                    "WRITING_MANUAL_STEP6_BINDING_INVALID",
                    "manual Step 6 transition contains a malformed input fingerprint",
                )
            safe_files.append({"path": item["path"], "sha256": digest, "size": item["size"]})
        draft_files = [item for item in safe_files if item["path"].endswith("/draft_v2.tex")]
        attestations = [
            item for item in safe_files if item["path"].endswith("/results/manual_data_completion.json")
        ]
        if len(draft_files) != 1 or len(attestations) != 1:
            raise WritingSDKDispatchError(
                "WRITING_MANUAL_STEP6_BINDING_INVALID",
                "manual Step 6 transition must bind one draft_v2 and one manual completion attestation",
            )
        if _MANUAL_STEP6_EXECUTION_ENVELOPE_MARKER in base_prompt.text or (
            _MANUAL_STEP6_EXECUTION_ENVELOPE_MARKER in execution_prompt
        ):
            raise WritingSDKDispatchError(
                "WRITING_MANUAL_STEP6_BINDING_INVALID",
                "the immutable prompt unexpectedly contains the reserved manual-transition envelope marker",
            )
        payload = {
            "schema": (
                "unified-paper-orchestrator/writing-policy-step6-transition/v1"
                if policy_transition
                else "unified-paper-orchestrator/writing-manual-step6-transition/v1"
            ),
            "step": 6,
            "base_prompt_sha256": base_prompt.sha256,
            "gate_profile": gate_profile,
            "gate_snapshot_sha256": snapshot_sha256,
            "paper_id": str(snapshot.get("paper_id") or ""),
            "draft_v2": draft_files[0],
            "manual_completion": attestations[0],
            "transition_mode": "workspace_auto_trust" if policy_transition else "manual_edit",
            "marker_cleanup_owner": "step_6",
            "import_route_files_required": False,
        }
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        transition_description = (
            "the password-protected workspace auto-trust policy transition bound to the current draft_v2 bytes."
            if policy_transition
            else "the user-confirmed manual-edit transition bound to the current draft_v2 bytes."
        )
        route_description = "workspace-policy route" if policy_transition else "manual-edit route"
        envelope = (
            f"\n\n<<<{_MANUAL_STEP6_EXECUTION_ENVELOPE_MARKER}>>>\n"
            "The exact tuned Step 6 prompt above is unchanged. The deterministic controller has validated "
            f"{transition_description}\n"
            f"For this {route_description} only, paper_materials.md and results/verification_manifest.json belong "
            "to the importer route and may be absent. Verification markers and placeholder wording are expected "
            "to remain in draft_v2 before this turn because the tuned Step 6 itself owns their cleanup.\n"
            "Proceed with Step 6 and create draft_v3.tex. Do not refuse solely because those importer-route files "
            "are absent or because cleanup markers remain. Do not alter real numeric values or invent evidence.\n"
            f"{serialized}\n"
            f"<<<END_{_MANUAL_STEP6_EXECUTION_ENVELOPE_MARKER}>>>\n"
        )
        if len(envelope) > _MANUAL_STEP6_EXECUTION_ENVELOPE_MAX_CHARS:
            raise WritingSDKDispatchError(
                "WRITING_MANUAL_STEP6_BINDING_INVALID",
                "manual Step 6 controller envelope exceeds its length bound",
            )
        return execution_prompt + envelope, payload

    @staticmethod
    def _validate_retry_send_baseline(
        feedback: Mapping[str, Any],
        output_baseline: Mapping[str, Any],
    ) -> None:
        if output_baseline.get("mode") != "static" or not isinstance(output_baseline.get("artifacts"), list):
            raise WritingSDKDispatchError(
                "WRITING_RETRY_BASELINE_DRIFT",
                "semantic retry no longer has a static pre-send output baseline",
            )
        actual: list[dict[str, Any]] = []
        for item in output_baseline["artifacts"]:
            digest = _normalized_sha256(item.get("sha256")) if isinstance(item, Mapping) else ""
            if (
                not isinstance(item, Mapping)
                or item.get("state") != "file"
                or item.get("contained") is not True
                or not isinstance(item.get("path"), str)
                or not digest
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or item["size"] <= 0
            ):
                raise WritingSDKDispatchError(
                    "WRITING_RETRY_BASELINE_DRIFT",
                    "semantic retry pre-send output is missing, unsafe, or malformed",
                )
            actual.append({"path": item["path"], "sha256": digest, "size": item["size"]})
        if actual != feedback.get("validated_retry_artifacts"):
            raise WritingSDKDispatchError(
                "WRITING_RETRY_BASELINE_DRIFT",
                "semantic retry output bytes changed after retry validation and before send",
            )

    def _retry_output_baseline(
        self,
        feedback: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Fingerprint the validator-sealed retry outputs, including Step 1.

        Normal Step 1 dispatch watches the whole ``ideas`` directory because
        the paper id is not known before its first Turn. Once identity recovery
        has proven the paper id, a semantic retry already has an exact,
        validator-sealed artifact list. Reusing the original dynamic directory
        baseline would make that legitimate retry impossible to send.
        """

        artifacts: list[dict[str, Any]] = []
        for expected in feedback.get("validated_retry_artifacts", []):
            if not isinstance(expected, Mapping):
                return {"mode": "static", "artifacts": []}
            raw_path = Path(str(expected.get("path") or ""))
            if raw_path.is_absolute() or not raw_path.parts:
                return {"mode": "static", "artifacts": []}
            path = (self.root / raw_path).resolve(strict=False)
            try:
                path.relative_to(self.root)
            except ValueError:
                return {"mode": "static", "artifacts": []}
            artifacts.append(self._output_path_baseline(path))
        return {"mode": "static", "artifacts": artifacts}

    async def _execute_step(self, codex: Any, plan: WritingPaperPlan, step: int) -> bool:
        paper = self._state["papers"][plan.paper_key]
        paper_id = str(paper.get("paper_id") or "")
        source_step = int(workflow_step(plan.workflow_snapshot, step)["source_step"])
        await self._set_paper(plan.paper_key, active_step=step)
        decision = self._normalize_gate_decision(self._gate_ready_for_plan(plan, paper_id, source_step))
        if not decision["ready"]:
            if decision["waitable"] and source_step in _PREREQUISITE_GATE_STEPS:
                await self._wait_for_prerequisite(plan.paper_key, step, decision)
            else:
                await self._block(
                    plan.paper_key,
                    str(decision["blocker"]),
                    str(decision["blocker_message"]),
                    step=step,
                )
            return False
        if source_step == 3 and decision["waitable"]:
            try:
                if not await self._reconcile_completed_step2_artifacts(plan, decision):
                    return False
            except WritingSDKDispatchError as exc:
                await self._block(plan.paper_key, exc.code, str(exc), step=step)
                return False
        if decision["waitable"] and not await self._clear_prerequisite_wait(
            plan.paper_key,
            step,
            decision,
        ):
            return False
        try:
            prompt = plan.prompt_provider(step, paper_id)
            if inspect.isawaitable(prompt):
                prompt = await prompt
            if not isinstance(prompt, ExactWritingPrompt):
                raise WritingSDKDispatchError(
                    "WRITING_PROMPT_PROVIDER_INVALID",
                    "prompt provider must return ExactWritingPrompt",
                )
            prompt.validate()
        except WritingSDKDispatchError as exc:
            await self._block(plan.paper_key, exc.code, str(exc), step=step)
            return False
        except Exception as exc:
            await self._block(
                plan.paper_key,
                "WRITING_PROMPT_PROVIDER_FAILED",
                type(exc).__name__,
                step=step,
            )
            return False

        step_record = paper.setdefault("steps", {}).get(str(step))
        try:
            execution_profile = self._step_execution_profile(plan, step)
            persisted_profile = (
                step_record.get("execution_profile") if isinstance(step_record, Mapping) else None
            )
            if (
                isinstance(persisted_profile, Mapping)
                and str(persisted_profile.get("fingerprint") or "")
                and str(persisted_profile.get("fingerprint") or "")
                != str(execution_profile.get("fingerprint") or "")
            ):
                raise WritingSDKDispatchError(
                    "WRITING_STEP_EXECUTION_PROFILE_MISMATCH",
                    "本 Step 已记录的模型执行配置与论文冻结快照不一致",
                )
            await self._validate_step_execution_profile(codex, execution_profile)
        except WritingSDKDispatchError as exc:
            await self._block(plan.paper_key, exc.code, str(exc), step=step)
            return False
        try:
            retry_feedback = self._retry_feedback_for_step(paper, step)
        except WritingSDKDispatchError as exc:
            await self._block(plan.paper_key, exc.code, str(exc), step=step)
            return False
        if retry_feedback is not None and retry_feedback["prompt_sha256"] != prompt.sha256:
            await self._block(
                plan.paper_key,
                "WRITING_RETRY_PROMPT_HASH_MISMATCH",
                "semantic retry base prompt SHA-256 differs from its validated retry binding",
                step=step,
            )
            return False
        if (
            retry_feedback is None
            and isinstance(step_record, dict)
            and step_record.get("prompt_sha256") not in {None, prompt.sha256}
        ):
            await self._block(
                plan.paper_key,
                "WRITING_PROMPT_CHANGED_AFTER_JOURNAL",
                "external prompt bytes changed after this step was journaled",
                step=step,
            )
            return False
        if (
            isinstance(step_record, dict)
            and isinstance(step_record.get("source_prompt"), dict)
            and step_record.get("source_prompt") != prompt.source_prompt
        ):
            await self._block(
                plan.paper_key,
                "WRITING_SOURCE_PROMPT_CHANGED",
                "the tuned writing-step prompt fingerprint differs from the journaled bytes",
                step=step,
            )
            return False
        execution_prompt = prompt.text
        execution_fields: dict[str, Any] = {}
        if retry_feedback is not None:
            try:
                execution_prompt, retry_envelope = self._retry_execution_prompt(prompt, retry_feedback)
            except WritingSDKDispatchError as exc:
                await self._block(plan.paper_key, exc.code, str(exc), step=step)
                return False
            persisted_envelope = step_record.get("retry_execution_envelope") if isinstance(step_record, dict) else None
            if persisted_envelope is not None and persisted_envelope != retry_envelope:
                await self._block(
                    plan.paper_key,
                    "WRITING_RETRY_FEEDBACK_INVALID",
                    "persisted semantic retry envelope differs from the deterministic controller envelope",
                    step=step,
                )
                return False
            execution_fields["retry_execution_envelope"] = retry_envelope
        try:
            execution_prompt, manual_step6_envelope = self._manual_step6_execution_prompt(
                prompt,
                execution_prompt,
                decision,
            )
        except WritingSDKDispatchError as exc:
            await self._block(plan.paper_key, exc.code, str(exc), step=step)
            return False
        if manual_step6_envelope is not None:
            persisted_manual_envelope = (
                step_record.get("manual_step6_execution_envelope") if isinstance(step_record, dict) else None
            )
            if persisted_manual_envelope is not None and persisted_manual_envelope != manual_step6_envelope:
                await self._block(
                    plan.paper_key,
                    "WRITING_MANUAL_STEP6_BINDING_INVALID",
                    "persisted manual Step 6 envelope differs from the deterministic controller envelope",
                    step=step,
                )
                return False
            execution_fields["manual_step6_execution_envelope"] = manual_step6_envelope
        if execution_fields:
            execution_prompt_sha256 = _sha256_text(execution_prompt)
            persisted_execution_sha256 = (
                str(step_record.get("execution_prompt_sha256") or "") if isinstance(step_record, dict) else ""
            )
            if persisted_execution_sha256 and persisted_execution_sha256 != execution_prompt_sha256:
                await self._block(
                    plan.paper_key,
                    "WRITING_RETRY_FEEDBACK_INVALID"
                    if retry_feedback is not None
                    else "WRITING_MANUAL_STEP6_BINDING_INVALID",
                    "persisted execution SHA-256 differs from the deterministic controller envelope",
                    step=step,
                )
                return False
            execution_fields["execution_prompt_sha256"] = execution_prompt_sha256
        inputs = self._input_fingerprints(paper_id, step, plan)
        if inputs is None:
            await self._block(
                plan.paper_key,
                "WRITING_INPUT_CONTRACT_MISSING",
                f"required inputs for Step {step} are missing or unsafe",
                step=step,
            )
            return False
        prompt_fields: dict[str, Any] = {"prompt_sha256": prompt.sha256}
        if prompt.source_prompt is not None:
            prompt_fields["source_prompt"] = copy.deepcopy(prompt.source_prompt)
        await self._set_step(
            plan.paper_key,
            step,
            state="dispatching",
            input_fingerprints=inputs,
            execution_profile=copy.deepcopy(execution_profile),
            blocker="",
            blocker_message="",
            **prompt_fields,
            **execution_fields,
        )
        try:
            thread = await self._ensure_thread(codex, plan)
        except WritingSDKDispatchError as exc:
            if exc.code != _PROGRESS_CALLBACK_FAILED:
                await self._block(plan.paper_key, exc.code, str(exc), step=step)
            return False
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._block(
                plan.paper_key,
                "WRITING_THREAD_CREATION_UNCERTAIN",
                type(exc).__name__,
                step=step,
            )
            return False
        thread_id = str(getattr(thread, "id", "") or "")

        while True:
            try:
                async with self.permit_provider.permit(
                    codex,
                    thread_id,
                    weight=self.step_weights.get(step, WRITING_SDK_SESSION_WEIGHT),
                ) as permit:
                    history = await thread.read(include_turns=True)
                    baseline_ids = [str(getattr(turn, "id", "") or "") for turn in _thread_turns(history)]
                    configured_limit = int(execution_profile.get("hard_token_limit") or 0)
                    hard_token_limit = configured_limit or None
                    token_baseline: int | None = None
                    if hard_token_limit is not None:
                        token_baseline = self._thread_cumulative_usage_baseline(
                            plan.paper_key,
                            step,
                            thread_id,
                        )
                        if token_baseline is None:
                            if baseline_ids:
                                raise WritingSDKDispatchError(
                                    "WRITING_STEP_TOKEN_BASELINE_MISSING",
                                    f"Step {step} cannot derive a cumulative token baseline for an existing thread",
                                )
                            token_baseline = 0
                    output_baseline = (
                        self._retry_output_baseline(retry_feedback)
                        if retry_feedback is not None
                        else self._step_output_baseline(paper_id, step, plan)
                    )
                    baseline_problem = self._output_baseline_problem(output_baseline)
                    if baseline_problem is not None:
                        raise WritingSDKDispatchError(*baseline_problem)
                    if retry_feedback is not None:
                        assert output_baseline is not None
                        self._validate_retry_send_baseline(
                            retry_feedback,
                            output_baseline,
                        )
                    prerequisite_gate: dict[str, Any] | None = None
                    prerequisite_token = ""
                    if source_step in _PREREQUISITE_GATE_STEPS and decision["waitable"]:
                        refreshed_gate = self._normalize_gate_decision(self._gate_ready_for_plan(plan, paper_id, source_step))
                        if not refreshed_gate["ready"]:
                            await self._wait_for_prerequisite(plan.paper_key, step, refreshed_gate)
                            return False
                        prerequisite_gate = copy.deepcopy(dict(refreshed_gate["gate"]))
                        prerequisite_token = self._gate_snapshot_token(refreshed_gate)
                        if not prerequisite_token:
                            raise WritingSDKDispatchError(
                                "WRITING_PREREQUISITE_GATE_BINDING_MISSING",
                                f"READY prerequisite for Step {step} has no stable input snapshot",
                            )
                        inputs = self._input_fingerprints(paper_id, step, plan)
                        if inputs is None:
                            raise WritingSDKDispatchError(
                                "WRITING_INPUT_CONTRACT_MISSING",
                                f"required inputs for Step {step} are missing or unsafe",
                            )
                    permit_snapshot = dict(permit) if isinstance(permit, Mapping) else {}
                    starting_fields: dict[str, Any] = {
                        "state": "starting",
                        "input_fingerprints": inputs,
                        "baseline_turn_ids": baseline_ids,
                        "output_baseline": output_baseline,
                        "permit": permit_snapshot,
                        "blocker": "",
                        "blocker_message": "",
                        "started_at": now_iso(),
                        "thread_id": thread_id,
                        "execution_profile": copy.deepcopy(execution_profile),
                        **prompt_fields,
                        **execution_fields,
                    }
                    if hard_token_limit is not None and token_baseline is not None:
                        starting_fields["token_budget"] = {
                            "hard_limit_tokens": hard_token_limit,
                            "baseline_cumulative_total_tokens": token_baseline,
                            "observed_cumulative_total_tokens": token_baseline,
                            "consumed_step_tokens": 0,
                        }
                    if prerequisite_gate is not None:
                        starting_fields["prerequisite_gate"] = prerequisite_gate
                    await self._set_step(plan.paper_key, step, **starting_fields)
                    await self._set_paper(
                        plan.paper_key,
                        status="starting",
                        blocker="",
                        blocker_message="",
                        active_step=step,
                    )
                    if await self._pause_for_open_batch_circuit(plan.paper_key, step):
                        return False
                    prompt.validate()
                    if prerequisite_gate is not None:
                        before_send = self._normalize_gate_decision(self._gate_ready_for_plan(plan, paper_id, source_step))
                        if not before_send["ready"]:
                            await self._wait_for_prerequisite(plan.paper_key, step, before_send)
                            return False
                        if self._gate_snapshot_token(before_send) != prerequisite_token:
                            await self._wait_for_prerequisite(
                                plan.paper_key,
                                step,
                                self._gate_change_wait_decision(
                                    before_send,
                                    message=(
                                        f"Step {step} prerequisite bytes changed before send; "
                                        "the next run will validate the new snapshot"
                                    ),
                                ),
                            )
                            return False
                    self._validate_prompt_sha256_manifest(plan.workflow_snapshot)
                    if not await self._continue_before_step(plan.paper_key, step):
                        return False
                    if retry_feedback is not None:
                        final_output_baseline = self._retry_output_baseline(
                            retry_feedback
                        )
                        final_baseline_problem = self._output_baseline_problem(final_output_baseline)
                        if final_baseline_problem is not None:
                            raise WritingSDKDispatchError(
                                "WRITING_RETRY_BASELINE_DRIFT",
                                final_baseline_problem[1],
                            )
                        assert final_output_baseline is not None
                        self._validate_retry_send_baseline(retry_feedback, final_output_baseline)
                    try:
                        turn_handle = await thread.turn(
                            execution_prompt,
                            cwd=str(self.root),
                            approval_mode=ApprovalMode.deny_all,
                            sandbox=Sandbox.workspace_write,
                            **self._turn_execution_kwargs(execution_profile),
                        )
                    except BaseException as exc:
                        await self._set_step(
                            plan.paper_key,
                            step,
                            state="send_uncertain",
                            send_error=type(exc).__name__,
                        )
                        raise
                    turn_id = str(getattr(turn_handle, "id", "") or "")
                    await self._set_step(plan.paper_key, step, state="running", turn_id=turn_id)
                    await self._set_paper(plan.paper_key, status="running", active_step=step)
                    # The dispatcher journal already knows that the Turn is live,
                    # but the durable controller projection used to remain
                    # "queued" until the whole Step completed. Publish a
                    # best-effort lifecycle event after the Turn has a stable ID
                    # so status visibility never becomes part of Turn success.
                    await self._notify_progress(
                        "step_started",
                        plan.paper_key,
                        fail_closed=False,
                    )
                    if hard_token_limit is not None and token_baseline is not None:
                        result = await self._run_turn_with_budget(
                            turn_handle,
                            paper_key=plan.paper_key,
                            step=step,
                            baseline_total=token_baseline,
                            hard_limit=hard_token_limit,
                        )
                    else:
                        result = await turn_handle.run()
                await self._complete_turn(plan, step, result, inputs, output_baseline)
                return True
            except GlobalSessionBudgetExhausted as exc:
                await self._set_step(
                    plan.paper_key,
                    step,
                    state="waiting_capacity",
                    capacity={
                        "limit": exc.limit,
                        "occupied": exc.active,
                        "requested_weight": exc.requested_weight,
                    },
                )
                await self._set_paper(
                    plan.paper_key,
                    status="waiting_capacity",
                    blocker=exc.code,
                    blocker_message=str(exc),
                    active_step=step,
                )
                waiter = getattr(self.permit_provider, "wait_for_capacity", None)
                try:
                    if callable(waiter):
                        waiting = waiter(exc)
                        if inspect.isawaitable(waiting):
                            await waiting
                    else:
                        await asyncio.sleep(_EXTERNAL_CAPACITY_RECHECK_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as wait_exc:
                    await self._block(
                        plan.paper_key,
                        "GLOBAL_CODEX_SESSION_CAPACITY_WAIT_FAILED",
                        f"capacity wait failed: {type(wait_exc).__name__}",
                        step=step,
                    )
                    return False
                if await self._pause_for_open_batch_circuit(plan.paper_key, step):
                    return False
                if not await self._continue_before_step(plan.paper_key, step):
                    return False
                continue
            except WritingSDKDispatchError as exc:
                if exc.code != _PROGRESS_CALLBACK_FAILED:
                    await self._block(plan.paper_key, exc.code, str(exc), step=step)
                return False
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                recovered = await self._recover_step(codex, plan, step)
                if not recovered:
                    await self._block(
                        plan.paper_key,
                        "WRITING_TURN_EXECUTION_FAILED",
                        type(exc).__name__,
                        step=step,
                    )
                return False

    async def _run_step(
        self,
        codex: Any,
        plan: WritingPaperPlan,
        step: int,
        semaphore: asyncio.Semaphore,
    ) -> bool:
        if step == 1 and not plan.reserved_pipeline_prefix:
            async with self._legacy_step_one_lock:
                async with semaphore:
                    if await self._pause_for_open_batch_circuit(plan.paper_key, step):
                        return False
                    if not await self._continue_before_step(plan.paper_key, step):
                        return False
                    await self._clear_expired_batch_pause(plan.paper_key, step)
                    return await self._execute_step(codex, plan, step)
        async with semaphore:
            if await self._pause_for_open_batch_circuit(plan.paper_key, step):
                return False
            if not await self._continue_before_step(plan.paper_key, step):
                return False
            await self._clear_expired_batch_pause(plan.paper_key, step)
            return await self._execute_step(codex, plan, step)

    async def _run_paper(self, codex: Any, plan: WritingPaperPlan, semaphore: asyncio.Semaphore) -> None:
        paper = self._state["papers"][plan.paper_key]
        if paper.get("status") == "blocked":
            return
        next_step = int(paper.get("completed_step") or 0) + 1
        if next_step > plan.run_until_step:
            await self._set_paper(plan.paper_key, status="complete")
            return
        if await self._recover_step(codex, plan, next_step):
            paper = self._state["papers"][plan.paper_key]
            if str(paper.get("status") or "") != "queued":
                return
        while int(self._state["papers"][plan.paper_key].get("completed_step") or 0) < plan.run_until_step:
            step = int(self._state["papers"][plan.paper_key].get("completed_step") or 0) + 1
            if await self._pause_for_open_batch_circuit(plan.paper_key, step):
                return
            if not await self._run_step(codex, plan, step, semaphore):
                return

    async def run_until_idle(self, plans: list[WritingPaperPlan]) -> dict[str, Any]:
        """Run until every plan is complete or needs an external event/action.

        New turns wait on in-process permit release, with low-frequency SDK
        snapshots for external capacity. Active crash-recovery turns are read
        at the same low-frequency style until terminal or explicitly paused.
        """

        normalized = list(plans)
        path_key = os.path.normcase(os.path.abspath(self.lock_path))
        async with self._run_lock:
            with _ACTIVE_DISPATCH_PATHS_LOCK:
                if path_key in _ACTIVE_DISPATCH_PATHS:
                    raise WritingSDKDispatchError(
                        "WRITING_SDK_DISPATCHER_ALREADY_RUNNING",
                        "one deterministic local dispatcher already owns this journal",
                    )
                _ACTIVE_DISPATCH_PATHS.add(path_key)
            try:
                with FileLock(self.lock_path, stale_seconds=7 * 24 * 60 * 60):
                    if self.pre_dispatch_guard is not None:
                        self.pre_dispatch_guard()
                    self._state = self._load_state()
                    self._start_batch_circuit_unlocked()
                    self._register_plans(normalized)
                    async with self.codex_factory() as codex:
                        semaphore = asyncio.Semaphore(self.max_parallel_papers)
                        await asyncio.gather(
                            *(
                                self._run_paper(codex, plan, semaphore)
                                for plan in sorted(normalized, key=lambda item: item.paper_key.casefold())
                            )
                        )
                    await self._finish_batch_circuit()
                    papers = {
                        plan.paper_key: dict(self._state["papers"][plan.paper_key])
                        for plan in sorted(normalized, key=lambda item: item.paper_key.casefold())
                    }
                    return {
                        "ok": all(item.get("status") == "complete" for item in papers.values()),
                        "schema": WRITING_SDK_JOURNAL_SCHEMA,
                        "batch_circuit": copy.deepcopy(self._state["batch_circuit"]),
                        "papers": papers,
                    }
            finally:
                with _ACTIVE_DISPATCH_PATHS_LOCK:
                    _ACTIVE_DISPATCH_PATHS.discard(path_key)


__all__ = [
    "ExactWritingPrompt",
    "GLOBAL_CODEX_SESSION_LIMIT",
    "GlobalSessionBudgetExhausted",
    "ProgressCallback",
    "SDKGlobalPermitProvider",
    "ShouldContinueCallback",
    "WRITING_SDK_JOURNAL_SCHEMA",
    "WRITING_SDK_AGENT_MAX_DEPTH",
    "WRITING_SDK_AGENT_MAX_THREADS",
    "WRITING_SDK_MAX_CHILD_THREADS",
    "WRITING_SDK_SESSION_WEIGHT",
    "WRITING_PROMPT_MANIFEST_MISMATCH",
    "WritingPaperPlan",
    "WritingSDKDispatchError",
    "WritingSDKDispatcher",
    "existing_writing_prompt_provider",
]
