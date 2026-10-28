# GPT 网页版提示词：补全图表、结果结构并生成 `draft_v2.tex`

## 使用前准备

GPT 网页版不能直接读取你电脑上的 `papers/`、`ideas/`、`refs/` 或数据目录。运行本提示词前，请在本轮对话上传下列文件，或确保它们已经存在于当前 ChatGPT Project 且本轮可访问：

- 必需：`draft_v1.tex`
- 必需：`YYYY-MM-DD_XXX_short-title.md`（idea 文件）
- 必需：`references.md`
- 必需：`literature_map.md`
- 必需：`novelty_check.md`
- 强烈建议：真实实验 CSV、表格、日志摘要、结果文件或用户明确提供的实测数据

完整 idea ID 的格式为 `YYYY-MM-DD_XXX_short-title`。最好在上传时直接说明 idea ID，并确保 idea 文件名包含该完整 ID。输入优先按照实际附件文件名和 Project 文件名识别，而不是按照用户电脑上的目录位置识别。

原流程中的 `papers/YYYY-MM-DD_XXX_short-title/draft_v1.tex`、`ideas/...`、`refs/...` 仅表示用户后续保存时的逻辑归档路径。ChatGPT 不得声称已经访问、创建或写入这些本机路径。

## 执行规则

1. 只使用本轮附件和当前 ChatGPT Project 中实际可访问的文件；不要尝试自动遍历用户电脑目录。
2. 优先使用用户明确指定的完整 idea ID。若用户未指定，只能根据实际附件文件名、Project 文件名或文件内容中明确出现且相互一致的完整 ID 确认。不要猜日期，也不要按今天日期编 ID。
3. 如果无法唯一确定 idea ID，或缺少任一必需输入，只询问用户提供“完整 idea ID”和/或“缺失附件”；不要开始改稿，也不要提出无关问题。
4. 必须确认 idea 文件、参考资料与 `draft_v1.tex` 属于同一 idea ID。若存在冲突，停止并指出冲突文件名。
5. 只生成一个最终交付物，文件名必须精确为 `draft_v2.tex`。`papers/YYYY-MM-DD_XXX_short-title/draft_v2.tex` 只是逻辑归档位置；不要创建 `draft_v3.tex`、`draft_001.tex`、外部 CSV、单独 `.bib`、图片或其他文件。
6. 优先把完整结果作为可下载附件 `draft_v2.tex` 提供。如果当前界面无法生成文件附件，则直接输出完整内容，放在一个 `latex` 代码块中，并在代码块前写明“请将以下完整内容保存为 `draft_v2.tex`”。不得只输出摘要、diff、patch 或解释，也不得声称已经写入用户电脑。
7. 若当前 ChatGPT 环境有可用的数据分析/Python能力，可用它检查数值一致性或生成可嵌入 LaTeX 的数据结构；没有时也必须完成严格静态检查。若没有可用的 LaTeX 编译器，只能说明“已做静态检查，未实际编译”，绝不能虚构编译成功。
8. 完成 `draft_v2.tex` 后停止，不继续执行后续步骤。

---

现在请读取本轮附件或当前 Project 中对应同一完整 idea ID 的 `draft_v1.tex`、idea 文件、`references.md`、`literature_map.md`、`novelty_check.md`，以及用户上传的真实实验数据（如果有）。

在 `draft_v1.tex` 的基础上，补全达到 submission-style draft 所需要的图、表、结果结构和待验证数据，并生成完整的 `draft_v2.tex`。不要删除已有论文核心 idea，也不要把论文改成另一个方向。

# 核心目标

把 `draft_v1.tex` 改成一篇图表结构完整、实验叙述完整、结果结构完整的论文稿。

本步骤不是为了机械满足 figure/table 数量，而是为了让论文具有 AI、LLM 推理加速、模型压缩、量化或蒸馏方向 empirical paper 应有的图表证据结构。

# 总体要求

1. 不是额外补充 5 张图，而是保证最终 `draft_v2.tex` 中的 figure 总数不少于 5 张。
2. 不是额外补充 4 张表，而是保证最终 `draft_v2.tex` 中的 table 总数不少于 4 张。
3. 已有 figure 和 table 可以计入总数。
4. 如果已有 figure/table 只是内部 TODO 表、无效占位、文字框、表格伪装图或不适合投稿论文，应改写、替换、升级或转换类型。
5. 如果最终图表数量不足，在合适位置补充新的 figure/table。
6. 如果论文内容需要，可以超过 5 张图和 4 张表。
7. 所有 figure 和 table 都必须在正文中被引用和解释。
8. 最终 `draft_v2.tex` 必须仍是一篇完整 LaTeX 论文，而不是图表清单。
9. 不要删除已有论文核心 idea。
10. 不要把论文改成另一个方向。

