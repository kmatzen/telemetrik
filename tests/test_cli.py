"""Tests for the telemetrik CLI.

See GitHub issue #8: main() always exited 0 even when every file failed,
output was silently overwritten with no way to redirect it, and CSV output
had no header and didn't use the csv module.
"""

import csv
import inspect
import os
import sys
from pathlib import Path

import pytest

from telemetrik import cli
from telemetrik.cli import main, save_stream_to_file
from telemetrik.parser import TelemetryStream


def _make_stream(key="ACCL", data=None, pts_data=None):
    if data is None:
        data = [(0.0, (1.0, 2.0, 3.0)), (10.0, (4.0, 5.0, 6.0))]
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
    assert rows[1] == ["0.0", "1.0", "2.0", "3.0"]
    assert rows[2] == ["10.0", "4.0", "5.0", "6.0"]


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


def test_save_stream_to_file_escapes_csv_formula_injection(tmp_path):
    """A string value starting with '=' must not become a live spreadsheet formula."""
    stream = _make_stream(key="TXTA", data=[(0, "=cmd|'/c calc'!A0"), (1, "+EVIL"), (2, "@x")])
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    output_path = save_stream_to_file(stream, video_path)

    with open(output_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1] == ["0", "'=cmd|'/c calc'!A0"]
    assert rows[2] == ["1", "'+EVIL"]
    assert rows[3] == ["2", "'@x"]


def test_save_stream_to_file_keeps_negative_numbers_unescaped(tmp_path):
    """str(-117.3) starts with '-' but is a number, not a formula; it must stay intact."""
    stream = _make_stream(
        key="GPS5",
        data=[
            (0, (-117.3, 33.4, 100.0, -5.5, 1.0, 3, 0.9)),
            (1, (37.0, -122.0, 0.0, 0.0, 0.0, 0, 0.0)),
        ],
    )
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    output_path = save_stream_to_file(stream, video_path)

    with open(output_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1] == ["0", "-117.3", "33.4", "100.0", "-5.5", "1.0", "3", "0.9"]
    assert rows[2] == ["1", "37.0", "-122.0", "0.0", "0.0", "0.0", "0", "0.0"]


def test_save_stream_to_file_refuses_to_overwrite_without_force(tmp_path):
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    existing = tmp_path / "GX010042.accl"
    existing.write_text("existing content")

    with pytest.raises(FileExistsError) as excinfo:
        save_stream_to_file(stream, video_path)

    # The CLI is the only place a user learns --force exists (the README never
    # quotes it), so the hint must survive the exclusive-create refactor.
    assert "force" in str(excinfo.value)
    assert "--force" in str(excinfo.value)
    assert existing.read_text() == "existing content"


def test_save_stream_to_file_overwrites_with_force(tmp_path):
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    existing = tmp_path / "GX010042.accl"
    existing.write_text("existing content")

    save_stream_to_file(stream, video_path, force=True)

    assert "existing content" not in existing.read_text()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX-only symlink semantics")
def test_save_stream_to_file_with_force_does_not_follow_symlink(tmp_path):
    """A symlink planted at the output path must not be written through (check-then-write race)."""
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    secret = tmp_path / "secret.txt"
    secret.write_text("DO NOT TOUCH")
    link = tmp_path / "GX010042.accl"
    link.symlink_to(secret)

    with pytest.raises(OSError):
        save_stream_to_file(stream, video_path, force=True)

    assert secret.read_text() == "DO NOT TOUCH"
    assert link.is_symlink()


def test_save_stream_to_file_without_force_refuses_existing_symlink_output(tmp_path):
    """Exclusive create refuses a symlinked output path, leaving the target intact.

    Pins the *observable* contract only: the file is refused and the target is
    untouched. It cannot observe the check-then-write race window (main's
    exists() check passed for the same input); the force=True sibling, which
    shows O_NOFOLLOW doing the work, is the race-sensitive test.
    """
    stream = _make_stream()
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    secret = tmp_path / "secret.txt"
    secret.write_text("DO NOT TOUCH")
    link = tmp_path / "GX010042.accl"
    link.symlink_to(secret)

    with pytest.raises(FileExistsError):
        save_stream_to_file(stream, video_path)

    assert secret.read_text() == "DO NOT TOUCH"


def test_save_stream_to_file_uses_pts_seconds_converted_to_ms(tmp_path):
    stream = _make_stream(pts_data=[(0.0, (1.0, 2.0, 3.0)), (0.5, (4.0, 5.0, 6.0))])
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    output_path = save_stream_to_file(stream, video_path)

    with open(output_path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1][0] == "0.0"
    assert rows[2][0] == "500.0"


