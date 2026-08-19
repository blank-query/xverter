#!/usr/bin/env python3
"""xiso_extract.py - Extract an XDVDFS (Xbox / Xbox 360 disc) image to a
directory. Pure Python 3 stdlib.

Handles:
  * bare game partitions (magic at 0x10000) - e.g. god2iso.py output
  * full XGD1/XGD2/XGD3 redump-style images (auto-detects the game
    partition base offset)

XDVDFS layout:
  Volume descriptor at sector 32 (0x10000 from partition base):
      20B  magic "MICROSOFT*XBOX*MEDIA"
      u32  root directory table sector (LE)
      u32  root directory table size  (LE)
  Directory tables are binary trees of 4-byte-aligned entries:
      u16 left child offset  (in dwords/4-byte units from table start; 0 or 0xFFFF = none)
      u16 right child offset
      u32 start sector (LE)
      u32 file size    (LE)
      u8  attributes   (0x10 = directory)
      u8  name length
      name (Windows-1252)
  An empty directory is a table starting with 0xFFFF 0xFFFF.
"""

import argparse
import os
import struct
import sys

SECTOR = 0x800
MAGIC = b"MICROSOFT*XBOX*MEDIA"
# Known game-partition base offsets for full disc images.
PARTITION_BASES = (0x0, 0x2080000, 0xFD90000, 0x18300000)  # bare, XGD3, XGD2, XGD1
ATTR_DIR = 0x10
CHUNK = 1 << 20
SENDFILE_CHUNK = 1 << 24
#: In-kernel copy paths, best first. copy_file_range can reflink on
#: btrfs/XFS; sendfile always copies. Both beat moving bytes through
#: Python, and the plain loop remains the fallback.
_KCOPY = tuple(fn for fn in (getattr(os, "copy_file_range", None),
                             getattr(os, "sendfile", None))
               if fn is not None) if sys.platform != "win32" else ()


class XdvdfsError(Exception):
    pass


import contextlib


@contextlib.contextmanager
def _as_file(src):
    """Accept a path or a seekable file-like object (e.g. god.GodStream)."""
    if hasattr(src, "read") and hasattr(src, "seek"):
        yield src
    else:
        f = open(src, "rb")
        try:
            yield f
        finally:
            f.close()


def die(msg):
    raise XdvdfsError(msg)


def find_base(f):
    for base in PARTITION_BASES:
        f.seek(base + 32 * SECTOR)
        if f.read(len(MAGIC)) == MAGIC:
            return base
    die("XDVDFS magic not found at any known partition base")


def read_table(f, base, sector, size):
    if size == 0:
        return b""
    f.seek(base + sector * SECTOR)
    table = f.read(size)
    if len(table) != size:
        die("short read of directory table at sector %d" % sector)
    return table


def walk_table(table):
    """Return [(name, start_sector, size, attr), ...] via in-order traversal.

    The root entry sits at byte offset 0; child pointers are dword offsets (4-byte units)
    — 0 and 0xFFFF both mean "no child" (0 can never be a real child,
    the root occupies it). An empty directory is a table starting FFFF FFFF.
    """
    entries = []
    if len(table) < 14 or table[:4] == b"\xff\xff\xff\xff":
        return entries
    seen = set()

    def visit(off):
        if off in seen:
            die("directory table cycle at offset %d" % off)
        seen.add(off)
        if off + 14 > len(table):
            die("directory entry at offset %d overruns table" % off)
        left, right = struct.unpack_from("<HH", table, off)
        start, size = struct.unpack_from("<II", table, off + 4)
        attr = table[off + 12]
        nlen = table[off + 13]
        name_raw = table[off + 14:off + 14 + nlen]
        if len(name_raw) != nlen:
            die("directory entry name at offset %d overruns table" % off)
        if left not in (0, 0xFFFF):
            visit(left * 4)
        entries.append((name_raw.decode("cp1252", "replace"), start, size, attr))
        if right not in (0, 0xFFFF):
            visit(right * 4)

    visit(0)
    return entries


