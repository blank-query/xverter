"""CHD (MAME Compressed Hunks of Data) support via the reference
`chdman` binary.

Scope: DVD-type CHDs (chdman createdvd / extractdvd) wrapping a
2048-byte-sector XDVDFS image - a bare game partition, a full redump
image, or (through the pivot) anything else xverter can read. All Xbox
and Xbox 360 discs are DVD-structured, so `createcd` never applies in
this project's scope; the writers stay content-agnostic like CCI/CSO.

Consumer status (2026-08): no released Xbox emulator reads CHD yet.
xemu had working CHD support in review (PR #2921, libchdr, tested on
both redump and trimmed images) before the author deleted their fork;
xverter produces the format that work targeted, ready for its revival.

Same delegation policy as ZAR/ISO/GoD: chdman (MAME's own maintained
tool) does the writing and extraction; xverter parses the CHD v5 header
natively for detection and info (it stores logical size and SHA-1s of
the decompressed data), and round-trip verification is done with
xverter's readers on the extracted stream.

CHD v5 header (all big-endian, 124 bytes):
  char[8] magic         "MComprHD"
  u32     length        124
  u32     version       5
  u32[4]  compressors   fourccs (zero-padded list)
  u64     logicalbytes  decompressed size
  u64     mapoffset
  u64     metaoffset
  u32     hunkbytes
  u32     unitbytes
  u8[20]  rawsha1       data SHA-1
  u8[20]  sha1          data+metadata SHA-1
  u8[20]  parentsha1    zero when standalone
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
    """chd -> ISO via chdman extractdvd."""
    exe = _need_chdman()
    _run([exe, "extractdvd", "-i", chd_path, "-o", out_iso, "-f"],
         progress=progress)
    if not os.path.isfile(out_iso):
        raise ChdError("chdman reported success but wrote no output")
    return out_iso


def build(iso_path, out_chd, progress=None):
    """ISO -> chd via chdman createdvd; the input is wrapped as-is
    (bare partition, full redump image, whatever - content-agnostic)."""
    exe = _need_chdman()
    if os.path.exists(out_chd):
        raise ChdError("output already exists: %s" % out_chd)
    _run([exe, "createdvd", "-i", iso_path, "-o", out_chd],
         progress=progress)
    if not os.path.isfile(out_chd):
        raise ChdError("chdman reported success but wrote no output")
    return out_chd


def verify(chd_path, progress=None):
    """chdman verify: decompresses everything and checks the header's
    internal SHA-1s. Returns the parsed header on success."""
    exe = _need_chdman()
    _run([exe, "verify", "-i", chd_path], progress=progress)
    return read_header(chd_path)
