from __future__ import annotations

import threading
from pathlib import Path

import pytest

from unified_paper_orchestrator.ai_execution import (
    AIExecutionCoordinator,
    AIExecutionError,
    PROVIDER_COMPATIBILITY_TOKEN_BUDGET,
    runtime_provider_config,
)
from unified_paper_orchestrator.ai_execution_policy import API_KEY_MODE, LEGACY_CODEX_MODE
from unified_paper_orchestrator.ai_provider_store import AIProviderStore
import unified_paper_orchestrator.automation as automation_module
from unified_paper_orchestrator.automation import (
    SchedulerAutomationController,
    WritingLegacyAutomationRetiredError,
    parse_toml_like,
)
from unified_paper_orchestrator_v2.contracts import ResultStatus, TaskResult


class ReversingCipher:
    scheme = "test"

    def encrypt(self, plaintext: str) -> str:
        return plaintext[::-1]

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext[::-1]


def _store(tmp_path: Path) -> AIProviderStore:
    return AIProviderStore(tmp_path, cipher=ReversingCipher())


def _profile(store: AIProviderStore):
    profile = store.create_profile(
        profile_id="generic_relay",
        display_name="Generic relay",
        base_url="https://relay.example.test/v1",
        default_model="codex-model",
        reasoning_effort="",
    )
    store.set_api_key(profile.profile_id, "secret")
    return profile


class FakeExecutor:
    def __init__(self, result: TaskResult) -> None:
        self.result = result
        self.tasks = []

    def execute(self, task):
        self.tasks.append(task)
        return self.result


def test_runtime_profile_preserves_exact_endpoint_and_provider_default_effort(tmp_path: Path) -> None:
    profile = _profile(_store(tmp_path))

    runtime = runtime_provider_config(profile)

    assert runtime.base_url == "https://relay.example.test/v1"
    assert runtime.reasoning_effort == ""
    assert runtime.wire_api == "responses"


def test_billable_sdk_canary_marks_profile_compatible_without_persisting_response(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = _profile(store)
    fake = FakeExecutor(
        TaskResult(
            status=ResultStatus.COMPLETED,
            data={"final_response": "PROVIDER_OK"},
            external_ref="thread-123",
        )
    )
    coordinator = AIExecutionCoordinator(
        store,
        sdk_executor_factory=lambda config, resolver: fake,
    )

    result = coordinator.test_profile(profile.profile_id)

    assert result["capability_status"] == "compatible"
    assert result["billable_request"] is True
    assert fake.tasks[0].allowed_tools == []
    assert fake.tasks[0].payload["sandbox"] == "read_only"
    assert fake.tasks[0].token_budget == PROVIDER_COMPATIBILITY_TOKEN_BUDGET == 32_768
    assert result["request_url"] == "https://relay.example.test/v1/responses"
    assert "PROVIDER_OK" not in store.profiles_path.read_text(encoding="utf-8")


def test_mode_switch_is_guarded_by_legacy_and_gateway_activity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = _profile(store)
    profile = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
    )
    settings = store.set_active_profile(profile.profile_id, expected_revision=0)
    coordinator = AIExecutionCoordinator(
        store,
        legacy_status_reader=lambda: {"writing": {"status": "ACTIVE"}},
        gateway_task_counts_reader=lambda: {},
    )

    with pytest.raises(Exception, match="writing"):
        coordinator.set_execution_mode(API_KEY_MODE, expected_revision=settings.revision)
    assert store.load_settings().execution_mode == LEGACY_CODEX_MODE

    coordinator.legacy_status_reader = lambda: {"writing": {"status": "PAUSED"}}
    coordinator.gateway_task_counts_reader = lambda: {"running": 1}
    with pytest.raises(Exception, match="running=1"):
        coordinator.set_execution_mode(API_KEY_MODE, expected_revision=settings.revision)

    coordinator.gateway_task_counts_reader = lambda: {"completed": 2}
    updated = coordinator.set_execution_mode(
        API_KEY_MODE,
        expected_revision=settings.revision,
    )
    assert updated.execution_mode == API_KEY_MODE


