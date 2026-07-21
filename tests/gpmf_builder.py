"""Helpers for constructing synthetic MP4/GPMF binary fixtures for tests."""

import struct
from typing import List, Optional


def u16(n: int) -> bytes:
    return n.to_bytes(2, "big")


def u32(n: int) -> bytes:
    return n.to_bytes(4, "big")


def mp4_box(fourcc: str, payload: bytes) -> bytes:
    """Build a raw MP4 box: 4-byte size + 4-byte fourcc + payload."""
    size = 8 + len(payload)
    return u32(size) + fourcc.encode("ascii") + payload


def gpmf_leaf(
    key: str, type_char: Optional[str], struct_size: int, repeat: int, payload: bytes
) -> bytes:
    """Build a single GPMF KLV element (leaf or container)."""
    raw_len = struct_size * repeat
    assert len(payload) == raw_len, f"{key}: payload length {len(payload)} != {raw_len}"
    type_byte = b"\x00" if type_char is None else type_char.encode("ascii")
    header = key.encode("ascii") + type_byte + bytes([struct_size]) + u16(repeat)
    total_size = ((raw_len + 8 + 3) // 4) * 4
    pad = total_size - 8 - raw_len
    return header + payload + b"\x00" * pad


def gpmf_container(key: str, children: List[bytes]) -> bytes:
    """Build a GPMF container element (type byte == 0) wrapping *children*."""
    payload = b"".join(children)
    assert len(payload) % 4 == 0
    return gpmf_leaf(key, None, 1, len(payload), payload)


def stmp_leaf(stmp_us: int) -> bytes:
    return gpmf_leaf("STMP", "J", 8, 1, stmp_us.to_bytes(8, "big"))


def scal_leaf(value: int, wide: bool = False) -> bytes:
    if wide:
        return gpmf_leaf("SCAL", "l", 4, 1, struct.pack(">i", value))
    return gpmf_leaf("SCAL", "s", 2, 1, struct.pack(">h", value))


def stnm_leaf(name: str) -> bytes:
    raw = name.encode("ascii")
    return gpmf_leaf("STNM", "c", len(raw), 1, raw)


def siun_leaf(units: str) -> bytes:
    raw = units.encode("ascii")
    return gpmf_leaf("SIUN", "c", len(raw), 1, raw)


def gpsf_leaf(fix: int) -> bytes:
    return gpmf_leaf("GPSF", "L", 4, 1, struct.pack(">I", fix))


def gpsp_leaf(precision: int) -> bytes:
    return gpmf_leaf("GPSP", "S", 2, 1, struct.pack(">H", precision))


def s16_values(values) -> bytes:
    return b"".join(struct.pack(">h", v) for v in values)


def strm_block(
    stmp_us: int,
    stream_key: str,
    type_char: str,
    struct_size: int,
    repeat: int,
    values_payload: bytes,
    scal: Optional[int] = None,
    scal_wide: bool = False,
    name: Optional[str] = None,
    units: Optional[str] = None,
    gpsf: Optional[int] = None,
    gpsp: Optional[int] = None,
    include_stmp: bool = True,
) -> bytes:
    """Build a single STRM container with the given stream payload and metadata."""
    children = []
    if include_stmp:
        children.append(stmp_leaf(stmp_us))
    if scal is not None:
        children.append(scal_leaf(scal, wide=scal_wide))
    if gpsf is not None:
        children.append(gpsf_leaf(gpsf))
    if gpsp is not None:
        children.append(gpsp_leaf(gpsp))
    children.append(gpmf_leaf(stream_key, type_char, struct_size, repeat, values_payload))
    if name is not None:
        children.append(stnm_leaf(name))
    if units is not None:
        children.append(siun_leaf(units))
    return gpmf_container("STRM", children)


def devc_block(strm_blocks: List[bytes]) -> bytes:
    return gpmf_container("DEVC", strm_blocks)


def build_mp4(
    sample_payloads: List[bytes],
    timescale: int = 90000,
    sample_deltas: Optional[List[int]] = None,
    composition_offsets: Optional[List[int]] = None,
    include_gpmf_marker: bool = True,
) -> bytes:
    """
    Build a minimal synthetic MP4 file with a single GPMF metadata track.

    *sample_payloads* are the raw bytes of each DEVC sample (placed in mdat,
    referenced by stco/stsz). *sample_deltas* (stts) and *composition_offsets*
    (ctts) default to 1 tick apart / zero, one entry per sample.
    """
    num_samples = len(sample_payloads)
    sizes = [len(p) for p in sample_payloads]

    if sample_deltas is None:
        sample_deltas = [1] * num_samples
    if composition_offsets is None:
        composition_offsets = [0] * num_samples

    # stsz: version/flags(4) + sample_size(4)=0 + num_entries(4) + size per sample
    stsz_payload = u32(0) + u32(0) + u32(num_samples) + b"".join(u32(s) for s in sizes)
    stsz = mp4_box("stsz", stsz_payload)

    # stco: version/flags(4) + num_entries(4) + offset per sample (filled below)
    stco_placeholder_payload = u32(0) + u32(num_samples) + b"".join(u32(0) for _ in sizes)
    stco_placeholder = mp4_box("stco", stco_placeholder_payload)

    # stts: version/flags(4) + num_entries(4) + (count=1, delta) per sample
    stts_entries = b"".join(u32(1) + u32(d) for d in sample_deltas)
    stts_payload = u32(0) + u32(num_samples) + stts_entries
    stts = mp4_box("stts", stts_payload)

    # ctts: version/flags(4) + num_entries(4) + (count=1, offset) per sample
    ctts_entries = b"".join(u32(1) + u32(o) for o in composition_offsets)
    ctts_payload = u32(0) + u32(num_samples) + ctts_entries
    ctts = mp4_box("ctts", ctts_payload)

    stbl_payload = stsz + stco_placeholder + stts + ctts
    stbl = mp4_box("stbl", stbl_payload)

    if include_gpmf_marker:
        gpmd = mp4_box("gpmd", b"")
        gmhd = mp4_box("gmhd", gpmd)
    else:
        gmhd = b""

    minf = mp4_box("minf", gmhd + stbl)

    # mdhd: version/flags(4) + creation(4) + modification(4) + timescale(4)
    #       + duration(4) + language(2) + pre_defined(2)
    mdhd_payload = u32(0) + u32(0) + u32(0) + u32(timescale) + u32(0) + u16(0) + u16(0)
    mdhd = mp4_box("mdhd", mdhd_payload)

    mdia = mp4_box("mdia", mdhd + minf)
    trak = mp4_box("trak", mdia)
    moov = mp4_box("moov", trak)

    mdat_header_len = 8
    mdat_start = len(moov) + mdat_header_len

    offsets = []
    running = mdat_start
    for s in sizes:
        offsets.append(running)
        running += s

    stco_payload = u32(0) + u32(num_samples) + b"".join(u32(o) for o in offsets)
    stco = mp4_box("stco", stco_payload)

    stbl_payload = stsz + stco + stts + ctts
    stbl = mp4_box("stbl", stbl_payload)
    minf = mp4_box("minf", gmhd + stbl)
    mdia = mp4_box("mdia", mdhd + minf)
    trak = mp4_box("trak", mdia)
    moov = mp4_box("moov", trak)
    assert len(moov) + mdat_header_len == mdat_start

    mdat = mp4_box("mdat", b"".join(sample_payloads))

    return moov + mdat
