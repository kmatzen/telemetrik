# telemetrik

[![PyPI](https://img.shields.io/pypi/v/telemetrik)](https://pypi.org/project/telemetrik/)
[![Python](https://img.shields.io/pypi/pyversions/telemetrik)](https://pypi.org/project/telemetrik/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Extract GPS, gyroscope, accelerometer, and other sensor data from GoPro video files.

telemetrik is a pure-Python parser for the [GoPro Metadata Format (GPMF)](https://github.com/gopro/gpmf-parser) — no compiled dependencies, no FFmpeg, just `pip install` and go.

## Install

```
pip install telemetrik
```

## Usage

### Python

```python
from telemetrik import extract_all_telemetry

streams = extract_all_telemetry("GX010042.MP4")

# GPS track
for ts, (lat, lon, alt, spd2d, spd3d, fix, dop) in streams["GPS5"].data:
    if fix >= 3:  # 3D fix
        print(f"{lat:.6f}, {lon:.6f}  alt={alt:.0f}m")

# 200 Hz accelerometer
for ts, (x, y, z) in streams["ACCL"].data:
    print(f"{ts}ms  accel=({x:.2f}, {y:.2f}, {z:.2f}) m/s²")

# Camera orientation quaternions
for ts, (w, x, y, z) in streams["CORI"].data:
    print(f"{ts}ms  quat=({w:.4f}, {x:.4f}, {y:.4f}, {z:.4f})")
```

### Command line

```bash
telemetrik GX010042.MP4                        # extract all streams
telemetrik --streams ACCL,GYRO,GPS5 *.MP4      # specific streams, batch
```

Each stream is saved as `<stem>.<fourcc>` (e.g. `GX010042.accl`, `GX010042.gps5`) in CSV format with millisecond timestamps.

## Supported streams

| FourCC | Stream | Shape |
|--------|--------|-------|
| `ACCL` | Accelerometer | `(x, y, z)` m/s² |
| `GYRO` | Gyroscope | `(x, y, z)` rad/s |
| `GPS5` | GPS + quality | `(lat, lon, alt, speed_2d, speed_3d, fix, dop)` |
| `CORI` | Camera orientation | `(w, x, y, z)` quaternion |
| `IORI` | Image orientation | `(w, x, y, z)` quaternion |
| `GRAV` | Gravity vector | `(x, y, z)` |
| `SHUT` | Shutter speed | scalar, microseconds |
| `ISOE` | ISO | scalar |
| `WBAL` | White balance | `(temperature, tint)` |
| `TMPC` | Sensor temperature | scalar, °C |

Pass any GPMF FourCC to extract unlisted streams: `extract_all_telemetry(path, streams=["MWET"])`.

## Timestamps

Every `TelemetryStream` carries two timestamp arrays:

- **`data`** — `(ms, value)` pairs from the GPMF STMP clock (millisecond precision, always available)
- **`pts_data`** — `(seconds, value)` pairs on the video presentation timeline (available when the MP4 has timing atoms)

Use `pts_data` to align telemetry with video frames.

## GPS quality fields

The GPS stream includes two "sticky" metadata fields that persist across samples:

| Field | Values | Meaning |
|-------|--------|---------|
| `fix` | 0 / 2 / 3 | No lock / 2D / 3D |
| `dop` | 0–9999 | Dilution of precision (lower = better) |

Filter on `fix >= 3` and `dop < 500` for reliable positions.

## API reference

### `extract_all_telemetry(video_path, streams=None)`

Extract all (or selected) telemetry streams from an MP4 file.

**Parameters:**
- `video_path` — path to a GoPro MP4 file
- `streams` — list of FourCC strings, or `None` for all default streams

**Returns:** `dict[str, TelemetryStream]` — only streams with data are included.

**Raises:** `ValueError` if the file has no GPMF metadata track.

### `TelemetryStream`

| Attribute | Type | Description |
|-----------|------|-------------|
| `key` | `str` | GPMF FourCC |
| `sample_count` | `int` | Number of readings |
| `name` | `str` | Human-readable name (from STNM) |
| `units` | `str` | SI unit string (from SIUN) |
| `data` | `list[tuple[int, Any]]` | `(timestamp_ms, value)` |
| `pts_data` | `list[tuple[float, Any]] \| None` | `(pts_seconds, value)` |

### `get_stream_data(f, samples, stream_key, scale=True, time_base=None)`

Low-level extraction of a single stream from an open file handle. Use this when you need control over file I/O or want to extract non-standard streams.

## Attribution

Core MP4 box walking and GPMF binary parsing adapted from [gopro-video-sync](https://github.com/evoth/gopro-video-sync) by Ethan Vo (MIT).

## License

MIT
