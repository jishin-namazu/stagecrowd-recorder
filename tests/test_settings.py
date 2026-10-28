"""Settings-file parsing and precedence."""

from __future__ import annotations

from pathlib import Path

from stagecrowd_recorder import settings as cfg


def test_a_friendly_alias_maps_to_its_canonical_variable():
    # The left-hand side is what somebody actually types when noting down a
    # stream in a hurry.
    values = cfg.parse_settings_file("m3u8=https://cdn.example/x.m3u8\nwv_token=abc\n")
    assert values[cfg.ENV_URL] == "https://cdn.example/x.m3u8"
    assert values[cfg.ENV_TOKEN] == "abc"


def test_every_documented_alias_resolves():
    for alias in ("url", "stream", "token", "license", "key", "keys", "cdm", "out", "hls"):
        assert cfg.canonical_name(alias).startswith(cfg.PREFIX)


def test_the_hls_alias_maps_to_the_hls_environment_variable():
    assert cfg.parse_settings_file("hls=127.0.0.1:8080\n")[cfg.ENV_HLS] == "127.0.0.1:8080"


def test_an_unknown_key_is_passed_through_unchanged():
    values = cfg.parse_settings_file("SOME_OTHER_VAR=1\n")
    assert values["SOME_OTHER_VAR"] == "1"


def test_comments_and_blank_lines_are_ignored():
    values = cfg.parse_settings_file("# a note\n\nurl=https://x/y.m3u8\n")
    assert list(values) == [cfg.ENV_URL]


def test_an_export_prefix_is_tolerated():
    values = cfg.parse_settings_file("export url=https://x/y.m3u8\n")
    assert values[cfg.ENV_URL] == "https://x/y.m3u8"


def test_one_layer_of_matching_quotes_is_stripped():
    values = cfg.parse_settings_file('url="https://x/y.m3u8"\n')
    assert values[cfg.ENV_URL] == "https://x/y.m3u8"


def test_mismatched_quotes_are_left_alone():
    values = cfg.parse_settings_file("url=\"https://x/y.m3u8'\n")
    assert values[cfg.ENV_URL] == "\"https://x/y.m3u8'"


def test_a_value_containing_equals_signs_survives():
    # JWTs and query strings both carry them.
    token = "eyJhbGciOiJIUzI1NiJ9.abc=.def=="
    values = cfg.parse_settings_file(f"wv_token={token}\n")
    assert values[cfg.ENV_TOKEN] == token


def test_a_line_without_a_separator_is_skipped():
    assert cfg.parse_settings_file("just some words\n") == {}


def test_a_real_environment_variable_is_never_overridden(tmp_path, monkeypatch):
    # Exporting a variable is a deliberate act; a file must not undo it silently.
    path = tmp_path / ".stagecrowd"
    path.write_text("url=https://from-file/x.m3u8\n", encoding="utf-8")
    monkeypatch.setenv(cfg.ENV_URL, "https://from-environment/x.m3u8")
    _, applied = cfg.load_settings_file(path)
    assert cfg.ENV_URL not in applied
    assert cfg.env(cfg.ENV_URL) == "https://from-environment/x.m3u8"


def test_a_file_fills_in_what_the_environment_leaves_unset(tmp_path, monkeypatch):
    path = tmp_path / ".stagecrowd"
    path.write_text("url=https://from-file/x.m3u8\n", encoding="utf-8")
    monkeypatch.delenv(cfg.ENV_URL, raising=False)
    _, applied = cfg.load_settings_file(path)
    assert applied[cfg.ENV_URL] == "https://from-file/x.m3u8"


def test_an_explicit_path_that_does_not_exist_is_not_replaced_by_a_search(tmp_path):
    assert cfg.find_settings_file(tmp_path / "absent") is None


def test_the_environment_names_the_settings_file(tmp_path, monkeypatch):
    path = tmp_path / "custom.conf"
    path.write_text("url=https://x/y.m3u8\n", encoding="utf-8")
    monkeypatch.setenv(cfg.ENV_SETTINGS_FILE, str(path))
    assert cfg.find_settings_file() == path


def test_the_working_directory_is_the_last_place_looked(tmp_path, monkeypatch):
    monkeypatch.delenv(cfg.ENV_SETTINGS_FILE, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".stagecrowd").write_text("url=https://x/y.m3u8\n", encoding="utf-8")
    assert cfg.find_settings_file() == tmp_path / ".stagecrowd"


def test_an_unparseable_integer_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("STC_SOME_NUMBER", "not-a-number")
    assert cfg.env_int("STC_SOME_NUMBER", 42) == 42


def test_the_default_cdm_path_is_the_container_mount_point():
    assert cfg.DEFAULT_CDM == Path("/config/device.wvd")
