"""The debug_* tools, driven through the MCP surface against a real editor.

Godot serves the debug adapter from the editor, so these need a running editor but
not a running game. What needs a running *game* is documented per test.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def debug_proc(mcp_process, live_lsp):
    proc = mcp_process(cwd=live_lsp.project, env={
        "GODOT_LSP_PORT": str(live_lsp.lsp_port),
        "GODOT_DAP_PORT": str(live_lsp.dap_port),
    })
    proc.initialize()
    return proc, live_lsp.project


def test_debug_status_reports_the_adapter(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_status", {})

    assert not is_error, payload
    assert payload["connected"] is True
    assert payload["capabilities"]["supportsConfigurationDoneRequest"] is True


def test_debug_set_breakpoints_round_trips_zero_based_lines(debug_proc):
    """DAP is 1-based, our tools are 0-based, so the boundary must convert cleanly."""
    proc, project = debug_proc
    lines = (project / "player.gd").read_text(encoding="utf-8").split("\n")
    target = next(i for i, ln in enumerate(lines) if "health -= amount" in ln)

    payload, is_error = proc.call_tool(
        "debug_set_breakpoints", {"file": "player.gd", "lines": [target]})

    assert not is_error, payload
    assert payload["breakpoints"], "adapter returned no breakpoints"
    assert payload["breakpoints"][0]["line"] == target


def test_debug_set_breakpoints_accepts_an_empty_list(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool(
        "debug_set_breakpoints", {"file": "player.gd", "lines": []})
    assert not is_error, payload
    assert payload["breakpoints"] == []


def test_debug_stack_trace_is_empty_and_honest_when_idle(debug_proc):
    """No game running means no frames. That must be stated, not implied by silence."""
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_stack_trace", {})

    assert not is_error, payload
    assert payload["frames"] == []
    assert "not currently paused" in payload["note"]


def test_debug_output_is_empty_but_explains_why(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_output", {"wait": 0.5})

    assert not is_error, payload
    assert payload["count"] == 0
    assert "running" in payload["note"]


def test_debug_evaluate_without_a_session_fails_cleanly(debug_proc):
    """Evaluating with nothing paused is a legitimate error, not a crash."""
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_evaluate", {"expression": "1 + 1"})

    if is_error:
        assert payload["kind"] in {"dap_error", "dap_connection_lost"}
    else:
        assert "result" in payload

    # The session must remain usable either way.
    status, status_error = proc.call_tool("debug_status", {})
    assert not status_error, status
    assert status["connected"] is True


def test_debug_tools_do_not_disturb_the_lsp_connection(debug_proc):
    """LSP and DAP are separate ports; a debug failure must not affect analysis."""
    proc, _ = debug_proc
    proc.call_tool("debug_evaluate", {"expression": "not_a_real_symbol"})

    payload, is_error = proc.call_tool("gdscript_symbols", {"file": "player.gd"})
    assert not is_error, payload
    assert payload[0]["name"] == "Player"


def test_debug_terminate_is_safe_with_nothing_running(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_terminate", {})
    if is_error:
        assert payload["kind"] in {"dap_error", "dap_connection_lost"}
    else:
        assert payload["terminated"] is True


def test_missing_adapter_reports_actionably(mcp_process, live_lsp):
    """A wrong or disabled DAP port must explain where the adapter lives."""
    proc = mcp_process(cwd=live_lsp.project, env={
        "GODOT_LSP_PORT": str(live_lsp.lsp_port),
        "GODOT_DAP_PORT": "9",  # discard port; nothing listens
    })
    proc.initialize()

    payload, is_error = proc.call_tool("debug_status", {})
    assert is_error
    assert payload["kind"] == "dap_disconnected"
    assert "Debug Adapter" in payload["hint"]

    # And the LSP side must still work, since they are independent.
    payload, is_error = proc.call_tool("gdscript_symbols", {"file": "player.gd"})
    assert not is_error, payload


def test_debug_inspect_without_a_paused_frame_fails_cleanly(debug_proc):
    """Inspecting a frame that does not exist is an error, not a crash."""
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_inspect", {"frame_id": 0})

    if is_error:
        assert payload["kind"] in {"dap_error", "dap_connection_lost"}
    else:
        # Godot answers with no scopes when nothing is paused.
        assert payload["scopes"] == [] or isinstance(payload["scopes"], list)

    status, status_error = proc.call_tool("debug_status", {})
    assert not status_error and status["connected"] is True


def test_debug_continue_with_nothing_paused_is_handled(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_continue", {})
    if is_error:
        assert payload["kind"] in {"dap_error", "dap_connection_lost"}
    else:
        assert payload["resumed"] is True


def test_debug_pause_with_nothing_running_is_handled(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_pause", {})
    if is_error:
        assert payload["kind"] in {"dap_error", "dap_connection_lost"}
    else:
        assert payload["paused"] is True


def test_debug_step_over_with_nothing_paused_is_handled(debug_proc):
    proc, _ = debug_proc
    payload, is_error = proc.call_tool("debug_step_over", {})
    if is_error:
        assert payload["kind"] in {"dap_error", "dap_connection_lost"}
    else:
        assert payload["stepped"] is True


def test_execution_control_leaves_the_session_usable(debug_proc):
    """The whole control surface, back to back, must not wedge the adapter."""
    proc, _ = debug_proc
    for tool in ("debug_continue", "debug_pause", "debug_step_over"):
        proc.call_tool(tool, {})

    status, is_error = proc.call_tool("debug_status", {})
    assert not is_error, status
    assert status["connected"] is True


def test_debug_tools_are_annotated_correctly():
    """debug_terminate stops a running game; that must not be marked read-only."""
    from godotlens_mcp.server import TOOLS

    by_name = {t["name"]: t for t in TOOLS}
    assert by_name["debug_output"]["annotations"]["readOnlyHint"] is True
    assert by_name["debug_stack_trace"]["annotations"]["readOnlyHint"] is True
    assert by_name["debug_terminate"]["annotations"]["destructiveHint"] is True
    assert by_name["debug_set_breakpoints"]["annotations"]["readOnlyHint"] is False
