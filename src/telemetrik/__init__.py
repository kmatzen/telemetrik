"""
telemetrik — extract sensor telemetry from GoPro video files.

Pure-Python parser for GoPro Metadata Format (GPMF). No compiled
dependencies required.

Usage::

    from telemetrik import extract_all_telemetry

    streams = extract_all_telemetry("GX010042.MP4")
    gps = streams["GPS5"]
    accel = streams["ACCL"]
"""

__version__ = "0.1.0"

from telemetrik.parser import (
    DEFAULT_STREAMS,
    TelemetryStream,
    extract_all_telemetry,
    get_stream_data,
)

__all__ = [
    "__version__",
    "DEFAULT_STREAMS",
    "extract_all_telemetry",
    "get_stream_data",
    "TelemetryStream",
]
