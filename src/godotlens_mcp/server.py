"""MCP server for GDScript semantic analysis via Godot's built-in LSP."""

import asyncio
import json
import os
import re
import sys
from typing import Any

from godotlens_mcp import __version__
from godotlens_mcp.dap_client import (
    DAPClient,
    DAPConnectionLost,
    DAPError,
    DAPTimeout,
)
from godotlens_mcp.lsp_client import (
    DEFAULT_TIMEOUT,
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
from godotlens_mcp.runtime import tools as runtime_tools
from godotlens_mcp.runtime.session import SessionError
from godotlens_mcp.scene import (
    GodotBinaryNotFound,
    collect_scripts,
    dump_project,
    dump_scenes,
    to_res_path,
    validate_project,
    validate_scene,
)

_lsp: LSPClient | None = None
_dap: DAPClient | None = None

# MCP revisions this server implements, newest first. The client's requested version
# is honoured when we support it; otherwise we answer with our latest, per the spec.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# How long to wait for Godot to publish diagnostics after a sync. A cold project or a
# busy editor can take seconds; the previous fixed 0.3s sleep silently reported clean.
DIAGNOSTICS_TIMEOUT = float(os.environ.get("GODOT_DIAGNOSTICS_TIMEOUT", "8.0"))

# Godot's debug adapter, served by the editor alongside the language server.
DAP_PORT = int(os.environ.get("GODOT_DAP_PORT", "6006"))
DAP_HOST = os.environ.get("GODOT_DAP_HOST", "127.0.0.1")

# gdscript_find issues one documentSymbol request per candidate file, serially. A
# common name can match hundreds of files, so cap the fan-out rather than block the
# server for minutes; callers are told when the cap applied.
FIND_FILE_LIMIT = int(os.environ.get("GODOT_FIND_FILE_LIMIT", "60"))

# How long to wait for the editor to register a class_name file it has never seen.
# With "Import Resources When Unfocused" on, the editor rescans every 0.5 s.
NEW_CLASS_WAIT = 3.0

# The analyzer's wording for a name it cannot resolve (gdscript_analyzer.cpp).
_UNDECLARED = re.compile(
    r'(?:Identifier|Could not find type|Could not find base class) "([A-Za-z_]\w*)"')
# Only decides which names to wait for; whether a class exists is Godot's answer.
_CLASS_NAME = re.compile(r"^\s*class_name\s+([A-Za-z_]\w*)", re.MULTILINE)

UNREGISTERED_CLASS_HINT = (
    "Godot has not registered these new class_name files yet, so every script using "
    "them reports the name as undeclared and the game will fail to parse. The editor "
    "registers new files only when its window gains focus, unless the editor setting "
    "'Import Resources When Unfocused' is on (interface/editor/behavior/"
    "import_resources_when_unfocused; on 4.6 interface/editor/"
    "import_resources_when_unfocused), which rescans every 0.5 s. Turn it on, or focus "
    "the editor once, then sync again."
)

SERVER_INSTRUCTIONS = (
    "GodotLens exposes Godot's own language server, so answers reflect how Godot "
    "compiles the project rather than a text search.\n"
    "- All line and character parameters are ZERO-BASED (editor line 1 = line 0).\n"
    "- Godot's LSP does not watch the filesystem. After editing a .gd file, call "
    "gdscript_sync_file or gdscript_sync_files before relying on any other result.\n"
    "- gdscript_* and debug_* need the Godot editor open on the project; check "
    "gdscript_status first. scene_*, project_* and runtime_* run the engine themselves.\n"
    "- Godot's reference search reads .gd files only; it cannot see method names "
    "referenced from .tscn scene files, so renaming a signal handler may leave a "
    "scene connection pointing at the old name.\n"
    "- To see what the game actually does, use runtime_run (headless; one game per "
    "agent, no editor needed), then runtime_tree, runtime_inspect, runtime_output, "
    "runtime_eval and runtime_input; runtime_reload applies script fixes while it runs. "
    "Use windowed=true only for runtime_screenshot. Stop games with runtime_stop.\n"
    "- Tool calls run concurrently, but two agents writing the same file overwrite each "
    "other: one writer at a time."
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


def tool_result(content_text: str, is_error: bool = False,
                images: list[tuple[str, str]] | None = None) -> dict:
    """A tools/call result: the JSON text first, then any images as (base64, mime type)."""
    content = [{"type": "text", "text": content_text}]
    content += [{"type": "image", "data": data, "mimeType": mime} for data, mime in images or []]
    return {
        "content": content,
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
            "Check a scene for what Godot gets silently wrong when it loads or runs it. "
            "Errors: connections to handlers or nodes that do not exist, signals the "
            "source does not have, handlers taking the wrong number of arguments, unknown "
            "node types, scripts whose base type does not fit the node, missing parents, "
            "duplicate names and duplicate unique names, and changes to nodes that do not "
            "exist in an instanced scene. Warnings: unknown properties, values of the "
            "wrong type, and NodePaths to nodes not in the scene. "
            "Returns: per-scene problems with kind, severity and detail; valid means no "
            "errors. "
            "WHEN TO CALL: after editing a .tscn, or after renaming or removing a signal "
            "handler in GDScript. Godot accepts all of these with no error or only a "
            "console warning. Facts come from the engine; handler existence from the LSP's "
            "parse of the attached script."
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
    # Runtime inspection via Godot's Debug Adapter Protocol (port 6006).
    # The language server can say whether code compiles; only the debugger can say
    # what it actually did.
    {
        "name": "debug_status",
        "description": (
            "Check the connection to Godot's debug adapter and report whether the game is "
            "running, paused, or finished. "
            "The adapter is served by the Godot editor on port 6006 and needs no addon. "
            "Use this first if any debug_* tool behaves unexpectedly."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "debug_output",
        "description": (
            "Read what a game started with debug_run produced: print() output and "
            "stdout/stderr lines, plus structured errors - push_error, push_warning, "
            "engine errors and runtime script errors - each with file, function, "
            "ZERO-BASED line and call stack. "
            "Returns: lines, errors, and their counts. "
            "THIS IS THE ONLY WAY to see what the game actually did; the language server "
            "reports whether code compiles, not what it printed. "
            "Output and errors are drained on each call, so successive calls return only "
            "what is new."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait": {"type": "number", "description": "Seconds to wait for output (default 1)"},
                "clear": {"type": "boolean", "description": "Drain the buffer (default true)"},
            },
            "required": [],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "debug_set_breakpoints",
        "description": (
            "Set breakpoints in a GDScript file, replacing any previously set in that file. "
            "Returns: each breakpoint with whether Godot verified it and the line it bound to. "
            "IMPORTANT: lines are ZERO-BASED, matching every other tool here. "
            "Set these before running the game, then use debug_stack_trace and debug_inspect "
            "once execution stops."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to the .gd file"},
                "lines": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Zero-based line numbers. Pass [] to clear all breakpoints in the file.",
                },
            },
            "required": ["file", "lines"],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "debug_stack_trace",
        "description": (
            "Get the call stack where execution is currently paused. "
            "Returns: frames with function name, file and ZERO-BASED line, plus why it stopped. "
            "An empty frame list means execution is not paused - frames exist only while "
            "stopped at a breakpoint or a runtime error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread id (default 1)"},
            },
            "required": [],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "debug_inspect",
        "description": (
            "Inspect variables visible in a stack frame. "
            "Returns: each scope (locals, members, globals) with its variables, values and types. "
            "Use the frame_id from debug_stack_trace. Only meaningful while execution is paused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "integer", "description": "Frame id from debug_stack_trace"},
            },
            "required": ["frame_id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "debug_evaluate",
        "description": (
            "Evaluate a GDScript expression in the context of a paused frame. "
            "Returns: the resulting value and its type. "
            "Use to check state at a breakpoint without adding print() calls and re-running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "GDScript expression"},
                "frame_id": {"type": "integer", "description": "Frame id from debug_stack_trace"},
            },
            "required": ["expression"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "debug_continue",
        "description": "Resume a paused game. Returns: confirmation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread id (default 1)"},
            },
            "required": [],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
    },
    {
        "name": "debug_pause",
        "description": (
            "Pause the running game. Returns: where it stopped. "
            "Use to inspect state at an arbitrary moment rather than a preset breakpoint."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread id (default 1)"},
            },
            "required": [],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
    },
    {
        "name": "debug_step_over",
        "description": "Step over one line in the paused game. Returns: the new stop location.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread id (default 1)"},
            },
            "required": [],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
    },
    {
        "name": "debug_terminate",
        "description": "Stop the running game. Returns: confirmation.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "gdscript_find",
        "description": (
            "Find where a symbol is declared BY NAME, without needing its position. "
            "Returns: declaration sites with file, ZERO-BASED line and character, kind, "
            "and containing class. "
            "USE THIS FIRST when you know a name but not its location - the returned "
            "line/character feed directly into gdscript_references, gdscript_hover and "
            "gdscript_rename. Guessing a character offset and landing one column off "
            "returns an empty result that looks identical to 'no such symbol'. "
            "Positions come from the language server, not from text matching."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact symbol name, e.g. take_damage"},
                "file": {
                    "type": "string",
                    "description": "Optional: restrict the search to one file instead of the project",
                },
                "include_references": {
                    "type": "boolean",
                    "description": "Also return every reference to the first declaration found",
                },
                "max_files": {
                    "type": "integer",
                    "description": ("Cap on files inspected when searching the whole project "
                                    "(default 60). The response reports when the cap applied."),
                },
            },
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "project_config",
        "description": (
            "Get the project's resolved configuration: autoload singletons, input action "
            "names, class_name globals, and the main scene. "
            "Autoload and input action names are BARE STRINGS at the point of use - "
            "GameState.add_score(1), Input.is_action_pressed(\"jump\") - and nothing "
            "validates them. Neither the compiler nor the language server catches a typo; "
            "it is a silent runtime no-op. Check names here before writing them. "
            "Values come from ProjectSettings via the engine, so defaults and "
            "feature-tagged overrides resolve correctly. Requires a Godot binary."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "project_validate",
        "description": (
            "Find references that no longer resolve, project-wide: scene and resource "
            "dependencies pointing at moved or deleted files, missing autoload scripts, and "
            "a missing main scene. "
            "Returns: broken references with the file that holds each, plus stale UIDs "
            "(Godot falls back to the text path and warns). "
            "WHEN TO CALL: after moving, renaming or deleting files. Nothing else reports "
            "these until the reference is loaded at runtime. Uses the engine's own "
            "resolution: a reference whose UID still resolves is not broken. Script "
            "preload()/extends paths are compile errors - gdscript_diagnostics reports "
            "them. Requires a Godot binary; runs no project code."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "debug_run",
        "description": (
            "Run the project from the open Godot editor and collect what it prints. "
            "Returns: captured output, structured errors with call stacks, whether the game "
            "exited, and any stop reason. "
            "THIS CLOSES THE LOOP: edit, sync, run, and read the actual behaviour, without "
            "the developer pressing F5 or pasting console output back. "
            "The editor has ONE run slot: a second debug_run stops the first game. "
            "Set breakpoints with debug_set_breakpoints BEFORE calling this if you want "
            "execution to pause. Long-running games keep going - use debug_output to keep "
            "reading and debug_terminate to stop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scene": {
                    "type": "string",
                    "description": ("Which scene to run: 'main' (default), 'current', "
                                    "'pinned', or a res:// path"),
                },
                "collect": {
                    "type": "number",
                    "description": "Seconds to collect output before returning (default 10)",
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait for the game to start (default 60)",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Extra engine flags appended to the game's command line, "
                                    "e.g. [\"--headless\"] to run with no window, "
                                    "[\"--fixed-fps\", \"60\"], [\"--debug-collisions\"]"),
                },
            },
            "required": [],
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
    },
    # Runtime: games GodotLens launches itself (runtime/tools.py).
    *runtime_tools.TOOLS,
]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

