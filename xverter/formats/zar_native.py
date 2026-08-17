"""Pure-Python (stdlib-only) reader and writer for the ZArchive (.zar) format.

Format reference: https://github.com/Exzap/ZArchive (MIT-0 licensed), files
src/zarchivecommon.h, src/zarchivereader.cpp, src/zarchivewriter.cpp.

Layout (all integers BIG-endian on disk):

  [ compressed data section ]   starts at file offset 0; the virtual
                                uncompressed stream is split into 64 KiB
                                blocks, each stored zstd-compressed, or raw
                                when the compressed size would be >= 64 KiB
                                (stored size == 65536 means "uncompressed").
  [ pad to 8 bytes ]
  [ offset-record section ]     CompressionOffsetRecord[]: { u64 baseOffset;
                                u16 size[16] } where size[i] is
                                (compressed block size - 1). Block i maps to
                                record i//16, sub-entry i%16; its offset is
                                baseOffset + sum(size[j]+1 for j < i%16),
                                relative to the compressed-data section.
  [ name table section ]        length-prefixed names: 1 header byte
                                (len 0..0x7F), or 2 header bytes when MSB of
                                the first is set: len = (b0 & 0x7F)|(b1 << 7).
  [ file tree section ]         FileDirectoryEntry[16 bytes]: u32
                                nameOffsetAndTypeFlag (MSB set -> file, lower
                                31 bits -> name table offset, 0x7FFFFFFF for
                                the unnamed root) + 3 u32 payload words.
                                File: offsetLow, sizeLow, offsetAndSizeHigh
                                (low 16 bits extend offset to 48 bits, high
                                16 bits extend size to 48 bits). Directory:
                                nodeStartIndex, count, reserved. Entry 0 is
                                the root directory; a directory's children
                                occupy indices [nodeStartIndex,
                                nodeStartIndex+count).
  [ meta directory / meta data sections ]  (currently always empty)
  [ footer, 144 bytes ]         6 x { u64 offset; u64 size } for the sections
                                above (in the order compressedData,
                                offsetRecords, names, fileTree, metaDirectory,
                                metaData), u8 integrityHash[32], u64 totalSize,
                                u32 version (0x61bf3a01), u32 magic
                                (0x169f52d6).

The integrity hash is SHA-256 over the whole file up to the footer, followed
by the serialized footer with its hash field zeroed.

The writer (``ZarWriter`` / ``pack``) mirrors the reference
``ZArchiveWriter``: 64 KiB blocks zstd-compressed at level 6 (stored raw
when compression does not shrink them), the final partial block
zero-padded to a full block, directory entries sorted case-insensitively
(ASCII fold, bytewise) within each directory, and the tree serialized
breadth-first. Output is deterministic for a given source tree.

Zstd support comes from the stdlib ``compression.zstd`` module on
Python 3.14+, or the ``zstandard`` package on older interpreters (which
the wheel pulls in automatically via an environment marker). The
module-level bool ``HAVE_ZSTD`` reports availability (constructing a
``ZarReader`` or ``ZarWriter`` without either raises ``ZarNativeError``).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import os
import threading
import struct
from collections import OrderedDict, deque

try:
    from compression import zstd as _zstd          # Python 3.14+

    def _zstd_decompress(raw):
        return _zstd.decompress(raw)

    def _zstd_compress(raw):
        return _zstd.compress(raw, _ZSTD_LEVEL)

    HAVE_ZSTD = True
except ImportError:  # pragma: no cover - depends on interpreter version
    try:
        import zstandard as _zstandard             # pip, < 3.14

        _zstd_tls = threading.local()

        def _zstd_decompress(raw):
            return _zstandard.ZstdDecompressor().decompress(
                raw, max_output_size=64 * 1024)

        def _zstd_compress(raw):
            comp = getattr(_zstd_tls, "c", None)
            if comp is None:
                comp = _zstd_tls.c = _zstandard.ZstdCompressor(
                    level=_ZSTD_LEVEL)
            return comp.compress(raw)

        HAVE_ZSTD = True
    except ImportError:
        _zstd_decompress = None
        _zstd_compress = None
        HAVE_ZSTD = False

__all__ = ["ZarReader", "ZarWriter", "ZarNativeError", "pack", "HAVE_ZSTD"]

_FOOTER_SIZE = 144
_FOOTER_MAGIC = 0x169F52D6
_FOOTER_VERSION1 = 0x61BF3A01
_BLOCK_SIZE = 64 * 1024
_ENTRIES_PER_RECORD = 16
_RECORD_SIZE = 8 + 2 * _ENTRIES_PER_RECORD  # 40
_ENTRY_SIZE = 16
_ROOT_NAME_OFFSET = 0x7FFFFFFF
_ZSTD_LEVEL = 6            # matches the reference writer (StoreBlock, level 6)
_MAX_NAME_BYTES = 128      # the reference reader misparses names >= 128 bytes
_MAX_FILE_FIELD = 1 << 48  # file offset/size are 48-bit in FileDirectoryEntry
_PACK_CHUNK = 1 << 20

_FOOTER_STRUCT = struct.Struct(">12Q32sQ2I")
_RECORD_STRUCT = struct.Struct(">Q16H")
_ENTRY_STRUCT = struct.Struct(">4I")

assert _FOOTER_STRUCT.size == _FOOTER_SIZE
assert _RECORD_STRUCT.size == _RECORD_SIZE
assert _ENTRY_STRUCT.size == _ENTRY_SIZE


class ZarNativeError(Exception):
    """Raised for any structural, I/O, or integrity problem with a .zar file."""


def _lower_ascii(s: str) -> str:
    # The reference implementation only case-folds ASCII A-Z.
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


class _ZarFileHandle(io.RawIOBase):
    """Read-only, seekable file-like view of a single archived file."""

    def __init__(self, reader: "ZarReader", file_offset: int, file_size: int):
        self._reader = reader
        self._file_offset = file_offset
        self._file_size = file_size
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new_pos = offset
        elif whence == os.SEEK_CUR:
            new_pos = self._pos + offset
        elif whence == os.SEEK_END:
            new_pos = self._file_size + offset
        else:
            raise ValueError("invalid whence: %r" % (whence,))
        if new_pos < 0:
            raise ValueError("negative seek position")
        self._pos = new_pos
        return self._pos

    def readinto(self, b) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        remaining = self._file_size - self._pos
        if remaining <= 0:
            return 0
        n = min(len(b), remaining)
        data = self._reader._pread(self._file_offset + self._pos, n)
        b[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def size(self) -> int:
        return self._file_size


class ZarReader:
    """Reader for ZArchive (.zar) files.

    Usage::

        with ZarReader(path) as zr:
            for name, size in zr.files():
                ...
    """

    def __init__(self, path, cache_blocks: int = 64):
        if not HAVE_ZSTD:
            raise ZarNativeError(
                "compression.zstd is unavailable (Python 3.14+ required)"
            )
        self._path = os.fspath(path)
        self._cache_limit = max(2, int(cache_blocks))
        self._cache: "OrderedDict[int, bytes]" = OrderedDict()
        self._fh = None
        fh = open(self._path, "rb")
        try:
            self._parse(fh)
        except ZarNativeError:
            fh.close()
            raise
        except Exception as exc:
            fh.close()
            raise ZarNativeError("failed to parse %s: %s" % (self._path, exc)) from exc
        self._fh = fh

    # ------------------------------------------------------------------ setup

    def _parse(self, fh) -> None:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        if file_size <= _FOOTER_SIZE:
            raise ZarNativeError("file too small to be a ZArchive")
        fh.seek(file_size - _FOOTER_SIZE)
        footer_raw = fh.read(_FOOTER_SIZE)
        if len(footer_raw) != _FOOTER_SIZE:
            raise ZarNativeError("short read on footer")
        fields = _FOOTER_STRUCT.unpack(footer_raw)
        sections = [(fields[i], fields[i + 1]) for i in range(0, 12, 2)]
        integrity_hash = fields[12]
        total_size = fields[13]
        version = fields[14]
        magic = fields[15]
        if magic != _FOOTER_MAGIC:
            raise ZarNativeError("bad footer magic 0x%08x" % magic)
        if version != _FOOTER_VERSION1:
            raise ZarNativeError("unsupported version 0x%08x" % version)
        if total_size != file_size:
            raise ZarNativeError(
                "footer totalSize %d does not match file size %d"
                % (total_size, file_size)
            )
        for off, size in sections:
            if off + size > file_size:
                raise ZarNativeError("section exceeds file bounds")
        (
            (self._cdata_off, self._cdata_size),
            (rec_off, rec_size),
            (names_off, names_size),
            (tree_off, tree_size),
            _meta_dir,
            _meta_data,
        ) = sections
        if rec_size > 0xFFFFFFFF or names_size > 0x7FFFFFFF or tree_size > 0xFFFFFFFF:
            raise ZarNativeError("section size out of range")
        if rec_size == 0 or rec_size % _RECORD_SIZE != 0:
            raise ZarNativeError("invalid offset-record section size %d" % rec_size)
        if tree_size == 0 or tree_size % _ENTRY_SIZE != 0:
            raise ZarNativeError("invalid file-tree section size %d" % tree_size)

        self._file_size = file_size
        self._integrity_hash = integrity_hash
        self._footer_raw = footer_raw

        # Offset records -> per-block (offset within section, compressed size).
        fh.seek(rec_off)
        rec_raw = fh.read(rec_size)
        if len(rec_raw) != rec_size:
            raise ZarNativeError("short read on offset records")
        blk_off: list[int] = []
        blk_csize: list[int] = []
        for base_and_sizes in _RECORD_STRUCT.iter_unpack(rec_raw):
            offset = base_and_sizes[0]
            for stored in base_and_sizes[1:]:
                csize = stored + 1
                blk_off.append(offset)
                blk_csize.append(csize)
                offset += csize
        self._blk_off = blk_off
        self._blk_csize = blk_csize
        self._block_count = len(blk_off)

        # Name table.
        fh.seek(names_off)
        self._name_table = fh.read(names_size)
        if len(self._name_table) != names_size:
            raise ZarNativeError("short read on name table")

        # File tree.
        fh.seek(tree_off)
        tree_raw = fh.read(tree_size)
        if len(tree_raw) != tree_size:
            raise ZarNativeError("short read on file tree")
        self._tree = list(_ENTRY_STRUCT.iter_unpack(tree_raw))
        w0 = self._tree[0][0]
        if w0 & 0x80000000:
            raise ZarNativeError("first file-tree entry is not a directory")
        if (w0 & 0x7FFFFFFF) != _ROOT_NAME_OFFSET and self._get_name(w0 & 0x7FFFFFFF):
            raise ZarNativeError("root node must not have a name")

    # -------------------------------------------------------------- lifecycle

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._cache.clear()

    def __enter__(self) -> "ZarReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------- name table

    def _get_name(self, name_offset: int) -> str:
        table = self._name_table
        if name_offset == _ROOT_NAME_OFFSET or name_offset >= len(table):
            return ""
        header = table[name_offset]
        length = header & 0x7F
        if header & 0x80:
            # Extended 2-byte header (writer convention: second byte holds
            # bits 7..14 of the length; the reference reader has a bug here
            # and re-reads the first byte, but the writer's layout is
            # authoritative).
            if name_offset + 1 >= len(table):
                return ""
            length |= table[name_offset + 1] << 7
            name_offset += 2
        else:
            name_offset += 1
        if name_offset + length > len(table):
            return ""
        return table[name_offset : name_offset + length].decode(
            "utf-8", "surrogateescape"
        )

    # -------------------------------------------------------------- file tree

    @staticmethod
    def _entry_is_file(entry) -> bool:
        return bool(entry[0] & 0x80000000)

    @staticmethod
    def _entry_file_offset(entry) -> int:
        return entry[1] | ((entry[3] & 0xFFFF) << 32)

    @staticmethod
    def _entry_file_size(entry) -> int:
        return entry[2] | ((entry[3] & 0xFFFF0000) << 16)

    def _walk(self):
        """Yield (path, entry_index, is_file) depth-first in stored order."""
        tree = self._tree
        stack = [("", 0)]
        while stack:
            prefix, idx = stack.pop()
            entry = tree[idx]
            start = entry[1]
            count = entry[2]
            if start + count > len(tree):
                raise ZarNativeError("directory child range out of bounds")
            # push in reverse so children are yielded in stored order
            pending = []
            for child_idx in range(start, start + count):
                child = tree[child_idx]
                name = self._get_name(child[0] & 0x7FFFFFFF)
                if not name:
                    raise ZarNativeError(
                        "file-tree entry %d has an invalid name" % child_idx
                    )
                child_path = prefix + name if not prefix else prefix + "/" + name
                if self._entry_is_file(child):
                    yield child_path, child_idx, True
                else:
                    yield child_path, child_idx, False
                    pending.append((child_path, child_idx))
            for item in reversed(pending):
                stack.append(item)

    def _lookup(self, path: str) -> int:
        """Resolve a path to a file-tree index (reference LookUp semantics)."""
        tree = self._tree
        current = 0
        for part in str(path).replace("\\", "/").split("/"):
            if not part:
                continue
            entry = tree[current]
            if self._entry_is_file(entry):
                raise ZarNativeError("not found in archive: %r" % (path,))
            want = _lower_ascii(part)
            start, count = entry[1], entry[2]
            for child_idx in range(start, start + count):
                child = tree[child_idx]
                name = self._get_name(child[0] & 0x7FFFFFFF)
                if _lower_ascii(name) == want:
                    current = child_idx
                    break
            else:
                raise ZarNativeError("not found in archive: %r" % (path,))
        return current

    # ------------------------------------------------------------ block layer

    def _load_block(self, block_index: int) -> bytes:
        cache = self._cache
        data = cache.get(block_index)
        if data is not None:
            cache.move_to_end(block_index)
            return data
        if block_index >= self._block_count:
            raise ZarNativeError("block index %d out of range" % block_index)
        offset = self._blk_off[block_index]
        csize = self._blk_csize[block_index]
        if offset + csize > self._cdata_size:
            raise ZarNativeError("block %d outside compressed data" % block_index)
        fh = self._fh
        if fh is None:
            raise ZarNativeError("reader is closed")
        fh.seek(self._cdata_off + offset)
        raw = fh.read(csize)
        if len(raw) != csize:
            raise ZarNativeError("short read on block %d" % block_index)
        if csize == _BLOCK_SIZE:
            data = raw  # stored uncompressed (compression did not help)
        else:
            try:
                data = _zstd_decompress(raw)
            except Exception as exc:
                raise ZarNativeError(
                    "zstd decompression failed for block %d: %s" % (block_index, exc)
                ) from exc
            if len(data) != _BLOCK_SIZE:
                raise ZarNativeError(
                    "block %d decompressed to %d bytes, expected %d"
                    % (block_index, len(data), _BLOCK_SIZE)
                )
        cache[block_index] = data
        if len(cache) > self._cache_limit:
            cache.popitem(last=False)
        return data

    def _iter_range(self, virtual_offset: int, length: int):
        while length > 0:
            block_index, block_offset = divmod(virtual_offset, _BLOCK_SIZE)
            step = min(length, _BLOCK_SIZE - block_offset)
            block = self._load_block(block_index)
            if step == _BLOCK_SIZE:
                yield block
            else:
                yield block[block_offset : block_offset + step]
            virtual_offset += step
            length -= step

    def _pread(self, virtual_offset: int, length: int) -> bytes:
        return b"".join(self._iter_range(virtual_offset, length))

    # ------------------------------------------------------------- public API

    def files(self):
        """Return a list of (path, size) for every file in the archive."""
        out = []
        for path, idx, is_file in self._walk():
            if is_file:
                out.append((path, self._entry_file_size(self._tree[idx])))
        return out

    def directories(self):
        """Return a list of directory paths (excluding the root)."""
        return [path for path, _idx, is_file in self._walk() if not is_file]

    def file_size(self, path: str) -> int:
        entry = self._tree[self._lookup(path)]
        if not self._entry_is_file(entry):
            raise ZarNativeError("not a file: %r" % (path,))
        return self._entry_file_size(entry)

    def read_iter(self, path: str, chunk_size: int = _BLOCK_SIZE):
        """Yield the content of an archived file as a sequence of chunks.

        Chunks are naturally 64 KiB block-sized; ``chunk_size`` larger than a
        block coalesces whole blocks per yield.
        """
        entry = self._tree[self._lookup(path)]
        if not self._entry_is_file(entry):
            raise ZarNativeError("not a file: %r" % (path,))
        offset = self._entry_file_offset(entry)
        size = self._entry_file_size(entry)
        if chunk_size <= _BLOCK_SIZE:
            yield from self._iter_range(offset, size)
            return
        pending = []
        pending_len = 0
        for piece in self._iter_range(offset, size):
            pending.append(piece)
            pending_len += len(piece)
            if pending_len >= chunk_size:
                yield b"".join(pending)
                pending = []
                pending_len = 0
        if pending:
            yield b"".join(pending)

    def open_read(self, path: str) -> _ZarFileHandle:
        """Open an archived file as a read-only, seekable file-like object."""
        entry = self._tree[self._lookup(path)]
        if not self._entry_is_file(entry):
            raise ZarNativeError("not a file: %r" % (path,))
        return _ZarFileHandle(
            self, self._entry_file_offset(entry), self._entry_file_size(entry)
        )

    def extract_all(self, out_dir) -> int:
        """Extract the archive under out_dir. Returns the number of files."""
        out_dir = os.fspath(out_dir)
        os.makedirs(out_dir, exist_ok=True)
        count = 0
        for path, idx, is_file in self._walk():
            dest = os.path.join(out_dir, *path.split("/"))
            if not is_file:
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            entry = self._tree[idx]
            offset = self._entry_file_offset(entry)
            size = self._entry_file_size(entry)
            with open(dest, "wb") as out:
                for piece in self._iter_range(offset, size):
                    out.write(piece)
            count += 1
        return count

    def hash_walk(self, algorithm: str = "sha1", progress=None):
        """Return {path: hexdigest} for every file, streamed without writing."""
        result = {}
        grand = sum(sz for _n, sz in self.files()) or 1
        done = 0
        for path, idx, is_file in self._walk():
            if not is_file:
                continue
            entry = self._tree[idx]
            hasher = hashlib.new(algorithm)
            for piece in self._iter_range(
                self._entry_file_offset(entry), self._entry_file_size(entry)
            ):
                hasher.update(piece)
                done += len(piece)
                if progress:
                    progress(done, grand)
            result[path] = hasher.hexdigest()
        return result

    def verify_integrity(self, chunk_size: int = 8 * 1024 * 1024,
                         progress=None) -> bool:
        """Check the embedded SHA-256 over the whole archive.

        The hash covers every byte up to the footer, plus the footer itself
        with its 32-byte hash field zeroed. Returns True when it matches.
        """
        fh = self._fh
        if fh is None:
            raise ZarNativeError("reader is closed")
        hasher = hashlib.sha256()
        remaining = self._file_size - _FOOTER_SIZE
        fh.seek(0)
        total = self._file_size - _FOOTER_SIZE
        while remaining > 0:
            piece = fh.read(min(chunk_size, remaining))
            if not piece:
                raise ZarNativeError("short read while hashing archive")
            hasher.update(piece)
            remaining -= len(piece)
            if progress:
                progress(total - remaining, total)
        # Footer with the integrityHash field (bytes 96..128) zeroed.
        hasher.update(self._footer_raw[:96])
        hasher.update(b"\x00" * 32)
        hasher.update(self._footer_raw[128:])
        return hasher.digest() == self._integrity_hash


# ---------------------------------------------------------------------- writer

# ASCII-only case-fold table, mirroring the reference CompareNodeName().
_FOLD_TABLE = bytes(c + 32 if 0x41 <= c <= 0x5A else c for c in range(256))


def _fold_bytes(raw: bytes) -> bytes:
    return raw.translate(_FOLD_TABLE)


class _PathNode:
    __slots__ = (
        "name", "is_file", "children", "lookup",
        "file_offset", "file_size", "start_index", "name_off",
    )

    def __init__(self, name: bytes, is_file: bool):
        self.name = name
        self.is_file = is_file
        self.children: list["_PathNode"] = []
        self.lookup: dict[bytes, "_PathNode"] = {}  # folded name -> node
        self.file_offset = 0
        self.file_size = 0
        self.start_index = 0
        self.name_off = 0


_ZSTD_POOL = None


def _zstd_pool():
    global _ZSTD_POOL
    if _ZSTD_POOL is None:
        _ZSTD_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, os.cpu_count() or 1),
            thread_name_prefix="zarzstd")
    return _ZSTD_POOL


def _compress_or_store(block):
    comp = _zstd_compress(block)
    return block if len(comp) >= _BLOCK_SIZE else comp


class ZarWriter:
    """Streaming writer for ZArchive (.zar) files.

    Usage::

        with ZarWriter(out_path) as zw:
            zw.make_dir("dir")
            zw.start_file("dir/file.bin")
            zw.append(data)
            zw.finalize()

    File data is consumed incrementally (memory use is O(64 KiB block)
    plus the metadata tree). Leaving the ``with`` block without a
    successful ``finalize()`` deletes the partial output file.
    """

    def __init__(self, path):
        if not HAVE_ZSTD:
            raise ZarNativeError(
                "compression.zstd is unavailable (Python 3.14+ required)"
            )
        self._path = os.fspath(path)
        self._root = _PathNode(b"", False)
        self._current: "_PathNode | None" = None
        self._buffer = bytearray()
        self._records: list[tuple[int, list[int]]] = []
        self._block_count = 0
        self._out_offset = 0
        self._input_offset = 0
        self._sha = hashlib.sha256()
        self._pending: deque = deque()
        self._finalized = False
        try:
            self._fh = open(self._path, "xb")
        except FileExistsError:
            raise ZarNativeError(
                "output already exists: %s" % self._path
            ) from None

    # -------------------------------------------------------------- lifecycle

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def abort(self) -> None:
        """Close and delete the (unfinished) output file."""
        self.close()
        if not self._finalized:
            try:
                os.unlink(self._path)
            except OSError:
                pass

    def __enter__(self) -> "ZarWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None or not self._finalized:
            self.abort()
        else:
            self.close()

    # -------------------------------------------------------------- path/tree

    def _check_open(self) -> None:
        if self._fh is None or self._finalized:
            raise ZarNativeError("writer is closed")

    @staticmethod
    def _split(path: str) -> list[bytes]:
        parts = []
        for part in str(path).replace("\\", "/").split("/"):
            if not part:
                continue
            raw = part.encode("utf-8", "surrogateescape")
            if len(raw) >= _MAX_NAME_BYTES:
                raise ZarNativeError(
                    "name %r is %d bytes; the ZArchive reference reader "
                    "breaks on names of 128+ bytes" % (part, len(raw))
                )
            parts.append(raw)
        return parts

    def _ensure_dir(self, parts: list[bytes]) -> _PathNode:
        node = self._root
        for raw in parts:
            key = _fold_bytes(raw)
            child = node.lookup.get(key)
            if child is None:
                child = _PathNode(raw, False)
                node.children.append(child)
                node.lookup[key] = child
            elif child.is_file:
                raise ZarNativeError(
                    "path component %r already exists as a file"
                    % raw.decode("utf-8", "surrogateescape")
                )
            node = child
        return node

    def make_dir(self, path: str) -> None:
        """Create a directory (and any missing parents)."""
        self._check_open()
        self._ensure_dir(self._split(path))

    def start_file(self, path: str) -> None:
        """Create a file entry; subsequent append() calls add its content."""
        self._check_open()
        parts = self._split(path)
        if not parts:
            raise ZarNativeError("empty file path")
        parent = self._ensure_dir(parts[:-1])
        raw = parts[-1]
        key = _fold_bytes(raw)
        if key in parent.lookup:
            raise ZarNativeError(
                "duplicate name %r (ZArchive names are case-insensitive)"
                % raw.decode("utf-8", "surrogateescape")
            )
        node = _PathNode(raw, True)
        node.file_offset = self._input_offset
        parent.children.append(node)
        parent.lookup[key] = node
        self._current = node

    def append(self, data) -> None:
        """Append bytes to the file opened by the last start_file()."""
        self._check_open()
        if self._current is None:
            raise ZarNativeError("append() without start_file()")
        self._append_raw(data)
        self._current.file_size += len(memoryview(data))

    # ------------------------------------------------------------ block layer

    def _write(self, data) -> None:
        self._fh.write(data)
        self._sha.update(data)
        self._out_offset += len(data)

    def _store_block(self, block: bytes) -> None:
        # Compression runs on a bounded thread pool; blocks are written
        # strictly in submission order, so the archive stays byte-identical
        # to the sequential writer (each 64 KiB block is an independent
        # single-shot zstd compression - determinism does not depend on
        # which thread ran it).
        self._pending.append(
            _zstd_pool().submit(_compress_or_store, block))
        while len(self._pending) > 32:
            self._flush_one()

    def _flush_one(self) -> None:
        comp = self._pending.popleft().result()
        base = self._out_offset
        sub = self._block_count % _ENTRIES_PER_RECORD
        if sub == 0:
            # unused trailing entries stay 0 (reference value-initializes)
            self._records.append((base, [0] * _ENTRIES_PER_RECORD))
        self._records[-1][1][sub] = len(comp) - 1
        self._write(comp)
        self._block_count += 1

    def _drain_blocks(self) -> None:
        while self._pending:
            self._flush_one()

    def _append_raw(self, data) -> None:
        view = memoryview(data)
        self._input_offset += len(view)
        buf = self._buffer
        while len(view):
            if not buf and len(view) >= _BLOCK_SIZE:
                self._store_block(bytes(view[:_BLOCK_SIZE]))
                view = view[_BLOCK_SIZE:]
                continue
            take = min(_BLOCK_SIZE - len(buf), len(view))
            buf += view[:take]
            view = view[take:]
            if len(buf) == _BLOCK_SIZE:
                self._store_block(bytes(buf))
                del buf[:]

    # --------------------------------------------------------------- finalize

    def finalize(self) -> None:
        """Write all metadata sections and the footer."""
        self._check_open()
        self._current = None
        if self._buffer:
            # zero-pad the final partial block to a full 64 KiB block
            self._buffer += b"\x00" * (_BLOCK_SIZE - len(self._buffer))
            self._store_block(bytes(self._buffer))
            del self._buffer[:]
        self._drain_blocks()
        if self._block_count == 0:
            # Divergence from the reference writer, which emits an empty
            # offset-record section here - one that every reader (including
            # the reference one) then rejects. Emit a single zero block so
            # a data-less archive is still readable.
            self._store_block(b"\x00" * _BLOCK_SIZE)
            self._drain_blocks()
        cdata_size = self._out_offset
        while self._out_offset % 8:
            self._write(b"\x00")

        order = self._assign_indices()
        name_table = self._build_name_table(order)

        rec_off = self._out_offset
        for base, sizes in self._records:
            self._write(_RECORD_STRUCT.pack(base, *sizes))
        rec_size = self._out_offset - rec_off

        names_off = self._out_offset
        self._write(bytes(name_table))

        tree_off = self._out_offset
        for node in order:
            self._write(_ENTRY_STRUCT.pack(*self._entry_words(node)))
        tree_size = self._out_offset - tree_off

        meta_off = self._out_offset
        footer = _FOOTER_STRUCT.pack(
            0, cdata_size, rec_off, rec_size, names_off, len(name_table),
            tree_off, tree_size, meta_off, 0, meta_off, 0,
            b"\x00" * 32, self._out_offset + _FOOTER_SIZE,
            _FOOTER_VERSION1, _FOOTER_MAGIC,
        )
        # Integrity hash: every preceding byte plus the footer with its
        # hash field zeroed; the written footer then carries the digest.
        self._sha.update(footer)
        digest = self._sha.digest()
        self._fh.write(footer[:96] + digest + footer[128:])
        self._fh.flush()
        self._finalized = True

    def _assign_indices(self) -> list[_PathNode]:
        """Sort children and assign BFS indices; returns nodes in BFS order."""
        order: list[_PathNode] = []
        queue = deque([self._root])
        current_index = 1  # root node is at index 0
        while queue:
            node = queue.popleft()
            order.append(node)
            if node.is_file:
                continue
            node.children.sort(key=lambda n: (_fold_bytes(n.name), n.name))
            node.start_index = current_index
            current_index += len(node.children)
            queue.extend(node.children)
        return order

    def _build_name_table(self, order: list[_PathNode]) -> bytearray:
        table = bytearray()
        offsets: dict[bytes, int] = {}
        for node in order[1:]:  # the root is unnamed
            off = offsets.get(node.name)
            if off is None:
                off = len(table)
                if off >= _ROOT_NAME_OFFSET:
                    raise ZarNativeError("name table exceeds 31-bit offsets")
                offsets[node.name] = off
                table.append(len(node.name))  # always < 128: 1-byte header
                table += node.name
            node.name_off = off
        return table

    def _entry_words(self, node: _PathNode) -> tuple:
        if node is self._root:
            w0 = _ROOT_NAME_OFFSET
        else:
            w0 = node.name_off | (0x80000000 if node.is_file else 0)
        if node.is_file:
            offset, size = node.file_offset, node.file_size
            if offset >= _MAX_FILE_FIELD or size >= _MAX_FILE_FIELD:
                raise ZarNativeError("file exceeds the 48-bit offset/size limit")
            return (
                w0,
                offset & 0xFFFFFFFF,
                size & 0xFFFFFFFF,
                ((size >> 16) & 0xFFFF0000) | ((offset >> 32) & 0xFFFF),
            )
        return w0, node.start_index, len(node.children), 0


def pack(src_dir, zar_path, progress=None) -> int:
    """Pack a directory tree into a new .zar file. Returns the file count.

    Deterministic: entries are packed in the archive's canonical order
    (per-directory bytewise sort on ASCII-case-folded names), so packing
    the same tree twice yields byte-identical archives.
    """
    src_dir = os.fspath(src_dir)
    if not os.path.isdir(src_dir):
        raise ZarNativeError("not a directory: %s" % src_dir)
    grand = 0
    for _dp, _dn, _fs in os.walk(src_dir):
        for _f in _fs:
            grand += os.path.getsize(os.path.join(_dp, _f))
    state = [0]
    with ZarWriter(zar_path) as zw:
        count = _pack_tree(zw, src_dir, "", progress=progress,
                           grand=grand, state=state)
        zw.finalize()
    return count


def _pack_tree(zw: ZarWriter, dir_path: str, arc_prefix: str,
               progress=None, grand=0, state=None) -> int:
    def sort_key(entry):
        raw = entry.name.encode("utf-8", "surrogateescape")
        return (_fold_bytes(raw), raw)

    with os.scandir(dir_path) as it:
        entries = sorted(it, key=sort_key)
    count = 0
    for entry in entries:
        if "\\" in entry.name:
            raise ZarNativeError(
                "cannot pack %r: ZArchive treats '\\' as a path separator"
                % entry.path
            )
        arc = arc_prefix + "/" + entry.name if arc_prefix else entry.name
        if entry.is_dir(follow_symlinks=True):
            zw.make_dir(arc)
            count += _pack_tree(zw, entry.path, arc, progress=progress,
                                grand=grand, state=state)
        elif entry.is_file(follow_symlinks=True):
            zw.start_file(arc)
            with open(entry.path, "rb") as f:
                while True:
                    chunk = f.read(_PACK_CHUNK)
                    if not chunk:
                        break
                    zw.append(chunk)
                    if progress and state is not None:
                        state[0] += len(chunk)
                        progress(state[0], grand)
            count += 1
        else:
            raise ZarNativeError("cannot pack special file: %s" % entry.path)
    return count


def cli(argv=None):
    """Standalone CLI:  xv-zar {list|extract|hash|verify|pack} archive.zar [dir]"""
    import argparse
    import sys as _sys
    ap = argparse.ArgumentParser(
        prog="xv-zar",
        description="Pure-Python ZArchive (.zar) tool: list, extract, hash, "
                    "integrity-verify, or pack without the reference binary.")
    ap.add_argument("action", choices=["list", "extract", "hash", "verify",
                                       "pack"])
    ap.add_argument("archive")
    ap.add_argument("directory", nargs="?",
                    help="output dir for extract, source dir for pack")
    a = ap.parse_args(argv)
    try:
        if a.action == "pack":
            if not a.directory:
                ap.error("pack needs a source directory")
            print("packed %d files" % pack(a.directory, a.archive))
            return 0
        with ZarReader(a.archive) as zr:
            if a.action == "list":
                for path, size in zr.files():
                    print("%12d  %s" % (size, path))
            elif a.action == "extract":
                if not a.directory:
                    ap.error("extract needs an output directory")
                zr.extract_all(a.directory)
                print("extracted %d files" % len(zr.files()))
            elif a.action == "hash":
                for path, digest in sorted(zr.hash_walk().items()):
                    print("%s  %s" % (digest, path))
            elif a.action == "verify":
                ok = zr.verify_integrity()
                print("integrity: %s" % ("OK" if ok else "FAILED"))
                return 0 if ok else 1
    except ZarNativeError as e:
        print("ERROR: %s" % e, file=_sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(cli())
