"""Shared fixtures.

The payloads here are real: PSSH blobs, KIDs and a master playlist shaped like
the streams this tool was built for. Synthetic fixtures agree with whatever the
parser happens to do; these disagree when it is wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Bare Widevine CENC headers — the protobuf with no pssh box around it.
VIDEO_PSSH_BARE = (
    "EhCTIqnzx485JZkFPcZSYus8IiRleUpoYzNObGRFbGtJam9pTmpNNE5qVXdNRFUyTnpFeE1pSjk4AEjzxombBg=="
)
AUDIO_PSSH_BARE = (
    "EhBuSKwGvL0+MLvk9DFrh2HsIiRleUpoYzNObGRFbGtJam9pTmpNNE5qVXdNRFUyTnpFeE1pSjk4AEjzxombBg=="
)
# The same audio header wrapped in a version 0 pssh box.
AUDIO_PSSH_BOXED = (
    "AAAAYHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAAEASEG5IrAa8vT4wu+T0MWuHYewiJGV5"
    "SmhjM05sZEVsa0lqb2lOak00TmpVd01EVTJOekV4TWlKOTgASPPGiZsG"
)
# A third KID, declared by a low-bitrate variant that selection never picks.
SPARE_PSSH_BARE = "EhBRTqXEyPQ5Fps8HqDXcOTZ"

VIDEO_KID = "9322a9f3c78f392599053dc65262eb3c"
AUDIO_KID = "6e48ac06bcbd3e30bbe4f4316b8761ec"
SPARE_KID = "514ea5c4c8f439169b3c1ea0d770e4d9"

VIDEO_KEY = f"{VIDEO_KID}:11d266834186cead9cbab298325bd542"
AUDIO_KEY = f"{AUDIO_KID}:1e8a2baba24bc73f1848466e404de37f"

PLAYREADY_SYSTEM_ID = "9a04f07998404286ab92e65be0885f95"


def session_key(pssh: str) -> str:
    return (
        "#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES-CTR,"
        'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1",'
        f'URI="data:text/plain;base64,{pssh}"'
    )


def media_key(pssh: str) -> str:
    return (
        "#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,"
        'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1",'
        f'URI="data:text/plain;base64,{pssh}"'
    )


MASTER_PLAYLIST = "\n".join(
    [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        session_key(SPARE_PSSH_BARE),
        session_key(VIDEO_PSSH_BARE),
        session_key(AUDIO_PSSH_BARE),
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio0",NAME="eng",LANGUAGE="eng",'
        'DEFAULT=NO,AUTOSELECT=YES,URI="chunklist_audio0.m3u8"',
        "#EXT-X-STREAM-INF:BANDWIDTH=700000,AVERAGE-BANDWIDTH=640000,"
        'RESOLUTION=640x360,CODECS="mp4a.40.2,avc1.4d401e",AUDIO="audio0"',
        "chunklist_0.m3u8",
        "#EXT-X-STREAM-INF:BANDWIDTH=5640800,AVERAGE-BANDWIDTH=5240000,"
        'RESOLUTION=1920x1080,CODECS="mp4a.40.2,avc1.640028",AUDIO="audio0"',
        "chunklist_3.m3u8",
        "",
    ]
)


def media_playlist(pssh: str, *, first: int = 1183, count: int = 3, live: bool = True) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-TARGETDURATION:6",
        f"#EXT-X-MEDIA-SEQUENCE:{first}",
        media_key(pssh),
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for index in range(count):
        lines += ["#EXTINF:6.000,", f"segment_{first + index}.m4s"]
    if not live:
        lines.append("#EXT-X-ENDLIST")
    lines.append("")
    return "\n".join(lines)


def fmp4_init(handler: bytes) -> bytes:
    """A minimal fMP4 init segment carrying only the hdlr box that matters."""
    body = b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + handler + b"\x00" * 12
    box = b"hdlr" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + handler
    padding = b"\x00" * 8
    return b"\x00\x00\x00\x20ftypiso6" + padding + b"\x00\x00\x00\x40moov" + box + body


@pytest.fixture
def video_init() -> bytes:
    return fmp4_init(b"vide")


@pytest.fixture
def audio_init() -> bytes:
    return fmp4_init(b"soun")
