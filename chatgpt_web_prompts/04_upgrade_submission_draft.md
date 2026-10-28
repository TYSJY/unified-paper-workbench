# GPT 网页版提示词：将 `draft_v0.tex` 升级为投稿级 `draft_v1.tex`

## 使用前准备

GPT 网页版不能直接读取你电脑上的 `papers/`、`ideas/`、`refs/` 目录，也不能自动遍历本机路径。运行本提示词前，请在本轮对话上传下列文件，或确保它们已经存在于当前 ChatGPT Project 且本轮可访问：

- 必需：`draft_v0.tex`
- 必需：`YYYY-MM-DD_XXX_short-title.md`（idea 文件）
- 必需：`references.md`
- 必需：`literature_map.md`
- 必需：`novelty_check.md`
- 可选：`paper_materials.md`（存在时必须一并使用）

完整 idea ID 的格式为 `YYYY-MM-DD_XXX_short-title`。最好在上传时直接说明 idea ID，并确保 idea 文件名包含该完整 ID。如果同一对话或 Project 中有多组论文材料，请只指定本次要处理的一组。

输入应优先按实际附件文件名和 Project 文件名识别。原流程中的 `papers/YYYY-MM-DD_XXX_short-title/draft_v0.tex`、`ideas/...`、`refs/...` 与 `papers/YYYY-MM-DD_XXX_short-title/draft_v1.tex` 仅表示用户下载后的逻辑归档路径；ChatGPT 不得声称已经访问、创建或写入这些本机路径。

## 执行规则

1. 只使用本轮附件和当前 ChatGPT Project 中实际可访问的文件；不要声称读取了用户电脑目录，也不要要求或尝试自动扫描本机路径。
2. 优先使用用户明确指定的完整 idea ID。若用户未明确指定，只能根据已上传文件名、Project 文件名或文件内容中明确出现且相互一致的完整 ID 确认，不能猜日期、不能用今天日期临时编 ID。
3. 如果无法唯一确定 idea ID，或缺少任一必需输入，只询问用户提供“完整 idea ID”和/或“缺失附件”；不要先进行论文改写，也不要提出与阻塞无关的问题。
4. 必须确认 idea 文件、参考资料和 `draft_v0.tex` 属于同一个 idea ID。若材料明显来自不同项目，停止并指出冲突文件名，等待用户补齐正确材料。
5. 本任务只生成一个最终交付物，文件名必须精确为 `draft_v1.tex`。不要生成 `draft_v2.tex`、`draft_001.tex`、单独的 `.bib` 或任何其他文件。
6. 优先把完整结果作为可下载附件 `draft_v1.tex` 提供。如果当前界面无法生成文件附件，则直接输出完整内容，放在一个 `latex` 代码块中，并在代码块前写明“请将以下完整内容保存为 `draft_v1.tex`”。不得只给摘要、片段、diff 或 patch，也不得声称文件已经写入用户电脑。
7. 输出前进行静态一致性检查。若当前 ChatGPT 环境没有可用的 LaTeX 编译器，只能明确说明“已做静态检查，未实际编译”，绝不能虚构编译成功；该说明应放在文件交付说明中，不要写入论文正文。
8. 完成 `draft_v1.tex` 后停止，不继续执行后续步骤。

---

现在请读取本轮附件或当前 Project 中对应同一完整 idea ID 的以下内容：

- `draft_v0.tex`
- idea 文件 `YYYY-MM-DD_XXX_short-title.md`
- `references.md`
- `literature_map.md`
- `novelty_check.md`
- `paper_materials.md`（如果提供）

在保留论文核心 idea、技术方向和已提供事实的前提下，把 `draft_v0.tex` 从初稿骨架提升为 submission-level paper draft，并生成完整的 `draft_v1.tex`。

## 核心目标

把当前 `draft_v0.tex` 从 proposal-style draft 改写成可以继续投稿打磨的完整论文稿，而不是把论文改成另一个方向。

请重点完成以下工作：

1. 删除或改写所有明显的 draft/proposal 表述，例如：

   - `This draft proposes`
   - `in this version`
   - `we do not claim results`
   - `this paper should`
   - TODO-style proposal wording

2. 将全文改写成正式投稿论文语气：

   - 清楚说明本文提出什么方法、framework 或 system；
   - 清楚说明本文解决什么问题；
   - 清楚说明相对已有工作的差异与新意；
   - 清楚说明本文贡献；
   - 清楚说明技术内容如何工作。

3. 强化 Introduction：

   - 第一段讲清楚背景和重要性；
   - 第二段讲清楚现有方法的核心不足；
   - 第三段讲清楚本文 key insight；
   - 第四段讲清楚本文方法；
   - 第五段列出明确贡献点；
   - 不要写成泛泛 motivation。

