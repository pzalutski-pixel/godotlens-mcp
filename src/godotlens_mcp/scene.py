"""Scene inspection, delegated to Godot itself.

Godot's language server never opens .tscn — ``find_all_usages`` collects ``.gd``
files only — so signal connections declared in a scene are invisible to references
and rename. An agent that edits a scene has nothing to check its work against.

Rather than parse ``.tscn`` here, a helper GDScript is run through
``godot --headless --script`` and reports ``PackedScene.get_state()``: the node
tree, script attachments and connection list. A scene file stores only overrides for
nodes that come from an inherited base or an instanced child, so the helper also
merges those scenes' own states into ``resolved_nodes``, using the engine's
``SceneState`` API rather than reading any file itself. Writing a second
interpretation of the format would reintroduce exactly the class of silent
disagreement this project exists to avoid.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import tempfile
from pathlib import Path

HELPER_SCRIPT = Path(__file__).with_name("scene_dump.gd")
PROJECT_HELPER_SCRIPT = Path(__file__).with_name("project_dump.gd")
VALIDATE_HELPER_SCRIPT = Path(__file__).with_name("project_validate.gd")


class GodotBinaryNotFound(RuntimeError):
    """No Godot executable is available to run the helper."""


_LOG_DIR = Path(tempfile.gettempdir()) / "godotlens-logs"
_log_ids = itertools.count(1)


def private_log(label: str) -> Path:
    """A log file of its own for one engine run GodotLens starts.

    Without --log-file every run writes into the project's user://logs, where Godot keeps
    only the newest few (debug/file_logging/max_log_files, default 5): runs GodotLens
    starts would push out the developer's own play and test logs. Given --log-file, Godot
    turns rotation off and writes just that file (Main::setup).
    """
    _LOG_DIR.mkdir(exist_ok=True)
    return _LOG_DIR / f"{label}-{os.getpid()}-{next(_log_ids)}.log"


async def run_engine(command: list[str], label: str, timeout: float) -> tuple[bytes, bytes]:
    """Run the engine with a private log, kill it on timeout, and remove the log after."""
    log = private_log(label)
    process = await asyncio.create_subprocess_exec(
        command[0], "--log-file", str(log), *command[1:],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    finally:
        await asyncio.to_thread(log.unlink, missing_ok=True)


def find_godot_binary() -> str | None:
    """Locate a Godot executable: GODOT_BIN, a ./godot/ directory, then PATH."""
    env_bin = os.environ.get("GODOT_BIN")
    if env_bin and os.path.isfile(env_bin):
        return env_bin

    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        local = base / "godot"
        if local.is_dir():
            # Prefer the console build on Windows: it keeps stdout usable.
            for candidate in sorted(local.glob("*console*")) + sorted(local.iterdir()):
                if candidate.is_file() and candidate.suffix in (".exe", ""):
                    return str(candidate)

    for name in ("godot", "godot4", "Godot"):
        found = shutil.which(name)
        if found:
            return found
    return None


def to_res_path(path: str, project_root: str) -> str:
    """Convert a filesystem path to the res:// form the engine expects."""
    if path.startswith("res://"):
        return path
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, project_root)
    except ValueError:  # different drive on Windows
        return path
    if relative.startswith(".."):
        return path
    return "res://" + relative.replace(os.sep, "/")


async def dump_scenes(paths: list[str], project_root: str, timeout: float = 90.0) -> dict:
    """Ask Godot for its resolved view of each scene.

    Returns ``{"scenes": {...}, "errors": {...}}`` exactly as the helper produced it.
    """
    binary = find_godot_binary()
    if not binary:
        raise GodotBinaryNotFound(
            "No Godot executable found. Set GODOT_BIN to the editor binary, or place "
            "one in a ./godot/ directory. Scene inspection runs the engine itself so "
            "that inherited scenes and script attachments resolve the way Godot does."
        )

    res_paths = [to_res_path(p, project_root) for p in paths]
    command = [
        binary, "--headless", "--path", project_root,
        "--script", str(HELPER_SCRIPT), "--", *res_paths,
    ]

    try:
        stdout, stderr = await run_engine(command, "scene_dump", timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Godot did not finish inspecting the scene within {timeout}s. A cold "
            "project import can be slow; retry once the editor has imported it."
        ) from None

    text = stdout.decode("utf-8", "replace")
    payload = _extract_json(text)
    if payload is None:
        detail = (stderr.decode("utf-8", "replace") or text).strip()[-1500:]
        raise RuntimeError(f"Could not read Godot's scene dump. Output:\n{detail}")
    return payload


async def dump_project(project_root: str, timeout: float = 90.0) -> dict:
    """Ask Godot for its resolved project configuration.

    Autoload and input-action names are bare strings at the point of use and nothing
    validates them, so a typo is a silent runtime no-op. ProjectSettings is queried
    rather than project.godot being read, so defaults and feature-tagged overrides
    resolve the way the engine resolves them.
    """
    payload = await _run_helper(PROJECT_HELPER_SCRIPT, [], project_root, timeout,
                                marker="autoloads")
    return payload


