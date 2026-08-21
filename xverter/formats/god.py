#!/usr/bin/env python3
"""god2iso.py - Convert an Xbox 360 Games-on-Demand (GoD) container to a
standard XDVDFS ISO, verifying the container's built-in SHA-1 hash tree
during extraction.

Format (constants taken verbatim from iso2god-rs src/god/):
  BLOCK_SIZE          = 0x1000  (4096)
  BLOCKS_PER_SUBPART  = 0xCC    (204 data blocks per sub-hash table)
  SUBPARTS_PER_PART   = 0xCB    (203 subparts per part)
  BLOCKS_PER_PART     = 0xA1C4  (203*204 data blocks per part)

Each DataNNNN part file:
  [master hash table (MHT), 1 block]
  repeat up to 203x:
      [sub hash table, 1 block]     <- SHA-1 of each following data block (20B entries)
      [up to 204 data blocks]       <- raw ISO stream (last block may be partial)

MHT entries: SHA-1 of each sub hash table's full 4096 bytes, in order; for every
part except the last, one extra trailing entry = SHA-1 of the NEXT part's MHT
(4096 bytes) -- the hash chain. The CON/LIVE header stores SHA-1 of part 0's
MHT at 0x37D, sealing the whole tree.

Header fields (big-endian unless noted):
  0x0344 u32   content type (0x7000 = GamesOnDemand, 0x5000 = XboxOriginal)
  0x032C 20B   SHA-1 of header bytes [0x344 .. 0x344+0xACBC)
  0x037D 20B   MHT hash (SHA-1 of part 0's master hash table)
  0x0392 u24   blocks allocated (read big-endian per iso2god-rs; Velocity reads LE — only used for an informational warning here)
  0x03A0 u32   part count (LITTLE-endian, sic)
  0x03A4 u32   total parts size / 0x100
  0x0411 utf16-be  game title

The embedded data stream is the source ISO from the game-partition base onward
(XGD2 base 0xFD90000, XGD3 0x2080000), i.e. the output ISO is a bare XDVDFS
game partition: "MICROSOFT*XBOX*MEDIA" at offset 0x10000 (sector 0x20).

Also contains the native GoD WRITER (build()): the exact inverse of the
reader above, producing containers laid out byte-for-byte like iso2god-rs
output (header template, hash tree, MHT chain, directory naming), but with
correct trim on >4GiB compact images (iso2god 1.8.0 wraps a u32 there and
silently truncates).

Pure stdlib. Streams subpart-at-a-time (~836 KiB).
"""

import argparse
import hashlib
import os
import queue
import shutil
import struct
import sys
import threading
import time

BLOCK_SIZE = 0x1000
WRITE_QUEUE_DEPTH = 4        # subparts in flight to the writer thread
BLOCKS_PER_SUBPART = 0xCC          # 204
SUBPARTS_PER_PART = 0xCB           # 203
BLOCKS_PER_PART = 0xA1C4           # 41412 data blocks
SUBPART_DATA_SIZE = BLOCK_SIZE * BLOCKS_PER_SUBPART        # 0xCC000
SUBPART_SPAN = BLOCK_SIZE + SUBPART_DATA_SIZE              # subtable + data
FULL_PART_SIZE = BLOCK_SIZE * (1 + SUBPARTS_PER_PART * (1 + BLOCKS_PER_SUBPART))  # 0xA290 blocks
HASH_SIZE = 20
SECTOR_SIZE = 0x800
XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"

CONTENT_TYPES = {0x7000: "GamesOnDemand", 0x5000: "XboxOriginal"}


def sha1(data):
    return hashlib.sha1(data).digest()


#: Subparts hashed concurrently before results are drained in order.
#: 16 x 816 KiB of data in flight, which is nothing next to the write
#: queue it feeds.
HASH_BATCH = 16
_HASH_POOL = None


def _free_threaded():
    try:
        import sysconfig
        return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    except Exception:                                 # noqa: BLE001
        return False


def _hash_pool():
    """A pool for subpart hashing, or None to hash on this thread.

    Measured at this writer's real granularity - 204 blocks of 4 KiB per
    subpart, looped, not one convenient giant batch:

      GIL build      2 workers 1.17x, and 4+ is SLOWER than serial
      free-threaded  2 -> 2.04x, 8 -> 5.48x, 16 -> 8.10x

    So with a GIL there is nothing here worth the risk, and this returns
    None: that path stays exactly the serial loop it has always been.
    Without one the same work parallelises properly and is worth taking.
    """
    global _HASH_POOL
    if not _free_threaded():
        return None
    if _HASH_POOL is None:
        import concurrent.futures
        _HASH_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(16, (os.cpu_count() or 4)),
            thread_name_prefix="godsha1")
    return _HASH_POOL


