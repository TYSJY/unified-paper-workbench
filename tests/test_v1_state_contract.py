from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import unified_paper_orchestrator.orchestrator as orchestrator_module
from unified_paper_orchestrator.config import SMART_SUBMISSION_STATE_PATH, UNIFIED_STATE_PATH
from unified_paper_orchestrator.orchestrator import UnifiedOrchestrator
from unified_paper_orchestrator.smart_submission import smart_submission_status


CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "v1_state_contract.json").read_text(encoding="utf-8")
)
TYPE_MAP = {
    "int": int,
    "str": str,
    "dict": dict,
    "list": list,
    "bool": bool,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    assert isinstance(value, dict)
    return value


def _assert_types(value: dict[str, Any], expected: dict[str, str], *, label: str) -> None:
    for key, type_name in expected.items():
        assert key in value, f"{label} missing required key: {key}"
        expected_type = TYPE_MAP[type_name]
        if expected_type is int:
            assert not isinstance(value[key], bool), f"{label}.{key} expected int, got bool"
        assert isinstance(value[key], expected_type), (
            f"{label}.{key} expected {type_name}, got {type(value[key]).__name__}"
        )


def test_current_unified_projection_satisfies_v1_ui_contract() -> None:
    if not UNIFIED_STATE_PATH.is_file():
        pytest.skip("local V1 unified state is not present")
    state = _read_json(UNIFIED_STATE_PATH)
    contract = CONTRACT["unified"]
    _assert_types(state, contract["top_types"], label="unified")
    assert state["stage_order"] == contract["stage_names"]
    assert set(state["paper_queue"]).issubset(state["papers"])
    for paper_id, paper in state["papers"].items():
        _assert_types(paper, contract["paper_types"], label=f"paper[{paper_id}]")
        assert paper["paper_id"] == paper_id
        assert set(contract["stage_names"]).issubset(paper["stages"])
        for stage_name in contract["stage_names"]:
            stage = paper["stages"][stage_name]
            for key in contract["stage_required_keys"]:
                assert key in stage, f"paper[{paper_id}].stages[{stage_name}] missing {key}"
        route = paper["submission_route"]
        _assert_types(route, contract["route_types"], label=f"paper[{paper_id}].submission_route")
        assert route["execution_owner"] in contract["execution_owners"]
        assert route["mode"] in contract["route_modes"]
        assert state["submission_routes"].get(paper_id) == route


def test_current_smart_state_satisfies_v1_ui_contract() -> None:
    if not SMART_SUBMISSION_STATE_PATH.is_file():
        pytest.skip("local V1 Smart state is not present")
    state = _read_json(SMART_SUBMISSION_STATE_PATH)
    contract = CONTRACT["smart"]
    _assert_types(state, contract["top_types"], label="smart")
    for paper_id, paper in state["papers"].items():
        _assert_types(paper, contract["paper_required_types"], label=f"smart.paper[{paper_id}]")
        for key, type_name in contract["paper_optional_types"].items():
            if key in paper:
                assert isinstance(paper[key], TYPE_MAP[type_name]), (
                    f"smart.paper[{paper_id}].{key} expected {type_name}"
                )


def test_smart_status_keeps_keys_consumed_by_v1_ui_and_cli() -> None:
    if not SMART_SUBMISSION_STATE_PATH.is_file():
        pytest.skip("local V1 Smart state is not present")
    status = smart_submission_status()
    for key in CONTRACT["smart"]["status_required_keys"]:
        assert key in status, f"smart_submission_status missing V1 key: {key}"
    _assert_types(status, CONTRACT["smart"]["status_required_types"], label="smart_status")


def test_v1_load_unified_state_upgrades_a_minimal_legacy_document_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "unified_state.json"
    original = '{"version": 1, "paper_queue": ["legacy-paper"]}\n'
    state_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(orchestrator_module, "UNIFIED_STATE_PATH", state_path)

    state = UnifiedOrchestrator().load_unified_state()

    assert state["version"] == 1
    assert state["paper_queue"] == ["legacy-paper"]
    assert state["settings"] == {"scheduling_mode": "manual", "one_action_per_run": True}
    assert state["papers"] == {}
    assert state["submission_routes"] == {}
    assert state["submission_route_operations"] == {}
    assert state["history"] == []
    assert state_path.read_text(encoding="utf-8") == original


def test_v1_load_unified_state_rejects_a_non_object_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "unified_state.json"
    state_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator_module, "UNIFIED_STATE_PATH", state_path)

    with pytest.raises(ValueError, match="Unified state must be a JSON object"):
        UnifiedOrchestrator().load_unified_state()
