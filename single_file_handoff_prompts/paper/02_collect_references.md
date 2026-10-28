# 第 02 步：在同一个交接文件中补全参考文献与新颖性验证

## 输入与输出契约（最高优先级）

- 本轮只读取用户上传的上一步 `research_workflow.md`。
- 不要求用户再次上传 `research_profile.md`；它应已包含在交接文件中。
- 不读取目录或任何未上传材料。
- 不生成项目编号、日期序号、参考文献文件夹或多个独立文件。
- 在输入文件基础上继续增加内容，最终仍只返回一个完整文件，文件名固定为 `research_workflow.md`。
- 必须完整保留第 01 步已有内容；不得把它缩写成摘要。
- 能生成附件时返回可下载文件；否则用一个 Markdown 代码块输出完整全文。

## 前置检查

输入必须包含：

- `Original Research Profile`
- `Idea Decision Card`
- 明确的最终 idea
- `Claims Requiring Focused Verification in Step 02`

缺少关键内容时停止，只说明交接文件缺少哪些章节，不要猜测。

## 任务

使用 Deep Research 或 Search，为交接文件中的具体 idea 建立真实、可核验的文献基础，并检验新颖性。目标不是写论文。

## 检索要求

1. 收集至少 50 篇真实且可核验的相关文献。
2. 覆盖近 3 年和必要的 foundational work。
3. 优先核验 arXiv、OpenReview、正式出版页、DOI 页面、会议论文集和官方项目页。
4. 对标题、作者、年份、venue、DOI/arXiv ID 去重和交叉核验。
5. 不得编造参考文献。
6. UNVERIFIED 条目可作为补充，但不计入至少 50 篇 verified 文献的门槛。
7. 如果无法获得至少 50 篇 verified 文献，停止生成伪完整文献库；在文件中记录实际数量和缺口，并将 novelty 结论保持为不确定。

## 在原文件末尾追加以下完整章节

```markdown
# Step 02 Literature Package

## Literature Package Status
stage: 02_literature_verified
verified_reference_count:
unverified_reference_count:

## Verified References
至少 50 条。每条包含：
- citation key
- title
- authors
- year
- venue
- DOI、arXiv ID 或可点击官方来源
- relevance
- category: foundational / adjacent / directly competing / dangerous prior work
- verification status

## BibTeX Library
在一个 bibtex 代码块中提供与 Verified References 一一对应的完整 BibTeX。citation key 必须唯一、稳定。

## Literature Map
按主题组织文献，不要只按时间罗列。至少覆盖：
- foundational work
- direct competitors
- adjacent techniques
- benchmarks and evaluation methodology
- dangerous prior work
- remaining gaps

## Novelty Check
回答：
1. 这个 idea 是否已经被做过？
2. 哪些论文最危险？
3. 哪些部分仍然站得住？
4. 哪些 claim 应避免或弱化？
5. idea 应保留、修改还是放弃？

## Claim-to-Reference Map
把预期主要 claim 映射到支持文献、竞争文献和风险文献。

## Writing Constraints for Step 03
- 只能引用本文件中的 verified references。
- 正文实际引用至少 50 篇。
- 高风险 novelty claim 必须弱化。
- 缺失实验结果不得编造。

## Handoff Rules for the Next Step
- Step 03 must convert this complete Markdown handoff into one complete paper.tex.
- Step 03 must preserve the idea, novelty boundaries, verified bibliography and claim constraints.
```

更新文件顶部的 `Workflow Status`，把 stage 改为 `02_literature_verified`；如果文献数量或 novelty 验证未达标，则改为 `02_literature_incomplete` 并如实说明。

最终只交付更新后的完整 `research_workflow.md`。
