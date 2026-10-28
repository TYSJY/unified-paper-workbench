# P6 通用工作流库与线性编辑器

日期：2026-08-05

## 交付结论

P6 新增了与现有 SCI 生产入口完全隔离的通用工作流产品面：不可变版本库、copy-on-write 线性编辑器、内容寻址 Prompt 资源、严格导入导出、脱敏运行时间线与独立 CLI。现有提示词、旧阶段 JSON、旧 scheduler、`Main.qml` 和正式启动入口均未修改。

## 用户能力

- 浏览内置 SCI 模板与全部版本历史；内置版本保持 `builtin + readonly`。
- 克隆任意明确标记为 `cloneable` 的模板；克隆结果始终是新的 custom draft。
- 增删、重排线性节点，并编辑 executor、模型、推理、Token、PromptRef、Artifact 与 Session/Retry Policy。
- 每次保存追加新版本；`expected_parent_version_id` 提供并发 compare-and-swap，旧版本不可覆盖。
- 只有最新 custom draft 可以发布；发布新版本时，同一 custom workflow 的旧 published 版本原子降级为 deprecated。
- SQLite partial unique index 保证每个 workflow 最多一个 published 版本；公开状态迁移与内置 pack 升级均复用受约束的原子发布语义。
- 编辑器保存会同时核对 editor 的冻结来源与调用方 CAS parent，陈旧编辑器不能冒充较新版本覆盖修改。
- Prompt 编辑写入 `state/v2/resources/prompts/<sha256>.prompt`；相同内容幂等，不同内容生成新资源，任何已有 blob 漂移都拒绝而不是修复或覆盖。
- portable bundle 不包含运行时绝对路径、binding、数据库 ID 或可信发布状态；导入总是重写为调用方指定的 custom workflow ID 和 draft。
- 时间线只读取 workflow SQLite 的一致性快照，展示 run、所有 retry attempt、event、artifact 与 session 指纹，不返回输入、metadata、error message、外部 session ID 或 fencing token。

## 独立 CLI

入口：`python tools/workflow_cli.py --workspace <ROOT> <command>`

命令：

- `init [--install-sci]`
- `list`
- `show <version_id>`
- `clone <source_version_id> --workflow-id <ID> --name <NAME>`
- `edit-prompt <parent_version_id> <node_id> <prompt_file>`
- `publish <version_id>`
- `import <bundle_file> --workflow-id <ID>`
- `export <version_id> --resource RESOURCE_ID=PATH --output <FILE>`
- `timeline <run_id>`

CLI 只接受显式 workspace，不导入产品 `config.py`，不会寻找 active workspace，也不会调用 legacy runtime 或生产 scheduler。

## 安全与回滚边界

- custom API 禁止创建 `legacy-stage` 节点。
- 运行需要显式 published version、显式 runtime binding 与显式 `create_run`；P6 不注册任何生产执行回调。
- runtime binding 可在排队时只做结构冻结，但 scheduler 会在每次 executor dispatch 前重新读取并校验全部绑定文件摘要。
- 导入器严格拒绝未知 JSON 字段、重复 key、摘要漂移、绝对路径、缺失/多余 Prompt 和 legacy-stage。
- 内容寻址 Prompt 路径拒绝 symlink/reparse，写入采用同目录临时文件与排他 hard-link 发布，不覆盖既有摘要路径。
- P6 可通过本阶段 Git commit 整体回退；P0-P5 数据与旧生产入口不依赖 P6。