async def ensure_dap() -> tuple[bool, str]:
    """Connect to the debug adapter on demand.

    Kept separate from the LSP connection: they are different ports and different
    protocols, and a project with the language server working may still have the
    debug adapter disabled.
    """
    global _dap
    if _dap is None:
        _dap = DAPClient(host=DAP_HOST, port=DAP_PORT)
    return await _dap.connect()


async def ensure_connected() -> tuple[bool, str]:
    """Ensure LSP is connected, return (ok, error_message)."""
    return await _lsp.connect()


async def _list_project_scripts(root: str, limit: int = 4000) -> list[str]:
    """Every .gd file under the project, skipping Godot's own cache."""
    def _walk() -> list[str]:
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in {".godot", ".git", ".import", "node_modules"}]
            for filename in filenames:
                if filename.endswith(".gd"):
                    found.append(os.path.join(dirpath, filename))
                    if len(found) >= limit:
                        return found
        return found

    return await asyncio.get_event_loop().run_in_executor(None, _walk)


async def _files_mentioning(name: str, paths: list[str]) -> list[str]:
    """Narrow the candidate set with a cheap text scan.

    This only decides which files to ASK THE LSP about — it never decides what a
    symbol means. A file that does not contain the identifier as text cannot declare
    it, so this is a safe filter rather than an interpretation.
    """
    def _scan() -> list[str]:
        hits = []
        for path in paths:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    if name in handle.read():
                        hits.append(path)
            except OSError:
                continue
        return hits

    return await asyncio.get_event_loop().run_in_executor(None, _scan)


