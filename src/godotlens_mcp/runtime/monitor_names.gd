extends MainLoop

## Prints the Performance.Monitor enum of THIS engine build as JSON on stdout.
##
## Run via: godot --headless --path <project> --script monitor_names.gd
##
## performance:profile_frame carries bare values in enum order, so the names must come
## from the same build that sends them. A MainLoop, not a SceneTree, so no autoload or
## other project code starts.


func _initialize() -> void:
	var monitors := {}
	for constant in ClassDB.class_get_enum_constants("Performance", "Monitor"):
		if constant != "MONITOR_MAX":
			monitors[str(ClassDB.class_get_integer_constant("Performance", constant))] = constant
	print(JSON.stringify({"monitors": monitors}))


func _process(_delta: float) -> bool:
	return true
