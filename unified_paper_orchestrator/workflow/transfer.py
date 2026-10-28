"""Strict portable export/import for linear workflow drafts.

Bundles never carry runtime paths, runtime bindings, database identities or a
trusted publication status. Import assigns a caller-chosen workflow identity,
rewrites PromptRefs to the local content-addressed namespace, and produces only
an in-memory custom draft until a library explicitly stores it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .contracts import (
    NodeType,
    PromptRef,
    ValidationMode,
    WorkflowDefinition,
    WorkflowVersion,
    canonical_json,
    canonical_sha256,
    is_sha256,
)
from .resources import (
    MAX_PROMPT_BYTES,
    USER_PROMPT_RESOURCE_PREFIX,
    ContentAddressedPromptStore,
    PromptResourceRecord,
)


WORKFLOW_BUNDLE_SCHEMA = "unified-paper-orchestrator/workflow-portable-bundle/v1"
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_RESOURCES = 256
_ROOT_FIELDS = {"schema", "source", "definition", "resources", "bundle_sha256"}
_SOURCE_FIELDS = {"workflow_id", "version", "definition_sha256"}
_RESOURCE_FIELDS = {"resource_id", "content_sha256", "version", "content_utf8"}
_PROTECTED_METADATA_KEYS = {
    "builtin",
    "readonly",
    "pack_id",
    "pack_version",
    "legacy_stage",
    "native_dispatch_ready",
}


class WorkflowTransferError(ValueError):
    """A portable bundle is malformed, unsafe or internally inconsistent."""


def _expect_exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise WorkflowTransferError(f"{label} must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise WorkflowTransferError(f"{label} has missing or unknown fields")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowTransferError("portable bundle contains a duplicate JSON key")
        result[key] = value
    return result


def _decode_bundle(payload: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        try:
            encoded = canonical_json(dict(payload)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WorkflowTransferError("portable bundle must be finite JSON") from exc
        if len(encoded) > MAX_BUNDLE_BYTES:
            raise WorkflowTransferError("portable bundle exceeds the size limit")
        return json.loads(encoded.decode("utf-8"))
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        encoded = payload
    else:
        raise TypeError("portable bundle must be JSON text, bytes or a mapping")
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise WorkflowTransferError("portable bundle exceeds the size limit")
    try:
        value = json.loads(encoded.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except WorkflowTransferError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowTransferError("portable bundle is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowTransferError("portable bundle top level must be an object")
    return value


def _prompt_refs(definition: WorkflowDefinition) -> dict[str, PromptRef]:
    result: dict[str, PromptRef] = {}
    for node in definition.nodes:
        prompt_ref = node.prompt_ref
        if prompt_ref is None:
            continue
        previous = result.get(prompt_ref.resource_id)
        if previous is not None and previous != prompt_ref:
            raise WorkflowTransferError("one Prompt resource ID refers to different content")
        result[prompt_ref.resource_id] = prompt_ref
    return result


@dataclass(frozen=True)
class ImportedPromptResource:
    original_resource_id: str
    resource_id: str
    content_sha256: str
    version: str
    content_utf8: str

    def publish(self, store: ContentAddressedPromptStore) -> PromptResourceRecord:
        if not isinstance(store, ContentAddressedPromptStore):
            raise TypeError("store must be a ContentAddressedPromptStore")
        record = store.put(self.content_utf8, version=self.version)
        if record.resource_id != self.resource_id or record.content_sha256 != self.content_sha256:
            raise WorkflowTransferError("published Prompt resource identity changed")
        return record


@dataclass(frozen=True)
class ImportedWorkflowDraft:
    definition: WorkflowDefinition
    resources: tuple[ImportedPromptResource, ...]
    source_workflow_id: str
    source_version: int
    source_definition_sha256: str
    bundle_sha256: str

    def publish_resources(
        self,
        store: ContentAddressedPromptStore,
    ) -> tuple[PromptResourceRecord, ...]:
        return tuple(item.publish(store) for item in self.resources)


def export_workflow_bundle(
    version: WorkflowVersion,
    *,
    prompt_text_by_resource_id: Mapping[str, str],
) -> str:
    """Return deterministic canonical JSON for one portable workflow version."""

    if not isinstance(version, WorkflowVersion):
        raise TypeError("version must be a WorkflowVersion")
    if not isinstance(prompt_text_by_resource_id, Mapping):
        raise TypeError("prompt_text_by_resource_id must be a mapping")
    version.validate(mode=ValidationMode.LINEAR)
    refs = _prompt_refs(version.definition)
    missing = set(refs) - set(prompt_text_by_resource_id)
    if missing:
        raise WorkflowTransferError("portable export is missing referenced Prompt content")
    resources: list[dict[str, Any]] = []
    total_resource_bytes = 0
    for resource_id, prompt_ref in sorted(refs.items()):
        text = prompt_text_by_resource_id[resource_id]
        if not isinstance(text, str):
            raise WorkflowTransferError("Prompt export content must be text")
        content = text.encode("utf-8")
        total_resource_bytes += len(content)
        if not content or b"\x00" in content or len(content) > MAX_PROMPT_BYTES:
            raise WorkflowTransferError("Prompt export content is empty or too large")
        if hashlib.sha256(content).hexdigest() != prompt_ref.content_sha256.casefold():
            raise WorkflowTransferError("Prompt export content does not match its PromptRef digest")
        resources.append(
            {
                "resource_id": resource_id,
                "content_sha256": prompt_ref.content_sha256.casefold(),
                "version": prompt_ref.version,
                "content_utf8": text,
            }
        )
    if len(resources) > MAX_BUNDLE_RESOURCES or total_resource_bytes > MAX_BUNDLE_BYTES:
        raise WorkflowTransferError("portable export contains too many Prompt resources")
    core = {
        "schema": WORKFLOW_BUNDLE_SCHEMA,
        "source": {
            "workflow_id": version.workflow_id,
            "version": version.version,
            "definition_sha256": version.definition_sha256,
        },
        "definition": version.definition.to_dict(),
        "resources": resources,
    }
    return canonical_json({**core, "bundle_sha256": canonical_sha256(core)})


def import_workflow_bundle(
    payload: str | bytes | Mapping[str, Any],
    *,
    workflow_id: str,
    name: str | None = None,
    description: str | None = None,
) -> ImportedWorkflowDraft:
    """Validate and rewrite a bundle into an unpersisted custom draft."""

    value = _decode_bundle(payload)
    _expect_exact(value, _ROOT_FIELDS, "portable bundle")
    if value["schema"] != WORKFLOW_BUNDLE_SCHEMA:
        raise WorkflowTransferError("unsupported portable bundle schema")
    source = value["source"]
    _expect_exact(source, _SOURCE_FIELDS, "portable bundle source")
    resources_value = value["resources"]
    if not isinstance(resources_value, list):
        raise WorkflowTransferError("portable bundle resources must be an array")
    if len(resources_value) > MAX_BUNDLE_RESOURCES:
        raise WorkflowTransferError("portable bundle contains too many resources")
    core = {
        "schema": value["schema"],
        "source": source,
        "definition": value["definition"],
        "resources": resources_value,
    }
    bundle_sha256 = value["bundle_sha256"]
    if not isinstance(bundle_sha256, str) or not is_sha256(bundle_sha256):
        raise WorkflowTransferError("portable bundle digest is invalid")
    if canonical_sha256(core) != bundle_sha256.casefold():
        raise WorkflowTransferError("portable bundle digest does not match its content")
    try:
        definition = WorkflowDefinition.from_dict(value["definition"])
        definition.validate(mode=ValidationMode.LINEAR)
    except (TypeError, ValueError) as exc:
        raise WorkflowTransferError("portable workflow definition is invalid") from exc
    if source["workflow_id"] != definition.workflow_id:
        raise WorkflowTransferError("portable bundle source workflow identity does not match")
    if (
        not isinstance(source["version"], int)
        or isinstance(source["version"], bool)
        or source["version"] <= 0
    ):
        raise WorkflowTransferError("portable bundle source version is invalid")
    if source["definition_sha256"] != definition.sha256():
        raise WorkflowTransferError("portable bundle source definition digest does not match")
    if any(node.node_type is NodeType.LEGACY_STAGE for node in definition.nodes):
        raise WorkflowTransferError("portable import cannot activate legacy-stage nodes")

    referenced = _prompt_refs(definition)
    imported_by_original: dict[str, ImportedPromptResource] = {}
    total_resource_bytes = 0
    for item in resources_value:
        _expect_exact(item, _RESOURCE_FIELDS, "portable Prompt resource")
        resource_id = item["resource_id"]
        digest = item["content_sha256"]
        version = item["version"]
        text = item["content_utf8"]
        if not all(isinstance(part, str) for part in (resource_id, digest, version, text)):
            raise WorkflowTransferError("portable Prompt resource fields must be strings")
        if resource_id in imported_by_original:
            raise WorkflowTransferError("portable bundle contains duplicate Prompt resources")
        content = text.encode("utf-8")
        total_resource_bytes += len(content)
        if not content or b"\x00" in content or len(content) > MAX_PROMPT_BYTES or not is_sha256(digest):
            raise WorkflowTransferError("portable Prompt resource is empty, oversized or invalid")
        if hashlib.sha256(content).hexdigest() != digest.casefold():
            raise WorkflowTransferError("portable Prompt content digest does not match")
        reference = referenced.get(resource_id)
        if reference is None or reference.content_sha256.casefold() != digest.casefold():
            raise WorkflowTransferError("portable Prompt resource is not referenced by the definition")
        imported_by_original[resource_id] = ImportedPromptResource(
            original_resource_id=resource_id,
            resource_id=f"{USER_PROMPT_RESOURCE_PREFIX}{digest.casefold()}",
            content_sha256=digest.casefold(),
            version=digest.casefold()[:12],
            content_utf8=text,
        )
    if set(imported_by_original) != set(referenced):
        raise WorkflowTransferError("portable bundle is missing referenced Prompt resources")
    if total_resource_bytes > MAX_BUNDLE_BYTES:
        raise WorkflowTransferError("portable Prompt resources exceed the aggregate size limit")

    rewritten_nodes = tuple(
        replace(
            node,
            prompt_ref=(
                PromptRef(
                    imported_by_original[node.prompt_ref.resource_id].resource_id,
                    imported_by_original[node.prompt_ref.resource_id].content_sha256,
                    imported_by_original[node.prompt_ref.resource_id].version,
                )
                if node.prompt_ref is not None
                else None
            ),
        )
        for node in definition.nodes
    )
    metadata = {
        key: item
        for key, item in definition.metadata.items()
        if key not in _PROTECTED_METADATA_KEYS
    }
    metadata.update(
        {
            "builtin": False,
            "readonly": False,
            "cloneable": True,
            "imported_from": {
                "workflow_id": definition.workflow_id,
                "version": int(source["version"]),
                "definition_sha256": definition.sha256(),
                "bundle_sha256": bundle_sha256.casefold(),
            },
        }
    )
    rewritten = replace(
        definition,
        workflow_id=workflow_id,
        name=definition.name if name is None else name,
        description=definition.description if description is None else description,
        nodes=rewritten_nodes,
        metadata=metadata,
    )
    try:
        rewritten.validate(mode=ValidationMode.LINEAR)
    except (TypeError, ValueError) as exc:
        raise WorkflowTransferError("rewritten custom workflow definition is invalid") from exc
    return ImportedWorkflowDraft(
        definition=rewritten,
        resources=tuple(imported_by_original[key] for key in sorted(imported_by_original)),
        source_workflow_id=definition.workflow_id,
        source_version=int(source["version"]),
        source_definition_sha256=definition.sha256(),
        bundle_sha256=bundle_sha256.casefold(),
    )


__all__ = [
    "ImportedPromptResource",
    "ImportedWorkflowDraft",
    "MAX_BUNDLE_BYTES",
    "MAX_BUNDLE_RESOURCES",
    "WORKFLOW_BUNDLE_SCHEMA",
    "WorkflowTransferError",
    "export_workflow_bundle",
    "import_workflow_bundle",
]
