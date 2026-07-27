"""Helper functions that carry real failure modes.

Mostly pure, so they are cheap to test exhaustively — and each one here has a
concrete way of going wrong that would be hard to spot from higher up.
"""

import asyncio
import os

import pytest

from godotlens_mcp.lsp_client import (
    DEFAULT_TIMEOUT,
    LSPClient,
    canonical_key,
    compact_location,
    compact_symbol,
    find_project_root,
)
from godotlens_mcp.scene import (
    GodotBinaryNotFound,
    _extract_json,
    dump_scenes,
    find_godot_binary,
    normalize_node_path,
    to_res_path,
)

# ---------------------------------------------------------------------------
# Godot's stdout is noisy
# ---------------------------------------------------------------------------


def test_extract_json_ignores_engine_banners():
    """Godot prints its banner and import progress alongside script output."""
    noisy = (
        "Godot Engine v4.7.1.stable.official - https://godotengine.org\n"
        "[  0% ] first_scan_filesystem | Scanning file structure...\n"
        '{"scenes": {"res://a.tscn": {"nodes": []}}, "errors": {}}\n'
    )
    assert _extract_json(noisy)["scenes"]["res://a.tscn"] == {"nodes": []}


def test_extract_json_takes_the_payload_not_an_earlier_json_line():
    """Only the object carrying "scenes" is ours."""
    text = '{"unrelated": 1}\n{"scenes": {}, "errors": {}}\n'
    assert _extract_json(text) == {"scenes": {}, "errors": {}}


def test_extract_json_returns_none_when_absent():
    assert _extract_json("ERROR: could not open project\n") is None


def test_extract_json_survives_malformed_braces():
    assert _extract_json("{not valid json}\n") is None


# ---------------------------------------------------------------------------
# res:// conversion
# ---------------------------------------------------------------------------


def test_to_res_path_leaves_paths_outside_the_project_alone(tmp_path):
    """A path outside the project cannot be expressed as res://; do not fabricate one."""
    outside = tmp_path.parent / "elsewhere" / "a.tscn"
    result = to_res_path(str(outside), str(tmp_path))
    assert not result.startswith("res://")


def test_to_res_path_normalizes_separators(tmp_path):
    nested = tmp_path / "scenes" / "levels" / "one.tscn"
    assert to_res_path(str(nested), str(tmp_path)) == "res://scenes/levels/one.tscn"


@pytest.mark.parametrize("given,expected", [
    ("./Player", "Player"),
    ("Player", "Player"),
    (".", "."),
    ("", "."),
    ("./Player/HitArea", "Player/HitArea"),
    ("/Player", "Player"),
])
def test_normalize_node_path(given, expected):
    """SceneState and connection targets spell the same node differently."""
    assert normalize_node_path(given) == expected


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------


def test_find_godot_binary_honours_the_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "my-godot"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("GODOT_BIN", str(fake))
    assert find_godot_binary() == str(fake)


