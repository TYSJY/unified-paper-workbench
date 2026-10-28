本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

规则：

* 不要自己猜日期。
* 不要根据今天日期临时编一个 ID。
* 如果用户或上游任务已经指定 idea ID，就使用该 ID。
* 如果没有指定 idea ID，请先查看 `papers/` 目录，并选择已经完成真实实验替换、且需要从 `draft_v2.tex` 清理成 `draft_v3.tex` 的那个 idea ID。
* 如果无法确定唯一目标 idea ID，请停止并要求用户指定，不要继续执行。
* `draft_v3.tex` 不需要额外编号；编号属于父目录 `papers/YYYY-MM-DD_XXX_short-title/`。
* 不要创建 `draft_v4.tex`、`draft_001.tex` 或其他草稿文件。
* 不要覆盖不属于该 idea ID 的 paper 目录。

读取下面这个论文文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v2.tex

同时读取：

ideas/YYYY-MM-DD_XXX_short-title.md
refs/YYYY-MM-DD_XXX_short-title/references.md
refs/YYYY-MM-DD_XXX_short-title/literature_map.md
refs/YYYY-MM-DD_XXX_short-title/novelty_check.md

现在假设我已经完成真实实验，并且已经把 draft_v2.tex 中所有待验证模拟数据替换成了真实实验数据。

如果本步骤由用户点击“数据已手动改完并继续”进入，则该确认具有明确含义：用户已经逐项核对并确认当前 `draft_v2.tex` 中的实验数值。即使某个真实实测值与 Step 5 生成的模拟占位值完全相同，该数值也已经由用户确认成真实实验数据，不能因为字节没有变化、仍位于注释数据块中或曾经带有验证标记而将其删除。

你的任务不是简单删除 `【验】` 标记，而是把 draft_v2.tex 从“带验证标记的实验占位稿 / protocol-style draft”转换成一篇干净的、正式的、已完成实验的 empirical submission-style paper。

只创建一个输出文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 目录、draft_v2.tex 所在目录一致。

不要创建其他文件。

# 用户确认数据的保留与转换规则

以下规则优先于“删除验证标记”和“删除占位表述”的文字清理要求。清理的是状态标记和占位语气，不是数据本身。

1. 进入 Step 6 代表当前 `draft_v2.tex` 中所有现有实验数值已经由用户确认。正文、table、figure、caption 以及注释数据块中的数值都属于必须保留的真实数据。
2. 即使用户确认后的真实数值与 Step 5 原先写入的模拟值完全相同，也必须原样保留；不得以“数值没有发生变化”为理由删除、替换或忽略。
3. 禁止删除任何 `% VERIFY_DATA_START ... % VERIFY_DATA_END` 数据块中的 `DATA_PURPOSE`、`CSV_SCHEMA`、`DATA_ROWS`、方法名、字段名、数值或单位。
4. 对每一个验证数据块，只转换状态和边界标记：

   * `% VERIFY_DATA_START: <label>` 改为 `% CONFIRMED_DATA_START: <label>`；
   * `% DATA_STATUS: SIMULATED_TO_REPLACE` 改为 `% DATA_STATUS: USER_CONFIRMED_REAL`；
   * `% VERIFY_DATA_END` 改为 `% CONFIRMED_DATA_END`；
   * `DATA_PURPOSE`、`CSV_SCHEMA` 和每一行 `DATA_ROWS` 必须逐字保留，数值和顺序都不能变化。

5. 如果某些真实数值只存在于验证数据块中、没有在正文或 table 中重复出现，仍然必须把转换后的 `CONFIRMED_DATA` 数据块保留在 `draft_v3.tex` 中，供后续审稿、画图和数据溯源使用。
6. 删除 `\VERIFY`、`【验】`、`placeholder`、`simulated` 等标记时，不得连带删除其旁边的数值、数据行、方法名、figure 数据来源或实验结论。
7. 不要根据数值曾经位于 `SIMULATED_TO_REPLACE` 数据块中而把它判定为未验证；用户进入 Step 6 的确认已经将当前数据升级为真实数据。
8. 生成 `draft_v3.tex` 前，逐个数据块比较 `draft_v2.tex` 与 `draft_v3.tex`：数据块数量、label、schema、数据行数量、方法名和全部数值必须完全一致。发现任何丢失或变化时，必须先恢复原始确认数据，不能通过删除数据块、删除 figure 或减少数据行来通过清理。
9. `CONFIRMED_DATA` 数据块是正式论文源码中的机器可读数据溯源注释，不属于 placeholder、TODO、protocol 或未完成稿表述，不得在本步骤的最终字符串清理中删除。

# 核心目标

将 draft_v2.tex 转换为一篇正式的 empirical paper，而不是 protocol paper、proposal paper、future-work paper 或 claim-evidence planning document。

第六步完成后，论文中不应该再出现任何“实验尚未完成”“未来需要测量”“本文只是评估协议”“本文不声称实验结果”等语气。

