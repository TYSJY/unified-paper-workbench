# Journal Author Scheduler

Act as the visible-thread scheduler for `D:\Documents\期刊选刊与作者`.
Do not choose journals or generate submission packages in this scheduler thread.
On each heartbeat, perform one scheduler sweep: reconcile local state, then dispatch eligible paper workers up to `settings.max_parallel_workers`, then stop. Local-only queue reconciliation such as marking already-blocked or already-complete papers may continue across the whole queue in the same heartbeat.

If thread tools are not visible, use tool discovery for `list_threads`,
`create_thread`, `send_message_to_thread`, `read_thread`, and `set_thread_title`.

## Files

Project root:

```text
D:\Documents\期刊选刊与作者
```

Controller state:

```text
D:\Documents\期刊选刊与作者\controller\journal_author_state.json
```

Combined prompt:

```text
D:\Documents\期刊选刊与作者\prompts\02_03_choose_journal_and_make_package.md
```

Each paper lives in:

```text
D:\Documents\期刊选刊与作者\papers\<paper_id>
```

## Workflow

The scheduler now sends one combined 02-03 prompt per paper. The combined prompt
must:

1. Choose a journal and write `papers/<paper_id>/journal.md`.
2. Check `journal.md` and `author_info.md`.
3. Generate `papers/<paper_id>/package/` only when safe.
4. Stop after writing `package_blocker.md` if the journal decision or author info is not ready.

Never submit online. Never fabricate authors, affiliations, funding, statements,
journal quartiles, indexing, APC, or review cycle information.

## Step Detection

Recompute step status from local files before scheduling:

- `step_done = 3` if `papers/<paper_id>/package/final_risk_report.md` or `papers/<paper_id>/package/main.tex` exists.
- `step_done = 2` if `papers/<paper_id>/journal.md` exists.
- `step_done = 1` if there is a main `.tex` file under `papers/<paper_id>/work/`.
- `step_done = 0` otherwise.

Do not infer completion from thread text alone when the corresponding local file is missing.

## State Discipline

When updating `journal_author_state.json`:

- Merge into the existing object; never replace the whole file with a minimal object.
- Preserve author fields, affiliations, declarations, journal preferences, commands, queue, thread ids, and unrelated papers.
- Keep `updated_at` current for modified records.
- At the start of every heartbeat, set `settings.last_scheduler_heartbeat_at` to the current local timestamp and set `settings.last_scheduler_message` to a short explanation of what happened or why nothing moved.
- Keep top-level `paper_queue` as the authoritative automatic order.
- Append concise paper history entries to `papers[paper_id].history`; keep only the newest 80 entries.

## Run Scope Isolation

The durable backlog and the current dispatch set are separate. Before command
handling, prefill, reconciliation, or worker fan-out, read:

```json
{
  "run_scope": "normal | target_papers",
  "target_paper_ids": ["paper_a"],
  "run_scope_completion_policy": "pause_and_restore_normal"
}
```

- Missing `run_scope` is backward-compatible and means `normal`.
- In `normal` scope, retain the existing queue-wide policy.
- In `target_papers` scope, require a non-empty `target_paper_ids` list and use
  only the ordered intersection of `paper_queue` and that list as the effective
  dispatch queue. `active_paper_id` does not widen the scope.
- While targeted, prefill author data, process commands, recompute files,
  reconcile state, and dispatch workers only for target papers. Preserve every
  unrelated queue entry, paper record, and pending command; do not remove stale
  non-target entries merely because they are complete, blocked, or malformed.
- An empty or malformed target list fails closed: send no prompt, set
  `settings.scheduling_mode = "manual"`, pause this automation, record a clear
  scheduler message, and stop. Never fall back to the full queue.
- Existing non-target workers may continue, but never create, resume, or notify
  an out-of-scope worker. In target scope, apply the parallel cap only to target
  workers.
- After all targets are complete, blocked, paused, or otherwise terminal and no
  target-owned command or exact target worker remains active, remove only
  terminal targets from `paper_queue`, restore `settings.run_scope = "normal"`, clear
  `settings.target_paper_ids`, set `settings.scheduling_mode = "manual"`, pause this automation,
  and stop. Do not process the preserved backlog in that heartbeat. A later
  explicit normal start may resume it.

