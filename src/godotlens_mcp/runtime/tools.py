"""The runtime_* tools: games GodotLens launches itself, observed and driven directly.

Each runtime_run starts its own game process, connected to its own port, so several
agents can each run a game at once without the editor's single run slot. Games run
headless unless a screenshot is wanted; a windowed run is placed far off-screen.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from godotlens_mcp.runtime import variant
from godotlens_mcp.runtime.session import (
    RuntimeSession,
    SessionError,
    import_project,
    project_is_imported,
)
from godotlens_mcp.scene import (
    GodotBinaryNotFound,
    dump_project,
    find_godot_binary,
    run_engine,
    to_res_path,
)

MAX_SESSIONS = int(os.environ.get("GODOT_RUNTIME_MAX_SESSIONS", "4"))
MONITOR_NAMES_SCRIPT = Path(__file__).with_name("monitor_names.gd")

# Property usage flags marking inspector layout rows, not properties (PropertyUsageFlags).
_LAYOUT_ROWS = 64 | 128 | 256  # GROUP | CATEGORY | SUBGROUP
# RemoteNode view flags (scene_debugger_object.h).
_HAS_VISIBLE, _VISIBLE, _VISIBLE_IN_TREE = 1 << 1, 1 << 2, 1 << 3
INSPECT_REPLIES = {"scene:inspect_objects", "remote_objects_selected", "remote_nothing_selected"}

SESSIONS: dict[str, RuntimeSession] = {}
_ids = itertools.count(1)
_monitor_names: dict[str, dict[int, str]] = {}
_main_loop_types: dict[str, str] = {}

_SESSION_ID = {"type": "string",
               "description": "Session from runtime_run. Required only when several games run."}
_NODE = {"type": "string",
         "description": ("Node path: '/root/Main/Player', 'Main/Player', or an unambiguous "
                         "tail such as 'Player'")}
_OBJECT_ID = {"type": "integer", "description": "Object id from runtime_tree or runtime_inspect"}


def _tool(name: str, description: str, properties: dict, required: list[str],
          read_only: bool, destructive: bool = False) -> dict:
    annotations = {"readOnlyHint": read_only, "openWorldHint": False}
    if not read_only:
        annotations.update({"destructiveHint": destructive, "idempotentHint": False})
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required},
            "annotations": annotations}


TOOLS = [
    _tool("runtime_run", (
        "Launch the game as its own process, connected straight back to GodotLens - no "
        "editor run slot, so several agents can each run one at once. "
        "Returns: session_id, and the output and structured errors from the first seconds. "
        "Runs HEADLESS by default (no window, no sound); pass windowed=true only when you "
        "need runtime_screenshot - the window is placed off-screen and takes no focus. "
        "Script errors are reported, never stop the game. Then use runtime_tree, "
        "runtime_inspect and runtime_output to see what the game is doing. "
        "The game runs through GodotLens's helper (loaded for this run only, nothing is "
        "written to the project), which adds runtime_eval and runtime_input."
    ), {
        "scene": {"type": "string", "description": "res:// scene to run (default: the main scene)"},
        "windowed": {"type": "boolean", "description": "Render a real window (needed for screenshots)"},
        "resolution": {"type": "string", "description": "Window size for windowed runs, e.g. '1280x720'"},
        "args": {"type": "array", "items": {"type": "string"},
                 "description": "Extra engine flags, e.g. ['--fixed-fps', '60']"},
        "collect": {"type": "number", "description": "Seconds of output to wait for (default 2)"},
        "timeout": {"type": "number", "description": "Seconds to wait for the game to connect (default 30)"},
        "helper": {"type": "boolean",
                   "description": "Run through the helper for runtime_eval/runtime_input (default true)"},
    }, [], read_only=False),
    _tool("runtime_stop", "Stop a game started with runtime_run. Returns: exit code.",
          {"session_id": _SESSION_ID}, [], read_only=False, destructive=True),
    _tool("runtime_status", (
        "List the games GodotLens is running. Returns: each session with pid, scene, "
        "whether it is headless, running and connected, plus this server's pid."
    ), {}, [], read_only=True),
    _tool("runtime_output", (
        "Read what a running game printed and every structured error it reported since "
        "the last call - push_error, push_warning, engine and script errors, each with "
        "file, function, ZERO-BASED line and call stack. Drained on each call."
    ), {"session_id": _SESSION_ID,
        "wait": {"type": "number", "description": "Seconds to wait for more first (default 0.5)"}},
        [], read_only=True),
    _tool("runtime_tree", (
        "The live scene tree of a running game, including nodes created at runtime. "
        "Returns: nested nodes with name, type (script class_name or path when scripted), "
        "path, object_id and visibility. Use the path or object_id with runtime_inspect."
    ), {"session_id": _SESSION_ID,
        "path": {"type": "string", "description": "Only this subtree, e.g. '/root/Main/Enemies'"},
        "max_depth": {"type": "integer", "description": "Levels below the start to include (default 8)"}},
        [], read_only=True),
    _tool("runtime_inspect", (
        "Current property values of one node or object in a running game, as the "
        "engine reports them. Returns: class and each property's type and value."
    ), {"session_id": _SESSION_ID, "node": _NODE, "object_id": _OBJECT_ID,
        "properties": {"type": "array", "items": {"type": "string"},
                       "description": "Only these properties (default: all)"}},
        [], read_only=True),
    _tool("runtime_set", (
        "Change a property on a node in a running game, then read it back. "
        "Values are converted to the property's own type: pass [1, 2] or 'Vector2(1, 2)' "
        "for a Vector2, '#ff0000' for a Color, a res:// path for a resource. "
        "Returns: the value the engine now reports."
    ), {"session_id": _SESSION_ID, "node": _NODE, "object_id": _OBJECT_ID,
        "property": {"type": "string", "description": "Property name, e.g. position"},
        "value": {"description": "New value"}},
        ["property", "value"], read_only=False),
    _tool("runtime_eval", (
        "Evaluate an expression in a running game, while it runs - no breakpoint needed. "
        "Uses Godot's Expression: calls, operators, member access and constructors, no "
        "statements or assignment. Names available: the base node's members (default the "
        "current scene), autoloads, engine singletons such as Input, and class_name "
        "globals. Calls run for real and may change the game. "
        "Returns: the value, or the engine's error text."
    ), {"session_id": _SESSION_ID,
        "expression": {"type": "string",
                       "description": "e.g. get_tree().get_nodes_in_group(\"enemies\").size()"},
        "node": {"type": "string", "description": "Base node path for member lookups (default: current scene)"}},
        ["expression"], read_only=False),
    _tool("runtime_input", (
        "Press an input action in a running game, as a player would: pressed for the given "
        "number of frames, then released, through the normal input path, so "
        "is_action_just_pressed and _unhandled_input see it. Actions come from the project's "
        "Input Map (project_config lists them). Returns: once released."
    ), {"session_id": _SESSION_ID,
        "action": {"type": "string", "description": "Input Map action, e.g. jump"},
        "frames": {"type": "integer", "description": "Frames to hold it (default 2)"},
        "strength": {"type": "number", "description": "0..1 for analog actions (default 1)"}},
        ["action"], read_only=False),
    _tool("runtime_reload", (
        "Hot-reload changed scripts into a running game, keeping its state. "
        "Refuses when the language server reports errors in them: a script that fails "
        "to compile is disabled in the running game. Covers .gd scripts only - edited "
        ".tscn files and new or changed assets need a restart."
    ), {"session_id": _SESSION_ID,
        "files": {"type": "array", "items": {"type": "string"}, "description": "Scripts to reload"}},
        ["files"], read_only=False),
    _tool("runtime_pause", (
        "Freeze or unfreeze a running game's scene tree. Returns: the new state."
    ), {"session_id": _SESSION_ID, "paused": {"type": "boolean", "description": "true to freeze"}},
        ["paused"], read_only=False),
    _tool("runtime_time_scale", (
        "Speed up or slow down a running game. Returns: the new scale."
    ), {"session_id": _SESSION_ID, "scale": {"type": "number", "description": "1.0 is normal speed"}},
        ["scale"], read_only=False),
    _tool("runtime_step", (
        "Advance a paused game by one frame. Windowed sessions only: a headless game "
        "never draws a frame, and Godot re-pauses after the next drawn frame."
    ), {"session_id": _SESSION_ID}, [], read_only=False),
    _tool("runtime_monitors", (
        "Performance monitors of a running game, about once a second: FPS, frame times, "
        "object, node and orphan-node counts, memory, draw calls, physics, plus any "
        "custom monitors the game registers. Returns: latest values, and a history of the "
        "requested monitors - a rising orphan count is the usual sign of a leak."
    ), {"session_id": _SESSION_ID,
        "names": {"type": "array", "items": {"type": "string"},
                  "description": "Monitors to return history for, e.g. ['OBJECT_ORPHAN_NODE_COUNT']"}},
        [], read_only=True),
    _tool("runtime_screenshot", (
        "Capture what a windowed game is drawing. Returns: the image, with its size. "
        "Needs a session started with windowed=true; a headless game has no pixels."
    ), {"session_id": _SESSION_ID}, [], read_only=True),
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}

# check_scripts(files) -> {file: [error, ...]} for files with errors, or None when the
# language server is not available to check them.
ScriptCheck = Callable[[list[str]], Awaitable[dict | None]]


async def call(name: str, arguments: dict, project_root: str,
               check_scripts: ScriptCheck) -> tuple[dict, list[tuple[str, str]]]:
    """Run one runtime tool. Returns (result, images as (base64, mime type))."""
    if name == "runtime_run":
        return await _run(arguments, project_root), []
    if name == "runtime_status":
        return {"sessions": [s.describe() for s in SESSIONS.values()],
                "max_sessions": MAX_SESSIONS, "server_pid": os.getpid()}, []
    if name == "runtime_stop":
        session = _session(arguments)
        code = await session.stop()
        SESSIONS.pop(session.session_id, None)
        return {"session_id": session.session_id, "stopped": True, "exit_code": code}, []

    session = _session(arguments)
    if name == "runtime_output":
        await asyncio.sleep(max(0.0, min(float(arguments.get("wait", 0.5)), 30.0)))
        return _output(session), []
    if name == "runtime_screenshot":
        return await _screenshot(session)

    _require_live(session)
    if name == "runtime_tree":
        return await _tree(session, arguments), []
    if name == "runtime_inspect":
        return await _inspect(session, arguments), []
    if name == "runtime_set":
        return await _set(session, arguments), []
    if name == "runtime_eval":
        return await _eval(session, arguments), []
    if name == "runtime_input":
        return await _input(session, arguments), []
    if name == "runtime_reload":
        return await _reload(session, arguments, project_root, check_scripts), []
    if name == "runtime_pause":
        paused = bool(arguments.get("paused", True))
        await session.put("scene:suspend_changed", [paused])
        session.paused = paused
        return {"session_id": session.session_id, "paused": paused}, []
    if name == "runtime_time_scale":
        scale = float(arguments.get("scale", 1.0))
        await session.put("scene:speed_changed", [scale])
        return {"session_id": session.session_id, "time_scale": scale}, []
    if name == "runtime_step":
        return await _step(session), []
    if name == "runtime_monitors":
        return await _monitors(session, arguments, project_root), []
    raise SessionError(f"Unknown tool: {name}", "unknown_tool")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def _run(arguments: dict, project_root: str) -> dict:
    binary = find_godot_binary()
    if not binary:
        raise GodotBinaryNotFound(
            "No Godot executable found. Set GODOT_BIN to the editor binary, or place one in "
            "a ./godot/ directory. runtime_run launches the game with it.")
    if not await asyncio.to_thread(os.path.isfile, os.path.join(project_root, "project.godot")):
        raise SessionError(f"No Godot project at {project_root} (no project.godot).", "no_project",
                           "Run GodotLens from inside the project, or set GODOT_PROJECT_ROOT.")
    for stale in [sid for sid, s in SESSIONS.items() if not s.running]:
        SESSIONS.pop(stale)
    if len(SESSIONS) >= MAX_SESSIONS:
        raise SessionError(
            f"{len(SESSIONS)} games are already running (GODOT_RUNTIME_MAX_SESSIONS="
            f"{MAX_SESSIONS}). Stop one with runtime_stop first.", "too_many_sessions")

    if not await asyncio.to_thread(project_is_imported, project_root):
        # A game reads imported assets and the class cache the editor's import writes.
        await import_project(binary, project_root)

    helper = bool(arguments.get("helper", True))
    note = None
    if helper and await _main_loop_type(project_root) != "SceneTree":
        # The helper runs as the main loop; a project with its own would not be itself.
        helper = False
        note = "This project sets its own main loop type, so it runs without the helper."
    scene = arguments.get("scene")
    session = RuntimeSession(
        f"g{next(_ids)}", binary, project_root,
        scene=to_res_path(scene, project_root) if scene else None,
        windowed=bool(arguments.get("windowed", False)),
        resolution=arguments.get("resolution"),
        args=[str(a) for a in arguments.get("args") or []],
        helper=helper)
    await session.start(timeout=float(arguments.get("timeout", 30.0)))
    SESSIONS[session.session_id] = session
    if helper:
        # The helper announces itself once its message capture is registered.
        try:
            await asyncio.wait_for(session.helper_ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            note = "The helper did not start; runtime_eval and runtime_input are unavailable."

    await asyncio.sleep(max(0.0, min(float(arguments.get("collect", 2.0)), 60.0)))
    result = {**session.describe(), **_output(session)}
    result["note"] = ("Keep reading with runtime_output; look with runtime_tree and "
                      "runtime_inspect; stop with runtime_stop.")
    if note:
        result["helper_note"] = note
    return result


async def _main_loop_type(project_root: str) -> str:
    if project_root not in _main_loop_types:
        config = await dump_project(project_root)
        _main_loop_types[project_root] = (config.get("application") or {}).get(
            "main_loop_type") or "SceneTree"
    return _main_loop_types[project_root]


def _session(arguments: dict) -> RuntimeSession:
    wanted = arguments.get("session_id")
    if wanted:
        if wanted not in SESSIONS:
            raise SessionError(f"No session {wanted!r}.", "unknown_session",
                               f"Known: {sorted(SESSIONS) or 'none'}")
        return SESSIONS[wanted]
    if not SESSIONS:
        raise SessionError("No game is running. Start one with runtime_run.", "no_session")
    if len(SESSIONS) > 1:
        raise SessionError("Several games are running; pass session_id.", "session_required",
                           f"Running: {sorted(SESSIONS)}")
    return next(iter(SESSIONS.values()))


def _require_live(session: RuntimeSession) -> None:
    if not session.connected or not session.running:
        raise SessionError(
            f"Session {session.session_id} has ended (exit code {session.process.returncode}).",
            "game_exited", "runtime_output still returns what it printed before exiting.")


def _output(session: RuntimeSession) -> dict:
    result = {
        "session_id": session.session_id,
        "running": session.running,
        "lines": session.take_output(),
        "errors": session.take_errors(),
    }
    if session.notes:
        result["notes"] = list(session.notes)
        session.notes.clear()
    if not session.running:
        result["exit_code"] = session.process.returncode
        result["console_tail"] = list(session.console)[-20:]
    return result


# ---------------------------------------------------------------------------
# Looking
# ---------------------------------------------------------------------------


async def _flat_tree(session: RuntimeSession) -> list[dict]:
    """Every node, depth first, with its absolute path."""
    _, data = await session.request("scene:request_scene_tree", [], {"scene:scene_tree"})
    nodes: list[dict] = []
    stack: list[tuple[str, int]] = []  # (parent path, children still to come)
    for i in range(0, len(data) - 5, 6):
        child_count, name, type_name, object_id, scene_file, flags = data[i:i + 6]
        while stack and stack[-1][1] == 0:
            stack.pop()
        parent = stack[-1][0] if stack else ""
        if stack:
            stack[-1] = (stack[-1][0], stack[-1][1] - 1)
        path = f"{parent}/{name}"
        node = {"name": str(name), "type": str(type_name), "path": path,
                "object_id": int(object_id), "depth": len(stack),
                "child_count": int(child_count)}
        if scene_file:
            node["scene"] = str(scene_file)
        if flags & _HAS_VISIBLE:
            node["visible"] = bool(flags & _VISIBLE)
            if not flags & _VISIBLE_IN_TREE:
                node["visible_in_tree"] = False
        nodes.append(node)
        stack.append((path, int(child_count)))
    return nodes


async def _tree(session: RuntimeSession, arguments: dict) -> dict:
    flat = await _flat_tree(session)
    start = _find(flat, arguments["path"]) if arguments.get("path") else flat[0]
    max_depth = int(arguments.get("max_depth", 8))
    by_path: dict[str, dict] = {}
    root: dict | None = None
    hidden = 0
    for node in flat:
        if node["path"] != start["path"] and not node["path"].startswith(start["path"] + "/"):
            continue
        relative_depth = node["depth"] - start["depth"]
        if relative_depth > max_depth:
            hidden += 1
            continue
        entry = {k: v for k, v in node.items() if k not in {"depth", "child_count"}}
        if node["child_count"]:
            entry["children"] = []
        by_path[node["path"]] = entry
        parent = node["path"].rsplit("/", 1)[0]
        if parent in by_path:
            by_path[parent]["children"].append(entry)
        elif root is None:
            root = entry
    result = {"session_id": session.session_id, "tree": root, "node_count": len(by_path)}
    if hidden:
        result["deeper_nodes_not_shown"] = hidden
    return result


def _find(flat: list[dict], wanted: str) -> dict:
    """Resolve a node path the way an agent is likely to write it."""
    text = wanted.rstrip("/")
    for candidate in (text, f"/root/{text.lstrip('/')}"):
        for node in flat:
            if node["path"] == candidate:
                return node
    tail = [node for node in flat if node["path"].endswith("/" + text.lstrip("/"))]
    if len(tail) == 1:
        return tail[0]
    if tail:
        raise SessionError(f"{wanted!r} matches {len(tail)} nodes; give more of the path.",
                           "ambiguous_node", ", ".join(n["path"] for n in tail[:10]))
    raise SessionError(f"No node at {wanted!r} in the running game.", "unknown_node",
                       "runtime_tree lists what exists right now.")


async def _object_id(session: RuntimeSession, arguments: dict) -> tuple[int, str | None]:
    if arguments.get("object_id") is not None:
        return int(arguments["object_id"]), None
    if not arguments.get("node"):
        raise SessionError("Pass node (a path) or object_id.", "missing_target")
    node = _find(await _flat_tree(session), arguments["node"])
    return node["object_id"], node["path"]


async def _inspect_raw(session: RuntimeSession, object_id: int
                       ) -> tuple[str, dict[str, tuple[int, Any]], dict[str, Any]]:
    """(class, properties, script constants) as the engine reports them.

    Godot lists a script's own variables as "Members/<script path><name>" and its
    constants as "Constants/<name>" (SceneDebuggerObject). Variables are given back
    under their plain names, which is also what setting them takes.
    """
    name, data = await session.request("scene:inspect_objects", [[object_id], False], INSPECT_REPLIES)
    if name != "scene:inspect_objects" or not data:
        raise SessionError(f"Object {object_id} no longer exists in the game.", "unknown_object")
    _, class_name, rows = data[0]
    properties: dict[str, tuple[int, Any]] = {}
    constants: dict[str, Any] = {}
    for row in rows:
        prop_name, prop_type, _hint, _hint_string, usage, value = row[:6]
        if usage & _LAYOUT_ROWS:
            continue
        prop_name = str(prop_name)
        if prop_name.startswith("Constants/"):
            constants[prop_name.rsplit("/", 1)[-1]] = value
        elif prop_name.startswith("Members/"):
            properties[prop_name.rsplit("/", 1)[-1]] = (int(prop_type), value)
        else:
            properties[prop_name] = (int(prop_type), value)
    return str(class_name), properties, constants


async def _inspect(session: RuntimeSession, arguments: dict) -> dict:
    object_id, path = await _object_id(session, arguments)
    class_name, properties, constants = await _inspect_raw(session, object_id)
    wanted = arguments.get("properties")
    if wanted:
        missing = [p for p in wanted if p not in properties and p not in constants]
        properties = {k: v for k, v in properties.items() if k in wanted}
        constants = {k: v for k, v in constants.items() if k in wanted}
    shown = {name: {"type": variant.TYPE_NAMES[t] if t < len(variant.TYPE_NAMES) else str(t),
                    "value": variant.to_json(value)}
             for name, (t, value) in properties.items()}
    result = {"session_id": session.session_id, "object_id": object_id, "class": class_name,
              "properties": shown}
    if constants:
        result["constants"] = {name: variant.to_json(value) for name, value in constants.items()}
    if path:
        result["path"] = path
    if wanted and missing:
        result["unknown_properties"] = missing
    return result


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------


async def _set(session: RuntimeSession, arguments: dict) -> dict:
    object_id, path = await _object_id(session, arguments)
    prop = str(arguments["property"])
    _, properties, _ = await _inspect_raw(session, object_id)
    if prop not in properties:
        close = [p for p in properties if prop.lower() in p.lower()][:10]
        raise SessionError(f"The object has no property {prop!r}.", "unknown_property",
                           f"Similar: {close}" if close else None)
    type_id = properties[prop][0]
    type_name = variant.TYPE_NAMES[type_id]
    if type_name == "Object" and isinstance(arguments["value"], str):
        value = arguments["value"]  # the game loads a string as a resource path
    else:
        try:
            value = variant.from_json(arguments["value"], type_name)
        except (ValueError, TypeError) as exc:
            raise SessionError(f"Cannot use {arguments['value']!r} as a {type_name}: {exc}",
                               "bad_value") from None
    await session.put("scene:set_object_property", [object_id, prop, value])
    _, after, _ = await _inspect_raw(session, object_id)
    result = {"session_id": session.session_id, "object_id": object_id, "property": prop,
              "type": type_name, "value": variant.to_json(after.get(prop, (0, None))[1])}
    if path:
        result["path"] = path
    return result


def _require_helper(session: RuntimeSession) -> None:
    if session.helper_version is None:
        raise SessionError(
            "This game is not running through the GodotLens helper.", "helper_missing",
            "Start it with runtime_run (helper defaults to true). Projects that set their "
            "own main loop type cannot use the helper.")


async def _lens(session: RuntimeSession, name: str, args: list, timeout: float) -> Any:
    _, data = await session.request(f"lens:{name}", [next(_ids), *args], {"lens:result"},
                                    timeout=timeout)
    _, ok, value = data[:3]
    if not ok:
        raise SessionError(str(value), "game_refused")
    return value


async def _eval(session: RuntimeSession, arguments: dict) -> dict:
    _require_helper(session)
    expression = str(arguments.get("expression") or "")
    if not expression:
        raise SessionError("Pass an expression.", "missing_expression")
    base = ""
    if arguments.get("node"):
        base = _find(await _flat_tree(session), arguments["node"])["path"]
    value = await _lens(session, "eval", [expression, base], timeout=10)
    return {"session_id": session.session_id, "expression": expression,
            "value": variant.to_json(value), "text": variant.to_text(value)}


async def _input(session: RuntimeSession, arguments: dict) -> dict:
    _require_helper(session)
    action = str(arguments.get("action") or "")
    frames = max(1, int(arguments.get("frames", 2)))
    strength = float(arguments.get("strength", 1.0))
    await _lens(session, "action", [action, frames, strength], timeout=10 + frames / 10)
    return {"session_id": session.session_id, "action": action, "frames": frames,
            "released": True}


async def _reload(session: RuntimeSession, arguments: dict, project_root: str,
                  check_scripts: ScriptCheck) -> dict:
    files = [str(f) for f in arguments.get("files") or []]
    if not files:
        raise SessionError("Pass the scripts to reload.", "missing_files")
    problems = await check_scripts(files)
    if problems:
        return {"session_id": session.session_id, "reloaded": False, "errors": problems,
                "reason": ("These scripts do not compile. Reloading would disable them in "
                           "the running game; fix the errors first.")}
    res_paths = await asyncio.to_thread(
        lambda: [to_res_path(f if f.startswith("res://") else os.path.abspath(f), project_root)
                 for f in files])
    session.take_errors(clear=True)
    await session.put("reload_scripts", res_paths)
    await asyncio.sleep(0.5)  # the game reloads on its next frame
    result = {"session_id": session.session_id, "reloaded": True, "scripts": res_paths,
              "errors": session.take_errors()}
    if problems is None:
        result["warning"] = ("Not checked: the language server is not connected. A script "
                             "with errors is disabled in the running game.")
    return result


async def _step(session: RuntimeSession) -> dict:
    if not session.windowed:
        raise SessionError(
            "Stepping needs a windowed session.", "headless_session",
            "Godot re-pauses after the next DRAWN frame, and a headless game draws none, "
            "so it would simply resume. Start with runtime_run windowed=true.")
    if not session.paused:
        raise SessionError("Pause the game first with runtime_pause.", "not_paused")
    await session.put("scene:next_frame", [])
    return {"session_id": session.session_id, "stepped": True}


async def _monitors(session: RuntimeSession, arguments: dict, project_root: str) -> dict:
    if not session.monitors:
        await asyncio.sleep(1.2)  # the game reports about once a second
    if not session.monitors:
        return {"session_id": session.session_id, "latest": {},
                "note": "No performance frame has arrived yet."}
    names = await _monitor_name_table(session.binary, project_root)
    def label(index: int) -> str:
        if index in names:
            return names[index]
        custom = index - len(names)
        if 0 <= custom < len(session.custom_monitor_names):
            return session.custom_monitor_names[custom]
        return f"monitor_{index}"
    _, latest = session.monitors[-1]
    result = {"session_id": session.session_id,
              "latest": {label(i): variant.to_json(v) for i, v in enumerate(latest)}}
    wanted = arguments.get("names")
    if wanted:
        index_of = {label(i): i for i in range(len(latest))}
        result["history"] = {
            name: [variant.to_json(frame[index_of[name]]) for _, frame in session.monitors
                   if index_of.get(name) is not None and index_of[name] < len(frame)]
            for name in wanted if name in index_of}
        result["seconds_per_sample"] = 1
    return result


async def _monitor_name_table(binary: str, project_root: str) -> dict[int, str]:
    """Performance.Monitor names from this build, asked of the engine once."""
    if binary not in _monitor_names:
        stdout, _ = await run_engine(
            [binary, "--headless", "--path", project_root, "--script", str(MONITOR_NAMES_SCRIPT)],
            "monitor_names", timeout=60)
        table: dict[int, str] = {}
        for line in stdout.decode("utf-8", "replace").splitlines():
            if line.startswith('{"monitors"'):
                table = {int(k): v for k, v in json.loads(line)["monitors"].items()}
        _monitor_names[binary] = table
    return _monitor_names[binary]


async def _screenshot(session: RuntimeSession) -> tuple[dict, list[tuple[str, str]]]:
    if not session.windowed:
        raise SessionError(
            "Screenshots need a windowed session.", "headless_session",
            "A headless game uses the dummy renderer and has no pixels. Start one with "
            "runtime_run windowed=true; it opens off-screen and takes no focus.")
    _require_live(session)
    request_id = next(_ids)
    _, data = await session.request("scene:rq_screenshot", [request_id],
                                    {"game_view:get_screenshot"}, timeout=15)
    _, width, height, path = data[:4]
    image = Path(str(path))  # written by the game into its OS temp directory
    try:
        encoded = base64.b64encode(await asyncio.to_thread(image.read_bytes)).decode("ascii")
    finally:
        await asyncio.to_thread(image.unlink, missing_ok=True)
    return ({"session_id": session.session_id, "width": int(width), "height": int(height)},
            [(encoded, "image/png")])


async def stop_all() -> None:
    for session in list(SESSIONS.values()):
        await session.stop()
    SESSIONS.clear()
