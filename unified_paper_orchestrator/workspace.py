from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4


WORKSPACE_SCHEMA = "unified-paper-orchestrator/workspace/v1"
WORKSPACE_REGISTRY_SCHEMA = "unified-paper-orchestrator/workspace-registry/v1"
WORKSPACE_MIGRATION_SCHEMA = "unified-paper-orchestrator/workspace-migration/v1"
WORKSPACE_ENV = "UNIFIED_PAPER_WORKSPACE"
WORKSPACE_REGISTRY_ENV = "UNIFIED_PAPER_WORKSPACE_REGISTRY"
WORKSPACE_DISABLE_REGISTRY_ENV = "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"
WORKSPACE_MANIFEST_NAME = "workspace.json"
STAGE_NAMES = ("writing", "figures", "journal_author", "submission")

_LEGACY_ROOTS = {
    "writing": Path(r"D:\Documents\项目控制平台\edge-llm-research"),
    "figures": Path(r"D:\Documents\论文画图\paper-figure-codex"),
    "journal_author": Path(r"D:\Documents\期刊选刊与作者"),
    "submission": Path(r"D:\Documents\New project 11"),
}
_STAGE_ENV = {
    "writing": "UNIFIED_WRITING_ROOT",
    "figures": "UNIFIED_FIGURE_ROOT",
    "journal_author": "UNIFIED_JOURNAL_AUTHOR_ROOT",
    "submission": "UNIFIED_SUBMISSION_ROOT",
}
_DEFAULT_STAGE_PATHS = {
    "writing": "stages/writing",
    "figures": "stages/figures",
    "journal_author": "stages/journal_author",
    "submission": "stages/submission",
}
_STATE_FILE_NAMES = {
    "writing": "pipeline_state.json",
    "figures": "figure_pipeline_state.json",
    "journal_author": "journal_author_state.json",
    "submission": "auto_submission_panel_state.json",
}
_SCHEDULER_PROMPT_NAMES = {
    "writing": "SCHEDULER_PROMPT.md",
    "figures": "FIGURE_SCHEDULER_PROMPT.md",
    "journal_author": "JOURNAL_SCHEDULER_PROMPT.md",
    "submission": "AUTO_SUBMISSION_SCHEDULER_PROMPT.md",
}
_GENERAL_EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tmp",
}
_STATE_EXCLUDED_DIRS = {
    "canary_compile",
    "e2e_runs",
    "logs",
    "tmp",
    "v2",
}
_CONTROLLER_EXCLUDED_DIRS = {
    "audit_runs",
    "run_intents",
    "upload_staging",
}
_SEED_SUPPORT_DIRS = {
    "writing": (),
    "figures": ("tools",),
    "journal_author": (".agents", ".codex"),
    "submission": ("tools", "自动投稿skill"),
}
_LOCK_SUFFIXES = (".lock", ".lck")


class WorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WorkspaceDescriptor:
    workspace_id: str
    name: str
    root: Path
    state_dir: Path
    stage_roots: Mapping[str, Path]
    mode: str
    manifest_path: Path | None = None
    created_at: str = ""
    path_aliases: Mapping[str, str] | None = None

    @property
    def is_legacy_bridge(self) -> bool:
        return self.mode == "legacy_bridge"

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "root": str(self.root),
            "state_dir": str(self.state_dir),
            "stage_roots": {stage: str(path) for stage, path in self.stage_roots.items()},
            "mode": self.mode,
            "manifest_path": str(self.manifest_path or ""),
            "created_at": self.created_at,
            "path_aliases": dict(self.path_aliases or {}),
            "is_legacy_bridge": self.is_legacy_bridge,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _registry_enabled() -> bool:
    return os.environ.get(WORKSPACE_DISABLE_REGISTRY_ENV, "").strip().casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def registry_path() -> Path:
    override = os.environ.get(WORKSPACE_REGISTRY_ENV, "").strip()
    if override:
        return _normalize_path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "UnifiedPaperOrchestrator" / "workspaces.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            "WORKSPACE_JSON_UNREADABLE",
            f"无法读取工作区配置：{path}（{type(exc).__name__}）",
        ) from exc


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_registry() -> dict[str, Any]:
    path = registry_path()
    data = _read_json(path, {})
    if not data:
        return {
            "schema": WORKSPACE_REGISTRY_SCHEMA,
            "version": 1,
            "active_workspace_id": "",
            "active_workspace_path": "",
            "workspaces": [],
        }
    if not isinstance(data, dict) or data.get("schema") != WORKSPACE_REGISTRY_SCHEMA:
        raise WorkspaceError("WORKSPACE_REGISTRY_INVALID", f"工作区注册表格式无效：{path}")
    records = data.get("workspaces")
    if not isinstance(records, list):
        raise WorkspaceError("WORKSPACE_REGISTRY_INVALID", f"工作区列表格式无效：{path}")
    return data


