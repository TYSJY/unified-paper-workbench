# 第 10 步：对同一个 `paper.tex` 做最终投稿强化

## 单文件契约

- 本轮只读取上一步的 `paper.tex`。
- 最终只返回最终版完整 `paper.tex`。
- 不生成图片、不运行新实验、不新增外部文件。
- 不要求任何旧版本、目录或 ID。
- 不返回 patch、说明或清单。
- 未实际编译时不得声称编译通过。

## 数据保护（最高优先级）

完整保留 `CONFIRMED_DATA`、`USER_CONFIRMED_REAL` 及其支持的正文数值、单位、方法名、表格行、图表关系和事实性结论。不得修改、删除、重算、平滑、美化或替换确认数据。

## 最终强化任务

1. 检查 Abstract、Introduction、Results、Discussion、Conclusion 中所有重要 claim。
2. 每个 claim 必须至少满足一种：
   - 有实验结果支持；
   - 有 figure/table 支持；
   - 有 reference 支持；
   - 明确限定在 evaluated settings、tested workloads 或 studied devices 内。
3. 支撑不足时弱化 claim，不得编造证据。
4. Abstract 必须包含问题、缺口、方法、评估范围、关键结果和贡献。
5. Introduction 必须形成 problem → gap → insight → method → evidence → contributions 的连贯故事。
6. Related Work 必须能防御 novelty 攻击，并避免无依据的 `first`、`solve`、`universally`、`guarantee`。
7. Method 必须足够具体，符号、模块、算法和实现关系一致。
8. Evaluation 必须清楚说明 baselines、metrics、models、devices、workloads 和测量方法。
9. Results 必须与表格、图和摘要中的数值完全一致。
10. Discussion 和 Limitations 应专业、清晰、不过度自我削弱。
11. figure 总数不少于 5，table 总数不少于 4；全部被正文引用和解释。
12. 正文实际引用至少 50 篇；所有 `\cite{}` 与 `\bibitem{}` 对应。
13. 删除所有 TODO、placeholder、simulation、verification-required、内部材料、审稿注释、自动生成和工作流痕迹，但保留正式的 `CONFIRMED_DATA` 数据溯源注释。
14. 清理 LaTeX 投稿格式、过宽表格、caption、label/ref、重复段落和不一致术语。
15. 不改变核心 idea，不新增实验结果，不编造文献或 baseline。

## 最终元数据

把 `WORKFLOW_METADATA` 精简为：

```latex
% <WORKFLOW_METADATA_START>
% stage: 10_final_submission
% data_status: CONFIRMED | PARTIAL | NO_EXPERIMENTAL_DATA
% confirmed_data_preserved: true
% static_checks_completed: true
% latex_compiled_in_this_session: true | false
% <WORKFLOW_METADATA_END>
```

如果未实际编译，`latex_compiled_in_this_session` 必须为 `false`。

最终只交付完整 `paper.tex`，不得省略任何章节。