def _ready_mode_switch_store(tmp_path: Path) -> tuple[AIProviderStore, int]:
    store = _store(tmp_path / "providers")
    profile = _profile(store)
    profile = store.record_capability_result(
        profile.profile_id,
        "compatible",
        expected_revision=profile.revision,
    )
    settings = store.set_active_profile(profile.profile_id, expected_revision=0)
    return store, settings.revision


def _legacy_controller_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: AIProviderStore,
) -> tuple[SchedulerAutomationController, Path, dict]:
    automation_root = tmp_path / "automations"
    primary = automation_root / "writing-primary" / "automation.toml"
    primary.parent.mkdir(parents=True)
    primary.write_text(
        "\n".join(
            (
                "version = 1",
                'id = "writing-primary"',
                'status = "PAUSED"',
                'rrule = "RRULE:FREQ=MINUTELY;INTERVAL=5"',
                'prompt = "legacy"',
                "",
            )
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "writing_state.json"
    state_path.write_text('{"settings":{"scheduling_mode":"manual"}}', encoding="utf-8")
    monkeypatch.setattr(automation_module, "AUTOMATIONS_ROOT", automation_root)
    controller = SchedulerAutomationController(execution_mode_store=store)
    controller.projects = {
        "writing": {
            "label": "写论文",
            "state_path": state_path,
            "prompt_path": tmp_path / "scheduler_prompt.md",
            "primary_automation_id": "writing-primary",
            "automation_ids": ["writing-primary"],
            "heartbeat_minutes": 5,
        }
    }
    state = {"settings": {"scheduling_mode": "manual"}}
    return controller, primary, state


def test_production_writing_model_scheduler_start_is_permanently_retired(tmp_path: Path) -> None:
    controller = SchedulerAutomationController(execution_mode_store=_store(tmp_path))

    with pytest.raises(WritingLegacyAutomationRetiredError) as direct:
        controller.set_enabled("writing", True, {})
    with pytest.raises(WritingLegacyAutomationRetiredError) as all_stages:
        controller.start_all()

    assert direct.value.code == "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED"
    assert all_stages.value.code == "WRITING_LEGACY_MODEL_SCHEDULER_RETIRED"


def test_atomic_mode_switch_wins_before_legacy_start_without_api_active_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revision = _ready_mode_switch_store(tmp_path)
    controller, primary, state = _legacy_controller_fixture(tmp_path, monkeypatch, store)
    switch_holds_lock = threading.Event()
    release_switch = threading.Event()
    switch_result: list[object] = []
    start_errors: list[BaseException] = []

    def statuses():
        switch_holds_lock.set()
        assert release_switch.wait(3)
        return {"writing": parse_toml_like(primary)}

    coordinator = AIExecutionCoordinator(
        store,
        legacy_status_reader=statuses,
        gateway_task_counts_reader=lambda: {},
    )

    def switch_mode() -> None:
        switch_result.append(coordinator.set_execution_mode(API_KEY_MODE, expected_revision=revision))

    def start_legacy() -> None:
        try:
            controller.set_enabled("writing", True, state)
        except BaseException as exc:
            start_errors.append(exc)

    switch_thread = threading.Thread(target=switch_mode)
    switch_thread.start()
    assert switch_holds_lock.wait(3)
    start_thread = threading.Thread(target=start_legacy)
    start_thread.start()
    release_switch.set()
    switch_thread.join(5)
    start_thread.join(5)

    assert not switch_thread.is_alive() and not start_thread.is_alive()
    assert switch_result and switch_result[0].execution_mode == API_KEY_MODE
    assert start_errors and getattr(start_errors[0], "code", "") == "AI_LEGACY_ACTION_REQUIRES_CODEX_MODE"
    assert parse_toml_like(primary)["status"] == "PAUSED"
    assert store.load_settings().execution_mode == API_KEY_MODE


def test_atomic_legacy_start_wins_before_mode_switch_and_blocks_api_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revision = _ready_mode_switch_store(tmp_path)
    controller, primary, state = _legacy_controller_fixture(tmp_path, monkeypatch, store)
    start_holds_lock = threading.Event()
    release_start = threading.Event()
    switch_errors: list[BaseException] = []
    original = controller._set_enabled_core

    def delayed_start(*args, **kwargs):
        start_holds_lock.set()
        assert release_start.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "_set_enabled_core", delayed_start)
    coordinator = AIExecutionCoordinator(
        store,
        legacy_status_reader=lambda: {"writing": parse_toml_like(primary)},
        gateway_task_counts_reader=lambda: {},
    )

    start_thread = threading.Thread(target=lambda: controller.set_enabled("writing", True, state))
    start_thread.start()
    assert start_holds_lock.wait(3)

    def switch_mode() -> None:
        try:
            coordinator.set_execution_mode(API_KEY_MODE, expected_revision=revision)
        except BaseException as exc:
            switch_errors.append(exc)

    switch_thread = threading.Thread(target=switch_mode)
    switch_thread.start()
    release_start.set()
    start_thread.join(5)
    switch_thread.join(5)

    assert not switch_thread.is_alive() and not start_thread.is_alive()
    assert switch_errors and getattr(switch_errors[0], "code", "") == "AI_EXECUTION_MODE_SWITCH_BLOCKED"
    assert parse_toml_like(primary)["status"] == "ACTIVE"
    assert store.load_settings().execution_mode == LEGACY_CODEX_MODE


def test_failed_sdk_canary_records_incompatible_and_returns_no_model_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = _profile(store)
    fake = FakeExecutor(
        TaskResult(
            status=ResultStatus.COMPLETED,
            data={"final_response": "unexpected long model text"},
            external_ref="thread-123",
        )
    )
    coordinator = AIExecutionCoordinator(
        store,
        sdk_executor_factory=lambda config, resolver: fake,
    )

    with pytest.raises(AIExecutionError):
        coordinator.test_profile(profile.profile_id)

    assert store.get_profile(profile.profile_id).capability_status == "incompatible"


def test_sdk_token_budget_failure_reports_connection_success_and_local_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = _profile(store)
    fake = FakeExecutor(
        TaskResult(
            status=ResultStatus.FATAL_FAILURE,
            blocker_code="CODEX_SDK_TOKEN_BUDGET_EXCEEDED",
            message="output discarded",
            metrics={"input_tokens": 11_118, "output_tokens": 7, "total_tokens": 11_125},
            external_ref="thread-completed-before-local-budget-check",
        )
    )
    coordinator = AIExecutionCoordinator(
        store,
        sdk_executor_factory=lambda config, resolver: fake,
    )

    with pytest.raises(AIExecutionError) as exc_info:
        coordinator.test_profile(profile.profile_id)

    error = exc_info.value
    assert error.code == "CODEX_SDK_TOKEN_BUDGET_EXCEEDED"
    assert "API 连接和 Codex SDK 回合已成功" in str(error)
    assert error.details == {
        "request_url": "https://relay.example.test/v1/responses",
        "sdk_status": "fatal_failure",
        "blocker_code": "CODEX_SDK_TOKEN_BUDGET_EXCEEDED",
        "token_budget": 32_768,
        "total_tokens": 11_125,
    }
    assert store.get_profile(profile.profile_id).capability_status == "error"
    assert "output discarded" not in str(error)


def test_sdk_exception_reports_only_safe_type_and_endpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = _profile(store)

    class RaisingExecutor:
        def execute(self, _task):
            raise RuntimeError("Authorization: Bearer must-not-leak")

    coordinator = AIExecutionCoordinator(
        store,
        sdk_executor_factory=lambda config, resolver: RaisingExecutor(),
    )

    with pytest.raises(AIExecutionError) as exc_info:
        coordinator.test_profile(profile.profile_id)

    error = exc_info.value
    assert error.code == "AI_PROVIDER_TEST_FAILED"
    assert error.details == {
        "request_url": "https://relay.example.test/v1/responses",
        "failure_type": "RuntimeError",
    }
    assert "must-not-leak" not in str(error)
    assert store.get_profile(profile.profile_id).capability_status == "error"
