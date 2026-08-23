"""The run record, and the files written beside it.

``run.json`` exists for one reason: to link an output directory back to the
shards that produced it. Nothing in the directory tree reveals that link — the
shards land as a sibling of the working directory, named after the run — so it
gets written down.

It gets written down three ways, and that is deliberate. The absolute path is the
direct answer. The relative path survives the directory being moved. The run name
reconstructs the location from the downloader's own naming convention. Both
documented workflows for this tool — capture in a container and salvage on the
host, or simply move the folder — turn an absolute path into a dead link while
the relationship it described still holds.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

RECORD_NAME = "run.json"
MANIFEST_NAME = "source.json"
PSSH_NAME = "pssh.txt"
KEYS_NAME = "keys.txt"
LICENSE_NAME = "license.txt"


@dataclass(slots=True)
class RunRecord:
    url: str
    run_name: str
    output_dir: Path
    shard_root: Path
    shards_kept: bool
    decryptor: str
    session_shard_root: Path | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    key_kids: list[str] = field(default_factory=list)
    stream_kids: list[str] = field(default_factory=list)
    segment_ms: int = 0

    def as_document(self) -> dict:
        output = self.output_dir.resolve()
        shard_root = self.shard_root.resolve()
        try:
            relative = os.path.relpath(shard_root, output)
        except ValueError:
            relative = None  # different drives on Windows hosts
        return {
            "url": self.url,
            "run_name": self.run_name,
            "output_dir": str(output),
            "shard_root": str(shard_root),
            "shared_shard_root": str(shard_root),
            "shard_root_relative": relative,
            "session_shard_root": str(self.session_shard_root.resolve())
            if self.session_shard_root is not None
            else str(shard_root),
            "shards_kept": self.shards_kept,
            "decryptor": self.decryptor,
            "tool_versions": self.tool_versions,
            "key_kids": sorted(self.key_kids),
            "stream_kids": sorted(self.stream_kids),
            "segment_ms": self.segment_ms,
        }

    def write(self, into: Path | None = None) -> Path:
        target = (into or self.output_dir) / RECORD_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_document(), indent=2), encoding="utf-8")
        return target


def read_record(output_dir: Path) -> dict | None:
    try:
        document = json.loads((output_dir / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


class Artefacts:
    """Small text files written beside a run, for diagnosis afterwards."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, content: str) -> Path:
        target = self.out_dir / name
        target.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        return target

    def write_json(self, name: str, document: dict) -> Path:
        return self.write(name, json.dumps(document, indent=2))