def _workspace_record(descriptor: WorkspaceDescriptor) -> dict[str, Any]:
    return {
        "workspace_id": descriptor.workspace_id,
        "name": descriptor.name,
        "path": str(descriptor.root),
        "mode": descriptor.mode,
        "last_opened_at": _now_iso(),
    }


def register_workspace(
    descriptor: WorkspaceDescriptor,
    *,
    make_active: bool = False,
) -> dict[str, Any]:
    registry = read_registry()
    records = [
        dict(item)
        for item in registry.get("workspaces", [])
        if isinstance(item, dict)
        and str(item.get("workspace_id") or "") != descriptor.workspace_id
        and str(item.get("path") or "").casefold() != str(descriptor.root).casefold()
    ]
    records.append(_workspace_record(descriptor))
    records.sort(key=lambda item: str(item.get("name") or "").casefold())
    registry["workspaces"] = records
    registry["updated_at"] = _now_iso()
    if make_active:
        registry["active_workspace_id"] = descriptor.workspace_id
        registry["active_workspace_path"] = str(descriptor.root)
    _write_json_atomic(registry_path(), registry)
    return registry


def remove_registered_workspace(workspace_id: str) -> dict[str, Any]:
    target = str(workspace_id or "").strip()
    registry = read_registry()
    records = [
        dict(item)
        for item in registry.get("workspaces", [])
        if isinstance(item, dict) and str(item.get("workspace_id") or "") != target
    ]
    registry["workspaces"] = records
    if str(registry.get("active_workspace_id") or "") == target:
        registry["active_workspace_id"] = ""
        registry["active_workspace_path"] = ""
    registry["updated_at"] = _now_iso()
    _write_json_atomic(registry_path(), registry)
    return registry


def clear_active_workspace() -> dict[str, Any]:
    registry = read_registry()
    registry["active_workspace_id"] = ""
    registry["active_workspace_path"] = ""
    registry["updated_at"] = _now_iso()
    _write_json_atomic(registry_path(), registry)
    return registry


def list_registered_workspaces() -> list[dict[str, Any]]:
    registry = read_registry()
    active_id = str(registry.get("active_workspace_id") or "")
    rows: list[dict[str, Any]] = []
    for item in registry.get("workspaces", []):
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("path") or "")).expanduser()
        manifest = path / WORKSPACE_MANIFEST_NAME
        rows.append(
            {
                **dict(item),
                "exists": path.is_dir(),
                "manifest_exists": manifest.is_file(),
                "active": str(item.get("workspace_id") or "") == active_id,
            }
        )
    return rows


def _resolve_manifest_path(root: Path, value: object, label: str) -> Path:
    token = str(value or "").strip()
    if not token:
        raise WorkspaceError("WORKSPACE_PATH_MISSING", f"工作区缺少路径：{label}")
    candidate = Path(token).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(
            "WORKSPACE_PATH_OUTSIDE_ROOT",
            f"正式工作区路径必须位于工作区根目录内：{label}={resolved}",
        ) from exc
    return resolved


