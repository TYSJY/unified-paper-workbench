from __future__ import annotations

import copy
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from unified_paper_orchestrator.full_pipeline import (
    FullPipelineCoordinator,
    FullPipelineError,
    FullPipelineRuntime,
    FullPipelineStore,
    _status_from_store,
)
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic
from unified_paper_orchestrator.writing_gate_policy import AUTO_TRUST_MODE


class FakeAdapter:
    def __init__(self, parallel: int) -> None:
        self.state: dict[str, Any] = {"settings": {"max_parallel_workers": parallel}}
        self.start_calls = 0
        self.resume_calls: list[list[str]] = []

    def load_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def save_state(self, state: dict[str, Any]) -> None:
        self.state = copy.deepcopy(state)

    def _start_sdk_background(
        self,
        *,
        priority_paper_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        self.state["runtime_priority_keys"] = list(priority_paper_keys or [])
        return {"ok": True, "started": True}

    def start(self) -> dict[str, Any]:
        self.start_calls += 1
        self.state.setdefault("settings", {})["scheduling_mode"] = "auto"
        return {"ok": True, "started": True}

    def resume(self) -> dict[str, Any]:
        result = self.start()
        result["action"] = "resume"
        return result

    def resume_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = [str(value) for value in paper_ids]
        self.resume_calls.append(ids)
        return {"ok": True, "started": True, "paper_ids": ids}

    def pipeline_mutation_scope(self):
        return nullcontext()


def stage(status: str, *, artifact: str = "", detail: str = "") -> dict[str, Any]:
    return {"status": status, "artifact": artifact, "detail": detail}


class FakeOrchestrator:
    def __init__(self, *, auto_trust: bool = True) -> None:
        self.auto_trust = auto_trust
        self.adapters = {
            "writing": FakeAdapter(2),
            "figures": FakeAdapter(3),
            "journal_author": FakeAdapter(4),
        }
        self.papers: dict[str, dict[str, Any]] = {}
        self.start_contexts: list[dict[str, Any]] = []
        self.figure_calls: list[str] = []
        self.journal_calls: list[str] = []
        self.figure_failure = ""
        self.command_count_override: int | None = None

    def writing_gate_policy(self) -> dict[str, Any]:
        return {"mode": AUTO_TRUST_MODE if self.auto_trust else "strict"}

    def start_new_writing_sessions(
        self,
        count: int,
        *,
        single_active: bool,
        request_context: dict[str, Any],
    ) -> dict[str, Any]:
        assert single_active is False
        self.start_contexts.append(copy.deepcopy(request_context))
        command_count = count if self.command_count_override is None else self.command_count_override
        commands = [
            {
                "id": f"command-{index}",
                "reserved_pipeline_prefix": f"2026-07-20_{index:03d}",
            }
            for index in range(1, command_count + 1)
        ]
        self.adapters["writing"].state["commands"] = copy.deepcopy(commands)
        return {
            "last_scheduler_result": {
                "results": {
                    "writing": {"legacy_result": {"commands": commands}},
                }
            }
        }

    def sync(self) -> dict[str, Any]:
        return {"papers": copy.deepcopy(self.papers)}

    def queue_figures_from_step1(self, paper_ids: list[str]) -> dict[str, Any]:
        paper_id = paper_ids[0]
        self.figure_calls.append(paper_id)
        result = (
            {"ok": False, "error": self.figure_failure}
            if self.figure_failure
            else {"ok": True, "legacy_result": {"queued": True}}
        )
        return {"last_scheduler_result": {"results": {paper_id: result}}}

    def run_journal_author_papers(
        self,
        paper_ids: list[str],
        *,
        run_mode: str,
    ) -> dict[str, Any]:
        assert run_mode == "full"
        paper_id = paper_ids[0]
        self.journal_calls.append(paper_id)
        return {
            "last_scheduler_result": {
                "results": {paper_id: {"ok": True, "result": {"queued": True}}}
            }
        }


class SmartServices:
    def __init__(self) -> None:
        self.apply_calls: list[int] = []
        self.restore_calls: list[dict[str, Any]] = []
        self.prepare_calls: list[str] = []
        self.start_calls: list[str] = []

    def apply(self, *, max_parallel_workers: int) -> dict[str, Any]:
        self.apply_calls.append(max_parallel_workers)
        return {
            "ok": True,
            "previous": {"max_parallel_workers": {"present": True, "value": 2}},
            "expected": {"max_parallel_workers": max_parallel_workers},
        }

    def restore(self, *, previous: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
        self.restore_calls.append({"previous": previous, "expected": expected})
        return {"ok": True, "restored": ["max_parallel_workers"]}

    def prepare(self, paper_ids: list[str], *, orchestrator: Any) -> dict[str, Any]:
        assert orchestrator is not None
        self.prepare_calls.extend(paper_ids)
        return {"queued": list(paper_ids), "resumed": [], "skipped": {}}

    def start(self, paper_ids: list[str]) -> dict[str, Any]:
        self.start_calls.extend(paper_ids)
        return {"ok": True, "started": list(paper_ids)}


def make_coordinator(
    tmp_path: Path,
    orchestrator: FakeOrchestrator,
    smart: SmartServices | None = None,
) -> tuple[FullPipelineCoordinator, Path]:
    services = smart or SmartServices()
    smart_state = tmp_path / "smart.json"
    write_json_atomic(smart_state, {"papers": {}})
    coordinator = FullPipelineCoordinator(
        orchestrator,
        store=FullPipelineStore(tmp_path / "full.json", tmp_path / "full.lock"),
        smart_state_path=smart_state,
        smart_prepare=services.prepare,
        smart_start=services.start,
        smart_policy_apply=services.apply,
        smart_policy_restore=services.restore,
    )
    return coordinator, smart_state


def bind_one_paper(
    coordinator: FullPipelineCoordinator,
    orchestrator: FakeOrchestrator,
    *,
    paper_id: str = "2026-07-20_001_pipeline-test",
) -> str:
    writing = orchestrator.adapters["writing"]
    writing.state["sessions"] = {
        paper_id: {"reserved_pipeline_prefix": "2026-07-20_001"}
    }
    orchestrator.papers[paper_id] = {
        "stages": {
            "writing": stage("running", detail="Step 2 / 10"),
            "figures": stage("missing"),
            "journal_author": stage("missing"),
        }
    }
    coordinator.tick()
    return paper_id


def clear_retry_delay(coordinator: FullPipelineCoordinator, action: str) -> None:
    def mutate(state: dict[str, Any]) -> None:
        batch = state["batches"][state["active_batch_id"]]
        marker = batch["slots"][0]["dispatches"][action]
        marker["next_retry_at_epoch"] = 0

    coordinator.store.mutate(mutate)


def test_read_only_status_is_idle_before_any_batch(tmp_path: Path) -> None:
    status = _status_from_store(FullPipelineStore(tmp_path / "full.json", tmp_path / "lock"))

    assert status["active"] is False
    assert status["status"] == "idle"


def test_store_reads_atomic_snapshot_without_competing_for_writer_lock(tmp_path: Path) -> None:
    state_path = tmp_path / "full.json"
    lock_path = tmp_path / "full.lock"
    write_json_atomic(
        state_path,
        {
            "schema": "unified-paper-orchestrator/full-pipeline/v1",
            "revision": 1,
            "active_batch_id": "",
            "batches": {},
            "history": [],
            "updated_at": "",
        },
    )
    lock_path.write_text(
        f"version=2\npid={os.getpid()}\nowner_token=ui-reader-race\n",
        encoding="utf-8",
    )

    loaded = FullPipelineStore(state_path, lock_path).load()

    assert loaded["revision"] == 1
    assert lock_path.exists()


def test_store_mutation_retries_transient_cross_process_lock(tmp_path: Path) -> None:
    state_path = tmp_path / "full.json"
    lock_path = tmp_path / "full.lock"
    lock_path.write_text(
        f"version=2\npid={os.getpid()}\nowner_token=transient-ui-lock\n",
        encoding="utf-8",
    )

    def release_transient_lock() -> None:
        time.sleep(0.1)
        lock_path.unlink(missing_ok=True)

    releaser = threading.Thread(target=release_transient_lock)
    releaser.start()
    try:
        store = FullPipelineStore(
            state_path,
            lock_path,
            lock_wait_seconds=1.0,
            lock_poll_seconds=0.02,
        )
        store.mutate(lambda state: state.update({"retry_marker": "retried"}))
    finally:
        releaser.join(timeout=1.0)

    assert store.load()["retry_marker"] == "retried"


def test_start_requires_step5_auto_trust(tmp_path: Path) -> None:
    coordinator, _ = make_coordinator(tmp_path, FakeOrchestrator(auto_trust=False))

    with pytest.raises(FullPipelineError) as error:
        coordinator.start_batch(1)

    assert error.value.code == "FULL_PIPELINE_STEP5_AUTO_TRUST_REQUIRED"
    assert coordinator.status()["status"] == "idle"


def test_start_creates_exact_slots_and_applies_bounded_stage_limits(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    smart = SmartServices()
    coordinator, _ = make_coordinator(tmp_path, orchestrator, smart)

    status = coordinator.start_batch(7)

    assert status["requested_count"] == 7
    assert len(status["papers"]) == 7
    assert orchestrator.start_contexts[0]["full_pipeline_batch_id"] == status["batch_id"]
    assert orchestrator.adapters["writing"].state["settings"]["max_parallel_workers"] == 7
    assert orchestrator.adapters["figures"].state["settings"]["max_parallel_workers"] == 4
    assert orchestrator.adapters["journal_author"].state["settings"]["max_parallel_workers"] == 6
    assert smart.apply_calls == [7]


def test_failed_start_restores_original_limits(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    orchestrator.command_count_override = 0
    smart = SmartServices()
    coordinator, _ = make_coordinator(tmp_path, orchestrator, smart)

    with pytest.raises(FullPipelineError):
        coordinator.start_batch(2)

    assert orchestrator.adapters["writing"].state["settings"]["max_parallel_workers"] == 2
    assert orchestrator.adapters["figures"].state["settings"]["max_parallel_workers"] == 3
    assert orchestrator.adapters["journal_author"].state["settings"]["max_parallel_workers"] == 4
    assert len(smart.restore_calls) == 1
    assert coordinator.status()["limits_restored"] is True
    assert coordinator.status()["status"] == "failed_start"
    assert coordinator.status()["active"] is False


def test_runtime_retry_recovers_existing_failed_start_without_duplicate_writing_commands(
    tmp_path: Path,
) -> None:
    orchestrator = FakeOrchestrator()
    smart = SmartServices()
    coordinator, _ = make_coordinator(tmp_path, orchestrator, smart)
    orchestrator.command_count_override = 0
    with pytest.raises(FullPipelineError):
        coordinator.start_batch(2)

    failed_batch_id = coordinator.status()["batch_id"]
    existing_commands = [
        {
            "id": "survived-command-1",
            "action": "new_session",
            "status": "pending",
            "reserved_pipeline_prefix": "2026-07-23_001",
            "full_pipeline_batch_id": failed_batch_id,
            "sdk_paper_key": "new-session:survived-command-1",
        },
        {
            "id": "survived-command-2",
            "action": "new_session",
            "status": "pending",
            "reserved_pipeline_prefix": "2026-07-23_002",
            "full_pipeline_batch_id": failed_batch_id,
            "sdk_paper_key": "new-session:survived-command-2",
        },
    ]
    orchestrator.adapters["writing"].state["commands"] = copy.deepcopy(existing_commands)
    orchestrator.command_count_override = None
    runtime = FullPipelineRuntime(coordinator, poll_seconds=60)

    recovered = runtime.start_new(2)
    runtime.shutdown()

    assert recovered["batch_id"] == failed_batch_id
    assert recovered["status"] == "running"
    assert [item["writing_command_id"] for item in recovered["papers"]] == [
        "survived-command-1",
        "survived-command-2",
    ]
    assert len(orchestrator.start_contexts) == 1
    assert orchestrator.adapters["writing"].state["runtime_priority_keys"] == [
        "new-session:survived-command-1",
        "new-session:survived-command-2",
    ]


def test_identity_and_each_stage_handoff_are_idempotent(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    smart = SmartServices()
    coordinator, smart_state = make_coordinator(tmp_path, orchestrator, smart)
    coordinator.start_batch(1)
    paper_id = bind_one_paper(coordinator, orchestrator)

    assert coordinator.status()["papers"][0]["paper_id"] == paper_id

    orchestrator.papers[paper_id]["stages"]["writing"] = stage(
        "complete", artifact="draft_v6.tex"
    )
    coordinator.tick()
    coordinator.tick()
    assert orchestrator.figure_calls == [paper_id]

    orchestrator.papers[paper_id]["stages"]["figures"] = stage(
        "complete", artifact="main.tex"
    )
    coordinator.tick()
    coordinator.tick()
    assert orchestrator.journal_calls == [paper_id]

    orchestrator.papers[paper_id]["stages"]["journal_author"] = stage(
        "complete", artifact="submission"
    )
    coordinator.tick()
    coordinator.tick()
    assert smart.prepare_calls == [paper_id]
    assert smart.start_calls == [paper_id]

    write_json_atomic(smart_state, {"papers": {paper_id: {"status": "monitoring"}}})
    result = coordinator.tick()

    assert result["completed"] is True
    assert coordinator.status()["status"] == "completed"
    assert orchestrator.adapters["writing"].state["settings"]["max_parallel_workers"] == 2
    assert orchestrator.adapters["figures"].state["settings"]["max_parallel_workers"] == 3
    assert orchestrator.adapters["journal_author"].state["settings"]["max_parallel_workers"] == 4
    assert len(smart.restore_calls) == 1


def test_failed_handoff_retries_three_times_then_needs_attention(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    orchestrator.figure_failure = "temporary queue failure"
    coordinator, _ = make_coordinator(tmp_path, orchestrator)
    coordinator.start_batch(1)
    paper_id = bind_one_paper(coordinator, orchestrator)
    orchestrator.papers[paper_id]["stages"]["writing"] = stage(
        "complete", artifact="draft_v6.tex"
    )

    coordinator.tick()
    clear_retry_delay(coordinator, "figures")
    coordinator.tick()
    clear_retry_delay(coordinator, "figures")
    coordinator.tick()

    status = coordinator.status()
    marker = status["papers"][0]["dispatches"]["figures"]
    assert marker["attempts"] == 3
    assert status["status"] == "needs_attention"
    assert status["papers"][0]["status"] == "needs_attention"
    assert len(orchestrator.figure_calls) == 3


def test_pause_resume_survives_a_new_coordinator_instance(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    coordinator, smart_state = make_coordinator(tmp_path, orchestrator)
    coordinator.start_batch(1)

    paused = coordinator.pause()
    replacement = FullPipelineCoordinator(
        orchestrator,
        store=coordinator.store,
        smart_state_path=smart_state,
        smart_prepare=SmartServices().prepare,
        smart_start=SmartServices().start,
        smart_policy_apply=SmartServices().apply,
        smart_policy_restore=SmartServices().restore,
    )

    assert paused["status"] == "paused"
    assert replacement.status()["status"] == "paused"
    assert replacement.resume()["status"] == "running"
    assert replacement.status()["status"] == "running"


def test_limit_restore_preserves_a_user_edit(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    coordinator, _ = make_coordinator(tmp_path, orchestrator)
    coordinator.start_batch(1)
    state = coordinator.store.load()
    batch = state["batches"][state["active_batch_id"]]
    orchestrator.adapters["figures"].state["settings"]["max_parallel_workers"] = 9

    restored = coordinator._restore_stage_limits(batch["stage_limits"])

    assert restored["figures"]["restored"] is False
    assert restored["figures"]["reason"] == "user_or_external_change_preserved"
    assert orchestrator.adapters["figures"].state["settings"]["max_parallel_workers"] == 9
    assert read_json(coordinator.store.path, {})["schema"].endswith("/v1")


def test_resume_if_active_restarts_process_local_writing_worker(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator()
    coordinator, _ = make_coordinator(tmp_path, orchestrator)
    started = coordinator.start_batch(1)
    assert started["active"] is True
    writing = orchestrator.adapters["writing"]
    assert writing.start_calls == 0
    runtime = FullPipelineRuntime(coordinator, poll_seconds=60)

    resumed = runtime.resume_if_active()
    runtime.shutdown()

    assert resumed["workers"]["ok"] is True
    assert resumed["workers"]["status"] == "resumed"
    assert resumed["workers"]["stages"]["writing"]["action"] == "resume"
    assert writing.start_calls == 1
    assert writing.state["settings"]["scheduling_mode"] == "auto"
    assert resumed["runtime"]["started"] is True


def test_runtime_recovers_after_one_unexpected_tick_error() -> None:
    class FlakyCoordinator:
        def __init__(self) -> None:
            self.calls = 0
            self.errors: list[str] = []

        def tick(self) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary projection failure")
            return {"ok": True, "active": False}

        def record_runtime_error(self, error: BaseException) -> None:
            self.errors.append(str(error))

        def status(self) -> dict[str, Any]:
            return {"ok": True, "active": self.calls < 2, "status": "running"}

    coordinator = FlakyCoordinator()
    runtime = FullPipelineRuntime(
        coordinator,
        poll_seconds=0.01,
        error_retry_seconds=0.01,
    )

    runtime.start_background()
    for _ in range(120):
        if not runtime.status()["runtime_running"]:
            break
        import time

        time.sleep(0.05)

    assert coordinator.calls == 2
    assert coordinator.errors == ["temporary projection failure"]
    assert runtime.status()["runtime_running"] is False
