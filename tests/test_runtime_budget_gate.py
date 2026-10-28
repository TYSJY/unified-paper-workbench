from __future__ import annotations

import copy
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from ctypes import wintypes

import pytest

import unified_paper_orchestrator.__main__ as cli_module
import unified_paper_orchestrator.runtime_budget_gate as gate_module
from unified_paper_orchestrator.runtime_budget_gate import (
    CALIBRATION_MIN_SAMPLE_COUNT,
    MAX_SAFE_QUANTITY,
    RUNTIME_BUDGET_CALIBRATION_ENVIRONMENT_MISMATCH,
    RUNTIME_BUDGET_CALIBRATION_FUTURE,
    RUNTIME_BUDGET_CALIBRATION_INVALID,
    RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING,
    RUNTIME_BUDGET_CALIBRATION_REQUIRED,
    RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH,
    RUNTIME_BUDGET_CALIBRATION_STALE,
    RUNTIME_BUDGET_CALIBRATION_WINDOW_INVALID,
    RUNTIME_BUDGET_COMMAND_MISMATCH,
    RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
    RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED,
    RUNTIME_BUDGET_DISK_INSUFFICIENT,
    RUNTIME_BUDGET_DISK_INSPECTION_FAILED,
    RUNTIME_BUDGET_ESTIMATE_OVERFLOW,
    RUNTIME_BUDGET_OUTPUT_CAP_EXCEEDED,
    RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP,
    RUNTIME_BUDGET_READY,
    RUNTIME_BUDGET_WORKLOAD_INVALID,
    RuntimeLaunchOutcome,
    capture_current_environment,
    environment_fingerprint,
    evaluate_runtime_budget,
    evaluate_runtime_budget_files,
    launch_fingerprint,
    launch_if_runtime_budget_allows,
    launch_runtime_budget_files,
)
from unified_paper_orchestrator.windows_process_tree import run_in_kill_on_close_job


NOW = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
GIB = 1024**3
MIB = 1024**2
BASE = Path.cwd().absolute()
ARGV = [str(Path(sys.executable).absolute()), "formal_experiment.py", "--workers", "2"]
ENVIRONMENT = capture_current_environment()
ENVIRONMENT_FINGERPRINT = environment_fingerprint(ENVIRONMENT)
LAUNCH_FINGERPRINT = launch_fingerprint(ARGV, BASE)


def workload(*, output_path: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workload_id": "revision-5-lite",
        "kind": "formal_experiment",
        "environment_key": "verified-current-host-workers2",
        "environment_fingerprint": ENVIRONMENT_FINGERPRINT,
        "launch_fingerprint": LAUNCH_FINGERPRINT,
        "output_path": output_path or str(BASE / "results" / "formal"),
        "hard_stop_seconds": 7200,
        "deadline_reserve_seconds": 600,
        "fixed_overhead_p95_seconds": 60,
        "minimum_free_after_bytes": GIB,
        "maximum_output_bytes": 2 * GIB,
        "calibration_max_age_seconds": 24 * 60 * 60,
        "phases": [
            {
                "phase_id": "correctness",
                "calibration_key": "correctness-medium",
                "execution_shape": "workers=2;raw=retain;compression=gzip",
                "units": 40,
            },
            {
                "phase_id": "aggregate",
                "calibration_key": "aggregate",
                "execution_shape": "workers=1;input=full",
                "units": 1,
            },
        ],
    }


