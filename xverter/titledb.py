"""Title-id -> game-name lookup for Xbox 360.

An Xbox 360 executable (XEX) carries no display name, so a package
synthesized from a 360 game (`iso/zar/... -> stfs/god`) has no name of its
own to show on a dashboard. This resolves the title id - which we DO read
from the payload default.xex - to the game's marketplace name, so a
converted package names itself automatically.

The mapping is distilled from xenia-manager/x360db, an open recreation of
the Xbox Marketplace metadata archive; a title_id <-> name correspondence
is factual data. A compact copy is bundled in data/xbox360_titles.json and
refreshable in-place, exactly as the redump DAT is (see datcache).

Original Xbox games are NOT covered here and do not need to be: an XBE
carries its display name in its certificate, which the reader already
extracts - so OG Xbox names come straight from the game.
"""

import json
import os

X360DB_URL = "https://raw.githubusercontent.com/xenia-manager/x360db/main/games.json"

# xVerter's own reserved title ids - the legal test discs, so that converting
# one names itself as a first-class release instead of tripping the "matches
# no retail game" warning. Their title ids spell the disc format (XGD1/2/3)
# and are checked BEFORE the x360db map, so they always resolve and survive a
# `dat update` that refreshes the external database. Coordinated with the
# test-disc project, which stamps these ids into the discs' executables.
XVERTER_TITLES = {
    "58474431": "xVerter Test Disc (XGD1)",   # "XGD1"
    "58474432": "xVerter Test Disc (XGD2)",   # "XGD2"
    "58474433": "xVerter Test Disc (XGD3)",   # "XGD3"
}

_cache = {}


def _bundled_path(system="xbox360"):
    return os.path.join(os.path.dirname(__file__), "data",
                        "%s_titles.json" % system)


def _cache_path(system="xbox360"):
    base = os.environ.get(
        "XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache"))
    d = os.path.join(base, "xverter")
    return os.path.join(d, "%s_titles.json" % system)


def _load(system="xbox360"):
    """The title map: a freshly fetched cache if present, else the bundled
    copy, else empty. Parsed once per process."""
    if system in _cache:
        return _cache[system]
    m = {}
    for path in (_cache_path(system), _bundled_path(system)):
        try:
            with open(path, encoding="utf-8") as f:
                m = json.load(f)
            break
        except (OSError, ValueError):
            continue
    _cache[system] = m
    return m


def name_for_title_id(title_id, system="xbox360"):
    """The game's name for a 32-bit title id, or None. Accepts an int or a
    hex string; matching is case-insensitive on the 8-digit form."""
    if title_id in (None, 0, ""):
        return None
    if isinstance(title_id, str):
        try:
            title_id = int(title_id, 16)
        except ValueError:
            return None
    key = "%08X" % (int(title_id) & 0xFFFFFFFF)
    return XVERTER_TITLES.get(key) or _load(system).get(key) or None


def xverter_icon():
    """The bundled xVerter icon PNG bytes, or None. Auto-embedded as the
    package thumbnail for the reserved test discs so they show art."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "data",
                               "xverter_icon.png"), "rb") as f:
            return f.read()
    except OSError:
        return None


def distill(games):
    """Reduce x360db's game records to {TITLE_ID: name}. The primary id
    wins; regional alternate ids fill in only where unmapped."""
    out = {}
    for g in games:
        tid = (g.get("id") or "").strip().upper()
        title = (g.get("title") or "").strip()
        if len(tid) == 8 and title:
            out.setdefault(tid, title)
    for g in games:
        title = (g.get("title") or "").strip()
        for alt in g.get("alternative_id") or []:
            alt = (alt or "").strip().upper()
            if len(alt) == 8 and title:
                out.setdefault(alt, title)
    return out


def fetch():
    """Download x360db and return the distilled {TITLE_ID: name} map.
    Network access - only call where the user asked for it."""
    import urllib.request
    req = urllib.request.Request(
        X360DB_URL, headers={"User-Agent": "xverter-titledb"})
    with urllib.request.urlopen(req, timeout=30) as r:
        games = json.loads(r.read().decode("utf-8"))
    return distill(games)


def update(system="xbox360"):
    """Refresh the cached title map from x360db. Returns (path, count).
    Atomic write, mirroring datcache.save()."""
    data = fetch()
    p = _cache_path(system)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = "%s.tmp.%d" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True)
    os.replace(tmp, p)
    _cache.pop(system, None)
    return p, len(data)
