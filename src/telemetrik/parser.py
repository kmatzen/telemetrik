"""
GPMF (GoPro Metadata Format) parser.

Core MP4 box walking and GPMF binary parsing adapted from gopro-video-sync
by evoth (MIT License): https://github.com/evoth/gopro-video-sync

Extended with PTS/DTS timestamps, GPS sticky metadata, unified extraction
API, and additional stream support.
"""

import os
import struct
from dataclasses import dataclass, field
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
                boxes += [Box(offset, box_size)]
            else:
                boxes += get_boxes(f, offset + header_len, box_size - header_len, box_path[1:])
        offset += box_size
        f.seek(offset)
    return boxes


def _entry_count(f: BinaryIO, box: Box, context: str, entry_size: int) -> int:
    """Read a full-box entry count at the cursor, bounded by the box's own size.

    Entry counts are raw 32-bit ints taken straight from the file (legal up to
    4,294,967,295). Checking the count against the bytes the box actually has
    left, *before* it drives a loop, is what keeps a forged count from costing
    gigabytes of memory.
    """
    num_entries = _from_bytes(_read(f, 4, context))
    available = max(box.offset + box.size - f.tell(), 0)
    if num_entries > available // entry_size:
        raise ValueError(
            f"{context} declares {num_entries} entries of {entry_size} bytes, "
            f"but its box leaves {available}."
        )
    return num_entries


# ---------------------------------------------------------------------------
# MP4 sample table
# ---------------------------------------------------------------------------


@dataclass()
class Sample:
    """A sample from the MP4 sample table, with optional PTS."""

    offset: int
    size: int
    pts: Optional[int] = None


def get_samples(f: BinaryIO, stbl: Box) -> list[Sample]:
    """Parse the sample table (stbl) and return :class:`Sample` objects with PTS."""
    # Sample sizes (stsz)
    sample_sizes = []
    stsz_boxes = get_boxes(f, stbl.offset, stbl.size, box_path=["stbl", "stsz"])
    if not stsz_boxes:
        raise ValueError("Sample table is missing the sample size box (stsz).")
    stsz = stsz_boxes[0]
    f.seek(stsz.offset + 12)
    sample_size = _from_bytes(_read(f, 4, "stsz sample_size"))
    if sample_size != 0:
        # Uniform sizes: no per-entry table follows, so the box offers nothing to
        # measure against. The file itself is the bound — every sample has to
        # occupy at least one byte of media data. The bound caps the *count*, not
        # the cost: a file that respects it still allocates one Sample per declared
        # entry, measured at roughly 175 bytes of RAM per declared sample, so a
        # ~100 MB file that stays inside the bound reaches OOM (uncatchable) before
        # any content is validated. See the README's 270x note; `max_readings`
        # cannot help, because the sample table is built before it is consulted.
        num_entries = _from_bytes(_read(f, 4, "stsz entry count"))
        file_size = f.seek(0, os.SEEK_END)
        if num_entries > file_size:
            raise ValueError(
                f"stsz declares {num_entries} samples but the file holds {file_size} bytes."
            )
        sample_sizes = [sample_size] * num_entries
    else:
        num_entries = _entry_count(f, stsz, "stsz entry count", 4)
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
        num_entries = _entry_count(f, stco, "stco entry count", 4)
        for _ in range(num_entries):
            chunk_offsets.append(_from_bytes(_read(f, 4, "stco entry")))
    elif co64_boxes:
        co64 = co64_boxes[0]
        f.seek(co64.offset + 12)
        num_entries = _entry_count(f, co64, "co64 entry count", 8)
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
        num_entries = _entry_count(f, stsc, "stsc entry count", 12)
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
        num_entries = _entry_count(f, stts, "stts entry count", 8)
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
        num_entries = _entry_count(f, ctts, "ctts entry count", 8)
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
        samples.append(Sample(sample_offsets[i], sample_sizes[i], pts=pts))
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


