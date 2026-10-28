# 第 04 步：把同一个 `paper.tex` 升级为投稿级论文稿

## 单文件契约

- 本轮只读取上一步的 `paper.tex`。
- 不要求其他 idea、reference、novelty 或 materials 文件。
- 所需边界必须来自论文正文、bibliography 和 `WORKFLOW_METADATA` 注释区。
- 最终只返回修改后的完整 `paper.tex`，不得返回 patch、说明或其他文件。
- 不读取或写入用户电脑目录。
- 未实际编译时不得声称编译成功。

## 任务

在不改变核心 idea、不编造数据和文献的前提下，把当前论文从 proposal-style 或骨架稿提升为 submission-level paper draft。

## 修改重点

1. 删除或改写 `this draft proposes`、`in this version`、`this paper should`、`we do not claim results` 等草稿口吻。
2. 强化 Abstract 的 problem、gap、method、evidence status 和 contribution。
3. 强化 Introduction 的 problem → gap → insight → method → contributions 叙事。
4. Related Work 按主题组织，并明确与危险 prior work 的差异。
5. Problem Formulation 给出变量、约束、目标和清楚的问题边界。
6. Method / Framework 必须具体、技术化、可复现；加入必要公式、模块、算法或伪代码。
7. 如果没有真实实验数据，Evaluation 必须诚实地保留为待完成设置或 TODO，不得写成已有结果。
8. 可以加入 figure/table 的 LaTeX skeleton 和明确 caption intent，但不得编造数值。
9. Discussion 和 Limitations 必须诚实且不自毁。
10. 保持正文实际引用至少 50 篇 verified references。
11. 保证 `\cite{}` 与 `\bibitem{}` 完全匹配。
12. 保留并更新 `WORKFLOW_METADATA`，stage 改为 `04_submission_draft`。

## 输出

返回一篇完整、连贯、英文、机器学习/系统会议风格的 `paper.tex`。不得省略未修改部分，不得使用“其余同上”。
