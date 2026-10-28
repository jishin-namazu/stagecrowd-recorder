"""Argument parsing, precedence, and dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import AUDIO_KEY, VIDEO_KEY

from stagecrowd_recorder import cli
from stagecrowd_recorder.settings import (
    DEFAULT_HLS_ADDRESS,
    ENV_HLS,
    ENV_KEYS,
    ENV_LICENSE_URL,
    ENV_SETTINGS_FILE,
    ENV_TOKEN,
    ENV_URL,
    SETTINGS_NAMES,
    SETTINGS_SEARCH_HELP,
)
from stagecrowd_recorder.toolchain import Decryptor


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def settings_for(*argv):
    return cli._settings_from(parse(*argv))


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit):
        parse()


def test_repeated_key_flags_accumulate():
    settings = settings_for("capture", "--url", "https://x/y.m3u8", "--key", VIDEO_KEY, "--key", AUDIO_KEY)
    assert len(settings.literal_keys) == 2


def test_a_comma_separated_key_list_is_accepted():
    settings = settings_for("capture", "--url", "https://x/y.m3u8", "--key", f"{VIDEO_KEY},{AUDIO_KEY}")
    from stagecrowd_recorder.keys import KeyRing

    assert len(KeyRing.scrape(",".join(settings.literal_keys))) == 2


def test_the_negated_flags_map_to_positive_settings():
    settings = settings_for(
        "capture",
        "--url",
        "https://x/y.m3u8",
        "--discard-shards",
        "--allow-partial-keys",
        "--verbose-downloader",
        "--quiet-shards",
        "--no-shard-log",
    )
    assert settings.keep_shards is False
    assert settings.strict_coverage is False
    assert settings.quiet_downloader is False
    assert settings.shard_echo is False
    assert settings.shard_log is False


def test_the_defaults_keep_shards_and_enforce_coverage():
    settings = settings_for("capture", "--url", "https://x/y.m3u8")
    assert settings.keep_shards is True
    assert settings.strict_coverage is True
    assert settings.quiet_downloader is True
    # Paced by default: the muxed file is meant to be playable while it is written.
    assert settings.paced_output is True


def test_the_decryptor_choice_is_parsed_as_the_enum():
    settings = settings_for("capture", "--url", "https://x/y.m3u8", "--decryptor", "MP4DECRYPT")
    assert settings.decryptor is Decryptor.BENTO


def test_a_license_url_and_a_token_are_both_carried():
    settings = settings_for(
        "capture", "--url", "https://x/y.m3u8", "--license-url", "https://lic/wv?token=a"
    )
    assert settings.license_url == "https://lic/wv?token=a"
    assert settings.has_license_route


def test_no_license_route_is_reported_as_such():
    assert not settings_for("capture", "--url", "https://x/y.m3u8").has_license_route


def test_a_settings_file_is_read_out_of_argv_before_the_parser_exists(tmp_path):
    # The chicken and egg: argparse captures its defaults at construction, so a
    # value read from the environment afterwards arrives too late.
    path = tmp_path / "custom.conf"
    assert cli._settings_file_from_argv(["--settings", str(path), "capture"]) == path
    assert cli._settings_file_from_argv([f"--settings={path}", "capture"]) == path


def test_a_settings_file_survives_the_abbreviations_argparse_accepts(tmp_path):
    # argparse accepts unambiguous prefixes, so the sniffer must too.
    path = tmp_path / "custom.conf"
    assert cli._settings_file_from_argv(["--set", str(path), "capture"]) == path
    assert cli._settings_file_from_argv(["capture", "--set", str(path)]) == path


def test_the_file_is_found_even_where_the_real_parser_rejects_the_flag(tmp_path):
    # rebuild and probe do not register --settings; the sniffer still finds it.
    path = tmp_path / "custom.conf"
    assert cli._settings_file_from_argv(["probe", "--settings", str(path)]) == path
    assert cli._settings_file_from_argv(["rebuild", "d", "--settings", str(path)]) == path


def test_a_malformed_argv_leaves_the_diagnostic_to_the_real_parser(capsys):
    # A bare --settings must not make the sniffer print its own usage and exit.
    assert cli._settings_file_from_argv(["--settings"]) is None
    assert capsys.readouterr().err == ""


def test_the_search_order_help_names_every_place_that_is_searched():
    # Rewriting SETTINGS_NAMES or ENV_SETTINGS_FILE must rewrite the prose too.
    for name in SETTINGS_NAMES:
        assert f"./{name}" in SETTINGS_SEARCH_HELP
    assert f"${ENV_SETTINGS_FILE}" in SETTINGS_SEARCH_HELP


def test_the_search_order_help_does_not_promise_a_fallback():
    # find_settings_file stops at $STC_SETTINGS when it is set, so the prose
    # must not read as a chain that continues into the working directory.
    assert "when set" in SETTINGS_SEARCH_HELP
    assert "else ./" not in SETTINGS_SEARCH_HELP


def test_the_help_text_states_the_one_search_order():
    # argparse wraps long help, so compare with whitespace collapsed.
    help_text = " ".join(cli.build_parser().format_help().split())
    assert " ".join(SETTINGS_SEARCH_HELP.split()) in help_text


def test_no_settings_flag_yields_no_explicit_path():
    assert cli._settings_file_from_argv(["capture", "--url", "x"]) is None


def test_the_environment_supplies_a_default_url(monkeypatch):
    monkeypatch.setenv(ENV_URL, "https://from-environment/x.m3u8")
    # The parser reads defaults at construction, so build it after the change.
    settings = cli._settings_from(cli.build_parser().parse_args(["capture"]))
    assert settings.url == "https://from-environment/x.m3u8"


def test_the_command_line_beats_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_URL, "https://from-environment/x.m3u8")
    parser = cli.build_parser()
    settings = cli._settings_from(parser.parse_args(["capture", "--url", "https://explicit/x.m3u8"]))
    assert settings.url == "https://explicit/x.m3u8"


def test_the_environment_supplies_a_default_token(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN, "abc123")
    settings = cli._settings_from(cli.build_parser().parse_args(["capture"]))
    assert settings.license_token == "abc123"


def test_the_rebuild_command_takes_a_target():
    args = parse("rebuild", "archive/run_x")
    assert args.target == "archive/run_x"
    assert args.output is None


def test_the_rebuild_command_takes_a_destination():
    args = parse("rebuild", "archive/run_x", "-o", "clean.mkv")
    assert args.output == "clean.mkv"


def test_the_backfill_command_has_a_safe_default_rate():
    args = parse("backfill", "archive/run_x.out")
    assert args.target == "archive/run_x.out"
    assert args.rate_limit == 3 * 1024**2
    assert args.scan_only is False
    assert args.include_live_tail is False


def test_backfill_can_only_scan_and_can_include_the_live_tail():
    args = parse(
        "backfill",
        "archive/run_x.out",
        "--rate-limit",
        "512K",
        "--scan-only",
        "--include-live-tail",
    )
    assert args.rate_limit == 512 * 1024
    assert args.scan_only is True
    assert args.include_live_tail is True


def test_scan_only_reports_missing_indexes_without_claiming_they_were_recovered(
    monkeypatch,
):
    result = cli.backfill_mod.BackfillResult(
        active=False,
        tracks=(cli.backfill_mod.TrackResult("video", 10, 20, 20, 10, 1, 0),),
    )
    monkeypatch.setattr(cli.backfill_mod, "run", lambda *args, **kwargs: result)
    lines = []
    monkeypatch.setattr(cli.console, "stage", lambda text: None)
    monkeypatch.setattr(cli.console, "detail", lines.append)
    monkeypatch.setattr(cli.console, "good", lines.append)
    assert cli.main(["backfill", "archive/run_x.out", "--scan-only"]) == 0
    output = "\n".join(lines)
    assert "missing 1; recovered 0" in output
    assert "CDN scan complete" in output


def test_the_probe_command_needs_no_stream():
    args = parse("probe")
    assert args.command == "probe"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No settings file and no inherited variables to fall back on."""
    monkeypatch.chdir(tmp_path)
    for name in (ENV_URL, ENV_TOKEN, ENV_SETTINGS_FILE, ENV_KEYS, ENV_LICENSE_URL, ENV_HLS):
        monkeypatch.delenv(name, raising=False)


