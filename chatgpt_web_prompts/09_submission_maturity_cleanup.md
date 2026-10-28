# 使用前准备

在发送本提示词前，请把以下文件上传到**当前聊天**，或放入当前 **ChatGPT Project** 并确保本次对话可以读取：

必需文件：

* `draft_v4.tex`
* idea 文件：文件名必须是 `YYYY-MM-DD_XXX_short-title.md`，其中不含扩展名的部分就是完整 idea ID
* `references.md`
* `literature_map.md`
* `novelty_check.md`
* `draft_v4.tex` 引用且完成论文清理所必需的外部 figure、TikZ、PGFPlots、CSV、宏、样式或其他依赖文件

条件文件：

* 如果该项目存在 `review_round_1.md`，请一并提供
* 如果该项目存在 `review_round_2.md`，请一并提供
* 如果该项目存在 `manual_data_completion.json`，必须一并提供

如果当前聊天或 Project 中存在多个候选 idea 文件，请在发送时明确写出本次要处理的完整 idea ID。

# GPT 网页版执行规则（最高优先级）

1. 你只能使用当前聊天附件和当前 ChatGPT Project 中本次对话实际可读取的文件。你不能读取、搜索或遍历用户电脑中的 `papers/`、`ideas/`、`refs/` 或任何本地目录，也不能假设某个未上传文件存在。
2. 必须优先按实际附件文件名识别输入，并通过 idea 文件的附件文件名确认完整 idea ID；格式为 `YYYY-MM-DD_XXX_short-title`。如果用户明确给出 idea ID，还必须核对它与 idea 文件名一致。不要猜日期，不要根据今天日期创建 ID。
3. 开始清理前先核对必需文件、idea ID 和文件对应关系。如果缺少任何必需文件或必要依赖，或存在多个候选而无法唯一确定目标，请立即停止；回复只能列出缺失项，或只询问用户要使用的完整 idea ID。此时不要修改论文、不要输出部分草稿、不要补猜内容。
4. 文中形如 `papers/<idea-id>/...`、`ideas/<idea-id>.md`、`refs/<idea-id>/...` 的路径只是用户项目中的**逻辑归档路径**，不代表你能访问这些路径。所有输入以当前聊天附件或 Project 文件为准。
5. 最终只交付一个名为 `draft_v5.tex` 的完整文件。优先将它作为可下载文件提供，文件名必须精确一致；如果当前界面不能生成附件，则只输出 `Filename: draft_v5.tex`，随后给出一个包含完整 LaTeX 全文、不中断、不省略的代码块。不要输出第二个文件。
6. ChatGPT 只提供可下载文件或完整代码块。不要声称已经把结果写入、保存到或覆盖了用户电脑中的任何逻辑归档路径或本地路径。
7. 只有在当前会话确实具备相应执行能力，并且你实际完成了 LaTeX 编译或静态脚本运行时，才能声称相关检查“已编译通过”或“已运行通过”。否则只能进行静态检查，不得声称编译、脚本或格式验证已经通过。
8. 不得编造论文内容、实验、数据、引用、图表、编译结果或文件状态。缺失信息只能专业地限定或列为 limitation，不能假装已经补齐。

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
* 不要覆盖作为输入的 `draft_v4.tex`。
* 不要混用或生成不属于该 idea ID 的内容。

将以下附件按实际文件名和逻辑归属对应起来并读取：

