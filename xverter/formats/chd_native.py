"""Native CHD v5 reader - no chdman.

CHD is MAME's format and chdman is its living definition, so this reads
what chdman writes and is held to that standard: the bytes this module
produces for an image must equal the bytes `chdman extractdvd` produces
for the same file, or this module is wrong.

Scope is v5, which is what chdman has written for over a decade. v4 and
earlier are refused with a pointer at `chdman copy`, exactly as the
delegating layer already did.

The format is documented in MAME's own source (src/lib/util/chd.cpp,
huffman.cpp, chdcodec.cpp) and this follows it structurally so the two
can be read side by side. Three details are worth calling out because
they are not guessable:

  * The hunk map is itself compressed, with a Huffman tree that is RLE
    encoded, and MAME assigns canonical codes from the *longest* length
    downwards rather than the usual shortest-first. Getting that
    backwards produces a tree that decodes plausible nonsense.

  * The LZMA properties are never stored in the file. MAME derives them
    from the encoder settings it used - level 6, reduceSize = hunkbytes
    - and the decoder reconstructs them the same way. For the 4096-byte
    hunks chdman writes for DVDs that normalises to lc=3, lp=0, pb=2 and
    a 4096-byte dictionary. The MAME source has a FIXME about this.

  * Several map entries are pseudo-types that expand into a base type
    plus a computed offset, so the map cannot be read as a flat table.
"""

import hashlib
import os
import struct
import zlib

CHD_MAGIC = b"MComprHD"
V5_HEADER_LEN = 124

# Map entry compression codes (chd.cpp). 0-3 index the header's
# compressor list; the rest are literal or computed references.
_TYPE_0, _TYPE_1, _TYPE_2, _TYPE_3 = 0, 1, 2, 3
_NONE, _SELF, _PARENT = 4, 5, 6
_RLE_SMALL, _RLE_LARGE = 7, 8
_SELF_0, _SELF_1 = 9, 10
_PARENT_SELF, _PARENT_0, _PARENT_1 = 11, 12, 13


class ChdNativeError(Exception):
    """This file is not a v5 CHD, or is damaged."""


def _tag(value):
    return bytes((value >> s) & 0xFF for s in (24, 16, 8, 0))


# --------------------------------------------------------------- crc16

def _crc16_table():
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 \
                else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC16 = _crc16_table()


def _crc16_words():
    """65536-entry table: T[x] is the CRC of the two bytes of x with a
    zero initial state. For a CRC whose width equals the step size,
    state' = T[state ^ word] - one lookup and one xor per two bytes,
    which is the least Python per byte of any formulation tried (the
    slice-by-8 classic was no faster here: Python pays per operation,
    not per loop iteration)."""
    table = [0] * 65536
    for hi in range(256):
        base = _CRC16[hi]
        for lo in range(256):
            table[(hi << 8) | lo] = ((base << 8) & 0xFFFF) \
                ^ _CRC16[((base >> 8) ^ lo) & 0xFF]
    return tuple(table)


