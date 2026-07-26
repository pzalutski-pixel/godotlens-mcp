"""Tests for the MCP server — tool dispatch with mocked LSP client."""

import io
import json
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from godotlens_mcp.lsp_client import LSPConnectionLost, LSPError
from godotlens_mcp.server import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS,
    ParseFailure,
    handle_request,
    handle_tool_call,
    read_message,
    write_message,
)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def test_list_tools_count():
    assert len(TOOLS) == 15


def test_list_tools_names():
    names = {t["name"] for t in TOOLS}
    expected = {
        "gdscript_status",
        "gdscript_definition",
        "gdscript_declaration",
        "gdscript_references",
        "gdscript_hover",
        "gdscript_symbols",
        "gdscript_signature_help",
        "gdscript_rename",
        "gdscript_sync_file",
        "gdscript_sync_files",
        "gdscript_delete_file",
        "gdscript_symbols_batch",
        "gdscript_definitions_batch",
        "gdscript_references_batch",
        "gdscript_diagnostics",
    }
    assert names == expected


def test_all_tools_have_input_schema():
    for tool in TOOLS:
        assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"
        assert tool["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# MCP request routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_response():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    resp = await handle_request(msg)
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "godotlens-mcp"
    assert "ZERO-BASED" in result["instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", SUPPORTED_PROTOCOL_VERSIONS)
async def test_initialize_echoes_supported_protocol_version(requested):
    """Spec: reply with the client's version when we support it."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": requested}}
    resp = await handle_request(msg)
    assert resp["result"]["protocolVersion"] == requested


@pytest.mark.asyncio
async def test_initialize_offers_latest_for_unknown_protocol_version():
    """Spec: reply with a version we do support, not the one we were handed."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "1999-01-01"}}
    resp = await handle_request(msg)
    assert resp["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_ping_response():
    msg = {"jsonrpc": "2.0", "id": 42, "method": "ping", "params": {}}
    resp = await handle_request(msg)
    assert resp["id"] == 42
    assert resp["result"] == {}


@pytest.mark.asyncio
async def test_tools_list_response():
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = await handle_request(msg)
    tools = resp["result"]["tools"]
    assert len(tools) == 15
    assert tools[0]["name"] == "gdscript_status"


@pytest.mark.asyncio
async def test_notification_returns_none():
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    resp = await handle_request(msg)
    assert resp is None


@pytest.mark.asyncio
async def test_unknown_method():
    msg = {"jsonrpc": "2.0", "id": 3, "method": "unknown/method", "params": {}}
    resp = await handle_request(msg)
    assert "error" in resp
    assert resp["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _mock_lsp():
    """Create a mock LSPClient."""
    lsp = AsyncMock()
    lsp.host = "127.0.0.1"
    lsp.port = 6005
    lsp.connect = AsyncMock(return_value=(True, "Connected to Godot LSP"))
    lsp.disconnect = AsyncMock()
    lsp.request = AsyncMock(return_value=None)
    lsp.notify = AsyncMock()
    lsp.drain_notifications = AsyncMock()
    lsp.diagnostics_cache = {}
    return lsp


@pytest.mark.asyncio
async def test_status_connected():
    lsp = _mock_lsp()
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_status", {})
    data = json.loads(result["content"][0]["text"])
    assert data["status"] == "connected"


@pytest.mark.asyncio
async def test_status_disconnected():
    lsp = _mock_lsp()
    lsp.connect = AsyncMock(return_value=(False, "Connection refused"))
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_status", {})
    data = json.loads(result["content"][0]["text"])
    assert data["status"] == "disconnected"


@pytest.mark.asyncio
async def test_tool_requires_connection():
    lsp = _mock_lsp()
    lsp.connect = AsyncMock(return_value=(False, "Connection refused"))
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 0, "character": 0})
    assert result["isError"] is True
    data = json.loads(result["content"][0]["text"])
    assert "error" in data


@pytest.mark.asyncio
async def test_definition_returns_compact():
    lsp = _mock_lsp()
    lsp.request = AsyncMock(return_value={
        "uri": "file:///C:/project/player.gd",
        "range": {"start": {"line": 10, "character": 0}, "end": {"line": 10, "character": 5}},
    })
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 5, "character": 3})
    data = json.loads(result["content"][0]["text"])
    assert data[0]["file"] == "C:/project/player.gd"
    assert data[0]["line"] == 10


@pytest.mark.asyncio
async def test_definition_null_result_is_structured_not_a_sentinel_string():
    """A null/empty result must stay machine-readable.

    Regression: every falsy result rendered as the literal string "No results", so an
    agent could not tell "zero references" from "the call failed" or "your coordinates
    landed on whitespace".
    """
    lsp = _mock_lsp()
    lsp.request = AsyncMock(return_value=None)
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 0, "character": 0})
    assert result["content"][0]["text"] != "No results"
    assert json.loads(result["content"][0]["text"]) is None
    assert not result.get("isError")


@pytest.mark.asyncio
async def test_empty_list_result_is_distinguishable_from_failure():
    lsp = _mock_lsp()
    lsp.request = AsyncMock(return_value=[])
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_references", {"file": "main.gd", "line": 0, "character": 0})
    assert json.loads(result["content"][0]["text"]) == []
    assert not result.get("isError")


@pytest.mark.asyncio
async def test_hover_extracts_value():
    lsp = _mock_lsp()
    lsp.request = AsyncMock(return_value={
        "contents": {"kind": "markdown", "value": "func _ready() -> void"}
    })
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_hover", {"file": "main.gd", "line": 3, "character": 5})
    text = json.loads(result["content"][0]["text"])
    assert text == "func _ready() -> void"


