# 使用前准备（ChatGPT 网页版）

开始前，请在消息中指定一个已经存在的完整 idea ID，或确保本轮已上传文件/当前 Project 中只有一套可唯一识别的目标材料。该套材料必须包括：

- 对应 idea 文件
- references.md
- references.bib
- literature_map.md
- novelty_check.md

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

# 执行环境规则

- 你只能使用本轮已上传的文件、当前 Project 中可见的文件及用户在对话中粘贴的内容。你不能查看用户设备上的 ideas、refs 或 papers 目录，也不能因为消息中出现了路径就声称已经读取该路径。
- 依据附件清单、Project 中可见的文件名、文件内容中的 Idea ID 和用户明确给出的 ID 识别目标材料。
- 如果用户或上游任务已经指定完整 idea ID，就使用该 ID。
- 如果没有指定 ID，但可见输入中恰好只有一套 idea ID 一致的完整材料，可以使用该 ID。
- 如果存在多个候选、材料所属 ID 冲突或不能唯一确定目标，只问这一句并停止：“请提供要处理的完整 idea ID（YYYY-MM-DD_XXX_short-title）。”不要同时询问其他问题，也不要自行选择。
- 确定 ID 后，如果上述任何必需文件未上传或在当前 Project 中不可见，停止并列出缺少的文件；不要猜测其内容。
- 不得凭记忆补充或伪造参考文献。需要复核近期文献或元数据时，必须使用 ChatGPT Search 或 Deep Research，并依据可点击的原始论文、出版方或官方项目来源；搜索不可用或证据不足时诚实停止。
- 输出前至少做 LaTeX 静态一致性检查。只有在当前会话确实有可用编译器且实际运行过编译时，才可以声称“编译通过”；否则只能说明“已做静态检查，未实际编译”，不得虚构编译结果。
- 只交付本提示词要求的 draft_v0.tex。网页若支持文件生成，请提供 exact filename 的可下载文件；不要声称文件已保存到用户设备或某个用户目录。若无法生成文件，则标明 exact filename，并用一个独立代码块完整输出该文件，不得省略。

---

本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

规则：

- 不要自己猜日期。
- 不要根据今天日期临时编一个 ID。
- 如果用户或上游任务已经指定 idea ID，就使用该 ID。
- 如果没有指定 idea ID，只能依据本轮附件或当前 Project 中可见材料判断；只有目标唯一时才能继续。
- 如果无法确定唯一目标 idea ID，按执行环境规则只请求完整 idea ID，然后停止。
- draft_v0.tex 不需要改名或编号；编号属于其逻辑归档父路径 papers/YYYY-MM-DD_XXX_short-title/。
- 不要生成 draft_v1.tex、draft_001.tex 或其他草稿文件。
- 不要把其他 idea ID 的材料混入本次论文，也不要覆盖或冒充不属于该 idea ID 的 paper 内容。

读取本轮已上传或当前 Project 中与下列逻辑归档路径相对应的 idea 文件：

ideas/YYYY-MM-DD_XXX_short-title.md

然后读取同一 idea ID 对应的参考文献材料：

refs/YYYY-MM-DD_XXX_short-title/

其中必须包括：

- references.md
- references.bib
- literature_map.md
- novelty_check.md

这里的 YYYY-MM-DD_XXX_short-title 必须替换为已经确定的完整 idea ID。网页端以可见文件名、Project 中可见的归属关系及文件内容中的 Idea ID 为准，不要求用户设备上的这些路径可被直接访问。

## 写作前强制检查

在起草前完成以下检查：

1. idea 文件中的 Idea ID、参考文献材料所属 ID 和用户指定 ID 必须完全一致。
2. references.md、references.bib、literature_map.md 与 novelty_check.md 之间的 citation key、标题和结论必须能够相互对应。
3. 必须至少有 50 篇可验证的真实参考文献可供正文实际引用。UNVERIFIED 条目不计入这 50 篇。
4. 对可疑、近期或元数据不一致的条目，使用 ChatGPT Search 或 Deep Research 回到 arXiv、出版方、OpenReview、DOI 页面或官方项目页核验。不要增加 references.bib 中不存在的新文献来凑数。
5. 如果可验证参考文献不足 50 篇、必要文件缺失、搜索不可用导致关键条目无法核验，或不同文件存在无法解决的严重冲突，请明确指出并停止写作，不要编造内容。

现在请基于这个 idea 和这些参考文献，写一篇完整论文初稿。

只交付一个输出文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v0.tex

其中 YYYY-MM-DD_XXX_short-title 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件及参考文献材料一致。

不要生成其他文件。

## 写作要求

1. 论文必须严格围绕 ideas/YYYY-MM-DD_XXX_short-title.md 中的最终 idea 展开。
2. 论文的 motivation、problem formulation、method/framework、contribution 和 positioning 必须和 idea 文件保持一致。
3. Related Work 必须基于 references.md、literature_map.md 和 novelty_check.md。
4. 引用必须来自所提供参考文献材料中的真实参考文献。
5. 不要编造参考文献。
6. 不要使用 references.bib 里不存在的文献。
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
17. 如果所提供材料中可验证参考文献不足 50 篇，请先明确指出这一点并停止写作，不要编造文献。
18. 如果某个 claim、定义、对比、设计细节、论证材料或必要信息缺失，不要另建文件，直接在论文对应段落中用 \textbf{TODO: ...} 或 \textbf{Missing information: ...} 标出。
19. 如果 novelty_check.md 中指出某些 claim 风险较高，不要把它们写成强 claim；只能写成谨慎表述，并在正文中标注需要进一步确认。
20. 不要编造任何未提供的定量结果、图表结果、表格结果或消融结果。
21. 如果需要具体数据、图表、表格或对比，但当前信息不足，请直接在论文对应位置写成 LaTeX 注释或 TODO 段落。
22. 论文语言使用英文。
23. 论文风格接近机器学习 / 系统方向会议论文。
24. 输出应是一篇可以继续修改的完整 LaTeX 初稿，而不是大纲。
25. bibliography 中的 DOI 或 URL 如果在已核验材料中存在，应保留，并使用合适的 LaTeX 写法使生成 PDF 后的来源可点击；不要为缺失链接编造 URL。
26. 完稿前检查所有 \cite{} key 和 \bibitem{} key：两边集合必须完全对应，不得有未定义引用，也不得有正文未引用的 bibliography 条目。

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

基于所提供的参考文献材料组织 related work。
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
- 不要加入无法验证的参考文献，除非确有必要且在对应 \bibitem{} 中明确标注为 unverified；这类条目不计入至少 50 篇 verified 文献的门槛。
- 不要引用未出现在 bibliography 中的 key。

\end{thebibliography}

## 交付规则

最终只交付 exact filename 为 draft_v0.tex 的文件，并注明其逻辑归档路径是：

papers/YYYY-MM-DD_XXX_short-title/draft_v0.tex

上述 papers 路径仅表示逻辑归档位置。网页支持文件生成时，只提供可下载的 draft_v0.tex；不要声称已经把它写入用户设备上的 papers 目录。若无法生成可下载文件，则标明 exact filename：draft_v0.tex，并用一个独立的 LaTeX 代码块完整输出文件内容，不得使用摘要、节选、占位语或“其余同上”。

不要生成 ZIP，因为本步骤只有一个输出文件。
不要生成其他文件。

停止在交付 draft_v0.tex 之后。
