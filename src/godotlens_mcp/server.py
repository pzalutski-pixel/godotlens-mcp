"""MCP server for GDScript semantic analysis via Godot's built-in LSP."""

import asyncio
import json
import os
import sys
from typing import Any

from godotlens_mcp import __version__
from godotlens_mcp.lsp_client import (
    LSPClient,
    compact_location,
    compact_symbol,
    file_uri,
)

_lsp: LSPClient | None = None


# ---------------------------------------------------------------------------
# MCP protocol handler (JSON-RPC 2.0 over stdio, newline-delimited)
# ---------------------------------------------------------------------------

async def read_message() -> dict | None:
    """Read a newline-delimited JSON-RPC message from stdin."""
    loop = asyncio.get_event_loop()
    line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
    if not line:
        return None  # EOF
    line = line.strip()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


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
    },
    {
        "name": "gdscript_declaration",
        "description": (
            "Navigate to the declaration of a symbol at a given position. "
            "Returns: file path and line number of the declaration. "
            "IMPORTANT: Uses ZERO-BASED coordinates (editor line 1 = pass line 0). "
            "Similar to gdscript_definition but returns the declaration site."
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
            },
            "required": ["file", "line", "character", "new_name"],
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
    },
    {
        "name": "gdscript_delete_file",
        "description": (
            "Notify Godot's LSP that a file was deleted from the project. "
            "Returns: confirmation of deletion. "
            "WHEN TO CALL: After deleting a .gd file from disk. "
            "Ensures LSP removes the file from its analysis and clears stale diagnostics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative path to the deleted .gd file"},
            },
            "required": ["file"],
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
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

async def ensure_connected() -> tuple[bool, str]:
    """Ensure LSP is connected, return (ok, error_message)."""
    return await _lsp.connect()


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

        elif name == "gdscript_declaration":
            result = await _lsp.request("textDocument/declaration", {
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
            result = await _lsp.request("textDocument/rename", {
                "textDocument": {"uri": file_uri(arguments["file"])},
                "position": {"line": arguments["line"], "character": arguments["character"]},
                "newName": arguments["new_name"]
            })

        # Sync operations
        elif name == "gdscript_sync_file":
            file_path = arguments["file"]
            content = arguments.get("content")
            if content is None:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            uri = file_uri(file_path)
            await _lsp.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "gdscript", "version": 1, "text": content}
            })
            await _lsp.notify("textDocument/didSave", {
                "textDocument": {"uri": uri}, "text": content
            })

            await asyncio.sleep(0.3)
            try:
                await asyncio.wait_for(_lsp.drain_notifications(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

            diags = _lsp.diagnostics_cache.get(uri, [])
            result = {
                "synced": file_path,
                "diagnostics": [{
                    "line": d.get("range", {}).get("start", {}).get("line", 0),
                    "severity": d.get("severity", 1),
                    "message": d.get("message", "")
                } for d in diags]
            }

        elif name == "gdscript_sync_files":
            synced_uris = []
            errors = []
            for file_path in arguments["files"]:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    uri = file_uri(file_path)
                    await _lsp.notify("textDocument/didOpen", {
                        "textDocument": {"uri": uri, "languageId": "gdscript", "version": 1, "text": content}
                    })
                    await _lsp.notify("textDocument/didSave", {
                        "textDocument": {"uri": uri}, "text": content
                    })
                    synced_uris.append((file_path, uri))
                except Exception as e:
                    errors.append({"file": file_path, "error": str(e)})

            await asyncio.sleep(0.3)
            try:
                await asyncio.wait_for(_lsp.drain_notifications(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

            results = {}
            for file_path, uri in synced_uris:
                diags = _lsp.diagnostics_cache.get(uri, [])
                results[file_path] = [{
                    "line": d.get("range", {}).get("start", {}).get("line", 0),
                    "severity": d.get("severity", 1),
                    "message": d.get("message", "")
                } for d in diags]

            result = {"synced": len(synced_uris), "diagnostics": results}
            if errors:
                result["errors"] = errors

        elif name == "gdscript_delete_file":
            file_path = arguments["file"]
            uri = file_uri(file_path)
            await _lsp.notify("textDocument/didClose", {
                "textDocument": {"uri": uri}
            })
            await _lsp.notify("workspace/didDeleteFiles", {
                "files": [{"uri": uri}]
            })
            result = {"deleted": file_path}

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
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    uri = file_uri(file_path)
                    await _lsp.notify("textDocument/didOpen", {
                        "textDocument": {"uri": uri, "languageId": "gdscript", "version": 1, "text": content}
                    })
                except Exception as e:
                    results[file_path] = {"error": str(e)}

            await asyncio.sleep(0.3)
            try:
                await asyncio.wait_for(_lsp.drain_notifications(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

            for file_path in arguments["files"]:
                if file_path not in results:
                    uri = file_uri(file_path)
                    diags = _lsp.diagnostics_cache.get(uri, [])
                    results[file_path] = [{
                        "line": d.get("range", {}).get("start", {}).get("line", 0),
                        "severity": d.get("severity", 1),
                        "message": d.get("message", "")
                    } for d in diags]
            result = results

        else:
            return tool_result(json.dumps({"error": f"Unknown tool: {name}"}), is_error=True)

        text = json.dumps(result, indent=2) if result else "No results"
        return tool_result(text)

    except Exception as e:
        await _lsp.disconnect()
        return tool_result(
            json.dumps({
                "error": str(e),
                "hint": "LSP connection may have been lost. Try gdscript_status to reconnect.",
            }),
            is_error=True,
        )


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
        return jsonrpc_response(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "godotlens-mcp",
                "version": __version__,
            },
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
                break
            response = await handle_request(msg)
            if response is not None:
                write_message(response)
    finally:
        await _lsp.disconnect()
        _lsp = None


def main_sync():
    """Sync entry point for console_scripts and __main__."""
    asyncio.run(main())
