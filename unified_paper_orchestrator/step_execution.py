from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


EXECUTION_PROFILE_SCHEMA = "unified-paper-orchestrator/step-execution-profile/v1"
REASONING_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EXECUTION_PRESETS = {
    "inherit": {
        "label": "继承阶段默认",
        "model": "",
        "reasoning_effort": "",
        "hard_token_limit": None,
    },
    "quality": {
        "label": "质量优先",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "hard_token_limit": None,
    },
    "balanced": {
        "label": "均衡",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "hard_token_limit": None,
    },
    "economy": {
        "label": "节省",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "hard_token_limit": None,
    },
    "custom": {
        "label": "自定义",
        "model": "",
        "reasoning_effort": "",
        "hard_token_limit": None,
    },
}
EXECUTION_PRESET_LABELS = {
    str(value["label"]): key for key, value in EXECUTION_PRESETS.items()
}
MAX_EXECUTION_MODEL_ID_CHARS = 200
MAX_STEP_TOKEN_LIMIT = 100_000_000


class StepExecutionProfileError(ValueError):
    pass


def _clean_model(value: Any, *, required: bool) -> str:
    model = str(value or "").strip()
    if required and not model:
        raise StepExecutionProfileError("执行模型不能为空")
    if len(model) > MAX_EXECUTION_MODEL_ID_CHARS or any(ord(char) < 32 for char in model):
        raise StepExecutionProfileError("执行模型标识无效")
    return model


def _clean_effort(value: Any) -> str:
    raw = getattr(value, "value", value)
    effort = str(raw or "").strip().casefold()
    # Syntax only here; per-model support is validated against model/list.
    # New runtime-provided levels must not require another GUI release.
    if effort and not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", effort):
        raise StepExecutionProfileError(
            "推理强度标识无效；请从当前模型支持的档位中选择"
        )
    return effort


def _clean_token_limit(value: Any, *, allow_inherit: bool, fallback: int) -> int | None:
    if value is None or value == "":
        return None if allow_inherit else int(fallback)
    if isinstance(value, bool):
        raise StepExecutionProfileError("单步 Token 上限必须是整数")
    try:
        limit = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise StepExecutionProfileError("单步 Token 上限必须是整数") from None
    if not 0 <= limit <= MAX_STEP_TOKEN_LIMIT:
        raise StepExecutionProfileError(
            f"单步 Token 上限必须是 0-{MAX_STEP_TOKEN_LIMIT:,}；0 表示不限制"
        )
    return limit


