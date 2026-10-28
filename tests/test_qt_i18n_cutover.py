from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator.ui_next.controller import WorkbenchController
from unified_paper_orchestrator.ui_next.i18n import I18nManager
from unified_paper_orchestrator.ui_next.read_model import paper_rows


ROOT = Path(__file__).resolve().parents[1]
LOCALE_DIR = ROOT / "unified_paper_orchestrator" / "ui_next" / "locales"
QML_DIR = ROOT / "unified_paper_orchestrator" / "ui_next" / "qml"


def _leaf_keys(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    result: set[str] = set()
    for key, child in value.items():
        if key == "_meta":
            continue
        nested = f"{prefix}.{key}" if prefix else str(key)
        result.update(_leaf_keys(child, nested))
    return result


def test_locale_catalogs_have_identical_complete_key_sets() -> None:
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in LOCALE_DIR.glob("*.json")
    }

    assert set(catalogs) >= {"zh_CN", "en_US"}
    key_sets = {_name: _leaf_keys(payload) for _name, payload in catalogs.items()}
    assert all(keys == key_sets["zh_CN"] for keys in key_sets.values())


def test_language_switch_persists_and_localizes_read_model(tmp_path: Path) -> None:
    settings = tmp_path / "preferences.ini"
    manager = I18nManager(settings_path=settings)
    manager.setLocale("en_US")
    state = {
        "papers": {
            "paper-1": {
                "title": "Test",
                "current_stage": "figures",
                "next_action": "queue_submission",
                "stages": {
                    "writing": {"status": "complete", "step_done": 1, "workflow_step_count": 1},
                    "figures": {"status": "running", "current_step": 2, "workflow_step_count": 3},
                    "journal_author": {"status": "missing"},
                    "submission": {"status": "missing"},
                },
            }
        }
    }

    row = paper_rows(state, manager.translate)[0]
    assert row["currentStage"] == "Figures"
    assert row["statusLabel"] == "Running"
    assert row["nextActionLabel"] == "Move to Smart Submission"
    assert I18nManager(settings_path=settings).locale == "en_US"


def test_modern_qml_has_no_classic_or_stable_submission_entry() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in QML_DIR.rglob("*.qml")
    )

    assert "openClassic" not in source
    assert "经典控制台" not in source
    assert "稳定投稿" not in source
    assert "LanguageSelector" in (QML_DIR / "Main.qml").read_text(encoding="utf-8")


def test_translated_buttons_are_content_sized_and_narrow_header_is_locale_aware() -> None:
    main_source = (QML_DIR / "Main.qml").read_text(encoding="utf-8")
    button_source = (QML_DIR / "components" / "ModernButton.qml").read_text(encoding="utf-8")
    language_source = (QML_DIR / "components" / "LanguageSelector.qml").read_text(encoding="utf-8")

    assert "property int minimumButtonWidth" in button_source
    assert "naturalContentWidth + leftPadding + rightPadding" in button_source
    assert "elide: Text.ElideRight" in button_source
    assert "ToolTip.visible:" in button_source
    assert 'property bool verboseLocale: i18n.locale !== "zh_CN"' in main_source
    assert "property bool narrowHeader: width < (verboseLocale ? 1640 : 1220)" in main_source
    assert 'toolTipText: window.tr("action.accounts_sites")' in main_source
    assert 'toolTipText: window.tr("action.emergency_stop")' in main_source
    assert 'toolTipText: window.tr("workspace.switch_short")' in main_source
    assert 'window.tr("submission_monitor.last_check")' in main_source
    assert "implicitWidth: window.narrowHeader" not in main_source
    assert "languageLabelMeasure.implicitWidth + 48" in language_source


def test_settings_use_modern_fields_and_the_real_strict_gate_token() -> None:
    source = (QML_DIR / "components" / "SettingsCenter.qml").read_text(encoding="utf-8")
    module = (QML_DIR / "components" / "qmldir").read_text(encoding="utf-8")

    assert "ModernTextField" in source
    assert "ModernComboBox" in source
    assert "SpinBox {" not in source
    assert '"strict_confirmation"' in source
    assert "ModernTextField 1.0 ModernTextField.qml" in module
    assert "ModernComboBox 1.0 ModernComboBox.qml" in module


