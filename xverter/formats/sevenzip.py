"""7z archive reading - native, with random access into LZMA2 members.

Format facts from 7-Zip's own DOC/7zFormat.txt (github.com/ip7z/7zip): a
32-byte signature header points at the "next header", which is either a
plain kHeader or a kEncodedHeader (the real header packed as a one-folder
stream - decoded here the same way as any other folder). The header holds
the pack streams (where the compressed bytes sit), the folders (one coder
chain each, with unpack sizes and CRCs), the substreams (how a solid
folder's output splits into files) and the file names.

What this reader supports natively: folders with ONE simple coder that is
LZMA2 (0x21), LZMA (03 01 01) or Copy (00), one packed stream each - the
shape every game rip in the wild has. Anything else (BCJ/BCJ2 filters,
Delta, PPMd, BZip2, Deflate, AES) raises SevenZipError, and the caller
falls back to the 7-Zip engine.

Random access: an LZMA2 stream is a sequence of chunks, and a chunk whose
control byte asks for a dictionary reset starts a fresh decode with no
dependency on anything before it. 7-Zip's multithreaded LZMA2 encoder
emits such a reset at every block boundary (blocks of about four
dictionaries), so a member compressed that way can be entered at any
block: seeking costs at most one block of decompression, and blocks
decode independently - in parallel, on any interpreter, because liblzma
releases the GIL while it works. A stream compressed single-threaded has
one block, so a backward seek restarts from the beginning; reads are
then still correct, just paid for the way a pipe would pay.

Verification: the archive's CRCs are checked on every full sequential
read of a member (crc32 over the bytes delivered); a random-access read
cannot check anything and says so. The signature header and the packed
header are CRC-checked on open.
"""

import bisect
import lzma
import os
import struct
import threading
import zlib

MAGIC = b"7z\xbc\xaf\x27\x1c"

# property ids (7zFormat.txt "Property IDs")
_END, _HEADER, _ARCHIVE_PROPS, _ADDITIONAL_STREAMS, _MAIN_STREAMS, _FILES_INFO = 0, 1, 2, 3, 4, 5
_PACK_INFO, _UNPACK_INFO, _SUBSTREAMS_INFO, _SIZE, _CRC, _FOLDER = 6, 7, 8, 9, 10, 11
_CODERS_UNPACK_SIZE, _NUM_UNPACK_STREAM, _EMPTY_STREAM, _EMPTY_FILE, _ANTI = 12, 13, 14, 15, 16
_NAME, _CTIME, _ATIME, _MTIME, _ATTRIBUTES, _COMMENT, _ENCODED_HEADER, _START_POS, _DUMMY = \
    17, 18, 19, 20, 21, 22, 23, 24, 25

CODER_COPY = b"\x00"
CODER_LZMA = b"\x03\x01\x01"
CODER_LZMA2 = b"\x21"
_CODER_NAMES = {
    b"\x03\x03\x01\x03": "BCJ x86", b"\x03\x03\x01\x1b": "BCJ2", b"\x03": "Delta",
    b"\x03\x04\x01": "PPMd", b"\x04\x02\x02": "BZip2", b"\x04\x01\x08": "Deflate",
    b"\x04\x01\x09": "Deflate64", b"\x06\xf1\x07\x01": "AES-256", b"\x21": "LZMA2",
    b"\x03\x01\x01": "LZMA", b"\x00": "Copy", b"\x04\xf7\x11\x01": "Zstandard",
}

_IO_CHUNK = 1 << 20
_OUT_CHUNK = 8 << 20
_DISCARD_CHUNK = 4 << 20
#: A block is decoded whole (and cached / prefetched) only up to this size;
#: bigger blocks - a single-threaded archive's one block is the whole
#: member - are streamed with a bounded window instead.
BLOCK_CACHE_MAX = 512 << 20
PREFETCH_BLOCKS = 2


def _ram_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 8 << 30


