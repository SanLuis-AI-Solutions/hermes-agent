"""The environment a worker was handed must be readable from the run that recorded it.

``_set_worker_pid`` publishes the pid; the ``env_probe`` it persists beside it
answers "which process set this variable?" from the database instead of a guess.
Four strings only — never tokens, never the full environ — and an unset value is
``""`` rather than a missing key, so a reader can tell "unset" from "not probed".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PROBE_KEYS = {"hermes_home", "hermes_scratch_dir", "tmpdir", "scratch_dir_effective"}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _latest_run_metadata(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, "the spawn path must have opened a run"
    return json.loads(row["metadata"] or "{}")


def test_spawn_records_env_probe_on_the_run(kanban_home, all_assignees_spawnable):
    """A spawn that returns a pid leaves the four probe strings on that run."""
    def fake_spawn(task, workspace, board=None):
        # The default spawn builds the env and hands it back exactly this way.
        task.env_probe = kbd._env_probe({
            "HERMES_HOME": "/profiles/alice",
            "HERMES_SCRATCH_DIR": "/profiles/alice/cache/scratch",
            "TMPDIR": "/scratch/tmp",
            "TMP": "/ignored-when-tmpdir-is-set",
        })
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="probe card", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        meta = _latest_run_metadata(conn, tid)
        spawned = [e for e in kb.list_events(conn, tid) if e.kind == "spawned"]

    probe = meta["env_probe"]
    assert set(probe) == PROBE_KEYS
    assert all(isinstance(value, str) for value in probe.values())
    assert probe["hermes_home"] == "/profiles/alice"
    assert probe["hermes_scratch_dir"] == "/profiles/alice/cache/scratch"
    assert probe["tmpdir"] == "/scratch/tmp"  # TMPDIR beats TMP
    # It rides on the same transition that publishes the pid.
    assert spawned and spawned[-1].payload["pid"] == 4242


def test_env_probe_records_absent_scratch_dir_as_empty_string(kanban_home, all_assignees_spawnable):
    """HERMES_SCRATCH_DIR is usually unset: the key must exist as ``""``, not vanish."""
    def fake_spawn(task, workspace, board=None):
        task.env_probe = kbd._env_probe({"HERMES_HOME": "/profiles/bob"})
        return 4243

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="probe card without scratch dir", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        probe = _latest_run_metadata(conn, tid)["env_probe"]

    assert set(probe) == PROBE_KEYS
    assert probe["hermes_scratch_dir"] == ""
    assert probe["tmpdir"] == ""
    assert all(isinstance(value, str) for value in probe.values())


def test_env_probe_falls_back_through_tmp_and_temp():
    """``tmpdir`` is the first non-empty of TMPDIR / TMP / TEMP."""
    assert kbd._env_probe({"TMP": "/tmp-b", "TEMP": "/tmp-c"})["tmpdir"] == "/tmp-b"
    assert kbd._env_probe({"TEMP": "/tmp-c"})["tmpdir"] == "/tmp-c"
