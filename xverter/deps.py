"""Locating optional helper binaries.

xVerter requires no external tools - every reader and writer is its
own code. What remains here is a path resolver kept for the zar
cross-check hook and any future optional helper. Tools are searched
first in xverter's own bin directory, then on PATH.
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
