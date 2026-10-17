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


def test_helper_script_is_packaged():
    """scene_dump.gd must ship with the package.

    It is the only non-Python file in the distribution, so it is the one most likely
    to be dropped by a packaging change — and the scene_* tools are dead without it.
    """
    from godotlens_mcp.scene import HELPER_SCRIPT

    assert HELPER_SCRIPT.is_file(), f"helper script missing at {HELPER_SCRIPT}"
    body = HELPER_SCRIPT.read_text(encoding="utf-8")
    assert "get_connection_method" in body, "helper no longer reports connections"
    assert "extends SceneTree" in body


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


def _with_root(*nodes):
    root = {"path": ".", "name": "Main", "type": "Node2D", "script": None, "properties": {}}
    return [root, *nodes]


BUTTON = {"path": "X", "name": "X", "type": "Button", "script": None, "properties": {}}


def _kinds(report):
    return [problem["kind"] for problem in report["problems"]]


def test_connection_to_a_scriptless_node_is_reported():
    bare = {"path": "Bare", "name": "Bare", "type": "Node2D", "script": None, "properties": {}}
    scene = {
        "nodes": _with_root(BUTTON, bare),
        "connections": [{"signal": "pressed", "from": "X", "to": "Bare", "method": "_on_pressed",
                         "facts": {"signal_exists": True, "signal_args": 0, "method_native": False}}],
    }
    assert _kinds(validate_scene(scene, {})) == ["target_has_no_script"]


def test_a_native_method_on_a_scriptless_node_is_a_valid_handler():
    """Regression: connecting to an engine method such as queue_free was flagged."""
    scene = {
        "nodes": _with_root(BUTTON),
        "connections": [{"signal": "pressed", "from": "X", "to": ".", "method": "queue_free",
                         "facts": {"signal_exists": True, "signal_args": 0, "method_native": True}}],
    }
    assert validate_scene(scene, {})["valid"] is True


def test_connection_to_a_missing_node_is_reported():
    scene = {
        "nodes": _with_root(BUTTON),
        "connections": [{"signal": "pressed", "from": "X", "to": "Ghost", "method": "_on_pressed"}],
    }
    assert _kinds(validate_scene(scene, {})) == ["missing_target_node"]


def test_unknown_symbols_do_not_produce_false_positives():
    """If the LSP could not report a script's symbols, say nothing rather than guess."""
    report = validate_scene(SCENE, {})  # no symbol info at all
    assert report["valid"] is True


def test_targets_are_resolved_through_the_merged_view():
    """A node from a base scene is typeless and scriptless in the file itself.

    Regression: validating against the raw per-file nodes reported every connection
    to such a node as target_has_no_script.
    """
    scene = {
        "nodes": [{"path": ".", "name": "Base", "type": "", "script": None, "properties": {}}],
        "resolved_nodes": [{"path": ".", "type": "Node2D", "script": "res://handler.gd",
                            "origin": "instance:res://base.tscn"},
                           {"path": "Button", "type": "Button", "script": None,
                            "origin": "instance:res://base.tscn"}],
        "connections": [{"signal": "pressed", "from": "Button", "to": ".",
                         "method": "_on_button_pressed"}],
    }
    report = validate_scene(scene, {"res://handler.gd": {"_on_button_pressed"}})
    assert report["valid"] is True, report["problems"]
    assert collect_scripts(scene) == ["res://handler.gd"]


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