# 必须删除的验证标记

请严格执行：

1. 删除所有 `【验】` 标记。

2. 删除所有 `\VERIFY` 标记。

3. 删除 LaTeX preamble 中用于定义 `\VERIFY` 的命令，例如：

   * `\newcommand{\VERIFY}{...}`

4. 删除或改写所有表示数据待验证、占位、模拟、需要替换的表述，包括但不限于：

   * verification-required
   * placeholder
   * simulated data
   * to-be-validated
   * must be replaced by real measurements
   * blocking TODO
   * TODO
   * Missing information
   * verification-required placeholders
   * strict reasoning simulated data
   * UNRESOLVED

5. 如果某句话原本说“这些数值是待验证占位数据”，请删除该句。

6. 如果 Evaluation Setup 或 Limitations 中有关于 `【验】`、`\VERIFY` 或 placeholder data 的声明，请删除。

# 必须改写的 protocol / future-measurement 语气

请全文搜索并删除或改写所有投稿论文中不应出现的未完成稿表述，包括但不限于：

* evaluation protocol
* evaluation plan
* claim-evidence map
* claim-evidence boundary
* claim-evidence plan
* future measurement
* future empirical version
* future experiments should
* future empirical work
* present version
* current version
* in this version
* this draft
* draft paper
* does not establish completed measurements
* does not establish empirical results
* does not present measured speedups
* does not present measured memory reductions
* does not present measured energy savings
* we do not claim measured results
* we do not claim new quantitative results
* remains to be measured
* must be measured in future work
* should be validated in future work
* requires future measurement
* not claimed as established

如果这些表述所在段落表达的是“本文还没做实验”，请根据现在已经完成真实实验这一前提，改写为正式实验论文表述。

例如：

错误表述：

“This section defines an evaluation protocol for future measurement.”

应改写为：

“This section describes the evaluation methodology used to measure latency, memory, energy, and quality under heterogeneous on-device decoding.”

错误表述：

“This paper does not establish completed measurements.”

应改写为：

“This paper evaluates the proposed method across representative on-device settings and reports latency, memory, energy, and quality tradeoffs.”

错误表述：

“The claim-evidence map identifies what must be measured in future work.”

应改写为：

“The results connect each major claim to the corresponding measurement, including main comparisons, ablations, sensitivity analysis, and overhead breakdowns.”

错误表述：

“Future empirical work should validate the handoff overhead.”

应改写为：

“We quantify handoff overhead by decomposing export, transfer, import, synchronization, and continuation-check costs.”

# 数据与实验结果要求

1. 不要删除真实实验数据本身。
2. 不要修改真实实验数据的数值。
3. 不要重新生成、猜测、平滑、美化或编造任何实验结果。
4. 不要添加新的模拟数据。
5. 不要把论文核心 idea 改成另一个 idea。
6. 不要改变论文主张，除非原句仍然是占位稿、草稿、protocol、future measurement 或未完成实验语气。
7. 将所有结果叙述改写成正式论文语气。
8. 将所有图表 caption 改写成正式论文 caption。
9. 保留论文中的 figure 和 table。
10. 保证最终 draft_v3.tex 中 figure 总数不少于 5。
11. 保证最终 draft_v3.tex 中 table 总数不少于 4。
12. 所有 figure 和 table 都必须在正文中被引用和解释。
13. 保证全文没有 `【验】`、`\VERIFY`、placeholder、TODO、BLOCKING TODO、simulated、to-be-validated、future measurement、evaluation protocol、claim-evidence map 等投稿不应出现的字样。

如果你发现仍有明显未替换的数据、空白图表、TODO、占位结果或无法清理的问题，不要假装完成。请在 draft_v3.tex 对应位置保留明确的 `\textbf{UNRESOLVED: ...}`，并不要把它写成真实结果。

本条不适用于已经由用户确认并保存在 `CONFIRMED_DATA` 数据块中的数值；这些数值必须作为真实实验数据保留，不能重新降级为 `UNRESOLVED`。

但是如果内容已经可以清理，请不要新增任何 `UNRESOLVED`。

# 章节改写要求

## Abstract

Abstract 必须是正式 empirical paper 摘要。

必须包含：

* problem
* gap
* proposed method/system
* evaluation setting
* main measured results
* contributions

不允许出现：

* protocol
* future measurement
* placeholder
* does not establish results
* we do not claim results
* present version
* future empirical version

## Introduction

Introduction 必须呈现清晰的：

* problem
* gap
* key insight
* proposed method/system
* measured evidence
* contributions

不允许说本文只是 protocol、proposal、future measurement plan 或 claim-evidence map。

如果原贡献点中有“we define an evaluation protocol”，应改写为更正式的贡献，例如：

* “We implement and evaluate ...”
* “We measure ...”
* “We provide an empirical characterization of ...”
* “We show that ...”

但不要编造数值。

## Background / Motivation

保持背景和动机完整，但删除所有暗示实验尚未完成的语气。

