"""Shared fixtures.

The suite that shipped before this file could not catch the bugs that mattered: it
never opened a socket, never split an LSP frame across TCP segments, and mocked the
LSP client with a bare AsyncMock that would keep passing if the real class changed.
These fixtures exist so those failure modes are reachable from a test.
"""

import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from godotlens_mcp.lsp_client import LSPClient

# ---------------------------------------------------------------------------
# LSP framing helpers
# ---------------------------------------------------------------------------


def frame(payload: dict) -> bytes:
    """Encode a JSON-RPC payload as a Content-Length framed LSP message."""
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class FakeLSP:
    """A real TCP server that speaks LSP framing, scripted per test.

    Unlike a mocked reader this exercises the actual socket path, so it can reproduce
    short reads (``chunk_size``), unsolicited notifications, and server-initiated
    requests — the three things that broke the client in production.
    """

    def __init__(self):
        self.port: int = 0
        self.responses: dict[str, object] = {}      # method -> result
        self.errors: dict[str, dict] = {}           # method -> JSON-RPC error object
        self.push_before_response: list[dict] = []  # sent ahead of the next response
        self.chunk_size: int | None = None          # split writes to force short reads
        self.delay_before_response: float = 0.0     # provoke client-side timeouts
        self.silent_methods: set[str] = set()       # never answer these
        self.drop_connection: bool = False          # simulate Godot dying mid-call
        self.received: list[dict] = []
        # Unsolicited traffic, independent of requests, as a running game produces:
        # one stream_message(i) every stream_interval seconds after connect.
        self.stream_interval: float | None = None
        self.stream_message = lambda i: {"jsonrpc": "2.0", "method": "window/logMessage",
                                         "params": {"type": 3, "message": f"tick {i}"}}
        # Written all at once on connect; burst_done is set once the client has
        # accepted every byte. A client that only reads inside calls never lets the
        # write complete, which is exactly how it would freeze Godot's editor.
        self.burst_after_connect: list[dict] = []
        self.burst_done = asyncio.Event()
        # Called with each client notification; whatever it returns is pushed back,
        # as Godot answers didOpen/didChange with publishDiagnostics.
        self.notification_hook = None
        self._background: list[asyncio.Task] = []
        self._server: asyncio.AbstractServer | None = None
        self._peers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        for task in self._background:
            task.cancel()
        self._background.clear()
        # Close live peers first: wait_closed() blocks on in-flight handlers, so
        # leaving a connected client attached would deadlock teardown.
        for peer in self._peers:
            try:
                peer.close()
            except Exception:
                pass
        self._peers.clear()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    def count(self, method: str) -> int:
        return sum(1 for m in self.received if m.get("method") == method)

    async def wait_for_method(self, method: str, count: int = 1, timeout: float = 5.0) -> None:
        """Await at least ``count`` messages of ``method``.

        Notifications are fire-and-forget, so the client returns before the server has
        necessarily read them; asserting immediately is a race.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.count(method) >= count:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"expected >= {count} {method}, saw {self.count(method)}; "
            f"received methods: {[m.get('method') for m in self.received]}")

    async def wait_for_received(self, predicate, timeout: float = 5.0) -> dict:
        """Await a message matching ``predicate``; the client writes asynchronously."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for msg in self.received:
                if predicate(msg):
                    return msg
            await asyncio.sleep(0.02)
        raise AssertionError("no received message matched within timeout")

    async def _write(self, writer: asyncio.StreamWriter, payload: dict):
        data = frame(payload)
        if self.chunk_size:
            for i in range(0, len(data), self.chunk_size):
                writer.write(data[i:i + self.chunk_size])
                await writer.drain()
                await asyncio.sleep(0.01)  # force a separate TCP segment
        else:
            writer.write(data)
            await writer.drain()

    def _start_background(self, writer: asyncio.StreamWriter) -> None:
        """Begin the configured unsolicited traffic for a newly connected client."""
        loop = asyncio.get_event_loop()
        if self.burst_after_connect:
            async def burst():
                for message in self.burst_after_connect:
                    writer.write(frame(message))
                await writer.drain()
                self.burst_done.set()
            self._background.append(loop.create_task(burst()))
        if self.stream_interval:
            async def stream():
                i = 0
                while True:
                    await asyncio.sleep(self.stream_interval)
                    i += 1
                    await self._write(writer, self.stream_message(i))
            self._background.append(loop.create_task(stream()))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._peers.append(writer)
        self._start_background(writer)
        try:
            while True:
                headers = {}
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    text = line.decode().strip()
                    if not text:
                        break
                    if ":" in text:
                        key, val = text.split(":", 1)
                        headers[key.strip().lower()] = val.strip()

                length = int(headers.get("content-length", 0))
                if length <= 0:
                    continue
                msg = json.loads((await reader.readexactly(length)).decode())
                self.received.append(msg)

                method = msg.get("method", "")
                if "id" not in msg:
                    for pushed in (self.notification_hook(msg) if self.notification_hook else []):
                        await self._write(writer, pushed)
                    continue  # a notification; nothing to answer
                if "method" not in msg:
                    continue  # a response/error FROM the client; never answer it

                for pending in self.push_before_response:
                    await self._write(writer, pending)
                self.push_before_response = []

                if self.drop_connection:
                    writer.close()  # peer vanishes mid-request, as a killed editor would
                    return

                if method in self.silent_methods:
                    continue

                if self.delay_before_response:
                    await asyncio.sleep(self.delay_before_response)

                if method in self.errors:
                    await self._write(writer, {
                        "jsonrpc": "2.0", "id": msg["id"], "error": self.errors[method]})
                else:
                    await self._write(writer, {
                        "jsonrpc": "2.0", "id": msg["id"],
                        "result": self.responses.get(method)})
        except (asyncio.IncompleteReadError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            try:
                writer.close()
            except Exception:
                pass


class FakeDAP(FakeLSP):
    """A fake debug adapter.

    DAP shares LSP's Content-Length framing but correlates on seq/request_seq and
    carries an explicit message type, so responses and events need their own shapes.
    """

    def __init__(self):
        super().__init__()
        self.bodies: dict[str, object] = {}
        self.failures: dict[str, str] = {}
        self.push_events: list[dict] = []
        self.stream_message = lambda i: {"seq": 0, "type": "event", "event": "output",
                                         "body": {"category": "stdout", "output": f"tick {i}\n"}}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._peers.append(writer)
        self._start_background(writer)
        try:
            while True:
                headers = {}
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    text = line.decode().strip()
                    if not text:
                        break
                    if ":" in text:
                        key, val = text.split(":", 1)
                        headers[key.strip().lower()] = val.strip()

                length = int(headers.get("content-length", 0))
                if length <= 0:
                    continue
                msg = json.loads((await reader.readexactly(length)).decode())
                self.received.append(msg)

                if msg.get("type") != "request":
                    continue
                command = msg.get("command", "")

                for event in self.push_events:
                    await self._write(writer, event)
                self.push_events = []

                if self.drop_connection:
                    writer.close()  # adapter vanishes mid-request, as a killed editor would
                    return

                if command in self.silent_methods:
                    continue

                if command in self.failures:
                    await self._write(writer, {
                        "seq": 0, "type": "response", "request_seq": msg["seq"],
                        "command": command, "success": False,
                        "message": self.failures[command]})
                else:
                    await self._write(writer, {
                        "seq": 0, "type": "response", "request_seq": msg["seq"],
                        "command": command, "success": True,
                        "body": self.bodies.get(command, {})})
        except (asyncio.IncompleteReadError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            try:
                writer.close()
            except Exception:
                pass


@pytest.fixture
async def fake_dap():
    server = FakeDAP()
    await server.start()
    server.bodies["initialize"] = {
        "supportsConfigurationDoneRequest": True,
        "supportsTerminateRequest": True,
        "supportsEvaluateForHovers": True,
    }
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def dap_client(fake_dap):
    from godotlens_mcp.dap_client import DAPClient

    client = DAPClient(port=fake_dap.port, timeout=5.0)
    ok, msg = await client.connect()
    assert ok, msg
    try:
        yield client
    finally:
        await client.disconnect()


@pytest.fixture
async def fake_lsp():
    server = FakeLSP()
    await server.start()
    # initialize must succeed for LSPClient.connect() to complete.
    server.responses["initialize"] = {"capabilities": {}}
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def connected_client(fake_lsp):
    """An LSPClient already connected to the fake server."""
    client = LSPClient(port=fake_lsp.port, timeout=5.0)
    ok, msg = await client.connect()
    assert ok, msg
    try:
        yield client
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# MCP stdio harness
# ---------------------------------------------------------------------------


class MCPProcess:
    """Drives the real server over stdio, one message at a time."""

    def __init__(self, cwd: str, env: dict | None = None):
        merged = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "godotlens_mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=merged,
        )
        self._id = 0

    def send_raw(self, line: str) -> None:
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def read(self) -> dict | None:
        line = self.proc.stdout.readline()
        return json.loads(line.decode("utf-8")) if line.strip() else None

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))
        return self.read()

    def initialize(self) -> dict:
        resp = self.request("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"}})
        self.send_raw('{"jsonrpc":"2.0","method":"notifications/initialized"}')
        return resp

    def call_tool(self, name: str, arguments: dict) -> tuple[object, bool]:
        """Returns (parsed content, isError)."""
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        result = resp.get("result", {})
        text = result.get("content", [{}])[0].get("text", "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        return parsed, bool(result.get("isError"))

    def close(self) -> str:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        return self.proc.stderr.read().decode("utf-8", "replace")


@pytest.fixture
def mcp_process(tmp_path):
    """Factory for MCP server subprocesses; each is torn down at test end."""
    spawned: list[MCPProcess] = []

    def _make(cwd: str | None = None, env: dict | None = None) -> MCPProcess:
        proc = MCPProcess(cwd=str(cwd or tmp_path), env=env)
        spawned.append(proc)
        return proc

    yield _make
    for proc in spawned:
        proc.close()


# ---------------------------------------------------------------------------
# Godot project fixture
# ---------------------------------------------------------------------------

PLAYER_GD = '''class_name Player
extends CharacterBody2D

signal health_changed(new_health: int)

const MAX_HEALTH: int = 100

@export var speed: float = 300.0

var health: int = MAX_HEALTH


func take_damage(amount: int) -> void:
\thealth -= amount
\thealth_changed.emit(health)


func die() -> void:
\tqueue_free()


# Wired ONLY in main.tscn via a [connection] block; nothing in .gd calls it.
func _on_hit_area_entered(area: Area2D) -> void:
\ttake_damage(10)
'''

ENEMY_GD = '''extends Node2D

const DAMAGE: int = 25

var target: Player = null


func attack() -> void:
\tif target != null:
\t\ttarget.take_damage(DAMAGE)
\t# take_damage in a comment - grep would match, the LSP must not
'''

BROKEN_GD = '''extends Node


func good() -> void:
\tprint(1)


func bad() -> void:
\tthis is not valid !!
'''

MAIN_TSCN = '''[gd_scene load_steps=3 format=3 uid="uid://bqxvtest00001"]

[ext_resource type="Script" path="res://player.gd" id="1_player"]
[ext_resource type="Script" path="res://enemy.gd" id="2_enemy"]

[node name="Main" type="Node2D"]

[node name="Player" type="CharacterBody2D" parent="."]
script = ExtResource("1_player")
speed = 250.0

[node name="HitArea" type="Area2D" parent="Player"]

[node name="HealthBar" type="ProgressBar" parent="."]
unique_name_in_owner = true

[node name="Enemy" type="Node2D" parent="."]
script = ExtResource("2_enemy")

[connection signal="area_entered" from="Player/HitArea" to="Player" method="_on_hit_area_entered"]
'''

PROJECT_GODOT = '''config_version=5

[application]

config/name="GodotLensTest"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.6", "GL Compatibility")

[autoload]

GameState="*res://game_state.gd"

[editor_overrides]

interface/editor/import_resources_when_unfocused=false

[input]

jump={
"deadzone": 0.5,
"events": []
}

[rendering]

renderer/rendering_method="gl_compatibility"
'''

# The 4.6 key name on purpose: 4.7 renamed the setting and migrates an override under
# the old name on load, so this also checks that the name the README gives works.
RESCAN_OVERRIDE = "interface/editor/import_resources_when_unfocused="

RUNTIME_PROBE_GD = '''extends Node2D

## Prints identifiable output and then quits, so a debug_run test terminates itself.
##
## The quit is deferred deliberately. Calling get_tree().quit() straight from _ready
## tears the debugger connection down before the print output is flushed, so the
## harness sees the engine banner and nothing else.


func _ready() -> void:
	print("GODOTLENS_TEST: started")
	print("GODOTLENS_TEST: score is ", GameState.score)
	await get_tree().create_timer(1.5).timeout
	print("GODOTLENS_TEST: quitting")
	get_tree().quit()
'''

RUNTIME_MAIN_TSCN = '''[gd_scene load_steps=2 format=3 uid="uid://bqxvruntest01"]

[ext_resource type="Script" path="res://runtime_probe.gd" id="1_probe"]

[node name="RuntimeMain" type="Node2D"]
script = ExtResource("1_probe")
'''

GAME_STATE_GD = '''extends Node

var score: int = 0


func add_score(amount: int) -> void:
\tscore += amount
'''


# Scenes whose nodes come from another scene. The file stores only overrides for
# those nodes (an empty type, usually no script), so validating against the raw
# per-file state flagged every one of these correct connections.
HANDLER_GD = '''extends Node2D


func _on_button_pressed() -> void:
\tpass
'''

BASE_TSCN = '''[gd_scene format=3]

[ext_resource type="Script" path="res://handler.gd" id="1_handler"]

[node name="Base" type="Node2D"]
script = ExtResource("1_handler")

[node name="Button" type="Button" parent="."]

[node name="Panel" type="Panel" parent="."]
'''

# Inherits base.tscn and wires the base's own Button to the base's root script.
INHERITED_TSCN = '''[gd_scene format=3]

[ext_resource type="PackedScene" path="res://base.tscn" id="1_base"]

[node name="Base" instance=ExtResource("1_base")]

[node name="Button" parent="." index="0"]
text = "Go"

[connection signal="pressed" from="Button" to="." method="_on_button_pressed"]
'''

# Instances base.tscn as a child and connects into it.
INSTANCER_TSCN = '''[gd_scene format=3]

[ext_resource type="PackedScene" path="res://base.tscn" id="1_base"]

[node name="Root" type="Node"]

[node name="Child" parent="." instance=ExtResource("1_base")]

[connection signal="pressed" from="Child/Button" to="Child" method="_on_button_pressed"]
'''

# Two levels deep: instancer.tscn inside another scene.
NESTED_TSCN = '''[gd_scene format=3]

[ext_resource type="PackedScene" path="res://instancer.tscn" id="1_instancer"]

[node name="Top" type="Node"]

[node name="Outer" parent="." instance=ExtResource("1_instancer")]

[connection signal="pressed" from="Outer/Child/Button" to="Outer/Child" method="_on_button_pressed"]

[editable path="Outer"]
'''

# Editable children: a node added inside the instance, plus a connection into it.
EDITABLE_TSCN = '''[gd_scene format=3]

[ext_resource type="PackedScene" path="res://base.tscn" id="1_base"]

[node name="Root" type="Node"]

[node name="Child" parent="." instance=ExtResource("1_base")]

[node name="Extra" type="Label" parent="Child/Panel"]

[connection signal="pressed" from="Child/Button" to="Child" method="_on_button_pressed"]

[editable path="Child"]
'''

# A script that names an autoload compiles only once autoloads are registered, which
# happens after a --script main loop is created.
AUTOLOAD_USER_GD = '''extends Node


func _ready() -> void:
\tGameState.add_score(1)
'''

AUTOLOAD_USER_TSCN = '''[gd_scene format=3]

[ext_resource type="Script" path="res://autoload_user.gd" id="1_user"]

[node name="User" type="Node"]
script = ExtResource("1_user")
'''

# Games for debug_run. Each quits on its own (deferred, so output is flushed first),
# and the tests pass --headless so no window opens.
ERRORS_PROBE_GD = '''extends Node

## push_error and push_warning reach the debugger only as structured "error" messages.


func _ready() -> void:
\tpush_error("GODOTLENS_TEST: pushed an error")
\tpush_warning("GODOTLENS_TEST: pushed a warning")
\tawait get_tree().create_timer(1.0).timeout
\tget_tree().quit()
'''

CRASH_PROBE_GD = '''extends Node

## A runtime script error: the game breaks into the debugger on this line.


func _ready() -> void:
\tvar missing: Node = null
\tmissing.queue_free()
'''

# Prints every frame, as a chatty game does, then quits after LIMIT seconds.
CHATTY_PROBE_GD = '''extends Node

const LIMIT := {limit}

var elapsed := 0.0


func _process(delta: float) -> void:
\telapsed += delta
\tprint("GODOTLENS_TEST: tick ", Engine.get_process_frames())
\tif elapsed > LIMIT:
\t\tget_tree().quit()
'''

# Breakpoints go on the marked lines: one in a static function, which has no script
# instance to evaluate against, and one in an instance method, which does.
GUARD_PROBE_GD = '''extends Node

var hp: int = 100


static func helper(x: int) -> int:
\tvar doubled := x * 2  # BREAK_STATIC
\treturn doubled


func _ready() -> void:
\tvar total := helper(21)
\tprint("GODOTLENS_TEST: total ", total)  # BREAK_INSTANCE
\tawait get_tree().create_timer(1.0).timeout
\tget_tree().quit()
'''


# For runtime_* against a real game: nodes created at runtime, a structured error, a
# custom monitor, and a printed counter and VERSION so pause and hot reload are visible.
# The monitor is a bound method; a lambda there crashed Godot on exit (measured, 4.7.1).
LIVE_PROBE_GD = '''extends Node2D

const VERSION := 1

var counter := 0
var hp := 100
var jumps := 0


func _ready() -> void:
\tfor i in 3:
\t\tvar enemy := Node2D.new()
\t\tenemy.name = "Enemy%d" % i
\t\tenemy.position = Vector2(i * 10, 5)
\t\tadd_child(enemy)
\tPerformance.add_custom_monitor("game/enemy_count", _enemy_count)
\tpush_error("GODOTLENS_TEST: live error")
\tvar timer := Timer.new()
\ttimer.wait_time = 0.3
\ttimer.autostart = true
\ttimer.timeout.connect(_tick)
\tadd_child(timer)


func _enemy_count() -> int:
\treturn get_child_count() - 1


func _process(_delta: float) -> void:
\tif Input.is_action_just_pressed("jump"):
\t\tjumps += 1


func _tick() -> void:
\tcounter += 1
\tprint("GODOTLENS_TEST: version ", VERSION, " counter ", counter)
'''

LIVE_MAIN_TSCN = '''[gd_scene load_steps=2 format=3 uid="uid://bqxvlive000001"]

[ext_resource type="Script" path="res://live_probe.gd" id="1_live"]

[node name="Live" type="Node2D"]
script = ExtResource("1_live")
'''

# A known picture for the windowed screenshot test: a red rectangle on the clear colour.
SHOT_MAIN_TSCN = '''[gd_scene format=3 uid="uid://bqxvshot000001"]

[node name="Shot" type="Node2D"]

[node name="Red" type="ColorRect" parent="."]
offset_left = 20.0
offset_top = 20.0
offset_right = 220.0
offset_bottom = 120.0
color = Color(1, 0, 0, 1)
'''


def _single_script_scene(script: str, uid: str) -> str:
    return (f'[gd_scene load_steps=2 format=3 uid="uid://{uid}"]\n\n'
            f'[ext_resource type="Script" path="res://{script}" id="1_probe"]\n\n'
            f'[node name="Probe" type="Node"]\nscript = ExtResource("1_probe")\n')


RUNTIME_FIXTURES = {
    "errors_probe.gd": ERRORS_PROBE_GD,
    "errors_main.tscn": _single_script_scene("errors_probe.gd", "bqxverrors0001"),
    "crash_probe.gd": CRASH_PROBE_GD,
    "crash_main.tscn": _single_script_scene("crash_probe.gd", "bqxvcrash00001"),
    "chatty_probe.gd": CHATTY_PROBE_GD.replace("{limit}", "4.0"),
    "chatty_main.tscn": _single_script_scene("chatty_probe.gd", "bqxvchatty0001"),
    # Long enough to fill a socket nobody reads, for the editor-freeze regression test.
    "chatty_long_probe.gd": CHATTY_PROBE_GD.replace("{limit}", "75.0"),
    "chatty_long_main.tscn": _single_script_scene("chatty_long_probe.gd", "bqxvchatty0002"),
    "guard_probe.gd": GUARD_PROBE_GD,
    "guard_main.tscn": _single_script_scene("guard_probe.gd", "bqxvguard00001"),
    "live_probe.gd": LIVE_PROBE_GD,
    "live_main.tscn": LIVE_MAIN_TSCN,
    "shot_main.tscn": SHOT_MAIN_TSCN,
}

SCENE_FIXTURES = {
    "handler.gd": HANDLER_GD,
    "base.tscn": BASE_TSCN,
    "inherited.tscn": INHERITED_TSCN,
    "instancer.tscn": INSTANCER_TSCN,
    "nested.tscn": NESTED_TSCN,
    "editable.tscn": EDITABLE_TSCN,
    "autoload_user.gd": AUTOLOAD_USER_GD,
    "autoload_user.tscn": AUTOLOAD_USER_TSCN,
}


def project_log_dir(project_name: str) -> Path:
    """Where Godot writes a project's user://logs on this platform."""
    if sys.platform == "win32":
        base = Path(os.environ["APPDATA"]) / "Godot"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Godot"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "godot"
    return base / "app_userdata" / project_name / "logs"


def log_snapshot(project_name: str) -> dict:
    folder = project_log_dir(project_name)
    return {p.name: p.stat().st_mtime for p in folder.glob("*")} if folder.is_dir() else {}


def build_godot_project(root: Path) -> Path:
    """Write a minimal but realistic Godot project into ``root``.

    Deliberately includes a signal handler wired only in the .tscn, a cross-file
    call, a comment mentioning the symbol, and a file with a genuine syntax error.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text(PROJECT_GODOT, encoding="utf-8")
    (root / "player.gd").write_text(PLAYER_GD, encoding="utf-8")
    (root / "enemy.gd").write_text(ENEMY_GD, encoding="utf-8")
    (root / "broken.gd").write_text(BROKEN_GD, encoding="utf-8")
    (root / "game_state.gd").write_text(GAME_STATE_GD, encoding="utf-8")
    (root / "main.tscn").write_text(MAIN_TSCN, encoding="utf-8")
    (root / "runtime_probe.gd").write_text(RUNTIME_PROBE_GD, encoding="utf-8")
    (root / "runtime_main.tscn").write_text(RUNTIME_MAIN_TSCN, encoding="utf-8")
    for name, text in {**SCENE_FIXTURES, **RUNTIME_FIXTURES}.items():
        (root / name).write_text(text, encoding="utf-8")
    from tests.scene_mistakes import FILES as MISTAKE_FILES
    (root / "mistakes").mkdir(exist_ok=True)
    for name, text in MISTAKE_FILES.items():
        (root / "mistakes" / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def godot_project(tmp_path) -> Path:
    return build_godot_project(tmp_path / "proj")


@pytest.fixture(scope="session")
def godot_project_session(tmp_path_factory) -> Path:
    """One project shared by the integration suite: booting Godot is expensive."""
    return build_godot_project(tmp_path_factory.mktemp("live") / "proj")


# ---------------------------------------------------------------------------
# Live Godot (integration)
# ---------------------------------------------------------------------------


def find_godot_binary() -> str | None:
    """Locate a Godot editor binary: GODOT_BIN, then ./godot/, then PATH."""
    env_bin = os.environ.get("GODOT_BIN")
    if env_bin and os.path.isfile(env_bin):
        return env_bin

    repo_root = Path(__file__).resolve().parent.parent
    local = repo_root / "godot"
    if local.is_dir():
        # Prefer the console build on Windows: it keeps stdio usable.
        candidates = sorted(local.glob("*console*")) + sorted(local.iterdir())
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK) and path.suffix in (".exe", ""):
                return str(path)

    for name in ("godot", "godot4", "Godot"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    """Pick an unused port inside Godot's accepted range.

    Godot documents --lsp-port as "Recommended port range [1024, 49151]" and will not
    bind outside it. The OS ephemeral range on Windows starts well above 49151, so
    asking the OS for port 0 hands back a port Godot silently refuses.
    """
    for _ in range(200):
        candidate = random.randint(20000, 49000)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise RuntimeError("could not find a free port in Godot's accepted range")


def wait_for_port(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(1.0)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def godot_binary() -> str:
    binary = find_godot_binary()
    if not binary:
        pytest.skip("No Godot binary found (set GODOT_BIN or place one in ./godot/)")
    return binary


class LiveGodot(NamedTuple):
    """A running headless editor. Unpacks as (lsp_port, project) for existing tests."""

    lsp_port: int
    dap_port: int
    project: Path

    def __iter__(self):
        return iter((self.lsp_port, self.project))


def _run_editor(godot_binary: str, project: Path):
    """Launch a headless editor on ``project``; yield a LiveGodot, then stop it."""
    port = free_port()
    dap_port = free_port()
    log_path = project.parent / "godot-editor.log"
    # Never use PIPE here: Godot's import output is verbose enough to fill the pipe
    # buffer, and with nobody draining it the process blocks before it binds the port.
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [godot_binary, "--path", str(project), "--editor", "--headless",
         "--lsp-port", str(port), "--dap-port", str(dap_port)],
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_port(port):
            proc.kill()
            log_file.close()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            pytest.skip(f"Godot LSP did not come up on port {port}. Editor log tail:\n{tail}")
        # The debug adapter comes up with the editor, on its own port.
        wait_for_port(dap_port, timeout=30.0)
        yield LiveGodot(lsp_port=port, dap_port=dap_port, project=project)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


@pytest.fixture(scope="session")
def live_lsp(godot_binary, godot_project_session):
    """Launch one headless Godot editor for the session; yield (port, project root).

    Verified on Godot 4.7.1: --editor --headless --lsp-port brings the language
    server up with no GUI, which is what makes these tests runnable in CI.

    Session-scoped on purpose — a cold editor boot plus project import takes tens of
    seconds, so per-test launches make the suite unusable. Its project pins "Import
    Resources When Unfocused" off, whatever the machine's own editor settings say.
    """
    yield from _run_editor(godot_binary, godot_project_session)


@pytest.fixture(scope="session")
def live_lsp_rescanning(godot_binary, tmp_path_factory):
    """A second headless editor whose project turns "Import Resources When Unfocused" on.

    That editor rescans the filesystem every 0.5 s, which is how a class_name file
    it has never seen gets registered without the window ever gaining focus.
    """
    project = build_godot_project(tmp_path_factory.mktemp("rescan") / "proj")
    config = project / "project.godot"
    config.write_text(config.read_text(encoding="utf-8").replace(
        RESCAN_OVERRIDE + "false", RESCAN_OVERRIDE + "true"), encoding="utf-8")
    for live in _run_editor(godot_binary, project):
        # A freshly started editor defers change scans until its first full scan and
        # import finish (EditorFileSystem::scan_changes), so tests would race the
        # boot rather than check the warm behaviour an open editor has. Register a
        # canary class first, with a generous timeout.
        if not asyncio.run(_wait_until_new_classes_register(live)):
            pytest.skip("the rescanning editor never registered a new class_name file")
        yield live


async def _wait_until_new_classes_register(live: "LiveGodot", timeout: float = 90.0) -> bool:
    """Create canary class_name files until the editor registers one.

    A new canary each round, because a file created while the first scan is still
    walking the folder can be missed until the folder changes again.
    """
    from godotlens_mcp.lsp_client import canonical_key, file_uri

    previous_root = os.environ.get("GODOT_PROJECT_ROOT")
    os.environ["GODOT_PROJECT_ROOT"] = str(live.project)
    client = LSPClient(port=live.lsp_port, timeout=20.0)
    try:
        if not (await client.connect())[0]:
            return False
        deadline = time.time() + timeout
        round_no = 0
        while time.time() < deadline:
            round_no += 1
            name = f"RescanCanary{round_no}"
            (live.project / f"rescan_canary_{round_no}.gd").write_text(
                f"class_name {name}\nextends Node\n", encoding="utf-8")
            user = live.project / f"rescan_canary_user_{round_no}.gd"
            text = f"extends Node\n\n\nfunc _ready() -> void:\n\tprint({name}.new())\n"
            user.write_text(text, encoding="utf-8")
            key = canonical_key(str(user))
            round_end = min(deadline, time.time() + 10.0)
            while time.time() < round_end:
                client.diagnostics_cache.pop(key, None)
                await client.sync_document(file_uri(str(user)), text, notify_save=False)
                await client.wait_for_diagnostics([key], timeout=10.0)
                if key in client.diagnostics_cache and not any(
                        name in d.get("message", "") for d in client.diagnostics_cache[key]):
                    return True
                await asyncio.sleep(0.5)
        return False
    finally:
        await client.disconnect()
        if previous_root is None:
            os.environ.pop("GODOT_PROJECT_ROOT", None)
        else:
            os.environ["GODOT_PROJECT_ROOT"] = previous_root
