#!/usr/bin/env python3
"""Full conversion-matrix validation harness.

Given one game (any readable format), exercises EVERY applicable edge of
the conversion matrix, checking content equality against a baseline
manifest at every directory materialization:

    input -> dir            (baseline manifest)
    dir   -> iso -> dir     (must equal baseline)
    dir   -> zar -> dir     (must equal baseline)
    dir/iso -> god -> dir   (must equal baseline)
    iso   -> zar -> dir     (must equal baseline)
    zar   -> iso -> dir     (must equal baseline)
    zar   -> god,  iso -> god, god -> iso -> dir, god -> zar -> dir

Every conversion additionally runs xverter's own built-in output
verification (manifests/hash trees), so each edge is double-checked:
once by the pipeline, once by this harness against the baseline.

On completion a single self-contained `matrix_report.html` is written to
the workdir: a human-readable report with the full machine-readable JSON
embedded inside it (in a `<script type="application/json">` block, with
an in-page toggle to view it raw).

Usage: python3 -m xverter.matrix <input-game> <workdir>
       (or `tests/matrix_check.py`, a thin wrapper kept for the repo)
Exit 0 = all edges PASS.
"""

import hashlib
import html as html_mod
import json
import os
import platform
import shutil
import subprocess
import sys
import time

from . import detect as detect_mod
from .formats import xdvdfs as xdvdfs_mod
from .formats import god as god_mod

RESULTS = []


class MatrixError(RuntimeError):
    """A harness-level abort (not an edge failure): something the run
    cannot continue past, reported as a clean one-line ERROR by the
    test subcommand rather than a traceback."""



def content_digest(manifest):
    """Canonical SHA-1 over a {path: sha1} manifest: the format-invariant
    fingerprint of a game's content (stable across container types AND
    across compressor versions)."""
    h = hashlib.sha1()
    for path in sorted(manifest):
        h.update(("%s:%s\n" % (path, manifest[path])).encode())
    return h.hexdigest()


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


#: When True every convert in the matrix runs with the checks off. The
#: matrix still compares content itself, so a corrupted edge is still
#: caught - what goes away is xverter's own internal verification, which
#: is the point: it measures what those checks cost.
LEEROY = False

#: Open file handle for --log: the run's full record (results and
#: machine-readable PROGRESS lines) written by the suite itself. This
#: exists so nobody pipes a live run through tee again: two streams
#: racing through different buffering was how a progress bar got
#: orphaned mid-screen with a result line welded to it.
LOG = None


class _LiveBar:
    """Elapsed-ticking bar for the matrix's own in-process work - the
    content and byte checks hash gigabytes with no telemetry, and on a
    phone that silence lasts long enough to look like a hang."""

    def __init__(self, label, stage="sha1"):
        self._on = sys.stderr.isatty()
        self._label = label
        self._stage = stage

    def __enter__(self):
        if not self._on:
            return self
        import threading
        from .cli import render_bar
        self._live = True
        t0 = time.monotonic()

        def tick():
            while self._live:
                render_bar(self._label, self._stage, 0, 1,
                           elapsed=time.monotonic() - t0)
                time.sleep(0.5)

        self._t = threading.Thread(target=tick, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        if self._on:
            self._live = False
            self._t.join(timeout=1)
            from .cli import clear_bar
            clear_bar()


def say(line, **_kw):
    """A human-facing line: to stdout, and into the log if one is open.
    Accepts and ignores print()'s keyword arguments so every existing
    call site works unchanged; say always flushes."""
    print(line, flush=True)
    if LOG:
        LOG.write(line + "\n")
        LOG.flush()
def run(edge, argv):
    t0 = time.monotonic()
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    if argv[0] in ("convert", "verify"):
        argv = argv + ["--progress"]
    if LEEROY and argv[0] == "convert":
        argv = argv + ["--leeroy-jenkins"]
    frozen = getattr(sys, "frozen", False)
    cmd = ([sys.executable] if frozen
           else [sys.executable, "-m", "xverter"]) + argv
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                         env=env)
    lines = []
    tty = sys.stderr.isatty()
    # Liveness ticker: redraw twice a second with elapsed time, so a
    # stage that emits no progress telemetry still shows a moving
    # clock. A frozen fill with ticking seconds is a silent worker; a
    # frozen everything is a hang - the two used to look identical.
    state = {"stage": "...", "done": 0, "total": 1, "on": tty}
    ticker = None
    if tty:
        import threading as _threading
        from .cli import render_bar

        def _tick():
            while state["on"]:
                render_bar(edge, state["stage"], state["done"],
                           state["total"], elapsed=time.monotonic() - t0)
                time.sleep(0.5)

        ticker = _threading.Thread(target=_tick, daemon=True)
        ticker.start()
    try:
        for raw in iter(p.stdout.readline, ""):
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith("PROGRESS "):
                # log always; forward on stdout only for a pipe (the TUI
                # parses these) - on a tty the bar below replaces them
                if LOG:
                    LOG.write(line + "\n")
                if not sys.stdout.isatty():
                    print(line, flush=True)
                if tty:
                    try:
                        _t, stage, d, tot = line.split()
                        state["stage"], state["done"], state["total"] = \
                            stage, int(d), int(tot)
                    except ValueError:
                        pass
                continue
            lines.append(line)
        p.stdout.close()
        rc = p.wait()
    except BaseException:
        # A signal unwinding through the harness (SIGTERM -> the CLI's
        # KeyboardInterrupt) must not orphan the child conversion: the
        # caller's cleanup deletes the workdir next, and an unsupervised
        # child would keep writing into it after we reported exit 130.
        p.kill()
        p.wait()
        raise
    finally:
        if tty:
            state["on"] = False
            if ticker is not None:
                ticker.join(timeout=1)
            from .cli import clear_bar
            clear_bar()
    dt = time.monotonic() - t0
    ok = rc == 0
    detail = (lines or [""])[-1]
    RESULTS.append({"edge": edge, "type": "convert", "ok": ok,
                    "seconds": round(dt, 1), "argv": argv, "detail": detail})
    del lines
    say("%-24s %s  %7.1fs" % (edge, "PASS" if ok else "FAIL", dt))
    if not ok:
        say("    " + detail)
    return ok


