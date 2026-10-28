from __future__ import annotations

import json
from pathlib import Path

from unified_paper_orchestrator.ui.main_window import (
    _writing_session_log_path,
    _writing_session_transcript,
    _writing_task_rows,
)


def test_live_rows_show_exact_running_step_and_progress() -> None:
    rows = _writing_task_rows(
        {
            "session:paper-a": {
                "paper_id": "2026-07-14_001_paper-a",
                "status": "running",
                "completed_step": 3,
                "active_step": 4,
                "run_until_step": 10,
                "updated_at": "2026-07-14T23:01:02+08:00",
                "thread_id": "019f610f-f2f8-7bb3-bb17-e6d765a75285",
                "steps": {"4": {"state": "running", "turn_id": "turn-step-4"}},
            }
        }
    )

    assert len(rows) == 1
    assert rows[0]["status_label"] == "正在运行"
    assert rows[0]["step_label"] == "正在执行 Step 4 / 10"
    assert rows[0]["completed_step"] == 3
    assert rows[0]["progress"].endswith("3/10")
    assert rows[0]["tag"] == "running"
    assert rows[0]["thread_id"] == "019f610f-f2f8-7bb3-bb17-e6d765a75285"
    assert rows[0]["turn_id"] == "turn-step-4"


def test_live_rows_include_pending_unnamed_command_before_journal_registration() -> None:
    rows = _writing_task_rows(
        {},
        {
            "commands": [
                {
                    "id": "cmd-new-one",
                    "sdk_paper_key": "new-session:cmd-new-one",
                    "action": "new_session",
                    "status": "pending",
                    "params": {
                        "run_until_step": 10,
                        "reserved_pipeline_prefix": "2026-07-16_003",
                    },
                    "created_at": "2026-07-14T23:02:03+08:00",
                }
            ]
        },
    )

    assert len(rows) == 1
    assert rows[0]["paper_key"] == "new-session:cmd-new-one"
    assert rows[0]["paper_id"] == ""
    assert rows[0]["label"] == "2026-07-16_003_待定标题（Step 1 后补全）"
    assert rows[0]["step_label"] == "待执行 Step 1 / 10"


def test_live_rows_explain_step_five_evidence_gate_in_user_language() -> None:
    rows = _writing_task_rows(
        {
            "session:paper-a": {
                "paper_id": "2026-07-14_001_paper-a",
                "status": "waiting_prerequisite",
                "completed_step": 5,
                "run_until_step": 10,
                "blocker": "WRITING_EVIDENCE_GATE_BLOCKED",
                "steps": {},
            }
        }
    )

    assert rows[0]["status_label"] == "等待实验数据确认"
    assert rows[0]["step_label"] == "Step 5 已完成 → 等待 Step 6"
    assert "数据已手动改完并继续" in rows[0]["blocker_label"]


def test_live_rows_present_proven_step5_cleanup_block_as_manual_data_wait() -> None:
    rows = _writing_task_rows(
        {
            "session:paper-a": {
                "paper_id": "2026-07-18_003_paper-a",
                "status": "blocked",
                "completed_step": 4,
                "run_until_step": 10,
                "blocker": "WRITING_OUTPUT_SEMANTIC_INVALID",
                "blocker_message": "old validator wording",
                "steps": {
                    "5": {
                        "state": "blocked",
                        "semantic_validation": {
                            "ready": False,
                            "issues": [
                                {"code": "latex_unescaped_verify_data_identifier"},
                                {"code": "latex_unescaped_text_underscore"},
                            ],
                            "warnings": [],
                        },
                    }
                },
            }
        }
    )

    assert rows[0]["status_label"] == "等待实验数据确认"
    assert rows[0]["step_label"] == "Step 5 产物已生成 → 确认后进入 Step 6"
    assert "数据已手动改完并继续" in rows[0]["blocker_label"]


def test_live_rows_put_running_and_queued_work_before_completed_history() -> None:
    rows = _writing_task_rows(
        {
            "complete": {
                "paper_id": "complete-paper",
                "status": "complete",
                "completed_step": 10,
                "run_until_step": 10,
                "steps": {},
            },
            "queued": {
                "paper_id": "queued-paper",
                "status": "queued",
                "completed_step": 2,
                "run_until_step": 10,
                "steps": {},
            },
            "running": {
                "paper_id": "running-paper",
                "status": "running",
                "completed_step": 4,
                "active_step": 5,
                "run_until_step": 10,
                "steps": {},
            },
        }
    )

    assert [row["paper_id"] for row in rows] == ["running-paper", "queued-paper", "complete-paper"]


def test_live_rows_show_real_blocker_message_and_deferred_cleanup_warning() -> None:
    rows = _writing_task_rows(
        {
            "blocked": {
                "paper_id": "blocked-paper",
                "status": "blocked",
                "completed_step": 5,
                "blocker": "WRITING_OUTPUT_SEMANTIC_INVALID",
                "blocker_message": "Step 6 still contains VERIFY at line 174",
                "thread_id": "sdk-thread-blocked",
                "steps": {
                    "6": {
                        "state": "blocked",
                        "turn_id": "turn-step-6",
                        "semantic_validation": {
                            "issues": [{"code": "old", "message": "old issue"}],
                            "warnings": [],
                            "path": "papers/blocked-paper/draft_v3.tex",
                        },
                    }
                },
            },
            "warning": {
                "paper_id": "warning-paper",
                "status": "queued",
                "completed_step": 6,
                "thread_id": "sdk-thread-warning",
                "steps": {
                    "6": {
                        "state": "completed",
                        "turn_id": "turn-step-6",
                        "semantic_validation": {
                            "issues": [],
                            "warnings": [
                                {
                                    "code": "latex_forbidden_marker_deferred",
                                    "message": "cleanup remains due by Step 10",
                                }
                            ],
                        },
                    }
                },
            },
        }
    )

    blocked = next(row for row in rows if row["paper_id"] == "blocked-paper")
    warning = next(row for row in rows if row["paper_id"] == "warning-paper")
    assert blocked["blocker_label"] == "Step 6 still contains VERIFY at line 174"
    assert blocked["semantic_path"] == "papers/blocked-paper/draft_v3.tex"
    assert warning["blocker_label"].startswith("待 Step 10 清理：")


def test_sdk_session_log_locator_and_transcript_are_bounded(tmp_path: Path) -> None:
    thread_id = "019f610f-f2f8-7bb3-bb17-e6d765a75285"
    session = tmp_path / "sessions" / "2026" / "07" / "15" / f"rollout-now-{thread_id}.jsonl"
    session.parent.mkdir(parents=True)
    rows = [
        {
            "timestamp": "2026-07-15T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Step 6 prompt"},
        },
        {
            "timestamp": "2026-07-15T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Working"},
        },
        {
            "timestamp": "2026-07-15T00:00:02Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "duplicate"},
        },
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    located = _writing_session_log_path(tmp_path, thread_id)
    transcript = _writing_session_transcript(session, max_messages=10)

    assert located == session.resolve()
    assert transcript["ok"] is True
    assert [(item["role"], item["text"]) for item in transcript["messages"]] == [
        ("用户 / 调度器", "Step 6 prompt"),
        ("Codex", "Working"),
    ]
