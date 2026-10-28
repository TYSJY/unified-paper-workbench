本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

规则：

* 不要自己猜日期。
* 不要根据今天日期临时编一个 ID。
* 如果用户或上游任务已经指定 idea ID，就使用该 ID。
* 如果没有指定 idea ID，请先查看 `papers/` 目录，并选择刚刚生成 `draft_v1.tex`、且需要升级到 `draft_v2.tex` 的那个 idea ID。
* 如果无法确定唯一目标 idea ID，请停止并要求用户指定，不要继续执行。
* `draft_v2.tex` 不需要额外编号；编号属于父目录 `papers/YYYY-MM-DD_XXX_short-title/`。
* 不要创建 `draft_v3.tex`、`draft_001.tex` 或其他草稿文件。
* 不要覆盖不属于该 idea ID 的 paper 目录。
* 本步骤不要调用 `$ml-paper-writing` 或其他写作 skill。你现在的任务不是普通论文润色，而是图表证据结构设计、结果结构补全和待验证数据嵌入。

读取下面这个论文文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v1.tex

同时读取：

ideas/YYYY-MM-DD_XXX_short-title.md
refs/YYYY-MM-DD_XXX_short-title/references.md
refs/YYYY-MM-DD_XXX_short-title/literature_map.md
refs/YYYY-MM-DD_XXX_short-title/novelty_check.md

现在请在 draft_v1.tex 的基础上，补全论文达到 submission-style draft 所需要的图、表、结果结构和待验证数据。

只创建一个输出文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v2.tex

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 目录、draft_v1.tex 所在目录一致。

不要创建其他文件。

# 核心目标

把 draft_v1.tex 改成一篇图表结构完整、实验叙述完整、结果结构完整的论文稿。

本步骤不是为了机械满足 figure/table 数量，而是为了让论文具有 AI / LLM 推理加速 / 模型压缩 / 量化 / 蒸馏方向 empirical paper 应有的图表证据结构。

# 总体要求

1. 不是额外补充 5 张图，而是保证最终 draft_v2.tex 中的 figure 总数不少于 5 张。
2. 不是额外补充 4 张表，而是保证最终 draft_v2.tex 中的 table 总数不少于 4 张。
3. 已有 figure 和 table 可以计入总数。
4. 如果已有 figure/table 只是内部 TODO 表、无效占位、文字框、表格伪装图、或不适合投稿论文，请改写、替换、升级或转换类型。
5. 如果最终图表数量不足，请在合适位置补充新的 figure/table。
6. 如果论文内容需要，可以超过 5 张图和 4 张表。
7. 所有 figure 和 table 都必须在正文中被引用和解释。
8. 最终 draft_v2.tex 仍然必须是一篇完整 LaTeX 论文，而不是图表清单。
9. 不要删除已有论文核心 idea。
10. 不要把论文改成另一个方向。

# VERIFY 标记要求

请在 LaTeX preamble 中加入：

\newcommand{\VERIFY}{\textsuperscript{\textbf{【验】}}}

所有待验证模拟数据必须保留 `\VERIFY` 标记，例如：

* `23.4\%\VERIFY`
* `1.18$\times$\VERIFY`
* `6.7 ms/token\VERIFY`
* `0.8 PPL\VERIFY`

如果当前 LaTeX 编译环境不支持中文字符，仍然保留 `【验】` 标记，不要改成其他符号。

# 模拟数据原则

如果缺少真实 CSV 数据或真实实验数据，请先通过严格推理构造内部一致的模拟数据，用于形成论文图表结构和结果叙述结构。

但是：

