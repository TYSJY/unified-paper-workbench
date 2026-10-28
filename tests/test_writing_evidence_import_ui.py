from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from unified_paper_orchestrator.ui import main_window as ui_module
from unified_paper_orchestrator.ui.main_window import (
    UnifiedControlPanel,
    _writing_existing_evidence_summary,
    _writing_evidence_import_eligible,
    _writing_evidence_import_summary,
    _writing_evidence_transition_eligible,
)


UI_PATH = Path(ui_module.__file__)


def function_source(name: str) -> str:
    source = UI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function not found: {name}")


def waiting_evidence_record() -> dict:
    return {
        "stages": {
            "writing": {
                "status": "blocked",
                "step_done": 5,
                "evidence_gate": {"status": "BLOCKED", "ready": False},
            }
        }
    }


def fake_panel(record: dict) -> SimpleNamespace:
    resumed_state = {"last_scheduler_result": {"results": {"writing": {"ok": True}}}}
    panel = SimpleNamespace(
        selected_ids=lambda: ["paper-1"],
        state_data={"papers": {"paper-1": record}},
        _set_technical_details=Mock(),
        refresh=Mock(),
        status_var=SimpleNamespace(set=Mock()),
        orchestrator=SimpleNamespace(resume_writing_papers=Mock(return_value=resumed_state)),
    )
    panel._continue_writing_after_evidence = lambda paper_id: UnifiedControlPanel._continue_writing_after_evidence(
        panel,
        paper_id,
    )
    return panel


def test_writing_tab_exposes_clear_real_evidence_action() -> None:
    source = function_source("_build_stage_tab")

    assert 'if stage == "writing"' in source
    assert 'text="导入真实实验证据…"' in source
    assert "import_selected_writing_evidence" in source
    assert "continue_selected_writing_with_existing_evidence" in source
    assert "数据已手动改完并继续" in source


def test_evidence_ui_handler_imports_then_continues_without_direct_runtime_access() -> None:
    source = function_source("import_selected_writing_evidence")

    assert "_continue_writing_after_evidence" in source
    assert ".runtime" not in source
    assert "expected_manifest_sha256" in source
    assert "default=messagebox.YES" in source


def test_import_eligibility_is_limited_to_step5_blocked_gate() -> None:
    assert _writing_evidence_import_eligible(waiting_evidence_record()) is True
    ready = waiting_evidence_record()
    ready["stages"]["writing"]["evidence_gate"]["ready"] = True
    assert _writing_evidence_import_eligible(ready) is False
    wrong_step = waiting_evidence_record()
    wrong_step["stages"]["writing"]["step_done"] = 4
    assert _writing_evidence_import_eligible(wrong_step) is False
    assert _writing_evidence_import_eligible({}) is False


def test_manual_transition_accepts_proven_step5_output_with_only_deferred_cleanup() -> None:
    live = {
        "status": "blocked",
        "completed_step": 4,
        "blocker": "WRITING_OUTPUT_SEMANTIC_INVALID",
        "steps": {
            "5": {
                "state": "blocked",
                "semantic_validation": {
                    "ready": False,
                    "issues": [
                        {"code": "latex_unescaped_verify_data_identifier"},
                        {"code": "latex_unescaped_text_underscore"},
                    ],
                },
            }
        },
    }

    assert _writing_evidence_transition_eligible({}, live) is True
    live["steps"]["5"]["semantic_validation"]["issues"].append(
        {"code": "latex_bibitems_insufficient"}
    )
    assert _writing_evidence_transition_eligible({}, live) is False


def test_ui_checks_previews_confirms_commits_and_continues(
    tmp_path: Path,
    monkeypatch,
) -> None:
    papers_dir = tmp_path / "papers"
    spec_path = papers_dir / "paper-1" / "results" / "evidence_import.json"
    panel = fake_panel(waiting_evidence_record())
    calls: list[dict] = []
    preview = {
        "ok": True,
        "status": "CHECKED",
        "artifact_count": 2,
        "claim_count": 1,
        "manifest_sha256": "a" * 64,
        "transition_ready": False,
        "model_work_resumed": False,
        "issues": [],
    }
    committed = {
        **preview,
        "status": "IMPORTED",
        "transition_ready": True,
        "manifest_written": True,
    }

    def run_import(
        root: Path,
        paper_id: str,
        selected_spec: Path,
        *,
        check_only: bool,
        expected_manifest_sha256: str = "",
    ) -> dict:
        calls.append(
            {
                "root": root,
                "paper_id": paper_id,
                "spec": selected_spec,
                "check_only": check_only,
                "expected": expected_manifest_sha256,
            }
        )
        return preview if check_only else committed

    monkeypatch.setattr(ui_module, "LEGACY_PROJECTS", {"writing": {"papers_dir": papers_dir}})
    monkeypatch.setattr(ui_module, "prepare_writing_evidence_import", run_import)
    monkeypatch.setattr(ui_module.filedialog, "askopenfilename", Mock(return_value=str(spec_path)))
    askyesno = Mock(return_value=True)
    monkeypatch.setattr(ui_module.messagebox, "askyesno", askyesno)
    showinfo = Mock()
    monkeypatch.setattr(ui_module.messagebox, "showinfo", showinfo)
    monkeypatch.setattr(ui_module.messagebox, "showerror", Mock())

    UnifiedControlPanel.import_selected_writing_evidence(panel)

    assert [call["check_only"] for call in calls] == [True, False]
    assert calls[1]["expected"] == "a" * 64
    assert calls[0]["spec"] == spec_path
    assert "进入 Step 6" in askyesno.call_args.args[1]
    panel.orchestrator.resume_writing_papers.assert_called_once_with(["paper-1"], single_active=True)
    panel.refresh.assert_called_once_with()
    panel.status_var.set.assert_called_once()
    showinfo.assert_called_once()
    assert "Step 6" in showinfo.call_args.args[1]


