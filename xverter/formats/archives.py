"""Archive (.zip / .7z) support: transparent input, plain output.

Games travel the internet in archives, so xverter accepts them
directly as INPUT: detect by magic bytes, extract to scratch, locate
the game payload (a supported game file, or a game directory tree) and
continue the pipeline with the payload's own kind. `Halo 3.7z -> .zar`
is one conversion.

As OUTPUT (.zip / .7z targets) the rule is deliberately boring: the
archive wraps the input exactly as it is - a file as a single entry, a
GoD/extracted tree as a directory tree. No format change is smuggled
in; convert first if you want the payload converted.

Support, per the no-required-dependencies rule:
  zip - Python stdlib, always available (zip64 handled; reads
        torrentzipped fine). Output is canonicalized - sorted entry
        order, fixed timestamps - so identical input trees produce
        identical archives.
  7z  - the official 7-Zip engine, multithreaded: built into the
        standalone binaries; pip installs use a system 7-Zip
        (7zz/7z/7za). Invoked as a separate program (aggregation).
RAR is deliberately unsupported: there is no free native
implementation, and xverter does not take binary dependencies for it.

Output verification: zip contents are re-read from the finished
archive and stream-hashed against the source (full round-trip). 7z
gets the engine's whole-archive test (`7z t`: every member decoded and
CRC-checked).
"""

import hashlib
import ntpath
import os
import sys
import contextlib
import zipfile
import zlib

MAGIC_ZIP = b"PK\x03\x04"
MAGIC_7Z = b"7z\xbc\xaf\x27\x1c"

GAME_EXTS = (".iso", ".zar", ".cci", ".cso", ".chd", ".god",
             ".xex", ".xbe")
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_CHUNK = 1 << 20


class ArchiveError(Exception):
    pass