# 数据真实性与图像生成边界

1. 数据来源优先级为：用户上传的真实测量数据 > 用户在消息中明确给出的真实数据 > 明确标记的待验证模拟数据。不得把推测当作真实结果。
2. 若用户提供真实数据，只能依据这些数据计算、制表和作图；不得擅自改值、平滑、美化、补齐缺失结果或声称出现附件未支持的结论。
3. 若缺少真实数据，允许为搭建证据结构而构造内部一致的模拟数据，但必须完全遵守下文 `\VERIFY` 和 `VERIFY_DATA` 规则。任何读者都必须能一眼看出这些数值尚未实测。
4. 定量数据图不得使用 ChatGPT 图像生成功能生成。数据图必须由真实数据或明确标记的模拟数据，通过 PGFPlots/TikZ、表格或可复核的数据分析/Python流程形成，且数据必须嵌入 `.tex`。
5. ChatGPT 图像生成功能只可用于不含实验数值的 system architecture、workflow、mechanism、method overview 等概念图。由于本步骤只允许一个 `.tex` 文件，默认使用自包含 TikZ/LaTeX 概念图；不要为概念图额外生成图片文件。
6. 严禁生成“看起来像真实实验结果”但没有真实数据来源、又未显著标注为模拟待验证的数据图。

# VERIFY 标记要求

请在 LaTeX preamble 中加入：

```latex
\newcommand{\VERIFY}{\textsuperscript{\textbf{【验】}}}
```

所有待验证模拟数据必须保留 `\VERIFY` 标记，例如：

- `23.4\%\VERIFY`
- `1.18$\times$\VERIFY`
- `6.7 ms/token\VERIFY`
- `0.8 PPL\VERIFY`

如果当前 LaTeX 环境可能不支持中文字符，仍然保留 `【验】` 标记，不要改成其他符号；如因此无法编译，只能如实说明静态检查结果，不能删除验证标记来伪装编译通过。

# 模拟数据原则

如果缺少真实 CSV 或真实实验数据，可以通过严格推理构造内部一致的模拟数据，用于形成论文图表结构和结果叙述结构。但是：

1. 所有模拟数据必须标注 `\VERIFY`。
2. 不允许把带 `\VERIFY` 的数据写成已经完成的真实实验结果。
3. 不要编造真实实验环境、真实硬件测量、真实 speedup 或真实能耗结果。
4. 需要真实实验才能确认的结果必须保留为带 `\VERIFY` 的待验证结果。
5. 模拟数据必须内部一致，不能互相矛盾。
6. latency、memory、energy、quality、risk、coverage、accuracy、perplexity、throughput 的趋势必须符合方法机制。
7. 不允许出现不合理的超大提升。
8. 不允许所有指标都无代价提升；必须体现真实 tradeoff。
9. 如果方法降低 latency、memory 或 energy，但可能带来 quality degradation，数据中必须体现这一代价。
10. ablation 数据必须能体现每个模块的贡献。
11. sensitivity 数据必须体现不同 context length、device、resource pressure、bit-width、compression ratio、batch size 等条件下的趋势变化。
12. 所有使用模拟数据的正文句子必须带 `\VERIFY`。
13. 不要写没有 `\VERIFY` 的具体模拟数值。
14. 不要声称这些数值已经通过真实实验验证。
15. 如果同一段混合真实数据与模拟数据，必须逐项区分，不能用一句模糊声明掩盖来源差异。

# Embedded Verification Data Requirement

如果缺少真实 CSV，不要创建外部 CSV 作为唯一数据来源，因为下一步默认只读取 `draft_v2.tex`。所有待验证模拟数据必须直接写入 `draft_v2.tex`。

对于每个 quantitative evidence figure 或 data-figure placeholder，必须在对应 figure 环境前加入以下 LaTeX 注释形式的 embedded data block：

```latex
% VERIFY_DATA_START: <figure_or_table_label>
% DATA_STATUS: SIMULATED_TO_REPLACE
% DATA_PURPOSE: main result / ablation / sensitivity / robustness / calibration / tradeoff / breakdown
% CSV_SCHEMA: column1,column2,column3,...
% DATA_ROWS:
% row1_value1,row1_value2,row1_value3,...
% row2_value1,row2_value2,row2_value3,...
% VERIFY_DATA_END
```

要求：

