"""Lease-fenced scheduler for the portable linear workflow runtime.

The scheduler is deliberately an explicit composition root.  Importing this
module and constructing :class:`WorkflowScheduler` do not initialize a
database, touch a workspace, acquire a lock, or start a thread.  A caller must
invoke ``run_once`` or ``run_until_idle`` to authorize work.

The supported executor API receives only frozen contract snapshots, sealed
artifacts, and narrow cancellation/heartbeat capabilities. In-process
executors are trusted extensions rather than a Python sandbox; untrusted
plugins must use a process-isolated adapter. Executor output declarations are
copied into host-owned seals and verified before the fenced completion
transaction accepts them.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .artifacts import (
    ArtifactContractError,
    ArtifactService,
    ProducedArtifact,
    SchemaValidator,
)
from .contracts import (
    NodeRun,
    NodeRunStatus,
    NodeType,
    WorkflowRun,
    WorkflowRunStatus,
)
from .executors import (
    ExecutionContext,
    ExecutionControl,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRegistry,
)
from .kernel import KernelAction, LinearWorkflowKernel, WorkflowInvariantError
from .sessions import SessionError, SessionHandle, SessionManager
from .store import (
    LeaseGrant,
    LeaseLostError,
    WorkflowConflictError,
    WorkflowStore,
)


_DISCOVERABLE_RUN_STATUSES = (
    WorkflowRunStatus.QUEUED,
    WorkflowRunStatus.RUNNING,
    WorkflowRunStatus.RETRYABLE_FAILURE,
)


@dataclass(frozen=True)
class SchedulerConfig:
    """Bounded worker and lease policy for one scheduler composition root."""

    max_workers: int = 4
    lease_seconds: int = 300
    heartbeat_seconds: float = 30.0
    poll_seconds: float = 0.05

    def __post_init__(self) -> None:
        if isinstance(self.max_workers, bool) or not isinstance(self.max_workers, int):
            raise TypeError("max_workers must be an integer")
        if not 1 <= self.max_workers <= 64:
            raise ValueError("max_workers must be between 1 and 64")
        if isinstance(self.lease_seconds, bool) or not isinstance(self.lease_seconds, int):
            raise TypeError("lease_seconds must be an integer")
        if not 1 <= self.lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        for name, value in {
            "heartbeat_seconds": self.heartbeat_seconds,
            "poll_seconds": self.poll_seconds,
        }.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if float(self.heartbeat_seconds) >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be shorter than lease_seconds")


@dataclass(frozen=True)
class SchedulerIssue:
    run_id: str
    node_run_id: str
    code: str
    message: str


@dataclass(frozen=True)
class SchedulerReport:
    """One bounded scheduling cycle, suitable for logs and acceptance tests."""

    owner_acquired: bool = True
    recovered: int = 0
    retries_activated: int = 0
    retries_scheduled: int = 0
    advanced: int = 0
    claimed: int = 0
    completed: int = 0
    waiting: int = 0
    failed: int = 0
    cancelled: int = 0
    lease_lost: int = 0
    runnable_remaining: int = 0
    issues: tuple[SchedulerIssue, ...] = ()

    @property
    def made_progress(self) -> bool:
        return any(
            (
                self.recovered,
                self.retries_activated,
                self.retries_scheduled,
                self.advanced,
                self.claimed,
                self.completed,
                self.waiting,
                self.failed,
                self.cancelled,
            )
        )

    def merge(self, other: "SchedulerReport") -> "SchedulerReport":
        return SchedulerReport(
            owner_acquired=self.owner_acquired and other.owner_acquired,
            recovered=self.recovered + other.recovered,
            retries_activated=self.retries_activated + other.retries_activated,
            retries_scheduled=self.retries_scheduled + other.retries_scheduled,
            advanced=self.advanced + other.advanced,
            claimed=self.claimed + other.claimed,
            completed=self.completed + other.completed,
            waiting=self.waiting + other.waiting,
            failed=self.failed + other.failed,
            cancelled=self.cancelled + other.cancelled,
            lease_lost=self.lease_lost + other.lease_lost,
            runnable_remaining=other.runnable_remaining,
            issues=self.issues + other.issues,
        )


@dataclass
class _MutableReport:
    recovered: int = 0
    retries_activated: int = 0
    retries_scheduled: int = 0
    advanced: int = 0
    claimed: int = 0
    completed: int = 0
    waiting: int = 0
    failed: int = 0
    cancelled: int = 0
    lease_lost: int = 0
    issues: list[SchedulerIssue] = field(default_factory=list)

    def freeze(self, *, owner_acquired: bool, runnable_remaining: int) -> SchedulerReport:
        return SchedulerReport(
            owner_acquired=owner_acquired,
            recovered=self.recovered,
            retries_activated=self.retries_activated,
            retries_scheduled=self.retries_scheduled,
            advanced=self.advanced,
            claimed=self.claimed,
            completed=self.completed,
            waiting=self.waiting,
            failed=self.failed,
            cancelled=self.cancelled,
            lease_lost=self.lease_lost,
            runnable_remaining=runnable_remaining,
            issues=tuple(self.issues),
        )


class _LeaseControl:
    """Keep a grant private while exposing only safe callbacks to an executor."""

    def __init__(
        self,
        store: WorkflowStore,
        grant: LeaseGrant,
        config: SchedulerConfig,
        *,
        base_now: datetime | None,
    ) -> None:
        self._store = store
        self._grant = grant
        self._config = config
        self._base_now = base_now
        self._started = time.monotonic()
        self._guard = threading.Lock()
        self._heartbeat_guard = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    @property
    def grant(self) -> LeaseGrant:
        # The scheduler may use the grant; the ExecutionContext never receives it.
        with self._guard:
            return self._grant

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def operation_now(self) -> datetime | None:
        if self._base_now is None:
            return None
        return self._base_now + timedelta(seconds=max(0.0, time.monotonic() - self._started))

    def cancellation_requested(self) -> bool:
        return self._store.cancellation_requested(self._grant.run_id)

    def heartbeat(self) -> None:
        # The executor may request a heartbeat while the background heartbeat
        # thread is active. Serialize the complete read/store/write operation;
        # the ledger independently enforces monotonic timestamps as defense in
        # depth for heartbeats from different owners/processes.
        with self._heartbeat_guard:
            try:
                refreshed = self._store.heartbeat(
                    self.grant,
                    lease_seconds=self._config.lease_seconds,
                    now=self.operation_now(),
                )
            except BaseException as exc:
                self.error = exc
                self._lost.set()
                raise
            with self._guard:
                self._grant = refreshed

    def start(self) -> None:
        interval = min(
            float(self._config.heartbeat_seconds),
            max(0.05, self._config.lease_seconds / 3.0),
        )

        def run() -> None:
            while not self._stop.wait(interval):
                try:
                    self.heartbeat()
                except BaseException:
                    return

        self._thread = threading.Thread(
            target=run,
            name=f"workflow-heartbeat-{self._grant.node_run_id}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, float(self._config.heartbeat_seconds) * 2.0))

    def execution_control(self) -> ExecutionControl:
        return ExecutionControl(
            cancellation_probe=self.cancellation_requested,
            heartbeat_callback=self.heartbeat,
        )


@dataclass(frozen=True)
class _WorkerResult:
    run_id: str
    node_run_id: str
    kind: str
    advanced: int = 0
    retries_scheduled: int = 0
    issue: SchedulerIssue | None = None


class WorkflowScheduler:
    """Run frozen linear workflows with global concurrency and lease fencing."""

    def __init__(
        self,
        store: WorkflowStore,
        workspace_root: str | Path,
        registry: ExecutorRegistry,
        *,
        config: SchedulerConfig | None = None,
        scheduler_id: str = "",
        schema_validators: Mapping[str, SchemaValidator] | None = None,
    ) -> None:
        if not isinstance(store, WorkflowStore):
            raise TypeError("store must be a WorkflowStore")
        if not isinstance(registry, ExecutorRegistry):
            raise TypeError("registry must be an ExecutorRegistry")
        raw_root = os.fspath(workspace_root)
        if not raw_root or "\x00" in raw_root:
            raise ValueError("workspace_root must be a non-empty path")
        self.store = store
        self.workspace_root = Path(os.path.abspath(os.path.expanduser(raw_root)))
        self.registry = registry
        self.config = config or SchedulerConfig()
        self.scheduler_id = scheduler_id.strip() or f"scheduler-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.kernel = LinearWorkflowKernel(store)
        self.artifacts = ArtifactService(
            self.workspace_root,
            store,
            schema_validators=schema_validators,
        )
        self.sessions = SessionManager(store)
        self.lock_path = store.path.with_name(f".{store.path.name}.workflow-scheduler.lock")
        self._fairness_cursor = ""
        self._worker_sequence = 0

    @staticmethod
    def _workflow_run(record: dict[str, object]) -> WorkflowRun:
        return WorkflowRun(
            run_id=str(record["run_id"]),
            workflow_version_id=str(record["workflow_version_id"]),
            definition_sha256=str(record["definition_sha256"]),
            binding_sha256=str(record["binding_sha256"]),
            status=WorkflowRunStatus(str(record["status"])),
            created_at=str(record["created_at"]),
            started_at=str(record["started_at"]),
            finished_at=str(record["finished_at"]),
            inputs=record.get("inputs", {}),
            metadata=record.get("metadata", {}),
        )

    @staticmethod
    def _node_run(record: dict[str, object]) -> NodeRun:
        return NodeRun(
            node_run_id=str(record["node_run_id"]),
            workflow_run_id=str(record["workflow_run_id"]),
            node_id=str(record["node_id"]),
            attempt=int(record["attempt"]),
            status=NodeRunStatus(str(record["status"])),
            session_id=str(record.get("session_id", "")),
            created_at=str(record["created_at"]),
            started_at=str(record["started_at"]),
            finished_at=str(record["finished_at"]),
            error_code=str(record.get("error_code", "")),
            error_message=str(record.get("error_message", "")),
            outputs=record.get("outputs", {}),
        )

    def _candidate_runs(self, run_ids: Sequence[str] | None) -> list[dict[str, object]]:
        if run_ids is not None:
            unique: list[str] = []
            seen: set[str] = set()
            for raw in run_ids:
                run_id = str(raw).strip()
                if not run_id:
                    raise ValueError("run IDs must be non-empty")
                if run_id not in seen:
                    unique.append(run_id)
                    seen.add(run_id)
            records = [self.store.get_run(run_id) for run_id in unique]
        else:
            records = self.store.list_runs(statuses=_DISCOVERABLE_RUN_STATUSES)
        if len(records) < 2 or not self._fairness_cursor:
            return records
        split = next(
            (index + 1 for index, item in enumerate(records) if str(item["run_id"]) == self._fairness_cursor),
            0,
        )
        return records[split:] + records[:split]

    @staticmethod
    def _retryable_attempt(record: dict[str, object]) -> dict[str, object] | None:
        latest = record.get("latest")
        if isinstance(latest, dict) and str(latest.get("status")) == NodeRunStatus.RETRYABLE_FAILURE.value:
            return latest
        return None

    def _repair_retryable_failures(
        self,
        records: Sequence[dict[str, object]],
        report: _MutableReport,
        *,
        now: datetime | None,
    ) -> None:
        for run in records:
            run_id = str(run["run_id"])
            latest = self.store.latest_node_runs(run_id)
            failed = next(
                (item for item in latest if str(item["status"]) == NodeRunStatus.RETRYABLE_FAILURE.value),
                None,
            )
            if failed is None:
                continue
            try:
                self.store.schedule_retry(str(failed["node_run_id"]), now=now)
            except WorkflowConflictError:
                continue
            report.retries_scheduled += 1

    def _advance_candidates(
        self,
        records: Sequence[dict[str, object]],
        report: _MutableReport,
        *,
        now: datetime | None,
    ) -> set[str]:
        invalid: set[str] = set()
        for run in records:
            run_id = str(run["run_id"])
            try:
                decision = self.kernel.advance(run_id, now=now)
            except WorkflowInvariantError as exc:
                invalid.add(run_id)
                report.issues.append(
                    SchedulerIssue(run_id, "", "WORKFLOW_INVARIANT_VIOLATION", str(exc))
                )
                continue
            if decision.action is not KernelAction.NOOP:
                report.advanced += 1
        return invalid

    def _prepare_context(
        self,
        grant: LeaseGrant,
        control: _LeaseControl,
    ) -> tuple[ExecutionContext, SessionHandle | None]:
        run_record = self.store.get_run(grant.run_id)
        version_record = self.store.get_version(str(run_record["workflow_version_id"]))
        version = version_record["version"]
        definition = version.definition
        binding = self.store.get_runtime_binding(str(run_record["binding_sha256"]))
        # Runtime paths are intentionally excluded from portable identity. Seal
        # every bound file immediately before dispatch so a queued run cannot
        # execute with a deleted, replaced, or drifted resource.
        binding.validate_files(definition)
        node_record = self.store.get_node_run(grant.node_run_id)
        node = node_record["node"]
        try:
            descriptor = self.registry.get(node.node_type).descriptor()
        except KeyError:
            descriptor = None
        session = (
            self.sessions.prepare(
                grant,
                node,
                descriptor.executor_id,
                requires_session=node.node_type is NodeType.CODEX,
                now=control.operation_now(),
            )
            if descriptor is not None and descriptor.available
            else None
        )
        # Session attachment mutates the frozen attempt's session_id, so refresh
        # the record before creating the contract-validated execution context.
        node_record = self.store.get_node_run(grant.node_run_id)
        inputs = self.artifacts.resolve_inputs(definition, node, grant.run_id)
        output_root = self.artifacts.output_root_for(
            grant.run_id,
            grant.node_run_id,
            grant.generation,
        )
        context = ExecutionContext(
            definition=definition,
            runtime_binding=binding,
            workflow_run=self._workflow_run(run_record),
            node_run=self._node_run(node_record),
            node=node,
            workspace_root=self.workspace_root,
            output_root=output_root,
            inputs=inputs,
            session=session,
            control=control.execution_control(),
        )
        prepared = self.artifacts.prepare_output_root(
            grant.run_id,
            grant.node_run_id,
            grant.generation,
        )
        if prepared != context.output_root:
            raise ArtifactContractError(
                "artifact.output_root.mismatch",
                "prepared output root differs from the frozen execution context",
            )
        return context, session

    @staticmethod
    def _issue(grant: LeaseGrant, code: str, message: str) -> SchedulerIssue:
        return SchedulerIssue(grant.run_id, grant.node_run_id, code, message)

    def _acknowledge_cancel(self, control: _LeaseControl) -> _WorkerResult:
        grant = control.grant
        try:
            if str(self.store.get_run(grant.run_id)["status"]) == WorkflowRunStatus.CANCELLED.value:
                return _WorkerResult(grant.run_id, grant.node_run_id, "cancelled")
        except KeyError:
            pass
        try:
            self.store.release_lease(
                grant,
                node_status=NodeRunStatus.CANCELLED,
                now=control.operation_now(),
            )
        except (LeaseLostError, WorkflowConflictError) as exc:
            try:
                if str(self.store.get_run(grant.run_id)["status"]) == WorkflowRunStatus.CANCELLED.value:
                    return _WorkerResult(grant.run_id, grant.node_run_id, "cancelled")
            except KeyError:
                pass
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "lease-lost",
                issue=self._issue(grant, "LEASE_LOST_DURING_CANCEL", str(exc)),
            )
        return _WorkerResult(grant.run_id, grant.node_run_id, "cancelled")

    def _fail_owned_node(
        self,
        control: _LeaseControl,
        *,
        status: NodeRunStatus,
        error_code: str,
        message: str,
    ) -> _WorkerResult:
        grant = control.grant
        try:
            if self.store.cancellation_requested(grant.run_id):
                return self._acknowledge_cancel(control)
            failed = self.store.fail_node(
                grant,
                status=status,
                error_code=error_code,
                error_message=message,
                now=control.operation_now(),
            )
            scheduled = 0
            if str(failed["status"]) == NodeRunStatus.RETRYABLE_FAILURE.value:
                self.store.schedule_retry(grant.node_run_id, now=control.operation_now())
                scheduled = 1
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "failed",
                retries_scheduled=scheduled,
                issue=self._issue(grant, error_code, message),
            )
        except WorkflowConflictError as exc:
            try:
                if self.store.cancellation_requested(grant.run_id):
                    return self._acknowledge_cancel(control)
            except Exception:
                pass
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "lease-lost",
                issue=self._issue(grant, "STATE_CONFLICT_DURING_FAILURE", str(exc)),
            )
        except LeaseLostError as exc:
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "lease-lost",
                issue=self._issue(grant, "LEASE_LOST_DURING_FAILURE", str(exc)),
            )

    def _commit_result(
        self,
        control: _LeaseControl,
        context: ExecutionContext,
        session: SessionHandle | None,
        result: ExecutionResult,
    ) -> _WorkerResult:
        grant = control.grant
        if result.external_session_id:
            try:
                if session is None:
                    raise SessionError("executor returned an external session without a durable session record")
                self.sessions.bind_external(
                    control.grant,
                    session,
                    result.external_session_id,
                    now=control.operation_now(),
                )
            except (SessionError, ValueError) as exc:
                return self._fail_owned_node(
                    control,
                    status=NodeRunStatus.BLOCKED,
                    error_code="SESSION_BINDING_FAILED",
                    message=str(exc),
                )
            except WorkflowConflictError as exc:
                return self._fail_owned_node(
                    control,
                    status=NodeRunStatus.BLOCKED,
                    error_code="SESSION_BINDING_CONFLICT",
                    message=str(exc),
                )
        try:
            if self.store.cancellation_requested(grant.run_id):
                return self._acknowledge_cancel(control)
        except Exception as exc:
            return self._fail_owned_node(
                control,
                status=NodeRunStatus.RETRYABLE_FAILURE,
                error_code="CANCELLATION_PROBE_FAILED",
                message=str(exc),
            )
        if control.lost:
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "lease-lost",
                issue=self._issue(grant, "LEASE_HEARTBEAT_LOST", str(control.error or "lease lost")),
            )

        if result.outcome is ExecutionOutcome.CANCELLED:
            self.store.request_cancel(grant.run_id, now=control.operation_now())
            return self._acknowledge_cancel(control)
        if result.outcome in {ExecutionOutcome.WAITING_APPROVAL, ExecutionOutcome.WAITING_HUMAN}:
            target = (
                NodeRunStatus.WAITING_APPROVAL
                if result.outcome is ExecutionOutcome.WAITING_APPROVAL
                else NodeRunStatus.WAITING_HUMAN
            )
            try:
                self.store.wait_node(
                    grant,
                    status=target,
                    reason=result.message or result.error_code or result.outcome.value,
                    details={"error_code": result.error_code, "metadata": dict(result.metadata)},
                    now=control.operation_now(),
                )
            except (LeaseLostError, WorkflowConflictError) as exc:
                try:
                    if self.store.cancellation_requested(grant.run_id):
                        return self._acknowledge_cancel(control)
                except Exception:
                    pass
                return _WorkerResult(
                    grant.run_id,
                    grant.node_run_id,
                    "lease-lost",
                    issue=self._issue(grant, "LEASE_LOST_DURING_WAIT", str(exc)),
                )
            return _WorkerResult(grant.run_id, grant.node_run_id, "waiting")
        if result.outcome is ExecutionOutcome.RETRYABLE_FAILURE:
            return self._fail_owned_node(
                control,
                status=NodeRunStatus.RETRYABLE_FAILURE,
                error_code=result.error_code or "EXECUTOR_RETRYABLE_FAILURE",
                message=result.message,
            )
        if result.outcome is ExecutionOutcome.FATAL_FAILURE:
            return self._fail_owned_node(
                control,
                status=NodeRunStatus.FATAL_FAILURE,
                error_code=result.error_code or "EXECUTOR_FATAL_FAILURE",
                message=result.message,
            )
        if result.outcome is ExecutionOutcome.BLOCKED:
            return self._fail_owned_node(
                control,
                status=NodeRunStatus.BLOCKED,
                error_code=result.error_code or "EXECUTOR_BLOCKED",
                message=result.message,
            )
        if result.outcome is not ExecutionOutcome.COMPLETED:
            return self._fail_owned_node(
                control,
                status=NodeRunStatus.FATAL_FAILURE,
                error_code="EXECUTOR_OUTCOME_INVALID",
                message=f"unsupported executor outcome: {result.outcome!r}",
            )

        try:
            verified = self.artifacts.verify_outputs(
                context.node,
                result.produced_artifacts,
                output_root=context.output_root,
            )
            self.store.complete_node(grant, verified, now=control.operation_now())
            decision = self.kernel.advance(grant.run_id, now=control.operation_now())
        except (ArtifactContractError, SessionError, ValueError) as exc:
            return self._fail_owned_node(
                control,
                status=NodeRunStatus.BLOCKED,
                error_code="OUTPUT_VERIFICATION_FAILED",
                message=str(exc),
            )
        except (LeaseLostError, WorkflowConflictError) as exc:
            try:
                if self.store.cancellation_requested(grant.run_id):
                    return self._acknowledge_cancel(control)
            except Exception:
                pass
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "lease-lost",
                issue=self._issue(grant, "LEASE_LOST_DURING_COMPLETION", str(exc)),
            )
        except WorkflowInvariantError as exc:
            # The node completion is already durable, but no downstream state
            # is advanced when the frozen workflow invariant no longer holds.
            return _WorkerResult(
                grant.run_id,
                grant.node_run_id,
                "completed",
                issue=self._issue(grant, "WORKFLOW_INVARIANT_VIOLATION", str(exc)),
            )
        return _WorkerResult(
            grant.run_id,
            grant.node_run_id,
            "completed",
            advanced=int(decision.action is not KernelAction.NOOP),
        )

    def _execute_grant(self, grant: LeaseGrant, *, now: datetime | None) -> _WorkerResult:
        control = _LeaseControl(self.store, grant, self.config, base_now=now)
        control.start()
        try:
            try:
                context, session = self._prepare_context(grant, control)
            except LeaseLostError as exc:
                return _WorkerResult(
                    grant.run_id,
                    grant.node_run_id,
                    "lease-lost",
                    issue=self._issue(grant, "LEASE_LOST_DURING_PREPARATION", str(exc)),
                )
            except WorkflowConflictError as exc:
                try:
                    if self.store.cancellation_requested(grant.run_id):
                        return self._acknowledge_cancel(control)
                except Exception:
                    pass
                return self._fail_owned_node(
                    control,
                    status=NodeRunStatus.BLOCKED,
                    error_code="EXECUTION_CONTEXT_CONFLICT",
                    message=str(exc),
                )
            except (ArtifactContractError, SessionError, WorkflowInvariantError, KeyError, ValueError) as exc:
                return self._fail_owned_node(
                    control,
                    status=NodeRunStatus.BLOCKED,
                    error_code="EXECUTION_CONTEXT_INVALID",
                    message=str(exc),
                )
            # Context preparation may involve filesystem and session work.
            # Recheck the host-owned cancellation/lease barrier immediately
            # before entering the trusted in-process executor boundary.
            try:
                if self.store.cancellation_requested(grant.run_id):
                    return self._acknowledge_cancel(control)
                self.store.assert_live_lease(grant, now=control.operation_now())
            except WorkflowConflictError:
                return self._acknowledge_cancel(control)
            except LeaseLostError as exc:
                return _WorkerResult(
                    grant.run_id,
                    grant.node_run_id,
                    "lease-lost",
                    issue=self._issue(grant, "LEASE_LOST_BEFORE_EXECUTOR", str(exc)),
                )
            if control.lost:
                return _WorkerResult(
                    grant.run_id,
                    grant.node_run_id,
                    "lease-lost",
                    issue=self._issue(grant, "LEASE_HEARTBEAT_LOST", str(control.error or "lease lost")),
                )
            try:
                result = self.registry.execute(context)
                if not isinstance(result, ExecutionResult):
                    raise TypeError("executor did not return an ExecutionResult")
            except Exception as exc:
                return self._fail_owned_node(
                    control,
                    status=NodeRunStatus.RETRYABLE_FAILURE,
                    error_code="EXECUTOR_UNHANDLED_EXCEPTION",
                    message=f"executor raised {type(exc).__name__}",
                )
            return self._commit_result(control, context, session, result)
        finally:
            control.close()

    @staticmethod
    def _count_runnable(store: WorkflowStore, run_ids: Iterable[str]) -> int:
        count = 0
        for run_id in run_ids:
            try:
                run = store.get_run(run_id)
            except KeyError:
                continue
            if str(run["status"]) not in {
                WorkflowRunStatus.QUEUED.value,
                WorkflowRunStatus.RUNNING.value,
            }:
                continue
            count += sum(
                str(item["status"]) == NodeRunStatus.READY.value
                for item in store.latest_node_runs(run_id)
            )
        return count

    @staticmethod
    def _record_worker_result(report: _MutableReport, result: _WorkerResult) -> None:
        report.advanced += result.advanced
        report.retries_scheduled += result.retries_scheduled
        if result.kind == "completed":
            report.completed += 1
        elif result.kind == "waiting":
            report.waiting += 1
        elif result.kind == "failed":
            report.failed += 1
        elif result.kind == "cancelled":
            report.cancelled += 1
        elif result.kind == "lease-lost":
            report.lease_lost += 1
        if result.issue is not None:
            report.issues.append(result.issue)

    def _run_owned_cycle(
        self,
        run_ids: Sequence[str] | None,
        *,
        now: datetime | None,
    ) -> SchedulerReport:
        report = _MutableReport()
        scoped_ids = None if run_ids is None else tuple(dict.fromkeys(str(item).strip() for item in run_ids))
        # Capacity is global even for a scoped scheduling call, so globally
        # expired leases must always be recovered before counting pool slots.
        report.recovered += len(self.store.recover_expired_leases(now=now))
        if scoped_ids is None:
            report.retries_activated += len(self.store.activate_due_retries(now=now))
        else:
            for run_id in scoped_ids:
                if not run_id:
                    raise ValueError("run IDs must be non-empty")
                report.retries_activated += len(self.store.activate_due_retries(run_id=run_id, now=now))

        records = self._candidate_runs(scoped_ids)
        self._repair_retryable_failures(records, report, now=now)
        records = self._candidate_runs(scoped_ids)
        invalid_run_ids = self._advance_candidates(records, report, now=now)
        records = self._candidate_runs(scoped_ids)

        grants: list[LeaseGrant] = []
        # Live leases can survive a scheduler handover until their heartbeat
        # expires.  Count them against the global pool so a replacement owner
        # cannot temporarily exceed the configured concurrency bound.
        available_slots = max(
            0,
            self.config.max_workers - int(self.store.summary()["active_lease_count"]),
        )
        active_by_type = self.store.active_lease_counts_by_node_type()
        for run in records:
            if len(grants) >= available_slots:
                break
            run_id = str(run["run_id"])
            if run_id in invalid_run_ids:
                continue
            if str(run["status"]) not in {
                WorkflowRunStatus.QUEUED.value,
                WorkflowRunStatus.RUNNING.value,
            } or bool(run.get("cancel_requested")):
                continue
            ready = next(
                (
                    item
                    for item in self.store.latest_node_runs(run_id)
                    if str(item["status"]) == NodeRunStatus.READY.value
                ),
                None,
            )
            if ready is None:
                continue
            node_type = ready["node"].node_type
            try:
                descriptor = self.registry.get(node_type).descriptor()
                executor_limit = descriptor.max_concurrency
            except KeyError:
                executor_limit = 1
            node_type_key = node_type.value
            if active_by_type.get(node_type_key, 0) >= executor_limit:
                continue
            self._worker_sequence += 1
            try:
                grant = self.store.claim_ready_node(
                    run_id,
                    f"{self.scheduler_id}/worker-{self._worker_sequence}",
                    lease_seconds=self.config.lease_seconds,
                    global_active_limit=self.config.max_workers,
                    node_type_active_limit=executor_limit,
                    enforce_linear_order=True,
                    now=now,
                )
            except WorkflowConflictError as exc:
                report.issues.append(
                    SchedulerIssue(run_id, str(ready["node_run_id"]), "CLAIM_INVARIANT_VIOLATION", str(exc))
                )
                continue
            if grant is not None:
                grants.append(grant)
                active_by_type[node_type_key] = active_by_type.get(node_type_key, 0) + 1
                self._fairness_cursor = run_id
        report.claimed = len(grants)

        if grants:
            with ThreadPoolExecutor(
                max_workers=min(self.config.max_workers, len(grants)),
                thread_name_prefix="workflow-worker",
            ) as pool:
                futures: dict[Future[_WorkerResult], LeaseGrant] = {
                    pool.submit(self._execute_grant, grant, now=now): grant for grant in grants
                }
                for future in as_completed(futures):
                    grant = futures[future]
                    try:
                        result = future.result()
                    except BaseException as exc:
                        # This is a scheduler bug boundary.  Leave the lease to
                        # expire and be fenced rather than performing an
                        # unverified transition from the coordinator thread.
                        result = _WorkerResult(
                            grant.run_id,
                            grant.node_run_id,
                            "lease-lost",
                            issue=self._issue(
                                grant,
                                "SCHEDULER_WORKER_CRASHED",
                                f"scheduler worker raised {type(exc).__name__}",
                            ),
                        )
                    self._record_worker_result(report, result)

        candidate_ids = [str(item["run_id"]) for item in self._candidate_runs(scoped_ids)]
        runnable = self._count_runnable(self.store, candidate_ids)
        return report.freeze(owner_acquired=True, runnable_remaining=runnable)

    def run_once(
        self,
        run_ids: Sequence[str] | None = None,
        *,
        now: datetime | None = None,
    ) -> SchedulerReport:
        """Perform one globally bounded scheduling cycle.

        Contending scheduler processes return ``owner_acquired=False`` without
        dispatching work.  The workspace-level lock is held until all workers
        in this cycle have committed or relinquished their outcomes.
        """

        # Import lazily so construction remains isolated from active product
        # configuration and filesystem initialization.
        from unified_paper_orchestrator.io_utils import FileLock

        lock = FileLock(self.lock_path, stale_seconds=0)
        try:
            lock.__enter__()
        except RuntimeError:
            return SchedulerReport(owner_acquired=False)
        except OSError as exc:
            transient_windows_contention = os.name == "nt" and (
                (isinstance(exc, PermissionError) and os.access(self.lock_path.parent, os.W_OK))
                or getattr(exc, "winerror", None) in {32, 33}
            )
            if transient_windows_contention:
                return SchedulerReport(owner_acquired=False)
            raise
        try:
            return self._run_owned_cycle(run_ids, now=now)
        finally:
            lock.__exit__(None, None, None)

    def complete_waiting_node(
        self,
        node_run_id: str,
        produced: Sequence[ProducedArtifact] = (),
        *,
        expected_status: NodeRunStatus,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Verify/seal operator outputs before completing a waiting node."""

        expected = NodeRunStatus(expected_status)
        if expected not in {
            NodeRunStatus.WAITING_APPROVAL,
            NodeRunStatus.WAITING_HUMAN,
        }:
            raise ValueError("expected_status must be waiting-approval or waiting-human")
        node_record = self.store.get_node_run(node_run_id)
        node = node_record["node"]
        verified = self.artifacts.verify_outputs(node, produced)
        completed = self.store.complete_waiting_node(
            node_run_id,
            verified,
            expected_status=expected,
            now=now,
        )
        run_id = str(completed["workflow_run_id"])
        try:
            self.kernel.advance(run_id, now=now)
        except WorkflowInvariantError:
            # Completion is already durable. The next scheduler cycle reports
            # the invariant failure and remains fail-closed for dispatch.
            pass
        return completed

    def run_until_idle(
        self,
        run_ids: Sequence[str] | None = None,
        *,
        max_cycles: int = 1000,
        timeout_seconds: float = 300.0,
        now: datetime | None = None,
    ) -> SchedulerReport:
        """Run bounded cycles until no immediate transition is possible."""

        if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles <= 0:
            raise ValueError("max_cycles must be a positive integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be a number")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        aggregate = SchedulerReport()
        deadline = time.monotonic() + float(timeout_seconds)
        base_now = now
        started = time.monotonic()
        for _cycle in range(max_cycles):
            cycle_now = (
                None
                if base_now is None
                else base_now + timedelta(seconds=max(0.0, time.monotonic() - started))
            )
            current = self.run_once(run_ids, now=cycle_now)
            aggregate = aggregate.merge(current)
            if not current.owner_acquired:
                break
            if not current.made_progress:
                break
            if time.monotonic() >= deadline:
                break
            if current.runnable_remaining == 0 and current.claimed == 0:
                # One more cycle is unnecessary when every visible run is
                # terminal, waiting, blocked, paused, or sleeping in backoff.
                break
            time.sleep(min(float(self.config.poll_seconds), max(0.0, deadline - time.monotonic())))
        return aggregate

    def request_cancel(self, run_id: str, *, now: datetime | None = None) -> dict[str, object]:
        return self.store.request_cancel(run_id, now=now)

    def pause(self, run_id: str, *, now: datetime | None = None) -> dict[str, object]:
        return self.store.pause_run(run_id, now=now)

    def resume(self, run_id: str, *, now: datetime | None = None) -> dict[str, object]:
        return self.store.resume_run(run_id, now=now)

    def recover(self, run_id: str = "", *, now: datetime | None = None) -> list[dict[str, object]]:
        return self.store.recover_expired_leases(run_id=run_id, now=now)


__all__ = [
    "SchedulerConfig",
    "SchedulerIssue",
    "SchedulerReport",
    "WorkflowScheduler",
]
