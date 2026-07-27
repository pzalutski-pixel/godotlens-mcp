"""Integration tests against a real Godot editor running headless.

Skipped automatically when no Godot binary is available (set GODOT_BIN, or drop one
in ./godot/). These are the only tests that prove what Godot actually does, as
opposed to what we believe it does.

Verified against Godot 4.7.1.
"""

import pytest

from godotlens_mcp.lsp_client import LSPClient, canonical_key, file_uri, uri_to_path

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _client(port: int) -> LSPClient:
    client = LSPClient(port=port, timeout=30.0)
    ok, msg = await client.connect()
    assert ok, msg
    return client


async def _open(client: LSPClient, path) -> str:
    uri = file_uri(str(path))
    await client.notify("textDocument/didOpen", {"textDocument": {
        "uri": uri, "languageId": "gdscript", "version": 1,
        "text": path.read_text(encoding="utf-8")}})
    await client.drain_notifications(1.0)
    return uri


async def test_headless_lsp_answers_and_reports_capabilities(live_lsp):
    """--editor --headless --lsp-port brings the language server up with no GUI."""
    port, _ = live_lsp
    client = await _client(port)
    try:
        caps = client.server_capabilities
        assert caps.get("referencesProvider") is True
        assert caps.get("definitionProvider") is True
        # Godot returns no serverInfo/version, which is why capability detection
        # (rather than version branching) is the only workable strategy.
        assert "serverInfo" not in caps
    finally:
        await client.disconnect()


async def test_native_class_documentation_is_pushed_on_connect(live_lsp):
    """Godot pushes its whole native class list; we used to discard it."""
    port, _ = live_lsp
    client = await _client(port)
    try:
        classes = client.native_capabilities.get("native_classes", [])
        assert len(classes) > 500, f"expected the full engine class list, got {len(classes)}"
    finally:
        await client.disconnect()


async def test_diagnostics_reach_the_cache_despite_uri_encoding(live_lsp):
    """The headline regression: Godot 4.5+ percent-encodes published URIs.

    Keying the cache on the raw URI string meant this returned zero diagnostics for a
    file with a genuine syntax error, and the tool reported success.
    """
    port, project = live_lsp
    client = await _client(port)
    try:
        broken = project / "broken.gd"
        await _open(client, broken)

        diags = client.diagnostics_cache.get(canonical_key(str(broken)), [])
        assert diags, "no diagnostics cached for a file with a real syntax error"
        assert any("Expected" in d.get("message", "") for d in diags)
    finally:
        await client.disconnect()


async def test_references_are_cross_file_and_ignore_comments(live_lsp):
    """The project's core value claim, checked against a planted decoy comment."""
    port, project = live_lsp
    client = await _client(port)
    try:
        player = project / "player.gd"
        await _open(client, player)
        await _open(client, project / "enemy.gd")

        lines = player.read_text(encoding="utf-8").split("\n")
        line_no = next(i for i, ln in enumerate(lines) if ln.startswith("func take_damage"))
        col = lines[line_no].index("take_damage") + 3

        refs = await client.request("textDocument/references", {
            "textDocument": {"uri": file_uri(str(player))},
            "position": {"line": line_no, "character": col},
            "context": {"includeDeclaration": True}})

        names = {uri_to_path(r["uri"]).rsplit("/", 1)[-1] for r in refs or []}
        assert "enemy.gd" in names, f"references were not cross-file: {names}"
        assert "player.gd" in names

        # enemy.gd contains "take_damage" inside a comment; a grep would match it.
        enemy_lines = (project / "enemy.gd").read_text(encoding="utf-8").split("\n")
        comment_lines = {i for i, ln in enumerate(enemy_lines) if ln.strip().startswith("#")}
        hit_lines = {r["range"]["start"]["line"] for r in refs
                     if uri_to_path(r["uri"]).endswith("enemy.gd")}
        assert not (hit_lines & comment_lines), "matched a comment, so this is not semantic"
    finally:
        await client.disconnect()


async def test_references_cannot_see_scene_connections(live_lsp):
    """Documents the blind spot that makes renaming a signal handler dangerous.

    Godot's find_all_usages collects .gd files only, so the [connection] entry in
    main.tscn is invisible. If this ever starts failing, Godot gained scene awareness
    and the scene-verification layer can be reconsidered.
    """
    port, project = live_lsp
    client = await _client(port)
    try:
        player = project / "player.gd"
        await _open(client, player)

        lines = player.read_text(encoding="utf-8").split("\n")
        line_no = next(i for i, ln in enumerate(lines) if "_on_hit_area_entered" in ln and ln.startswith("func"))
        col = lines[line_no].index("_on_hit_area_entered") + 5

        refs = await client.request("textDocument/references", {
            "textDocument": {"uri": file_uri(str(player))},
            "position": {"line": line_no, "character": col},
            "context": {"includeDeclaration": True}}) or []

        assert not any(".tscn" in r["uri"] for r in refs)
        assert "_on_hit_area_entered" in (project / "main.tscn").read_text(encoding="utf-8")
    finally:
        await client.disconnect()


async def test_resync_survives_the_4_6_did_open_guard(live_lsp, tmp_path):
    """Syncing the same file twice must not poison the document.

    Godot 4.6+ has ERR_FAIL_COND_MSG(managed_files.has(path)) in lsp_did_open and
    returns *before* reparsing, so a second didOpen leaves the first text cached for
    the life of the connection and every later query answers from stale source.
    sync_document() switches to didChange for an already-open file.
    """
    port, project = live_lsp
    scratch = project / "resync_probe.gd"
    scratch.write_text("extends Node\n\n\nfunc first_name() -> void:\n\tpass\n", encoding="utf-8")

    client = await _client(port)
    try:
        uri = file_uri(str(scratch))
        await client.sync_document(uri, scratch.read_text(encoding="utf-8"))
        await client.wait_for_diagnostics([canonical_key(str(scratch))], timeout=15.0)

        symbols = await client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        names = _symbol_names(symbols)
        assert "first_name" in names, f"initial sync failed: {names}"

        # Now rewrite the file and re-sync, exactly as an agent editing code would.
        updated = "extends Node\n\n\nfunc second_name() -> void:\n\tpass\n"
        scratch.write_text(updated, encoding="utf-8")
        assert await client.sync_document(uri, updated) == "changed"

        symbols = await client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        names = _symbol_names(symbols)
        assert "second_name" in names, f"LSP served stale text after re-sync: {names}"
        assert "first_name" not in names
    finally:
        await client.disconnect()


def _symbol_names(symbols) -> set[str]:
    found = set()

    def walk(nodes):
        for node in nodes or []:
            found.add(node.get("name", ""))
            walk(node.get("children"))

    walk(symbols)
    return found


async def test_unsupported_method_errors_without_killing_the_connection(live_lsp):
    """workspace/* was removed in Godot 4.6; it must fail loudly and stay usable."""
    from godotlens_mcp.lsp_client import LSPError

    port, project = live_lsp
    client = await _client(port)
    try:
        with pytest.raises(LSPError) as excinfo:
            await client.request("workspace/didDeleteFiles", {"files": []})
        assert excinfo.value.is_method_not_found

        player = project / "player.gd"
        await _open(client, player)
        symbols = await client.request("textDocument/documentSymbol", {
            "textDocument": {"uri": file_uri(str(player))}})
        assert symbols, "connection was unusable after an LSP error"
    finally:
        await client.disconnect()
