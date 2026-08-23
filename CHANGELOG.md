# Changelog

## Unreleased

**New writer: STFS (`.stfs`) output — write LIVE packages from any input.**
STFS was the one format xVerter read but could not write. Now every input
writes it, held to the same standard as every other writer: the package is
re-read by xVerter's own reader and its entire interleaved SHA-1 hash chain
re-verified block-by-block to the volume descriptor's root before success is
reported. Content is byte-equal to source, proven at all three hash-tree
levels — including a full-scale run on the real 2.34 GB Halo: Spartan Assault
retail package.

**Content type is chosen, never guessed.** A LIVE package carries its type
(XBLA `0x000D0000`, DLC `0x00000002`, title update `0x000B0000`, Xbox Original
`0x00005000`) in its header. An STFS **source** already has one, so
`stfs → stfs` is a faithful rebuild that preserves it. Any **other** source
has none to carry, so you name it with `--content-type
xbla|dlc|title-update|xbox-original` (or a raw value). Without it, a non-STFS
source is a clear error — never a silent XBLA stamp. The junk RSA signature
bytes are the same convention xVerter's GoD output (and iso2god's) already
ships.

**TUI: a content-type picker.** Clicking **→ STFS** on a non-STFS game pops a
modal to choose the type; an STFS source skips it and preserves its own. The
`→ STFS` button joins the Xbox 360 row.

**Matrix: STFS is a normal output column for every input.** Each input now
runs the STFS output family — write, round-trip to a directory, content-check
against the baseline, and `verify` to the descriptor root. Image input **67
edges** (was 62); STFS input **62** (it lacks the five pressed-byte audits an
image earns: `67 − 5 = 62`).

**STFS names over 40 bytes are refused, not truncated.** STFS's file-table
name field is a hard 40 bytes of ASCII; some disc games carry longer asset
names (a real XGD3 shader is `transparent_generic_viewer_centered_m.vsh`,
41 chars). The writer now scans names up front and raises a clear error
naming the offender before writing anything, rather than truncating into a
valid-looking package with corrupted names. The matrix skips the STFS edge
for such a tree, like the xiso slice on a bare image.

**Fixed: the TUI RAM-scratch and 4GiB-split toggles did nothing.** Their
handler sat inside `on_button_pressed` guarded by `event.switch`, which a
button press never carries — so the switches were dead and an unrecognized
button id crashed. Moved to a proper `on_switch_changed`; the toggles now
take effect.

## 1.3.0 — the missing conversion, and three sweeps deeper

**New format: `.xiso` output — the trimmed bare image emulators actually want.**
`xverter convert "Game (redump).iso" -o Game.xiso` was the one conversion an
xemu user needed and the tool refused. The new writer is a byte SLICE, not a
rebuild: XDVDFS offsets are partition-relative, so the bytes from the
partition base to the end of the image are already a complete bare image with
the original pressed layout intact. Every image-bearing source slices
(iso/GoD/CCI/CSO, and CHD via its materialized image); zar/STFS/folder
sources rebuild a bare image under the .xiso name; a bare image is refused —
there is nothing to trim. Verified the only way that counts: readback
SHA-1 against the sliced bytes, structure-checked, byte-audited against the
pressed source in the suite — and boot-tested: a real redump sliced by this
writer boots to the Halo main menu in xemu.

**New interface: `--progress=json`.** Bare `--progress` keeps today's text
protocol byte-identically; `--progress=json` renders the same events as pure
NDJSON on stderr with a closed envelope (progress* → error? → exit). Programs
driving xverter get a parser-stable contract instead of scraping human
output.

**The suite grew again: 62 edges (57 for STFS).** Every first-hop conversion
descends from the true source for every input kind; the xiso slice family is
byte-audited against the pressed disc; the double-build redundancy the old
composition carried is gone. The README explains exactly why STFS runs five
fewer edges (they are byte-audits whose ground truth a download title cannot
provide — fewer possible checks, never less coverage).

**Three bug sweeps, ~30 verified fixes.** Two hostile review passes and a
fuzzing campaign (420 generative round-trips over hostile trees, nearly 300
mutated containers, concurrency hammering, chaos input) found and fixed —
among others: two real data-loss paths (converting a gamedir onto itself
moved the user's files; an output placed inside its own source directory was
deleted by its own verification), an orphaned child conversion on SIGTERM, a
family of hostile-input hangs and allocation bombs across the parsers,
mid-read zip corruption crashing instead of refusing, and Windows
drive-letter path traversal in archive extraction. Interrupts are clean
everywhere: Ctrl-C and SIGTERM leave no partial outputs.

