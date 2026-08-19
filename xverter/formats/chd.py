"""CHD (MAME Compressed Hunks of Data) - native v5 reader and writer.

Scope: DVD-type CHDs wrapping a 2048-byte-sector XDVDFS image - a bare
game partition, a full redump image, or (through the pivot) anything
else xverter can read. All Xbox and Xbox 360 discs are DVD-structured,
so createcd semantics never apply here; the writer stays
content-agnostic like CCI/CSO.

Consumer status (2026-08): no released Xbox emulator reads CHD yet.
xemu had working CHD support in review (PR #2921, libchdr, tested on
both redump and trimmed images) before the author deleted their fork;
xverter produces the format that work targeted, ready for its revival.

This was the last delegated format. Reading, writing and verification
are native (formats/chd_native.py, formats/chd_flac.py); the format
reference is MAME's own BSD-3-Clause CHD library (src/lib/util/chd.cpp
and friends, Aaron Giles), reimplemented rather than copied, and held
to the only standard that matters: chdman verifies what we write and
extracts it byte-identical, and we read everything chdman writes -
FLAC hunks included. chdman remains recognised as an optional
differential reference and as the escape hatch for the two shapes we
refuse: parent-delta CHDs and pre-v5 files, both of which
`chdman copy` flattens into something we read.
"""

import os
import shutil
import struct
import subprocess

MAGIC = b"MComprHD"
V5_HEADER = struct.Struct(">8sII4IQQQII20s20s20s")
V5_SIZE = 124


class ChdError(Exception):
    pass


def _need_chdman():
    from .. import deps
    exe = deps.find("chdman")
    if not exe:
        raise ChdError("`chdman` binary not found - install MAME tools "
                       "(Arch/Debian/Ubuntu: mame-tools, macOS: brew "
                       "install rom-tools, Windows: chdman.exe ships "
                       "inside the MAME download)")
    return exe


def is_chd(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_header(path):
    """Parse the CHD v5 header natively (no chdman needed)."""
    with open(path, "rb") as f:
        raw = f.read(V5_SIZE)
    if len(raw) < V5_SIZE or raw[:8] != MAGIC:
        raise ChdError("not a CHD file: %s" % path)
    (_, length, version, c0, c1, c2, c3, logical, mapoff, metaoff,
     hunkbytes, unitbytes, rawsha1, sha1, parentsha1) = V5_HEADER.unpack(raw)
    if version != 5:
        raise ChdError("CHD v%d not supported (chdman can upgrade it: "
                       "`chdman copy`)" % version)
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


def _run(argv, progress=None):
    import re as _re
    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    # chdman reports "Compressing, 45.6% complete" style lines (\r-separated)
    for raw in iter(p.stdout.readline, ""):
        for line in raw.replace("\r", "\n").splitlines():
            line = line.strip()
            if not line:
                continue
            tail = (tail + [line])[-4:]
            if progress:
                m = _re.search(r"(\d+(?:\.\d+)?)%", line)
                if m:
                    progress(int(float(m.group(1)) * 10), 1000)
    p.stdout.close()
    if p.wait() != 0:
        raise ChdError("%s failed: %s"
                       % (os.path.basename(argv[0]), tail[-1:] or ["?"]))
    return p


def extract(chd_path, out_iso, progress=None):
    """chd -> ISO, natively and in parallel, verified as it goes: the
    decoded stream's SHA-1 is computed during extraction and must match
    the header's, so a damaged CHD cannot extract quietly.

    Parent-delta CHDs are the one case handed to chdman when present -
    they reference a file we do not have."""
    from . import chd_native
    try:
        got = chd_native.extract_to(chd_path, out_iso, progress=progress)
        with open(chd_path, "rb") as fh:
            want = chd_native.read_header(fh)["rawsha1"]
        if got != want:
            raise ChdError("extracted data sha1 %s does not match the "
                           "header's %s - the CHD is damaged" % (got, want))
        return out_iso
    except chd_native.ChdNativeError as e:
        if "parent" not in str(e):
            raise ChdError(str(e))
    exe = _need_chdman()
    _run([exe, "extractdvd", "-i", chd_path, "-o", out_iso, "-f"],
         progress=progress)
    if not os.path.isfile(out_iso):
        raise ChdError("chdman reported success but wrote no output")
    return out_iso


def build(iso_path, out_chd, progress=None):
    """ISO -> chd with xverter's native writer; the input is wrapped
    as-is (bare partition, full redump image, whatever -
    content-agnostic). chdman verifies the result and extracts it
    byte-identical; that equivalence gates this writer in the suite."""
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
        if "parent" in str(e):
            exe = _need_chdman()
            _run([exe, "verify", "-i", chd_path], progress=progress)
            return read_header(chd_path)
        raise ChdError(str(e))
    return read_header(chd_path)
