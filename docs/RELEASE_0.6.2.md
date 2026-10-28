# 0.6.2 Step 6 用户确认数据保留

本版本按用户明确要求调整写作 Step 6 内部 Prompt，修复用户确认的真实数值因仍位于验证注释块而被连同标记一起删除的问题。

- 进入 Step 6 明确表示当前 `draft_v2.tex` 中所有实验数值已由用户确认。
- 即使真实实测值与 Step 5 原模拟值完全相同，也必须作为真实数据保留。
- 禁止删除 `VERIFY_DATA` 中的 `DATA_PURPOSE`、`CSV_SCHEMA`、`DATA_ROWS`、方法名、数值和单位。
- 每个 `VERIFY_DATA` 块转换为 `CONFIRMED_DATA`，状态改为 `USER_CONFIRMED_REAL`。
- Step 6 完成前必须逐块核对 label、schema、数据行和全部数值没有丢失或变化。
- 确认数据块保留在 `draft_v3.tex` 中，供后续审稿、画图与数据溯源使用。
- 其余 9 份内置写作 Prompt 和全部画图 Prompt 未修改。
- Step 6 新 SHA-256 为 `462901FF9B906737AC4650156D45D1AFEDBA7817DC690232A6D1ECB7CA827E1E`，生产封印与测试合同已同步更新。

本次修改不会回写或重跑既有论文；已有论文需要按其当前状态单独恢复缺失数据或图表。
