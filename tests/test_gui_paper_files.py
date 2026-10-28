from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from unified_paper_orchestrator.ui_next.paper_files import paper_output_directory, paper_pdf


@pytest.fixture
def roots(tmp_path):
    return {stage: tmp_path / stage for stage in ("writing", "figures", "journal_author", "submission")}


def create(path, content=b"%PDF-test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path.resolve()


def test_figure_page_opens_submit_instead_of_writing_sources(roots):
    source = create(roots["writing"] / "papers/paper-1/draft_v6.tex", b"source")
    pdf = create(roots["figures"] / "papers/paper-1/submit/paper-1.pdf")
    record = {"stages": {"writing": {"artifact": str(source)}, "figures": {"artifact": str(pdf.parent)}}}
    assert paper_output_directory(roots, "paper-1", "figures", record) == pdf.parent
    assert paper_output_directory(roots, "paper-1", "writing", record) == source.parent
    assert paper_pdf(roots, "paper-1", "figures", record) == pdf


def test_overview_prefers_current_journal_package_and_figure_page_keeps_its_output(roots):
    figure = create(roots["figures"] / "papers/paper-1/submit/paper-1.pdf")
    package = create(roots["journal_author"] / "papers/paper-1/package/main.pdf")
    assert paper_pdf(roots, "paper-1", "overview", {}) == package
    assert paper_output_directory(roots, "paper-1", "overview", {}) == package.parent
    assert paper_pdf(roots, "paper-1", "figures", {}) == figure


def test_manuscript_manifest_supports_custom_filenames_and_stage_relative_paths(roots):
    submit = roots["figures"] / "papers/paper-1/submit"
    pdf = create(submit / "custom-paper.pdf")
    (submit / "submission_manifest.json").write_text(json.dumps({
        "compiled_pdf": "papers/paper-1/submit/custom-paper.pdf"}), encoding="utf-8")
    assert paper_pdf(roots, "paper-1", "figures", {}) == pdf


def test_individual_figures_and_archived_manuscripts_are_not_chosen(roots):
    create(roots["figures"] / "papers/paper-1/submit/figure1.pdf")
    create(roots["journal_author"] / "papers/paper-1/retarget_archives/old/main.pdf")
    assert paper_pdf(roots, "paper-1", "figures", {}) is None


def test_external_manifest_and_other_paper_artifacts_are_not_opened(roots):
    external = create(roots["journal_author"] / "papers/other/package/main.pdf")
    submit = roots["figures"] / "papers/paper-1/submit"
    submit.mkdir(parents=True)
    (submit / "submission_manifest.json").write_text(json.dumps({
        "compiled_pdf": str(external)}), encoding="utf-8")
    record = {"stages": {"figures": {"artifact": str(external)}}}
    assert paper_pdf(roots, "paper-1", "figures", record) is None
    assert paper_output_directory(roots, "../other", "overview", {}) is None


def test_controller_uses_the_selected_paper_pdf_and_reports_missing_without_execution(roots, monkeypatch):
    from unified_paper_orchestrator.ui_next import controller as module
    pdf = create(roots["figures"] / "papers/paper-1/submit/paper-1.pdf")
    opened, messages = [], []
    monkeypatch.setattr(module, "LEGACY_PROJECTS", {s: {"root": p} for s, p in roots.items()})
    monkeypatch.setattr(module.os, "startfile", opened.append)
    fake = SimpleNamespace(_selected_id="paper-1", _page="figures", _selected_record=lambda: {},
        _t=lambda key, default: default, toastRequested=SimpleNamespace(emit=lambda *x: messages.append(x)))
    module.WorkbenchController._open_selected_paper_pdf(fake)
    assert opened == [str(pdf)]
    fake._selected_id = "missing-paper"
    module.WorkbenchController._open_selected_paper_pdf(fake)
    assert len(opened) == 1
    assert messages[-1][1] == "attention"
