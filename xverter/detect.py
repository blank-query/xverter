"""Input format detection by magic bytes and structure, never by extension
(extensions are honored only as a tiebreaker hint for .zar, whose magic
lives in the footer).

Kinds returned:
  god        - GoD container (header file, or a dir containing <TitleID>/00007000)
  stfs       - LIVE/CON/PIRS content package that is NOT a GoD header (XBLA/DLC/TU)
  iso        - XDVDFS image (bare game partition or full XGD1/2/3 redump)
  zar        - ZArchive
  cci        - Compressed Cerbios Image (LZ4 wrapper around an ISO)
  cso        - CISO v2, Xbox dialect (LZ4 wrapper around an ISO)
  chd        - MAME CHD (DVD-type, wrapping an ISO)
  zip / 7z   - archive holding a game (transparent input layer)
  gamedir    - extracted game directory (has default.xex at its root)
"""

import os
import struct

from .formats import stfs as stfs_mod
from .formats import zar as zar_mod
from .formats.xdvdfs import MAGIC as XDVDFS_MAGIC, PARTITION_BASES, SECTOR


class DetectError(Exception):
    pass


def detect(path):
    if os.path.isdir(path):
        return _detect_dir(path)
    if not os.path.isfile(path):
        # "Game.cci"/"Game.cso" may name a >4GiB split set existing only
        # as "Game.1.cci", "Game.2.cci", ... - detect via the first
        # slice but keep the original path (readers resolve the set the
        # same way).
        base, ext = os.path.splitext(path)
        first = "%s.1%s" % (base, ext)
        if ext.lower() in (".cci", ".cso") and os.path.isfile(first):
            kind, _ = _detect_file(first)
            return kind, path
        raise DetectError("no such file or directory: %s" % path)
    return _detect_file(path)


def _detect_dir(path):
    for name in os.listdir(path):
        if name.lower() in ("default.xex", "default.xbe"):
            return "gamedir", path
    # GoD/STFS tree: <TitleID>/<contenttype>/<package>
    hits = []
    for root, dirs, files in os.walk(path):
        depth = os.path.relpath(root, path).count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        base = os.path.basename(root)
        if base in ("00007000", "00005000") and files:
            for f in files:
                if not f.endswith(".data"):
                    hits.append(("god", os.path.join(root, f)))
        elif base in ("000D0000", "000B0000", "00000002") and files:
            hits.append(("stfs", os.path.join(root, files[0])))
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise DetectError("directory contains multiple game packages: %s"
                          % [h[1] for h in hits])
    raise DetectError("directory is not a game dir, GoD tree, or STFS tree: %s" % path)


def _detect_file(path):
    with open(path, "rb") as f:
        head8 = f.read(8)
        head = head8[:4]
        if head8 == b"MComprHD":
            return "chd", path
        if head == b"PK\x03\x04":
            return "zip", path
        if head8[:6] == b"7z\xbc\xaf\x27\x1c":
            return "7z", path
        if head == b"CCIM":
            return "cci", path
        if head == b"CISO":
            return "cso", path
        if head in stfs_mod.STFS_MAGICS:
            f.seek(stfs_mod.CONTENT_TYPE_OFFSET)
            ctype = struct.unpack(">I", f.read(4))[0]
            if ctype in (stfs_mod.CONTENT_TYPE_GOD_GAME,
                         stfs_mod.CONTENT_TYPE_GOD_XBOXORIG):
                return "god", path
            return "stfs", path
        for base in PARTITION_BASES:
            f.seek(base + 32 * SECTOR)
            if f.read(len(XDVDFS_MAGIC)) == XDVDFS_MAGIC:
                return "iso", path
    if zar_mod.is_zar(path):
        return "zar", path
    raise DetectError("unrecognized format: %s" % path)