def test_account_center_inherits_current_workspace_locale_and_hides_console(monkeypatch) -> None:
    import unified_paper_orchestrator.ui_next.controller as controller_module

    launches: list[dict[str, object]] = []
    monkeypatch.setattr(
        controller_module.subprocess,
        "Popen",
        lambda command, **options: launches.append(
            {"command": command, **options}
        ),
    )
    fake = SimpleNamespace(
        i18n=SimpleNamespace(locale="en_US"),
        toastRequested=SimpleNamespace(emit=lambda *_args: None),
    )

    WorkbenchController.openAccountCenter(fake)

    assert len(launches) == 1
    launch = launches[0]
    assert launch["env"]["UNIFIED_PAPER_UI_LOCALE"] == "en_US"
    assert launch["env"]["UNIFIED_PAPER_WORKSPACE"] == str(controller_module.WORKSPACE_ROOT)
    assert launch["cwd"] == str(Path(controller_module.__file__).resolve().parents[2])
    assert launch["command"][-3:] == [
        "--workspace",
        str(controller_module.WORKSPACE_ROOT),
        "account-site-manager",
    ]
    assert launch["creationflags"] == int(
        getattr(controller_module.subprocess, "CREATE_NO_WINDOW", 0)
    )


def test_source_workspace_restart_uses_repo_cwd_and_explicit_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import unified_paper_orchestrator.ui_next.controller as controller_module

    launches: list[dict[str, object]] = []
    monkeypatch.setattr(
        controller_module,
        "set_active_workspace",
        lambda _path: SimpleNamespace(root=tmp_path),
    )
    monkeypatch.setattr(
        controller_module.subprocess,
        "Popen",
        lambda command, **options: launches.append(
            {"command": command, **options}
        ),
    )
    quit_events: list[bool] = []
    fake = SimpleNamespace(
        i18n=SimpleNamespace(locale="en_US"),
        errorRequested=SimpleNamespace(emit=lambda *_args: pytest.fail("restart must succeed")),
        quitRequested=SimpleNamespace(emit=lambda: quit_events.append(True)),
        _t=lambda _key, default="", **_values: default,
    )

    WorkbenchController._switch_workspace_now(fake, str(tmp_path))

    assert quit_events == [True]
    assert len(launches) == 1
    launch = launches[0]
    assert launch["cwd"] == str(Path(controller_module.__file__).resolve().parents[2])
    assert launch["command"][-3:] == [
        "--workspace",
        str(tmp_path),
        "ui",
    ]
    assert launch["env"]["UNIFIED_PAPER_WORKSPACE"] == str(tmp_path)
    assert launch["env"]["UNIFIED_PAPER_UI_LOCALE"] == "en_US"


def test_full_pipeline_requests_step5_trust_before_starting() -> None:
    events: list[tuple[int, bool]] = []
    fake = SimpleNamespace(
        orchestrator=SimpleNamespace(
            writing_gate_policy=lambda: {
                "mode": "strict_confirmation",
                "password_configured": True,
            }
        ),
        fullPipelineTrustRequested=SimpleNamespace(
            emit=lambda count, configured: events.append((count, configured))
        ),
        _submit=lambda *_args, **_kwargs: pytest.fail("pipeline must not start before trust is enabled"),
    )

    WorkbenchController.executeAction(fake, "full_pipeline", 2)

    assert events == [(2, True)]


def test_full_pipeline_enables_trust_then_starts_exact_count() -> None:
    calls: list[tuple[str, object]] = []
    fake = SimpleNamespace(
        commercial=SimpleNamespace(
            require_features=lambda features: calls.append(
                ("entitlements", tuple(features))
            ),
            require_capacity=lambda count: calls.append(("capacity", count)),
        ),
        orchestrator=SimpleNamespace(
            set_writing_gate_policy=lambda mode, **values: calls.append(
                ("gate", (mode, values))
            )
            or {"mode": mode}
        ),
        full_pipeline_runtime=SimpleNamespace(
            start_new=lambda count: calls.append(("start", count)) or {"ok": True}
        ),
    )

    result = WorkbenchController._enable_trust_and_start_pipeline(
        fake,
        3,
        "secret",
        "secret",
    )

    assert result["pipeline"]["ok"] is True
    assert calls == [
        (
            "entitlements",
            ("writing", "figures", "journal_author", "smart_submission"),
        ),
        ("capacity", 3),
        (
            "gate",
            (
                "auto_trust",
                {"password": "secret", "password_confirmation": "secret"},
            ),
        ),
        ("start", 3),
    ]


def test_commercial_settings_are_present_and_default_compatibility_is_explicit() -> None:
    source = (QML_DIR / "components" / "SettingsCenter.qml").read_text(
        encoding="utf-8"
    )

    assert 'root.tr("settings.commercial_tab")' in source
    assert "configureCommercial(" in source
    assert "requestCommercialLoginCode(" in source
    assert "completeCommercialLogin(" in source
    assert "refreshCommercialLicense()" in source
    assert "checkCommercialUpdate()" in source
    assert "flushCommercialDiagnostics()" in source


def test_codex_sdk_account_is_available_in_the_current_qml_settings_center() -> None:
    source = (QML_DIR / "components" / "SettingsCenter.qml").read_text(
        encoding="utf-8"
    )

    assert 'root.tr("settings.codex_tab")' in source
    assert "controllerRef.codexAccountStatus" in source
    assert "connectCodexAccount()" in source
    assert "refreshCodexAccount()" in source
    assert "disconnectCodexAccount()" in source
    assert 'root.tr("settings.codex_device_note")' in source


