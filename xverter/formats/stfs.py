"""STFS (LIVE/CON/PIRS) content package reading - original implementation.

Format facts from the free60 project's STFS documentation and xverter's
own packet archaeology; earlier xverter versions vendored the
XBLA-Extract / extract360.py / wxPirs reader lineage, which taught this
module the format before it was rewritten from the spec.

Layout: a signed header (size field at 0x340, block-aligned up) is
followed by 0x1000-byte blocks. SHA-1 hash tables are interleaved among
the data blocks: an L0 table before every 170 data blocks, an L1 table
before every 170 L0 tables, an L2 above that; the volume descriptor
(0x379) holds the root hash of the top table plus the file-table
location and allocated block count. When the descriptor's separation bit
is set every table occupies two blocks (primary + backup copy). Each
0x18-byte table entry is [20-byte SHA-1][status][24-bit next block] -
the next-block field is the file allocation chain, which this reader
follows (rather than assuming consecutive blocks, which only happens to
hold for read-only packages).

Integrity: extraction verifies as it reads - the table chain is checked
up to the descriptor's root hash first, then every data block is hashed
against its L0 entry as it streams out, so a bit-rotted package fails
loudly instead of extracting silently wrong. verify_chains() checks
every allocated block including slack the files don't reference.

STFS as an *output* format is not built yet (LIVE/PIRS signatures are
Microsoft-private-key RSA; the ecosystem norm is junk signature bytes,
which modded consoles and emulators don't check).
"""

import hashlib
import os
import struct

STFS_MAGICS = (b"LIVE", b"CON ", b"PIRS")
CONTENT_TYPE_OFFSET = 0x344
CONTENT_TYPE_GOD_GAME = 0x7000
CONTENT_TYPE_GOD_XBOXORIG = 0x5000

BLOCK = 0x1000
FT_ENTRY = 0x40
HASH_ENTRY = 0x18
PER_TABLE = 170
HEADER_SIZE_OFFSET = 0x340
DESCRIPTOR_OFFSET = 0x379
TITLE_OFFSET = 0x411


class StfsError(Exception):
    pass


# ------------------------------------------------------------- geometry

def _tables_through(c):
    """Number of hash-table slots at sites <= data block c: one L0 per
    170 blocks plus one higher-level table per 170^n blocks once that
    level exists. Closed form of the interleave arithmetic, validated
    against real 1-, 2- and 3-level packages."""
    n = c // PER_TABLE + 1
    level_span = PER_TABLE
    while c >= level_span:
        n += c // (level_span * PER_TABLE) + 1
        level_span *= PER_TABLE
    return n