def _walk_symbols(symbols: Any, wanted: str, container: str = "") -> list[dict]:
    """Collect declarations matching ``wanted`` from a documentSymbol tree.

    selectionRange covers the identifier itself, so it yields the exact position the
    position-based tools need — no column counting, which is where callers go wrong.
    """
    found: list[dict] = []
    for node in symbols or []:
        if not isinstance(node, dict):
            continue
        node_name = node.get("name", "")
        if node_name == wanted:
            selection = node.get("selectionRange") or node.get("range") or {}
            start = selection.get("start", {})
            found.append({
                "line": start.get("line", 0),
                "char": start.get("character", 0),
                "kind": node.get("kind", 0),
                "detail": node.get("detail", ""),
                "container": container,
            })
        found.extend(_walk_symbols(node.get("children"), wanted,
                                   f"{container}.{node_name}" if container else node_name))
    return found


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


# Deeper than any engine class hierarchy; it only stops a malformed list looping.
MAX_INHERITANCE_DEPTH = 32


def _ancestors(class_name: str, natives: list) -> list[str]:
    """The engine inheritance chain above ``class_name``, nearest first.

    Taken from the ``inherits`` field of the class list Godot pushes on connect, so
    the chain is the exact build's, not a remembered one.
    """
    parents = {_native_name(c): c.get("inherits", "") for c in natives if isinstance(c, dict)}
    chain: list[str] = []
    current = parents.get(class_name, "")
    while (current and current != class_name and current not in chain
           and len(chain) < MAX_INHERITANCE_DEPTH):
        chain.append(current)
        current = parents.get(current, "")
    return chain


