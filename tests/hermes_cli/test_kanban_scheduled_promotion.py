"""Regression tests for #325/6533472610 — a card parked in ``scheduled`` must
move to ``ready`` once its blocker clears.

The bug
-------
``kanban_schedule`` parks a card in ``scheduled`` ("waiting on time, not a
human; not dispatchable").  The only automatic exit from a parked state is
``recompute_ready`` — the promotion pass the dispatcher runs on every tick and
``complete_task`` runs after every completion.  ``recompute_ready`` scanned
``status IN ('todo', 'blocked')`` only, so a card that was parked in
``scheduled`` while its parent was still running stayed ``scheduled`` forever
after the parent finished.  ``unblock_task`` (a human/agent action) was the
only way out, so the card appeared to stall with no owner.

These tests pin down:

* a ``scheduled`` card whose only parent completes is promoted to ``ready``
  by the dispatcher tick (``recompute_ready``), and by ``complete_task``;
* a ``scheduled`` card that has no blocker at all stays parked — ``scheduled``
  remains a deliberate park for orchestrator re-gating, not a state that the
  promotion pass silently drains;
* a worker-initiated (sticky) block keeps its existing behaviour, and a
  ``scheduled`` card whose parent is still unfinished is left alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parent_child(conn, status_of_parent: str = "todo"):
    parent = kb.create_task(conn, title="upstream blocker")
    child = kb.create_task(conn, title="gated card", parents=(parent,))
    if status_of_parent == "done":
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()
    return parent, child


# ---------------------------------------------------------------------------
# The fix: a parked card is promoted when its blocker clears
# ---------------------------------------------------------------------------


def test_scheduled_card_promotes_when_parent_completes(kanban_home: Path) -> None:
    """Dispatcher tick (``recompute_ready``) must re-gate a parked card."""
    with kbc.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.get_task(conn, child).status == "todo"

        # Orchestrator parks the gated card while it waits on the blocker.
        assert kb.schedule_task(conn, child, reason="waiting on upstream blocker")
        assert kb.get_task(conn, child).status == "scheduled"

        # Blocker clears (no dispatcher tick involved).
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()

        promoted = kb.recompute_ready(conn)

        assert promoted >= 1, "the promotion pass must see the parked card"
        assert kb.get_task(conn, child).status == "ready", (
            "a scheduled card whose blocker cleared must land in ready"
        )
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert "promoted" in kinds, "the promotion must be recorded as an event"


def test_complete_task_promotes_scheduled_child(kanban_home: Path) -> None:
    """The completion path (not just the tick) must un-park the child."""
    with kbc.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.schedule_task(conn, child, reason="waiting on upstream blocker")

        assert kb.complete_task(conn, parent, result="blocker finished")

        assert kb.get_task(conn, child).status == "ready"


def test_scheduled_card_stays_parked_while_parent_is_unfinished(kanban_home: Path) -> None:
    """Only the *cleared* blocker promotes; an unfinished one leaves it alone."""
    with kbc.connect() as conn:
        _parent, child = _parent_child(conn)
        assert kb.schedule_task(conn, child, reason="waiting on upstream blocker")

        for _ in range(3):
            kb.recompute_ready(conn)
            assert kb.get_task(conn, child).status == "scheduled"


def test_scheduled_card_without_blocker_stays_parked(kanban_home: Path) -> None:
    """``scheduled`` is still a deliberate park: no blocker => no auto-resume.

    A parentless card parked by an orchestrator has no blocker that could be
    "cleared", so the promotion pass must not drain the column.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deliberately parked")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        assert kb.schedule_task(conn, tid, reason="hold for orchestrator")

        for _ in range(3):
            kb.recompute_ready(conn)
            assert kb.get_task(conn, tid).status == "scheduled"


def test_sticky_block_still_survives_promotion(kanban_home: Path) -> None:
    """Guard against widening the pass into sticky-block territory."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs human review")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify ACL change",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, tid).status == "blocked"
