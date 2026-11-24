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
   - The exit hook writes one final checkpoint on every exit path: normal exit,
     exception, SIGTERM (cap reached), CancelledError.
   - The body is derived mechanically, never from the model: elapsed time, phase,
     heartbeat count and last heartbeat note from the board, workspace `git`
     HEAD/subject + changed-file count + newest changed path, artifact paths that
     exist in the workspace, and any commit the run produced.
   - Bounding instead of delta suppression: timer checkpoints stop at
     `MAX_TIMER_CHECKPOINTS` (12), so a runaway run gets a periodic record without
     papering the card, while the exit checkpoint always writes. A short clean run
     (ends before the 600 s first tick) therefore writes exactly one closing
     checkpoint. (An earlier draft claimed "no write when nothing changed"; that
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
comments. A clean run of ~2 s with a 30 s interval shows one closing checkpoint
and no timer noise. Scenario C covers the dispatcher backstop and the ownership
CAS. `proof/proof-run.log` is the raw output of the last run, with the subject
module's sha256 printed at the top.

Tests: `tests/hermes_cli/test_kanban_run_checkpoint.py` — 11 behaviour tests over
a real board DB (timer body, stale claim lock, superseded run, bounded timer,
cutoff states, idempotent backstop, `silent_run` scoring).
