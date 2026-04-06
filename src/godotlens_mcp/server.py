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
# MCP protocol handler (JSON-RPC 2.0 over stdio with Content-Length framing)
# ---------------------------------------------------------------------------

async def read_message(reader: asyncio.StreamReader) -> dict | None:
    """Read a Content-Length framed JSON-RPC message from the stream."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return None  # EOF
        line = line.decode("utf-8").strip()
        if not line:
            break
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip()] = val.strip()

    length = int(headers.get("Content-Length", 0))
    if length == 0:
        return None

    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


def write_message(data: dict) -> None:
    """Write a Content-Length framed JSON-RPC message to stdout."""
    body = json.dumps(data).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header + body)
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
        "description": "Check if Godot LSP is connected",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    # Navigation
    {
        "name": "gdscript_definition",
        "description": "Go to definition of a symbol",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "line": {"type": "integer", "description": "Line number (0-indexed)"},
                "character": {"type": "integer", "description": "Character position (0-indexed)"},
            },
            "required": ["file", "line", "character"],
        },
    },
    {
        "name": "gdscript_declaration",
        "description": "Go to declaration of a symbol",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "line": {"type": "integer", "description": "Line number (0-indexed)"},
                "character": {"type": "integer", "description": "Character position (0-indexed)"},
            },
            "required": ["file", "line", "character"],
        },
    },
    {
        "name": "gdscript_references",
        "description": "Find all references to a symbol",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "line": {"type": "integer", "description": "Line number (0-indexed)"},
                "character": {"type": "integer", "description": "Character position (0-indexed)"},
            },
            "required": ["file", "line", "character"],
        },
    },
    {
        "name": "gdscript_hover",
        "description": "Get hover information for a symbol",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "line": {"type": "integer", "description": "Line number (0-indexed)"},
                "character": {"type": "integer", "description": "Character position (0-indexed)"},
            },
            "required": ["file", "line", "character"],
        },
    },
    {
        "name": "gdscript_symbols",
        "description": "List all symbols in a file",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "gdscript_signature_help",
        "description": "Get function signature at position",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "line": {"type": "integer", "description": "Line number (0-indexed)"},
                "character": {"type": "integer", "description": "Character position (0-indexed)"},
            },
            "required": ["file", "line", "character"],
        },
    },
    # Refactoring
    {
        "name": "gdscript_rename",
        "description": "Rename a symbol across all files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "line": {"type": "integer", "description": "Line number (0-indexed)"},
                "character": {"type": "integer", "description": "Character position (0-indexed)"},
                "new_name": {"type": "string", "description": "New name for the symbol"},
            },
            "required": ["file", "line", "character", "new_name"],
        },
    },
    # Sync operations
    {
        "name": "gdscript_sync_file",
        "description": "Notify LSP that a file changed",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path"},
                "content": {
                    "type": "string",
                    "description": "File content (optional, reads from disk if not provided)",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "gdscript_sync_files",
        "description": "Notify LSP that multiple files changed",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "description": "List of file paths"},
            },
            "required": ["files"],
        },
    },
    {
        "name": "gdscript_delete_file",
        "description": "Notify LSP that a file was deleted",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path that was deleted"},
            },
            "required": ["file"],
        },
    },
    # Batch operations
    {
        "name": "gdscript_symbols_batch",
        "description": "Get symbols from multiple files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "description": "List of file paths"},
            },
            "required": ["files"],
        },
    },
    {
        "name": "gdscript_definitions_batch",
        "description": "Get definitions for multiple positions",
        "inputSchema": {
            "type": "object",
            "properties": {
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "character": {"type": "integer"},
                        },
                        "required": ["file", "line", "character"],
                    },
                    "description": "List of positions",
                },
            },
            "required": ["positions"],
        },
    },
    {
        "name": "gdscript_references_batch",
        "description": "Find references for multiple positions",
        "inputSchema": {
            "type": "object",
            "properties": {
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "character": {"type": "integer"},
                        },
                        "required": ["file", "line", "character"],
                    },
                    "description": "List of positions",
                },
            },
            "required": ["positions"],
        },
    },
    {
        "name": "gdscript_diagnostics",
        "description": "Get diagnostics (errors/warnings) for files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "description": "List of file paths"},
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

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    try:
        while True:
            msg = await read_message(reader)
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
