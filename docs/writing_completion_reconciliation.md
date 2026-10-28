# Writing completion reconciliation

The unified controller treats the writing scheduler state as a lagging
projection, not as proof that a worker step is complete.  Before unified sync,
scheduler status, master-heartbeat planning, and writing start/resume, it may
advance a writing session only when both of these independent checks pass:

1. The session's exact stored Codex thread has a latest terminal
   `task_complete` event whose final response contains one unambiguous matching
   `PIPELINE_ID=<paper_id>` and `STEP_DONE=<1..10>` marker pair.
2. The expected output contract for that step exists under the writing project,
   is non-empty, remains stable while read, and has a SHA-256 fingerprint.

`last_dispatched` and file existence by themselves are never completion
evidence.  A newer open thread turn, a mismatched paper marker, a malformed or
changing rollout, a missing artifact, or an unfingerprintable artifact leaves
the persistent state unchanged.

The rollout reader follows the observed Codex JSONL turn-boundary schema.  A
post-terminal `event_msg/task_started`, top-level `turn_context`,
`response_item/message` with `role=user`, or `event_msg/user_message` is enough
to classify the thread as `newer_turn_open`; it does not wait for the later
user-message record before failing closed.  A later `task_complete` replaces
that open-turn evidence only when it supplies a new terminal event.

On a verified incomplete step, a stale `running` session becomes `queued` for
the next step.  An explicit `paused` or `stopped` state is preserved.  Step 10,
or a reached explicit `run_until_step`, becomes `complete`.  The session stores
the terminal event identity and artifact fingerprints in
`completion_reconciliation`, making repeated refresh/resume calls idempotent.

## Concurrency boundary

Reconciliation is read-only unless every discovered primary and legacy writing
automation is `PAUSED`.  If either configured scheduler is `ACTIVE`, sync and
status reconciliation takes the `scheduler_active_no_write` path and leaves the
writing state byte-for-byte unchanged.  An existing automation with a missing
or unknown status similarly takes `scheduler_status_unreadable_no_write`.
Queue, start, and new-session mutations fail closed
until all writing automations are paused; after reconciliation they re-check
the pause condition immediately before mutating and only then enable the
primary scheduler.

While paused, each reconciliation attempt reads the exact state bytes and
their SHA-256, merges only verified target sessions into that newest object,
and preserves other sessions, settings, commands, and unknown fields.  The
replacement is attempted only if the source-byte digest still matches; a
conflict causes a fresh read-and-merge, for at most three attempts.

This is deliberately not described as a true cross-process CAS.  The writing
scheduler does not participate in the unified controller lock, and ordinary
filesystems do not provide a conditional `os.replace`.  A narrow race remains
between the final digest check and replacement, as does the possibility that a
scheduler turn already in flight writes after its automation is paused.  The
pause guard is the primary exclusion boundary; digest retries prevent detected
intervening writes from being overwritten but cannot make an uncooperative
writer transactional.

This controller contract does not modify the writing scheduler prompt or any
of the user's tuned writing, figure, journal-selection, or author prompts.
