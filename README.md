# GodotLens: AI-First Code Analysis for GDScript

[![GitHub Release](https://img.shields.io/github/v/release/pzalutski-pixel/godotlens-mcp)](https://github.com/pzalutski-pixel/godotlens-mcp/releases)
[![npm](https://img.shields.io/npm/v/godotlens-mcp)](https://www.npmjs.com/package/godotlens-mcp)
[![PyPI](https://img.shields.io/pypi/v/godotlens-mcp)](https://pypi.org/project/godotlens-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP server providing 15 semantic analysis tools for GDScript, powered by Godot's built-in Language Server.

## Built for AI Agents

AI coding agents work with text files but lack semantic understanding of GDScript. When an agent uses `grep` to find usages of a function, it cannot distinguish a function call from a comment containing the same name, a signal declaration from a signal emission, or an overridden method from an unrelated function.

GodotLens bridges this gap by exposing Godot's built-in Language Server through the Model Context Protocol (MCP), giving AI agents compiler-accurate code intelligence for GDScript — go to definition, find references, diagnostics, rename, and more.

**Example:** Finding all usages of `_on_player_hit`:

| Approach | Result |
|----------|--------|
| `grep "_on_player_hit"` | 12 matches including comments, strings, and similarly named functions |
| `gdscript_references` | Exactly 4 call sites where `_on_player_hit` is invoked |

## Prerequisites

- **Godot 4.x** editor must be **running** with your project open — Godot's LSP server starts automatically when the editor opens a project
- **Python 3.10+** (for pip install) or **Node.js 16+** (for npx)

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
| `GODOT_LSP_PORT` | `6005` | Godot LSP server port |

## Tools

### Health

| Tool | Description |
|------|-------------|
| `gdscript_status` | Check if Godot LSP is connected |

### Navigation (6 tools)

| Tool | Description |
|------|-------------|
| `gdscript_definition` | Go to definition of a symbol |
| `gdscript_declaration` | Go to declaration of a symbol |
| `gdscript_references` | Find all references to a symbol |
| `gdscript_hover` | Get hover information (type, docs) for a symbol |
| `gdscript_symbols` | List all symbols in a file |
| `gdscript_signature_help` | Get function signature at call site |

### Refactoring

| Tool | Description |
|------|-------------|
| `gdscript_rename` | Rename a symbol across all files |

### Synchronization (3 tools)

| Tool | Description |
|------|-------------|
| `gdscript_sync_file` | Notify LSP that a file changed, returns diagnostics |
| `gdscript_sync_files` | Batch sync multiple files |
| `gdscript_delete_file` | Notify LSP that a file was deleted |

### Batch Operations (3 tools)

| Tool | Description |
|------|-------------|
| `gdscript_symbols_batch` | Get symbols from multiple files |
| `gdscript_definitions_batch` | Get definitions for multiple positions |
| `gdscript_references_batch` | Find references for multiple positions |

### Diagnostics

| Tool | Description |
|------|-------------|
| `gdscript_diagnostics` | Get errors/warnings for files |

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
