"""Client for Godot's Debug Adapter Protocol.

Godot serves DAP from the editor on port 6006 (Editor Settings > Network > Debug
Adapter), alongside the language server and with no addon required. It is the same
principle as the LSP bridge applied to runtime: the engine reports what it observed,
rather than us inferring it.

This closes the one part of the edit-verify loop the LSP cannot reach. The language
server can say whether code compiles; only the debugger can say what it did — printed
output, script errors with a stack trace, and variable values at a breakpoint.

Verified against Godot 4.7.1. Advertised capabilities include
supportsConfigurationDoneRequest, supportsEvaluateForHovers, supportsSetVariable,
supportsTerminateRequest and supportsBreakpointLocationsRequest.

Line numbering: DAP is 1-based by default. Every GodotLens tool is 0-based, matching
the LSP, so conversion happens explicitly at this boundary rather than relying on the
``linesStartAt1`` negotiation flag being honoured.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

DEFAULT_DAP_PORT = 6006
DEFAULT_TIMEOUT = 20.0

# Cap retained events so a chatty game cannot grow the buffer without bound.
MAX_EVENTS = 2000


class DAPError(Exception):
    """The adapter returned success: false."""


class DAPConnectionLost(Exception):
    """Transport failed or the adapter closed."""


class DAPTimeout(Exception):
    """The adapter did not answer in time."""


class DAPClient:
    """Async client for Godot's debug adapter."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_DAP_PORT,
                 timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.seq = 0
        self.initialized = False
        self.capabilities: dict = {}
        # Console/stderr lines and lifecycle events the game emitted.
        self.output: list[dict] = []
        self.events: list[dict] = []
        self.stopped_state: dict | None = None
        self.terminated = False
        self._lock = asyncio.Lock()

    # -- connection --------------------------------------------------------

    async def connect(self) -> tuple[bool, str]:
        if self.writer and not self.writer.is_closing():
            return True, "Connected"

        self.reader = self.writer = None
        self.initialized = False
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0)
        except asyncio.TimeoutError:
            return False, (f"Timeout connecting to Godot's debug adapter at "
                           f"{self.host}:{self.port}.")
        except ConnectionRefusedError:
            return False, (
                f"Connection refused at {self.host}:{self.port}. The debug adapter runs "
                "inside the Godot editor — check Editor Settings > Network > Debug Adapter, "
                "or launch with --dap-port."
            )
        except OSError as exc:
            return False, f"Debug adapter connection error: {exc}"

        try:
            await self._initialize()
        except (DAPTimeout, DAPError, DAPConnectionLost) as exc:
            await self.disconnect()
            return False, str(exc)
        return True, "Connected to Godot debug adapter"

    async def disconnect(self) -> None:
        if self.writer:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
        self.reader = self.writer = None
        self.initialized = False
        self.capabilities = {}

    async def _initialize(self) -> None:
        body = await self.request("initialize", {
            "clientID": "godotlens",
            "clientName": "GodotLens",
            "adapterID": "godot",
            # Explicitly 1-based on the wire; conversion happens in this module.
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "pathFormat": "path",
            "supportsVariableType": True,
            "supportsRunInTerminalRequest": False,
        })
        self.capabilities = body or {}
        await self.drain_events(1.0)
        self.initialized = True

    # -- transport ---------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        if self.writer is None:
            raise DAPConnectionLost("Not connected to Godot's debug adapter")
        body = json.dumps(payload).encode("utf-8")
        self.writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await self.writer.drain()

    async def _read_message(self, timeout: float | None) -> dict:
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
            if not line:
                raise DAPConnectionLost("Debug adapter closed the connection")
            text = line.decode("utf-8").strip()
            if not text:
                break
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        length = int(headers.get("content-length", 0))
        if length <= 0:
            raise DAPConnectionLost(f"Adapter sent a frame with Content-Length {length}")
        raw = await asyncio.wait_for(self.reader.readexactly(length), timeout)
        return json.loads(raw.decode("utf-8"))

    def _record_event(self, message: dict) -> None:
        event = message.get("event")
        body = message.get("body") or {}

        if event == "output":
            self.output.append({
                "category": body.get("category", "console"),
                "text": body.get("output", ""),
                "line": _to_zero_based(body.get("line")),
                "source": (body.get("source") or {}).get("path"),
            })
            if len(self.output) > MAX_EVENTS:
                del self.output[:-MAX_EVENTS]
            return

        if event == "stopped":
            self.stopped_state = {
                "reason": body.get("reason"),
                "thread_id": body.get("threadId"),
                "description": body.get("description"),
                "text": body.get("text"),
            }
        elif event in {"terminated", "exited"}:
            self.terminated = True

        self.events.append({"event": event, "body": body})
        if len(self.events) > MAX_EVENTS:
            del self.events[:-MAX_EVENTS]

    async def request(self, command: str, arguments: dict | None = None,
                      timeout: float | None = None) -> Any:
        """Send a DAP request and return its body."""
        deadline = timeout if timeout is not None else self.timeout
        async with self._lock:
            self.seq += 1
            expect = self.seq
            try:
                await self._send({"seq": expect, "type": "request", "command": command,
                                  "arguments": arguments or {}})
                while True:
                    message = await self._read_message(deadline)
                    if message.get("type") == "event":
                        self._record_event(message)
                        continue
                    if message.get("type") != "response":
                        continue
                    if message.get("request_seq") != expect:
                        continue  # a stale response we are no longer waiting on
                    if not message.get("success", False):
                        raise DAPError(message.get("message") or f"{command} failed")
                    return message.get("body")
            except asyncio.TimeoutError as exc:
                raise DAPTimeout(
                    f"Godot's debug adapter did not answer '{command}' within {deadline}s."
                ) from exc
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                raise DAPConnectionLost(f"Debug adapter connection lost: {exc}") from exc

    async def drain_events(self, window: float = 0.3) -> int:
        """Consume pending events until the stream goes quiet. Returns how many."""
        if self._lock.locked():
            return await self._drain(window)
        async with self._lock:
            return await self._drain(window)

    async def _drain(self, window: float) -> int:
        count = 0
        while True:
            try:
                message = await self._read_message(window)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    DAPConnectionLost, json.JSONDecodeError):
                return count
            if message.get("type") == "event":
                self._record_event(message)
                count += 1

    # -- operations --------------------------------------------------------

    async def set_breakpoints(self, file_path: str, lines: list[int]) -> list[dict]:
        """Set breakpoints in a file. ``lines`` are 0-based; DAP is 1-based."""
        body = await self.request("setBreakpoints", {
            "source": {"path": file_path},
            "breakpoints": [{"line": line + 1} for line in lines],
        })
        return [{
            "verified": bp.get("verified", False),
            "line": _to_zero_based(bp.get("line")),
            "id": bp.get("id"),
        } for bp in (body or {}).get("breakpoints", [])]

    async def configuration_done(self) -> None:
        await self.request("configurationDone", {})

    async def launch(self, project_root: str, scene: str | None = None,
                     timeout: float = 60.0, **extra: Any) -> dict:
        """Run the project and start collecting its output.

        The handshake order is load-bearing and not obvious: Godot does not answer
        ``launch`` until ``configurationDone`` has arrived, so sending
        ``configurationDone`` first — which reads as the natural setup order, and is
        what the DAP overview diagram suggests — deadlocks. Both requests therefore go
        out before either response is collected.

        Verified against Godot 4.7.1: this returns, the game starts, and its stdout
        arrives as output events.
        """
        arguments: dict = {"project": project_root, "request": "launch", "type": "godot"}
        if scene:
            arguments["scene"] = scene
        arguments.update(extra)

        async with self._lock:
            self.output.clear()
            self.events.clear()
            self.terminated = False
            self.stopped_state = None

            self.seq += 1
            launch_seq = self.seq
            self.seq += 1
            config_seq = self.seq

            try:
                await self._send({"seq": launch_seq, "type": "request",
                                  "command": "launch", "arguments": arguments})
                await self._send({"seq": config_seq, "type": "request",
                                  "command": "configurationDone", "arguments": {}})

                while True:
                    message = await self._read_message(timeout)
                    if message.get("type") == "event":
                        self._record_event(message)
                        continue
                    if message.get("type") != "response":
                        continue
                    if message.get("request_seq") == launch_seq:
                        if not message.get("success", False):
                            raise DAPError(message.get("message") or "launch failed")
                        return message.get("body") or {}
            except asyncio.TimeoutError as exc:
                raise DAPTimeout(
                    f"Godot did not start the game within {timeout}s. Check that the "
                    "editor is open on this project and the main scene is set."
                ) from exc
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                raise DAPConnectionLost(f"Debug adapter connection lost: {exc}") from exc

    async def collect_output(self, seconds: float, until_exit: bool = True) -> list[dict]:
        """Gather output for a window, stopping early once the game exits."""
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            await self.drain_events(0.5)
            if until_exit and self.terminated:
                break
        return self.take_output()

    async def threads(self) -> list[dict]:
        body = await self.request("threads", {})
        return (body or {}).get("threads", [])

    async def stack_trace(self, thread_id: int = 1) -> list[dict]:
        body = await self.request("stackTrace", {"threadId": thread_id})
        return [{
            "id": frame.get("id"),
            "name": frame.get("name"),
            "file": (frame.get("source") or {}).get("path"),
            "line": _to_zero_based(frame.get("line")),
        } for frame in (body or {}).get("stackFrames", [])]

    async def scopes(self, frame_id: int) -> list[dict]:
        body = await self.request("scopes", {"frameId": frame_id})
        return [{
            "name": scope.get("name"),
            "variables_reference": scope.get("variablesReference"),
            "expensive": scope.get("expensive", False),
        } for scope in (body or {}).get("scopes", [])]

    async def variables(self, reference: int) -> list[dict]:
        body = await self.request("variables", {"variablesReference": reference})
        return [{
            "name": var.get("name"),
            "value": var.get("value"),
            "type": var.get("type"),
            "variables_reference": var.get("variablesReference", 0),
        } for var in (body or {}).get("variables", [])]

    async def evaluate(self, expression: str, frame_id: int | None = None) -> dict:
        args: dict = {"expression": expression, "context": "repl"}
        if frame_id is not None:
            args["frameId"] = frame_id
        body = await self.request("evaluate", args)
        return {
            "result": (body or {}).get("result"),
            "type": (body or {}).get("type"),
            "variables_reference": (body or {}).get("variablesReference", 0),
        }

    async def continue_execution(self, thread_id: int = 1) -> None:
        await self.request("continue", {"threadId": thread_id})
        self.stopped_state = None

    async def pause(self, thread_id: int = 1) -> None:
        await self.request("pause", {"threadId": thread_id})

    async def step_over(self, thread_id: int = 1) -> None:
        await self.request("next", {"threadId": thread_id})

    async def terminate(self) -> None:
        await self.request("terminate", {})
        self.terminated = True

    def take_output(self, clear: bool = True) -> list[dict]:
        """Return captured output; by default drain it so the next call is fresh."""
        captured = list(self.output)
        if clear:
            self.output.clear()
        return captured


def _to_zero_based(line: Any) -> Any:
    """DAP reports 1-based lines; every GodotLens tool is 0-based."""
    if isinstance(line, int):
        return max(line - 1, 0)
    return line