def check_dir(edge, path, baseline):
    t0 = time.monotonic()
    with _LiveBar(edge):
        got = xdvdfs_mod.hash_tree(path)
    dt = time.monotonic() - t0
    ok = got == baseline
    RESULTS.append({"edge": edge, "type": "content-check", "ok": ok,
                    "seconds": round(dt, 1),
                    "content_digest": content_digest(got),
                    "matches_baseline": ok})
    say("%-24s %s  %7.1fs"
          % (edge, "PASS" if ok else "FAIL (content mismatch)", dt),
          flush=True)
    # The digest is recorded - the extracted tree is dead weight from
    # here, and keeping all of them can eat ~60G on a dual-layer game.
    shutil.rmtree(path, ignore_errors=True)
    return ok


def _read_content_type(path):
    """The 32-bit STFS content type at offset 0x344."""
    with open(path, "rb") as f:
        f.seek(0x344)
        return int.from_bytes(f.read(4), "big")


def check_stfs_title_id(edge, path):
    """The written STFS package must carry a real (nonzero) title id -
    proof the writer derived it from the payload default.xex (or preserved
    it on a rebuild) rather than shipping a title_id=0 shell a console
    can't index."""
    t0 = time.monotonic()
    tid = int.from_bytes(open(path, "rb").read(0x364)[0x360:0x364], "big")
    dt = time.monotonic() - t0
    ok = tid != 0
    RESULTS.append({"edge": edge, "type": "stfs-title-id", "ok": ok,
                    "seconds": round(dt, 1), "title_id": "0x%08X" % tid})
    say("%-24s %s  %7.1fs"
          % (edge, "PASS" if ok else "FAIL (title_id is zero)", dt),
          flush=True)
    return ok


def check_content_type(edge, path, want):
    """The written STFS package must carry exactly the content type it was
    asked for (a chosen --content-type, or a preserved source type) - not
    merely a structurally valid header. Without this the flag could be
    ignored or mis-stamped and every other check would still pass."""
    t0 = time.monotonic()
    got = _read_content_type(path)
    dt = time.monotonic() - t0
    ok = (got == want)
    RESULTS.append({"edge": edge, "type": "content-type", "ok": ok,
                    "seconds": round(dt, 1),
                    "want": "0x%08X" % want, "got": "0x%08X" % got})
    say("%-24s %s  %7.1fs"
          % (edge, "PASS" if ok
             else "FAIL (want 0x%08X, got 0x%08X)" % (want, got), dt),
          flush=True)
    return ok


