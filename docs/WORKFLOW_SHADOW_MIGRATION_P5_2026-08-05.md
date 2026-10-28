# P5 旧 Runtime 兼容与影子验证合同

日期：2026-08-05

## 结论

P5 只增加旁路观察能力。旧 runtime 仍是论文阶段 JSON、SDK journal、命令队列、会话和论文产物的唯一生产写入者。影子扫描不得创建真实 `WorkflowRun`，不得调用 `WorkflowScheduler`，不得注册生产 `LegacyStageExecutor` 回调，也不得调用任何带同步、恢复、排队或对账副作用的旧入口。

## 权威源

每次观察至少联合读取阶段 pipeline 与 SDK journal；仅看 UI 投影不足以比较模型、Token、会话和 Step 细节。

| 阶段 | Pipeline | SDK journal |
|---|---|---|
| writing | `controller/pipeline_state.json` | `state/writing_sdk_dispatch.json` |
| figures | `controller/figure_pipeline_state.json` | `state/figure_sdk_dispatch.json` |
| journal-author | `controller/journal_author_state.json` | `state/journal_author_sdk_dispatch.json` |
| smart-submission | Smart 主状态与旧面板状态 | Smart SDK journal |

路径必须由调用方显式传入；通用 workflow 包不导入 `config.py`，也不自行选择 active workspace。

## 一致性算法

1. 读取所有显式源的精确字节 SHA256。
2. 从冻结字节投影 Step、状态、会话指纹、模型、推理、Token、阻塞码和产物证据。
3. 产物只做受控路径内的稳定读取与 SHA256，不复制、不密封、不修复。
4. 再读所有源 SHA256。
5. 任一源变化则有限重试；持续变化只记录 `unstable_snapshot`，parity 必须是 `unknown`。

观察器不获取旧 runtime 的文件锁，因为获取锁本身可能创建文件或干扰生产写者。

## 数据隔离与脱敏

- Shadow SQLite 使用独立文件 `state/v2/workflow-shadow.sqlite3`。
- 数据库没有 run、node、event、lease 或 session 调度表，不能被调度器 claim。
- 原始 paper/thread ID、绝对路径、Prompt 正文、final response、命令、Cookie、凭证和 blocker 正文不得持久化。
- paper ID 使用 domain-separated SHA256；thread ID 只保存会话指纹；相对产物 URI 与 blocker 摘要在持久化前再次哈希。
- observation 与 comparison 都 append-only、内容寻址且幂等；同一稳定身份出现不同 payload 必须报冲突。
- Shadow 写锁、磁盘或数据库失败仅返回“未记录”诊断，不影响旧 runtime。

## 字段 verdict

字段比较只允许：

- `exact`：冻结值逐项相同；
- `equivalent`：语义相同但表示不同，例如旧 `hard_token_limit=0` 与通用合同 `None` 都表示不限；
- `unknown`：源缺失、未采样或快照不稳定；
- `mismatch`：两个已知值真实不一致。

缺失值不得被归一化为零；累计 Token 与最后一次 Token 不得重复相加；只有程序产物合同通过后，legacy `complete` 才能与 generic `completed` 对齐。

## LegacyStageExecutor 边界

兼容端口只允许“整阶段调用一次”，旧 runtime 继续拥有内部 Step 循环、下一步选择、重试和会话管理。默认模式返回空 callback；`shadow` 明确拒绝；只有未来显式 `cutover` 且注入整阶段 port 时才可构建 callback。

目前三个旧 runtime 仍包含后台等待语义，而通用节点合同没有 `WAITING_EXTERNAL`。因此 P5 不激活任何生产 callback。正式切流前必须增加统一阻塞式整阶段 facade，或增加由观察器驱动完成的外部等待状态；不得借用 `WAITING_HUMAN` 冒充后台执行。

## 禁止调用

- `UnifiedOrchestrator.sync()` / `advance_once(apply=True)`；
- 任意 adapter 的 `save_state`、`queue`、`start`、`pause`；
- 任意 runtime 的 `run`、`start_background`、`request_pause`；
- Smart 默认会持久化或同步账本的 plan/scheduler 入口；
- `WorkflowStore.create_run()`、`WorkflowScheduler.run_once()`；
- 旧 `run_smart_submission_scheduler` 兼容调度器。

P5 one-shot 工具默认只向 stdout 输出脱敏比较；只有显式提供 shadow DB 路径时才允许写入隔离账本。
