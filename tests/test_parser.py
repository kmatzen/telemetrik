"""Regression tests for GPMF parsing bugs found in a repo audit.

See AUDIT.md / GitHub issues #1 and #2 for the original bug reports.
"""

import io
import os
import struct

import pytest

from telemetrik.parser import (
    Sample,
    extract_all_telemetry,
    get_stream_data,
)

from .helpers import (
    _raw_klv,
    accl_payload,
    build_gopro_mp4,
    devc,
    gps5_payload,
    gpsf,
    gpsp,
    klv,
    mp4_box,
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

    stream = _extract(build_gopro_mp4([devc(strm(body))]), streams=["GPS5"])["GPS5"]

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
    body = (
        stmp(1000)
        + scal([10_000_000, 10_000_000, 1000, 1000, 100])
        + gps5_payload([(377749000, -1224194000, 15300, 500, 510)])
    )

    stream = _extract(build_gopro_mp4([devc(strm(body))]), streams=["GPS5"])["GPS5"]

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

    stream = _extract(build_gopro_mp4([payload1, payload2]), streams=["GPS5"])["GPS5"]

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

    stream = _extract(build_gopro_mp4([payload1, payload2]), streams=["GPS5"])["GPS5"]

    assert stream.name == "GPS (Latitude, Longitude, Altitude)"
    assert stream.units == "m,m,m,m/s,m/s,fix,dop"


# ---------------------------------------------------------------------------
# Bug-bash regression tests (2026-10-14)
# ---------------------------------------------------------------------------


def test_stsc_multi_samples_per_chunk():
    """stco holds CHUNK offsets; stsc must be honored to expand per-sample
    offsets when a chunk holds several samples."""
    from .helpers import _mdhd, _stco, _stsc, _stsz

    p1 = devc(strm(stmp(0) + accl_payload([(1, 2, 3)])))
    p2 = devc(strm(stmp(1_000_000) + accl_payload([(4, 5, 6)])))
    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))

    def build(offset):
        stsc = _stsc([(1, 2)])  # chunk 1 holds 2 samples
        stbl = mp4_box(b"stbl", _stsz([len(p1), len(p2)]) + stsc + _stco([offset]))
        return mp4_box(
            b"moov",
            mp4_box(b"trak", mp4_box(b"mdia", _mdhd(1_000_000) + mp4_box(b"minf", gmhd + stbl))),
        )

    moov = build(0)
    moov = build(len(moov))
    fn = _tmp_path(moov + p1 + p2)
    try:
        telemetry = extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)

    values = [v for _, v in telemetry["ACCL"].data]
    assert values == [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]


def test_repeat_zero_payload_is_skipped():
    """A stream payload with repeat == 0 must be skipped, not divide by zero."""
    empty = klv(b"ACCL", ord("s"), 6, 0, b"")
    p1 = devc(strm(stmp(0) + empty))
    p2 = devc(strm(stmp(1_000) + accl_payload([(7, 8, 9)])))
    f = io.BytesIO(p1 + p2)

    stream = get_stream_data(f, [Sample(0, len(p1)), Sample(len(p1), len(p2))], "ACCL")

    assert stream.data == [(1.0, (7.0, 8.0, 9.0))]


def test_stts_expansion_is_capped_at_sample_count():
    """A corrupted 4.29e9 stts sample_count must not expand a huge list."""
    import struct as _struct

    from .helpers import _mdhd, _stco, _stsz

    p1 = devc(strm(stmp(0) + accl_payload([(1, 2, 3)])))
    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    stts = mp4_box(
        b"stts",
        bytes(4)
        + (1).to_bytes(4, "big")
        + _struct.pack(">I", 0xFFFFFFFF)  # absurd sample_count
        + (1).to_bytes(4, "big"),
    )

    def build(offset):
        stbl = mp4_box(b"stbl", _stsz([len(p1)]) + _stco([offset]) + stts)
        return mp4_box(
            b"moov",
            mp4_box(b"trak", mp4_box(b"mdia", _mdhd(1_000_000) + mp4_box(b"minf", gmhd + stbl))),
        )

    moov = build(0)
    moov = build(len(moov))
    fn = _tmp_path(moov + p1)
    try:
        telemetry = extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)

    assert telemetry["ACCL"].sample_count == 1


