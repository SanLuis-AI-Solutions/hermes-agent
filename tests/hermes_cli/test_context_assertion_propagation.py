"""A best-effort worker-context render must never swallow ``AssertionError``.

History, so the next reader does not re-litigate it: the old tree rendered a RESUME
block through ``_ctx_resume`` inside ``build_worker_context`` and wrapped that call in
``except Exception``. A reviewer rejected it (e7535d3029d) because the test-isolation
guard raises ``AssertionError`` to stop forbidden I/O against the real home — swallowing
it lets a test pass while the guard has fired, and the isolation it exists to prove is
unproven.

On v0.23.4 the code that carried that guard does not exist: ``_ctx_resume`` is gone from
``hermes_cli/`` and ``tests/`` (the feature that added it, c7a1b4e0b4d, was never part of
the replay onto v0.23.4), and the retry/resume rendering that survives upstream is
``_ctx_prior_attempts`` — called unguarded at ``kanban_db.py:build_worker_context``.

Scope, stated precisely, because the first version of this docstring overclaimed it and a
reviewer caught it: these tests pin the ABSENCE of a handler at two places only — inside
``build_worker_context`` itself, and at the ``_ctx_prior_attempts`` call boundary. They do
not prove the render chain is handler-free. A handler inside a helper is invisible here:
``_ctx_metadata_line`` (``kanban_db_context.py:72``) already carries ``except Exception``
and sits inside this render. A future ``try/except Exception`` at either pinned place
turns these red, which is the point; a handler added deeper is not evidence these tests
would give you.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_context as kbcx


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


def test_assertion_from_the_prior_attempts_render_is_not_swallowed(conn, monkeypatch):
    """The isolation guard's AssertionError must escape the worker-context render.

    ``_ctx_prior_attempts`` is where the retry rendering lives now. If anyone wraps it
    (or ``build_worker_context``) in a best-effort handler, a fired guard becomes an
    invisible pass -- exactly the defect e7535d3029d closed.
    """
    task_id = kb.create_task(conn, title="card", assignee="alice")

    def boom(*_args, **_kwargs):
        raise AssertionError("TEST BUG: the isolation guard fired")

    monkeypatch.setattr(kbcx, "_ctx_prior_attempts", boom)

    with pytest.raises(AssertionError, match="isolation guard fired"):
        kb.build_worker_context(conn, task_id)


def test_ordinary_errors_are_not_silently_turned_into_context_either(conn, monkeypatch):
    """Control: the render is not best-effort at all now.

    Pinned separately so the assertion test above cannot be satisfied by a handler that
    simply re-raises everything under a different name; if the render ever becomes
    best-effort again, both this and the guard rule have to be re-argued in one place.
    """
    task_id = kb.create_task(conn, title="card", assignee="alice")

    def boom(*_args, **_kwargs):
        raise RuntimeError("sqlite went away")

    monkeypatch.setattr(kbcx, "_ctx_prior_attempts", boom)

    with pytest.raises(RuntimeError, match="sqlite went away"):
        kb.build_worker_context(conn, task_id)
