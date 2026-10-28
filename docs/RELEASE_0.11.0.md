# 0.11.0 发布记录

## 本版结果

- 默认桌面入口升级为 PySide6 / Qt Quick 工作台，经典 Tk 控制台继续作为可显式启动的回退入口。
- 新增统一品牌头部、五阶段导航、圆角渐变按钮、指标卡、状态徽标、结构化详情、技术信息折叠和响应式侧栏。
- 论文列表改为虚拟化模型；搜索、阶段切换和状态筛选只处理内存数据。
- 启动采用“缓存首屏 + 后台真实同步”，刷新和阶段操作不再阻塞 GUI 主线程。
- 修复历史状态中 `Step 4/3` 一类展示错误；步骤已执行但产物契约未通过时显示“验收未通过”，不会伪装成完成或业务阻塞。
- 切换阶段时自动选择该阶段可见论文，右侧详情与当前页面保持一致。
- 版本升级到 0.11.0，PySide6 Essentials 固定为 6.11.1，QML 资源进入正式打包流程。

## 明确未改变

- 写作、画图、选刊作者和智能投稿内部提示词未修改。
- 各阶段状态机、SDK 固定会话、并发队列、Token 配置和产物验收规则未修改。
- 论文工作区与既有文件没有迁移或重写。

## 使用入口

- 双击 `UnifiedPaperOrchestrator.exe`：现代工作台。
- `UnifiedPaperOrchestrator.exe ui`：现代工作台。
- `UnifiedPaperOrchestrator.exe ui-legacy`：经典控制台。

## 回退

上一版 EXE 位于 `releases/binaries/0.10.24/UnifiedPaperOrchestrator.exe`。源码级回退说明位于 `releases/source-backups/0.10.24-qt-ui-next-prechange-20260722/RESTORE.md`。

## 发布验收

- 完整仓库测试：1240 passed，54 subtests passed。
- Qt 工作台专项与入口测试：19 passed。
- 既有 UI 合同与 Windows 子进程测试：69 passed，1 skipped。
- QML 全量语法检查：通过。
- 正式 EXE 版本：0.11.0。
- 正式 EXE 大小：208,396,564 bytes。
- SHA-256：`6895aa1caf844fc55b44bde059b850f0d7dedc0a22f91ca5df308d75e8bc388d`。
- 实机检查：现代首屏、五阶段导航、一键全流程数量弹窗、经典控制台回退入口均通过；未执行真实论文任务或投稿副作用。
