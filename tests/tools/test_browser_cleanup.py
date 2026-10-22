"""Regression tests for browser session cleanup (tools/browser_session_lifecycle.py)."""

import sys
from unittest.mock import patch

from tools import browser_session as bs
from tools import browser_session_lifecycle as lifecycle


class TestBrowserCleanup:
    def setup_method(self):
        self.orig_active_sessions = bs._active_sessions.copy()
        self.orig_session_last_activity = bs._session_last_activity.copy()
        self.orig_cleanup_done = bs._cleanup_done

    def teardown_method(self):
        bs._active_sessions.clear()
        bs._active_sessions.update(self.orig_active_sessions)
        bs._session_last_activity.clear()
        bs._session_last_activity.update(self.orig_session_last_activity)
        bs._cleanup_done = self.orig_cleanup_done

    def test_cleanup_browser_clears_tracking_state(self):
        bs._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": None,
            "launcher_key": "task-1",
            "features": {"local": True},
        }
        bs._session_last_activity["task-1"] = 123.0

        with patch("tools.browser_launcher.stop_browser") as mock_stop:
            lifecycle.cleanup_browser("task-1")

        assert "task-1" not in bs._active_sessions
        assert "task-1" not in bs._session_last_activity
        mock_stop.assert_called_once_with("task-1")


    def test_emergency_cleanup_clears_all_tracking_state(self):
        bs._cleanup_done = False
        bs._active_sessions["task-1"] = {"session_name": "sess-1"}
        bs._active_sessions["task-2"] = {"session_name": "sess-2"}
        bs._session_last_activity["task-1"] = 1.0
        bs._session_last_activity["task-2"] = 2.0

        with (
            patch("tools.browser_session_lifecycle.cleanup_all_browsers") as mock_cleanup_all,
            patch("tools.browser_session_lifecycle.reap_orphaned_browser_sessions") as mock_reap,
        ):
            lifecycle.emergency_cleanup_all_sessions()

        mock_cleanup_all.assert_called_once_with()
        mock_reap.assert_called_once_with()
        assert bs._active_sessions == {}
        assert bs._session_last_activity == {}
        assert bs._cleanup_done is True


class TestInactivityJanitorMultiplex:
    """#86402 / #100738: the process-global janitor thread has no profile scope."""

    def setup_method(self):
        from agent import secret_scope

        self.saved = {
            name: getattr(bs, name).copy()
            for name in (
                "_active_sessions", "_session_last_activity",
                "_session_owner_homes", "_cleanup_failures",
            )
        }
        self.orig_timeout = bs.BROWSER_SESSION_INACTIVITY_TIMEOUT
        bs.BROWSER_SESSION_INACTIVITY_TIMEOUT = 0
        for name in self.saved:
            getattr(bs, name).clear()
        secret_scope.set_multiplex_active(True)

    def teardown_method(self):
        from agent import secret_scope

        secret_scope.set_multiplex_active(False)
        bs.BROWSER_SESSION_INACTIVITY_TIMEOUT = self.orig_timeout
        for name, saved in self.saved.items():
            live = getattr(bs, name)
            live.clear()
            live.update(saved)

    def test_janitor_tears_down_under_owner_profile_scope(self, tmp_path, monkeypatch):
        from agent import secret_scope
        from hermes_constants import (
            get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
        )

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        p1 = tmp_path / "profiles" / "p1"
        p1.mkdir(parents=True)
        (p1 / ".env").write_text("BROWSER_USE_API_KEY=p1-key\n", encoding="utf-8")

        # Profile p1's turn opens the session; the janitor later runs unscoped.
        home_tok = set_hermes_home_override(str(p1))
        scope_tok = secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(p1))
        try:
            lifecycle.update_session_activity("t1")
            bs._active_sessions["t1"] = {"session_name": "s1", "bb_session_id": None,
                                         "launcher_key": "t1", "features": {"local": True}}
        finally:
            secret_scope.reset_secret_scope(scope_tok)
            reset_hermes_home_override(home_tok)
        bs._session_last_activity["t1"] -= 10

        seen = {}

        def fake_stop(launcher_key):
            seen["home"] = str(get_hermes_home())
            seen["key"] = secret_scope.get_secret("BROWSER_USE_API_KEY")

        with patch("tools.browser_launcher.stop_browser", side_effect=fake_stop):
            lifecycle.cleanup_inactive_sessions()

        assert seen == {"home": str(p1), "key": "p1-key"}
        assert "t1" not in bs._session_last_activity
        assert "t1" not in bs._active_sessions
        assert "t1" not in bs._session_owner_homes

    def test_repeated_failures_force_reap_and_close_cloud_session(self):
        from unittest.mock import MagicMock

        bs._active_sessions["t1"] = {"session_name": "s1", "bb_session_id": "bb-1"}
        bs._session_last_activity["t1"] = 1.0
        provider = MagicMock()

        with (
            patch("tools.browser_session_lifecycle.cleanup_single_session", side_effect=RuntimeError("boom")),
            patch("tools.browser_session_cloud.get_cloud_provider", return_value=provider),
        ):
            for _ in range(bs.MAX_INACTIVITY_CLEANUP_FAILURES - 1):
                lifecycle.cleanup_inactive_sessions()
            # An activity touch must NOT reset the failure budget.
            lifecycle.update_session_activity("t1")
            bs._session_last_activity["t1"] = 1.0
            assert bs._cleanup_failures["t1"] == bs.MAX_INACTIVITY_CLEANUP_FAILURES - 1
            assert "t1" in bs._active_sessions
            provider.close_session.assert_not_called()

            lifecycle.cleanup_inactive_sessions()

        provider.close_session.assert_called_once_with("bb-1")
        assert "t1" not in bs._active_sessions
        assert "t1" not in bs._session_last_activity
        assert "t1" not in bs._cleanup_failures


class TestAtexitStopSwallowsInterrupt:
    def test_second_ctrl_c_during_join_does_not_propagate(self, monkeypatch):
        """A second Ctrl+C while the atexit hook waits on the janitor must not escape as a
        traceback (#10764): the thread is a daemon, the interpreter is already exiting."""

        class _InterruptedJoin:
            def join(self, timeout=None):
                raise KeyboardInterrupt

        monkeypatch.setattr(bs, "_cleanup_thread", _InterruptedJoin())
        monkeypatch.setattr(bs, "_cleanup_running", True)
        lifecycle.stop_cleanup_thread()  # must not raise
        assert bs._cleanup_running is False


class TestAtexitLifecycleUnimportable:
    def test_hook_stays_silent_when_lifecycle_import_fails(self, monkeypatch):
        """Mid-`hermes update` the on-disk tree can be half-new, so the atexit hook's import of
        the lifecycle module raises; the hook must stay silent (#112437)."""
        monkeypatch.setitem(sys.modules, "tools.browser_session_lifecycle", None)  # import -> ImportError
        bs._atexit_cleanup()  # must not raise
