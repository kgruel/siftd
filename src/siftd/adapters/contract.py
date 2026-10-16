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

The derived pieces live on a :class:`DecodingAdapter` that wraps the module, so
the registry hands every consumer an adapter with the whole interface and the
module itself stays declarations plus decoding. A version-1 module is returned
as it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from siftd.adapters.assembly import Assembly, assemble
from siftd.adapters.peek_projection import make_peek_hooks_from_decoder
from siftd.adapters.records import Session
from siftd.adapters.sdk import build_harness
from siftd.adapters.validation import DECODING_VERSION
from siftd.domain import Conversation, Harness
from siftd.domain.peek import PeekScanResult
from siftd.domain.source import Source

_PEEK_HOOKS = ("peek_scan", "peek_exchanges", "peek_tail")


def decodes(adapter: Any) -> bool:
    """True if ``adapter`` declares the decoding interface (version 2)."""
    return getattr(adapter, "ADAPTER_INTERFACE_VERSION", None) == DECODING_VERSION


class DecodingAdapter:
    """A version-2 adapter module plus everything the framework derives for it.

    Attribute reads fall through to the module, so ``NAME``, ``discover``,
    ``can_handle`` and the optional hooks are the module's own. Declarations are
    read when used, not when wrapped, so a patched module is seen as patched.
    """

    def __init__(self, module: ModuleType) -> None:
        self._module = module
        # The module's own description, not this class's: documentation generated
        # from the registry reads ``__doc__`` as the adapter's.
        self.__doc__ = module.__doc__
        self._peek: tuple | None | bool = False  # False: not derived yet

    def __getattr__(self, name: str) -> Any:
        if name in _PEEK_HOOKS:
            if hasattr(self._module, name):
                return getattr(self._module, name)
            hooks = self._derived_peek()
            if hooks is None:
                raise AttributeError(name)
            return hooks[_PEEK_HOOKS.index(name)]
        return getattr(self._module, name)

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

    def _derived_peek(self):
        """The derived ``(scan, exchanges, tail)``, or None when the adapter opts out of peek."""
        if self._peek is False:
            self._peek = self._derive_peek()
        return self._peek

    def _derive_peek(self):
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
        if enrich is None:
            return scan, exchanges, tail

        def enriched_scan(path: Path) -> PeekScanResult | None:
            result = scan(path)
            return None if result is None else enrich(path, result)

        return enriched_scan, exchanges, tail


def adapt(module: Any) -> Any:
    """``module`` as the adapter consumers use: wrapped if it is a decoding module, itself if not.

    Only a bare module is wrapped. Anything else is already an adapter: a wrapper
    this function made, or a proxy (the config path override) around one. So it
    is idempotent, and safe to apply at every boundary that accepts an adapter.
    """
    if isinstance(module, ModuleType) and decodes(module):
        return DecodingAdapter(module)
    return module


__all__ = ["DecodingAdapter", "adapt", "decodes"]
