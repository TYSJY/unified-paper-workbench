"""Fail-closed runtime and disk budget gate for expensive workloads.

The module has two deliberately separate inputs:

* workload/calibration documents describe the planned and measured work; and
* a current binding describes the *actual* host, command and execution shape.

No READY decision is possible without that third binding.  The production CLI
launch path also executes the exact argv/cwd stored in the binding, so a check
cannot silently authorize a different command.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .windows_process_tree import run_in_kill_on_close_job


DECISION_SCHEMA_VERSION = 1
DOCUMENT_SCHEMA_VERSION = 1
CALIBRATION_MIN_SECONDS = 5 * 60
CALIBRATION_MAX_SECONDS = 10 * 60
CALIBRATION_MIN_SAMPLE_COUNT = 20
CALIBRATION_FUTURE_TOLERANCE_SECONDS = 5 * 60
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_SAFE_QUANTITY = 2**63 - 1
MAX_HARD_STOP_SECONDS = 30 * 24 * 60 * 60
MAX_PHASES = 256
MAX_COMMAND_ARGUMENTS = 512
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

RUNTIME_BUDGET_READY = "RUNTIME_BUDGET_READY"
RUNTIME_BUDGET_WORKLOAD_INVALID = "RUNTIME_BUDGET_WORKLOAD_INVALID"
RUNTIME_BUDGET_CALIBRATION_REQUIRED = "RUNTIME_BUDGET_CALIBRATION_REQUIRED"
RUNTIME_BUDGET_CALIBRATION_INVALID = "RUNTIME_BUDGET_CALIBRATION_INVALID"
RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED = "RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED"
RUNTIME_BUDGET_CURRENT_BINDING_INVALID = "RUNTIME_BUDGET_CURRENT_BINDING_INVALID"
RUNTIME_BUDGET_CALIBRATION_WINDOW_INVALID = "RUNTIME_BUDGET_CALIBRATION_WINDOW_INVALID"
RUNTIME_BUDGET_CALIBRATION_FUTURE = "RUNTIME_BUDGET_CALIBRATION_FUTURE"
RUNTIME_BUDGET_CALIBRATION_STALE = "RUNTIME_BUDGET_CALIBRATION_STALE"
RUNTIME_BUDGET_CALIBRATION_ENVIRONMENT_MISMATCH = "RUNTIME_BUDGET_CALIBRATION_ENVIRONMENT_MISMATCH"
RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING = "RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING"
RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH = "RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH"
RUNTIME_BUDGET_COMMAND_MISMATCH = "RUNTIME_BUDGET_COMMAND_MISMATCH"
RUNTIME_BUDGET_ESTIMATE_OVERFLOW = "RUNTIME_BUDGET_ESTIMATE_OVERFLOW"
RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP = "RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP"
RUNTIME_BUDGET_OUTPUT_CAP_EXCEEDED = "RUNTIME_BUDGET_OUTPUT_CAP_EXCEEDED"
RUNTIME_BUDGET_DISK_INSPECTION_FAILED = "RUNTIME_BUDGET_DISK_INSPECTION_FAILED"
RUNTIME_BUDGET_DISK_INSUFFICIENT = "RUNTIME_BUDGET_DISK_INSUFFICIENT"
RUNTIME_BUDGET_LAUNCH_TIMEOUT = "RUNTIME_BUDGET_LAUNCH_TIMEOUT"
RUNTIME_BUDGET_LAUNCH_FAILED = "RUNTIME_BUDGET_LAUNCH_FAILED"
RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE = "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE"
RUNTIME_BUDGET_PROCESS_TREE_TERMINATION_UNVERIFIED = (
    "RUNTIME_BUDGET_PROCESS_TREE_TERMINATION_UNVERIFIED"
)

_T = TypeVar("_T")

_WORKLOAD_KEYS = {
    "schema_version",
    "workload_id",
    "kind",
    "environment_key",
    "environment_fingerprint",
    "launch_fingerprint",
    "output_path",
    "hard_stop_seconds",
    "deadline_reserve_seconds",
    "fixed_overhead_p95_seconds",
    "minimum_free_after_bytes",
    "maximum_output_bytes",
    "calibration_max_age_seconds",
    "phases",
}
_WORKLOAD_PHASE_KEYS = {"phase_id", "calibration_key", "execution_shape", "units"}
_CALIBRATION_KEYS = {
    "schema_version",
    "workload_id",
    "environment_key",
    "environment_fingerprint",
    "launch_fingerprint",
    "captured_at",
    "duration_seconds",
    "samples",
}
_CALIBRATION_SAMPLE_KEYS = {
    "calibration_key",
    "execution_shape",
    "sample_count",
    "p95_seconds_per_unit",
    "p95_output_bytes_per_unit",
}
_BINDING_KEYS = {
    "schema_version",
    "environment_key",
    "environment",
    "execution_shapes",
    "argv",
    "cwd",
}
_ENVIRONMENT_KEYS = {
    "system",
    "release",
    "machine",
    "python_implementation",
    "python_version",
    "logical_cpu_count",
}
_BINDING_SHAPE_KEYS = {"phase_id", "execution_shape"}


class _DocumentError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeLaunchOutcome:
    started: bool
    returncode: int | None
    timed_out: bool = False
    tree_guarded: bool = False
    tree_terminated_verified: bool = False
    error_code: str = ""
    error_type: str = ""


def _issue(code: str, summary_zh: str, **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "summary_zh": summary_zh}
    if details:
        item["details"] = details
    return item


def _decision(
    *,
    issues: Sequence[dict[str, Any]],
    estimate: Mapping[str, Any] | None = None,
    calibration: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    blocked = bool(issues)
    if blocked:
        primary = issues[0]
        summary = str(primary["summary_zh"])
        if len(issues) > 1:
            summary += f"（另有 {len(issues) - 1} 项预算问题）"
        code = str(primary["code"])
    else:
        code = RUNTIME_BUDGET_READY
        p95 = int((estimate or {}).get("estimated_p95_seconds", 0))
        output = int((estimate or {}).get("predicted_output_bytes", 0))
        summary = f"运行前预算校验通过：预计 p95 {_format_duration_zh(p95)}，新增输出约 {_format_bytes_zh(output)}。"
    return {
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "status": "BLOCKED" if blocked else "READY",
        "ok": not blocked,
        "should_start": not blocked,
        "code": code,
        "summary_zh": summary,
        "issues": list(issues),
        "estimate": dict(estimate or {}),
        "calibration": dict(calibration or {}),
        "binding": dict(binding or {}),
        "read_only": True,
    }


def _format_duration_zh(seconds: int) -> str:
    safe = min(MAX_SAFE_QUANTITY, max(0, int(seconds)))
    hours, remainder = divmod(safe, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _format_bytes_zh(value: int) -> str:
    safe = min(MAX_SAFE_QUANTITY, max(0, int(value)))
    if safe >= 1024**3:
        return f"{safe / 1024**3:.2f} GB"
    if safe >= 1024**2:
        return f"{safe / 1024**2:.1f} MB"
    if safe >= 1024:
        return f"{safe / 1024:.1f} KB"
    return f"{safe} B"


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _DocumentError(f"{field} must be an object")
    return value


def _strict_object(value: Any, field: str, expected_keys: set[str]) -> Mapping[str, Any]:
    source = _mapping(value, field)
    actual_keys = set(source)
    missing = sorted(expected_keys - actual_keys)
    unknown = sorted(actual_keys - expected_keys)
    if missing:
        raise _DocumentError(f"{field} missing required keys: {', '.join(missing)}")
    if unknown:
        raise _DocumentError(f"{field} has unknown keys: {', '.join(unknown)}")
    return source


def _schema_version(source: Mapping[str, Any], field: str) -> None:
    value = source["schema_version"]
    if isinstance(value, bool) or type(value) is not int or value != DOCUMENT_SCHEMA_VERSION:
        raise _DocumentError(f"{field}.schema_version must be the integer 1")


def _plain_identifier(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise _DocumentError(f"{field} must be a string")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > maximum
        or "\x00" in cleaned
        or any(ord(character) < 32 for character in cleaned)
    ):
        raise _DocumentError(f"{field} is invalid")
    return cleaned


def _fingerprint(value: Any, field: str) -> str:
    text = _plain_identifier(value, field, maximum=64).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise _DocumentError(f"{field} must be a lowercase SHA-256 hex digest")
    return text


def _number(
    value: Any,
    field: str,
    *,
    minimum: float = 0,
    maximum: float = MAX_SAFE_QUANTITY,
    strictly_positive: bool = False,
    integer: bool = False,
) -> float | int:
    if isinstance(value, bool):
        raise _DocumentError(f"{field} must be numeric")
    if integer:
        if type(value) is not int:
            raise _DocumentError(f"{field} must be an integer")
        numeric_int = int(value)
        if strictly_positive and numeric_int <= 0:
            raise _DocumentError(f"{field} must be positive")
        if not strictly_positive and numeric_int < minimum:
            raise _DocumentError(f"{field} is below its minimum")
        if numeric_int > maximum:
            raise _DocumentError(f"{field} exceeds its maximum")
        return numeric_int
    if not isinstance(value, (int, float)):
        raise _DocumentError(f"{field} must be numeric")
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _DocumentError(f"{field} must be finite") from exc
    if not math.isfinite(numeric):
        raise _DocumentError(f"{field} must be finite")
    if strictly_positive and numeric <= 0:
        raise _DocumentError(f"{field} must be positive")
    if not strictly_positive and numeric < minimum:
        raise _DocumentError(f"{field} is below its minimum")
    if numeric > maximum:
        raise _DocumentError(f"{field} exceeds its maximum")
    return numeric


def _object_list(value: Any, field: str, *, maximum: int = MAX_PHASES) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _DocumentError(f"{field} must be an array")
    if not value:
        raise _DocumentError(f"{field} must not be empty")
    if len(value) > maximum:
        raise _DocumentError(f"{field} has too many items")
    return [_mapping(item, f"{field}[]") for item in value]


def _string_list(value: Any, field: str) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _DocumentError(f"{field} must be an array")
    if not value or len(value) > MAX_COMMAND_ARGUMENTS:
        raise _DocumentError(f"{field} must contain 1-{MAX_COMMAND_ARGUMENTS} arguments")
    result: list[str] = []
    for index, argument in enumerate(value):
        if not isinstance(argument, str) or len(argument) > 8192 or "\x00" in argument:
            raise _DocumentError(f"{field}[{index}] is invalid")
        result.append(argument)
    if not result[0].strip():
        raise _DocumentError(f"{field}[0] must name an executable")
    return result


def _parse_timestamp(value: Any) -> datetime:
    text = _plain_identifier(value, "calibration.captured_at", maximum=64)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (OverflowError, ValueError) as exc:
        raise _DocumentError("calibration.captured_at must be RFC 3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _DocumentError("calibration.captured_at must include a timezone")
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise _DocumentError("calibration.captured_at is outside the supported range") from exc


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_current_environment() -> dict[str, Any]:
    """Capture non-secret host facts that can be independently verified now."""

    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "logical_cpu_count": int(os.cpu_count() or 0),
    }


def environment_fingerprint(environment: Mapping[str, Any] | None = None) -> str:
    source = capture_current_environment() if environment is None else _validate_environment(environment)
    return _canonical_sha256(source)


def launch_fingerprint(argv: Sequence[str], cwd: str | Path) -> str:
    return _canonical_sha256({"argv": list(argv), "cwd": str(Path(cwd).absolute())})


def _resolve_from_base(raw: str, base_dir: Path | None, field: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if base_dir is None:
            raise _DocumentError(f"{field} is relative but no workload base directory was supplied")
        path = base_dir / path
    try:
        return path.absolute()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _DocumentError(f"{field} cannot be resolved") from exc


def _validate_environment(value: Any) -> dict[str, Any]:
    source = _strict_object(value, "binding.environment", _ENVIRONMENT_KEYS)
    return {
        "system": _plain_identifier(source["system"], "binding.environment.system"),
        "release": _plain_identifier(source["release"], "binding.environment.release"),
        "machine": _plain_identifier(source["machine"], "binding.environment.machine"),
        "python_implementation": _plain_identifier(
            source["python_implementation"], "binding.environment.python_implementation"
        ),
        "python_version": _plain_identifier(source["python_version"], "binding.environment.python_version"),
        "logical_cpu_count": int(
            _number(
                source["logical_cpu_count"],
                "binding.environment.logical_cpu_count",
                minimum=0,
                maximum=65536,
                integer=True,
            )
        ),
    }


def _validate_workload(document: Any, *, base_dir: Path | None) -> dict[str, Any]:
    source = _strict_object(document, "workload", _WORKLOAD_KEYS)
    _schema_version(source, "workload")

    hard_stop = float(
        _number(
            source["hard_stop_seconds"],
            "workload.hard_stop_seconds",
            strictly_positive=True,
            maximum=MAX_HARD_STOP_SECONDS,
        )
    )
    reserve = float(
        _number(source["deadline_reserve_seconds"], "workload.deadline_reserve_seconds", minimum=0)
    )
    if reserve >= hard_stop:
        raise _DocumentError("workload.deadline_reserve_seconds must be below hard_stop_seconds")

    phases: list[dict[str, Any]] = []
    phase_ids: set[str] = set()
    for index, raw_phase in enumerate(_object_list(source["phases"], "workload.phases")):
        prefix = f"workload.phases[{index}]"
        phase = _strict_object(raw_phase, prefix, _WORKLOAD_PHASE_KEYS)
        phase_id = _plain_identifier(phase["phase_id"], f"{prefix}.phase_id")
        if phase_id in phase_ids:
            raise _DocumentError("workload phase_id values must be unique")
        phase_ids.add(phase_id)
        phases.append(
            {
                "phase_id": phase_id,
                "calibration_key": _plain_identifier(phase["calibration_key"], f"{prefix}.calibration_key"),
                "execution_shape": _plain_identifier(phase["execution_shape"], f"{prefix}.execution_shape"),
                "units": float(_number(phase["units"], f"{prefix}.units", strictly_positive=True)),
            }
        )

    output_path_raw = _plain_identifier(source["output_path"], "workload.output_path", maximum=4096)
    output_path = _resolve_from_base(output_path_raw, base_dir, "workload.output_path")
    return {
        "workload_id": _plain_identifier(source["workload_id"], "workload.workload_id"),
        "kind": _plain_identifier(source["kind"], "workload.kind"),
        "environment_key": _plain_identifier(source["environment_key"], "workload.environment_key"),
        "environment_fingerprint": _fingerprint(
            source["environment_fingerprint"], "workload.environment_fingerprint"
        ),
        "launch_fingerprint": _fingerprint(source["launch_fingerprint"], "workload.launch_fingerprint"),
        "output_path": output_path,
        "hard_stop_seconds": hard_stop,
        "deadline_reserve_seconds": reserve,
        "fixed_overhead_p95_seconds": float(
            _number(
                source["fixed_overhead_p95_seconds"],
                "workload.fixed_overhead_p95_seconds",
                minimum=0,
            )
        ),
        "minimum_free_after_bytes": int(
            _number(
                source["minimum_free_after_bytes"],
                "workload.minimum_free_after_bytes",
                minimum=0,
                integer=True,
            )
        ),
        "maximum_output_bytes": int(
            _number(
                source["maximum_output_bytes"],
                "workload.maximum_output_bytes",
                minimum=0,
                integer=True,
            )
        ),
        "calibration_max_age_seconds": int(
            _number(
                source["calibration_max_age_seconds"],
                "workload.calibration_max_age_seconds",
                strictly_positive=True,
                integer=True,
            )
        ),
        "phases": phases,
    }


def _validate_calibration(document: Any) -> dict[str, Any]:
    source = _strict_object(document, "calibration", _CALIBRATION_KEYS)
    _schema_version(source, "calibration")

    samples: dict[tuple[str, str], dict[str, Any]] = {}
    keys: set[str] = set()
    for index, raw_sample in enumerate(_object_list(source["samples"], "calibration.samples")):
        prefix = f"calibration.samples[{index}]"
        sample = _strict_object(raw_sample, prefix, _CALIBRATION_SAMPLE_KEYS)
        calibration_key = _plain_identifier(sample["calibration_key"], f"{prefix}.calibration_key")
        execution_shape = _plain_identifier(sample["execution_shape"], f"{prefix}.execution_shape")
        identity = (calibration_key, execution_shape)
        if identity in samples:
            raise _DocumentError("calibration sample identities must be unique")
        keys.add(calibration_key)
        samples[identity] = {
            "sample_count": int(
                _number(
                    sample["sample_count"],
                    f"{prefix}.sample_count",
                    minimum=CALIBRATION_MIN_SAMPLE_COUNT,
                    integer=True,
                )
            ),
            "p95_seconds_per_unit": float(
                _number(sample["p95_seconds_per_unit"], f"{prefix}.p95_seconds_per_unit", strictly_positive=True)
            ),
            "p95_output_bytes_per_unit": float(
                _number(sample["p95_output_bytes_per_unit"], f"{prefix}.p95_output_bytes_per_unit", minimum=0)
            ),
        }
    return {
        "workload_id": _plain_identifier(source["workload_id"], "calibration.workload_id"),
        "environment_key": _plain_identifier(source["environment_key"], "calibration.environment_key"),
        "environment_fingerprint": _fingerprint(
            source["environment_fingerprint"], "calibration.environment_fingerprint"
        ),
        "launch_fingerprint": _fingerprint(source["launch_fingerprint"], "calibration.launch_fingerprint"),
        "captured_at": _parse_timestamp(source["captured_at"]),
        "duration_seconds": float(
            _number(source["duration_seconds"], "calibration.duration_seconds", strictly_positive=True)
        ),
        "samples": samples,
        "sample_keys": keys,
    }


def _validate_binding(document: Any, *, base_dir: Path | None) -> dict[str, Any]:
    source = _strict_object(document, "binding", _BINDING_KEYS)
    _schema_version(source, "binding")
    environment = _validate_environment(source["environment"])

    shapes: dict[str, str] = {}
    for index, raw_shape in enumerate(_object_list(source["execution_shapes"], "binding.execution_shapes")):
        prefix = f"binding.execution_shapes[{index}]"
        shape = _strict_object(raw_shape, prefix, _BINDING_SHAPE_KEYS)
        phase_id = _plain_identifier(shape["phase_id"], f"{prefix}.phase_id")
        if phase_id in shapes:
            raise _DocumentError("binding execution phase_id values must be unique")
        shapes[phase_id] = _plain_identifier(shape["execution_shape"], f"{prefix}.execution_shape")

    argv = _string_list(source["argv"], "binding.argv")
    executable = Path(argv[0]).expanduser()
    if not executable.is_absolute():
        raise _DocumentError("binding.argv[0] must be an absolute executable path")
    try:
        executable = _reject_reparse_path_components(executable)
    except OSError as exc:
        raise _DocumentError("binding.argv[0] cannot be inspected") from exc
    if not executable.is_file():
        raise _DocumentError("binding.argv[0] must be an existing executable file")
    argv[0] = str(executable)
    cwd_raw = _plain_identifier(source["cwd"], "binding.cwd", maximum=4096)
    cwd = _resolve_from_base(cwd_raw, base_dir, "binding.cwd")
    return {
        "environment_key": _plain_identifier(source["environment_key"], "binding.environment_key"),
        "environment": environment,
        "environment_fingerprint": environment_fingerprint(environment),
        "execution_shapes": shapes,
        "argv": argv,
        "cwd": cwd,
        "launch_fingerprint": launch_fingerprint(argv, cwd),
    }


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise OSError("no existing ancestor for output path")
    return candidate


def _default_disk_usage(path: Path) -> Any:
    return shutil.disk_usage(_nearest_existing_path(path))


def _disk_values(usage: Any) -> tuple[int, int]:
    if isinstance(usage, Mapping):
        total_raw, free_raw = usage.get("total"), usage.get("free")
    else:
        total_raw, free_raw = getattr(usage, "total", None), getattr(usage, "free", None)
    total = int(_number(total_raw, "disk.total", minimum=0, integer=True))
    free = int(_number(free_raw, "disk.free", minimum=0, integer=True))
    if free > total:
        raise _DocumentError("disk.free cannot exceed disk.total")
    return total, free


def _safe_product(left: float, right: float) -> float:
    value = left * right
    if left > 0 and right > 0 and value == 0:
        raise OverflowError("budget estimate multiplication underflow")
    if value >= 2**53:
        value = math.nextafter(value, math.inf)
    if not math.isfinite(value) or value > MAX_SAFE_QUANTITY:
        raise OverflowError("budget estimate multiplication overflow")
    return value


def _safe_sum(left: float, right: float) -> float:
    value = left + right
    if value >= 2**53 and right > 0:
        value = math.nextafter(value, math.inf)
    if not math.isfinite(value) or value > MAX_SAFE_QUANTITY:
        raise OverflowError("budget estimate addition overflow")
    return value


def evaluate_runtime_budget(
    workload: Mapping[str, Any] | None,
    calibration: Mapping[str, Any] | None,
    current_binding: Mapping[str, Any] | None = None,
    *,
    workload_base_dir: Path | None = None,
    now: datetime | None = None,
    disk_usage_provider: Callable[[Path], Any] | None = None,
    current_environment_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a read-only, fail-closed decision; never infer a current binding."""

    try:
        base_dir = Path(workload_base_dir).absolute() if workload_base_dir is not None else None
        normalized_workload = _validate_workload(workload, base_dir=base_dir)
    except Exception as exc:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_WORKLOAD_INVALID,
                    "工作量预算声明无效，任务未启动。",
                    reason=str(exc),
                )
            ]
        )

    if calibration is None:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_CALIBRATION_REQUIRED,
                    "缺少最近的 5–10 分钟校准记录，任务未启动。",
                )
            ],
            estimate={
                "hard_stop_seconds": math.ceil(normalized_workload["hard_stop_seconds"]),
                "deadline_reserve_seconds": math.ceil(normalized_workload["deadline_reserve_seconds"]),
            },
        )

    try:
        normalized_calibration = _validate_calibration(calibration)
    except Exception as exc:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_CALIBRATION_INVALID,
                    "校准记录无效，任务未启动。",
                    reason=str(exc),
                )
            ]
        )

    if current_binding is None:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED,
                    "缺少当前主机、命令与执行形态绑定，任务未启动。",
                )
            ]
        )

    try:
        binding = _validate_binding(current_binding, base_dir=base_dir)
        actual_environment = _validate_environment(
            (current_environment_provider or capture_current_environment)()
        )
    except Exception as exc:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
                    "当前运行绑定无效，任务未启动。",
                    reason=str(exc),
                )
            ]
        )

    binding_info = {
        "environment_key": binding["environment_key"],
        "environment_fingerprint": binding["environment_fingerprint"],
        "launch_fingerprint": binding["launch_fingerprint"],
        "execution_shape_count": len(binding["execution_shapes"]),
    }
    issues: list[dict[str, Any]] = []

    actual_environment_fingerprint = environment_fingerprint(actual_environment)
    if binding["environment"] != actual_environment:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
                "绑定中的主机环境与当前可验证环境不一致，任务未启动。",
                actual_environment_fingerprint=actual_environment_fingerprint,
            )
        )

    current = now or datetime.now(timezone.utc)
    try:
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise _DocumentError("now must include a timezone")
        current = current.astimezone(timezone.utc)
        age_seconds = (current - normalized_calibration["captured_at"]).total_seconds()
        if not math.isfinite(age_seconds):
            raise OverflowError("calibration age is not finite")
    except Exception as exc:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_CALIBRATION_INVALID,
                    "预算校验时间无效，任务未启动。",
                    reason=str(exc),
                )
            ],
            binding=binding_info,
        )

    calibration_info = {
        "captured_at": normalized_calibration["captured_at"].isoformat().replace("+00:00", "Z"),
        "age_seconds": max(0, math.floor(age_seconds)),
        "duration_seconds": math.ceil(normalized_calibration["duration_seconds"]),
        "minimum_duration_seconds": CALIBRATION_MIN_SECONDS,
        "maximum_duration_seconds": CALIBRATION_MAX_SECONDS,
        "minimum_sample_count": CALIBRATION_MIN_SAMPLE_COUNT,
        "maximum_age_seconds": normalized_workload["calibration_max_age_seconds"],
    }

    if normalized_calibration["workload_id"] != normalized_workload["workload_id"]:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CALIBRATION_INVALID,
                "校准记录与当前工作量不匹配，任务未启动。",
            )
        )
    duration = normalized_calibration["duration_seconds"]
    if duration < CALIBRATION_MIN_SECONDS or duration > CALIBRATION_MAX_SECONDS:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CALIBRATION_WINDOW_INVALID,
                "校准时长必须在 5–10 分钟内，任务未启动。",
                duration_seconds=math.ceil(duration),
                minimum_seconds=CALIBRATION_MIN_SECONDS,
                maximum_seconds=CALIBRATION_MAX_SECONDS,
            )
        )
    if age_seconds < -CALIBRATION_FUTURE_TOLERANCE_SECONDS:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CALIBRATION_FUTURE,
                "校准时间晚于当前时间，任务未启动。",
                future_offset_seconds=math.ceil(-age_seconds),
            )
        )
    elif age_seconds > normalized_workload["calibration_max_age_seconds"]:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CALIBRATION_STALE,
                "校准记录已过期，请重新执行 5–10 分钟校准；任务未启动。",
                age_seconds=math.floor(age_seconds),
                maximum_age_seconds=normalized_workload["calibration_max_age_seconds"],
            )
        )

    environment_values = {
        normalized_workload["environment_key"],
        normalized_calibration["environment_key"],
        binding["environment_key"],
    }
    environment_fingerprints = {
        normalized_workload["environment_fingerprint"],
        normalized_calibration["environment_fingerprint"],
        binding["environment_fingerprint"],
        actual_environment_fingerprint,
    }
    if len(environment_values) != 1 or len(environment_fingerprints) != 1:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CALIBRATION_ENVIRONMENT_MISMATCH,
                "校准、工作量与当前可验证运行环境不一致，任务未启动。",
            )
        )

    launch_fingerprints = {
        normalized_workload["launch_fingerprint"],
        normalized_calibration["launch_fingerprint"],
        binding["launch_fingerprint"],
    }
    if len(launch_fingerprints) != 1:
        issues.append(
            _issue(
                RUNTIME_BUDGET_COMMAND_MISMATCH,
                "校准命令与当前将要启动的命令不一致，任务未启动。",
            )
        )

    phase_estimates: list[dict[str, Any]] = []
    total_seconds = normalized_workload["fixed_overhead_p95_seconds"]
    total_output_bytes = 0.0
    estimate_overflow = False
    expected_phase_ids = {phase["phase_id"] for phase in normalized_workload["phases"]}
    if set(binding["execution_shapes"]) != expected_phase_ids:
        issues.append(
            _issue(
                RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH,
                "当前执行形态绑定未精确覆盖工作量阶段，任务未启动。",
            )
        )

    for phase in normalized_workload["phases"]:
        current_shape = binding["execution_shapes"].get(phase["phase_id"])
        if current_shape != phase["execution_shape"]:
            issues.append(
                _issue(
                    RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH,
                    f"阶段“{phase['phase_id']}”与当前执行形态不匹配，任务未启动。",
                    phase_id=phase["phase_id"],
                )
            )
            continue
        identity = (phase["calibration_key"], current_shape)
        sample = normalized_calibration["samples"].get(identity)
        if sample is None:
            if phase["calibration_key"] in normalized_calibration["sample_keys"]:
                issues.append(
                    _issue(
                        RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH,
                        f"阶段“{phase['phase_id']}”的校准执行形态不匹配，任务未启动。",
                        phase_id=phase["phase_id"],
                    )
                )
            else:
                issues.append(
                    _issue(
                        RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING,
                        f"阶段“{phase['phase_id']}”缺少校准样本，任务未启动。",
                        phase_id=phase["phase_id"],
                    )
                )
            continue
        try:
            phase_seconds = _safe_product(phase["units"], sample["p95_seconds_per_unit"])
            phase_output = _safe_product(phase["units"], sample["p95_output_bytes_per_unit"])
            total_seconds = _safe_sum(total_seconds, phase_seconds)
            total_output_bytes = _safe_sum(total_output_bytes, phase_output)
        except OverflowError:
            estimate_overflow = True
            break
        phase_estimates.append(
            {
                "phase_id": phase["phase_id"],
                "units": phase["units"],
                "sample_count": sample["sample_count"],
                "p95_seconds": math.ceil(phase_seconds),
                "predicted_output_bytes": math.ceil(phase_output),
            }
        )

    if estimate_overflow:
        issues.append(
            _issue(
                RUNTIME_BUDGET_ESTIMATE_OVERFLOW,
                "预算估算数值溢出安全范围，任务未启动。",
            )
        )
        return _decision(issues=issues, calibration=calibration_info, binding=binding_info)

    hard_stop = normalized_workload["hard_stop_seconds"]
    reserve = normalized_workload["deadline_reserve_seconds"]
    effective_budget = hard_stop - reserve
    estimated_seconds = math.ceil(total_seconds)
    predicted_output = math.ceil(total_output_bytes)
    try:
        required_free = int(
            _safe_sum(float(predicted_output), float(normalized_workload["minimum_free_after_bytes"]))
        )
    except OverflowError:
        issues.append(
            _issue(
                RUNTIME_BUDGET_ESTIMATE_OVERFLOW,
                "磁盘空间预算数值溢出安全范围，任务未启动。",
            )
        )
        return _decision(issues=issues, calibration=calibration_info, binding=binding_info)

    estimate: dict[str, Any] = {
        "complete": len(phase_estimates) == len(normalized_workload["phases"]),
        "estimated_p95_seconds": estimated_seconds,
        "hard_stop_seconds": math.ceil(hard_stop),
        "deadline_reserve_seconds": math.ceil(reserve),
        "effective_runtime_budget_seconds": math.floor(effective_budget),
        "deadline_margin_seconds": math.floor(effective_budget - total_seconds),
        "predicted_output_bytes": predicted_output,
        "minimum_free_after_bytes": normalized_workload["minimum_free_after_bytes"],
        "maximum_output_bytes": normalized_workload["maximum_output_bytes"],
        "required_free_bytes": required_free,
        "output_path": str(normalized_workload["output_path"]),
        "phases": phase_estimates,
    }

    incomplete_codes = {
        RUNTIME_BUDGET_CALIBRATION_INVALID,
        RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING,
        RUNTIME_BUDGET_CALIBRATION_SHAPE_MISMATCH,
        RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
        RUNTIME_BUDGET_COMMAND_MISMATCH,
    }
    if not any(item["code"] in incomplete_codes for item in issues) and total_seconds > effective_budget:
        issues.append(
            _issue(
                RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP,
                (
                    f"预计 p95 {_format_duration_zh(estimated_seconds)}，超过扣除缓冲后的 "
                    f"{_format_duration_zh(math.floor(effective_budget))} 硬停止预算，任务未启动。"
                ),
                estimated_p95_seconds=estimated_seconds,
                effective_runtime_budget_seconds=math.floor(effective_budget),
                hard_stop_seconds=math.ceil(hard_stop),
            )
        )

    maximum_output = normalized_workload["maximum_output_bytes"]
    if predicted_output > maximum_output:
        issues.append(
            _issue(
                RUNTIME_BUDGET_OUTPUT_CAP_EXCEEDED,
                (
                    f"预计新增输出 {_format_bytes_zh(predicted_output)}，超过工作量声明的 "
                    f"{_format_bytes_zh(maximum_output)} 上限，任务未启动。"
                ),
                predicted_output_bytes=predicted_output,
                maximum_output_bytes=maximum_output,
            )
        )

    provider = disk_usage_provider or _default_disk_usage
    try:
        total_bytes, free_bytes = _disk_values(provider(normalized_workload["output_path"]))
    except Exception:
        issues.append(
            _issue(
                RUNTIME_BUDGET_DISK_INSPECTION_FAILED,
                "无法读取目标磁盘可用空间，任务未启动。",
            )
        )
    else:
        estimate["disk_total_bytes"] = total_bytes
        estimate["disk_free_bytes"] = free_bytes
        estimate["disk_margin_bytes"] = free_bytes - required_free
        if free_bytes < required_free:
            issues.append(
                _issue(
                    RUNTIME_BUDGET_DISK_INSUFFICIENT,
                    (
                        f"目标磁盘可用 {_format_bytes_zh(free_bytes)}，运行与保留空间至少需要 "
                        f"{_format_bytes_zh(required_free)}，任务未启动。"
                    ),
                    free_bytes=free_bytes,
                    required_free_bytes=required_free,
                    predicted_output_bytes=predicted_output,
                    minimum_free_after_bytes=normalized_workload["minimum_free_after_bytes"],
                )
            )

    return _decision(issues=issues, estimate=estimate, calibration=calibration_info, binding=binding_info)


