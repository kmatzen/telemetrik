import io

from telemetrik.parser import Sample, _get_gps_data_with_sticky_metadata

from .gpmf_builder import devc_block, s16_values, strm_block


def _build_gps_samples(name=None, units=None):
    strm1 = strm_block(
        stmp_us=1_000_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([100, 200, 300, 10, 20]),
        scal=10,
        gpsf=3,
        gpsp=100,
        name=name,
        units=units,
    )
    # No GPSF/GPSP box here -> should inherit sticky values from strm1.
    strm2 = strm_block(
        stmp_us=1_001_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([110, 210, 310, 15, 25]),
        scal=10,
    )
    devc1 = devc_block([strm1])
    devc2 = devc_block([strm2])
    buf = io.BytesIO(devc1 + devc2)
    samples = [
        Sample(offset=0, size=len(devc1), pts=0, dts=0),
        Sample(offset=len(devc1), size=len(devc2), pts=0, dts=0),
    ]
    return buf, samples


def test_gps_sticky_fix_and_precision_carry_forward():
    buf, samples = _build_gps_samples()

    stream = _get_gps_data_with_sticky_metadata(buf, samples)

    assert stream.key == "GPS5"
    assert stream.sample_count == 2
    assert stream.data[0] == (1000, (10.0, 20.0, 30.0, 1.0, 2.0, 3, 100))
    # Second sample has no GPSF/GPSP box -> sticky values from sample 1 persist.
    assert stream.data[1] == (1001, (11.0, 21.0, 31.0, 1.5, 2.5, 3, 100))


def test_gps_default_name_and_units_when_absent():
    buf, samples = _build_gps_samples()

    stream = _get_gps_data_with_sticky_metadata(buf, samples)

    assert stream.name == "GPS (Latitude, Longitude, Altitude)"
    assert stream.units == "deg,deg,m,m/s,m/s,fix,dop"


def test_gps_uses_stnm_and_siun_when_present():
    buf, samples = _build_gps_samples(name="GPS", units="deg,deg,m,m/s,m/s")

    stream = _get_gps_data_with_sticky_metadata(buf, samples)

    assert stream.name == "GPS"
    assert stream.units == "deg,deg,m,m/s,m/s,fix,dop"


def test_gps_time_base_populates_pts_data():
    buf, samples = _build_gps_samples()
    samples[0] = Sample(offset=samples[0].offset, size=samples[0].size, pts=2000, dts=0)
    samples[1] = Sample(offset=samples[1].offset, size=samples[1].size, pts=2100, dts=0)

    stream = _get_gps_data_with_sticky_metadata(buf, samples, time_base=(1, 1000))

    assert stream.pts_data is not None
    assert stream.pts_data[0][0] == 2.0
    assert stream.pts_data[1][0] == 2.1


def test_gps_skips_devc_sample_without_gps5_strm():
    non_gps_strm = strm_block(
        stmp_us=500_000,
        stream_key="ACCL",
        type_char="s",
        struct_size=6,
        repeat=1,
        values_payload=s16_values([1, 2, 3]),
    )
    gps_strm1 = strm_block(
        stmp_us=1_000_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([100, 200, 300, 10, 20]),
        scal=10,
        gpsf=3,
        gpsp=100,
    )
    gps_strm2 = strm_block(
        stmp_us=1_001_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([110, 210, 310, 15, 25]),
        scal=10,
    )
    devc_no_gps = devc_block([non_gps_strm])
    devc_gps1 = devc_block([gps_strm1])
    devc_gps2 = devc_block([gps_strm2])
    buf = io.BytesIO(devc_no_gps + devc_gps1 + devc_gps2)
    samples = [
        Sample(offset=0, size=len(devc_no_gps), pts=0, dts=0),
        Sample(offset=len(devc_no_gps), size=len(devc_gps1), pts=0, dts=0),
        Sample(offset=len(devc_no_gps) + len(devc_gps1), size=len(devc_gps2), pts=0, dts=0),
    ]

    stream = _get_gps_data_with_sticky_metadata(buf, samples)

    # The DEVC sample without a GPS5 stream is skipped entirely.
    assert stream.sample_count == 2
    assert stream.data[0] == (1000, (10.0, 20.0, 30.0, 1.0, 2.0, 3, 100))


def test_gps_skips_strm_without_stmp():
    strm = strm_block(
        stmp_us=0,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([100, 200, 300, 10, 20]),
        scal=10,
        include_stmp=False,
    )
    devc = devc_block([strm])
    buf = io.BytesIO(devc)
    samples = [Sample(offset=0, size=len(devc), pts=0, dts=0)]

    stream = _get_gps_data_with_sticky_metadata(buf, samples)

    assert stream.sample_count == 0
    assert stream.data == []


def test_gps_wide_scal_divisor():
    strm1 = strm_block(
        stmp_us=1_000_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([100, 200, 300, 10, 20]),
        scal=10,
        scal_wide=True,
        gpsf=3,
        gpsp=100,
    )
    strm2 = strm_block(
        stmp_us=1_001_000,
        stream_key="GPS5",
        type_char="s",
        struct_size=10,
        repeat=1,
        values_payload=s16_values([110, 210, 310, 15, 25]),
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

    stream = _get_gps_data_with_sticky_metadata(buf, samples)

    assert stream.data[0] == (1000, (10.0, 20.0, 30.0, 1.0, 2.0, 3, 100))
