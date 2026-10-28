from __future__ import annotations

import unittest

from unified_paper_orchestrator.config import STAGE_ORDER
from unified_paper_orchestrator.models import action_for


class IsolatedSmokeTest(unittest.TestCase):
    def test_stage_order_is_complete(self) -> None:
        self.assertEqual(STAGE_ORDER, ("writing", "figures", "journal_author", "submission"))

    def test_blocked_stage_is_not_requeued(self) -> None:
        action, _ = action_for("writing", {"writing": {"status": "blocked", "detail": "manual fact required"}})
        self.assertEqual(action, "blocked")


if __name__ == "__main__":
    unittest.main()
