"""Status vocabulary — themed CLI status / notice output.

The siftd-domain layer over painted's ``callout`` primitive. painted owns the
*rendering* (glyph, color, layout, ASCII degradation); this module owns the
*meaning* (which severity) and the *CLI policy* painted has no concept of (which
stream). Every status line is a painted callout: a severity glyph (themed +
ASCII-degrading) + message, optionally with a muted detail or "↳ hint" line.

    from siftd.output import status
    status.confirm("Imported 412 files")
    status.error("Database not found: …", hint="Run 'siftd ingest' to create it.")
    status.db_missing(db)

Stream policy (overridable via ``stream=``): confirmations -> stdout (the
command's answer); info / warning / error -> stderr, so a piped stdout (or a
``--json`` payload) stays clean. Color is auto-stripped for non-TTY / NO_COLOR by
painted; glyphs degrade to ASCII under a non-UTF-8 locale.

This is the human-presentation layer only. ``--json`` branches stay the caller's
(they are machine-output keepers); a status line never writes onto a ``--json``
stdout payload.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from siftd.output.common import supports_unicode

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

# siftd's severity literal -> painted callout severity. The "hint" level (a soft
# advisory severity) presents as a neutral note.
_CALLOUT_SEVERITY: dict[str, str] = {
    "success": "success",
    "info": "info",
    "warning": "warning",
    "error": "error",
    "hint": "info",
}


def _emit(
    subject: str,
    severity: str,
    *,
    detail: str | None = None,
    hint: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """Build a callout for ``severity`` and print it to the policy stream.

    The glyph is chosen ASCII vs Unicode by the *target* stream's capability
    (TTY + a UTF-8-capable stdout), mirroring the doctor degradation ladder;
    color is left to painted's ``print_block`` auto-detection.
    """
    from painted import ASCII_ICONS, print_block, use_icons
    from painted.views import callout

    out = stream if stream is not None else (sys.stdout if severity == "success" else sys.stderr)
    callout_severity = _CALLOUT_SEVERITY.get(severity, "info")
    as_ascii = not (out.isatty() and supports_unicode())

    if as_ascii:
        with use_icons(ASCII_ICONS):
            block = callout(subject, severity=callout_severity, detail=detail, hint=hint)
    else:
        block = callout(subject, severity=callout_severity, detail=detail, hint=hint)
    print_block(block, out)


def confirm(subject: str, *, detail: str | None = None, hint: str | None = None,
            stream: TextIO | None = None) -> None:
    """An action succeeded (✓, stdout)."""
    _emit(subject, "success", detail=detail, hint=hint, stream=stream)


def info(subject: str, *, detail: str | None = None, hint: str | None = None,
         stream: TextIO | None = None) -> None:
    """A neutral note / hint / empty-state (ℹ, stderr)."""
    _emit(subject, "info", detail=detail, hint=hint, stream=stream)


def warning(subject: str, *, detail: str | None = None, hint: str | None = None,
            stream: TextIO | None = None) -> None:
    """A caution; the command continues (⚠, stderr)."""
    _emit(subject, "warning", detail=detail, hint=hint, stream=stream)


def error(subject: str, *, detail: str | None = None, hint: str | None = None,
          stream: TextIO | None = None) -> None:
    """An action failed (✗, stderr)."""
    _emit(subject, "error", detail=detail, hint=hint, stream=stream)


def db_missing(db: str | Path, *, stream: TextIO | None = None) -> None:
    """The recurring 'Database not found' + remediation couplet (~15 call sites)."""
    error(f"Database not found: {db}", hint="Run 'siftd ingest' to create it.", stream=stream)


def caveats(items: Iterable[object], *, stream: TextIO | None = None) -> None:
    """Replay caveats / findings as status lines, severity -> glyph.

    Duck-typed (``.severity`` / ``.message`` / optional ``.fix_command``) so
    output/ need not import the api-layer ``Caveat`` type.
    """
    for c in items:
        severity = getattr(c, "severity", "info") or "info"
        _emit(
            getattr(c, "message", str(c)),
            severity,
            hint=getattr(c, "fix_command", None),
            stream=stream,
        )