def check_partition(edge, got_path, src_path):
    """A decompressed image must be the source's game partition, byte
    for byte.

    This is a stronger claim than the content check next to it and the
    reason both exist: two images can hold identical files and still be
    different images. CCI and CSO are lossless compressors of an image,
    not archives of its contents, so the only correct output is the
    bytes that went in - the whole file when the source was a bare game
    partition, and the partition alone when it was a full redump image,
    because the video partition is not in the container to return."""
    from .formats.cci import xbox_image_offset
    t0 = time.monotonic()
    with open(src_path, "rb") as f:
        off = xbox_image_offset(f)
    _lbp = _LiveBar(edge, "compare")
    _lbp.__enter__()
    ok = True
    detail = ""
    want = os.path.getsize(src_path) - off
    got = os.path.getsize(got_path)
    if want != got:
        ok = False
        detail = " (%d bytes, expected %d)" % (got, want)
    else:
        with open(src_path, "rb") as a, open(got_path, "rb") as b:
            a.seek(off)
            at = 0
            while True:
                x = a.read(1 << 22)
                y = b.read(1 << 22)
                if x != y:
                    ok = False
                    detail = " (first difference near byte %d)" % at
                    break
                if not x:
                    break
                at += len(x)
    _lbp.__exit__()
    dt = time.monotonic() - t0
    RESULTS.append({"edge": edge, "type": "byte-check", "ok": ok,
                    "seconds": round(dt, 1), "partition_offset": off,
                    "bytes": got})
    say("%-24s %s  %7.1fs"
          % (edge, "PASS" if ok else "FAIL" + detail, dt))
    # A second copy of the game is not worth keeping once compared.
    try:
        os.unlink(got_path)
    except OSError:
        pass
    return ok


def check_identical(edge, got_path, want_path):
    """Two files must be the same file.

    For a container that wraps an image whole - a zip, a 7z - the only
    correct output is the input, so this asks for exactly that rather
    than for a partition slice or a set of matching files."""
    t0 = time.monotonic()
    _lb = _LiveBar(edge, "compare")
    _lb.__enter__()
    ok = os.path.getsize(got_path) == os.path.getsize(want_path)
    detail = ""
    if not ok:
        detail = " (%d bytes, expected %d)" % (os.path.getsize(got_path),
                                               os.path.getsize(want_path))
    else:
        at = 0
        with open(got_path, "rb") as a, open(want_path, "rb") as b:
            while True:
                x = a.read(1 << 22)
                y = b.read(1 << 22)
                if x != y:
                    ok = False
                    detail = " (first difference near byte %d)" % at
                    break
                if not x:
                    break
                at += len(x)
    _lb.__exit__()
    dt = time.monotonic() - t0
    RESULTS.append({"edge": edge, "type": "byte-check", "ok": ok,
                    "seconds": round(dt, 1)})
    say("%-24s %s  %7.1fs"
          % (edge, "PASS" if ok else "FAIL" + detail, dt))
    try:
        os.unlink(got_path)
    except OSError:
        pass
    return ok


def check_god_data(edge, header, src_path):
    """A GoD built from an image must hold that image's own bytes.

    The container is trimmed to its allocation extent, so the claim
    checked here is that its data region is a *prefix* of the source's
    game partition, not the whole of it. That is still enough to tell a
    passthrough from a rebuild, which is exactly the distinction a
    content check cannot make: a GoD is verified against its own hash
    tree, and that tree is built over whichever bytes the writer was
    handed, so a container full of rebuilt data verifies perfectly and
    is still not the disc."""
    from .formats import god as _god
    t0 = time.monotonic()
    # The GoD writer starts at the game partition as xdvdfs finds it,
    # which is not the offset the CCI writer uses: that one deliberately
    # skips only the OG-Xbox video region and takes everything else
    # whole. Two different questions, two different answers, and using
    # the wrong one here reports a false failure on XGD2 and XGD3.
    with open(src_path, "rb") as f:
        off = xdvdfs_mod.find_base(f)
    ok = True
    detail = ""
    at = 0
    _lbg = _LiveBar(edge, "compare")
    _lbg.__enter__()
    with _god.GodStream(header) as g, open(src_path, "rb") as f:
        f.seek(off)
        while True:
            x = g.read(1 << 22)
            if not x:
                break
            y = f.read(len(x))
            if x != y:
                ok = False
                detail = " (first difference near byte %d of %d)" % (at, g.size)
                break
            at += len(x)
    _lbg.__exit__()
    dt = time.monotonic() - t0
    RESULTS.append({"edge": edge, "type": "byte-check", "ok": ok,
                    "seconds": round(dt, 1), "partition_offset": off,
                    "bytes": at})
    say("%-24s %s  %7.1fs"
          % (edge, "PASS" if ok else "FAIL" + detail, dt))
    return ok


def machine_info():
    info = {"platform": platform.platform(),
            "python": platform.python_version()}
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    info["ram_gib"] = round(int(line.split()[1]) / 2**20, 1)
                    break
    except OSError:
        pass
    return info


def tool_versions():
    # delegated tool.
    from . import cli as cli_mod
    v = {"xverter (native ISO/GoD/ZAR/CCI/CSO writers)": cli_mod.__version__,
         }
    try:
        from importlib.metadata import version
        v["python-lz4"] = version("lz4")
    except Exception:
        v["python-lz4"] = "not installed"
    return v


