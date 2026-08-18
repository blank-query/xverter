"""xverter command-line interface.

  xverter info    <input>
  xverter verify  <input> [--deep] [--dat FILE.dat]
  xverter convert <input> -o <output.iso|output.zar|outdir/>

Any input format, any output format, through a common verified pivot
(the extracted game directory). STFS is read-only by design: repacked
LIVE/CON packages would be invalidly signed.
"""

import argparse
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import tempfile
import zlib

from . import detect as detect_mod
from .formats import god as god_mod
from .formats import xdvdfs as xdvdfs_mod
from .formats import zar as zar_mod
from .formats import stfs as stfs_mod
from .formats import builders as builders_mod
from .formats import cci as cci_mod
from .formats import cso as cso_mod
from .formats import chd as chd_mod
from .formats import archives as archives_mod
from . import datcache

__version__ = "1.0.3"


class CliError(Exception):
    pass


# ---------------------------------------------------------------- helpers

class _Progress:
    """Progress sink. mode 'lines' prints machine-readable
    'PROGRESS <stage> <done> <total>' lines (the TUI parses these);
    mode 'tty' redraws a percent in place; None is silent. Throttled to
    ~4 updates/second, always emitting the terminal 100%."""

    def __init__(self, mode):
        self.mode = mode
        self._last = 0.0

    def cb(self, stage):
        if self.mode is None:
            return None
        import time as _time

        def fn(done, total):
            now = _time.monotonic()
            if done < total and now - self._last < 0.25:
                return
            self._last = now
            if self.mode == "lines":
                sys.stderr.write("PROGRESS %s %d %d\n"
                                 % (stage, done, max(total, 1)))
            else:
                pct = 100 * done // max(total, 1)
                sys.stderr.write("\r%-10s %3d%%%s"
                                 % (stage, pct,
                                    "\n" if done >= total else ""))
            sys.stderr.flush()
        return fn


def _tempdir(base=None):
    """Scratch directory for pivot files. Honors --workdir, then the
    standard TMPDIR environment variable (point either at /dev/shm or
    another tmpfs to keep pivots entirely in RAM)."""
    if base:
        os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="xverter_", dir=base)


# Peak scratch usage of a pivot: extracted game dir + repacked ISO live
# simultaneously (~2x content), plus slack.
RAM_SCRATCH_FACTOR = 2.2


