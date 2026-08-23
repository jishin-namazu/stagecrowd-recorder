"""Settings, and the file they can come from.

Precedence is command line, then real environment, then the settings file, then
built-in defaults. The rule that matters is the middle one: a variable exported
into the process is a deliberate act and a file must never silently override it.

The file parser handles ``KEY=value`` and nothing else — no interpolation, no
multi-line values, no shell semantics. It accepts short aliases because the file
is usually written in a hurry to note down one broadcast, and insisting on
``STC_`` prefixes there is friction that buys nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .toolchain import Decryptor

PREFIX = "STC_"

ENV_URL = PREFIX + "URL"
ENV_LICENSE_URL = PREFIX + "LICENSE_URL"
ENV_TOKEN = PREFIX + "TOKEN"
ENV_KEYS = PREFIX + "KEYS"
ENV_CDM = PREFIX + "CDM"
ENV_OUT = PREFIX + "OUT"
ENV_SETTINGS_FILE = PREFIX + "SETTINGS"
ENV_HEADERS = PREFIX + "HEADERS"
ENV_HLS = PREFIX + "HLS"

DEFAULT_CDM = Path("/config/device.wvd")
DEFAULT_OUT_ROOT = Path("archive")
DEFAULT_HLS_ADDRESS = "127.0.0.1:8080"

# Searched in order. .env is accepted because that is what the surrounding
# tooling already writes, and the parser reads it the same way either name.
SETTINGS_NAMES = (".stagecrowd", ".env")

# The search in prose. The file names come from SETTINGS_NAMES so they cannot
# drift; the order around them is hand-written, and must be reworded whenever
# find_settings_file changes. Note "when set", not "else": naming a file in the
# environment replaces the search rather than starting it, so a bad $STC_SETTINGS
# finds nothing at all. --help embeds this (argparse may wrap it across lines);
# README's --settings row says the same in Chinese, and is edited by hand too.
SETTINGS_SEARCH_HELP = (
    f"${ENV_SETTINGS_FILE} when set, otherwise ./" + " or ./".join(SETTINGS_NAMES)
)

_ALIASES = {
    "url": ENV_URL,
    "m3u8": ENV_URL,
    "stream": ENV_URL,
    "token": ENV_TOKEN,
    "wv_token": ENV_TOKEN,
    "license_token": ENV_TOKEN,
    "license": ENV_LICENSE_URL,
    "license_url": ENV_LICENSE_URL,
    "key": ENV_KEYS,
    "keys": ENV_KEYS,
    "cdm": ENV_CDM,
    "device_wvd": ENV_CDM,
    "out": ENV_OUT,
    "output": ENV_OUT,
    "headers": ENV_HEADERS,
    "hls": ENV_HLS,
    "hls_address": ENV_HLS,
}


def canonical_name(name: str) -> str:
    """Map an alias to its canonical variable; unknown names pass through."""
    return _ALIASES.get(name.strip().lower(), name.strip())


def parse_settings_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].strip()
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        value = value.strip()
        # Only a matching pair of quotes is stripped, and only one layer: a JWT
        # is unquoted and full of characters that must survive verbatim.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            values[canonical_name(name)] = value
    return values


def find_settings_file(explicit: Path | None = None) -> Path | None:
    """Explicit path, then the environment, then the working directory.

    An explicit path or one named in the environment is never replaced by a
    search: it was chosen deliberately, and silently reading a different file
    than the one asked for is worse than reading none.
    """
    if explicit is not None:
        return explicit if explicit.is_file() else None
    from_env = os.environ.get(ENV_SETTINGS_FILE, "").strip()
    if from_env:
        candidate = Path(from_env)
        return candidate if candidate.is_file() else None
    for name in SETTINGS_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return None


def load_settings_file(explicit: Path | None = None) -> tuple[Path | None, dict[str, str]]:
    """Apply a settings file into the environment without overriding it."""
    path = find_settings_file(explicit)
    if path is None:
        return None, {}
    try:
        values = parse_settings_file(path.read_text(encoding="utf-8"))
    except OSError:
        return None, {}
    applied: dict[str, str] = {}
    for name, value in values.items():
        if name in os.environ:
            continue
        os.environ[name] = value
        applied[name] = value
    return path, applied


def env(name: str, fallback: str = "") -> str:
    return os.environ.get(name, fallback).strip()


def env_int(name: str, fallback: int) -> int:
    try:
        return int(env(name, str(fallback)))
    except ValueError:
        return fallback


@dataclass(slots=True)
class Settings:
    """One resolved run configuration."""

    url: str = ""
    out_dir: Path | None = None
    cdm_path: Path = DEFAULT_CDM
    license_url: str = ""
    license_token: str = ""
    headers_file: Path | None = None
    literal_keys: tuple[str, ...] = ()
    decryptor: Decryptor = Decryptor.SHAKA
    keep_shards: bool = True
    strict_coverage: bool = True
    paced_output: bool = True
    quiet_downloader: bool = True
    shard_log: bool = True
    shard_echo: bool = True
    guard_interval: float = 240.0
    hls_address: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def has_license_route(self) -> bool:
        return bool(self.license_url or self.license_token)
