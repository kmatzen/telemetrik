"""
Pure-Python parser for GoPro Metadata Format (GPMF).

Extract GPS, gyroscope, accelerometer, orientation, exposure, and other
telemetry from GoPro MP4 files with no external dependencies.
"""

from gpmf_parser.parser import (
    TelemetryStream,
    extract_all_telemetry,
    get_stream_data,
)

__all__ = [
    "extract_all_telemetry",
    "get_stream_data",
    "TelemetryStream",
]
