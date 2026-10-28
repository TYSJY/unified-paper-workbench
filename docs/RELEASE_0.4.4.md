# 0.4.4 发布记录：快速多执行器网关

发布日期：2026-07-12

当前状态：第三方兼容中转纵向切片正在并入最终候选；此前记录的测试数量、EXE 时间/大小/哈希和“验收已完成”结论已经过期。最终全量测试、重建、EXE 验收与真实联网 canary 均须刷新，因此当前发布完成度为 `INCOMPLETE`。

## 交付结论

0.4.4 在保留 V1 图形界面、现有功能和 20 份用户调优内部 Prompt 原字节的前提下，加入了同进程
`ExecutionGateway`。官方纵向切片 `writing.review_round_1.canary -> openai_review` 保持固定官方端点与既有合同不变；另增
`writing.review_round_1.compatible_relay_canary -> openai_compatible_relay_review`，固定接入麒麟第三方 Responses-compatible endpoint。两条路径都只允许生成私有 staging 评审，不会写回论文、旧项目状态、稳定投稿或 Smart 投稿。

中转路径是独立 action/executor，不是官方执行器的可变 base URL，也不会接管、改写或重试官方任务。它要求显式第三方数据披露确认和持久的完整意图审批，并把 provider assurance 固定为 `third_party_claim_unverified`；第三方接口兼容性和上游模型身份不能被表述为 OpenAI 官方已验证。

本版不新增第二套 UI，不启动后台 worker，不自动重试，也不改变稳定投稿和 Smart 投稿的生产执行 owner。
唯一 canonical commit primitive/adapter 已安装并完成隔离测试，但默认未授权、产品 route 默认关闭，canary 不会自动提升为正式产物。

当前唯一正式写入者仍是 V1 既有工作流。`BackendRouter` 默认为 `legacy_only`，每个动作只允许一个 authoritative
backend；0.4.4 默认网关的 `canonical_writes_enabled=false`、`canonical_route_enabled=false`，因此不会与 V1 双写。
后续仍缺的是让 V1 与新 writer 共享同一写入协议并完成生产 owner 迁移，而不是 commit primitive 本身。

## 核心变更

- 复用 V2 `TaskEnvelope 2.0`、SQLite WAL `EventStore`、`WorkflowKernel` 和 executor registry。
- 新增显式网关 CLI：`gateway-status`、`gateway-review-canary`、`gateway-relay-review-canary`、`gateway-dispatch`、`gateway-task`、
  `gateway-retry`、`gateway-approve`、`gateway-legacy-automation`、`gateway-recover-review`、
  `gateway-recover-automation`。
- `legacy_codex` 真实包装现有 `SchedulerAutomationController`：支持只读 status，以及需 operation ID、持久审批和独立 control latch 的 start/pause；`legacy.apply` 永久拒绝，论文/投稿业务 owner 不变。
- 新增 `dispatch_owner=kernel|gateway`，在 claim、审批、取消、重试和终态写入时原子隔离；实验 API/CLI 不能创建或变更 gateway-owned 任务。
- 新增默认关闭的 `CanonicalCommitter`/`CanonicalCommitExecutor`：完整意图 SHA256 审批、唯一 staging 绑定、canonical/staging/control 根隔离、Windows symlink/junction 防护、目标 CAS、跨进程锁、pre-image 备份、原子替换、hash-only audit、事务日志恢复及 `fallback_allowed=false`。
- 固定 `openai==2.45.0`；两个审阅请求都会把 Prompt 07 的内存渲染结果作为 `instructions`，把五类论文文件作为固定角色
  `input`，并使用 `store=False`、零 tools、`max_retries=0`。