1. 所有模拟数据必须写入 `draft_v2.tex`。
2. 不要只在外部 CSV schema 中描述数据。
3. 不要只写“需要 `main_results.csv`”，却不给出当前模拟数据。
4. 如果是 table，模拟数据直接写在 table 单元格中，并在每个模拟数值旁边加 `\VERIFY`。
5. 如果是 figure/plot，为保证 LaTeX 坐标可用，plot 内部可以使用纯数字坐标；但必须在 figure caption、正文解释和 embedded data block 中明确说明这些 plotted values 需要验证。
6. 如果使用 PGFPlots/TikZ 坐标，坐标中不要写 `\VERIFY`，以免破坏编译；对应 caption 必须原样或等义写明：`All numerical values visualized in this figure are verification-required placeholders documented in the embedded VERIFY_DATA block.`
7. 所有正文中引用这些模拟结果的定量句子必须带 `\VERIFY`。
8. 所有 table 中的模拟数值必须带 `\VERIFY`。
9. 每个 embedded data block 必须有唯一 label，并与 figure/table 的 `\label{}` 对应。
10. 不允许把没有 embedded data block 的模拟数据写入 quantitative figure 或 table。
11. 如果表格使用模拟数据，也必须建立与其 `\label{}` 对应的 embedded data block；表格单元格与数据块数值必须逐项一致。

示例：

```latex
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
% PGFPlots/TikZ data plot here
\caption{Latency--memory--quality tradeoff. All numerical values visualized in this figure are verification-required placeholders documented in the embedded VERIFY_DATA block.}
\label{fig:main_latency_memory}
\end{figure}
```

正文中的模拟结果写法示例：

```latex
The proposed method reduces latency from 18.2 ms/token\VERIFY{} to 12.6 ms/token\VERIFY{} while reducing peak memory from 7.8 GB\VERIFY{} to 4.6 GB\VERIFY{}.
```

# Figure/Table Evidence Requirements

最终 `draft_v2.tex` 必须满足：

1. figure 总数不少于 5。
2. table 总数不少于 4。
3. conceptual schematic 最多 2 张。
4. quantitative evidence figure 至少 3 张。
5. 如果论文需要，可以超过 5 张 figure 和 4 张 table。
6. figure 和 table 必须类型分明。
7. 不允许把 table 包装成 figure。
8. 不允许把文字框、流程框、boxed text、tabular-in-figure 伪装成实验结果图。
9. 如果 caption 声称是 Pareto、tradeoff、calibration、ablation、sensitivity、robustness、comparison、breakdown 或 result，该 figure 必须是 quantitative evidence figure 或 data-figure placeholder，不能是纯文字框或流程图。
10. 数据图必须由数据驱动，后续可由真实数据文件、数据分析/Python、PGFPlots 或其他可复核方式重绘；不得用图像生成功能伪造。
11. 概念图只适合 system architecture、workflow、mechanism、method overview 等不承载实验数值的内容。

先审计 `draft_v1.tex` 中已有的每个 figure 和 table，并判断每个 figure 属于以下哪类：

- Conceptual schematic：解释方法、系统结构、控制流程、机制。
- Quantitative evidence figure：展示 latency、memory、energy、quality、risk、coverage、accuracy、perplexity、throughput、ablation、sensitivity、Pareto frontier 等实验结果。
- Hybrid figure：同时解释机制并展示实验数据。
- Table disguised as figure：主要内容是数值表、配置表、对照表或状态表，应改成 table。
- Data-figure placeholder：等待真实数据替换的数据图，必须有坐标轴、指标、方法对比、数据 schema 和显著的验证声明。

根据审计结果执行：

1. 有效 conceptual schematic 可以保留，但总数最多 2 张。
2. Table disguised as figure 必须改成 table 环境。
3. caption 声称是 tradeoff/Pareto/calibration/ablation/sensitivity/robustness/comparison/breakdown/result，而内容只是文字框时，必须替换为真正的数据图或明确的数据图占位。
4. quantitative evidence figure 少于 3 张时，新增适合论文机制的数据图。
5. 每个数据图必须包含 figure title/caption、x-axis、y-axis、compared methods、metric name、expected CSV schema、expected interpretation 和 embedded `VERIFY_DATA` block。
6. 模拟数值必须符合论文机制，并在正文与 caption 中明确带验证状态。
7. 使用 PGFPlots/TikZ 时坐标可以为纯数字，但 caption 必须说明所有 plotted values 均需验证并记录在 embedded data block。
8. 不要把没有坐标轴的数据结果图写成文字框。
9. 所有 figure 和 table 都必须在正文中被引用和解释。

# 推荐主文图表结构

推荐 figure 结构：

- Figure 1：problem-solution hero figure 或 system/method overview；conceptual schematic。
- Figure 2：runtime architecture/controller/framework；conceptual schematic 或 hybrid。
- Figure 3：main quantitative result；必须是 quantitative evidence figure。
- Figure 4：latency-memory-quality tradeoff/Pareto frontier；必须是 quantitative evidence figure。
- Figure 5：ablation study；必须是 quantitative evidence figure。
- Figure 6：sensitivity/robustness/multi-device behavior；quantitative evidence figure。
- Figure 7：可选 case study、token-level action timeline 或 overhead breakdown；可为 hybrid 或 quantitative evidence figure。

