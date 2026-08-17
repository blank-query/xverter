#!/usr/bin/env python3
"""Generate synthetic XGD1 / XGD2 / XGD3 disc images for CI.

Real game images cannot go in CI: they are copyrighted and 7-9 GB. These
are tiny, built from scratch, and exercise the same code paths - each one
carries a valid executable and sits at its generation's real partition
base, so detection, trimming and the whole conversion matrix behave as
they do on a retail disc.

  XGD1  base 0x18300000  original Xbox, default.xbe
  XGD2  base 0x0FD90000  Xbox 360,      default.xex
  XGD3  base 0x02080000  Xbox 360,      default.xex

The video partition preceding the game partition is written sparse, so a
387 MB XGD1 image costs almost no disk and no I/O.
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xverter.formats import xdvdfs as xdvdfs_mod   # noqa: E402

GENERATIONS = {
    "xgd1": (0x18300000, "xbe"),
    "xgd2": (0x0FD90000, "xex"),
    "xgd3": (0x02080000, "xex"),
}


def make_xex(title_id, media_id, disc=1, discs=1):
    """Minimal XEX2 carrying an execution-info record (optional header
    0x40006), which is all xverter reads from it."""
    field_count = 1
    table_end = 0x18 + field_count * 8
    exec_off = table_end
    head = bytearray(0x18)
    head[0:4] = b"XEX2"
    struct.pack_into(">I", head, 0x14, field_count)
    table = struct.pack(">II", 0x40006, exec_off)
    exec_info = struct.pack(">IIII", media_id, 0, 0, title_id) + \
        bytes([0, 0, disc, discs]) + b"\x00" * 4
    return bytes(head) + table + exec_info


def make_xbe(title_id, title):
    """Minimal XBE whose certificate carries the title id and display
    name. base_addr/cert_addr are the only pointers xverter follows."""
    base_addr = 0x10000
    cert_off = 0x200
    cert_addr = base_addr + cert_off
    head = bytearray(0x11C)
    head[0:4] = b"XBEH"
    struct.pack_into("<I", head, 0x104, base_addr)
    struct.pack_into("<I", head, 0x118, cert_addr)
    cert = bytearray(0x5C)
    struct.pack_into("<I", cert, 0x0, 0x5C)
    struct.pack_into("<I", cert, 0x8, title_id)
    name = title.encode("utf-16-le")[:0x50]
    cert[0xC:0xC + len(name)] = name
    out = bytearray(cert_off + 0x5C)
    out[0:len(head)] = head
    out[cert_off:cert_off + 0x5C] = cert
    return bytes(out)


def build_tree(root, kind, title_id, media_id, title):
    os.makedirs(root, exist_ok=True)
    if kind == "xex":
        payload = make_xex(title_id, media_id)
        exe = "default.xex"
    else:
        payload = make_xbe(title_id, title)
        exe = "default.xbe"
    with open(os.path.join(root, exe), "wb") as f:
        f.write(payload)
    # a little content so the matrix has files to hash and compare
    with open(os.path.join(root, "readme.txt"), "wb") as f:
        f.write(b"synthetic xverter CI fixture\n" * 64)
    sub = os.path.join(root, "media")
    os.makedirs(sub, exist_ok=True)
    for i in range(3):
        with open(os.path.join(sub, "asset%02d.bin" % i), "wb") as f:
            # deterministic, compressible-but-not-trivial content
            f.write(bytes((i * 7 + j) % 251 for j in range(4096)) * 8)
    return root


def build_image(gen, out_path, workdir):
    base, kind = GENERATIONS[gen]
    tree = build_tree(os.path.join(workdir, gen + "_tree"), kind,
                      title_id=0x4D530000 + int(gen[-1]),
                      media_id=0x00394000 + int(gen[-1]),
                      title="xVerter CI %s" % gen.upper())
    partition = os.path.join(workdir, gen + "_part.iso")
    if os.path.exists(partition):
        os.remove(partition)
    xdvdfs_mod.pack(tree, partition)

    # full image = sparse video partition, then the game partition at base
    with open(out_path, "wb") as out:
        out.truncate(base)              # sparse: costs no blocks
        out.seek(base)
        with open(partition, "rb") as p:
            while True:
                chunk = p.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    os.remove(partition)

    with open(out_path, "rb") as f:
        got = xdvdfs_mod.find_base(f)
    if got != base:
        raise SystemExit("%s: detected base 0x%X, expected 0x%X" % (gen, got, base))
    st = os.stat(out_path)
    return st.st_size, st.st_blocks * 512


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("outdir", help="directory to write the images into")
    ap.add_argument("--only", choices=sorted(GENERATIONS), action="append")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    wanted = args.only or sorted(GENERATIONS)
    for gen in wanted:
        path = os.path.join(args.outdir, "%s.iso" % gen)
        apparent, on_disk = build_image(gen, path, args.outdir)
        print("%s  base 0x%08X  apparent %d bytes  on disk %d bytes  %s"
              % (gen.upper(), GENERATIONS[gen][0], apparent, on_disk, path))


if __name__ == "__main__":
    main()
