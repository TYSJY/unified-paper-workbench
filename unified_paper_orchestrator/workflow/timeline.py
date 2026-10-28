"""Sanitized read models for generic workflow run timelines."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .contracts import WorkflowVersion, canonical_sha256
from .store import WorkflowStore


def _fingerprint(domain: str, value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(f"upo-timeline-{domain}/v1\x00{value}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TimelineNodeAttempt:
    node_run_id: str
    node_id: str
    name: str
    node_type: str
    display_order: int
    attempt: int
    status: str
    is_latest: bool
    has_error: bool
    error_code_sha256: str
    output_artifact_ids: tuple[str, ...]
    session_attached: bool
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_run_id": self.node_run_id,
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type,
            "display_order": self.display_order,
            "attempt": self.attempt,
            "status": self.status,
            "is_latest": self.is_latest,
            "has_error": self.has_error,
            "error_code_sha256": self.error_code_sha256,
            "output_artifact_ids": list(self.output_artifact_ids),
            "session_attached": self.session_attached,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class TimelineEvent:
    sequence: int
    event_type: str
    node_run_id: str
    payload_sha256: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "node_run_id": self.node_run_id,
            "payload_sha256": self.payload_sha256,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TimelineArtifact:
    artifact_record_id: str
    node_run_id: str
    producer_node_id: str
    artifact_id: str
    uri: str
    sha256: str
    media_type: str
    schema_ref: str
    size_bytes: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_record_id": self.artifact_record_id,
            "node_run_id": self.node_run_id,
            "producer_node_id": self.producer_node_id,
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TimelineSession:
    session_record_id: str
    session_group: str
    executor: str
    generation: int
    state: str
    external_session_bound: bool
    external_session_sha256: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_record_id": self.session_record_id,
            "session_group": self.session_group,
            "executor": self.executor,
            "generation": self.generation,
            "state": self.state,
            "external_session_bound": self.external_session_bound,
            "external_session_sha256": self.external_session_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class WorkflowRunTimeline:
    run_id: str
    workflow_id: str
    workflow_name: str
    workflow_version_id: str
    workflow_version: int
    definition_sha256: str
    status: str
    cancel_requested: bool
    completed_nodes: int
    total_nodes: int
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str
    node_attempts: tuple[TimelineNodeAttempt, ...]
    events: tuple[TimelineEvent, ...]
    artifacts: tuple[TimelineArtifact, ...]
    sessions: tuple[TimelineSession, ...]

    @property
    def progress(self) -> float:
        return self.completed_nodes / self.total_nodes if self.total_nodes else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "workflow_version_id": self.workflow_version_id,
            "workflow_version": self.workflow_version,
            "definition_sha256": self.definition_sha256,
            "status": self.status,
            "cancel_requested": self.cancel_requested,
            "completed_nodes": self.completed_nodes,
            "total_nodes": self.total_nodes,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "node_attempts": [item.to_dict() for item in self.node_attempts],
            "events": [item.to_dict() for item in self.events],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "sessions": [item.to_dict() for item in self.sessions],
        }


class WorkflowTimelineService:
    def __init__(self, store: WorkflowStore) -> None:
        if not isinstance(store, WorkflowStore):
            raise TypeError("store must be a WorkflowStore")
        self.store = store

    def snapshot(self, run_id: str) -> WorkflowRunTimeline:
        raw = self.store.read_run_snapshot(run_id)
        run = raw["run"]
        version: WorkflowVersion = raw["version"]["version"]
        latest_attempt = {
            item["node_id"]: max(
                candidate["attempt"]
                for candidate in raw["node_attempts"]
                if candidate["node_id"] == item["node_id"]
            )
            for item in raw["node_attempts"]
        }
        attempts = tuple(
            TimelineNodeAttempt(
                node_run_id=item["node_run_id"],
                node_id=item["node_id"],
                name=item["node"].name,
                node_type=item["node"].node_type.value,
                display_order=item["display_order"],
                attempt=item["attempt"],
                status=item["status"],
                is_latest=item["attempt"] == latest_attempt[item["node_id"]],
                has_error=bool(item["error_code"] or item["error_message"]),
                error_code_sha256=_fingerprint("error-code", item["error_code"]),
                output_artifact_ids=tuple(sorted(item["outputs"])),
                session_attached=bool(item["session_id"]),
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                started_at=item["started_at"],
                finished_at=item["finished_at"],
            )
            for item in raw["node_attempts"]
        )
        latest = [item for item in attempts if item.is_latest]
        completed_nodes = sum(item.status in {"completed", "skipped"} for item in latest)
        events = tuple(
            TimelineEvent(
                sequence=item["sequence"],
                event_type=item["event_type"],
                node_run_id=item["node_run_id"],
                payload_sha256=canonical_sha256(item["payload"]),
                created_at=item["created_at"],
            )
            for item in raw["events"]
        )
        artifacts = tuple(
            TimelineArtifact(
                artifact_record_id=item["artifact_record_id"],
                node_run_id=item["node_run_id"],
                producer_node_id=item["producer_node_id"],
                artifact_id=item["artifact_id"],
                uri=item["uri"],
                sha256=item["sha256"],
                media_type=item["media_type"],
                schema_ref=item["schema_ref"],
                size_bytes=item["size_bytes"],
                created_at=item["created_at"],
            )
            for item in raw["artifacts"]
        )
        sessions = tuple(
            TimelineSession(
                session_record_id=item["session_record_id"],
                session_group=item["session_group"],
                executor=item["executor"],
                generation=item["generation"],
                state=item["state"],
                external_session_bound=bool(item["external_session_id"]),
                external_session_sha256=_fingerprint(
                    "external-session", item["external_session_id"]
                ),
                created_at=item["created_at"],
                updated_at=item["updated_at"],
            )
            for item in raw["sessions"]
        )
        return WorkflowRunTimeline(
            run_id=run["run_id"],
            workflow_id=version.workflow_id,
            workflow_name=version.definition.name,
            workflow_version_id=version.version_id,
            workflow_version=version.version,
            definition_sha256=version.definition_sha256,
            status=run["status"],
            cancel_requested=run["cancel_requested"],
            completed_nodes=completed_nodes,
            total_nodes=len(latest),
            created_at=run["created_at"],
            updated_at=run["updated_at"],
            started_at=run["started_at"],
            finished_at=run["finished_at"],
            node_attempts=attempts,
            events=events,
            artifacts=artifacts,
            sessions=sessions,
        )


__all__ = [
    "TimelineArtifact",
    "TimelineEvent",
    "TimelineNodeAttempt",
    "TimelineSession",
    "WorkflowRunTimeline",
    "WorkflowTimelineService",
]
