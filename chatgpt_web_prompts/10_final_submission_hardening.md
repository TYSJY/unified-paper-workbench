# 使用前准备

在发送本提示词前，请把以下文件上传到**当前聊天**，或放入当前 **ChatGPT Project** 并确保本次对话可以读取：

必需文件：

* `draft_v5.tex`
* idea 文件：文件名必须是 `YYYY-MM-DD_XXX_short-title.md`，其中不含扩展名的部分就是完整 idea ID
* `references.md`
* `literature_map.md`
* `novelty_check.md`
* `draft_v5.tex` 引用且完成最终强化所必需的外部 figure、TikZ、PGFPlots、CSV、宏、样式或其他依赖文件

条件文件：

* 如果该项目存在 `review_round_1.md`，请一并提供
* 如果该项目存在 `review_round_2.md`，请一并提供
* 如果该项目存在 `review_round_3.md`，请一并提供
* 如果该项目存在 `manual_data_completion.json`，必须一并提供

如果当前聊天或 Project 中存在多个候选 idea 文件，请在发送时明确写出本次要处理的完整 idea ID。

# GPT 网页版执行规则（最高优先级）

1. 你只能使用当前聊天附件和当前 ChatGPT Project 中本次对话实际可读取的文件。你不能读取、搜索或遍历用户电脑中的 `papers/`、`ideas/`、`refs/` 或任何本地目录，也不能假设某个未上传文件存在。
2. 必须优先按实际附件文件名识别输入，并通过 idea 文件的附件文件名确认完整 idea ID；格式为 `YYYY-MM-DD_XXX_short-title`。如果用户明确给出 idea ID，还必须核对它与 idea 文件名一致。不要猜日期，不要根据今天日期创建 ID。
3. 开始强化前先核对必需文件、idea ID 和文件对应关系。如果缺少任何必需文件或必要依赖，或存在多个候选而无法唯一确定目标，请立即停止；回复只能列出缺失项，或只询问用户要使用的完整 idea ID。此时不要修改论文、不要输出部分草稿、不要补猜内容。
4. 文中形如 `papers/<idea-id>/...`、`ideas/<idea-id>.md`、`refs/<idea-id>/...` 的路径只是用户项目中的**逻辑归档路径**，不代表你能访问这些路径。所有输入以当前聊天附件或 Project 文件为准。
5. 最终只交付一个名为 `draft_v6.tex` 的完整文件。优先将它作为可下载文件提供，文件名必须精确一致；如果当前界面不能生成附件，则只输出 `Filename: draft_v6.tex`，随后给出一个包含完整 LaTeX 全文、不中断、不省略的代码块。不要输出第二个文件。
6. ChatGPT 只提供可下载文件或完整代码块。不要声称已经把结果写入、保存到或覆盖了用户电脑中的任何逻辑归档路径或本地路径。
7. 只有在当前会话确实具备相应执行能力，并且你实际完成了 LaTeX 编译或静态脚本运行时，才能声称相关检查“已编译通过”或“已运行通过”。否则只能进行静态检查，不得声称编译、脚本或格式验证已经通过。
8. 不得编造论文内容、实验、数据、baseline、引用、图表、编译结果或文件状态。缺失信息只能专业地限定或列为 limitation，不能假装已经补齐。

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
* 不要覆盖作为输入的 `draft_v5.tex`。
* 不要混用或生成不属于该 idea ID 的内容。

将以下附件按实际文件名和逻辑归属对应起来并读取：

