"""Runtime run checkpoint: the card keeps a record even when the model stops taking turns.

A profile instruction to checkpoint was held by two runs and obeyed by neither (run 957: 41m19s,
no comment, no artifact, no commit). These tests pin the mechanism that replaces the instruction:
the worker client writes the card itself, on a timer and on every exit path, and only while it
still holds the claim.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_checkpoint as kc
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worker_client as wcc

LOCK = "host:4242"


@pytest.fixture
def board(tmp_path):
    """A real board DB with one running task/attempt, the way the dispatcher leaves it."""
    db = tmp_path / "board.db"
    kbc.init_db(db)
    conn = kbc.connect(db)
    try:
        with kbc.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks (id, title, body, status, assignee, created_at, started_at,"
                " claim_lock, max_runtime_seconds, current_run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("t_card", "checkpoint card", "", "running", "codingboss",
                 int(time.time()), int(time.time()), LOCK, 2400, 1),
            )
            conn.execute(
                "INSERT INTO task_runs (id, task_id, profile, status, started_at)"
                " VALUES (1, 't_card', 'codingboss', 'running', ?)",
                (int(time.time()),),
            )
    finally:
        conn.close()
    return str(db)


def writer(board, tmp_path, **overrides):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "artifact-report.md").write_text("findings\n", encoding="utf-8")
    kwargs = dict(task_id="t_card", run_id=1, claim_lock=LOCK, board_db=board,
                  workspace=str(workspace))
    kwargs.update(overrides)
    return kc.RunCheckpoint(**kwargs)


def comments(board):
    conn = kbc.connect(Path(board))
    try:
        return [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = 't_card' ORDER BY id").fetchall()]
    finally:
        conn.close()


def events(board, kind):
    conn = kbc.connect(Path(board))
    try:
        return conn.execute(
            "SELECT payload FROM task_events WHERE task_id = 't_card' AND kind = ?", (kind,)
        ).fetchall()
    finally:
        conn.close()


def test_timer_checkpoint_lands_on_the_card_and_names_the_work(board, tmp_path):
    """The timer path writes a durable comment naming elapsed time and the files the run wrote."""
    cp = writer(board, tmp_path)

    assert cp.write("timer") is not None

    body, = comments(board)
    assert kc.MARKER in body
    assert "run checkpoint 1" in body
    assert "in flight" in body
    # The artifact path is the reviewable work a killed run must leave behind.
    assert "artifact-report.md" in body
    assert len(events(board, kc.EVENT_KIND)) == 1


def test_a_process_without_the_claim_writes_nothing(board, tmp_path):
    """Ownership is not weakened: a stale claim lock writes no comment and no event."""
    stale = writer(board, tmp_path, claim_lock="host:9999")

    assert stale.write("timer") is None
    assert stale.finish("exit") is None

    assert comments(board) == []
    assert events(board, kc.EVENT_KIND) == []


def test_a_reclaimed_run_writes_nothing(board, tmp_path):
    """A run that lost the card to a newer attempt cannot annotate the new attempt's record."""
    conn = kbc.connect(Path(board))
    try:
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET current_run_id = 2 WHERE id = 't_card'")
    finally:
        conn.close()

    assert writer(board, tmp_path).write("timer") is None
    assert comments(board) == []


def test_timer_checkpoints_are_bounded_but_the_exit_checkpoint_is_not(board, tmp_path):
    """A runaway run stops papering the card, and still records how it ended while it holds the
    claim (the cap's SIGTERM / crash path)."""
    cp = writer(board, tmp_path)
    for _ in range(kc.MAX_TIMER_CHECKPOINTS + 3):
        cp.write("timer")
    assert len(comments(board)) == kc.MAX_TIMER_CHECKPOINTS

    assert cp.finish("exit") is not None
    bodies = comments(board)
    assert len(bodies) == kc.MAX_TIMER_CHECKPOINTS + 1
    assert "finished" in bodies[-1]


def test_sigterm_checkpoint_continues_the_sequence(board, tmp_path):
    """The SIGTERM hook builds its own writer; its line must not restart at seq 1.

    A cap kill's last line is the evidence a reader trusts most, so it is numbered *after* the
    timer checkpoints that came before it, not before them.
    """
    cp = writer(board, tmp_path)
    assert cp.write("timer") is not None
    assert cp.write("timer") is not None

    # The handler's exact shape: a fresh instance for the same attempt (kanban_worker_client).
    handler = writer(board, tmp_path)
    assert handler.finish("sigterm") is not None

    payloads = [json.loads(r[0]) for r in events(board, kc.EVENT_KIND)]
    assert [p["seq"] for p in payloads] == [1, 2, 3]
    assert [p["reason"] for p in payloads] == ["timer", "timer", "sigterm"]
    assert "run checkpoint 3" in comments(board)[-1]
    assert "cut off (SIGTERM)" in comments(board)[-1]


