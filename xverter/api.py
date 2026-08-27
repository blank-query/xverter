"""Stable library entry points for embedding xverter as a conversion engine.

Two functions, both non-interactive (no tty/stdout, raise on error):

    convert(src, out_dir, out_kind="god"|"stfs", verify=False, progress=cb)
        -> written package-header path
    probe(src) -> {"title_id": int, "kind": "god"|"stfs", "name": str|None}

`convert` wraps the same routing the CLI uses, so every source it accepts the
CLI accepts (iso/chd/cci/cso/god/stfs/7z/zip/gamedir). It writes the native
package tree *directly* under out_dir - `<content_dir>/<pkg>` (+ `<pkg>.data/`)
for GoD, `<content_dir>/<contentid>` for STFS - with no TitleID nesting, so the
caller owns out_dir as the title directory. `progress`, if given, is called
`progress(done, total)` for the dominant write stage.

`probe` reads only the header/executable - no decode, no conversion - for
building a catalog by title id before deciding to convert.
"""

import contextlib
import io
import os
import shutil
import struct
import tempfile
from types import SimpleNamespace

from . import detect as _detect
from . import titledb as _titledb
from .formats import god as _god
from .formats import stfs as _stfs


class ConvertError(Exception):
    """Any failure of convert() or probe(). The message names the cause."""


# ----------------------------------------------------------------- convert

def convert(src, out_dir, out_kind="god", verify=False, progress=None):
    """Convert `src` into a native `out_kind` package tree under `out_dir`.

    Returns the written package-header path. Raises ConvertError on failure.
    A source already of the target kind (god->god, stfs->stfs) is copied
    verbatim - byte-for-byte, no re-synthesis.
    """
    src = os.fspath(src)
    out_dir = os.fspath(out_dir)
    if out_kind not in ("god", "stfs"):
        raise ConvertError("out_kind must be 'god' or 'stfs', got %r" % out_kind)
    try:
        kind, path = _detect.detect(src)
    except Exception as e:
        raise ConvertError("cannot read source %s: %s" % (src, e)) from e
    os.makedirs(out_dir, exist_ok=True)

    if kind == out_kind:
        return _passthrough(kind, path, out_dir)

    if out_kind == "stfs":
        raise ConvertError(
            "cross-format -> stfs needs an explicit content type and is not yet "
            "exposed through convert(); an stfs source passes through verbatim. "
            "Use the CLI with --content-type for cross-format stfs.")

    # out_kind == "god": build through the CLI routing into a private
    # scratch, then lift the content_dir tree up under out_dir.
    from . import cli as _cli
    work = tempfile.mkdtemp(prefix="xv-convert-")
    try:
        scratch = os.path.join(work, "out.god")
        args = SimpleNamespace(
            input=src, output=scratch, no_verify=not verify,
            content_type=None, media_id=None, title_id=None,
            title=None, thumbnail=None, store=False,
            progress=progress, workdir=None, scratch="disk")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                _cli.cmd_convert(args)
        except _cli.CliError as e:
            raise ConvertError(str(e)) from e
        except ConvertError:
            raise
        except BaseException as e:
            raise ConvertError("conversion failed: %s" % e) from e
        return _lift_tree(scratch, out_dir, move=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _passthrough(kind, path, out_dir):
    """Copy an already-target-kind source into out_dir verbatim."""
    if kind == "god":
        # detect() returns the header file: <TitleID>/<content_dir>/<pkg>.
        # Copy its content_dir (header + <pkg>.data) verbatim under out_dir.
        src_cd = os.path.dirname(path)
        content_dir = os.path.basename(src_cd)
        dst_cd = os.path.join(out_dir, content_dir)
        if os.path.exists(dst_cd):
            shutil.rmtree(dst_cd)
        shutil.copytree(src_cd, dst_cd)
        return os.path.join(dst_cd, os.path.basename(path))
    # stfs: a single package file. Place it at <content_type>/<contentid>,
    # bytes untouched (it carries its own hashes + license/content-size).
    with open(path, "rb") as f:
        head = f.read(0xB000)
    ctype = "%08X" % struct.unpack_from(">I", head, 0x344)[0]
    contentid = head[0x32C:0x32C + 20].hex().upper()
    dst_dir = os.path.join(out_dir, ctype)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, contentid)
    shutil.copyfile(path, dst)
    return dst


