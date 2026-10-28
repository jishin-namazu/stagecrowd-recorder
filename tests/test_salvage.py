"""Locating shards from a run record, and rebuilding honestly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_shards import AUDIO_DIR, FIRST_STAMP, STEP, VIDEO_DIR, make_track, write_shard

from stagecrowd_recorder import records, salvage, shards
from stagecrowd_recorder.errors import SalvageError


def build_shards(root: Path, *, video: int = 4, audio: int = 4, tmp: int = 0, gap: bool = False):
    video_dir = make_track(root, VIDEO_DIR, b"vide")
    audio_dir = make_track(root, AUDIO_DIR, b"soun")
    offsets = list(range(video))
    if gap and video >= 4:
        offsets.remove(2)
    for index in offsets:
        write_shard(video_dir, FIRST_STAMP + index * STEP, size=4096)
    for index in range(audio):
        write_shard(audio_dir, FIRST_STAMP + index * STEP, size=1024)
    for index in range(tmp):
        write_shard(video_dir, FIRST_STAMP + (video + index) * STEP, state="tmp")
    return root


def write_record(out_dir: Path, shards_at: Path, **overrides) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "url": "https://cdn.example/live/playlist.m3u8",
        "run_name": shards_at.name,
        "output_dir": str(out_dir),
        "shard_root": str(shards_at),
        "shard_root_relative": str(Path("..") / shards_at.name),
        "shards_kept": True,
        "decryptor": "SHAKA_PACKAGER",
        "segment_ms": STEP,
    }
    document.update(overrides)
    (out_dir / records.RECORD_NAME).write_text(json.dumps(document), encoding="utf-8")
    return out_dir


# -- locating ----------------------------------------------------------------


def test_the_run_record_points_from_the_output_directory_to_the_shards(tmp_path):
    # These are two different places and the link is not guessable.
    shard_root = build_shards(tmp_path / "run_x")
    out_dir = write_record(tmp_path / "archive" / "run_x", shard_root)
    assert salvage.locate(out_dir).root == shard_root


def test_a_shard_directory_can_be_given_directly(tmp_path):
    shard_root = build_shards(tmp_path / "run_x")
    assert salvage.locate(shard_root).root == shard_root


def test_a_moved_project_still_finds_its_shards(tmp_path):
    # An absolute path is the wrong shape for this link: capture in a container
    # and salvage on the host makes it a dead link while the relationship holds.
    shard_root = build_shards(tmp_path / "run_x")
    out_dir = write_record(
        tmp_path / "archive" / "run_x",
        shard_root,
        shard_root="/somewhere/that/never/existed/run_x",
        shard_root_relative=str(Path("..") / ".." / "run_x"),
    )
    assert salvage.locate(out_dir).root == shard_root


def test_the_naming_convention_is_the_last_resort(tmp_path, monkeypatch):
    # Both recorded paths are dead, but the run's own name still locates it.
    monkeypatch.chdir(tmp_path)
    shard_root = build_shards(tmp_path / "run_x")
    out_dir = write_record(
        tmp_path / "archive" / "run_x",
        shard_root,
        shard_root="/gone/run_x",
        shard_root_relative=str(Path("..") / "also-gone"),
    )
    assert salvage.locate(out_dir).root == shard_root


def test_dead_recorded_paths_are_named_in_the_error(tmp_path, monkeypatch):
    # Rather than reporting "no shards found" about a directory that was never
    # supposed to hold any.
    monkeypatch.chdir(tmp_path)
    out_dir = write_record(
        tmp_path / "archive" / "run_x",
        tmp_path / "run_x",
        shard_root="/gone/run_x",
        shard_root_relative=str(Path("..") / "also-gone"),
        run_name="also-missing",
    )
    with pytest.raises(SalvageError) as refused:
        salvage.locate(out_dir)
    assert "/gone/run_x" in refused.value.message.replace("\\", "/")


def test_a_recorded_decryptor_means_plain_shards_are_ciphertext(tmp_path):
    shard_root = build_shards(tmp_path / "run_x")
    out_dir = write_record(tmp_path / "archive" / "run_x", shard_root)
    assert salvage.locate(out_dir).decrypting is True


def test_a_missing_shared_root_falls_back_to_the_session_root(tmp_path):
    session_root = build_shards(tmp_path / "run_2")
    out_dir = write_record(
        tmp_path / "archive" / "run_2.out",
        session_root,
        shard_root="/gone/shared",
        shard_root_relative=str(Path("..") / "gone-shared"),
        session_shard_root=str(session_root),
        run_name="also-missing",
    )
    assert salvage.locate(out_dir).root == session_root


def test_the_segment_duration_is_read_back_from_the_record(tmp_path):
    shard_root = build_shards(tmp_path / "run_x")
    out_dir = write_record(tmp_path / "archive" / "run_x", shard_root)
    assert salvage.locate(out_dir).segment_ms == STEP


def test_a_directory_with_no_record_is_taken_as_the_shard_root(tmp_path):
    location = salvage.locate(tmp_path)
    assert location.root == tmp_path
    assert location.decrypting is None


# -- the run record ----------------------------------------------------------


def test_the_record_writes_three_forms_of_the_shard_path(tmp_path):
    out_dir = tmp_path / "archive" / "run_x"
    record = records.RunRecord(
        url="https://cdn.example/x.m3u8",
        run_name="run_x",
        output_dir=out_dir,
        shard_root=tmp_path / "run_x",
        shards_kept=True,
        decryptor="SHAKA_PACKAGER",
    )
    document = record.as_document()
    assert document["shard_root"]
    assert document["shard_root_relative"]
    assert document["run_name"] == "run_x"


def test_the_record_distinguishes_shared_and_session_shard_roots(tmp_path):
    out_dir = tmp_path / "archive" / "run_2.out"
    record = records.RunRecord(
        url="https://cdn.example/x.m3u8",
        run_name="run_2",
        output_dir=out_dir,
        shard_root=tmp_path / "run_1",
        session_shard_root=tmp_path / "run_2",
        shards_kept=True,
        decryptor="SHAKA_PACKAGER",
    ).as_document()
    assert record["shared_shard_root"].endswith("run_1")
    assert record["session_shard_root"].endswith("run_2")


def test_the_record_round_trips_through_disk(tmp_path):
    out_dir = tmp_path / "archive" / "run_x"
    records.RunRecord(
        url="https://cdn.example/x.m3u8",
        run_name="run_x",
        output_dir=out_dir,
        shard_root=tmp_path / "run_x",
        shards_kept=True,
        decryptor="SHAKA_PACKAGER",
        tool_versions={"ffmpeg": "7.1"},
    ).write()
    read_back = records.read_record(out_dir)
    assert read_back is not None
    assert read_back["tool_versions"] == {"ffmpeg": "7.1"}


def test_a_corrupt_record_reads_as_absent(tmp_path):
    tmp_path.joinpath(records.RECORD_NAME).write_text("{not json", encoding="utf-8")
    assert records.read_record(tmp_path) is None


# -- concatenation -----------------------------------------------------------


def test_concatenation_leads_with_the_init_segment(tmp_path):
    # fMP4 cannot be decoded without it; the order is not decoration.
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    (directory / "_init.mp4").write_bytes(b"INIT")
    write_shard(directory, FIRST_STAMP, size=4)
    track = shards.read_track(directory, decrypting=True)
    destination = tmp_path / "out.mp4"
    salvage._concatenate(track, destination)
    assert destination.read_bytes().startswith(b"INIT")


def test_concatenation_keeps_shard_order(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    for index, marker in enumerate((b"AAAA", b"BBBB", b"CCCC")):
        (directory / f"{FIRST_STAMP + index * STEP}_dec.m4s").write_bytes(marker)
    track = shards.read_track(directory, decrypting=True)
    destination = tmp_path / "out.mp4"
    salvage._concatenate(track, destination)
    # The init segment leads; the shards follow it in sequence order.
    assert destination.read_bytes().endswith(b"AAAABBBBCCCC")


def test_concatenation_survives_one_unreadable_shard(tmp_path):
    # One bad file should not cost the whole rebuild.
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    for index in range(3):
        write_shard(directory, FIRST_STAMP + index * STEP, size=16)
    track = shards.read_track(directory, decrypting=True)
    missing = track.shards[1]
    missing.unlink()
    destination = tmp_path / "out.mp4"
    assert salvage._concatenate(track, destination) == 2


# -- honest reporting --------------------------------------------------------


def test_an_unfinished_download_is_reported_rather_than_dropped(tmp_path):
    outcome = salvage.TrackOutcome(
        kind="video", written=4, missing=0, unfinished=2, covered=24.0, span=24.0
    )
    assert not outcome.intact
    assert any("never finished" in note for note in outcome.notes())


def test_a_gap_in_the_middle_is_reported(tmp_path):
    outcome = salvage.TrackOutcome(
        kind="video", written=3, missing=1, unfinished=0, covered=18.0, span=24.0
    )
    assert any("missing from the middle" in note for note in outcome.notes())


def test_a_shortfall_between_span_and_coverage_is_reported():
    outcome = salvage.TrackOutcome(
        kind="video", written=3, missing=1, unfinished=0, covered=18.0, span=24.0
    )
    assert any("of a 24s span" in note for note in outcome.notes())


def test_tracks_of_different_lengths_are_flagged():
    # The mechanism by which a recording holding 36s of picture and 84s of sound
    # announces itself as 84s.
    result = salvage.Rebuilt(
        destination=Path("out.mkv"),
        seconds=84.0,
        tracks=[
            salvage.TrackOutcome("video", 6, 0, 0, 36.0, 36.0),
            salvage.TrackOutcome("audio", 14, 0, 0, 84.0, 84.0),
        ],
    )
    assert any("disagree" in note for note in result.notes())
    # And the honest length is what every track covers.
    assert result.shortest_track() == 36.0


def test_an_intact_rebuild_reports_no_notes():
    result = salvage.Rebuilt(
        destination=Path("out.mkv"),
        seconds=24.0,
        tracks=[
            salvage.TrackOutcome("video", 4, 0, 0, 24.0, 24.0),
            salvage.TrackOutcome("audio", 4, 0, 0, 24.0, 24.0),
        ],
    )
    assert result.notes() == []
    assert result.intact


def test_rebuilding_a_directory_with_no_shards_is_refused(tmp_path):
    with pytest.raises(SalvageError, match="no shards found"):
        salvage.rebuild(tmp_path)
