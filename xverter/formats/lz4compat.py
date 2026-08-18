"""LZ4 raw-block codec with optional python-lz4 acceleration.

CCI and CSO (Xbox dialect) both store 2048-byte sectors as raw LZ4
*blocks* (lz4_Block_format.md): a sequence of
[token][literal-length ext][literals][offset u16le][match-length ext]
records with no frame header, no checksums and no embedded size.

Decompression works everywhere: `lz4.block` is used when the python-lz4
package is installed (HAVE_LZ4 is True), and an original pure-Python
decoder is the fallback, so reading CCI/CSO images never needs a
third-party package. Compression is only available through python-lz4 -
a pure-Python LZ4-HC encoder would be uselessly slow - so
compress_block() raises Lz4Missing with an install hint when the
package is absent.

Setting HAVE_LZ4 = False at runtime forces the pure-Python read path
(used by the test suite); compression then reports itself unavailable.
"""

try:
    import lz4.block as _lz4block
except ImportError:  # pragma: no cover - environment dependent
    _lz4block = None

HAVE_LZ4 = _lz4block is not None

INSTALL_HINT = ("LZ4 compression requires the python-lz4 package: "
                "pip install lz4  (bundled with the wheel; needed manually for the .pyz)")

# Both reference encoders use LZ4-HC at maximum level: XGDTool calls
# LZ4_compress_HC(..., 12) for CCI, stellar's ciso.py uses lz4.frame
# COMPRESSIONLEVEL_MAX (16, clamped to LZ4HC_CLEVEL_MAX = 12 by liblz4)
# for CSO. Level 12 therefore reproduces both.
HC_LEVEL = 12


class Lz4Error(Exception):
    """A block failed to decode (corrupt data or wrong expected size)."""


class Lz4Missing(Exception):
    """python-lz4 is required for this operation but is not installed."""


_POOL = None
#: blocks per pool task - 8 MiB of work, keeping per-task
#: coordination negligible against the compression itself.
BATCH_CHUNK = 4096


def _pool():
    global _POOL
    if _POOL is None:
        import concurrent.futures
        import os as _os
        # Measured, not guessed: 2 KB LZ4-HC blocks are small enough
        # that GIL hand-off dominates past a handful of threads. On a
        # 24-thread machine, 16 workers x 256-block tasks ran at 453
        # MB/s while 4 workers x 4096-block tasks ran at 725 MB/s -
        # more threads were actively slower. Output is unaffected:
        # blocks compress independently and are reassembled in order.
        _POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(4, _os.cpu_count() or 4),
            thread_name_prefix="lz4hc")
    return _POOL


def compress_batch(blocks, chunk=BATCH_CHUNK):
    """Order-preserving parallel compress_block over a sequence of raw
    blocks. Output is byte-for-byte identical to sequential calls (same
    LZ4-HC level, block-independent compression); parallelism is real
    because python-lz4 releases the GIL. Blocks are grouped into runs of
    `chunk` per task so per-future overhead stays negligible at
    millions of 2KB blocks per image."""
    if not HAVE_LZ4:
        raise Lz4Missing(INSTALL_HINT)
    if len(blocks) <= chunk:
        return [compress_block(b) for b in blocks]
    out = []
    for res in compress_batch_iter(blocks, chunk):
        out.extend(res)
    return out


def compress_batch_iter(blocks, chunk=BATCH_CHUNK):
    """compress_batch's output, yielded run by run in submission order,
    so a caller can start writing before the whole batch is done. Same
    bytes as compress_batch - only the delivery is incremental."""
    if not HAVE_LZ4:
        raise Lz4Missing(INSTALL_HINT)
    if len(blocks) <= chunk:
        yield [compress_block(b) for b in blocks]
        return
    parts = [blocks[i:i + chunk] for i in range(0, len(blocks), chunk)]

    # compress_block is bound out of the loop: at millions of 2 KB
    # blocks its Python call frame cost 2.2s of a 21s image build.
    _c = _raw_compress()

    def run(part):
        return [_c(b) for b in part]

    for res in _pool().map(run, parts):
        yield res