def test_settings_tab_labels_follow_the_stack_page_order() -> None:
    source = (QML_DIR / "components" / "SettingsCenter.qml").read_text(
        encoding="utf-8"
    )
    tab_order = [
        source.index(f'root.tr("settings.{key}_tab")')
        for key in ("stage", "smart", "codex", "gate", "monitor", "author", "commercial")
    ]
    page_order = [
        source.index(f'root.tr("settings.{key}_title")')
        for key in ("smart", "codex", "gate", "monitor", "author", "commercial")
    ]

    assert tab_order == sorted(tab_order)
    assert page_order == sorted(page_order)


def test_codex_account_qml_projection_drops_unknown_secret_fields() -> None:
    emitted: list[bool] = []
    fake = SimpleNamespace(
        _codex_account_status={},
        codexAccountChanged=SimpleNamespace(emit=lambda: emitted.append(True)),
    )

    WorkbenchController._publish_codex_account_status(
        fake,
        {
            "ok": True,
            "authenticated": True,
            "auth_mode": "chatgpt",
            "plan_type": "plus",
            "isolated_home": True,
            "access_token": "must-not-reach-qml",
            "email": "must-not-reach-qml@example.test",
        },
        login_pending=False,
    )

    assert emitted == [True]
    assert fake._codex_account_status["authenticated"] is True
    assert fake._codex_account_status["auth_mode"] == "chatgpt"
    assert fake._codex_account_status["plan_type"] == "plus"
    assert "access_token" not in fake._codex_account_status
    assert "email" not in fake._codex_account_status


def test_codex_account_controller_helpers_keep_login_challenge_ephemeral() -> None:
    service = SimpleNamespace(
        begin_browser_login=lambda: SimpleNamespace(
            attempt_id="attempt-1",
            method="browser",
            verification_url="https://auth.example.test/authorize",
        ),
        wait_for_login=lambda attempt_id: SimpleNamespace(
            success=attempt_id == "attempt-1",
            error_code="",
            status={
                "ok": True,
                "authenticated": True,
                "auth_mode": "chatgpt",
                "plan_type": "plus",
                "isolated_home": True,
            },
        ),
    )
    fake = SimpleNamespace(codex_account=service)

    challenge = WorkbenchController._begin_codex_browser_login(fake)
    result = WorkbenchController._wait_for_codex_browser_login(
        fake,
        challenge["attemptId"],
    )

    assert challenge == {
        "attemptId": "attempt-1",
        "method": "browser",
        "verificationUrl": "https://auth.example.test/authorize",
    }
    assert result["success"] is True
    assert result["status"]["authenticated"] is True


def test_submission_stage_prepares_and_starts_only_smart_sdk(monkeypatch) -> None:
    import unified_paper_orchestrator.smart_submission as smart_module

    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        smart_module,
        "prepare_smart_submission_sdk_papers",
        lambda paper_ids, orchestrator=None: calls.append(("prepare", list(paper_ids)))
        or {"ok": True, "queued": ["paper-1"], "resumed": []},
    )
    monkeypatch.setattr(
        smart_module,
        "start_smart_submission_program_runtime",
        lambda paper_ids: calls.append(("start", list(paper_ids))) or {"ok": True},
    )
    fake = SimpleNamespace(
        orchestrator=object(),
        _submission_candidate_ids=lambda: ["paper-1"],
        _t=lambda _key, default="", **_values: default,
    )

    result = WorkbenchController._start_smart_submission_stage(fake)

    assert result["ok"] is True
    assert calls == [("prepare", ["paper-1"]), ("start", ["paper-1"])]


def test_submission_stage_does_not_start_when_preflight_has_no_runnable_paper(monkeypatch) -> None:
    import unified_paper_orchestrator.smart_submission as smart_module

    monkeypatch.setattr(
        smart_module,
        "prepare_smart_submission_sdk_papers",
        lambda *_args, **_kwargs: {
            "ok": False,
            "code": "SMART_SDK_NO_ONLINE_READY_PAPERS",
            "queued": [],
            "resumed": [],
            "skipped": {"paper-1": "missing author metadata"},
        },
    )
    monkeypatch.setattr(
        smart_module,
        "start_smart_submission_program_runtime",
        lambda _ids: pytest.fail("runtime must not start after failed preparation"),
    )
    fake = SimpleNamespace(
        orchestrator=object(),
        _submission_candidate_ids=lambda: ["paper-1"],
        _t=lambda _key, default="", **_values: default,
    )

    with pytest.raises(RuntimeError, match="SMART_SDK_NO_ONLINE_READY_PAPERS"):
        WorkbenchController._start_smart_submission_stage(fake)
