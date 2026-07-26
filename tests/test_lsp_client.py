"""Tests for the LSP client and utility functions."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from godotlens_mcp.lsp_client import (
    LSPClient,
    UnsupportedPathError,
    canonical_key,
    compact_location,
    compact_symbol,
    file_uri,
    uri_to_path,
)

# ---------------------------------------------------------------------------
# file_uri
# ---------------------------------------------------------------------------

WINDOWS = os.name == "nt"


@pytest.mark.skipif(WINDOWS, reason="POSIX absolute paths are drive-relative on Windows")
def test_file_uri_unix_path():
    assert file_uri("/home/user/main.gd") == "file:///home/user/main.gd"


@pytest.mark.skipif(not WINDOWS, reason="Windows drive paths")
def test_file_uri_windows_path():
    assert file_uri("C:\\Users\\pzalu\\main.gd") == "file:///C:/Users/pzalu/main.gd"


@pytest.mark.skipif(not WINDOWS, reason="Windows drive paths")
def test_file_uri_forward_slash_windows():
    assert file_uri("C:/Users/pzalu/main.gd") == "file:///C:/Users/pzalu/main.gd"


def test_file_uri_resolves_relative_paths(tmp_path, monkeypatch):
    """Relative paths must resolve against cwd, not the filesystem root.

    Regression: file_uri("scripts/player.gd") returned "file:///scripts/player.gd",
    which pointed at the root of the filesystem on every platform, even though every
    tool schema advertises relative paths as supported.
    """
    monkeypatch.chdir(tmp_path)
    uri = file_uri("scripts/player.gd")
    assert uri != "file:///scripts/player.gd"
    assert uri.endswith("/scripts/player.gd")
    assert uri_to_path(uri) == canonical_path_str(tmp_path / "scripts" / "player.gd")


def test_file_uri_rejects_res_scheme():
    """Godot 4.5+ hard-rejects non-file schemes, so fail loudly rather than mangle."""
    with pytest.raises(UnsupportedPathError, match="res://"):
        file_uri("res://scripts/player.gd")


@pytest.mark.parametrize("name", [
    "plain.gd",
    "with space.gd",
    "with#hash.gd",
    "with(parens).gd",
    "ünïcode.gd",
    "with%percent.gd",
])
def test_file_uri_round_trip(tmp_path, name):
    """path -> file_uri -> uri_to_path must return the original path on any platform."""
    target = tmp_path / "sub dir" / name
    original = canonical_path_str(target)
    assert uri_to_path(file_uri(original)) == original


def canonical_path_str(path) -> str:
    """Forward-slash absolute form, matching what uri_to_path returns."""
    return os.path.abspath(str(path)).replace("\\", "/")


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
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


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
    """publishDiagnostics notifications populate the cache under a canonical key.

    Regression test for the Godot 4.5+ percent-encoding bug: Godot publishes
    ``file:///C%3A/...`` while file_uri() builds ``file:///C:/...``. Keying the cache
    on the raw URI string meant the lookup never matched and every diagnostic was
    silently dropped, so sync/diagnostics reported clean on broken code.
    """
    client.writer = MagicMock()
    client.writer.write = MagicMock()
    client.writer.drain = AsyncMock()
    client.writer.is_closing = MagicMock(return_value=False)

    # Exactly the encoding Godot 4.5+ emits on Windows.
    notification = {
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {
            "uri": "file:///C%3A/My%20Project/test.gd",
            "diagnostics": [{"range": {"start": {"line": 1}}, "message": "error", "severity": 1}],
        },
    }
    response = {"jsonrpc": "2.0", "id": 1, "result": None}

    client.reader = asyncio.StreamReader()
    client.reader.feed_data(_make_lsp_message(notification))
    client.reader.feed_data(_make_lsp_message(response))

    await client.request("textDocument/definition", {})

    # The unencoded path a caller would pass must find the encoded publication.
    key = canonical_key("C:/My Project/test.gd")
    assert key in client.diagnostics_cache
    assert len(client.diagnostics_cache[key]) == 1


@pytest.mark.parametrize(
    "published,requested",
    [
        # Godot 4.5+ percent-encodes the drive colon; we build it plain.
        ("file:///C%3A/proj/main.gd", "C:/proj/main.gd"),
        ("file:///C%3A/My%20Project/main.gd", "C:/My Project/main.gd"),
        ("file:///C%3A/pr%C3%B3j/main.gd", "C:/prój/main.gd"),
        # POSIX publications must match a POSIX path.
        ("file:///home/user/proj/main.gd", "/home/user/proj/main.gd"),
        ("file:///home/user/My%20Proj/main.gd", "/home/user/My Proj/main.gd"),
        # Redundant separators and dot segments normalize away.
        ("file:///C%3A/proj/./sub/../main.gd", "C:/proj/main.gd"),
    ],
)
def test_canonical_key_matches_published_and_requested(published, requested):
    """A URI Godot publishes and the path a caller passes must key identically."""
    assert canonical_key(published) == canonical_key(requested)


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
