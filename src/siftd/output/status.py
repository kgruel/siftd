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

from siftd.output.common import prefers_ascii

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from painted import Style

# siftd's severity literal -> painted callout severity. The "hint" level (a soft
# advisory severity) presents as a neutral note.
_CALLOUT_SEVERITY: dict[str, str] = {
    "success": "success",
    "info": "info",
    "warning": "warning",
    "error": "error",
    "hint": "info",
}

# The other projection of the same severity vocabulary: severity -> a glyph + the
# palette role that colours it. ``_CALLOUT_SEVERITY`` (above) drives the emitted
# status lines; this drives surfaces that lay out the mark themselves (the doctor
# progress/findings blocks, the db dry-run previews). The glyph CHARACTERS live
# in painted's IconSet — one owner, its ok/info/warn/error ladder (Unicode +
# ASCII); this owns only the mapping of siftd's severities onto those slots and
# the role colours. ``None`` is the pass / all-clear state (a check with no
# findings). painted has no glyph for an unrecognised severity — including the
# declared-but-unused "hint" — so it falls back to a neutral ``?``, never the
# all-clear mark. (This lives beside ``_CALLOUT_SEVERITY`` so the two projections
# of one vocabulary stay together; the doctor consumed it from here to drop a
# ``cli -> doctor`` import.)
_SEVERITY_ICON: dict[str | None, tuple[str, str]] = {
    # severity: (IconSet glyph attribute, palette-key)
    "error": ("error", "error"),
    "warning": ("warn", "warning"),
    "info": ("info", "muted"),
    None: ("ok", "success"),  # pass / all-clear (no findings for a check)
}


def severity_glyph(severity: str | None, *, as_ascii: bool = False) -> tuple[str, str]:
    """Return ``(glyph, palette-key)`` for a finding severity.

    The glyph character comes from painted's IconSet (the single source):
    ``as_ascii=True`` reads ASCII_ICONS for the plain path (non-Unicode
    terminals), otherwise the Unicode default. ``None`` is the pass / all-clear
    glyph; an unrecognised severity — including the declared-but-unused "hint" —
    yields a neutral ``?``, never the all-clear mark.
    """
    mapping = _SEVERITY_ICON.get(severity)
    if mapping is None:
        return "?", "muted"
    from painted import ASCII_ICONS, IconSet

    icon_attr, key = mapping
    icons = ASCII_ICONS if as_ascii else IconSet()
    return getattr(icons, icon_attr), key


def severity_mark(severity: str | None, *, as_ascii: bool = False) -> tuple[str, Style]:
    """Return ``(glyph, Style)`` — the glyph already resolved to its palette role.

    The ``severity_glyph`` companion for callers that want to *style* the mark
    inline (e.g. a coloured glyph inside a ``definitions`` value) rather than
    drive a separate palette lookup. The palette role is read from the ambient
    theme, so the colour tracks ``use_theme`` like every other surface.
    """
    from painted import Style, current_palette

    glyph, key = severity_glyph(severity, as_ascii=as_ascii)
    style = getattr(current_palette(), key, None)
    return glyph, style if style is not None else Style()


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
    as_ascii = prefers_ascii(out)

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
