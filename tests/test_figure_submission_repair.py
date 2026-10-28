from __future__ import annotations

import json
from pathlib import Path

from unified_paper_orchestrator.figure_output_contract import validate_figure_step
from unified_paper_orchestrator import figure_submission_repair as repair_module


PAPER_ID = "2026-07-16_999_repair-contract"
MERGED_PAPER_ID = "2026-07-23_998_merged-label-repair"


def _write_broken_submission(root: Path) -> Path:
    paper = root / "papers" / PAPER_ID
    submit = paper / "submit"
    submit.mkdir(parents=True)
    source = r"""\documentclass{article}
\begin{document}
See Figures~\ref{fig:a} and~\ref{fig:b}, plus Table~\ref{tab:a}.
\section{Evaluation}
\begin{figure}[t]
\centering
\rule{1cm}{1cm}
\caption{Old A}
\label{fig:a}
\end{figure}
Text between figures must survive.
\begin{figure}[t]
\centering
\rule{1cm}{1cm}
\caption{Old B}
\label{fig:b}
\end{figure}
\begin{table}[t]
\caption{Measurements}
\label{tab:a}
\begin{tabular}{c}1\end{tabular}
\end{table}
\end{document}
"""
    (paper / "input.tex").write_text(source, encoding="utf-8")
    final_figure = submit / "figure1_combined.pdf"
    final_figure.write_bytes(b"%PDF-1.4\nfigure\n")
    broken_tex = submit / f"{PAPER_ID}.tex"
    broken_tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n"
        "\\includegraphics{figure1_combined.pdf}\n"
        "See Figure~\\ref{fig:a}.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (submit / f"{PAPER_ID}.pdf").write_bytes(b"%PDF-1.4\nbroken\n")
    (submit / f"{PAPER_ID}.log").write_text(
        "LaTeX Warning: There were undefined references.\n",
        encoding="utf-8",
    )
    manifest = {
        "paper_id": PAPER_ID,
        "source_tex": f"papers/{PAPER_ID}/input.tex",
        "submit_tex": f"papers/{PAPER_ID}/submit/{PAPER_ID}.tex",
        "compiled_pdf": f"papers/{PAPER_ID}/submit/{PAPER_ID}.pdf",
        "compiled_pdf_created": True,
        "compile_status": "success",
        "figures": [
            {
                "figure": "Figure 1",
                "label": "fig:a, fig:b",
                "figure_type": "quantitative_data_figure",
                "renderer": "matplotlib",
                "final_pdf": f"papers/{PAPER_ID}/submit/figure1_combined.pdf",
                "inserted_in_tex": True,
                "caption": "Combined result.",
            }
        ],
        "tables": ["tab:a"],
        "data_requirements": [],
    }
    (submit / "submission_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8-sig",
    )
    return paper


