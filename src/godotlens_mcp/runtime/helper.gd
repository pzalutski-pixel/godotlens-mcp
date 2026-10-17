extends SceneTree

## The GodotLens runtime helper: runs the game, and answers lens:* debugger messages.
##
## Loaded per run as the main loop - godot --script helper.gd -- godotlens_scene=<res://...>
## - and never written into the project. Godot still loads the project's settings,
## input map and autoloads for a --script main loop; the helper then opens the scene
## itself, which is what the engine would have done.
##
## It answers only while a debugger is attached, on the connection the debugger
## already uses (EngineDebugger message captures); it opens no port of its own.
##
## A capture callback must return a bool synchronously (EngineDebugger::call_capture
## rejects anything else), so every handler here returns at once; work that waits on
## frames runs in a separate coroutine started without await.

const VERSION := 1

var _globals_names := PackedStringArray()
var _globals_values: Array = []
var _globals_ready := false


func _initialize() -> void:
	var scene := ""
	var offscreen := false
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("godotlens_scene="):
			scene = arg.trim_prefix("godotlens_scene=")
		elif arg == "godotlens_offscreen":
			offscreen = true
	if scene.is_empty():
		scene = str(ProjectSettings.get_setting("application/run/main_scene", ""))
	if EngineDebugger.is_active():
		EngineDebugger.register_message_capture("lens", _on_lens)
		# Announced rather than asked for: the game connects before this main loop
		# starts, and a lens message that arrives before the capture exists is dropped.
		EngineDebugger.send_message("lens:ready", [VERSION])
	if not scene.is_empty():
		change_scene_to_file(scene)
	if offscreen:
		# Godot keeps a third of a window on screen when it opens, but not when it
		# moves; out of sight it still renders, so screenshots keep working.
		root.position = Vector2i(-100000, -100000)


func _on_lens(message: String, data: Array) -> bool:
	match message:
		"hello":
			EngineDebugger.send_message("lens:hello", [data[0], VERSION])
		"eval":
			_evaluate(int(data[0]), str(data[1]), str(data[2]) if data.size() > 2 else "")
		"action":
			@warning_ignore("missing_await")
			_press(int(data[0]), str(data[1]), int(data[2]), float(data[3]))
		_:
			return false
	return true


func _evaluate(id: int, text: String, base_path: String) -> void:
	_collect_globals()
	var base: Node = current_scene
	if not base_path.is_empty():
		base = root.get_node_or_null(NodePath(base_path))
		if base == null:
			_reply(id, false, "No node at %s." % base_path)
			return
	var expression := Expression.new()
	if expression.parse(text, _globals_names) != OK:
		_reply(id, false, expression.get_error_text())
		return
	var value = expression.execute(_globals_values, base, false)
	if expression.has_execute_failed():
		_reply(id, false, expression.get_error_text())
		return
	_reply(id, true, value)


## Names an expression can use besides the base node's members: autoloads, engine
## singletons such as Input, and class_name globals. Expression knows none of them.
func _collect_globals() -> void:
	if _globals_ready:
		return
	_globals_ready = true
	var seen := {}
	for setting in ProjectSettings.get_property_list():
		var key: String = setting.get("name", "")
		if key.begins_with("autoload/"):
			var autoload_name := key.trim_prefix("autoload/")
			var node := root.get_node_or_null(NodePath(autoload_name))
			if node != null and not seen.has(autoload_name):
				seen[autoload_name] = true
				_globals_names.append(autoload_name)
				_globals_values.append(node)
	for singleton in Engine.get_singleton_list():
		if not seen.has(singleton):
			seen[singleton] = true
			_globals_names.append(singleton)
			_globals_values.append(Engine.get_singleton(singleton))
	for entry in ProjectSettings.get_global_class_list():
		var class_name_text := String(entry.get("class", ""))
		if not class_name_text.is_empty() and not seen.has(class_name_text):
			seen[class_name_text] = true
			_globals_names.append(class_name_text)
			_globals_values.append(load(String(entry.get("path", ""))))


func _press(id: int, action: String, frames: int, strength: float) -> void:
	if not InputMap.has_action(action):
		_reply(id, false, "No input action named %s." % action)
		return
	# Input.action_press from inside a capture does not register as just pressed; an
	# event fed through the normal input path does.
	var down := InputEventAction.new()
	down.action = action
	down.pressed = true
	down.strength = strength
	Input.parse_input_event(down)
	for i in maxi(frames, 1):
		await process_frame
	var up := InputEventAction.new()
	up.action = action
	up.pressed = false
	Input.parse_input_event(up)
	_reply(id, true, action)


func _reply(id: int, ok: bool, value) -> void:
	EngineDebugger.send_message("lens:result", [id, ok, value])
