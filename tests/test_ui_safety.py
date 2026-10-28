from __future__ import annotations

import ast
import json
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

from unified_paper_orchestrator.ui.main_window import (
    TaskRunner,
    UnifiedControlPanel,
    _stage_result_error,
    _sync_treeview_rows,
    _treeview_row,
)


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "unified_paper_orchestrator" / "ui" / "main_window.py"
ACCOUNT_MANAGER_PATH = ROOT / "unified_paper_orchestrator" / "ui" / "account_site_manager.py"


def function_source(name: str) -> str:
    source = UI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function not found: {name}")


class FakeRoot:
    def __init__(self) -> None:
        self.callbacks: list[callable] = []

    def after(self, _delay: int, callback: callable) -> None:
        self.callbacks.append(callback)

    def run_one_callback(self) -> None:
        callback = self.callbacks.pop(0)
        callback()


class FakeTreeview:
    """Small public-API Treeview double for incremental rendering tests."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.rows: dict[str, dict[str, object]] = {}
        self.selected: list[str] = []
        self.focused = ""
        self.scroll = (0.0, 1.0)

    def get_children(self, _parent: str = "") -> tuple[str, ...]:
        return tuple(self.order)

    def selection(self) -> tuple[str, ...]:
        return tuple(self.selected)

    def selection_set(self, *items: str) -> None:
        self.selected = [str(item) for item in items]

    def selection_remove(self, *items: str) -> None:
        removed = {str(item) for item in items}
        self.selected = [item for item in self.selected if item not in removed]

    def focus(self, item: str | None = None) -> str:
        if item is not None:
            self.focused = str(item)
        return self.focused

    def yview(self) -> tuple[float, float]:
        return self.scroll

    def yview_moveto(self, fraction: float) -> None:
        span = self.scroll[1] - self.scroll[0]
        self.scroll = (float(fraction), min(1.0, float(fraction) + span))

    def exists(self, iid: str) -> bool:
        return str(iid) in self.rows

    def insert(
        self,
        _parent: str,
        index: int,
        *,
        iid: str,
        text: str,
        values: tuple[str, ...],
        tags: tuple[str, ...],
    ) -> None:
        iid = str(iid)
        self.order.insert(int(index), iid)
        self.rows[iid] = {"text": text, "values": values, "tags": tags}

    def delete(self, iid: str) -> None:
        iid = str(iid)
        self.order.remove(iid)
        self.rows.pop(iid)

    def item(self, iid: str, **updates: object) -> dict[str, object]:
        iid = str(iid)
        if updates:
            self.rows[iid].update(updates)
        return dict(self.rows[iid])

    def index(self, iid: str) -> int:
        return self.order.index(str(iid))

    def move(self, iid: str, _parent: str, index: int) -> None:
        iid = str(iid)
        self.order.remove(iid)
        self.order.insert(int(index), iid)


class UiSafetyTest(unittest.TestCase):
    def test_treeview_diff_refresh_preserves_selection_focus_and_scroll(self) -> None:
        tree = FakeTreeview()
        for index, row in enumerate(
            (
                _treeview_row("a", "论文 A", ("等待",), ("waiting",)),
                _treeview_row("b", "论文 B", ("运行",), ("running",)),
                _treeview_row("c", "论文 C", ("完成",), ("done",)),
            )
        ):
            tree.insert("", index, **row)
        tree.selection_set("b")
        tree.focus("b")
        tree.scroll = (0.35, 0.75)

        rows = [
            _treeview_row("c", "论文 C", ("完成",), ("done",)),
            _treeview_row("b", "论文 B", ("Step 2",), ("running",)),
            _treeview_row("d", "论文 D", ("排队",), ("queued",)),
        ]
        result = _sync_treeview_rows(tree, rows)

        self.assertEqual(tree.order, ["c", "b", "d"])
        self.assertEqual(result, {"inserted": 1, "updated": 1, "moved": 1, "deleted": 1})
        self.assertEqual(tree.selection(), ("b",))
        self.assertEqual(tree.focus(), "b")
        self.assertEqual(tree.yview()[0], 0.35)

        self.assertEqual(
            _sync_treeview_rows(tree, rows),
            {"inserted": 0, "updated": 0, "moved": 0, "deleted": 0},
        )

    def test_nested_scheduler_failure_is_visible_to_stage_callbacks(self) -> None:
        failed = {
            "last_scheduler_result": {
                "results": {"writing": {"ok": False, "error": "writing scheduler refused"}}
            }
        }
        succeeded = {"last_scheduler_result": {"results": {"writing": {"ok": True}}}}

        self.assertEqual(_stage_result_error(failed, "writing"), "writing scheduler refused")
        self.assertEqual(_stage_result_error(succeeded, "writing"), "")

    def test_regular_stage_tab_has_no_smart_queue_controls(self) -> None:
        source = function_source("_build_stage_tab")
        self.assertNotIn("prioritize_smart_submission_selected", source)
        self.assertNotIn("move_smart_submission_selected", source)

    def test_stage_primary_start_is_single_active_and_parallel_is_advanced(self) -> None:
        stage_source = function_source("_build_stage_tab")
        tools_source = function_source("show_stage_tools")
        new_writing_source = function_source("start_new_writing_session")
        resume_writing_source = function_source("resume_selected_writing")
        self.assertIn("start_stage_single_active", stage_source)
        self.assertNotIn("start_stage_parallel", stage_source)
        self.assertIn("高级：并行启动", tools_source)
        self.assertIn("start_stage_parallel", tools_source)
        self.assertIn("single_active=True", new_writing_source)
        self.assertIn("single_active=True", resume_writing_source)

    def test_writing_buttons_name_their_scope_and_new_count_is_not_called_concurrency(self) -> None:
        stage_source = function_source("_build_stage_tab")
        new_source = function_source("start_new_writing_session")
        start_source = function_source("start_stage_single_active")
        pause_source = function_source("pause_stage")

        self.assertIn("运行全部写作任务", stage_source)
        self.assertIn("暂停全部写作（当前 Step 后）", stage_source)
        self.assertIn("新建数量", new_source)
        self.assertIn("不是并发量", new_source)
        self.assertIn("只运行本次新建的论文", new_source)
        self.assertNotIn("本次并发新建", new_source)
        self.assertIn("所有排队或暂停的论文", start_source)
        self.assertIn("作用范围：全部论文", pause_source)

    def test_writing_parallel_limit_is_editable_and_bounded(self) -> None:
        source = function_source("_open_stage_prompt_settings_dialog")
        self.assertIn("同时运行的论文上限", source)
        self.assertIn("WRITING_MAX_PARALLEL_WORKERS", source)
        self.assertIn("one_action_var.set(False)", source)
        self.assertIn('stage in {"writing", "figures", "journal_author"}', source)
        self.assertIn("每篇论文只占一个 SDK 会话", source)
        self.assertIn("Step 1→当前流程终点", source)
        self.assertIn('text="写作步骤"', source)
        self.assertNotIn('text="目标调度会话标识" if stage == "writing"', source)

    def test_heavy_settings_and_account_registry_load_off_the_tk_thread(self) -> None:
        settings_source = function_source("open_stage_prompt_settings")
        account_source = function_source("open_account_site_manager")
        close_source = function_source("_on_close")

        self.assertIn("_run_dialog_background_task", settings_source)
        self.assertIn("stage_prompt_inventory", settings_source)
        self.assertIn("_run_dialog_background_task", account_source)
        self.assertIn("AccountSiteStore", account_source)
        self.assertIn("store=store", account_source)
        self.assertIn("dialog_runner.close()", close_source)
        self.assertIn("maintenance_runner.close()", close_source)

    def test_periodic_credential_recovery_runs_on_maintenance_worker(self) -> None:
        source = function_source("_poll_due_smart_submission_monitor")

        self.assertIn("maintenance_runner.submit", source)
        self.assertIn("retry_pending_credentials", source)
        self.assertIn("retry_failed", source)

    def test_smart_refresh_without_snapshot_is_scheduled_off_the_tk_thread(self) -> None:
        requests: list[dict[str, object]] = []
        panel = SimpleNamespace(
            _request_ui_refresh=lambda **payload: requests.append(payload)
        )

        UnifiedControlPanel.refresh_smart_submission(panel, update_detail=False)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["tab"], "smart_submission")
        self.assertTrue(requests[0]["preserve_smart_detail"])

    def test_action_bar_wraps_buttons_instead_of_clipping_on_narrow_windows(self) -> None:
        class FakeContainer:
            def __init__(self) -> None:
                self.callback = None

            def bind(self, _event: str, callback: object, add: str = "") -> None:
                self.callback = callback

            def after_idle(self, callback: object) -> None:
                callback()

            def winfo_width(self) -> int:
                return 250

        class FakeButton:
            def __init__(self, width: int) -> None:
                self.width = width
                self.position: tuple[int, int] | None = None

            def winfo_reqwidth(self) -> int:
                return self.width

            def grid(self, *, row: int, column: int, **_options: object) -> None:
                self.position = (row, column)

        container = FakeContainer()
        buttons = [FakeButton(100), FakeButton(100), FakeButton(100)]

        UnifiedControlPanel._install_responsive_action_layout(container, buttons)

        self.assertEqual(
            [button.position for button in buttons],
            [(0, 0), (0, 1), (1, 0)],
        )

    def test_smart_status_bar_has_one_success_and_one_fail_closed_write(self) -> None:
        source = function_source("refresh_smart_submission")
        self.assertEqual(source.count("smart_submission_status_var.set("), 2)
        self.assertIn("读取失败", source)
        self.assertIn("return", source)

    def test_smart_monitor_time_is_user_editable_and_persisted(self) -> None:
        settings_source = function_source("open_smart_submission_settings")
        enable_source = function_source("enable_smart_submission_daily_monitoring")
        build_source = function_source("_build_smart_submission_tab")
        immediate_source = function_source("run_smart_submission_monitor_now")
        poll_source = function_source("_poll_due_smart_submission_monitor")

        self.assertIn("每日检查时间", settings_source)
        self.assertIn("monitor_time_var", settings_source)
        self.assertIn("立即检查一次", settings_source)
        self.assertIn(
            "update_smart_submission_daily_monitoring_time",
            settings_source,
        )
        self.assertIn("configured_time", enable_source)
        self.assertNotIn("每天 09:00 的状态巡检", enable_source)
        self.assertIn("立即检查状态", build_source)
        self.assertIn("force=True", immediate_source)
        self.assertIn('trigger="manual_ui"', immediate_source)
        self.assertIn("smart_submission_daily_monitor_due", poll_source)
        self.assertIn('trigger="in_app_schedule"', poll_source)

    def test_smart_monitor_result_remains_visible_in_banner_and_paper_rows(self) -> None:
        build_source = function_source("_build_smart_submission_tab")
        refresh_source = function_source("refresh_smart_submission")

        self.assertIn("smart_submission_monitor_result_var", build_source)
        self.assertIn('tree.heading("monitor", text="状态检查")', build_source)
        self.assertIn("_smart_monitor_summary_presentation", refresh_source)
        self.assertIn("_smart_monitor_paper_label", refresh_source)

        normal, normal_color = UnifiedControlPanel._smart_monitor_summary_presentation(
            {
                "daily_monitor_last_status": "completed",
                "daily_monitor_last_finished_at": "2026-07-18T03:23:26+08:00",
                "daily_monitor_last_paper_count": 7,
                "daily_monitor_last_checked_count": 7,
            }
        )
        self.assertTrue(normal.startswith("✓ 最近检查正常"))
        self.assertIn("7/7", normal)
        self.assertEqual(normal_color, "#237a3b")

        action, action_color = UnifiedControlPanel._smart_monitor_summary_presentation(
            {
                "daily_monitor_last_status": "completed",
                "daily_monitor_last_finished_at": "2026-07-18T04:00:00+08:00",
                "daily_monitor_last_paper_count": 2,
                "daily_monitor_last_checked_count": 2,
                "daily_monitor_last_action_required_count": 1,
                "daily_monitor_last_followup_auto_queued_count": 1,
            }
        )
        self.assertTrue(action.startswith("⚠ 最近检查发现新状态"))
        self.assertEqual(action_color, "#9a6700")

        self.assertEqual(
            UnifiedControlPanel._smart_monitor_paper_label(
                {
                    "status": "monitoring",
                    "last_monitor_checked_at": "2026-07-18T03:23:12+08:00",
                    "last_monitor_result": "monitoring",
                    "last_monitor_action_kind": "none",
                }
            ),
            "✓ 正常 07-18 03:23",
        )
        self.assertEqual(
            UnifiedControlPanel._smart_monitor_paper_label(
                {
                    "status": "revision_needed",
                    "last_monitor_checked_at": "2026-07-18T04:01:00+08:00",
                    "last_monitor_action_kind": "scientific_revision",
                    "last_monitor_followup_status": "waiting_scientific_revision",
                }
            ),
            "⚠ 待修订稿 07-18 04:01",
        )
        self.assertEqual(
            UnifiedControlPanel._smart_monitor_paper_label(
                {
                    "status": "revision_needed",
                    "last_monitor_checked_at": "2026-07-18T04:02:00+08:00",
                    "last_monitor_followup_status": "identity_reconciliation_required",
                    "submission_identity_status": "multiple_active_submissions",
                }
            ),
            "⛔ 重复投稿待处理 07-18 04:02",
        )
        self.assertEqual(
            UnifiedControlPanel._smart_monitor_paper_label(
                {
                    "status": "revision_needed",
                    "last_monitor_checked_at": "2026-07-18T04:03:00+08:00",
                    "last_monitor_followup_status": "online_withdrawal_queued",
                    "last_monitor_action_kind": "retarget",
                }
            ),
            "↻ 正在撤回后转投 07-18 04:03",
        )
        self.assertEqual(
            UnifiedControlPanel._smart_monitor_paper_label(
                {
                    "status": "monitoring",
                    "monitor_check_pending": True,
                    "next_action": "check_submission_status_on_verified_journal_dashboard",
                }
            ),
            "● 检查中｜期刊后台",
        )
        source_lines = UnifiedControlPanel._smart_monitor_source_lines(
            {
                "last_monitor_source_results": [
                    {
                        "source_id": "gmail",
                        "source_label": "Gmail",
                        "available": True,
                        "status": "monitoring",
                    },
                    {
                        "source_id": "xujc-mail",
                        "source_label": "学校邮箱",
                        "available": True,
                        "status": "revision_needed",
                        "summary": "Major revision requested.",
                    },
                    {
                        "source_id": "journal-dashboard",
                        "source_label": "期刊投稿后台",
                        "available": False,
                        "status": "waiting",
                        "error_code": "JOURNAL_DASHBOARD_UNAVAILABLE",
                    },
                ]
            }
        )
        self.assertEqual(source_lines[0], "检查来源（本轮）：")
        self.assertTrue(any("学校邮箱：已检查｜需要处理/返修" in line for line in source_lines))
        self.assertTrue(any("期刊投稿后台：不可用" in line for line in source_lines))

    def test_submission_hub_keeps_stable_default_and_smart_trial_routes(self) -> None:
        source = function_source("_build_ui")
        self.assertIn("投稿中心", source)
        self.assertIn("稳定投稿（默认）", source)
        self.assertIn("智能投稿（试运行）", source)
        self.assertIn("submission_notebook", source)

    def test_nested_submission_tabs_preserve_selection_scope(self) -> None:
        source = function_source("current_tab_key")
        self.assertIn("submission_hub", source)
        self.assertIn("submission_notebook.select", source)
        self.assertIn('return "smart_submission"', source)

    def test_smart_online_requires_explicit_selection_and_uses_sdk_program_dispatch(self) -> None:
        source = function_source("start_smart_submission_visible_online")
        self.assertIn("if not selected", source)
        self.assertIn("prepare_smart_submission_sdk_papers", source)
        self.assertIn("start_smart_submission_program_runtime", source)
        self.assertNotIn("start_single_smart_canary", source)
        self.assertNotIn("set_submission_route", source)
        self.assertNotIn("selected_ids=None", source)

    def test_smart_local_precheck_never_queues_online_and_sdk_start_is_separate(self) -> None:
        local_source = function_source("request_smart_submission_run")
        online_source = function_source("start_smart_submission_visible_online")
        self.assertIn("queue_online=False", local_source)
        self.assertIn("activate_online=False", local_source)
        self.assertIn("prepare_smart_submission_sdk_papers", online_source)
        self.assertIn("queued_ids", online_source)
        self.assertIn("if queued_ids", online_source)

    def test_account_site_manager_is_a_single_top_level_entry(self) -> None:
        source = UI_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count('text="账号与站点"'), 1)
        self.assertNotIn("打开账号库", source)
        self.assertNotIn("打开站点库", source)
        self.assertIn("vault_path=ACCOUNT_VAULT_PATH", source)
        self.assertIn("legacy_vault_path=LEGACY_CREDENTIAL_VAULT_PATH", source)
        self.assertNotIn(r'D:\Documents\New project 11\controller\credential_store.json', source)

    def test_account_site_manager_keeps_the_store_canonical_backup_root(self) -> None:
        source = ACCOUNT_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn("backup_dir=backup_dir", source)
        self.assertNotIn('backup_dir=backup_dir or BACKUP_DIR / "account_site_manager"', source)

    def test_permanent_delete_context_is_bound_to_the_clicked_paper(self) -> None:
        purge_source = function_source("purge_selected")
        stage_context_source = function_source("show_stage_context_menu")
        live_context_source = function_source("show_live_task_context_menu")

        self.assertIn("requested_ids", purge_source)
        self.assertIn("只会处理下方明确列出的编号", purge_source)
        self.assertIn("self.purge_selected([paper_id])", stage_context_source)
        self.assertIn("self.purge_selected([paper_id])", live_context_source)

    def test_account_site_manager_uses_compact_master_detail_and_hides_technical_ids_by_default(self) -> None:
        source = ACCOUNT_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn("ttk.PanedWindow", source)
        self.assertIn("_build_master_pane", source)
        self.assertIn("_build_detail_pane", source)
        self.assertIn('text="显示技术信息"', source)
        self.assertIn("technical_width = 155 if show_technical else 0", source)
        self.assertNotIn("ttk.Notebook", source)
        self.assertNotIn('heading("technical", text="密码', source)

    def test_account_site_manager_exposes_audited_dpapi_migration(self) -> None:
        source = ACCOUNT_MANAGER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        copy_method = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "copy_smart_password_to_vault"
        )
        copy_source = ast.get_source_segment(source, copy_method) or ""
        self.assertIn("store.copy_smart_account_to_vault", copy_source)
        self.assertIn("迁移选中旧明文到 Windows 加密库", source)
        self.assertIn("migrate_smart_plaintext_credentials", source)
        self.assertIn("Windows 当前用户 DPAPI 加密库", copy_source)

    def test_grouped_records_require_explicit_technical_selection_before_dangerous_actions(self) -> None:
        source = ACCOUNT_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn("_selection_requires_technical_info", source)
        self.assertIn("_confirm_specific_record_selection", source)
        self.assertIn("请先打开“显示技术信息”", source)
        self.assertIn("requires_technical", source)

    def test_figure_target_parser_has_one_definition(self) -> None:
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "parse_figure_targets"
        ]
        self.assertEqual(len(definitions), 1)
        self.assertEqual(
            UnifiedControlPanel.parse_figure_targets("Figure 1，fig:latency; F2"),
            ["Figure 1", "Figure 2", "fig:latency"],
        )

    def test_emergency_stop_and_manual_pause_use_true_pause_helper(self) -> None:
        pause_all = function_source("pause_all")
        pause_smart = function_source("pause_smart_submission")
        helper = function_source("_pause_smart_submission_runtime")
        daily = function_source("enable_smart_submission_daily_monitoring")
        self.assertIn("orchestrator.pause_all", pause_all)
        self.assertIn("_pause_smart_submission_runtime", pause_all)
        self.assertIn("_pause_smart_submission_runtime", pause_smart)
        self.assertNotIn("set_smart_submission_automation_fast_wakeup(False)", pause_smart)
        self.assertIn("pause_smart_submission_automation", helper)
        self.assertIn("pause_smart_submission_sdk_after_current", helper)
        self.assertIn("reason=reason", helper)
        self.assertIn("restore_smart_submission_daily_monitoring", daily)

    def test_online_confirmation_exposes_scope_network_and_parallelism(self) -> None:
        source = function_source("start_smart_submission_visible_online")
        self.assertIn("max_parallel", source)
        self.assertIn("prepare_smart_submission_sdk_papers", source)
        self.assertIn("start_smart_submission_program_runtime", source)

    def test_final_submit_defaults_to_program_auto_with_manual_fallback(self) -> None:
        menu_source = function_source("show_smart_submission_context_menu")
        more_source = function_source("show_smart_submission_tools")
        readiness_source = function_source("_smart_final_submit_available")
        confirm_source = function_source("confirm_smart_submission_final_submit")
        start_source = function_source("start_smart_submission_visible_online")
        settings_source = function_source("open_smart_submission_settings")
        cli_source = (ROOT / "unified_paper_orchestrator" / "__main__.py").read_text(encoding="utf-8")

        self.assertIn("FINAL_SUBMIT_REVIEW_READY_ACTION", readiness_source)
        self.assertIn("auto_final_submit_enabled", readiness_source)
        self.assertIn("自动签发", start_source)
        self.assertIn("自动完成最终提交", settings_source)
        self.assertIn("update_smart_submission_execution_policy", settings_source)
        self.assertIn("确认最终提交", menu_source)
        self.assertIn("确认最终提交", more_source)
        self.assertIn("prepare_final_submit_confirmation_challenge", confirm_source)
        self.assertIn("ui_window_handle=int(self.winfo_id())", confirm_source)
        self.assertIn("messagebox.askyesno", confirm_source)
        self.assertIn("default=messagebox.NO", confirm_source)
        self.assertIn('challenge.pop("confirmation_phrase", "")', confirm_source)
        self.assertIn("authorize_final_submit", confirm_source)
        self.assertNotIn("smart-submission-authorize-final-submit", cli_source)

    def test_smart_context_menu_has_keyboard_paths_and_daily_monitoring_is_not_a_primary_button(self) -> None:
        build_source = function_source("_build_smart_submission_tab")
        binding_source = function_source("_bind_keyboard_context_menu")
        tools_source = function_source("show_smart_submission_tools")

        self.assertNotIn('text="每日监控"', build_source)
        self.assertIn("<Shift-F10>", binding_source)
        self.assertIn("<KeyPress-App>", binding_source)
        self.assertIn("<KeyPress-Menu>", binding_source)
        self.assertIn("设为智能投稿路线", tools_source)
        self.assertIn("改回稳定投稿路线", tools_source)

    def test_normal_result_panels_separate_human_summary_from_redacted_technical_details(self) -> None:
        build_source = function_source("_build_overview_tab")
        toggle_source = function_source("_toggle_overview_technical_details")
        result_source = function_source("_show_operation_result")
        redaction_source = UI_PATH.read_text(encoding="utf-8")

        self.assertIn('text="显示技术信息"', build_source)
        self.assertIn("tk.BooleanVar(value=False)", build_source)
        self.assertNotIn('notebook.add(self.technical_text.master, text="技术详情")', build_source)
        self.assertIn('text="技术详情"', toggle_source)
        self.assertIn("notebook.forget(technical_text.master)", toggle_source)
        self.assertIn("_set_technical_details", result_source)
        self.assertIn("confirmation_phrase", redaction_source)
        self.assertIn("password", redaction_source)

    def test_overview_technical_tab_is_only_attached_while_opted_in(self) -> None:
        class FakeNotebook:
            def __init__(self) -> None:
                self.visible: list[str] = []
                self.selected = ""

            def tabs(self) -> tuple[str, ...]:
                return tuple(self.visible)

            def add(self, tab: str, *, text: str) -> None:
                self.visible.append(str(tab))

            def select(self, tab: str) -> None:
                self.selected = str(tab)

            def forget(self, tab: str) -> None:
                self.visible.remove(str(tab))

        notebook = FakeNotebook()
        show_var = SimpleNamespace(get=lambda: True)
        panel = SimpleNamespace(
            overview_detail_notebook=notebook,
            technical_text=SimpleNamespace(master="technical-tab"),
            show_overview_technical_var=show_var,
        )

        UnifiedControlPanel._toggle_overview_technical_details(panel)
        self.assertEqual(notebook.visible, ["technical-tab"])
        self.assertEqual(notebook.selected, "technical-tab")

        show_var.get = lambda: False
        UnifiedControlPanel._toggle_overview_technical_details(panel)
        self.assertEqual(notebook.visible, [])

    def test_smart_primary_actions_do_not_repeat_global_or_settings_entries(self) -> None:
        build_source = function_source("_build_smart_submission_tab")
        tools_source = function_source("show_smart_submission_tools")

        self.assertNotIn("open_account_site_manager", build_source)
        self.assertEqual(build_source.count("open_smart_submission_settings"), 1)
        self.assertNotIn("open_smart_submission_settings", tools_source)
        self.assertNotIn('text="每日监控"', build_source)

    def test_smart_retarget_completion_uses_existing_refresh_api(self) -> None:
        source = function_source("open_smart_retarget_dialog")

        self.assertIn("self.refresh()", source)
        self.assertNotIn("self.refresh_all()", source)

    def test_technical_details_recursively_remove_secret_values_and_url_credentials(self) -> None:
        secrets = {
            "dict-password",
            "nested-token",
            "tuple-cookie",
            "free-secret",
            "vault-secret-ref",
            "auth-secret",
            "digest-user-secret",
            "digest-response-secret",
            "credential-hyphen-secret",
            "credential-space-secret",
            "one-time-password-secret",
            "response-assignment-secret",
            "signature-assignment-secret",
            "session-assignment-secret",
            "cookie-header-one",
            "cookie-header-two",
            "url-user-password",
            "url-query-secret",
            "exception-otp",
        }
        payload = {
            "password": "dict-password",
            "nested": [
                {"access_token": "nested-token"},
                (
                    "cookie=tuple-cookie",
                    "credential_ref=vault:vault-secret-ref",
                    "Cookie: session=cookie-header-one; csrftoken=cookie-header-two",
                ),
            ],
            "credentialRef": "camel-case-reference-secret",
            "message": (
                "failed password=free-secret Authorization: Bearer auth-secret\n"
                'Authorization: Digest username="digest-user-secret", '
                'response="digest-response-secret"\n'
                "credential-ref = credential-hyphen-secret; "
                "credential ref=credential-space-secret; "
                "one-time password=one-time-password-secret; "
                "response=response-assignment-secret; signature=signature-assignment-secret; "
                "session=session-assignment-secret\n"
                "at https://alice:url-user-password@example.test/path?token=url-query-secret#fragment"
            ),
            "error": RuntimeError("OTP=exception-otp"),
        }

        clean = UnifiedControlPanel._redact_technical_payload(payload)
        serialized = json.dumps(clean, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertIn("https://example.test/path", serialized)
        self.assertNotIn("?", serialized)
        self.assertNotIn("#", serialized)
        self.assertNotIn("alice", serialized)
        self.assertNotIn("camel-case-reference-secret", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_account_forms_are_scrollable_and_hide_generated_ids_by_default(self) -> None:
        source = ACCOUNT_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn("self._form_canvas", source)
        self.assertIn('actions.grid(row=1, column=0', source)
        self.assertIn("def _scroll_form", source)
        self.assertIn("def _system_fields(*, include_technical: bool = False)", source)
        self.assertIn("include_technical: bool = False", source)
        self.assertIn('{"key": "system_id", "label": "内部标识（高级）", "readonly": True}', source)
        self.assertIn('{"key": "journal_key", "label": "内部标识（高级）", "readonly": True}', source)

    def test_supported_tk_accepts_keyboard_context_sequences(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        try:
            root.withdraw()
            tree = ttk.Treeview(root)
            tree.bind("<Shift-F10>", lambda _event: "break")
            accepted = 1
            for sequence in ("<KeyPress-App>", "<KeyPress-Menu>"):
                try:
                    tree.bind(sequence, lambda _event: "break")
                except tk.TclError:
                    continue
                accepted += 1
            self.assertGreaterEqual(accepted, 1)
        finally:
            root.destroy()

    def test_global_exception_hooks_and_rotating_log_are_installed(self) -> None:
        source = UI_PATH.read_text(encoding="utf-8")
        main_source = function_source("main")
        self.assertIn("RotatingFileHandler", source)
        self.assertIn("report_callback_exception", source)
        self.assertIn("install_global_exception_hooks()", main_source)
        self.assertIn("configure_ui_logging()", main_source)

    def test_task_runner_serializes_work_and_marshals_result(self) -> None:
        root = FakeRoot()
        runner = TaskRunner(root, poll_ms=1)
        completed = threading.Event()
        results: list[int] = []
        errors: list[BaseException] = []

        self.assertTrue(runner.submit("first", lambda: 42, lambda value: (results.append(value), completed.set()), errors.append))
        self.assertFalse(runner.submit("second", lambda: 7, results.append, errors.append))

        deadline = time.time() + 2
        while runner._results.empty() and time.time() < deadline:  # bounded test of the thread-to-Tk queue
            time.sleep(0.01)
        root.run_one_callback()

        self.assertTrue(completed.is_set())
        self.assertEqual(results, [42])
        self.assertEqual(errors, [])
        self.assertEqual(runner.active_name, "")
        runner.close()


if __name__ == "__main__":
    unittest.main()