def allocation_extent(f, base):
    """Last allocated byte inside the game partition, relative to base:
    the volume-descriptor region, every directory table, every file
    extent. The smallest size a complete image can have.

    (builders._allocation_extent computes the same thing from a path,
    and god._stream_allocation_extent from a mid-build stream.)"""
    f.seek(base + 32 * SECTOR + len(MAGIC))
    root_sector, root_size = struct.unpack("<II", f.read(8))
    extent = 33 * SECTOR                       # volume-descriptor region
    stack = [(root_sector, root_size)]
    seen = set()
    while stack:
        sector, size = stack.pop()
        if (sector, size) in seen:             # cycle guard: corrupt tables
            continue
        seen.add((sector, size))
        extent = max(extent, sector * SECTOR + size)
        for _name, start, sz, attr in walk_table(
                read_table(f, base, sector, size)):
            if attr & ATTR_DIR:
                stack.append((start, sz))
            else:
                extent = max(extent, start * SECTOR + sz)
    return extent


def validate_image(iso_path):
    """Refuse an image whose own directory tables describe more data than
    the file actually contains.

    A raw ISO carries no internal checksum, so nothing else catches a
    truncated one: the block-level wrappers (CCI/CSO) are deliberately
    content-agnostic and will compress whatever bytes exist, producing a
    container that round-trips perfectly and is silently missing game
    data - which is why this runs over CCI and CSO sources too, and not
    only over raw images. They carry no integrity of their own at all.

    GoD, zar and STFS do carry their own (hash tree, SHA-256, hash
    chain), but those prove the storage is intact, not that the image
    inside it coheres: a container built faithfully from a truncated
    dump passes its own checks. So this runs over every source that is
    an image, and the self-checking formats simply pass it quickly.

    Takes a path or an open seekable image, so a source that is an
    image without being a file - a compressed container, a GoD - can be
    checked without being written out first.

    Returns the allocation extent. Raises XdvdfsError if the image is
    not XDVDFS or is short."""
    with _as_file(iso_path) as f:
        size = getattr(f, "size", None)
        if size is None:
            pos = f.tell()
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(pos)
        base = find_base(f)
        extent = allocation_extent(f, base)
    need = base + extent
    if need > size:
        die("truncated image: its directory tables describe %d bytes "
            "(partition base 0x%X plus %d of content) but the file holds "
            "%d - %d bytes are missing"
            % (need, base, extent, size, need - size))
    return extent


def safe_name(name):
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        die("refusing unsafe filename %r" % name)
    return name


def _extract_parallel(f, base, root_sector, root_size, out_dir, quiet,
                      manifest, progress, opener, grand):
    """Same extraction, a few files at a time on their own readers."""
    import concurrent.futures
    import hashlib
    import threading

    jobs = []
    dirs = 0
    stack = [(read_table(f, base, root_sector, root_size), out_dir)]
    while stack:
        table, path = stack.pop()
        for name, start, size, attr in walk_table(table):
            name = safe_name(name)
            dest = os.path.join(path, name)
            if attr & ATTR_DIR:
                os.makedirs(dest, exist_ok=True)
                dirs += 1
                stack.append((read_table(f, base, start, size), dest))
            else:
                jobs.append((start, size, dest))

    lock = threading.Lock()
    done = [0]
    local = threading.local()

    def reader():
        r = getattr(local, "r", None)
        if r is None:
            r = local.r = opener()
        return r

    def one(job):
        start, size, dest = job
        r = reader()
        r.seek(base + start * SECTOR)
        h = hashlib.sha1() if manifest is not None else None
        remaining = size
        with open(dest, "wb") as o:
            while remaining > 0:
                chunk = r.read(min(CHUNK, remaining))
                if not chunk:
                    die("unexpected EOF extracting %s (missing %d of %d "
                        "bytes) - truncated image?" % (dest, remaining, size))
                o.write(chunk)
                if h is not None:
                    h.update(chunk)
                remaining -= len(chunk)
                if progress:
                    with lock:
                        done[0] += len(chunk)
                        progress(done[0], grand)
        return dest, (h.hexdigest() if h is not None else None), size

    opened = []
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=EXTRACT_WORKERS,
                thread_name_prefix="xiso") as ex:
            results = list(ex.map(one, jobs))
    finally:
        for r in opened:
            try:
                r.close()
            except Exception:                         # noqa: BLE001
                pass

    total = 0
    for dest, digest, size in results:
        if manifest is not None:
            manifest[os.path.relpath(dest, out_dir).replace(os.sep, "/")] = digest
        total += size
        if not quiet:
            print("  %s (%d bytes)" % (os.path.relpath(dest, out_dir), size))
    print("extracted %d files, %d dirs, %d bytes total"
          % (len(jobs), dirs, total))
    return len(jobs), total


