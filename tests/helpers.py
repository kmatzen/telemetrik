"""Synthetic MP4/GPMF payload builders for tests.

These build the minimum valid structure needed to exercise the parser without
requiring real GoPro footage as a fixture.
"""

import struct
from typing import List, Optional, Sequence


def klv(
    key: bytes, type_char: Optional[int], struct_size: int, repeat: int, payload: bytes
) -> bytes:
    """Build a single GPMF KLV element (32-bit aligned)."""
    assert len(payload) == struct_size * repeat, "payload length must equal struct_size * repeat"
    type_byte = type_char if type_char is not None else 0
    header = key + bytes([type_byte, struct_size]) + repeat.to_bytes(2, "big")
    pad = (-len(payload)) % 4
    return header + payload + b"\x00" * pad


def stmp(microseconds: int) -> bytes:
    """STMP: sample timestamp in microseconds (uint64)."""
    return klv(b"STMP", ord("J"), 8, 1, microseconds.to_bytes(8, "big"))


def scal(divisors: Sequence[int]) -> bytes:
    """SCAL: one int32 divisor per channel."""
    payload = struct.pack(f">{len(divisors)}l", *divisors)
    return klv(b"SCAL", ord("l"), 4, len(divisors), payload)


def stnm(name: str) -> bytes:
    data = name.encode("ascii")
    return klv(b"STNM", ord("c"), len(data), 1, data)


def siun(unit: str) -> bytes:
    data = unit.encode("ascii")
    return klv(b"SIUN", ord("c"), len(data), 1, data)


def strm(children: bytes) -> bytes:
    return klv(b"STRM", 0, 1, len(children), children)


def devc(children: bytes) -> bytes:
    return klv(b"DEVC", 0, 1, len(children), children)


def accl_payload(readings: Sequence[Sequence[int]]) -> bytes:
    """ACCL: N readings of (x, y, z) int16."""
    flat = [v for reading in readings for v in reading]
    payload = struct.pack(f">{len(flat)}h", *flat)
    return klv(b"ACCL", ord("s"), 6, len(readings), payload)


def gps5_payload(readings: Sequence[Sequence[int]]) -> bytes:
    """GPS5: N readings of (lat, lon, alt, speed_2d, speed_3d) int32 (raw, pre-SCAL)."""
    flat = [v for reading in readings for v in reading]
    payload = struct.pack(f">{len(flat)}l", *flat)
    return klv(b"GPS5", ord("l"), 20, len(readings), payload)


def gpsf(fix: int) -> bytes:
    return klv(b"GPSF", ord("L"), 4, 1, fix.to_bytes(4, "big"))


def gpsp(precision: int) -> bytes:
    return klv(b"GPSP", ord("S"), 2, 1, precision.to_bytes(2, "big"))


# ---------------------------------------------------------------------------
# Minimal synthetic MP4 container
# ---------------------------------------------------------------------------


def mp4_box(key: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + key + payload


def _mdhd(timescale: int, version: int = 0) -> bytes:
    if version == 1:
        body = bytes([1, 0, 0, 0])  # version + flags
        # Distinguishable (non-zero) creation/modification times: reading the
        # timescale at the wrong offset (e.g. assuming the 32-bit layout)
        # would land inside these fields and produce a clearly wrong value,
        # rather than accidentally landing on zero.
        body += bytes([0x11] * 8)  # creation_time (64-bit)
        body += bytes([0x22] * 8)  # modification_time (64-bit)
        body += timescale.to_bytes(4, "big")
        body += bytes(8)  # duration (64-bit)
    else:
        body = bytes(4)  # version + flags
        body += bytes(4) * 2  # creation_time, modification_time (32-bit)
        body += timescale.to_bytes(4, "big")
        body += bytes(4)  # duration (32-bit)
    body += bytes(2)  # language
    body += bytes(2)  # reserved
    return mp4_box(b"mdhd", body)


def _stts(sample_deltas: Sequence[int]) -> bytes:
    """One (count=1, delta) entry per sample, in microseconds/timescale units."""
    body = bytes(4)  # version + flags
    body += len(sample_deltas).to_bytes(4, "big")
    for delta in sample_deltas:
        body += (1).to_bytes(4, "big")  # sample_count
        body += delta.to_bytes(4, "big")  # sample_delta
    return mp4_box(b"stts", body)


def _stsz(sample_sizes: Sequence[int]) -> bytes:
    body = bytes(4)  # version + flags
    body += (0).to_bytes(4, "big")  # sample_size == 0 -> use per-sample table
    body += len(sample_sizes).to_bytes(4, "big")
    for size in sample_sizes:
        body += size.to_bytes(4, "big")
    return mp4_box(b"stsz", body)


def _stco(offsets: Sequence[int]) -> bytes:
    body = bytes(4)
    body += len(offsets).to_bytes(4, "big")
    for offset in offsets:
        body += offset.to_bytes(4, "big")
    return mp4_box(b"stco", body)


def _co64(offsets: Sequence[int]) -> bytes:
    body = bytes(4)
    body += len(offsets).to_bytes(4, "big")
    for offset in offsets:
        body += offset.to_bytes(8, "big")
    return mp4_box(b"co64", body)


def build_gopro_mp4(
    devc_payloads: List[bytes],
    timescale: int = 1000000,
    mdhd_version: int = 0,
    use_co64: bool = False,
    sample_deltas: Optional[Sequence[int]] = None,
) -> bytes:
    """
    Build a minimal synthetic MP4 file with a GPMF metadata track.

    *devc_payloads* are placed back-to-back as the "samples"; their absolute
    file offsets are recorded in the stco/co64 + stsz sample table. Pass
    *sample_deltas* (one per sample) to also emit an stts box, giving samples
    after the first a non-zero DTS/PTS.
    """

    def chunk_offset_box(offsets: Sequence[int]) -> bytes:
        return _co64(offsets) if use_co64 else _stco(offsets)

    stts = _stts(sample_deltas) if sample_deltas is not None else b""

    gmhd = mp4_box(b"gmhd", mp4_box(b"gpmd", b""))
    stbl = mp4_box(
        b"stbl",
        _stsz([len(p) for p in devc_payloads]) + chunk_offset_box([0] * len(devc_payloads)) + stts,
    )
    minf = mp4_box(b"minf", gmhd + stbl)
    mdia = mp4_box(b"mdia", _mdhd(timescale, mdhd_version) + minf)
    trak = mp4_box(b"trak", mdia)
    moov = mp4_box(b"moov", trak)

    # Sample data follows moov; patch the real absolute offsets into stco/co64.
    offsets = []
    running = len(moov)
    for payload in devc_payloads:
        offsets.append(running)
        running += len(payload)

    stbl = mp4_box(
        b"stbl", _stsz([len(p) for p in devc_payloads]) + chunk_offset_box(offsets) + stts
    )
    minf = mp4_box(b"minf", gmhd + stbl)
    mdia = mp4_box(b"mdia", _mdhd(timescale, mdhd_version) + minf)
    trak = mp4_box(b"trak", mdia)
    moov = mp4_box(b"moov", trak)

    return moov + b"".join(devc_payloads)
