$ml-paper-writing

本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

规则：

- 不要自己猜日期。
- 不要根据今天日期临时编一个 ID。
- 如果用户或上游任务已经指定 idea ID，就使用该 ID。
- 如果没有指定 idea ID，请先查看 `papers/` 目录，并选择已经有 `draft_v3.tex` 和 `review_round_1.md`、且需要修改成 `draft_v4.tex` 的那个 idea ID。
- 如果无法确定唯一目标 idea ID，请停止并要求用户指定，不要继续执行。
- `draft_v4.tex` 不需要额外编号；编号属于父目录 `papers/YYYY-MM-DD_XXX_short-title/`。
- 不要创建 `draft_v5.tex`、`draft_001.tex` 或其他草稿文件。
- 不要覆盖不属于该 idea ID 的 paper 目录。

读取下面这些文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex
papers/YYYY-MM-DD_XXX_short-title/review_round_1.md
ideas/YYYY-MM-DD_XXX_short-title.md
refs/YYYY-MM-DD_XXX_short-title/references.md
refs/YYYY-MM-DD_XXX_short-title/literature_map.md
refs/YYYY-MM-DD_XXX_short-title/novelty_check.md

如果存在，也读取：

papers/YYYY-MM-DD_XXX_short-title/results/manual_data_completion.json

现在请根据 review_round_1.md 对 draft_v3.tex 做一次系统性修改。

只创建一个输出文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v4.tex

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 目录、draft_v3.tex、review_round_1.md 所在目录一致。

不要创建其他文件。

# 用户确认数据保护规则（最高优先级）

本节优先于 `review_round_1.md`、后续修改要求以及任何“弱化 claim”或“删除未支撑结果”的一般规则。

如果 `manual_data_completion.json` 中同时存在 `status: READY` 与
`confirmation: DATA_MANUALLY_APPLIED`，或者 `draft_v3.tex` 中存在
`CONFIRMED_DATA` 数据块 / `DATA_STATUS: USER_CONFIRMED_REAL`，则当前论文的实验数据已经由用户确认并锁定：

1. 必须完整保留全部实验数值、单位、方法名、表格行、比较关系、结果段落、figure/table 证据以及全部 `CONFIRMED_DATA` 数据块。
2. 不得修改、重算、平滑、美化、替换或删除任何已确认数值；数值与 Step 5 原值相同也不影响其真实性。
3. 不得因为审稿意见声称缺少 CSV、raw trace、power log、设备附件或其他独立文件，就删除实验结果、定量 claim、结果表格或数据图。
4. 审稿意见与本规则冲突时，以本规则为准。可以改进实验设置说明、复现描述、统计边界、claim 范围和 limitation，但必须保留已确认数据及其事实性结论。
5. 独立 CSV 不存在时，论文中的表格和 `CONFIRMED_DATA` 数据块本身就是有效数据源；不得把论文降级为 proposal、protocol、methodology-only 或 evaluation-plan paper。
6. 生成 `draft_v4.tex` 前，逐项比较 `draft_v3.tex` 与输出中的数据块、表格和定量结果。发现任何数据、数据行、方法或单位丢失时，必须恢复后再完成本步骤。

修改目标：

把 draft_v3.tex 从“完整论文稿”提升为“更能经受审稿人细致检查的投稿候选稿”。

修改要求：

1. 保留原论文核心 idea，不要改成另一个 idea。
2. 逐条处理 review_round_1.md 中的 Rejection-level Issues。
3. 逐条处理 Major Weaknesses。
4. 优先修复 novelty、method、experiment、baseline、claim-evidence alignment 相关问题。
5. 如果某个问题无法仅通过写作修复，需要真实实验或额外材料，请在正文中谨慎弱化相关 claim，不要假装已经解决。
6. 强化 Introduction 中的 problem、gap、insight、method、contribution。
7. 强化 Related Work 中本文和危险已有工作的区别。
8. 删除或弱化 novelty_check.md 中不安全的 claim。
9. 强化 Method / Framework，使其更具体、更可复现、更像技术贡献。
10. 强化 Evaluation / Results，使每个主要 claim 都有实验结果或图表支撑。
11. 强化 Discussion 和 Limitations，使论文更诚实、更不容易被攻击。
12. 修复所有 figure/table 相关问题。
13. 保证最终 draft_v4.tex 中 figure 总数不少于 5。
14. 保证最终 draft_v4.tex 中 table 总数不少于 4。
15. 所有 figure 和 table 都必须在正文中被引用和解释。
16. 保证正文实际引用至少 50 篇参考文献。
17. 保证每个 \cite{} 有对应 \bibitem{}。
18. 保证每个 \bibitem{} 至少被正文引用一次。
19. 不要编造参考文献。
20. 不要使用 refs 中不存在的文献。
21. 不要编造实验结果。
22. 不要新增未经依据的强 claim。
23. 删除所有 TODO、placeholder、simulation、verification-required、【验】、\VERIFY 等投稿不应出现的痕迹。
24. 输出必须是完整 LaTeX 论文，而不是 patch、说明或修改清单。

写作风格：

1. 使用英文。
2. 保持机器学习 / 系统会议论文风格。
3. 避免过度宣传。
4. 避免空泛句子。
5. 每个 claim 尽量具体、有证据、有边界。
6. 让论文更难被 reviewer 2 一句话打掉。

停止在生成 papers/YYYY-MM-DD_XXX_short-title/draft_v4.tex 之后。
