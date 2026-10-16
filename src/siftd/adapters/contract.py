"""What the framework supplies for an adapter that decodes (interface version 2).

A version-2 adapter module declares how its source is read and decoded; this
module derives everything else from those declarations, in one place, so no
adapter hand-writes it:

- ``parse(source)`` and ``assemble(source)``: read, decode, assemble. ``assemble``
  returns the :class:`~siftd.adapters.assembly.Assembly` (conversations *and* the
  ledger), so ingest obtains the ledger from the one parse it already does.
- ``HARNESS``: the :class:`~siftd.domain.Harness` the declarations
  (``NAME``, ``HARNESS_SOURCE``, ``HARNESS_LOG_FORMAT``, ``HARNESS_DISPLAY_NAME``) describe.
- ``peek_scan`` / ``peek_exchanges`` / ``peek_tail``: peek projected from the same
  decoder (:mod:`siftd.adapters.peek_projection`), so peek and ingest classify
  each record once.

What the module declares, beyond the required names in :mod:`siftd.adapters.validation`:

========================  =====================================================
``default_session(source)``  The session of a source whose records state none.
``peek_read(source)``        A reader for peek when it must differ from ``read``
                             (VSCode reads a live, half-written file leniently).
``peek_enrich(path, scan)``  Adds to the derived scan what the log alone cannot
                             say (Claude Code: a subagent's sidecar file).
``SUPPORTS_PEEK = False``    Peek is not derived (one database, many sessions).
``PEEK_LAST_OF_MANY = True`` A source holding several conversations peeks as its
                             last, the live one (aider's history file).
``TOOL_ALIASES``             Canonicalizes tool names in peek, as in ingest.
========================  =====================================================

An explicit ``peek_scan``, ``peek_exchanges`` or ``peek_tail`` on the module
wins over the derived one: a format with no records to parse (aider's markdown
tail) says so by defining its own.

The derived pieces live on a wrapper around the module, so the registry hands
every consumer an adapter with the interface and the module itself stays
declarations plus decoding. A version-2 module gets a :class:`DecodingAdapter`.
A version-1 module gets a :class:`LegacyAdapter`, which derives the peek scan and
exchanges from its ``normalize_record`` and nothing else (see that class).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from siftd.adapters.assembly import Assembly, assemble
from siftd.adapters.peek_projection import make_peek_hooks_from_decoder
from siftd.adapters.records import Session
from siftd.adapters.sdk import build_harness, make_peek_hooks
from siftd.adapters.validation import DECODING_VERSION
from siftd.domain import Conversation, Harness
from siftd.domain.peek import PeekScanResult
from siftd.domain.source import Source

_PEEK_HOOKS = ("peek_scan", "peek_exchanges", "peek_tail")


def decodes(adapter: Any) -> bool:
    """True if ``adapter`` declares the decoding interface (version 2)."""
    return getattr(adapter, "ADAPTER_INTERFACE_VERSION", None) == DECODING_VERSION


class _Wrapped:
    """A module plus what the framework derives for it.

    Attribute reads fall through to the module, so ``NAME``, ``discover``,
    ``can_handle`` and the optional hooks are the module's own. A peek hook the
    module defines wins over the derived one. Derivations are read when used, not
    when wrapped, so a patched module is seen as patched.
    """

    def __init__(self, module: ModuleType) -> None:
        self._module = module
        # The module's own description, not this class's: documentation generated
        # from the registry reads ``__doc__`` as the adapter's.
        self.__doc__ = module.__doc__
        self._hooks: dict[str, Any] | None = None
        self._hooks_derived = False

    def __getattr__(self, name: str) -> Any:
        if name in _PEEK_HOOKS:
            if hasattr(self._module, name):
                return getattr(self._module, name)
            hooks = self._derived_peek()
            if hooks is None or name not in hooks:
                raise AttributeError(name)
            return hooks[name]
        return getattr(self._module, name)

    def _derived_peek(self) -> dict[str, Any] | None:
        """The derived peek hooks by name, or None when the adapter has none."""
        if not self._hooks_derived:
            self._hooks = self._derive_peek()
            self._hooks_derived = True
        return self._hooks

    def _derive_peek(self) -> dict[str, Any] | None:
        raise NotImplementedError


class DecodingAdapter(_Wrapped):
    """A version-2 adapter module: the framework assembles what it decodes."""

    @property
    def HARNESS(self) -> Harness:
        m = self._module
        return build_harness(
            m.NAME,
            m.HARNESS_SOURCE,
            getattr(m, "HARNESS_LOG_FORMAT", None),
            getattr(m, "HARNESS_DISPLAY_NAME", None),
        )

    def default_session(self, source: Source) -> Session | None:
        hook = getattr(self._module, "default_session", None)
        return hook(source) if hook is not None else None

    def assemble(self, source: Source, *, units: Iterable[Any] | None = None) -> Assembly:
        """Read, decode and assemble one source: its conversations and its ledger.

        ``units`` stands in for reading ``source`` when the caller already holds
        them (a preflight that validated the units before assembling).
        """
        m = self._module
        return assemble(
            source,
            read=m.read if units is None else (lambda _source: units),
            decoder=m.decoder,
            harness=self.HARNESS,
            default_session=self.default_session(source),
        )

    def parse(self, source: Source) -> Iterable[Conversation]:
        """The conversations of ``source``."""
        return self.assemble(source).conversations

    def _derive_peek(self) -> dict[str, Any] | None:
        """``peek_scan``, ``peek_exchanges`` and ``peek_tail`` from the module's declarations."""
        m = self._module
        if not getattr(m, "SUPPORTS_PEEK", True):
            return None

        def read(source: Source) -> Iterable[Any]:
            return getattr(m, "peek_read", m.read)(source)

        scan, exchanges, tail = make_peek_hooks_from_decoder(
            read,
            lambda source: m.decoder(source),
            self.HARNESS,
            default_session_for=lambda path: self.default_session(Source(kind="file", location=path)),
            tool_aliases=getattr(m, "TOOL_ALIASES", None),
            last_of_many=getattr(m, "PEEK_LAST_OF_MANY", False),
        )
        enrich = getattr(m, "peek_enrich", None)
        if enrich is not None:
            derived_scan = scan

            def enriched_scan(path: Path) -> PeekScanResult | None:
                result = derived_scan(path)
                return None if result is None else enrich(path, result)

            scan = enriched_scan
        return {"peek_scan": scan, "peek_exchanges": exchanges, "peek_tail": tail}


