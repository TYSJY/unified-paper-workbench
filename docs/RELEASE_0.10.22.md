# 0.10.22：企业级控制台与一键全流程

发布日期：2026-07-20

## 本版目标

本版以 `0.10.21` 为可恢复起始基线，先核对当前真实生产路径，再完成用户提出的八项改造。产品仍是同一个 `UnifiedPaperOrchestrator.exe`；没有另建 V3，也没有用新的 AI 调度器替换现有工作流。

完整架构、状态源、目录关系、并发模型、风险和逐项实施证据见：

- `docs/PROJECT_ARCHITECTURE_AND_REMEDIATION_2026-07-20.md`

## 主要变化

### 写作引用规则

- 只修改写作 Step 3 的生产提示词和同内容兼容副本；
- 明确规定同一个引用位置最多放 3 篇文献；
- 禁止 `[13-25]` 这类大范围引用和 `[31,33,35,46,56,67]` 这类引用堆叠；
- 没有改写画图、选刊作者或智能投稿阶段内部提示词。

### ORCID 优先认证

- 新站点且没有已有账号时，程序优先识别并选择 ORCID 登录/注册；
- ORCID 不可用时才回退站点原生注册；
- 已有站点账号继续优先登录/恢复，不会重复注册；
- CAPTCHA、2FA 和必须由本人确认的法律事实仍可转入可见人工接管。

### 智能投稿人工接管

- 智能投稿页增加单篇“人工修改”入口；
- 直接打开并聚焦该论文已经绑定的受控浏览器页面；
- 人工完成后可在同一任务上继续，不新建论文、不旋转固定 SDK 会话、不丢失投稿尝试；
- 接管状态、页面绑定和恢复动作均写入任务账本。

### 前端性能、响应式与按钮治理

- 慢速读取和维护操作移出 Tk 主线程；
- 实时任务表改为增量更新，保留选中项、焦点和滚动位置；
- 常用操作区按窗口宽度自动换行，窄屏保留水平滚动；
- 移除或收拢重复入口，按钮文字明确作用对象和暂停边界；
- 账号站点、设置和状态窗口改为后台加载、主线程创建控件。

### 无必要 CMD 窗口

- GUI 在进入 Tk 前处理继承控制台；
- Windows 后台子进程统一使用无控制台启动参数；
- EXE 使用 PyInstaller `--console --hide-console hide-early` 构建；
- 命令行入口仍保留标准输出、退出码和诊断能力。

### 企业级视觉系统

- 新增集中式语义主题，统一字体、颜色、间距、按钮、表格、页签、滚动条和状态色；
- 保留 Windows 原生 Tk/ttk 稳定架构，避免为了外观重写生产 UI；
- 主窗口和关键设置窗口已在常规与最低支持分辨率下进行真实创建、检查和关闭。

### 一键全流程

- 主界面新增“一键全流程”，可输入本次新建和期望并发数量 1–10；
- 程序状态机依次交接：写作 → 画图 → 选刊作者 → 智能投稿；
- 写作默认要求工作区 Step 5 处于自动信任模式；
- 各阶段继续使用原有固定 Codex SDK 会话、原有 Step 配置和原有产物契约；
- 阶段实际并发上限分别为写作 10、画图 4、选刊作者 6，Smart 使用本次数量，超出容量的论文在程序队列中等待；
- 智能投稿临时启用自动最终提交，批次结束后采用 compare-and-swap 恢复旧设置，不覆盖用户运行中手工修改的新值；
- 批次、槽位、阶段、交接 claim、重试和设置恢复均持久化；关闭并重开 EXE 可继续未完成批次；
- 单篇临时错误按 5/30/120 秒最多重试 3 次，不阻塞其他论文。

## 回归期间修复的真实问题

- 投稿状态检查遇到凭据代理安全重试时，本次只读检查现在会正确结束并回到“监控中”，不再永久占用在线队列；
- Worker 准备网页字段时会优先保留已经验证并显式选择的封面信文件，不再替换文件后使精确最终提交授权失效；
- 智能投稿路由测试样本升级为满足当前正式投稿包契约的完整样本，生产验收规则没有被削弱。
- 修正只读 `ai-execution-status` 和界面对内部兼容值 `legacy_codex` 的过期解释：当前显示为“Codex 账号模式（四阶段 V2 SDK）”，四阶段 owner 均为本地 SDK 工作流；历史 Automation 只作为暂停的回退诊断，不再决定当前模式是否可运行。

## 验证结果

- 全项目：`1229 passed, 54 subtests passed`，并在正式打包脚本中完整复跑同样结果；
- 智能投稿路由与精确授权：`41 passed`；
- 智能投稿加固：`72 passed`；
- 一键全流程专项：`9 passed`；
- 候选 EXE 的 `--version`、`--help` 和只读 `ai-execution-status` 均通过；内置 `openai-codex 0.1.0b3` 与 `codex.exe 0.137.0a4` 可用；
- 真实 UI 检查只打开并关闭控制台，没有启动论文、没有修改论文状态、没有弹出 CMD、没有遗留测试进程；
- 正式 EXE：`dist/UnifiedPaperOrchestrator.exe`；
- ProductVersion / FileVersion：`0.10.22`；
- 文件大小：`154854674` 字节；
- SHA-256：`A3692A65A594EBE847A0E49154E52849A3F854804E641B56A2E62D27D11B3A74`；
- 最终提升记录：`state/release_promotion_0.10.22.log`，2026-07-20 21:23（Asia/Taipei）。

正式只读状态确认：`active_mode_ready=true`、`full_four_stage_execution_owner=local_codex_sdk_workflows`、`remaining_v1_stage_owner=none`、`legacy_automation_role=paused_compatibility_fallback`。

## 恢复

- 本轮起始源码/配置快照：`releases/source-backups/0.10.21-goal-baseline-20260720T172013/`；
- 企业视觉改造前只读 Git 引用：`refs/codex/backups/0.10.21-enterprise-ui-prechange-20260720`；
- 一键全流程改造前只读 Git 引用：`refs/codex/backups/0.10.21-full-pipeline-prechange-20260720`；
- 一键全流程精确恢复说明：`releases/source-backups/0.10.21-full-pipeline-prechange-20260720/RESTORE.md`；
- 0.10.21 正式 EXE：`releases/exe-backups/0.10.21-pre-0.10.22-20260720T204338/UnifiedPaperOrchestrator-0.10.21.exe`；
- 最终状态语义对齐前的 0.10.22 EXE：`releases/exe-backups/0.10.22-pre-status-alignment-20260720T212000/UnifiedPaperOrchestrator-0.10.22.exe`，恢复说明在同目录 `RESTORE.md`；
- 候选在全部探针通过后才原子提升到 `dist/`，提升完成后候选文件已清理。
