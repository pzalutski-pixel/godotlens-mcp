"""Godot's binary Variant format, checked against what Godot itself produces.

tests/data/variant_golden.jsonl holds, for each value literal in
variant_golden_inputs.txt, the bytes var_to_bytes() wrote and the text var_to_str()
wrote, from Godot 4.7.1. The codec must decode those bytes, re-encode them to the
identical bytes, and print the same text. The integration test regenerates the file
with the Godot binary in use, so an engine change to the format fails loudly.
"""

import json
import struct
import subprocess
from pathlib import Path

import pytest

from godotlens_mcp.runtime.variant import (
    TYPE_IDS,
    CallableRef,
    DecodeError,
    NodePath,
    ObjectId,
    Rid,
    SignalRef,
    StringName,
    Value,
    decode,
    encode,
    frame,
    from_json,
    message,
    to_json,
    to_text,
)

DATA = Path(__file__).parent / "data"
GOLDEN = [json.loads(line) for line in
          (DATA / "variant_golden.jsonl").read_text(encoding="utf-8").splitlines() if line]


def _id(case):
    return case["literal"][:40]


@pytest.mark.parametrize("case", GOLDEN, ids=_id)
def test_decodes_what_godot_encodes(case):
    raw = bytes.fromhex(case["hex"])
    value, end = decode(raw)
    assert end == len(raw), "the whole value must be consumed, padding included"
    assert encode(value) == raw, "re-encoding must reproduce Godot's bytes exactly"


def _same_text(case):
    # Dictionaries: var_to_str sorts keys, the wire keeps insertion order (below).
    # Array[Node]: the wire names object element types EncodedObjectAsID (below).
    return case["type"] != TYPE_IDS["Dictionary"] and case["literal"] != "Array[Node]([])"


@pytest.mark.parametrize("case", [c for c in GOLDEN if _same_text(c)], ids=_id)
def test_text_matches_var_to_str(case):
    """Agents read values in the same literal syntax Godot writes."""
    value, _ = decode(bytes.fromhex(case["hex"]))
    assert to_text(value) == case["var_to_str"]


def test_dictionaries_keep_their_wire_order():
    """var_to_str sorts keys for display; the wire carries insertion order."""
    case = next(c for c in GOLDEN if c["literal"] == '{"b": 2, "a": 1}')
    value, _ = decode(bytes.fromhex(case["hex"]))
    assert list(value) == ["b", "a"]
    assert to_text(value) == '{"b": 2, "a": 1}'


def test_typed_containers_keep_their_type():
    case = next(c for c in GOLDEN if c["literal"] == "Array[int]([1, 2])")
    value, _ = decode(bytes.fromhex(case["hex"]))
    assert value.element.text() == "int"
    assert to_text(value) == "Array[int]([1, 2])"


def test_object_element_types_arrive_as_encoded_object_as_id():
    """Without full objects, Godot writes EncodedObjectAsID in place of the class
    name (marshalls.cpp, _encode_container_type), so the real class never arrives."""
    case = next(c for c in GOLDEN if c["literal"] == "Array[Node]([])")
    value, _ = decode(bytes.fromhex(case["hex"]))
    assert value.element.name == "EncodedObjectAsID"


# ---------------------------------------------------------------------------
# What only the debugger sends (no text form, so no golden literal)
# ---------------------------------------------------------------------------


def test_object_ids_round_trip():
    """The debugger sends objects by id (header flag 1 << 16), never whole objects."""
    raw = struct.pack("<IQ", TYPE_IDS["Object"] | (1 << 16), 30064771079)
    value, _ = decode(raw)
    assert value == ObjectId(30064771079)
    assert encode(value) == raw
    assert to_json(value) == {"object_id": 30064771079}
    assert to_json(ObjectId(0)) is None


@pytest.mark.parametrize("value", [Rid(42), SignalRef("pressed", 7), CallableRef()])
def test_other_reference_types_round_trip(value):
    decoded, end = decode(encode(value))
    assert decoded == value
    assert end == len(encode(value))


def test_a_full_object_is_refused():
    """Without the as-id flag an object carries its class and properties; the debugger
    link never sends that, and decoding it would mean instantiating classes."""
    with pytest.raises(DecodeError):
        decode(struct.pack("<I", TYPE_IDS["Object"]) + b"\0" * 8)


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    b"",
    b"\x04\x00\x00\x00\x10\x00\x00\x00ab",          # String claims 16 bytes, has 2
    struct.pack("<I", 200),                          # no such type
    struct.pack("<II", TYPE_IDS["Array"], 3),         # three elements, none present
    struct.pack("<Ii", TYPE_IDS["PackedInt32Array"], -1),
])
def test_malformed_values_raise_rather_than_misread(raw):
    with pytest.raises(DecodeError):
        decode(raw)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_message_is_a_length_prefixed_array_for_the_main_thread():
    raw = message("scene:request_scene_tree", [])
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    value, _ = decode(raw[4:])
    assert value == ["scene:request_scene_tree", 1, []]


def test_an_oversized_message_is_refused_before_sending():
    with pytest.raises(ValueError, match="8 MiB"):
        frame(b"x" * (9 << 20))


# ---------------------------------------------------------------------------
# What agents pass in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", [[1, 2], "Vector2(1, 2)", {"x": 1, "y": 2},
                                   {"type": "Vector2", "value": [1, 2]}])
def test_a_vector_can_be_given_in_any_natural_form(given):
    assert from_json(given, "Vector2") == Value("Vector2", (1.0, 2.0))


def test_a_colour_can_be_given_as_hex():
    assert from_json("#ff000080", "Color").components == pytest.approx((1.0, 0.0, 0.0, 128 / 255))


def test_the_wrong_literal_type_is_refused():
    with pytest.raises(ValueError, match="expected a Vector2"):
        from_json("Vector3(1, 2, 3)", "Vector2")


def test_scalars_are_coerced_to_the_property_type():
    assert from_json("42", "int") == 42
    assert from_json(3, "float") == 3.0 and isinstance(from_json(3, "float"), float)
    assert from_json("true", "bool") is True
    assert isinstance(from_json("idle", "StringName"), StringName)
    assert from_json('NodePath("Player/Gun")', "NodePath") == NodePath("Player/Gun")


def test_values_come_out_as_json_an_agent_can_read():
    value = [1, 2.5, "s", Value("Vector2", (1.0, 0.5)), {"k": NodePath("A/B")}]
    assert to_json(value) == [1, 2.5, "s", "Vector2(1, 0.5)", {"k": 'NodePath("A/B")'}]


# ---------------------------------------------------------------------------
# Against the engine in use
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_golden_file_matches_the_engine_in_use(godot_binary, godot_project):
    """Regenerate with this Godot; any difference means the wire format changed."""
    result = subprocess.run(
        [godot_binary, "--headless", "--path", str(godot_project),
         "--script", str(DATA / "variant_golden.gd"), "--",
         str(DATA / "variant_golden_inputs.txt")],
        capture_output=True, timeout=120)
    fresh = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()
             if line.startswith('{"')]
    assert fresh == GOLDEN
