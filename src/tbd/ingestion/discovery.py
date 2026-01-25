"""Discovery: find sources across all adapters."""

from collections.abc import Iterable
from typing import Any

from tbd.domain import Source

# Adapter modules have NAME, DEDUP_STRATEGY, parse(), discover(), can_handle()
# as module-level attributes. Using Any since Python doesn't have a clean type
# for "module with specific attributes".
AdapterModule = Any


def discover_all(adapters: list[AdapterModule]) -> Iterable[tuple[Source, AdapterModule]]:
    """Yield (source, adapter) pairs for all discoverable files.

    Iterates through adapters, calls discover() on each, validates
    with can_handle(), yields pairs.
    """
    for adapter in adapters:
        for source in adapter.discover():
            if adapter.can_handle(source):
                yield source, adapter