_CRC16W = _crc16_words()


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT as MAME computes it: poly 0x1021, init 0xFFFF, no
    reflection and no final xor. Runs once over every stored byte of an
    image, so it is written for speed, not for looks."""
    n = len(data)
    table = _CRC16W
    if n >= 2:
        words = struct.unpack(">%dH" % (n // 2), data[:n - (n % 2)])
        for w in words:
            crc = table[crc ^ w]
    if n % 2:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16[((crc >> 8) ^ data[-1]) & 0xFF]
    return crc


# ----------------------------------------------------------- bitstream

class _BitReader:
    """MSB-first bit reader, matching MAME's bitstream_in.

    Reads past the end yield zero bits rather than raising, which is
    what the reference does; a map that relies on them fails its CRC."""

    __slots__ = ("_data", "_pos", "_acc", "_nbits")

    def __init__(self, data):
        self._data = data
        self._pos = 0
        self._acc = 0
        self._nbits = 0

    def read(self, numbits):
        if numbits == 0:
            return 0
        while self._nbits < numbits:
            byte = self._data[self._pos] if self._pos < len(self._data) else 0
            self._pos += 1
            self._acc = (self._acc << 8) | byte
            self._nbits += 8
        shift = self._nbits - numbits
        value = (self._acc >> shift) & ((1 << numbits) - 1)
        self._acc &= (1 << shift) - 1
        self._nbits = shift
        return value


# ------------------------------------------------------------- huffman

class _Huffman:
    """MAME's canonical Huffman decoder, in the shape the map needs."""

    def __init__(self, numcodes, maxbits):
        self.numcodes = numcodes
        self.maxbits = maxbits
        self.lengths = [0] * numcodes
        self._lookup = {}

    def import_tree_rle(self, bits):
        """Read the RLE-encoded code lengths (huffman.cpp)."""
        numbits = 5 if self.maxbits >= 16 else 4 if self.maxbits >= 8 else 3
        cur = 0
        while cur < self.numcodes:
            nodebits = bits.read(numbits)
            if nodebits != 1:
                self.lengths[cur] = nodebits
                cur += 1
                continue
            # A 1 is an escape: a second 1 means a literal 1, anything
            # else is a run of that length, repeated 3 + count times.
            nodebits = bits.read(numbits)
            if nodebits == 1:
                self.lengths[cur] = 1
                cur += 1
                continue
            repcount = bits.read(numbits) + 3
            while repcount and cur < self.numcodes:
                self.lengths[cur] = nodebits
                cur += 1
                repcount -= 1
        if cur != self.numcodes:
            raise ChdNativeError("huffman tree is the wrong size")
        self._assign_canonical_codes()

    def _assign_canonical_codes(self):
        """Longest length first, which is MAME's order and not the
        conventional one. Reversing it decodes to plausible garbage."""
        histo = [0] * 33
        for length in self.lengths:
            if length > self.maxbits:
                raise ChdNativeError("huffman code longer than the maximum")
            if length <= 32:
                histo[length] += 1
        curstart = 0
        for codelen in range(32, 0, -1):
            nextstart = (curstart + histo[codelen]) >> 1
            if codelen != 1 and nextstart * 2 != (curstart + histo[codelen]):
                raise ChdNativeError("huffman tree is not canonical")
            histo[codelen] = curstart
            curstart = nextstart
        self._lookup = {}
        for code, length in enumerate(self.lengths):
            if length > 0:
                self._lookup[(length, histo[length])] = code
                histo[length] += 1

    def import_tree_huffman(self, bits):
        """Read a tree encoded with its own small Huffman tree, which is
        how the 8-bit hunk codec stores one (huffman.cpp). The small
        tree codes *lengths*, and a zero symbol introduces a run of the
        previous length."""
        small = _Huffman(24, 6)
        small.lengths[0] = bits.read(3)
        start = bits.read(3) + 1
        count = 0
        for index in range(1, 24):
            if index < start or count == 7:
                small.lengths[index] = 0
            else:
                count = bits.read(3)
                small.lengths[index] = 0 if count == 7 else count
        small._assign_canonical_codes()

        temp = self.numcodes - 9
        rlefullbits = 0
        while temp != 0:
            temp >>= 1
            rlefullbits += 1

        last = 0
        cur = 0
        while cur < self.numcodes:
            value = small.decode_one(bits)
            if value != 0:
                last = value - 1
                self.lengths[cur] = last
                cur += 1
            else:
                run = bits.read(3) + 2
                if run == 7 + 2:
                    run += bits.read(rlefullbits)
                while run != 0 and cur < self.numcodes:
                    self.lengths[cur] = last
                    cur += 1
                    run -= 1
        if cur != self.numcodes:
            raise ChdNativeError("huffman tree is the wrong size")
        self._assign_canonical_codes()

    def decode_one(self, bits):
        code = 0
        for length in range(1, self.maxbits + 1):
            code = (code << 1) | bits.read(1)
            hit = self._lookup.get((length, code))
            if hit is not None:
                return hit
        raise ChdNativeError("no huffman code matched")


# --------------------------------------------------------------- header

def read_header(fh):
    """Parse a v5 header from an open file. Returns a dict."""
    fh.seek(0)
    raw = fh.read(V5_HEADER_LEN)
    if len(raw) < V5_HEADER_LEN or raw[:8] != CHD_MAGIC:
        raise ChdNativeError("not a CHD file")
    length, version = struct.unpack(">II", raw[8:16])
    if version != 5:
        raise ChdNativeError(
            "CHD v%d is not supported natively (chdman can upgrade it: "
            "`chdman copy`)" % version)
    if length != V5_HEADER_LEN:
        raise ChdNativeError("v5 header claims %d bytes, expected %d"
                             % (length, V5_HEADER_LEN))
    compressors = struct.unpack(">4I", raw[16:32])
    logicalbytes, mapoffset, metaoffset = struct.unpack(">QQQ", raw[32:56])
    hunkbytes, unitbytes = struct.unpack(">II", raw[56:64])
    if hunkbytes == 0 or unitbytes == 0:
        raise ChdNativeError("header declares a zero hunk or unit size")
    return {
        "version": version,
        "compressors": [_tag(c) for c in compressors],
        "logicalbytes": logicalbytes,
        "mapoffset": mapoffset,
        "metaoffset": metaoffset,
        "hunkbytes": hunkbytes,
        "unitbytes": unitbytes,
        "rawsha1": raw[64:84].hex(),
        "sha1": raw[84:104].hex(),
        "parentsha1": raw[104:124].hex(),
        "hunkcount": (logicalbytes + hunkbytes - 1) // hunkbytes,
    }


