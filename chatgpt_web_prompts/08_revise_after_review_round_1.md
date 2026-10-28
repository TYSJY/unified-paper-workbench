# 使用前准备

在发送本提示词前，请把以下文件上传到**当前聊天**，或放入当前 **ChatGPT Project** 并确保本次对话可以读取：

必需文件：

* `draft_v3.tex`
* `review_round_1.md`
* idea 文件：文件名必须是 `YYYY-MM-DD_XXX_short-title.md`，其中不含扩展名的部分就是完整 idea ID
* `references.md`
* `literature_map.md`
* `novelty_check.md`
* `draft_v3.tex` 引用且完成论文修改所必需的外部 figure、TikZ、PGFPlots、CSV、宏、样式或其他依赖文件

条件文件：

* 如果该项目存在 `manual_data_completion.json`，也必须提供

如果当前聊天或 Project 中存在多个候选 idea 文件，请在发送时明确写出本次要处理的完整 idea ID。

# GPT 网页版执行规则（最高优先级）

1. 你只能使用当前聊天附件和当前 ChatGPT Project 中本次对话实际可读取的文件。你不能读取、搜索或遍历用户电脑中的 `papers/`、`ideas/`、`refs/` 或任何本地目录，也不能假设某个未上传文件存在。
2. 必须优先按实际附件文件名识别输入，并通过 idea 文件的附件文件名确认完整 idea ID；格式为 `YYYY-MM-DD_XXX_short-title`。如果用户明确给出 idea ID，还必须核对它与 idea 文件名一致。不要猜日期，不要根据今天日期创建 ID。
3. 开始修改前先核对必需文件、idea ID 和文件对应关系。如果缺少任何必需文件或必要依赖，或存在多个候选而无法唯一确定目标，请立即停止；回复只能列出缺失项，或只询问用户要使用的完整 idea ID。此时不要修改论文、不要输出部分草稿、不要补猜内容。
4. 文中形如 `papers/<idea-id>/...`、`ideas/<idea-id>.md`、`refs/<idea-id>/...` 的路径只是用户项目中的**逻辑归档路径**，不代表你能访问这些路径。所有输入以当前聊天附件或 Project 文件为准。
5. 最终只交付一个名为 `draft_v4.tex` 的完整文件。优先将它作为可下载文件提供，文件名必须精确一致；如果当前界面不能生成附件，则只输出 `Filename: draft_v4.tex`，随后给出一个包含完整 LaTeX 全文、不中断、不省略的代码块。不要输出第二个文件。
6. ChatGPT 只提供可下载文件或完整代码块。不要声称已经把结果写入、保存到或覆盖了用户电脑中的任何逻辑归档路径或本地路径。
7. 只有在当前会话确实具备相应执行能力，并且你实际完成了 LaTeX 编译或静态脚本运行时，才能声称相关检查“已编译通过”或“已运行通过”。否则只能进行静态检查，不得声称编译、脚本或格式验证已经通过。
8. 不得编造论文内容、实验、数据、引用、图表、编译结果或文件状态。若某项问题无法仅靠现有附件可靠解决，按正文要求谨慎弱化 claim，不得假装已经补齐证据。

# 目标识别与输入

本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

`YYYY-MM-DD_XXX_short-title`

规则：

* 不要自己猜日期。
* 不要根据今天日期临时编一个 ID。
* 如果用户已经指定 idea ID，就使用该 ID，并与附件中的 idea 文件名核对。
* 如果用户没有指定 idea ID，只能根据唯一的 `YYYY-MM-DD_XXX_short-title.md` idea 附件确定。
* 如果无法确定唯一目标 idea ID，请按“GPT 网页版执行规则”停止并询问用户，不要继续执行。
* `draft_v4.tex` 不需要额外编号；编号属于其逻辑归档父目录 `papers/YYYY-MM-DD_XXX_short-title/`。
* 不要生成 `draft_v5.tex`、`draft_001.tex` 或其他草稿文件。
* 不要混用或覆盖不属于该 idea ID 的内容。

