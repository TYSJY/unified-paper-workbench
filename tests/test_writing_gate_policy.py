from __future__ import annotations

import json
from pathlib import Path

import pytest

from unified_paper_orchestrator.writing_gate_policy import (
    AUTO_TRUST_MODE,
    STRICT_CONFIRMATION_MODE,
    WritingGatePolicyError,
    WritingGatePolicyStore,
)


def _store(tmp_path: Path) -> WritingGatePolicyStore:
    return WritingGatePolicyStore(tmp_path / "state" / "writing_gate_policy.json", password_iterations=1_000)


def test_missing_policy_defaults_to_strict_without_creating_state(tmp_path: Path) -> None:
    store = _store(tmp_path)

    public = store.load_public()

    assert public["mode"] == STRICT_CONFIRMATION_MODE
    assert public["auto_trust_enabled"] is False
    assert public["password_configured"] is False
    assert public["revision"] == 0
    assert not store.path.exists()


def test_first_switch_sets_hashed_password_and_persists_auto_mode(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(WritingGatePolicyError) as mismatch:
        store.set_step5_mode(AUTO_TRUST_MODE, password="test-pass", password_confirmation="different")
    assert mismatch.value.code == "WRITING_GATE_PASSWORD_CONFIRMATION_MISMATCH"
    assert not store.path.exists()

    changed = store.set_step5_mode(
        AUTO_TRUST_MODE,
        password="test-pass",
        password_confirmation="test-pass",
    )

    assert changed["mode"] == AUTO_TRUST_MODE
    assert changed["password_configured"] is True
    assert changed["revision"] == 1
    raw = store.path.read_text(encoding="utf-8")
    assert "test-pass" not in raw
    saved = json.loads(raw)
    assert saved["admin_password"]["algorithm"] == "pbkdf2-sha256"
    assert saved["history"][-1]["to"] == AUTO_TRUST_MODE
    assert store.auto_trust_snapshot()["revision"] == 1


def test_existing_password_is_required_to_restore_strict_mode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_step5_mode(AUTO_TRUST_MODE, password="test-pass", password_confirmation="test-pass")

    with pytest.raises(WritingGatePolicyError) as wrong:
        store.set_step5_mode(STRICT_CONFIRMATION_MODE, password="wrong-pass")
    assert wrong.value.code == "WRITING_GATE_PASSWORD_INCORRECT"
    assert store.load_public()["mode"] == AUTO_TRUST_MODE

    restored = store.set_step5_mode(STRICT_CONFIRMATION_MODE, password="test-pass")

    assert restored["mode"] == STRICT_CONFIRMATION_MODE
    assert restored["revision"] == 2
    assert store.auto_trust_snapshot() is None


def test_malformed_existing_policy_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text('{"schema":"wrong"}\n', encoding="utf-8")

    with pytest.raises(WritingGatePolicyError) as unreadable:
        store.load_public()

    assert unreadable.value.code == "WRITING_GATE_POLICY_SCHEMA_INVALID"