async def _evaluate_refusal(frame_id: int | None) -> tuple[dict | None, int | None]:
    """Decide whether debug_evaluate may be sent; returns (refusal, frame to use).

    Godot answers an evaluate only while stopped in its debug loop, and only against
    the script instance of the requested frame. Otherwise the request is never
    answered, or - worse - the game-side handler breaks out of the debug loop and the
    game RESUMES (``RemoteDebugger::debug``). That happens at a parse-error break and
    in a static function. A frame has an instance exactly when Godot lists ``self``
    among its members, which is the check used here.
    """
    hint = ("Set a breakpoint with debug_set_breakpoints and run, or evaluate in a game "
            "GodotLens launched with runtime_eval (requires the GodotLens helper).")
    if not _dap.stopped_state:
        return {"error": "Execution is not paused, so Godot will not evaluate anything.",
                "kind": "not_paused", "hint": hint}, None

    frames = await _dap.stack_trace(int(_dap.stopped_state.get("thread_id") or 1))
    if not frames:
        return {"error": "Execution is stopped outside script code; there is no frame to "
                         "evaluate in.", "kind": "not_paused", "hint": hint}, None
    ids = [frame["id"] for frame in frames]
    if frame_id is None:
        frame_id = ids[0]
    elif frame_id not in ids:
        return {"error": f"Frame {frame_id} is not on the current stack.",
                "kind": "unknown_frame", "frames": ids}, None

    has_self = False
    for scope in await _dap.scopes(frame_id):
        if scope.get("name") == "Members" and scope.get("variables_reference"):
            members = await _dap.variables(scope["variables_reference"])
            has_self = any(var.get("name") == "self" for var in members)
    if not has_self:
        return {
            "error": "This frame has no script instance to evaluate against.",
            "kind": "no_script_instance",
            "detail": ("It is a static function or a parse-error break. Godot would "
                       "resume the game instead of answering. Pick another frame from "
                       "debug_stack_trace, or break inside an instance method."),
            "frames": ids,
        }, None
    return None, frame_id


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


async def _abspath(path: str) -> str:
    """Resolve a path off the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, os.path.abspath, path)


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


def _undeclared_names(file_paths: list[str]) -> dict[str, list[str]]:
    """Names the analyzer could not resolve, mapped to the files reporting them."""
    found: dict[str, list[str]] = {}
    for file_path in file_paths:
        for diagnostic in _lsp.diagnostics_cache.get(canonical_key(file_path), []):
            match = _UNDECLARED.search(diagnostic.get("message", ""))
            if match and file_path not in found.setdefault(match.group(1), []):
                found[match.group(1)].append(file_path)
    return found


async def _project_class_declarations(names: set[str]) -> dict[str, str]:
    """Which of ``names`` a project script declares with class_name -> its res:// path."""
    root = find_project_root()
    scripts = await _list_project_scripts(root)
    declared: dict[str, str] = {}
    for name in names:
        for path in await _files_mentioning(name, scripts):
            try:
                text = await read_text_file(path)
            except (OSError, UnicodeDecodeError):
                continue
            if name in _CLASS_NAME.findall(text):
                declared[name] = to_res_path(path, root)
                break
    return declared


# Created and removed at once in a folder the editor has missed a new file in. The
# editor's scan skips names starting with "." (EditorFileSystem), so it is never
# registered; its only effect is the folder's modification time.
RESCAN_MARKER = ".godotlens-rescan"


def _nudge_folder(folder: str) -> None:
    """Make the editor's next change scan re-read ``folder``.

    A change scan re-reads only folders whose modification time differs from the one
    it recorded, and Godot keeps those times in whole seconds (file_access_windows.cpp).
    A file created in the same second as a scan already recorded is therefore invisible
    until the folder changes again in a later second.
    """
    marker = os.path.join(folder, RESCAN_MARKER)
    try:
        with open(marker, "w", encoding="utf-8"):
            pass
        os.remove(marker)
    except OSError:
        pass  # a folder we cannot write to; the hint still explains what to do


