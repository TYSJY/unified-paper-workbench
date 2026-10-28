# 0.4.5 发布记录：双模式、通用 API 配置与执行权收口

## 结果

0.4.5 保留 V1 图形界面、既有业务功能和 20 份用户调优内部 Prompt 的原始字节，同时在同一个 EXE 中加入：

- `Codex 兼容模式（V1）` 与 `API 密钥模式` 的显式选择；
- 不绑定特定中转站的 Responses/Codex SDK 服务配置档；
- Windows 当前用户保护的独立 API Key 存储；
- 一次显式、可能计费、无工具、无重试的真实兼容性检测；
- Prompt 07 单篇论文审阅的审批、幂等、预算、结构校验和私有暂存纵切；
- 只读 `ai-execution-status` 发布诊断；
- 内置 `openai-codex==0.1.0b3` 与 `openai-codex-cli-bin==0.137.0a4`。

## 能力边界

API 密钥模式当前不是 V1 四阶段的等价替代。它只支持连接检测和 Prompt 07 私有暂存审阅；写作、画图、选刊作者、
稳定投稿及智能投稿的完整执行仍由 V1/Codex 工作流拥有。API 模式下点击这些旧执行入口会返回稳定错误，不会自动回退。
API 审阅结果不会覆盖论文目录或 V1 状态，也不会自动重试或改用另一种模式。

## 并发与旧版桥接

模式提交、四阶段 Automation 激活和 Smart Submission Automation 激活共用同一个跨进程执行模式锁。启动先取得
`legacy_codex` 租约并在锁内完成 ACTIVE 写入；切换 API 则在同一锁内重新检查所有调度状态后提交。因此两种相反的
并发顺序都不能产生 `api_key + legacy ACTIVE`。

V1 控制器只接受已经存在的旧版 primary `automation.toml`。它不推断 Codex 桌面端私有 SQLite，也不会因为目录或
thread ID 存在就生成配置。缺少桥接时启动在任何旧项目状态写入前停止；暂停则明确标记为“未能确认”，不伪装成功。

## 通用 API 配置

用户在“更多 → AI执行与API”中填写完整 API base URL、模型和可选推理等级。程序不追加或猜测 `/v1`；非本机地址
必须使用 HTTPS。协议当前固定为 Responses。代理必须显式配置，且 API Key 不进入 profile、任务信封、日志或审计。
Codex SDK 使用隔离的临时 `CODEX_HOME` 和随机环境变量名，执行后清理；它不读取或改写用户 Codex Desktop 的
`config.toml`、`auth.json`、ChatGPT 登录状态或桌面端模型设置。

## 使用顺序

1. 打开 EXE，进入“更多 → AI执行与API”。
2. 新增服务配置，完整填写供应商提供的 base URL 和模型；保存 API Key。
3. 点击“测试连接”，确认一次小额真实请求。
4. 测试通过后保存为 API 密钥模式。
5. 在写作页只选择一篇具备 Prompt 07 输入的论文，从“更多操作”运行 API 私有暂存审阅。
6. 要运行完整四阶段或智能投稿时，先切回 Codex 兼容模式（V1），并确认旧版 Automation 桥显示可用。

## 发布验收

- Python/Windows 产品版本：`0.4.5`。
- 20 份内部 Prompt：由 `tests/test_v1_prompt_contract.py` 逐字节 SHA-256 验证。
- SDK：`openai-codex==0.1.0b3`。
- 内置 runtime：`openai-codex-cli-bin==0.137.0a4`。
- EXE 路径：`dist/UnifiedPaperOrchestrator.exe`。
- 全量测试：`634 passed, 54 subtests passed`。
- 构建时间：`2026-07-12T21:12:18+08:00`。
- 文件大小：`153,670,580 bytes`。
- SHA-256：`5b39f38cb7dc406ebd1a37633ca4fe7a025df99da02d6403592dad03aba2264a`。
- 打包后验证：CLI/FileVersion/ProductVersion 均为 `0.4.5`；SDK/runtime 状态、网关帮助、非法参数和缺桥拒绝启动探针全部通过。
- 旧 0.4.4 EXE 已由成功验收的 0.4.5 唯一发布物替换。