#: Files extracted at once when a second reader can be opened. Measured
#: on NVMe: 2 workers is 1.35-1.65x over serial on both interpreters,
#: and every count above 2 is slower again - past that the limit is the
#: storage, not the CPU.
EXTRACT_WORKERS = 2


def extract(iso_path, out_dir, quiet=False, manifest=None, progress=None,
            opener=None):
    """Extract to out_dir, streaming each file in 1MiB chunks (memory
    stays O(chunk) regardless of file size). If manifest is a dict,
    per-file SHA-1 hashes are computed inline during the same pass.

    `opener` is a zero-argument callable returning a fresh reader for
    the same source. Given one, a couple of files are extracted at a
    time on separate readers. The files are independent, so this is a
    scheduling change only: same bytes out, same manifest."""
    import hashlib
    files = dirs = 0
    total = 0
    with _as_file(iso_path) as f:
        base = find_base(f)
        f.seek(base + 32 * SECTOR + len(MAGIC))
        root_sector, root_size = struct.unpack("<II", f.read(8))
        if not quiet:
            print("partition base 0x%X, root table: sector %d, %d bytes"
                  % (base, root_sector, root_size))

        grand = None
        if progress:
            grand = 0
            tstack = [read_table(f, base, root_sector, root_size)]
            while tstack:
                for _n, st, sz, at in walk_table(tstack.pop()):
                    if at & ATTR_DIR:
                        tstack.append(read_table(f, base, st, sz))
                    else:
                        grand += sz
        os.makedirs(out_dir, exist_ok=True)
        if opener is not None and EXTRACT_WORKERS > 1:
            return _extract_parallel(f, base, root_sector, root_size,
                                     out_dir, quiet, manifest, progress,
                                     opener, grand)
        stack = [(read_table(f, base, root_sector, root_size), out_dir)]
        while stack:
            table, path = stack.pop()
            for name, start, size, attr in walk_table(table):
                name = safe_name(name)
                dest = os.path.join(path, name)
                if attr & ATTR_DIR:
                    os.makedirs(dest, exist_ok=True)
                    dirs += 1
                    stack.append((read_table(f, base, start, size), dest))
                else:
                    f.seek(base + start * SECTOR)
                    remaining = size
                    h = hashlib.sha1() if manifest is not None else None
                    with open(dest, "wb") as o:
                        while remaining > 0:
                            chunk = f.read(min(CHUNK, remaining))
                            if not chunk:
                                die("unexpected EOF extracting %s "
                                    "(missing %d of %d bytes) - truncated image?"
                                    % (dest, remaining, size))
                            o.write(chunk)
                            if h is not None:
                                h.update(chunk)
                            remaining -= len(chunk)
                            if progress:
                                progress(total + (size - remaining), grand)
                    if manifest is not None:
                        manifest[os.path.relpath(dest, out_dir).replace(os.sep, "/")] = h.hexdigest()
                    files += 1
                    total += size
                    if not quiet:
                        print("  %s (%d bytes)" % (os.path.relpath(dest, out_dir), size))
    print("extracted %d files, %d dirs, %d bytes total" % (files, dirs, total))
    return files, total


