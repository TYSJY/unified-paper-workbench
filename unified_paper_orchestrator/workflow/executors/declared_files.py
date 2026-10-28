from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from ..artifacts import ProducedArtifact
from ..contracts import NodeDefinition, NodeType
from .base import ExecutionContext
from .codex_sdk import CodexResponse


DECLARED_OUTPUT_FILES_METADATA_KEY = "declared_output_files"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class DeclaredOutputCollectionError(ValueError):
    """A node's portable output map or produced file set is unsafe."""


def _portable_relative_parts(value: object, artifact_id: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise DeclaredOutputCollectionError(f"declared output path is invalid: {artifact_id}")
    if "\\" in value or ":" in value:
        raise DeclaredOutputCollectionError(f"declared output path is not portable: {artifact_id}")
    relative = PurePosixPath(value)
    parts = relative.parts
    if (
        relative.is_absolute()
        or value != relative.as_posix()
        or not parts
        or any(
            part in {"", ".", ".."}
            or len(part) > 255
            or part.endswith((" ", "."))
            or any(character in '<>"\\|?*' or unicodedata.category(character).startswith("C") for character in part)
            for part in parts
        )
    ):
        raise DeclaredOutputCollectionError(f"declared output path is not canonical: {artifact_id}")
    for part in parts:
        normalized_part = unicodedata.normalize("NFKC", part)
        if any(character in '<>:"/\\|?*' or unicodedata.category(character).startswith("C") for character in normalized_part):
            raise DeclaredOutputCollectionError(f"declared output path is not portable: {artifact_id}")
        device_name = normalized_part.split(".", 1)[0].upper()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise DeclaredOutputCollectionError(f"declared output path uses a reserved name: {artifact_id}")
    return parts


def declared_output_files(node: NodeDefinition) -> Mapping[str, str]:
    """Validate and freeze the portable artifact-to-filename map for one node."""

    if not isinstance(node, NodeDefinition):
        raise TypeError("node must be a NodeDefinition")
    if node.node_type is not NodeType.CODEX:
        raise DeclaredOutputCollectionError("declared file collection is available only for Codex nodes")
    raw = node.metadata.get(DECLARED_OUTPUT_FILES_METADATA_KEY)
    if not isinstance(raw, Mapping):
        raise DeclaredOutputCollectionError("Codex node metadata must contain a declared_output_files object")
    output_specs = {spec.artifact_id: spec for spec in node.outputs}
    if any(not isinstance(key, str) for key in raw):
        raise DeclaredOutputCollectionError("declared_output_files keys must be strings")
    if set(raw) != set(output_specs):
        raise DeclaredOutputCollectionError("declared_output_files keys must exactly match node output IDs")
    frozen: dict[str, str] = {}
    normalized_paths: list[tuple[str, ...]] = []
    for artifact_id in sorted(output_specs):
        parts = _portable_relative_parts(raw[artifact_id], artifact_id)
        portable = PurePosixPath(*parts).as_posix()
        normalized_parts = tuple(unicodedata.normalize("NFKC", part).casefold() for part in parts)
        if any(
            normalized_parts == prior
            or normalized_parts[: len(prior)] == prior
            or prior[: len(normalized_parts)] == normalized_parts
            for prior in normalized_paths
        ):
            raise DeclaredOutputCollectionError("declared output paths must be unique across platforms")
        normalized_paths.append(normalized_parts)
        frozen[artifact_id] = portable
    return MappingProxyType(frozen)


def _is_reparse(value: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(int(getattr(value, "st_file_attributes", 0)) & flag)


def _require_plain_output_file(output_root: Path, relative_path: str, *, required: bool, artifact_id: str) -> Path | None:
    root = Path(os.path.abspath(output_root))
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise DeclaredOutputCollectionError("workflow output root is unavailable") from exc
    if _is_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise DeclaredOutputCollectionError("workflow output root must be a non-reparse directory")
    parts = PurePosixPath(relative_path).parts
    candidate = Path(os.path.abspath(root.joinpath(*parts)))
    try:
        contained = os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) == os.path.normcase(root)
    except ValueError:
        contained = False
    if not contained or candidate == root:
        raise DeclaredOutputCollectionError(f"declared output escaped its attempt root: {artifact_id}")
    cursor = root
    try:
        for part in parts:
            cursor /= part
            current = os.lstat(cursor)
            if _is_reparse(current):
                raise DeclaredOutputCollectionError(f"declared output traverses a reparse point: {artifact_id}")
    except FileNotFoundError:
        if required:
            raise DeclaredOutputCollectionError(f"required declared output file is missing: {artifact_id}") from None
        return None
    except OSError as exc:
        raise DeclaredOutputCollectionError(f"declared output file is unavailable: {artifact_id}") from exc
    if not stat.S_ISREG(current.st_mode):
        raise DeclaredOutputCollectionError(f"declared output must be a regular file: {artifact_id}")
    return candidate


class DeclaredFileOutputCollector:
    """Collect only files named by a frozen node output contract.

    The collector performs an early fail-closed path check. ``ArtifactService``
    remains authoritative and independently opens, hashes, seals, and records
    every returned file before a node can complete.
    """

    def __call__(
        self,
        context: ExecutionContext,
        _response: CodexResponse,
    ) -> tuple[ProducedArtifact, ...]:
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        mapping = declared_output_files(context.node)
        specs = {spec.artifact_id: spec for spec in context.node.outputs}
        produced: list[ProducedArtifact] = []
        for artifact_id, relative_path in mapping.items():
            spec = specs[artifact_id]
            path = _require_plain_output_file(
                context.output_root,
                relative_path,
                required=spec.required,
                artifact_id=artifact_id,
            )
            if path is not None:
                produced.append(
                    ProducedArtifact(
                        artifact_id=artifact_id,
                        path=path,
                        media_type=spec.media_type,
                        schema_ref=spec.schema_ref,
                        metadata={"collector": "declared-files/v1", "relative_path": relative_path},
                    )
                )
        return tuple(produced)


__all__ = [
    "DECLARED_OUTPUT_FILES_METADATA_KEY",
    "DeclaredFileOutputCollector",
    "DeclaredOutputCollectionError",
    "declared_output_files",
]
