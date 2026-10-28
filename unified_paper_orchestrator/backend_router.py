from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from types import MappingProxyType
from typing import Any, TypeVar


T = TypeVar("T")
_MISSING = object()


class BackendMode(str, Enum):
    """Internal implementation selected for one registered V1 action."""

    LEGACY = "legacy"
    SHADOW = "shadow"
    NEW = "new"


class OperationKind(str, Enum):
    """Side-effect class fixed by the action registry, never by a call site."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


@dataclass(frozen=True)
class ActionSpec:
    name: str
    operation: OperationKind
    shadow_allowed: bool = True


@dataclass(frozen=True)
class ReadOnlyPreview:
    """A declared pure projection that may be evaluated in shadow mode.

    The router also freezes the supplied snapshot recursively. Python cannot
    sandbox arbitrary callables in-process, so only reviewed pure functions may
    be wrapped in this descriptor.
    """

    name: str
    evaluate: Callable[[Any], Any]
    network_access: bool = False
    writable_roots: tuple[str, ...] = ()
    external_side_effects: bool = False


@dataclass(frozen=True)
class RouteDecision:
    action: str
    requested_mode: str
    effective_mode: BackendMode
    operation: OperationKind
    reason: str


@dataclass(frozen=True)
class ShadowObservation:
    """Value-free comparison metadata safe for normal diagnostics."""

    action: str
    status: str
    matched: bool | None = None
    error_type: str = ""


class BackendRouteError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _freeze(value: Any) -> Any:
    """Recursively turn normal JSON-like containers into immutable views."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


class BackendRouter:
    """Replace V1 internals one registered action at a time without dual writes."""

    def __init__(
        self,
        specs: Mapping[str, ActionSpec],
        overrides: Mapping[str, str | BackendMode] | None = None,
        *,
        allow_new: bool = False,
        observation_limit: int = 200,
    ) -> None:
        self._specs: dict[str, ActionSpec] = {}
        for action, spec in specs.items():
            action_name = str(action).strip()
            if not action_name or action_name != spec.name:
                raise BackendRouteError(
                    "BACKEND_ACTION_SPEC_INVALID",
                    f"Action registry key and spec name must be the same non-empty value: {action!r}.",
                )
            self._specs[action_name] = spec

        self._overrides: dict[str, BackendMode] = {}
        for action, mode in (overrides or {}).items():
            action_name = str(action).strip()
            spec = self._specs.get(action_name)
            if spec is None:
                raise BackendRouteError(
                    "BACKEND_ACTION_UNKNOWN",
                    f"Backend override references an unregistered action: {action_name!r}.",
                )
            raw_mode = mode.value if isinstance(mode, BackendMode) else str(mode).strip().lower()
            try:
                parsed_mode = BackendMode(raw_mode)
            except ValueError as exc:
                raise BackendRouteError(
                    "BACKEND_MODE_INVALID",
                    f"Invalid backend mode for {action_name!r}: {raw_mode!r}.",
                ) from exc
            if parsed_mode is BackendMode.SHADOW and not spec.shadow_allowed:
                raise BackendRouteError(
                    "BACKEND_SHADOW_DISABLED",
                    f"Shadow evaluation is not allowed for {action_name!r}.",
                )
            self._overrides[action_name] = parsed_mode

        self._allow_new = bool(allow_new)
        self._observation_limit = max(1, int(observation_limit))
        self._observations: list[ShadowObservation] = []
        self._lock = Lock()

    @classmethod
    def legacy_only(cls, specs: Mapping[str, ActionSpec]) -> BackendRouter:
        return cls(specs)

    @property
    def allow_new(self) -> bool:
        return self._allow_new

    def decision(self, action: str) -> RouteDecision:
        spec = self._spec_for(action)
        configured = spec.name in self._overrides
        mode = self._overrides.get(spec.name, BackendMode.LEGACY)
        if mode is BackendMode.NEW and not self._allow_new:
            raise BackendRouteError(
                "BACKEND_NEW_DISABLED",
                f"New backend is configured for {spec.name!r}, but the process safety latch is closed.",
            )
        return RouteDecision(
            action=spec.name,
            requested_mode=mode.value,
            effective_mode=mode,
            operation=spec.operation,
            reason="configured" if configured else "default_legacy",
        )

    def execute(
        self,
        action: str,
        *,
        legacy: Callable[[], T],
        shadow_preview: ReadOnlyPreview | None = None,
        new_backend: Callable[[], T] | None = None,
        snapshot: Any = _MISSING,
    ) -> T:
        """Execute exactly one authoritative backend for a registered action."""

        decision = self.decision(action)
        if decision.effective_mode is BackendMode.LEGACY:
            return legacy()

        if decision.effective_mode is BackendMode.SHADOW:
            self._validate_preview(decision.action, shadow_preview, snapshot)
            assert shadow_preview is not None  # Narrowed by _validate_preview.
            try:
                preview_value = shadow_preview.evaluate(_freeze(snapshot))
            except Exception as exc:  # A pure preview failure cannot break stable V1.
                self._record(
                    ShadowObservation(
                        action=decision.action,
                        status="preview_error",
                        error_type=type(exc).__name__,
                    )
                )
                return legacy()

            legacy_value = legacy()
            try:
                matched = preview_value == legacy_value
            except Exception as exc:
                self._record(
                    ShadowObservation(
                        action=decision.action,
                        status="comparison_error",
                        error_type=type(exc).__name__,
                    )
                )
                return legacy_value
            self._record(ShadowObservation(action=decision.action, status="compared", matched=bool(matched)))
            return legacy_value

        if new_backend is None:
            raise BackendRouteError(
                "BACKEND_CANDIDATE_MISSING",
                f"New backend is enabled for {decision.action!r}, but no implementation was supplied.",
            )
        # Never fall back after a new implementation starts: an irreversible
        # write may have succeeded even when its response is uncertain.
        return new_backend()

    def observations(self) -> tuple[ShadowObservation, ...]:
        with self._lock:
            return tuple(self._observations)

    def clear_observations(self) -> None:
        with self._lock:
            self._observations.clear()

    def _spec_for(self, action: str) -> ActionSpec:
        action_name = str(action).strip()
        spec = self._specs.get(action_name)
        if spec is None:
            raise BackendRouteError(
                "BACKEND_ACTION_UNKNOWN",
                f"Backend action is not registered: {action_name!r}.",
            )
        return spec

    @staticmethod
    def _validate_preview(action: str, preview: ReadOnlyPreview | None, snapshot: Any) -> None:
        if preview is None:
            raise BackendRouteError(
                "BACKEND_SHADOW_PREVIEW_MISSING",
                f"Shadow mode for {action!r} requires a reviewed read-only preview.",
            )
        if not preview.name.strip():
            raise BackendRouteError("BACKEND_SHADOW_PREVIEW_INVALID", "Read-only preview name is empty.")
        if preview.network_access or preview.external_side_effects or tuple(preview.writable_roots):
            raise BackendRouteError(
                "BACKEND_SHADOW_CAPABILITY_DENIED",
                f"Shadow preview {preview.name!r} requests a forbidden capability.",
            )
        if snapshot is _MISSING:
            raise BackendRouteError(
                "BACKEND_SHADOW_SNAPSHOT_MISSING",
                f"Shadow mode for {action!r} requires an explicit snapshot.",
            )

    def _record(self, observation: ShadowObservation) -> None:
        with self._lock:
            self._observations.append(observation)
            del self._observations[: -self._observation_limit]

