"""A small HTTP server for the rolling HLS output produced during capture."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from .errors import ConfigError

PLAYLIST_NAME = "live.m3u8"
DEFAULT_PORT = 8080


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, value: str) -> "Endpoint":
        value = value.strip()
        try:
            parsed = urlsplit(f"//{value}")
            port = parsed.port or DEFAULT_PORT
        except ValueError as exc:
            raise ConfigError(
                f"invalid HLS address: {value}",
                remedy="Use HOST:PORT, for example 127.0.0.1:8080.",
            ) from exc
        if (
            not parsed.hostname
            or ":" in parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not 1 <= port <= 65535
        ):
            raise ConfigError(
                f"invalid HLS address: {value}",
                remedy="Use HOST:PORT, for example 127.0.0.1:8080.",
            )
        return cls(parsed.hostname, port)

    @property
    def public_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        shown = f"[{host}]" if ":" in host else host
        return f"http://{shown}:{self.port}/{PLAYLIST_NAME}"


class _Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
    }

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path.partition("?")[0].endswith(".m3u8"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass(slots=True)
class HlsServer:
    directory: Path
    endpoint: Endpoint
    _server: _Server | None = field(default=None, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)

    @property
    def url(self) -> str:
        return self.endpoint.public_url

    def start(self) -> None:
        if self._server is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        handler = partial(_Handler, directory=str(self.directory.resolve()))
        try:
            server = _Server((self.endpoint.host, self.endpoint.port), handler)
        except OSError as exc:
            raise ConfigError(
                f"could not serve HLS on {self.endpoint.host}:{self.endpoint.port}: {exc}",
                remedy="Stop the process using that port, or choose another address with --hls.",
            ) from exc
        self._server = server
        self._thread = Thread(target=server.serve_forever, name="hls-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._server = None
        self._thread = None