def calibration() -> dict[str, object]:
    return {
        "schema_version": 1,
        "workload_id": "revision-5-lite",
        "environment_key": "verified-current-host-workers2",
        "environment_fingerprint": ENVIRONMENT_FINGERPRINT,
        "launch_fingerprint": LAUNCH_FINGERPRINT,
        "captured_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "duration_seconds": 300,
        "samples": [
            {
                "calibration_key": "correctness-medium",
                "execution_shape": "workers=2;raw=retain;compression=gzip",
                "sample_count": CALIBRATION_MIN_SAMPLE_COUNT,
                "p95_seconds_per_unit": 60,
                "p95_output_bytes_per_unit": 10 * MIB,
            },
            {
                "calibration_key": "aggregate",
                "execution_shape": "workers=1;input=full",
                "sample_count": CALIBRATION_MIN_SAMPLE_COUNT,
                "p95_seconds_per_unit": 120,
                "p95_output_bytes_per_unit": 50 * MIB,
            },
        ],
    }


def binding(*, cwd: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "environment_key": "verified-current-host-workers2",
        "environment": copy.deepcopy(ENVIRONMENT),
        "execution_shapes": [
            {
                "phase_id": "correctness",
                "execution_shape": "workers=2;raw=retain;compression=gzip",
            },
            {"phase_id": "aggregate", "execution_shape": "workers=1;input=full"},
        ],
        "argv": list(ARGV),
        "cwd": cwd or str(BASE),
    }


def disk(free: int = 10 * GIB):
    return lambda _path: SimpleNamespace(total=100 * GIB, free=free)


def issue_codes(result: dict[str, object]) -> list[str]:
    return [str(item["code"]) for item in result["issues"]]  # type: ignore[index]


def evaluate(
    declared: dict[str, object] | None = None,
    measured: dict[str, object] | None = None,
    current: dict[str, object] | None = None,
    **kwargs,
) -> dict[str, object]:
    return evaluate_runtime_budget(
        workload() if declared is None else declared,
        calibration() if measured is None else measured,
        binding() if current is None else current,
        now=NOW,
        disk_usage_provider=disk(),
        **kwargs,
    )


def test_ready_decision_exposes_stable_ui_fields_and_conservative_estimate() -> None:
    declared = workload()
    measured = calibration()
    current = binding()
    before = copy.deepcopy((declared, measured, current))

    result = evaluate(declared, measured, current)

    assert result["status"] == "READY"
    assert result["should_start"] is True
    assert result["code"] == RUNTIME_BUDGET_READY
    assert result["issues"] == []
    assert "预算校验通过" in result["summary_zh"]
    assert result["estimate"]["estimated_p95_seconds"] == 2580  # type: ignore[index]
    assert result["estimate"]["predicted_output_bytes"] == 450 * MIB  # type: ignore[index]
    assert result["estimate"]["deadline_margin_seconds"] == 4020  # type: ignore[index]
    assert result["calibration"]["minimum_sample_count"] == 20  # type: ignore[index]
    assert result["binding"]["environment_fingerprint"] == ENVIRONMENT_FINGERPRINT  # type: ignore[index]
    assert result["read_only"] is True
    assert (declared, measured, current) == before


