"""
GPMF (GoPro Metadata Format) parser.

Core MP4 box walking and GPMF binary parsing adapted from gopro-video-sync
by evoth (MIT License): https://github.com/evoth/gopro-video-sync

Extended with PTS/DTS timestamps, GPS sticky metadata, unified extraction
API, and additional stream support.
"""

import struct
from dataclasses import dataclass
from math import ceil
from os.path import getsize
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple, Union


def _from_bytes(*args: Any, **kwargs: Any) -> int:
    """Big-endian int.from_bytes wrapper."""
    kwargs["byteorder"] = "big"
    return int.from_bytes(*args, **kwargs)


# ---------------------------------------------------------------------------
# MP4 box navigation
# ---------------------------------------------------------------------------


@dataclass()
class Box:
    """An MP4 box (atom)."""

    key: str
    offset: int
    size: int


def get_boxes(f: BinaryIO, offset: int, size: int, box_path: list[str]) -> list[Box]:
    """
    Walk the MP4 box hierarchy along *box_path* and return matching boxes.

    Example *box_path*: ``["moov", "trak", "mdia", "minf", "stbl"]``

    Raises:
        ValueError: If a box header is truncated, has a key that isn't valid
            ASCII, or has a size too small to contain its own header.
    """
    boxes = []
    end = offset + size
    f.seek(offset)
    while offset < end:
        header = f.read(8)
        if len(header) < 8:
            raise ValueError(f"Truncated MP4 box header at offset {offset}.")
        raw_size = _from_bytes(header[0:4])
        try:
            key = header[4:8].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Invalid MP4 box key at offset {offset}.") from exc

        header_len = 8
        if raw_size == 1:
            # size == 1 means the real (64-bit) size follows as "largesize".
            largesize_bytes = f.read(8)
            if len(largesize_bytes) < 8:
                raise ValueError(f"Truncated MP4 box largesize at offset {offset}.")
            box_size = _from_bytes(largesize_bytes)
            header_len = 16
        elif raw_size == 0:
            # size == 0 means "extends to the end of the file/parent box".
            box_size = end - offset
        else:
            box_size = raw_size

        if box_size < header_len:
            raise ValueError(f"Invalid MP4 box size {box_size} at offset {offset}.")

        if key == box_path[0]:
            if len(box_path) == 1:
                boxes += [Box(key, offset, box_size)]
            else:
                boxes += get_boxes(f, offset + header_len, box_size - header_len, box_path[1:])
        offset += box_size
        f.seek(offset)
    return boxes


# ---------------------------------------------------------------------------
# MP4 sample table
# ---------------------------------------------------------------------------


@dataclass()
class Sample:
    """A sample from the MP4 sample table, with optional PTS/DTS."""

    offset: int
    size: int
    pts: Optional[int] = None
    dts: Optional[int] = None


