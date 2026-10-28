# 通用工作流改造交接单：P1

更新时间：2026-08-05

## 当前状态

- 仓库：`D:\Documents\大一统`
- 开发分支：`codex/generic-workflow-engine`
- 当前提交：`af4b42ee0ace05194bc2f127acc60a848f4e12ea`
- P0 状态：已完成并提交
- P1 状态：尚未写入代码，可从干净边界开始
- 工作树：除本机未跟踪目录 `.codex/` 外无业务代码改动
- `.codex/` 属于本机工具状态，不得加入 Git

## 已完成的 P0

P0 提交：

```text
af4b42e chore(workflow): freeze pre-refactor production baseline
```

回退标签：

```text
pre-generic-workflow-0.12.0b3-20260801
```

基线 EXE：

```text
D:\Documents\大一统\releases\binaries\0.12.0b3\UnifiedPaperOrchestrator.exe
```

SHA256：

```text
8378E75834A21FC56E8038C0B17D31EDADF1B5C2231ED5B3F0954C5047C5D371
```

测试记录：

- 改造前：1339 passed，54 subtests passed
- 加入 P0 基线合同测试后：1342 passed，54 subtests passed

基线文件：

- `docs/GENERIC_LINEAR_WORKFLOW_MIGRATION_PLAN_2026-08-01.md`
- `docs/baselines/GENERIC_WORKFLOW_BASELINE_0.12.0b3.json`
- `tests/test_generic_workflow_migration_baseline.py`

## 用户已经确认的目标

先把产品建设成通用线性工作流编排器，同时完整保留当前 SCI 工作流。第一版 UI 只开放线性流程，但底层合同必须能在后续支持条件分支、有限循环、子流程和插件，不能依赖 `current_step += 1`。

程序负责调度、并发、状态推进、重试和恢复；Codex SDK 只是节点执行器。每篇任务内部按节点顺序执行，不同任务可以并行。固定 SDK 会话通过 `session_group` 配置；SCI 继续保留写作、画图、选刊作者各自的会话边界。

智能投稿继续作为专业插件和外部状态机接入，本轮不重写其网页执行逻辑。

## 不可违反的约束

1. 不修改写作、画图、选刊作者现有内部提示词。
2. 每次修改后继续验证 Prompt SHA256 封印。
3. 不直接切换生产调度器；P1-P4 只能新增旁路能力。
4. 不覆盖现有论文目录、阶段 JSON、SQLite 或工作区设置。
5. SCI 内置 Workflow Pack 必须只读，但以后允许克隆为自定义模板。
6. 已启动任务冻结工作流版本、Prompt、模型、推理、Token 和产物合同。
7. 工作流切换只影响新任务，不能改变运行中的论文。
8. 每个阶段独立提交、测试和保留回退点。

## P1 的准确开发范围

只新增稳定合同包，建议目录：

```text
unified_paper_orchestrator/workflow/
  __init__.py
  contracts.py
  validation.py
```

P1 应定义：

- `WorkflowDefinition`
- `WorkflowVersion`
- `NodeDefinition`
- `EdgeDefinition`
- `WorkflowRun`
- `NodeRun`
- `ArtifactSpec` 与 `ArtifactBinding`
- `PromptRef` 与独立的运行时 `ResourceBinding`
- `ExecutionProfile`
- `RetryPolicy`
- `SessionPolicy`
- 工作流、节点运行状态枚举

关键设计：

- 工作流定义只保存逻辑资源 ID 和内容 SHA256，不保存绝对 Prompt 路径。
- 绝对路径放入独立 `WorkflowRuntimeBinding`，另算 binding SHA256。
- portable definition SHA256 不得因工作区路径或设备变化而改变。
- 节点使用稳定 `node_id`；显示顺序不能充当运行身份。
- Edge 合同预留 success、condition、loop 和 error 类型。
- loop edge 必须包含明确的最大遍历次数。
- Node 类型预留 codex、deterministic、human、plugin、subworkflow 和 legacy-stage。
- 第一版发布验证严格要求单入口、单出口、无分支、无循环的线性结构。
- 图模式合同可序列化分支和有限循环，但 P1 不执行它们。

## P1 测试要求

新增独立测试，至少覆盖：

1. 三节点线性工作流验证通过。
2. 定义序列化、反序列化和 canonical SHA256 稳定。
3. 同一逻辑定义绑定到不同绝对路径时 definition SHA256 相同、binding SHA256 不同。
4. 重复节点 ID、缺失边端点、断开节点、多个入口或出口被拒绝。
5. 线性模式拒绝分支和循环。
6. 图合同可表达条件边、有限循环、插件节点和子流程节点。
7. loop edge 没有上限时被拒绝。
8. ArtifactBinding 只能引用存在的工作流输入或上游节点输出。
9. WorkflowRun 与 NodeRun 状态可稳定往返序列化。
10. P0 Prompt 基线测试继续通过。

P1 完成后先运行针对性测试，再运行完整测试；通过后单独提交，不进入 P2。

## 后续阶段

完整计划以 `docs/GENERIC_LINEAR_WORKFLOW_MIGRATION_PLAN_2026-08-01.md` 为准：

- P2：SQLite 工作流账本、事件、租约和恢复
- P3：通用线性调度内核和执行器接口
- P4：只读 SCI Workflow Pack
- P5：旧 runtime 兼容适配与影子验证
- P6：工作流库和线性编辑器
- P7：三步自定义流程与本科论文模板
- P8：按选刊作者、画图、写作顺序逐阶段切换
- P9：双篇并发全流程、候选 EXE 与回退演练

## 新会话启动检查

新会话开始时先执行只读检查：

```powershell
cd "D:\Documents\大一统"
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

预期分支为 `codex/generic-workflow-engine`。如果 HEAD 已包含本交接文档提交，则直接从 P1 合同设计开始；不要重新执行 P0。
