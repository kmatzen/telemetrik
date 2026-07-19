"""Regression tests for GPMF parsing bugs found in a repo audit.

See AUDIT.md / GitHub issues #1 and #2 for the original bug reports.
"""

import io

from telemetrik.parser import (
    Sample,
    _get_gps_data_with_sticky_metadata,
    extract_all_telemetry,
    get_stream_data,
)

from .helpers import (
    accl_payload,
    build_gopro_mp4,
    devc,
    gps5_payload,
    gpsf,
    gpsp,
    scal,
    siun,
    stmp,
    stnm,
    strm,
)


def test_gps5_multi_value_scal_scales_each_channel_independently():
    """Regression test for issue #1: SCAL had 5 divisors (lat, lon, alt, speed_2d,
    speed_3d) but only the first was read and applied to every channel."""
    body = (
        stmp(0)
        + scal([10_000_000, 10_000_000, 1_000, 1_000, 100])
        + gps5_payload([(377749000, -1224194000, 15300, 500, 510)])
    )
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = _get_gps_data_with_sticky_metadata(f, [Sample(0, len(payload))])

    assert stream.sample_count == 1
    _, (lat, lon, alt, speed_2d, speed_3d, fix, dop) = stream.data[0]
    assert lat == 37.7749
    assert lon == -122.4194
    assert alt == 15.3
    assert speed_2d == 0.5
    assert speed_3d == 5.1


def test_scal_single_value_still_broadcasts_to_all_channels():
    """A single SCAL divisor (the common case for ACCL/GYRO) must still apply to
    every channel, not just the first."""
    body = stmp(0) + scal([1000]) + accl_payload([(1000, 2000, 3000)])
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = get_stream_data(f, [Sample(0, len(payload))], "ACCL")

    assert stream.data[0][1] == (1.0, 2.0, 3.0)


def test_single_payload_stream_does_not_crash():
    """Regression test for issue #2: a stream with exactly one payload crashed
    with TypeError because reading_duration_ms was never assigned."""
    body = stmp(1000) + accl_payload([(100, 200, 300), (400, 500, 600)])
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = get_stream_data(f, [Sample(0, len(payload))], "ACCL")

    assert stream.sample_count == 2
    assert stream.data == [(1, (100.0, 200.0, 300.0)), (1, (400.0, 500.0, 600.0))]


def test_single_payload_gps_stream_does_not_crash():
    body = stmp(1000) + scal([10_000_000, 10_000_000, 1000, 1000, 100]) + gps5_payload(
        [(377749000, -1224194000, 15300, 500, 510)]
    )
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = _get_gps_data_with_sticky_metadata(f, [Sample(0, len(payload))])

    assert stream.sample_count == 1


def test_multi_payload_timestamp_interpolation():
    body1 = stmp(0) + accl_payload([(100, 100, 100), (200, 200, 200)])
    body2 = stmp(1_000_000) + accl_payload([(300, 300, 300), (400, 400, 400)])
    payload1 = devc(strm(body1))
    payload2 = devc(strm(body2))
    f = io.BytesIO(payload1 + payload2)
    samples = [Sample(0, len(payload1)), Sample(len(payload1), len(payload2))]

    stream = get_stream_data(f, samples, "ACCL")

    timestamps = [ts for ts, _ in stream.data]
    assert timestamps == [0, 500, 1000, 1500]


def test_gps5_sticky_gpsf_gpsp_persist_across_samples():
    """GPSF/GPSP are "sticky" metadata: once seen, they apply to subsequent
    samples that don't repeat them."""
    body1 = (
        stmp(0)
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gpsf(3)
        + gpsp(250)
        + gps5_payload([(377749000, -1224194000, 15300, 500, 510)])
    )
    body2 = (
        stmp(1_000_000)
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gps5_payload([(377750000, -1224195000, 15400, 510, 520)])
    )
    payload1 = devc(strm(body1))
    payload2 = devc(strm(body2))
    f = io.BytesIO(payload1 + payload2)
    samples = [Sample(0, len(payload1)), Sample(len(payload1), len(payload2))]

    stream = _get_gps_data_with_sticky_metadata(f, samples)

    assert stream.sample_count == 2
    fix1, dop1 = stream.data[0][1][5], stream.data[0][1][6]
    fix2, dop2 = stream.data[1][1][5], stream.data[1][1][6]
    assert (fix1, dop1) == (3, 250)
    assert (fix2, dop2) == (3, 250)  # carried over, not reset


def test_extract_all_telemetry_end_to_end():
    """Exercise the full pipeline (MP4 box walking, sample table, PTS/time_base,
    GPS scaling) through the public API against a synthetic file."""
    body = (
        stmp(0)
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gps5_payload([(377749000, -1224194000, 15300, 500, 510)])
    )
    mp4_bytes = build_gopro_mp4([devc(strm(body))])

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "test.mp4"
        video_path.write_bytes(mp4_bytes)
        telemetry = extract_all_telemetry(str(video_path), streams=["GPS5"])

    assert "GPS5" in telemetry
    _, (lat, lon, alt, speed_2d, speed_3d, fix, dop) = telemetry["GPS5"].data[0]
    assert lat == 37.7749
    assert alt == 15.3


def test_multiple_streams_in_one_devc_are_extracted_independently():
    """Two distinct STRM boxes (ACCL, GPS5) inside the same DEVC must not
    cross-contaminate each other's timestamps, SCAL divisors, or values."""
    accl_strm = strm(stmp(0) + accl_payload([(100, 200, 300)]))
    gps_strm = strm(
        stmp(0)
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gps5_payload([(377749000, -1224194000, 15300, 500, 510)])
    )
    payload = devc(accl_strm + gps_strm)
    mp4_bytes = build_gopro_mp4([payload])

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "test.mp4"
        video_path.write_bytes(mp4_bytes)
        telemetry = extract_all_telemetry(str(video_path), streams=["ACCL", "GPS5"])

    assert telemetry["ACCL"].data[0][1] == (100.0, 200.0, 300.0)
    _, (lat, lon, alt, *_rest) = telemetry["GPS5"].data[0]
    assert lat == 37.7749
    assert alt == 15.3


def test_gps5_units_are_retried_independently_of_name():
    """name and units are captured from whichever payload has them first,
    independently of each other (not "give up on units once name is found")."""
    body1 = (
        stmp(0)
        + stnm("GPS (Latitude, Longitude, Altitude)")
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gps5_payload([(377749000, -1224194000, 15300, 500, 510)])
    )
    body2 = (
        stmp(1_000_000)
        + siun("m,m,m,m/s,m/s")
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gps5_payload([(377750000, -1224195000, 15400, 510, 520)])
    )
    payload1 = devc(strm(body1))
    payload2 = devc(strm(body2))
    f = io.BytesIO(payload1 + payload2)
    samples = [Sample(0, len(payload1)), Sample(len(payload1), len(payload2))]

    stream = _get_gps_data_with_sticky_metadata(f, samples)

    assert stream.name == "GPS (Latitude, Longitude, Altitude)"
    assert stream.units == "m,m,m,m/s,m/s,fix,dop"
