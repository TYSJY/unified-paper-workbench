from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import threading
import time
from datetime import datetime
from ctypes import wintypes
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import BACKUP_DIR, LOCK_PATH, LOCK_STALE_SECONDS


_PROCESS_FILE_LOCKS_GUARD = threading.Lock()
_PROCESS_FILE_LOCKS: dict[str, Any] = {}


def _process_file_lock(path: Path) -> Any:
    key = os.path.normcase(os.path.abspath(path))
    with _PROCESS_FILE_LOCKS_GUARD:
        return _PROCESS_FILE_LOCKS.setdefault(key, threading.RLock())


class JsonReadError(RuntimeError):
    """Raised when an existing JSON file cannot be read safely."""

    def __init__(self, path: Path, reason: Exception) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Cannot read JSON file {path}: {reason}")


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def timestamp_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def command_id(prefix: str = "cmd") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # An existing but unreadable state file is materially different from a
        # missing optional file.  Returning a default here lets callers write a
        # fresh empty state over the only recoverable copy, so fail closed.
        raise JsonReadError(path, exc) from exc


def read_json_snapshot(path: Path, default: Any | None = None) -> tuple[Any, str | None]:
    """Read JSON together with the SHA-256 of the exact decoded source bytes.

    The digest is used only as a pre-replace conflict detector.  It does not
    turn a filesystem replace into a cross-process transactional CAS.
    """

    if not path.exists():
        return ({} if default is None else default), None
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JsonReadError(path, exc) from exc
    return data, hashlib.sha256(raw).hexdigest()


def _fsync_parent_directory(path: Path) -> None:
    """Best-effort directory fsync after an atomic replace.

    Windows does not consistently allow directories to be opened for fsync,
    while POSIX does.  The payload file itself is always fsynced before the
    replace; this helper adds the directory durability guarantee where the
    platform supports it.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(path.parent), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace_atomic(tmp: Path, path: Path) -> None:
    last_error: PermissionError | None = None
    for attempt in range(30):
        try:
            os.replace(tmp, path)
            _fsync_parent_directory(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(min(0.1 + attempt * 0.05, 1.0))
    if last_error is not None:
        raise last_error


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_atomic(tmp, path)
    finally:
        # Covers serialization, flush/fsync, and replace failures.  A failed
        # write must never leave an unbounded collection of state-sized temps.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically replace one file with exact bytes and fsync the payload."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_atomic(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_bytes_atomic_if_unchanged(
    path: Path,
    data: bytes,
    expected_sha256: str | None,
) -> bool:
    """Atomically replace exact bytes only while the current digest matches."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            current_sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        except OSError:
            return False
        if current_sha256 != expected_sha256:
            return False
        _replace_atomic(tmp, path)
        return True
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def unlink_file_durable_if_unchanged(path: Path, expected_sha256: str) -> bool:
    """Unlink one exact file and fsync its parent directory where supported.

    Callers that need a true cross-process CAS must still hold their shared
    writer lock.  The digest check prevents a cooperative recovery path from
    deleting transaction evidence it does not recognize.
    """

    try:
        current = path.read_bytes()
    except (FileNotFoundError, OSError):
        return False
    if hashlib.sha256(current).hexdigest() != expected_sha256:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_parent_directory(path)
    return True


