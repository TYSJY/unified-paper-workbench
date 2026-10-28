"""Render real Qt UI with fictional data, no accounts and no model calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if not output.is_relative_to(repo / "build") or output.exists():
        raise ValueError("Use a new output directory under build/.")
    output.mkdir(parents=True)
    profile = output / "demo-profile"
    os.environ.update({
        "UNIFIED_PAPER_ORCHESTRATOR_ROOT": str(output),
        "UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT": str(output),
        "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY": "1", "UNIFIED_PAPER_WORKSPACE": "",
        "USERPROFILE": str(profile), "HOME": str(profile),
        "APPDATA": str(profile / "Roaming"), "LOCALAPPDATA": str(profile / "Local"),
        "CODEX_HOME": str(output / "unused-codex"),
        "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
        "QT_QUICK_CONTROLS_STYLE": "Basic",
        **{env: str(output / stage) for env, stage in [
            ("UNIFIED_WRITING_ROOT", "writing"), ("UNIFIED_FIGURE_ROOT", "figures"),
            ("UNIFIED_JOURNAL_AUTHOR_ROOT", "journal_author"), ("UNIFIED_SUBMISSION_ROOT", "submission")]},
    })
    from PySide6.QtCore import QObject, QMetaObject, QUrl
    from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    from unified_paper_orchestrator.ui_next.controller import WorkbenchController

    app = QGuiApplication([])
    for font in ("msyh.ttc", "segoeui.ttf", "segmdl2.ttf"):
        path = Path("C:/Windows/Fonts") / font
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont("Microsoft YaHei"))
    with patch.object(WorkbenchController, "_bootstrap_commercial", lambda _: None):
        controller = WorkbenchController()
    controller._projection_timer.stop()
    controller._sync_timer.stop()
    # Settings may request account/catalog refresh when opened. Demo rendering
    # must never schedule those workers or contact an external service.
    blocked_actions = []
    controller._submit = lambda action, *_args, **_kwargs: blocked_actions.append(action)
    controller.i18n.setLocale("zh_CN")
    store = controller.orchestrator.writing_workflow_store
    store.select_prompt_scheme("undergraduate_software")
    custom = store.add_custom_step("交付前检查与答辩材料整理")
    custom_id = custom["steps"][-1]["step_id"]
    demo_prompt = """# 交付前检查与答辩材料整理

继续处理当前论文，不另建论文。依据已导入的学校格式要求和当前正式稿：

1. 检查标题层级、目录、图表编号、交叉引用、参考文献和附录。
2. 核对图表与正文结论是否一致；保留未核实数据的待验证标记。
3. 为每个关键结论列出对应的数据、测试或文献来源，不编造实验。
4. 整理答辩演示提纲：研究问题、需求分析、系统设计、实现、测试与局限。
5. 产出可编辑 Word 正式稿、PDF 预览和交付检查清单。

