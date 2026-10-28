# GPT 网页版提示词：清理已确认实验稿并生成 `draft_v3.tex`

## 使用前准备

本步骤只能在真实实验已经完成、且你已经逐项核对 `draft_v2.tex` 中全部实验数值后执行。GPT 网页版没有原工作流中的“数据已手动改完并继续”按钮，因此请在发送本提示词时同时写出以下明确确认（不要在尚未核实时发送）：

> 我确认：当前上传的 `draft_v2.tex` 中，正文、表格、图、caption 及所有 `VERIFY_DATA` 数据块里的现有实验数值，均已由我逐项核对并确认是真实实验数据；即使某些实测值与原模拟占位值相同，也应作为真实数据原样保留。

同时，请在本轮对话上传下列文件，或确保它们已经存在于当前 ChatGPT Project 且本轮可访问：

- 必需：`draft_v2.tex`
- 必需：`YYYY-MM-DD_XXX_short-title.md`（idea 文件）
- 必需：`references.md`
- 必需：`literature_map.md`
- 必需：`novelty_check.md`

完整 idea ID 的格式为 `YYYY-MM-DD_XXX_short-title`。最好在上传时直接说明 idea ID，并确保 idea 文件名包含该完整 ID。输入优先按照实际附件文件名和 Project 文件名识别，而不是按照用户电脑上的目录位置识别。

GPT 网页版不能直接读取你电脑上的 `papers/`、`ideas/`、`refs/` 目录，也不能自动遍历本机路径。原流程中的 `papers/YYYY-MM-DD_XXX_short-title/draft_v2.tex` 与 `papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex` 仅表示用户下载后的逻辑归档路径。

## 执行规则

1. 只使用本轮附件和当前 ChatGPT Project 中实际可访问的文件；不得声称访问、创建或写入用户电脑目录。
2. 优先使用用户明确指定的完整 idea ID。若未指定，只能根据实际附件文件名、Project 文件名或文件内容中明确出现且相互一致的完整 ID 确认。不要猜日期，也不要按今天日期编 ID。
3. 如果无法唯一确定 idea ID、缺少必需附件或缺少上面的明确数据确认，只询问用户提供“完整 idea ID”“缺失附件”和/或“数据确认声明”；不要先执行清理，也不要提出无关问题。
4. 必须确认 idea 文件、参考资料与 `draft_v2.tex` 属于同一 idea ID；如材料冲突，停止并指出冲突文件名。
5. 只生成一个最终交付物，文件名必须精确为 `draft_v3.tex`。`papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex` 只是逻辑归档位置；不要生成 `draft_v4.tex`、`draft_001.tex`、单独 `.bib`、外部数据文件或其他文件。
6. 优先把完整结果作为可下载附件 `draft_v3.tex` 提供。如果当前界面无法生成文件附件，则直接输出完整内容，放在一个 `latex` 代码块中，并在代码块前写明“请将以下完整内容保存为 `draft_v3.tex`”。不得只输出摘要、diff、patch 或解释，也不得声称已经写入用户电脑。
7. 输出前执行数据块逐项核对与 LaTeX 静态检查。若当前 ChatGPT 环境没有可用的 LaTeX 编译器，只能说明“已做静态检查，未实际编译”，绝不能虚构编译成功。
8. 完成 `draft_v3.tex` 后停止，不继续执行其他步骤。

---

现在请读取本轮附件或当前 Project 中属于同一完整 idea ID 的 `draft_v2.tex`、idea 文件、`references.md`、`literature_map.md` 和 `novelty_check.md`。

用户已经通过明确确认声明表示：真实实验已完成，且当前 `draft_v2.tex` 中所有现有实验数值已经逐项核对。即使某个真实实测值与此前的模拟占位值完全相同，该数值也已经被用户确认为真实数据，不能因为字符没有变化、仍位于注释数据块中或曾经带有验证标记而删除。

你的任务不是简单删除 `【验】` 标记，而是把 `draft_v2.tex` 从带验证标记的实验稿转换成一篇干净、正式、已完成实验的 empirical submission-style paper，并生成完整的 `draft_v3.tex`。

# 用户确认数据的保留与转换规则

以下规则优先于删除验证标记和删除占位语气。清理的是状态标记与占位表述，不是数据本身。

