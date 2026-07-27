"""MCP server for GDScript semantic analysis via Godot's built-in LSP."""

import asyncio
import json
import os
import sys
from typing import Any

from godotlens_mcp import __version__
from godotlens_mcp.lsp_client import (
    LSPClient,
    LSPConnectionLost,
    LSPError,
    LSPTimeout,
    UnsupportedPathError,
    canonical_key,
    compact_location,
    compact_symbol,
    file_uri,
    find_project_root,
)
from godotlens_mcp.scene import (
    GodotBinaryNotFound,
    collect_scripts,
    dump_scenes,
    validate_scene,
)

_lsp: LSPClient | None = None

# MCP revisions this server implements, newest first. The client's requested version
# is honoured when we support it; otherwise we answer with our latest, per the spec.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# How long to wait for Godot to publish diagnostics after a sync. A cold project or a
# busy editor can take seconds; the previous fixed 0.3s sleep silently reported clean.
DIAGNOSTICS_TIMEOUT = float(os.environ.get("GODOT_DIAGNOSTICS_TIMEOUT", "8.0"))

SERVER_INSTRUCTIONS = (
    "GodotLens exposes Godot's own language server, so answers reflect how Godot "
    "compiles the project rather than a text search.\n"
    "- All line and character parameters are ZERO-BASED (editor line 1 = line 0).\n"
    "- Godot's LSP does not watch the filesystem. After editing a .gd file, call "
    "gdscript_sync_file or gdscript_sync_files before relying on any other result.\n"
    "- Godot must be running with the project open. Check gdscript_status first.\n"
    "- Godot's reference search reads .gd files only; it cannot see method names "
    "referenced from .tscn scene files, so renaming a signal handler may leave a "
    "scene connection pointing at the old name."
)


