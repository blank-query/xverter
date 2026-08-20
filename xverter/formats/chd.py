"""CHD (MAME Compressed Hunks of Data) - native v5 reader and writer.

Scope: DVD-type CHDs wrapping a 2048-byte-sector XDVDFS image - a bare
game partition, a full redump image, or (through the pivot) anything
else xverter can read. All Xbox and Xbox 360 discs are DVD-structured,
so CD semantics never apply here; the writer stays content-agnostic
like CCI/CSO.

Consumer status (2026-08): no released Xbox emulator reads CHD yet.
xemu had working CHD support in review (PR #2921, libchdr, tested on
both redump and trimmed images) before the author deleted their fork;
xverter produces the format that work targeted, ready for its revival.

Reading, writing and verification are fully native
(formats/chd_native.py, formats/chd_flac.py). The format reference is
MAME's BSD-3-Clause CHD library, reimplemented rather than copied; see
the README for the validation story. Two shapes are refused outright:
parent-delta CHDs (they reference a file we were not given) and
pre-v5 files (flatten either to a standalone v5 CHD with MAME's tools
and it reads fine).
"""

import os
import struct

MAGIC = b"MComprHD"
V5_HEADER = struct.Struct(">8sII4IQQQII20s20s20s")
V5_SIZE = 124


class ChdError(Exception):
    pass


def is_chd(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_header(path):
    """Parse the CHD v5 header natively."""
    with open(path, "rb") as f:
        raw = f.read(V5_SIZE)
    if len(raw) < V5_SIZE or raw[:8] != MAGIC:
        raise ChdError("not a CHD file: %s" % path)
    (_, length, version, c0, c1, c2, c3, logical, mapoff, metaoff,
     hunkbytes, unitbytes, rawsha1, sha1, parentsha1) = V5_HEADER.unpack(raw)
    if version != 5:
        raise ChdError("CHD v%d is not supported - only v5; convert it "
                       "to a standalone v5 CHD first" % version)
    comps = []
    for c in (c0, c1, c2, c3):
        if c:
            comps.append(struct.pack(">I", c).decode("ascii", "replace"))
    return {
        "version": version,
        "logical_bytes": logical,
        "hunk_bytes": hunkbytes,
        "unit_bytes": unitbytes,
        "compressors": comps,
        "raw_sha1": rawsha1.hex(),
        "sha1": sha1.hex(),
        "parent_sha1": (parentsha1.hex()
                        if parentsha1 != b"\x00" * 20 else None),
    }


def extract(chd_path, out_iso, progress=None):
    """chd -> ISO, natively and in parallel, verified as it goes: the
    decoded stream's SHA-1 is computed during extraction and must match
    the header's, so a damaged CHD cannot extract quietly.

    Parent-delta CHDs are refused: they reference a file we were not
    given. Flattened standalone v5 files read fine."""
    from . import chd_native
    try:
        got = chd_native.extract_to(chd_path, out_iso, progress=progress)
    except chd_native.ChdNativeError as e:
        raise ChdError(str(e))
    with open(chd_path, "rb") as fh:
        want = chd_native.read_header(fh)["rawsha1"]
    if got != want:
        raise ChdError("extracted data sha1 %s does not match the "
                       "header's %s - the CHD is damaged" % (got, want))
    return out_iso


def build(iso_path, out_chd, progress=None):
    """ISO -> chd with xverter's native writer; the input is wrapped
    as-is (bare partition, full redump image, whatever -
    content-agnostic). The reference implementation verified this
    writer's output and extracted it byte-identical before it shipped;
    the suite holds it to byte-identity against the source ever since."""
    from . import chd_native
    if os.path.exists(out_chd):
        raise ChdError("output already exists: %s" % out_chd)
    try:
        # Everything xverter currently wraps in a CHD is an XDVDFS or
        # STFS payload - game data, no CD audio - so the FLAC audition
        # is left out of the codec list entirely rather than probed per
        # hunk. The encoder exists and is exercised (chd_native's
        # default list includes it, and the CD-type path will want it);
        # this call site simply knows its content.
        chd_native.write_dvd(iso_path, out_chd,
                             compressors=(b"lzma", b"zlib"),
                             progress=progress)
    except chd_native.ChdNativeError as e:
        raise ChdError("native CHD build failed: %s" % e)
    return out_chd


def verify(chd_path, progress=None):
    """Decompress everything and check both of the header's internal
    SHA-1s, natively. Returns the parsed header on success."""
    from . import chd_native
    try:
        chd_native.verify_file(chd_path, progress=progress)
    except chd_native.ChdNativeError as e:
        raise ChdError(str(e))
    return read_header(chd_path)
