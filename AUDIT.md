# Repository audit — telemetrik

Audit of `telemetrik` 0.1.0 (2026-07-19). Overall: a clean, focused, zero-dependency
library with good docs and packaging. The main risks are **two confirmed correctness
bugs in the parser**, **no test suite**, and **CI that doesn't enforce the tooling the
project already configures**.

Findings are ordered by priority. Items 1–2 were reproduced with synthetic GPMF
payloads (repro code included below); everything else is from static review, `ruff`,
and `mypy --strict`.

---

## 1. Confirmed bugs

### 1.1 Multi-value SCAL is ignored — GPS altitude and speeds are wrong by 10⁴–10⁵×

`src/telemetrik/parser.py:340-347` (and the duplicate at `parser.py:459-466`) read
SCAL as a single scalar:

```python
if scal.struct_size == 2:
    scale_divisor = _from_bytes(f.read(2), signed=True)
elif scal.struct_size == 4:
    scale_divisor = _from_bytes(f.read(4), signed=True)
```

But SCAL is `struct_size × repeat`, and real GoPro GPS5 streams carry **five**
per-channel divisors: `[10000000, 10000000, 1000, 1000, 100]`. The code reads only
the first (10⁷) and divides *all five* GPS5 channels by it.

Reproduced with a synthetic payload using the real GPS5 SCAL values:

```
lat=37.7749  lon=-122.4194  alt=0.00153  spd2d=5e-05   spd3d=5.1e-05
expected:                   alt=15.3     spd2d=0.5     spd3d=5.1
```

Latitude/longitude happen to be correct (their divisor comes first), which makes
this easy to miss. The README's own example (`alt={alt:.0f}m`) prints `0m` for real
footage. Any stream whose SCAL `repeat > 1` is affected.

**Fix:** read all `repeat` SCAL values into a tuple and apply them element-wise in
`_parse_gpmf_value` (scalar divisor broadcast when SCAL has a single value).

### 1.2 Streams with a single payload crash with `TypeError`

`src/telemetrik/parser.py:376` (duplicate at `parser.py:510`):

```python
timestamp_ms = base_timestamp_ms + (i * reading_duration_ms)
```

`reading_duration_ms` is initialized to `None` and only assigned when
`payload_idx + 1 < len(stmp_timestamps)`. With exactly one matching payload (a very
short clip, or a stream that only appears once), it is still `None` at first use:

```
TypeError: unsupported operand type(s) for *: 'int' and 'NoneType'
```

`mypy --strict` flags both call sites. **Fix:** fall back to a sensible default for
the last/only payload — e.g. reuse the previous payload's duration, or `0.0` when
there is only one payload.

---

## 2. Robustness of the MP4/GPMF walkers

These are edge cases where malformed or merely uncommon files cause hangs or wrong
data instead of a clean error:

- **`get_boxes` can loop forever** (`parser.py:37-57`). A box with `size == 0`
  ("extends to end of file", legal for a trailing top-level box) makes
  `offset += size` a no-op → infinite loop. A truncated 8-byte header read has the
  same effect (`_from_bytes(b"") == 0`). Guard with `size < 8 → raise ValueError`,
  and bail out when `len(header) < 8`.
- **No 64-bit box sizes.** `size == 1` means the real size follows as a 64-bit
  `largesize`. Unhandled, the walker misparses everything after such a box.
- **No `co64` support** (`parser.py:91`). `get_samples` reads only `stco` (32-bit
  chunk offsets). Files whose sample data sits beyond 4 GB use `co64`;
  `get_boxes(...)[0]` then raises a bare `IndexError` instead of either parsing the
  file or reporting a clear error.
- **`mdhd` version 1 not handled** (`parser.py:616-619`). The code assumes the
  version-0 layout (32-bit times). A version-1 `mdhd` (64-bit times) puts the
  timescale 8 bytes later, so `time_base` — and every `pts_data` timestamp — is
  garbage. Read the version byte and adjust the offset.
