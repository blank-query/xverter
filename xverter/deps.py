"""Locating optional helper binaries.

xVerter requires no external tools - every reader and writer is its
own code, CHD included as of 1.2.0. What remains here is a resolver
for the strictly optional helpers some workflows still recognise (the
test suite will use chdman as a differential referee when present;
the zar path can point at a reference binary for cross-checks). Tools
are searched first in xverter's own bin directory, then on PATH.

The registry, version checks, GitHub release lookups and one-click
installers that used to live here left with the last delegated
format: there is nothing to install any more.
"""

import os
import shutil


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
