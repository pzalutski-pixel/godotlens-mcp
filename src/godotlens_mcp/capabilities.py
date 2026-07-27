"""Capability detection for Godot's language server.

Godot returns no ``serverInfo`` and no version field from ``initialize`` (verified
against 4.7.1), so there is nothing to branch on directly. Instead we read what the
server advertises and probe where that is ambiguous.

Detection is by capability, not by version number, so a future Godot that adds a
method starts working automatically, and one that removes a method disables the
affected tool rather than returning a wrong answer.

One trap this deliberately avoids: Godot <= 4.4 *lies*. It advertises
``workspaceSymbolProvider: true`` for a method that has never existed in any 4.x,
corrected to ``false`` in 4.5. Advertised flags are therefore used to identify the
server, never as a feature gate on their own — a method is considered usable only
once it has answered, and any ``-32601`` permanently marks it unsupported.
"""

from __future__ import annotations

import os

# Behaviour splits across the 4.x line. 4.6 is the supported floor: it is where the
# model stabilises, and 4.7 only adds to it.
#   <=4.4  advertises workspaceSymbolProvider: true (a lie), no URI percent-encoding
#    4.5   URI percent-encoding arrives; workspaceSymbolProvider corrected to false
#    4.6   didOpen guard added; the whole workspace/* namespace removed
#    4.7   documentHighlight added
MINIMUM_SUPPORTED = "4.6"


class Capabilities:
    """What this Godot instance can actually do."""

    def __init__(self, advertised: dict | None = None):
        self.advertised: dict = advertised or {}
        # Methods proven unsupported by a -32601 at runtime. Authoritative: a method
        # that answered with "method not found" cannot be talked into working.
        self._unsupported: set[str] = set()
        self.version_hint: str | None = os.environ.get("GODOT_VERSION")

    # -- identification ----------------------------------------------------

    @property
    def claims_workspace_symbol(self) -> bool:
        """True only on Godot <= 4.4, where the flag is set but the method is absent."""
        return self.advertised.get("workspaceSymbolProvider") is True

    @property
    def has_document_highlight(self) -> bool:
        """documentHighlight arrived in 4.7."""
        return self.advertised.get("documentHighlightProvider") is True

    @property
    def looks_pre_4_5(self) -> bool:
        return self.claims_workspace_symbol

    @property
    def below_minimum(self) -> bool:
        """Detect a server older than the supported floor.

        Only <=4.4 is positively identifiable from the advertised set, via the
        workspaceSymbolProvider lie. 4.5 is not distinguishable from 4.6 without a
        probe, and the practical difference for us is the workspace/* namespace,
        which is handled by graceful degradation anyway.
        """
        if self.version_hint:
            return _version_tuple(self.version_hint) < _version_tuple(MINIMUM_SUPPORTED)
        return self.looks_pre_4_5

    # -- per-method support ------------------------------------------------

    def supports(self, method: str) -> bool:
        if method in self._unsupported:
            return False
        flag = _CAPABILITY_FLAGS.get(method)
        if flag is None:
            return True  # nothing advertised either way; try it and find out
        # Only trust a *negative* advertised flag. Positives are unreliable on <=4.4.
        return self.advertised.get(flag) is not False

    def mark_unsupported(self, method: str) -> None:
        """Record a -32601 so we stop re-asking and can explain the refusal."""
        self._unsupported.add(method)

    def describe(self) -> dict:
        return {
            "minimum_supported_godot": MINIMUM_SUPPORTED,
            "version_hint": self.version_hint,
            "document_highlight": self.has_document_highlight,
            "below_minimum": self.below_minimum,
            "known_unsupported": sorted(self._unsupported),
            "advertised": self.advertised,
        }


# LSP method -> the ServerCapabilities key that would disable it.
_CAPABILITY_FLAGS = {
    "textDocument/definition": "definitionProvider",
    "textDocument/references": "referencesProvider",
    "textDocument/hover": "hoverProvider",
    "textDocument/documentSymbol": "documentSymbolProvider",
    "textDocument/documentHighlight": "documentHighlightProvider",
    "textDocument/documentLink": "documentLinkProvider",
    "textDocument/completion": "completionProvider",
    "textDocument/signatureHelp": "signatureHelpProvider",
    "textDocument/rename": "renameProvider",
    "textDocument/prepareRename": "renameProvider",
}

# Methods removed from Godot in 4.6 along with the whole workspace/* namespace. They
# are notifications in normal use, and a notification's METHOD_NOT_FOUND is discarded
# by the server — which is exactly how gdscript_delete_file shipped reporting success
# for a total no-op. Never probe these with a notification.
REMOVED_IN_4_6 = frozenset({
    "workspace/didDeleteFiles",
    "workspace/didCreateFiles",
    "workspace/didRenameFiles",
    "workspace/didChangeWatchedFiles",
    "workspace/symbol",
})


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)
