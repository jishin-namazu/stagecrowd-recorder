"""Argument parsing and dispatch.

The settings file is loaded before the parser is built, because argparse
captures its defaults at construction time: a value read from the environment
after that point is read too late and is silently ignored. So the file is applied
into the environment first, and the precedence that falls out is command line,
then real environment, then file, then built-in default.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NoReturn

from . import backfill as backfill_mod
from . import console, runbook, salvage, settings as cfg
from .errors import ArcError
from .settings import Settings
from .toolchain import Decryptor


class _Unreadable(Exception):
    """Raised instead of exiting when the sniffer cannot read argv."""


class _Sniffer(argparse.ArgumentParser):
    """An argparse parser that reports failure to its caller, not to the user."""

    def error(self, message: str) -> NoReturn:
        # argparse's own diagnostic is dropped: the real parser is about to
        # print a better one, against the real usage line.
        raise _Unreadable


def _settings_file_from_argv(argv: list[str]) -> Path | None:
    """Pull --settings out of argv before the real parser exists.

    A throwaway parser rather than a hand-rolled scan, so that every spelling
    argparse accepts — ``--settings X``, ``--settings=X``, and prefix
    abbreviations such as ``--set X`` — resolves to the same file here.

    It scans the whole argv, so it also picks the flag up after ``backfill``,
    ``rebuild`` or ``probe``, where the real parser does not accept it and will
    exit with "unrecognized arguments". The file is loaded either way; only the exit
    status differs.

    A malformed argv yields None rather than a diagnostic: complaining is the
    real parser's job, and its usage line is the one worth showing.
    """
    sniffer = _Sniffer(add_help=False)
    sniffer.add_argument("--settings")
    try:
        known, _ = sniffer.parse_known_args(argv)
    except _Unreadable:
        return None
    return Path(known.settings) if known.settings else None


def _add_settings_option(parser: argparse.ArgumentParser) -> None:
    """Teach one parser that --settings exists.

    Nothing ever reads the parsed value: the file was already located by
    _settings_file_from_argv and applied to the environment before this parser
    existed. SUPPRESS only keeps a subparser from overwriting the root's
    attribute with a default when the flag is absent.
    """
    parser.add_argument(
        "--settings",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help=f"settings file (default: {cfg.SETTINGS_SEARCH_HELP})",
    )


def _add_stream_options(parser: argparse.ArgumentParser) -> None:
    _add_settings_option(parser)
    parser.add_argument(
        "--url",
        default=cfg.env(cfg.ENV_URL),
        help="m3u8 URL. A master playlist is strongly preferred.",
    )
    parser.add_argument(
        "--out",
        default=cfg.env(cfg.ENV_OUT),
        help="output directory (default: archive/run_<timestamp>)",
    )
    parser.add_argument(
        "--key",
        action="append",
        default=[cfg.env(cfg.ENV_KEYS)] if cfg.env(cfg.ENV_KEYS) else [],
        metavar="KID:KEY",
        help="content key; repeat or comma-separate. Skips the license step entirely.",
    )
    parser.add_argument(
        "--token",
        default=cfg.env(cfg.ENV_TOKEN),
        help="license session token; the endpoint URL is built from it",
    )
    parser.add_argument(
        "--license-url",
        default=cfg.env(cfg.ENV_LICENSE_URL),
        help="full license URL, used instead of --token",
    )
    parser.add_argument(
        "--headers-file",
        default=cfg.env(cfg.ENV_HEADERS),
        help="headers for --license-url, one 'Name: value' per line",
    )
    parser.add_argument(
        "--cdm",
        default=cfg.env(cfg.ENV_CDM) or str(cfg.DEFAULT_CDM),
        help="Widevine device file (default: /config/device.wvd)",
    )
    parser.add_argument(
        "--decryptor",
        type=Decryptor,
        choices=list(Decryptor),
        default=Decryptor.SHAKA,
        help="decryption engine (default: SHAKA_PACKAGER)",
    )
    parser.add_argument(
        "--allow-partial-keys",
        action="store_true",
        help="capture even when some tracks have no key; those will not decrypt",
    )
    parser.add_argument(
        "--discard-shards",
        action="store_true",
        help="do not keep decrypted shards — halves disk use, gives up rebuild",
    )
    parser.add_argument(
        "--burst-output",
        action="store_true",
        help="write the muxed file as fast as data arrives; faster, but the file "
        "cannot be played while it is being written",
    )
    parser.add_argument(
        "--hls",
        nargs="?",
        const=cfg.DEFAULT_HLS_ADDRESS,
        default=cfg.env(cfg.ENV_HLS),
        metavar="HOST:PORT",
        help="serve a rolling live.m3u8 over HTTP; without an address, use "
        f"{cfg.DEFAULT_HLS_ADDRESS}",
    )
    parser.add_argument("--quiet-shards", action="store_true", help="do not echo each shard to the console")
    parser.add_argument("--no-shard-log", action="store_true", help="do not track shards at all")
    parser.add_argument("--verbose-downloader", action="store_true", help="let the downloader log to the console")
    parser.add_argument(
        "--guard-interval",
        type=float,
        default=240.0,
        help="seconds between key-rotation re-checks (default: 240)",
    )


def _rate_limit(value: str) -> int:
    try:
        return backfill_mod.parse_rate_limit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stagecrowd_recorder",
        description="Archive a Widevine-protected HLS live stream.",
    )
    _add_settings_option(parser)
    subcommands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("capture", "resolve, obtain keys, check coverage, then record"),
        ("plan", "everything capture does, but print the command instead of running it"),
        ("keys", "obtain and print keys without recording"),
    ):
        sub = subcommands.add_parser(name, help=help_text)
        _add_stream_options(sub)

    rebuild = subcommands.add_parser("rebuild", help="rebuild a playable file from kept shards")
    rebuild.add_argument("target", help="a run output directory, or a shard directory")
    rebuild.add_argument("-o", "--output", help="destination file (default: <shards>-rebuilt.mkv)")

    backfill = subcommands.add_parser(
        "backfill", help="discover CDN indexes and recover shards missing locally"
    )
    backfill.add_argument("target", help="a run output directory, or its shard directory")
    backfill.add_argument(
        "--rate-limit",
        type=_rate_limit,
        default=backfill_mod.DEFAULT_RATE_LIMIT,
        metavar="RATE",
        help="maximum sequential download rate, e.g. 512K or 3M (default: 3M)",
    )
    backfill.add_argument(
        "--scan-only",
        action="store_true",
        help="print the CDN range and missing counts without downloading",
    )
    backfill.add_argument(
        "--include-live-tail",
        action="store_true",
        help="also fetch indexes newer than an actively growing local run",
    )

    probe = subcommands.add_parser("probe", help="exercise the toolchain and the CDM")
    probe.add_argument("--cdm", default=cfg.env(cfg.ENV_CDM) or str(cfg.DEFAULT_CDM))
    probe.add_argument("--decryptor", type=Decryptor, choices=list(Decryptor), default=Decryptor.SHAKA)

    return parser


def _settings_from(args: argparse.Namespace) -> Settings:
    literal = tuple(k for k in (args.key or []) if k.strip())
    return Settings(
        url=(args.url or "").strip(),
        out_dir=Path(args.out) if getattr(args, "out", "") else None,
        cdm_path=Path(args.cdm),
        license_url=(args.license_url or "").strip(),
        license_token=(args.token or "").strip(),
        headers_file=Path(args.headers_file) if getattr(args, "headers_file", "") else None,
        literal_keys=literal,
        decryptor=args.decryptor,
        keep_shards=not args.discard_shards,
        strict_coverage=not args.allow_partial_keys,
        paced_output=not args.burst_output,
        quiet_downloader=not args.verbose_downloader,
        shard_log=not args.no_shard_log,
        shard_echo=not args.quiet_shards,
        guard_interval=args.guard_interval,
        hls_address=(args.hls or "").strip(),
    )


def _run_rebuild(args: argparse.Namespace) -> int:
    destination = Path(args.output) if args.output else None
    result = salvage.rebuild(Path(args.target), destination)
    console.stage("rebuilt")
    size_mb = result.destination.stat().st_size / 1_048_576
    console.say(
        f"{result.destination}  ({size_mb:.1f} MB, {result.seconds:.1f}s container / "
        f"{result.shortest_track():.0f}s on every track)"
    )
    for note in result.notes():
        console.warn(note)
    if not result.intact:
        console.detail(
            "Rebuilt from an interrupted run. This is everything that survived; "
            "the rest was never written to disk."
        )
    return 0


def _run_backfill(args: argparse.Namespace) -> int:
    console.stage("discovering CDN shards")
    result = backfill_mod.run(
        Path(args.target),
        rate_limit=args.rate_limit,
        scan_only=args.scan_only,
        include_live_tail=args.include_live_tail,
        echo=console.detail,
    )
    if result.active and not args.include_live_tail:
        console.warn(
            "the run is still growing — backfill stopped at the newest local index "
            "to avoid racing the recorder"
        )
    console.stage("backfill")
    for track in result.tracks:
        console.detail(
            f"{track.kind}: CDN {track.remote_first}..{track.remote_last}; "
            f"checked through {track.considered_last}; local {track.local}; "
            f"missing {track.missing}; recovered {track.recovered}"
        )
        if track.failures:
            shown = ", ".join(str(index) for index in track.failures[:8])
            if len(track.failures) > 8:
                shown += f", and {len(track.failures) - 8} more"
            console.warn(f"{track.kind}: failed CDN indexes: {shown}")
    if args.scan_only:
        console.good("CDN scan complete")
        return 0
    if result.ok:
        console.good("local shards cover every checked CDN index")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    path, applied = cfg.load_settings_file(_settings_file_from_argv(argv))
    args = build_parser().parse_args(argv)
    if path is not None and applied:
        console.detail(f"settings from {path}: {', '.join(sorted(applied))}")

    try:
        if args.command == "rebuild":
            return _run_rebuild(args)
        if args.command == "backfill":
            return _run_backfill(args)
        if args.command == "probe":
            return runbook.probe_environment(
                Settings(cdm_path=Path(args.cdm), decryptor=args.decryptor)
            )

        settings = _settings_from(args)
        if args.command == "capture":
            return runbook.execute(settings)
        if args.command == "plan":
            return runbook.describe_plan(settings)
        if args.command == "keys":
            return runbook.show_keys(settings)
        raise AssertionError(f"unhandled command {args.command!r}")
    except ArcError as error:
        return runbook.report_error(error)
    except KeyboardInterrupt:
        console.warn("interrupted")
        return 130
