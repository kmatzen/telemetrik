import io

from telemetrik.parser import Box, get_boxes

from .gpmf_builder import mp4_box


def test_get_boxes_flat_match():
    payload = mp4_box("free", b"\x00\x00\x00\x00") + mp4_box("skip", b"abcd")
    f = io.BytesIO(payload)

    boxes = get_boxes(f, 0, len(payload), ["skip"])

    assert len(boxes) == 1
    assert boxes[0].key == "skip"
    assert boxes[0].size == 12


def test_get_boxes_no_match_returns_empty():
    payload = mp4_box("free", b"1234")
    f = io.BytesIO(payload)

    boxes = get_boxes(f, 0, len(payload), ["nope"])

    assert boxes == []


def test_get_boxes_multiple_siblings():
    payload = mp4_box("trak", b"a") + mp4_box("trak", b"bb") + mp4_box("trak", b"ccc")
    f = io.BytesIO(payload)

    boxes = get_boxes(f, 0, len(payload), ["trak"])

    assert [b.size for b in boxes] == [9, 10, 11]


def test_get_boxes_nested_path():
    inner = mp4_box("mdhd", b"XYZ1")
    mdia = mp4_box("mdia", inner)
    trak = mp4_box("trak", mdia)
    moov = mp4_box("moov", trak)
    f = io.BytesIO(moov)

    boxes = get_boxes(f, 0, len(moov), ["moov", "trak", "mdia", "mdhd"])

    assert len(boxes) == 1
    box = boxes[0]
    assert isinstance(box, Box)
    f.seek(box.offset + 8)
    assert f.read(4) == b"XYZ1"


def test_get_boxes_stops_recursing_on_wrong_branch():
    # "mdia" exists, but doesn't contain "gmhd" -> nested lookup finds nothing.
    mdia = mp4_box("mdia", mp4_box("mdhd", b"junk"))
    trak = mp4_box("trak", mdia)
    f = io.BytesIO(trak)

    boxes = get_boxes(f, 0, len(trak), ["trak", "mdia", "gmhd"])

    assert boxes == []
