# 使用前准备（ChatGPT 网页版）

开始前，请确保本轮对话已上传或当前 Project 已包含：

1. research_profile.md。
2. 用于编号的 ideas 文件名清单：至少要覆盖执行当日已有的 ideas/YYYY-MM-DD_*.md。可以上传这些文件、上传一份清单，或直接在消息中粘贴完整文件名；如果当日确实没有任何 idea 文件，也必须明确写明“当日无已有 idea 文件”。
3. 建议在可用时启用 Deep Research；否则必须使用 ChatGPT Search 完成检索。

# 执行环境规则

- 你只能使用本轮已上传的文件、当前 Project 中可见的文件及用户在对话中粘贴的内容。你不能查看用户设备上的目录，也不能因为消息里出现了某个路径就声称已经读取该路径。
- 依据附件清单、Project 中可见的文件名和文件内容识别输入。缺少 research_profile.md 时停止，并请用户提供该文件。
- 新 idea 的日期必须使用执行时 Asia/Taipei 时区的真实当前日期。不要从示例、附件日期或上下文猜测日期。
- 近期文献和新颖性判断必须使用 Deep Research 或 ChatGPT Search，并给出可点击的论文、出版方或官方项目来源链接。不得凭记忆补齐或伪造来源。
- 如果搜索功能不可用、检索数量不达标或关键证据不足，必须如实说明并停止作出强结论；不得将最终决定写成 Commit。
- 只交付本提示词要求的最终文件。网页若支持文件生成，请提供 exact filename 的可下载文件；不要声称文件已保存到用户设备或某个用户目录。若无法生成可下载文件，则在回复中标明 exact filename，并用一个独立代码块完整输出该文件，不得省略。

---

读取本轮已上传或当前 Project 中的 research_profile.md。

请为我在 efficient LLM / edge LLM / resource-constrained local LLM inference 方向找到 exactly ONE 最合适的科研 idea。

## 核心原则：不要凭空想 idea

本步骤不是纯 brainstorm。必须先做检索驱动的文献侦察，再选择 idea。

如果没有进行外部检索、没有查看近期论文、系统、benchmark 或框架方向，不允许给出 Commit。

本步骤仍然只创建一个最终 idea 文件；不要生成 refs 文件夹，不要生成 bibliography 文件，不要写论文。但必须使用真实检索结果约束 idea 选择，并在最终 idea 文件中留下简明、可审计的证据轨迹。

## 文献侦察要求

在生成最终 idea 前，必须完成以下侦察：

1. 围绕 research_profile.md 中的方向，检索至少 6 组查询词，例如：
   - edge LLM inference KV cache compression
   - resource constrained LLM inference scheduling
   - speculative decoding edge devices
   - adaptive decoding memory pressure LLM
   - local LLM inference energy latency tradeoff
   - KV cache eviction quantization long context LLM
2. 至少广泛查看 50 篇相关论文、系统、benchmark、框架或技术报告的标题和摘要。
3. 至少重点检查 20 篇最相关工作。优先使用 arXiv 论文页或 PDF、ACM/IEEE/USENIX 出版页、OpenReview、作者或机构官方项目页等第一方来源；Semantic Scholar 可用于发现条目，但关键事实应尽量回到原始论文或官方页面核验。
4. 必须覆盖近 3 年工作；如果发现更早的 foundational work，也要纳入判断。
5. 必须识别：
   - 直接竞争方向
   - 相邻但不同的方向
   - 已经饱和或容易撞车的方向
   - 仍然可能有 gap 的方向
6. 候选 idea 不少于 8 个；进入最终比较的 top candidate 不少于 3 个。
7. 对每个 top candidate，必须基于检索结果判断：
   - novelty risk
   - saturation risk
   - technical depth
   - reviewer defensibility
   - fit to edge/local/resource-constrained LLM inference
8. 如果检索结果显示没有足够证据支持某个 idea 的新颖性，最终决定必须是 Needs literature verification 或 Do not commit，不能写成 Commit。
9. 不允许用“可能”“看起来”“应该有空白”等纯直觉判断替代文献证据。
10. 对扫描的 50 篇和重点检查的 20 篇在内部去重并记录核验轨迹；无需另交分析文件，但最终卡片中的 8–12 篇必须来自这些已检查工作。

## 最终 idea 文件中的证据要求

最终 idea 文件不是完整文献综述，但必须包含可审计的简明证据：

