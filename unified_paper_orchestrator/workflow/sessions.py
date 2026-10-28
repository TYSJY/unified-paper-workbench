from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from .contracts import NodeDefinition, NodeRunStatus
from .store import LeaseGrant, LeaseLostError, WorkflowConflictError, WorkflowStore


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExternalSessionStart:
    session_id: str
    created: bool


class ExternalSessionBroker(Protocol):
    """Minimal idempotent boundary implemented by a concrete SDK adapter."""

    def find_exact(self, owner_name: str) -> Sequence[str]: ...

    def start_named(self, owner_name: str) -> ExternalSessionStart: ...

    def stop(self, session_id: str) -> None: ...


@dataclass(frozen=True)
class SessionHandle:
    session_record_id: str
    run_id: str
    session_group: str
    executor: str
    generation: int
    external_session_id: str
    owner_name: str


class SessionManager:
    """Resolve durable, run-scoped executor sessions from frozen node policy."""

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store

    @staticmethod
    def deterministic_owner_name(
        run_id: str,
        session_group: str,
        executor: str,
        generation: int,
    ) -> str:
        return f"workflow/{run_id}/{session_group}/{executor}/generation-{generation}"

    @classmethod
    def _handle(cls, record: dict[str, object]) -> SessionHandle:
        run_id = str(record["run_id"])
        group = str(record["session_group"])
        executor = str(record["executor"])
        generation = int(record["generation"])
        return SessionHandle(
            session_record_id=str(record["session_record_id"]),
            run_id=run_id,
            session_group=group,
            executor=executor,
            generation=generation,
            external_session_id=str(record["external_session_id"]),
            owner_name=cls.deterministic_owner_name(run_id, group, executor, generation),
        )

    def prepare(
        self,
        grant: LeaseGrant,
        node: NodeDefinition,
        executor_name: str,
        *,
        requires_session: bool = False,
        now: datetime | None = None,
    ) -> SessionHandle | None:
        # Validate the opaque fencing capability before returning an attached
        # external handle or creating any durable session state. Every later
        # mutation repeats this check transactionally in the store.
        self.store.assert_live_lease(grant, now=now)
        policy = node.session_policy
        configured = bool(
            policy.session_group or policy.reuse_within_run or policy.reuse_on_retry or policy.force_new_session
        )
        if not configured and not requires_session:
            return None
        executor = executor_name.strip()
        if not executor:
            raise ValueError("executor_name is required")
        node_run = self.store.get_node_run(grant.node_run_id)
        if (
            str(node_run["workflow_run_id"]) != grant.run_id
            or str(node_run["node_id"]) != node.node_id
            or str(node_run["status"]) != NodeRunStatus.RUNNING.value
        ):
            raise SessionError("lease does not own the requested running node snapshot")
        attached_id = str(node_run["session_id"])
        if attached_id:
            record = self.store.get_session(attached_id)
            if (
                str(record["run_id"]) != grant.run_id
                or str(record["state"]) != "active"
                or str(record["executor"]) != executor
                or (policy.session_group and str(record["session_group"]) != policy.session_group)
            ):
                raise SessionError("node references a stale or incompatible workflow session")
            return self._handle(record)
        if policy.session_group and (policy.reuse_within_run or policy.reuse_on_retry or policy.force_new_session):
            group = policy.session_group
        else:
            group = f"node/{grant.node_run_id}"
        record = self.store.get_or_create_session(
            grant.run_id,
            group,
            executor,
            force_new=policy.force_new_session,
            now=now,
        )
        self.store.attach_session(
            grant,
            str(record["session_record_id"]),
            now=now,
        )
        return self._handle(record)

    def attach(
        self,
        grant: LeaseGrant,
        handle: SessionHandle,
        *,
        now: datetime | None = None,
    ) -> None:
        if handle.run_id != grant.run_id:
            raise SessionError("session handle belongs to another workflow run")
        self.store.attach_session(grant, handle.session_record_id, now=now)

    def bind_external(
        self,
        grant: LeaseGrant,
        handle: SessionHandle,
        external_session_id: str,
        *,
        now: datetime | None = None,
    ) -> SessionHandle:
        record = self.store.bind_external_session(
            grant,
            handle.session_record_id,
            external_session_id,
            expected_generation=handle.generation,
            now=now,
        )
        return self._handle(record)

    def reconcile_external(
        self,
        grant: LeaseGrant,
        handle: SessionHandle,
        broker: ExternalSessionBroker,
        *,
        now: datetime | None = None,
    ) -> SessionHandle:
        # External broker lookup/start can itself create remote state, so a
        # stale or forged grant must fail before either the already-bound fast
        # path or the broker boundary is reached.
        self.store.assert_live_lease(grant, now=now)
        if handle.run_id != grant.run_id:
            raise SessionError("session handle belongs to another workflow run")
        node_run = self.store.get_node_run(grant.node_run_id)
        if str(node_run["session_id"]) != handle.session_record_id:
            raise SessionError("leased node is not attached to this workflow session")
        if handle.external_session_id:
            return handle
        matches = tuple(str(item).strip() for item in broker.find_exact(handle.owner_name) if str(item).strip())
        if len(matches) > 1:
            raise SessionError("multiple external sessions have the deterministic owner name")
        # The broker lookup can be slow; cancellation or lease expiry during
        # it must prevent a subsequent remote create.
        self.store.assert_live_lease(grant, now=now)
        started: ExternalSessionStart | None = None
        if matches:
            external_id = matches[0]
        else:
            self.store.assert_live_lease(grant, now=now)
            started = broker.start_named(handle.owner_name)
            if not isinstance(started, ExternalSessionStart):
                raise SessionError("external session broker returned an invalid start result")
            external_id = str(started.session_id).strip()
        if not external_id:
            raise SessionError("external session broker returned an empty session ID")
        try:
            self.store.assert_live_lease(grant, now=now)
            return self.bind_external(grant, handle, external_id, now=now)
        except (WorkflowConflictError, LeaseLostError) as exc:
            if started is not None and started.created:
                try:
                    broker.stop(external_id)
                except Exception as compensation_error:
                    raise SessionError(
                        "external session binding failed and compensation could not stop the new session"
                    ) from compensation_error
            refreshed = self.store.get_session(handle.session_record_id)
            if str(refreshed["external_session_id"]) == external_id:
                return self._handle(refreshed)
            raise SessionError("external session binding raced with another owner") from exc


__all__ = [
    "ExternalSessionBroker",
    "ExternalSessionStart",
    "SessionError",
    "SessionHandle",
    "SessionManager",
]
