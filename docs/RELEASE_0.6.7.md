# 0.6.7 用户确认数据贯穿写作与画图

本版本修复用户已确认实验数据在后续审稿修改中被删除、并导致画图阶段再次等待真实数据的问题。

- 写作 Step 7 将 `DATA_MANUALLY_APPLIED` 和 `USER_CONFIRMED_REAL` 视为用户最终确认，不再因数值未变化、缺少独立 CSV 或 raw log 而否定数据。
- 写作 Step 8～10 必须保留已确认的数值、单位、方法、表格、结果段落、图表证据和 `CONFIRMED_DATA` 数据块；审稿意见与数据保护规则冲突时以数据保护规则为准。
- 允许后续步骤优化实验描述、claim 范围、caption 和 limitation，但不得把已确认数据降级成 simulated、placeholder 或 methodology-only 内容。
- 画图 Step 2 会把 `input.tex`、LaTeX 表格和 `CONFIRMED_DATA` 中的确认数据视为可用数据，优先围绕已有证据规划图表。
- 画图 Step 3 在独立 CSV 不存在时会从论文内无损提取并生成 CSV；只有所需字段在论文和结果文件中都确实不存在时，才生成数据需求卡点。
- V2 写作和画图提示词完整性哈希已同步更新，防止合法提示词更新被误判为篡改。

Step 6 原有规则已经覆盖“数值即使与 Step 5 相同也必须保留”，本版本没有重复改写 Step 6。
