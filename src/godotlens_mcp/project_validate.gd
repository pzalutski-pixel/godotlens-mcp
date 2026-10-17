extends MainLoop

## Reports references that no longer resolve, as JSON on stdout.
##
## Run via: godot --headless --path <project> --script project_validate.gd
##
## After a file is moved or renamed, scenes and resources can point at something that
## is no longer there, and nothing reports it until that reference is loaded at runtime. The engine's own dependency lists are used, and its
## own resolution rule: an ext_resource with a known UID loads from that UID's path
## even if its text path is stale, and only falls back to the text path otherwise.
##
## A MainLoop, not a SceneTree, so no autoload or other project code starts; listing
## dependencies parses files without instantiating anything.
##
## Scripts are not walked: GDScript's parser reports no dependencies (its
## get_dependencies() is an empty TODO in 4.7). A broken preload() or extends path is
## a compile error instead, which the language server's diagnostics report.

const EXTENSIONS := ["tscn", "tres", "res", "scn"]


func _initialize() -> void:
	var broken: Array = []
	var stale: Array = []
	var files := _walk("res://")
	for file in files:
		for entry in ResourceLoader.get_dependencies(file):
			var problem := _check(file, entry)
			if problem.is_empty():
				continue
			if problem["kind"] == "stale_uid":
				stale.append(problem)
			else:
				broken.append(problem)

	var autoloads: Array = []
	for setting in ProjectSettings.get_property_list():
		var key: String = setting.get("name", "")
		if key.begins_with("autoload/"):
			var path := str(ProjectSettings.get_setting(key)).trim_prefix("*")
			if not _exists(path):
				autoloads.append({"name": key.trim_prefix("autoload/"), "path": path})

	var main_scene := str(ProjectSettings.get_setting("application/run/main_scene", ""))
	var result := {
		"checked_files": files.size(),
		"broken": broken,
		"stale_uids": stale,
		"missing_autoloads": autoloads,
		"main_scene": {"path": main_scene, "exists": main_scene.is_empty() or _exists(main_scene)},
		"scripts_note": "preload() and extends paths in scripts are compile errors; gdscript_diagnostics reports them.",
	}
	print(JSON.stringify({"project_validate": result}))


func _process(_delta: float) -> bool:
	return true


## One dependency entry: "uid://...::Type::fallback/path" or "res://path::Type".
func _check(file: String, entry: String) -> Dictionary:
	var parts := entry.split("::")
	var first := parts[0]
	var type := parts[1] if parts.size() > 1 else ""
	var fallback := parts[2] if parts.size() > 2 else ""
	if first.begins_with("uid://"):
		var id := ResourceUID.text_to_id(first)
		var by_uid := ResourceUID.has_id(id) and ResourceLoader.exists(ResourceUID.get_id_path(id))
		if by_uid:
			return {}
		if not fallback.is_empty() and _exists(fallback):
			return {"kind": "stale_uid", "file": file, "uid": first, "path": fallback, "type": type}
		return {"kind": "missing", "file": file, "missing": fallback if not fallback.is_empty() else first,
				"uid": first, "type": type}
	if _exists(first):
		return {}
	return {"kind": "missing", "file": file, "missing": first, "type": type}


func _exists(path: String) -> bool:
	if path.begins_with("uid://"):
		var id := ResourceUID.text_to_id(path)
		return ResourceUID.has_id(id) and ResourceLoader.exists(ResourceUID.get_id_path(id))
	return ResourceLoader.exists(path) or FileAccess.file_exists(path)


func _walk(dir: String) -> PackedStringArray:
	var found: PackedStringArray = []
	var access := DirAccess.open(dir)
	if access == null:
		return found
	access.include_hidden = false
	for sub in access.get_directories():
		if not sub.begins_with("."):
			found.append_array(_walk(dir.path_join(sub)))
	for name in access.get_files():
		if name.get_extension() in EXTENSIONS:
			found.append(dir.path_join(name))
	return found
