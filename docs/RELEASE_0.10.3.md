# Unified Paper Orchestrator 0.10.3

本版本完成 Springer 智能投稿从本地预检、登录、草稿复用、文件上传到最终 Review 页的闭环，并修复最终确认状态在 SDK、任务账本和 UI 之间不一致的问题。

## 主要变化

- 默认作者模板加入固定作者贡献声明（发布文档已使用匿名示例）：`The corresponding author conceived and designed the study, conducted the experiments and analysis, and wrote and revised the manuscript.` 后续新论文自动继承，现有论文仍可单独修改。
- 修正作者单位解析与默认资料，确保作者、单位、城市和邮编可稳定进入投稿载荷。
- Springer 流程复用论文已有草稿和每篇论文固定 SDK 会话，确定性准备上传包并校验六个图表 PDF 已上传。
- 修复已认证投稿页被作者邮箱误判为登录页的问题。
- 修复页脚 Terms 链接以及 Cookie/Privacy 控件被误判为必须人工确认条款的问题；只有明确的同意、接受或授权控件才建立条款卡点。
- SDK 完成 Authors、Declarations 等页面后可稳定停在 Review 页；`Submit manuscript` 不会被普通 SDK 工具点击。
- 统一 `SMART_FINAL_SUBMIT_CONFIRMATION_REQUIRED` 标记、精确尝试状态与 UI 显示，任务会明确呈现“等待最终提交确认”。
- 新的成功 Turn 会清除旧的 SDK 错误字段，历史失败不会覆盖当前成功状态。

## 当前验证结果

- 真实论文 `2026-07-16_002_kv-churn-speculation` 已到达 Springer Review 页。
- 投稿内容、作者、单位、作者贡献声明、声明信息和上传文件均已进入 Review。
- 最终投稿按钮尚未点击，必须由用户针对当前精确尝试再次明确授权。
- 完整自动化测试通过：1049 个测试及 54 个子测试。

## 不变边界

- 写作、画图和选刊作者阶段的内部提示词未改动。
- 密码仍由本机可信凭据代理填写，不进入 SDK Prompt、日志或任务状态。
- 最终投稿继续使用一次性、精确尝试绑定的人工确认门禁。
