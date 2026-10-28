# Smart Submission 架构与操作边界

> **0.11.1 状态更新：** Smart Submission 已成为新版投稿中心唯一可执行生产路线。本文中关于“试运行旁路”或与稳定投稿并行的描述用于解释历史状态和迁移来源；旧状态仍只读参与稿号与重复投稿核对，但旧执行器已无新版 UI、CLI 或自动回退入口。

## 定位

Smart Submission 是统一项目内的可选试运行控制面。它不替代稳定投稿项目，也没有“成熟后自动下线旧路径”的迁移目标。EXE 负责状态展示、逐篇执行通道路由和队列准备；可见 Codex scheduler 负责派发；每个 worker 只处理一篇论文的浏览器、邮箱或投稿页面动作。

## 组件

| 组件 | 职责 | 不应承担的职责 |
|---|---|---|
| Tk UI | 展示状态、确认范围、切换模式、提交后台任务 | 不在 Tk 主线程执行长预检或浏览器恢复 |
| Smart state | 保存 mode、paper queue、online queue、论文和 worker 摘要 | 不保存密码或整份浏览器/邮件正文 |
| Credential store | 本机保存投稿/邮箱账号材料 | 不进入 Git、聊天或普通日志 |
| Site registry | 保存系统与期刊入口信息 | `discovered` 不等于已验证的投稿 adapter |
| Visible scheduler | 读取队列、核对 worker、派发单论文任务 | 不在 scheduler 线程内直接投稿 |
| Visible worker | 单论文、目标锁定的浏览器/CDP/邮箱动作 | 不处理其它论文，不修改本地全局状态 |
| Audit/evidence | 保存紧凑证据与下一动作 | 不保存密码、Cookie、验证码或完整私人邮件 |

## 多来源投稿状态检查

状态巡检采用“程序编排、模型只做语义判断”的三层链路：

1. Python 从统一账号库发现并去重启用的邮箱来源；学校邮箱由本机只读连接器一次扫描最近 120 封邮件，按投稿编号或完整标题精确匹配。账号、密码和完整发件地址不会进入 SDK。
2. 同一临时只读 SDK Turn 批量处理所有待巡检论文：Gmail 只允许受限搜索；学校邮箱只提供主题、最多 600 字符短摘要、日期、发件域名和匹配依据。每篇论文必须逐来源返回结果，遗漏来源视为本轮失败。
3. Python 重新确定性聚合逐来源结果。明确的接收、退稿或返修通知才触发后续动作；无明确通知、无法判定的来源冲突或邮箱不可用时，自动转入该论文固定会话核对期刊后台。回退排队失败会落账为失败，不会误显示“检查正常”。

邮箱检查不发送、回复、转发、删除、归档或修改邮件。逐来源的可用性、结果、错误码和短摘要会写入任务账本并显示在智能投稿详情页；完整邮件正文不持久化。

## UI 模式

### 手动模式（暂停）

- Smart state 设为 `mode=manual`。
- Smart automation 明确写为 `PAUSED`。
- 不创建新的 scheduler heartbeat 或 worker 派发。
- 已经运行的 Codex worker 不会被 Windows UI 强杀；需要在对应会话中停止。

### 每日监控

- Smart state 保持 `manual`，避免点击按钮后立即执行在线队列。
- automation 恢复每日 09:00 cadence。
- heartbeat 可以联网检查状态和已登录邮箱；遇到需作者动作或安全边界时停止。

### 在线调度

这是危险入口。确认框必须在执行前显示：

- 论文范围：必须明确选中的一篇论文；
- 联网行为：投稿站点和已登录邮箱；
- 当前试运行的有效并发固定为 1；
- CAPTCHA、2FA、付款、强制 OA、ORCID、作者确认和新法律/伦理事实等停止条件。

确认后统一状态先把该论文的 `execution_owner` 切换为 `smart`、`mode` 切换为 `canary`，然后才使用带 `selected_ids` 的本地预检与在线入队路径。没有选择、多选、已有其它在线队列或已有执行任务时，UI 一律拒绝启动。

## 单活与并行

