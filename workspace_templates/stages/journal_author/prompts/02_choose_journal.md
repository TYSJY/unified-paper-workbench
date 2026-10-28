任务：为每篇论文选择最稳妥的投稿期刊。

项目路径：

D:\Documents\期刊选刊与作者

读取：

D:\Documents\期刊选刊与作者\papers\<paper_id>\work\<论文标题slug>.tex

如果存在，也必须读取：

D:\Documents\期刊选刊与作者\papers\<paper_id>\journal_preferences.md

journal_preferences.md 中的用户分区偏好、非 OA 偏好、优先期刊、排除期刊和“最稳中稿策略”优先级高于本文件中的默认偏好。若用户显式选择 Q1 / Q2，只能作为需要解释风险并请求用户确认的偏好记录；不得在未解释风险时直接把 Q1 / Q2 作为最终最稳目标。

目标：

1. 只选择 2 区以下期刊。
2. 优先 Q3 / Q4。
3. 期刊级别低没关系，核心是范围匹配、风险低、最稳妥。
4. 默认不要选择 Q1 / Q2；只有 journal_preferences.md 显式选择 Q1 / Q2 时，才允许把它们列为候选，并必须解释风险、标记 Needs user confirmation。
5. 不要选择假刊、掠夺性期刊、预警期刊、收录异常期刊。
6. 不能保证中稿，只能推荐相对稳妥目标。

请实时核查：

- 期刊官网
- aims and scope
- author guidelines
- 是否 Scopus / WoS / ESCI / SCIE 收录
- JCR / SJR / CiteScore 分区，如果可查
- APC
- 是否接受 AI、edge AI、LLM inference、model compression、quantization、embedded systems、applied ML 类论文
- 是否近期发表过相近主题论文
- 是否支持 LaTeX

对每篇论文生成至少 5 个候选期刊，按“稳妥程度”排序。

每个候选期刊写清楚：

- Journal name
- Publisher
- Official website
- Scope match
- Quartile evidence
- Indexing evidence
- APC if available
- Similar papers if found
- Scope fit score 1-5
- Acceptance safety score 1-5
- Risk score 1-5，5 表示风险高
- Reason to choose
- Reason to avoid
- Verification status: verified / partially verified / unverified

最后推荐 1 个最稳妥目标期刊。

输出：

D:\Documents\期刊选刊与作者\papers\<paper_id>\journal.md

journal.md 必须包含：

# Journal Decision

## Paper ID

## Paper Title

## Recommended Journal

## Why this journal is the safest choice

## Scope match

## Quartile / indexing evidence

## Similar published papers

## APC and cost

## Submission format notes

## Required files

## Top 5 candidates

## Rejected journals

## Final decision

Final decision 只能是：

- Recommended
- Needs user confirmation
- Do not submit yet

如果无法确定唯一目标期刊，输出 Needs user confirmation，并列出 Top 3。
