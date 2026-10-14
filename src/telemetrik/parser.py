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
from typing import Any, BinaryIO, Callable, Optional, Union


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
    stsz_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stsz"])
    if not stsz_boxes:
        raise ValueError("Sample table is missing the sample size box (stsz).")
    stsz = stsz_boxes[0]
    f.seek(stsz.offset + 12)
    sample_size = _from_bytes(_read(f, 4, "stsz sample_size"))
    num_entries = _from_bytes(_read(f, 4, "stsz entry count"))
    if sample_size != 0:
        sample_sizes = [sample_size] * num_entries
    else:
        for _ in range(num_entries):
            sample_sizes.append(_from_bytes(_read(f, 4, "stsz entry")))

    # Chunk offsets: 32-bit (stco), or 64-bit (co64) for files with sample data
    # beyond the 4 GB mark.
    chunk_offsets = []
    stco_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stco"])
    co64_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "co64"])
    if stco_boxes:
        stco = stco_boxes[0]
        f.seek(stco.offset + 12)
        num_entries = _from_bytes(_read(f, 4, "stco entry count"))
        for _ in range(num_entries):
            chunk_offsets.append(_from_bytes(_read(f, 4, "stco entry")))
    elif co64_boxes:
        co64 = co64_boxes[0]
        f.seek(co64.offset + 12)
        num_entries = _from_bytes(_read(f, 4, "co64 entry count"))
        for _ in range(num_entries):
            chunk_offsets.append(_from_bytes(_read(f, 8, "co64 entry")))
    else:
        raise ValueError("Sample table has no chunk offset box (stco/co64).")

    # Sample-to-chunk (stsc): maps chunks to samples. When present, a chunk may
    # hold several samples back-to-back, so per-sample offsets must be expanded
    # from the chunk offsets plus the running sample sizes. Without stsc, each
    # chunk holds exactly one sample (chunk offset == sample offset).
    sample_offsets: list[int] = []
    stsc_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stsc"])
    if stsc_boxes:
        stsc = stsc_boxes[0]
        f.seek(stsc.offset + 12)
        num_entries = _from_bytes(_read(f, 4, "stsc entry count"))
        stsc_entries = []
        for _ in range(num_entries):
            first_chunk = _from_bytes(_read(f, 4, "stsc first_chunk"))
            samples_per_chunk = _from_bytes(_read(f, 4, "stsc samples_per_chunk"))
            _read(f, 4, "stsc sample_description_index")
            stsc_entries.append((first_chunk, samples_per_chunk))
        for chunk_index, chunk_offset in enumerate(chunk_offsets):
            # stsc first_chunk is 1-based.
            chunk_number = chunk_index + 1
            samples_per_chunk = 1
            for first_chunk, spc in stsc_entries:
                if first_chunk <= chunk_number:
                    # samples_per_chunk == 0 means "variable", i.e. one sample
                    # per chunk.
                    samples_per_chunk = spc if spc != 0 else 1
                else:
                    break
            offset_in_chunk = 0
            for _ in range(samples_per_chunk):
                if len(sample_offsets) >= len(sample_sizes):
                    break
                sample_offsets.append(chunk_offset + offset_in_chunk)
                offset_in_chunk += sample_sizes[len(sample_offsets) - 1]
    else:
        sample_offsets = list(chunk_offsets)

    if len(sample_offsets) < len(sample_sizes):
        raise ValueError(
            f"Sample table has {len(sample_offsets)} sample offsets "
            f"(stco/co64 + stsc) but {len(sample_sizes)} sample sizes (stsz)."
        )

    # Sample durations (stts) → used to compute DTS. A missing stts box is
    # legal in MP4 but leaves every sample at DTS 0 (the durations table
    # stays empty below), so all DTS/PTS values will read as 0.

    sample_durations: list[int] = []
    stts_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stts"])
    if stts_boxes:
        stts = stts_boxes[0]
        f.seek(stts.offset + 12)
        num_entries = _from_bytes(f.read(4))
        # Cap the expansion at the number of samples: a corrupted or malicious
        # sample_count (the field is a 32-bit int) must not blow up memory.
        for _ in range(num_entries):
            sample_count = _from_bytes(_read(f, 4, "stts sample_count"))
            sample_delta = _from_bytes(_read(f, 4, "stts sample_delta"))
            remaining = len(sample_sizes) - len(sample_durations)
            for _ in range(min(sample_count, remaining)):
                sample_durations.append(sample_delta)
            if remaining <= 0:
                break

    # Composition time offsets (ctts) → PTS = DTS + offset
    composition_offsets = [0] * len(sample_sizes)
    ctts_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "ctts"])
    if ctts_boxes:
        ctts = ctts_boxes[0]
        f.seek(ctts.offset + 12)
        num_entries = _from_bytes(_read(f, 4, "ctts entry count"))
        offset_index = 0
        for _ in range(num_entries):
            sample_count = _from_bytes(_read(f, 4, "ctts sample_count"))
            sample_offset = _from_bytes(_read(f, 4, "ctts sample_offset"))
            for _ in range(min(sample_count, len(composition_offsets) - offset_index)):
                composition_offsets[offset_index] = sample_offset
                offset_index += 1
            if offset_index >= len(composition_offsets):
                break

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
    """Look up the divisor for channel *index*, broadcasting a scalar/single-value SCAL.

    A SCAL with fewer divisors than the stream's channels clamps to the last
    divisor instead of raising IndexError.
    """
    if isinstance(scale_divisor, tuple):
        if len(scale_divisor) == 1:
            return scale_divisor[0]
        return scale_divisor[min(index, len(scale_divisor) - 1)]
    return scale_divisor