def _reject_json_constant(value: str) -> None:
    raise _DocumentError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DocumentError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _reject_reparse_path_components(path: Path) -> Path:
    candidate = path.absolute()
    components = [*reversed(candidate.parents), candidate]
    for component in components:
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            continue
        if _is_reparse(metadata):
            raise _DocumentError("budget document path cannot contain a symlink or reparse point")
    return candidate


def _read_json_document(path: Path) -> Mapping[str, Any]:
    """Read one regular, non-reparse file from one descriptor.

    ``lstat`` is only a pre-open link/reparse check.  Size/type and all bytes
    come from the same descriptor, and identity is compared after opening to
    catch ordinary swap races.
    """

    candidate = _reject_reparse_path_components(Path(path))
    before = os.lstat(candidate)
    if _is_reparse(before):
        raise _DocumentError("budget document cannot be a symlink or reparse point")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        metadata = os.fstat(descriptor)
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise _DocumentError("budget document must be a regular non-reparse file")
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise _DocumentError("budget document changed while opening")
        if metadata.st_size > MAX_DOCUMENT_BYTES:
            raise _DocumentError("budget document is too large")
        chunks: list[bytes] = []
        remaining = MAX_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise _DocumentError("budget document is too large")
    finally:
        os.close(descriptor)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _DocumentError("budget document must be UTF-8") from exc
    value = json.loads(
        text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    return _mapping(value, "document")


def _load_file_inputs(
    workload_path: Path,
    calibration_path: Path,
    binding_path: Path | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any] | None, Path]:
    workload_file = Path(workload_path).absolute()
    workload = _read_json_document(workload_file)
    try:
        calibration = _read_json_document(Path(calibration_path).absolute())
    except FileNotFoundError:
        calibration = None
    binding = None if binding_path is None else _read_json_document(Path(binding_path).absolute())
    return workload, calibration, binding, workload_file.parent