def load_workspace(path: str | Path) -> WorkspaceDescriptor:
    root = _normalize_path(path)
    manifest_path = root / WORKSPACE_MANIFEST_NAME
    data = _read_json(manifest_path, None)
    if not isinstance(data, dict) or data.get("schema") != WORKSPACE_SCHEMA:
        raise WorkspaceError("WORKSPACE_MANIFEST_INVALID", f"所选目录不是有效论文工作区：{root}")
    if str(data.get("status") or "ready") != "ready":
        raise WorkspaceError("WORKSPACE_NOT_READY", f"论文工作区尚未完成创建或导入：{root}")
    workspace_id = str(data.get("workspace_id") or "").strip()
    name = str(data.get("name") or "").strip()
    paths = data.get("paths")
    if not workspace_id or not name or not isinstance(paths, dict):
        raise WorkspaceError("WORKSPACE_MANIFEST_INVALID", f"论文工作区清单缺少必要字段：{manifest_path}")
    state_dir = _resolve_manifest_path(root, paths.get("state"), "state")
    stage_roots = {
        stage: _resolve_manifest_path(root, paths.get(stage), stage)
        for stage in STAGE_NAMES
    }
    aliases = data.get("path_aliases")
    return WorkspaceDescriptor(
        workspace_id=workspace_id,
        name=name,
        root=root,
        state_dir=state_dir,
        stage_roots=stage_roots,
        mode=str(data.get("mode") or "managed"),
        manifest_path=manifest_path,
        created_at=str(data.get("created_at") or ""),
        path_aliases=dict(aliases) if isinstance(aliases, dict) else {},
    )


def legacy_workspace(app_root: Path) -> WorkspaceDescriptor:
    stage_roots: dict[str, Path] = {}
    for stage, default in _LEGACY_ROOTS.items():
        override = os.environ.get(_STAGE_ENV[stage], "").strip()
        stage_roots[stage] = _normalize_path(override) if override else default
    return WorkspaceDescriptor(
        workspace_id="legacy-current",
        name="当前兼容工作区",
        root=app_root.resolve(),
        state_dir=(app_root / "state").resolve(),
        stage_roots=stage_roots,
        mode="legacy_bridge",
        manifest_path=None,
        created_at="",
        path_aliases={},
    )


def active_workspace(app_root: Path) -> WorkspaceDescriptor:
    explicit = os.environ.get(WORKSPACE_ENV, "").strip()
    if explicit:
        return load_workspace(explicit)
    if _registry_enabled():
        try:
            registry = read_registry()
            selected = str(registry.get("active_workspace_path") or "").strip()
            if selected:
                return load_workspace(selected)
        except WorkspaceError:
            # A stale recent-workspace entry must not make the application
            # impossible to start. The manager can repair it from the UI.
            pass
    return legacy_workspace(app_root)


def extract_workspace_argument(argv: Iterable[str]) -> tuple[str, list[str]]:
    args = [str(item) for item in argv]
    selected = ""
    remaining: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--workspace":
            if index + 1 >= len(args):
                raise WorkspaceError("WORKSPACE_ARGUMENT_MISSING", "--workspace 后必须提供目录")
            selected = args[index + 1]
            index += 2
            continue
        if item.startswith("--workspace="):
            selected = item.split("=", 1)[1]
            index += 1
            continue
        remaining.append(item)
        index += 1
    return selected, remaining


def bootstrap_process_workspace(argv: Iterable[str] | None = None) -> str:
    selected, _remaining = extract_workspace_argument(sys.argv[1:] if argv is None else argv)
    if selected:
        descriptor = load_workspace(selected)
        os.environ[WORKSPACE_ENV] = str(descriptor.root)
        return str(descriptor.root)
    if os.environ.get(WORKSPACE_ENV, "").strip() or not _registry_enabled():
        return os.environ.get(WORKSPACE_ENV, "").strip()
    try:
        registry = read_registry()
        selected = str(registry.get("active_workspace_path") or "").strip()
        if selected:
            descriptor = load_workspace(selected)
            os.environ[WORKSPACE_ENV] = str(descriptor.root)
            return str(descriptor.root)
    except WorkspaceError:
        pass
    return ""