def _raw_compress():
    """The bare lz4 entry point with this project's fixed settings, so
    hot loops can call it without a Python wrapper frame in between."""
    if not HAVE_LZ4:
        raise Lz4Missing(INSTALL_HINT)
    import lz4.block as _b
    _compress = _b.compress

    def go(data):
        return _compress(data, mode="high_compression",
                         compression=HC_LEVEL, store_size=False)
    return go


def compress_block(data):
    """LZ4-HC compress one raw block (no size header, no frame).

    Requires python-lz4; raises Lz4Missing with an install hint
    otherwise.
    """
    if not HAVE_LZ4 or _lz4block is None:
        raise Lz4Missing(INSTALL_HINT)
    return _lz4block.compress(bytes(data), mode="high_compression",
                              compression=HC_LEVEL, store_size=False)


def decode_block(data, uncompressed_size):
    """Decode one raw LZ4 block to exactly uncompressed_size bytes.

    Uses lz4.block when available (and HAVE_LZ4 has not been forced
    off), else the pure-Python decoder. Raises Lz4Error if the block is
    corrupt or does not decode to the expected size.
    """
    if HAVE_LZ4 and _lz4block is not None:
        try:
            out = _lz4block.decompress(bytes(data),
                                       uncompressed_size=uncompressed_size)
        except Exception as e:
            raise Lz4Error("LZ4 block decode failed: %s" % e)
    else:
        out = _decode_block_py(data, uncompressed_size)
    if len(out) != uncompressed_size:
        raise Lz4Error("LZ4 block decoded to %d bytes, expected %d"
                       % (len(out), uncompressed_size))
    return out


def _decode_block_py(src, uncompressed_size):
    """Original pure-Python LZ4 raw-block decoder.

    Follows lz4_Block_format.md: token high nibble = literal length
    (15 -> extended by 0xFF-terminated byte run), literals, then a
    little-endian u16 match offset and token low nibble + 4 = match
    length (15+4 -> extended the same way). The final sequence of a
    block is literals-only. Overlapping matches replicate the pattern.
    """
    if uncompressed_size == 0:
        if len(src) != 0 and bytes(src) != b"\x00":
            # An empty block is either 0 bytes or a single 0x00 token.
            raise Lz4Error("nonempty LZ4 block for empty output")
        return b""
    src = bytes(src)
    n = len(src)
    if n == 0:
        raise Lz4Error("empty LZ4 block")
    dst = bytearray()
    i = 0
    try:
        while i < n:
            token = src[i]
            i += 1
            # -- literals --
            length = token >> 4
            if length == 15:
                while True:
                    b = src[i]
                    i += 1
                    length += b
                    if b != 255:
                        break
            if length:
                if i + length > n:
                    raise Lz4Error("literal run past end of block")
                dst += src[i:i + length]
                i += length
            if i == n:
                break  # last sequence: literals only, no match
            # -- match --
            if i + 2 > n:
                raise Lz4Error("truncated match offset")
            offset = src[i] | (src[i + 1] << 8)
            i += 2
            if offset == 0:
                raise Lz4Error("invalid zero match offset")
            length = (token & 0x0F) + 4
            if (token & 0x0F) == 15:
                while True:
                    b = src[i]
                    i += 1
                    length += b
                    if b != 255:
                        break
            start = len(dst) - offset
            if start < 0:
                raise Lz4Error("match offset before start of output")
            if offset >= length:
                dst += dst[start:start + length]
            else:
                # Overlapping match: replicate the offset-sized pattern.
                pattern = dst[start:]
                reps = (length // offset) + 2
                dst += (bytes(pattern) * reps)[:length]
    except IndexError:
        raise Lz4Error("truncated LZ4 block")
    return bytes(dst)


if __name__ == "__main__":  # tiny self-test
    import os
    for size in (0, 1, 17, 2048, 65536):
        for blob in (b"\x00" * size, os.urandom(size),
                     (b"abc123" * (size // 6 + 1))[:size]):
            if not HAVE_LZ4:
                print("python-lz4 not installed; cannot self-test encode")
                raise SystemExit(0)
            comp = compress_block(blob)
            assert decode_block(comp, size) == blob
            assert _decode_block_py(comp, size) == blob
    print("lz4compat self-test OK")