## Related Work

Related Work 必须继续基于已有文献，不要删除关键引用。

如果 related work 中有 “future empirical validation” 或 “this draft only positions”，请改写为正式 positioning 语气。

## Problem Formulation

保持问题定义完整。
不要把问题定义写成未来评估计划。
如果里面出现 “to be measured” 或 “future measurement”，请改成当前论文中已经测量的变量和指标。

## Method / Framework / System Design

保持方法、系统设计、算法、模块描述完整。

如果原文把方法描述成 proposal，请改成正式系统或方法描述。

例如：

* “We propose to design ...” 可以改为 “We design ...”
* “The system would ...” 可以改为 “The system ...”
* “The controller should ...” 可以改为 “The controller ...”

但不要编造新的实现细节。

## Evaluation / Experimental Setup

如果原章节名是：

* Evaluation Protocol
* Evaluation Plan
* Measurement Protocol

请改成：

* Experimental Setup
* Evaluation Methodology
* Evaluation

语气必须是“本文如何评估”，不是“未来应该如何评估”。

例如：

* “We would measure latency ...” 改成 “We measure latency ...”
* “The protocol should compare ...” 改成 “We compare ...”
* “Future experiments should evaluate ...” 改成 “We evaluate ...”

## Results

Results 必须使用正式论文语气。

不允许写：

* would
* expected
* to be measured
* future experiments
* remains to be validated
* not yet measured

如果数据已经在表格或图中，正文应解释这些结果如何支撑 claim。

不要新增或修改真实数据。

## Discussion

可以讨论适用范围、失败场景、tradeoff、部署限制和威胁，但不能说核心结果尚未验证。

## Limitations

Limitations 应保留真实限制，但不能写“没有实验结果”“没有完成测量”或“核心 claim 需要未来验证”。

可以写：

* 设备范围有限
* workload 范围有限
* backend 覆盖有限
* 某些策略依赖硬件 telemetry 精度
* 某些结果可能随硬件平台变化

不要写：

* no measured results
* future measurement required for main claims
* this is only a protocol
* empirical validation remains future work

## Conclusion

Conclusion 必须总结本文完成了什么、实验表明了什么、贡献是什么。

不允许把核心贡献说成未来工作。

# 图表要求

1. 保留论文中的 figure 和 table。
2. 保证最终 figure 总数不少于 5。
3. 保证最终 table 总数不少于 4。
4. 所有 figure 和 table 都必须被正文引用。
5. caption 中不得出现 placeholder、future measurement、to-be-validated、verification-required、protocol plan、claim-evidence map。
6. 如果 caption 说的是待验证图，请改成正式结果图或正式方法图表述。
7. 不要删除图表中的真实实验数据。
8. 不要修改图表中的真实实验数据。

# 引用要求

1. 所有重要 claim 都必须有引用或实验结果支撑。
2. 不要编造参考文献。
3. 不要使用 refs 中不存在的参考文献。
4. 保持正文实际引用至少 50 篇参考文献。
5. 保持每个 \cite{} 有对应 \bibitem{}。
6. 保持每个 \bibitem{} 至少被正文引用一次。
7. 参考文献继续放在 draft_v3.tex 末尾的 thebibliography 环境中。
8. 不要生成单独 .bib 文件。

# 最终内部检查

在生成 draft_v3.tex 前，请在内部检查全文是否还存在以下任一字符串或语义等价表达：

* `【验】`
* `\VERIFY`
* evaluation protocol
* evaluation plan
* measurement protocol
* claim-evidence map
* claim-evidence boundary
* future measurement
* future empirical version
* future experiments should
* present version
* current version
* this draft
* draft paper
* does not establish
* does not present measured
* we do not claim
* remains to be measured
* must be measured in future work
* should be validated in future work
* placeholder
* simulated data
* to-be-validated
* verification-required
* TODO
* BLOCKING TODO
* Missing information
* UNRESOLVED

如果发现这些字符串或语义等价表达，请继续改写，直到全文不再包含它们。

同时检查：

* 是否已将每个 `VERIFY_DATA` 数据块转换为对应的 `CONFIRMED_DATA` 数据块
* 是否完整保留每个确认数据块的 label、schema、方法名、数据行和全部数值
* 是否存在任何从 draft_v2.tex 到 draft_v3.tex 丢失或变化的确认数据
* 是否还有未完成图表
* 是否所有图表都被正文引用
* 是否 figure 数量不少于 5
* 是否 table 数量不少于 4
* 是否正文实际引用至少 50 篇参考文献
* 是否所有 \cite{} 和 \bibitem{} 匹配
* 是否每个 \bibitem{} 至少被正文引用一次

# 输出要求

1. 输出完整 draft_v3.tex。
2. 不要输出 patch。
3. 不要输出解释说明。
4. 不要创建额外文件。
5. 停止在生成 papers/YYYY-MM-DD_XXX_short-title/draft_v3.tex 之后。