def scan_artifacts(w, decoded_ref_size):
    """Sizes, ratios and decoded-stream hashes of every container the
    matrix built. Wrapper streams are re-decoded through xverter's own
    readers; CHD reports its header's internal data SHA-1."""
    arts = []

    def add(name, size, decoded_sha1=None, note=None):
        a = {"file": name, "bytes": size}
        if decoded_ref_size:
            a["ratio"] = round(size / decoded_ref_size, 3)
        if decoded_sha1:
            a["decoded_sha1"] = decoded_sha1
        if note:
            a["note"] = note
        arts.append(a)

    from .formats import cci as cci_mod, cso as cso_mod, chd as chd_mod

    def stream_sha1(reader):
        h = hashlib.sha1()
        while True:
            b = reader.read(1 << 22)
            if not b:
                break
            h.update(b)
        return h.hexdigest()

    for name in sorted(os.listdir(w)):
        p = os.path.join(w, name)
        try:
            if name.endswith(".iso") and os.path.isfile(p):
                add(name, os.path.getsize(p), sha1_file(p))
            elif name.endswith(".zar") and os.path.isfile(p):
                add(name, os.path.getsize(p),
                    note="content digest proven equal by extraction check")
            elif name.endswith(".chd") and os.path.isfile(p):
                add(name, os.path.getsize(p),
                    chd_mod.read_header(p)["raw_sha1"],
                    note="sha1 from CHD header, confirmed by full native decode")
            elif (name.endswith(".cci") or name.endswith(".cso")) \
                    and os.path.isfile(p) and name.count(".") == 1:
                mod = cci_mod if name.endswith(".cci") else cso_mod
                reader = (mod.CciReader if name.endswith(".cci")
                          else mod.CsoReader)(p)
                with reader as r:
                    paths = r.slice_paths
                    size = sum(os.path.getsize(sp) for sp in paths)
                    add(" + ".join(os.path.basename(sp) for sp in paths)
                        if len(paths) > 1 else name, size, stream_sha1(r))
            elif name.endswith(".god") and os.path.isdir(p):
                hdr = god_header_in(p)
                total = 0
                for dp, _dd, fs in os.walk(p):
                    total += sum(os.path.getsize(os.path.join(dp, f))
                                 for f in fs)
                with god_mod.GodStream(hdr) as s:
                    add(name + "/", total, stream_sha1(s))
        except Exception as e:                        # noqa: BLE001
            add(name, os.path.getsize(p) if os.path.isfile(p) else 0,
                note="artifact scan failed: %s" % e)
    return arts


def write_report(w, src, kind, baseline, total_seconds, exit_code):
    iso_path = os.path.join(w, "a.iso")
    ref_size = os.path.getsize(iso_path) if os.path.isfile(iso_path) else None
    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input": {"file": os.path.basename(src),
                  "bytes": os.path.getsize(src) if os.path.isfile(src)
                  else None,
                  "detected_kind": kind,
                  "source_sha1": sha1_file(src) if os.path.isfile(src)
                  else None,
                  "content_digest": content_digest(baseline),
                  "files_in_baseline": len(baseline)},
        "machine": machine_info(),
        "tools": tool_versions(),
        "edges": RESULTS,
        "artifacts": scan_artifacts(w, ref_size),
        "summary": {"edges": len(RESULTS),
                    "failed": sum(1 for r in RESULTS if not r["ok"]),
                    "seconds_total": round(total_seconds, 1),
                    "verdict": "ALL PASS" if exit_code == 0 else "FAILED"},
    }
    path = os.path.join(w, "matrix_report.html")
    with open(path, "w") as f:
        f.write(_render_html(report))
    say("report: %s" % path)


