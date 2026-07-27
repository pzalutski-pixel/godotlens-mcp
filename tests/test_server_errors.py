"""Error handling in tool dispatch.

These run against a mocked client, so every failure branch is reachable without a
Godot install. The rule they enforce: a tool must fail in a way the agent can act
on, and only a genuine transport failure may drop the connection.
"""

import json
from unittest.mock import AsyncMock, create_autospec, patch

import pytest

from godotlens_mcp.capabilities import Capabilities
from godotlens_mcp.dap_client import DAPClient, DAPConnectionLost, DAPError, DAPTimeout
from godotlens_mcp.lsp_client import (
    LSPClient,
    LSPConnectionLost,
    LSPError,
    LSPTimeout,
)
from godotlens_mcp.scene import GodotBinaryNotFound
from godotlens_mcp.server import TOOLS, handle_tool_call

pytestmark = pytest.mark.asyncio

# Tools that reach the LSP with a single (file, line, character) position.
POSITION_TOOLS = [
    "gdscript_definition", "gdscript_references", "gdscript_hover",
    "gdscript_signature_help", "gdscript_complete", "gdscript_references_in_file",
]
POSITION_ARGS = {"file": "player.gd", "line": 1, "character": 1}


def make_lsp(**overrides):
    """A client mock specced against the real class.

    autospec matters: a bare AsyncMock keeps passing after a method is renamed or
    removed, which silently hollows out every test that uses it.
    """
    lsp = create_autospec(LSPClient, instance=True)
    lsp.host = "127.0.0.1"
    lsp.port = 6005
    lsp.connect = AsyncMock(return_value=(True, "Connected"))
    lsp.disconnect = AsyncMock()
    lsp.request = AsyncMock(return_value=None)
    lsp.notify = AsyncMock()
    lsp.drain_notifications = AsyncMock()
    lsp.wait_for_diagnostics = AsyncMock(return_value=set())
    lsp.sync_document = AsyncMock(return_value="opened")
    lsp.close_document = AsyncMock(return_value=True)
    # is_open True keeps _ensure_document_open from touching the filesystem; these
    # tests are about error classification, not file IO.
    lsp.is_open = lambda uri: True
    lsp.diagnostics_cache = {}
    lsp.native_capabilities = {}
    # Set in __init__ rather than on the class, so autospec does not provide them.
    lsp.capabilities = Capabilities({"documentHighlightProvider": True})
    lsp.server_capabilities = {}
    lsp.workspace_mismatch = None
    lsp.server_messages = []
    for key, value in overrides.items():
        setattr(lsp, key, value)
    return lsp


async def call(name, arguments, lsp=None, dap=None):
    lsp = lsp or make_lsp()
    with patch("godotlens_mcp.server._lsp", lsp), patch("godotlens_mcp.server._dap", dap):
        result = await handle_tool_call(name, arguments)
    payload = json.loads(result["content"][0]["text"])
    return payload, bool(result.get("isError")), lsp


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", POSITION_TOOLS)
async def test_transport_failure_disconnects(tool):
    lsp = make_lsp(request=AsyncMock(side_effect=LSPConnectionLost("pipe died")))
    payload, is_error, lsp = await call(tool, POSITION_ARGS, lsp)

    assert is_error
    assert payload["kind"] == "connection_lost"
    lsp.disconnect.assert_awaited()


@pytest.mark.parametrize("tool", POSITION_TOOLS)
async def test_timeout_disconnects_and_is_typed(tool):
    lsp = make_lsp(request=AsyncMock(side_effect=LSPTimeout("too slow")))
    payload, is_error, lsp = await call(tool, POSITION_ARGS, lsp)

    assert is_error
    assert payload["kind"] == "connection_lost"
    lsp.disconnect.assert_awaited()


@pytest.mark.parametrize("tool", POSITION_TOOLS)
async def test_lsp_error_keeps_the_connection(tool):
    """An LSP-level error means the connection is healthy. Dropping it is wasteful."""
    lsp = make_lsp(request=AsyncMock(side_effect=LSPError("Method not found", code=-32601)))
    payload, is_error, lsp = await call(tool, POSITION_ARGS, lsp)

    assert is_error
    assert payload["kind"] == "unsupported_method"
    assert payload["code"] == -32601
    lsp.disconnect.assert_not_awaited()


