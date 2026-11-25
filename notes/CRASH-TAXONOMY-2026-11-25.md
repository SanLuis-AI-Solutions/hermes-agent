# Crash taxonomy — the 67 crashed / timed-out runs of the last 30 days

**Board:** `phase1d-canary` · **Window:** `2026-10-26T19:16Z` → `2026-11-25T19:16Z` (30 days from `started_at`)
**Source:** `C:\Users\ninef\AppData\Local\hermes\kanban\boards\phase1d-canary\kanban.db`, opened `mode=ro`
**Installed head at time of writing:** `3a9ecbb7e05`

## 1. The population, and a correction to the card

The card body says 35 crashed + 29 timed out (64). The live table says **37 crashed + 30 timed out = 67**.
The 67 holds both with `started_at >= now-30d` and with `started_at >= <card created_at> - 30d`, so the
card's 64 is not a window artefact — it is three runs short of the table. Counts below use 67.

Full census over the same window, for the denominator:

| outcome | n |
|---|---|
| completed | 147 |
| crashed | 37 |
| timed_out | 30 |
| blocked | 30 |
| review_requested | 14 |
| reclaimed | 8 |
| scheduled | 5 |
| changes_requested | 3 |
| spawn_failed | 1 |
| gave_up | 1 |
| NULL (still running) | 2 |
| **total** | **278** |

67 / 278 = **24.1%** of runs in the window ended crashed or timed out.

### SQL

```sql
SELECT r.id, r.task_id, r.profile, r.outcome, r.started_at, r.ended_at,
       r.max_runtime_seconds, r.summary, r.metadata, r.error,
       t.title, t.max_runtime_seconds AS task_max_runtime, length(t.body) AS body_len
FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id
WHERE r.outcome IN ('crashed','timed_out')
  AND r.started_at >= 1793042190          -- 2026-10-26T19:16:30Z
ORDER BY r.started_at;
```

Row count: **67**. Every run is in exactly one bucket below; the buckets sum to 67 with nothing in "other".

## 2. Buckets

| bucket | runs | share | already fixed in the installed tree? |
|---|---|---|---|
| **T1 — runtime cap hit** (`elapsed Ns > limit caps`) | **30** | 44.8% | no |
| **C4 — owner admission never settled** (`Kanban execution unknown; retained by owner`) | 15 | 22.4% | yes — three commits, all 2026-11-24, all after the last such run |
| **C2 — gateway `draining: update_paused`** (source update in flight) | 12 | 17.9% | no commit; one 18-minute incident |
| **C1 — session-store save failure** (`Hermes couldn't save this conversation`) | 7 | 10.4% | **no** — a fix exists on a branch, not in the installed head (§4) |
| **C5 — tool-guard refusal → managed worker lost, no error text** | 2 | 3.0% | no |
| **C3 — gateway unreachable at submit** (`Gateway connection failed; no local fallback`) | 1 | 1.5% | no commit; single 2026-10-27 event |
| **total** | **67** | 100% | |

No run is "unknown-with-no-error-text" at the top level: the 15 C4 and 2 C5 runs carry a quoted line, and
where that line is not causal it is said so below.

### Incident view (same data, deduplicated by burst)

| bucket | runs | distinct incidents | incident windows (UTC) |
|---|---|---|---|
| C4 | 15 | 4 | 11-14 23:15–11-15 02:53 (11); 11-18 17:29 (1); 11-24 14:37 (2); … |
| C2 | 12 | 1 | 11-11 18:05–18:22 |
| C1 | 7 | 3 | 10-27 17:14 (1); 11-14 23:58–11-15 00:44 (5); 11-24 14:37 (1) |
| C5 | 2 | 1 | 11-25 19:08–19:10 (one card, retried) |

The 11-24 14:37 burst is one event: three profiles (auditor `t_a704bddb`, claudebot `t_16c818f0`,
finance `t_e17dd9af`) died inside the same minute, and each carries the session-store message. Read the
counts as runs, not as incidents — the distinction changes C2 (12 runs, one source update) from a
recurring cause into a one-off.

## 3. Evidence per bucket

### T1 — runtime cap hit — 30 runs (11-15 03:15 → 11-25 18:38)

Most recent ids: **1037, 1019, 1007, 1006**. Quoted, verbatim:

