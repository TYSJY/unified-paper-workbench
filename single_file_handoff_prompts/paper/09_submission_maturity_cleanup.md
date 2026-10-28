# 第 09 步：对同一个 `paper.tex` 做投稿成熟度清理

## 单文件契约

- 本轮只读取上一步的 `paper.tex`。
- 最终只返回更新后的完整 `paper.tex`。
- 不要求任何更早文件或目录。
- 不编造数据、引用、baseline、编译结果或文件状态。
- 未实际编译时只做静态检查。

## 用户确认数据保护

如果存在 `CONFIRMED_DATA` 或 `USER_CONFIRMED_REAL`：

1. 完整保留所有数值、单位、方法名、表格行、比较关系、结果段落和图表证据。
2. 不得删除、修改、重算、平滑、美化或替换数据。
3. 不得因缺少 CSV、raw trace 或附件，把确认数据描述为 simulated、placeholder 或 unsupported。
4. 可以成熟化实验描述、caption 和 limitation，但不能把论文改成 methodology-only 或 proposal。

## 清理目标

把论文从“完整但仍有明显内部材料口吻的稿件”提升为成熟、正式、难以被明显攻击的投稿候选稿。

全文查找并改写：

- available material
- provided material
- result artifact
- result package
- aggregate-only evidence
- raw trace logs are not available
- exact SKU-level metadata is not available
- this draft / current version / generated paper
- 任何暴露内部工作流、材料包或自动生成过程的表述

不要掩盖真实缺口；应把它们改写成正式、边界清楚的 limitation。

重点检查：

1. Evaluation Setup 是否完整说明 models、devices、workloads、metrics、baselines、measurement methodology 和 aggregation。
2. Results 是否围绕 research questions/claims 组织。
3. 每个主要结果是否说明观察、原因、证据和边界。
4. claim 是否过强或过弱。
5. caption 是否正式并与图表内容一致。
6. figure 至少 5、table 至少 4，且全部被正文引用。
7. 正文实际引用至少 50 篇，引用键一致。
8. 表格过宽、图表排版和 LaTeX 格式是否适合投稿。
9. Abstract、Introduction、Discussion、Limitations 和 Conclusion 是否前后一致。
10. 是否仍有 TODO、placeholder、verification-required、内部审稿或工作流痕迹。

保留必要的 `CONFIRMED_DATA` 和简洁 `WORKFLOW_METADATA` 注释。更新 stage 为 `09_submission_mature`。

最终只交付完整 `paper.tex`。
