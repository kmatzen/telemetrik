# Changelog

## 0.1.1 (2026-11-25)

Security and robustness release. A hostile MP4 can no longer drive the parser past the bytes it declares.

- Stop trusting every length declared by the file; anchor the walk on real bytes, fix duplicate keys, blank names, generator streams, and add a read cap.
- Fix CLI CSV injection, a symlink write race, an `mkdir` traceback, and dead exception handlers.
- Add `--max-readings`, restore the `--force` hint, drop dead code.
- Collect all requested streams in one pass.
- CI: least-privilege tokens, locked dependency installs, hard sample-file guard.
- Version now comes from `__init__.py`; the accidentally committed sample file is removed and ignored.

## 0.1.0 (2026-04-11)

Initial release.

- Extract 10 telemetry streams from GoPro MP4 files (ACCL, GYRO, GPS5, CORI, IORI, GRAV, SHUT, ISOE, WBAL, TMPC)
- GPS5 includes sticky GPSF (fix type) and GPSP (precision) metadata
- Dual timestamps: STMP-based (millisecond) and PTS-based (video timeline)
- `telemetrik` CLI for batch CSV extraction
- Pure Python, zero dependencies, Python 3.9+
