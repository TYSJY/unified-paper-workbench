# 0.4.6 发布记录：恢复 V1 官方 Codex Automation 执行桥

## 结果

0.4.6 恢复了 V1 四阶段的真实调度入口：写论文、画图、选刊作者和稳定投稿均重新注册为 Codex Desktop 当前支持的本地 cron Automation。现有 UI、业务状态和 20 份用户调优内部 Prompt 未修改。

API 密钥配置继续保留；当前默认模式切回 `legacy_codex`。两种模式仍严格隔离，不会在失败时自动回退。

## 防止伪启动

仅有 `~/.codex/automations/*/automation.toml` 不再足以证明 V1 可用。生产启动还要求：

- 四个 primary Automation 都是官方注册器生成的 `cron`/`local project` 配置；
- ID、项目目标、工作目录、模型和推理等级与注册收据一致；
- 存在一次有界的真实消费探针及 Automation memory 证据；
- 当前模式确实为 `legacy_codex`。

旧灾备 TOML、手工复制文件或字段漂移会 fail closed。暂停操作始终可用。

## 实机证据

写作 Automation 由 EXE 临时设为 1 分钟心跳，只处理一条既有 `pause_session` 命令。Codex Desktop 创建了对应 Automation 任务，并于 `2026-07-13T01:33:17+08:00` 将命令从 `pending` 更新为 `done`；没有创建论文、发送写作步骤或修改内部 Prompt。验证后四个调度器全部恢复为 `PAUSED`，写作心跳恢复为 5 分钟。

## 版本

- Python/Windows 产品版本：`0.4.6`。
- 发布物：`dist/UnifiedPaperOrchestrator.exe`。
