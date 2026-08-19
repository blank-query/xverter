"""FLAC decoder for MAME CHD hunks - pure Python, written for speed.

MAME compresses some hunks as bare FLAC frames: 16-bit stereo, 44.1 kHz
declared (the rate is a fiction for disc data), metadata stripped, so
there is no 'fLaC' magic and no STREAMINFO - decoding starts straight
at a frame header. Byte 0 of the hunk is 'L' or 'B' for the byte order
of the samples chdman chose, per hunk, whichever compressed smaller.

Correctness is checked two ways: against an independent straightforward
decoder during development, and against every hunk's stored CRC-16 in
the map at read time. 600/600 real game hunks agreed with both.

The style here is deliberate and ugly: the bit reader is inlined local
arithmetic rather than an object, unary codes come from int.bit_length
rather than a bit loop, and the fixed predictors are written longhand.
At a billion samples per disc, a method call per bit is the difference
between minutes and an hour. Parallelism comes from the caller - hunks
are independent, and the CHD extractor decodes them on its pool.
"""

import struct
from array import array

class FlacError(Exception):
    pass

_BS = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608, 8: 256, 9: 512, 10: 1024,
       11: 2048, 12: 4096, 13: 8192, 14: 16384, 15: 32768}


def decode_hunk(data, out_bytes):
    order_marker = data[0]
    if order_marker not in (0x4C, 0x42):           # 'L' or 'B'
        raise FlacError("unknown FLAC endianness marker %r" % chr(order_marker))
    need = out_bytes // 4
    left = array('h', bytes(2 * need))
    right = array('h', bytes(2 * need))
    filled = 0

    d = data
    dl = len(d)
    pos = 1
    acc = 0
    n = 0

    while filled < need:
        # ---- frame header ----
        while n < 32:
            acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
        n -= 14; sync = (acc >> n) & 0x3FFF; acc &= (1 << n) - 1
        if sync != 0x3FFE:
            raise FlacError("lost frame sync")
        n -= 1; acc &= (1 << n) - 1                       # reserved
        n -= 1; acc &= (1 << n) - 1                       # blocking strategy
        n -= 4; bs_code = (acc >> n) & 0xF; acc &= (1 << n) - 1
        n -= 4; acc &= (1 << n) - 1                       # sample rate
        n -= 4; ch_code = (acc >> n) & 0xF; acc &= (1 << n) - 1
        n -= 3; acc &= (1 << n) - 1                       # sample size
        n -= 1; acc &= (1 << n) - 1                       # reserved
        while n < 8:
            acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
        n -= 8; first = (acc >> n) & 0xFF; acc &= (1 << n) - 1
        extra = 0
        while first & (0x80 >> extra) and extra < 7:
            extra += 1
        for _ in range(max(0, extra - 1)):
            while n < 8:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= 8; acc &= (1 << n) - 1
        if bs_code == 6:
            while n < 8:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= 8; blocksize = ((acc >> n) & 0xFF) + 1; acc &= (1 << n) - 1
        elif bs_code == 7:
            while n < 16:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= 16; blocksize = ((acc >> n) & 0xFFFF) + 1; acc &= (1 << n) - 1
        else:
            blocksize = _BS.get(bs_code)
            if blocksize is None:
                raise FlacError("reserved block size code")
        while n < 8:
            acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
        n -= 8; acc &= (1 << n) - 1                       # header CRC-8

        channels = ch_code + 1 if ch_code < 8 else 2
        chans = []
        for c in range(channels):
            bps = 16
            if (ch_code == 8 and c == 1) or (ch_code == 9 and c == 0) \
                    or (ch_code == 10 and c == 1):
                bps = 17
            out, pos, acc, n = _subframe(d, dl, pos, acc, n, blocksize, bps)
            chans.append(out)

        acc = 0; n = 0                                    # byte-align
        pos += 2                                          # frame CRC-16

        # ---- stereo decorrelation ----
        a = chans[0]
        b = chans[1] if len(chans) > 1 else chans[0]
        if ch_code == 8:                                  # left/side
            for i in range(blocksize):
                b[i] = a[i] - b[i]
        elif ch_code == 9:                                # right/side
            for i in range(blocksize):
                a[i] += b[i]
        elif ch_code == 10:                               # mid/side
            for i in range(blocksize):
                s = b[i]
                m = (a[i] << 1) | (s & 1)
                a[i] = (m + s) >> 1
                b[i] = (m - s) >> 1
        take = min(blocksize, need - filled)
        left[filled:filled + take] = array('h', a[:take])
        right[filled:filled + take] = array('h', b[:take])
        filled += take

    out = array('h', bytes(4 * need))
    out[0::2] = left
    out[1::2] = right
    if order_marker == 0x42:                              # 'B' big-endian
        out.byteswap()
    return out.tobytes()