1. 进入本步骤并附带明确数据确认，代表当前 `draft_v2.tex` 中所有现有实验数值均已由用户确认。正文、table、figure、caption 以及注释数据块中的数值都属于必须保留的真实数据。
2. 即使用户确认后的真实数值与此前的模拟值完全相同，也必须原样保留；不得以“数值没有发生变化”为由删除、替换或忽略。
3. 禁止删除任何 `% VERIFY_DATA_START ... % VERIFY_DATA_END` 数据块中的 `DATA_PURPOSE`、`CSV_SCHEMA`、`DATA_ROWS`、方法名、字段名、数值或单位。
4. 对每一个验证数据块，只转换状态和边界标记：

   - `% VERIFY_DATA_START: <label>` 改为 `% CONFIRMED_DATA_START: <label>`；
   - `% DATA_STATUS: SIMULATED_TO_REPLACE` 改为 `% DATA_STATUS: USER_CONFIRMED_REAL`；
   - `% VERIFY_DATA_END` 改为 `% CONFIRMED_DATA_END`；
   - `DATA_PURPOSE`、`CSV_SCHEMA` 与 `DATA_ROWS` 下每一行必须逐字保留，数值、方法名、单位和顺序都不能变化。

5. 如果某些真实数值只存在于验证数据块中、没有在正文或 table 中重复出现，仍必须把转换后的 `CONFIRMED_DATA` 数据块保留在 `draft_v3.tex` 中，供后续审稿、画图和数据溯源使用。
6. 删除 `\VERIFY`、`【验】`、`placeholder`、`simulated` 等状态标记时，不得连带删除其旁边的数值、数据行、方法名、figure 数据来源或实验结论。
7. 不得因为数值曾位于 `SIMULATED_TO_REPLACE` 数据块中就将其判定为未验证；用户在本轮给出的明确确认已经将当前数值升级为真实数据。
8. 生成 `draft_v3.tex` 前，逐个数据块比较输入与输出：数据块数量、label、schema、数据行数量、方法名和全部数值必须完全一致。发现任何丢失或变化时，先恢复输入中的确认数据，不能通过删除数据块、删除 figure 或减少数据行来通过清理。
9. `CONFIRMED_DATA` 数据块是正式论文源码中的机器可读数据溯源注释，不属于未完成稿内容，不得在最终字符串清理时删除。
10. 用户确认只覆盖当前文件里实际存在且可读的数值，不授权你猜测、补齐或生成任何新实验数据。

# 核心目标

将 `draft_v2.tex` 转换为一篇正式 empirical paper，而不是 protocol paper、proposal paper、future-work paper 或 claim-evidence planning document。

完成后，论文中不应再出现任何“实验尚未完成”“未来需要测量”“本文只是评估协议”“本文不声称实验结果”等语气。

# 必须删除的验证标记

严格执行：

1. 删除所有 `【验】` 标记。
2. 删除所有 `\VERIFY` 标记，但保留其相邻的数值、单位、标点和句意。
3. 删除 LaTeX preamble 中用于定义 `\VERIFY` 的命令，例如 `\newcommand{\VERIFY}{...}`。
4. 删除或改写所有表示数据待验证、占位、模拟或需要替换的表述，包括但不限于：

   - `verification-required`
   - `placeholder`
   - `simulated data`
   - `to-be-validated`
   - `must be replaced by real measurements`
   - `blocking TODO`
   - `TODO`
   - `Missing information`
   - `verification-required placeholders`
   - `strict reasoning simulated data`
   - `UNRESOLVED`

5. 如果某句话只是声明“这些数值是待验证占位数据”，删除该句。
6. 如果 Evaluation Setup 或 Limitations 中有关于 `【验】`、`\VERIFY`、embedded verification blocks 或 placeholder data 的待验证声明，删除或改写成正式的数据溯源说明；不要删除确认数据块本身。

# 必须改写的 protocol / future-measurement 语气

全文搜索并删除或改写所有投稿论文中不应出现的未完成稿表述，包括但不限于：

- `evaluation protocol`
- `evaluation plan`
- `claim-evidence map`
- `claim-evidence boundary`
- `claim-evidence plan`
- `future measurement`
- `future empirical version`
- `future experiments should`
- `future empirical work`
- `present version`
- `current version`
- `in this version`
- `this draft`
- `draft paper`
- `does not establish completed measurements`
- `does not establish empirical results`
- `does not present measured speedups`
- `does not present measured memory reductions`
- `does not present measured energy savings`
- `we do not claim measured results`
- `we do not claim new quantitative results`
- `remains to be measured`
- `must be measured in future work`
- `should be validated in future work`
- `requires future measurement`
- `not claimed as established`

如果这些表述所在段落表达的是“本文还没做实验”，请依据当前已确认的真实数据，改写为正式实验论文表述，但不能生成输入中不存在的新结果。

示例：

错误：

```text
This section defines an evaluation protocol for future measurement.
```

改写为：

```text
This section describes the evaluation methodology used to measure latency, memory, energy, and quality under heterogeneous on-device decoding.
```

