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
