"""Proof for card t_9f2487ef: a capped run leaves reviewable work, a clean run stays clean.

Runs the REAL ``hermes_cli.kanban_checkpoint`` module against a REAL board SQLite DB (built by
``kanban_db_connect.init_db``, same schema the dispatcher uses). Timing is compressed and the
compression is stated in the output: the card cannot spend 40 real minutes, so the interval is
2 s and the cap is 8 s. What is NOT compressed is the mechanism — the same timer task, the same
ownership CAS, the same hard kill.

Scenario A (over cap, hard kill): child checkpoints every 2 s, parent SIGKILLs it at the cap the
way a Windows dispatcher does (TerminateProcess, no catchable handler). The card must already
carry checkpoints and a named artifact.
Scenario B (clean): child runs 3 s and exits normally; exactly one closing checkpoint, no noise.
Scenario C (dispatcher backstop): a capped run with zero checkpoints gets a 'cut off' line from
``record_cutoff``, and a run that already checkpointed gets none.

Usage: python proof/over_cap_proof.py   (also `--worker <db>/<task>/<run>/<lock>` for the child)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def fixture(tmp: Path, name: str, *, lock: str = "host:0") -> tuple[str, str, str, str]:
    from hermes_cli import kanban_db_connect
    db = tmp / f"{name}.db"
    kanban_db_connect.init_db(db)
    conn = kanban_db_connect.connect(db)
    try:
        with kanban_db_connect.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks (id, title, body, status, assignee, created_at, started_at,"
                " claim_lock, max_runtime_seconds, current_run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, f"proof {name}", "", "running", "codingboss",
                 int(time.time()), int(time.time()), lock, 8, 1))
            conn.execute(
                "INSERT INTO task_runs (id, task_id, profile, status, started_at)"
                " VALUES (1, ?, 'codingboss', 'running', ?)", (name, int(time.time())))
    finally:
        conn.close()
    return str(db), name, "1", lock


WORKER = """
import asyncio, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from hermes_cli.kanban_checkpoint import RunCheckpoint
cp = RunCheckpoint(task_id={task!r}, run_id=1, claim_lock={lock!r}, board_db={db!r},
                   workspace={ws!r})
# A heartbeat the way a model's kanban_heartbeat writes one, so the snapshot has board facts.
conn = sqlite3.connect({db!r}, timeout=5.0, isolation_level=None)
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
             " VALUES (?,1,'heartbeat',?,?)", ({task!r}, '{{"note": "training epoch 3/50"}}',
                                               int(time.time())))
conn.execute("COMMIT")
conn.close()
open({ready!r}, "w").close()
cp.started = time.time()   # the run's clock starts when work starts


async def main():
    ticker = asyncio.create_task(cp.timer_loop({interval!r}))   # the real timer task
    for i in range(1, {ticks}):
        with open({artifact!r}, "a") as fh:
            fh.write(f"tick {{i}}\\n")
        await asyncio.sleep(1.0)
    ticker.cancel()
    cp.finish("exit")

