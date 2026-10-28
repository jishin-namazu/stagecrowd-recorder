"""The key normalisation contract, and the coverage comparison it enables."""

from __future__ import annotations

import pytest
from conftest import AUDIO_KEY, AUDIO_KID, SPARE_KID, VIDEO_KEY, VIDEO_KID

from stagecrowd_recorder.keys import ContentKey, KeyRing, normalise_kid


def test_a_key_is_normalised_to_lowercase():
    key = ContentKey(VIDEO_KID.upper(), "11D266834186CEAD9CBAB298325BD542")
    assert key.kid == VIDEO_KID
    assert key.key == "11d266834186cead9cbab298325bd542"


def test_a_dashed_uuid_kid_normalises_to_bare_hex():
    # The same track named two ways must compare equal.
    dashed = "9322a9f3-c78f-3925-9905-3dc65262eb3c"
    assert normalise_kid(dashed) == VIDEO_KID


def test_rejects_a_kid_that_is_too_short():
    with pytest.raises(ValueError):
        ContentKey("abcd", "11d266834186cead9cbab298325bd542")


def test_rejects_a_kid_that_is_not_hex():
    with pytest.raises(ValueError):
        ContentKey("z" * 32, "11d266834186cead9cbab298325bd542")


def test_a_key_renders_as_kid_colon_key():
    assert str(ContentKey.parse(VIDEO_KEY)) == VIDEO_KEY


def test_a_ring_scrapes_pairs_out_of_arbitrary_text():
    text = f"here are the keys:\n  {VIDEO_KEY}\n  {AUDIO_KEY}\nthat is all"
    ring = KeyRing.scrape(text)
    assert ring.kids == {VIDEO_KID, AUDIO_KID}


def test_a_ring_scrapes_a_comma_separated_list():
    ring = KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}")
    assert len(ring) == 2


def test_a_ring_round_trips_through_its_string_form():
    ring = KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}")
    assert KeyRing.scrape(str(ring)) == ring


def test_scraping_prose_finds_nothing():
    assert not KeyRing.scrape("no keys were harmed in the making of this test")


def test_a_ring_reports_no_gap_when_every_kid_is_held():
    ring = KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}")
    assert ring.gap({VIDEO_KID, AUDIO_KID}) == frozenset()


def test_a_ring_reports_the_kid_it_lacks():
    ring = KeyRing.scrape(VIDEO_KEY)
    assert ring.gap({VIDEO_KID, AUDIO_KID}) == {AUDIO_KID}


def test_holding_extra_keys_is_not_a_gap():
    ring = KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}")
    assert ring.gap({VIDEO_KID}) == frozenset()


def test_a_gap_compares_regardless_of_kid_spelling():
    ring = KeyRing.scrape(VIDEO_KEY)
    dashed = "9322a9f3-c78f-3925-9905-3dc65262eb3c"
    assert ring.gap({dashed}) == frozenset()


def test_a_repeated_kid_keeps_the_key_already_held():
    first = ContentKey(VIDEO_KID, "11d266834186cead9cbab298325bd542")
    second = ContentKey(VIDEO_KID, "00000000000000000000000000000000")
    ring = KeyRing([first]).with_keys([second])
    assert list(ring) == [first]


def test_an_unrelated_kid_does_not_satisfy_coverage():
    ring = KeyRing.scrape(f"{SPARE_KID}:{'a' * 32}")
    assert ring.gap({VIDEO_KID, AUDIO_KID}) == {VIDEO_KID, AUDIO_KID}
