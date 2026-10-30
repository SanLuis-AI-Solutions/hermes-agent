"""Regression tests for the ``scheduled`` no-exit-path defect.

``recompute_ready`` only scanned ``status IN ('todo', 'blocked')``, so a card
parked in ``scheduled`` behind a still-open parent (a ``task_links`` edge)
never left ``scheduled`` after that parent reached ``done`` / ``archived``:
no timer revisits the row and the dispatcher never claims ``scheduled``.
Live instance: board ``phase1d-canary``, card ``t_3ae8944d`` (parent
``t_ae053f25`` archived, card still ``scheduled``).

These tests pin the fix down:

* ``schedule_task`` records the parents that were NOT terminal at park time
  under ``blocked_by`` in the ``scheduled`` event payload.
* ``recompute_ready`` releases such a card once every recorded blocker is
  terminal, and emits a ``promoted`` event carrying ``resolved_blockers``.
* A still-open recorded blocker keeps the card parked.
* Operator parks (no open parent, so no ``blocked_by``) are never released.
* Legacy ``scheduled`` rows (payload without ``blocked_by``) are never
  released — that is the safety property, not an oversight.
* A recorded blocker whose task row was deleted counts as resolved.
"""

from __future__ import annotations

import json
import time
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


def _newest_event(conn, task_id: str) -> tuple[str, dict | None]:
    """``(kind, payload)`` of the newest ``task_events`` row for a task."""
    row = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    assert row is not None, f"no task_events row for {task_id}"
    return row["kind"], (json.loads(row["payload"]) if row["payload"] else None)


def _mark_done(conn, task_id: str) -> None:
    """Write the ``done`` transition directly.

    ``complete_task`` runs its own ``recompute_ready`` at the tail, which would
    consume the release before the test can observe ``recompute_ready``'s
    return value. Same DB state, one fewer dispatcher tick.
    """
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), task_id),
        )


def test_dependency_parked_card_released_when_only_parent_completes(
    kanban_home: Path,
) -> None:
    """The defect: park behind an open parent, parent finishes, card must move."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream")
        child = kb.create_task(conn, title="downstream", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"

        assert kb.schedule_task(conn, child) is True
        assert kb.get_task(conn, child).status == "scheduled"
        kind, payload = _newest_event(conn, child)
        assert kind == "scheduled"
        assert payload["blocked_by"] == [parent]

        _mark_done(conn, parent)

        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, child).status == "ready"
        kind, payload = _newest_event(conn, child)
        assert kind == "promoted"
        assert payload["resolved_blockers"] == [parent]
        # ``ready`` keeps the legacy event style: no ``status`` key.
        assert "status" not in payload

        # Idempotent: a second tick has nothing left to release.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "ready"


def test_open_blocker_keeps_dependency_parked_card_scheduled(
    kanban_home: Path,
) -> None:
    """A recorded blocker that is still open must not release the card."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream")
        child = kb.create_task(conn, title="downstream", parents=[parent])
        assert kb.schedule_task(conn, child) is True
        assert kb.get_task(conn, child).status == "scheduled"

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "scheduled"


def test_operator_park_is_not_released(kanban_home: Path) -> None:
    """Operator/Founder park: no open parent, so no ``blocked_by`` — stays parked."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="already finished")
        _mark_done(conn, parent)
        child = kb.create_task(conn, title="parked by a human", parents=[parent])
        assert kb.get_task(conn, child).status == "ready"

        assert kb.schedule_task(conn, child, reason="run next week") is True
        assert kb.get_task(conn, child).status == "scheduled"
        kind, payload = _newest_event(conn, child)
        assert kind == "scheduled"
        assert "blocked_by" not in payload

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "scheduled"


def test_legacy_scheduled_row_without_blocked_by_is_not_released(
    kanban_home: Path,
) -> None:
    """A pre-fix ``scheduled`` row carries no blocker list: never auto-released."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream")
        child = kb.create_task(conn, title="legacy parked", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"

        # Shape written by builds that predate ``blocked_by``.
        with kb.write_txn(conn):
            kb._append_event(conn, child, "scheduled", {"reason": "legacy"})
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?", (child,),
            )
        assert kb.get_task(conn, child).status == "scheduled"

        _mark_done(conn, parent)

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "scheduled"


def test_recorded_blocker_with_deleted_task_row_counts_as_resolved(
    kanban_home: Path,
) -> None:
    """A recorded parent that no longer exists cannot block the card forever."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream")
        child = kb.create_task(conn, title="downstream", parents=[parent])
        assert kb.schedule_task(conn, child) is True
        kind, payload = _newest_event(conn, child)
        assert payload["blocked_by"] == [parent]

        with kb.write_txn(conn):
            conn.execute("DELETE FROM tasks WHERE id = ?", (parent,))

        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, child).status == "ready"
        kind, payload = _newest_event(conn, child)
        assert kind == "promoted"
        assert payload["resolved_blockers"] == [parent]
