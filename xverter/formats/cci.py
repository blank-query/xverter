"""CCI (Compressed Cerbios Image) reading and writing.

CCI is the LZ4 sibling of the Xbox scene's CISO: a 2048-byte-sector
image (normally a bare XDVDFS game partition) stored block-by-block,
each block either raw or LZ4-HC compressed. Format facts were verified
against Team Resurgent's XboxToolkit/Repackinator and XGDTool sources
(both GPL - facts only, all code here is original) and validated
differentially against Repackinator output.

File layout (all little-endian):

  header, 32 bytes:
    char[4]  magic            "CCIM"
    u32      header_size      32
    u64      uncompressed_size   decoded bytes of THIS file (slice)
    u64      index_offset        absolute offset of the index (at EOF)
    u32      block_size        2048
    u8       version           1
    u8       index_alignment   2
    u16      reserved          0
  data blocks                 (start at offset 32)
  index at index_offset: N+1 u32 entries, N = uncompressed_size / 2048
    entry  = (file_offset >> 2) | (0x80000000 if LZ4-compressed)
    entry N (terminator) = end-of-data offset >> 2, top bit clear

Block storage:
  compressed: [u8 pad_count p][raw LZ4 block][p zero bytes], total
              size 4-aligned; chosen only when the LZ4 output is
              < 2040 bytes, so a compressed record is always < 2048
  raw:        the 2048 sector verbatim
Read rule: a block is compressed if its index flag is set OR its stored
size (next offset - offset) != 2048; payload length is then
stored_size - 1 - pad_count. No checksums, no EOF padding.

Split images: written as independent, standalone slices
Name.1.cci / Name.2.cci (each with its own header and index; a new
slice is started once the current one grows past ~0xFF000000 bytes so
the finished file stays under FATX's 4 GiB limit). An unsplit image is
a plain Name.cci.

Reading needs no third-party packages (pure-Python LZ4 fallback);
writing requires python-lz4 (installed with xverter; pyz users: pip install lz4).
"""

import os
import struct
import sys

from . import lz4compat
from .lz4compat import Lz4Missing  # noqa: F401  (re-exported convenience)

MAGIC = b"CCIM"
HEADER = struct.Struct("<4sLQQLBBH")
HEADER_SIZE = 32
BLOCK_SIZE = 2048
VERSION = 1
INDEX_ALIGNMENT = 2
SPLIT_OFFSET = 0xFF000000  # start a new slice once tell() exceeds this
COMPRESS_THRESHOLD = 2040  # store compressed only if LZ4 size < this

XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"
REDUMP_VD_OFFSET = 0x18310000   # volume descriptor inside a full OGX redump
REDUMP_IMAGE_OFFSET = 0x18300000  # game-partition base of that image

assert HEADER.size == HEADER_SIZE


class CciError(Exception):
    pass


def find_slices(path):
    """Return the ordered slice list for a CCI path.

    "Game.cci" -> [that file]; "Game.1.cci" (or .2 ...) -> every
    sibling "Game.<digit>.cci", sorted by slice number.
    """
    path = os.fspath(path)
    base, ext = os.path.splitext(path)
    stem, sub = os.path.splitext(base)
    if len(sub) == 2 and sub[1].isdigit():
        d = os.path.dirname(path)
        prefix = os.path.basename(stem) + "."
        found = []
        for name in os.listdir(d or "."):
            if (name.startswith(prefix) and name.endswith(ext)
                    and len(name) == len(prefix) + 1 + len(ext)
                    and name[len(prefix)].isdigit()):
                found.append((int(name[len(prefix)]), os.path.join(d, name)))
        found.sort()
        if not found:
            raise CciError("no slices found matching %s" % path)
        nums = [n for n, _ in found]
        if nums != list(range(nums[0], nums[0] + len(nums))):
            raise CciError("non-contiguous slice numbers: %s" % nums)
        return [p for _, p in found]
    if not os.path.exists(path):
        # "Game.cci" may name a split set that only exists as
        # "Game.1.cci", "Game.2.cci", ... - resolve via the first slice.
        first = "%s.1%s" % (base, ext)
        if os.path.exists(first):
            return find_slices(first)
    return [path]