```
elapsed 1881s > limit 1800s          (run 1037, task t_774db51f, profile codingboss)
```

The kill is **late on every run**: over-cap delta min 5 s, **median 45.5 s**, max 82 s (n=30). Nothing in
the table suggests the timer fires early.

### C4 — owner admission never settled — 15 runs (11-14 23:15 → 11-24 14:37)

Most recent ids: **954, 953, 925, 877**. Quoted:

```
pid 101432 exited with code 1 [managed worker lost: managed_worker_failed] Worker's last output:
'Session: local-… Kanban owner refused: Kanban execution unknown; retained by owner'   (run 954)
```

In 15 of these the receipt never reached `terminal` and `hermes_cli/kanban_worker_client.py:180` raised
`GatewayClientError('Kanban execution unknown; retained by owner')`; the `worker_result` event of these
runs has an empty `last_output`, i.e. no other error text exists.

### C2 — gateway draining for a source update — 12 runs (11-11 18:05 → 18:22)

Most recent ids: **816, 815, 814, 813**. Quoted:

```
hermes: source-update completion failed: an update is still running; wait for it to exit, then relaunch
Hermes; running with the previous dependencies — run `hermes update` to finish it
Kanban owner refused: Kanban owner silent for 190s: Gateway draining: update_paused   (run 816)
```

One source update, 12 runs across 4 profiles (qa, worker), 17 minutes. Also visible in the log: `dependency
lock still held after 10.0s; continuing without it (…\installs\fc33b49b5924bb4e\.install.lock)`.

### C1 — session-store save failure — 7 runs (10-27 17:14 → 11-24 14:37)

Most recent ids: **955, 865, 856, 855**. Quoted from the `worker_result` event:

```
⚠️ No reply: Hermes couldn't save this conversation, so it stopped rather than lose your messages.
Possible causes: the drive is out of room, or another Hermes process is holding the database.
Close other Hermes windows, run `hermes -p finance doctor` to check storage, then send your message again.
```

The message is the `default` variant of `locales/en.yaml:3716` — the *disk* variant (`:3715`) was not the one
raised, so "disk full" is **not** established by this text; the store itself refused the write. Causal
order verified: the session ends without a reply → the receipt never settles → the wrapper raises the
`execution unknown` error above. C1 is therefore the parent cause of 7 of the 15 C4-shaped crashes.

### C5 — tool-guard refusal → managed worker lost — 2 runs (both 2026-11-25 19:08/19:10, one card)

Ids: **1039, 1041** (task `t_9be7c06e`, twice). Quoted:

```
[write_file] Refusing to overwrite
`C:/Users/ninef/AppData/Local/hermes/profiles/lunabot/cache/scratch/retry_history_replay.py`
```

Measured, not assumed: this is the discriminating signature the operator asked for (`managed worker lost:
managed_worker_failed` together with a `Refusing to` line), and it holds for **2 of 37 crashes (5.4%)** —
one card retried identically, not a fleet-wide class. Correlation only: both runs' `worker_result`
`last_output` is **empty** (exit_code 1, `managed_worker_failed`), so the refusal is the last *tool* output
and not a recorded error; the process died without a final reply. It is a real class with a real remedy
(surface the refusal to the worker instead of ending the run), but at 2 runs it is not the biggest cause.

### C3 — gateway unreachable at submit — 1 run (2026-10-27 17:14)

Id: **767**. Quoted:

```
Session: local-… Kanban owner refused: Gateway connection failed; no local fallback
```

## 4. Already fixed — and one that only looks fixed