# ------------------------------------------------------------ hunk map

def read_map(fh, header):
    """Expand the compressed hunk map into a list of entries.

    Each entry is (compression, length, offset, crc16). The walk follows
    chd.cpp's decompress_v5_map step for step, including the pseudo-type
    expansion, and ends by checking the same CRC over the same twelve
    bytes per hunk that the reference builds."""
    hunkcount = header["hunkcount"]
    hunkbytes = header["hunkbytes"]
    unitbytes = header["unitbytes"]
    mapoffset = header["mapoffset"]
    if mapoffset == 0:
        raise ChdNativeError("file has no map: it was never finished")

    fh.seek(mapoffset)
    head = fh.read(16)
    if len(head) < 16:
        raise ChdNativeError("truncated map header")
    mapbytes = struct.unpack(">I", head[0:4])[0]
    firstoffs = int.from_bytes(head[4:10], "big")
    mapcrc = struct.unpack(">H", head[10:12])[0]
    lengthbits, selfbits, parentbits = head[12], head[13], head[14]

    compressed = fh.read(mapbytes)
    if len(compressed) < mapbytes:
        raise ChdNativeError("truncated map")
    bits = _BitReader(compressed)

    decoder = _Huffman(16, 8)
    decoder.import_tree_rle(bits)

    # Pass one: the compression type of every hunk, run-length encoded.
    types = [0] * hunkcount
    lastcomp = 0
    repcount = 0
    for i in range(hunkcount):
        if repcount > 0:
            types[i] = lastcomp
            repcount -= 1
            continue
        val = decoder.decode_one(bits)
        if val == _RLE_SMALL:
            types[i] = lastcomp
            repcount = 2 + decoder.decode_one(bits)
        elif val == _RLE_LARGE:
            types[i] = lastcomp
            repcount = 2 + 16 + (decoder.decode_one(bits) << 4)
            repcount += decoder.decode_one(bits)
        else:
            lastcomp = val
            types[i] = val

    # Pass two: lengths, offsets and CRCs, accumulating as we go.
    entries = []
    raw = bytearray(hunkcount * 12)
    curoffset = firstoffs
    last_self = 0
    last_parent = 0
    for i in range(hunkcount):
        comp = types[i]
        offset = curoffset
        length = 0
        crc = 0
        if comp in (_TYPE_0, _TYPE_1, _TYPE_2, _TYPE_3):
            length = bits.read(lengthbits)
            curoffset += length
            crc = bits.read(16)
        elif comp == _NONE:
            length = hunkbytes
            curoffset += length
            crc = bits.read(16)
        elif comp == _SELF:
            offset = bits.read(selfbits)
            last_self = offset
        elif comp == _PARENT:
            offset = bits.read(parentbits)
            last_parent = offset
        elif comp in (_SELF_0, _SELF_1):
            if comp == _SELF_1:
                last_self += 1
            comp = _SELF
            offset = last_self
        elif comp == _PARENT_SELF:
            comp = _PARENT
            offset = last_parent = (i * hunkbytes) // unitbytes
        elif comp in (_PARENT_0, _PARENT_1):
            if comp == _PARENT_1:
                last_parent += hunkbytes // unitbytes
            comp = _PARENT
            offset = last_parent
        else:
            raise ChdNativeError("unknown map compression type %d" % comp)
        base = i * 12
        raw[base] = comp
        raw[base + 1:base + 4] = length.to_bytes(3, "big")
        raw[base + 4:base + 10] = offset.to_bytes(6, "big")
        raw[base + 10:base + 12] = crc.to_bytes(2, "big")
        entries.append((comp, length, offset, crc))

    if crc16(raw) != mapcrc:
        raise ChdNativeError("map failed its own CRC - the file is damaged")
    return entries


# --------------------------------------------------------------- hunks

def _lzma_filters(hunkbytes):
    """The LZMA settings MAME used, reconstructed rather than read.

    chdman never records them. It configures the encoder with level 6
    and reduceSize = hunkbytes and lets the SDK normalise, so the
    decoder has to repeat that arithmetic exactly: level 6 asks for a
    32 MiB dictionary, the reduce step shrinks it to the smallest
    2<<i or 3<<i that still covers a hunk, and lc/lp/pb keep the SDK
    defaults. MAME carries a FIXME about this being unupgradable."""
    import lzma
    dict_size = 1 << 25                      # level 6
    if dict_size > hunkbytes:
        for i in range(11, 31):
            if hunkbytes <= (2 << i):
                dict_size = 2 << i
                break
            if hunkbytes <= (3 << i):
                dict_size = 3 << i
                break
    return [{"id": lzma.FILTER_LZMA1, "lc": 3, "lp": 0, "pb": 2,
             "dict_size": dict_size}]


