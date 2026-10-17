"""Scenes with exactly one mistake each, as hand-edited .tscn files go wrong.

Godot accepts every one of these with no error at all, or only a console warning,
and silently drops or misplaces what the mistake touched. scene_validate must report
each with its own problem kind, and nothing for the correct scene at the end.
Written into the test project under res://mistakes/.
"""

TOGGLE_HANDLER_GD = '''extends Node


func _on_toggled() -> void:
\tpass
'''

POINTER_GD = '''extends Node

@export var target: NodePath
'''

_HANDLER = '[ext_resource type="Script" path="res://handler.gd" id="1_handler"]\n\n'


def _scene(body: str, resources: str = "") -> str:
    return "[gd_scene format=3]\n\n" + resources + body


# file name -> (expected problem kind, scene text)
MISTAKES = {
    "unknown_property.tscn": ("unknown_property", _scene(
        '[node name="Root" type="Control"]\n\n'
        '[node name="Text" type="Label" parent="."]\ntxet = "typo"\n')),
    "unknown_script_property.tscn": ("unknown_property", _scene(
        '[node name="Root" type="Node2D"]\nscript = ExtResource("1_handler")\nsped = 5\n', _HANDLER)),
    "wrong_value_type.tscn": ("wrong_value_type", _scene(
        '[node name="Root" type="Node"]\n\n'
        '[node name="Thing" type="Node2D" parent="."]\nposition = "not a vector"\n')),
    "unknown_node_type.tscn": ("unknown_node_type", _scene(
        '[node name="Root" type="Control"]\n\n[node name="Text" type="Lable" parent="."]\n')),
    "script_type_mismatch.tscn": ("script_type_mismatch", _scene(
        '[node name="Root" type="Node"]\n\n'
        '[node name="Body" type="Node2D" parent="."]\nscript = ExtResource("1_player")\n',
        '[ext_resource type="Script" path="res://player.gd" id="1_player"]\n\n')),
    "missing_parent.tscn": ("missing_parent", _scene(
        '[node name="Root" type="Node"]\n\n[node name="Lost" type="Node" parent="DoesNotExist"]\n')),
    "missing_override_target.tscn": ("missing_override_target", _scene(
        '[node name="Root" type="Node"]\n\n'
        '[node name="Child" parent="." instance=ExtResource("1_base")]\n\n'
        '[node name="Ghost" parent="Child"]\nvisible = false\n',
        '[ext_resource type="PackedScene" path="res://base.tscn" id="1_base"]\n\n')),
    "duplicate_name.tscn": ("duplicate_name", _scene(
        '[node name="Root" type="Node"]\n\n'
        '[node name="A" type="Node" parent="."]\n\n[node name="A" type="Node" parent="."]\n')),
    "duplicate_unique_name.tscn": ("duplicate_unique_name", _scene(
        '[node name="Root" type="Node"]\n\n'
        '[node name="B" type="Node" parent="."]\nunique_name_in_owner = true\n\n'
        '[node name="Holder" type="Node" parent="."]\n\n'
        '[node name="B" type="Node" parent="Holder"]\nunique_name_in_owner = true\n')),
    "missing_source_node.tscn": ("missing_source_node", _scene(
        '[node name="Root" type="Node2D"]\nscript = ExtResource("1_handler")\n\n'
        '[connection signal="pressed" from="Ghost" to="." method="_on_button_pressed"]\n', _HANDLER)),
    "missing_signal.tscn": ("missing_signal", _scene(
        '[node name="Root" type="Node2D"]\nscript = ExtResource("1_handler")\n\n'
        '[node name="Button" type="Button" parent="."]\n\n'
        '[connection signal="pressd" from="Button" to="." method="_on_button_pressed"]\n', _HANDLER)),
    "handler_arg_mismatch.tscn": ("handler_arg_mismatch", _scene(
        '[node name="Root" type="Node"]\nscript = ExtResource("1_toggle")\n\n'
        '[node name="Check" type="CheckBox" parent="."]\n\n'
        '[connection signal="toggled" from="Check" to="." method="_on_toggled"]\n',
        '[ext_resource type="Script" path="res://mistakes/toggle_handler.gd" id="1_toggle"]\n\n')),
    "missing_nodepath_target.tscn": ("missing_nodepath_target", _scene(
        '[node name="Root" type="Node"]\nscript = ExtResource("1_pointer")\ntarget = NodePath("Nope")\n',
        '[ext_resource type="Script" path="res://mistakes/pointer.gd" id="1_pointer"]\n\n')),
}

# Correct, and deliberately built from what a naive check gets wrong: a property that
# exists only once another is set (surface_material_override/0 needs a mesh), a
# handler that is an engine method on a scriptless node, a NodePath to a real node,
# and one unique name.
CORRECT_TSCN = _scene(
    '[sub_resource type="BoxMesh" id="BoxMesh_1"]\n\n'
    '[sub_resource type="StandardMaterial3D" id="Material_1"]\n\n'
    '[node name="Root" type="Node3D"]\nscript = ExtResource("1_pointer")\ntarget = NodePath("Label")\n\n'
    '[node name="Mesh" type="MeshInstance3D" parent="."]\nmesh = SubResource("BoxMesh_1")\n'
    'surface_material_override/0 = SubResource("Material_1")\n\n'
    '[node name="Label" type="Label" parent="."]\nunique_name_in_owner = true\n'
    'offset_right = 40.0\ntext = "ok"\n\n'
    '[node name="Button" type="Button" parent="."]\n\n'
    '[connection signal="pressed" from="Button" to="Label" method="queue_free"]\n',
    '[ext_resource type="Script" path="res://mistakes/pointer.gd" id="1_pointer"]\n\n')

FILES = {
    "toggle_handler.gd": TOGGLE_HANDLER_GD,
    "pointer.gd": POINTER_GD,
    "correct.tscn": CORRECT_TSCN,
    **{name: text for name, (_, text) in MISTAKES.items()},
}
