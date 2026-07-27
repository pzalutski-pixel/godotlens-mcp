"""LSP client for communicating with Godot's built-in language server."""

import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from godotlens_mcp.capabilities import Capabilities

DEFAULT_TIMEOUT = 15.0


class LSPError(Exception):
    """The LSP returned a JSON-RPC error. The connection is still healthy."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def is_method_not_found(self) -> bool:
        return self.code == -32601


class LSPProtocolError(Exception):
    """A malformed frame was received."""


class LSPConnectionLost(Exception):
    """The transport failed or the peer closed. Requires reconnect."""


class LSPTimeout(Exception):
    """The LSP did not answer within the deadline."""


class LSPClient:
    """Async client for Godot's LSP server over TCP."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6005, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.request_id = 0
        self.initialized = False
        self.last_error: str | None = None
        # Keyed by canonical_key(uri), NOT by the raw URI — Godot 4.5+ percent-encodes
        # published URIs, so raw string keys never match what file_uri() builds.
        self.diagnostics_cache: dict[str, list] = {}
        # Populated from server->client traffic that used to be discarded.
        self.server_capabilities: dict = {}
        self.native_capabilities: dict = {}
        self.server_messages: list[str] = []
        self.workspace_mismatch: str | None = None
        self.capabilities = Capabilities()
        # URIs this connection has opened. Godot 4.6+ rejects a second didOpen for a
        # file it already owns (ERR_FAIL_COND_MSG on managed_files) and returns before
        # reparsing, so the first text stays cached and poisons every later query.
        # Re-syncing must use didChange, which means tracking what is open.
        self._open_docs: dict[str, int] = {}  # uri -> document version
        # Serializes request/response pairs over the single shared socket.
        self._lock = asyncio.Lock()

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
            return False, (
                f"Connection refused at {self.host}:{self.port}. Start Godot editor with your "
                "project open, or check the port under Editor Settings > Network > Language Server."
            )
        except LSPTimeout as e:
            self.last_error = str(e)
            await self._force_close()
            return False, str(e)
        except Exception as e:
            self.last_error = str(e)
            await self._force_close()
            return False, f"LSP connection error: {e}"

    async def _force_close(self):
        """Drop the transport without awaiting a clean handshake."""
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = None
        self.writer = None
        self.initialized = False
        self._open_docs.clear()

    async def disconnect(self):
        """Disconnect from LSP server."""
        if self.writer:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass  # peer already gone; the socket is closed either way
        self.reader = None
        self.writer = None
        self.initialized = False
        self.server_capabilities = {}
        # Document ownership is per-connection; a reconnect starts from nothing open.
        self._open_docs.clear()

    async def _initialize(self):
        if self.initialized:
            return
        root = find_project_root()
        result = await self.request("initialize", {
            "processId": os.getpid(),
            # Godot <=4.4 compares rootPath (not rootUri) to decide whether the client
            # is on the same workspace, so both must be sent.
            "rootUri": file_uri(root),
            "rootPath": root,
            "capabilities": {},
        })
        self.server_capabilities = (result or {}).get("capabilities", {}) or {}
        self.capabilities = Capabilities(self.server_capabilities)
        await self.notify("initialized", {})
        # Godot pushes gdscript/capabilities (the full native class list) right after
        # initialized, plus changeWorkspace/showMessage if we named the wrong project.
        await self.drain_notifications(window=1.0)
        self.initialized = True

    async def _send(self, payload: dict) -> None:
        """Frame and write one JSON-RPC message."""
        if self.writer is None:
            raise LSPConnectionLost("Not connected to Godot LSP")
        body = json.dumps(payload).encode("utf-8")
        # Length must count BYTES. json.dumps escapes non-ASCII by default so this is
        # currently always equal to len(str), but encoding first keeps it correct if
        # ensure_ascii is ever disabled.
        self.writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await self.writer.drain()

    async def request(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and return its result.

        Serialized under a lock: one socket, and responses are matched by id, so
        overlapping callers must not interleave their frames.
        """
        async with self._lock:
            self.request_id += 1
            expect_id = self.request_id
            try:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": expect_id,
                    "method": method,
                    "params": params,
                })
                return await self._read_response(expect_id)
            except asyncio.TimeoutError as exc:
                raise LSPTimeout(
                    f"Godot LSP did not respond to {method} within {self.timeout}s. "
                    "The editor may be busy reimporting, or the request may be unsupported."
                ) from exc
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                raise LSPConnectionLost(f"LSP connection lost during {method}: {exc}") from exc

    async def notify(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        async with self._lock:
            try:
                await self._send({"jsonrpc": "2.0", "method": method, "params": params})
            except (ConnectionError, OSError) as exc:
                raise LSPConnectionLost(f"LSP connection lost sending {method}: {exc}") from exc

    async def _read_message(self, timeout: float | None) -> dict:
        """Read exactly one Content-Length framed message."""
        headers = {}
        while True:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
            if not line:
                raise LSPConnectionLost("LSP closed the connection")
            text = line.decode("utf-8").strip()
            if not text:
                break
            if ":" in text:
                key, val = text.split(":", 1)
                headers[key.strip().lower()] = val.strip()

        length = int(headers.get("content-length", 0))
        if length <= 0:
            raise LSPProtocolError(f"LSP sent a message with Content-Length {length}")
        # readexactly, not read: read(n) returns *up to* n bytes, so a body split
        # across TCP segments came back truncated and json.loads blew up. That scaled
        # with payload size, breaking large documentSymbol/references responses first.
        body = await asyncio.wait_for(self.reader.readexactly(length), timeout)
        return json.loads(body.decode("utf-8"))

    def _handle_inbound(self, data: dict) -> None:
        """Process a message that is not the response we are waiting for."""
        method = data.get("method")
        if method == "textDocument/publishDiagnostics":
            params = data.get("params", {})
            self.diagnostics_cache[canonical_key(params.get("uri", ""))] = params.get("diagnostics", [])
        elif method == "gdscript_client/changeWorkspace":
            # Godot telling us the client's rootUri is not the project it has open.
            self.workspace_mismatch = (data.get("params") or {}).get("path") or "unknown"
        elif method == "window/showMessage":
            self.server_messages.append((data.get("params") or {}).get("message", ""))
        elif method == "gdscript/capabilities":
            self.native_capabilities = data.get("params") or {}

    async def _read_response(self, expect_id: int) -> Any:
        """Read until the response carrying ``expect_id`` arrives."""
        while True:
            data = await self._read_message(self.timeout)

            msg_id = data.get("id")
            if msg_id == expect_id:
                if "error" in data:
                    raise LSPError(
                        data["error"].get("message", "LSP error"),
                        code=data["error"].get("code"),
                    )
                return data.get("result")

            if msg_id is not None and "method" in data:
                # A server-initiated *request*. It carries an id but is not our
                # response; previously it was mistaken for one, which returned None
                # and left the real reply in the buffer, desyncing every later call.
                await self._reject_server_request(msg_id, data["method"])
                continue

            if msg_id is not None:
                continue  # stale response to a request we already gave up on

            self._handle_inbound(data)

    async def _reject_server_request(self, req_id: Any, method: str) -> None:
        """Answer an unsupported server->client request instead of swallowing it."""
        await self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })

    async def sync_document(self, uri: str, text: str, notify_save: bool = True) -> str:
        """Push current file contents to the LSP, opening or updating as appropriate.

        Returns "opened" or "changed". Godot 4.6+ hard-rejects a repeat didOpen, so a
        file already open on this connection must be updated with didChange (Full
        sync) instead — otherwise the LSP keeps serving the text from the first sync.
        """
        if uri in self._open_docs:
            version = self._open_docs[uri] + 1
            self._open_docs[uri] = version
            await self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                # textDocumentSync.change == 1 (Full): one range-less content entry.
                "contentChanges": [{"text": text}],
            })
            action = "changed"
        else:
            self._open_docs[uri] = 1
            await self.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "gdscript",
                                 "version": 1, "text": text},
            })
            action = "opened"

        if notify_save:
            await self.notify("textDocument/didSave", {
                "textDocument": {"uri": uri}, "text": text})
        return action

    async def close_document(self, uri: str) -> bool:
        """Release a document so the LSP stops shadowing the on-disk copy."""
        if uri not in self._open_docs:
            return False
        await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self._open_docs.pop(uri, None)
        return True

    def is_open(self, uri: str) -> bool:
        return uri in self._open_docs

    async def wait_for_diagnostics(self, keys: list[str], timeout: float = 5.0) -> set[str]:
        """Drain until every key has a published diagnostics entry, or time runs out.

        Returns the keys still missing. Godot publishes an entry for every file it
        parses — an *empty list* means "checked, clean", whereas *no entry* means we
        never heard back. A fixed sleep conflated the two and reported clean on files
        Godot had not finished parsing.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        pending = {k for k in keys if k not in self.diagnostics_cache}
        while pending and loop.time() < deadline:
            await self.drain_notifications(0.15)
            pending = {k for k in keys if k not in self.diagnostics_cache}
        return pending

    async def drain_notifications(self, window: float = 0.1):
        """Consume pending server->client traffic until the stream goes quiet."""
        if self._lock.locked():
            # Already inside request()/_initialize(); read directly to avoid deadlock.
            await self._drain(window)
        else:
            async with self._lock:
                await self._drain(window)

    async def _drain(self, window: float):
        while True:
            try:
                data = await self._read_message(window)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, LSPConnectionLost):
                break
            except (LSPProtocolError, json.JSONDecodeError):
                continue

            msg_id = data.get("id")
            if msg_id is not None and "method" in data:
                await self._reject_server_request(msg_id, data["method"])
            elif msg_id is None:
                self._handle_inbound(data)


