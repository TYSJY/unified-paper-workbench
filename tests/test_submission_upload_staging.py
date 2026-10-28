from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from unified_paper_orchestrator.submission_upload_staging import (
    SubmissionUploadStagingError,
    stage_snapp_latex_archive,
)


def test_snapp_archive_contains_only_referenced_latex_sources(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n"
        "\\includegraphics{figure1.pdf}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (package / "figure1.pdf").write_bytes(b"figure")
    (package / "main.pdf").write_bytes(b"compiled manuscript")
    (package / "main.log").write_text("build log", encoding="utf-8")

    first = stage_snapp_latex_archive(package, tmp_path / "upload.zip")
    second = stage_snapp_latex_archive(package, tmp_path / "upload-2.zip")

    assert first.sha256 == second.sha256
    assert first.files == ("figure1.pdf", "main.tex")
    with zipfile.ZipFile(first.path) as archive:
        assert archive.namelist() == ["figure1.pdf", "main.tex"]


def test_snapp_archive_fails_when_a_referenced_figure_is_missing(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "main.tex").write_text(
        "\\includegraphics{missing-figure.pdf}\n",
        encoding="utf-8",
    )

    with pytest.raises(SubmissionUploadStagingError, match="unresolved"):
        stage_snapp_latex_archive(package, tmp_path / "upload.zip")
