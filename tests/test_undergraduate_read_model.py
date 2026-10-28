from unified_paper_orchestrator.ui_next.read_model import paper_rows, selected_paper


def test_undergraduate_completion_and_progress_have_two_stages_not_four():
    state = {"papers": {"ug": {"paper_id": "ug", "title": "本科论文", "current_stage": "done", "next_action": "none",
        "stages": {"writing": {"status": "complete", "prompt_scheme": "undergraduate_software"},
                   "figures": {"status": "complete", "artifact": "fixture-submit", "word_delivery_ok": True},
                   "journal_author": {"status": "ready", "admission": {"admitted": True}},
                   "submission": {"status": "missing"}}}}}
    row = paper_rows(state)[0]
    assert row["isComplete"] and row["progress"] == 1
    assert not row["visible_journal_author"] and not row["visible_submission"]
    selected = selected_paper(state, "ug")
    assert selected["status"] == "complete" and selected["progress"] == 1
    assert selected["artifact"] == "fixture-submit"


def test_journal_progress_keeps_four_stages():
    state = {"papers": {"j": {"current_stage": "journal_author", "stages": {
        "writing": {"status": "complete"}, "figures": {"status": "complete"},
        "journal_author": {"status": "missing"}, "submission": {"status": "missing"}}}}}
    row = paper_rows(state)[0]
    assert row["progress"] == 0.5 and not row["isComplete"]
