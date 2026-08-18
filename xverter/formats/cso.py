"""CSO (Xbox dialect: "CISO" v2, LZ4) reading and writing.

This is the Xbox scene's LZ4 CISO as produced by MakeMHz's ciso.py
(stellar) and consumed by Cerbios/XGDTool - NOT the PSP CSO v1
(deflate, inverted plain-block bit), which is rejected with a clear
error. The reader/writer are content-agnostic: they wrap any 2048-byte
-sector ISO stream (Xbox 360 images in CSO are cursed but supported).

All code here is original. Lineage, stated fully: an earlier xverter
version adapted the writer from stellar-cso's ciso.py (BSD-3-Clause,
Copyright 2018 David O'Rourke, Copyright 2022 MakeMHz LLC - notice
retained here as a matter of course); the current writer is a
from-scratch restructure validated BYTE-IDENTICAL to both that
adaptation and stellar-cso's own output, so it inherits the hardware
validation that implementation earned. Format facts additionally
cross-checked against maxcso's CSO documentation. (An independent LZ4 frame block
is [u32 size][raw HC block], which is why lz4.block reproduces
lz4.frame's bytes exactly.)

File layout (all little-endian, struct '<LLQLBBxx'):

  header, 24 bytes at 0:
    char[4] magic "CISO"; u32 header_size 0x18;
    u64 uncompressed_size (TOTAL decoded bytes, across all parts);
    u32 block_size 2048; u8 version 2; u8 index_alignment 2; 2 pad
  index at 0x18: N+1 u32 entries, N = uncompressed_size / 2048
    entry = (file_offset >> 2) | (0x80000000 if LZ4-compressed)
    terminator entry: top bit clear (end-of-data marker, unused here)
  data after the index. Every block starts 4-aligned (up to 3 zero pad
  bytes between blocks), so when reading a compressed block trust its
  own u32 size prefix, never the index delta.

Block storage:
  compressed: [u32 LE lz4_block_size][raw LZ4 block]  (an lz4.frame
              block; a set top bit in the prefix would mean the frame
              stored the payload uncompressed - handled, never emitted)
  raw:        the 2048 sector verbatim (chosen when prefixed size + 12
              >= 2048; index top bit clear)
Each part's EOF is zero-padded to a multiple of 0x400 (stellar quirk
kept for byte-identity: a full extra 0x400 is appended when already
aligned).

Split images (FATX 4 GiB limit): a new part starts once the write
position passes 0xFFBF6000. The single header and the whole index live
in part 1; part 2 is bare block data whose offsets RESET to 0 - the
reader detects the wrap by the monotonicity break in the index.
stellar names even unsplit output "Game.1.cso"; plain "Game.cso" is
accepted too (this writer renames a single part to the plain name).

Reading needs no third-party packages (pure-Python LZ4 fallback);
writing requires python-lz4 (installed with xverter; pyz users: pip install lz4).
"""

import os
import queue
import struct
import threading
import sys

from . import lz4compat
from .lz4compat import Lz4Missing  # noqa: F401  (re-exported convenience)

MAGIC = b"CISO"
HEADER = struct.Struct("<4sLQLBB2x")
HEADER_SIZE = 0x18
BLOCK_SIZE = 2048
VERSION = 2
INDEX_ALIGNMENT = 2
ALIGN_B = 1 << INDEX_ALIGNMENT
SPLIT_OFFSET = 0xFFBF6000  # start a new part once write_pos exceeds this
FILE_MODULUS = 0x400       # each part's EOF is zero-padded to this

XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"
REDUMP_VD_OFFSET = 0x18310000
REDUMP_IMAGE_OFFSET = 0x18300000

assert HEADER.size == HEADER_SIZE


class CsoError(Exception):
    pass


def find_slices(path):
    """Return the ordered part list for a CSO path.

    "Game.cso" -> [that file]; "Game.1.cso" (or .2 ...) -> every
    sibling "Game.<digit>.cso", sorted by part number.
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
            raise CsoError("no parts found matching %s" % path)
        nums = [n for n, _ in found]
        if nums != list(range(nums[0], nums[0] + len(nums))):
            raise CsoError("non-contiguous part numbers: %s" % nums)
        return [p for _, p in found]
    if not os.path.exists(path):
        # "Game.cso" may name a split set that only exists as
        # "Game.1.cso", "Game.2.cso", ... - resolve via the first part.
        first = "%s.1%s" % (base, ext)
        if os.path.exists(first):
            return find_slices(first)
    return [path]


def is_cso(path):
    """True if path (a .cso file or the first part of a set) is an
    Xbox-dialect (v2) CSO."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_SIZE)
    except OSError:
        return False
    if len(head) < HEADER_SIZE:
        return False
    magic, hsize, _, bsize, ver, align = HEADER.unpack(head)
    return (magic == MAGIC and hsize == HEADER_SIZE and bsize == BLOCK_SIZE
            and ver == VERSION and align == INDEX_ALIGNMENT)


