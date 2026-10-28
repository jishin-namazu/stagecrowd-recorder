"""Shard identity, track classification, and the two period estimators."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import fmp4_init

from stagecrowd_recorder import shards
from stagecrowd_recorder.shards import AUDIO, VIDEO

# Real directory names. The video directory is named from the CODECS attribute,
# which lists the audio codec first, so it reads as though it were audio.
VIDEO_DIR = "0__mp4a.40.2_5640800_"
AUDIO_DIR = "1_audio_0___eng"
FIRST_STAMP = 1785694666000
STEP = 6000


def make_track(root: Path, name: str, handler: bytes | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if handler is not None:
        (directory / "_init.mp4").write_bytes(fmp4_init(handler))
    return directory


def write_shard(directory: Path, stamp: int, *, state: str = "dec", size: int = 2048) -> Path:
    """Write one stage of a shard's life. Stages are separate calls on purpose."""
    if state == "tmp":
        target = directory / f"{stamp}.m4s.tmp"
    elif state == "raw":
        target = directory / f"{stamp}.m4s"
    else:
        target = directory / f"{stamp}_dec.m4s"
    target.write_bytes(b"\x00" * size)
    return target


# -- location ----------------------------------------------------------------


def test_the_shard_root_is_a_sibling_of_the_working_directory(tmp_path):
    # Counter-intuitive but true: shards do not land under the output directory.
    assert shards.shard_root("run_x", tmp_path) == tmp_path / "run_x"


# -- identity ----------------------------------------------------------------


def test_the_dec_mark_is_stripped_from_a_sequence():
    assert shards.sequence_of(Path("a/1785694666000_dec.m4s")) == "1785694666000"
    assert shards.sequence_of(Path("a/1785694666000.m4s")) == "1785694666000"


def test_a_ciphertext_shard_and_its_dec_twin_share_one_identity():
    # Keyed on the filename they are two arrivals a second apart.
    raw = Path("d/1785694666000.m4s")
    dec = Path("d/1785694666000_dec.m4s")
    assert shards.identity(raw) == shards.identity(dec)


# -- track classification ----------------------------------------------------


def test_the_track_kind_is_read_from_the_init_segment(tmp_path):
    # The directory name is one substring away from lying about this.
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    assert shards.classify_track(directory) == VIDEO


def test_an_audio_init_segment_is_recognised(tmp_path):
    directory = make_track(tmp_path, AUDIO_DIR, b"soun")
    assert shards.classify_track(directory) == AUDIO


def test_a_subtitle_handler_is_neither_video_nor_audio(tmp_path):
    directory = make_track(tmp_path, "2_subs", b"subt")
    assert shards.classify_track(directory) == shards.SUBTITLE


def test_classification_falls_back_to_the_directory_name(tmp_path):
    directory = make_track(tmp_path, AUDIO_DIR)
    assert shards.classify_track(directory) == AUDIO


def test_an_unreadable_init_segment_falls_back_to_the_name(tmp_path):
    directory = make_track(tmp_path, AUDIO_DIR)
    (directory / "_init.mp4").write_bytes(b"nonsense")
    assert shards.classify_track(directory) == AUDIO


# -- reading a track ---------------------------------------------------------


def test_the_init_segment_is_not_listed_as_content(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP)
    track = shards.read_track(directory, decrypting=True)
    assert track.init is not None
    assert len(track.shards) == 1


def test_ciphertext_is_never_published_once_a_dec_file_exists(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP, state="dec")
    write_shard(directory, FIRST_STAMP + STEP, state="raw")
    track = shards.read_track(directory, decrypting=True)
    assert [shards.sequence_of(s) for s in track.shards] == [str(FIRST_STAMP)]


def test_a_broken_decryptor_must_not_make_ciphertext_look_like_content(tmp_path):
    # No _dec file ever appears, so inference would publish the ciphertext.
    # An explicit decrypting=True is the only correct answer here.
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP, state="raw")
    assert shards.read_track(directory, decrypting=True).shards == ()
    assert shards.read_track(directory, decrypting=None).shards != ()