def write_json_atomic_if_unchanged(
    path: Path,
    data: Any,
    expected_sha256: str | None,
) -> bool:
    """Atomically replace JSON only if a last-moment byte digest still matches.

    The payload is serialized and fsynced before the current file is hashed, so
    a detected intervening write leaves ``path`` untouched and the caller can
    re-read and merge.  There remains an unavoidable narrow race between the
    digest check and ``os.replace`` when another process does not participate
    in a shared lock; callers must therefore also require an externally
    quiescent writer (the writing reconciler requires paused automations).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            current_sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        except OSError:
            return False
        if current_sha256 != expected_sha256:
            return False
        _replace_atomic(tmp, path)
        return True
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text.rstrip() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_atomic(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def rel_to(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def append_unique(items: list[str], item: str) -> list[str]:
    if item and item not in items:
        items.append(item)
    return items


def append_history(record: dict[str, Any], event: str, message: str) -> None:
    history = record.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        record["history"] = history
    history.append({"at": now_iso(), "event": event, "message": message})


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def backup_file(path: Path, label: str = "") -> str:
    if not path.exists():
        return ""
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in (label or path.stem))
    backup_root = BACKUP_DIR / timestamp_id()
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / f"{safe_label}_{path.name}"
    counter = 1
    while target.exists():
        target = backup_root / f"{safe_label}_{counter}_{path.name}"
        counter += 1
    shutil.copy2(path, target)
    return str(target)


def copy_directory_contents(source_dir: Path, target_dir: Path) -> list[str]:
    copied: list[str] = []
    if not source_dir.exists() or not source_dir.is_dir():
        return copied
    target_dir.mkdir(parents=True, exist_ok=True)
    ignored_names = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}

    def copy_one(source: Path, target: Path) -> None:
        if source.name in ignored_names:
            return
        try:
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                target.mkdir(parents=True, exist_ok=True)
                for child in source.iterdir():
                    copy_one(child, target / child.name)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            copied.append(str(target))
        except (PermissionError, OSError):
            # Source submit directories can contain locked browser/git cache
            # files; those are not submission artifacts and should not block a
            # retarget/import sweep.
            return

    for source in source_dir.iterdir():
        target = target_dir / source.name
        copy_one(source, target)
    return copied


def markdown_escape(value: Any) -> str:
    text = str(value or "")
    return text.replace("|", "\\|").replace("\n", " ")


class FileLock:
    def __init__(self, path: Path = LOCK_PATH, stale_seconds: int = LOCK_STALE_SECONDS) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False
        self.owner_token = ""
        self._thread_lock: Any | None = None

    @staticmethod
    def _parse_identity(raw: str) -> tuple[int | None, str]:
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if separator and key and key not in fields:
                fields[key] = value.strip()
        try:
            pid = int(fields.get("pid", ""))
        except (TypeError, ValueError):
            pid = None
        return pid, fields.get("owner_token", "")

    @staticmethod
    def _pid_is_alive(pid: int | None) -> bool | None:
        """Return True/False for known liveness, or None when access is uncertain."""

        if pid is None or pid <= 0:
            return None
        if pid == os.getpid():
            return True
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
            error_invalid_parameter = 87
            error_access_denied = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            handle = open_process(process_query_limited_information, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error == error_invalid_parameter:
                    return False
                if error == error_access_denied:
                    return True
                return None
            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == still_active
            finally:
                close_handle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    def _current_identity(self) -> tuple[int | None, str, str]:
        raw = read_text(self.path)
        pid, owner_token = self._parse_identity(raw)
        return pid, owner_token, raw

    def __enter__(self) -> "FileLock":
        self._thread_lock = _process_file_lock(self.path)
        self._thread_lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._remove_stale_lock()
            owner_token = uuid4().hex
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                detail = read_text(self.path).strip()
                raise RuntimeError(f"统一调度锁已存在：{self.path}\n{detail}") from exc
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"version=2\npid={os.getpid()}\nowner_token={owner_token}\ncreated_at={now_iso()}\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                try:
                    _pid, current_token, _raw = self._current_identity()
                    if current_token == owner_token:
                        self.path.unlink(missing_ok=True)
                finally:
                    self.owner_token = ""
                raise
            self.owner_token = owner_token
            self.acquired = True
            return self
        except BaseException:
            self._release_thread_lock()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self.acquired:
                try:
                    for attempt in range(30):
                        pid, owner_token, _raw = self._current_identity()
                        if pid != os.getpid() or owner_token != self.owner_token:
                            break
                        try:
                            self.path.unlink(missing_ok=True)
                        except PermissionError:
                            if attempt + 1 >= 30:
                                raise
                            time.sleep(min(0.01 + attempt * 0.005, 0.1))
                        else:
                            break
                finally:
                    self.acquired = False
                    self.owner_token = ""
        finally:
            self._release_thread_lock()

    def _release_thread_lock(self) -> None:
        thread_lock = self._thread_lock
        self._thread_lock = None
        if thread_lock is not None:
            thread_lock.release()

    def _remove_stale_lock(self) -> None:
        if not self.path.exists():
            return
        try:
            original_raw = self.path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            return
        pid, _owner_token = self._parse_identity(original_raw)
        # A positively dead PID is a conclusive orphan signal, even when the
        # lock file is fresh.  Live and indeterminate owners remain fail-closed
        # regardless of mtime, and the byte comparison below prevents deleting
        # a replacement owner that raced with this liveness check.
        if self._pid_is_alive(pid) is not False:
            return
        try:
            if self.path.read_text(encoding="utf-8-sig", errors="ignore") != original_raw:
                return
            self.path.unlink(missing_ok=True)
        except OSError:
            return
