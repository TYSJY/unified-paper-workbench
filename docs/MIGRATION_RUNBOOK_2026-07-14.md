# 大一统项目跨电脑迁移运行手册

> 适用于 0.5.7 当前架构。执行前先阅读 [项目全景梳理](PROJECT_PANORAMA_2026-07-14.md)。  
> 原则：源机冻结、加密双份备份、目标机重建身份、单机单 owner、逐级验收。

## 0. 当前阻断项

在以下事项完成前，不切换生产：

- [ ] 当前完整工作树（含全部 untracked；不要依赖动态文件数）已进入迁移基线或不可变快照；
- [ ] 四个外部项目和 `state/**` 已纳入离线 manifest；
- [ ] 写作 SDK `waiting_event_count` 从当前 1 完成对账，或有明确人工恢复记录；
- [ ] 所有 scheduler/worker/Chrome CDP/SQLite writer 已停止；
- [ ] 目标机能复刻 `D:\Documents\...` 路径，或代码已完成路径参数化；
- [ ] 所有密码/API key/Codex/Chrome 身份已有安全重建方案；
- [ ] 0.5.7 verifier 已修复，或已批准使用本手册的替代验收集合。

## 1. 源机冻结

### 1.1 暂停调度

```powershell
Set-Location -LiteralPath 'D:\Documents\大一统'
python -m unified_paper_orchestrator pause-schedulers
python -m unified_paper_orchestrator scheduler-status
python -m unified_paper_orchestrator writing-sdk-status
python -m unified_paper_orchestrator gateway-status
```

接受标准：

- 图表、选刊作者、稳定投稿均为 `PAUSED`；
- 写作 `active_count/running_count/starting_count/creating_thread_count/recovery_count` 全为 0；
- `waiting_event_count` 为 0；
- V2 无 `queued/running/waiting_approval`；
- Smart `online_queue`、running workers 为 0。

如果 `waiting_event_count` 非 0，不删除 journal、不重建 thread。先在源机按现有恢复接口对账，并保存恢复结果。

### 1.2 关闭写入者

- 退出统一 EXE、所有相关 Python/Codex SDK 进程；
- 关闭 Codex Desktop 中相关执行任务；
- 关闭用于 Smart Submission 的 Chrome/CDP；
- 确认没有 `pdflatex`、Node worker 或投稿脚本运行；
- 停止后再复制 SQLite 主文件、WAL、SHM 和 JSON journal。

### 1.3 记录冻结状态

```powershell
python -m unified_paper_orchestrator doctor > .\state\migration_doctor_20260714.json
python -m unified_paper_orchestrator scheduler-status > .\state\migration_scheduler_status_20260714.json
python -m unified_paper_orchestrator ai-execution-status > .\state\migration_ai_status_20260714.json
python -m unified_paper_orchestrator gateway-status > .\state\migration_gateway_status_20260714.json
git status --short --branch > .\state\migration_git_status_20260714.txt
```

这些文件可能包含本地路径和任务元数据，只进入加密迁移介质，不提交公共仓库。

## 2. 迁移清单

### 2.1 必须进入精简运行包

```text
D:\Documents\大一统\
  README.md, pyproject.toml, prompts/, docs/, tools/
  unified_paper_orchestrator/, unified_paper_orchestrator_v2/, tests/
  state/unified_state.json
  state/writing_sdk_dispatch.json
  state/writing_parallel_dispatch.json（若仍使用兼容链）
  state/smart_submission/ 的状态、audit、receipt、确认材料
  state/v2/orchestrator.sqlite3 及同批 staging/receipt
  state/backups/ 中与当前状态相关的恢复证据
  dist/UnifiedPaperOrchestrator.exe（回滚副本）

D:\Documents\项目控制平台\edge-llm-research\
  controller/, ideas/, papers/, prompts/, refs/, README.md, research_profile.md

D:\Documents\论文画图\paper-figure-codex\
  controller/, papers/, prompts/, refs/, README.md, AGENTS.md

D:\Documents\期刊选刊与作者\
  controller/, papers/, prompts/, author_info.md, 投稿包总览.*

D:\Documents\New project 11\
  controller/, 正式论文/投稿包目录, 自动投稿skill/, tools/, 配置说明
```

### 2.2 只进入加密冷备，不作为目标机可用身份

