# 使用前准备（ChatGPT 网页版）

开始前，请在消息中指定一个已经存在的完整 idea ID，或确保本轮已上传文件/当前 Project 中只有一个可唯一识别的目标 idea 文件。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

建议在可用时启用 Deep Research；否则必须使用 ChatGPT Search 完成文献搜集和核验。

# 执行环境规则

- 你只能使用本轮已上传的文件、当前 Project 中可见的文件及用户在对话中粘贴的内容。你不能查看用户设备上的 ideas 或 refs 目录，也不能因为消息中出现了路径就声称已经读取该路径。
- 依据附件清单、Project 中可见的文件名、文件内的 Idea ID 和用户明确给出的 ID 识别目标。
- 如果用户或上游任务已经指定完整 idea ID，就使用该 ID。
- 如果没有指定 ID，但可见输入中恰好只有一个符合命名规则、且内容中的 Idea ID 与文件名一致的 idea 文件，可以使用它。
- 如果存在多个候选或不能唯一确定目标，只问这一句并停止：“请提供要处理的完整 idea ID（YYYY-MM-DD_XXX_short-title）。”不要同时询问其他问题，也不要自行选择。
- 确定 ID 后，如果匹配的 idea 文件不在本轮上传文件或当前 Project 中，停止并请用户提供该文件；不要猜测文件内容。
- 文献搜集与近期事实核验必须使用 Deep Research 或 ChatGPT Search。必须给出可点击的原始论文、出版方或官方项目来源链接，不能凭记忆伪造条目、作者、年份、venue、DOI 或 arXiv ID。
- 如果搜索不可用、证据不足或无法达到至少 50 篇真实文献，必须诚实停止并说明缺口，不得用虚构文献补足数量。
- 多文件结果必须打包成 ZIP。网页若支持文件生成，请提供可下载 ZIP；不要声称已保存到用户设备或某个用户目录。若无法生成文件，则按 exact filename 为每个文件使用独立代码块完整输出，不得省略。

---

本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

规则：

- 不要自己猜日期。
- 不要根据今天日期临时编一个 ID。
- 如果用户或上游任务已经指定 idea ID，就使用该 ID。
- 如果没有指定 idea ID，只能依据本轮附件或当前 Project 中可见的 idea 文件判断；只有目标唯一时才能继续。
- 如果无法确定唯一目标 idea 文件，按执行环境规则只请求完整 idea ID，然后停止。
- 不要把其他 idea ID 的材料混入本次结果，也不要覆盖或冒充不属于该 idea ID 的 refs 内容。

读取本轮已上传或当前 Project 中与下列逻辑归档路径相对应的 idea 文件：

ideas/YYYY-MM-DD_XXX_short-title.md

这里的 YYYY-MM-DD_XXX_short-title 必须替换为已确定的完整 idea ID。网页端以可见文件名及文件内 Idea ID 为准，不要求用户设备上的该路径可被直接访问。

请基于这个具体 idea 收集参考文献。

你的目标不是写论文，而是为这个 idea 建立文献基础，并验证它的新颖性。

最终 ZIP 的内部逻辑归档路径结构必须是：

refs/YYYY-MM-DD_XXX_short-title/

其中 YYYY-MM-DD_XXX_short-title 必须是已经确定的完整 idea ID，并且必须和读取的 idea 文件一致。

在这个目录中只放置：

- references.md
- references.bib
- literature_map.md
- novelty_check.md

## 检索与核验要求

请收集至少 50 篇和这个 idea 相关的真实参考文献。

1. 使用 Deep Research 或 ChatGPT Search 进行多轮检索，覆盖近 3 年研究和必要的更早 foundational work。
2. 优先核验 arXiv 论文页或 PDF、ACM/IEEE/USENIX 出版页、OpenReview、DOI 落地页、作者或机构官方项目页等第一方来源。聚合搜索页只能用于发现条目，不能替代关键元数据核验。
3. 去重时同时检查标题、DOI、arXiv ID 和版本关系，避免把同一工作或预印本/正式发表版本重复计数。
4. 不要编造参考文献。任何无法由可点击来源核验的字段都必须明确标记，不能靠记忆补齐。
5. 搜索不可用、无法核验关键事实或真实条目不足 50 篇时，停止文件生成并报告已核验数量、未核验数量及具体缺口。

每篇参考文献需要包含：

- title
- authors
- year
- venue
- DOI、arXiv ID 或 official URL，如果有
- 至少一个可点击的原始论文、出版方或官方项目来源链接
- 为什么它和这个 idea 相关
- 它是 foundational、adjacent、directly competing，还是 dangerous prior work
- verification status：verified / unverified

不要编造参考文献。
如果某篇文献无法验证，请标记为 UNVERIFIED，并明确指出哪些字段尚未验证。UNVERIFIED 条目可以作为补充记录，但不能用于把“至少 50 篇真实且可核验文献”的门槛凑足。

## 各文件要求

### references.md

- 至少包含 50 篇 verified 的真实参考文献；如有 UNVERIFIED 条目，单列在末尾。
- 每篇都包含上述全部字段、分类、相关性说明、verification status 和可点击来源链接。
- 条目之间保持稳定且唯一的 citation key，并与 references.bib 对应。

### references.bib

- 为 references.md 中的文献提供有效的 BibTeX 条目。
- citation key 必须唯一，并与 references.md 一致。
- title、authors、year、venue 以及 DOI、eprint 或 URL 等字段必须以核验结果为准。
- 无法核验的字段不要猜测；必要时保留带清晰注释的 UNVERIFIED 条目。

### literature_map.md

- 按 foundational、adjacent、directly competing、dangerous prior work 及该 idea 所需的其他主题簇组织文献。
- 说明各文献簇的核心问题、主要方法、和本 idea 的关系、重叠边界与尚未覆盖的 gap。
- 识别直接竞争路线、相邻路线、饱和方向以及最危险的近邻工作。
- 所有关键判断必须链接回 references.md 中的真实条目和可点击来源。

### novelty_check.md

必须基于已核验文献明确回答：

1. 这个 idea 是否已经被做过？
2. 哪些论文对这个 idea 最危险？
3. 这个 idea 的哪些部分仍然站得住？
4. 哪些 claim 应该避免？
5. 这个 idea 应该保留、修改，还是放弃？

不能用纯直觉作新颖性结论。证据不充分时必须明确写出需要进一步验证的 claims、未覆盖的数据库或检索限制，并降低结论强度。

## 交付规则

只交付上述四个文件，不要写论文，不要生成其他分析文件。

以下 refs 路径均为逻辑归档路径。ChatGPT 只负责提供下载文件或代码块，不代表已经写入用户设备。

网页支持文件生成时：

- 创建 exact filename 为 refs_YYYY-MM-DD_XXX_short-title.zip 的可下载 ZIP。
- ZIP 内必须包含且只包含：
  - refs/YYYY-MM-DD_XXX_short-title/references.md
  - refs/YYYY-MM-DD_XXX_short-title/references.bib
  - refs/YYYY-MM-DD_XXX_short-title/literature_map.md
  - refs/YYYY-MM-DD_XXX_short-title/novelty_check.md
- 不要声称 ZIP 或其内容已经写入用户设备上的 refs 目录。

如果网页无法生成可下载文件，则依次标明以下 exact filename，并为每个文件使用一个独立代码块完整输出内容：

- references.md
- references.bib
- literature_map.md
- novelty_check.md

不要用摘要、节选、占位语或“其余同上”替代完整内容。

停止在交付参考文献 ZIP 或四个完整文件之后。
