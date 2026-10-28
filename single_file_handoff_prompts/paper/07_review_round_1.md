# 第 07 步：在同一个 `paper.tex` 中嵌入第一轮严格审稿意见

## 单文件契约

- 本轮只读取上一步的 `paper.tex`。
- 不单独生成 `review_round_1.md`。
- 不修改论文正文、数据、图表、引用或 bibliography。
- 只在 `\end{document}` 之前新增一个完整 LaTeX 注释审稿区。
- 最终仍只返回完整 `paper.tex`。
- 不读取目录，不要求其他附件。

## 用户确认数据的审稿边界

如果文件包含 `CONFIRMED_DATA` 或 `DATA_STATUS: USER_CONFIRMED_REAL`：

- 这些数值、单位、方法名、表格行和比较关系均为用户确认数据。
- 不得把它们重新判定为 simulated、placeholder、fabricated 或 unverified。
- 缺少独立 CSV、日志或附件不得成为建议删除结果的理由。
- 可以批评实验描述、统计范围、复现信息、claim 边界和图表表现，但不得建议篡改或删除已确认数据。

## 审稿任务

像严格的顶会审稿人一样审查：

1. novelty 和与危险 prior work 的差异；
2. technical depth；
3. method clarity 和 reproducibility；
4. claim-evidence alignment；
5. evaluation design、baselines、metrics、devices、workloads；
6. quantitative evidence 是否足够；
7. figure/table 是否真的支持 claim；
8. 引用完整性与 related-work positioning；
9. limitations 和适用边界；
10. 是否存在一票否决问题。

特别检查：

- figure 总数是否至少 5、table 是否至少 4；
- 正文实际引用是否至少 50；
- 概念图是否过多、定量图是否过少；
- result/Pareto/ablation/sensitivity 图是否只是文字框；
- 表格是否被伪装成图；
- 数据图是否应由 CSV/Python/PGFPlots 等可复现流程产生，而非图像生成；
- 所有重要 claim 是否有数据、图表、引用或明确范围限定。

## 注释审稿区格式

如果已有旧的 `REVIEW_ROUND_1` 区，先替换它，不要重复追加。

在 `\end{document}` 之前加入：

```latex
% <REVIEW_ROUND_1_START>
% Overall Recommendation: Accept / Weak Accept / Borderline / Weak Reject / Reject
% Confidence:
%
% Summary:
% ...
%
% Strengths:
% 1. ...
%
% Rejection-Level Issues:
% 1. ...
%
% Major Weaknesses:
% 1. ...
%
% Minor Weaknesses:
% 1. ...
%
% Novelty Review:
% ...
%
% Method and Reproducibility Review:
% ...
%
% Evaluation and Claim-Evidence Review:
% ...
%
% Figure Evidence Review:
% ...
%
% Reference and Positioning Review:
% ...
%
% Required Revision Checklist:
% [ ] ...
%
% Final Rationale:
% ...
% <REVIEW_ROUND_1_END>
```

每一行必须以 `%` 开头，确保不影响 LaTeX 排版。

把 `WORKFLOW_METADATA` 的 stage 更新为 `07_review_embedded`。最终只交付包含原论文全文和审稿注释区的完整 `paper.tex`。
