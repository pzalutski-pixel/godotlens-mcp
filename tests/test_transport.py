"""Transport-level tests against a real socket.

Every test here fails against the pre-fix client. They exist because the previous
suite mocked the reader and fed each frame in one piece, which made short reads and
response miscorrelation structurally unreachable.
"""

import asyncio

import pytest

from godotlens_mcp.lsp_client import (
    LSPClient,
    LSPConnectionLost,
    LSPError,
    LSPTimeout,
)

pytestmark = pytest.mark.asyncio


async def test_large_body_split_across_tcp_segments(fake_lsp):
    """readexactly, not read: a body split across segments must not truncate.

    read(n) returns *up to* n bytes. Large documentSymbol/references payloads are
    exactly the ones TCP splits, so this failed worst on big projects.
    """
    payload = {"symbols": ["s" * 40 for _ in range(300)]}
    fake_lsp.responses["textDocument/documentSymbol"] = payload
    fake_lsp.chunk_size = 64  # force many segments

    client = LSPClient(port=fake_lsp.port, timeout=10.0)
    ok, msg = await client.connect()
    assert ok, msg
    try:
        result = await client.request("textDocument/documentSymbol", {})
        assert result == payload
    finally:
        await client.disconnect()


async def test_header_and_body_split(fake_lsp):
    """Even a 1-byte-at-a-time trickle must reassemble correctly."""
    fake_lsp.responses["textDocument/hover"] = {"contents": "ok"}
    fake_lsp.chunk_size = 1

    client = LSPClient(port=fake_lsp.port, timeout=15.0)
    assert (await client.connect())[0]
    try:
        assert await client.request("textDocument/hover", {}) == {"contents": "ok"}
    finally:
        await client.disconnect()


async def test_server_initiated_request_does_not_steal_the_response(connected_client, fake_lsp):
    """A server->client request carries an id but is not our response.

    Regression: _read_response returned the first message with any id, so this
    returned None and left the real reply buffered, desyncing every later call.
    """
    fake_lsp.responses["textDocument/definition"] = [{"uri": "file:///x.gd"}]
    fake_lsp.push_before_response = [
        {"jsonrpc": "2.0", "id": 9999, "method": "workspace/configuration",
         "params": {"items": []}},
    ]

    result = await connected_client.request("textDocument/definition", {})
    assert result == [{"uri": "file:///x.gd"}]

    # And the next call must still line up rather than returning the previous answer.
    fake_lsp.responses["textDocument/hover"] = {"contents": "second"}
    assert await connected_client.request("textDocument/hover", {}) == {"contents": "second"}


async def test_server_request_is_answered_with_method_not_found(connected_client, fake_lsp):
    """We must reply to a server request we don't implement, not swallow it."""
    fake_lsp.push_before_response = [
        {"jsonrpc": "2.0", "id": 4242, "method": "workspace/applyEdit", "params": {}},
    ]
    fake_lsp.responses["textDocument/hover"] = {"contents": "x"}
    await connected_client.request("textDocument/hover", {})

    reply = await fake_lsp.wait_for_received(lambda m: m.get("id") == 4242)
    assert reply["error"]["code"] == -32601


async def test_notifications_are_consumed_while_waiting(connected_client, fake_lsp):
    """publishDiagnostics arriving before our response must be cached, not confused."""
    fake_lsp.push_before_response = [
        {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
         "params": {"uri": "file:///C%3A/proj/a.gd",
                    "diagnostics": [{"range": {"start": {"line": 3}}, "message": "boom",
                                     "severity": 1}]}},
    ]
    fake_lsp.responses["textDocument/hover"] = {"contents": "hi"}

    assert await connected_client.request("textDocument/hover", {}) == {"contents": "hi"}

    from godotlens_mcp.lsp_client import canonical_key
    assert connected_client.diagnostics_cache[canonical_key("C:/proj/a.gd")][0]["message"] == "boom"


async def test_silent_server_times_out_instead_of_hanging(fake_lsp):
    """A server that accepts but never answers must not wedge the session forever."""
    fake_lsp.silent_methods = {"textDocument/hover"}
    client = LSPClient(port=fake_lsp.port, timeout=1.0)
    assert (await client.connect())[0]
    try:
        with pytest.raises(LSPTimeout):
            await asyncio.wait_for(client.request("textDocument/hover", {}), timeout=10)
    finally:
        await client.disconnect()


async def test_lsp_error_is_typed_and_connection_survives(connected_client, fake_lsp):
    fake_lsp.errors["workspace/didDeleteFiles"] = {
        "code": -32601, "message": "Method not found: workspace/didDeleteFiles"}
    fake_lsp.responses["textDocument/hover"] = {"contents": "still here"}

    with pytest.raises(LSPError) as excinfo:
        await connected_client.request("workspace/didDeleteFiles", {"files": []})
    assert excinfo.value.is_method_not_found

    assert await connected_client.request("textDocument/hover", {}) == {"contents": "still here"}


async def test_peer_vanishing_mid_request_raises_connection_lost(fake_lsp):
    """Godot killed mid-call must surface as a typed error, not a hang.

    This is the failure an agent hits when the user closes the editor while a tool
    call is in flight.
    """
    client = LSPClient(port=fake_lsp.port, timeout=5.0)
    assert (await client.connect())[0]
    try:
        fake_lsp.drop_connection = True
        with pytest.raises((LSPConnectionLost, LSPTimeout)):
            await asyncio.wait_for(client.request("textDocument/hover", {}), timeout=15)
    finally:
        await client.disconnect()


async def test_request_ids_increment_and_match(connected_client, fake_lsp):
    fake_lsp.responses["textDocument/hover"] = {"contents": "x"}
    for _ in range(3):
        await connected_client.request("textDocument/hover", {})
    ids = [m["id"] for m in fake_lsp.received if m.get("method") == "textDocument/hover"]
    assert ids == sorted(set(ids)), "request ids must be unique and monotonic"


async def test_concurrent_requests_do_not_interleave(connected_client, fake_lsp):
    """One socket, id-matched responses: overlapping callers must be serialized."""
    fake_lsp.responses["textDocument/hover"] = {"contents": "concurrent"}
    results = await asyncio.gather(*(
        connected_client.request("textDocument/hover", {}) for _ in range(8)
    ))
    assert all(r == {"contents": "concurrent"} for r in results)


async def test_initialize_sends_root_path_and_uri(fake_lsp, godot_project, monkeypatch):
    """Godot <=4.4 compares rootPath, so omitting it caused a workspace mismatch."""
    monkeypatch.chdir(godot_project)
    client = LSPClient(port=fake_lsp.port, timeout=5.0)
    assert (await client.connect())[0]
    try:
        init = next(m for m in fake_lsp.received if m.get("method") == "initialize")
        params = init["params"]
        assert params["rootPath"] == str(godot_project)
        assert params["rootUri"].startswith("file://")
        assert params["rootUri"].endswith("/proj")
    finally:
        await client.disconnect()


async def test_initialize_finds_project_root_from_a_subdirectory(fake_lsp, godot_project, monkeypatch):
    nested = godot_project / "scripts" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    client = LSPClient(port=fake_lsp.port, timeout=5.0)
    assert (await client.connect())[0]
    try:
        init = next(m for m in fake_lsp.received if m.get("method") == "initialize")
        assert init["params"]["rootPath"] == str(godot_project)
    finally:
        await client.disconnect()
