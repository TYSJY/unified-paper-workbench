from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .account_semantics import (
    ACCOUNT_SCOPE_ALIASES,
    canonical_account_scope,
    has_explicit_live_browser_session,
)
from .browser_session import BrowserSessionEvidenceSigner, normalize_origin
from .config import (
    ACCOUNT_VAULT_PATH,
    CREDENTIAL_VAULT_PATH,
    LEGACY_CREDENTIAL_VAULT_PATH,
    SMART_SUBMISSION_STATE_PATH,
    UNIFIED_STATE_PATH,
)
from .credential_resolver import SmartCredentialResolver
from .credential_vault import (
    ACCOUNT_VAULT_REF_PREFIX,
    CREDENTIAL_REF_PREFIX,
    DpapiCredentialVault,
)
from .io_utils import FileLock, now_iso, read_json, write_bytes_atomic, write_json_atomic
from .submission_site_adapters import submission_site_adapter
from .windows_security import restrict_private_path


ACCOUNT_SCOPES = tuple(ACCOUNT_SCOPE_ALIASES)
AUDIT_EVENT_LIMIT = 500
VAULT_TRANSACTION_SCHEMA_VERSION = 4
LEGACY_DPAPI_VAULT_PATH = LEGACY_CREDENTIAL_VAULT_PATH


class RegistryValidationError(ValueError):
    """Raised when registry data cannot be edited without risking data loss."""


class DuplicateRecordError(RegistryValidationError):
    """Raised when a stable record key or identity is already registered."""


class RecordNotFoundError(RegistryValidationError):
    """Raised when an edited or deleted record no longer exists."""