def log(message: str) -> None:
    """Write a diagnostic line to stderr.

    stdout carries the MCP stream exclusively; the stdio transport spec forbids
    writing anything else there, but explicitly permits stderr for logging.
    """
    print(f"[godotlens] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# MCP protocol handler (JSON-RPC 2.0 over stdio, newline-delimited)
# ---------------------------------------------------------------------------

class ParseFailure:
    """A stdin line that could not be turned into a JSON-RPC message."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


async def read_message() -> dict | ParseFailure | None:
    """Read one newline-delimited JSON-RPC message from stdin.

    Returns None only at true EOF. A blank line is skipped rather than treated as EOF —
    a single stray newline used to terminate the server and drop every later request.
    Malformed input yields a ParseFailure so the caller can reply with a JSON-RPC error
    instead of dying on an uncaught exception.
    """
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:
            return None  # genuine EOF: the pipe is closed
        stripped = line.strip()
        if not stripped:
            continue  # blank line between messages — keep reading

        try:
            payload = json.loads(stripped.decode("utf-8"))
        except UnicodeDecodeError:
            return ParseFailure(-32700, "Parse error: stdin was not valid UTF-8")
        except json.JSONDecodeError as exc:
            return ParseFailure(-32700, f"Parse error: {exc}")

        if not isinstance(payload, dict):
            # JSON-RPC batching was removed in MCP 2025-06-18, so an array is invalid.
            kind = "array (JSON-RPC batching is not supported)" if isinstance(payload, list) else type(payload).__name__
            return ParseFailure(-32600, f"Invalid Request: expected a JSON object, got {kind}")
        return payload


def write_message(data: dict) -> None:
    """Write a newline-delimited JSON-RPC message to stdout."""
    line = json.dumps(data) + "\n"
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def jsonrpc_response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def tool_result(content_text: str, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": content_text}],
        "isError": is_error,
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    # Health
    {
        "name": "gdscript_status",
        "description": (
            "Check connection status to the Godot LSP server. "
            "Returns: connection status, host, and port. "
            "Use this to verify Godot editor is running before using other tools. "
            "If disconnected, start Godot editor with your project open."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    # Navigation
    {
        "name": "gdscript_definition",
        "description": (
            "Navigate to the definition of a symbol at a given position. "
            "Returns: file path and line number where the symbol is defined. "
            "IMPORTANT: Uses ZERO-BASED coordinates (editor line 1 = pass line 0). "
            "Use when you need to find where a function, variable, or class is defined."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number (editor line - 1)"},
                "character": {"type": "integer", "description": "Zero-based character position"},
            },
            "required": ["file", "line", "character"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_references",
        "description": (
            "Find all references to a symbol across the entire project. "
            "Returns: list of locations (file, line, character) where the symbol is used. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "Essential for impact analysis before refactoring: 'What code uses this symbol?'"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number (editor line - 1)"},
                "character": {"type": "integer", "description": "Zero-based character position"},
            },
            "required": ["file", "line", "character"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_hover",
        "description": (
            "Get type information and documentation for a symbol at a given position. "
            "Returns: type signature, documentation string, or description of the symbol. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "Use to understand what type a variable is, or what a function returns."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number (editor line - 1)"},
                "character": {"type": "integer", "description": "Zero-based character position"},
            },
            "required": ["file", "line", "character"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_symbols",
        "description": (
            "List all symbols (classes, functions, variables, signals, enums) in a file. "
            "Returns: symbol tree with name, kind, and line number for each symbol. "
            "Use to understand the structure of a file before making changes. "
            "WORKFLOW: gdscript_symbols to explore, then gdscript_hover or gdscript_definition for details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
            },
            "required": ["file"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_signature_help",
        "description": (
            "Get function signature and parameter information at a call site. "
            "Returns: function name, parameters with types, and return type. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "Use when you need to know the correct parameters for a function call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number (editor line - 1)"},
                "character": {"type": "integer", "description": "Zero-based character position"},
            },
            "required": ["file", "line", "character"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    # Refactoring
    {
        "name": "gdscript_rename",
        "description": (
            "Rename a symbol across all files in the project. "
            "Returns: workspace edit with all changes needed. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "WORKFLOW: (1) gdscript_references to preview impact, "
            "(2) gdscript_rename to rename, (3) gdscript_sync_files to refresh LSP state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number (editor line - 1)"},
                "character": {"type": "integer", "description": "Zero-based character position"},
                "new_name": {"type": "string", "description": "New name for the symbol"},
                "old_name": {
                    "type": "string",
                    "description": ("Current name of the symbol. Optional but recommended: "
                                    "enables scanning .tscn/.tres files for references Godot "
                                    "cannot see or update."),
                },
            },
            "required": ["file", "line", "character", "new_name"],
        },
        "annotations": {
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    # Sync operations
    {
        "name": "gdscript_sync_file",
        "description": (
            "Notify Godot's LSP that a file was modified and get updated diagnostics. "
            "Returns: diagnostics (errors/warnings) for the synced file. "
            "WHEN TO CALL: After using Edit/Write tools to modify a .gd file. "
            "The LSP does not watch files, so you must call this to refresh analysis. "
            "Optionally pass content directly to avoid reading from disk."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the modified .gd file"},
                "content": {
                    "type": "string",
                    "description": "File content to sync (optional, reads from disk if not provided)",
                },
            },
            "required": ["file"],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "gdscript_sync_files",
        "description": (
            "Batch sync multiple modified files with Godot's LSP. "
            "Returns: diagnostics for all synced files. "
            "WHEN TO CALL: After modifying multiple .gd files with Edit/Write tools. "
            "More efficient than calling gdscript_sync_file repeatedly. "
            "Reads content from disk for all specified files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of absolute or relative paths to modified .gd files",
                },
            },
            "required": ["files"],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "gdscript_release_file",
        "description": (
            "Release a file from the LSP session, so Godot stops serving the copy this "
            "session opened and reads from disk again. "
            "Returns: whether the file was open, and what was actually done. "
            "WHEN TO CALL: after deleting a .gd file, or when you want the LSP to forget "
            "content you synced earlier. "
            "NOTE: Godot removed its file-deletion notification in 4.6, so this cannot "
            "purge project-wide state; that clears when the editor rescans."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
            },
            "required": ["file"],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    # Batch operations
    {
        "name": "gdscript_symbols_batch",
        "description": (
            "Get symbols from multiple files in a single call. "
            "Returns: map of file path to symbol tree for each file. "
            "More efficient than calling gdscript_symbols repeatedly. "
            "Use to understand the structure of multiple files at once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of absolute or relative paths to .gd files",
                },
            },
            "required": ["files"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_definitions_batch",
        "description": (
            "Get definitions for multiple symbol positions in a single call. "
            "Returns: list of definition locations for each position. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "More efficient than calling gdscript_definition repeatedly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Path to .gd file"},
                            "line": {"type": "integer", "description": "Zero-based line number"},
                            "character": {"type": "integer", "description": "Zero-based character position"},
                        },
                        "required": ["file", "line", "character"],
                    },
                    "description": "List of positions to look up definitions for",
                },
            },
            "required": ["positions"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_references_batch",
        "description": (
            "Find references for multiple symbols in a single call. "
            "Returns: list of reference locations for each position. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "More efficient than calling gdscript_references repeatedly. "
            "Use for bulk impact analysis across multiple symbols."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Path to .gd file"},
                            "line": {"type": "integer", "description": "Zero-based line number"},
                            "character": {"type": "integer", "description": "Zero-based character position"},
                        },
                        "required": ["file", "line", "character"],
                    },
                    "description": "List of positions to find references for",
                },
            },
            "required": ["positions"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_diagnostics",
        "description": (
            "Get compiler errors and warnings for one or more files. "
            "Returns: list of diagnostics with line, severity (1=Error, 2=Warning, 3=Info, 4=Hint), and message. "
            "WORKFLOW: (1) Edit files, (2) gdscript_sync_files to refresh, "
            "(3) gdscript_diagnostics to check for errors. "
            "Use before committing to catch issues early."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of absolute or relative paths to .gd files to check",
                },
            },
            "required": ["files"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    # Engine API and authoring
    {
        "name": "gdscript_engine_api",
        "description": (
            "Get authoritative documentation for a Godot ENGINE class or member, from the "
            "exact editor build in use. "
            "Returns: signature with argument names, types and defaults, plus documentation. "
            "USE THIS instead of recalling Godot's API from memory - it is the ground truth "
            "for the user's version and prevents inventing methods that do not exist. "
            "Pass 'member' for a specific method/property/signal; omit it to check a class exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "class_name": {"type": "string", "description": "Engine class, e.g. CharacterBody2D"},
                "member": {"type": "string", "description": "Method, property or signal name"},
            },
            "required": ["class_name"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_complete",
        "description": (
            "Get valid completions at a cursor position, from Godot's own completion engine. "
            "Returns: candidate labels with kind and detail. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "This is the only SCENE-AWARE query available: Godot resolves the scene that owns "
            "this script and completes against the real node, so $NodePath entries and the "
            "signals actually present on that node are included. No analysis of the .gd file "
            "alone can reproduce that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number"},
                "character": {"type": "integer", "description": "Zero-based character position"},
                "limit": {"type": "integer", "description": "Max items to return (default 100)"},
            },
            "required": ["file", "line", "character"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_validate",
        "description": (
            "Check proposed file content for errors WITHOUT writing it to disk. "
            "Returns: valid flag, errors and warnings. "
            "WHEN TO CALL: before writing an edit, so broken code never reaches the project. "
            "The LSP is restored to the on-disk content afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path the content is intended for"},
                "content": {"type": "string", "description": "Full proposed file content"},
            },
            "required": ["file", "content"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "gdscript_references_in_file",
        "description": (
            "Find occurrences of a symbol within ONE file. "
            "Returns: list of line/char positions. "
            "IMPORTANT: Uses ZERO-BASED coordinates. "
            "Much cheaper than gdscript_references, which reparses every .gd file in the "
            "project on Godot 4.6+. Requires Godot 4.7+; reports unsupported otherwise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the .gd file"},
                "line": {"type": "integer", "description": "Zero-based line number"},
                "character": {"type": "integer", "description": "Zero-based character position"},
            },
            "required": ["file", "line", "character"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    # Scene inspection (runs Godot itself; the LSP cannot see .tscn at all)
    {
        "name": "scene_state",
        "description": (
            "Get Godot's own resolved view of a scene: node tree with types, script "
            "attachments, unique_name_in_owner flags, exported property values, and the "
            "signal connections declared in the scene. "
            "Godot's LSP reads .gd files only, so NONE of this is visible to "
            "gdscript_references or gdscript_rename. "
            "Runs the engine to resolve the scene, so inherited scenes and instanced "
            "children are resolved the way Godot actually instantiates them. "
            "Requires a Godot binary (GODOT_BIN or ./godot/)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scene paths, res:// or filesystem",
                },
            },
            "required": ["files"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "scene_validate",
        "description": (
            "Check that a scene's signal connections still point at methods that exist. "
            "Returns: per-scene problems - missing handler methods, targets with no "
            "script, and connections aimed at nodes that are not in the scene. "
            "WHEN TO CALL: after editing a .tscn, or after renaming or removing a "
            "signal handler in GDScript. "
            "Connections are stored as unvalidated STRINGS, so a stale one produces no "
            "compile error and fails only when the signal fires at runtime. "
            "Handler existence is checked against the LSP's parse of the attached script."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scene paths, res:// or filesystem",
                },
            },
            "required": ["files"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

async def ensure_connected() -> tuple[bool, str]:
    """Ensure LSP is connected, return (ok, error_message)."""
    return await _lsp.connect()


def _res_to_disk(res_path: str, project_root: str) -> str:
    """res://scripts/player.gd -> <project>/scripts/player.gd"""
    if res_path.startswith("res://"):
        return os.path.join(project_root, res_path[len("res://"):].replace("/", os.sep))
    return res_path


def _symbol_names(symbols: Any) -> set[str]:
    """Flatten a documentSymbol tree to a set of names."""
    found: set[str] = set()

    def walk(nodes: Any) -> None:
        for node in nodes or []:
            if isinstance(node, dict):
                found.add(node.get("name", ""))
                walk(node.get("children"))

    walk(symbols)
    return found


def _native_name(entry: Any) -> str:
    """Class name from a gdscript/capabilities native_classes entry."""
    if isinstance(entry, dict):
        return entry.get("name") or entry.get("native_class") or ""
    return str(entry)


def _compact_completions(items: Any, limit: int) -> dict:
    """Strip completion items down to what an agent can act on.

    Godot echoes the full request context back in a per-item ``data`` blob — the
    document URI, the position, the trigger — on every single item. Left in, a
    few hundred completions bury the agent's context window in noise.
    """
    raw = items.get("items", []) if isinstance(items, dict) else (items or [])
    compact = []
    for item in raw[:limit]:
        entry = {"label": item.get("label", ""), "kind": item.get("kind", 0)}
        insert = item.get("insertText")
        if insert and insert != entry["label"]:
            entry["insert"] = insert
        detail = item.get("detail")
        if detail:
            entry["detail"] = detail
        compact.append(entry)
    return {"items": compact, "returned": len(compact), "total": len(raw),
            "truncated": len(raw) > len(compact)}


async def _ensure_document_open(uri: str, file_path: str) -> None:
    """Godot resolves completion/highlight against its cached document."""
    if not _lsp.is_open(uri):
        await _lsp.sync_document(uri, await read_text_file(file_path), notify_save=False)


async def _scan_scenes_for_symbol(symbol: str) -> list[dict]:
    """Find a name inside project scene/resource files.

    Godot's find_all_usages collects .gd files only, so anything named from a .tscn —
    most importantly [connection method="..."] — is invisible to references and rename.
    This is a plain text scan, deliberately reported separately from LSP results so the
    provenance stays obvious.
    """
    if not symbol:
        return []

    root = find_project_root()

    def _scan() -> list[dict]:
        hits: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".godot", ".git", "node_modules"}]
            for filename in filenames:
                if not filename.endswith((".tscn", ".tres")):
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    with open(full, encoding="utf-8", errors="replace") as handle:
                        for lineno, text in enumerate(handle):
                            if symbol in text:
                                hits.append({
                                    "file": full.replace("\\", "/"),
                                    "line": lineno,
                                    "text": text.strip()[:200],
                                })
                except OSError:
                    continue
        return hits

    return await asyncio.get_event_loop().run_in_executor(None, _scan)


