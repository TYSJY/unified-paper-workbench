# 第 05 步：在同一个 `paper.tex` 中补全图表与结果证据结构

## 单文件契约

- 本轮只读取上一步的 `paper.tex`。
- 最终只返回更新后的完整 `paper.tex`。
- 本步骤不生成外部图片、CSV 或脚本；它负责在 TeX 内建立完整图表、数据来源和结果叙述结构。
- 不读取目录或要求更早步骤文件。
- 未实际编译时不得声称编译通过。

## 核心目标

把论文改成具有完整 empirical evidence structure 的 submission-style draft，而不是机械堆图。

## 数量与引用要求

1. 最终 figure 总数不少于 5。
2. 最终 table 总数不少于 4。
3. 已有有效图表可计入。
4. 所有图表都必须在正文中被引用和解释。
5. 至少包含主结果、tradeoff/Pareto、ablation、sensitivity/robustness 等证据类型。
6. 概念图通常控制在 1–2 张；不能用大量 workflow 图代替定量证据。
7. 表格必须是 LaTeX table，不能伪装成 figure。
8. 正文仍需实际引用至少 50 篇文献。

## 数据真实性规则

数据来源优先级：

1. 已存在且明确标为用户确认真实的数据；
2. 论文中已有的真实结果；
3. 为搭建结构而构造的待验证模拟数据。

不得把模拟数据写成真实实验结果。

在 preamble 中定义：

```latex
\newcommand{\VERIFY}{\textsuperscript{\textbf{【验】}}}
```

每个待验证数值必须带 `\VERIFY`。每个定量图或数据占位前必须加入：

```latex
% VERIFY_DATA_START: <label>
% DATA_STATUS: SIMULATED_TO_REPLACE
% DATA_PURPOSE: ...
% CSV_SCHEMA: ...
% DATA_ROWS:
% ...
% VERIFY_DATA_END
```

数据块中的方法名、单位、行关系和数值必须与正文、表格、caption 和结果叙述一致。

## 图类型边界

- 概念图：在 figure 环境中放置明确、可替换的 LaTeX 占位与 `% FIGURE_GENERATION_BRIEF` 注释，描述之后应生成的 system architecture、workflow 或 method overview。不要伪装成已经存在的外部图片。
- 定量图：可使用自包含 PGFPlots/TikZ 数据图，或保留清晰的数据图占位；所有数值必须来自真实数据或带 `\VERIFY` 的模拟数据。
- 禁止让概念图承载精确实验曲线、坐标轴或大量数值。

## 结果结构

围绕 research questions 或 claims 组织 Evaluation / Results。每个主要结果必须回答：

- 观察到了什么；
- 为什么会发生；
- 支持哪个 claim；
- 由哪张图或哪张表支持；
- 有何边界或 tradeoff。

加入必要的 algorithm/伪代码、evaluation setup、baseline、metrics、device/workload 定义和 limitations。缺失信息必须明确标注，不得编造。

## 元数据

更新 `WORKFLOW_METADATA`：

```latex
% stage: 05_evidence_structure_complete
% data_status: SIMULATED_TO_REPLACE | PARTIAL_CONFIRMED | CONFIRMED
% figure_count: ...
% table_count: ...
```

最终只交付完整 `paper.tex`。
