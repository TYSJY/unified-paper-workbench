from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .config import (
    JOURNAL_AUTHOR_CODEX_MODEL,
    JOURNAL_AUTHOR_CUSTOM_PROMPT_DIR,
    JOURNAL_AUTHOR_MAX_WORKFLOW_STEPS,
    JOURNAL_AUTHOR_WORKFLOW_STATE_PATH,
    LEGACY_PROJECTS,
)
from .io_utils import backup_file, now_iso, read_json, write_json_atomic, write_text
from .journal_author_prompt_integrity import validate_journal_author_prompt
from .step_execution import (
    StepExecutionProfileError,
    normalize_execution_profile,
    resolve_execution_profile,
)


JOURNAL_AUTHOR_WORKFLOW_SCHEMA = "unified-paper-orchestrator/journal-author-workflow/v1"
JOURNAL_AUTHOR_WORKFLOW_SNAPSHOT_SCHEMA = "unified-paper-orchestrator/journal-author-workflow-snapshot/v2"
JOURNAL_AUTHOR_BUILTIN_STEP_ID = "builtin-02-03"
JOURNAL_AUTHOR_CHOOSE_STEP_ID = "builtin-02"
JOURNAL_AUTHOR_PACKAGE_STEP_ID = "builtin-03"
JOURNAL_AUTHOR_WORKFLOW_MODES = ("combined", "split")
_CUSTOM_ID_RE = re.compile(r"[^a-z0-9]+")


class JournalAuthorWorkflowError(ValueError):
    pass


def _empty_step_profile() -> dict[str, Any]:
    return {
        "model": "",
        "reasoning_effort": "",
        "hard_token_limit": None,
        "unavailable_policy": "block",
        "preset": "inherit",
    }


def _builtin_step(name: str) -> dict[str, Any]:
    root = Path(LEGACY_PROJECTS["journal_author"]["root"])
    definitions = {
        "combined": {
            "step_id": JOURNAL_AUTHOR_BUILTIN_STEP_ID,
            "name": "选刊并生成投稿包（原 Step 2-3）",
            "source_step": "2-3",
            "contract_step": 3,
            "prompt_key": "combined",
        },
        "choose": {
            "step_id": JOURNAL_AUTHOR_CHOOSE_STEP_ID,
            "name": "核验并选择投稿期刊（原 Step 2）",
            "source_step": 2,
            "contract_step": 2,
            "prompt_key": "choose",
        },
        "package": {
            "step_id": JOURNAL_AUTHOR_PACKAGE_STEP_ID,
            "name": "生成并验收投稿包（原 Step 3）",
            "source_step": 3,
            "contract_step": 3,
            "prompt_key": "package",
        },
    }
    definition = definitions[name]
    fingerprint = validate_journal_author_prompt(root, str(definition["prompt_key"]))
    return {
        **definition,
        "kind": "builtin",
        "prompt_path": str(fingerprint.path),
        "prompt_sha256": fingerprint.sha256,
        "execution_profile": _empty_step_profile(),
    }


def _builtin_steps(mode: str) -> list[dict[str, Any]]:
    if mode == "combined":
        return [_builtin_step("combined")]
    if mode == "split":
        return [_builtin_step("choose"), _builtin_step("package")]
    raise JournalAuthorWorkflowError("选刊作者流程模式必须是 combined 或 split")


def _infer_mode(steps: list[dict[str, Any]]) -> str:
    ids = [str(item.get("step_id") or "") for item in steps if str(item.get("kind") or "builtin") == "builtin"]
    return "split" if ids[:2] == [JOURNAL_AUTHOR_CHOOSE_STEP_ID, JOURNAL_AUTHOR_PACKAGE_STEP_ID] else "combined"