def evaluate_runtime_budget_files(
    workload_path: Path,
    calibration_path: Path,
    binding_path: Path | None = None,
    *,
    now: datetime | None = None,
    disk_usage_provider: Callable[[Path], Any] | None = None,
    current_environment_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Safely read declarative files and evaluate them without writes."""

    try:
        workload_file = Path(workload_path).absolute()
        workload = _read_json_document(workload_file)
    except Exception as exc:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_WORKLOAD_INVALID,
                    "无法读取有效的工作量预算声明，任务未启动。",
                    reason=type(exc).__name__,
                )
            ]
        )
    try:
        calibration = _read_json_document(Path(calibration_path).absolute())
    except FileNotFoundError:
        calibration = None
    except Exception as exc:
        return _decision(
            issues=[
                _issue(
                    RUNTIME_BUDGET_CALIBRATION_INVALID,
                    "无法读取有效的校准记录，任务未启动。",
                    reason=type(exc).__name__,
                )
            ]
        )
    if binding_path is None:
        binding = None
    else:
        try:
            binding = _read_json_document(Path(binding_path).absolute())
        except Exception as exc:
            return _decision(
                issues=[
                    _issue(
                        RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
                        "无法读取有效的当前运行绑定，任务未启动。",
                        reason=type(exc).__name__,
                    )
                ]
            )
    return evaluate_runtime_budget(
        workload,
        calibration,
        binding,
        workload_base_dir=workload_file.parent,
        now=now,
        disk_usage_provider=disk_usage_provider,
        current_environment_provider=current_environment_provider,
    )


def launch_if_runtime_budget_allows(
    launcher: Callable[[], _T],
    workload: Mapping[str, Any] | None,
    calibration: Mapping[str, Any] | None,
    current_binding: Mapping[str, Any] | None = None,
    *,
    workload_base_dir: Path | None = None,
    now: datetime | None = None,
    disk_usage_provider: Callable[[Path], Any] | None = None,
    current_environment_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], _T | None]:
    """Evaluate immediately before launch and never call a blocked callback."""

    decision = evaluate_runtime_budget(
        workload,
        calibration,
        current_binding,
        workload_base_dir=workload_base_dir,
        now=now,
        disk_usage_provider=disk_usage_provider,
        current_environment_provider=current_environment_provider,
    )
    if not decision["should_start"]:
        return decision, None
    return decision, launcher()


def launch_runtime_budget_files(
    workload_path: Path,
    calibration_path: Path,
    binding_path: Path,
    *,
    runner: Callable[..., Any] | None = None,
    now: datetime | None = None,
    disk_usage_provider: Callable[[Path], Any] | None = None,
    current_environment_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], RuntimeLaunchOutcome | None]:
    """Gate and run the exact bound argv/cwd from one safe read of each file."""

    try:
        workload, calibration, raw_binding, base_dir = _load_file_inputs(
            workload_path, calibration_path, binding_path
        )
    except FileNotFoundError as exc:
        code = (
            RUNTIME_BUDGET_CALIBRATION_REQUIRED
            if Path(exc.filename or "") == Path(calibration_path)
            else RUNTIME_BUDGET_WORKLOAD_INVALID
        )
        return _decision(issues=[_issue(code, "预算门禁输入文件缺失，任务未启动。")]), None
    except Exception as exc:
        return (
            _decision(
                issues=[
                    _issue(
                        RUNTIME_BUDGET_CURRENT_BINDING_INVALID,
                        "无法安全读取预算门禁输入，任务未启动。",
                        reason=type(exc).__name__,
                    )
                ]
            ),
            None,
        )

    decision = evaluate_runtime_budget(
        workload,
        calibration,
        raw_binding,
        workload_base_dir=base_dir,
        now=now,
        disk_usage_provider=disk_usage_provider,
        current_environment_provider=current_environment_provider,
    )
    if not decision["should_start"]:
        return decision, None

    # Re-validate the already-read binding to obtain the exact command.  No
    # source file is reopened between authorization and process creation.
    binding = _validate_binding(raw_binding, base_dir=base_dir)
    normalized_workload = _validate_workload(workload, base_dir=base_dir)
    if normalized_workload["kind"] != "formal_experiment":
        return (
            _decision(
                issues=[
                    _issue(
                        RUNTIME_BUDGET_WORKLOAD_INVALID,
                        "显式生产启动入口只接受 formal_experiment，任务未启动。",
                    )
                ]
            ),
            None,
        )
    timeout = math.ceil(normalized_workload["hard_stop_seconds"])
    if runner is None:
        raw_outcome = run_in_kill_on_close_job(
            list(binding["argv"]),
            cwd=binding["cwd"],
            timeout_seconds=timeout,
        )
        return decision, RuntimeLaunchOutcome(
            started=bool(raw_outcome.get("started")),
            returncode=raw_outcome.get("returncode"),
            timed_out=bool(raw_outcome.get("timed_out")),
            tree_guarded=bool(raw_outcome.get("tree_guarded")),
            tree_terminated_verified=bool(raw_outcome.get("tree_terminated_verified")),
            error_code=str(raw_outcome.get("error_code") or ""),
            error_type=str(raw_outcome.get("error_type") or ""),
        )

    # Dependency injection is reserved for unit tests.  It deliberately does
    # not claim process-tree protection and is never used by the production CLI.
    try:
        completed = runner(
            list(binding["argv"]),
            cwd=str(binding["cwd"]),
            shell=False,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            decision,
            RuntimeLaunchOutcome(
                started=True,
                returncode=None,
                timed_out=True,
                error_code=RUNTIME_BUDGET_LAUNCH_TIMEOUT,
                error_type="TimeoutExpired",
            ),
        )
    except Exception as exc:
        return (
            decision,
            RuntimeLaunchOutcome(
                started=False,
                returncode=None,
                error_code=RUNTIME_BUDGET_LAUNCH_FAILED,
                error_type=type(exc).__name__,
            ),
        )
    return decision, RuntimeLaunchOutcome(
        started=True,
        returncode=getattr(completed, "returncode", None),
    )
