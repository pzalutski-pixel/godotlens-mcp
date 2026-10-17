"""Decoders for messages a running game sends over Godot's debugger link.

The same arrays reach GodotLens two ways: wrapped in ``godot/custom_data`` events by
the editor's debug adapter, and directly over the binary protocol when GodotLens
launches the game itself. Both paths decode through here so they cannot disagree.
"""

from __future__ import annotations

from typing import Any


def _int(value: Any, default: int = 0) -> int:
    """JSON gives Godot ints back as int or float; the binary codec as int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def decode_error(data: Any) -> dict | None:
    """Decode an ``error`` message (``DebuggerMarshalls::OutputError::serialize``).

    Layout: ``[hr, min, sec, msec, source_file, source_func, source_line, error,
    error_descr, warning, callstack_len * 3, (file, func, line)...]``.

    For ``push_error`` and engine errors the source location points into engine C++
    (``core/variant/variant_utility.cpp``), so the reported location is the first
    call-stack entry when there is one: that is the script line that raised it.
    Lines are converted to 0-based, matching every other GodotLens tool.
    """
    if not isinstance(data, list) or len(data) < 11:
        return None

    stack = []
    count = _int(data[10]) // 3
    for i in range(count):
        base = 11 + i * 3
        if base + 2 >= len(data):
            break
        stack.append({
            "file": str(data[base]),
            "function": str(data[base + 1]),
            "line": max(_int(data[base + 2]) - 1, 0),
        })

    source = {
        "file": str(data[4]),
        "function": str(data[5]),
        "line": max(_int(data[6]) - 1, 0),
    }
    location = stack[0] if stack else source
    return {
        "file": location["file"],
        "function": location["function"],
        "line": location["line"],
        "message": str(data[7]),
        "description": str(data[8]),
        "warning": bool(data[9]),
        "stack": stack,
        "engine_source": source,
        "time": f"{_int(data[0])}:{_int(data[1]):02d}:{_int(data[2]):02d}.{_int(data[3]):03d}",
    }