* `draft_v4.tex`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/draft_v4.tex`
* `YYYY-MM-DD_XXX_short-title.md`，逻辑归档路径为 `ideas/YYYY-MM-DD_XXX_short-title.md`
* `references.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/references.md`
* `literature_map.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/literature_map.md`
* `novelty_check.md`，逻辑归档路径为 `refs/YYYY-MM-DD_XXX_short-title/novelty_check.md`

如果已提供，也读取：

* `review_round_1.md`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/review_round_1.md`
* `review_round_2.md`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/review_round_2.md`
* `manual_data_completion.json`，逻辑归档路径为 `papers/YYYY-MM-DD_XXX_short-title/results/manual_data_completion.json`

现在请对 `draft_v4.tex` 做一次“投稿成熟度清理”。

最终只交付一个完整文件：

`draft_v5.tex`

该文件在用户项目中的逻辑归档路径为：

`papers/YYYY-MM-DD_XXX_short-title/draft_v5.tex`

不要生成其他文件，也不要声称已写入该逻辑归档路径。

# 用户确认数据保护规则（最高优先级）

如果 `manual_data_completion.json` 中同时存在 `status: READY` 与
`confirmation: DATA_MANUALLY_APPLIED`，或者 `draft_v4.tex` 中存在
`CONFIRMED_DATA` 数据块 / `DATA_STATUS: USER_CONFIRMED_REAL`，则当前实验数据已经由用户确认并锁定。本规则优先于本提示词中所有清理、弱化 claim 和删除不成熟表述的要求：

1. 完整保留全部实验数值、单位、方法名、表格行、比较关系、结果段落、figure/table 证据及 `CONFIRMED_DATA` 数据块。
2. 不得删除、修改、重算、平滑、美化或替换已确认数据；数值与早期草稿相同不构成删除理由。
3. 不得因缺少独立 CSV、raw trace、日志或附件，把已确认数据描述为 simulated、placeholder、unverified 或 unsupported。
4. 可以成熟化文字、caption、实验设置和 limitation，但不能通过删除定量结果或把论文改成 methodology-only / proposal paper 来完成清理。
5. 如果需要结构化数据文件，应保留论文数据并交由画图阶段从表格或 `CONFIRMED_DATA` 数据块无损提取。
6. 输出前逐项检查输入中的数据块、表格和定量结果在 `draft_v5.tex` 中仍完整存在。

# 核心目标

将 `draft_v4.tex` 从“完整但仍容易被审稿人抓问题的论文稿”提升为“更成熟、更正式、更难被明显攻击的投稿候选稿”。

这一步不是重新发明 idea，不是重写整篇论文，不是编造新实验，而是专门修复以下问题：

* 残留的内部材料口吻
* 不成熟的实验表述
* 过弱或过强的 claim
* 图表 caption 不正式
* Results 叙述不够有说服力
* Discussion / Limitations 过度自我削弱
* 可复现性信息不够清楚
* baseline / metric / device / workload 描述不够完整
* 表格过宽或排版不适合投稿
* 论文看起来像“从材料包生成”，而不是作者完成的正式论文

# 必须重点清理的表述

请全文查找并删除或改写以下类型的表述：

* available material
* available result artifact
* available artifact
* available data
* the provided material
* the result artifact reports
* the available material reports
* the available material does not contain
* aggregate-only evidence
* aggregate measurements rather than
* raw trace logs are not available
* exact SKU-level metadata is not available
* full confidence intervals are not available
* calibration curve is not available
* this material
* this artifact
* reported artifact
* result package

这些表述容易让论文看起来像“基于不完整材料整理出来”，而不是正式实验论文。

改写原则：

1. 如果原句只是说“available material reports ...”，改成正式论文语气，例如：

   * “Our evaluation reports ...”
   * “We report ...”
   * “The measurements show ...”
   * “The results indicate ...”

2. 如果原句说“没有 raw trace / confidence interval / SKU metadata”，不要生硬地承认材料缺失。应改写为正式 limitation，例如：

   * “Our evaluation focuses on aggregate path-level behavior rather than per-SKU microarchitectural analysis.”
   * “The current study reports aggregate trends and does not attempt to model every device-specific microarchitectural factor.”
   * “We leave exhaustive per-SKU characterization to deployment-specific follow-up studies.”

3. 如果原句过度削弱核心结果，请改成边界清晰但不过度自毁的表述。

4. 不要把真实缺口伪装成已经解决。但也不要用“材料不全”的口吻暴露内部流程。

# 实验与结果成熟化要求

请重点检查并改写 Evaluation / Results / Discussion / Limitations：

1. Evaluation Setup 必须清楚说明：

   * models
   * devices
   * workloads
   * metrics
   * baselines
   * measurement methodology
   * number of runs or aggregation 方式，如果论文中已有
   * energy / latency / memory / quality 的定义

2. 如果某项信息确实缺失，不能编造。但应把它写成合理 limitation，而不是“available material does not contain”。

3. Results 必须围绕 research questions 或 claims 组织。每个主要结果都要回答：

   * 观察到了什么？
   * 为什么发生？
   * 支撑了哪个 claim？
   * 有什么 tradeoff？
   * 有什么边界？

4. 不要只是复述表格数值。需要加入解释性分析。
5. 所有定量 claim 必须由图表或表格支撑。
6. 不要新增、修改、平滑、美化或编造任何实验数值。
7. 如果某个强 claim 没有足够数据支撑，请弱化，而不是编造补充结果。

# Claim 强度校准

请检查全文 claim，并做强度校准：

1. 如果结果支持强 claim，可以写：

   * “The results show ...”
   * “The measurements indicate ...”
   * “The evaluation demonstrates ...”

2. 如果结果只支持有限 setting 下的 claim，应写：

   * “In the evaluated settings ...”
   * “Across the studied workloads ...”
   * “Within the tested device paths ...”

3. 避免：

   * universal claims
   * all devices
   * always
   * guarantees
   * universally improves
   * broadly solves

4. 删除或弱化没有实验结果支撑的过强 novelty / performance claim。

# 图表要求

1. 保留论文中的 figure 和 table。
2. 保证最终 figure 总数不少于 5。
3. 保证最终 table 总数不少于 4。
4. 所有 figure 和 table 都必须在正文中被引用和解释。
5. caption 必须是正式论文 caption。
6. caption 不要出现：

   * placeholder
   * artifact
   * available material
   * protocol
   * TODO
   * future measurement
   * simulated
   * verification

7. 如果有过宽表格，请改成更适合投稿的 LaTeX 排版，例如：

   * 使用 `table*`
   * 使用 `\small`
   * 缩短列名
   * 拆分过宽表格
   * 使用 `p{...}` 列宽

8. 不要删除重要结果表。
9. 不要修改真实实验数值。

# Related Work 和 Novelty

请检查 Related Work 和 Introduction 中的 novelty 表述：

1. 必须清楚说明本文和已有工作的区别。
2. 不要把 novelty 说得过强。
3. 不要说“没有任何已有工作做过 X”，除非 references 和 `novelty_check.md` 明确支持。
4. 更推荐使用：

   * “Unlike prior work that focuses on ..., this paper focuses on ...”
   * “Prior work optimizes ..., whereas our system targets ...”
   * “This distinction matters because ...”

5. 对 `novelty_check.md` 中标记危险的方向，要谨慎表述。

# Method / System Design

请检查方法部分：

1. 方法描述必须具体，而不是概念描述。
2. 模块、输入、输出、状态、控制逻辑、接口必须清楚。
3. 如果有 algorithm，确保它和正文一致。
4. 不要新增不存在的实现细节。
5. 如果实现范围有限，要在 Limitations 中诚实但专业地说明。

# Limitations 改写要求

Limitations 需要保留，但不要过度自我削弱。

可以写：

* evaluation covers representative but not exhaustive devices
* workload diversity is limited to the studied tasks
* policy thresholds may require retuning across hardware generations
* energy measurements may depend on platform telemetry precision
* backend support depends on runtime access to decode-state export/import hooks

不要写：

* available material does not contain
* raw traces are missing
* this artifact lacks
* no confidence intervals are available
* this remains future measurement
* the results are not established

# 引用要求

1. 保持正文实际引用至少 50 篇参考文献。
2. 保持每个 `\cite{}` 有对应 `\bibitem{}`。
3. 保持每个 `\bibitem{}` 至少被正文引用一次。
4. 不要编造参考文献。
5. 不要使用 refs 中不存在的参考文献。
6. 参考文献继续放在 `draft_v5.tex` 末尾的 `thebibliography` 环境中。
7. 不要生成单独 `.bib` 文件。

# 禁止事项

1. 不要修改实验数值。
2. 不要编造新实验。
3. 不要新增没有依据的结果。
4. 不要改变论文核心 idea。
5. 不要删除必要 limitation。
6. 不要删除 references。
7. 不要生成额外文件。
8. 不要输出 patch。
9. 不要输出解释说明。

# 最终内部检查

在生成 `draft_v5.tex` 前，请检查全文是否还存在以下字样或语义等价表达：

* available material
* available artifact
* result artifact
* provided material
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

如果发现，请继续改写，直到论文全文不再包含这些投稿不成熟表述。

同时检查：

* figure 数量不少于 5
* table 数量不少于 4
* 所有图表都被正文引用
* 所有重要结果都有图表或表格支撑
* Abstract / Introduction / Results / Limitations 没有内部材料口吻
* 正文实际引用至少 50 篇参考文献
* 所有 `\cite{}` 和 `\bibitem{}` 匹配
* 每个 `\bibitem{}` 至少被引用一次

上述检查默认只能作为静态核对。若当前会话没有实际运行 LaTeX 编译器或检查脚本，不得声称论文已编译通过或脚本验证通过。

# 输出要求

1. 输出完整 `draft_v5.tex`。
2. 优先提供文件名精确为 `draft_v5.tex` 的可下载文件；无法提供附件时，按“GPT 网页版执行规则”输出完整代码块。
3. 不要输出 patch。
4. 不要输出解释说明。
5. 不要生成额外文件。
6. 不要声称已写入本地或逻辑归档路径。
7. 停止在交付 `draft_v5.tex` 之后。
