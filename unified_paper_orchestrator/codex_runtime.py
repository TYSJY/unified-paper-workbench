"""Select the app-owned native runtime, independently of the Python SDK release.

The Python SDK pins its own CLI dependency.  We leave that dependency intact
and use its supported CodexConfig.codex_bin override for the newer runtime.
Never discover an arbitrary executable on PATH or borrow the desktop login.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

NATIVE_RUNTIME_VERSION = "0.153.4"
PYTHON_SDK_VERSION = "0.147.0"


def application_runtime_dir() -> Path | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    directory = base / "runtime" / f"codex-{NATIVE_RUNTIME_VERSION}" / "codex_cli_bin"
    if directory.is_dir():
        return directory
    if getattr(sys, "frozen", False):
        raise FileNotFoundError("The application-owned Codex runtime is missing")
    return None


def runtime_identity() -> dict[str, Any] | None:
    directory = application_runtime_dir()
    if directory is None:
        return None
    metadata = json.loads((directory / "codex-package.json").read_text(encoding="utf-8"))
    if metadata.get("version") != NATIVE_RUNTIME_VERSION:
        raise ValueError("Application Codex runtime version mismatch")
    binary = directory / "bin" / ("codex.exe" if os.name == "nt" else "codex")
    if not binary.is_file():
        raise FileNotFoundError("Application Codex runtime binary is missing")
    return {"version": NATIVE_RUNTIME_VERSION, "binary_path": str(binary),
            "source": "application_pinned", "directory": str(directory)}


def runtime_launch_options(environment: dict[str, str]) -> dict[str, Any]:
    identity = runtime_identity()
    env = dict(environment)
    if identity is None:
        return {"env": env}
    path_dir = Path(identity["directory"]) / "codex-path"
    # Explicit codex_bin bypasses the SDK's automatic native helper PATH setup.
    if path_dir.is_dir():
        inherited = {**os.environ, **env}
        path_key = next((key for key in inherited if key.upper() == "PATH"), "PATH")
        current = next((value for key, value in env.items() if key.upper() == "PATH"),
                       inherited.get(path_key, ""))
        env = {key: value for key, value in env.items() if key.upper() != "PATH"}
        env[path_key] = str(path_dir) + os.pathsep + current
    return {"codex_bin": identity["binary_path"], "env": env}
