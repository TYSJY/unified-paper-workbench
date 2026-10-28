from __future__ import annotations

from unified_paper_orchestrator import smart_submission


def test_legacy_secret_broker_block_is_requeued_for_new_local_broker() -> None:
    state = {
        "settings": {"trusted_credential_broker_enabled": True},
        "paper_queue": [],
        "papers": {
            "paper-1": {
                "status": "blocked",
                "next_action": smart_submission.SMART_SECRET_BROKER_REQUIRED,
                "blocker": (
                    "preflight passed, but the configured journal account has "
                    "no current exact-origin live session and no trusted broker "
                    "completion evidence"
                ),
            }
        },
    }

    result = smart_submission._recover_legacy_secret_broker_blocks(
        state,
        selected_ids={"paper-1"},
    )

    paper = state["papers"]["paper-1"]
    assert result == {"changed": True, "recovered": ["paper-1"]}
    assert state["paper_queue"] == ["paper-1"]
    assert paper["status"] == "queued"
    assert paper["workflow_status"] == "queued"
    assert paper["next_action"] == "preflight"
    assert paper["blocker"] == ""
    assert paper["credential_broker_status"] == "legacy_block_recovered"


def test_current_broker_checkpoint_is_not_auto_requeued() -> None:
    state = {
        "settings": {"trusted_credential_broker_enabled": True},
        "paper_queue": [],
        "papers": {
            "paper-1": {
                "status": "blocked",
                "next_action": smart_submission.SMART_SECRET_BROKER_REQUIRED,
                "credential_broker_status": "waiting_for_two_factor",
            }
        },
    }

    result = smart_submission._recover_legacy_secret_broker_blocks(state)

    assert result == {"changed": False, "recovered": []}
    assert state["papers"]["paper-1"]["status"] == "blocked"
    assert state["paper_queue"] == []