async def validate_project(project_root: str, timeout: float = 180.0) -> dict:
    """Ask Godot which references in the project's scenes and resources are broken.

    Uses the engine's dependency lists and its own resolution rule, so a reference
    whose UID still resolves is not reported just because its text path is stale.
    """
    payload = await _run_helper(VALIDATE_HELPER_SCRIPT, [], project_root, timeout,
                                marker="project_validate")
    return payload["project_validate"]


async def _run_helper(script: Path, args: list[str], project_root: str,
                      timeout: float, marker: str) -> dict:
    binary = find_godot_binary()
    if not binary:
        raise GodotBinaryNotFound(
            "No Godot executable found. Set GODOT_BIN to the editor binary, or place "
            "one in a ./godot/ directory."
        )

    command = [binary, "--headless", "--path", project_root, "--script", str(script)]
    if args:
        command += ["--", *args]

    try:
        stdout, stderr = await run_engine(command, script.stem, timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Godot did not finish within {timeout}s. A cold project import can be slow."
        ) from None

    text = stdout.decode("utf-8", "replace")
    payload = _extract_json(text, marker)
    if payload is None:
        detail = (stderr.decode("utf-8", "replace") or text).strip()[-1500:]
        raise RuntimeError("Could not read Godot's output. Got:\n" + detail)
    return payload


def _extract_json(text: str, marker: str = "scenes") -> dict | None:
    """Pull the helper's JSON out of Godot's chatter.

    Godot prints engine banners and import progress to stdout alongside script
    output, so the payload cannot be assumed to be the whole stream.
    """
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and marker in parsed:
                return parsed
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_node_path(path: str) -> str:
    """Reduce a NodePath to a comparable form.

    SceneState.get_node_path() returns "./Player" while get_connection_target()
    returns "Player" for the same node, and the scene root is "." from one and ""
    from the other. Comparing them raw makes every connection look like it points at
    a node that does not exist.
    """
    text = (path or "").strip()
    if text in {"", "."}:
        return "."
    if text.startswith("./"):
        text = text[2:]
    return text.strip("/") or "."


# Problems that make a scene behave wrongly are errors; checks that cannot yet rule out
# false alarms on real projects report warnings, which do not make a scene invalid.
WARNING_KINDS = {"unknown_property", "wrong_value_type", "missing_nodepath_target"}


def validate_scene(scene: dict, script_symbols: dict[str, set[str]]) -> dict:
    """Check a scene for what Godot gets silently wrong when it loads or runs it.

    ``script_symbols`` maps a script path (res:// form) to the set of function names
    the LSP reported for it. Connections are matched by an unvalidated string, so a
    handler that was renamed or removed produces no error until the signal fires.

    Every fact comes from the engine (the dump helper): node classes, script base
    types, signals and their argument counts, handler argument ranges, and which
    stored properties a class actually has. Targets are looked up in the resolved
    view, where a node from a base or instanced scene carries the type and script
    that scene gives it.
    """
    problems: list[dict] = []
    nodes_by_path = {normalize_node_path(node.get("path", "")): node
                     for node in _scene_nodes(scene)}

    def report(kind: str, detail: str, **where) -> None:
        problems.append({"kind": kind, "severity": "warning" if kind in WARNING_KINDS else "error",
                         **where, "detail": detail})

    raw_nodes = scene.get("nodes", [])
    _check_structure(raw_nodes, nodes_by_path, report)
    for node in _scene_nodes(scene):
        path = normalize_node_path(node.get("path", ""))
        if node.get("script_compatible") is False:
            report("script_type_mismatch",
                   f"'{node['script']}' extends {node.get('script_base')}, which a "
                   f"{node['type']} is not. Godot refuses to attach it and the node runs "
                   "with no script.", node=path, script=node["script"])
        if node.get("origin") == "this_file" and not node.get("type"):
            report("missing_override_target",
                   f"This scene changes '{path}', but no such node exists in the scene it "
                   "comes from. Godot drops the change.", node=path)

    unverifiable = 0
    for node in raw_nodes:
        unverifiable += _check_node_paths(node, nodes_by_path, report)

    for connection in scene.get("connections", []):
        _check_connection(connection, nodes_by_path, script_symbols, report)

    result = {
        "valid": not any(p["severity"] == "error" for p in problems),
        "problems": problems,
        "connections_checked": len(scene.get("connections", [])),
    }
    if unverifiable:
        result["unverifiable_node_paths"] = unverifiable
    return result