def test_a_missing_url_is_reported_as_an_operator_error(capsys, isolated):
    status = cli.main(["capture"])
    assert status == 2
    assert "no stream URL" in capsys.readouterr().err


def test_no_keys_and_no_license_route_is_reported(capsys, isolated):
    status = cli.main(["keys", "--url", "http://127.0.0.1:1/nothing.m3u8"])
    assert status == 2


def test_a_settings_file_named_env_is_discovered(tmp_path, monkeypatch):
    # The surrounding tooling writes .env; both names are read the same way.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_SETTINGS_FILE, raising=False)
    (tmp_path / ".env").write_text("m3u8=https://x/y.m3u8\n", encoding="utf-8")
    from stagecrowd_recorder import settings as cfg

    assert cfg.find_settings_file() == tmp_path / ".env"


def test_the_dedicated_name_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_SETTINGS_FILE, raising=False)
    (tmp_path / ".env").write_text("m3u8=https://env/y.m3u8\n", encoding="utf-8")
    (tmp_path / ".stagecrowd").write_text("m3u8=https://dedicated/y.m3u8\n", encoding="utf-8")
    from stagecrowd_recorder import settings as cfg

    assert cfg.find_settings_file() == tmp_path / ".stagecrowd"


def test_burst_output_turns_pacing_off():
    settings = settings_for("capture", "--url", "https://x/y.m3u8", "--burst-output")
    assert settings.paced_output is False


def test_hls_is_disabled_by_default():
    assert settings_for("capture", "--url", "https://x/y.m3u8").hls_address == ""


def test_a_bare_hls_flag_uses_the_local_http_address():
    settings = settings_for("capture", "--url", "https://x/y.m3u8", "--hls")
    assert settings.hls_address == DEFAULT_HLS_ADDRESS


def test_an_explicit_hls_bind_address_is_carried():
    address = "0.0.0.0:9090"
    settings = settings_for(
        "capture", "--url", "https://x/y.m3u8", "--hls", address
    )
    assert settings.hls_address == address


def test_the_environment_supplies_a_default_hls_address(monkeypatch):
    address = "127.0.0.1:9090"
    monkeypatch.setenv(ENV_HLS, address)
    settings = cli._settings_from(cli.build_parser().parse_args(["capture"]))
    assert settings.hls_address == address
