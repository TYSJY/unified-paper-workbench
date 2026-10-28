# P9 通用工作流独立候选发布说明（2026-08-05）

## 结论

P9 交付的是与现有生产桌面应用隔离的控制台候选：`GenericWorkflowCandidate.exe`。当前双击友好修订版为 `0.13.1-candidate`；无参数启动会进入中文引导菜单，有参数启动仍保持稳定 JSON CLI。它以候选专属工作区运行通用工作流库、持久化账本和调度器，不导入 Qt/生产应用入口，不修改现有提示词、生产调度入口或顶层 `dist`。

本候选目前只能作离线机械验收。在线生产验收状态必须保持 `NOT_RUN`，所有 fake-provider 与 SCI 验收执行均标记 `production_equivalence=false`，因此不具备生产晋级资格。

## 交付面

- `tools/workflow_candidate.py`：零参数中文引导菜单；显式参数模式保持 `zh-CN` / `en-US` JSON CLI。
- `unified_paper_orchestrator/workflow/candidate.py`：候选组合根及离线验收器。
- `tools/build_workflow_candidate.ps1`：只清理 `build/workflow_candidate` 的独立 one-file console 构建。
- `build/workflow_candidate/release/GenericWorkflowCandidate.exe`：构建后的候选二进制。
- `GenericWorkflowCandidate.exe.sha256`、`build-manifest.json`、`offline-acceptance.json`、`rollback-drill.json`：构建证据。

候选初始化会安装六条内置工作流：一条通用离线示例、一条本科论文工作流和四条 SCI 阶段工作流。本科六份已封印提示词在初始化时按固定 SHA-256 复制到显式工作区的内容寻址存储，因此运行绑定不会持有 PyInstaller `_MEIPASS` 临时路径，重启后仍能稳定校验。

## 控制台使用

直接从资源管理器双击 `GenericWorkflowCandidate.exe`，会显示中文菜单并预选 `%LOCALAPPDATA%\GenericWorkflowCandidate\workspace`。单纯打开或退出不会创建文件；选择“快速开始”或“初始化”后才会写入。菜单可临时切换到另一个绝对路径的新目录或空目录。候选 marker、SQLite owner 和重解析点检查仍然生效，因此不能借交互菜单认领生产工作区。

需要自动化时，所有有状态命令仍必须显式给出 `--workspace`：

```powershell
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate --locale zh-CN init
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate --locale en-US list
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate doctor
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate demo-create --idempotency-key demo-1
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate tick <RUN_ID>
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate resume <RUN_ID>
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate timeline <RUN_ID>
python tools/workflow_candidate.py --workspace D:\WorkflowCandidate accept-offline
```

`tick` 只执行一个有界调度周期；随后可从另一进程用 `resume` 重开同一 SQLite 账本并运行至稳定状态。`timeline` 只返回脱敏后的运行投影。

首次 `init` 只会认领新目录或既有空目录，并以候选专属 marker 标记所有权；marker 从 `initializing` 原子推进到 `ready`，其随机 workspace ID 会同时写入 SQLite 元数据。后续命令必须双向校验根 marker 与数据库 owner；无 marker 的非空目录、marker/数据库身份错配、既有产品账本以及经过 symlink/junction/reparse point 的路径都会在写入前被拒绝。初始化在建库前或空 schema 窗口中断时可沿用同一 ID 幂等恢复；恢复要求精确的 SQLite schema 指纹、meta allowlist、空业务表、空 sequence 与空 freelist，并在同一 `BEGIN IMMEDIATE` 事务内复核后才写 owner，因此不会自动收养已有或并发写入数据的未知账本。

`accept-offline` 的报告只能写入显式工作区，使用独占创建，拒绝覆盖，并拒绝通过 `..`、symlink 或 Windows reparse point/junction 逃逸。

本修订同时关闭了 dormant stage-routing sidecar 的首次并发初始化窗口：schema、owner marker 与三条默认 policy 现在在 `BEGIN IMMEDIATE` 事务内原子提交，提交后再以有界重试启用 WAL；线程和独立进程并发初始化均保持幂等。该 sidecar 仍未接入生产调度入口。

