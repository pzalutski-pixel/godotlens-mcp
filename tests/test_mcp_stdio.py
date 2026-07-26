"""Process-level MCP tests: the real server, over real stdio pipes.

The previous suite drove handle_tool_call() directly, so the tools/call route through
handle_request() and the main read loop were never exercised in a live process. Every
"server dies on bad input" bug lived in exactly that gap.
"""


import pytest


def test_initialize_and_tools_list(mcp_process):
    proc = mcp_process()
    resp = proc.initialize()
    assert resp["result"]["serverInfo"]["name"] == "godotlens-mcp"
    assert resp["result"]["protocolVersion"] == "2025-11-25"
    assert "instructions" in resp["result"]

    tools = proc.request("tools/list")["result"]["tools"]
    assert tools
    assert all("inputSchema" in t for t in tools)


def test_blank_line_does_not_end_the_session(mcp_process):
    """Regression: one stray newline terminated the server, silently dropping work."""
    proc = mcp_process()
    proc.initialize()
    proc.send_raw("")
    proc.send_raw("")
    assert proc.request("ping")["result"] == {}


def test_malformed_json_returns_parse_error_and_continues(mcp_process):
    """Regression: json.loads raised out of the loop and killed the process."""
    proc = mcp_process()
    proc.initialize()

    proc.send_raw("NOT JSON AT ALL")
    err = proc.read()
    assert err["error"]["code"] == -32700

    assert proc.request("ping")["result"] == {}


def test_json_array_is_rejected_and_session_continues(mcp_process):
    """Regression: msg.get() on a list raised AttributeError and killed the process."""
    proc = mcp_process()
    proc.initialize()

    proc.send_raw('[{"jsonrpc":"2.0","id":99,"method":"ping"}]')
    err = proc.read()
    assert err["error"]["code"] == -32600

    assert proc.request("ping")["result"] == {}


def test_unknown_method_errors_without_dying(mcp_process):
    proc = mcp_process()
    proc.initialize()
    resp = proc.request("no/such/method")
    assert resp["error"]["code"] == -32601
    assert proc.request("ping")["result"] == {}


def test_tool_call_without_godot_reports_disconnected_not_a_crash(mcp_process, godot_project):
    """With no editor running, every tool must fail cleanly and stay responsive."""
    proc = mcp_process(cwd=godot_project, env={"GODOT_LSP_PORT": "16"})  # nothing listens
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_symbols", {"file": "player.gd"})
    assert is_error
    assert "error" in payload

    # And the server is still alive afterwards.
    assert proc.request("ping")["result"] == {}


def test_status_tool_reports_disconnected_cleanly(mcp_process, godot_project):
    proc = mcp_process(cwd=godot_project, env={"GODOT_LSP_PORT": "16"})
    proc.initialize()
    payload, _ = proc.call_tool("gdscript_status", {})
    assert payload["status"] == "disconnected"
    assert payload["port"] == 16


def test_stdout_carries_only_jsonrpc(mcp_process, godot_project):
    """The stdio transport forbids non-protocol output on stdout."""
    proc = mcp_process(cwd=godot_project, env={"GODOT_LSP_PORT": "16"})
    proc.initialize()
    proc.call_tool("gdscript_symbols", {"file": "player.gd"})
    resp = proc.request("ping")
    assert resp["jsonrpc"] == "2.0"  # would fail if a log line landed on stdout


@pytest.mark.integration
def test_relative_path_resolves_against_cwd(mcp_process, live_lsp):
    """Regression: relative paths produced file:///player.gd and returned nothing.

    Every tool schema advertises relative paths as supported.
    """
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_symbols", {"file": "player.gd"})
    assert not is_error, payload
    assert payload, "relative path returned nothing"
    assert payload[0]["name"] == "Player"


@pytest.mark.integration
def test_sync_file_reports_a_real_syntax_error(mcp_process, live_lsp):
    """The headline regression, end to end through the MCP surface.

    Before the cache-key fix this returned {"diagnostics": []} with isError false —
    a false clean bill of health on genuinely broken code.
    """
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_sync_file", {"file": "broken.gd"})
    assert not is_error, payload
    diagnostics = payload["diagnostics"]
    assert diagnostics, "reported clean on a file with a syntax error"
    assert any("Expected" in d["message"] for d in diagnostics)


@pytest.mark.integration
def test_empty_and_failed_results_are_distinguishable(mcp_process, live_lsp):
    """A clean file yields an empty list, not a sentinel string."""
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_sync_file", {"file": "game_state.gd"})
    assert not is_error
    assert payload["diagnostics"] == []
    assert not isinstance(payload, str)


@pytest.mark.integration
def test_tools_survive_a_bad_path_and_keep_the_connection(mcp_process, live_lsp):
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_sync_file", {"file": "nope_missing.gd"})
    assert is_error
    assert payload["kind"] == "file_error"

    # A file error must not have torn down the LSP connection.
    payload, is_error = proc.call_tool("gdscript_symbols", {"file": "player.gd"})
    assert not is_error, payload
    assert payload[0]["name"] == "Player"


@pytest.mark.integration
def test_res_scheme_is_rejected_with_guidance(mcp_process, live_lsp):
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_symbols", {"file": "res://player.gd"})
    assert is_error
    assert payload["kind"] == "unsupported_path"
    assert "res://" in payload["error"]


def test_server_exits_cleanly_on_eof(mcp_process, godot_project):
    proc = mcp_process(cwd=godot_project, env={"GODOT_LSP_PORT": "16"})
    proc.initialize()
    stderr = proc.close()
    assert proc.proc.returncode == 0, f"non-zero exit; stderr:\n{stderr}"
    assert "Traceback" not in stderr
