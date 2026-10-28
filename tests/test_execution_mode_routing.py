from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import unified_paper_orchestrator.automation as automation_module
import unified_paper_orchestrator.ai_provider_store as provider_store_module
import unified_paper_orchestrator.orchestrator as orchestrator_module
import unified_paper_orchestrator.smart_submission as smart_submission_module
from unified_paper_orchestrator.ai_execution_policy import ExecutionModePolicyError
from unified_paper_orchestrator.ai_provider_store import AIProviderStore
from unified_paper_orchestrator.automation import (
    LegacyAutomationUnavailableError,
    SchedulerAutomationController,
    parse_toml_like,
)
from unified_paper_orchestrator.backend_router import BackendMode, BackendRouteError, BackendRouter, OperationKind
from unified_paper_orchestrator.orchestrator import (
    STAGE_BACKEND_ACTIONS,
    V1_BACKEND_ACTION_SPECS,
    UnifiedOrchestrator,
)


class _ModeStore:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def load_settings(self) -> SimpleNamespace:
        return SimpleNamespace(execution_mode=self.mode)


class _Bomb:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected legacy access: {name}")


class _StageBackend:
    def __init__(self, *, fail: bool = False, invalid_result: bool = False) -> None:
        self.fail = fail
        self.invalid_result = invalid_result
        self.calls: list[tuple[object, ...]] = []

    def _result(self, action: str, stage: str = "", single_active: bool = False):
        self.calls.append((action, stage, single_active))
        if self.fail:
            raise RuntimeError("injected new stage backend failure")
        if self.invalid_result:
            return "invalid"
        return {
            "backend": "new",
            "action": action,
            "stage": stage,
            "single_active": single_active,
        }

    def start_stage(self, stage: str, *, single_active: bool = False):
        return self._result("start_stage", stage, single_active)

    def pause_stage(self, stage: str):
        return self._result("pause_stage", stage)

    def start_all(self):
        return self._result("start_all")

    def pause_all(self):
        return self._result("pause_all")


def _orchestrator_for_mode(mode: str) -> UnifiedOrchestrator:
    orchestrator = object.__new__(UnifiedOrchestrator)
    orchestrator.ai_provider_store = _ModeStore(mode)
    orchestrator.adapters = {
        "writing": _Bomb(),
        "figures": _Bomb(),
        "journal_author": _Bomb(),
        "submission": _Bomb(),
    }
    orchestrator.automation = _Bomb()
    return orchestrator


@pytest.mark.parametrize(
    "invoke",
    [
        lambda item: item.resume_writing_papers(["paper-1"]),
        lambda item: item.queue_figure_image2_revisions(["paper-1"]),
        lambda item: item.advance_once(apply=True),
        lambda item: item.start_all(),
        lambda item: item.start_stage("writing"),
        lambda item: item.start_new_writing_session(),
    ],
)
def test_api_mode_never_falls_back_to_v1_execution(invoke) -> None:
    orchestrator = _orchestrator_for_mode("api_key")

    with pytest.raises(ExecutionModePolicyError) as raised:
        invoke(orchestrator)

    assert raised.value.code == "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"


@pytest.mark.parametrize("stage", ("writing", "figures", "journal_author", "submission"))
def test_all_four_default_stage_start_routes_fail_before_legacy_access_in_api_mode(stage: str) -> None:
    orchestrator = _orchestrator_for_mode("api_key")
    orchestrator.backend_router = BackendRouter.legacy_only(V1_BACKEND_ACTION_SPECS)

    with pytest.raises(ExecutionModePolicyError) as raised:
        orchestrator.start_stage(stage, single_active=True)

    assert raised.value.code == "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"


def test_stage_control_specs_are_mutating_and_forbid_shadow_dual_execution() -> None:
    expected = {
        f"stage.{operation}.{stage}"
        for operation in ("start", "pause")
        for stage in ("writing", "figures", "journal_author", "submission")
    } | {"stage.start_all", "stage.pause_all"}

    assert STAGE_BACKEND_ACTIONS == expected
    for action in expected:
        spec = V1_BACKEND_ACTION_SPECS[action]
        assert spec.operation is OperationKind.MUTATING
        assert spec.shadow_allowed is False
    with pytest.raises(BackendRouteError) as raised:
        BackendRouter(V1_BACKEND_ACTION_SPECS, {"stage.start.writing": "shadow"})
    assert raised.value.code == "BACKEND_SHADOW_DISABLED"


