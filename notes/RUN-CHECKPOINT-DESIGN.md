# Run checkpoint at the runtime layer — design note

Card: t_9f2487ef. Target repo: `C:/Users/ninef/AppData/Local/hermes/hermes-agent`
(read-only live tree; this card works in a detached worktree at
`.../workspaces/t_9f2487ef/wt`, base HEAD `ae2a81ccc00`).

## The failure being repaired

Run 957: 41m19s against a 2400 s cap, killed, card left with no comment, no
artifact, no commit. The profile carried prose telling the model to checkpoint
within ten minutes; two runs, two violations. Prose is not a mechanism.

## Mechanism (no model turn, no model permission)

1. `hermes_cli/kanban_checkpoint.py` — a checkpoint writer that runs **inside the
   worker client process** (`hermes_cli/kanban_worker_client.py`), which is the
   process the dispatcher spawns, fingerprints, SIGTERMs and SIGKILLs.
   - A timer task writes a compact checkpoint comment every
     `CHECKPOINT_INTERVAL_S` (600 s default; `HERMES_KANBAN_CHECKPOINT_INTERVAL_S`
     override for the proof harness only) while the run is in flight.
   - The exit hook writes one final checkpoint on the paths where the process
     still holds the attempt's claim: SIGTERM (cap reached), exception,
     CancelledError, a lost owner. It does **not** land on a run that ended by
     its own handoff: `kanban_complete`/`kanban_request_review` execute inside
     the owner's turn and clear `claim_lock` *and* `current_run_id` before the
     client exits, so the ownership CAS refuses the closing write (live
     ordering on this card's own run 971: `review_requested` at 1795538175,
     `worker_result` at 1795538214). A clean run shorter than the interval
     therefore leaves **zero** checkpoints, and a longer clean run's record ends
     with its last timer line (`in flight`); the card's `completed` /
     `review_requested` event carries how it ended. The alternative — writing a
     closing line after the row was settled — would need a new ownership proof
     and was rejected rather than weakening the guard.
   - The sequence number is seeded from the checkpoints this attempt already
     left (`prior_checkpoints` in `_board_facts`), and a lock serialises the
     timer thread and the SIGTERM hook. The hook builds its own writer instance,
     and without the seed a cap-killed card read `seq 1,2,3,1` — the last line
     numbered below the first. It now continues: two timer lines and the SIGTERM
     line read `1,2,3`.
   - The body is derived mechanically, never from the model: elapsed time, phase,
     heartbeat count and last heartbeat note from the board, workspace `git`
     HEAD/subject + changed-file count + newest changed path, artifact paths that
     exist in the workspace, and any commit the run produced.
   - Bounding instead of delta suppression: timer checkpoints stop at
     `MAX_TIMER_CHECKPOINTS` (12), so a runaway run gets a periodic record without
     papering the card, while the exit checkpoint (on the claim-holding paths) is
     never bounded. A short clean run ends before the 600 s first tick and writes
     nothing. (An earlier draft claimed "no write when nothing changed"; that
     was not implemented — a delta needs a prior snapshot, and the bound gives the
     same clean record with less state.)
2. Ownership guard is preserved, not weakened: every write is a single
   `BEGIN IMMEDIATE` transaction that CASes on
   `tasks.current_run_id = run_id AND tasks.claim_lock = claim_lock` and refuses
   to insert when the CAS fails. A second worker that does not hold the claim
   cannot write a checkpoint to the card.
3. Dispatcher exit path (`kanban_db_dispatch.enforce_max_runtime`): when a run is
   terminated for its cap and the card holds no checkpoint from that run, the
   dispatcher itself writes a `run_cutoff` record saying plainly that the run was
   cut off — an empty record is not an acceptable outcome of a kill. Idempotent:
   a retried tick does not stack duplicate `run_cutoff` lines.
4. Scorecard/diagnostics (`hermes_cli/kanban_diagnostics.py`): the `silent_run`
   rule surfaces a run that ended non-done, ran longer than
   `silent_run_min_seconds` (600 s) and produced zero checkpoints as a `warning`
   diagnostic — "Run N ran 41m and wrote nothing to its card". Silence is the
   defect, not a quiet zero. No checkpoint at all is therefore visible on the
   board, not only in a database column.

## Proof plan (compressed timing, stated as such)

Real 40-minute wait is not available inside a 2400 s card, so the harness runs the
**real** module against a **real** board DB with a 2 s interval and an 8 s cap:
`proof/over_cap_proof.py` spawns a child that checkpoints, hard-kills it
(`TerminateProcess` — no catchable handler) at the cap, then dumps the card's
comments. Scenario B models the **real** exit ordering: the child settles its own
card exactly as `kanban_request_review` does (status `review`, `claim_lock` and
`current_run_id` cleared) and then reaches its closing checkpoint, which must be
refused — a clean run shorter than the interval leaves zero checkpoints, which is
the cleanest possible record. Scenario B2 pins the long-clean case: timer lines
stand, and no line says `finished`, because the settled card never reaches a state
the closing line could claim. Scenario C covers the dispatcher backstop and the
ownership CAS. `proof/proof-run.log` is the raw output of the last run, with the
subject module's sha256 printed at the top.

Tests: `tests/hermes_cli/test_kanban_run_checkpoint.py` — 14 behaviour tests over
a real board DB (timer body, stale claim lock, superseded run, bounded timer,
cutoff states, idempotent backstop, `silent_run` scoring, monotonic `seq` across
timer→SIGTERM, and the settled-card exit ordering).
