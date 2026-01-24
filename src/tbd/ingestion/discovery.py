"""Discovery: find sources across all adapters."""

from typing import Iterable

from tbd.domain import Source


def discover_all(adapters: list) -> Iterable[tuple[Source, object]]:
    """Yield (source, adapter) pairs for all discoverable files.

    Iterates through adapters, calls discover() on each, validates
    with can_handle(), yields pairs.
    """
    for adapter in adapters:
        for source in adapter.discover():
            if adapter.can_handle(source):
                yield source, adapter
