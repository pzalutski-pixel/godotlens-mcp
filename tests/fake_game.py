"""A stand-in for a Godot game connected to GodotLens as its debugger.

Run as ``python fake_game.py <port>``: it connects to 127.0.0.1:<port> the way a game
started with --remote-debug does, and speaks the same binary protocol, so the runtime
tools can be tested without Godot. Behaviour is fixed and small: a scene tree with a
Main node and two enemies, properties that can be read and set, a performance frame
every 0.2 s, and answers to screenshot and reload requests.

Messages GodotLens sends are appended, decoded, to the file named by FAKE_GAME_LOG so
tests can check what was sent.
"""

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from godotlens_mcp.runtime import variant

ROOT, MAIN, ENEMIES, ENEMY_A, ENEMY_B = 1001, 1002, 1003, 1004, 1005

# Godot's flat depth-first tree: child_count, name, type, id, scene file, view flags.
TREE = [
    2, "root", "Window", ROOT, "", 0,
    0, "GameState", "res://game_state.gd", 1006, "", 0,
    1, "Main", "res://main.gd", MAIN, "res://main.tscn", 2 | 4 | 8,
    2, "Enemies", "Node2D", ENEMIES, "", 2 | 4 | 8,
    0, "Enemy", "Enemy", ENEMY_A, "res://enemy.tscn", 2 | 4 | 8,
    0, "Enemy2", "Enemy", ENEMY_B, "res://enemy.tscn", 2 | 8,
]

# name -> [type id, value]; a category row and a group row, as the inspector sends.
PROPERTIES = {
    ENEMY_A: {"position": [5, variant.Value("Vector2", (40.0, 50.0))], "hp": [2, 100],
              "label": [4, "grunt"], "modulate": [20, variant.Value("Color", (1.0, 1.0, 1.0, 1.0))],
              # Script variables and constants, named the way SceneDebuggerObject names them.
              "Members/res://enemy.gd/speed": [3, 2.5], "Constants/MAX_HP": [2, 300]},
    MAIN: {"score": [2, 0]},
}


def _rows(object_id):
    rows = [["Node2D", 0, 0, "", 128, None], ["Transform", 0, 0, "", 64, None]]
    for name, (type_id, value) in PROPERTIES.get(object_id, {}).items():
        rows.append([name, type_id, 0, "", 6, value])
    return rows


def main():
    port = int(sys.argv[1])
    log = os.environ.get("FAKE_GAME_LOG")
    mode = os.environ.get("FAKE_GAME_MODE", "")
    if mode == "exit_early":
        print("fake game: failing before it connects", flush=True)
        sys.exit(3)

    sock = socket.create_connection(("127.0.0.1", port))
    lock = threading.Lock()

    def send(name, data):
        with lock:
            sock.sendall(variant.message(name, data))

    send("set_pid", [os.getpid()])
    send("lens:ready", [1])  # as the helper announces itself
    send("output", [variant.Packed("PackedStringArray", ("fake game started\nsecond line",)),
                    variant.Packed("PackedInt32Array", (0,))])
    send("error", [0, 0, 1, 5, "core/variant/variant_utility.cpp", "push_error", 1, "spawn failed",
                   "", False, 3, "res://main.gd", "_ready", 12])
    if mode == "garbage":
        with lock:
            sock.sendall(struct.pack("<I", 8) + b"\xff" * 8)  # undecodable, but framed
        send("output", [["after garbage"], [0]])

    def frames():
        while True:
            orphans = 2 + int(time.time()) % 3
            send("performance:profile_frame",
                 [60.0, 0.016, 0.0, 0.0, 1.0, 2.0, 3.0, 400, 10, 30, orphans, 7.0])
            time.sleep(0.2)

    send("performance:profile_names", [["game/enemy_count"], [0]])
    threading.Thread(target=frames, daemon=True).start()

    buffer = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buffer += chunk
        while len(buffer) >= 4:
            (size,) = struct.unpack("<I", buffer[:4])
            if len(buffer) < 4 + size:
                break
            message, _ = variant.decode(buffer[4:4 + size])
            buffer = buffer[4 + size:]
            name, _thread, data = message
            if log:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps([name, variant.to_json(data)]) + "\n")
            if name == "scene:request_scene_tree":
                send("scene:scene_tree", TREE)
            elif name == "scene:inspect_objects":
                found = [[oid, "Node2D", _rows(oid)] for oid in data[0] if oid in PROPERTIES]
                send("scene:inspect_objects" if found else "remote_nothing_selected", found)
            elif name == "scene:set_object_property":
                object_id, prop, value = data
                # Godot keeps only the last path segment of a "Members/..." name.
                for key in PROPERTIES.get(object_id, {}):
                    if key.rsplit("/", 1)[-1] == prop.rsplit("/", 1)[-1] and not key.startswith("Constants/"):
                        PROPERTIES[object_id][key][1] = value
            elif name == "scene:rq_screenshot":
                path = Path(tempfile.gettempdir()) / f"fake-shot-{os.getpid()}.png"
                path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
                send("game_view:get_screenshot", [data[0], 64, 32, str(path)])
            elif name == "lens:hello":
                send("lens:hello", [data[0], 1])
            elif name == "lens:eval":
                request_id, expression = data[0], data[1]
                if expression == "6 * 7":
                    send("lens:result", [request_id, True, 42])
                else:
                    send("lens:result", [request_id, False, f"Invalid named index '{expression}'"])
            elif name == "lens:action":
                send("lens:result", [data[0], data[1] == "jump", data[1] if data[1] == "jump"
                                     else f"No input action named {data[1]}."])
            elif name == "quit":
                return


if __name__ == "__main__":
    main()
