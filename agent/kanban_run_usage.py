"""Per-run token accounting for dispatcher-owned Kanban workers.

Every run row (``task_runs``) is the unit we price. Until now nothing on that row
said what the run consumed, so a delivered job could not be costed. This module
writes a ``usage`` block into the run's own ``metadata`` column — no new table,
because the column already round-trips through the worker context and every
consumer (dashboard, notifier, build_worker_context) already reads it.

Why the write happens mid-run, not only at completion: the runs we most need to
price are the wasted ones. A worker that hits its runtime cap, crashes or is
reclaimed never reaches a completion handler, so a completion-only stamp would
record nothing for exactly those runs. ``record_response_usage`` calls
:func:`checkpoint_run_usage` after each folded provider response, so the run row
always carries the latest measured cumulative total; the dispatcher's close path
(:func:`normalize_usage_for_close`) then marks it final, partial, or unknown —
never a silent zero.

Tokens are a measurement. Money is not: the provider's invoice is the only
authority for dollars, so ``cost`` stays ``None`` here rather than inventing an
exchange rate or a price-table guess.

Scope: only a dispatcher-owned worker writes (``HERMES_KANBAN_TASK`` +
``HERMES_KANBAN_RUN_ID`` + ``HERMES_KANBAN_DB``). A chat session, a cron job or
a delegate child inherits some of these vars without owning the card, so the
write is skipped unless all three are present and the run id is an integer.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

USAGE_KEY = "usage"
SCHEMA = "kanban-run-usage-v1"

# Outcomes where the run reached its own handoff: the last checkpoint IS the
# run's delivered consumption, so the value can be marked final.
CLEAN_OUTCOMES = frozenset({"completed", "blocked", "scheduled", "review"})


def _scope() -> Optional[tuple[str, int, str]]:
    """``(task_id, run_id, db_path)`` for a dispatcher-owned worker, else None.

    Read straight from the environment the dispatcher pins on the child
    (``kanban_db_dispatch``): the task, the run and the board DB. Absence of any
    one means this process is not that run's owner, and writing would be a
    cross-run mutation.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    db_path = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not task_id or not db_path or not raw_run:
        return None
    try:
        run_id = int(raw_run)
    except ValueError:
        return None
    return task_id, run_id, db_path


def usage_from_agent(agent: Any, *, status: str = "running") -> Optional[dict]:
    """Cumulative token usage of *agent*'s session, or None when unmeasurable.

    ``total_tokens`` is prompt + completion as the session counters fold them
    (``agent/turn_usage.record_response_usage``). A session that made calls but
    never got a usage-bearing response reports ``api_calls`` > 0 with a null
    token count — honest, not a zero.
    """
    if agent is None:
        return None
    total = getattr(agent, "session_total_tokens", None)
    calls = getattr(agent, "session_api_calls", None)
    if total is None and calls is None:
        return None
    return {
        "schema": SCHEMA,
        "status": status,
        "source": "agent-session",
        "total_tokens": int(total) if isinstance(total, int) else None,
        "prompt_tokens": int(getattr(agent, "session_prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(agent, "session_completion_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(agent, "session_cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(agent, "session_cache_write_tokens", 0) or 0),
        "api_calls": int(calls) if isinstance(calls, int) else None,
        "model": getattr(agent, "model", None),
        "provider": getattr(agent, "provider", None),
        "measured_at": int(time.time()),
        # Dollars need the provider's invoice; a price-table guess is not a measurement.
        "cost_usd": None,
    }


def _merge_into_metadata(conn: sqlite3.Connection, run_id: int, usage: dict) -> bool:
    row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return False
    raw = row[0]
    try:
        existing = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing[USAGE_KEY] = usage
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps(existing, ensure_ascii=False), run_id),
    )
    return True


def checkpoint_run_usage(agent: Any) -> bool:
    """Stamp the run's current cumulative usage into its metadata row.

    Best-effort by design: cost accounting must never abort a model turn. A
    failure is logged and the previous checkpoint (if any) stays on the row.
    """
    scope = _scope()
    if scope is None:
        return False
    usage = usage_from_agent(agent)
    if usage is None:
        return False
    task_id, run_id, db_path = scope
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as exc:
        logger.debug("kanban usage checkpoint skipped (open %s): %s", db_path, exc)
        return False
    try:
        with conn:
            # The run must still be open AND belong to this task: a stale worker
            # whose run was reclaimed must not write into a successor's row.
            row = conn.execute(
                "SELECT task_id FROM task_runs WHERE id = ? AND ended_at IS NULL",
                (run_id,),
            ).fetchone()
            if row is None or row[0] != task_id:
                return False
            return _merge_into_metadata(conn, run_id, usage)
    except sqlite3.Error as exc:
        logger.debug("kanban usage checkpoint failed (run %s): %s", run_id, exc)
        return False
    finally:
        conn.close()


def normalize_usage_for_close(
    existing_metadata: Optional[dict], payload: Optional[dict], outcome: str,
) -> dict:
    """Merge the close payload over the run's metadata and settle ``usage``.

    - a checkpoint from a clean outcome is ``final`` (the last measurement is the
      run's delivered consumption);
    - a checkpoint from a cap/crash/reclaim is ``partial`` and says why, because
      the worker may have been killed after its last write;
    - no checkpoint at all is ``unknown`` with an explicit reason. Never 0: a
      silent zero would price a wasted run as free.
    """
    merged = dict(existing_metadata or {})
    if payload:
        merged.update(payload)
    usage = merged.get(USAGE_KEY)
    if isinstance(usage, dict) and usage.get("status") == "running":
        settled = dict(usage)
        if outcome in CLEAN_OUTCOMES:
            settled["status"] = "final"
        else:
            settled["status"] = "partial"
            settled["partial_reason"] = (
                f"run ended as {outcome} after the last checkpoint; "
                "tokens after it are not measured"
            )
        merged[USAGE_KEY] = settled
    elif not isinstance(usage, dict):
        merged[USAGE_KEY] = {
            "schema": SCHEMA,
            "status": "unknown",
            "source": "dispatcher",
            "total_tokens": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "api_calls": None,
            "model": None,
            "provider": None,
            "measured_at": int(time.time()),
            "cost_usd": None,
            "reason": f"no usage checkpoint was written before the run ended as {outcome}",
        }
    return merged