def is_cci(path):
    """True if path (a .cci file or the first slice of a set) is CCI."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_SIZE)
    except OSError:
        return False
    if len(head) < HEADER_SIZE:
        return False
    magic, hsize, _, _, bsize, ver, align, _ = HEADER.unpack(head)
    return (magic == MAGIC and hsize == HEADER_SIZE and bsize == BLOCK_SIZE
            and ver == VERSION and align == INDEX_ALIGNMENT)


def _read_header(f, path):
    """Validate one slice's header; return (uncompressed_size, index_offset)."""
    f.seek(0)
    head = f.read(HEADER_SIZE)
    if len(head) < HEADER_SIZE:
        raise CciError("%s: file too small for CCI header" % path)
    magic, hsize, usize, ioff, bsize, ver, align, _ = HEADER.unpack(head)
    if magic != MAGIC:
        raise CciError("%s: bad magic %r (expected CCIM)" % (path, magic))
    if hsize != HEADER_SIZE:
        raise CciError("%s: bad header_size %d" % (path, hsize))
    if bsize != BLOCK_SIZE:
        raise CciError("%s: unsupported block_size %d" % (path, bsize))
    if ver != VERSION:
        raise CciError("%s: unsupported version %d" % (path, ver))
    if align != INDEX_ALIGNMENT:
        raise CciError("%s: unsupported index_alignment %d" % (path, align))
    if usize % BLOCK_SIZE:
        raise CciError("%s: uncompressed_size %d not a multiple of %d"
                       % (path, usize, BLOCK_SIZE))
    return usize, ioff


def _read_index(f, path, index_offset, nblocks):
    """Read N+1 index entries; return (offsets list, flags list)."""
    f.seek(index_offset)
    raw = f.read((nblocks + 1) * 4)
    if len(raw) != (nblocks + 1) * 4:
        raise CciError("%s: truncated index" % path)
    vals = struct.unpack("<%dI" % (nblocks + 1), raw)
    offs = [(v & 0x7FFFFFFF) << INDEX_ALIGNMENT for v in vals]
    flags = [bool(v & 0x80000000) for v in vals]
    if flags[-1]:
        raise CciError("%s: index terminator has compressed bit set" % path)
    for i in range(nblocks):
        if offs[i + 1] <= offs[i]:
            raise CciError("%s: index not strictly increasing at block %d"
                           % (path, i))
    return offs, flags


