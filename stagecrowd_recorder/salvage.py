"""Rebuilding a playable file from the shards that survived.

This is the tool of last resort, which sets its design rule: a quiet, plausible
answer does the most damage here. So it reports per track — what was written,
what never finished, what is missing from the middle, and how much media each
track actually holds — rather than announcing a single "success".

The reason a rebuild is possible at all: the muxed output is produced through an
ffmpeg pipe and is only as complete as the shutdown was, while each shard is a
file that was either finished or not. A run stopped with SIGTERM leaves a damaged
tail on the muxed file and a directory of intact shards beside it.

The reason a rebuild can still mislead: container duration is taken from the
longest track. An interrupted run that left 6 video shards and 14 audio ones
produces a file that reports the audio length, with video covering a fraction of
it. Hence the per-track accounting.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import records, shards
from .errors import SalvageError
from .shards import AUDIO, VIDEO, TrackShards
from .toolchain import Toolchain

REBUILD_ORDER = (VIDEO, AUDIO)


@dataclass(frozen=True, slots=True)
class ShardLocation:
    root: Path
    record: dict | None
    stale: bool
    decrypting: bool | None
    segment_ms: int


def locate(target: Path) -> ShardLocation:
    """Resolve a shard root from either an output directory or a shard directory.

    Both are accepted because both are plausible things to point at, and they are
    not the same place. When a run record is present its three recorded paths are
    tried in order; the first that exists wins.
    """
    record = records.read_record(target)
    if record is None:
        return ShardLocation(target, None, stale=False, decrypting=None, segment_ms=0)

    # A recorded decryptor means the run decrypted, so a plain shard is
    # ciphertext. That is a fact read back, not re-derived from which files
    # happen to be present.
    decrypting = True if record.get("decryptor") else None
    segment_ms = record.get("segment_ms") or 0

    candidates: list[Path] = []
    absolute = record.get("shard_root")
    if absolute:
        candidates.append(Path(absolute))
    relative = record.get("shard_root_relative")
    if relative:
        candidates.append((target / relative).resolve())
    session = record.get("session_shard_root")
    if session:
        candidates.append(Path(session))
    run_name = record.get("run_name")
    if run_name:
        candidates.append(shards.shard_root(str(run_name)))

    for candidate in candidates:
        if candidate.is_dir():
            return ShardLocation(
                candidate, record, stale=False, decrypting=decrypting, segment_ms=int(segment_ms)
            )

    if candidates:
        listed = "\n  ".join(str(c) for c in candidates)
        raise SalvageError(
            f"{records.RECORD_NAME} points at shard directories that no longer exist:\n  {listed}",
            remedy="Pass the shard directory directly if it was moved.",
        )
    return ShardLocation(target, record, stale=True, decrypting=decrypting, segment_ms=int(segment_ms))


@dataclass(slots=True)
class TrackOutcome:
    kind: str
    written: int
    missing: int
    unfinished: int
    covered: float
    span: float

    @property
    def intact(self) -> bool:
        return not self.missing and not self.unfinished

    def notes(self) -> list[str]:
        lines = []
        if self.unfinished:
            lines.append(f"{self.kind}: {self.unfinished} shard(s) never finished downloading — omitted")
        if self.missing:
            lines.append(f"{self.kind}: {self.missing} shard(s) missing from the middle of the sequence")
        shortfall = self.span - self.covered
        if shortfall > 1.0:
            lines.append(f"{self.kind}: holds {self.covered:.0f}s of a {self.span:.0f}s span")
        return lines


@dataclass(slots=True)
class Rebuilt:
    destination: Path
    seconds: float
    tracks: list[TrackOutcome] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return all(track.intact for track in self.tracks)

    def shortest_track(self) -> float:
        """Media every track genuinely covers — the honest length."""
        if not self.tracks:
            return 0.0
        return min(track.covered for track in self.tracks)

    def notes(self) -> list[str]:
        lines = []
        for track in self.tracks:
            lines.extend(track.notes())
        if len(self.tracks) > 1:
            longest = max(t.covered for t in self.tracks)
            shortest = min(t.covered for t in self.tracks)
            if longest - shortest > max(2.0, longest * 0.02):
                lines.append(
                    f"tracks disagree by {longest - shortest:.0f}s — the container will report the longer one"
                )
        return lines


def _concatenate(track: TrackShards, destination: Path) -> int:
    """Byte-concatenate a track's shards, init segment first.

    Plain concatenation is correct here: fMP4 is built for it, and it is what a
    player does when it reads these same files through a playlist. The init
    segment must lead — without it the rest cannot be decoded, so the order is
    not cosmetic.
    """
    written = 0
    with destination.open("wb") as out:
        if track.init is not None:
            try:
                out.write(track.init.read_bytes())
            except OSError as exc:
                raise SalvageError(
                    f"the init segment for {track.kind} is unreadable: {exc}",
                    remedy="Without it this track cannot be decoded. Nothing can be done for it.",
                ) from exc
        for shard in track.shards:
            try:
                out.write(shard.read_bytes())
            except OSError:
                # One bad file should not cost the whole rebuild.
                continue
            written += 1
    return written


def _duration(tools: Toolchain, path: Path) -> float:
    prober = tools.prober
    if prober is None:
        return 0.0
    try:
        finished = subprocess.run(
            [
                str(prober),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return float(finished.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def rebuild(target: Path, destination: Path | None = None, *, tools: Toolchain | None = None) -> Rebuilt:
    tools = tools or Toolchain.discover()
    if tools.muxer is None:
        raise SalvageError(
            "ffmpeg is required to remux the rebuilt tracks",
            remedy="Run this inside the image, which ships ffmpeg.",
        )

    location = locate(target)
    tracks = shards.discover_tracks(location.root, decrypting=location.decrypting)
    if not tracks:
        raise SalvageError(
            f"no shards found under {location.root}",
            remedy="A run started with shards discarded leaves nothing to rebuild from.",
        )

    hint = location.segment_ms or None
    usable = [t for t in tracks if t.kind in REBUILD_ORDER]
    if not usable:
        raise SalvageError(f"no audio or video tracks under {location.root}")
    usable.sort(key=lambda t: REBUILD_ORDER.index(t.kind))

    destination = destination or location.root.with_name(f"{location.root.name}-rebuilt.mkv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.parent / f"_{destination.stem}_parts"
    scratch.mkdir(parents=True, exist_ok=True)

    outcomes: list[TrackOutcome] = []
    try:
        parts: list[Path] = []
        for track in usable:
            part = scratch / f"{track.kind}.mp4"
            written = _concatenate(track, part)
            if not written:
                continue
            parts.append(part)
            outcomes.append(
                TrackOutcome(
                    kind=track.kind,
                    written=written,
                    missing=len(track.gaps(hint)),
                    unfinished=len(track.unfinished),
                    covered=written * track.duration_seconds(hint),
                    span=track.span_seconds(hint),
                )
            )
        if not parts:
            raise SalvageError(f"no readable shards under {location.root}")

        argv = [str(tools.muxer), "-hide_banner", "-loglevel", "error", "-y"]
        for part in parts:
            argv += ["-i", str(part)]
        for index in range(len(parts)):
            # Mapped explicitly. Without it ffmpeg picks one stream per type by
            # its own ordering, which is right most of the time.
            argv += ["-map", str(index)]
        argv += ["-c", "copy", str(destination)]

        finished = subprocess.run(argv, capture_output=True, text=True, check=False)
        if finished.returncode != 0:
            raise SalvageError(
                f"ffmpeg failed to remux the rebuilt tracks: {finished.stderr.strip()[:300]}"
            )
    finally:
        for leftover in scratch.glob("*"):
            leftover.unlink(missing_ok=True)
        scratch.rmdir()

    return Rebuilt(destination=destination, seconds=_duration(tools, destination), tracks=outcomes)
