extends SceneTree

## Regenerates tests/data/variant_golden.json: for each Godot value literal in the
## input file, the bytes var_to_bytes() produces and the text var_to_str() produces.
##
## Run via: godot --headless --script tests/data/variant_golden.gd -- <inputs.txt>
## The output is one JSON object per line on stdout.


func _initialize() -> void:
	var inputs := FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0])
	for line in inputs.split("\n"):
		var text := line.strip_edges()
		if text.is_empty() or text.begins_with("#"):
			continue
		var value = str_to_var(text)
		print(JSON.stringify({
			"literal": text,
			"type": typeof(value),
			"hex": var_to_bytes(value).hex_encode(),
			"var_to_str": var_to_str(value),
		}))
	quit(0)