def _read_header(f, path):
    f.seek(0)
    head = f.read(HEADER_SIZE)
    if len(head) < HEADER_SIZE:
        raise CsoError("%s: file too small for CSO header" % path)
    magic, hsize, usize, bsize, ver, align = HEADER.unpack(head)
    if magic != MAGIC:
        raise CsoError("%s: bad magic %r (expected CISO)" % (path, magic))
    if ver != VERSION:
        raise CsoError("%s: CISO version %d is not the Xbox dialect "
                       "(v2, LZ4); PSP/v1 CSO is unsupported" % (path, ver))
    if hsize != HEADER_SIZE:
        raise CsoError("%s: bad header_size %d" % (path, hsize))
    if bsize != BLOCK_SIZE:
        raise CsoError("%s: unsupported block_size %d" % (path, bsize))
    if align != INDEX_ALIGNMENT:
        raise CsoError("%s: unsupported index_alignment %d" % (path, align))
    return usize


class CsoReader:
    """Seekable, read-only file-like view of a CSO image's decoded ISO
    stream (same shape as god.GodStream: read/seek/tell/size), handling
    split sets with their offset-reset second part. Pass any part or
    the plain file.
    """

    def __init__(self, path):
        self.slice_paths = find_slices(path)
        self._files = []
        try:
            for p in self.slice_paths:
                self._files.append(open(p, "rb"))
            f0 = self._files[0]
            usize = _read_header(f0, self.slice_paths[0])
            nblocks = usize // BLOCK_SIZE
            if usize % BLOCK_SIZE:
                raise CsoError("%s: uncompressed_size %d not a multiple "
                               "of %d" % (self.slice_paths[0], usize,
                                          BLOCK_SIZE))
            raw = f0.read((nblocks + 1) * 4)  # index follows the header
            if len(raw) != (nblocks + 1) * 4:
                raise CsoError("%s: truncated index" % self.slice_paths[0])
            vals = struct.unpack("<%dI" % (nblocks + 1), raw)
            if vals[-1] & 0x80000000:
                raise CsoError("%s: index terminator has compressed bit set"
                               % self.slice_paths[0])
            self._offs = [(v & 0x7FFFFFFF) << INDEX_ALIGNMENT for v in vals]
            self._comp = [bool(v & 0x80000000) for v in vals]
            # Split parts restart their offsets at 0: a monotonicity
            # break in the block entries marks the switch to the next
            # file (the terminator is excluded - it may legally be a
            # small part-2 offset after a big part-1 one).
            self._fidx = [0] * nblocks
            fidx = 0
            for i in range(1, nblocks):
                if self._offs[i] < self._offs[i - 1]:
                    fidx += 1
                self._fidx[i] = fidx
            if fidx + 1 != len(self._files):
                raise CsoError(
                    "index spans %d part(s) but %d file(s) found: %s"
                    % (fidx + 1, len(self._files),
                       ", ".join(self.slice_paths)))
            self._nblocks = nblocks
        except Exception:
            self.close()
            raise
        self.block_size = BLOCK_SIZE
        self.size = usize
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
    def _read_block(self, idx):
        if idx == self._cache_idx:
            return self._cache
        if idx >= self._nblocks:
            raise CsoError("block %d out of range" % idx)
        f = self._files[self._fidx[idx]]
        f.seek(self._offs[idx])
        if self._comp[idx]:
            prefix = f.read(4)
            if len(prefix) != 4:
                raise CsoError("short read of block %d size prefix" % idx)
            (word,) = struct.unpack("<I", prefix)
            length = word & 0x7FFFFFFF
            if length == 0 or length > BLOCK_SIZE + 16:
                raise CsoError("block %d: implausible stored size %d"
                               % (idx, length))
            data = f.read(length)
            if len(data) != length:
                raise CsoError("short read of block %d" % idx)
            if word & 0x80000000:
                # lz4-frame "uncompressed block" flag inside the prefix
                if length != BLOCK_SIZE:
                    raise CsoError("block %d: raw frame block of %d bytes"
                                   % (idx, length))
                block = data
            else:
                block = lz4compat.decode_block(data, BLOCK_SIZE)
        else:
            block = f.read(BLOCK_SIZE)
            if len(block) != BLOCK_SIZE:
                raise CsoError("short read of block %d" % idx)
        self._cache_idx = idx
        self._cache = block
        return block


