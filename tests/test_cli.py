"""Tests for the telemetrik CLI.

See GitHub issue #8: main() always exited 0 even when every file failed,
output was silently overwritten with no way to redirect it, and CSV output
had no header and didn't use the csv module.
"""

import csv
import sys

import pytest

from telemetrik.cli import main, save_stream_to_file
from telemetrik.parser import TelemetryStream


def _make_stream(key="ACCL", data=None, pts_data=None):
    if data is None:
        data = [(0, (1.0, 2.0, 3.0)), (10, (4.0, 5.0, 6.0))]
    return TelemetryStream(
        key=key,
        sample_count=len(data),
        name="Accelerometer",
        units="m/s^2",
        data=data,
        pts_data=pts_data,
    )


def test_save_stream_to_file_writes_header_and_rows(tmp_path):
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    output_path = save_stream_to_file(stream, video_path)

    assert output_path == tmp_path / "GX010042.accl"
    with open(output_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["timestamp_ms", "x", "y", "z"]
    assert rows[1] == ["0", "1.0", "2.0", "3.0"]
    assert rows[2] == ["10", "4.0", "5.0", "6.0"]


def test_save_stream_to_file_unknown_stream_uses_generic_column_names(tmp_path):
    stream = _make_stream(key="MWET", data=[(0, (1.0, 0.0))])
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    output_path = save_stream_to_file(stream, video_path)

    with open(output_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["timestamp_ms", "value_1", "value_2"]


def test_save_stream_to_file_uses_output_dir(tmp_path):
    stream = _make_stream()
    video_path = tmp_path / "videos" / "GX010042.MP4"
    video_path.parent.mkdir()
    video_path.touch()
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    output_path = save_stream_to_file(stream, video_path, output_dir=output_dir)

    assert output_path == output_dir / "GX010042.accl"
    assert output_path.exists()


def test_save_stream_to_file_refuses_to_overwrite_without_force(tmp_path):
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    existing = tmp_path / "GX010042.accl"
    existing.write_text("existing content")

    with pytest.raises(FileExistsError):
        save_stream_to_file(stream, video_path)

    assert existing.read_text() == "existing content"


def test_save_stream_to_file_overwrites_with_force(tmp_path):
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    existing = tmp_path / "GX010042.accl"
    existing.write_text("existing content")

    save_stream_to_file(stream, video_path, force=True)

    assert "existing content" not in existing.read_text()


def test_save_stream_to_file_uses_pts_seconds_converted_to_ms(tmp_path):
    stream = _make_stream(pts_data=[(0.0, (1.0, 2.0, 3.0)), (0.5, (4.0, 5.0, 6.0))])
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    output_path = save_stream_to_file(stream, video_path)

    with open(output_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1][0] == "0.0"
    assert rows[2][0] == "500.0"


def test_main_exits_nonzero_when_a_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(tmp_path / "missing.MP4")])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1


def test_main_exits_nonzero_when_extraction_fails(tmp_path, monkeypatch):
    video_path = tmp_path / "GX010042.MP4"
    video_path.write_bytes(b"not a real mp4")
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video_path)])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1


def test_main_writes_files_and_does_not_exit_on_success(tmp_path, monkeypatch):
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    stream = _make_stream()

    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video_path)])
    monkeypatch.setattr(
        "telemetrik.cli.extract_all_telemetry", lambda path, streams=None: {"ACCL": stream}
    )

    main()  # must not raise SystemExit

    assert (tmp_path / "GX010042.accl").exists()


def test_main_respects_output_dir_flag(tmp_path, monkeypatch):
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    output_dir = tmp_path / "out"
    stream = _make_stream()

    monkeypatch.setattr(
        sys, "argv", ["telemetrik", str(video_path), "--output-dir", str(output_dir)]
    )
    monkeypatch.setattr(
        "telemetrik.cli.extract_all_telemetry", lambda path, streams=None: {"ACCL": stream}
    )

    main()

    assert (output_dir / "GX010042.accl").exists()


def test_main_reports_no_telemetry_without_failing(tmp_path, monkeypatch, capsys):
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video_path)])
    monkeypatch.setattr("telemetrik.cli.extract_all_telemetry", lambda path, streams=None: {})

    main()  # must not raise SystemExit

    assert "No telemetry found" in capsys.readouterr().out
