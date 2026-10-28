from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unified_paper_orchestrator.io_utils import FileLock
from unified_paper_orchestrator.windows_security import restrict_private_path

from .contracts import ArtifactRef, canonical_json


_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_TRANSACTION = re.compile(r"^\.txn_task_[0-9a-f]{32}_[0-9a-f]{12}$")
_EXPECTED_FILES = frozenset({"review_round_1.md", "result.json"})


class StagingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_link_like(path: Path) -> bool:
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


def _durable_write_new(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StagingError(
            "STAGING_WRITE_FAILED",
            f"Unable to persist staging artifact: {path.name}",
        ) from exc


class StagingStore:
    """Single writer and verifier for non-canonical multi-executor artifacts."""

    def __init__(
        self,
        root: Path,
        *,
        acl_hardener: Callable[[Path], None] | None = restrict_private_path,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        if _path_has_link_component(self.root):
            raise StagingError(
                "STAGING_ROOT_UNSAFE",
                "Staging root crosses a symbolic link or Windows reparse point.",
            )
        self.acl_hardener = acl_hardener
        self.lock_timeout_seconds = max(0.0, float(lock_timeout_seconds))

    def task_dir(self, run_id: str, task_id: str) -> Path:
        if _RUN_ID.fullmatch(str(run_id or "")) is None:
            raise StagingError("STAGING_ID_INVALID", "Invalid run_id for staging.")
        if _TASK_ID.fullmatch(str(task_id or "")) is None:
            raise StagingError("STAGING_ID_INVALID", "Invalid task_id for staging.")
        target = Path(os.path.abspath(os.fspath(self.root / run_id / task_id)))
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise StagingError(
                "STAGING_PATH_ESCAPE",
                "Staging path escapes the configured root.",
            ) from exc
        if _path_has_link_component(target):
            raise StagingError(
                "STAGING_REPARSE_REJECTED",
                "Staging path crosses a symbolic link or Windows reparse point.",
            )
        return target

    def ensure_root(self) -> Path:
        """Create and harden the configured private root before execution."""

        self._ensure_root()
        return self.root

    def persist_review(
        self,
        *,
        run_id: str,
        task_id: str,
        markdown: str,
        manifest: dict[str, Any],
    ) -> tuple[list[ArtifactRef], dict[str, Any]]:
        target = self.task_dir(run_id, task_id)
        review_bytes, manifest_bytes, compact_manifest = self._serialize_review(
            run_id=run_id,
            task_id=task_id,
            target=target,
            markdown=markdown,
            manifest=manifest,
        )
        self._ensure_root()
        with self._task_lock(run_id, task_id):
            if _path_exists(target):
                outputs, observed_manifest, observed_markdown = self._load_review_locked(
                    run_id,
                    task_id,
                )
                if (
                    observed_markdown.encode("utf-8") != review_bytes
                    or canonical_json(observed_manifest).encode("utf-8") + b"\n"
                    != manifest_bytes
                ):
                    raise StagingError(
                        "STAGING_TARGET_CONFLICT",
                        "Existing staging target is bound to different content.",
                    )
                return outputs, observed_manifest

            self._ensure_parent(target.parent)
            temporary = target.parent / f".txn_{task_id}_{uuid.uuid4().hex[:12]}"
            published = False
            try:
                temporary.mkdir(exist_ok=False)
                self._assert_safe_directory(temporary, "STAGING_TRANSACTION_UNSAFE")
                if self.acl_hardener is not None:
                    self.acl_hardener(temporary)
                _durable_write_new(temporary / "review_round_1.md", review_bytes)
                _durable_write_new(temporary / "result.json", manifest_bytes)
                self._verify_pair_bytes(
                    temporary,
                    expected_review=review_bytes,
                    expected_manifest=manifest_bytes,
                )
                os.replace(temporary, target)
                published = True
                self._fsync_directory(target.parent)
                self._verify_pair_bytes(
                    target,
                    expected_review=review_bytes,
                    expected_manifest=manifest_bytes,
                )
            except StagingError:
                raise
            except Exception as exc:
                raise StagingError(
                    "STAGING_PUBLISH_FAILED",
                    "Unable to publish the verified staging pair.",
                ) from exc
            finally:
                if not published and _path_exists(temporary):
                    self._safe_remove_transaction(temporary)

            outputs = self._artifact_refs(target, review_bytes, manifest_bytes)
            return outputs, compact_manifest

    def load_review(
        self,
        run_id: str,
        task_id: str,
    ) -> tuple[list[ArtifactRef], dict[str, Any], str]:
        self._ensure_root()
        with self._task_lock(run_id, task_id):
            return self._load_review_locked(run_id, task_id)

    def review_exists(self, run_id: str, task_id: str) -> bool:
        target = self.task_dir(run_id, task_id)
        if not _path_exists(self.root):
            return False
        self._assert_safe_directory(self.root, "STAGING_ROOT_UNSAFE")
        if not _path_exists(target):
            return False
        self._assert_safe_directory(target, "STAGING_TARGET_UNSAFE")
        return True

    def _load_review_locked(
        self,
        run_id: str,
        task_id: str,
    ) -> tuple[list[ArtifactRef], dict[str, Any], str]:
        target = self.task_dir(run_id, task_id)
        if not _path_exists(target):
            raise StagingError(
                "STAGING_TARGET_MISSING",
                "Staging review target does not exist.",
            )
        self._assert_safe_directory(target, "STAGING_TARGET_UNSAFE")
        names = self._directory_names(target)
        if names != _EXPECTED_FILES:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging review target contains unexpected files.",
            )
        review_path = target / "review_round_1.md"
        manifest_path = target / "result.json"
        review_bytes = self._read_regular_file(review_path)
        manifest_bytes = self._read_regular_file(manifest_path)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging result manifest is not valid UTF-8 JSON.",
            ) from exc
        if (
            not isinstance(manifest, dict)
            or canonical_json(manifest).encode("utf-8") + b"\n" != manifest_bytes
        ):
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging result manifest is not canonical JSON.",
            )
        self._validate_manifest_binding(
            manifest,
            run_id=run_id,
            task_id=task_id,
            target=target,
            review_bytes=review_bytes,
        )
        try:
            markdown = review_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging review is not valid UTF-8 Markdown.",
            ) from exc
        return self._artifact_refs(target, review_bytes, manifest_bytes), manifest, markdown

    def _serialize_review(
        self,
        *,
        run_id: str,
        task_id: str,
        target: Path,
        markdown: str,
        manifest: dict[str, Any],
    ) -> tuple[bytes, bytes, dict[str, Any]]:
        try:
            review_bytes = markdown.encode("utf-8")
            review_sha256 = _sha256(review_bytes)
            compact_manifest = dict(manifest)
            compact_manifest["output"] = {
                "path": str(target / "review_round_1.md"),
                "sha256": review_sha256,
                "bytes": len(review_bytes),
            }
            manifest_bytes = (canonical_json(compact_manifest) + "\n").encode("utf-8")
        except (UnicodeError, TypeError, ValueError) as exc:
            raise StagingError(
                "STAGING_SERIALIZATION_FAILED",
                "Staging review or manifest could not be serialized safely.",
            ) from exc
        self._validate_manifest_binding(
            compact_manifest,
            run_id=run_id,
            task_id=task_id,
            target=target,
            review_bytes=review_bytes,
        )
        return review_bytes, manifest_bytes, compact_manifest

    def _validate_manifest_binding(
        self,
        manifest: dict[str, Any],
        *,
        run_id: str,
        task_id: str,
        target: Path,
        review_bytes: bytes,
    ) -> None:
        output = manifest.get("output")
        if not isinstance(output, dict):
            raise StagingError(
                "STAGING_BINDING_INVALID",
                "Staging manifest output binding is missing.",
            )
        if (
            manifest.get("run_id") != run_id
            or manifest.get("task_id") != task_id
            or output.get("path") != str(target / "review_round_1.md")
            or output.get("sha256") != _sha256(review_bytes)
            or output.get("bytes") != len(review_bytes)
        ):
            raise StagingError(
                "STAGING_BINDING_INVALID",
                "Staging manifest does not match its task and review bytes.",
            )

    def _verify_pair_bytes(
        self,
        directory: Path,
        *,
        expected_review: bytes,
        expected_manifest: bytes,
    ) -> None:
        self._assert_safe_directory(directory, "STAGING_TARGET_UNSAFE")
        if self._directory_names(directory) != _EXPECTED_FILES:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging pair contains unexpected files.",
            )
        if self._read_regular_file(directory / "review_round_1.md") != expected_review:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Published staging review bytes changed.",
            )
        if self._read_regular_file(directory / "result.json") != expected_manifest:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Published staging manifest bytes changed.",
            )

    def _read_regular_file(self, path: Path) -> bytes:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise StagingError(
                "STAGING_PATH_ESCAPE",
                "Staging file escapes the configured root.",
            ) from exc
        if _path_has_link_component(path) or not path.is_file():
            raise StagingError(
                "STAGING_REPARSE_REJECTED",
                "Staging artifact is not a safe regular file.",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise StagingError(
                        "STAGING_REPARSE_REJECTED",
                        "Staging artifact is not a regular file.",
                    )
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
            finally:
                os.close(descriptor)
            current = os.stat(path, follow_symlinks=False)
        except StagingError:
            raise
        except OSError as exc:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging artifact could not be read safely.",
            ) from exc
        if _path_has_link_component(path) or not stat.S_ISREG(current.st_mode):
            raise StagingError(
                "STAGING_REPARSE_REJECTED",
                "Staging artifact changed identity during verification.",
            )
        if opened.st_ino and current.st_ino and (
            opened.st_dev != current.st_dev or opened.st_ino != current.st_ino
        ):
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging artifact changed identity during verification.",
            )
        return b"".join(chunks)

    def _directory_names(self, directory: Path) -> frozenset[str]:
        self._assert_safe_directory(directory, "STAGING_TARGET_UNSAFE")
        try:
            names = frozenset(item.name for item in directory.iterdir())
        except OSError as exc:
            raise StagingError(
                "STAGING_VERIFY_FAILED",
                "Staging directory could not be enumerated safely.",
            ) from exc
        self._assert_safe_directory(directory, "STAGING_TARGET_UNSAFE")
        return names

    def _ensure_root(self) -> None:
        if _path_has_link_component(self.root):
            raise StagingError(
                "STAGING_ROOT_UNSAFE",
                "Staging root crosses a symbolic link or Windows reparse point.",
            )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StagingError(
                "STAGING_ROOT_UNAVAILABLE",
                "Staging root could not be created.",
            ) from exc
        self._assert_safe_directory(self.root, "STAGING_ROOT_UNSAFE")
        if self.acl_hardener is not None:
            try:
                self.acl_hardener(self.root)
            except Exception as exc:
                raise StagingError(
                    "STAGING_ACL_FAILED",
                    "Unable to establish private staging root.",
                ) from exc

    def _ensure_parent(self, parent: Path) -> None:
        try:
            parent.relative_to(self.root)
            parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            raise StagingError(
                "STAGING_PATH_ESCAPE",
                "Staging task parent is unavailable or outside the root.",
            ) from exc
        self._assert_safe_directory(parent, "STAGING_REPARSE_REJECTED")
        if self.acl_hardener is not None:
            try:
                self.acl_hardener(parent)
            except Exception as exc:
                raise StagingError(
                    "STAGING_ACL_FAILED",
                    "Unable to establish private staging task parent.",
                ) from exc

    def _assert_safe_directory(self, path: Path, code: str) -> None:
        if not path.is_dir() or _path_has_link_component(path):
            raise StagingError(
                code,
                "Staging directory is unavailable or crosses a reparse point.",
            )

    @contextmanager
    def _task_lock(self, run_id: str, task_id: str) -> Iterator[None]:
        self.task_dir(run_id, task_id)
        lock_path = self.root / f".{run_id}.{task_id}.lock"
        if _path_has_link_component(lock_path):
            raise StagingError(
                "STAGING_LOCK_UNSAFE",
                "Staging lock path crosses a reparse point.",
            )
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            lock = FileLock(lock_path, stale_seconds=0)
            try:
                lock.__enter__()
            except RuntimeError as exc:
                if time.monotonic() >= deadline:
                    raise StagingError(
                        "STAGING_TARGET_BUSY",
                        "Another process owns this staging task transaction.",
                    ) from exc
                time.sleep(0.01)
                continue
            break
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def _safe_remove_transaction(self, temporary: Path) -> None:
        if temporary.parent.parent != self.root or _TRANSACTION.fullmatch(temporary.name) is None:
            return
        if _is_link_like(temporary):
            try:
                if temporary.is_dir():
                    temporary.rmdir()
                else:
                    temporary.unlink()
            except OSError:
                pass
            return
        if not temporary.is_dir() or _path_has_link_component(temporary):
            return
        try:
            entries = list(temporary.iterdir())
            if any(item.name not in _EXPECTED_FILES for item in entries):
                return
            for item in entries:
                if _is_link_like(item) or not item.is_file():
                    return
            for item in entries:
                item.unlink()
            temporary.rmdir()
        except OSError:
            return

    @staticmethod
    def _artifact_refs(
        target: Path,
        review_bytes: bytes,
        manifest_bytes: bytes,
    ) -> list[ArtifactRef]:
        return [
            ArtifactRef(
                uri=str(target / "review_round_1.md"),
                sha256=_sha256(review_bytes),
                media_type="text/markdown",
            ),
            ArtifactRef(
                uri=str(target / "result.json"),
                sha256=_sha256(manifest_bytes),
                media_type="application/json",
            ),
        ]

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