def test_non_ascii_stnm_does_not_kill_extraction():
    """Non-ASCII bytes in STNM must degrade, not raise UnicodeDecodeError."""
    name = "Temp\xe9".encode("latin-1")
    body = stmp(0) + klv(b"STNM", ord("c"), len(name), 1, name) + accl_payload([(1, 2, 3)])
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = get_stream_data(f, [Sample(0, len(payload))], "ACCL")

    assert stream.name  # decoded with replacement, not a crash
    assert stream.sample_count == 1


def test_siun_multi_entry_joins_with_commas():
    """Multi-entry SIUN: NUL padding ends only its own entry; entries are
    joined with commas."""
    data = b"m/s\xb2" + b"rad\x00" + b"m\x00\x00\x00"
    siun = klv(b"SIUN", ord("c"), 4, 3, data)
    body = stmp(0) + siun + accl_payload([(1, 2, 3)])
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = get_stream_data(f, [Sample(0, len(payload))], "ACCL")

    assert stream.units == "m/s\u00b2,rad,m"


def test_truncated_stsz_raises_valueerror():
    """A truncated stsz entry table must raise ValueError, not decode to 0."""
    from .helpers import _mdhd, _stco

    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    # stsz claims 5 entries but has none.
    stsz = mp4_box(b"stsz", bytes(4) + bytes(4) + (5).to_bytes(4, "big"))
    moov = mp4_box(
        b"moov",
        mp4_box(
            b"trak",
            mp4_box(
                b"mdia",
                _mdhd(1_000_000) + mp4_box(b"minf", gmhd + mp4_box(b"stbl", stsz + _stco([0] * 5))),
            ),
        ),
    )
    fn = _tmp_path(moov)
    try:
        with pytest.raises(ValueError):
            extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)


def test_zero_timescale_raises_valueerror():
    p1 = devc(strm(stmp(0) + accl_payload([(1, 2, 3)])))
    fn = _tmp_path(build_gopro_mp4([p1], timescale=0))
    try:
        with pytest.raises(ValueError):
            extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)


def test_missing_stsz_raises_valueerror():
    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    from .helpers import _mdhd

    moov = mp4_box(
        b"moov",
        mp4_box(
            b"trak",
            mp4_box(b"mdia", _mdhd(1_000_000) + mp4_box(b"minf", gmhd + mp4_box(b"stbl", b""))),
        ),
    )
    fn = _tmp_path(moov)
    try:
        with pytest.raises(ValueError):
            extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)


def test_fractional_timestamps_are_preserved():
    """Three readings over 10 ms must not truncate to [0, 3, 6]."""
    p1 = devc(strm(stmp(0) + accl_payload([(1, 2, 3)] * 3)))
    p2 = devc(strm(stmp(10_000) + accl_payload([(1, 2, 3)])))
    f = io.BytesIO(p1 + p2)

    stream = get_stream_data(f, [Sample(0, len(p1)), Sample(len(p1), len(p2))], "ACCL")

    timestamps = [ts for ts, _ in stream.data]
    assert timestamps == pytest.approx([0.0, 10 / 3, 20 / 3, 10.0])


def test_float_stream_is_scaled_by_scal():
    """SCAL divisors must apply to 'f'/'d' streams too."""
    data = struct.pack(">3f", 1000.0, 2000.0, 3000.0)
    body = stmp(0) + scal([1000]) + klv(b"ACCL", ord("f"), 12, 1, data)
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    stream = get_stream_data(f, [Sample(0, len(payload))], "ACCL")

    assert stream.data[0][1] == (1.0, 2.0, 3.0)


