"""Output builders that delegate to maintained reference tools, with every
result verified by xverter's own readers before being reported as success.

  dir -> ISO : xverter's native deterministic XDVDFS writer
               (formats/xdvdfs.py pack), verified by stream-hashing every
               file inside the built ISO against the source-tree manifest
  ISO -> GoD : xverter's native GoD writer (formats/god.py build), verified
               by walking the container's full SHA-1 hash tree with our GoD
               reader and auditing its size against the source's allocation
               extent (never trust a writer, including our own)
"""

import filecmp
import os
import tempfile

from . import god as god_mod
from . import xdvdfs as xdvdfs_mod


class BuildError(Exception):
    pass


def _trees_identical(a, b):
    def walk(dc):
        if dc.left_only or dc.right_only or dc.diff_files or dc.funny_files:
            return False
        return all(walk(sub) for sub in dc.subdirs.values())
    return walk(filecmp.dircmp(a, b, shallow=False))


def build_iso_from_tree(tree, out_iso, verify=True, manifest=None,
                        progress=None, verify_progress=None):
    """Pack a prebuilt node tree - files supplied by openers rather than
    by a directory on disk - into a bare XDVDFS ISO.

    Identical output to build_iso() given the same files: the layout
    depends on names and sizes, not on where the bytes come from. This
    is what lets a zar go straight to an ISO without being unpacked to
    a scratch directory first."""
    if os.path.exists(out_iso):
        raise BuildError("output already exists: %s" % out_iso)
    try:
        xdvdfs_mod.pack_tree(tree, out_iso, progress=progress)
    except xdvdfs_mod.XdvdfsError as e:
        raise BuildError("XDVDFS pack failed: %s" % e)
    if verify:
        if not manifest:
            raise BuildError("a streamed pack cannot be verified without "
                             "the source manifest")
        got = xdvdfs_mod.hash_walk(out_iso, progress=verify_progress)
        if manifest != got:
            missing = sorted(set(manifest) - set(got))[:3]
            extra = sorted(set(got) - set(manifest))[:3]
            diff = sorted(k for k in set(manifest) & set(got)
                          if manifest[k] != got[k])[:3]
            os.unlink(out_iso)
            raise BuildError("built ISO failed manifest verification "
                             "(missing=%s extra=%s content-diff=%s)"
                             % (missing, extra, diff))
    return out_iso


def build_iso(gamedir, out_iso, verify=True, manifest=None,
              progress=None, verify_progress=None):
    """Pack an extracted game dir into a bare XDVDFS ISO with xverter's
    native deterministic writer (byte-identical output for identical input
    trees; see the layout contract in formats/xdvdfs.py)."""
    if os.path.exists(out_iso):
        raise BuildError("output already exists: %s" % out_iso)
    try:
        xdvdfs_mod.pack(gamedir, out_iso, progress=progress)
    except xdvdfs_mod.XdvdfsError as e:
        raise BuildError("XDVDFS pack failed: %s" % e)
    if verify:
        # Manifest verification: hash the source tree once, then stream-hash
        # every file inside the built ISO without writing anything.
        want = manifest if manifest else xdvdfs_mod.hash_tree(gamedir)
        got = xdvdfs_mod.hash_walk(out_iso, progress=verify_progress)
        if want != got:
            missing = sorted(set(want) - set(got))[:3]
            extra = sorted(set(got) - set(want))[:3]
            diff = sorted(k for k in set(want) & set(got)
                          if want[k] != got[k])[:3]
            os.unlink(out_iso)
            raise BuildError("built ISO failed manifest verification "
                             "(missing=%s extra=%s content-diff=%s)"
                             % (missing, extra, diff))


def _allocation_extent(iso_path):
    """Last allocated byte inside the image's game partition, relative to
    the partition base: max end of every file extent and directory table.
    This is the minimum data size a lossless GoD container must hold."""
    import struct
    with open(iso_path, "rb") as f:
        base = xdvdfs_mod.find_base(f)
        f.seek(base + 32 * xdvdfs_mod.SECTOR + len(xdvdfs_mod.MAGIC))
        root_sector, root_size = struct.unpack("<II", f.read(8))
        extent = 33 * xdvdfs_mod.SECTOR            # volume descriptor region
        stack = [(root_sector, root_size)]
        while stack:
            sector, size = stack.pop()
            extent = max(extent, sector * xdvdfs_mod.SECTOR + size)
            for _name, start, sz, attr in xdvdfs_mod.walk_table(
                    xdvdfs_mod.read_table(f, base, sector, size)):
                if attr & 0x10:
                    stack.append((start, sz))
                else:
                    extent = max(extent, start * xdvdfs_mod.SECTOR + sz)
    return extent


def build_god(iso_path, out_dir, verify=True, progress=None,
              verify_progress=None):
    """Convert an ISO into a GoD container tree with xverter's native
    writer (formats/god.py build); returns the path of the created GoD
    header file.

    Trimming writes only up to the source's true allocation extent -
    volume descriptor, every directory table, every file - computed by
    our own XDVDFS reader. (iso2god trims to the last FILE extent only;
    an image whose root directory table sits past its last file gets
    silently truncated into an unreadable container, rc=0. Present
    through at least 1.8.1; fixed here by construction.) The result is
    still audited: the data region is re-measured against an
    independently recomputed extent, and the container's full SHA-1
    hash tree is walked by our GoD reader."""
    os.makedirs(out_dir, exist_ok=True)
    try:
        header = god_mod.build(iso_path, out_dir,
                               progress=progress)
    except god_mod.GodError as e:
        raise BuildError("GoD build failed: %s" % e)
    extent = _allocation_extent(iso_path)
    with god_mod.GodStream(header) as s:
        got = s.size
    if got < extent:
        raise BuildError("GoD data region %d bytes < source allocation "
                         "extent %d" % (got, extent))
    if verify:
        god_mod.convert(header, None, verify_only=True,
                        progress=verify_progress)
    return header


GOD_CONTENT_DIRS = ("00007000", "00005000")   # 360 GoD, Xbox Originals


def _find_god_header(root):
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) in GOD_CONTENT_DIRS:
            for f in filenames:
                if not f.endswith(".data") and \
                        os.path.isdir(os.path.join(dirpath, f + ".data")):
                    return os.path.join(dirpath, f)
    return None
