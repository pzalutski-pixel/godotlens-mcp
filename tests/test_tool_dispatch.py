"""Dispatch coverage for every tool.

A tool name appearing in the tools/list assertion is not coverage. These tests
actually invoke each remaining tool through the MCP surface, against real Godot
where the answer depends on the engine.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def live_proc(mcp_process, live_lsp):
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port), "GODOT_BIN": ""})
    proc.initialize()
    return proc, project


def _find_line(project, filename, needle):
    lines = (project / filename).read_text(encoding="utf-8").split("\n")
    for i, line in enumerate(lines):
        if needle in line:
            return i, lines[i]
    raise AssertionError(f"{needle!r} not found in {filename}")


# ---------------------------------------------------------------------------
# Batch tools
# ---------------------------------------------------------------------------


def test_sync_files_reports_per_file_diagnostics(live_proc):
    """One broken file among good ones must be pinpointed, not averaged away."""
    proc, _project = live_proc
    payload, is_error = proc.call_tool(
        "gdscript_sync_files", {"files": ["player.gd", "enemy.gd", "broken.gd"]})

    assert not is_error, payload
    assert payload["synced"] == 3
    diagnostics = payload["diagnostics"]

    # severity 1 == Error. player.gd carries a legitimate UNUSED_PARAMETER warning,
    # so assert on errors rather than on total silence.
    def errors(entries):
        return [d for d in entries if d["severity"] == 1]

    assert errors(diagnostics["broken.gd"]), "the broken file reported no errors"
    assert errors(diagnostics["player.gd"]) == []
    assert errors(diagnostics["enemy.gd"]) == []
    assert payload["verified"] is True


def test_sync_files_isolates_a_missing_file(live_proc):
    proc, _project = live_proc
    payload, is_error = proc.call_tool(
        "gdscript_sync_files", {"files": ["player.gd", "does_not_exist.gd"]})

    assert not is_error, payload
    assert payload["synced"] == 1
    assert payload["errors"][0]["file"] == "does_not_exist.gd"
    assert "player.gd" in payload["diagnostics"]


def test_diagnostics_tool_reports_errors(live_proc):
    proc, _project = live_proc
    payload, is_error = proc.call_tool("gdscript_diagnostics", {"files": ["broken.gd", "game_state.gd"]})

    assert not is_error, payload
    broken = [d for d in payload["diagnostics"]["broken.gd"] if d["severity"] == 1]
    assert broken, "no errors for a file with a syntax error"
    assert [d for d in payload["diagnostics"]["game_state.gd"] if d["severity"] == 1] == []
    assert payload["verified"] is True


def test_definitions_batch_resolves_several_positions(live_proc):
    proc, project = live_proc
    proc.call_tool("gdscript_sync_files", {"files": ["player.gd", "enemy.gd"]})

    line_no, text = _find_line(project, "enemy.gd", "target.take_damage(DAMAGE)")
    col = text.index("take_damage") + 3
    const_line, const_text = _find_line(project, "enemy.gd", "target.take_damage(DAMAGE)")
    const_col = const_text.index("DAMAGE)") + 2

    payload, is_error = proc.call_tool("gdscript_definitions_batch", {"positions": [
        {"file": "enemy.gd", "line": line_no, "character": col},
        {"file": "enemy.gd", "line": const_line, "character": const_col},
    ]})

    assert not is_error, payload
    assert len(payload) == 2
    assert all("position" in entry for entry in payload)
    # At least one position must resolve; both are real symbols.
    assert any(entry.get("definitions") for entry in payload), payload


def test_references_batch_returns_a_result_per_position(live_proc):
    proc, project = live_proc
    proc.call_tool("gdscript_sync_files", {"files": ["player.gd", "enemy.gd"]})

    take_line, take_text = _find_line(project, "player.gd", "func take_damage")
    die_line, die_text = _find_line(project, "player.gd", "func die")

    payload, is_error = proc.call_tool("gdscript_references_batch", {"positions": [
        {"file": "player.gd", "line": take_line, "character": take_text.index("take_damage") + 3},
        {"file": "player.gd", "line": die_line, "character": die_text.index("die") + 1},
    ]})

    assert not is_error, payload
    assert len(payload) == 2
    take_refs = payload[0]["references"]
    assert any("enemy.gd" in r["file"] for r in take_refs), "take_damage refs were not cross-file"


def test_batch_tools_isolate_per_item_failures(live_proc):
    """A bad position must not sink the whole batch."""
    proc, _project = live_proc
    payload, is_error = proc.call_tool("gdscript_definitions_batch", {"positions": [
        {"file": "player.gd", "line": 0, "character": 0},
        {"file": "does_not_exist.gd", "line": 0, "character": 0},
    ]})
    assert not is_error, payload
    assert len(payload) == 2


# ---------------------------------------------------------------------------
# signature_help
# ---------------------------------------------------------------------------


def test_signature_help_at_a_call_site(live_proc):
    proc, project = live_proc
    proc.call_tool("gdscript_sync_files", {"files": ["player.gd", "enemy.gd"]})

    line_no, text = _find_line(project, "enemy.gd", "target.take_damage(DAMAGE)")
    col = text.index("(") + 1

    payload, is_error = proc.call_tool(
        "gdscript_signature_help", {"file": "enemy.gd", "line": line_no, "character": col})

    # Godot returns null when it cannot resolve a signature; both outcomes are
    # acceptable, but it must not error or return the old sentinel string.
    assert not is_error, payload
    assert payload is None or isinstance(payload, dict)
    assert payload != "No results"


# ---------------------------------------------------------------------------
# Scene tools, through the MCP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def scene_proc(mcp_process, live_lsp, godot_binary):
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={
        "GODOT_LSP_PORT": str(port), "GODOT_BIN": godot_binary})
    proc.initialize()
    return proc, project


def test_scene_state_through_the_mcp_surface(scene_proc):
    proc, _project = scene_proc
    payload, is_error = proc.call_tool("scene_state", {"files": ["main.tscn"]})

    assert not is_error, payload
    scene = payload["scenes"]["res://main.tscn"]
    assert scene["node_count"] > 0
    assert any(n.get("script") == "res://player.gd" for n in scene["nodes"])
    assert scene["connections"][0]["method"] == "_on_hit_area_entered"


def test_scene_validate_passes_on_a_healthy_scene(scene_proc):
    proc, _project = scene_proc
    proc.call_tool("gdscript_sync_files", {"files": ["player.gd"]})

    payload, is_error = proc.call_tool("scene_validate", {"files": ["main.tscn"]})
    assert not is_error, payload
    report = payload["scenes"]["res://main.tscn"]
    assert report["valid"] is True, report["problems"]
    assert report["connections_checked"] >= 1


def test_scene_validate_catches_a_handler_removed_from_the_script(scene_proc):
    """The end-to-end version of the bug that motivated the scene layer.

    Delete the handler from GDScript and the scene connection is left dangling —
    something no LSP query can detect, because find_all_usages reads .gd only.
    """
    proc, project = scene_proc
    player = project / "player.gd"
    original = player.read_text(encoding="utf-8")
    try:
        player.write_text(original.replace("_on_hit_area_entered", "_on_hit_area_renamed"),
                          encoding="utf-8")
        proc.call_tool("gdscript_sync_files", {"files": ["player.gd"]})

        payload, is_error = proc.call_tool("scene_validate", {"files": ["main.tscn"]})
        assert not is_error, payload
        report = payload["scenes"]["res://main.tscn"]
        assert report["valid"] is False, "dangling scene connection was not detected"
        problem = report["problems"][0]
        assert problem["kind"] == "missing_handler"
        assert problem["method"] == "_on_hit_area_entered"
    finally:
        player.write_text(original, encoding="utf-8")
        proc.call_tool("gdscript_sync_files", {"files": ["player.gd"]})


def test_scene_tools_report_a_missing_binary_actionably(mcp_process, live_lsp, tmp_path):
    """Without a Godot binary the scene tools must say so, not fail obscurely."""
    port, project = live_lsp
    fake = tmp_path / "not-godot"
    proc = mcp_process(cwd=project, env={
        "GODOT_LSP_PORT": str(port), "GODOT_BIN": str(fake), "PATH": str(tmp_path)})
    proc.initialize()

    payload, is_error = proc.call_tool("scene_state", {"files": ["main.tscn"]})
    if is_error:
        assert payload["kind"] == "godot_binary_missing"
        assert "GODOT_BIN" in payload["error"]
    else:
        # A binary was still discoverable (e.g. ./godot/); then it must have worked.
        assert "scenes" in payload, payload


def test_scene_state_reports_an_unknown_scene_without_failing(scene_proc):
    proc, _project = scene_proc
    payload, is_error = proc.call_tool("scene_state", {"files": ["no_such_scene.tscn"]})
    assert not is_error, payload
    assert payload["errors"], "a missing scene should be reported in errors"


def test_every_tool_is_invoked_somewhere_in_the_suite():
    """Guard against adding a tool with no dispatch test.

    A tool name appearing in the tools/list assertion is not coverage; this checks
    for an actual invocation.
    """
    import pathlib
    import re

    from godotlens_mcp.server import TOOLS

    body = ""
    for path in pathlib.Path(__file__).parent.glob("test_*.py"):
        body += path.read_text(encoding="utf-8")

    invoked = set(re.findall(r"(?:call_tool|handle_tool_call)\(\s*[\"']([a-z_]+)[\"']", body))
    invoked |= set(re.findall(r"[\"']([a-z_]+)[\"'],\s*\{", body))

    never = sorted(t["name"] for t in TOOLS if t["name"] not in invoked)
    assert not never, f"tools with no dispatch test: {never}"