def _check_subtable(computed, stored, data, n_blocks, where):
    """Compare a computed hash table against the stored one.

    The whole table is compared in one go, which is the common case and
    costs nothing. Only when it differs do we walk block by block - the
    slow path exists purely to name the exact block that failed, and a
    mismatch means this container is being rejected anyway."""
    if computed[:n_blocks * HASH_SIZE] == stored[:n_blocks * HASH_SIZE]:
        return
    mv = memoryview(data)
    for bi in range(n_blocks):
        if sha1(mv[bi * BLOCK_SIZE:(bi + 1) * BLOCK_SIZE]) != \
                stored[bi * HASH_SIZE:(bi + 1) * HASH_SIZE]:
            die(where(bi))
    die(where(None))


def _subtable(data):
    """The 4096-byte hash table for one subpart: SHA-1 of every 4 KiB
    block, in order, zero-padded."""
    subtable = bytearray(BLOCK_SIZE)
    mv = memoryview(data)
    n_blocks = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
    for bi in range(n_blocks):
        subtable[bi * HASH_SIZE:(bi + 1) * HASH_SIZE] = \
            sha1(mv[bi * BLOCK_SIZE:(bi + 1) * BLOCK_SIZE])
    return subtable


class GodError(Exception):
    pass


def die(msg):
    raise GodError(msg)


def parse_header(path):
    with open(path, "rb") as f:
        buf = f.read()
    if len(buf) < 0x344 + 0xACBC:
        die("header file too small: %d bytes" % len(buf))
    magic = buf[:4]
    if magic not in (b"CON ", b"LIVE", b"PIRS"):
        die("unknown header magic %r (expected CON /LIVE/PIRS)" % magic)

    h = {
        "magic": magic.decode("ascii").strip(),
        "content_type": struct.unpack_from(">I", buf, 0x344)[0],
        "header_hash": buf[0x32C:0x32C + HASH_SIZE],
        "mht_hash": buf[0x37D:0x37D + HASH_SIZE],
        "blocks_allocated": int.from_bytes(buf[0x392:0x395], "big"),
        "part_count": struct.unpack_from("<I", buf, 0x3A0)[0],  # LE, sic
        "parts_total_size": struct.unpack_from(">I", buf, 0x3A4)[0] * 0x100,
        "media_id": struct.unpack_from(">I", buf, 0x354)[0],
        "title_id": struct.unpack_from(">I", buf, 0x360)[0],
        "disc_number": buf[0x366],
        "disc_count": buf[0x367],
    }
    title_raw = buf[0x411:0x411 + 0x100]
    title = title_raw.decode("utf-16-be", "replace")
    h["title"] = title.split("\x00", 1)[0]

    # Verify the header's own content-region hash (covers 0x344..0xB000).
    computed = sha1(buf[0x344:0x344 + 0xACBC])
    h["header_hash_ok"] = computed == h["header_hash"]
    return h


def expected_layout(part_size, is_last):
    """Return list of data-byte counts per subpart for a part file of part_size."""
    if not is_last and part_size != FULL_PART_SIZE:
        die("non-last part has size %d, expected full part size %d"
            % (part_size, FULL_PART_SIZE))
    remaining = part_size - BLOCK_SIZE  # minus MHT
    if remaining <= 0:
        die("part file too small: %d bytes" % part_size)
    subparts = []
    while remaining > 0:
        if remaining <= BLOCK_SIZE:
            die("dangling %d bytes: sub hash table without data" % remaining)
        data = min(remaining - BLOCK_SIZE, SUBPART_DATA_SIZE)
        subparts.append(data)
        remaining -= BLOCK_SIZE + data
    if len(subparts) > SUBPARTS_PER_PART:
        die("part has %d subparts, max is %d" % (len(subparts), SUBPARTS_PER_PART))
    return subparts