class _Package:
    """Parsed geometry + hash tables of an open STFS package."""

    def __init__(self, f):
        self.f = f
        magic = f.read(4)
        if magic not in STFS_MAGICS:
            raise StfsError("not a LIVE/CON/PIRS package: %r" % magic)
        self.magic = magic

        f.seek(HEADER_SIZE_OFFSET)
        header_size = struct.unpack(">I", f.read(4))[0]

        f.seek(DESCRIPTOR_OFFSET)
        d = f.read(0x24)
        if len(d) != 0x24 or d[0] != 0x24:
            raise StfsError("bad STFS volume descriptor (size byte %r)"
                            % (d[:1],))
        # Separation bit SET = single-width tables (read-only packages
        # carry no backup copies); CLEAR = every table is primary+backup.
        self.doubled = not (d[2] & 1)
        self.ft_blocks = struct.unpack("<H", d[3:5])[0]
        self.ft_start = d[5] | (d[6] << 8) | (d[7] << 16)
        self.top_hash = d[8:0x1C]
        self.nblocks = struct.unpack(">I", d[0x1C:0x20])[0]
        if self.nblocks <= 0:
            raise StfsError("descriptor reports no allocated blocks")

        self.width = BLOCK * (2 if self.doubled else 1)
        # L0 table #0 sits at the block-aligned end of the header;
        # data block 0 follows it.
        self.base = (header_size + BLOCK - 1) & ~(BLOCK - 1)

    def data_off(self, c):
        """Absolute file offset of data block c: the aligned header end
        (where L0 table #0 sits), plus every table slot at sites <= c,
        plus the data blocks before c."""
        return self.base + self.width * _tables_through(c) + BLOCK * c

    def read_block(self, pos):
        self.f.seek(pos)
        b = self.f.read(BLOCK)
        return b + b"\x00" * (BLOCK - len(b))

    # -------------------------------------------------- table loading

    def load_tables(self):
        """Locate, load and chain-verify every hash table up to the
        descriptor's root hash. Populates self.l0 (list of table bytes,
        one per 170 data blocks). Content-addressed: each site's slots
        are enumerated from the interleave arithmetic and a slot must
        prove which table it is by hashing what sits below it."""
        n_l0 = (self.nblocks + PER_TABLE - 1) // PER_TABLE
        sites = []
        for t in range(n_l0):
            c0 = t * PER_TABLE
            m = (_tables_through(c0) - _tables_through(c0 - 1)) \
                if c0 else 1
            first = self.data_off(c0)
            sites.append((c0, [first - k * self.width
                               for k in range(1, m + 1)]))

        def match(pos, want_entry0):
            for cp in ((pos, pos + BLOCK) if self.doubled else (pos,)):
                tb = self.read_block(cp)
                if tb[0:20] == want_entry0:
                    return tb
            return None

        self.l0 = [None] * n_l0
        l0_hash = [None] * n_l0
        extra = []
        for t, (c0, slots) in enumerate(sites):
            want = hashlib.sha1(self.read_block(self.data_off(c0))).digest()
            tb = None
            for pos in slots:
                tb = match(pos, want)
                if tb is not None:
                    extra.extend(p for p in slots if p != pos)
                    break
            if tb is None:
                raise StfsError(
                    "cannot locate L0 hash table %d (site block %d) - "
                    "package corrupt or truncated" % (t, c0))
            self.l0[t] = tb
            l0_hash[t] = hashlib.sha1(tb).digest()

        levels = 1
        child = l0_hash
        while len(child) > 1:
            levels += 1
            parents = []
            for j in range((len(child) + PER_TABLE - 1) // PER_TABLE):
                tb = None
                for pos in extra:
                    tb = match(pos, child[j * PER_TABLE])
                    if tb is not None:
                        extra.remove(pos)
                        break
                if tb is None:
                    raise StfsError("cannot locate level-%d hash table %d"
                                    % (levels, j))
                lo = j * PER_TABLE
                for i, ch in enumerate(child[lo:lo + PER_TABLE]):
                    if tb[i * HASH_ENTRY:i * HASH_ENTRY + 20] != ch:
                        raise StfsError(
                            "hash chain FAILED: level-%d table %d entry %d"
                            % (levels - 1, j, i))
                parents.append(hashlib.sha1(tb).digest())
            child = parents
        if extra:
            raise StfsError("%d hash-table slot(s) unaccounted for - "
                            "layout mismatch" % len(extra))
        if child[0] != self.top_hash:
            raise StfsError("hash chain FAILED: top table does not match "
                            "the volume descriptor root hash")
        self.levels = levels
        return self

    # ---------------------------------------------------- block access

    def entry(self, c):
        """(sha1, status, next_block) for data block c."""
        e = self.l0[c // PER_TABLE]
        o = (c % PER_TABLE) * HASH_ENTRY
        return (e[o:o + 20], e[o + 20],
                (e[o + 21] << 16) | (e[o + 22] << 8) | e[o + 23])

    def verified_block(self, c):
        """Data block c, hash-checked against its L0 entry."""
        if not 0 <= c < self.nblocks:
            raise StfsError("block %d outside allocated range (%d)"
                            % (c, self.nblocks))
        b = self.read_block(self.data_off(c))
        if hashlib.sha1(b).digest() != self.entry(c)[0]:
            raise StfsError("hash chain FAILED: data block %d does not "
                            "match its L0 entry" % c)
        return b

    def chain(self, start, count):
        """Follow the block chain from start for count blocks."""
        c = start
        for _ in range(count):
            yield c
            c = self.entry(c)[2]

    # ------------------------------------------------------ file table

    def entries(self):
        """Parse the file table into entry dicts."""
        raw = b"".join(self.verified_block(c)
                       for c in self.chain(self.ft_start,
                                           max(self.ft_blocks, 1)))
        paths = {0xFFFF: ""}
        out = []
        for i in range(len(raw) // FT_ENTRY):
            e = raw[i * FT_ENTRY:(i + 1) * FT_ENTRY]
            flags = e[0x28]
            name_len = flags & 0x3F
            if name_len == 0:
                break
            if name_len > 0x28:
                continue
            name = e[:name_len].decode("ascii", "replace")
            blocks = e[0x29] | (e[0x2A] << 8) | (e[0x2B] << 16)
            start = e[0x2F] | (e[0x30] << 8) | (e[0x31] << 16)
            parent = struct.unpack(">H", e[0x32:0x34])[0]
            size = struct.unpack(">I", e[0x34:0x38])[0]
            is_dir = bool(flags & 0x80)
            path = paths.get(parent, "") + name
            if is_dir:
                paths[i] = path + "/"
            out.append({"id": i, "name": name,
                        "path": path, "size": 0 if is_dir else size,
                        "is_dir": is_dir, "startclust": start,
                        "blocks": blocks})
        return out


# ------------------------------------------------------------ public API

def _open(path):
    f = open(path, "rb")
    try:
        return _Package(f)
    except Exception:
        f.close()
        raise


def verify_chains(path, progress=None):
    """Verify the package's complete internal SHA-1 hash chain: the
    table chain up to the descriptor's root hash, then every allocated
    data block (files, file table and slack alike) against its L0
    entry. Raises StfsError on any mismatch; returns a stats dict."""
    pkg = _open(path)
    try:
        pkg.load_tables()
        for c in range(pkg.nblocks):
            pkg.verified_block(c)
            if progress and c % 64 == 0:
                progress(c + 1, pkg.nblocks)
        if progress:
            progress(pkg.nblocks, pkg.nblocks)
        return {"blocks": pkg.nblocks, "l0_tables": len(pkg.l0),
                "levels": pkg.levels, "doubled_tables": pkg.doubled}
    finally:
        pkg.f.close()


def list_entries(path):
    """List package entries: dicts with name/path/size/is_dir/startclust."""
    pkg = _open(path)
    try:
        pkg.load_tables()
        return pkg.entries()
    finally:
        pkg.f.close()


def _safe_relpath(p):
    parts = [x for x in p.split("/") if x]
    for x in parts:
        if x in (".", "..") or "\\" in x or "\x00" in x:
            raise StfsError("refusing unsafe entry path %r" % p)
    return os.path.join(*parts) if parts else ""


def extract(stfs_path, out_dir, manifest=None, verify=True,
            progress=None):
    """Extract all entries of an STFS package into out_dir, streaming
    block by block. The hash-table chain is always verified to the root
    before anything is written (the chain is also the block-allocation
    map, so it must be loaded regardless); every data block is
    additionally checked against its L0 entry as it is read unless
    verify=False. If manifest is a dict, per-file SHA-1 hashes are
    recorded inline."""
    pkg = _open(stfs_path)
    try:
        pkg.load_tables()   # also resolves geometry; cheap either way
        entries = pkg.entries()
        if not entries:
            raise StfsError("empty STFS file table")
        os.makedirs(out_dir, exist_ok=True)
        n = 0
        grand = sum(e["size"] for e in entries if not e["is_dir"]) or 1
        done = 0
        for e in entries:
            rel = _safe_relpath(e["path"])
            dest = os.path.join(out_dir, rel)
            if e["is_dir"]:
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest) or out_dir, exist_ok=True)
            remaining = e["size"]
            nblocks = (remaining + BLOCK - 1) // BLOCK
            h = hashlib.sha1() if manifest is not None else None
            with open(dest, "wb") as o:
                for c in pkg.chain(e["startclust"], nblocks):
                    b = (pkg.verified_block(c) if verify
                         else pkg.read_block(pkg.data_off(c)))
                    chunk = b[:remaining] if remaining < BLOCK else b
                    o.write(chunk)
                    if h is not None:
                        h.update(chunk)
                    remaining -= len(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, grand)
            if manifest is not None:
                manifest[rel.replace(os.sep, "/")] = h.hexdigest()
            n += 1
        if n == 0:
            raise StfsError("no files extracted from %s" % stfs_path)
        return n
    finally:
        pkg.f.close()


def read_title(path):
    """Best-effort display title from the package header region."""
    with open(path, "rb") as f:
        f.seek(TITLE_OFFSET)
        raw = f.read(0x100)
    title = raw.decode("utf-16-be", "replace").split("\x00", 1)[0].strip()
    return title or None


def cli(argv=None):
    """Standalone CLI:  xv-stfs {list|extract|title|verify} package
    [outdir]"""
    import argparse
    import sys as _sys
    ap = argparse.ArgumentParser(
        prog="xv-stfs",
        description="STFS (LIVE/CON/PIRS) content package reader: list, "
                    "extract (hash-chain-verified), read the display "
                    "title, or verify the full chain.")
    ap.add_argument("action", choices=["list", "extract", "title", "verify"])
    ap.add_argument("package")
    ap.add_argument("outdir", nargs="?", help="required for extract")
    a = ap.parse_args(argv)
    try:
        if a.action == "verify":
            st = verify_chains(a.package)
            print("hash chain OK: %d blocks, %d L0 tables, %d level(s)%s"
                  % (st["blocks"], st["l0_tables"], st["levels"],
                     ", doubled tables" if st["doubled_tables"] else ""))
        elif a.action == "list":
            for e in list_entries(a.package):
                print("%12d  %s" % (e["size"], e["path"]))
        elif a.action == "extract":
            if not a.outdir:
                ap.error("extract needs an output directory")
            n = extract(a.package, a.outdir)
            print("extracted %d files (hash-chain verified)" % n)
        elif a.action == "title":
            print(read_title(a.package) or "(no title)")
    except StfsError as e:
        print("ERROR: %s" % e, file=_sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(cli())
