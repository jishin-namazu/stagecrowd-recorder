"""The downloader command line."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import AUDIO_KEY, VIDEO_KEY

from stagecrowd_recorder.capture import LOG_NAME, PIPE_OPTIONS_ENV, CapturePlan
from stagecrowd_recorder.errors import ConfigError
from stagecrowd_recorder.keys import KeyRing
from stagecrowd_recorder.toolchain import Decryptor, Toolchain

URL = "https://cdn.example/live/playlist-hls.m3u8"


def plan(**overrides) -> CapturePlan:
    settings = dict(
        url=URL,
        keys=KeyRing.scrape(f"{VIDEO_KEY},{AUDIO_KEY}"),
        out_dir=Path("/archive/run_20260803_120000"),
        run_name="run_20260803_120000",
        tools=Toolchain(
            downloader=Path("/usr/local/bin/N_m3u8DL-RE"),
            muxer=Path("/usr/bin/ffmpeg"),
            decryptor=Path("/usr/local/bin/shaka-packager"),
        ),
    )
    settings.update(overrides)
    return CapturePlan(**settings)  # type: ignore[arg-type]


def test_the_url_is_the_first_positional_argument():
    assert plan().argv()[1] == URL


def test_each_key_becomes_its_own_flag():
    argv = plan().argv()
    assert argv.count("--key") == 2


def test_the_engine_and_its_binary_path_are_both_passed():
    # shaka is published as packager-linux-x64 and the downloader never looks
    # for that name, so an implicit sibling search fails silently.
    argv = plan().argv()
    assert "--decryption-engine" in argv
    assert argv[argv.index("--decryption-engine") + 1] == "SHAKA_PACKAGER"
    assert "--decryption-binary-path" in argv


def test_the_bento_engine_selects_its_own_value():
    tools = Toolchain(
        downloader=Path("/usr/local/bin/N_m3u8DL-RE"),
        muxer=Path("/usr/bin/ffmpeg"),
        decryptor=Path("/usr/local/bin/mp4decrypt"),
        decryptor_kind=Decryptor.BENTO,
    )
    argv = plan(tools=tools).argv()
    assert argv[argv.index("--decryption-engine") + 1] == "MP4DECRYPT"


def test_shards_are_kept_by_default():
    argv = plan().argv()
    assert argv[argv.index("--live-keep-segments") + 1] == "True"


def test_shards_can_be_discarded_explicitly():
    argv = plan(keep_shards=False).argv()
    assert argv[argv.index("--live-keep-segments") + 1] == "False"


def test_a_vod_stream_omits_the_live_flags():
    argv = plan(live=False).argv()
    assert "--live-pipe-mux" not in argv
    assert "--live-keep-segments" not in argv


def test_console_logging_is_off_by_default():
    argv = plan().argv()
    assert argv[argv.index("--log-level") + 1] == "OFF"


def test_the_log_file_is_written_even_when_the_console_is_silent():
    # Silencing the console must not lose a real failure.
    argv = plan().argv()
    assert argv[argv.index("--log-file-path") + 1].endswith(LOG_NAME)


def test_verbose_mode_restores_console_logging_and_keeps_the_file():
    argv = plan(quiet=False).argv()
    assert "--log-level" not in argv
    assert "--log-file-path" in argv


def test_the_save_name_matches_the_output_directory_name():
    argv = plan().argv()
    assert argv[argv.index("--save-name") + 1] == "run_20260803_120000"


def test_automatic_selection_is_requested():
    assert "--auto-select" in plan().argv()


def test_the_display_form_quotes_paths_with_spaces():
    tools = Toolchain(
        downloader=Path("/opt/my tools/N_m3u8DL-RE"),
        muxer=Path("/usr/bin/ffmpeg"),
        decryptor=Path("/usr/local/bin/shaka-packager"),
    )
    shown = plan(tools=tools, paced=False).display()
    # Separators are normalised by Path on Windows, so assert on the quoting.
    assert "my tools" in shown
    assert shown.startswith('"')
    assert shown.split('"')[1].endswith("N_m3u8DL-RE")


def test_the_display_form_is_a_single_copyable_line():
    assert "\n" not in plan().display()


# -- paced output ------------------------------------------------------------


def test_a_live_run_asks_the_downloader_for_paced_output():
    # Setting this variable is what makes the muxing ffmpeg run with -re, which
    # is the difference between a file that grows continuously and one that jumps
    # 8 MB whenever the CDN releases segments.
    env = plan().environment()
    assert PIPE_OPTIONS_ENV in env


def test_the_paced_destination_is_a_path_not_an_ffmpeg_flag():
    # A value starting with "-" is spliced in as raw ffmpeg arguments instead,
    # which loses control of where the output goes.
    value = plan().environment()[PIPE_OPTIONS_ENV]
    assert not value.startswith("-")
    assert value.endswith(".ts")


def test_the_muxed_path_is_named_so_the_caller_can_report_it():
    assert plan().muxed_output.name == "run_20260803_120000.ts"


def test_burst_output_leaves_the_variable_unset():
    assert PIPE_OPTIONS_ENV not in plan(paced=False).environment()


def test_a_vod_run_does_not_ask_for_pacing():
    # There is no pipe mux without --live-pipe-mux, so the variable would do
    # nothing but mislead anyone reading the environment.
    assert PIPE_OPTIONS_ENV not in plan(live=False).environment()


def test_the_environment_keeps_what_it_inherited(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    assert plan().environment()["HTTPS_PROXY"] == "http://proxy:8080"


def test_the_display_form_shows_the_pacing_variable():
    # Omitting it would make a copied command behave differently from the run.
    assert PIPE_OPTIONS_ENV in plan().display()


def test_the_display_form_omits_it_for_burst_output():
    assert PIPE_OPTIONS_ENV not in plan(paced=False).display()


# -- local HLS ---------------------------------------------------------------


def test_hls_uses_one_ffmpeg_tee_for_the_archive_and_playlist():
    hls_plan = plan(hls=True)
    options = hls_plan.environment()[PIPE_OPTIONS_ENV]
    assert options.startswith("-f tee -shortest")
    assert "[f=mpegts:onfail=abort]" in options
    assert plan().muxed_output.as_posix() in options
    assert "[f=hls:onfail=ignore" in options
    assert hls_plan.hls_playlist.as_posix() in options


def test_hls_keeps_a_short_rolling_playlist():
    options = plan(hls=True).pipe_options
    assert "hls_time=2" in options
    assert "hls_list_size=6" in options
    assert "delete_segments" in options
    assert "omit_endlist" in options
    assert "temp_file" in options


def test_an_hls_failure_is_configured_not_to_abort_the_archive():
    options = plan(hls=True).pipe_options
    assert "onfail=abort" in options
    assert "onfail=ignore" in options


def test_burst_output_cannot_be_combined_with_hls():
    with pytest.raises(ConfigError, match="burst-output"):
        plan(hls=True, paced=False)


def test_a_vod_cannot_be_served_as_live_hls():
    with pytest.raises(ConfigError, match="only available for a live stream"):
        plan(hls=True, live=False)


def test_the_hls_output_has_its_own_directory():
    hls_plan = plan(hls=True)
    assert hls_plan.hls_directory == hls_plan.out_dir / "hls"
    assert hls_plan.hls_playlist.name == "live.m3u8"


def test_tee_separators_in_the_output_path_are_rejected_for_hls():
    with pytest.raises(ConfigError, match="cannot quote safely"):
        plan(hls=True, out_dir=Path("bad|path"))


def test_the_display_form_includes_the_hls_playlist():
    shown = plan(hls=True).display()
    assert "live.m3u8" in shown
    assert "-f tee" in shown
