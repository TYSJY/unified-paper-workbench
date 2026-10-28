# 通用线性工作流迁移计划与生产基线

更新日期：2026-08-01

当前产品版本：0.12.0b3

生产基线提交：`7f33ff4ee9ccd1dc34e7e01a21a9483c73147836`

回退标签：`pre-generic-workflow-0.12.0b3-20260801`

开发分支：`codex/generic-workflow-engine`

## 1. 目标

将写作、画图和选刊作者阶段中重复的 Step 调度能力逐步抽取为通用工作流内核，同时把当前 SCI 流程保留为只读内置 Workflow Pack。第一版产品界面只开放线性编排，但持久化合同从第一天开始采用稳定节点 ID 和有向边，以便以后增加条件分支、有限循环、子流程和插件，而不重写运行账本。

智能投稿继续作为专业、长周期、外部状态驱动的插件接入。本轮不把投稿网页状态机强行改造成普通 Codex Step。

## 2. 不可破坏的生产行为

1. 当前 SCI 写作、画图、选刊作者提示词字节保持不变，并继续受 SHA256 封印保护。
2. 调度、并发槽、下一步推进、重试和恢复由程序决定，不交给 AI 判断。
3. 同一篇论文在同一会话组内复用固定 Codex SDK 会话；不同论文可以并行。
4. 同一篇论文的线性节点默认一次只执行一个，不能因为多次点击而重复派发。
5. 每个运行冻结工作流版本、Prompt 指纹、模型、推理强度、Token 上限和产物合同。
6. 重试同一节点默认复用原快照、原模型和原会话；显式用户操作才能创建新版本或新会话。
7. AI 完成后必须由程序验收产物，AI 的自然语言声明不能单独作为完成依据。
8. Step 5 信任模式、人工处理节点、归档删除、工作区切换和一键全流程现有语义保持不变。
9. 切换工作流只影响新任务和视图筛选，不能修改已经开始的运行。
10. 新内核在完成影子验证前不得成为生产状态的唯一写入者。

基线核验同时确认了一个迁移债务：现有三套工作流快照摘要包含绝对 `prompt_path`，因此相同 Prompt 在不同工作区会产生不同 workflow SHA256。P1 必须把“可移植工作流定义摘要”和“运行时路径绑定摘要”拆开；Prompt 本体仍由独立 SHA256 封印判断，不能把路径变化误报为 Prompt 内容变化。

## 3. 目标模块边界

稳定实现放入 `unified_paper_orchestrator/workflow/`，不再继续扩大带实验含义的 `unified_paper_orchestrator_v2` 包。

建议模块：

```text
workflow/
  contracts.py       工作流、节点、边、运行和节点运行合同
  validation.py      图结构、线性约束、版本和绑定校验
  registry.py        Workflow Pack 与版本注册表
  store.py           SQLite 持久化、迁移、事件和租约
  kernel.py          Ready-node 计算和状态转换
  scheduler.py       并发槽、派发、恢复和取消
  artifacts.py       输入输出绑定与产物指纹
  sessions.py        Codex 固定会话分组
  executors/
    base.py
    codex_sdk.py
    deterministic.py
    human.py
    plugin.py
    legacy_stage.py
  packs/
    sci.py
```

现有 `unified_paper_orchestrator_v2` 的 `TaskEnvelope`、`EventStore`、`WorkflowKernel` 和执行器注册表作为设计与迁移来源；生产代码在新稳定命名空间中演进，旧实验 API 暂时保留兼容层。

## 4. 分阶段实施

### P0：冻结基线

- 建立精确回退标签和独立开发分支。
- 保存候选 EXE 到 `releases/binaries/0.12.0b3/`。
- 保存机器可读基线 `docs/baselines/GENERIC_WORKFLOW_BASELINE_0.12.0b3.json`。
- 完整测试基线：1339 passed，54 subtests passed。
- 加入 3 项基线合同测试后的 P0 回归：1342 passed，54 subtests passed。