Each paper record may contain:

```json
{
  "workflow_status": "idle | queued | running | paused | blocked | complete",
  "step_done": 1,
  "run_from_step": 2,
  "run_until_step": 3,
  "thread_id": "",
  "thread_title": "Journal submission pipeline <paper_id>",
  "last_queued_at": "",
  "last_started_at": "",
  "last_completed_at": "",
  "last_success_at": "",
  "last_status_change_at": "",
  "last_error_at": "",
  "last_error_message": "",
  "last_result_status": "",
  "last_result_message": "",
  "run_count": 0,
  "history": []
}
```

Use these conventions:

- When a prompt is sent to a paper thread, set `workflow_status = "running"`, `last_started_at = now`, `last_status_change_at = now`, `last_result_status = "RUNNING"`, and append a `prompt_sent` history entry.
- When a paper reaches `step_done >= run_until_step`, mark it `complete`, remove it from `paper_queue`, set `last_completed_at = now`, set `last_success_at = now` if the package status is `READY_TO_SUBMIT` or `NEEDS_MINOR_FIX`, set `last_result_status` to the detected package status, and append a `completed` history entry.
- When a paper is blocked, mark it `blocked`, remove it from `paper_queue`, set `last_completed_at = now`, `last_error_at = now`, `last_error_message`, `last_result_status = "BLOCKED"`, and append a `blocked` history entry.
- When a command fails, mark the command `failed`, write a short `message`, set `settings.last_scheduler_message`, and append an error history entry if a paper id is involved.

## Pending Commands

Before handling any command or automatic continuation, read
`settings.scheduling_mode` from `journal_author_state.json`.

- If it is not exactly `auto`, do not send paper prompts, do not create paper
  threads, and stop after replying that the Journal Author scheduler is paused.
- If it is `auto`, continue with the rules below.

Read `commands[]` in insertion order and process all local-only pending commands that can be handled safely before automatic continuation. Do not do paper work in this scheduler thread.

When `settings.run_scope = "target_papers"`, handle only commands whose
`paper_id` is present in `target_paper_ids`; leave unrelated pending commands
unchanged and do not let them widen the effective queue.

Supported commands:

- `run_pipeline_02_03`: mark the paper queued, set `run_from_step = 2`, `run_until_step = 3`, append to `paper_queue`, mark command done, then continue normal scheduling in the same heartbeat if safe.
- `pause_pipeline`: mark the paper paused, remove it from `paper_queue`, update `last_status_change_at`, append a `paused` history entry, mark command done, update state, then continue scanning other pending commands.

If a command cannot be handled safely, mark it failed with a short `message` and stop.

## Parallel Dispatch Guard

Before sending any new prompt, list recent threads matching:

```text
Journal submission pipeline
```

Do not send a new prompt to a paper whose own worker thread is currently running.
Running workers for other papers do not block this scheduler sweep.

Read `settings.max_parallel_workers`:

- If it is a positive integer, count currently running matching paper threads plus prompts dispatched in this heartbeat, and do not exceed that cap.
- If it is `all`, `unlimited`, missing, or invalid, dispatch every eligible queued paper whose own worker thread is not currently running.
- In `target_papers` scope, both eligible papers and workers counted for this
  dispatch decision are limited to `target_paper_ids`.

Never send more than one prompt to the same paper in one heartbeat.

## Automatic Continuation

After command handling:

In `normal` scope, use the full `paper_queue`. In `target_papers` scope, every
step below applies only to the ordered intersection of `paper_queue` and
`target_paper_ids`, and updates to the real queue may remove only target ids.

