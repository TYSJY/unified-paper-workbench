# 正式高成本任务运行预算门禁

`runtime_budget_gate.py` 只用于正式、耗时、可能产生大量文件的任务。普通调度心跳、小型单元测试和界面刷新不经过它，避免把日常操作误拦截。

当前仓库没有一个能够确认覆盖全部正式实验的既有生产启动函数。因此新增了唯一可声明“已受门禁保护”的显式入口：

```powershell
UnifiedPaperOrchestrator.exe runtime-budget-launch workload.json calibration.json current_binding.json
```

直接运行实验脚本、从终端手工启动、或只运行 `runtime-budget-check`，都不得宣称该次正式实验已经接入运行预算门禁。

## 三份严格输入

三份 JSON 都使用严格 schema 1：`schema_version` 必须是 JSON 整数 `1`，`true`、`1.0` 均无效；缺少字段、未知字段、重复 key、非有限数值、超限数值均 fail closed。

### 1. 工作量声明

所有安全字段都必须显式填写，不能依赖默认值。`output_path` 若为相对路径，按 `workload.json` 所在目录解析。

```json
{
  "schema_version": 1,
  "workload_id": "experiment-v1",
  "kind": "formal_experiment",
  "environment_key": "verified-host-workers2",
  "environment_fingerprint": "64位小写SHA256",
  "launch_fingerprint": "64位小写SHA256",
  "output_path": "results/formal",
  "hard_stop_seconds": 25200,
  "deadline_reserve_seconds": 1800,
  "fixed_overhead_p95_seconds": 600,
  "minimum_free_after_bytes": 10737418240,
  "maximum_output_bytes": 5368709120,
  "calibration_max_age_seconds": 86400,
  "phases": [
    {
      "phase_id": "correctness",
      "calibration_key": "correctness-medium",
      "execution_shape": "workers=2;raw=retain;compression=gzip",
      "units": 40
    }
  ]
}
```

### 2. 校准记录

校准必须持续 300–600 秒，带时区、未过期，并与同一工作量、环境指纹、启动命令和执行形态精确绑定。每个 profile 的 `sample_count` 至少为 20；少于 20 个样本无法支撑可审计的经验 p95，直接拒绝。

```json
{
  "schema_version": 1,
  "workload_id": "experiment-v1",
  "environment_key": "verified-host-workers2",
  "environment_fingerprint": "与当前主机一致的64位小写SHA256",
  "launch_fingerprint": "与实际argv和cwd一致的64位小写SHA256",
  "captured_at": "2026-07-11T10:00:00Z",
  "duration_seconds": 480,
  "samples": [
    {
      "calibration_key": "correctness-medium",
      "execution_shape": "workers=2;raw=retain;compression=gzip",
      "sample_count": 20,
      "p95_seconds_per_unit": 420,
      "p95_output_bytes_per_unit": 67108864
    }
  ]
}
```

### 3. 当前运行绑定

工作量与校准不能只互相声称环境一致。第三份 binding 描述当前主机、即将执行的精确 argv/cwd，以及本次运行的每个 phase 执行形态。门禁会现场采集非敏感主机事实并逐项比对；缺少 binding 或绑定环境与现场不一致时一律 BLOCKED。

```json
{
  "schema_version": 1,
  "environment_key": "verified-host-workers2",
  "environment": {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.10",
    "logical_cpu_count": 16
  },
  "execution_shapes": [
    {
      "phase_id": "correctness",
      "execution_shape": "workers=2;raw=retain;compression=gzip"
    }
  ],
  "argv": ["C:\\Python312\\python.exe", "run_formal.py", "--workers", "2", "--retain-raw"],
  "cwd": "."
}
```

binding 的 `cwd` 相对 `workload.json` 目录解析。`argv[0]` 必须是当前存在、不是 symlink/reparse 的绝对可执行文件路径；只写 `python` 会在 check 阶段稳定返回 `RUNTIME_BUDGET_CURRENT_BINDING_INVALID`，不会出现 check 通过后才无法保护的状态。生产入口只会启动 binding 中这组 argv/cwd，不接受检查通过后再替换命令。

校准器可用同一模块生成字段：