- **Non-ASCII box keys** raise `UnicodeDecodeError` on corrupt input; wrap the walk
  so callers get `ValueError("not a valid MP4/GPMF file")` rather than assorted
  low-level exceptions.

## 3. GPMF type table gaps (`_parse_gpmf_value`, `parser.py:185-253`)

- **`'q'` is misinterpreted.** Per the GPMF spec, `q` is a 32-bit Q15.16
  fixed-point number — not "quaternion of 4 floats". The current branch also always
  reads 16 bytes regardless of `struct_size`, which desyncs every subsequent read in
  that payload. (Quaternion streams like CORI/IORI are actually type `s` with SCAL,
  which is why this hasn't bitten yet.)
- **`'b'`/`'B'` with `struct_size > 1`** are parsed as one big integer instead of a
  tuple of bytes.
- **Missing types:** `'d'` (float64), `'j'`/`'J'` (int64/uint64), `'F'` (FourCC),
  `'U'` (UTC date string), `'?'` (complex/typed struct). The raw-bytes fallback is a
  reasonable default, but `'d'`, `'j'`, `'J'`, and `'U'` appear in real GoPro files
  (e.g. GPSU on older cameras) and are cheap to add.
- The parameter is annotated `type_char: str` but callers pass `Optional[str]`
  (flagged by mypy).

## 4. Testing — the biggest structural gap

There are **no tests**. `pyproject.toml` configures `testpaths = ["tests"]`, but the
directory doesn't exist, and CI only smoke-imports the package. For a binary-format
parser this is the highest-leverage improvement available — both confirmed bugs above
would have been caught by a first-pass unit suite.

Recommended approach (no large binary fixtures needed):

1. **Synthetic payload builders** — a ~15-line `klv(key, type, struct_size, repeat,
   payload)` helper builds valid GPMF in memory; a similar helper nests MP4 boxes.
   This is exactly how the two bugs above were reproduced.
2. **Unit tests:** one per type character in `_parse_gpmf_value`; multi-value SCAL;
   single-payload streams; sticky GPSF/GPSP carry-over; STMP/PTS timestamp
   interpolation; `get_boxes`/`get_gpmf_boxes` on nested and malformed input
   (truncated, zero-size box) with timeouts.
3. **CLI tests:** run `main()` against a tmp dir; assert CSV contents, exit codes,
   missing-file handling.
4. Optionally, one small real GoPro clip (a few MB, or a truncated file containing
   just `moov` + one telemetry `mdat` chunk) as an integration fixture.

## 5. CI/CD

- **CI doesn't run the linter the repo configures.** `ruff check` currently reports
  19 violations (mostly `UP006`/`UP035` — the configured `target-version = "py39"`
  supports built-in generics, so `List`/`Dict`/`Tuple`/`Optional` should become
  `list`/`dict`/`tuple`/`X | None` via `from __future__ import annotations`), and
  `ruff format --check` would reformat `parser.py`.
- **No type checking in CI** despite shipping `py.typed` and the `Typing :: Typed`
  classifier; `mypy --strict` currently fails (see §1.2).
- Add to `ci.yml`: `ruff check`, `ruff format --check`, `mypy src`, `pytest` (once
  tests exist). Run lint/type once on a single Python version; keep the test matrix.
- `publish.yml` is in good shape (OIDC trusted publishing, separate build/publish
  jobs, `environment: pypi`). Optional hardening: pin actions to commit SHAs and add
  a `twine check dist/*` step after build.

## 6. Code quality and performance

- **~120 lines duplicated** between `get_stream_data` and
  `_get_gps_data_with_sticky_metadata` (payload scan, SCAL read, STMP read,
  timestamp interpolation). Extract a shared payload-collection helper; the GPS
  variant then only adds the sticky GPSF/GPSP lookup and tuple extension.
- **Repeated full-file passes.** `extract_all_telemetry` re-walks every sample's
  GPMF tree once per requested stream (10 by default), and `get_stream_data` calls
  `get_gpmf_boxes` up to 6× per payload. A single pass per sample that indexes all
  STRM children once would cut I/O roughly 10×on default extraction — noticeable on
  long recordings.
- `import struct` appears inside two function bodies; move to module top.
- `type` shadows the builtin in `get_gpmf_boxes` (cosmetic).

## 7. CLI

- **Exit code is always 0**, even when every input file fails. Track failures and
  `sys.exit(1)` so the CLI is usable in scripts.
- Output files are written next to the source video with no `--output-dir` option
  and **silently overwrite** existing files.
- CSV output has no header row, and string values containing commas are not quoted —
  use the `csv` module. A header like `timestamp_ms,x,y,z` (derived from the stream
  key) would make the files self-describing.
- Nice-to-have: `--format json` and GPX export for the GPS stream are natural
  extensions users of this kind of tool expect.

## 8. Packaging / hygiene

- **Version is duplicated** in `pyproject.toml` and `__init__.py`. Use hatch's
  dynamic versioning (`[tool.hatch.version] path = "src/telemetrik/__init__.py"` +
  `dynamic = ["version"]`) so releases can't skew.
- Add a dev dependency group (`[dependency-groups] dev = ["pytest", "ruff", "mypy"]`)
  so contributors and CI install the same toolchain.
- `.gitignore`, LICENSE, CHANGELOG, README, `py.typed` are all present and solid.
  README is genuinely good — accurate API docs and honest attribution. After fixing
  §1.1, re-verify the README GPS example output against a real file.

---

## Suggested order of work

1. Fix the two confirmed bugs (§1) — they affect the library's core promise.
2. Stand up the test suite (§4) with synthetic payloads, locking in the §1 fixes.
3. Enforce ruff + mypy + pytest in CI (§5) and clean the existing 19 lint findings.
4. Robustness hardening of the walkers (§2) and type-table fixes (§3).
5. Deduplicate the GPS/generic extraction paths and single-pass extraction (§6).
6. CLI exit codes / output options (§7) and packaging polish (§8).

## Appendix — repro for §1.1 and §1.2

```python
import io, struct
from telemetrik.parser import Sample, get_stream_data, _get_gps_data_with_sticky_metadata

def klv(key, type_char, struct_size, repeat, payload):
    pad = (-len(payload)) % 4
    return key + bytes([type_char, struct_size]) + repeat.to_bytes(2, "big") + payload + b"\x00" * pad

# --- 1.2: single payload -> TypeError ---
stmp = klv(b"STMP", ord("J"), 8, 1, (1000).to_bytes(8, "big"))
accl = klv(b"ACCL", ord("s"), 6, 2, struct.pack(">6h", 100, 200, 300, 400, 500, 600))
strm = klv(b"STRM", 0, 1, len(stmp + accl), stmp + accl)
devc = klv(b"DEVC", 0, 1, len(strm), strm)
get_stream_data(io.BytesIO(devc), [Sample(0, len(devc))], "ACCL")
# TypeError: unsupported operand type(s) for *: 'int' and 'NoneType'

# --- 1.1: real-world GPS5 SCAL -> wrong altitude/speeds ---
def gps_payload(stmp_us):
    stmp = klv(b"STMP", ord("J"), 8, 1, stmp_us.to_bytes(8, "big"))
    scal = klv(b"SCAL", ord("l"), 4, 5, struct.pack(">5l", 10000000, 10000000, 1000, 1000, 100))
    gps5 = klv(b"GPS5", ord("l"), 20, 1, struct.pack(">5l", 377749000, -1224194000, 15300, 500, 510))
    body = stmp + scal + gps5
    strm = klv(b"STRM", 0, 1, len(body), body)
    return klv(b"DEVC", 0, 1, len(strm), strm)

p1, p2 = gps_payload(0), gps_payload(1_000_000)
f = io.BytesIO(p1 + p2)
s = _get_gps_data_with_sticky_metadata(f, [Sample(0, len(p1)), Sample(len(p1), len(p2))])
print(s.data[0])  # alt=0.00153 (should be 15.3), spd2d=5e-05 (should be 0.5)
```