1. 所有模拟数据必须标注 `\VERIFY`。
2. 不允许把带 `\VERIFY` 的数据写成已经完成的真实实验结果。
3. 不要编造真实实验环境、真实硬件测量、真实 speedup、真实能耗结果。
4. 如果某个结果需要真实实验才能确认，请保留为带 `\VERIFY` 的待验证结果。
5. 模拟数据必须内部一致，不能互相矛盾。
6. latency、memory、energy、quality、risk、coverage、accuracy、perplexity、throughput 的趋势必须符合方法机制。
7. 不允许出现不合理的超大提升。
8. 不允许所有指标都无代价提升；必须体现真实 tradeoff。
9. 如果本文方法降低 latency / memory / energy，但可能带来 quality degradation，需要在数据中体现。
10. ablation 数据必须能体现每个模块的贡献。
11. sensitivity 数据必须体现不同 context length、device、resource pressure、bit-width、compression ratio、batch size 等条件下的趋势变化。
12. 所有使用模拟数据的正文句子必须带 `\VERIFY`。
13. 不要写没有 `\VERIFY` 的具体模拟数值。
14. 不要声称这些数值已经通过真实实验验证。

# Embedded Verification Data Requirement

如果缺少真实 CSV 数据，请不要创建外部 CSV 文件作为唯一数据来源。
因为下一步默认只读取 draft_v2.tex，所以所有待验证模拟数据必须直接写入 draft_v2.tex。

对于每个 quantitative evidence figure 或 data-figure placeholder，必须在对应 figure 环境前加入 LaTeX 注释形式的 embedded data block：

% VERIFY_DATA_START: <figure_or_table_label>
% DATA_STATUS: SIMULATED_TO_REPLACE
% DATA_PURPOSE: main result / ablation / sensitivity / robustness / calibration / tradeoff / breakdown
% CSV_SCHEMA: column1,column2,column3,...
% DATA_ROWS:
% row1_value1,row1_value2,row1_value3,...
% row2_value1,row2_value2,row2_value3,...
% VERIFY_DATA_END

要求：

1. 所有模拟数据必须写入 draft_v2.tex。
2. 不要只在外部 CSV schema 中描述数据。
3. 不要只写“需要 main_results.csv”，而不给出当前模拟数据。
4. 如果是 table，模拟数据直接写在 table 单元格中，并在每个模拟数值旁边加 `\VERIFY`。
5. 如果是 figure / plot，由于坐标轴数值必须保持 LaTeX 可编译，plot 内部可以使用纯数字坐标；但必须在：

   * figure caption；
   * 正文解释；
   * embedded data block；
     中明确标出这些 plotted values are verification-required。
6. 如果某个 figure 使用 PGFPlots / TikZ 坐标，坐标中不要写 `\VERIFY` 以免破坏编译；但对应 caption 必须写：
   “All numerical values visualized in this figure are verification-required placeholders documented in the embedded VERIFY_DATA block.”
7. 所有正文中引用这些模拟结果的定量句子必须带 `\VERIFY`。
8. 所有 table 中的模拟数值必须带 `\VERIFY`。
9. 每个 embedded data block 都必须有唯一 label，并与 figure/table 的 `\label{}` 对应。
10. 不允许把没有 embedded data block 的模拟数据写入 quantitative figure 或 table。

示例：

% VERIFY_DATA_START: fig:main_latency_memory
% DATA_STATUS: SIMULATED_TO_REPLACE
% DATA_PURPOSE: main latency-memory-quality comparison
% CSV_SCHEMA: method,latency_ms_per_token,peak_memory_gb,quality_score
% DATA_ROWS:
% FP16,18.2,7.8,78.4
% Quantized,13.9,5.1,76.9
% Ours,12.6,4.6,77.8
% VERIFY_DATA_END

\begin{figure}[t]
\centering
% PGFPlots / TikZ / placeholder plot here
\caption{Latency-memory-quality tradeoff. All plotted numerical values are verification-required placeholders documented in the embedded VERIFY_DATA block.}
\label{fig:main_latency_memory}
\end{figure}

正文中写法示例：

The proposed method reduces latency from 18.2 ms/token\VERIFY to 12.6 ms/token\VERIFY while reducing peak memory from 7.8 GB\VERIFY to 4.6 GB\VERIFY.

# Figure/Table Evidence Requirements

最终 draft_v2.tex 必须满足：

