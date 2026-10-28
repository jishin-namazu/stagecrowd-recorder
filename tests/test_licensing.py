"""The license target, header handling, and the response reader."""

from __future__ import annotations

import base64
import json

import pytest

from stagecrowd_recorder import licensing, netio
from stagecrowd_recorder.errors import LicenseError
from stagecrowd_recorder.licensing import BRIGHTCOVE_LICENSE, LicenseTarget

TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"


# -- addressing --------------------------------------------------------------


def test_a_bare_token_builds_the_endpoint_url():
    target = LicenseTarget.from_token(TOKEN)
    assert target.url == f"{BRIGHTCOVE_LICENSE}?token={TOKEN}"


def test_a_token_that_is_already_a_url_is_used_as_given():
    given = "https://license.example/lic/wv?token=abc"
    assert LicenseTarget.from_token(given).url == given


def test_a_token_containing_equals_signs_survives():
    # JWTs and query strings are full of them.
    token = "abc==def=="
    assert LicenseTarget.from_token(token).url.endswith(token)


def test_a_relative_license_url_is_refused():
    with pytest.raises(LicenseError, match="must be absolute"):
        LicenseTarget.from_url("license.example/lic/wv")


def test_the_token_is_elided_when_the_url_is_printed():
    shown = LicenseTarget.from_token(TOKEN).redacted()
    assert TOKEN not in shown
    assert "<redacted>" in shown


def test_the_default_headers_are_the_minimal_set():
    # The server authorises on the token and ignores origin, referer and
    # user-agent; sending a minimal set keeps replay reproducible.
    assert LicenseTarget.from_token(TOKEN).headers == {
        "Content-Type": "application/octet-stream"
    }


# -- headers -----------------------------------------------------------------


def test_entity_headers_a_client_must_compute_are_stripped():
    cleaned = licensing.sanitise_headers(
        {
            "Content-Type": "application/octet-stream",
            "Content-Length": "1234",
            "Accept-Encoding": "gzip",
            "Host": "license.example",
        }
    )
    assert cleaned == {"Content-Type": "application/octet-stream"}


def test_http2_pseudo_headers_are_stripped():
    cleaned = licensing.sanitise_headers(
        {":method": "POST", ":authority": "x", "Origin": "https://example"}
    )
    assert cleaned == {"Origin": "https://example"}


def test_stripping_everything_falls_back_to_the_minimal_set():
    assert licensing.sanitise_headers({"Host": "x"}) == {
        "Content-Type": "application/octet-stream"
    }


def test_a_headers_file_is_read_line_by_line(tmp_path):
    path = tmp_path / "headers.txt"
    path.write_text(
        "Content-Type: application/octet-stream\nOrigin: https://example\n", encoding="utf-8"
    )
    assert licensing.read_header_file(path) == {
        "Content-Type": "application/octet-stream",
        "Origin": "https://example",
    }


def test_a_header_value_containing_a_colon_survives(tmp_path):
    path = tmp_path / "headers.txt"
    path.write_text("Referer: https://example.com/watch\n", encoding="utf-8")
    assert licensing.read_header_file(path)["Referer"] == "https://example.com/watch"


def test_a_quoted_header_value_is_unwrapped_once(tmp_path):
    path = tmp_path / "headers.txt"
    path.write_text('Origin: "https://example"\n', encoding="utf-8")
    assert licensing.read_header_file(path)["Origin"] == "https://example"


def test_a_headers_file_with_nothing_usable_is_refused(tmp_path):
    path = tmp_path / "headers.txt"
    path.write_text("# only a comment\n\n", encoding="utf-8")
    with pytest.raises(LicenseError, match="no headers"):
        licensing.read_header_file(path)


def test_a_missing_headers_file_is_refused(tmp_path):
    with pytest.raises(LicenseError, match="cannot read"):
        licensing.read_header_file(tmp_path / "absent.txt")


# -- reading the response ----------------------------------------------------


def test_raw_protobuf_is_passed_through_unchanged():
    body = b"\x08\x01\x12\x10" + bytes(16)
    assert licensing._payload_from_reply(netio.Reply(200, body)) == body


def test_a_json_wrapped_license_is_unwrapped():
    inner = b"\x08\x01license-bytes"
    body = json.dumps({"license": base64.b64encode(inner).decode()}).encode()
    assert licensing._payload_from_reply(netio.Reply(200, body)) == inner


def test_a_json_body_under_the_response_key_is_unwrapped():
    inner = b"\x08\x02more-bytes"
    body = json.dumps({"response": base64.b64encode(inner).decode()}).encode()
    assert licensing._payload_from_reply(netio.Reply(200, body)) == inner


def test_a_json_body_with_no_recognised_key_is_left_alone():
    body = json.dumps({"error": "nope"}).encode()
    assert licensing._payload_from_reply(netio.Reply(200, body)) == body


# -- diagnosing failures -----------------------------------------------------


def test_an_html_404_is_diagnosed_as_the_wrong_endpoint():
    remedy = licensing._explain_http_failure(404, "<!DOCTYPE html><html>404</html>")
    assert "not a license endpoint" in remedy


def test_a_401_is_diagnosed_as_an_expired_token():
    assert "expires" in licensing._explain_http_failure(401, "denied")


def test_a_403_is_diagnosed_as_a_refused_token():
    assert "refused" in licensing._explain_http_failure(403, "denied")


def test_a_400_is_diagnosed_as_a_pssh_token_mismatch():
    assert "different assets" in licensing._explain_http_failure(400, "bad request")


def test_a_500_is_diagnosed_as_a_server_failure():
    assert "license server failed" in licensing._explain_http_failure(503, "oops")


# -- aggregating across payloads ---------------------------------------------


def test_no_pssh_at_all_is_refused_before_any_request():
    with pytest.raises(LicenseError, match="no Widevine PSSH"):
        licensing.acquire((), frozenset(), LicenseTarget.from_token(TOKEN), tmp_cdm())


def tmp_cdm():
    from pathlib import Path

    return Path("device.wvd")