INHERITED_AND_INSTANCED = [
    "res://inherited.tscn", "res://instancer.tscn", "res://nested.tscn", "res://editable.tscn"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dump_resolves_nodes_from_base_and_instanced_scenes(
        godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(INHERITED_AND_INSTANCED, str(godot_project))
    assert not dump["errors"], dump["errors"]

    def resolved(scene_path):
        return {n["path"]: n for n in dump["scenes"][scene_path]["resolved_nodes"]}

    inherited = resolved("res://inherited.tscn")
    assert inherited["."]["type"] == "Node2D"
    assert inherited["."]["script"] == "res://handler.gd"
    assert inherited["Button"]["type"] == "Button"

    nested = resolved("res://nested.tscn")
    assert nested["Outer/Child"]["script"] == "res://handler.gd"
    assert nested["Outer/Child/Button"]["origin"] == "instance:res://base.tscn"

    editable = resolved("res://editable.tscn")
    assert editable["Child/Panel/Extra"]["type"] == "Label"
    assert editable["Child/Panel/Extra"]["origin"] == "this_file"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validate_accepts_correct_connections_into_other_scenes(
        godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(INHERITED_AND_INSTANCED, str(godot_project))
    symbols = {"res://handler.gd": {"_on_button_pressed"}}

    for path in INHERITED_AND_INSTANCED:
        report = validate_scene(dump["scenes"][path], symbols)
        assert report["valid"] is True, f"{path}: {report['problems']}"
        assert report["connections_checked"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validate_still_flags_a_missing_handler_in_a_base_scene_script(
        godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://inherited.tscn"], str(godot_project))
    report = validate_scene(dump["scenes"]["res://inherited.tscn"],
                            {"res://handler.gd": {"_on_button_renamed"}})
    assert report["valid"] is False
    assert report["problems"][0]["kind"] == "missing_handler"
    assert report["problems"][0]["script"] == "res://handler.gd"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_script_that_uses_an_autoload_compiles_in_the_dump(
        godot_binary, godot_project, monkeypatch):
    """Loaded before autoloads are registered, it failed with 'Identifier not found'."""
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://autoload_user.tscn"], str(godot_project))
    facts = dump["scenes"]["res://autoload_user.tscn"]["scripts"]["res://autoload_user.gd"]
    assert facts["compiled"] is True
    assert facts["base_type"] == "Node"


from tests.scene_mistakes import MISTAKES  # noqa: E402


@pytest.mark.integration
@pytest.mark.asyncio
async def test_each_kind_of_scene_mistake_is_reported_and_nothing_else(
        godot_binary, godot_project, monkeypatch):
    """One scene per mistake Godot accepts silently; each yields exactly its own kind."""
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    paths = [f"res://mistakes/{name}" for name in MISTAKES]
    dump = await dump_scenes(paths, str(godot_project))
    assert not dump["errors"], dump["errors"]

    wrong = {}
    for name, (expected, _) in MISTAKES.items():
        report = validate_scene(dump["scenes"][f"res://mistakes/{name}"], {})
        kinds = [problem["kind"] for problem in report["problems"]]
        if kinds != [expected]:
            wrong[name] = kinds
    assert not wrong, f"expected exactly one problem each; got {wrong}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_correct_scene_has_no_problems_even_where_naive_checks_misfire(
        godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://mistakes/correct.tscn"], str(godot_project))
    report = validate_scene(dump["scenes"]["res://mistakes/correct.tscn"], {})
    assert report["problems"] == [], report["problems"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_runs_leave_the_projects_own_logs_alone(godot_binary, godot_project, monkeypatch):
    """Godot keeps only the newest five logs per project; GodotLens's runs must not
    push the developer's own logs out."""
    from godotlens_mcp.scene import dump_project, validate_project
    from tests.conftest import log_snapshot

    monkeypatch.setenv("GODOT_BIN", godot_binary)
    before = log_snapshot("GodotLensTest")
    await dump_scenes(["res://main.tscn"], str(godot_project))
    await dump_project(str(godot_project))
    await validate_project(str(godot_project))
    assert log_snapshot("GodotLensTest") == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_scene_is_reported_not_raised(godot_binary, godot_project, monkeypatch):
    monkeypatch.setenv("GODOT_BIN", godot_binary)
    dump = await dump_scenes(["res://does_not_exist.tscn"], str(godot_project))
    assert "res://does_not_exist.tscn" in dump["errors"]
