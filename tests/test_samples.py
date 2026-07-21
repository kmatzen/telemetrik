import io

from telemetrik.parser import get_boxes, get_samples

from .gpmf_builder import mp4_box, u32


def _build_stbl(sizes, offsets, deltas=None, comp_offsets=None, uniform_size=None):
    if uniform_size is not None:
        stsz_payload = u32(0) + u32(uniform_size) + u32(len(offsets))
    else:
        stsz_payload = (
            u32(0) + u32(0) + u32(len(sizes)) + b"".join(u32(s) for s in sizes)
        )
    stsz = mp4_box("stsz", stsz_payload)

    stco_payload = u32(0) + u32(len(offsets)) + b"".join(u32(o) for o in offsets)
    stco = mp4_box("stco", stco_payload)

    boxes = [stsz, stco]

    if deltas is not None:
        entries = b"".join(u32(1) + u32(d) for d in deltas)
        stts_payload = u32(0) + u32(len(deltas)) + entries
        boxes.append(mp4_box("stts", stts_payload))

    if comp_offsets is not None:
        entries = b"".join(u32(1) + u32(o) for o in comp_offsets)
        ctts_payload = u32(0) + u32(len(comp_offsets)) + entries
        boxes.append(mp4_box("ctts", ctts_payload))

    return mp4_box("stbl", b"".join(boxes))


def test_get_samples_variable_sizes_with_stts_and_ctts():
    sizes = [10, 20, 30]
    offsets = [100, 110, 130]
    deltas = [1000, 2000, 3000]
    comp_offsets = [0, 500, 0]

    stbl_bytes = _build_stbl(sizes, offsets, deltas=deltas, comp_offsets=comp_offsets)
    f = io.BytesIO(stbl_bytes)
    stbl = get_boxes(f, 0, len(stbl_bytes), ["stbl"])[0]

    samples = get_samples(f, stbl)

    assert [s.offset for s in samples] == offsets
    assert [s.size for s in samples] == sizes
    assert [s.dts for s in samples] == [0, 1000, 3000]
    assert [s.pts for s in samples] == [0, 1500, 3000]


def test_get_samples_uniform_size_without_stts_or_ctts():
    offsets = [0, 5, 10]
    stbl_bytes = _build_stbl([], offsets, uniform_size=5)
    f = io.BytesIO(stbl_bytes)
    stbl = get_boxes(f, 0, len(stbl_bytes), ["stbl"])[0]

    samples = get_samples(f, stbl)

    assert [s.offset for s in samples] == offsets
    assert [s.size for s in samples] == [5, 5, 5]
    # No stts present -> dts stays 0 for every sample.
    assert [s.dts for s in samples] == [0, 0, 0]
    assert [s.pts for s in samples] == [0, 0, 0]
