"""The runtime_* tools, against a fake game that speaks Godot's debugger protocol.

tests/fake_game.py connects back to GodotLens the way a game started with
--remote-debug does, so every tool path runs over a real socket and the real codec.
The integration tests in test_runtime_integration.py repeat the important ones
against Godot itself.
"""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from godotlens_mcp import server
from godotlens_mcp.runtime import tools as runtime_tools
from godotlens_mcp.runtime.session import OFFSCREEN_POSITION, RuntimeSession

pytestmark = pytest.mark.asyncio

FAKE_GAME = Path(__file__).with_name("fake_game.py")
NAMES = {i: name for i, name in enumerate([
    "TIME_FPS", "TIME_PROCESS", "TIME_PHYSICS_PROCESS", "TIME_NAVIGATION_PROCESS",
    "MEMORY_STATIC", "MEMORY_STATIC_MAX", "MEMORY_MESSAGE_BUFFER_MAX", "OBJECT_COUNT",
    "OBJECT_RESOURCE_COUNT", "OBJECT_NODE_COUNT", "OBJECT_ORPHAN_NODE_COUNT"])}


class FakeGameSession(RuntimeSession):
    """Launches tests/fake_game.py instead of Godot; everything else is the real thing."""

    def command(self, port: int) -> list[str]:
        self.real_command = super().command(port)
        return [sys.executable, str(FAKE_GAME), str(port)]


