"""Playlist parsing, track selection, and walking down from a master.

The selection tests model the real shape that broke earlier attempts: a master
declaring three session keys while only two tracks are ever captured.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import (
    AUDIO_KID,
    AUDIO_PSSH_BARE,
    MASTER_PLAYLIST,
    SPARE_KID,
    VIDEO_KID,
    VIDEO_PSSH_BARE,
    media_playlist,
    session_key,
)

from stagecrowd_recorder import playlist
from stagecrowd_recorder.errors import SourceError

ROUTES = {
    "/playlist.m3u8": MASTER_PLAYLIST,
    "/chunklist_0.m3u8": media_playlist("EhBRTqXEyPQ5Fps8HqDXcOTZ"),
    "/chunklist_3.m3u8": media_playlist(VIDEO_PSSH_BARE),
    "/chunklist_audio0.m3u8": media_playlist(AUDIO_PSSH_BARE),
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        body = ROUTES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


# -- parsing -----------------------------------------------------------------


def test_a_master_is_recognised_as_one():
    parsed = playlist.parse(MASTER_PLAYLIST, "https://example/live/playlist.m3u8")
    assert parsed.is_master
    assert parsed.segment_count == 0


def test_a_master_collects_every_session_key():
    # An earlier design kept one PSSH slot, so the last tag won and the rest
    # were lost.
    parsed = playlist.parse(MASTER_PLAYLIST)
    assert len(parsed.protection.payloads) == 3
    assert parsed.protection.advertised == {VIDEO_KID, AUDIO_KID, SPARE_KID}


def test_variant_urls_resolve_against_the_playlist_url():
    parsed = playlist.parse(MASTER_PLAYLIST, "https://example/live/playlist.m3u8")
    urls = {r.url for r in parsed.renditions}
    assert "https://example/live/chunklist_3.m3u8" in urls
    assert "https://example/live/chunklist_audio0.m3u8" in urls


def test_a_video_rendition_carries_its_attributes():
    parsed = playlist.parse(MASTER_PLAYLIST)
    best = max(parsed.video_renditions, key=lambda r: r.bandwidth)
    assert best.resolution == "1920x1080"
    assert best.bandwidth == 5_240_000  # AVERAGE-BANDWIDTH, not BANDWIDTH


def test_average_bandwidth_is_not_confused_with_bandwidth():
    # An unanchored attribute search matches BANDWIDTH inside
    # AVERAGE-BANDWIDTH and reads the wrong number.
    text = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=100,BANDWIDTH=900,RESOLUTION=1x1",
            "a.m3u8",
            "",
        ]
    )
    parsed = playlist.parse(text)
    assert parsed.video_renditions[0].bandwidth == 100


def test_an_audio_rendition_carries_its_group_and_language():
    parsed = playlist.parse(MASTER_PLAYLIST)
    audio = parsed.audio_renditions[0]
    assert (audio.group_id, audio.language, audio.default) == ("audio0", "eng", False)


def test_a_media_playlist_counts_its_segments_and_sequence():
    parsed = playlist.parse(media_playlist(VIDEO_PSSH_BARE, first=1183, count=3))
    assert not parsed.is_master
    assert parsed.segment_count == 3
    assert parsed.media_sequence == 1183
    assert parsed.target_duration == 6


def test_a_playlist_without_an_endlist_is_live():
    assert playlist.parse(media_playlist(VIDEO_PSSH_BARE, live=True)).live


def test_a_playlist_with_an_endlist_is_not_live():
    assert not playlist.parse(media_playlist(VIDEO_PSSH_BARE, live=False)).live


def test_a_fairplay_key_is_recorded_separately():
    text = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-TARGETDURATION:6",
            '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",'
            'URI="skd://xyz?keyId=ABCDEF0123456789ABCDEF0123456789"',
            "#EXTINF:6.0,",
            "a.m4s",
            "",
        ]
    )
    parsed = playlist.parse(text)
    assert parsed.protection.fairplay_kids == {"abcdef0123456789abcdef0123456789"}
    # FairPlay contributes no Widevine KID; counting it would make coverage
    # permanently unsatisfiable.
    assert parsed.protection.advertised == frozenset()


def test_a_non_playlist_body_is_rejected():
    with pytest.raises(SourceError, match="not an HLS playlist"):
        playlist.parse("<!DOCTYPE html><html>login</html>", "https://example/x.m3u8")


def test_a_playlist_that_advertises_widevine_with_no_readable_pssh_is_rejected():
    text = "\n".join(
        ["#EXTM3U", session_key("!!!not base64!!!"), "#EXTINF:6.0,", "a.m4s", ""]
    )
    with pytest.raises(SourceError, match="no PSSH could be read"):
        playlist.parse(text)


def test_one_unreadable_pssh_among_good_ones_is_ignored():
    text = "\n".join(
        [
            "#EXTM3U",
            session_key(VIDEO_PSSH_BARE),
            session_key("!!!broken!!!"),
            "chunklist_3.m3u8",
            "",
        ]
    )
    parsed = playlist.parse(text)
    assert parsed.protection.payloads == (VIDEO_PSSH_BARE,)


# -- selection ---------------------------------------------------------------


def test_selection_takes_the_highest_bandwidth_video_and_one_audio():
    parsed = playlist.parse(MASTER_PLAYLIST, "https://example/live/playlist.m3u8")
    picked = playlist.pick_tracks(parsed)
    kinds = sorted(track.kind for track in picked)
    assert kinds == ["audio", "video"]
    video = next(t for t in picked if t.kind == "video")
    assert video.resolution == "1920x1080"


def test_walking_down_narrows_the_needed_kids_to_the_captured_tracks(server):
    source = playlist.resolve(f"{server}/playlist.m3u8")
    assert source.protection.must_cover == {VIDEO_KID, AUDIO_KID}
    # The 640x360 variant's KID is advertised and never needed.
    assert source.protection.spare == {SPARE_KID}


def test_every_pssh_is_kept_even_when_its_kid_is_not_needed(server):
    # More payloads means more chances a server accepts one of them.
    source = playlist.resolve(f"{server}/playlist.m3u8")
    assert len(source.protection.payloads) == 3


def test_target_duration_is_backfilled_from_a_variant(server):
    # A master never carries EXT-X-TARGETDURATION; it is a media playlist tag.
    source = playlist.resolve(f"{server}/playlist.m3u8")
    assert source.playlist.target_duration == 6
    assert source.segment_ms == 6000


def test_a_media_playlist_needs_its_own_kid(server):
    source = playlist.resolve(f"{server}/chunklist_audio0.m3u8")
    assert source.protection.must_cover == {AUDIO_KID}


def test_an_unreachable_url_reports_a_source_error():
    with pytest.raises(SourceError):
        playlist.resolve("http://127.0.0.1:1/nothing.m3u8")


# -- aligning with what the downloader reported ------------------------------


def test_reported_selection_maps_back_onto_renditions():
    parsed = playlist.parse(MASTER_PLAYLIST, "https://example/live/playlist.m3u8")
    reported = [
        {"MediaType": "", "Bandwidth": 5240000, "Resolution": "1920x1080"},
        {"MediaType": "AUDIO", "GroupId": "audio0", "Name": "eng", "Language": "eng"},
    ]
    aligned = playlist.align_selection(parsed, reported)
    assert aligned is not None
    assert sorted(t.kind for t in aligned) == ["audio", "video"]


def test_one_unmatched_entry_discards_the_whole_mapping():
    # A partial mapping narrows the required set and lets a key set missing the
    # audio key through the gate — worse than not reading the file at all.
    parsed = playlist.parse(MASTER_PLAYLIST, "https://example/live/playlist.m3u8")
    reported = [
        {"MediaType": "", "Bandwidth": 5240000, "Resolution": "1920x1080"},
        {"MediaType": "AUDIO", "GroupId": "changed-by-the-cdn"},
    ]
    assert playlist.align_selection(parsed, reported) is None


def test_subtitle_entries_are_skipped_rather_than_failing_the_mapping():
    parsed = playlist.parse(MASTER_PLAYLIST, "https://example/live/playlist.m3u8")
    reported = [
        {"MediaType": "", "Bandwidth": 5240000, "Resolution": "1920x1080"},
        {"MediaType": "SUBTITLES", "GroupId": "sub"},
    ]
    aligned = playlist.align_selection(parsed, reported)
    assert aligned is not None
    assert [t.kind for t in aligned] == ["video"]


def test_no_reported_selection_yields_no_alignment():
    parsed = playlist.parse(MASTER_PLAYLIST)
    assert playlist.align_selection(parsed, None) is None
    assert playlist.align_selection(parsed, []) is None


def test_a_reported_selection_is_read_through_a_bom(tmp_path):
    (tmp_path / playlist.SELECTION_FILE).write_text(
        '[{"MediaType":"AUDIO","GroupId":"audio0"}]', encoding="utf-8-sig"
    )
    assert playlist.read_reported_selection(tmp_path) == [
        {"MediaType": "AUDIO", "GroupId": "audio0"}
    ]


def test_a_missing_selection_file_is_not_an_error(tmp_path):
    assert playlist.read_reported_selection(tmp_path) is None
