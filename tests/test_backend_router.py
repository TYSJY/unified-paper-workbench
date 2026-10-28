from __future__ import annotations

import pytest

from unified_paper_orchestrator.backend_router import (
    ActionSpec,
    BackendMode,
    BackendRouteError,
    BackendRouter,
    OperationKind,
    ReadOnlyPreview,
)
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator


SPECS = {
    "status": ActionSpec("status", OperationKind.READ_ONLY),
    "save": ActionSpec("save", OperationKind.MUTATING),
}


def test_default_router_runs_only_legacy() -> None:
    calls: list[str] = []
    router = BackendRouter.legacy_only(SPECS)

    result = router.execute(
        "status",
        legacy=lambda: calls.append("legacy") or {"source": "legacy"},
        shadow_preview=ReadOnlyPreview(
            "candidate",
            lambda _snapshot: calls.append("preview") or {"source": "preview"},
        ),
        new_backend=lambda: calls.append("new") or {"source": "new"},
        snapshot={},
    )

    assert result == {"source": "legacy"}
    assert calls == ["legacy"]
    assert router.decision("status").effective_mode is BackendMode.LEGACY
    assert router.observations() == ()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"missing": "legacy"}, "BACKEND_ACTION_UNKNOWN"),
        ({"status": "typo"}, "BACKEND_MODE_INVALID"),
    ],
)
def test_invalid_configuration_is_rejected_before_execution(overrides: dict[str, str], code: str) -> None:
    with pytest.raises(BackendRouteError) as exc_info:
        BackendRouter(SPECS, overrides)

    assert exc_info.value.code == code


def test_unknown_action_is_rejected_without_calling_either_backend() -> None:
    calls: list[str] = []
    router = BackendRouter.legacy_only(SPECS)

    with pytest.raises(BackendRouteError) as exc_info:
        router.execute(
            "unknown",
            legacy=lambda: calls.append("legacy"),
            new_backend=lambda: calls.append("new"),
        )

    assert exc_info.value.code == "BACKEND_ACTION_UNKNOWN"
    assert calls == []


def test_locked_new_route_is_rejected_without_legacy_fallback() -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"save": "new"})

    with pytest.raises(BackendRouteError) as exc_info:
        router.execute(
            "save",
            legacy=lambda: calls.append("legacy"),
            new_backend=lambda: calls.append("new"),
        )

    assert exc_info.value.code == "BACKEND_NEW_DISABLED"
    assert calls == []


def test_shadow_preview_uses_frozen_snapshot_then_returns_legacy() -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"status": "shadow"})

    def preview(snapshot: object) -> dict[str, bool]:
        calls.append("preview")
        with pytest.raises(TypeError):
            snapshot["ok"] = False  # type: ignore[index]
        return {"ok": bool(snapshot["ok"])}  # type: ignore[index]

    result = router.execute(
        "status",
        legacy=lambda: calls.append("legacy") or {"ok": True},
        shadow_preview=ReadOnlyPreview("v2.status", preview),
        new_backend=lambda: calls.append("new") or {"ok": False},
        snapshot={"ok": True},
    )

    assert result == {"ok": True}
    assert calls == ["preview", "legacy"]
    observation = router.observations()[-1]
    assert observation.status == "compared"
    assert observation.matched is True


def test_shadow_preview_error_cannot_break_legacy_result() -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"status": "shadow"})

    def fail(_snapshot: object) -> dict[str, bool]:
        calls.append("preview")
        raise RuntimeError("preview unavailable")

    result = router.execute(
        "status",
        legacy=lambda: calls.append("legacy") or {"ok": True},
        shadow_preview=ReadOnlyPreview("v2.status", fail),
        snapshot={},
    )

    assert result == {"ok": True}
    assert calls == ["preview", "legacy"]
    observation = router.observations()[-1]
    assert observation.status == "preview_error"
    assert observation.error_type == "RuntimeError"


@pytest.mark.parametrize(
    "preview",
    [
        None,
        ReadOnlyPreview("networked", lambda _snapshot: None, network_access=True),
        ReadOnlyPreview("writer", lambda _snapshot: None, writable_roots=("D:/tmp",)),
        ReadOnlyPreview("side-effect", lambda _snapshot: None, external_side_effects=True),
    ],
)
def test_shadow_rejects_missing_or_unsafe_preview_before_legacy(
    preview: ReadOnlyPreview | None,
) -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"save": "shadow"})

    with pytest.raises(BackendRouteError):
        router.execute(
            "save",
            legacy=lambda: calls.append("legacy"),
            shadow_preview=preview,
            snapshot={"intent": "save"},
        )

    assert calls == []


def test_explicit_new_route_has_exactly_one_writer() -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"save": "new"}, allow_new=True)

    result = router.execute(
        "save",
        legacy=lambda: calls.append("legacy") or "legacy",
        new_backend=lambda: calls.append("new") or "new",
    )

    assert result == "new"
    assert calls == ["new"]


def test_enabled_new_route_requires_candidate_before_any_legacy_write() -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"save": "new"}, allow_new=True)

    with pytest.raises(BackendRouteError) as exc_info:
        router.execute("save", legacy=lambda: calls.append("legacy") or "legacy")

    assert exc_info.value.code == "BACKEND_CANDIDATE_MISSING"
    assert calls == []


def test_new_backend_error_does_not_fall_back_to_legacy() -> None:
    calls: list[str] = []
    router = BackendRouter(SPECS, {"save": "new"}, allow_new=True)

    def fail() -> None:
        calls.append("new")
        raise RuntimeError("uncertain write outcome")

    with pytest.raises(RuntimeError, match="uncertain write outcome"):
        router.execute("save", legacy=lambda: calls.append("legacy"), new_backend=fail)

    assert calls == ["new"]


def test_observations_are_bounded() -> None:
    router = BackendRouter(SPECS, {"status": "shadow"}, observation_limit=2)
    preview = ReadOnlyPreview("v2.status", lambda snapshot: snapshot)
    for value in range(4):
        router.execute("status", legacy=lambda v=value: v, shadow_preview=preview, snapshot=value)

    assert len(router.observations()) == 2


def test_v1_orchestrator_is_wired_legacy_only_by_default() -> None:
    orchestrator = UnifiedOrchestrator()

    assert orchestrator.backend_router.allow_new is False
    assert orchestrator.backend_router.decision("_plan_for").effective_mode is BackendMode.LEGACY
