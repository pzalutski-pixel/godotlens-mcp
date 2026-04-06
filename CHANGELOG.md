# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-04-05

### Added
- Initial public release
- 15 MCP tools for GDScript semantic code analysis via Godot's built-in LSP
  - Health: `gdscript_status`
  - Navigation: `gdscript_definition`, `gdscript_declaration`, `gdscript_references`,
    `gdscript_hover`, `gdscript_symbols`, `gdscript_signature_help`
  - Refactoring: `gdscript_rename`
  - Synchronization: `gdscript_sync_file`, `gdscript_sync_files`, `gdscript_delete_file`
  - Batch operations: `gdscript_symbols_batch`, `gdscript_definitions_batch`, `gdscript_references_batch`
  - Diagnostics: `gdscript_diagnostics`
- PyPI distribution (`pip install godotlens-mcp`)
- npm wrapper (`npx godotlens-mcp`)
- Clean LSP disconnect on server shutdown via lifespan management
- GitHub Actions release workflow for automated PyPI and npm publishing
