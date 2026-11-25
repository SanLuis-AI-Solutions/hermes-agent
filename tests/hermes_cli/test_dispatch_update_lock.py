"""The dispatcher may not spawn a worker into an update-locked checkout.

A leftover ``source-completion-pending`` makes every launch of a source tree run an
unfinished update tail. That tail takes the shared ``UpdateLock``, which publishes
``.hermes-update-in-progress`` naming the *launching* process; every other launch in
that window is fenced by ``gateway_runtime._update_fenced``. A worker the dispatcher
spawned into the window can therefore never be admitted — it waits for the gateway,
the owner heartbeat goes silent, and it dies with::

    Kanban owner refused: Kanban owner silent for 183s: Gateway draining: update_paused

(twelve such runs in thirty days, six of them back to back between 15:18 and 15:37 —
each one burned its whole budget learning it was never going to be allowed to work).

So a dispatch tick asks the fence ONCE, and a fenced tick spawns nothing and penalises
nobody. These four tests are the guard:

1. a fenced tick spawns nothing and leaves every card ``ready``;
2. an unfenced tick is exactly as before (the control — a guard that also blocks the
   healthy path is worse than the bug);
3. the fence is asked once per tick, not once per card;
4. deferred is not failed: no failure counter, no ``last_failure_error``, no retry.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import update_lock


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


@pytest.fixture
def checkout_probe(monkeypatch):
    """Count the checkout-lock probes; report the lock free (the marker fences instead).

    A counter, not a constant: this is what makes "read once per tick" falsifiable, and a
    real lock probe would depend on the repository this suite runs from.
    """
    calls: list = []

    def probe(install_root=None):
        calls.append(install_root)
        return False

    monkeypatch.setattr(update_lock, "checkout_lock_held", probe)
    return calls


@pytest.fixture
def live_update_marker(kanban_home):
    """A real, live update marker for THIS process in the test home (not a stub).

    Same bytes ``UpdateLock`` publishes: pid, started_at, ``ct:`` creation time. The
    fixture asserts the marker is genuinely judged live, so a test can never pass by
    probing a marker nobody would fence on.
    """
    path = update_lock.update_marker_path()
    assert path.parent == kanban_home, f"marker resolved outside the test home: {path}"
    path.write_bytes(
        f"{os.getpid()}\n{int(time.time())}\n{update_lock._identity_line()}\n".encode())
    holder = update_lock.read_live_update()
    assert holder is not None and holder.pid == os.getpid(), (
        f"the fixture's marker is not a live claim: {holder!r}")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _ready_cards(conn, n: int = 3) -> list[str]:
    return [kb.create_task(conn, title=f"card {i}", assignee="alice") for i in range(n)]


def _spawn_recorder():
    calls: list = []

    def spawn(task, workspace_path, board=None):
        calls.append((task.id, workspace_path))
        return 4242

    return calls, spawn


def _placeholders(ids: list[str]) -> tuple[str, tuple]:
    return ",".join("?" * len(ids)), tuple(ids)


def _count(conn, table: str, ids: list[str]) -> int:
    marks, args = _placeholders(ids)
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE task_id IN ({marks})", args).fetchone()[0])


def _statuses(conn, ids: list[str]) -> dict[str, str]:
    marks, args = _placeholders(ids)
    return {row["id"]: row["status"] for row in conn.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({marks})", args)}


def test_fenced_tick_spawns_nothing(
    conn, all_assignees_spawnable, checkout_probe, live_update_marker,
):
    """An update holds the checkout: zero runs, three cards still ready, all three reported."""
    ids = _ready_cards(conn, 3)
    calls, spawn = _spawn_recorder()

    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert live_update_marker.exists()
    assert calls == [], "no worker may be spawned into an update-locked checkout"
    assert result.spawned == []
    assert {task_id for task_id, _reason in result.deferred_update_lock} == set(ids)
    assert all(reason for _task_id, reason in result.deferred_update_lock), (
        "the bucket must say WHY, not just that something was deferred")
    assert _statuses(conn, ids) == {task_id: "ready" for task_id in ids}
    assert _count(conn, "task_runs", ids) == 0, "a deferred card never opened a run"


def test_unfenced_tick_is_unaffected(conn, all_assignees_spawnable, checkout_probe):
    """The control: with no update holding the checkout, the same board spawns normally."""
    assert update_lock.read_live_update() is None, "the control must start unfenced"
    ids = _ready_cards(conn, 3)
    calls, spawn = _spawn_recorder()

    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert sorted(task_id for task_id, _ws in calls) == sorted(ids)
    assert sorted(row[0] for row in result.spawned) == sorted(ids)
    assert result.deferred_update_lock == [], "the guard must not defer a healthy tick"


def test_fence_is_asked_once_per_tick(
    conn, all_assignees_spawnable, checkout_probe, live_update_marker,
):
    """One probe for a three-card fenced tick, and one more for the next tick — never per card."""
    _ready_cards(conn, 3)
    _calls, spawn = _spawn_recorder()

    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert len(checkout_probe) == 1, (
        f"3 cards, 1 tick, {len(checkout_probe)} probes: the fence is being asked per card")

    # Still fenced (the marker is untouched) — a second tick is a second read, not a free pass.
    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert len(checkout_probe) == 2, (
        f"2 ticks over 3 cards gave {len(checkout_probe)} probes, expected 2")


def test_deferred_card_keeps_its_retry_budget(
    conn, all_assignees_spawnable, checkout_probe, live_update_marker,
):
    """A card that was never attempted must not be charged for the wait.

    Card 0 carries a real failure from an earlier attempt. The fenced tick must leave
    every failure column exactly as it found it — no increment, no ``last_failure_error``,
    no reset, no run row, no per-card event — because "deferred" and "failed" are
    different verdicts and only one of them spends the card's budget.
    """
    ids = _ready_cards(conn, 3)
    with kbc.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1, last_failure_error = ? WHERE id = ?",
            ("boom: an earlier real attempt", ids[0]),
        )
    query = (
        "SELECT id, status, consecutive_failures, last_failure_error, max_retries, "
        "claim_lock, current_run_id, worker_pid FROM tasks "
        f"WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id"
    )
    before = {row["id"]: tuple(row) for row in conn.execute(query, tuple(ids))}
    events_before = _count(conn, "task_events", ids)
    _calls, spawn = _spawn_recorder()

    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    after = {row["id"]: tuple(row) for row in conn.execute(query, tuple(ids))}
    assert len(result.deferred_update_lock) == 3
    assert after == before, "a fenced tick wrote task state for cards it never attempted"
    assert before[ids[0]][2] == 1, "the seeded failure counter is the precondition"
    assert after[ids[0]][2] == 1, "the deferred tick incremented consecutive_failures"
    assert after[ids[0]][3] == "boom: an earlier real attempt", (
        "the deferred tick overwrote last_failure_error")
    assert all(row[2] == 0 and row[3] is None for row in after.values() if row[0] != ids[0])
    assert _count(conn, "task_events", ids) == events_before, (
        "a fenced tick must not write a per-card event (that noise is what the guard removes)")
    assert _count(conn, "task_runs", ids) == 0, "a deferred card must not consume a run/retry"