def get_samples(f: BinaryIO, stbl: Box) -> list[Sample]:
    """Parse the sample table (stbl) and return :class:`Sample` objects with PTS/DTS."""
    # Sample sizes (stsz)
    sample_sizes = []
    stsz = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stsz"])[0]
    f.seek(stsz.offset + 12)
    sample_size = _from_bytes(f.read(4))
    num_entries = _from_bytes(f.read(4))
    if sample_size != 0:
        sample_sizes = [sample_size] * num_entries
    else:
        for _ in range(num_entries):
            sample_sizes.append(_from_bytes(f.read(4)))

    # Sample offsets: 32-bit (stco), or 64-bit (co64) for files with sample data
    # beyond the 4 GB mark.
    sample_offsets = []
    stco_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stco"])
    co64_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "co64"])
    if stco_boxes:
        stco = stco_boxes[0]
        f.seek(stco.offset + 12)
        num_entries = _from_bytes(f.read(4))
        for _ in range(num_entries):
            sample_offsets.append(_from_bytes(f.read(4)))
    elif co64_boxes:
        co64 = co64_boxes[0]
        f.seek(co64.offset + 12)
        num_entries = _from_bytes(f.read(4))
        for _ in range(num_entries):
            sample_offsets.append(_from_bytes(f.read(8)))
    else:
        raise ValueError("Sample table has no chunk offset box (stco/co64).")

    # Sample durations (stts) → used to compute DTS
    sample_durations = []
    stts_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stts"])
    if stts_boxes:
        stts = stts_boxes[0]
        f.seek(stts.offset + 12)
        num_entries = _from_bytes(f.read(4))
        for _ in range(num_entries):
            sample_count = _from_bytes(f.read(4))
            sample_delta = _from_bytes(f.read(4))
            for _ in range(sample_count):
                sample_durations.append(sample_delta)

    # Composition time offsets (ctts) → PTS = DTS + offset
    composition_offsets = [0] * len(sample_sizes)
    ctts_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "ctts"])
    if ctts_boxes:
        ctts = ctts_boxes[0]
        f.seek(ctts.offset + 12)
        num_entries = _from_bytes(f.read(4))
        offset_index = 0
        for _ in range(num_entries):
            sample_count = _from_bytes(f.read(4))
            sample_offset = _from_bytes(f.read(4))
            for _ in range(sample_count):
                if offset_index < len(composition_offsets):
                    composition_offsets[offset_index] = sample_offset
                    offset_index += 1

    samples = []
    current_dts = 0
    for i in range(len(sample_sizes)):
        dts = current_dts
        pts = dts + composition_offsets[i]
        samples.append(Sample(sample_offsets[i], sample_sizes[i], pts=pts, dts=dts))
        if i < len(sample_durations):
            current_dts += sample_durations[i]

    return samples


# ---------------------------------------------------------------------------
# GPMF binary format
# ---------------------------------------------------------------------------


@dataclass()
class GPMFBox:
    """
    A GPMF KLV element.

    See: https://github.com/gopro/gpmf-parser#definitions
    """

    key: str
    offset: int
    size: int
    type: Optional[str]
    struct_size: int
    repeat: int


def get_gpmf_boxes(f: BinaryIO, offset: int, size: int, box_path: list[str]) -> list[GPMFBox]:
    """
    Walk the GPMF KLV hierarchy and return matching elements.

    Raises:
        ValueError: If a KLV header is truncated or has a key/type that isn't
            valid ASCII.
    """
    boxes = []
    end = offset + size
    f.seek(offset)
    while offset < end:
        header = f.read(8)
        if len(header) < 8:
            raise ValueError(f"Truncated GPMF KLV header at offset {offset}.")
        try:
            key = header[0:4].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Invalid GPMF KLV key at offset {offset}.") from exc
        if _from_bytes(header[4:5]) == 0:
            type = None
        else:
            try:
                type = header[4:5].decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Invalid GPMF KLV type at offset {offset}.") from exc
        struct_size = _from_bytes(header[5:6])
        repeat = _from_bytes(header[6:8])
        # 32-bit aligned
        size = ceil((struct_size * repeat + 8) / 4) * 4
        if key == box_path[0]:
            if len(box_path) == 1:
                boxes += [GPMFBox(key, offset, size, type, struct_size, repeat)]
            elif type is None:
                boxes += get_gpmf_boxes(f, offset + 8, size - 8, box_path[1:])
        offset += size
        f.seek(offset)
    return boxes


#: Either a single divisor applied to every channel, or one divisor per channel
#: (as GPMF SCAL provides for e.g. GPS5's 5 distinct units).
ScaleDivisor = Union[float, tuple[float, ...]]


def _divisor_at(scale_divisor: ScaleDivisor, index: int) -> float:
    """Look up the divisor for channel *index*, broadcasting a scalar/single-value SCAL."""
    if isinstance(scale_divisor, tuple):
        return scale_divisor[index] if len(scale_divisor) > 1 else scale_divisor[0]
    return scale_divisor


