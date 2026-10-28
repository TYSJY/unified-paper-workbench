# Unified Paper Orchestrator 0.9.0

## Smart Submission program control plane

- Added a deterministic Smart Submission state/action projection.
- Program logic now owns queue order, parallel-slot allocation, dispatch candidates, and executor selection.
- Added value-free account/site/origin binding before visible-worker dispatch.
- Added an isolated SQLite workflow ledger for planned Smart Submission actions.
- Kept visible Codex worker threads only as a compatibility bridge for desktop thread and unported site-page capabilities.
- Reduced the canonical Smart scheduler/bridge prompt from roughly 29.5K to 15.1K characters by removing AI-owned scheduling instructions.
- Final-submit approval, one-use claim, trusted confirmation capture, and unknown-result fail-closed behavior remain unchanged.

## UI and operations

- Smart Submission now displays program phase, current action, executor, candidate count, and model-free action count.
- Added “生成程序计划”, which writes the plan and ledger without starting a browser, model, or final submission.
- Added `smart-submission-program-plan` CLI with read-only/no-ledger options.

## Rollback

This release was developed on `codex/smart-deterministic-orchestration`. The complete pre-change source and EXE snapshot is stored outside the repository under:

`D:\Documents\UnifiedPaper_Rollback\smart_deterministic_20260716_233451`
