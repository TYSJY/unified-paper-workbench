from __future__ import annotations

import os
from pathlib import Path

from ..canonical import (
    CanonicalCommitError,
    CanonicalCommitResult,
    CanonicalCommitter,
    DestinationCAS,
)
from ..contracts import ApprovalPolicy, ArtifactRef, ResultStatus, TaskEnvelope, TaskResult
from .base import ExecutorDescriptor


CANONICAL_COMMIT_ACTION = "artifact.canonical_commit"
_ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "commit_id",
        "root_name",
        "relative_path",
        "expected_exists",
        "expected_sha256",
    }
)


def _normalized_path(path: Path | str) -> str:
    try:
        normalized = Path(path).resolve(strict=False)
    except OSError:
        normalized = Path(os.path.abspath(os.fspath(path)))
    return os.path.normcase(os.fspath(normalized))


class CanonicalCommitExecutor:
    """Gateway-only adapter around the single V2 canonical writer primitive."""

    name = "canonical_commit"

    def __init__(self, committer: CanonicalCommitter | None = None) -> None:
        self.committer = committer or CanonicalCommitter()

    def descriptor(self) -> ExecutorDescriptor:
        if self.committer.enabled:
            detail = (
                "Explicitly authorized single-writer commit gate; every write requires "
                "gateway approval, staged SHA256 verification, and destination CAS."
            )
        else:
            detail = (
                "Single-writer commit gate is installed but disabled; V1 remains the "
                "canonical production owner."
            )
        return ExecutorDescriptor(
            name=self.name,
            available=self.committer.enabled,
            kind="single_canonical_writer",
            detail=detail,
            side_effecting=True,
        )

    def configured_root(self, root_name: str) -> Path:
        return self.committer.configured_root(root_name)

    def configured_staging_roots(self) -> tuple[Path, ...]:
        return self.committer.configured_staging_roots()

    def execute(self, task: TaskEnvelope) -> TaskResult:
        validation_error = self._validate_task(task)
        if validation_error is not None:
            return validation_error

        payload = task.payload
        try:
            result = self.committer.commit(
                commit_id=str(payload["commit_id"]),
                staged=task.inputs[0],
                root_name=str(payload["root_name"]),
                destination=DestinationCAS(
                    relative_path=str(payload["relative_path"]),
                    expected_exists=payload["expected_exists"],
                    expected_sha256=str(payload.get("expected_sha256") or ""),
                ),
            )
        except CanonicalCommitError as exc:
            return self._canonical_error(exc)
        except Exception as exc:
            return TaskResult(
                status=ResultStatus.RETRYABLE_FAILURE,
                blocker_code="CANONICAL_EXECUTOR_EXCEPTION",
                message=f"Canonical commit executor failed with {type(exc).__name__}.",
                data={
                    "canonical_write_state": "unknown",
                    "fallback_allowed": False,
                    "error_type": type(exc).__name__,
                },
            )

        return self._completed_result(task, result)

    def reconcile(self, task: TaskEnvelope) -> TaskResult | None:
        """Reconcile an audited commit after task-ledger completion was interrupted."""

        validation_error = self._validate_task(task)
        if validation_error is not None:
            return validation_error
        payload = task.payload
        try:
            result = self.committer.reconcile_committed(
                commit_id=str(payload["commit_id"]),
                staged_sha256=task.inputs[0].sha256,
                root_name=str(payload["root_name"]),
                destination=DestinationCAS(
                    relative_path=str(payload["relative_path"]),
                    expected_exists=payload["expected_exists"],
                    expected_sha256=str(payload.get("expected_sha256") or ""),
                ),
            )
        except CanonicalCommitError as exc:
            return self._canonical_error(exc)
        if result is None:
            return None
        return self._completed_result(task, result)

    @staticmethod
    def _completed_result(task: TaskEnvelope, result: CanonicalCommitResult) -> TaskResult:
        source = task.inputs[0]
        output = ArtifactRef(
            uri=f"canonical://{result.root_name}/{result.relative_path}",
            sha256=result.sha256,
            schema_version=source.schema_version,
            media_type=source.media_type,
        )
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="Staged artifact passed approval, SHA256, CAS, and single-writer commit gates.",
            outputs=[output],
            data={
                **result.to_dict(),
                "canonical_write": True,
                "canonical_write_state": "committed",
                "write_performed": not result.idempotent,
                "fallback_allowed": False,
            },
        )

    def _validate_task(self, task: TaskEnvelope) -> TaskResult | None:
        if task.action != CANONICAL_COMMIT_ACTION:
            return self._blocked(
                "CANONICAL_ACTION_UNSUPPORTED",
                "The canonical executor only supports artifact.canonical_commit.",
            )
        if not self.committer.enabled:
            return self._blocked(
                "CANONICAL_COMMIT_DISABLED",
                "Canonical commit is disabled; V1 remains the canonical production owner.",
            )
        if task.network_access or task.allowed_tools:
            return self._blocked(
                "CANONICAL_CAPABILITY_DENIED",
                "Canonical commit requires zero network access and zero external tools.",
            )
        if task.approval_policy is not ApprovalPolicy.REQUIRED:
            return self._blocked(
                "CANONICAL_APPROVAL_REQUIRED",
                "Canonical commit requires approval_policy=required.",
            )
        if len(task.inputs) != 1:
            return self._fatal(
                "CANONICAL_INPUT_COUNT_INVALID",
                "Canonical commit requires exactly one staged artifact reference.",
            )
        if set(task.payload) != _ALLOWED_PAYLOAD_KEYS:
            return self._fatal(
                "CANONICAL_PAYLOAD_INVALID",
                "Canonical commit payload fields do not match the strict contract.",
            )
        if not isinstance(task.payload.get("expected_exists"), bool):
            return self._fatal(
                "CANONICAL_CAS_INVALID",
                "Canonical destination existence must be an explicit boolean.",
            )
        root_name = str(task.payload.get("root_name") or "")
        try:
            expected_root = self.configured_root(root_name)
        except CanonicalCommitError as exc:
            return self._canonical_error(exc)
        if len(task.writable_roots) != 1 or _normalized_path(task.writable_roots[0]) != _normalized_path(
            expected_root
        ):
            return self._blocked(
                "CANONICAL_WRITABLE_ROOT_DENIED",
                "The declared writable root must exactly match the authorized canonical root.",
            )
        return None

    @staticmethod
    def _canonical_error(exc: CanonicalCommitError) -> TaskResult:
        if exc.code == "CANONICAL_COMMIT_BUSY":
            status = ResultStatus.RETRYABLE_FAILURE
        elif exc.code in {
            "CANONICAL_AUTHORIZATION_INVALID",
            "CANONICAL_COMMIT_DISABLED",
            "CANONICAL_CONFIGURATION_INVALID",
            "CANONICAL_ROOT_UNKNOWN",
            "CANONICAL_ROOT_INVALID",
            "CANONICAL_CAS_MISMATCH",
        }:
            status = ResultStatus.BLOCKED
        else:
            status = ResultStatus.FATAL_FAILURE
        data: dict[str, object] = {
            "canonical_write_state": exc.write_state,
            "fallback_allowed": False,
        }
        if exc.write_state != "unknown":
            data["canonical_write"] = exc.write_state == "committed"
        return TaskResult(
            status=status,
            blocker_code=exc.code,
            message=str(exc),
            data=data,
        )

    @staticmethod
    def _blocked(code: str, message: str) -> TaskResult:
        return TaskResult(
            status=ResultStatus.BLOCKED,
            blocker_code=code,
            message=message,
            data={
                "canonical_write": False,
                "canonical_write_state": "not_committed",
                "fallback_allowed": False,
            },
        )

    @staticmethod
    def _fatal(code: str, message: str) -> TaskResult:
        return TaskResult(
            status=ResultStatus.FATAL_FAILURE,
            blocker_code=code,
            message=message,
            data={
                "canonical_write": False,
                "canonical_write_state": "not_committed",
                "fallback_allowed": False,
            },
        )
