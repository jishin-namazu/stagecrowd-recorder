"""Stage ordering for a run.

The sequence is fixed and each stage refuses to start unless the one before it
produced what it needs:

1. resolve the stream, walking down from a master playlist
2. obtain keys — from the command line, or by replaying a license request
3. gate on coverage against the tracks that will actually be captured
4. record what this run is, so it can be salvaged later
5. capture, with the shard watcher and the rotation watch running alongside

The gate sits at step 3 and not later on purpose: once capture starts the
broadcast is being consumed in real time, and a key set discovered to be wrong
at that point has already cost the opening minutes, which a sliding-window
manifest does not keep.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import capture, console, coverage, hls, licensing, playlist, records, shared, shards
from .errors import ArcError, ConfigError, LicenseError
from .keys import KeyRing
from .playlist import Source
from .settings import Settings
from .toolchain import Toolchain


def default_run_name(at: datetime | None = None) -> str:
    return f"run_{at or datetime.now():%Y%m%d_%H%M%S}"


def default_out_dir(root: Path | None = None) -> Path:
    """Artefacts go in the working directory, beside the shards.

    Not in a subdirectory of their own: the downloader puts shards under the
    working directory named after the run, and the mount point is that same
    directory, so an extra level here produces archive/archive/run_… on the host
    while the shards sit at archive/run_….
    """
    name = default_run_name()
    return (root / name) if root is not None else Path(f"{name}.out")


@dataclass(slots=True)
class Prepared:
    source: Source
    keys: KeyRing
    report: coverage.GateReport
    out_dir: Path
    run_name: str
    shard_root: Path
    session_shard_root: Path
    tools: Toolchain
    tool_versions: dict[str, str]


def _license_target(settings: Settings) -> licensing.LicenseTarget:
    if settings.license_url:
        headers = (
            licensing.read_header_file(settings.headers_file)
            if settings.headers_file is not None
            else None
        )
        return licensing.LicenseTarget.from_url(settings.license_url, headers)
    return licensing.LicenseTarget.from_token(settings.license_token)


def gather_keys(settings: Settings, source: Source) -> KeyRing:
    """Literal keys first, then a license replay.

    Literal keys win because supplying them is an explicit statement that the
    license step is not wanted — that is the offline path, and it must not reach
    for a CDM that may not be mounted.
    """
    if settings.literal_keys:
        ring = KeyRing.scrape(",".join(settings.literal_keys))
        if not ring:
            raise ConfigError(
                "no KID:KEY pair could be read from the keys given",
                remedy="Each key looks like 32 hex characters, a colon, then 32 more.",
            )
        return ring

    if not settings.has_license_route:
        raise ConfigError(
            "no keys were given and no license route was configured",
            remedy=(
                "Either pass --key KID:KEY, or pass --token/--license-url so the "
                "license request can be replayed here."
            ),
        )

    target = _license_target(settings)
    console.detail(f"license endpoint {target.redacted()}")
    acquired = licensing.acquire(
        source.protection.payloads,
        source.protection.must_cover,
        target,
        settings.cdm_path,
    )
    console.detail(
        f"{len(acquired.keys)} key(s) from {acquired.requests_made} "
        f"of {len(source.protection.payloads)} PSSH payload(s)"
    )
    for failure in acquired.failures:
        console.warn(f"license attempt failed: {failure}")
    return acquired.keys


def prepare(settings: Settings) -> Prepared:
    if settings.hls_address:
        hls.Endpoint.parse(settings.hls_address)
    if not settings.url:
        raise ConfigError(
            "no stream URL was given",
            remedy="Pass --url, or set url= in the settings file.",
        )

    console.stage("resolving the stream")
    source = playlist.resolve(settings.url)
    kind = "master" if source.playlist.is_master else "media"
    console.detail(f"{kind} playlist, {'live' if source.live else 'vod'}")
    if not source.playlist.is_master:
        console.warn(
            "this is a single media playlist — automatic selection has no audio track to "
            "choose, so the result may be video only"
        )
    for track in source.tracks:
        console.detail(f"track {track.describe()}")
    if source.protection.fairplay_kids:
        console.detail(f"{len(source.protection.fairplay_kids)} FairPlay key id(s) ignored")
    if not source.protection.encrypted:
        console.detail("no Widevine protection declared")

    console.stage("obtaining keys")
    keys = gather_keys(settings, source)

    console.stage("checking key coverage")
    report = coverage.inspect(keys, source.protection)
    if report.spare:
        console.detail(f"not captured, no key needed: {', '.join(sorted(report.spare))}")
    if report.missing and not settings.strict_coverage:
        console.warn(
            f"proceeding without keys for: {', '.join(sorted(report.missing))} — "
            "those tracks will not decrypt"
        )
    coverage.enforce(keys, source.protection, strict=settings.strict_coverage)
    if report.covered and report.needed:
        console.good(f"all {len(report.needed)} stream KID(s) covered")

    out_dir = settings.out_dir or default_out_dir()
    # The shards are named from the run, and the artefact directory carries a
    # suffix so the two are siblings rather than one nested in the other.
    run_name = out_dir.name.removesuffix(".out")

    console.stage("verifying the toolchain")
    tools = Toolchain.discover(settings.decryptor)
    versions = tools.verify()
    console.table(sorted(versions.items()))

    session_shard_root = shards.shard_root(run_name)
    shared_shard_root = (
        shared.canonical_shard_root(source.url, session_shard_root)
        if settings.keep_shards
        else session_shard_root
    )

    return Prepared(
        source=source,
        keys=keys,
        report=report,
        out_dir=out_dir,
        run_name=run_name,
        shard_root=shared_shard_root,
        session_shard_root=session_shard_root,
        tools=tools,
        tool_versions=versions,
    )


def write_artefacts(prepared: Prepared, settings: Settings) -> None:
    artefacts = records.Artefacts(prepared.out_dir)
    source = prepared.source
    artefacts.write_json(
        records.MANIFEST_NAME,
        {
            "url": source.url,
            "is_master": source.playlist.is_master,
            "live": source.live,
            "segment_count": source.playlist.segment_count,
            "media_sequence": source.playlist.media_sequence,
            "target_duration": source.playlist.target_duration,
            "tracks": [
                {
                    "url": t.url,
                    "kind": t.kind,
                    "bandwidth": t.bandwidth,
                    "resolution": t.resolution,
                    "language": t.language,
                }
                for t in source.tracks
            ],
            "pssh": list(source.protection.payloads),
            "advertised_kids": sorted(source.protection.advertised),
            "needed_kids": sorted(source.protection.must_cover),
            "fairplay_kids": sorted(source.protection.fairplay_kids),
        },
    )
    if source.protection.payloads:
        artefacts.write(records.PSSH_NAME, "\n".join(source.protection.payloads))
    artefacts.write(records.KEYS_NAME, str(prepared.keys))
    if settings.has_license_route:
        artefacts.write(records.LICENSE_NAME, _license_target(settings).redacted())

    records.RunRecord(
        url=source.url,
        run_name=prepared.run_name,
        output_dir=prepared.out_dir,
        shard_root=prepared.shard_root,
        session_shard_root=prepared.session_shard_root,
        shards_kept=settings.keep_shards,
        decryptor=settings.decryptor.value,
        tool_versions=prepared.tool_versions,
        key_kids=sorted(prepared.keys.kids),
        stream_kids=sorted(source.protection.must_cover),
        segment_ms=source.segment_ms,
    ).write()


def build_plan(prepared: Prepared, settings: Settings) -> capture.CapturePlan:
    return capture.CapturePlan(
        url=prepared.source.url,
        keys=prepared.keys,
        out_dir=prepared.out_dir,
        run_name=prepared.run_name,
        tools=prepared.tools,
        live=prepared.source.live,
        keep_shards=settings.keep_shards,
        quiet=settings.quiet_downloader,
        paced=settings.paced_output,
        hls=bool(settings.hls_address),
    )


def execute(settings: Settings) -> int:
    prepared = prepare(settings)
    prepared.out_dir.mkdir(parents=True, exist_ok=True)
    write_artefacts(prepared, settings)
    plan = build_plan(prepared, settings)

    watcher = None
    if settings.keep_shards:
        watcher = shards.ShardWatcher(
            prepared.session_shard_root,
            log_dir=prepared.out_dir if settings.shard_log else None,
            mirror_root=prepared.shard_root,
            echo=console.say if settings.shard_log and settings.shard_echo else None,
            hint_ms=prepared.source.segment_ms or None,
            decrypting=True,
        )

    guard = coverage.RotationWatch(
        prepared.source.url,
        prepared.keys,
        interval=settings.guard_interval,
        shard_root=prepared.session_shard_root,
        accepted=prepared.report.missing,
        echo=console.warn,
    )

    console.stage("capturing")
    console.detail(f"output   {prepared.out_dir}")
    console.detail(f"shards   {prepared.shard_root}  (shared by this playback URL)")
    if prepared.session_shard_root != prepared.shard_root:
        console.detail(f"session  {prepared.session_shard_root}")
    if plan.live and plan.paced:
        console.detail(f"file     {plan.muxed_output}  (playable while recording)")
    hls_server = None
    if settings.hls_address:
        endpoint = hls.Endpoint.parse(settings.hls_address)
        hls_server = hls.HlsServer(plan.hls_directory, endpoint)
        console.detail(f"hls      {hls_server.url}")

    try:
        if watcher is not None:
            watcher.start()
        guard.start()
        if hls_server is not None:
            hls_server.start()
        status = capture.run(plan)
    finally:
        if hls_server is not None:
            hls_server.stop()
        guard.stop()
        if watcher is not None:
            watcher.stop()

    console.stage("finished")
    if watcher is not None and watcher.summary():
        console.detail(watcher.summary())
    console.detail(guard.summary())
    console.detail(f"artefacts in {prepared.out_dir}")
    if settings.keep_shards:
        console.detail(
            f"rebuild with: docker compose run --rm recorder rebuild {prepared.out_dir}"
        )
    return status


def describe_plan(settings: Settings) -> int:
    """Everything execute() would do, without running the downloader."""
    prepared = prepare(settings)
    plan = build_plan(prepared, settings)
    console.stage("planned command")
    console.say(plan.display())
    console.stage("planned run record")
    console.say(
        json.dumps(
            records.RunRecord(
                url=prepared.source.url,
                run_name=prepared.run_name,
                output_dir=prepared.out_dir,
                shard_root=prepared.shard_root,
                session_shard_root=prepared.session_shard_root,
                shards_kept=settings.keep_shards,
                decryptor=settings.decryptor.value,
                tool_versions=prepared.tool_versions,
                key_kids=sorted(prepared.keys.kids),
                stream_kids=sorted(prepared.source.protection.must_cover),
                segment_ms=prepared.source.segment_ms,
            ).as_document(),
            indent=2,
        )
    )
    return 0


def show_keys(settings: Settings) -> int:
    console.stage("resolving the stream")
    source = playlist.resolve(settings.url)
    console.stage("obtaining keys")
    keys = gather_keys(settings, source)
    report = coverage.inspect(keys, source.protection)
    console.stage("keys")
    console.say(str(keys))
    console.stage("coverage")
    console.table(
        [
            ("needed", ", ".join(sorted(report.needed)) or "(none declared)"),
            ("held", ", ".join(sorted(report.held))),
            ("missing", ", ".join(sorted(report.missing)) or "(none)"),
        ]
    )
    if settings.out_dir is not None:
        records.Artefacts(settings.out_dir).write(records.KEYS_NAME, str(keys))
        console.detail(f"written to {settings.out_dir / records.KEYS_NAME}")
    return 0 if report.covered else 2


def probe_environment(settings: Settings) -> int:
    """Exercise everything a run depends on, rather than checking it exists."""
    from . import toolchain as tools_mod

    console.stage("environment")
    ok = True
    tools = Toolchain.discover(settings.decryptor)
    for label, path in (
        (tools_mod.DOWNLOADER, tools.downloader),
        (tools_mod.MUXER, tools.muxer),
        (settings.decryptor.binary, tools.decryptor),
        (tools_mod.PROBER, tools.prober),
    ):
        if path is None:
            console.fail(f"{label}: not found")
            ok = False
            continue
        result = tools_mod.probe(path)
        if result.runnable:
            console.good(f"{label}: {result.detail}")
        else:
            console.fail(f"{label}: present but not runnable — {result.detail}")
            ok = False

    console.stage("local CDM")
    if not settings.cdm_path.exists():
        console.warn(f"no device file at {settings.cdm_path} — license replay will not work")
    else:
        try:
            from pywidevine import Cdm, Device  # type: ignore

            device = Device.load(str(settings.cdm_path))
            Cdm.from_device(device)
            console.good(
                f"{settings.cdm_path.name}: {device.type.name} L{device.security_level}"
            )
        except ImportError as exc:
            console.warn(f"pywidevine unavailable — cannot import {getattr(exc, 'name', 'pywidevine')}")
        except Exception as exc:
            console.fail(f"the CDM at {settings.cdm_path} could not be loaded: {exc}")
            ok = False

    if ok:
        console.stage("ready")
    return 0 if ok else 1


def report_error(error: ArcError) -> int:
    console.fail(error.message, error.remedy)
    return error.exit_code


__all__ = [
    "ArcError",
    "LicenseError",
    "Prepared",
    "default_out_dir",
    "describe_plan",
    "execute",
    "prepare",
    "probe_environment",
    "report_error",
    "show_keys",
]
