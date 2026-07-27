# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-07-27

### Fixed

- **The 1.1.0 npm package shipped with no `LICENSE` and no `NOTICE`.** `npm pack` runs
  inside `npm/`, so it cannot reach the repository root; the `files` entry listed both
  and matched nothing, and npm omitted them without complaint. That left the published
  package out of compliance with Apache 2.0 sections 4(a) and 4(d). Both workflows now
  stage the files before packing.

  The check that was supposed to catch this asserted on `package.json`'s `files` array
  rather than on the built tarball — the same manifest-instead-of-artifact mistake that
  let a broken npm package ship for six releases. Both workflows now unpack the tarball
  and assert the required paths are present before publishing, and the unit guard checks
  that they do.

  Unaffected: the PyPI distribution, which carries both files correctly.

## [1.1.0] - 2026-07-26

Correctness release. Everything below was verified against a real Godot 4.7.1
editor, not inferred. Several of these were returning confident wrong answers.

### Fixed

- **`npx godotlens-mcp` never worked, in any published version.** The launcher
  spawned `server/__main__.py`, but the published package places it at
  `server/godotlens_mcp/__main__.py`, and `PYTHONPATH` was off by the same
  directory. Six releases shipped broken because CI never executed the built
  artifact.
- **Diagnostics silently reported clean on broken code.** Godot 4.5+ percent-encodes
  the URIs it publishes (`file:///C%3A/...`), while the server built
  `file:///C:/...` and keyed its cache on the raw URI string, so the lookup never
  matched. On Windows it missed every time. `gdscript_sync_file` on a file with a
  syntax error returned `"diagnostics": []` with no error flag.
- **Re-syncing a file did nothing on Godot 4.6+, and every later query answered
  from the original text.** 4.6 rejects a second `didOpen` for a file it already
  owns and returns before reparsing. Re-syncs now use `didChange`.
- **Every path returned on Linux and macOS was relative.** `uri_to_path` sliced a
  fixed eight characters, removing the POSIX leading `/` along with the URI
  delimiter. Invisible because every path test used `C:/` literals and CI ran only
  on Linux.
- **Relative paths produced garbage URIs** (`file:///scripts/player.gd`), despite
  every tool schema advertising them as supported.
- **A blank line, malformed JSON, or a JSON array each killed the server process.**
  They now return `-32700`/`-32600` and the session continues.
- **Large responses could be truncated.** `read(n)` returns *up to* n bytes, so a
  body split across TCP segments arrived incomplete — worst on exactly the large
  `documentSymbol` and project-wide `references` payloads that matter most.
- **A server-initiated request was mistaken for our response**, returning `None`
  and desynchronising every subsequent call.
- **A stalled LSP hung the whole session**, since there was no read timeout and the
  main loop is serial.
- **Any exception tore down a healthy LSP connection**, so an unsupported method or
  a missing file forced a full reconnect and re-initialize.
- **`gdscript_delete_file` reported success for a total no-op.** It sent
  `workspace/didDeleteFiles`, a namespace Godot removed entirely in 4.6, as a
  *notification* — so the `METHOD_NOT_FOUND` was discarded. Replaced by
  `gdscript_release_file`, which reports what it actually did.
- **`gdscript_rename` could not distinguish "not renameable" from "no usages".**
  Godot returns a well-formed empty edit in both cases. It now calls
  `prepareRename` first.
- **Fixed sleeps before reading diagnostics** were a race on a cold project.
  Replaced with polling; results carry `verified`, separating "Godot checked it and
  it is clean" from "Godot never reported back".
- Blocking file reads inside async handlers stalled the event loop.

### Added

Three new capability groups, each a different way of asking Godot rather than guessing.

**Runtime** — the debugger, via Godot's Debug Adapter Protocol on port 6006, served by the
editor with no addon required. The language server says whether code compiles; only the
debugger says what the code *did*.