class _Codecs:
    """Hunk decompressors, built once per file."""

    def __init__(self, header):
        self.hunkbytes = header["hunkbytes"]
        self.tags = header["compressors"]
        self._lzma_f = _lzma_filters(self.hunkbytes)

    def decode(self, index, data, out_len):
        tag = self.tags[index] if index < len(self.tags) else b""
        if tag == b"zlib":
            return zlib.decompress(data, -15, out_len)
        if tag == b"lzma":
            import lzma
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
                                        filters=self._lzma_f)
            return dec.decompress(data, out_len)
        if tag == b"zstd":
            from .zar_native import _zstd_decompress
            if _zstd_decompress is None:
                raise ChdNativeError("this CHD uses zstd and no zstd "
                                     "support is installed")
            return _zstd_decompress(data)
        if tag == b"huff":
            return _huff_decode(data, out_len)
        if tag == b"flac":
            from . import chd_flac
            try:
                return chd_flac.decode_hunk(data, out_len)
            except chd_flac.FlacError as e:
                raise ChdNativeError("FLAC hunk decode failed: %s" % e)
        raise ChdNativeError("unknown compressor %r" % tag)


def _huff_decode(data, out_len):
    """MAME's 8-bit huffman hunk codec (huffman_decoder<256, 16>)."""
    bits = _BitReader(data)
    decoder = _Huffman(256, 16)
    decoder.import_tree_huffman(bits)
    out = bytearray(out_len)
    for i in range(out_len):
        out[i] = decoder.decode_one(bits)
    return bytes(out)


class ChdReader:
    """A seekable, read-only stream over the image inside a v5 CHD.

    Presents the decompressed image, so every reader in xverter that
    takes "a path or a seekable file-like" can be handed one of these
    and never know the difference - which is the same shape GodStream,
    CciReader and CsoReader already have.

    One hunk is cached, because sequential reads walk a hunk many times
    and the codecs are the expensive part."""

    def __init__(self, path):
        self._fh = open(path, "rb")
        try:
            self.header = read_header(self._fh)
            self._map = read_map(self._fh, self.header)
        except BaseException:
            self._fh.close()
            raise
        self._codecs = _Codecs(self.header)
        self.size = self.header["logicalbytes"]
        self.hunkbytes = self.header["hunkbytes"]
        self._pos = 0
        self._cached = -1
        self._cache = b""

    # -- hunk access ---------------------------------------------------

    def _hunk(self, index):
        if index == self._cached:
            return self._cache
        data = self._read_hunk(index, set())
        self._cached = index
        self._cache = data
        return data

    def _read_hunk(self, index, seen):
        if index >= len(self._map):
            raise ChdNativeError("hunk %d is past the end of the map" % index)
        comp, length, offset, crc = self._map[index]
        if comp == _SELF:
            # A self-reference names another hunk. Guard the chain: a
            # damaged map could point one at itself and hang the reader.
            if index in seen:
                raise ChdNativeError("self-referencing hunk loop at %d" % index)
            seen.add(index)
            return self._read_hunk(offset, seen)
        if comp == _PARENT:
            raise ChdNativeError(
                "this CHD is a delta against a parent file, which xverter "
                "does not read - `chdman copy` will flatten it")
        self._fh.seek(offset)
        if comp == _NONE:
            data = self._fh.read(self.hunkbytes)
        else:
            data = self._codecs.decode(comp, self._fh.read(length),
                                       self.hunkbytes)
        if len(data) != self.hunkbytes:
            raise ChdNativeError("hunk %d decoded to %d bytes, expected %d"
                                 % (index, len(data), self.hunkbytes))
        if crc and crc16(data) != crc:
            raise ChdNativeError("hunk %d failed its CRC" % index)
        return data

    # -- stream interface ----------------------------------------------

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self._pos
        elif whence == 2:
            offset += self.size
        self._pos = max(0, offset)
        return self._pos

    def tell(self):
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        n = max(0, min(n, self.size - self._pos))
        out = bytearray()
        while n > 0:
            index, within = divmod(self._pos, self.hunkbytes)
            chunk = self._hunk(index)[within:within + n]
            if not chunk:
                break
            out += chunk
            self._pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# --------------------------------------------------------------- writing

class _BitWriter:
    """MSB-first bit writer, the mirror of _BitReader."""

    def __init__(self):
        self._out = bytearray()
        self._acc = 0
        self._n = 0

    def write(self, value, numbits):
        if numbits == 0:
            return
        self._acc = (self._acc << numbits) | (value & ((1 << numbits) - 1))
        self._n += numbits
        while self._n >= 8:
            self._n -= 8
            self._out.append((self._acc >> self._n) & 0xFF)
        self._acc &= (1 << self._n) - 1

    def flush(self):
        if self._n:
            self._out.append((self._acc << (8 - self._n)) & 0xFF)
            self._acc = 0
            self._n = 0
        return bytes(self._out)


