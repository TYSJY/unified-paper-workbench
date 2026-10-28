from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any, Protocol

from ..config import LEGACY_UNIFIED_STATE_PATH
from ..contracts import ApprovalPolicy, ResultStatus, TaskEnvelope, TaskResult
from .base import ExecutorDescriptor


AUTOMATION_CONTROL_ENV = "UNIFIED_V2_ALLOW_LEGACY_AUTOMATION_CONTROL"
AUTOMATION_STAGES = frozenset({"writing", "figures", "journal_author", "submission"})
AUTOMATION_ACTIONS = frozenset(
    {
        "legacy.automation.status",
        "legacy.automation.start",
        "legacy.automation.pause",
    }
)
_KNOWN_AUTOMATION_STATUSES = frozenset({"ACTIVE", "PAUSED", "MISSING"})
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _stored_execution_mode() -> str:
    """Read the global execution owner without exposing provider secrets."""

    from unified_paper_orchestrator.ai_provider_store import AIProviderStore

    return str(AIProviderStore().load_settings().execution_mode or "").strip()


class AutomationController(Protocol):
    def read_statuses(self, stage: str) -> list[dict[str, Any]]: ...

    def set_enabled(
        self,
        stage: str,
        enabled: bool,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def _blocked(code: str, message: str, **data: Any) -> TaskResult:
    return TaskResult(
        status=ResultStatus.BLOCKED,
        blocker_code=code,
        message=message,
        data=data,
    )


def _compact_statuses(stage: str, configs: object) -> dict[str, Any]:
    safe_configs = configs if isinstance(configs, list) else []
    statuses: list[dict[str, Any]] = []
    active_count = 0
    for config in safe_configs:
        if not isinstance(config, dict):
            continue
        raw_status = str(config.get("status") or "").strip().upper()
        status = raw_status if raw_status in _KNOWN_AUTOMATION_STATUSES else "UNKNOWN"
        if status == "ACTIVE":
            active_count += 1
        statuses.append(
            {
                "id": str(config.get("id") or "")[:128],
                "exists": bool(config.get("exists", False)),
                "status": status,
            }
        )
    return {
        "target_stage": stage,
        "read_only": True,
        "config_count": len(statuses),
        "active_count": active_count,
        "statuses": statuses,
    }


class LegacyOrchestratorExecutor:
    name = "legacy"

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            name=self.name,
            available=True,
            kind="v1_compatibility",
            detail="Read-only V1 projection compatibility; legacy.apply is permanently disabled.",
            side_effecting=False,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        if task.action == "legacy.apply":
            return _blocked(
                "LEGACY_WRITES_DISABLED",
                "legacy.apply is permanently disabled; V1 remains the production execution owner.",
            )
        if task.action != "legacy.plan":
            return _blocked(
                "LEGACY_ACTION_UNSUPPORTED",
                "The legacy projection executor only supports legacy.plan.",
            )
        if not LEGACY_UNIFIED_STATE_PATH.is_file():
            return _blocked(
                "LEGACY_PROJECTION_MISSING",
                f"V1 unified projection does not exist: {LEGACY_UNIFIED_STATE_PATH}",
            )
        try:
            state = json.loads(LEGACY_UNIFIED_STATE_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return _blocked(
                "LEGACY_PROJECTION_INVALID",
                "V1 unified projection could not be read as valid JSON.",
            )
        papers = state.get("papers", {}) if isinstance(state, dict) else {}
        record = papers.get(task.paper_id) if isinstance(papers, dict) else None
        if not isinstance(record, dict):
            return _blocked(
                "LEGACY_PAPER_NOT_FOUND",
                f"paper is not present in the V1 projection: {task.paper_id}",
            )
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="read-only legacy projection plan completed",
            data={
                "applied": False,
                "paper_id": task.paper_id,
                "current_stage": record.get("current_stage"),
                "next_action": record.get("next_action"),
                "blocked_reason": record.get("blocked_reason"),
                "source": str(LEGACY_UNIFIED_STATE_PATH),
            },
        )


class LegacyCodexAutomationExecutor(LegacyOrchestratorExecutor):
    """Controlled adapter for V1 projection reads and scheduler Automation toggles."""

    name = "legacy_codex"

    def __init__(
        self,
        controller: AutomationController | None = None,
        *,
        allow_control: bool = False,
        execution_mode_reader: Callable[[], str] | None = None,
        retire_writing_start: bool | None = None,
    ) -> None:
        controller_was_injected = controller is not None
        if controller is None:
            from unified_paper_orchestrator.automation import SchedulerAutomationController

            controller = SchedulerAutomationController()
        self._controller = controller
        self._allow_control = allow_control or os.environ.get(AUTOMATION_CONTROL_ENV, "") == "1"
        self._execution_mode_reader = execution_mode_reader or _stored_execution_mode
        self._retire_writing_start = (
            not controller_was_injected if retire_writing_start is None else bool(retire_writing_start)
        )

    def descriptor(self) -> ExecutorDescriptor:
        control = "enabled" if self._allow_control else "disabled"
        return ExecutorDescriptor(
            name=self.name,
            available=True,
            kind="v1_codex_automation_compatibility",
            detail=(
                "Read-only V1 planning and Automation status are available. "
                f"Approved start/pause control is {control}; writing start and legacy.apply are permanently disabled."
            ),
            side_effecting=True,
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        if task.action == "legacy.apply":
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_CODEX_GATEWAY_WRITES_DISABLED",
                    "legacy.apply is permanently disabled; V1 remains the production execution owner.",
                )
            )
        if task.action == "legacy.plan":
            return self._execute_plan(task)
        if task.action not in AUTOMATION_ACTIONS:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_CODEX_ACTION_UNSUPPORTED",
                    "Unsupported legacy Codex Automation action.",
                )
            )

        target_stage = str(task.payload.get("target_stage") or "").strip()
        if target_stage not in AUTOMATION_STAGES:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_STAGE_INVALID",
                    "target_stage must be one of: writing, figures, journal_author, submission.",
                )
            )
        capability_error = self._validate_capabilities(task)
        if capability_error is not None:
            return self._with_port_metadata(capability_error)
        if task.action == "legacy.automation.status":
            return self._automation_status(target_stage)

        if self._retire_writing_start and task.action == "legacy.automation.start" and target_stage == "writing":
            return self._with_port_metadata(
                _blocked(
                    "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED",
                    (
                        "The model-driven writing Automation scheduler is retired. "
                        "Use the EXE's deterministic local SDK scheduler."
                    ),
                    target_stage=target_stage,
                )
            )

        if task.approval_policy != ApprovalPolicy.REQUIRED:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_APPROVAL_REQUIRED",
                    "Automation start/pause requires approval_policy=required.",
                    target_stage=target_stage,
                )
            )
        if not self._allow_control:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_CONTROL_DISABLED",
                    (
                        "Automation control is disabled. Construct with allow_control=True or set "
                        f"{AUTOMATION_CONTROL_ENV}=1 for this process."
                    ),
                    target_stage=target_stage,
                )
            )
        enabled = task.action == "legacy.automation.start"
        if enabled:
            try:
                execution_mode = str(self._execution_mode_reader() or "").strip()
            except Exception:
                return self._with_port_metadata(
                    _blocked(
                        "AI_EXECUTION_MODE_STATE_UNREADABLE",
                        "AI execution mode could not be read; legacy start was stopped.",
                        target_stage=target_stage,
                    )
                )
            if execution_mode != "legacy_codex":
                return self._with_port_metadata(
                    _blocked(
                        "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE",
                        "API key mode cannot start a V1 Codex Automation or fall back implicitly.",
                        target_stage=target_stage,
                    )
                )
        return self._set_automation_enabled(target_stage, enabled=enabled)

    def reconcile(self, task: TaskEnvelope) -> TaskResult:
        """Verify the desired Automation state without repeating a control write."""

        if task.action == "legacy.automation.status":
            return self.execute(task)
        if task.action not in {"legacy.automation.start", "legacy.automation.pause"}:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_RECOVERY_DENIED",
                    "Only Automation status/start/pause tasks can be reconciled.",
                )
            )
        target_stage = str(task.payload.get("target_stage") or "").strip()
        if target_stage not in AUTOMATION_STAGES:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_STAGE_INVALID",
                    "Automation reconciliation target stage is invalid.",
                )
            )
        if self._retire_writing_start and task.action == "legacy.automation.start" and target_stage == "writing":
            return self._with_port_metadata(
                _blocked(
                    "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED",
                    "The retired writing Automation cannot be adopted as a successful recovery outcome.",
                    target_stage=target_stage,
                )
            )
        capability_error = self._validate_capabilities(task)
        if capability_error is not None:
            return self._with_port_metadata(capability_error)
        try:
            automation = _compact_statuses(
                target_stage,
                self._controller.read_statuses(target_stage),
            )
        except Exception:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_STATUS_FAILED",
                    "Automation state could not be read for reconciliation.",
                    target_stage=target_stage,
                )
            )
        desired_status = "ACTIVE" if task.action.endswith(".start") else "PAUSED"
        statuses = automation.get("statuses", [])
        proven = bool(statuses) and all(
            isinstance(item, dict) and item.get("exists") is True and item.get("status") == desired_status
            for item in statuses
        )
        if not proven:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_RECOVERY_UNPROVEN",
                    "Current Automation state does not prove the approved control outcome.",
                    target_stage=target_stage,
                    desired_status=desired_status,
                    automation=automation,
                )
            )
        return self._with_port_metadata(
            TaskResult(
                status=ResultStatus.COMPLETED,
                message="Automation control outcome was reconciled by read-only status.",
                data={
                    "target_stage": target_stage,
                    "desired_status": desired_status,
                    "reconciled_from_status": True,
                    "automation": automation,
                },
            )
        )

    def _execute_plan(self, task: TaskEnvelope) -> TaskResult:
        result = super().execute(task)
        if result.status != ResultStatus.COMPLETED:
            return self._with_port_metadata(result)
        current_stage = str(result.data.get("current_stage") or "").strip()
        if current_stage not in AUTOMATION_STAGES:
            result.data["automation"] = {
                "target_stage": current_stage,
                "read_only": True,
                "available": False,
                "reason": "LEGACY_AUTOMATION_STAGE_UNAVAILABLE",
            }
            return self._with_port_metadata(result)
        try:
            result.data["automation"] = _compact_statuses(
                current_stage,
                self._controller.read_statuses(current_stage),
            )
        except Exception:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_STATUS_FAILED",
                    "Automation status could not be read.",
                    target_stage=current_stage,
                )
            )
        return self._with_port_metadata(result)

    def _validate_capabilities(self, task: TaskEnvelope) -> TaskResult | None:
        if set(task.payload) != {"target_stage", "operation_id"}:
            return _blocked(
                "LEGACY_AUTOMATION_CONTRACT_INVALID",
                "Automation payload does not match the typed action contract.",
            )
        if task.network_access or task.allowed_tools or task.writable_roots:
            return _blocked(
                "LEGACY_AUTOMATION_CAPABILITIES_DENIED",
                "Automation actions require zero network, tools, and writable roots.",
            )
        operation_id = str(task.payload.get("operation_id") or "")
        if task.action in {"legacy.automation.start", "legacy.automation.pause"}:
            operation = task.action.rsplit(".", 1)[-1]
            target_stage = str(task.payload.get("target_stage") or "")
            if (
                _SAFE_OPERATION_ID.fullmatch(operation_id) is None
                or task.idempotency_key != f"legacy-automation:{operation}:{target_stage}:{operation_id}"
            ):
                return _blocked(
                    "LEGACY_AUTOMATION_IDEMPOTENCY_INVALID",
                    "Automation control operation ID is not bound to its request.",
                )
        elif operation_id:
            return _blocked(
                "LEGACY_AUTOMATION_CONTRACT_INVALID",
                "Read-only Automation status cannot include an operation ID.",
            )
        return None

    def _automation_status(self, target_stage: str) -> TaskResult:
        try:
            automation = _compact_statuses(
                target_stage,
                self._controller.read_statuses(target_stage),
            )
        except Exception:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_STATUS_FAILED",
                    "Automation status could not be read.",
                    target_stage=target_stage,
                )
            )
        return self._with_port_metadata(
            TaskResult(
                status=ResultStatus.COMPLETED,
                message="read-only Automation status completed",
                data={"automation": automation},
            )
        )

    def _set_automation_enabled(self, target_stage: str, *, enabled: bool) -> TaskResult:
        try:
            update = self._controller.set_enabled(target_stage, enabled)
        except Exception:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_CONTROL_FAILED",
                    "Automation control operation failed.",
                    target_stage=target_stage,
                )
            )
        updated = update.get("updated", []) if isinstance(update, dict) else []
        warnings = update.get("warnings", []) if isinstance(update, dict) else []
        safe_updated = updated if isinstance(updated, list) else []
        safe_warnings = warnings if isinstance(warnings, list) else []
        if not safe_updated:
            return self._with_port_metadata(
                _blocked(
                    "LEGACY_AUTOMATION_UNAVAILABLE",
                    "No controllable legacy Automation configuration was found.",
                    target_stage=target_stage,
                )
            )
        return self._with_port_metadata(
            TaskResult(
                status=ResultStatus.COMPLETED,
                message=f"Automation {'started' if enabled else 'paused'} for {target_stage}.",
                data={
                    "target_stage": target_stage,
                    "requested_enabled": enabled,
                    "updated_count": len(safe_updated),
                    "changed_count": sum(
                        1 for item in safe_updated if isinstance(item, dict) and item.get("changed") is True
                    ),
                    "warning_count": len(safe_warnings),
                },
            )
        )

    def _with_port_metadata(self, result: TaskResult) -> TaskResult:
        result.data = {
            **result.data,
            "compatibility_port": self.name,
            "production_execution_owner": "v1_codex_automation",
            "gateway_business_write_enabled": False,
            "automation_control_enabled": self._allow_control,
        }
        return result
