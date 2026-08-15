"""External-tool management: discovery, version checks and (where a
project publishes per-platform release binaries) one-click installs.

xVerter requires no external tools; this registry exists for the
strictly optional ones - today just chdman, wanted only for CHD. Tools
are searched first in xverter's own bin directory (created on demand,
see bin_dir()), then on PATH. Downloads come from official GitHub
releases over HTTPS (certificate-verified); tools with no per-platform
release binaries (chdman ships inside MAME) report an install hint
instead of a button.
"""

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

USER_AGENT = "xverter-deps"

# Every other writer is native as of 1.0: ISO, GoD, ZAR, CCI, CSO and
# STFS reading all run on xverter's own code. chdman remains the one
# delegated tool - CHD is MAME's format and chdman its living reference.
TOOLS = {
    "chdman": {
        "repo": None,   # ships inside MAME; no standalone release
        "version_argv": ["help"],
        "hint": "install MAME tools (Arch/Debian/Ubuntu: mame-tools; "
                "macOS: brew install rom-tools; Windows: chdman.exe "
                "ships inside the MAME download)",
        "needed_for": ".chd in/out (optional - only if you want CHD)",
    },
}


def bin_dir(create=False):
    """xverter's own tool directory, searched before PATH."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA",
                              os.path.expanduser("~\\AppData\\Local"))
        d = os.path.join(root, "xverter", "bin")
    else:
        d = os.path.join(os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            "xverter", "bin")
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def find(tool):
    """Resolve a tool: xverter bin dir first, then PATH."""
    d = bin_dir()
    for name in ((tool + ".exe", tool) if os.name == "nt" else (tool,)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(tool)


def _installed_version(tool, path):
    argv = TOOLS[tool]["version_argv"]
    if not argv:
        return None
    try:
        r = subprocess.run([path] + argv, capture_output=True, text=True,
                           timeout=10)
        out = (r.stdout or r.stderr).strip().splitlines()
        m = re.search(r"\d+(\.\d+)+", out[0] if out else "")
        return m.group(0) if m else None
    except Exception:
        return None


def _latest_release(repo):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/releases/latest" % repo,
        headers={"User-Agent": USER_AGENT,
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    m = re.search(r"\d+(\.\d+)+", tag)
    return {"tag": tag, "version": m.group(0) if m else None,
            "assets": [{"name": a["name"],
                        "url": a["browser_download_url"]}
                       for a in data.get("assets", [])]}


def _pick_asset(assets):
    """Choose the release asset for this platform/arch, or None."""
    sysname = {"linux": ("linux",), "darwin": ("darwin", "macos", "osx"),
               "win32": ("windows", "win64", "win32")}.get(
        sys.platform, (sys.platform,))
    mach = platform.machine().lower()
    arches = {"x86_64": ("x86_64", "amd64", "x64"),
              "amd64": ("x86_64", "amd64", "x64"),
              "aarch64": ("aarch64", "arm64"),
              "arm64": ("aarch64", "arm64")}.get(mach, (mach,))
    scored = []
    for a in assets:
        n = a["name"].lower()
        if n.endswith((".sha256", ".sig", ".asc", ".txt", ".sbom")):
            continue
        if not any(s in n for s in sysname):
            continue
        score = 2 if any(x in n for x in arches) else \
            1 if not any(x in n for x in
                         ("x86_64", "amd64", "x64", "aarch64", "arm64",
                          "i686", "armv7")) else 0
        if score:
            scored.append((score, a))
    scored.sort(key=lambda s: -s[0])
    return scored[0][1] if scored else None


def check(tool, online=False):
    """Status dict for one tool. online=True also queries the latest
    release to detect updates (network; may raise)."""
    info = TOOLS[tool]
    path = find(tool)
    st = {"tool": tool, "path": path, "needed_for": info["needed_for"],
          "hint": info["hint"], "repo": info["repo"],
          "installed_version": _installed_version(tool, path)
          if path else None,
          "latest_version": None, "asset": None}
    if online and info["repo"]:
        rel = _latest_release(info["repo"])
        st["latest_version"] = rel["version"]
        st["asset"] = _pick_asset(rel["assets"])
    return st


def check_all(online=False):
    out = []
    for tool in TOOLS:
        try:
            out.append(check(tool, online=online))
        except Exception as e:                        # noqa: BLE001
            st = check(tool, online=False)
            st["error"] = str(e)
            out.append(st)
    return out


def install(tool, log=lambda m: None):
    """Download the latest release asset for this platform into
    xverter's bin dir. Returns the installed path."""
    info = TOOLS[tool]
    if not info["repo"]:
        raise RuntimeError("%s has no downloadable release: %s"
                           % (tool, info["hint"]))
    rel = _latest_release(info["repo"])
    asset = _pick_asset(rel["assets"])
    if asset is None:
        raise RuntimeError("no %s release asset for this platform "
                           "(%s/%s): %s" % (tool, sys.platform,
                                            platform.machine(),
                                            info["hint"]))
    log("downloading %s (%s) ..." % (asset["name"], rel["tag"]))
    req = urllib.request.Request(asset["url"],
                                 headers={"User-Agent": USER_AGENT})
    with tempfile.TemporaryDirectory(prefix="xverter_dep_") as td:
        blob = os.path.join(td, asset["name"])
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(blob, "wb") as o:
            shutil.copyfileobj(r, o)
        exe = _extract_binary(tool, blob, td, log)
        dest = os.path.join(bin_dir(create=True),
                            os.path.basename(exe))
        shutil.move(exe, dest)
        os.chmod(dest, os.stat(dest).st_mode
                 | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log("installed %s -> %s" % (tool, dest))
    return dest


def _extract_binary(tool, blob, td, log):
    """Find the tool's executable inside a downloaded asset (archive or
    bare binary)."""
    names = (tool, tool + ".exe")
    out = os.path.join(td, "x")
    os.makedirs(out, exist_ok=True)
    low = blob.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(blob) as z:
            z.extractall(out)
    elif low.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        with tarfile.open(blob) as t:
            t.extractall(out, filter="data")
    else:
        # bare binary: normalize its name to the tool's
        dest = os.path.join(td, tool + (".exe" if blob.lower()
                                        .endswith(".exe") else ""))
        os.replace(blob, dest)
        return dest
    candidates = []
    for root, _dirs, files in os.walk(out):
        for fn in files:
            if fn.lower() in names or fn.lower().startswith(tool):
                candidates.append(os.path.join(root, fn))
    if not candidates:
        raise RuntimeError("downloaded %s archive contains no %r binary"
                           % (os.path.basename(blob), tool))
    candidates.sort(key=lambda p: (not os.path.basename(p).lower()
                                   in names, len(p)))
    return candidates[0]