1. figure 总数不少于 5。
2. table 总数不少于 4。
3. conceptual schematic 最多 2 张。
4. quantitative evidence figure 至少 3 张。
5. 如果论文需要，可以超过 5 张 figure 和 4 张 table。
6. figure 和 table 必须类型分明。
7. 不允许把 table 包装成 figure。
8. 不允许把文字框、流程框、boxed text、tabular-in-figure 伪装成实验结果图。
9. 如果 caption 声称是 Pareto、tradeoff、calibration、ablation、sensitivity、robustness、comparison、breakdown、result，则该 figure 必须是 quantitative evidence figure 或 data-figure placeholder，不能是纯文字框或流程图。
10. 数据图后续应由真实数据文件、Python、PGFPlots 或其他脚本生成，不应由 image2 生成。
11. image2 只适合 system architecture、workflow、mechanism、method overview 等概念图。

请先在内部审计 draft_v1.tex 中已有的所有 figure 和 table，并判断每个 figure 属于以下哪类：

* Conceptual schematic：解释方法、系统结构、控制流程、机制。
* Quantitative evidence figure：展示实验结果，例如 latency、memory、energy、quality、risk、coverage、accuracy、perplexity、throughput、ablation、sensitivity、Pareto frontier。
* Hybrid figure：上半部分解释机制，下半部分展示实验结果。
* Table disguised as figure：主要内容是数值表、配置表、对照表、状态表，应改成 table。
* Data-figure placeholder：等待真实数据填充的数据图占位，必须有坐标轴、指标、方法对比、数据 schema。

请根据审计结果执行：

1. 如果已有 figure 是有效 conceptual schematic，可以保留，但总数最多 2 张。
2. 如果已有 figure 是 table disguised as figure，请改成 table 环境。
3. 如果已有 figure 的 caption 声称是 tradeoff / Pareto / calibration / ablation / sensitivity / robustness / comparison / breakdown / result，但内容只是文字框，请替换为真正的数据图占位。
4. 如果 quantitative evidence figure 数量不足 3 张，请新增合适的数据图占位。
5. 数据图占位必须包含：

   * figure title / caption
   * x-axis
   * y-axis
   * compared methods
   * metric name
   * expected CSV schema
   * expected interpretation
   * embedded VERIFY_DATA block
6. 数据图中的模拟数值必须符合论文方法机制，并在正文或 caption 中明确带 `\VERIFY` 标记。
7. 如果使用 PGFPlots / TikZ 画数据图，坐标可使用纯数字以保证 LaTeX 编译，但 caption 必须明确说明 plotted numerical values are verification-required and documented in the embedded VERIFY_DATA block.
8. 不要把没有坐标轴的数据结果图写成文字框。
9. 所有 figure 和 table 都必须在正文中被引用和解释。

# 推荐主文图表结构

推荐主文 figure 结构：

* Figure 1：problem-solution hero figure 或 system/method overview。Conceptual schematic。
* Figure 2：runtime architecture / controller / framework。Conceptual schematic 或 hybrid。
* Figure 3：main quantitative result。必须是 quantitative evidence figure。
* Figure 4：latency-memory-quality tradeoff / Pareto frontier。必须是 quantitative evidence figure。
* Figure 5：ablation study。必须是 quantitative evidence figure。
* Figure 6：sensitivity / robustness / multi-device behavior。quantitative evidence figure。
* Figure 7：optional case study / token-level action timeline / overhead breakdown。可以是 hybrid 或 quantitative evidence figure。

推荐 table 结构：

* Table 1：Models, devices, workloads, and metrics。
* Table 2：Main comparison with baselines。
* Table 3：Ablation study。
* Table 4：Sensitivity analysis。
* Table 5：Failure cases / overhead breakdown / adjacent system comparison，如论文需要。

请注意：

* Main comparison、ablation、sensitivity、robustness 可以同时有 table 和 figure。
* table 负责精确数值。
* figure 负责趋势、对比、tradeoff、Pareto、分布、breakdown。
* 不要用 figure 承担配置表、方法对照表、状态表或文字解释表的功能。

