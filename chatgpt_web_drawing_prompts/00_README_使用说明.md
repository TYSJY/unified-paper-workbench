# GPT 网页版论文画图工作流——使用说明

本目录中的 `01`–`04` 是为 ChatGPT 网页版改写的论文画图提示词。原流程的论文图类型、参考图研究、数据保护、可复现绘图、LaTeX 插图、投稿包和 manifest 规则均予以保留；主要变化是把 Codex 的本地目录与 `image2/imagegen` 工具调用改成 ChatGPT Project、文件上传、ChatGPT Image Generation 和 Data Analysis / Python 的交互式工作流。

## 推荐设置

1. 为每篇论文新建一个独立 ChatGPT Project，把 `input.tex` 和相关材料加入项目文件。
2. 四步尽量在同一个 Project 中执行。每一步仍应明确上传或指出本轮所需文件，不要只给出电脑路径。
3. 第 01 步优先使用 Deep Research；没有时至少开启 Search。
4. 生成概念图时使用 ChatGPT Image Generation；生成定量数据图时使用 Data Analysis / Python、PGFPlots 或其他可复现绘图方式。
5. 每一步下载产物并按提示词中的逻辑路径归档。`papers/...` 与 `refs/...` 只是你下载后的目录结构，ChatGPT 不能直接读写这些本机路径。

## 四步交接表

| 步骤 | 提示词 | 主要输入 | 主要输出 |
|---|---|---|---|
| 01 | `01_research_reference_board.md` | `input.tex`；可选已有 reference board、manifest、参考截图 | `reference_board.md`、field notes、图片 manifest 或 missing-image 记录；能实际获取时可附参考截图 |
| 02 | `02_make_figure_plan.md` | `input.tex`、第 01 步的 board/manifest/参考图片；没有截图时可仅用文字资料 | `figure_plan.md`、`figure_prompts.md`、每张图的 `figureX_brief.md`，通常打包为 ZIP |
| 03 | `03_generate_figures_and_submit_package.md` | `input.tex`、figure plan、prompts、briefs、相关参考资料、真实数据或论文中的 `CONFIRMED_DATA` | 概念图 PNG、定量图 PDF/PNG、CSV、脚本、更新后的 TeX、manifest、可用时的编译 PDF 和最终 ZIP |
| 04 | `04_single_figure_revision.md` | 目标图、`input.tex`、plan、prompts、对应 brief；定量图还需数据和脚本 | 修订图及实际变更过的 brief/prompt/data/script/manifest 文件 |

## 图类型规则

- **概念图 `conceptual_schematic`**：使用 ChatGPT Image Generation。上传当前图片即可做编辑，也可以按完整 figure prompt 重新生成。标签应简短；生成后必须目视核对文字、箭头和结构。不得用代码在生成图上补语义标签来掩盖生成错误。
- **定量图 `quantitative_data_figure`**：只能使用真实上传数据，或从 `input.tex`、LaTeX 表格、`CONFIRMED_DATA` 中无损提取的数据，通过 Data Analysis / Python、PGFPlots 等方式生成。禁止让图像生成模型画坐标轴、曲线、柱状图、热图或实验数值。
- **混合图 `hybrid_figure`**：概念面板与定量面板分开生成，使用 LaTeX subfigure 等可复现方式组合，不能让图像模型重绘定量面板。
- **表格**：保持为 LaTeX table，不要伪装成图片。

## 网页版的重要交接点

ChatGPT Image Generation 生成的图片不一定能被 Data Analysis 的文件环境直接读取。因此第 03、04 步包含一个真实的交接点：如果系统不能直接访问刚生成的图片，请先下载图片，按提示词指定的精确文件名重命名，再上传回同一聊天或 Project。完成重新上传前，ChatGPT 不得声称已经打包、组合或编译了该图。

同样，Data Analysis / Python 不能依赖未上传的外部数据。需要 CSV、脚本、字体、样式、参考图片或 LaTeX 依赖时，应明确上传。

## 输出与验证

- 能生成文件时，优先获取可下载附件或 ZIP；不能打包时，逐项下载并以 `submission_manifest.json` 为准核对。
- 文本文件无法作为附件生成时，可以使用提示词要求的完整代码块回退；图片不能用文字占位冒充已生成文件。
- 只有真正运行过 LaTeX 编译器并得到目标 PDF，manifest 才能写 `compiled_pdf_created: true`。否则必须写 `false` 并保留准确原因及其余可用产物。
- 参考论文图片仅用于私人视觉分析。必须保留来源、论文名、figure/page 编号；不得复制、描摹或直接复用到最终论文图中。