async def _await_new_classes(contents: dict[str, str]) -> tuple[dict, set[str]]:
    """Give the editor time to register class_name files it has never seen.

    ``contents`` maps each just-synced file to the text it was synced with. If their
    diagnostics name a class that a project file declares, the editor simply has not
    registered that file yet: the language server never learns about a new class from
    a sync, only from the editor's filesystem scan. The dependants are re-checked for
    up to NEW_CLASS_WAIT seconds. A name no project file declares is a real typo and
    is left to the diagnostics.

    Returns extra result fields, and the files whose re-check Godot never answered.
    """
    undeclared = _undeclared_names(list(contents))
    if not undeclared:
        return {}, set()
    declared = await _project_class_declarations(set(undeclared))
    pending = dict(declared)
    if not pending:
        return {}, set()

    affected = sorted({fp for name in pending for fp in undeclared[name]})
    loop = asyncio.get_event_loop()
    started = loop.time()
    deadline = started + NEW_CLASS_WAIT
    missing: set[str] = set()
    nudged = False
    while pending and loop.time() < deadline:
        await asyncio.sleep(0.5)
        if not nudged and loop.time() - started >= 1.0:
            # Still unknown a second after the file was written: the editor's scan may have
            # recorded the folder in the same second, so make the folder change again.
            nudged = True
            root = find_project_root()
            for folder in sorted({os.path.dirname(_res_to_disk(path, root)) for path in pending.values()}):
                await asyncio.to_thread(_nudge_folder, folder)
        for file_path in affected:
            _lsp.diagnostics_cache.pop(canonical_key(file_path), None)
            await _lsp.sync_document(file_uri(file_path), contents[file_path], notify_save=False)
        missing = await _lsp.wait_for_diagnostics([canonical_key(fp) for fp in affected],
                                                  timeout=DIAGNOSTICS_TIMEOUT)
        still = _undeclared_names(affected)
        pending = {name: path for name, path in pending.items() if name in still}

    unverified = {fp for fp in affected if canonical_key(fp) in missing}
    if not pending:
        return {"new_classes_registered": sorted(declared)}, unverified
    return {"unregistered_classes": pending, "hint": UNREGISTERED_CLASS_HINT}, unverified


async def _check_scripts(files: list[str]) -> dict | None:
    """Errors the language server reports in these scripts, read from disk.

    Returns {file: [error, ...]} for files with errors ({} when all are clean), or None
    when no editor is connected to ask. Used before hot-reloading into a running game,
    where a script that fails to compile is disabled.
    """
    ok, _ = await ensure_connected()
    if not ok:
        return None
    async with _lsp.document_lock:
        keys = {}
        for file_path in files:
            disk_path = _res_to_disk(file_path, find_project_root())
            key = canonical_key(disk_path)
            _lsp.diagnostics_cache.pop(key, None)
            await _lsp.sync_document(file_uri(disk_path), await read_text_file(disk_path),
                                     notify_save=False)
            keys[file_path] = key
        await _lsp.wait_for_diagnostics(list(keys.values()), timeout=DIAGNOSTICS_TIMEOUT)
    problems = {}
    for file_path, key in keys.items():
        errors = [d for d in _compact_diagnostics(_lsp.diagnostics_cache.get(key, []))
                  if d["severity"] == 1]
        if errors:
            problems[file_path] = errors
    return problems


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
        return tool_result(json.dumps({
            "status": status, "message": msg, "host": _lsp.host, "port": _lsp.port,
            # Agents that share one GodotLens see the same pid; separate servers differ.
            "server_pid": os.getpid(),
        }))

    # Only the language-server tools need the language server. scene_*, project_config
    # and debug_* reach Godot by other routes and used to be refused for no reason
    # whenever the editor's LSP was down.
    if name.startswith("gdscript_"):
        ok, msg = await ensure_connected()
        if not ok:
            return tool_result(json.dumps({"error": msg, "status": "disconnected"}), is_error=True)
        # Tool calls run concurrently. Godot's language server answers one request at a
        # time anyway, and some of these tools briefly put temporary content into a
        # document (validate's draft, a re-sync), so they run one after another and a
        # concurrent query can never observe it.
        async with _lsp.document_lock:
            return await _dispatch_tool(name, arguments)
    return await _dispatch_tool(name, arguments)


