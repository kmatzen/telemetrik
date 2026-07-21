import pytest

from telemetrik.parser import extract_all_telemetry

from .gpmf_builder import build_mp4, devc_block, s16_values, strm_block


def _sample_payloads():
    accl1 = strm_block(
        stmp_us=1_000_000,
        stream_key="ACCL",
        type_char="s",
        struct_size=6,
        repeat=1,
        values_payload=s16_values([100, 200, 300]),
        scal=100,
        name="Accelerometer",
        units="m/s2",
    )
    accl2 = strm_block(
        stmp_us=2_000_000,
        stream_key="ACCL",
        type_char="s",
        struct_size=6,
        repeat=1,
        values_payload=s16_values([110, 210, 310]),
        scal=100,
        name="Accelerometer",
        units="m/s2",
    )
    gps1 = strm_block(
        stmp_us=1_000_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([100, 200, 300, 10, 20]),
        scal=10,
        gpsf=3,
        gpsp=50,
    )
    gps2 = strm_block(
        stmp_us=2_000_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([110, 210, 310, 15, 25]),
        scal=10,
        gpsf=3,
        gpsp=50,
    )
    devc1 = devc_block([accl1, gps1])
    devc2 = devc_block([accl2, gps2])
    return [devc1, devc2]


def _write_mp4(tmp_path, **kwargs):
    payloads = kwargs.pop("payloads", None) or _sample_payloads()
    mp4_bytes = build_mp4(
        payloads,
        timescale=1000,
        sample_deltas=[1000, 1000],
        composition_offsets=[0, 0],
        **kwargs,
    )
    path = tmp_path / "video.mp4"
    path.write_bytes(mp4_bytes)
    return path


def test_extract_default_streams_only_returns_present_streams(tmp_path):
    path = _write_mp4(tmp_path)

    telemetry = extract_all_telemetry(str(path))

    assert set(telemetry.keys()) == {"ACCL", "GPS5"}
    assert telemetry["ACCL"].sample_count == 2
    assert telemetry["ACCL"].data[0] == (1000, (1.0, 2.0, 3.0))
    assert telemetry["GPS5"].sample_count == 2
    assert telemetry["GPS5"].data[0] == (1000, (10.0, 20.0, 30.0, 1.0, 2.0, 3, 50))


def test_extract_uses_pts_data_from_mdhd_timescale(tmp_path):
    path = _write_mp4(tmp_path)

    telemetry = extract_all_telemetry(str(path))

    assert telemetry["ACCL"].pts_data is not None
    assert telemetry["ACCL"].pts_data[0][0] == 0.0
    assert telemetry["ACCL"].pts_data[1][0] == 1.0


def test_extract_custom_stream_subset(tmp_path):
    path = _write_mp4(tmp_path)

    telemetry = extract_all_telemetry(str(path), streams=["GPS5"])

    assert set(telemetry.keys()) == {"GPS5"}


def test_extract_raises_without_gpmf_track(tmp_path):
    path = _write_mp4(tmp_path, include_gpmf_marker=False)

    with pytest.raises(ValueError, match="does not contain GPMF"):
        extract_all_telemetry(str(path))


def test_extract_empty_dict_when_requested_stream_absent(tmp_path):
    path = _write_mp4(tmp_path)

    telemetry = extract_all_telemetry(str(path), streams=["TMPC"])

    assert telemetry == {}
