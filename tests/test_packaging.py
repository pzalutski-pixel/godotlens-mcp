"""Packaging and documentation consistency.

Two of these encode failures this project actually shipped: the npm package was
broken for six releases because nothing executed the built artifact, and npm/README.md
sat untouched from 1.0.0 while the tool surface doubled — still advertising tools that
had been removed.
"""

import json
import re
from pathlib import Path

import pytest

from godotlens_mcp import __version__
from godotlens_mcp.server import TOOLS

ROOT = Path(__file__).resolve().parent.parent
READMES = [ROOT / "README.md", ROOT / "npm" / "README.md"]

# Every environment variable the code actually reads.
ENV_VARS = {
    "GODOT_LSP_HOST", "GODOT_LSP_PORT", "GODOT_DAP_HOST", "GODOT_DAP_PORT",
    "GODOT_BIN", "GODOT_PROJECT_ROOT", "GODOT_LSP_TIMEOUT",
    "GODOT_DIAGNOSTICS_TIMEOUT", "GODOT_VERSION", "GODOT_FIND_FILE_LIMIT",
}

TOOL_PATTERN = re.compile(r"`((?:gdscript|scene|debug|project)_[a-z_]+)`")


def tool_names() -> set[str]:
    return {tool["name"] for tool in TOOLS}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def test_versions_agree_across_manifests():
    """pyproject (1.0.5), npm (1.0.0) and server.json (1.0.2) had all drifted apart."""
    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert npm["version"] == __version__
    assert server["version"] == __version__
    for package in server["packages"]:
        assert package["version"] == __version__


def test_pyproject_takes_its_version_from_the_package():
    """A literal version in pyproject is how the drift started."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'path = "src/godotlens_mcp/__init__.py"' in text


# ---------------------------------------------------------------------------
# Documentation matches the code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.parent.name or "root")
def test_readme_does_not_document_removed_tools(readme):
    """npm/README.md advertised gdscript_declaration long after it was removed."""
    documented = set(TOOL_PATTERN.findall(readme.read_text(encoding="utf-8")))
    phantom = documented - tool_names()
    assert not phantom, f"{readme.name} documents tools that do not exist: {sorted(phantom)}"


def test_main_readme_documents_every_tool():
    documented = set(TOOL_PATTERN.findall((ROOT / "README.md").read_text(encoding="utf-8")))
    missing = tool_names() - documented
    assert not missing, f"README.md is missing: {sorted(missing)}"


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.parent.name or "root")
def test_readme_states_the_tool_count_correctly(readme):
    """Both READMEs hardcoded '15 tools' well past the point it was true."""
    text = readme.read_text(encoding="utf-8")
    for stated in re.findall(r"\*?\*?(\d+)\*?\*? tools", text):
        assert int(stated) == len(TOOLS), \
            f"{readme.name} claims {stated} tools; there are {len(TOOLS)}"


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.parent.name or "root")
def test_readme_documents_every_environment_variable(readme):
    text = readme.read_text(encoding="utf-8")
    missing = {var for var in ENV_VARS if var not in text}
    assert not missing, f"{readme.name} does not document: {sorted(missing)}"


def test_server_json_advertises_every_environment_variable():
    """This is what the MCP Registry shows users, so an omission is invisible to us."""
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    for package in server["packages"]:
        declared = {entry["name"] for entry in package["environmentVariables"]}
        missing = ENV_VARS - declared
        assert not missing, f"server.json {package['registryType']} omits: {sorted(missing)}"


def test_server_json_description_fits_the_registry_limit():
    """The MCP Registry rejects descriptions over 100 characters."""
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert len(server["description"]) <= 100


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.parent.name or "root")
def test_readme_states_the_godot_version_floor(readme):
    """'Godot 4.x' is wrong now: below 4.6 is refused."""
    text = readme.read_text(encoding="utf-8")
    assert "4.6" in text, f"{readme.name} does not state the supported Godot floor"


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.parent.name or "root")
def test_readme_explains_that_scene_tools_need_a_binary(readme):
    """A requirement that is only discovered at runtime is a support burden."""
    text = readme.read_text(encoding="utf-8")
    assert "GODOT_BIN" in text
    assert "scene_" in text


def test_license_is_consistent_everywhere():
    """A relicense is easy to do halfway: LICENSE says one thing, a manifest another."""
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0" in license_text
    assert "[yyyy]" not in license_text, "copyright placeholder was left unfilled"

    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    assert npm["license"] == "Apache-2.0"

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "Apache-2.0"' in pyproject
    assert "MIT" not in pyproject

    for readme in READMES:
        text = readme.read_text(encoding="utf-8")
        assert "Apache" in text, f"{readme.name} does not state the license"
        assert "License: MIT" not in text


def test_notice_file_ships_with_both_distributions():
    """Apache 2.0 section 4(d): NOTICE is how attribution travels downstream."""
    assert (ROOT / "NOTICE").is_file()

    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    assert "NOTICE" in npm["files"] and "LICENSE" in npm["files"]

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "NOTICE" in pyproject


def test_changelog_covers_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{__version__}]" in text, f"CHANGELOG has no entry for {__version__}"


# ---------------------------------------------------------------------------
# The shipped artifact
# ---------------------------------------------------------------------------


def test_npm_launcher_points_at_the_bundled_layout():
    """The exact bug that broke six releases.

    The release workflow copies the package to server/godotlens_mcp/, so the launcher
    must target server/godotlens_mcp/__main__.py and put server/ on PYTHONPATH. It
    previously targeted server/__main__.py, which does not exist.
    """
    launcher = (ROOT / "npm" / "bin" / "godotlens-mcp.js").read_text(encoding="utf-8")
    assert 'path.join(SERVER_DIR, "godotlens_mcp")' in launcher
    assert "PYTHONPATH: SERVER_DIR" in launcher
    assert "existsSync(ENTRY_POINT)" in launcher, "must fail loudly if files are missing"


def test_release_workflow_strips_pycache_from_the_bundle():
    """A local build would otherwise ship .pyc files alongside the source."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "__pycache__" in workflow


