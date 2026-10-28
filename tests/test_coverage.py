"""The coverage gate and the rotation watch."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import (
    AUDIO_KEY,
    AUDIO_KID,
    AUDIO_PSSH_BARE,
    SPARE_KID,
    VIDEO_KEY,
    VIDEO_KID,
    VIDEO_PSSH_BARE,
    media_playlist,
)

from stagecrowd_recorder import coverage
from stagecrowd_recorder.errors import CoverageGap
from stagecrowd_recorder.keys import KeyRing
from stagecrowd_recorder.protection import Protection

ROTATED_KID = "dd75b3df034b32bc8ca4c961b585ecd9"
ROTATED_PSSH = "EhDddbPfA0syvIykyWG1hezZ"


def protection(needed):
    return Protection(
        payloads=(VIDEO_PSSH_BARE, AUDIO_PSSH_BARE),
        advertised=frozenset({VIDEO_KID, AUDIO_KID, SPARE_KID}),
        needed=frozenset(needed),
    )


# -- the gate ----------------------------------------------------------------


def test_the_two_track_key_set_is_accepted():
    # The exact set an earlier design rejected: three KIDs advertised, two
    # captured, keys for both captured tracks.
    ring = KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}")
    report = coverage.enforce(ring, protection({VIDEO_KID, AUDIO_KID}))
    assert report.covered
    assert report.spare == {SPARE_KID}


def test_a_key_set_for_a_track_that_is_not_captured_is_still_refused():
    ring = KeyRing.scrape(f"{SPARE_KID}:{'a' * 32}")
    with pytest.raises(CoverageGap, match="do not cover"):
        coverage.enforce(ring, protection({VIDEO_KID, AUDIO_KID}))


def test_a_missing_audio_key_is_refused():
    ring = KeyRing.scrape(VIDEO_KEY)
    with pytest.raises(CoverageGap) as refused:
        coverage.enforce(ring, protection({VIDEO_KID, AUDIO_KID}))
    assert AUDIO_KID in str(refused.value)


def test_the_refusal_lists_what_is_held_as_a_remedy():
    ring = KeyRing.scrape(VIDEO_KEY)
    with pytest.raises(CoverageGap) as refused:
        coverage.enforce(ring, protection({VIDEO_KID, AUDIO_KID}))
    assert VIDEO_KID in (refused.value.remedy or "")


def test_a_partial_key_set_proceeds_when_that_is_asked_for():
    ring = KeyRing.scrape(VIDEO_KEY)
    report = coverage.enforce(ring, protection({VIDEO_KID, AUDIO_KID}), strict=False)
    assert report.missing == {AUDIO_KID}


def test_nothing_declared_does_not_block():
    ring = KeyRing.scrape(VIDEO_KEY)
    assert coverage.enforce(ring, Protection()).covered


# -- the rotation watch ------------------------------------------------------


class _State:
    body = media_playlist(VIDEO_PSSH_BARE)
    status = 200


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        if _State.status != 200:
            self.send_response(_State.status)
            self.end_headers()
            return
        payload = _State.body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def stream():
    _State.body = media_playlist(VIDEO_PSSH_BARE)
    _State.status = 200
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/chunklist.m3u8"
    httpd.shutdown()
    httpd.server_close()


def test_held_coverage_reports_nothing(stream):
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    assert watch.check_once() == frozenset()


def test_extra_keys_do_not_raise_a_false_alarm(stream):
    watch = coverage.RotationWatch(stream, KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}"))
    assert watch.check_once() == frozenset()


def test_a_rotation_is_detected(stream):
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    assert watch.check_once() == frozenset()
    _State.body = media_playlist(ROTATED_PSSH)
    assert watch.check_once() == {ROTATED_KID}


def test_a_fetch_failure_is_not_read_as_a_rotation(stream):
    # A recording must never be disturbed by a transient network wobble.
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    _State.status = 503
    assert watch.check_once() is None
    assert watch.rotations == 0


def test_failures_are_counted_and_cleared_by_any_success(stream):
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    _State.status = 503
    watch.check_once()
    assert watch.failures == 1
    _State.status = 200
    watch.check_once()
    assert watch.failures == 0
    assert watch.total_failures == 1


def test_never_having_checked_is_not_reported_as_success(stream):
    # The known failure of this fetch: the manifest URL expiring mid-run.
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    _State.status = 503
    watch.check_once()
    assert "never re-checked" in watch.summary()


def test_a_run_shorter_than_one_interval_is_not_reported_as_a_failure(stream):
    # Nothing went wrong; the interval simply had not elapsed.
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    summary = watch.summary()
    assert "shorter than one" in summary
    assert "failed" not in summary


def test_a_clean_run_is_summarised_as_held(stream):
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    watch.check_once()
    assert "held across 1" in watch.summary()


def test_a_detected_rotation_is_summarised_as_such(stream):
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY))
    watch.check_once()
    _State.body = media_playlist(ROTATED_PSSH)
    watch._heartbeat()
    assert "rotation(s) detected" in watch.summary()


def test_accepted_gaps_are_not_re_reported_as_a_rotation(stream):
    # Pre-seeded, or the first heartbeat announces a gap the operator already
    # accepted knowingly.
    _State.body = media_playlist(ROTATED_PSSH)
    watch = coverage.RotationWatch(
        stream, KeyRing.scrape(VIDEO_KEY), accepted=frozenset({ROTATED_KID})
    )
    watch._heartbeat()
    assert watch.rotations == 0
    assert "knowingly left uncovered" in watch.summary()


def test_stopping_a_watch_that_never_started_is_safe(stream):
    coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY)).stop()


def test_a_rotation_is_announced_once(stream):
    warnings: list[str] = []
    watch = coverage.RotationWatch(stream, KeyRing.scrape(VIDEO_KEY), echo=warnings.append)
    watch.check_once()
    _State.body = media_playlist(ROTATED_PSSH)
    watch._heartbeat()
    watch._heartbeat()
    assert len(warnings) == 1
