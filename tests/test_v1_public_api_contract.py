from __future__ import annotations

import inspect
import json
from pathlib import Path

from unified_paper_orchestrator import smart_submission
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator


CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "v1_public_api.json").read_text(encoding="utf-8")
)


def test_unified_orchestrator_public_signatures_are_v1_compatible() -> None:
    for name, expected in CONTRACT["orchestrator_methods"].items():
        assert hasattr(UnifiedOrchestrator, name), f"missing V1 orchestrator method: {name}"
        assert str(inspect.signature(getattr(UnifiedOrchestrator, name))) == expected


def test_ui_adapter_compatibility_shims_remain_available() -> None:
    orchestrator = UnifiedOrchestrator()
    assert list(orchestrator.adapters) == CONTRACT["adapter_keys"]
    journal_author = orchestrator.adapters["journal_author"]
    for name in CONTRACT["journal_author_adapter_methods"]:
        assert callable(getattr(journal_author, name, None)), f"missing journal-author UI shim: {name}"


def test_smart_submission_public_signatures_and_constants_are_v1_compatible() -> None:
    for name, expected in CONTRACT["smart_functions"].items():
        assert hasattr(smart_submission, name), f"missing V1 Smart function: {name}"
        assert str(inspect.signature(getattr(smart_submission, name))) == expected
    for name in CONTRACT["smart_required_constants"]:
        assert hasattr(smart_submission, name), f"missing V1 Smart constant: {name}"


def test_smart_submission_guard_error_contract_is_stable() -> None:
    error_type = smart_submission.SmartSubmissionGuardError
    assert str(inspect.signature(error_type)) == CONTRACT["smart_guard_error_signature"]
    error = error_type("TEST_CODE", "paper-1", "message", details={"reason": "test"})
    result = error.as_dict()
    assert list(result) == CONTRACT["smart_guard_error_result_keys"]
    assert result == {
        "ok": False,
        "code": "TEST_CODE",
        "paper_id": "paper-1",
        "message": "message",
        "details": {"reason": "test"},
    }
