"""Config v52: agent-browser and the built-in browser tools left Hermes.

browser.backend is dropped (``off`` gets a one-time notice); AGENT_BROWSER_* from a profile's own
.env (plus the process env for the launch home only) moves to browser.* keys that are still unset.
"""

import hermes_yaml as yaml
import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_AB_ENV = ("AGENT_BROWSER_EXECUTABLE_PATH", "AGENT_BROWSER_PROFILE", "AGENT_BROWSER_ARGS",
           "AGENT_BROWSER_HEADED", "AGENT_BROWSER_ENGINE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _AB_ENV:
        monkeypatch.delenv(name, raising=False)


def _home(path, config, env=""):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (path / ".env").write_text(env, encoding="utf-8")
    return path


def _raw(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def _migrate(home, *, quiet=False):
    from hermes_cli.config import migrate_config

    token = set_hermes_home_override(home)
    try:
        return migrate_config(interactive=False, quiet=quiet)
    finally:
        reset_hermes_home_override(token)


def test_backend_off_is_dropped_with_a_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    home = _home(tmp_path / "a", {"_config_version": 51, "browser": {"backend": "off", "inactivity_timeout": 60}})
    _migrate(home)
    out = capsys.readouterr().out
    assert _raw(home)["browser"] == {"inactivity_timeout": 60}
    assert _raw(home)["_config_version"] == 52
    assert "browser_exec is the browser now" in out and "`browser` toolset in `hermes tools`" in out
    # Once: a second pass at v52 has nothing left to say.
    _migrate(home)
    assert "browser_exec" not in capsys.readouterr().out


def test_quiet_migration_hands_the_notice_to_the_caller(tmp_path, monkeypatch, capsys):
    """`hermes update` migrates quietly and re-prints only ``config_added``; the notice must be in it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    home = _home(tmp_path / "a", {"_config_version": 51, "browser": {"backend": "off"}})
    results = _migrate(home, quiet=True)
    assert capsys.readouterr().out == ""
    assert any("browser_exec is the browser now" in note and "`browser` toolset" in note
               for note in results["config_added"])


@pytest.mark.parametrize("value", ["", "browser-use", "Browser-Use", None])
def test_other_backend_values_are_dropped_silently(tmp_path, monkeypatch, capsys, value):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    home = _home(tmp_path / "a", {"_config_version": 51, "browser": {"backend": value, "headed": True}})
    _migrate(home)
    assert _raw(home)["browser"] == {"headed": True}
    assert "browser_exec" not in capsys.readouterr().out


def test_unversioned_config_still_loses_backend(tmp_path, monkeypatch):
    """The installer seeds config.yaml without a stamp; a legacy key is legacy however old the file."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    home = _home(tmp_path / "a", {"browser": {"backend": "off"}})
    _migrate(home, quiet=True)
    assert "backend" not in (_raw(home).get("browser") or {})


def test_dotenv_settings_move_to_unset_keys_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    env = ("AGENT_BROWSER_EXECUTABLE_PATH=/opt/chrome/chrome\n"
           "AGENT_BROWSER_PROFILE=/data/profile\n"
           "AGENT_BROWSER_ARGS=--no-sandbox, --lang=de\n"
           "AGENT_BROWSER_HEADED=true\n"
           "AGENT_BROWSER_ENGINE=lightpanda\n")
    home = _home(tmp_path / "a", {"_config_version": 51, "browser": {"user_data_dir": "/mine"}}, env)
    results = _migrate(home, quiet=True)
    browser = _raw(home)["browser"]
    assert browser == {
        "executable_path": "/opt/chrome/chrome",
        "user_data_dir": "/mine",  # already set: the user's value wins
        "chromium_args": ["--no-sandbox", "--lang=de"],
        "headed": True,
        "engine": "lightpanda",
    }
    assert any("AGENT_BROWSER_ARGS → browser.chromium_args" in item for item in results["config_added"])
    # .env is left as it was.
    assert (home / ".env").read_text(encoding="utf-8") == env


def test_falsy_headed_and_unknown_engine_carry_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    home = _home(tmp_path / "a", {"_config_version": 51},
                 "AGENT_BROWSER_HEADED=0\nAGENT_BROWSER_ENGINE=webkit\n")
    _migrate(home, quiet=True)
    assert not _raw(home).get("browser")


def test_pm_store_chromium_path_is_not_pinned(tmp_path, monkeypatch):
    """pm (and the Docker image) exported its own Chromium under AGENT_BROWSER_EXECUTABLE_PATH; that is the
    default now, and a copied versioned store path would break at the next Chromium update."""
    from pm import paths

    store = tmp_path / "tools"
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
    pm_binary = paths.store_root() / "chromium-1208" / "chrome-linux64" / "chrome"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    home = _home(tmp_path / "a", {"_config_version": 51})
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", str(pm_binary))
    _migrate(home, quiet=True)
    assert "executable_path" not in (_raw(home).get("browser") or {})


def test_profiles_are_islands_a_b_a(tmp_path, monkeypatch, capsys):
    """Each home is decided by its own files; the process env counts for the launch home only."""
    a = _home(tmp_path / "a", {"_config_version": 51, "browser": {"backend": "off"}},
              "AGENT_BROWSER_EXECUTABLE_PATH=/opt/a/chrome\n")
    b = _home(tmp_path / "b", {"_config_version": 51, "browser": {"inactivity_timeout": 300}})
    monkeypatch.setenv("HERMES_HOME", str(a))  # A is the launch home
    monkeypatch.setenv("AGENT_BROWSER_HEADED", "1")  # e.g. a systemd unit for A

    _migrate(a)
    out_a = capsys.readouterr().out
    _migrate(b)
    out_b = capsys.readouterr().out
    _migrate(a)
    out_a2 = capsys.readouterr().out

    assert _raw(a)["browser"] == {"executable_path": "/opt/a/chrome", "headed": True}
    assert "browser_exec is the browser now" in out_a
    assert _raw(b)["browser"] == {"inactivity_timeout": 300}
    assert "browser_exec" not in out_b and "agent-browser" not in out_b
    assert "browser_exec" not in out_a2 and "agent-browser" not in out_a2