- `state/codex_sdk_home/auth.json`、sessions、logs 和 SQLite；
- `state/ai_providers/secrets.local.json`；
- `account_vault.local.json`、legacy DPAPI vault、browser evidence keys；
- `.chrome-*` profiles、cookies、邮箱/投稿站点会话；
- Smart screenshots/page_snapshots/原始审计；
- 外部项目巨大但无提交的 `.git/objects`；
- archive/deleted/旧迁移包；
- `D:\Documents\大一统_backups\` 独立历史快照；去重后仅把确需运行期恢复的部分派生到精简运行包。

### 2.3 目标机重建

- `node_modules/`；
- `state/v2/codex_cli/node_modules/`；
- `.pytest_cache/`、`.ruff_cache/`、`__pycache__/`；
- build/tmp/可再生成预览；
- Codex plugin/cache；
- Chrome profile 和所有登录；
- Codex SDK/Desktop 登录；
- API key 和投稿密码；
- Codex Automations 注册与 receipt。

## 3. 制作两份备份

推荐使用 BitLocker 加密移动盘。不要用未加密 ZIP、普通网盘、聊天或邮件传送。

### 3.1 全量冷备

保留五个项目和独立备份根的原始目录结构，包括隐藏文件。建议用 `robocopy /E /COPY:DAT /DCOPY:DAT /XJ` 逐根复制到只增不删的新目录；不要在备份命令中使用 `/MIR`，以免误删目标端既有文件。逐根记录 `$LASTEXITCODE`，任何失败都先停止处理；复制完成后再用第 3.3 节 manifest 做只读差异验证。

### 3.2 精简运行包

从全量冷备派生，明确排除可重建依赖、cache、Chrome profiles 和巨大无提交 Git 对象。不要直接在源工作目录里删除这些内容。

### 3.3 生成关键 hash manifest

至少覆盖：

- 当前工作树所有源码、测试、文档和 Prompt；
- 20 份用户 Prompt；
- 四个外部 state JSON；
- 统一 state、写作 SDK journal、Smart state；
- V2 SQLite 与成功 staging/receipt；
- 正式论文/图/投稿包；
- 当前 EXE。

示例：

```powershell
$roots = @(
  'D:\Documents\大一统',
  'D:\Documents\项目控制平台\edge-llm-research',
  'D:\Documents\论文画图\paper-figure-codex',
  'D:\Documents\期刊选刊与作者',
  'D:\Documents\New project 11',
  'D:\Documents\大一统_backups'
)

