from __future__ import annotations

import json
from pathlib import Path

from unified_paper_orchestrator.journal_author_output_contract import (
    validate_journal_author_output,
)
from unified_paper_orchestrator.journal_submission_policy import (
    DEFAULT_AI_USE_STATEMENT,
    apply_submission_defaults_to_author_info,
    apply_submission_defaults_to_record,
    finalize_journal_submission_policy,
)


PAPER_ID = "2026-07-16_002_policy-test"


def _required_journal_files(root: Path) -> tuple[Path, Path]:
    paper = root / "papers" / PAPER_ID
    work = paper / "work"
    package = paper / "package"
    work.mkdir(parents=True)
    package.mkdir(parents=True)
    manuscript = (
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{url}\n"
        "\\newcommand{\\method}{KVC-Spec}\n"
        "\\title{\\method: A Policy Test}\n"
        "\\author{Alice Example}\n"
        "\\begin{document}\n"
        "\\begin{abstract}\n"
        + " ".join(["result"] * 120)
        + "\n\\end{abstract}\n"
        "\\includegraphics{figures/figure1.pdf}\n"
        "\\section*{Declarations}\n"
        "\\textbf{Code availability.} The source code will be released under the author's GitHub account "
        "(https://github.com/TYSJY); the repository-specific URL should be inserted before final online submission.\n"
        "\\begin{thebibliography}{1}\\end{thebibliography}\n"
        "\\end{document}\n"
    )
    (work / f"{PAPER_ID}.tex").write_text(manuscript, encoding="utf-8")
    (paper / "author_info.md").write_text(
        "# Author Information\n\nStatus: READY\n\n"
        "## Code Availability\n\n"
        "The source code will be released under the author's GitHub account "
        "(https://github.com/TYSJY); the repository-specific URL should be inserted before final online submission.\n\n"
        "## Ethics Statement\n\nNo human participants were involved.\n",
        encoding="utf-8",
    )
    (paper / "journal.md").write_text(
        "# Journal Decision\n\n"
        "## Submission format notes\n\n"
        "The abstract must contain 100-150 words. Please avoid subfolders for LaTeX uploads. "
        "The Springer template is recommended.\n\n"
        "## Final decision\n\nFinal decision: Recommended\n",
        encoding="utf-8",
    )
    (package / "main.tex").write_text(manuscript, encoding="utf-8")
    (package / "main.pdf").write_bytes(b"%PDF-1.4\npolicy-test\n")
    figures = package / "figures"
    figures.mkdir()
    (figures / "figure1.pdf").write_bytes(b"%PDF-1.4\nfigure\n")
    (package / "declarations.md").write_text(
        "# Declarations\n\n"
        "## Code Availability\n\n"
        "The repository-specific URL should be inserted before final online submission.\n\n"
        "## Ethics Statement\n\nNo human participants were involved.\n",
        encoding="utf-8",
    )
    for name in ("cover_letter.md", "submission_checklist.md", "file_manifest.md"):
        (package / name).write_text(f"# {name}\n", encoding="utf-8")
    (package / "final_risk_report.md").write_text(
        "# Final Risk Report\n\n"
        "PACKAGE_STATUS=NEEDS_MINOR_FIX\n\n"
        "## Issues to fix before online submission\n\n"
        "1. The Springer template is recommended but has not been applied. MANUAL_REQUIRED.\n"
        "2. Flatten figure subfolders before online submission. MANUAL_REQUIRED.\n"
        "3. Insert the repository-specific code URL before online submission.\n",
        encoding="utf-8",
    )
    return paper, package


def test_submission_defaults_use_manuscript_method_name_and_standing_ai_statement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    paper, _ = _required_journal_files(root)
    record: dict[str, object] = {
        "paper_id": PAPER_ID,
        "code_availability": (
            "The source code will be released under the author's GitHub account "
            "(https://github.com/TYSJY); the repository-specific URL should be inserted."
        ),
        "submission_requirements": {
            "github_repo_url": "",
            "ai_use_statement": "",
        },
    }
    identity = apply_submission_defaults_to_record(root, PAPER_ID, record)
    assert identity.repository_name == "KVC-Spec"
    assert identity.repository_url == "https://github.com/TYSJY/KVC-Spec"
    requirements = record["submission_requirements"]
    assert isinstance(requirements, dict)
    assert requirements["github_repo_url"] == identity.repository_url
    assert requirements["ai_use_statement"] == DEFAULT_AI_USE_STATEMENT
    assert apply_submission_defaults_to_author_info(
        paper / "author_info.md",
        identity,
    )
    assert not apply_submission_defaults_to_author_info(
        paper / "author_info.md",
        identity,
    )
    author_info = (paper / "author_info.md").read_text(encoding="utf-8")
    assert identity.repository_url in author_info
    assert DEFAULT_AI_USE_STATEMENT in author_info
    assert author_info.count("## AI Use Statement") == 1
    assert "## Ethics Statement" in author_info