def canonical_key(uri_or_path: str) -> str:
    """Normalize a file:// URI or a filesystem path to a stable comparison key.

    Godot 4.5+ percent-encodes each path segment when it publishes diagnostics, so on
    Windows it emits ``file:///C%3A/project/main.gd`` while ``file_uri()`` builds
    ``file:///C:/project/main.gd``. Comparing those URI strings directly never matches,
    which silently drops every diagnostic. Both sides are normalized to a canonical
    filesystem path instead, so the cache key is independent of URI encoding.
    """
    text = uri_or_path
    if text.startswith("file://"):
        parts = urlsplit(text)
        text = unquote(parts.path)
        # "/C:/x" (Windows drive) -> "C:/x"; a POSIX "/home/x" keeps its leading slash.
        if re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
        if parts.netloc:
            # UNC: file://server/share/x -> //server/share/x
            text = f"//{parts.netloc}{text}"
    else:
        text = os.path.expanduser(unquote(text))
    # Absolutize BOTH branches, symmetrically. Callers legitimately pass relative paths
    # (the tool schemas advertise them) while Godot always publishes absolute URIs;
    # comparing "broken.gd" against "C:/proj/broken.gd" silently never matched, so a
    # relative path yielded an empty diagnostics list — a false clean bill of health.
    # Applying abspath to only one side reintroduces the same mismatch on Windows,
    # where a POSIX-style path is drive-relative.
    return os.path.normcase(os.path.abspath(text))


