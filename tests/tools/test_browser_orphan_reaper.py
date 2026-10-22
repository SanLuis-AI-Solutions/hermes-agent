"""Tests for the browser orphan reaper: Chromium launched by a Hermes process that exited without
cleaning up is found through the launcher's host-wide records and stopped, but only after its
identity is proven (``tools.browser_launcher.reap_orphaned_browsers``, driven by
``browser_session_lifecycle.reap_orphaned_browser_sessions``)."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import browser_launcher as launcher
from tools import browser_session as bs
from tools import browser_session_lifecycle as lifecycle

_DEAD_OWNER = 99999
_LIVE_OTHER_OWNER = 88888


@pytest.fixture
def records(tmp_path, monkeypatch):
    """Host-wide launch records in a temp dir; no real process is ever probed or signalled."""
    path = tmp_path / "records"
    path.mkdir()
    monkeypatch.setattr(launcher, "_records_dir", lambda: path)
    monkeypatch.setattr(launcher, "_browsers", {})
    monkeypatch.setattr(launcher, "_procs", {})
    terminated = []
    monkeypatch.setattr(launcher, "_terminate", lambda pid, start_time, proc: terminated.append(pid))
    return SimpleNamespace(dir=path, terminated=terminated)


def _record(records, *, pid: int, udd: str, owner_pid: int = _DEAD_OWNER, start_time=777,
            ephemeral: bool = True) -> Path:
    path = records.dir / f"rec-{pid}.json"
    path.write_text(json.dumps({"owner_pid": owner_pid, "owner_start_time": None, "pid": pid,
                                "start_time": start_time, "user_data_dir": udd, "home_key": "x",
                                "ephemeral": ephemeral}))
    return path


def _alive(*pids):
    return lambda pid: pid in pids


class TestReapOrphanedBrowserSessions:
    """Tests for the orphan reaper function."""

    def test_real_profile_browser_is_reaped_when_owner_is_dead_and_its_copy_kept(self, records, tmp_path,
                                                                                 monkeypatch):
        """#100855: the shared real-profile browser is not ``<prefix>_<hex>`` named, so it must still be
        found. Same owner-liveness rule as every other lane: dead owner => reaped. Its persistent
        profile copy is never deleted by the reap."""
        from tools.browser_session_real_profile import REAL_PROFILE_KEY

        copy_dir = tmp_path / REAL_PROFILE_KEY
        copy_dir.mkdir()
        rec = _record(records, pid=4242, udd=str(copy_dir), ephemeral=False)
        monkeypatch.setattr(launcher, "_pid_alive", _alive(4242))  # browser alive, owning hermes gone
        monkeypatch.setattr(launcher, "_start_time", lambda pid: 777)
        monkeypatch.setattr(launcher, "_cmdline_names_dir", lambda pid, udd: True)
        monkeypatch.setattr("tools.browser_lightpanda.reap_orphaned_lightpanda", lambda: 0)

        assert lifecycle.reap_orphaned_browser_sessions() == 1

        assert records.terminated == [4242]
        assert not rec.exists()
        assert copy_dir.exists()

    def test_unfingerprintable_browser_is_refused(self, records, tmp_path, monkeypatch):
        """No start-time fingerprint -> the kill is refused (fail closed).

        A PID whose identity cannot be pinned could be recycled between the verify and the
        tree-kill, so it must be left alone.
        """
        _record(records, pid=12345, udd=str(tmp_path / "p"), start_time=None)
        monkeypatch.setattr(launcher, "_pid_alive", _alive(12345))
        monkeypatch.setattr(launcher, "_start_time", lambda pid: None)
        monkeypatch.setattr(launcher, "_cmdline_names_dir", lambda pid, udd: True)

        launcher.reap_orphaned_browsers()

        assert records.terminated == []


    def test_corrupt_record_is_cleaned(self, records, monkeypatch):
        """A record that is not a JSON object is removed and nothing is signalled."""
        bad = records.dir / "corrupt.json"
        bad.write_text("not-json")
        listed = records.dir / "list.json"
        listed.write_text("[1, 2]")

        assert launcher.reap_orphaned_browsers() == 0
        assert not bad.exists() and not listed.exists()
        assert records.terminated == []


class TestOwnerPidCrossProcess:
    """Owner-based cross-process safe reaping: concurrent Hermes processes never reap each other's
    browsers, so orphans from crashed processes are reaped without touching live ones."""

    def test_alive_owner_is_not_reaped_even_when_untracked(self, records, tmp_path, monkeypatch):
        """A browser with a live owner is NOT reaped, even though this process does not track it.

        This is the core cross-process safety check: Process B scanning while Process A is using a
        browser must not kill A's browser.
        """
        rec = _record(records, pid=12345, udd=str(tmp_path / "p"), owner_pid=_LIVE_OTHER_OWNER)
        monkeypatch.setattr(launcher, "_pid_alive", _alive(12345, _LIVE_OTHER_OWNER))
        monkeypatch.setattr(launcher, "_start_time", lambda pid: 777)
        monkeypatch.setattr(launcher, "_cmdline_names_dir", lambda pid, udd: True)

        assert launcher.reap_orphaned_browsers() == 0
        assert records.terminated == []
        assert rec.exists()


class TestReaperIdentityGuard:
    """The #14073 guard: before signalling a live PID the reaper confirms it really is the recorded
    browser (its argv names the recorded ``--user-data-dir``), defeating planted records and
    recycled PIDs that would otherwise become an arbitrary same-user DoS."""

    class _FakeProc:
        def __init__(self, cmdline):
            self._cmdline = cmdline

        def cmdline(self):
            return self._cmdline

    def _run(self, cmdline, udd, *, no_such=False, access_denied=False):
        import psutil

        def _factory(pid):
            if no_such:
                raise psutil.NoSuchProcess(pid)
            if access_denied:
                raise psutil.AccessDenied(pid)
            return self._FakeProc(cmdline)

        with patch("psutil.Process", side_effect=_factory):
            return launcher._cmdline_names_dir(12345, udd)

    def test_real_browser_bound_via_cmdline_is_reapable(self):
        udd = "/home/u/.hermes/cache/browser/chromium/abc"
        assert self._run(["chrome", "--remote-debugging-port=0", f"--user-data-dir={udd}"], udd) is True

    def test_recycled_pid_browser_not_bound_to_our_dir_is_refused(self):
        """A Chromium for a DIFFERENT profile dir must not be reaped (PID reuse / an unrelated browser)."""
        udd = "/home/u/.hermes/cache/browser/chromium/abc"
        assert self._run(["chrome", "--user-data-dir=/home/u/.hermes/cache/browser/chromium/OTHER"], udd) is False

    def test_recycled_pid_carrying_only_the_dir_as_a_bare_token_is_refused(self):
        """The dir anywhere in argv is not a binding (#116884): a recycled PID whose argv merely mentions
        it (a grep, a shell) must not pass; only the exact ``--user-data-dir=<dir>`` token binds."""
        udd = "/home/u/.hermes/cache/browser/chromium/abc"
        assert self._run(["grep", udd, "/var/log/syslog"], udd) is False
        assert self._run(["grep", "abc", "/var/log/syslog"], udd) is False
        # Control: the normalized full path as the flag token still binds.
        assert self._run(["chrome", f"--user-data-dir={udd}/"], udd) is True

    def test_unreadable_identity_fails_closed(self):
        udd = "/home/u/.hermes/cache/browser/chromium/abc"
        assert self._run([], udd, no_such=True) is False
        assert self._run([], udd, access_denied=True) is False


class TestLeakedBrowserOfThisProcess:
    """A record owned by THIS (live) process but no longer tracked by it is a leak: reap it. A browser
    this process still tracks is never reaped, however old."""

    def test_untracked_own_browser_is_reaped(self, records, tmp_path, monkeypatch):
        rec = _record(records, pid=12345, udd=str(tmp_path / "p"), owner_pid=os.getpid())
        monkeypatch.setattr(launcher, "_pid_alive", _alive(12345, os.getpid()))
        monkeypatch.setattr(launcher, "_start_time", lambda pid: 777)
        monkeypatch.setattr(launcher, "_cmdline_names_dir", lambda pid, udd: True)

        assert launcher.reap_orphaned_browsers() == 1
        assert records.terminated == [12345]
        assert not rec.exists()

    def test_tracked_own_browser_is_spared(self, records, tmp_path, monkeypatch):
        udd = str(tmp_path / "p")
        rec = _record(records, pid=12345, udd=udd, owner_pid=os.getpid())
        launcher._browsers["mine"] = launcher.LaunchedBrowser(
            key="mine", pid=12345, start_time=777, cdp_url="ws://127.0.0.1:1/devtools/browser/x",
            http_url="http://127.0.0.1:1", user_data_dir=udd, owned=True, ephemeral=True, home_key="x")
        monkeypatch.setattr(launcher, "_pid_alive", _alive(12345, os.getpid()))
        monkeypatch.setattr(launcher, "_start_time", lambda pid: 777)
        monkeypatch.setattr(launcher, "_cmdline_names_dir", lambda pid, udd: True)

        assert launcher.reap_orphaned_browsers() == 0
        assert records.terminated == []
        assert rec.exists()

    def test_identity_guard_still_gates_the_leak_path(self, records, tmp_path, monkeypatch):
        """The leak path must not bypass the argv identity check."""
        _record(records, pid=12345, udd=str(tmp_path / "p"), owner_pid=os.getpid())
        monkeypatch.setattr(launcher, "_pid_alive", _alive(12345, os.getpid()))
        monkeypatch.setattr(launcher, "_start_time", lambda pid: 777)
        monkeypatch.setattr(launcher, "_cmdline_names_dir", lambda pid, udd: False)

        assert launcher.reap_orphaned_browsers() == 0
        assert records.terminated == []


class TestPeriodicOrphanReap:
    """The reaper must run repeatedly, not only at cleanup-thread startup.

    A startup-only reap can never recover from a leak that appears *after*
    boot — which is exactly what happens in a hermes process that stays up
    for days.
    """

    def test_reaper_runs_on_every_interval_not_just_startup(self):
        cycles_to_run = 21
        reap_calls = []
        remaining = {"n": cycles_to_run}

        def fake_cleanup():
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                bs._cleanup_running = False

        orig_running = bs._cleanup_running
        bs._cleanup_running = True
        try:
            with patch("tools.browser_session_lifecycle.reap_orphaned_browser_sessions",
                       side_effect=lambda: reap_calls.append(1)), \
                 patch("tools.browser_session_lifecycle.cleanup_inactive_sessions",
                       side_effect=fake_cleanup), \
                 patch("tools.browser_session_lifecycle.time.sleep"):
                lifecycle._janitor()
        finally:
            bs._cleanup_running = orig_running

        assert len(reap_calls) > 1, "startup-only reap would give exactly 1"