def _code_lengths(counts, maxbits):
    """Huffman code lengths, capped at maxbits.

    MAME's decoder insists the code be *complete* - its canonical
    assignment fails outright on a code whose Kraft sum is not exactly
    one - so when a skewed histogram pushes a length past the cap this
    falls back to a flat code, which is complete by construction and
    valid at any histogram. A slightly larger map beats an unreadable
    one."""
    import heapq
    used = [(c, i) for i, c in enumerate(counts) if c]
    if not used:
        return [0] * len(counts)
    if len(used) == 1:
        lengths = [0] * len(counts)
        lengths[used[0][1]] = 1
        return lengths
    heap = [(c, 0, (i,)) for c, i in used]
    heapq.heapify(heap)
    lengths = [0] * len(counts)
    while len(heap) > 1:
        c1, d1, s1 = heapq.heappop(heap)
        c2, d2, s2 = heapq.heappop(heap)
        for i in s1 + s2:
            lengths[i] += 1
        heapq.heappush(heap, (c1 + c2, max(d1, d2) + 1, s1 + s2))
    if max(lengths) > maxbits:
        flat = 1
        while (1 << flat) < len(counts):
            flat += 1
        return [flat] * len(counts)
    return lengths


def _export_tree_rle(bw, lengths, maxbits):
    """Write code lengths the way import_tree_rle reads them."""
    numbits = 5 if maxbits >= 16 else 4 if maxbits >= 8 else 3
    i = 0
    n = len(lengths)
    while i < n:
        value = lengths[i]
        run = 1
        while i + run < n and lengths[i + run] == value and run < (1 << numbits) + 2:
            run += 1
        if value == 1:
            # 1 is the escape code, so a literal 1 is written twice.
            for _ in range(run):
                bw.write(1, numbits)
                bw.write(1, numbits)
            i += run
            continue
        if run >= 3:
            bw.write(1, numbits)
            bw.write(value, numbits)
            bw.write(run - 3, numbits)
            i += run
        else:
            for _ in range(run):
                bw.write(value, numbits)
            i += run


def _canonical_codes(lengths, maxbits):
    """The same assignment the decoder performs, so the two agree."""
    histo = [0] * 33
    for length in lengths:
        if length:
            histo[length] += 1
    curstart = 0
    for codelen in range(32, 0, -1):
        nextstart = (curstart + histo[codelen]) >> 1
        if codelen != 1 and nextstart * 2 != (curstart + histo[codelen]):
            raise ChdNativeError("refusing to write an incomplete huffman code")
        histo[codelen] = curstart
        curstart = nextstart
    codes = [0] * len(lengths)
    for i, length in enumerate(lengths):
        if length:
            codes[i] = histo[length]
            histo[length] += 1
    return codes


#: Compressors used when writing, in the order they occupy the header's
#: four slots. zlib is the only one every CHD consumer has understood
#: for the life of the format; zstd is faster and smaller but needs a
#: reader from 2024 or later, so it is opt-in rather than default.
DEFAULT_COMPRESSORS = (b"lzma", b"zlib", b"flac")
DVD_HUNKBYTES = 4096
DVD_UNITBYTES = 2048