class _Window:
    """Read-only view of one file inside an image, hashing as it goes.

    Owns the reader it is given and closes it. The manifest entry is
    only written if the file was read to its end: a partially consumed
    window records nothing, so a caller that skips bytes fails
    verification loudly instead of certifying a short read."""

    def __init__(self, f, offset, size, rel=None, manifest=None):
        import hashlib
        self._f = f
        self._left = size
        self._rel = rel
        self._manifest = manifest
        self._h = hashlib.sha1() if manifest is not None else None
        if size:
            f.seek(offset)

    def read(self, n=-1):
        if self._left <= 0:
            return b""
        if n is None or n < 0:
            n = self._left
        data = self._f.read(min(n, self._left))
        if not data:
            die("unexpected EOF reading %s (missing %d bytes) - "
                "truncated image?" % (self._rel, self._left))
        self._left -= len(data)
        if self._h is not None:
            self._h.update(data)
        return data

    def close(self):
        try:
            self._f.close()
        finally:
            if self._h is not None and self._left == 0:
                self._manifest[self._rel] = self._h.hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _NoClose:
    """A reader whose close() does nothing, so it can outlive a window."""

    def __init__(self, f):
        self._f = f

    def read(self, n=-1):
        return self._f.read(n)

    def seek(self, *a):
        return self._f.seek(*a)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def shared_opener(opener):
    """Turn an opener into one that hands out views of a single reader,
    plus the closer for it.

    Opening a fresh reader per file is right when files are fetched
    concurrently and wasteful when they are not: a GoD or compressed
    reader re-reads its index every time it is constructed, which on a
    many-file image costs more than the reads themselves. A consumer
    that takes one file at a time, in order, to its end - which the
    archive writers do - can safely share one reader between them,
    because the windows never overlap. Do not use it for anything
    concurrent."""
    box = []

    def go():
        if not box:
            box.append(opener())
        return _NoClose(box[0])

    def close():
        if box:
            try:
                box[0].close()
            finally:
                del box[:]

    return go, close


def file_entries(iso_path, opener=None, manifest=None):
    """List an image's files as (relpath, size, open) without extracting
    it - `open` being a zero-argument callable returning a stream over
    just that file's bytes.

    This is the same walk `extract` does, stopping short of writing
    anything: the tables are read once up front, then each file is
    fetched on demand from its own reader. `opener` supplies those
    readers for sources that are not plain files (a GoD container, a
    compressed image); with none, the path is opened directly. Given a
    manifest dict, each stream fills in its own SHA-1 as it is consumed,
    so a caller that streams every file ends up with exactly the
    manifest a full extraction would have produced.
    """
    if opener is None:
        if not isinstance(iso_path, (str, bytes, os.PathLike)):
            raise XdvdfsError("file_entries needs an opener for a "
                              "non-path source")
        _p = iso_path

        def opener():
            return open(_p, "rb")

    found = []
    with _as_file(iso_path) as f:
        base = find_base(f)
        f.seek(base + 32 * SECTOR + len(MAGIC))
        root_sector, root_size = struct.unpack("<II", f.read(8))
        stack = [(read_table(f, base, root_sector, root_size), "")]
        while stack:
            table, prefix = stack.pop()
            for name, start, size, attr in walk_table(table):
                name = safe_name(name)
                rel = prefix + "/" + name if prefix else name
                if attr & ATTR_DIR:
                    stack.append((read_table(f, base, start, size), rel))
                else:
                    found.append((rel, size, base + start * SECTOR))

    def make(rel, size, off):
        def go():
            return _Window(opener(), off, size, rel, manifest)
        return go

    return [(rel, size, make(rel, size, off)) for rel, size, off in found]


def main():
    ap = argparse.ArgumentParser(
        prog="xv-xiso",
        description="Extract an XDVDFS (Xbox/Xbox 360) image to a directory. "
                    "Auto-detects bare game partitions and full XGD1/2/3 images.")
    ap.add_argument("iso", help="path to the image")
    ap.add_argument("outdir", help="output directory (created if needed)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print the final summary")
    args = ap.parse_args()
    extract(args.iso, args.outdir, quiet=args.quiet)


def cli():
    try:
        return main()
    except XdvdfsError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())


def hash_walk(iso_path, progress=None):
    """Walk the full filesystem, SHA-1 hashing every file's content without
    writing anything. Returns {relative_path: sha1hex}. Single read pass -
    used to verify built ISOs against a source-tree manifest."""
    import hashlib
    result = {}
    with _as_file(iso_path) as f:
        base = find_base(f)
        f.seek(base + 32 * SECTOR + len(MAGIC))
        root_sector, root_size = struct.unpack("<II", f.read(8))
        grand = done = 0
        if progress:
            tstack = [read_table(f, base, root_sector, root_size)]
            while tstack:
                for _n, st, sz, at in walk_table(tstack.pop()):
                    if at & ATTR_DIR:
                        tstack.append(read_table(f, base, st, sz))
                    else:
                        grand += sz
        stack = [(read_table(f, base, root_sector, root_size), "")]
        while stack:
            table, prefix = stack.pop()
            for name, start, size, attr in walk_table(table):
                rel = prefix + name
                if attr & ATTR_DIR:
                    stack.append((read_table(f, base, start, size), rel + "/"))
                    continue
                h = hashlib.sha1()
                f.seek(base + start * SECTOR)
                remaining = size
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        die("unexpected EOF hashing %s (missing %d of %d bytes)"
                            % (rel, remaining, size))
                    h.update(chunk)
                    remaining -= len(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, grand)
                result[rel] = h.hexdigest()
    return result


