"""Offline Word integration fixture. Never opens a real paper or starts a model."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from unified_paper_orchestrator.undergraduate_word import SCHEMA, export_word, write_context


def prepare(root: Path) -> str:
    paper_id = "word-delivery-fixture"
    paper = root / "papers" / paper_id
    submit = paper / "submit"
    submit.mkdir(parents=True, exist_ok=True)
    title = "本科论文 Word 交付验证样稿"
    brief = {"title": title, "requirements": "验证样稿：A4，宋体小四，1.5倍行距，包含目录、原生表格、配图和参考文献。不是实际学校标准。", "minimum_references": None}
    write_context(root, paper_id, {"prompt_scheme": "undergraduate_software", "thesis_brief": brief})
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)
    image = Image.new("RGB", (1400, 420), "white")
    draw = ImageDraw.Draw(image)
    for x, label in ((50, "填写题目与学校要求"), (500, "写作与图表整合"), (950, "Word 排版与复核")):
        draw.rounded_rectangle((x, 130, x + 380, 260), radius=12, outline="#24557a", width=4)
        draw.text((x + 22, 176), label, font=font, fill="#203446")
    for x in (440, 890):
        draw.line((x, 195, x + 48, 195), fill="#24557a", width=4)
        draw.polygon([(x+48, 195), (x+36, 186), (x+36, 204)], fill="#24557a")
    image.save(submit / "figure01.pdf", "PDF", resolution=150)
    image.close()
    tex = r"""\documentclass{ctexart}
\usepackage{graphicx,booktabs,amsmath}
\title{本科论文 Word 交付验证样稿}
\author{}
\date{}
\begin{document}
\maketitle
\section*{摘要}
本文仅用于验证软件的 Word 交付流程，检查中文正文、标题结构、图表、目录、引用与公式在格式转换后是否保留。本样稿没有报告实际调查或实验结论。软件应先完成内容与图表整合，再生成可以编辑的 Word 文件，最后从该文件导出预览并逐页复核。
\section{绪论}
本科论文交付需要兼顾内容和学校格式。本例使用明确的测试格式，所有样例内容仅用于验证软件，不能作为真实研究成果。Word 应保留可编辑段落与标题样式，而不能把整篇论文变成图片。流程如图\ref{fig:flow}所示。
\begin{figure}[htbp]
\centering
\includegraphics[width=0.95\textwidth]{figure01.pdf}
\caption{论文交付验证流程}
\label{fig:flow}
\end{figure}
\subsection{验证范围}
检查范围包含输入材料、写作中间稿、图表整合和 Word 交付。目录与页码需要通过排版引擎更新；图片必须嵌入文档，表格应保留 Word 原生结构，引用应与文末列表一致。格式转换需要检查信息是否完整，不能把生成文件等同于通过验收。
\section{实现与检查}
本例使用本地转换和排版，不调用 AI 生成实验数据。表\ref{tab:checks}列出本次检查项目，项目名称是软件验证需求，并非调查统计结果。公式 $y = ax + b$ 用于验证 Word 原生公式的保留。
\begin{table}[htbp]
\centering
\caption{交付检查项目}
\label{tab:checks}
\begin{tabular}{lll}
\toprule
检查对象 & 预期形式 & 检查方法 \\
\midrule
正文与标题 & 可编辑文字 & 检查段落和样式 \\
图表 & 内嵌图片与原生表格 & 核对对象数量 \\
目录与页码 & 已更新字段 & 查看实际预览 \\
\bottomrule
\end{tabular}
\end{table}
\subsection{异常处理}
当 Word 无法渲染，或者缺少当前版本的逐页复核时，软件应保留未完成状态。若原稿、图文件或排版配置变化，需要重新生成并检查预览，旧检查记录不得继续代表新文件。相关文字处理格式的基础资料见文献\cite{ecma}。
\section{结论}
本样稿的目的仅为验证软件交付链路。只有实际生成并检查 Word，才能说明软件提供了可编辑文档；本科论文的学术内容和具体学校要求仍需要各自的证据和排版检查。本例不代表任何学生的毕业成果。
\begin{thebibliography}{9}
\bibitem{ecma} Ecma International. Office Open XML File Formats. ECMA-376.
\end{thebibliography}
\end{document}
"""
    source = submit / "integrated.tex"
    source.write_text(tex, encoding="utf-8")
    (submit / "submission_manifest.json").write_text(json.dumps({"paper_id": paper_id, "submit_tex": source.relative_to(root).as_posix()}), encoding="utf-8")
    styles = {name: {"font": "黑体" if name.startswith("Heading") or name == "Title" else "宋体", "size_pt": size}
              for name, size in (("Normal", 12), ("Title", 20), ("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 12), ("Caption", 10.5))}
    styles["Normal"].update(first_line_chars=2, line_multiple=1.5)
    styles["Title"].update(align="center")
    styles["Caption"].update(align="center")
    layout = {"schema": SCHEMA, "unresolved": [], "requirements": [
        {"requirement": "A4、宋体小四、1.5倍行距", "source": "离线验证样稿要求", "implementation": "页面和 Normal 样式"},
        {"requirement": "可编辑正文、目录页码、内嵌图、原生表格和公式", "source": "离线验证样稿要求", "implementation": "转换后检查 Word XML 与所有预览页"}],
        "page": {"width_cm": 21, "height_cm": 29.7, "top_cm": 2.5, "bottom_cm": 2.5, "left_cm": 3, "right_cm": 2.5},
        "styles": styles, "toc": True, "page_numbers": True}
    (paper / "word_layout.json").write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    return paper_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    if root.exists():
        raise SystemExit("Use a new fixture directory")
    paper_id = prepare(root)
    result = export_word(root, paper_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