def convert(header_path, out_path, verify_only=False, progress=None):
    t0 = time.monotonic()
    hdr = parse_header(header_path)

    ctype = CONTENT_TYPES.get(hdr["content_type"], "0x%X" % hdr["content_type"])
    print("Header: %s  content-type=%s  title-id=%08X  media-id=%08X  disc %d/%d"
          % (hdr["magic"], ctype, hdr["title_id"], hdr["media_id"],
             hdr["disc_number"], hdr["disc_count"]))
    if hdr["title"]:
        print("Title : %s" % hdr["title"])
    if not hdr["header_hash_ok"]:
        die("header content hash mismatch (0x32C) - header is corrupt or modified")
    print("Header content hash (0x32C): OK")

    data_dir = header_path + ".data"
    if not os.path.isdir(data_dir):
        die("data directory not found: %s" % data_dir)

    part_count = hdr["part_count"]
    part_paths = [os.path.join(data_dir, "Data%04d" % i) for i in range(part_count)]
    for p in part_paths:
        if not os.path.isfile(p):
            die("missing part file: %s" % p)
    extra = sorted(set(os.listdir(data_dir)) - {os.path.basename(p) for p in part_paths})
    if extra:
        die("unexpected extra files in data dir: %s" % ", ".join(extra))

    sizes = [os.path.getsize(p) for p in part_paths]
    container_size = sum(sizes) + os.path.getsize(header_path)
    if hdr["parts_total_size"] != sum(sizes):
        die("header parts_total_size %d != actual %d"
            % (hdr["parts_total_size"], sum(sizes)))
    print("Parts : %d files, %d bytes total (matches header)" % (part_count, sum(sizes)))

    blocks_verified = 0
    subtables_verified = 0
    mht_chain_verified = 0
    bytes_written = 0

    # expected MHT hash for the part about to be read; part 0's comes from the header
    expected_mht_hash = hdr["mht_hash"]

    total_data = sum(sizes)
    pool = _hash_pool()
    vbatch = []
    with open(os.devnull if verify_only else out_path, "wb") as out:
        for pi, ppath in enumerate(part_paths):
            is_last = pi == part_count - 1
            layout = expected_layout(sizes[pi], is_last)
            with open(ppath, "rb") as pf:
                mht = pf.read(BLOCK_SIZE)
                if len(mht) != BLOCK_SIZE:
                    die("short read of MHT in %s" % ppath)

                if sha1(mht) != expected_mht_hash:
                    src = "header mht_hash (0x37D)" if pi == 0 \
                        else "chain entry in part %d's MHT" % (pi - 1)
                    die("master hash table of part %d does not match %s" % (pi, src))
                if pi == 0:
                    print("MHT of part 0 matches header mht_hash: OK")
                else:
                    mht_chain_verified += 1

                n_entries = len(layout) + (0 if is_last else 1)
                if any(b for b in mht[n_entries * HASH_SIZE:]):
                    die("part %d MHT has data beyond expected %d entries"
                        % (pi, n_entries))
                if not is_last:
                    expected_mht_hash = mht[len(layout) * HASH_SIZE:
                                            (len(layout) + 1) * HASH_SIZE]

                for si, data_len in enumerate(layout):
                    subtable = pf.read(BLOCK_SIZE)
                    if len(subtable) != BLOCK_SIZE:
                        die("short read of sub hash table %d in part %d" % (si, pi))
                    if sha1(subtable) != mht[si * HASH_SIZE:(si + 1) * HASH_SIZE]:
                        die("sub hash table %d of part %d does not match MHT entry"
                            % (si, pi))
                    subtables_verified += 1

                    data = pf.read(data_len)
                    if len(data) != data_len:
                        die("short read of subpart %d data in part %d" % (si, pi))

                    n_blocks = (data_len + BLOCK_SIZE - 1) // BLOCK_SIZE
                    if any(b for b in subtable[n_blocks * HASH_SIZE:]):
                        die("subtable %d of part %d has data beyond expected "
                            "%d entries" % (si, pi, n_blocks))

                    def _where(pi=pi, si=si, at=bytes_written):
                        def msg(bi):
                            if bi is None:
                                return ("subpart %d of part %d does not match "
                                        "its hash table" % (si, pi))
                            return ("DATA BLOCK HASH MISMATCH: part %d, "
                                    "subpart %d, block %d (global block %d, "
                                    "iso offset 0x%X)"
                                    % (pi, si, bi,
                                       pi * BLOCKS_PER_PART
                                       + si * BLOCKS_PER_SUBPART + bi,
                                       at + bi * BLOCK_SIZE))
                        return msg

                    if pool is None:
                        _check_subtable(_subtable(data), subtable, data,
                                        n_blocks, _where())
                        blocks_verified += n_blocks
                        out.write(data)
                    else:
                        # Hash several subparts at once, then verify and
                        # write them in order - identical checks, identical
                        # output, just not one at a time.
                        vbatch.append((pool.submit(_subtable, data), subtable,
                                       data, n_blocks, _where()))
                        if len(vbatch) >= HASH_BATCH:
                            for fut, st_, d_, nb_, w_ in vbatch:
                                _check_subtable(fut.result(), st_, d_, nb_, w_)
                                blocks_verified += nb_
                                out.write(d_)
                            del vbatch[:]
                    bytes_written += data_len
                    if progress:
                        progress(bytes_written, total_data)

                for fut, st_, d_, nb_, w_ in vbatch:      # tail of the batch
                    _check_subtable(fut.result(), st_, d_, nb_, w_)
                    blocks_verified += nb_
                    out.write(d_)
                del vbatch[:]

                if pf.read(1):
                    die("trailing bytes after last subpart in part %d" % pi)

    if hdr["blocks_allocated"] != blocks_verified:
        print("WARNING: header blocks_allocated=%d but %d data blocks found"
              % (hdr["blocks_allocated"], blocks_verified))

    if progress:
        progress(bytes_written, bytes_written)
    elapsed = time.monotonic() - t0
    print()
    print("Hash tree verification: PASS")
    print("  data blocks verified     : %d / %d" % (blocks_verified, blocks_verified))
    print("  sub hash tables verified : %d" % subtables_verified)
    print("  MHT chain links verified : %d (+ part 0 MHT vs header)" % mht_chain_verified)
    if verify_only:
        print("Verify-only mode: no ISO written")
    else:
        print("ISO written: %s" % out_path)
    print("  stream size    : %d bytes (%.2f GiB)"
          % (bytes_written, bytes_written / 2**30))
    print("  container size : %d bytes (%.2f GiB)"
          % (container_size, container_size / 2**30))
    print("  elapsed        : %.1f s (%.1f MiB/s)"
          % (elapsed, bytes_written / 2**20 / elapsed if elapsed else 0))
    return out_path