# ---------------------------------------------------------------------------
# Security audit regression tests (lengths declared by the file)
# ---------------------------------------------------------------------------


def test_stsz_uniform_entry_count_bounded_by_file_size():
    """Audit finding 1: a uniform-size stsz drives ``[sample_size] * num_entries``
    straight from a 32-bit count. A 1e8 count from a ~100-byte file allocated
    ~900 MB before the offset/size consistency check ran."""
    from .helpers import _mdhd, _stco

    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    stsz = mp4_box(b"stsz", bytes(4) + (8).to_bytes(4, "big") + (10**8).to_bytes(4, "big"))
    moov = mp4_box(
        b"moov",
        mp4_box(
            b"trak",
            mp4_box(
                b"mdia",
                _mdhd(1_000_000) + mp4_box(b"minf", gmhd + mp4_box(b"stbl", stsz + _stco([0]))),
            ),
        ),
    )
    fn = _tmp_path(moov)
    try:
        with pytest.raises(ValueError, match="stsz declares 100000000 samples"):
            extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)


@pytest.mark.parametrize("key", ["stsz", "stco", "co64", "stsc"])
def test_sample_table_entry_count_bounded_by_its_box(key: str):
    """Audit finding 1 (same shape in stsz/stco/co64/stsc): the entry count must be
    checked against the bytes its own box offers (4, 4, 8 and 12 bytes per entry,
    respectively) before it drives a loop."""
    from .helpers import _mdhd, _stco, _stsz

    # stsz carries a sample_size field before the count; the others don't.
    forged = mp4_box(
        key.encode(), bytes(8 if key == "stsz" else 4) + (0xFFFFFFFF).to_bytes(4, "big")
    )
    stsz = forged if key == "stsz" else _stsz([48])
    chunk_offsets = _stco([0]) if key in ("stsz", "stsc") else forged
    stbl = mp4_box(b"stbl", stsz + chunk_offsets + (forged if key == "stsc" else b""))
    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    moov = mp4_box(
        b"moov",
        mp4_box(b"trak", mp4_box(b"mdia", _mdhd(1_000_000) + mp4_box(b"minf", gmhd + stbl))),
    )

    fn = _tmp_path(moov)
    try:
        with pytest.raises(ValueError, match=rf"{key} entry count declares 4294967295 entries"):
            extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)


def test_truncated_stmp_payload_raises_value_error():
    """Audit finding 4: the STMP value was read with a bare ``f.read()``, so a
    missing timestamp payload silently dropped a reading instead of raising.

    With the walk anchored on real bytes (round 2), a payload truncated at the
    end of the sample is stopped one layer earlier — by the parent-size guard,
    which is the cheaper and earlier stop — so either message is a pass. Both
    raise ValueError; the point of the test is that nothing is invented.
    """
    accl = accl_payload([(1, 2, 3)])
    body = accl + stmp(0)  # STMP last, so its payload is the tail of the sample
    payload = devc(strm(body))
    # Keep the STMP header (at 8+8+len(accl)) but only 3 of its 8 payload bytes.
    truncated = payload[: 16 + len(accl) + 8 + 3]
    f = io.BytesIO(truncated)

    with pytest.raises(ValueError, match="STMP timestamp|remain in its parent box"):
        get_stream_data(f, [Sample(0, len(payload))], "ACCL")