$manifest = foreach ($root in $roots) {
  Get-ChildItem -LiteralPath $root -Recurse -File -Force |
    Where-Object { $_.FullName -notmatch '\\(node_modules|__pycache__|\.pytest_cache|\.ruff_cache|build)\\' } |
    ForEach-Object {
      $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
      [PSCustomObject]@{
        Root = $root
        RelativePath = $_.FullName.Substring($root.Length).TrimStart('\')
        Length = $_.Length
        LastWriteTimeUtc = $_.LastWriteTimeUtc.ToString('o')
        SHA256 = $hash.Hash
      }
    }
}

$manifest | Export-Csv -LiteralPath 'E:\UPO-MIGRATION\manifest-20260714.csv' -NoTypeInformation -Encoding UTF8
```

在 20+ GiB 数据上计算 hash 会耗时；运行期间保持系统冻结。

## 4. 目标机环境

### 4.1 推荐版本基线

| 组件 | 源机已验证版本/状态 |
|---|---|
| Windows | x64，支持 DPAPI、PowerShell、Tkinter |
| Python | 3.12.10 |
| OpenAI Python | 2.45.0 |
| openai-codex | 0.1.0b3 |
| openai-codex-cli-bin | 0.137.0a4 |
| pytest | 9.0.3 |
| PyInstaller | 6.21.0 |
| Node | 24.15.0 |
| Chrome | 149.0.7827.199（目标可用受支持稳定版，需重新验证 CDP） |
| LaTeX | MiKTeX + `pdflatex` 在 PATH |
| 其他 | Git、7-Zip、Codex Desktop |

### 4.2 恢复目录

优先原样恢复到：

```text
D:\Documents\大一统
D:\Documents\项目控制平台\edge-llm-research
D:\Documents\论文画图\paper-figure-codex
D:\Documents\期刊选刊与作者
D:\Documents\New project 11
D:\Documents\dayi_unified -> D:\Documents\大一统
```

最后一行是 Smart Submission 当前硬编码依赖的专用 ASCII 兼容 junction；未完成路径参数化前需要原样重建。不要把 junction 扩展成通用迁移方案，其他受安全门保护的路径会拒绝 reparse point。未完成路径参数化前，也不要把旧根改到 C 盘或用户目录。

仅在目标路径不存在、且 `D:\Documents\大一统` 已完整恢复并核验后创建：

```powershell
$link = 'D:\Documents\dayi_unified'
$target = 'D:\Documents\大一统'
if (Test-Path -LiteralPath $link) { throw "Refusing to replace existing path: $link" }
New-Item -ItemType Junction -Path $link -Target $target | Out-Null
$item = Get-Item -LiteralPath $link -Force
$item | Select-Object FullName, LinkType, Target
if ($item.LinkType -ne 'Junction' -or [string]$item.Target -ne $target) {
  throw 'ASCII workspace junction verification failed.'
}
```

### 4.3 Python 环境

```powershell
Set-Location -LiteralPath 'D:\Documents\大一统'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[build]' 'ruff==0.15.11'
```

安装完成后记录 `python --version` 和 `pip freeze`。在依赖锁完成前，优先按上表精确版本复现。

当前 `tools/build_exe.ps1` 还绑定源机 `C:\Users\Administrator\...\python.exe`、解释器 hash、TEMP、APPDATA 和 PYTHONPATH。目标机首次构建前必须先参数化或生成目标机专用的受控构建配置，不能直接执行原脚本并把失败当作环境异常。

## 5. 目标机身份重建

顺序不要颠倒：

1. 登录 Codex Desktop；
2. 在应用独立 `CODEX_HOME` 中重新连接 Codex SDK 账号；
3. 重新注册图表、选刊作者、稳定投稿 Automations，并生成新的官方 registration receipt；
4. 在 UI 中重新录入 OpenAI/relay API key，并做显式连接测试；
5. 从密码管理器重新录入投稿账号密码；
6. 创建新的 Chrome profiles，逐站点登录并验证 2FA/邮箱；
7. 重新采集 browser/CDP session evidence；
8. 不把源机 DPAPI 密文解密失败当成数据损坏，它本来就绑定源 Windows 用户。

## 6. 目标机离线验收

```powershell
Set-Location -LiteralPath 'D:\Documents\大一统'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check unified_paper_orchestrator unified_paper_orchestrator_v2 tests tools
.\.venv\Scripts\python.exe -m unified_paper_orchestrator doctor
.\.venv\Scripts\python.exe -m unified_paper_orchestrator ai-execution-status
.\.venv\Scripts\python.exe -m unified_paper_orchestrator scheduler-status
.\.venv\Scripts\python.exe -m unified_paper_orchestrator gateway-status
.\.venv\Scripts\python.exe -m unified_paper_orchestrator advance
```

接受标准：

- pytest/ruff 全通过；
- doctor 无 failed；
- 四根路径、state、Prompt 可读；
- 写作账号 ready；
- 三个旧 Automation 已重新注册但保持 PAUSED；
- V2 DB 一致；旧任务保留为历史账本，不编辑、不 retry，需要继续的工作在新路径创建新 task/approval；
- `advance` 只输出 dry-run；
- manifest 中关键源码、Prompt、state、artifact hash 匹配。

当前 `tools/verify_release_044.py` 不能作为 0.5.7 的通过门，因为它固定期待 0.4.4。修复后再把新版 verifier 加入此清单。

## 7. 受控上线

1. 保持源机所有 scheduler 暂停；
2. 目标机先运行只读 sync/doctor/status；
3. 选一篇无投稿风险的论文，做一个阶段 dry-run；
4. 只启动该阶段、单篇 canary；
5. 验证目标 state、artifact、journal、备份与 UI 投影；
6. 再放开下一阶段；
7. Smart Submission 仍保持单篇并发 1；
8. 未完成终态证据核对前，不在两台机器上并行启动任何投稿 scheduler。

## 8. 回滚

出现以下任一情况立即停止目标机：

- 任务 owner/route revision 与源机不一致；
- 写作 thread 无法恢复或出现第二个 thread；
- DPAPI/账号映射异常；
- Smart/stable 都准备提交同一论文；
- V2 task 输入绝对路径漂移；
- state/SQLite/Prompt/artifact hash 不一致；
- 投稿结果不明或证据写错。

回滚动作：

1. 暂停目标机全部 scheduler/worker；
2. 保留目标机日志与 state，不反向覆盖源机；
3. 比较冻结 manifest 和目标变更；
4. 确认源机仍保持冻结快照；
5. 只有在目标机没有造成外部副作用，或外部结果已人工对账后，才恢复源机单机运行。

## 9. 迁移完成定义

- [ ] 源码、20 Prompt、四外部 state、关键 artifact hash 一致；
- [ ] 统一投影 paper 数、queue、route owner/revision 一致；
- [ ] 写作 journal 每个 paper/thread/step 已对账；
- [ ] V2 DB task/event/approval 数与 staging receipt 一致；
- [ ] Codex SDK/Desktop、API、投稿账号和 Chrome 均在目标机重新验证；
- [ ] 全量测试、doctor、status、dry-run 通过；
- [ ] 单篇单阶段 canary 通过；
- [ ] 源机保持停机回滚窗口；
- [ ] 明确宣布目标机为唯一生产运行主机。
