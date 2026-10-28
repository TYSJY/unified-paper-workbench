"""SDK discovery shared by the settings UI and its save-time guard."""
from __future__ import annotations

from typing import Any

from ..step_execution import execution_profile_catalog_error


STAGES = ("writing", "figures", "journal_author")
WORKFLOWS = dict(zip(STAGES, ("writing_workflow", "figure_workflow", "journal_author_workflow")))


def discover_models(orchestrator: Any, stage: str) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError("Unknown SDK stage")
    result = orchestrator.stage_available_models(stage)
    models = [dict(item) for item in result.get("models", []) if not item.get("hidden")]
    if not result.get("ok") or not models:
        raise RuntimeError("SDK model catalog unavailable")
    # Do not forward the SDK account payload to the model picker.
    metadata = result.get("catalog", {})
    cache_state = metadata.get("cache_state", "fresh")
    return {"state": "ready" if cache_state == "fresh" else "stale", "models": models,
            "configuredModel": str(result.get("configured_model") or ""),
            "sdkVersion": str(metadata.get("sdk_version") or ""),
            "runtimeVersion": str(metadata.get("runtime_version") or ""),
            "checkedAt": str(metadata.get("checked_at") or ""),
            "cacheFetchedAt": str(metadata.get("cache_fetched_at") or ""),
            "cacheState": str(cache_state)}


def validate_profile(orchestrator: Any, stage: str, model: str, reasoning: str,
                     *, inherit: bool = False) -> None:
    try:
        catalog = discover_models(orchestrator, stage)
        if catalog["state"] != "ready":
            raise RuntimeError("SDK model catalog is stale or unverified")
    except Exception:
        raise RuntimeError("无法验证 SDK 模型目录。请检查 Codex 账号连接后重试；设置未写入。") from None
    if inherit:
        workflow = orchestrator.stage_prompt_inventory(stage).get(WORKFLOWS[stage], {})
        default = workflow.get("default_execution_profile", {})
        model = model or str(default.get("model") or catalog["configuredModel"])
        reasoning = reasoning or str(default.get("reasoning_effort") or "")
    profile = {"model": model or catalog["configuredModel"], "reasoning_effort": reasoning}
    error = execution_profile_catalog_error(profile, catalog["models"])
    if error:
        raise ValueError(error[1])
    if profile["reasoning_effort"]:
        selected = next(item for item in catalog["models"]
                        if profile["model"] in {item.get("model"), item.get("id")})
        if profile["reasoning_effort"] not in selected.get("supported_reasoning_efforts", []):
            raise ValueError("SDK 未声明该模型支持此推理强度；请选择模型默认或重新检测。")
