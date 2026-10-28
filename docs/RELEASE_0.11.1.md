# 0.11.1 发布记录

## 产品切割

- 正式桌面产品只保留 Qt Quick 工作台；删除经典控制台按钮、`ui-legacy` CLI 命令以及 Qt 启动失败后的旧界面回退。
- “账号与站点”保留为独立业务窗口，不再通过经典控制台打开。
- 投稿中心不再调用 `UnifiedOrchestrator.start_stage("submission")` 旧稳定投稿路线，而是确定性准备论文后直接启动 Smart Submission 程序队列与固定 Codex SDK 会话。
- 旧稳定投稿文件和状态只用于历史投影、稿号核对与防重复判断；新版不会向其创建新任务。

## 多语言

- 工作台支持运行时切换简体中文和英文，选择写入工作区 UI 偏好并在重启后恢复。
- QML 静态文本、阶段状态、Step 文案、指标、下一动作、事件摘要、确认框和错误提示统一经过同一翻译服务。
- 语言目录位于 `unified_paper_orchestrator/ui_next/locales/`；新增完整 JSON 目录即可扩展其他语言。

## 打包与回退

- 版本升级到 0.11.1，语言目录进入 PyInstaller 正式资源。
- PyInstaller 明确排除经典 `ui.main_window`，但保留独立账号与站点管理器。
- 改动前正式 EXE 备份在 `releases/binaries/0.11.0/`，对应源码快照在 `releases/source-backups/0.11.0-i18n-cutover-prechange-20260723/`。
- 本次没有修改写作、画图、选刊作者或智能投稿阶段内部提示词。
