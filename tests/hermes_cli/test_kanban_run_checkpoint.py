"""Runtime run checkpoint: the card keeps a record even when the model stops taking turns.

A profile instruction to checkpoint was held by two runs and obeyed by neither (run 957: 41m19s,
no comment, no artifact, no commit). These tests pin the mechanism that replaces the instruction:
the worker client writes the card itself, on a timer and on every exit path, and only while it
still holds the claim.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_checkpoint as kc
from hermes_cli import kanban_db_connect as kbc

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
    """A runaway run stops papering the card, and still records how it ended."""
    cp = writer(board, tmp_path)
    for _ in range(kc.MAX_TIMER_CHECKPOINTS + 3):
        cp.write("timer")
    assert len(comments(board)) == kc.MAX_TIMER_CHECKPOINTS

    assert cp.finish("exit") is not None
    bodies = comments(board)
    assert len(bodies) == kc.MAX_TIMER_CHECKPOINTS + 1
    assert "finished" in bodies[-1]


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
