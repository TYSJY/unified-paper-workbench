$ml-paper-writing

本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

规则：

- 不要自己猜日期。
- 不要根据今天日期临时编一个 ID。
- 如果用户或上游任务已经指定 idea ID，就使用该 ID。
- 如果没有指定 idea ID，请先查看 `papers/` 目录，并选择刚刚生成 `draft_v0.tex`、且需要升级到 `draft_v1.tex` 的那个 idea ID。
- 如果无法确定唯一目标 idea ID，请停止并要求用户指定，不要继续执行。
- `draft_v1.tex` 不需要额外编号；编号属于父目录 `papers/YYYY-MM-DD_XXX_short-title/`。
- 不要创建 `draft_v2.tex`、`draft_001.tex` 或其他草稿文件。
- 不要覆盖不属于该 idea ID 的 paper 目录。

读取下面这个论文初稿：

papers/YYYY-MM-DD_XXX_short-title/draft_v0.tex

同时读取：

ideas/YYYY-MM-DD_XXX_short-title.md
refs/YYYY-MM-DD_XXX_short-title/references.md
refs/YYYY-MM-DD_XXX_short-title/literature_map.md
refs/YYYY-MM-DD_XXX_short-title/novelty_check.md

如果存在下面这个文件，也一起读取：

papers/YYYY-MM-DD_XXX_short-title/paper_materials.md

现在请把 draft_v0.tex 从初稿骨架提升为 submission-level paper draft。

只创建一个输出文件：

papers/YYYY-MM-DD_XXX_short-title/draft_v1.tex

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件、refs 目录、draft_v0.tex 所在目录一致。

不要创建其他文件。

核心目标：

把当前 draft_v0.tex 从“proposal-style draft”改写成“可以继续投稿打磨的完整论文稿”。

请重点完成以下事情：

1. 删除或改写所有明显的 draft/proposal 表述，例如：

   - This draft proposes
   - in this version
   - we do not claim results
   - this paper should
   - TODO-style proposal wording

2. 将论文改写成正式投稿论文语气：

   - 本文提出什么方法 / framework / system
   - 本文解决什么问题
   - 本文相对已有工作的新意是什么
   - 本文贡献是什么
   - 本文的技术内容如何工作

3. 强化 Introduction：

   - 第一段讲清楚背景和重要性
   - 第二段讲清楚现有方法的核心不足
   - 第三段讲清楚本文 key insight
   - 第四段讲清楚本文方法
   - 第五段列出明确贡献点
   - 不要写成泛泛 motivation

4. 强化 Related Work：

   - 不要堆文献
   - 必须按主题组织
   - 每一类相关工作都要说明和本文的区别
   - 必须根据 novelty_check.md 避免过强或不安全的新颖性 claim

5. 强化 Problem Formulation：

   - 给出清晰的问题定义
   - 定义关键变量、约束、目标函数或优化目标
   - 让读者知道本文到底解决什么问题
   - 不要只停留在自然语言描述

6. 强化 Method / Framework / System Design：

   - 把当前高层 idea 展开成完整方法部分
   - 明确系统模块
   - 明确输入、输出、状态变量、控制策略
   - 明确关键算法步骤
   - 如果适合，请加入 LaTeX algorithm 环境或伪代码
   - 方法部分必须看起来像论文贡献，而不是 idea 描述

7. 增加 Evaluation 或 Experimental Setup 章节，前提是 paper_materials.md 中提供了真实材料。

   - 如果 paper_materials.md 中有真实结果，请基于真实结果写 Results
   - 如果没有真实结果，不要编造任何结果
   - 如果缺少达到投稿水平所需的关键信息，请在对应位置写：
     \textbf{BLOCKING TODO: ...}
   - 不要虚构 latency、memory、energy、accuracy、perplexity、throughput、ablation 等结果

8. 增加 Figure / Table 占位，但不要编造结果：

   - 可以写 figure/table 的 LaTeX skeleton
   - caption 必须说明图表应该展示什么
   - 如果缺少真实数据，在 caption 或正文中标注 \textbf{BLOCKING TODO: add real data}

9. 保持引用完整：

   - 正文实际引用的参考文献数量仍然至少为 50 篇
   - 每个 \cite{} 必须有对应 \bibitem{}
   - 每个 \bibitem{} 必须至少被正文引用一次
   - 不要编造参考文献
   - 不要使用 refs 中不存在的文献
   - 参考文献继续放在 draft_v1.tex 末尾的 thebibliography 环境中
   - 不要生成单独 .bib 文件

10. 改写 Abstract：

    - 不要说这是 draft
    - 不要说 “we do not claim results”
    - 要像正式论文摘要
    - 包含问题、gap、方法、核心 insight、贡献
    - 如果没有真实结果，不要写具体数值

11. 改写 Limitations：

    - Limitations 应该像正式论文的限制讨论
    - 不要写“本文没有实现、没有结果”这种会直接导致拒稿的表述
    - 如果确实缺少投稿必需内容，用 \textbf{BLOCKING TODO: ...} 标出

12. 全文风格：

    - 使用英文
    - 使用机器学习 / 系统会议论文风格
    - 避免空泛句子
    - 避免宣传式语言
    - 避免过强 claim
    - 避免没有证据支撑的断言
    - 让论文看起来是一个完整 submission draft

13. 最终 draft_v1.tex 必须包含：

    - LaTeX preamble
    - title
    - abstract
    - introduction
    - background / motivation
    - related work
    - problem formulation
    - method / framework / system design
    - evaluation / experimental setup / results，如果有真实材料
    - discussion
    - limitations
    - conclusion
    - thebibliography

14. 如果由于缺少真实材料导致无法达到投稿水平，请不要假装已经达到。
    在 draft_v1.tex 中用 \textbf{BLOCKING TODO: ...} 明确标出所有阻碍投稿的缺失项。

停止在生成 papers/YYYY-MM-DD_XXX_short-title/draft_v1.tex 之后。

