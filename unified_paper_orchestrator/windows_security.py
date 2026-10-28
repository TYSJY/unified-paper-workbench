from __future__ import annotations

import csv
import base64
import ctypes
import io
import json
import os
import subprocess
from pathlib import Path


class PrivateAclError(RuntimeError):
    """Value-free failure while applying a local private ACL."""


def _trusted_windows_paths() -> tuple[Path, Path, Path, Path, Path]:
    if os.name != "nt":
        raise PrivateAclError("Windows 权限工具只能在 Windows 上调用。")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        copied = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError):
        copied = 0
    if copied <= 0 or copied >= len(buffer):
        raise PrivateAclError("无法定位可信 Windows 权限工具。")
    windows_dir = Path(buffer.value).resolve()
    system32 = windows_dir / "System32"
    powershell_home = system32 / "WindowsPowerShell" / "v1.0"
    whoami = system32 / "whoami.exe"
    powershell = powershell_home / "powershell.exe"
    if not whoami.is_file() or not powershell.is_file():
        raise PrivateAclError("可信 Windows 权限工具不可用。")
    return windows_dir, system32, powershell_home, whoami, powershell


def _minimal_windows_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    windows_dir, system32, powershell_home, _whoami, _powershell = _trusted_windows_paths()
    env = {
        "SYSTEMROOT": str(windows_dir),
        "WINDIR": str(windows_dir),
        "COMSPEC": str(system32 / "cmd.exe"),
        "PATH": os.pathsep.join((str(system32), str(powershell_home))),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PSMODULEPATH": str(powershell_home / "Modules"),
    }
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _run_hidden(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=creationflags,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PrivateAclError("无法调用 Windows 权限工具，已停止敏感文件写入。") from None


def current_user_sid() -> str:
    if os.name != "nt":
        return ""
    _windows_dir, _system32, _powershell_home, whoami, _powershell = _trusted_windows_paths()
    result = _run_hidden(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        env=_minimal_windows_environment(),
    )
    if result.returncode != 0:
        raise PrivateAclError("无法确认当前 Windows 用户，已停止敏感文件写入。")
    try:
        row = next(csv.reader(io.StringIO(result.stdout.strip())))
    except (csv.Error, StopIteration):
        raise PrivateAclError("无法确认当前 Windows 用户，已停止敏感文件写入。") from None
    sid = row[1].strip() if len(row) >= 2 else ""
    if not sid.startswith("S-1-"):
        raise PrivateAclError("当前 Windows 用户标识不合法，已停止敏感文件写入。")
    return sid


def restrict_private_path(path: Path | str) -> None:
    """Restrict a file/directory to current user, SYSTEM, and Administrators.

    This intentionally removes inherited and explicit broad grants.  The DACL
    is assembled in memory and applied with one ``Set-Acl`` call, so a failed
    preparation cannot leave the target reset to a broader inherited ACL.
    """

    if os.name != "nt":
        return
    target = Path(path)
    if not target.exists():
        raise PrivateAclError("敏感文件或目录尚不存在，无法设置最小权限。")
    sid = current_user_sid()
    _apply_or_verify_private_acl(target, sid, apply=True)


def _verify_private_acl(path: Path, allowed_sids: set[str]) -> None:
    current_sid = next(
        (sid for sid in allowed_sids if sid not in {"S-1-5-18", "S-1-5-32-544"}),
        "",
    )
    if not current_sid:
        raise PrivateAclError("Windows 最小权限复核缺少当前用户。")
    _apply_or_verify_private_acl(path, current_sid, apply=False)


def _apply_or_verify_private_acl(path: Path, current_sid: str, *, apply: bool) -> None:
    script = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$target = $env:UPO_PRIVATE_ACL_TARGET
$userSid = New-Object System.Security.Principal.SecurityIdentifier($env:UPO_PRIVATE_ACL_USER_SID)
$systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$adminsSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
$isDirectory = Test-Path -LiteralPath $target -PathType Container
$acl = Get-Acl -LiteralPath $target
if ($env:UPO_PRIVATE_ACL_APPLY -eq '1') {
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    $inheritance = if ($isDirectory) {
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
        [System.Security.AccessControl.InheritanceFlags]::None
    }
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    foreach ($sid in @($userSid, $systemSid, $adminsSid)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            $propagation,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $target -AclObject $acl -ErrorAction Stop
}
$acl = Get-Acl -LiteralPath $target
$ownerSid = $acl.Owner
try {
    $ownerSid = (New-Object System.Security.Principal.NTAccount($acl.Owner)).Translate([System.Security.Principal.SecurityIdentifier]).Value
} catch {
    if (-not $ownerSid.StartsWith('S-1-')) { throw }
}
$rows = @($acl.Access | ForEach-Object {
    $sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    [pscustomobject]@{
        sid = $sid
        type = $_.AccessControlType.ToString()
        inherited = [bool]$_.IsInherited
        rights = $_.FileSystemRights.ToString()
        inheritance = $_.InheritanceFlags.ToString()
        propagation = $_.PropagationFlags.ToString()
    }
})
[pscustomobject]@{
    owner = $ownerSid
    is_directory = [bool]$isDirectory
    rules = $rows
} | ConvertTo-Json -Depth 5 -Compress
""".strip()
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    _windows_dir, _system32, _powershell_home, _whoami, powershell = _trusted_windows_paths()
    env = _minimal_windows_environment(
        {
            "UPO_PRIVATE_ACL_TARGET": str(path),
            "UPO_PRIVATE_ACL_USER_SID": current_sid,
            "UPO_PRIVATE_ACL_APPLY": "1" if apply else "0",
        }
    )
    result = _run_hidden(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        env=env,
    )
    if result.returncode != 0:
        raise PrivateAclError("Windows 最小权限设置或复核失败，已停止敏感文件写入。")
    try:
        parsed = json.loads(result.stdout.strip() or "{}")
    except (TypeError, ValueError):
        raise PrivateAclError("Windows 最小权限复核失败，已停止敏感文件写入。") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list):
        raise PrivateAclError("Windows 最小权限复核失败，已停止敏感文件写入。")
    allowed_sids = {current_sid, "S-1-5-18", "S-1-5-32-544"}
    if str(parsed.get("owner") or "") not in allowed_sids:
        raise PrivateAclError("敏感文件所有者不在最小权限允许范围内。")
    is_directory = bool(parsed.get("is_directory"))
    rows = parsed["rules"]
    if len(rows) != len(allowed_sids):
        raise PrivateAclError("敏感文件权限项数量不符合最小权限要求。")
    observed: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PrivateAclError("Windows 最小权限复核失败，已停止敏感文件写入。")
        sid = str(row.get("sid") or "")
        inheritance = {
            item.strip()
            for item in str(row.get("inheritance") or "").split(",")
            if item.strip()
        }
        expected_inheritance = {"ContainerInherit", "ObjectInherit"} if is_directory else {"None"}
        if (
            row.get("type") != "Allow"
            or row.get("inherited") is not False
            or row.get("rights") != "FullControl"
            or inheritance != expected_inheritance
            or row.get("propagation") != "None"
            or sid not in allowed_sids
        ):
            raise PrivateAclError("敏感文件仍含未授权权限项，已停止写入。")
        observed.add(sid)
    if observed != allowed_sids:
        raise PrivateAclError("敏感文件最小权限项不完整，已停止写入。")
