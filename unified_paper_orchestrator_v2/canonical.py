from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unified_paper_orchestrator.io_utils import FileLock
from unified_paper_orchestrator.windows_security import restrict_private_path

from .contracts import ArtifactRef, canonical_json


_CAPABILITY = "v2.canonical.commit"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CanonicalCommitError(RuntimeError):
    """Fail-closed canonical commit error with a stable machine code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        write_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fallback_allowed = False
        if write_state is None:
            write_state = "unknown" if code == "CANONICAL_ROLLBACK_FAILED" else "not_committed"
        if write_state not in {"not_committed", "committed", "unknown"}:
            raise ValueError(f"invalid canonical write_state: {write_state}")
        self.write_state = write_state


@dataclass(frozen=True)
class CanonicalCommitAuthorization:
    """Explicit, auditable construction-time authority for the commit gate."""

    actor: str
    reason: str
    capability: str = _CAPABILITY
    authorization_id: str = field(default_factory=lambda: f"auth_{uuid.uuid4().hex}")

    def validate(self) -> None:
        if self.capability != _CAPABILITY:
            raise CanonicalCommitError(
                "CANONICAL_AUTHORIZATION_INVALID",
                "Canonical commit authorization has the wrong capability.",
            )
        if not self.actor.strip() or not self.reason.strip() or not _SAFE_NAME.fullmatch(self.authorization_id):
            raise CanonicalCommitError(
                "CANONICAL_AUTHORIZATION_INVALID",
                "Canonical commit authorization is incomplete.",
            )


@dataclass(frozen=True)
class DestinationCAS:
    """An explicit expected state for one canonical destination."""

    relative_path: str
    expected_exists: bool
    expected_sha256: str | None = None

    def normalized_sha256(self) -> str:
        if not isinstance(self.expected_exists, bool):
            raise CanonicalCommitError(
                "CANONICAL_CAS_INVALID",
                "Destination existence must be an explicit boolean expectation.",
            )
        digest = str(self.expected_sha256 or "").strip().lower()
        if self.expected_exists:
            if not _SHA256.fullmatch(digest):
                raise CanonicalCommitError(
                    "CANONICAL_CAS_INVALID",
                    "An existing destination requires an explicit SHA256 expectation.",
                )
        elif digest:
            raise CanonicalCommitError(
                "CANONICAL_CAS_INVALID",
                "A nonexistent destination expectation cannot include a SHA256 value.",
            )
        return digest


@dataclass(frozen=True)
class CanonicalCommitResult:
    commit_id: str
    root_name: str
    relative_path: str
    sha256: str
    status: str
    idempotent: bool
    backup_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_link_like(path: Path) -> bool:
    """Reject every Windows reparse boundary, not only POSIX-style symlinks."""

    if not _path_exists(path):
        return False
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return bool(reparse_flag and attributes & reparse_flag)
    except OSError:
        return True


def _path_has_link_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    anchor = Path(absolute.anchor)
    try:
        relative = absolute.relative_to(anchor)
    except ValueError:
        return True
    current = anchor
    for part in relative.parts:
        current /= part
        if _is_link_like(current):
            return True
    return False


class CanonicalCommitter:
    """The only V2 primitive allowed to publish a staged file canonically.

    The gate is disabled unless a :class:`CanonicalCommitAuthorization` is
    explicitly supplied.  It has no alternate writer and never calls an
    executor, gateway, or V1 production path.
    """

    def __init__(
        self,
        *,
        canonical_roots: Mapping[str, Path] | None = None,
        staging_roots: tuple[Path, ...] | list[Path] | None = None,
        control_root: Path | None = None,
        authorization: CanonicalCommitAuthorization | None = None,
        lock_timeout_seconds: float = 10.0,
        lock_stale_seconds: int = 300,
        acl_hardener: Callable[[Path], None] | None = restrict_private_path,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._roots = {
            str(name): Path(os.path.abspath(os.fspath(path)))
            for name, path in dict(canonical_roots or {}).items()
        }
        self._staging_roots = tuple(
            Path(os.path.abspath(os.fspath(path))) for path in (staging_roots or ())
        )
        self._control_root = (
            Path(os.path.abspath(os.fspath(control_root))) if control_root is not None else None
        )
        self._authorization = authorization
        self._lock_timeout_seconds = max(0.0, float(lock_timeout_seconds))
        self._lock_stale_seconds = max(0, int(lock_stale_seconds))
        self._acl_hardener = acl_hardener
        self._fault_injector = fault_injector

    @property
    def enabled(self) -> bool:
        return self._authorization is not None

    def configured_root(self, root_name: str) -> Path:
        """Return one configured root without granting or performing a write."""

        normalized = str(root_name or "").strip()
        if normalized not in self._roots:
            raise CanonicalCommitError(
                "CANONICAL_ROOT_UNKNOWN",
                "The requested canonical root is not configured.",
            )
        return self._roots[normalized]

    def configured_staging_roots(self) -> tuple[Path, ...]:
        """Expose configured staging boundaries without granting write authority."""

        return self._staging_roots

    @property
    def audit_path(self) -> Path | None:
        return self._control_root / "canonical_audit.jsonl" if self._control_root else None

    def commit(
        self,
        *,
        commit_id: str,
        staged: ArtifactRef,
        root_name: str,
        destination: DestinationCAS,
    ) -> CanonicalCommitResult:
        self._require_enabled()
        self._validate_commit_id(commit_id)
        expected_sha256 = destination.normalized_sha256()
        root, target, relative_path = self._destination(root_name, destination.relative_path)
        source, staged_sha256 = self._staged_file(staged)
        if target.exists() and source == target.resolve(strict=True):
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "A canonical destination cannot also be its staged source.",
            )
        request = {
            "root_name": root_name,
            "relative_path": relative_path,
            "staged_sha256": staged_sha256,
            "expected_exists": bool(destination.expected_exists),
            "expected_sha256": expected_sha256,
        }
        request_digest = hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()

        with self._commit_lock():
            self._recover_active_locked()
            # Revalidate every filesystem-bound claim after acquiring the
            # cross-process writer lock.  The declared digest is never trusted.
            root, target, relative_path = self._destination(root_name, relative_path)
            source, staged_sha256 = self._staged_file(staged)
            if staged_sha256 != request["staged_sha256"]:
                raise CanonicalCommitError(
                    "CANONICAL_SOURCE_HASH_MISMATCH",
                    "The staged artifact changed before canonical commit.",
                )

            records = self._read_audit()
            existing = self._record_for_commit(records, commit_id)
            if existing is not None:
                return self._idempotent_result(
                    existing=existing,
                    request_digest=request_digest,
                    target=target,
                    commit_id=commit_id,
                    root_name=root_name,
                    relative_path=relative_path,
                )

            self._assert_expected_state(
                target,
                expected_exists=destination.expected_exists,
                expected_sha256=expected_sha256,
            )
            try:
                return self._commit_locked(
                    commit_id=commit_id,
                    source=source,
                    staged_sha256=staged_sha256,
                    root_name=root_name,
                    root=root,
                    target=target,
                    relative_path=relative_path,
                    expected_exists=destination.expected_exists,
                    expected_sha256=expected_sha256,
                    request_digest=request_digest,
                )
            except Exception as exc:
                # An audit record is the linearization point.  If it exists,
                # an error immediately after the append cannot turn a committed
                # transaction into a rollback.
                committed = self._record_for_commit(self._read_audit(), commit_id)
                if committed is not None:
                    result = self._idempotent_result(
                        existing=committed,
                        request_digest=request_digest,
                        target=target,
                        commit_id=commit_id,
                        root_name=root_name,
                        relative_path=relative_path,
                    )
                    try:
                        self._cleanup_active_journal(preserve_backup=True)
                    except CanonicalCommitError as cleanup_exc:
                        raise CanonicalCommitError(
                            cleanup_exc.code,
                            "Canonical data and audit were committed, but transaction cleanup failed.",
                            write_state="committed",
                        ) from cleanup_exc
                    return CanonicalCommitResult(
                        **{**result.to_dict(), "status": "committed", "idempotent": False}
                    )
                try:
                    self._recover_active_locked()
                except Exception as recovery_exc:
                    raise CanonicalCommitError(
                        "CANONICAL_ROLLBACK_FAILED",
                        "Canonical commit failed and the original file could not be safely restored.",
                    ) from recovery_exc
                if isinstance(exc, CanonicalCommitError):
                    raise exc
                raise CanonicalCommitError(
                    "CANONICAL_COMMIT_FAILED",
                    "Canonical commit failed before its audit linearization point.",
                ) from exc

    def recover(self) -> bool:
        """Recover one interrupted authorized transaction, if present."""

        self._require_enabled()
        with self._commit_lock():
            return self._recover_active_locked()

    def reconcile_committed(
        self,
        *,
        commit_id: str,
        staged_sha256: str,
        root_name: str,
        destination: DestinationCAS,
    ) -> CanonicalCommitResult | None:
        """Verify an audited commit without requiring the staged file to remain."""

        self._require_enabled()
        self._validate_commit_id(commit_id)
        declared_sha256 = str(staged_sha256 or "").strip().lower()
        if not _SHA256.fullmatch(declared_sha256):
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "Canonical reconciliation requires the original staged SHA256.",
            )
        expected_sha256 = destination.normalized_sha256()
        _root, target, relative_path = self._destination(
            root_name, destination.relative_path
        )
        request = {
            "root_name": root_name,
            "relative_path": relative_path,
            "staged_sha256": declared_sha256,
            "expected_exists": bool(destination.expected_exists),
            "expected_sha256": expected_sha256,
        }
        request_digest = hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
        with self._commit_lock():
            self._recover_active_locked()
            existing = self._record_for_commit(self._read_audit(), commit_id)
            if existing is None:
                return None
            return self._idempotent_result(
                existing=existing,
                request_digest=request_digest,
                target=target,
                commit_id=commit_id,
                root_name=root_name,
                relative_path=relative_path,
            )

    def _require_enabled(self) -> None:
        if self._authorization is None:
            raise CanonicalCommitError(
                "CANONICAL_COMMIT_DISABLED",
                "Canonical commit is disabled until explicit construction-time authorization is supplied.",
            )
        self._authorization.validate()
        if not self._roots or not self._staging_roots or self._control_root is None:
            raise CanonicalCommitError(
                "CANONICAL_CONFIGURATION_INVALID",
                "Authorized canonical commit requires canonical roots, staging roots, and an isolated control directory.",
            )
        for name in self._roots:
            if not _SAFE_NAME.fullmatch(name):
                raise CanonicalCommitError(
                    "CANONICAL_CONFIGURATION_INVALID",
                    "A canonical root name is invalid.",
                )
        self._ensure_control_root()
        self._validate_root_layout()

    def _validate_root_layout(self) -> None:
        assert self._control_root is not None
        try:
            control = self._control_root.resolve(strict=True)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_CONFIGURATION_INVALID",
                "The canonical control root cannot be resolved safely.",
            ) from exc
        canonical: list[Path] = []
        staging: list[Path] = []
        for raw in self._roots.values():
            if not raw.is_dir() or _path_has_link_component(raw):
                raise CanonicalCommitError(
                    "CANONICAL_CONFIGURATION_INVALID",
                    "A canonical root is unavailable or unsafe.",
                )
            canonical.append(raw.resolve(strict=True))
        for raw in self._staging_roots:
            if not raw.is_dir() or _path_has_link_component(raw):
                raise CanonicalCommitError(
                    "CANONICAL_CONFIGURATION_INVALID",
                    "A staging root is unavailable or unsafe.",
                )
            staging.append(raw.resolve(strict=True))
        all_roots = [
            ("control", control),
            *(("canonical", path) for path in canonical),
            *(("staging", path) for path in staging),
        ]
        for index, (left_kind, left) in enumerate(all_roots):
            for right_kind, right in all_roots[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise CanonicalCommitError(
                        "CANONICAL_CONFIGURATION_INVALID",
                        f"Canonical, staging, and control roots must be disjoint ({left_kind}/{right_kind}).",
                    )

    def _ensure_control_root(self) -> None:
        assert self._control_root is not None
        if _path_has_link_component(self._control_root):
            raise CanonicalCommitError(
                "CANONICAL_CONTROL_INVALID",
                "The canonical control directory cannot be a symbolic link.",
            )
        try:
            self._control_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_CONTROL_INVALID",
                "The canonical control directory is unavailable.",
            ) from exc
        if not self._control_root.is_dir() or _path_has_link_component(self._control_root):
            raise CanonicalCommitError(
                "CANONICAL_CONTROL_INVALID",
                "The canonical control path is not a private directory.",
            )
        if self._acl_hardener is not None:
            try:
                self._acl_hardener(self._control_root)
            except Exception as exc:
                raise CanonicalCommitError(
                    "CANONICAL_CONTROL_INVALID",
                    "The canonical control directory could not be hardened.",
                ) from exc

    @staticmethod
    def _validate_commit_id(commit_id: str) -> None:
        if not _SAFE_NAME.fullmatch(str(commit_id or "")):
            raise CanonicalCommitError(
                "CANONICAL_COMMIT_ID_INVALID",
                "Canonical commit_id must be a safe, nonempty identifier.",
            )

    def _destination(self, root_name: str, relative_raw: str) -> tuple[Path, Path, str]:
        if root_name not in self._roots:
            raise CanonicalCommitError(
                "CANONICAL_ROOT_UNKNOWN",
                "The requested canonical root is not configured.",
            )
        root = self._roots[root_name]
        if not root.exists() or not root.is_dir() or _path_has_link_component(root):
            raise CanonicalCommitError(
                "CANONICAL_ROOT_INVALID",
                "The configured canonical root is unavailable or unsafe.",
            )
        root = root.resolve(strict=True)
        relative = Path(str(relative_raw or ""))
        if (
            relative.is_absolute()
            or relative.drive
            or not relative.parts
            or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
        ):
            raise CanonicalCommitError(
                "CANONICAL_PATH_ESCAPE",
                "The canonical destination must be a safe relative path.",
            )
        normalized_relative = Path(*relative.parts).as_posix()
        target = Path(os.path.abspath(os.fspath(root / Path(*relative.parts))))
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise CanonicalCommitError(
                "CANONICAL_PATH_ESCAPE",
                "The canonical destination escapes its configured root.",
            ) from exc

        current = root
        for part in relative.parts:
            current /= part
            if _is_link_like(current):
                raise CanonicalCommitError(
                    "CANONICAL_SYMLINK_REJECTED",
                    "Symbolic links are forbidden in canonical destination paths.",
                )
        if not target.parent.exists() or not target.parent.is_dir():
            raise CanonicalCommitError(
                "CANONICAL_PARENT_MISSING",
                "The canonical destination parent directory must already exist.",
            )
        try:
            target.parent.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise CanonicalCommitError(
                "CANONICAL_PATH_ESCAPE",
                "The canonical destination parent resolves outside its configured root.",
            ) from exc
        return root, target, normalized_relative

    def _staged_file(self, staged: ArtifactRef) -> tuple[Path, str]:
        try:
            staged.validate()
        except ValueError as exc:
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "The staged artifact reference is invalid.",
            ) from exc
        declared = staged.sha256.strip().lower()
        if not _SHA256.fullmatch(declared):
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "A staged artifact requires an explicit SHA256 digest.",
            )
        source = Path(staged.uri)
        if not source.is_absolute():
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "The staged artifact URI must be an absolute local path.",
            )
        source = Path(os.path.abspath(os.fspath(source)))
        allowed = False
        for raw_root in self._staging_roots:
            root = raw_root.resolve(strict=True)
            try:
                relative = source.relative_to(root)
            except ValueError:
                continue
            current = root
            unsafe = False
            for part in relative.parts:
                current /= part
                if _is_link_like(current):
                    unsafe = True
                    break
            if not unsafe:
                allowed = True
                break
        if not allowed:
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_OUTSIDE_STAGING",
                "The staged artifact is outside every configured staging root or crosses a symbolic link.",
            )
        if not source.exists() or not source.is_file() or _is_link_like(source):
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "The staged artifact is unavailable or unsafe.",
            )
        source = source.resolve(strict=True)
        try:
            actual = _sha256_file(source)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_INVALID",
                "The staged artifact could not be read safely.",
            ) from exc
        if actual != declared:
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_HASH_MISMATCH",
                "The staged artifact SHA256 does not match its reference.",
            )
        return source, actual

    def _assert_expected_state(
        self,
        target: Path,
        *,
        expected_exists: bool,
        expected_sha256: str,
    ) -> None:
        exists, digest = self._file_state(target)
        if exists != bool(expected_exists) or (exists and digest != expected_sha256):
            raise CanonicalCommitError(
                "CANONICAL_CAS_MISMATCH",
                "The canonical destination no longer matches its explicit expected state.",
            )

    @staticmethod
    def _file_state(path: Path) -> tuple[bool, str]:
        if not _path_exists(path):
            return False, ""
        if _is_link_like(path):
            raise CanonicalCommitError(
                "CANONICAL_SYMLINK_REJECTED",
                "Symbolic links are forbidden at canonical destinations.",
            )
        if not path.is_file():
            raise CanonicalCommitError(
                "CANONICAL_DESTINATION_INVALID",
                "The canonical destination is not a regular file.",
            )
        try:
            return True, _sha256_file(path)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_DESTINATION_INVALID",
                "The canonical destination could not be read safely.",
            ) from exc

    @contextmanager
    def _commit_lock(self) -> Iterator[None]:
        assert self._control_root is not None
        lock_path = self._control_root / "canonical_writer.lock"
        deadline = time.monotonic() + self._lock_timeout_seconds
        while True:
            lock = FileLock(lock_path, stale_seconds=self._lock_stale_seconds)
            try:
                lock.__enter__()
            except RuntimeError as exc:
                if time.monotonic() >= deadline:
                    raise CanonicalCommitError(
                        "CANONICAL_COMMIT_BUSY",
                        "Another canonical writer owns the cross-process commit lock.",
                    ) from exc
                time.sleep(0.01)
                continue
            break
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def _commit_locked(
        self,
        *,
        commit_id: str,
        source: Path,
        staged_sha256: str,
        root_name: str,
        root: Path,
        target: Path,
        relative_path: str,
        expected_exists: bool,
        expected_sha256: str,
        request_digest: str,
    ) -> CanonicalCommitResult:
        assert self._control_root is not None
        transaction_key = hashlib.sha256(commit_id.encode("utf-8")).hexdigest()[:24]
        backup_dir = self._control_root / "backups" / transaction_key
        backup_path = backup_dir / "preimage.bin"
        temporary = target.parent / f".canonical-{transaction_key}-{uuid.uuid4().hex[:8]}.tmp"
        journal = {
            "schema_version": "1",
            "commit_id": commit_id,
            "request_digest": request_digest,
            "root_name": root_name,
            "relative_path": relative_path,
            "pre_exists": bool(expected_exists),
            "pre_sha256": expected_sha256,
            "post_sha256": staged_sha256,
            "backup_path": str(backup_path) if expected_exists else "",
            "temporary_path": str(temporary),
        }
        self._write_journal(journal)
        self._fault("after_journal_write", "CANONICAL_JOURNAL_WRITE_FAILED")

        if expected_exists:
            try:
                self._ensure_backups_root()
                backup_dir.mkdir(parents=True, exist_ok=False)
                if self._acl_hardener is not None:
                    self._acl_hardener(backup_dir)
                backup_sha = self._copy_exclusive(target, backup_path)
            except CanonicalCommitError:
                raise
            except Exception as exc:
                raise CanonicalCommitError(
                    "CANONICAL_BACKUP_FAILED",
                    "The existing canonical destination could not be backed up.",
                ) from exc
            if backup_sha != expected_sha256:
                raise CanonicalCommitError(
                    "CANONICAL_CAS_MISMATCH",
                    "The canonical destination changed while its backup was created.",
                )
            self._fault("after_backup", "CANONICAL_BACKUP_FAILED")

        try:
            copied_sha = self._copy_exclusive(source, temporary)
        except CanonicalCommitError:
            raise
        except Exception as exc:
            raise CanonicalCommitError(
                "CANONICAL_WRITE_FAILED",
                "The same-directory canonical temporary file could not be written.",
            ) from exc
        if copied_sha != staged_sha256:
            raise CanonicalCommitError(
                "CANONICAL_SOURCE_HASH_MISMATCH",
                "The staged artifact changed while the canonical temporary file was written.",
            )
        self._fault("after_temp_write", "CANONICAL_WRITE_FAILED")

        self._assert_expected_state(
            target,
            expected_exists=expected_exists,
            expected_sha256=expected_sha256,
        )
        self._fault("before_publish", "CANONICAL_PUBLISH_FAILED")
        try:
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_PUBLISH_FAILED",
                "The verified canonical temporary file could not be atomically published.",
            ) from exc
        self._fault("after_publish", "CANONICAL_PUBLISH_FAILED")

        post_exists, post_sha256 = self._file_state(target)
        if not post_exists or post_sha256 != staged_sha256:
            raise CanonicalCommitError(
                "CANONICAL_POST_VERIFY_FAILED",
                "The published canonical destination failed SHA256 verification.",
            )
        self._fault("after_post_verify", "CANONICAL_POST_VERIFY_FAILED")

        event = {
            "schema_version": "1",
            "event": "canonical_commit",
            "recorded_at": _utc_now(),
            "commit_id": commit_id,
            "request_digest": request_digest,
            "root_name": root_name,
            "relative_path": relative_path,
            "source_sha256": staged_sha256,
            "destination_pre_exists": bool(expected_exists),
            "destination_pre_sha256": expected_sha256,
            "destination_post_sha256": staged_sha256,
            "backup_sha256": expected_sha256 if expected_exists else "",
            "authorization_id": self._authorization.authorization_id,
        }
        self._fault("before_audit_write", "CANONICAL_AUDIT_WRITE_FAILED")
        try:
            self._append_audit(event)
        except CanonicalCommitError:
            raise
        except Exception as exc:
            raise CanonicalCommitError(
                "CANONICAL_AUDIT_WRITE_FAILED",
                "The compact canonical audit record could not be persisted.",
            ) from exc
        self._fault("after_audit_write", "CANONICAL_AUDIT_WRITE_FAILED")
        self._cleanup_active_journal(preserve_backup=True)
        return CanonicalCommitResult(
            commit_id=commit_id,
            root_name=root_name,
            relative_path=relative_path,
            sha256=staged_sha256,
            status="committed",
            idempotent=False,
            backup_sha256=expected_sha256 if expected_exists else "",
        )

    def _copy_exclusive(self, source: Path, destination: Path) -> str:
        digest = hashlib.sha256()
        created = False
        try:
            with source.open("rb") as reader, destination.open("xb") as writer:
                created = True
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if self._acl_hardener is not None:
                self._acl_hardener(destination)
            return digest.hexdigest()
        except Exception:
            try:
                if created and _path_exists(destination) and not _is_link_like(destination):
                    destination.unlink()
            except OSError:
                pass
            raise

    def _write_journal(self, journal: dict[str, Any]) -> None:
        assert self._control_root is not None
        path = self._control_root / "active_commit.json"
        if _path_exists(path):
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_REQUIRED",
                "An interrupted canonical transaction must be recovered first.",
            )
        try:
            self._atomic_write(path, (canonical_json(journal) + "\n").encode("utf-8"))
        except CanonicalCommitError:
            raise
        except Exception as exc:
            raise CanonicalCommitError(
                "CANONICAL_JOURNAL_WRITE_FAILED",
                "The canonical transaction journal could not be persisted.",
            ) from exc

    def _read_journal(self) -> dict[str, Any] | None:
        assert self._control_root is not None
        path = self._control_root / "active_commit.json"
        if not _path_exists(path):
            return None
        if _is_link_like(path) or not path.is_file():
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction journal is unsafe.",
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction journal is unreadable.",
            ) from exc
        required = {
            "schema_version",
            "commit_id",
            "request_digest",
            "root_name",
            "relative_path",
            "pre_exists",
            "pre_sha256",
            "post_sha256",
            "backup_path",
            "temporary_path",
        }
        if not isinstance(data, dict) or set(data) != required or data.get("schema_version") != "1":
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction journal has an unsupported schema.",
            )
        return data

    def _recover_active_locked(self) -> bool:
        journal = self._read_journal()
        if journal is None:
            return False
        root_name = str(journal["root_name"])
        _root, target, relative_path = self._destination(root_name, str(journal["relative_path"]))
        if relative_path != journal["relative_path"]:
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction destination is not normalized.",
            )
        commit_id = str(journal["commit_id"])
        self._validate_commit_id(commit_id)
        request_digest = str(journal["request_digest"])
        pre_sha256 = str(journal["pre_sha256"])
        post_sha256 = str(journal["post_sha256"])
        if not isinstance(journal["pre_exists"], bool):
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction existence expectation is invalid.",
            )
        pre_exists = journal["pre_exists"]
        if (
            not _SHA256.fullmatch(post_sha256)
            or (pre_exists and not _SHA256.fullmatch(pre_sha256))
            or (not pre_exists and bool(pre_sha256))
        ):
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction hashes are invalid.",
            )
        if not _SHA256.fullmatch(request_digest):
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction request digest is invalid.",
            )
        recovered_request = {
            "root_name": root_name,
            "relative_path": relative_path,
            "staged_sha256": post_sha256,
            "expected_exists": pre_exists,
            "expected_sha256": pre_sha256,
        }
        recovered_digest = hashlib.sha256(
            canonical_json(recovered_request).encode("utf-8")
        ).hexdigest()
        if recovered_digest != request_digest:
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction journal does not match its request digest.",
            )
        self._validate_journal_paths(journal, target)

        committed = self._record_for_commit(self._read_audit(), commit_id)
        if committed is not None:
            if committed.get("request_digest") != request_digest:
                raise CanonicalCommitError(
                    "CANONICAL_COMMIT_ID_CONFLICT",
                    "The interrupted commit_id is bound to another request.",
                )
            current_exists, current_sha256 = self._file_state(target)
            if not current_exists or current_sha256 != post_sha256:
                raise CanonicalCommitError(
                    "CANONICAL_POST_COMMIT_DRIFT",
                    "An audited canonical destination no longer matches its committed SHA256.",
                )
            try:
                self._cleanup_active_journal(preserve_backup=True)
            except CanonicalCommitError as cleanup_exc:
                raise CanonicalCommitError(
                    cleanup_exc.code,
                    "Canonical data and audit are committed, but recovery cleanup failed.",
                    write_state="committed",
                ) from cleanup_exc
            return True

        current_exists, current_sha256 = self._file_state(target)
        already_pre = current_exists == pre_exists and (
            not pre_exists or current_sha256 == pre_sha256
        )
        if not already_pre:
            if not current_exists or current_sha256 != post_sha256:
                raise CanonicalCommitError(
                    "CANONICAL_RECOVERY_CONFLICT",
                    "The interrupted destination has an unknown state; recovery stopped without overwrite.",
                )
            if pre_exists:
                backup = Path(str(journal["backup_path"]))
                if (
                    not backup.is_absolute()
                    or not backup.exists()
                    or not backup.is_file()
                    or _is_link_like(backup)
                    or _sha256_file(backup) != pre_sha256
                ):
                    raise CanonicalCommitError(
                        "CANONICAL_ROLLBACK_FAILED",
                        "The canonical pre-image backup is unavailable or invalid.",
                    )
                restore_temp = target.parent / f".canonical-restore-{uuid.uuid4().hex[:12]}.tmp"
                try:
                    restored_sha = self._copy_exclusive(backup, restore_temp)
                    if restored_sha != pre_sha256:
                        raise CanonicalCommitError(
                            "CANONICAL_ROLLBACK_FAILED",
                            "The canonical pre-image backup changed during restore.",
                        )
                    os.replace(restore_temp, target)
                    self._fsync_directory(target.parent)
                except Exception as exc:
                    if isinstance(exc, CanonicalCommitError):
                        raise
                    raise CanonicalCommitError(
                        "CANONICAL_ROLLBACK_FAILED",
                        "The canonical pre-image could not be atomically restored.",
                    ) from exc
            else:
                try:
                    target.unlink()
                    self._fsync_directory(target.parent)
                except OSError as exc:
                    raise CanonicalCommitError(
                        "CANONICAL_ROLLBACK_FAILED",
                        "The newly created canonical destination could not be rolled back.",
                    ) from exc

        restored_exists, restored_sha256 = self._file_state(target)
        if restored_exists != pre_exists or (pre_exists and restored_sha256 != pre_sha256):
            raise CanonicalCommitError(
                "CANONICAL_ROLLBACK_FAILED",
                "The canonical destination did not return to its expected pre-image.",
            )
        self._cleanup_active_journal(preserve_backup=False)
        return True

    def _validate_journal_paths(self, journal: dict[str, Any], target: Path) -> None:
        assert self._control_root is not None
        commit_id = str(journal.get("commit_id") or "")
        self._validate_commit_id(commit_id)
        transaction_key = hashlib.sha256(commit_id.encode("utf-8")).hexdigest()[:24]
        temporary = Path(str(journal["temporary_path"]))
        temporary_pattern = re.compile(
            rf"^\.canonical-{re.escape(transaction_key)}-[0-9a-f]{{8}}\.tmp$"
        )
        if (
            not temporary.is_absolute()
            or temporary.parent != target.parent
            or temporary_pattern.fullmatch(temporary.name) is None
        ):
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction temporary path is outside its destination directory.",
            )
        backup_raw = str(journal.get("backup_path") or "")
        if not bool(journal["pre_exists"]):
            if backup_raw:
                raise CanonicalCommitError(
                    "CANONICAL_RECOVERY_FAILED",
                    "A new-file transaction cannot name a pre-image backup.",
                )
            return
        backup = Path(backup_raw)
        expected_backup = self._control_root / "backups" / transaction_key / "preimage.bin"
        if (
            not backup.is_absolute()
            or backup != expected_backup
            or self._path_crosses_symlink(self._control_root, backup)
        ):
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical pre-image backup path is invalid.",
            )

    def _ensure_backups_root(self) -> None:
        assert self._control_root is not None
        backups_root = self._control_root / "backups"
        if _path_exists(backups_root) and (
            _is_link_like(backups_root) or not backups_root.is_dir()
        ):
            raise CanonicalCommitError(
                "CANONICAL_BACKUP_FAILED",
                "The canonical backup namespace is unsafe.",
            )
        try:
            backups_root.mkdir(exist_ok=True)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_BACKUP_FAILED",
                "The canonical backup namespace is unavailable.",
            ) from exc
        if _is_link_like(backups_root) or not backups_root.is_dir():
            raise CanonicalCommitError(
                "CANONICAL_BACKUP_FAILED",
                "The canonical backup namespace is unsafe.",
            )
        if self._acl_hardener is not None:
            try:
                self._acl_hardener(backups_root)
            except Exception as exc:
                raise CanonicalCommitError(
                    "CANONICAL_BACKUP_FAILED",
                    "The canonical backup namespace could not be hardened.",
                ) from exc

    @staticmethod
    def _path_crosses_symlink(root: Path, path: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return True
        current = root
        for part in relative.parts:
            current /= part
            if _is_link_like(current):
                return True
        return False

    def _cleanup_active_journal(self, *, preserve_backup: bool) -> None:
        assert self._control_root is not None
        journal = self._read_journal()
        if journal is None:
            return
        _root, target, relative_path = self._destination(
            str(journal["root_name"]), str(journal["relative_path"])
        )
        if relative_path != journal["relative_path"]:
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The canonical transaction destination is not normalized.",
            )
        self._validate_journal_paths(journal, target)
        temporary = Path(str(journal["temporary_path"]))
        if _path_exists(temporary):
            if _is_link_like(temporary):
                raise CanonicalCommitError(
                    "CANONICAL_RECOVERY_FAILED",
                    "The canonical temporary path became a symbolic link.",
                )
            try:
                temporary.unlink()
            except OSError as exc:
                raise CanonicalCommitError(
                    "CANONICAL_RECOVERY_FAILED",
                    "The canonical temporary file could not be cleaned safely.",
                ) from exc
        if not preserve_backup and str(journal.get("backup_path") or ""):
            backup = Path(str(journal["backup_path"]))
            try:
                if _path_exists(backup):
                    if _is_link_like(backup):
                        raise CanonicalCommitError(
                            "CANONICAL_RECOVERY_FAILED",
                            "The canonical backup path became a symbolic link.",
                        )
                    backup.unlink()
                backup.parent.rmdir()
                backups_root = backup.parent.parent
                if backups_root.exists() and not any(backups_root.iterdir()):
                    backups_root.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise CanonicalCommitError(
                    "CANONICAL_RECOVERY_FAILED",
                    "The interrupted canonical backup could not be cleaned safely.",
                ) from exc
        journal_path = self._control_root / "active_commit.json"
        try:
            journal_path.unlink()
            self._fsync_directory(self._control_root)
        except OSError as exc:
            raise CanonicalCommitError(
                "CANONICAL_RECOVERY_FAILED",
                "The completed canonical transaction journal could not be removed.",
            ) from exc

    def _read_audit(self) -> list[dict[str, Any]]:
        assert self._control_root is not None
        path = self._control_root / "canonical_audit.jsonl"
        if not _path_exists(path):
            return []
        if _is_link_like(path) or not path.is_file():
            raise CanonicalCommitError(
                "CANONICAL_AUDIT_CORRUPT",
                "The canonical audit ledger is unsafe.",
            )
        try:
            raw = path.read_text(encoding="utf-8")
            lines = raw.splitlines()
            records = [json.loads(line) for line in lines]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CanonicalCommitError(
                "CANONICAL_AUDIT_CORRUPT",
                "The canonical audit ledger cannot be parsed safely.",
            ) from exc
        required = {
            "schema_version",
            "event",
            "recorded_at",
            "commit_id",
            "request_digest",
            "root_name",
            "relative_path",
            "source_sha256",
            "destination_pre_exists",
            "destination_pre_sha256",
            "destination_post_sha256",
            "backup_sha256",
            "authorization_id",
        }
        seen: set[str] = set()
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != required
                or record.get("schema_version") != "1"
                or record.get("event") != "canonical_commit"
            ):
                raise CanonicalCommitError(
                    "CANONICAL_AUDIT_CORRUPT",
                    "The canonical audit ledger contains an unsupported record.",
                )
            commit_id = str(record.get("commit_id") or "")
            if not _SAFE_NAME.fullmatch(commit_id) or commit_id in seen:
                raise CanonicalCommitError(
                    "CANONICAL_AUDIT_CORRUPT",
                    "The canonical audit ledger contains duplicate or invalid commit IDs.",
                )
            source_sha256 = str(record.get("source_sha256") or "")
            post_sha256 = str(record.get("destination_post_sha256") or "")
            pre_sha256 = str(record.get("destination_pre_sha256") or "")
            backup_sha256 = str(record.get("backup_sha256") or "")
            pre_exists = record.get("destination_pre_exists")
            if (
                not _SHA256.fullmatch(str(record.get("request_digest") or ""))
                or not _SHA256.fullmatch(source_sha256)
                or post_sha256 != source_sha256
                or not isinstance(pre_exists, bool)
                or (pre_exists and not _SHA256.fullmatch(pre_sha256))
                or (not pre_exists and bool(pre_sha256))
                or backup_sha256 != pre_sha256
                or not _SAFE_NAME.fullmatch(str(record.get("root_name") or ""))
                or not _SAFE_NAME.fullmatch(str(record.get("authorization_id") or ""))
                or not str(record.get("relative_path") or "")
                or not str(record.get("recorded_at") or "")
            ):
                raise CanonicalCommitError(
                    "CANONICAL_AUDIT_CORRUPT",
                    "The canonical audit ledger contains invalid hashes or metadata.",
                )
            seen.add(commit_id)
        return records

    @staticmethod
    def _record_for_commit(
        records: list[dict[str, Any]], commit_id: str
    ) -> dict[str, Any] | None:
        return next((item for item in records if item.get("commit_id") == commit_id), None)

    def _append_audit(self, event: dict[str, Any]) -> None:
        assert self._control_root is not None
        path = self._control_root / "canonical_audit.jsonl"
        records = self._read_audit()
        payload = b"".join(
            (canonical_json(record) + "\n").encode("utf-8") for record in [*records, event]
        )
        try:
            self._atomic_write(path, payload)
        except Exception as exc:
            if isinstance(exc, CanonicalCommitError):
                raise
            raise CanonicalCommitError(
                "CANONICAL_AUDIT_WRITE_FAILED",
                "The compact canonical audit record could not be persisted.",
            ) from exc

    def _idempotent_result(
        self,
        *,
        existing: dict[str, Any],
        request_digest: str,
        target: Path,
        commit_id: str,
        root_name: str,
        relative_path: str,
    ) -> CanonicalCommitResult:
        if existing.get("request_digest") != request_digest:
            raise CanonicalCommitError(
                "CANONICAL_COMMIT_ID_CONFLICT",
                "The canonical commit_id is already bound to a different request.",
            )
        exists, digest = self._file_state(target)
        expected_post = str(existing.get("destination_post_sha256") or "")
        if not exists or digest != expected_post:
            raise CanonicalCommitError(
                "CANONICAL_POST_COMMIT_DRIFT",
                "The audited canonical destination no longer matches its committed SHA256.",
            )
        return CanonicalCommitResult(
            commit_id=commit_id,
            root_name=root_name,
            relative_path=relative_path,
            sha256=digest,
            status="already_committed",
            idempotent=True,
            backup_sha256=str(existing.get("backup_sha256") or ""),
        )

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        if _is_link_like(path):
            raise CanonicalCommitError(
                "CANONICAL_CONTROL_INVALID",
                "A canonical control file cannot be a symbolic link.",
            )
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        created = False
        try:
            with temporary.open("xb") as handle:
                created = True
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self._acl_hardener is not None:
                self._acl_hardener(temporary)
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                if created and _path_exists(temporary) and not _is_link_like(temporary):
                    temporary.unlink()
            except OSError:
                pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fault(self, point: str, error_code: str) -> None:
        if self._fault_injector is None:
            return
        try:
            self._fault_injector(point)
        except Exception as exc:
            raise CanonicalCommitError(
                error_code,
                f"Canonical commit fault at {point}; no alternate writer was attempted.",
            ) from exc
