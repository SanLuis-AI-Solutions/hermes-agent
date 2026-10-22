"""The bot's Chromium INSIDE the terminal backend's sandbox (Bot Desktop placed there).

Same browser a human takes over in the pane: the sandbox image's Playwright Chromium on the profile the
dock's Browser icon uses, on the sandbox screen's DISPLAY. Its DevTools port is the sandbox's loopback,
unreachable from this host (Docker bridge / ssh remote), so it is forwarded over the sandbox's exec
stream; Chromium's DevTools accepts any loopback ``Host`` header, port included.
"""

from __future__ import annotations

import logging
import shlex
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LAUNCH_TIMEOUT_S = 60.0
# Sandbox Chromium this process launched, per shared key: ``(sandbox env, pid)``. Every session on the profile
# attaches to it, so it is stopped only when the LAST of them tears down (release_session_backend's in-use
# check on the shared ``launcher_key``); the env is kept so a teardown outside the owner's scope hits the
# right sandbox.
_launched: dict[str, tuple[Any, int]] = {}
_launched_lock = threading.Lock()


def _sandbox():
    from tools.bot_desktop import runtime as _bd_runtime
    env = _bd_runtime._sandbox_env(create=True)
    if env is None:
        raise RuntimeError("the terminal backend's sandbox is not running, so there is nowhere to run the browser")
    published = _bd_runtime.published_env()
    if not published.get("DISPLAY"):
        # The sandbox died under a live screen: fail here rather than run headless in a dead sandbox or,
        # worse, fall back to the host.
        raise RuntimeError("the screen inside the terminal backend's sandbox is gone; start it again")
    return env, published


def _read_devtools(env, user, udd: str) -> Optional[tuple[int, str]]:
    from tools.environments import streams
    proc = streams.run_in(env, ["cat", f"{udd}/DevToolsActivePort"], user=user, timeout=10)
    lines = proc.stdout.decode("utf-8", "replace").splitlines() if proc.returncode == 0 else []
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/browser/"):
        return int(lines[0].strip()), lines[1].strip()
    return None


def _live_devtools(env, user, udd: str) -> Optional[tuple[int, str]]:
    """``(port, path)`` of the Chromium on ``udd`` while its DevTools port still answers, in one exec (a
    crashed Chromium leaves a stale ``DevToolsActivePort`` behind)."""
    from tools.environments import streams
    active = shlex.quote(f"{udd}/DevToolsActivePort")
    script = f'p=$(head -n1 {active}) && exec 3<>"/dev/tcp/127.0.0.1/$p" && cat {active}'
    proc = streams.run_in(env, ["bash", "-c", script], user=user, timeout=10)
    lines = proc.stdout.decode("utf-8", "replace").splitlines() if proc.returncode == 0 else []
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/browser/"):
        return int(lines[0].strip()), lines[1].strip()
    return None


def _shared_key(udd: str) -> str:
    """The launcher key every session on this profile's sandbox Chromium shares (the sandbox is per profile)."""
    from hermes_constants import hermes_home_key
    return f"sandbox:{hermes_home_key()}:{udd}"


def _start_chromium(env, user, exe: str, udd: str, child_env: dict) -> int:
    """Launch detached in the sandbox; returns the sandbox pid."""
    from tools.bot_desktop.browser import SHARED_PROFILE_ARGS
    from tools.browser_launcher import CHROMIUM_SANDBOX_BYPASS_ARGS, build_chromium_argv
    from tools.environments import streams
    # Container: no user namespaces for Chromium's own sandbox, so the bypass flags always apply (this
    # host's facts say nothing about the sandbox); the shared-profile flags match the dock's icon.
    argv = build_chromium_argv(exe, udd, packaged=False, headed=True, sandbox_bypass=False,
                               extra_args=(*SHARED_PROFILE_ARGS, *CHROMIUM_SANDBOX_BYPASS_ARGS))
    argv = list(dict.fromkeys(argv))
    script = (f"mkdir -p {shlex.quote(udd)} && rm -f {shlex.quote(udd)}/DevToolsActivePort && "
              f"(setsid {shlex.join(argv)} >/dev/null 2>&1 & echo $!)")
    proc = streams.run_in(env, ["bash", "-c", script], child_env=child_env, user=user, timeout=30)
    pid_text = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] if proc.returncode == 0 else []
    if not pid_text or not pid_text[0].isdigit():
        raise RuntimeError(f"Chromium could not be started in the sandbox: {proc.stderr.decode('utf-8', 'replace')[-500:]}")
    return int(pid_text[0])


def _wait_for_port(env, user, udd: str) -> tuple[int, str]:
    deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        found = _read_devtools(env, user, udd)
        if found:
            return found
        time.sleep(0.5)
    raise RuntimeError("Chromium in the sandbox did not expose a DevTools endpoint in time")


def create_sandbox_session(cache_key: str) -> dict[str, Any]:
    """Session on the sandbox Chromium (attach to the running one, else launch it)."""
    from tools.bot_desktop import sandbox_host
    from tools.browser_session import _session_record
    from tools.environments import streams
    env, published = _sandbox()
    user = sandbox_host._user_for(env)
    udd = sandbox_host.browser_profile_dir(env)
    key = _shared_key(udd)
    found = _live_devtools(env, user, udd)
    if found is None:
        exe = sandbox_host.chromium_executable(env)
        if not exe:
            raise RuntimeError("the sandbox image has no Chromium to run the browser")
        child_env = {**published, "TMPDIR": streams.sandbox_temp_dir(env)}
        pid = _start_chromium(env, user, exe, udd, child_env)
        found = _wait_for_port(env, user, udd)
        with _launched_lock:
            _launched[key] = (env, pid)
    local_port = streams.forward_port(env, found[0], user=user)
    info = _session_record("sb", f"ws://127.0.0.1:{local_port}{found[1]}", {"local": True, "sandbox": True})
    info.update(shared_profile=True, launcher_key=key)
    logger.info("Created sandbox browser session %s for %s", info["session_name"], cache_key)
    return info


def sandbox_browser_alive(session_info: dict[str, Any]) -> bool:
    """The sandbox Chromium still serves this session's endpoint through the current port forward: False once
    it died, was relaunched (new DevTools path) or the sandbox was replaced (new forward)."""
    from tools.bot_desktop import runtime as _bd_runtime, sandbox_host
    from tools.environments import streams
    env = _bd_runtime._sandbox_env(create=False)
    if env is None:
        return False
    try:
        user = sandbox_host._user_for(env)
        found = _live_devtools(env, user, sandbox_host.browser_profile_dir(env))
    except (OSError, RuntimeError) as exc:
        logger.debug("sandbox browser liveness probe failed: %s", exc)
        return False
    if found is None:
        return False
    return session_info.get("cdp_url") == f"ws://127.0.0.1:{streams.forward_port(env, found[0], user=user)}{found[1]}"


def stop_sandbox_browser(session_info: dict[str, Any]) -> None:
    """SIGTERM the sandbox Chromium this process launched on the session's shared key (best effort; the
    sandbox's lifetime bounds it). The caller guarantees no other live session uses it."""
    with _launched_lock:
        launched = _launched.pop(session_info.get("launcher_key") or "", None)
    if launched is None:
        return
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    env, pid = launched
    try:
        streams.run_in(env, ["kill", "-TERM", str(int(pid))], user=sandbox_host._user_for(env), timeout=15)
    except (OSError, RuntimeError) as exc:
        logger.debug("sandbox browser stop for pid %s failed: %s", pid, exc)
