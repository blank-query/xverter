"""ZArchive (.zar) support: native pure-Python pack/unpack, with the
reference `zarchive` binary (github.com/Exzap/ZArchive) as a fallback.

Both directions go through zar_native whenever a zstd implementation is
importable; the external binary is only invoked when neither zstd source
is available. Third-party ZAR writers have a corruption track record
(see XGDTool issues #1/#2), so the native writer structurally mirrors
the reference ZArchiveWriter (64KiB zstd blocks, BFS file tree, embedded
whole-archive SHA-256) and every pack is round-trip verified by default.

Detection: ZArchive is a footer-based format - the magic 0x169f52d6 sits
in the final bytes; the file *starts* with zstd frame data.
"""

import concurrent.futures
import hashlib
import threading
import os
import shutil
import subprocess
import tempfile

try:
    from . import zar_native
    _NATIVE = zar_native.HAVE_ZSTD
except Exception:
    zar_native = None
    _NATIVE = False

ZAR_FOOTER_MAGIC = bytes.fromhex("169f52d6")


class ZarError(Exception):
    pass


def _tool():
    from .. import deps
    exe = deps.find("zarchive")
    if not exe:
        raise ZarError("`zarchive` binary not found - install the reference "
                       "ZArchive tool (Arch: pacman -S zarchive)")
    return exe


def is_zar(path):
    try:
        with open(path, "rb") as f:
            f.seek(-16, os.SEEK_END)
            return ZAR_FOOTER_MAGIC in f.read(16)
    except OSError:
        return False


#: Files unpacked at once, each on its own reader. Two, for the
#: same reason as the xdvdfs extractor: past that the limit is
#: storage rather than CPU.
UNPACK_WORKERS = 2


class _IntegrityAhead:
    """The archive's own whole-file SHA-256, checked on a thread while
    the archive is being consumed.

    A zar carries exactly one piece of integrity data - the footer's
    SHA-256 over the entire file - and until this existed no conversion
    ever looked at it: a bit-rotted archive converted "successfully"
    into corrupted output, because the manifest it was verified against
    was computed from the corrupted bytes themselves. Found by flipping
    bits, kept out by this. The check is an independent second read on
    its own reader, overlapped with the real work; check() joins and
    raises, and every zar-consuming path calls it before claiming
    success."""

    def __init__(self, zar_path):
        import threading
        self._path = zar_path
        self._ok = []
        self._err = []
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            with zar_native.ZarReader(self._path) as zr:
                self._ok.append(zr.verify_integrity())
        except BaseException as exc:                  # noqa: BLE001
            self._err.append(exc)

    def check(self):
        self._t.join()
        if self._err:
            raise ZarError("integrity check of %s failed to run: %s"
                           % (self._path, self._err[0]))
        if not self._ok or not self._ok[0]:
            raise ZarError(
                "%s FAILS its own integrity hash - the archive is "
                "damaged and its contents cannot be trusted"
                % self._path)


def iso_tree(zar_path, manifest=None, integrity=True):
    """(tree, closer) for packing this zar straight into an ISO.

    Files are read out of the archive on demand instead of being
    unpacked to a scratch directory first, which saves writing the whole
    game to disk and reading it back. Each file is hashed as it is
    consumed, so `manifest` fills with exactly the per-file digests the
    ISO writer's verification pass needs."""
    if not _NATIVE:
        raise ZarError("streaming a zar needs the native reader (zstd "
                       "support missing)")
    zr = zar_native.ZarReader(zar_path)

    def _opener(rel):
        def go():
            return _HashingStream(zr.read_iter(rel), rel, manifest)
        return go

    # Anything that raises before we return the closer must not leak the
    # open reader (a name collision in tree_from_entries, an unreadable
    # file table). Close it on the way out.
    try:
        entries = [(rel, size, _opener(rel)) for rel, size in zr.files()]
        guard = _IntegrityAhead(zar_path) if integrity and _NATIVE else None
        from . import xdvdfs as _xd
        tree = _xd.tree_from_entries(entries, where=zar_path)
    except BaseException:
        zr.close()
        raise

    def closer():
        try:
            zr.close()
        finally:
            if guard is not None:
                guard.check()

    return tree, closer


