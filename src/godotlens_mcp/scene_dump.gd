@tool
extends SceneTree

## Dumps Godot's OWN resolved view of a scene as JSON on stdout.
##
## Run via: godot --headless --path <project> --script scene_dump.gd -- <res://path.tscn> ...
##
## This exists so GodotLens never has to parse .tscn itself. Reading the file as text
## would be a second, independent interpretation of the format that could silently
## disagree with the engine - and inherited scenes make that near certain, since a
## node's real type, script and property values can come from a base scene entirely
## absent from the file being read. PackedScene.get_state() is what Godot actually
## instantiates, with inheritance already resolved.


func _init() -> void:
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

		for p in state.get_node_property_count(i):
			var prop_name := String(state.get_node_property_name(i, p))
			var value = state.get_node_property_value(i, p)
			if prop_name == "script":
				if value != null and value is Resource:
					node_info["script"] = (value as Resource).resource_path
			elif prop_name == "unique_name_in_owner":
				node_info["unique_name_in_owner"] = bool(value)
			else:
				node_info["properties"][prop_name] = _describe(value)

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

	return {
		"nodes": nodes,
		"connections": connections,
		"node_count": state.get_node_count(),
		"connection_count": state.get_connection_count(),
	}


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