def normalize_execution_profile(
    value: Any,
    *,
    allow_inherit: bool,
    fallback_model: str = "",
    fallback_hard_token_limit: int = 0,
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    if allow_inherit:
        model = _clean_model(raw.get("model"), required=False)
        effort = _clean_effort(raw.get("reasoning_effort"))
    else:
        model = _clean_model(raw.get("model") or fallback_model, required=True)
        effort = _clean_effort(raw.get("reasoning_effort"))
    limit = _clean_token_limit(
        raw.get("hard_token_limit"),
        allow_inherit=allow_inherit,
        fallback=fallback_hard_token_limit,
    )
    policy = str(raw.get("unavailable_policy") or "block").strip().casefold()
    if policy != "block":
        raise StepExecutionProfileError("模型不可用时只能阻塞并提示，不能静默切换模型")
    preset = str(raw.get("preset") or ("inherit" if allow_inherit else "custom")).strip().casefold()
    if preset not in EXECUTION_PRESETS:
        preset = "custom"
    return {
        "schema": EXECUTION_PROFILE_SCHEMA,
        "model": model,
        "reasoning_effort": effort,
        "hard_token_limit": limit,
        "unavailable_policy": "block",
        "preset": preset,
    }


def execution_profile_fingerprint(profile: Mapping[str, Any]) -> str:
    payload = {
        "model": str(profile.get("model") or ""),
        "reasoning_effort": str(profile.get("reasoning_effort") or ""),
        "hard_token_limit": int(profile.get("hard_token_limit") or 0),
        "unavailable_policy": str(profile.get("unavailable_policy") or "block"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().upper()


def resolve_execution_profile(
    default_profile: Any,
    step_profile: Any,
    *,
    fallback_model: str,
    fallback_hard_token_limit: int = 0,
) -> dict[str, Any]:
    default = normalize_execution_profile(
        default_profile,
        allow_inherit=False,
        fallback_model=fallback_model,
        fallback_hard_token_limit=fallback_hard_token_limit,
    )
    step = normalize_execution_profile(step_profile, allow_inherit=True)
    hard_limit = step["hard_token_limit"]
    if hard_limit is None:
        hard_limit = int(default["hard_token_limit"] or 0)
    effective = {
        "schema": EXECUTION_PROFILE_SCHEMA,
        "model": str(step["model"] or default["model"]),
        "reasoning_effort": str(step["reasoning_effort"] or default["reasoning_effort"]),
        "hard_token_limit": int(hard_limit),
        "unavailable_policy": "block",
        "preset": str(step.get("preset") or "custom"),
        "model_source": "step" if step["model"] else "stage_default",
        "reasoning_effort_source": "step" if step["reasoning_effort"] else "stage_default",
        "token_limit_source": "step" if step["hard_token_limit"] is not None else "stage_default",
    }
    effective["fingerprint"] = execution_profile_fingerprint(effective)
    return effective


def apply_execution_preset(preset: str, *, current_token_limit: int | None = None) -> dict[str, Any]:
    key = str(preset or "").strip().casefold()
    if key not in EXECUTION_PRESETS:
        key = EXECUTION_PRESET_LABELS.get(str(preset or "").strip(), "custom")
    value = dict(EXECUTION_PRESETS[key])
    value.pop("label", None)
    if key in {"quality", "balanced", "economy", "custom"} and current_token_limit is not None:
        value["hard_token_limit"] = current_token_limit
    value.update({"preset": key, "unavailable_policy": "block"})
    return normalize_execution_profile(value, allow_inherit=True)


def _read(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def normalize_model_catalog(value: Any) -> list[dict[str, Any]]:
    data = _read(value, "data")
    if data is None and isinstance(value, (list, tuple)):
        data = value
    result: list[dict[str, Any]] = []
    for raw in list(data or []):
        model = str(_read(raw, "model", "id") or "").strip()
        model_id = str(_read(raw, "id", "model") or model).strip()
        if not model:
            continue
        efforts: list[str] = []
        for option in list(_read(raw, "supported_reasoning_efforts", "supportedReasoningEfforts") or []):
            effort = _read(option, "reasoning_effort", "reasoningEffort")
            normalized = str(getattr(effort, "value", effort) or "").strip().casefold()
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", normalized) and normalized not in efforts:
                efforts.append(normalized)
        tiers: list[str] = []
        for tier in list(_read(raw, "service_tiers", "serviceTiers") or []):
            tier_id = str(_read(tier, "id") or "").strip()
            if tier_id:
                tiers.append(tier_id)
        default_effort = _read(raw, "default_reasoning_effort", "defaultReasoningEffort")
        result.append(
            {
                "id": model_id,
                "model": model,
                "display_name": str(_read(raw, "display_name", "displayName") or model),
                "description": str(_read(raw, "description") or ""),
                "is_default": bool(_read(raw, "is_default", "isDefault")),
                "hidden": bool(_read(raw, "hidden")),
                "default_reasoning_effort": str(
                    getattr(default_effort, "value", default_effort) or ""
                ).casefold(),
                "supported_reasoning_efforts": efforts,
                "service_tiers": tiers,
            }
        )
    result.sort(key=lambda item: (not bool(item["is_default"]), str(item["display_name"]).casefold()))
    return result


def execution_profile_catalog_error(
    profile: Mapping[str, Any],
    catalog: list[dict[str, Any]] | None,
) -> tuple[str, str] | None:
    if catalog is None:
        return None
    model = str(profile.get("model") or "").strip()
    match = next(
        (
            item
            for item in catalog
            if model in {str(item.get("model") or ""), str(item.get("id") or "")}
        ),
        None,
    )
    if match is None:
        return "MODEL_UNAVAILABLE", f"当前 Codex 账号不可用模型：{model}"
    effort = str(profile.get("reasoning_effort") or "").strip().casefold()
    supported = [str(value).casefold() for value in match.get("supported_reasoning_efforts", [])]
    if effort and effort not in supported:
        return (
            "REASONING_EFFORT_UNAVAILABLE",
            f"模型 {model} 不支持推理强度 {effort}；可用值：{', '.join(supported)}",
        )
    return None


def token_limit_label(value: Any) -> str:
    if value is None:
        return "继承"
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return "-"
    return "不限制" if limit == 0 else f"{limit:,}"


__all__ = [
    "EXECUTION_PRESETS",
    "EXECUTION_PRESET_LABELS",
    "EXECUTION_PROFILE_SCHEMA",
    "MAX_STEP_TOKEN_LIMIT",
    "REASONING_EFFORTS",
    "StepExecutionProfileError",
    "apply_execution_preset",
    "execution_profile_catalog_error",
    "execution_profile_fingerprint",
    "normalize_execution_profile",
    "normalize_model_catalog",
    "resolve_execution_profile",
    "token_limit_label",
]
