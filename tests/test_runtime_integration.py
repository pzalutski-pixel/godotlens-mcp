"""runtime_* against real Godot games, launched headless by GodotLens itself.

The session's headless editor has the same project open, as a developer's editor
would; the games run beside it on their own debugger connections. Nothing here opens
a window except the test marked ``windowed``.
"""

import os
import struct
import subprocess
import sys
import time
import zlib

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
    yield process, live_lsp.project
    status, _ = process.call_tool("runtime_status", {})
    for session in status.get("sessions", []):
        process.call_tool("runtime_stop", {"session_id": session["session_id"]})


def _run(process, scene, **extra):
    payload, is_error = process.call_tool("runtime_run", {"scene": scene, "collect": 2, **extra})
    assert not is_error, payload
    return payload


def _wait_for_line(process, sid, needle, timeout=10.0):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        payload, _ = process.call_tool("runtime_output", {"session_id": sid, "wait": 0.5})
        seen += [line["text"] for line in payload["lines"]]
        hits = [text for text in seen if needle in text]
        if hits:
            return hits
    raise AssertionError(f"{needle!r} never printed; last lines: {seen[-5:]}")


def test_a_game_shows_nodes_it_created_and_the_errors_it_raised(proc):
    process, _ = proc
    started = _run(process, "res://live_main.tscn")
    error = next(e for e in started["errors"] if "live error" in e["message"])
    assert error["file"] == "res://live_probe.gd"
    assert error["function"] == "_ready"

    tree, is_error = process.call_tool("runtime_tree", {"path": "Live"})
    assert not is_error, tree
    names = [child["name"] for child in tree["tree"]["children"]]
    assert {"Enemy0", "Enemy1", "Enemy2"} <= set(names), names


def test_a_launched_game_leaves_the_projects_own_logs_alone(proc):
    from tests.conftest import log_snapshot

    process, _ = proc
    before = log_snapshot("GodotLensTest")
    started = _run(process, "res://live_main.tscn")
    process.call_tool("runtime_stop", {"session_id": started["session_id"]})
    assert log_snapshot("GodotLensTest") == before


def test_inspect_and_set_on_a_live_node(proc):
    process, _ = proc
    _run(process, "res://live_main.tscn")

    enemy, _ = process.call_tool("runtime_inspect", {"node": "Live/Enemy1", "properties": ["position"]})
    assert enemy["properties"]["position"]["value"] == "Vector2(10, 5)"

    moved, is_error = process.call_tool(
        "runtime_set", {"node": "Live/Enemy1", "property": "position", "value": [123, 7]})
    assert not is_error, moved
    assert moved["value"] == "Vector2(123, 7)"

    hp, is_error = process.call_tool("runtime_set", {"node": "Live", "property": "hp", "value": 55})
    assert not is_error, hp
    assert hp["value"] == 55


def test_hot_reload_changes_code_and_keeps_state(proc):
    process, project = proc
    script = project / "live_probe.gd"
    original = script.read_text(encoding="utf-8")
    started = _run(process, "res://live_main.tscn")
    sid = started["session_id"]
    before = _wait_for_line(process, sid, "version 1")
    try:
        script.write_text(original.replace("const VERSION := 1", "const VERSION := 2"), encoding="utf-8")
        reloaded, is_error = process.call_tool("runtime_reload", {"files": ["live_probe.gd"]})
        assert not is_error, reloaded
        assert reloaded["reloaded"] is True, reloaded
        after = _wait_for_line(process, sid, "version 2")
        counter_before = int(before[-1].rsplit(" ", 1)[1])
        counter_after = int(after[0].rsplit(" ", 1)[1])
        assert counter_after > counter_before, "state was reset by the reload"
    finally:
        script.write_text(original, encoding="utf-8")
        process.call_tool("gdscript_sync_file", {"file": "live_probe.gd"})


def test_a_script_that_does_not_compile_is_not_reloaded(proc):
    process, project = proc
    script = project / "live_probe.gd"
    original = script.read_text(encoding="utf-8")
    _run(process, "res://live_main.tscn")
    try:
        script.write_text(original + "\nfunc broken(:\n", encoding="utf-8")
        payload, is_error = process.call_tool("runtime_reload", {"files": ["live_probe.gd"]})
        assert not is_error, payload
        assert payload["reloaded"] is False
        assert payload["errors"]["live_probe.gd"]
    finally:
        script.write_text(original, encoding="utf-8")
        process.call_tool("gdscript_sync_file", {"file": "live_probe.gd"})


def test_monitors_come_with_names_from_the_engine_including_custom_ones(proc):
    process, _ = proc
    _run(process, "res://live_main.tscn")
    payload, is_error = process.call_tool("runtime_monitors", {"names": ["OBJECT_NODE_COUNT"]})
    assert not is_error, payload
    assert payload["latest"]["game/enemy_count"] == 3
    assert "TIME_FPS" in payload["latest"]
    assert payload["history"]["OBJECT_NODE_COUNT"]


def test_pause_stops_the_game_and_unpause_resumes_it(proc):
    process, _ = proc
    _run(process, "res://live_main.tscn")

    def counter():
        payload, _ = process.call_tool("runtime_inspect", {"node": "Live", "properties": ["counter"]})
        return payload["properties"]["counter"]["value"]

    process.call_tool("runtime_pause", {"paused": True})
    frozen = counter()
    time.sleep(1.0)
    assert counter() == frozen, "the game kept running while paused"
    process.call_tool("runtime_pause", {"paused": False})
    time.sleep(1.0)
    assert counter() > frozen


