"""Read-only legacy runtime observation and field-level parity comparison.

This module is intentionally disconnected from the production composition
root.  It never imports a legacy runtime, acquires its locks, creates a
``WorkflowRun`` or changes an artifact.  Callers provide explicit source files,
and an observation is emitted only after two identical source revisions are
seen around the projection window.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .contracts import NodeRunStatus, WorkflowDefinition, canonical_json, canonical_sha256, is_sha256


SHADOW_PROJECTOR_VERSION = "legacy-shadow-projector-v1"
SHADOW_COMPARATOR_VERSION = "legacy-shadow-comparator-v1"
_SAFE_STAGE_ALIASES = {
    "writing": "writing",
    "figures": "figures",
    "figure": "figures",
    "journal-author": "journal-author",
    "journal_author": "journal-author",
    "journal": "journal-author",
    "smart-submission": "smart-submission",
    "smart_submission": "smart-submission",
    "submission": "smart-submission",
}
_COMPLETE = {"complete", "completed", "success", "succeeded", "done", "accepted", "submitted"}
_RUNNING = {"running", "starting", "creating_thread", "waiting_event", "monitoring"}
_QUEUED = {"queued", "pending", "ready", "reserved"}
_WAITING = {"waiting_data", "needs_user", "migration_review", "waiting_human", "waiting-human"}
_BLOCKED = {"blocked", "error", "failed", "failure", "incomplete"}
_PAUSED = {"paused", "stopped"}
_CANCELLED = {"cancelled", "canceled", "deleted"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_BLOCKER_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_SOURCE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHADOW_FIELDS = frozenset(
    {
        "source.coherent",
        "workflow.node_ids",
        "workflow.session_continuity",
        "node.present",
        "node.status",
        "node.model",
        "node.reasoning_effort",
        "node.token_limit",
        "node.token_usage",
        "node.blocker",
        "node.artifacts",
        "node.session",
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|access[_-]?token|auth(?:orization)?)\b\s*[:=]\s*[^\s,;]+"
)
_TOKEN_RE = re.compile(r"(?i)\b(?:sk|Bearer)[-_ ][A-Za-z0-9._-]{8,}\b")


class LegacyShadowError(RuntimeError):
    """Base error for an isolated shadow scan."""


class LegacyShadowSourceError(LegacyShadowError):
    def __init__(self, logical_name: str, code: str, message: str) -> None:
        self.logical_name = logical_name
        self.code = code
        super().__init__(message)


def _stage(value: str) -> str:
    key = str(value or "").strip().casefold()
    try:
        return _SAFE_STAGE_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unsupported legacy shadow stage: {value!r}") from exc


def _safe_text(value: Any, *, limit: int = 512) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _TOKEN_RE.sub("[REDACTED]", text)
    return " ".join(text.split())[:limit]


def _blocker_code(value: Any) -> str:
    code = str(value or "").strip()
    if not code:
        return ""
    return code if _BLOCKER_CODE_RE.fullmatch(code) else "LEGACY_BLOCKER_UNCLASSIFIED"


def _text_fingerprint(domain: str, value: Any) -> str:
    """Hash legacy-controlled text with a field-specific domain separator."""

    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(f"upo-shadow-{domain}/v1\x00{text}".encode("utf-8")).hexdigest()


def _safe_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _safe_json(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _portable_uri(value: Any) -> str:
    raw = str(value or "").strip()
    if "\\" in raw:
        raise ValueError("shadow artifact URI must be a canonical workspace-relative path")
    uri = raw
    parsed = PurePosixPath(uri)
    if (
        not uri
        or parsed.is_absolute()
        or parsed.as_posix() != uri
        or any(part in {"", ".", ".."} or ":" in part for part in parsed.parts)
    ):
        raise ValueError("shadow artifact URI must be a canonical workspace-relative path")
    return uri


def _digest_thread(value: Any) -> str:
    thread_id = str(value or "").strip()
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest() if thread_id else ""


@dataclass(frozen=True)
class LegacySource:
    logical_name: str
    path: str | Path
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.logical_name, str) or _SOURCE_NAME_RE.fullmatch(self.logical_name) is None:
            raise ValueError("legacy source logical_name must be a lowercase logical identifier")
        if not isinstance(self.path, (str, Path)) or not os.fspath(self.path):
            raise ValueError("legacy source path is required")
        object.__setattr__(self, "path", Path(self.path).expanduser().absolute())


@dataclass(frozen=True)
class SourceRevision:
    logical_name: str
    sha256: str = ""
    schema: str = ""
    missing: bool = False

    def __post_init__(self) -> None:
        if _SOURCE_NAME_RE.fullmatch(self.logical_name) is None:
            raise ValueError("source revision logical_name must be a lowercase logical identifier")
        if self.sha256 and not is_sha256(self.sha256):
            raise ValueError("source revision sha256 must be a SHA-256 digest")
        if not self.missing and not self.sha256:
            raise ValueError("present source revision requires a SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "sha256": self.sha256,
            "schema": self.schema,
            "missing": self.missing,
        }


@dataclass(frozen=True)
class LegacySourceSnapshot:
    revision: SourceRevision
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(_safe_json(dict(self.data))))


@dataclass(frozen=True)
class SnapshotBundle:
    stage: str
    paper_id: str
    sources: tuple[LegacySourceSnapshot, ...]
    coherent: bool
    attempts: int
    issue: str = ""

    @property
    def revisions(self) -> tuple[SourceRevision, ...]:
        return tuple(item.revision for item in self.sources)


@dataclass(frozen=True)
class LegacyArtifactObservation:
    artifact_id: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    schema_ref: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LegacyArtifactObservation":
        artifact_id = _safe_text(value.get("artifact_id"), limit=200)
        digest = str(value.get("sha256") or "").strip().lower()
        size = _safe_int(value.get("size_bytes"))
        if not artifact_id or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid shadow artifact identity")
        if size is None:
            raise ValueError("invalid shadow artifact size")
        return cls(
            artifact_id=artifact_id,
            uri=_portable_uri(value.get("uri")),
            sha256=digest,
            size_bytes=size,
            media_type=_safe_text(value.get("media_type") or "application/octet-stream", limit=200),
            schema_ref=_safe_text(value.get("schema_ref"), limit=200),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
        }


@dataclass(frozen=True)
class LegacyStepObservation:
    legacy_step: str
    node_id: str
    raw_status: str
    normalized_status: str
    session_fingerprint: str = ""
    model: str = ""
    reasoning_effort: str = ""
    token_limit_kind: str = "missing"
    token_limit: int | None = None
    token_usage: Mapping[str, int] = field(default_factory=dict)
    blocker: str = ""
    blocker_summary: str = ""
    artifacts: tuple[LegacyArtifactObservation, ...] = ()
    artifacts_checked: bool = False

    def __post_init__(self) -> None:
        if self.token_limit_kind not in {"missing", "unlimited", "limited"}:
            raise ValueError("invalid token_limit_kind")
        if self.token_limit_kind == "limited" and (self.token_limit is None or self.token_limit <= 0):
            raise ValueError("limited token contract requires a positive value")
        usage = {
            str(key): int(value)
            for key, value in self.token_usage.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        object.__setattr__(self, "token_usage", MappingProxyType(usage))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_step": self.legacy_step,
            "node_id": self.node_id,
            "raw_status": self.raw_status,
            "normalized_status": self.normalized_status,
            "session_fingerprint": self.session_fingerprint,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "token_limit_kind": self.token_limit_kind,
            "token_limit": self.token_limit,
            "token_usage": dict(self.token_usage),
            "blocker": self.blocker,
            "blocker_summary": self.blocker_summary,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "artifacts_checked": self.artifacts_checked,
        }


@dataclass(frozen=True)
class LegacyObservation:
    stage: str
    paper_id: str
    source_revisions: tuple[SourceRevision, ...]
    coherent: bool
    workflow_version_id: str
    completed_step: int
    active_step: int
    steps: tuple[LegacyStepObservation, ...]
    issue: str = ""
    evidence_revision_sha256: str = ""
    projector_version: str = SHADOW_PROJECTOR_VERSION

    def __post_init__(self) -> None:
        if self.evidence_revision_sha256 and not is_sha256(self.evidence_revision_sha256):
            raise ValueError("evidence_revision_sha256 must be a SHA-256 digest")

    @property
    def source_revision_sha256(self) -> str:
        return canonical_sha256(
            {
                "source_revisions": [item.to_dict() for item in self.source_revisions],
                "evidence_revision_sha256": self.evidence_revision_sha256,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "paper_id": self.paper_id,
            "source_revisions": [item.to_dict() for item in self.source_revisions],
            "coherent": self.coherent,
            "workflow_version_id": self.workflow_version_id,
            "completed_step": self.completed_step,
            "active_step": self.active_step,
            "steps": [item.to_dict() for item in self.steps],
            "issue": self.issue,
            "evidence_revision_sha256": self.evidence_revision_sha256,
            "projector_version": self.projector_version,
        }

    def to_storage_payload(self) -> dict[str, Any]:
        """Return a store-safe whitelist without raw subject or message data."""

        return {
            "schema": "workflow-shadow-observation-payload-v1",
            "coherent": self.coherent,
            "workflow_version_sha256": _text_fingerprint(
                "workflow-version", self.workflow_version_id
            ),
            "completed_step": self.completed_step,
            "active_step": self.active_step,
            "source_revisions": [
                {
                    "logical_name_sha256": _text_fingerprint(
                        "source-name", item.logical_name
                    ),
                    "content_sha256": item.sha256,
                    "schema_sha256": _text_fingerprint("source-schema", item.schema),
                    "missing": item.missing,
                }
                for item in self.source_revisions
            ],
            "evidence_revision_sha256": self.evidence_revision_sha256,
            "issue_sha256": _text_fingerprint("issue", self.issue),
            "steps": [
                {
                    "legacy_step_sha256": _text_fingerprint(
                        "legacy-step", item.legacy_step
                    ),
                    "node_id_sha256": _text_fingerprint("node-id", item.node_id),
                    "raw_status_sha256": _text_fingerprint(
                        "raw-status", item.raw_status
                    ),
                    "normalized_status": item.normalized_status,
                    "session_fingerprint": item.session_fingerprint,
                    "model_sha256": _text_fingerprint("model", item.model),
                    "reasoning_effort_sha256": _text_fingerprint(
                        "reasoning-effort", item.reasoning_effort
                    ),
                    "token_limit_kind": item.token_limit_kind,
                    "token_limit": item.token_limit,
                    "token_usage": [
                        {
                            "metric_sha256": _text_fingerprint("usage-metric", key),
                            "value": value,
                        }
                        for key, value in sorted(item.token_usage.items())
                    ],
                    "has_blocker": bool(item.blocker),
                    "blocker_code_sha256": _text_fingerprint(
                        "blocker-code", item.blocker
                    ),
                    "blocker_summary_sha256": (
                        _text_fingerprint("blocker-summary", item.blocker_summary)
                    ),
                    "artifacts": [
                        {
                            "artifact_id_sha256": _text_fingerprint(
                                "artifact-id", artifact.artifact_id
                            ),
                            "relative_uri_sha256": _text_fingerprint(
                                "artifact-uri", artifact.uri
                            ),
                            "content_sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                            "media_type_sha256": _text_fingerprint(
                                "artifact-media-type", artifact.media_type
                            ),
                            "schema_ref_sha256": _text_fingerprint(
                                "artifact-schema-ref", artifact.schema_ref
                            ),
                        }
                        for artifact in item.artifacts
                    ],
                    "artifacts_checked": item.artifacts_checked,
                }
                for item in self.steps
            ],
        }


@dataclass(frozen=True)
class ShadowFieldResult:
    field: str
    verdict: str
    expected: Any
    observed: Any
    node_id: str = ""
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.field not in _SHADOW_FIELDS:
            raise ValueError("invalid shadow comparison field")
        if self.verdict not in {"exact", "equivalent", "unknown", "mismatch"}:
            raise ValueError("invalid shadow verdict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "verdict": self.verdict,
            "expected": _safe_json(self.expected),
            "observed": _safe_json(self.observed),
            "node_id": self.node_id,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ShadowComparison:
    stage: str
    paper_id: str
    definition_sha256: str
    verdict: str
    fields: tuple[ShadowFieldResult, ...]
    comparator_version: str = SHADOW_COMPARATOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "paper_id": self.paper_id,
            "definition_sha256": self.definition_sha256,
            "verdict": self.verdict,
            "fields": [item.to_dict() for item in self.fields],
            "comparator_version": self.comparator_version,
        }

    def to_storage_payload(self) -> dict[str, Any]:
        return {
            "schema": "workflow-shadow-comparison-payload-v1",
            "verdict": self.verdict,
            "fields": [
                {
                    "field": item.field,
                    "verdict": item.verdict,
                    "expected_sha256": canonical_sha256(item.expected),
                    "observed_sha256": canonical_sha256(item.observed),
                    "node_id_sha256": _text_fingerprint("node-id", item.node_id),
                    "evidence_sha256": (
                        _text_fingerprint("comparison-evidence", item.evidence)
                    ),
                }
                for item in self.fields
            ],
        }


def _read_source(source: LegacySource) -> LegacySourceSnapshot:
    path = Path(source.path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if source.required:
            raise LegacyShadowSourceError(source.logical_name, "source_missing", "required shadow source is missing")
        return LegacySourceSnapshot(SourceRevision(source.logical_name, missing=True), {})
    except OSError as exc:
        raise LegacyShadowSourceError(
            source.logical_name,
            "source_unreadable",
            f"shadow source cannot be read ({type(exc).__name__})",
        ) from exc
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyShadowSourceError(
            source.logical_name,
            "source_invalid_json",
            "shadow source is not valid JSON",
        ) from exc
    if not isinstance(value, Mapping):
        raise LegacyShadowSourceError(
            source.logical_name,
            "source_invalid_shape",
            "shadow source top level must be an object",
        )
    schema = _safe_text(value.get("schema"), limit=200)
    return LegacySourceSnapshot(
        SourceRevision(source.logical_name, hashlib.sha256(raw).hexdigest(), schema, False),
        value,
    )


def capture_once(
    stage: str,
    paper_id: str,
    *,
    sources: Sequence[LegacySource],
    attempts: int = 3,
) -> SnapshotBundle:
    """Double-read explicit sources until a coherent read-only snapshot exists."""

    normalized_stage = _stage(stage)
    if not str(paper_id or "").strip():
        raise ValueError("paper_id is required")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise ValueError("attempts must be between 1 and 10")
    source_values = tuple(sources)
    if not source_values or any(not isinstance(item, LegacySource) for item in source_values):
        raise ValueError("at least one LegacySource is required")
    names = [item.logical_name for item in source_values]
    if len(names) != len(set(names)):
        raise ValueError("legacy source logical names must be unique")
    last: tuple[LegacySourceSnapshot, ...] = ()
    for current_attempt in range(1, attempts + 1):
        before = tuple(_read_source(item) for item in source_values)
        after = tuple(_read_source(item) for item in source_values)
        last = after
        if [item.revision for item in before] == [item.revision for item in after]:
            return SnapshotBundle(
                normalized_stage,
                str(paper_id).strip(),
                before,
                True,
                current_attempt,
            )
    return SnapshotBundle(
        normalized_stage,
        str(paper_id).strip(),
        last,
        False,
        attempts,
        "unstable_snapshot",
    )


ArtifactEvidenceReader = Callable[
    [SnapshotBundle, WorkflowDefinition],
    Mapping[str, Sequence[Mapping[str, Any]]],
]


def observe_once(
    stage: str,
    paper_id: str,
    workflow_definition: WorkflowDefinition,
    *,
    sources: Sequence[LegacySource],
    artifact_reader: ArtifactEvidenceReader | None = None,
    attempts: int = 3,
) -> LegacyObservation:
    """Bracket projection and optional artifact reads with source revisions."""

    normalized_stage = _stage(stage)
    if not str(paper_id or "").strip():
        raise ValueError("paper_id is required")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise ValueError("attempts must be between 1 and 10")
    source_values = tuple(sources)
    if not source_values or any(not isinstance(item, LegacySource) for item in source_values):
        raise ValueError("at least one LegacySource is required")
    names = [item.logical_name for item in source_values]
    if len(names) != len(set(names)):
        raise ValueError("legacy source logical names must be unique")
    last_after: tuple[LegacySourceSnapshot, ...] = ()
    for current_attempt in range(1, attempts + 1):
        before = tuple(_read_source(item) for item in source_values)
        bundle = SnapshotBundle(
            normalized_stage,
            str(paper_id).strip(),
            before,
            True,
            current_attempt,
        )
        evidence = artifact_reader(bundle, workflow_definition) if artifact_reader is not None else None
        observation = project_legacy(bundle, workflow_definition, artifact_evidence=evidence)
        after = tuple(_read_source(item) for item in source_values)
        last_after = after
        if [item.revision for item in before] == [item.revision for item in after]:
            return observation
    unstable = SnapshotBundle(
        normalized_stage,
        str(paper_id).strip(),
        last_after,
        False,
        attempts,
        "unstable_snapshot",
    )
    return project_legacy(unstable, workflow_definition)


def _find_record(container: Any, paper_id: str) -> Mapping[str, Any]:
    values = _mapping(container)
    exact = values.get(paper_id)
    if isinstance(exact, Mapping):
        return exact
    candidates: list[Mapping[str, Any]] = []
    for key, value in values.items():
        if not isinstance(value, Mapping):
            continue
        identities = {
            str(key),
            str(value.get("paper_id") or ""),
            str(value.get("pipeline_id") or ""),
            str(value.get("paper_key") or ""),
        }
        if paper_id in identities or str(key).endswith(f":{paper_id}"):
            candidates.append(value)
    return candidates[0] if len(candidates) == 1 else {}


def _records(bundle: SnapshotBundle) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    pipeline: Mapping[str, Any] = {}
    journal: Mapping[str, Any] = {}
    for snapshot in bundle.sources:
        name = snapshot.revision.logical_name.casefold().replace("_", "-")
        data = snapshot.data
        if "journal" in name or "sdk" in name:
            candidate = _find_record(data.get("papers"), bundle.paper_id)
            if candidate:
                journal = candidate
        elif bundle.stage == "writing":
            candidate = _find_record(data.get("sessions"), bundle.paper_id)
            if candidate:
                pipeline = candidate
        else:
            candidate = _find_record(data.get("papers"), bundle.paper_id)
            if candidate:
                pipeline = candidate
    return pipeline, journal


def _normalize_status(value: Any) -> str:
    raw = str(value or "").strip().casefold().replace("-", "_")
    if raw in _COMPLETE:
        return NodeRunStatus.COMPLETED.value
    if raw in _RUNNING:
        return NodeRunStatus.RUNNING.value
    if raw in _QUEUED:
        return NodeRunStatus.READY.value if raw == "ready" else NodeRunStatus.PENDING.value
    if raw in _WAITING:
        return NodeRunStatus.WAITING_HUMAN.value
    if raw in _PAUSED:
        return NodeRunStatus.BLOCKED.value
    if raw in _CANCELLED:
        return NodeRunStatus.CANCELLED.value
    if raw in _BLOCKED:
        return NodeRunStatus.BLOCKED.value
    return "unknown" if raw else NodeRunStatus.PENDING.value


def _step_record(record: Mapping[str, Any], position: int, legacy_step: str) -> Mapping[str, Any]:
    steps = _mapping(record.get("steps"))
    for key in (str(position), legacy_step, f"step-{position}", f"step_{position}"):
        value = steps.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _profile(record: Mapping[str, Any], step: Mapping[str, Any]) -> Mapping[str, Any]:
    for candidate in (
        step.get("effective_execution_profile"),
        step.get("execution_profile"),
        record.get("effective_execution_profile"),
        record.get("execution_profile"),
    ):
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _token_contract(profile: Mapping[str, Any]) -> tuple[str, int | None]:
    marker = object()
    value = profile.get("hard_token_limit", marker)
    if value is marker:
        value = profile.get("token_limit", marker)
    if value is marker or value is None or value == "":
        return "missing", None
    limit = _safe_int(value)
    if limit is None:
        return "missing", None
    return ("unlimited", None) if limit == 0 else ("limited", limit)


def _usage(record: Mapping[str, Any], step: Mapping[str, Any]) -> Mapping[str, int]:
    result: dict[str, int] = {}
    for prefix, candidate in (
        ("last", step.get("usage")),
        ("last", step.get("last_usage")),
        ("total", step.get("total_usage")),
        ("total", record.get("usage")),
    ):
        if not isinstance(candidate, Mapping):
            continue
        for key, value in candidate.items():
            number = _safe_int(value)
            if number is not None:
                result[f"{prefix}.{str(key)[:80]}"] = number
    return result


def project_legacy(
    bundle: SnapshotBundle,
    workflow_definition: WorkflowDefinition,
    *,
    artifact_evidence: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> LegacyObservation:
    """Project explicit legacy source bytes into the generic node vocabulary."""

    if workflow_definition.workflow_id.casefold().find(bundle.stage.replace("journal-author", "journal-author")) < 0:
        expected_stage = str(workflow_definition.metadata.get("legacy_stage") or "").replace("_", "-")
        if expected_stage and expected_stage != bundle.stage:
            raise ValueError("workflow definition does not describe the captured legacy stage")
    pipeline, journal = _records(bundle)
    authority = journal or pipeline
    completed = _safe_int(journal.get("completed_step"))
    if completed is None:
        completed = _safe_int(journal.get("legacy_step_done"))
    if completed is None:
        completed = _safe_int(pipeline.get("step_done")) or 0
    active = _safe_int(journal.get("active_step"))
    if active is None:
        active = _safe_int(pipeline.get("sdk_current_step")) or 0
    if bundle.stage == "journal-author":
        completed = 1 if completed >= 3 or str(authority.get("status") or "").casefold() in _COMPLETE else 0
        active = 1 if active else 0
    if bundle.stage == "smart-submission":
        status = str(authority.get("status") or authority.get("workflow_status") or "")
        completed = 1 if status.casefold() in _COMPLETE else 0
        active = 1 if status and not completed else 0
    completed = min(completed, len(workflow_definition.nodes))
    active = min(active, len(workflow_definition.nodes))

    workflow_snapshot = _mapping(authority.get("workflow_snapshot"))
    version_id = _safe_text(
        workflow_snapshot.get("version_id")
        or authority.get("workflow_version_id")
        or pipeline.get("workflow_version_id"),
        limit=200,
    )
    global_raw = _safe_text(
        authority.get("status") or authority.get("workflow_status") or pipeline.get("status"),
        limit=100,
    )
    global_thread = authority.get("thread_id") or pipeline.get("thread_id")
    evidence = artifact_evidence or {}
    known_node_ids = {item.node_id for item in workflow_definition.nodes}
    unknown_evidence = set(evidence) - known_node_ids
    if unknown_evidence:
        raise ValueError("artifact evidence contains an unknown workflow node")
    steps: list[LegacyStepObservation] = []
    ordered_nodes = sorted(workflow_definition.nodes, key=lambda item: (item.display_order, item.node_id))
    for position, node in enumerate(ordered_nodes, start=1):
        legacy_step = str(node.metadata.get("legacy_step") or ("program" if bundle.stage == "smart-submission" else position))
        step_value = _step_record(authority, position, legacy_step)
        raw_status = _safe_text(step_value.get("state") or step_value.get("status"), limit=100)
        if not raw_status:
            if position <= completed:
                raw_status = "complete"
            elif position == active:
                raw_status = global_raw or "running"
            else:
                raw_status = "pending"
        profile = _profile(authority, step_value)
        model = _safe_text(profile.get("model") or step_value.get("model") or authority.get("model"), limit=200)
        effort = _safe_text(
            profile.get("reasoning_effort") or step_value.get("reasoning_effort"),
            limit=40,
        ).casefold()
        token_kind, token_limit = _token_contract(profile)
        blocker = _blocker_code(
            step_value.get("blocker")
            or step_value.get("blocker_code")
            or step_value.get("error_code")
            or authority.get("blocker")
        )
        blocker_summary = _safe_text(
            step_value.get("blocker_message")
            or step_value.get("message")
            or authority.get("blocker_message"),
            limit=300,
        )
        raw_artifacts = evidence.get(node.node_id, ())
        artifacts = tuple(LegacyArtifactObservation.from_mapping(item) for item in raw_artifacts)
        steps.append(
            LegacyStepObservation(
                legacy_step=legacy_step,
                node_id=node.node_id,
                raw_status=raw_status,
                normalized_status=_normalize_status(raw_status),
                session_fingerprint=_digest_thread(step_value.get("thread_id") or global_thread),
                model=model,
                reasoning_effort=effort,
                token_limit_kind=token_kind,
                token_limit=token_limit,
                token_usage=_usage(authority, step_value),
                blocker=blocker,
                blocker_summary=blocker_summary,
                artifacts=artifacts,
                artifacts_checked=node.node_id in evidence,
            )
        )
    normalized_evidence = {
        item.node_id: [artifact.to_dict() for artifact in item.artifacts]
        for item in steps
        if item.artifacts_checked
    }
    evidence_revision_sha256 = (
        canonical_sha256(normalized_evidence) if artifact_evidence is not None else ""
    )
    return LegacyObservation(
        stage=bundle.stage,
        paper_id=bundle.paper_id,
        source_revisions=bundle.revisions,
        coherent=bundle.coherent,
        workflow_version_id=version_id,
        completed_step=completed,
        active_step=active,
        steps=tuple(steps),
        issue=bundle.issue,
        evidence_revision_sha256=evidence_revision_sha256,
    )


def _result(
    field: str,
    verdict: str,
    expected: Any,
    observed: Any,
    *,
    node_id: str = "",
    evidence: str = "",
) -> ShadowFieldResult:
    return ShadowFieldResult(field, verdict, expected, observed, node_id, evidence)


def _direct_verdict(expected: Any, observed: Any) -> str:
    if expected is None or expected == "" or observed is None or observed == "":
        return "unknown"
    return "exact" if expected == observed else "mismatch"


def compare_legacy(
    observation: LegacyObservation,
    workflow_definition: WorkflowDefinition,
    *,
    shadow_steps: Mapping[str, Mapping[str, Any]] | None = None,
) -> ShadowComparison:
    """Compare one observation to the frozen template or explicit shadow rows."""

    fields: list[ShadowFieldResult] = []
    fields.append(
        _result(
            "source.coherent",
            "exact" if observation.coherent else "unknown",
            True,
            observation.coherent,
            evidence=observation.issue,
        )
    )
    if not observation.coherent:
        return ShadowComparison(
            observation.stage,
            observation.paper_id,
            workflow_definition.sha256(),
            "unknown",
            tuple(fields),
        )
    expected_nodes = sorted(workflow_definition.nodes, key=lambda item: (item.display_order, item.node_id))
    observed_by_node = {item.node_id: item for item in observation.steps}
    fields.append(
        _result(
            "workflow.node_ids",
            "exact" if [item.node_id for item in expected_nodes] == [item.node_id for item in observation.steps] else "mismatch",
            [item.node_id for item in expected_nodes],
            [item.node_id for item in observation.steps],
        )
    )
    for position, node in enumerate(expected_nodes, start=1):
        observed = observed_by_node.get(node.node_id)
        if observed is None:
            fields.append(_result("node.present", "mismatch", True, False, node_id=node.node_id))
            continue
        explicit = _mapping((shadow_steps or {}).get(node.node_id))
        expected_status = explicit.get("status")
        if expected_status is None:
            expected_status = (
                NodeRunStatus.COMPLETED.value
                if position <= observation.completed_step
                else NodeRunStatus.RUNNING.value
                if position == observation.active_step
                else NodeRunStatus.PENDING.value
            )
        fields.append(
            _result(
                "node.status",
                _direct_verdict(expected_status, observed.normalized_status),
                expected_status,
                observed.normalized_status,
                node_id=node.node_id,
            )
        )
        expected_model = explicit.get("model", node.execution_profile.model)
        fields.append(
            _result(
                "node.model",
                _direct_verdict(expected_model, observed.model),
                expected_model,
                observed.model,
                node_id=node.node_id,
            )
        )
        expected_effort = explicit.get("reasoning_effort", node.execution_profile.reasoning_effort)
        fields.append(
            _result(
                "node.reasoning_effort",
                _direct_verdict(expected_effort, observed.reasoning_effort),
                expected_effort,
                observed.reasoning_effort,
                node_id=node.node_id,
            )
        )
        if "token_limit" in explicit:
            expected_token = explicit.get("token_limit")
        else:
            expected_token = node.execution_profile.token_limit
        if observed.token_limit_kind == "missing":
            token_verdict = "unknown"
            token_observed: Any = "missing"
        elif expected_token is None and observed.token_limit_kind == "unlimited":
            token_verdict = "equivalent"
            token_observed = 0
        else:
            token_observed = observed.token_limit
            token_verdict = "exact" if expected_token == token_observed else "mismatch"
        fields.append(
            _result(
                "node.token_limit",
                token_verdict,
                expected_token,
                token_observed,
                node_id=node.node_id,
            )
        )
        expected_blocker = _blocker_code(explicit.get("blocker")) if explicit else None
        fields.append(
            _result(
                "node.blocker",
                _direct_verdict(expected_blocker, observed.blocker) if explicit else "unknown",
                expected_blocker,
                observed.blocker,
                node_id=node.node_id,
            )
        )
        expected_usage = explicit.get("token_usage") if explicit else None
        fields.append(
            _result(
                "node.token_usage",
                _direct_verdict(expected_usage, dict(observed.token_usage)) if expected_usage is not None else "unknown",
                expected_usage,
                dict(observed.token_usage),
                node_id=node.node_id,
            )
        )
        observed_artifact_records = [item.to_dict() for item in observed.artifacts]
        if "artifacts" in explicit:
            expected_artifact_records = list(explicit.get("artifacts") or [])
            artifact_verdict = (
                "exact"
                if canonical_json(expected_artifact_records) == canonical_json(observed_artifact_records)
                else "mismatch"
            )
            artifact_expected: Any = expected_artifact_records
            artifact_observed: Any = observed_artifact_records
        else:
            expected_artifacts = {spec.artifact_id for spec in node.outputs if spec.required}
            observed_artifacts = {item.artifact_id for item in observed.artifacts}
            if not observed.artifacts_checked:
                artifact_verdict = "unknown"
            elif expected_artifacts.issubset(observed_artifacts):
                artifact_verdict = "exact"
            else:
                artifact_verdict = "mismatch"
            artifact_expected = sorted(expected_artifacts)
            artifact_observed = sorted(observed_artifacts)
        fields.append(
            _result(
                "node.artifacts",
                artifact_verdict,
                artifact_expected,
                artifact_observed,
                node_id=node.node_id,
            )
        )
        if explicit:
            expected_session = explicit.get("session_fingerprint")
            fields.append(
                _result(
                    "node.session",
                    _direct_verdict(expected_session, observed.session_fingerprint),
                    expected_session,
                    observed.session_fingerprint,
                    node_id=node.node_id,
                )
            )
    session_values = {item.session_fingerprint for item in observation.steps if item.session_fingerprint}
    fields.append(
        _result(
            "workflow.session_continuity",
            "unknown" if not session_values else "equivalent" if len(session_values) == 1 else "mismatch",
            "one fixed session fingerprint",
            sorted(session_values),
        )
    )
    verdicts = {item.verdict for item in fields}
    overall = (
        "mismatch"
        if "mismatch" in verdicts
        else "unknown"
        if "unknown" in verdicts
        else "equivalent"
        if "equivalent" in verdicts
        else "exact"
    )
    return ShadowComparison(
        observation.stage,
        observation.paper_id,
        workflow_definition.sha256(),
        overall,
        tuple(fields),
    )


__all__ = [
    "ArtifactEvidenceReader",
    "LegacyArtifactObservation",
    "LegacyObservation",
    "LegacyShadowError",
    "LegacyShadowSourceError",
    "LegacySource",
    "LegacySourceSnapshot",
    "LegacyStepObservation",
    "SHADOW_COMPARATOR_VERSION",
    "SHADOW_PROJECTOR_VERSION",
    "ShadowComparison",
    "ShadowFieldResult",
    "SnapshotBundle",
    "SourceRevision",
    "capture_once",
    "compare_legacy",
    "observe_once",
    "project_legacy",
]