class GodStream:
    """Seekable, read-only, hash-verifying file-like view of a GoD
    container's embedded data stream.

    Lets XDVDFS extraction run DIRECTLY against the container - no
    temporary ISO is ever written. On open, the header self-hash, the
    master-hash-table chain, and every sub hash table are verified
    (a few MB of reads). Each 4 KiB data block is then verified against
    its sub-table entry on first read, so any data an extractor touches
    is exactly as hash-protected as in a full conversion.
    """

    def __init__(self, header_path):
        hdr = parse_header(header_path)
        if not hdr["header_hash_ok"]:
            die("header content hash mismatch (0x32C)")
        data_dir = header_path + ".data"
        part_paths = [os.path.join(data_dir, "Data%04d" % i)
                      for i in range(hdr["part_count"])]
        sizes = [os.path.getsize(p) for p in part_paths]
        if hdr["parts_total_size"] != sum(sizes):
            die("header parts_total_size mismatch")

        self._handles = [open(p, "rb") for p in part_paths]
        self._subtables = []       # flat: per global subpart, 4096B table
        self._part_layout = []     # per part: list of data byte counts
        self._block_base = []      # per global subpart: (part, file_off of first data block)
        expected_mht = hdr["mht_hash"]
        for pi, f in enumerate(self._handles):
            is_last = pi == len(self._handles) - 1
            layout = expected_layout(sizes[pi], is_last)
            f.seek(0)
            mht = f.read(BLOCK_SIZE)
            if sha1(mht) != expected_mht:
                die("master hash table of part %d does not match chain" % pi)
            if not is_last:
                expected_mht = mht[len(layout) * HASH_SIZE:
                                   (len(layout) + 1) * HASH_SIZE]
            off = BLOCK_SIZE
            for si, data_len in enumerate(layout):
                f.seek(off)
                st = f.read(BLOCK_SIZE)
                if sha1(st) != mht[si * HASH_SIZE:(si + 1) * HASH_SIZE]:
                    die("sub hash table %d of part %d does not match MHT"
                        % (si, pi))
                self._subtables.append(st)
                self._block_base.append((pi, off + BLOCK_SIZE, data_len))
                off += BLOCK_SIZE + data_len
            self._part_layout.append(layout)

        # cumulative virtual offsets per subpart
        self._cum = [0]
        for _, _, dlen in self._block_base:
            self._cum.append(self._cum[-1] + dlen)
        self.size = self._cum[-1]
        self._pos = 0
        self._cache_idx = None
        self._cache = None
        self.title = hdr["title"]

    # -- file-like API --
    def read(self, n=-1):
        if n < 0:
            n = self.size - self._pos
        n = max(0, min(n, self.size - self._pos))
        out = bytearray()
        while n > 0:
            chunk = self._read_at(self._pos, n)
            if not chunk:
                break
            out.extend(chunk)
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
        for f in self._handles:
            f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- internals --
    def _subpart_for(self, off):
        import bisect
        i = bisect.bisect_right(self._cum, off) - 1
        return i

    def _read_at(self, off, n):
        """Read up to n bytes at virtual offset off, verified, within one
        subpart's span."""
        si = self._subpart_for(off)
        if si >= len(self._block_base):
            return b""
        local = off - self._cum[si]
        pi, file_off, dlen = self._block_base[si]
        bi = local // BLOCK_SIZE
        block = self._verified_block(si, bi)
        in_block = local % BLOCK_SIZE
        return block[in_block:in_block + n]

    def _verified_block(self, si, bi):
        """One verified 4 KiB block.

        The whole subpart around it is read and verified at once and then
        cached: consumers stream through this in order, so fetching a
        block at a time meant 204 seeks and 204 separate hashes per
        subpart. Every block is still checked against its sub-table entry
        before any of it is handed out - that is the point of this class
        and it does not change."""
        if self._cache_idx == si:
            return self._subpart_block(si, bi)
        pi, file_off, dlen = self._block_base[si]
        f = self._handles[pi]
        f.seek(file_off)
        data = f.read(dlen)
        if len(data) != dlen:
            die("short read in part %d subpart %d" % (pi, si))
        st = self._subtables[si]
        n_blocks = (dlen + BLOCK_SIZE - 1) // BLOCK_SIZE

        def _where(pi=pi, si=si):
            def msg(b):
                if b is None:
                    return ("subpart %d of part %d does not match its hash "
                            "table" % (si, pi))
                return "DATA BLOCK HASH MISMATCH at subpart %d block %d" % (si, b)
            return msg

        _check_subtable(_subtable(data), st, data, n_blocks, _where())
        self._cache_idx = si
        self._cache = data
        return self._subpart_block(si, bi)

    def _subpart_block(self, si, bi):
        off = bi * BLOCK_SIZE
        return self._cache[off:off + BLOCK_SIZE]


