"""A playback URL keeps one canonical shard directory across restarts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from stagecrowd_recorder import records, shared, shards
from test_shards import FIRST_STAMP, VIDEO_DIR, make_track, write_shard


URL = "https://cdn.example/event/playlist-hls.m3u8"


def record(out_dir: Path, url: str, shard_root: Path, run_name: str) -> None:
    out_dir.mkdir(parents=True)
    (out_dir / records.RECORD_NAME).write_text(
        json.dumps(
            {
                "url": url,
                "run_name": run_name,
                "shard_root": str(shard_root),
            }
        ),
        encoding="utf-8",
    )


def test_the_first_run_uses_its_own_shard_directory(tmp_path):
    session = tmp_path / "run_2"
    assert shared.canonical_shard_root(URL, session) == session


def test_a_restart_reuses_the_first_existing_directory_for_the_same_url(tmp_path):
    first = tmp_path / "run_1"
    first.mkdir()
    record(tmp_path / "run_1.out", URL, first, "run_1")
    assert shared.canonical_shard_root(URL, tmp_path / "run_2") == first


def test_a_different_playback_url_does_not_share_shards(tmp_path):
    first = tmp_path / "run_1"
    first.mkdir()
    record(tmp_path / "run_1.out", "https://cdn.example/other.m3u8", first, "run_1")
    session = tmp_path / "run_2"
    assert shared.canonical_shard_root(URL, session) == session


def test_a_dead_old_shard_path_is_skipped(tmp_path):
    record(tmp_path / "run_1.out", URL, tmp_path / "gone", "run_1")
    session = tmp_path / "run_2"
    assert shared.canonical_shard_root(URL, session) == session


def test_mirroring_prefers_a_hardlink_on_the_same_filesystem(tmp_path):
    source = tmp_path / "source.m4s"
    source.write_bytes(b"media")
    destination = tmp_path / "shared" / "source.m4s"
    assert shared.mirror_file(source, destination)
    assert destination.read_bytes() == b"media"
    assert os.path.samefile(source, destination)


def test_the_watcher_mirrors_init_metadata_and_decrypted_shards(tmp_path):
    session = tmp_path / "run_2"
    canonical = tmp_path / "run_1"
    track = make_track(session, VIDEO_DIR, b"vide")
    shard = write_shard(track, FIRST_STAMP)
    (session / "meta_selected.json").write_text("[]", encoding="utf-8")

    watcher = shards.ShardWatcher(session, mirror_root=canonical, decrypting=True)
    watcher.sweep()

    mirrored_track = canonical / VIDEO_DIR
    assert (canonical / "meta_selected.json").read_text(encoding="utf-8") == "[]"
    assert (mirrored_track / "_init.mp4").is_file()
    assert (mirrored_track / shard.name).is_file()
