"""Command-line interface for extracting GPMF telemetry to CSV."""

import argparse
import json
import sys
from pathlib import Path

from gpmf_parser import TelemetryStream, extract_all_telemetry


def save_stream_to_file(stream: TelemetryStream, video_path: Path) -> None:
    """Save a telemetry stream to a CSV file next to the source video."""
    suffix = stream.key.lower()
    output_path = video_path.parent / f"{video_path.stem}.{suffix}"

    data_to_save = stream.pts_data if stream.pts_data else stream.data
    timestamp_type = "PTS" if stream.pts_data else "STMP"

    with open(output_path, "w") as f:
        for timestamp, value in data_to_save:
            if isinstance(value, (list, tuple)):
                value_str = ",".join(str(v) for v in value)
            elif isinstance(value, dict):
                value_str = json.dumps(value)
            else:
                value_str = str(value)

            timestamp_ms = timestamp * 1000.0 if stream.pts_data else timestamp
            f.write(f"{timestamp_ms},{value_str}\n")

    print(f"  {stream.key}: {output_path} ({stream.sample_count} samples, {timestamp_type})")


def main() -> None:
    """Extract telemetry from GoPro MP4 files to CSV."""
    parser = argparse.ArgumentParser(
        prog="gpmf-extract",
        description="Extract GPMF telemetry from GoPro MP4 files",
    )
    parser.add_argument("video_files", nargs="+", type=str, help="GoPro MP4 file(s)")
    parser.add_argument(
        "--streams",
        type=str,
        default=None,
        help="Comma-separated stream keys (default: all). E.g. ACCL,GYRO,GPS5",
    )

    args = parser.parse_args()

    streams = None
    if args.streams:
        streams = [s.strip().upper() for s in args.streams.split(",")]

    for video_file in args.video_files:
        video_path = Path(video_file)
        if not video_path.exists():
            print(f"Error: {video_path} does not exist", file=sys.stderr)
            continue

        print(f"{video_path}:")
        try:
            telemetry = extract_all_telemetry(str(video_path), streams=streams)
        except ValueError as e:
            print(f"  Error: {e}", file=sys.stderr)
            continue

        if not telemetry:
            print("  No telemetry found")
            continue

        for stream in telemetry.values():
            save_stream_to_file(stream, video_path)


if __name__ == "__main__":
    main()