```python
from unified_paper_orchestrator.runtime_budget_gate import (
    capture_current_environment,
    environment_fingerprint,
    launch_fingerprint,
)

environment = capture_current_environment()
environment_sha256 = environment_fingerprint(environment)
command_sha256 = launch_fingerprint(argv, absolute_cwd)
```

校准程序必须从真实测量生成 p95，不能用估计值伪装成校准。`execution_shape` 的业务语义（例如 workers、原始文件保留、压缩策略）仍由正式实验启动器负责准确声明；门禁保证三份输入和实际启动命令完全绑定，但不会理解任意实验脚本内部的自定义参数语义。

## 检查与退出码

只读检查：

```powershell
UnifiedPaperOrchestrator.exe runtime-budget-check workload.json calibration.json current_binding.json
```

- `READY`：退出码 `0`；
- `BLOCKED`：退出码 `2`；
- 不传第三个 binding：稳定返回 `RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED` 和退出码 `2`。

`runtime-budget-launch` 的门禁被拒绝时退出码仍为 `2`。门禁 READY 后，只有正式命令返回 `0`、整棵进程树保护已启用且终止核验通过时 CLI 才返回 `0`；正式命令非零、超时、Job Object 建立失败或整树终止无法核验时统一返回 `1`。JSON 同时保留预算 `decision.status`、真实 `returncode`、`timed_out`、`tree_guarded`、`tree_terminated_verified` 和稳定错误码，因此不会把“预算通过”误写成“实验运行成功”。

Windows 生产入口先以 `CREATE_SUSPENDED` 创建进程，再建立带 `KILL_ON_JOB_CLOSE` 的 Job Object、把尚未执行的进程加入 Job，成功后才恢复主线程。达到 `hard_stop_seconds`（当前上限 30 天）时终止整个 Job；正常主进程退出时也会清理仍存活的派生进程。返回前通过 Job accounting 核验 `ActiveProcesses=0`，关闭 Job handle 仍是最后一道整树终止后备。如果 Job 建立或绑定失败，挂起进程会在恢复前被终止，本次启动不宣称受门禁保护。

## 安全读取与稳定失败

- 三份输入都拒绝 symlink/reparse point 和非普通文件；
- 文件从同一个 descriptor 完成 `fstat`、限长和读取，并核对打开前后文件身份，避免 `stat`/`read_text` 分离造成的常见 TOCTOU；
- JSON 最大 1 MiB，拒绝重复 key、NaN、Infinity；
- 所有数字有有限性和上界检查，乘法、求和或磁盘需求溢出返回 `RUNTIME_BUDGET_ESTIMATE_OVERFLOW`，不会抛出未处理异常；
- 无法检查磁盘返回 `RUNTIME_BUDGET_DISK_INSPECTION_FAILED`；
- 预计 p95、输出上限、运行后保留空间任何一项不满足都不会调用 launcher。
- Windows 正式启动若无法预先建立 Job Object 整树保护，返回 `RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE`；超时为 `RUNTIME_BUDGET_LAUNCH_TIMEOUT`，终止后无法核验整树为空为 `RUNTIME_BUDGET_PROCESS_TREE_TERMINATION_UNVERIFIED`。

主要稳定错误码包括：

- `RUNTIME_BUDGET_CURRENT_BINDING_REQUIRED` / `RUNTIME_BUDGET_CURRENT_BINDING_INVALID`；
- `RUNTIME_BUDGET_CALIBRATION_REQUIRED` / `..._INVALID` / `..._WINDOW_INVALID` / `..._STALE`；
- `RUNTIME_BUDGET_CALIBRATION_ENVIRONMENT_MISMATCH`；
- `RUNTIME_BUDGET_COMMAND_MISMATCH`；
- `RUNTIME_BUDGET_CALIBRATION_PROFILE_MISSING` / `..._SHAPE_MISMATCH`；
- `RUNTIME_BUDGET_ESTIMATE_OVERFLOW`；
- `RUNTIME_BUDGET_P95_EXCEEDS_HARD_STOP`；
- `RUNTIME_BUDGET_OUTPUT_CAP_EXCEEDED`；
- `RUNTIME_BUDGET_DISK_INSUFFICIENT` / `..._INSPECTION_FAILED`。
- `RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE` / `..._TERMINATION_UNVERIFIED` / `RUNTIME_BUDGET_LAUNCH_TIMEOUT`。
