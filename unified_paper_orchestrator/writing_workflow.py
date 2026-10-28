from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .config import (
    LEGACY_PROJECTS,
    WRITING_CODEX_MODEL,
    WRITING_CUSTOM_PROMPT_DIR,
    WRITING_MAX_WORKFLOW_STEPS,
    WRITING_SDK_STEP_TOKEN_BUDGETS,
    WRITING_WORKFLOW_STATE_PATH,
)
from .io_utils import backup_file, now_iso, read_json, write_json_atomic, write_text
from .step_execution import (
    StepExecutionProfileError,
    normalize_execution_profile,
    resolve_execution_profile,
)
from .writing_prompt_integrity import TUNED_WRITING_PROMPT_FILES, writing_prompt_unchanged
from .writing_prompt_schemes import (
    JOURNAL_SCHEME, bundled_scheme_steps, is_undergraduate, normalize_thesis_brief,
    scheme_choices, scheme_id, snapshot_prompt_fingerprint,
)


WRITING_WORKFLOW_SCHEMA = "unified-paper-orchestrator/writing-workflow/v1"
WRITING_WORKFLOW_SNAPSHOT_SCHEMA = "unified-paper-orchestrator/writing-workflow-snapshot/v1"
_CUSTOM_ID_RE = re.compile(r"[^a-z0-9]+")

_BUILTIN_LABELS = {
    1: "寻找并确定研究 idea",
    2: "收集并核验参考文献",
    3: "撰写初稿",
    4: "升级投稿稿件",
    5: "补齐图表与结果结构",
    6: "使用真实数据清理稿件",
    7: "第一轮审稿",
    8: "根据第一轮审稿修改",
    9: "投稿成熟度清理",
    10: "最终投稿强化",
}


class WritingWorkflowError(ValueError):
    pass