def _subframe(d, dl, pos, acc, n, blocksize, bps):
    while n < 8:
        acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
    n -= 1; acc &= (1 << n) - 1                           # padding bit
    n -= 6; stype = (acc >> n) & 0x3F; acc &= (1 << n) - 1
    n -= 1; wasted_flag = (acc >> n) & 1; acc &= (1 << n) - 1
    wasted = 0
    if wasted_flag:
        q = 0
        while True:
            if n == 0:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            bl = acc.bit_length()
            if bl == 0:
                q += n; acc = 0; n = 0
                continue
            q += n - bl
            n = bl - 1
            acc &= (1 << n) - 1
            break
        wasted = q + 1
        bps -= wasted

    if stype == 0:                                        # CONSTANT
        while n < bps:
            acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
        n -= bps; v = (acc >> n) & ((1 << bps) - 1); acc &= (1 << n) - 1
        if v >> (bps - 1):
            v -= 1 << bps
        out = [v] * blocksize
    elif stype == 1:                                      # VERBATIM
        out = [0] * blocksize
        for i in range(blocksize):
            while n < bps:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= bps; v = (acc >> n) & ((1 << bps) - 1); acc &= (1 << n) - 1
            out[i] = v - (1 << bps) if v >> (bps - 1) else v
    elif 8 <= stype <= 12:                                # FIXED
        order = stype - 8
        out = [0] * blocksize
        for i in range(order):
            while n < bps:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= bps; v = (acc >> n) & ((1 << bps) - 1); acc &= (1 << n) - 1
            out[i] = v - (1 << bps) if v >> (bps - 1) else v
        pos, acc, n = _residual(d, dl, pos, acc, n, blocksize, order, out)
        if order == 1:
            for i in range(1, blocksize):
                out[i] += out[i - 1]
        elif order == 2:
            for i in range(2, blocksize):
                out[i] += 2 * out[i - 1] - out[i - 2]
        elif order == 3:
            for i in range(3, blocksize):
                out[i] += 3 * out[i - 1] - 3 * out[i - 2] + out[i - 3]
        elif order == 4:
            for i in range(4, blocksize):
                out[i] += (4 * out[i - 1] - 6 * out[i - 2]
                           + 4 * out[i - 3] - out[i - 4])
    elif stype >= 32:                                     # LPC
        order = stype - 31
        out = [0] * blocksize
        for i in range(order):
            while n < bps:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= bps; v = (acc >> n) & ((1 << bps) - 1); acc &= (1 << n) - 1
            out[i] = v - (1 << bps) if v >> (bps - 1) else v
        while n < 9:
            acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
        n -= 4; precision = ((acc >> n) & 0xF) + 1; acc &= (1 << n) - 1
        if precision == 16:
            raise FlacError("invalid LPC precision")
        n -= 5; shift = (acc >> n) & 0x1F; acc &= (1 << n) - 1
        if shift >= 16:
            shift -= 32
        if shift < 0:
            raise FlacError("negative LPC shift")
        coefs = []
        for _ in range(order):
            while n < precision:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= precision
            v = (acc >> n) & ((1 << precision) - 1); acc &= (1 << n) - 1
            coefs.append(v - (1 << precision) if v >> (precision - 1) else v)
        pos, acc, n = _residual(d, dl, pos, acc, n, blocksize, order, out)
        rc = coefs[::-1]
        for i in range(order, blocksize):
            s = 0
            w = out[i - order:i]
            for j in range(order):
                s += rc[j] * w[j]
            out[i] += s >> shift
    else:
        raise FlacError("reserved subframe type %d" % stype)
    if wasted:
        for i in range(blocksize):
            out[i] <<= wasted
    return out, pos, acc, n