def _digest(mode: str, default_profile: Mapping[str, Any], steps: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"mode": mode, "default_execution_profile": dict(default_profile), "steps": steps},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_journal_author_workflow_snapshot(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    try:
        default_profile = normalize_execution_profile(
            raw.get("default_execution_profile"),
            allow_inherit=False,
            fallback_model=JOURNAL_AUTHOR_CODEX_MODEL,
            fallback_hard_token_limit=0,
        )
    except StepExecutionProfileError as exc:
        raise JournalAuthorWorkflowError(f"选刊作者阶段默认执行配置无效：{exc}") from exc

    raw_steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    steps = [copy.deepcopy(dict(item)) for item in raw_steps if isinstance(item, Mapping)]
    mode = str(raw.get("mode") or _infer_mode(steps)).strip().casefold()
    if mode not in JOURNAL_AUTHOR_WORKFLOW_MODES:
        raise JournalAuthorWorkflowError("选刊作者流程模式必须是 combined 或 split")
    if not steps:
        steps = _builtin_steps(mode)
    if not 1 <= len(steps) <= JOURNAL_AUTHOR_MAX_WORKFLOW_STEPS:
        raise JournalAuthorWorkflowError(f"选刊作者流程必须包含 1-{JOURNAL_AUTHOR_MAX_WORKFLOW_STEPS} 个步骤")

    expected_builtins = _builtin_steps(mode)
    expected_ids = [item["step_id"] for item in expected_builtins]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    builtin_ids: list[str] = []
    custom_seen = False
    for position, raw_step in enumerate(steps, start=1):
        step_id = str(raw_step.get("step_id") or "").strip()
        kind = str(raw_step.get("kind") or "builtin").strip().casefold()
        name = " ".join(str(raw_step.get("name") or "").split())
        if not step_id or step_id in seen:
            raise JournalAuthorWorkflowError("选刊作者流程的 step_id 必须非空且唯一")
        if kind not in {"builtin", "custom"}:
            raise JournalAuthorWorkflowError("选刊作者步骤类型必须是 builtin 或 custom")
        if not name:
            raise JournalAuthorWorkflowError(f"第 {position} 步缺少名称")
        if kind == "builtin":
            if custom_seen:
                raise JournalAuthorWorkflowError("自定义选刊作者步骤只能排在全部内置步骤之后")
            if len(builtin_ids) >= len(expected_builtins):
                raise JournalAuthorWorkflowError("当前流程包含多余的内置选刊作者步骤")
            expected = expected_builtins[len(builtin_ids)]
            if step_id != expected["step_id"]:
                raise JournalAuthorWorkflowError("当前模式的内置选刊作者步骤顺序或组合无效")
            prompt_path = Path(str(raw_step.get("prompt_path") or expected["prompt_path"])).resolve()
            if prompt_path != Path(expected["prompt_path"]).resolve():
                raise JournalAuthorWorkflowError("选刊作者内置步骤不能改绑提示词文件")
            if int(raw_step.get("contract_step") or expected["contract_step"]) != int(expected["contract_step"]):
                raise JournalAuthorWorkflowError("选刊作者内置步骤的产物契约不能修改")
            item = {**expected, "name": name}
            builtin_ids.append(step_id)
        else:
            custom_seen = True
            prompt_path = Path(str(raw_step.get("prompt_path") or "")).resolve()
            try:
                prompt_path.relative_to(JOURNAL_AUTHOR_CUSTOM_PROMPT_DIR.resolve())
            except ValueError as exc:
                raise JournalAuthorWorkflowError("自定义选刊作者提示词必须位于专用提示词目录") from exc
            item = {
                "step_id": step_id,
                "name": name,
                "kind": "custom",
                "source_step": 0,
                "contract_step": 3,
                "prompt_key": "custom",
                "prompt_path": str(prompt_path),
                "prompt_ready": bool(raw_step.get("prompt_ready")),
                "prompt_sha256": str(raw_step.get("prompt_sha256") or "").strip().casefold(),
            }
        try:
            item["execution_profile"] = normalize_execution_profile(
                raw_step.get("execution_profile") if "execution_profile" in raw_step else {},
                allow_inherit=True,
            )
        except StepExecutionProfileError as exc:
            raise JournalAuthorWorkflowError(f"第 {position} 步执行配置无效：{exc}") from exc
        item["position"] = position
        normalized.append(item)
        seen.add(step_id)

    if builtin_ids != expected_ids:
        raise JournalAuthorWorkflowError("当前模式必须保留完整的内置选刊作者交付链")
    digest = _digest(mode, default_profile, normalized)
    return {
        "schema": JOURNAL_AUTHOR_WORKFLOW_SNAPSHOT_SCHEMA,
        "version_id": str(raw.get("version_id") or f"journal-author-workflow-{digest[:16]}"),
        "mode": mode,
        "step_count": len(normalized),
        "sha256": digest,
        "created_at": str(raw.get("created_at") or now_iso()),
        "default_execution_profile": default_profile,
        "steps": normalized,
    }


def default_journal_author_workflow_snapshot() -> dict[str, Any]:
    return normalize_journal_author_workflow_snapshot(
        {"mode": "combined", "steps": _builtin_steps("combined"), "created_at": "legacy-combined-prompt"}
    )


def journal_author_workflow_step(snapshot: Any, position: int) -> dict[str, Any]:
    normalized = normalize_journal_author_workflow_snapshot(snapshot)
    if not 1 <= int(position) <= int(normalized["step_count"]):
        raise JournalAuthorWorkflowError(f"选刊作者步骤超出流程范围：{position}")
    return dict(normalized["steps"][int(position) - 1])


def journal_author_workflow_position_for_source(snapshot: Any, source_step: int | str) -> int:
    token = str(source_step)
    normalized = normalize_journal_author_workflow_snapshot(snapshot)
    for item in normalized["steps"]:
        if item["kind"] == "builtin" and str(item.get("source_step")) == token:
            return int(item["position"])
    raise JournalAuthorWorkflowError(f"当前选刊作者流程没有内置 Step {source_step}")


def journal_author_custom_prompt_fingerprint(path: str | Path) -> dict[str, Any]:
    prompt = Path(path).resolve()
    try:
        prompt.relative_to(JOURNAL_AUTHOR_CUSTOM_PROMPT_DIR.resolve())
        before = prompt.stat()
        data = prompt.read_bytes()
        after = prompt.stat()
    except (OSError, ValueError) as exc:
        raise JournalAuthorWorkflowError("无法读取自定义选刊作者提示词") from exc
    if not prompt.is_file() or not data or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise JournalAuthorWorkflowError("自定义选刊作者提示词必须是稳定的非空文件")
    return {
        "kind": "custom",
        "path": str(prompt),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mtime_ns": after.st_mtime_ns,
    }


def journal_author_workflow_prompt_fingerprint(root: str | Path, snapshot: Any, position: int) -> dict[str, Any]:
    step = journal_author_workflow_step(snapshot, position)
    if step["kind"] == "builtin":
        value = validate_journal_author_prompt(root, str(step["prompt_key"]))
        return {"kind": "builtin", "path": str(value.path.resolve()), "sha256": value.sha256.casefold(), "size": value.size}
    current = journal_author_custom_prompt_fingerprint(step["prompt_path"])
    if not step.get("prompt_ready") or current["sha256"] != str(step.get("prompt_sha256") or ""):
        raise JournalAuthorWorkflowError("自定义选刊作者提示词与任务快照不一致")
    return current


def journal_author_workflow_with_mode(snapshot: Any, mode: str) -> dict[str, Any]:
    current = normalize_journal_author_workflow_snapshot(snapshot)
    target_mode = str(mode or "").strip().casefold()
    if target_mode == current["mode"]:
        return current
    profiles = {str(item["step_id"]): copy.deepcopy(item.get("execution_profile") or {}) for item in current["steps"]}
    fallback = profiles.get(JOURNAL_AUTHOR_BUILTIN_STEP_ID) or profiles.get(JOURNAL_AUTHOR_PACKAGE_STEP_ID) or _empty_step_profile()
    builtins = _builtin_steps(target_mode)
    for item in builtins:
        item["execution_profile"] = copy.deepcopy(profiles.get(str(item["step_id"])) or fallback)
    custom = [copy.deepcopy(item) for item in current["steps"] if item["kind"] == "custom"]
    return normalize_journal_author_workflow_snapshot(
        {
            "mode": target_mode,
            "default_execution_profile": current["default_execution_profile"],
            "steps": builtins + custom,
            "created_at": now_iso(),
        }
    )


class JournalAuthorWorkflowStore:
    def __init__(self, path: str | Path | None = None, custom_prompt_dir: str | Path | None = None) -> None:
        self.path = Path(path or JOURNAL_AUTHOR_WORKFLOW_STATE_PATH)
        self.custom_prompt_dir = Path(custom_prompt_dir or JOURNAL_AUTHOR_CUSTOM_PROMPT_DIR)

    def load(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        if not isinstance(data, dict) or data.get("schema") != JOURNAL_AUTHOR_WORKFLOW_SCHEMA:
            snapshot = default_journal_author_workflow_snapshot()
            return {
                "schema": JOURNAL_AUTHOR_WORKFLOW_SCHEMA,
                "active_version_id": snapshot["version_id"],
                "versions": {snapshot["version_id"]: snapshot},
                "history": [],
            }
        versions = data.get("versions")
        if not isinstance(versions, dict):
            raise JournalAuthorWorkflowError("选刊作者流程版本库损坏")
        active = str(data.get("active_version_id") or "")
        if active not in versions:
            raise JournalAuthorWorkflowError("选刊作者流程当前版本不存在")
        normalize_journal_author_workflow_snapshot(versions[active])
        return data

    def active_snapshot(self) -> dict[str, Any]:
        state = self.load()
        return normalize_journal_author_workflow_snapshot(state["versions"][state["active_version_id"]])

    def executable_snapshot(self) -> dict[str, Any]:
        validate_journal_author_prompt(LEGACY_PROJECTS["journal_author"]["root"], "scheduler")
        snapshot = self.active_snapshot()
        for step in snapshot["steps"]:
            if step["kind"] == "builtin":
                validate_journal_author_prompt(LEGACY_PROJECTS["journal_author"]["root"], str(step["prompt_key"]))
            else:
                journal_author_workflow_prompt_fingerprint(LEGACY_PROJECTS["journal_author"]["root"], snapshot, int(step["position"]))
        return snapshot

    def inventory(self) -> dict[str, Any]:
        state = self.load()
        snapshot = self.active_snapshot()
        steps = copy.deepcopy(snapshot["steps"])
        for item in steps:
            item["effective_execution_profile"] = resolve_execution_profile(
                snapshot["default_execution_profile"],
                item.get("execution_profile"),
                fallback_model=JOURNAL_AUTHOR_CODEX_MODEL,
            )
        return {
            "active_version_id": snapshot["version_id"],
            "mode": snapshot["mode"],
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
        mode: str,
        default_execution_profile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        created_at = now_iso()
        if default_execution_profile is None:
            default_execution_profile = self.active_snapshot()["default_execution_profile"]
        candidate = normalize_journal_author_workflow_snapshot(
            {
                "mode": mode,
                "steps": steps,
                "default_execution_profile": dict(default_execution_profile),
                "created_at": created_at,
            }
        )
        candidate["version_id"] = (
            f"journal-author-workflow-{created_at.replace(':', '').replace('-', '')[:15]}-"
            f"{candidate['sha256'][:8]}-{uuid4().hex[:8]}"
        )
        state.setdefault("versions", {})[candidate["version_id"]] = candidate
        state["active_version_id"] = candidate["version_id"]
        state["updated_at"] = created_at
        history = state.setdefault("history", [])
        history.append({"at": created_at, "event": event, **dict(detail), "version_id": candidate["version_id"]})
        state["history"] = history[-200:]
        backup_file(self.path, "journal_author_workflow")
        write_json_atomic(self.path, state)
        return self.inventory()

    def set_mode(self, mode: str) -> dict[str, Any]:
        snapshot = journal_author_workflow_with_mode(self.active_snapshot(), mode)
        return self._publish(
            [dict(item) for item in snapshot["steps"]],
            "set_mode",
            {"mode": snapshot["mode"]},
            mode=str(snapshot["mode"]),
            default_execution_profile=snapshot["default_execution_profile"],
        )

    def add_custom_step(self, name: str) -> dict[str, Any]:
        title = " ".join(str(name or "").split())
        if not title:
            raise JournalAuthorWorkflowError("自定义选刊作者步骤名称不能为空")
        snapshot = self.active_snapshot()
        if int(snapshot["step_count"]) >= JOURNAL_AUTHOR_MAX_WORKFLOW_STEPS:
            raise JournalAuthorWorkflowError(f"选刊作者流程最多 {JOURNAL_AUTHOR_MAX_WORKFLOW_STEPS} 步")
        self.custom_prompt_dir.mkdir(parents=True, exist_ok=True)
        slug = _CUSTOM_ID_RE.sub("-", title.casefold()).strip("-")[:36] or "custom"
        digest = hashlib.sha256(f"{title}|{now_iso()}".encode("utf-8")).hexdigest()[:8]
        step_id = f"custom-{slug}-{digest}"
        path = self.custom_prompt_dir / f"{step_id}.md"
        write_text(
            path,
            f"# {title}\n\n这是用户自定义的选刊作者后处理步骤。请在下方补充完整要求。\n\n"
            "必须继续处理 `<paper_id>` 的既有 journal.md 与 package；不得编造期刊、作者或声明，"
            "完成后仍须保证投稿包通过最终产物契约。\n",
        )
        steps = [dict(item) for item in snapshot["steps"]]
        steps.append(
            {
                "step_id": step_id,
                "name": title,
                "kind": "custom",
                "source_step": 0,
                "contract_step": 3,
                "prompt_key": "custom",
                "prompt_path": str(path),
                "prompt_ready": False,
                "prompt_sha256": "",
                "execution_profile": _empty_step_profile(),
            }
        )
        return self._publish(steps, "add_custom_step", {"step_id": step_id, "name": title}, mode=str(snapshot["mode"]))

    def remove_step(self, step_id: str) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        index = next((i for i, item in enumerate(steps) if item["step_id"] == target), -1)
        if index < 0:
            raise JournalAuthorWorkflowError("没有找到要删除的选刊作者步骤")
        if steps[index]["kind"] == "builtin":
            raise JournalAuthorWorkflowError("内置选刊与投稿包交付链受保护，不能删除；可切换合并/精细模式")
        del steps[index]
        return self._publish(steps, "remove_step", {"step_id": target}, mode=str(snapshot["mode"]))

    def move_custom_step(self, step_id: str, direction: int) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        index = next((i for i, item in enumerate(steps) if item["step_id"] == target), -1)
        if index < 0 or steps[index]["kind"] != "custom":
            raise JournalAuthorWorkflowError("只有自定义选刊作者步骤可以调整顺序")
        destination = index + (-1 if int(direction) < 0 else 1)
        if not 0 <= destination < len(steps) or steps[destination]["kind"] != "custom":
            raise JournalAuthorWorkflowError("自定义选刊作者步骤不能移动到内置交付链之前")
        steps[index], steps[destination] = steps[destination], steps[index]
        return self._publish(steps, "move_custom_step", {"step_id": target, "direction": int(direction)}, mode=str(snapshot["mode"]))

    def restore_default(self) -> dict[str, Any]:
        snapshot = default_journal_author_workflow_snapshot()
        return self._publish(
            [dict(item) for item in snapshot["steps"]],
            "default_restored",
            {"mode": "combined"},
            mode="combined",
            default_execution_profile=snapshot["default_execution_profile"],
        )

    def update_default_execution_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        try:
            normalized = normalize_execution_profile(profile, allow_inherit=False, fallback_model=JOURNAL_AUTHOR_CODEX_MODEL)
        except StepExecutionProfileError as exc:
            raise JournalAuthorWorkflowError(str(exc)) from exc
        return self._publish(
            [dict(item) for item in snapshot["steps"]],
            "default_execution_profile_updated",
            {"profile": normalized},
            mode=str(snapshot["mode"]),
            default_execution_profile=normalized,
        )

    def update_step_execution_profile(self, step_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        target = str(step_id or "").strip()
        snapshot = self.active_snapshot()
        steps = [dict(item) for item in snapshot["steps"]]
        item = next((value for value in steps if str(value.get("step_id") or "") == target), None)
        if item is None:
            raise JournalAuthorWorkflowError("选刊作者步骤不存在")
        try:
            item["execution_profile"] = normalize_execution_profile(profile, allow_inherit=True)
        except StepExecutionProfileError as exc:
            raise JournalAuthorWorkflowError(str(exc)) from exc
        return self._publish(steps, "step_execution_profile_updated", {"step_id": target}, mode=str(snapshot["mode"]))

    def read_custom_prompt(self, step_id: str) -> dict[str, Any]:
        step = next((item for item in self.active_snapshot()["steps"] if item["step_id"] == step_id), None)
        if not step or step["kind"] != "custom":
            raise JournalAuthorWorkflowError("请选择一个自定义选刊作者步骤")
        path = Path(step["prompt_path"])
        return {"step_id": step_id, "path": str(path), "text": path.read_text(encoding="utf-8")}

    def save_custom_prompt(self, step_id: str, text: str) -> dict[str, Any]:
        snapshot = self.active_snapshot()
        step = next((item for item in snapshot["steps"] if item["step_id"] == step_id), None)
        if not step or step["kind"] != "custom":
            raise JournalAuthorWorkflowError("内置提示词受保护；这里只能保存自定义选刊作者步骤提示词")
        body = str(text or "")
        if not body.strip():
            raise JournalAuthorWorkflowError("自定义选刊作者步骤提示词不能为空")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        path = self.custom_prompt_dir / f"{step_id}-{digest[:12]}.md"
        if not path.exists():
            write_text(path, body)
        fingerprint = journal_author_custom_prompt_fingerprint(path)
        steps = [dict(item) for item in snapshot["steps"]]
        for item in steps:
            if item["step_id"] == step_id:
                item["prompt_path"] = str(path)
                item["prompt_ready"] = True
                item["prompt_sha256"] = fingerprint["sha256"]
                break
        result = self._publish(steps, "save_custom_prompt", {"step_id": step_id, "path": str(path)}, mode=str(snapshot["mode"]))
        result.update({"status": "saved", "step_id": step_id, "path": str(path), "fingerprint": fingerprint})
        return result


__all__ = [
    "JOURNAL_AUTHOR_BUILTIN_STEP_ID",
    "JOURNAL_AUTHOR_CHOOSE_STEP_ID",
    "JOURNAL_AUTHOR_PACKAGE_STEP_ID",
    "JOURNAL_AUTHOR_WORKFLOW_MODES",
    "JOURNAL_AUTHOR_WORKFLOW_SCHEMA",
    "JournalAuthorWorkflowError",
    "JournalAuthorWorkflowStore",
    "default_journal_author_workflow_snapshot",
    "journal_author_custom_prompt_fingerprint",
    "journal_author_workflow_position_for_source",
    "journal_author_workflow_prompt_fingerprint",
    "journal_author_workflow_step",
    "journal_author_workflow_with_mode",
    "normalize_journal_author_workflow_snapshot",
]