def _pcm_likely(raw):
    """Cheap probe: does this hunk plausibly hold 16-bit PCM audio?

    Audio waveforms are smooth - consecutive samples are close - while
    compressed assets and code are not, and smoothness is what FLAC's
    predictors monetise. Sampling 128 stereo pairs and comparing the
    step size against the amplitude answers it in microseconds.

    The first version of this gate asked the wrong question ("did LZ
    do badly?"). On a real disc most hunks are pre-compressed assets,
    which LZ also does badly on, so nearly every hunk paid a
    multi-millisecond FLAC attempt for a measured 42 KB of benefit and
    the writer did not finish inside ten minutes. FLAC should be tried
    where audio is plausible, not wherever LZ struggled. chdman
    auditions FLAC on everything - at C speed, that costs it little;
    the honest Python equivalent is this probe. CD-type content, when
    the PS1/PS2 work arrives, is wall-to-wall PCM and passes it
    everywhere it matters.
    """
    n = len(raw)
    if n < 512 or n % 4:
        return False
    step = max(4, (n // 128) & ~3)
    prev_l = int.from_bytes(raw[0:2], "little", signed=True)
    amp = diff = 0
    for i in range(step, n - 1, step):
        cur = int.from_bytes(raw[i:i + 2], "little", signed=True)
        amp += cur if cur >= 0 else -cur
        d = cur - prev_l
        diff += d if d >= 0 else -d
        prev_l = cur
    # Silence is audio too, and constant hunks encode brilliantly.
    if amp == 0:
        return not any(raw)
    return diff * 2 < amp


def _compress_hunk(raw, tags, hunkbytes, level):
    """Best of the configured codecs for one hunk, or None to store it
    raw. Returns (type_index, payload)."""
    best = None
    flac_index = None
    for index, tag in enumerate(tags):
        try:
            if tag == b"flac":
                flac_index = index
                continue
            if tag == b"zlib":
                # CHD stores raw deflate, without zlib's 2-byte header
                # and 4-byte trailer.
                obj = zlib.compressobj(level, zlib.DEFLATED, -15)
                comp = obj.compress(raw) + obj.flush()
            elif tag == b"lzma":
                # Raw LZMA1 with exactly the properties MAME's decoder
                # will reconstruct (it never reads them from the file):
                # lc=3 lp=0 pb=2, dictionary normalised from hunkbytes.
                import lzma as _lzma
                comp = _lzma.compress(
                    raw, format=_lzma.FORMAT_RAW,
                    filters=_lzma_filters(hunkbytes))
            elif tag == b"zstd":
                try:
                    from compression import zstd as _zstd
                except ImportError:
                    continue
                comp = _zstd.compress(raw, level)
            else:
                continue
        except Exception:                                 # noqa: BLE001
            continue
        if best is None or len(comp) < len(best[1]):
            best = (index, comp)
    if flac_index is not None and _pcm_likely(raw):
        from . import chd_flac
        limit = (len(best[1]) if best is not None else hunkbytes) - 1
        comp = chd_flac.encode_hunk(raw, limit)
        if comp is not None:
            best = (flac_index, comp)
    if best is None or len(best[1]) >= hunkbytes:
        return None
    return best


def write_dvd(src, out_path, compressors=DEFAULT_COMPRESSORS, level=6,
              workers=None, progress=None):
    """Write a DVD-type v5 CHD from an image (path or seekable stream).

    Not byte-identical to chdman, and cannot be: chdman auditions four
    codecs per hunk and one of them is FLAC, which we do not write (on
    DVD images it was measured worth 0.0006% of file size - it exists
    for CD audio tracks). The claim made instead is the one that
    matters: chdman verifies what this writes, extracts it byte-
    identical, and agrees about both SHA-1s - checked, not asserted.

    Built to stream: hunks are read in large blocks, compressed and
    CRC'd on a pool (zlib and lzma release the GIL, so the pool is real
    on either interpreter), payloads go straight to disk in order, and
    duplicate hunks are recognised before compression by digest, so the
    memory held is the in-flight window and a 20-byte key per unique
    hunk - not the image."""
    own = False
    stream = src
    if isinstance(src, (str, bytes, os.PathLike)):
        stream = open(src, "rb")
        own = True
    try:
        stream.seek(0, 2)
        logicalbytes = stream.tell()
        stream.seek(0)
        hunkbytes = DVD_HUNKBYTES
        hunkcount = (logicalbytes + hunkbytes - 1) // hunkbytes
        if workers is None:
            workers = min(24, max(2, os.cpu_count() or 4))

        meta_payload = b"\x00"
        meta_entry = (b"DVD " + bytes([0x01])
                      + len(meta_payload).to_bytes(3, "big")
                      + (0).to_bytes(8, "big") + meta_payload)
        data_start = V5_HEADER_LEN + len(meta_entry)

        rawhash = hashlib.sha1()
        seen = {}
        entries = [None] * hunkcount
        done_bytes = [0]
        blockhunks = 1024                       # 4 MiB reads
        window = max(4, workers + 4)

        def job(block):
            """One read block per task: digest, compress and CRC every
            hunk in it. Hunk-per-future was measured and lost - at 1.9M
            hunks the future overhead alone was tens of seconds, the
            same small-units lesson the LZ4 pool taught."""
            view = memoryview(block)
            results = []
            for i in range(0, len(block), hunkbytes):
                raw = bytes(view[i:i + hunkbytes])
                results.append((hashlib.sha1(raw).digest(),
                                _compress_hunk(raw, compressors, hunkbytes,
                                               level),
                                crc16(raw), raw))
            return results

        import collections
        import concurrent.futures

        with open(out_path, "wb") as out:
            out.seek(data_start)
            offset = data_start
            index = 0

            def emit(results):
                """Consume one block's results, strictly in order, so
                self-references always point backwards. A duplicate's
                compression was wasted work, and cheaper than the digest
                round-trip that avoiding it would put on this thread."""
                nonlocal offset, index
                for key, best, crc, raw in results:
                    prior = seen.get(key)
                    if prior is not None:
                        entries[index] = (_SELF, 0, prior, 0)
                    else:
                        seen[key] = index
                        if best is None:
                            entries[index] = (_NONE, hunkbytes, offset, crc)
                            out.write(raw)
                            offset += hunkbytes
                        else:
                            type_index, payload = best
                            entries[index] = (type_index, len(payload),
                                              offset, crc)
                            out.write(payload)
                            offset += len(payload)
                    index += 1
                    done_bytes[0] += hunkbytes
                if progress:
                    progress(min(done_bytes[0], logicalbytes), logicalbytes)

            pending = collections.deque()
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers) as pool:
                while True:
                    block = stream.read(hunkbytes * blockhunks)
                    if not block:
                        break
                    rawhash.update(block)
                    if len(block) % hunkbytes:
                        block += b"\x00" * (hunkbytes - len(block) % hunkbytes)
                    pending.append(pool.submit(job, block))
                    if len(pending) >= window:
                        emit(pending.popleft().result())
                while pending:
                    emit(pending.popleft().result())

            if index != hunkcount or any(e is None for e in entries):
                raise ChdNativeError("built %d hunks, expected %d"
                                     % (index, hunkcount))
            mapoffset = offset
            out.write(_encode_map(entries, hunkbytes, data_start))
            rawsha1 = rawhash.digest()
            overall = hashlib.sha1(
                rawsha1 + b"DVD " + hashlib.sha1(meta_payload).digest()
            ).digest()
            out.seek(0)
            slots = list(compressors) + [b"\x00" * 4] * (4 - len(compressors))
            out.write(CHD_MAGIC + struct.pack(">II", V5_HEADER_LEN, 5)
                      + b"".join(t if len(t) == 4 else b"\x00" * 4
                                 for t in slots)
                      + struct.pack(">QQQ", logicalbytes, mapoffset,
                                    V5_HEADER_LEN)
                      + struct.pack(">II", hunkbytes, DVD_UNITBYTES)
                      + rawsha1 + overall + b"\x00" * 20)
            out.write(meta_entry)
        return out_path
    finally:
        if own:
            stream.close()


def _encode_map(entries, hunkbytes, data_start):
    """Compress the hunk map the way chd.cpp expects to read it."""
    raw = bytearray(len(entries) * 12)
    for i, (comp, length, offset, crc) in enumerate(entries):
        base = i * 12
        raw[base] = comp
        raw[base + 1:base + 4] = length.to_bytes(3, "big")
        raw[base + 4:base + 10] = offset.to_bytes(6, "big")
        raw[base + 10:base + 12] = crc.to_bytes(2, "big")
    mapcrc = crc16(raw)

    maxlen = max((e[1] for e in entries), default=0)
    lengthbits = max(1, maxlen.bit_length())
    selfbits = max(1, (len(entries) - 1).bit_length()) if entries else 1

    counts = [0] * 16
    for comp, _l, _o, _c in entries:
        counts[comp] += 1
    lengths = _code_lengths(counts, 8)
    codes = _canonical_codes(lengths, 8)

    bw = _BitWriter()
    _export_tree_rle(bw, lengths, 8)
    for comp, _l, _o, _c in entries:
        bw.write(codes[comp], lengths[comp])
    for comp, length, offset, crc in entries:
        if comp in (_TYPE_0, _TYPE_1, _TYPE_2, _TYPE_3):
            bw.write(length, lengthbits)
            bw.write(crc, 16)
        elif comp == _NONE:
            bw.write(crc, 16)
        elif comp == _SELF:
            bw.write(offset, selfbits)
    body = bw.flush()
    head = (struct.pack(">I", len(body))
            + data_start.to_bytes(6, "big")
            + struct.pack(">H", mapcrc)
            + bytes([lengthbits, selfbits, 0, 0]))
    return head + body


def extract_to(chd_path, out_path, workers=None, progress=None):
    """Decompress a CHD to an image file, in parallel.

    The sequential read() path decodes one hunk at a time on one
    thread, which is fine for scattered access and measured 4.9x slower
    than chdman for a full extraction. Hunks are independent, so a full
    extraction decodes them on a pool - codec and CRC both - and writes
    in order. Self-references are resolved on the writer thread from a
    small retained set: only hunks some later hunk points at are kept,
    which on a real image is the padding, not the game.

    out_path=None decodes and hashes without writing - the verify path.

    Returns the SHA-1 of the decoded image, which the caller is
    expected to compare against the header's rawsha1 - reporting a
    mismatch is its job, deciding what to do about it is theirs."""
    import collections
    import concurrent.futures

    if workers is None:
        workers = min(24, max(2, os.cpu_count() or 4))

    with open(chd_path, "rb") as fh:
        header = read_header(fh)
        entries = read_map(fh, header)
        hunkbytes = header["hunkbytes"]
        logical = header["logicalbytes"]

        # Which hunks does anyone point back at?
        wanted = set()
        for comp, _l, offset, _c in entries:
            if comp == _SELF:
                wanted.add(offset)
        retained = {}
        codecs = _Codecs(header)

        def job(batch):
            """Decode one run of stored hunks: (index, raw) each."""
            done = []
            for index, comp, length, offset, crc, blob in batch:
                if comp == _NONE:
                    raw = blob
                else:
                    raw = codecs.decode(comp, blob, hunkbytes)
                if len(raw) != hunkbytes:
                    raise ChdNativeError(
                        "hunk %d decoded to %d bytes, expected %d"
                        % (index, len(raw), hunkbytes))
                if crc and crc16(raw) != crc:
                    raise ChdNativeError("hunk %d failed its CRC" % index)
                done.append((index, raw))
            return done

        digest = hashlib.sha1()
        written = [0]

        with open(out_path if out_path is not None else os.devnull,
                  "wb") as out:
            def emit(results):
                for index, raw in results:
                    if index in wanted:
                        retained[index] = raw
                    take = min(hunkbytes, logical - written[0])
                    if take > 0:
                        chunk = raw[:take]
                        out.write(chunk)
                        digest.update(chunk)
                        written[0] += take
                    if progress:
                        progress(written[0], logical)

            # The deque holds futures and self-references in file
            # order; emission is FIFO, so by the time a self-reference
            # is emitted every earlier hunk has been, and its target is
            # in the retained set. No flush, no stall: a dup-heavy
            # stretch costs a dict lookup, not a pool drain.
            pending = collections.deque()
            batch = []
            batch_hunks = 512                       # 2 MiB decoded per task

            def emit_one(item):
                if item[0] == "fut":
                    emit(item[1].result())
                    return
                _kind, index, target = item
                raw = retained.get(target)
                if raw is None:
                    raise ChdNativeError(
                        "hunk %d references hunk %d, which did not "
                        "come before it" % (index, target))
                emit([(index, raw)])

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers) as pool:
                def flush_batch():
                    nonlocal batch
                    if batch:
                        pending.append(("fut", pool.submit(job, batch)))
                        batch = []

                for index, (comp, length, offset, crc) in enumerate(entries):
                    if comp == _SELF:
                        flush_batch()
                        pending.append(("self", index, offset))
                    elif comp == _PARENT:
                        raise ChdNativeError(
                            "this CHD is a delta against a parent file, "
                            "which xverter does not read - `chdman copy` "
                            "will flatten it")
                    else:
                        fh.seek(offset)
                        blob = fh.read(length if comp != _NONE else hunkbytes)
                        batch.append((index, comp, length, offset, crc, blob))
                        if len(batch) >= batch_hunks:
                            flush_batch()
                    while len(pending) > workers + 4:
                        emit_one(pending.popleft())
                flush_batch()
                while pending:
                    emit_one(pending.popleft())
        return digest.hexdigest()


