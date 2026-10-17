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

The socket is read continuously by a background task, never only inside a call.
Godot's adapter writes from the editor's main thread with a loop that spins until the
socket accepts every byte (``DAPeer::send_data``), so a client that stops reading
while a game prints freezes the whole editor, language server included. Reading only
inside calls also meant "wait until the stream goes quiet" never returned while a
game printed faster than the wait window.

Line numbering: DAP is 1-based by default. Every GodotLens tool is 0-based, matching
the LSP, so conversion happens explicitly at this boundary rather than relying on the
``linesStartAt1`` negotiation flag being honoured.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from godotlens_mcp.debugger_messages import decode_error

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
        # Structured errors (push_error, engine errors, script errors), decoded from
        # godot/custom_data. Godot never turns these into output events.
        self.errors: list[dict] = []
        self.stopped_state: dict | None = None
        self.terminated = False
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._event_count = 0
        # True between sending launch and receiving its response. Godot never sends
        # terminated to the launching client during that window, so one seen then is
        # a leftover from an earlier session and must not mark the new game finished.
        self._awaiting_launch = False
        # Expression -> how many times Godot left it unanswered; see evaluate().
        self._unanswered: dict[str, int] = {}

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

        self._ensure_reader()
        try:
            await self._initialize()
        except (DAPTimeout, DAPError, DAPConnectionLost) as exc:
            await self.disconnect()
            return False, str(exc)
        return True, "Connected to Godot debug adapter"

    async def disconnect(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        self._fail_pending(DAPConnectionLost("Disconnected from Godot's debug adapter"))
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
        self.initialized = True

    # -- transport ---------------------------------------------------------

    def _ensure_reader(self) -> None:
        """Start the background reader if a stream is attached and none is running."""
        if self.reader is not None and (self._reader_task is None or self._reader_task.done()):
            self._reader_task = asyncio.get_event_loop().create_task(self._reader_loop())

    async def _send(self, payload: dict) -> None:
        if self.writer is None:
            raise DAPConnectionLost("Not connected to Godot's debug adapter")
        body = json.dumps(payload).encode("utf-8")
        async with self._write_lock:
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

    async def _reader_loop(self) -> None:
        """Read every frame for the life of the connection and route it."""
        try:
            while True:
                try:
                    message = await self._read_message(None)
                except json.JSONDecodeError:
                    continue  # the frame was consumed whole; the stream is still aligned
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (DAPConnectionLost, asyncio.IncompleteReadError, ConnectionError, OSError,
                ValueError) as exc:
            self._fail_pending(DAPConnectionLost(f"Debug adapter connection lost: {exc}"))
            if self.writer is not None:
                self.writer.close()

    def _dispatch(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "event":
            self._record_event(message)
        elif kind == "response":
            future = self._pending.pop(message.get("request_seq"), None)
            if future is not None and not future.done():
                future.set_result(message)
            # Otherwise a stale response to a request we already gave up on.

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _record_event(self, message: dict) -> None:
        event = message.get("event")
        body = message.get("body") or {}
        self._event_count += 1

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

        if event == "godot/custom_data":
            # Every debugger message from the game arrives here once launch opted in,
            # including a performance frame every second. Keep only errors.
            if body.get("message") == "error":
                decoded = decode_error(body.get("data"))
                if decoded is not None:
                    self.errors.append(decoded)
                    if len(self.errors) > MAX_EVENTS:
                        del self.errors[:-MAX_EVENTS]
            return

        if event == "stopped":
            self.stopped_state = {
                "reason": body.get("reason"),
                "thread_id": body.get("threadId"),
                "description": body.get("description"),
                "text": body.get("text"),
            }
        elif event == "continued":
            self.stopped_state = None
        elif event in {"terminated", "exited"} and not self._awaiting_launch:
            self.terminated = True

        self.events.append({"event": event, "body": body})
        if len(self.events) > MAX_EVENTS:
            del self.events[:-MAX_EVENTS]

    async def request(self, command: str, arguments: dict | None = None,
                      timeout: float | None = None) -> Any:
        """Send a DAP request and return its body."""
        deadline = timeout if timeout is not None else self.timeout
        if self.writer is None:
            raise DAPConnectionLost("Not connected to Godot's debug adapter")
        self.seq += 1
        expect = self.seq
        future = asyncio.get_event_loop().create_future()
        self._pending[expect] = future
        self._ensure_reader()
        try:
            await self._send({"seq": expect, "type": "request", "command": command,
                              "arguments": arguments or {}})
            message = await asyncio.wait_for(future, deadline)
        except asyncio.TimeoutError as exc:
            raise DAPTimeout(
                f"Godot's debug adapter did not answer '{command}' within {deadline}s."
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise DAPConnectionLost(f"Debug adapter connection lost: {exc}") from exc
        finally:
            self._pending.pop(expect, None)

        if not message.get("success", False):
            raise DAPError(message.get("message") or f"{command} failed")
        return message.get("body")

    async def drain_events(self, window: float = 0.3) -> int:
        """Give in-flight events up to ``window`` seconds to arrive. Returns how many did.

        Always returns within ``window``: the background reader records events as they
        come, so this only waits, it never reads.
        """
        before = self._event_count
        if self.writer is not None and window > 0:
            await asyncio.sleep(window)
        return self._event_count - before

    async def wait_for_event(self, names: set[str], timeout: float) -> dict | None:
        """Wait up to ``timeout`` for an event named in ``names`` to arrive after now."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        start = len(self.events)
        while loop.time() < deadline:
            for event in self.events[start:]:
                if event["event"] in names:
                    return event
            start = len(self.events)
            await asyncio.sleep(0.05)
        return None

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
                     timeout: float = 60.0, play_args: list[str] | None = None,
                     **extra: Any) -> dict:
        """Run the project and start collecting its output.

        The handshake order is load-bearing and not obvious: Godot does not answer
        ``launch`` until ``configurationDone`` has arrived, so sending
        ``configurationDone`` first — which reads as the natural setup order, and is
        what the DAP overview diagram suggests — deadlocks. Both requests therefore go
        out before either response is collected.

        ``godot/custom_data`` opts this client in to every debugger message the game
        sends; only ``launch`` can opt in, ``attach`` cannot. ``play_args`` are
        appended verbatim to the game's command line.

        Verified against Godot 4.7.1: this returns, the game starts, and its stdout
        arrives as output events.
        """
        arguments: dict = {"project": project_root, "request": "launch", "type": "godot",
                           "godot/custom_data": True}
        if scene:
            arguments["scene"] = scene
        if play_args:
            arguments["playArgs"] = list(play_args)
        arguments.update(extra)

        if self.writer is None:
            raise DAPConnectionLost("Not connected to Godot's debug adapter")
        self.output.clear()
        self.events.clear()
        self.errors.clear()
        self.terminated = False
        self.stopped_state = None

        self.seq += 1
        launch_seq = self.seq
        self.seq += 1
        config_seq = self.seq
        loop = asyncio.get_event_loop()
        launch_future = loop.create_future()
        config_future = loop.create_future()
        self._pending[launch_seq] = launch_future
        self._pending[config_seq] = config_future
        self._ensure_reader()

        self._awaiting_launch = True
        try:
            await self._send({"seq": launch_seq, "type": "request",
                              "command": "launch", "arguments": arguments})
            await self._send({"seq": config_seq, "type": "request",
                              "command": "configurationDone", "arguments": {}})
            message = await asyncio.wait_for(launch_future, timeout)
        except asyncio.TimeoutError as exc:
            raise DAPTimeout(
                f"Godot did not start the game within {timeout}s. Check that the "
                "editor is open on this project and the main scene is set."
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise DAPConnectionLost(f"Debug adapter connection lost: {exc}") from exc
        finally:
            self._awaiting_launch = False
            self._pending.pop(launch_seq, None)
            self._pending.pop(config_seq, None)

        if not message.get("success", False):
            raise DAPError(message.get("message") or "launch failed")
        return message.get("body") or {}

    async def collect_output(self, seconds: float, until_exit: bool = True) -> list[dict]:
        """Gather output for a window, stopping early once the game exits."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            if until_exit and self.terminated:
                break
            await asyncio.sleep(min(0.25, max(deadline - loop.time(), 0)))
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

    async def evaluate(self, expression: str, frame_id: int | None = None,
                       timeout: float | None = None) -> dict:
        """Evaluate at a paused frame.

        Godot's adapter keys pending evaluations by expression text and forgets one
        only when the game answers (``eval_pending_list``), so after one unanswered
        evaluate the same text is never forwarded again for the editor's lifetime.
        A failed expression is therefore resent wrapped in extra parentheses, which
        changes the text but not its meaning.
        """
        depth = self._unanswered.get(expression, 0)
        text = "(" * depth + expression + ")" * depth
        args: dict = {"expression": text, "context": "repl"}
        if frame_id is not None:
            args["frameId"] = frame_id
        try:
            body = await self.request("evaluate", args, timeout=timeout)
        except (DAPTimeout, DAPError):
            self._unanswered[expression] = depth + 1
            raise
        self._unanswered.pop(expression, None)
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

    def take_errors(self, clear: bool = True) -> list[dict]:
        """Return captured structured errors; by default drain them."""
        captured = list(self.errors)
        if clear:
            self.errors.clear()
        return captured


def _to_zero_based(line: Any) -> Any:
    """DAP reports 1-based lines; every GodotLens tool is 0-based."""
    if isinstance(line, int):
        return max(line - 1, 0)
    return line
