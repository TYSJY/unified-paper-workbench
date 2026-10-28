# 能力与限制 / Capabilities and limits

| 项目 | 当前实现 | 公开发布边界 |
|---|---|---|
| Qt Quick 桌面 GUI | 已有 Windows 本机验证 | 不是所有系统都已测试 |
| 四阶段任务管理 | 写作、图表、选刊作者、投稿 | 阶段完成不等于整篇论文全部完成 |
| 本科提示词 | 人文社科、计算机软件；题目与学校资料入口 | 学校具体格式仍需复核 |
| Word 交付 | 可编辑 DOCX、分页与 PDF 预览检查 | 当前依赖 Windows Microsoft Word |
| 期刊交付 | TeX、图表、PDF 与投稿包 | 依赖工作区模板及 LaTeX 运行环境 |
| 模型目录 | SDK 动态检测与保存校验 | 用户账号和服务实际权限决定可用性 |
| 数据核验 | 待核验数据、真实数据关口与产物验收 | 程序不能替代真实研究或证明所有结论正确 |
| 投稿工作流 | Smart Submission 适配与状态管理 | 按站点、账号、授权及操作状态分别判断 |
| 公共安装 | 源码与开发入口 | 零配置首启与公开安装包尚待完成 |

本机 `0.12.0b11` 最近一次完整回归为 1848 passed、54 subtests passed，并通过隔离 GUI 启动检查。这是具体源码快照的本机证据，不是所有环境或真实论文结果的保证。

Screenshots are rendered from the real application with fictional demo data. They do not show a real publication, completed experiment, or live model execution.