def _seven_zip():
    """Path to the 7-Zip engine: the copy bundled inside standalone
    binaries first, then a system install (7zz = official, 7z/7za =
    p7zip). Invoked as a subprocess - shipped alongside, never
    linked."""
    if getattr(sys, "frozen", False):
        # standalone binaries carry the official multithreaded engine
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        for name in ("7zz", "7zr.exe"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    # platform wheels ship the engine inside the package (xverter/bin)
    pkg_bin = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin")
    for name in ("7zz", "7zr.exe"):
        p = os.path.join(pkg_bin, name)
        if os.path.isfile(p):
            if os.name != "nt" and not os.access(p, os.X_OK):
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
            return p
    import shutil as _shutil
    for name in ("7zz", "7z", "7za"):
        exe = _shutil.which(name)
        if exe:
            return exe
    if os.name == "nt":
        for p in (os.path.expandvars(r"%ProgramFiles%\7-Zip\7z.exe"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\7-Zip\7z.exe")):
            if os.path.isfile(p):
                return p
    return None


def _need_7z():
    exe = _seven_zip()
    if exe is None:
        raise ArchiveError(
            "7z support needs the 7-Zip engine: the standalone xverter "
            "binaries include it; for pip installs, install 7-Zip "
            "(Linux: p7zip / 7zip package; macOS: brew install sevenzip; "
            "Windows: 7-zip.org)")
    return exe


def _run_7z(argv, progress=None, total=1, cwd=None):
    """Run 7-Zip with -bsp1 percent output parsed into progress()."""
    import re as _re
    import subprocess as _sp
    p = _sp.Popen(argv, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                  bufsize=1, cwd=cwd)
    tail = []
    for raw in iter(p.stdout.readline, ""):
        for line in raw.replace("\r", "\n").splitlines():
            line = line.strip()
            if not line:
                continue
            m = _re.match(r"(\d+)%", line)
            if m and progress:
                progress(int(m.group(1)) * total // 100, total)
                continue
            tail = (tail + [line])[-4:]
    p.stdout.close()
    if p.wait() != 0:
        raise ArchiveError("7-Zip failed: %s" % (tail[-1:] or ["?"]))


def sniff(path):
    """'zip' | '7z' | None by magic bytes."""
    try:
        with open(path, "rb") as f:
            head = f.read(6)
    except OSError:
        return None
    if head[:4] == MAGIC_ZIP:
        return "zip"
    if head == MAGIC_7Z:
        return "7z"
    return None


def _safe(name):
    parts = name.replace("\\", "/").split("/")
    # A Windows drive spec ("C:\...", or drive-relative "C:evil") makes
    # os.path.join on Windows discard out_dir entirely and write to an
    # absolute location - so it must be refused alongside leading-slash
    # absolutes and ".." escapes. ntpath answers this the same on every
    # platform, so the check protects the archives we make on Linux for
    # a user who later unpacks them on Windows too.
    if (name.startswith(("/", "\\")) or ".." in parts or "" == parts[0]
            or ntpath.splitdrive(name)[0] or ntpath.isabs(name)):
        raise ArchiveError("refusing unsafe archive path %r" % name)
    return name


def _open_zip(path):
    """zipfile.ZipFile, but a structurally broken archive that merely
    starts with the ZIP magic is a bad input, not a crash."""
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ArchiveError("damaged zip archive: %s (%s)" % (path, e))


def _zip_damage(path, exc):
    """Translate mid-read corruption into the same clean refusal a bad
    open gets. _open_zip only covers the central directory; a flipped
    bit inside a member's deflate stream or CRC surfaces later, as
    zlib.error or BadZipFile, from any read."""
    return ArchiveError("damaged zip archive: %s (%s)" % (path, exc))


def list_entries(path):
    """[(name, size)] for the archive's files (no extraction)."""
    kind = sniff(path)
    if kind == "zip":
        try:
            with _open_zip(path) as z:
                return [(i.filename, i.file_size) for i in z.infolist()
                        if not i.is_dir()]
        except (zipfile.BadZipFile, zlib.error) as e:
            raise _zip_damage(path, e)
    if kind == "7z":
        return _list_7z(path)
    raise ArchiveError("not a zip/7z archive: %s" % path)


def _list_7z(path):
    """[(name, size)] for a .7z via `7z l -slt` structured output."""
    import subprocess as _sp
    exe = _need_7z()
    r = _sp.run([exe, "l", "-slt", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise ArchiveError("7-Zip could not list %s: %s"
                           % (path, r.stdout.strip().splitlines()[-1:]))
    out = []
    block = {}
    in_entries = False
    for line in r.stdout.splitlines() + [""]:
        line = line.rstrip()
        if line == "----------":
            in_entries = True
            continue
        if not in_entries:
            continue
        if not line:
            if block.get("Path") and "D" not in block.get("Attributes", ""):
                try:
                    size = int(block.get("Size", "0") or "0")
                except ValueError:
                    size = 0
                out.append((block["Path"].replace("\\", "/"), size))
            block = {}
            continue
        if " = " in line:
            k, _sep, v = line.partition(" = ")
            block[k] = v
    return out


def extract(path, out_dir, progress=None):
    """Extract the whole archive into out_dir (paths sanitized),
    reporting decompressed bytes via progress(done, total)."""
    kind = sniff(path)
    os.makedirs(out_dir, exist_ok=True)
    if kind == "zip":
        try:
            with _open_zip(path) as z:
                infos = [i for i in z.infolist() if not i.is_dir()]
                for i in z.infolist():
                    _safe(i.filename)
                total = sum(i.file_size for i in infos) or 1
                done = 0
                for i in z.infolist():
                    dest = os.path.join(out_dir,
                                        i.filename.replace("/", os.sep))
                    if i.is_dir():
                        os.makedirs(dest, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(dest) or out_dir,
                                exist_ok=True)
                    with z.open(i) as f, open(dest, "wb") as o:
                        while True:
                            b = f.read(_CHUNK)
                            if not b:
                                break
                            o.write(b)
                            done += len(b)
                            if progress:
                                progress(done, total)
        except (zipfile.BadZipFile, zlib.error) as e:
            raise _zip_damage(path, e)
        return
    if kind == "7z":
        exe = _need_7z()
        entries = _list_7z(path)
        for name, _sz in entries:
            _safe(name)
        total = sum(sz for _n, sz in entries) or 1
        _run_7z([exe, "x", "-y", "-bsp1", "-o" + out_dir, path],
                progress=progress, total=total)
        if progress:
            progress(total, total)
        return
    raise ArchiveError("not a zip/7z archive: %s" % path)


def find_payload(root):
    """Locate the game inside an extracted archive tree: the largest
    file with a game extension (a default.xex/.xbe promotes its
    directory), else a directory that detects as a game container, else
    a lone large file. Absolute path; ArchiveError when nothing
    game-like is found."""
    from .. import detect as detect_mod

    candidates = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(GAME_EXTS):
                p = os.path.join(dirpath, fn)
                candidates.append((os.path.getsize(p), p))
    for _size, p in sorted(candidates, reverse=True):
        if os.path.basename(p).lower() in ("default.xex", "default.xbe"):
            return os.path.dirname(p)
        return p
    try:
        detect_mod.detect(root)
        return root
    except Exception:
        pass
    entries = os.listdir(root)
    if len(entries) == 1:
        only = os.path.join(root, entries[0])
        if os.path.isdir(only):
            try:
                detect_mod.detect(only)
                return only
            except Exception:
                pass
        elif os.path.getsize(only) > 1 << 20:
            return only
    raise ArchiveError(
        "no game payload found in the archive (looked for %s or a game "
        "directory tree)" % ", ".join(GAME_EXTS))


# ---------------------------------------------------------------- output

def _walk_sources(src):
    """[(absolute path, archive name)] for a file or directory input,
    sorted for deterministic entry order."""
    src = os.path.abspath(src)
    if os.path.isfile(src):
        return [(src, os.path.basename(src))]
    out = []
    base = os.path.basename(src.rstrip(os.sep)) or "game"
    for dirpath, dirnames, files in os.walk(src):
        dirnames.sort()
        rel = os.path.relpath(dirpath, src)
        for fn in sorted(files):
            arc = base if rel == "." else base + "/" + \
                rel.replace(os.sep, "/")
            out.append((os.path.join(dirpath, fn), arc + "/" + fn))
    if not out:
        raise ArchiveError("nothing to archive under %s" % src)
    return out


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


#: ISA-L's deflate level for ZIP output. Measured against zlib level 6
#: on both extremes of compressibility, same 7.8 GB image:
#:
#:   zero/video region   204 MB/s -> 4384 MB/s   26.34% vs 26.34%
#:   game data            76 MB/s -> 2121 MB/s   96.91% vs 96.89%
#:
#: 21-28x faster for the same bytes-out, which is not a trade so much as
#: zlib spending its time hunting matches that are not there. Level 3 is
#: both slower and slightly worse than 2, so 2 it is.
_ISAL_LEVEL = 2

#: 7-Zip compression level for .7z output - see build_7z.
_SEVENZ_LEVEL = 1


@contextlib.contextmanager
def _isal_zip():
    """Have zipfile deflate and inflate with ISA-L for the duration.

    Deflate only, and the trade is worth stating plainly. Measured on a
    7.8 GB image, same extraction code reading both archives:

      made by zlib 6    write 99.0s   read back  6.7s
      made by ISA-L 2   write 10.2s   read back 10.6s   +0.04% size

    Writing is 9.7x faster; reading what it wrote is 1.58x slower,
    because the fast encoder emits a stream that is cheaper to produce
    and slightly costlier to decode. For an archive format - written
    once, read rarely - that is the right end of the trade: you would
    have to re-read the same archive around twenty times before the
    write saving is spent.

    ISA-L's inflate is not used. It is faster in isolation (2506 ->
    3300 MB/s) but slower through zipfile, which reads via
    `decompress(data, max_length)` in bounded chunks. zlib keeps the
    read path.

    Nor is the bundled 7-Zip engine, which was measured for the same
    job and lost - decode only, no writing, two reps each:

      zlib-made zip    7-Zip 7.6s    zipfile 6.7s
      ISA-L-made zip   7-Zip 13.2s   zipfile 10.5s

    stdlib is already the fastest reader available here. Note both
    readers slow by the same factor on the ISA-L archive, so the read
    cost above is a property of the stream, not of Python.

    zipfile has no public hook for choosing a codec, so the private ones
    are swapped and put back. Every attribute and the import are
    checked: without any of them the block is a no-op and stdlib zlib
    does the work as before. What ISA-L emits is ordinary deflate -
    stdlib inflates it, and every entry is re-read and hashed by the
    verify pass either way."""
    try:
        from isal import isal_zlib
    except ImportError:
        yield False
        return
    o_comp = getattr(zipfile, "_get_compressor", None)
    if o_comp is None:                  # stdlib moved it: leave well alone
        yield False
        return

    def _comp(compress_type, compresslevel=None):
        if compress_type == zipfile.ZIP_DEFLATED:
            return isal_zlib.compressobj(_ISAL_LEVEL, isal_zlib.DEFLATED, -15)
        return o_comp(compress_type, compresslevel)

    zipfile._get_compressor = _comp
    try:
        yield True
    finally:
        zipfile._get_compressor = o_comp


def build_zip(src, out_path, verify=True, progress=None):
    """Archive a file or directory tree as .zip, deflating with ISA-L
    where it is available. Wraps _build_zip so the codec swap covers the
    write and the verify re-read alike."""
    with _isal_zip():
        return _build_zip(src, out_path, verify=verify, progress=progress)


def _build_zip(src, out_path, verify=True, progress=None):
    """Archive a file or directory tree as .zip. Canonical output:
    sorted entries, fixed timestamps - same input, same bytes. verify
    re-reads every entry from the finished archive and stream-hashes it
    against the source file."""
    sources = _walk_sources(src)
    if os.path.exists(out_path):
        raise ArchiveError("output already exists: %s" % out_path)
    _grand = sum(os.path.getsize(p) for p, _a in sources) or 1
    _done = [0]
    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True) as z:
            for path, arc in sources:
                info = zipfile.ZipInfo(arc, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(path, "rb") as f, \
                        z.open(info, "w", force_zip64=True) as o:
                    while True:
                        b = f.read(_CHUNK)
                        if not b:
                            break
                        o.write(b)
                        _done[0] += len(b)
                        if progress:
                            progress(_done[0], _grand)
        if verify:
            with zipfile.ZipFile(out_path) as z:
                for path, arc in sources:
                    h = hashlib.sha1()
                    with z.open(arc) as f:
                        while True:
                            b = f.read(_CHUNK)
                            if not b:
                                break
                            h.update(b)
                    if h.hexdigest() != _sha1(path):
                        raise ArchiveError(
                            "zip round-trip mismatch on %s" % arc)
    except BaseException:
        # BaseException, not Exception: a Ctrl-C or SIGTERM mid-write
        # must still delete the partial archive at the user's path.
        if os.path.exists(out_path):
            os.unlink(out_path)
        raise
    return out_path


def build_7z(src, out_path, verify=True, progress=None):
    """Archive a file or directory tree as .7z with the official 7-Zip
    engine (multithreaded, invoked as a separate program). verify runs
    the engine's whole-archive test (CRC of every member)."""
    exe = _need_7z()
    sources = _walk_sources(src)
    if os.path.exists(out_path):
        raise ArchiveError("output already exists: %s" % out_path)
    _grand = sum(os.path.getsize(p) for p, _a in sources) or 1
    try:
        src_abs = os.path.abspath(src)
        # -mx1 rather than the engine default of -mx5. Game images are
        # already compressed, so the higher levels spend a long time
        # finding matches that are not there. Measured on a 7.8 GB
        # redump: -mx1 20s/87.99%, -mx5 69s/87.86%, -mx9 82s/87.78% -
        # 3.45x the speed for 0.13% more bytes, which is 9 MB on a
        # 6.87 GB archive. Raise this constant if size matters more.
        _run_7z([exe, "a", "-t7z", "-y", "-bsp1", "-mx" + str(_SEVENZ_LEVEL),
                 os.path.abspath(out_path),
                 os.path.basename(src_abs)],
                progress=progress, total=_grand,
                cwd=os.path.dirname(src_abs) or ".")
        if progress:
            progress(_grand, _grand)
        if verify:
            _run_7z([exe, "t", "-bsp1", os.path.abspath(out_path)],
                    progress=progress, total=_grand)
    except BaseException:
        # BaseException, not Exception: a Ctrl-C or SIGTERM mid-write
        # must still delete the partial archive at the user's path.
        if os.path.exists(out_path):
            os.unlink(out_path)
        raise
    return out_path