def _gpmf_children(f: BinaryIO, offset: int, size: int) -> list[GPMFBox]:
    """Walk the direct KLV children of the range ``[offset, offset + size)``.

    Raises:
        ValueError: If a KLV header is truncated, has a key/type that isn't
            valid ASCII, or declares a payload running past the end of the box
            being walked.
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
            type_char = None
        else:
            try:
                type_char = header[4:5].decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Invalid GPMF KLV type at offset {offset}.") from exc
        struct_size = _from_bytes(header[5:6])
        repeat = _from_bytes(header[6:8])
        # 32-bit aligned: header (8) + payload, padded up to a multiple of 4.
        box_size = ((struct_size * repeat + 8 + 3) // 4) * 4
        # The declared repeat is a raw 16-bit int from the file; a payload that
        # runs past this box would read (and invent) the following boxes' bytes.
        #
        # What this guard cannot see: a payload that stays inside the parent but
        # overlaps its *following sibling*. The declared tiling stays
        # self-consistent there (the boxes still tile the parent exactly, and the
        # values read out of the neighbour's header are legal ints sitting on
        # bytes legally inside the parent), so no size-vs-parent check can tell it
        # apart — spotting it needs knowledge of what the payload must contain.
        # Verified: a SCAL header laid over the following STMP header yields its
        # divisors read out of STMP's bytes, and a GPSF header over a GPS5 header
        # makes the whole GPS5 stream vanish silently. Known, accepted limit.
        if box_size > end - offset:
            raise ValueError(
                f"GPMF KLV '{key}' at offset {offset} declares {box_size} bytes, "
                f"but only {end - offset} remain in its parent box."
            )
        boxes.append(GPMFBox(key, offset, box_size, type_char, struct_size, repeat))
        offset += box_size
        f.seek(offset)
    return boxes


def get_gpmf_boxes(f: BinaryIO, offset: int, size: int, box_path: list[str]) -> list[GPMFBox]:
    """
    Walk the GPMF KLV hierarchy and return matching elements.

    Raises:
        ValueError: see :func:`_gpmf_children`.
    """
    boxes = []
    for box in _gpmf_children(f, offset, size):
        if box.key != box_path[0]:
            continue
        if len(box_path) == 1:
            boxes.append(box)
        elif box.type is None:
            boxes += get_gpmf_boxes(f, box.offset + 8, box.size - 8, box_path[1:])
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


@dataclass()
class _PayloadInfo:
    """One matched STRM payload: its box, the SCAL divisor(s) in effect, the current
    value of each requested sticky box (GPS5's GPSF/GPSP, in request order), and the
    sample's STMP/PTS timing.

    The timestamps ride *with* their payload: in parallel lockstep lists, one stray
    ``continue`` above a single append silently shifts every later timestamp of that
    stream, with nothing to catch it.
    """

    stream_box: GPMFBox
    scale_divisor: ScaleDivisor
    sticky: tuple[int, ...]  # one value per requested sticky key
    stmp_ms: float
    pts: int


@dataclass()
class _StreamCollection:
    """Everything one pass over the samples gathered for a single stream key."""

    payloads: list[_PayloadInfo] = field(default_factory=list)
    #: Decoded STNM / SIUN strings of the first payload that carried a real one.
    name: Optional[str] = None
    units: Optional[str] = None


def _decode_name(f: BinaryIO, box: Optional[GPMFBox]) -> Optional[str]:
    """Decode an STNM box, or None when it is absent or only padding.

    An empty name box (the NUL-padded STNM a writer emits for a stream that has
    no name yet) must not count as *the* name: the first payload carrying a real
    one wins, so a blank first sample cannot shadow the name of a later one.
    """
    if box is None:
        return None
    f.seek(box.offset + 8)
    name = (
        _read(f, box.struct_size * box.repeat, "STNM name")
        .decode("ascii", errors="replace")
        .rstrip("\x00")
    )
    return name or None


def _decode_units(f: BinaryIO, box: Optional[GPMFBox]) -> Optional[str]:
    """Decode an SIUN box into a comma-joined unit string, or None when it is
    absent or only padding (see :func:`_decode_name` for why empty does not count).

    Each repeat is one unit string; a NUL pad byte ends only its own entry.
    """
    if box is None:
        return None
    f.seek(box.offset + 8)
    parts = []
    for _ in range(box.repeat):
        part = ""
        for unit_byte in _read(f, box.struct_size, "SIUN entry"):
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
    return ",".join(parts) or None


#: Sticky metadata for GPS5: fix type and precision, carried forward per
#: stream from the closest earlier sample that had them.
_GPS5_STICKY_KEYS: tuple[tuple[str, int], ...] = (("GPSF", 0), ("GPSP", 9999))


def _collect_all_stream_payloads(
    f: BinaryIO,
    samples: list[Sample],
    stream_keys: list[str],
    scale: bool,
    sticky_keys_map: Optional[dict[str, tuple[tuple[str, int], ...]]] = None,
) -> dict[str, _StreamCollection]:
    """
    ONE pass over *samples* collecting the payloads of every key in
    *stream_keys* from the same walk, so extra requested streams cost no
    extra walks over the file.

    Each sample's ``DEVC -> STRM`` tree is walked once and each STRM subtree
    is walked once, capturing its direct children; STMP, SCAL, the sticky
    metadata boxes, STNM and SIUN are then read from the captured children
    instead of re-walking the subtree per stream.

    The walk is anchored on bytes that exist, not on the lengths the file
    declares for itself (see the clamp inside the loop).

    *sticky_keys_map* maps a stream key to a list of ``(fourcc, default)``
    pairs for metadata boxes that persist across samples once first seen
    (GPS5's GPSF/GPSP); their current values are carried forward per stream
    from the closest earlier sample that had them.
    """
    sticky_keys_map = sticky_keys_map or {}
    collections = {key: _StreamCollection() for key in dict.fromkeys(stream_keys)}
    sticky_values = {key: dict(sticky_keys) for key, sticky_keys in sticky_keys_map.items()}
    file_size = f.seek(0, os.SEEK_END)

    for index, sample in enumerate(samples):
        # Every length that anchors the walk below — the sample's own stsz size and
        # the DEVC/STRM sizes inside it — is declared by the file. Clamp the walk to
        # bytes that really exist: a sample is never read past the end of the file,
        # nor past the start of its successor. Without this, a sample that declares
        # 300 readings and carries 10 of them happily invents 290 more out of the
        # neighbouring sample's data, and every parent-size guard in the walker
        # passes, because each parent declared the room too.
        room = file_size - sample.offset
        if index + 1 < len(samples):
            room = min(room, samples[index + 1].offset - sample.offset)
        sample_end = sample.offset + max(min(sample.size, room), 0)

        matched_this_sample: set[str] = set()
        for strm in get_gpmf_boxes(f, sample.offset, sample_end - sample.offset, ["DEVC", "STRM"]):
            if strm.type is not None:
                continue  # a typed STRM is not a container; matching skips it
            # The same clamp one level down: DEVC's declared size may reach into the
            # next sample, so the STRM payload is bounded by the sample, not by its
            # own declaration.
            strm_payload_size = max(min(strm.size, sample_end - strm.offset) - 8, 0)
            children = _gpmf_children(f, strm.offset + 8, strm_payload_size)
            first_child: dict[str, GPMFBox] = {}
            for child in children:
                first_child.setdefault(child.key, child)
            # Over the deduped collection keys, not the raw request list: a key
            # requested twice would append this payload to that stream twice.
            keys_here = [
                key for key in collections if key not in matched_this_sample and key in first_child
            ]
            if not keys_here:
                continue
            matched_this_sample.update(keys_here)

            stmp_box = first_child.get("STMP")
            if stmp_box is None:
                continue
            f.seek(stmp_box.offset + 8)
            stmp_us = _from_bytes(
                _read(f, stmp_box.struct_size * stmp_box.repeat, "STMP timestamp")
            )
            stmp_ms = stmp_us / 1000

            scale_divisor: ScaleDivisor = 1.0
            scal_box = first_child.get("SCAL") if scale else None
            if scal_box is not None:
                # An empty divisor tuple would make every _divisor_at() lookup
                # index out of range; a SCAL with no divisors carries no scale.
                if scal_box.repeat == 0:
                    raise ValueError(f"GPMF SCAL at offset {scal_box.offset} has repeat == 0.")
                f.seek(scal_box.offset + 8)
                if scal_box.struct_size in (2, 4):
                    scale_divisor = tuple(
                        _from_bytes(_read(f, scal_box.struct_size, "SCAL divisor"), signed=True)
                        for _ in range(scal_box.repeat)
                    )

            pts = sample.pts if sample.pts is not None else 0
            for key in keys_here:
                col = collections[key]
                sticky_state = sticky_values.get(key)
                if sticky_state is not None:
                    for sticky_key, _default in sticky_keys_map[key]:
                        box = first_child.get(sticky_key)
                        if box is not None:
                            f.seek(box.offset + 8)
                            sticky_state[sticky_key] = _from_bytes(
                                _read(f, box.struct_size, sticky_key)
                            )
                if col.name is None:
                    col.name = _decode_name(f, first_child.get("STNM"))
                if col.units is None:
                    col.units = _decode_units(f, first_child.get("SIUN"))
                col.payloads.append(
                    _PayloadInfo(
                        first_child[key],
                        scale_divisor,
                        tuple(sticky_state[k] for k, _ in sticky_keys_map[key])
                        if sticky_state is not None
                        else (),
                        stmp_ms,
                        pts,
                    )
                )

    return collections


def _decode_stream_values(
    f: BinaryIO,
    col: _StreamCollection,
    time_base: Optional[tuple[int, int]],
    transform: Optional[Callable[[Any, tuple[int, ...]], Optional[Any]]] = None,
    max_readings: Optional[int] = None,
) -> tuple[list[tuple[float, Any]], list[tuple[float, Any]], int]:
    """
    Decode each payload's raw values with interpolated STMP/PTS timestamps. The
    stream's name (STNM) and units (SIUN) ride on *col*, decoded during collection.

    *transform* post-processes each raw decoded value together with that
    payload's sticky metadata tuple; returning ``None`` skips the reading
    (used by GPS5 to discard incomplete tuples).

    *max_readings* caps how many readings this stream decodes. The check runs
    inside the decode loop, so the cap bounds the memory the decode peaks at and
    not only what it hands back; it raises as soon as the count passes the cap.

    Falls back to the previous payload's duration for the final payload (no
    "next" timestamp to interpolate against), and to 0.0 when there is only
    ever a single payload.
    """
    data = []
    pts_data = []
    sample_count = 0

    reading_duration_ms = 0.0
    reading_duration_pts = 0.0

    payloads = col.payloads
    for payload_idx, info in enumerate(payloads):
        stream_box = info.stream_box
        f.seek(stream_box.offset + 8)

        base_timestamp_ms = info.stmp_ms
        base_pts = info.pts

        # A payload with repeat == 0 carries no readings; skipping it avoids a
        # division by zero when interpolating timestamps.
        if stream_box.repeat > 0:
            if payload_idx + 1 < len(payloads):
                next_stmp_ms = payloads[payload_idx + 1].stmp_ms
                delta_stmp_ms = next_stmp_ms - base_timestamp_ms
                reading_duration_ms = delta_stmp_ms / stream_box.repeat

                if time_base is not None:
                    next_pts = payloads[payload_idx + 1].pts
                    delta_pts = next_pts - base_pts
                    reading_duration_pts = delta_pts / stream_box.repeat

            for i in range(stream_box.repeat):
                timestamp_ms = base_timestamp_ms + (i * reading_duration_ms)

                raw_value = _parse_gpmf_value(
                    f,
                    stream_box.type,
                    stream_box.struct_size,
                    stream_box.repeat,
                    info.scale_divisor,
                )
                value = transform(raw_value, info.sticky) if transform is not None else raw_value
                if value is None:
                    continue
                data.append((timestamp_ms, value))

                if time_base is not None:
                    pts_value = base_pts + (i * reading_duration_pts)
                    pts_seconds = pts_value * time_base[0] / time_base[1]
                    pts_data.append((pts_seconds, value))

                sample_count += 1
                if max_readings is not None and sample_count > max_readings:
                    raise ValueError(
                        f"Telemetry extraction exceeds max_readings={max_readings} "
                        f"({sample_count} readings so far)."
                    )

    return data, pts_data, sample_count


def _stream_from_collection(
    f: BinaryIO,
    stream_key: str,
    col: _StreamCollection,
    time_base: Optional[tuple[int, int]],
    max_readings: Optional[int] = None,
    transform: Optional[Callable[[Any, tuple[int, ...]], Optional[Any]]] = None,
) -> TelemetryStream:
    """The single place a :class:`TelemetryStream` is assembled, for every key. GPS5
    is the one key with a per-reading *transform* (it carries its sticky fix/precision
    along, dropping incomplete tuples) and with defaults for name and units.
    """
    data, pts_data, sample_count = _decode_stream_values(
        f, col, time_base, transform=transform, max_readings=max_readings
    )
    name = col.name or ""
    units = col.units or ""
    if stream_key == "GPS5":
        # GPS5's 7 channels: lat, lon, alt, speed_2d, speed_3d, plus the sticky fix/dop.
        name = name or "GPS (Latitude, Longitude, Altitude)"
        units = units + ",fix,dop" if units else "deg,deg,m,m/s,m/s,fix,dop"

    return TelemetryStream(
        stream_key, sample_count, name, units, data, pts_data=pts_data if pts_data else None
    )


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
    col = _collect_all_stream_payloads(f, samples, [stream_key], scale)[stream_key]
    return _stream_from_collection(f, stream_key, col, time_base)


def _extend_with_gps_quality(raw_value: Any, sticky: tuple[int, ...]) -> Optional[Any]:
    """GPS5 readings carry their sticky fix/precision along; drop incomplete tuples."""
    if isinstance(raw_value, tuple) and len(raw_value) == 5:
        return raw_value + sticky
    return None


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
    max_readings: Optional[int] = None,
) -> dict[str, TelemetryStream]:
    """
    Extract telemetry streams from a GoPro MP4 file.

    Args:
        video_path: Path to an MP4 file containing GPMF metadata.
        streams: Iterable of GPMF FourCCs to extract (iterated once, so a
            generator is fine). Defaults to :data:`DEFAULT_STREAMS`.
        max_readings: Optional cap on the total number of readings extracted
            across all streams. Extraction of a metadata track costs roughly
            two orders of magnitude more RAM than the file's size (see the
            README), so a cap bounds the damage from oversized or hostile
            tracks. The cap is enforced inside the decode loop, so it bounds
            the peak memory of the decode and not just the returned dict.
            ``None`` (default) means unlimited.

    Returns:
        Dict mapping stream keys to :class:`TelemetryStream` objects.
        Only streams with at least one sample are included.

    Raises:
        ValueError: If the file does not contain a GPMF metadata track, or
            if the extraction exceeds *max_readings*.
    """
    # Normalised once: the collection pass and the decode loop below both
    # iterate it, so a one-shot iterator (a generator) must not be consumed
    # by the first pass and silently yield nothing to the second.
    streams = list(DEFAULT_STREAMS if streams is None else streams)

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

        # One pass over the samples collects every requested stream's payloads
        # from the same walk; decoding per key afterwards adds no file walks.
        collections = _collect_all_stream_payloads(
            f,
            samples,
            streams,
            scale=True,
            sticky_keys_map={"GPS5": _GPS5_STICKY_KEYS},
        )

        total_readings = 0
        for stream_key in streams:
            col = collections[stream_key]
            # Hand the remaining allowance to the decode loop, so the cap stops
            # the decode (and its allocations) instead of just its result.
            allowance = None if max_readings is None else max_readings - total_readings
            transform = _extend_with_gps_quality if stream_key == "GPS5" else None
            stream = _stream_from_collection(
                f, stream_key, col, time_base, max_readings=allowance, transform=transform
            )
            if stream.sample_count == 0:
                continue

            total_readings += stream.sample_count
            # Each stream may run one reading over its own allowance, so this
            # after-the-fact check is what bounds the *total across streams*.
            if max_readings is not None and total_readings > max_readings:
                raise ValueError(
                    f"Telemetry extraction exceeds max_readings={max_readings} "
                    f"({total_readings} readings so far)."
                )
            telemetry_streams[stream_key] = stream

    return telemetry_streams