class UnsupportedPathError(ValueError):
    """Raised for a path the LSP cannot accept, with a message naming the fix."""


def find_project_root(start: str | None = None) -> str:
    """Locate the Godot project root by walking up for ``project.godot``.

    The MCP server's working directory is wherever the client launched it, which is
    not necessarily the Godot project. Sending the wrong root makes Godot discard our
    rootUri and reply with a changeWorkspace notification.
    """
    override = os.environ.get("GODOT_PROJECT_ROOT")
    if override:
        return os.path.abspath(os.path.expanduser(override))

    current = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(current, "project.godot")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start or os.getcwd())
        current = parent


def file_uri(path: str) -> str:
    """Convert a filesystem path to an absolute ``file://`` URI.

    Relative paths are resolved against the process working directory — the tool
    schemas advertise relative paths, and previously ``"scripts/player.gd"`` became
    ``file:///scripts/player.gd``, pointing at the filesystem root.

    Godot 4.5+ rejects any non-``file`` scheme outright, so ``res://`` is refused here
    with an actionable message rather than being silently mangled into
    ``file:///res://...``.
    """
    if path.startswith("res://"):
        raise UnsupportedPathError(
            "Godot's LSP does not accept res:// URIs; pass a filesystem path instead "
            f"(got {path!r}). Resolve it against the project root first."
        )
    if path.startswith("file://"):
        # Already a URI — normalize through the inverse to avoid double-encoding.
        path = uri_to_path(path)

    text = path.replace("\\", "/")
    is_unc = text.startswith("//")
    absolute = os.path.abspath(os.path.expanduser(path))

    if is_unc:
        # abspath preserves the UNC form; emit file://server/share/... (authority form).
        body = absolute.replace("\\", "/").lstrip("/")
        host, _, rest = body.partition("/")
        return "file://" + quote(host) + "/" + quote(rest)

    text = absolute.replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text  # Windows drive path: C:/x -> /C:/x
    # Keep "/" and ":" literal; percent-encode spaces, "#", and non-ASCII.
    return "file://" + quote(text, safe="/:")


def uri_to_path(uri: str) -> str:
    """Convert a ``file://`` URI back to a filesystem path.

    Fully percent-decodes, preserves the POSIX leading slash (the old 8-character
    slice turned ``file:///home/u/x.gd`` into the relative ``home/u/x.gd``), and
    restores UNC paths from the authority component.
    """
    if not uri.startswith("file://"):
        return uri

    parts = urlsplit(uri)
    path = unquote(parts.path)

    if parts.netloc:
        # file://server/share/x -> //server/share/x
        return f"//{parts.netloc}{path}"
    if re.match(r"^/[A-Za-z]:", path):
        return path[1:]  # /C:/x -> C:/x
    return path  # POSIX absolute path keeps its leading slash


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