class _HashingStream:
    """Read-only stream over a zar member, hashing as it goes."""

    def __init__(self, chunks, rel, manifest):
        self._chunks = chunks
        self._rel = rel
        self._manifest = manifest
        self._h = hashlib.sha1() if manifest is not None else None
        self._buf = b""
        self._eof = False

    def read(self, n=-1):
        while not self._eof and (n < 0 or len(self._buf) < n):
            try:
                c = next(self._chunks)
            except StopIteration:
                self._eof = True
                break
            if self._h is not None:
                self._h.update(c)
            self._buf += c
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self):
        if self._h is not None and self._manifest is not None:
            self._manifest[self._rel] = self._h.hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def unpack(zar_path, out_dir, manifest=None, progress=None,
           integrity=True):
    """Extract a zar. Uses the native reader when zstd support is present
    (allowing inline manifest capture); falls back to the reference binary.

    integrity=True (the default) checks the archive's own whole-file
    SHA-256 on a thread while extracting, and fails the extraction on a
    mismatch - see _IntegrityAhead for the bit-rot story."""
    guard = _IntegrityAhead(zar_path) if integrity and _NATIVE else None
    if _NATIVE:
        try:
            with zar_native.ZarReader(zar_path) as zr:
                os.makedirs(out_dir, exist_ok=True)
                _flist = zr.files()
                _grand = sum(sz for _r, sz in _flist) or 1
                _done = [0]
                for rel, _size in _flist:      # directories first, once
                    dest = os.path.join(out_dir, rel.replace("/", os.sep))
                    os.makedirs(os.path.dirname(dest) or out_dir,
                                exist_ok=True)
                # Recreate directories that hold no files too - a
                # files-only pre-pass silently dropped empty directories,
                # a structural difference that the content-only manifest
                # comparison could never catch.
                for rel in zr.directories():
                    os.makedirs(
                        os.path.join(out_dir, rel.replace("/", os.sep)),
                        exist_ok=True)

                # Files are independent, so a couple come out at a time,
                # each on its own reader - the same scheduling change the
                # xdvdfs extractor got, and for the same reason: one file
                # at a time leaves the machine idle. Same bytes, same
                # manifest; only the order of work changes.
                lock = threading.Lock()
                local = threading.local()
                opened = []

                def _reader():
                    r = getattr(local, "r", None)
                    if r is None:
                        r = local.r = zar_native.ZarReader(zar_path)
                        with lock:
                            opened.append(r)
                    return r

                def _one(job):
                    rel, _size = job
                    dest = os.path.join(out_dir, rel.replace("/", os.sep))
                    h = hashlib.sha1() if manifest is not None else None
                    with open(dest, "wb") as o:
                        for chunk in _reader().read_iter(rel):
                            o.write(chunk)
                            if h is not None:
                                h.update(chunk)
                            if progress:
                                with lock:
                                    _done[0] += len(chunk)
                                    progress(_done[0], _grand)
                    return rel, (h.hexdigest() if h is not None else None)

                try:
                    if UNPACK_WORKERS > 1 and len(_flist) > 1:
                        with concurrent.futures.ThreadPoolExecutor(
                                max_workers=UNPACK_WORKERS,
                                thread_name_prefix="zar") as ex:
                            results = list(ex.map(_one, _flist))
                    else:
                        results = [_one(j) for j in _flist]
                finally:
                    for r in opened:
                        try:
                            r.close()
                        except Exception:             # noqa: BLE001
                            pass
                if manifest is not None:
                    for rel, digest in results:
                        manifest[rel] = digest
            if not any(os.scandir(out_dir)):
                raise ZarError("zar contained no files")
            if guard is not None:
                guard.check()
            return
        except zar_native.ZarNativeError as e:
            raise ZarError("native zar read failed: %s" % e)
    r = subprocess.run([_tool(), zar_path, out_dir],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ZarError("zarchive unpack failed: %s" % (r.stderr or r.stdout).strip())
    if not any(os.scandir(out_dir)):
        raise ZarError("zarchive unpack produced an empty directory")


def hash_walk(zar_path, progress=None):
    """Native manifest of a zar's contents without extraction, or None if
    the native reader is unavailable."""
    if not _NATIVE:
        return None
    with zar_native.ZarReader(zar_path) as zr:
        return zr.hash_walk(progress=progress)


def list_files(zar_path):
    """File list [(path, size)] from the metadata tree only (no data reads),
    or None if the native reader is unavailable."""
    if not _NATIVE:
        return None
    with zar_native.ZarReader(zar_path) as zr:
        return zr.files()


def verify_native(zar_path, progress=None):
    """Full native verification: embedded SHA-256 integrity over all bytes.
    Returns file count, or None if native reader unavailable."""
    if not _NATIVE:
        return None
    with zar_native.ZarReader(zar_path) as zr:
        if not zr.verify_integrity(progress=progress):
            raise ZarError("embedded SHA-256 integrity check FAILED")
        return len(zr.files())


def can_stream():
    """True when a source image can be packed straight into a zar.

    The reference binary only takes a directory, so the streamed route
    exists only where the native writer does."""
    return bool(_NATIVE)


def pack_entries(entries, zar_path, roundtrip_verify=True, manifest=None,
                 progress=None, verify_progress=None):
    """Pack (relpath, size, opener) entries into a zar without staging
    them on disk. See zar_native.pack_entries for why the result is
    byte-identical to packing the same files out of a directory.

    `manifest` is filled in by the entry streams as they are consumed,
    so it is only complete once packing has finished - which is why the
    verification pass reads it afterwards rather than being handed it.
    """
    if not _NATIVE:
        raise ZarError("streamed pack needs the native writer")
    if os.path.exists(zar_path):
        raise ZarError("output already exists: %s" % zar_path)
    entries = list(entries)
    # Same refusal as pack(): a 128+ byte name component packs cleanly
    # and then misparses in the reference reader, so catch it up front.
    for rel, _size, _opener in entries:
        for name in rel.split("/"):
            if len(name.encode("utf-8", "surrogateescape")) >= 128:
                raise ZarError(
                    "cannot pack: name %r (in %s) is %d bytes; the "
                    "ZArchive format's reference reader breaks at 128+ "
                    "characters - rename it first"
                    % (name, rel,
                       len(name.encode("utf-8", "surrogateescape"))))
    try:
        zar_native.pack_entries(entries, zar_path, progress=progress)
    except zar_native.ZarNativeError as e:
        raise ZarError("native zar pack failed: %s" % e)
    if not roundtrip_verify:
        return
    if entries and not manifest:
        # Nothing on disk to fall back to: without hashes captured on
        # the way in there is no independent record of what went in, and
        # a verification that compares the archive against itself is
        # worse than none. An archive with no files in it is the one
        # case with genuinely nothing to check.
        raise ZarError("streamed pack cannot be verified without a manifest")
    got = hash_walk(zar_path, progress=verify_progress)
    # An empty streamed pack (entries=[], no manifest) reaches here past
    # the guard above with manifest None; an empty archive is correct,
    # so compare against {} rather than crashing on set(None).
    want = manifest or {}
    if got != want:
        raise ZarError(
            "packed zar manifest mismatch: missing=%s extra=%s diff=%s"
            % (sorted(set(want) - set(got))[:3],
               sorted(set(got) - set(want))[:3],
               sorted(k for k in set(got) & set(want)
                      if got[k] != want[k])[:3]))


def pack(src_dir, zar_path, roundtrip_verify=True, manifest=None,
         progress=None, verify_progress=None):
    if os.path.exists(zar_path):
        raise ZarError("output already exists: %s" % zar_path)
    # The ZArchive reference implementation (which Xenia embeds) misreads
    # name components of 128+ characters - a zar containing one would be
    # written "successfully" and then misparse in every consumer. Refuse
    # up front with the offending path named.
    for dirpath, dirnames, filenames in os.walk(src_dir):
        for name in list(dirnames) + filenames:
            if len(name.encode("utf-8", "surrogateescape")) >= 128:
                raise ZarError(
                    "cannot pack: name %r (under %s) is %d bytes; the "
                    "ZArchive format's reference reader breaks at 128+ "
                    "characters - rename it first"
                    % (name, os.path.relpath(dirpath, src_dir),
                       len(name.encode("utf-8", "surrogateescape"))))
    # Snapshot the source manifest BEFORE writing, so a zar_path that
    # lands inside src_dir cannot appear as an "extra" file in the
    # post-pack re-walk and fail an otherwise-correct archive.
    if roundtrip_verify and _NATIVE and not manifest:
        from .xdvdfs import hash_tree
        manifest = hash_tree(src_dir)
    if _NATIVE:
        try:
            zar_native.pack(src_dir, zar_path, progress=progress)
        except zar_native.ZarNativeError as e:
            raise ZarError("native zar pack failed: %s" % e)
    else:
        r = subprocess.run([_tool(), src_dir, zar_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ZarError("zarchive pack failed: %s"
                           % (r.stderr or r.stdout).strip())
    if roundtrip_verify:
        if _NATIVE:
            # no temp extraction: stream-hash the packed archive in place
            want = manifest
            got = hash_walk(zar_path, progress=verify_progress)
            if got != want:
                raise ZarError(
                    "packed zar manifest mismatch: missing=%s extra=%s diff=%s"
                    % (sorted(set(want) - set(got))[:3],
                       sorted(set(got) - set(want))[:3],
                       sorted(k for k in set(got) & set(want)
                              if got[k] != want[k])[:3]))
            return
        tmp = tempfile.mkdtemp(prefix="zarrt_")
        try:
            unpack(zar_path, tmp)
            if manifest:
                from .xdvdfs import hash_tree
                got = hash_tree(tmp)
                if got != manifest:
                    raise ZarError(
                        "round-trip manifest mismatch: missing=%s extra=%s diff=%s"
                        % (sorted(set(manifest) - set(got))[:3],
                           sorted(set(got) - set(manifest))[:3],
                           sorted(k for k in set(got) & set(manifest)
                                  if got[k] != manifest[k])[:3]))
            else:
                _assert_trees_identical(src_dir, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _assert_trees_identical(a, b):
    import filecmp

    def walk(dc):
        if dc.left_only or dc.right_only or dc.diff_files or dc.funny_files:
            raise ZarError(
                "round-trip mismatch: only-in-src=%s only-in-zar=%s diff=%s"
                % (dc.left_only, dc.right_only, dc.diff_files + dc.funny_files))
        for sub in dc.subdirs.values():
            walk(sub)

    walk(filecmp.dircmp(a, b, shallow=False))
