"""One game launched by GodotLens and connected back to it as its debugger.

The order is load-bearing: listen first, then spawn the game with
``--remote-debug tcp://127.0.0.1:<port>``. The game tries to connect at startup
(``RemoteDebuggerPeerTCP``, about 3 s of retries) and runs undebugged if nothing
answers. This is what the editor does internally on port 6007; no editor is needed,
and each game gets its own port, so several can run at once.

Nothing here may leave a game blocked waiting for a debugger:

- ``--ignore-error-breaks``: a script error, including a parse error on hot reload,
  would otherwise stop the game in its debug loop until a debugger resumes it.
- ``set_skip_breakpoints`` on connect: the same for a ``breakpoint`` statement.
- Both console pipes are drained continuously; an unread pipe fills and blocks the
  game on its next print.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import struct
import sys
import time
from collections import deque
from pathlib import Path

from godotlens_mcp.debugger_messages import decode_error
from godotlens_mcp.runtime import variant
from godotlens_mcp.scene import private_log

HELPER_SCRIPT = Path(__file__).with_name("helper.gd")
MAX_BUFFERED = 2000
# What a screenshot run asks for. Godot keeps at least a third of a window on screen
# whatever position is requested (DisplayServerWindows), so a far-off position leaves
# a small sliver in a corner. Measured on Windows: it did not take focus.
OFFSCREEN_POSITION = "100000,100000"
DEFAULT_RESOLUTION = "640x360"
# How many performance frames (about one per second) to keep.
MONITOR_HISTORY = 60


class SessionError(Exception):
    """The game could not be started, or stopped answering."""

    def __init__(self, message: str, kind: str = "runtime_error", detail: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


class RuntimeSession:
    """A running game and the debugger connection it opened to GodotLens."""

    def __init__(self, session_id: str, binary: str, project_root: str, *,
                 scene: str | None = None, windowed: bool = False,
                 resolution: str | None = None, args: list[str] | None = None,
                 helper: bool = True):
        self.session_id = session_id
        self.binary = binary
        self.project_root = project_root
        self.scene = scene
        self.windowed = windowed
        self.resolution = resolution or DEFAULT_RESOLUTION
        self.args = list(args or [])
        # The game's log goes to a file of its own, not the project's rotated user://logs.
        self.log_path = None if "--log-file" in self.args else private_log(f"game-{session_id}")
        # Run through helper.gd, which answers lens:* (evaluate, input) messages.
        self.helper = helper
        self.helper_version: int | None = None
        self.helper_ready = asyncio.Event()
        self.process: asyncio.subprocess.Process | None = None
        self.started_at = time.time()
        self.game_pid: int | None = None
        self.paused = False  # as runtime_pause last set it
        self._server: asyncio.AbstractServer | None = None
        self._connected = asyncio.Event()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._waiters: list[tuple[set[str], asyncio.Future]] = []
        self.output: deque[dict] = deque(maxlen=MAX_BUFFERED)
        self.errors: deque[dict] = deque(maxlen=MAX_BUFFERED)
        self.console: deque[str] = deque(maxlen=200)  # raw stdout/stderr, for diagnosis
        self.monitor_names: list[str] = []
        self.custom_monitor_names: list[str] = []
        self.monitors: deque[tuple[float, list]] = deque(maxlen=MONITOR_HISTORY)
        self.notes: deque[str] = deque(maxlen=50)

    # -- lifecycle ---------------------------------------------------------

    def command(self, port: int) -> list[str]:
        display = (["--position", OFFSCREEN_POSITION, "--resolution", self.resolution,
                    "--audio-driver", "Dummy"]
                   if self.windowed else ["--headless"])
        logging = ["--log-file", str(self.log_path)] if self.log_path else []
        base = [self.binary, "--path", self.project_root, *logging,
                "--remote-debug", f"tcp://127.0.0.1:{port}", "--ignore-error-breaks", *display]
        if not self.helper:
            return [*base, *([self.scene] if self.scene else []), *self.args]
        # The helper is the main loop and opens the scene itself; what it needs comes
        # after "--", where the engine leaves it for OS.get_cmdline_user_args().
        user = [f"godotlens_scene={self.scene}"] if self.scene else []
        if self.windowed:
            user.append("godotlens_offscreen")
        return [*base, "--script", str(HELPER_SCRIPT), *self.args, "--", *user]

    async def start(self, timeout: float = 30.0) -> None:
        self._server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console pops up
        else:
            kwargs["start_new_session"] = True  # a process group to stop as one
        self.process = await asyncio.create_subprocess_exec(
            *self.command(port), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)
        _track(self.process)
        self._tasks.append(asyncio.get_event_loop().create_task(self._drain_console()))

        exited = asyncio.get_event_loop().create_task(self.process.wait())
        connected = asyncio.get_event_loop().create_task(self._connected.wait())
        done, _ = await asyncio.wait({exited, connected}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        connected.cancel()
        exited.cancel()
        if self._connected.is_set():
            return
        detail = "\n".join(self.console)[-2000:]
        await self.stop()
        if exited in done:
            raise SessionError("The game exited before connecting.", "game_exited", detail)
        raise SessionError(
            f"The game did not connect within {timeout}s. A cold project import can be "
            "slow; retry once the project has been imported.", "connect_timeout", detail)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self._connected.is_set():
            writer.close()  # one game per session
            return
        self._reader, self._writer = reader, writer
        self._connected.set()
        # A breakpoint statement would otherwise stop the game until a debugger resumed it.
        await self.put("set_skip_breakpoints", [True])
        self._tasks.append(asyncio.get_event_loop().create_task(self._read_messages()))

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def stop(self) -> int | None:
        """Stop the game and release everything this session holds."""
        if self.process is not None and self.process.returncode is None:
            _terminate(self.process)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                _terminate(self.process, force=True)
                await self.process.wait()
        for task in self._tasks:
            task.cancel()
        for _, future in self._waiters:
            if not future.done():
                future.set_exception(SessionError("The game stopped.", "game_exited"))
        self._waiters.clear()
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
        if self.log_path is not None:
            await asyncio.to_thread(self.log_path.unlink, missing_ok=True)
        return self.process.returncode if self.process else None

    # -- I/O ---------------------------------------------------------------

    async def _drain_console(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                return
            self.console.append(line.decode("utf-8", "replace").rstrip())

    async def _read_messages(self) -> None:
        try:
            while True:
                (size,) = struct.unpack("<I", await self._reader.readexactly(4))
                payload = await self._reader.readexactly(size)
                try:
                    message, _ = variant.decode(payload)
                except variant.DecodeError:
                    continue  # the frame was length-prefixed; the stream is still aligned
                if isinstance(message, list) and len(message) == 3 and isinstance(message[0], str):
                    self._dispatch(message[0], message[2])
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            for _, future in self._waiters:
                if not future.done():
                    future.set_exception(SessionError("The game disconnected.", "game_exited"))
            self._waiters.clear()

    def _dispatch(self, name: str, data: list) -> None:
        if name == "output" and len(data) >= 2:
            # [strings, types]; consecutive log lines arrive joined by newlines.
            for text, kind in zip(_items(data[0]), _items(data[1]), strict=False):
                label = {0: "log", 1: "error", 2: "log_rich"}.get(kind, "log")
                for line in str(text).split("\n"):
                    self.output.append({"text": line, "type": label})
        elif name == "error":
            decoded = decode_error(list(data))
            if decoded is not None:
                self.errors.append(decoded)
        elif name == "performance:profile_frame":
            self.monitors.append((time.time(), list(data)))
        elif name == "performance:profile_names":
            self.custom_monitor_names = [str(n) for n in (data[0] if data else [])]
        elif name == "lens:ready" and self.helper:
            self.helper_version = int(data[0]) if data else 0
            self.helper_ready.set()
        elif name == "set_pid":
            self.game_pid = int(data[0]) if data else None
        elif name == "debug_enter":
            # Should not happen with breakpoints skipped and error breaks ignored; never
            # leave the game waiting on a debugger GodotLens does not act as.
            self.notes.append("The game entered its debug loop; GodotLens resumed it.")
            asyncio.get_event_loop().create_task(self.put("continue", []))

        for index, (names, future) in enumerate(self._waiters):
            if name in names and not future.done():
                future.set_result((name, data))
                del self._waiters[index]
                break

    async def put(self, name: str, data: list | tuple = ()) -> None:
        if not self.connected:
            raise SessionError("The game is not connected.", "game_exited")
        async with self._write_lock:
            self._writer.write(variant.message(name, data))
            await self._writer.drain()

    async def request(self, name: str, data: list | tuple, replies: set[str],
                      timeout: float = 10.0) -> tuple[str, list]:
        """Send a message and wait for the first reply whose name is in ``replies``."""
        future = asyncio.get_event_loop().create_future()
        self._waiters.append((replies, future))
        try:
            await self.put(name, data)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise SessionError(f"The game did not answer {name} within {timeout}s.",
                               "no_reply") from None
        finally:
            self._waiters = [(n, f) for n, f in self._waiters if f is not future]

    # -- buffers -----------------------------------------------------------

    def take_output(self, clear: bool = True) -> list[dict]:
        lines = list(self.output)
        if clear:
            self.output.clear()
        return lines

    def take_errors(self, clear: bool = True) -> list[dict]:
        errors = list(self.errors)
        if clear:
            self.errors.clear()
        return errors

    def describe(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.process.pid if self.process else None,
            # The game's own pid; differs from pid when a launcher (Godot's Windows
            # console wrapper) started it.
            "game_pid": self.game_pid,
            "scene": self.scene or "main scene",
            "headless": not self.windowed,
            "helper": self.helper_version is not None,
            "running": self.running,
            "connected": self.connected,
            "exit_code": None if self.running or not self.process else self.process.returncode,
            "uptime_s": round(time.time() - self.started_at, 1),
        }


# ---------------------------------------------------------------------------
# Making sure games die with GodotLens
# ---------------------------------------------------------------------------

_tracked: list[asyncio.subprocess.Process] = []
_job_handle = None


def _track(process: asyncio.subprocess.Process) -> None:
    """Tie the game's lifetime to this process.

    On Windows the game joins a job object that kills its members when GodotLens's
    handle to it closes - which the OS does however GodotLens exits. Godot's console
    wrapper puts the real game in a kill-on-close job of its own, so ending the
    wrapper ends the game too. Elsewhere the game leads its own process group, which
    is stopped at exit; a GodotLens killed outright cannot stop it there.
    """
    _tracked.append(process)
    if sys.platform == "win32":
        _assign_to_job(process.pid)


def _terminate(process: asyncio.subprocess.Process, force: bool = False) -> None:
    try:
        if sys.platform == "win32":
            process.kill()  # TerminateProcess; the wrapper's job takes the game with it
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@atexit.register
def _stop_tracked_games() -> None:
    for process in _tracked:
        if process.returncode is None:
            _terminate(process, force=True)


def _assign_to_job(pid: int) -> None:
    global _job_handle
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    if _job_handle is None:
        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(job)
            return
        _job_handle = job  # deliberately never closed: the OS closes it when we exit

    handle = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
    if handle:
        kernel32.AssignProcessToJobObject(_job_handle, handle)
        kernel32.CloseHandle(handle)


async def import_project(binary: str, project_root: str, timeout: float = 600.0) -> None:
    """Run the editor's import once, as a tracked process that cannot outlive us."""
    kwargs: dict = {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
    log = private_log("import")
    process = await asyncio.create_subprocess_exec(
        binary, "--headless", "--log-file", str(log), "--path", project_root, "--import",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, **kwargs)
    _track(process)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _terminate(process, force=True)
        raise
    finally:
        await asyncio.to_thread(log.unlink, missing_ok=True)


def _items(value) -> list:
    """Elements of an Array or a packed array alike."""
    return list(value.items) if isinstance(value, variant.Packed) else list(value or [])


def project_is_imported(project_root: str) -> bool:
    """A game reads its imports and class cache from .godot/, which an import writes."""
    return (Path(project_root) / ".godot").is_dir()
