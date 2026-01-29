"""Discovery: find sources across all adapters."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from siftd.domain import Source

if TYPE_CHECKING:
    from siftd.ingestion import AdapterModule


def discover_all(adapters: list[AdapterModule]) -> Iterable[tuple[Source, AdapterModule]]:
    """Yield (source, adapter) pairs for all discoverable files.

    Iterates through adapters, calls discover() on each, validates
    with can_handle(), yields pairs.
    """
    for adapter in adapters:
        for source in adapter.discover():
            if adapter.can_handle(source):
                yield source, adapter
