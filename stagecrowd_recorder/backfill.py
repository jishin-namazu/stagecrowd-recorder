"""Recover CDN shards that are absent from a local recording.

N_m3u8DL-RE keeps decrypted fMP4 shards but does not retain the CDN sequence
number in their filenames; it names them by program time instead.  Backfill
bridges those two namespaces from ``meta_selected.json``: a recorded
index/program-time pair identifies what the local timestamps mean, while the
fragment's ``tfdt`` box gives an exact timestamp for newly downloaded audio
without rounding away AAC frame-boundary jitter.

The CDN boundary search applies to the sequence-shaped URLs emitted by the
Brightcove live service (``..._<number>.mp4``).  It deliberately probes with
HEAD before downloading and assumes the surviving objects form one contiguous
range, which is verified at both discovered boundaries.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from . import netio, records, salvage, shards
from .errors import BackfillError, ToolError
from .keys import KeyRing
from .protection import read_kids
from .toolchain import Decryptor, find, probe

SELECTED_NAME = "meta_selected.json"
DEFAULT_RATE_LIMIT = 3 * 1024**2
MAX_DISCOVERY_SPAN = 1_000_000

_INDEXED_NAME = re.compile(r"\A(?P<prefix>.*_)(?P<index>[0-9]+)(?P<suffix>\.(?:m4s|mp4))\Z", re.I)
_RATE = re.compile(r"\A([0-9]+(?:\.[0-9]+)?)\s*([KMG]?)\Z", re.I)
_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf"})


@dataclass(frozen=True, slots=True)
class IndexedUrl:
    scheme: str
    netloc: str
    path_prefix: str
    index: int
    suffix: str
    query: str = ""
    fragment: str = ""

    @classmethod
    def parse(cls, url: str) -> "IndexedUrl":
        parts = urlsplit(url)
        name = parts.path.rsplit("/", 1)[-1]
        match = _INDEXED_NAME.match(name)
        if not match:
            raise ValueError("segment URL has no numeric segment index at the end")
        parent = parts.path[: -len(name)]
        return cls(
            parts.scheme,
            parts.netloc,
            parent + match.group("prefix"),
            int(match.group("index")),
            match.group("suffix"),
            parts.query,
            parts.fragment,
        )

    def with_index(self, index: int) -> str:
        path = f"{self.path_prefix}{index}{self.suffix}"
        return urlunsplit((self.scheme, self.netloc, path, self.query, self.fragment))


def parse_rate_limit(text: str) -> int:
    match = _RATE.match(text.strip())
    if not match:
        raise ValueError("rate limit must look like 512K, 3M, or 0")
    amount = float(match.group(1))
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2).upper()]
    value = int(amount * multiplier)
    if value < 0:
        raise ValueError("rate limit cannot be negative")
    return value


def discover_bounds(
    seed: int,
    exists: Callable[[int], bool],
    *,
    max_span: int = MAX_DISCOVERY_SPAN,
) -> tuple[int, int]:
    """Find the first and last existing index around one known-good seed."""
    cache: dict[int, bool] = {}

    def has(index: int) -> bool:
        if index < 0:
            return False
        if index not in cache:
            cache[index] = exists(index)
        return cache[index]

    if not has(seed):
        raise ValueError(f"seed segment {seed} is not available")

    step = 1
    first_good = seed
    while step <= max_span and has(seed - step):
        first_good = seed - step
        step *= 2
    first_bad = max(-1, seed - step)
    low, high = first_bad + 1, first_good
    while low < high:
        middle = (low + high) // 2
        if has(middle):
            high = middle
        else:
            low = middle + 1
    first = low

    step = 1
    last_good = seed
    while step <= max_span and has(seed + step):
        last_good = seed + step
        step *= 2
    if step > max_span:
        raise ValueError(f"CDN range extends more than {max_span} indexes from the seed")
    last_bad = seed + step
    low, high = last_good, last_bad - 1
    while low < high:
        middle = (low + high + 1) // 2
        if has(middle):
            low = middle
        else:
            high = middle - 1
    return first, low


def index_for_timestamp(stamp_ms: int, anchor_index: int, anchor_ms: int, segment_ms: int) -> int:
    if segment_ms <= 0:
        raise ValueError("segment duration must be positive")
    return anchor_index + round((stamp_ms - anchor_ms) / segment_ms)


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    limit = len(data) if end is None else min(end, len(data))
    at = start
    while at + 8 <= limit:
        size, kind = struct.unpack_from(">I4s", data, at)
        header = 8
        if size == 1:
            if at + 16 > limit:
                break
            (size,) = struct.unpack_from(">Q", data, at + 8)
            header = 16
        elif size == 0:
            size = limit - at
        if size < header or at + size > limit:
            break
        yield kind, at + header, at + size
        at += size


def _find_payload(data: bytes, wanted: bytes, start: int = 0, end: int | None = None) -> bytes | None:
    for kind, payload_start, box_end in _boxes(data, start, end):
        if kind == wanted:
            return data[payload_start:box_end]
        if kind in _CONTAINERS:
            found = _find_payload(data, wanted, payload_start, box_end)
            if found is not None:
                return found
    return None


def read_tfdt(data: bytes) -> int:
    payload = _find_payload(data, b"tfdt")
    if payload is None or len(payload) < 8:
        raise ValueError("fragment has no readable tfdt box")
    version = payload[0]
    if version == 1:
        if len(payload) < 12:
            raise ValueError("version 1 tfdt box is truncated")
        return struct.unpack_from(">Q", payload, 4)[0]
    return struct.unpack_from(">I", payload, 4)[0]


def read_timescale(data: bytes) -> int:
    payload = _find_payload(data, b"mdhd")
    if payload is None or len(payload) < 16:
        raise ValueError("init segment has no readable mdhd box")
    version = payload[0]
    offset = 20 if version == 1 else 12
    if len(payload) < offset + 4:
        raise ValueError("mdhd box is truncated")
    timescale = struct.unpack_from(">I", payload, offset)[0]
    if not timescale:
        raise ValueError("mdhd timescale is zero")
    return timescale


def timestamp_from_decode_time(
    *,
    anchor_timestamp_ms: int,
    anchor_decode_time: int,
    candidate_decode_time: int,
    timescale: int,
) -> int:
    return anchor_timestamp_ms + round(
        (candidate_decode_time - anchor_decode_time) * 1000 / timescale
    )


def _datetime_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return round(parsed.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class TrackPlan:
    kind: str
    track: shards.TrackShards
    indexed: IndexedUrl
    anchor_index: int
    anchor_timestamp_ms: int
    segment_ms: int
    kid: str
    key: str


@dataclass(frozen=True, slots=True)
class TrackResult:
    kind: str
    remote_first: int
    remote_last: int
    considered_last: int
    local: int
    missing: int
    recovered: int
    failures: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class BackfillResult:
    active: bool
    tracks: tuple[TrackResult, ...]

    @property
    def ok(self) -> bool:
        return not any(track.failures for track in self.tracks) and all(
            track.recovered == track.missing for track in self.tracks
        )


def _output_dir(target: Path, location: salvage.ShardLocation) -> Path:
    if (target / records.RECORD_NAME).is_file():
        return target
    sibling = location.root.with_name(f"{location.root.name}.out")
    if (sibling / records.RECORD_NAME).is_file():
        return sibling
    if location.record and location.record.get("output_dir"):
        recorded = Path(location.record["output_dir"])
        if (recorded / records.RECORD_NAME).is_file():
            return recorded
    raise BackfillError(
        "could not find the output directory that holds run.json and keys.txt",
        remedy="Pass the run's .out directory, for example /archive/run_....out.",
    )


def _plans(target: Path) -> tuple[salvage.ShardLocation, Path, tuple[TrackPlan, ...]]:
    location = salvage.locate(target)
    out_dir = _output_dir(target, location)
    selected_path = location.root / SELECTED_NAME
    try:
        selected = json.loads(selected_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise BackfillError(
            f"could not read {selected_path}: {exc}",
            remedy="Backfill needs the downloader's meta_selected.json in the shard directory.",
        ) from exc
    if not isinstance(selected, list):
        raise BackfillError(f"{selected_path} does not contain a stream list")

    try:
        key_ring = KeyRing.scrape((out_dir / records.KEYS_NAME).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise BackfillError(f"could not read {out_dir / records.KEYS_NAME}: {exc}") from exc
    local_tracks = {track.kind: track for track in shards.discover_tracks(location.root, decrypting=True)}
    segment_ms = location.segment_ms or 6000
    plans: list[TrackPlan] = []

    for stream in selected:
        if not isinstance(stream, dict):
            continue
        kind = shards.AUDIO if stream.get("MediaType") == "AUDIO" else shards.VIDEO
        track = local_tracks.get(kind)
        if track is None or track.init is None:
            continue
        try:
            segment = stream["Playlist"]["MediaParts"][0]["MediaSegments"][0]
            indexed = IndexedUrl.parse(segment["Url"])
            anchor_index = int(segment["Index"])
            anchor_timestamp = _datetime_ms(segment["DateTime"])
            kids = read_kids(stream["Playlist"]["MediaInit"]["EncryptInfo"]["Key"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BackfillError(f"could not read the selected {kind} metadata: {exc}") from exc
        if len(kids) != 1:
            raise BackfillError(f"selected {kind} stream names {len(kids)} content KIDs")
        content_key = next((item for item in key_ring if item.kid == kids[0]), None)
        if content_key is None:
            raise BackfillError(f"keys.txt has no key for the selected {kind} stream")
        plans.append(
            TrackPlan(
                kind,
                track,
                indexed,
                anchor_index,
                anchor_timestamp,
                segment_ms,
                kids[0],
                content_key.key,
            )
        )
    if not plans:
        raise BackfillError("meta_selected.json did not map to any local audio or video track")
    return location, out_dir, tuple(plans)


def _is_active(root: Path, segment_ms: int) -> bool:
    latest = 0.0
    try:
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                if entry.is_file() and (entry.name.endswith(".tmp") or entry.name.endswith("_dec.m4s")):
                    latest = max(latest, entry.stat().st_mtime)
    except OSError:
        return False
    return latest > 0 and time.time() - latest < max(30.0, segment_ms * 5 / 1000)


def _download(url: str, destination: Path, rate_limit: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": netio.DEFAULT_UA, "Accept": "*/*"})
    for attempt in range(4):
        try:
            started = time.monotonic()
            written = 0
            with urllib.request.urlopen(request, timeout=45) as response, destination.open("wb") as out:
                if response.status != 200:
                    raise OSError(f"HTTP {response.status}")
                expected = int(response.headers.get("Content-Length") or 0)
                while True:
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    out.write(block)
                    written += len(block)
                    if rate_limit:
                        delay = written / rate_limit - (time.monotonic() - started)
                        if delay > 0:
                            time.sleep(delay)
            if expected and written != expected:
                raise OSError(f"short download {written}/{expected}")
            return
        except (OSError, urllib.error.URLError):
            destination.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def _decrypt(plan: TrackPlan, encrypted: Path, destination: Path, binary: Path) -> None:
    assert plan.track.init is not None
    combined = encrypted.with_suffix(".itmp")
    decrypted = encrypted.with_name("decrypted.m4s")
    with combined.open("wb") as out:
        with plan.track.init.open("rb") as source:
            shutil.copyfileobj(source, out)
        with encrypted.open("rb") as source:
            shutil.copyfileobj(source, out)
    finished = subprocess.run(
        [
            str(binary),
            "--quiet",
            "--enable_raw_key_decryption",
            f"input={combined},stream=0,output={decrypted}",
            "--keys",
            f"key_id={plan.kid}:key={plan.key}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    if finished.returncode != 0 or not decrypted.is_file() or not decrypted.stat().st_size:
        raise OSError(f"shaka-packager exited {finished.returncode}")
    staging = destination.with_name(destination.name + ".backfill.tmp")
    shutil.copyfile(decrypted, staging)
    if destination.exists():
        staging.unlink(missing_ok=True)
        return
    staging.replace(destination)


def run(
    target: Path,
    *,
    rate_limit: int = DEFAULT_RATE_LIMIT,
    scan_only: bool = False,
    include_live_tail: bool = False,
    echo: Callable[[str], None] | None = None,
) -> BackfillResult:
    location, _out_dir, plans = _plans(target)
    active = _is_active(location.root, max(plan.segment_ms for plan in plans))

    binary: Path | None = None
    if not scan_only:
        binary = find(*Decryptor.SHAKA.candidates, env="STC_SHAKA")
        if binary is None:
            raise ToolError("shaka-packager is required for backfill")
        checked = probe(binary)
        if not checked.runnable:
            raise ToolError(f"shaka-packager is not runnable: {checked.detail}")

    outcomes: list[TrackResult] = []
    for plan in plans:
        def exists(index: int) -> bool:
            try:
                return netio.head(plan.indexed.with_index(index), timeout=15).ok
            except netio.TransportError as exc:
                raise BackfillError(f"CDN probe failed for {plan.kind}: {exc}") from exc

        try:
            remote_first, remote_last = discover_bounds(plan.anchor_index, exists)
        except ValueError as exc:
            raise BackfillError(f"could not discover the {plan.kind} CDN range: {exc}") from exc

        local_indexes: dict[int, Path] = {}
        for shard in plan.track.shards:
            sequence = shards.sequence_of(shard)
            if not sequence.isdigit():
                continue
            index = index_for_timestamp(
                int(sequence), plan.anchor_index, plan.anchor_timestamp_ms, plan.segment_ms
            )
            local_indexes.setdefault(index, shard)
        if not local_indexes:
            raise BackfillError(f"the local {plan.kind} track contains no timestamped shards")
        considered_last = remote_last
        if active and not include_live_tail:
            considered_last = min(remote_last, max(local_indexes))
        missing = [
            index
            for index in range(remote_first, considered_last + 1)
            if index not in local_indexes
        ]

        recovered = 0
        failures: list[int] = []
        if missing and not scan_only:
            anchor_path = local_indexes.get(plan.anchor_index)
            if anchor_path is None:
                anchor_path = min(
                    plan.track.shards,
                    key=lambda path: abs(int(shards.sequence_of(path)) - plan.anchor_timestamp_ms),
                )
            try:
                anchor_decode = read_tfdt(anchor_path.read_bytes())
                timescale = read_timescale(plan.track.init.read_bytes())  # type: ignore[union-attr]
            except (OSError, ValueError) as exc:
                raise BackfillError(f"could not calibrate the {plan.kind} media clock: {exc}") from exc

            for position, index in enumerate(missing, 1):
                if echo and (position == 1 or position % 10 == 0 or position == len(missing)):
                    echo(f"{plan.kind}: recovering {position}/{len(missing)} (CDN index {index})")
                try:
                    with tempfile.TemporaryDirectory(prefix="stagecrowd-backfill-") as temporary:
                        encrypted = Path(temporary) / f"segment{plan.indexed.suffix}"
                        _download(plan.indexed.with_index(index), encrypted, rate_limit)
                        decode_time = read_tfdt(encrypted.read_bytes())
                        stamp = timestamp_from_decode_time(
                            anchor_timestamp_ms=plan.anchor_timestamp_ms,
                            anchor_decode_time=anchor_decode,
                            candidate_decode_time=decode_time,
                            timescale=timescale,
                        )
                        destination = plan.track.path / f"{stamp}_dec.m4s"
                        assert binary is not None
                        _decrypt(plan, encrypted, destination, binary)
                    recovered += 1
                except (OSError, ValueError, subprocess.SubprocessError):
                    failures.append(index)

        outcomes.append(
            TrackResult(
                plan.kind,
                remote_first,
                remote_last,
                considered_last,
                len(local_indexes),
                len(missing),
                recovered,
                tuple(failures),
            )
        )
    return BackfillResult(active, tuple(outcomes))