### P1：图兼容合同

- 新增 WorkflowDefinition、WorkflowVersion、NodeDefinition、EdgeDefinition、WorkflowRun、NodeRun。
- 节点使用稳定 `node_id`，显示顺序与运行身份分离。
- 第一版验证器要求单入口、单出口、无分支、无循环，但数据结构允许未来放宽。
- 加入工作流快照和 canonical SHA256。

### P2：持久化账本

- 对现有 SQLite 仅做增量迁移。
- 增加 workflow_versions、workflow_runs、node_runs、workflow_events、workflow_sessions、workflow_artifacts 和 workflow_leases。
- 实现原子 claim、幂等创建、过期租约恢复、暂停、取消和显式重试。
- 不导入或覆盖现有阶段 JSON；先把它们视为外部生产事实源。

### P3：通用线性调度内核

- 程序计算唯一 Ready 节点。
- 每篇运行最多一个活动节点，不同运行共享可配置并发池。
- 接入 Codex SDK、确定性程序、人工和插件执行器。
- `session_group` 决定固定会话复用范围，SCI 保留现有阶段级会话边界。
- 执行成功后必须通过节点输出合同，才允许推进下一节点。

### P4：SCI 内置 Workflow Pack

- 把当前写作 10 Step、画图 3 Step、选刊作者当前 combined 模式和智能投稿插件注册为只读版本。
- Prompt 只保存引用与封印，不复制、不重写。
- 内置模板允许克隆，不允许原地编辑。

### P5：兼容适配和影子验证

- LegacyStageExecutor 调用现有 runtime，不复制其内部调度职责。
- 新内核先只镜像事件并比较 Step、状态、会话、模型、Token、阻塞和产物。
- 影子运行期间旧 runtime 仍是唯一生产写入者。

### P6：工作流库与线性编辑器

- 增加工作流库、模板选择、克隆、版本历史、导入导出。
- 增加 Step 新增、删除、排序、执行器、模型、Prompt、输入输出和会话组配置。
- SCI 专业页暂时保留，通用任务使用统一运行时间线。

### P7：通用性验证

- 建立不依赖论文目录的三步测试工作流。
- 建立本科毕业论文 Workflow Pack。
- 验证工作流引擎不依赖固定十步、SCI 文件名或 Step 5/6 专用语义。

### P8：逐阶段生产切换

- 按选刊作者、画图、写作的顺序由低风险到高风险切换。
- 每个阶段均保留功能开关和旧 runtime 回退路径。
- 只有连续回归一致后，才删除该阶段重复的队列、重试和会话推进职责。

### P9：发布验收

- 两篇 SCI 任务并发跑通写作、画图、选刊和智能投稿。
- 验证自定义工作流、本科论文、重启恢复、失败重试、工作区切换和中英文界面。
- 生成候选 EXE、SHA256、迁移说明和回退演练记录。

## 5. 阶段完成门槛

每个阶段必须同时满足：

1. 针对性测试通过；
2. 完整测试不低于本基线；
3. Prompt 封印全部通过；
4. Git 工作树除明确保留的本地 `.codex/` 外无意外文件；
5. 提交信息只包含该阶段内容；
6. 若改变生产入口，必须提供并实际验证回退路径。

## 6. 回退方式

源码基线：

```powershell
git switch --detach pre-generic-workflow-0.12.0b3-20260801
```

保留的基线 EXE：

```text
D:\Documents\大一统\releases\binaries\0.12.0b3\UnifiedPaperOrchestrator.exe
```

SHA256：

```text
8378E75834A21FC56E8038C0B17D31EDADF1B5C2231ED5B3F0954C5047C5D371
```

由于 P1-P7 使用新增表、旁路内核和功能开关，不要求删除现有阶段 JSON，因此回退旧 EXE 时原生产数据仍可继续读取。