def test_truncated_scal_payload_raises_value_error():
    """Audit finding 4: SCAL divisors were read with a bare ``f.read()``, so a
    truncated divisor table scaled by whatever bytes followed.

    As with the STMP case, anchoring the walk on real bytes reports the same
    truncation one layer earlier; both paths raise ValueError.
    """
    accl = accl_payload([(1000, 1000, 1000)])
    stmp_bytes = stmp(0)
    scal_bytes = scal([1000, 1000])
    body = accl + stmp_bytes + scal_bytes  # SCAL last, so its table is the tail
    payload = devc(strm(body))
    # Keep the SCAL header (2 box headers + accl + stmp) and 2 of its 8 payload bytes.
    truncated = payload[: 16 + len(accl) + len(stmp_bytes) + 8 + 2]
    f = io.BytesIO(truncated)

    with pytest.raises(ValueError, match="SCAL divisor|remain in its parent box"):
        get_stream_data(f, [Sample(0, len(payload))], "ACCL")


def test_scal_repeat_zero_raises_value_error():
    """Audit finding 5: an empty divisor tuple made ``_divisor_at`` compute
    ``min(0, -1)`` and escape as IndexError; the API documents ValueError."""
    body = stmp(0) + scal([]) + accl_payload([(100, 200, 300)])
    payload = devc(strm(body))
    f = io.BytesIO(payload)

    with pytest.raises(ValueError, match="SCAL at offset .* has repeat == 0"):
        get_stream_data(f, [Sample(0, len(payload))], "ACCL")


def test_uniform_size_stsz_is_still_accepted():
    """The stsz guard must not reject the legal uniform-size form (no per-sample
    table), as long as the sample count fits the file."""
    from .helpers import _mdhd, _stco

    p1 = devc(strm(stmp(0) + accl_payload([(1, 2, 3)])))
    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    # Uniform form: sample_size != 0 and no per-sample table follows.
    stsz = mp4_box(b"stsz", bytes(4) + (len(p1)).to_bytes(4, "big") + (1).to_bytes(4, "big"))

    def build(offset: int) -> bytes:
        stbl = mp4_box(b"stbl", stsz + _stco([offset]))
        return mp4_box(
            b"moov",
            mp4_box(b"trak", mp4_box(b"mdia", _mdhd(1_000_000) + mp4_box(b"minf", gmhd + stbl))),
        )

    moov = build(0)
    moov = build(len(moov))
    fn = _tmp_path(moov + p1)
    try:
        telemetry = extract_all_telemetry(fn, streams=["ACCL"])
    finally:
        os.unlink(fn)

    assert [v for _, v in telemetry["ACCL"].data] == [(1.0, 2.0, 3.0)]


def _tmp_path(data: bytes) -> str:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".mp4")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _extract(mp4_bytes: bytes, **kwargs):
    """Write *mp4_bytes* to a temp file and run extract_all_telemetry on it."""
    fn = _tmp_path(mp4_bytes)
    try:
        return extract_all_telemetry(fn, **kwargs)
    finally:
        os.unlink(fn)


# ---------------------------------------------------------------------------
# Round-2 audit regression tests (review of the round-1 fixes)
# ---------------------------------------------------------------------------


def test_duplicate_stream_key_is_extracted_once():
    """A repeated key in the request list must not duplicate the readings.

    The collection dict dedupes the request, but the per-sample match list was
    built from the raw request list, so ``streams=["ACCL", "ACCL"]`` appended
    every payload twice per STRM: 4164 accelerometer readings on the real sample
    where the single key gives 2082, silently, and the CSV export wrote every row
    twice.
    """
    samples = [devc(strm(stmp(i * 1000) + accl_payload([(1, 2, 3), (4, 5, 6)]))) for i in range(5)]
    fn = _tmp_path(build_gopro_mp4(samples))
    try:
        single = extract_all_telemetry(fn, streams=["ACCL"])
        duplicated = extract_all_telemetry(fn, streams=["ACCL", "ACCL"])
        tripled = extract_all_telemetry(fn, streams=["ACCL", "ACCL", "ACCL"])
    finally:
        os.unlink(fn)

    assert single["ACCL"].sample_count == 10
    assert duplicated["ACCL"].sample_count == 10
    assert tripled["ACCL"].data == single["ACCL"].data