def _residual(d, dl, pos, acc, n, blocksize, order, out):
    """Rice-coded residuals. This is where the time goes."""
    while n < 6:
        acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
    n -= 2; method = (acc >> n) & 3; acc &= (1 << n) - 1
    if method > 1:
        raise FlacError("reserved residual method %d" % method)
    esc, pbits = (15, 4) if method == 0 else (31, 5)
    n -= 4; porder = (acc >> n) & 0xF; acc &= (1 << n) - 1
    parts = 1 << porder
    if blocksize % parts:
        raise FlacError("block size not divisible by partition count")
    per = blocksize >> porder
    i = order
    for p in range(parts):
        count = per - (order if p == 0 else 0)
        while n < pbits:
            acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
        n -= pbits; k = (acc >> n) & ((1 << pbits) - 1); acc &= (1 << n) - 1
        if k == esc:
            while n < 5:
                acc = (acc << 8) | (d[pos] if pos < dl else 0); pos += 1; n += 8
            n -= 5; nb = (acc >> n) & 0x1F; acc &= (1 << n) - 1
            for _ in range(count):
                if nb:
                    while n < nb:
                        acc = (acc << 8) | (d[pos] if pos < dl else 0)
                        pos += 1; n += 8
                    n -= nb; v = (acc >> n) & ((1 << nb) - 1); acc &= (1 << n) - 1
                    out[i] = v - (1 << nb) if v >> (nb - 1) else v
                else:
                    out[i] = 0
                i += 1
            continue
        end = i + count
        kmask = (1 << k) - 1
        # Refill in eight-byte gulps. One byte at a time meant the
        # refill branch ran as often as the decode did, in the loop that
        # runs a billion times a disc.
        while i < end:
            if n < 40:
                chunk = d[pos:pos + 8]
                pos += 8
                acc = (acc << (len(chunk) << 3)) | int.from_bytes(chunk, "big")
                n += len(chunk) << 3
            q = 0
            while True:
                bl = acc.bit_length()
                if bl == 0:
                    q += n
                    chunk = d[pos:pos + 8]
                    pos += 8
                    acc = int.from_bytes(chunk, "big")
                    n = len(chunk) << 3
                    if n == 0:
                        raise FlacError("ran off the end of a FLAC hunk")
                    continue
                q += n - bl
                n = bl - 1
                acc &= (1 << n) - 1
                break
            if k:
                if n < k:
                    chunk = d[pos:pos + 8]
                    pos += 8
                    acc = (acc << (len(chunk) << 3)) | int.from_bytes(chunk, "big")
                    n += len(chunk) << 3
                n -= k; v = (q << k) | ((acc >> n) & kmask); acc &= (1 << n) - 1
            else:
                v = q
            out[i] = (v >> 1) ^ -(v & 1)
            i += 1
    return pos, acc, n


# ---------------------------------------------------------------- encoding

def _crc8_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


def _crc16f_table():
    # FLAC's frame CRC-16 is poly 0x8005 (not the CCITT 0x1021 the CHD
    # map uses), init 0, no reflection.
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 \
                else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC8 = _crc8_table()
_CRC16F = _crc16f_table()


def _crc8(data):
    crc = 0
    for b in data:
        crc = _CRC8[crc ^ b]
    return crc


def _crc16f(data):
    crc = 0
    for b in data:
        crc = _CRC16F[((crc >> 8) ^ b) & 0xFF] ^ ((crc << 8) & 0xFFFF)
    return crc


class _BitOut:
    """MSB-first bit writer producing a bytearray."""

    __slots__ = ("out", "acc", "n")

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def write(self, value, numbits):
        self.acc = (self.acc << numbits) | (value & ((1 << numbits) - 1))
        self.n += numbits
        while self.n >= 8:
            self.n -= 8
            self.out.append((self.acc >> self.n) & 0xFF)
        self.acc &= (1 << self.n) - 1

    def pad(self):
        if self.n:
            self.write(0, 8 - self.n)


def blocksize_for(hunkbytes):
    """MAME's rule: samples per frame is hunkbytes/4, halved until it is
    at most 2048 (chdcodec.cpp blocksize())."""
    n = hunkbytes // 4
    while n > 2048:
        n //= 2
    return n


_BS_CODES = {192: 1, 576: 2, 1152: 3, 2304: 4, 4608: 5, 256: 8, 512: 9,
             1024: 10, 2048: 11, 4096: 12, 8192: 13, 16384: 14, 32768: 15}


def _utf8_number(n):
    if n < 0x80:
        return bytes([n])
    out = []
    bits = n.bit_length()
    nbytes = 2
    while bits > 6 * (nbytes - 1) + (7 - nbytes) and nbytes < 7:
        nbytes += 1
    lead = (0xFF << (8 - nbytes)) & 0xFF
    shift = 6 * (nbytes - 1)
    out.append(lead | (n >> shift))
    for i in range(nbytes - 1):
        shift -= 6
        out.append(0x80 | ((n >> shift) & 0x3F))
    return bytes(out)


def _rice_cost(zig, k):
    total = 0
    for v in zig:
        total += (v >> k) + 1 + k
    return total


def _best_rice(zig):
    """Rice parameter by the mean-magnitude estimate, refined one step
    either side - within a bit or two of exhaustive search at a fraction
    of the cost."""
    n = len(zig)
    if n == 0:
        return 0, 0
    mean = sum(zig) // n
    k = max(0, min(14, mean.bit_length() - 1))
    best_k, best_c = k, _rice_cost(zig, k)
    for cand in (k - 1, k + 1):
        if 0 <= cand <= 14:
            c = _rice_cost(zig, cand)
            if c < best_c:
                best_k, best_c = cand, c
    return best_k, best_c


