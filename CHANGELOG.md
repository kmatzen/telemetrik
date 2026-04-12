# Changelog

## 0.1.0 (2026-04-11)

Initial release.

- Extract 10 telemetry streams from GoPro MP4 files (ACCL, GYRO, GPS5, CORI, IORI, GRAV, SHUT, ISOE, WBAL, TMPC)
- GPS5 includes sticky GPSF (fix type) and GPSP (precision) metadata
- Dual timestamps: STMP-based (millisecond) and PTS-based (video timeline)
- `telemetrik` CLI for batch CSV extraction
- Pure Python, zero dependencies, Python 3.9+