def xbox_image_offset(stream):
    """Byte offset of the game partition inside stream: 0x18300000 for a
    full OG-Xbox redump image, else 0 (content-agnostic pass-through)."""
    pos = stream.tell()
    try:
        stream.seek(REDUMP_VD_OFFSET)
        if stream.read(len(XDVDFS_MAGIC)) == XDVDFS_MAGIC:
            return REDUMP_IMAGE_OFFSET
        return 0
    finally:
        stream.seek(pos)


class _PartWriter:
    """One in-progress CSO part. Part 1 carries the header and (later)
    the index; parts 2+ are bare block streams whose stored offsets
    restart at zero - the split convention CsoReader mirrors."""

    def __init__(self, path, header=b""):
        self.path = path
        self.f = open(path, "wb")
        self.pos = 0
        if header:
            self.f.write(header)
            self.pos = len(header)

    def align(self):
        gap = self.pos & (ALIGN_B - 1)
        if gap:
            self.f.write(b"\x00" * (ALIGN_B - gap))
            self.pos += ALIGN_B - gap

    def put(self, payload):
        self.f.write(payload)
        self.pos += len(payload)

    def rewrite_index(self, index):
        self.f.seek(HEADER_SIZE)
        self.f.write(struct.pack("<%dI" % len(index), *index))

    def finish(self):
        # Every part's EOF is zero-padded to the next 0x400 boundary -
        # unconditionally, so an already-aligned part gains a full 0x400
        # (dialect behavior, load-bearing for byte-level compatibility).
        # Seek explicitly: rewrite_index leaves the fd mid-file.
        self.f.seek(0, os.SEEK_END)
        self.f.write(b"\x00" * (FILE_MODULUS - (self.pos & (FILE_MODULUS - 1))))
        self.f.close()


def _stream_comps(raws):
    """Flatten compress_batch_iter into a per-block iterator, so each
    block is written as its run lands rather than after the window."""
    for run in lz4compat.compress_batch_iter(raws):
        for c in run:
            yield c


