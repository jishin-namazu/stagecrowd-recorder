"""Building and running the downloader command.

The recording itself is N_m3u8DL-RE's job: it fetches the playlist, keeps up
with a sliding window, hands each shard to the decryptor and muxes the result.
This module's whole responsibility is to construct that invocation correctly and
to get out of the way while it runs.

Getting out of the way is literal. The child is not given pipes; it shares the
console. That is what lets Ctrl+C reach it directly so it can run its own
drain-and-finalise path, which is the only way it produces a playable file.

One environment variable does more than its name suggests. Under
``--live-pipe-mux`` the downloader feeds named pipes to ffmpeg, and when
``RE_LIVE_PIPE_OPTIONS`` is set it adds ``-re`` to that ffmpeg invocation —
paced output, at playback rate, instead of writing each batch as fast as the
disk accepts it. That single flag is the difference between a file that grows in
8 MB steps every twelve seconds and one that grows continuously, which is what
decides whether a player can be pointed at the file while it is still being
written. When HLS serving is configured, the same ffmpeg process uses its tee
muxer to keep that archive while maintaining a short rolling live playlist.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import CaptureError, ConfigError
from .keys import KeyRing
from .toolchain import Toolchain

LOG_NAME = "downloader.log"

# Read by the downloader, not by us. Its presence is what makes the muxing
# ffmpeg run with -re; its value is where that ffmpeg writes.
PIPE_OPTIONS_ENV = "RE_LIVE_PIPE_OPTIONS"

_UNSAFE_TEE_CHARACTERS = frozenset("\r\n\"'|")


def _tee_path(path: Path) -> str:
    value = path.as_posix()
    if any(character in _UNSAFE_TEE_CHARACTERS for character in value):
        raise ConfigError(
            "the output path contains a character ffmpeg's tee output cannot quote safely",
            remedy="Choose an --out path without quotes, line breaks, or |.",
        )
    return value


@dataclass(frozen=True, slots=True)
class CapturePlan:
    url: str
    keys: KeyRing
    out_dir: Path
    run_name: str
    tools: Toolchain
    live: bool = True
    keep_shards: bool = True
    quiet: bool = True
    paced: bool = True
    hls: bool = False

    def __post_init__(self) -> None:
        if not self.hls:
            return
        if not self.live:
            raise ConfigError(
                "HLS serving is only available for a live stream",
                remedy="Remove --hls when capturing a finished VOD playlist.",
            )
        if not self.paced:
            raise ConfigError(
                "--hls cannot be combined with --burst-output",
                remedy="Remove --burst-output so the playlist advances in real time.",
            )
        _tee_path(self.muxed_output)
        _tee_path(self.hls_playlist)

    @property
    def muxed_output(self) -> Path:
        """Where the muxed .ts lands.

        Named here rather than left to the downloader's own save pattern because
        paced output requires handing ffmpeg an explicit destination, and the
        caller has to be able to print the path it will appear at.
        """
        return self.out_dir / f"{self.run_name}.ts"

    @property
    def hls_directory(self) -> Path:
        return self.out_dir / "hls"

    @property
    def hls_playlist(self) -> Path:
        return self.hls_directory / "live.m3u8"

    def environment(self) -> dict[str, str]:
        """The child's environment.

        Setting the pipe options is what turns on ``-re``. The value is a plain
        path, which the downloader treats as a destination; a value starting with
        ``-`` would instead be spliced in as raw ffmpeg arguments, and that path
        loses control of where the output goes.
        """
        env = dict(os.environ)
        if self.live and self.paced:
            env[PIPE_OPTIONS_ENV] = self.pipe_options
        return env

    @property
    def pipe_options(self) -> str:
        """Destination passed to the downloader's live ffmpeg process."""
        if not self.hls:
            return str(self.muxed_output)
        archive = f"[f=mpegts:onfail=abort]{_tee_path(self.muxed_output)}"
        flags = "delete_segments+omit_endlist+independent_segments+temp_file"
        live = (
            "[f=hls:onfail=ignore:hls_time=2:hls_list_size=6:"
            f"hls_delete_threshold=2:hls_allow_cache=0:hls_flags={flags}]"
            f"{_tee_path(self.hls_playlist)}"
        )
        return f'-f tee -shortest "{archive}|{live}"'

    def argv(self) -> list[str]:
        downloader = str(self.tools.downloader) if self.tools.downloader else "N_m3u8DL-RE"
        argv = [downloader, self.url]

        for key in self.keys:
            argv += ["--key", str(key)]

        if self.live:
            argv += ["--live-pipe-mux"]
            # The flag takes a capitalised boolean, not a lowercase one.
            argv += ["--live-keep-segments", "True" if self.keep_shards else "False"]

        argv += ["--auto-select"]
        argv += ["--save-dir", str(self.out_dir)]
        argv += ["--save-name", self.run_name]
        argv += ["--decryption-engine", self.tools.decryptor_kind.value]

        if self.tools.decryptor:
            # Passed explicitly rather than relying on a sibling lookup: shaka
            # is published as packager-linux-x64 and the downloader never looks
            # for that name, so an implicit search fails silently for the
            # default engine.
            argv += ["--decryption-binary-path", str(self.tools.decryptor)]

        argv += ["--log-file-path", str(self.out_dir / LOG_NAME)]
        if self.quiet:
            # Every playlist refresh carries a FairPlay skd:// URI and an
            # embedded Widevine PSSH, neither of which the downloader handles;
            # it logs both at ERROR level several times a minute for the whole
            # broadcast. The progress display renders separately and survives
            # this, and the log file still records everything.
            argv += ["--log-level", "OFF"]
        return argv

    def display(self) -> str:
        parts = []
        for argument in self.argv():
            if any(ch in argument for ch in ' "\\'):
                parts.append('"' + argument.replace('"', '\\"') + '"')
            else:
                parts.append(argument)
        shown = " ".join(parts)
        if self.live and self.paced:
            # Part of the command in every sense that matters; omitting it from
            # the printed form would make a copied command behave differently.
            options = self.pipe_options.replace('"', '\\"')
            shown = f'{PIPE_OPTIONS_ENV}="{options}" {shown}'
        return shown


def run(plan: CapturePlan) -> int:
    """Run the downloader to completion. Returns its exit status."""
    plan.tools.verify()
    plan.out_dir.mkdir(parents=True, exist_ok=True)
    if plan.hls:
        plan.hls_directory.mkdir(parents=True, exist_ok=True)
    try:
        finished = subprocess.run(plan.argv(), env=plan.environment(), check=False)
    except FileNotFoundError as exc:
        raise CaptureError(
            f"could not start the downloader: {exc}",
            remedy="Rebuild the image — N_m3u8DL-RE should be on PATH.",
        ) from exc
    except KeyboardInterrupt:
        return 130

    if finished.returncode not in (0, 130):
        raise CaptureError(
            f"the downloader exited with status {finished.returncode}",
            remedy=f"See {plan.out_dir / LOG_NAME} for what it reported.",
        )
    return finished.returncode
