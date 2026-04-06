"""LSP client for communicating with Godot's built-in language server."""

import asyncio
import json
import os
from typing import Any


class LSPClient:
    """Async client for Godot's LSP server over TCP."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6005):
        self.host = host
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.request_id = 0
        self.initialized = False
        self.last_error: str | None = None
        self.diagnostics_cache: dict[str, list] = {}  # uri -> [diagnostics]

    async def connect(self) -> tuple[bool, str]:
        """Connect to LSP. Returns (success, message)."""
        if self.writer and not self.writer.is_closing():
            return True, "Connected"

        self.reader = None
        self.writer = None
        self.initialized = False

        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0
            )
            await self._initialize()
            self.last_error = None
            return True, "Connected to Godot LSP"
        except asyncio.TimeoutError:
            self.last_error = f"Connection timeout to {self.host}:{self.port}"
            return False, (
                f"Timeout connecting to Godot LSP at {self.host}:{self.port}. "
                "Is Godot editor running with project open?"
            )
        except ConnectionRefusedError:
            self.last_error = f"Connection refused at {self.host}:{self.port}"
            return False, f"Connection refused at {self.host}:{self.port}. Start Godot editor with your project open."
        except Exception as e:
            self.last_error = str(e)
            return False, f"LSP connection error: {e}"

    async def disconnect(self):
        """Disconnect from LSP server."""
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None
        self.initialized = False

    async def _initialize(self):
        if self.initialized:
            return
        await self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": "file:///" + os.getcwd().replace(os.sep, "/"),
            "capabilities": {}
        })
        await self.notify("initialized", {})
        self.initialized = True

    async def request(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and return the result."""
        self.request_id += 1
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params
        })
        header = f"Content-Length: {len(msg)}\r\n\r\n"
        self.writer.write((header + msg).encode())
        await self.writer.drain()
        return await self._read_response()

    async def notify(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        })
        header = f"Content-Length: {len(msg)}\r\n\r\n"
        self.writer.write((header + msg).encode())
        await self.writer.drain()

    async def _read_response(self) -> Any:
        headers = {}
        while True:
            line = await self.reader.readline()
            line = line.decode().strip()
            if not line:
                break
            if ":" in line:
                key, val = line.split(":", 1)
                headers[key.strip()] = val.strip()

        length = int(headers.get("Content-Length", 0))
        body = await self.reader.read(length)
        data = json.loads(body.decode())

        # Process notifications (cache diagnostics), wait for response with id
        while "id" not in data:
            # Cache publishDiagnostics notifications
            if data.get("method") == "textDocument/publishDiagnostics":
                params = data.get("params", {})
                uri = params.get("uri", "")
                self.diagnostics_cache[uri] = params.get("diagnostics", [])

            # Read next message
            headers = {}
            while True:
                line = await self.reader.readline()
                line = line.decode().strip()
                if not line:
                    break
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.strip()] = val.strip()
            length = int(headers.get("Content-Length", 0))
            body = await self.reader.read(length)
            data = json.loads(body.decode())

        if "error" in data:
            raise Exception(data["error"].get("message", "LSP error"))
        return data.get("result")

    async def drain_notifications(self):
        """Read and process any pending notifications (non-blocking)."""
        while True:
            try:
                headers = {}
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
                line = line.decode().strip()
                if not line:
                    continue
                while line:
                    if ":" in line:
                        key, val = line.split(":", 1)
                        headers[key.strip()] = val.strip()
                    line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
                    line = line.decode().strip()

                length = int(headers.get("Content-Length", 0))
                body = await self.reader.read(length)
                data = json.loads(body.decode())

                # Cache diagnostics
                if data.get("method") == "textDocument/publishDiagnostics":
                    params = data.get("params", {})
                    uri = params.get("uri", "")
                    self.diagnostics_cache[uri] = params.get("diagnostics", [])

            except asyncio.TimeoutError:
                break


def file_uri(path: str) -> str:
    """Convert a file path to a file:// URI."""
    path = path.replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    return f"file://{path}"


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a file path."""
    if uri.startswith("file:///"):
        return uri[8:].replace("%3A", ":").replace("%20", " ")
    return uri


def compact_location(loc: dict) -> dict:
    """Compact a Location to file:line:char format."""
    return {
        "file": uri_to_path(loc.get("uri", "")),
        "line": loc.get("range", {}).get("start", {}).get("line", 0),
        "char": loc.get("range", {}).get("start", {}).get("character", 0)
    }


def compact_symbol(sym: dict) -> dict:
    """Compact a symbol to essential info."""
    result = {
        "name": sym.get("name", ""),
        "kind": sym.get("kind", 0),
        "line": sym.get("range", sym.get("location", {})).get("start", {}).get("line", 0)
    }
    if "children" in sym:
        result["children"] = [compact_symbol(c) for c in sym["children"]]
    return result
