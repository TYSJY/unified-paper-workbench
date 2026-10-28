# 迁移计划

## 阶段 1：桥接型统一总控

已实现目标：

- 新建 `unified_paper_orchestrator` 包。
- 从四个旧项目读取状态。
- 生成 `unified_state.json` 和 `统一总控状态.md`。
- 提供 Tkinter 统一界面。

验收命令：

```powershell
python -m unified_paper_orchestrator sync
python -m unified_paper_orchestrator doctor
```

## 阶段 2：统一状态模型和统一队列

已实现目标：

- 每篇论文统一展示四个阶段。
- 支持 `queue` / `clear-queue`。
- 支持从下游状态推断上游已交接，避免把外部导入论文误判为需要重写。

验收命令：

```powershell
python -m unified_paper_orchestrator queue <paper_id>
python -m unified_paper_orchestrator advance
python -m unified_paper_orchestrator clear-queue
```

## 阶段 3：Master Scheduler

已实现目标：

- `advance` 默认 dry-run。
- `advance --apply` 才写回旧项目。
- 一次只推进一个队首动作。
- `advance --apply` 使用全局锁。
- 写回旧项目 state 前自动备份。
- 支持四类写回：
  - `queue_writing`
  - `queue_figures`
  - `queue_journal_author`
  - `queue_submission`

下一步增强：

- 增加每个阶段 apply 前的更严格 preflight。
- 增加可视化审计列表，直接从 UI 打开每次备份。

## 调度器统一控制

已实现目标：

- `scheduler-status` 查看四个旧 scheduler automation。
- UI 阶段主按钮默认单活；CLI 使用 `start-stage <stage> --single-active` 明确暂停其他阶段。
- `pause-stage <stage>` 暂停一个阶段。
- `pause-schedulers` 暂停四个旧 scheduler automation。
- EXE 工具栏提供同等按钮。
- `start-schedulers` 仍保留为高级命令，但不是日常推荐入口。

## 阶段 4：代码收拢

当前已抽出的公共能力：

- JSON 原子写入：`io_utils.write_json_atomic`
- Markdown 总览生成：`UnifiedOrchestrator.render_summary`
- 状态聚合：四个 adapter
- 健康检查：`doctor`
- Dry-run 调度：`advance`

后续代码收拢顺序：

1. 先把四个旧控制器里的 JSON/queue/history 通用逻辑抽成共享模块。
2. 再把四个旧 Tkinter 窗口并入统一界面的四个 tab。
3. 最后把四个旧 scheduler prompt 变成统一 prompt 下的 stage prompt。
4. 投稿自动化保持最晚合并，因为它有账号、浏览器、邮箱和安全门，风险最高。

## EXE 打包

推荐先打包统一总控，不打包四个旧项目：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_exe.ps1
```

输出目标：

```text
dist\UnifiedPaperOrchestrator.exe
```

构建前必须先运行测试；测试通过后只清理仓库内的 `build/`、`dist/`，最终验证 `dist/` 中只有一个 `UnifiedPaperOrchestrator.exe`。

## UI 安全收敛

已落实：

- 普通阶段页移除误放的 Smart Submission 优先级/移动按钮；
- 阶段主入口默认单活，并行入口降级到高级菜单；
- 顶部紧急停止同时覆盖旧四阶段与 Smart Submission；
- Smart 手动暂停与每日监控拆成两个入口；
- 在线启动确认范围、联网行为和并发数；
- 增加单槽后台 TaskRunner、Tk 全局异常处理和 rotating log；
- `state/**`、凭据、浏览器证据和生成物全部保持本地。

## 阶段 5：P0 稳定性收敛

已落实：

- JSON 读取失败改为 fail-closed，原子写入补充文件和目录级刷盘；
- dry-run、doctor、scheduler status 全部只读，不再隐式创建或改写运行配置；
- blocked/waiting/manual 状态不再被错误重新入队，队列会跳过不可执行项；
- Smart Submission state 升级到 v2，历史不再递归保存完整 state，并限制事件与历史长度；
- Smart Submission 的公开读改写入口增加进程内可重入锁和跨进程文件锁；
- Smart Submission 状态查看改为纯只读投影；界面刷新和 CLI status 不再顺手归一化或写回运行状态；
- `submitted`、`monitoring`、`accepted` 必须有 submission ID、本地证据文件或确认 URL；
- 选中论文范围贯穿本地预检和在线入队，不再中途退化为全量；
- 默认作者、单位、声明改为保守 DRAFT，不再自动补造事实；
- 旧投稿与 Smart Submission 在统一投影层并列读取；只有可验证的稿号、确认网址或本地证据能全局锁止两条路径。
- 每篇论文新增 `execution_owner` 和 revision/CAS 路由；默认永远是 `legacy_auto`，Smart 的 blocked/waiting/monitoring 状态文字不再遮蔽稳定投稿。
- Master Scheduler 和投稿 adapter 都会核对执行通道，Smart 或人工处理论文绝不写入稳定投稿队列。

## 阶段 6：下一步架构收敛

建议按以下顺序继续，不要再向 `smart_submission.py` 追加大段功能：

1. 把 Smart 模块拆为 `state_store`、`state_machine`、`gates`、`dispatch`、`site_adapters`、`audit` 六个边界明确的包。
2. 建立只读、追加式的投稿事件模型；两套执行器仍各自保存状态，统一层只聚合来源、证据、冲突和 UI 投影，不反向双写。
3. 将 package gate 改成可组合、可单测的声明式校验器；任何缺失作者事实、声明或 fingerprint 都返回明确 blocker。
4. 给状态写入增加 revision/CAS 与崩溃注入测试，验证多进程争用、断电和半写入恢复。
5. scheduler 只消费紧凑任务信封；浏览器、邮箱和整份审计材料按需加载，继续压低 token 与上下文体积。
6. 在真正只读的影子运行期比较稳定投稿与 Smart 决策；Smart 只在通过验证的站点和任务类型上逐步开放单篇试运行。
7. 稳定投稿作为默认生产路径长期保留。除非用户未来另行明确决定，否则不存在自动下线旧投稿写路径的迁移阶段。

## 阶段 7：双轨投稿与界面收敛

已落实：

- 主界面合并为一个“投稿中心”，内部使用“稳定投稿（默认）”和“智能投稿（试运行）”两个页签；
- 智能在线入口强制逐篇显式选择，试运行阶段一次只允许一篇，并在已有其它在线任务时拒绝启动；
- 投稿路线切换使用 revision/CAS，已有稿号、在线运行、最终提交结果不明确时拒绝切换；
- 顶部账号入口合并为“账号与站点”，采用分类列表与详情区的主从管理方式；
- 新登记账号写入独立的当前用户加密库；旧 DPAPI 账号库保持只读，Smart 明文记录只作兼容显示，迁移功能在可信密码填充代理完成前保持关闭；
- 日常按钮中文化并按“常用操作 / 管理 / 更多操作”分组，技术路径和维护入口移入高级页面。

后续验收重点：

1. 为 Smart 增加真正 `persist=False` 的只读影子检查，禁止影子模式修包、写队列或导入凭据。
2. 记录两条通道的实际 token、回合数、工具调用数、人工干预次数、成功率和 p95 时间。
3. Smart 单篇试运行期间保持有效并发为 1；出现重复投稿风险、目标错误、提交结果不明、证据误写或秘密泄漏时立即暂停 Smart。