def test_missing_current_binding_fails_closed_and_launcher_is_not_called() -> None:
    launched: list[bool] = []

    decision, launch_result = launch_if_runtime_budget_allows(
        lambda: launched.append(True),
        workload(),
        calibration(),
        None,
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED
    assert decision["should_start"] is False
    assert launch_result is None
    assert launched == []


def test_p95_over_effective_hard_stop_blocks_and_guard_never_calls_launcher() -> None:
    declared = workload()
    declared["hard_stop_seconds"] = 1800
    launched: list[bool] = []

    decision, launch_result = launch_if_runtime_budget_allows(
        lambda: launched.append(True),
        declared,
        calibration(),
        binding(),
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP
    assert launched == []
    assert launch_result is None


def test_ready_guard_calls_launcher_exactly_once() -> None:
    launched: list[str] = []

    decision, launch_result = launch_if_runtime_budget_allows(
        lambda: launched.append("started") or "pid-1",
        workload(),
        calibration(),
        binding(),
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["should_start"] is True
    assert launched == ["started"]
    assert launch_result == "pid-1"


@pytest.mark.parametrize("duration", [299, 601])
def test_calibration_must_be_between_five_and_ten_minutes(duration: int) -> None:
    measured = calibration()
    measured["duration_seconds"] = duration

    result = evaluate(measured=measured)

    assert result["code"] == RUNTIME_BUDGET_CALIBRATION_WINDOW_INVALID


def test_sample_count_below_twenty_is_rejected() -> None:
    measured = calibration()
    measured["samples"][0]["sample_count"] = 19  # type: ignore[index]

    result = evaluate(measured=measured)

    assert result["code"] == RUNTIME_BUDGET_CALIBRATION_INVALID
    assert "sample_count" in result["issues"][0]["details"]["reason"]  # type: ignore[index]


def test_missing_stale_future_and_wrong_environment_calibrations_fail_closed() -> None:
    missing = evaluate_runtime_budget(workload(), None, binding(), now=NOW, disk_usage_provider=disk())
    assert missing["code"] == RUNTIME_BUDGET_CALIBRATION_REQUIRED

    stale_calibration = calibration()
    stale_calibration["captured_at"] = (NOW - timedelta(days=2)).isoformat()
    assert evaluate(measured=stale_calibration)["code"] == RUNTIME_BUDGET_CALIBRATION_STALE

    future_calibration = calibration()
    future_calibration["captured_at"] = (NOW + timedelta(minutes=6)).isoformat()
    assert evaluate(measured=future_calibration)["code"] == RUNTIME_BUDGET_CALIBRATION_FUTURE

    wrong_environment = calibration()
    wrong_environment["environment_key"] = "different-machine"
    assert evaluate(measured=wrong_environment)["code"] == RUNTIME_BUDGET_CALIBRATION_ENVIRONMENT_MISMATCH


def test_binding_environment_is_verified_against_live_host() -> None:
    current = binding()
    current["environment"]["machine"] = "not-this-machine"  # type: ignore[index]

    result = evaluate(current=current)

    assert result["code"] == RUNTIME_BUDGET_CURRENT_BINDING_INVALID


def test_check_rejects_relative_or_missing_bound_executable_before_ready() -> None:
    relative = binding()
    relative["argv"][0] = "python"  # type: ignore[index]
    assert evaluate(current=relative)["code"] == RUNTIME_BUDGET_CURRENT_BINDING_INVALID

    missing = binding()
    missing["argv"][0] = str(BASE / "definitely-missing-python.exe")  # type: ignore[index]
    assert evaluate(current=missing)["code"] == RUNTIME_BUDGET_CURRENT_BINDING_INVALID


def test_command_and_execution_shapes_bind_the_actual_launch() -> None:
    wrong_command = calibration()
    wrong_command["launch_fingerprint"] = "0" * 64
    assert evaluate(measured=wrong_command)["code"] == RUNTIME_BUDGET_COMMAND_MISMATCH

    wrong_shape = binding()
    wrong_shape["execution_shapes"][0]["execution_shape"] = "workers=8;raw=drop"  # type: ignore[index]
    assert evaluate(current=wrong_shape)["code"] == RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH


def test_missing_profile_and_calibration_execution_shape_mismatch_do_not_extrapolate() -> None:
    missing_profile = calibration()
    missing_profile["samples"] = missing_profile["samples"][:1]  # type: ignore[index]
    assert evaluate(measured=missing_profile)["code"] == RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING

    wrong_shape = calibration()
    wrong_shape["samples"][0]["execution_shape"] = "workers=8;raw=hash-only"  # type: ignore[index]
    assert evaluate(measured=wrong_shape)["code"] == RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH


def test_output_cap_disk_capacity_and_disk_inspection_are_separate_stable_failures() -> None:
    capped = workload()
    capped["maximum_output_bytes"] = 100 * MIB
    assert evaluate(declared=capped)["code"] == RUNTIME_BUDGET_OUTPUT_CAP_EXCEEDED

    low_disk = evaluate_runtime_budget(
        workload(), calibration(), binding(), now=NOW, disk_usage_provider=disk(GIB)
    )
    assert low_disk["code"] == RUNTIME_BUDGET_DISK_INSUFFICIENT
    assert low_disk["estimate"]["disk_margin_bytes"] < 0

    def unavailable(_path: Path) -> object:
        raise OSError("volume unavailable")

    unreadable = evaluate_runtime_budget(
        workload(), calibration(), binding(), now=NOW, disk_usage_provider=unavailable
    )
    assert unreadable["code"] == RUNTIME_BUDGET_DISK_INSPECTION_FAILED


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("hard_stop_seconds"),
        lambda value: value.update(hard_stop_seconds=float("nan")),
        lambda value: value.update(deadline_reserve_seconds=7200),
        lambda value: value.update(phases=[]),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(unexpected_safety_override=True),
    ],
)
def test_malformed_or_non_strict_workload_fails_closed(mutation) -> None:
    declared = workload()
    mutation(declared)

    result = evaluate(declared=declared)

    assert result["code"] == RUNTIME_BUDGET_WORKLOAD_INVALID


@pytest.mark.parametrize(
    "target,mutation",
    [
        ("calibration", lambda value: value.update(schema_version=True)),
        ("calibration", lambda value: value.update(unknown=1)),
        ("binding", lambda value: value.update(schema_version=1.0)),
        ("binding", lambda value: value.update(unknown=1)),
        ("phase", lambda value: value["phases"][0].update(unknown=1)),
        ("sample", lambda value: value["samples"][0].update(unknown=1)),
    ],
)
def test_all_nested_schemas_reject_bool_float_versions_and_unknown_keys(target, mutation) -> None:
    declared, measured, current = workload(), calibration(), binding()
    selected = {"calibration": measured, "binding": current, "phase": declared, "sample": measured}[target]
    mutation(selected)

    result = evaluate(declared, measured, current)

    expected = {
        "calibration": RUNTIME_BUDGET_CALIBRATION_INVALID,
        "sample": RUNTIME_BUDGET_CALIBRATION_INVALID,
        "binding": RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
        "phase": RUNTIME_BUDGET_WORKLOAD_INVALID,
    }[target]
    assert result["code"] == expected


def test_numeric_overflow_and_disk_sum_overflow_return_stable_blocked_decisions() -> None:
    measured = calibration()
    declared = workload()
    declared["phases"][0]["units"] = MAX_SAFE_QUANTITY // 2  # type: ignore[index]
    measured["samples"][0]["p95_seconds_per_unit"] = 3  # type: ignore[index]

    product_overflow = evaluate(declared, measured)
    assert product_overflow["code"] == RUNTIME_BUDGET_ESTIMATE_OVERFLOW

    disk_declared = workload()
    disk_declared["minimum_free_after_bytes"] = MAX_SAFE_QUANTITY
    disk_overflow = evaluate(declared=disk_declared)
    assert disk_overflow["code"] == RUNTIME_BUDGET_ESTIMATE_OVERFLOW

    huge = workload()
    huge["hard_stop_seconds"] = 1e308
    invalid = evaluate(declared=huge)
    assert invalid["code"] == RUNTIME_BUDGET_WORKLOAD_INVALID

    tiny_declared = workload()
    tiny_measured = calibration()
    tiny_declared["phases"][0]["units"] = 1e-300  # type: ignore[index]
    tiny_measured["samples"][0]["p95_seconds_per_unit"] = 1e-300  # type: ignore[index]
    underflow = evaluate(tiny_declared, tiny_measured)
    assert underflow["code"] == RUNTIME_BUDGET_ESTIMATE_OVERFLOW


def _write_contract_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    declared = workload(output_path="results/formal")
    current = binding(cwd=".")
    declared["launch_fingerprint"] = launch_fingerprint(ARGV, tmp_path)
    measured = calibration()
    measured["launch_fingerprint"] = declared["launch_fingerprint"]
    workload_path = tmp_path / "workload.json"
    calibration_path = tmp_path / "calibration.json"
    binding_path = tmp_path / "binding.json"
    workload_path.write_text(json.dumps(declared), encoding="utf-8")
    calibration_path.write_text(json.dumps(measured), encoding="utf-8")
    binding_path.write_text(json.dumps(current), encoding="utf-8")
    return workload_path, calibration_path, binding_path


def test_file_contract_resolves_relative_output_and_cwd_against_workload_directory(tmp_path: Path) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)
    received: list[Path] = []

    result = evaluate_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        now=NOW,
        disk_usage_provider=lambda path: received.append(path) or SimpleNamespace(total=100 * GIB, free=10 * GIB),
    )

    assert result["code"] == RUNTIME_BUDGET_READY
    assert received == [(tmp_path / "results" / "formal").absolute()]
    assert result["estimate"]["output_path"] == str((tmp_path / "results" / "formal").absolute())


