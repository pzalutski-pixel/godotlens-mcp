"""project_validate against real Godot: references that no longer resolve.

Runs the binary only; no editor. Each test builds its own project, because the
breakages are planted in it.
"""

import subprocess

import pytest

pytestmark = pytest.mark.integration


def _import(godot_binary, project):
    """The UID cache, which decides how references resolve, is written by an import."""
    subprocess.run([godot_binary, "--headless", "--path", str(project), "--import"],
                   capture_output=True, timeout=300)


@pytest.fixture
def validate(mcp_process, godot_binary):
    def _call(project):
        proc = mcp_process(cwd=project, env={"GODOT_LSP_PORT": "16", "GODOT_BIN": godot_binary})
        proc.initialize()
        payload, is_error = proc.call_tool("project_validate", {})
        assert not is_error, payload
        return payload
    return _call


def test_a_clean_project_has_nothing_broken(godot_project, godot_binary, validate):
    _import(godot_binary, godot_project)
    report = validate(godot_project)
    assert report["valid"] is True, report
    assert report["checked_files"] > 0


def test_moved_files_missing_autoloads_and_a_missing_main_scene_are_found(
        godot_project, godot_binary, validate):
    config = godot_project / "project.godot"
    config.write_text(config.read_text(encoding="utf-8")
                      .replace('GameState="*res://game_state.gd"',
                               'GameState="*res://game_state.gd"\nMissing="*res://missing_state.gd"')
                      .replace('run/main_scene="res://main.tscn"', 'run/main_scene="res://gone.tscn"'),
                      encoding="utf-8")
    (godot_project / "moved_ref.tscn").write_text(
        '[gd_scene format=3]\n\n[ext_resource type="Script" path="res://moved/player.gd" id="1"]\n\n'
        '[node name="M" type="Node"]\nscript = ExtResource("1")\n', encoding="utf-8")
    _import(godot_binary, godot_project)

    report = validate(godot_project)
    assert report["valid"] is False
    assert [(b["file"], b["missing"]) for b in report["broken"]] == [
        ("res://moved_ref.tscn", "res://moved/player.gd")]
    assert report["missing_autoloads"] == [{"name": "Missing", "path": "res://missing_state.gd"}]
    assert report["main_scene"] == {"path": "res://gone.tscn", "exists": False}


def test_a_reference_whose_uid_still_resolves_is_not_broken(godot_project, godot_binary, validate):
    """Godot loads by UID first, so a stale text path alone is not a breakage."""
    _import(godot_binary, godot_project)
    uid = (godot_project / "player.gd.uid").read_text(encoding="utf-8").strip()
    (godot_project / "uid_ref.tscn").write_text(
        f'[gd_scene format=3]\n\n[ext_resource type="Script" uid="{uid}" '
        f'path="res://old/place/player.gd" id="1"]\n\n'
        '[node name="M" type="CharacterBody2D"]\nscript = ExtResource("1")\n', encoding="utf-8")

    report = validate(godot_project)
    assert report["broken"] == [], report["broken"]
