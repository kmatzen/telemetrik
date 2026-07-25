"""Command-line interface for extracting GPMF telemetry to CSV."""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

from telemetrik import TelemetryStream, extract_all_telemetry

#: Column names for the streams telemetrik knows the shape of. Streams outside
#: this table (including arbitrary FourCCs passed via --streams) fall back to
#: generic names derived from the value shape.
_COLUMN_NAMES = {
    "ACCL": ("x", "y", "z"),
    "GYRO": ("x", "y", "z"),
    "GRAV": ("x", "y", "z"),
    "CORI": ("w", "x", "y", "z"),
    "IORI": ("w", "x", "y", "z"),
    "GPS5": ("lat", "lon", "alt", "speed_2d", "speed_3d", "fix", "dop"),
    "SHUT": ("shutter_speed",),
    "ISOE": ("iso",),
    "WBAL": ("temperature", "tint"),
    "TMPC": ("temperature",),
}


def _columns_for(stream: TelemetryStream) -> list[str]:
    """Column names for a stream's CSV body, after the leading timestamp_ms."""
    names = _COLUMN_NAMES.get(stream.key)
    if names:
        return list(names)

    sample_value = stream.data[0][1] if stream.data else None
    if isinstance(sample_value, (list, tuple)):
        return [f"value_{i + 1}" for i in range(len(sample_value))]
    return ["value"]


def _row_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [json.dumps(value)]
    if isinstance(value, bytes):
        return [value.hex()]
    return [str(value)]


def save_stream_to_file(
    stream: TelemetryStream,
    video_path: Path,
    output_dir: Optional[Path] = None,
    force: bool = False,
) -> Path:
    """Save a telemetry stream to a CSV file next to the source video (or *output_dir*).

    Raises:
        FileExistsError: If the output file already exists and *force* is False.
    """
    suffix = stream.key.lower()
    target_dir = output_dir if output_dir is not None else video_path.parent
    output_path = target_dir / f"{video_path.stem}.{suffix}"

    if output_path.exists() and not force:
        raise FileExistsError(f"{output_path} already exists (use --force to overwrite)")

    data_to_save = stream.pts_data if stream.pts_data else stream.data
    timestamp_type = "PTS" if stream.pts_data else "STMP"

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", *_columns_for(stream)])
        for timestamp, value in data_to_save:
            timestamp_ms = timestamp * 1000.0 if stream.pts_data else timestamp
            writer.writerow([timestamp_ms, *_row_values(value)])

    print(f"  {stream.key}: {output_path} ({stream.sample_count} samples, {timestamp_type})")
    return output_path


def main() -> None:
    """Extract telemetry from GoPro MP4 files to CSV."""
    parser = argparse.ArgumentParser(
        prog="telemetrik",
        description="Extract GPMF telemetry from GoPro MP4 files",
    )
    parser.add_argument("video_files", nargs="+", type=str, help="GoPro MP4 file(s)")
    parser.add_argument(
        "--streams",
        type=str,
        default=None,
        help="Comma-separated stream keys (default: all). E.g. ACCL,GYRO,GPS5",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to write CSV files to (default: next to each source video)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output files instead of skipping them",
    )

    args = parser.parse_args()

    streams = None
    if args.streams:
        streams = [s.strip().upper() for s in args.streams.split(",")]

    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    had_failure = False

    for video_file in args.video_files:
        video_path = Path(video_file)
        if not video_path.exists():
            print(f"Error: {video_path} does not exist", file=sys.stderr)
            had_failure = True
            continue

        print(f"{video_path}:")
        try:
            telemetry = extract_all_telemetry(str(video_path), streams=streams)
        except ValueError as e:
            print(f"  Error: {e}", file=sys.stderr)
            had_failure = True
            continue

        if not telemetry:
            print("  No telemetry found")
            continue

        for stream in telemetry.values():
            try:
                save_stream_to_file(stream, video_path, output_dir, args.force)
            except FileExistsError as e:
                print(f"  Error: {e}", file=sys.stderr)
                had_failure = True

    if had_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