def test_an_unencrypted_run_publishes_its_plain_shards(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP, state="raw")
    assert len(shards.read_track(directory, decrypting=False).shards) == 1


def test_an_unfinished_download_is_reported_not_discarded(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP)
    write_shard(directory, FIRST_STAMP + STEP, state="tmp")
    track = shards.read_track(directory, decrypting=True)
    assert len(track.shards) == 1
    assert len(track.unfinished) == 1


def test_a_zero_byte_shard_is_skipped_while_it_is_written(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP, size=0)
    assert shards.read_track(directory, decrypting=True).shards == ()


def test_non_media_files_are_ignored(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    write_shard(directory, FIRST_STAMP)
    (directory / "notes.txt").write_text("hello")
    assert len(shards.read_track(directory, decrypting=True).shards) == 1


def test_a_missing_directory_is_not_an_error(tmp_path):
    track = shards.read_track(tmp_path / "absent", decrypting=True)
    assert track.shards == ()


def test_discovery_skips_directories_with_no_shards(tmp_path):
    make_track(tmp_path, VIDEO_DIR, b"vide")
    audio = make_track(tmp_path, AUDIO_DIR, b"soun")
    write_shard(audio, FIRST_STAMP)
    found = shards.discover_tracks(tmp_path, decrypting=True)
    assert [t.kind for t in found] == [AUDIO]


# -- the two estimators ------------------------------------------------------


def test_a_gap_does_not_hide_inside_the_inferred_step():
    # A pure median is fooled once gaps outnumber whole steps: the median of
    # {24000, 6000} readings makes the hole disappear.
    stamps = [0, 24000, 30000]
    assert shards.infer_step_ms(stamps) == 6000


def test_a_duplicate_shard_does_not_become_the_inferred_step():
    # A pure minimum adopts the artefact as the period, after which every normal
    # interval looks like dozens of missing shards. The duplicate splits one
    # period into 100 + 5900, so the estimate lands near the period rather than
    # exactly on it; what must not happen is collapsing onto the 100.
    stamps = [0, 100, 6000, 12000, 18000]
    step = shards.infer_step_ms(stamps)
    assert step > 5000
    # And the consequence that made this matter: no phantom losses.
    assert shards.find_gaps(stamps) == []


def test_the_duration_estimator_is_not_dragged_down_by_a_duplicate():
    # This number lands in EXTINF; a minimum here tells players to poll
    # twelve times too fast.
    stamps = [0, 100, 6000, 12000, 18000, 24000]
    assert shards.infer_duration_ms(stamps) == 6000


def test_the_duration_estimator_is_not_stretched_by_a_gap():
    stamps = [0, 6000, 12000, 36000, 42000, 48000]
    assert shards.infer_duration_ms(stamps) == 6000


def test_the_manifest_hint_breaks_a_tie_when_samples_are_too_few():
    # {100, 6000} fits both "short period with a gap" and "long period with a
    # restart artefact"; the timestamps alone cannot say which.
    assert shards.infer_step_ms([0, 100, 6100], hint=6000) == 6000


def test_the_hint_has_no_effect_once_there_are_enough_samples():
    stamps = [0, 6000, 12000, 18000, 24000]
    assert shards.infer_step_ms(stamps, hint=2000) == 6000


@pytest.mark.parametrize(
    "stamps",
    [
        [],
        [0],
        [0, 6000],
        [0, 6000, 12000],
        [0, 6000, 6000, 12000],
        [0, 18000, 24000, 30000],
    ],
)
def test_the_estimators_answer_something_positive_for_every_shape(stamps):
    assert shards.infer_step_ms(stamps, hint=6000) > 0
    assert shards.infer_duration_ms(stamps, hint=6000) > 0


# -- gaps --------------------------------------------------------------------


def test_out_of_order_arrival_is_not_a_gap():
    # Real log order from a concurrent download. Comparing each arrival with the
    # previous one counts this as two losses and never retracts them.
    stamps = [150000, 156000, 174000, 162000, 168000]
    assert shards.find_gaps(stamps) == []


def test_a_genuinely_missing_shard_is_reported():
    stamps = [0, 6000, 18000, 24000, 30000, 36000]
    assert 12000 in shards.find_gaps(stamps)


def test_unclear_geometry_is_not_claimed_as_a_gap():
    # A stream that changes its segment length should not warn on every shard.
    stamps = [0, 6000, 15500, 21000]
    assert shards.find_gaps(stamps) == []


def test_a_gap_is_announced_only_once():
    ledger = shards.GapLedger(hint_ms=6000)
    stamps = [0, 6000, 18000, 24000, 30000, 36000, 42000]
    first = ledger.unreported(VIDEO, stamps)
    assert 12000 in first
    assert ledger.unreported(VIDEO, stamps) == []


def test_a_gap_inside_the_grace_period_is_not_yet_announced():
    ledger = shards.GapLedger(hint_ms=6000)
    stamps = [0, 6000, 18000]
    assert ledger.unreported(VIDEO, stamps) == []


# -- the watcher -------------------------------------------------------------


def test_the_watcher_counts_each_shard_once(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    watcher = shards.ShardWatcher(tmp_path, decrypting=True)
    write_shard(directory, FIRST_STAMP)
    watcher.sweep()
    watcher.sweep()
    assert watcher.tallies[VIDEO].count == 1


def test_the_watcher_picks_up_shards_that_appear_later(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    watcher = shards.ShardWatcher(tmp_path, decrypting=True)
    write_shard(directory, FIRST_STAMP)
    watcher.sweep()
    write_shard(directory, FIRST_STAMP + STEP)
    watcher.sweep()
    assert watcher.tallies[VIDEO].count == 2


def test_the_watcher_keeps_a_separate_log_per_track(tmp_path):
    # Interleaved and different by fortyfold in size; merged, neither is legible.
    root = tmp_path / "shards"
    logs = tmp_path / "logs"
    logs.mkdir()
    video = make_track(root, VIDEO_DIR, b"vide")
    audio = make_track(root, AUDIO_DIR, b"soun")
    write_shard(video, FIRST_STAMP, size=200_000)
    write_shard(audio, FIRST_STAMP, size=5_000)

    watcher = shards.ShardWatcher(root, log_dir=logs, decrypting=True)
    watcher.sweep()
    watcher.stop()

    assert (logs / "shards-video.log").is_file()
    assert (logs / "shards-audio.log").is_file()


def test_the_watcher_tallies_bytes_per_track(tmp_path):
    root = tmp_path / "shards"
    video = make_track(root, VIDEO_DIR, b"vide")
    audio = make_track(root, AUDIO_DIR, b"soun")
    write_shard(video, FIRST_STAMP, size=200_000)
    write_shard(audio, FIRST_STAMP, size=5_000)
    watcher = shards.ShardWatcher(root, decrypting=True)
    watcher.sweep()
    assert watcher.tallies[VIDEO].total_bytes == 200_000
    assert watcher.tallies[AUDIO].total_bytes == 5_000


def test_the_watcher_summary_is_empty_when_nothing_arrived(tmp_path):
    watcher = shards.ShardWatcher(tmp_path, decrypting=True)
    watcher.sweep()
    assert watcher.summary() == ""


def test_stopping_a_watcher_that_never_started_is_safe(tmp_path):
    shards.ShardWatcher(tmp_path, decrypting=True).stop()


# -- derived quantities ------------------------------------------------------


def test_covered_seconds_counts_media_held_not_time_straddled(tmp_path):
    directory = make_track(tmp_path, VIDEO_DIR, b"vide")
    for index in (0, 1, 5, 6):
        write_shard(directory, FIRST_STAMP + index * STEP)
    track = shards.read_track(directory, decrypting=True)
    assert track.covered_seconds(STEP) == pytest.approx(24.0)
    assert track.span_seconds(STEP) == pytest.approx(42.0)