def test_a_script_error_is_reported_and_the_game_keeps_running(proc):
    """Under a debugger an error would stop the game until resumed; nothing resumes it."""
    process, _ = proc
    started = _run(process, "res://crash_main.tscn")
    assert any("queue_free" in e["message"] for e in started["errors"]), started["errors"]
    status, _ = process.call_tool("runtime_status", {})
    assert status["sessions"][0]["running"] is True


def test_two_games_run_side_by_side(proc):
    process, _ = proc
    first = _run(process, "res://live_main.tscn")
    second = _run(process, "res://live_main.tscn")
    for sid in (first["session_id"], second["session_id"]):
        tree, is_error = process.call_tool("runtime_tree", {"session_id": sid, "max_depth": 1})
        assert not is_error, tree
    status, _ = process.call_tool("runtime_status", {})
    assert len(status["sessions"]) == 2


def test_eval_sees_autoloads_singletons_classes_and_the_scene(proc):
    process, _ = proc
    _run(process, "res://live_main.tscn")
    expectations = {
        "GameState.score + 1": 1,                # an autoload
        "Input.is_action_pressed(\"jump\")": False,  # an engine singleton
        "Player.MAX_HEALTH": 100,                # a class_name constant
        "hp * 2": 200,                           # the current scene's own member
        "get_child_count()": 4,                  # three enemies and a timer
    }
    for expression, expected in expectations.items():
        payload, is_error = process.call_tool("runtime_eval", {"expression": expression})
        assert not is_error, (expression, payload)
        assert payload["value"] == expected, (expression, payload)

    on_autoload, _ = process.call_tool("runtime_eval", {"expression": "score", "node": "GameState"})
    assert on_autoload["value"] == 0


def test_a_failing_expression_reports_and_the_game_keeps_running(proc):
    process, _ = proc
    _run(process, "res://live_main.tscn")
    payload, is_error = process.call_tool("runtime_eval", {"expression": "no_such_method_xyz()"})
    assert is_error and payload["kind"] == "game_refused"
    status, _ = process.call_tool("runtime_status", {})
    assert status["sessions"][0]["running"] is True


def test_an_injected_action_is_just_pressed_in_the_game(proc):
    """parse_input_event, not action_press: the latter never registers as just pressed."""
    process, _ = proc
    _run(process, "res://live_main.tscn")
    payload, is_error = process.call_tool("runtime_input", {"action": "jump", "frames": 2})
    assert not is_error, payload
    jumps, _ = process.call_tool("runtime_inspect", {"node": "Live", "properties": ["jumps"]})
    assert jumps["properties"]["jumps"]["value"] == 1


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in listing.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(sys.platform != "win32", reason="kill-on-close job objects are Windows-only")
def test_a_game_does_not_outlive_godotlens(mcp_process, live_lsp, godot_binary):
    """Killed outright, GodotLens cannot clean up; the job object must."""
    process = mcp_process(cwd=live_lsp.project, env={"GODOT_BIN": godot_binary})
    process.initialize()
    started = _run(process, "res://live_main.tscn")
    status, _ = process.call_tool("runtime_status", {})
    game_pids = [started["pid"], started["game_pid"]]
    assert all(_alive(pid) for pid in game_pids)

    subprocess.run(["taskkill", "/F", "/PID", str(status["server_pid"])], capture_output=True)
    deadline = time.time() + 15
    while time.time() < deadline and any(_alive(pid) for pid in game_pids):
        time.sleep(0.5)
    assert not any(_alive(pid) for pid in game_pids), "the game outlived GodotLens"


# ---------------------------------------------------------------------------
# A real window (opt-in)
# ---------------------------------------------------------------------------


def _windowed_allowed() -> bool:
    if os.environ.get("GODOTLENS_WINDOWED_TESTS") == "1":
        return True
    return sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))


def _pixel(png: bytes, x: int, y: int) -> tuple:
    """The RGB(A) value at (x, y) of a non-interlaced 8-bit PNG, stdlib only."""
    width, _height, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert depth == 8, "only 8-bit PNGs are read here"
    channels = {2: 3, 6: 4}[colour]
    data, pos = b"", 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        kind = png[pos + 4:pos + 8]
        if kind == b"IDAT":
            data += png[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(data)
    stride = width * channels
    previous = bytearray(stride)
    for row in range(y + 1):
        filter_type = raw[row * (stride + 1)]
        line = bytearray(raw[row * (stride + 1) + 1:(row + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + up) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 0xFF
        previous = line
    return tuple(previous[x * channels:(x + 1) * channels])


@pytest.mark.windowed
@pytest.mark.skipif(not _windowed_allowed(), reason="opens a game window; set GODOTLENS_WINDOWED_TESTS=1")
def test_a_screenshot_shows_what_the_game_draws(proc):
    import base64

    process, _ = proc
    started = _run(process, "res://shot_main.tscn", windowed=True, resolution="320x240")
    response = process.request("tools/call", {"name": "runtime_screenshot",
                                              "arguments": {"session_id": started["session_id"]}})
    content = response["result"]["content"]
    assert not response["result"]["isError"], content[0]["text"]
    png = base64.b64decode(content[1]["data"])
    assert png.startswith(b"\x89PNG")
    red = _pixel(png, 60, 60)
    assert red[0] > 200 and red[1] < 50 and red[2] < 50, f"expected the red rectangle, got {red}"