@pytest.mark.parametrize(
    ("action", "invoke", "expected_call"),
    [
        (
            "stage.start.figures",
            lambda item: item.start_stage("figures", single_active=True),
            ("start_stage", "figures", True),
        ),
        (
            "stage.pause.journal_author",
            lambda item: item.pause_stage("journal_author"),
            ("pause_stage", "journal_author", False),
        ),
        ("stage.start_all", lambda item: item.start_all(), ("start_all", "", False)),
        ("stage.pause_all", lambda item: item.pause_all(), ("pause_all", "", False)),
    ],
)
def test_explicit_new_stage_route_calls_only_injected_backend(action, invoke, expected_call) -> None:
    orchestrator = _orchestrator_for_mode("api_key")
    backend = _StageBackend()
    orchestrator.stage_execution_backend = backend
    orchestrator.backend_router = BackendRouter(
        V1_BACKEND_ACTION_SPECS,
        {action: "new"},
        allow_new=True,
    )

    result = invoke(orchestrator)

    assert result["backend"] == "new"
    assert backend.calls == [expected_call]
    assert orchestrator.backend_router.decision(action).effective_mode is BackendMode.NEW


def test_new_stage_backend_failure_never_falls_back_to_legacy() -> None:
    orchestrator = _orchestrator_for_mode("api_key")
    backend = _StageBackend(fail=True)
    orchestrator.stage_execution_backend = backend
    orchestrator.backend_router = BackendRouter(
        V1_BACKEND_ACTION_SPECS,
        {"stage.start.submission": "new"},
        allow_new=True,
    )

    with pytest.raises(RuntimeError, match="injected new stage backend failure"):
        orchestrator.start_stage("submission")

    assert backend.calls == [("start_stage", "submission", False)]


@pytest.mark.parametrize("backend", (None, _StageBackend(invalid_result=True)))
def test_new_stage_route_fails_closed_for_missing_or_invalid_candidate(backend) -> None:
    orchestrator = _orchestrator_for_mode("api_key")
    orchestrator.stage_execution_backend = backend
    orchestrator.backend_router = BackendRouter(
        V1_BACKEND_ACTION_SPECS,
        {"stage.start.writing": "new"},
        allow_new=True,
    )

    with pytest.raises(BackendRouteError) as raised:
        orchestrator.start_stage("writing")

    assert raised.value.code in {"BACKEND_CANDIDATE_MISSING", "STAGE_BACKEND_RESULT_INVALID"}


def test_new_stage_start_is_still_bound_to_api_mode_before_backend_access() -> None:
    orchestrator = _orchestrator_for_mode("legacy_codex")
    backend = _StageBackend()
    orchestrator.stage_execution_backend = backend
    orchestrator.backend_router = BackendRouter(
        V1_BACKEND_ACTION_SPECS,
        {"stage.start.figures": "new"},
        allow_new=True,
    )

    with pytest.raises(ExecutionModePolicyError) as raised:
        orchestrator.start_stage("figures")

    assert raised.value.code == "AI_API_STAGE_REQUIRES_API_MODE"
    assert backend.calls == []


def test_api_mode_rejects_enabling_scheduler_settings_before_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        saved = False

        def load_state(self):
            return {"settings": {"scheduling_mode": "manual"}}

        def save_state(self, _state):
            self.saved = True

        def pipeline_mutation_scope(self):
            return nullcontext()

    adapter = Adapter()
    orchestrator = _orchestrator_for_mode("api_key")
    orchestrator.adapters = {"writing": adapter}
    monkeypatch.setattr(orchestrator_module, "_unified_state_lock", nullcontext)

    with pytest.raises(ExecutionModePolicyError) as raised:
        orchestrator.save_stage_scheduler_settings(
            "writing",
            {"scheduling_mode": "auto"},
        )

    assert raised.value.code == "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"
    assert adapter.saved is False


def test_api_mode_still_allows_pause_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        def pause(self):
            return {"enabled": False, "updated": []}

    orchestrator = _orchestrator_for_mode("api_key")
    orchestrator.adapters = {"writing": Adapter()}
    orchestrator.sync = lambda persist=False: {"history": []}
    orchestrator.save = lambda _state: None
    monkeypatch.setattr(orchestrator_module, "_unified_state_lock", nullcontext)

    paused = orchestrator.pause_stage("writing")
    orchestrator.sync = lambda persist=False: {"paper_queue": [], "papers": {}}
    planned = orchestrator.advance_once(apply=False)

    assert paused["last_scheduler_result"]["result"]["ok"] is False
    assert paused["last_scheduler_result"]["result"]["action"] == "pause_unverified"
    assert planned["status"] == "idle"