async def test_non_method_lsp_error_is_labelled_separately():
    lsp = make_lsp(request=AsyncMock(side_effect=LSPError("bad params", code=-32602)))
    payload, is_error, _ = await call("gdscript_definition", POSITION_ARGS, lsp)
    assert is_error
    assert payload["kind"] == "lsp_error"
    assert payload["code"] == -32602


async def test_unsupported_path_is_its_own_category():
    payload, is_error, lsp = await call(
        "gdscript_symbols", {"file": "res://player.gd"})
    assert is_error is True
    assert payload["kind"] == "unsupported_path"
    assert "res://" in payload["error"]
    lsp.disconnect.assert_not_awaited()


async def test_missing_file_is_a_file_error_not_a_transport_error():
    payload, is_error, lsp = await call(
        "gdscript_sync_file", {"file": "definitely_missing_xyz.gd"})
    assert is_error
    assert payload["kind"] == "file_error"
    lsp.disconnect.assert_not_awaited()


async def test_unexpected_exception_is_contained():
    lsp = make_lsp(request=AsyncMock(side_effect=RuntimeError("something odd")))
    payload, is_error, lsp = await call("gdscript_definition", POSITION_ARGS, lsp)

    assert is_error
    assert payload["kind"] == "internal_error"
    lsp.disconnect.assert_not_awaited(), "an internal bug must not drop a healthy socket"


async def test_unknown_tool_is_rejected():
    payload, is_error, _ = await call("gdscript_not_a_tool", {})
    assert is_error
    assert "Unknown tool" in payload["error"]


async def test_disconnected_lsp_short_circuits_every_tool():
    lsp = make_lsp(connect=AsyncMock(return_value=(False, "Connection refused")))
    payload, is_error, _ = await call("gdscript_definition", POSITION_ARGS, lsp)
    assert is_error
    assert payload["status"] == "disconnected"


async def test_status_reports_disconnection_without_erroring():
    """status is the diagnostic tool; it must answer even when nothing is connected."""
    lsp = make_lsp(connect=AsyncMock(return_value=(False, "Connection refused")))
    payload, is_error, _ = await call("gdscript_status", {}, lsp)
    assert not is_error
    assert payload["status"] == "disconnected"


# ---------------------------------------------------------------------------
# Scene tool failures
# ---------------------------------------------------------------------------


async def test_missing_godot_binary_is_actionable():
    with patch("godotlens_mcp.server.dump_scenes",
               AsyncMock(side_effect=GodotBinaryNotFound("no binary; set GODOT_BIN"))):
        payload, is_error, _ = await call("scene_state", {"files": ["main.tscn"]})
    assert is_error
    assert payload["kind"] == "godot_binary_missing"
    assert "GODOT_BIN" in payload["error"]


async def test_scene_dump_timeout_is_its_own_category():
    with patch("godotlens_mcp.server.dump_scenes",
               AsyncMock(side_effect=TimeoutError("cold import took too long"))):
        payload, is_error, _ = await call("scene_validate", {"files": ["main.tscn"]})
    assert is_error
    assert payload["kind"] == "godot_timeout"


async def test_scene_validate_skips_scripts_the_lsp_cannot_parse():
    """A script the LSP rejects must not produce phantom missing-handler reports."""
    scene = {
        "nodes": [{"path": ".", "name": "Main", "type": "Node2D",
                   "script": "res://missing.gd", "properties": {}}],
        "connections": [{"signal": "x", "from": ".", "to": ".", "method": "_on_x"}],
    }
    lsp = make_lsp(request=AsyncMock(side_effect=LSPError("cannot parse", code=-32603)))
    with patch("godotlens_mcp.server.dump_scenes",
               AsyncMock(return_value={"scenes": {"res://main.tscn": scene}, "errors": {}})):
        payload, is_error, _ = await call("scene_validate", {"files": ["main.tscn"]}, lsp)

    assert not is_error, payload
    assert payload["scenes"]["res://main.tscn"]["valid"] is True


# ---------------------------------------------------------------------------
# Debug tool failures
# ---------------------------------------------------------------------------


