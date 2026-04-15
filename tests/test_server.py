"""Tests for the MCP server — tool dispatch with mocked LSP client."""

import io
import json
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from godotlens_mcp.server import TOOLS, handle_request, handle_tool_call, read_message, write_message

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
    assert result["protocolVersion"] == "2025-03-26"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "godotlens-mcp"


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
async def test_definition_null_result():
    lsp = _mock_lsp()
    lsp.request = AsyncMock(return_value=None)
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 0, "character": 0})
    assert result["content"][0]["text"] == "No results"


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
async def test_error_disconnects_and_hints():
    lsp = _mock_lsp()
    lsp.request = AsyncMock(side_effect=Exception("broken pipe"))
    with patch("godotlens_mcp.server._lsp", lsp):
        result = await handle_tool_call("gdscript_definition", {"file": "main.gd", "line": 0, "character": 0})
    assert result["isError"] is True
    lsp.disconnect.assert_awaited_once()
    data = json.loads(result["content"][0]["text"])
    assert "hint" in data


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
