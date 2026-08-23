#!/usr/bin/env python3
"""Generate synthetic XGD1 / XGD2 / XGD3 disc images for CI - and
deliberately broken ones for testing error paths.

Every byte here is written from the XDVDFS layout directly. Nothing
imports xverter. That is the point: a fixture built by the code under
test cannot detect a regression in that code, because the fixture
regresses with it. These images are an independent second opinion about
what the format is.

  XGD1  base 0x18300000  original Xbox, default.xbe
  XGD2  base 0x0FD90000  Xbox 360,      default.xex
  XGD3  base 0x02080000  Xbox 360,      default.xex

Layout, relative to the game-partition base:
  sector 32      volume descriptor: MAGIC, <IIQ root_sector root_size 0,
                 padding, MAGIC again
  sector 33..    directory tables, one sector each
  then           file data, sector-aligned

Directory entries: <HH left right> (dword offsets, 0/0xFFFF = none),
<II start_sector size>, <B attr>, <B name_len>, name; each entry padded
to a 4-byte boundary and never crossing a sector. Entries form a BST
whose in-order traversal is the sorted name order.

The video partition ahead of the game partition is sparse, so a 387 MB
XGD1 image costs a few hundred KB on disk.
"""
import argparse
import os
import struct

SECTOR = 0x800
MAGIC = b"MICROSOFT*XBOX*MEDIA"
ATTR_DIR = 0x10
ATTR_ARCHIVE = 0x20

GENERATIONS = {
    "xgd1": (0x18300000, "xbe"),
    "xgd2": (0x0FD90000, "xex"),
    "xgd3": (0x02080000, "xex"),
}

CORRUPTIONS = {
    "none": "a valid image",
    "truncated": "file ends before the extent its tables describe",
    "bad-magic": "volume descriptor magic clobbered - no partition base",
    "extent-past-eof": "a file entry claims data beyond the image",
    "bad-exe": "default.xex/.xbe header magic clobbered",
    "table-cycle": "a directory entry points back at itself",
}


# ----------------------------------------------------------- executables

def make_xex(title_id, media_id, disc=1, discs=1):
    """XEX2 with an execution-info record (optional header key 0x40006)."""
    field_count = 1
    exec_off = 0x18 + field_count * 8
    head = bytearray(0x18)
    head[0:4] = b"XEX2"
    struct.pack_into(">I", head, 0x14, field_count)
    table = struct.pack(">II", 0x40006, exec_off)
    info = struct.pack(">IIII", media_id, 0, 0, title_id) \
        + bytes([0, 0, disc, discs]) + b"\x00" * 4
    return bytes(head) + table + info


def make_xbe(title_id, title):
    """XBE whose certificate carries the title id and display name."""
    base_addr, cert_off = 0x10000, 0x200
    head = bytearray(0x11C)
    head[0:4] = b"XBEH"
    struct.pack_into("<I", head, 0x104, base_addr)
    struct.pack_into("<I", head, 0x118, base_addr + cert_off)
    cert = bytearray(0x5C)
    struct.pack_into("<I", cert, 0x0, 0x5C)
    struct.pack_into("<I", cert, 0x8, title_id)
    name = title.encode("utf-16-le")[:0x50]
    cert[0xC:0xC + len(name)] = name
    out = bytearray(cert_off + 0x5C)
    out[0:len(head)] = head
    out[cert_off:cert_off + 0x5C] = cert
    return bytes(out)


# -------------------------------------------------------- directory tree

def _span(name):
    return (14 + len(name) + 3) & ~3


