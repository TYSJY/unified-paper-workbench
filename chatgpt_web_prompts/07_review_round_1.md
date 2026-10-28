# 使用前准备

在发送本提示词前，请把以下文件上传到**当前聊天**，或放入当前 **ChatGPT Project** 并确保本次对话可以读取：

必需文件：

* `draft_v3.tex`
* idea 文件：文件名必须是 `YYYY-MM-DD_XXX_short-title.md`，其中不含扩展名的部分就是完整 idea ID
* `references.md`
* `literature_map.md`
* `novelty_check.md`
* `draft_v3.tex` 通过 `\includegraphics` 等命令引用的全部外部 figure 文件；如果图由单独的 TikZ、PGFPlots、CSV 或其他源文件生成，也一并提供

条件文件：

* 如果该项目存在 `manual_data_completion.json`，也必须提供

如果当前聊天或 Project 中存在多个候选 idea 文件，请在发送时明确写出本次要处理的完整 idea ID。

# GPT 网页版执行规则（最高优先级）

1. 你只能使用当前聊天附件和当前 ChatGPT Project 中本次对话实际可读取的文件。你不能读取、搜索或遍历用户电脑中的 `papers/`、`ideas/`、`refs/` 或任何本地目录，也不能假设某个未上传文件存在。
2. 必须通过 idea 文件的附件文件名确认完整 idea ID；格式为 `YYYY-MM-DD_XXX_short-title`。如果用户明确给出 idea ID，还必须核对它与 idea 文件名一致。不要猜日期，不要根据今天日期创建 ID。
3. 开始审稿前先核对必需文件、idea ID 和文件对应关系。如果缺少任何必需文件、`draft_v3.tex` 引用但未提供的外部 figure 资源，或存在多个候选而无法唯一确定目标，请立即停止；回复只能列出缺失项，或只询问用户要使用的完整 idea ID。此时不要审稿、不要生成部分结果、不要补猜内容。
4. 文中形如 `papers/<idea-id>/...`、`ideas/<idea-id>.md`、`refs/<idea-id>/...` 的路径只是用户项目中的**逻辑归档路径**，不代表你能访问这些路径。所有输入以当前聊天附件或 Project 文件为准。
5. 最终只交付一个名为 `review_round_1.md` 的完整文件。优先将它作为可下载文件提供，文件名必须精确一致；如果当前界面不能生成附件，则只输出 `Filename: review_round_1.md`，随后给出一个包含全文、不中断、不省略的 Markdown 代码块。不要输出第二个文件。
6. ChatGPT 只提供可下载文件或完整代码块。不要声称已经把结果写入、保存到或覆盖了用户电脑中的任何逻辑归档路径或本地路径。
7. 只有在当前会话确实具备相应执行能力，并且你实际完成了 LaTeX 编译或脚本运行时，才能声称相关检查“已编译通过”或“已运行通过”。否则只能进行静态审查，并明确不得把静态判断说成编译或运行结果。
8. 不得编造论文内容、图像内容、数据、引用、编译结果或文件状态。若证据不足，必须如实限定判断。

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
* 不要混用或覆盖不属于该 idea ID 的内容。

将以下附件按实际文件名和逻辑归属对应起来并读取：

* `draft_v3.tex`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex`
* `YYYY-MM-DD_XXX_short-title.md`，逻辑归档路径为 `ideas/YYYY-MM-DD_XXX_short-title.md`
* `references.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/references.md`
* `literature_map.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/literature_map.md`
* `novelty_check.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/novelty_check.md`

如果已提供，也读取：

* `manual_data_completion.json`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/results/manual_data_completion.json`

# 用户确认数据的审稿边界（最高优先级）

本节优先于后续所有审稿标准、Figure Evidence Review 和修改建议。

如果 `manual_data_completion.json` 中同时存在 `status: READY` 与
`confirmation: DATA_MANUALLY_APPLIED`，或者 `draft_v3.tex` 中存在
`CONFIRMED_DATA` 数据块 / `DATA_STATUS: USER_CONFIRMED_REAL`，则表示用户已经逐项核对并确认当前实验数据真实有效。此时必须遵守：

