"""What the language server holds for a file after a tool call.

The LSP answers from the document a client opened, not from disk, so a tool that
leaves temporary content open poisons every later query on that file. With several
agents sharing one GodotLens, the agent that pays for it is usually not the one that
caused it.
"""

import asyncio
import json
import os

import pytest

from godotlens_mcp import server
from godotlens_mcp.lsp_client import LSPClient, file_uri

pytestmark = pytest.mark.asyncio

ON_DISK = "extends Node\n\n\nfunc on_disk() -> void:\n\tpass\n"
DRAFT = "extends Node\n\n\nfunc rejected_draft() -> void:\n\tpass\n"


@pytest.fixture
async def lsp(fake_lsp, monkeypatch):
    client = LSPClient(port=fake_lsp.port, timeout=5.0)
    assert (await client.connect())[0]
    monkeypatch.setattr(server, "_lsp", client)
    monkeypatch.setattr(server, "DIAGNOSTICS_TIMEOUT", 0.3)  # the fake never publishes
    try:
        yield client
    finally:
        await client.disconnect()


def _document_messages(fake_lsp):
    """(method, text) for every document notification the client sent."""
    sent = []
    for message in fake_lsp.received:
        method = message.get("method", "")
        if not method.startswith("textDocument/did"):
            continue
        params = message.get("params", {})
        text = params.get("textDocument", {}).get("text")
        if text is None and params.get("contentChanges"):
            text = params["contentChanges"][0].get("text")
        sent.append((method, text))
    return sent


async def test_validate_releases_a_file_it_had_to_open(lsp, fake_lsp, tmp_path):
    """Regression: the draft stayed open, so later queries answered from it."""
    path = tmp_path / "player.gd"
    path.write_text(ON_DISK, encoding="utf-8")

    await server.handle_tool_call("gdscript_validate", {"file": str(path), "content": DRAFT})
    await fake_lsp.wait_for_method("textDocument/didClose")

    assert not lsp.is_open(file_uri(str(path)))
    assert _document_messages(fake_lsp) == [
        ("textDocument/didOpen", DRAFT),
        ("textDocument/didClose", None),
    ]


async def test_validate_restores_a_file_that_was_already_open(lsp, fake_lsp, tmp_path):
    path = tmp_path / "player.gd"
    path.write_text(ON_DISK, encoding="utf-8")
    await lsp.sync_document(file_uri(str(path)), ON_DISK, notify_save=False)

    await server.handle_tool_call("gdscript_validate", {"file": str(path), "content": DRAFT})
    await fake_lsp.wait_for_method("textDocument/didChange", count=2)

    assert lsp.is_open(file_uri(str(path)))
    assert _document_messages(fake_lsp)[-1] == ("textDocument/didChange", ON_DISK)


async def test_a_concurrent_query_never_sees_another_calls_draft(lsp, fake_lsp, tmp_path):
    """Tool calls run concurrently; validate's draft window must stay closed to others."""
    path = tmp_path / "player.gd"
    path.write_text(ON_DISK, encoding="utf-8")

    validating = asyncio.get_event_loop().create_task(server.handle_tool_call(
        "gdscript_validate", {"file": str(path), "content": DRAFT}))
    await fake_lsp.wait_for_method("textDocument/didOpen")  # the draft is now open
    await server.handle_tool_call("gdscript_symbols", {"file": str(path)})
    await validating

    order = [m.get("method") for m in fake_lsp.received]
    assert order.index("textDocument/documentSymbol") > order.index("textDocument/didClose"), \
        f"the query ran while the draft was open: {order}"


# ---------------------------------------------------------------------------
# A class_name file the editor has not registered yet
# ---------------------------------------------------------------------------
#
# The language server learns a new class only from the editor's filesystem scan,
# which runs on window focus or, with "Import Resources When Unfocused", every 0.5 s.

