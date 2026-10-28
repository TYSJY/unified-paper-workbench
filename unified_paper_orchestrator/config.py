from __future__ import annotations

import os
import sys
from pathlib import Path

from .workspace import active_workspace


def resolve_repo_root() -> Path:
    override = os.environ.get("UNIFIED_PAPER_ORCHESTRATOR_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "dist" else exe_dir
    return Path(__file__).resolve().parents[1]


APP_ROOT = resolve_repo_root()
REPO_ROOT = APP_ROOT
ACTIVE_WORKSPACE = active_workspace(APP_ROOT)
WORKSPACE_ROOT = ACTIVE_WORKSPACE.root
WORKSPACE_ID = ACTIVE_WORKSPACE.workspace_id
WORKSPACE_NAME = ACTIVE_WORKSPACE.name
WORKSPACE_MODE = ACTIVE_WORKSPACE.mode
WORKSPACE_MANIFEST_PATH = ACTIVE_WORKSPACE.manifest_path
WORKSPACE_PATH_ALIASES = dict(ACTIVE_WORKSPACE.path_aliases or {})
STATE_DIR = ACTIVE_WORKSPACE.state_dir
UNIFIED_STATE_PATH = STATE_DIR / "unified_state.json"
SMART_SUBMISSION_STATE_PATH = STATE_DIR / "smart_submission" / "smart_submission_state.json"
SMART_SUBMISSION_SDK_JOURNAL_PATH = STATE_DIR / "smart_submission" / "sdk_dispatch.json"
SMART_SUBMISSION_SDK_LOCK_PATH = STATE_DIR / "smart_submission" / "sdk_dispatch.lock"
SMART_SUBMISSION_BROWSER_CONTEXT_PATH = (
    STATE_DIR / "smart_submission" / "sdk_browser_contexts.json"
)
UNIFIED_PROJECTION_DIRTY_PATH = STATE_DIR / "unified_projection_dirty.json"
UNIFIED_PROJECTION_LOCK_PATH = STATE_DIR / "unified_projection_dirty.lock"
BROWSER_SESSION_EVIDENCE_KEY_PATH = STATE_DIR / "smart_submission" / "browser_session_evidence_key.json"
UNIFIED_SUMMARY_PATH = STATE_DIR / "统一总控状态.md"
FULL_PIPELINE_STATE_PATH = STATE_DIR / "full_pipeline_state.json"
FULL_PIPELINE_LOCK_PATH = STATE_DIR / "full_pipeline_state.lock"
UNIFIED_PROMPT_PATH = REPO_ROOT / "prompts" / "UNIFIED_SCHEDULER_PROMPT.md"
BACKUP_DIR = STATE_DIR / "backups"
DELETED_DIR = STATE_DIR / "deleted"
ARCHIVED_PAPERS_DIR = STATE_DIR / "archived_papers"
PAPER_LIFECYCLE_AUDIT_PATH = STATE_DIR / "paper_lifecycle_audit.json"
LOCK_PATH = STATE_DIR / "unified_scheduler.lock"
LOCK_STALE_SECONDS = 30 * 60
WRITING_DISPATCH_PROTOCOL = "visible_slots_v1"
WRITING_DISPATCH_STATE_PATH = STATE_DIR / "writing_parallel_dispatch.json"
WRITING_DISPATCH_LOCK_PATH = STATE_DIR / "writing_parallel_dispatch.lock"
WRITING_SDK_DISPATCH_PROTOCOL = "sdk_events_v1"
WRITING_SDK_JOURNAL_PATH = STATE_DIR / "writing_sdk_dispatch.json"
WRITING_SDK_LOCK_PATH = STATE_DIR / "writing_sdk_dispatch.lock"
WRITING_GATE_POLICY_PATH = STATE_DIR / "writing_gate_policy.json"
WRITING_SDK_DISPATCH_GENERATION_KEY = "sdk_dispatch_generation"
WRITING_DEFAULT_PARALLEL_WORKERS = 1
WRITING_MAX_PARALLEL_WORKERS = 10
FULL_PIPELINE_MAX_PAPERS = 10
WRITING_MAX_WORKFLOW_STEPS = 50
WRITING_WORKFLOW_STATE_PATH = STATE_DIR / "writing_workflow.json"
WRITING_CUSTOM_PROMPT_DIR = STATE_DIR / "writing_workflow_prompts"
WRITING_CODEX_MODEL = os.environ.get("UNIFIED_WRITING_CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
WRITING_SDK_STEP_TOKEN_BUDGETS = {
    1: 1_000_000,
    2: 2_000_000,
    3: 3_000_000,
    4: 3_000_000,
    5: 4_000_000,
    6: 4_000_000,
    7: 4_000_000,
    8: 4_000_000,
    9: 4_000_000,
    10: 4_000_000,
}
FIGURE_SDK_DISPATCH_PROTOCOL = "figure_sdk_events_v1"
FIGURE_SDK_JOURNAL_PATH = STATE_DIR / "figure_sdk_dispatch.json"
FIGURE_SDK_LOCK_PATH = STATE_DIR / "figure_sdk_dispatch.lock"
FIGURE_PIPELINE_LOCK_PATH = STATE_DIR / "figure_pipeline_dispatch.lock"
FIGURE_DEFAULT_PARALLEL_WORKERS = 1
FIGURE_MAX_PARALLEL_WORKERS = 4
FIGURE_MAX_WORKFLOW_STEPS = 20
FIGURE_WORKFLOW_STATE_PATH = STATE_DIR / "figure_workflow.json"
FIGURE_CUSTOM_PROMPT_DIR = STATE_DIR / "figure_workflow_prompts"
FIGURE_CODEX_MODEL = os.environ.get("UNIFIED_FIGURE_CODEX_MODEL", WRITING_CODEX_MODEL).strip() or WRITING_CODEX_MODEL
JOURNAL_AUTHOR_SDK_DISPATCH_PROTOCOL = "journal_author_sdk_events_v1"
JOURNAL_AUTHOR_SDK_JOURNAL_PATH = STATE_DIR / "journal_author_sdk_dispatch.json"
JOURNAL_AUTHOR_SDK_LOCK_PATH = STATE_DIR / "journal_author_sdk_dispatch.lock"
JOURNAL_AUTHOR_PIPELINE_LOCK_PATH = STATE_DIR / "journal_author_pipeline_dispatch.lock"
JOURNAL_AUTHOR_DEFAULT_PARALLEL_WORKERS = 1
JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS = 6
JOURNAL_AUTHOR_MAX_WORKFLOW_STEPS = 20
JOURNAL_AUTHOR_WORKFLOW_STATE_PATH = STATE_DIR / "journal_author_workflow.json"
JOURNAL_AUTHOR_CUSTOM_PROMPT_DIR = STATE_DIR / "journal_author_workflow_prompts"
JOURNAL_AUTHOR_CODEX_MODEL = (
    os.environ.get("UNIFIED_JOURNAL_AUTHOR_CODEX_MODEL", WRITING_CODEX_MODEL).strip()
    or WRITING_CODEX_MODEL
)
SMART_SUBMISSION_CODEX_MODEL = (
    os.environ.get("UNIFIED_SMART_SUBMISSION_CODEX_MODEL", WRITING_CODEX_MODEL).strip()
    or WRITING_CODEX_MODEL
)

WRITING_ROOT = Path(ACTIVE_WORKSPACE.stage_roots["writing"])
FIGURE_ROOT = Path(ACTIVE_WORKSPACE.stage_roots["figures"])
JOURNAL_ROOT = Path(ACTIVE_WORKSPACE.stage_roots["journal_author"])
SUBMISSION_ROOT = Path(ACTIVE_WORKSPACE.stage_roots["submission"])
LEGACY_CREDENTIAL_VAULT_PATH = SUBMISSION_ROOT / "controller" / "credential_store.json"
_credential_vault_override = os.environ.get("UNIFIED_PAPER_CREDENTIAL_VAULT_PATH", "").strip()
CREDENTIAL_VAULT_PATH = (
    Path(_credential_vault_override).expanduser().resolve()
    if _credential_vault_override
    else LEGACY_CREDENTIAL_VAULT_PATH
)
_account_vault_override = os.environ.get("UNIFIED_PAPER_ACCOUNT_VAULT_PATH", "").strip()
ACCOUNT_VAULT_PATH = (
    Path(_account_vault_override).expanduser().resolve()
    if _account_vault_override
    else STATE_DIR / "smart_submission" / "account_vault.local.json"
)

STAGE_ORDER = ("writing", "figures", "journal_author", "submission")
STAGE_LABELS = {
    "writing": "写论文",
    "figures": "画图",
    "journal_author": "选刊作者",
    "submission": "投稿",
}

LEGACY_PROJECTS = {
    "writing": {
        "label": STAGE_LABELS["writing"],
        "root": WRITING_ROOT,
        "state_path": WRITING_ROOT / "controller" / "pipeline_state.json",
        "prompt_path": WRITING_ROOT / "controller" / "SCHEDULER_PROMPT.md",
        "papers_dir": WRITING_ROOT / "papers",
        "automation_ids": [
            "visible-edge-llm-10-step-paper-scheduler",
            "visible-edge-llm-paper-thread-scheduler",
        ],
        "primary_automation_id": "visible-edge-llm-10-step-paper-scheduler",
        "heartbeat_minutes": 5,
    },
    "figures": {
        "label": STAGE_LABELS["figures"],
        "root": FIGURE_ROOT,
        "state_path": FIGURE_ROOT / "controller" / "figure_pipeline_state.json",
        "prompt_path": FIGURE_ROOT / "controller" / "FIGURE_SCHEDULER_PROMPT.md",
        "papers_dir": FIGURE_ROOT / "papers",
        "automation_ids": ["paper-figure-codex-scheduler"],
        "primary_automation_id": "paper-figure-codex-scheduler",
        "heartbeat_minutes": 5,
    },
    "journal_author": {
        "label": STAGE_LABELS["journal_author"],
        "root": JOURNAL_ROOT,
        "state_path": JOURNAL_ROOT / "controller" / "journal_author_state.json",
        "prompt_path": JOURNAL_ROOT / "controller" / "JOURNAL_SCHEDULER_PROMPT.md",
        "papers_dir": JOURNAL_ROOT / "papers",
        "automation_ids": ["journal-author-codex-scheduler"],
        "primary_automation_id": "journal-author-codex-scheduler",
        "heartbeat_minutes": 5,
    },
    "submission": {
        "label": STAGE_LABELS["submission"],
        "root": SUBMISSION_ROOT,
        "state_path": SUBMISSION_ROOT / "controller" / "auto_submission_panel_state.json",
        "prompt_path": SUBMISSION_ROOT / "controller" / "AUTO_SUBMISSION_SCHEDULER_PROMPT.md",
        "papers_dir": SUBMISSION_ROOT,
        "automation_ids": ["auto-submission-codex-scheduler"],
        "primary_automation_id": "auto-submission-codex-scheduler",
        "heartbeat_minutes": 1,
    },
}