async def _dispatch_tool(name: str, arguments: dict) -> dict:
    """Run one tool and turn every failure into a result the agent can act on."""
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
                # nativeSymbol searches only the named class's own members
                # (GDScriptWorkspace::resolve_native_symbol), so add_child on a
                # CharacterBody2D came back "not found". Walk the ancestors on a miss.
                chain = [class_name, *_ancestors(
                    class_name, _lsp.native_capabilities.get("native_classes") or [])]
                result = None
                for owner in chain:
                    result = await _lsp.request("textDocument/nativeSymbol", {
                        "native_class": owner, "symbol_name": member})
                    if result is not None:
                        if isinstance(result, dict):
                            result["declared_in"] = owner
                            result["inheritance"] = chain
                        break
                if result is None:
                    result = {"found": False, "class_name": class_name, "member": member,
                              "searched": chain}
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

        # Runtime (Debug Adapter Protocol, port 6006)
        elif name.startswith("debug_"):
            ok, message = await ensure_dap()
            if not ok:
                return tool_result(json.dumps({
                    "error": message,
                    "kind": "dap_disconnected",
                    "hint": ("The debug adapter lives in the Godot editor. Confirm it is "
                             "enabled under Editor Settings > Network > Debug Adapter, or "
                             "start Godot with --dap-port."),
                }), is_error=True)

            if name == "debug_status":
                await _dap.drain_events(0.3)
                result = {
                    "connected": True,
                    "host": _dap.host,
                    "port": _dap.port,
                    "server_pid": os.getpid(),
                    "capabilities": _dap.capabilities,
                    "stopped": _dap.stopped_state,
                    "terminated": _dap.terminated,
                    "buffered_output_lines": len(_dap.output),
                }

            elif name == "debug_output":
                # Collect whatever the game has emitted since the last call.
                await _dap.drain_events(float(arguments.get("wait", 1.0)))
                clear = bool(arguments.get("clear", True))
                lines = _dap.take_output(clear=clear)
                errors = _dap.take_errors(clear=clear)
                result = {
                    "lines": lines,
                    "count": len(lines),
                    "errors": errors,
                    "error_count": len(errors),
                    "note": ("Empty means nothing was printed since the last call. Output "
                             "only arrives while a game started with debug_run is running."),
                }

            elif name == "debug_set_breakpoints":
                result = {
                    "file": arguments["file"],
                    "breakpoints": await _dap.set_breakpoints(
                        await _abspath(arguments["file"]), arguments["lines"]),
                }

            elif name == "debug_stack_trace":
                await _dap.drain_events(0.3)
                frames = await _dap.stack_trace(int(arguments.get("thread_id", 1)))
                result = {
                    "frames": frames,
                    "stopped": _dap.stopped_state,
                    "note": ("An empty frame list means execution is not currently paused. "
                             "Frames are only available while stopped at a breakpoint or error."),
                }

            elif name == "debug_inspect":
                frame_id = int(arguments["frame_id"])
                scopes = await _dap.scopes(frame_id)
                contents = []
                for scope in scopes:
                    reference = scope.get("variables_reference")
                    variables = []
                    if reference:
                        try:
                            variables = await _dap.variables(reference)
                        except DAPError:
                            variables = []
                    contents.append({"scope": scope["name"], "variables": variables})
                result = {"frame_id": frame_id, "scopes": contents}

            elif name == "debug_evaluate":
                refusal, frame_id = await _evaluate_refusal(
                    int(arguments["frame_id"]) if arguments.get("frame_id") is not None else None)
                if refusal is not None:
                    return tool_result(json.dumps(refusal), is_error=True)
                result = await _dap.evaluate(arguments["expression"], frame_id)

            elif name == "debug_continue":
                await _dap.continue_execution(int(arguments.get("thread_id", 1)))
                result = {"resumed": True}

            elif name == "debug_pause":
                await _dap.pause(int(arguments.get("thread_id", 1)))
                await _dap.drain_events(1.0)
                result = {"paused": True, "stopped": _dap.stopped_state}

            elif name == "debug_step_over":
                await _dap.step_over(int(arguments.get("thread_id", 1)))
                await _dap.drain_events(1.0)
                result = {"stepped": True, "stopped": _dap.stopped_state}

            elif name == "debug_run":
                root = find_project_root()
                body = await _dap.launch(
                    root,
                    scene=arguments.get("scene"),
                    timeout=float(arguments.get("timeout", 60.0)),
                    play_args=[str(a) for a in arguments.get("args") or []])
                lines = await _dap.collect_output(float(arguments.get("collect", 10.0)))
                errors = _dap.take_errors()
                result = {
                    "launched": True,
                    "project": root,
                    "output": lines,
                    "output_lines": len(lines),
                    "errors": errors,
                    "exited": _dap.terminated,
                    "stopped": _dap.stopped_state,
                    "events": [e["event"] for e in _dap.events],
                    "launch_body": body,
                }
                if not _dap.terminated and not _dap.stopped_state:
                    result["note"] = (
                        "The game is still running. Call debug_output for more, "
                        "debug_stack_trace if it hits a breakpoint, or debug_terminate to stop it."
                    )

            elif name == "debug_terminate":
                await _dap.terminate()
                result = {"terminated": True}

            else:
                return tool_result(json.dumps({"error": f"Unknown tool: {name}"}), is_error=True)

        # Runtime: games GodotLens launched, over their own debugger connection.
        elif name.startswith("runtime_"):
            if name not in runtime_tools.TOOL_NAMES:
                return tool_result(json.dumps({"error": f"Unknown tool: {name}"}), is_error=True)
            result, images = await runtime_tools.call(
                name, arguments, find_project_root(), _check_scripts)
            if images:
                return tool_result(json.dumps(result), images=images)

        elif name == "gdscript_find":
            wanted = arguments["name"]
            root = find_project_root()
            if arguments.get("file"):
                candidates = [await _abspath(arguments["file"])]
            else:
                candidates = await _files_mentioning(wanted, await _list_project_scripts(root))

            # Each candidate costs one documentSymbol round trip, issued serially. A
            # common name like "_ready" can appear in hundreds of files, which would
            # block the whole server for minutes. Cap it, and say so rather than
            # quietly returning a partial answer that looks complete.
            limit = int(arguments.get("max_files", FIND_FILE_LIMIT))
            truncated = len(candidates) > limit
            searched = candidates[:limit]

            declarations = []
            for path in searched:
                try:
                    tree = await _lsp.request("textDocument/documentSymbol", {
                        "textDocument": {"uri": file_uri(path)}})
                except (LSPError, UnsupportedPathError):
                    continue
                for hit in _walk_symbols(tree, wanted):
                    declarations.append({"file": path.replace("\\", "/"), **hit})

            result = {
                "name": wanted,
                "declarations": declarations,
                "count": len(declarations),
                "searched_files": len(searched),
                "candidate_files": len(candidates),
            }
            if truncated:
                result["truncated"] = True
                result["warning"] = (
                    f"{len(candidates)} files mention '{wanted}'; only the first {limit} "
                    "were checked. Results may be incomplete — narrow with 'file', or "
                    "raise 'max_files'."
                )
            if not declarations:
                result["hint"] = (
                    "No declaration found. The name may be a local variable, a parameter, "
                    "or an engine symbol — try gdscript_engine_api for built-ins."
                )
            elif arguments.get("include_references"):
                primary = declarations[0]
                try:
                    refs = await _lsp.request("textDocument/references", {
                        "textDocument": {"uri": file_uri(primary["file"])},
                        "position": {"line": primary["line"], "character": primary["char"]},
                        "context": {"includeDeclaration": True}})
                    result["references"] = [compact_location(r) for r in refs or []]
                except LSPError as exc:
                    result["references_error"] = str(exc)

        elif name == "project_config":
            root = find_project_root()
            result = await dump_project(root)

        elif name == "project_validate":
            report = await validate_project(find_project_root())
            report["valid"] = not (report["broken"] or report["missing_autoloads"]
                                   or not report["main_scene"]["exists"])
            result = report

        elif name == "scene_state":
            root = find_project_root()
            dump = await dump_scenes(arguments["files"], root)
            result = dump

        elif name == "scene_validate":
            root = find_project_root()
            dump = await dump_scenes(arguments["files"], root)
            # Handler existence comes from the LSP's parse. Without an editor the other
            # checks still run; the handler check is reported as skipped, never passed.
            lsp_ok, _ = await ensure_connected()
            report = {}
            for res_path, scene in (dump.get("scenes") or {}).items():
                # Ask the LSP which functions each attached script really has, so the
                # check is against Godot's parse rather than our own reading.
                symbols: dict[str, set[str]] = {}
                async with _lsp.document_lock:
                    for script_res in collect_scripts(scene) if lsp_ok else []:
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
            if not lsp_ok:
                result["handler_check"] = "skipped: language server not connected"

        elif name == "gdscript_validate":
            # Check proposed content WITHOUT writing it to disk, so a broken edit is
            # caught before it lands in the project.
            file_path = arguments["file"]
            uri = file_uri(file_path)
            key = canonical_key(file_path)
            was_open = _lsp.is_open(uri)
            original = None
            if was_open:
                try:
                    original = await read_text_file(file_path)
                except OSError:
                    original = None

            _lsp.diagnostics_cache.pop(key, None)
            await _lsp.sync_document(uri, arguments["content"], notify_save=False)
            missing = await _lsp.wait_for_diagnostics([key], timeout=DIAGNOSTICS_TIMEOUT)
            diagnostics = _compact_diagnostics(_lsp.diagnostics_cache.get(key, []))

            # Put the LSP back on the real file so a rejected draft is not left behind.
            # A file this session had not opened is released instead: leaving the draft
            # open made every later query on it answer from code that was never written.
            _lsp.diagnostics_cache.pop(key, None)
            if original is not None:
                await _lsp.sync_document(uri, original, notify_save=False)
            else:
                await _lsp.close_document(uri)

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
            new_classes, recheck_missing = await _await_new_classes({file_path: content})
            missing = set(missing) | {canonical_key(fp) for fp in recheck_missing}

            result = {
                "synced": file_path,
                "action": action,
                "diagnostics": _compact_diagnostics(_lsp.diagnostics_cache.get(key, [])),
                # Distinguishes "Godot checked it and it is clean" from "Godot never
                # reported back". Reporting the second as clean is how broken code
                # used to get a passing verdict.
                "verified": not missing,
                **new_classes,
            }
            if missing:
                result["warning"] = (
                    f"Godot did not publish diagnostics within {DIAGNOSTICS_TIMEOUT}s; "
                    "the file may still be parsing. Results are not a clean bill of health."
                )

        elif name == "gdscript_sync_files":
            synced_uris = []
            contents: dict[str, str] = {}
            errors = []
            for file_path in arguments["files"]:
                try:
                    content = await read_text_file(file_path)
                    uri = file_uri(file_path)
                    _lsp.diagnostics_cache.pop(canonical_key(file_path), None)
                    await _lsp.sync_document(uri, content)
                    synced_uris.append((file_path, uri))
                    contents[file_path] = content
                except (OSError, UnsupportedPathError) as e:
                    errors.append({"file": file_path, "error": str(e)})

            keys = [canonical_key(fp) for fp, _ in synced_uris]
            missing = await _lsp.wait_for_diagnostics(keys, timeout=DIAGNOSTICS_TIMEOUT)
            new_classes, recheck_missing = await _await_new_classes(contents)
            missing = set(missing) | {canonical_key(fp) for fp in recheck_missing}

            results = {}
            for file_path, _uri in synced_uris:
                results[file_path] = _compact_diagnostics(
                    _lsp.diagnostics_cache.get(canonical_key(file_path), []))

            result = {
                "synced": len(synced_uris),
                "diagnostics": results,
                "verified": not missing,
                **new_classes,
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
                         "state clears when the editor rescans. After deleting or moving "
                         "a file, project_validate finds scenes still pointing at it."),
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

    except (DAPConnectionLost, DAPTimeout) as e:
        if _dap is not None:
            await _dap.disconnect()
        return tool_result(json.dumps({
            "error": str(e), "kind": "dap_connection_lost",
            "hint": "Call debug_status to reconnect to the debug adapter.",
        }), is_error=True)

    except DAPError as e:
        return tool_result(json.dumps({"error": str(e), "kind": "dap_error"}), is_error=True)

    except SessionError as e:
        payload = {"error": str(e), "kind": e.kind}
        if e.detail:
            payload["detail"] = e.detail
        return tool_result(json.dumps(payload), is_error=True)

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

