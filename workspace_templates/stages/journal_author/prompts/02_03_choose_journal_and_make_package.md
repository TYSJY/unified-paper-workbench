任务：先选刊，再在安全条件满足时生成投稿包。

项目路径：

D:\Documents\期刊选刊与作者

目标论文目录：

D:\Documents\期刊选刊与作者\papers\<paper_id>

本 prompt 合并原第 2 步和第 3 步，但必须保留中间安全关卡。不要在线投稿。

## 输入

必须读取：

D:\Documents\期刊选刊与作者\papers\<paper_id>\work\<论文标题slug>.tex

如果存在，也必须读取：

D:\Documents\期刊选刊与作者\papers\<paper_id>\journal_preferences.md

作者与声明信息优先读取：

D:\Documents\期刊选刊与作者\papers\<paper_id>\author_info.md

只有当每篇论文自己的 author_info.md 不存在时，才读取：

D:\Documents\期刊选刊与作者\author_info.md

## 第一阶段：选刊

如果还没有：

D:\Documents\期刊选刊与作者\papers\<paper_id>\journal.md

则先按下面规则实时核查并生成 journal.md。

目标：

1. 默认只选择 2 区以下期刊。
2. 优先 Q3 / Q4。
3. 期刊级别低没关系，核心是范围匹配、风险低、最稳妥。
4. 如果 journal_preferences.md 显式选择 Q1 / Q2，只能作为候选偏好记录，必须解释风险并标记 Needs user confirmation。
5. 不要选择假刊、掠夺性期刊、预警期刊、收录异常期刊。
6. 不能保证中稿，只能推荐相对稳妥目标。
7. 优先非 OA / subscription / traditional publishing 路线。

必须实时核查：

- 期刊官网
- aims and scope
- author guidelines
- 是否 Scopus / WoS / ESCI / SCIE 收录
- JCR / SJR / CiteScore 分区，如果可查
- APC
- 是否接受 AI、edge AI、LLM inference、model compression、quantization、embedded systems、applied ML 类论文
- 是否近期发表过相近主题论文
- 是否支持 LaTeX
- 是否有非 OA / subscription / traditional publishing 选项

对每篇论文生成至少 5 个候选期刊，按“稳妥程度”排序。

输出：

D:\Documents\期刊选刊与作者\papers\<paper_id>\journal.md

journal.md 必须包含：

# Journal Decision

## Paper ID

## Paper Title

## Recommended Journal

## Why this journal is the safest choice

## Scope match

## Quartile / indexing evidence

## Similar published papers

## APC and cost

## Submission format notes

## Required files

## Top 5 candidates

## Rejected journals

## Final decision

Final decision 只能是：

- Recommended
- Needs user confirmation
- Do not submit yet

## 中间安全关卡

完成 journal.md 后，必须检查：

1. journal.md 的 Final decision 是否为 Recommended。
2. author_info.md 是否存在。
3. author_info.md 是否包含 `Status: READY`。
4. author_info.md 是否仍有 `TODO`。
5. journal.md 是否仍为 Needs user confirmation 或 Do not submit yet。

如果任一条件不满足：

- 停止，不执行投稿包生成。
- 在目标论文目录写入：

D:\Documents\期刊选刊与作者\papers\<paper_id>\package_blocker.md

package_blocker.md 必须说明阻塞原因，例如：

- journal decision is not Recommended
- author_info.md is not READY
- author_info.md still contains TODO
- user confirmation required

最终回复必须包含：

JOURNAL_PIPELINE_ID=<paper_id>
JOURNAL_STEP_DONE=2
PACKAGE_STATUS=BLOCKED

## 第二阶段：生成投稿包

只有当 journal.md 的 Final decision 是 Recommended，且 author_info.md 为 READY 且无 TODO，才继续生成投稿包。

输出：

D:\Documents\期刊选刊与作者\papers\<paper_id>\package\

规则：

1. 不要修改 source_submit。
2. 只处理 work 和 package。
3. 根据 journal.md 中的目标期刊，实时核查该期刊 author guidelines。
4. 根据期刊要求整理投稿格式。
5. 如果期刊有 LaTeX template，但当前无法自动下载，不要假装已使用模板，在 checklist 中标记 MANUAL_REQUIRED。
6. 替换作者、单位、通讯作者、邮箱、基金、冲突声明、数据可用性、代码可用性、伦理声明。
7. 作者和单位只能来自 author_info.md，优先使用 papers\<paper_id>\author_info.md。
8. 不要编造任何作者、单位、基金或声明。
9. 不要改变论文核心内容。
10. 不要修改实验数据。
11. 不要新增实验结果。
12. 不要在线投稿。
13. 最终主投稿包必须是作者可见版：`package\main.tex` 和 `package\main.pdf` 必须写入 `author_info.md` 中的真实作者、单位、通讯作者和邮箱。
14. 如果目标期刊要求双盲，额外生成 `package\main_blind.tex` 和 `package\main_blind.pdf`；不要用匿名稿覆盖 `package\main.tex` / `package\main.pdf`。
15. 如果 `package\main.tex` 或 `package\main.pdf` 中仍包含 `Anonymous Authors`，而 `author_info.md` 已有真实作者，必须视为 BLOCKED 并先修复。
16. `work\` 和 `source_submit\` 可以保持原始匿名稿；最终检查和给用户打开的投稿文件以 `package\` 为准，并在报告里说明。

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
  package_metadata_fingerprint.json

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
15. `package\main.tex` 和 `package\main.pdf` 是否能读到真实作者与单位。
16. 如果生成双盲稿，双盲稿必须命名为 `main_blind.*`，不能替代主稿 `main.*`。

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
如果成功生成或更新 `package\`，必须读取论文根目录的
`submission_metadata_fingerprint.json`，并原样复制为
`package\package_metadata_fingerprint.json`。这个文件用于控制面板判断
package 是否对应当前作者、单位、声明、选刊偏好和投稿补全信息；不要手写或改写其中的 sha256。

完成后最终回复必须包含：

JOURNAL_PIPELINE_ID=<paper_id>
JOURNAL_STEP_DONE=3
PACKAGE_STATUS=<READY_TO_SUBMIT | NEEDS_MINOR_FIX | BLOCKED>
