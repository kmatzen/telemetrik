import io
import struct

from telemetrik.parser import GPMFBox, get_gpmf_boxes

from .gpmf_builder import gpmf_container, gpmf_leaf


def test_get_gpmf_boxes_leaf_match():
    leaf = gpmf_leaf("TEST", "L", 4, 2, struct.pack(">II", 1, 2))
    f = io.BytesIO(leaf)

    boxes = get_gpmf_boxes(f, 0, len(leaf), ["TEST"])

    assert len(boxes) == 1
    box = boxes[0]
    assert isinstance(box, GPMFBox)
    assert box.key == "TEST"
    assert box.type == "L"
    assert box.struct_size == 4
    assert box.repeat == 2


def test_get_gpmf_boxes_nested_container():
    inner = gpmf_leaf("STMP", "J", 8, 1, (12345).to_bytes(8, "big"))
    strm = gpmf_container("STRM", [inner])
    devc = gpmf_container("DEVC", [strm])
    f = io.BytesIO(devc)

    boxes = get_gpmf_boxes(f, 0, len(devc), ["DEVC", "STRM", "STMP"])

    assert len(boxes) == 1
    box = boxes[0]
    f.seek(box.offset + 8)
    assert int.from_bytes(f.read(8), "big") == 12345


def test_get_gpmf_boxes_no_match():
    leaf = gpmf_leaf("TEST", "L", 4, 1, struct.pack(">I", 7))
    f = io.BytesIO(leaf)

    assert get_gpmf_boxes(f, 0, len(leaf), ["NOPE"]) == []


def test_get_gpmf_boxes_sibling_skip():
    a = gpmf_leaf("AAAA", "L", 4, 1, struct.pack(">I", 1))
    b = gpmf_leaf("BBBB", "L", 4, 1, struct.pack(">I", 2))
    payload = a + b
    f = io.BytesIO(payload)

    boxes = get_gpmf_boxes(f, 0, len(payload), ["BBBB"])

    assert len(boxes) == 1
    f.seek(boxes[0].offset + 8)
    assert int.from_bytes(f.read(4), "big") == 2


def test_get_gpmf_boxes_padding_is_skipped_correctly():
    # struct_size * repeat = 3 bytes -> padded to 4-byte alignment total size.
    leaf = gpmf_leaf("PADX", "c", 3, 1, b"abc")
    trailing = gpmf_leaf("NEXT", "L", 4, 1, struct.pack(">I", 99))
    payload = leaf + trailing
    f = io.BytesIO(payload)

    boxes = get_gpmf_boxes(f, 0, len(payload), ["NEXT"])

    assert len(boxes) == 1
    f.seek(boxes[0].offset + 8)
    assert int.from_bytes(f.read(4), "big") == 99