FOO_GD = "class_name Foo\nextends Node\n"
USER_GD = "extends Node\n\n\nfunc _ready() -> void:\n\tFoo.new()\n"
UNDECLARED = 'Identifier "Foo" not declared in the current scope.'


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (tmp_path / "foo.gd").write_text(FOO_GD, encoding="utf-8")
    (tmp_path / "user.gd").write_text(USER_GD, encoding="utf-8")
    monkeypatch.setenv("GODOT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "NEW_CLASS_WAIT", 1.5)
    return tmp_path


def _publish_for(path, unresolved_until: int):
    """A Godot that reports Foo undeclared for the first ``unresolved_until`` parses."""
    parses = {"n": 0}

    def hook(message):
        params = message.get("params", {})
        uri = params.get("textDocument", {}).get("uri", "")
        if message.get("method") not in {"textDocument/didOpen", "textDocument/didChange"} \
                or not uri.endswith(path):
            return []
        parses["n"] += 1
        unresolved = parses["n"] <= unresolved_until
        diagnostics = [{"range": {"start": {"line": 4}}, "severity": 1,
                        "message": UNDECLARED}] if unresolved else []
        return [{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                 "params": {"uri": uri, "diagnostics": diagnostics}}]
    return hook


async def _sync_user(lsp, project):
    result = await server.handle_tool_call("gdscript_sync_file", {"file": str(project / "user.gd")})
    return json.loads(result["content"][0]["text"])


async def test_an_unregistered_class_gets_a_hint_naming_the_setting(lsp, fake_lsp, project):
    fake_lsp.notification_hook = _publish_for("user.gd", unresolved_until=99)
    payload = await _sync_user(lsp, project)
    assert payload["unregistered_classes"] == {"Foo": "res://foo.gd"}
    assert "Import Resources When Unfocused" in payload["hint"]
    assert payload["diagnostics"][0]["message"] == UNDECLARED


async def test_a_class_the_editor_registers_meanwhile_is_rechecked(lsp, fake_lsp, project):
    fake_lsp.notification_hook = _publish_for("user.gd", unresolved_until=1)
    payload = await _sync_user(lsp, project)
    assert payload["diagnostics"] == [], "dependants were not re-checked after the rescan"
    assert payload["new_classes_registered"] == ["Foo"]
    assert payload["verified"] is True
    assert "hint" not in payload


async def test_a_missed_folder_is_nudged_once_without_leaving_anything(lsp, fake_lsp, project, monkeypatch):
    """Godot keeps folder times in whole seconds, so a scan can miss a new file; changing
    the folder again makes the next scan re-read it."""
    nudged = []
    original = server._nudge_folder
    monkeypatch.setattr(server, "_nudge_folder", lambda folder: (nudged.append(folder), original(folder)))
    fake_lsp.notification_hook = _publish_for("user.gd", unresolved_until=99)

    await _sync_user(lsp, project)
    assert [os.path.normcase(f) for f in nudged] == [os.path.normcase(str(project))], "nudged exactly once"
    assert not (project / server.RESCAN_MARKER).exists(), "the marker must not be left behind"


async def test_a_class_registered_at_once_needs_no_nudge(lsp, fake_lsp, project, monkeypatch):
    nudged = []
    monkeypatch.setattr(server, "_nudge_folder", nudged.append)
    fake_lsp.notification_hook = _publish_for("user.gd", unresolved_until=1)
    await _sync_user(lsp, project)
    assert nudged == []


async def test_a_real_typo_is_not_waited_on(lsp, fake_lsp, project):
    """No project file declares the name, so it is the agent's typo, not a new file."""
    (project / "foo.gd").write_text("extends Node\n", encoding="utf-8")
    fake_lsp.notification_hook = _publish_for("user.gd", unresolved_until=99)

    started = asyncio.get_event_loop().time()
    payload = await _sync_user(lsp, project)
    assert asyncio.get_event_loop().time() - started < 1.0
    assert "hint" not in payload and "unregistered_classes" not in payload
    assert payload["diagnostics"][0]["message"] == UNDECLARED


async def test_validate_of_a_new_file_leaves_nothing_open(lsp, fake_lsp, tmp_path):
    """Content proposed for a file that does not exist yet must not linger either."""
    path = tmp_path / "not_written_yet.gd"

    await server.handle_tool_call("gdscript_validate", {"file": str(path), "content": DRAFT})
    await fake_lsp.wait_for_method("textDocument/didClose")
    assert not lsp.is_open(file_uri(str(path)))
