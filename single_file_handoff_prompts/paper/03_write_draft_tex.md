# 第 03 步：把单一 Markdown 交接文件转换成完整 LaTeX 初稿

## 输入与输出契约（最高优先级）

- 本轮只读取上一步返回的 `research_workflow.md`。
- 不要求重新上传研究画像、idea 文件、references、BibTeX 或 novelty check；这些内容必须都已包含在该文件中。
- 不读取本机目录或其他文件。
- 最终只返回一个完整文件，文件名固定为 `paper.tex`。
- 不返回 `.bib`、Markdown、patch、修改说明或其他文件。
- 能生成附件时提供可下载 `paper.tex`；否则用一个 LaTeX 代码块完整输出。
- 只有实际运行过 LaTeX 编译器，才可以声称编译通过；否则只做静态检查。

## 写作门槛

输入文件必须包含：

- 原始研究画像
- Idea Decision Card
- 至少 50 篇 verified references
- BibTeX Library
- Literature Map
- Novelty Check
- Claim-to-Reference Map

如果 verified references 不足 50 篇或关键章节缺失，停止并只列出缺口，不要写论文或编造文献。

## 任务

基于交接文件中的最终 idea 和 verified literature，写一篇完整英文 LaTeX 论文初稿。

## 强制要求

1. motivation、problem formulation、method/framework、contributions 和 positioning 必须与 idea 卡一致。
2. Related Work 必须基于 Literature Map 和 Novelty Check。
3. 只能使用交接文件中 verified references 的 citation keys。
4. 正文实际引用至少 50 篇文献。
5. 每个 `\bibitem{}` 至少被正文 `\cite{}` 一次；每个 `\cite{}` 都有对应 `\bibitem{}`。
6. 所有文献直接写在同一个 `paper.tex` 末尾的 `thebibliography` 中，不依赖外部 `.bib`。
7. 重要 claim 必须有引用或被明确限定。
8. 不得编造实验、数据、图表结果、baseline 或消融结论。
9. 信息不足时，在正文对应位置使用 `\textbf{TODO: ...}` 或 LaTeX 注释明确标出。
10. 论文必须是完整初稿，不是大纲。

## 必需结构

- preamble
- title
- abstract
- introduction
- background and motivation
- related work
- problem formulation
- method / framework / system design
- evaluation plan or evaluation section（没有真实数据时不得伪造结果）
- discussion
- limitations
- conclusion
- `thebibliography`

## 单文件工作流元数据

在 `\end{document}` 之前加入一个不会出现在排版结果中的注释区，保存后续步骤仍需使用的边界信息：

```latex
% <WORKFLOW_METADATA_START>
% stage: 03_initial_draft
% core_idea: ...
% one_sentence_thesis: ...
% novelty_boundaries:
% - ...
% dangerous_prior_work:
% - ...
% claims_requiring_caution:
% - ...
% verified_reference_count: ...
% data_status: NO_CONFIRMED_EXPERIMENTAL_DATA | PARTIAL | CONFIRMED
% <WORKFLOW_METADATA_END>
```

不得在注释区复制整篇 Markdown，但必须保留会约束后续改写的核心 idea、novelty 边界、危险 prior work 和数据状态。

输出前静态检查 LaTeX 环境、引用键和 bibliography 一致性。最终只交付完整 `paper.tex`。
