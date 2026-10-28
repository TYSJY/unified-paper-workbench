"""Immutable, content-addressed user Prompt resources.

Built-in and legacy Prompt files are never edited or copied in place. A user
edit creates a new UTF-8 blob under the generic workflow state directory and a
new :class:`PromptRef`/runtime :class:`ResourceBinding` pair. Construction and
import are side-effect free; callers must explicitly initialize and write.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .contracts import PromptRef, ResourceBinding, is_sha256
from .validation import is_logical_id


MAX_PROMPT_BYTES = 2 * 1024 * 1024
USER_PROMPT_RESOURCE_PREFIX = "user.prompt.sha256."


class PromptResourceError(RuntimeError):
    """A Prompt blob path or immutable content check failed."""


def prompt_resource_directory(workspace_root: str | Path) -> Path:
    """Return the canonical user Prompt directory without creating it."""

    return Path(workspace_root).expanduser().absolute() / "state" / "v2" / "resources" / "prompts"


def _is_reparse_or_link(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse and attributes & reparse)


def _metadata(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Prompt blob write made no progress")
        view = view[written:]


@dataclass(frozen=True)
class PromptResourceRecord:
    resource_id: str
    content_sha256: str
    version: str
    absolute_path: str
    size_bytes: int

    def __post_init__(self) -> None:
        if self.resource_id != f"{USER_PROMPT_RESOURCE_PREFIX}{self.content_sha256}":
            raise ValueError("Prompt resource ID must be derived from its content digest")
        if not is_sha256(self.content_sha256):
            raise ValueError("Prompt resource digest must be SHA-256")
        if not Path(self.absolute_path).is_absolute():
            raise ValueError("Prompt resource path must be absolute")
        if not is_logical_id(self.version):
            raise ValueError("Prompt resource version must be a logical ID")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("Prompt resource size must be a positive integer")

    @property
    def prompt_ref(self) -> PromptRef:
        return PromptRef(self.resource_id, self.content_sha256, self.version)

    @property
    def runtime_binding(self) -> ResourceBinding:
        return ResourceBinding(self.resource_id, self.absolute_path, self.content_sha256)


class ContentAddressedPromptStore:
    """Explicit writer/reader for immutable user Prompt blobs."""

    def __init__(self, root: str | Path) -> None:
        if not isinstance(root, (str, Path)):
            raise TypeError("Prompt resource root must be a filesystem path")
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("Prompt resource root must be absolute")
        suffix = tuple(part.casefold() for part in self.root.parts[-4:])
        if suffix != ("state", "v2", "resources", "prompts"):
            raise ValueError("Prompt resource root must end with state/v2/resources/prompts")

    def _require_safe_path(self, path: Path, *, require_existing: bool) -> None:
        if path != self.root and path.parent != self.root:
            raise PromptResourceError("Prompt resource path escaped its canonical directory")
        for candidate in tuple(reversed(path.parents)) + (path,):
            metadata = _metadata(candidate)
            if metadata is None:
                continue
            if _is_reparse_or_link(metadata):
                raise PromptResourceError("Prompt resource symlink or reparse paths are not permitted")
            if candidate == path and path != self.root:
                if not stat.S_ISREG(metadata.st_mode):
                    raise PromptResourceError("Prompt resource target must be a regular file")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise PromptResourceError("Prompt resource ancestor must be a directory")
        expected_exists = path.is_dir() if path == self.root else path.is_file()
        if require_existing and not expected_exists:
            raise PromptResourceError("Prompt resource has not been initialized or does not exist")

    def initialize(self) -> None:
        self._require_safe_path(self.root, require_existing=False)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PromptResourceError(f"Prompt resource initialization failed ({type(exc).__name__})") from exc
        self._require_safe_path(self.root, require_existing=True)

    @staticmethod
    def _content(text: str) -> bytes:
        if not isinstance(text, str):
            raise TypeError("Prompt text must be a string")
        if not text.strip():
            raise ValueError("Prompt text cannot be empty")
        if "\x00" in text:
            raise ValueError("Prompt text cannot contain NUL")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_PROMPT_BYTES:
            raise ValueError(f"Prompt text exceeds {MAX_PROMPT_BYTES} bytes")
        return encoded

    def _path(self, digest: str) -> Path:
        if not is_sha256(digest):
            raise ValueError("Prompt resource digest must be SHA-256")
        return self.root / f"{digest.casefold()}.prompt"

    def _read_verified(self, path: Path, digest: str) -> bytes:
        self._require_safe_path(path, require_existing=True)
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 128 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = os.lstat(path)
        def identity(value: os.stat_result) -> tuple[int, int, int, int]:
            return (int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size))

        if (
            identity(before) != identity(opened)
            or identity(opened) != identity(after)
            or identity(after) != identity(final)
        ):
            raise PromptResourceError("Prompt resource changed while it was read")
        content = b"".join(chunks)
        if hashlib.sha256(content).hexdigest() != digest.casefold():
            raise PromptResourceError("Prompt resource content does not match its immutable digest")
        return content

    def put(self, text: str, *, version: str = "") -> PromptResourceRecord:
        """Publish exact UTF-8 bytes once; an existing digest is only verified."""

        content = self._content(text)
        digest = hashlib.sha256(content).hexdigest()
        target = self._path(digest)
        self._require_safe_path(self.root, require_existing=True)
        if target.exists():
            self._read_verified(target, digest)
            return self.record(digest, version=version)

        temporary = self.root / f".{digest}.{secrets.token_hex(12)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            self._read_verified(target, digest)
        except OSError as exc:
            raise PromptResourceError(f"Prompt resource write failed ({type(exc).__name__})") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # A unique, non-addressable temp file cannot become a runtime binding.
                pass
        return self.record(digest, version=version)

    def record(self, digest: str, *, version: str = "") -> PromptResourceRecord:
        path = self._path(digest)
        content = self._read_verified(path, digest)
        return PromptResourceRecord(
            resource_id=f"{USER_PROMPT_RESOURCE_PREFIX}{digest.casefold()}",
            content_sha256=digest.casefold(),
            version=version or digest.casefold()[:12],
            absolute_path=str(path),
            size_bytes=len(content),
        )

    def read_text(self, prompt_ref: PromptRef) -> str:
        if not isinstance(prompt_ref, PromptRef):
            raise TypeError("prompt_ref must be a PromptRef")
        expected_resource_id = f"{USER_PROMPT_RESOURCE_PREFIX}{prompt_ref.content_sha256.casefold()}"
        if prompt_ref.resource_id != expected_resource_id:
            raise PromptResourceError("PromptRef does not identify this content-addressed store")
        try:
            return self._read_verified(
                self._path(prompt_ref.content_sha256),
                prompt_ref.content_sha256,
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PromptResourceError("Prompt resource is not valid UTF-8") from exc


__all__ = [
    "ContentAddressedPromptStore",
    "MAX_PROMPT_BYTES",
    "PromptResourceError",
    "PromptResourceRecord",
    "USER_PROMPT_RESOURCE_PREFIX",
    "prompt_resource_directory",
]
