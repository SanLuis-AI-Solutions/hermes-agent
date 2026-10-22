"""Wiring regression tests for the browser extension router.

These guard the *registry wiring* — that the ``browser_cdp`` handler (the one core browser tool the router
serves) routes through :func:`tools.browser_extension_router.routed_browser_handler` with the tool's action
name, its raw args, and its identity kwargs, instead of calling the legacy backend directly. The routing
contract itself is tested by ``test_browser_extension_router.py``; here we only pin the plumbing.
"""

import pytest

from tools.registry import registry


@pytest.fixture(autouse=True)
def _route_spy(monkeypatch):
    """Replace the wrapper with a spy that records the route and then runs
    the legacy fallback, so each test proves the handler is wired without
    exercising real routing or a real browser backend."""
    calls = []

    def spy(action, args, *, fallback, task_id=None, session_id=None, tool_call_id=None):
        calls.append(
            {
                "action": action,
                "args": dict(args),
                "task_id": task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            }
        )
        return fallback()

    from tools import browser_cdp_tool

    monkeypatch.setattr(browser_cdp_tool, "routed_browser_handler", spy)
    monkeypatch.setattr(browser_cdp_tool, "browser_cdp", lambda *a, **k: "legacy-cdp")
    return calls


def test_browser_cdp_handler_routes_through_wrapper(_route_spy):
    handler = registry.get_entry("browser_cdp").handler
    args = {"method": "Target.getTargets", "params": {"filter": []}}
    result = handler(dict(args), task_id="task-fixture", session_id="session-fixture")
    assert result == "legacy-cdp"
    route = _route_spy[0]
    assert route["action"] == "browser_cdp"
    assert route["args"] == args
    assert route["task_id"] == "task-fixture"
    assert route["session_id"] == "session-fixture"
