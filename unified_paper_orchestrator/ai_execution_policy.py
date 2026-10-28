from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


LEGACY_CODEX_MODE = "legacy_codex"
API_KEY_MODE = "api_key"
EXECUTION_MODES = frozenset({LEGACY_CODEX_MODE, API_KEY_MODE})
API_READY_CAPABILITY_STATUSES = frozenset(
    {
        "compatible",
        "ready",
        "responses_compatible",
        "verified",
    }
)
NONTERMINAL_GATEWAY_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_approval",
        "waiting_human",
    }
)


class ExecutionModePolicyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_legacy_execution_mode(execution_mode: str, *, action: str = "") -> None:
    """Reject a production SDK-workflow mutation while API mode owns the UI.

    API mode deliberately has no implicit fallback to the legacy Codex
    account mode.  The current API product slice is the Prompt 07
    private-staging review, so full four-stage controls stay fail-closed until
    the API path is explicitly implemented for those workflows.
    """

    if normalize_execution_mode(execution_mode) == LEGACY_CODEX_MODE:
        return
    label = str(action or "该操作").strip() or "该操作"
    raise ExecutionModePolicyError(
        "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE",
        f"{label}需要 Codex 账号模式下的本地 SDK 工作流，当前 API 密钥模式不会自动切换执行来源。"
        "如需执行完整写作、画图、选刊作者或智能投稿流程，请先在“AI执行与API”中切换到"
        "“Codex 账号模式（完整流程）”；API 模式当前可用于连接检测和论文私有暂存审阅。",
    )


@dataclass(frozen=True)
class ExecutionModeSwitchDecision:
    current_mode: str
    requested_mode: str
    allowed: bool
    changed: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def require_allowed(self) -> None:
        if not self.allowed:
            raise ExecutionModePolicyError(
                "AI_EXECUTION_MODE_SWITCH_BLOCKED",
                "；".join(self.blockers) or "当前不能切换 AI 执行方式。",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_mode": self.current_mode,
            "requested_mode": self.requested_mode,
            "allowed": self.allowed,
            "changed": self.changed,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


def normalize_execution_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in EXECUTION_MODES:
        raise ExecutionModePolicyError(
            "AI_EXECUTION_MODE_INVALID",
            f"不支持的 AI 执行方式：{value!r}",
        )
    return normalized


def evaluate_execution_mode_switch(
    *,
    current_mode: str,
    requested_mode: str,
    legacy_scheduler_statuses: Mapping[str, Mapping[str, Any]] | None = None,
    gateway_task_counts: Mapping[str, int] | None = None,
    active_profile: Mapping[str, Any] | None = None,
    active_profile_has_key: bool = False,
) -> ExecutionModeSwitchDecision:
    current = normalize_execution_mode(current_mode)
    requested = normalize_execution_mode(requested_mode)
    if current == requested:
        return ExecutionModeSwitchDecision(
            current_mode=current,
            requested_mode=requested,
            allowed=True,
            changed=False,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    active_legacy = _active_legacy_stages(legacy_scheduler_statuses or {})
    if active_legacy:
        blockers.append(
            "请先暂停仍在运行的 Codex 调度：" + "、".join(active_legacy)
        )

    active_gateway = _active_gateway_counts(gateway_task_counts or {})
    if active_gateway:
        rendered = "、".join(f"{status}={count}" for status, count in active_gateway)
        blockers.append("请先处理尚未终结的 API 任务：" + rendered)

    if requested == API_KEY_MODE:
        profile = dict(active_profile or {})
        if not str(profile.get("profile_id") or "").strip():
            blockers.append("API 密钥模式需要先选择一个服务配置。")
        if not active_profile_has_key:
            blockers.append("所选 API 服务尚未保存密钥。")
        if str(profile.get("wire_api") or "responses").strip().lower() != "responses":
            blockers.append("当前 Codex SDK 只支持 Responses 协议。")
        capability = str(profile.get("capability_status") or "untested").strip().lower()
        if capability not in API_READY_CAPABILITY_STATUSES:
            blockers.append("所选 API 服务尚未通过 Codex/Responses 兼容性测试。")
    else:
        observed = [
            str(item.get("status") or "MISSING").strip().upper()
            for item in (legacy_scheduler_statuses or {}).values()
            if isinstance(item, Mapping)
        ]
        if not observed or all(status == "MISSING" for status in observed):
            warnings.append(
                "未检测到历史 Codex Automation 回退配置；这不影响当前四阶段 V2 SDK 执行，"
                "但旧版本回退控制不可用。"
            )

    return ExecutionModeSwitchDecision(
        current_mode=current,
        requested_mode=requested,
        allowed=not blockers,
        changed=True,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def _active_legacy_stages(
    statuses: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    active: list[str] = []
    for stage, record in statuses.items():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("status") or "").strip().upper() == "ACTIVE":
            active.append(str(stage))
    return sorted(active)


def _active_gateway_counts(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    active: list[tuple[str, int]] = []
    for status in sorted(NONTERMINAL_GATEWAY_STATUSES):
        try:
            count = int(counts.get(status, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            active.append((status, count))
    return active


def mode_snapshot_fields(
    *,
    execution_mode: str,
    provider_profile_id: str = "",
    provider_revision: int = 0,
) -> dict[str, Any]:
    mode = normalize_execution_mode(execution_mode)
    profile_id = str(provider_profile_id or "").strip()
    if mode == API_KEY_MODE and not profile_id:
        raise ExecutionModePolicyError(
            "AI_PROVIDER_PROFILE_REQUIRED",
            "API 密钥任务必须固定服务配置。",
        )
    return {
        "execution_mode": mode,
        "provider_profile_id": profile_id if mode == API_KEY_MODE else "",
        "provider_revision": max(0, int(provider_revision or 0)) if mode == API_KEY_MODE else 0,
        "automatic_cross_mode_fallback": False,
    }


def require_no_cross_mode_fallback(route: Mapping[str, Any]) -> None:
    if bool(route.get("automatic_cross_mode_fallback")):
        raise ExecutionModePolicyError(
            "AI_CROSS_MODE_FALLBACK_FORBIDDEN",
            "禁止在 API 密钥模式与 Codex 账号模式之间自动回退。",
        )
    mode_snapshot_fields(
        execution_mode=str(route.get("execution_mode") or ""),
        provider_profile_id=str(route.get("provider_profile_id") or ""),
        provider_revision=int(route.get("provider_revision") or 0),
    )


def compact_switch_blockers(decisions: Sequence[ExecutionModeSwitchDecision]) -> list[str]:
    return [blocker for decision in decisions for blocker in decision.blockers]
