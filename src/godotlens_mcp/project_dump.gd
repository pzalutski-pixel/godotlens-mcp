@tool
extends SceneTree

## Dumps Godot's resolved project configuration as JSON on stdout.
##
## Run via: godot --headless --path <project> --script project_dump.gd
##
## Autoload names and input action names are bare strings at the point of use —
## `GameState.add_score(1)` and `Input.is_action_pressed("jump")` — and nothing in the
## compiler, the language server, or a scene check validates them. A typo is a silent
## runtime no-op. This reports what the project actually declares so those uses can be
## checked.
##
## ProjectSettings is asked rather than project.godot being read, so defaults,
## overrides and feature-tagged values resolve the way the engine resolves them.


func _init() -> void:
	var out := {
		"autoloads": _autoloads(),
		"input_actions": _input_actions(),
		"global_classes": _global_classes(),
		"application": {
			"name": ProjectSettings.get_setting("application/config/name", ""),
			"main_scene": ProjectSettings.get_setting("application/run/main_scene", ""),
			"features": _features(),
		},
	}
	print(JSON.stringify(out))
	quit(0)


func _autoloads() -> Array:
	var found: Array = []
	for setting in ProjectSettings.get_property_list():
		var key: String = setting.get("name", "")
		if not key.begins_with("autoload/"):
			continue
		var value = ProjectSettings.get_setting(key)
		var path := str(value)
		# A leading "*" marks the autoload as instantiated as a Node rather than
		# loaded as a plain script.
		var is_node := path.begins_with("*")
		if is_node:
			path = path.substr(1)
		found.append({
			"name": key.trim_prefix("autoload/"),
			"path": path,
			"is_node": is_node,
		})
	return found


func _input_actions() -> Array:
	var found: Array = []
	for setting in ProjectSettings.get_property_list():
		var key: String = setting.get("name", "")
		if not key.begins_with("input/"):
			continue
		var action := key.trim_prefix("input/")
		var value = ProjectSettings.get_setting(key)
		var events: Array = []
		if value is Dictionary and value.has("events"):
			for event in value["events"]:
				if event != null:
					events.append(event.as_text() if event.has_method("as_text") else str(event))
		found.append({"name": action, "events": events})

	# Godot's own built-in ui_* actions are usable without being declared, so report
	# them too; otherwise a correct use of ui_accept looks like a typo.
	for builtin in InputMap.get_actions():
		var action_name := String(builtin)
		var already := false
		for existing in found:
			if existing["name"] == action_name:
				already = true
				break
		if not already:
			found.append({"name": action_name, "events": [], "builtin": true})
	return found


func _global_classes() -> Array:
	var found: Array = []
	for entry in ProjectSettings.get_global_class_list():
		found.append({
			"class": String(entry.get("class", "")),
			"base": String(entry.get("base", "")),
			"path": String(entry.get("path", "")),
		})
	return found


func _features() -> Array:
	var value = ProjectSettings.get_setting("application/config/features", [])
	var out: Array = []
	for item in value:
		out.append(String(item))
	return out