def hash_tree(dir_path):
    """SHA-1 manifest of a directory tree: {relative_path: sha1hex}."""
    import hashlib
    result = {}
    for root, _dirs, files in os.walk(dir_path):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, dir_path).replace(os.sep, "/")
            h = hashlib.sha1()
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
            result[rel] = h.hexdigest()
    return result


# ---------------------------------------------------------------------------
# Writer: pack a directory tree into a bare XDVDFS image.
#
# Deterministic layout contract (v1) - the same input tree (paths + bytes)
# produces a byte-identical image on every run and every machine:
#   * directory entries sorted by their cp1252 name folded to ASCII uppercase
#     (bytes 0x61-0x7A minus 0x20, all other bytes unchanged), compared as
#     byte strings; a strict prefix sorts first.  This is the on-disc BST
#     comparator used by Microsoft's mastering tools (verified against real
#     images) and by extract-xiso/xdvdfs.  Names that collide after folding
#     are rejected.
#   * each table is a perfectly balanced BST over the sorted entries:
#     the subtree root of slice [lo, hi) is index lo + (hi - lo) // 2,
#     recursively.  Entries are stored in preorder (root at offset 0, then
#     the whole left subtree, then the right).  "No child" is encoded as 0,
#     as in Microsoft-mastered images.
#   * entries are 4-byte aligned and never cross a 2048-byte sector
#     boundary (skipped space and all other table padding is 0xFF); table
#     sizes are rounded up to whole sectors.  An empty directory is a
#     single 0xFF-filled sector.
#   * sectors 0-31 are zero.  Sector 32 is the volume descriptor: magic,
#     root sector u32 LE, root size u32 LE, filetime u64 = 0, zero padding,
#     magic again at 0x7EC.  Partition base is 0 (bare image).
#   * directory tables start at sector 33, allocated depth-first: a
#     directory's table, then each entry in canonical order with
#     subdirectories recursed immediately.  File data follows all tables,
#     allocated by the same depth-first canonical walk; every file starts
#     on a fresh sector and its last sector is zero-padded.  A zero-byte
#     file is recorded as start sector 0, size 0.
#   * attributes: 0x10 for directories, 0x20 for files.  The image ends
#     exactly at the last allocated sector.

ATTR_FILE = 0x20
DESC_PAD = 0x800 - 2 * len(MAGIC) - 16


def _fold(raw):
    return bytes(c - 32 if 0x61 <= c <= 0x7A else c for c in raw)


def _encode_name(name, where):
    if not name or name in (".", "..") or "/" in name or "\\" in name \
            or "\x00" in name:
        die("invalid entry name %r in %s" % (name, where))
    try:
        raw = name.encode("cp1252")
    except UnicodeEncodeError:
        die("entry name %r in %s is not Windows-1252 encodable" % (name, where))
    if len(raw) > 255:
        die("entry name %r in %s exceeds 255 bytes" % (name, where))
    return raw


def _scan_dir(path):
    """One level of the source tree as canonically sorted entry dicts."""
    entries = []
    for name in os.listdir(path):
        raw = _encode_name(name, path)
        p = os.path.join(path, name)
        if os.path.isdir(p):
            e = {"raw": raw, "dir": True, "node": _scan_dir(p)}
        elif os.path.isfile(p):
            size = os.path.getsize(p)
            if size > 0xFFFFFFFF:
                die("%s is %d bytes; XDVDFS file sizes are 32-bit" % (p, size))
            e = {"raw": raw, "dir": False, "src": p, "size": size}
        else:
            die("unsupported entry (not a file or directory): %s" % p)
        e["start"] = 0
        entries.append(e)
    entries.sort(key=lambda e: _fold(e["raw"]))
    for i in range(1, len(entries)):
        if _fold(entries[i]["raw"]) == _fold(entries[i - 1]["raw"]):
            die("names collide under XDVDFS case folding in %s: %r vs %r"
                % (path, entries[i - 1]["raw"], entries[i]["raw"]))
    return {"entries": entries}


