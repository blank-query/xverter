"""xverter TUI - a Textual front end over the converter.

    xverter tui [library-dir]

Fully mouse-driven: click a game in the library, click the button for
the format you want (or the Verify/Test buttons), flip the option
switches. No hotkeys to memorize - it works the same on Linux, Windows
and macOS terminals. Every action runs as a background worker invoking
the same verified CLI paths, so everything the TUI does is exactly as
checked as the command line.

The textual package installs with xverter; bare `xverter` opens this UI.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, DataTable, Header, Label,
                             ProgressBar, RichLog, Static, Switch,
                             TabbedContent, TabPane)

from . import deps as deps_mod
from . import detect as detect_mod

KIND_BADGE = {
    "god": "GoD", "iso": "ISO", "zar": "ZAR",
    "stfs": "STFS", "gamedir": "DIR",
    "cci": "CCI", "cso": "CSO", "chd": "CHD",
}

CONVERT_TARGETS = [
    # (button id, label, output suffix or "dir", button variant)
    ("to-iso", "→ ISO", "iso", "primary"),
    ("to-zar", "→ ZAR", "zar", "primary"),
    ("to-god", "→ GoD", "god", "primary"),
    ("to-cci", "→ CCI", "cci", "default"),
    ("to-cso", "→ CSO", "cso", "default"),
    ("to-chd", "→ CHD", "chd", "default"),
    ("to-dir", "→ Folder", "dir", "default"),
    ("to-7z", "→ 7z", "7z", "default"),
    ("to-zip", "→ ZIP", "zip", "default"),
]


def _self_cmd(argv):
    """Command line that re-invokes xverter itself, correct in both a
    normal interpreter and a frozen (PyInstaller) binary - where
    sys.executable IS xverter and must not be given "-m xverter"."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + argv
    return [sys.executable, "-m", "xverter"] + argv


class LibraryTable(DataTable):
    """DataTable that reports double-clicks (navigation) and spacebar
    (batch-select toggle) to the app. Clicks are consumed by DataTable
    itself (they move the cursor and never bubble to the App), so both
    must hook in at the widget."""

    def on_click(self, event):
        if getattr(event, "chain", 1) >= 2:
            self.app.enter_selected()

    def on_key(self, event):
        if event.key == "space":
            self.app.toggle_batch()
            event.stop()
            event.prevent_default()


def _human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return "%d%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024


UPDATE_REPO = "blank-query/xverter"


def _newer(latest, current):
    """True if version string latest > current (dotted-int compare)."""
    def parse(v):
        try:
            return tuple(int(x) for x in v.strip().lstrip("v").split("."))
        except ValueError:
            return ()
    a, b = parse(latest), parse(current)
    return bool(a) and bool(b) and a > b


