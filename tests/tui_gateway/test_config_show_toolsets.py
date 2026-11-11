"""``config.show``'s "Toolsets" row is the session's RESOLVED toolsets, never a config key.

The top-level ``enabled_toolsets:`` key is inert — ``_get_platform_tools`` reads
``platform_toolsets.<platform>``. The row used to read that dead key and fall back to the string
"all", so a lane pinned to a handful of toolsets displayed as unrestricted.
"""

from __future__ import annotations

from pathlib import Path

from tui_gateway import server


def _home(tmp_path, monkeypatch, worker_config: str) -> Path:
    """Launch home + a ``worker`` profile carrying ``worker_config``."""
    root = tmp_path / "hermes_home"
    worker = root / "profiles" / "worker"
    worker.mkdir(parents=True)
    (root / "config.yaml").write_text("model:\n  default: probe-model\n", encoding="utf-8")
    (worker / "config.yaml").write_text(worker_config, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(server, "_hermes_home", root)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    server._cfg_cache = server._cfg_mtime = server._cfg_path = None
    return root


def _toolsets_row(params: dict) -> str:
    server._cfg_cache = server._cfg_mtime = server._cfg_path = None
    resp = server._methods["config.show"]("rid", params)
    assert "error" not in resp, resp
    sections = {section["title"]: dict(section["rows"]) for section in resp["result"]["sections"]}
    return sections["Agent"]["Toolsets"]


PINNED = (
    # The inert key the row used to read: must not reach the display.
    "enabled_toolsets:\n"
    "- memory\n"
    "platform_toolsets:\n"
    "  cli:\n"
    "  - file\n"
    "  - kanban\n"
    "  - terminal\n"
)


def test_toolsets_row_is_resolved_not_the_inert_config_key(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, PINNED)
    row = _toolsets_row({"profile": "worker"})
    # The profile's pinned CLI toolsets plus the client surface's own `project` toolset, which
    # _load_enabled_toolsets folds in for this surface (the same list the agent build receives).
    assert row == "file, kanban, project, terminal"
    assert "memory" not in row.split(", ")
    assert row != "all"


def test_toolsets_row_reads_the_requested_profile_not_the_launch_home(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, PINNED)
    assert _toolsets_row({}) != _toolsets_row({"profile": "worker"})


def test_explicitly_empty_selection_is_none_not_all(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "enabled_toolsets:\n- memory\nplatform_toolsets:\n  cli: []\n")
    # An explicitly saved empty list is a zero-tool state (#82010) — never "all".
    assert _toolsets_row({"profile": "worker"}) == "(none)"