def test_default_streams_plus_a_repeat_extracts_each_stream_once():
    """``DEFAULT_STREAMS + ["ACCL"]`` (the "make sure ACCL is in there" idiom) must
    give the same ACCL stream as the plain default list."""
    from telemetrik.parser import DEFAULT_STREAMS

    body = stmp(0) + stnm("Accelerometer") + siun("m/s^2") + accl_payload([(1, 2, 3)])
    fn = _tmp_path(build_gopro_mp4([devc(strm(body))]))
    try:
        plain = extract_all_telemetry(fn)
        repeated = extract_all_telemetry(fn, streams=DEFAULT_STREAMS + ["ACCL"])
    finally:
        os.unlink(fn)

    assert list(repeated) == list(plain)
    assert repeated["ACCL"].data == plain["ACCL"].data


def test_declared_sample_size_beyond_the_real_bytes_raises():
    """Audit round 2, finding 2: every length that anchored the walk was declared by
    the file — the sample's stsz entry, the DEVC/STRM sizes inside it — so a 552-byte
    file whose sample declares 412 bytes and carries 52 walked straight over its
    neighbour: SHUT declared 300 readings, carried 10, and the other 289 were read
    out of the neighbouring sample's 0x07 filler. No error, and main agreed.

    The walk is clamped to bytes that exist (file end, next sample's offset), so the
    payload that does not fit is now rejected.
    """
    from .helpers import _mdhd, _stco, _stsz

    # sample 1: 52 real bytes; SHUT declares 300 uint8 readings, carries 10.
    shut = _raw_klv(b"SHUT", ord("b"), 1, 300, bytes(range(10)))
    strm_box = b"STRM" + bytes([0, 4]) + (97).to_bytes(2, "big") + stmp(0) + shut  # 388 declared
    s1 = b"DEVC" + bytes([0, 4]) + (101).to_bytes(2, "big") + strm_box  # 404 declared
    s2 = b"\x07" * 288 + b"\x00" * 72  # the filler the readings get lifted from
    sizes = [412, 0]  # stsz: sample 1 declares 412 bytes, 52 exist

    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))

    def build(stbl_payload: bytes) -> bytes:
        stbl = mp4_box(b"stbl", stbl_payload)
        return mp4_box(
            b"moov",
            mp4_box(b"trak", mp4_box(b"mdia", _mdhd(1_000_000) + mp4_box(b"minf", gmhd + stbl))),
        )

    moov = build(_stsz(sizes) + _stco([0, 0]))
    offsets, running = [], len(moov)
    for sample in (s1, s2):
        offsets.append(running)
        running += len(sample)
    mp4 = build(_stsz(sizes) + _stco(offsets)) + s1 + s2
    assert len(mp4) == 552 and len(s1) == 52

    with pytest.raises(ValueError, match="remain in its parent box"):
        _extract(mp4, streams=["SHUT"])


def test_empty_stnm_and_siun_do_not_shadow_the_real_name():
    """A blank STNM/SIUN must not become the stream's name/units.

    Real writers emit a NUL-padded (4-byte aligned) empty STNM for a stream that
    has no name yet. Latching that first box made name/units come out blank; the
    name and units of the first payload that carries a real one win, as before.
    """
    blank = _raw_klv(b"STNM", ord("c"), 4, 1, b"\x00") + klv(b"SIUN", ord("c"), 0, 0, b"")
    p1 = devc(strm(stmp(0) + blank + accl_payload([(1, 2, 3)])))
    p2 = devc(strm(stmp(1000) + stnm("Accelerometer") + siun("m/s^2") + accl_payload([(4, 5, 6)])))
    f = io.BytesIO(p1 + p2)

    stream = get_stream_data(f, [Sample(0, len(p1)), Sample(len(p1), len(p2))], "ACCL")

    assert stream.name == "Accelerometer"
    assert stream.units == "m/s^2"


