"""Name-based symbol lookup and project configuration.

Both exist for the same reason: the agent knows a NAME, and everything else in the
toolchain wants either a coordinate or nothing at all.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def proc(mcp_process, live_lsp, godot_binary):
    process = mcp_process(cwd=live_lsp.project, env={
        "GODOT_LSP_PORT": str(live_lsp.lsp_port),
        "GODOT_DAP_PORT": str(live_lsp.dap_port),
        "GODOT_BIN": godot_binary,
    })
    process.initialize()
    return process, live_lsp.project


# ---------------------------------------------------------------------------
# gdscript_find
# ---------------------------------------------------------------------------


def test_find_locates_a_function_by_name(proc):
    process, _ = proc
    payload, is_error = process.call_tool("gdscript_find", {"name": "take_damage"})

    assert not is_error, payload
    assert payload["count"] >= 1
    declaration = payload["declarations"][0]
    assert declaration["file"].endswith("player.gd")
    assert isinstance(declaration["line"], int)


def test_found_position_actually_works_in_other_tools(proc):
    """The whole point: the returned coordinate must be usable, not approximate.

    A position one column off returns an empty result indistinguishable from
    'no such symbol', which is what drives an agent back to grep.
    """
    process, _ = proc
    found, _ = process.call_tool("gdscript_find", {"name": "take_damage"})
    declaration = found["declarations"][0]

    references, is_error = process.call_tool("gdscript_references", {
        "file": declaration["file"],
        "line": declaration["line"],
        "character": declaration["char"],
    })
    assert not is_error, references
    assert references, "the position from gdscript_find resolved to nothing"
    assert any("enemy.gd" in r["file"] for r in references), \
        "expected the known cross-file usage"


def test_find_can_return_references_directly(proc):
    process, _ = proc
    payload, is_error = process.call_tool(
        "gdscript_find", {"name": "take_damage", "include_references": True})

    assert not is_error, payload
    assert payload["references"], "no references returned"
    assert any("enemy.gd" in r["file"] for r in payload["references"])


def test_find_restricted_to_one_file(proc):
    process, _ = proc
    payload, is_error = process.call_tool(
        "gdscript_find", {"name": "take_damage", "file": "player.gd"})

    assert not is_error, payload
    assert payload["searched_files"] == 1
    assert all(d["file"].endswith("player.gd") for d in payload["declarations"])


def test_find_reports_a_signal(proc):
    process, _ = proc
    payload, is_error = process.call_tool("gdscript_find", {"name": "health_changed"})
    assert not is_error, payload
    assert payload["count"] >= 1


def test_find_reports_a_class_name(proc):
    process, _ = proc
    payload, is_error = process.call_tool("gdscript_find", {"name": "Player"})
    assert not is_error, payload
    assert payload["count"] >= 1


def test_find_gives_an_actionable_miss(proc):
    """A miss must explain itself rather than returning a bare empty list."""
    process, _ = proc
    payload, is_error = process.call_tool(
        "gdscript_find", {"name": "definitely_not_a_symbol_xyz"})

    assert not is_error, payload
    assert payload["count"] == 0
    assert payload["declarations"] == []
    assert "hint" in payload


def test_find_does_not_match_a_comment(proc):
    """enemy.gd mentions take_damage in a comment; that is not a declaration."""
    process, _ = proc
    payload, _ = process.call_tool("gdscript_find", {"name": "take_damage", "file": "enemy.gd"})
    assert payload["count"] == 0, f"comment matched as a declaration: {payload}"


# ---------------------------------------------------------------------------
# project_config
# ---------------------------------------------------------------------------


def test_project_config_reports_autoloads(proc):
    """Autoloads are bare globals with no compile-time validation anywhere."""
    process, _ = proc
    payload, is_error = process.call_tool("project_config", {})

    assert not is_error, payload
    names = {a["name"] for a in payload["autoloads"]}
    assert "GameState" in names

    game_state = next(a for a in payload["autoloads"] if a["name"] == "GameState")
    assert game_state["path"] == "res://game_state.gd"
    assert game_state["is_node"] is True, "the leading * marks it as a Node autoload"


def test_project_config_reports_input_actions(proc):
    """Input.is_action_pressed takes a string nothing checks."""
    process, _ = proc
    payload, is_error = process.call_tool("project_config", {})

    assert not is_error, payload
    actions = {a["name"] for a in payload["input_actions"]}
    assert "jump" in actions, "the project's declared action is missing"
    assert "ui_accept" in actions, "engine defaults should be usable too"


def test_project_config_reports_global_classes(proc):
    process, _ = proc
    payload, is_error = process.call_tool("project_config", {})

    assert not is_error, payload
    classes = {c["class"]: c for c in payload["global_classes"]}
    assert "Player" in classes
    assert classes["Player"]["base"] == "CharacterBody2D"
    assert classes["Player"]["path"] == "res://player.gd"


def test_project_config_reports_the_main_scene(proc):
    process, _ = proc
    payload, is_error = process.call_tool("project_config", {})
    assert not is_error, payload
    assert payload["application"]["main_scene"] == "res://main.tscn"


def test_project_config_without_a_binary_is_actionable(mcp_process, live_lsp, tmp_path):
    process = mcp_process(cwd=live_lsp.project, env={
        "GODOT_LSP_PORT": str(live_lsp.lsp_port),
        "GODOT_BIN": str(tmp_path / "absent"),
        "PATH": str(tmp_path),
    })
    process.initialize()

    payload, is_error = process.call_tool("project_config", {})
    if is_error:
        assert payload["kind"] == "godot_binary_missing"
    else:
        assert "autoloads" in payload, payload


def test_a_typo_is_visibly_absent(proc):
    """The workflow this enables: check the name before writing it.

    Nothing else in the toolchain catches Input.is_action_pressed("jmup") - not the
    compiler, not the language server, not scene validation.
    """
    process, _ = proc
    payload, _ = process.call_tool("project_config", {})
    actions = {a["name"] for a in payload["input_actions"]}

    assert "jump" in actions
    assert "jmup" not in actions


def test_find_reports_when_it_hits_the_file_cap(proc):
    """A common name can match hundreds of files, each costing an LSP round trip.

    The cap must be visible in the response — a silently truncated search reads as a
    complete one, which is worse than being slow.
    """
    process, _ = proc
    payload, is_error = process.call_tool(
        "gdscript_find", {"name": "extends", "max_files": 1})

    assert not is_error, payload
    assert payload["searched_files"] <= 1
    if payload["candidate_files"] > 1:
        assert payload["truncated"] is True
        assert "max_files" in payload["warning"]


def test_find_does_not_flag_truncation_when_it_searched_everything(proc):
    process, _ = proc
    payload, is_error = process.call_tool(
        "gdscript_find", {"name": "take_damage", "file": "player.gd"})

    assert not is_error, payload
    assert "truncated" not in payload
    assert payload["searched_files"] == payload["candidate_files"]