def _lift_tree(god_root, out_dir, move):
    """Given a GoD tree rooted at <TitleID>/<content_dir>/<pkg>, place the
    content_dir directly under out_dir (no TitleID level) and return the
    header path. move=True relocates (built output); False copies (source)."""
    tids = [d for d in os.listdir(god_root)
            if os.path.isdir(os.path.join(god_root, d))]
    if len(tids) != 1:
        raise ConvertError("unexpected GoD tree under %s: %r" % (god_root, tids))
    tdir = os.path.join(god_root, tids[0])
    cdirs = [d for d in os.listdir(tdir)
             if os.path.isdir(os.path.join(tdir, d))]
    if len(cdirs) != 1:
        raise ConvertError("unexpected content dirs in GoD tree: %r" % cdirs)
    content_dir = cdirs[0]
    src_cd = os.path.join(tdir, content_dir)
    dst_cd = os.path.join(out_dir, content_dir)
    if os.path.exists(dst_cd):
        shutil.rmtree(dst_cd)
    if move:
        shutil.move(src_cd, dst_cd)
    else:
        shutil.copytree(src_cd, dst_cd)
    pkgs = [f for f in os.listdir(dst_cd) if not f.endswith(".data")]
    if len(pkgs) != 1:
        raise ConvertError("unexpected package files in %s: %r" % (dst_cd, pkgs))
    return os.path.join(dst_cd, pkgs[0])


# ------------------------------------------------------------------- probe

def probe(src):
    """Read a source's identity without converting it. Returns
    {"title_id": int, "kind": "god"|"stfs", "name": str|None}.

    `kind` is the native target the source would serve as: "stfs" for a
    content package (XBLA/DLC/TU), "god" for a disc game. Raises
    ConvertError if the identity cannot be read.
    """
    src = os.fspath(src)
    try:
        kind, path = _detect.detect(src)
    except Exception as e:
        raise ConvertError("cannot read source %s: %s" % (src, e)) from e

    if kind == "stfs":
        with open(path, "rb") as f:
            head = f.read(0xB000)
        tid = struct.unpack_from(">I", head, 0x360)[0]
        name = _stfs.read_title(path) or _titledb.name_for_title_id(tid)
        return {"title_id": tid, "kind": "stfs", "name": name}

    if kind == "god":
        hdr = _god.parse_header(_god_header_path(path))
        tid = hdr["title_id"]
        ctype = hdr.get("content_type")
        name = hdr.get("title") or _titledb.name_for_title_id(tid)
        return {"title_id": tid,
                "kind": "stfs" if ctype not in (0x7000, None) else "god",
                "name": name}

    # Image / archive / gamedir: read the executable's exec-info.
    from . import cli as _cli
    info = _title_info_of(kind, path, _cli)
    tid = info["title_id"]
    name = info.get("title") or _titledb.name_for_title_id(tid)
    return {"title_id": tid, "kind": "god", "name": name}


def _god_header_path(path):
    """The GoD header file given whatever detect() returned for a god
    source (the tree root, or the header file itself)."""
    if os.path.isfile(path):
        return path
    for tid in os.listdir(path):
        cdir = os.path.join(path, tid)
        if not os.path.isdir(cdir):
            continue
        for cd in os.listdir(cdir):
            inner = os.path.join(cdir, cd)
            if os.path.isdir(inner):
                for f in os.listdir(inner):
                    if not f.endswith(".data"):
                        return os.path.join(inner, f)
    raise ConvertError("no GoD header found under %s" % path)


def _title_info_of(kind, path, cli):
    """exec-info dict (with title_id, title) for an image/archive/gamedir."""
    if kind == "gamedir":
        xex = os.path.join(path, "default.xex")
        xbe = os.path.join(path, "default.xbe")
        if os.path.isfile(xex):
            with open(xex, "rb") as f:
                return _god._parse_xex(f, 0)
        if os.path.isfile(xbe):
            with open(xbe, "rb") as f:
                return _god._parse_xbe(f, 0)
        raise ConvertError("gamedir has no default.xex/.xbe: %s" % path)
    from .formats import xdvdfs as _xdvdfs_mod
    opener = cli._image_opener(kind, path)
    if opener is None:
        raise ConvertError("cannot probe source kind %r" % kind)
    with opener() as img:
        base = _xdvdfs_mod.find_base(img)
        _ctype, info = _god._title_info(img, base, _god._xdvdfs())
    return info
