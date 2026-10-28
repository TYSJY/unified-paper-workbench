from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import sys
from pathlib import Path

import PyInstaller
import codex_cli_bin
import openai
import openai_codex
import pytest
from unified_paper_orchestrator.codex_runtime import runtime_identity


TRUSTED_POWERSHELL_HOME = Path(
    r"C:\Windows\System32\WindowsPowerShell\v1.0"
)
TRUSTED_POWERSHELL_MODULE_PATH = TRUSTED_POWERSHELL_HOME / "Modules"
TRUSTED_GIT_PATH = Path(r"C:\Program Files\Git\cmd\git.exe")


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    paths = [str(Path(item).resolve()) for item in sys.path if item]
    meta_path = [
        f"{type(item).__module__}.{type(item).__qualname__}"
        for item in sys.meta_path
    ]
    powershell_path = shutil.which("powershell.exe") or ""
    git_path = shutil.which("git.exe") or ""
    module_paths = [
        item
        for item in os.environ.get("PSMODULEPATH", "").split(os.pathsep)
        if item
    ]
    payload = {
        "no_site": bool(sys.flags.no_site),
        "safe_path": bool(sys.flags.safe_path),
        "workspace_first": bool(paths) and Path(paths[0]) == workspace,
        "empty_path_absent": "" not in sys.path,
        "mesa_path_absent": all("mesa" not in item.casefold() for item in paths),
        "editable_finder_absent": all(
            "editable" not in item.casefold() for item in meta_path
        ),
        "trusted_powershell_path": bool(powershell_path)
        and _normalized_path(powershell_path)
        == _normalized_path(TRUSTED_POWERSHELL_HOME / "powershell.exe"),
        "trusted_psmodule_path": len(module_paths) == 1
        and _normalized_path(module_paths[0])
        == _normalized_path(TRUSTED_POWERSHELL_MODULE_PATH),
        "trusted_git_path": bool(git_path)
        and _normalized_path(git_path) == _normalized_path(TRUSTED_GIT_PATH),
        "powershell_origin": powershell_path,
        "git_origin": git_path,
        "psmodule_path": module_paths,
        "pytest_origin": str(Path(pytest.__file__).resolve()),
        "pyinstaller_origin": str(Path(PyInstaller.__file__).resolve()),
        "openai_origin": str(Path(openai.__file__).resolve()),
        "openai_version": openai.__version__,
        "openai_codex_origin": str(Path(openai_codex.__file__).resolve()),
        "openai_codex_version": importlib.metadata.version("openai-codex"),
        "codex_cli_bin_version": importlib.metadata.version("openai-codex-cli-bin"),
        "codex_runtime_path": str((runtime_identity() or {})["binary_path"]),
        "selected_runtime_version": str((runtime_identity() or {})["version"]),
        "sys_path": paths,
        "sys_meta_path": meta_path,
    }
    if not all(
        payload[key]
        for key in (
            "no_site",
            "safe_path",
            "workspace_first",
            "empty_path_absent",
            "mesa_path_absent",
            "editable_finder_absent",
            "trusted_powershell_path",
            "trusted_psmodule_path",
            "trusted_git_path",
        )
    ):
        raise RuntimeError(f"isolated build environment mismatch: {payload}")
    if payload["openai_version"] != "2.45.0":
        raise RuntimeError(f"unexpected OpenAI SDK version: {payload}")
    if payload["openai_codex_version"] != "0.147.0":
        raise RuntimeError(f"unexpected Codex SDK version: {payload}")
    if payload["codex_cli_bin_version"] != "0.147.0":
        raise RuntimeError(f"unexpected bundled Codex runtime version: {payload}")
    if not Path(payload["codex_runtime_path"]).is_file():
        raise RuntimeError(f"bundled Codex runtime is unavailable: {payload}")
    if payload["selected_runtime_version"] != "0.153.4":
        raise RuntimeError("Application native runtime was not upgraded")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
