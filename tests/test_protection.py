"""PSSH reading, and the assumption that one stream means one KID."""

from __future__ import annotations

import base64
import struct

import pytest
from conftest import (
    AUDIO_KID,
    AUDIO_PSSH_BARE,
    AUDIO_PSSH_BOXED,
    PLAYREADY_SYSTEM_ID,
    VIDEO_KID,
    VIDEO_PSSH_BARE,
)

from stagecrowd_recorder.errors import ProtectionError
from stagecrowd_recorder.protection import Protection, read_kids, read_kids_quietly


def test_reads_the_kid_from_a_bare_video_header():
    assert read_kids(VIDEO_PSSH_BARE) == [VIDEO_KID]


def test_reads_the_kid_from_a_bare_audio_header():
    assert read_kids(AUDIO_PSSH_BARE) == [AUDIO_KID]


def test_a_boxed_payload_yields_the_same_kid_as_the_bare_one():
    # Wrapping a header in a pssh box must not change what it names.
    assert read_kids(AUDIO_PSSH_BOXED) == read_kids(AUDIO_PSSH_BARE)


def test_video_and_audio_name_different_kids():
    # The assumption that broke earlier designs: one stream, several KIDs.
    assert read_kids(VIDEO_PSSH_BARE) != read_kids(AUDIO_PSSH_BARE)


def test_tolerates_missing_base64_padding():
    stripped = AUDIO_PSSH_BARE.rstrip("=")
    assert read_kids(stripped) == [AUDIO_KID]


def test_tolerates_whitespace_inside_the_payload():
    wrapped = "\n".join([AUDIO_PSSH_BARE[:20], AUDIO_PSSH_BARE[20:]])
    assert read_kids(wrapped) == [AUDIO_KID]


def test_rejects_text_that_is_not_base64():
    with pytest.raises(ProtectionError, match="base64"):
        read_kids("this is not base64 !!!")


def test_rejects_a_box_belonging_to_another_drm_system():
    payload = b"\x12\x10" + bytes(16)
    box = (
        struct.pack(">I", 32 + len(payload))
        + b"pssh"
        + b"\x00\x00\x00\x00"
        + bytes.fromhex(PLAYREADY_SYSTEM_ID)
        + struct.pack(">I", len(payload))
        + payload
    )
    with pytest.raises(ProtectionError, match="not Widevine"):
        read_kids(base64.b64encode(box).decode())


def test_reads_every_repeated_key_id_in_order():
    first = bytes(range(16))
    second = bytes(range(16, 32))
    header = b"\x12\x10" + first + b"\x12\x10" + second
    assert read_kids(base64.b64encode(header).decode()) == [first.hex(), second.hex()]


def test_reads_kids_from_a_version_one_box_header():
    kid = bytes.fromhex(VIDEO_KID)
    box = (
        struct.pack(">I", 52)
        + b"pssh"
        + b"\x01\x00\x00\x00"
        + bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
        + struct.pack(">I", 1)
        + kid
        + struct.pack(">I", 0)
    )
    assert read_kids(base64.b64encode(box).decode()) == [VIDEO_KID]


def test_quiet_reader_answers_empty_for_rubbish():
    assert read_kids_quietly("garbage") == []


def test_merging_protection_keeps_payload_order_without_duplicates():
    left = Protection(payloads=(VIDEO_PSSH_BARE,), advertised=frozenset({VIDEO_KID}))
    right = Protection(
        payloads=(VIDEO_PSSH_BARE, AUDIO_PSSH_BARE), advertised=frozenset({AUDIO_KID})
    )
    merged = left.merge(right)
    assert merged.payloads == (VIDEO_PSSH_BARE, AUDIO_PSSH_BARE)
    assert merged.advertised == {VIDEO_KID, AUDIO_KID}


def test_must_cover_falls_back_to_everything_advertised():
    # With no selection known, the conservative reading is the whole set.
    protection = Protection(
        payloads=(VIDEO_PSSH_BARE,), advertised=frozenset({VIDEO_KID, AUDIO_KID})
    )
    assert protection.must_cover == {VIDEO_KID, AUDIO_KID}
    assert protection.spare == frozenset()


def test_must_cover_narrows_to_the_needed_set_when_it_is_known():
    protection = Protection(
        payloads=(VIDEO_PSSH_BARE,),
        advertised=frozenset({VIDEO_KID, AUDIO_KID}),
        needed=frozenset({VIDEO_KID}),
    )
    assert protection.must_cover == {VIDEO_KID}
    assert protection.spare == {AUDIO_KID}