def list_xdvdfs(iso_path):
    """Minimal XDVDFS reader: print root directory listing of the ISO."""
    with open(iso_path, "rb") as f:
        f.seek(0x20 * SECTOR_SIZE)
        vd = f.read(SECTOR_SIZE)
        if vd[:20] != XDVDFS_MAGIC:
            die("XDVDFS magic not found at 0x10000 - not a bare game partition?")
        root_sector, root_size = struct.unpack_from("<II", vd, 20)
        f.seek(root_sector * SECTOR_SIZE)
        table = f.read(root_size)

    print()
    print("XDVDFS volume descriptor OK (magic at 0x10000)")
    print("  root dir: sector %d, size %d bytes" % (root_sector, root_size))
    print("Root directory listing:")

    entries = []

    # Iterative in-order traversal with a visited guard: a hostile or
    # deep root table would otherwise recurse until RecursionError (this
    # listing runs at the end of an otherwise successful conversion).
    if len(table) >= 14 and not (
            struct.unpack_from("<HH", table, 0) == (0xFFFF, 0xFFFF)):
        seen = set()
        stack = []
        cur = 0
        descend = True
        while stack or descend:
            while descend:
                if cur in seen or cur + 14 > len(table):
                    descend = False
                    break
                seen.add(cur)
                stack.append(cur)
                left = struct.unpack_from("<HH", table, cur)[0]
                if left not in (0, 0xFFFF):
                    cur = left * 4
                else:
                    descend = False
            if not stack:
                break
            off = stack.pop()
            _l, right = struct.unpack_from("<HH", table, off)
            _start, size = struct.unpack_from("<II", table, off + 4)
            attr = table[off + 12]
            nlen = table[off + 13]
            name = table[off + 14:off + 14 + nlen].decode("ascii", "replace")
            entries.append((name, size, attr))
            if right not in (0, 0xFFFF):
                cur = right * 4
                descend = True
    for name, size, attr in entries:
        kind = "<DIR>" if attr & 0x10 else "%d" % size
        print("  %-32s %12s  attr=0x%02X" % (name, kind, attr))
    return [e[0].lower() for e in entries]


# ---------------------------------------------------------------------------
# Native GoD writer - exact inverse of the reader above. Container layout is
# byte-identical to iso2god-rs output (validated differentially), except that
# trim clamps the data stream at the source's true allocation extent instead
# of iso2god's fill-to-part-boundary (and its 1.8.0 u32 trim wraparound).
# ---------------------------------------------------------------------------

_HEADER_SIZE = 0xB000

# The 278 nonzero bytes below reproduce iso2god-rs's src/god/
# empty_live.bin header template. That file is MIT-licensed,
# Copyright (c) Ilia Pozdnyakov (github.com/iliazeus/iso2god-rs);
# this notice satisfies the MIT license's attribution condition for
# the reproduced data. It is a data template (structural constants,
# checkbox flags and placeholder strings the header format requires),
# byte-for-byte: only 278 of its 0xB000 bytes are nonzero, stored here as
# (offset, hex) spans. SHA-1 of the reconstruction (== of the original file):
# 7dfbbd2bdca918fa754a5c6ee003b097d3523cfb
_HEADER_TEMPLATE_SPANS = (
    (0x000, "4c495645"),                    # "LIVE" magic
    (0x22C, "ffffffffffffffff"),            # license entry: all
    (0x32C, "c06530d687eb083f3055adf6f17f434afffc48950000ad0e"
            "00007000000000020000000000000000000000000000000a"
            "0000000a00000000000001010000000000000000000000000"
            "00000000024050511aa369f3ad52aa7a28ec4853990b5895b"
            "65b52f85405442004e41000000000000000000444e0000000"
            "000000000000001"),
    (0xD12, "5400680069007300200069007300200061006e0020006900"
            "6e007300740061006c006c00650064002000670061006d00"
            "65002e00200054006f00200070006c00610079002c002000"
            "69006e007300650072007400200074006800650020006f00"
            "72006900670069006e0061006c002000670061006d006500"
            "200064006900730063002e"),                          # description
    (0x1714, "384100003841"),               # template thumbnail sizes
)


