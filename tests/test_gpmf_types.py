"""Coverage for every GPMF type character handled by _parse_gpmf_value.

See GitHub issue #4 for the bugs this locks in: 'q' was misinterpreted as a
4-float quaternion instead of a Q15.16 fixed-point number (and always read 16
bytes regardless of struct_size, desyncing the rest of the payload); 'b'/'B'
with struct_size > 1 collapsed multiple bytes into one big integer instead of
a tuple; 'd', 'j', 'J', 'U', 'F' were unhandled.
"""

import io
import struct

import pytest

from telemetrik.parser import _parse_gpmf_value


def test_signed_16_scalar_and_tuple():
    f = io.BytesIO(struct.pack(">h", -100))
    assert _parse_gpmf_value(f, "s", 2, 1.0) == -100.0

    f = io.BytesIO(struct.pack(">3h", 1, 2, 3))
    assert _parse_gpmf_value(f, "s", 6, 1.0) == (1.0, 2.0, 3.0)


def test_unsigned_32_scalar_and_tuple():
    f = io.BytesIO(struct.pack(">L", 42))
    assert _parse_gpmf_value(f, "L", 4, 1.0) == 42.0

    f = io.BytesIO(struct.pack(">2L", 10, 20))
    assert _parse_gpmf_value(f, "L", 8, (2.0, 5.0)) == (5.0, 4.0)


def test_float32():
    f = io.BytesIO(struct.pack(">f", 1.5))
    assert _parse_gpmf_value(f, "f", 4, 1.0) == pytest.approx(1.5)


def test_float64():
    f = io.BytesIO(struct.pack(">d", 3.14159))
    assert _parse_gpmf_value(f, "d", 8, 1.0) == pytest.approx(3.14159)


def test_signed_64():
    f = io.BytesIO(struct.pack(">q", -1234567890123))
    assert _parse_gpmf_value(f, "j", 8, 1.0) == -1234567890123.0


def test_unsigned_64():
    f = io.BytesIO(struct.pack(">Q", 1234567890123))
    assert _parse_gpmf_value(f, "J", 8, 1.0) == 1234567890123.0


def test_signed_byte_scalar_and_tuple():
    f = io.BytesIO(bytes([0xFF]))  # -1 as signed byte
    assert _parse_gpmf_value(f, "b", 1, 1.0) == -1.0

    f = io.BytesIO(bytes([1, 2, 3]))
    assert _parse_gpmf_value(f, "b", 3, 1.0) == (1.0, 2.0, 3.0)


def test_unsigned_byte_scalar_and_tuple():
    f = io.BytesIO(bytes([200]))
    assert _parse_gpmf_value(f, "B", 1, 1.0) == 200.0

    f = io.BytesIO(bytes([1, 2, 3, 4]))
    assert _parse_gpmf_value(f, "B", 4, 1.0) == (1.0, 2.0, 3.0, 4.0)


def test_string():
    f = io.BytesIO(b"ACCL\x00\x00")
    assert _parse_gpmf_value(f, "c", 6, 1.0) == "ACCL"


def test_fourcc():
    f = io.BytesIO(b"GPS5")
    assert _parse_gpmf_value(f, "F", 4, 1.0) == "GPS5"


def test_utc_date_string():
    f = io.BytesIO(b"250714120000.000")
    assert _parse_gpmf_value(f, "U", 16, 1.0) == "250714120000.000"


def test_q_fixed_point_scalar_and_tuple():
    # Q15.16: raw int32 / 65536.0
    f = io.BytesIO(struct.pack(">l", int(2.5 * 65536)))
    assert _parse_gpmf_value(f, "q", 4, 1.0) == 2.5

    f = io.BytesIO(struct.pack(">2l", int(1.0 * 65536), int(-0.5 * 65536)))
    assert _parse_gpmf_value(f, "q", 8, 1.0) == (1.0, -0.5)


def test_default_raw_bytes_for_unknown_type():
    f = io.BytesIO(b"\x01\x02\x03")
    assert _parse_gpmf_value(f, "?", 3, 1.0) == b"\x01\x02\x03"


def test_none_type_returns_none():
    f = io.BytesIO(b"")
    assert _parse_gpmf_value(f, None, 0, 1.0) is None