class PostCommitDriftError(RegistryValidationError):
    """Raised when an immutable committed marker no longer matches disk state."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    return _text(value).casefold()


def _normalized_identifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _text(value)).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


def _canonical_scope(value: Any) -> str:
    return canonical_account_scope(value)


def _same_path(left: Path | str, right: Path | str) -> bool:
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except OSError:
        return str(left).casefold() == str(right).casefold()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    return []


def _required(record: dict[str, Any], field: str, label: str) -> str:
    value = _text(record.get(field))
    if not value:
        raise RegistryValidationError(f"{label}不能为空。")
    return value


class AccountSiteStore:
    """Fail-closed storage facade for local accounts and journal site metadata.

    The store deliberately returns password-free summaries for list views.  A
    full account record is only returned by :meth:`get_account` for an explicit
    edit operation.  Existing unknown fields are preserved so older migration
    evidence remains intact after a manual edit.
    """

    def __init__(
        self,
        credential_path: Path | str,
        site_registry_path: Path | str,
        *,
        vault_path: Path | str | None = None,
        backup_dir: Path | str | None = None,
        audit_path: Path | str | None = None,
        legacy_vault_path: Path | str = LEGACY_DPAPI_VAULT_PATH,
        runtime_vault_path: Path | str = CREDENTIAL_VAULT_PATH,
        smart_state_path: Path | str = SMART_SUBMISSION_STATE_PATH,
        unified_state_path: Path | str = UNIFIED_STATE_PATH,
        lock_path: Path | str | None = None,
        acl_hardener: Callable[[Path | str], None] | None = None,
        vault_backend: DpapiCredentialVault | None = None,
        legacy_vault_backend: DpapiCredentialVault | None = None,
    ) -> None:
        self.credential_path = Path(credential_path)
        self.site_registry_path = Path(site_registry_path)
        common_parent = self.credential_path.parent
        default_vault = ACCOUNT_VAULT_PATH
        self.vault_path = Path(vault_path) if vault_path is not None else default_vault
        self.legacy_vault_path = Path(legacy_vault_path)
        self.runtime_vault_path = Path(runtime_vault_path)
        self.smart_state_path = Path(smart_state_path)
        self.unified_state_path = Path(unified_state_path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else common_parent / "account_site_store.lock"
        )
        self.vault_transaction_dir = common_parent / "vault_transactions"
        if _same_path(self.vault_path, self.legacy_vault_path):
            raise RegistryValidationError(
                "新账号库不能与旧稳定投稿账号库使用同一路径；已停止初始化。"
            )
        if _same_path(self.vault_path, self.runtime_vault_path):
            raise RegistryValidationError(
                "新账号库不能与智能投稿运行凭据库使用同一路径；已停止初始化。"
            )
        if vault_backend is not None and not _same_path(vault_backend.path, self.vault_path):
            raise RegistryValidationError("新账号库后端路径与配置路径不一致；已停止初始化。")
        if vault_backend is not None and not _same_path(
            vault_backend.transaction_dir,
            self.vault_transaction_dir,
        ):
            raise RegistryValidationError(
                "Injected account vault backend does not use the configured transaction directory."
            )
        if legacy_vault_backend is not None and not _same_path(
            legacy_vault_backend.path,
            self.legacy_vault_path,
        ):
            raise RegistryValidationError("旧稳定投稿账号库后端路径与配置路径不一致；已停止初始化。")
        for backend, label in (
            (vault_backend, "account vault"),
            (legacy_vault_backend, "legacy account vault"),
        ):
            if backend is not None and not _same_path(backend.lock_path, self.lock_path):
                raise RegistryValidationError(
                    f"Injected {label} backend does not use the shared account/site lock."
                )
        self.vault = vault_backend or DpapiCredentialVault(
            self.vault_path,
            lock_path=self.lock_path,
            transaction_dir=self.vault_transaction_dir,
        )
        self.legacy_vault = legacy_vault_backend or DpapiCredentialVault(
            self.legacy_vault_path,
            lock_path=self.lock_path,
        )
        self.vault_read_only = False
        self.backup_dir = Path(backup_dir) if backup_dir is not None else common_parent / "backups" / "account_site_manager"
        self.audit_path = Path(audit_path) if audit_path is not None else common_parent / "account_site_audit.json"
        self._acl_hardener = acl_hardener or restrict_private_path
        self._lock = threading.RLock()
        if self.vault_transaction_dir.exists():
            self.recover_vault_transactions()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize all account/site writes across UI and scheduler processes."""

        with self._lock:
            self.credential_path.parent.mkdir(parents=True, exist_ok=True)
            self._acl_hardener(self.credential_path.parent)
            with FileLock(self.lock_path, stale_seconds=5 * 60):
                self._recover_vault_transactions_locked(compact_committed=True)
                yield

    @staticmethod
    def _file_state(path: Path) -> tuple[bool, str]:
        if not path.exists():
            return False, ""
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RegistryValidationError("账号库事务文件无法安全读取。") from exc
        return True, hashlib.sha256(raw).hexdigest()

    def _require_file_state(
        self,
        path: Path,
        *,
        expected_exists: bool,
        expected_sha256: str,
        label: str,
    ) -> None:
        current_exists, current_sha256 = self._file_state(path)
        if current_exists != expected_exists or current_sha256 != expected_sha256:
            raise RegistryValidationError(f"{label} changed concurrently; transaction stopped.")

    @staticmethod
    def _valid_digest(value: Any) -> bool:
        text = _text(value).casefold()
        return len(text) == 64 and all(char in "0123456789abcdef" for char in text)

    @classmethod
    def _valid_state_digest(cls, value: Any, *, exists: bool) -> bool:
        return cls._valid_digest(value) if exists else not _text(value)

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except (OSError, ValueError):
            return False
        return True

    def _pending_transaction_path(self, operation_id: str) -> Path:
        return self.vault_transaction_dir / f"{operation_id}.pending.json"

    def _committed_transaction_path(self, operation_id: str) -> Path:
        return self.vault_transaction_dir / f"{operation_id}.committed.json"

    def _reconciled_transaction_path(self, operation_id: str) -> Path:
        return self.vault_transaction_dir / f"{operation_id}.reconciled.json"

    def _post_image_path(self, operation_id: str) -> Path:
        return self.vault_transaction_dir / f"{operation_id}.post.wal.json"

    def _audit_pre_image_path(self, operation_id: str) -> Path:
        return self.vault_transaction_dir / f"{operation_id}.audit-pre.wal.json"

    @staticmethod
    def _json_bytes(data: Any) -> bytes:
        return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def _is_managed_target(self, path: Path) -> bool:
        return any(
            _same_path(path, candidate)
            for candidate in (self.vault_path, self.credential_path, self.site_registry_path)
        )

    def _write_transaction_post_image(self, path: Path, target: Path, raw: bytes) -> None:
        encoded = base64.b64encode(raw).decode("ascii")
        envelope = {
            "version": 1,
            "encryption": "win32-dpapi-current-user",
            "created_at": now_iso(),
            "source_name": target.name,
            "source_bytes": len(raw),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "payload_ciphertext": self.vault.protect_value(encoded),
        }
        self.vault_transaction_dir.mkdir(parents=True, exist_ok=True)
        self._acl_hardener(self.vault_transaction_dir)
        write_json_atomic(path, envelope)
        self._acl_hardener(path)
        encoded = ""

    def _read_transaction_post_image(self, path: Path, expected_sha256: str) -> bytes:
        data = read_json(path, {})
        required = {
            "version",
            "encryption",
            "created_at",
            "source_name",
            "source_bytes",
            "source_sha256",
            "payload_ciphertext",
        }
        if (
            not isinstance(data, dict)
            or set(data) != required
            or data.get("version") != 1
            or data.get("encryption") != "win32-dpapi-current-user"
            or not _text(data.get("payload_ciphertext"))
        ):
            raise RegistryValidationError("账号与站点事务 post WAL 缺少完整加密封装。")
        try:
            raw = base64.b64decode(
                self.vault.unprotect_value(_text(data.get("payload_ciphertext"))).encode("ascii"),
                validate=True,
            )
            source_bytes = int(data.get("source_bytes"))
        except Exception:
            raise RegistryValidationError("账号与站点事务 post WAL 无法安全读取。") from None
        source_sha256 = _text(data.get("source_sha256")).casefold()
        if (
            source_bytes < 0
            or source_bytes > 100 * 1024 * 1024
            or len(raw) != source_bytes
            or not self._valid_digest(source_sha256)
            or hashlib.sha256(raw).hexdigest() != source_sha256
            or source_sha256 != expected_sha256.casefold()
        ):
            raise RegistryValidationError("账号与站点事务 post WAL 完整性校验失败。")
        return raw

    def _write_vault_pending(self, path: Path, journal: dict[str, Any]) -> None:
        self.vault_transaction_dir.mkdir(parents=True, exist_ok=True)
        self._acl_hardener(self.vault_transaction_dir)
        write_json_atomic(path, journal)
        self._acl_hardener(path)

    @staticmethod
    def _remove_vault_pending(path: Path) -> bool:
        try:
            path.unlink(missing_ok=True)
            return not path.exists()
        except OSError:
            # A committed journal is safe to leave for deterministic recovery.
            return False

    def _remove_transaction_artifacts(
        self,
        pending_path: Path,
        post_image_path: Path,
        audit_pre_image_path: Path | None = None,
    ) -> None:
        if not self._remove_vault_pending(pending_path):
            return
        for artifact_path in (post_image_path, audit_pre_image_path):
            if artifact_path is None:
                continue
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError:
                # An encrypted orphan WAL is harmless and is cleaned on recovery.
                pass

    def _validated_vault_journal(
        self,
        path: Path,
        *,
        marker_kind: str = "pending",
    ) -> dict[str, Any]:
        data = read_json(path, {})
        required = {
            "schema_version",
            "operation_id",
            "stage",
            "created_at",
            "target_path",
            "target_pre_exists",
            "target_pre_sha256",
            "target_post_exists",
            "target_post_sha256",
            "backup_path",
            "post_image_path",
            "audit_path",
            "audit_pre_exists",
            "audit_pre_sha256",
            "audit_pre_image_path",
            "audit_post_exists",
            "audit_post_sha256",
            "audit_intent",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise RegistryValidationError("账号库 pending 事务日志结构不完整，已停止恢复。")
        operation_id = _text(data.get("operation_id"))
        if (
            data.get("schema_version") != VAULT_TRANSACTION_SCHEMA_VERSION
            or len(operation_id) != 32
            or operation_id != operation_id.casefold()
            or any(char not in "0123456789abcdef" for char in operation_id)
            or data.get("stage") not in {"prepared", "target_written", "audit_written"}
            or type(data.get("target_pre_exists")) is not bool
            or type(data.get("target_post_exists")) is not bool
            or type(data.get("audit_pre_exists")) is not bool
            or type(data.get("audit_post_exists")) is not bool
        ):
            raise RegistryValidationError("账号库 pending 事务日志元数据不合法，已停止恢复。")
        expected_marker_paths = {
            "pending": self._pending_transaction_path(operation_id),
            "committed": self._committed_transaction_path(operation_id),
        }
        expected_marker_path = expected_marker_paths.get(marker_kind)
        if expected_marker_path is None or not _same_path(path, expected_marker_path):
            raise RegistryValidationError("账号库 pending 事务日志文件名与操作标识不匹配，已停止恢复。")
        if marker_kind == "committed" and data.get("stage") != "audit_written":
            raise RegistryValidationError(
                "Committed account transaction marker is not at the audit-written stage."
            )
        if not _text(data.get("created_at")):
            raise RegistryValidationError("账号库 pending 事务日志缺少创建时间，已停止恢复。")
        target_path = Path(str(data.get("target_path") or ""))
        if not self._is_managed_target(target_path):
            raise RegistryValidationError("账号库 pending 事务目标路径不匹配，已停止恢复。")
        if not _same_path(Path(str(data.get("audit_path") or "")), self.audit_path):
            raise RegistryValidationError("账号库 pending 事务审计路径不匹配，已停止恢复。")
        pre_exists = bool(data["target_pre_exists"])
        post_exists = bool(data["target_post_exists"])
        audit_pre_exists = bool(data["audit_pre_exists"])
        audit_post_exists = bool(data["audit_post_exists"])
        if not self._valid_state_digest(data.get("target_pre_sha256"), exists=pre_exists):
            raise RegistryValidationError("账号库 pending 事务前置哈希不合法。")
        if not post_exists or not self._valid_state_digest(
            data.get("target_post_sha256"),
            exists=True,
        ):
            raise RegistryValidationError("账号库 pending 事务后置哈希不合法。")
        if not self._valid_state_digest(data.get("audit_pre_sha256"), exists=audit_pre_exists):
            raise RegistryValidationError("账号库 pending 事务审计哈希不合法。")
        if not audit_post_exists or not self._valid_state_digest(
            data.get("audit_post_sha256"),
            exists=True,
        ):
            raise RegistryValidationError("Account transaction audit post-state digest is invalid.")
        backup_text = _text(data.get("backup_path"))
        if pre_exists:
            if not backup_text or not self._path_is_within(Path(backup_text), self.backup_dir):
                raise RegistryValidationError("账号库事务缺少受信任备份。")
        elif backup_text:
            raise RegistryValidationError("新建账号事务不应声明前置备份。")
        post_image_text = _text(data.get("post_image_path"))
        expected_post_path = self._post_image_path(operation_id)
        if not post_image_text or not _same_path(Path(post_image_text), expected_post_path):
            raise RegistryValidationError("账号库 pending 事务 post WAL 路径不匹配。")
        if not self._path_is_within(Path(post_image_text), self.vault_transaction_dir):
            raise RegistryValidationError("账号库 pending 事务 post WAL 路径越界。")
        audit_pre_image_text = _text(data.get("audit_pre_image_path"))
        expected_audit_pre_path = self._audit_pre_image_path(operation_id)
        if audit_pre_exists:
            if (
                not audit_pre_image_text
                or not _same_path(Path(audit_pre_image_text), expected_audit_pre_path)
                or not self._path_is_within(
                    Path(audit_pre_image_text),
                    self.vault_transaction_dir,
                )
            ):
                raise RegistryValidationError(
                    "Account transaction audit pre-image WAL path is invalid."
                )
        elif audit_pre_image_text:
            raise RegistryValidationError(
                "A transaction without an audit pre-image must not declare an audit WAL."
            )
        intent = data.get("audit_intent")
        if not isinstance(intent, dict) or set(intent) != {
            "action",
            "record_type",
            "record_id",
            "target_post_sha256",
        }:
            raise RegistryValidationError("账号库 pending 事务审计意图不完整。")
        if not all(
            _text(intent.get(key))
            for key in ("action", "record_type", "record_id", "target_post_sha256")
        ):
            raise RegistryValidationError("账号库 pending 事务审计意图为空。")
        action = _text(intent.get("action"))
        record_type = _text(intent.get("record_type"))
        record_id = _text(intent.get("record_id"))
        if (
            re.fullmatch(r"[a-z0-9_-]{1,64}", action) is None
            or re.fullmatch(r"[a-z0-9_-]{1,64}", record_type) is None
            or re.fullmatch(r"[0-9a-f]{24}", record_id) is None
            or _text(intent.get("target_post_sha256")).casefold()
            != _text(data.get("target_post_sha256")).casefold()
        ):
            raise RegistryValidationError("账号库 pending 事务审计意图不合法，已停止恢复。")
        return data

    @staticmethod
    def _audit_contains_operation(
        audit: dict[str, Any],
        operation_id: str,
        intent: dict[str, Any],
    ) -> bool:
        matches = [
            event
            for event in audit.get("events", [])
            if isinstance(event, dict) and _text(event.get("operation_id")) == operation_id
        ]
        if not matches:
            return False
        if len(matches) != 1 or any(
            _text(matches[0].get(key)) != _text(intent.get(key))
            for key in ("action", "record_type", "record_id", "target_post_sha256")
        ):
            raise RegistryValidationError("账号库事务审计提交证据不一致，已停止恢复。")
        return True

    def _restore_transaction_target_locked(self, journal: dict[str, Any]) -> None:
        target_path = Path(str(journal.get("target_path") or ""))
        if not self._is_managed_target(target_path):
            raise RegistryValidationError("账号与站点事务目标路径不受信任，已停止恢复。")
        pre_exists = bool(journal["target_pre_exists"])
        pre_sha256 = _text(journal.get("target_pre_sha256")).casefold()
        if pre_exists:
            backup_path = Path(str(journal.get("backup_path") or ""))
            if not self._path_is_within(backup_path, self.backup_dir):
                raise RegistryValidationError("账号库事务备份路径越界，已停止恢复。")
            raw = self.read_backup_bytes(backup_path)
            if hashlib.sha256(raw).hexdigest() != pre_sha256:
                raise RegistryValidationError("账号库事务备份与前置哈希不一致，已停止恢复。")
            write_bytes_atomic(target_path, raw)
            self._acl_hardener(target_path)
        elif target_path.exists():
            try:
                target_path.unlink()
            except OSError as exc:
                raise RegistryValidationError("新建账号事务回滚失败，已保留 pending 日志。") from exc
        restored_exists, restored_sha256 = self._file_state(target_path)
        if restored_exists != pre_exists or restored_sha256 != pre_sha256:
            raise RegistryValidationError("账号库事务回滚校验失败，已保留 pending 日志。")

    def _audit_pre_raw_from_journal(self, journal: dict[str, Any]) -> bytes:
        if not bool(journal["audit_pre_exists"]):
            return b""
        audit_pre_image_path = Path(str(journal.get("audit_pre_image_path") or ""))
        return self._read_transaction_post_image(
            audit_pre_image_path,
            _text(journal.get("audit_pre_sha256")).casefold(),
        )

    def _restore_audit_from_journal_locked(
        self,
        journal: dict[str, Any],
        audit_pre_raw: bytes,
    ) -> None:
        self._restore_audit_snapshot_locked(
            bool(journal["audit_pre_exists"]),
            audit_pre_raw,
            expected_post_sha256=_text(journal.get("audit_post_sha256")).casefold(),
        )

    def _cleanup_orphan_transaction_images_locked(
        self,
        *,
        exclude_operation_id: str = "",
    ) -> None:
        orphan_patterns = (
            ("*.post.wal.json", ".post.wal.json"),
            ("*.audit-pre.wal.json", ".audit-pre.wal.json"),
        )
        for pattern, suffix in orphan_patterns:
            for image_path in sorted(self.vault_transaction_dir.glob(pattern)):
                operation_id = image_path.name.removesuffix(suffix)
                if operation_id == exclude_operation_id:
                    continue
                if (
                    self._pending_transaction_path(operation_id).exists()
                    or self._committed_transaction_path(operation_id).exists()
                ):
                    continue
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prepare_transaction_commit_locked(self, journal: dict[str, Any]) -> None:
        """Clean only unrelated orphans, then verify the intended commit state."""

        operation_id = _text(journal.get("operation_id"))
        self._cleanup_orphan_transaction_images_locked(
            exclude_operation_id=operation_id,
        )
        self._require_file_state(
            Path(str(journal.get("target_path") or "")),
            expected_exists=bool(journal["target_post_exists"]),
            expected_sha256=_text(journal.get("target_post_sha256")).casefold(),
            label="Managed target before commit linearization",
        )
        self._require_file_state(
            self.audit_path,
            expected_exists=bool(journal["audit_post_exists"]),
            expected_sha256=_text(journal.get("audit_post_sha256")).casefold(),
            label="Account audit before commit linearization",
        )

    def _commit_vault_pending_locked(
        self,
        pending_path: Path,
        committed_path: Path,
        journal: dict[str, Any],
    ) -> None:
        """Atomically rename pending to the immutable transaction commit marker."""

        operation_id = _text(journal.get("operation_id"))
        if (
            not _same_path(pending_path, self._pending_transaction_path(operation_id))
            or not _same_path(committed_path, self._committed_transaction_path(operation_id))
            or committed_path.exists()
            or self._reconciled_transaction_path(operation_id).exists()
        ):
            raise RegistryValidationError(
                "Account transaction commit marker path or lifecycle is invalid."
            )
        self._require_file_state(
            pending_path,
            expected_exists=True,
            expected_sha256=hashlib.sha256(self._json_bytes(journal)).hexdigest(),
            label="Pending transaction marker",
        )
        self._require_file_state(
            Path(str(journal.get("target_path") or "")),
            expected_exists=bool(journal["target_post_exists"]),
            expected_sha256=_text(journal.get("target_post_sha256")).casefold(),
            label="Managed target at commit linearization",
        )
        self._require_file_state(
            self.audit_path,
            expected_exists=bool(journal["audit_post_exists"]),
            expected_sha256=_text(journal.get("audit_post_sha256")).casefold(),
            label="Account audit at commit linearization",
        )
        os.replace(pending_path, committed_path)
        self._verify_committed_state_locked(committed_path, journal)

    def _verify_committed_state_locked(
        self,
        committed_path: Path,
        journal: dict[str, Any],
    ) -> None:
        """Immediately bind the marker linearization point to target and audit."""

        try:
            self._require_file_state(
                committed_path,
                expected_exists=True,
                expected_sha256=hashlib.sha256(self._json_bytes(journal)).hexdigest(),
                label="Committed transaction marker",
            )
            self._require_file_state(
                Path(str(journal.get("target_path") or "")),
                expected_exists=bool(journal["target_post_exists"]),
                expected_sha256=_text(journal.get("target_post_sha256")).casefold(),
                label="Committed transaction target",
            )
            self._require_file_state(
                self.audit_path,
                expected_exists=bool(journal["audit_post_exists"]),
                expected_sha256=_text(journal.get("audit_post_sha256")).casefold(),
                label="Committed transaction audit",
            )
        except RegistryValidationError:
            raise PostCommitDriftError(
                "POST_COMMIT_DRIFT: target or audit changed at the atomic marker boundary; "
                "the committed marker and WAL were preserved."
            ) from None

    def _reconcile_committed_marker_locked(
        self,
        committed_path: Path,
        journal: dict[str, Any],
    ) -> None:
        """Archive a verified prior commit before a later locked mutation."""

        operation_id = _text(journal.get("operation_id"))
        reconciled_path = self._reconciled_transaction_path(operation_id)
        if reconciled_path.exists():
            raise RegistryValidationError(
                "Account transaction already has a reconciled marker; compaction stopped."
            )
        self._require_file_state(
            committed_path,
            expected_exists=True,
            expected_sha256=hashlib.sha256(self._json_bytes(journal)).hexdigest(),
            label="Committed transaction marker",
        )
        self._require_file_state(
            Path(str(journal.get("target_path") or "")),
            expected_exists=bool(journal["target_post_exists"]),
            expected_sha256=_text(journal.get("target_post_sha256")).casefold(),
            label="Committed target before reconciliation",
        )
        self._require_file_state(
            self.audit_path,
            expected_exists=bool(journal["audit_post_exists"]),
            expected_sha256=_text(journal.get("audit_post_sha256")).casefold(),
            label="Committed audit before reconciliation",
        )
        os.replace(committed_path, reconciled_path)
        post_image_path = Path(str(journal.get("post_image_path") or ""))
        audit_pre_image_path = (
            Path(str(journal.get("audit_pre_image_path") or ""))
            if bool(journal["audit_pre_exists"])
            else None
        )
        for artifact_path in (post_image_path, audit_pre_image_path):
            if artifact_path is None:
                continue
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _recover_vault_transactions_locked(
        self,
        *,
        compact_committed: bool = False,
    ) -> list[str]:
        if not self.vault_transaction_dir.exists():
            return []
        self._acl_hardener(self.vault_transaction_dir)
        recovered: list[str] = []
        for committed_path in sorted(self.vault_transaction_dir.glob("*.committed.json")):
            self._acl_hardener(committed_path)
            journal = self._validated_vault_journal(
                committed_path,
                marker_kind="committed",
            )
            operation_id = _text(journal.get("operation_id"))
            target_path = Path(str(journal.get("target_path") or ""))
            post_image_path = Path(str(journal.get("post_image_path") or ""))
            self._read_transaction_post_image(
                post_image_path,
                _text(journal.get("target_post_sha256")).casefold(),
            )
            self._audit_pre_raw_from_journal(journal)
            current_exists, current_sha256 = self._file_state(target_path)
            target_matches_commit = (
                current_exists == bool(journal["target_post_exists"])
                and current_sha256 == _text(journal.get("target_post_sha256")).casefold()
            )
            audit_exists, audit_sha256 = self._file_state(self.audit_path)
            audit_matches_commit = (
                audit_exists == bool(journal["audit_post_exists"])
                and audit_sha256 == _text(journal.get("audit_post_sha256")).casefold()
            )
            if not target_matches_commit or not audit_matches_commit:
                raise PostCommitDriftError(
                    "POST_COMMIT_DRIFT: immutable account transaction marker no longer "
                    "matches target or audit state; marker and disk state were preserved."
                )
            audit = self._read_audit()
            if not self._audit_contains_operation(
                audit,
                operation_id,
                journal["audit_intent"],
            ):
                raise PostCommitDriftError(
                    "POST_COMMIT_DRIFT: committed audit event is missing; evidence was preserved."
                )
            if compact_committed:
                self._reconcile_committed_marker_locked(committed_path, journal)
            recovered.append(operation_id)

        for pending_path in sorted(self.vault_transaction_dir.glob("*.pending.json")):
            self._acl_hardener(pending_path)
            journal = self._validated_vault_journal(pending_path)
            operation_id = _text(journal.get("operation_id"))
            target_path = Path(str(journal.get("target_path") or ""))
            post_image_path = Path(str(journal.get("post_image_path") or ""))
            audit_pre_image_path = (
                Path(str(journal.get("audit_pre_image_path") or ""))
                if bool(journal["audit_pre_exists"])
                else None
            )
            pre_exists = bool(journal["target_pre_exists"])
            pre_sha256 = _text(journal.get("target_pre_sha256")).casefold()
            post_exists = bool(journal["target_post_exists"])
            post_sha256 = _text(journal.get("target_post_sha256")).casefold()
            self._read_transaction_post_image(post_image_path, post_sha256)
            audit_pre_raw = self._audit_pre_raw_from_journal(journal)
            current_exists, current_sha256 = self._file_state(target_path)
            audit_exists, audit_sha256 = self._file_state(self.audit_path)
            audit_is_pre = (
                audit_exists == bool(journal["audit_pre_exists"])
                and audit_sha256 == _text(journal.get("audit_pre_sha256")).casefold()
            )
            audit_is_post = (
                audit_exists == bool(journal["audit_post_exists"])
                and audit_sha256 == _text(journal.get("audit_post_sha256")).casefold()
            )
            if not audit_is_pre and not audit_is_post:
                raise RegistryValidationError(
                    "Account transaction audit has an unknown concurrent state; recovery stopped."
                )
            audit = self._read_audit()
            contains_operation = self._audit_contains_operation(
                audit,
                operation_id,
                journal["audit_intent"],
            )
            if audit_is_post != contains_operation:
                raise RegistryValidationError(
                    "Account transaction audit commit marker does not match its bound post-state."
                )
            target_is_pre = current_exists == pre_exists and current_sha256 == pre_sha256
            target_is_post = current_exists == post_exists and current_sha256 == post_sha256
            if not target_is_pre and not target_is_post:
                if audit_is_post:
                    self._restore_audit_from_journal_locked(journal, audit_pre_raw)
                raise RegistryValidationError(
                    "Uncommitted account transaction target has an unknown concurrent state; "
                    "target and pending evidence were preserved."
                )
            if audit_is_post and journal["stage"] == "prepared":
                raise RegistryValidationError(
                    "Prepared account transaction cannot contain an audit post-state."
                )
            if audit_is_post:
                self._restore_audit_from_journal_locked(journal, audit_pre_raw)
            if target_is_post:
                self._restore_transaction_target_locked(journal)
            self._remove_transaction_artifacts(
                pending_path,
                post_image_path,
                audit_pre_image_path,
            )
            recovered.append(operation_id)
        self._cleanup_orphan_transaction_images_locked()
        return recovered

    def recover_vault_transactions(self) -> list[str]:
        """Recover pending work and validate committed markers under the shared lock."""

        if not self.vault_transaction_dir.exists():
            return []
        with self._lock:
            self.credential_path.parent.mkdir(parents=True, exist_ok=True)
            self._acl_hardener(self.credential_path.parent)
            with FileLock(self.lock_path, stale_seconds=5 * 60):
                return self._recover_vault_transactions_locked(compact_committed=False)

    def _prepare_private_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._acl_hardener(path.parent)
        if path.exists():
            self._acl_hardener(path)

    def _require_writable_vault(self) -> None:
        if self.vault_read_only:
            raise RegistryValidationError(
                "新账号库与旧稳定投稿库路径冲突；为保护旧库，新增和改写已安全停用。"
            )

    def is_account_read_only(self, record_key: str) -> bool:
        source, _record_id = self._split_record_key(record_key)
        return source == "legacy" or (source == "vault" and self.vault_read_only)

    def _legacy_vault_backend(self) -> DpapiCredentialVault:
        """Return the immutable legacy backend without duplicating a same-path fixture."""

        return self.vault if self.vault_read_only else self.legacy_vault

    @staticmethod
    def _default_credentials() -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": now_iso(),
            "warning": "Local-only credential registry. Never expose passwords in logs or chat.",
            "accounts": [],
        }

    @staticmethod
    def _default_sites() -> dict[str, Any]:
        return {"version": 1, "updated_at": now_iso(), "systems": [], "journals": []}

    def _load_credentials(self) -> dict[str, Any]:
        data = read_json(self.credential_path, self._default_credentials())
        if not isinstance(data, dict) or not isinstance(data.get("accounts", []), list):
            raise RegistryValidationError("账号库结构不合法，已停止读取和写入。")
        if any(not isinstance(item, dict) for item in data.get("accounts", [])):
            raise RegistryValidationError("账号库包含无法识别的记录，已停止读取和写入。")
        result = copy.deepcopy(data)
        result.setdefault("accounts", [])
        return result

    def _load_sites(self) -> dict[str, Any]:
        data = read_json(self.site_registry_path, self._default_sites())
        if not isinstance(data, dict):
            raise RegistryValidationError("站点库结构不合法，已停止读取和写入。")
        for key, label in (("systems", "投稿系统"), ("journals", "期刊记录")):
            value = data.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise RegistryValidationError(f"{label}结构不合法，已停止读取和写入。")
        result = copy.deepcopy(data)
        result.setdefault("systems", [])
        result.setdefault("journals", [])
        return result

    _PROTECTED_SMART_STATUSES = {
        "queued",
        "running",
        "in_flight",
        "in-flight",
        "inflight",
        "in_progress",
        "in-progress",
        "in progress",
        "processing",
        "dispatching",
        "uploading",
        "submitting",
        "ready_for_online_submit",
        "waiting",
        "waiting_user_approval",
        "monitoring",
        "submitted",
        "accepted",
    }

    @staticmethod
    def _optional_state_object(path: Path, label: str) -> dict[str, Any]:
        data = read_json(path, {})
        if not isinstance(data, dict):
            raise RegistryValidationError(f"{label}结构不合法，已停止账号与站点改写。")
        return data

    def _protected_smart_papers(self) -> list[dict[str, Any]]:
        smart = self._optional_state_object(self.smart_state_path, "智能投稿状态")
        unified = self._optional_state_object(self.unified_state_path, "统一投稿路线状态")
        papers = smart.get("papers", {})
        if not isinstance(papers, dict):
            raise RegistryValidationError("智能投稿论文引用结构不合法，已停止账号与站点改写。")
        for key in ("paper_queue", "online_queue"):
            if key in smart and not isinstance(smart.get(key), list):
                raise RegistryValidationError("智能投稿队列引用结构不合法，已停止账号与站点改写。")
        queued_ids = {
            _normalized_identifier(item)
            for key in ("paper_queue", "online_queue")
            for item in (smart.get(key, []) if isinstance(smart.get(key), list) else [])
            if _text(item)
        }
        route_ids: set[str] = set()
        for source in (smart, unified):
            routes = source.get("submission_routes", {})
            if not isinstance(routes, dict):
                raise RegistryValidationError("统一投稿路线引用结构不合法，已停止账号与站点改写。")
            route_ids.update(_normalized_identifier(item) for item in routes if _text(item))

        protected: list[dict[str, Any]] = []
        for paper_id, raw_paper in papers.items():
            if not isinstance(raw_paper, dict):
                raise RegistryValidationError("智能投稿论文引用结构不合法，已停止账号与站点改写。")
            clean_paper_id = _normalized_identifier(paper_id)
            statuses = {
                _normalized(raw_paper.get(field))
                for field in ("status", "workflow_status", "worker_status")
                if _text(raw_paper.get(field))
            }
            has_submission_evidence = any(
                _text(raw_paper.get(field))
                for field in ("submission_id", "manuscript_id", "confirmation_url")
            )
            if (
                clean_paper_id in queued_ids
                or clean_paper_id in route_ids
                or bool(statuses & self._PROTECTED_SMART_STATUSES)
                or has_submission_evidence
            ):
                protected.append(copy.deepcopy(raw_paper))
        return protected

    def _account_has_protected_reference(self, account_id: str) -> bool:
        wanted = _normalized_identifier(account_id)
        for paper in self._protected_smart_papers():
            reference = _normalized_identifier(
                paper.get("credential_account_id") or paper.get("account_id")
            )
            forms = {reference}
            for prefix in ("vault:", "legacy:", "smart:"):
                if reference.startswith(prefix):
                    forms.add(reference[len(prefix) :])
            if wanted in forms:
                return True
        return False

    def _system_has_protected_reference(self, system_id: str) -> bool:
        wanted = _normalized_identifier(system_id)
        return any(
            _normalized_identifier(
                paper.get("submission_system_id") or paper.get("system_id")
            )
            == wanted
            for paper in self._protected_smart_papers()
        )

    def _journal_has_protected_reference(self, journal: dict[str, Any]) -> bool:
        wanted_key = _normalized_identifier(journal.get("journal_key"))
        wanted_name = _normalized_identifier(journal.get("journal_name"))
        for paper in self._protected_smart_papers():
            paper_key = _normalized_identifier(
                paper.get("journal_site_key") or paper.get("journal_key")
            )
            paper_name = _normalized_identifier(
                paper.get("target_journal") or paper.get("journal_name") or paper.get("journal")
            )
            if (wanted_key and paper_key == wanted_key) or (wanted_name and paper_name == wanted_name):
                return True
        return False

    @staticmethod
    def _verified_browser_origins_from_data(
        credentials: dict[str, Any],
        sites: dict[str, Any],
        *,
        account_id: str,
        system_id: str,
    ) -> tuple[str, ...]:
        wanted_account = _normalized(account_id)
        wanted_system = _normalized(system_id)
        adapter = submission_site_adapter(wanted_system)
        account = next(
            (
                item
                for item in credentials.get("accounts", [])
                if isinstance(item, dict) and _normalized(item.get("account_id")) == wanted_account
            ),
            None,
        )
        if account is None:
            raise RecordNotFoundError("Smart account record does not exist.")
        if canonical_account_scope(account.get("scope")) != "journal_system":
            raise RegistryValidationError("Only a Smart journal-system account can receive browser-session evidence.")
        system = next(
            (
                item
                for item in sites.get("systems", [])
                if isinstance(item, dict) and _normalized(item.get("system_id")) == wanted_system
            ),
            None,
        )
        if system is None or not (
            _normalized(system.get("status")) == "verified"
            or (
                adapter is not None
                and _normalized(system.get("status")) == "discovered"
                and adapter.trusts_registry_record(system)
            )
        ):
            raise RegistryValidationError("The target submission system is not explicitly verified.")

        system_origins = {
            origin
            for origin in (
                normalize_origin(system.get("login_url")),
                normalize_origin(system.get("submission_url")),
            )
            if origin
        }
        if adapter is not None:
            system_origins.update(adapter.allowed_origins)
        account_system_id = _normalized(account.get("system_id"))
        account_login_origin = normalize_origin(account.get("login_url"))
        if account_system_id:
            if account_system_id != wanted_system:
                raise RegistryValidationError("The account is bound to a different submission system.")
        elif not account_login_origin or account_login_origin not in system_origins:
            raise RegistryValidationError(
                "The account login origin is not bound to the requested verified submission system."
            )

        verified_origins = set(system_origins)
        for journal in sites.get("journals", []):
            if not isinstance(journal, dict):
                continue
            if _normalized(journal.get("system_id")) != wanted_system:
                continue
            if not (
                _normalized(journal.get("status")) == "verified"
                or (
                    adapter is not None
                    and _normalized(journal.get("status")) == "discovered"
                    and adapter.trusts_registry_record(journal)
                )
            ):
                continue
            origin = normalize_origin(journal.get("submission_url"))
            if origin:
                verified_origins.add(origin)
        if not verified_origins:
            raise RegistryValidationError("The verified submission-system record has no valid HTTP(S) origin.")
        return tuple(sorted(verified_origins))

    def verified_browser_origins(self, account_id: str, system_id: str) -> tuple[str, ...]:
        """Return only origins explicitly bound to one Smart account/system pair."""

        return self._verified_browser_origins_from_data(
            self._load_credentials(),
            self._load_sites(),
            account_id=account_id,
            system_id=system_id,
        )

    def record_browser_session_evidence(
        self,
        account_id: str,
        system_id: str,
        expected_origin: str,
        evidence: dict[str, Any],
        *,
        signer: BrowserSessionEvidenceSigner,
    ) -> dict[str, Any]:
        """Audit and persist one signed, value-free, exact-origin probe receipt."""

        clean_account_id = _text(account_id)
        clean_system_id = _text(system_id)
        origin = normalize_origin(expected_origin)
        if not clean_account_id or not clean_system_id or not origin:
            raise RegistryValidationError("Account, submission system, and exact target origin are required.")
        if not isinstance(evidence, dict) or not signer.verify(evidence):
            raise RegistryValidationError("Browser-session evidence signature or value-free shape is invalid.")

        with self._transaction():
            credentials = self._load_credentials()
            sites = self._load_sites()
            verified_origins = self._verified_browser_origins_from_data(
                credentials,
                sites,
                account_id=clean_account_id,
                system_id=clean_system_id,
            )
            if origin not in verified_origins:
                raise RegistryValidationError("The probed origin is not verified for this account and system.")
            wanted = _normalized(clean_account_id)
            account = next(
                (
                    item
                    for item in credentials["accounts"]
                    if _normalized(item.get("account_id")) == wanted
                ),
                None,
            )
            if account is None:
                raise RecordNotFoundError("Smart account record does not exist.")
            if not has_explicit_live_browser_session(
                {**account, "browser_session_evidence": evidence},
                expected_origins=(origin,),
                evidence_verifier=signer.verify,
            ):
                raise RegistryValidationError("Browser-session evidence is stale or does not match the account/origin.")
            account["browser_session_evidence"] = copy.deepcopy(evidence)
            account["browser_session_evidence_system_id"] = clean_system_id
            account["last_verified_at"] = str(evidence.get("checked_at") or "")
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="record-browser-session-evidence",
                record_type="account",
                record_id=clean_account_id,
            )
            return {
                "account_id": clean_account_id,
                "system_id": clean_system_id,
                "origin": origin,
                "evidence_id": str(evidence.get("evidence_id") or ""),
                "checked_at": str(evidence.get("checked_at") or ""),
                "expires_at": str(evidence.get("expires_at") or ""),
                "probe_method": str(evidence.get("probe_method") or ""),
            }

    def _backup_before_write(self, path: Path, record_type: str, action: str) -> Path | None:
        if not path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._acl_hardener(self.backup_dir)
        target_dir = self.backup_dir / stamp
        target_dir.mkdir(parents=True, exist_ok=False)
        self._acl_hardener(target_dir)
        backup_label = hashlib.sha256(
            f"{record_type}\x1f{action}".encode("utf-8")
        ).hexdigest()[:12]
        target = target_dir / f"backup-{backup_label}_{path.name}"
        sensitive_registry = (
            _same_path(path, self.credential_path)
            or _same_path(path, self.vault_path)
            or any(marker in path.name.casefold() for marker in ("credential", "password", "vault"))
        )
        if sensitive_registry:
            raw = path.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            envelope = {
                "version": 1,
                "encryption": "win32-dpapi-current-user",
                "created_at": now_iso(),
                "source_name": path.name,
                "source_bytes": len(raw),
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "payload_ciphertext": self.vault.protect_value(encoded),
            }
            write_json_atomic(target, envelope)
            encoded = ""
            raw = b""
        else:
            shutil.copy2(path, target)
        self._acl_hardener(target)
        return target

    def read_backup_bytes(self, backup_path: Path | str) -> bytes:
        """Read a manager backup and verify an encrypted envelope when used."""

        path = Path(backup_path)
        data = read_json(path, {})
        sensitive_backup = any(
            marker in path.name.casefold()
            for marker in ("credential", "password", "vault", "account-smart", "account-vault")
        )
        if not sensitive_backup:
            return path.read_bytes()
        required_keys = {
            "version",
            "encryption",
            "created_at",
            "source_name",
            "source_bytes",
            "source_sha256",
            "payload_ciphertext",
        }
        if (
            not isinstance(data, dict)
            or set(data) != required_keys
            or data.get("version") != 1
            or data.get("encryption") != "win32-dpapi-current-user"
            or not _text(data.get("payload_ciphertext"))
        ):
            raise RegistryValidationError("账号备份缺少完整的 DPAPI 加密封装。")
        ciphertext = _text(data.get("payload_ciphertext"))
        try:
            raw = base64.b64decode(self.vault.unprotect_value(ciphertext).encode("ascii"), validate=True)
        except Exception:
            raise RegistryValidationError("账号备份无法由当前 Windows 用户安全读取。") from None
        try:
            source_bytes = int(data.get("source_bytes"))
        except (TypeError, ValueError):
            raise RegistryValidationError("账号备份长度元数据不合法。") from None
        if source_bytes < 0 or source_bytes > 100 * 1024 * 1024 or len(raw) != source_bytes:
            raise RegistryValidationError("账号备份长度校验失败。")
        source_sha256 = _text(data.get("source_sha256")).casefold()
        if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
            raise RegistryValidationError("账号备份哈希元数据不合法。")
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise RegistryValidationError("账号备份哈希校验失败。")
        return raw

    @staticmethod
    def _validated_audit_data(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict) or not isinstance(data.get("events", []), list):
            raise RegistryValidationError("账号与站点审计文件结构不合法，已停止写入。")
        result = copy.deepcopy(data)
        result.setdefault("version", 1)
        result.setdefault("events", [])
        return result

    def _read_audit_snapshot(self) -> tuple[dict[str, Any], bool, bytes]:
        existed = self.audit_path.exists()
        if not existed:
            return {"version": 1, "events": []}, False, b""
        try:
            raw = self.audit_path.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RegistryValidationError(
                "Account and site audit file cannot be read safely; writes are disabled."
            ) from None
        return self._validated_audit_data(data), True, raw

    def _read_audit(self) -> dict[str, Any]:
        audit, _existed, _raw = self._read_audit_snapshot()
        return audit

    @staticmethod
    def _audit_payload(
        audit: dict[str, Any],
        *,
        action: str,
        record_type: str,
        record_id: str,
        backup: Path | None,
        operation_id: str = "",
        event_at: str = "",
        target_post_sha256: str = "",
    ) -> dict[str, Any]:
        result = copy.deepcopy(audit)
        events = result.setdefault("events", [])
        event = {
            "at": event_at or now_iso(),
            "action": action,
            "record_type": record_type,
            "record_id": record_id,
            "backup_path": str(backup) if backup is not None else "",
        }
        if operation_id:
            event["operation_id"] = operation_id
        if target_post_sha256:
            event["target_post_sha256"] = target_post_sha256
        events.append(event)
        result["events"] = events[-AUDIT_EVENT_LIMIT:]
        return result

    def _append_audit(
        self,
        audit: dict[str, Any],
        *,
        action: str,
        record_type: str,
        record_id: str,
        backup: Path | None,
        operation_id: str = "",
        event_at: str = "",
        target_post_sha256: str = "",
    ) -> None:
        payload = self._audit_payload(
            audit,
            action=action,
            record_type=record_type,
            record_id=record_id,
            backup=backup,
            operation_id=operation_id,
            event_at=event_at,
            target_post_sha256=target_post_sha256,
        )
        write_json_atomic(self.audit_path, payload)
        self._acl_hardener(self.audit_path)

    def _restore_audit_snapshot_locked(
        self,
        existed: bool,
        raw: bytes,
        *,
        expected_post_sha256: str,
    ) -> None:
        pre_sha256 = hashlib.sha256(raw).hexdigest() if existed else ""
        current_exists, current_sha256 = self._file_state(self.audit_path)
        if current_exists == existed and current_sha256 == pre_sha256:
            return
        if not current_exists or current_sha256 != expected_post_sha256:
            raise RegistryValidationError(
                "Audit changed outside this transaction; automatic rollback was stopped."
            )
        if existed:
            write_bytes_atomic(self.audit_path, raw)
            self._acl_hardener(self.audit_path)
        elif self.audit_path.exists():
            try:
                self.audit_path.unlink()
            except OSError as exc:
                raise RegistryValidationError("账号库事务审计回滚失败。") from exc
        restored_exists, restored_sha256 = self._file_state(self.audit_path)
        if restored_exists != existed or restored_sha256 != pre_sha256:
            raise RegistryValidationError(
                "Audit rollback verification failed; pending transaction was preserved."
            )

    def _run_audited_file_replace_locked(
        self,
        *,
        target_path: Path,
        post_bytes: bytes,
        action: str,
        record_type: str,
        record_id: str,
    ) -> None:
        """Atomically replace one managed file and bind it to an audit event.

        The complete encrypted post image and its digest are durable before the
        target replace starts.  Therefore even ``prepared/current=post`` after
        an abrupt process exit is distinguishable from a third-party write.
        """

        pending_path: Path | None = None
        committed_path: Path | None = None
        post_image_path: Path | None = None
        audit_pre_image_path: Path | None = None
        audit_pre_exists = False
        audit_pre_raw = b""
        audit_pre_sha256 = ""
        audit_post_sha256 = ""
        preparation_step = "validate"
        try:
            if not self._is_managed_target(target_path):
                raise RegistryValidationError("账号与站点事务目标文件不受信任。")
            if not isinstance(post_bytes, bytes) or len(post_bytes) > 100 * 1024 * 1024:
                raise RegistryValidationError("账号与站点事务 post 镜像不合法。")
            preparation_step = "read_audit"
            audit, audit_pre_exists, audit_pre_raw = self._read_audit_snapshot()
            audit_pre_sha256 = hashlib.sha256(audit_pre_raw).hexdigest() if audit_pre_exists else ""
            preparation_step = "snapshot_target"
            target_pre_exists, target_pre_sha256 = self._file_state(target_path)
            target_post_sha256 = hashlib.sha256(post_bytes).hexdigest()
            preparation_step = "backup_target"
            backup = self._backup_before_write(target_path, record_type, action)
            if target_pre_exists:
                if backup is None:
                    raise RegistryValidationError("账号与站点事务缺少前置备份。")
                backup_raw = self.read_backup_bytes(backup)
                if hashlib.sha256(backup_raw).hexdigest() != target_pre_sha256:
                    raise RegistryValidationError("账号与站点事务前置备份哈希不匹配。")
                backup_raw = b""
            operation_id = uuid4().hex
            pending_path = self._pending_transaction_path(operation_id)
            committed_path = self._committed_transaction_path(operation_id)
            post_image_path = self._post_image_path(operation_id)
            event_at = now_iso()
            audit_post = self._audit_payload(
                audit,
                action=action,
                record_type=record_type,
                record_id=record_id,
                backup=backup,
                operation_id=operation_id,
                event_at=event_at,
                target_post_sha256=target_post_sha256,
            )
            audit_post_sha256 = hashlib.sha256(self._json_bytes(audit_post)).hexdigest()
            preparation_step = "write_post_wal"
            self._write_transaction_post_image(post_image_path, target_path, post_bytes)
            if audit_pre_exists:
                audit_pre_image_path = self._audit_pre_image_path(operation_id)
                preparation_step = "write_audit_pre_wal"
                self._write_transaction_post_image(
                    audit_pre_image_path,
                    self.audit_path,
                    audit_pre_raw,
                )
            journal: dict[str, Any] = {
                "schema_version": VAULT_TRANSACTION_SCHEMA_VERSION,
                "operation_id": operation_id,
                "stage": "prepared",
                "created_at": now_iso(),
                "target_path": str(target_path.resolve(strict=False)),
                "target_pre_exists": target_pre_exists,
                "target_pre_sha256": target_pre_sha256,
                "target_post_exists": True,
                "target_post_sha256": target_post_sha256,
                "backup_path": str(backup.resolve(strict=False)) if backup is not None else "",
                "post_image_path": str(post_image_path.resolve(strict=False)),
                "audit_path": str(self.audit_path.resolve(strict=False)),
                "audit_pre_exists": audit_pre_exists,
                "audit_pre_sha256": audit_pre_sha256,
                "audit_pre_image_path": (
                    str(audit_pre_image_path.resolve(strict=False))
                    if audit_pre_image_path is not None
                    else ""
                ),
                "audit_post_exists": True,
                "audit_post_sha256": audit_post_sha256,
                "audit_intent": {
                    "action": action,
                    "record_type": record_type,
                    "record_id": record_id,
                    "target_post_sha256": target_post_sha256,
                },
            }
            preparation_step = "write_pending"
            self._write_vault_pending(pending_path, journal)
        except Exception:
            if pending_path is not None and post_image_path is not None:
                self._remove_transaction_artifacts(
                    pending_path,
                    post_image_path,
                    audit_pre_image_path,
                )
            else:
                for artifact_path in (post_image_path, audit_pre_image_path):
                    if artifact_path is None:
                        continue
                    try:
                        artifact_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise RegistryValidationError(
                f"账号库事务准备失败（{preparation_step}）；未执行账号库写入。"
            ) from None
        concurrent_change_before_replace = False
        unknown_change_after_replace = False
        target_post_was_verified = False
        commit_phase_started = False
        try:
            try:
                self._require_file_state(
                    target_path,
                    expected_exists=target_pre_exists,
                    expected_sha256=target_pre_sha256,
                    label="Managed target",
                )
                self._require_file_state(
                    self.audit_path,
                    expected_exists=audit_pre_exists,
                    expected_sha256=audit_pre_sha256,
                    label="Account audit",
                )
            except RegistryValidationError:
                concurrent_change_before_replace = True
                raise
            write_bytes_atomic(target_path, post_bytes)
            self._acl_hardener(target_path)
            target_post_exists, actual_post_sha256 = self._file_state(target_path)
            if not target_post_exists or actual_post_sha256 != target_post_sha256:
                unknown_change_after_replace = not (
                    target_post_exists == target_pre_exists
                    and actual_post_sha256 == target_pre_sha256
                )
                raise RegistryValidationError("账号与站点事务目标后置哈希不匹配。")
            target_post_was_verified = True
            journal["stage"] = "target_written"
            self._write_vault_pending(pending_path, journal)
            self._require_file_state(
                target_path,
                expected_exists=True,
                expected_sha256=target_post_sha256,
                label="Managed target before audit commit",
            )
            self._require_file_state(
                self.audit_path,
                expected_exists=audit_pre_exists,
                expected_sha256=audit_pre_sha256,
                label="Account audit before commit",
            )
            self._append_audit(
                copy.deepcopy(audit),
                action=action,
                record_type=record_type,
                record_id=record_id,
                backup=backup,
                operation_id=operation_id,
                event_at=event_at,
                target_post_sha256=target_post_sha256,
            )
            self._require_file_state(
                self.audit_path,
                expected_exists=True,
                expected_sha256=audit_post_sha256,
                label="Account audit post-state",
            )
            self._require_file_state(
                target_path,
                expected_exists=True,
                expected_sha256=target_post_sha256,
                label="Managed target after audit commit",
            )
            journal["stage"] = "audit_written"
            self._write_vault_pending(pending_path, journal)
            commit_phase_started = True
            self._prepare_transaction_commit_locked(journal)
            self._commit_vault_pending_locked(
                pending_path,
                committed_path,
                journal,
            )
        except Exception as transaction_error:
            if (
                committed_path is not None
                and committed_path.exists()
                and pending_path is not None
                and not pending_path.exists()
            ):
                # The atomic marker rename is the only linearization point.  An
                # exception observed after it cannot retroactively uncommit.
                self._verify_committed_state_locked(committed_path, journal)
                if isinstance(transaction_error, PostCommitDriftError):
                    raise transaction_error
                return
            if concurrent_change_before_replace:
                self._remove_transaction_artifacts(
                    pending_path,
                    post_image_path,
                    audit_pre_image_path,
                )
                raise RegistryValidationError(
                    "账号与站点目标文件在写入前发生并发变化；未覆盖该变化。"
                ) from None
            current_exists, current_sha256 = self._file_state(target_path)
            current_is_pre = (
                current_exists == target_pre_exists
                and current_sha256 == target_pre_sha256
            )
            current_is_post = current_exists and current_sha256 == target_post_sha256
            audit_exists, audit_sha256 = self._file_state(self.audit_path)
            audit_is_pre = audit_exists == audit_pre_exists and audit_sha256 == audit_pre_sha256
            audit_is_post = audit_exists and audit_sha256 == audit_post_sha256
            target_changed_after_verified_post = target_post_was_verified and not current_is_post
            if (
                unknown_change_after_replace
                or target_changed_after_verified_post
                or not (current_is_pre or current_is_post)
            ):
                if audit_is_post:
                    try:
                        self._restore_audit_snapshot_locked(
                            audit_pre_exists,
                            audit_pre_raw,
                            expected_post_sha256=audit_post_sha256,
                        )
                    except Exception:
                        pass
                raise RegistryValidationError(
                    "账号与站点目标文件发生未知并发变化；pending 日志已保留并停止自动覆盖。"
                ) from None
            if not (audit_is_pre or audit_is_post):
                raise RegistryValidationError(
                    "Account audit changed concurrently; target and pending transaction were preserved."
                ) from None
            try:
                self._restore_audit_snapshot_locked(
                    audit_pre_exists,
                    audit_pre_raw,
                    expected_post_sha256=audit_post_sha256,
                )
                if current_is_post:
                    self._restore_transaction_target_locked(journal)
                if not commit_phase_started:
                    self._remove_transaction_artifacts(
                        pending_path,
                        post_image_path,
                        audit_pre_image_path,
                    )
            except Exception:
                raise RegistryValidationError(
                    "账号库事务失败且自动回滚未完成；pending 日志已保留，必须先恢复。"
                ) from None
            if commit_phase_started:
                raise RegistryValidationError(
                    "Account transaction did not reach the atomic commit marker; "
                    "target and audit were rolled back and pending evidence was preserved."
                ) from None
            raise RegistryValidationError("账号库写入失败；目标文件与审计已自动回滚。") from None

    def _write_with_audit(
        self,
        path: Path,
        data: dict[str, Any],
        *,
        action: str,
        record_type: str,
        record_id: str,
    ) -> None:
        data["updated_at"] = now_iso()
        self._run_audited_file_replace_locked(
            target_path=path,
            post_bytes=self._json_bytes(data),
            action=action,
            record_type=record_type,
            record_id=self._record_reference_digest(record_type, record_id),
        )

    @staticmethod
    def _infer_vault_scope(record: dict[str, Any]) -> str:
        combined = " ".join(
            _normalized(record.get(field)) for field in ("site_name", "site_url", "purpose")
        )
        if any(token in combined for token in ("gmail", "outlook", "mail", "邮箱", "邮件")):
            return "email"
        if any(token in combined for token in ("github", "overleaf", "bark", "orcid")):
            return "external"
        return "journal_system"

    @staticmethod
    def _smart_account_identity(record: dict[str, Any]) -> tuple[str, str, str] | None:
        identity = _normalized(record.get("username") or record.get("email"))
        if not identity:
            return None
        provider = _normalized(record.get("provider") or record.get("login_url"))
        return (_normalized(record.get("scope")), provider, identity)

    @staticmethod
    def _vault_account_identity(record: dict[str, Any]) -> tuple[str, str] | None:
        identity = _normalized(record.get("username") or record.get("email"))
        if not identity:
            return None
        return (_normalized(record.get("site_url")), identity)

    @staticmethod
    def _stable_record_id(prefix: str, *values: Any) -> str:
        payload = "\x1f".join(_normalized(value) for value in values)
        if not payload.strip("\x1f"):
            payload = uuid4().hex
        return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _record_reference_digest(source: str, record_id: str) -> str:
        """Return a stable, value-free reference suitable for local audit logs."""

        payload = f"{_text(source).casefold()}\x1f{_text(record_id)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def list_account_summaries(
        self,
        query: str = "",
        scope: str = "all",
        source: str = "all",
    ) -> list[dict[str, Any]]:
        """List writable, legacy-read-only, and Smart accounts without secrets."""
        smart_accounts = self._load_credentials()["accounts"]
        vault_accounts = self.vault.list_records()
        legacy_accounts = self._legacy_vault_backend().list_records()
        if self.vault_read_only:
            vault_accounts = []
        account_vault_ids = {str(item.get("id") or "") for item in vault_accounts}
        legacy_ids = {str(item.get("id") or "") for item in legacy_accounts}
        needle = _normalized(query)
        wanted_scope = _normalized(scope)
        wanted_source = _normalized(source)
        summaries: list[dict[str, Any]] = []

        for account in vault_accounts:
            account_id = _text(account.get("id"))
            inferred_scope = self._infer_vault_scope(account)
            summary = {
                "record_key": f"vault:{account_id}",
                "account_id": account_id,
                "scope": inferred_scope,
                "provider": _text(account.get("site_name")),
                "username": _text(account.get("username") or account.get("email")),
                "login_url": _text(account.get("site_url")),
                "status": _text(account.get("login_status")),
                "last_verified_at": _text(account.get("updated_at")),
                "password_saved": bool(_text(account.get("password_ciphertext"))),
                "password_storage": "dpapi",
                "source_kind": "vault",
                "pending_migration": False,
                "read_only": False,
                "auto_fill_ready": bool(_text(account.get("password_ciphertext"))),
            }
            searchable = (
                summary["account_id"],
                summary["scope"],
                summary["provider"],
                summary["username"],
                summary["login_url"],
                summary["status"],
                _text(account.get("purpose")),
                _text(account.get("related_journals")),
                _text(account.get("notes")),
            )
            if wanted_scope not in {"", "all"} and inferred_scope != wanted_scope:
                continue
            if wanted_source not in {"", "all", "vault"}:
                continue
            if needle and not any(needle in _normalized(value) for value in searchable):
                continue
            summaries.append(summary)

        for account in legacy_accounts:
            account_id = _text(account.get("id"))
            inferred_scope = self._infer_vault_scope(account)
            summary = {
                "record_key": f"legacy:{account_id}",
                "account_id": account_id,
                "scope": inferred_scope,
                "provider": _text(account.get("site_name")),
                "username": _text(account.get("username") or account.get("email")),
                "login_url": _text(account.get("site_url")),
                "status": _text(account.get("login_status")),
                "last_verified_at": _text(account.get("updated_at")),
                "password_saved": bool(_text(account.get("password_ciphertext"))),
                "password_storage": "dpapi_legacy_read_only",
                "source_kind": "legacy",
                "pending_migration": False,
                "read_only": True,
                "auto_fill_ready": True,
            }
            searchable = (
                summary["account_id"],
                summary["scope"],
                summary["provider"],
                summary["username"],
                summary["login_url"],
                summary["status"],
                _text(account.get("purpose")),
                _text(account.get("related_journals")),
                _text(account.get("notes")),
            )
            if wanted_scope not in {"", "all"} and inferred_scope != wanted_scope:
                continue
            if wanted_source not in {"", "all", "legacy"}:
                continue
            if needle and not any(needle in _normalized(value) for value in searchable):
                continue
            summaries.append(summary)

        for account in smart_accounts:
            account_id = _text(account.get("account_id"))
            credential_ref = _text(account.get("credential_ref"))
            if credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX):
                ref_id = credential_ref[len(ACCOUNT_VAULT_REF_PREFIX) :]
                ref_ready = bool(ref_id and ref_id in account_vault_ids)
                ref_storage = "dpapi_account_vault"
            elif credential_ref.startswith(CREDENTIAL_REF_PREFIX):
                ref_id = credential_ref[len(CREDENTIAL_REF_PREFIX) :]
                ref_ready = bool(ref_id and ref_id in legacy_ids)
                ref_storage = "dpapi_legacy_linked"
            else:
                ref_id = ""
                ref_ready = False
                ref_storage = "broken_reference" if credential_ref else "none"
            has_plaintext = bool(_text(account.get("password")))
            if ref_ready and has_plaintext:
                storage = f"{ref_storage}_plaintext_retained"
            elif ref_ready:
                storage = ref_storage
            elif has_plaintext:
                storage = "plaintext_compatibility"
            else:
                storage = ref_storage
            summary = {
                "record_key": f"smart:{account_id}",
                "account_id": account_id,
                "scope": _text(account.get("scope")),
                "provider": _text(account.get("provider")),
                "username": _text(account.get("username") or account.get("email")),
                "login_url": _text(account.get("login_url")),
                "status": _text(account.get("status")),
                "last_verified_at": _text(account.get("last_verified_at")),
                "password_saved": has_plaintext or ref_ready,
                "password_storage": storage,
                "source_kind": "smart",
                "pending_migration": has_plaintext,
                "read_only": False,
                "auto_fill_ready": ref_ready,
            }
            searchable_fields = (
                "account_id",
                "scope",
                "provider",
                "username",
                "email",
                "recovery_email",
                "login_url",
                "status",
                "notes",
            )
            if wanted_scope not in {"", "all"} and _canonical_scope(account.get("scope")) != _canonical_scope(wanted_scope):
                continue
            if wanted_source == "vault":
                continue
            if wanted_source == "pending" and not summary["pending_migration"]:
                continue
            if wanted_source not in {"", "all", "smart", "pending"}:
                continue
            if needle and not any(needle in _normalized(account.get(field)) for field in searchable_fields):
                continue
            summaries.append(summary)

        source_order = {"legacy": 0, "vault": 1, "smart": 2}
        return sorted(
            summaries,
            key=lambda item: (
                source_order.get(_text(item.get("source_kind")), 9),
                _normalized(item.get("provider")),
                _normalized(item.get("username")),
            ),
        )

    @staticmethod
    def _split_record_key(record_key: str) -> tuple[str, str]:
        source, separator, record_id = _text(record_key).partition(":")
        if separator and source in {"vault", "legacy", "smart"} and record_id:
            return source, record_id
        raise RecordNotFoundError("账号记录标识不合法，请刷新后重试。")

    def get_account(self, record_key: str) -> dict[str, Any]:
        source, record_id = self._split_record_key(record_key)
        if source == "legacy":
            record = self._legacy_vault_backend().get_record_by_id(record_id)
            return {
                "record_key": record_key,
                "source_kind": "legacy",
                "account_id": record_id,
                "scope": self._infer_vault_scope(record),
                "provider": _text(record.get("site_name")),
                "username": _text(record.get("username")),
                "email": _text(record.get("email")),
                "login_url": _text(record.get("site_url")),
                "purpose": _text(record.get("purpose")),
                "related_journals": _text(record.get("related_journals")),
                "status": _text(record.get("login_status")),
                "notes": _text(record.get("notes")),
                "updated_at": _text(record.get("updated_at")),
                "password_storage": "dpapi_legacy_read_only",
                "read_only": True,
                "auto_fill_ready": True,
            }
        if source == "vault":
            record = self.vault.get_record_by_id(record_id)
            return {
                "record_key": record_key,
                "source_kind": "vault",
                "account_id": record_id,
                "scope": self._infer_vault_scope(record),
                "provider": _text(record.get("site_name")),
                "username": _text(record.get("username")),
                "email": _text(record.get("email")),
                "login_url": _text(record.get("site_url")),
                "purpose": _text(record.get("purpose")),
                "related_journals": _text(record.get("related_journals")),
                "status": _text(record.get("login_status")),
                "notes": _text(record.get("notes")),
                "updated_at": _text(record.get("updated_at")),
                "password_storage": "dpapi",
                "read_only": False,
                "auto_fill_ready": bool(_text(record.get("password_ciphertext"))),
            }
        wanted = _normalized(record_id)
        for account in self._load_credentials()["accounts"]:
            if _normalized(account.get("account_id")) == wanted:
                result = copy.deepcopy(account)
                result.pop("password", None)
                result["record_key"] = record_key
                result["source_kind"] = "smart"
                credential_ref = _text(account.get("credential_ref"))
                if credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX):
                    result["password_storage"] = "dpapi_account_vault"
                    result["auto_fill_ready"] = True
                elif credential_ref.startswith(CREDENTIAL_REF_PREFIX):
                    result["password_storage"] = "dpapi_legacy_linked"
                    result["auto_fill_ready"] = True
                elif _text(account.get("password")):
                    result["password_storage"] = "plaintext_compatibility"
                    result["auto_fill_ready"] = False
                else:
                    result["password_storage"] = (
                        "broken_reference" if credential_ref else "none"
                    )
                    result["auto_fill_ready"] = False
                return result
        raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")

    def reveal_password(self, record_key: str) -> str:
        source, record_id = self._split_record_key(record_key)
        audit_id = self._record_reference_digest(source, record_id)
        with self._transaction():
            audit = self._read_audit()
            try:
                if source == "legacy":
                    password = self._legacy_vault_backend().reveal_by_id(record_id)
                elif source == "vault":
                    password = self.vault.reveal_by_id(record_id)
                else:
                    wanted = _normalized(record_id)
                    account = next(
                        (
                            item
                            for item in self._load_credentials()["accounts"]
                            if _normalized(item.get("account_id")) == wanted
                        ),
                        None,
                    )
                    if account is None:
                        raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
                    password = SmartCredentialResolver(
                        self.legacy_vault_path,
                        vault=self._legacy_vault_backend(),
                        account_vault_path=self.vault_path,
                        account_vault=self.vault,
                    ).resolve_password(account)
            except Exception:
                try:
                    self._append_audit(
                        copy.deepcopy(audit),
                        action="reveal-password-failure",
                        record_type=f"account-secret-{source}",
                        record_id=audit_id,
                        backup=None,
                        operation_id=uuid4().hex,
                    )
                except Exception:
                    raise RegistryValidationError("密码读取失败，且安全审计未完成。") from None
                raise RegistryValidationError("密码无法由当前 Windows 用户安全读取。") from None

            try:
                self._append_audit(
                    copy.deepcopy(audit),
                    action="reveal-password-success",
                    record_type=f"account-secret-{source}",
                    record_id=audit_id,
                    backup=None,
                    operation_id=uuid4().hex,
                )
            except Exception:
                password = ""
                raise RegistryValidationError("密码未显示：安全审计写入失败。") from None
            return password

    def save_account(
        self,
        record: dict[str, Any],
        *,
        original_id: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Save new accounts to DPAPI; Smart compatibility edits keep plaintext unchanged."""
        if not isinstance(record, dict):
            raise RegistryValidationError("账号记录格式不合法。")
        scope = _required(record, "scope", "账号分类")
        if scope not in ACCOUNT_SCOPES:
            raise RegistryValidationError("账号分类必须是邮箱账号、投稿系统账号或外部服务账号。")
        source = "vault"
        record_id = ""
        if original_id is not None:
            source, record_id = self._split_record_key(original_id)
        if source == "legacy":
            raise RegistryValidationError(
                "旧稳定投稿加密账号库当前为只读；不能修改、迁移或删除其中的记录。"
            )
        if source == "vault":
            self._require_writable_vault()
            if not (_text(record.get("provider")) or _text(record.get("login_url"))):
                raise RegistryValidationError("网站/系统名称和登录网址至少填写一项。")
            if not (_text(record.get("username")) or _text(record.get("email"))):
                raise RegistryValidationError("账号和邮箱至少填写一项。")
            with self._transaction():
                self._prepare_private_parent(self.vault_path)
                existing_records = self.vault.list_records()
                supplied_id = _text(record.get("account_id"))
                if original_id is not None:
                    if supplied_id and supplied_id != record_id:
                        raise RegistryValidationError("账号内部标识只读，编辑时不能修改。")
                    if not any(_text(item.get("id")) == record_id for item in existing_records):
                        raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
                    candidate_id = record_id
                else:
                    candidate_id = supplied_id or uuid4().hex
                    normalized_candidate = _normalized_identifier(candidate_id)
                    if not normalized_candidate:
                        raise RegistryValidationError("账号内部标识不合法。")
                    if normalized_candidate.startswith(("vault:", "legacy:", "smart:")):
                        raise RegistryValidationError("账号内部标识不能使用保留来源前缀。")
                    if any(
                        _normalized_identifier(item.get("id")) == normalized_candidate
                        for item in existing_records
                    ):
                        raise DuplicateRecordError("账号内部标识已存在，不能覆盖现有账号。")
                vault_record = {
                    "id": candidate_id,
                    "site_name": _text(record.get("provider")),
                    "site_url": _text(record.get("login_url")),
                    "username": _text(record.get("username")),
                    "email": _text(record.get("email")),
                    "purpose": _text(record.get("purpose")) or scope,
                    "related_journals": _text(record.get("related_journals")),
                    "login_status": _text(record.get("status")) or "unknown",
                    "notes": _text(record.get("notes")),
                    "source": _text(record.get("source")) or "shared-account-center",
                }
                action = "update" if original_id else "create"
                vault_data, saved = self.vault.prepare_upsert(
                    vault_record,
                    password=password,
                    original_id=record_id or None,
                )
                self._run_audited_file_replace_locked(
                    target_path=self.vault_path,
                    post_bytes=self._json_bytes(vault_data),
                    action=action,
                    record_type="account-vault",
                    record_id=self._record_reference_digest("vault", candidate_id),
                )
                saved_id = _text(saved.get("id"))
                return self.get_account(f"vault:{saved_id}")

        if password:
            raise RegistryValidationError("智能投稿兼容记录不能直接改写密码；加密迁移尚未开放。")
        account_id = _required(record, "account_id", "账号标识")
        normalized_account_id = _normalized_identifier(account_id)
        if normalized_account_id.startswith(("vault:", "legacy:", "smart:")):
            raise RegistryValidationError("账号内部标识不能使用保留来源前缀。")
        with self._transaction():
            data = self._load_credentials()
            accounts = data["accounts"]
            wanted = _normalized(record_id)
            original_index = next(
                (index for index, item in enumerate(accounts) if _normalized(item.get("account_id")) == wanted),
                None,
            )
            if original_index is None:
                raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
            if account_id != record_id and self._account_has_protected_reference(record_id):
                raise RegistryValidationError(
                    "账号正在被智能投稿任务或投稿路线引用，内部标识不能修改。"
                )
            for index, item in enumerate(accounts):
                if index != original_index and _normalized(item.get("account_id")) == _normalized(account_id):
                    raise DuplicateRecordError("账号标识已存在，请使用其他标识。")
            candidate = copy.deepcopy(accounts[original_index])
            ui_only_fields = {
                "password",
                "credential_ref",
                "record_key",
                "source_kind",
                "password_storage",
                "pending_migration",
            }
            safe_record = {key: value for key, value in record.items() if key not in ui_only_fields}
            candidate.update(copy.deepcopy(safe_record))
            candidate["account_id"] = account_id
            candidate["scope"] = scope
            accounts[original_index] = candidate
            self._write_with_audit(
                self.credential_path,
                data,
                action="update",
                record_type="account-smart-compatibility",
                record_id=account_id,
            )
            result = copy.deepcopy(candidate)
            result.pop("password", None)
            return result

    def copy_smart_account_to_vault(self, record_key: str) -> dict[str, Any]:
        """Move one Smart compatibility secret into the current-user DPAPI vault.

        The encrypted vault is committed first.  Only after that commit is
        durable is the plaintext removed and replaced by an ``account-vault:``
        reference.  A crash between the two commits therefore leaves a
        recoverable duplicate, never a lost password.
        """

        source, record_id = self._split_record_key(record_key)
        if source != "smart":
            raise RegistryValidationError("只有智能投稿兼容记录需要迁移。")
        self._require_writable_vault()
        with self._transaction():
            credentials = self._load_credentials()
            wanted = _normalized(record_id)
            account = next(
                (
                    item
                    for item in credentials["accounts"]
                    if _normalized(item.get("account_id")) == wanted
                ),
                None,
            )
            if account is None:
                raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")

            credential_ref = _text(account.get("credential_ref"))
            plaintext_retained = bool(_text(account.get("password")))
            if (
                credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX)
                and not plaintext_retained
            ):
                vault_record_id = credential_ref[len(ACCOUNT_VAULT_REF_PREFIX) :]
                if any(
                    _text(item.get("id")) == vault_record_id
                    for item in self.vault.list_records()
                ):
                    return self.get_account(record_key)
                raise RegistryValidationError(
                    "账号凭据引用对应的加密记录不存在，已停止迁移。"
                )

            resolver = SmartCredentialResolver(
                self.legacy_vault_path,
                vault=self._legacy_vault_backend(),
                account_vault_path=self.vault_path,
                account_vault=self.vault,
            )
            try:
                password = resolver.resolve_password(account)
            except Exception:
                raise RegistryValidationError(
                    "账号密码无法由当前 Windows 用户安全读取，未修改原记录。"
                ) from None
            if not password:
                raise RegistryValidationError("账号没有可迁移的密码。")

            existing_vault_id = (
                credential_ref[len(ACCOUNT_VAULT_REF_PREFIX) :]
                if credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX)
                else ""
            )
            vault_record_id = existing_vault_id or self._stable_record_id(
                "smart",
                account.get("account_id"),
                account.get("provider"),
                account.get("username") or account.get("email"),
            )
            vault_record = {
                "id": vault_record_id,
                "site_name": _text(account.get("provider")),
                "site_url": _text(account.get("login_url")),
                "username": _text(account.get("username")),
                "email": _text(account.get("email")),
                "purpose": _text(account.get("scope")) or "journal_system",
                "related_journals": _text(
                    account.get("related_journals")
                    or account.get("journal")
                    or account.get("journal_name")
                ),
                "login_status": _text(account.get("status")) or "configured",
                "notes": _text(account.get("notes")),
                "source": "smart-plaintext-migration",
            }
            vault_data, saved = self.vault.prepare_upsert(
                vault_record,
                password=password,
                original_id=existing_vault_id or None,
            )
            password = ""
            self._prepare_private_parent(self.vault_path)
            self._run_audited_file_replace_locked(
                target_path=self.vault_path,
                post_bytes=self._json_bytes(vault_data),
                action="migrate-secret",
                record_type="account-vault",
                record_id=self._record_reference_digest("smart", record_id),
            )
            # Compact the first committed marker before the second audited
            # target is written under the same outer lock.
            self._recover_vault_transactions_locked(compact_committed=True)

            migrated = copy.deepcopy(account)
            migrated.pop("password", None)
            migrated["credential_ref"] = (
                f"{ACCOUNT_VAULT_REF_PREFIX}{_text(saved.get('id'))}"
            )
            migrated["credential_migrated_at"] = now_iso()
            migrated["credential_storage"] = "win32-dpapi-current-user"
            account_index = credentials["accounts"].index(account)
            credentials["accounts"][account_index] = migrated
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="remove-plaintext-after-vault-commit",
                record_type="account-smart-compatibility",
                record_id=record_id,
            )
        return self.get_account(record_key)

    def migrate_smart_plaintext_credentials(self) -> dict[str, Any]:
        """Migrate every remaining Smart plaintext secret without exposing it."""

        credentials = self._load_credentials()
        pending = [
            _text(item.get("account_id"))
            for item in credentials.get("accounts", [])
            if isinstance(item, dict)
            and _text(item.get("account_id"))
            and _text(item.get("password"))
        ]
        migrated: list[str] = []
        failed: dict[str, str] = {}
        for account_id in pending:
            try:
                self.copy_smart_account_to_vault(f"smart:{account_id}")
            except Exception as exc:
                failed[account_id] = type(exc).__name__
            else:
                migrated.append(account_id)
        return {
            "ok": not failed,
            "pending_before": len(pending),
            "migrated_count": len(migrated),
            "migrated_ids": migrated,
            "failed": failed,
            "plaintext_remaining": sum(
                1
                for item in self._load_credentials().get("accounts", [])
                if isinstance(item, dict) and _text(item.get("password"))
            ),
        }

    def create_smart_account_with_secret(
        self,
        record: dict[str, Any],
        *,
        password: str,
    ) -> dict[str, Any]:
        """Create one runtime Smart account with only a DPAPI reference on disk."""

        if not isinstance(record, dict):
            raise RegistryValidationError("账号记录格式不合法。")
        account_id = _required(record, "account_id", "账号标识")
        scope = _required(record, "scope", "账号分类")
        if scope not in ACCOUNT_SCOPES:
            raise RegistryValidationError("账号分类不受支持。")
        if not (_text(record.get("username")) or _text(record.get("email"))):
            raise RegistryValidationError("账号和邮箱至少填写一项。")
        if not password:
            raise RegistryValidationError("空密码不会写入密码保险库。")
        self._require_writable_vault()

        with self._transaction():
            credentials = self._load_credentials()
            normalized_id = _normalized_identifier(account_id)
            if any(
                _normalized_identifier(item.get("account_id")) == normalized_id
                for item in credentials["accounts"]
            ):
                raise DuplicateRecordError("账号标识已存在。")
            vault_record_id = self._stable_record_id(
                "smart",
                account_id,
                record.get("provider"),
                record.get("username") or record.get("email"),
            )
            vault_record = {
                "id": vault_record_id,
                "site_name": _text(record.get("provider")),
                "site_url": _text(record.get("login_url")),
                "username": _text(record.get("username")),
                "email": _text(record.get("email")),
                "purpose": scope,
                "related_journals": _text(record.get("related_journals")),
                "login_status": _text(record.get("status")) or "registration_pending",
                "notes": _text(record.get("notes")),
                "source": "trusted-credential-broker-registration",
            }
            vault_data, saved = self.vault.prepare_upsert(
                vault_record,
                password=password,
            )
            password = ""
            self._prepare_private_parent(self.vault_path)
            self._run_audited_file_replace_locked(
                target_path=self.vault_path,
                post_bytes=self._json_bytes(vault_data),
                action="create-registration-secret",
                record_type="account-vault",
                record_id=self._record_reference_digest("smart", account_id),
            )
            self._recover_vault_transactions_locked(compact_committed=True)

            now = now_iso()
            account = {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if key not in {"password", "password_ciphertext", "record_key"}
            }
            account["account_id"] = account_id
            account["scope"] = scope
            account["credential_ref"] = (
                f"{ACCOUNT_VAULT_REF_PREFIX}{_text(saved.get('id'))}"
            )
            account["credential_storage"] = "win32-dpapi-current-user"
            account["created_at"] = now
            account["updated_at"] = now
            credentials["accounts"].append(account)
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="create-registration-account",
                record_type="account-smart-compatibility",
                record_id=account_id,
            )
        return self.get_account(f"smart:{account_id}")

    def create_smart_session_account(
        self,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Create metadata for an already authenticated browser session.

        No password or credential reference is invented. The record only gives
        signed live-session evidence a stable account identity; when that
        browser session expires, normal credential recovery remains fail-closed.
        """

        if not isinstance(record, dict):
            raise RegistryValidationError("账号记录格式不合法。")
        account_id = _required(record, "account_id", "账号标识")
        scope = _required(record, "scope", "账号分类")
        if scope not in ACCOUNT_SCOPES:
            raise RegistryValidationError("账号分类不受支持。")
        if not (_text(record.get("username")) or _text(record.get("email"))):
            raise RegistryValidationError("账号和邮箱至少填写一项。")

        with self._transaction():
            credentials = self._load_credentials()
            normalized_id = _normalized_identifier(account_id)
            if any(
                _normalized_identifier(item.get("account_id")) == normalized_id
                for item in credentials["accounts"]
            ):
                raise DuplicateRecordError("账号标识已存在。")
            now = now_iso()
            account = {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if key
                not in {
                    "password",
                    "password_ciphertext",
                    "credential_ref",
                    "record_key",
                }
            }
            account["account_id"] = account_id
            account["scope"] = scope
            account["credential_storage"] = "browser-session-only"
            account["created_at"] = now
            account["updated_at"] = now
            credentials["accounts"].append(account)
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="create-browser-session-account",
                record_type="account-smart-compatibility",
                record_id=account_id,
            )
        return self.get_account(f"smart:{account_id}")

    def create_smart_account_with_credential_reference(
        self,
        record: dict[str, Any],
        *,
        credential_record_key: str,
    ) -> dict[str, Any]:
        """Create Smart account metadata linked to an existing encrypted secret."""

        if not isinstance(record, dict):
            raise RegistryValidationError("账号记录格式不合法。")
        account_id = _required(record, "account_id", "账号标识")
        scope = _required(record, "scope", "账号分类")
        if scope not in ACCOUNT_SCOPES:
            raise RegistryValidationError("账号分类不受支持。")
        if not (_text(record.get("username")) or _text(record.get("email"))):
            raise RegistryValidationError("账号和邮箱至少填写一项。")

        source, source_id = self._split_record_key(credential_record_key)
        source_account = self.get_account(credential_record_key)
        if not bool(source_account.get("auto_fill_ready")):
            raise RegistryValidationError("被引用账号没有可由本地代理读取的加密密码。")
        if source == "vault":
            credential_ref = f"{ACCOUNT_VAULT_REF_PREFIX}{source_id}"
        elif source == "legacy":
            credential_ref = f"{CREDENTIAL_REF_PREFIX}{source_id}"
        else:
            credential_ref = _text(source_account.get("credential_ref"))
        if not credential_ref.startswith(
            (ACCOUNT_VAULT_REF_PREFIX, CREDENTIAL_REF_PREFIX)
        ):
            raise RegistryValidationError("被引用账号的加密凭据链接无效。")

        with self._transaction():
            credentials = self._load_credentials()
            normalized_id = _normalized_identifier(account_id)
            if any(
                _normalized_identifier(item.get("account_id")) == normalized_id
                for item in credentials["accounts"]
            ):
                raise DuplicateRecordError("账号标识已存在。")
            now = now_iso()
            account = {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if key
                not in {
                    "password",
                    "password_ciphertext",
                    "credential_ref",
                    "record_key",
                }
            }
            account["account_id"] = account_id
            account["scope"] = scope
            account["credential_ref"] = credential_ref
            account["credential_storage"] = "linked-encrypted-reference"
            account["created_at"] = now
            account["updated_at"] = now
            credentials["accounts"].append(account)
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="create-linked-identity-account",
                record_type="account-smart-compatibility",
                record_id=account_id,
            )
        return self.get_account(f"smart:{account_id}")

    def update_smart_account_recovered_secret(
        self,
        account_id: str,
        *,
        password: str,
    ) -> dict[str, Any]:
        """Commit a remotely accepted recovery password and mark the account ready.

        This narrow mutation is reserved for the trusted local credential
        broker.  The encrypted vault is committed first; a crash between the
        two audited writes leaves a usable secret with conservative pending
        metadata, never a ready account with a missing password.
        """

        clean_id = _text(account_id)
        if not clean_id:
            raise RegistryValidationError("账号标识不能为空。")
        if not password:
            raise RegistryValidationError("空密码不会写入密码保险库。")
        self._require_writable_vault()
        with self._transaction():
            credentials = self._load_credentials()
            wanted = _normalized(clean_id)
            account = next(
                (
                    item
                    for item in credentials["accounts"]
                    if _normalized(item.get("account_id")) == wanted
                ),
                None,
            )
            if account is None:
                raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
            credential_ref = _text(account.get("credential_ref"))
            if not credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX):
                raise RegistryValidationError(
                    "账号尚未迁移到当前 Windows 用户的加密密码库，不能自动恢复。"
                )
            vault_record_id = credential_ref[len(ACCOUNT_VAULT_REF_PREFIX) :]
            try:
                vault_record = self.vault.get_record_by_id(vault_record_id)
            except Exception:
                raise RegistryValidationError(
                    "账号对应的加密密码记录不存在，不能自动恢复。"
                ) from None
            vault_record.update(
                {
                    "login_status": "ready",
                    "source": "trusted-credential-broker-recovery",
                }
            )
            vault_data, _saved = self.vault.prepare_upsert(
                vault_record,
                password=password,
                original_id=vault_record_id,
            )
            password = ""
            self._prepare_private_parent(self.vault_path)
            self._run_audited_file_replace_locked(
                target_path=self.vault_path,
                post_bytes=self._json_bytes(vault_data),
                action="recover-account-secret",
                record_type="account-vault",
                record_id=self._record_reference_digest("smart", clean_id),
            )
            self._recover_vault_transactions_locked(compact_committed=True)

            account["status"] = "ready"
            account["updated_at"] = now_iso()
            account["credential_recovered_at"] = account["updated_at"]
            account["credential_recovery_method"] = "trusted-mailbox-reset"
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="recover-registration-account",
                record_type="account-smart-compatibility",
                record_id=clean_id,
            )
        return self.get_account(f"smart:{clean_id}")

    def mark_smart_account_registration_ready(
        self,
        account_id: str,
    ) -> dict[str, Any]:
        """Mark a broker-created account ready after remote authentication."""

        clean_id = _text(account_id)
        if not clean_id:
            raise RegistryValidationError("账号标识不能为空。")
        self._require_writable_vault()
        with self._transaction():
            credentials = self._load_credentials()
            wanted = _normalized(clean_id)
            account = next(
                (
                    item
                    for item in credentials["accounts"]
                    if _normalized(item.get("account_id")) == wanted
                ),
                None,
            )
            if account is None:
                raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
            credential_ref = _text(account.get("credential_ref"))
            if not credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX):
                raise RegistryValidationError(
                    "账号尚未迁移到当前 Windows 用户的加密密码库。"
                )
            vault_record_id = credential_ref[len(ACCOUNT_VAULT_REF_PREFIX) :]
            try:
                vault_record = self.vault.get_record_by_id(vault_record_id)
            except Exception:
                raise RegistryValidationError(
                    "账号对应的加密密码记录不存在，不能确认注册完成。"
                ) from None
            vault_record.update(
                {
                    "login_status": "ready",
                    "source": "trusted-credential-broker-registration-complete",
                }
            )
            vault_data, _saved = self.vault.prepare_upsert(
                vault_record,
                original_id=vault_record_id,
            )
            self._prepare_private_parent(self.vault_path)
            self._run_audited_file_replace_locked(
                target_path=self.vault_path,
                post_bytes=self._json_bytes(vault_data),
                action="complete-registration-account",
                record_type="account-vault",
                record_id=self._record_reference_digest("smart", clean_id),
            )
            self._recover_vault_transactions_locked(compact_committed=True)

            account["status"] = "ready"
            account["updated_at"] = now_iso()
            account["registration_completed_at"] = account["updated_at"]
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="complete-registration-account",
                record_type="account-smart-compatibility",
                record_id=clean_id,
            )
        return self.get_account(f"smart:{clean_id}")

    def update_smart_account_recovered_username(
        self,
        account_id: str,
        *,
        username: str,
    ) -> dict[str, Any]:
        """Commit a username proven by a trusted local recovery mailbox."""

        clean_id = _text(account_id)
        clean_username = _text(username)
        if not clean_id or not clean_username:
            raise RegistryValidationError(
                "账号标识和恢复后的用户名都不能为空。"
            )
        with self._transaction():
            credentials = self._load_credentials()
            wanted = _normalized(clean_id)
            account = next(
                (
                    item
                    for item in credentials["accounts"]
                    if _normalized(item.get("account_id")) == wanted
                ),
                None,
            )
            if account is None:
                raise RecordNotFoundError(
                    "账号记录已不存在，不能写入恢复后的用户名。"
                )
            account["username"] = clean_username
            account["updated_at"] = now_iso()
            account["credential_username_recovered_at"] = account["updated_at"]
            account["credential_username_recovery_method"] = (
                "trusted-mailbox-reminder"
            )
            self._write_with_audit(
                self.credential_path,
                credentials,
                action="recover-account-username",
                record_type="account-smart-compatibility",
                record_id=clean_id,
            )
        return self.get_account(f"smart:{clean_id}")

    def delete_account(self, record_key: str) -> None:
        source, record_id = self._split_record_key(record_key)
        if source == "legacy":
            raise RegistryValidationError(
                "旧稳定投稿加密账号库当前为只读；不能修改、迁移或删除其中的记录。"
            )
        with self._transaction():
            if source == "vault":
                self._require_writable_vault()
                self._prepare_private_parent(self.vault_path)
                if not any(
                    _text(item.get("id")) == record_id
                    for item in self.vault.list_records()
                ):
                    raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
                if self._account_has_protected_reference(record_id):
                    raise RegistryValidationError(
                        "账号正在被智能投稿任务或投稿路线引用，不能删除。"
                    )
                linked = [
                    item
                    for item in self._load_credentials()["accounts"]
                    if _text(item.get("credential_ref")) == self.vault.ref_for_record_id(record_id)
                ]
                if linked:
                    raise RegistryValidationError("该加密账号仍被 Smart 兼容记录引用，不能直接删除。")
                vault_data, deleted = self.vault.prepare_delete_by_id(record_id)
                if not deleted:
                    raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
                self._run_audited_file_replace_locked(
                    target_path=self.vault_path,
                    post_bytes=self._json_bytes(vault_data),
                    action="delete",
                    record_type="account-vault",
                    record_id=self._record_reference_digest("vault", record_id),
                )
                return
            data = self._load_credentials()
            wanted = _normalized(record_id)
            original = next(
                (item for item in data["accounts"] if _normalized(item.get("account_id")) == wanted),
                None,
            )
            if original is None:
                raise RecordNotFoundError("账号记录已不存在，请刷新后重试。")
            if self._account_has_protected_reference(record_id):
                raise RegistryValidationError(
                    "账号正在被智能投稿任务或投稿路线引用，不能删除。"
                )
            remaining = [item for item in data["accounts"] if _normalized(item.get("account_id")) != wanted]
            data["accounts"] = remaining
            self._write_with_audit(
                self.credential_path,
                data,
                action="delete",
                record_type="account-smart-compatibility",
                record_id=record_id,
            )

    def list_systems(self, query: str = "", status: str = "all") -> list[dict[str, Any]]:
        systems = self._load_sites()["systems"]
        needle = _normalized(query)
        wanted_status = _normalized(status)
        fields = ("system_id", "label", "status", "login_url", "submission_url", "status_url_template", "notes")
        result = [
            copy.deepcopy(item)
            for item in systems
            if (wanted_status in {"", "all"} or _normalized(item.get("status")) == wanted_status)
            and (not needle or any(needle in _normalized(item.get(field)) for field in fields))
        ]
        return sorted(result, key=lambda item: _normalized(item.get("label") or item.get("system_id")))

    def get_system(self, system_id: str) -> dict[str, Any]:
        wanted = _normalized(system_id)
        for system in self._load_sites()["systems"]:
            if _normalized(system.get("system_id")) == wanted:
                return copy.deepcopy(system)
        raise RecordNotFoundError("投稿系统记录已不存在，请刷新后重试。")

    def save_system(self, record: dict[str, Any], *, original_id: str | None = None) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise RegistryValidationError("投稿系统记录格式不合法。")
        label = _required(record, "label", "系统名称")
        system_id = _text(record.get("system_id"))
        if original_id is not None:
            system_id = system_id or _text(original_id)
            if system_id != _text(original_id):
                raise RegistryValidationError("投稿系统内部标识只读，编辑时不能修改。")
        else:
            system_id = system_id or self._stable_record_id(
                "system",
                label,
                record.get("login_url"),
                record.get("submission_url"),
            )
        with self._transaction():
            data = self._load_sites()
            systems = data["systems"]
            original_index: int | None = None
            original_record: dict[str, Any] = {}
            if original_id is not None:
                wanted = _normalized(original_id)
                original_index = next(
                    (index for index, item in enumerate(systems) if _normalized(item.get("system_id")) == wanted),
                    None,
                )
                if original_index is None:
                    raise RecordNotFoundError("投稿系统记录已不存在，请刷新后重试。")
                original_record = systems[original_index]
            for index, item in enumerate(systems):
                if index != original_index and _normalized(item.get("system_id")) == _normalized(system_id):
                    raise DuplicateRecordError("系统标识已存在，请使用其他标识。")

            candidate = copy.deepcopy(original_record)
            candidate.update(copy.deepcopy(record))
            candidate["system_id"] = system_id
            candidate["email_senders"] = _string_list(candidate.get("email_senders"))
            action = "update" if original_index is not None else "create"
            if original_index is None:
                systems.append(candidate)
            else:
                systems[original_index] = candidate
            self._write_with_audit(
                self.site_registry_path,
                data,
                action=action,
                record_type="system",
                record_id=system_id,
            )
            return copy.deepcopy(candidate)

    def delete_system(self, system_id: str) -> None:
        with self._transaction():
            data = self._load_sites()
            wanted = _normalized(system_id)
            linked = [item for item in data["journals"] if _normalized(item.get("system_id")) == wanted]
            if linked:
                raise RegistryValidationError("该投稿系统仍被期刊记录引用，请先调整或删除关联期刊。")
            if self._system_has_protected_reference(system_id):
                raise RegistryValidationError("投稿系统正在被智能投稿任务或投稿路线引用，不能删除。")
            remaining = [item for item in data["systems"] if _normalized(item.get("system_id")) != wanted]
            if len(remaining) == len(data["systems"]):
                raise RecordNotFoundError("投稿系统记录已不存在，请刷新后重试。")
            data["systems"] = remaining
            self._write_with_audit(
                self.site_registry_path,
                data,
                action="delete",
                record_type="system",
                record_id=_text(system_id),
            )

    def list_journals(self, query: str = "", system_id: str = "all") -> list[dict[str, Any]]:
        journals = self._load_sites()["journals"]
        needle = _normalized(query)
        wanted_system = _normalized(system_id)
        fields = ("journal_key", "journal_name", "system_id", "status", "submission_url", "source")
        result = [
            copy.deepcopy(item)
            for item in journals
            if (wanted_system in {"", "all"} or _normalized(item.get("system_id")) == wanted_system)
            and (not needle or any(needle in _normalized(item.get(field)) for field in fields))
        ]
        return sorted(result, key=lambda item: _normalized(item.get("journal_name") or item.get("journal_key")))

    def get_journal(self, journal_key: str) -> dict[str, Any]:
        wanted = _normalized(journal_key)
        for journal in self._load_sites()["journals"]:
            if _normalized(journal.get("journal_key")) == wanted:
                return copy.deepcopy(journal)
        raise RecordNotFoundError("期刊记录已不存在，请刷新后重试。")

    def save_journal(self, record: dict[str, Any], *, original_id: str | None = None) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise RegistryValidationError("期刊记录格式不合法。")
        journal_name = _required(record, "journal_name", "期刊名称")
        journal_key = _text(record.get("journal_key"))
        if original_id is not None:
            journal_key = journal_key or _text(original_id)
            if journal_key != _text(original_id):
                raise RegistryValidationError("期刊内部标识只读，编辑时不能修改。")
        else:
            journal_key = journal_key or self._stable_record_id(
                "journal",
                journal_name,
                record.get("system_id"),
                record.get("submission_url"),
            )
        with self._transaction():
            data = self._load_sites()
            journals = data["journals"]
            original_index: int | None = None
            original_record: dict[str, Any] = {}
            if original_id is not None:
                wanted = _normalized(original_id)
                original_index = next(
                    (index for index, item in enumerate(journals) if _normalized(item.get("journal_key")) == wanted),
                    None,
                )
                if original_index is None:
                    raise RecordNotFoundError("期刊记录已不存在，请刷新后重试。")
                original_record = journals[original_index]
            for index, item in enumerate(journals):
                if index != original_index and _normalized(item.get("journal_key")) == _normalized(journal_key):
                    raise DuplicateRecordError("期刊标识已存在，请使用其他标识。")

            candidate = copy.deepcopy(original_record)
            candidate.update(copy.deepcopy(record))
            candidate["journal_key"] = journal_key
            candidate["journal_name"] = journal_name
            candidate["linked_papers"] = _string_list(candidate.get("linked_papers"))
            candidate["updated_at"] = now_iso()
            candidate_system_id = _normalized(candidate.get("system_id"))
            if candidate_system_id and not any(
                _normalized(item.get("system_id")) == candidate_system_id
                for item in data["systems"]
            ):
                raise RegistryValidationError("期刊引用的投稿系统不存在，已停止保存。")
            if original_record and self._journal_has_protected_reference(original_record) and (
                _normalized_identifier(candidate.get("system_id"))
                != _normalized_identifier(original_record.get("system_id"))
                or _normalized_identifier(candidate.get("journal_name"))
                != _normalized_identifier(original_record.get("journal_name"))
            ):
                raise RegistryValidationError(
                    "期刊正在被智能投稿任务或投稿路线引用，不能更改绑定名称或投稿系统。"
                )
            signature = (_normalized(journal_name), _normalized(candidate.get("system_id")))
            old_signature = (
                _normalized(original_record.get("journal_name")),
                _normalized(original_record.get("system_id")),
            ) if original_record else None
            if signature != old_signature:
                for index, item in enumerate(journals):
                    other = (_normalized(item.get("journal_name")), _normalized(item.get("system_id")))
                    if index != original_index and other == signature:
                        raise DuplicateRecordError("同一投稿系统下已经存在同名期刊。")

            action = "update" if original_index is not None else "create"
            if original_index is None:
                journals.append(candidate)
            else:
                journals[original_index] = candidate
            self._write_with_audit(
                self.site_registry_path,
                data,
                action=action,
                record_type="journal",
                record_id=journal_key,
            )
            return copy.deepcopy(candidate)

    def delete_journal(self, journal_key: str) -> None:
        with self._transaction():
            data = self._load_sites()
            wanted = _normalized(journal_key)
            original = next(
                (item for item in data["journals"] if _normalized(item.get("journal_key")) == wanted),
                None,
            )
            if original is None:
                raise RecordNotFoundError("期刊记录已不存在，请刷新后重试。")
            if _string_list(original.get("linked_papers")):
                raise RegistryValidationError("该期刊仍被论文记录引用，不能删除。")
            if self._journal_has_protected_reference(original):
                raise RegistryValidationError("期刊正在被智能投稿任务或投稿路线引用，不能删除。")
            remaining = [item for item in data["journals"] if _normalized(item.get("journal_key")) != wanted]
            data["journals"] = remaining
            self._write_with_audit(
                self.site_registry_path,
                data,
                action="delete",
                record_type="journal",
                record_id=_text(journal_key),
            )

    def duplicate_report(self) -> dict[str, list[list[str]]]:
        """Return record identifiers only; identity values never leave the store."""
        credentials = self._load_credentials()
        sites = self._load_sites()
        report: dict[str, list[list[str]]] = {"accounts": [], "systems": [], "journals": []}

        account_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for account in self.vault.list_records() if not self.vault_read_only else []:
            identity = self._vault_account_identity(account)
            if identity is not None:
                account_groups[identity].append(f"新加密库:{_text(account.get('id'))}")
        for account in self._legacy_vault_backend().list_records():
            identity = self._vault_account_identity(account)
            if identity is not None:
                account_groups[identity].append(f"旧只读库:{_text(account.get('id'))}")
        for account in credentials["accounts"]:
            identity_value = _normalized(account.get("username") or account.get("email"))
            if identity_value:
                identity = (_normalized(account.get("login_url")), identity_value)
                account_groups[identity].append(f"Smart:{_text(account.get('account_id'))}")
        report["accounts"] = [sorted(ids) for ids in account_groups.values() if len(ids) > 1]

        system_groups: dict[str, list[str]] = defaultdict(list)
        for system in sites["systems"]:
            for field in ("login_url", "submission_url"):
                value = _normalized(system.get(field))
                if value:
                    system_groups[f"{field}:{value}"].append(_text(system.get("system_id")))
        report["systems"] = [sorted(set(ids)) for ids in system_groups.values() if len(set(ids)) > 1]

        journal_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for journal in sites["journals"]:
            signature = (_normalized(journal.get("journal_name")), _normalized(journal.get("system_id")))
            if signature[0]:
                journal_groups[signature].append(_text(journal.get("journal_key")))
        report["journals"] = [sorted(ids) for ids in journal_groups.values() if len(ids) > 1]
        return report