def test_a_settled_clean_run_leaves_no_closing_checkpoint(board, tmp_path):
    """Production ordering: the owner's handoff settles the card before the client exits.

    ``kanban_complete``/``kanban_request_review`` run inside the owner's turn and set
    ``claim_lock = NULL`` and ``current_run_id = NULL``; by the time the worker client reaches
    ``finish('exit')`` it no longer holds the claim, so no closing line can be written without a
    fresh ownership proof. A run shorter than the interval therefore leaves zero checkpoints.
    """
    conn = kbc.connect(Path(board))
    try:
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL, current_run_id = NULL"
                " WHERE id = 't_card'")
    finally:
        conn.close()

    assert writer(board, tmp_path).finish("exit") is None
    assert comments(board) == []
    assert events(board, kc.EVENT_KIND) == []


def test_a_settled_run_keeps_its_timer_record_and_gains_no_finished_line(board, tmp_path):
    """A long clean run is not retroactively rewritten: its timer lines stand, and no line
    claims a 'finished' state the settled card never reached."""
    cp = writer(board, tmp_path)
    assert cp.write("timer") is not None

    conn = kbc.connect(Path(board))
    try:
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL, current_run_id = NULL"
                " WHERE id = 't_card'")
    finally:
        conn.close()

    assert cp.finish("exit") is None
    bodies = comments(board)
    assert len(bodies) == 1
    assert "in flight" in bodies[0]
    assert "finished" not in bodies[0]


def test_exit_paths_say_plainly_that_the_run_was_cut_off(board, tmp_path):
    """SIGTERM/error/cancel leave a record that reads as an interrupted attempt, not a clean one."""
    cp = writer(board, tmp_path)

    cp.finish("sigterm")

    body, = comments(board)
    assert "cut off (SIGTERM)" in body
    payload, = [json.loads(r[0]) for r in events(board, kc.EVENT_KIND)]
    assert payload["reason"] == "sigterm"


def test_record_cutoff_surfaces_a_silent_run_exactly_once(board):
    """The dispatcher backstop writes the 'cut off' line only when the run left no checkpoint."""
    conn = kbc.connect(Path(board))
    try:
        with kbc.write_txn(conn):
            assert kc.record_cutoff(conn, "t_card", 1, elapsed=2412, limit=2400, sigkill=True)
        # A retried tick for the same run must not stack duplicate evidence.
        with kbc.write_txn(conn):
            assert kc.record_cutoff(conn, "t_card", 1, elapsed=2413, limit=2400, sigkill=True) is None
    finally:
        conn.close()
    bodies = comments(board)
    assert len(bodies) == 1
    assert "was cut off at its cap" in bodies[0]
    assert "no checkpoint" in bodies[0]


def test_a_run_that_checkpointed_gets_no_cutoff_line(board, tmp_path):
    """The backstop defers to the worker's own record — it is a fallback, not a duplicate."""
    assert writer(board, tmp_path).write("timer") is not None

    conn = kbc.connect(Path(board))
    try:
        with kbc.write_txn(conn):
            assert kc.record_cutoff(conn, "t_card", 1, elapsed=2412, limit=2400, sigkill=True) is None
    finally:
        conn.close()

    assert len(comments(board)) == 1


def test_silent_run_scores_a_long_silent_attempt_as_suspicious(board, tmp_path):
    """A 40-minute attempt that ended non-done with zero checkpoints is surfaced, not a zero."""
    conn = kbc.connect(Path(board))
    try:
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status = 'timed_out', outcome = 'timed_out',"
                " ended_at = started_at + 2420 WHERE id = 1")
        assert kc.silent_run(conn, "t_card", min_seconds=600) == {
            "run_id": 1, "outcome": "timed_out", "ran_seconds": 2420}
        # Short attempts are ordinary failures, not silence defects.
        assert kc.silent_run(conn, "t_card", min_seconds=3600) is None
    finally:
        conn.close()

    # Once the attempt left a checkpoint it is no longer silent.
    assert writer(board, tmp_path).finish("sigterm") is not None
    conn = kbc.connect(Path(board))
    try:
        assert kc.silent_run(conn, "t_card", min_seconds=600) is None
    finally:
        conn.close()


# --- scorecard: silence is a defect to surface -------------------------------------------


def _diagnose(runs, events):
    from hermes_cli import kanban_diagnostics as kd
    task = {"id": "t_card", "status": "blocked", "consecutive_failures": 0}
    return [d for d in kd.compute_task_diagnostics(task, events, runs, now=10_000)
            if d.kind == "silent_run"]


