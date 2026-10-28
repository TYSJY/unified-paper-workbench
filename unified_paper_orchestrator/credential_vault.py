"""Windows-current-user encrypted password vault.

The on-disk schema intentionally matches the established legacy automatic
submission credential store.  This lets both submission paths converge on one
safe credential model without copying plaintext passwords into scheduler state.
"""

from __future__ import annotations

import base64
import binascii
import copy
import ctypes
import os
import uuid
from ctypes import wintypes
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .io_utils import FileLock, now_iso, read_json, write_json_atomic


VAULT_VERSION = 1
ENCRYPTION_SCHEME = "win32-dpapi-current-user"
CREDENTIAL_REF_PREFIX = "vault:"
ACCOUNT_VAULT_REF_PREFIX = "account-vault:"


class VaultError(RuntimeError):
    """Safe, value-free error raised for vault operations."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _protect_bytes(raw: bytes) -> bytes:
    if os.name != "nt":
        raise VaultError("Windows DPAPI 仅可在 Windows 上使用。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = _DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise VaultError("Windows DPAPI 加密失败。")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect_bytes(raw: bytes) -> bytes:
    if os.name != "nt":
        raise VaultError("Windows DPAPI 仅可在 Windows 上使用。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = _DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise VaultError("密码无法由当前 Windows 用户解密。")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect_text(value: str) -> str:
    if not value:
        return ""
    return base64.b64encode(_protect_bytes(value.encode("utf-8"))).decode("ascii")


def unprotect_text(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        raw = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        return _unprotect_bytes(raw).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise VaultError("加密密码格式不合法。") from exc


class DpapiCredentialVault:
    """Fail-closed vault compatible with the legacy ``credential_store.json``."""

    def __init__(
        self,
        path: Path | str,
        *,
        protector: Callable[[str], str] | None = None,
        unprotector: Callable[[str], str] | None = None,
        lock_path: Path | str | None = None,
        transaction_dir: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.path.parent / "account_site_store.lock"
        )
        self.transaction_dir = (
            Path(transaction_dir)
            if transaction_dir is not None
            else self.path.parent / "vault_transactions"
        )
        self._protector = protector or protect_text
        self._unprotector = unprotector or unprotect_text

    @contextmanager
    def _mutation_transaction(self) -> Iterator[None]:
        """Serialize every standalone vault mutation with AccountSiteStore."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path, stale_seconds=5 * 60):
            if self.transaction_dir.exists() and (
                next(self.transaction_dir.glob("*.pending.json"), None) is not None
                or next(self.transaction_dir.glob("*.committed.json"), None) is not None
            ):
                raise VaultError(
                    "Account/site transaction evidence must be recovered or reconciled "
                    "before a standalone vault mutation."
                )
            yield

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": VAULT_VERSION,
            "updated_at": now_iso(),
            "encryption": ENCRYPTION_SCHEME,
            "credentials": [],
        }

    def load(self) -> dict[str, Any]:
        data = read_json(self.path, self.empty())
        if not isinstance(data, dict) or not isinstance(data.get("credentials", []), list):
            raise VaultError("密码保险库结构不合法，已停止读取和写入。")
        if any(not isinstance(item, dict) for item in data.get("credentials", [])):
            raise VaultError("密码保险库包含无法识别的记录，已停止读取和写入。")
        result = copy.deepcopy(data)
        result.setdefault("version", VAULT_VERSION)
        result.setdefault("encryption", ENCRYPTION_SCHEME)
        result.setdefault("credentials", [])
        return result

    def has(self, credential_ref: str) -> bool:
        record_id = self.record_id_from_ref(credential_ref)
        return any(str(item.get("id") or "") == record_id for item in self.load()["credentials"])

    def list_records(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.load()["credentials"])

    def get_record(self, credential_ref: str) -> dict[str, Any]:
        return self.get_record_by_id(self.record_id_from_ref(credential_ref))

    def get_record_by_id(self, record_id: str) -> dict[str, Any]:
        """Read an already-routed raw ID without interpreting it as a reference."""

        clean_id = str(record_id or "")
        for record in self.load()["credentials"]:
            if str(record.get("id") or "") == clean_id:
                return copy.deepcopy(record)
        raise VaultError("密码引用不存在。")

    def reveal(self, credential_ref: str) -> str:
        return self.reveal_by_id(self.record_id_from_ref(credential_ref))

    def reveal_by_id(self, record_id: str) -> str:
        """Reveal an already-routed raw ID without stripping a second prefix."""

        clean_id = str(record_id or "")
        for record in self.load()["credentials"]:
            if str(record.get("id") or "") == clean_id:
                ciphertext = str(record.get("password_ciphertext") or "")
                if not ciphertext:
                    return ""
                return self._unprotector(ciphertext)
        raise VaultError("密码引用不存在。")

    def protect_value(self, value: str) -> str:
        """Protect a local value with the vault's current-user protector."""

        if not value:
            return ""
        return self._protector(value)

    def unprotect_value(self, ciphertext: str) -> str:
        """Unprotect a value previously produced by :meth:`protect_value`."""

        if not ciphertext:
            return ""
        return self._unprotector(ciphertext)

    def upsert_password(self, record_id: str, password: str, *, source: str = "shared-account-center") -> str:
        clean_id = str(record_id or "").strip()
        if not clean_id:
            raise VaultError("密码引用标识不能为空。")
        if not password:
            raise VaultError("空密码不会写入密码保险库。")
        with self._mutation_transaction():
            data = self.load()
            records = data["credentials"]
            record = next(
                (item for item in records if str(item.get("id") or "") == clean_id),
                None,
            )
            now = now_iso()
            if record is None:
                record = {"id": clean_id, "created_at": now}
                records.append(record)
            record["password_ciphertext"] = self._protector(password)
            record["password_scheme"] = ENCRYPTION_SCHEME
            record["source"] = source
            record["updated_at"] = now
            data["version"] = VAULT_VERSION
            data["encryption"] = ENCRYPTION_SCHEME
            data["updated_at"] = now
            write_json_atomic(self.path, data)
        return self.ref_for_record_id(clean_id)

    def upsert(
        self,
        record: dict[str, Any],
        *,
        password: str | None = None,
        original_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one legacy-compatible encrypted account record."""
        with self._mutation_transaction():
            data, saved = self.prepare_upsert(
                record,
                password=password,
                original_id=original_id,
            )
            write_json_atomic(self.path, data)
        return saved

    def prepare_upsert(
        self,
        record: dict[str, Any],
        *,
        password: str | None = None,
        original_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build an upsert result entirely in memory without touching the vault file."""

        if not isinstance(record, dict):
            raise VaultError("账号记录格式不合法。")
        data = self.load()
        records = data["credentials"]
        wanted = str(original_id or record.get("id") or "").strip()
        existing = next((item for item in records if str(item.get("id") or "") == wanted), None) if wanted else None
        now = now_iso()
        if existing is None:
            record_id = str(record.get("id") or "").strip() or uuid.uuid4().hex
            if any(str(item.get("id") or "") == record_id for item in records):
                raise VaultError("账号标识已存在。")
            existing = {"id": record_id, "created_at": now}
            records.append(existing)
        for key in (
            "site_name",
            "site_url",
            "username",
            "email",
            "purpose",
            "related_journals",
            "login_status",
            "notes",
            "source",
        ):
            if key in record:
                existing[key] = str(record.get(key) or "").strip()
        if password:
            existing["password_ciphertext"] = self._protector(password)
            existing["password_scheme"] = ENCRYPTION_SCHEME
        existing.setdefault("password_ciphertext", "")
        existing.setdefault("password_scheme", ENCRYPTION_SCHEME)
        existing["updated_at"] = now
        data["version"] = VAULT_VERSION
        data["encryption"] = ENCRYPTION_SCHEME
        data["updated_at"] = now
        return data, copy.deepcopy(existing)

    def delete(self, credential_ref: str) -> bool:
        with self._mutation_transaction():
            data, deleted = self.prepare_delete(credential_ref)
            if deleted:
                write_json_atomic(self.path, data)
        return deleted

    def prepare_delete(self, credential_ref: str) -> tuple[dict[str, Any], bool]:
        return self.prepare_delete_by_id(self.record_id_from_ref(credential_ref))

    def delete_by_id(self, record_id: str) -> bool:
        with self._mutation_transaction():
            data, deleted = self.prepare_delete_by_id(record_id)
            if deleted:
                write_json_atomic(self.path, data)
        return deleted

    def prepare_delete_by_id(self, record_id: str) -> tuple[dict[str, Any], bool]:
        """Build a raw-ID deletion entirely in memory without touching the file."""

        clean_id = str(record_id or "")
        data = self.load()
        records = data["credentials"]
        remaining = [item for item in records if str(item.get("id") or "") != clean_id]
        if len(remaining) == len(records):
            return data, False
        data["credentials"] = remaining
        data["updated_at"] = now_iso()
        return data, True

    @staticmethod
    def ref_for_record_id(record_id: str) -> str:
        return f"{CREDENTIAL_REF_PREFIX}{str(record_id).strip()}"

    @staticmethod
    def record_id_from_ref(credential_ref: str) -> str:
        value = str(credential_ref or "").strip()
        if value.startswith(CREDENTIAL_REF_PREFIX):
            value = value[len(CREDENTIAL_REF_PREFIX) :]
        if not value:
            raise VaultError("密码引用为空。")
        return value