* `draft_v5.tex`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/draft_v5.tex`
* `YYYY-MM-DD_XXX_short-title.md`，逻辑归档路径为 `ideas/YYYY-MM-DD_XXX_short-title.md`
* `references.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/references.md`
* `literature_map.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/literature_map.md`
* `novelty_check.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/novelty_check.md`

如果已提供，也读取：

* `review_round_1.md`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/review_round_1.md`
* `review_round_2.md`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/review_round_2.md`
* `review_round_3.md`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/review_round_3.md`
* `manual_data_completion.json`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/results/manual_data_completion.json`

现在请对 `draft_v5.tex` 做最终投稿强化。

最终只交付一个完整文件：

`draft_v6.tex`

该文件在用户项目中的逻辑归档路径为：

`papers/YYYY-MM-DD_XXX_short-title/draft_v6.tex`

不要生成其他文件，也不要声称已写入该逻辑归档路径。

# 用户确认数据保护规则（最高优先级）

如果 `manual_data_completion.json` 中同时存在 `status: READY` 与
`confirmation: DATA_MANUALLY_APPLIED`，或者 `draft_v5.tex` 中存在
`CONFIRMED_DATA` 数据块 / `DATA_STATUS: USER_CONFIRMED_REAL`，则当前实验数据已经由用户确认并锁定。本规则优先于本提示词中所有 claim 校准、证据链加固、格式清理和 limitation 改写要求：

1. 完整保留全部实验数值、单位、方法名、表格行、比较关系、结果段落、figure/table 证据及 `CONFIRMED_DATA` 数据块。
2. 不得删除、修改、重算、平滑、美化或替换任何已确认数据；数值与早期草稿完全相同也必须保留。
3. 不得因缺少独立 CSV、raw trace、日志、设备附件或其他外部文件而删除定量结果、弱化为“尚待验证”，或把论文改成 methodology-only / proposal / protocol paper。
4. 可以收紧 claim 的适用范围、补强实验描述和专业化 limitation，但不能否定已确认数据及其直接支持的事实性结论。
5. 画图所需独立 CSV 不存在时，保留论文表格与 `CONFIRMED_DATA` 数据块，供画图阶段无损提取。
6. 生成 `draft_v6.tex` 前，逐项比较 `draft_v5.tex` 与输出中的数据块、表格和定量结果；发现任何丢失或变化时必须恢复。

# 核心目标

将 `draft_v5.tex` 从“完整投稿候选稿”提升为“更接近可直接提交的正式投稿稿”。

这一步不负责画图，不生成图片，不生成新实验，不编造数据。
这一步只负责：

* 证据链加固
* claim 强度校准
* 实验叙述成熟化
* baseline 解释增强
* 可复现性信息补强
* 结果分析补强
* limitation 专业化
* 图表 caption 正式化
* LaTeX 投稿格式清理

# 禁止事项

1. 不要改变论文核心 idea。
2. 不要新增实验结果。
3. 不要修改真实实验数值。
4. 不要平滑、美化、猜测或重算任何结果。
5. 不要编造 baseline。
6. 不要编造参考文献。
7. 不要新增 refs 中不存在的引用。
8. 不要删除关键实验结果。
9. 不要删除必要 limitation。
10. 不要生成图片。
11. 不要生成额外文件。
12. 不要输出 patch 或解释说明。
13. 不要把论文改成另一个方向。

# 重点任务 1：Claim-Evidence Alignment

请检查 Abstract、Introduction、Results、Discussion 和 Conclusion 中的所有重要 claim。

每个重要 claim 必须满足以下至少一种条件：

* 有实验结果支撑；
* 有 figure 或 table 支撑；
* 有 references 支撑；
* 被明确限定在 evaluated settings / tested workloads / studied devices 范围内。

如果某个 claim 支撑不足，请弱化它，而不是编造证据。

例如：

过强：

“RCCC consistently improves decoding efficiency across heterogeneous devices.”

更好：

“Across the evaluated heterogeneous decoding settings, RCCC improves coverage-risk tradeoffs while maintaining competitive latency and memory profiles.”

过强：

“Our method solves resource-constrained on-device decoding.”

更好：

“Our method addresses a specific failure mode in resource-constrained on-device decoding: unsafe or unstable routing decisions under changing resource pressure.”

# 重点任务 2：Abstract 强化

Abstract 必须像正式投稿论文摘要。

请检查并改写 Abstract，使其包含：

1. 问题背景；
2. 现有方法缺口；
3. 本文方法；
4. 实验设置范围；
5. 关键结果；
6. 贡献总结。

要求：

* 不要写内部流程口吻。
* 不要写 available material / artifact / generated draft。
* 不要过度声称。
* 所有定量结果必须和正文表格或图一致。
* 如果结果只在特定 setting 下成立，摘要中要明确限定。

# 重点任务 3：Introduction 强化

Introduction 必须回答：

1. 为什么这个问题重要？
2. 现有方法为什么不够？
3. 本文的 key insight 是什么？
4. 方法到底做了什么？
5. 实验证据支持了什么？
6. 本文贡献是什么？

请重点修改：

* contribution list；
* problem-gap-insight-method-evidence 的连贯性；
* 不够具体的泛泛动机；
* 没有证据支撑的强贡献 claim；
* 和 Related Work 重复但没有推进故事的段落。

贡献点建议使用更审慎、更可防守的写法：

* “We identify ...”
* “We design ...”
* “We implement and evaluate ...”
* “We show, in the evaluated settings, that ...”
* “We analyze the tradeoff between ...”

避免：

* “We solve ...”
* “We are the first ...” 除非 `novelty_check.md` 明确支持；
* “universally improves ...”
* “guarantees ...”

# 重点任务 4：Related Work / Novelty 加固

请检查 Related Work 是否能防住 reviewer 的 novelty 攻击。

要求：

1. 明确本文和已有工作的边界。
2. 对最危险相关方向要有直接定位。
3. 不要泛泛罗列文献。
4. 不要过度声称“没人做过”。
5. 不要删除关键引用。
6. 如果 `novelty_check.md` 中标记某方向危险，请谨慎表述。

推荐句式：

* “Prior work primarily optimizes ..., whereas this paper focuses on ...”
* “Unlike methods that assume ..., our approach targets ...”
* “This distinction matters because ...”
* “The closest line of work is ..., but it does not address ...”

# 重点任务 5：Method / System Design 加固

请检查方法部分是否足够具体。

要求：

1. 模块输入、输出、状态、控制逻辑必须清楚。
2. 算法描述必须和正文一致。
3. 关键变量必须定义清楚。
4. 不要只停留在 conceptual framework。
5. 不要新增不存在的实现细节。
6. 如果某些实现范围有限，在 Limitations 中专业说明。

请特别检查：

* controller 如何决策；
* runtime state 如何被使用；
* resource pressure 如何影响 action；
* fallback / safety / verification 机制是否清楚；
* 方法和 baseline 的区别是否清楚。

# 重点任务 6：Evaluation Setup 可复现性强化

请检查 Evaluation / Experimental Setup。

这一节至少应清楚说明：

* models 或 model-size classes；
* devices 或 device classes；
* workloads；
* context lengths；
* baselines；
* metrics；
* measurement methodology；
* latency 如何定义；
* memory 如何定义；
* energy 如何定义；
* quality / risk / coverage / accuracy 如何定义；
* 结果是如何聚合的；
* 如果论文已有 run 数、平均值、标准差、置信区间，请写清楚。

请用专业限制语言处理，而不是内部材料口吻。

例如：

不要写：

“The available material does not include exact SKU metadata.”

改成：

“Our evaluation reports device-class results under anonymized hardware descriptions; exhaustive per-SKU characterization is left to deployment-specific studies.”

不要写：

“Full confidence intervals are not available.”

改成：

“The evaluation emphasizes aggregate cross-setting trends; future deployments may require additional per-device uncertainty calibration.”

# 重点任务 7：Results 强化

请检查 Results 部分。

每个主要结果段落都要回答：

1. 表格或图显示了什么？
2. 这个结果支持哪个 claim？
3. 和最强 baseline 比，优势在哪里？
4. 如果某个指标不是最优，为什么 tradeoff 仍然合理？
5. 这个结果有什么边界？

不要只是复述数字。

如果本文方法在某些指标上不如某个 baseline，不要回避。
请解释 tradeoff，例如：

* 更高 coverage；
* 更低 risk；
* 更稳定 fallback；
* 更好的 safety-efficiency balance；
* 更适合 resource-pressure setting。

请确保所有定量结果都能在 figure 或 table 中找到对应数值。

# 重点任务 8：Baseline 解释强化

请检查 baseline 是否被充分解释。

对于每个主要 baseline，应说明：

* 它代表哪类已有方法；
* 为什么选择它；
* 它强在哪里；
* 它弱在哪里；
* 本文方法和它的关键区别是什么。

特别注意：

如果某个 baseline 在 latency、memory、energy 上优于本文方法，而本文方法在 risk、coverage、stability 上更好，请明确解释：

* 本文不是单指标最低成本方法；
* 本文目标是 risk-aware / contract-aware / resource-aware tradeoff；
* 本文强调更稳定的 coverage-risk balance。

# 重点任务 9：Ablation / Sensitivity 强化

请检查 ablation 和 sensitivity analysis。

要求：

1. 每个模块消融都要对应一个明确解释。
2. 不要只说“性能下降”，要解释为什么下降。
3. sensitivity analysis 要说明在什么条件下方法更有效，什么条件下收益变小。
4. 如果出现 failure case，要解释原因。
5. 如果某些模块贡献不明显，要诚实描述并限制 claim。

# 重点任务 10：Discussion / Limitations 专业化

Limitations 必须保留，但不能过度自我削弱。

可以写：

* evaluated devices are representative but not exhaustive；
* results may vary across hardware generations；
* policy thresholds may require retuning；
* energy telemetry precision may vary across platforms；
* backend integration requires access to runtime hooks；
* workload coverage is limited to the studied settings。

不要写：

* available material does not contain；
* artifact lacks；
* no confidence intervals are available；
* evidence is incomplete；
* results are not established；
* this remains future measurement；
* the paper only defines a protocol。

Discussion 要解释 tradeoff，而不是重复结果。

# 重点任务 11：图表和 caption 清理

用户会单独处理图片质量，所以本步骤不生成图片。

但是必须检查：

1. figure 数量不少于 5。
2. table 数量不少于 4。
3. 所有 figure 和 table 都在正文中被引用。
4. 所有 caption 都是正式论文 caption。
5. caption 不应出现内部材料口吻。
6. caption 不应出现 placeholder、artifact、future measurement、protocol、TODO、verification。
7. 如果表格过宽，请调整 LaTeX 排版，例如：

   * `table*`；
   * `\small`；
   * 缩短列名；
   * `p{...}` 列宽；
   * 拆成两个表。
8. 不要删除重要结果表。
9. 不要修改真实实验数值。

# 重点任务 12：投稿痕迹清理

论文全文不得出现以下字样或语义等价表达：

* available material
* available artifact
* result artifact
* provided material
* this artifact
* aggregate-only evidence
* raw traces are not available
* exact SKU-level metadata is not available
* full confidence intervals are not available
* placeholder
* TODO
* future measurement
* evaluation protocol
* claim-evidence map
* 【验】
* `\VERIFY`
* simulated data
* to-be-validated
* verification-required
* UNRESOLVED
* this draft
* current version
* present version

如果发现，请继续改写。

# 重点任务 13：引用检查

请保持：

1. 正文实际引用至少 50 篇参考文献。
2. 每个 `\cite{}` 有对应 `\bibitem{}`。
3. 每个 `\bibitem{}` 至少被正文引用一次。
4. 不要编造参考文献。
5. 不要使用 refs 中不存在的参考文献。
6. 参考文献继续放在 `draft_v6.tex` 末尾的 `thebibliography` 环境中。
7. 不要生成单独 `.bib` 文件。

# 最终内部检查

在生成 `draft_v6.tex` 前，请检查：

* 是否还有内部材料口吻；
* 是否还有未完成稿表述；
* 是否还有过强 claim；
* 是否所有主要 claim 都有 evidence；
* 是否 Evaluation Setup 足够清楚；
* 是否 Results 不只是复述数字；
* 是否 strongest baseline 的对比解释清楚；
* 是否 Limitations 专业但不自毁；
* 是否 figure 数量不少于 5；
* 是否 table 数量不少于 4；
* 是否所有图表都被正文引用；
* 是否正文实际引用至少 50 篇参考文献；
* 是否所有 `\cite{}` 和 `\bibitem{}` 匹配；
* 是否每个 `\bibitem{}` 至少被引用一次。

上述检查默认只能作为静态核对。若当前会话没有实际运行 LaTeX 编译器或检查脚本，不得声称论文已编译通过或脚本验证通过。

# 输出要求

1. 输出完整 `draft_v6.tex`。
2. 优先提供文件名精确为 `draft_v6.tex` 的可下载文件；无法提供附件时，按“GPT 网页版执行规则”输出完整代码块。
3. 不要输出 patch。
4. 不要输出解释说明。
5. 不要生成额外文件。
6. 不要声称已写入本地或逻辑归档路径。
7. 停止在交付 `draft_v6.tex` 之后。