async def _exists(path: str) -> bool:
    """Filesystem check off the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, os.path.isfile, path)


async def read_text_file(path: str) -> str:
    """Read a UTF-8 file without blocking the event loop.

    Reading inline stalls every other in-flight coroutine, which matters most on the
    batch tools where dozens of files are read in one call.
    """
    def _read() -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    return await asyncio.get_event_loop().run_in_executor(None, _read)


def _compact_diagnostics(diagnostics: list) -> list:
    """Reduce LSP diagnostics to line/severity/message."""
    return [{
        "line": d.get("range", {}).get("start", {}).get("line", 0),
        "severity": d.get("severity", 1),
        "message": d.get("message", ""),
    } for d in diagnostics]


def _compact_locations(result: Any) -> list:
    """Normalize an LSP location result (single or list) to compacted list."""
    items = result if isinstance(result, list) else [result]
    return [compact_location(r) if isinstance(r, dict) else r for r in items]


async def handle_tool_call(name: str, arguments: dict) -> dict:
    """Dispatch a tool call and return a tool_result dict."""

    # Status check doesn't require connection
    if name == "gdscript_status":
        ok, msg = await _lsp.connect()
        status = "connected" if ok else "disconnected"
        return tool_result(json.dumps({"status": status, "message": msg, "host": _lsp.host, "port": _lsp.port}))

    # All other tools require connection
    ok, msg = await ensure_connected()
    if not ok:
        return tool_result(json.dumps({"error": msg, "status": "disconnected"}), is_error=True)

    try:
        result = None

        # Single position operations
        if name == "gdscript_definition":
            result = await _lsp.request("textDocument/definition", {
                "textDocument": {"uri": file_uri(arguments["file"])},
                "position": {"line": arguments["line"], "character": arguments["character"]}
            })
            if result:
                result = _compact_locations(result)

        elif name == "gdscript_references":
            result = await _lsp.request("textDocument/references", {
                "textDocument": {"uri": file_uri(arguments["file"])},
                "position": {"line": arguments["line"], "character": arguments["character"]},
                "context": {"includeDeclaration": True}
            })
            if result:
                result = [compact_location(r) for r in result]

        elif name == "gdscript_hover":
            result = await _lsp.request("textDocument/hover", {
                "textDocument": {"uri": file_uri(arguments["file"])},
                "position": {"line": arguments["line"], "character": arguments["character"]}
            })
            if result and "contents" in result:
                contents = result["contents"]
                if isinstance(contents, dict):
                    result = contents.get("value", str(contents))
                else:
                    result = str(contents)

        elif name == "gdscript_symbols":
            result = await _lsp.request("textDocument/documentSymbol", {
                "textDocument": {"uri": file_uri(arguments["file"])}
            })
            if result:
                result = [compact_symbol(s) for s in result]

        elif name == "gdscript_signature_help":
            result = await _lsp.request("textDocument/signatureHelp", {
                "textDocument": {"uri": file_uri(arguments["file"])},
                "position": {"line": arguments["line"], "character": arguments["character"]}
            })

        elif name == "gdscript_rename":
            uri = file_uri(arguments["file"])
            position = {"line": arguments["line"], "character": arguments["character"]}

            # prepareRename first: GDScriptWorkspace::rename declares an empty
            # WorkspaceEdit and fills it only when is_valid_rename_target() passes, then
            # returns it unconditionally. A built-in, an unresolvable symbol, and a
            # symbol with no usages all come back as a well-formed {"changes": {}} —
            # indistinguishable from success. prepareRename returns null on refusal.
            can_rename = await _lsp.request("textDocument/prepareRename", {
                "textDocument": {"uri": uri}, "position": position})
            if can_rename is None:
                result = {
                    "renamed": False,
                    "reason": "not_renameable",
                    "detail": ("Godot will not rename this symbol. It is likely an engine "
                               "built-in, or the position does not resolve to a symbol "
                               "defined in project source."),
                }
            else:
                edit = await _lsp.request("textDocument/rename", {
                    "textDocument": {"uri": uri}, "position": position,
                    "newName": arguments["new_name"]})
                changes = (edit or {}).get("changes") or {}
                result = {
                    "renamed": bool(changes),
                    "changes": changes,
                    "files_affected": len(changes),
                }
                if not changes:
                    result["reason"] = "no_usages_found"
                # Godot's find_all_usages only walks .gd files, so a method named in a
                # .tscn [connection] block keeps the old name and breaks at runtime
                # with no compile error anywhere.
                scene_hits = await _scan_scenes_for_symbol(arguments.get("old_name") or "")
                if scene_hits:
                    result["scene_references"] = scene_hits
                    result["warning"] = (
                        "This name also appears in scene files, which Godot's rename "
                        "cannot update. Edit them yourself or the connection will break "
                        "at runtime with no compile error."
                    )

        elif name == "gdscript_engine_api":
            class_name = arguments["class_name"]
            member = arguments.get("member")
            if member:
                result = await _lsp.request("textDocument/nativeSymbol", {
                    "native_class": class_name, "symbol_name": member})
                if result is None:
                    result = {"found": False, "class_name": class_name, "member": member}
            else:
                # An empty symbol_name returns the class AND every child, which for
                # Node or Control is enormous. Serve the index from the capabilities
                # push instead, and make the caller name a member for detail.
                natives = _lsp.native_capabilities.get("native_classes") or []
                match = next((c for c in natives if _native_name(c) == class_name), None)
                result = {
                    "found": match is not None,
                    "class_name": class_name,
                    "detail": match,
                    "hint": "Pass 'member' for the full signature and documentation.",
                }

        elif name == "gdscript_complete":
            uri = file_uri(arguments["file"])
            await _ensure_document_open(uri, arguments["file"])
            items = await _lsp.request("textDocument/completion", {
                "textDocument": {"uri": uri},
                "position": {"line": arguments["line"], "character": arguments["character"]},
                # 4.4/4.5 load CompletionContext unguarded; omitting it silently
                # overwrites the struct default, so always send one.
                "context": {"triggerKind": 1, "triggerCharacter": ""},
            })
            limit = int(arguments.get("limit", 100))
            result = _compact_completions(items, limit)

        elif name == "gdscript_references_in_file":
            if not _lsp.capabilities.has_document_highlight:
                result = {
                    "supported": False,
                    "reason": "documentHighlight requires Godot 4.7 or newer.",
                    "alternative": "gdscript_references (project-wide, slower)",
                }
            else:
                uri = file_uri(arguments["file"])
                await _ensure_document_open(uri, arguments["file"])
                hits = await _lsp.request("textDocument/documentHighlight", {
                    "textDocument": {"uri": uri},
                    "position": {"line": arguments["line"], "character": arguments["character"]}})
                result = [{
                    "line": h.get("range", {}).get("start", {}).get("line", 0),
                    "char": h.get("range", {}).get("start", {}).get("character", 0),
                } for h in hits or []]

        elif name == "scene_state":
            root = find_project_root()
            dump = await dump_scenes(arguments["files"], root)
            result = dump

        elif name == "scene_validate":
            root = find_project_root()
            dump = await dump_scenes(arguments["files"], root)
            report = {}
            for res_path, scene in (dump.get("scenes") or {}).items():
                # Ask the LSP which functions each attached script really has, so the
                # check is against Godot's parse rather than our own reading.
                symbols: dict[str, set[str]] = {}
                for script_res in collect_scripts(scene):
                    disk_path = _res_to_disk(script_res, root)
                    if not await _exists(disk_path):
                        continue
                    try:
                        tree = await _lsp.request("textDocument/documentSymbol", {
                            "textDocument": {"uri": file_uri(disk_path)}})
                        symbols[script_res] = _symbol_names(tree)
                    except LSPError:
                        continue
                report[res_path] = validate_scene(scene, symbols)
            result = {"scenes": report, "errors": dump.get("errors") or {}}

        elif name == "gdscript_validate":
            # Check proposed content WITHOUT writing it to disk, so a broken edit is
            # caught before it lands in the project.
            file_path = arguments["file"]
            uri = file_uri(file_path)
            key = canonical_key(file_path)
            original = None
            if _lsp.is_open(uri):
                try:
                    original = await read_text_file(file_path)
                except OSError:
                    original = None

            _lsp.diagnostics_cache.pop(key, None)
            await _lsp.sync_document(uri, arguments["content"], notify_save=False)
            missing = await _lsp.wait_for_diagnostics([key], timeout=DIAGNOSTICS_TIMEOUT)
            diagnostics = _compact_diagnostics(_lsp.diagnostics_cache.get(key, []))

            # Put the LSP back on the real file so a rejected draft is not left behind.
            if original is not None:
                _lsp.diagnostics_cache.pop(key, None)
                await _lsp.sync_document(uri, original, notify_save=False)

            result = {
                "file": file_path,
                "valid": bool(not diagnostics and not missing),
                "verified": not missing,
                "errors": [d for d in diagnostics if d["severity"] == 1],
                "warnings": [d for d in diagnostics if d["severity"] == 2],
            }
            if missing:
                result["warning"] = "Godot did not report back; this is not a clean bill of health."

        # Sync operations
        elif name == "gdscript_sync_file":
            file_path = arguments["file"]
            content = arguments.get("content")
            if content is None:
                content = await read_text_file(file_path)
            uri = file_uri(file_path)
            action = await _lsp.sync_document(uri, content)

            key = canonical_key(file_path)
            # A re-sync republishes diagnostics, so drop the previous entry to avoid
            # reading a stale one and calling it verified.
            _lsp.diagnostics_cache.pop(key, None)
            missing = await _lsp.wait_for_diagnostics([key], timeout=DIAGNOSTICS_TIMEOUT)

            result = {
                "synced": file_path,
                "action": action,
                "diagnostics": _compact_diagnostics(_lsp.diagnostics_cache.get(key, [])),
                # Distinguishes "Godot checked it and it is clean" from "Godot never
                # reported back". Reporting the second as clean is how broken code
                # used to get a passing verdict.
                "verified": not missing,
            }
            if missing:
                result["warning"] = (
                    f"Godot did not publish diagnostics within {DIAGNOSTICS_TIMEOUT}s; "
                    "the file may still be parsing. Results are not a clean bill of health."
                )

        elif name == "gdscript_sync_files":
            synced_uris = []
            errors = []
            for file_path in arguments["files"]:
                try:
                    content = await read_text_file(file_path)
                    uri = file_uri(file_path)
                    _lsp.diagnostics_cache.pop(canonical_key(file_path), None)
                    await _lsp.sync_document(uri, content)
                    synced_uris.append((file_path, uri))
                except (OSError, UnsupportedPathError) as e:
                    errors.append({"file": file_path, "error": str(e)})

            keys = [canonical_key(fp) for fp, _ in synced_uris]
            missing = await _lsp.wait_for_diagnostics(keys, timeout=DIAGNOSTICS_TIMEOUT)

            results = {}
            for file_path, _uri in synced_uris:
                results[file_path] = _compact_diagnostics(
                    _lsp.diagnostics_cache.get(canonical_key(file_path), []))

            result = {
                "synced": len(synced_uris),
                "diagnostics": results,
                "verified": not missing,
            }
            if missing:
                result["unverified_files"] = [
                    fp for fp, _ in synced_uris if canonical_key(fp) in missing]
            if errors:
                result["errors"] = errors

        elif name == "gdscript_release_file":
            file_path = arguments["file"]
            uri = file_uri(file_path)
            was_open = await _lsp.close_document(uri)
            _lsp.diagnostics_cache.pop(canonical_key(file_path), None)
            result = {
                "released": file_path,
                "was_open": was_open,
                # Godot removed the entire workspace/* namespace in 4.6, and sending
                # didDeleteFiles as a NOTIFICATION means its METHOD_NOT_FOUND is
                # discarded - which is how the old tool reported success for a total
                # no-op. didClose is the only portable effect available.
                "note": ("Released the document from the LSP. Godot has no working "
                         "file-deletion notification on 4.6+, so stale project-wide "
                         "state clears when the editor rescans."),
            }

        # Batch operations
        elif name == "gdscript_symbols_batch":
            results = {}
            for file_path in arguments["files"]:
                try:
                    symbols = await _lsp.request("textDocument/documentSymbol", {
                        "textDocument": {"uri": file_uri(file_path)}
                    })
                    results[file_path] = [compact_symbol(s) for s in symbols] if symbols else []
                except Exception as e:
                    results[file_path] = {"error": str(e)}
            result = results

        elif name == "gdscript_definitions_batch":
            results = []
            for pos in arguments["positions"]:
                try:
                    defs = await _lsp.request("textDocument/definition", {
                        "textDocument": {"uri": file_uri(pos["file"])},
                        "position": {"line": pos["line"], "character": pos["character"]}
                    })
                    if defs:
                        defs = _compact_locations(defs)
                    results.append({"position": pos, "definitions": defs or []})
                except Exception as e:
                    results.append({"position": pos, "error": str(e)})
            result = results

        elif name == "gdscript_references_batch":
            results = []
            for pos in arguments["positions"]:
                try:
                    refs = await _lsp.request("textDocument/references", {
                        "textDocument": {"uri": file_uri(pos["file"])},
                        "position": {"line": pos["line"], "character": pos["character"]},
                        "context": {"includeDeclaration": True}
                    })
                    if refs:
                        refs = [compact_location(r) for r in refs]
                    results.append({"position": pos, "references": refs or []})
                except Exception as e:
                    results.append({"position": pos, "error": str(e)})
            result = results

        elif name == "gdscript_diagnostics":
            results = {}
            for file_path in arguments["files"]:
                try:
                    content = await read_text_file(file_path)
                    # No didSave here: this is a read-only check, and didSave triggers
                    # a real script hot-reload inside the editor.
                    await _lsp.sync_document(file_uri(file_path), content, notify_save=False)
                except (OSError, UnsupportedPathError) as e:
                    results[file_path] = {"error": str(e)}

            pending = [canonical_key(fp) for fp in arguments["files"] if fp not in results]
            missing = await _lsp.wait_for_diagnostics(pending, timeout=DIAGNOSTICS_TIMEOUT)

            for file_path in arguments["files"]:
                if file_path not in results:
                    results[file_path] = _compact_diagnostics(
                        _lsp.diagnostics_cache.get(canonical_key(file_path), []))
            result = {"diagnostics": results, "verified": not missing}
            if missing:
                result["unverified_files"] = [
                    fp for fp in arguments["files"] if canonical_key(fp) in missing]

        else:
            return tool_result(json.dumps({"error": f"Unknown tool: {name}"}), is_error=True)

        # Always serialize, even for empty results. Previously an empty list, empty
        # dict, 0 and False all rendered as the string "No results", which an agent
        # could not distinguish from a failed call — "zero references" and "your
        # coordinates were wrong" are very different facts.
        return tool_result(json.dumps(result if result is not None else None))

    except UnsupportedPathError as e:
        return tool_result(json.dumps({"error": str(e), "kind": "unsupported_path"}), is_error=True)

    except GodotBinaryNotFound as e:
        return tool_result(json.dumps({"error": str(e), "kind": "godot_binary_missing"}), is_error=True)

    except TimeoutError as e:
        return tool_result(json.dumps({"error": str(e), "kind": "godot_timeout"}), is_error=True)

    except (LSPConnectionLost, LSPTimeout) as e:
        # Genuine transport failure — drop the socket so the next call reconnects.
        await _lsp.disconnect()
        return tool_result(
            json.dumps({
                "error": str(e),
                "kind": "connection_lost",
                "hint": "Connection to Godot was lost. Call gdscript_status to reconnect.",
            }),
            is_error=True,
        )

    except LSPError as e:
        # The LSP answered with an error. The connection is fine — keep it. Tearing it
        # down here used to force a full re-initialize after any protocol-level error.
        return tool_result(
            json.dumps({
                "error": str(e),
                "kind": "unsupported_method" if e.is_method_not_found else "lsp_error",
                "code": e.code,
            }),
            is_error=True,
        )

    except OSError as e:
        # File read failures in the sync/diagnostics tools. Not a transport problem.
        return tool_result(json.dumps({"error": str(e), "kind": "file_error"}), is_error=True)

    except Exception as e:
        log(f"tool {name} failed: {e!r}")
        return tool_result(json.dumps({"error": str(e), "kind": "internal_error"}), is_error=True)


# ---------------------------------------------------------------------------
# MCP request router
# ---------------------------------------------------------------------------

async def handle_request(msg: dict) -> dict | None:
    """Route an incoming JSON-RPC message. Returns a response dict, or None for notifications."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params", {})

    # Notifications (no id) — no response needed
    if req_id is None:
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        # Spec: echo the requested version when supported, otherwise reply with ours.
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        if requested and requested != negotiated:
            log(f"client requested unsupported protocol {requested}; offering {negotiated}")
        return jsonrpc_response(req_id, {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "godotlens-mcp",
                "version": __version__,
            },
            "instructions": SERVER_INSTRUCTIONS,
        })

    elif method == "ping":
        return jsonrpc_response(req_id, {})

    elif method == "tools/list":
        return jsonrpc_response(req_id, {"tools": TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = await handle_tool_call(tool_name, arguments)
        return jsonrpc_response(req_id, result)

    else:
        return jsonrpc_error(req_id, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main():
    """Run the MCP server over stdio."""
    global _lsp

    host = os.environ.get("GODOT_LSP_HOST", "127.0.0.1")
    port = int(os.environ.get("GODOT_LSP_PORT", "6005"))
    _lsp = LSPClient(host=host, port=port)

    try:
        while True:
            msg = await read_message()
            if msg is None:
                break  # EOF — the client closed stdin
            if isinstance(msg, ParseFailure):
                write_message(jsonrpc_error(None, msg.code, msg.message))
                continue  # a bad line must not end the session
            try:
                response = await handle_request(msg)
            except Exception as exc:
                log(f"unhandled error in {msg.get('method', '?')}: {exc!r}")
                response = jsonrpc_error(msg.get("id"), -32603, f"Internal error: {exc}")
            if response is not None:
                write_message(response)
    finally:
        await _lsp.disconnect()
        _lsp = None


def main_sync():
    """Sync entry point for console_scripts and __main__."""
    asyncio.run(main())
