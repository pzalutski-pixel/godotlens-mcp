# GodotLens: AI-First Code Analysis for GDScript

[![GitHub Release](https://img.shields.io/github/v/release/pzalutski-pixel/godotlens-mcp)](https://github.com/pzalutski-pixel/godotlens-mcp/releases)
[![npm](https://img.shields.io/npm/v/godotlens-mcp)](https://www.npmjs.com/package/godotlens-mcp)
[![PyPI](https://img.shields.io/pypi/v/godotlens-mcp)](https://pypi.org/project/godotlens-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP server that gives AI agents Godot's own view of a GDScript project — navigation,
diagnostics, engine API, and scene verification — powered by Godot's built-in Language Server.

## Built for AI Agents

AI coding agents work with text files but lack semantic understanding of GDScript. When an agent uses `grep` to find usages of a function, it cannot distinguish a function call from a comment containing the same name, a signal declaration from a signal emission, or an overridden method from an unrelated function.

GodotLens bridges this gap by exposing Godot's built-in Language Server through the Model Context Protocol (MCP), giving AI agents compiler-accurate code intelligence for GDScript — go to definition, find references, diagnostics, rename, and more.

**Example**, measured against Godot 4.7.1 on a project where `take_damage` is defined in
`player.gd`, called twice from `enemy.gd`, once from `player.gd` itself, and mentioned in a
comment:

| Approach | Result |
|----------|--------|
| `grep "take_damage"` | 5 matches, including the comment |
| `gdscript_references` | Exactly 4 real call sites; the comment is not among them |

### What the LSP cannot see

Godot's reference search reads `.gd` files only. A signal handler wired in a `.tscn`
`[connection]` block is invisible to it, so renaming that handler leaves the scene pointing at a
method that no longer exists — and that fails at runtime with no compile error anywhere. This is
why `scene_state` and `scene_validate` exist, and why `gdscript_rename` warns when a name also
appears in scene files.

## Prerequisites

- **Godot 4.6+** running with your project open — the LSP starts automatically when the editor
  opens a project. Older 4.x releases are refused rather than silently misread: behaviour changed
  materially at 4.5 (URI encoding) and 4.6 (document ownership).
- **Python 3.10+** (for pip install) or **Node.js 16+** (for npx)
- A Godot **binary** for the `scene_*` tools, which run the engine to resolve scenes. Found via
  `GODOT_BIN`, a `./godot/` directory, or `PATH`.

> The editor does not need a window. `godot --path <project> --editor --headless --lsp-port 6005`
> serves the LSP with no GUI, which is what the integration tests use in CI.

## Quick Start

### Option A: npx (recommended for MCP clients)

Add to your MCP configuration (e.g., `.mcp.json` for Claude Code):

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

The npm package bundles the full server (~20 KB of Python). Zero external Python dependencies.

### Option B: pip

```bash
pip install godotlens-mcp
```

```json
{
  "mcpServers": {
    "godotlens": {
      "command": "godotlens-mcp"
    }
  }
}
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `GODOT_LSP_HOST` | `127.0.0.1` | Godot LSP server host |
| `GODOT_LSP_PORT` | `6005` | Godot LSP server port. The official VS Code extension defaults to `6008` |
| `GODOT_LSP_TIMEOUT` | `15` | Seconds to wait for any single LSP response |
| `GODOT_DIAGNOSTICS_TIMEOUT` | `8` | Seconds to wait for Godot to publish diagnostics after a sync |
| `GODOT_PROJECT_ROOT` | auto | Project root. Auto-detected by walking up for `project.godot` |
| `GODOT_BIN` | auto | Godot executable, required by the `scene_*` tools |
| `GODOT_VERSION` | auto | Override capability detection |
| `GODOT_DAP_HOST` | `127.0.0.1` | Godot debug adapter host |
| `GODOT_DAP_PORT` | `6006` | Godot debug adapter port, used by the `debug_*` tools |

## Tools

All line and character parameters are **0-indexed**, matching the LSP specification.

### Health

| Tool | Description |
|------|-------------|
| `gdscript_status` | Check the connection to Godot. Use first if anything is behaving oddly. |

### Navigation

| Tool | Description |
|------|-------------|
| `gdscript_definition` | Where a symbol is defined. |
| `gdscript_references` | Every reference project-wide. On Godot 4.6+ this reparses every `.gd` file, so it is not cheap. |
| `gdscript_references_in_file` | Occurrences within one file, via `documentHighlight`. Much cheaper. Godot 4.7+. |
| `gdscript_hover` | Type information and documentation for a symbol. |
| `gdscript_symbols` | The symbol tree of a file. |
| `gdscript_signature_help` | Parameter info at a call site. |

### Authoring

| Tool | Description |
|------|-------------|
| `gdscript_engine_api` | Authoritative signatures and docs for an engine class or member, from the exact build in use. Use this instead of recalling Godot's API. |
| `gdscript_complete` | Valid completions at a position. The only **scene-aware** query: includes real `$NodePath` entries and the signals actually on the owning node. |
| `gdscript_validate` | Check proposed content for errors **without writing it to disk**. |

### Refactoring

| Tool | Description |
|------|-------------|
| `gdscript_rename` | Rename a symbol. Refuses when Godot will not rename it, and warns when the name also appears in scene files it cannot update. |

### Scenes

These run the Godot binary to resolve a scene the way the engine does, including
inherited scenes. They do not parse `.tscn` as text.

| Tool | Description |
|------|-------------|
| `scene_state` | Node tree, types, script attachments, `unique_name_in_owner` flags, exported values, and signal connections. |
| `scene_validate` | Verifies each connection points at a method that exists. Connections are unvalidated strings, so a stale one fails only at runtime. |

### Runtime (debugger)

Godot serves a Debug Adapter Protocol server from the editor on port 6006, with no
addon required. The language server can tell you whether code compiles; only the
debugger can tell you what it actually **did**.

| Tool | Description |
|------|-------------|
| `debug_status` | Adapter connection, capabilities, and whether the game is running or paused. |
| `debug_output` | Console output from the running game — `print`, stdout, stderr, and runtime errors with their source location. Drained on each call. |
| `debug_set_breakpoints` | Set breakpoints in a file (0-based lines, converted to DAP's 1-based on the wire). |
| `debug_stack_trace` | The call stack where execution is paused. Empty means not paused. |
| `debug_inspect` | Variables in a stack frame, by scope. |
| `debug_evaluate` | Evaluate a GDScript expression at a breakpoint, instead of adding `print` and re-running. |
| `debug_continue` / `debug_pause` / `debug_step_over` | Execution control. |
| `debug_terminate` | Stop the running game. |

Measured on Godot 4.7.1: the adapter runs headless and answers `setBreakpoints`,
`threads`, `stackTrace`, `scopes` and execution control. **Launching a game requires a
non-headless editor** — a headless editor has no display driver to run one.

### Synchronization

Godot's LSP does not watch the filesystem, so it must be told when a file changes.

| Tool | Description |
|------|-------------|
| `gdscript_sync_file` | Sync one modified file and get its diagnostics. |
| `gdscript_sync_files` | Sync several at once. |
| `gdscript_release_file` | Release a file from the LSP session so it reads from disk again. |

### Diagnostics and batch

| Tool | Description |
|------|-------------|
| `gdscript_diagnostics` | Errors and warnings for one or more files. |
| `gdscript_symbols_batch` | Symbols for several files in one call. |
| `gdscript_definitions_batch` | Definitions for several positions. |
| `gdscript_references_batch` | References for several symbols. |

Results carry a `verified` flag where it matters: `true` means Godot checked the file
and reported back, `false` means it did not answer in time. An empty diagnostics list
with `verified: false` is **not** a clean bill of health.

## Architecture

```
┌──────────────┐         ┌────────────────────┐         ┌───────────────────┐
│   AI Agent   │  stdio  │  GodotLens (MCP)   │   TCP   │  Godot Editor     │
│ (Claude, etc)├────────►│  JSON-RPC 2.0      ├────────►│  Built-in LSP     │
│              │◄────────┤  Python 3.10+      │◄────────┤  Port 6005        │
└──────────────┘         └────────────────────┘         └───────────────────┘
```

GodotLens acts as a bridge between the AI agent and Godot's built-in Language Server. The AI agent communicates with GodotLens via MCP (JSON-RPC over stdio). GodotLens translates MCP tool calls into LSP requests and sends them to the Godot editor over TCP. Responses are compacted for efficient AI consumption.

**Zero dependencies** — the server uses only the Python standard library. The MCP and LSP protocols are implemented directly, keeping the server lightweight and self-contained.

## Important: File Synchronization

Godot's LSP does not automatically detect file changes made outside the editor. When the AI agent modifies a `.gd` file, it should call `gdscript_sync_file` or `gdscript_sync_files` so the LSP re-analyzes the changed code. Without this, diagnostics and navigation results may be stale.

**Recommended workflow:**
1. Use GodotLens tools to analyze code
2. Write changes to files
3. Call `gdscript_sync_file` to refresh LSP state
4. Use GodotLens tools to verify changes

## Coordinate System

All line and character parameters are **0-indexed**, matching the LSP specification:
- Line 0, Character 0 = first character of the file

## License

MIT License — see [LICENSE](LICENSE) for details.

<!-- mcp-name: io.github.pzalutski-pixel/godotlens -->
