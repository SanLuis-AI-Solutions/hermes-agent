"""Proof harness for per-run Kanban usage accounting (agent/kanban_run_usage.py).

Exercises the real code paths against a throwaway DB:
  clean   -> checkpoint, then _end_run(completed)      => usage.status final, tokens kept
  capped  -> checkpoint, then _end_run(timed_out)      => usage.status partial, tokens kept
  crashed -> checkpoint, then _end_run(crashed)        => usage.status partial, tokens kept
  crashed/unmeasured -> no checkpoint, _end_run(crashed) => usage.status unknown + reason (never 0)
  stale   -> checkpoint against an already-ended run    => refused, successor row untouched
"""
import json
import os
import sqlite3
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DB = os.path.join(tempfile.mkdtemp(prefix="kanban-usage-proof-"), "board.db")
SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, current_run_id INTEGER);
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, status TEXT NOT NULL,
  outcome TEXT, summary TEXT, error TEXT, metadata TEXT, ended_at INTEGER, claim_expires INTEGER
);
"""


def stub_agent(total, prompt, completion, calls, model="deepseek-v4.1-flash:1017", provider="ollama-cloud"):
    return types.SimpleNamespace(
        session_total_tokens=total, session_prompt_tokens=prompt,
        session_completion_tokens=completion, session_cache_read_tokens=0,
        session_cache_write_tokens=0, session_api_calls=calls, model=model, provider=provider)


def new_run(conn, run_id, task):
    conn.execute("INSERT INTO tasks(id, current_run_id) VALUES(?, ?)", (task, run_id))
    conn.execute("INSERT INTO task_runs(id, task_id, status) VALUES(?, ?, 'running')", (run_id, task))
    conn.commit()


def usage(conn, run_id):
    raw = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]
    return json.loads(raw)["usage"] if raw else None


def main():
    from hermes_cli import kanban_db as kb
    from agent import kanban_run_usage as kru

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    failures = []
    results = {}

    def check(name, cond, detail):
        print(f"{'PASS' if cond else 'FAIL'} {name}: {detail}")
        if not cond:
            failures.append(name)

    # clean: checkpoint then completion
    os.environ.update(HERMES_KANBAN_TASK="t_clean", HERMES_KANBAN_RUN_ID="101", HERMES_KANBAN_DB=DB)
    new_run(conn, 101, "t_clean")
    ok = kru.checkpoint_run_usage(stub_agent(120000, 90000, 30000, 12))
    mid = usage(conn, 101)
    kb._end_run(conn, "t_clean", outcome="completed", summary="done")
    conn.commit()
    end = usage(conn, 101)
    results["clean"] = end
    check("clean.checkpoint_written", ok and mid["total_tokens"] == 120000, f"mid={mid}")
    check("clean.final", end["status"] == "final" and end["total_tokens"] == 120000
          and end["model"] == "deepseek-v4.1-flash:1017" and end["cost_usd"] is None, f"{end}")

    # capped: checkpoint then the dispatcher's cap close
    os.environ.update(HERMES_KANBAN_TASK="t_capped", HERMES_KANBAN_RUN_ID="102")
    new_run(conn, 102, "t_capped")
    kru.checkpoint_run_usage(stub_agent(50749 + 39658, 50749, 39658, 37))
    kb._end_run(conn, "t_capped", outcome="timed_out", error="elapsed 1555s > limit 1500s",
                metadata={"pid": 48700, "elapsed_seconds": 1555, "limit_seconds": 1500})
    conn.commit()
    end = usage(conn, 102)
    meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=102").fetchone()[0])
    results["capped"] = {**end, "elapsed_seconds": meta.get("elapsed_seconds")}
    check("capped.partial_kept", end["status"] == "partial" and end["total_tokens"] == 90407
          and meta.get("elapsed_seconds") == 1555 and "partial_reason" in end, f"{results['capped']}")

    # crashed with a measured checkpoint
    os.environ.update(HERMES_KANBAN_TASK="t_crashed", HERMES_KANBAN_RUN_ID="103")
    new_run(conn, 103, "t_crashed")
    kru.checkpoint_run_usage(stub_agent(20520 + 4678, 20520, 4678, 8))
    kb._end_run(conn, "t_crashed", outcome="crashed", metadata={"pid": 92876, "exit_code": 1})
    conn.commit()
    end = usage(conn, 103)
    results["crashed"] = end
    check("crashed.partial_kept", end["status"] == "partial" and end["total_tokens"] == 25198, f"{end}")

    # crashed with no measurement: explicit unknown, never zero
    os.environ.update(HERMES_KANBAN_TASK="t_unmeasured", HERMES_KANBAN_RUN_ID="104")
    new_run(conn, 104, "t_unmeasured")
    kb._end_run(conn, "t_unmeasured", outcome="crashed", metadata={"pid": 1})
    conn.commit()
    end = usage(conn, 104)
    results["unmeasured"] = end
    check("unmeasured.unknown_not_zero", end["status"] == "unknown" and end["total_tokens"] is None
          and "no usage checkpoint" in end["reason"], f"{end}")

    # stale worker must not write over an ended run
    os.environ.update(HERMES_KANBAN_TASK="t_clean", HERMES_KANBAN_RUN_ID="101")
    wrote = kru.checkpoint_run_usage(stub_agent(999, 999, 0, 1))
    after = usage(conn, 101)
    check("stale.refused", wrote is False and after["total_tokens"] == 120000, f"wrote={wrote} usage={after}")

    conn.close()
    print("\nDB:", DB)
    print(json.dumps(results, indent=2))
    print("\nRESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
