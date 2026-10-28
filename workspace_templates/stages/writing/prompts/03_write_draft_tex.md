$ml-paper-writing

本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

规则：

- 不要自己猜日期。
- 不要根据今天日期临时编一个 ID。
- 如果用户或上游任务已经指定 idea ID，就使用该 ID。
- 如果没有指定 idea ID，请先查看 `refs/` 目录，并选择刚刚完成参考文献收集、且需要写初稿的那个 idea ID。
- 如果无法确定唯一目标 idea ID，请停止并要求用户指定，不要继续执行。
- `draft_v0.tex` 不需要改名或编号；编号属于父目录 `papers/YYYY-MM-DD_XXX_short-title/`。
- 不要创建 `draft_v1.tex`、`draft_001.tex` 或其他草稿文件。
- 不要覆盖不属于该 idea ID 的 paper 目录。

读取下面这个 idea 文件：

ideas/YYYY-MM-DD_XXX_short-title.md

然后读取对应的参考文献目录：

refs/YYYY-MM-DD_XXX_short-title/

其中至少包括：

- references.md
- references.bib
- literature_map.md
- novelty_check.md

现在请基于这个 idea 和这些参考文献，写一篇完整论文初稿。

只创建一个输出文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v0.tex

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 目录一致。

不要创建其他文件。

写作要求：

1. 论文必须严格围绕 ideas/YYYY-MM-DD_XXX_short-title.md 中的最终 idea 展开。
2. 论文的 motivation、problem formulation、method/framework、contribution 和 positioning 必须和 idea 文件保持一致。
3. Related Work 必须基于 refs/ 目录中的 references.md、literature_map.md 和 novelty_check.md。
4. 引用必须来自 refs/ 目录中的真实参考文献。
5. 不要编造参考文献。
6. 不要使用 refs/references.bib 里不存在的文献。
7. 不要输出单独的 .bib 文件。
8. 不要要求外部 bibliography 文件。
9. 所有参考文献都必须直接写在 draft_v0.tex 文件末尾的 thebibliography 环境中。
10. 正文中的引用使用 \cite{}，对应的 \bibitem{} 必须出现在同一个 draft_v0.tex 文件中。
11. 正文中实际引用的参考文献数量必须至少为 50 篇。
12. thebibliography 中至少包含 50 个被正文实际引用过的 \bibitem{}。
13. 不允许只在参考文献列表中堆满 50 篇但正文不引用。
14. 每个 \bibitem{} 都必须至少在正文中被 \cite{} 一次。
15. 每个重要 claim 都要有对应引用。
16. Introduction、Background、Related Work、Problem Formulation、Method/Framework、Discussion 中都应该合理分布引用，不要把 50 篇引用集中堆在 Related Work 一节。
17. 引用簇必须遵守以下约束：
    - 每一个引用位置最多引用 3 篇参考文献；单个 \cite{} 最多包含 3 个引用 key。
    - 禁止把大量文献压缩为连续编号区间（例如 `[13--25]` 或 `[13–25]`），也不要生成一次包含 4 篇及以上文献的引用簇（例如 `[31,33,35,46,56,67]`）。
    - 如果同一主题相关文献超过 3 篇，必须按它们实际支撑的具体 claim 分散到相邻句子或段落中；不得因此删除、编造或错配文献。
18. 如果 refs/ 目录中可验证参考文献不足 50 篇，请先明确指出这一点，并停止写作，不要编造文献。
19. 如果某个 claim、定义、对比、设计细节、论证材料或必要信息缺失，不要另建文件，直接在论文对应段落中用 \textbf{TODO: ...} 或 \textbf{Missing information: ...} 标出。
20. 如果 novelty_check.md 中指出某些 claim 风险较高，不要把它们写成强 claim；只能写成谨慎表述，并在正文中标注需要进一步确认。
21. 不要编造任何未提供的定量结果、图表结果、表格结果或消融结果。
22. 如果需要具体数据、图表、表格或对比，但当前信息不足，请直接在论文对应位置写成 LaTeX 注释或 TODO 段落。
23. 论文语言使用英文。
24. 论文风格接近机器学习 / 系统方向会议论文。
25. 输出应是一篇可以继续修改的完整 LaTeX 初稿，而不是大纲。

draft_v0.tex 请包含：

- LaTeX preamble
- title
- abstract
- introduction
- background and motivation
- related work
- problem formulation
- method / framework / system design
- discussion
- limitations
- conclusion
- references using thebibliography

论文结构建议如下：

\section{Introduction}

需要讲清楚：

- 背景问题
- 当前方法的不足
- 本文 idea 的核心观察
- 本文的 thesis
- 本文的贡献点

\section{Background and Motivation}

解释该问题为什么重要，以及为什么在 efficient LLM / edge LLM / resource-constrained local LLM inference 场景下尤其重要。

\section{Related Work}

基于 refs/ 目录中的文献组织 related work。
不要简单罗列文献，要按主题归类，并突出本文和已有工作的区别。
这一节应覆盖该 idea 相关的主要文献簇，并引用足够多的核心文献。

\section{Problem Formulation}

形式化定义本文要解决的问题、约束、目标和关键变量。
如果需要符号、假设或定义但当前信息不足，请直接用 TODO 标出。

\section{Method}

如果 idea 更偏系统或框架，可以将本节命名为：

\section{Framework}

或：

\section{System Design}

本节必须围绕 idea 文件中的 key insight 和 expected contribution claims 展开。
如果某些机制细节尚未确定，请在对应段落中写出 TODO，而不是编造。

\section{Discussion}

讨论该 idea 的适用范围、潜在优势、可能限制，以及和已有工作的关系。

\section{Limitations}

清楚说明当前版本的限制和需要后续补充的部分。
所有缺失信息直接写在本节或相关正文段落中，不要另建文件。

\section{Conclusion}

总结本文问题、核心 insight 和贡献。

\begin{thebibliography}{99}

在这里直接写入所有被正文引用到的参考文献。

要求：

- 至少 50 个 \bibitem{}。
- 每个 \bibitem{} 都必须在正文中被 \cite{}。
- 不要加入正文没有引用的文献。
- 不要加入无法验证的参考文献，除非明确标注为 unverified。
- 不要引用未出现在 bibliography 中的 key。

\end{thebibliography}

停止在生成 papers/YYYY-MM-DD_XXX_short-title/draft_v0.tex 之后。
