"""Regression tests for MP4/GPMF walker robustness issues.

See GitHub issue #3: get_boxes could infinite-loop on a size == 0 box or a
truncated header, didn't support 64-bit box sizes (largesize) or co64 chunk
offsets, and extract_all_telemetry misread the timescale for a version-1
mdhd box.
"""

import io
import tempfile
from pathlib import Path

import pytest

from telemetrik.parser import extract_all_telemetry, get_boxes, get_gpmf_boxes

from .helpers import accl_payload, build_gopro_mp4, devc, stmp, strm


def test_get_boxes_size_zero_extends_to_end_of_search_range():
    payload = b"\x00" * 20
    data = (0).to_bytes(4, "big") + b"free" + payload

    boxes = get_boxes(io.BytesIO(data), 0, len(data), ["free"])

    assert len(boxes) == 1
    assert boxes[0].offset == 0
    assert boxes[0].size == len(data)


def test_get_boxes_truncated_header_raises():
    data = b"\x00\x00\x00"

    with pytest.raises(ValueError, match="Truncated"):
        get_boxes(io.BytesIO(data), 0, len(data), ["moov"])


def test_get_boxes_size_smaller_than_header_raises():
    data = (4).to_bytes(4, "big") + b"free"  # declared size 4 < 8-byte header

    with pytest.raises(ValueError, match="Invalid MP4 box size"):
        get_boxes(io.BytesIO(data), 0, len(data), ["free"])


def test_get_boxes_supports_64bit_largesize():
    payload = b"\x00" * 20
    real_size = 16 + len(payload)  # 8-byte header + 8-byte largesize + payload
    data = (1).to_bytes(4, "big") + b"free" + real_size.to_bytes(8, "big") + payload

    boxes = get_boxes(io.BytesIO(data), 0, len(data), ["free"])

    assert len(boxes) == 1
    assert boxes[0].offset == 0
    assert boxes[0].size == real_size


def test_get_boxes_invalid_key_raises_value_error():
    payload = b"\x00" * 4
    data = (12).to_bytes(4, "big") + bytes([0xFF, 0xFE, 0xFD, 0xFC]) + payload

    with pytest.raises(ValueError, match="Invalid MP4 box key"):
        get_boxes(io.BytesIO(data), 0, len(data), ["free"])


def test_get_gpmf_boxes_truncated_header_raises():
    data = b"\x00\x00\x00"

    with pytest.raises(ValueError, match="Truncated GPMF"):
        get_gpmf_boxes(io.BytesIO(data), 0, len(data), ["DEVC"])


def test_get_gpmf_boxes_invalid_key_raises_value_error():
    data = bytes([0xFF, 0xFE, 0xFD, 0xFC]) + bytes(4)

    with pytest.raises(ValueError, match="Invalid GPMF KLV key"):
        get_gpmf_boxes(io.BytesIO(data), 0, len(data), ["DEVC"])


def _extract(mp4_bytes: bytes, streams):
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "test.mp4"
        video_path.write_bytes(mp4_bytes)
        return extract_all_telemetry(str(video_path), streams=streams)


def test_extract_all_telemetry_with_co64_chunk_offsets():
    body = stmp(0) + accl_payload([(100, 200, 300)])
    mp4_bytes = build_gopro_mp4([devc(strm(body))], use_co64=True)

    telemetry = _extract(mp4_bytes, streams=["ACCL"])

    assert telemetry["ACCL"].data[0][1] == (100.0, 200.0, 300.0)


def test_extract_all_telemetry_with_mdhd_version_1():
    body1 = stmp(0) + accl_payload([(100, 100, 100)])
    body2 = stmp(0) + accl_payload([(200, 200, 200)])
    mp4_bytes = build_gopro_mp4(
        [devc(strm(body1)), devc(strm(body2))],
        timescale=1_000_000,
        mdhd_version=1,
        sample_deltas=[500_000, 500_000],  # 0.5s per sample at a 1e6 timescale
    )

    telemetry = _extract(mp4_bytes, streams=["ACCL"])

    pts_seconds = [ts for ts, _ in telemetry["ACCL"].pts_data]
    # Correct timescale (1_000_000) gives dts=0 and dts=500_000 -> 0.0s, 0.5s.
    # A version-1 mdhd misread as version-0 would read a garbage timescale
    # from the creation/modification time fields and produce a very different
    # (or zero-division) result.
    assert pts_seconds == pytest.approx([0.0, 0.5])