def test_generator_streams_matches_list_streams():
    """``streams`` is used by the collection pass and again by the decode loop, so it
    must be materialised once: a generator was consumed by the first pass and the
    extraction returned ``{}`` with no error (the CLI printed "No telemetry found").
    """
    body = stmp(0) + accl_payload([(100, 200, 300)])
    fn = _tmp_path(build_gopro_mp4([devc(strm(body))]))
    try:
        from_generator = extract_all_telemetry(fn, streams=(key for key in ["ACCL", "GYRO"]))
        from_list = extract_all_telemetry(fn, streams=["ACCL", "GYRO"])
    finally:
        os.unlink(fn)

    assert set(from_generator) == set(from_list) == {"ACCL"}
    assert from_generator["ACCL"].data == from_list["ACCL"].data


def test_max_readings_bounds_the_decode_not_just_the_result():
    """The README sells max_readings as the bound on the ~270x RAM amplification, so
    the cap has to stop the decode. Measured with the check after the stream was
    decoded: an 8 MB / 8M-reading track with max_readings=100 peaked at 1938 MiB
    (uncapped: 2081 MiB) — the cap bounded the return value, not the damage.
    """
    import tracemalloc

    big = klv(b"SHUT", ord("b"), 1, 8000, bytes(8000))
    # 150 samples x 8000 readings = 1.2M readings from a 1.2 MB file.
    mp4 = build_gopro_mp4([devc(strm(stmp(i) + big)) for i in range(150)])
    fn = _tmp_path(mp4)
    try:
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match="max_readings"):
                extract_all_telemetry(fn, streams=["SHUT"], max_readings=100)
            peak_bytes = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    finally:
        os.unlink(fn)

    # 1.2M readings as tuples is two orders of magnitude more than this; the cap is
    # what stopped the decode, not just the returned dict.
    assert peak_bytes < 10 * 1024 * 1024, f"decode peaked at {peak_bytes / 1e6:.1f} MB at cap 100"


def test_max_readings_bounds_the_total_across_streams():
    """Each stream gets the allowance left of *max_readings*, one reading of slack
    each, so the running total is checked too: 8 + 8 readings across two streams must
    not return 16 readings under a cap of 12."""
    gyro = klv(b"GYRO", ord("s"), 6, 8, struct.pack(">24h", *([1] * 24)))
    samples = [
        devc(strm(stmp(0) + accl_payload([(1, 2, 3)] * 8))),
        devc(strm(stmp(1_000_000) + gyro)),
    ]
    fn = _tmp_path(build_gopro_mp4(samples))
    try:
        assert extract_all_telemetry(fn, streams=["ACCL"], max_readings=8)["ACCL"].sample_count == 8
        assert len(extract_all_telemetry(fn, streams=["ACCL", "GYRO"], max_readings=16)) == 2
        with pytest.raises(ValueError, match="max_readings"):
            extract_all_telemetry(fn, streams=["ACCL", "GYRO"], max_readings=12)
    finally:
        os.unlink(fn)


def test_max_readings_caps_total_extraction():
    """max_readings bounds the total readings extracted: at the cap the
    extraction succeeds, one reading over raises ValueError, and the default
    (None) leaves extraction unlimited."""
    samples = [
        devc(strm(stmp(i * 1000) + accl_payload([(1, 2, 3), (4, 5, 6)]))) for i in range(5)
    ]  # 5 samples x 2 readings = 10 readings
    fn = _tmp_path(build_gopro_mp4(samples))
    try:
        assert extract_all_telemetry(fn, streams=["ACCL"])["ACCL"].sample_count == 10
        assert (
            extract_all_telemetry(fn, streams=["ACCL"], max_readings=10)["ACCL"].sample_count == 10
        )
        with pytest.raises(ValueError, match="max_readings"):
            extract_all_telemetry(fn, streams=["ACCL"], max_readings=9)
    finally:
        os.unlink(fn)
