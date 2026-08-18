"""Local cache of redump.org's official DAT database exports.

Instead of per-hash website queries (or, worse, scraping), redump publishes
its full per-system database at http://redump.org/datfile/<system>/ as a
small zip. We fetch that once into ~/.cache/xverter/ and verify against it
locally; the DAT is never redistributed with this project (fetch-on-use
keeps it current and respects redump's terms).
"""

import io
import os
import re
import time
import urllib.request
import zipfile

# redump.info is the current site; redump.org is an older mirror that
# can lag it by months (it served a June export while .info had
# August). Always try .info first and only fall back to .org if it is
# unreachable, or `dat update` would quietly downgrade the database.
SYSTEMS = {
    "xbox360": ("https://redump.info/datfile/xbox360",
                "http://redump.org/datfile/xbox360/"),
    "xbox": ("https://redump.info/datfile/xbox",
             "http://redump.org/datfile/xbox/"),
}
MAX_AGE_DAYS = 30


class DatCacheError(Exception):
    pass


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME",
                          os.path.join(os.path.expanduser("~"), ".cache"))
    d = os.path.join(base, "xverter")
    os.makedirs(d, exist_ok=True)
    return d


def dat_path(system="xbox360"):
    return os.path.join(cache_dir(), "%s.dat" % system)


def bundled(system="xbox360"):
    """Path to the DAT shipped with the package (updated weekly by CI),
    or None. Works both from a normal install and from inside a zipapp:
    zip-packaged DATs are materialized into the cache dir once."""
    p = os.path.join(os.path.dirname(__file__), "data", "%s.dat" % system)
    if os.path.isfile(p):
        return p
    try:
        from importlib import resources
        src = resources.files("xverter").joinpath("data/%s.dat" % system)
        data = src.read_bytes()
    except Exception:
        return None
    out = os.path.join(cache_dir(), "bundled_%s.dat" % system)
    if not os.path.isfile(out) or os.path.getsize(out) != len(data):
        with open(out, "wb") as f:
            f.write(data)
    return out


def cached(system="xbox360"):
    """Return (path, age_days) of a cached DAT, or (None, None)."""
    p = dat_path(system)
    if not os.path.isfile(p):
        return None, None
    age = (time.time() - os.path.getmtime(p)) / 86400
    return p, age


def dat_version(path_or_bytes):
    """The <version> redump stamps into a DAT header, or "unknown"."""
    if isinstance(path_or_bytes, bytes):
        head = path_or_bytes[:2048]
    else:
        try:
            with open(path_or_bytes, "rb") as f:
                head = f.read(2048)
        except OSError:
            return "unknown"
    m = re.search(rb"<version>([^<]+)</version>", head)
    return m.group(1).decode("ascii", "replace") if m else "unknown"


def active_version(system="xbox360"):
    """Version of the DAT this install would actually use: the cached
    one if present, else the bundled one."""
    cpath, _ = cached(system)
    src = cpath or bundled(system)
    return dat_version(src) if src else "none"


def is_newer(candidate, current):
    """Both are redump's "YYYY-MM-DD HH-MM-SS" stamps, which sort as
    plain strings. An unparseable stamp on either side is never treated
    as newer: silently replacing a good DAT with an unknown one is the
    one outcome worth refusing outright."""
    if not candidate or candidate == "unknown":
        return False
    if not current or current in ("unknown", "none"):
        return True
    return candidate > current


def fetch(system="xbox360"):
    """Download redump's official DAT export and return (data, version)
    WITHOUT touching the cache. Network access, so only ever call this
    where the user asked for it."""
    if system not in SYSTEMS:
        raise DatCacheError("unknown system %r (known: %s)"
                            % (system, ", ".join(sorted(SYSTEMS))))
    blob, last = None, None
    for url in SYSTEMS[system]:
        req = urllib.request.Request(
            url, headers={"User-Agent": "xverter-datcache"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                blob = r.read()
            break
        except Exception as e:                        # noqa: BLE001
            last = e
    if blob is None:
        raise DatCacheError("could not download DAT for %s: %s"
                            % (system, last))
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in zf.namelist() if n.lower().endswith(".dat")]
        if not names:
            raise DatCacheError("no .dat inside downloaded zip")
        data = zf.read(names[0])
    except zipfile.BadZipFile:
        raise DatCacheError("download was not a zip (site change or block?)")
    return data, dat_version(data)


def save(system, data):
    """Write already-fetched DAT bytes into the cache. Returns the path."""
    p = dat_path(system)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return p


def update(system="xbox360"):
    """Fetch the official redump DAT export for a system into the cache.
    Returns (path, version, installed) - installed is False when what
    the site served is not newer than the DAT already in use, in which
    case nothing was written."""
    data, version = fetch(system)
    have = active_version(system)
    if not is_newer(version, have):
        return None, version, False
    return save(system, data), version, True
