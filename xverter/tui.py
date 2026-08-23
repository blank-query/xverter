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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, DataTable, Header, Label,
                             ProgressBar, RichLog, Static, Switch,
                             TabbedContent, TabPane)

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
    ("to-chd", "→ CHD", "chd", "primary"),
    ("to-xiso", "→ XISO", "xiso", "default"),
    ("to-cci", "→ CCI", "cci", "default"),
    ("to-cso", "→ CSO", "cso", "default"),
    ("to-zip", "→ ZIP", "zip", "default"),
    ("to-7z", "→ 7z", "7z", "default"),
    ("to-dir", "→ Folder", "dir", "default"),
]

# The button rows are grouped by console: Xbox 360 formats first,
# Original Xbox formats second (xiso for xemu; CCI/CSO were only ever
# XGD1 containers), general transport/extract third, utilities last.
BUTTON_ROWS = [
    ("to-iso", "to-zar", "to-god", "to-chd"),      # Xbox 360
    ("to-xiso", "to-cci", "to-cso"),               # Original Xbox
    ("to-zip", "to-7z", "to-dir"),                 # transport / extract
]


def _self_cmd(argv):
    """Command line that re-invokes xverter itself, correct in both a
    normal interpreter and a frozen (PyInstaller) binary - where
    sys.executable IS xverter and must not be given "-m xverter"."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + argv
    return [sys.executable, "-m", "xverter"] + argv


class LibraryTable(DataTable):
    """DataTable that reports double-clicks and Enter (navigation) and
    spacebar (batch-select toggle) to the app. Clicks and keys are
    consumed by DataTable itself (they move the cursor and never bubble
    to the App), so all three must hook in at the widget."""

    def on_click(self, event):
        if getattr(event, "chain", 1) >= 2:
            # Capture the row NAME now (the first click of the pair
            # already placed the cursor), then defer the navigation:
            # DataTable's own click handler runs after this one on the
            # same event and would re-apply the pre-navigation row index
            # to the rebuilt table. Deferring by name rather than by
            # cursor also survives a background job's rescan resetting
            # the cursor in the window before the callback runs.
            if self.cursor_row is None or self.row_count == 0:
                return
            name = str(self.get_row_at(self.cursor_row)[1])
            self.app.call_after_refresh(
                lambda n=name: self.app.enter_selected(n))

    def on_key(self, event):
        if event.key == "space":
            self.app.toggle_batch()
            event.stop()
            event.prevent_default()
        elif event.key == "enter":
            self.app.enter_selected()
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
    #side { width: 46; height: auto; margin-bottom: 2; }
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
    .btnrow { height: 3; }
    .btnrow Button {
        width: 1fr;
        min-width: 0;
        height: 3;
        padding: 0;
        margin: 0 1 0 0;
        background: transparent;
        text-style: bold;
    }
    /* rows are console families: 360 / OG Xbox / transport */
    #to-iso, #to-zar, #to-god, #to-chd {
        border: round #2196f3;
        color: #64b5f6;
    }
    #to-iso:hover, #to-zar:hover, #to-god:hover, #to-chd:hover {
        background: #1565c0;
        color: #eaf4fe;
    }
    #to-xiso, #to-cci, #to-cso {
        border: round #12b096;
        color: #3cd0b5;
    }
    #to-xiso:hover, #to-cci:hover, #to-cso:hover {
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
    #progressrow > Static { width: 20; }
    #progressrow ProgressBar { width: 1fr; }
    #progressrow Bar, #batchrow Bar {
        width: 1fr;
        background: $foreground 12%;
    }
    #convert-top { height: auto; }
    .deprow { height: 3; }
    .deprow .depstatus { padding: 1 1 0 0; width: 1fr; }
    .deprow Button { min-width: 14; }
    #depspanel2 {
        border: round $secondary;
        border-title-color: $text;
        padding: 0 1;
        height: auto;
    }
    #updpanel {
        border: round $secondary;
        border-title-color: $text;
        padding: 0 1;
        height: auto;
    }
    #updnote { color: $text-muted; margin: 0 0 1 0; }
    """

    def __init__(self, library):
        super().__init__()
        self.library = os.path.abspath(library)
        self._busy = set()
        self._probe_want = None
        self.ram_scratch = False
        self.split_4gib = False
        self.leeroy = False          # checks are the point; opt out, never in
        self.batch = set()
        self._identify_cache = {}
        # Update buttons are two-step: the first press checks, the
        # second acts on what the check found. None = nothing checked
        # yet, so the next press is a check.
        self._prog_t0 = {}          # count-up clock start, per bar row
        self._prog_stage = {}
        self._prog_timer = {}
        self._app_update = None     # release dict once a newer one exists
        self._dat_update = None     # {system: (data, version)} once fetched

    # ---------------------------------------------------------------- UI

    def compose(self) -> ComposeResult:
        from . import cli as cli_mod
        from . import datcache
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
                                by_id = {b: (lbl, var) for b, lbl, _t, var
                                         in CONVERT_TARGETS}
                                for row in BUTTON_ROWS:
                                    with Horizontal(classes="btnrow"):
                                        for bid in row:
                                            lbl, var = by_id[bid]
                                            yield Button(lbl, id=bid,
                                                         variant=var)
                                with Horizontal(classes="btnrow"):
                                    yield Button("Test", id="run-test",
                                                 variant="warning")
                                    yield Button("Verify", id="do-verify",
                                                 variant="success")
                                    yield Button("Rescan", id="do-rescan")
                                with Horizontal(classes="optrow"):
                                    yield Label("Leeroy Jenkins mode\n"
                                                "(skip every check)")
                                    yield Switch(value=False, id="opt-leeroy")
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
                    with Vertical(id="updpanel"):
                        yield Static(
                            "Nothing here contacts the network until you "
                            "press a button. First press checks, second "
                            "press downloads.", id="updnote")
                        with Horizontal(classes="deprow"):
                            yield Static("xVerter %s" % cli_mod.__version__,
                                         classes="depstatus",
                                         id="upd-st-app")
                            yield Button("xVerter Update", id="upd-app")
                        with Horizontal(classes="deprow"):
                            yield Static("redump database: %s"
                                         % datcache.active_version("xbox360"),
                                         classes="depstatus",
                                         id="upd-st-dat")
                            yield Button("Database Update", id="upd-dat")
                    yield RichLog(id="depslog", wrap=True, markup=False)

    def on_mount(self):
        t = self.query_one("#table", DataTable)
        t.border_title = "library: %s" % self.library
        self._colkeys = t.add_columns(" ", "Name", "Kind", "Size")
        self.query_one("#details", Static).border_title = "details"
        self.query_one("#actions", Vertical).border_title = "convert selected to"
        self.query_one("#log", RichLog).border_title = "log"
        self.query_one("#depslog", RichLog).border_title = "setup log"
        self.query_one("#updpanel", Vertical).border_title = "updates"
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
            # Record what the pane should show. A probe's subprocess
            # keeps running even after Textual "cancels" the worker, so
            # the worker checks this before writing - a slow probe that
            # finishes after the user has moved on must not clobber the
            # newer selection's details.
            self._probe_want = path
            self._probe_gen = getattr(self, "_probe_gen", 0) + 1
            self._probe(path, self._probe_gen)

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

    def enter_selected(self, name=None):
        """Double-click or Enter on the library table: enter the
        selected directory (".." goes up); files are ignored. A caller
        that captured the row name earlier passes it explicitly so a
        concurrent rescan cannot redirect the navigation."""
        if name is None:
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
        elif bid == "upd-app":
            self._update_app()
        elif bid == "upd-dat":
            self._update_dat()
        elif bid == "install-desktop":
            self._install_desktop_entry()
        elif bid == "remove-desktop":
            self._remove_desktop_entry()
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
            # Key _busy on the full path, exactly as the single-file
            # jobs do (do_verify/do_convert/do_matrix_test add `path`),
            # so a batch in flight and a direct click on the same game
            # actually see each other. Bare names never matched a path
            # and let two conversions of one file run at once.
            keys = [os.path.join(self.library, n) for n in names]
            if any(k in self._busy for k in keys):
                self._log("a batch member is busy - wait for it")
                return
            self._log("batch convert -> %s: %d games, alphabetical"
                      % (target, len(names)))
            for k in keys:
                self._busy.add(k)
            # Pin the library HERE, on the UI thread, where the busy
            # keys were just computed - the worker body starts later and
            # self.library may have changed by then.
            self._run_batch(target, names, self.library)
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
        if self.leeroy:
            argv += ["--leeroy-jenkins"]
        if self.ram_scratch:
            argv += ["--scratch", "ram"]
        if self.split_4gib:
            argv += ["--split"]
        self._log("convert %s -> %s ...%s%s%s"
                  % (base, os.path.basename(out.rstrip(os.sep)),
                     " [ram scratch]" if self.ram_scratch else "",
                     " [4GiB split]" if self.split_4gib else "",
                     " [LEEROY JENKINS - NO GUARANTEES]" if self.leeroy else ""))
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

    def _asset_name(self):
        """The release asset that matches this platform, for frozen
        builds. pip installs update through pip, not a download."""
        import platform as _platform
        mach = _platform.machine().lower()
        if sys.platform == "win32":
            return "xverter.exe"
        if sys.platform == "darwin":
            return "xverter-macos-arm64"
        if mach in ("aarch64", "arm64"):
            return "xverter-linux-arm64"
        return "xverter-linux-x86_64"

    @work(thread=True, group="deps")
    def _update_app(self):
        """Press 1: ask GitHub what the newest release is. Press 2:
        download it (frozen builds) and say exactly what to do with it.
        Never runs on its own - the user pressed a button."""
        import json
        import urllib.request
        from . import cli as cli_mod
        if self._app_update is None:
            self.call_from_thread(self._deps_log,
                                  "asking GitHub for the newest xVerter "
                                  "release ...")
            try:
                req = urllib.request.Request(
                    "https://api.github.com/repos/%s/releases/latest"
                    % UPDATE_REPO,
                    headers={"User-Agent": "xverter-update-check",
                             "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    rel = json.load(r)
            except Exception as e:                    # noqa: BLE001
                self.call_from_thread(self._deps_log,
                                      "update check failed: %s" % e)
                self.call_from_thread(self._set_upd_status, "app",
                                      "xVerter %s (check failed - offline?)"
                                      % cli_mod.__version__)
                return
            tag = rel.get("tag_name", "")
            if not _newer(tag, cli_mod.__version__):
                self.call_from_thread(
                    self._set_upd_status, "app",
                    "xVerter %s - newest release" % cli_mod.__version__)
                self.call_from_thread(self._deps_log,
                                      "xVerter %s is the newest release"
                                      % cli_mod.__version__)
                return
            self._app_update = rel
            if not getattr(sys, "frozen", False):
                # pip owns this install; downloading a binary over it
                # would leave two xverters fighting over the same name
                self.call_from_thread(
                    self._set_upd_status, "app",
                    "xVerter %s -> %s available: run `pip install -U "
                    "xverter`" % (cli_mod.__version__, tag))
                self.call_from_thread(self._deps_log,
                                      "UPDATE AVAILABLE: %s - this is a pip "
                                      "install, so update with: pip install "
                                      "-U xverter" % tag)
                self._app_update = None
                return
            self.call_from_thread(
                self._set_upd_status, "app",
                "xVerter %s -> %s available" % (cli_mod.__version__, tag))
            self.call_from_thread(self._set_btn_label, "#upd-app",
                                  "Download %s" % tag)
            self.call_from_thread(self._deps_log,
                                  "UPDATE AVAILABLE: %s (you have %s) - "
                                  "press the button again to download it"
                                  % (tag, cli_mod.__version__))
            return

        # second press: download the asset next to the running binary
        rel = self._app_update
        tag = rel.get("tag_name", "")
        want = self._asset_name()
        url = next((a.get("browser_download_url")
                    for a in rel.get("assets", [])
                    if a.get("name") == want), None)
        if not url:
            self.call_from_thread(
                self._deps_log,
                "release %s has no %s asset - download it yourself: "
                "https://github.com/%s/releases" % (tag, want, UPDATE_REPO))
            return
        # Stage it beside the running binary, tagged so it cannot
        # collide, and keep any extension last so Windows still sees an
        # .exe. The rename target is whatever the user actually named
        # the program, not the release asset's name.
        running = os.path.abspath(sys.executable)
        root, ext = os.path.splitext(want)
        dest = os.path.join(os.path.dirname(running),
                            "%s-%s%s" % (root, tag, ext))
        self.call_from_thread(self._deps_log, "downloading %s ..." % want)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "xverter-update-check"})
            with urllib.request.urlopen(req, timeout=120) as r, \
                    open(dest, "wb") as o:
                shutil.copyfileobj(r, o)
            os.chmod(dest, os.stat(dest).st_mode | 0o111)
        except Exception as e:                        # noqa: BLE001
            self.call_from_thread(self._deps_log,
                                  "download failed: %s" % e)
            return
        self._app_update = None
        self.call_from_thread(self._set_btn_label, "#upd-app",
                              "xVerter Update")
        self.call_from_thread(self._set_upd_status, "app",
                              "xVerter %s downloaded - see the setup log"
                              % tag)
        for line in (
                "downloaded %s" % dest,
                "TO FINISH THE UPDATE, do this yourself - xVerter will not "
                "overwrite a running program:",
                "  1. quit xVerter",
                "  2. delete the old binary: %s" % running,
                "  3. rename %s to %s and launch it"
                % (os.path.basename(dest), os.path.basename(running)),
                "the new version ships a fresh redump database, so a "
                "Database Update right after is usually unnecessary"):
            self.call_from_thread(self._deps_log, line)

    @work(thread=True, group="deps")
    def _update_dat(self):
        """Press 1: download redump's current DAT export and compare it
        with the one in use. Press 2: install what was fetched. The
        point of this button is longevity - the database stays
        refreshable even if xVerter itself stops being released."""
        from . import datcache
        if self._dat_update is None:
            self.call_from_thread(self._deps_log,
                                  "fetching the current redump DAT export "
                                  "...")
            fetched, newer = {}, []
            for system in sorted(datcache.SYSTEMS):
                try:
                    data, version = datcache.fetch(system)
                except Exception as e:                # noqa: BLE001
                    self.call_from_thread(self._deps_log,
                                          "%s: download failed: %s"
                                          % (system, e))
                    return
                have = datcache.active_version(system)
                self.call_from_thread(self._deps_log,
                                      "%s: have %s, redump has %s"
                                      % (system, have, version))
                # Only ever move forward. The site has served an export
                # older than the bundled DAT before now, and installing
                # that would lose entries.
                if datcache.is_newer(version, have):
                    fetched[system] = (data, version)
                    newer.append(system)
            if not newer:
                self.call_from_thread(
                    self._set_upd_status, "dat",
                    "redump database: %s - current"
                    % datcache.active_version("xbox360"))
                self.call_from_thread(self._deps_log,
                                      "nothing newer than what you already "
                                      "have - nothing downloaded into place")
                return
            self._dat_update = fetched
            self.call_from_thread(self._set_btn_label, "#upd-dat",
                                  "Install database")
            self.call_from_thread(
                self._set_upd_status, "dat",
                "redump database: update ready (%s)" % ", ".join(newer))
            self.call_from_thread(self._deps_log,
                                  "newer database available for %s - press "
                                  "the button again to install it"
                                  % ", ".join(newer))
            return

        for system, (data, version) in sorted(self._dat_update.items()):
            try:
                path = datcache.save(system, data)
            except Exception as e:                    # noqa: BLE001
                self.call_from_thread(self._deps_log,
                                      "%s: could not save: %s" % (system, e))
                return
            self.call_from_thread(self._deps_log,
                                  "%s: installed version %s -> %s"
                                  % (system, version, path))
        self._dat_update = None
        self._identify_cache.clear()
        self.call_from_thread(self._set_btn_label, "#upd-dat",
                              "Database Update")
        self.call_from_thread(self._set_upd_status, "dat",
                              "redump database: %s"
                              % datcache.active_version("xbox360"))
        self.call_from_thread(self._deps_log,
                              "database reloaded - authentication now uses "
                              "the new DAT")

    def _set_upd_status(self, which, text):
        self.query_one("#upd-st-%s" % which, Static).update(text)

    def _set_btn_label(self, selector, label):
        self.query_one(selector, Button).label = label

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
        self._prog_stage[pre] = stage
        try:
            self.query_one("#%sprogstage" % pre, Static).update(
                self._stage_text(pre))
            bar = self.query_one("#%sprogbar" % pre, ProgressBar)
        except Exception:
            return                      # screen tearing down
        bar.total = max(total, 1)
        bar.progress = min(done, total)

    def _stage_text(self, pre):
        stage = self._prog_stage.get(pre, "idle")
        t0 = self._prog_t0.get(pre)
        if t0 is None or stage == "idle":
            return stage
        return "%s %ds" % (stage, int(time.monotonic() - t0))

    def _prog_clock_start(self, test=False):
        """Start (or restart) the count-up clock for a run. A ticker
        repaints the stage label once a second so long stages show a
        moving clock - the same liveness contract the terminal bars
        keep, in the TUI's own widgets."""
        pre = "test" if test else ""
        self._prog_t0[pre] = time.monotonic()
        if self._prog_timer.get(pre) is None:
            def tick():
                try:
                    self.query_one("#%sprogstage" % pre, Static).update(
                        self._stage_text(pre))
                except Exception:
                    pass
            self._prog_timer[pre] = self.set_interval(1.0, tick)

    def _prog_clock_stop(self, test=False):
        pre = "test" if test else ""
        self._prog_t0[pre] = None
        self._prog_stage[pre] = "idle"
        try:
            self.query_one("#%sprogstage" % pre, Static).update("idle")
        except Exception:
            pass

    # ------------------------------------------------------------ workers

    @work(thread=True, exclusive=True, group="probe")
    def _probe(self, path, gen=0):
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
        if (getattr(self, "_probe_want", path) != path
                or getattr(self, "_probe_gen", gen) != gen):
            # A newer selection - or a newer probe of the SAME path -
            # owns the pane now (path equality alone let a stale worker
            # from an A-B-A bounce write over its fresher twin).
            return
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
        after the pane is already useful, and results are cached. One
        in-flight hash per path: arrow-key bouncing must not stack a
        second full-image pass behind the first."""
        inflight = getattr(self, "_identify_inflight", None)
        if inflight is None:
            inflight = self._identify_inflight = set()
        if path in inflight:
            return
        inflight.add(path)
        try:
            r = subprocess.run(_self_cmd(["info", path, "--identify"]),
                               capture_output=True, text=True,
                               timeout=600)
            line = next((ln for ln in r.stdout.splitlines()
                         if ln.startswith("redump :")), None)
        except Exception:                             # noqa: BLE001
            line = None
        finally:
            inflight.discard(path)
        if line:
            self._identify_cache[path] = line
            self.call_from_thread(self._append_details, path, line)

    def _stream_cmd(self, argv):
        """Run a CLI invocation, feeding PROGRESS lines to the stage
        bar. Returns (rc, tail_lines). Called from worker threads."""
        if argv[0] in ("convert", "verify"):
            argv = argv + ["--progress"]
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.call_from_thread(self._prog_clock_start)
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
        rc = p.wait()
        self.call_from_thread(self._prog_clock_stop)
        return rc, tail

    def _out_path_for(self, path, target):
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0] if os.path.isfile(path) else base
        if target == "dir":
            return os.path.join(self.library, stem + "_extracted") + os.sep
        return os.path.join(self.library, stem + "." + target)

    @work(thread=True, group="jobs")
    def _run_batch(self, target, names, lib):
        # `lib` is the library pinned at dispatch time - the same value
        # the busy keys were built from - so navigating elsewhere before
        # or during the batch neither redirects a member nor leaks a
        # busy key.
        done_ok = skipped = failed = 0
        try:
            m = len(names)
            for i, name in enumerate(names):
                self.call_from_thread(self._set_batch_progress,
                                      "%d/%d %s" % (i + 1, m, name),
                                      i, m)
                path = os.path.join(lib, name)
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
                if target == "xiso" and kind == "iso":
                    # A bare image IS an xiso; only a full redump has a
                    # video partition to trim. Skip, never fail.
                    try:
                        from .formats import xdvdfs as _xd
                        with open(path, "rb") as _f:
                            if _xd.find_base(_f) == 0:
                                skipped += 1
                                self.call_from_thread(
                                    self._log,
                                    "SKIP %s: already a bare xiso" % name)
                                continue
                    except Exception:                 # noqa: BLE001
                        pass                          # let convert decide
                out = self._out_path_for(path, target)
                if os.path.exists(out.rstrip(os.sep)):
                    skipped += 1
                    self.call_from_thread(
                        self._log, "SKIP %s: output exists" % name)
                    continue
                argv = ["convert", path, "-o", out]
                if self.leeroy:
                    argv += ["--leeroy-jenkins"]
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
                self._busy.discard(os.path.join(lib, name))

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
            self.call_from_thread(self._prog_clock_start, True)
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
                    # The edge count varies by input kind (60 for disc
                    # images, fewer where a check has no meaning), so
                    # the batch bar counts up rather than pretending to
                    # know the total.
                    self.call_from_thread(self._set_batch_progress,
                                          "edge %d" % edges,
                                          edges, max(edges, 1))
            rc = p.wait()
            self.call_from_thread(self._prog_clock_stop, True)
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