def test_strict_contract_detects_structural_truncation(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    _write_broken_submission(root)

    result = validate_figure_step(root, PAPER_ID, 3)

    assert result.ok is False
    assert any("未解析交叉引用" in issue for issue in result.issues)
    assert any("table 环境异常减少" in issue for issue in result.issues)
    assert any("undefined references" in issue for issue in result.issues)


def test_programmatic_repair_restores_source_structure_without_model_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "figures"
    paper = _write_broken_submission(root)

    def fake_compile(candidate_tex: Path) -> tuple[bool, str]:
        candidate_tex.with_suffix(".pdf").write_bytes(b"%PDF-1.4\nrepaired\n")
        candidate_tex.with_suffix(".log").write_text(
            "Output written on repaired.pdf.\n",
            encoding="utf-8",
        )
        return True, ""

    monkeypatch.setattr(repair_module, "_compile_candidate", fake_compile)

    repaired = repair_module.repair_figure_submission(root, PAPER_ID)

    assert repaired.ok is True
    assert repaired.repaired is True
    final_contract = validate_figure_step(root, PAPER_ID, 3)
    assert final_contract.ok is True
    final_tex = (paper / "submit" / f"{PAPER_ID}.tex").read_text(encoding="utf-8")
    assert "\\label{fig:a}" in final_tex
    assert "\\label{fig:b}" in final_tex
    assert "\\label{tab:a}" in final_tex
    assert "Text between figures must survive." in final_tex
    assert final_tex.count("\\includegraphics") == 1
    manifest = json.loads(
        (paper / "submit" / "submission_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["programmatic_repair"]["model_turn_started"] is False


def test_programmatic_repair_grafts_explicitly_planned_merged_label(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / MERGED_PAPER_ID
    submit = paper / "submit"
    submit.mkdir(parents=True)
    source = r"""\documentclass{article}
\begin{document}
See Figures~\ref{fig:architecture} and~\ref{fig:state-machine}.
\begin{figure}[t]
\centering
\rule{1cm}{1cm}
\caption{Architecture}
\label{fig:architecture}
\end{figure}
\begin{figure}[t]
\centering
\rule{1cm}{1cm}
\caption{State machine}
\label{fig:state-machine}
\end{figure}
\end{document}
"""
    (paper / "input.tex").write_text(source, encoding="utf-8")
    (paper / "figure_plan.md").write_text(
        "| `fig:architecture` | runtime architecture | replace with Fig. 2 |\n"
        "| `fig:state-machine` | state machine | merge with Fig. 2 |\n",
        encoding="utf-8",
    )
    for name in ("figure1_hero.pdf", "figure2_runtime.pdf"):
        (submit / name).write_bytes(b"%PDF-1.4\nfigure\n")
    generated = r"""\documentclass{article}
\usepackage{graphicx}
\begin{document}
See Figures~\ref{fig:architecture} and~\ref{fig:state-machine}.
\begin{figure}[t]
\centering
\includegraphics{figure1_hero.pdf}
\caption{Teaser}
\label{fig:hero}
\end{figure}
\begin{figure}[t]
\centering
\includegraphics{figure2_runtime.pdf}
\caption{Merged runtime and state machine}
\label{fig:architecture}
\end{figure}
\end{document}
"""
    submit_tex = submit / f"{MERGED_PAPER_ID}.tex"
    submit_tex.write_text(generated, encoding="utf-8")
    submit_tex.with_suffix(".pdf").write_bytes(b"%PDF-1.4\ncompiled\n")
    submit_tex.with_suffix(".log").write_text(
        "Output written on merged.pdf.\n",
        encoding="utf-8",
    )
    manifest = {
        "paper_id": "wrong-paper-id",
        "source_tex": f"papers/{MERGED_PAPER_ID}/input.tex",
        "submit_tex": f"papers/wrong-paper-id/submit/{MERGED_PAPER_ID}.tex",
        "compiled_pdf": f"papers/wrong-paper-id/submit/{MERGED_PAPER_ID}.pdf",
        "compiled_pdf_created": True,
        "compile_status": "success",
        "figures": [
            {
                "figure": "Figure 1",
                "label": "fig:hero",
                "figure_type": "quantitative_data_figure",
                "renderer": "matplotlib",
                "final_pdf": f"papers/{MERGED_PAPER_ID}/submit/figure1_hero.pdf",
                "inserted_in_tex": False,
            },
            {
                "figure": "Figure 2",
                "label": "fig:architecture",
                "figure_type": "quantitative_data_figure",
                "renderer": "matplotlib",
                "final_pdf": f"papers/{MERGED_PAPER_ID}/submit/figure2_runtime.pdf",
                "inserted_in_tex": False,
            },
        ],
        "tables": [],
        "data_requirements": [],
    }
    (submit / "submission_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_compile(candidate_tex: Path) -> tuple[bool, str]:
        candidate_tex.with_suffix(".pdf").write_bytes(b"%PDF-1.4\nrepaired\n")
        candidate_tex.with_suffix(".log").write_text(
            "Output written on repaired.pdf.\n",
            encoding="utf-8",
        )
        return True, ""

    monkeypatch.setattr(repair_module, "_compile_candidate", fake_compile)

    repaired = repair_module.repair_figure_submission(root, MERGED_PAPER_ID)

    assert repaired.ok is True
    assert repaired.repaired is True
    final_contract = validate_figure_step(root, MERGED_PAPER_ID, 3)
    assert final_contract.ok is True
    final_tex = submit_tex.read_text(encoding="utf-8")
    assert final_tex.count("\\label{fig:state-machine}") == 1
    assert "\\label{fig:hero}" in final_tex
    final_manifest = json.loads(
        (submit / "submission_manifest.json").read_text(encoding="utf-8")
    )
    assert final_manifest["programmatic_repair"]["mode"] == (
        "bounded-planned-label-graft"
    )
    assert final_manifest["paper_id"] == MERGED_PAPER_ID
    assert final_manifest["submit_tex"].endswith(f"/{MERGED_PAPER_ID}.tex")
    assert all(entry["inserted_in_tex"] for entry in final_manifest["figures"])
    assert "fig:state-machine" in final_manifest["figures"][1]["label"]


def test_restore_checkpoint_can_preserve_invalid_submission(tmp_path: Path) -> None:
    root = tmp_path / "figures"
    paper = root / "papers" / PAPER_ID
    submit = paper / "submit"
    submit.mkdir(parents=True)
    (submit / "original.txt").write_text("original", encoding="utf-8")
    checkpoint = repair_module.create_figure_submission_checkpoint(
        root,
        PAPER_ID,
        attempt_id="preserve-invalid",
    )
    (submit / "original.txt").unlink()
    (submit / "invalid.txt").write_text("invalid", encoding="utf-8")

    restored = repair_module.restore_figure_submission_checkpoint(
        root,
        PAPER_ID,
        checkpoint,
        preserve_failed=True,
    )

    assert restored is True
    assert (submit / "original.txt").read_text(encoding="utf-8") == "original"
    archives = list(paper.glob(".upo-failed-submit-*"))
    assert len(archives) == 1
    assert (archives[0] / "invalid.txt").read_text(encoding="utf-8") == "invalid"