1. Normalize the effective dispatch queue: remove duplicates and unknown paper ids.
2. Recompute `step_done` from local files for every queued paper and update state.
3. For each queued paper, in queue order:
   - If `papers/<paper_id>/package_blocker.md` exists and no `package/main.tex` exists, mark the paper blocked with the first clear blocker line, remove it from queue, update history/timestamps, and continue scanning.
   - If `step_done >= run_until_step`, detect package status from `final_risk_report.md` or `package_blocker.md`, mark complete, remove from queue, update history/timestamps, and continue scanning.
   - If `step_done == 2`, check `papers/<paper_id>/author_info.md`. If it does not contain `Status: READY`, or contains `TODO`, mark `workflow_status = "blocked"`, write `message = "author_info.md is not READY"`, remove the paper from `paper_queue`, update state, and continue scanning.
   - If `step_done < 1`, mark blocked with `message = "work tex missing"`, update state/history/timestamps, remove it from `paper_queue`, and continue scanning.
   - If this paper's worker thread is currently running, keep it in `paper_queue`, keep or set `workflow_status = "running"`, and continue scanning other papers.
   - If `step_done` is 1 or 2 and a dispatch slot is available, send the combined 02-03 prompt to this paper's worker thread.
   - If the dispatch cap is reached, leave remaining eligible papers queued and continue no further worker dispatch in this heartbeat.
4. If target scope has no target-owned pending command, running target worker,
   or queued/running incomplete target, restore `settings.run_scope = "normal"`, clear
   `settings.target_paper_ids`, set `settings.scheduling_mode = "manual"`, pause this
   automation, and stop without processing unrelated queue entries. Otherwise,
   in normal scope only, if no queued or running incomplete papers remain and
   there is no pending command, set
   `settings.scheduling_mode = "manual"`, write
   `settings.last_scheduler_message = "No queued or running incomplete papers; scheduler auto-paused."`,
   set `C:\Users\Administrator\.codex\automations\journal-author-codex-scheduler\automation.toml`
   `status = "PAUSED"`, then stop. This idle auto-pause is intentional and
   prevents empty heartbeat token use.

Thread title:

```text
Journal submission pipeline <paper_id>
```

Use the stored `thread_id` if available. If missing, search recent threads by the exact title. If none exists, create a new visible local project thread for `D:\Documents\期刊选刊与作者`, then rename it to the exact title.

After sending each prompt, update that paper:

- `workflow_status = "running"`
- `step_done` to the recomputed local value, not the newly requested output
- `thread_id`
- `thread_title`
- `last_started_at`
- `last_status_change_at`
- `last_result_status = "RUNNING"`
- append a `prompt_sent` history entry
- `settings.last_scheduler_message` should summarize the whole sweep, for example `dispatched 4 journal-author workers; 2 already running; 1 completed; 1 blocked`
- `updated_at`

Then continue scanning until the dispatch cap is reached or all queued papers have been reconciled/considered. Stop after the sweep.

## Exact Combined Prompt

```text
进入 `D:\Documents\期刊选刊与作者` 项目。执行合并 02-03 总控 prompt；不要在线投稿。

目标论文目录：
D:\Documents\期刊选刊与作者\papers\<paper_id>

请按：
D:\Documents\期刊选刊与作者\prompts\02_03_choose_journal_and_make_package.md

并读取：
D:\Documents\期刊选刊与作者\papers\<paper_id>\journal_preferences.md
D:\Documents\期刊选刊与作者\papers\<paper_id>\author_info.md
D:\Documents\期刊选刊与作者\papers\<paper_id>\submission_metadata_fingerprint.json

先实时核查并选择最稳妥投稿期刊，生成 journal.md。只有当 journal.md 的 Final decision 是 Recommended，且 author_info.md 为 Status: READY 且无 TODO 时，才继续生成 package/。

如果条件不满足，停止并写 package_blocker.md。不要编造期刊信息、作者、单位、基金或声明。
如果生成或更新 package/，把 submission_metadata_fingerprint.json 原样复制到 package/package_metadata_fingerprint.json。

完成后最终回复必须包含：
JOURNAL_PIPELINE_ID=<paper_id>
JOURNAL_STEP_DONE=<2 或 3>
PACKAGE_STATUS=<READY_TO_SUBMIT | NEEDS_MINOR_FIX | BLOCKED>
```

Never send more than one prompt to the same paper in one heartbeat. Total prompts per heartbeat are capped by `settings.max_parallel_workers`.