def test_main_passes_max_readings_to_extraction(tmp_path, monkeypatch):
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    stream = _make_stream()
    captured = {}

    def fake_extract(path, streams=None, max_readings=None):
        captured["streams"] = streams
        captured["max_readings"] = max_readings
        return {"ACCL": stream}

    monkeypatch.setattr(
        sys,
        "argv",
        ["telemetrik", str(video_path), "--streams", "ACCL", "--max-readings", "5000"],
    )
    monkeypatch.setattr("telemetrik.cli.extract_all_telemetry", fake_extract)

    main()

    assert captured["streams"] == ["ACCL"]
    assert captured["max_readings"] == 5000


def test_main_max_readings_defaults_to_none(tmp_path, monkeypatch):
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    captured = {}

    def fake_extract(path, streams=None, max_readings=None):
        captured["max_readings"] = max_readings
        return {}

    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video_path)])
    monkeypatch.setattr("telemetrik.cli.extract_all_telemetry", fake_extract)

    main()

    assert captured["max_readings"] is None


def test_main_reports_max_readings_cap_as_clean_error(tmp_path, monkeypatch, capsys):
    """Hitting the cap must print 'Error: …' and exit 1, not raise a traceback."""
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    def fake_extract(path, streams=None, max_readings=None):
        if max_readings is not None and max_readings < 1000:
            raise ValueError(
                f"Telemetry extraction exceeds max_readings={max_readings} (1000 readings)"
            )
        return {"ACCL": _make_stream()}

    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video_path), "--max-readings", "10"])
    monkeypatch.setattr("telemetrik.cli.extract_all_telemetry", fake_extract)

    with pytest.raises(SystemExit) as exc_info:
        main()

    err = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "Error" in err and "max_readings" in err
    assert "Traceback" not in err


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
        "telemetrik.cli.extract_all_telemetry",
        lambda path, streams=None, max_readings=None: {"ACCL": stream},
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
        "telemetrik.cli.extract_all_telemetry",
        lambda path, streams=None, max_readings=None: {"ACCL": stream},
    )

    main()

    assert (output_dir / "GX010042.accl").exists()


def test_main_reports_no_telemetry_without_failing(tmp_path, monkeypatch, capsys):
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()

    monkeypatch.setattr(sys, "argv", ["telemetrik", str(video_path)])
    monkeypatch.setattr(
        "telemetrik.cli.extract_all_telemetry", lambda path, streams=None, max_readings=None: {}
    )

    main()  # must not raise SystemExit

    assert "No telemetry found" in capsys.readouterr().out


def test_main_does_not_crash_when_video_path_is_a_directory(tmp_path, monkeypatch, capsys):
    """A directory (or other OSError on open) must print an error and exit 1,
    not crash with a traceback."""
    monkeypatch.setattr(sys, "argv", ["telemetrik", str(tmp_path)])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert "Error" in capsys.readouterr().err


def test_main_reports_uncreatable_output_dir_without_traceback(tmp_path, monkeypatch, capsys):
    """An uncreatable --output-dir must print 'Error: …' and exit non-zero, not raise."""
    readonly_parent = tmp_path / "ro"
    readonly_parent.mkdir()
    readonly_parent.chmod(0o555)
    try:
        video_path = tmp_path / "GX010042.MP4"
        video_path.touch()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "telemetrik",
                str(video_path),
                "--output-dir",
                str(readonly_parent / "nested" / "deeper"),
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()  # must not raise PermissionError/OSError

        assert exc_info.value.code == 1
        assert "Error" in capsys.readouterr().err
    finally:
        readonly_parent.chmod(0o755)


def test_cli_has_no_dead_struct_error_handler():
    """struct.error/ZeroDivisionError were unreachable (the parser raises ValueError first)."""
    assert not hasattr(cli, "struct")
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "struct.error" not in source
    assert "ZeroDivisionError" not in source


def test_cli_has_no_duplicate_file_exists_error_handler():
    """FileExistsError subclasses OSError, so an identical-bodied arm before
    'except OSError' in main() can never change behaviour (it existed for a
    bespoke message that is now raised inside _open_output)."""
    main_source = inspect.getsource(cli.main)
    assert "except FileExistsError" not in main_source
    assert "except OSError" in main_source


def test_main_continues_after_save_failure(tmp_path, monkeypatch, capsys):
    """An unwritable output dir must not crash the CLI for remaining streams."""
    video_path = tmp_path / "GX010042.MP4"
    video_path.touch()
    stream = _make_stream()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_dir.chmod(0o555)
    try:
        monkeypatch.setattr(
            sys, "argv", ["telemetrik", str(video_path), "--output-dir", str(out_dir)]
        )
        monkeypatch.setattr(
            "telemetrik.cli.extract_all_telemetry",
            lambda path, streams=None, max_readings=None: {"ACCL": stream},
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert "Error" in capsys.readouterr().err
    finally:
        out_dir.chmod(0o755)
