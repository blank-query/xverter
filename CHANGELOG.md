# Changelog

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