错误：

```text
This paper does not establish completed measurements.
```

改写为：

```text
This paper evaluates the proposed method across representative on-device settings and reports latency, memory, energy, and quality tradeoffs.
```

错误：

```text
The claim-evidence map identifies what must be measured in future work.
```

改写为：

```text
The results connect each major claim to the corresponding measurement, including main comparisons, ablations, sensitivity analysis, and overhead breakdowns.
```

错误：

```text
Future empirical work should validate the handoff overhead.
```

改写为：

```text
We quantify handoff overhead by decomposing export, transfer, import, synchronization, and continuation-check costs.
```

只有当输入中的结果或数据块确实支持这些完成式陈述时才能使用；若例句涉及输入中不存在的指标，不得照抄成新增 claim。

# 数据与实验结果要求

1. 不要删除真实实验数据本身。
2. 不要修改真实实验数据的数值。
3. 不要重新生成、猜测、平滑、美化或编造任何实验结果。
4. 不要添加新的模拟数据或新的未经用户确认的数值。
5. 不要把论文核心 idea 改成另一个 idea。
6. 不要改变论文主张，除非原句仍是占位稿、草稿、protocol、future measurement 或未完成实验语气；改写后的主张必须由现有数据或引用支持。
7. 将所有结果叙述改写成正式论文语气。
8. 将所有图表 caption 改写成正式论文 caption。
9. 保留论文中的 figure 和 table。
10. 保证最终 `draft_v3.tex` 中 figure 总数不少于 5。
11. 保证最终 `draft_v3.tex` 中 table 总数不少于 4。
12. 所有 figure 和 table 都必须在正文中被引用和解释。
13. 正常完成时，全文不得残留 `【验】`、`\VERIFY`、`placeholder`、`TODO`、`BLOCKING TODO`、`simulated`、`to-be-validated`、`future measurement`、`evaluation protocol`、`claim-evidence map` 等投稿不应出现的字样。

如果发现明显未替换的数据、空白图表、无数据的结果 claim 或无法安全清理的问题，不要把缺失内容伪装成真实结果，也不要生成新数值。应在对应位置保留明确的 `\textbf{UNRESOLVED: ...}`，并在交付说明中指出该稿仍含未解决项。

上一段不适用于已经由用户确认并保存在 `CONFIRMED_DATA` 数据块中的现有数值；这些数值必须作为真实实验数据保留，不能重新降级为未确认状态。如果内容可以安全清理，不要新增任何 `UNRESOLVED`。

如果输入本身少于 5 个 figure 或少于 4 个 table，不得为了凑数编造数据图或结果表；保留 `UNRESOLVED` 说明缺失项。只有输入中已有足够真实材料时，才可将这些材料重组为新增图表，而且不能改动任何数值。

# 章节改写要求

## Abstract

Abstract 必须是正式 empirical paper 摘要，并包含 problem、gap、proposed method/system、evaluation setting、main measured results 和 contributions。

不允许出现 protocol、future measurement、placeholder、does not establish results、we do not claim results、present version 或 future empirical version。摘要中的具体结果必须来自输入中的确认数据，不能新编数值。

## Introduction

Introduction 必须呈现清晰的 problem、gap、key insight、proposed method/system、measured evidence 和 contributions。不得说本文只是 protocol、proposal、future measurement plan 或 claim-evidence map。

如果原贡献点写 `we define an evaluation protocol`，应在数据支持范围内改成正式贡献，例如：

- `We implement and evaluate ...`
- `We measure ...`
- `We provide an empirical characterization of ...`
- `We show that ...`

不要编造数值或输入中没有的实验范围。

## Background / Motivation

保持背景与动机完整，但删除所有暗示实验尚未完成的语气。

## Related Work

Related Work 必须继续基于已有文献，不要删除关键引用。如果存在 `future empirical validation` 或 `this draft only positions` 等表述，改写为正式 positioning 语气。

## Problem Formulation

保持问题定义完整。不要把问题定义写成未来评估计划。若出现 `to be measured` 或 `future measurement`，改成本文已经使用的变量和指标，且必须与确认数据一致。

## Method / Framework / System Design

保持方法、系统设计、算法与模块描述完整。若原文把方法描述成 proposal，改成正式系统或方法描述：

- `We propose to design ...` 可改为 `We design ...`
- `The system would ...` 可改为 `The system ...`
- `The controller should ...` 可改为 `The controller ...`

不要编造新的实现细节。

## Evaluation / Experimental Setup

如果原章节名是 `Evaluation Protocol`、`Evaluation Plan` 或 `Measurement Protocol`，改成 `Experimental Setup`、`Evaluation Methodology` 或 `Evaluation`。

