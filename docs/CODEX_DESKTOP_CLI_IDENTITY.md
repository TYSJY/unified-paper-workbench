# Codex Desktop 与 EXE/CLI 身份隔离

## 结论

本项目把 Codex Desktop 和 EXE 使用的 Codex CLI 视为两个完全独立的运行面。二者可以同时运行，不应共享或互相改写配置、登录态、模型参数与计费身份。

桌面端继续由 ChatGPT 应用管理，保留用户当前的 `gpt-5.6-sol`、Sol/Luna 与 `ultra` 等应用侧设置。EXE 的 Qilin 路由只在一次性 CLI 子进程内使用 `gpt-5.5` 与 CLI/API 命名空间中的 `xhigh`。项目不会把 `ultra` 全局替换为 `xhigh`，也不会尝试在两个命名空间之间做参数映射。

## 四个身份面

| 身份面 | 凭证与计费 | 当前角色 | 是否自动回退 |
|---|---|---|---|
| Codex Desktop / ChatGPT | 应用管理的 ChatGPT 登录与订阅 | 开发和人工控制面；不属于 EXE 执行链 | 否 |
| `openai_review` | 独立 OpenAI API key 与 API 计费 | 可选官方 API 执行器 | 否 |
| `openai_compatible_relay_review` | 独立 Qilin key | 通用兼容接口；已观察到 Codex 专用组可能拒绝通用 SDK | 否 |
| `qilin_codex_cli_review` | 独立 Qilin key，经原生 Codex CLI 发出 | 当前 Qilin/Codex 组的首选执行器 | 否 |

ChatGPT 订阅不等于 OpenAI API 账户，也不等于 Qilin 账户。Qilin key 指纹只能证明任务执行时使用了审批过的同一串凭证，不能证明第三方实际使用的上游账户、模型或扣费主体。

## EXE 的 CLI 隔离合同

精确路由为：

```text
writing.review_round_1.qilin_codex_cli_canary
  -> qilin_codex_cli_review
  -> pinned native codex.exe
  -> https://www.qilinapi.com (Responses wire API)
```

执行前必须满足：

- CLI 是绝对路径指定的原生 `codex.exe`，不接受 `.cmd`、WindowsApps/Desktop 内置二进制或用户 `.codex` 树内的文件。
- CLI 路径、SHA-256、最低版本、Qilin key 的域分离指纹、请求模型、`xhigh`、预算、超时和第三方数据披露全部进入持久审批与幂等合同。
- 每次执行创建私有 `CODEX_HOME`、`HOME`、`USERPROFILE`、`APPDATA`、`LOCALAPPDATA`、`TEMP` 和 `TMP`，任务结束后清理。
- 子进程使用 `--ignore-user-config`、`--ignore-rules`、`--ephemeral`、只读 sandbox 和零交互审批。
- 子进程不继承 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、父进程 `CODEX_HOME`、ChatGPT/Desktop 模型设置或通用代理变量。
- custom provider 使用 `UNIFIED_RELAY_API_KEY`、`requires_openai_auth=false`，因此不会读取 Desktop 的 `auth.json`。
- shell、浏览器、应用、插件、MCP、图片、computer use、多代理、依赖自动安装和遥测均关闭。
- 不自动重试，也不在失败后改走官方 API、通用 relay、Desktop 或其他执行器。

官方 Codex CLI 文档说明，`--ignore-user-config` 不加载用户 `config.toml`；custom provider 可从专用环境变量读取 key，且 `requires_openai_auth` 默认为 `false`。`model_reasoning_effort` 的官方枚举包含 `xhigh`，但是否支持仍取决于模型。因此，`xhigh` 只属于这个 CLI/API 任务，不代表或修改 Desktop 的 `ultra`。

## 审计口径

CLI 的 JSONL 事件会返回 Codex thread ID，但不会证明 Qilin 的上游 Responses response ID 或实际模型身份。V3 receipt 因而明确记录：

```text
requested_model = gpt-5.5
reported_model = ""
model_identity_assurance = relay_claim_unverified
provider_external_ref_kind = codex_thread_id
provider_response_id = ""
chatgpt_subscription_used = false
openai_api_account_used = false
official_openai_endpoint_proven = false
upstream_model_identity_verified = false
```

CLI 0.125 的 JSONL usage 若没有 `total_tokens`，项目按 `input_tokens + output_tokens` 派生，并记录 `usage_total_source=input_plus_output_v1`；`cached_input_tokens` 和 `reasoning_output_tokens` 不重复相加。

Codex CLI 没有可靠的服务端 `max_output_tokens` 硬上限。任务中的输出限制是审批后的事后门禁：联网前检查预算声明是否自洽，返回后若 `output_tokens` 超限则丢弃输出并失败。它不能被描述为费用硬上限。

## 当前机器状态

独立 CLI 已安装在项目忽略的运行目录：

```text
state/v2/codex_cli/node_modules/@openai/codex-win32-x64/
  vendor/x86_64-pc-windows-msvc/codex/codex.exe
```

当前固定版本为 `codex-cli 0.125.0`。本机验证到 Authenticode 状态为 `Valid`，签名主体为 OpenAI OpCo, LLC；任务本身继续以路径、版本和 SHA-256 作为审批绑定。Codex Desktop 的 WindowsApps 二进制仍明确禁止用于该路由。

## 操作入口

重新构建 EXE 后，通过交互脚本执行一次真实、非自动重试的 canary：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\run_live_codex_relay_review_canary.ps1
```

脚本会先验证 standalone CLI 的签名、版本和哈希，再隐藏输入 Qilin key；key 必须在创建任务前进入内存，因为其指纹属于审批合同。脚本显示完整审批意图，只有输入 `APPROVE` 后才会单次派发。脚本不会读取或写入全局 `.codex/config.toml`、`auth.json`，也不依赖 CC Switch。

## 尚未完成的发布门槛

- 使用最终重建的 EXE 完成一次真实 Qilin CLI canary，并由 `verify_release_044.py` 验证 V3 receipt。
- 为 Windows CLI runner 增加 Job Object 进程树终止；当前工具全部关闭，超时会终止直接 CLI 进程，但进程树证明仍需加强。
- 若以后让核心执行器直接声称“官方发行二进制”，需把 Authenticode 证书状态和证书指纹也纳入任务 envelope；当前核心审计口径仅声明“操作者批准的路径、版本和 SHA-256”。
