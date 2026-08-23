"""One canonical shard directory for every exact playback URL.

The downloader needs a fresh run name after a restart, so it still writes into
one session directory of its own.  Finished shards are mirrored into the first
surviving directory recorded for the same URL.  On a normal archive volume the
mirror is a hard link: both directory entries name the same bytes and consume no
second copy of the media.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import records

ROOT_METADATA = ("meta_selected.json", "meta.json", "raw.m3u8")


def _recorded_roots(document: dict, out_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for key in ("shared_shard_root", "shard_root", "session_shard_root"):
        value = document.get(key)
        if value:
            candidates.append(Path(value))
    relative = document.get("shard_root_relative")
    if relative:
        candidates.append((out_dir / relative).resolve())
    run_name = document.get("run_name")
    if run_name:
        candidates.append(out_dir.parent / str(run_name))
    return candidates


def canonical_shard_root(url: str, session_root: Path) -> Path:
    """First surviving shard directory recorded for this exact URL."""
    archive_root = session_root.parent
    try:
        outputs = sorted(path for path in archive_root.glob("*.out") if path.is_dir())
    except OSError:
        return session_root
    for out_dir in outputs:
        document = records.read_record(out_dir)
        if not document or document.get("url") != url:
            continue
        for candidate in _recorded_roots(document, out_dir):
            if candidate.is_dir():
                return candidate
    return session_root


def mirror_file(source: Path, destination: Path) -> bool:
    """Place one immutable file in the canonical store without overwriting."""
    if source == destination:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return False
    try:
        os.link(source, destination)
        return True
    except FileExistsError:
        return False
    except OSError:
        # Some bind/network filesystems do not expose hard links. Copy into the
        # destination directory, then link that completed copy into place so a
        # racing watcher never observes a partially written media file.
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.mirror.tmp")
        try:
            shutil.copy2(source, temporary)
            try:
                os.link(temporary, destination)
                return True
            except FileExistsError:
                return False
        finally:
            temporary.unlink(missing_ok=True)


def mirror_metadata(session_root: Path, canonical_root: Path) -> None:
    if session_root == canonical_root:
        return
    for name in ROOT_METADATA:
        source = session_root / name
        if source.is_file():
            mirror_file(source, canonical_root / name)