def _parse_gpmf_value(
    f: BinaryIO, type_char: Optional[str], struct_size: int, scale_divisor: ScaleDivisor
) -> Any:
    """Parse a single GPMF value based on its type character."""
    if type_char is None:
        return None

    # Signed 16-bit integer
    if type_char == "s":
        if struct_size == 2:
            return _from_bytes(f.read(2), signed=True) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(2), signed=True) / _divisor_at(scale_divisor, i)
                for i in range(struct_size // 2)
            )

    # Signed 32-bit integer
    elif type_char == "l":
        if struct_size == 4:
            return _from_bytes(f.read(4), signed=True) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(4), signed=True) / _divisor_at(scale_divisor, i)
                for i in range(struct_size // 4)
            )

    # Unsigned 16-bit integer
    elif type_char == "S":
        if struct_size == 2:
            return _from_bytes(f.read(2)) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(2)) / _divisor_at(scale_divisor, i)
                for i in range(struct_size // 2)
            )

    # Unsigned 32-bit integer
    elif type_char == "L":
        if struct_size == 4:
            return _from_bytes(f.read(4)) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(4)) / _divisor_at(scale_divisor, i)
                for i in range(struct_size // 4)
            )

    # Float (32-bit)
    elif type_char == "f":
        if struct_size == 4:
            return struct.unpack(">f", f.read(4))[0]
        else:
            return tuple(struct.unpack(">f", f.read(4))[0] for _ in range(struct_size // 4))

    # Double (64-bit float)
    elif type_char == "d":
        if struct_size == 8:
            return struct.unpack(">d", f.read(8))[0]
        else:
            return tuple(struct.unpack(">d", f.read(8))[0] for _ in range(struct_size // 8))

    # Signed 64-bit integer
    elif type_char == "j":
        if struct_size == 8:
            return _from_bytes(f.read(8), signed=True) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(8), signed=True) / _divisor_at(scale_divisor, i)
                for i in range(struct_size // 8)
            )

    # Unsigned 64-bit integer
    elif type_char == "J":
        if struct_size == 8:
            return _from_bytes(f.read(8)) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(8)) / _divisor_at(scale_divisor, i)
                for i in range(struct_size // 8)
            )

    # Signed byte
    elif type_char == "b":
        if struct_size == 1:
            return _from_bytes(f.read(1), signed=True) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(1), signed=True) / _divisor_at(scale_divisor, i)
                for i in range(struct_size)
            )

    # Unsigned byte
    elif type_char == "B":
        if struct_size == 1:
            return _from_bytes(f.read(1)) / _divisor_at(scale_divisor, 0)
        else:
            return tuple(
                _from_bytes(f.read(1)) / _divisor_at(scale_divisor, i) for i in range(struct_size)
            )

    # String/character
    elif type_char == "c":
        return f.read(struct_size).decode("ascii").rstrip("\x00")

    # FourCC code
    elif type_char == "F":
        return f.read(struct_size).decode("ascii")

    # UTC date/time string ("YYMMDDhhmmss.sss")
    elif type_char == "U":
        return f.read(struct_size).decode("ascii").rstrip("\x00")

    # Q15.16 fixed-point number
    elif type_char == "q":
        if struct_size == 4:
            return _from_bytes(f.read(4), signed=True) / 65536.0
        else:
            return tuple(
                _from_bytes(f.read(4), signed=True) / 65536.0 for _ in range(struct_size // 4)
            )

    # Default: raw bytes
    else:
        return f.read(struct_size)


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass()
class TelemetryStream:
    """A single telemetry stream extracted from a GoPro video.

    Attributes:
        key: GPMF FourCC (e.g. ``"ACCL"``, ``"GPS5"``).
        sample_count: Total number of readings.
        name: Human-readable name from STNM tag.
        units: SI units string from SIUN tag.
        data: List of ``(timestamp_ms, value)`` tuples using STMP timing.
        pts_data: Optional list of ``(pts_seconds, value)`` tuples mapped to
            the video presentation timeline.
    """

    key: str
    sample_count: int
    name: str
    units: str
    data: list[tuple[int, Any]]
    pts_data: Optional[list[tuple[float, Any]]] = None


# ---------------------------------------------------------------------------
# Stream extraction
# ---------------------------------------------------------------------------


#: One matched STRM payload: the stream's own box, its parent STRM box, the
#: SCAL divisor(s) in effect, and the current value of each requested
#: "sticky" metadata box (e.g. GPS5's GPSF/GPSP), in the order requested.
_PayloadInfo = Tuple[GPMFBox, GPMFBox, ScaleDivisor, Tuple[int, ...]]


def _collect_stream_payloads(
    f: BinaryIO,
    samples: list[Sample],
    stream_key: str,
    scale: bool,
    sticky_keys: Tuple[Tuple[str, int], ...] = (),
) -> Tuple[List[float], List[int], List[_PayloadInfo]]:
    """
    Walk each sample's ``DEVC -> STRM`` tree for one matching *stream_key*,
    collecting its STMP timestamp and SCAL divisor(s).

    *sticky_keys* is a list of ``(fourcc, default)`` pairs for metadata boxes
    that persist across samples once first seen (GPS5's GPSF/GPSP); their
    current values are carried forward from the closest earlier sample that
    had them.
    """
    stmp_timestamps = []
    sample_pts_list = []
    payload_info = []
    sticky_values = dict(sticky_keys)

    for sample in samples:
        strm = None
        strm_boxes = get_gpmf_boxes(f, sample.offset, sample.size, ["DEVC", "STRM"])
        for box in strm_boxes:
            if get_gpmf_boxes(f, box.offset, box.size, ["STRM", stream_key]):
                strm = box
                break
        if strm is None:
            continue

        stmp_boxes = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", "STMP"])
        if not stmp_boxes:
            continue
        stmp = stmp_boxes[0]
        f.seek(stmp.offset + 8)
        stmp_us = _from_bytes(f.read(stmp.struct_size * stmp.repeat))
        stmp_ms = stmp_us / 1000

        scale_divisor: ScaleDivisor = 1.0
        if scale:
            scal_boxes = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", "SCAL"])
            if scal_boxes:
                scal = scal_boxes[0]
                f.seek(scal.offset + 8)
                if scal.struct_size in (2, 4):
                    scale_divisor = tuple(
                        _from_bytes(f.read(scal.struct_size), signed=True)
                        for _ in range(scal.repeat)
                    )

        for key, _default in sticky_keys:
            key_boxes = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", key])
            if key_boxes:
                key_box = key_boxes[0]
                f.seek(key_box.offset + 8)
                sticky_values[key] = _from_bytes(f.read(key_box.struct_size))

        stream_box = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", stream_key])[0]

        stmp_timestamps.append(stmp_ms)
        sample_pts_list.append(sample.pts if sample.pts is not None else 0)
        payload_info.append(
            (stream_box, strm, scale_divisor, tuple(sticky_values[key] for key, _ in sticky_keys))
        )

    return stmp_timestamps, sample_pts_list, payload_info


def _decode_stream_values(
    f: BinaryIO,
    payload_info: List[_PayloadInfo],
    stmp_timestamps: List[float],
    sample_pts_list: List[int],
    time_base: Optional[Tuple[int, int]],
    transform: Optional[Callable[[Any, Tuple[int, ...]], Optional[Any]]] = None,
) -> Tuple[List[Tuple[int, Any]], List[Tuple[float, Any]], int, str, str]:
    """
    Decode each payload's raw values with interpolated STMP/PTS timestamps,
    collecting the stream's name (STNM) and units (SIUN) along the way.

    *transform* post-processes each raw decoded value together with that
    payload's sticky metadata tuple; returning ``None`` skips the reading
    (used by GPS5 to discard incomplete tuples).

    Falls back to the previous payload's duration for the final payload (no
    "next" timestamp to interpolate against), and to 0.0 when there is only
    ever a single payload.
    """
    data = []
    pts_data = []
    sample_count = 0
    name = ""
    units = ""

    reading_duration_ms = 0.0
    reading_duration_pts = 0.0

    for payload_idx, (stream_box, strm, scale_divisor, sticky) in enumerate(payload_info):
        f.seek(stream_box.offset + 8)

        base_timestamp_ms = stmp_timestamps[payload_idx]
        base_pts = sample_pts_list[payload_idx]

        if payload_idx + 1 < len(stmp_timestamps):
            next_stmp_ms = stmp_timestamps[payload_idx + 1]
            delta_stmp_ms = next_stmp_ms - base_timestamp_ms
            reading_duration_ms = delta_stmp_ms / stream_box.repeat

            if time_base is not None:
                next_pts = sample_pts_list[payload_idx + 1]
                delta_pts = next_pts - base_pts
                reading_duration_pts = delta_pts / stream_box.repeat

        for i in range(stream_box.repeat):
            timestamp_ms = base_timestamp_ms + (i * reading_duration_ms)

            raw_value = _parse_gpmf_value(f, stream_box.type, stream_box.struct_size, scale_divisor)
            value = transform(raw_value, sticky) if transform is not None else raw_value
            if value is None:
                continue
            data.append((int(timestamp_ms), value))

            if time_base is not None:
                pts_value = base_pts + (i * reading_duration_pts)
                pts_seconds = pts_value * time_base[0] / time_base[1]
                pts_data.append((pts_seconds, value))

            sample_count += 1

        if name == "":
            stnm_boxes = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", "STNM"])
            if stnm_boxes:
                stnm = stnm_boxes[0]
                f.seek(stnm.offset + 8)
                name = f.read(stnm.struct_size * stnm.repeat).decode("ascii").rstrip("\x00")

        if units == "":
            siun_boxes = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", "SIUN"])
            if siun_boxes:
                siun = siun_boxes[0]
                f.seek(siun.offset + 8)
                for _ in range(siun.struct_size * siun.repeat):
                    unit_char = f.read(1)
                    if len(unit_char) == 0:
                        break
                    unit_char_value = _from_bytes(unit_char)
                    if unit_char_value == 0xB0:
                        units += "\u00b0"
                    elif unit_char_value == 0xB2:
                        units += "\u00b2"
                    elif unit_char_value == 0xB3:
                        units += "\u00b3"
                    elif unit_char_value == 0xB5:
                        units += "\u00b5"
                    elif unit_char_value == 0x00:
                        break
                    else:
                        units += unit_char.decode("ascii")

    return data, pts_data, sample_count, name, units


def get_stream_data(
    f: BinaryIO,
    samples: List[Sample],
    stream_key: str,
    scale: bool = True,
    time_base: Optional[Tuple[int, int]] = None,
) -> TelemetryStream:
    """
    Extract a single GPMF stream from an MP4 file.

    Args:
        f: Binary file stream opened with ``"rb"``.
        samples: Sample table from :func:`get_samples`.
        stream_key: GPMF FourCC (e.g. ``"ACCL"``, ``"GYRO"``, ``"SHUT"``).
        scale: Apply the SCAL divisor to raw values.
        time_base: ``(numerator, denominator)`` for converting PTS to seconds.

    Returns:
        A :class:`TelemetryStream` with STMP-based and optional PTS-based
        timestamps.
    """
    stmp_timestamps, sample_pts_list, payload_info = _collect_stream_payloads(
        f, samples, stream_key, scale
    )
    data, pts_data, sample_count, name, units = _decode_stream_values(
        f, payload_info, stmp_timestamps, sample_pts_list, time_base
    )

    return TelemetryStream(
        stream_key, sample_count, name, units, data, pts_data=pts_data if pts_data else None
    )


def _get_gps_data_with_sticky_metadata(
    f: BinaryIO, samples: list[Sample], time_base: Optional[tuple[int, int]] = None
) -> TelemetryStream:
    """Extract GPS5 with sticky GPSF (fix type) and GPSP (precision) metadata."""
    stmp_timestamps, sample_pts_list, payload_info = _collect_stream_payloads(
        f, samples, "GPS5", scale=True, sticky_keys=(("GPSF", 0), ("GPSP", 9999))
    )

    def _extend_with_gps_quality(raw_value: Any, sticky: Tuple[int, ...]) -> Optional[Any]:
        if isinstance(raw_value, tuple) and len(raw_value) == 5:
            return raw_value + sticky
        return None

    data, pts_data, sample_count, name, units = _decode_stream_values(
        f,
        payload_info,
        stmp_timestamps,
        sample_pts_list,
        time_base,
        transform=_extend_with_gps_quality,
    )

    return TelemetryStream(
        key="GPS5",
        sample_count=sample_count,
        name=name if name else "GPS (Latitude, Longitude, Altitude)",
        units=units + ",fix,dop" if units else "deg,deg,m,m/s,m/s,fix,dop",
        data=data,
        pts_data=pts_data if pts_data else None,
    )


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------

#: Default streams to extract.
DEFAULT_STREAMS = [
    "SHUT",  # Shutter speed
    "ISOE",  # ISO exposure
    "WBAL",  # White balance
    "ACCL",  # Accelerometer
    "GYRO",  # Gyroscope
    "GRAV",  # Gravity vector
    "CORI",  # Camera orientation quaternion
    "IORI",  # Image orientation quaternion
    "GPS5",  # GPS (lat, lon, alt, speed_2d, speed_3d)
    "TMPC",  # Temperature
]


def extract_all_telemetry(
    video_path: str,
    streams: Optional[list[str]] = None,
) -> dict[str, TelemetryStream]:
    """
    Extract telemetry streams from a GoPro MP4 file.

    Args:
        video_path: Path to an MP4 file containing GPMF metadata.
        streams: List of GPMF FourCCs to extract. Defaults to
            :data:`DEFAULT_STREAMS`.

    Returns:
        Dict mapping stream keys to :class:`TelemetryStream` objects.
        Only streams with at least one sample are included.

    Raises:
        ValueError: If the file does not contain a GPMF metadata track.
    """
    if streams is None:
        streams = DEFAULT_STREAMS

    telemetry_streams = {}

    with open(video_path, "rb") as f:
        size = getsize(video_path)
        stbl = None
        gpmf_mdia = None

        minf_boxes = get_boxes(f, 0, size, ["moov", "trak", "mdia", "minf"])
        for box in minf_boxes:
            if get_boxes(f, box.offset, box.size, ["minf", "gmhd", "gpmd"]):
                stbl = get_boxes(f, box.offset, box.size, ["minf", "stbl"])[0]
                mdia_boxes = get_boxes(f, 0, size, ["moov", "trak", "mdia"])
                for mdia in mdia_boxes:
                    if box.offset >= mdia.offset and box.offset < mdia.offset + mdia.size:
                        gpmf_mdia = mdia
                        break
                break
        if stbl is None:
            raise ValueError("Video does not contain GPMF data.")

        time_base = None
        if gpmf_mdia:
            mdhd_boxes = get_boxes(f, gpmf_mdia.offset, gpmf_mdia.size, ["mdia", "mdhd"])
            if mdhd_boxes:
                mdhd = mdhd_boxes[0]
                f.seek(mdhd.offset + 8)
                version = _from_bytes(f.read(1))
                f.seek(mdhd.offset + 12)
                if version == 1:
                    f.read(16)  # skip creation_time + modification_time (64-bit each)
                else:
                    f.read(8)  # skip creation_time + modification_time (32-bit each)
                timescale = _from_bytes(f.read(4))
                time_base = (1, timescale)

        samples = get_samples(f, stbl)

        for stream_key in streams:
            if stream_key == "GPS5":
                stream = _get_gps_data_with_sticky_metadata(f, samples, time_base=time_base)
            else:
                stream = get_stream_data(f, samples, stream_key, scale=True, time_base=time_base)

            if stream.sample_count > 0:
                telemetry_streams[stream_key] = stream

    return telemetry_streams
