import io
import struct

from telemetrik.parser import Sample, get_stream_data

from .gpmf_builder import devc_block, gpmf_container, gpmf_leaf, s16_values, stmp_leaf, strm_block


def _build_accl_samples(scal=1000):
    strm1 = strm_block(
        stmp_us=1_000_000,
        stream_key="ACCL",
        type_char="s",
        struct_size=6,
        repeat=2,
        values_payload=s16_values([100, 200, 300, 110, 210, 310]),
        scal=scal,
        name="Accelerometer",
        units="m/s2",
    )
    strm2 = strm_block(
        stmp_us=1_002_000,
        stream_key="ACCL",
        type_char="s",
        struct_size=6,
        repeat=2,
        values_payload=s16_values([120, 220, 320, 130, 230, 330]),
        scal=scal,
        name="Accelerometer",
        units="m/s2",
    )
    devc1 = devc_block([strm1])
    devc2 = devc_block([strm2])
    buf = io.BytesIO(devc1 + devc2)
    samples = [
        Sample(offset=0, size=len(devc1), pts=5000, dts=0),
        Sample(offset=len(devc1), size=len(devc2), pts=5100, dts=100),
    ]
    return buf, samples


def test_get_stream_data_scaled_values_and_timestamps():
    buf, samples = _build_accl_samples()

    stream = get_stream_data(buf, samples, "ACCL", scale=True)

    assert stream.key == "ACCL"
    assert stream.name == "Accelerometer"
    assert stream.units == "m/s2"
    assert stream.sample_count == 4
    assert stream.data == [
        (1000, (0.1, 0.2, 0.3)),
        (1001, (0.11, 0.21, 0.31)),
        (1002, (0.12, 0.22, 0.32)),
        (1003, (0.13, 0.23, 0.33)),
    ]
    assert stream.pts_data is None


def test_get_stream_data_unscaled_ignores_scal():
    buf, samples = _build_accl_samples(scal=1000)

    stream = get_stream_data(buf, samples, "ACCL", scale=False)

    assert stream.data[0] == (1000, (100.0, 200.0, 300.0))


def test_get_stream_data_with_time_base_populates_pts_data():
    buf, samples = _build_accl_samples()

    stream = get_stream_data(buf, samples, "ACCL", scale=True, time_base=(1, 1000))

    assert stream.pts_data is not None
    pts_seconds = [round(t, 3) for t, _ in stream.pts_data]
    assert pts_seconds == [5.0, 5.05, 5.1, 5.15]


def test_get_stream_data_missing_stream_key_returns_empty():
    buf, samples = _build_accl_samples()

    stream = get_stream_data(buf, samples, "GYRO", scale=True)

    assert stream.sample_count == 0
    assert stream.data == []
    assert stream.name == ""
    assert stream.units == ""
    assert stream.pts_data is None


def test_get_stream_data_skips_strm_without_stmp():
    strm = strm_block(
        stmp_us=0,
        stream_key="ACCL",
        type_char="s",
        struct_size=6,
        repeat=1,
        values_payload=s16_values([1, 2, 3]),
        include_stmp=False,
    )
    devc = devc_block([strm])
    buf = io.BytesIO(devc)
    samples = [Sample(offset=0, size=len(devc), pts=0, dts=0)]

    stream = get_stream_data(buf, samples, "ACCL", scale=True)

    assert stream.sample_count == 0
    assert stream.data == []


def test_get_stream_data_no_scal_uses_divisor_one():
    strm1 = strm_block(
        stmp_us=1_000_000,
        stream_key="SHUT",
        type_char="L",
        struct_size=4,
        repeat=1,
        values_payload=(500).to_bytes(4, "big"),
    )
    strm2 = strm_block(
        stmp_us=1_001_000,
        stream_key="SHUT",
        type_char="L",
        struct_size=4,
        repeat=1,
        values_payload=(600).to_bytes(4, "big"),
    )
    devc1 = devc_block([strm1])
    devc2 = devc_block([strm2])
    buf = io.BytesIO(devc1 + devc2)
    samples = [
        Sample(offset=0, size=len(devc1), pts=0, dts=0),
        Sample(offset=len(devc1), size=len(devc2), pts=0, dts=0),
    ]

    stream = get_stream_data(buf, samples, "SHUT", scale=True)

    assert stream.data == [(1000, 500.0), (1001, 600.0)]


def test_get_stream_data_wide_scal_divisor():
    strm1 = strm_block(
        stmp_us=1_000_000,
        stream_key="SHUT",
        type_char="L",
        struct_size=4,
        repeat=1,
        values_payload=(500).to_bytes(4, "big"),
        scal=10,
        scal_wide=True,
    )
    strm2 = strm_block(
        stmp_us=1_001_000,
        stream_key="SHUT",
        type_char="L",
        struct_size=4,
        repeat=1,
        values_payload=(600).to_bytes(4, "big"),
        scal=10,
        scal_wide=True,
    )
    devc1 = devc_block([strm1])
    devc2 = devc_block([strm2])
    buf = io.BytesIO(devc1 + devc2)
    samples = [
        Sample(offset=0, size=len(devc1), pts=0, dts=0),
        Sample(offset=len(devc1), size=len(devc2), pts=0, dts=0),
    ]

    stream = get_stream_data(buf, samples, "SHUT", scale=True)

    assert stream.data == [(1000, 50.0), (1001, 60.0)]


def test_get_stream_data_siun_special_degree_and_micro_symbols():
    raw_units = bytes([0xB0, 0xB2, 0xB3, 0xB5]) + b"C"
    siun = gpmf_leaf("SIUN", "c", len(raw_units), 1, raw_units)
    data_leaf = gpmf_leaf("TMPC", "f", 4, 1, struct.pack(">f", 21.5))
    strm1 = gpmf_container("STRM", [stmp_leaf(1_000_000), data_leaf, siun])
    strm2 = gpmf_container(
        "STRM",
        [stmp_leaf(1_001_000), gpmf_leaf("TMPC", "f", 4, 1, struct.pack(">f", 22.0))],
    )
    devc1 = devc_block([strm1])
    devc2 = devc_block([strm2])
    buf = io.BytesIO(devc1 + devc2)
    samples = [
        Sample(offset=0, size=len(devc1), pts=0, dts=0),
        Sample(offset=len(devc1), size=len(devc2), pts=0, dts=0),
    ]

    stream = get_stream_data(buf, samples, "TMPC", scale=True)

    assert stream.units == "°²³µC"


def test_get_stream_data_siun_stops_at_null_terminator():
    raw_units = b"m\x00\x00"
    siun = gpmf_leaf("SIUN", "c", len(raw_units), 1, raw_units)
    data_leaf = gpmf_leaf("TMPC", "f", 4, 1, struct.pack(">f", 21.5))
    strm1 = gpmf_container("STRM", [stmp_leaf(1_000_000), data_leaf, siun])
    strm2 = gpmf_container(
        "STRM",
        [stmp_leaf(1_001_000), gpmf_leaf("TMPC", "f", 4, 1, struct.pack(">f", 22.0))],
    )
    devc1 = devc_block([strm1])
    devc2 = devc_block([strm2])
    buf = io.BytesIO(devc1 + devc2)
    samples = [
        Sample(offset=0, size=len(devc1), pts=0, dts=0),
        Sample(offset=len(devc1), size=len(devc2), pts=0, dts=0),
    ]

    stream = get_stream_data(buf, samples, "TMPC", scale=True)

    assert stream.units == "m"
