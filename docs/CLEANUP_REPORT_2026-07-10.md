# 项目收敛与清理报告（2026-07-10）

## 结果摘要

本轮在所有 scheduler automation 保持 `PAUSED`、没有调度进程运行的条件下完成。没有触发浏览器、邮箱或在线投稿，也没有修改四个外部项目的论文内容。

- 工作区有效内容从盘点时约 1.85 GiB 收敛到 251.91 MiB（含最终 EXE，不计 `.git`）。
- Codex 恢复检查点从 6.58 GiB 松散 Git 对象无损压缩到 169.31 MiB；64 个检查点引用全部保留，`git fsck` 通过。
- Smart state 从 690,169 B 降到 243,278 B，减少 64.75%。
- 最终根目录只保留：源码、文档、prompt、测试、工具、`state/` 和唯一 `dist/` EXE。

## 备份与回滚点

执行删除或迁移前已建立并校验 SHA256：

- `state/backups/20260710T150517_pre_cleanup/`：统一状态、Smart 状态、凭据、站点 registry、scheduler prompt 和五份 automation 配置。
- `state/backups/20260710T152719_pre_smart_v2/`：Smart v2 迁移前状态、prompt、凭据、站点 registry 和 Smart automation。
- canonical prompt 重建前还在 Smart 运行目录生成了时间戳 `.bak`；其 SHA256 与旧 prompt 完全一致。

凭据只做本地备份和哈希等值验证，没有把内容输出到日志、文档或 Git。

## 删除的内容

只删除可重建、失败遗留或明确重复的内容：

- 旧 `build/`、`dist/` 和 24 个根目录 PyInstaller `.spec`；
- `work/cdp-tools/node_modules/`；
- 5 个失败遗留的 `smart_submission_state.json.*.tmp`，合计约 793 MiB；
- Python、pytest、ruff 缓存；
- 空的 `.agents/`、`.codex/`；
- 两份字节完全相同的临时 PDF 中的一份。

构建验证后重新生成了唯一 `dist/UnifiedPaperOrchestrator.exe`，并再次删除 `build/` 中间目录。

## 归档而非删除的内容

无法证明无价值的内容全部移入 `state/`：

- 40 份 Smart legacy `.bak` → `state/backups/smart_submission_legacy_20260705/`；
- 顶层误生成的 `JOURNAL_AUTHOR_ROOT/` → `state/archive/20260710_cleanup/orphan_JOURNAL_AUTHOR_ROOT/`；
- 一次性 `work/` → `state/archive/20260710_cleanup/one_off_work/`；
- 培训 PPT、图片与预览输出 → `state/archive/20260710_cleanup/project_outputs/`；
- 保留的一份临时 PDF → `state/archive/20260710_cleanup/tmp_forelease.pdf`。

现有 Smart screenshots、page snapshots、audit 和提交证据没有清空；它们仍约占 Smart 目录 197.84 MiB，因为这些文件可能用于证明投稿状态。

## 已完成的 P0 修复

### 状态与调度

- 损坏或不可读 JSON 改为 fail-closed；原子写入增加刷盘和异常清理。
- dry-run、doctor、scheduler status 改为只读。
- blocked/waiting/manual 不再误回队；队首不可执行时可以继续找后续可执行动作。
- 终态论文从统一队列投影移除，`last_scheduler_result` 不再被 sync 冲掉。
- 稳定投稿与 Smart 状态在 projection 层并列；可验证终态证据全局锁止，没有证据时由逐篇执行路线决定，默认稳定投稿。

### Smart Submission

- state v2 禁止 history 嵌入完整 state；最多 500 条、单条最多 16 KiB。
- 所有公开状态读改写入口增加进程内可重入锁与跨进程文件锁。
- 状态查看和 UI 刷新改为只读投影，不再因查看而隐式归一化并写回 Smart state。
- 选中论文的 `selected_ids` 贯穿导入、预检、恢复和在线入队。
- `submitted`、`monitoring`、`accepted` 必须有 submission ID、本地证据或确认 URL。
- 删除硬编码作者、邮箱、单位、国家和虚构 CRediT；资料不足时明确 blocked。
- 自动 hard-stop retarget 默认并迁移为关闭。
- scheduler prompt 升级为 canonical v2；漂移时先备份再整体重建。
- 手动暂停和每日监控分成两个 API；暂停不再 requeue 或快速唤醒。
- CLI 只输出紧凑结果，不再打印完整 state/history。

真实迁移校验结果：论文 ID 集合、每篇状态、paper queue、online queue、commands 和 mode 全部保持不变；history 中 `state` 键为 0，最大事件 4,288 B。

### UI、作者与仓库安全

- 阶段主按钮默认单活，并行入口降级为高级操作。
- 紧急停止覆盖四个旧 scheduler 与 Smart automation。
- 投稿入口合并为“投稿中心”，稳定投稿标为默认，Smart 标为试运行。
- Smart 危险在线入口强制单篇显式选择，并拒绝与其它在线任务并行启动。
- 账号和站点入口合并为一个分页管理窗口；旧 DPAPI 加密库继续作为主库。
- Smart 长任务移到单槽后台 `TaskRunner`；Tk 异常进入轮转日志。
- 默认作者表单改为 DRAFT，作者、邮箱、单位或声明缺失时不能 READY。
- `state/**`、凭据、截图、证据、构建产物和 node_modules 均被 `.gitignore` 排除。

## 验证记录

- `pytest`：57 passed，另有 12 个子测试通过。
- Ruff：passed。
- Python `compileall`：passed。
- PowerShell 构建/清理脚本语法：passed。
- `doctor`：`PASS`，`read_only=true`；运行前后统一状态、Smart 状态和四个外部 state 的 SHA256 均不变。
- Smart canonical prompt：版本、必需安全章节和全文一致性校验通过。
- 六个相关 automation：迁移前后均为 `PAUSED`。
- EXE：唯一文件，12,546,931 B，SHA256 `07F047ABD307661D19A865CCF5B2BC186879828E22D6455D06E9A0FAE666873A`；包内确认包含 `prompts/UNIFIED_SCHEDULER_PROMPT.md`。

EXE 当前未做 Authenticode 签名；这不影响本机运行，但分发到其它机器前应加入代码签名与发布清单。

## 仍存在的问题与下一步

1. `smart_submission.py` 仍是超大模块。下一步应拆分 state store、状态机、gates、dispatch、site adapters 和 audit，禁止继续横向堆功能。
2. 稳定投稿与 Smart 是长期双轨执行器。统一投稿事件模型只做追加式证据索引，不反向双写，也不以自动下线稳定路径为目标。
3. 逐篇投稿路线已加入 revision/CAS；状态核心仍缺崩溃注入和真实多进程争用集成测试。
4. package gate 仍有大量过程式判断，应改为声明式 validator，并为每个 blocker 建固定错误码。
5. token 控制应继续推进：scheduler 只接收紧凑任务信封，浏览器、邮箱和审计正文按需加载；manual 模式不做无效 heartbeat。
6. 为 EXE 增加隔离运行 smoke test、代码签名、版本化发布 manifest 和可回滚升级流程。

详细阶段顺序见 `docs/MIGRATION_PLAN.md`。
