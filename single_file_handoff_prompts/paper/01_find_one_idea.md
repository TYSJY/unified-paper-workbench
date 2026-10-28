# 第 01 步：检索驱动地选择一个科研 Idea

## 输入与输出契约（最高优先级）

- 本轮只读取用户上传的 `research_profile.md`。
- 不读取或假装读取用户电脑、目录或其他未上传文件。
- 不生成日期序号、项目编号或文件夹。
- 最终只返回一个完整文件，文件名固定为 `research_workflow.md`。
- 如果当前界面能生成附件，提供可下载的 `research_workflow.md`；否则用一个 Markdown 代码块完整输出，不得省略。
- 输出文件必须完整保留 `research_profile.md` 的原文，供下一步继续使用。
- 不返回分析过程、额外报告、patch 或多个文件。

## 任务

读取 `research_profile.md`，为我在 efficient LLM、edge LLM、resource-constrained local LLM inference 方向找到 exactly ONE 最合适的科研 idea。

这不是纯 brainstorm。必须先使用 Deep Research 或 Search 做检索驱动的文献侦察，再作决定。如果没有实际联网检索，不允许给出 `Commit`。

## 文献侦察要求

1. 围绕研究画像检索至少 6 组查询词。
2. 至少广泛查看 50 篇相关论文、系统、benchmark、框架或技术报告的标题和摘要。
3. 至少重点核查 20 篇最相关工作。
4. 覆盖近 3 年研究，并纳入必要的 foundational work。
5. 优先使用论文原始页面、arXiv、OpenReview、ACM、IEEE、USENIX、会议论文集或官方项目页。
6. 每个关键事实必须有可点击来源；不得凭记忆伪造论文、作者、年份、venue 或链接。
7. 识别直接竞争、相邻方向、已饱和方向和仍可能存在的 gap。
8. 内部候选 idea 不少于 8 个，最终比较的 top candidates 不少于 3 个。
9. 对 top candidates 比较 novelty risk、saturation risk、technical depth、reviewer defensibility 和研究画像匹配度。
10. 证据不足时，决定必须是 `Needs literature verification` 或 `Do not commit`，不能是 `Commit`。

## `research_workflow.md` 的固定结构

```markdown
# Research Workflow Handoff

## Workflow Status
stage: 01_idea_selected

## Original Research Profile
这里逐字保留上传的 research_profile.md 全文。

## Idea Decision Card

### Final Title

### Short Title

### One-Sentence Thesis

### Core Research Question

### Main Bottleneck

### Key Insight

### Why This Is Worth Doing

### Why It May Be Novel
必须引用检索证据，不能只写直觉。

### Why It Has Publication Potential

### Why It Is Not Pure Engineering Optimization

### Why It Is Not Merely Incremental

### Expected Contributions

### Closest Related Directions

### Literature Reconnaissance Summary
列出 8–12 个最相关的已核查工作。每项包含 title、year、venue，以及可点击的原论文或官方来源；说明与本 idea 的关系，以及为何没有完全覆盖本 idea 或为何构成风险。

### Most Dangerous Prior-Work Categories
列出 3–5 类。

### Top Candidate Comparison
至少列出 3 个 top candidates 及选择或淘汰理由。

### Claims Requiring Focused Verification in Step 02

### Proposed Evaluation

### Required Resources

### Main Risks

### Scope Boundaries

### Expected Paper Structure

### Final Decision
只能填写 Commit / Needs literature verification / Do not commit。

### Search Limitations
如实记录数据库、搜索、PDF 或证据限制。

## Handoff Rules for the Next Step
- Step 02 must preserve all content above.
- Step 02 must append a verified literature package to this same file.
```

如果无法完成实际搜索或无法核查至少 8 个真实相关工作，仍可返回该文件，但必须明确写出限制，且 `Final Decision` 不得为 `Commit`。

完成并交付 `research_workflow.md` 后停止。