@pytest.fixture
async def game(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".godot").mkdir(parents=True)
    (project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    log = tmp_path / "sent.jsonl"
    monkeypatch.setenv("FAKE_GAME_LOG", str(log))
    monkeypatch.setenv("GODOT_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runtime_tools, "RuntimeSession", FakeGameSession)
    monkeypatch.setattr(runtime_tools, "find_godot_binary", lambda: "godot")
    monkeypatch.setattr(runtime_tools, "_monitor_name_table", AsyncMock(return_value=NAMES))
    monkeypatch.setattr(runtime_tools, "_main_loop_type", AsyncMock(return_value="SceneTree"))

    class Game:
        root = project

        @staticmethod
        def sent() -> list:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

        @staticmethod
        async def received(expected: list, timeout: float = 5.0) -> bool:
            """Messages are fire-and-forget; wait for the game to have logged one."""
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                if expected in Game.sent():
                    return True
                await asyncio.sleep(0.05)
            return False

    yield Game
    await runtime_tools.stop_all()


async def tool(name, arguments=None):
    result = await server.handle_tool_call(name, arguments or {})
    return json.loads(result["content"][0]["text"]), result


async def run(**arguments):
    payload, result = await tool("runtime_run", {"collect": 0.5, **arguments})
    assert not result["isError"], payload
    return payload


# ---------------------------------------------------------------------------
# Starting a game
# ---------------------------------------------------------------------------


async def test_a_game_reports_its_output_and_structured_errors(game):
    payload = await run()
    texts = [line["text"] for line in payload["lines"]]
    assert "fake game started" in texts and "second line" in texts
    error = payload["errors"][0]
    assert error["message"] == "spawn failed"
    assert (error["file"], error["line"]) == ("res://main.gd", 11)
    assert payload["headless"] is True


async def test_nothing_is_left_able_to_block_the_game(game):
    """Error breaks are ignored and breakpoints skipped: no debugger will resume it."""
    await run()
    session = next(iter(runtime_tools.SESSIONS.values()))
    assert "--ignore-error-breaks" in session.real_command
    assert game.sent()[0] == ["set_skip_breakpoints", [True]]


async def test_headless_and_windowed_launch_flags():
    headless = RuntimeSession("g", "godot", "/p", scene="res://level.tscn").command(5000)
    assert "--headless" in headless and "--remote-debug" in headless
    # Through the helper, loaded for this run only: it opens the scene itself.
    assert headless[headless.index("--script") + 1].endswith("helper.gd")
    assert headless[headless.index("--") + 1:] == ["godotlens_scene=res://level.tscn"]
    plain = RuntimeSession("g", "godot", "/p", scene="res://level.tscn", helper=False).command(5000)
    assert "--script" not in plain and "res://level.tscn" in plain
    windowed = RuntimeSession("g", "godot", "/p", windowed=True, resolution="320x200").command(5000)
    assert "--headless" not in windowed
    assert windowed[windowed.index("--position") + 1] == OFFSCREEN_POSITION
    assert windowed[windowed.index("--resolution") + 1] == "320x200"
    assert windowed[windowed.index("--audio-driver") + 1] == "Dummy", "a test run must be silent"
    assert "godotlens_offscreen" in windowed[windowed.index("--"):]


async def test_a_game_logs_to_a_file_of_its_own():
    """Not the project's user://logs, where Godot keeps only the newest five and the
    developer's own play and test logs would be pushed out."""
    command = RuntimeSession("g", "godot", "/p").command(5000)
    log = Path(command[command.index("--log-file") + 1])
    assert "godotlens-logs" in log.parts
    own = RuntimeSession("g", "godot", "/p", args=["--log-file", "mine.log"]).command(5000)
    assert own.count("--log-file") == 1, "a log file the caller chose is respected"


async def test_a_game_that_dies_before_connecting_says_what_it_printed(game, monkeypatch):
    monkeypatch.setenv("FAKE_GAME_MODE", "exit_early")
    payload, result = await tool("runtime_run", {"timeout": 10})
    assert result["isError"]
    assert payload["kind"] == "game_exited"
    assert "failing before it connects" in payload["detail"]


async def test_an_undecodable_frame_is_skipped_without_losing_the_stream(game, monkeypatch):
    monkeypatch.setenv("FAKE_GAME_MODE", "garbage")
    payload = await run()
    assert "after garbage" in [line["text"] for line in payload["lines"]]


async def test_a_folder_that_is_not_a_project_is_refused(game, tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_PROJECT_ROOT", str(tmp_path / "nowhere"))
    payload, result = await tool("runtime_run", {})
    assert result["isError"] and payload["kind"] == "no_project"


# ---------------------------------------------------------------------------
# Several games at once
# ---------------------------------------------------------------------------


async def test_each_agent_can_run_its_own_game(game):
    first, second = await run(), await run()
    assert first["session_id"] != second["session_id"]

    payload, result = await tool("runtime_tree", {})
    assert result["isError"] and payload["kind"] == "session_required"

    payload, result = await tool("runtime_tree", {"session_id": second["session_id"]})
    assert not result["isError"], payload

    status, _ = await tool("runtime_status")
    assert {s["session_id"] for s in status["sessions"]} == {first["session_id"], second["session_id"]}
    assert status["server_pid"] == os.getpid()


async def test_the_number_of_games_is_capped(game, monkeypatch):
    monkeypatch.setattr(runtime_tools, "MAX_SESSIONS", 1)
    await run()
    payload, result = await tool("runtime_run", {"collect": 0})
    assert result["isError"] and payload["kind"] == "too_many_sessions"


async def test_a_stopped_game_is_gone(game):
    started = await run()
    stopped, _ = await tool("runtime_stop", {"session_id": started["session_id"]})
    assert stopped["stopped"] is True
    payload, result = await tool("runtime_tree", {})
    assert result["isError"] and payload["kind"] == "no_session"


# ---------------------------------------------------------------------------
# Looking
# ---------------------------------------------------------------------------


async def test_the_tree_is_nested_with_paths_and_visibility(game):
    await run()
    payload, _ = await tool("runtime_tree")
    root = payload["tree"]
    assert root["path"] == "/root"
    main = next(c for c in root["children"] if c["name"] == "Main")
    enemies = main["children"][0]
    assert [e["path"] for e in enemies["children"]] == [
        "/root/Main/Enemies/Enemy", "/root/Main/Enemies/Enemy2"]
    assert enemies["children"][0]["visible"] is True
    assert enemies["children"][1]["visible"] is False
    assert payload["node_count"] == 6


async def test_the_tree_can_start_at_a_path_and_stop_at_a_depth(game):
    await run()
    payload, _ = await tool("runtime_tree", {"path": "Main", "max_depth": 1})
    assert payload["tree"]["path"] == "/root/Main"
    assert payload["deeper_nodes_not_shown"] == 2


@pytest.mark.parametrize("written", ["/root/Main/Enemies/Enemy", "Main/Enemies/Enemy", "Enemies/Enemy"])
async def test_a_node_can_be_named_the_way_an_agent_writes_it(game, written):
    await run()
    payload, result = await tool("runtime_inspect", {"node": written})
    assert not result["isError"], payload
    assert payload["path"] == "/root/Main/Enemies/Enemy"


async def test_an_unknown_node_is_reported(game):
    await run()
    payload, result = await tool("runtime_inspect", {"node": "Boss"})
    assert result["isError"] and payload["kind"] == "unknown_node"


async def test_inspect_reports_typed_values_without_inspector_layout_rows(game):
    await run()
    payload, _ = await tool("runtime_inspect", {"node": "Enemy"})
    props = payload["properties"]
    assert "Node2D" not in props and "Transform" not in props, "category and group rows leaked"
    assert props["position"] == {"type": "Vector2", "value": "Vector2(40, 50)"}
    assert props["hp"] == {"type": "int", "value": 100}


async def test_script_variables_are_shown_and_set_under_their_own_names(game):
    """Godot reports them as Members/<script path><name>; agents write plain names."""
    await run()
    payload, _ = await tool("runtime_inspect", {"node": "Enemy"})
    assert payload["properties"]["speed"] == {"type": "float", "value": 2.5}
    assert payload["constants"] == {"MAX_HP": 300}
    assert not [k for k in payload["properties"] if "/" in k]

    changed, result = await tool("runtime_set", {"node": "Enemy", "property": "speed", "value": 4})
    assert not result["isError"], changed
    assert changed["value"] == 4.0


async def test_inspect_can_return_only_some_properties(game):
    await run()
    payload, _ = await tool("runtime_inspect", {"node": "Enemy", "properties": ["hp", "nope"]})
    assert list(payload["properties"]) == ["hp"]
    assert payload["unknown_properties"] == ["nope"]


async def test_monitors_are_named_including_custom_ones(game):
    await run()
    payload, _ = await tool("runtime_monitors", {"names": ["OBJECT_ORPHAN_NODE_COUNT"]})
    assert payload["latest"]["TIME_FPS"] == 60.0
    assert "game/enemy_count" in payload["latest"]
    assert payload["history"]["OBJECT_ORPHAN_NODE_COUNT"], "history was not kept"


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ([1, 2], "Vector2(1, 2)"),
    ("Vector2(3.5, -1)", "Vector2(3.5, -1)"),
    ({"x": 7, "y": 8}, "Vector2(7, 8)"),
])
async def test_set_converts_to_the_property_type_and_reads_back(game, given, expected):
    await run()
    payload, result = await tool("runtime_set", {"node": "Enemy", "property": "position", "value": given})
    assert not result["isError"], payload
    assert payload["value"] == expected


async def test_set_of_an_int_and_a_colour(game):
    await run()
    hp, _ = await tool("runtime_set", {"node": "Enemy", "property": "hp", "value": "250"})
    assert hp["value"] == 250
    colour, _ = await tool("runtime_set", {"node": "Enemy", "property": "modulate", "value": "#ff0000"})
    assert colour["value"] == "Color(1, 0, 0, 1)"


async def test_set_refuses_an_unknown_property_and_suggests_close_ones(game):
    await run()
    payload, result = await tool("runtime_set", {"node": "Enemy", "property": "positio", "value": 1})
    assert result["isError"] and payload["kind"] == "unknown_property"
    assert "position" in payload["detail"]


async def test_set_refuses_a_value_of_the_wrong_shape(game):
    await run()
    payload, result = await tool("runtime_set", {"node": "Enemy", "property": "position", "value": [1, 2, 3]})
    assert result["isError"] and payload["kind"] == "bad_value"


async def test_pause_and_time_scale_reach_the_game(game):
    await run()
    await tool("runtime_pause", {"paused": True})
    await tool("runtime_time_scale", {"scale": 0.25})
    assert await game.received(["scene:suspend_changed", [True]])
    assert await game.received(["scene:speed_changed", [0.25]])


async def test_stepping_needs_a_window_and_a_pause(game):
    """Godot re-pauses after the next DRAWN frame; headless never draws one."""
    await run()
    payload, result = await tool("runtime_step", {})
    assert result["isError"] and payload["kind"] == "headless_session"
    await runtime_tools.stop_all()

    await run(windowed=True)
    payload, result = await tool("runtime_step", {})
    assert result["isError"] and payload["kind"] == "not_paused"
    await tool("runtime_pause", {"paused": True})
    payload, result = await tool("runtime_step", {})
    assert not result["isError"], payload
    assert await game.received(["scene:next_frame", []])


async def test_reload_is_refused_when_the_scripts_do_not_compile(game):
    await run()
    errors = {"player.gd": [{"line": 3, "severity": 1, "message": "Expected expression"}]}
    with patch.object(server, "_check_scripts", AsyncMock(return_value=errors)):
        payload, _ = await tool("runtime_reload", {"files": ["player.gd"]})
    assert payload["reloaded"] is False
    assert payload["errors"] == errors
    assert not [m for m in game.sent() if m[0] == "reload_scripts"]


async def test_reload_sends_res_paths_when_the_scripts_compile(game):
    await run()
    with patch.object(server, "_check_scripts", AsyncMock(return_value={})):
        payload, _ = await tool("runtime_reload", {"files": [str(game.root / "player.gd")]})
    assert payload["reloaded"] is True
    assert await game.received(["reload_scripts", ["res://player.gd"]])
    assert "warning" not in payload


async def test_reload_without_an_editor_warns_that_it_did_not_check(game):
    await run()
    with patch.object(server, "_check_scripts", AsyncMock(return_value=None)):
        payload, _ = await tool("runtime_reload", {"files": ["res://player.gd"]})
    assert payload["reloaded"] is True
    assert "Not checked" in payload["warning"]


# ---------------------------------------------------------------------------
# The helper: evaluate and press input while the game runs
# ---------------------------------------------------------------------------


async def test_eval_answers_while_the_game_runs(game):
    started = await run()
    assert started["helper"] is True
    payload, result = await tool("runtime_eval", {"expression": "6 * 7"})
    assert not result["isError"], payload
    assert payload["value"] == 42


async def test_eval_reports_the_engines_error_text(game):
    await run()
    payload, result = await tool("runtime_eval", {"expression": "nonsense"})
    assert result["isError"] and payload["kind"] == "game_refused"
    assert "nonsense" in payload["error"]


async def test_input_presses_and_releases_an_action(game):
    await run()
    payload, result = await tool("runtime_input", {"action": "jump", "frames": 3})
    assert not result["isError"], payload
    assert payload["released"] is True
    payload, result = await tool("runtime_input", {"action": "fly"})
    assert result["isError"] and "fly" in payload["error"]


async def test_without_the_helper_eval_says_why(game):
    await run(helper=False)
    payload, result = await tool("runtime_eval", {"expression": "6 * 7"})
    assert result["isError"] and payload["kind"] == "helper_missing"


async def test_a_project_with_its_own_main_loop_runs_without_the_helper(game, monkeypatch):
    monkeypatch.setattr(runtime_tools, "_main_loop_type", AsyncMock(return_value="MyLoop"))
    started = await run()
    assert started["helper"] is False
    assert "main loop" in started["helper_note"]


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


async def test_a_headless_game_has_no_pixels_to_capture(game):
    await run()
    payload, result = await tool("runtime_screenshot", {})
    assert result["isError"] and payload["kind"] == "headless_session"


async def test_a_screenshot_comes_back_as_an_image_and_the_file_is_removed(game):
    await run(windowed=True)
    payload, result = await tool("runtime_screenshot", {})
    assert not result["isError"], payload
    assert (payload["width"], payload["height"]) == (64, 32)
    image = result["content"][1]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]).startswith(b"\x89PNG")
    leftovers = await asyncio.to_thread(
        lambda: list(Path(os.environ.get("TEMP", "/tmp")).glob("fake-shot-*.png")))
    assert not leftovers, "the screenshot file was left behind"
