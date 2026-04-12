# telemetrik

Pure-Python parser for [GoPro Metadata Format (GPMF)](https://github.com/gopro/gpmf-parser). Extract GPS, gyroscope, accelerometer, orientation, exposure, and other telemetry from GoPro MP4 files — no compiled dependencies required.

## Install

```bash
pip install telemetrik
```

## Quick start

```python
from telemetrik import extract_all_telemetry

streams = extract_all_telemetry("GX010042.MP4")

# GPS track
gps = streams["GPS5"]
for timestamp_ms, (lat, lon, alt, speed_2d, speed_3d, fix, dop) in gps.data:
    print(f"{timestamp_ms}ms: {lat:.6f}, {lon:.6f} (alt={alt:.1f}m, fix={fix})")

# IMU data
accel = streams["ACCL"]
for timestamp_ms, (x, y, z) in accel.data:
    print(f"{timestamp_ms}ms: accel=({x:.3f}, {y:.3f}, {z:.3f})")

# Camera orientation quaternions
if "CORI" in streams:
    cori = streams["CORI"]
    for timestamp_ms, (w, x, y, z) in cori.data:
        print(f"{timestamp_ms}ms: quat=({w:.4f}, {x:.4f}, {y:.4f}, {z:.4f})")
```

## CLI

```bash
# Extract all streams to CSV files alongside the video
telemetrik GX010042.MP4

# Extract specific streams only
telemetrik --streams ACCL,GYRO,GPS5 GX010042.MP4

# Batch extract
telemetrik *.MP4
```

Output files are named `<video_stem>.<stream_key>` (e.g., `GX010042.accl`, `GX010042.gps5`).

## Supported streams

| FourCC | Description | Data shape |
|--------|-------------|------------|
| `ACCL` | Accelerometer | `(x, y, z)` m/s² |
| `GYRO` | Gyroscope | `(x, y, z)` rad/s |
| `GPS5` | GPS position + speed | `(lat, lon, alt, speed_2d, speed_3d, gps_fix, gps_precision)` |
| `CORI` | Camera orientation | `(w, x, y, z)` quaternion |
| `IORI` | Image orientation | `(w, x, y, z)` quaternion |
| `GRAV` | Gravity vector | `(x, y, z)` |
| `SHUT` | Shutter speed | scalar (µs) |
| `ISOE` | ISO exposure | scalar |
| `WBAL` | White balance | `(temp, tint)` |
| `TMPC` | Temperature | scalar (°C) |

Custom streams can be requested by passing their FourCC to `extract_all_telemetry(path, streams=["MYFC"])`.

## Timestamps

Each `TelemetryStream` provides two timestamp sources:

- **`data`**: `(timestamp_ms, value)` — STMP-based timestamps from the GPMF track (millisecond precision)
- **`pts_data`**: `(pts_seconds, value)` — mapped to the video presentation timeline (available when the MP4 contains timing atoms)

Use `pts_data` when you need to correlate telemetry with video frame numbers.

## GPS metadata

GPS5 streams include "sticky" metadata that persists across samples:

- **GPSF** (GPS fix): 0 = no lock, 2 = 2D, 3 = 3D
- **GPSP** (GPS precision): DOP value (lower is better)

These are appended as the 6th and 7th elements of each GPS5 tuple.

## Attribution

Core MP4 box walking and GPMF binary parsing adapted from [gopro-video-sync](https://github.com/evoth/gopro-video-sync) by Ethan Vo (MIT License).

## License

MIT