| bucket | verdict | evidence (commit subject) |
|---|---|---|
| **C4 (15 runs)** | **already fixed in the installed tree** | `4307dda710d fix(kanban): an unknown admission costs the worker one redial, not the attempt` (2026-11-24T11:14-06:00 = 17:14Z), `6b0aa91a7d2 Re-admit an unknown admission when the worker that owned it is gone` (17:53Z), `23249496105 Pin the worker witness the reservation writes, so the readmit has a durable fact to read` (18:04Z). The last C4 run started **2026-11-24T14:37Z**, i.e. before the first of the three. |
| C1 (7 runs) | **not fixed in the installed tree** | `d494af3be1f fix(persistence): v34 leaves compression-closed rows alone and lets a locked store retry` is dated 2026-11-24T10:33Z (earlier than the last C1 run) **but is not an ancestor of the installed head**: `git merge-base --is-ancestor d494af3be1f HEAD` → not in head; `git branch -a --contains d494af3be1f` → `remotes/origin/lanec/resume-preserve-1124` only. |
| T1, C2, C3, C5 | not fixed | no commit names them; `cd67c10c873 feat(kanban): runtime run checkpoint written by the worker client` and `0dc8bcfd3fd proof(kanban): over-cap hard-kill leaves checkpoints and a named artifact` landed 2026-11-24, after 28 of the 30 timeouts; whether they change the outcome of an over-cap kill is **UNKNOWN** from the board (no post-fix timed-out run's checkpoint was located in this pass). |

Nothing in the installed head claims the timeout path, the update-drain path, or the tool-guard path.

## 5. Timeouts vs the card-shape rules

The card asks whether the timeouts are cards too big for their budget. **They are not.**

- **Size:** timed-out tasks have median body 2,724 chars (n=30, max 5,208); completed runs in the same
  window have median 2,534 (n=147, max 12,402). The biggest cards in the window completed.
- **Fit:** 27 distinct tasks timed out. **17 later completed** (63%). Of the 12 that needed a real worker
  retry, every one finished *under the same cap*: 713, 744, 894, 1,030, 1,085, 1,123, 1,180, 1,434, 1,471,
  1,602, 1,635 s (cap 1,800 s). The work fits the budget; the first attempt spent the budget without
  finishing.
- **No goal-loop inflation:** 0 of 30 timed-out runs had `goal_mode` set.
- **Repeat offenders are rare:** 2 tasks timed out twice (`t_a172eb45` 1808 s then 1862 s; `t_0bf80192`
  1853 s then 1808 s); 1 timed out at a 120 s cap after 151 s (`t_5054660a`, a 672-char card).

So the fix for 30 runs is **not** "bigger caps" (the same cards complete inside the same cap) and **not**
"smaller cards". It is what happens to an attempt that runs out of wall clock.

## 6. The biggest unfixed cause

**Cap exhaustion — 30 runs, 44.8% of every crashed or timed-out run in the window.** It is not the loudest
failure (the owner-refusal family is louder per incident and is already fixed) and it is not card shape
(§5): the same card, retried, finishes in 713–1,635 s against the same 1,800 s cap, so the budget is
adequate and the *attempt* is what fails. The smallest change that addresses it is at the kill, not in the
budget: make the over-cap kill preserve the attempt so the next run resumes instead of restarting — the
checkpoint writer exists (`cd67c10c873`) and the kill currently lands a median 45.5 s late, so a checkpoint
written at the cap is both reachable and cheap. Estimated effect: at most **30 runs/month**, realistically
**≈15** (the 12 retried runs show that half the exhausted attempts are one restart away from finishing),
i.e. runs ending without delivery fall from 67 to ≈52 a month (24.1% → ≈18.7% of ~278 runs).

The second unfixed cause is worth naming because its fix already exists: **C1, 7 runs**, the session store
refusing the write. `d494af3be1f` is on `lanec/resume-preserve-1124` and not in the installed tree; landing
it is a merge, not a new change. The tool-guard class the operator flagged (C5) is real but measures **2
runs on 1 card (3.0%)**, so on this board it is not the biggest unfixed cause.

## 7. Acceptance checklist

1. SQL and row count: §1 — **67** rows returned, equal to the 37 + 30 reported.
2. Bucket table: §2 — 30 + 15 + 12 + 7 + 2 + 1 = **67**.
3. Three quoted errors proving the top bucket (verbatim from `task_runs.error`): run **1037**
   `elapsed 1881s > limit 1800s`; run **981** `elapsed 1805s > limit 1800s`; run **917**
   `elapsed 1861s > limit 1800s`. Plus the over-cap distribution (min 5, median 45.5, max 82, n=30) in §5.
4. Committed report path and sha: this file, `notes/CRASH-TAXONOMY-2026-11-25.md`, docs-only branch
   `notes/crash-taxonomy-2026-11-25`, PR opened, **not merged**.
5. Already fixed: **C4 only** (15 runs), commits `4307dda710d`, `6b0aa91a7d2`, `23249496105`; C1's fix
   `d494af3be1f` is explicitly **not** in the installed head.