推荐 table 结构：

- Table 1：Models, devices, workloads, and metrics。
- Table 2：Main comparison with baselines。
- Table 3：Ablation study。
- Table 4：Sensitivity analysis。
- Table 5：Failure cases、overhead breakdown 或 adjacent system comparison（如论文需要）。

Main comparison、ablation、sensitivity、robustness 可以同时有 table 和 figure：table 负责精确数值，figure 负责趋势、对比、tradeoff、Pareto、分布或 breakdown。不要用 figure 承担配置表、方法对照表、状态表或文字解释表的功能。

# 不同论文类型的典型 quantitative evidence figures

如果论文偏 LLM 推理加速/系统，优先安排：latency comparison、TTFT/TPOT comparison、throughput vs batch size、memory footprint、KV cache memory usage、latency breakdown、throughput-latency curve、multi-device robustness、context length sensitivity、ablation study。

如果论文偏模型压缩/量化，优先安排：accuracy/perplexity vs bit-width、accuracy/perplexity vs compression ratio、memory vs quality、latency vs quality、layer-wise sensitivity、quantization error distribution、calibration/reliability figure、ablation study。

如果论文偏蒸馏，优先安排：teacher-student performance comparison、student size vs task score、latency/memory comparison、distillation loss ablation、temperature/loss-weight sensitivity、quality-gap analysis。

# Evaluation / Results 要求

新增或重写 Evaluation/Results 相关章节，使其具有完整投稿论文形态。Results 必须围绕 research questions 组织，例如：

- RQ1: Does the method improve the primary efficiency/quality/risk tradeoff?
- RQ2: How does the method compare with the strongest baseline?
- RQ3: Which modules contribute most to the improvement?
- RQ4: How robust is the method across devices, workloads, context lengths, resource pressure, or compression levels?
- RQ5: What is the overhead, failure mode, or safety tradeoff?

每个 RQ 都必须引用对应 figure 或 table，并包含：

1. 观察到什么趋势；
2. 哪个 table 给出精确数值；
3. 哪个 figure 给出趋势或 tradeoff；
4. 该结果支持哪个 claim；
5. 结果有什么限制；
6. 哪些数值需要真实实验替换，并逐项带 `\VERIFY`。

对于只含模拟数据的 RQ，不得用 completed measurement、measured improvement、demonstrates in real deployment 等语气；应明确这是待实测的结构化占位结果。

# 方法部分要求

1. 如果 `draft_v1.tex` 缺少 algorithm，加入一个 LaTeX algorithm 或 algorithmic-style 伪代码。
2. 算法至少描述输入、状态、关键决策、输出和更新规则。
3. 伪代码不需要 `\VERIFY`，除非包含具体数值阈值。
4. 阈值、超参数或 budget 若不是用户提供的真实设置，必须标注 `\VERIFY`。

# 引用要求

1. 保持正文实际引用至少 50 篇参考文献。
2. 每个 `\cite{}` 必须有对应 `\bibitem{}`。
3. 每个 `\bibitem{}` 必须至少被正文引用一次。
4. 不要编造参考文献。
5. 不要使用已提供文献材料中不存在的参考文献。
6. 参考文献继续放在 `draft_v2.tex` 末尾的 `thebibliography` 环境中。
7. 不要生成单独 `.bib` 文件。

# 必须加入的声明

在 Evaluation Setup 或 Limitations 中加入：

```text
All numerical entries marked with \VERIFY are verification-required placeholders and must be replaced by real measurements before submission.
```

如果使用 embedded `VERIFY_DATA` block，再加入：

```text
Embedded VERIFY_DATA blocks in the LaTeX source record the current simulated values and the expected CSV schema for replacement by measured results.
```

# 交付前内部检查

交付前逐项检查：

1. 输出是完整 `draft_v2.tex`，而不是 patch、解释或片段。
2. figure 总数不少于 5，table 总数不少于 4。
3. conceptual schematic 最多 2 张，quantitative evidence figure 至少 3 张。
4. 所有新增或保留的 figure/table 均在正文中被引用并解释。
5. 所有模拟数值在 table 和正文中逐项保留 `\VERIFY`。
6. 每个定量 figure/data-figure placeholder，以及每个含模拟数据的 table，都有 label 对应、数值一致的 embedded `VERIFY_DATA` block。
7. 未把模拟值说成真实测量，未用图像生成功能制作数据图。
8. 未删除或改变论文核心 idea。
9. 正文实际引用至少 50 篇，`\cite{}` 与 `\bibitem{}` 双向匹配，且没有新增虚构文献。
10. LaTeX 环境、label/reference 和数值单位经过静态检查；若未实际运行编译器，不得声称编译成功。

确认完成后，只交付 `draft_v2.tex`，然后停止。