- `debug_run` — run the project and return what it printed. One implementation note worth
  recording: Godot withholds the `launch` response until `configurationDone` arrives, so
  sending `configurationDone` first — the natural reading of the DAP setup sequence —
  deadlocks and is indistinguishable from launch being unsupported. With the correct order it
  works, **including headless**, so it is usable in CI as well as beside an open editor.
- `debug_output`, `debug_set_breakpoints`, `debug_stack_trace`, `debug_inspect`,
  `debug_evaluate`, `debug_continue`, `debug_pause`, `debug_step_over`, `debug_terminate`,
  `debug_status`.

**Project and scenes** — obtained by running the engine, never by parsing the file format, so
inherited scenes and setting overrides resolve exactly as Godot resolves them.

- `scene_state` and `scene_validate`. Godot's reference search reads `.gd` files only, so
  renaming a signal handler silently leaves `[connection method="..."]` pointing at a method
  that no longer exists, failing at runtime with no compile error anywhere.
  `gdscript_rename` now warns when a name also appears in scene files.
- `project_config` — autoloads, input actions, `class_name` globals and the main scene.
  These names are bare strings at the point of use and nothing validates them: a typo in
  `Input.is_action_pressed("jmup")` is a silent runtime no-op that neither the compiler nor
  the language server nor scene validation catches.

**Language server capabilities that were already available and unused.**

- `gdscript_find` — locate a declaration **by name**, returning a position the other tools
  accept directly. Every navigation tool takes `(file, line, character)`, so an agent that
  knows a name has to read the file and count columns; landing one column off returns an empty
  result indistinguishable from "no such symbol", which sends it back to `grep`. Positions come
  from `documentSymbol`'s `selectionRange`, so they are the language server's answer rather
  than a text match.
- `gdscript_engine_api` — authoritative signatures and documentation for any engine class or
  member, from the exact editor build in use. Backed by the 1,076-class list Godot pushes on
  connect, which was previously received and discarded.
- `gdscript_complete` — the only scene-aware query available: Godot resolves the scene owning
  the script and completes against the real node, so `$NodePath` entries and the node's actual
  signals are included.
- `gdscript_validate` — check proposed content and get diagnostics **without writing to disk**.
- `gdscript_references_in_file` — `documentHighlight` on Godot 4.7+, avoiding the
  whole-project reparse that `references` triggers on 4.6+.

**Correctness and protocol infrastructure.**

- Capability detection: what the server *does*, not its version number. Godot returns no
  `serverInfo`, and Godot ≤4.4 advertises `workspaceSymbolProvider: true` for a method that has
  never existed, so advertised positives are never trusted alone.
- MCP tool annotations on every tool. The schema defaults `destructiveHint` and
  `openWorldHint` to *true*, so silence meant clients had to assume `gdscript_hover` might
  destroy something.
- `instructions` in the initialize result, and stderr logging.
- CI on every push and PR across {Linux, Windows, macOS} × Python 3.10–3.13, integration tests
  against a real headless Godot on all three platforms, and a job that installs the built npm
  tarball and executes it.

### Changed

- Protocol version is negotiated rather than hardcoded; current is `2025-11-25`.
- Empty results are structured JSON instead of the string `"No results"`, which an
  agent could not distinguish from a failed call.
- Minimum supported Godot is **4.6**, where the LSP's behaviour stabilises.
- Versions are single-sourced from `__init__.py`.

### Changed — licensing

- **Relicensed from MIT to Apache License 2.0.** Apache 2.0 adds an explicit patent grant and
  requires downstream users to state their changes; both matter more as the tool surface grows.
  The project has a single author across its entire history, so no third-party consent was
  needed. A `NOTICE` file now ships with both distributions, per section 4(d).

### Removed

- `gdscript_declaration`. It called the identical `find_symbols()` helper as
  `gdscript_definition` and returned the same result; its only divergence is that on
  engine built-ins it can call `DisplayServer::window_move_to_foreground()`, pulling
  the user's Godot window in front of whatever they were doing. Its useful payload is
  what `gdscript_engine_api` now returns directly.

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
