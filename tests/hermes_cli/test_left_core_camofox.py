"""Camofox left core for the ``camofox`` catalog plugin: a home that ran core's Camofox backend gets the
plugin once, and its core-era ``browser`` toolset (Camofox's browser_* tools then; browser_exec now) becomes
the plugin's ``camofox`` toolset, a one-time conversion a later run never undoes.

Real config/.env files under temp homes, a fixture catalog dir; the network install is a stand-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.left_core_migration as lcm
import hermes_yaml as yaml

_CATALOG_ENTRY = """\
name: camofox
repo: https://github.com/NousResearch/hermes-official-plugins
sha: 0123456789abcdef0123456789abcdef01234567
subdir: camofox
description: Camofox anti-detection browser backend (fixture).
maintainer: NousResearch
tier: official
category: tools
capabilities:
  provides_tools: [browser_navigate]
  provides_hooks: []
  provides_middleware: []
  requires_env: []
"""


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(lcm, "_attempted", set())
    monkeypatch.setattr(lcm, "_undelivered", {})
    for name in ("CAMOFOX_URL", "BROWSER_CDP_URL"):
        monkeypatch.delenv(name, raising=False)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import plugin_catalog
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "camofox.yaml").write_text(_CATALOG_ENTRY, encoding="utf-8")
    monkeypatch.setattr(plugin_catalog, "get_catalog_dir", lambda: catalog)
    monkeypatch.setattr(plugin_catalog, "fetch_live_catalog", lambda **_: None)
    return catalog


def _home(tmp_path: Path, name: str = "home", *, env: str = "", config: dict | None = None) -> Path:
    home = tmp_path / name
    home.mkdir(parents=True)
    if env:
        (home / ".env").write_text(env, encoding="utf-8")
    if config is not None:
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return home


def _config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


def _installer(home: Path, calls: list):
    def install(name):
        calls.append(name)
        (home / "plugins" / name).mkdir(parents=True)
        return {"ok": True}
    return install


@pytest.mark.parametrize(("env", "config", "in_use"), [
    ("CAMOFOX_URL=http://localhost:9377\n", None, True),                       # A: the address alone selected it
    ("", None, False),                                                          # B
    ("", {"browser": {"camofox": {"managed_persistence": True}}}, True),        # C: Camofox settings of its own
    ("", {"browser": {"cloud_provider": "camofox"}}, True),
    # A config core's save_config materialised: the old schema defaults are not a Camofox setup.
    ("", {"browser": {"camofox": {"managed_persistence": False, "user_id": "",
                                  "loopback_host_alias": "host.docker.internal"}}}, False),
    # A stored selection decides alone; a CDP endpoint won over Camofox.
    ("CAMOFOX_URL=http://localhost:9377\n", {"browser": {"cloud_provider": "browserbase"}}, False),
    ("CAMOFOX_URL=http://localhost:9377\n", {"browser": {"use_gateway": True}}, False),
    ("CAMOFOX_URL=http://localhost:9377\n", {"browser": {"cdp_url": "ws://127.0.0.1:9222"}}, False),
    ("CAMOFOX_URL=http://localhost:9377\nBROWSER_CDP_URL=ws://127.0.0.1:9222\n", None, False),
])
def test_camofox_in_use_reads_core_selection(tmp_path, env, config, in_use):
    assert lcm.camofox_in_use(_home(tmp_path, env=env, config=config)) is in_use


def test_managed_env_url_counts(tmp_path):
    (tmp_path / "managed" / ".env").write_text("CAMOFOX_URL=http://camofox:9377\n", encoding="utf-8")
    assert lcm.camofox_in_use(_home(tmp_path)) is True


def test_process_env_url_counts_only_for_the_launch_home(tmp_path, monkeypatch):
    launch = _home(tmp_path, "launch")
    secondary = _home(tmp_path, "profiles/work")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert lcm.camofox_in_use(launch) is False
    assert lcm.camofox_in_use(launch, process_env=True) is True
    assert lcm.camofox_in_use(secondary, process_env=True) is False


def _resolved(home: Path, platform: str, monkeypatch) -> set[str]:
    from hermes_cli import tools_config
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: {"camofox"})
    return tools_config._get_platform_tools(_config(home), platform, include_default_mcp_servers=False)


def test_browser_becomes_camofox_where_core_carried_it(tmp_path, monkeypatch):
    home = _home(tmp_path, env="CAMOFOX_URL=http://localhost:9377\n", config={
        "platform_toolsets": {
            "cli": ["terminal", "browser", "file"],   # browser named directly
            "telegram": ["hermes-telegram"],          # through the composite
            "discord": ["terminal", "file"],          # core had browser off here
        },
        "agent": {"disabled_toolsets": ["memory"]},
    })
    calls, said = [], []
    install = _installer(home, calls)
    assert lcm.migrate_home(home, install=install, say=said.append) == ["camofox"]
    assert calls == ["camofox"]
    assert "Camofox browser backend moved out of core" in said[0]

    config = _config(home)
    assert config["platform_toolsets"]["cli"] == ["terminal", "file", "camofox"]
    assert config["agent"]["disabled_toolsets"] == ["memory", "browser"]
    known = config["known_plugin_toolsets"]
    assert "camofox" in known["discord"] and "camofox" in known["webhook"]
    assert "cli" not in known and "telegram" not in known
    # The end state per platform: the camofox surface where core had browser on, never browser_exec.
    for platform in ("cli", "telegram", "slack"):  # slack: no saved list, default composite
        enabled = _resolved(home, platform, monkeypatch)
        assert "camofox" in enabled and "browser" not in enabled, platform
    for platform in ("discord", "webhook"):
        enabled = _resolved(home, platform, monkeypatch)
        assert "camofox" not in enabled and "browser" not in enabled, platform


def test_conversion_is_never_undone(tmp_path):
    home = _home(tmp_path, env="CAMOFOX_URL=http://localhost:9377\n",
                 config={"platform_toolsets": {"cli": ["terminal", "browser"]}})
    lcm._record_migration(home, next(f for f in lcm.LEFT_CORE if f.plugin == "camofox"))
    config = _config(home)
    assert config["_left_core_scoped"] == ["camofox"]
    # The user turns core's browser (browser_exec) back on for the CLI afterwards.
    config["platform_toolsets"]["cli"] = ["terminal", "camofox", "browser"]
    config["agent"]["disabled_toolsets"] = []
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    before = (home / "config.yaml").read_bytes()
    calls = []
    install = _installer(home, calls)
    lcm.migrate_home(home, install=install, say=lambda _m: None)
    after = _config(home)
    assert after["platform_toolsets"]["cli"] == ["terminal", "camofox", "browser"]
    assert after["agent"]["disabled_toolsets"] == []
    assert calls == ["camofox"]
    lcm.migrate_home(home, install=install, say=lambda _m: None)
    assert calls == ["camofox"]
    assert {k: v for k, v in _config(home).items() if k != "_left_core_installed"} == yaml.safe_load(before)


def test_homes_are_islands_a_b_c_a(tmp_path):
    """A (CAMOFOX_URL in .env) and C (browser.camofox only) are migrated; B is never touched."""
    a = _home(tmp_path, "a", env="CAMOFOX_URL=http://localhost:9377\n",
              config={"platform_toolsets": {"cli": ["browser"]}})
    b = _home(tmp_path, "b", config={"platform_toolsets": {"cli": ["browser", "terminal"]}})
    c = _home(tmp_path, "c", config={"browser": {"camofox": {"managed_persistence": True}}})
    b_before = (b / "config.yaml").read_bytes()
    calls: list[tuple[str, str]] = []

    def run(home):
        def install(name):
            calls.append((home.name, name))
            (home / "plugins" / name).mkdir(parents=True)
            return {"ok": True}
        return lcm.migrate_home(home, install=install, say=lambda _m: None)

    assert run(a) == ["camofox"]
    assert run(b) == []
    assert run(c) == ["camofox"]
    assert run(a) == []
    assert calls == [("a", "camofox"), ("c", "camofox")]
    assert (b / "config.yaml").read_bytes() == b_before
    assert not (b / "plugins").exists()
    assert _config(a)["platform_toolsets"]["cli"] == ["camofox"]
    assert _config(c)["agent"]["disabled_toolsets"] == ["browser"]


def test_catalog_miss_names_the_install_command(tmp_path, _fresh):
    (_fresh / "camofox.yaml").unlink()
    home = _home(tmp_path, env="CAMOFOX_URL=http://localhost:9377\n")
    said = []
    assert lcm.migrate_home(home, install=lambda name: pytest.fail("no catalog entry"), say=said.append) == []
    assert "cannot find in the plugin catalog yet" in said[0]
    assert "hermes plugins install camofox" in said[0]


def test_camofox_env_stays_out_of_children():
    """With the plugin absent no manifest declares CAMOFOX_*: core keeps stripping them."""
    from tools.environments import local_env_policy as policy
    assert "CAMOFOX_URL" in policy._build_provider_env_blocklist()
    assert "CAMOFOX_API_KEY" in policy._ALWAYS_STRIP_KEYS