def make_dap(**overrides):
    dap = create_autospec(DAPClient, instance=True)
    dap.host = "127.0.0.1"
    dap.port = 6006
    dap.connect = AsyncMock(return_value=(True, "Connected"))
    dap.disconnect = AsyncMock()
    dap.drain_events = AsyncMock(return_value=0)
    dap.capabilities = {}
    dap.stopped_state = None
    dap.terminated = False
    dap.output = []
    dap.take_output = lambda clear=True: []
    for key, value in overrides.items():
        setattr(dap, key, value)
    return dap


async def test_debug_tool_without_an_adapter_is_actionable():
    dap = make_dap(connect=AsyncMock(return_value=(False, "Connection refused")))
    payload, is_error, _ = await call("debug_status", {}, dap=dap)
    assert is_error
    assert payload["kind"] == "dap_disconnected"
    assert "Debug Adapter" in payload["hint"]


async def test_dap_error_does_not_drop_the_lsp():
    dap = make_dap(stack_trace=AsyncMock(side_effect=DAPError("no active session")))
    lsp = make_lsp()
    payload, is_error, lsp = await call("debug_stack_trace", {}, lsp, dap)

    assert is_error
    assert payload["kind"] == "dap_error"
    lsp.disconnect.assert_not_awaited(), "a debug failure must not disturb analysis"


async def test_dap_connection_loss_disconnects_only_the_adapter():
    dap = make_dap(threads=AsyncMock(side_effect=DAPConnectionLost("adapter gone")),
                   stack_trace=AsyncMock(side_effect=DAPConnectionLost("adapter gone")))
    lsp = make_lsp()
    payload, is_error, lsp = await call("debug_stack_trace", {}, lsp, dap)

    assert is_error
    assert payload["kind"] == "dap_connection_lost"
    dap.disconnect.assert_awaited()
    lsp.disconnect.assert_not_awaited()


async def test_dap_timeout_is_typed():
    dap = make_dap(stack_trace=AsyncMock(side_effect=DAPTimeout("adapter silent")))
    payload, is_error, _ = await call("debug_stack_trace", {}, dap=dap)
    assert is_error
    assert payload["kind"] == "dap_connection_lost"


async def test_unknown_debug_tool_is_rejected():
    payload, is_error, _ = await call("debug_not_a_tool", {}, dap=make_dap())
    assert is_error
    assert "Unknown tool" in payload["error"]


# ---------------------------------------------------------------------------
# Result shape guarantees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, [], {}, 0, False])
async def test_empty_results_stay_machine_readable(empty):
    """Every falsy result once became the string "No results", which an agent could
    not distinguish from a failure."""
    lsp = make_lsp(request=AsyncMock(return_value=empty))
    payload, is_error, _ = await call("gdscript_definition", POSITION_ARGS, lsp)
    assert not is_error
    assert payload != "No results"


async def test_every_error_payload_carries_a_kind():
    """The agent branches on `kind`, so an error without one is unactionable."""
    cases = [
        (LSPConnectionLost("x"), "connection_lost"),
        (LSPError("x", code=-32601), "unsupported_method"),
        (LSPError("x", code=-1), "lsp_error"),
        (RuntimeError("x"), "internal_error"),
    ]
    for exc, expected in cases:
        lsp = make_lsp(request=AsyncMock(side_effect=exc))
        payload, is_error, _ = await call("gdscript_hover", POSITION_ARGS, lsp)
        assert is_error
        assert payload["kind"] == expected, f"{exc!r} -> {payload}"


async def test_batch_tools_isolate_failures_per_item():
    calls = {"n": 0}

    async def flaky(method, params, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise LSPError("boom", code=-32603)
        return []

    lsp = make_lsp(request=flaky)
    payload, is_error, _ = await call(
        "gdscript_symbols_batch", {"files": ["a.gd", "b.gd", "c.gd"]}, lsp)

    assert not is_error, "one bad file must not fail the whole batch"
    assert len(payload) == 3
    assert any("error" in v for v in payload.values() if isinstance(v, dict))


async def test_every_tool_has_a_dispatch_branch():
    """A tool advertised in tools/list but absent from dispatch is a dead promise."""
    lsp = make_lsp()
    dap = make_dap()
    unroutable = []
    for tool in TOOLS:
        payload, is_error, _ = await call(tool["name"], {}, lsp, dap)
        if is_error and "Unknown tool" in str(payload.get("error", "")):
            unroutable.append(tool["name"])
    assert not unroutable, f"advertised but not dispatched: {unroutable}"
