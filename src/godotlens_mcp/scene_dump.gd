@tool
extends SceneTree

## Dumps Godot's OWN view of a scene as JSON on stdout.
##
## Run via: godot --headless --path <project> --script scene_dump.gd -- <res://path.tscn> ...
##
## This exists so GodotLens never has to parse .tscn itself. Reading the file as text
## would be a second, independent interpretation of the format that could silently
## disagree with the engine.
##
## PackedScene.get_state() describes only what THIS file stores. For a node that comes
## from an inherited base scene or an instanced child scene, the file holds just its
## overrides: the type is empty and the script usually lives in the other scene. So
## besides the raw per-file nodes, the dump reports "resolved_nodes": the base and
## instanced scenes' own states merged under their paths, with this file's overrides
## applied on top - which is the tree Godot builds when it instantiates the scene.
##
## The work happens in _initialize(), not _init(): a --script main loop is created
## before autoload names are registered with the GDScript compiler, so a script that
## uses an autoload fails to compile if it is loaded any earlier. Autoloads are
## therefore started, as they would be in the game.

# Guards against a scene that instances itself through a chain of other scenes.
const MAX_INSTANCE_DEPTH := 16


func _initialize() -> void:
	var targets: PackedStringArray = []
	var args := OS.get_cmdline_user_args()
	for arg in args:
		targets.append(arg)

	var out := {"scenes": {}, "errors": {}}

	for path in targets:
		if not ResourceLoader.exists(path):
			out["errors"][path] = "not found"
			continue
		var packed := ResourceLoader.load(path) as PackedScene
		if packed == null:
			out["errors"][path] = "not a PackedScene, or failed to load"
			continue
		out["scenes"][path] = _dump_scene(packed)

	print(JSON.stringify(out))
	quit(0)


func _dump_scene(packed: PackedScene) -> Dictionary:
	var state := packed.get_state()
	var nodes: Array = []

	for i in state.get_node_count():
		var node_info := {
			"index": i,
			"name": String(state.get_node_name(i)),
			"type": String(state.get_node_type(i)),
			"path": String(state.get_node_path(i)),
			"owner": String(state.get_node_owner_path(i)),
			"instance": null,
			"script": null,
			"properties": {},
		}

		# An instanced child scene: its contents live in the base scene, not here.
		var instance := state.get_node_instance(i)
		if instance != null:
			node_info["instance"] = instance.resource_path

		var stored: Array = []  # [name, value] in file order, for the property checks
		var script: Script = null
		for p in state.get_node_property_count(i):
			var prop_name := String(state.get_node_property_name(i, p))
			var value = state.get_node_property_value(i, p)
			if prop_name == "script":
				if value != null and value is Resource:
					node_info["script"] = (value as Resource).resource_path
					script = value as Script
			elif prop_name == "unique_name_in_owner":
				node_info["unique_name_in_owner"] = bool(value)
			else:
				node_info["properties"][prop_name] = _describe(value)
				stored.append([prop_name, value])

		var node_type := String(state.get_node_type(i))
		if not node_type.is_empty():
			node_info["class_exists"] = ClassDB.class_exists(node_type) or _global_classes().has(node_type)
			if node_info["class_exists"]:
				node_info["property_issues"] = _property_issues(node_type, script, stored)

		nodes.append(node_info)

	var connections: Array = []
	for i in state.get_connection_count():
		connections.append({
			"signal": String(state.get_connection_signal(i)),
			"from": String(state.get_connection_source(i)),
			"to": String(state.get_connection_target(i)),
			# An unvalidated STRING. Nothing checks that this method exists, and if it
			# does not the failure is at runtime with no compile error anywhere.
			"method": String(state.get_connection_method(i)),
			"flags": state.get_connection_flags(i),
			"binds": _describe(state.get_connection_binds(i)),
			"unbinds": state.get_connection_unbinds(i),
		})

	var resolved := {}
	_merge_state(state, ".", "this_file", resolved, 0)
	var resolved_nodes: Array = resolved.values()
	for node in resolved_nodes:
		_add_script_compatibility(node)
	for connection in connections:
		connection["facts"] = _connection_facts(connection, resolved)

	return {
		"nodes": nodes,
		"resolved_nodes": resolved_nodes,
		"connections": connections,
		"scripts": _script_facts(resolved_nodes),
		"node_count": state.get_node_count(),
		"connection_count": state.get_connection_count(),
	}


