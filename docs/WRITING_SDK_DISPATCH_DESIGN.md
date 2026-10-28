# Local Codex SDK writing dispatcher

## Status and boundary

`unified_paper_orchestrator.writing_sdk_dispatch` remains an isolated execution engine: it owns only its private crash journal and never activates the old Automation. The separate `writing_sdk_runtime` and `WritingAdapter` now provide the live, locked `pipeline_state.json` bridge and keep that Automation paused as a compatibility fallback.

The engine is written against the installed `openai-codex==0.1.0b3` API:

- `AsyncCodex.thread_start()` creates the first thread for a paper.
- `AsyncCodex.thread_resume()` resumes that same journaled thread for later steps.
- `AsyncThread.turn()` starts exactly one turn for one step.
- `AsyncTurnHandle.run()` waits on the routed `turn/completed` event. There is no timer or minute polling loop.

## Contracts

Each `WritingPaperPlan` has a stable `paper_key`. Step 1 may discover the final `PIPELINE_ID`; after that marker is validated, the journal binds the key, pipeline id, and one Codex thread id. The binding cannot be changed by a later plan.

An existing Desktop/Automation paper can cross the execution boundary once with `migration_new_thread=True`. This is accepted only when `completed_step > 0`, `paper_id` is present, and `thread_id` is empty. The SDK dispatcher creates and journals one thread, immediately clears the migration flag in its journal, and resumes only that id afterward. The default remains `False`, so missing resume bindings fail closed.

The prompt provider returns `ExactWritingPrompt(text, sha256)`. The dispatcher validates the SHA-256 of the exact UTF-8 string and passes `text` directly to `AsyncThread.turn()` without any further prefix, suffix, or normalization. `existing_writing_prompt_provider()` is the production adapter: it reads the existing exact step template and wraps it with the existing `_ticket_prompt` control envelope, using a deterministic attempt id so the complete envelope hash is stable after a crash. It never writes the scheduler prompt. A journaled step also rejects a later provider value with different bytes.

Before a turn, the dispatcher fingerprints the input contract. After a completed event, it requires:

1. SDK turn status `completed`;
2. exactly one `PIPELINE_ID` and one `STEP_DONE` terminal marker;
3. the expected paper and step;
4. unchanged input fingerprints; and
5. every artifact in `writing_completion.expected_step_artifacts()`.

Step 3 and Step 6 retain the existing novelty and evidence gates. The SDK thread uses `Sandbox.workspace_write` and `ApprovalMode.deny_all`, so an unattended run cannot pause on an approval request.

## Determinism and concurrency

One process holds the journal file lock for the complete `run_until_idle()` call. In-process ownership also prevents two dispatcher objects from owning the same lock path. Plans are built in case-insensitive deterministic order. Each paper is sequential while different papers use `asyncio` concurrency. A dedicated Step 1 lock serializes all new-paper turns to protect the daily paper-number allocator; an existing paper's Step 2-10 turn does not take that lock and may overlap Step 1. A Step 1 waiting for this lock does not consume a general execution slot, so multiple queued new papers cannot starve an eligible later step.

`SDKGlobalPermitProvider` pages through `thread_list()` with every `ThreadSourceKind`, including all sub-agent variants. Every SDK-visible active thread consumes one of at most ten permits. V2 disables both `multi_agent` and `multi_agent_v2`; every Step 1-10 turn therefore reserves weight 1 for its single fixed root conversation. The bundled CLI requires `agents.max_threads >= 1` even while multi-agent is disabled, so that validation field remains one and does not represent an extra writing session. Any externally visible active thread still consumes a global slot. An inspection error fails closed and starts no turn.

This is a deterministic bound for the one locked writing dispatcher and its one isolated app-server process. The SDK can observe and limit child threads inside that process, but `thread/list()` reports a thread loaded by another app-server process as `notLoaded`, not `active`, and the Python SDK exposes no cross-process atomic global semaphore. An unrelated launcher using the same account therefore remains outside this proof; a machine- or account-wide hard guarantee requires one shared permit authority used by every launcher.

## Live integration callbacks

`progress_callback(event, paper_snapshot)` may be synchronous or asynchronous. It runs after `thread_created`, `step_completed`, `paper_blocked`, and `paper_paused`. The snapshot is a deep copy of the journal paper record. A callback exception is recorded as `WRITING_PROGRESS_CALLBACK_FAILED`, leaves the paper blocked, and prevents later dispatcher invocations from advancing it until the integration failure is explicitly reconciled.

`should_continue(next_step, paper_snapshot)` may also be synchronous or asynchronous. It is checked immediately before each new step. A false result writes `status=paused` and `paused_before_step=<next_step>` and returns normally without marking a blocker. This lets an EXE pause request take effect after the current event-driven turn completes. An exception fails closed as `WRITING_SHOULD_CONTINUE_FAILED`.

## Crash journal recovery

| Journal point | Recovery action |
|---|---|
| Thread has a stored id | Resume only that id. |
| Thread creation intent has an exact deterministic SDK name | Adopt the single exact-name match. |
| Creation intent has no exact-name match | Block; never create a possibly duplicate thread. |
| Exactly one new completed turn exists | Validate and commit it without sending again. |
| Exactly one new active turn exists | Return `waiting_event`; an external event should invoke the dispatcher again. |
| Step was `starting` or explicitly send-uncertain, and no turn is visible | Block; the journal/RPC boundary is not atomic, so never retry automatically. |
| Multiple candidate turns exist | Block as ambiguous. |

The SDK has no idempotency key for `thread/start`. A process can die after the server creates a thread but before `set_name()` succeeds. The journal deliberately blocks this case instead of risking a second paper thread; an operator or future SDK idempotency feature is required to resolve it.

## Live runtime integration

`unified_paper_orchestrator.writing_sdk_runtime` now provides the reviewed live bridge. It performs account preflight before queue mutation, requires the old writing Automation to remain paused, translates pending `new_session` commands and queued/running sessions into deterministic plans, and merges dispatcher events back into `pipeline_state.json` under the same cross-process lock used by `WritingAdapter`. Every write uses byte-conditional atomic replacement and rechecks the legacy Automation before commit.

The runtime uses an application-owned `CODEX_HOME` at `state/codex_sdk_home`, so SDK authentication does not overwrite Codex Desktop authentication. `WritingAdapter` wakes one process-wide background runtime; a second EXE process is rejected by the dispatcher journal lock. Existing Desktop papers that already completed Step 1 retain their old id in `legacy_thread_id` and receive exactly one new isolated SDK thread for Step 2 onward. New papers use one fixed SDK thread from Step 1 through Step 10. A new-paper command arriving during an older all-queue pass sets an in-process selection request: active turns finish normally, old papers pause at the next Step boundary, and the next pass runs only the exact new batch. Old work resumes only through the explicit all-queue action, without an AI scheduler.

The UI exposes browser login under “AI执行与API → Codex 账号模式”. A missing account is a strict no-write failure. Pause changes the pipeline to manual immediately; an active turn may finish, then `should_continue` emits `paper_paused` before any next step starts.

The isolated tests cover Steps 1-10 on one fixed thread, exact envelope/template hashing, serialized Step 1 with concurrent Step 2, global pagination and one-slot lifecycle reservations, new-paper boundary priority, marker/artifact failures, duplicate pipeline ids, crash uncertainty, explicit migration, progress callback failures, and graceful `should_continue` pauses:

```powershell
python -m pytest -q tests\test_writing_sdk_dispatch.py tests\test_writing_sdk_runtime.py
```