def _snapshot_digest(default_profile: Mapping[str, Any], steps: list[dict[str, Any]], extra: dict | None = None) -> str:
    payload = json.dumps(
        {"default_execution_profile": dict(default_profile), "steps": steps, **(extra or {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _builtin_steps() -> list[dict[str, Any]]:
    root = Path(LEGACY_PROJECTS["writing"]["root"])
    return [
        {
            "step_id": f"builtin-{step:02d}",
            "name": _BUILTIN_LABELS[step],
            "kind": "builtin",
            "source_step": step,
            "contract_step": step,
            "prompt_path": str(root / "prompts" / TUNED_WRITING_PROMPT_FILES[step]),
        }
        for step in range(1, 11)
    ]


def normalize_workflow_snapshot(value: Any, *, legacy_default: bool = True) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    identifier = scheme_id(raw)
    undergraduate = identifier != JOURNAL_SCHEME
    extra: dict[str, Any] = {"prompt_scheme": identifier} if undergraduate else {}
    if undergraduate and raw.get("thesis_brief") is not None:
        extra["thesis_brief"] = normalize_thesis_brief(raw["thesis_brief"], required=True)
    if undergraduate and raw.get("figure_workflow_snapshot") is not None:
        from .figure_workflow import normalize_figure_workflow_snapshot
        figures = normalize_figure_workflow_snapshot(raw["figure_workflow_snapshot"])
        if scheme_id(figures) != identifier or figures.get("thesis_brief") != extra.get("thesis_brief"):
            raise WritingWorkflowError("写作与画图必须绑定相同的本科方案和输入材料")
        extra["figure_workflow_snapshot"] = figures
    try:
        default_execution_profile = normalize_execution_profile(
            raw.get("default_execution_profile"),
            allow_inherit=False,
            fallback_model=WRITING_CODEX_MODEL,
            fallback_hard_token_limit=0,
        )
    except StepExecutionProfileError as exc:
        raise WritingWorkflowError(f"写作阶段默认执行配置无效：{exc}") from exc
    raw_steps = raw.get("steps") if isinstance(raw, Mapping) else None
    steps = [copy.deepcopy(dict(item)) for item in raw_steps or [] if isinstance(item, Mapping)]
    if not steps and legacy_default:
        steps = _builtin_steps()
    if not 1 <= len(steps) <= WRITING_MAX_WORKFLOW_STEPS:
        raise WritingWorkflowError(f"写作流程必须包含 1-{WRITING_MAX_WORKFLOW_STEPS} 个步骤")

    seen_ids: set[str] = set()
    builtin_sources: list[int] = []
    normalized: list[dict[str, Any]] = []
    for position, raw_step in enumerate(steps, start=1):
        step_id = str(raw_step.get("step_id") or "").strip()
        name = str(raw_step.get("name") or "").strip()
        kind = str(raw_step.get("kind") or "builtin").strip().lower()
        try:
            source_step = int(raw_step.get("source_step") or 0)
            contract_step = int(raw_step.get("contract_step") or 0)
        except (TypeError, ValueError):
            raise WritingWorkflowError(f"第 {position} 步的步骤定义无效") from None
        prompt_path = str(raw_step.get("prompt_path") or "").strip()
        if not step_id or step_id in seen_ids:
            raise WritingWorkflowError("写作流程的 step_id 必须非空且唯一")
        if not name:
            raise WritingWorkflowError(f"第 {position} 步缺少名称")
        if kind not in {"builtin", "custom"}:
            raise WritingWorkflowError(f"第 {position} 步类型必须是 builtin 或 custom")
        if kind == "builtin":
            if source_step not in TUNED_WRITING_PROMPT_FILES or contract_step != source_step:
                raise WritingWorkflowError(f"内置步骤 {step_id} 的提示词/产物契约无效")
            if undergraduate:
                if prompt_path != f"scheme:{identifier}/{source_step:02d}":
                    raise WritingWorkflowError("本科内置步骤不能改绑提示词方案")
                snapshot_prompt_fingerprint(raw_step)
            else:
                expected = Path(LEGACY_PROJECTS["writing"]["root"]) / "prompts" / TUNED_WRITING_PROMPT_FILES[source_step]
                if Path(prompt_path).resolve() != expected.resolve():
                    raise WritingWorkflowError(f"内置步骤 {step_id} 不能改绑提示词文件")
            builtin_sources.append(source_step)
        else:
            if source_step != 0 or not 3 <= contract_step <= 10:
                raise WritingWorkflowError("自定义步骤必须复用一个可交付的论文产物契约（Step 3-10）")
            try:
                Path(prompt_path).resolve().relative_to(WRITING_CUSTOM_PROMPT_DIR.resolve())
            except ValueError as exc:
                raise WritingWorkflowError("自定义步骤提示词必须位于专用提示词目录") from exc
        seen_ids.add(step_id)
        item = {
                "position": position,
                "step_id": step_id,
                "name": name,
                "kind": kind,
                "source_step": source_step,
                "contract_step": contract_step,
                "prompt_path": prompt_path,
            }
        profile_value: Any
        if "execution_profile" in raw_step:
            profile_value = raw_step.get("execution_profile")
        elif kind == "builtin":
            profile_value = {
                "model": "",
                "reasoning_effort": "",
                "hard_token_limit": int(WRITING_SDK_STEP_TOKEN_BUDGETS.get(source_step, 0)),
                "unavailable_policy": "block",
                "preset": "inherit",
            }
        else:
            profile_value = {}
        try:
            item["execution_profile"] = normalize_execution_profile(
                profile_value,
                allow_inherit=True,
            )
        except StepExecutionProfileError as exc:
            raise WritingWorkflowError(f"第 {position} 步执行配置无效：{exc}") from exc
        if kind == "custom":
            item["prompt_ready"] = bool(raw_step.get("prompt_ready"))
            item["prompt_sha256"] = str(raw_step.get("prompt_sha256") or "").strip().casefold()
        elif undergraduate:
            item["prompt_text"] = str(raw_step["prompt_text"])
            item["prompt_sha256"] = str(raw_step["prompt_sha256"])
        normalized.append(item)

    if normalized[0]["kind"] != "builtin" or normalized[0]["source_step"] != 1:
        raise WritingWorkflowError("Step 1 负责生成论文编号，必须保留为流程第一步")
    if builtin_sources != list(range(1, max(builtin_sources) + 1)):
        raise WritingWorkflowError("内置步骤存在连续产物依赖，只能保留从 Step 1 开始的连续前缀")
    last_builtin_position = max(
        index for index, item in enumerate(normalized, start=1) if item["kind"] == "builtin"
    )
    if any(item["kind"] == "custom" for item in normalized[:last_builtin_position]):
        raise WritingWorkflowError("自定义步骤当前只能排在全部内置步骤之后")
    if normalized[-1]["contract_step"] < 3:
        raise WritingWorkflowError("写作流程终点必须产出可供画图阶段使用的 TeX 稿件")

    digest = _snapshot_digest(default_execution_profile, normalized, extra)
    version_id = str(raw.get("version_id") or f"workflow-{digest[:16]}").strip()
    return {
        "schema": WRITING_WORKFLOW_SNAPSHOT_SCHEMA,
        "version_id": version_id,
        "step_count": len(normalized),
        "sha256": digest,
        "created_at": str(raw.get("created_at") or now_iso()),
        "default_execution_profile": default_execution_profile,
        "steps": normalized,
        **extra,
    }


def default_workflow_snapshot() -> dict[str, Any]:
    return normalize_workflow_snapshot({"steps": _builtin_steps(), "created_at": "legacy-default"})


def workflow_step(snapshot: Any, position: int) -> dict[str, Any]:
    normalized = normalize_workflow_snapshot(snapshot)
    if not 1 <= int(position) <= int(normalized["step_count"]):
        raise WritingWorkflowError(f"写作步骤超出流程范围：{position}")
    return dict(normalized["steps"][int(position) - 1])


def workflow_step_count(snapshot: Any) -> int:
    return int(normalize_workflow_snapshot(snapshot)["step_count"])


def workflow_contract_step(snapshot: Any, position: int) -> int:
    return int(workflow_step(snapshot, position)["contract_step"])


def workflow_previous_contract_step(snapshot: Any, position: int) -> int:
    return 0 if int(position) <= 1 else workflow_contract_step(snapshot, int(position) - 1)


def custom_prompt_fingerprint(path: str | Path) -> dict[str, Any]:
    prompt = Path(path).resolve()
    try:
        prompt.relative_to(WRITING_CUSTOM_PROMPT_DIR.resolve())
    except ValueError as exc:
        raise WritingWorkflowError("自定义提示词路径不在专用目录中") from exc
    try:
        before = prompt.stat()
        data = prompt.read_bytes()
        after = prompt.stat()
    except OSError as exc:
        raise WritingWorkflowError(f"无法读取自定义提示词：{type(exc).__name__}") from exc
    if not prompt.is_file() or not data or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise WritingWorkflowError("自定义提示词必须是稳定的非空文件")
    return {
        "kind": "custom",
        "path": str(prompt),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mtime_ns": after.st_mtime_ns,
    }


def workflow_prompt_unchanged(root: str | Path, expected: Mapping[str, Any]) -> bool:
    if expected.get("kind") == "scheme":
        return bool(expected.get("text")) and hashlib.sha256(
            str(expected["text"]).encode("utf-8")
        ).hexdigest() == expected.get("sha256")
    if str(expected.get("kind") or "builtin") != "custom":
        return writing_prompt_unchanged(root, dict(expected))
    try:
        current = custom_prompt_fingerprint(str(expected.get("path") or ""))
    except WritingWorkflowError:
        return False
    return all(current.get(key) == expected.get(key) for key in ("kind", "path", "sha256", "size", "mtime_ns"))


class WritingWorkflowStore:
    def __init__(self, path: str | Path | None = None, custom_prompt_dir: str | Path | None = None) -> None:
        self.path = Path(path or WRITING_WORKFLOW_STATE_PATH)
        self.custom_prompt_dir = Path(custom_prompt_dir or WRITING_CUSTOM_PROMPT_DIR)

    def load(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        if not isinstance(data, dict) or data.get("schema") != WRITING_WORKFLOW_SCHEMA:
            snapshot = default_workflow_snapshot()
            return {
                "schema": WRITING_WORKFLOW_SCHEMA,
                "active_version_id": snapshot["version_id"],
                "versions": {snapshot["version_id"]: snapshot},
                "history": [],
            }
        versions = data.get("versions")
        if not isinstance(versions, dict):
            raise WritingWorkflowError("写作流程版本库损坏")
        active = str(data.get("active_version_id") or "")
        if active not in versions:
            raise WritingWorkflowError("写作流程当前版本不存在")
        normalize_workflow_snapshot(versions[active])
        return data

    def active_snapshot(self) -> dict[str, Any]:
        state = self.load()
        return normalize_workflow_snapshot(state["versions"][state["active_version_id"]])

    def snapshot_for_scheme(self, identifier: str) -> dict[str, Any]:
        """Read a scheme for one new paper without switching the global workflow."""
        identifier = scheme_id({"prompt_scheme": identifier})
        state = self.load()
        saved = next((value for value in reversed(list(state["versions"].values()))
                      if scheme_id(value) == identifier), None)
        if saved is not None:
            snapshot = normalize_workflow_snapshot(saved)
            from .writing_prompt_schemes import is_previous_undergraduate_factory
            if not is_previous_undergraduate_factory(snapshot):
                return snapshot
            steps = copy.deepcopy(snapshot["steps"])
            for step, content in zip(steps, bundled_scheme_steps(identifier)):
                # Keep execution settings and user-facing names; replace only
                # exact old factory text. Never publish over saved versions.
                previous_title = re.search(r"(?m)^## Step \d+｜([^\r\n]+)", step["prompt_text"])
                if previous_title and step["name"] == previous_title[1].strip():
                    step["name"] = content["name"]
                step.update(prompt_text=content["prompt_text"], prompt_sha256=content["prompt_sha256"])
            return normalize_workflow_snapshot({"steps": steps, "prompt_scheme": identifier,
                "created_at": "bundled-verification-upgrade", "default_execution_profile": snapshot["default_execution_profile"]})
        steps = _builtin_steps()
        if identifier != JOURNAL_SCHEME:
            for step, content in zip(steps, bundled_scheme_steps(identifier)):
                step.update(content)
                step["prompt_path"] = f"scheme:{identifier}/{step['source_step']:02d}"
        return normalize_workflow_snapshot({"steps": steps, "prompt_scheme": identifier,
            "created_at": "bundled-scheme", "default_execution_profile": self.active_snapshot()["default_execution_profile"]})

    def executable_snapshot(self, identifier: str | None = None) -> dict[str, Any]:
        snapshot = self.snapshot_for_scheme(identifier) if identifier else self.active_snapshot()
        for step in snapshot["steps"]:
            if step["kind"] != "custom":
                continue
            if not step.get("prompt_ready"):
                raise WritingWorkflowError(f"自定义步骤“{step['name']}”的提示词尚未保存")
            fingerprint = custom_prompt_fingerprint(step["prompt_path"])
            if fingerprint["sha256"] != str(step.get("prompt_sha256") or ""):
                raise WritingWorkflowError(f"自定义步骤“{step['name']}”的提示词版本不匹配")
        return snapshot

    def inventory(self) -> dict[str, Any]:
        state = self.load()
        snapshot = self.active_snapshot()
        steps = copy.deepcopy(snapshot["steps"])
        for item in steps:
            item["effective_execution_profile"] = resolve_execution_profile(
                snapshot["default_execution_profile"],
                item.get("execution_profile"),
                fallback_model=WRITING_CODEX_MODEL,
            )
        return {
            "active_version_id": snapshot["version_id"],
            "prompt_scheme": scheme_id(snapshot),
            "prompt_schemes": scheme_choices(),
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
        prompt_scheme: str | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        created_at = now_iso()
        active = normalize_workflow_snapshot(state["versions"][state["active_version_id"]])
        identifier = prompt_scheme if prompt_scheme is not None else scheme_id(active)
        if default_execution_profile is None:
            active = normalize_workflow_snapshot(state["versions"][state["active_version_id"]])
            default_execution_profile = active["default_execution_profile"]
        candidate = normalize_workflow_snapshot(
            {
                "steps": steps,
                "default_execution_profile": dict(default_execution_profile),
                "created_at": created_at,
                "prompt_scheme": identifier,
            }
        )
        candidate["version_id"] = (
            f"workflow-{created_at.replace(':', '').replace('-', '')[:15]}-"
            f"{candidate['sha256'][:8]}-{uuid4().hex[:8]}"
        )
        versions = state.setdefault("versions", {})
        versions[candidate["version_id"]] = candidate
        state["active_version_id"] = candidate["version_id"]
        state["updated_at"] = created_at
        history = state.setdefault("history", [])
        history.append({"at": created_at, "event": event, **dict(detail), "version_id": candidate["version_id"]})
        state["history"] = history[-200:]
        backup_file(self.path, "writing_workflow")
        write_json_atomic(self.path, state)
        return self.inventory()

    def select_prompt_scheme(self, identifier: str) -> dict[str, Any]:
        identifier = scheme_id({"prompt_scheme": identifier})
        state = self.load()
        active = self.active_snapshot()
        if scheme_id(active) == identifier:
            return self.inventory()
        # Restore the last configured version of each scheme, including custom
        # steps and model settings, without mutating any queued paper snapshot.
        saved = next((value for value in reversed(list(state["versions"].values()))
                      if scheme_id(value) == identifier), None)
        if saved is not None:
            target = normalize_workflow_snapshot(saved)
        else:
            steps = _builtin_steps()
            if identifier != JOURNAL_SCHEME:
                for step, content in zip(steps, bundled_scheme_steps(identifier)):
                    step.update(content)
                    step["prompt_path"] = f"scheme:{identifier}/{step['source_step']:02d}"
            target = {"steps": steps, "default_execution_profile": active["default_execution_profile"]}
        return self._publish(target["steps"], "select_prompt_scheme", {"prompt_scheme": identifier},
                             prompt_scheme=identifier, default_execution_profile=target["default_execution_profile"])

    def read_scheme_prompt(self, step_id: str) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        step = next((item for item in snapshot["steps"] if item["step_id"] == step_id), None)
        if not is_undergraduate(snapshot) or not step or step["kind"] != "builtin":
            raise WritingWorkflowError("请选择本科方案中的内置步骤")
        snapshot_prompt_fingerprint(step)
        return {"path": step["prompt_path"], "text": step["prompt_text"], "step_id": step_id}

    def save_scheme_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        self.read_scheme_prompt(step_id)
        if not str(text).strip():
            raise WritingWorkflowError("提示词不能为空")
        snapshot = self.active_snapshot()
        for step in snapshot["steps"]:
            if step["step_id"] == step_id:
                step["prompt_text"] = str(text)
                step["prompt_sha256"] = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
        return self._publish(snapshot["steps"], "save_scheme_prompt", {"step_id": step_id})

    def add_custom_step(self, name: str) -> dict[str, Any]:
        title = " ".join(str(name or "").split())
        if not title:
            raise WritingWorkflowError("自定义步骤名称不能为空")
        snapshot = self.active_snapshot()
        if int(snapshot["step_count"]) >= WRITING_MAX_WORKFLOW_STEPS:
            raise WritingWorkflowError(f"写作流程最多 {WRITING_MAX_WORKFLOW_STEPS} 步")
        self.custom_prompt_dir.mkdir(parents=True, exist_ok=True)
        slug = _CUSTOM_ID_RE.sub("-", title.casefold()).strip("-")[:36] or "custom"
        digest = hashlib.sha256(f"{title}|{now_iso()}".encode("utf-8")).hexdigest()[:8]
        step_id = f"custom-{slug}-{digest}"
        prompt_path = self.custom_prompt_dir / f"{step_id}.md"
        terminal_contract = int(snapshot["steps"][-1]["contract_step"])
        starter = (
            f"# {title}\n\n"
            "这是用户自定义的写作步骤。请在下方补充该步骤的完整要求。\n\n"
            "必须继续处理 `<paper_id>`，并更新当前流程沿用的 TeX 产物；不要创建另一篇论文。\n"
        )
        write_text(prompt_path, starter)
        steps = [dict(item) for item in snapshot["steps"]]
        steps.append(
            {
                "step_id": step_id,
                "name": title,
                "kind": "custom",
                "source_step": 0,
                "contract_step": terminal_contract,
                "prompt_path": str(prompt_path),
                "prompt_ready": False,
                "prompt_sha256": "",
            }
        )
        return self._publish(steps, "add_custom_step", {"step_id": step_id, "name": title})

    def restore_default(self) -> dict[str, Any]:
        identifier = scheme_id(self.active_snapshot())
        steps = _builtin_steps()
        if identifier != JOURNAL_SCHEME:
            for step, content in zip(steps, bundled_scheme_steps(identifier)):
                step.update(content)
                step["prompt_path"] = f"scheme:{identifier}/{step['source_step']:02d}"
        return self._publish(
            steps,
            "restore_default_workflow",
            {"step_count": 10},
            default_execution_profile=normalize_execution_profile(
                {},
                allow_inherit=False,
                fallback_model=WRITING_CODEX_MODEL,
            ),
        )

    def update_default_execution_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        try:
            normalized = normalize_execution_profile(
                profile,
                allow_inherit=False,
                fallback_model=WRITING_CODEX_MODEL,
            )
        except StepExecutionProfileError as exc:
            raise WritingWorkflowError(str(exc)) from exc
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
            raise WritingWorkflowError("没有找到要配置的写作步骤")
        try:
            normalized = normalize_execution_profile(profile, allow_inherit=True)
        except StepExecutionProfileError as exc:
            raise WritingWorkflowError(str(exc)) from exc
        item["execution_profile"] = normalized
        return self._publish(
            steps,
            "update_step_execution_profile",
            {"step_id": target, "profile": normalized},
            default_execution_profile=snapshot["default_execution_profile"],
        )

    def remove_step(self, step_id: str) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        matches = [index for index, item in enumerate(steps) if item["step_id"] == target]
        if not matches:
            raise WritingWorkflowError("没有找到要删除的写作步骤")
        index = matches[0]
        if index == 0:
            raise WritingWorkflowError("Step 1 负责创建论文编号，不能删除")
        removed = steps[index:] if steps[index]["kind"] == "builtin" else [steps[index]]
        if steps[index]["kind"] == "builtin":
            steps = steps[:index]
        else:
            del steps[index]
        return self._publish(
            steps,
            "remove_step",
            {"step_id": target, "removed_step_ids": [item["step_id"] for item in removed]},
        )

    def move_custom_step(self, step_id: str, direction: int) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        index = next((i for i, item in enumerate(steps) if item["step_id"] == target), -1)
        if index < 0 or steps[index]["kind"] != "custom":
            raise WritingWorkflowError("只有自定义步骤可以调整顺序")
        destination = index + (-1 if int(direction) < 0 else 1)
        if not 0 <= destination < len(steps) or steps[destination]["kind"] != "custom":
            raise WritingWorkflowError("自定义步骤不能移动到内置步骤之前")
        steps[index], steps[destination] = steps[destination], steps[index]
        return self._publish(steps, "move_custom_step", {"step_id": target, "direction": int(direction)})

    def read_custom_prompt(self, step_id: str) -> dict[str, Any]:
        step = next((item for item in self.active_snapshot()["steps"] if item["step_id"] == step_id), None)
        if not step or step["kind"] != "custom":
            raise WritingWorkflowError("请选择一个自定义步骤")
        path = Path(step["prompt_path"])
        return {"step_id": step_id, "path": str(path), "text": path.read_text(encoding="utf-8")}

    def save_custom_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        step = next((item for item in snapshot["steps"] if item["step_id"] == step_id), None)
        if not step or step["kind"] != "custom":
            raise WritingWorkflowError("内置提示词受保护；这里只能保存自定义步骤提示词")
        body = str(text or "")
        if not body.strip():
            raise WritingWorkflowError("自定义步骤提示词不能为空")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        path = self.custom_prompt_dir / f"{step_id}-{digest[:12]}.md"
        if not path.exists():
            write_text(path, body)
        fingerprint = custom_prompt_fingerprint(path)
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


__all__ = [
    "WRITING_WORKFLOW_SCHEMA",
    "WRITING_WORKFLOW_SNAPSHOT_SCHEMA",
    "WritingWorkflowError",
    "WritingWorkflowStore",
    "custom_prompt_fingerprint",
    "default_workflow_snapshot",
    "normalize_workflow_snapshot",
    "workflow_contract_step",
    "workflow_previous_contract_step",
    "workflow_prompt_unchanged",
    "workflow_step",
    "workflow_step_count",
]
