# 0.6.0 画图阶段 V2 SDK 固定会话

本版本把图表与插图阶段迁移到产品 EXE 内的本地事件调度器。

- 程序负责队列、步骤顺序、暂停边界和状态投影，不再让 AI 会话充当调度器。
- 每篇论文只创建一个固定 Codex SDK 会话；Step 1–3 在该会话内连续执行，默认并发 1。
- 从写作阶段导入时总是比较源文件哈希并刷新过期的 `input.tex`，随后自动唤醒所选论文。
- 4 份既有画图 Prompt 按原始 SHA-256 锁定，运行时只读，未改动任何提示词内容。
- Step 2 验收 `figure_plan.md`、`figure_prompts.md` 和 figure briefs。
- Step 3 验收 submission manifest、渲染器分工、概念图原始 image2 PNG、最终图、TeX 引用、编译结果和产物新鲜度。
- 缺少真实定量数据时显示“等待真实实验数据”，不会把不完整提交包交给选刊阶段。
- 图表页新增实时任务表和会话查看器，可见当前 Step、进度、固定会话 ID、Turn ID、卡点及本地 JSONL 记录。
- 旧画图 Automation 在 V2 启动前保持暂停，仅作为历史兼容信息，不再承担调度。

唯一用户入口仍为 `dist/UnifiedPaperOrchestrator.exe`。