def test_file_contract_rejects_duplicate_json_keys_missing_binding_and_symlinks(tmp_path: Path) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)

    missing_binding = evaluate_runtime_budget_files(
        workload_path, calibration_path, None, now=NOW, disk_usage_provider=disk()
    )
    assert missing_binding["code"] == RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED

    calibration_path.write_text(
        json.dumps(calibration())[:-1] + ',"duration_seconds":300}',
        encoding="utf-8",
    )
    duplicate = evaluate_runtime_budget_files(
        workload_path, calibration_path, binding_path, now=NOW, disk_usage_provider=disk()
    )
    assert duplicate["code"] == RUNTIME_BUDGET_CALIBRATION_INVALID

    target = tmp_path / "real-workload.json"
    target.write_text(json.dumps(workload()), encoding="utf-8")
    link = tmp_path / "linked-workload.json"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    linked = evaluate_runtime_budget_files(link, calibration_path, binding_path)
    assert linked["code"] == RUNTIME_BUDGET_WORKLOAD_INVALID


def test_file_contract_rejects_reparse_parent_directory(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    workload_path, calibration_path, binding_path = _write_contract_files(real_dir)
    linked_dir = tmp_path / "linked"
    try:
        os.symlink(real_dir, linked_dir, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    result = evaluate_runtime_budget_files(
        linked_dir / workload_path.name,
        calibration_path,
        binding_path,
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert result["code"] == RUNTIME_BUDGET_WORKLOAD_INVALID


def test_invalid_calibration_numbers_fail_closed_with_stable_code() -> None:
    measured = calibration()
    measured["samples"][0]["p95_seconds_per_unit"] = float("inf")  # type: ignore[index]

    result = evaluate(measured=measured)

    assert result["code"] == RUNTIME_BUDGET_CALIBRATION_INVALID
    assert RUNTIME_BUDGET_CALIBRATION_INVALID in issue_codes(result)


def test_gated_file_launcher_uses_exact_bound_command_once_and_never_rereads(tmp_path: Path) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    decision, completed = launch_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        runner=runner,
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_READY
    assert completed.returncode == 0
    assert completed.started is True
    assert completed.tree_guarded is False
    assert calls == [
        (
            ARGV,
            {
                "cwd": str(tmp_path.absolute()),
                "shell": False,
                "check": False,
                "timeout": 7200,
            },
        )
    ]


def test_default_production_launcher_requires_job_object_tree_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)
    calls: list[tuple[list[str], Path, int]] = []

    def job_runner(argv, *, cwd, timeout_seconds):
        calls.append((argv, Path(cwd), timeout_seconds))
        return {
            "started": True,
            "returncode": 0,
            "timed_out": False,
            "tree_guarded": True,
            "tree_terminated_verified": True,
            "error_code": "",
            "error_type": "",
        }

    monkeypatch.setattr(gate_module, "run_in_kill_on_close_job", job_runner)

    decision, completed = launch_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_READY
    assert completed is not None
    assert completed.tree_guarded is True
    assert completed.tree_terminated_verified is True
    assert calls == [(ARGV, tmp_path.absolute(), 7200)]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_job_object_normal_exit_is_guarded_and_verified(tmp_path: Path) -> None:
    outcome = run_in_kill_on_close_job(
        [str(Path(sys.executable).absolute()), "-c", "pass"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert outcome == {
        "started": True,
        "returncode": 0,
        "timed_out": False,
        "tree_guarded": True,
        "tree_terminated_verified": True,
        "error_code": "",
        "error_type": "",
    }


def test_job_object_runner_fails_before_start_for_unbound_executable(tmp_path: Path) -> None:
    outcome = run_in_kill_on_close_job(
        ["python", "-c", "pass"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert outcome["started"] is False
    assert outcome["tree_guarded"] is False
    assert outcome["error_code"] == "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_job_object_timeout_kills_and_verifies_spawned_child_tree(tmp_path: Path) -> None:
    child_code = "import time;time.sleep(60)"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "pathlib.Path('child.pid').write_text(str(p.pid),encoding='ascii');"
        "time.sleep(60)"
    )
    argv = [str(Path(sys.executable).absolute()), "-c", parent_code]
    launch_sha = launch_fingerprint(argv, tmp_path)
    declared = workload(output_path="results/formal")
    declared.update(
        hard_stop_seconds=1,
        deadline_reserve_seconds=0,
        fixed_overhead_p95_seconds=0,
        launch_fingerprint=launch_sha,
    )
    declared["phases"] = [
        {
            "phase_id": "correctness",
            "calibration_key": "correctness-medium",
            "execution_shape": "workers=2;raw=retain;compression=gzip",
            "units": 1,
        }
    ]
    measured = calibration()
    measured["launch_fingerprint"] = launch_sha
    measured["samples"] = [
        {
            "calibration_key": "correctness-medium",
            "execution_shape": "workers=2;raw=retain;compression=gzip",
            "sample_count": CALIBRATION_MIN_SAMPLE_COUNT,
            "p95_seconds_per_unit": 0.1,
            "p95_output_bytes_per_unit": 0,
        }
    ]
    current = binding(cwd=".")
    current["argv"] = argv
    current["execution_shapes"] = current["execution_shapes"][:1]  # type: ignore[index]
    workload_path = tmp_path / "workload.json"
    calibration_path = tmp_path / "calibration.json"
    binding_path = tmp_path / "binding.json"
    workload_path.write_text(json.dumps(declared), encoding="utf-8")
    calibration_path.write_text(json.dumps(measured), encoding="utf-8")
    binding_path.write_text(json.dumps(current), encoding="utf-8")

    decision, completed = launch_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_READY
    assert completed is not None
    assert completed.timed_out is True
    assert completed.tree_guarded is True
    assert completed.tree_terminated_verified is True
    child_pid = int((tmp_path / "child.pid").read_text(encoding="ascii"))
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(synchronize, False, child_pid)
    if process:
        try:
            assert kernel32.WaitForSingleObject(process, 5000) == 0
        finally:
            kernel32.CloseHandle(process)


def test_gated_file_launcher_never_calls_runner_when_binding_is_wrong(tmp_path: Path) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)
    current = json.loads(binding_path.read_text(encoding="utf-8"))
    current["argv"].append("--different")
    binding_path.write_text(json.dumps(current), encoding="utf-8")

    decision, completed = launch_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        runner=lambda *args, **kwargs: pytest.fail("blocked launch must not run"),
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_COMMAND_MISMATCH
    assert completed is None


def test_gated_file_launcher_reports_timeout_without_an_unhandled_exception(tmp_path: Path) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)

    def timeout_runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    decision, completed = launch_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        runner=timeout_runner,
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_READY
    assert completed is not None
    assert completed.started is True
    assert completed.timed_out is True
    assert completed.error_code == "RUNTIME_BUDGET_LAUNCH_TIMEOUT"


def test_gated_file_launcher_rejects_non_formal_kind(tmp_path: Path) -> None:
    workload_path, calibration_path, binding_path = _write_contract_files(tmp_path)
    declared = json.loads(workload_path.read_text(encoding="utf-8"))
    declared["kind"] = "small_test"
    workload_path.write_text(json.dumps(declared), encoding="utf-8")

    decision, completed = launch_runtime_budget_files(
        workload_path,
        calibration_path,
        binding_path,
        runner=lambda *args, **kwargs: pytest.fail("small tests do not use the formal launcher"),
        now=NOW,
        disk_usage_provider=disk(),
    )

    assert decision["code"] == RUNTIME_BUDGET_WORKLOAD_INVALID
    assert completed is None


def test_cli_help_lists_check_and_explicit_gated_launch() -> None:
    help_text = cli_module.build_parser().format_help()

    assert "runtime-budget-check" in help_text
    assert "runtime-budget-launch" in help_text
    assert "Read-only preflight" in help_text


@pytest.mark.parametrize(("should_start", "expected_code"), [(True, 0), (False, 2)])
def test_cli_runtime_budget_check_emits_json_and_stable_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    should_start: bool,
    expected_code: int,
) -> None:
    workload_path = tmp_path / "workload.json"
    calibration_path = tmp_path / "calibration.json"
    binding_path = tmp_path / "binding.json"
    expected = {
        "status": "READY" if should_start else "BLOCKED",
        "should_start": should_start,
        "code": RUNTIME_BUDGET_READY if should_start else RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP,
        "summary_zh": "预算校验结果",
    }
    calls: list[tuple[Path, Path, Path]] = []

    def fake_check(received_workload: Path, received_calibration: Path, received_binding: Path) -> dict[str, object]:
        calls.append((received_workload, received_calibration, received_binding))
        return expected

    monkeypatch.setattr(cli_module, "evaluate_runtime_budget_files", fake_check)
    monkeypatch.setattr(
        cli_module,
        "UnifiedOrchestrator",
        lambda: pytest.fail("read-only budget check must not initialise the orchestrator"),
    )

    exit_code = cli_module.main(
        ["runtime-budget-check", str(workload_path), str(calibration_path), str(binding_path)]
    )

    assert exit_code == expected_code
    assert calls == [(workload_path, calibration_path, binding_path)]
    assert json.loads(capsys.readouterr().out) == expected


def test_cli_runtime_budget_launch_is_an_explicit_production_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workload_path = tmp_path / "workload.json"
    calibration_path = tmp_path / "calibration.json"
    binding_path = tmp_path / "binding.json"
    decision = {"status": "READY", "should_start": True, "code": RUNTIME_BUDGET_READY}
    calls: list[tuple[Path, Path, Path]] = []

    def fake_launch(received_workload: Path, received_calibration: Path, received_binding: Path):
        calls.append((received_workload, received_calibration, received_binding))
        return decision, RuntimeLaunchOutcome(
            started=True,
            returncode=7,
            tree_guarded=True,
            tree_terminated_verified=True,
        )

    monkeypatch.setattr(cli_module, "launch_runtime_budget_files", fake_launch)
    monkeypatch.setattr(
        cli_module,
        "UnifiedOrchestrator",
        lambda: pytest.fail("gated launch must not initialise the orchestrator"),
    )

    exit_code = cli_module.main(
        ["runtime-budget-launch", str(workload_path), str(calibration_path), str(binding_path)]
    )

    assert exit_code == 1
    assert calls == [(workload_path, calibration_path, binding_path)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == decision
    assert payload["launch"] == {
        "started": True,
        "returncode": 7,
        "timed_out": False,
        "tree_guarded": True,
        "tree_terminated_verified": True,
        "error_code": "",
        "error_type": "",
    }