def test_ui_blocks_commit_when_check_only_fails(tmp_path: Path, monkeypatch) -> None:
    papers_dir = tmp_path / "papers"
    panel = fake_panel(waiting_evidence_record())
    service = Mock(
        return_value={
            "ok": False,
            "status": "BLOCKED",
            "issues": [{"code": "claim_evidence_row_missing", "path": "spec#claims[0]"}],
        }
    )
    monkeypatch.setattr(ui_module, "LEGACY_PROJECTS", {"writing": {"papers_dir": papers_dir}})
    monkeypatch.setattr(ui_module, "prepare_writing_evidence_import", service)
    monkeypatch.setattr(
        ui_module.filedialog,
        "askopenfilename",
        Mock(return_value=str(papers_dir / "paper-1" / "results" / "bad.json")),
    )
    askyesno = Mock()
    monkeypatch.setattr(ui_module.messagebox, "askyesno", askyesno)
    showerror = Mock()
    monkeypatch.setattr(ui_module.messagebox, "showerror", showerror)

    UnifiedControlPanel.import_selected_writing_evidence(panel)

    service.assert_called_once()
    assert service.call_args.kwargs["check_only"] is True
    askyesno.assert_not_called()
    panel.refresh.assert_not_called()
    assert "claim_evidence_row_missing" in showerror.call_args.args[1]


def test_preview_summary_is_compact_and_does_not_claim_transition() -> None:
    summary = _writing_evidence_import_summary(
        {
            "ok": True,
            "artifact_count": 3,
            "claim_count": 2,
            "manifest_sha256": "b" * 64,
            "transition_ready": False,
        }
    )

    assert "证据文件：3 个" in summary
    assert "定量声明：2 条" in summary
    assert "第 6 步" not in summary


def test_manual_existing_evidence_path_validates_then_continues(tmp_path: Path, monkeypatch) -> None:
    papers_dir = tmp_path / "papers"
    panel = fake_panel(waiting_evidence_record())
    validation = {
        "ok": True,
        "status": "READY",
        "ready": True,
        "transition_ready": True,
        "draft_v2_sha256": "d" * 64,
        "artifact_count": 3,
        "claim_count": 2,
        "issues": [],
    }
    validator = Mock(return_value=validation)
    monkeypatch.setattr(ui_module, "LEGACY_PROJECTS", {"writing": {"papers_dir": papers_dir}})
    monkeypatch.setattr(ui_module, "prepare_manual_writing_evidence", validator)
    monkeypatch.setattr(ui_module.messagebox, "askyesno", Mock(return_value=True))
    monkeypatch.setattr(ui_module.messagebox, "showinfo", Mock())
    monkeypatch.setattr(ui_module.messagebox, "showerror", Mock())

    UnifiedControlPanel.continue_selected_writing_with_existing_evidence(panel)

    assert validator.call_args_list == [
        call(papers_dir, "paper-1", check_only=True),
        call(
            papers_dir,
            "paper-1",
            check_only=False,
            expected_draft_sha256="d" * 64,
        ),
    ]
    panel.orchestrator.resume_writing_papers.assert_called_once_with(["paper-1"], single_active=True)
    panel.refresh.assert_called_once_with()
    assert "3 个" in _writing_existing_evidence_summary(validation)


def test_manual_existing_evidence_path_does_not_continue_when_gate_is_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    papers_dir = tmp_path / "papers"
    panel = fake_panel(waiting_evidence_record())
    monkeypatch.setattr(ui_module, "LEGACY_PROJECTS", {"writing": {"papers_dir": papers_dir}})
    monkeypatch.setattr(
        ui_module,
        "prepare_manual_writing_evidence",
        Mock(
            return_value={
                "ok": False,
                "status": "BLOCKED",
                "ready": False,
                "transition_ready": False,
                "issues": [
                    {
                        "code": "draft_v2_missing",
                        "path": "draft_v2.tex",
                    }
                ],
            }
        ),
    )
    showerror = Mock()
    monkeypatch.setattr(ui_module.messagebox, "showerror", showerror)
    monkeypatch.setattr(ui_module.messagebox, "askyesno", Mock())

    UnifiedControlPanel.continue_selected_writing_with_existing_evidence(panel)

    panel.orchestrator.resume_writing_papers.assert_not_called()
    assert "draft_v2_missing" in showerror.call_args.args[1]