def build_cso(src, out_path, split=True, split_offset=SPLIT_OFFSET,
              image_offset=None, progress=None):
    """Compress an ISO stream (path or seekable file-like) to CSO
    (Xbox dialect, CISO v2): per-2048-sector records that are either the
    raw sector or [u32 LE compressed size][raw LZ4 block], 4-aligned,
    with the block index in part 1. Output is validated byte-identical
    to the ecosystem's established writer for the same input.

    out_path should end in .cso. Output is written as Name.1.cso (and
    Name.2.cso when split); a single part is renamed to plain out_path.
    Returns the list of written paths.

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
    if ext.lower() != ".cso":
        raise CsoError("output path must end in .cso: %s" % out_path)

    stream = src
    own = False
    if isinstance(src, (str, bytes, os.PathLike)):
        stream = open(src, "rb")
        own = True
    parts = []
    try:
        if image_offset is None:
            image_offset = xbox_image_offset(stream)
        stream.seek(0, os.SEEK_END)
        total = stream.tell() - image_offset
        if total <= 0:
            raise CsoError("input stream is empty past offset 0x%X"
                           % image_offset)
        stream.seek(image_offset)
        nblocks = (total + BLOCK_SIZE - 1) // BLOCK_SIZE
        usize = nblocks * BLOCK_SIZE if total % BLOCK_SIZE else total

        index = [0] * (nblocks + 1)
        parts.append(_PartWriter(
            "%s.1%s" % (base, ext),
            header=HEADER.pack(MAGIC, HEADER_SIZE, usize, BLOCK_SIZE,
                               VERSION, INDEX_ALIGNMENT)
            + b"\x00" * (4 * len(index))))    # index placeholder

        # Batched windows: bulk-read, compress across cores (order-
        # preserving, byte-identical to sequential), write in order.
        window = 16384                                 # 32 MiB of blocks
        i = 0
        # Reader thread holds the next window, so the source read
        # overlaps compression of the current one.
        rq = queue.Queue(2)
        rerr = []

        def _fill():
            left = nblocks
            try:
                while left > 0:
                    k = min(window, left)
                    b = stream.read(k * BLOCK_SIZE)
                    if len(b) < k * BLOCK_SIZE:
                        b = b + b"\x00" * (k * BLOCK_SIZE - len(b))
                    rq.put((k, b))
                    left -= k
            except BaseException as exc:                # noqa: BLE001
                rerr.append(exc)
            finally:
                rq.put(None)

        rt = threading.Thread(target=_fill, daemon=True)
        rt.start()
        while i < nblocks:
            item = rq.get()
            if item is None:
                break
            if rerr:
                raise rerr[0]
            n, buf = item
            mv = memoryview(buf)
            raws = [mv[j * BLOCK_SIZE:(j + 1) * BLOCK_SIZE]
                    for j in range(n)]
            for raw, comp in zip(raws, _stream_comps(raws)):
                p = parts[-1]
                if split and p.pos > split_offset:
                    p = _PartWriter("%s.%d%s" % (base, len(parts) + 1, ext))
                    parts.append(p)
                p.align()
                index[i] = p.pos >> INDEX_ALIGNMENT
                # A record only earns compressed storage when it beats
                # the raw sector with room to spare (16 bytes: prefix
                # plus the dialect's safety margin).
                if len(comp) + 16 < BLOCK_SIZE:
                    index[i] |= 0x80000000
                    p.put(struct.pack("<I", len(comp)))
                    p.put(comp)
                else:
                    p.put(raw)
                i += 1
            if progress:
                progress(i, nblocks)
        rt.join()
        if rerr:
            raise rerr[0]
        index[-1] = parts[-1].pos >> INDEX_ALIGNMENT
        parts[0].rewrite_index(index)
        for p in parts:
            p.finish()
        if progress:
            progress(nblocks, nblocks)
    except Exception:
        for p in parts:
            p.f.close()
        raise
    finally:
        if own:
            stream.close()

    paths = [p.path for p in parts]
    if len(paths) == 1:
        os.replace(paths[0], out_path)
        return [out_path]
    return paths


def info(path):
    """Header/stat dict for a CSO set (pass any part)."""
    paths = find_slices(path)
    with open(paths[0], "rb") as f:
        usize = _read_header(f, paths[0])
    fsizes = [os.path.getsize(p) for p in paths]
    return {"paths": paths, "uncompressed_size": usize,
            "blocks": usize // BLOCK_SIZE, "file_sizes": fsizes,
            "ratio": sum(fsizes) / usize if usize else 0.0}


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
    """Standalone CLI:  cso-convert {info|unpack|pack} ..."""
    import argparse
    import time
    ap = argparse.ArgumentParser(
        prog="xv-cso",
        description="CSO (Xbox dialect CISO v2, LZ4) tool: show info, "
                    "unpack to ISO, or pack an ISO into CSO.")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("info", help="print header info for a CSO set")
    p.add_argument("cso")
    p = sub.add_parser("unpack", help="decode a CSO (any part) to an ISO")
    p.add_argument("cso")
    p.add_argument("iso")
    p = sub.add_parser("pack", help="compress an ISO into a CSO")
    p.add_argument("iso")
    p.add_argument("cso")
    p.add_argument("--no-split", action="store_true",
                   help="never split, even past the FATX 4 GiB limit")
    p.add_argument("--image-offset", type=lambda s: int(s, 0), default=None,
                   help="byte offset of the data to compress (default: "
                        "auto-detect redump game partition, else 0)")
    a = ap.parse_args(argv)
    try:
        if a.action == "info":
            s = info(a.cso)
            print("parts        : %s" % ", ".join(s["paths"]))
            print("uncompressed : %d bytes (%d blocks)"
                  % (s["uncompressed_size"], s["blocks"]))
            print("file size    : %d bytes (%.1f%% of original)"
                  % (sum(s["file_sizes"]), 100.0 * s["ratio"]))
        elif a.action == "unpack":
            with CsoReader(a.cso) as r:
                _copy_stream(r, a.iso, "unpacked")
        elif a.action == "pack":
            t0 = time.monotonic()

            def prog(done, total):
                if sys.stderr.isatty():
                    sys.stderr.write("\rpacking: %3d%%" % (done * 100 // total))
                    if done == total:
                        sys.stderr.write("\r")

            paths = build_cso(a.iso, a.cso, split=not a.no_split,
                              image_offset=a.image_offset, progress=prog)
            elapsed = time.monotonic() - t0
            insize = os.path.getsize(a.iso)
            outsize = sum(os.path.getsize(p) for p in paths)
            print("packed: %s (%d -> %d bytes, %.1f%%, %.1f s, %.1f MiB/s)"
                  % (" + ".join(paths), insize, outsize,
                     100.0 * outsize / insize, elapsed,
                     insize / 2**20 / elapsed if elapsed else 0))
    except (CsoError, Lz4Missing, lz4compat.Lz4Error, OSError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
