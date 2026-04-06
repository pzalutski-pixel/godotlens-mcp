"""Tests for the LSP client and utility functions."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from godotlens_mcp.lsp_client import (
    LSPClient,
    compact_location,
    compact_symbol,
    file_uri,
    uri_to_path,
)

# ---------------------------------------------------------------------------
# file_uri
# ---------------------------------------------------------------------------

def test_file_uri_unix_path():
    assert file_uri("/home/user/main.gd") == "file:///home/user/main.gd"


def test_file_uri_windows_path():
    assert file_uri("C:\\Users\\pzalu\\main.gd") == "file:///C:/Users/pzalu/main.gd"


def test_file_uri_forward_slash_windows():
    assert file_uri("C:/Users/pzalu/main.gd") == "file:///C:/Users/pzalu/main.gd"


# ---------------------------------------------------------------------------
# uri_to_path
# ---------------------------------------------------------------------------

def test_uri_to_path_standard():
    assert uri_to_path("file:///C:/foo/main.gd") == "C:/foo/main.gd"


def test_uri_to_path_encoded_chars():
    assert uri_to_path("file:///C%3A/My%20Project/main.gd") == "C:/My Project/main.gd"


def test_uri_to_path_non_file_uri():
    assert uri_to_path("https://example.com") == "https://example.com"


# ---------------------------------------------------------------------------
# compact_location
# ---------------------------------------------------------------------------

def test_compact_location_full():
    loc = {
        "uri": "file:///C:/project/main.gd",
        "range": {"start": {"line": 10, "character": 5}, "end": {"line": 10, "character": 15}},
    }
    assert compact_location(loc) == {"file": "C:/project/main.gd", "line": 10, "char": 5}


def test_compact_location_empty():
    result = compact_location({})
    assert result == {"file": "", "line": 0, "char": 0}


# ---------------------------------------------------------------------------
# compact_symbol
# ---------------------------------------------------------------------------

def test_compact_symbol_flat():
    sym = {
        "name": "my_func",
        "kind": 12,
        "range": {"start": {"line": 5, "character": 0}},
    }
    assert compact_symbol(sym) == {"name": "my_func", "kind": 12, "line": 5}


def test_compact_symbol_with_children():
    sym = {
        "name": "MyClass",
        "kind": 5,
        "range": {"start": {"line": 0, "character": 0}},
        "children": [
            {"name": "method_a", "kind": 12, "range": {"start": {"line": 2, "character": 0}}},
        ],
    }
    result = compact_symbol(sym)
    assert result["name"] == "MyClass"
    assert len(result["children"]) == 1
    assert result["children"][0] == {"name": "method_a", "kind": 12, "line": 2}


def test_compact_symbol_location_fallback():
    sym = {
        "name": "var_x",
        "kind": 13,
        "location": {"start": {"line": 7, "character": 0}},
    }
    assert compact_symbol(sym) == {"name": "var_x", "kind": 13, "line": 7}


# ---------------------------------------------------------------------------
# LSPClient — async tests
# ---------------------------------------------------------------------------

def _make_lsp_message(data: dict) -> bytes:
    """Encode a dict as a Content-Length framed LSP message."""
    body = json.dumps(data).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body


@pytest.fixture
def client():
    return LSPClient(host="127.0.0.1", port=6005)


@pytest.mark.asyncio
async def test_request_formats_jsonrpc(client):
    """Verify that request() writes a properly framed JSON-RPC message."""
    written = bytearray()

    client.writer = MagicMock()
    client.writer.write = lambda data: written.extend(data)
    client.writer.drain = AsyncMock()
    client.writer.is_closing = MagicMock(return_value=False)

    # Mock reader to return a valid response
    response_data = {"jsonrpc": "2.0", "id": 1, "result": {"test": True}}
    client.reader = asyncio.StreamReader()
    client.reader.feed_data(_make_lsp_message(response_data))

    result = await client.request("textDocument/definition", {"textDocument": {"uri": "file:///test.gd"}})

    # Verify what was written
    written_str = written.decode("utf-8")
    assert "Content-Length:" in written_str
    # Extract JSON body after the double CRLF
    json_start = written_str.index("\r\n\r\n") + 4
    body = json.loads(written_str[json_start:])
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["method"] == "textDocument/definition"
    assert result == {"test": True}


@pytest.mark.asyncio
async def test_request_raises_on_error(client):
    """Verify that an LSP error response raises an exception."""
    client.writer = MagicMock()
    client.writer.write = MagicMock()
    client.writer.drain = AsyncMock()
    client.writer.is_closing = MagicMock(return_value=False)

    response_data = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid request"}}
    client.reader = asyncio.StreamReader()
    client.reader.feed_data(_make_lsp_message(response_data))

    with pytest.raises(Exception, match="Invalid request"):
        await client.request("textDocument/definition", {})


@pytest.mark.asyncio
async def test_diagnostics_cached_from_notifications(client):
    """Verify that publishDiagnostics notifications populate the cache."""
    client.writer = MagicMock()
    client.writer.write = MagicMock()
    client.writer.drain = AsyncMock()
    client.writer.is_closing = MagicMock(return_value=False)

    # Feed a notification followed by a real response
    notification = {
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {
            "uri": "file:///test.gd",
            "diagnostics": [{"range": {"start": {"line": 1}}, "message": "error", "severity": 1}],
        },
    }
    response = {"jsonrpc": "2.0", "id": 1, "result": None}

    client.reader = asyncio.StreamReader()
    client.reader.feed_data(_make_lsp_message(notification))
    client.reader.feed_data(_make_lsp_message(response))

    await client.request("textDocument/definition", {})

    assert "file:///test.gd" in client.diagnostics_cache
    assert len(client.diagnostics_cache["file:///test.gd"]) == 1


@pytest.mark.asyncio
async def test_connect_timeout(client):
    with patch("asyncio.open_connection", side_effect=asyncio.TimeoutError):
        ok, msg = await client.connect()
    assert ok is False
    assert "Timeout" in msg


@pytest.mark.asyncio
async def test_connect_refused(client):
    with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
        ok, msg = await client.connect()
    assert ok is False
    assert "Connection refused" in msg


@pytest.mark.asyncio
async def test_disconnect_cleanup(client):
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    client.writer = writer
    client.reader = MagicMock()
    client.initialized = True

    await client.disconnect()

    writer.close.assert_called_once()
    assert client.reader is None
    assert client.writer is None
    assert client.initialized is False
