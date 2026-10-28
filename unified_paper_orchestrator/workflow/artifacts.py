"""Program-enforced artifact binding and file verification.

The generic workflow contracts describe *what* a node consumes and produces.
This module is the runtime boundary that turns those declarations into trusted
records.  In particular, an executor's claim that it wrote a file is never
enough: :class:`ArtifactService` reads the file itself, inside the workspace,
and computes the digest and size before the ledger may accept it.

Importing this module and constructing the service are deliberately free of
filesystem and database I/O.  I/O only happens in the three explicit service
operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    ArtifactBinding,
    ArtifactSourceKind,
    ArtifactSpec,
    NodeDefinition,
    NodeRunStatus,
    WorkflowDefinition,
    canonical_json,
    is_sha256,
)
from .store import ArtifactRecord


SchemaValidator = Callable[[Path], None]


_ARTIFACT_RECORD_FIELDS = {
    "artifact_record_id",
    "run_id",
    "node_run_id",
    "producer_node_id",
    "artifact_id",
    "uri",
    "sha256",
    "media_type",
    "schema_ref",
    "size_bytes",
    "metadata",
    "created_at",
}


class ArtifactContractError(ValueError):
    """A runtime value does not satisfy a frozen artifact contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        artifact_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.artifact_id = artifact_id


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ArtifactContractError("artifact.type", f"{label} must be a string")
    if "\x00" in value:
        raise ArtifactContractError("artifact.text.nul", f"{label} cannot contain NUL")
    if not allow_empty and not value.strip():
        raise ArtifactContractError("artifact.text.required", f"{label} is required")
    return value


def _json_value(value: Any, label: str) -> Any:
    """Return a detached JSON value, rejecting NaN and custom objects."""

    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError(
            "artifact.json.invalid",
            f"{label} must be finite JSON data",
        ) from exc
    return json.loads(encoded)


def _frozen_mapping(value: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ArtifactContractError("artifact.type", f"{label} must be an object")
    detached = _json_value(dict(value), label)
    return MappingProxyType(detached)


def _size(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactContractError(
            "artifact.size.invalid",
            f"{label} must be a non-negative integer or null",
        )
    return value


@dataclass(frozen=True)
class BoundArtifact:
    """One node/workflow input after its declarative binding is resolved."""

    artifact_id: str
    source_kind: ArtifactSourceKind
    source_artifact_id: str
    media_type: str
    schema_ref: str = ""
    source_node_id: str = ""
    value: Any = None
    uri: str = ""
    sha256: str = ""
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    artifact_record_id: str = ""
    node_run_id: str = ""

    def __post_init__(self) -> None:
        _text(self.artifact_id, "BoundArtifact.artifact_id")
        _text(self.source_artifact_id, "BoundArtifact.source_artifact_id")
        _text(self.media_type, "BoundArtifact.media_type")
        _text(self.schema_ref, "BoundArtifact.schema_ref", allow_empty=True)
        _text(self.source_node_id, "BoundArtifact.source_node_id", allow_empty=True)
        _text(self.uri, "BoundArtifact.uri", allow_empty=True)
        _text(self.sha256, "BoundArtifact.sha256", allow_empty=True)
        _text(self.artifact_record_id, "BoundArtifact.artifact_record_id", allow_empty=True)
        _text(self.node_run_id, "BoundArtifact.node_run_id", allow_empty=True)
        object.__setattr__(self, "source_kind", ArtifactSourceKind(self.source_kind))
        object.__setattr__(self, "size_bytes", _size(self.size_bytes, "BoundArtifact.size_bytes"))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata, "BoundArtifact.metadata"))
        object.__setattr__(self, "value", _json_value(self.value, "BoundArtifact.value"))

        if self.source_kind is ArtifactSourceKind.NODE_OUTPUT:
            if not self.source_node_id:
                raise ArtifactContractError(
                    "artifact.source_node.required",
                    "node-output artifacts require source_node_id",
                    artifact_id=self.artifact_id,
                )
            if not self.uri or not is_sha256(self.sha256):
                raise ArtifactContractError(
                    "artifact.record.invalid",
                    "node-output artifacts require a URI and valid SHA256",
                    artifact_id=self.artifact_id,
                )
        elif self.source_node_id:
            raise ArtifactContractError(
                "artifact.source_node.unexpected",
                "workflow-input artifacts cannot name a source node",
                artifact_id=self.artifact_id,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "source_kind": self.source_kind.value,
            "source_artifact_id": self.source_artifact_id,
            "source_node_id": self.source_node_id,
            "value": _json_value(self.value, "BoundArtifact.value"),
            "uri": self.uri,
            "sha256": self.sha256.lower(),
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
            "size_bytes": self.size_bytes,
            "metadata": dict(self.metadata),
            "artifact_record_id": self.artifact_record_id,
            "node_run_id": self.node_run_id,
        }


@dataclass(frozen=True)
class ProducedArtifact:
    """An executor's untrusted declaration of a file it produced."""

    artifact_id: str
    path: str | Path
    media_type: str = "application/octet-stream"
    schema_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.artifact_id, "ProducedArtifact.artifact_id")
        if not isinstance(self.path, (str, Path)):
            raise ArtifactContractError("artifact.path.type", "ProducedArtifact.path must be a path")
        _text(os.fspath(self.path), "ProducedArtifact.path")
        _text(self.media_type, "ProducedArtifact.media_type")
        _text(self.schema_ref, "ProducedArtifact.schema_ref", allow_empty=True)
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata, "ProducedArtifact.metadata"))