- 四个旧阶段的日常主按钮默认单活：当前阶段启动，其余三个阶段暂停。
- 并行启动只存在于“更多工具”的高级入口。
- Smart Submission 底层仍保留 `max_parallel_workers` 配置，但当前 UI 的单篇 canary 会把有效并发限制为 1。
- 顶部紧急停止覆盖旧四阶段与 Smart automation。

## UI 后台任务

`TaskRunner` 是一个单槽、daemon-thread 执行器：

1. Tk 主线程提交纯工作函数；
2. 后台线程不得调用 Tk 控件；
3. 结果通过线程安全队列返回；
4. Tk 使用 `after()` 轮询并在主线程调用成功/失败 callback；
5. 同一窗口只允许一个后台任务，避免重复点击造成并发写状态。

当前用于 Smart Submission 本地预检、本地执行和在线入队。紧急停止仍同步执行，避免排在后台长任务之后。

## 异常与日志

UI 安装三层异常记录：

- `sys.excepthook`：主线程未捕获异常；
- `threading.excepthook`：普通后台线程未捕获异常；
- `report_callback_exception`：Tk callback 异常。

日志路径为 `state/logs/unified_control_panel.log`，单文件 2 MiB，轮转备份 4 个。日志目录属于 `state/**`，不得提交。

## 仓库边界

以下内容必须保持本地：

- `state/**`；
- `credential_store.local.json`；
- screenshots、page snapshots、audit、浏览器证据；
- build/dist/node_modules 等生成物。

文档和设计决策放在 `docs/`，不要把运行 state 当成配置模板提交。

## 当前限制

- 稳定投稿与 Smart Submission 是有意长期保留的两套持久化状态源；统一 adapter 只做只读投影、证据锁止和执行通道路由，不尝试反向双写。
- 没有终态证据时，只有每篇论文显式保存的 `execution_owner` 决定有效状态；默认是 `legacy_auto`。Smart 的 blocked、waiting、monitoring 或 submitted 状态文字本身不能遮蔽稳定投稿。
- Smart Submission 的锁、历史压缩、终态证据门和选中范围已经加固；核心 state store、状态机、package gate、dispatch 和站点适配仍集中在一个大模块中，维护成本依然很高。
- 当前“导入并检查”仍会写 Smart 本地状态和审计，因此还不是真正的只读 shadow；在加入 `persist=False` 前只能称为本地预检。
- 日常状态查看现在只在内存副本上生成归一化投影，不初始化模块、不写回 state；真正的归一化只在明确的调度或维护动作中持久化。
- “暂停”可以阻止新的 heartbeat/派发，但不能可靠撤销已经由外部 Codex worker 完成的网页动作。
- 每日监控使用 canonical v6 scheduler prompt；在状态机和站点 adapter 完成拆分前，仍应保留人工监督。

## 账号与站点

- 统一窗口同时读取新可写账号库、旧稳定投稿只读 DPAPI 库和 Smart 兼容账号记录，但列表不返回密码或密文。
- 新账号只写入 `state/smart_submission/account_vault.local.json` 并使用 Windows 当前用户加密；查看密码需要显式操作，复制后 30 秒尝试清空剪贴板。
- 旧稳定投稿库不会由统一窗口修改、迁移或删除；Smart 兼容明文也不会自动迁移。
- 可信本地密码填充代理就绪前，新账号库记录仅用于安全登记，明确标注为尚不可用于自动填充；迁移入口和存储 API 均保持关闭。
- 每次写入前备份，审计不记录密码、密文、Cookie、验证码或邮件正文。

## State v2 不变量

- `history` 最多保留 500 条，每条最多 16 KiB；历史事件不能嵌入完整 `state`。
- 所有公开 Smart 状态读改写入口必须持有同一把可重入的进程内锁和跨进程文件锁。
- `auto_retarget_after_site_hard_stop_enabled` 默认并迁移为 `false`。
- `submitted`、`monitoring`、`accepted` 至少需要 submission ID、存在的本地证据文件或有效确认 URL 之一。
- 任何作者身份、单位、国家、CRediT 或法律/伦理声明都必须来自文件证据或用户确认，缺失时保持 blocker。
- scheduler prompt 必须匹配 canonical v6；发现漂移时先备份原文件，再整体重建，禁止继续堆叠字符串补丁。
