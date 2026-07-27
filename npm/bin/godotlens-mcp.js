#!/usr/bin/env node
"use strict";

const { execSync, spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

// Layout produced by the release workflow:
//   <pkg>/server/godotlens_mcp/{__main__,server,lsp_client,__init__}.py
// PYTHONPATH must therefore point at <pkg>/server, the parent of the package dir.
const SERVER_DIR = path.join(__dirname, "..", "server");
const PACKAGE_DIR = path.join(SERVER_DIR, "godotlens_mcp");
const ENTRY_POINT = path.join(PACKAGE_DIR, "__main__.py");

function findPython() {
  for (const cmd of ["python3", "python"]) {
    try {
      const output = execSync(`${cmd} --version`, {
        encoding: "utf-8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
      const match = output.match(/Python (\d+)\.(\d+)/);
      if (match) {
        const major = parseInt(match[1], 10);
        const minor = parseInt(match[2], 10);
        if (major === 3 && minor >= 10) {
          return cmd;
        }
        process.stderr.write(
          `Found Python ${match[1]}.${match[2]} but Python 3.10+ is required.\n`
        );
      }
    } catch {
      // command not found, try next
    }
  }
  return null;
}

const python = findPython();
if (!python) {
  process.stderr.write(
    "Error: Python 3.10+ not found.\n" +
      "GodotLens MCP requires Python 3.10 or later.\n" +
      "Install from https://www.python.org/downloads/\n"
  );
  process.exit(1);
}

if (!fs.existsSync(ENTRY_POINT)) {
  process.stderr.write(
    `Error: godotlens-mcp server files are missing or misplaced.\n` +
      `Expected entry point at: ${ENTRY_POINT}\n` +
      `This indicates a broken package install. Please reinstall, and if the\n` +
      `problem persists report it at\n` +
      `https://github.com/pzalutski-pixel/godotlens-mcp/issues\n`
  );
  process.exit(1);
}

const args = process.argv.slice(2);
const child = spawn(python, [ENTRY_POINT, ...args], {
  stdio: "inherit",
  env: { ...process.env, PYTHONPATH: SERVER_DIR },
});

child.on("exit", (code) => process.exit(code ?? 0));
child.on("error", (err) => {
  process.stderr.write(`Error: Failed to start godotlens-mcp: ${err.message}\n`);
  process.exit(1);
});