1. 当前论文中的实验数值、单位、方法名、表格行、图表数据关系和 `CONFIRMED_DATA` 数据块均为用户确认的真实数据。
2. 数值与 Step 5 生成值完全相同，不代表数据未替换或无效；不得据此把数据重新判定为 simulated、placeholder、fabricated 或 unverified。
3. 缺少独立 CSV、原始日志或额外附件，不得成为否定、删除或降级这些已确认数据的理由。
4. 可以审查实验方法描述、统计范围、复现说明、图表表现、claim 边界和数据来源表述，但不得建议删除、修改、替换已确认数据，也不得建议把 empirical paper 改回 proposal、protocol 或 evaluation-plan paper。
5. 如果需要独立 CSV 或绘图文件，应建议从论文表格和 `CONFIRMED_DATA` 数据块中无损提取，而不是要求重新提供同一批数据。
6. 审稿意见不得把“缺少独立数据文件”写成“现有数值可能是伪造的”，也不得要求后续 Step 8 通过删除实验结果来解决该问题。

现在请对 `draft_v3.tex` 做严格的投稿前审稿。

你的任务不是润色论文，而是像顶会审稿人一样找问题、挑漏洞、判断是否会被拒稿。

最终交付一个完整文件：

`review_round_1.md`

该文件在用户项目中的逻辑归档路径为：

`papers/YYYY-MM-DD_XXX_short-title/review_round_1.md`

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 文件及 `draft_v3.tex` 的归属一致。不要生成其他文件，也不要声称已写入该逻辑归档路径。

# 审稿标准

1. 按照机器学习 / 系统 / efficient LLM / edge LLM / inference optimization / compression / quantization / distillation 方向会议论文标准审稿。
2. 假设审稿人非常严格，会质疑 novelty、motivation、method、实验、图表、引用和 claim。
3. 不要只给泛泛建议，要指出具体问题。
4. 每个严重问题都要说明它为什么可能导致拒稿。
5. 对每个问题给出具体修改建议。
6. 检查论文是否真正围绕 idea 文件中的核心 idea 展开。
7. 检查 novelty claim 是否和 `novelty_check.md` 一致。
8. 检查是否存在过强 claim、未支撑 claim、逻辑跳跃或贡献夸大。
9. 检查 Related Work 是否充分覆盖危险相关工作。
10. 检查论文是否清楚解释了本文和已有工作的区别。
11. 检查 Method / Framework 是否足够具体，是否像真正论文贡献，而不是概念描述。
12. 检查 Problem Formulation 是否清晰、必要、可支撑后文方法。
13. 检查 Evaluation 是否能支撑 Introduction 和 Abstract 中的核心 claim。
14. 检查实验设计是否有明显缺口。
15. 检查 baselines 是否足够强。
16. 检查 metrics 是否合理。
17. 检查 ablation 是否能证明每个模块有贡献。
18. 检查 sensitivity analysis 是否足够。
19. 检查 figure 和 table 是否必要、清晰、被正文解释。
20. 检查是否至少有 5 张 figure 和 4 张 table。
21. 检查是否还残留 TODO、placeholder、simulation、verification-required、【验】、`\VERIFY` 等痕迹。
22. 检查是否正文实际引用至少 50 篇参考文献。
23. 检查每个 `\cite{}` 是否有对应 `\bibitem{}`。
24. 检查每个 `\bibitem{}` 是否至少被正文引用一次。
25. 检查语言是否接近正式投稿论文。
26. 检查是否存在明显会被 reviewer 2 抓住的问题。

# Figure Evidence Review

请对论文中的所有 figure 和 table 做专门审查。

你的任务不是只检查数量，而是判断图表是否真正支撑一篇 empirical LLM efficiency / compression / inference optimization paper。

请逐一检查每个 figure，并建立审稿判断：

1. 当前 figure 编号。
2. caption。
3. 当前视觉类型，只能从下面选择或组合：

   * system diagram
   * workflow diagram
   * controller diagram
   * state machine
   * runtime loop
   * timeline schematic
   * boxed text
   * tabular-in-figure
   * data plot
   * result chart
   * ablation chart
   * sensitivity chart
   * Pareto / tradeoff plot
   * calibration / reliability plot
   * breakdown chart
4. 该 figure 是否是 conceptual schematic。
5. 该 figure 是否是 quantitative evidence figure。
6. 该 figure 是否是 hybrid figure。
7. 该 figure 是否是 table disguised as figure。
8. 该 figure 的 caption 是否声称是实验结果图，但实际只是文字框、tabular 或流程图。
9. 该 figure 是否应保留。
10. 该 figure 是否应转成 table。
11. 该 figure 是否应替换成真正的数据图。
12. 推荐新图类型。
13. 需要的数据文件或 CSV schema。
14. 正文是否正确引用和解释该图。

请特别检查以下问题：