def test_release_verifies_the_tag_matches_the_package_version():
    """PyPI takes its version from __init__.py while npm takes it from the tag.

    A mismatched tag would publish two different versions under one release.
    """
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "verify-version" in workflow
    assert "__version__" in workflow


def test_release_runs_the_built_package_before_publishing():
    """ci.yml does not run on tag pushes, so the packaging check must exist here too.

    Without it, the bug that broke six releases could ship again unnoticed.
    """
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "npm pack" in workflow
    assert "godotlens-mcp" in workflow
    assert "serverInfo" in workflow, "must assert the package actually answers"


def test_ci_runs_on_every_platform_and_tests_the_package():
    """Tests previously ran only on tag push, only on Linux, and never on the artifact."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for platform in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert platform in ci
    assert "npm pack" in ci, "nothing verifies the built npm artifact"
    assert "ruff check" in ci


def test_helper_scripts_are_packaged():
    """The .gd helpers are the only non-Python files shipped, so the likeliest to be lost."""
    for helper in ("scene_dump.gd", "project_dump.gd"):
        path = ROOT / "src" / "godotlens_mcp" / helper
        assert path.is_file(), f"{helper} is missing"


def test_every_tool_has_a_description_and_schema():
    for tool in TOOLS:
        assert tool.get("description"), f"{tool['name']} has no description"
        assert len(tool["description"]) > 40, f"{tool['name']} description is too thin"
        assert "inputSchema" in tool
        assert "annotations" in tool


def test_position_tools_state_zero_based_in_their_description():
    """The single most common way a caller gets a wrong answer."""
    for tool in TOOLS:
        properties = tool["inputSchema"].get("properties", {})
        if "line" in properties and "character" in properties:
            assert "ZERO-BASED" in tool["description"] or "0-based" in tool["description"], \
                f"{tool['name']} takes coordinates but does not say they are zero-based"
