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

SYSTEMS = {
    "xbox360": "http://redump.org/datfile/xbox360/",
    "xbox": "http://redump.org/datfile/xbox/",
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


def update(system="xbox360"):
    """Fetch the official redump DAT export for a system into the cache.
    Returns (path, version_string)."""
    if system not in SYSTEMS:
        raise DatCacheError("unknown system %r (known: %s)"
                            % (system, ", ".join(sorted(SYSTEMS))))
    req = urllib.request.Request(
        SYSTEMS[system], headers={"User-Agent": "xverter-datcache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            blob = r.read()
    except Exception as e:
        raise DatCacheError("could not download DAT for %s: %s" % (system, e))
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in zf.namelist() if n.lower().endswith(".dat")]
        if not names:
            raise DatCacheError("no .dat inside downloaded zip")
        data = zf.read(names[0])
    except zipfile.BadZipFile:
        raise DatCacheError("download was not a zip (site change or block?)")
    p = dat_path(system)
    with open(p, "wb") as f:
        f.write(data)
    m = re.search(rb"<version>([^<]+)</version>", data[:2048])
    version = m.group(1).decode("ascii", "replace") if m else "unknown"
    return p, version
