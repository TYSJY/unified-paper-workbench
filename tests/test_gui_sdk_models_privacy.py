from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator.ui.account_privacy import MASK, identity_tokens, mask_personal
from unified_paper_orchestrator.ui_next.sdk_models import discover_models, validate_profile


def model_orchestrator():
    return SimpleNamespace(
        stage_available_models=lambda stage: {
            "ok": True, "configured_model": "model-a", "account": {"email": "private@example.test"},
            "models": [
                {"model": "model-a", "id": "model-a", "supported_reasoning_efforts": ["low", "high"]},
                {"model": "model-b", "id": "model-b", "supported_reasoning_efforts": ["minimal"]},
                {"model": "hidden-model", "hidden": True},
            ]},
        stage_prompt_inventory=lambda stage: {"writing_workflow": {
            "default_execution_profile": {"model": "model-a", "reasoning_effort": "high"}}},
    )


def test_catalog_comes_from_sdk_and_drops_account_metadata():
    result = discover_models(model_orchestrator(), "writing")
    assert result["state"] == "ready"
    assert len(result["models"]) == 2
    assert "private@example.test" not in json.dumps(result)
    with pytest.raises(ValueError):
        discover_models(model_orchestrator(), "unknown")


def test_save_rejects_unknown_model_and_unsupported_or_inherited_effort():
    source = model_orchestrator()
    validate_profile(source, "writing", "model-a", "low")
    validate_profile(source, "writing", "", "", inherit=True)
    with pytest.raises(ValueError):
        validate_profile(source, "writing", "typo-model", "")
    with pytest.raises(ValueError):
        validate_profile(source, "writing", "model-a", "minimal")
    with pytest.raises(ValueError):
        validate_profile(source, "writing", "model-b", "", inherit=True)


def test_discovery_failure_does_not_supply_fabricated_models():
    source = SimpleNamespace(stage_available_models=lambda stage: {"ok": False, "models": []})
    with pytest.raises(RuntimeError):
        discover_models(source, "writing")


def test_invalid_save_does_not_write_scheduler_or_profile():
    from unified_paper_orchestrator.ui_next.controller import WorkbenchController
    source = model_orchestrator()
    source.save_stage_scheduler_settings = lambda *args: pytest.fail("must not write")
    fake = SimpleNamespace(orchestrator=source, commercial=SimpleNamespace(
        require_capacity=lambda value: pytest.fail("validation must happen first")))
    with pytest.raises(ValueError):
        WorkbenchController._save_stage_settings(fake, "writing", 4, "typo-model", "")


def test_identity_mask_covers_title_email_orcid_technical_and_url_without_mutation():
    raw = [{"username": "demo-login", "email": "demo@example.test", "account_id": "record-27",
            "notes": "demo-login", "username_candidates": ["candidate-login"]}]
    before = copy.deepcopy(raw)
    tokens = identity_tokens(raw)
    text = "demo-login demo@example.test 0000-0002-1825-0097 record-27 candidate-login"
    assert mask_personal(text, tokens).replace(MASK, "").strip() == ""
    assert "unexpected@example.test" not in mask_personal("unexpected@example.test", tokens)
    assert "secret-value" not in mask_personal("https://example.test/login?hint=secret-value")
    assert "user:secret" not in mask_personal("https://user:secret@example.test/login")
    assert raw == before


@pytest.fixture(scope="module")
def tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


def test_record_editor_masks_and_preserves_real_values(tk_root):
    from unified_paper_orchestrator.ui.account_site_manager import _RecordDialog
    original = {"username": "demo-login", "notes": "demo@example.test\nkeep this", "scope": "email"}
    fields = [{"key": "username", "label": "Login"},
              {"key": "notes", "label": "Notes", "kind": "multiline"},
              {"key": "scope", "label": "Scope", "kind": "combo", "values": ["email"]}]
    dialog = _RecordDialog(tk_root, title="Privacy fixture", record=original, fields=fields)
    assert dialog.hide_sensitive_var.get()
    assert all(widget.cget("show") == "*" for widget in dialog._privacy_entries)
    assert dialog._texts["notes"].get("1.0", "end-1c") == MASK
    dialog.hide_sensitive_var.set(False)
    dialog._toggle_privacy()
    assert dialog._texts["notes"].get("1.0", "end-1c") == original["notes"]
    dialog._texts["notes"].insert("end", " changed")
    dialog.hide_sensitive_var.set(True)
    dialog._toggle_privacy()
    dialog._accept()
    assert dialog.result["username"] == original["username"]
    assert dialog.result["notes"] == original["notes"] + " changed"


def test_account_window_hides_on_every_open_and_technical_view_cannot_bypass(tk_root):
    from unified_paper_orchestrator.ui.account_site_manager import AccountSiteManager
    rows = [{"record_key": "smart:record-27", "account_id": "record-27", "username": "demo-login",
             "provider": "Demo Mail", "scope": "email", "email": "demo@example.test", "source_kind": "smart"}]
    original = copy.deepcopy(rows)
    store = SimpleNamespace(vault_read_only=False, list_account_summaries=lambda: rows,
                            list_systems=lambda: [], list_journals=lambda: [],
                            is_account_read_only=lambda record_id: False)
    for _ in range(2):
        window = AccountSiteManager(tk_root, store=store)
        window.refresh_all()
        assert window.hide_sensitive_var.get()
        item = window.master_tree.get_children()[0]
        assert "demo-login" not in str(window.master_tree.item(item))
        assert "demo-login" not in window.detail_subtitle_var.get()
        window.show_technical_var.set(True)
        window._render_active_group()
        assert "record-27" not in str([window.detail_tree.item(i) for i in window.detail_tree.get_children()])
        window.hide_sensitive_var.set(False)
        window._toggle_privacy()
        assert "demo-login" in window.detail_subtitle_var.get()
        window.destroy()
    assert rows == original


def test_sdk_choice_preserves_unavailable_values_and_updates_options():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    path = Path(__file__).resolve().parents[1] / "unified_paper_orchestrator/ui_next/qml/components/SdkChoice.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    widget = component.create()
    assert widget is not None, [error.toString() for error in component.errors()]
    widget.setProperty("selectedValue", "legacy-model")
    widget.setProperty("choices", [{"value": "sdk-model", "label": "SDK model"}])
    app.processEvents()
    assert widget.property("selectedValue") == "legacy-model"
    assert not widget.property("selectionAvailable")
    widget.setProperty("selectedValue", "sdk-model")
    assert widget.property("selectionAvailable")
    widget.setProperty("choices", [])
    assert widget.property("selectedValue") == "sdk-model"
    assert not widget.property("selectionAvailable")
    widget.deleteLater()
