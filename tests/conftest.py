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
        self._server: asyncio.AbstractServer | None = None
        self._peers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
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

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._peers.append(writer)
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

[rendering]

renderer/rendering_method="gl_compatibility"
'''

GAME_STATE_GD = '''extends Node

var score: int = 0


func add_score(amount: int) -> void:
\tscore += amount
'''


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


@pytest.fixture(scope="session")
def live_lsp(godot_binary, godot_project_session):
    """Launch one headless Godot editor for the session; yield (port, project root).

    Verified on Godot 4.7.1: --editor --headless --lsp-port brings the language
    server up with no GUI, which is what makes these tests runnable in CI.

    Session-scoped on purpose — a cold editor boot plus project import takes tens of
    seconds, so per-test launches make the suite unusable.
    """
    port = free_port()
    log_path = godot_project_session.parent / "godot-editor.log"
    # Never use PIPE here: Godot's import output is verbose enough to fill the pipe
    # buffer, and with nobody draining it the process blocks before it binds the port.
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [godot_binary, "--path", str(godot_project_session), "--editor", "--headless",
         "--lsp-port", str(port)],
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_port(port):
            proc.kill()
            log_file.close()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            pytest.skip(f"Godot LSP did not come up on port {port}. Editor log tail:\n{tail}")
        yield port, godot_project_session
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
