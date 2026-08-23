"""The one place this package talks HTTP.

Standard library only. The requests dependency bought convenience this codebase
never uses — no sessions with cookie jars, no adapters, no retries policy — and
it has to be present inside the image for the recorder path to start at all.
urllib carries the same two verbs with an install step of zero.

Both helpers return bodies as bytes and leave decoding to the caller: a license
response is protobuf, a playlist is text, and a layer that guesses which one it
is holding guesses wrong on the one that matters.
"""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

DEFAULT_UA = "Mozilla/5.0"
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class TransportError(Exception):
    """The request never produced an HTTP reply."""


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = encoding.lower().strip()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _send(request: urllib.request.Request, timeout: float) -> Reply:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            body = response.read()
            return Reply(response.status, _decompress(body, headers.get("content-encoding", "")), headers)
    except urllib.error.HTTPError as exc:
        # A 4xx from a license server is data, not a transport failure: the body
        # explains which of several indistinguishable problems occurred, so it
        # has to survive to the caller instead of becoming an exception string.
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        body = exc.read() or b""
        return Reply(exc.code, _decompress(body, headers.get("content-encoding", "")), headers)
    except urllib.error.URLError as exc:
        raise TransportError(str(exc.reason)) from exc
    except OSError as exc:
        raise TransportError(str(exc)) from exc


def get(url: str, *, headers: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Reply:
    merged = {"User-Agent": DEFAULT_UA, "Accept": "*/*", **(headers or {})}
    return _send(urllib.request.Request(url, headers=merged, method="GET"), timeout)


def head(url: str, *, headers: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Reply:
    """Fetch response metadata without transferring the media body."""
    merged = {"User-Agent": DEFAULT_UA, "Accept": "*/*", **(headers or {})}
    return _send(urllib.request.Request(url, headers=merged, method="HEAD"), timeout)


def post(
    url: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Reply:
    merged = {"User-Agent": DEFAULT_UA, **(headers or {})}
    return _send(urllib.request.Request(url, data=body, headers=merged, method="POST"), timeout)
