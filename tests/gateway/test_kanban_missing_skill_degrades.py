"""An unresolvable preload skill must not kill a Kanban worker before its first model call.

The dispatcher force-loads ``sdlc-review`` on every review-lane card. When that skill is the only
requested one and the reviewer profile cannot resolve it, ``_run_task_turns`` used to raise
``kanban_skills_unavailable`` (gateway/session_kanban.py:392 at head 3a9ecbb7e05) — the worker exited 1
before any model call, and the board booked a crash with no verdict. That is the exact stack in
managed-worker.log for t_9be7c06e runs 1039/1041/1045/1046 (pids 134764/152004/154060/154416).

A missing skill body is a smaller failure than a dead worker: warn, name the missing skills, and run the
turn with the card body + kanban lifecycle prompt it already carries. The turn must still fail loudly
when the *turn* fails, so the exit-code tests below pin both directions.
"""
import json

import pytest

from agent import skill_commands
from gateway import session_kanban
from tools import file_tools


class _Agent:
    session_id = "local-test-worker"

    def __init__(self):
        self.prompts = []

    def run_conversation(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return {"completed": True, "final_response": "reviewed"}


def _context(**overrides):
    context = {"skills": ["sdlc-review"], "context": "card body", "task_id": "t_x", "goal_mode": False}
    context.update(overrides)
    return context


def _frame(context):
    return {"text": "work kanban task t_x", "policy": {"kanban_json": json.dumps(context)}}


def test_only_skill_missing_warns_and_still_runs_the_turn(monkeypatch, caplog):
    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt",
                        lambda *_a, **_k: ("", [], ["sdlc-review"]))
    agent = _Agent()

    result = session_kanban._run_task_turns(agent, {"text": "work kanban task t_x"}, [], _context())

    assert result == {"completed": True, "final_response": "reviewed"}
    assert agent.prompts == ["card body\n\nwork kanban task t_x"]
    assert any("continuing without: sdlc-review" in record.getMessage() for record in caplog.records)


def test_loaded_and_missing_mix_runs_with_the_loaded_block(monkeypatch):
    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt",
                        lambda *_a, **_k: ("[skill: sdlc-review]", ["sdlc-review"], ["typo-skill"]))
    agent = _Agent()

    session_kanban._run_task_turns(agent, {"text": "request"}, [],
                                   _context(skills=["sdlc-review", "typo-skill"], task_id="t_y"))

    assert agent.prompts == ["[skill: sdlc-review]\n\ncard body\n\nrequest"]


def test_a_refused_write_is_a_tool_result_not_a_death(tmp_path):
    """The stale-overwrite guard keeps refusing — it returns the refusal as the tool result, so the
    worker can read the file and continue instead of the exception killing the process."""
    target = tmp_path / "retry_history_replay.py"
    target.write_text("print('author wrote this first')\n", encoding="utf-8")
    task_id = "t_refusal_fresh"

    result = file_tools.write_file_tool(str(target), "print('reviewer overwrite')\n", task_id=task_id)

    payload = json.loads(result)
    assert payload["stale_write_blocked"] is True
    assert payload["error"].startswith(f"Refusing to overwrite {target}")
    assert "The file was NOT modified" in payload["error"]
    # The guard still held: the author's bytes are untouched.
    assert target.read_text(encoding="utf-8") == "print('author wrote this first')\n"


def test_completed_run_records_exit_zero(monkeypatch):
    recorded = []
    monkeypatch.setattr(session_kanban, "_run_task_turns",
                        lambda *_a, **_k: {"completed": True, "final_response": "verdict"})
    monkeypatch.setattr(session_kanban, "_record_worker_result",
                        lambda context, code, last_output: recorded.append((code, last_output)))
    context = _context(db="unused", run_id=1, claim_lock="lock")

    result = session_kanban._run_worker_turns(_Agent(), _frame(context), [])

    assert result["completed"] is True
    assert recorded and recorded[0][0] == 0


def test_genuine_fault_still_fails_the_run(monkeypatch):
    recorded = []

    def _boom(*_a, **_k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(session_kanban, "_run_task_turns", _boom)
    monkeypatch.setattr(session_kanban, "_record_worker_result",
                        lambda context, code, last_output: recorded.append((code, last_output)))
    context = _context(db="unused", run_id=1, claim_lock="lock")

    with pytest.raises(RuntimeError, match="provider exploded"):
        session_kanban._run_worker_turns(_Agent(), _frame(context), [])

    assert recorded and recorded[0][0] == 1
