"""Process-level MCP tests: the real server, over real stdio pipes.

The previous suite drove handle_tool_call() directly, so the tools/call route through
handle_request() and the main read loop were never exercised in a live process. Every
"server dies on bad input" bug lived in exactly that gap.
"""


import json

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


@pytest.mark.integration
def test_engine_api_returns_real_signatures(mcp_process, live_lsp):
    """Authoritative engine API for the exact build, instead of recalled-from-memory.

    Backed by textDocument/nativeSymbol plus the native class list Godot pushes on
    connect, which the server previously received and discarded.
    """
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool(
        "gdscript_engine_api", {"class_name": "CharacterBody2D", "member": "move_and_slide"})
    assert not is_error, payload
    assert "move_and_slide" in json.dumps(payload)
    assert "bool" in json.dumps(payload), "expected the real return type"


@pytest.mark.integration
def test_engine_api_reports_a_missing_member_honestly(mcp_process, live_lsp):
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, _ = proc.call_tool(
        "gdscript_engine_api",
        {"class_name": "CharacterBody2D", "member": "definitely_not_a_real_method"})
    assert payload.get("found") is False


@pytest.mark.integration
def test_completion_returns_candidates_without_the_context_blob(mcp_process, live_lsp):
    """Godot echoes the whole request context into every item's `data` field."""
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    lines = (project / "player.gd").read_text(encoding="utf-8").split("\n")
    line_no = next(i for i, ln in enumerate(lines) if "health -= amount" in ln)

    payload, is_error = proc.call_tool(
        "gdscript_complete", {"file": "player.gd", "line": line_no, "character": 6})
    assert not is_error, payload
    assert payload["items"], "no completions returned"
    serialized = json.dumps(payload)
    assert "triggerKind" not in serialized, "per-item context blob was not stripped"
    assert "textDocument" not in serialized


@pytest.mark.integration
def test_validate_catches_broken_content_without_writing_it(mcp_process, live_lsp):
    """The pre-write gate: check an edit before it reaches disk."""
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    target = project / "player.gd"
    before = target.read_text(encoding="utf-8")

    payload, is_error = proc.call_tool("gdscript_validate", {
        "file": "player.gd",
        "content": "extends Node\n\n\nfunc broken() -> void:\n\tthis is not valid !!\n",
    })
    assert not is_error, payload
    assert payload["valid"] is False
    assert payload["errors"], "a syntax error should have been reported"
    assert target.read_text(encoding="utf-8") == before, "validate must not touch disk"


@pytest.mark.integration
def test_validate_accepts_good_content(mcp_process, live_lsp):
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, is_error = proc.call_tool("gdscript_validate", {
        "file": "fresh_check.gd",
        "content": "extends Node\n\n\nfunc fine() -> int:\n\treturn 1\n",
    })
    assert not is_error, payload
    assert payload["valid"] is True
    assert payload["errors"] == []


@pytest.mark.integration
def test_rename_warns_about_scene_connections(mcp_process, live_lsp):
    """Godot's rename cannot see .tscn, so a signal handler breaks silently.

    main.tscn wires _on_hit_area_entered via a [connection] block and nothing in any
    .gd file calls it.
    """
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    lines = (project / "player.gd").read_text(encoding="utf-8").split("\n")
    line_no = next(i for i, ln in enumerate(lines)
                   if ln.startswith("func _on_hit_area_entered"))
    col = lines[line_no].index("_on_hit_area_entered") + 5

    payload, is_error = proc.call_tool("gdscript_rename", {
        "file": "player.gd", "line": line_no, "character": col,
        "new_name": "_on_hit_area_entered_renamed",
        "old_name": "_on_hit_area_entered",
    })
    assert not is_error, payload
    assert payload.get("scene_references"), "did not flag the .tscn connection"
    assert "main.tscn" in json.dumps(payload["scene_references"])
    assert "warning" in payload


@pytest.mark.integration
def test_rename_refuses_engine_builtins_instead_of_silently_doing_nothing(mcp_process, live_lsp):
    """GDScriptWorkspace::rename returns a well-formed empty edit for a built-in.

    Without a prepareRename check that is indistinguishable from success.
    """
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    lines = (project / "player.gd").read_text(encoding="utf-8").split("\n")
    line_no = next(i for i, ln in enumerate(lines) if "extends CharacterBody2D" in ln)
    col = lines[line_no].index("CharacterBody2D") + 3

    payload, is_error = proc.call_tool("gdscript_rename", {
        "file": "player.gd", "line": line_no, "character": col,
        "new_name": "Nonsense", "old_name": "CharacterBody2D",
    })
    assert not is_error, payload
    assert payload.get("renamed") is False
    assert payload.get("reason") in {"not_renameable", "no_usages_found"}


@pytest.mark.integration
def test_references_in_file_is_gated_on_capability(mcp_process, live_lsp):
    """documentHighlight is 4.7+; on older editors report unsupported, don't guess."""
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    lines = (project / "player.gd").read_text(encoding="utf-8").split("\n")
    line_no = next(i for i, ln in enumerate(lines) if ln.startswith("func take_damage"))
    col = lines[line_no].index("take_damage") + 3

    payload, is_error = proc.call_tool(
        "gdscript_references_in_file", {"file": "player.gd", "line": line_no, "character": col})
    assert not is_error, payload
    if isinstance(payload, dict) and payload.get("supported") is False:
        assert "4.7" in payload["reason"]
    else:
        assert payload, "expected in-file occurrences"


@pytest.mark.integration
def test_release_file_reports_what_it_actually_did(mcp_process, live_lsp):
    """The old delete_file returned {"deleted": ...} for a total no-op."""
    port, project = live_lsp
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(port)})
    proc.initialize()

    payload, _ = proc.call_tool("gdscript_release_file", {"file": "never_opened_xyz.gd"})
    assert payload["was_open"] is False

    proc.call_tool("gdscript_sync_file", {"file": "game_state.gd"})
    payload, _ = proc.call_tool("gdscript_release_file", {"file": "game_state.gd"})
    assert payload["was_open"] is True


def test_server_exits_cleanly_on_eof(mcp_process, godot_project):
    proc = mcp_process(cwd=godot_project, env={"GODOT_LSP_PORT": "16"})
    proc.initialize()
    stderr = proc.close()
    assert proc.proc.returncode == 0, f"non-zero exit; stderr:\n{stderr}"
    assert "Traceback" not in stderr