def _check_structure(raw_nodes: list[dict], nodes_by_path: dict, report) -> None:
    """Names, parents, classes and stored properties of the nodes this file declares."""
    seen: dict[str, int] = {}
    unique: dict[str, list[str]] = {}
    for node in raw_nodes:
        path = normalize_node_path(node.get("path", ""))
        seen[path] = seen.get(path, 0) + 1
        if node.get("unique_name_in_owner"):
            unique.setdefault(node.get("name", ""), []).append(path)
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        if path != "." and parent not in nodes_by_path:
            report("missing_parent", f"'{path}' names a parent that is not in the scene; "
                   "Godot puts the node somewhere else.", node=path)
        if node.get("class_exists") is False:
            report("unknown_node_type", f"'{node.get('type')}' is not a class. Godot creates a "
                   "placeholder and the node's properties are lost.", node=path,
                   type=node.get("type"))
        for issue in node.get("property_issues", []):
            if issue["kind"] == "unknown_property":
                detail = (f"{node.get('type')} has no property '{issue['property']}'; Godot "
                          "ignores it.")
            else:
                detail = (f"'{issue['property']}' expects {issue['expected']} but the scene "
                          f"stores {issue['got']}; Godot ignores it.")
            report(issue["kind"], detail, node=path, property=issue["property"])
    for path, count in seen.items():
        if count > 1:
            report("duplicate_name", f"{count} nodes are named '{path}'. Godot keeps the "
                   "duplicate name and $-paths reach only the first.", node=path)
    for name, paths in unique.items():
        if len(paths) > 1:
            report("duplicate_unique_name", f"%{name} is marked unique on {len(paths)} nodes; "
                   "it resolves to only one of them.", node=name, nodes=paths)


def _check_node_paths(node: dict, nodes_by_path: dict, report) -> int:
    """NodePath properties that point inside this scene must reach a node. Returns how
    many point outside it, which cannot be checked without running the game."""
    unverifiable = 0
    base = normalize_node_path(node.get("path", ""))
    for prop, value in (node.get("properties") or {}).items():
        if not isinstance(value, dict) or value.get("__type") != "NodePath" or not value.get("__value"):
            continue
        target = _resolve_relative(base, value["__value"])
        if target is None:
            unverifiable += 1
        elif target not in nodes_by_path:
            report("missing_nodepath_target", f"'{prop}' points at '{value['__value']}', which "
                   "is not in this scene. It fails when used, unless that node is created at "
                   "runtime.", node=base, property=prop)
    return unverifiable


def _resolve_relative(base: str, node_path: str) -> str | None:
    """Where a NodePath set on ``base`` leads, or None when it leaves the scene."""
    text = node_path.split(":", 1)[0]
    if text.startswith("/"):
        return None
    parts = [] if base == "." else base.split("/")
    for segment in text.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts) or "."


def _accepted(args: dict) -> str:
    if args["vararg"]:
        return f"{args['required']} or more"
    if args["required"] == args["total"]:
        return str(args["total"])
    return f"{args['required']} to {args['total']}"


def _check_connection(connection: dict, nodes_by_path: dict, script_symbols: dict, report) -> None:
    signal = connection.get("signal")
    source_path = connection.get("from", "")
    target_path = connection.get("to", "")
    method = connection.get("method", "")
    facts = connection.get("facts") or {}
    where = {"signal": signal, "from": source_path, "to": target_path, "method": method}

    if normalize_node_path(source_path) not in nodes_by_path:
        report("missing_source_node", f"Connection comes from '{source_path}', which is not in "
               "this scene; Godot drops it.", **where)
    elif facts.get("signal_exists") is False:
        report("missing_signal", f"'{source_path}' has no signal '{signal}'; Godot drops the "
               "connection.", **where)

    target = nodes_by_path.get(normalize_node_path(target_path))
    if target is None:
        report("missing_target_node",
               f"Connection targets node '{target_path}', which is not in this scene.", **where)
        return
    if facts.get("method_native"):
        return  # an engine method on the node itself, such as queue_free
    script_path = target.get("script")
    if not script_path:
        report("target_has_no_script",
               f"Node '{target_path}' has no script and its class has no '{method}'. "
               "This connection fails at runtime.", **where)
        return
    known = script_symbols.get(script_path)
    if known is not None and method not in known:
        report("missing_handler",
               f"'{script_path}' has no method '{method}'. The signal is wired in the scene "
               "by name, so this fails at runtime with no compile error.",
               script=script_path, **where)
        return

    method_args = facts.get("method_args")
    signal_args = facts.get("signal_args", -1)
    if method_args and signal_args is not None and signal_args >= 0:
        binds = connection.get("binds") or []
        passed = max(signal_args - int(connection.get("unbinds") or 0), 0) + len(binds)
        if passed < method_args["required"] or (not method_args["vararg"]
                                                and passed > method_args["total"]):
            report("handler_arg_mismatch",
                   f"'{signal}' calls '{method}' with {passed} argument(s), but it accepts "
                   f"{_accepted(method_args)}. Godot reports an error every time the signal "
                   "fires.", script=script_path, **where)


def _scene_nodes(scene: dict) -> list[dict]:
    """The resolved node view when the helper produced one, else the per-file nodes."""
    resolved = scene.get("resolved_nodes")
    return resolved if resolved is not None else scene.get("nodes", [])


def collect_scripts(scene: dict) -> list[str]:
    """Every script attached to a node in this scene, in res:// form."""
    seen: list[str] = []
    for node in _scene_nodes(scene):
        script = node.get("script")
        if script and script not in seen:
            seen.append(script)
    return seen
