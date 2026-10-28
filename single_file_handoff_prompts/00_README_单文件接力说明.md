# GPT 网页版单文件接力工作流

这套提示词不要求 GPT 网页版访问本机目录，也不使用额外的命名编号或文件夹结构。

核心规则只有一条：

> 每一步只上传“上一步返回的完整文件”并粘贴“下一步提示词”；GPT 在该文件基础上继续增加、修改或清理内容，再返回一个新的完整文件。

## 论文 10 步

| 步骤 | 上传给 GPT 的文件 | GPT 返回 |
|---|---|---|
| 01 选题 | `research_profile.md` | `research_workflow.md` |
| 02 文献 | 上一步的 `research_workflow.md` | 更新后的 `research_workflow.md` |
| 03 初稿 | 上一步的 `research_workflow.md` | `paper.tex` |
| 04 投稿级改写 | 上一步的 `paper.tex` | 更新后的 `paper.tex` |
| 05 图表和结果结构 | 上一步的 `paper.tex` | 更新后的 `paper.tex` |
| 06 清理已确认数据 | 上一步、且已经由用户手动核对数据的 `paper.tex` | 更新后的 `paper.tex` |
| 07 第一轮审稿 | 上一步的 `paper.tex` | 在源码末尾嵌入审稿注释区的 `paper.tex` |
| 08 根据审稿修改 | 上一步含审稿注释区的 `paper.tex` | 修改后的 `paper.tex` |
| 09 投稿成熟度清理 | 上一步的 `paper.tex` | 更新后的 `paper.tex` |
| 10 最终强化 | 上一步的 `paper.tex` | 最终 `paper.tex` |

第 01 步的输出必须保留研究画像和选题卡；第 02 步继续在同一个 Markdown 文件中加入完整文献库、BibTeX、literature map 和 novelty check。第 03 步将全部内容转成完整 LaTeX 论文，并把仍需后续使用的工作流信息保存为 LaTeX 注释区。此后始终修改同一个 `paper.tex`。

第 07 步不会单独返回 `review_round_1.md`，而是在 `paper.tex` 末尾加入：

```latex
% <REVIEW_ROUND_1_START>
% ...
% <REVIEW_ROUND_1_END>
```

第 08 步读取这个注释区完成修改，再把它替换为简短的已处理记录。这样下一步始终只需上传一个文件。

## 画图 4 步

| 步骤 | 上传给 GPT 的文件 | GPT 返回 |
|---|---|---|
| 01 参考图研究 | 当前 `paper.tex` | `figure_workflow.md` |
| 02 图表规划 | 上一步的 `figure_workflow.md` | 更新后的 `figure_workflow.md` |
| 03 生成图与投稿包 | 上一步的 `figure_workflow.md` | `figure_package.zip`；若当前界面无法一次打包，则返回实际生成的图、数据、脚本、`paper_with_figures.tex` 和 manifest |
| 04 单图修改 | 目标图的修订交接文件或第 03 步 manifest，加上目标图及其源数据/原始 PNG | `figure_revision_package.zip` 或实际生成的修订文件 |

为了让第 02、03 步不需要重新上传论文，画图第 01 步输出的 `figure_workflow.md` 必须包含：

- 论文全文的完整副本；
- 参考文献视觉研究；
- 已确认数据块和现有表格；
- 后续画图所需的全部上下文。

第 02 步继续在这个 Markdown 文件中追加 figure plan、完整生成提示词和逐图 brief。

第 03 步是真正产生二进制图片的步骤，因此它是唯一自然会产生多个文件的步骤。概念图使用 ChatGPT Image Generation；定量图必须由明确数据通过 Data Analysis / Python、PGFPlots 等可复现方式生成。若图像生成结果不能自动进入打包环境，GPT 必须停下来要求用户下载并重新上传该 PNG，不能假装已经自动传输。

## 通用输出规则

- 不要询问或生成额外的日期序号、项目编号或目录名。
- 不要声称读取、遍历或写入用户电脑。
- 不要要求用户重新上传更早步骤的文件；所需信息必须由上一步交接文件保留。
- 文本步骤只返回一个完整文件，不返回 patch、diff、摘要或“其余不变”。
- 如果界面可以生成附件，返回可下载文件；否则用一个完整代码块输出全文。
- 只有实际运行过 LaTeX 编译器，才可以声称编译通过。
- 只有实际生成并检查过图片，才可以声称图片已完成。
