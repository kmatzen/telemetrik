"""Real-sample regression test for the GPS5 SCAL per-channel fix (issue #1 / PR #10).

The synthetic payload tests in ``test_parser.py`` prove the decode logic; this
exercises the *same* fix against a real GoPro file, so a regression in the
MP4/GPMF box walk or the SCAL handling shows up on actual telemetry rather than
only on hand-built bytes.

The sample is deliberately **not vendored** (GoPro sample-redistribution terms
are unclear, and a multi-MB binary would bloat a pure-Python package). CI
fetches it from a pinned ``gpmf-parser`` commit, verifies its checksum, and
points ``TELEMETRIK_SAMPLE`` at it. Locally the test skips unless you provide
that env var::

    TELEMETRIK_SAMPLE=/path/to/max-heromode.mp4 pytest tests/test_real_sample.py
"""

import hashlib
import os

import pytest

from telemetrik import extract_all_telemetry

# gpmf-parser ``samples/max-heromode.mp4`` pinned to commit
# e40f6d9d3d7d17c9641c26d60e56d2927151d490. It is the only official GoPro
# sample that carries STMP payload timestamps (the HERO5/6/7 samples use TSMP
# and currently decode to zero readings -- see issue #17).
SAMPLE_SHA256 = "8e8fa98887f86119be1b886762b1080a92afb6cc146db719a238bdcba908277b"


@pytest.fixture
def sample_path() -> str:
    path = os.environ.get("TELEMETRIK_SAMPLE")
    if not path or not os.path.exists(path):
        pytest.skip("real GoPro sample not available; set TELEMETRIK_SAMPLE to run")
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    assert digest == SAMPLE_SHA256, (
        f"TELEMETRIK_SAMPLE checksum mismatch: {digest} != {SAMPLE_SHA256}"
    )
    return path


def test_gps5_scal_per_channel_on_real_sample(sample_path: str) -> None:
    streams = extract_all_telemetry(sample_path, streams=["GPS5"])
    assert "GPS5" in streams, "GPS5 stream missing from a sample that contains one"

    gps = streams["GPS5"]
    assert gps.sample_count == 191
    assert gps.units == "deg,deg,m,m/s,m/s,fix,dop"

    _, (lat, lon, alt, speed_2d, speed_3d, _fix, _dop) = gps.data[0]

    # Recorded near Carlsbad, CA -- lat/lon use channel 0's divisor and were
    # correct even before the fix, so they anchor that nothing else shifted.
    assert 33.0 < lat < 33.5, lat
    assert -117.5 < lon < -117.0, lon

    # The per-channel SCAL fix (#10): before it, altitude and speed reused
    # channel 0's divisor and came out ~1e4 too small (alt ~-0.0023 m,
    # speed_2d ~1e-4 m/s). Correct values are alt ~-23 m, speed_2d ~1 m/s.
    assert abs(alt) > 1.0, f"altitude {alt} looks unscaled (SCAL-per-channel regression)"
    assert 0.5 < speed_2d < 5.0, f"speed_2d {speed_2d} looks unscaled"
    assert speed_3d >= 0.0