1. 论文是否概念图过多、数据图太少。
2. 主文 figure 是否大多数都是 framework、workflow、controller、state machine、runtime loop、timeline schematic。
3. 是否存在 caption 写 Pareto / tradeoff / calibration / ablation / sensitivity / robustness / result，但图里只是文字框、tabular 或说明性 schematic。
4. 是否存在本应是 table 的内容被放进 figure。
5. 是否至少有 3 张 quantitative evidence figure 或明确的数据图占位。
6. 是否最多只有 1–2 张纯 conceptual schematic。
7. 是否有主结果图、tradeoff 图、ablation 图、sensitivity / robustness 图。
8. 数据图是否有清晰坐标轴、指标、方法对比和数据来源。
9. 数据图是否由真实或明确标注待验证的数据，通过 CSV / Python / PGFPlots 等可复核流程生成，而不是由 ChatGPT 图像生成功能绘制。
10. ChatGPT 图像生成功能是否仅用于不含实验数值的概念图，而没有用于精确定量图。

如果发现以下情况，请把它列为 Major Weakness；严重时列为 Rejection-level Issue：

* figure 数量达标，但大部分都是概念图。
* empirical paper 缺少 quantitative evidence figures。
* table 被伪装成 figure。
* result / Pareto / tradeoff / ablation / sensitivity 图实际是文字框。
* 图表不能支撑实验 claim。
* 图和正文结果叙述不匹配。
* 图表结构让论文看起来像 proposal / design draft，而不是完成实验的 empirical paper。

在 `review_round_1.md` 中必须加入一个单独小节：

## Figure and Quantitative Evidence Review

该小节必须给出：

* figure 总数；
* table 总数；
* conceptual figure 数量；
* quantitative evidence figure 数量；
* hybrid figure 数量；
* table-disguised-as-figure 数量；
* 应保留的 figure；
* 应转成 table 的 figure；
* 应替换成数据图的 figure；
* 缺失的数据图类型；
* 需要的 CSV schema；
* 是否达到 empirical paper 的图表证据标准；
* 如果不达标，下一版 draft 应该如何重构 figure/table。

# 审稿人视角

请从以下审稿人视角分别审稿：

Reviewer A: Systems reviewer
重点关注系统设计、runtime、overhead、可实现性、设备实验、baseline、公平性。

Reviewer B: ML efficiency reviewer
重点关注 compression、decoding、quality tradeoff、metrics、ablation、和已有 efficient LLM 工作的区别。

Reviewer C: Skeptical novelty reviewer
重点关注 novelty、incrementality、和已有工作撞车风险、贡献是否夸大。

Reviewer D: Writing and presentation reviewer
重点关注论文叙事、结构、图表、claim-evidence alignment、表达是否清楚。

Reviewer E: Figure and evidence reviewer
重点关注 figure 是否过多是概念图，是否缺少 quantitative evidence figures，是否存在 table disguised as figure，是否图表能支撑 empirical claims。

# 输出结构

输出完整的 `review_round_1.md`，结构如下：

# Review Round 1

## Overall Verdict

只能选择：

* Strong Accept
* Accept
* Weak Accept
* Borderline
* Weak Reject
* Reject

## One-sentence Reason

## Summary

## Strengths

## Major Weaknesses

## Rejection-level Issues

## Reviewer A: Systems Review

## Reviewer B: ML Efficiency Review

## Reviewer C: Novelty Review

## Reviewer D: Writing and Presentation Review

## Reviewer E: Figure and Evidence Review

## Novelty Concerns

## Method Concerns

## Experiment Concerns

## Baseline Concerns

## Figure and Quantitative Evidence Review

请在这一节输出完整 figure audit。

必须包含一个表格，列为：

| Fig. | Caption Summary | Current Type | Conceptual? | Quantitative? | Table-like? | Keep / Convert / Replace | Recommended New Type | Required Data / CSV Schema |
|---|---|---|---|---|---|---|---|---|

并在表格后给出总结：

* 当前 figure 结构是否像 empirical paper；
* 是否概念图过多；
* 是否数据图不足；
* 哪些 figure 应转成 table；
* 哪些 figure 应替换为 data-driven plots；
* 最少需要新增或替换哪些 quantitative evidence figures。

## Figure and Table Concerns

## Citation and Related Work Concerns

## Claim-Evidence Problems

## Overclaiming Problems

## Section-by-section Comments

## Required Revisions

把必须修改的问题列成 checklist。

## Optional Revisions

## Final Action Plan

明确说明下一版 `draft_v4.tex` 应该怎么改。

完成后只交付 `review_round_1.md`，不要附加解释，也不要生成其他文件。