class CciReader:
    """Seekable, read-only file-like view of a CCI image's decoded ISO
    stream (same shape as god.GodStream: read/seek/tell/size), handling
    multi-slice sets transparently. Pass any slice or the plain file.
    """

    def __init__(self, path):
        self.slice_paths = find_slices(path)
        self._files = []
        self._slices = []       # per slice: (offsets, flags, nblocks)
        self._cum = [0]         # cumulative block counts
        try:
            for p in self.slice_paths:
                f = open(p, "rb")
                self._files.append(f)
                usize, ioff = _read_header(f, p)
                nblocks = usize // BLOCK_SIZE
                offs, flags = _read_index(f, p, ioff, nblocks)
                self._slices.append((offs, flags, nblocks))
                self._cum.append(self._cum[-1] + nblocks)
        except Exception:
            self.close()
            raise
        self.block_size = BLOCK_SIZE
        self.size = self._cum[-1] * BLOCK_SIZE
        self._pos = 0
        self._cache_idx = None
        self._cache = None

    # -- file-like API --
    def read(self, n=-1):
        if n < 0:
            n = self.size - self._pos
        n = max(0, min(n, self.size - self._pos))
        out = bytearray()
        while n > 0:
            block = self._read_block(self._pos // BLOCK_SIZE)
            in_block = self._pos % BLOCK_SIZE
            chunk = block[in_block:in_block + n]
            out += chunk
            self._pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def seek(self, off, whence=0):
        if whence == 0:
            self._pos = off
        elif whence == 1:
            self._pos += off
        elif whence == 2:
            self._pos = self.size + off
        self._pos = max(0, min(self._pos, self.size))
        return self._pos

    def tell(self):
        return self._pos

    def close(self):
        for f in self._files:
            f.close()
        self._files = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- internals --
    def _read_block(self, gidx):
        if gidx == self._cache_idx:
            return self._cache
        import bisect
        si = bisect.bisect_right(self._cum, gidx) - 1
        if si >= len(self._slices):
            raise CciError("block %d out of range" % gidx)
        offs, flags, nblocks = self._slices[si]
        local = gidx - self._cum[si]
        f = self._files[si]
        stored = offs[local + 1] - offs[local]
        f.seek(offs[local])
        if flags[local] or stored != BLOCK_SIZE:
            rec = f.read(stored)
            if len(rec) != stored:
                raise CciError("short read of block %d" % gidx)
            pad = rec[0]
            payload_len = stored - 1 - pad
            if payload_len <= 0:
                raise CciError("block %d: bad pad count %d for %d-byte record"
                               % (gidx, pad, stored))
            block = lz4compat.decode_block(rec[1:1 + payload_len], BLOCK_SIZE)
        else:
            block = f.read(BLOCK_SIZE)
            if len(block) != BLOCK_SIZE:
                raise CciError("short read of block %d" % gidx)
        self._cache_idx = gidx
        self._cache = block
        return block


def xbox_image_offset(stream):
    """Byte offset of the game partition inside stream: 0x18300000 for a
    full OG-Xbox redump image, else 0 (bare partition / anything else -
    the writers are content-agnostic and compress whatever they're
    given)."""
    pos = stream.tell()
    try:
        stream.seek(REDUMP_VD_OFFSET)
        if stream.read(len(XDVDFS_MAGIC)) == XDVDFS_MAGIC:
            return REDUMP_IMAGE_OFFSET
        return 0
    finally:
        stream.seek(pos)


class _SliceWriter:
    """One in-progress CCI slice: data blocks + deferred header/index."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(b"\x00" * HEADER_SIZE)   # placeholder header
        self.records = []                     # (record_size, compressed)

    def tell(self):
        return self.f.tell()

    def add_block(self, comp, raw):
        """Write one 2048-byte sector, choosing compressed or raw storage.
        comp may be None (lz4 output unavailable/oversized -> store raw)."""
        if comp is not None and len(comp) < COMPRESS_THRESHOLD:
            pad = (-(len(comp) + 1)) % 4
            self.f.write(bytes((pad,)))
            self.f.write(comp)
            if pad:
                self.f.write(b"\x00" * pad)
            self.records.append((1 + len(comp) + pad, True))
        else:
            self.f.write(raw)
            self.records.append((BLOCK_SIZE, False))

    def finalize(self):
        index_offset = self.f.tell()
        pos = HEADER_SIZE
        out = bytearray()
        for size, compressed in self.records:
            entry = (pos >> INDEX_ALIGNMENT) | (0x80000000 if compressed else 0)
            out += struct.pack("<I", entry)
            pos += size
        out += struct.pack("<I", pos >> INDEX_ALIGNMENT)  # terminator
        self.f.write(out)
        self.f.seek(0)
        self.f.write(HEADER.pack(MAGIC, HEADER_SIZE,
                                 len(self.records) * BLOCK_SIZE, index_offset,
                                 BLOCK_SIZE, VERSION, INDEX_ALIGNMENT, 0))
        self.f.close()


def build_cci(src, out_path, split=True, split_offset=SPLIT_OFFSET,
              image_offset=None, progress=None):
    """Compress an ISO stream (path or seekable file-like) to CCI.

    out_path should end in .cci. The image is written as Name.1.cci,
    Name.2.cci, ... slices; if it fits in one slice the file is renamed
    to plain out_path. Returns the list of written paths.

    image_offset: byte offset into src to start from; None (default)
    auto-detects a full OG-Xbox redump image and compresses only its
    game partition, using 0 for everything else (content-agnostic).
    A final partial sector is zero-padded to 2048 bytes.

    Requires python-lz4 (raises Lz4Missing with an install hint).
    """
    if not lz4compat.HAVE_LZ4:
        raise Lz4Missing(lz4compat.INSTALL_HINT)
    out_path = os.fspath(out_path)
    base, ext = os.path.splitext(out_path)
    if ext.lower() != ".cci":
        raise CciError("output path must end in .cci: %s" % out_path)

    stream = src
    own = False
    if isinstance(src, (str, bytes, os.PathLike)):
        stream = open(src, "rb")
        own = True
    try:
        if image_offset is None:
            image_offset = xbox_image_offset(stream)
        stream.seek(0, os.SEEK_END)
        total = stream.tell() - image_offset
        if total <= 0:
            raise CciError("input stream is empty past offset 0x%X"
                           % image_offset)
        stream.seek(image_offset)
        nblocks = (total + BLOCK_SIZE - 1) // BLOCK_SIZE

        slices = [_SliceWriter("%s.1%s" % (base, ext))]
        done_paths = []
        # Batched windows: bulk-read, compress the whole window across
        # cores (order-preserving, byte-identical to sequential), write
        # in order so the split/layout logic is untouched.
        window = 16384                                 # 32 MiB of blocks
        done = 0
        while done < nblocks:
            n = min(window, nblocks - done)
            buf = stream.read(n * BLOCK_SIZE)
            if len(buf) < n * BLOCK_SIZE:
                buf = buf + b"\x00" * (n * BLOCK_SIZE - len(buf))
            raws = [buf[j * BLOCK_SIZE:(j + 1) * BLOCK_SIZE]
                    for j in range(n)]
            comps = lz4compat.compress_batch(raws)
            for raw, comp in zip(raws, comps):
                if split and slices[-1].tell() > split_offset:
                    slices[-1].finalize()
                    done_paths.append(slices[-1].path)
                    slices.append(_SliceWriter("%s.%d%s"
                                               % (base, len(slices) + 1,
                                                  ext)))
                slices[-1].add_block(comp, raw)
            done += n
            if progress:
                progress(done, nblocks)
        slices[-1].finalize()
        done_paths.append(slices[-1].path)
        if progress:
            progress(nblocks, nblocks)
    finally:
        if own:
            stream.close()

    if len(done_paths) == 1:
        os.replace(done_paths[0], out_path)
        return [out_path]
    return done_paths


def info(path):
    """Per-slice header/stat dicts for a CCI path (any slice)."""
    out = []
    for p in find_slices(path):
        with open(p, "rb") as f:
            usize, ioff = _read_header(f, p)
        fsize = os.path.getsize(p)
        out.append({"path": p, "uncompressed_size": usize,
                    "blocks": usize // BLOCK_SIZE, "index_offset": ioff,
                    "file_size": fsize,
                    "ratio": fsize / usize if usize else 0.0})
    return out


def _copy_stream(reader, out_path, label):
    import time
    t0 = time.monotonic()
    done = 0
    with open(out_path, "wb") as out:
        while True:
            chunk = reader.read(4 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if sys.stderr.isatty():
                sys.stderr.write("\r%s: %3d%%" % (label, done * 100 // reader.size))
    elapsed = time.monotonic() - t0
    if sys.stderr.isatty():
        sys.stderr.write("\r")
    print("%s: %d bytes in %.1f s (%.1f MiB/s)"
          % (label, done, elapsed, done / 2**20 / elapsed if elapsed else 0))


def cli(argv=None):
    """Standalone CLI:  cci-convert {info|unpack|pack} ..."""
    import argparse
    import time
    ap = argparse.ArgumentParser(
        prog="xv-cci",
        description="CCI (Compressed Cerbios Image) tool: show slice info, "
                    "unpack to ISO, or pack an ISO into CCI.")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("info", help="print header info for each slice")
    p.add_argument("cci")
    p = sub.add_parser("unpack", help="decode a CCI (any slice) to an ISO")
    p.add_argument("cci")
    p.add_argument("iso")
    p = sub.add_parser("pack", help="compress an ISO into a CCI")
    p.add_argument("iso")
    p.add_argument("cci")
    p.add_argument("--no-split", action="store_true",
                   help="never split, even past the FATX 4 GiB limit")
    p.add_argument("--image-offset", type=lambda s: int(s, 0), default=None,
                   help="byte offset of the data to compress (default: "
                        "auto-detect redump game partition, else 0)")
    a = ap.parse_args(argv)
    try:
        if a.action == "info":
            for s in info(a.cci):
                print("%s:" % s["path"])
                print("  uncompressed : %d bytes (%d blocks)"
                      % (s["uncompressed_size"], s["blocks"]))
                print("  file size    : %d bytes (%.1f%% of original)"
                      % (s["file_size"], 100.0 * s["ratio"]))
                print("  index offset : 0x%X" % s["index_offset"])
        elif a.action == "unpack":
            with CciReader(a.cci) as r:
                _copy_stream(r, a.iso, "unpacked")
        elif a.action == "pack":
            t0 = time.monotonic()

            def prog(done, total):
                if sys.stderr.isatty():
                    sys.stderr.write("\rpacking: %3d%%" % (done * 100 // total))
                    if done == total:
                        sys.stderr.write("\r")

            paths = build_cci(a.iso, a.cci, split=not a.no_split,
                              image_offset=a.image_offset, progress=prog)
            elapsed = time.monotonic() - t0
            insize = os.path.getsize(a.iso)
            outsize = sum(os.path.getsize(p) for p in paths)
            print("packed: %s (%d -> %d bytes, %.1f%%, %.1f s, %.1f MiB/s)"
                  % (" + ".join(paths), insize, outsize,
                     100.0 * outsize / insize, elapsed,
                     insize / 2**20 / elapsed if elapsed else 0))
    except (CciError, Lz4Missing, lz4compat.Lz4Error, OSError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
