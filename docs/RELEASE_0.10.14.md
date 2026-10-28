# Unified Paper Orchestrator 0.10.14

## 重点变化

- 修复 Step 5 已生成完整 `draft_v2.tex`，却因中间稿中的 `VERIFY_DATA` 或未转义下划线被提前拦截，导致“数据已手动改完并继续”无法进入 Step 6 的死锁。
- Step 3～5 非终态中间稿的上述 LaTeX 清理项改为可见警告，并由 Step 6 强制验收；自定义流程若在 Step 3～5 结束，仍按终态严格阻塞。
- 手动数据按钮同时读取统一投影和权威 SDK 实时账本，并优先使用实时任务表中实际选中的论文。
- 对仅剩上述清理项的历史 Step 5 阻塞记录，程序复核固定会话、终态 Turn 和当前产物指纹后，无 AI 接纳既有 Step 5 产物，不重跑 Step 5、不覆盖手工数据。
- 实时任务表会把此类记录显示为“Step 5 产物已生成，确认后进入 Step 6”，不再笼统显示为语义失败。
- 修复选刊作者在未提供上游投稿包目录时把程序当前目录误当作导入源的问题；空输入现在不会递归复制项目目录。
- 写作阶段内部提示词未修改。

## 回退

修复前代码和运行状态分别保存在：

- `tmp/rollback/writing-step5-ui-prechange-20260718-181215`
- `D:\PaperProjects\UnifiedPaper-Main\state\backups\writing-step5-ui-prechange-20260718-181215`
