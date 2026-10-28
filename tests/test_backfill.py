"""Discovering and naming CDN shards for backfill."""

from __future__ import annotations

import struct

import pytest

from stagecrowd_recorder import backfill


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full_box(kind: bytes, version: int, payload: bytes) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def test_an_indexed_segment_url_round_trips_its_query():
    indexed = backfill.IndexedUrl.parse(
        "https://cdn.example/live/media_hls1080p_297912786.mp4?token=x"
    )
    assert indexed.index == 297912786
    assert indexed.with_index(143).endswith("media_hls1080p_143.mp4?token=x")


def test_a_url_without_a_numeric_tail_is_refused():
    with pytest.raises(ValueError, match="numeric segment index"):
        backfill.IndexedUrl.parse("https://cdn.example/live/segment.mp4")


def test_contiguous_cdn_bounds_are_found_with_few_probes():
    calls = []

    def exists(index: int) -> bool:
        calls.append(index)
        return 143 <= index <= 912

    assert backfill.discover_bounds(500, exists) == (143, 912)
    assert len(calls) < 50


def test_the_seed_must_still_exist():
    with pytest.raises(ValueError, match="seed segment"):
        backfill.discover_bounds(500, lambda _: False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", 0), ("512K", 512 * 1024), ("3M", 3 * 1024**2), ("1.5m", int(1.5 * 1024**2))],
)
def test_rate_limits_are_human_readable(text, expected):
    assert backfill.parse_rate_limit(text) == expected


def test_bad_rate_limits_are_refused():
    with pytest.raises(ValueError, match="rate limit"):
        backfill.parse_rate_limit("quickly")


def test_local_timestamps_map_back_to_cdn_indexes_despite_aac_jitter():
    anchor_index = 297912785
    anchor_ms = 1787476708010
    assert backfill.index_for_timestamp(1787476600000, anchor_index, anchor_ms, 6000) == 297912767
    assert backfill.index_for_timestamp(1787476702016, anchor_index, anchor_ms, 6000) == 297912784


def test_tfdt_version_zero_is_read_through_moof_and_traf():
    tfdt = full_box(b"tfdt", 0, struct.pack(">I", 123456))
    fragment = box(b"moof", box(b"traf", tfdt))
    assert backfill.read_tfdt(fragment) == 123456


def test_tfdt_version_one_keeps_a_64_bit_decode_time():
    tfdt = full_box(b"tfdt", 1, struct.pack(">Q", 2**40 + 7))
    assert backfill.read_tfdt(box(b"moof", box(b"traf", tfdt))) == 2**40 + 7


def test_mdhd_timescale_is_read_from_a_version_zero_init():
    mdhd = full_box(b"mdhd", 0, struct.pack(">IIII", 1, 2, 48_000, 99))
    init = box(b"moov", box(b"trak", box(b"mdia", mdhd)))
    assert backfill.read_timescale(init) == 48_000


def test_decode_time_calibrates_the_exact_program_timestamp():
    stamp = backfill.timestamp_from_decode_time(
        anchor_timestamp_ms=1_000_005,
        anchor_decode_time=48_000,
        candidate_decode_time=48_000 + 288_512,
        timescale=48_000,
    )
    assert stamp == 1_006_015


def test_missing_but_not_recovered_is_not_a_complete_result():
    track = backfill.TrackResult("video", 10, 20, 20, 10, 1, 0)
    assert not backfill.BackfillResult(False, (track,)).ok
