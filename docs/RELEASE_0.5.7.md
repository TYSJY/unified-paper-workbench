# 0.5.7 发布记录：LaTeX 真实可编译性加固

## 真实编译发现

`2026-07-13_002_adaptive-npu-rendezvous` 的 Step 3 产物通过了原有结构门禁，但使用本机 `pdflatex -halt-on-error` 真实编译时，在普通文本里发现三个未转义下划线：

- 正文中的 `io_uring`；
- 正文中的 `novelty_check.md`；
- 内嵌参考文献标题中的 `io_uring`。

编译器因此报告 `Missing $ inserted`。这是结构统计无法发现、但用户真实使用时会遇到的可编译性问题。

## 确定性预检

- 在 Step 3、4、5、6、8、9、10 的既有 LaTeX 语义验收中增加裸下划线检测。
- 数学环境与 `$...$`、`\(...\)`、`\[...\]` 中的下标不误报。
- `\cite`、`\bibitem`、`\label`、`\ref`、`\url`、`\path`、`\href`、`\includegraphics` 等安全参数不误报。
- LaTeX 注释、`\verb`/`\verb*` 内容和已转义的 `\_` 不误报。
- 检测结果记录稳定错误码 `latex_unescaped_text_underscore` 和计数，供审计、重验与安全恢复使用。
- 上下文识别改为单向游标状态机；未闭合的 `\[`、`\(`、数学环境、受保护参数和 verbatim 构造均失败关闭，不再用可能灾难性回溯的跨文档正则。
- 注释与 verbatim 直接在原始 LaTeX 上按词法优先级处理；约 40 KB 的真实 Step 5 论文重验由卡住恢复到约 0.01 秒，并新增大输入与畸形嵌套性能回归。

## 安全重试

重试信封忽略 journal 里的自由文本，只把稳定错误码转换为控制器内置、限长、白名单指令。修复仍绑定原产物哈希、原 Step 3 Prompt 哈希和同一固定 Codex 会话；文件或 Prompt 在发送前发生变化时会拒绝运行。

所有用户调优 Prompt 保持原字节不变。

## 并发状态显示

`waiting_event` 论文已经释放 SDK 许可，不再计入 `active_count`。Step 6 因缺少真实证据而等待时，界面明确显示“等待真实实验数据（不占用并发）”，不再让用户误以为后台仍有模型会话运行。运行时状态同时新增 `waiting_event_count`，并将 `recovery_count` 专用于真正未完成的持久恢复事务。