def _header_template():
    buf = bytearray(_HEADER_SIZE)
    for off, hx in _HEADER_TEMPLATE_SPANS:
        chunk = bytes.fromhex(hx)
        buf[off:off + len(chunk)] = chunk
    return buf


def _xdvdfs():
    try:
        from . import xdvdfs
    except ImportError:
        import xdvdfs
    return xdvdfs


def _read_full(f, n):
    out = bytearray()
    while n > 0:
        chunk = f.read(n)
        if not chunk:
            break
        out += chunk
        n -= len(chunk)
    return bytes(out)


def _read_exact(f, off, n, what):
    f.seek(off)
    data = _read_full(f, n)
    if len(data) != n:
        die("short read of %s" % what)
    return data


def _parse_xex(f, start):
    """Execution-info record (optional-header id 0x40006) of the XEX2 file
    at absolute offset start: media/title ids, platform, executable type,
    disc number/count. XEX2 headers carry no display title."""
    head = _read_exact(f, start, 0x18, "default.xex header")
    if head[:4] != b"XEX2":
        die("default.xex: missing XEX2 magic")
    field_count = struct.unpack_from(">I", head, 0x14)[0]
    if field_count > 0x400:
        die("default.xex: implausible optional-header count %d" % field_count)
    table = _read_exact(f, start + 0x18, field_count * 8,
                        "default.xex optional-header table")
    for i in range(field_count):
        key, value = struct.unpack_from(">II", table, i * 8)
        if key == 0x40006:
            rec = _read_exact(f, start + value, 24,
                              "default.xex execution info")
            media_id, _ver, _base_ver, title_id = \
                struct.unpack_from(">IIII", rec, 0)
            return {"media_id": media_id, "title_id": title_id,
                    "platform": rec[16], "executable_type": rec[17],
                    "disc_number": rec[18], "disc_count": rec[19],
                    "title": ""}
    die("default.xex: no execution-info record (0x40006) in header table")


def _parse_xbe(f, start):
    """Certificate fields of the XBE file at absolute offset start: title id
    (cert+0x8 LE) and display name (cert+0xC, UTF-16-LE, 40 wchars).
    iso2god parity: media id 0, platform/type 0, disc 1/1."""
    head = _read_exact(f, start, 0x11C, "default.xbe header")
    if head[:4] != b"XBEH":
        die("default.xbe: missing XBEH magic")
    base_addr = struct.unpack_from("<I", head, 0x104)[0]
    cert_addr = struct.unpack_from("<I", head, 0x118)[0]
    if cert_addr < base_addr:
        die("default.xbe: certificate address below base address")
    cert = _read_exact(f, start + cert_addr - base_addr, 0x5C,
                       "default.xbe certificate")
    title_id = struct.unpack_from("<I", cert, 0x8)[0]
    title = cert[0xC:0xC + 0x50].decode("utf-16-le", "replace")
    return {"media_id": 0, "title_id": title_id,
            "platform": 0, "executable_type": 0,
            "disc_number": 1, "disc_count": 1,
            "title": title.split("\x00", 1)[0]}


def _title_info(f, base, xd):
    """Locate default.xex / default.xbe in the image root and parse it.
    Returns (content_type, info dict)."""
    f.seek(base + 32 * SECTOR_SIZE + len(XDVDFS_MAGIC))
    root_sector, root_size = struct.unpack("<II", _read_full(f, 8))
    table = xd.read_table(f, base, root_sector, root_size)
    entries = {}
    for name, start, _size, attr in xd.walk_table(table):
        entries[name.lower()] = (start, attr)
    for name, ctype in (("default.xex", 0x7000), ("default.xbe", 0x5000)):
        ent = entries.get(name)
        if ent and not ent[1] & 0x10:
            start = base + ent[0] * SECTOR_SIZE
            info = _parse_xex(f, start) if ctype == 0x7000 \
                else _parse_xbe(f, start)
            return ctype, info
    die("no default.xex or default.xbe in image root - cannot build GoD")


def _stream_allocation_extent(f, base, xd):
    """Last allocated byte inside the game partition, relative to base:
    volume-descriptor region, every directory table, every file extent."""
    f.seek(base + 32 * SECTOR_SIZE + len(XDVDFS_MAGIC))
    root_sector, root_size = struct.unpack("<II", _read_full(f, 8))
    extent = 33 * SECTOR_SIZE
    stack = [(root_sector, root_size)]
    seen = set()
    while stack:
        sector, size = stack.pop()
        if (sector, size) in seen:                 # cycle guard: corrupt tables
            continue
        seen.add((sector, size))
        extent = max(extent, sector * SECTOR_SIZE + size)
        for _name, start, sz, attr in xd.walk_table(
                xd.read_table(f, base, sector, size)):
            if attr & 0x10:
                stack.append((start, sz))
            else:
                extent = max(extent, start * SECTOR_SIZE + sz)
    return extent