asyncio.run(main())
"""


def run_worker(db, task, lock, ws, artifact, ticks, interval=2.0):
    ready = Path(ws) / "ready.txt"
    code = WORKER.format(repo=str(REPO), db=db, task=task, lock=lock, ws=str(ws),
                         artifact=str(artifact), ticks=ticks, interval=interval, ready=str(ready))
    script = Path(ws) / "_worker.py"
    script.write_text(code, encoding="utf-8")
    env = dict(os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    proc = subprocess.Popen([sys.executable, str(script)], env=env, cwd=str(ws))
    # The cap clock starts when the worker is actually working, not while the interpreter boots.
    deadline = time.time() + 120
    while not ready.exists() and time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc


def card(db, task):
    from hermes_cli import kanban_db_connect
    conn = kanban_db_connect.connect(Path(db))
    try:
        rows = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id", (task,)
        ).fetchall()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM task_events WHERE task_id = ? GROUP BY kind", (task,)
        ).fetchall()
    finally:
        conn.close()
    return [tuple(r) for r in rows], {k: n for k, n in kinds}


def main() -> int:
    from hermes_cli import kanban_checkpoint
    tmp = Path(tempfile.mkdtemp(prefix="checkpoint-proof-"))
    out = []
    def say(line=""):
        print(line)
        out.append(line)

    say(f"subject: {Path(kanban_checkpoint.__file__).name} "
        f"sha256={__import__('hashlib').sha256(Path(kanban_checkpoint.__file__).read_bytes()).hexdigest()}")
    say(f"compressed timing: interval 2s / cap 8s (a real run: 600s interval / 2400s cap)")
    say()

    # --- A: over cap, hard kill -----------------------------------------------------------
    say("=== A. over-cap run, hard-killed at the cap (TerminateProcess: no catchable handler) ===")
    db, task, run, lock = fixture(tmp, "overcap", lock="host:4242")
    ws = tmp / "ws-overcap"
    ws.mkdir()
    artifact = ws / "artifact-report.md"
    proc = run_worker(db, task, lock, ws, artifact, ticks=40)
    time.sleep(8.0)
    say(f"cap reached after 8s; worker pid {proc.pid} alive={proc.poll() is None}")
    proc.kill()
    proc.wait(timeout=10)
    say(f"worker exit code {proc.returncode} (killed, no orderly exit)")
    comments, kinds = card(db, task)
    ck = [b for _, b in comments if "hermes-run-checkpoint" in b]
    say(f"checkpoints on the card after the kill: {len(ck)}  (event kinds: {json.dumps(kinds)})")
    say(f"named artifact present: {artifact.exists()} -> {artifact} "
        f"({artifact.stat().st_size if artifact.exists() else 0} bytes)")
    say("--- last checkpoint as the card shows it ---")
    say(ck[-1] if ck else "(none)")
    say("--- first checkpoint (proves the timer, not the exit path, wrote it) ---")
    say(ck[0].splitlines()[1] if ck else "(none)")
    ok_a = len(ck) >= 2 and artifact.exists() and "written this run" in ck[-1]
    say(f"RESULT A: {'PASS' if ok_a else 'FAIL'}")
    say()

    # --- B: clean run ---------------------------------------------------------------------
    say("=== B. clean run, no kill: checkpoints must not clutter the record ===")
    db2, task2, run2, lock2 = fixture(tmp, "clean", lock="host:4243")
    ws2 = tmp / "ws-clean"
    ws2.mkdir()
    art2 = ws2 / "clean-output.txt"
    # Interval 30 s against a 3 s run: the real relationship (600 s interval, a card that ends
    # before the first tick), so the clean record must hold exactly one closing checkpoint.
    proc2 = run_worker(db2, task2, lock2, ws2, art2, ticks=3, interval=30.0)
    proc2.wait(timeout=30)
    comments2, kinds2 = card(db2, task2)
    ck2 = [b for _, b in comments2 if "hermes-run-checkpoint" in b]
    say(f"worker exit code {proc2.returncode}; comments on the card: {len(comments2)}")
    say(f"checkpoints: {len(ck2)} (event kinds: {json.dumps(kinds2)})")
    say(ck2[0] if ck2 else "(none)")
    ok_b = len(ck2) == 1 and "finished" in ck2[0]
    say(f"RESULT B: {'PASS' if ok_b else 'FAIL'}")
    say()

    # --- C: dispatcher backstop + ownership guard ------------------------------------------
    say("=== C. dispatcher backstop: silent run is surfaced, owned run is not duplicated ===")
    db3, task3, _, lock3 = fixture(tmp, "silent", lock="host:4244")
    from hermes_cli import kanban_db_connect
    conn = kanban_db_connect.connect(Path(db3))
    try:
        with kanban_db_connect.write_txn(conn):
            kanban_checkpoint.record_cutoff(conn, task3, 1, elapsed=2412, limit=2400, sigkill=True)
        silent = kanban_checkpoint.silent_run(conn, task3, min_seconds=600)
    finally:
        conn.close()
    comments3, _ = card(db3, task3)
    say(f"cutoff lines written for a silent run: {len(comments3)}")
    say(comments3[0][1].splitlines()[-1] if comments3 else "(none)")
    # ownership guard: a writer whose claim lock no longer matches writes nothing
    cp_stale = kanban_checkpoint.RunCheckpoint(task_id="overcap", run_id=1,
                                               claim_lock="host:9999", board_db=db,
                                               workspace=str(ws))
    wrote = cp_stale.write("timer", force=True)
    say(f"foreign claim_lock write returned {wrote!r} (None = refused, guard intact)")
    cp_ok = kanban_checkpoint.checkpoint_count(
        kanban_db_connect.connect(Path(db)), "overcap", 1)
    ok_c = len(comments3) == 1 and wrote is None and cp_ok == len(ck)
    say(f"RESULT C: {'PASS' if ok_c else 'FAIL'}")
    say()
    say(f"OVERALL: {'PASS' if (ok_a and ok_b and ok_c) else 'FAIL'}")
    (tmp / "proof.log").write_text("\n".join(out) + "\n", encoding="utf-8")
    return 0 if (ok_a and ok_b and ok_c) else 1


if __name__ == "__main__":
    raise SystemExit(main())