## 离线验收口径

无 SCI 根目录时，离线总结果最多是 `PASS_WITH_LIMITATIONS`；SCI 检查诚实记录为 `NOT_RUN`。提供三组根目录时：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_workflow_candidate.ps1 `
  -SciWritingRoot D:\PaperProjects\UnifiedPaper-Main\stages\writing `
  -SciFiguresRoot D:\PaperProjects\UnifiedPaper-Main\stages\figures `
  -SciJournalAuthorRoot D:\PaperProjects\UnifiedPaper-Main\stages\journal_author
```

离线报告覆盖：

1. 两个真实 deterministic run 并发完成，并证明最大同时执行数为 2。
2. 受保护示例被 clone 为 custom draft、publish、执行，同时源定义哈希不变。
3. 本科六节点链通过无网络 fake-provider 和正式结构校验器完成；这不是模型质量验证。
4. 两个 subject 各自对 writing、figures、journal-author、smart-submission 四个独立工作流执行机械 smoke；使用仅限验收的 artifact producer，不调用 Codex provider，不打开浏览器，不提交期刊。
5. 一个节点后关闭组合根，再从同一版本、同一 runtime binding 重开并完成。
6. 首次 transient failure 形成独立 retry attempt，第二次完成，两个 attempt 均留在账本。
7. 两个显式工作区的数据库、Prompt 存储和运行记录相互隔离，工作区注册表字节指纹不变。

SCI contract simulation 只证明各独立工作流定义、Prompt seal、声明产物的媒体类型/引用合同、并发调度和账本机械行为。它没有执行结构 schema 验证、跨工作流 artifact 交接或端到端阶段顺序，也不证明模型可用性、输出质量、token/cost、真实远端 session、浏览器账户状态或 Smart Submission 可逆性。

## 独立构建边界

构建脚本固定校验 Python 3.12 可执行文件 SHA-256 和 PyInstaller `6.21.0`，拒绝除本机 `.codex/` 状态外的任何脏 Git 源码树，先运行候选专项测试，再使用 `--onefile --console --noupx` 构建。它显式加入本科 Prompt 资产，排除 `PySide6`、`qasync`、生产 app 和生产 orchestrator 模块。

脚本只允许递归清理精确路径 `build/workflow_candidate`；清理前验证 workspace root、`build` 和 candidate root 均不是 reparse point。它不会调用 `tools/build_exe.ps1`，不会覆盖 `dist/UnifiedPaperOrchestrator.exe`，并在构建前后比较生产文件指纹。

## 已封存生产基线与回退演练

权威基线是：

- 路径：`releases/binaries/0.12.0b3/UnifiedPaperOrchestrator.exe`
- 大小：211,944,903 bytes
- SHA-256：`8378e75834a21fc56e8038c0b17d31edadf1b5c2231ed5b3f0954c5047c5d371`
- 实际 `--version`：`UnifiedPaperOrchestrator.exe 0.12.0b3`
- 回退 tag：`pre-generic-workflow-0.12.0b3-20260801`
- peeled commit：`7f33ff4ee9ccd1dc34e7e01a21a9483c73147836`

`rollback-drill.json` 是只读演练：重新运行基线 `--version`、核对基线 SHA、peel tag，并比较顶层生产 `dist` 的前后指纹。演练不会执行 `git switch`，不会将基线复制回 `dist`，也不会覆盖任何生产文件。实际回退只有在另行授权后才可执行。

## 未完成且不得误报为完成的边界

- 真实 Codex provider、真实网络与在线模型验收：`NOT_RUN`。
- 真实浏览器、期刊账户和投稿站点验收：`NOT_RUN`。
- Smart Submission 不可逆动作：`NOT_RUN`。
- 生产 GUI 接线、production feature flag 切换、原生 SCI cutover：未包含在本候选中。
- fake-provider / contract simulation 与生产执行等价：`production_equivalence=false`。

因此，候选可以用于离线验证、演示和继续迭代，但不能替代或宣称升级现有 `0.12.0b3` 生产发布。
