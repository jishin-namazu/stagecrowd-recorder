"""Binary discovery and the proving step.

The fixtures build real, runnable programs instead of mocking the probe: what is
being tested is whether the operating system will execute a file, and only the
operating system can answer that.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from stagecrowd_recorder import toolchain
from stagecrowd_recorder.errors import ToolError
from stagecrowd_recorder.toolchain import Decryptor, Toolchain

WINDOWS = sys.platform == "win32"
PROGRAM_SUFFIX = ".cmd" if WINDOWS else ""
BROKEN_SUFFIX = ".exe" if WINDOWS else ""


def make_program(
    directory: Path, name: str, *, version: str = "version 1.2.3", exit_code: int = 0
) -> Path:
    path = directory / f"{name}{PROGRAM_SUFFIX}"
    if WINDOWS:
        body = f"@echo {version}\r\n"
        if exit_code:
            body += f"@exit /b {exit_code}\r\n"
    else:
        body = f"#!/bin/sh\necho {version}\n"
        if exit_code:
            body += f"exit {exit_code}\n"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_truncated_download(directory: Path, name: str) -> Path:
    """A real file header with no body — the shape a dropped connection leaves."""
    path = directory / f"{name}{BROKEN_SUFFIX}"
    header = b"MZ" + b"\x00" * 10 if WINDOWS else b"\x7fELF" + b"\x00" * 8
    path.write_bytes(header)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_a_working_binary_probes_cleanly(tmp_path):
    result = toolchain.probe(make_program(tmp_path, "tool"))
    assert result.runnable
    assert "1.2.3" in result.detail


def test_a_truncated_download_is_detected(tmp_path):
    # Every path-based check reports this file as present. Only running it says
    # otherwise — and a truncated tool fails at the moment capture starts.
    assert not toolchain.probe(make_truncated_download(tmp_path, "tool")).runnable


def test_a_file_that_is_not_a_program_at_all_is_detected(tmp_path):
    plain = tmp_path / "tool.txt"
    plain.write_text("just some text")
    assert not toolchain.probe(plain).runnable


def test_a_missing_file_does_not_crash(tmp_path):
    assert not toolchain.probe(tmp_path / "absent").runnable


def test_a_non_zero_exit_is_not_a_failure(tmp_path):
    # mp4decrypt has no --version and answers with usage text and exit 1.
    program = make_program(tmp_path, "tool", version="usage: tool", exit_code=1)
    assert toolchain.probe(program).runnable


def test_verify_names_the_binaries_that_are_missing():
    with pytest.raises(ToolError, match="missing required binaries"):
        Toolchain(downloader=None, muxer=None, decryptor=None).verify()


def test_verify_refuses_a_binary_that_cannot_run(tmp_path):
    tools = Toolchain(
        downloader=make_program(tmp_path, "N_m3u8DL-RE"),
        muxer=make_program(tmp_path, "ffmpeg"),
        decryptor=make_truncated_download(tmp_path, "shaka-packager"),
    )
    with pytest.raises(ToolError) as refused:
        tools.verify()
    assert "not runnable" in refused.value.message
    # And it must say how to fix it.
    assert "Rebuild" in (refused.value.remedy or "")


def test_verify_returns_the_versions_it_already_paid_to_learn(tmp_path):
    tools = Toolchain(
        downloader=make_program(tmp_path, "N_m3u8DL-RE"),
        muxer=make_program(tmp_path, "ffmpeg"),
        decryptor=make_program(tmp_path, "shaka-packager"),
    )
    assert set(tools.verify()) == {"N_m3u8DL-RE", "ffmpeg", "shaka-packager"}


def test_an_environment_override_wins_when_the_path_exists(tmp_path, monkeypatch):
    program = make_program(tmp_path, "custom-packager")
    monkeypatch.setenv("STC_SHAKA", str(program))
    assert toolchain.find("shaka-packager", env="STC_SHAKA") == program


def test_an_override_pointing_nowhere_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("STC_SHAKA", str(tmp_path / "absent"))
    assert toolchain.find("definitely-not-a-real-binary", env="STC_SHAKA") is None


def test_the_shaka_engine_accepts_the_upstream_asset_name():
    # A copy downloaded by hand should work without being renamed first.
    assert "packager-linux-x64" in Decryptor.SHAKA.candidates


def test_the_engine_renders_as_the_value_the_user_types():
    # argparse renders choices with str(); the enum repr shows the wrong text.
    assert str(Decryptor.SHAKA) == "SHAKA_PACKAGER"
    assert str(Decryptor.BENTO) == "MP4DECRYPT"


def test_ffprobe_is_looked_for_beside_ffmpeg(tmp_path):
    muxer = make_program(tmp_path, "ffmpeg")
    prober = make_program(tmp_path, "ffprobe")
    tools = Toolchain(downloader=None, muxer=muxer, decryptor=None)
    assert tools.prober == prober