@pytest.mark.asyncio
async def test_unknown_tool():
    lsp = _mock_lsp()
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("nonexistent_tool", {})
    assert result["isError"] is True
    data = json.loads(result["content"][0]["text"])
    assert "Unknown tool" in data["error"]


@pytest.mark.asyncio
async def test_transport_failure_disconnects_and_hints():
    """A genuine transport failure should drop the socket so the next call reconnects."""
    lsp = _mock_lsp()
    lsp.request = AsyncMock(side_effect=LSPConnectionLost("broken pipe"))
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 0, "character": 0})
    assert result["isError"] is True
    lsp.disconnect.assert_awaited_once()
    data = json.loads(result["content"][0]["text"])
    assert data["kind"] == "connection_lost"
    assert "hint" in data


@pytest.mark.asyncio
async def test_lsp_error_does_not_tear_down_a_healthy_connection():
    """An LSP-level error means the connection is fine — keep it.

    Regression: any exception triggered disconnect(), so a -32601 for an unsupported
    method (or a missing-file error) forced a full reconnect and re-initialize.
    """
    lsp = _mock_lsp()
    lsp.request = AsyncMock(side_effect=LSPError("Method not found: x", code=-32601))
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 0, "character": 0})
    assert result["isError"] is True
    lsp.disconnect.assert_not_awaited()
    data = json.loads(result["content"][0]["text"])
    assert data["kind"] == "unsupported_method"
    assert data["code"] == -32601


@pytest.mark.asyncio
async def test_missing_file_does_not_disconnect():
    lsp = _mock_lsp()
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_sync_file", {"file": "does_not_exist_xyz.gd"})
    assert result["isError"] is True
    lsp.disconnect.assert_not_awaited()
    assert json.loads(result["content"][0]["text"])["kind"] == "file_error"


@pytest.mark.asyncio
async def test_batch_symbols_per_file_errors():
    lsp = _mock_lsp()
    call_count = 0

    async def mock_request(method, params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [{"name": "func_a", "kind": 12, "range": {"start": {"line": 0, "character": 0}}}]
        raise Exception("file not found")

    lsp.request = mock_request
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_symbols_batch", {"files": ["good.gd", "bad.gd"]})
    data = json.loads(result["content"][0]["text"])
    assert len(data["good.gd"]) == 1
    assert "error" in data["bad.gd"]


# ---------------------------------------------------------------------------
# MCP stdio protocol — read/write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_message_parses_json_line():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    line = json.dumps(msg) + "\n"
    fake_buffer = io.BytesIO(line.encode("utf-8"))
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdin.buffer = fake_buffer
        result = await read_message()
    assert result == msg


@pytest.mark.asyncio
async def test_read_message_returns_none_on_eof():
    fake_buffer = io.BytesIO(b"")
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdin.buffer = fake_buffer
        result = await read_message()
    assert result is None


@pytest.mark.asyncio
async def test_blank_line_is_skipped_not_treated_as_eof():
    """Regression: a stray newline terminated the server, dropping every later request."""
    data = b'\n\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdin.buffer = io.BytesIO(data)
        result = await read_message()
    assert result == {"jsonrpc": "2.0", "id": 7, "method": "ping"}


@pytest.mark.asyncio
async def test_malformed_json_yields_parse_error_not_a_crash():
    """Regression: json.loads raised out of the loop and killed the process."""
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdin.buffer = io.BytesIO(b"NOT JSON\n")
        result = await read_message()
    assert isinstance(result, ParseFailure)
    assert result.code == -32700


@pytest.mark.asyncio
async def test_json_array_is_rejected_not_a_crash():
    """Regression: msg.get() on a list raised AttributeError and killed the process.

    JSON-RPC batching was removed in MCP 2025-06-18, so an array is simply invalid.
    """
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdin.buffer = io.BytesIO(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n')
        result = await read_message()
    assert isinstance(result, ParseFailure)
    assert result.code == -32600
    assert "batching" in result.message


@pytest.mark.asyncio
async def test_non_object_scalar_is_rejected():
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdin.buffer = io.BytesIO(b'"just a string"\n')
        result = await read_message()
    assert isinstance(result, ParseFailure)
    assert result.code == -32600


def test_write_message_outputs_newline_delimited_json():
    buf = io.BytesIO()
    fake_stdout = type("FakeStdout", (), {"buffer": buf, "flush": lambda self: None})()
    with patch("godotlens_mcp.server.sys") as mock_sys:
        mock_sys.stdout = fake_stdout
        msg = {"jsonrpc": "2.0", "id": 1, "result": {}}
        write_message(msg)
    output = buf.getvalue().decode("utf-8")
    assert output.endswith("\n")
    parsed = json.loads(output.strip())
    assert parsed == msg


# ---------------------------------------------------------------------------
# Integration test — full stdio round-trip
# ---------------------------------------------------------------------------

def test_stdio_integration_initialize_and_tools_list():
    """Send initialize + tools/list through the actual server process and verify responses."""
    init_msg = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test"}}
    })
    initialized_msg = json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized"
    })
    tools_msg = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    })
    stdin_data = (init_msg + "\n" + initialized_msg + "\n" + tools_msg + "\n").encode("utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "godotlens_mcp"],
        input=stdin_data,
        capture_output=True,
        timeout=10,
    )

    stdout = result.stdout.decode("utf-8").strip()
    lines = [line for line in stdout.split("\n") if line.strip()]
    assert len(lines) == 2, f"Expected 2 responses, got {len(lines)}: {lines}"

    init_response = json.loads(lines[0])
    assert init_response["id"] == 1
    assert init_response["result"]["serverInfo"]["name"] == "godotlens-mcp"
    assert "tools" in init_response["result"]["capabilities"]

    tools_response = json.loads(lines[1])
    assert tools_response["id"] == 2
    assert len(tools_response["result"]["tools"]) == 15
