import sys

from telemetrik import TelemetryStream, cli

from .gpmf_builder import build_mp4, devc_block, s16_values, strm_block


def _write_mp4(tmp_path, filename="video.mp4"):
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
    mp4_bytes = build_mp4(
        [devc1, devc2], timescale=1000, sample_deltas=[1000, 1000], composition_offsets=[0, 0]
    )
    path = tmp_path / filename
    path.write_bytes(mp4_bytes)
    return path


def test_cli_extracts_all_streams_to_files(tmp_path, monkeypatch, capsys):
    video = _write_mp4(tmp_path)
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video)])

    cli.main()

    accl_file = tmp_path / "video.accl"
    gps_file = tmp_path / "video.gps5"
    assert accl_file.exists()
    assert gps_file.exists()

    accl_lines = accl_file.read_text().strip().splitlines()
    assert len(accl_lines) == 2
    # CLI prefers PTS-based timestamps (video timeline) over STMP when available.
    assert accl_lines[0] == "0.0,1.0,2.0,3.0"
    assert accl_lines[1] == "1000.0,1.1,2.1,3.1"

    out = capsys.readouterr().out
    assert "ACCL" in out
    assert "GPS5" in out


def test_cli_respects_streams_filter(tmp_path, monkeypatch):
    video = _write_mp4(tmp_path)
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video), "--streams", "gps5"])

    cli.main()

    assert not (tmp_path / "video.accl").exists()
    assert (tmp_path / "video.gps5").exists()


def test_cli_reports_missing_file(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nope.mp4"
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(missing)])

    cli.main()

    err = capsys.readouterr().err
    assert "does not exist" in err


def test_cli_reports_value_error_for_non_gpmf_video(tmp_path, monkeypatch, capsys):
    video = _write_mp4(tmp_path)
    # Corrupt the file so it no longer parses as a valid GPMF-bearing MP4.
    video.write_bytes(b"not a real mp4 file")
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video)])

    cli.main()

    err = capsys.readouterr().err
    assert "Error" in err


def test_cli_reports_no_telemetry_found(tmp_path, monkeypatch, capsys):
    video = _write_mp4(tmp_path)
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video), "--streams", "TMPC"])

    cli.main()

    out = capsys.readouterr().out
    assert "No telemetry found" in out


def test_cli_writes_gps5_extended_columns(tmp_path, monkeypatch):
    video = _write_mp4(tmp_path)
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video), "--streams", "GPS5"])

    cli.main()

    gps_lines = (tmp_path / "video.gps5").read_text().strip().splitlines()
    assert gps_lines[0] == "0.0,10.0,20.0,30.0,1.0,2.0,3,50"
    assert gps_lines[1] == "1000.0,11.0,21.0,31.0,1.5,2.5,3,50"


def test_save_stream_to_file_handles_scalar_and_dict_values(tmp_path):
    scalar_stream = TelemetryStream(
        key="SHUT", sample_count=1, name="Shutter", units="s", data=[(0, 0.005)]
    )
    cli.save_stream_to_file(scalar_stream, tmp_path / "video.mp4")
    assert (tmp_path / "video.shut").read_text().strip() == "0,0.005"

    dict_stream = TelemetryStream(
        key="META", sample_count=1, name="Meta", units="", data=[(0, {"a": 1})]
    )
    cli.save_stream_to_file(dict_stream, tmp_path / "video.mp4")
    assert (tmp_path / "video.meta").read_text().strip() == '0,{"a": 1}'
