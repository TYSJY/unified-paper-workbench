# 能力与限制 / Capabilities and limits

| 项目 | 当前实现 | 公开发布边界 |
|---|---|---|
| Qt Quick 桌面 GUI | 已有 Windows 本机验证 | 不是所有系统都已测试 |
| 四阶段任务管理 | 写作、图表、选刊作者、投稿 | 阶段完成不等于整篇论文全部完成 |
| 本科提示词 | 人文社科、计算机软件；题目与学校资料入口 | 学校具体格式仍需复核 |
| Word 交付 | 可编辑 DOCX、分页与 PDF 预览检查 | 当前依赖 Windows Microsoft Word |
| 期刊交付 | TeX、图表、PDF 与投稿包 | 依赖工作区模板及 LaTeX 运行环境 |
| 模型目录 | SDK 动态检测与保存校验 | 用户账号和服务实际权限决定可用性 |
| 自定义工作流 | 模块内新增、排序、删除 Step；编辑与保存每步提示词 | 四个顶层模块固定，运行任务保留流程快照 |
| 执行参数 | 阶段默认与单步模型、推理强度、Token 上限及并发 | 上限是执行约束，不代表实际已消耗用量 |
| 内置 SDK 授权 | 浏览器授权、设备码登录与账号状态 | 用户自行完成官方授权，不共享桌面端登录凭据 |
| 账号与站点 | 邮箱、投稿系统、外部服务、站点与期刊分类；默认隐藏个人信息 | 演示记录不代表实际账号已连接；本地凭据由代理处理 |
| 数据核验 | 待核验数据、真实数据关口与产物验收 | 程序不能替代真实研究或证明所有结论正确 |
| 自动化投稿 | Smart Submission 登录、表单、上传、最终提交策略、回执核对与人工接管 | 按站点、账号、授权及操作状态分别判断，不保证所有站点可用 |
| 投稿监控 | 每日本地时间定时检查、立即检查与后续状态处理 | 依赖本地程序运行及站点可访问性 |
| 公共安装 | Windows 预览安装包、内置运行时、空工作区模板与初始化 | 已验证隔离安装与 GUI 首启，独立电脑的完整 AI 流程仍待验证 |

本机 `0.12.0b11` 最近一次完整回归为 1848 passed、54 subtests passed，并通过隔离 GUI 启动检查。这是具体源码快照的本机证据，不是所有环境或真实论文结果的保证。

主页包含真实软件的设置、提示词编辑、自定义步骤、SDK、账号站点和投稿中心截图。软件截图由隔离演示任务与虚构记录生成，未执行模型调用、账号授权或真实投稿。论文案例图片为用户提供的节选，仅展示排版与图表效果。

Reproduce the Qt feature captures with `tools/render_public_demo.py --output build/<new-directory>`. The memory-only native account fixture is `tools/render_public_accounts_demo.py`; capture its window with a desktop screenshot tool. Neither fixture opens the operator's account store or submits a manuscript.
