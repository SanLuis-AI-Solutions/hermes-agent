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
   - Delta suppression: if nothing observable changed since the previous
     checkpoint, no write happens. A short clean run therefore writes exactly one
     closing checkpoint, not a stream.
2. Ownership guard is preserved, not weakened: every write is a single
   `BEGIN IMMEDIATE` transaction that CASes on
   `tasks.current_run_id = run_id AND tasks.claim_lock = claim_lock` and refuses
   to insert when the CAS fails. A second worker that does not hold the claim
   cannot write a checkpoint to the card.
3. Dispatcher exit path (`kanban_db_dispatch.enforce_max_runtime`): when a run is
   terminated for its cap and the card holds no checkpoint from that run, the
   dispatcher itself writes a `run_cutoff` record saying plainly that the run was
   cut off — an empty record is not an acceptable outcome of a kill.
4. Scorecard/diagnostics (`hermes_cli/kanban_diagnostics.py`): a run that ended
   non-done, ran longer than `SILENT_RUN_MIN_SECONDS` and produced zero
   checkpoints is surfaced as a `silent_run` defect instead of a quiet zero.

## Proof plan (compressed timing, stated as such)

Real 40-minute wait is not available inside a 2400 s card, so the harness runs the
**real** module against a **real** board DB with a 3 s interval and a 20 s cap:
`proof/over_cap_proof.py` spawns a child that checkpoints, SIGTERMs it at cap,
SIGKILLs if needed, then dumps the card's comments. A clean run of ~4 s shows one
closing checkpoint and no timer noise. Both digests and the raw card dump are
attached.