def set_active_workspace(path: str | Path) -> WorkspaceDescriptor:
    descriptor = load_workspace(path)
    register_workspace(descriptor, make_active=True)
    return descriptor


def _safe_slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return normalized or "PaperWorkspace"


def suggested_workspace_directory(parent: str | Path, name: str) -> Path:
    base = _normalize_path(parent)
    return base / _safe_slug(name)


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _excluded_directory(path: Path, *, stage: str, relative_parts: tuple[str, ...]) -> bool:
    name = path.name
    lowered = name.casefold()
    if lowered in _GENERAL_EXCLUDED_DIRS:
        return True
    if "controller" in {item.casefold() for item in relative_parts}:
        if lowered in {"logs", "trash"} | _CONTROLLER_EXCLUDED_DIRS:
            return True
    if stage == "submission":
        if lowered.startswith(".chrome-"):
            return True
        if "迁移包" in name or lowered.startswith("migration"):
            return True
    return False


def _copy_tree_filtered(
    source: Path,
    target: Path,
    *,
    stage: str,
    stats: dict[str, Any],
    relative_parts: tuple[str, ...] = (),
) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        next_parts = relative_parts + (child.name,)
        if _is_reparse_point(child):
            stats["skipped_reparse_points"] += 1
            continue
        if child.is_dir():
            if _excluded_directory(child, stage=stage, relative_parts=relative_parts):
                stats["skipped_directories"].append(str(child))
                continue
            _copy_tree_filtered(
                child,
                target / child.name,
                stage=stage,
                stats=stats,
                relative_parts=next_parts,
            )
            continue
        if not child.is_file():
            continue
        if child.name.casefold().endswith(_LOCK_SUFFIXES):
            stats["skipped_lock_files"] += 1
            continue
        destination = target / child.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, destination)
        stats["files"] += 1
        stats["bytes"] += int(child.stat().st_size)


def _copy_seed_files(source: Path, target: Path, *, stage: str, stats: dict[str, Any]) -> None:
    prompts = source / "prompts"
    if prompts.is_dir():
        _copy_tree_filtered(prompts, target / "prompts", stage=stage, stats=stats)
    controller = source / "controller"
    if controller.is_dir():
        # Empty workspaces still need the compatibility scripts, author
        # templates and submission helpers that the stage prompts call.  The
        # copied mutable state is replaced with a fresh paused state below.
        _copy_tree_filtered(
            controller,
            target / "controller",
            stage=stage,
            stats=stats,
            relative_parts=("controller",),
        )
    else:
        (target / "controller").mkdir(parents=True, exist_ok=True)
    for folder in _SEED_SUPPORT_DIRS[stage]:
        support = source / folder
        if support.is_dir():
            _copy_tree_filtered(
                support,
                target / folder,
                stage=stage,
                stats=stats,
                relative_parts=(folder,),
            )
    for name in ("README.md", "AGENTS.md", "research_profile.md", "author_info.md"):
        path = source / name
        if path.is_file():
            shutil.copy2(path, target / name)
            stats["files"] += 1
            stats["bytes"] += int(path.stat().st_size)
    for folder in ("papers", "ideas", "refs"):
        (target / folder).mkdir(parents=True, exist_ok=True)


