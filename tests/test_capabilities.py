"""Capability detection and stateful document sync."""

import pytest

from godotlens_mcp.capabilities import MINIMUM_SUPPORTED, REMOVED_IN_4_6, Capabilities

# Advertised sets observed from real editors. 4.7.1 was captured directly; the <=4.4
# shape is characterised by the workspaceSymbolProvider lie.
CAPS_4_7 = {
    "definitionProvider": True,
    "referencesProvider": True,
    "hoverProvider": True,
    "documentSymbolProvider": True,
    "documentHighlightProvider": True,
    "workspaceSymbolProvider": False,
    "renameProvider": {"prepareProvider": True},
    "foldingRangeProvider": False,
    "colorProvider": False,
}
CAPS_4_4 = {
    "definitionProvider": True,
    "referencesProvider": True,
    "hoverProvider": True,
    "documentSymbolProvider": True,
    "workspaceSymbolProvider": True,  # the lie: the method has never existed
}


def test_detects_document_highlight_on_4_7():
    assert Capabilities(CAPS_4_7).has_document_highlight is True


def test_absent_document_highlight_on_older_editors():
    assert Capabilities(CAPS_4_4).has_document_highlight is False


def test_pre_4_5_identified_by_the_workspace_symbol_lie():
    """Godot <=4.4 advertises a method it does not implement; 4.5 corrected it."""
    assert Capabilities(CAPS_4_4).looks_pre_4_5 is True
    assert Capabilities(CAPS_4_7).looks_pre_4_5 is False


def test_below_minimum_rejects_pre_4_5_editors():
    assert Capabilities(CAPS_4_4).below_minimum is True
    assert Capabilities(CAPS_4_7).below_minimum is False


def test_version_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("GODOT_VERSION", "4.3.1")
    assert Capabilities(CAPS_4_7).below_minimum is True
    monkeypatch.setenv("GODOT_VERSION", MINIMUM_SUPPORTED)
    assert Capabilities(CAPS_4_7).below_minimum is False


def test_negative_advertised_flag_disables_a_method():
    caps = Capabilities({"documentHighlightProvider": False})
    assert caps.supports("textDocument/documentHighlight") is False


def test_positive_advertised_flag_is_not_trusted_alone():
    """<=4.4 advertised workspaceSymbolProvider: true for a method that never existed.

    Advertised positives therefore identify the server; they never gate a feature on
    their own. Support is only ever *withdrawn* by evidence, never granted by a flag.
    """
    caps = Capabilities(CAPS_4_4)
    assert caps.supports("workspace/symbol") is True  # optimistic before evidence
    caps.mark_unsupported("workspace/symbol")
    assert caps.supports("workspace/symbol") is False  # -32601 is authoritative


def test_unknown_methods_are_attempted():
    """A future Godot that adds a method should work without a code change."""
    assert Capabilities(CAPS_4_7).supports("textDocument/somethingNew") is True


def test_method_not_found_is_remembered():
    caps = Capabilities(CAPS_4_7)
    caps.mark_unsupported("textDocument/documentLink")
    assert caps.supports("textDocument/documentLink") is False
    assert "textDocument/documentLink" in caps.describe()["known_unsupported"]


def test_workspace_namespace_is_known_removed():
    assert "workspace/didDeleteFiles" in REMOVED_IN_4_6
    assert "workspace/symbol" in REMOVED_IN_4_6


# ---------------------------------------------------------------------------
# Stateful document sync
# ---------------------------------------------------------------------------

pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_first_sync_opens_and_resync_changes(connected_client, fake_lsp):
    """Godot 4.6+ rejects a repeat didOpen and returns before reparsing.

    The stale first text then keeps being served for the life of the connection, so a
    re-sync must use didChange instead.
    """
    uri = "file:///C:/proj/a.gd"
    assert await connected_client.sync_document(uri, "extends Node") == "opened"
    assert await connected_client.sync_document(uri, "extends Node2D") == "changed"
    assert await connected_client.sync_document(uri, "extends Control") == "changed"

    await fake_lsp.wait_for_method("textDocument/didChange", 2)
    assert fake_lsp.count("textDocument/didOpen") == 1, "sent didOpen more than once"
    assert fake_lsp.count("textDocument/didChange") == 2


@pytest.mark.asyncio
async def test_didchange_uses_full_sync_and_increments_version(connected_client, fake_lsp):
    uri = "file:///C:/proj/a.gd"
    await connected_client.sync_document(uri, "one")
    await connected_client.sync_document(uri, "two")
    await fake_lsp.wait_for_method("textDocument/didChange", 1)

    change = next(m for m in fake_lsp.received if m.get("method") == "textDocument/didChange")
    changes = change["params"]["contentChanges"]
    assert len(changes) == 1
    assert changes[0] == {"text": "two"}, "must be a full-text replacement, not a range edit"
    assert change["params"]["textDocument"]["version"] == 2


@pytest.mark.asyncio
async def test_close_allows_a_fresh_open(connected_client, fake_lsp):
    uri = "file:///C:/proj/a.gd"
    await connected_client.sync_document(uri, "one")
    assert connected_client.is_open(uri) is True

    assert await connected_client.close_document(uri) is True
    assert connected_client.is_open(uri) is False

    assert await connected_client.sync_document(uri, "two") == "opened"
    await fake_lsp.wait_for_method("textDocument/didOpen", 2)
    assert fake_lsp.count("textDocument/didOpen") == 2
    assert fake_lsp.count("textDocument/didClose") == 1


@pytest.mark.asyncio
async def test_closing_an_unopened_document_is_a_no_op(connected_client, fake_lsp):
    assert await connected_client.close_document("file:///C:/proj/never.gd") is False
    assert fake_lsp.count("textDocument/didClose") == 0


@pytest.mark.asyncio
async def test_reconnect_forgets_open_documents(connected_client, fake_lsp):
    """Document ownership is per-connection, so a reconnect must start clean."""
    uri = "file:///C:/proj/a.gd"
    await connected_client.sync_document(uri, "one")
    await connected_client.disconnect()
    assert connected_client.is_open(uri) is False


@pytest.mark.asyncio
async def test_diagnostics_only_check_skips_did_save(connected_client, fake_lsp):
    """didSave triggers a real script hot-reload in the editor; a read-only check
    must not cause one."""
    uri = "file:///C:/proj/a.gd"
    await connected_client.sync_document(uri, "extends Node", notify_save=False)
    await fake_lsp.wait_for_method("textDocument/didOpen", 1)
    assert fake_lsp.count("textDocument/didSave") == 0


@pytest.mark.asyncio
async def test_capabilities_populated_from_initialize(fake_lsp):
    from godotlens_mcp.lsp_client import LSPClient

    fake_lsp.responses["initialize"] = {"capabilities": CAPS_4_7}
    client = LSPClient(port=fake_lsp.port, timeout=5.0)
    assert (await client.connect())[0]
    try:
        assert client.capabilities.has_document_highlight is True
        assert client.capabilities.below_minimum is False
    finally:
        await client.disconnect()