def _build_header(path, content_type, info, title, block_count, part_count,
                  parts_total_size, mht_hash):
    buf = _header_template()
    struct.pack_into(">I", buf, 0x344, content_type)
    struct.pack_into(">I", buf, 0x354, info["media_id"])
    struct.pack_into(">I", buf, 0x360, info["title_id"])
    buf[0x364] = info["platform"]
    buf[0x365] = info["executable_type"]
    buf[0x366] = info["disc_number"]
    buf[0x367] = info["disc_count"]
    buf[0x392:0x395] = block_count.to_bytes(3, "big")
    struct.pack_into(">H", buf, 0x395, 0)            # blocks not allocated
    struct.pack_into("<I", buf, 0x3A0, part_count)   # LE, sic
    struct.pack_into(">I", buf, 0x3A4, parts_total_size // 0x100)
    buf[0x37D:0x37D + HASH_SIZE] = mht_hash
    if title:
        enc = title[:0x40].encode("utf-16-be") + b"\x00\x00"
        buf[0x411:0x411 + len(enc)] = enc
        buf[0x1691:0x1691 + len(enc)] = enc
    buf[0x35B] = buf[0x35F] = buf[0x391] = 0         # iso2god finalize parity
    buf[0x32C:0x32C + HASH_SIZE] = sha1(buf[0x344:0x344 + 0xACBC])
    with open(path, "wb") as f:
        f.write(buf)


def build(iso, out_dir, trim=True, game_title=None, progress=None):
    """Build a GoD container from an XDVDFS image (path or seekable
    stream); returns the header file path.

    Output tree (iso2god-rs layout; pkg is the media id for 360 content,
    the title id for Xbox Originals):
      <out_dir>/<TitleID>/<content dir>/<pkg>              header
      <out_dir>/<TitleID>/<content dir>/<pkg>.data/DataNNNN

    Content type and metadata come from the image itself: default.xex
    (360, content dir 00007000) or default.xbe (OG Xbox, 00005000). Full
    redump images are detected via the game-partition base and only the
    game partition is packed. trim=True writes only up to the source's
    allocation extent (computed with our own XDVDFS walk - no u32
    truncation on >4GiB images); trim=False writes everything past the
    partition base. game_title overrides the header title (default: XBE
    certificate name for Originals, empty for 360)."""
    stream = iso
    own = False
    if isinstance(iso, (str, bytes, os.PathLike)):
        stream = open(iso, "rb")
        own = True
    try:
        return _build(stream, out_dir, trim, game_title, progress)
    finally:
        if own:
            stream.close()


def _build(f, out_dir, trim, game_title, progress):
    xd = _xdvdfs()
    try:
        base = xd.find_base(f)
    except xd.XdvdfsError as e:
        die(str(e))
    content_type, info = _title_info(f, base, xd)
    if trim:
        raw_size = _stream_allocation_extent(f, base, xd)
    else:
        f.seek(0, 2)
        raw_size = f.tell() - base
    if raw_size <= 0:
        die("empty data region past partition base 0x%X" % base)
    # round up to a whole sector: extents allocate whole sectors on disc,
    # and the header's parts_total_size field has 0x100 granularity
    data_size = (raw_size + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE
    pad = data_size - raw_size
    block_count = (data_size + BLOCK_SIZE - 1) // BLOCK_SIZE
    if block_count > 0xFFFFFF:
        die("image too large: %d blocks exceeds the header's u24 field"
            % block_count)
    part_count = (block_count + BLOCKS_PER_PART - 1) // BLOCKS_PER_PART

    title = game_title if game_title is not None else info["title"]
    pkg = "%08X" % (info["media_id"] if content_type == 0x7000
                    else info["title_id"])
    content_dir = os.path.join(out_dir, "%08X" % info["title_id"],
                               "%08X" % content_type)
    data_dir = os.path.join(content_dir, pkg + ".data")
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)

    f.seek(base)
    remaining = data_size
    mhts = []                       # per part: concatenated subtable hashes
    part_sizes = []
    pool = _hash_pool()
    for pi in range(part_count):
        mht = bytearray()
        with open(os.path.join(data_dir, "Data%04d" % pi), "wb") as pf:
            pf.write(b"\x00" * BLOCK_SIZE)          # MHT placeholder
            # Hand the writes to a thread so the next subpart is read and
            # hashed while this one drains to disk. Whether the hashing
            # itself is also spread across threads depends on the
            # interpreter - see _hash_pool().
            wq = queue.Queue(WRITE_QUEUE_DEPTH)
            werr = []

            def _drain(fh=pf, q=wq, err=werr):
                while True:
                    item = q.get()
                    if item is None:
                        return
                    try:
                        fh.write(item[0])
                        fh.write(item[1])
                    except BaseException as exc:     # surface on the main thread
                        err.append(exc)
                        return

            wt = threading.Thread(target=_drain, daemon=True)
            wt.start()

            def _put(item):
                """Queue a write, but abandon it if the writer thread has
                died. The queue is bounded, so without this a dead
                consumer (a mid-part write failure - disk full) would
                block the producer forever on a full queue."""
                while not werr:
                    try:
                        wq.put(item, timeout=0.25)
                        return True
                    except queue.Full:
                        continue
                return False

            batch = []
            try:
                for _si in range(SUBPARTS_PER_PART):
                    if remaining <= 0:
                        break
                    if werr:
                        break
                    want = min(SUBPART_DATA_SIZE, remaining)
                    data = _read_full(f, want)
                    if len(data) != want:
                        # only the sector-rounding tail may be missing from
                        # the stream; zero-fill it, anything more is truncation
                        if remaining - len(data) > pad:
                            die("source image truncated: %d bytes missing "
                                "before allocation extent"
                                % (remaining - len(data) - pad))
                        data += b"\x00" * (want - len(data))
                    remaining -= len(data)
                    if pool is None:
                        # neither buffer is touched again after queueing
                        subtable = _subtable(data)
                        if not _put((subtable, data)):
                            break
                        mht += sha1(subtable)
                    else:
                        # Whole subparts hash in parallel and are emitted
                        # strictly in submission order, so the tables, the
                        # MHT and the file are exactly what the serial
                        # path produces.
                        batch.append((pool.submit(_subtable, data), data))
                        if len(batch) >= HASH_BATCH:
                            for fut, buf_ in batch:
                                st = fut.result()
                                if not _put((st, buf_)):
                                    break
                                mht += sha1(st)
                            del batch[:]
                    if progress:
                        progress(data_size - remaining, data_size)
                for fut, buf_ in batch:            # tail of the last batch
                    st = fut.result()
                    if not _put((st, buf_)):
                        break
                    mht += sha1(st)
                del batch[:]
            finally:
                for fut, _buf in batch:            # never leave futures running
                    try:
                        fut.result()
                    except BaseException:          # noqa: BLE001
                        pass
                wq.put(None)
                wt.join()
            if werr:
                raise werr[0]
            # every queued write has landed, so tell() is the true size
            part_sizes.append(pf.tell())
        mhts.append(mht)

    # backward MHT chain: every part's MHT gains one trailing entry, the
    # SHA-1 of the NEXT part's finished 4096-byte MHT; part 0's digest
    # seals the tree in the header (0x37D)
    digest = None
    tables = [None] * part_count
    for pi in range(part_count - 1, -1, -1):
        m = bytearray(BLOCK_SIZE)
        m[:len(mhts[pi])] = mhts[pi]
        if digest is not None:
            m[len(mhts[pi]):len(mhts[pi]) + HASH_SIZE] = digest
        digest = sha1(m)
        tables[pi] = m
    for pi in range(part_count):
        with open(os.path.join(data_dir, "Data%04d" % pi), "r+b") as pf:
            pf.write(tables[pi])

    header_path = os.path.join(content_dir, pkg)
    _build_header(header_path, content_type, info, title, block_count,
                  part_count, sum(part_sizes), digest)
    return header_path


__version__ = "1.0.0"


def main():
    ap = argparse.ArgumentParser(
        prog="xv-god",
        description="Convert an Xbox 360 Games-on-Demand container to an "
                    "XDVDFS ISO, verifying the container's SHA-1 hash tree "
                    "block-by-block during extraction.",
        epilog="Exit status: 0 on success, 1 on any verification or format "
               "error (the error names the exact block/table that failed).")
    ap.add_argument("header", help="path to the GoD header file "
                                   "(the <header>.data directory must sit next to it)")
    ap.add_argument("output", nargs="?",
                    help="output .iso path (omit with --verify-only)")
    ap.add_argument("--verify-only", action="store_true",
                    help="verify the full hash tree without writing an ISO "
                         "(GoD integrity check)")
    ap.add_argument("--no-list", action="store_true",
                    help="skip the XDVDFS root-directory listing after conversion")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args()

    if not args.verify_only and not args.output:
        ap.error("output path required unless --verify-only is given")

    convert(args.header, args.output, verify_only=args.verify_only)
    if not args.verify_only and not args.no_list:
        names = list_xdvdfs(args.output)
        if "default.xex" in names:
            print("default.xex present in root: OK")
        else:
            die("default.xex NOT found in root directory")


def cli():
    try:
        return main()
    except GodError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