- 官方 `openai_review` 仍把 base URL 固定为 `https://api.openai.com/v1`，HTTP client 使用 `trust_env=False`，不继承 `OPENAI_BASE_URL` 或通用环境代理。直连不可用时，只接受独立配置的无凭据 HTTP loopback 代理；远程/带凭据/附加路径配置 fail closed，且不把代理 URL 写入账本。
- 独立 `openai_compatible_relay_review` 把 base URL 固定为 `https://www.qilinapi.com`，使用独立的 `UNIFIED_RELAY_API_KEY`、`UNIFIED_RELAY_MODEL` 和可选的受限 loopback proxy；它不读取官方 OpenAI 或 Codex 的 key/model/base 配置。key 仅存在于交互脚本及子进程内存，不进入磁盘、任务信封或审计。
- 中转 payload/approval 绑定 `provider_kind`、`provider_profile`、`provider_assurance`、`wire_api`、固定 endpoint digest 与 `third_party_data_disclosure_ack`；缺少披露确认、完整 intent SHA256 审批或精确 action/executor 绑定即 fail closed。
- 通用 `openai_api` executor 永久返回 `OPENAI_GENERIC_ROUTE_DISABLED`；官方与中转都只能经各自 action-specific 网关 route 调用，双方不自动 fallback。
- 默认总 Token 预算 50,000、最大输出 8,000；联网前先做版本化保守估算，返回后再核对真实 usage。
- 输入在请求前后均按 SHA256 校验；正文输入与模型输出正文不进入紧凑 SQLite 账本。
- `StagingStore` 在私有临时目录完成双文件写入和校验后，才原子发布整个任务目录。
- staging 只接受标准 run/task ID，拒绝 Windows reparse/junction；每任务跨进程锁、发布前后精确 bytes/hash
  验证、manifest-task 绑定和相同 pair 幂等复用共同防止半成品与冲突覆盖。
- queued review 不接受预存 staging 快捷完成；每次 claim 生成
  `execution_attempt_token_sha256` 并与 receipt 绑定，只有 running 任务的显式 recovery 可以消费匹配 receipt。
- API timeout、连接/服务端错误和 incomplete response 等模糊结果禁止直接重排；必须显式使用
  `--acknowledge-possible-duplicate-charge` 确认可能产生第二次请求/费用，确认写入 ledger 审计。
- review/Automation/canonical 均提供显式证据对账：分别只复用完整 staging receipt、只读目标状态或
  canonical audit；恢复过程不会重复 API 调用、控制写入或正式写入。
- API 已完成但二次输入校验或 staging 失败时，仍保留 compact usage、response ID、model 和 endpoint 证据。
- Windows ACL helper 改用可信 System32 绝对路径和最小子进程环境，不向权限工具继承 API key、代理或模型变量。
- 修复 `doctor` 潜在 writing reconciliation 写入；最终 EXE 的 `doctor` 已在 2,223 个观测文件上验证零变化。

## 真实 fixture 离线回放

fixture：`2026-07-02_001_reconfiguration-debt-decoding`

- Prompt 07 SHA256：`FAF7E03DC571FD7EAF5FF0DCEC38E30FCFD8CFA732434372384378CB1EE13830`
- 历史评审/回放输出 SHA256：`2819F57CBA1E2392666299DBFE9D89FB1F5BA63456F23439F47B8649399C7156`
- 历史评审只作为 injected fake response output；未进入请求，未进入 SQLite 正文。
- 7 个源文件前后 SHA256 全部一致。
- 只生成 `review_round_1.md` 与 `result.json`；`canonical_write=false`。
- 当前保守输入估算 37,487 Token；加 8,000 最大输出后为 45,487，低于 50,000 总闸门。
- `evidence_scope=tex_source_only_no_rendered_figures`：本 canary 验证 TeX 级审阅接缝，不代表图像视觉质量已验收。

离线 fixture 不是联网成功证据。官方与中转 profile 都必须以真实 response JSON 中的非空 ID、正数 usage、validator、输入未变和 staging receipt/task claim token 绑定来验收；手写 JSON、injected response 或第三方配置页不能替代。中转成功也只能证明本项目到固定兼容端点的合同完成，不能证明官方 OpenAI endpoint 或第三方所称上游模型身份。

## 回归与发布物（待最终刷新）

加入中转 action/executor 后，先前构建的 `dist/UnifiedPaperOrchestrator.exe` 不再对应当前源码；此前文档中的测试数量、构建时间、文件大小和 SHA256 均不得继续作为 0.4.4 最终发布证据。本节不预填未知最终数字。

最终候选必须重新取得并记录以下证据：

