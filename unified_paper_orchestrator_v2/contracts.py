from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    RETRYABLE_FAILURE = "retryable_failure"
    FATAL_FAILURE = "fatal_failure"
    CANCELLED = "cancelled"


class ResultStatus(str, Enum):
    COMPLETED = "completed"
    WAITING_HUMAN = "waiting_human"
    BLOCKED = "blocked"
    RETRYABLE_FAILURE = "retryable_failure"
    FATAL_FAILURE = "fatal_failure"


class ApprovalPolicy(str, Enum):
    NONE = "none"
    REQUIRED = "required"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    sha256: str = ""
    schema_version: str = "1"
    media_type: str = "application/octet-stream"

    def validate(self) -> None:
        if not self.uri.strip():
            raise ValueError("artifact uri is required")
        digest = self.sha256.strip().lower()
        if digest and (len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)):
            raise ValueError(f"invalid artifact sha256: {self.sha256}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRef":
        value = cls(
            uri=str(data.get("uri") or ""),
            sha256=str(data.get("sha256") or ""),
            schema_version=str(data.get("schema_version") or "1"),
            media_type=str(data.get("media_type") or "application/octet-stream"),
        )
        value.validate()
        return value

    @classmethod
    def from_file(cls, path: Path, *, schema_version: str = "1", media_type: str = "application/octet-stream") -> "ArtifactRef":
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return cls(uri=str(path.resolve()), sha256=digest, schema_version=schema_version, media_type=media_type)


@dataclass
class TaskEnvelope:
    paper_id: str
    stage: str
    action: str
    executor: str
    dispatch_owner: str = "kernel"
    task_id: str = field(default_factory=lambda: _new_id("task"))
    run_id: str = field(default_factory=lambda: _new_id("run"))
    schema_version: str = "2.0"
    idempotency_key: str = ""
    prompt_version: str = ""
    inputs: list[ArtifactRef] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    writable_roots: list[str] = field(default_factory=list)
    network_access: bool = False
    token_budget: int | None = None
    timeout_seconds: int = 1800
    approval_policy: ApprovalPolicy = ApprovalPolicy.NONE

    def validate(self) -> None:
        for name, value in {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "paper_id": self.paper_id,
            "stage": self.stage,
            "action": self.action,
            "executor": self.executor,
        }.items():
            if not str(value).strip():
                raise ValueError(f"{name} is required")
        if self.schema_version != "2.0":
            raise ValueError(f"unsupported task schema_version: {self.schema_version}")
        if self.dispatch_owner not in {"kernel", "gateway"}:
            raise ValueError(f"unsupported dispatch_owner: {self.dispatch_owner}")
        if self.token_budget is not None and self.token_budget <= 0:
            raise ValueError("token_budget must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for artifact in self.inputs:
            artifact.validate()
        if not self.idempotency_key:
            self.idempotency_key = self.task_id

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        data = asdict(self)
        data["approval_policy"] = self.approval_policy.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskEnvelope":
        value = cls(
            task_id=str(data.get("task_id") or _new_id("task")),
            run_id=str(data.get("run_id") or _new_id("run")),
            paper_id=str(data.get("paper_id") or ""),
            stage=str(data.get("stage") or ""),
            action=str(data.get("action") or ""),
            executor=str(data.get("executor") or ""),
            dispatch_owner=str(data.get("dispatch_owner") or "kernel"),
            schema_version=str(data.get("schema_version") or "2.0"),
            idempotency_key=str(data.get("idempotency_key") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            inputs=[ArtifactRef.from_dict(item) for item in data.get("inputs", [])],
            payload=dict(data.get("payload") or {}),
            allowed_tools=[str(item) for item in data.get("allowed_tools", [])],
            writable_roots=[str(item) for item in data.get("writable_roots", [])],
            network_access=bool(data.get("network_access", False)),
            token_budget=int(data["token_budget"]) if data.get("token_budget") is not None else None,
            timeout_seconds=int(data.get("timeout_seconds") or 1800),
            approval_policy=ApprovalPolicy(str(data.get("approval_policy") or "none")),
        )
        value.validate()
        return value


@dataclass
class TaskResult:
    status: ResultStatus
    message: str = ""
    blocker_code: str = ""
    outputs: list[ArtifactRef] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    external_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskResult":
        return cls(
            status=ResultStatus(str(data.get("status") or "fatal_failure")),
            message=str(data.get("message") or ""),
            blocker_code=str(data.get("blocker_code") or ""),
            outputs=[ArtifactRef.from_dict(item) for item in data.get("outputs", [])],
            evidence=list(data.get("evidence") or []),
            metrics=dict(data.get("metrics") or {}),
            data=dict(data.get("data") or {}),
            external_ref=str(data.get("external_ref") or ""),
        )


@dataclass
class ArtifactManifest:
    paper_id: str
    stage: str
    producer: str
    artifacts: list[ArtifactRef]
    schema_version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.paper_id.strip() or not self.stage.strip() or not self.producer.strip():
            raise ValueError("manifest paper_id, stage, and producer are required")
        if self.schema_version != "1.0":
            raise ValueError(f"unsupported manifest schema_version: {self.schema_version}")
        if not self.artifacts:
            raise ValueError("manifest requires at least one artifact")
        for artifact in self.artifacts:
            artifact.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactManifest":
        value = cls(
            paper_id=str(data.get("paper_id") or ""),
            stage=str(data.get("stage") or ""),
            producer=str(data.get("producer") or ""),
            artifacts=[ArtifactRef.from_dict(item) for item in data.get("artifacts", [])],
            schema_version=str(data.get("schema_version") or "1.0"),
            metadata=dict(data.get("metadata") or {}),
        )
        value.validate()
        return value


@dataclass
class ApprovalRequest:
    task_id: str
    reason: str
    approval_id: str = field(default_factory=lambda: _new_id("approval"))
    status: str = "pending"
    artifact_hashes: list[str] = field(default_factory=list)
    intent_sha256: str = ""
    route_revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