## Merge a scene state into ``out`` (keyed by path from the top scene's root).
##
## An instanced scene - including the base of an inherited scene, which Godot reports
## as the root node's instance - is merged first, so the overrides the outer file
## stores are applied on top of what the instanced scene defines.
func _merge_state(state: SceneState, prefix: String, origin: String, out: Dictionary, depth: int) -> void:
	for i in state.get_node_count():
		var path := _join(prefix, String(state.get_node_path(i)))

		var instance := state.get_node_instance(i)
		if instance != null and depth < MAX_INSTANCE_DEPTH:
			_merge_state(instance.get_state(), path, "instance:" + instance.resource_path, out, depth + 1)

		var entry: Dictionary = out.get(path, {})
		if entry.is_empty():
			entry = {"path": path, "type": "", "script": null, "origin": origin}
			out[path] = entry

		var node_type := String(state.get_node_type(i))
		if not node_type.is_empty():
			entry["type"] = node_type
		elif entry["type"] == "" and state.is_node_instance_placeholder(i):
			entry["type"] = "InstancePlaceholder"

		for p in state.get_node_property_count(i):
			var prop_name := String(state.get_node_property_name(i, p))
			var value = state.get_node_property_value(i, p)
			if prop_name == "script":
				entry["script"] = (value as Resource).resource_path if value is Resource else null
			elif prop_name == "unique_name_in_owner":
				entry["unique_name_in_owner"] = bool(value)


## Per-script facts the checks need, asked of the engine rather than inferred.
func _script_facts(resolved_nodes: Array) -> Dictionary:
	var facts := {}
	for node in resolved_nodes:
		var path = node.get("script")
		if path == null or facts.has(path):
			continue
		var script := ResourceLoader.load(path) as Script
		if script == null:
			facts[path] = {"compiled": false, "base_type": ""}
			continue
		facts[path] = {
			"compiled": script.can_instantiate(),
			"base_type": String(script.get_instance_base_type()),
		}
	return facts


# ---------------------------------------------------------------------------
# Facts for scene_validate, all from engine APIs
# ---------------------------------------------------------------------------

var _global_class_names := {}


func _global_classes() -> Dictionary:
	if _global_class_names.is_empty():
		for entry in ProjectSettings.get_global_class_list():
			_global_class_names[String(entry.get("class", ""))] = true
	return _global_class_names


## Whether the attached script can drive a node of this type. A mismatch makes Godot
## refuse to attach it, and the node runs with no script at all.
func _add_script_compatibility(node: Dictionary) -> void:
	var path = node.get("script")
	var node_type: String = node.get("type", "")
	if path == null or node_type.is_empty() or not ClassDB.class_exists(node_type):
		return
	var script := ResourceLoader.load(path) as Script
	if script == null or not script.can_instantiate():
		return
	var base := String(script.get_instance_base_type())
	node["script_base"] = base
	node["script_compatible"] = base.is_empty() or node_type == base or ClassDB.is_parent_class(node_type, base)