将以下附件按实际文件名和逻辑归属对应起来并读取：

* `draft_v3.tex`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex`
* `review_round_1.md`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/review_round_1.md`
* `YYYY-MM-DD_XXX_short-title.md`，逻辑归档路径为 `ideas/YYYY-MM-DD_XXX_short-title.md`
* `references.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/references.md`
* `literature_map.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/literature_map.md`
* `novelty_check.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/novelty_check.md`

如果已提供，也读取：

* `manual_data_completion.json`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/results/manual_data_completion.json`

现在请根据 `review_round_1.md` 对 `draft_v3.tex` 做一次系统性修改。

最终只交付一个完整文件：

`draft_v4.tex`

该文件在用户项目中的逻辑归档路径为：

`papers/YYYY-MM-DD_XXX_short-title/draft_v4.tex`

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 文件、`draft_v3.tex`、`review_round_1.md` 的归属一致。不要生成其他文件，也不要声称已写入该逻辑归档路径。

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

# 修改目标

把 `draft_v3.tex` 从“完整论文稿”提升为“更能经受审稿人细致检查的投稿候选稿”。

# 修改要求

1. 保留原论文核心 idea，不要改成另一个 idea。
2. 逐条处理 `review_round_1.md` 中的 Rejection-level Issues。
3. 逐条处理 Major Weaknesses。
4. 优先修复 novelty、method、experiment、baseline、claim-evidence alignment 相关问题。
5. 如果某个问题无法仅通过写作修复，需要真实实验或额外材料，请在正文中谨慎弱化相关 claim，不要假装已经解决。
6. 强化 Introduction 中的 problem、gap、insight、method、contribution。
7. 强化 Related Work 中本文和危险已有工作的区别。
8. 删除或弱化 `novelty_check.md` 中不安全的 claim。
9. 强化 Method / Framework，使其更具体、更可复现、更像技术贡献。
10. 强化 Evaluation / Results，使每个主要 claim 都有实验结果或图表支撑。
11. 强化 Discussion 和 Limitations，使论文更诚实、更不容易被攻击。
12. 修复所有 figure/table 相关问题。
13. 保证最终 `draft_v4.tex` 中 figure 总数不少于 5。
14. 保证最终 `draft_v4.tex` 中 table 总数不少于 4。
15. 所有 figure 和 table 都必须在正文中被引用和解释。
16. 保证正文实际引用至少 50 篇参考文献。
17. 保证每个 `\cite{}` 有对应 `\bibitem{}`。
18. 保证每个 `\bibitem{}` 至少被正文引用一次。
19. 不要编造参考文献。
20. 不要使用 refs 中不存在的文献。
21. 不要编造实验结果。
22. 不要新增未经依据的强 claim。
23. 删除所有 TODO、placeholder、simulation、verification-required、【验】、`\VERIFY` 等投稿不应出现的痕迹。
24. 输出必须是完整 LaTeX 论文，而不是 patch、说明或修改清单。

# 写作风格

1. 使用英文。
2. 保持机器学习 / 系统会议论文风格。
3. 避免过度宣传。
4. 避免空泛句子。
5. 每个 claim 尽量具体、有证据、有边界。
6. 让论文更难被 reviewer 2 一句话打掉。

# 交付前内部检查

在交付 `draft_v4.tex` 前，静态核对数据锁定规则、figure/table 数量与正文引用、至少 50 篇正文引用、`\cite{}` 与 `\bibitem{}` 的双向对应、残留投稿痕迹，以及输出是否为完整 LaTeX。若没有实际运行编译器或检查脚本，不得声称这些项目已经“编译通过”或“脚本验证通过”；只能准确说明完成了静态核对。最终文件正文中不要加入本次处理说明。

完成后只交付 `draft_v4.tex`，不要附加解释，也不要生成其他文件。