def _empty_stage_state(stage: str, source_state: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(source_state or {})
    settings = dict(source.get("settings") or {}) if isinstance(source.get("settings"), dict) else {}
    settings["scheduling_mode"] = "manual"
    settings.pop("target_paper_ids", None)
    settings.pop("run_scope", None)
    settings.pop("run_now_status", None)
    base: dict[str, Any] = {
        "version": int(source.get("version") or 1),
        "updated_at": _now_iso(),
        "settings": settings,
        "commands": [],
    }
    if stage == "writing":
        base["sessions"] = {}
    else:
        base.update({"papers": {}, "paper_queue": [], "active_paper_id": ""})
    if stage == "submission":
        base["deferred_queue"] = []
    return base


def _seed_empty_state(source: WorkspaceDescriptor, target: WorkspaceDescriptor) -> None:
    for stage in STAGE_NAMES:
        source_state_path = source.stage_roots[stage] / "controller" / _STATE_FILE_NAMES[stage]
        source_state = _read_json(source_state_path, {})
        if not isinstance(source_state, dict):
            source_state = {}
        target_state_path = target.stage_roots[stage] / "controller" / _STATE_FILE_NAMES[stage]
        _write_json_atomic(target_state_path, _empty_stage_state(stage, source_state))


def _copy_workspace_state(
    source: WorkspaceDescriptor,
    target: WorkspaceDescriptor,
    *,
    import_data: bool,
    stats: dict[str, Any],
) -> None:
    if not import_data or not source.state_dir.is_dir():
        return
    for child in source.state_dir.iterdir():
        if _is_reparse_point(child):
            stats["skipped_reparse_points"] += 1
            continue
        if child.is_dir() and child.name.casefold() in _STATE_EXCLUDED_DIRS:
            stats["skipped_directories"].append(str(child))
            continue
        if child.is_file() and child.name.casefold().endswith(_LOCK_SUFFIXES):
            stats["skipped_lock_files"] += 1
            continue
        destination = target.state_dir / child.name
        if child.is_dir():
            _copy_tree_filtered(child, destination, stage="state", stats=stats)
        elif child.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)
            stats["files"] += 1
            stats["bytes"] += int(child.stat().st_size)


def _replace_paths(value: Any, replacements: Mapping[str, str]) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        result: dict[str, Any] = {}
        for key, item in value.items():
            next_item, item_changed = _replace_paths(item, replacements)
            result[str(key)] = next_item
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        changed = False
        result_list: list[Any] = []
        for item in value:
            next_item, item_changed = _replace_paths(item, replacements)
            result_list.append(next_item)
            changed = changed or item_changed
        return result_list, changed
    if not isinstance(value, str):
        return value, False
    text = value
    for old, new in replacements.items():
        for source, destination in (
            (old, new),
            (old.replace("\\", "/"), new.replace("\\", "/")),
        ):
            text = re.sub(
                re.escape(source),
                lambda _match, replacement=destination: replacement,
                text,
                flags=re.IGNORECASE,
            )
    return text, text != value


def _rewrite_json_file(path: Path, replacements: Mapping[str, str]) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    updated, changed = _replace_paths(data, replacements)
    if changed and isinstance(updated, (dict, list)):
        _write_json_atomic(path, updated)
    return changed


def _pause_imported_state(path: Path) -> None:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return
    changed = False
    settings = data.get("settings")
    if isinstance(settings, dict) and str(settings.get("scheduling_mode") or "") != "manual":
        settings["scheduling_mode"] = "manual"
        changed = True
    if data.get("module") == "smart_submission_sidecar" and str(data.get("mode") or "") != "manual":
        data["mode"] = "manual"
        changed = True
    if changed:
        data["updated_at"] = _now_iso()
        _write_json_atomic(path, data)