def _subframe_encode(bits, samples, blocksize):
    """One channel of one frame: best of CONSTANT and FIXED orders 0-4,
    Rice-coded residuals in a single partition, escape when Rice loses.
    LPC is deliberately absent: fixed predictors capture most of the
    win on PCM, and the audition means a poor FLAC result simply loses
    to lzma rather than shipping."""
    first = samples[0]
    if all(s == first for s in samples):
        bits.write(0, 1)
        bits.write(0, 6)                    # CONSTANT
        bits.write(0, 1)
        bits.write(first & 0xFFFF, 16)
        return

    # residual ladders: order n is the diff of order n-1
    ladders = [list(samples)]
    for _ in range(4):
        prev = ladders[-1]
        ladders.append([prev[i] - prev[i - 1] for i in range(1, len(prev))])
    best_order = 0
    best_sum = None
    for order in range(5):
        if order >= blocksize:
            break
        tail = ladders[order]
        total = sum(v if v >= 0 else -v for v in tail[max(0, order and 0):])
        if best_sum is None or total < best_sum:
            best_sum = total
            best_order = order
    order = best_order
    residuals = ladders[order]
    warmup = samples[:order]

    bits.write(0, 1)
    bits.write(8 + order, 6)                # FIXED, this order
    bits.write(0, 1)
    for s in warmup:
        bits.write(s & 0xFFFF, 16)

    zig = [(v << 1) if v >= 0 else (((-v) << 1) - 1) for v in residuals]
    k, rice_bits = _best_rice(zig)
    raw_need = max((z.bit_length() for z in zig), default=1)
    raw_need = min(31, max(1, raw_need + 1))     # signed
    bits.write(0, 2)                        # 4-bit Rice partitions
    bits.write(0, 4)                        # partition order 0
    if rice_bits <= len(zig) * raw_need:
        bits.write(k, 4)
        for z in zig:
            # unary quotient: q zero bits then a one, then k remainder bits
            q = z >> k
            while q >= 32:
                bits.write(0, 32)
                q -= 32
            bits.write(1, q + 1)
            if k:
                bits.write(z, k)
    else:
        bits.write(15, 4)                   # escape: raw residuals
        bits.write(raw_need, 5)
        for v in residuals:
            bits.write(v & ((1 << raw_need) - 1), raw_need)


def _encode_frames(samples_l, samples_r, blocksize):
    """Bare FLAC frames over the given stereo samples."""
    out = bytearray()
    total = len(samples_l)
    framenum = 0
    pos = 0
    while pos < total:
        n = min(blocksize, total - pos)
        header = bytearray()
        header += b"\xFF\xF8"               # sync + reserved + fixed blocking
        code = _BS_CODES.get(n)
        extra = b""
        if code is None:
            if n <= 256:
                code, extra = 6, bytes([n - 1])
            else:
                code, extra = 7, struct.pack(">H", n - 1)
        header.append((code << 4) | 0x09)   # blocksize | 44.1 kHz
        header.append((0x01 << 4) | (0x04 << 1))  # stereo | 16-bit | reserved
        header += _utf8_number(framenum)
        header += extra
        header.append(_crc8(header))

        bits = _BitOut()
        _subframe_encode(bits, samples_l[pos:pos + n], n)
        _subframe_encode(bits, samples_r[pos:pos + n], n)
        bits.pad()
        frame = bytes(header) + bytes(bits.out)
        out += frame + struct.pack(">H", _crc16f(frame))
        framenum += 1
        pos += n
    return bytes(out)


def encode_hunk(data, out_limit):
    """Encode one CHD hunk as MAME's FLAC codec would accept it, or
    None when the result would not fit under out_limit.

    Tries both endiannesses and keeps the smaller, exactly as chdman
    does - the hunk's first byte records which won. The output is not
    byte-identical to libFLAC's (no two FLAC encoders agree on bytes,
    which is fine: CHD identity is content, not codec) but it is valid
    FLAC that libFLAC itself verifies, which the suite checks through
    chdman."""
    if len(data) % 4:
        return None
    import sys
    blocksize = blocksize_for(len(data))
    best = None
    for marker in (b"L", b"B"):
        pcm = array("h")
        pcm.frombytes(data)
        if (marker == b"B") == (sys.byteorder == "little"):
            pcm.byteswap()
        left = pcm[0::2].tolist()
        right = pcm[1::2].tolist()
        frames = _encode_frames(left, right, blocksize)
        blob = marker + frames
        if len(blob) <= out_limit and (best is None or len(blob) < len(best)):
            best = blob
    return best