def test_programmatic_finalizer_resolves_automatic_minor_fixes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    paper, package = _required_journal_files(root)
    record: dict[str, object] = {
        "paper_id": PAPER_ID,
        "submission_requirements": {"ai_use_policy": "ALWAYS"},
        "code_availability": "",
    }
    identity = apply_submission_defaults_to_record(root, PAPER_ID, record)
    apply_submission_defaults_to_author_info(paper / "author_info.md", identity)
    payload = {
        "paper_id": PAPER_ID,
        "paper_title": identity.paper_title,
        "code_availability": record["code_availability"],
        "submission_requirements": record["submission_requirements"],
    }
    fingerprint = {
        "version": 1,
        "paper_id": PAPER_ID,
        "sha256": "test",
        "payload": payload,
    }
    (paper / "submission_metadata_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2),
        encoding="utf-8",
    )
    (package / "package_metadata_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2),
        encoding="utf-8",
    )

    result = finalize_journal_submission_policy(
        root,
        PAPER_ID,
        compile_tex=False,
    )
    assert result.ok is True
    assert result.applied is True
    repeated = finalize_journal_submission_policy(
        root,
        PAPER_ID,
        compile_tex=False,
    )
    assert repeated.ok is True
    main = (package / "main.tex").read_text(encoding="utf-8")
    declarations = (package / "declarations.md").read_text(encoding="utf-8")
    assert "\\includegraphics{figure1.pdf}" in main
    assert identity.repository_url in main
    assert DEFAULT_AI_USE_STATEMENT in main
    assert identity.repository_url in declarations
    assert DEFAULT_AI_USE_STATEMENT in declarations
    assert declarations.count("## AI Use Statement") == 1
    assert "## Ethics Statement" in declarations
    assert (package / "figure1.pdf").is_file()
    assert not (package / "figures").exists()
    manifest = json.loads(
        (package / "submission_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "unified-paper-orchestrator/submission-manifest/v1"
    assert manifest["paper_id"] == PAPER_ID
    assert manifest["contract_authority"] == "journal_author_output_contract"
    assert manifest["upload_candidates"]["manuscript_review_pdf"] == "main.pdf"
    assert manifest["upload_candidates"]["manuscript_source"] == "main.tex"
    source_fingerprint = json.loads(
        (paper / "submission_metadata_fingerprint.json").read_text(encoding="utf-8")
    )
    assert manifest["metadata_fingerprint"]["sha256"] == source_fingerprint["sha256"]
    assert any(item["path"] == "main.pdf" for item in manifest["files"])
    (package / "main.pdf").touch()

    contract = validate_journal_author_output(root, PAPER_ID)
    assert contract.ok is True, contract.as_dict()
    assert contract.package_status == "READY_TO_SUBMIT"
    assert any("程序复核" in warning for warning in contract.warnings)


def test_real_manual_action_remains_visible_and_minor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    paper, package = _required_journal_files(root)
    record: dict[str, object] = {
        "paper_id": PAPER_ID,
        "submission_requirements": {"ai_use_policy": "ALWAYS"},
        "code_availability": "",
    }
    identity = apply_submission_defaults_to_record(root, PAPER_ID, record)
    payload = {
        "paper_id": PAPER_ID,
        "paper_title": identity.paper_title,
        "code_availability": record["code_availability"],
        "submission_requirements": record["submission_requirements"],
    }
    fingerprint = {
        "version": 1,
        "paper_id": PAPER_ID,
        "sha256": "test",
        "payload": payload,
    }
    (paper / "submission_metadata_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2),
        encoding="utf-8",
    )
    apply_submission_defaults_to_author_info(paper / "author_info.md", identity)
    finalize_journal_submission_policy(root, PAPER_ID, compile_tex=False)
    (package / "main.pdf").touch()
    (package / "final_risk_report.md").write_text(
        "# Final Risk Report\n\n"
        "PACKAGE_STATUS=NEEDS_MINOR_FIX\n\n"
        "## Minor Fixes Before Online Submission\n\n"
        "- Four expert reviewer suggestions must be supplied in the online form.\n",
        encoding="utf-8",
    )

    contract = validate_journal_author_output(root, PAPER_ID)
    assert contract.ok is True
    assert contract.package_status == "NEEDS_MINOR_FIX"
    assert any(
        warning == "需人工处理：在线投稿表单需要填写 4 位推荐审稿专家；当前尚未提供"
        for warning in contract.warnings
    )


def test_conditional_online_template_and_contribution_items_do_not_block(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    paper, package = _required_journal_files(root)
    record: dict[str, object] = {
        "paper_id": PAPER_ID,
        "submission_requirements": {"ai_use_policy": "ALWAYS"},
        "code_availability": "",
    }
    identity = apply_submission_defaults_to_record(root, PAPER_ID, record)
    payload = {
        "paper_id": PAPER_ID,
        "paper_title": identity.paper_title,
        "code_availability": record["code_availability"],
        "submission_requirements": record["submission_requirements"],
    }
    fingerprint = {
        "version": 1,
        "paper_id": PAPER_ID,
        "sha256": "test",
        "payload": payload,
    }
    (paper / "submission_metadata_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2),
        encoding="utf-8",
    )
    apply_submission_defaults_to_author_info(paper / "author_info.md", identity)
    finalize_journal_submission_policy(root, PAPER_ID, compile_tex=False)
    (package / "main.pdf").touch()
    (paper / "journal.md").write_text(
        "# Journal Decision\n\n"
        "## Submission format notes\n\n"
        "SAGE accepts LaTeX files and provides a template.\n\n"
        "## Final decision\n\nFinal decision: Recommended\n",
        encoding="utf-8",
    )
    (package / "final_risk_report.md").write_text(
        "# Final Risk Report\n\n"
        "PACKAGE_STATUS=NEEDS_MINOR_FIX\n\n"
        "## Remaining Fixes Before Online Submission\n\n"
        "1. MANUAL_REQUIRED: Convert to the official SAGE LaTeX template or confirm the current article-class LaTeX file is acceptable in ScholarOne/Sage Track.\n"
        "2. MANUAL_REQUIRED: Rewrite the abstract into SAGE's structured abstract style if the submission system enforces the requirement.\n"
        "3. MANUAL_REQUIRED: Enter author contribution metadata if requested by the online system.\n",
        encoding="utf-8",
    )

    contract = validate_journal_author_output(root, PAPER_ID)

    assert contract.ok is True
    assert contract.package_status == "READY_TO_SUBMIT"
    assert not any(warning.startswith("需人工处理：") for warning in contract.warnings)
    assert sum(warning.startswith("非阻塞提示：") for warning in contract.warnings) == 2


def test_complete_reviewer_file_turns_online_entry_into_non_blocking_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    paper, package = _required_journal_files(root)
    record: dict[str, object] = {
        "paper_id": PAPER_ID,
        "submission_requirements": {"ai_use_policy": "ALWAYS"},
        "code_availability": "",
    }
    identity = apply_submission_defaults_to_record(root, PAPER_ID, record)
    apply_submission_defaults_to_author_info(paper / "author_info.md", identity)
    payload = {
        "paper_id": PAPER_ID,
        "paper_title": identity.paper_title,
        "code_availability": record["code_availability"],
        "submission_requirements": record["submission_requirements"],
    }
    fingerprint = {
        "version": 1,
        "paper_id": PAPER_ID,
        "payload": payload,
        "sha256": "test",
    }
    (paper / "submission_metadata_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2),
        encoding="utf-8",
    )
    finalize_journal_submission_policy(root, PAPER_ID, compile_tex=False)
    (package / "main.pdf").touch()
    reviewers = [
        (
            f"## {index}. Expert {index}\n\n"
            f"- Email: expert{index}@example.edu\n"
            f"- Full address: University {index}, City {index}, Country {index}\n"
            f"- Expertise: mobile and edge systems\n"
        )
        for index in range(1, 5)
    ]
    (package / "suggested_reviewers.md").write_text(
        "# Suggested Reviewers\n\n" + "\n".join(reviewers),
        encoding="utf-8",
    )
    (package / "final_risk_report.md").write_text(
        "# Final Risk Report\n\n"
        "PACKAGE_STATUS=NEEDS_MINOR_FIX\n\n"
        "## Remaining Fixes Before Online Submission\n\n"
        "1. MANUAL_REQUIRED: Enter the four suggested reviewers from suggested_reviewers.md into the online submission form.\n"
        "2. MANUAL_REQUIRED: Apply the official template if the user wants template-level conformity before upload.\n"
        "3. MANUAL_REQUIRED: Convert references to Harvard style if the submission system enforces it before review.\n"
        "4. MANUAL_REQUIRED: Move figures and tables to the end if the submission system requires strict layout.\n",
        encoding="utf-8",
    )

    contract = validate_journal_author_output(root, PAPER_ID)

    assert contract.ok is True, contract.as_dict()
    assert contract.package_status == "READY_TO_SUBMIT"
    assert not any(warning.startswith("需人工处理：") for warning in contract.warnings)
    assert any("suggested reviewers" in warning for warning in contract.warnings)
