from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .config import (
    FIGURE_CODEX_MODEL,
    FIGURE_CUSTOM_PROMPT_DIR,
    FIGURE_MAX_WORKFLOW_STEPS,
    FIGURE_WORKFLOW_STATE_PATH,
    LEGACY_PROJECTS,
)
from .figure_prompt_integrity import FIGURE_PROMPT_FILES, validate_figure_prompt
from .io_utils import backup_file, now_iso, read_json, write_json_atomic, write_text
from .step_execution import (
    StepExecutionProfileError,
    normalize_execution_profile,
    resolve_execution_profile,
)


FIGURE_WORKFLOW_SCHEMA = "unified-paper-orchestrator/figure-workflow/v1"
FIGURE_WORKFLOW_SNAPSHOT_SCHEMA = "unified-paper-orchestrator/figure-workflow-snapshot/v1"
_CUSTOM_ID_RE = re.compile(r"[^a-z0-9]+")
_BUILTIN_LABELS = {
    1: "研究并整理参考图",
    2: "制定图表规划与逐图提示词",
    3: "生成图表并编译提交包",
}


class FigureWorkflowError(ValueError):
    pass


def _snapshot_digest(default_profile: Mapping[str, Any], steps: list[dict[str, Any]], extra=None) -> str:
    payload = json.dumps(
        {"default_execution_profile": dict(default_profile), "steps": steps, **(extra or {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _builtin_steps(*, include_reference_step: bool = True) -> list[dict[str, Any]]:
    root = Path(LEGACY_PROJECTS["figures"]["root"])
    source_steps = (1, 2, 3) if include_reference_step else (2, 3)
    return [
        {
            "step_id": f"builtin-{step:02d}",
            "name": _BUILTIN_LABELS[step],
            "kind": "builtin",
            "source_step": step,
            "contract_step": step,
            "prompt_path": str(root / "prompts" / FIGURE_PROMPT_FILES[step]),
        }
        for step in source_steps
    ]


def normalize_figure_workflow_snapshot(value: Any, *, legacy_default: bool = True) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    from .writing_prompt_schemes import is_undergraduate, scheme_id, normalize_thesis_brief, snapshot_prompt_fingerprint
    undergraduate = is_undergraduate(raw)
    extra = ({"prompt_scheme": scheme_id(raw), "thesis_brief": normalize_thesis_brief(raw.get("thesis_brief"), required=True)}
             if undergraduate else {})
    try:
        default_execution_profile = normalize_execution_profile(
            raw.get("default_execution_profile"),
            allow_inherit=False,
            fallback_model=FIGURE_CODEX_MODEL,
            fallback_hard_token_limit=0,
        )
    except StepExecutionProfileError as exc:
        raise FigureWorkflowError(f"画图阶段默认执行配置无效：{exc}") from exc
    raw_steps = raw.get("steps") if isinstance(raw, Mapping) else None
    steps = [copy.deepcopy(dict(item)) for item in raw_steps or [] if isinstance(item, Mapping)]
    if not steps and legacy_default:
        steps = _builtin_steps()
    if not 2 <= len(steps) <= FIGURE_MAX_WORKFLOW_STEPS:
        raise FigureWorkflowError(f"画图流程必须包含 2-{FIGURE_MAX_WORKFLOW_STEPS} 个步骤")

    seen_ids: set[str] = set()
    builtin_sources: list[int] = []
    normalized: list[dict[str, Any]] = []
    project_root = Path(LEGACY_PROJECTS["figures"]["root"])
    for position, raw_step in enumerate(steps, start=1):
        step_id = str(raw_step.get("step_id") or "").strip()
        name = " ".join(str(raw_step.get("name") or "").split())
        kind = str(raw_step.get("kind") or "builtin").strip().lower()
        try:
            source_step = int(raw_step.get("source_step") or 0)
            contract_step = int(raw_step.get("contract_step") or 0)
        except (TypeError, ValueError):
            raise FigureWorkflowError(f"第 {position} 步定义无效") from None
        prompt_path = str(raw_step.get("prompt_path") or "").strip()
        if not step_id or step_id in seen_ids:
            raise FigureWorkflowError("画图流程的 step_id 必须非空且唯一")
        if not name:
            raise FigureWorkflowError(f"第 {position} 步缺少名称")
        if kind not in {"builtin", "custom"}:
            raise FigureWorkflowError(f"第 {position} 步类型必须是 builtin 或 custom")
        if kind == "builtin":
            if source_step not in {1, 2, 3} or contract_step != source_step:
                raise FigureWorkflowError(f"内置步骤 {step_id} 的提示词/产物契约无效")
            expected = project_root / "prompts" / FIGURE_PROMPT_FILES[source_step]
            if undergraduate:
                if prompt_path != f"scheme:{scheme_id(raw)}/figures/{source_step:02d}":
                    raise FigureWorkflowError("本科画图步骤方案不匹配")
                snapshot_prompt_fingerprint(raw_step)
            elif Path(prompt_path).resolve() != expected.resolve():
                raise FigureWorkflowError(f"内置步骤 {step_id} 不能改绑提示词文件")
            builtin_sources.append(source_step)
        else:
            if source_step != 0 or contract_step != 3:
                raise FigureWorkflowError("自定义画图步骤必须继续维护最终提交包（复用 Step 3 产物契约）")
            try:
                Path(prompt_path).resolve().relative_to(FIGURE_CUSTOM_PROMPT_DIR.resolve())
            except ValueError as exc:
                raise FigureWorkflowError("自定义画图提示词必须位于专用提示词目录") from exc
        seen_ids.add(step_id)
        item: dict[str, Any] = {
            "position": position,
            "step_id": step_id,
            "name": name,
            "kind": kind,
            "source_step": source_step,
            "contract_step": contract_step,
            "prompt_path": prompt_path,
        }
        try:
            item["execution_profile"] = normalize_execution_profile(
                raw_step.get("execution_profile") if "execution_profile" in raw_step else {},
                allow_inherit=True,
            )
        except StepExecutionProfileError as exc:
            raise FigureWorkflowError(f"第 {position} 步执行配置无效：{exc}") from exc
        if undergraduate and kind == "builtin":
            item["prompt_text"] = raw_step["prompt_text"]
            item["prompt_sha256"] = raw_step["prompt_sha256"]
        if kind == "custom":
            item["prompt_ready"] = bool(raw_step.get("prompt_ready"))
            item["prompt_sha256"] = str(raw_step.get("prompt_sha256") or "").strip().casefold()
        normalized.append(item)

    if builtin_sources not in ([1, 2, 3], [2, 3]):
        raise FigureWorkflowError("内置画图步骤必须保持 Step 2→3；仅 Step 1（参考图研究）可选")
    last_builtin = max(index for index, item in enumerate(normalized) if item["kind"] == "builtin")
    if any(item["kind"] == "custom" for item in normalized[:last_builtin]):
        raise FigureWorkflowError("自定义画图步骤当前只能排在全部内置步骤之后")
    if int(normalized[-1]["contract_step"]) != 3:
        raise FigureWorkflowError("画图流程终点必须保留完整提交包契约")

    digest = _snapshot_digest(default_execution_profile, normalized, extra)
    return {
        "schema": FIGURE_WORKFLOW_SNAPSHOT_SCHEMA,
        "version_id": str(raw.get("version_id") or f"figure-workflow-{digest[:16]}").strip(),
        "step_count": len(normalized),
        "sha256": digest,
        "created_at": str(raw.get("created_at") or now_iso()),
        "default_execution_profile": default_execution_profile,
        "steps": normalized,
        **extra,
    }


def default_figure_workflow_snapshot() -> dict[str, Any]:
    return normalize_figure_workflow_snapshot({"steps": _builtin_steps(), "created_at": "legacy-default"})


def figure_workflow_step(snapshot: Any, position: int) -> dict[str, Any]:
    normalized = normalize_figure_workflow_snapshot(snapshot)
    if not 1 <= int(position) <= int(normalized["step_count"]):
        raise FigureWorkflowError(f"画图步骤超出流程范围：{position}")
    return dict(normalized["steps"][int(position) - 1])


def figure_workflow_step_count(snapshot: Any) -> int:
    return int(normalize_figure_workflow_snapshot(snapshot)["step_count"])


def figure_workflow_position_for_source(snapshot: Any, source_step: int) -> int:
    normalized = normalize_figure_workflow_snapshot(snapshot)
    for item in normalized["steps"]:
        if item["kind"] == "builtin" and int(item["source_step"]) == int(source_step):
            return int(item["position"])
    raise FigureWorkflowError(f"当前画图流程没有内置 Step {source_step}")


def figure_custom_prompt_fingerprint(path: str | Path) -> dict[str, Any]:
    prompt = Path(path).resolve()
    try:
        prompt.relative_to(FIGURE_CUSTOM_PROMPT_DIR.resolve())
    except ValueError as exc:
        raise FigureWorkflowError("自定义画图提示词路径不在专用目录中") from exc
    try:
        before = prompt.stat()
        data = prompt.read_bytes()
        after = prompt.stat()
    except OSError as exc:
        raise FigureWorkflowError(f"无法读取自定义画图提示词：{type(exc).__name__}") from exc
    if not prompt.is_file() or not data or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise FigureWorkflowError("自定义画图提示词必须是稳定的非空文件")
    return {
        "kind": "custom",
        "path": str(prompt),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mtime_ns": after.st_mtime_ns,
    }


class FigureWorkflowStore:
    def __init__(self, path: str | Path | None = None, custom_prompt_dir: str | Path | None = None) -> None:
        self.path = Path(path or FIGURE_WORKFLOW_STATE_PATH)
        self.custom_prompt_dir = Path(custom_prompt_dir or FIGURE_CUSTOM_PROMPT_DIR)

    def load(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        if not isinstance(data, dict) or data.get("schema") != FIGURE_WORKFLOW_SCHEMA:
            snapshot = default_figure_workflow_snapshot()
            return {
                "schema": FIGURE_WORKFLOW_SCHEMA,
                "active_version_id": snapshot["version_id"],
                "versions": {snapshot["version_id"]: snapshot},
                "history": [],
            }
        versions = data.get("versions")
        if not isinstance(versions, dict):
            raise FigureWorkflowError("画图流程版本库损坏")
        active = str(data.get("active_version_id") or "")
        if active not in versions:
            raise FigureWorkflowError("画图流程当前版本不存在")
        normalize_figure_workflow_snapshot(versions[active])
        return data

    def active_snapshot(self) -> dict[str, Any]:
        state = self.load()
        return normalize_figure_workflow_snapshot(state["versions"][state["active_version_id"]])

    def executable_snapshot(self) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        for step in snapshot["steps"]:
            if step["kind"] != "custom":
                continue
            if not step.get("prompt_ready"):
                raise FigureWorkflowError(f"自定义步骤“{step['name']}”的提示词尚未保存")
            current = figure_custom_prompt_fingerprint(step["prompt_path"])
            if current["sha256"] != str(step.get("prompt_sha256") or ""):
                raise FigureWorkflowError(f"自定义步骤“{step['name']}”的提示词版本不匹配")
        return snapshot

    def inventory(self) -> dict[str, Any]:
        state = self.load()
        snapshot = self.active_snapshot()
        steps = copy.deepcopy(snapshot["steps"])
        for item in steps:
            item["effective_execution_profile"] = resolve_execution_profile(
                snapshot["default_execution_profile"],
                item.get("execution_profile"),
                fallback_model=FIGURE_CODEX_MODEL,
            )
        return {
            "active_version_id": snapshot["version_id"],
            "step_count": snapshot["step_count"],
            "sha256": snapshot["sha256"],
            "default_execution_profile": copy.deepcopy(snapshot["default_execution_profile"]),
            "steps": steps,
            "version_count": len(state.get("versions", {})),
        }

    def _publish(
        self,
        steps: list[dict[str, Any]],
        event: str,
        detail: Mapping[str, Any],
        *,
        default_execution_profile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        created_at = now_iso()
        if default_execution_profile is None:
            active = normalize_figure_workflow_snapshot(state["versions"][state["active_version_id"]])
            default_execution_profile = active["default_execution_profile"]
        candidate = normalize_figure_workflow_snapshot(
            {
                "steps": steps,
                "default_execution_profile": dict(default_execution_profile),
                "created_at": created_at,
            }
        )
        candidate["version_id"] = (
            f"figure-workflow-{created_at.replace(':', '').replace('-', '')[:15]}-"
            f"{candidate['sha256'][:8]}-{uuid4().hex[:8]}"
        )
        state.setdefault("versions", {})[candidate["version_id"]] = candidate
        state["active_version_id"] = candidate["version_id"]
        state["updated_at"] = created_at
        history = state.setdefault("history", [])
        history.append({"at": created_at, "event": event, **dict(detail), "version_id": candidate["version_id"]})
        state["history"] = history[-200:]
        backup_file(self.path, "figure_workflow")
        write_json_atomic(self.path, state)
        return self.inventory()

    def add_custom_step(self, name: str) -> dict[str, Any]:
        title = " ".join(str(name or "").split())
        if not title:
            raise FigureWorkflowError("自定义画图步骤名称不能为空")
        snapshot = self.active_snapshot()
        if int(snapshot["step_count"]) >= FIGURE_MAX_WORKFLOW_STEPS:
            raise FigureWorkflowError(f"画图流程最多 {FIGURE_MAX_WORKFLOW_STEPS} 步")
        self.custom_prompt_dir.mkdir(parents=True, exist_ok=True)
        slug = _CUSTOM_ID_RE.sub("-", title.casefold()).strip("-")[:36] or "custom"
        digest = hashlib.sha256(f"{title}|{now_iso()}".encode("utf-8")).hexdigest()[:8]
        step_id = f"custom-{slug}-{digest}"
        prompt_path = self.custom_prompt_dir / f"{step_id}.md"
        starter = (
            f"# {title}\n\n"
            "这是用户自定义的画图后处理步骤。请在下方补充完整要求。\n\n"
            "必须继续处理 `<paper_id>` 的既有画图项目与提交包；不要新建另一篇论文，"
            "不要删除有效真实数据，完成后仍需保证提交 TeX、图文件、manifest 和编译 PDF 一致。\n"
        )
        write_text(prompt_path, starter)
        steps = [dict(item) for item in snapshot["steps"]]
        steps.append(
            {
                "step_id": step_id,
                "name": title,
                "kind": "custom",
                "source_step": 0,
                "contract_step": 3,
                "prompt_path": str(prompt_path),
                "prompt_ready": False,
                "prompt_sha256": "",
            }
        )
        return self._publish(steps, "add_custom_step", {"step_id": step_id, "name": title})

    def remove_step(self, step_id: str) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        index = next((i for i, item in enumerate(steps) if item["step_id"] == target), -1)
        if index < 0:
            raise FigureWorkflowError("没有找到要删除的画图步骤")
        item = steps[index]
        if item["kind"] == "builtin" and int(item["source_step"]) != 1:
            raise FigureWorkflowError("内置 Step 2 和 Step 3 构成最终画图交付链，不能删除")
        del steps[index]
        return self._publish(steps, "remove_step", {"step_id": target})

    def move_custom_step(self, step_id: str, direction: int) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        index = next((i for i, item in enumerate(steps) if item["step_id"] == target), -1)
        if index < 0 or steps[index]["kind"] != "custom":
            raise FigureWorkflowError("只有自定义画图步骤可以调整顺序")
        destination = index + (-1 if int(direction) < 0 else 1)
        if not 0 <= destination < len(steps) or steps[destination]["kind"] != "custom":
            raise FigureWorkflowError("自定义画图步骤不能移动到内置步骤之前")
        steps[index], steps[destination] = steps[destination], steps[index]
        return self._publish(steps, "move_custom_step", {"step_id": target, "direction": int(direction)})

    def restore_default(self) -> dict[str, Any]:
        return self._publish(
            _builtin_steps(),
            "restore_default_workflow",
            {"step_count": 3},
            default_execution_profile=normalize_execution_profile(
                {},
                allow_inherit=False,
                fallback_model=FIGURE_CODEX_MODEL,
            ),
        )

    def update_default_execution_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        try:
            normalized = normalize_execution_profile(
                profile,
                allow_inherit=False,
                fallback_model=FIGURE_CODEX_MODEL,
            )
        except StepExecutionProfileError as exc:
            raise FigureWorkflowError(str(exc)) from exc
        return self._publish(
            [dict(item) for item in snapshot["steps"]],
            "update_default_execution_profile",
            {"profile": normalized},
            default_execution_profile=normalized,
        )

    def update_step_execution_profile(
        self,
        step_id: str,
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        item = next((value for value in steps if str(value.get("step_id") or "") == target), None)
        if item is None:
            raise FigureWorkflowError("没有找到要配置的画图步骤")
        try:
            normalized = normalize_execution_profile(profile, allow_inherit=True)
        except StepExecutionProfileError as exc:
            raise FigureWorkflowError(str(exc)) from exc
        item["execution_profile"] = normalized
        return self._publish(
            steps,
            "update_step_execution_profile",
            {"step_id": target, "profile": normalized},
            default_execution_profile=snapshot["default_execution_profile"],
        )

    def read_custom_prompt(self, step_id: str) -> dict[str, Any]:
        step = next((item for item in self.active_snapshot()["steps"] if item["step_id"] == step_id), None)
        if not step or step["kind"] != "custom":
            raise FigureWorkflowError("请选择一个自定义画图步骤")
        path = Path(step["prompt_path"])
        return {"step_id": step_id, "path": str(path), "text": path.read_text(encoding="utf-8")}

    def save_custom_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        step = next((item for item in snapshot["steps"] if item["step_id"] == step_id), None)
        if not step or step["kind"] != "custom":
            raise FigureWorkflowError("内置提示词受保护；这里只能保存自定义画图步骤提示词")
        body = str(text or "")
        if not body.strip():
            raise FigureWorkflowError("自定义画图步骤提示词不能为空")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        path = self.custom_prompt_dir / f"{step_id}-{digest[:12]}.md"
        if not path.exists():
            write_text(path, body)
        fingerprint = figure_custom_prompt_fingerprint(path)
        steps = [dict(item) for item in snapshot["steps"]]
        for item in steps:
            if item["step_id"] == step_id:
                item["prompt_path"] = str(path)
                item["prompt_ready"] = True
                item["prompt_sha256"] = fingerprint["sha256"]
                break
        result = self._publish(steps, "save_custom_prompt", {"step_id": step_id, "path": str(path)})
        result.update({"status": "saved", "step_id": step_id, "path": str(path), "fingerprint": fingerprint})
        return result


def figure_workflow_prompt_fingerprint(root: str | Path, snapshot: Any, position: int) -> dict[str, Any]:
    step = figure_workflow_step(snapshot, position)
    from .writing_prompt_schemes import is_undergraduate, snapshot_prompt_fingerprint
    if is_undergraduate(snapshot) and step["kind"] == "builtin":
        return snapshot_prompt_fingerprint(step)
    if step["kind"] == "builtin":
        value = validate_figure_prompt(Path(root), int(step["source_step"]))
        return {
            "kind": "builtin",
            "path": str(value.path.resolve()),
            "sha256": value.sha256.casefold(),
            "size": value.size,
        }
    current = figure_custom_prompt_fingerprint(step["prompt_path"])
    if not step.get("prompt_ready") or current["sha256"] != str(step.get("prompt_sha256") or ""):
        raise FigureWorkflowError("自定义画图提示词与任务快照不一致")
    return current


__all__ = [
    "FIGURE_WORKFLOW_SCHEMA",
    "FIGURE_WORKFLOW_SNAPSHOT_SCHEMA",
    "FigureWorkflowError",
    "FigureWorkflowStore",
    "default_figure_workflow_snapshot",
    "figure_custom_prompt_fingerprint",
    "figure_workflow_position_for_source",
    "figure_workflow_prompt_fingerprint",
    "figure_workflow_step",
    "figure_workflow_step_count",
    "normalize_figure_workflow_snapshot",
]
