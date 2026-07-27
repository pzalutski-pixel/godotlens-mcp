"""Scene inspection, delegated to Godot itself.

Godot's language server never opens .tscn — ``find_all_usages`` collects ``.gd``
files only — so signal connections declared in a scene are invisible to references
and rename. An agent that edits a scene has nothing to check its work against.

Rather than parse ``.tscn`` here, a helper GDScript is run through
``godot --headless --script`` and reports ``PackedScene.get_state()``: the node
tree, script attachments and connection list, with inherited-scene resolution
already applied by the engine. Writing a second interpretation of the format would
reintroduce exactly the class of silent disagreement this project exists to avoid.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

HELPER_SCRIPT = Path(__file__).with_name("scene_dump.gd")
PROJECT_HELPER_SCRIPT = Path(__file__).with_name("project_dump.gd")


class GodotBinaryNotFound(RuntimeError):
    """No Godot executable is available to run the helper."""


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

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
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

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
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


def validate_scene(scene: dict, script_symbols: dict[str, set[str]]) -> dict:
    """Cross-check a scene's connections against the methods that actually exist.

    ``script_symbols`` maps a script path (res:// form) to the set of function names
    the LSP reported for it. Connections are matched by an unvalidated string, so a
    handler that was renamed or removed produces no error until the signal fires.
    """
    problems: list[dict] = []
    nodes_by_path = {normalize_node_path(node.get("path", "")): node
                     for node in scene.get("nodes", [])}

    for connection in scene.get("connections", []):
        target_path = connection.get("to", "")
        method = connection.get("method", "")
        target = nodes_by_path.get(normalize_node_path(target_path))

        if target is None:
            problems.append({
                "kind": "missing_target_node",
                "signal": connection.get("signal"),
                "to": target_path,
                "method": method,
                "detail": f"Connection targets node '{target_path}', which is not in this scene.",
            })
            continue

        script_path = target.get("script")
        if not script_path:
            problems.append({
                "kind": "target_has_no_script",
                "signal": connection.get("signal"),
                "to": target_path,
                "method": method,
                "detail": (f"Node '{target_path}' has no script, so '{method}' cannot exist. "
                           "This connection fails at runtime."),
            })
            continue

        known = script_symbols.get(script_path)
        if known is None:
            continue  # symbols unavailable; do not guess
        if method not in known:
            problems.append({
                "kind": "missing_handler",
                "signal": connection.get("signal"),
                "to": target_path,
                "method": method,
                "script": script_path,
                "detail": (f"'{script_path}' has no method '{method}'. The signal is wired "
                           "in the scene by name, so this fails at runtime with no compile "
                           "error."),
            })

    return {
        "valid": not problems,
        "problems": problems,
        "connections_checked": len(scene.get("connections", [])),
    }


def collect_scripts(scene: dict) -> list[str]:
    """Every script attached to a node in this scene, in res:// form."""
    seen: list[str] = []
    for node in scene.get("nodes", []):
        script = node.get("script")
        if script and script not in seen:
            seen.append(script)
    return seen
