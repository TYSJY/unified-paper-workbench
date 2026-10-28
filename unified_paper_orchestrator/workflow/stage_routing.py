"""Fail-closed candidate routing contracts for a future staged cutover.

This module is intentionally disconnected from the production composition
root.  It owns policy and admission decisions only; callers must inject both
backends explicitly.  The separate process latch defaults to closed so a
persisted candidate policy cannot enable the generic backend after a restart.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from .contracts import (
    NodeType,
    WorkflowRuntimeBinding,
    WorkflowVersion,
    WorkflowVersionStatus,
    canonical_json,
    canonical_sha256,
    is_sha256,
)


STAGE_ROUTING_SCHEMA = "unified-paper-orchestrator/stage-routing/v1"
REQUIRED_COMPARATOR_VERSION = "legacy-shadow-comparator-v1"
STAGE_ORDER = ("journal-author", "figures", "writing")
CRITICAL_PARITY_FIELDS = (
    "source.coherent",
    "workflow.node_ids",
    "node.status",
    "node.model",
    "node.reasoning_effort",
    "node.token_limit",
    "node.artifacts",
    "workflow.session_continuity",
)
PREFLIGHT_REQUIRED_CHECKS = frozenset(
    {
        "candidate.stage",
        "candidate.published",
        "candidate.builtin",
        "candidate.readonly",
        "candidate.native_dispatch_ready",
        "candidate.runtime_freeze_complete",
        "candidate.execution_profile_frozen",
        "candidate.runtime_files_verified",
        "candidate.prompt_seals_verified",
        "candidate.executor_available",
        "candidate.output_validators_available",
        "candidate.compatibility_projection_available",
        "candidate.no_legacy_stage_nodes",
        "candidate.production_writer",
        "process.allow_candidate",
        "policy.candidate_identity",
        "migration.previous_stages_active",
        "parity.present",
        "parity.stage",
        "parity.definition",
        "parity.binding",
        "parity.critical_fields",
        "parity.comparator_version",
        "parity.zero_mismatches",
        "parity.consecutive_passes",
        "parity.distinct_subjects",
        "parity.fresh",
        "parity.head_not_replayed",
    }
)
_PASSING_PARITY_VERDICTS = frozenset({"exact", "equivalent"})
_NONTERMINAL_ASSIGNMENT_STATUSES = frozenset({"reserved", "running", "uncertain"})
_EXPECTED_WORKFLOW_IDS = MappingProxyType(
    {
        "journal-author": "builtin.sci.journal-author",
        "figures": "builtin.sci.figures",
        "writing": "builtin.sci.writing",
    }
)


class Stage(str, Enum):
    JOURNAL_AUTHOR = "journal-author"
    FIGURES = "figures"
    WRITING = "writing"


class PolicyMode(str, Enum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    DRAINING = "draining"


class RouteOwner(str, Enum):
    LEGACY = "legacy"
    GENERIC = "generic"
    BLOCKED = "blocked"


class AssignmentStatus(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class LegacyOperationState(str, Enum):
    ABSENT = "absent"
    QUEUED = "queued"
    RUNNING = "running"
    UNKNOWN_EFFECTS = "unknown-effects"


class StageRoutingError(RuntimeError):
    """Base error for the dormant candidate router."""


class StageRoutingValidationError(StageRoutingError, ValueError):
    pass


class StageRoutingConflictError(StageRoutingError):
    pass


class StageRoutingBlockedError(StageRoutingError):
    pass


class GenericDispatchUncertainError(StageRoutingBlockedError):
    pass


class LegacyDispatchUncertainError(StageRoutingBlockedError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StageRoutingValidationError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise StageRoutingValidationError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StageRoutingValidationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StageRoutingValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageRoutingValidationError(f"{name} is required")
    return value.strip()


def _digest(value: str, name: str) -> str:
    digest = _required_text(value, name).lower()
    if not is_sha256(digest):
        raise StageRoutingValidationError(f"{name} must be a SHA-256 digest")
    return digest


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StageRoutingValidationError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageRoutingValidationError(f"{name} must be a non-negative integer")
    return value


def _frozen_bool_mapping(value: Mapping[str, bool], name: str) -> Mapping[str, bool]:
    if not isinstance(value, Mapping):
        raise StageRoutingValidationError(f"{name} must be an object")
    frozen: dict[str, bool] = {}
    for key, item in value.items():
        normalized = _required_text(key, f"{name} key")
        if not isinstance(item, bool):
            raise StageRoutingValidationError(f"{name}.{normalized} must be a boolean")
        frozen[normalized] = item
    return MappingProxyType(dict(sorted(frozen.items())))


def _frozen_text_mapping(value: Mapping[str, str], name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise StageRoutingValidationError(f"{name} must be an object")
    frozen: dict[str, str] = {}
    for key, item in value.items():
        frozen[_required_text(key, f"{name} key")] = _required_text(item, f"{name}.{key}").lower()
    return MappingProxyType(dict(sorted(frozen.items())))


def _json_safe(value: Any, name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise StageRoutingValidationError(f"{name} must be canonical JSON data") from exc


def _expect_exact_mapping(data: Mapping[str, Any], fields: set[str], name: str) -> None:
    if not isinstance(data, Mapping):
        raise StageRoutingValidationError(f"{name} must be an object")
    missing = fields - set(data)
    unknown = set(data) - fields
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown fields: {', '.join(sorted(unknown))}")
        raise StageRoutingValidationError(f"invalid {name}: {'; '.join(details)}")


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise StageRoutingValidationError(f"{name} must be a boolean")
    return value


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageRoutingValidationError(f"{name} must be an integer")
    return value


def route_identity_sha256(kind: str, stage: Stage | str, value: Any) -> str:
    """Hash a routing identity without persisting raw operation or subject IDs."""

    normalized_stage = Stage(stage)
    normalized_kind = _required_text(kind, "identity kind").lower()
    payload = canonical_json(_json_safe(value, "routing identity"))
    envelope = f"{STAGE_ROUTING_SCHEMA}\x00{normalized_kind}\x00{normalized_stage.value}\x00{payload}"
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidateDescriptor:
    stage: Stage
    workflow_version_id: str
    workflow_id: str
    definition_sha256: str
    binding_sha256: str
    output_contract_sha256: str
    published: bool
    builtin: bool
    readonly: bool
    workflow_id_matches_stage: bool
    native_dispatch_ready: bool
    requires_runtime_freeze: bool
    execution_profile_frozen: bool
    runtime_files_verified: bool
    prompt_seals_verified: bool
    executor_available: bool
    output_validators_available: bool
    compatibility_projection_available: bool
    has_legacy_stage_nodes: bool
    production_writer: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", Stage(self.stage))
        object.__setattr__(
            self,
            "workflow_version_id",
            _required_text(self.workflow_version_id, "CandidateDescriptor.workflow_version_id"),
        )
        object.__setattr__(self, "workflow_id", _required_text(self.workflow_id, "CandidateDescriptor.workflow_id"))
        for name in ("definition_sha256", "binding_sha256", "output_contract_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), f"CandidateDescriptor.{name}"))
        for name in (
            "published",
            "builtin",
            "readonly",
            "workflow_id_matches_stage",
            "native_dispatch_ready",
            "requires_runtime_freeze",
            "execution_profile_frozen",
            "runtime_files_verified",
            "prompt_seals_verified",
            "executor_available",
            "output_validators_available",
            "compatibility_projection_available",
            "has_legacy_stage_nodes",
            "production_writer",
        ):
            if not isinstance(getattr(self, name), bool):
                raise StageRoutingValidationError(f"CandidateDescriptor.{name} must be a boolean")

    @classmethod
    def from_workflow(
        cls,
        stage: Stage | str,
        version: WorkflowVersion,
        binding: WorkflowRuntimeBinding,
        *,
        runtime_files_verified: bool,
        prompt_seals_verified: bool,
        executor_available: bool,
        output_validators_available: bool,
        compatibility_projection_available: bool,
    ) -> "CandidateDescriptor":
        normalized_stage = Stage(stage)
        version.validate()
        binding.validate(version.definition)
        codex_nodes = tuple(node for node in version.definition.nodes if node.node_type is NodeType.CODEX)
        native_ready = bool(version.definition.nodes) and all(
            node.metadata.get("native_dispatch_ready") is True for node in version.definition.nodes
        )
        requires_freeze = any(
            node.metadata.get("requires_runtime_freeze") is True for node in version.definition.nodes
        )
        profiles_frozen = bool(codex_nodes) and all(
            bool(node.execution_profile.model.strip())
            and bool(node.execution_profile.reasoning_effort.strip())
            and node.execution_profile.token_limit is not None
            and node.execution_profile.token_limit > 0
            and node.execution_profile.timeout_seconds > 0
            for node in codex_nodes
        )
        metadata = version.definition.metadata
        output_contract = canonical_sha256(
            {
                "outputs": [item.to_dict() for item in version.definition.outputs],
                "bindings": [item.to_dict() for item in version.definition.output_bindings],
            }
        )
        return cls(
            stage=normalized_stage,
            workflow_version_id=version.version_id,
            workflow_id=version.workflow_id,
            definition_sha256=version.definition_sha256,
            binding_sha256=binding.sha256(),
            output_contract_sha256=output_contract,
            published=version.status is WorkflowVersionStatus.PUBLISHED,
            builtin=metadata.get("builtin") is True,
            readonly=metadata.get("readonly") is True,
            workflow_id_matches_stage=version.workflow_id == _EXPECTED_WORKFLOW_IDS[normalized_stage.value],
            native_dispatch_ready=native_ready,
            requires_runtime_freeze=requires_freeze,
            execution_profile_frozen=profiles_frozen,
            runtime_files_verified=runtime_files_verified,
            prompt_seals_verified=prompt_seals_verified,
            executor_available=executor_available,
            output_validators_available=output_validators_available,
            compatibility_projection_available=compatibility_projection_available,
            has_legacy_stage_nodes=any(
                node.node_type is NodeType.LEGACY_STAGE for node in version.definition.nodes
            ),
            production_writer=bool(version.definition.nodes)
            and all(node.metadata.get("production_writer") is True for node in version.definition.nodes),
        )

    def identity_dict(self) -> dict[str, str]:
        return {
            "workflow_version_id": self.workflow_version_id,
            "definition_sha256": self.definition_sha256,
            "binding_sha256": self.binding_sha256,
            "output_contract_sha256": self.output_contract_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            **self.identity_dict(),
            "workflow_id": self.workflow_id,
            "published": self.published,
            "builtin": self.builtin,
            "readonly": self.readonly,
            "workflow_id_matches_stage": self.workflow_id_matches_stage,
            "native_dispatch_ready": self.native_dispatch_ready,
            "requires_runtime_freeze": self.requires_runtime_freeze,
            "execution_profile_frozen": self.execution_profile_frozen,
            "runtime_files_verified": self.runtime_files_verified,
            "prompt_seals_verified": self.prompt_seals_verified,
            "executor_available": self.executor_available,
            "output_validators_available": self.output_validators_available,
            "compatibility_projection_available": self.compatibility_projection_available,
            "has_legacy_stage_nodes": self.has_legacy_stage_nodes,
            "production_writer": self.production_writer,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateDescriptor":
        fields = {
            "stage",
            "workflow_version_id",
            "workflow_id",
            "definition_sha256",
            "binding_sha256",
            "output_contract_sha256",
            "published",
            "builtin",
            "readonly",
            "workflow_id_matches_stage",
            "native_dispatch_ready",
            "requires_runtime_freeze",
            "execution_profile_frozen",
            "runtime_files_verified",
            "prompt_seals_verified",
            "executor_available",
            "output_validators_available",
            "compatibility_projection_available",
            "has_legacy_stage_nodes",
            "production_writer",
        }
        _expect_exact_mapping(data, fields, "CandidateDescriptor")
        return cls(
            stage=Stage(data["stage"]),
            workflow_version_id=data["workflow_version_id"],
            workflow_id=data["workflow_id"],
            definition_sha256=data["definition_sha256"],
            binding_sha256=data["binding_sha256"],
            output_contract_sha256=data["output_contract_sha256"],
            published=_strict_bool(data["published"], "CandidateDescriptor.published"),
            builtin=_strict_bool(data["builtin"], "CandidateDescriptor.builtin"),
            readonly=_strict_bool(data["readonly"], "CandidateDescriptor.readonly"),
            workflow_id_matches_stage=_strict_bool(
                data["workflow_id_matches_stage"], "CandidateDescriptor.workflow_id_matches_stage"
            ),
            native_dispatch_ready=_strict_bool(
                data["native_dispatch_ready"], "CandidateDescriptor.native_dispatch_ready"
            ),
            requires_runtime_freeze=_strict_bool(
                data["requires_runtime_freeze"], "CandidateDescriptor.requires_runtime_freeze"
            ),
            execution_profile_frozen=_strict_bool(
                data["execution_profile_frozen"], "CandidateDescriptor.execution_profile_frozen"
            ),
            runtime_files_verified=_strict_bool(
                data["runtime_files_verified"], "CandidateDescriptor.runtime_files_verified"
            ),
            prompt_seals_verified=_strict_bool(
                data["prompt_seals_verified"], "CandidateDescriptor.prompt_seals_verified"
            ),
            executor_available=_strict_bool(
                data["executor_available"], "CandidateDescriptor.executor_available"
            ),
            output_validators_available=_strict_bool(
                data["output_validators_available"], "CandidateDescriptor.output_validators_available"
            ),
            compatibility_projection_available=_strict_bool(
                data["compatibility_projection_available"],
                "CandidateDescriptor.compatibility_projection_available",
            ),
            has_legacy_stage_nodes=_strict_bool(
                data["has_legacy_stage_nodes"], "CandidateDescriptor.has_legacy_stage_nodes"
            ),
            production_writer=_strict_bool(
                data["production_writer"], "CandidateDescriptor.production_writer"
            ),
        )


@dataclass(frozen=True)
class ParityWindow:
    stage: Stage
    definition_sha256: str
    binding_sha256: str
    comparison_head_sequence: int
    consecutive_passes: int
    distinct_subjects: int
    critical_verdicts: Mapping[str, str]
    newest_observed_at: str
    comparator_version: str = REQUIRED_COMPARATOR_VERSION
    mismatch_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", Stage(self.stage))
        object.__setattr__(self, "definition_sha256", _digest(self.definition_sha256, "ParityWindow.definition_sha256"))
        object.__setattr__(self, "binding_sha256", _digest(self.binding_sha256, "ParityWindow.binding_sha256"))
        object.__setattr__(
            self,
            "comparison_head_sequence",
            _positive_integer(self.comparison_head_sequence, "ParityWindow.comparison_head_sequence"),
        )
        object.__setattr__(
            self,
            "consecutive_passes",
            _nonnegative_integer(self.consecutive_passes, "ParityWindow.consecutive_passes"),
        )
        object.__setattr__(
            self,
            "distinct_subjects",
            _nonnegative_integer(self.distinct_subjects, "ParityWindow.distinct_subjects"),
        )
        object.__setattr__(
            self,
            "critical_verdicts",
            _frozen_text_mapping(self.critical_verdicts, "ParityWindow.critical_verdicts"),
        )
        _parse_timestamp(self.newest_observed_at, "ParityWindow.newest_observed_at")
        object.__setattr__(
            self,
            "comparator_version",
            _required_text(self.comparator_version, "ParityWindow.comparator_version"),
        )
        object.__setattr__(
            self,
            "mismatch_count",
            _nonnegative_integer(self.mismatch_count, "ParityWindow.mismatch_count"),
        )

    def critical_fields_pass(self) -> bool:
        return set(self.critical_verdicts) == set(CRITICAL_PARITY_FIELDS) and all(
            verdict in _PASSING_PARITY_VERDICTS for verdict in self.critical_verdicts.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "definition_sha256": self.definition_sha256,
            "binding_sha256": self.binding_sha256,
            "comparison_head_sequence": self.comparison_head_sequence,
            "consecutive_passes": self.consecutive_passes,
            "distinct_subjects": self.distinct_subjects,
            "critical_verdicts": dict(self.critical_verdicts),
            "newest_observed_at": self.newest_observed_at,
            "comparator_version": self.comparator_version,
            "mismatch_count": self.mismatch_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParityWindow":
        fields = {
            "stage",
            "definition_sha256",
            "binding_sha256",
            "comparison_head_sequence",
            "consecutive_passes",
            "distinct_subjects",
            "critical_verdicts",
            "newest_observed_at",
            "comparator_version",
            "mismatch_count",
        }
        _expect_exact_mapping(data, fields, "ParityWindow")
        return cls(
            stage=Stage(data["stage"]),
            definition_sha256=data["definition_sha256"],
            binding_sha256=data["binding_sha256"],
            comparison_head_sequence=_strict_int(
                data["comparison_head_sequence"], "ParityWindow.comparison_head_sequence"
            ),
            consecutive_passes=_strict_int(data["consecutive_passes"], "ParityWindow.consecutive_passes"),
            distinct_subjects=_strict_int(data["distinct_subjects"], "ParityWindow.distinct_subjects"),
            critical_verdicts=data["critical_verdicts"],
            newest_observed_at=data["newest_observed_at"],
            comparator_version=data["comparator_version"],
            mismatch_count=_strict_int(data["mismatch_count"], "ParityWindow.mismatch_count"),
        )


@dataclass(frozen=True)
class StageRoutePolicy:
    stage: Stage
    mode: PolicyMode = PolicyMode.LEGACY
    revision: int = 0
    workflow_version_id: str = ""
    definition_sha256: str = ""
    binding_sha256: str = ""
    output_contract_sha256: str = ""
    parity_head_sequence: int = 0
    min_consecutive_passes: int = 3
    min_distinct_subjects: int = 3
    max_parity_age_seconds: int = 86_400
    updated_at: str = "1970-01-01T00:00:00Z"

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", Stage(self.stage))
        object.__setattr__(self, "mode", PolicyMode(self.mode))
        object.__setattr__(self, "revision", _nonnegative_integer(self.revision, "StageRoutePolicy.revision"))
        object.__setattr__(
            self,
            "parity_head_sequence",
            _nonnegative_integer(self.parity_head_sequence, "StageRoutePolicy.parity_head_sequence"),
        )
        for name in ("min_consecutive_passes", "min_distinct_subjects", "max_parity_age_seconds"):
            object.__setattr__(self, name, _positive_integer(getattr(self, name), f"StageRoutePolicy.{name}"))
        _parse_timestamp(self.updated_at, "StageRoutePolicy.updated_at")
        identity = (
            self.workflow_version_id,
            self.definition_sha256,
            self.binding_sha256,
            self.output_contract_sha256,
        )
        if any(identity) and not all(identity):
            raise StageRoutingValidationError("candidate policy identity must be complete")
        if self.workflow_version_id:
            _required_text(self.workflow_version_id, "StageRoutePolicy.workflow_version_id")
            for name in ("definition_sha256", "binding_sha256", "output_contract_sha256"):
                object.__setattr__(self, name, _digest(getattr(self, name), f"StageRoutePolicy.{name}"))

    @property
    def candidate_identity(self) -> tuple[str, str, str, str]:
        return (
            self.workflow_version_id,
            self.definition_sha256,
            self.binding_sha256,
            self.output_contract_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "mode": self.mode.value,
            "revision": self.revision,
            "workflow_version_id": self.workflow_version_id,
            "definition_sha256": self.definition_sha256,
            "binding_sha256": self.binding_sha256,
            "output_contract_sha256": self.output_contract_sha256,
            "parity_head_sequence": self.parity_head_sequence,
            "min_consecutive_passes": self.min_consecutive_passes,
            "min_distinct_subjects": self.min_distinct_subjects,
            "max_parity_age_seconds": self.max_parity_age_seconds,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PreflightReport:
    stage: Stage
    policy_revision: int
    candidate: CandidateDescriptor
    eligible: bool
    checks: Mapping[str, bool]
    evidence_sha256: str
    expires_at: str
    comparison_head_sequence: int
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", Stage(self.stage))
        object.__setattr__(
            self,
            "policy_revision",
            _nonnegative_integer(self.policy_revision, "PreflightReport.policy_revision"),
        )
        if self.candidate.stage is not self.stage:
            raise StageRoutingValidationError("preflight candidate stage does not match report stage")
        if not isinstance(self.eligible, bool):
            raise StageRoutingValidationError("PreflightReport.eligible must be a boolean")
        object.__setattr__(self, "checks", _frozen_bool_mapping(self.checks, "PreflightReport.checks"))
        if set(self.checks) != set(PREFLIGHT_REQUIRED_CHECKS):
            raise StageRoutingValidationError("preflight checks are incomplete or contain unknown checks")
        if self.eligible != all(self.checks.values()):
            raise StageRoutingValidationError("preflight eligibility must equal all checks")
        object.__setattr__(self, "evidence_sha256", _digest(self.evidence_sha256, "PreflightReport.evidence_sha256"))
        object.__setattr__(
            self,
            "comparison_head_sequence",
            _nonnegative_integer(self.comparison_head_sequence, "PreflightReport.comparison_head_sequence"),
        )
        _parse_timestamp(self.created_at, "PreflightReport.created_at")
        if _parse_timestamp(self.expires_at, "PreflightReport.expires_at") <= _parse_timestamp(
            self.created_at, "PreflightReport.created_at"
        ):
            raise StageRoutingValidationError("preflight expiry must follow creation")
        if self.evidence_sha256 != canonical_sha256(self.evidence_dict()):
            raise StageRoutingValidationError("preflight evidence digest does not match its payload")

    def evidence_dict(self) -> dict[str, Any]:
        return {
            "schema": STAGE_ROUTING_SCHEMA,
            "stage": self.stage.value,
            "policy_revision": self.policy_revision,
            "candidate": self.candidate.to_dict(),
            "eligible": self.eligible,
            "checks": dict(self.checks),
            "expires_at": self.expires_at,
            "comparison_head_sequence": self.comparison_head_sequence,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.evidence_dict(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PreflightReport":
        fields = {
            "schema",
            "stage",
            "policy_revision",
            "candidate",
            "eligible",
            "checks",
            "expires_at",
            "comparison_head_sequence",
            "created_at",
            "evidence_sha256",
        }
        _expect_exact_mapping(data, fields, "PreflightReport")
        if data["schema"] != STAGE_ROUTING_SCHEMA:
            raise StageRoutingValidationError("unsupported PreflightReport schema")
        candidate_data = data["candidate"]
        candidate = CandidateDescriptor.from_dict(candidate_data)
        return cls(
            stage=Stage(data["stage"]),
            policy_revision=_strict_int(data["policy_revision"], "PreflightReport.policy_revision"),
            candidate=candidate,
            eligible=_strict_bool(data["eligible"], "PreflightReport.eligible"),
            checks=data["checks"],
            evidence_sha256=data["evidence_sha256"],
            expires_at=data["expires_at"],
            comparison_head_sequence=_strict_int(
                data["comparison_head_sequence"], "PreflightReport.comparison_head_sequence"
            ),
            created_at=data["created_at"],
        )


@dataclass(frozen=True)
class StageRouteRequest:
    stage: Stage
    operation_id: str
    subject_id: str
    intent: Any
    canary_opt_in: bool = False
    legacy_operation_state: LegacyOperationState = LegacyOperationState.ABSENT

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", Stage(self.stage))
        object.__setattr__(self, "operation_id", _required_text(self.operation_id, "StageRouteRequest.operation_id"))
        object.__setattr__(self, "subject_id", _required_text(self.subject_id, "StageRouteRequest.subject_id"))
        object.__setattr__(self, "intent", _json_safe(self.intent, "StageRouteRequest.intent"))
        if not isinstance(self.canary_opt_in, bool):
            raise StageRoutingValidationError("StageRouteRequest.canary_opt_in must be a boolean")
        object.__setattr__(self, "legacy_operation_state", LegacyOperationState(self.legacy_operation_state))

    @property
    def operation_id_sha256(self) -> str:
        return route_identity_sha256("operation", self.stage, self.operation_id)

    @property
    def subject_id_sha256(self) -> str:
        return route_identity_sha256("subject", self.stage, self.subject_id)

    @property
    def intent_sha256(self) -> str:
        return route_identity_sha256("intent", self.stage, self.intent)


@dataclass(frozen=True)
class RouteAssignment:
    assignment_id: str
    stage: Stage
    operation_id_sha256: str
    subject_id_sha256: str
    intent_sha256: str
    owner: RouteOwner
    policy_revision: int
    preflight_sha256: str
    status: AssignmentStatus
    reason_code: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignment_id", _required_text(self.assignment_id, "RouteAssignment.assignment_id"))
        object.__setattr__(self, "stage", Stage(self.stage))
        for name in ("operation_id_sha256", "subject_id_sha256", "intent_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), f"RouteAssignment.{name}"))
        object.__setattr__(self, "owner", RouteOwner(self.owner))
        object.__setattr__(
            self,
            "policy_revision",
            _nonnegative_integer(self.policy_revision, "RouteAssignment.policy_revision"),
        )
        if self.preflight_sha256:
            object.__setattr__(
                self,
                "preflight_sha256",
                _digest(self.preflight_sha256, "RouteAssignment.preflight_sha256"),
            )
        object.__setattr__(self, "status", AssignmentStatus(self.status))
        object.__setattr__(self, "reason_code", _required_text(self.reason_code, "RouteAssignment.reason_code"))
        _parse_timestamp(self.created_at, "RouteAssignment.created_at")
        _parse_timestamp(self.updated_at, "RouteAssignment.updated_at")

    @property
    def terminal(self) -> bool:
        return self.status.value not in _NONTERMINAL_ASSIGNMENT_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "stage": self.stage.value,
            "operation_id_sha256": self.operation_id_sha256,
            "subject_id_sha256": self.subject_id_sha256,
            "intent_sha256": self.intent_sha256,
            "owner": self.owner.value,
            "policy_revision": self.policy_revision,
            "preflight_sha256": self.preflight_sha256,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class StageRouteStorePort(Protocol):
    def initialize(self) -> None: ...

    def get_policy(self, stage: Stage | str) -> StageRoutePolicy: ...

    def list_policies(self) -> tuple[StageRoutePolicy, ...]: ...

    def record_preflight(self, report: PreflightReport) -> PreflightReport: ...

    def get_preflight(self, evidence_sha256: str) -> PreflightReport | None: ...

    def compare_and_set_policy(
        self,
        replacement: StageRoutePolicy,
        *,
        expected_revision: int,
        event_type: str,
    ) -> StageRoutePolicy: ...

    def begin_rollback(
        self,
        stage: Stage | str,
        *,
        expected_revision: int,
        now: str,
    ) -> tuple[StageRoutePolicy, ...]: ...

    def count_nonterminal_generic(self, stage: Stage | str) -> int: ...

    def admit(
        self,
        request: StageRouteRequest,
        *,
        report: PreflightReport | None,
        allow_candidate: bool,
        now: str,
    ) -> RouteAssignment: ...

    def transition_assignment(
        self,
        assignment_id: str,
        *,
        expected_status: AssignmentStatus,
        status: AssignmentStatus,
        reason_code: str,
        now: str,
    ) -> RouteAssignment: ...


def _candidate_checks(candidate: CandidateDescriptor) -> dict[str, bool]:
    return {
        "candidate.stage": candidate.workflow_id_matches_stage,
        "candidate.published": candidate.published,
        "candidate.builtin": candidate.builtin,
        "candidate.readonly": candidate.readonly,
        "candidate.native_dispatch_ready": candidate.native_dispatch_ready,
        "candidate.runtime_freeze_complete": not candidate.requires_runtime_freeze,
        "candidate.execution_profile_frozen": candidate.execution_profile_frozen,
        "candidate.runtime_files_verified": candidate.runtime_files_verified,
        "candidate.prompt_seals_verified": candidate.prompt_seals_verified,
        "candidate.executor_available": candidate.executor_available,
        "candidate.output_validators_available": candidate.output_validators_available,
        "candidate.compatibility_projection_available": candidate.compatibility_projection_available,
        "candidate.no_legacy_stage_nodes": not candidate.has_legacy_stage_nodes,
        "candidate.production_writer": candidate.production_writer,
    }


def _identity_matches(policy: StageRoutePolicy, candidate: CandidateDescriptor) -> bool:
    if not policy.workflow_version_id:
        return True
    return policy.candidate_identity == (
        candidate.workflow_version_id,
        candidate.definition_sha256,
        candidate.binding_sha256,
        candidate.output_contract_sha256,
    )


class StageRoutingController:
    """Policy controller with an independent, closed-by-default safety latch."""

    def __init__(
        self,
        store: StageRouteStorePort,
        *,
        allow_candidate: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(allow_candidate, bool):
            raise TypeError("allow_candidate must be a boolean")
        self._store = store
        self._allow_candidate = allow_candidate
        self._clock = clock

    @property
    def allow_candidate(self) -> bool:
        return self._allow_candidate

    def preflight(
        self,
        candidate: CandidateDescriptor,
        parity: ParityWindow | None,
        *,
        ttl_seconds: int = 300,
    ) -> PreflightReport:
        ttl_seconds = _positive_integer(ttl_seconds, "ttl_seconds")
        policy = self._store.get_policy(candidate.stage)
        now = self._clock().astimezone(timezone.utc)
        checks = _candidate_checks(candidate)
        checks["process.allow_candidate"] = self._allow_candidate
        checks["policy.candidate_identity"] = _identity_matches(policy, candidate)
        earlier = STAGE_ORDER[: STAGE_ORDER.index(candidate.stage.value)]
        policy_map = {item.stage.value: item for item in self._store.list_policies()}
        observed_at = (
            _parse_timestamp(parity.newest_observed_at, "ParityWindow.newest_observed_at")
            if parity is not None
            else None
        )
        checks["migration.previous_stages_active"] = all(
            policy_map[name].mode is PolicyMode.ACTIVE for name in earlier
        )
        checks.update(
            {
                "parity.present": parity is not None,
                "parity.stage": parity is not None and parity.stage is candidate.stage,
                "parity.definition": parity is not None
                and parity.definition_sha256 == candidate.definition_sha256,
                "parity.binding": parity is not None and parity.binding_sha256 == candidate.binding_sha256,
                "parity.critical_fields": parity is not None and parity.critical_fields_pass(),
                "parity.comparator_version": parity is not None
                and parity.comparator_version == REQUIRED_COMPARATOR_VERSION,
                "parity.zero_mismatches": parity is not None and parity.mismatch_count == 0,
                "parity.consecutive_passes": parity is not None
                and parity.consecutive_passes >= policy.min_consecutive_passes,
                "parity.distinct_subjects": parity is not None
                and parity.distinct_subjects >= policy.min_distinct_subjects,
                "parity.fresh": parity is not None
                and observed_at is not None
                and timedelta(0) <= now - observed_at <= timedelta(seconds=policy.max_parity_age_seconds),
                "parity.head_not_replayed": parity is not None
                and (
                    parity.comparison_head_sequence > policy.parity_head_sequence
                    if policy.mode is PolicyMode.CANARY
                    else parity.comparison_head_sequence >= policy.parity_head_sequence
                ),
            }
        )
        created_at = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=ttl_seconds))
        eligible = all(checks.values())
        payload = {
            "schema": STAGE_ROUTING_SCHEMA,
            "stage": candidate.stage.value,
            "policy_revision": policy.revision,
            "candidate": candidate.to_dict(),
            "eligible": eligible,
            "checks": dict(sorted(checks.items())),
            "expires_at": expires_at,
            "comparison_head_sequence": parity.comparison_head_sequence if parity is not None else 0,
            "created_at": created_at,
        }
        report = PreflightReport(
            stage=candidate.stage,
            policy_revision=policy.revision,
            candidate=candidate,
            eligible=eligible,
            checks=checks,
            evidence_sha256=canonical_sha256(payload),
            expires_at=expires_at,
            comparison_head_sequence=parity.comparison_head_sequence if parity is not None else 0,
            created_at=created_at,
        )
        return self._store.record_preflight(report)

    def enable_shadow(
        self,
        stage: Stage | str,
        *,
        expected_revision: int,
        min_consecutive_passes: int = 3,
        min_distinct_subjects: int = 3,
        max_parity_age_seconds: int = 86_400,
    ) -> StageRoutePolicy:
        current = self._store.get_policy(stage)
        if current.mode is not PolicyMode.LEGACY:
            raise StageRoutingConflictError("only LEGACY can transition to SHADOW")
        replacement = StageRoutePolicy(
            stage=current.stage,
            mode=PolicyMode.SHADOW,
            revision=current.revision + 1,
            min_consecutive_passes=min_consecutive_passes,
            min_distinct_subjects=min_distinct_subjects,
            max_parity_age_seconds=max_parity_age_seconds,
            updated_at=_timestamp(self._clock()),
        )
        return self._store.compare_and_set_policy(
            replacement,
            expected_revision=expected_revision,
            event_type="shadow-enabled",
        )

    def disable_shadow(
        self,
        stage: Stage | str,
        *,
        expected_revision: int,
    ) -> StageRoutePolicy:
        current = self._store.get_policy(stage)
        if current.mode is not PolicyMode.SHADOW:
            raise StageRoutingConflictError("only SHADOW can transition to LEGACY")
        replacement = StageRoutePolicy(
            stage=current.stage,
            mode=PolicyMode.LEGACY,
            revision=current.revision + 1,
            min_consecutive_passes=current.min_consecutive_passes,
            min_distinct_subjects=current.min_distinct_subjects,
            max_parity_age_seconds=current.max_parity_age_seconds,
            updated_at=_timestamp(self._clock()),
        )
        return self._store.compare_and_set_policy(
            replacement,
            expected_revision=expected_revision,
            event_type="shadow-disabled",
        )

    def promote_canary(
        self,
        stage: Stage | str,
        report: PreflightReport,
        *,
        expected_revision: int,
    ) -> StageRoutePolicy:
        return self._promote(stage, report, expected_revision=expected_revision, target=PolicyMode.CANARY)

    def promote_active(
        self,
        stage: Stage | str,
        report: PreflightReport,
        *,
        expected_revision: int,
    ) -> StageRoutePolicy:
        return self._promote(stage, report, expected_revision=expected_revision, target=PolicyMode.ACTIVE)

    def _promote(
        self,
        stage: Stage | str,
        report: PreflightReport,
        *,
        expected_revision: int,
        target: PolicyMode,
    ) -> StageRoutePolicy:
        normalized_stage = Stage(stage)
        current = self._store.get_policy(normalized_stage)
        expected_mode = PolicyMode.SHADOW if target is PolicyMode.CANARY else PolicyMode.CANARY
        if current.mode is not expected_mode:
            raise StageRoutingConflictError(f"only {expected_mode.value.upper()} can transition to {target.value.upper()}")
        if expected_revision != current.revision or report.policy_revision != current.revision:
            raise StageRoutingConflictError("policy or preflight revision is stale")
        if report.stage is not normalized_stage or not report.eligible:
            raise StageRoutingBlockedError("an eligible same-stage preflight is required")
        if not self._allow_candidate:
            raise StageRoutingBlockedError("candidate process latch is closed")
        if target is PolicyMode.CANARY and report.comparison_head_sequence <= 0:
            raise StageRoutingBlockedError("canary promotion requires positive parity evidence")
        if target is PolicyMode.ACTIVE and report.comparison_head_sequence <= current.parity_head_sequence:
            raise StageRoutingBlockedError("active promotion requires newer parity evidence than canary")
        if _parse_timestamp(report.expires_at, "PreflightReport.expires_at") <= self._clock().astimezone(timezone.utc):
            raise StageRoutingBlockedError("preflight evidence has expired")
        persisted = self._store.get_preflight(report.evidence_sha256)
        if persisted != report:
            raise StageRoutingBlockedError("preflight evidence is not persisted or changed")
        if target is PolicyMode.ACTIVE and current.candidate_identity != (
            report.candidate.workflow_version_id,
            report.candidate.definition_sha256,
            report.candidate.binding_sha256,
            report.candidate.output_contract_sha256,
        ):
            raise StageRoutingBlockedError("candidate identity changed after canary promotion")
        replacement = StageRoutePolicy(
            stage=normalized_stage,
            mode=target,
            revision=current.revision + 1,
            workflow_version_id=report.candidate.workflow_version_id,
            definition_sha256=report.candidate.definition_sha256,
            binding_sha256=report.candidate.binding_sha256,
            output_contract_sha256=report.candidate.output_contract_sha256,
            parity_head_sequence=report.comparison_head_sequence,
            min_consecutive_passes=current.min_consecutive_passes,
            min_distinct_subjects=current.min_distinct_subjects,
            max_parity_age_seconds=current.max_parity_age_seconds,
            updated_at=_timestamp(self._clock()),
        )
        return self._store.compare_and_set_policy(
            replacement,
            expected_revision=expected_revision,
            event_type=f"promoted-{target.value}",
        )

    def begin_rollback(
        self,
        stage: Stage | str,
        *,
        expected_revision: int,
    ) -> tuple[StageRoutePolicy, ...]:
        return self._store.begin_rollback(
            stage,
            expected_revision=expected_revision,
            now=_timestamp(self._clock()),
        )

    def finish_drain(
        self,
        stage: Stage | str,
        *,
        expected_revision: int,
        target: PolicyMode = PolicyMode.SHADOW,
    ) -> StageRoutePolicy:
        normalized_stage = Stage(stage)
        target = PolicyMode(target)
        if target not in {PolicyMode.SHADOW, PolicyMode.LEGACY}:
            raise StageRoutingValidationError("draining can finish only in SHADOW or LEGACY")
        current = self._store.get_policy(normalized_stage)
        if current.mode is not PolicyMode.DRAINING:
            raise StageRoutingConflictError("only DRAINING can finish a rollback")
        if self._store.count_nonterminal_generic(normalized_stage):
            raise StageRoutingBlockedError("generic assignments are still nonterminal")
        keep_identity = target is PolicyMode.SHADOW
        replacement = StageRoutePolicy(
            stage=normalized_stage,
            mode=target,
            revision=current.revision + 1,
            workflow_version_id=current.workflow_version_id if keep_identity else "",
            definition_sha256=current.definition_sha256 if keep_identity else "",
            binding_sha256=current.binding_sha256 if keep_identity else "",
            output_contract_sha256=current.output_contract_sha256 if keep_identity else "",
            parity_head_sequence=current.parity_head_sequence if keep_identity else 0,
            min_consecutive_passes=current.min_consecutive_passes,
            min_distinct_subjects=current.min_distinct_subjects,
            max_parity_age_seconds=current.max_parity_age_seconds,
            updated_at=_timestamp(self._clock()),
        )
        return self._store.compare_and_set_policy(
            replacement,
            expected_revision=expected_revision,
            event_type=f"drain-finished-{target.value}",
        )

    def admit(
        self,
        request: StageRouteRequest,
        *,
        report: PreflightReport | None = None,
    ) -> RouteAssignment:
        return self._store.admit(
            request,
            report=report,
            allow_candidate=self._allow_candidate,
            now=_timestamp(self._clock()),
        )

    def reconcile_uncertain(
        self,
        assignment_id: str,
        *,
        status: AssignmentStatus,
        evidence_sha256: str,
    ) -> RouteAssignment:
        terminal = AssignmentStatus(status)
        if terminal not in {
            AssignmentStatus.COMPLETED,
            AssignmentStatus.FAILED,
            AssignmentStatus.BLOCKED,
            AssignmentStatus.CANCELLED,
        }:
            raise StageRoutingValidationError("uncertain reconciliation requires a terminal status")
        evidence = _digest(evidence_sha256, "reconciliation evidence_sha256")
        return self._store.transition_assignment(
            _required_text(assignment_id, "assignment_id"),
            expected_status=AssignmentStatus.UNCERTAIN,
            status=terminal,
            reason_code=f"reconciled-{terminal.value}-{evidence}",
            now=_timestamp(self._clock()),
        )

    _T = TypeVar("_T")

    def dispatch(
        self,
        assignment: RouteAssignment,
        *,
        legacy: Callable[[], _T],
        generic: Callable[[], _T],
    ) -> _T:
        if assignment.owner is RouteOwner.BLOCKED:
            raise StageRoutingBlockedError(f"route is blocked: {assignment.reason_code}")
        running = self._store.transition_assignment(
            assignment.assignment_id,
            expected_status=AssignmentStatus.RESERVED,
            status=AssignmentStatus.RUNNING,
            reason_code="dispatch-started",
            now=_timestamp(self._clock()),
        )
        backend = generic if running.owner is RouteOwner.GENERIC else legacy
        try:
            result = backend()
        except Exception as exc:
            self._store.transition_assignment(
                running.assignment_id,
                expected_status=AssignmentStatus.RUNNING,
                status=AssignmentStatus.UNCERTAIN,
                reason_code=f"{running.owner.value}-dispatch-raised-{type(exc).__name__}",
                now=_timestamp(self._clock()),
            )
            error = (
                GenericDispatchUncertainError
                if running.owner is RouteOwner.GENERIC
                else LegacyDispatchUncertainError
            )
            raise error(
                f"{running.owner.value} backend raised after dispatch; no fallback was attempted"
            ) from exc
        self._store.transition_assignment(
            running.assignment_id,
            expected_status=AssignmentStatus.RUNNING,
            status=AssignmentStatus.COMPLETED,
            reason_code="dispatch-returned",
            now=_timestamp(self._clock()),
        )
        return result


__all__ = [
    "AssignmentStatus",
    "CRITICAL_PARITY_FIELDS",
    "CandidateDescriptor",
    "GenericDispatchUncertainError",
    "LegacyDispatchUncertainError",
    "LegacyOperationState",
    "ParityWindow",
    "PREFLIGHT_REQUIRED_CHECKS",
    "PolicyMode",
    "PreflightReport",
    "REQUIRED_COMPARATOR_VERSION",
    "RouteAssignment",
    "RouteOwner",
    "STAGE_ORDER",
    "STAGE_ROUTING_SCHEMA",
    "Stage",
    "StageRoutePolicy",
    "StageRouteRequest",
    "StageRouteStorePort",
    "StageRoutingBlockedError",
    "StageRoutingConflictError",
    "StageRoutingController",
    "StageRoutingError",
    "StageRoutingValidationError",
    "route_identity_sha256",
]