# How long in-flight tool calls may finish after the client closes stdin.
SHUTDOWN_GRACE = 10.0


async def respond(msg: dict) -> None:
    """Handle one request and write its response, containing any failure."""
    try:
        response = await handle_request(msg)
    except Exception as exc:
        log(f"unhandled error in {msg.get('method', '?')}: {exc!r}")
        response = jsonrpc_error(msg.get("id"), -32603, f"Internal error: {exc}")
    if response is not None:
        write_message(response)


def lsp_client_from_env() -> LSPClient:
    """The language-server client as GODOT_LSP_HOST/PORT/TIMEOUT configure it."""
    return LSPClient(
        host=os.environ.get("GODOT_LSP_HOST", "127.0.0.1"),
        port=int(os.environ.get("GODOT_LSP_PORT", "6005")),
        timeout=float(os.environ.get("GODOT_LSP_TIMEOUT", str(DEFAULT_TIMEOUT))),
    )


async def main():
    """Run the MCP server over stdio."""
    global _lsp, _dap

    _lsp = lsp_client_from_env()
    in_flight: set[asyncio.Task] = set()

    try:
        while True:
            msg = await read_message()
            if msg is None:
                break  # EOF — the client closed stdin
            if isinstance(msg, ParseFailure):
                write_message(jsonrpc_error(None, msg.code, msg.message))
                continue  # a bad line must not end the session
            if msg.get("method") == "tools/call" and msg.get("id") is not None:
                # Tool calls run concurrently: agents sharing this server must not wait
                # behind one another's debug_run or scene dump. Everything else, and
                # initialize in particular, is answered in order.
                task = asyncio.get_event_loop().create_task(respond(msg))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            else:
                await respond(msg)
        # The client closed stdin; answer what it already asked before shutting down.
        if in_flight:
            await asyncio.wait(in_flight, timeout=SHUTDOWN_GRACE)
    finally:
        for task in in_flight:
            task.cancel()
        await runtime_tools.stop_all()  # games GodotLens launched end with it
        await _lsp.disconnect()
        _lsp = None
        if _dap is not None:
            await _dap.disconnect()
            _dap = None


def main_sync():
    """Sync entry point for console_scripts and __main__."""
    asyncio.run(main())
