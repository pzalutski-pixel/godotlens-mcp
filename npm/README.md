# godotlens-mcp

[![npm](https://img.shields.io/npm/v/godotlens-mcp.svg)](https://www.npmjs.com/package/godotlens-mcp)
[![PyPI](https://img.shields.io/pypi/v/godotlens-mcp.svg)](https://pypi.org/project/godotlens-mcp/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/pzalutski-pixel/godotlens-mcp/blob/main/LICENSE)

An MCP server that gives AI agents **Godot's own view** of a GDScript project — navigation,
diagnostics, engine API and scene verification from the engine itself — and lets them run the
game, look at it and drive it: the live scene tree, values, errors, input, hot reload and
screenshots.

## Requirements

| | Needed for |
|---|---|
| **Godot 4.6+**, editor open on your project | `gdscript_*` and `debug_*` (the LSP and debug adapter live in the editor) |
| A Godot **binary** via `GODOT_BIN`, `./godot/`, or PATH | `scene_*`, `project_*` and `runtime_*`, which run the engine themselves |
| **Python 3.10+** on PATH | this npm wrapper spawns the bundled server |

Godot 4.6 is the floor because the language server's behaviour changed materially at 4.5
(URI encoding) and 4.6 (document ownership). Older versions are refused with a clear
message rather than silently misread.

Turn on **Import Resources When Unfocused** in Editor Settings: an agent-driven editor never
gains focus, so without it new `class_name` scripts and assets are not registered. Games that
`runtime_run` launches run headless, beside your editor, one per agent. For CI, the editor
does not need a visible window:

```bash
godot --path <project> --editor --headless --lsp-port 6005 --dap-port 6006
```

## Quick Start

```json
{
  "mcpServers": {
    "godotlens": {
      "command": "npx",
      "args": ["-y", "godotlens-mcp"]
    }
  }
}
```

## What this package does

Bundles the full server (pure Python, **zero runtime dependencies**), checks for Python 3.10+,
and launches it over stdio. No pip install, no network access after install.

## Why

An agent working from text alone cannot tell a call from a comment, cannot know which methods
exist on a `CharacterBody2D` in *your* Godot version, and cannot see that a signal handler is
wired by name inside a `.tscn`. Every answer here comes from the engine.

| Without | With |
|---|---|
| `grep take_damage` — matches comments and strings | `gdscript_references` — the actual call sites |
| Guess a coordinate, land one column off, get nothing | `gdscript_find` — look a symbol up by name |
| Recall Godot's API from memory | `gdscript_engine_api` — signatures from *your* build |
| Rename a signal handler, break the scene silently | `gdscript_rename` warns; `scene_validate` catches it |
| Paste console output back by hand | `debug_run` — run it and read what it printed |

## Tools

49 tools. Line and character parameters are **0-indexed** throughout.

- **Navigation** — `gdscript_find` (by name), `gdscript_definition`, `gdscript_references`,
  `gdscript_references_in_file`, `gdscript_hover`, `gdscript_symbols`, `gdscript_signature_help`
- **Authoring** — `gdscript_engine_api`, `gdscript_complete` (scene-aware), `gdscript_validate`
  (check content before writing it to disk)
- **Refactoring** — `gdscript_rename`
- **Project & scenes** — `project_config`, `project_validate`, `scene_state`, `scene_validate`
- **Debugger (your editor)** — `debug_run`, `debug_output`, `debug_set_breakpoints`,
  `debug_stack_trace`, `debug_inspect`, `debug_evaluate`, plus execution control
- **Runtime (games GodotLens launches)** — `runtime_run`, `runtime_tree`, `runtime_inspect`,
  `runtime_set`, `runtime_eval`, `runtime_input`, `runtime_output`, `runtime_reload`, `runtime_screenshot`, `runtime_monitors`,
  `runtime_pause`, `runtime_time_scale`, `runtime_step`, `runtime_status`, `runtime_stop`
- **Sync & diagnostics** — `gdscript_sync_file`, `gdscript_sync_files`, `gdscript_diagnostics`,
  `gdscript_release_file`, batch variants

Godot's LSP does not watch the filesystem, so call `gdscript_sync_file` after editing a `.gd`
file. Results carry a `verified` flag: an empty diagnostics list with `verified: false` means
Godot never reported back — **not** that the file is clean.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GODOT_LSP_HOST` | `127.0.0.1` | Language server host |
| `GODOT_LSP_PORT` | `6005` | Language server port (the official VS Code extension uses `6008`) |
| `GODOT_DAP_HOST` | `127.0.0.1` | Debug adapter host |
| `GODOT_DAP_PORT` | `6006` | Debug adapter port, used by `debug_*` |
| `GODOT_BIN` | auto | Godot executable, required by `scene_*` and `project_config` |
| `GODOT_PROJECT_ROOT` | auto | Project root; auto-detected by walking up for `project.godot` |
| `GODOT_LSP_TIMEOUT` | `15` | Seconds to wait for any single LSP response |
| `GODOT_DIAGNOSTICS_TIMEOUT` | `8` | Seconds to wait for diagnostics after a sync |
| `GODOT_VERSION` | auto | Override capability detection |
| `GODOT_FIND_FILE_LIMIT` | `60` | Max files `gdscript_find` inspects when searching the whole project |
| `GODOT_RUNTIME_MAX_SESSIONS` | `4` | Max games `runtime_run` keeps running at once |

## Documentation

Full tool reference and rationale: [GitHub](https://github.com/pzalutski-pixel/godotlens-mcp)