def read_metadata(fh, header):
    """The metadata chain as [(tag, flags, payload)], in file order."""
    found = []
    offset = header["metaoffset"]
    while offset:
        fh.seek(offset)
        raw = fh.read(16)
        if len(raw) < 16:
            raise ChdNativeError("truncated metadata entry")
        tag = raw[0:4]
        flags = raw[4]
        length = int.from_bytes(raw[5:8], "big")
        offset_next = int.from_bytes(raw[8:16], "big")
        payload = fh.read(length)
        if len(payload) < length:
            raise ChdNativeError("truncated metadata payload")
        found.append((tag, flags, payload))
        offset = offset_next
    return found


def verify_file(chd_path, workers=None, progress=None):
    """Decode everything and check both header SHA-1s, natively.

    The same two claims chdman's verify makes: the decoded data matches
    rawsha1, and rawsha1 combined with the checksummed metadata matches
    the overall sha1. Raises on the first disagreement; returns the
    parsed header when both hold."""
    got = extract_to(chd_path, None, workers=workers, progress=progress)
    with open(chd_path, "rb") as fh:
        header = read_header(fh)
        metadata = read_metadata(fh, header)
    if got != header["rawsha1"]:
        raise ChdNativeError("data does not match the header: decoded "
                             "sha1 %s, header %s" % (got, header["rawsha1"]))
    hashes = []
    for tag, flags, payload in metadata:
        if flags & 0x01:                     # CHD_MDFLAGS_CHECKSUM
            hashes.append(tag + hashlib.sha1(payload).digest())
    overall = hashlib.sha1()
    overall.update(bytes.fromhex(got))
    for entry in sorted(hashes):
        overall.update(entry)
    if overall.hexdigest() != header["sha1"]:
        raise ChdNativeError("metadata does not match the header: overall "
                             "sha1 %s, header %s"
                             % (overall.hexdigest(), header["sha1"]))
    return header