#: Decoded blocks held in memory across EVERY open member in the process
#: (a conversion opens two or three readers at once: the build, the
#: source-hash-ahead, the validator) - an eighth of RAM, at most 1 GiB, at
#: least two 512 MiB blocks' worth is never promised: below 2 blocks the
#: prefetch simply stays shallower.
BLOCK_CACHE_BYTES = min(1 << 30, _ram_bytes() // 8)
_CACHE_TOTAL = [0]
_CACHE_TOTAL_LOCK = threading.Lock()
#: (archive path, folder pack offset) -> [_BlockCache, refcount]. A
#: conversion opens the same image several times at once (build, validator,
#: source-hash-ahead); sharing one cache means a block decoded for one of
#: them serves the others.
_SHARED_CACHES = {}
_SHARED_LOCK = threading.Lock()

def _acquire_cache(path, folder):
    key = (os.path.realpath(path), folder.pack_off)
    with _SHARED_LOCK:
        ent = _SHARED_CACHES.get(key)
        if ent is None:
            ent = [_BlockCache(lambda p=path: open(p, "rb"), folder), 0]
            _SHARED_CACHES[key] = ent
        ent[1] += 1
        return ent[0]


def _release_cache(path, folder):
    key = (os.path.realpath(path), folder.pack_off)
    with _SHARED_LOCK:
        ent = _SHARED_CACHES.get(key)
        if ent is None:
            return
        ent[1] -= 1
        if ent[1] <= 0:
            del _SHARED_CACHES[key]
            ent[0].close()


class SevenZipError(Exception):
    pass


# ------------------------------------------------------------- primitives

class _Cursor:
    """Byte cursor with the 7z number encodings."""

    def __init__(self, data):
        self.d = data
        self.p = 0

    def byte(self):
        if self.p >= len(self.d):
            raise SevenZipError("7z header truncated")
        b = self.d[self.p]
        self.p += 1
        return b

    def read(self, n):
        if self.p + n > len(self.d):
            raise SevenZipError("7z header truncated")
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def u64(self):
        """7z UINT64: the first byte's leading 1-bits count the extra
        little-endian bytes; its remaining bits are the high part."""
        first = self.byte()
        mask = 0x80
        extra = 0
        while extra < 8 and first & mask:
            extra += 1
            mask >>= 1
        low = int.from_bytes(self.read(extra), "little") if extra else 0
        if extra >= 7:
            return low
        high = first & (mask - 1)
        return (high << (8 * extra)) | low

    def bits(self, n):
        out = []
        cur = 0
        for i in range(n):
            if i % 8 == 0:
                cur = self.byte()
            out.append(bool(cur & (0x80 >> (i % 8))))
        return out

    def digests(self, n):
        all_defined = self.byte()
        defined = [True] * n if all_defined else self.bits(n)
        return [self.u32() if d else None for d in defined]


class _Folder:
    __slots__ = ("coder", "props", "unpack_size", "crc", "pack_off", "pack_size",
                 "index", "lock")

    def __init__(self):
        self.coder = None
        self.props = b""
        self.unpack_size = 0
        self.crc = None
        self.pack_off = 0
        self.pack_size = 0
        self.index = None          # LZMA2: [(unpacked_start, packed_offset)]
        self.lock = threading.Lock()

    def coder_name(self):
        return _CODER_NAMES.get(self.coder, (self.coder or b"").hex())


class Member:
    __slots__ = ("name", "size", "crc", "folder", "offset")

    def __init__(self, name, size, crc, folder, offset):
        self.name, self.size, self.crc, self.folder, self.offset = name, size, crc, folder, offset


# --------------------------------------------------------- header parsing

def _read_pack_info(c):
    pack_pos = c.u64()
    n = c.u64()
    sizes = []
    while True:
        nid = c.byte()
        if nid == _END:
            break
        if nid == _SIZE:
            sizes = [c.u64() for _ in range(n)]
        elif nid == _CRC:
            c.digests(n)
        else:
            raise SevenZipError("unexpected id %d in PackInfo" % nid)
    if len(sizes) != n:
        raise SevenZipError("PackInfo without sizes")
    return pack_pos, sizes


def _read_folder(c):
    f = _Folder()
    num_coders = c.u64()
    n_in = n_out = 0
    for i in range(num_coders):
        flags = c.byte()
        cid = c.read(flags & 0x0F)
        if flags & 0x10:
            ci, co = c.u64(), c.u64()
        else:
            ci = co = 1
        props = c.read(c.u64()) if flags & 0x20 else b""
        if flags & 0x80:
            raise SevenZipError("7z alternative coder methods are not supported")
        n_in += ci
        n_out += co
        if i == 0:
            f.coder, f.props = cid, props
        else:
            # a chain (filter + compressor): not decoded natively
            f.coder = b"chain:" + (f.coder or b"") + b"+" + cid
    for _ in range(n_out - 1):                      # bind pairs
        c.u64(); c.u64()
    n_packed = n_in - (n_out - 1)
    if n_packed > 1:
        for _ in range(n_packed):
            c.u64()
    return f, n_out, n_packed


def _read_unpack_info(c):
    if c.byte() != _FOLDER:
        raise SevenZipError("UnpackInfo without folders")
    n = c.u64()
    if c.byte() != 0:
        raise SevenZipError("external folder data is not supported")
    folders, outs, packed = [], [], []
    for _ in range(n):
        f, n_out, n_packed = _read_folder(c)
        folders.append(f)
        outs.append(n_out)
        packed.append(n_packed)
    if c.byte() != _CODERS_UNPACK_SIZE:
        raise SevenZipError("UnpackInfo without unpack sizes")
    for f, n_out in zip(folders, outs):
        sizes = [c.u64() for _ in range(n_out)]
        f.unpack_size = sizes[-1]                 # the chain's final output
    while True:
        nid = c.byte()
        if nid == _END:
            break
        if nid == _CRC:
            for f, d in zip(folders, c.digests(n)):
                f.crc = d
        else:
            raise SevenZipError("unexpected id %d in UnpackInfo" % nid)
    return folders, packed


def _read_substreams(c, folders):
    counts = [1] * len(folders)
    sizes = None
    crcs = None
    while True:
        nid = c.byte()
        if nid == _END:
            break
        if nid == _NUM_UNPACK_STREAM:
            counts = [c.u64() for _ in folders]
        elif nid == _SIZE:
            sizes = []
            for f, k in zip(folders, counts):
                ss = [c.u64() for _ in range(k - 1)] if k else []
                if k:
                    ss.append(f.unpack_size - sum(ss))
                sizes.append(ss)
        elif nid == _CRC:
            need = sum(k for f, k in zip(folders, counts)
                       if not (k == 1 and f.crc is not None))
            crcs = c.digests(need)
        else:
            raise SevenZipError("unexpected id %d in SubStreamsInfo" % nid)
    if sizes is None:
        sizes = [[f.unpack_size] if k == 1 else [] for f, k in zip(folders, counts)]
    # distribute crcs: a folder with one stream and a folder crc keeps that
    out_crcs = []
    it = iter(crcs or [])
    for f, k in zip(folders, counts):
        if k == 1 and f.crc is not None:
            out_crcs.append([f.crc])
        else:
            out_crcs.append([next(it, None) for _ in range(k)])
    return counts, sizes, out_crcs


def _read_streams_info(c):
    pack_pos, pack_sizes, folders, packed_per_folder = 0, [], [], []
    counts = sizes = crcs = None
    while True:
        nid = c.byte()
        if nid == _END:
            break
        if nid == _PACK_INFO:
            pack_pos, pack_sizes = _read_pack_info(c)
        elif nid == _UNPACK_INFO:
            folders, packed_per_folder = _read_unpack_info(c)
        elif nid == _SUBSTREAMS_INFO:
            counts, sizes, crcs = _read_substreams(c, folders)
        else:
            raise SevenZipError("unexpected id %d in StreamsInfo" % nid)
    if counts is None:
        counts, sizes, crcs = _read_substreams(_Cursor(b"\x00"), folders)
    # place each folder's packed bytes: folders consume pack streams in order
    pos = 32 + pack_pos
    k = 0
    for f, n_packed in zip(folders, packed_per_folder):
        if n_packed != 1:
            f.coder = b"multi-stream:" + (f.coder or b"")
        f.pack_off = pos
        f.pack_size = sum(pack_sizes[k:k + n_packed])
        pos += f.pack_size
        k += n_packed
    return folders, counts, sizes, crcs


def _read_files_info(c):
    n = c.u64()
    names = [""] * n
    empty_stream = [False] * n
    while True:
        nid = c.byte()
        if nid == _END:
            break
        size = c.u64()
        data = c.read(size)
        sub = _Cursor(data)
        if nid == _EMPTY_STREAM:
            empty_stream = sub.bits(n)
        elif nid == _NAME:
            if sub.byte() != 0:
                raise SevenZipError("external file names are not supported")
            raw = data[1:]
            parts = raw.decode("utf-16-le", "surrogateescape").split("\x00")
            names = parts[:n]
            if len(names) != n:
                raise SevenZipError("7z name table is short")
        # times, attributes, empty-file, anti, dummy: skipped
    return names, empty_stream


def _decode_folder_whole(fobj, folder):
    """Decode a whole (small) folder - used for the encoded header."""
    return b"".join(_FolderStream(fobj, folder).iter_from(0, folder.unpack_size))


# ------------------------------------------------------------- decoding

def _lzma2_dict_size(props):
    if len(props) < 1:
        raise SevenZipError("LZMA2 coder without properties")
    p = props[0]
    if p > 40:
        raise SevenZipError("bad LZMA2 dictionary property %d" % p)
    if p == 40:
        return 0xFFFFFFFF
    return (2 | (p & 1)) << (p // 2 + 11)


def _lzma1_filter(props):
    if len(props) < 5:
        raise SevenZipError("LZMA coder without properties")
    d = props[0]
    lc, lp, pb = d % 9, (d // 9) % 5, d // 45
    return {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack("<I", props[1:5])[0],
            "lc": lc, "lp": lp, "pb": pb}


def _scan_lzma2_index(fobj, folder):
    """[(unpacked_start, packed_offset)] of every dictionary-reset chunk in
    the folder's LZMA2 stream - the points a decode may start from. One
    pass over the chunk headers only (a few bytes per chunk)."""
    index = []
    pos = folder.pack_off
    end = folder.pack_off + folder.pack_size
    unp = 0
    buf = b""
    bpos = 0

    def rd(off, k):
        nonlocal buf, bpos
        if off < bpos or off + k > bpos + len(buf):
            fobj.seek(off)
            buf = fobj.read(_IO_CHUNK)
            bpos = off
        return buf[off - bpos:off - bpos + k]

    while pos < end:
        ctl = rd(pos, 1)
        if not ctl:
            break
        ctl = ctl[0]
        if ctl == 0:
            break
        if ctl in (1, 2):
            size = struct.unpack(">H", rd(pos + 1, 2))[0] + 1
            hdr, packed, unpacked, reset = 3, size, size, (ctl == 1)
        elif ctl & 0x80:
            h = rd(pos + 1, 5)
            unpacked = ((ctl & 0x1F) << 16) + struct.unpack(">H", h[:2])[0] + 1
            packed = struct.unpack(">H", h[2:4])[0] + 1
            mode = (ctl >> 5) & 3
            hdr, reset = 5 + (1 if mode >= 2 else 0), (mode == 3)
        else:
            raise SevenZipError("bad LZMA2 chunk control byte 0x%02X at %d" % (ctl, pos))
        if reset:
            index.append((unp, pos))
        pos += hdr + packed
        unp += unpacked
    if not index or index[0][0] != 0:
        raise SevenZipError("LZMA2 stream does not start with a dictionary reset")
    if unp != folder.unpack_size:
        raise SevenZipError("LZMA2 chunk sizes sum to %d, folder says %d"
                            % (unp, folder.unpack_size))
    return index


class _FolderStream:
    """Decodes one folder's unpacked bytes on demand."""

    def __init__(self, fobj, folder):
        self.f = fobj
        self.folder = folder
        self.coder = folder.coder
        if self.coder not in (CODER_COPY, CODER_LZMA, CODER_LZMA2):
            name = _CODER_NAMES.get(self.coder, self.coder.hex() if self.coder else "?")
            raise SevenZipError("7z coder %s is not decoded natively" % name)
        self._dec = None
        self._dec_pos = 0          # unpacked offset of the next byte the decoder yields
        self._pack_pos = 0         # next packed byte to feed
        self._pack_end = folder.pack_off + folder.pack_size
        self._pending = b""

    # -- block index --------------------------------------------------
    def blocks(self):
        """Entry points: [(unpacked_start, packed_offset)], ascending."""
        fo = self.folder
        if fo.index is None:
            with fo.lock:
                if fo.index is None:
                    if self.coder == CODER_LZMA2:
                        fo.index = _scan_lzma2_index(self.f, fo)
                    else:
                        fo.index = [(0, fo.pack_off)]
        return fo.index

    def block_of(self, unpacked_off):
        idx = self.blocks()
        b = bisect.bisect_right([s for s, _p in idx], unpacked_off) - 1
        return max(b, 0)

    def block_span(self, b):
        idx = self.blocks()
        start = idx[b][0]
        end = idx[b + 1][0] if b + 1 < len(idx) else self.folder.unpack_size
        return start, end

    # -- decoder ------------------------------------------------------
    def _new_decoder(self):
        if self.coder == CODER_LZMA2:
            return lzma.LZMADecompressor(
                format=lzma.FORMAT_RAW,
                filters=[{"id": lzma.FILTER_LZMA2,
                          "dict_size": _lzma2_dict_size(self.folder.props)}])
        if self.coder == CODER_LZMA:
            return lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
                                         filters=[_lzma1_filter(self.folder.props)])
        return None                                    # Copy

    def _restart(self, b):
        start, pack_off = self.blocks()[b]
        self._dec = self._new_decoder()
        self._dec_pos = start
        self._pack_pos = pack_off
        self._pending = b""

    def _pull(self, want):
        """Up to `want` decoded bytes from the current position."""
        if self.coder == CODER_COPY:
            self.f.seek(self.folder.pack_off + self._dec_pos)
            out = self.f.read(want)
            self._dec_pos += len(out)
            return out
        # Common case: one decompress call yields the whole request, which
        # is returned as-is - no bytearray copy on the hot path.
        parts = []
        got = 0
        while got < want:
            if self._pending:
                take = self._pending[:want - got]
                self._pending = self._pending[len(take):]
                parts.append(take)
                got += len(take)
                continue
            dec = self._dec
            if dec.eof:
                break
            chunk = b""
            if dec.needs_input:
                if self._pack_pos >= self._pack_end:
                    break
                self.f.seek(self._pack_pos)
                chunk = self.f.read(min(_IO_CHUNK, self._pack_end - self._pack_pos))
                if not chunk:
                    break
                self._pack_pos += len(chunk)
            try:
                data = dec.decompress(chunk, max_length=want - got)
            except lzma.LZMAError as e:
                raise SevenZipError("corrupt LZMA data in %s at packed "
                                    "offset %d: %s"
                                    % (self.folder.coder_name(), self._pack_pos, e))
            if data:
                parts.append(data)
                got += len(data)
        self._dec_pos += got
        if len(parts) == 1:
            return parts[0]
        return b"".join(parts)

    def _restart_at_zero(self):
        """Start decoding from the folder's first byte - needs no chunk
        index, so a purely sequential consumer never pays the scan."""
        self._dec = self._new_decoder()
        self._dec_pos = 0
        self._pack_pos = self.folder.pack_off
        self._pending = b""

    def iter_from(self, off, n):
        """Yield the folder's bytes [off, off+n) in chunks."""
        if n <= 0:
            return
        if self.coder == CODER_COPY:
            self._dec_pos = off
        elif self._dec is not None and self._dec_pos <= off and (
                self.folder.index is None
                or self.blocks()[self.block_of(off)][0] <= self._dec_pos):
            pass                # continue forward from where the decoder is
        elif self.folder.index is None and off == 0:
            self._restart_at_zero()
        else:
            # a jump: enter at the nearest dictionary-reset block (the scan
            # that finds them runs once, here, the first time it is needed)
            self._restart(self.block_of(off))
        while self._dec_pos < off:
            skip = self._pull(min(_OUT_CHUNK, off - self._dec_pos))
            if not skip:
                raise SevenZipError("7z stream ended before offset %d" % off)
        left = n
        while left > 0:
            chunk = self._pull(min(_IO_CHUNK * 4, left))
            if not chunk:
                raise SevenZipError("7z stream ended %d bytes short" % left)
            left -= len(chunk)
            yield chunk


class _BlockCache:
    """Whole-block decode with an LRU and a prefetch pool, for members whose
    blocks are small enough to hold (multithreaded archives)."""

    def __init__(self, fobj_factory, folder):
        self.factory = fobj_factory
        self.folder = folder
        self.lock = threading.Lock()
        self.cache = {}            # block -> bytes
        self.order = []
        self.futures = {}          # block -> Future
        self.pool = None

    def _decode(self, b):
        """Decode block b whole: a fresh decoder at the block's packed
        offset, fed in 1 MiB pieces until the block's unpacked size is out -
        one tight loop, no generator, no per-call slicing."""
        fobj = self.factory()
        try:
            st = _FolderStream(fobj, self.folder)
            start, end = st.block_span(b)
            size = end - start
            idx = st.blocks()
            pack_pos = idx[b][1]
            pack_end = self.folder.pack_off + self.folder.pack_size
            if st.coder == CODER_COPY:
                fobj.seek(self.folder.pack_off + start)
                return fobj.read(size)
            dec = st._new_decoder()
            parts = []
            got = 0
            fobj.seek(pack_pos)
            while got < size:
                if dec.eof:
                    break
                if dec.needs_input:
                    chunk = fobj.read(min(_IO_CHUNK, pack_end - fobj.tell()))
                    if not chunk:
                        break
                else:
                    chunk = b""
                try:
                    # a bounded max_length: asking for the whole remaining
                    # block makes liblzma's output buffer grow and copy
                    d = dec.decompress(chunk, max_length=min(size - got, _OUT_CHUNK))
                except lzma.LZMAError as e:
                    raise SevenZipError("corrupt LZMA data in block %d: %s" % (b, e))
                if d:
                    parts.append(d)
                    got += len(d)
            if got != size:
                raise SevenZipError("7z block %d decoded %d of %d bytes" % (b, got, size))
            return parts[0] if len(parts) == 1 else b"".join(parts)
        finally:
            fobj.close()

    def _pool(self):
        if self.pool is None:
            import concurrent.futures
            self.pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(PREFETCH_BLOCKS + 1, os.cpu_count() or 2),
                thread_name_prefix="7zblock")
        return self.pool

    def get(self, b, nblocks, sequential):
        import concurrent.futures
        with self.lock:
            data = self.cache.get(b)
            fut = None
            mine = False
            if data is None:
                fut = self.futures.get(b)
                if fut is None:
                    # decode it here, registered so a concurrent reader of
                    # the same block waits for this decode instead of
                    # repeating it
                    fut = concurrent.futures.Future()
                    self.futures[b] = fut
                    mine = True
        if data is None:
            if mine:
                try:
                    data = self._decode(b)
                except BaseException as e:            # noqa: BLE001
                    fut.set_exception(e)
                    with self.lock:
                        self.futures.pop(b, None)
                    raise
                fut.set_result(data)
            else:
                data = fut.result()
            with self.lock:
                self.futures.pop(b, None)
                if b not in self.cache:
                    self.cache[b] = data
                    self.order.append(b)
                    self._charge(len(data))
                    # evict oldest while over the PROCESS-wide budget,
                    # keeping the block just decoded
                    while len(self.order) > 1 and _CACHE_TOTAL[0] > BLOCK_CACHE_BYTES:
                        old = self.order.pop(0)
                        self._charge(-len(self.cache.pop(old)))
        if sequential:
            with self.lock:
                # prefetch only what the budget can hold alongside this block
                room = BLOCK_CACHE_BYTES - _CACHE_TOTAL[0]
                per = len(data)
                depth = max(0, min(PREFETCH_BLOCKS, room // per if per else 0))
                for nb in range(b + 1, min(b + 1 + depth, nblocks)):
                    if nb not in self.cache and nb not in self.futures:
                        self.futures[nb] = self._pool().submit(self._decode_into, nb)
        return data

    def _decode_into(self, b):
        """Pool entry: decode block b and file it in the cache (charged),
        so a prefetched block is accounted the moment it exists."""
        data = self._decode(b)
        with self.lock:
            if b not in self.cache:
                self.cache[b] = data
                self.order.append(b)
                self._charge(len(data))
        return data

    @staticmethod
    def _charge(n):
        with _CACHE_TOTAL_LOCK:
            _CACHE_TOTAL[0] += n

    def close(self):
        with self.lock:
            futs = list(self.futures.values())
            self.futures.clear()
            self._charge(-sum(len(v) for v in self.cache.values()))
            self.cache.clear()
            self.order.clear()
        for f in futs:
            f.cancel()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None


class MemberFile:
    """Seekable, read-only file-like view of one member."""

    def __init__(self, archive, member):
        self.archive = archive
        self.member = member
        self.size = member.size
        self.name = member.name
        self._pos = 0
        self._fobj = open(archive.path, "rb")
        self._stream = _FolderStream(self._fobj, member.folder)
        self._cache = None
        self._last_end = None
        self._crc = 0
        self._crc_pos = 0          # bytes covered by a continuous read from 0

    # -- file-like -------------------------------------------------------
    def tell(self):
        return self._pos

    def seek(self, off, whence=0):
        if whence == 1:
            off = self._pos + off
        elif whence == 2:
            off = self.size + off
        self._pos = max(0, min(off, self.size))
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        data = self.read_at(self._pos, n)
        self._pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def read_at(self, off, n):
        """Bytes [off, off+n) of the member (clamped to its size)."""
        if off < 0:
            raise ValueError("negative offset")
        n = min(n, self.size - off)
        if n <= 0:
            return b""
        fold_off = self.member.offset + off
        st = self._stream
        if self._use_cache():
            data = self._read_cached(fold_off, n)
        else:
            data = b"".join(st.iter_from(fold_off, n))
        # continuous-from-zero reads accumulate the CRC for crc_ok(); a
        # read that starts over at 0 starts the accumulation over too
        if off == 0:
            self._crc, self._crc_pos = 0, 0
        if off == self._crc_pos:
            self._crc = zlib.crc32(data, self._crc)
            self._crc_pos += len(data)
        return data

    def _use_cache(self):
        st = self._stream
        if st.coder != CODER_LZMA2:
            return False
        idx = st.blocks()
        if len(idx) < 2:
            return False
        if self._cache is None:
            largest = max(st.block_span(b)[1] - st.block_span(b)[0] for b in range(len(idx)))
            if largest > BLOCK_CACHE_MAX:
                self._cache = False
            else:
                self._cache = _acquire_cache(self.archive.path, self.member.folder)
        return bool(self._cache)

    def _read_cached(self, fold_off, n):
        st = self._stream
        nblocks = len(st.blocks())
        out = bytearray()
        pos = fold_off
        sequential = (self._last_end == fold_off)
        while n > 0:
            b = st.block_of(pos)
            start, end = st.block_span(b)
            data = self._cache.get(b, nblocks, sequential)
            lo = pos - start
            take = data[lo:lo + n]
            out += take
            pos += len(take)
            n -= len(take)
            if not take:
                raise SevenZipError("7z block %d shorter than indexed" % b)
        self._last_end = pos
        return bytes(out)

    # -- integrity ---------------------------------------------------------
    def crc_ok(self):
        """True if a continuous read from offset 0 has covered the whole
        member and matched the archive's CRC; None if it has not been fully
        read that way (a random-access read cannot be checked); False on a
        mismatch."""
        if self.member.crc is None or self._crc_pos != self.size:
            return None
        return self._crc == self.member.crc

    def close(self):
        if self._cache:
            _release_cache(self.archive.path, self.member.folder)
            self._cache = None
        try:
            self._fobj.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# --------------------------------------------------------------- archive

class Archive:
    """A parsed .7z: members, and seekable readers over them."""

    def __init__(self, path):
        self.path = os.fspath(path)
        with open(self.path, "rb") as f:
            sig = f.read(32)
            if len(sig) < 32 or sig[:6] != MAGIC:
                raise SevenZipError("not a 7z archive: %s" % self.path)
            if zlib.crc32(sig[12:32]) != struct.unpack_from("<I", sig, 8)[0]:
                raise SevenZipError("7z signature header CRC mismatch")
            nh_off, nh_size, nh_crc = struct.unpack_from("<QQI", sig, 12)
            f.seek(32 + nh_off)
            hdr = f.read(nh_size)
            if len(hdr) != nh_size:
                raise SevenZipError("7z next header truncated")
            if zlib.crc32(hdr) != nh_crc:
                raise SevenZipError("7z header CRC mismatch")
            # an encoded header unpacks to the real header; unwrap until plain
            for _ in range(4):
                c = _Cursor(hdr)
                nid = c.byte()
                if nid == _HEADER:
                    break
                if nid != _ENCODED_HEADER:
                    raise SevenZipError("unexpected 7z header id %d" % nid)
                folders, _counts, _sizes, _crcs = _read_streams_info(c)
                if len(folders) != 1:
                    raise SevenZipError("encoded header with %d folders" % len(folders))
                hdr = _decode_folder_whole(f, folders[0])
                if folders[0].crc is not None and zlib.crc32(hdr) != folders[0].crc:
                    raise SevenZipError("7z encoded header CRC mismatch")
            else:
                raise SevenZipError("7z header nesting too deep")
            self.members = self._parse_header(c)

    def _parse_header(self, c):
        folders, counts, sizes, crcs = [], [], [], []
        names, empty = [], []
        while True:
            nid = c.byte()
            if nid == _END:
                break
            if nid == _ARCHIVE_PROPS:
                while True:
                    t = c.byte()
                    if t == 0:
                        break
                    c.read(c.u64())
            elif nid == _ADDITIONAL_STREAMS:
                _read_streams_info(c)
            elif nid == _MAIN_STREAMS:
                folders, counts, sizes, crcs = _read_streams_info(c)
            elif nid == _FILES_INFO:
                names, empty = _read_files_info(c)
            else:
                raise SevenZipError("unexpected id %d in 7z header" % nid)
        # files with a stream consume folder substreams in order
        members = []
        fi = 0
        si = 0
        off = 0
        for name, is_empty in zip(names, empty):
            if is_empty:
                continue
            while fi < len(folders) and si >= counts[fi]:
                fi += 1
                si = 0
                off = 0
            if fi >= len(folders):
                raise SevenZipError("more files than streams in 7z header")
            size = sizes[fi][si]
            crc = crcs[fi][si] if si < len(crcs[fi]) else None
            members.append(Member(name.replace("\\", "/"), size, crc, folders[fi], off))
            off += size
            si += 1
        return members

    def list(self):
        return [(m.name, m.size) for m in self.members]

    def member(self, name):
        for m in self.members:
            if m.name == name:
                return m
        raise SevenZipError("no member %r in %s" % (name, self.path))

    def open(self, name):
        """A seekable MemberFile; raises SevenZipError if the member's
        folder uses a coder this reader does not decode."""
        m = self.member(name)
        with open(self.path, "rb") as f:
            _FolderStream(f, m.folder)                 # validates the coder
        return MemberFile(self, m)

    def native(self, name):
        """True if `name` can be read natively (coder supported)."""
        try:
            m = self.member(name)
            with open(self.path, "rb") as f:
                _FolderStream(f, m.folder)
            return True
        except SevenZipError:
            return False

    def block_count(self, name):
        m = self.member(name)
        with open(self.path, "rb") as f:
            return len(_FolderStream(f, m.folder).blocks())


# ------------------------------------------------------------------ cli

def cli(argv=None):
    import argparse
    import sys
    import time
    ap = argparse.ArgumentParser(prog="xverter-7z", description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--verify", metavar="MEMBER", help="sequentially read MEMBER and check its CRC")
    a = ap.parse_args(argv)
    arc = Archive(a.archive)
    for m in arc.members:
        f = arc.native(m.name)
        blocks = arc.block_count(m.name) if f else "-"
        print("%12d  %s  coder=%s blocks=%s  %s" % (
            m.size, "crc=%08X" % m.crc if m.crc is not None else "crc=?",
            _CODER_NAMES.get(m.folder.coder, (m.folder.coder or b"").hex()), blocks, m.name))
    if a.verify:
        t = time.time()
        with arc.open(a.verify) as mf:
            n = 0
            while True:
                d = mf.read(4 << 20)
                if not d:
                    break
                n += len(d)
            ok = mf.crc_ok()
        print("%s: %d bytes, crc %s, %.1fs" % (a.verify, n, {True: "OK", False: "MISMATCH", None: "unchecked"}[ok], time.time() - t))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    cli()