def _render_table(entries, corrupt=None):
    """entries: list of dicts with name/start/size/attr, any order.

    Laid out as a balanced BST in preorder so in-order traversal yields
    sorted names, which is what the format expects."""
    ents = sorted(entries, key=lambda e: e["name"].upper())
    for e in ents:
        e["raw"] = e["name"].encode("cp1252")

    placed = []

    def place(lo, hi, cursor):
        """Assign byte offsets preorder; return (offset, next_cursor)."""
        if lo > hi:
            return None, cursor
        mid = (lo + hi) // 2
        e = ents[mid]
        span = _span(e["raw"])
        if (cursor % SECTOR) + span > SECTOR:      # never cross a sector
            cursor = (cursor // SECTOR + 1) * SECTOR
        e["off"] = cursor
        placed.append(e)
        cursor += span
        e["left"], cursor = place(lo, mid - 1, cursor)
        e["right"], cursor = place(mid + 1, hi, cursor)
        return e["off"], cursor

    place(0, len(ents) - 1, 0)
    size = max((e["off"] + _span(e["raw"]) for e in ents), default=0)
    size = max(size, 1)
    buf = bytearray(((size + SECTOR - 1) // SECTOR) * SECTOR)
    for e in ents:
        o = e["off"]
        left = 0xFFFF if e["left"] is None else e["left"] // 4
        right = 0xFFFF if e["right"] is None else e["right"] // 4
        if corrupt == "table-cycle" and e is ents[0]:
            right = o // 4                          # points at itself
        struct.pack_into("<HH", buf, o, left, right)
        struct.pack_into("<II", buf, o + 4, e["start"], e["size"])
        buf[o + 12] = e["attr"]
        buf[o + 13] = len(e["raw"])
        buf[o + 14:o + 14 + len(e["raw"])] = e["raw"]
    return bytes(buf)


# ------------------------------------------------------------- the image

def build_partition(kind, corrupt, title_id, media_id, title):
    """Return (partition_bytes, declared_extent). The partition is what
    lives at the generation's base offset."""
    exe_name = "default.xex" if kind == "xex" else "default.xbe"
    exe = make_xex(title_id, media_id) if kind == "xex" \
        else make_xbe(title_id, title)
    if corrupt == "bad-exe":
        exe = b"\x00\x00\x00\x00" + exe[4:]

    files = [
        (exe_name, exe),
        ("readme.txt", b"synthetic xverter CI fixture\n" * 64),
    ]
    media = [("asset%02d.bin" % i,
              bytes((i * 7 + j) % 251 for j in range(4096)) * 8)
             for i in range(3)]

    # sector 32 descriptor, 33 root table, 34 media table, then data
    root_sector, media_sector = 33, 34
    cursor = 35
    placed = {}
    for name, data in files + media:
        placed[name] = (cursor, len(data))
        cursor += max(1, -(-len(data) // SECTOR))

    media_entries = [{"name": n, "start": placed[n][0],
                      "size": placed[n][1], "attr": ATTR_ARCHIVE}
                     for n, _ in media]
    media_tbl = _render_table(media_entries, corrupt)

    root_entries = [{"name": n, "start": placed[n][0],
                     "size": placed[n][1], "attr": ATTR_ARCHIVE}
                    for n, _ in files]
    root_entries.append({"name": "media", "start": media_sector,
                         "size": len(media_tbl), "attr": ATTR_DIR})
    if corrupt == "extent-past-eof":
        root_entries[0]["size"] = 64 << 20         # claims 64 MiB
    root_tbl = _render_table(root_entries, corrupt)

    out = bytearray(cursor * SECTOR)
    desc = MAGIC + struct.pack("<IIQ", root_sector, len(root_tbl), 0)
    desc += b"\x00" * (SECTOR - 2 * len(MAGIC) - 16) + MAGIC
    if corrupt == "bad-magic":
        desc = b"\x00" * len(MAGIC) + desc[len(MAGIC):]
    out[32 * SECTOR:32 * SECTOR + SECTOR] = desc
    out[root_sector * SECTOR:root_sector * SECTOR + len(root_tbl)] = root_tbl
    out[media_sector * SECTOR:media_sector * SECTOR + len(media_tbl)] = media_tbl
    for name, data in files + media:
        s = placed[name][0] * SECTOR
        out[s:s + len(data)] = data

    extent = max(placed[n][0] * SECTOR + placed[n][1] for n, _ in files + media)
    return bytes(out), extent


def build_image(gen, out_path, corrupt="none"):
    base, kind = GENERATIONS[gen]
    part, extent = build_partition(
        kind, corrupt,
        title_id=0x4D530000 + int(gen[-1]),
        media_id=0x00394000 + int(gen[-1]),
        title="xVerter CI %s" % gen.upper())
    with open(out_path, "wb") as f:
        f.truncate(base)                 # sparse video partition
        f.seek(base)
        f.write(part)
    if corrupt == "truncated":
        os.truncate(out_path, os.path.getsize(out_path) - (64 << 10))
    st = os.stat(out_path)
    return st.st_size, st.st_blocks * 512, extent


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--only", choices=sorted(GENERATIONS), action="append")
    ap.add_argument("--corrupt", choices=sorted(CORRUPTIONS), default="none",
                    help="; ".join("%s: %s" % kv for kv in CORRUPTIONS.items()))
    ap.add_argument("--suffix", default="",
                    help="appended to each filename, e.g. -truncated")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    for gen in (args.only or sorted(GENERATIONS)):
        path = os.path.join(args.outdir, "%s%s.iso" % (gen, args.suffix))
        apparent, on_disk, extent = build_image(gen, path, args.corrupt)
        print("%s  base 0x%08X  %-15s apparent %d  on disk %d  extent %d"
              % (gen.upper(), GENERATIONS[gen][0], args.corrupt,
                 apparent, on_disk, extent))


if __name__ == "__main__":
    main()
