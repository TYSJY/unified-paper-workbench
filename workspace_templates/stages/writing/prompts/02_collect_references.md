本提示词是可复用模板。执行前必须先确定一个已经存在的完整 idea ID。

完整 idea ID 的格式为：

YYYY-MM-DD_XXX_short-title

规则：

- 不要自己猜日期。
- 不要根据今天日期临时编一个 ID。
- 如果用户或上游任务已经指定 idea ID，就使用该 ID。
- 如果没有指定 idea ID，请先查看 `ideas/` 目录，并选择刚刚生成且需要收集文献的那个 idea 文件。
- 如果无法确定唯一目标 idea 文件，请停止并要求用户指定，不要继续执行。
- 不要覆盖不属于该 idea ID 的 refs 目录。

读取下面这个 idea 文件：

ideas/YYYY-MM-DD_XXX_short-title.md

请基于这个具体 idea 收集参考文献。

你的目标不是写论文，而是为这个 idea 建立文献基础，并验证它的新颖性。

请创建文件夹：

refs/YYYY-MM-DD_XXX_short-title/

其中 `YYYY-MM-DD_XXX_short-title` 必须替换为已经确定的完整 idea ID，并且必须和读取的 idea 文件一致。

在这个文件夹中创建：

- references.md
- references.bib
- literature_map.md
- novelty_check.md

请收集至少 50 篇和这个 idea 相关的真实参考文献。

每篇参考文献需要包含：

- title
- authors
- year
- venue
- DOI、arXiv ID 或 official URL，如果有的话
- 为什么它和这个 idea 相关
- 它是 foundational、adjacent、directly competing，还是 dangerous prior work
- verification status：verified / unverified

不要编造参考文献。
如果某篇文献无法验证，请标记为 UNVERIFIED。

在 novelty_check.md 中回答：

1. 这个 idea 是否已经被做过？
2. 哪些论文对这个 idea 最危险？
3. 这个 idea 的哪些部分仍然站得住？
4. 哪些 claim 应该避免？
5. 这个 idea 应该保留、修改，还是放弃？

停止在创建参考文献文件夹和这些文件之后。

