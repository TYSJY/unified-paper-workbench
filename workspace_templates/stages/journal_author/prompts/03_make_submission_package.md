任务：根据选定期刊和用户指定作者信息，生成干净投稿包。

项目路径：

D:\Documents\期刊选刊与作者

读取：

D:\Documents\期刊选刊与作者\author_info.md

如果存在每篇论文自己的作者信息，优先读取：

D:\Documents\期刊选刊与作者\papers\<paper_id>\author_info.md

只有当 papers\<paper_id>\author_info.md 不存在时，才使用项目根目录 author_info.md。

D:\Documents\期刊选刊与作者\papers\<paper_id>\work\<论文标题slug>.tex

D:\Documents\期刊选刊与作者\papers\<paper_id>\journal.md

输出：

D:\Documents\期刊选刊与作者\papers\<paper_id>\package\

规则：

1. 不要修改 source_submit。
2. 只处理 work 和 package。
3. 根据 journal.md 中的目标期刊，核查该期刊 author guidelines。
4. 根据期刊要求整理投稿格式。
5. 如果期刊有 LaTeX template，但当前无法自动下载，不要假装已使用模板，在 checklist 中标记 MANUAL_REQUIRED。
6. 替换作者、单位、通讯作者、邮箱、基金、冲突声明、数据可用性、代码可用性、伦理声明。
7. 作者和单位只能来自 author_info.md，优先使用 papers\<paper_id>\author_info.md。
8. 如果 author_info.md 缺信息、有 TODO、或 Metadata Status 不是 READY，停止并要求用户补充。
9. 不要编造任何作者、单位、基金或声明。
10. 不要改变论文核心内容。
11. 不要修改实验数据。
12. 不要新增实验结果。

生成投稿包：

package\
  main.tex
  main.pdf，如果可以编译
  figures\
  cover_letter.md
  declarations.md
  submission_checklist.md
  file_manifest.md
  final_risk_report.md

检查：

1. main.tex 是否存在。
2. main.pdf 是否能编译。
3. figure 文件是否存在。
4. 所有 figure 是否被正文引用。
5. 所有 table 是否被正文引用。
6. 所有 citation 是否有 bibliography。
7. 是否仍有 TODO / placeholder / 【验】 / \VERIFY / UNRESOLVED。
8. 是否有本地绝对路径。
9. 是否有作者信息。
10. 是否有单位信息。
11. 是否有通讯作者。
12. 是否有声明。
13. 是否符合目标期刊基本格式。
14. 是否存在 MANUAL_REQUIRED。

cover_letter.md 包含：

- Dear Editor
- Journal name
- Manuscript title
- Why the paper fits the journal
- Main contributions
- Originality statement
- No simultaneous submission statement
- Conflict of interest statement
- Corresponding author information

final_risk_report.md 中给出结论：

- READY_TO_SUBMIT
- NEEDS_MINOR_FIX
- BLOCKED

如果 BLOCKED，必须列出阻碍投稿的问题。

不要在线提交论文。