## What the engine knows about a connection's signal and handler.
func _connection_facts(connection: Dictionary, resolved: Dictionary) -> Dictionary:
	var facts := {}
	var source = resolved.get(_join(".", connection["from"]))
	var target = resolved.get(_join(".", connection["to"]))
	var signal_name: String = connection["signal"]
	if source != null and not String(source["type"]).is_empty():
		var exists := false
		var arg_count := -1
		var script := _loaded_script(source.get("script"))
		if script != null:
			for sig in script.get_script_signal_list():
				if sig["name"] == signal_name:
					exists = true
					arg_count = sig["args"].size()
		if not exists and ClassDB.class_has_signal(source["type"], signal_name):
			exists = true
			arg_count = ClassDB.class_get_signal(source["type"], signal_name)["args"].size()
		facts["signal_exists"] = exists
		facts["signal_args"] = arg_count
	if target != null and not String(target["type"]).is_empty():
		var method: String = connection["method"]
		facts["method_native"] = ClassDB.class_exists(target["type"]) and ClassDB.class_has_method(target["type"], method)
		var script := _loaded_script(target.get("script"))
		if script != null:
			for m in script.get_script_method_list():
				if m["name"] == method:
					var total: int = m["args"].size()
					facts["method_args"] = {
						"total": total,
						"required": total - m["default_args"].size(),
						"vararg": (int(m["flags"]) & METHOD_FLAG_VARARG) != 0,
					}
	return facts


func _loaded_script(path) -> Script:
	if path == null:
		return null
	var script := ResourceLoader.load(path) as Script
	return script if script != null and script.can_instantiate() else null


## Properties this file stores that the node's class or script does not have, or whose
## value is of the wrong type. Godot drops both silently at load.
##
## The stored properties are applied, in file order, to a bare instance of the engine
## class (no script, so no project code runs) and the instance is then asked what it
## has. Asking a fresh instance or ClassDB alone misses properties that exist only
## once another is set - surface_material_override/0 appears once a mesh is set.
func _property_issues(node_type: String, script: Script, stored: Array) -> Array:
	if not ClassDB.can_instantiate(node_type):
		return []
	var script_properties := {}
	if script != null and script.can_instantiate():
		for prop in script.get_script_property_list():
			script_properties[prop["name"]] = true
	var probe = ClassDB.instantiate(node_type)
	var issues: Array = []
	for pair in stored:
		var name: String = pair[0]
		if name.begins_with("metadata/") or script_properties.has(name):
			continue
		probe.set(name, pair[1])
		var info := _property_info(probe, name)
		if info.is_empty():
			issues.append({"property": name, "kind": "unknown_property"})
		elif not _type_fits(int(info["type"]), pair[1]):
			issues.append({"property": name, "kind": "wrong_value_type",
					"expected": type_string(int(info["type"])), "got": type_string(typeof(pair[1]))})
	if not probe is RefCounted:
		probe.free()
	return issues


func _property_info(object: Object, name: String) -> Dictionary:
	for prop in object.get_property_list():
		if prop["name"] == name:
			return prop
	return {}


func _type_fits(expected: int, value) -> bool:
	var got := typeof(value)
	if expected == TYPE_NIL or got == expected or got == TYPE_NIL:
		return true
	var numbers := [TYPE_INT, TYPE_FLOAT]
	var strings := [TYPE_STRING, TYPE_STRING_NAME, TYPE_NODE_PATH]
	return (expected in numbers and got in numbers) or (expected in strings and got in strings)


func _join(prefix: String, path: String) -> String:
	var relative := path.trim_prefix("./")
	if relative == "." or relative.is_empty():
		return prefix
	if prefix == ".":
		return relative
	return prefix + "/" + relative


func _describe(value) -> Variant:
	## Reduce a Variant to something JSON can carry without losing the type.
	if value == null:
		return null
	if value is Resource:
		var res := value as Resource
		return {"__resource": res.resource_path, "__type": res.get_class()}
	if value is Array:
		var items: Array = []
		for item in value:
			items.append(_describe(item))
		return items
	if value is Dictionary:
		var mapped := {}
		for key in value:
			mapped[String(key)] = _describe(value[key])
		return mapped
	if value is bool or value is int or value is float or value is String:
		return value
	return {"__value": str(value), "__type": type_string(typeof(value))}