def _entry_span(raw):
    return (14 + len(raw) + 3) & ~3


def _layout_table(node):
    """Assign each entry its byte offset in the table (balanced-BST
    preorder, entries never crossing a sector boundary) and store the
    sector-aligned table size on the node."""
    entries = node["entries"]
    offs = [0] * len(entries)
    pos = [0]

    def place(lo, hi):
        if lo >= hi:
            return
        mid = lo + (hi - lo) // 2
        span = _entry_span(entries[mid]["raw"])
        p = pos[0]
        if p % SECTOR + span > SECTOR:
            p = (p // SECTOR + 1) * SECTOR
        if p // 4 > 0xFFFE:
            die("directory table too large (child offset exceeds 16 bits)")
        offs[mid] = p
        pos[0] = p + span
        place(lo, mid)
        place(mid + 1, hi)

    place(0, len(entries))
    node["offs"] = offs
    node["tsize"] = max(-(-pos[0] // SECTOR), 1) * SECTOR


def _render_table(node):
    entries = node["entries"]
    offs = node["offs"]
    buf = bytearray(b"\xff" * node["tsize"])

    def emit(lo, hi):
        if lo >= hi:
            return 0
        mid = lo + (hi - lo) // 2
        left = emit(lo, mid)
        right = emit(mid + 1, hi)
        e = entries[mid]
        raw = e["raw"]
        if e["dir"]:
            attr, size = ATTR_DIR, e["node"]["tsize"]
        else:
            attr, size = ATTR_FILE, e["size"]
        struct.pack_into("<HHIIBB", buf, offs[mid], left, right,
                         e["start"], size, attr, len(raw))
        buf[offs[mid] + 14:offs[mid] + 14 + len(raw)] = raw
        return offs[mid] // 4

    emit(0, len(entries))
    return bytes(buf)


def pack(src_dir, out_iso, progress=None):
    """Pack src_dir into a bare XDVDFS image at out_iso.  Deterministic:
    see the layout contract above.  Streams file data in 1 MiB chunks.
    Returns (file_count, total_data_bytes)."""
    if not os.path.isdir(src_dir):
        die("not a directory: %s" % src_dir)
    return pack_tree(_scan_dir(src_dir), out_iso, progress=progress)


def tree_from_entries(entries, where="<stream>"):
    """Build the same node tree _scan_dir produces, from files that are
    not on disk.

    `entries` is an iterable of (relpath, size, opener) - opener being a
    zero-argument callable returning a readable stream positioned at the
    start of that file. The resulting tree is sorted and folded exactly
    as a directory scan would be, so an image packed from it is
    byte-identical to one packed from the same files unpacked to disk.
    That equivalence is the whole point and is checked in the tests."""
    root = {"entries": []}
    dirs = {(): root}

    def _dir(parts):
        node = dirs.get(parts)
        if node is not None:
            return node
        parent = _dir(parts[:-1])
        node = {"entries": []}
        parent["entries"].append({"raw": _encode_name(parts[-1], where),
                                  "dir": True, "node": node, "start": 0})
        dirs[parts] = node
        return node

    for rel, size, opener in entries:
        parts = tuple(p for p in rel.replace("\\", "/").split("/") if p)
        if not parts:
            die("empty entry name in %s" % where)
        if size > 0xFFFFFFFF:
            die("%s is %d bytes; XDVDFS file sizes are 32-bit" % (rel, size))
        _dir(parts[:-1])["entries"].append(
            {"raw": _encode_name(parts[-1], where), "dir": False,
             "size": size, "open": opener, "start": 0, "src": rel})

    def _sort(node):
        entries = node["entries"]
        entries.sort(key=lambda e: _fold(e["raw"]))
        for i in range(1, len(entries)):
            if _fold(entries[i]["raw"]) == _fold(entries[i - 1]["raw"]):
                die("names collide under XDVDFS case folding: %r vs %r"
                    % (entries[i - 1]["raw"], entries[i]["raw"]))
        for e in entries:
            if e["dir"]:
                _sort(e["node"])
    _sort(root)
    return root


def pack_tree(root, out_iso, progress=None):
    """Pack an already-built node tree. See pack() and
    tree_from_entries() for the two ways to get one."""

    tables = []
    file_runs = []
    next_sector = [33]

    def alloc(node):
        _layout_table(node)
        node["sector"] = next_sector[0]
        next_sector[0] += node["tsize"] // SECTOR
        tables.append(node)
        for e in node["entries"]:
            if e["dir"]:
                alloc(e["node"])
                e["start"] = e["node"]["sector"]

    def alloc_files(node):
        for e in node["entries"]:
            if e["dir"]:
                alloc_files(e["node"])
            elif e["size"] > 0:
                e["start"] = next_sector[0]
                next_sector[0] += -(-e["size"] // SECTOR)
                file_runs.append(e)

    alloc(root)
    alloc_files(root)

    files = 0
    total = 0
    try:
        with open(out_iso, "wb") as o:
            o.write(b"\x00" * (32 * SECTOR))
            o.write(MAGIC
                    + struct.pack("<IIQ", root["sector"], root["tsize"], 0)
                    + b"\x00" * DESC_PAD + MAGIC)
            for node in tables:
                if o.tell() != node["sector"] * SECTOR:
                    die("internal error: table allocation drift")
                o.write(_render_table(node))
            grand_total = sum(e["size"] for e in file_runs)
            for e in file_runs:
                if o.tell() != e["start"] * SECTOR:
                    die("internal error: file allocation drift")
                copied = 0
                with (e["open"]() if e.get("open") else
                      open(e["src"], "rb")) as f:
                    # os.sendfile moves the bytes inside the kernel: no
                    # read into Python, no write back out. Falls back to
                    # the portable chunk loop anywhere it is unavailable
                    # or refuses the descriptors.
                    ifd = None
                    if _KCOPY:
                        try:
                            ifd = f.fileno()
                        except (AttributeError, OSError, ValueError):
                            ifd = None   # a stream, not a file: copy in Python
                    if ifd is not None:
                        o.flush()
                        ofd = o.fileno()
                        # copy_file_range first, the way CPython's own
                        # shutil orders it: on a copy-on-write filesystem
                        # the kernel shares the extents instead of
                        # copying them, which is not a faster copy so
                        # much as no copy at all - measured 17.5 GB/s on
                        # btrfs against 1.2 GB/s for sendfile. Elsewhere
                        # it is an in-kernel copy like sendfile.
                        for how in _KCOPY:
                            try:
                                while copied < e["size"]:
                                    want = min(SENDFILE_CHUNK,
                                               e["size"] - copied)
                                    if how is os.copy_file_range:
                                        n = how(ifd, ofd, want, copied)
                                    else:
                                        n = how(ofd, ifd, None, want)
                                    if not n:
                                        break
                                    copied += n
                                    if progress:
                                        progress(total + copied, grand_total)
                                break
                            except OSError:
                                # this kernel or this pairing refuses it;
                                # try the next, then the portable loop
                                f.seek(copied)
                                continue
                    if copied < e["size"]:
                        while copied < e["size"]:
                            chunk = f.read(min(CHUNK, e["size"] - copied))
                            if not chunk:
                                break
                            o.write(chunk)
                            copied += len(chunk)
                            if progress:
                                progress(total + copied, grand_total)
                if copied != e["size"]:
                    die("%s changed size during pack (%d != %d)"
                        % (e.get("src", e["raw"]), copied, e["size"]))
                pad = -e["size"] % SECTOR
                if pad:
                    o.write(b"\x00" * pad)
                files += 1
                total += e["size"]
            files += sum(1 for n in tables for e in n["entries"]
                         if not e["dir"] and e["size"] == 0)
    except BaseException:
        try:
            os.unlink(out_iso)
        except OSError:
            pass
        raise
    return files, total


def list_root(iso_path):
    """Return the root directory entry names of an XDVDFS image."""
    with _as_file(iso_path) as f:
        base = find_base(f)
        f.seek(base + 32 * SECTOR + len(MAGIC))
        import struct as _struct
        root_sector, root_size = _struct.unpack("<II", f.read(8))
        table = read_table(f, base, root_sector, root_size)
    return [name for name, _, _, _ in walk_table(table)]