def _scaled(raw: Union[int, float], divisor: float) -> Union[int, float]:
    """Divide *raw* by *divisor*, leaving the value unchanged when the divisor is 0."""
    if divisor == 0:
        return raw
    return raw / divisor


def _read(f: BinaryIO, n: int, context: str) -> bytes:
    """Read exactly *n* bytes, raising ValueError (not struct.error) when truncated."""
    buf = f.read(n)
    if len(buf) < n:
        raise ValueError(
            f"Truncated GPMF payload ({context}): expected {n} bytes, read {len(buf)}."
        )
    return buf


def _parse_gpmf_value(
    f: BinaryIO,
    type_char: Optional[str],
    struct_size: int,
    repeat: int,
    scale_divisor: ScaleDivisor,
) -> Any:
    """Parse a single GPMF value based on its type character.

    *struct_size* must be a multiple of the type's element size, otherwise
    per-element decoding would misalign the file cursor.
    """
    if type_char is None:
        return None

    # Element size (bytes) for the numeric types. String-like types are handled
    # below via a raw read, so they are excluded here.
    elem_size = {
        "s": 2,
        "l": 4,
        "S": 2,
        "L": 4,
        "f": 4,
        "d": 8,
        "j": 8,
        "J": 8,
        "b": 1,
        "B": 1,
        "q": 4,
    }
    if type_char in elem_size:
        if struct_size % elem_size[type_char] != 0:
            raise ValueError(
                f"GPMF type '{type_char}' has element size {elem_size[type_char]} but "
                f"struct_size {struct_size} is not a multiple of it."
            )
        n_elems = struct_size // elem_size[type_char]
        if type_char == "f":
            values = [
                _scaled(
                    struct.unpack(">f", _read(f, 4, "'f' element"))[0],
                    _divisor_at(scale_divisor, i),
                )
                for i in range(n_elems)
            ]
        elif type_char == "d":
            values = [
                _scaled(
                    struct.unpack(">d", _read(f, 8, "'d' element"))[0],
                    _divisor_at(scale_divisor, i),
                )
                for i in range(n_elems)
            ]
        elif type_char == "q":
            # Q15.16 fixed-point
            values = [
                _from_bytes(_read(f, 4, "'q' element"), signed=True) / 65536.0
                for _ in range(n_elems)
            ]
        else:
            signed = type_char in ("s", "l", "j", "b")
            if n_elems == 1:
                values = [
                    _scaled(
                        _from_bytes(
                            _read(f, elem_size[type_char], f"'{type_char}' value"), signed=signed
                        ),
                        _divisor_at(scale_divisor, 0),
                    )
                ]
            else:
                values = [
                    _scaled(
                        _from_bytes(
                            _read(f, elem_size[type_char], f"'{type_char}' element {i}"),
                            signed=signed,
                        ),
                        _divisor_at(scale_divisor, i),
                    )
                    for i in range(n_elems)
                ]
        return values[0] if n_elems == 1 else tuple(values)

    # String/character
    if type_char == "c":
        return _read(f, struct_size, "'c' string").decode("ascii", errors="replace").rstrip("\x00")

    # FourCC code
    elif type_char == "F":
        return _read(f, struct_size, "'F' FourCC").decode("ascii", errors="replace")

    # UTC date/time string ("YYMMDDhhmmss.sss")
    elif type_char == "U":
        raw = _read(f, struct_size, "'U' time string")
        return raw.decode("ascii", errors="replace").rstrip("\x00")

    # Default: raw bytes
    else:
        return _read(f, struct_size, f"'{type_char}' payload")


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
        data: List of ``(timestamp_ms, value)`` tuples using STMP timing
            (float milliseconds; fractional values are preserved).
        pts_data: Optional list of ``(pts_seconds, value)`` tuples mapped to
            the video presentation timeline.
    """

    key: str
    sample_count: int
    name: str
    units: str
    data: list[tuple[float, Any]]
    pts_data: Optional[list[tuple[float, Any]]] = None


# ---------------------------------------------------------------------------
# Stream extraction
# ---------------------------------------------------------------------------


#: One matched STRM payload: the stream's own box, its parent STRM box, the
#: SCAL divisor(s) in effect, and the current value of each requested
#: "sticky" metadata box (e.g. GPS5's GPSF/GPSP), in the order requested.
_PayloadInfo = tuple[GPMFBox, GPMFBox, ScaleDivisor, tuple[int, ...]]


def _collect_stream_payloads(
    f: BinaryIO,
    samples: list[Sample],
    stream_key: str,
    scale: bool,
    sticky_keys: tuple[tuple[str, int], ...] = (),
) -> tuple[list[float], list[int], list[_PayloadInfo]]:
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
    payload_info: list[_PayloadInfo],
    stmp_timestamps: list[float],
    sample_pts_list: list[int],
    time_base: Optional[tuple[int, int]],
    transform: Optional[Callable[[Any, tuple[int, ...]], Optional[Any]]] = None,
) -> tuple[list[tuple[float, Any]], list[tuple[float, Any]], int, str, str]:
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

        # A payload with repeat == 0 carries no readings; skipping it avoids a
        # division by zero when interpolating timestamps.
        if stream_box.repeat > 0:
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

                raw_value = _parse_gpmf_value(
                    f, stream_box.type, stream_box.struct_size, stream_box.repeat, scale_divisor
                )
                value = transform(raw_value, sticky) if transform is not None else raw_value
                if value is None:
                    continue
                data.append((timestamp_ms, value))

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
                name = (
                    f.read(stnm.struct_size * stnm.repeat)
                    .decode("ascii", errors="replace")
                    .rstrip("\x00")
                )

        if units == "":
            siun_boxes = get_gpmf_boxes(f, strm.offset, strm.size, ["STRM", "SIUN"])
            if siun_boxes:
                siun = siun_boxes[0]
                f.seek(siun.offset + 8)
                # Each repeat is one unit string; decode per entry (a NUL pad
                # byte ends only its own entry) and join with commas.
                parts = []
                for _ in range(siun.repeat):
                    part = ""
                    for unit_byte in _read(f, siun.struct_size, "SIUN entry"):
                        if unit_byte == 0xB0:
                            part += "\u00b0"
                        elif unit_byte == 0xB2:
                            part += "\u00b2"
                        elif unit_byte == 0xB3:
                            part += "\u00b3"
                        elif unit_byte == 0xB5:
                            part += "\u00b5"
                        elif unit_byte == 0x00:
                            break
                        else:
                            part += bytes([unit_byte]).decode("ascii", errors="replace")
                    if part:
                        parts.append(part)
                units = ",".join(parts)

    return data, pts_data, sample_count, name, units


def get_stream_data(
    f: BinaryIO,
    samples: list[Sample],
    stream_key: str,
    scale: bool = True,
    time_base: Optional[tuple[int, int]] = None,
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

    def _extend_with_gps_quality(raw_value: Any, sticky: tuple[int, ...]) -> Optional[Any]:
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
                stbl_boxes = get_boxes(f, box.offset, box.size, ["minf", "stbl"])
                if not stbl_boxes:
                    continue  # malformed GPMF track; try the next candidate
                stbl = stbl_boxes[0]
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
                timescale = _from_bytes(_read(f, 4, "mdhd timescale"))
                if timescale == 0:
                    raise ValueError("GPMF track has a zero mdhd timescale.")
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
