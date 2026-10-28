# 0.5.4 发布记录：写作阶段端到端可靠性收口

## 目标

本版把写论文阶段收敛为 EXE 内的单一本地确定性调度器：每篇论文固定一个 Codex SDK 会话，10 个步骤在该会话中顺序执行，不再创建按分钟轮询的模型调度器会话。不同论文可以并发，但步骤产物、前置门禁、会话归属、暂停和恢复都由本地状态机验证。

全部用户调优写作 Prompt 保持原字节不变。

## 主要变化

- 旧写作模型 Automation 的“启动”入口正式退役；状态读取和暂停兼容仍保留，写作只由本地 SDK 调度器派发。
- Codex 的稳定子代理运行时固定为每棵论文会话树最多 3 个一层子会话；主会话加子会话统一按 4 个槽位预约。全局上限 10 时最多同时运行两棵满配树，第三棵等待，避免把“论文数”误当成实际会话数。
- 修复新任务到达时的丢失唤醒、进程崩溃后的孤儿锁、Windows 锁释放竞争，以及模型发送前最后一刻暂停的竞态。
- 10 个写作 Prompt 使用程序内置 SHA-256 清单封印；建/续会话前、发送前、回合返回后和提交前都会验证，异常时停止且不接受产物。
- Step 3–5 增加结构化 LaTeX 产物验收；Step 5 明确保留既有 `【验】` 语义，并声明 Unicode/CJK 编译配置，同时拒绝正文中未转义的 `VERIFY_DATA`。
- 新增只针对“最后一个已完成语义步骤”的安全重验命令。它验证固定线程、回合终态和产物哈希；若新验证器发现旧完成记录无效，只回退一步并保留完整审计，不调用模型。
- 写论文页新增“导入真实实验证据…”：先只读预检，再由用户确认提交；清单与预检哈希绑定，验证清单最后原子写入，且不会自动继续 Step 6 或产生模型用量。
- Step 6 的真实证据门禁保持 fail-closed：缺少实验材料、声明或可核验产物时进入等待，不会用模拟结果冒充完成。

## 真实金丝雀

真实论文 `2026-07-13_001_resource-shift-benchmark` 已在同一个固定 Codex 会话中完成 Step 2–5。Step 3 与 Step 4 的 PDF 分别完成双次编译和逐页目视检查；Step 5 的新验证器准确定位到 5 处未转义 `VERIFY_DATA`。Step 6 在缺少真实实验材料时进入 `WRITING_EVIDENCE_GATE_BLOCKED`，未创建模型回合、未增加 Token。

Step 6–10 不会用伪造证据强行跑通；导入真实材料后才允许继续，这是产品正确性边界，不是故障。

## 运维命令

```powershell
UnifiedPaperOrchestrator.exe writing-sdk-status
UnifiedPaperOrchestrator.exe writing-sdk-revalidate-last-completed --paper-key "session:<paper-id>"
UnifiedPaperOrchestrator.exe writing-evidence-import <paper-id> --spec <paper-relative-json> --check-only
```

第二条命令严格不派发模型；第三条命令去掉 `--check-only` 后才提交经预检的证据清单。