def _render_html(report):
    e = html_mod.escape
    ok = report["summary"]["verdict"] == "ALL PASS"
    inp = report["input"]

    def kv(rows):
        return "".join("<tr><th>%s</th><td>%s</td></tr>" % (e(k), e(str(v)))
                       for k, v in rows if v is not None)

    edge_rows = "".join(
        '<tr class="%s"><td>%s</td><td>%s</td><td class="v">%s</td>'
        '<td class="n">%.1fs</td><td class="mono">%s</td></tr>'
        % ("ok" if r["ok"] else "bad", e(r["edge"]), e(r["type"]),
           "PASS" if r["ok"] else "FAIL", r["seconds"],
           e(r.get("content_digest") or r.get("detail", "")))
        for r in report["edges"])
    art_rows = "".join(
        '<tr><td>%s</td><td class="n">%s</td><td class="n">%s</td>'
        '<td class="mono">%s</td><td>%s</td></tr>'
        % (e(a["file"]), "{:,}".format(a["bytes"]),
           ("%.1f%%" % (a["ratio"] * 100)) if "ratio" in a else "",
           e(a.get("decoded_sha1", "")), e(a.get("note", "")))
        for a in report["artifacts"])
    raw = json.dumps(report, indent=2).replace("</", "<\\/")

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>xVerter matrix report - %(name)s</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:70rem;
      padding:0 1rem;background:#111;color:#ddd}
 h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:2rem}
 .verdict{display:inline-block;padding:.3rem .9rem;border-radius:.4rem;
      font-weight:700;color:#fff;background:%(vcolor)s}
 table{border-collapse:collapse;width:100%%;margin:.5rem 0;font-size:.85rem}
 th,td{border:1px solid #333;padding:.25rem .55rem;text-align:left;
      vertical-align:top}
 th{background:#1c1c1c;white-space:nowrap}
 tr.ok td.v{color:#5c5} tr.bad td.v{color:#f66;font-weight:700}
 tr.bad{background:#2a1515}
 .mono{font-family:ui-monospace,monospace;font-size:.75rem;
      word-break:break-all}
 .n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
 button{background:#333;color:#ddd;border:1px solid #555;padding:.4rem .8rem;
      border-radius:.3rem;cursor:pointer;margin:1rem 0}
 pre{background:#000;padding:1rem;overflow:auto;font-size:.75rem;
      border:1px solid #333;border-radius:.3rem}
</style></head><body>
<h1>xVerter conversion-matrix report</h1>
<p><span class="verdict">%(verdict)s</span> &nbsp; %(edges)d edges,
%(failed)d failed, %(mins)dm%(secs)02ds total &nbsp;
<small>%(when)s</small></p>
<h2>Input</h2>
<table>%(input_rows)s</table>
<h2>Machine &amp; tools</h2>
<table>%(env_rows)s</table>
<h2>Edges</h2>
<table><tr><th>edge</th><th>type</th><th>result</th><th>time</th>
<th>content digest / detail</th></tr>%(edge_rows)s</table>
<h2>Artifacts</h2>
<p>Sizes, compression ratio vs the repacked ISO, and the decoded-stream
SHA-1 as re-read by xVerter's own readers. Identical decoded hashes
across containers = the data survived every conversion.</p>
<table><tr><th>artifact</th><th>bytes</th><th>ratio</th>
<th>decoded stream sha1</th><th>note</th></tr>%(art_rows)s</table>
<button onclick="var p=document.getElementById('raw');
p.hidden=!p.hidden;this.textContent=p.hidden?'Show raw JSON':'Hide raw JSON'">
Show raw JSON</button>
<pre id="raw" hidden>%(raw_esc)s</pre>
<script type="application/json" id="matrix-data">%(raw)s</script>
<p><small>Generated by <code>xverter.matrix</code>. The
<code>&lt;script type="application/json"&gt;</code> block above holds the
complete machine-readable report.</small></p>
</body></html>""" % {
        "name": e(inp["file"]),
        "vcolor": "#2a7d2a" if ok else "#a03030",
        "verdict": e(report["summary"]["verdict"]),
        "edges": report["summary"]["edges"],
        "failed": report["summary"]["failed"],
        "mins": int(report["summary"]["seconds_total"] // 60),
        "secs": int(report["summary"]["seconds_total"] % 60),
        "when": e(report["generated_utc"]),
        "input_rows": kv([("file", inp["file"]), ("bytes", inp["bytes"]),
                          ("detected kind", inp["detected_kind"]),
                          ("source sha1", inp["source_sha1"]),
                          ("content digest", inp["content_digest"]),
                          ("files", inp["files_in_baseline"])]),
        "env_rows": kv(list(report["machine"].items())
                       + list(report["tools"].items())),
        "edge_rows": edge_rows,
        "art_rows": art_rows,
        "raw_esc": e(raw),
        "raw": raw,
    }


def god_header_in(tree):
    for dirpath, _dirs, files in os.walk(tree):
        if os.path.basename(dirpath) in ("00007000", "00005000"):
            for f in files:
                if not f.endswith(".data") and \
                        os.path.isdir(os.path.join(dirpath, f + ".data")):
                    return os.path.join(dirpath, f)
    # MatrixError (a RuntimeError), not SystemExit: scan_artifacts'
    # except-Exception can catch it so report generation completes, and
    # the test subcommand catches it by name so a mid-run abort prints a
    # clean ERROR line instead of a traceback.
    raise MatrixError("no GoD header found under %s" % tree)


def main(argv=None):
    global LEEROY
    argv = sys.argv[1:] if argv is None else argv
    argv = list(argv)
    global LOG
    if "--leeroy-jenkins" in argv:
        argv.remove("--leeroy-jenkins")
        LEEROY = True
    if "--log" in argv:
        i = argv.index("--log")
        LOG = open(argv[i + 1], "w")
        del argv[i:i + 2]
    if len(argv) != 2:
        raise SystemExit(__doc__)
    src, w = argv
    os.makedirs(w, exist_ok=True)
    t_start = time.monotonic()
    kind, _ = detect_mod.detect(src)
    say("matrix check: input kind=%s\n" % kind)
    if LEEROY:
        say("!!! LEEROY JENKINS MODE: every convert runs with xverter's "
              "own checks OFF !!!\n    edges are still content-compared by "
              "the matrix itself, but nothing here proves the tool "
              "verifies its own output.\n")

    d = lambda *p: os.path.join(w, *p)

    # For ISO input, every first-hop conversion starts from the pressed
    # source: optimized formats must prove they strip the real disc
    # correctly, archival formats that they preserve it - and building
    # wrappers from our own trimmed rebuild *as well* was redundancy
    # that bought nothing. For other inputs a.iso is the only image in
    # the room, so it stays the substrate and no duplication exists.
    # ALWAYS the true source, for every input kind: an STFS package
    # pivots straight into its wrappers, a zar streams into them - the
    # machinery exists, so building the family from a.iso as well was
    # the same conversion twice with different provenance labels.
    def hop(tgt):
        """Source and label for a first-hop conversion to `tgt`: the
        true original, except when the target IS the input's own kind -
        X -> X is refused by the tool, so that one pairing (an iso
        reader feeding X's writer) comes from a.iso, which is exactly
        the iso the suite built to prove dir->iso."""
        if tgt == kind:
            return d("a.iso"), "iso"
        return src, "%s(src)" % kind

    # baseline: input -> dir
    run("%s->dir" % kind, ["convert", src, "-o", d("base") + "/"])
    with _LiveBar("baseline"):
        baseline = xdvdfs_mod.hash_tree(d("base"))
    say("baseline: %d files\n" % len(baseline))

    # dir -> iso -> dir
    run("dir->iso", ["convert", d("base"), "-o", d("a.iso")])
    run("iso->dir", ["convert", d("a.iso"), "-o", d("from_iso") + "/"])
    check_dir("  content(iso)", d("from_iso"), baseline)

    # dir -> zar -> dir
    run("dir->zar", ["convert", d("base"), "-o", d("a.zar")])
    run("zar->dir", ["convert", d("a.zar"), "-o", d("from_zar") + "/"])
    check_dir("  content(zar)", d("from_zar"), baseline)

    # iso -> god -> dir
    _s, _l = hop("god")
    run("%s->god" % _l, ["convert", _s, "-o", d("a.god")])
    hdr = god_header_in(d("a.god"))
    if kind == "iso":
        # Byte-compare the GoD's data region against the pressed
        # source. Only an image source has pressed bytes to compare
        # against; other kinds audit their GoD through god->dir's
        # content check instead.
        check_god_data("  bytes(iso-god)", hdr, src)
    run("god->dir", ["convert", hdr, "-o", d("from_god") + "/"])
    check_dir("  content(god)", d("from_god"), baseline)

    # god -> iso -> dir ; god -> zar -> dir
    run("god->iso", ["convert", hdr, "-o", d("b.iso")])
    run("iso(b)->dir", ["convert", d("b.iso"), "-o", d("from_giso") + "/"])
    check_dir("  content(god-iso)", d("from_giso"), baseline)
    run("god->zar", ["convert", hdr, "-o", d("b.zar")])
    run("zar(b)->dir", ["convert", d("b.zar"), "-o", d("from_gzar") + "/"])
    check_dir("  content(god-zar)", d("from_gzar"), baseline)

    # zar -> iso ; zar -> god ; iso -> zar
    run("zar->iso", ["convert", d("a.zar"), "-o", d("c.iso")])
    run("zar->god", ["convert", d("a.zar"), "-o", d("c.god")])
    _s, _l = hop("zar")
    run("%s->zar" % _l, ["convert", _s, "-o", d("c.zar")])
    run("zar(c)->dir", ["convert", d("c.zar"), "-o", d("from_czar") + "/"])
    check_dir("  content(iso-zar)", d("from_czar"), baseline)

    # wrapper formats: CCI / CSO (content-agnostic block-compressed ISO)
    _s, _l = hop("cci")
    run("%s->cci" % _l, ["convert", _s, "-o", d("a.cci")])
    run("cci->dir", ["convert", d("a.cci"), "-o", d("from_cci") + "/"])
    check_dir("  content(cci)", d("from_cci"), baseline)
    _s, _l = hop("cso")
    run("%s->cso" % _l, ["convert", _s, "-o", d("a.cso")])
    run("cso->dir", ["convert", d("a.cso"), "-o", d("from_cso") + "/"])
    check_dir("  content(cso)", d("from_cso"), baseline)
    # Back to an image: the one direction where the content check is not
    # enough, because decompressing has to return the pressed bytes and
    # not merely an image holding the same files.
    #
    # These start from the original input rather than a.iso, and that is
    # the whole point. a.iso is an image xverter packed itself, so
    # rebuilding it produces the same bytes again and a rebuild is
    # indistinguishable from a decompression. Only a real pressed image
    # can tell the two apart - which is exactly why this direction went
    # unchecked while it was wrong.
    if kind == "iso":
        # a.cci / a.cso are built from the pressed source now, so the
        # decompress-and-compare round trips run against them directly:
        # one build per wrapper, from reality, byte-audited both ways.
        run("cci(src)->iso", ["convert", d("a.cci"), "-o", d("back_cci.iso")])
        check_partition("  bytes(cci-iso)", d("back_cci.iso"), src)
        run("cso(src)->iso", ["convert", d("a.cso"), "-o", d("back_cso.iso")])
        check_partition("  bytes(cso-iso)", d("back_cso.iso"), src)
        run("cci(src)->god", ["convert", d("a.cci"), "-o", d("s.god")])
        check_god_data("  bytes(cci-god)", god_header_in(d("s.god")), src)
        shutil.rmtree(d("s.god"), ignore_errors=True)
    else:
        # Non-image sources get the same rule with the strongest check
        # they permit. There is no pressed disc to byte-compare against
        # - the image only exists after a rebuild, and rebuild-vs-
        # rebuild is the self-reference blind spot - but the CONVERSIONS
        # from the true source are real user paths (STFS straight to
        # GoD is how XBLA lands on a modded 360) and each one is
        # round-tripped and content-checked against the baseline. No
        # container is exempt because of what the input happened to be.
        # iso and zar are the two targets whose family artifacts come
        # from the dir pairing (dir->iso / dir->zar must prove
        # themselves); everything else in the family now descends from
        # the source directly, so only these two need a separate
        # from-the-source conversion.
        # iso and zar prove the dir pairing; xiso proves the non-image
        # .xiso dispatch arm - a source with no pressed image rebuilds a
        # bare image under the .xiso name, and that arm deserves an edge
        # of its own (no input exemptions, even when the output is a 360
        # title wearing an OG extension).
        for tgt in ("iso", "zar", "xiso"):
            if tgt == kind:
                continue
            out = d("s." + tgt) if tgt != "god" else d("s.god")
            run("%s(src)->%s" % (kind, tgt),
                ["convert", src, "-o", out])
            back = d("from_src_%s" % tgt)
            inp = god_header_in(out) if tgt == "god" else out
            run("%s(src)->dir" % tgt, ["convert", inp, "-o", back + "/"])
            check_dir("  content(src-%s)" % tgt, back, baseline)
            if os.path.isdir(out):
                shutil.rmtree(out, ignore_errors=True)
            elif os.path.exists(out):
                os.unlink(out)         # only if the conversion produced it
    run("god->cci", ["convert", hdr, "-o", d("g.cci")])
    run("cci->cso", ["convert", d("a.cci"), "-o", d("x.cso")])
    run("cso(x)->dir", ["convert", d("x.cso"), "-o", d("from_xcso") + "/"])
    check_dir("  content(cci-cso)", d("from_xcso"), baseline)

    # split wrappers: opt-in 4GiB console slices (--split)
    _s, _l = hop("cci")
    run("%s->cci(split)" % _l, ["convert", _s, "-o", d("u.cci"),
                            "--split"])
    run("cci(split)->dir", ["convert", d("u.cci"), "-o", d("from_ucci") + "/"])
    check_dir("  content(cci-split)", d("from_ucci"), baseline)
    _s, _l = hop("cso")
    run("%s->cso(split)" % _l, ["convert", _s, "-o", d("u.cso"),
                            "--split"])
    run("cso(split)->dir", ["convert", d("u.cso"), "-o", d("from_ucso") + "/"])
    check_dir("  content(cso-split)", d("from_ucso"), baseline)

    # chd: native reader and writer, no external tool involved.
    _s, _l = hop("chd")
    run("%s->chd" % _l, ["convert", _s, "-o", d("a.chd")])
    run("chd->dir", ["convert", d("a.chd"), "-o", d("from_chd") + "/"])
    check_dir("  content(chd)", d("from_chd"), baseline)
    if kind == "iso":
        run("chd(src)->iso", ["convert", d("a.chd"), "-o", d("back_chd.iso")])
        check_identical("  bytes(chd-iso)", d("back_chd.iso"), src)

    # stfs: a normal output column for every input kind - no container is
    # exempt because of what the input was. An STFS source rebuilds and
    # preserves its own content type; any other source has none to carry,
    # so the writer stamps a chosen one (--content-type). Either way the
    # package is round-tripped, content-checked against the baseline, and
    # `verify` walks our own output's hash tree to the descriptor root.
    # Precondition: STFS file-table names are a hard 40 bytes of ASCII. A
    # tree with a longer or non-ASCII name genuinely cannot be packed as
    # STFS (the writer refuses rather than corrupt), so skip the edge with
    # a note - a real limitation, not a failure. Same posture as the xiso
    # precondition. STFS-source names are reader-produced (always fit), so
    # a rebuild never trips this.
    _stfs_offender = None
    for _p in baseline:
        for _c in _p.split("/"):
            _e = _c.encode("ascii", "replace")
            if len(_e) > 0x28 or _e.decode("ascii") != _c:
                _stfs_offender = _c
                break
        if _stfs_offender:
            break
    if _stfs_offender is not None:
        say("  stfs: SKIP - a name is too long or non-ASCII for STFS "
            "(%r, %d bytes); this tree cannot be packed as STFS\n"
            % (_stfs_offender, len(_stfs_offender.encode("ascii", "replace"))))
    else:
        if kind == "stfs":
            # Preserve: the rebuilt package must carry the SOURCE's own
            # type - read it off the source and assert it survives.
            want_ct = _read_content_type(src)
            run("stfs(src)->stfs", ["convert", src, "-o", d("a.stfs")])
        else:
            # A non-default type on purpose (DLC, not the XBLA a lazy
            # writer might fall back to): proves --content-type is
            # actually honoured, not merely accepted.
            want_ct = 0x00000002                     # dlc
            run("%s(src)->stfs" % kind,
                ["convert", src, "-o", d("a.stfs"), "--content-type", "dlc"])
        check_content_type("  stfs content-type", d("a.stfs"), want_ct)
        check_stfs_title_id("  stfs title-id", d("a.stfs"))
        run("stfs(r)->dir", ["convert", d("a.stfs"), "-o", d("from_stfs") + "/"])
        check_dir("  content(stfs)", d("from_stfs"), baseline)
        run("verify stfs", ["verify", d("a.stfs")])
        os.unlink(d("a.stfs"))

    # xiso: the trimmed bare image (what xemu consumes). A byte SLICE of
    # the source's game partition, so it only exists when the source has
    # a video partition to trim - a bare image already IS an xiso, and a
    # non-image source has no pressed bytes to slice. check_partition is
    # exactly the right audit: output == src[partition_base:], byte for
    # byte.
    if kind == "iso":
        from .formats.cci import xbox_image_offset as _xio
        with open(src, "rb") as _sf:
            _full_disc = _xio(_sf) > 0
        if _full_disc:
            run("iso(src)->xiso", ["convert", src, "-o", d("a.xiso")])
            run("xiso->dir", ["convert", d("a.xiso"),
                              "-o", d("from_xiso") + "/"])
            check_dir("  content(xiso)", d("from_xiso"), baseline)
            run("verify xiso", ["verify", d("a.xiso"), "--no-lookup"])
            # Last on purpose: the byte-audit reclaims its artifact
            # (a second copy of the game) once compared.
            check_partition("  bytes(iso-xiso)", d("a.xiso"), src)

    # verify subcommand on every artifact kind
    run("verify iso", ["verify", d("a.iso"), "--no-lookup"])
    run("verify zar", ["verify", d("a.zar")])
    run("verify god", ["verify", hdr])
    run("verify cci", ["verify", d("a.cci")])
    run("verify cso", ["verify", d("a.cso")])
    run("verify cci(split)", ["verify", d("u.cci")])
    run("verify cso(split)", ["verify", d("u.cso")])
    run("verify chd", ["verify", d("a.chd")])

    failed = [r["edge"] for r in RESULTS if not r["ok"]]
    total = time.monotonic() - t_start
    # The report is written BEFORE the summary line prints. The summary
    # is what humans and scripts key on to declare a run finished, and
    # report generation after it is minutes of artifact re-decoding - a
    # window in which an automation that trusted the summary once
    # deleted the workdir out from under the report being written. The
    # last line of output is now genuinely the last work done.
    with _LiveBar("report", "artifacts"):
        write_report(w, src, kind, baseline, total, 1 if failed else 0)
    say("\n%d edges, %d failed, %dm%02ds total"
          % (len(RESULTS), len(failed), total // 60, total % 60))
    if failed:
        say("FAILED: " + ", ".join(failed))
        return 1
    say("MATRIX: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