**Portability hardening.** The engine now adapts to interpreters it never
met: zstd builds without multithreading, systems without
`os.copy_file_range`. The full release suite passes on Android/Termux on a
Galaxy Z Fold 4 — all four titles, every edge, zero failures — and the
README carries the phone quartet's numbers.

**TUI.** Enter navigates the library. Buttons regrouped by console family:
Xbox 360 (ISO ZAR GoD CHD), Original Xbox (XISO CCI CSO), transport
(ZIP 7z Folder). Live update buttons fixed (they crashed the TUI since they
were wired to a method that didn't exist — found by the sweep), plus a
handful of race fixes in navigation and batch mode.


## 1.2.1 — hammered

1.2.0 got fuzzed, bombed, and lied to from every direction we could think of. Most attacks
bounced. Three didn't, and they're fixed.

**A bit-rotted ZAR converted "successfully" into corrupted output.** The format carries
exactly one piece of integrity data — a whole-file SHA-256 in the footer — and no conversion
ever checked it: the manifest each conversion verified against was computed from the corrupted
bytes themselves. Self-consistency, mistaken for integrity, again. Every zar-consuming path
now checks the footer hash on a thread while the real work runs, and a damaged archive refuses
to convert. Found by flipping bits: before the fix most flips sailed through; after it,
20 of 20 refuse. (Bit-flipped CHDs and GoDs were already refused — 100 of 100 — because those
formats check themselves and our readers enforce it. CCI and CSO contain no checksums *at
all*, so silent rot there is a format property no reader can fix; the README now says so, and
recommends self-testifying formats for archival storage.)

**A crafted CHD header could freeze the machine.** `logicalbytes` is attacker-controlled and
was allowed to size allocations before anything validated it — a claimed exabyte allocated
terabytes of map. Headers are now held to the format's own physics before any claim sizes
anything: hunks cost map bytes, hunk size is capped at MAME's documented 512K, the map must
fit inside the file, and a metadata chain that loops is named for what it is. Five crafted
attacks, five refusals, under a 2 GB memory ceiling.

**Environment errors printed stack traces.** A read-only output directory — or a full disk —
now gets the one-line error it deserves.

Also verified while hammering, because a clean sweep should say what it swept: every writer is
byte-deterministic across runs (all eight formats, run twice, identical digests); the FLAC
decoder survives 4,000 garbage inputs without a hang or an unexpected exception; the FLAC
encoder round-trips 300 varied PCM hunks exactly; the CHD map codec round-trips 300 random
maps exactly; unicode paths, symlinks, relative paths, empty files and pre-existing outputs
all behave; and a wheel built fresh installs clean and converts on the first try.

## 1.2.0 — the last binary

xVerter no longer needs chdman. CHD — the one format that was delegated to an external tool —
is now read, written and verified by xVerter's own pure-Python code, and "no required external
tools" is finally true without a footnote.

That sentence took a native CHD v5 engine: the compressed hunk map with its RLE-encoded
Huffman tree (whose canonical codes MAME assigns longest-first, the reverse of the usual order
— get that wrong and you decode plausible garbage), fourteen map entry types, LZMA whose
parameters are never stored in the file and must be re-derived exactly the way MAME's encoder
normalises them, and a pure-Python FLAC decoder, because chdman compresses some hunks as bare
FLAC frames and every CHD in the wild has them.

The standard was never "byte-identical to chdman" — it auditions four codecs per hunk and keeps
the smallest, so no independent writer can be. The standard is better: **chdman itself verifies
what xVerter writes and extracts it byte-identical**, and xVerter reads everything chdman
writes. When chdman is installed, the test suite uses it as a differential referee on every
run. The format reference is MAME's own CHD library, which its authors deliberately licensed
BSD-3-Clause so that exactly this kind of independent implementation can exist. Credit where
due: Aaron Giles.

The numbers, on the same machine, same 7.8 GB disc, byte-identical outputs both ways:

| | chdman | xVerter native |
| --- | ---: | ---: |
| create | 53.9s | **34.5s — 1.56x faster** (+0.08% size) |
| extract | 16.8s | **9.7s — 1.73x faster, and verified during, not after** |

Yes: the pure-Python implementation beats the C reference at its own format, both directions.
Not by being cleverer at compression — by refusing to waste work. Four hunks in five on a real
disc are pre-compressed assets, and the most expensive thing a compressor does is fail, so a
cheap triage pass sends hopeless hunks straight to storage instead of letting the match finder
churn on them. LZMA runs in fast mode, which the format cannot even see — the properties byte
carries no encoder-effort settings, a fact confirmed by decoding fast-mode streams with plain
properties. Extraction reads contiguous spans in single syscalls instead of seeking 1.9
million times, and hashes the stream as it decodes, so the answer to "did that extract
correctly" is free by the time the file exists; chdman's extract answers no integrity question
at all.

zstd was auditioned for the writer's codec list and declined on measurement: once triage
stops compressing the incompressible, zstd's famous speed has nothing left to accelerate —
6% on create, nothing on extract, slightly larger files, and a compatibility cost (zstd CHDs
need a recent reader) that this format's whole future-proofing purpose argues against. The
codec is implemented in both directions; the default audition just doesn't use it.

A wrong first draft of the writer took 439 seconds and held most of the image in memory; what
shipped streams everything, holds a rolling window, and got its speed from the same lessons
the rest of this project keeps re-learning — batch small units, never compress the
incompressible, and hand the parallel part to threads that actually run in parallel.

Reading a foreign CHD's FLAC hunks costs about 3x chdman's speed on those hunks — pure Python,
decoding at 0.118 ms per hunk across 24 free-threaded cores, 22.7x the single-core figure.

**And xVerter writes FLAC too.** CHD is an archival format; if the format uses a codec, a
complete implementation writes it — and the CD-type discs on the roadmap (PS1, PS2) are where
FLAC is the difference between 100% and 70% of file size. The encoder is pure Python like
everything else, and it faced the harshest referee available: chdman verifies a CHD whose
every hunk is xVerter FLAC and extracts it byte-identical, meaning libFLAC itself accepts the
frames. Deciding *when* to try FLAC mattered more than writing it: the shipped gate asks "is
this plausibly PCM?" with a microseconds-cheap probe, so a DVD image pays about 5% to find its
handful of genuine audio hunks instead of 10x to find nothing, and audio content passes the
probe everywhere it counts — on pure PCM the audition picks FLAC 8192 hunks out of 8192, at
24% of original size.

A note on what "byte perfect" means here, because it was asked and deserves the straight
answer: a CHD's identity is its internal SHA-1s, by the format's own design — codec choice is
transport, not content. A chdman CHD and an xVerter CHD of the same disc carry **identical**
internal SHA-1s and match the same DATs; their file bytes differ, as chdman's own files do
across chdman versions, which is exactly why the format doesn't define identity by file bytes.
The disc inside comes back bit-perfect either way, and that is the archival promise.

The CHD edges of the test suite now run on every machine, not just the ones with a binary
installed.

And with confidence earned — four consecutive full-suite gates of chdman verifying every CHD
xVerter built from real pressed discs — **chdman is no longer a dependency of anything.** The
tool registry, the version checks, the installer and the Setup-tab panel are gone; the
fallbacks are gone; the two shapes xVerter refuses (parent-delta CHDs, pre-v5 files) are
refused with a pointer at `chdman copy` rather than handed to it. If chdman happens to be on
your machine, the test suite will still use it as an independent referee on the CHD edges —
the reference implementation approving our files is a stronger claim than our own reader
making the same check — but nothing anywhere needs it, offers to install it, or falls back to
it. The tool table in the README is deleted because the column would be empty.

## 1.1.1 — the same bug, three more times

1.1.0 fixed a conversion that rebuilt an image instead of returning it. This release is what
happened when we went looking for the rest of that bug, and then went looking for the kind of
bug that hides from tests entirely.

### Two words worth having

A container is now described as one of two things, and the distinction runs through everything
below:

- **Archival** — holds the entire authenticated disc image, video partition and all. It can
  still match its redump entry.
- **Optimized** — strips the unnecessary. It cannot match redump, by design and forever.

An ISO or a CHD of a full dump is archival. GoD, ZAR and STFS are optimized. Once you convert
to an optimized container the redump match is gone and no later conversion brings it back, so
it is worth knowing which one you are holding.

### The same bug, three more times

`cci → god` fed the GoD writer a rebuilt image, so `iso → god` and `cci → god` produced
different containers from the same disc — both reporting success, because a GoD is verified
against its own hash tree and that tree is computed over whatever bytes the writer was handed.

`zip → iso` and `7z → iso` threw away the image they had just unwrapped and rebuilt it from
its own files. A zip of a full redump is an archival container; handing back a rebuild
silently converts it to an optimized one and ends its redump match. On one profile that was
405,972,992 bytes in and 174,080 bytes out.

**CCI and CSO were archival on two disc generations and optimized on the third.** They hold the
game partition and drop what comes before it — but only ever looked for the original-Xbox
partition base, so on XGD2 and XGD3 they found nothing and kept the whole image. They now find
the partition the way every other reader here does. This is safe because CCI and CSO are XGD1
formats: the reference implementations never wrote them for XGD2 or XGD3, so there was nothing
upstream those outputs could have been identical to, and the XGD1 path is untouched. Older
containers still read correctly.

That one took two attempts, for a reason worth admitting: the partition offset was defined
twice, once in each writer. Fixing one left the other answering the old way, and the two then
compressed different ranges of the same disc. There is one definition now.

### How they were found, since it was not by reading the code

Build every intermediate from one original image, then reach each target format from every
source that can reach it, and require the results to agree. Each of these conversions verified
perfectly against itself; every one of them is obvious the moment two routes to the same place
are put side by side. Where routes legitimately disagree the class boundary says so — an
archive returns the whole disc and a CCI returns the game partition, because that is what each
one holds.

The test suite now asks for bytes rather than files on those edges, and — the part that
mattered — starts from the original disc rather than from an image xVerter packed itself.
Rebuilding our own image reproduces our own image, so from that starting point a rebuild and a
passthrough are indistinguishable. Aimed at the wrong starting point the new checks pass on
the broken code and prove nothing.

### A truncated dump could launder itself

The structure check that exists to catch a short image ran only on raw ISOs. CCI and CSO carry
no integrity of their own, so a container built from a truncated dump decodes perfectly,
round-trips perfectly, and came out stamped "round-trip verified" while missing game data.

Every source that is an image is now checked before anything is written. GoD, ZAR and STFS do
carry their own integrity, but a hash tree proves the storage is intact, not that the image
inside it coheres — a container built faithfully from a truncated dump passes its own checks.

Also: a failed conversion no longer leaves a partial file sitting there wearing the output's
name, and a damaged wrapper reports an error instead of a stack trace.

## 1.1.0 — stop waiting

**The full Halo test suite went from 23m48s to 15m08s. That is 36% faster, with every single
check still running** — and byte-for-byte the same output. Every GoD tree, every zar, every
CCI, CSO and ISO is identical to what 1.0 wrote. If "much faster, verifies exactly as much,
produces exactly the same bytes" sounds suspicious, good; it took a lot of measuring to earn.

| The whole quartet, every check enabled | |
| -------------------------------------- | ------:|
| 1.0, its best | 23m48s |
| **1.1.0, its best** | **15m08s** |

Two things got us there and they multiply: the work itself got faster, and the binaries now
ship on a Python that can actually run it in parallel. If you install with pip and stay on a
stock interpreter you still get 20m05s — a 16% improvement for changing nothing — but the
free-threaded build is where this release lives, and the binaries are already on it.

Here is how that happened, including the parts where we were wrong.

### We had the thread pools tuned backwards

xVerter compresses CCI and CSO in 2 KB blocks and hashes GoD containers in 4 KiB ones. Small
units. We had them spread across sixteen worker threads, on the reasonable theory that more
threads means more speed.

They were slower than four. Sometimes slower than *one*. At that block size the GIL spends
more time handing work between threads than the threads spend doing it, and we had walked
straight past the peak without looking down. Sixteen workers on small tasks ran at 453 MB/s;
four workers on big ones run at 725 MB/s. The GoD hash tree was worse — past two threads it
was slower than not threading at all.

So the pools are now sized from measurement rather than optimism. That alone made compressed
formats about 35% faster before anything clever happened.

### Then we removed the GIL, and everything we had just learned inverted

Python 3.14 ships a free-threaded build with no global interpreter lock. Run the same
measurements on it and the answer flips completely: sixteen workers on small tasks, the exact
configuration that was worst with a GIL, becomes the best by a mile — 2700 MB/s against 453.
Small-block hashing goes from "never thread this" to **6.5x**. Decompression goes from a 2x
*penalty* to a 4x gain.

Every rule of thumb this project had accumulated about threading turned out to be a rule about
the GIL.

So the pools now ask the interpreter which world they are in and configure themselves
accordingly, and **the standalone binaries ship on the free-threaded build**. A real 7.8 GB
conversion in a frozen binary: 25 seconds before, 17 after, identical bytes out. If you
install with pip you choose your own interpreter — the README explains how, and xVerter
mentions it once if you are on the slow one.

### The best trick was not making anything faster

Verifying a conversion means hashing the thing we built and hashing the thing we started
from, then comparing. We had been doing those one after the other. They have nothing to do
with each other, so now they run at the same time.

Then the better version of the same thought: the hash of the *source* does not depend on the
conversion at all. There is no reason to wait until the conversion finishes to start it. So
it now runs **while the file is being converted** — by the time there is an output to check,
the answer is already sitting there. Same for authenticating against redump, which used to be
three seconds of dead time before any work began and now happens alongside the work.

None of this made a single operation faster. It stopped the program standing around waiting
for work that never depended on what it was waiting for. It is the biggest win in the release.

It also would have been very easy to cheat here. The obvious version — hash the source once,
during the build, and skip the second read — is faster still and quietly worse: reading the
source a second time is exactly what catches a bad cable or bad RAM corrupting the first read,
which would otherwise be compressed faithfully and then certified correct. So the second read
stayed. Only the waiting went.

### A conversion that quietly wasn't one

Turning a CCI or CSO back into an ISO was not decompressing it. It was extracting the files
and building a **brand new image** out of them, which is the right thing to do for an archive
and the wrong thing to do for a compressed image. The padding went. The pressed layout went.
What came out held all the same files and was not the same disc — on the smallest test image,
266,053,632 bytes in and 174,080 bytes out.

The nasty part is what that does downstream. You can take a verified, authenticated redump,
compress it, decompress it, and be handed something that no longer matches the dump it came
from and never will again. And it reported success the whole way, because the check it was
passing was "did the files survive" — which was true, and was not the question.

The same thing was true one conversion over: building a **GoD container** from a CCI or CSO
fed the writer a rebuild too, so `iso → god` and `cci → god` produced different containers from
the same disc and both reported success. Both had to, because a GoD is checked against its own
hash tree — and that tree is computed over whatever bytes the writer was handed. Feed it a
rebuild and it will faithfully certify the rebuild. All three routes now agree byte for byte.

It now decompresses, which is what the format means. Out comes exactly what the container
holds: the original file byte for byte when it was made from a bare game partition, and the
game partition when it was made from a full redump image, because the video partition was
never in there to give back.

**The test suite never caught this because it never tried.** Every path it exercised ended in
a directory comparison, and files compare equal across images that are not equal. It now
compares bytes on that edge, and — the part that actually mattered — it starts from the
original image rather than from the one xVerter packed itself. Rebuilding our own image
reproduces our own image, so a rebuild and a decompression look identical from there; only a
real pressed disc can tell them apart. Aimed at the old code the new check fails on all three
profiles and says what was lost. That is the test worth having.

### Cutting out the middleman

Converting anything to anything went through a scratch copy of the whole game on disk. Unpack
3.4 GB, read 3.4 GB straight back, delete it. The archive and image writers now take a list of
files and a way to open each one, so the source feeds them directly.

| | before | after | |
| --- | ---: | ---: | ---: |
| anything → zar | 6.34s | 4.96s | **1.28x** |
| zar → ISO | 5.37s | 4.90s | 1.10x |

The gap between those two rows is the interesting part. Streaming into the ISO writer means
giving up `copy_file_range` — a decompressing stream has no file descriptor to hand the
kernel — and on a copy-on-write filesystem the reflink it surrenders nearly cancels the round
trip it saves. Packing an archive compresses every byte, so it could never have shared blocks
with the source in the first place: nothing to trade away, and the whole round trip is profit.

Correctness rests on one argument, checked rather than asserted: an archive's order and an
image's layout are functions of the names and sizes alone, so where the bytes arrive from
cannot change the output. Packed from a directory and packed from the source, byte-identical,
manifests identical, on the real 7.8 GB image and four times running.

And a small free one: the check that asks whether a file size matches any known dump was
parsing 1.4 MB of XML to answer it, once per process, in every one of the fifty processes a
matrix run spawns. It is now cached against the database's own size and timestamp, so an
update still invalidates it — 0.024s to 0.0001s, 255x, for a question that was never
interesting enough to earn a millisecond.

### Things that were embarrassingly slow

**ZIP output took longer than anything else in the tool** — a hundred seconds for a 7.8 GB
image, worse than CHD — because Python's zipfile deflates with zlib, single-threaded,
painstakingly hunting for matches in game data that was compressed years ago at the factory.
Swapping in Intel's ISA-L deflate makes it **21–28x faster for the same bytes out**. The whole
operation, verification included, went from 1m52s to 27s.

**7z was over-compressing**, spending 69 seconds at the engine's default setting to save 0.13%
over its fastest one. On an already-compressed disc image that is a bad trade. It now takes
21 seconds.

**The ISO packer was copying bytes it did not need to touch.** Building an image is mostly
concatenating files, and Linux has a call that tells the kernel to do that — which on a
copy-on-write filesystem shares the data instead of duplicating it. 2 GB "copied" in 0.12
seconds. The whole packing step dropped from 2.7s to 0.8s. We had been using the *second* best
syscall for this, which we would have known by reading how Python's own standard library
copies files.

### Things we tried that did not work

Because a release note that only lists wins is a sales pitch.

Windowed decoding for CCI/CSO looked obviously right — read a block of blocks, decode them in
a batch, parallelise it — and was slower at every size we tried, on both interpreters.
Extraction reads scattered file extents, so a window decodes megabytes nobody asked for.

Reading zips through the bundled 7-Zip engine: slower than Python. Writing them with it:
slower still. Widening the zar compression pool: no difference at all, because compression was
never the bottleneck there — hashing was. Multi-buffer SIMD hashing: a real technique, aimed
at CPUs without the hardware SHA instructions that yours already has.

And a genuine own goal — three test runs lost to a disk filling up, because our split writers
emit `Name.1.cci` alongside `Name.cci` and the cleanup only ever deleted the name it was
given.

### While we were in there

**xVerter was phoning home.** Three times, in fact: checking GitHub for updates on every
launch, checking for tool updates alongside it, and asking redump.org about any image its
local database did not recognise. None of that was asked for. Startup now makes **zero**
network connections — we instrumented the socket layer and counted, rather than believing
ourselves.

What replaced it is two buttons on the Setup tab, each of which takes two presses: the first
tells you what it found, the second acts on it. One updates xVerter. The other updates the
redump database, and it exists for a specific reason — every release ships a fresh database,
but that only helps while releases keep coming. If this project ever stops, the database stays
refreshable from the source, and the tool keeps being useful.

**And `dat update` was making things worse.** It fetched from redump.org, which had been
serving a two-month-old export, and wrote it over the newer database that ships with xVerter —
which takes precedence once cached. So the command whose entire job is improving
authentication was quietly degrading it. It now prefers the current source, and refuses to
move the database backwards under any circumstances.

### `--no-verify` is now `--leeroy-jenkins`

The old name sounded like a performance option. It is not one. It turns off structure
validation, output verification and redump authentication together, and anything written under
it carries no guarantees whatsoever. The new name is harder to type by accident and much
harder to misread. At least you have chicken.

Relatedly, xVerter now uses three words carefully and never interchangeably: **valid** means
the image's own structures cohere, **verified** means what came out matches what went in, and
**authenticated** means it is the genuine retail disc. Output that used to blur them no longer
does.

### The rest

- A valid image whose executable will not parse now says so, and converts anyway, and warns
  you it will not boot.
- `verify` no longer contacts anyone. A local miss is the whole verdict, and it now tells you
  what that verdict means: this dump may be unknown to the community, and redump would like it.
- `xverter test --leeroy-jenkins` runs the full matrix with the tool's own checks off, so the
  cost of paranoia is a number rather than a feeling. It is 23%.
- The same image is never hashed twice in a row: 3 seconds cold, 0.03 warm.
- `--version` tells you which interpreter you are on, because it matters now.
- Three different files disagreed about what version xVerter was. They no longer do.

### Under the hood, for people who care

ZIP now deflates with `isal` (PSF-2.0), whose wheels include Intel's ISA-L (BSD-3-Clause);
both licenses travel inside every binary and wheel as always. ZIP and 7z output bytes differ
from 1.0 as a result — still ordinary archives any tool reads, marginally larger, and
substantially faster to create. Both are one constant away from the old behaviour if you would
rather have the bytes back.
