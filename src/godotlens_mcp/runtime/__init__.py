"""Talking to a running game directly over Godot's remote debugger protocol.

GodotLens launches the game with ``--remote-debug`` pointing at a port it listens
on, so the game connects to GodotLens the way it would connect to the editor's
debugger. No editor is involved, and every game gets its own connection.
"""