语气必须是“本文如何评估”，不是“未来应该如何评估”：

- `We would measure latency ...` 改成 `We measure latency ...`
- `The protocol should compare ...` 改成 `We compare ...`
- `Future experiments should evaluate ...` 改成 `We evaluate ...`

这些改写必须受现有确认数据和输入所述实验设置约束。

## Results

Results 必须使用正式论文语气，不允许写 `would`、`expected`、`to be measured`、`future experiments`、`remains to be validated` 或 `not yet measured`。若数据已经在表格或图中，正文应解释这些结果如何支持 claim，但不得新增或修改真实数据。

## Discussion

可以讨论适用范围、失败场景、tradeoff、部署限制和威胁，但不能说核心结果尚未验证。

## Limitations

Limitations 应保留真实限制，但不能写“没有实验结果”“没有完成测量”或“核心 claim 需要未来验证”。可以讨论设备范围有限、workload 范围有限、backend 覆盖有限、策略依赖硬件 telemetry 精度，或结果可能随硬件平台变化。不要把核心论文降格为只有评估计划。

## Conclusion

Conclusion 必须总结本文完成了什么、实验表明了什么、贡献是什么；所有结论都必须由现有确认数据或已有引用支持，不得把核心贡献写成未来工作。

# 图表要求

1. 保留论文中的 figure 和 table。
2. 保证最终 figure 总数不少于 5。
3. 保证最终 table 总数不少于 4。
4. 所有 figure 和 table 都必须被正文引用和解释。
5. caption 中不得出现 placeholder、future measurement、to-be-validated、verification-required、protocol plan 或 claim-evidence map。
6. 将待验证 caption 改成正式结果图或正式方法图表述。
7. 不要删除图表中的真实实验数据。
8. 不要修改图表中的真实实验数据。
9. 不得使用 ChatGPT 图像生成功能重新生成定量数据图；保留或用输入中确认的数据重建数据驱动图表。概念图可保留，但不能用图像生成掺入貌似真实的实验数值。

# 引用要求

1. 所有重要 claim 都必须有引用或实验结果支撑。
2. 不要编造参考文献。
3. 不要使用已提供文献材料中不存在的参考文献。
4. 保持正文实际引用至少 50 篇参考文献。
5. 每个 `\cite{}` 必须有对应 `\bibitem{}`。
6. 每个 `\bibitem{}` 必须至少被正文引用一次。
7. 参考文献继续放在 `draft_v3.tex` 末尾的 `thebibliography` 环境中。
8. 不要生成单独 `.bib` 文件。

# 最终内部检查

在生成 `draft_v3.tex` 前，检查全文是否仍存在以下字符串或语义等价表达：

- `【验】`
- `\VERIFY`
- `evaluation protocol`
- `evaluation plan`
- `measurement protocol`
- `claim-evidence map`
- `claim-evidence boundary`
- `future measurement`
- `future empirical version`
- `future experiments should`
- `present version`
- `current version`
- `this draft`
- `draft paper`
- `does not establish`
- `does not present measured`
- `we do not claim`
- `remains to be measured`
- `must be measured in future work`
- `should be validated in future work`
- `placeholder`
- `simulated data`
- `to-be-validated`
- `verification-required`
- `TODO`
- `BLOCKING TODO`
- `Missing information`
- `UNRESOLVED`

如果发现这些字符串或语义等价表达，继续改写，直到正常完成的全文不再包含它们。唯一例外是确实无法安全补全的缺失项：此时保留 `\textbf{UNRESOLVED: ...}`，不得声称论文已完全清理，也不得用虚构数据消除该标记。

同时逐项检查：

1. 每个 `VERIFY_DATA` 数据块都已转换为对应的 `CONFIRMED_DATA` 数据块。
2. 每个确认数据块的 label、`DATA_PURPOSE`、schema、方法名、数据行、单位与全部数值均完整保留。
3. 从 `draft_v2.tex` 到 `draft_v3.tex` 没有任何确认数据丢失或变化。
4. figure 不少于 5，table 不少于 4；所有图表均被正文引用和解释。
5. 没有未完成图表被伪装成真实结果。
6. 正文实际引用至少 50 篇，所有 `\cite{}` 与 `\bibitem{}` 双向匹配。
7. 没有新增虚构文献、实验数值、硬件设置或测量结论。
8. LaTeX 环境、label/reference、命令定义和单位经过静态一致性检查。
9. 若未实际运行 LaTeX 编译器，交付时明确说明只完成静态检查，不能声称编译通过。

确认完成后，只交付完整的 `draft_v3.tex`，然后停止。
