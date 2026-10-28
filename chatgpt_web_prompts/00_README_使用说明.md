# GPT 网页版论文工作流提示词——使用说明

本目录中的 `01`–`10` 是为 ChatGPT 网页版改写的独立提示词。原有科研流程、引用约束、数据保护规则和版本命名均予以保留；主要变化是把 Codex 的本地文件系统、skill 和目录操作改成 ChatGPT 网页版的“项目文件 / 本轮附件 / 可下载产物”工作流。

## 推荐设置

1. 在 ChatGPT 中为每个论文 idea 新建一个独立“项目（Project）”。
2. 把 `research_profile.md` 加入项目文件。
3. 每执行一步，都在该项目中新建聊天或继续当前聊天，并粘贴对应提示词。
4. 把该步要求的输入文件上传到当前聊天或项目文件。不要只提供你电脑上的路径；ChatGPT 网页版看不到你的本地磁盘。
5. 第 01、02 步优先选择 **Deep Research**；若账号没有该功能，至少开启 **Search / 网页搜索**。没有实际联网检索时，不应接受 `Commit`、已验证 novelty 或已验证参考文献等结论。
6. 下载每一步产物，在本地按逻辑路径归档，并将下一步需要的文件重新上传或加入项目文件。

## 文件交接表

| 步骤 | 使用的提示词 | 必需输入 | 主要输出 |
|---|---|---|---|
| 01 | `01_find_one_idea.md` | `research_profile.md`；若要延续编号，再上传当日已有 idea 文件名清单或文件 | `YYYY-MM-DD_XXX_short-title.md` |
| 02 | `02_collect_references.md` | 第 01 步 idea 文件 | `references.md`、`references.bib`、`literature_map.md`、`novelty_check.md`（通常打包为 ZIP） |
| 03 | `03_write_draft_tex.md` | idea 文件及第 02 步的 4 个文件 | `draft_v0.tex` |
| 04 | `04_upgrade_submission_draft.md` | `draft_v0.tex`、idea 文件、`references.md`、`literature_map.md`、`novelty_check.md`；可选 `paper_materials.md` | `draft_v1.tex` |
| 05 | `05_complete_figures_tables_results.md` | `draft_v1.tex`、idea 文件、`references.md`、`literature_map.md`、`novelty_check.md` | `draft_v2.tex` |
| 06 | `06_clean_verified_submission_draft.md` | 已由你逐项确认真实实验数据的 `draft_v2.tex`，以及 idea / references / literature map / novelty check | `draft_v3.tex` |
| 07 | `07_review_round_1.md` | `draft_v3.tex`、idea / references / literature map / novelty check；可选 `manual_data_completion.json` | `review_round_1.md` |
| 08 | `08_revise_after_review_round_1.md` | 第 07 步全部核心输入、`review_round_1.md`；可选 `manual_data_completion.json` | `draft_v4.tex` |
| 09 | `09_submission_maturity_cleanup.md` | `draft_v4.tex`、idea / references / literature map / novelty check；可选审稿文件和数据确认文件 | `draft_v5.tex` |
| 10 | `10_final_submission_hardening.md` | `draft_v5.tex`、idea / references / literature map / novelty check；可选审稿文件和数据确认文件 | `draft_v6.tex` |

## 路径与文件名

提示词中保留的 `ideas/...`、`refs/...`、`papers/...` 是本地归档所用的**逻辑路径**。ChatGPT 应按这些路径推导目标文件名，但不能声称已经把文件写入你的电脑。你下载后自行放到对应目录即可。

如果 ChatGPT 当前能力可以生成附件，应要求它提供精确文件名的可下载文件；第 02 步的四个文件可打成一个 ZIP。如果不能生成附件，提示词已要求它在独立代码块中完整输出每个文件，方便你复制保存。

对于单个 `.md` 或 `.tex` 文件，也可以让 ChatGPT 在 **Canvas** 中打开完整内容，再用 Canvas 的下载按钮导出；下载后仍需核对文件名和内容是否完整。

## 关键注意事项

- 不要让 ChatGPT 仅凭“最新文件”“刚生成的文件”猜 idea ID；附件中无法唯一识别时，应明确提供 idea ID。
- 第 06 步只应在你已经逐项确认 `draft_v2.tex` 中的实验数据后运行。提示词不能替你完成实验或把占位数据自动认证为真实数据。
- 图像生成适合概念图，不适合伪造定量结果。数据图必须来自已上传的真实数据，或来自论文中明确标为待验证的模拟数据。
- 如果 ChatGPT 没有实际运行 LaTeX 编译器，它只能做静态检查，不得声称“编译通过”。
- 项目记忆有助于保持上下文，但关键输入仍应以当前可见的项目文件或本轮附件为准。
