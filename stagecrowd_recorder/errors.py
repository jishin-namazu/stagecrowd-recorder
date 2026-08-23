"""Failure types.

Every error carries an optional remedy. The CLI prints both, so a message says
what broke and the remedy says what to do next; neither line has to carry both
jobs. Anything that escapes as a bare Exception is a bug in this package, not a
condition the operator is expected to handle.
"""

from __future__ import annotations


class ArcError(Exception):
    """Base for every condition an operator can act on."""

    # Process exit code when this error reaches the top level.
    exit_code: int = 2

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


class ConfigError(ArcError):
    """Settings are contradictory or incomplete."""


class SourceError(ArcError):
    """The stream URL did not yield a usable HLS playlist."""


class ProtectionError(ArcError):
    """A PSSH payload could not be read."""


class LicenseError(ArcError):
    """The license exchange failed or returned nothing usable."""


class CoverageGap(ArcError):
    """The keys on hand do not cover the tracks about to be captured."""


class ToolError(ArcError):
    """An external binary is missing, or present but not runnable."""

    exit_code: int = 1


class CaptureError(ArcError):
    """The downloader exited badly."""


class SalvageError(ArcError):
    """Shards could not be located or rebuilt."""


class BackfillError(ArcError):
    """CDN shards could not be discovered or recovered."""
