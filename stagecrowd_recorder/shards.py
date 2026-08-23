"""Reading the shards the downloader leaves on disk.

Three consumers ask about shards — the live progress watcher, the periodic
coverage re-check, and the offline salvage — and they must agree on what counts
as one, so the identity rules live here and nowhere else.

Two facts about the layout drive everything below.

The shards do not land under the output directory. The downloader writes them
into a directory named after the run, as a *sibling* of the working directory,
one subdirectory per track. Nothing about the directory tree reveals the link
back to the run that produced them, which is why it gets written down.

Each shard exists on disk twice in succession: the downloader writes the
ciphertext, the decryptor produces a ``_dec`` copy beside it and deletes the
original. Counting files therefore double-counts arrivals, and — more
dangerously — the plain file is ciphertext while a ``_dec`` sibling exists
anywhere in that directory. Publishing it to a player produces a stream that
parses and decodes to noise.
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import shared

VIDEO = "video"
AUDIO = "audio"
SUBTITLE = "subtitle"

MEDIA_SUFFIXES = (".ts", ".m4s", ".mp4", ".m4a", ".aac", ".vtt")
UNFINISHED_SUFFIX = ".tmp"
DECRYPTED_MARK = "_dec"
INIT_NAMES = ("_init.mp4", "init.mp4")

DEFAULT_STEP_MS = 6000
_DURATION_WINDOW = 12
_AMBIGUOUS_BELOW = 3
_GEOMETRY_TOLERANCE = 0.25

# ISO/IEC 14496-12 handler types.
_HANDLERS = {
    b"vide": VIDEO,
    b"soun": AUDIO,
    b"text": SUBTITLE,
    b"subt": SUBTITLE,
    b"sbtl": SUBTITLE,
}


def shard_root(run_name: str, working_dir: Path | None = None) -> Path:
    """Where the downloader puts shards: a sibling of the working directory."""
    return (working_dir or Path.cwd()) / run_name


def sequence_of(path: Path) -> str:
    """The media timestamp naming a shard, with the ``_dec`` mark removed."""
    return path.stem.removesuffix(DECRYPTED_MARK)


def is_decrypted(path: Path) -> bool:
    return path.stem.endswith(DECRYPTED_MARK)


def identity(path: Path) -> tuple[str, str]:
    """What makes two files the same shard: its track and its timestamp.

    Not the filename. The ciphertext and its ``_dec`` twin are the same arrival
    a second apart, and keying on the name reports it twice.
    """
    return (path.parent.name, sequence_of(path))


def classify_track(directory: Path) -> str:
    """Video, audio or subtitle, read from the init segment when possible.

    The directory name is a poor witness and is only the fallback. The
    downloader names directories from the CODECS attribute, which lists audio
    first, so a *video* directory is called ``0__mp4a.40.2_5640800_``: it is one
    substring away from claiming to be the audio track.
    """
    for name in INIT_NAMES:
        init = directory / name
        if not init.is_file():
            continue
        try:
            head = init.read_bytes()[:4096]
        except OSError:
            break
        at = head.find(b"hdlr")
        if at == -1:
            break
        # size(4) 'hdlr'(4) version+flags(4) pre_defined(4) handler_type(4)
        handler = head[at + 12 : at + 16]
        if handler in _HANDLERS:
            return _HANDLERS[handler]
        break
    return AUDIO if "audio" in directory.name.lower() else VIDEO


def _deltas_in_order(stamps: list[int]) -> list[int]:
    ordered = sorted(set(stamps))
    return [b - a for a, b in zip(ordered, ordered[1:]) if b > a]


def _above_floor(deltas: list[int], ratio: float = 0.5) -> list[int]:
    """Drop deltas far below the lower median.

    A restarted or repeated shard puts two timestamps close together. Left in,
    that one short interval becomes the assumed period, and every normal
    interval afterwards looks like dozens of missing shards.
    """
    ordered = sorted(deltas)
    lower_median = ordered[(len(ordered) - 1) // 2]
    floor = lower_median * ratio
    return [d for d in ordered if d >= floor]


def infer_step_ms(stamps: list[int], hint: int | None = None) -> int:
    """The period to use for gap arithmetic: the smallest defensible one.

    Smallest, because a missing shard *widens* an interval, and a period taken
    from a widened interval swallows the gap it was supposed to expose. Floored
    first, because a duplicate shard *narrows* one, and an unfloored minimum
    adopts that as the period.
    """
    deltas = _deltas_in_order(stamps)
    if not deltas:
        return hint or DEFAULT_STEP_MS
    if hint and len(deltas) < _AMBIGUOUS_BELOW:
        # Below three intervals the timestamps genuinely cannot distinguish
        # "short period with a gap" from "long period with a restart artefact";
        # both readings fit. The manifest's declared duration breaks the tie and
        # has no effect once there are enough samples.
        return min(deltas, key=lambda d: abs(d - hint))
    return min(_above_floor(deltas))


def infer_duration_ms(stamps: list[int], hint: int | None = None) -> int:
    """The period to use for playback timing: the typical one.

    The mirror image of the above. This number lands in ``#EXTINF`` and
    ``#EXT-X-TARGETDURATION``, so taking a minimum here would declare a
    six-second stream to be a tenth of a second long and tell every player to
    poll twelve times faster than the stream produces data.
    """
    deltas = _deltas_in_order(stamps)
    if not deltas:
        return hint or DEFAULT_STEP_MS
    recent = deltas[-_DURATION_WINDOW:]
    if hint and len(recent) < _AMBIGUOUS_BELOW:
        return min(recent, key=lambda d: abs(d - hint))
    return int(statistics.median(_above_floor(recent)))


def find_gaps(stamps: list[int], *, settle: int = 0, hint: int | None = None) -> list[int]:
    """Timestamps missing from the middle of the sequence.

    A gap is a property of the whole set, never of one arrival. The downloader
    fetches shards concurrently and they land out of order routinely, so
    comparing each arrival against the previous one manufactures pairs of false
    positives — and never retracts them once the "missing" shards show up.
    """
    ordered = sorted(set(stamps))
    if len(ordered) < 2:
        return []
    step = infer_step_ms(ordered, hint)
    if step <= 0:
        return []
    horizon = ordered[-1] - settle * step

    missing: list[int] = []
    for a, b in zip(ordered, ordered[1:]):
        span = b - a
        steps = round(span / step)
        if steps < 2:
            continue
        if abs(span - steps * step) > step * _GEOMETRY_TOLERANCE:
            # The spacing is not a whole number of periods, which usually means
            # the segment length changed. Claiming a gap here would fire on
            # every shard of a stream that varies its duration.
            continue
        missing.extend(a + n * step for n in range(1, steps) if a + n * step < horizon)
    return missing


@dataclass(frozen=True, slots=True)
class TrackShards:
    """The publishable state of one track directory."""

    path: Path
    kind: str
    init: Path | None
    shards: tuple[Path, ...]
    unfinished: tuple[Path, ...]

    @property
    def name(self) -> str:
        return self.path.name

    def stamps(self) -> list[int]:
        return sorted(int(s) for shard in self.shards if (s := sequence_of(shard)).isdigit())

    def duration_seconds(self, hint_ms: int | None = None) -> float:
        stamps = self.stamps()
        if len(stamps) < 2:
            return (hint_ms or DEFAULT_STEP_MS) / 1000.0
        return max(0.5, infer_duration_ms(stamps, hint_ms) / 1000.0)

    def gaps(self, hint_ms: int | None = None) -> list[int]:
        return find_gaps(self.stamps(), hint=hint_ms)

    def covered_seconds(self, hint_ms: int | None = None) -> float:
        """Media actually held — count times duration, holes excluded."""
        return len(self.shards) * self.duration_seconds(hint_ms)

    def span_seconds(self, hint_ms: int | None = None) -> float:
        """Wall time straddled, holes included."""
        stamps = self.stamps()
        if not stamps:
            return 0.0
        return (stamps[-1] - stamps[0]) / 1000.0 + self.duration_seconds(hint_ms)


def read_track(directory: Path, *, decrypting: bool | None = None) -> TrackShards:
    """Read one track directory.

    ``decrypting`` must be supplied by whoever knows the run passed a decryption
    engine. Inferring it from "did any ``_dec`` file appear" is exactly wrong in
    the case that matters: when the decryptor is broken no ``_dec`` file ever
    appears, and the inference concludes the ciphertext is content.
    """
    init: Path | None = None
    candidates: list[Path] = []
    unfinished: list[Path] = []
    saw_decrypted = False

    try:
        entries = list(directory.iterdir())
    except OSError:
        return TrackShards(directory, classify_track(directory), None, (), ())

    for entry in entries:
        if not entry.is_file():
            continue
        if entry.name in INIT_NAMES:
            init = entry
            continue
        if entry.name.endswith(UNFINISHED_SUFFIX):
            unfinished.append(entry)
            continue
        if entry.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        try:
            if entry.stat().st_size == 0:
                continue  # still being written
        except OSError:
            continue
        if is_decrypted(entry):
            saw_decrypted = True
        candidates.append(entry)

    publish_only_decrypted = saw_decrypted if decrypting is None else decrypting
    if publish_only_decrypted:
        # Directory-level, not pairwise: once a _dec file has appeared here, a
        # plain file is either ciphertext or a shard whose decryption has not
        # happened yet. Neither is content.
        shards = [s for s in candidates if is_decrypted(s)]
    else:
        shards = candidates

    shards.sort(key=lambda p: (len(sequence_of(p)), sequence_of(p)))
    unfinished.sort(key=lambda p: p.name)
    return TrackShards(
        path=directory,
        kind=classify_track(directory),
        init=init,
        shards=tuple(shards),
        unfinished=tuple(unfinished),
    )


def discover_tracks(root: Path, *, decrypting: bool | None = None) -> tuple[TrackShards, ...]:
    """Every track directory beneath the shard root that holds a shard."""
    if not root.is_dir():
        return ()
    tracks = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        track = read_track(directory, decrypting=decrypting)
        if track.shards:
            tracks.append(track)
    return tuple(tracks)


class GapLedger:
    """Gap announcements, made once each.

    Declaring a shard lost is a one-way door, so a gap is only announced after
    it has stayed missing for a grace period; before that it is indistinguishable
    from a shard still in flight.
    """

    SETTLE = 3

    def __init__(self, hint_ms: int | None = None) -> None:
        self._hint = hint_ms
        self._announced: set[tuple[str, int]] = set()

    def unreported(self, track: str, stamps: list[int], *, settle: int | None = None) -> list[int]:
        grace = self.SETTLE if settle is None else settle
        fresh = []
        for stamp in find_gaps(stamps, settle=grace, hint=self._hint):
            marker = (track, stamp)
            if marker in self._announced:
                continue
            self._announced.add(marker)
            fresh.append(stamp)
        return fresh


@dataclass
class TrackTally:
    count: int = 0
    total_bytes: int = 0
    stamps: list[int] = field(default_factory=list)


class ShardWatcher:
    """Reports each shard as it lands, per track, on a background thread.

    The downloader logs no per-shard success at any level — only failures and
    retries — and its progress display is a redrawn region a caller cannot read.
    The shard appearing on disk *is* the success event, so this watches the
    filesystem instead of parsing anything.
    """

    POLL = 0.5

    def __init__(
        self,
        root: Path,
        *,
        log_dir: Path | None = None,
        mirror_root: Path | None = None,
        echo=None,
        hint_ms: int | None = None,
        decrypting: bool | None = None,
    ) -> None:
        self.root = root
        self.log_dir = log_dir
        self.mirror_root = mirror_root
        self.echo = echo
        self.decrypting = decrypting
        self.tallies: dict[str, TrackTally] = {}
        self._seen: set[tuple[str, str]] = set()
        self._ledger = GapLedger(hint_ms)
        self._logs: dict[str, object] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="shard-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.sweep()
        for handle in self._logs.values():
            try:
                handle.close()  # type: ignore[attr-defined]
            except OSError:
                pass
        self._logs.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:
                # Broad on purpose. A narrower clause misses exactly the cases
                # that occur — a console that cannot encode a character, an
                # unexpected filesystem state — and losing this thread loses the
                # per-shard log, gap detection and the running totals, with the
                # traceback scrolled away by the downloader's progress bar
                # seconds later.
                self._failures += 1
                if self._failures in (1, 20):
                    self._say(f"shard watch hiccup ({exc})")
            self._stop.wait(self.POLL)

    # -- scanning --------------------------------------------------------

    def sweep(self) -> None:
        if not self.root.is_dir():
            return
        if self.mirror_root is not None:
            shared.mirror_metadata(self.root, self.mirror_root)
        arrivals: list[tuple[Path, str]] = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            track = read_track(directory, decrypting=self.decrypting)
            if self.mirror_root is not None and track.init is not None:
                shared.mirror_file(
                    track.init,
                    self.mirror_root / directory.name / track.init.name,
                )
            for shard in track.shards:
                marker = identity(shard)
                if marker in self._seen:
                    continue
                if self.mirror_root is not None:
                    shared.mirror_file(
                        shard,
                        self.mirror_root / directory.name / shard.name,
                    )
                self._seen.add(marker)
                arrivals.append((shard, track.kind))

        arrivals.sort(key=lambda pair: (sequence_of(pair[0]), not is_decrypted(pair[0])))
        for shard, kind in arrivals:
            self._record(shard, kind)
        self._report_gaps()

    def _record(self, shard: Path, kind: str) -> None:
        try:
            size = shard.stat().st_size
        except OSError:
            size = 0
        tally = self.tallies.setdefault(kind, TrackTally())
        tally.count += 1
        tally.total_bytes += size
        sequence = sequence_of(shard)
        if sequence.isdigit():
            tally.stamps.append(int(sequence))

        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {sequence:>14s}  {size / 1024:9.1f} KB"
        self._write_log(kind, line)
        if self.echo:
            self.echo(f"[{stamp}] {kind:<8s} {sequence:>14s}  {size / 1024:9.1f} KB")

    def _report_gaps(self) -> None:
        for kind, tally in self.tallies.items():
            for stamp in self._ledger.unreported(kind, tally.stamps):
                self._say(f"{kind}: shard {stamp} never arrived")

    # -- output ----------------------------------------------------------

    def _write_log(self, kind: str, line: str) -> None:
        if self.log_dir is None:
            return
        handle = self._logs.get(kind)
        if handle is None:
            # One file per track. Video and audio arrive interleaved and differ
            # by roughly fortyfold in size; merged, neither is legible and the
            # question worth asking — is each track still advancing — is hidden.
            path = self.log_dir / f"shards-{kind}.log"
            try:
                handle = path.open("a", encoding="utf-8")
            except OSError:
                self.log_dir = None
                return
            self._logs[kind] = handle
        try:
            handle.write(line + "\n")  # type: ignore[attr-defined]
            handle.flush()  # type: ignore[attr-defined]
        except OSError:
            pass

    def _say(self, message: str) -> None:
        if self.echo:
            self.echo(message)

    # -- summary ---------------------------------------------------------

    def summary(self) -> str:
        if not self.tallies:
            return ""
        parts = []
        for kind in sorted(self.tallies):
            tally = self.tallies[kind]
            parts.append(f"{kind}: {tally.count} shards, {tally.total_bytes / 1_048_576:.1f} MB")
        return " | ".join(parts)


def wait_for_shards(root: Path, *, minimum: int = 1, timeout: float = 60.0) -> bool:
    """Block until some track holds ``minimum`` shards, or give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for track in discover_tracks(root):
            if len(track.shards) >= minimum:
                return True
        time.sleep(0.5)
    return False
