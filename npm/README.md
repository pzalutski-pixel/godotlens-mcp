# godotlens-mcp

[![npm](https://img.shields.io/npm/v/godotlens-mcp.svg)](https://www.npmjs.com/package/godotlens-mcp)
[![PyPI](https://img.shields.io/pypi/v/godotlens-mcp.svg)](https://pypi.org/project/godotlens-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/pzalutski-pixel/godotlens-mcp/blob/main/LICENSE)

An MCP server providing **15 AI-optimized tools** for GDScript semantic code analysis, powered by Godot's built-in Language Server.

## Requirements

- **Godot 4.x** editor running with your project open (the LSP server starts automatically)
- **Python 3.10+** installed and on PATH

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

## What This Package Does

This npm package bundles the full GodotLens server (~20 KB of Python). No pip install, no network dependency after install. It:

1. Checks that Python 3.10+ is installed
2. Launches the bundled MCP server with stdio for protocol communication

Zero external Python dependencies — the server uses only the Python standard library.

## Why GodotLens?

AI coding agents work with text files but lack semantic understanding of GDScript. GodotLens bridges this gap by exposing Godot's built-in LSP through the Model Context Protocol.

| Without GodotLens | With GodotLens |
|---|---|
| `grep "func _ready"` finds text matches including comments | `gdscript_definition` returns the exact definition |
| No way to find all callers of a function | `gdscript_references` returns every call site |
| Manual file reading to understand class structure | `gdscript_symbols` returns the full symbol tree |
| No error checking until Godot runs | `gdscript_diagnostics` returns compiler errors in real-time |

## Features

- **Navigation**: go to definition, declaration, references, hover info, document symbols, signature help
- **Refactoring**: rename symbol across all files
- **Synchronization**: notify LSP of file changes, deletions
- **Batch operations**: symbols, definitions, and references across multiple files
- **Diagnostics**: errors and warnings from Godot's compiler

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `GODOT_LSP_HOST` | `127.0.0.1` | Godot LSP server host |
| `GODOT_LSP_PORT` | `6005` | Godot LSP server port |

## Documentation

Full documentation and tool reference: [GitHub](https://github.com/pzalutski-pixel/godotlens-mcp)
