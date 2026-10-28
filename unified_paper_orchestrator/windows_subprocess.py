"""Windows subprocess helpers that prevent console-window flashes.

The desktop product is GUI-first, but several deterministic tools and the
``openai-codex`` SDK launch console-subsystem executables.  On Windows those
children must inherit redirected pipes without creating a visible console.
This module keeps that policy in one place instead of scattering magic flags
through every stage runtime.
"""

from __future__ import annotations

import os
import subprocess
import threading
from types import ModuleType
from typing import Any, Mapping


CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


def no_window_subprocess_kwargs(
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of Popen/run kwargs with the Windows console hidden."""

    kwargs = dict(values or {})
    if os.name != "nt":
        return kwargs
    flags = int(kwargs.get("creationflags") or 0)
    flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW))
    kwargs["creationflags"] = flags
    startupinfo = kwargs.get("startupinfo")
    if startupinfo is None:
        startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
        startupinfo = startupinfo_type() if startupinfo_type is not None else None
    if startupinfo is not None:
        startupinfo.dwFlags |= int(
            getattr(subprocess, "STARTF_USESHOWWINDOW", STARTF_USESHOWWINDOW)
        )
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", SW_HIDE))
        kwargs["startupinfo"] = startupinfo
    return kwargs


class _HiddenSubprocessProxy:
    """Module-like proxy overriding only ``Popen`` for one SDK module."""

    _unified_no_window_proxy = True

    def __init__(self, module: ModuleType) -> None:
        self._module = module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def Popen(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802 - subprocess API
        return self._module.Popen(
            *args,
            **no_window_subprocess_kwargs(kwargs),
        )


_CODEX_PATCH_LOCK = threading.Lock()


def configure_openai_codex_no_window() -> bool:
    """Make the SDK's internal ``codex.exe app-server`` launch windowless.

    ``openai_codex.client`` imports the standard subprocess module into a
    module variable and currently does not expose Popen creation flags through
    ``CodexConfig``.  Replacing only that module variable with a delegating
    proxy avoids changing global ``subprocess.Popen`` for the rest of the app.
    The operation is idempotent and leaves non-Windows platforms untouched.
    """

    if os.name != "nt":
        return False
    with _CODEX_PATCH_LOCK:
        try:
            import openai_codex.client as codex_client
        except ImportError:
            return False
        current = codex_client.subprocess
        if getattr(current, "_unified_no_window_proxy", False):
            return True
        codex_client.subprocess = _HiddenSubprocessProxy(current)
        return True


__all__ = [
    "CREATE_NO_WINDOW",
    "configure_openai_codex_no_window",
    "no_window_subprocess_kwargs",
]
