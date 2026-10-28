"""Local HTTP serving for the rolling live playlist."""

from __future__ import annotations

import socket
from pathlib import Path
from urllib.request import urlopen

import pytest

from stagecrowd_recorder.errors import ConfigError
from stagecrowd_recorder.hls import Endpoint, HlsServer


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_the_default_form_parses_a_loopback_endpoint():
    assert Endpoint.parse("127.0.0.1:8080") == Endpoint("127.0.0.1", 8080)


def test_an_omitted_port_uses_8080():
    assert Endpoint.parse("127.0.0.1").port == 8080


@pytest.mark.parametrize("value", ["", "http://127.0.0.1:8080", "127.0.0.1:bad", "127.0.0.1:70000"])
def test_invalid_bind_addresses_are_configuration_errors(value):
    with pytest.raises(ConfigError, match="invalid HLS address"):
        Endpoint.parse(value)


def test_a_wildcard_bind_is_advertised_as_loopback():
    assert Endpoint.parse("0.0.0.0:8080").public_url == "http://127.0.0.1:8080/live.m3u8"


def test_the_server_serves_the_playlist_with_hls_headers(tmp_path: Path):
    (tmp_path / "live.m3u8").write_bytes(b"#EXTM3U\n")
    endpoint = Endpoint("127.0.0.1", available_port())
    server = HlsServer(tmp_path, endpoint)
    server.start()
    try:
        with urlopen(server.url, timeout=2) as response:
            assert response.read() == b"#EXTM3U\n"
            assert response.headers.get_content_type() == "application/vnd.apple.mpegurl"
            assert response.headers["Cache-Control"].startswith("no-store")
            assert response.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.stop()


def test_stopping_a_server_that_never_started_is_safe(tmp_path: Path):
    HlsServer(tmp_path, Endpoint("127.0.0.1", available_port())).stop()


def test_an_occupied_port_is_reported_cleanly(tmp_path: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        endpoint = Endpoint("127.0.0.1", listener.getsockname()[1])
        with pytest.raises(ConfigError, match="could not serve HLS"):
            HlsServer(tmp_path, endpoint).start()
