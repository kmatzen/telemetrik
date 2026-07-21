import io
import struct

import pytest

from telemetrik.parser import _parse_gpmf_value


def test_parse_none_type_returns_none():
    f = io.BytesIO(b"")
    assert _parse_gpmf_value(f, None, 0, 1) is None


def test_parse_signed_16_single():
    f = io.BytesIO(struct.pack(">h", -1234))
    assert _parse_gpmf_value(f, "s", 2, 1) == -1234


def test_parse_signed_16_tuple_with_scale():
    f = io.BytesIO(struct.pack(">hhh", 100, -200, 300))
    result = _parse_gpmf_value(f, "s", 6, 10)
    assert result == (10.0, -20.0, 30.0)


def test_parse_signed_32_single_and_tuple():
    f = io.BytesIO(struct.pack(">i", -70000))
    assert _parse_gpmf_value(f, "l", 4, 1) == -70000

    f2 = io.BytesIO(struct.pack(">ii", 4, -8))
    assert _parse_gpmf_value(f2, "l", 8, 2) == (2.0, -4.0)


def test_parse_unsigned_16_single_and_tuple():
    f = io.BytesIO(struct.pack(">H", 500))
    assert _parse_gpmf_value(f, "S", 2, 1) == 500.0

    f2 = io.BytesIO(struct.pack(">HH", 20, 40))
    assert _parse_gpmf_value(f2, "S", 4, 2) == (10.0, 20.0)


def test_parse_unsigned_32_single_and_tuple():
    f = io.BytesIO(struct.pack(">I", 9000))
    assert _parse_gpmf_value(f, "L", 4, 1) == 9000.0

    f2 = io.BytesIO(struct.pack(">II", 100, 200))
    assert _parse_gpmf_value(f2, "L", 8, 2) == (50.0, 100.0)


def test_parse_float32_single_and_tuple():
    f = io.BytesIO(struct.pack(">f", 1.5))
    assert _parse_gpmf_value(f, "f", 4, 1) == pytest.approx(1.5)

    f2 = io.BytesIO(struct.pack(">ff", 1.0, 2.0))
    result = _parse_gpmf_value(f2, "f", 8, 2)
    assert result == pytest.approx((1.0, 2.0))


def test_parse_signed_byte():
    f = io.BytesIO((-5 & 0xFF).to_bytes(1, "big"))
    assert _parse_gpmf_value(f, "b", 1, 1) == -5


def test_parse_unsigned_byte_with_scale():
    f = io.BytesIO((200).to_bytes(1, "big"))
    assert _parse_gpmf_value(f, "B", 1, 10) == 20.0


def test_parse_string_strips_nulls():
    f = io.BytesIO(b"abc\x00\x00")
    assert _parse_gpmf_value(f, "c", 5, 1) == "abc"


def test_parse_quaternion_always_reads_four_floats():
    f = io.BytesIO(struct.pack(">ffff", 1.0, 0.0, 0.0, 0.0))
    result = _parse_gpmf_value(f, "q", 16, 1)
    assert result == pytest.approx((1.0, 0.0, 0.0, 0.0))


def test_parse_default_returns_raw_bytes():
    f = io.BytesIO(b"\x01\x02\x03\x04")
    assert _parse_gpmf_value(f, "?", 4, 1) == b"\x01\x02\x03\x04"