输出前逐项检查；缺少材料时列出待补项目，等待用户补充。
"""
    store.save_custom_prompt(custom_id, demo_prompt)
    snapshot = store.active_snapshot()
    demo_model = "gpt-6-astra"
    controller._settings_data = {
        "stages": [{"key": "writing", "label": "写论文", "parallel": 4, "parallelMax": 10,
            "defaultModel": demo_model, "defaultReasoning": "high", "stepCount": snapshot["step_count"],
            "workflowMode": "custom", "promptScheme": "undergraduate_software",
            "promptSchemes": [{"id": "journal", "name": "原期刊版"},
                {"id": "undergraduate_humanities", "name": "本科人文社科版"},
                {"id": "undergraduate_software", "name": "本科计算机软件版"}],
            "steps": [{"position": i + 1, "stepId": step["step_id"], "kind": step["kind"],
                "name": step["name"], "model": "", "reasoning": "", "effectiveModel": demo_model,
                "tokenLimit": (i + 1) * 100000, "promptPath": step.get("prompt_path", "")}
                for i, step in enumerate(snapshot["steps"])]}],
        "writingGate": {"mode": "strict", "passwordConfigured": False},
        "smartMonitor": {"time": "09:00", "enabled": True, "lastStatus": "演示：等待下一轮检查", "lastFinishedAt": "2026-10-28 09:00"},
        "smartPolicy": {"accountMissingPolicy": "ask", "preferOrcid": True,
            "autoAuthorizeOrcid": False, "requireTermsConfirmation": True, "autoFinalSubmit": False},
        "authorTemplates": [], "commercial": {"account": {"devices": []}},
    }
    demo_catalog = {"state": "ready", "configuredModel": demo_model, "models": [
        {"model": demo_model, "display_name": "GPT-6 Astra · 演示目录", "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"]},
        {"model": "gpt-6-sol", "display_name": "GPT-6 Sol · 演示目录", "supported_reasoning_efforts": ["low", "medium", "high"]}],
        "runtimeVersion": "0.153.4", "sdkVersion": "0.1.0b3"}
    controller._model_catalogs = {"writing": demo_catalog}
    controller._publish_codex_account_status({"ok": True, "authenticated": False}, login_pending=False)
    stages = ("writing", "figures", "journal_author", "submission")
    scenarios = [
        ("demo-001", "示例：软件系统的设计与实现", "figures", ("complete", "complete", "missing", "missing")),
        ("demo-002", "示例：公共服务满意度研究", "writing", ("waiting_data", "missing", "missing", "missing")),
        ("demo-003", "示例：本地推理性能评估", "journal_author", ("complete", "complete", "ready", "missing")),
    ]
    papers = {}
    for pid, title, current, statuses in scenarios:
        papers[pid] = {"paper_id": pid, "title": title, "current_stage": current,
            "updated_at": "2026-10-28T12:00:00+08:00", "stages": {
                stage: {"status": status, "detail": "虚构演示任务 · 展示界面，不执行研究或模型调用。",
                        "workflow_step_count": 3, "step_done": 3 if status == "complete" else 1}
                for stage, status in zip(stages, statuses)}}
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(error.toString() for error in values))
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("i18n", controller.i18n)
    engine.load(QUrl.fromLocalFile(str(repo / "unified_paper_orchestrator/ui_next/qml/Main.qml")))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    images = []
    def capture(name, *, popup=False):
        QTest.qWait(140)
        captured = window.grabWindow()
        if popup:
            # Keep the real popup at its native pixel size for legible README images.
            captured = captured.copy((captured.width() - 1000) // 2, (captured.height() - 780) // 2, 1000, 780)
        assert captured.save(str(output / name))
        images.append(name)
    try:
        controller._apply_state({"papers": papers}, duration_ms=0)
        controller.selectPaper("demo-001")
        capture("workbench-overview.png")
        controller.selectPage("figures")
        capture("workbench-pdf.png")
        settings = window.findChild(QObject, "settingsCenter")
        tabs = window.findChild(QObject, "settingsTabs")
        assert settings is not None and tabs is not None
        QMetaObject.invokeMethod(settings, "open")
        controller._model_catalogs = {"writing": demo_catalog}
        controller.modelCatalogsChanged.emit()
        capture("workbench-workflow-settings.png", popup=True)
        for child in settings.findChildren(QObject):
            if child.metaObject().indexOfProperty("count") >= 0 and child.property("count") == 11 and child.metaObject().indexOfProperty("contentY") >= 0:
                child.setProperty("contentY", max(0, child.property("contentHeight") - child.property("height")))
        capture("workbench-custom-step.png", popup=True)
        for index, name in [(2, "workbench-sdk.png"), (1, "workbench-submission-policy.png"), (4, "workbench-submission-monitor.png")]:
            tabs.setProperty("currentIndex", index)
            capture(name, popup=True)
        QMetaObject.invokeMethod(settings, "close")
        controller.promptEditorRequested.emit("交付前检查与答辩材料整理", "writing", custom_id, "custom", demo_prompt)
        capture("workbench-prompt-editor.png", popup=True)
        for child in window.findChildren(QObject):
            if child.metaObject().indexOfProperty("dialogTitle") >= 0:
                QMetaObject.invokeMethod(child, "close")
        for i, (pid, paper) in enumerate(papers.items()):
            paper["current_stage"] = "submission"
            paper["stages"]["journal_author"]["status"] = "selection_complete"
            paper["stages"]["submission"].update({"status": ["monitoring", "waiting_user", "ready"][i],
                "detail": ["演示：已记录回执，等待期刊状态更新。", "演示：等待用户确认站点条款后继续。", "演示：投稿包已就绪，等待启动。"][i]})
        controller._submission_monitor = {"hasRun": True, "status": "演示检查完成", "tone": "success",
            "finishedAt": "2026-10-28 09:00", "requested": 3, "checked": 3, "failed": 0, "pending": 1, "actionRequired": 1}
        controller.submissionMonitorChanged.emit()
        controller._apply_state({"papers": papers}, duration_ms=0)
        controller.selectPage("submission")
        controller.selectPaper("demo-001")
        capture("workbench-submission.png")
        assert not warnings, warnings
        result = {"ok": True, "synthetic_data": True, "model_turns": 0, "qml_warnings": warnings,
                  "images": images, "blocked_external_actions": blocked_actions,
                  "custom_step_saved": custom_id, "actual_submissions": 0}
        (output / "receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        window.close()
        controller.shutdown()
        engine.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    main()
