$idea-creator

读取 research_profile.md。

请为我在 efficient LLM / edge LLM / resource-constrained local LLM inference 方向找到 exactly ONE 最合适的科研 idea。

## 核心原则：不要凭空想 idea

本步骤不是纯 brainstorm。必须先做检索驱动的文献侦察，再选择 idea。

如果没有进行外部检索、没有查看近期论文/系统/benchmark/框架方向，不允许给出 `Commit`。

本步骤仍然只创建一个最终 idea 文件；不要创建 refs/ 目录，不要创建 bibliography 文件，不要写论文。但必须在内部使用真实检索结果来约束 idea 选择，并在最终 idea 文件中留下简明证据轨迹。

## 文献侦察要求

在生成最终 idea 前，必须完成以下侦察：

1. 围绕 research_profile.md 中的方向，检索至少 6 组查询词，例如：
   - edge LLM inference KV cache compression
   - resource constrained LLM inference scheduling
   - speculative decoding edge devices
   - adaptive decoding memory pressure LLM
   - local LLM inference energy latency tradeoff
   - KV cache eviction quantization long context LLM
2. 至少随机查看 50 篇相关论文、系统、benchmark、框架或技术报告的标题和摘要。
3. 至少重点检查 20 篇最相关工作，优先使用 arXiv、Semantic Scholar、ACM/IEEE/USENIX、OpenReview、官方项目页或论文 PDF。
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
8. 如果检索结果显示没有足够证据支持某个 idea 的新颖性，最终决定必须是 `Needs literature verification` 或 `Do not commit`，不能写成 `Commit`。
9. 不允许用“可能”“看起来”“应该有空白”等纯直觉判断替代文献证据。

## 最终 idea 文件中的证据要求

最终 idea 文件不是完整文献综述，但必须包含可审计的简明证据：

- 至少列出 8-12 个最相关的已检查工作，包含 title、year、venue/arXiv/official URL 中至少一项。
- 明确说明这些工作为什么没有完全覆盖最终 idea，或者为什么它们对最终 idea 构成风险。
- 明确说明最危险的 3-5 类已有工作。
- 明确说明哪些 claim 在第 2 步必须重点验证。
- 如果检索不足或网络/数据库不可用，必须在 idea 文件中明确写出限制，并将最终决定设为 `Needs literature verification`，不要假装已经完成验证。

## 文件命名和编号规则

在创建 idea 文件之前，必须先查看 `ideas/` 目录。

新 idea 文件名必须使用当前实际本地日期和当天的下一个可用序号，格式为：

ideas/YYYY-MM-DD_XXX_short-title.md

规则：

- `YYYY-MM-DD` 必须是运行时的当前实际本地日期，不要从示例、已有文件或上下文中猜测日期。
- `XXX` 必须是该日期下的下一个 3 位序号。
- 如果 `ideas/YYYY-MM-DD_*.md` 不存在，则使用 `001`。
- 如果该日期下已有 `001`、`002`，则使用 `003`，以此类推。
- `short-title` 使用英文小写 kebab-case，简短表达这个 idea 的主题。
- 完整 idea ID 等于文件名去掉 `.md` 后的字符串，例如 `2026-07-03_004_adaptive-kv-budgeting`。
- 不要覆盖任何已有 idea 文件。
- 不要复制或新建新的 prompt 文件；`prompts/01_find_one_idea.md` 始终是可复用模板。

你的内部流程应该包括：读取研究画像、进行外部文献侦察、生成候选 idea、打分、淘汰弱 idea、比较 Top 候选、检查危险近邻工作，最后选出一个最终 idea。

但是你只需要保存最终结果。

不要写论文。
不要创建 refs/ 目录或完整参考文献库。
不要输出 references.bib。
不要起草 related work。
不要实现任何东西。
不要创建额外分析文件。

请在 ideas/ 目录下创建一个 markdown 文件，命名格式为：

ideas/YYYY-MM-DD_XXX_short-title.md

其中 `YYYY-MM-DD_XXX_short-title` 必须按上面的编号规则生成。

这个文件必须使用下面的固定结构：

# Idea Decision Card

## Idea ID

这里必须填写完整 idea ID，例如 `2026-07-03_004_adaptive-kv-budgeting`。

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

必须列出 8-12 个最相关的已检查工作。每项包含：

- title
- year
- venue / arXiv / official URL 中至少一项
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

停止在创建这个 idea 文件之后。
