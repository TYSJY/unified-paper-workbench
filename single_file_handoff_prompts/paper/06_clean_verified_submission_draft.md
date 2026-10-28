# 第 06 步：把用户已核对的数据稿清理成正式 empirical paper

## 用户确认（最高优先级）

用户发送本提示词即明确确认：

> 当前上传的 `paper.tex` 中，正文、表格、图、caption、`VERIFY_DATA` 数据块和其他现有实验数值均已由用户逐项核对，并确认是真实实验数据。即使某些真实值与原模拟占位值完全相同，也必须视为真实数据原样保留。

如果用户尚未完成逐项核对，不应执行本步骤。

## 单文件契约

- 本轮只读取上一步、且已由用户确认数据的 `paper.tex`。
- 最终只返回清理后的完整 `paper.tex`。
- 不要求 JSON、CSV、日志、idea 文件或其他附件。
- 不读取目录，不返回 patch。
- 未实际编译时不得声称编译通过。

## 数据保护规则

1. 保留全部数值、单位、方法名、表格行、比较关系、结果段落和图表证据。
2. 不得修改、重算、平滑、美化、猜测或替换任何已确认数据。
3. 数值与先前模拟值相同不构成删除理由。
4. 缺少独立 CSV 或 raw log 不得成为否定、删除或降级已确认数据的理由。
5. 用户确认只覆盖文件中实际存在的数值，不授权新增实验数据。
6. 输出前逐项比较输入与输出的数据块、表格和定量结果，确保无丢失。

## 状态转换

把每个数据块：

```latex
% VERIFY_DATA_START: <label>
% DATA_STATUS: SIMULATED_TO_REPLACE
...
% VERIFY_DATA_END
```

转换为：

```latex
% CONFIRMED_DATA_START: <label>
% DATA_STATUS: USER_CONFIRMED_REAL
...
% CONFIRMED_DATA_END
```

其中 `DATA_PURPOSE`、`CSV_SCHEMA`、`DATA_ROWS`、数值、方法名、单位和顺序必须逐项保留。

删除所有 `\VERIFY`、`【验】`、placeholder、simulated、verification-required、future measurement、TODO 和 protocol-style 状态表述，但不能连带删除数据。

## 论文成熟化

1. 把 Evaluation / Results 改写为正式已完成实验的叙述。
2. 让每个主要 claim 都有图、表、数据或引用支撑。
3. 保持至少 5 张 figure、4 张 table、50 篇正文实际引用。
4. 所有图表必须被正文引用和解释。
5. 删除未完成稿口吻，同时保留专业 limitation。
6. 不改变核心 idea，不编造新实验或新文献。

更新元数据：

```latex
% stage: 06_verified_empirical_paper
% data_status: CONFIRMED
```

最终只交付完整 `paper.tex`。
