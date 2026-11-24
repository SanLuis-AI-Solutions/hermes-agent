"""Runtime run checkpoint: the worker client writes its own progress to its card.

A profile instruction to checkpoint is prose, and prose does not survive a long card: two runs
held a ten-minute rule and both left an empty record when the cap killed them (run 957: 41m19s,
no comment, no artifact, no commit). This module is the mechanism instead of the rule: the
worker client process — the one the dispatcher spawns, fingerprints and SIGTERMs — runs it.

Nothing here consults the model. The facts are read off the board and the filesystem (elapsed
time, heartbeats and their notes, git HEAD/subject, changed files, artifact paths), so a
checkpoint cannot be skipped by a model that ran out of turns, and it costs no model turn.

Ownership is not weakened: every write is one IMMEDIATE transaction that CASes on
``tasks.current_run_id`` and ``tasks.claim_lock``. A process that does not hold the claim
writes nothing.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# Ten minutes: short enough that a 40-minute run leaves three facts on the card, long enough
# that a normal card ends before the first timer tick and the record stays clean.
CHECKPOINT_INTERVAL_S = 600.0
INTERVAL_ENV = "HERMES_KANBAN_CHECKPOINT_INTERVAL_S"
MARKER = "<!-- hermes-run-checkpoint v1 -->"
EVENT_KIND = "run_checkpoint"
AUTHOR = "hermes-runtime"
# An exit-time checkpoint is always written (it says how the run ended); timer checkpoints are
# written on every tick up to this bound, so a runaway run gets a periodic record without papering
# the card, and a short run (interval 600 s against a card that ends first) writes one line.
MAX_TIMER_CHECKPOINTS = 12
_WALK_LIMIT = 4000
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}


@dataclass
class RunCheckpoint:
    """One attempt's checkpoint writer. ``write`` is synchronous on purpose: it must survive a
    cancellation, and sqlite's WAL write is far shorter than the dispatcher's SIGTERM grace."""

    task_id: str
    run_id: int
    claim_lock: str
    board_db: str
    workspace: Optional[str] = None
    started: float = field(default_factory=time.time)
    _seq: int = 0
    _timer_writes: int = 0
    _last_error: Optional[str] = None
    # The timer writes from a worker thread and the SIGTERM hook from the main thread: sequence
    # numbers and the timer bound are shared state, so one write at a time.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False,
                                  compare=False)

    @classmethod
    def from_params(cls, params: dict, board_db: str, workspace: Optional[str] = None):
        return cls(task_id=params["task_id"], run_id=int(params["run_id"]),
                   claim_lock=params["claim_lock"], board_db=board_db, workspace=workspace)

    # --- facts ---------------------------------------------------------------------------
    def _elapsed(self) -> float:
        return max(0.0, time.time() - self.started)

    def _board_facts(self, conn) -> dict:
        row = conn.execute(
            "SELECT COUNT(*), MAX(created_at) FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'heartbeat'",
            (self.task_id, self.run_id)).fetchone()
        note = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = 'heartbeat' AND payload IS NOT NULL ORDER BY id DESC LIMIT 1",
            (self.task_id, self.run_id)).fetchone()
        done = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ?",
            (self.task_id, self.run_id, EVENT_KIND)).fetchone()
        last_note = ""
        if note and note[0]:
            import json
            try:
                last_note = str((json.loads(note[0]) or {}).get("note") or "")
            except (ValueError, TypeError):
                last_note = ""
        return {"heartbeats": int(row[0] or 0), "last_note": last_note,
                "prior_checkpoints": int(done[0] or 0)}

    def _git_facts(self) -> dict:
        """HEAD, subject, changed-file count, newest changed path — empty when not a repo."""
        if not self.workspace:
            return {}
        ws = Path(self.workspace)
        if not (ws / ".git").exists():
            return {}

        def run(*args):
            return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True,
                                  timeout=8)

        try:
            head = run("rev-parse", "--short", "HEAD").stdout.strip()
            subject = run("log", "-1", "--format=%s").stdout.strip()
            porcelain = [ln for ln in run("status", "--porcelain").stdout.splitlines() if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            return {}
        if not head:
            return {}
        facts = {"head": head, "subject": subject[:80], "changed": len(porcelain)}
        if porcelain:
            facts["newest"] = porcelain[-1][3:].strip()[:120]
        return facts

    def _recent_paths(self, limit: int = 4) -> list[str]:
        """Files written in the workspace since this run started — artifact candidates."""
        if not self.workspace:
            return []
        root = Path(self.workspace)
        if not root.is_dir():
            return []
        seen: list[tuple[float, str]] = []
        cutoff = self.started - 60
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                count += 1
                if count > _WALK_LIMIT:
                    break
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    seen.append((st.st_mtime, str(Path(dirpath, name).relative_to(root))))
            if count > _WALK_LIMIT:
                break
        seen.sort(reverse=True)
        return [p for _, p in seen[:limit]]

    def _db(self):
        """A short-lived direct connection: the checkpoint must not pay for the board's
        corruption probe / init lock every ten minutes, and it holds the write lock only for the
        handful of statements below."""
        import sqlite3
        conn = sqlite3.connect(self.board_db, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _facts(self) -> dict:
        conn = self._db()
        try:
            facts = self._board_facts(conn)
        finally:
            conn.close()
        facts.update({"git": self._git_facts(), "recent": self._recent_paths()})
        return facts

    def _body(self, reason: str, facts: dict, seq: int) -> str:
        git = facts.get("git") or {}
        elapsed = int(self._elapsed())
        lines = [f"{MARKER} run={self.run_id} seq={seq} reason={reason}"]
        state = {"timer": "in flight", "exit": "finished", "sigterm": "cut off (SIGTERM)",
                 "error": "cut off (error)", "cancelled": "cut off (cancelled)"}.get(reason, reason)
        lines.append(f"run checkpoint {seq} · +{elapsed // 60}m{elapsed % 60:02d}s · {state}")
        heard = facts.get("heartbeats", 0)
        note = facts.get("last_note") or ""
        lines.append(f"board: {heard} heartbeat{'s' if heard != 1 else ''}"
                     + (f", last: {note[:160]}" if note else ""))
        if git:
            lines.append(f"git: {git.get('head', '?')} \"{git.get('subject', '')}\" · "
                         f"{git.get('changed', 0)} changed file(s)"
                         + (f" · newest {git['newest']}" if git.get("newest") else ""))
        recent = facts.get("recent") or []
        if recent:
            lines.append("written this run: " + ", ".join(recent))
        if self._last_error:
            lines.append(f"last checkpoint error: {self._last_error[:160]}")
        return "\n".join(lines)

    # --- write ---------------------------------------------------------------------------
    def write(self, reason: str = "timer") -> Optional[int]:
        """Append one checkpoint comment + ``run_checkpoint`` event, or None when suppressed.

        Suppressed when this is a timer tick past ``MAX_TIMER_CHECKPOINTS``, when the caller no
        longer holds the claim (run ended, card settled by its own handoff, card re-claimed, task
        archived), or on any failure — a checkpoint must never take a run down with it.

        The sequence number is seeded from the checkpoints this attempt already left, so a second
        writer for the same run (the SIGTERM hook builds its own instance) continues the record
        instead of restarting it at 1.
        """
        facts = self._facts()
        with self._lock:
            if reason == "timer":
                # Bound the record on a runaway run; a stalled run still gets a line every tick
                # until the bound, which is the point (silence is the defect being repaired).
                if self._timer_writes >= MAX_TIMER_CHECKPOINTS:
                    return None
            seq = max(self._seq, int(facts.get("prior_checkpoints") or 0)) + 1
            body = self._body(reason, facts, seq)
            try:
                conn = self._db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
                        (self.task_id,)).fetchone()
                    if row is None:
                        conn.execute("ROLLBACK")
                        return None
                    status, current_run, lock = row[0], row[1], row[2]
                    # The guard: this process writes only while it holds THIS attempt's claim.
                    # ``running`` is part of it: ``complete_task``/``request_review`` clear both
                    # the claim and the run id inside the owner's turn, so a closing line here
                    # after that handoff cannot be written without a fresh ownership proof — and
                    # the settled card's own events already say how the run ended.
                    if status != "running" or int(current_run or -1) != self.run_id \
                            or (lock or "") != self.claim_lock:
                        conn.execute("ROLLBACK")
                        return None
                    cur = conn.execute(
                        "INSERT INTO task_comments (task_id, author, body, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (self.task_id, AUTHOR, body, int(time.time())))
                    conn.execute(
                        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (self.task_id, self.run_id, EVENT_KIND,
                         '{"reason": "%s", "seq": %d}' % (reason, seq), int(time.time())))
                    conn.execute("COMMIT")
                    self._seq = seq
                    if reason == "timer":
                        self._timer_writes += 1
                    return int(cur.lastrowid or 0)
                finally:
                    conn.close()
            except Exception as exc:  # a checkpoint is telemetry: never the reason a run dies
                self._last_error = f"{type(exc).__name__}: {exc}"
                return None

    async def timer_loop(self, interval: Optional[float] = None) -> None:
        every = interval if interval is not None else _interval()
        while True:
            await asyncio.sleep(every)
            await asyncio.to_thread(self.write, "timer")

    def finish(self, reason: str) -> Optional[int]:
        """Exit-path checkpoint. Synchronous and never raises (see ``write``).

        It lands when the process still holds the attempt's claim — the cap's SIGTERM, a crash, a
        cancellation, a lost owner. A run that ended *by its own handoff* (``kanban_complete`` /
        ``kanban_request_review`` run inside the owner's turn and clear ``claim_lock`` and
        ``current_run_id``) is already settled, so this returns None: its timer checkpoints, or
        nothing at all for a run shorter than the interval, are its record.
        """
        return self.write(reason)


def _interval() -> float:
    raw = os.environ.get(INTERVAL_ENV)
    try:
        value = float(raw) if raw else CHECKPOINT_INTERVAL_S
    except ValueError:
        return CHECKPOINT_INTERVAL_S
    return value if value > 0 else CHECKPOINT_INTERVAL_S


def record_cutoff(conn, task_id: str, run_id: int, *, elapsed: int, limit: int,
                  sigkill: bool) -> Optional[int]:
    """Dispatcher-side backstop, inside the caller's txn: when a capped run left no checkpoint,
    write one line saying it was cut off. Called from ``enforce_max_runtime`` after ``_end_run``
    (the run is already closed, so this is a plain INSERT and no claim CAS applies)."""
    if checkpoint_count(conn, task_id, run_id) or _cutoff_count(conn, task_id, run_id):
        return None
    how = "SIGTERM then SIGKILL" if sigkill else "SIGTERM"
    body = (f"<!-- hermes-run-cutoff v1 run={run_id} -->\n"
            f"run {run_id} was cut off at its cap: elapsed {elapsed}s > limit {limit}s ({how}). "
            f"The worker left no checkpoint on this card, so this record is the only evidence of "
            f"the attempt. Suspicious by definition: a long run that writes nothing to its own "
            f"card is a runtime defect, not a quiet zero.")
    cur = conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, AUTHOR, body, int(time.time())))
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, "run_cutoff",
         '{"elapsed_seconds": %d, "limit_seconds": %d, "sigkill": %s}'
         % (elapsed, limit, "true" if sigkill else "false"), int(time.time())))
    return int(cur.lastrowid or 0)


def checkpoint_count(conn, task_id: str, run_id: int) -> int:
    """How many checkpoints this attempt left — the scorecard's 'did the record survive' probe."""
    return _kind_count(conn, task_id, run_id, EVENT_KIND)


def _kind_count(conn, task_id: str, run_id: int, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ?",
        (task_id, int(run_id), kind)).fetchone()
    return int(row[0] or 0)


def _cutoff_count(conn, task_id: str, run_id: int) -> int:
    """A retried dispatcher tick must not stack duplicate 'cut off' evidence on the card."""
    return _kind_count(conn, task_id, run_id, "run_cutoff")


def silent_run(conn, task_id: str, *, min_seconds: int = 600) -> Optional[dict]:
    """Return the newest attempt that ended non-done after ``min_seconds`` with zero
    checkpoints, or None. A silent forty-minute run is a defect to surface, not a quiet zero."""
    row = conn.execute(
        "SELECT id, outcome, started_at, ended_at FROM task_runs WHERE task_id = ? "
        "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None or (row[1] or "done") == "done":
        return None
    ran = int(row[3] or 0) - int(row[2] or 0)
    if ran < min_seconds:
        return None
    if checkpoint_count(conn, task_id, int(row[0])):
        return None
    return {"run_id": int(row[0]), "outcome": row[1], "ran_seconds": ran}