# 不同论文类型的典型 quantitative evidence figures

如果论文偏 LLM 推理加速 / 系统，请优先安排：

* latency comparison
* TTFT / TPOT comparison
* throughput vs batch size
* memory footprint
* KV cache memory usage
* latency breakdown
* throughput-latency curve
* multi-device robustness
* context length sensitivity
* ablation study

如果论文偏模型压缩 / 量化，请优先安排：

* accuracy / perplexity vs bit-width
* accuracy / perplexity vs compression ratio
* memory vs quality
* latency vs quality
* layer-wise sensitivity
* quantization error distribution
* calibration / reliability figure
* ablation study

如果论文偏蒸馏，请优先安排：

* teacher-student performance comparison
* student size vs task score
* latency / memory comparison
* distillation loss ablation
* temperature / loss weight sensitivity
* quality gap analysis

# Evaluation / Results 要求

请新增或重写 Evaluation / Results 相关章节，使其具有完整投稿论文形态。

Results 章节必须围绕 research questions 组织，例如：

* RQ1: Does the method improve the primary efficiency / quality / risk tradeoff?
* RQ2: How does the method compare with the strongest baseline?
* RQ3: Which modules contribute most to the improvement?
* RQ4: How robust is the method across devices, workloads, context lengths, resource pressure, or compression levels?
* RQ5: What is the overhead, failure mode, or safety tradeoff?

每个 RQ 下必须引用对应 figure 或 table。

每个 RQ 必须包含：

1. 观察到什么趋势；
2. 哪个 table 给出精确数值；
3. 哪个 figure 给出趋势或 tradeoff；
4. 这个结果支持哪个 claim；
5. 该结果有什么限制；
6. 哪些数值后续需要真实实验替换，并带 `\VERIFY`。

# 方法部分要求

1. 如果 draft_v1.tex 中还缺少 algorithm，请加入一个 LaTeX algorithm 或 algorithmic-style 伪代码。
2. 算法至少描述：

   * 输入；
   * 状态；
   * 关键决策；
   * 输出；
   * 更新规则。
3. 伪代码不需要 `\VERIFY`，除非里面包含具体数值阈值。
4. 如果出现阈值、超参数、budget，请标注 `\VERIFY`。

# 引用要求

1. 保持正文实际引用至少 50 篇参考文献。
2. 保持每个 \cite{} 有对应 \bibitem{}。
3. 保持每个 \bibitem{} 至少被正文引用一次。
4. 不要编造参考文献。
5. 不要使用 refs 中不存在的参考文献。
6. 参考文献继续放在 draft_v2.tex 末尾的 thebibliography 环境中。
7. 不要生成单独 .bib 文件。

# 必须加入的声明

在 Evaluation Setup 或 Limitations 中加入明确声明：

“All numerical entries marked with `\VERIFY` are verification-required placeholders and must be replaced by real measurements before submission.”

如果使用 embedded VERIFY_DATA block，也加入一句：

“Embedded VERIFY_DATA blocks in the LaTeX source record the current simulated values and the expected CSV schema for replacement by measured results.”

# 输出要求

1. 输出完整 draft_v2.tex。
2. 不要输出 patch。
3. 不要输出解释说明。
4. 不要创建额外文件。
5. 最终 figure 总数不少于 5。
6. 最终 table 总数不少于 4。
7. conceptual schematic 最多 2 张。
8. quantitative evidence figure 至少 3 张。
9. 所有新增或保留的 figure/table 都必须在正文中被引用。
10. 所有模拟数据旁边必须保留 `\VERIFY`。
11. 每个 quantitative evidence figure 或 data-figure placeholder 都必须有 embedded VERIFY_DATA block。
12. 不要删除已有论文核心 idea。
13. 不要把论文改成另一个方向。
14. 停止在生成 papers/YYYY-MM-DD_XXX_short-title/draft_v2.tex 之后。