def test_a_long_run_without_checkpoints_is_surfaced_as_a_defect():
    """Run 957's shape — 41m non-done, nothing written — must not score as a quiet zero."""
    runs = [{"id": 1, "task_id": "t_card", "outcome": "timed_out",
             "started_at": 0, "ended_at": 2479}]

    found = _diagnose(runs, [])

    assert len(found) == 1
    assert found[0].severity == "warning"
    assert found[0].run_id == 1
    assert found[0].data["ran_seconds"] == 2479


def test_a_run_that_left_a_checkpoint_is_not_a_silent_run():
    runs = [{"id": 1, "task_id": "t_card", "outcome": "timed_out",
             "started_at": 0, "ended_at": 2479}]
    events = [{"id": 1, "task_id": "t_card", "run_id": 1, "kind": "run_checkpoint",
               "payload": '{"reason": "timer", "seq": 1}', "created_at": 600}]

    assert _diagnose(runs, events) == []


def test_short_and_completed_runs_are_not_silent_run_defects():
    short = [{"id": 1, "task_id": "t_card", "outcome": "crashed",
              "started_at": 0, "ended_at": 90}]
    completed = [{"id": 2, "task_id": "t_card", "outcome": "completed",
                  "started_at": 0, "ended_at": 4000}]

    assert _diagnose(short, []) == []
    assert _diagnose(completed, []) == []


# --- a checkpoint is a record, never a participant -----------------------------------------


def _capture_checkpoint(monkeypatch):
    """Run the real ``run()`` drive and hand the test the checkpoint instance it built."""
    async def clean_drive(_params, _board_db):
        return 0

    made = {}
    real = kc.RunCheckpoint.from_params

    def capture(cls, params, board_db, workspace=None):
        cp = real(params, board_db, workspace)
        made["cp"] = cp
        return cp

    monkeypatch.setattr(wcc, "_drive_claim", clean_drive)
    monkeypatch.setattr(kc.RunCheckpoint, "from_params", classmethod(capture))
    return wcc, made


def test_a_missing_board_db_never_changes_the_run_exit(tmp_path, monkeypatch):
    """A facts-read failure on an absent board is recorded, not raised out of the run.

    ``RunCheckpoint._db`` must not create the file it did not find: the 0-byte DB a bare
    ``sqlite3.connect`` leaves behind is exactly how a bookkeeping error became a dead run.
    """
    wcc, made = _capture_checkpoint(monkeypatch)
    board = tmp_path / "absent.db"
    params = {"task_id": "t_card", "run_id": 1, "claim_lock": LOCK}

    assert asyncio.run(wcc.run(params, str(board), str(tmp_path))) == 0

    assert not board.exists(), "a checkpoint must never create a board it did not find"
    assert "refusing to create" in (made["cp"]._last_error or "")


def test_a_board_without_task_events_never_changes_the_run_exit(tmp_path, monkeypatch):
    """A pre-``task_events`` schema is a refusal, not a run-killing exception."""
    wcc, made = _capture_checkpoint(monkeypatch)
    board = tmp_path / "pre_schema.db"
    conn = sqlite3.connect(str(board))
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    params = {"task_id": "t_card", "run_id": 1, "claim_lock": LOCK}

    assert asyncio.run(wcc.run(params, str(board), str(tmp_path))) == 0

    assert "task_events" in (made["cp"]._last_error or "")


def test_sigterm_checkpoint_failure_still_restores_sig_dfl(monkeypatch):
    """A failing checkpoint must not skip the default disposition (that is SIGKILL, not SIGTERM)."""
    class _Raising:
        def finish(self, _reason):
            raise RuntimeError("board vanished under the worker")

    monkeypatch.setattr(kc.RunCheckpoint, "from_params",
                        classmethod(lambda cls, *a, **k: _Raising()))
    calls = {}

    def fake_signal(sig, handler):
        calls["signal"] = (sig, handler)

    kills = []
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    wcc._install_sigterm_checkpoint(
        {"task_id": "t_card", "run_id": 1, "claim_lock": LOCK}, "board.db", None)
    handler = calls["signal"][1]

    handler(signal.SIGTERM, None)  # must not propagate

    assert calls["signal"] == (signal.SIGTERM, signal.SIG_DFL)
    assert kills == [(os.getpid(), signal.SIGTERM)]


def test_a_locked_board_still_contains_the_checkpoint_failure(board, tmp_path):
    """Today's contained write half must stay contained: recorded, no raise, no partial write."""
    cp = writer(board, tmp_path)
    blocker = kbc.connect(Path(board))
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE tasks SET title = title WHERE id = 't_card'")

        assert cp.write("timer") is None
        assert "locked" in (cp._last_error or "")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert comments(board) == []
    # Once the lock clears the record lands, and it carries the failure it survived.
    assert cp.write("timer") is not None
    assert "last checkpoint error" in comments(board)[-1]
