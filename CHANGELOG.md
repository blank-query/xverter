# Changelog

## Unreleased

**Everything is faster, nothing changed what it produces.** Every format's output is
byte-for-byte what the previous release wrote — GoD trees, zar archives (still byte-identical
to the reference ZArchive implementation), CCI, CSO, ISOs — and every check that ran before
still runs. Where output *did* change, it is called out under "Things that changed" below.

### Speed

The full conversion matrix on the Halo quartet, 44 edges per game, same machine, same
fixtures, all checks enabled:

| | previous release | this release |
| ------------------------- | ---------------:| ------------:|
| stock CPython 3.14 | ~1428s | **1205s** (1.19x) |
| free-threaded CPython 3.14t | — | **908s** (1.57x) |

*(The previous release's figure is its published 48-edge run with the CHD edges removed, since
chdman's cost is unchanged and external. 768 edges ran across the four validation runs behind
these numbers, with zero failures.)*

Per operation, on a 7.8 GB Halo CE redump:

| Operation | before | after |
| ---------------------------------- | ------:| -----:|
| ZIP output (write + verify) | 111.7s | **26.7s** |
| 7z output | 67.9s | **21.1s** |
| CCI compression | 25.9s | **8.1s** *(free-threaded)* |
| ISO packing | 2.66s | **0.84s** |
| zar unpacking | 2.94s | **1.68s** |
| GoD container build | 3.3s | **2.06s** |
| zar packing | 6.25s | **5.05s** |

Where it came from:

- **Free-threaded Python.** The standalone binaries now ship on CPython 3.14t. xVerter hashes
  4 KiB blocks and LZ4-compresses 2 KB ones, and at that size GIL hand-off costs more than the
  work does — with a GIL, *more* threads are measurably slower. The compression, hashing and
  extraction pools now size themselves from `Py_GIL_DISABLED`, so the right settings are
  chosen automatically on either interpreter. A frozen binary converting a real 7.8 GB redump:
  25s built on 3.14, **17s** built on 3.14t, byte-identical output.
- **Thread pools sized by measurement rather than by hope.** The LZ4 pool was running 16
  workers on 256-block tasks (453 MB/s) where 4 workers on 4096-block tasks reach 725 MB/s —
  it had been configured past its own peak.
- **Work moved off the critical path instead of being made faster.** Verification's two passes
  now overlap; the source digest is computed *during* the build rather than after it; the
  redump identity check runs alongside the conversion instead of in front of it. None of these
  makes any single operation quicker — they stop the program waiting for work that never
  depended on what it was waiting for.
- **ZIP deflates with ISA-L**, 21–28x faster than zlib for the same bytes out.
- **`copy_file_range` for the ISO packer**, which on a copy-on-write filesystem shares extents
  instead of copying them: 2 GB moved at 17.5 GB/s against 1.2 GB/s for `sendfile`.
- **Parallel extraction and unpacking** — two files at a time, each on its own reader.
- **The GoD hash tree** hashes subparts in batches and stops re-seeking per block; `GodStream`
  reads and verifies a whole subpart at a time instead of 204 separate seeks and hashes.
- **An identity cache**, so the same image is never hashed twice: 3.02s cold, 0.03s warm,
  keyed on the bytes (device, inode, size, nanosecond mtime) rather than the path.

### Nothing reaches the network unless you press a button

xVerter used to make three network calls nobody asked for: a GitHub release check and a
tool-version check on every TUI start, and a redump.org hash lookup whenever `verify`'s local
DAT missed. All three are gone. Startup now makes **zero** network calls — verified by
instrumenting both `urlopen` and `socket.connect` and counting.

Two buttons on the Setup tab replace them, each taking two presses — the first checks and
reports, the second acts:

- **xVerter Update** — asks GitHub for the newest release, then downloads it beside the
  running binary and tells you to quit, delete the old one, and launch the new one. (A pip
  install is told to use pip, which owns that install.)
- **Database Update** — installs a newer redump DAT and reloads. This is the longevity hatch:
  releases carry a fresh database, but if releases ever stop, the database stays refreshable
  from source.

### Fixed

- **`dat update` was downgrading the database.** It fetched only from redump.org, which was
  serving a June export (3691 games) while the bundled DAT comes from redump.info (3698, and
  current). Since the cache takes precedence over the bundle at lookup time, updating made
  authentication *worse*. Now .info first with .org as fallback, plus a guard so neither the
  button nor the CLI can ever move the database backwards.
- **`.[tui]` in the binary build was a no-op** — `textual` is a hard dependency and `security`
  is the only extra, so pip warned and carried on, and every shipped binary fell back to
  stdlib XML for DAT parsing instead of `defusedxml`.
- **Three version strings that disagreed** — `xverter/__init__.py` said 1.0.0 while `cli.py`
  and `pyproject.toml` said 1.0.3. The package now holds it, `cli` imports it, and
  `pyproject` reads it dynamically.
- **A valid image with an unparseable executable** is now reported rather than passed over in
  silence: it converts, and says it will not boot.

### Things that changed

- **`--no-verify` is now `--leeroy-jenkins`.** The old name read like a routine optimisation;
  it skips *every* check, and outputs carry no guarantees. `verify --no-lookup` still exists
  and now means what it says — skip authentication and the hashing it needs.
- **One vocabulary throughout.** *valid* (the image's own structures cohere), *verified* (what
  came out matches what went in), *authenticated* (it matches redump's canonical hashes).
  These were previously blurred; the README documents the distinction.
- **`verify` no longer contacts redump.org.** The DATs ship with the tool and `dat update`
  refreshes them, so a local miss is the whole verdict — and it now says what that means: the
  dump may be unknown to the community, and redump would like it.
- **ZIP output bytes differ** (ISA-L deflate rather than zlib). Still ordinary deflate that
  every reader handles, 0.04% larger, and about 1.6x slower to *read* — the trade that buys
  9.7x on writing. Archives are written once and read rarely; `_ISAL_LEVEL` reverses it.
- **7z output bytes differ** (`-mx1` rather than the engine default `-mx5`): 3.45x faster for
  0.13% more bytes on an already-compressed payload. `_SEVENZ_LEVEL` reverses it.
- **New dependency: `isal`** (PSF-2.0), whose wheels statically include Intel ISA-L
  (BSD-3-Clause). Both notices travel in `xverter/data/THIRD_PARTY_NOTICES.txt`.
- **`--version` names the interpreter** — `xverter 1.0.3 (CPython 3.14.7, free-threaded)` —
  because which one you are on is worth knowing, and it is how the release build proves it got
  the interpreter it asked for.
- **On a GIL interpreter the CLI mentions the free-threaded one once per run**, to a terminal
  only. `XVERTER_NO_HINTS=1` silences it; piped and scripted runs never see it.

### Testing

- `xverter test --leeroy-jenkins` runs the matrix with the tool's own checks off. The matrix
  still content-compares every edge, so breakage is still caught — it prices the checks. On
  the Halo quartet they cost 23% (stock) and 26.5% (free-threaded) of a run.
- The conversion matrix runs on every push against hand-written synthetic discs.