- 至少列出 8–12 个最相关的已检查工作，每项包含 title、year，以及 venue、arXiv 或 official URL 中至少一项。
- 每项都必须给出可点击的原始论文、出版方或官方项目链接。
- 明确说明这些工作为什么没有完全覆盖最终 idea，或者为什么它们对最终 idea 构成风险。
- 明确说明最危险的 3–5 类已有工作。
- 明确说明哪些 claim 在第 2 步必须重点验证。
- 如果检索不足、搜索不可用或数据库访问受限，必须在 idea 文件中明确写出限制，并将最终决定设为 Needs literature verification；不要假装已经完成验证。若连 8 个真实相关工作都无法核验，不要补造条目，应明确列出实际核验数量和缺口。

## 文件命名和编号规则

在生成 idea 文件之前，先依据本轮附件、当前 Project 中可见文件或用户粘贴的文件名清单，识别执行当日已有的 ideas/YYYY-MM-DD_*.md。

新 idea 的逻辑归档路径和文件名格式为：

ideas/YYYY-MM-DD_XXX_short-title.md

规则：

- YYYY-MM-DD 必须是执行时 Asia/Taipei 的真实当前日期。
- XXX 必须是该日期下的下一个可用 3 位序号。
- 只有当所提供的清单明确显示该日期下不存在任何 ideas/YYYY-MM-DD_*.md 时，才使用 001。
- 如果该日期下已有 001、002，则使用 003，以此类推；序号有缺口时仍使用当前最大序号加 1，避免覆盖。
- 如果没有提供足以判断当日现有编号的文件名信息，不要猜测 001，也不要生成最终文件；停止并只请求用户补充执行当日已有 idea 文件名清单，或明确确认当日没有已有文件。
- short-title 使用英文小写 kebab-case，简短表达这个 idea 的主题。
- 完整 idea ID 等于文件名去掉 .md 后的字符串，例如 2026-07-03_004_adaptive-kv-budgeting。
- 不要覆盖任何已有 idea 文件。
- 不要复制或生成新的提示词文件；本提示词本身是可复用模板。

你的内部流程应该包括：读取研究画像、进行外部文献侦察、生成候选 idea、打分、淘汰弱 idea、比较 Top 候选、检查危险近邻工作，最后选出一个最终 idea。

但是你只需要交付最终结果。

不要写论文。
不要生成 refs 文件夹或完整参考文献库。
不要输出 references.bib。
不要起草 related work。
不要实现任何东西。
不要生成额外分析文件。

最终只交付一个 Markdown 文件。ideas/YYYY-MM-DD_XXX_short-title.md 仅表示逻辑归档路径，不代表你能写入用户设备。网页支持文件生成时，提供 exact filename 为 YYYY-MM-DD_XXX_short-title.md 的可下载文件，并注明其逻辑归档路径；不要声称已写入该位置。若不能生成文件，则先标明该 exact filename，再用一个独立 Markdown 代码块完整输出文件内容。

这个文件必须使用下面的固定结构：

# Idea Decision Card

## Idea ID

这里必须填写完整 idea ID，例如 2026-07-03_004_adaptive-kv-budgeting。

## 日期

这里必须填写该 idea ID 中的日期。

## 短标题

这里必须填写该 idea ID 中的 short-title。

## 最终标题

## 一句话 thesis

## 核心研究问题

## 主要瓶颈

## 关键 insight

## 为什么这个 idea 值得做

## 为什么这个 idea 有新颖性

这一节必须引用上面文献侦察中的证据，不能只写直觉判断。

## 为什么这个 idea 有发表潜力

## 为什么这不是纯工程优化

## 为什么这不是 incremental work

## 预期贡献点

## 最接近的相关方向

## 文献侦察摘要

必须列出 8–12 个最相关的已检查工作。每项包含：

- title
- year
- venue / arXiv / official URL 中至少一项
- 可点击的原始论文、出版方或官方项目链接
- 和本 idea 的关系
- 为什么它没有完全覆盖本 idea，或者它为什么危险

## 最危险的已有工作类别

## Top 候选比较摘要

必须简要列出至少 3 个 top candidate 及其被选择或淘汰的理由。

## 第 2 步必须重点验证的 claims

## 什么情况会推翻这个 idea

## Reviewer 2 最可能攻击什么

## 如何防守这个 idea

## 最终信心分数

## 最终决定

最终决定只能是以下三种之一：

- Commit
- Needs literature verification
- Do not commit

停止在交付这个唯一的 idea 文件之后。