def test_missing_legacy_bridge_is_never_bootstrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    automations_root = tmp_path / "automations"
    smart_automation = automations_root / "automation" / "automation.toml"
    smart_automation.parent.mkdir(parents=True)
    smart_automation.write_text('id = "smart-submission"\nstatus = "PAUSED"\n', encoding="utf-8")
    state_path = tmp_path / "figure_state.json"
    state_path.write_text(
        '{"settings":{"scheduler_target_thread_id":"thread-legacy"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", automations_root)
    controller = SchedulerAutomationController()
    controller.projects = {
        "figures": {
            "state_path": state_path,
            "primary_automation_id": "figure-primary",
            "automation_ids": ["figure-primary"],
        }
    }

    with pytest.raises(LegacyAutomationUnavailableError) as raised:
        controller.require_controllable(
            "figures",
            {"settings": {"scheduler_target_thread_id": "thread-legacy"}},
        )

    assert raised.value.code == "LEGACY_CODEX_AUTOMATION_UNAVAILABLE"
    assert smart_automation.is_file()
    assert not (automations_root / "figure-primary" / "automation.toml").exists()


def test_api_mode_uses_program_sdk_runtime_but_legacy_canary_stays_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(provider_store_module, "STATE_DIR", state_root)
    store = AIProviderStore(
        state_root / "ai_providers",
        protector=lambda value: value[::-1],
        unprotector=lambda value: value[::-1],
    )
    profile = store.create_profile(
        profile_id="provider",
        display_name="Provider",
        base_url="https://api.example.test/v1",
        default_model="model",
    )
    store.set_api_key(profile.profile_id, "secret")
    profile = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
    )
    settings = store.set_active_profile(profile.profile_id, expected_revision=0)
    store.set_execution_mode("api_key", expected_revision=settings.revision)
    smart_root = state_root / "smart_submission"
    automation_path = tmp_path / "automations" / "automation" / "automation.toml"
    monkeypatch.setattr(smart_submission_module, "SMART_SUBMISSION_DIR", smart_root)
    monkeypatch.setattr(smart_submission_module, "SMART_STATE_PATH", smart_root / "smart_submission_state.json")
    monkeypatch.setattr(smart_submission_module, "SMART_AUTOMATION_PATH", automation_path)
    import unified_paper_orchestrator.smart_submission_sdk_runtime as sdk_runtime_module

    monkeypatch.setattr(
        sdk_runtime_module,
        "start_smart_submission_sdk_background",
        lambda _paper_ids=None: {"ok": True, "started": True},
    )

    wakeup = smart_submission_module.set_smart_submission_automation_fast_wakeup(True)
    canary = smart_submission_module.start_single_smart_canary(
        "paper-1",
        expected_route_revision=1,
        orchestrator=_Bomb(),
    )

    assert wakeup["enabled"] is False
    assert wakeup["program_runtime"]["started"] is True
    assert canary["ok"] is False
    assert canary["code"] == "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"
    assert not automation_path.exists()
    persisted = smart_submission_module.read_json(
        smart_root / "smart_submission_state.json", {}
    )
    assert persisted["settings"]["trigger_owner"] == "program_timer"
    assert persisted["settings"]["legacy_automation_retired"] is True


def test_direct_start_all_preflights_every_stage_before_first_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    automation_root = tmp_path / "automations"
    writing_primary = automation_root / "writing-primary" / "automation.toml"
    writing_primary.parent.mkdir(parents=True)
    writing_primary.write_text(
        'id = "writing-primary"\nstatus = "PAUSED"\nprompt = "legacy"\n',
        encoding="utf-8",
    )
    writing_state = tmp_path / "writing_state.json"
    figures_state = tmp_path / "figures_state.json"
    writing_state.write_text('{"settings":{}}', encoding="utf-8")
    figures_state.write_text('{"settings":{}}', encoding="utf-8")
    mode_store = AIProviderStore(
        tmp_path / "providers",
        protector=lambda value: value[::-1],
        unprotector=lambda value: value[::-1],
    )
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", automation_root)
    controller = SchedulerAutomationController(execution_mode_store=mode_store)
    controller.projects = {
        "writing": {
            "label": "写论文",
            "state_path": writing_state,
            "prompt_path": tmp_path / "writing_prompt.md",
            "primary_automation_id": "writing-primary",
            "automation_ids": ["writing-primary"],
            "heartbeat_minutes": 5,
        },
        "figures": {
            "label": "画图",
            "state_path": figures_state,
            "prompt_path": tmp_path / "figures_prompt.md",
            "primary_automation_id": "figures-primary",
            "automation_ids": ["figures-primary"],
            "heartbeat_minutes": 5,
        },
    }

    with pytest.raises(LegacyAutomationUnavailableError):
        controller.start_all()

    assert parse_toml_like(writing_primary)["status"] == "PAUSED"
    assert not (automation_root / "figures-primary" / "automation.toml").exists()
