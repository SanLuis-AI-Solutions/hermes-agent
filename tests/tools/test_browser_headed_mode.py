"""Tests for headed browser mode: config resolution, the headed local launch, and the per-turn
cleanup skip that keeps headed sessions alive between turns.

Salvaged from PR #24064 (fixes #11020 lead bug).
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import browser_launcher as launcher
from tools import browser_session as bs
from tools import browser_session_cloud as bs_cloud


@pytest.fixture(autouse=True)
def _clean_headed_cache(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    bs_cloud.reset_caches()
    yield
    bs_cloud.reset_caches()


# ---------------------------------------------------------------------------
# is_headed_mode resolution
# ---------------------------------------------------------------------------

class TestIsHeadedMode:

    def test_config_true(self):
        cfg = {"browser": {"headed": True}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert bs_cloud.is_headed_mode() is True




# ---------------------------------------------------------------------------
# Per-turn cleanup skip (agent/chat_completion_helpers.cleanup_task_resources)
# ---------------------------------------------------------------------------

def _make_agent(verbose=False):
    return SimpleNamespace(verbose_logging=verbose)


class TestCleanupTaskResourcesHeadedSkip:
    def test_headless_still_cleans_browser(self):
        from agent.chat_completion_helpers import cleanup_task_resources
        with (
            patch("tools.browser_session_cloud.is_headed_mode", return_value=False),
            patch("run_agent.cleanup_vm"),
            patch("run_agent.cleanup_browser") as mock_cb,
            patch(
                "agent.chat_completion_helpers.is_persistent_env",
                return_value=False,
            ),
        ):
            cleanup_task_resources(_make_agent(), "task-x")
            mock_cb.assert_called_once_with("task-x")


    def test_headed_skips_browser_cleanup(self):
        """A headed session survives the turn; the inactivity reaper closes it when idle."""
        from agent.chat_completion_helpers import cleanup_task_resources
        with (
            patch("tools.browser_session_cloud.is_headed_mode", return_value=True),
            patch("run_agent.cleanup_vm"),
            patch("run_agent.cleanup_browser") as mock_cb,
            patch(
                "agent.chat_completion_helpers.is_persistent_env",
                return_value=False,
            ),
        ):
            cleanup_task_resources(_make_agent(), "task-x")
            mock_cb.assert_not_called()


    def test_headed_does_not_skip_vm_cleanup(self):
        """Headed mode only affects the browser; VM teardown is untouched."""
        from agent.chat_completion_helpers import cleanup_task_resources
        with (
            patch("tools.browser_session_cloud.is_headed_mode", return_value=True),
            patch("run_agent.cleanup_vm") as mock_vm,
            patch("run_agent.cleanup_browser"),
            patch(
                "agent.chat_completion_helpers.is_persistent_env",
                return_value=False,
            ),
        ):
            cleanup_task_resources(_make_agent(), "task-x")
            mock_vm.assert_called_once_with("task-x")


# ---------------------------------------------------------------------------
# Headed local launch vs cloud sessions
# ---------------------------------------------------------------------------

class TestHeadedLaunch:
    @pytest.mark.platforms("posix")
    def test_headed_window_in_local_mode(self, monkeypatch, tmp_path):
        """browser.headed + a display: the local Chromium launches without --headless."""
        monkeypatch.setattr(bs_cloud, "is_headed_mode", lambda: True)
        monkeypatch.setattr(bs_cloud, "chromium_args", list)
        monkeypatch.setattr(launcher, "needs_chromium_sandbox_bypass", lambda: False)
        captured = []

        class _Proc:
            pid = 4321

            def poll(self):
                return 1

        def capture_spawn(argv, env, user_data_dir):
            captured.append(argv)
            return _Proc()

        monkeypatch.setattr(launcher, "_spawn", capture_spawn)
        monkeypatch.setattr(launcher, "_start_time", lambda pid: None)
        monkeypatch.setattr(launcher, "_terminate", lambda pid, start_time, proc: None)
        launcher.launch_chromium("headed", executable="chromium", user_data_dir=str(tmp_path / "udd"),
                                 env={"DISPLAY": ":1"}, timeout=1)

        assert len(captured) == 1
        assert not any(arg.startswith("--headless") for arg in captured[0])


    def test_headed_not_applied_in_cloud_mode(self, monkeypatch):
        """Cloud (CDP) sessions never launch a local (headed) Chromium: headed is a local-only knob."""
        monkeypatch.setattr(bs_cloud, "is_headed_mode", lambda: True)
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        provider = SimpleNamespace(create_session=lambda key: {
            "session_name": "cloud", "bb_session_id": "bb-1",
            "cdp_url": "wss://example.invalid/devtools/browser/cloud", "features": {},
        })
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: provider)
        monkeypatch.setattr(launcher, "launch_chromium",
                            lambda *a, **kw: pytest.fail("a cloud session must not launch local Chromium"))
        screen = []
        monkeypatch.setattr(bs, "ensure_screen_for_headed_chromium", lambda: screen.append(1))

        session = bs._create_session_for_key("task1", force_local=False)

        assert session["cdp_url"] == "wss://example.invalid/devtools/browser/cloud"
        assert screen == []
