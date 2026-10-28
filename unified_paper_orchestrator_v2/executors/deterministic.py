from __future__ import annotations

import hashlib
from pathlib import Path

from ..contracts import ArtifactRef, ResultStatus, TaskEnvelope, TaskResult
from .base import ExecutorDescriptor


class DeterministicExecutor:
    name = "deterministic"

    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            name=self.name,
            available=True,
            kind="local_code",
            detail="No-model executor for artifact verification and deterministic control tasks.",
        )

    def execute(self, task: TaskEnvelope) -> TaskResult:
        if task.action == "system.noop":
            return TaskResult(
                status=ResultStatus.COMPLETED,
                message="deterministic no-op completed",
                data={"echo": task.payload},
            )
        if task.action != "artifact.verify":
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="DETERMINISTIC_ACTION_UNSUPPORTED",
                message=f"unsupported deterministic action: {task.action}",
            )
        if not task.inputs:
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="ARTIFACT_INPUT_REQUIRED",
                message="artifact.verify requires at least one input",
            )
        outputs: list[ArtifactRef] = []
        evidence = []
        for artifact in task.inputs:
            path = Path(artifact.uri).expanduser()
            if not path.is_file():
                return TaskResult(
                    status=ResultStatus.BLOCKED,
                    blocker_code="ARTIFACT_MISSING",
                    message=f"artifact does not exist: {path}",
                )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if artifact.sha256 and digest != artifact.sha256.lower():
                return TaskResult(
                    status=ResultStatus.BLOCKED,
                    blocker_code="ARTIFACT_SHA256_MISMATCH",
                    message=f"artifact digest mismatch: {path}",
                    evidence=[{"path": str(path.resolve()), "expected": artifact.sha256, "actual": digest}],
                )
            verified = ArtifactRef(
                uri=str(path.resolve()),
                sha256=digest,
                schema_version=artifact.schema_version,
                media_type=artifact.media_type,
            )
            outputs.append(verified)
            evidence.append({"path": verified.uri, "sha256": digest, "size": path.stat().st_size})
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message=f"verified {len(outputs)} artifact(s)",
            outputs=outputs,
            evidence=evidence,
        )

