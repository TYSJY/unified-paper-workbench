# 第 08 步：读取 TeX 内嵌审稿区并系统性修改论文

## 单文件契约

- 本轮只读取上一步包含 `REVIEW_ROUND_1` 注释区的 `paper.tex`。
- 不要求单独的 review 文件。
- 最终只返回修改后的完整 `paper.tex`。
- 不返回 patch、response letter 或多个文件。
- 未实际编译时不得声称编译通过。

## 前置检查

必须找到：

```latex
% <REVIEW_ROUND_1_START>
...
% <REVIEW_ROUND_1_END>
```

如缺失，停止并说明输入不是第 07 步输出，不要猜审稿意见。

## 数据保护（最高优先级）

所有 `CONFIRMED_DATA`、`USER_CONFIRMED_REAL`、正文数值、表格数据和图表关系必须完整保留。审稿意见若与此冲突，以数据保护规则为准。可以改进描述、统计边界和 limitation，不能删除、修改或重算已确认数据。

## 修改要求

1. 逐条处理 Rejection-Level Issues。
2. 逐条处理 Major Weaknesses。
3. 优先修复 novelty、method、experiment、baseline、reproducibility 和 claim-evidence alignment。
4. 无法靠写作解决的问题必须弱化相关 claim，不能假装新实验已经完成。
5. 强化 Introduction 的 problem、gap、insight、method、contribution。
6. 强化 Related Work 与危险已有工作的差异。
7. 强化 Method，使其具体、技术化、可复现。
8. 强化 Evaluation / Results，使主要 claim 有图表或数据支撑。
9. 强化 Discussion 和 Limitations。
10. 修复全部 figure/table 问题。
11. 保证 figure 不少于 5、table 不少于 4，且全部被引用和解释。
12. 保证正文实际引用至少 50 篇，`\cite{}` 与 `\bibitem{}` 匹配。
13. 不编造文献、实验、baseline 或新数据。
14. 输出必须是完整英文 LaTeX 论文。

## 审稿区处理

完成修改后，删除完整的 `REVIEW_ROUND_1` 详细注释区，替换为：

```latex
% <REVIEW_ROUND_1_RESOLUTION_START>
% stage: resolved
% rejection_level_items_addressed: ...
% major_items_addressed: ...
% claims_weakened_due_to_missing_evidence: ...
% unresolved_items: ...
% confirmed_data_preserved: true
% <REVIEW_ROUND_1_RESOLUTION_END>
```

更新 `WORKFLOW_METADATA` stage 为 `08_revised_after_review`。最终只交付完整 `paper.tex`。
