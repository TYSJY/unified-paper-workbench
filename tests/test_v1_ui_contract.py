from __future__ import annotations

import json
from pathlib import Path

from unified_paper_orchestrator.ui.main_window import STAGE_TABS


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tests" / "contracts" / "v1_ui_contract.json").read_text(encoding="utf-8"))
SOURCE = (ROOT / "unified_paper_orchestrator" / "ui" / "main_window.py").read_text(encoding="utf-8")


def test_current_window_title_and_stage_tabs_match_the_versioned_contract() -> None:
    assert f'self.title("{CONTRACT["window_title"]}")' in SOURCE
    assert [list(item) for item in STAGE_TABS] == CONTRACT["stage_tabs"]


def test_current_visible_controls_keep_their_order_and_callbacks() -> None:
    for group, fragments in CONTRACT["ordered_fragment_groups"].items():
        cursor = 0
        for fragment in fragments:
            # Some labels (for example "暂停" and "更多操作") intentionally
            # appear in more than one panel.  Search forward within the declared
            # sequence so the contract binds the occurrence belonging to this
            # control group instead of the first occurrence in the whole file.
            position = SOURCE.find(fragment, cursor)
            assert position >= 0, f"missing UI contract fragment in {group}: {fragment}"
            cursor = position + len(fragment)


def test_current_columns_safety_notice_and_hidden_technical_default_match_contract() -> None:
    for fragment in CONTRACT["required_fragments"]:
        assert fragment in SOURCE, f"missing UI contract fragment: {fragment}"
