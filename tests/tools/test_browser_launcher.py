"""Behaviour contracts of the Python Chromium launcher (tools/browser_launcher.py).

A fake ``chromium`` (a Python script) stands in for the real binary: it writes ``DevToolsActivePort``
into its ``--user-data-dir`` exactly like Chromium and records its argv, so every path through the
launcher runs for real (spawn, readiness poll, registry, records, parent-first stop, orphan reap).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tools import browser_launcher as launcher

_FAKE_CHROMIUM = textwrap.dedent('''\
    #!{python}
    import json, os, signal, subprocess, sys, time
    udd = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir="))
    os.makedirs(udd, exist_ok=True)
    log = os.path.join(udd, "order.log")
    with open(os.path.join(udd, "argv.json"), "w") as f:
        json.dump(sys.argv, f)
    # A helper child like Chromium's zygote: records when IT is told to stop.
    child = subprocess.Popen([sys.executable, "-c",
        "import os,signal,sys,time\\n"
        "def h(*_):\\n"
        "    open(sys.argv[1],'a').write('child\\\\n'); sys.exit(0)\\n"
        "signal.signal(signal.SIGTERM, h)\\n"
        "parent = os.getppid()\\n"
        "while os.getppid() == parent: time.sleep(0.1)\\n"
        "time.sleep(5)\\n", log])  # outlives a SIGKILLed parent briefly, then exits (no leak)
    def on_term(*_):
        open(log, "a").write("parent\\n")
        sys.exit(0)  # leaves the child running, as a browser mid-shutdown would
    signal.signal(signal.SIGTERM, on_term)
    with open(os.path.join(udd, "DevToolsActivePort"), "w") as f:
        f.write("45678\\n/devtools/browser/fake-id\\n")
    time.sleep(600)
''')


@pytest.fixture
def fake_chromium(tmp_path, monkeypatch):
    script = tmp_path / "chromium"
    script.write_text(_FAKE_CHROMIUM.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setattr(launcher, "_resolve_executable", lambda: str(script))
    monkeypatch.setattr(launcher, "needs_chromium_sandbox_bypass", lambda: False)
    records = tmp_path / "records"
    records.mkdir()
    monkeypatch.setattr(launcher, "_records_dir", lambda: records)
    monkeypatch.setattr("tools.browser_session_cloud.chromium_args", list)
    monkeypatch.setattr("tools.browser_session_cloud.is_headed_mode", lambda: False)
    yield script
    launcher.stop_all_browsers()


def _argv(browser) -> list[str]:
    return json.loads(Path(browser.user_data_dir, "argv.json").read_text())


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_launch_reads_devtools_port_and_runs_headless_on_an_ephemeral_home_profile(fake_chromium):
    browser, err = launcher.launch_chromium("task-a", env=dict(os.environ))
    assert err is None
    assert browser.cdp_url == "ws://127.0.0.1:45678/devtools/browser/fake-id"
    assert browser.http_url == "http://127.0.0.1:45678"
    assert browser.owned and browser.ephemeral
    from hermes_constants import get_hermes_home
    assert Path(browser.user_data_dir).parent == Path(get_hermes_home()) / "cache" / "browser" / "chromium"
    argv = _argv(browser)
    assert "--remote-debugging-port=0" in argv
    assert f"--user-data-dir={browser.user_data_dir}" in argv
    assert "--headless=new" in argv
    assert argv[-1] == "about:blank"


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_one_browser_per_key_and_separate_browsers_for_separate_keys(fake_chromium):
    a1, _ = launcher.launch_chromium("named-a", env=dict(os.environ))
    a2, _ = launcher.launch_chromium("named-a", env=dict(os.environ))
    b, _ = launcher.launch_chromium("named-b", env=dict(os.environ))
    assert a1.pid == a2.pid
    assert b.pid != a1.pid
    assert b.user_data_dir != a1.user_data_dir


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_stop_signals_the_browser_before_its_helpers_and_removes_the_ephemeral_profile(fake_chromium):
    browser, _ = launcher.launch_chromium("stop-me", env=dict(os.environ))
    order_log = Path(browser.user_data_dir, "order.log")
    snapshot = order_log.parent.parent / "order-snapshot.log"
    # The profile dir is removed on stop; mirror the log before it goes.
    real_rmtree = launcher.shutil.rmtree

    def keep_log(path, *a, **k):
        if Path(path) == Path(browser.user_data_dir) and order_log.exists():
            snapshot.write_text(order_log.read_text())
        return real_rmtree(path, *a, **k)

    launcher.shutil.rmtree = keep_log
    try:
        launcher.stop_browser("stop-me")
    finally:
        launcher.shutil.rmtree = real_rmtree
    assert snapshot.read_text().splitlines() == ["parent", "child"]
    assert not Path(browser.user_data_dir).exists()
    assert launcher.get_browser("stop-me") is None


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_a_dead_browser_is_dropped_so_the_next_launch_starts_a_new_one(fake_chromium):
    first, _ = launcher.launch_chromium("crashy", env=dict(os.environ))
    os.kill(first.pid, signal.SIGKILL)
    launcher._procs["crashy"].wait(timeout=5)
    assert launcher.get_browser("crashy") is None
    second, err = launcher.launch_chromium("crashy", env=dict(os.environ))
    assert err is None and second.pid != first.pid


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_launch_failure_reports_the_exit_and_leaves_nothing_registered(tmp_path, monkeypatch, fake_chromium):
    broken = tmp_path / "broken-chromium"
    broken.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('No usable sandbox!')\nsys.exit(7)\n")
    broken.chmod(0o755)
    browser, err = launcher.launch_chromium("broken", executable=str(broken), env=dict(os.environ), timeout=10)
    assert browser is None
    assert "exited with code 7" in err and "--no-sandbox" in err
    assert launcher.get_browser("broken") is None


def _write_orphan_record(records: Path, pid: int, udd: str) -> Path:
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    path = records / f"orphan-{pid}.json"
    path.write_text(json.dumps({"owner_pid": dead_owner.pid, "owner_start_time": None, "pid": pid,
                                "start_time": launcher._start_time(pid), "user_data_dir": udd,
                                "home_key": "x", "ephemeral": False}))
    return path


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_orphan_reap_refuses_a_pid_whose_cmdline_lacks_the_recorded_user_data_dir(tmp_path, fake_chromium):
    stranger = subprocess.Popen(["sleep", "3001"])
    try:
        record = _write_orphan_record(launcher._records_dir(), stranger.pid, str(tmp_path / "some-profile"))
        assert launcher.reap_orphaned_browsers() == 0
        assert stranger.poll() is None  # never signalled
        assert not record.exists()  # the stale record is dropped
    finally:
        stranger.kill()
        stranger.wait()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_orphan_reap_stops_a_crashed_owners_browser(tmp_path, fake_chromium):
    udd = tmp_path / "orphan-profile"
    proc = subprocess.Popen([str(fake_chromium), f"--user-data-dir={udd}"], start_new_session=True)
    deadline = time.monotonic() + 10
    while not (udd / "DevToolsActivePort").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    _write_orphan_record(launcher._records_dir(), proc.pid, str(udd))
    assert launcher.reap_orphaned_browsers() == 1
    proc.wait(timeout=10)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # the fake browser's reparented helper is ours to stop
def test_records_of_a_live_owner_are_left_alone(tmp_path, fake_chromium):
    launcher.launch_chromium("mine", env=dict(os.environ))
    assert launcher.reap_orphaned_browsers() == 0
    assert launcher.get_browser("mine") is not None
    assert list(launcher._records_dir().glob("*.json"))


@pytest.mark.parametrize("headed,display,expect_headless", [
    (False, ":1", True),
    (True, ":1", False),
    (True, "", True),  # Linux without a display: a headed launch would die
])
@pytest.mark.platforms("linux")
def test_headed_only_when_configured_and_a_display_exists(headed, display, expect_headless):
    env = {"DISPLAY": display} if display else {}
    assert launcher._resolve_headed(headed, env) is (not expect_headless)


@pytest.mark.platforms("posix")
def test_packaged_only_flags_never_reach_an_explicit_binary(monkeypatch):
    monkeypatch.setattr(launcher, "needs_chromium_sandbox_bypass", lambda: True)
    monkeypatch.setattr("tools.browser_session_cloud.chromium_args", lambda: ["--lang=de"])
    packaged = launcher.build_chromium_argv("/c", "/p", packaged=True, headed=False)
    real = launcher.build_chromium_argv("/c", "/p", packaged=False, headed=True)
    assert "--password-store=basic" in packaged and "--password-store=basic" not in real
    assert "--headless=new" in packaged and "--headless=new" not in real
    for argv in (packaged, real):
        assert set(launcher.CHROMIUM_SANDBOX_BYPASS_ARGS) <= set(argv)
        assert "--lang=de" in argv and argv[-1] == "about:blank"