4. 强化 Related Work：

   - 不要堆文献；
   - 必须按主题组织；
   - 每一类相关工作都要说明与本文的区别；
   - 必须依据 `novelty_check.md` 避免过强、不安全或没有证据支持的新颖性 claim。

5. 强化 Problem Formulation：

   - 给出清晰的问题定义；
   - 定义关键变量、约束、目标函数或优化目标；
   - 让读者明确本文到底解决什么问题；
   - 不要只停留在自然语言描述。

6. 强化 Method / Framework / System Design：

   - 把当前高层 idea 展开成完整方法部分；
   - 明确系统模块；
   - 明确输入、输出、状态变量、控制策略；
   - 明确关键算法步骤；
   - 如果适合，加入可编译的 LaTeX algorithm 环境或伪代码；
   - 方法部分必须看起来像论文贡献，而不是 idea 描述；
   - 不得补写附件中没有依据的实现事实。

7. 增加 Evaluation 或 Experimental Setup 章节，前提是 `paper_materials.md` 或其他已上传材料中提供了真实实验材料：

   - 如果材料中有真实结果，只能基于这些真实结果撰写 Results；
   - 如果没有真实结果，不要编造任何结果；
   - 如果缺少达到投稿水平所需的关键信息，在对应位置写 `\textbf{BLOCKING TODO: ...}`；
   - 不要虚构 latency、memory、energy、accuracy、perplexity、throughput、ablation 等结果；
   - 不要把概念性描述写成已经完成的测量。

8. 增加 Figure / Table 占位，但不要编造结果：

   - 可以写 figure/table 的 LaTeX skeleton；
   - caption 必须说明图表应该展示什么；
   - 如果缺少真实数据，在 caption 或正文中标注 `\textbf{BLOCKING TODO: add real data}`；
   - 数据图必须来自附件中真实数据，或保持为明确的空白数据结构占位；不能使用 ChatGPT 图像生成功能生成看似真实的实验数据图；
   - ChatGPT 图像生成功能只适合概念性 architecture、workflow 或 mechanism 图。由于本任务只允许一个 `.tex` 输出，默认优先使用自包含的 TikZ/LaTeX 概念图或明确占位，不额外生成图片文件。

9. 保持引用完整：

   - 正文实际引用的参考文献数量仍然至少为 50 篇；
   - 每个 `\cite{}` 必须有对应 `\bibitem{}`；
   - 每个 `\bibitem{}` 必须至少被正文引用一次；
   - 不要编造参考文献；
   - 不要使用已提供 references/文献材料中不存在的文献；
   - 参考文献继续放在 `draft_v1.tex` 末尾的 `thebibliography` 环境中；
   - 不要生成单独 `.bib` 文件。

10. 改写 Abstract：

    - 不要说这是 draft；
    - 不要说 `we do not claim results`；
    - 要像正式论文摘要；
    - 包含问题、gap、方法、核心 insight、贡献；
    - 如果没有真实结果，不要写具体数值或暗示已经完成实验。

11. 改写 Limitations：

    - Limitations 应像正式论文的限制讨论；
    - 不要使用“本文没有实现、没有结果”这类会直接导致拒稿的泛化表述；
    - 但不得用语气包装掩盖关键材料缺失；确实缺少投稿必需内容时，用 `\textbf{BLOCKING TODO: ...}` 明确标出。

12. 全文风格：

    - 使用英文；
    - 使用机器学习/系统会议论文风格；
    - 避免空泛句子；
    - 避免宣传式语言；
    - 避免过强 claim；
    - 避免没有证据支撑的断言；
    - 让论文形成完整 submission draft，同时诚实保留所有阻塞项。

13. 最终 `draft_v1.tex` 必须包含：

    - LaTeX preamble
    - title
    - abstract
    - introduction
    - background / motivation
    - related work
    - problem formulation
    - method / framework / system design
    - evaluation / experimental setup / results（仅在有真实材料时写真实结果）
    - discussion
    - limitations
    - conclusion
    - `thebibliography`

14. 如果由于缺少真实材料导致无法达到投稿水平，不要假装已经达到。在 `draft_v1.tex` 中用 `\textbf{BLOCKING TODO: ...}` 明确标出所有阻碍投稿的缺失项。

## 交付前内部检查

在交付前逐项确认：idea ID 唯一且材料一致；只生成 `draft_v1.tex`；论文核心 idea 未被替换；所有具体实验结论都有真实附件依据；所有缺失结果均用 `BLOCKING TODO` 标出；正文引用至少 50 篇且 `\cite{}`/`\bibitem{}` 双向匹配；LaTeX 结构闭合。若没有实际运行编译器，不得把静态检查表述成编译通过。
