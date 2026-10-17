"""Debug Adapter Protocol client.

The unit tests use a fake adapter so every branch is reachable; the integration
tests confirm the real editor answers the way we assume.
"""

import asyncio

import pytest

from godotlens_mcp.dap_client import (
    DAPClient,
    DAPConnectionLost,
    DAPError,
    DAPTimeout,
    _to_zero_based,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Line numbering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given,expected", [(1, 0), (2, 1), (15, 14), (0, 0), (None, None)])
async def test_line_conversion_is_clamped(given, expected):
    """DAP is 1-based; every GodotLens tool is 0-based. Never go negative."""
    assert _to_zero_based(given) == expected


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


async def test_connect_captures_capabilities(dap_client):
    assert dap_client.initialized is True
    assert dap_client.capabilities["supportsConfigurationDoneRequest"] is True


async def test_connect_negotiates_one_based_lines_explicitly(dap_client, fake_dap):
    """Conversion is done in our code, so the wire must be unambiguous."""
    init = next(m for m in fake_dap.received if m.get("command") == "initialize")
    assert init["arguments"]["linesStartAt1"] is True


async def test_connection_refused_is_actionable():
    client = DAPClient(port=9, timeout=2.0)  # discard port; nothing listens
    ok, message = await client.connect()
    assert ok is False
    assert "Debug Adapter" in message or "refused" in message.lower()


async def test_failed_command_raises_dap_error(dap_client, fake_dap):
    fake_dap.failures["stackTrace"] = "no active session"
    with pytest.raises(DAPError, match="no active session"):
        await dap_client.stack_trace()


async def test_error_does_not_drop_the_connection(dap_client, fake_dap):
    fake_dap.failures["variables"] = "unknown"
    with pytest.raises(DAPError):
        await dap_client.variables(1)

    fake_dap.bodies["threads"] = {"threads": [{"id": 1, "name": "Main"}]}
    assert await dap_client.threads() == [{"id": 1, "name": "Main"}]


async def test_silent_adapter_times_out(fake_dap):
    fake_dap.silent_methods = {"stackTrace"}
    client = DAPClient(port=fake_dap.port, timeout=1.0)
    assert (await client.connect())[0]
    try:
        with pytest.raises(DAPTimeout):
            await asyncio.wait_for(client.stack_trace(), timeout=10)
    finally:
        await client.disconnect()


async def test_peer_loss_raises_connection_lost(fake_dap):
    client = DAPClient(port=fake_dap.port, timeout=5.0)
    assert (await client.connect())[0]
    try:
        fake_dap.drop_connection = True
        with pytest.raises((DAPConnectionLost, DAPTimeout)):
            await asyncio.wait_for(client.threads(), timeout=15)
    finally:
        await client.disconnect()


async def test_large_body_split_across_segments(fake_dap):
    """Same short-read hazard as the LSP: stack traces can be large."""
    frames = [{"id": i, "name": f"frame_{i}" * 8, "line": i + 1,
               "source": {"path": "/proj/a.gd"}} for i in range(200)]
    fake_dap.bodies["stackTrace"] = {"stackFrames": frames}
    fake_dap.chunk_size = 128

    client = DAPClient(port=fake_dap.port, timeout=15.0)
    assert (await client.connect())[0]
    try:
        result = await client.stack_trace()
        assert len(result) == 200
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_output_events_are_captured_and_converted(dap_client, fake_dap):
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "output",
         "body": {"category": "stdout", "output": "hello from the game\n",
                  "line": 12, "source": {"path": "/proj/player.gd"}}},
        {"seq": 2, "type": "event", "event": "output",
         "body": {"category": "stderr", "output": "SCRIPT ERROR: boom\n"}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()  # events ride ahead of the response

    captured = dap_client.take_output()
    assert len(captured) == 2
    assert captured[0]["text"] == "hello from the game\n"
    assert captured[0]["category"] == "stdout"
    assert captured[0]["line"] == 11, "line should be converted to 0-based"
    assert captured[1]["category"] == "stderr"


async def test_take_output_drains_by_default(dap_client, fake_dap):
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "output", "body": {"output": "once"}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()

    assert len(dap_client.take_output()) == 1
    assert dap_client.take_output() == [], "output should not be returned twice"


async def test_take_output_can_preserve(dap_client, fake_dap):
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "output", "body": {"output": "keep"}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()

    assert len(dap_client.take_output(clear=False)) == 1
    assert len(dap_client.take_output(clear=False)) == 1


async def test_stopped_event_records_state(dap_client, fake_dap):
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "stopped",
         "body": {"reason": "breakpoint", "threadId": 1, "description": "Paused"}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()

    assert dap_client.stopped_state["reason"] == "breakpoint"
    assert dap_client.stopped_state["thread_id"] == 1


async def test_terminated_event_is_recorded(dap_client, fake_dap):
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "terminated", "body": {}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()
    assert dap_client.terminated is True


async def test_continue_clears_the_stopped_state(dap_client, fake_dap):
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "stopped", "body": {"reason": "breakpoint"}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()
    assert dap_client.stopped_state is not None

    await dap_client.continue_execution()
    assert dap_client.stopped_state is None


async def test_output_buffer_is_bounded(dap_client, fake_dap):
    """A chatty game must not grow the buffer without bound."""
    from godotlens_mcp.dap_client import MAX_EVENTS

    fake_dap.push_events = [
        {"seq": i, "type": "event", "event": "output", "body": {"output": f"line {i}"}}
        for i in range(MAX_EVENTS + 200)
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()
    assert len(dap_client.output) <= MAX_EVENTS


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def test_set_breakpoints_converts_to_one_based(dap_client, fake_dap):
    fake_dap.bodies["setBreakpoints"] = {
        "breakpoints": [{"id": 0, "verified": True, "line": 15}]}
    result = await dap_client.set_breakpoints("/proj/player.gd", [14])

    sent = next(m for m in fake_dap.received if m.get("command") == "setBreakpoints")
    assert sent["arguments"]["breakpoints"] == [{"line": 15}], "0-based 14 must go out as 15"
    assert result[0]["line"] == 14, "and come back as 0-based again"
    assert result[0]["verified"] is True


async def test_stack_trace_is_compacted_and_zero_based(dap_client, fake_dap):
    fake_dap.bodies["stackTrace"] = {"stackFrames": [
        {"id": 3, "name": "take_damage", "line": 21, "source": {"path": "/proj/player.gd"}},
    ]}
    frames = await dap_client.stack_trace()
    assert frames == [{"id": 3, "name": "take_damage", "file": "/proj/player.gd", "line": 20}]


async def test_scopes_and_variables(dap_client, fake_dap):
    fake_dap.bodies["scopes"] = {"scopes": [
        {"name": "Locals", "variablesReference": 7, "expensive": False}]}
    fake_dap.bodies["variables"] = {"variables": [
        {"name": "health", "value": "100", "type": "int", "variablesReference": 0}]}

    scopes = await dap_client.scopes(0)
    assert scopes[0]["variables_reference"] == 7
    variables = await dap_client.variables(7)
    assert variables[0] == {"name": "health", "value": "100", "type": "int",
                            "variables_reference": 0}


async def test_evaluate_passes_frame_and_returns_value(dap_client, fake_dap):
    fake_dap.bodies["evaluate"] = {"result": "42", "type": "int", "variablesReference": 0}
    result = await dap_client.evaluate("6 * 7", frame_id=2)

    sent = next(m for m in fake_dap.received if m.get("command") == "evaluate")
    assert sent["arguments"]["frameId"] == 2
    assert sent["arguments"]["context"] == "repl"
    assert result["result"] == "42"


async def test_evaluate_without_a_frame_omits_frame_id(dap_client, fake_dap):
    fake_dap.bodies["evaluate"] = {"result": "ok"}
    await dap_client.evaluate("1")
    sent = next(m for m in fake_dap.received if m.get("command") == "evaluate")
    assert "frameId" not in sent["arguments"]


async def test_an_unanswered_expression_is_resent_with_different_text(dap_client, fake_dap):
    """Godot never forwards a repeat of an expression text it left unanswered."""
    fake_dap.silent_methods = {"evaluate"}
    with pytest.raises(DAPTimeout):
        await dap_client.evaluate("hp", frame_id=0, timeout=0.3)

    fake_dap.silent_methods = set()
    fake_dap.bodies["evaluate"] = {"result": "100"}
    assert (await dap_client.evaluate("hp", frame_id=0))["result"] == "100"

    sent = [m["arguments"]["expression"] for m in fake_dap.received
            if m.get("command") == "evaluate"]
    assert sent == ["hp", "(hp)"]


async def test_terminate_marks_the_session_finished(dap_client, fake_dap):
    await dap_client.terminate()
    assert dap_client.terminated is True


async def test_requests_are_serialized(dap_client, fake_dap):
    """One socket and seq-correlated responses: overlapping callers must not interleave."""
    fake_dap.bodies["threads"] = {"threads": [{"id": 1, "name": "Main"}]}
    results = await asyncio.gather(*(dap_client.threads() for _ in range(8)))
    assert all(r == [{"id": 1, "name": "Main"}] for r in results)

    seqs = [m["seq"] for m in fake_dap.received if m.get("type") == "request"]
    assert len(seqs) == len(set(seqs)), "sequence numbers must be unique"


# ---------------------------------------------------------------------------
# A game that keeps talking
# ---------------------------------------------------------------------------
#
# Regression: the client read only inside calls and "drained" until the stream went
# quiet, with no overall deadline. A game printing more than once per wait window
# made connect(), debug_output and debug_run never return, and because the MCP server
# handles calls one at a time, every subagent stalled with it.


async def _streaming_client(fake_dap, interval=0.1) -> DAPClient:
    fake_dap.stream_interval = interval
    client = DAPClient(port=fake_dap.port, timeout=5.0)
    ok, message = await asyncio.wait_for(client.connect(), timeout=5)
    assert ok, message
    return client


async def test_connect_returns_while_the_adapter_streams_events(fake_dap):
    client = await _streaming_client(fake_dap)
    await client.disconnect()


async def test_drain_events_is_bounded_under_a_steady_stream(fake_dap):
    client = await _streaming_client(fake_dap)
    try:
        loop = asyncio.get_event_loop()
        started = loop.time()
        arrived = await asyncio.wait_for(client.drain_events(1.0), timeout=5)
        assert loop.time() - started < 2.0
        assert arrived > 0, "events kept arriving but none were recorded"
    finally:
        await client.disconnect()


async def test_requests_are_answered_while_events_stream(fake_dap):
    client = await _streaming_client(fake_dap)
    try:
        fake_dap.bodies["threads"] = {"threads": [{"id": 1, "name": "Main"}]}
        assert await asyncio.wait_for(client.threads(), timeout=5) == [{"id": 1, "name": "Main"}]
    finally:
        await client.disconnect()


async def test_collect_output_is_bounded_under_a_steady_stream(fake_dap):
    client = await _streaming_client(fake_dap)
    try:
        lines = await asyncio.wait_for(client.collect_output(1.0), timeout=5)
        assert lines, "the streamed output was not collected"
    finally:
        await client.disconnect()


async def test_an_idle_client_keeps_reading(fake_dap):
    """Nothing calls the client, yet a multi-megabyte burst must be accepted in full.

    Godot's adapter writes from the editor's main thread and spins until the socket
    takes every byte, so a client that stops reading freezes the editor.
    """
    fake_dap.burst_after_connect = [
        {"seq": 0, "type": "event", "event": "output",
         "body": {"category": "stdout", "output": "x" * 4000 + "\n"}}
        for _ in range(1000)
    ]
    client = DAPClient(port=fake_dap.port, timeout=5.0)
    assert (await client.connect())[0]
    try:
        await asyncio.wait_for(fake_dap.burst_done.wait(), timeout=15)
        from godotlens_mcp.dap_client import MAX_EVENTS
        assert 0 < len(client.output) <= MAX_EVENTS
    finally:
        await client.disconnect()


async def test_a_stale_terminated_does_not_end_a_new_launch(dap_client, fake_dap):
    """A terminated left over from an earlier session must not mark the new game done.

    Godot never sends terminated to the launching client while launch is pending, so
    one that arrives then belongs to something earlier.
    """
    fake_dap.push_events = [{"seq": 1, "type": "event", "event": "terminated", "body": {}}]
    await dap_client.launch("/proj")
    assert dap_client.terminated is False


async def test_launch_opts_in_to_structured_messages_and_passes_play_args(dap_client, fake_dap):
    await dap_client.launch("/proj", play_args=["--headless", "--fixed-fps", "60"])
    sent = next(m for m in fake_dap.received if m.get("command") == "launch")
    assert sent["arguments"]["godot/custom_data"] is True
    assert sent["arguments"]["playArgs"] == ["--headless", "--fixed-fps", "60"]


async def test_structured_errors_are_decoded_and_other_custom_data_dropped(dap_client, fake_dap):
    """push_error and engine errors arrive only as godot/custom_data 'error' messages."""
    error = [0, 0, 5, 130, "core/variant/variant_utility.cpp", "push_error", 1100,
             "Enemy count is wrong", "", False, 6,
             "res://main.gd", "_ready", 12, "res://spawner.gd", "spawn", 30]
    fake_dap.push_events = [
        {"seq": 1, "type": "event", "event": "godot/custom_data",
         "body": {"message": "performance:profile_frame", "data": [60.0, 0.016]}},
        {"seq": 2, "type": "event", "event": "godot/custom_data",
         "body": {"message": "error", "data": error}},
    ]
    fake_dap.bodies["threads"] = {"threads": []}
    await dap_client.threads()

    errors = dap_client.take_errors()
    assert len(errors) == 1
    assert errors[0]["file"] == "res://main.gd", "location must come from the script stack"
    assert errors[0]["line"] == 11
    assert errors[0]["message"] == "Enemy count is wrong"
    assert [frame["function"] for frame in errors[0]["stack"]] == ["_ready", "spawn"]
    assert dap_client.take_errors() == [], "errors are drained like output"


# ---------------------------------------------------------------------------
# Against the real editor
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_real_adapter_answers_and_reports_capabilities(live_lsp):
    """Godot serves DAP alongside the LSP with no addon, verified on 4.7.1."""
    client = DAPClient(port=live_lsp.dap_port, timeout=20.0)
    ok, message = await client.connect()
    assert ok, message
    try:
        caps = client.capabilities
        assert caps.get("supportsConfigurationDoneRequest") is True
        assert caps.get("supportsTerminateRequest") is True
    finally:
        await client.disconnect()


@pytest.mark.integration
async def test_real_adapter_sets_breakpoints(live_lsp):
    client = DAPClient(port=live_lsp.dap_port, timeout=20.0)
    assert (await client.connect())[0]
    try:
        target = str(live_lsp.project / "player.gd")
        lines = (live_lsp.project / "player.gd").read_text(encoding="utf-8").split("\n")
        line_no = next(i for i, ln in enumerate(lines) if "health -= amount" in ln)

        result = await client.set_breakpoints(target, [line_no])
        assert result, "adapter returned no breakpoints"
        assert result[0]["line"] == line_no, "round-trip line numbering is off"
    finally:
        await client.disconnect()


@pytest.mark.integration
async def test_real_adapter_reports_threads_and_empty_stack_when_idle(live_lsp):
    """With no game running there is no stack. That must be reported, not error."""
    client = DAPClient(port=live_lsp.dap_port, timeout=20.0)
    assert (await client.connect())[0]
    try:
        await client.configuration_done()
        threads = await client.threads()
        assert isinstance(threads, list)

        frames = await client.stack_trace()
        assert frames == [], "expected no frames while nothing is running"
    finally:
        await client.disconnect()