class LegacyAdapter(_Wrapped):
    """A version-1 module, wrapped so peek has one source of hooks.

    A version-1 module builds its own conversations, so ``parse`` falls through
    to it. Its peek scan and exchanges come from ``normalize_record`` through
    :func:`~siftd.adapters.sdk.make_peek_hooks`. ``peek_tail`` is not derived: a
    version-1 adapter with no tail hook has always shown the raw last lines.
    """

    def _derive_peek(self) -> dict[str, Any] | None:
        m = self._module
        normalize = getattr(m, "normalize_record", None)
        if normalize is None or not getattr(m, "SUPPORTS_PEEK", True):
            return None
        scan, exchanges, _tail = make_peek_hooks(
            normalize,
            tool_aliases=getattr(m, "TOOL_ALIASES", None),
            subagent_path_marker=getattr(m, "SUBAGENT_PATH_MARKER", None),
        )
        return {"peek_scan": scan, "peek_exchanges": exchanges}


def adapt(module: Any) -> Any:
    """``module`` as the adapter consumers use: wrapped, whichever version it is.

    Only a bare module is wrapped. Anything else is already an adapter: a wrapper
    this function made, or a proxy (the config path override) around one. So it
    is idempotent, and safe to apply at every boundary that accepts an adapter.
    """
    if not isinstance(module, ModuleType):
        return module
    return DecodingAdapter(module) if decodes(module) else LegacyAdapter(module)


__all__ = ["DecodingAdapter", "LegacyAdapter", "adapt", "decodes"]
