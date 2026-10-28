# 0.5.8 唯一产品入口与写作 V2 收口

## 产品入口

- 唯一用户入口仍是 `dist/UnifiedPaperOrchestrator.exe`。
- 本地 Codex SDK 写作流程属于 `unified_paper_orchestrator/` 产品源码树，并随同一个 EXE 发布。
- `unified_paper_orchestrator_v2/` 保留为产品内部引用的实验内核，不单独发布，不代表第二个产品 V2。
- 实验内核的开发脚本改名为 `paper-orchestrator-kernel-lab`，窗口标题明确标识“非产品入口”。

## 写作流程

- 单篇论文使用一个固定 SDK 会话依次执行 Step 1–10，不使用模型调度器或每步新建会话。
- Step 5 后支持“导入实验数据”和“数据已手动处理完成”两条确定性路径。
- 手动路径只写入与当前 `draft_v2.tex` SHA256 绑定的确认记录，不修改论文正文。
- `【验】`、`VERIFY`、`UNRESOLVED` 等占位标记继续由既有 Step 6 负责清理。
- SDK 空终态和语义验收失败只允许在同一步、同一会话内按白名单受控恢复。

## 验证

- 单篇真实流程已完成 Step 1–10，最终状态为 `complete`。
- `draft_v6.tex` 语义校验为 `READY`，验数标记和未转义正文下划线均为 0。
- 手动确认、严格导入、SDK 恢复相关定向测试通过。
- 20 份用户调优内部 Prompt 未修改。