@dataclass(frozen=True)
class ReadonlyArtifactFingerprint:
    """Portable evidence from a contained read that never creates a seal."""

    artifact_id: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    schema_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
        }


def _stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Stable file identity fields used to detect path/handle replacement."""

    return (int(value.st_dev), int(value.st_ino), int(value.st_mode))


def _stat_content(value: os.stat_result) -> tuple[int, int]:
    """Portable content-stability fields used around the verified read.

    ``ctime`` is intentionally excluded.  On Windows it can change merely as a
    consequence of opening or reading a file, which made a four-way
    path/handle comparison intermittently reject an unchanged artifact.  File
    replacement remains covered by ``_stat_identity`` and path re-resolution;
    in-place writes remain covered by size/mtime and the bytes actually hashed.
    """

    return (
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
    )


def _is_reparse(value: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(value, "st_file_attributes", 0))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & flag)


def _canonical_relative_uri(uri: str, artifact_id: str) -> str:
    """Validate the portable, relative URI stored in the artifact ledger."""

    value = _text(uri, "artifact record uri")
    if "\\" in value:
        raise ArtifactContractError(
            "artifact.uri.noncanonical",
            "artifact URI must use canonical forward slashes",
            artifact_id=artifact_id,
        )
    parsed = PurePosixPath(value)
    parts = parsed.parts
    if (
        parsed.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or parsed.as_posix() != value
    ):
        raise ArtifactContractError(
            "artifact.uri.noncanonical",
            "artifact URI must be a canonical portable relative path",
            artifact_id=artifact_id,
        )
    return value


def _windows_final_handle_path(descriptor: int) -> Path | None:
    """Return the kernel-resolved path for an open Windows file handle."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        get_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        get_path.restype = ctypes.c_uint32
        required = get_path(handle, None, 0, 0)
        if required == 0:
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_path(handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    except (AttributeError, ImportError, OSError, ValueError) as exc:
        raise ArtifactContractError(
            "artifact.handle.unresolved",
            "artifact file handle could not be resolved by the operating system",
        ) from exc


def _require_handle_within(descriptor: int, root: Path, artifact_id: str) -> None:
    handle_path = _windows_final_handle_path(descriptor)
    if handle_path is None:
        return
    try:
        root_value = os.path.normcase(os.path.abspath(root))
        handle_value = os.path.normcase(os.path.abspath(handle_path))
        contained = os.path.commonpath((root_value, handle_value)) == root_value
    except ValueError:
        contained = False
    if not contained or handle_value == root_value:
        raise ArtifactContractError(
            "artifact.handle.outside_workspace",
            "artifact file handle resolves outside the trusted workspace",
            artifact_id=artifact_id,
        )


class ArtifactService:
    """Resolve artifact bindings and independently verify produced files.

    ``store`` is intentionally duck typed.  The stable dependency consists of
    ``get_run``, ``latest_node_runs`` and ``list_artifacts``; this keeps the
    artifact boundary independently testable and avoids opening a database in
    the constructor.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        store: Any,
        *,
        schema_validators: Mapping[str, SchemaValidator] | None = None,
    ) -> None:
        if not isinstance(workspace_root, (str, Path)):
            raise TypeError("workspace_root must be a path")
        raw_root = os.fspath(workspace_root)
        if not raw_root or "\x00" in raw_root:
            raise ValueError("workspace_root must be a non-empty path")
        if store is None:
            raise TypeError("store is required")
        validators = dict(schema_validators or {})
        for schema_ref, validator in validators.items():
            _text(schema_ref, "schema validator reference")
            if not callable(validator):
                raise TypeError("schema validators must be callable")
        # Do not call resolve(), stat(), mkdir(), connect(), or any store method
        # here.  Construction must remain side-effect free.
        self.workspace_root = Path(workspace_root).expanduser()
        self.store = store
        self.schema_validators = MappingProxyType(validators)

    def resolve_inputs(
        self,
        definition: WorkflowDefinition,
        node: NodeDefinition,
        run_id: str,
        *,
        workflow_inputs: Mapping[str, Any] | None = None,
    ) -> dict[str, BoundArtifact]:
        """Resolve a node's bindings from run inputs and latest node attempts."""

        frozen_node = self._definition_node(definition, node)
        context = self._run_context(definition, run_id, workflow_inputs)
        return self._resolve_bindings(
            definition,
            frozen_node.inputs,
            frozen_node.input_bindings,
            run_id,
            context,
        )

    def output_root_for(self, run_id: str, node_run_id: str, generation: int = 1) -> Path:
        """Return the deterministic, lease-dispatch output root without I/O."""

        run = _text(run_id, "run_id")
        attempt = _text(node_run_id, "node_run_id")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ArtifactContractError(
                "artifact.output_root.generation",
                "lease generation must be a positive integer",
            )
        # 96 bits per component keeps Windows paths comfortably below legacy
        # MAX_PATH while retaining negligible collision probability.  The
        # attempt directory is still created exclusively, so a collision fails
        # closed as ``artifact.output_root.reused``.
        run_key = hashlib.sha256(run.encode("utf-8")).hexdigest()[:24]
        attempt_key = hashlib.sha256(attempt.encode("utf-8")).hexdigest()[:24]
        root = Path(os.path.abspath(self.workspace_root))
        return root / ".workflow_outputs" / "v1" / run_key / attempt_key / f"lease-{generation}"

    @staticmethod
    def _require_plain_directory(path: Path, artifact_id: str = "") -> None:
        value = os.lstat(path)
        if _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise ArtifactContractError(
                "artifact.output_root.invalid",
                "workflow output root must be a non-reparse directory",
                artifact_id=artifact_id,
            )

    def prepare_output_root(self, run_id: str, node_run_id: str, generation: int = 1) -> Path:
        """Create a brand-new directory owned by one fenced node dispatch.

        The final directory is created with exclusive ``mkdir`` semantics and
        is never reused or cleared.  Consequently a pre-existing artifact
        cannot be replayed as evidence for a new attempt or resumed lease.
        """

        try:
            root_lexical = Path(os.path.abspath(self.workspace_root))
            self._require_plain_directory(root_lexical)
            root_real = root_lexical.resolve(strict=True)
            expected = self.output_root_for(run_id, node_run_id, generation)
            relative = expected.relative_to(root_lexical)
            cursor = root_lexical
            parts = relative.parts
            for part in parts[:-1]:
                cursor /= part
                try:
                    os.mkdir(cursor)
                except FileExistsError:
                    pass
                self._require_plain_directory(cursor)
                resolved = cursor.resolve(strict=True)
                if resolved != root_real and root_real not in resolved.parents:
                    raise ArtifactContractError(
                        "artifact.output_root.outside_workspace",
                        "workflow output root escaped the workspace",
                    )
            attempt_root = cursor / parts[-1]
            try:
                os.mkdir(attempt_root)
            except FileExistsError as exc:
                raise ArtifactContractError(
                    "artifact.output_root.reused",
                    "node attempt output root already exists and cannot be reused",
                ) from exc
            self._require_plain_directory(attempt_root)
            resolved_attempt = attempt_root.resolve(strict=True)
            if resolved_attempt == root_real or root_real not in resolved_attempt.parents:
                raise ArtifactContractError(
                    "artifact.output_root.changed",
                    "node attempt output root changed during creation",
                )
            return attempt_root
        except ArtifactContractError:
            raise
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ArtifactContractError(
                "artifact.workspace.invalid",
                "artifact workspace root must already exist as a directory",
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactContractError(
                "artifact.output_root.invalid",
                f"node attempt output root cannot be safely prepared ({type(exc).__name__})",
            ) from exc

    def resolve_workflow_outputs(
        self,
        definition: WorkflowDefinition,
        run_id: str,
        *,
        workflow_inputs: Mapping[str, Any] | None = None,
    ) -> dict[str, BoundArtifact]:
        """Resolve the workflow's declared public outputs after execution."""

        context = self._run_context(definition, run_id, workflow_inputs)
        return self._resolve_bindings(
            definition,
            definition.outputs,
            definition.output_bindings,
            run_id,
            context,
        )

    def verify_outputs(
        self,
        node: NodeDefinition,
        produced: Sequence[ProducedArtifact],
        *,
        output_root: str | Path | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        """Verify executor files and return ledger-ready immutable records."""

        if isinstance(produced, (str, bytes, bytearray)) or not isinstance(produced, Sequence):
            raise ArtifactContractError(
                "artifact.outputs.type",
                "produced outputs must be a sequence of ProducedArtifact values",
            )
        values = list(produced)
        if any(not isinstance(item, ProducedArtifact) for item in values):
            raise ArtifactContractError(
                "artifact.outputs.type",
                "every produced output must be a ProducedArtifact",
            )

        specs = self._spec_index(node.outputs, "node output")
        ids = [item.artifact_id for item in values]
        duplicate = sorted({artifact_id for artifact_id in ids if ids.count(artifact_id) > 1})
        if duplicate:
            raise ArtifactContractError(
                "artifact.output.duplicate",
                f"duplicate produced artifact IDs: {', '.join(duplicate)}",
            )
        unknown = sorted(set(ids) - set(specs))
        if unknown:
            raise ArtifactContractError(
                "artifact.output.unknown",
                f"undeclared produced artifact IDs: {', '.join(unknown)}",
            )
        missing = sorted(artifact_id for artifact_id, spec in specs.items() if spec.required and artifact_id not in ids)
        if missing:
            raise ArtifactContractError(
                "artifact.output.required",
                f"missing required produced artifact IDs: {', '.join(missing)}",
            )

        trusted_output_root: Path | None = None
        if output_root is not None:
            try:
                trusted_output_root = Path(os.path.abspath(output_root)).resolve(strict=True)
                self._require_plain_directory(trusted_output_root)
                workspace = Path(os.path.abspath(self.workspace_root)).resolve(strict=True)
                if trusted_output_root == workspace or workspace not in trusted_output_root.parents:
                    raise ArtifactContractError(
                        "artifact.output_root.outside_workspace",
                        "node attempt output root must be a workspace descendant",
                    )
            except ArtifactContractError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                raise ArtifactContractError(
                    "artifact.output_root.invalid",
                    "node attempt output root is unavailable",
                ) from exc

        declarations: dict[str, ProducedArtifact] = {}
        for item in values:
            path = item.path
            if trusted_output_root is not None:
                candidate = (
                    Path(os.path.abspath(path))
                    if path.is_absolute()
                    else Path(os.path.abspath(trusted_output_root / path))
                )
                if candidate == trusted_output_root or trusted_output_root not in candidate.parents:
                    raise ArtifactContractError(
                        "artifact.output.outside_attempt",
                        "produced artifact is outside its node attempt output root",
                        artifact_id=item.artifact_id,
                    )
                item = ProducedArtifact(
                    item.artifact_id,
                    candidate,
                    item.media_type,
                    item.schema_ref,
                    item.metadata,
                )
            declarations[item.artifact_id] = item
        records: list[ArtifactRecord] = []
        for artifact_id in sorted(declarations):
            declaration = declarations[artifact_id]
            spec = specs[artifact_id]
            self._require_compatible(spec, declaration.media_type, declaration.schema_ref, artifact_id)
            uri, digest, size_bytes = self._read_and_fingerprint(
                declaration.path,
                artifact_id,
                seal=True,
            )
            sealed_path = Path(os.path.abspath(self.workspace_root / Path(uri)))
            self._validate_content(sealed_path, spec, artifact_id)
            final_uri, final_digest, final_size = self._read_and_fingerprint(
                sealed_path,
                artifact_id,
                seal=False,
            )
            if final_uri != uri or final_digest != digest or final_size != size_bytes:
                raise ArtifactContractError(
                    "artifact.seal.changed",
                    "sealed artifact changed during content validation",
                    artifact_id=artifact_id,
                )
            record = ArtifactRecord(
                artifact_id=artifact_id,
                uri=uri,
                sha256=digest,
                media_type=declaration.media_type,
                schema_ref=declaration.schema_ref,
                size_bytes=size_bytes,
                metadata=dict(declaration.metadata),
            )
            # Exercise the ledger record's independent validation now, before
            # the caller starts its completion transaction.
            record.to_dict()
            records.append(record)
        return tuple(records)

    @staticmethod
    def _definition_node(definition: WorkflowDefinition, node: NodeDefinition) -> NodeDefinition:
        if not isinstance(definition, WorkflowDefinition) or not isinstance(node, NodeDefinition):
            raise TypeError("definition and node must be workflow contract values")
        matches = [candidate for candidate in definition.nodes if candidate.node_id == node.node_id]
        if len(matches) != 1 or matches[0].to_dict() != node.to_dict():
            raise ArtifactContractError(
                "artifact.node.not_frozen",
                "node does not match the frozen workflow definition",
            )
        return matches[0]

    def _run_context(
        self,
        definition: WorkflowDefinition,
        run_id: str,
        workflow_inputs: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        _text(run_id, "run_id")
        run = self.store.get_run(run_id)
        if not isinstance(run, Mapping):
            raise ArtifactContractError("artifact.run.invalid", "workflow store returned an invalid run")
        if run.get("run_id") != run_id:
            raise ArtifactContractError("artifact.run.mismatch", "workflow store returned a different run")
        if run.get("definition_sha256") != definition.sha256():
            raise ArtifactContractError(
                "artifact.definition.mismatch",
                "workflow definition does not match the run's frozen digest",
            )
        stored_inputs = run.get("inputs")
        inputs = workflow_inputs if workflow_inputs is not None else stored_inputs
        if workflow_inputs is not None and stored_inputs is not None:
            if _json_value(workflow_inputs, "workflow_inputs") != _json_value(stored_inputs, "stored workflow inputs"):
                raise ArtifactContractError(
                    "artifact.inputs.not_frozen",
                    "supplied workflow inputs do not match the frozen run inputs",
                )
        return self._validate_workflow_inputs(definition, inputs)

    @staticmethod
    def _spec_index(specs: Sequence[ArtifactSpec], label: str) -> dict[str, ArtifactSpec]:
        result: dict[str, ArtifactSpec] = {}
        for spec in specs:
            if not isinstance(spec, ArtifactSpec):
                raise ArtifactContractError("artifact.spec.invalid", f"{label} contains a non-ArtifactSpec")
            if spec.artifact_id in result:
                raise ArtifactContractError(
                    "artifact.spec.duplicate",
                    f"{label} contains duplicate artifact ID: {spec.artifact_id}",
                )
            result[spec.artifact_id] = spec
        return result

    def _validate_workflow_inputs(
        self,
        definition: WorkflowDefinition,
        inputs: Any,
    ) -> dict[str, Any]:
        if not isinstance(inputs, Mapping):
            raise ArtifactContractError(
                "artifact.inputs.type",
                "workflow inputs must be an object",
            )
        specs = self._spec_index(definition.inputs, "workflow input")
        keys = list(inputs)
        if any(not isinstance(key, str) for key in keys):
            raise ArtifactContractError(
                "artifact.inputs.key",
                "workflow input keys must be strings",
            )
        unknown = sorted(set(keys) - set(specs))
        if unknown:
            raise ArtifactContractError(
                "artifact.inputs.unknown",
                f"undeclared workflow input IDs: {', '.join(unknown)}",
            )
        missing = sorted(
            artifact_id for artifact_id, spec in specs.items() if spec.required and artifact_id not in inputs
        )
        if missing:
            raise ArtifactContractError(
                "artifact.inputs.required",
                f"missing required workflow input IDs: {', '.join(missing)}",
            )
        return {key: _json_value(value, f"workflow input {key}") for key, value in inputs.items()}

    def _resolve_bindings(
        self,
        definition: WorkflowDefinition,
        target_specs: Sequence[ArtifactSpec],
        bindings: Sequence[ArtifactBinding],
        run_id: str,
        workflow_inputs: Mapping[str, Any],
    ) -> dict[str, BoundArtifact]:
        specs = self._spec_index(target_specs, "binding target")
        binding_by_target: dict[str, ArtifactBinding] = {}
        for binding in bindings:
            if not isinstance(binding, ArtifactBinding):
                raise ArtifactContractError("artifact.binding.invalid", "binding is not an ArtifactBinding")
            if binding.target_artifact_id in binding_by_target:
                raise ArtifactContractError(
                    "artifact.binding.duplicate",
                    f"duplicate binding target: {binding.target_artifact_id}",
                )
            if binding.target_artifact_id not in specs:
                raise ArtifactContractError(
                    "artifact.binding.unknown_target",
                    f"binding targets undeclared artifact: {binding.target_artifact_id}",
                )
            binding_by_target[binding.target_artifact_id] = binding

        missing_bindings = sorted(
            artifact_id for artifact_id, spec in specs.items() if spec.required and artifact_id not in binding_by_target
        )
        if missing_bindings:
            raise ArtifactContractError(
                "artifact.binding.required",
                f"required artifacts have no binding: {', '.join(missing_bindings)}",
            )

        latest: dict[str, Mapping[str, Any]] | None = None
        artifacts: list[Mapping[str, Any]] | None = None
        resolved: dict[str, BoundArtifact] = {}
        for target_id in sorted(binding_by_target):
            binding = binding_by_target[target_id]
            target_spec = specs[target_id]
            if binding.source_kind is ArtifactSourceKind.WORKFLOW_INPUT:
                if binding.source_node_id:
                    raise ArtifactContractError(
                        "artifact.binding.source_node",
                        "workflow-input binding cannot name a source node",
                        artifact_id=target_id,
                    )
                if binding.source_artifact_id not in workflow_inputs:
                    if target_spec.required:
                        raise ArtifactContractError(
                            "artifact.input.required",
                            f"required workflow input is absent: {binding.source_artifact_id}",
                            artifact_id=target_id,
                        )
                    continue
                source_specs = self._spec_index(definition.inputs, "workflow input")
                source_spec = source_specs.get(binding.source_artifact_id)
                if source_spec is None:
                    raise ArtifactContractError(
                        "artifact.binding.source_unknown",
                        f"workflow input binding source is undeclared: {binding.source_artifact_id}",
                        artifact_id=target_id,
                    )
                self._require_compatible(
                    target_spec,
                    source_spec.media_type,
                    source_spec.schema_ref,
                    target_id,
                )
                resolved[target_id] = BoundArtifact(
                    artifact_id=target_id,
                    source_kind=binding.source_kind,
                    source_artifact_id=binding.source_artifact_id,
                    media_type=source_spec.media_type,
                    schema_ref=source_spec.schema_ref,
                    value=workflow_inputs[binding.source_artifact_id],
                )
                continue

            if binding.source_kind is not ArtifactSourceKind.NODE_OUTPUT:
                raise ArtifactContractError(
                    "artifact.binding.source_kind",
                    "unsupported artifact source kind",
                    artifact_id=target_id,
                )
            if not binding.source_node_id:
                raise ArtifactContractError(
                    "artifact.binding.source_node",
                    "node-output binding requires source_node_id",
                    artifact_id=target_id,
                )
            source_nodes = [item for item in definition.nodes if item.node_id == binding.source_node_id]
            if len(source_nodes) != 1:
                raise ArtifactContractError(
                    "artifact.binding.source_unknown",
                    f"node-output binding source is undeclared: {binding.source_node_id}",
                    artifact_id=target_id,
                )
            source_outputs = self._spec_index(source_nodes[0].outputs, "source node output")
            source_spec = source_outputs.get(binding.source_artifact_id)
            if source_spec is None:
                raise ArtifactContractError(
                    "artifact.binding.source_unknown",
                    f"node output binding source is undeclared: {binding.source_artifact_id}",
                    artifact_id=target_id,
                )
            self._require_compatible(
                target_spec,
                source_spec.media_type,
                source_spec.schema_ref,
                target_id,
            )

            if latest is None:
                latest_rows = self.store.latest_node_runs(run_id)
                if not isinstance(latest_rows, list):
                    raise ArtifactContractError(
                        "artifact.store.invalid",
                        "workflow store returned invalid latest node attempts",
                    )
                latest = {}
                for row in latest_rows:
                    if not isinstance(row, Mapping) or not isinstance(row.get("node_id"), str):
                        raise ArtifactContractError(
                            "artifact.store.invalid",
                            "workflow store returned an invalid node attempt",
                        )
                    if row.get("workflow_run_id") != run_id:
                        raise ArtifactContractError(
                            "artifact.store.run_mismatch",
                            "workflow store returned a node attempt from another run",
                        )
                    if row["node_id"] in latest:
                        raise ArtifactContractError(
                            "artifact.store.ambiguous",
                            f"workflow store returned duplicate latest attempts for {row['node_id']}",
                        )
                    latest[str(row["node_id"])] = row
            source_attempt = latest.get(binding.source_node_id)
            if source_attempt is None:
                raise ArtifactContractError(
                    "artifact.source.attempt_missing",
                    f"source node has no latest attempt: {binding.source_node_id}",
                    artifact_id=target_id,
                )
            if source_attempt.get("status") != NodeRunStatus.COMPLETED.value:
                raise ArtifactContractError(
                    "artifact.source.not_completed",
                    f"latest source node attempt is not completed: {binding.source_node_id}",
                    artifact_id=target_id,
                )
            node_run_id = source_attempt.get("node_run_id")
            if not isinstance(node_run_id, str) or not node_run_id:
                raise ArtifactContractError(
                    "artifact.store.invalid",
                    "latest source node attempt has no node_run_id",
                    artifact_id=target_id,
                )
            if artifacts is None:
                artifact_rows = self.store.list_artifacts(run_id)
                if not isinstance(artifact_rows, list):
                    raise ArtifactContractError(
                        "artifact.store.invalid",
                        "workflow store returned invalid artifacts",
                    )
                for record in artifact_rows:
                    if not isinstance(record, Mapping):
                        raise ArtifactContractError(
                            "artifact.store.invalid",
                            "workflow store returned an invalid artifact record",
                        )
                    if record.get("run_id") != run_id:
                        raise ArtifactContractError(
                            "artifact.store.run_mismatch",
                            "workflow store returned an artifact from another run",
                        )
                artifacts = artifact_rows
            matches = [
                record
                for record in artifacts
                if isinstance(record, Mapping)
                and record.get("run_id") == run_id
                and record.get("node_run_id") == node_run_id
                and record.get("producer_node_id") == binding.source_node_id
                and record.get("artifact_id") == binding.source_artifact_id
            ]
            if len(matches) != 1:
                raise ArtifactContractError(
                    "artifact.source.ambiguous",
                    "latest completed source attempt must have exactly one matching artifact",
                    artifact_id=target_id,
                )
            resolved[target_id] = self._bound_from_record(target_id, binding, target_spec, matches[0])

        return resolved

    def _bound_from_record(
        self,
        target_id: str,
        binding: ArtifactBinding,
        target_spec: ArtifactSpec,
        record: Mapping[str, Any],
    ) -> BoundArtifact:
        keys = set(record)
        if keys != _ARTIFACT_RECORD_FIELDS:
            missing = sorted(_ARTIFACT_RECORD_FIELDS - keys)
            unknown = sorted(keys - _ARTIFACT_RECORD_FIELDS)
            parts = []
            if missing:
                parts.append(f"missing {', '.join(missing)}")
            if unknown:
                parts.append(f"unknown {', '.join(unknown)}")
            raise ArtifactContractError(
                "artifact.record.shape",
                f"artifact ledger record has invalid fields ({'; '.join(parts)})",
                artifact_id=target_id,
            )
        artifact_id = _text(record["artifact_id"], "artifact record artifact_id")
        producer = _text(record["producer_node_id"], "artifact record producer_node_id")
        node_run_id = _text(record["node_run_id"], "artifact record node_run_id")
        uri = _canonical_relative_uri(str(record["uri"]), target_id)
        digest = _text(record["sha256"], "artifact record sha256")
        media_type = _text(record["media_type"], "artifact record media_type")
        schema_ref = _text(record["schema_ref"], "artifact record schema_ref", allow_empty=True)
        artifact_record_id = _text(record["artifact_record_id"], "artifact record ID")
        if artifact_id != binding.source_artifact_id or producer != binding.source_node_id:
            raise ArtifactContractError(
                "artifact.record.source_mismatch",
                "artifact ledger record does not match its binding source",
                artifact_id=target_id,
            )
        if not is_sha256(digest):
            raise ArtifactContractError(
                "artifact.record.sha256",
                "artifact ledger record has an invalid SHA256",
                artifact_id=target_id,
            )
        self._require_compatible(target_spec, media_type, schema_ref, target_id)
        metadata = record["metadata"]
        if not isinstance(metadata, Mapping):
            raise ArtifactContractError(
                "artifact.record.metadata",
                "artifact ledger metadata must be an object",
                artifact_id=target_id,
            )
        size_bytes = _size(record["size_bytes"], "artifact record size_bytes")
        observed_uri, observed_digest, observed_size = self._read_and_fingerprint(
            Path(uri),
            target_id,
            seal=True,
        )
        if (
            observed_digest != digest.lower()
            or (size_bytes is not None and observed_size != size_bytes)
        ):
            raise ArtifactContractError(
                "artifact.record.changed",
                "artifact bytes no longer match the immutable ledger record",
                artifact_id=target_id,
            )
        sealed_path = Path(os.path.abspath(self.workspace_root / Path(observed_uri)))
        self._validate_content(sealed_path, target_spec, target_id)
        final_uri, final_digest, final_size = self._read_and_fingerprint(
            sealed_path,
            target_id,
            seal=False,
        )
        if (
            final_uri != observed_uri
            or final_digest != observed_digest
            or final_size != observed_size
        ):
            raise ArtifactContractError(
                "artifact.seal.changed",
                "sealed input changed during content validation",
                artifact_id=target_id,
            )
        return BoundArtifact(
            artifact_id=target_id,
            source_kind=binding.source_kind,
            source_artifact_id=artifact_id,
            source_node_id=producer,
            media_type=media_type,
            schema_ref=schema_ref,
            uri=observed_uri,
            sha256=digest.lower(),
            size_bytes=size_bytes,
            metadata=metadata,
            artifact_record_id=artifact_record_id,
            node_run_id=node_run_id,
        )

    @staticmethod
    def _require_compatible(
        spec: ArtifactSpec,
        media_type: str,
        schema_ref: str,
        artifact_id: str,
    ) -> None:
        if media_type != spec.media_type:
            raise ArtifactContractError(
                "artifact.media_type.mismatch",
                f"artifact media type does not match contract: {artifact_id}",
                artifact_id=artifact_id,
            )
        if schema_ref != spec.schema_ref:
            raise ArtifactContractError(
                "artifact.schema.mismatch",
                f"artifact schema does not match contract: {artifact_id}",
                artifact_id=artifact_id,
            )

    def _validate_content(
        self,
        path: Path,
        spec: ArtifactSpec,
        artifact_id: str,
    ) -> None:
        """Validate bytes independently of executor-supplied content labels."""

        try:
            media_type = spec.media_type.lower()
            if media_type == "application/json" or media_type.endswith("+json"):
                payload = path.read_text(encoding="utf-8")
                json.loads(
                    payload,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON constant: {value}")
                    ),
                )
            elif media_type.startswith("text/"):
                with path.open("r", encoding="utf-8", errors="strict") as stream:
                    while stream.read(1024 * 1024):
                        pass
            elif media_type == "application/pdf":
                with path.open("rb") as stream:
                    if stream.read(5) != b"%PDF-":
                        raise ValueError("missing PDF signature")
            elif media_type == "image/png":
                with path.open("rb") as stream:
                    if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise ValueError("missing PNG signature")
            elif media_type in {"image/jpeg", "image/jpg"}:
                with path.open("rb") as stream:
                    if stream.read(2) != b"\xff\xd8":
                        raise ValueError("missing JPEG signature")
            if spec.schema_ref:
                validator = self.schema_validators.get(spec.schema_ref)
                if validator is None:
                    raise ArtifactContractError(
                        "artifact.schema.validator_missing",
                        f"no trusted validator is registered for schema: {spec.schema_ref}",
                        artifact_id=artifact_id,
                    )
                validator(path)
        except ArtifactContractError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactContractError(
                "artifact.content.invalid",
                f"artifact bytes do not satisfy media/schema contract ({type(exc).__name__})",
                artifact_id=artifact_id,
            ) from exc
        except Exception as exc:
            raise ArtifactContractError(
                "artifact.schema.invalid",
                f"trusted schema validator rejected the artifact ({type(exc).__name__})",
                artifact_id=artifact_id,
            ) from exc

    def _open_sealed_target(
        self,
        root_lexical: Path,
        root_real: Path,
        artifact_id: str,
    ) -> tuple[Path, int]:
        cursor = root_lexical
        for part in (".workflow_artifacts", "v1", "objects"):
            cursor /= part
            try:
                os.mkdir(cursor)
            except FileExistsError:
                pass
            self._require_plain_directory(cursor, artifact_id)
            resolved = cursor.resolve(strict=True)
            if resolved != root_real and root_real not in resolved.parents:
                raise ArtifactContractError(
                    "artifact.seal.outside_workspace",
                    "artifact seal directory escaped the trusted workspace",
                    artifact_id=artifact_id,
                )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(16):
            target = cursor / f"{secrets.token_hex(20)}.blob"
            try:
                descriptor = os.open(target, flags, 0o600)
            except FileExistsError:
                continue
            try:
                _require_handle_within(descriptor, root_real, artifact_id)
            except Exception:
                os.close(descriptor)
                raise
            return target, descriptor
        raise ArtifactContractError(
            "artifact.seal.collision",
            "could not allocate a unique sealed artifact path",
            artifact_id=artifact_id,
        )

    def _read_and_fingerprint(
        self,
        declared_path: Path,
        artifact_id: str,
        *,
        seal: bool = False,
    ) -> tuple[str, str, int]:
        """Contain and fingerprint a file, optionally copying to a host seal."""

        try:
            root_lexical = Path(os.path.abspath(self.workspace_root))
            candidate_lexical = (
                Path(os.path.abspath(declared_path))
                if declared_path.is_absolute()
                else Path(os.path.abspath(root_lexical / declared_path))
            )
            if candidate_lexical != root_lexical and root_lexical not in candidate_lexical.parents:
                raise ArtifactContractError(
                    "artifact.path.outside_workspace",
                    "artifact path is outside the workspace",
                    artifact_id=artifact_id,
                )

            root_real = root_lexical.resolve(strict=True)
            root_stat = os.lstat(root_lexical)
            if _is_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode) or not root_real.is_dir():
                raise ArtifactContractError(
                    "artifact.workspace.invalid",
                    "artifact workspace root is not a directory",
                    artifact_id=artifact_id,
                )

            # Reject every descendant symlink or Windows reparse point.  Merely
            # checking the final resolved path is insufficient because a
            # junction can be swapped during a read.
            relative_parts = candidate_lexical.relative_to(root_lexical).parts
            if any(":" in part for part in relative_parts):
                raise ArtifactContractError(
                    "artifact.path.stream",
                    "artifact paths cannot contain alternate-stream or device separators",
                    artifact_id=artifact_id,
                )
            cursor = root_lexical
            for part in relative_parts:
                cursor /= part
                cursor_stat = os.lstat(cursor)
                if _is_reparse(cursor_stat):
                    raise ArtifactContractError(
                        "artifact.path.reparse",
                        "artifact path cannot traverse a symlink or reparse point",
                        artifact_id=artifact_id,
                    )

            candidate_real = candidate_lexical.resolve(strict=True)
            if candidate_real != root_real and root_real not in candidate_real.parents:
                raise ArtifactContractError(
                    "artifact.path.outside_workspace",
                    "artifact path resolves outside the workspace",
                    artifact_id=artifact_id,
                )
            if candidate_real == root_real:
                raise ArtifactContractError(
                    "artifact.path.not_file",
                    "artifact path must identify a regular file",
                    artifact_id=artifact_id,
                )

            before_path = os.stat(candidate_real, follow_symlinks=False)
            if _is_reparse(before_path) or not stat.S_ISREG(before_path.st_mode):
                raise ArtifactContractError(
                    "artifact.path.not_file",
                    "artifact path must identify a non-reparse regular file",
                    artifact_id=artifact_id,
                )

            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate_real, flags)
            sealed_path: Path | None = None
            sealed_descriptor: int | None = None
            try:
                before_handle = os.fstat(descriptor)
                if not stat.S_ISREG(before_handle.st_mode):
                    raise ArtifactContractError(
                        "artifact.path.not_file",
                        "artifact handle is not a regular file",
                        artifact_id=artifact_id,
                    )
                _require_handle_within(descriptor, root_real, artifact_id)
                if seal:
                    sealed_path, sealed_descriptor = self._open_sealed_target(
                        root_lexical,
                        root_real,
                        artifact_id,
                    )
                digest = hashlib.sha256()
                size_bytes = 0
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size_bytes += len(chunk)
                        if sealed_descriptor is not None:
                            pending = memoryview(chunk)
                            while pending:
                                written = os.write(sealed_descriptor, pending)
                                if written <= 0:
                                    raise OSError("sealed artifact write made no progress")
                                pending = pending[written:]
                if sealed_descriptor is not None:
                    os.fsync(sealed_descriptor)
                    sealed_stat = os.fstat(sealed_descriptor)
                    if int(sealed_stat.st_size) != size_bytes:
                        raise ArtifactContractError(
                            "artifact.seal.changed",
                            "sealed artifact byte count differs from verified input",
                            artifact_id=artifact_id,
                        )
                    _require_handle_within(sealed_descriptor, root_real, artifact_id)
                after_handle = os.fstat(descriptor)
            finally:
                if sealed_descriptor is not None:
                    os.close(sealed_descriptor)
                os.close(descriptor)

            after_path = os.stat(candidate_real, follow_symlinks=False)
            snapshots = (before_path, before_handle, after_handle, after_path)
            if len({_stat_identity(value) for value in snapshots}) != 1 or len(
                {_stat_content(value) for value in snapshots}
            ) != 1:
                raise ArtifactContractError(
                    "artifact.file.changed",
                    "artifact file changed while it was being verified",
                    artifact_id=artifact_id,
                )
            if size_bytes != int(after_path.st_size):
                raise ArtifactContractError(
                    "artifact.file.changed",
                    "artifact byte count changed while it was being verified",
                    artifact_id=artifact_id,
                )

            # Re-resolve both paths after the read to catch a parent directory
            # or trust-boundary replacement during verification.
            if (
                root_lexical.resolve(strict=True) != root_real
                or candidate_lexical.resolve(strict=True) != candidate_real
            ):
                raise ArtifactContractError(
                    "artifact.path.changed",
                    "artifact path changed while it was being verified",
                    artifact_id=artifact_id,
                )
            source_digest = digest.hexdigest()
            if sealed_path is not None:
                sealed_uri, sealed_digest, sealed_size = self._read_and_fingerprint(
                    sealed_path,
                    artifact_id,
                    seal=False,
                )
                if sealed_digest != source_digest or sealed_size != size_bytes:
                    raise ArtifactContractError(
                        "artifact.seal.changed",
                        "sealed artifact bytes changed before acceptance",
                        artifact_id=artifact_id,
                    )
                try:
                    os.chmod(sealed_path, stat.S_IREAD)
                except OSError as exc:
                    raise ArtifactContractError(
                        "artifact.seal.readonly",
                        "sealed artifact could not be made read-only",
                        artifact_id=artifact_id,
                    ) from exc
                return _canonical_relative_uri(sealed_uri, artifact_id), sealed_digest, sealed_size
            uri = _canonical_relative_uri(candidate_real.relative_to(root_real).as_posix(), artifact_id)
            return uri, source_digest, size_bytes
        except ArtifactContractError:
            raise
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ArtifactContractError(
                "artifact.path.missing",
                "artifact file or workspace does not exist",
                artifact_id=artifact_id,
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactContractError(
                "artifact.path.invalid",
                f"artifact file cannot be safely verified ({type(exc).__name__})",
                artifact_id=artifact_id,
            ) from exc


def fingerprint_workspace_artifact(
    workspace_root: str | Path,
    path: str | Path,
    *,
    artifact_id: str,
    media_type: str = "application/octet-stream",
    schema_ref: str = "",
) -> ReadonlyArtifactFingerprint:
    """Fingerprint one contained regular file without writing or sealing it.

    This is the public read-only boundary used by compatibility observers.  It
    intentionally reuses the same reparse, handle, stability and containment
    checks as production artifact acceptance while keeping the source file and
    workspace byte-for-byte untouched.
    """

    _text(artifact_id, "artifact_id")
    _text(media_type, "media_type")
    _text(schema_ref, "schema_ref", allow_empty=True)
    service = ArtifactService(workspace_root, object())
    uri, digest, size_bytes = service._read_and_fingerprint(Path(path), artifact_id, seal=False)
    return ReadonlyArtifactFingerprint(
        artifact_id=artifact_id,
        uri=uri,
        sha256=digest,
        size_bytes=size_bytes,
        media_type=media_type,
        schema_ref=schema_ref,
    )


__all__ = [
    "ArtifactContractError",
    "ArtifactService",
    "BoundArtifact",
    "ProducedArtifact",
    "ReadonlyArtifactFingerprint",
    "SchemaValidator",
    "fingerprint_workspace_artifact",
]