def _available_ram():
    """Bytes of memory actually available for new allocations, or None
    if unknown on this platform. Deliberately 'available', not total:
    cached/free, what a new tmpfs write can actually claim."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _ram_scratch_dir(input_size):
    """Resolve --scratch ram to a tmpfs path, with preflight checks."""
    candidates = ["/dev/shm", "/run/shm"]
    base = next((c for c in candidates if os.path.isdir(c)), None)
    if base is None:
        raise CliError(
            "no RAM-backed filesystem on this platform (looked for %s). "
            "On Windows/macOS, create a RAM drive (e.g. ImDisk) and pass "
            "it via --workdir instead." % ", ".join(candidates))
    need = int(input_size * RAM_SCRATCH_FACTOR)
    avail = _available_ram()
    if avail is not None and avail < need:
        print("WARNING: RAM scratch wants %.1f GiB available "
              "(%.1fx input), but only %.1f GiB is available - the "
              "conversion may fail with 'no space left on device'. "
              "AVAILABLE ram is what counts, not total."
              % (need / 2**30, RAM_SCRATCH_FACTOR, avail / 2**30),
              file=sys.stderr)
    try:
        free = shutil.disk_usage(base).free
        if free < need:
            print("WARNING: %s has only %.1f GiB free (tmpfs is usually "
                  "capped at half of total RAM) - the conversion may fail."
                  % (base, free / 2**30), file=sys.stderr)
    except OSError:
        pass
    return base


def _to_gamedir(kind, path, workdir, manifest=None, progress=None):
    """Read any supported input into an extracted game directory (the pivot).
    If manifest is a dict, per-file SHA-1s are captured inline where the
    read path allows it (GoD/ISO/STFS)."""
    if kind == "gamedir":
        return path
    out = os.path.join(workdir, "gamedir")
    os.makedirs(out, exist_ok=True)
    if kind == "god":
        # extract straight from the hash-verifying container view -
        # no temporary ISO is written
        with god_mod.GodStream(path) as stream:
            xdvdfs_mod.extract(stream, out, quiet=True, manifest=manifest,
                               progress=progress)
    elif kind == "iso":
        xdvdfs_mod.extract(path, out, quiet=True, manifest=manifest,
                           progress=progress)
    elif kind == "zar":
        zar_mod.unpack(path, out, manifest=manifest, progress=progress)
    elif kind == "stfs":
        stfs_mod.extract(path, out, manifest=manifest,
                         progress=progress)
    elif kind == "cci":
        with cci_mod.CciReader(path) as stream:
            xdvdfs_mod.extract(stream, out, quiet=True, manifest=manifest,
                               progress=progress)
    elif kind == "cso":
        with cso_mod.CsoReader(path) as stream:
            xdvdfs_mod.extract(stream, out, quiet=True, manifest=manifest,
                               progress=progress)
    else:
        raise CliError("cannot read input kind %r" % kind)
    return out


def _output_kind(out_path):
    # A trailing separator of either flavor means "extract to directory"
    # (Windows callers hand us backslashes).
    if out_path.endswith(("/", "\\")) or os.path.isdir(out_path):
        return "gamedir"
    ext = os.path.splitext(out_path)[1].lower()
    if ext == ".zar":
        return "zar"
    if ext == ".iso":
        return "iso"
    if ext == ".god":
        return "god"
    if ext == ".chd":
        return "chd"
    if ext == ".zip":
        return "zip"
    if ext == ".7z":
        return "7z"
    if ext == ".cci":
        return "cci"
    if ext == ".cso":
        return "cso"
    raise CliError("cannot infer output format from %r "
                   "(use .zar/.iso extension or a directory path)" % out_path)


def _stream_hashes(path, progress=None):
    # CRC32 and SHA-1 each run on their own thread (both release the
    # GIL), so wall time is max(read, crc, sha) instead of their sum
    sha = hashlib.sha1()
    state = {"crc": 0}
    lanes = [queue.Queue(maxsize=8), queue.Queue(maxsize=8)]

    def worker(lane, update):
        while True:
            b = lane.get()
            if b is None:
                return
            update(b)

    def crc_update(b):
        state["crc"] = zlib.crc32(b, state["crc"])

    threads = [
        threading.Thread(target=worker, args=(lanes[0], sha.update),
                         daemon=True),
        threading.Thread(target=worker, args=(lanes[1], crc_update),
                         daemon=True),
    ]
    for t in threads:
        t.start()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            for lane in lanes:
                lane.put(b)
            done += len(b)
            if progress:
                progress(done, total)
    for lane in lanes:
        lane.put(None)
    for t in threads:
        t.join()
    return "%08x" % (state["crc"] & 0xFFFFFFFF), sha.hexdigest()


def _dat_has_size(dat_path, size):
    """Cheap prefilter: does any entry in this DAT have exactly this size?

    Redump images have canonical sizes - about 111 distinct values across
    both DATs, 2627 of the 2690 OG-Xbox entries sharing one - so a
    trimmed or modified image can be ruled out by size alone, without
    hashing a single byte. That is what makes the identity check
    affordable enough to run by default."""
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        import xml.etree.ElementTree as ET
    want = str(size)
    for rom in ET.parse(dat_path).getroot().iter("rom"):
        if rom.get("size") == want:
            return True
    return False


def _executable_check(path):
    """Does the image's default.xex / default.xbe actually parse?

    Structure and content are separate claims. An image can be perfectly
    valid XDVDFS - tables coherent, extent within the file, every byte
    readable - while the one file that makes it a game is destroyed. GoD
    conversion already refuses such an image because it must read the
    title id out of it; the block wrappers never look, and would
    otherwise write it out and call the result verified.

    Returns None if the executable parses, else a reason."""
    try:
        with open(path, "rb") as f:
            base = xdvdfs_mod.find_base(f)
            god_mod._title_info(f, base, god_mod._xdvdfs())
    except Exception as e:                             # noqa: BLE001
        return str(e)
    return None


def _identity_path():
    return os.path.join(datcache.cache_dir(), "identity.json")


def _identity_key(path, st):
    """Identity of the bytes, not the name: device, inode, size and
    nanosecond mtime. Any edit moves at least one of them."""
    return "%d:%d:%d:%d" % (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _identity_cache_get(path, st):
    """Hashes previously computed for exactly these bytes, or None.

    Converting one game to several formats re-reads the same source each
    time, and hashing 7 GB is 3 seconds of pure repetition - a full
    conversion-matrix run paid it a dozen times over. The file is a
    convenience only: a miss just means hashing again."""
    try:
        with open(_identity_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        ent = data[_identity_key(path, st)]
        return ent["crc"], ent["sha1"]
    except Exception:                                 # noqa: BLE001
        return None


def _identity_cache_put(path, st, crc, sha1):
    try:
        try:
            with open(_identity_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:                             # noqa: BLE001
            data = {}
        data[_identity_key(path, st)] = {
            "crc": crc, "sha1": sha1, "name": os.path.basename(path)}
        if len(data) > 256:                # keep it small; order is insertion
            for k in list(data)[:len(data) - 256]:
                del data[k]
        tmp = _identity_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _identity_path())
    except Exception:                                 # noqa: BLE001
        pass                               # a cache that cannot write is fine


def _redump_check(path, progress=None):
    """Identify an ISO against the redump DATs.

    Returns (status, detail) where status is:
      match     - the image is a known-good dump, byte for byte
      altered   - its size is a canonical redump size but the content
                  differs: a modified, patched or damaged redump
      skipped   - no DAT entry has this size, so it cannot be a redump
                  (a trimmed image, for instance). Nothing was hashed.
    """
    st = os.stat(path)
    size = st.st_size
    sources = []
    for system, label in (("xbox360", "Xbox 360"), ("xbox", "Original Xbox")):
        for src in (datcache.cached(system)[0], datcache.bundled(system)):
            if src:
                sources.append((src, label))
    plausible = [(s, l) for s, l in sources if _dat_has_size(s, size)]
    if not plausible:
        return ("skipped", "%d bytes matches no known dump size" % size)
    hashes = _identity_cache_get(path, st)
    if hashes is None:
        crc, sha1 = _stream_hashes(path, progress=progress)
        _identity_cache_put(path, st, crc, sha1)
    else:
        crc, sha1 = hashes
    for src, label in plausible:
        name = _dat_lookup(src, size, crc, sha1)
        if name:
            return ("match", "%s [%s]" % (name, label))
    return ("altered", "size matches a redump image but the content does not "
                       "(crc %s) - modified, patched or damaged" % crc)


def _dat_lookup(dat_path, size, crc, sha1):
    # Prefer defusedxml (XXE/entity-expansion hardening) when available;
    # stdlib expat (Python >= 3.11 / libexpat >= 2.4) has built-in
    # amplification protection as the fallback.
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        import xml.etree.ElementTree as ET
    root = ET.parse(dat_path).getroot()
    for rom in root.iter("rom"):
        if rom.get("size") == str(size) and \
           (rom.get("crc", "").lower() == crc or
                rom.get("sha1", "").lower() == sha1):
            return rom.get("name")
    return None


def _wrapper_reader(kind, path):
    return (cci_mod.CciReader if kind == "cci" else cso_mod.CsoReader)(path)


def _sha1_file(path, limit=None):
    h = hashlib.sha1()
    remaining = limit
    with open(path, "rb") as f:
        while True:
            n = 1 << 22 if remaining is None else min(1 << 22, remaining)
            if n == 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if remaining is not None:
                remaining -= len(b)
    return h.hexdigest()


def _verify_chd_output(chd_path, src_iso, progress=None):
    """Round-trip a built CHD: the decompressed stream must equal the
    source ISO byte-for-byte, with only zero padding beyond it (chdman
    pads the last hunk)."""
    hdr = chd_mod.read_header(chd_path)
    src_size = os.path.getsize(src_iso)
    if hdr["logical_bytes"] < src_size:
        os.unlink(chd_path)
        raise CliError("built chd holds %d bytes < source %d"
                       % (hdr["logical_bytes"], src_size))
    with tempfile.TemporaryDirectory(prefix="xverter_chdchk_") as cw:
        back = os.path.join(cw, "back.iso")
        chd_mod.extract(chd_path, back, progress=progress)
        got = _sha1_file(back, limit=src_size)
        want = _sha1_file(src_iso)
        pad_ok = True
        with open(back, "rb") as f:
            f.seek(src_size)
            while pad_ok:
                b = f.read(1 << 20)
                if not b:
                    break
                pad_ok = b.count(0) == len(b)
    if got != want or not pad_ok:
        os.unlink(chd_path)
        raise CliError("built chd failed round-trip (sha1 %s != %s%s)"
                       % (got, want, "" if pad_ok else ", nonzero padding"))


def _verify_wrapper(out_kind, out_path, in_kind, in_path, w,
                    progress=None):
    """Round-trip a built CCI/CSO: decoded stream sha1 must equal source
    ISO stream sha1."""
    import hashlib

    def stream_sha1(f):
        h = hashlib.sha1()
        f.seek(0)
        size = getattr(f, "size", None)
        done = 0
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress and size:
                progress(done, size)
        return h.hexdigest()

    with _wrapper_reader(out_kind, out_path) as got_f:
        got = stream_sha1(got_f)
    if in_kind == "iso":
        with open(in_path, "rb") as f:
            from .formats.cci import xbox_image_offset
            off = xbox_image_offset(f)
            if off:
                import hashlib as _h
                h = _h.sha1()
                f.seek(off)
                while True:
                    b = f.read(1 << 22)
                    if not b:
                        break
                    h.update(b)
                want = h.hexdigest()
            else:
                want = stream_sha1(f)
    elif in_kind == "god":
        with god_mod.GodStream(in_path) as f:
            want = stream_sha1(f)
    elif in_kind in ("cci", "cso"):
        with _wrapper_reader(in_kind, in_path) as f:
            want = stream_sha1(f)
    else:
        iso = os.path.join(w, "pivot_out.iso")
        with open(iso, "rb") as f:
            want = stream_sha1(f)
    if got != want:
        os.unlink(out_path)
        raise CliError("built %s failed round-trip: decoded sha1 %s != source %s"
                       % (out_kind, got, want))


# ---------------------------------------------------------------- commands

def cmd_info(args):
    kind, path = detect_mod.detect(args.input)
    print("format : %s" % kind)
    print("path   : %s" % path)
    if getattr(args, "identify", False):
        if kind == "iso":
            prog = _Progress("lines" if getattr(args, "progress", False)
                             else None)
            crc, sha1 = _stream_hashes(path, progress=prog.cb("hash"))
            size = os.path.getsize(path)
            hit = None
            for system, label in (("xbox360", "Xbox 360"),
                                  ("xbox", "Original Xbox")):
                for src in (datcache.cached(system)[0],
                            datcache.bundled(system)):
                    if src:
                        name = _dat_lookup(src, size, crc, sha1)
                        if name:
                            hit = (name, label)
                            break
                if hit:
                    break
            if hit:
                print("redump : authenticated - %s [%s]" % hit)
            else:
                print("redump : not authenticated "
                      "(no DAT entry matches this image)")
        else:
            print("redump : identity is only defined for redump ISOs "
                  "(this is a %s container)" % kind)
    if kind == "god":
        h = god_mod.parse_header(path)
        print("title  : %s" % (h["title"] or "(none)"))
        print("ids    : title=%08X media=%08X disc %d/%d"
              % (h["title_id"], h["media_id"], h["disc_number"], h["disc_count"]))
        print("header : %s, hash %s"
              % (h["magic"], "OK" if h["header_hash_ok"] else "MISMATCH"))
        print("parts  : %d (%d bytes)" % (h["part_count"], h["parts_total_size"]))
    elif kind == "iso":
        with open(path, "rb") as f:
            base = xdvdfs_mod.find_base(f)
        structure = {0x0: "bare game partition",
                     0x2080000: "XGD3 full disc image",
                     0xFD90000: "XGD2 full disc image",
                     0x18300000: "XGD1 full disc image (Original Xbox)"}[base]
        print("disc   : %s (partition base 0x%X)" % (structure, base))
        names = xdvdfs_mod.list_root(path)
        lower = [n.lower() for n in names]
        system = ("Xbox 360" if "default.xex" in lower
                  else "Original Xbox" if "default.xbe" in lower else "?")
        print("system : %s" % system)
        print("root   : %d entries%s" % (
            len(names),
            ", default.xex present" if "default.xex" in lower
            else ", default.xbe present" if "default.xbe" in lower
            else ", NO default executable"))
    elif kind == "zar":
        print("size   : %d bytes" % os.path.getsize(path))
        print("footer : ZArchive magic OK")
        try:
            files = zar_mod.list_files(path)
        except Exception:
            files = None
        if files is not None:
            print("files  : %d, %d bytes content"
                  % (len(files), sum(sz for _, sz in files)))
    elif kind == "gamedir":
        n = sum(len(fs) for _, _, fs in os.walk(path))
        print("files  : %d" % n)
    elif kind in ("cci", "cso"):
        with _wrapper_reader(kind, path) as f:
            print("size   : %d bytes decoded stream" % f.size)
            names = xdvdfs_mod.list_root(f)
            print("inner  : XDVDFS, %d root entries%s" % (
                len(names),
                " (default.xex present)" if "default.xex" in
                [n.lower() for n in names] else
                " (default.xbe present)" if "default.xbe" in
                [n.lower() for n in names] else ""))
    elif kind == "chd":
        h = chd_mod.read_header(path)
        print("chd    : v%d, %s" % (h["version"], "+".join(h["compressors"])))
        print("size   : %d bytes decompressed (%d-byte hunks)"
              % (h["logical_bytes"], h["hunk_bytes"]))
        print("sha1   : %s (raw data)" % h["raw_sha1"])
        if h["parent_sha1"]:
            print("parent : %s (delta chd - xverter needs standalone)"
                  % h["parent_sha1"])
    elif kind in ("zip", "7z"):
        entries = archives_mod.list_entries(path)
        print("entries: %d (%d bytes uncompressed)"
              % (len(entries), sum(sz for _n, sz in entries)))
        for n, sz in sorted(entries, key=lambda e: -e[1])[:5]:
            print("  %12d  %s" % (sz, n))
        if len(entries) > 5:
            print("  ... and %d more" % (len(entries) - 5))
    elif kind == "stfs":
        with open(path, "rb") as f:
            print("magic  : %s (content package - XBLA/DLC/TU)"
                  % f.read(4).decode("ascii", "replace").strip())
        title = stfs_mod.read_title(path)
        if title:
            print("title  : %s" % title)
        entries = stfs_mod.list_entries(path)
        print("entries: %d (%d files)"
              % (len(entries), sum(1 for e in entries if not e["is_dir"])))
    return 0


def cmd_verify(args):
    prog = _Progress("lines" if getattr(args, "progress", False)
                     else "tty" if sys.stderr.isatty() else None)
    kind, path = detect_mod.detect(args.input)
    print("format: %s" % kind)
    if kind == "god":
        god_mod.convert(path, None, verify_only=True,
                        progress=prog.cb("verify"))
    elif kind == "iso":
        if args.deep:
            w = _tempdir()
            try:
                xdvdfs_mod.extract(path, os.path.join(w, "x"), quiet=True,
                                   progress=prog.cb("deep-read"))
                print("verified: full extraction OK - every file read and hashed")
            finally:
                shutil.rmtree(w, ignore_errors=True)
        else:
            names = xdvdfs_mod.list_root(path)
            print("valid: %d root entries (use --deep to read every file)"
                  % len(names))
        with open(path, "rb") as _f:
            _base = xdvdfs_mod.find_base(_f)
        if _base == 0:
            # Bare partitions cannot match redump by definition - redump
            # catalogs full discs. Not applicable is not a failure.
            print("authentication not applicable: bare game partition "
                  "(redump catalogs full disc images only)")
        elif args.dat or not args.no_lookup:
            crc, sha1 = _stream_hashes(path, progress=prog.cb("hash"))
            size = os.path.getsize(path)
            if args.dat:
                name = _dat_lookup(args.dat, size, crc, sha1)
                if name:
                    print("authenticated: %s (crc=%s sha1=%s)" % (name, crc, sha1))
                else:
                    print("NOT authenticated: no DAT entry matches "
                          "(size=%d crc=%s sha1=%s)" % (size, crc, sha1))
                    return 1
            else:
                hit = None
                oldest = None
                for system in ("xbox360", "xbox"):
                    sources = []
                    cpath, age = datcache.cached(system)
                    if cpath:
                        oldest = age if oldest is None else max(oldest, age)
                        sources.append(("cached DAT", cpath))
                    bpath = datcache.bundled(system)
                    if bpath:
                        sources.append(("bundled DAT", bpath))
                    for label, spath in sources:
                        hit = _dat_lookup(spath, size, crc, sha1)
                        if hit:
                            # a match is a match - age is irrelevant on a hit
                            print("authenticated (%s): %s" % (label, hit))
                            break
                    if hit:
                        break
                if not hit:
                    # The DATs ship with the tool and `xverter dat
                    # update` refreshes them on demand, so a local miss
                    # is the whole verdict: nothing is asked of the
                    # network behind the user's back.
                    print("NOT authenticated: no DAT entry matches "
                          "(sha1=%s crc=%s)" % (sha1, crc))
                    print("       either the image is modified, or this "
                          "dump is potentially unknown to the community "
                          "- if the disc is genuine and unmodified, "
                          "please upload it to redump: "
                          "https://forum.redump.info")
                    if oldest is not None and oldest > datcache.MAX_AGE_DAYS:
                        print("       (your cached DAT is %d days old - "
                              "`xverter dat update` first if you have not "
                              "lately)" % oldest)
                    return 1
    elif kind == "zar":
        n = None
        try:
            n = zar_mod.verify_native(path, progress=prog.cb("verify"))
        except zar_mod.ZarError:
            raise
        if n is not None:
            print("zar verify: embedded SHA-256 OK over all bytes "
                  "(%d files, nothing written)" % n)
        else:
            w = _tempdir()
            try:
                zar_mod.unpack(path, w)
                print("zar verify: full decompression OK (reference tool)")
            finally:
                shutil.rmtree(w, ignore_errors=True)
    elif kind in ("cci", "cso"):
        import hashlib
        h = hashlib.sha1()
        n = 0
        with _wrapper_reader(kind, path) as f:
            _vcb = prog.cb("verify")
            while True:
                b = f.read(1 << 22)
                if not b:
                    break
                h.update(b)
                n += len(b)
                if _vcb:
                    _vcb(n, f.size)
        print("%s verify: all blocks decoded OK (%d bytes, stream sha1 %s)"
              % (kind, n, h.hexdigest()))
    elif kind == "chd":
        h = chd_mod.verify(path, progress=prog.cb("verify"))
        print("chd verify: chdman decompressed everything and matched the "
              "internal SHA-1s (%d bytes, raw sha1 %s)"
              % (h["logical_bytes"], h["raw_sha1"]))
    elif kind == "stfs":
        st = stfs_mod.verify_chains(path, progress=prog.cb("verify"))
        print("stfs verify: complete internal hash chain OK "
              "(%d blocks, %d L0 tables, %d level(s)%s)"
              % (st["blocks"], st["l0_tables"], st["levels"],
                 ", doubled tables" if st["doubled_tables"] else ""))
    elif kind in ("zip", "7z"):
        w = _tempdir()
        try:
            archives_mod.extract(path, os.path.join(w, "a"),
                                 progress=prog.cb("unpack"))
            payload = archives_mod.find_payload(os.path.join(w, "a"))
            pkind, ppath = detect_mod.detect(payload)
            print("archive extracts OK; payload: %s (%s) - verifying it"
                  % (os.path.basename(ppath.rstrip(os.sep)), pkind))
            sub = argparse.Namespace(input=ppath, deep=args.deep,
                                     dat=args.dat,
                                     no_lookup=args.no_lookup,
                                     progress=getattr(args, "progress",
                                                      False))
            return cmd_verify(sub)
        finally:
            shutil.rmtree(w, ignore_errors=True)
    elif kind == "gamedir":
        print("nothing to verify for an extracted directory")
    else:
        raise CliError("verify not supported yet for %r" % kind)
    return 0


def cmd_test(args):
    """Run the full conversion-matrix validation against one game."""
    from . import matrix
    auto = args.workdir is None
    w = args.workdir or tempfile.mkdtemp(prefix="xverter_matrix_",
                                         dir=os.path.dirname(
                                             os.path.abspath(args.input))
                                         or None)
    keep = False
    try:
        margv = [args.input, w]
        if getattr(args, "no_verify", False):
            margv.append("--leeroy-jenkins")
        rc = matrix.main(margv)
        report = os.path.join(w, "matrix_report.html")
        if auto:
            if not os.path.isfile(report):
                # Promised and not delivered: say so rather than fall through.
                print("WARNING: the matrix finished but wrote no report "
                      "(expected %s)" % report, file=sys.stderr)
            else:
                stem = os.path.splitext(os.path.basename(args.input))[0]
                dest = os.path.join(
                    os.path.dirname(os.path.abspath(args.input)),
                    stem + "_matrix_report.html")
                try:
                    shutil.move(report, dest)
                    print("report saved: %s" % dest)
                except OSError as e:
                    # Losing the report to the cleanup below would be worse
                    # than leaving the scratch dir behind. Keep both.
                    keep = True
                    print("WARNING: could not move the report to %s (%s)\n"
                          "         it is still at %s - the scratch "
                          "directory has been left in place for it"
                          % (dest, e, report), file=sys.stderr)
        return rc
    finally:
        if auto and not keep and os.path.isdir(w):
            try:
                shutil.rmtree(w)
            except OSError as e:
                left = sum(len(fs) for _r, _d, fs in os.walk(w))
                print("WARNING: could not remove the scratch directory "
                      "(%s)\n         %d file(s) left in %s - remove it "
                      "yourself" % (e, left, w), file=sys.stderr)


def cmd_tui(args):
    try:
        from . import tui
    except ImportError:
        raise CliError("the TUI needs the textual package (installed "
                       "with xverter; pyz users: pip install textual)")
    tui.main(args.library)
    return 0


def cmd_dat(args):
    if args.action == "update":
        have = datcache.active_version(args.system)
        path, version, installed = datcache.update(args.system)
        if not installed:
            print("%s DAT already current: redump has %s, you have %s - "
                  "nothing written" % (args.system, version, have))
            return 0
        print("cached %s DAT version %s -> %s" % (args.system, version, path))
        return 0
    for system in sorted(datcache.SYSTEMS):
        cpath, age = datcache.cached(system)
        bpath = datcache.bundled(system)
        cache_s = "%s (%.0f days old)" % (cpath, age) if cpath else "not cached"
        print("%-8s: cache %s | bundled %s"
              % (system, cache_s, "yes" if bpath else "no"))
    return 0


def cmd_convert(args):
    kind, path = detect_mod.detect(args.input)
    out_kind = _output_kind(args.output)
    if out_kind == kind and kind != "gamedir":
        raise CliError("input is already %s" % kind)
    scratch_base = getattr(args, "workdir", None)
    if scratch_base is None and getattr(args, "scratch", "disk") == "ram":
        try:
            in_size = (os.path.getsize(path) if os.path.isfile(path)
                       else sum(os.path.getsize(os.path.join(r, f))
                                for r, _d, fs in os.walk(path) for f in fs))
        except OSError:
            in_size = 0
        scratch_base = _ram_scratch_dir(in_size)
    prog = _Progress("lines" if getattr(args, "progress", False)
                     else "tty" if sys.stderr.isatty() else None)
    w = _tempdir(scratch_base)
    try:
        if kind in ("zip", "7z"):
            # Transparent input layer: extract, find the game inside,
            # continue as that kind.
            arc_dir = os.path.join(w, "archive_in")
            archives_mod.extract(path, arc_dir,
                                 progress=prog.cb("unpack"))
            payload = archives_mod.find_payload(arc_dir)
            kind, path = detect_mod.detect(payload)
            print("archive payload: %s (%s)"
                  % (os.path.basename(path.rstrip(os.sep)), kind))
        if kind == "iso":
            # Validate before writing anything. A raw image carries no
            # internal checksum, and the block wrappers are content-
            # agnostic, so without this a truncated source converts
            # "successfully" and is reported as verified.
            try:
                xdvdfs_mod.validate_image(path)
            except xdvdfs_mod.XdvdfsError as e:
                raise CliError("source is INVALID: %s" % e)
            if not args.no_verify:
                # Three separate claims, never merged into one word:
                #   valid         - the image's own structures cohere
                #   verified      - what came out matches what went in
                #   authenticated - it is the genuine retail disc
                exe = _executable_check(path)
                if exe is None:
                    print("source : valid")
                else:
                    print("source : valid, but the executable is "
                          "unparseable (%s)\n"
                          "         this image will not boot - converting "
                          "it anyway, as asked" % exe)
                # Authentication is free unless the size is a canonical
                # redump size, so trimmed images cost nothing.
                status, detail = _redump_check(
                    path, progress=prog.cb("identify"))
                if status == "match":
                    print("source : authenticated - redump: %s" % detail)
                elif status == "altered":
                    print("source : NOT authenticated - %s" % detail)
                else:
                    print("source : not authenticated - %s" % detail)
        if out_kind in ("zip", "7z"):
            # Boring on purpose: the archive wraps the input as-is.
            build = (archives_mod.build_zip if out_kind == "zip"
                     else archives_mod.build_7z)
            build(path, args.output, verify=not args.no_verify,
                  progress=prog.cb(out_kind + "-write"))
            print("wrote %s (%s)"
                  % (args.output,
                     "NO GUARANTEES - --leeroy-jenkins" if args.no_verify
                     else "round-trip verified" if out_kind == "zip"
                     else "CRC verified"))
            return 0
        if kind == "chd":
            # CHD is a transparent decompression layer over an ISO:
            # materialize the wrapped image (verified against the CHD
            # header's internal data SHA-1), then continue as iso input.
            chd_iso = (args.output if out_kind == "iso"
                       else os.path.join(w, "chd_in.iso"))
            chd_mod.extract(path, chd_iso, progress=prog.cb("chd-read"))
            if not args.no_verify:
                want = chd_mod.read_header(path)["raw_sha1"]
                got = _sha1_file(chd_iso)
                if got != want:
                    os.unlink(chd_iso)
                    raise CliError("chd extraction sha1 %s != header raw "
                                   "sha1 %s" % (got, want))
            if out_kind == "iso":
                print("wrote %s (%s)"
                      % (args.output,
                         "NO GUARANTEES - --leeroy-jenkins" if args.no_verify
                         else "verified against chd internal sha1"))
                return 0
            kind, path = "iso", chd_iso
        if out_kind == "iso" and kind == "god":
            # direct verified path, no pivot needed
            god_mod.convert(path, args.output, progress=prog.cb("god-read"))
            print("wrote %s" % args.output)
            return 0
        if out_kind == "god" and kind == "iso":
            hdr = builders_mod.build_god(path, args.output,
                                         verify=not args.no_verify,
                                         progress=prog.cb("god-write"),
                                         verify_progress=prog.cb("verify"))
            print("wrote GoD container (header: %s) (%s)"
                  % (hdr, "NO GUARANTEES - --leeroy-jenkins"
                     if args.no_verify else "hash tree verified"))
            return 0
        if out_kind == "chd":
            if kind == "iso":
                src_iso = path
            elif kind == "god":
                src_iso = os.path.join(w, "pivot_out.iso")
                god_mod.convert(path, src_iso, progress=prog.cb("god-read"))
            elif kind in ("cci", "cso"):
                src_iso = os.path.join(w, "pivot_out.iso")
                with _wrapper_reader(kind, path) as r, \
                        open(src_iso, "wb") as out:
                    while True:
                        chunk = r.read(1 << 22)
                        if not chunk:
                            break
                        out.write(chunk)
            else:
                manifest = {}
                gamedir = _to_gamedir(kind, path, w, manifest=manifest,
                      progress=prog.cb("extract"))
                src_iso = os.path.join(w, "pivot_out.iso")
                builders_mod.build_iso(gamedir, src_iso,
                                       verify=not args.no_verify,
                                       manifest=manifest or None,
                                       progress=prog.cb("iso-write"))
            chd_mod.build(src_iso, args.output,
                          progress=prog.cb("chd-write"))
            if not args.no_verify:
                _verify_chd_output(args.output, src_iso,
                                   progress=prog.cb("verify"))
            print("wrote %s (%s)"
                  % (args.output,
                     "NO GUARANTEES - --leeroy-jenkins" if args.no_verify
                     else "round-trip verified"))
            return 0
        if out_kind in ("cci", "cso"):
            build = cci_mod.build_cci if out_kind == "cci" else cso_mod.build_cso
            split = getattr(args, "split", False)
            if kind == "iso":
                written = build(path, args.output, split=split,
                                progress=prog.cb("compress"))
            elif kind == "god":
                with god_mod.GodStream(path) as stream:
                    written = build(stream, args.output, split=split,
                                    progress=prog.cb("compress"))
            elif kind == "cci" and out_kind == "cso":
                with cci_mod.CciReader(path) as stream:
                    written = build(stream, args.output, split=split,
                                    progress=prog.cb("compress"))
            elif kind == "cso" and out_kind == "cci":
                with cso_mod.CsoReader(path) as stream:
                    written = build(stream, args.output, split=split,
                                    progress=prog.cb("compress"))
            else:
                manifest = {}
                gamedir = _to_gamedir(kind, path, w, manifest=manifest,
                      progress=prog.cb("extract"))
                iso = os.path.join(w, "pivot_out.iso")
                builders_mod.build_iso(gamedir, iso, verify=not args.no_verify,
                                       manifest=manifest or None,
                                       progress=prog.cb("iso-write"),
                                       verify_progress=prog.cb("verify"))
                written = build(iso, args.output, split=split,
                                progress=prog.cb("compress"))
            if not args.no_verify:
                _verify_wrapper(out_kind, written[0], kind, path, w,
                                progress=prog.cb("verify"))
            print("wrote %s (%s)"
                  % (", ".join(written),
                     "NO GUARANTEES - --leeroy-jenkins" if args.no_verify
                     else "round-trip verified"))
            return 0
        manifest = {}
        gamedir = _to_gamedir(kind, path, w, manifest=manifest,
                      progress=prog.cb("extract"))
        manifest = manifest or None
        if out_kind == "iso":
            builders_mod.build_iso(gamedir, args.output,
                                   verify=not args.no_verify,
                                   manifest=manifest,
                                   progress=prog.cb("iso-write"),
                                   verify_progress=prog.cb("verify"))
            print("wrote %s (manifest %s)"
                  % (args.output,
                     "NO GUARANTEES - --leeroy-jenkins" if args.no_verify
                     else "round-trip verified"))
            return 0
        if out_kind == "god":
            iso = os.path.join(w, "pivot_out.iso")
            builders_mod.build_iso(gamedir, iso, verify=not args.no_verify,
                                   manifest=manifest,
                                   progress=prog.cb("iso-write"))
            hdr = builders_mod.build_god(iso, args.output,
                                         verify=not args.no_verify,
                                         progress=prog.cb("god-write"),
                                         verify_progress=prog.cb("verify"))
            print("wrote GoD container (header: %s) (%s)"
                  % (hdr, "NO GUARANTEES - --leeroy-jenkins"
                     if args.no_verify else "hash tree verified"))
            return 0
        if out_kind == "gamedir":
            os.makedirs(args.output, exist_ok=True)
            for entry in os.listdir(gamedir):
                shutil.move(os.path.join(gamedir, entry),
                            os.path.join(args.output, entry))
            # A folder is not a container: there is no output-side
            # structure to re-read, so the only claim on offer is that
            # every file was hashed on the way out of the source.
            print("wrote %s/ (%s)"
                  % (args.output.rstrip("/"),
                     "NO GUARANTEES - --leeroy-jenkins" if args.no_verify
                     else "extracted and hashed - a folder carries no "
                          "container to re-verify"))
        elif out_kind == "zar":
            zar_mod.pack(gamedir, args.output,
                         roundtrip_verify=not args.no_verify,
                         manifest=manifest, progress=prog.cb("zar-write"),
                         verify_progress=prog.cb("verify"))
            print("wrote %s (%s)"
                  % (args.output, "NO GUARANTEES - --leeroy-jenkins"
                     if args.no_verify else "verified"))
        return 0
    finally:
        shutil.rmtree(w, ignore_errors=True)


def main(argv=None):
    # Double-click / bare launch = the program: no arguments opens the
    # TUI on the current directory. Subcommands remain for the CLI.
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["tui"]
    ap = argparse.ArgumentParser(prog="xverter",
                                 description="xVerter: Xbox and Xbox 360 game format converter - "
                                             "any format in, any format out, "
                                             "verified at every step. Run "
                                             "with no arguments to open "
                                             "the TUI.")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="identify a file/directory and show details")
    p.add_argument("input")
    p.add_argument("--identify", action="store_true",
                   help="also hash the file against the bundled redump "
                        "databases: true game name + system, regardless "
                        "of filename (ISOs only; reads the whole file)")
    p.add_argument("--progress", action="store_true",
                   help="emit PROGRESS lines during the --identify hash")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("verify", help="verify integrity (hash trees, structure, DAT)")
    p.add_argument("input")
    p.add_argument("--deep", action="store_true",
                   help="fully read all data, not just structure")
    p.add_argument("--dat", help="logiqx .dat file (e.g. from redump) to "
                                 "authenticate an ISO against offline")
    p.add_argument("--progress", action="store_true",
                   help="emit machine-readable PROGRESS lines on stderr")
    p.add_argument("--no-lookup", action="store_true",
                   help="skip redump authentication entirely, including "
                        "the whole-image hashing it needs")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("test", help="validate xverter against one of your "
                                    "own games: run the full conversion "
                                    "matrix, every edge content-verified, "
                                    "and write a self-contained HTML report")
    p.add_argument("input", help="a game in any readable format")
    p.add_argument("--workdir", metavar="DIR",
                   help="scratch dir for the run (default: auto temp dir "
                        "next to the input, cleaned up after; needs ~4-5x "
                        "the game's size free)")
    p.add_argument("--leeroy-jenkins", dest="no_verify", action="store_true",
                   help="run every conversion with xverter's own checks "
                        "off. The matrix still content-compares every edge, "
                        "so breakage is still caught - this measures what "
                        "the checks cost, it does not bless the output")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("tui", help="interactive terminal UI (also: bare `xverter` with no arguments)")
    p.add_argument("library", nargs="?", default=".",
                   help="library directory to browse (default: cwd)")
    p.set_defaults(fn=cmd_tui)

    p = sub.add_parser("dat", help="manage the cached redump database")
    p.add_argument("action", choices=["update", "status"])
    p.add_argument("--system", default="xbox360",
                   help="redump system id (xbox360, xbox)")
    p.set_defaults(fn=cmd_dat)

    p = sub.add_parser("convert", help="convert between formats")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True,
                   help="output path: .iso, .zar, or a directory")
    # dest stays no_verify: 18 read sites, and the internal name is
    # invisible. The flag name is the part that has to be unmissable.
    p.add_argument("--leeroy-jenkins", dest="no_verify", action="store_true",
                   help="skip every check - structure validation, content "
                        "verification and redump authentication. Outputs "
                        "carry no guarantees whatsoever")
    p.add_argument("--workdir", metavar="DIR",
                   help="scratch directory for intermediate pivot files "
                        "(default: system temp dir / $TMPDIR; overrides "
                        "--scratch)")
    p.add_argument("--scratch", choices=["disk", "ram"], default="disk",
                   help="where pivot files live (default: disk). 'ram' "
                        "uses a tmpfs (/dev/shm) and needs ~2.2x the game "
                        "size in AVAILABLE memory; Linux only - on other "
                        "platforms create a RAM drive and use --workdir")
    p.add_argument("--progress", action="store_true",
                   help="emit machine-readable PROGRESS lines on stderr "
                        "(the TUI uses this; humans get an automatic "
                        "percent display on a tty)")
    p.add_argument("--split", action="store_true",
                   help="split .cci/.cso output into Name.1/.2 slices at "
                        "4GiB (the console convention: FATX storage caps "
                        "files at 4GiB). Default writes one file, which "
                        "PC emulators read fine but console drives can't "
                        "hold")
    p.set_defaults(fn=cmd_convert)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (CliError, detect_mod.DetectError, god_mod.GodError,
            xdvdfs_mod.XdvdfsError, zar_mod.ZarError, stfs_mod.StfsError,
            builders_mod.BuildError, datcache.DatCacheError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