def test_find_godot_binary_ignores_a_nonexistent_override(tmp_path, monkeypatch):
    """A stale GODOT_BIN must not shadow a binary that is actually present."""
    monkeypatch.setenv("GODOT_BIN", str(tmp_path / "gone"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    # Falls through; result depends on the environment, but must not be the bad path.
    assert find_godot_binary() != str(tmp_path / "gone")


def test_find_godot_binary_prefers_the_console_build(tmp_path, monkeypatch):
    """On Windows the console build keeps stdout usable, which the helper needs."""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    local = tmp_path / "godot"
    local.mkdir()
    (local / "Godot_v4.7.1-stable_win64.exe").write_text("", encoding="utf-8")
    (local / "Godot_v4.7.1-stable_win64_console.exe").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    found = find_godot_binary()
    assert found is not None
    assert "console" in found


async def test_dump_scenes_without_a_binary_raises_actionably(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", str(tmp_path / "missing"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("godotlens_mcp.scene.find_godot_binary", lambda: None)

    with pytest.raises(GodotBinaryNotFound, match="GODOT_BIN"):
        await dump_scenes(["res://a.tscn"], str(tmp_path))


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------


def test_find_project_root_walks_up(tmp_path, monkeypatch):
    root = tmp_path / "game"
    nested = root / "scripts" / "enemies" / "deep"
    nested.mkdir(parents=True)
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    monkeypatch.delenv("GODOT_PROJECT_ROOT", raising=False)

    assert find_project_root() == str(root)


def test_find_project_root_falls_back_to_cwd(tmp_path, monkeypatch):
    """No project.godot anywhere above: return cwd rather than the filesystem root."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GODOT_PROJECT_ROOT", raising=False)
    assert find_project_root() == str(tmp_path)


def test_find_project_root_env_override_wins(tmp_path, monkeypatch):
    root = tmp_path / "game"
    root.mkdir()
    (root / "project.godot").write_text("", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()

    monkeypatch.chdir(root)
    monkeypatch.setenv("GODOT_PROJECT_ROOT", str(other))
    assert find_project_root() == str(other)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def test_compact_symbol_handles_deep_nesting():
    """A class with inner classes must not lose its leaves."""
    symbol = {
        "name": "Outer", "kind": 5, "range": {"start": {"line": 0}},
        "children": [{
            "name": "Inner", "kind": 5, "range": {"start": {"line": 5}},
            "children": [{"name": "method", "kind": 12, "range": {"start": {"line": 7}}}],
        }],
    }
    result = compact_symbol(symbol)
    assert result["children"][0]["children"][0]["name"] == "method"
    assert result["children"][0]["children"][0]["line"] == 7


def test_compact_location_decodes_percent_encoding():
    """Godot 4.5+ publishes encoded URIs; the agent needs a usable path back."""
    location = {
        "uri": "file:///C%3A/My%20Project/player.gd",
        "range": {"start": {"line": 3, "character": 4}},
    }
    assert compact_location(location)["file"] == "C:/My Project/player.gd"


def test_canonical_key_is_stable_across_spellings(tmp_path, monkeypatch):
    """Every spelling of the same file must collapse to one cache key."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sub" / "player.gd"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")

    spellings = [
        str(target),
        str(target).replace("\\", "/"),
        os.path.join("sub", "player.gd"),
        "./sub/player.gd",
        "sub/../sub/player.gd",
    ]
    keys = {canonical_key(s) for s in spellings}
    assert len(keys) == 1, f"same file produced {len(keys)} different keys: {keys}"


# ---------------------------------------------------------------------------
# Client defaults
# ---------------------------------------------------------------------------


def test_client_defaults():
    client = LSPClient()
    assert client.port == 6005
    assert client.timeout == DEFAULT_TIMEOUT
    assert client.initialized is False
    assert client.diagnostics_cache == {}


async def test_notify_without_a_connection_is_a_typed_error():
    from godotlens_mcp.lsp_client import LSPConnectionLost

    client = LSPClient(port=1)
    with pytest.raises(LSPConnectionLost):
        await client.notify("textDocument/didOpen", {})


async def test_request_without_a_connection_is_a_typed_error():
    from godotlens_mcp.lsp_client import LSPConnectionLost

    client = LSPClient(port=1)
    with pytest.raises(LSPConnectionLost):
        await client.request("textDocument/hover", {})


async def test_disconnect_is_idempotent():
    client = LSPClient(port=1)
    await client.disconnect()
    await client.disconnect()
    assert client.initialized is False


async def test_wait_for_diagnostics_returns_missing_keys_on_timeout():
    """The caller must be able to tell 'clean' from 'never heard back'."""
    client = LSPClient(port=1)
    client.drain_notifications = _noop
    missing = await client.wait_for_diagnostics(["a", "b"], timeout=0.2)
    assert missing == {"a", "b"}


async def test_wait_for_diagnostics_returns_empty_when_all_present():
    client = LSPClient(port=1)
    client.drain_notifications = _noop
    client.diagnostics_cache = {"a": [], "b": []}
    assert await client.wait_for_diagnostics(["a", "b"], timeout=0.2) == set()


async def _noop(*args, **kwargs):
    await asyncio.sleep(0)
