"""Scene inspection, delegated to Godot.

The unit tests cover the pure logic; the integration tests run the real engine and
are the ones that prove the helper script matches what Godot actually instantiates.
"""

import pytest

from godotlens_mcp.scene import (
    collect_scripts,
    dump_scenes,
    to_res_path,
    validate_scene,
)

# A dump shaped exactly like the helper's output.
SCENE = {
    "nodes": [
        {"path": ".", "name": "Main", "type": "Node2D", "script": None, "properties": {}},
        {"path": "Player", "name": "Player", "type": "CharacterBody2D",
         "script": "res://player.gd", "properties": {"speed": 250.0}},
        {"path": "Player/HitArea", "name": "HitArea", "type": "Area2D",
         "script": None, "properties": {}},
        {"path": "HealthBar", "name": "HealthBar", "type": "ProgressBar",
         "script": None, "properties": {}, "unique_name_in_owner": True},
    ],
    "connections": [
        {"signal": "area_entered", "from": "Player/HitArea", "to": "Player",
         "method": "_on_hit_area_entered"},
    ],
}


def test_collect_scripts_dedupes():
    assert collect_scripts(SCENE) == ["res://player.gd"]


def test_valid_when_the_handler_exists():
    report = validate_scene(SCENE, {"res://player.gd": {"_on_hit_area_entered", "take_damage"}})
    assert report["valid"] is True
    assert report["connections_checked"] == 1


def test_missing_handler_is_reported():
    """The failure mode renaming a signal handler creates.

    Connections name the method as a plain string, so nothing errors at compile time
    and the game breaks only when the signal fires.
    """
    report = validate_scene(SCENE, {"res://player.gd": {"take_damage"}})
    assert report["valid"] is False
    problem = report["problems"][0]
    assert problem["kind"] == "missing_handler"
    assert problem["method"] == "_on_hit_area_entered"
    assert problem["script"] == "res://player.gd"


def test_connection_to_a_scriptless_node_is_reported():
    scene = {
        "nodes": [{"path": "Bare", "name": "Bare", "type": "Node2D", "script": None,
                   "properties": {}}],
        "connections": [{"signal": "pressed", "from": "X", "to": "Bare", "method": "_on_pressed"}],
    }
    report = validate_scene(scene, {})
    assert report["problems"][0]["kind"] == "target_has_no_script"


def test_connection_to_a_missing_node_is_reported():
    scene = {
        "nodes": [{"path": ".", "name": "Main", "type": "Node2D", "script": None, "properties": {}}],
        "connections": [{"signal": "pressed", "from": "X", "to": "Ghost", "method": "_on_pressed"}],
    }
    report = validate_scene(scene, {})
    assert report["problems"][0]["kind"] == "missing_target_node"


def test_unknown_symbols_do_not_produce_false_positives():
    """If the LSP could not report a script's symbols, say nothing rather than guess."""
    report = validate_scene(SCENE, {})  # no symbol info at all
    assert report["valid"] is True


def test_to_res_path_conversions(tmp_path):
    root = str(tmp_path)
    assert to_res_path("res://a/b.tscn", root) == "res://a/b.tscn"
    assert to_res_path(str(tmp_path / "a" / "b.tscn"), root) == "res://a/b.tscn"


# ---------------------------------------------------------------------------
# Against the real engine
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dump_reports_the_scene_godot_actually_builds(godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://main.tscn"], str(godot_project))

    assert not dump.get("errors"), dump.get("errors")
    scene = dump["scenes"]["res://main.tscn"]

    from godotlens_mcp.scene import normalize_node_path
    paths = {normalize_node_path(node["path"]) for node in scene["nodes"]}
    assert "Player" in paths
    assert "Player/HitArea" in paths

    player = next(n for n in scene["nodes"] if normalize_node_path(n["path"]) == "Player")
    assert player["type"] == "CharacterBody2D"
    assert player["script"] == "res://player.gd"

    # unique_name_in_owner lives in the scene, not the script: %HealthBar only
    # resolves because of this flag, and nothing in any .gd file reveals it.
    health_bar = next(n for n in scene["nodes"]
                      if normalize_node_path(n["path"]) == "HealthBar")
    assert health_bar.get("unique_name_in_owner") is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dump_reports_connections_the_lsp_cannot_see(godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://main.tscn"], str(godot_project))
    scene = dump["scenes"]["res://main.tscn"]

    connections = scene["connections"]
    assert connections, "no connections reported"
    wired = connections[0]
    assert wired["signal"] == "area_entered"
    assert wired["method"] == "_on_hit_area_entered"
    from godotlens_mcp.scene import normalize_node_path
    assert normalize_node_path(wired["to"]) == "Player"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validate_catches_a_handler_renamed_out_from_under_a_scene(
        godot_binary, godot_project, monkeypatch):
    """End to end: rename the handler in GDScript, and the scene should be flagged.

    This is precisely the case gdscript_rename cannot detect, because Godot's
    find_all_usages walks .gd files only.
    """
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://main.tscn"], str(godot_project))
    scene = dump["scenes"]["res://main.tscn"]

    # Symbols as they are now: the handler exists.
    healthy = validate_scene(scene, {"res://player.gd": {"_on_hit_area_entered", "take_damage"}})
    assert healthy["valid"] is True

    # Symbols after someone renames it: the scene connection is now dangling.
    broken = validate_scene(scene, {"res://player.gd": {"_on_hit_area_entered_v2", "take_damage"}})
    assert broken["valid"] is False
    assert broken["problems"][0]["method"] == "_on_hit_area_entered"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_scene_is_reported_not_raised(godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://does_not_exist.tscn"], str(godot_project))
    assert "res://does_not_exist.tscn" in dump["errors"]