- 全量测试、Ruff、Python compileall 与全部 PowerShell 脚本解析通过，并记录实际最终统计。
- V1 守卫重新确认 20/20 内部 Prompt 与 frozen baseline 字节级一致，V1 UI/关键业务及稳定/Smart 投稿 route owner 零漂移。
- 官方 `openai_review` 的既有路由、固定端点和失败语义不因中转接入发生变化。
- 中转 action/executor 的审批、披露确认、endpoint/provider binding、幂等、模糊重试、staging/recovery 和 key 隔离测试通过。
- 离线 fixture 回放继续保持源文件不变、只生成两个 private staging 文件；但它不冒充联网 canary。
- 最终 EXE 重新构建后通过 `doctor` 只读性、CLI/version、Windows version resource、单文件 `dist/`、候选烟雾测试与 verifier 新鲜度检查。
- 最终真实 canary 取得真实 response JSON ID 与正数 usage，并通过所选官方或中转 provider profile 的交叉绑定；中转 profile 必须保留“官方 endpoint 未证明、上游模型身份未验证”的结论。
- 最终再填写 EXE 的构建时间、大小、SHA256、测试统计及 verifier 结果；刷新前不得称为最终发布物。

冻结的 V1 基线仍保留在：

`D:\Documents\大一统_backups\V1_FROZEN_BASELINE_0.4.2_20260712T002900`

## 使用边界

```powershell
python -m unified_paper_orchestrator gateway-status
$env:OPENAI_API_KEY = "<仅在当前终端设置>"
$env:OPENAI_MODEL = "<明确批准的模型 ID>"
python -m unified_paper_orchestrator gateway-review-canary "<paper_id>"
python -m unified_paper_orchestrator gateway-review-canary "<paper_id>" --execute
python -m unified_paper_orchestrator gateway-relay-review-canary "<paper_id>" --model "<relay-model-id>"
```

更安全的官方/中转交互式入口不会回显或落盘 API key：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\run_live_review_canary.ps1
powershell -ExecutionPolicy Bypass -File .\tools\run_live_relay_review_canary.ps1
python .\tools\verify_release_044.py
```

脚本只在自身进程内临时设置各自 key/model，不读取 Codex `config.toml`/`auth.json`，也不把 key 写入磁盘、命令行参数、任务信封、staging 或审计；运行独立 verifier 前强制清除 key，最后才恢复原进程环境，并清零转换用 BSTR 缓冲区。

不要把 API key 写入项目文件、任务信封、命令历史或聊天。官方执行与获批后的中转执行都会把 Prompt 07 渲染正文作为 `instructions`，并把指定论文的 idea、references、literature map、novelty check 和 `draft_v3.tex` 作为固定角色 `input`。20 份内部 Prompt 源文件不改，但中转 route 会把上述渲染正文和论文数据发送到固定第三方 endpoint，所以任务必须先显式确认披露并批准完整 intent SHA256。

## 已知限制与下一阶段门槛

- 没有通用 worker lease；`running` 不会被后台自动接管。只有完整 staging receipt、Automation 目标状态或
  canonical audit 能提供确定性证明时，才允许显式 ledger reconcile；证据不完整时绝不重放。
- staging 到正式论文的 commit primitive 已存在，但生产授权和 owner 迁移未启用；评审不会自动成为正式 `review_round_1.md`。
- canary 没有渲染 PDF/figure 像素，只能做 TeX-level figure inventory。
- 真实联网质量、成本和响应格式仍需在用户本地配置 key/model 后完成一次显式 canary 才能下结论；response ID 与 usage 必须来自实际返回 JSON。
- 中转 canary 即使通过也只能证明固定第三方兼容合同完成；官方 endpoint 与上游模型身份仍分别为未证明、未验证。
- 最终 EXE 的 Authenticode 状态必须在重建后重新记录；若要跨机器或公开分发，代码签名仍是发布门槛。
- 仓库仍包含前序 0.4.3/架构迁移阶段的未提交改动，当前工作树不等同于 Git tag。

下一条 API action 只有在当前审阅 canary 通过，并补齐独立 adapter、validator、最小权限、shadow 对比和
生产 owner 迁移合同后才能开放。投稿执行器迁移仍不属于下一条默认纵向切片。
