# P7 通用性验证与本科论文 Workflow Pack

日期：2026-08-05

## 结论

P7 已用两个彼此独立的工作流证明 P1-P6 内核不依赖固定十步、SCI 文件名、`paper_id`、`current_step += 1` 或 Step 5/6 专用语义：

- 三节点纯离线 demo 按 success edge 执行 `normalize-request → build-action-plan → render-summary`。定义中的 node tuple 顺序和 `display_order` 均故意不同于执行顺序；两条并发 run 对相同输入产生相同的三份 artifact SHA256。
- 六节点本科论文模板按范围界定、证据登记、方法与伦理、分析计划、草稿组装、完整性复核执行。模板为 `builtin + readonly + cloneable`，可克隆为 custom draft，源版本不变。

本阶段没有修改任何既有写作、画图、选刊作者 Prompt，也没有修改生产调度入口。

## 本科论文安全合同

- 六份 Prompt 是新增 package asset，各自带固定 SHA256；runtime binding 在构造时及 executor dispatch 前重新验证文件字节。
- 默认按 `thesis_brief.language` 输出，缺失时使用简体中文。
- 只允许使用绑定输入；禁止编造引用、书目信息、数据、结果、伦理审批和导师意见。证据不足必须保留显式缺口。
- 每个 Codex 节点只允许 `read`、`write`，网络关闭；同一 run 使用独立 session group。
- 每个输出均由 `declared_output_files` 精确映射。通用 collector 拒绝绝对路径、路径穿越、ADS、控制字符、Windows 设备名、跨平台大小写/Unicode 冲突、文件与目录前缀冲突以及 symlink/reparse。
- Collector 只声明候选文件；`ArtifactService` 仍独立执行 attempt-root containment、句柄与 reparse 检查、稳定读取、媒体类型验证、可信 schema validator、SHA256 封存和 ledger 写入。
- 本科包提供七个 output schema validator；完整六节点 fake-Codex E2E 已通过，避免出现“合同能加载但首个 artifact 必然阻断”的伪就绪状态。

## 新增产品能力

- `workflow/examples.py`：三个无网络、无时钟、无环境变量依赖的确定性 handler。
- `workflow/packs/examples.py`：可安装、克隆和运行的通用离线模板。
- `workflow/packs/undergraduate.py`：本科论文 pack、Prompt seal manifest、runtime binding 和可信输出 validator。
- `workflow/executors/declared_files.py`：可复用的声明文件 collector，供任何满足同一 metadata 合同的 Codex workflow 使用。

## 边界与回滚

- P7 仍是 side-by-side 能力；未注册生产客户端、未替换旧 runtime、未写论文目录或旧阶段 JSON。
- 离线 demo 可直接由通用 Scheduler 执行。本科模板需要显式注入 Codex client、`DeclaredFileOutputCollector` 和 `undergraduate_schema_validators()`。
- 本阶段可通过 P7 独立 Git commit 回退；P0-P6 和旧生产入口不依赖这些示例、模板或新增 Prompt。
