# V1 原地换芯兼容合同

## 产品边界

V1 的 Tk UI、按钮、名称、交互流程、正式 EXE 入口和用户调优内部 Prompt 是长期产品外壳。
新内核必须隐藏在现有 Python 接口之后，不发展第二套面向用户的 V2 UI。

## 硬冻结项

- `UnifiedOrchestrator` 被 UI/CLI 使用的方法签名和返回结构；
- UI 当前仍直接使用的 `_plan_for` 兼容 shim；
- `adapters` 的四个键及 journal-author 模板辅助方法；
- Smart Submission 被 UI/CLI 使用的函数、常量、错误结构和机器错误码；
- `unified_state.json`、Smart state 和 `smart_submission_status()` 中被 UI 消费的字段；
- 窗口标题、工具栏、页签、主要按钮、表格列和技术信息默认隐藏行为；
- 写作 10、画图 6、选刊作者 4，共 20 份内部 Prompt 的文件名和 SHA256。

合同测试只冻结外部可观察行为，不冻结源码文件位置、内部 helper、类实现或新增字段。

## 迁移模式

每个动作独立选择一种后端模式：

- `legacy`：仅执行当前 V1 实现；
- `shadow`：V1 是唯一写入者，新内核只读计算并比较，不执行外部副作用；
- `new`：新内核是唯一写入者，并生成 V1 兼容投影。

一个动作不得由 V1 JSON 和新数据库同时拥有写权。Smart/浏览器/邮箱/投稿等有外部副作用的动作禁止
双执行；它们只能先做只读 shadow，再显式切换唯一执行 owner。

`unified_paper_orchestrator.backend_router.BackendRouter` 已提供这条内部兼容缝：

- 当前 `UnifiedOrchestrator` 总是以 `legacy_only()` 启动；
- 没有配置的已登记动作默认走 `legacy`；配置拼写错误、未知动作和未解锁的 `new` 都会在运行前拒绝，两边均不执行；
- `shadow` 和 `new` 使用两个独立入口，不能把真实新执行器误当成影子实现运行；
- `shadow` 只接受禁止网络、写目录和外部副作用的纯预览，并对输入做递归只读冻结；
- `shadow` 只记录是否一致和错误类型，不保存正文、凭据或可字典攻击的敏感值指纹；
- `new` 需要动作路由和进程级开关同时明确启用，并且启动后失败不会再调用旧实现，避免重复写入。

首个接缝是纯计算动作 `_plan_for`：V2 的 `read_models.plan_for_snapshot()` 不导入数据库、执行器、浏览器、
网络、文件系统或时钟。正式启动仍走 `legacy`；自动测试分别验证 shadow 等价和显式解锁后的唯一 new 路径。
所有有写入或外部副作用的生产动作仍保持纯 `legacy`。

## Prompt 规则

内部 Prompt 不因 API、数据库或执行器迁移而修改。新增的工具权限、输入 artifact、预算、超时、审批、
输出 schema 和完成 Gate 必须放在 Prompt 外层任务合同中。

统一 Scheduler Prompt、四个阶段 Scheduler Prompt 和 Smart Scheduler Prompt 已进入冻结备份，但它们属于
待由确定性调度替代的控制面，不纳入 20 份内容 Prompt 的永久字节级硬门。

## 发布门

任何原地换芯版本必须同时通过：

1. V1 公共接口合同；
2. V1 状态结构合同；
3. V1 UI 呈现合同；
4. 20/20 内部 Prompt 哈希合同；
5. 全量测试、Ruff、compileall 和打包 EXE 冒烟；
6. `legacy` 一键回滚验证；
7. Smart 与稳定投稿无重复副作用验证。