def _normalize_imported_state(
    source: WorkspaceDescriptor,
    target: WorkspaceDescriptor,
    *,
    import_data: bool,
) -> int:
    raw_replacements = {
        str(source.root): str(target.root),
        str(source.state_dir): str(target.state_dir),
        **{
            str(source.stage_roots[stage]): str(target.stage_roots[stage])
            for stage in STAGE_NAMES
        },
    }
    replacements = dict(
        sorted(
            raw_replacements.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
    changed = 0
    candidates: list[Path] = []
    for stage in STAGE_NAMES:
        controller = target.stage_roots[stage] / "controller"
        if controller.is_dir():
            candidates.extend(controller.glob("*.json"))
    if target.state_dir.is_dir():
        candidates.extend(target.state_dir.glob("*.json"))
        smart = target.state_dir / "smart_submission"
        if smart.is_dir():
            candidates.extend(smart.rglob("*.json"))
    for path in candidates:
        if _rewrite_json_file(path, replacements):
            changed += 1
    for stage in STAGE_NAMES:
        _pause_imported_state(
            target.stage_roots[stage] / "controller" / _STATE_FILE_NAMES[stage]
        )
    _pause_imported_state(target.state_dir / "smart_submission" / "smart_submission_state.json")
    if not import_data:
        _seed_empty_state(source, target)
    return changed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _prompt_manifest(source: WorkspaceDescriptor, target: WorkspaceDescriptor) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stage in STAGE_NAMES:
        source_files: list[Path] = []
        prompt_dir = source.stage_roots[stage] / "prompts"
        if prompt_dir.is_dir():
            source_files.extend(path for path in prompt_dir.rglob("*") if path.is_file())
        scheduler = source.stage_roots[stage] / "controller" / _SCHEDULER_PROMPT_NAMES[stage]
        if scheduler.is_file():
            source_files.append(scheduler)
        for source_path in sorted(source_files):
            relative = source_path.relative_to(source.stage_roots[stage])
            target_path = target.stage_roots[stage] / relative
            if not target_path.is_file():
                raise WorkspaceError(
                    "WORKSPACE_PROMPT_COPY_MISSING",
                    f"工作区缺少提示词副本：{stage}/{relative.as_posix()}",
                )
            source_hash = _sha256(source_path)
            target_hash = _sha256(target_path)
            if source_hash != target_hash:
                raise WorkspaceError(
                    "WORKSPACE_PROMPT_COPY_MISMATCH",
                    f"提示词复制后字节不一致：{stage}/{relative.as_posix()}",
                )
            rows.append(
                {
                    "stage": stage,
                    "relative_path": relative.as_posix(),
                    "size": int(target_path.stat().st_size),
                    "sha256": target_hash,
                }
            )
    return rows


def create_workspace(
    path: str | Path,
    name: str,
    *,
    source: WorkspaceDescriptor,
    import_data: bool = False,
    make_active: bool = False,
) -> WorkspaceDescriptor:
    root = _normalize_path(path)
    display_name = " ".join(str(name or "").split())
    if not display_name:
        raise WorkspaceError("WORKSPACE_NAME_REQUIRED", "工作区名称不能为空")
    manifest_path = root / WORKSPACE_MANIFEST_NAME
    if manifest_path.exists():
        descriptor = load_workspace(root)
        register_workspace(descriptor, make_active=make_active)
        return descriptor
    if root.exists():
        existing = [child for child in root.iterdir() if child.name not in {"desktop.ini"}]
        if existing:
            raise WorkspaceError(
                "WORKSPACE_DIRECTORY_NOT_EMPTY",
                f"目标目录不是空目录，也不是已有工作区：{root}",
            )
    root.mkdir(parents=True, exist_ok=True)
    workspace_id = str(uuid4())
    created_at = _now_iso()
    aliases = {
        str(source.stage_roots[stage]): str(root / _DEFAULT_STAGE_PATHS[stage])
        for stage in STAGE_NAMES
    }
    manifest: dict[str, Any] = {
        "schema": WORKSPACE_SCHEMA,
        "version": 1,
        "workspace_id": workspace_id,
        "name": display_name,
        "mode": "managed",
        "status": "creating",
        "created_at": created_at,
        "updated_at": created_at,
        "paths": {"state": "state", **_DEFAULT_STAGE_PATHS},
        "path_aliases": aliases,
        "import": {
            "mode": "copy_existing" if import_data else "empty",
            "source_workspace_id": source.workspace_id,
            "source_workspace_name": source.name,
            "source_workspace_root": str(source.root),
        },
    }
    _write_json_atomic(manifest_path, manifest)
    provisional = WorkspaceDescriptor(
        workspace_id=workspace_id,
        name=display_name,
        root=root,
        state_dir=root / "state",
        stage_roots={stage: root / relative for stage, relative in _DEFAULT_STAGE_PATHS.items()},
        mode="managed",
        manifest_path=manifest_path,
        created_at=created_at,
        path_aliases=aliases,
    )
    stats: dict[str, Any] = {
        "files": 0,
        "bytes": 0,
        "skipped_directories": [],
        "skipped_reparse_points": 0,
        "skipped_lock_files": 0,
    }
    try:
        provisional.state_dir.mkdir(parents=True, exist_ok=True)
        for stage in STAGE_NAMES:
            target_root = provisional.stage_roots[stage]
            target_root.mkdir(parents=True, exist_ok=True)
            source_root = source.stage_roots[stage]
            if import_data:
                _copy_tree_filtered(source_root, target_root, stage=stage, stats=stats)
            else:
                _copy_seed_files(source_root, target_root, stage=stage, stats=stats)
        _copy_workspace_state(source, provisional, import_data=import_data, stats=stats)
        rewritten_json_files = _normalize_imported_state(
            source,
            provisional,
            import_data=import_data,
        )
        prompts = _prompt_manifest(source, provisional)
        migration_dir = root / "legacy_import"
        migration_dir.mkdir(parents=True, exist_ok=True)
        migration = {
            "schema": WORKSPACE_MIGRATION_SCHEMA,
            "version": 1,
            "created_at": _now_iso(),
            "mode": "copy_existing" if import_data else "empty",
            "source": source.as_dict(),
            "target": provisional.as_dict(),
            "copied_files": stats["files"],
            "copied_bytes": stats["bytes"],
            "rewritten_json_files": rewritten_json_files,
            "skipped_directories": stats["skipped_directories"],
            "skipped_reparse_points": stats["skipped_reparse_points"],
            "skipped_lock_files": stats["skipped_lock_files"],
            "prompt_files": prompts,
            "prompts_byte_identical": True,
            "source_directories_untouched": True,
        }
        _write_json_atomic(migration_dir / "migration_manifest.json", migration)
        manifest.update(
            {
                "status": "ready",
                "updated_at": _now_iso(),
                "import": {
                    **dict(manifest["import"]),
                    "completed_at": _now_iso(),
                    "copied_files": stats["files"],
                    "copied_bytes": stats["bytes"],
                    "prompts_byte_identical": True,
                },
            }
        )
        _write_json_atomic(manifest_path, manifest)
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["updated_at"] = _now_iso()
        manifest["failure_type"] = type(exc).__name__
        try:
            _write_json_atomic(manifest_path, manifest)
        except OSError:
            pass
        raise
    descriptor = load_workspace(root)
    register_workspace(descriptor, make_active=make_active)
    return descriptor


def workspace_status(app_root: Path) -> dict[str, Any]:
    descriptor = active_workspace(app_root)
    registered = list_registered_workspaces() if _registry_enabled() else []
    return {
        "schema": "unified-paper-orchestrator/workspace-status/v1",
        "active": descriptor.as_dict(),
        "registry_path": str(registry_path()),
        "registered": registered,
    }


__all__ = [
    "STAGE_NAMES",
    "WORKSPACE_DISABLE_REGISTRY_ENV",
    "WORKSPACE_ENV",
    "WORKSPACE_MANIFEST_NAME",
    "WorkspaceDescriptor",
    "WorkspaceError",
    "active_workspace",
    "bootstrap_process_workspace",
    "clear_active_workspace",
    "create_workspace",
    "extract_workspace_argument",
    "legacy_workspace",
    "list_registered_workspaces",
    "load_workspace",
    "read_registry",
    "register_workspace",
    "registry_path",
    "remove_registered_workspace",
    "set_active_workspace",
    "suggested_workspace_directory",
    "workspace_status",
]
