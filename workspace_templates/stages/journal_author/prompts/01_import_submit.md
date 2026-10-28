任务：复制已经完成的 submit 目录。

源目录：

D:\Documents\论文画图\paper-figure-codex\papers

目标项目：

D:\Documents\期刊选刊与作者

规则：

1. 扫描源目录下所有 paper_id 文件夹。
2. 如果某个 paper_id 文件夹下存在 submit 子目录，就认为该论文已完成。
3. 不要修改源目录任何文件。
4. 将 submit 目录完整复制到：

D:\Documents\期刊选刊与作者\papers\<paper_id>\source_submit\

5. 再复制一份到：

D:\Documents\期刊选刊与作者\papers\<paper_id>\work\

6. 后续只修改 work 目录，不修改 source_submit。

7. 在 work 目录中识别主 tex 文件：
   - 如果只有一个 .tex 文件，就是主文件；
   - 如果多个，选择包含 \documentclass、\begin{document}、\title 的文件。

8. 读取 \title{...}，根据论文标题生成英文文件名 slug：
   - 小写；
   - 空格改成连字符；
   - 去掉特殊符号；
   - 不超过 100 字符。

9. 将 work 中主 tex 文件重命名为：

<论文标题slug>.tex

10. 如果有主 pdf，也重命名为：

<论文标题slug>.pdf

11. 生成：

D:\Documents\期刊选刊与作者\papers\<paper_id>\import_report.md

报告包括：
- paper_id
- 源 submit 路径
- source_submit 路径
- work 路径
- 原 tex 文件名
- 新 tex 文件名
- 论文标题
- 图片文件列表
- 是否有缺失文件
- 状态：OK / WARNING / NEEDS_USER_CONFIRMATION

本步骤只负责复制和重命名，不选期刊，不改作者，不改格式。