class XVerterApp(App):
    TITLE = "xVerter"
    SUB_TITLE = "any format in, any format out, verified"
    CSS = """
    Screen { background: $surface; }

    #leftcol { width: 1fr; height: 100%; }
    #table {
        width: 100%;
        height: 1fr;
        border: round $primary;
        border-title-color: $text;
    }
    #side { width: 42; height: auto; margin-bottom: 2; }
    #details {
        height: auto;
        min-height: 8;
        max-height: 14;
        border: round $primary;
        border-title-color: $text;
        padding: 0 1;
    }
    #actions {
        border: round $secondary;
        border-title-color: $text;
        padding: 0 1;
        height: auto;
    }
    #buttons {
        grid-size: 3;
        grid-gutter: 0 1;
        grid-rows: 3;
        height: auto;
    }
    #buttons Button {
        width: 100%;
        min-width: 0;
        height: 3;
        padding: 0;
        margin: 0;
    }
    #buttons Button {
        background: transparent;
        text-style: bold;
    }
    #to-iso, #to-zar, #to-god {
        border: round #2196f3;
        color: #64b5f6;
    }
    #to-iso:hover, #to-zar:hover, #to-god:hover {
        background: #1565c0;
        color: #eaf4fe;
    }
    #to-cci, #to-cso, #to-chd {
        border: round #12b096;
        color: #3cd0b5;
    }
    #to-cci:hover, #to-cso:hover, #to-chd:hover {
        background: #0d8f7a;
        color: #eafcf8;
    }
    #to-dir, #to-7z, #to-zip {
        border: round #9575e0;
        color: #b39ddb;
    }
    #to-dir:hover, #to-7z:hover, #to-zip:hover {
        background: #6a4fc0;
        color: #f2eefc;
    }
    #do-verify {
        border: round $success;
        color: $success;
    }
    #do-verify:hover { background: $success-darken-1; color: #eafcf0; }
    #run-test {
        border: round $warning;
        color: $warning;
    }
    #run-test:hover { background: $warning-darken-1; color: #1a1205; }
    #do-rescan {
        border: round $panel-lighten-2;
    }
    .optrow { height: 2; }
    .optrow Label { width: 1fr; height: 2; padding: 0; }
    .optrow Switch { height: 1; border: none; padding: 0; }
    #log {
        border: round $secondary;
        border-title-color: $text;
    }
    #log { height: 1fr; min-height: 6; }
    #batchrow { height: 1; display: none; }
    #batchrow > Static { width: 12; }
    #batchrow ProgressBar { width: 1fr; }
    #progressrow { height: 1; }
    #progressrow > Static { width: 12; }
    #progressrow ProgressBar { width: 1fr; }
    #progressrow Bar, #batchrow Bar {
        width: 1fr;
        background: $foreground 12%;
    }
    #convert-top { height: auto; }
    #depspanel {
        border: round $secondary;
        border-title-color: $text;
        padding: 0 1;
        height: auto;
    }
    .deprow { height: 3; }
    .deprow .depstatus { padding: 1 1 0 0; width: 1fr; }
    .deprow Button { min-width: 14; }
    #depsnote { color: $text-muted; margin: 0 0 1 0; }
    #depspanel2 {
        border: round $secondary;
        border-title-color: $text;
        padding: 0 1;
        height: auto;
    }
    """

    def __init__(self, library):
        super().__init__()
        self.library = os.path.abspath(library)
        self._busy = set()
        self.ram_scratch = False
        self.split_4gib = False
        self.leroy = False          # checks are the point; opt out, never in
        self.batch = set()
        self._identify_cache = {}

    # ---------------------------------------------------------------- UI

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            with TabPane("Convert", id="tab-convert"):
                with Vertical():
                    with Horizontal(id="convert-top"):
                        with Vertical(id="leftcol"):
                            yield LibraryTable(id="table",
                                               cursor_type="row")
                            with Horizontal(id="batchrow"):
                                yield Static("", id="batchname")
                                yield ProgressBar(id="batchbar", total=1,
                                                  show_eta=False)
                            with Horizontal(id="progressrow"):
                                yield Static("idle", id="progstage")
                                yield ProgressBar(id="progbar", total=100,
                                                  show_eta=False)
                        with VerticalScroll(id="side"):
                            yield Static("click a game to inspect it",
                                         id="details")
                            with Vertical(id="actions"):
                                with Grid(id="buttons"):
                                    for bid, label, _t, variant in \
                                            CONVERT_TARGETS:
                                        yield Button(label, id=bid,
                                                     variant=variant)
                                    yield Button("Verify", id="do-verify",
                                                 variant="success")
                                    yield Button("Test", id="run-test",
                                                 variant="warning")
                                    yield Button("Rescan", id="do-rescan")
                                with Horizontal(classes="optrow"):
                                    yield Label("Leroy Jenkins mode\n"
                                                "(skip every check)")
                                    yield Switch(value=False, id="opt-leroy")
                                with Horizontal(classes="optrow"):
                                    yield Label("RAM scratch (~2.2x game\n"
                                                "size must be available)")
                                    yield Switch(value=False, id="opt-ram")
                                with Horizontal(classes="optrow"):
                                    yield Label("Split .cci/.cso at 4GiB\n"
                                                "(console storage)")
                                    yield Switch(value=False, id="opt-split")
                    yield RichLog(id="log", wrap=True, markup=False)
            with TabPane("Setup", id="tab-setup"):
                with Vertical():
                    with Vertical(id="depspanel"):
                        yield Static("Checked at startup. Installs go to "
                                     "xverter's own tool folder (%s) - "
                                     "your system is not touched."
                                     % deps_mod.bin_dir(), id="depsnote")
                        for tool in deps_mod.TOOLS:
                            with Horizontal(classes="deprow"):
                                yield Static("%s: checking ..." % tool,
                                             classes="depstatus",
                                             id="dep-st-%s" % tool)
                                yield Button("Install",
                                             id="dep-in-%s" % tool,
                                             disabled=True)
                    if sys.platform.startswith("linux"):
                        with Vertical(id="depspanel2"):
                            with Horizontal(classes="deprow"):
                                yield Static(
                                    "App-menu launcher: adds xVerter to "
                                    "your application list (terminal "
                                    "apps aren't double-clickable on "
                                    "Linux; this is the desktop way in)",
                                    classes="depstatus")
                                yield Button("Install",
                                             id="install-desktop")
                                yield Button("Remove",
                                             id="remove-desktop")
                    yield RichLog(id="depslog", wrap=True, markup=False)

    def on_mount(self):
        t = self.query_one("#table", DataTable)
        t.border_title = "library: %s" % self.library
        self._colkeys = t.add_columns(" ", "Name", "Kind", "Size")
        self.query_one("#details", Static).border_title = "details"
        self.query_one("#actions", Vertical).border_title = "convert selected to"
        self.query_one("#log", RichLog).border_title = "log"
        self.query_one("#depspanel", Vertical).border_title = \
            "external tools"
        self.query_one("#depslog", RichLog).border_title = "setup log"
        from . import cli as cli_mod
        import datetime
        self.sub_title = "v%s — any format in, any format out, verified" \
            % cli_mod.__version__
        stamp = ""
        if getattr(sys, "frozen", False):
            mt = os.path.getmtime(sys.executable)
            stamp = " (binary built %s)" % datetime.datetime.fromtimestamp(
                mt).strftime("%Y-%m-%d %H:%M")
        self._log("xverter %s%s" % (cli_mod.__version__, stamp))
        self.action_rescan()
        self._check_deps()
        self._check_update()

    def action_rescan(self):
        t = self.query_one("#table", DataTable)
        t.clear()
        try:
            names = sorted(os.listdir(self.library), key=str.lower)
        except OSError as e:
            self._log("ERROR: %s" % e)
            return
        self.batch.clear()
        if os.path.dirname(self.library) != self.library:
            t.add_row("", "..", "up", "", key="..")
        for name in names:
            p = os.path.join(self.library, name)
            if os.path.isfile(p):
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                kind = ext if ext in ("zar", "iso", "cci", "cso", "chd",
                                       "zip", "7z") \
                    else "?"
                size = _human(os.path.getsize(p))
            elif os.path.isdir(p):
                kind, size = "dir", ""
            else:
                continue
            t.add_row("", name, kind, size, key=name)
        t.border_title = "library: %s" % self.library
        self._log("scanned %s: %d entries" % (self.library, t.row_count))

    def _selected_path(self):
        t = self.query_one("#table", DataTable)
        if t.cursor_row is None or t.row_count == 0:
            return None
        name = t.get_row_at(t.cursor_row)[1]
        return os.path.join(self.library, name)

    def _log(self, msg):
        self.query_one("#log", RichLog).write(
            "[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    def on_data_table_row_highlighted(self, event):
        path = self._selected_path()
        if path and os.path.basename(path) != "..":
            self._probe(path)

    def toggle_batch(self):
        """Spacebar: toggle the highlighted row in the batch set."""
        t = self.query_one("#table", DataTable)
        if t.cursor_row is None or t.row_count == 0:
            return
        name = t.get_row_at(t.cursor_row)[1]
        if name == "..":
            return
        if name in self.batch:
            self.batch.discard(name)
            mark = ""
        else:
            self.batch.add(name)
            mark = "●"
        t.update_cell(name, self._colkeys[0], mark)
        self._log("batch: %d selected" % len(self.batch))

    def _set_batch_progress(self, label, done, total):
        try:
            row = self.query_one("#batchrow")
            row.display = True
            self.query_one("#batchname", Static).update(label)
            bar = self.query_one("#batchbar", ProgressBar)
        except Exception:
            return                      # screen tearing down
        bar.total = max(total, 1)
        bar.progress = min(done, total)

    def enter_selected(self):
        """Double-click on the library table: enter the selected
        directory (".." goes up); files are ignored."""
        t = self.query_one("#table", DataTable)
        if t.cursor_row is None or t.row_count == 0:
            return
        name = t.get_row_at(t.cursor_row)[1]
        if name == "..":
            self._navigate(os.path.dirname(self.library))
            return
        p = os.path.join(self.library, name)
        if os.path.isdir(p):
            self._navigate(p)

    def _navigate(self, path):
        self.library = os.path.abspath(path)
        self.action_rescan()

    # ------------------------------------------------------------- events

    def on_button_pressed(self, event):
        bid = event.button.id
        targets = {b: t for b, _l, t, _v in CONVERT_TARGETS}
        if bid in targets:
            self.do_convert(targets[bid])
        elif bid == "do-verify":
            self.do_verify()
        elif bid == "do-rescan":
            self.action_rescan()
        elif bid == "run-test":
            self.do_matrix_test()
        elif bid == "install-desktop":
            self._install_desktop_entry()
        elif bid == "remove-desktop":
            self._remove_desktop_entry()
        elif bid and bid.startswith("dep-in-"):
            tool = bid[len("dep-in-"):]
            event.button.disabled = True
            self._install_dep(tool)

    def on_switch_changed(self, event):
        if event.switch.id == "opt-leroy":
            self.leroy = event.value
            if event.value:
                self._log("!!! LEROY JENKINS MODE ON !!!")
                self._log("    structure is NOT validated, output is NOT "
                          "verified, sources are NOT authenticated.")
                self._log("    Anything written from here carries no "
                          "guarantees at all. At least you have chicken.")
            else:
                self._log("checks back ON - structure validated, every "
                          "output re-read and verified, sources "
                          "authenticated against redump")
        elif event.switch.id == "opt-ram":
            self.ram_scratch = event.value
            if event.value:
                self._log("RAM scratch ON - pivot files go to a tmpfs "
                          "(/dev/shm). You need ~2.2x the game's size in "
                          "AVAILABLE ram or conversions fail with 'no "
                          "space left on device'. Linux only; on Windows "
                          "make a RAM drive and use the CLI's --workdir.")
            else:
                self._log("RAM scratch off - pivot files go to the "
                          "system temp dir on disk.")
        elif event.switch.id == "opt-split":
            self.split_4gib = event.value
            if event.value:
                self._log("4GiB split ON - .cci/.cso outputs over 4GiB "
                          "become Name.1/.2 slices, the convention FATX "
                          "console storage requires.")
            else:
                self._log("4GiB split off - .cci/.cso outputs stay one "
                          "file regardless of size. Fine for PC "
                          "emulators; a console FATX drive can't hold "
                          "files past 4GiB.")

    # ------------------------------------------------------------ actions

    def do_convert(self, target):
        if self.batch:
            names = sorted(self.batch, key=str.lower)
            if any(n in self._busy for n in names):
                self._log("a batch member is busy - wait for it")
                return
            self._log("batch convert -> %s: %d games, alphabetical"
                      % (target, len(names)))
            for n in names:
                self._busy.add(n)
            self._run_batch(target, names)
            return
        path = self._selected_path()
        if not path:
            self._log("select a game first (or spacebar-mark several)")
            return
        if os.path.basename(path) == "..":
            self._log("that's the parent-directory row - select a game")
            return
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0] if os.path.isfile(path) else base
        if target == "dir":
            out = os.path.join(self.library, stem + "_extracted") + os.sep
        else:
            out = os.path.join(self.library, stem + "." + target)
        if os.path.exists(out.rstrip(os.sep)):
            self._log("SKIP: output exists: %s"
                      % os.path.basename(out.rstrip(os.sep)))
            return
        if path in self._busy:
            self._log("busy: %s" % base)
            return
        self._busy.add(path)
        argv = ["convert", path, "-o", out]
        if self.leroy:
            argv += ["--leroy-jenkins"]
        if self.ram_scratch:
            argv += ["--scratch", "ram"]
        if self.split_4gib:
            argv += ["--split"]
        self._log("convert %s -> %s ...%s%s%s"
                  % (base, os.path.basename(out.rstrip(os.sep)),
                     " [ram scratch]" if self.ram_scratch else "",
                     " [4GiB split]" if self.split_4gib else "",
                     " [LEROY JENKINS - NO GUARANTEES]" if self.leroy else ""))
        self._run_job(path, argv, "convert %s" % base)

    def do_verify(self):
        path = self._selected_path()
        if not path:
            self._log("select a game first")
            return
        if path in self._busy:
            self._log("busy: %s" % os.path.basename(path))
            return
        self._busy.add(path)
        self._log("verify %s ..." % os.path.basename(path))
        self._run_job(path, ["verify", path],
                      "verify %s" % os.path.basename(path))

    def do_matrix_test(self):
        path = self._selected_path()
        if not path:
            self._log("select a game first")
            return
        if path in self._busy:
            self._log("busy: %s" % os.path.basename(path))
            return
        self._busy.add(path)
        self._log("=== matrix test: %s === (needs ~4-5x the game's size "
                  "free; 10-20 min for a full disc)"
                  % os.path.basename(path))
        self._run_matrix(path)

    # ------------------------------------------------------- dependencies

    def _deps_log(self, msg):
        self.query_one("#depslog", RichLog).write(
            "[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    def _apply_dep_status(self, st):
        tool = st["tool"]
        label = self.query_one("#dep-st-%s" % tool, Static)
        btn = self.query_one("#dep-in-%s" % tool, Button)
        iv, lv = st["installed_version"], st["latest_version"]
        if st["path"]:
            txt = "%s: OK (%s)" % (tool, st["path"])
            if iv:
                txt = "%s: OK v%s" % (tool, iv)
            if iv and lv and iv != lv:
                txt += "  ->  v%s available" % lv
                btn.label = "Update"
                btn.disabled = st["asset"] is None
            else:
                btn.label = "Installed"
                btn.disabled = True
        else:
            txt = "%s: MISSING (needed for %s)" % (tool, st["needed_for"])
            if st["asset"]:
                btn.label = "Install"
                btn.disabled = False
            else:
                txt += " - %s" % st["hint"]
                btn.label = "manual"
                btn.disabled = True
        if st.get("error"):
            txt += "  [update check failed: offline?]"
        label.update(txt)

    @work(thread=True, group="deps")
    def _check_deps(self):
        missing = []
        for st in deps_mod.check_all(online=True):
            self.call_from_thread(self._apply_dep_status, st)
            if not st["path"]:
                missing.append(st["tool"])
        if missing:
            self.call_from_thread(
                self._deps_log,
                "missing: %s - conversions needing them will fail; "
                "one-click installs on this tab where available"
                % ", ".join(missing))
        else:
            self.call_from_thread(self._deps_log,
                                  "all external tools present")

    @work(thread=True, group="deps")
    def _check_update(self):
        """Compare the running version against the newest GitHub
        release. Quiet on any failure - an update nag must never be the
        loudest thing in the room, and never block anything. When an
        update exists, the message names the exact path for THIS
        install: the matching platform asset for a standalone binary,
        or `pip install -U` for a pip install."""
        import json
        import platform as _platform
        import urllib.request
        from . import cli as cli_mod
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/releases/latest"
                % UPDATE_REPO,
                headers={"User-Agent": "xverter-update-check",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                rel = json.load(r)
        except Exception:
            return                      # offline / private / no releases
        tag = rel.get("tag_name", "")
        if not _newer(tag, cli_mod.__version__):
            return
        if getattr(sys, "frozen", False):
            # standalone binary: point at this platform's asset
            mach = _platform.machine().lower()
            if sys.platform == "win32":
                want = "xverter.exe"
            elif sys.platform == "darwin":
                want = "xverter-macos-arm64"
            elif mach in ("aarch64", "arm64"):
                want = "xverter-linux-arm64"
            else:
                want = "xverter-linux-x86_64"
            url = next((a.get("browser_download_url")
                        for a in rel.get("assets", [])
                        if a.get("name") == want), None)
            how = url or ("https://github.com/%s/releases (get %s)"
                          % (UPDATE_REPO, want))
        else:
            how = "pip install -U xverter"
        msg = "UPDATE AVAILABLE: %s (you have %s) -> %s" \
            % (tag, cli_mod.__version__, how)
        self.call_from_thread(self._log, msg)
        self.call_from_thread(self._deps_log, msg)

    @work(thread=True, group="deps")
    def _install_dep(self, tool):
        try:
            deps_mod.install(
                tool,
                log=lambda m: self.call_from_thread(self._deps_log, m))
            st = deps_mod.check(tool, online=True)
        except Exception as e:                        # noqa: BLE001
            self.call_from_thread(self._deps_log,
                                  "%s install FAILED: %s" % (tool, e))
            try:
                st = deps_mod.check(tool, online=False)
            except Exception:                         # noqa: BLE001
                return
        self.call_from_thread(self._apply_dep_status, st)

    def _install_desktop_entry(self):
        """Write ~/.local/share/applications/xverter.desktop launching
        this xverter (frozen binary or installed command) in a detected
        terminal emulator."""
        if getattr(sys, "frozen", False):
            target = sys.executable
        else:
            target = shutil.which("xverter") or \
                "%s -m xverter" % sys.executable
        term_forms = (("ghostty", "-e"), ("alacritty", "-e"),
                      ("kitty", "-e"), ("foot", "-e"),
                      ("wezterm", "start --"), ("gnome-terminal", "--"),
                      ("konsole", "-e"), ("xfce4-terminal", "-e"),
                      ("xterm", "-e"))
        term = next(((t, a) for t, a in term_forms if shutil.which(t)),
                    None)
        if term:
            exec_line = "%s %s %s" % (term[0], term[1], target)
            terminal = "false"
        else:
            exec_line = target
            terminal = "true"
        d = os.path.join(os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            "applications")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "xverter.desktop")
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=xVerter\n"
                    "Comment=Xbox game format converter - any format "
                    "in, any format out, verified\n"
                    "Exec=%s\nTerminal=%s\nCategories=Utility;\n"
                    % (exec_line, terminal))
        subprocess.run(["update-desktop-database", d],
                       capture_output=True)
        self._deps_log("installed %s (%s)" % (
            path, "via " + term[0] if term else "Terminal=true fallback"))

    def _remove_desktop_entry(self):
        d = os.path.join(os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            "applications")
        path = os.path.join(d, "xverter.desktop")
        if os.path.isfile(path):
            os.unlink(path)
            subprocess.run(["update-desktop-database", d],
                           capture_output=True)
            self._deps_log("removed %s" % path)
        else:
            self._deps_log("no launcher entry installed (%s)" % path)

    def _set_progress(self, stage, done, total, test=False):
        pre = "test" if test else ""
        try:
            self.query_one("#%sprogstage" % pre, Static).update(stage)
            bar = self.query_one("#%sprogbar" % pre, ProgressBar)
        except Exception:
            return                      # screen tearing down
        bar.total = max(total, 1)
        bar.progress = min(done, total)

    # ------------------------------------------------------------ workers

    @work(thread=True, exclusive=True, group="probe")
    def _probe(self, path):
        try:
            kind, real = detect_mod.detect(path)
            lines = ["%s  [%s]" % (os.path.basename(path),
                                   KIND_BADGE.get(kind, kind))]
            r = subprocess.run(_self_cmd(["info", path]),
                               capture_output=True, text=True, timeout=120)
            lines += r.stdout.strip().splitlines()[2:9]
        except Exception as e:                        # noqa: BLE001
            kind = None
            lines = [os.path.basename(path), "detect: %s" % e]
        self._details_text = "\n".join(lines)
        self.call_from_thread(
            self.query_one("#details", Static).update, self._details_text)
        if kind == "iso":
            cached = self._identify_cache.get(path)
            if cached:
                self.call_from_thread(self._append_details, path, cached)
            else:
                self._identify(path)

    def _append_details(self, path, extra):
        if self._selected_path() != path:
            return                      # user moved on
        self._details_text += "\n" + extra
        self.query_one("#details", Static).update(self._details_text)

    @work(thread=True, group="identify")
    def _identify(self, path):
        """Hash the ISO against the bundled redump DATs: true name and
        system regardless of filename. Slow on big images, so it runs
        after the pane is already useful, and results are cached."""
        try:
            r = subprocess.run(_self_cmd(["info", path, "--identify"]),
                               capture_output=True, text=True,
                               timeout=600)
            line = next((ln for ln in r.stdout.splitlines()
                         if ln.startswith("redump :")), None)
        except Exception:                             # noqa: BLE001
            line = None
        if line:
            self._identify_cache[path] = line
            self.call_from_thread(self._append_details, path, line)

    def _stream_cmd(self, argv):
        """Run a CLI invocation, feeding PROGRESS lines to the stage
        bar. Returns (rc, tail_lines). Called from worker threads."""
        if argv[0] in ("convert", "verify"):
            argv = argv + ["--progress"]
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        p = subprocess.Popen(_self_cmd(argv), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             bufsize=1, env=env)
        tail = []
        for line in iter(p.stdout.readline, ""):
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("PROGRESS "):
                try:
                    _tag, stage, done, total = line.split()
                    self.call_from_thread(self._set_progress, stage,
                                          int(done), int(total))
                except ValueError:
                    pass
                continue
            tail = (tail + [line])[-3:]
        p.stdout.close()
        return p.wait(), tail

    def _out_path_for(self, path, target):
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0] if os.path.isfile(path) else base
        if target == "dir":
            return os.path.join(self.library, stem + "_extracted") + os.sep
        return os.path.join(self.library, stem + "." + target)

    @work(thread=True, group="jobs")
    def _run_batch(self, target, names):
        done_ok = skipped = failed = 0
        try:
            m = len(names)
            for i, name in enumerate(names):
                self.call_from_thread(self._set_batch_progress,
                                      "%d/%d %s" % (i + 1, m, name),
                                      i, m)
                path = os.path.join(self.library, name)
                # pre-flight: unrecognized files and already-target
                # members are SKIPPED, never failures
                try:
                    kind, _real = detect_mod.detect(path)
                except Exception as e:
                    skipped += 1
                    self.call_from_thread(
                        self._log, "SKIP %s: not a recognized game (%s)"
                        % (name, str(e).splitlines()[0][:60]))
                    continue
                if kind == target or (target == "dir"
                                      and kind == "gamedir"):
                    skipped += 1
                    self.call_from_thread(
                        self._log, "SKIP %s: already %s" % (name, kind))
                    continue
                out = self._out_path_for(path, target)
                if os.path.exists(out.rstrip(os.sep)):
                    skipped += 1
                    self.call_from_thread(
                        self._log, "SKIP %s: output exists" % name)
                    continue
                argv = ["convert", path, "-o", out]
                if self.leroy:
                    argv += ["--leroy-jenkins"]
                if self.ram_scratch:
                    argv += ["--scratch", "ram"]
                if self.split_4gib:
                    argv += ["--split"]
                t0 = time.monotonic()
                rc, tail = self._stream_cmd(argv)
                dt = time.monotonic() - t0
                status = "OK" if rc == 0 else "FAILED"
                if rc == 0:
                    done_ok += 1
                else:
                    failed += 1
                self.call_from_thread(
                    self._log, "[%d/%d] %s -> %s: %s (%.1fs)"
                    % (i + 1, m, name, target, status, dt))
                if rc != 0:
                    for line in tail[-2:]:
                        self.call_from_thread(self._log, "    " + line)
            summary = "batch done: %d OK" % done_ok
            if skipped:
                summary += ", %d skipped" % skipped
            if failed:
                summary += ", %d FAILED" % failed
            self.call_from_thread(self._set_batch_progress, summary, m, m)
            self.call_from_thread(self._set_progress, "done", 1, 1)
            self.call_from_thread(self.action_rescan)
        finally:
            for name in names:
                self._busy.discard(name)

    @work(thread=True, group="jobs")
    def _run_job(self, path, argv, label):
        t0 = time.monotonic()
        try:
            rc, tail = self._stream_cmd(argv)
            dt = time.monotonic() - t0
            status = "OK" if rc == 0 else "FAILED"
            self.call_from_thread(self._set_progress,
                                  "done" if rc == 0 else "failed", 1, 1)
            self.call_from_thread(self._log,
                                  "%s: %s (%.1fs)" % (label, status, dt))
            for line in tail:
                self.call_from_thread(self._log, "    " + line)
            if rc == 0 and argv[0] == "convert":
                self.call_from_thread(self.action_rescan)
        finally:
            self._busy.discard(path)

    @work(thread=True, group="jobs")
    def _run_matrix(self, path):
        workdir = tempfile.mkdtemp(prefix="xverter_matrix_",
                                   dir=self.library)
        try:
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            p = subprocess.Popen(
                _self_cmd(["test", path, "--workdir", workdir]),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env)
            edges = 0
            for line in p.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("PROGRESS "):
                    try:
                        _t, stage, d, tot = line.split()
                        self.call_from_thread(self._set_progress,
                                              stage, int(d), int(tot))
                    except ValueError:
                        pass
                    continue
                self.call_from_thread(self._log, line)
                if re.search(r"\s(PASS|FAIL|SKIP)\s", line):
                    edges += 1
                    self.call_from_thread(self._set_batch_progress,
                                          "edge %d/48" % edges,
                                          edges, 48)
            rc = p.wait()
            report = os.path.join(workdir, "matrix_report.html")
            if os.path.isfile(report):
                stem = os.path.splitext(os.path.basename(path))[0]
                dest = os.path.join(self.library,
                                    stem + "_matrix_report.html")
                shutil.move(report, dest)
                self.call_from_thread(self._log,
                                      "report saved: %s" % dest)
            self.call_from_thread(
                self._log,
                "=== %s ===" % ("ALL PASS" if rc == 0
                                else "FAILED (exit %d)" % rc))
        except Exception as e:                        # noqa: BLE001
            self.call_from_thread(self._log, "ERROR: %s" % e)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self._busy.discard(path)


def main(library="."):
    app = XVerterApp(library)
    app.run()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
