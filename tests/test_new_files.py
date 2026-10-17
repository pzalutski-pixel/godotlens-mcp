"""A class_name file created while the editor runs, against real headless editors.

The language server learns a new class only from the editor's filesystem scan, which
runs when the window gains focus or, with "Import Resources When Unfocused" on, every
0.5 s. An agent-driven editor is never focused. Each test writes files with names of
its own, because both editors are shared by the session.
"""

import uuid

import pytest

pytestmark = pytest.mark.integration


def _write_class_and_user(project):
    tag = uuid.uuid4().hex[:8]
    cls = f"Fresh{tag}"
    (project / f"fresh_{tag}.gd").write_text(f"class_name {cls}\nextends Node\n", encoding="utf-8")
    (project / f"uses_{tag}.gd").write_text(
        f"extends Node\n\n\nfunc _ready() -> void:\n\tvar thing := {cls}.new()\n\tprint(thing)\n",
        encoding="utf-8")
    return cls, f"fresh_{tag}.gd", f"uses_{tag}.gd"


def _errors(entries):
    return [d for d in entries if d["severity"] == 1]


def test_a_new_class_is_recognised_when_the_editor_rescans(mcp_process, live_lsp_rescanning):
    project = live_lsp_rescanning.project
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(live_lsp_rescanning.lsp_port)})
    proc.initialize()
    _cls, declaring, using = _write_class_and_user(project)

    payload, is_error = proc.call_tool("gdscript_sync_files", {"files": [declaring, using]})

    assert not is_error, payload
    assert payload["verified"] is True
    assert _errors(payload["diagnostics"][using]) == [], payload
    assert "hint" not in payload


def test_without_a_rescan_the_agent_is_told_why_and_how_to_fix_it(mcp_process, live_lsp):
    project = live_lsp.project
    proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": str(live_lsp.lsp_port)})
    proc.initialize()
    cls, declaring, using = _write_class_and_user(project)

    payload, is_error = proc.call_tool("gdscript_sync_files", {"files": [declaring, using]})

    assert not is_error, payload
    assert cls in payload["unregistered_classes"], payload
    assert "Import Resources When Unfocused" in payload["hint"]
    assert any(cls in d["message"] for d in _errors(payload["diagnostics"][using]))
