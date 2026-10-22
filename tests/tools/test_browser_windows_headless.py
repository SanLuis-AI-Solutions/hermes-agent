"""Regression for #64867: local Chromium stays headless on Windows Desktop.

Windows Desktop opened a blank top-level window during local browser automation: anything that
resolves the launch to headed turns local mode's documented "zero-cost headless Chromium" into a
visible browser over the Desktop chat. ``browser_launcher._resolve_headed`` keeps Windows headless
unless ``browser.headed`` is on, and the launch argv carries ``--headless=new``. These run only where
the behaviour lives, on the Windows lane (the Linux display rule is covered in test_browser_launcher.py).
"""

from pathlib import Path

import pytest

from tools import browser_launcher as launcher


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr("tools.browser_session_cloud.chromium_args", list)
    yield


@pytest.mark.platforms("windows")
def test_windows_stays_headless_unless_headed_is_configured(monkeypatch):
    monkeypatch.setattr("tools.browser_session_cloud.is_headed_mode", lambda: False)
    assert launcher._resolve_headed(None, {"PATH": "C:/Windows"}) is False
    argv = launcher.build_chromium_argv("chrome.exe", "C:/p", packaged=True, headed=False)
    assert "--headless=new" in argv


@pytest.mark.platforms("windows")
def test_explicit_headed_setting_is_never_overridden(monkeypatch):
    # Windows always has a desktop session: an opted-in headed launch needs no DISPLAY.
    monkeypatch.setattr("tools.browser_session_cloud.is_headed_mode", lambda: True)
    assert launcher._resolve_headed(None, {}) is True
    assert launcher._resolve_headed(True, {}) is True


@pytest.mark.platforms("windows")
def test_local_launch_argv_is_headless(monkeypatch, tmp_path):
    """A local launch's real argv carries --headless=new and no headed window (#64867)."""
    monkeypatch.setattr("tools.browser_session_cloud.is_headed_mode", lambda: False)
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
    browser, err = launcher.launch_chromium("win-headless", executable="chrome.exe",
                                            user_data_dir=str(tmp_path / "udd"), env={}, timeout=1)

    assert browser is None and "exited" in err  # the fake exits at once; only the argv matters
    assert len(captured) == 1
    assert "--headless=new" in captured[0]
